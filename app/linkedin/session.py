"""Hold the LinkedIn cookies and build the headers Voyager expects.

The web app authenticates with two cookies:

  li_at        the member session token
  JSESSIONID   a value shaped like "ajax:1234567890123456789"

Voyager also checks a CSRF header. Its value is the JSESSIONID with the quotes
removed. A request without a matching pair gets a 401, so the two must always
travel together.

We keep a pool of sessions. One dead cookie then costs us one account, not the
whole service.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.errors import ChallengeRequired, NoSessionConfigured

log = logging.getLogger(__name__)

_JSESSIONID_RE = re.compile(r'"?(ajax:\d+)"?')

# Paths LinkedIn redirects to when it wants a human rather than a client.
_CHALLENGE_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login")


async def _watch_redirects(response: httpx.Response) -> None:
    """Name a bot check at the moment of the redirect, not after following it.

    We follow redirects, because LinkedIn uses one to hand out its routing
    cookie. But that means we normally only see where we landed. If the
    challenge page itself fails to load, the reason for the failure is lost and
    the caller gets a generic transport error.

    Reading the Location header here catches it either way.
    """
    location = response.headers.get("location", "")
    if location and any(marker in location for marker in _CHALLENGE_MARKERS):
        raise ChallengeRequired(f"LinkedIn redirected to a check page: {location}")

# Cooling off period after a session fails, in seconds.
_QUARANTINE_S = 900


@dataclass
class LinkedInSession:
    li_at: str
    jsessionid: str | None = None
    label: str = "default"
    failures: int = 0
    quarantined_until: float = 0.0
    last_used: float = 0.0
    requests: int = 0
    warmed: bool = False
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    @property
    def csrf_token(self) -> str:
        if not self.jsessionid:
            # LinkedIn accepts a self chosen ajax token when the cookie matches.
            self.jsessionid = f"ajax:{uuid.uuid4().int % 10**19:019d}"
        match = _JSESSIONID_RE.search(self.jsessionid)
        return match.group(1) if match else self.jsessionid

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.quarantined_until

    @property
    def cookies(self) -> dict[str, str]:
        return {
            "li_at": self.li_at,
            "JSESSIONID": f'"{self.csrf_token}"',
            "lang": "v=2&lang=en-us",
        }

    def client(self, *, user_agent: str, timeout: float) -> httpx.AsyncClient:
        """Return this session's HTTP client, and build it on first use.

        Each session keeps its own client, and therefore its own cookie jar.
        That matters. LinkedIn answers a request that lacks its routing cookie
        with a 302 back to the same URL, plus a `Set-Cookie: lidc=...`. A client
        that stores the cookie and follows the redirect gets a 200 on the second
        try. A client that sends a fixed cookie dict on every request never
        stores `lidc`, so it is redirected forever.

        We observed exactly that: the browser succeeded while our service saw a
        302 on every route, including `/me`.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                max_redirects=10,
                headers={"user-agent": user_agent},
                cookies=self.cookies,
                http2=True,
                event_hooks={"response": [_watch_redirects]},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def headers(self, *, referer: str | None = None, accept: str | None = None) -> dict[str, str]:
        track = {
            "clientVersion": "1.13.27340",
            "mpVersion": "1.13.27340",
            "osName": "web",
            "timezoneOffset": 5.5,
            "timezone": "Asia/Kolkata",
            "deviceFormFactor": "DESKTOP",
            "mpName": "voyager-web",
            "displayDensity": 2,
            "displayWidth": 2560,
            "displayHeight": 1440,
        }
        headers = {
            "accept": accept or "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "csrf-token": self.csrf_token,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "x-li-track": json.dumps(track, separators=(",", ":")),
            "x-li-page-instance": (
                f"urn:li:page:d_flagship3_profile_view_base;{uuid.uuid4()}"
            ),
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if referer:
            headers["referer"] = referer
        return headers

    def mark_success(self) -> None:
        self.failures = 0
        self.quarantined_until = 0.0
        self.requests += 1
        self.last_used = time.monotonic()

    def mark_failure(self, *, hard: bool = False) -> None:
        self.failures += 1
        if hard or self.failures >= 3:
            self.quarantined_until = time.monotonic() + _QUARANTINE_S
            log.warning(
                "Session %s is quarantined for %ss after %s failures.",
                self.label,
                _QUARANTINE_S,
                self.failures,
            )

    def status(self) -> dict[str, object]:
        jar = self._client.cookies if self._client else {}
        return {
            "label": self.label,
            "cookies_held": sorted(jar.keys()) if jar else [],
            "warmed": self.warmed,
            "healthy": self.healthy,
            "failures": self.failures,
            "requests": self.requests,
            "quarantined_for_s": max(0, round(self.quarantined_until - time.monotonic())),
        }


@dataclass
class SessionPool:
    """Round robin over the healthy sessions."""

    sessions: list[LinkedInSession] = field(default_factory=list)
    _cursor: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionPool:
        sessions = [
            LinkedInSession(li_at=li_at, jsessionid=jsid, label=f"session-{index + 1}")
            for index, (li_at, jsid) in enumerate(settings.sessions)
        ]
        return cls(sessions=sessions)

    @property
    def configured(self) -> bool:
        return bool(self.sessions)

    def acquire(self) -> LinkedInSession:
        if not self.sessions:
            raise NoSessionConfigured()
        healthy = [s for s in self.sessions if s.healthy]
        if not healthy:
            # Everything is cooling off. Use the one that recovers soonest.
            return min(self.sessions, key=lambda s: s.quarantined_until)
        session = healthy[self._cursor % len(healthy)]
        self._cursor += 1
        return session

    def status(self) -> list[dict[str, object]]:
        return [s.status() for s in self.sessions]

    async def aclose(self) -> None:
        for session in self.sessions:
            await session.aclose()
