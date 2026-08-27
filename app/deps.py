"""Request time concerns: who is calling, and how often."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Header, Request

from app.config import Settings, get_settings
from app.errors import RateLimited, Unauthorized


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Check the caller key.

    With no API_KEYS set the service stays open, which keeps the demo simple.
    Set API_KEYS in production and every request needs a header.
    """
    settings: Settings = get_settings()
    if not settings.auth_required:
        return "anonymous"

    key = x_api_key or request.query_params.get("api_key")
    if not key or key not in settings.api_keys:
        raise Unauthorized()
    return key


class SlidingWindowLimiter:
    """Count requests per caller over the last minute."""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, caller: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        window = self._events[caller]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limit:
            retry_after = int(60 - (now - window[0])) + 1
            raise RateLimited(
                f"Limit is {self.limit} requests per minute. Try again in {retry_after}s.",
                retry_after=retry_after,
            )
        window.append(now)


_limiter: SlidingWindowLimiter | None = None


def get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        _limiter = SlidingWindowLimiter(get_settings().rate_limit_per_minute)
    return _limiter


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request, api_key: str) -> None:
    """Count this call against the caller's budget."""
    caller = api_key if api_key and api_key != "anonymous" else client_ip(request)
    get_limiter().check(caller)
