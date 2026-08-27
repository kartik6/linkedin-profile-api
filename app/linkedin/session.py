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

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.errors import AuthenticationFailed, ChallengeRequired, NoSessionConfigured

log = logging.getLogger(__name__)

_JSESSIONID_RE = re.compile(r'"?(ajax:\d+)"?')

# Paths LinkedIn redirects to when it wants a human rather than a client.
_CHALLENGE_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login")


def _deletes_session_cookie(response: httpx.Response) -> bool:
    """True when LinkedIn is deleting our session cookie rather than setting one.

    A server deletes a cookie by re-sending it with an expiry in the past:

        Set-Cookie: li_at=...; Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0

    Observed live on every route at once, for li_at, li_a and liap together,
    paired with a 302 back to the same URL. That is a logout instruction.
    """
    for key, value in response.headers.multi_items():
        if key.lower() != "set-cookie":
            continue
        name = value.split("=", 1)[0].strip().lower()
        if name not in ("li_at", "li_a"):
            continue
        lowered = value.lower()
        if "max-age=0" in lowered or "expires=thu, 01-jan-1970" in lowered:
            return True
    return False


async def _watch_redirects(response: httpx.Response) -> None:
    """Name a bot check at the moment of the redirect, not after following it.

    We follow redirects, because LinkedIn uses one to hand out its routing
    cookie. But that means we normally only see where we landed. If the
    challenge page itself fails to load, the reason for the failure is lost and
    the caller gets a generic transport error.

    Reading the Location header here catches it either way.
    """
    if _deletes_session_cookie(response):
        # Stop here. Following the redirect just presents the same dead token
        # again, so the client would loop to the redirect limit and report a
        # transport error instead of the truth.
        raise AuthenticationFailed(
            "LinkedIn deleted the session cookie. The li_at value is no longer "
            "valid, so it must be replaced."
        )

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
    duplicates_dropped: int = 0
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
        """Send only the headers we proved Voyager requires.

        This list is short on purpose, and the reason matters.

        The requests that demonstrably worked were `fetch()` calls made from
        the browser console. Those sent three headers of their own:

            csrf-token
            accept
            x-restli-protocol-version

        plus whatever the browser adds: user-agent, cookie, referer.

        Earlier versions of this file also sent `x-li-track`, announcing
        `mpName: voyager-web` and `clientVersion: 1.13.27340`. Both values were
        invented. The captured page shows LinkedIn's real client identifies
        itself as `flagship-web` version `0.2.6975`.

        So every request was claiming to be a client version that no longer
        exists, alongside a fabricated `x-li-page-instance` and made up
        `sec-ch-ua` hints. Announcing a nonexistent client is a far louder
        signal than announcing nothing, and we watched LinkedIn revoke a live
        session after three calls carrying them.

        The rule here: send what is required, never invent what is optional.
        If a header is ever added back, it must come from an observed request,
        not from a plausible guess.
        """
        headers = {
            "accept": accept or "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "csrf-token": self.csrf_token,
            "x-restli-protocol-version": "2.0.0",
        }
        if referer:
            headers["referer"] = referer
        return headers

    def reconcile_cookies(self) -> list[str]:
        """Keep exactly one entry per cookie name.

        A cookie jar keys entries on (name, domain, path), not on name alone.
        We seed `li_at` with no explicit domain, so it is stored host-scoped
        against `www.linkedin.com`. LinkedIn then sets its own `li_at` with a
        `Domain` attribute. Those are two different entries under the same
        name, and the jar dutifully sends **both** in one Cookie header.

        LinkedIn sees a request carrying two conflicting session tokens, cannot
        resolve it, and answers 302 back to the same URL while re-issuing the
        cookie. We come back with two again. That is the loop.

        It also explains why the very first request always succeeded: at that
        point there was only one.

        Rule: our configured values are known good, because they came from a
        real browser login, so they win for the auth cookies. For anything
        LinkedIn set itself, the most specific domain wins.
        """
        if self._client is None:
            return []
        jar = self._client.cookies
        by_name: dict[str, list] = {}
        for cookie in list(jar.jar):
            by_name.setdefault(cookie.name, []).append(cookie)

        dropped: list[str] = []
        for name, entries in by_name.items():
            if len(entries) < 2:
                continue
            if name == "li_at":
                keep = next((c for c in entries if c.value == self.li_at), entries[-1])
            elif name == "JSESSIONID":
                keep = next(
                    (c for c in entries if self.csrf_token in (c.value or "")), entries[-1]
                )
            else:
                keep = max(entries, key=lambda c: len(c.domain or ""))
            for cookie in entries:
                if cookie is not keep:
                    jar.jar.clear(cookie.domain, cookie.path, cookie.name)
                    dropped.append(f"{name}@{cookie.domain or 'host'}")
        if dropped:
            self.duplicates_dropped += len(dropped)
        return dropped

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
            "duplicate_cookies_dropped": self.duplicates_dropped,
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
