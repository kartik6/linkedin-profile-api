"""Strategy 4: the public page, with no login at all.

LinkedIn puts schema.org markup on public profile pages so that search engines
can index them:

    <script type="application/ld+json">
      {"@graph":[{"@type":"Person","name":"...","worksFor":[...],"alumniOf":[...]}]}
    </script>

It holds far less than the Voyager payload. There are no skills, no
certifications and no descriptions for most roles. It needs no cookie though,
so it still answers when every session is dead or quarantined. We use it as a
floor: a thin answer beats an outage.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bs4 import BeautifulSoup

from app.errors import ProfileNotFound
from app.linkedin.client import LinkedInClient
from app.linkedin.dates import parse_date
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.text import text_of
from app.linkedin.urls import ProfileRef
from app.models import (
    Company,
    DateRange,
    Education,
    Experience,
    Image,
    ImageArtifact,
    Language,
    Location,
    Profile,
    School,
)

log = logging.getLogger(__name__)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _year_date(value: Any) -> Any:
    """schema.org dates arrive as '2021' or '2021-05' or '2021-05-01'."""
    if not isinstance(value, str) or not value[:4].isdigit():
        return None
    parts = value.split("-")
    return parse_date(
        {
            "year": int(parts[0]),
            "month": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
            "day": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
        }
    )


def _date_range(member: dict[str, Any]) -> DateRange | None:
    start = _year_date(member.get("startDate"))
    end = _year_date(member.get("endDate"))
    if start is None and end is None:
        return None
    from app.linkedin.dates import months_between, render_date

    is_current = end is None
    return DateRange(
        start=start,
        end=end,
        is_current=is_current,
        duration_months=months_between(start, end, is_current),
        text=f"{render_date(start) or ''} - {render_date(end) or 'Present'}".strip(" -"),
    )


def find_person(document: Any) -> dict[str, Any] | None:
    """Walk the JSON-LD graph and return the Person node."""
    nodes = _as_list(document)
    while nodes:
        node = nodes.pop(0)
        if not isinstance(node, dict):
            continue
        node_type = node.get("@type")
        types = _as_list(node_type)
        if "Person" in types:
            return node
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            nodes.extend(_as_list(node.get(key)))
    return None


def parse_jsonld(page_html: str, ref: ProfileRef) -> Profile | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            document = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        person = find_person(document)
        if person:
            return _person_to_profile(person, ref)
    return None


def _person_to_profile(person: dict[str, Any], ref: ProfileRef) -> Profile:
    name = text_of(person, "name")
    given = text_of(person, "givenName")
    family = text_of(person, "familyName")
    if name and not given:
        parts = name.split()
        given = parts[0]
        family = " ".join(parts[1:]) or None

    job_titles = [t for t in _as_list(person.get("jobTitle")) if isinstance(t, str)]

    address = person.get("address") if isinstance(person.get("address"), dict) else {}
    locality = text_of(address, "addressLocality")
    country = text_of(address, "addressCountry")
    location = None
    if locality or country:
        location = Location(
            full=", ".join(p for p in (locality, country) if p),
            city=locality,
            country=country,
        )

    image_url = None
    image = person.get("image")
    if isinstance(image, dict):
        image_url = image.get("contentUrl") or image.get("url")
    elif isinstance(image, str):
        image_url = image

    profile = Profile(
        public_identifier=ref.public_identifier,
        profile_url=ref.canonical_url,
        first_name=given,
        last_name=family,
        full_name=name,
        headline=job_titles[0] if job_titles else None,
        about=text_of(person, "description"),
        location=location,
        profile_picture=Image(url=image_url, artifacts=[ImageArtifact(url=image_url)])
        if image_url
        else None,
    )

    for index, org in enumerate(_as_list(person.get("worksFor"))):
        if not isinstance(org, dict):
            continue
        member = org.get("member") if isinstance(org.get("member"), dict) else {}
        # LinkedIn lists the roles in `jobTitle` in the same order as `worksFor`.
        aligned_title = job_titles[index] if index < len(job_titles) else None
        profile.experience.append(
            Experience(
                title=text_of(member, "jobTitle") or text_of(org, "jobTitle") or aligned_title,
                company=Company(name=text_of(org, "name"), linkedin_url=text_of(org, "url")),
                location=text_of(org.get("location"), "name")
                if isinstance(org.get("location"), dict)
                else text_of(org, "location"),
                description=text_of(member, "description"),
                date_range=_date_range(member),
            )
        )

    for org in _as_list(person.get("alumniOf")):
        if not isinstance(org, dict):
            continue
        member = org.get("member") if isinstance(org.get("member"), dict) else {}
        profile.education.append(
            Education(
                school=School(name=text_of(org, "name"), linkedin_url=text_of(org, "url")),
                degree=text_of(member, "degree") or text_of(org, "degree"),
                field_of_study=text_of(member, "fieldOfStudy"),
                date_range=_date_range(member),
            )
        )

    for language in _as_list(person.get("knowsLanguage")):
        label = text_of(language, "name") if isinstance(language, dict) else text_of(language)
        if label:
            profile.languages.append(Language(name=label))

    return profile


class PublicJSONLDStrategy(Strategy):
    name = "public_jsonld"
    needs_auth = False
    description = "Read the schema.org markup on the logged out public page."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        page = await client.get_html(
            client.page_url(ref.public_identifier), authenticated=False
        )
        profile = parse_jsonld(page, ref)
        if profile is None:
            raise ProfileNotFound("The public page carried no schema.org Person markup.")
        return StrategyResult(
            name=self.name,
            profile=profile,
            warnings=[
                "Public markup only. Skills, certifications and role descriptions are absent."
            ],
        )
