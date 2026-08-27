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
from app.linkedin.session import LinkedInSession, SessionPool

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.linkedin.com"

# Paths LinkedIn redirects to when it wants a human.
_CHALLENGE_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login")


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
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s),
            follow_redirects=False,
            headers={"user-agent": settings.user_agent},
            http2=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        attempt = 0
        last_error: Exception | None = None

        while attempt < self.settings.max_retries:
            attempt += 1
            await self.limiter.wait()

            headers = (
                session.headers(referer=referer, accept=accept)
                if session
                else {"accept": accept or "text/html,application/xhtml+xml"}
            )
            cookies = session.cookies if session else None

            try:
                response = await self._client.request(
                    method, url, params=params, headers=headers, cookies=cookies
                )
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

        if status in (301, 302, 303, 307, 308):
            if any(marker in location for marker in _CHALLENGE_MARKERS):
                raise ChallengeRequired(
                    f"LinkedIn redirected to a check page: {location}"
                )
            return

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
        if response.status_code in (301, 302, 303, 307, 308):
            raise ProfileNotFound("LinkedIn redirected away from the profile.")
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
        if response.status_code in (301, 302, 303, 307, 308):
            raise ProfileNotFound("LinkedIn redirected away from the profile page.")
        return response.text

    def page_url(self, public_identifier: str) -> str:
        """The profile page URL on the configured origin."""
        return f"{self.base_url}/in/{public_identifier}/"

    async def check_session(self) -> dict[str, Any]:
        """Ask LinkedIn who we are. Cheap way to see if the cookie still works."""
        if not self.pool.configured:
            return {"configured": False, "authenticated": False, "sessions": []}
        try:
            data = await self.get_json("/me")
            mini = data.get("data", data).get("miniProfile", {})
            if isinstance(mini, str):
                mini = next(
                    (e for e in data.get("included", []) if e.get("entityUrn") == mini), {}
                )
            return {
                "configured": True,
                "authenticated": True,
                "logged_in_as": mini.get("publicIdentifier"),
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
