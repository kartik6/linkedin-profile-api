"""Turn raw LinkedIn payloads into our Profile model.

Two entry points cover every strategy:

  from_entity_pool   the flat entity graph (dash REST, GraphQL, embedded HTML)
  from_profile_view  the older nested /profileView document

Both are defensive on purpose. LinkedIn renames and moves fields without
notice, so every read tries several key names and every section falls back to
an empty list. A shape change should cost us one section, never the request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.linkedin.components import card_section, read_entity, split_parts, walk_entities
from app.linkedin.dates import parse_date, parse_date_range
from app.linkedin.entities import EntityPool
from app.linkedin.images import extract_image
from app.linkedin.text import text_of
from app.linkedin.urls import ProfileRef
from app.models import (
    Certification,
    Company,
    Course,
    Education,
    EmploymentType,
    Experience,
    Honor,
    Language,
    Location,
    Organization,
    Patent,
    Profile,
    Project,
    Publication,
    School,
    Skill,
    TestScore,
    Volunteer,
)

_EMPLOYMENT_TYPES = {
    "full-time": EmploymentType.full_time,
    "full time": EmploymentType.full_time,
    "part-time": EmploymentType.part_time,
    "part time": EmploymentType.part_time,
    "contract": EmploymentType.contract,
    "internship": EmploymentType.internship,
    "freelance": EmploymentType.freelance,
    "self-employed": EmploymentType.self_employed,
    "self employed": EmploymentType.self_employed,
    "apprenticeship": EmploymentType.apprenticeship,
    "seasonal": EmploymentType.seasonal,
}

_WORKPLACE_TYPES = {
    "on-site": "ON_SITE",
    "onsite": "ON_SITE",
    "on site": "ON_SITE",
    "remote": "REMOTE",
    "hybrid": "HYBRID",
}


def _employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    return _EMPLOYMENT_TYPES.get(value.strip().lower())


def _workplace_type(value: str | None) -> str | None:
    if not value:
        return None
    return _WORKPLACE_TYPES.get(value.strip().lower())


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """Run one section reader. A broken section must not fail the request."""
    try:
        result = fn()
        return default if result is None else result
    except Exception:  # noqa: BLE001 - deliberate: sections are independent
        return default


def _company_url(urn: str | None, universal_name: str | None) -> str | None:
    if universal_name:
        return f"https://www.linkedin.com/company/{universal_name}/"
    if urn and ":" in urn:
        return f"https://www.linkedin.com/company/{urn.rsplit(':', 1)[-1]}/"
    return None


def _school_url(urn: str | None, universal_name: str | None) -> str | None:
    if universal_name:
        return f"https://www.linkedin.com/school/{universal_name}/"
    if urn and ":" in urn:
        return f"https://www.linkedin.com/school/{urn.rsplit(':', 1)[-1]}/"
    return None


# ==========================================================================
# Strategy A and B and C: the flat entity graph
# ==========================================================================


def from_entity_pool(pool: EntityPool, ref: ProfileRef) -> Profile:
    profile = _pool_top_card(pool, ref)

    profile.experience = _safe(lambda: _pool_experience(pool), [])
    profile.education = _safe(lambda: _pool_education(pool), [])
    profile.skills = _safe(lambda: _pool_skills(pool), [])
    profile.certifications = _safe(lambda: _pool_certifications(pool), [])
    profile.languages = _safe(lambda: _pool_languages(pool), [])
    profile.projects = _safe(lambda: _pool_projects(pool), [])
    profile.publications = _safe(lambda: _pool_publications(pool), [])
    profile.honors = _safe(lambda: _pool_honors(pool), [])
    profile.volunteering = _safe(lambda: _pool_volunteering(pool), [])
    profile.courses = _safe(lambda: _pool_courses(pool), [])
    profile.patents = _safe(lambda: _pool_patents(pool), [])
    profile.organizations = _safe(lambda: _pool_organizations(pool), [])
    profile.test_scores = _safe(lambda: _pool_test_scores(pool), [])

    # Fill the gaps from the rendered card tree.
    _safe(lambda: _fill_from_cards(pool, profile), None)
    return profile


def _find_profile_entity(pool: EntityPool, ref: ProfileRef) -> dict[str, Any] | None:
    candidates = pool.by_type("Profile", "MiniProfile")
    if not candidates:
        return None
    # Prefer the entity whose public identifier matches the request.
    wanted = ref.public_identifier.lower()
    for entity in candidates:
        if str(entity.get("publicIdentifier", "")).lower() == wanted:
            return entity
    # Otherwise take the richest one.
    return max(candidates, key=lambda e: len(e))


def _pool_top_card(pool: EntityPool, ref: ProfileRef) -> Profile:
    entity = _find_profile_entity(pool, ref) or {}

    first = text_of(entity, "firstName")
    last = text_of(entity, "lastName")
    full = " ".join(p for p in (first, last) if p) or None

    industry = None
    industry_entity = pool.linked(entity, "industry", "industryV2")
    if industry_entity:
        industry = text_of(industry_entity, "name")
    industry = industry or text_of(entity, "industryName")

    location = _pool_location(pool, entity)

    profile_picture = extract_image(entity.get("profilePicture")) or extract_image(
        entity.get("picture")
    )
    background = (
        extract_image(entity.get("backgroundPicture"))
        or extract_image(entity.get("backgroundImage"))
        or extract_image(entity.get("backgroundImageReference"))
    )

    return Profile(
        public_identifier=text_of(entity, "publicIdentifier") or ref.public_identifier,
        urn=entity.get("entityUrn") or entity.get("objectUrn"),
        profile_url=ref.canonical_url,
        first_name=first,
        last_name=last,
        full_name=full,
        pronouns=_pool_pronouns(entity),
        headline=text_of(entity, "headline") or text_of(entity, "occupation"),
        about=text_of(entity, "summary") or text_of(entity, "about"),
        industry=industry,
        location=location,
        profile_picture=profile_picture,
        background_picture=background,
        follower_count=_pool_follower_count(pool, entity),
        connection_count=_pool_connection_count(pool, entity),
        connection_degree=_pool_degree(pool),
        open_to_work=_pool_open_to_work(entity),
        is_hiring=_pool_hiring(entity),
        is_premium=entity.get("premium"),
        is_influencer=entity.get("influencer"),
        is_verified=_pool_verified(entity),
    )


def _pool_pronouns(entity: dict[str, Any]) -> str | None:
    value = entity.get("pronoun") or entity.get("standardizedPronoun")
    text = text_of(value) or text_of(entity, "customPronoun")
    if isinstance(text, str) and text.isupper():
        return text.replace("_", "/").lower()
    return text


def _pool_location(pool: EntityPool, entity: dict[str, Any]) -> Location | None:
    full = (
        text_of(entity, "geoLocationName")
        or text_of(entity, "locationName")
        or text_of(entity.get("location"), "defaultLocalizedName")
    )
    country_code = None
    basic = entity.get("location")
    if isinstance(basic, dict):
        basic_location = basic.get("basicLocation")
        if isinstance(basic_location, dict):
            country_code = basic_location.get("countryCode")
    country = text_of(entity, "geoCountryName")

    geo = pool.linked(entity, "geoLocation", "geo")
    if geo and not full:
        full = text_of(geo, "defaultLocalizedName") or text_of(geo, "name")

    city = None
    if full and "," in full:
        city = full.split(",")[0].strip()
        if not country:
            country = full.split(",")[-1].strip()

    if not any((full, city, country, country_code)):
        return None
    return Location(
        full=full,
        city=city,
        country=country,
        country_code=country_code.upper() if country_code else None,
    )


def _pool_follower_count(pool: EntityPool, entity: dict[str, Any]) -> int | None:
    value = entity.get("followerCount")
    if isinstance(value, int):
        return value
    for state in pool.by_type("FollowingState"):
        count = state.get("followerCount")
        if isinstance(count, int):
            return count
    return None


def _pool_connection_count(pool: EntityPool, entity: dict[str, Any]) -> int | None:
    for key in ("connectionsCount", "connectionCount"):
        if isinstance(entity.get(key), int):
            return entity[key]
    connections = entity.get("connections")
    if isinstance(connections, dict):
        paging = connections.get("paging")
        if isinstance(paging, dict) and isinstance(paging.get("total"), int):
            return paging["total"]
    for card in pool.by_type("ProfileTopCard"):
        count = card.get("connectionsCount")
        if isinstance(count, int):
            return count
    return None


def _pool_degree(pool: EntityPool) -> str | None:
    for rel in pool.by_type("MemberRelationship", "MemberDistance"):
        value = rel.get("distance") or rel.get("memberDistance")
        text = text_of(value) if not isinstance(value, str) else value
        if isinstance(text, str) and "DISTANCE" in text.upper():
            return text.upper().replace("DISTANCE_", "").replace("_", " ")
    return None


def _pool_open_to_work(entity: dict[str, Any]) -> bool | None:
    frame = entity.get("profilePicture")
    if isinstance(frame, dict):
        frame_type = frame.get("frameType") or frame.get("photoFrameType")
        if isinstance(frame_type, str):
            return "OPEN_TO_WORK" in frame_type.upper()
    if "openToWork" in entity:
        return bool(entity["openToWork"])
    return None


def _pool_hiring(entity: dict[str, Any]) -> bool | None:
    frame = entity.get("profilePicture")
    if isinstance(frame, dict):
        frame_type = frame.get("frameType") or frame.get("photoFrameType")
        if isinstance(frame_type, str):
            return "HIRING" in frame_type.upper()
    return None


def _pool_verified(entity: dict[str, Any]) -> bool | None:
    for key in ("verified", "isVerified"):
        if key in entity:
            return bool(entity[key])
    badges = entity.get("memberBadges")
    if isinstance(badges, dict) and "verified" in badges:
        return bool(badges["verified"])
    return None


def _pool_company(pool: EntityPool, entity: dict[str, Any]) -> Company | None:
    company_entity = pool.linked(entity, "company", "companyResolutionResult", "miniCompany")
    name = text_of(entity, "companyName") or (
        text_of(company_entity, "name") if company_entity else None
    )
    urn = entity.get("companyUrn") or entity.get("*company")
    if company_entity and not urn:
        urn = company_entity.get("entityUrn")
    if not isinstance(urn, str):
        urn = None

    logo = None
    universal_name = None
    industry = None
    staff_count = None
    if company_entity:
        logo = extract_image(company_entity.get("logo")) or extract_image(company_entity)
        universal_name = company_entity.get("universalName")
        staff_count = company_entity.get("staffCount")
        industries = company_entity.get("industries")
        if isinstance(industries, list) and industries:
            industry = text_of(industries[0])

    if not any((name, urn, logo)):
        return None
    return Company(
        name=name,
        urn=urn,
        linkedin_url=_company_url(urn, universal_name),
        logo=logo,
        industry=industry,
        staff_count=staff_count,
    )


def _pool_school(pool: EntityPool, entity: dict[str, Any]) -> School | None:
    school_entity = pool.linked(entity, "school", "schoolResolutionResult", "miniSchool")
    name = text_of(entity, "schoolName") or (
        text_of(school_entity, "name") if school_entity else None
    )
    urn = entity.get("schoolUrn") or entity.get("*school")
    if school_entity and not urn:
        urn = school_entity.get("entityUrn")
    if not isinstance(urn, str):
        urn = None

    logo = None
    universal_name = None
    if school_entity:
        logo = extract_image(school_entity.get("logo")) or extract_image(school_entity)
        universal_name = school_entity.get("universalName")

    if not any((name, urn, logo)):
        return None
    return School(
        name=name,
        urn=urn,
        linkedin_url=_school_url(urn, universal_name),
        logo=logo,
    )


def _pool_experience(pool: EntityPool) -> list[Experience]:
    out: list[Experience] = []
    for entity in pool.by_type("Position"):
        date_range = parse_date_range(entity.get("dateRange") or entity.get("timePeriod"))
        employment = _employment_type(
            text_of(entity, "employmentTypeName")
            or text_of(pool.linked(entity, "employmentType") or {}, "name")
        )
        skills = [
            text_of(s, "name") or text_of(s)
            for s in pool.resolve_many(entity.get("*profileSkills") or [])
        ]
        out.append(
            Experience(
                title=text_of(entity, "title"),
                company=_pool_company(pool, entity),
                employment_type=employment,
                location=text_of(entity, "locationName") or text_of(entity, "geoLocationName"),
                workplace_type=_workplace_type(text_of(entity, "workplaceTypeName")),
                description=text_of(entity, "description"),
                date_range=date_range,
                skills=[s for s in skills if s],
            )
        )
    return out


def _pool_education(pool: EntityPool) -> list[Education]:
    out: list[Education] = []
    for entity in pool.by_type("Education"):
        out.append(
            Education(
                school=_pool_school(pool, entity),
                degree=text_of(entity, "degreeName"),
                field_of_study=text_of(entity, "fieldOfStudy"),
                grade=text_of(entity, "grade"),
                activities=text_of(entity, "activities"),
                description=text_of(entity, "description"),
                date_range=parse_date_range(
                    entity.get("dateRange") or entity.get("timePeriod")
                ),
            )
        )
    return out


def _pool_skills(pool: EntityPool) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for entity in pool.by_type("Skill"):
        name = text_of(entity, "name")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        count = entity.get("endorsementCount")
        if not isinstance(count, int):
            endorsements = entity.get("endorsedByProfiles")
            count = len(endorsements) if isinstance(endorsements, list) else None
        out.append(Skill(name=name, endorsement_count=count))
    return out


def _pool_certifications(pool: EntityPool) -> list[Certification]:
    out: list[Certification] = []
    for entity in pool.by_type("Certification"):
        date_range = parse_date_range(entity.get("dateRange") or entity.get("timePeriod"))
        company = pool.linked(entity, "company", "companyResolutionResult")
        out.append(
            Certification(
                name=text_of(entity, "name"),
                authority=text_of(entity, "authority")
                or (text_of(company, "name") if company else None),
                license_number=text_of(entity, "licenseNumber"),
                url=text_of(entity, "url"),
                logo=extract_image(company) if company else None,
                issued_on=date_range.start if date_range else None,
                expires_on=date_range.end if date_range else None,
            )
        )
    return out


def _pool_languages(pool: EntityPool) -> list[Language]:
    out: list[Language] = []
    for entity in pool.by_type("Language"):
        name = text_of(entity, "name")
        if not name:
            continue
        proficiency = text_of(entity, "proficiency")
        if proficiency and proficiency.isupper():
            proficiency = proficiency.replace("_", " ").title()
        out.append(Language(name=name, proficiency=proficiency))
    return out


def _pool_projects(pool: EntityPool) -> list[Project]:
    return [
        Project(
            name=text_of(e, "title") or text_of(e, "name"),
            description=text_of(e, "description"),
            url=text_of(e, "url"),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
            members=[
                text_of(m, "fullName") or text_of(m, "name") or ""
                for m in (e.get("contributors") or e.get("members") or [])
            ],
        )
        for e in pool.by_type("Project")
    ]


def _pool_publications(pool: EntityPool) -> list[Publication]:
    return [
        Publication(
            name=text_of(e, "name") or text_of(e, "title"),
            publisher=text_of(e, "publisher"),
            description=text_of(e, "description"),
            url=text_of(e, "url"),
            published_on=parse_date(e.get("date") or e.get("publishedOn")),
            authors=[
                text_of(a, "fullName") or text_of(a, "name") or ""
                for a in (e.get("authors") or [])
            ],
        )
        for e in pool.by_type("Publication")
    ]


def _pool_honors(pool: EntityPool) -> list[Honor]:
    return [
        Honor(
            title=text_of(e, "title") or text_of(e, "name"),
            issuer=text_of(e, "issuer"),
            description=text_of(e, "description"),
            issued_on=parse_date(e.get("issueDate") or e.get("issuedOn")),
        )
        for e in pool.by_type("Honor")
    ]


def _pool_volunteering(pool: EntityPool) -> list[Volunteer]:
    return [
        Volunteer(
            role=text_of(e, "role") or text_of(e, "title"),
            organization=text_of(e, "companyName") or text_of(e, "organizationName"),
            cause=text_of(e, "cause"),
            description=text_of(e, "description"),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
        )
        for e in pool.by_type("VolunteerExperience")
    ]


def _pool_courses(pool: EntityPool) -> list[Course]:
    return [
        Course(name=text_of(e, "name"), number=text_of(e, "number"))
        for e in pool.by_type("Course")
    ]


def _pool_patents(pool: EntityPool) -> list[Patent]:
    return [
        Patent(
            title=text_of(e, "title") or text_of(e, "name"),
            number=text_of(e, "number") or text_of(e, "applicationNumber"),
            description=text_of(e, "description"),
            url=text_of(e, "url"),
            issued_on=parse_date(e.get("issueDate") or e.get("filingDate")),
            inventors=[
                text_of(i, "fullName") or text_of(i, "name") or ""
                for i in (e.get("inventors") or [])
            ],
        )
        for e in pool.by_type("Patent")
    ]


def _pool_organizations(pool: EntityPool) -> list[Organization]:
    return [
        Organization(
            name=text_of(e, "name") or text_of(e, "organizationName"),
            position=text_of(e, "position"),
            description=text_of(e, "description"),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
        )
        for e in pool.by_type("Organization")
    ]


def _pool_test_scores(pool: EntityPool) -> list[TestScore]:
    return [
        TestScore(
            name=text_of(e, "name"),
            score=text_of(e, "score"),
            description=text_of(e, "description"),
            taken_on=parse_date(e.get("date") or e.get("dateOn")),
        )
        for e in pool.by_type("TestScore")
    ]


# --------------------------------------------------------------------------
# Card tree fallback
# --------------------------------------------------------------------------


def _fill_from_cards(pool: EntityPool, profile: Profile) -> None:
    """Read the rendered cards for any section the typed entities missed."""
    cards = pool.by_type("Card", "ProfileCard")
    if not cards:
        return

    for card in cards:
        section = card_section(card)
        if section == "experience" and not profile.experience:
            profile.experience = _cards_to_experience(card)
        elif section == "education" and not profile.education:
            profile.education = _cards_to_education(card)
        elif section in {"skills", "skill_details"} and not profile.skills:
            profile.skills = _cards_to_skills(card)
        elif section in {"licenses_and_certifications", "certifications"} and (
            not profile.certifications
        ):
            profile.certifications = _cards_to_certifications(card)
        elif section == "languages" and not profile.languages:
            profile.languages = _cards_to_languages(card)
        elif section == "about" and not profile.about:
            texts = [read_entity(e).get("description") for e in walk_entities(card)]
            profile.about = next((t for t in texts if t), None)


def _cards_to_experience(card: dict[str, Any]) -> list[Experience]:
    out: list[Experience] = []
    for entity in walk_entities(card):
        item = read_entity(entity)
        if not item["title"]:
            continue
        subtitle_parts = split_parts(item["subtitle"])
        caption_parts = split_parts(item["caption"])
        company_name = subtitle_parts[0] if subtitle_parts else None
        employment = None
        for part in subtitle_parts[1:]:
            employment = employment or _employment_type(part)
        out.append(
            Experience(
                title=item["title"],
                company=Company(name=company_name, logo=extract_image(item["image"]))
                if company_name
                else None,
                employment_type=employment,
                location=item["metadata"],
                description=item["description"],
                date_range=_range_from_caption(caption_parts),
            )
        )
    return out


def _cards_to_education(card: dict[str, Any]) -> list[Education]:
    out: list[Education] = []
    for entity in walk_entities(card):
        item = read_entity(entity)
        if not item["title"]:
            continue
        subtitle_parts = split_parts(item["subtitle"])
        out.append(
            Education(
                school=School(name=item["title"], logo=extract_image(item["image"])),
                degree=subtitle_parts[0] if subtitle_parts else None,
                field_of_study=subtitle_parts[1] if len(subtitle_parts) > 1 else None,
                description=item["description"],
                date_range=_range_from_caption(split_parts(item["caption"])),
            )
        )
    return out


def _cards_to_skills(card: dict[str, Any]) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for entity in walk_entities(card):
        name = read_entity(entity)["title"]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(Skill(name=name))
    return out


def _cards_to_certifications(card: dict[str, Any]) -> list[Certification]:
    out: list[Certification] = []
    for entity in walk_entities(card):
        item = read_entity(entity)
        if not item["title"]:
            continue
        out.append(
            Certification(
                name=item["title"],
                authority=(split_parts(item["subtitle"]) or [None])[0],
                logo=extract_image(item["image"]),
                issued_on=_date_from_caption(item["caption"]),
            )
        )
    return out


def _cards_to_languages(card: dict[str, Any]) -> list[Language]:
    out: list[Language] = []
    for entity in walk_entities(card):
        item = read_entity(entity)
        if item["title"]:
            out.append(Language(name=item["title"], proficiency=item["subtitle"]))
    return out


def _range_from_caption(parts: list[str]) -> Any:
    """Read 'May 2021 - Present' out of a rendered caption string."""
    from app.linkedin.dates import DateRange, months_between

    if not parts:
        return None
    span = parts[0]
    pieces = [p.strip() for p in span.replace("–", "-").split("-")]
    if len(pieces) < 2:
        return None
    start = _parse_text_date(pieces[0])
    end_text = pieces[1]
    is_current = end_text.lower() in {"present", "current", "now"}
    end = None if is_current else _parse_text_date(end_text)
    if start is None and end is None:
        return None
    return DateRange(
        start=start,
        end=end,
        is_current=is_current,
        duration_months=months_between(start, end, is_current),
        text=span,
    )


def _date_from_caption(caption: str | None) -> Any:
    if not caption:
        return None
    cleaned = caption.replace("Issued", "").replace("Expired", "").strip()
    return _parse_text_date(cleaned)


def _parse_text_date(value: str) -> Any:
    """Read 'May 2021' or '2021' into a Date."""
    from app.linkedin.dates import _MONTHS, Date, render_date

    if not value:
        return None
    tokens = value.replace(",", " ").split()
    year = next((int(t) for t in tokens if t.isdigit() and len(t) == 4), None)
    month = None
    for token in tokens:
        for index, name in enumerate(_MONTHS, start=1):
            if name.lower().startswith(token.lower()[:3]) and len(token) >= 3:
                month = index
                break
        if month:
            break
    if year is None and month is None:
        return None
    date = Date(year=year, month=month)
    date.text = render_date(date) or value
    return date


# ==========================================================================
# Strategy A: the older nested /profileView document
# ==========================================================================


def _elements(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    view = data.get(key)
    if isinstance(view, dict):
        elements = view.get("elements")
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, dict)]
    return []


def _country_code(base: dict[str, Any]) -> str | None:
    location = base.get("location")
    if isinstance(location, dict):
        basic = location.get("basicLocation")
        if isinstance(basic, dict) and basic.get("countryCode"):
            return str(basic["countryCode"]).upper()
    return None


def _view_company(entity: dict[str, Any]) -> Company | None:
    company = entity.get("company") if isinstance(entity.get("company"), dict) else {}
    mini = company.get("miniCompany") if isinstance(company.get("miniCompany"), dict) else {}
    name = text_of(entity, "companyName") or text_of(mini, "name")
    urn = entity.get("companyUrn") or mini.get("entityUrn") or mini.get("objectUrn")
    industries = company.get("industries")
    if not any((name, urn)):
        return None
    return Company(
        name=name,
        urn=urn if isinstance(urn, str) else None,
        linkedin_url=_company_url(urn if isinstance(urn, str) else None, mini.get("universalName")),
        logo=extract_image(mini.get("logo")) or extract_image(mini),
        industry=text_of(industries[0]) if isinstance(industries, list) and industries else None,
        staff_count=company.get("staffCount"),
    )


def _view_school(entity: dict[str, Any]) -> School | None:
    school = entity.get("school") if isinstance(entity.get("school"), dict) else {}
    name = text_of(entity, "schoolName") or text_of(school, "schoolName") or text_of(school, "name")
    urn = entity.get("schoolUrn") or school.get("entityUrn") or school.get("objectUrn")
    if not any((name, urn)):
        return None
    return School(
        name=name,
        urn=urn if isinstance(urn, str) else None,
        linkedin_url=_school_url(
            urn if isinstance(urn, str) else None, school.get("universalName")
        ),
        logo=extract_image(school.get("logo")) or extract_image(school),
    )


def _view_certification(entity: dict[str, Any]) -> Certification:
    date_range = parse_date_range(entity.get("timePeriod"))
    company = entity.get("company") if isinstance(entity.get("company"), dict) else {}
    return Certification(
        name=text_of(entity, "name"),
        authority=text_of(entity, "authority"),
        license_number=text_of(entity, "licenseNumber"),
        url=text_of(entity, "url"),
        logo=extract_image(company.get("miniCompany")) or extract_image(company),
        issued_on=date_range.start if date_range else None,
        expires_on=date_range.end if date_range else None,
    )


def from_profile_view(data: dict[str, Any], ref: ProfileRef) -> Profile:
    """Read the /identity/profiles/{id}/profileView document."""
    base = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    mini = base.get("miniProfile") if isinstance(base.get("miniProfile"), dict) else {}

    first = text_of(base, "firstName") or text_of(mini, "firstName")
    last = text_of(base, "lastName") or text_of(mini, "lastName")
    full = " ".join(p for p in (first, last) if p) or None

    location_name = text_of(base, "geoLocationName") or text_of(base, "locationName")
    country = text_of(base, "geoCountryName")
    location = None
    if location_name or country:
        city = None
        if location_name and "," in location_name:
            city = location_name.split(",")[0].strip()
        location = Location(
            full=location_name,
            city=city,
            country=country,
            country_code=_country_code(base),
        )

    profile = Profile(
        public_identifier=text_of(base, "publicIdentifier")
        or text_of(mini, "publicIdentifier")
        or ref.public_identifier,
        urn=mini.get("objectUrn") or base.get("entityUrn"),
        profile_url=ref.canonical_url,
        first_name=first,
        last_name=last,
        full_name=full,
        headline=text_of(base, "headline") or text_of(mini, "occupation"),
        about=text_of(base, "summary"),
        industry=text_of(base, "industryName"),
        location=location,
        profile_picture=extract_image(mini.get("picture")) or extract_image(base.get("picture")),
        background_picture=extract_image(mini.get("backgroundImage"))
        or extract_image(base.get("backgroundImage")),
    )

    profile.experience = _safe(
        lambda: [
            Experience(
                title=text_of(e, "title"),
                company=_view_company(e),
                location=text_of(e, "locationName"),
                description=text_of(e, "description"),
                date_range=parse_date_range(e.get("timePeriod")),
            )
            for e in _elements(data, "positionView")
        ],
        [],
    )
    profile.education = _safe(
        lambda: [
            Education(
                school=_view_school(e),
                degree=text_of(e, "degreeName"),
                field_of_study=text_of(e, "fieldOfStudy"),
                grade=text_of(e, "grade"),
                activities=text_of(e, "activities"),
                description=text_of(e, "description"),
                date_range=parse_date_range(e.get("timePeriod")),
            )
            for e in _elements(data, "educationView")
        ],
        [],
    )
    profile.skills = _safe(
        lambda: [
            Skill(name=text_of(e, "name"), endorsement_count=e.get("endorsementCount"))
            for e in _elements(data, "skillView")
            if text_of(e, "name")
        ],
        [],
    )
    profile.certifications = _safe(
        lambda: [_view_certification(e) for e in _elements(data, "certificationView")], []
    )
    profile.languages = _safe(
        lambda: [
            Language(
                name=text_of(e, "name"),
                proficiency=(text_of(e, "proficiency") or "").replace("_", " ").title() or None,
            )
            for e in _elements(data, "languageView")
            if text_of(e, "name")
        ],
        [],
    )
    profile.projects = _safe(
        lambda: [
            Project(
                name=text_of(e, "title") or text_of(e, "name"),
                description=text_of(e, "description"),
                url=text_of(e, "url"),
                date_range=parse_date_range(e.get("timePeriod")),
            )
            for e in _elements(data, "projectView")
        ],
        [],
    )
    profile.publications = _safe(
        lambda: [
            Publication(
                name=text_of(e, "name"),
                publisher=text_of(e, "publisher"),
                description=text_of(e, "description"),
                url=text_of(e, "url"),
                published_on=parse_date(e.get("date")),
            )
            for e in _elements(data, "publicationView")
        ],
        [],
    )
    profile.honors = _safe(
        lambda: [
            Honor(
                title=text_of(e, "title"),
                issuer=text_of(e, "issuer"),
                description=text_of(e, "description"),
                issued_on=parse_date(e.get("issueDate")),
            )
            for e in _elements(data, "honorView")
        ],
        [],
    )
    profile.volunteering = _safe(
        lambda: [
            Volunteer(
                role=text_of(e, "role"),
                organization=text_of(e, "companyName"),
                cause=text_of(e, "cause"),
                description=text_of(e, "description"),
                date_range=parse_date_range(e.get("timePeriod")),
            )
            for e in _elements(data, "volunteerExperienceView")
        ],
        [],
    )
    profile.courses = _safe(
        lambda: [
            Course(name=text_of(e, "name"), number=text_of(e, "number"))
            for e in _elements(data, "courseView")
        ],
        [],
    )
    profile.patents = _safe(
        lambda: [
            Patent(
                title=text_of(e, "title"),
                number=text_of(e, "number"),
                description=text_of(e, "description"),
                url=text_of(e, "url"),
                issued_on=parse_date(e.get("issueDate")),
            )
            for e in _elements(data, "patentView")
        ],
        [],
    )
    profile.organizations = _safe(
        lambda: [
            Organization(
                name=text_of(e, "name"),
                position=text_of(e, "position"),
                description=text_of(e, "description"),
                date_range=parse_date_range(e.get("timePeriod")),
            )
            for e in _elements(data, "organizationView")
        ],
        [],
    )
    profile.test_scores = _safe(
        lambda: [
            TestScore(
                name=text_of(e, "name"),
                score=text_of(e, "score"),
                description=text_of(e, "description"),
                taken_on=parse_date(e.get("date")),
            )
            for e in _elements(data, "testScoreView")
        ],
        [],
    )
    return profile


# ==========================================================================
# Merge and score
# ==========================================================================

CORE_SECTIONS = (
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
)

SCALAR_FIELDS = (
    "first_name",
    "last_name",
    "headline",
    "about",
    "location",
    "industry",
    "profile_picture",
)


def merge_profiles(base: Profile, extra: Profile) -> Profile:
    """Fill empty fields in `base` from `extra`. `base` always wins."""
    for name in base.model_fields:
        current = getattr(base, name)
        incoming = getattr(extra, name, None)
        if incoming in (None, [], ""):
            continue
        if current in (None, [], ""):
            setattr(base, name, incoming)
    return base


def completeness(profile: Profile) -> float:
    """Score how much of the profile came back, from 0.0 to 1.0.

    Callers use this to decide whether a result is worth keeping. The
    orchestrator uses it to decide whether to try the next strategy.
    """
    scalar_hits = sum(1 for f in SCALAR_FIELDS if getattr(profile, f, None))
    section_hits = sum(1 for s in CORE_SECTIONS if getattr(profile, s, None))
    total = len(SCALAR_FIELDS) + len(CORE_SECTIONS)
    return round((scalar_hits + section_hits) / total, 3)


def missing_sections(profile: Profile) -> list[str]:
    return [s for s in CORE_SECTIONS if not getattr(profile, s, None)]
