"""Turn user input into a public identifier.

LinkedIn profile URLs come in many shapes. Callers paste tracking parameters,
country subdomains, locale suffixes and percent encoded names. This module
reduces all of them to the one token the Voyager API needs: the public
identifier, also called the vanity name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from app.errors import InvalidProfileURL

_HOST_RE = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$", re.IGNORECASE)
_PUBLIC_ID_RE = re.compile(r"^[\w\-À-￿'’.%]{2,120}$", re.UNICODE)
_URN_RE = re.compile(r"urn:li:(?:fs_|fsd_)?(?:miniProfile|profile|member)?:?([A-Za-z0-9_\-]+)")

# Paths that look like a profile but are not one.
_NON_PROFILE_SEGMENTS = {
    "company", "school", "showcase", "groups", "feed", "posts", "jobs",
    "learning", "events", "newsletters", "pulse", "help", "legal", "sales",
    "talent", "premium", "checkpoint", "uas", "login", "signup", "mynetwork",
}


@dataclass(frozen=True)
class ProfileRef:
    """A resolved reference to one profile."""

    public_identifier: str
    canonical_url: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.public_identifier


def _canonical(public_id: str) -> str:
    return f"https://www.linkedin.com/in/{public_id}/"


def parse_profile_url(value: str) -> ProfileRef:
    """Resolve any accepted input into a ProfileRef.

    Accepted forms:
      - a full profile URL, with or without a scheme, subdomain or query string
      - an old style /pub/ URL
      - a bare public identifier, for example "satyanadella"
      - a member URN

    Raises InvalidProfileURL when the input points at something else.
    """
    if not value or not value.strip():
        raise InvalidProfileURL("The url field is empty.")

    raw = value.strip()

    # A member URN is already an identifier. The Voyager client can use it.
    if raw.lower().startswith("urn:li:"):
        match = _URN_RE.search(raw)
        if not match:
            raise InvalidProfileURL(f"Cannot read a member id from the URN {raw!r}.")
        return ProfileRef(match.group(1), _canonical(match.group(1)), "urn")

    # A bare identifier has no dot and no slash.
    if "/" not in raw and "." not in raw:
        if not _PUBLIC_ID_RE.match(raw):
            raise InvalidProfileURL(f"{raw!r} is not a valid public identifier.")
        return ProfileRef(raw, _canonical(raw), "public_identifier")

    candidate = raw if "//" in raw else f"https://{raw.lstrip('/')}"
    parsed = urlparse(candidate)

    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    if not _HOST_RE.match(host):
        raise InvalidProfileURL(f"{host or raw!r} is not a linkedin.com host.")

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise InvalidProfileURL("The URL has no path. Point it at a profile.")

    head = segments[0].lower()

    if head in _NON_PROFILE_SEGMENTS:
        raise InvalidProfileURL(
            f"This URL points at a {head} page, not a member profile."
        )

    if head == "in":
        if len(segments) < 2:
            raise InvalidProfileURL("The URL has no public identifier after /in/.")
        public_id = unquote(segments[1])
    elif head == "pub":
        # Old format: /pub/first-last/1/a2b/3c4
        if len(segments) < 2:
            raise InvalidProfileURL("The /pub/ URL has no name segment.")
        public_id = unquote(segments[1])
    else:
        raise InvalidProfileURL(
            "The URL is not a profile URL. A profile path starts with /in/."
        )

    public_id = public_id.strip("/").strip()
    if not public_id or not _PUBLIC_ID_RE.match(public_id):
        raise InvalidProfileURL(f"{public_id!r} is not a valid public identifier.")

    return ProfileRef(public_id, _canonical(public_id), "url")


def is_profile_url(value: str) -> bool:
    try:
        parse_profile_url(value)
        return True
    except InvalidProfileURL:
        return False
