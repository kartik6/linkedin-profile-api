"""The HTTP client that talks to LinkedIn.

Two jobs beyond plain HTTP:

1. Pace our outbound calls. LinkedIn watches request rate per account. One
   shared token bucket plus a little jitter keeps us under the limit no matter
   how many callers hit our own API at once.

2. Name the failure. LinkedIn answers a dead cookie, a bot check and a rate
   limit in three different ways, and none of them is a clean error body. We
   read the status, the redirect target and the content type, then raise the
   error that tells an operator what to do.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from app.config import Settings
from app.errors import (
    AuthenticationFailed,
    ChallengeRequired,
    LinkedInAPIError,
    ProfileNotFound,
    UpstreamRateLimited,
)
from app.linkedin.session import LinkedInSession, SessionPool, _watch_redirects

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.linkedin.com"

# Paths LinkedIn redirects to when it wants a human.
_CHALLENGE_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login")

# Browser identity and routing cookies. LinkedIn hands these out on a plain
# page load, and expects to see them on later API calls.
_BROWSER_COOKIES = ("bcookie", "bscookie", "lidc", "rtc", "trkCode", "trkInfo", "li_gc")


class RateLimiter:
    """A token bucket shared by every caller in the process."""

    def __init__(self, rate_per_second: float, jitter_ms: int = 0) -> None:
        self.interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self.jitter_ms = jitter_ms
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_slot - now)
            jitter = random.uniform(0, self.jitter_ms / 1000) if self.jitter_ms else 0.0
            self._next_slot = max(now, self._next_slot) + self.interval + jitter
        if delay > 0:
            await asyncio.sleep(delay)


class LinkedInClient:
    """One client per process. Reuses connections and shares the limiter."""

    def __init__(self, settings: Settings, pool: SessionPool | None = None) -> None:
        self.settings = settings
        self.base_url = settings.linkedin_base_url.rstrip("/")
        self.voyager = f"{self.base_url}/voyager/api"
        self.pool = pool or SessionPool.from_settings(settings)
        self.limiter = RateLimiter(settings.outbound_rps, settings.outbound_jitter_ms)
        # Used only for logged out requests. Authenticated calls go through
        # the per session clients, which each hold their own cookie jar.
        self._anon = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s),
            follow_redirects=True,
            max_redirects=5,
            headers={"user-agent": settings.user_agent},
            http2=True,
            event_hooks={"response": [_watch_redirects]},
        )

    async def aclose(self) -> None:
        await self._anon.aclose()
        await self.pool.aclose()

    # -- warm up -----------------------------------------------------------

    async def _ensure_warm(self, session: LinkedInSession) -> None:
        """Collect LinkedIn's browser identity cookies before the first API call.

        A browser never starts at an API endpoint. It loads linkedin.com, is
        handed `bcookie`, `bscookie` and `lidc`, and only then does the page
        make Voyager calls.

        Our client used to go straight to the API with nothing but `li_at`.
        LinkedIn answered every request with a 302 back to the same URL, while
        re-issuing `li_at`, `li_a` and `liap`. That is session establishment,
        not session use: it was trying to give us an identity we never came
        back with. The result was an endless redirect loop.

        So we do what the browser does. One unauthenticated page load, then
        copy the cookies it gave us into the session jar.
        """
        if session.warmed:
            return
        session.warmed = True  # set first, so a failure does not retry forever

        try:
            await self.limiter.wait()
            response = await self._anon.request(
                "GET",
                f"{self.base_url}/",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                    "accept-language": "en-US,en;q=0.9",
                },
            )
        except httpx.HTTPError as exc:
            log.warning("Warm up request failed for %s: %s", session.label, exc)
            return

        transport = session.client(
            user_agent=self.settings.user_agent,
            timeout=self.settings.request_timeout_s,
        )
        copied = []
        for name in _BROWSER_COOKIES:
            value = self._anon.cookies.get(name)
            if value:
                transport.cookies.set(name, value, domain=".linkedin.com", path="/")
                copied.append(name)
        log.info(
            "Warmed session %s from %s: copied %s",
            session.label,
            response.status_code,
            copied or "nothing",
        )

    # -- core --------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        session: LinkedInSession | None = None,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
        referer: str | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        session = session or (self.pool.acquire() if authenticated else None)
        if session is not None:
            await self._ensure_warm(session)
        attempt = 0
        last_error: Exception | None = None

        while attempt < self.settings.max_retries:
            attempt += 1
            await self.limiter.wait()

            if session:
                headers = session.headers(referer=referer, accept=accept)
                transport = session.client(
                    user_agent=self.settings.user_agent,
                    timeout=self.settings.request_timeout_s,
                )
            else:
                headers = {"accept": accept or "text/html,application/xhtml+xml"}
                transport = self._anon

            try:
                response = await transport.request(
                    method, url, params=params, headers=headers
                )
            except ChallengeRequired:
                # Raised by the session's redirect hook. The cookie is not dead,
                # but this session needs a human before it works again.
                if session:
                    session.mark_failure(hard=True)
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning("Timeout on %s, attempt %s.", url, attempt)
                await self._backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Transport error on %s: %s", url, exc)
                await self._backoff(attempt)
                continue

            try:
                self._raise_for_linkedin(response)
            except UpstreamRateLimited:
                if session:
                    session.mark_failure()
                if attempt < self.settings.max_retries:
                    await self._backoff(attempt, base=5.0)
                    continue
                raise
            except (AuthenticationFailed, ChallengeRequired):
                if session:
                    session.mark_failure(hard=True)
                raise

            if session:
                session.mark_success()
            return response

        raise LinkedInAPIError(
            f"Gave up on {url} after {self.settings.max_retries} attempts.",
            detail=str(last_error) if last_error else None,
        )

    async def _backoff(self, attempt: int, base: float = 1.0) -> None:
        delay = base * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        await asyncio.sleep(min(delay, 30.0))

    # -- failure detection -------------------------------------------------

    @staticmethod
    def _raise_for_linkedin(response: httpx.Response) -> None:
        status = response.status_code
        location = response.headers.get("location", "")

        # When we follow redirects, the landing URL is what matters. When we do
        # not, the Location header is. Check both, so a sign in wall is named a
        # sign in wall either way.
        final_url = str(response.url)
        for candidate in (location, final_url):
            if candidate and any(marker in candidate for marker in _CHALLENGE_MARKERS):
                raise ChallengeRequired(
                    f"LinkedIn redirected to a check page: {candidate}"
                )

        if status == 401:
            raise AuthenticationFailed("LinkedIn rejected the session cookie (401).")
        if status == 403:
            raise AuthenticationFailed(
                "LinkedIn refused the request (403). The cookie or the CSRF token is stale."
            )
        if status == 404:
            raise ProfileNotFound()
        if status == 429:
            retry_after = int(response.headers.get("retry-after", "60") or 60)
            raise UpstreamRateLimited(retry_after=retry_after)
        if status == 999:
            # LinkedIn's own non standard block code.
            raise ChallengeRequired("LinkedIn returned 999. The client looks automated to it.")
        if status >= 500:
            raise LinkedInAPIError(f"LinkedIn returned {status}.")

        content_type = response.headers.get("content-type", "")
        if "json" in (response.request.headers.get("accept") or "") and "html" in content_type:
            body = response.text[:2000].lower()
            if any(m in body for m in ("authwall", "sign in", "checkpoint/challenge")):
                raise ChallengeRequired("LinkedIn served a sign in wall instead of JSON.")

    # -- helpers -----------------------------------------------------------

    async def get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.voyager}{path}"
        response = await self.request("GET", url, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise LinkedInAPIError(
                "LinkedIn returned a body that is not JSON.", detail=response.text[:400]
            ) from exc
        if not isinstance(data, dict):
            raise LinkedInAPIError("LinkedIn returned JSON that is not an object.")
        return data

    async def get_html(self, url: str, **kwargs: Any) -> str:
        kwargs.setdefault("accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        response = await self.request("GET", url, **kwargs)
        return response.text

    async def probe(
        self, url: str, *, authenticated: bool = True, follow: bool = False
    ) -> dict[str, Any]:
        """Make one raw call and report exactly what LinkedIn answered.

        This exists because "profile_not_found" is not a diagnosis. An operator
        needs the status, the landing URL and the first bytes of the body to
        tell a restricted account apart from a retired route.
        """
        session = self.pool.acquire() if authenticated and self.pool.configured else None
        headers = (
            session.headers(accept="application/vnd.linkedin.normalized+json+2.1")
            if session
            else {"accept": "text/html,application/xhtml+xml"}
        )
        transport = (
            session.client(
                user_agent=self.settings.user_agent,
                timeout=self.settings.request_timeout_s,
            )
            if session
            else self._anon
        )
        await self.limiter.wait()
        try:
            response = await transport.request(
                "GET", url, headers=headers, follow_redirects=follow
            )
        except httpx.HTTPError as exc:
            return {"url": url, "transport_error": str(exc)}

        body = response.text[:280].replace("\n", " ")
        set_cookies = [
            value.split("=", 1)[0]
            for key, value in response.headers.multi_items()
            if key.lower() == "set-cookie"
        ]
        return {
            "url": url,
            "authenticated": session is not None,
            "status": response.status_code,
            "location": response.headers.get("location"),
            "set_cookie": set_cookies,
            "cookies_sent": sorted(
                (transport.cookies or {}).keys()
            ),
            "final_url": str(response.url),
            "redirects": len(response.history),
            "chain": [
                {"status": r.status_code, "location": r.headers.get("location")}
                for r in response.history
            ],
            "content_type": response.headers.get("content-type"),
            "content_length": response.headers.get("content-length"),
            "body_head": body,
        }

    @staticmethod
    def _resolve_me(data: dict[str, Any]) -> dict[str, Any]:
        """Find our own profile in the /me response.

        Under the normalized accept header the miniProfile arrives as a starred
        URN reference, not inline, so we resolve it against `included`.
        """
        body = data.get("data", {})
        ref = body.get("*miniProfile") or body.get("miniProfile")
        included = [e for e in data.get("included", []) if isinstance(e, dict)]
        if isinstance(ref, dict):
            return ref
        if isinstance(ref, str):
            found = next((e for e in included if e.get("entityUrn") == ref), None)
            if found:
                return found
        return next((e for e in included if e.get("publicIdentifier")), {})

    def page_url(self, public_identifier: str) -> str:
        """The profile page URL on the configured origin."""
        return f"{self.base_url}/in/{public_identifier}/"

    async def check_session(self) -> dict[str, Any]:
        """Ask LinkedIn who we are. Cheap way to see if the cookie still works."""
        if not self.pool.configured:
            return {"configured": False, "authenticated": False, "sessions": []}
        try:
            data = await self.get_json("/me")
            entity = self._resolve_me(data)
            return {
                "configured": True,
                "authenticated": True,
                "logged_in_as": entity.get("publicIdentifier"),
                "sessions": self.pool.status(),
            }
        except LinkedInAPIError as exc:
            return {
                "configured": True,
                "authenticated": False,
                "error": exc.code,
                "message": exc.message,
                "sessions": self.pool.status(),
            }
