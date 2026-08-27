"""Settings. Every secret comes from the environment, never from the repo."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- service ---
    app_name: str = "LinkedIn Profile API"
    version: str = "1.0.0"
    log_level: str = "INFO"
    port: int = 8080

    # --- our own auth ---
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma separated keys. An empty list leaves the API open.",
    )

    # --- LinkedIn session ---
    # A pool lets us spread load and survive one dead session.
    # Format of LI_AT_POOL: "li_at1:jsessionid1,li_at2:jsessionid2"
    li_at: str | None = None
    jsessionid: str | None = None
    li_at_pool: Annotated[list[str], NoDecode] = Field(default_factory=list)

    linkedin_base_url: str = "https://www.linkedin.com"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    linkedin_lang: str = "en_US"

    # --- pacing. These protect the LinkedIn account, not our server. ---
    outbound_rps: float = Field(
        default=1.0, description="Requests per second toward LinkedIn, across all callers."
    )
    outbound_jitter_ms: int = 400
    request_timeout_s: float = 20.0
    max_retries: int = 3

    # --- caching ---
    cache_ttl_s: int = 3600
    cache_max_entries: int = 1000
    redis_url: str | None = None

    # --- caller rate limit ---
    rate_limit_per_minute: int = 30
    batch_max_urls: int = 10
    batch_concurrency: int = 3

    # --- strategy control ---
    strategies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["voyager_dash"]
    )

    # Which profile sections to fetch. Each one costs a request to LinkedIn, so
    # a shorter list is faster and safer for the account. Empty means all.
    sections: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("api_keys", "li_at_pool", "strategies", "sections", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def sessions(self) -> list[tuple[str, str | None]]:
        """Return every configured LinkedIn session as (li_at, jsessionid)."""
        out: list[tuple[str, str | None]] = []
        if self.li_at:
            out.append((self.li_at, self.jsessionid))
        for entry in self.li_at_pool:
            li_at, _, jsid = entry.partition(":")
            if li_at:
                out.append((li_at, jsid or None))
        return out

    @property
    def auth_required(self) -> bool:
        return bool(self.api_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
