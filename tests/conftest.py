from __future__ import annotations

import json
import pathlib

import pytest

from app.config import Settings
from app.linkedin.urls import parse_profile_url

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def ref():
    return parse_profile_url("https://www.linkedin.com/in/adalovelace/")


@pytest.fixture
def profile_view():
    return load_json("profile_view.json")


@pytest.fixture
def dash_profile():
    return load_json("dash_profile.json")


@pytest.fixture
def profile_page():
    return load_text("profile_page.html")


@pytest.fixture
def public_page():
    return load_text("public_page.html")


@pytest.fixture
def settings():
    return Settings(
        li_at="fake-li-at-cookie",
        jsessionid="ajax:1234567890123456789",
        outbound_rps=0,          # no pacing inside tests
        outbound_jitter_ms=0,
        cache_ttl_s=60,
        rate_limit_per_minute=0,  # no caller limit inside tests
        max_retries=1,
        api_keys=[],
    )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep a developer's local .env out of the tests.

    Environment variables beat the .env file in pydantic-settings, so setting
    them here makes the suite behave the same on every machine and in CI.
    """
    import app.deps
    from app.config import get_settings

    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("LI_AT", "")
    monkeypatch.setenv("JSESSIONID", "")
    monkeypatch.setenv("LI_AT_POOL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("OUTBOUND_RPS", "0")
    monkeypatch.setenv("OUTBOUND_JITTER_MS", "0")

    get_settings.cache_clear()
    app.deps._limiter = None
    yield
    get_settings.cache_clear()
    app.deps._limiter = None
