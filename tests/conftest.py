"""Shared fixtures.

Every JSON fixture here came from a real LinkedIn response, captured on
2026-08-28, with the personal data replaced by `scripts/make_fixtures.py`. The
structure is untouched: real field names, real nesting, real types.

That matters. The first version of this suite used hand written fixtures that
matched what we believed LinkedIn returns. All 96 tests passed and none of them
could have caught the fact that the belief was wrong.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.config import Settings
from app.linkedin.entities import EntityPool
from app.linkedin.urls import parse_profile_url

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# The scrubbed identity used across the fixtures.
PUBLIC_ID = "ada-lovelace-000000000"
PROFILE_ID = "ACoAAAFAKEIDFAKEIDFAKEIDFAKEIDFAKEIDxxx"
PROFILE_URN = f"urn:li:fsd_profile:{PROFILE_ID}"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def ref():
    return parse_profile_url(f"https://www.linkedin.com/in/{PUBLIC_ID}/")


@pytest.fixture
def top_card() -> dict:
    """The response from /identity/dash/profiles?q=memberIdentity&memberIdentity=..."""
    return load("dash_query.json")


@pytest.fixture
def sections() -> dict[str, dict]:
    """Every /identity/dash/profile<Section>s?q=viewee response, keyed by route."""
    return load("sections.json")


@pytest.fixture
def full_pool(top_card, sections) -> EntityPool:
    """Everything merged, the way the strategy assembles it."""
    pool = EntityPool.from_payload(top_card)
    for body in sections.values():
        pool.merge(EntityPool.from_payload(body))
    return pool


@pytest.fixture
def settings():
    return Settings(
        li_at="fake-li-at-cookie",
        jsessionid="ajax:1234567890123456789",
        outbound_rps=0,
        outbound_jitter_ms=0,
        cache_ttl_s=60,
        rate_limit_per_minute=0,
        max_retries=1,
        api_keys=[],
    )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep a developer's local .env out of the tests."""
    import app.deps
    from app.config import get_settings

    for key in ("API_KEYS", "LI_AT", "JSESSIONID", "LI_AT_POOL", "REDIS_URL", "SECTIONS"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("OUTBOUND_RPS", "0")
    monkeypatch.setenv("OUTBOUND_JITTER_MS", "0")

    get_settings.cache_clear()
    app.deps._limiter = None
    yield
    get_settings.cache_clear()
    app.deps._limiter = None
