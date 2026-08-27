"""Error types. Each one maps to a stable code and an HTTP status."""

from __future__ import annotations

from typing import Any


class LinkedInAPIError(Exception):
    code = "internal_error"
    status = 500
    message = "Something went wrong."

    def __init__(self, message: str | None = None, detail: Any = None) -> None:
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class InvalidProfileURL(LinkedInAPIError):
    code = "invalid_profile_url"
    status = 400
    message = "The URL is not a LinkedIn profile URL."


class ProfileNotFound(LinkedInAPIError):
    code = "profile_not_found"
    status = 404
    message = "LinkedIn has no profile at this URL."


class AuthenticationFailed(LinkedInAPIError):
    """Our LinkedIn cookie is dead. An operator must replace it."""

    code = "linkedin_session_invalid"
    status = 503
    message = "The LinkedIn session is no longer valid. Refresh the cookies."


class ChallengeRequired(LinkedInAPIError):
    """LinkedIn asked for a CAPTCHA or a login challenge."""

    code = "linkedin_challenge_required"
    status = 503
    message = "LinkedIn asked for a human check. The session needs manual attention."


class RateLimited(LinkedInAPIError):
    code = "rate_limited"
    status = 429
    message = "Too many requests."

    def __init__(self, message: str | None = None, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamRateLimited(RateLimited):
    code = "linkedin_rate_limited"
    status = 429
    message = "LinkedIn throttled us. Try again later."


class AllStrategiesFailed(LinkedInAPIError):
    code = "all_strategies_failed"
    status = 502
    message = "No fetch strategy returned a profile."


class Unauthorized(LinkedInAPIError):
    code = "unauthorized"
    status = 401
    message = "Send a valid X-API-Key header."


class NoSessionConfigured(LinkedInAPIError):
    code = "no_linkedin_session"
    status = 503
    message = "The server has no LinkedIn cookie configured."
