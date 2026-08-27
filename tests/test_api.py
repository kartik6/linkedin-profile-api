"""The HTTP surface, driven through the real app with a stubbed fetch layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.errors import AuthenticationFailed, ProfileNotFound
from app.linkedin.normalize import from_entity_pool
from app.linkedin.service import ProfileService
from app.models import Meta, ProfileResponse
from tests.conftest import PUBLIC_ID


class StubService(ProfileService):
    """Answers without touching the network."""

    def __init__(self, profile=None, error=None):
        self._profile = profile
        self._error = error
        self.calls: list[tuple[str, bool]] = []

    async def get_profile(self, url: str, *, refresh: bool = False):
        from app.linkedin.urls import parse_profile_url

        parse_profile_url(url)  # keep the real validation
        self.calls.append((url, refresh))
        if self._error:
            raise self._error
        return ProfileResponse(
            profile=self._profile,
            meta=Meta(
                strategy="stub",
                strategies_tried=["stub"],
                fetched_at=datetime.now(UTC),
                duration_ms=1,
                completeness=1.0,
            ),
        )


@pytest.fixture
def rich(full_pool, ref):
    """A complete profile, built from the real captured payloads."""
    return from_entity_pool(full_pool, ref)


@pytest.fixture
def client(rich):
    with TestClient(main.app) as tc:
        main.state["service"] = StubService(profile=rich)
        yield tc


class TestOps:
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "strategies" in body

    def test_index_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "LinkedIn Profile API" in response.text

    def test_openapi_is_served(self, client):
        schema = client.get("/openapi.json").json()
        assert "/api/v1/profile" in schema["paths"]

    def test_strategies_are_listed(self, client):
        body = client.get("/api/v1/strategies").json()
        assert body["order"] == ["voyager_dash"]
        assert body["available"]["voyager_dash"]["needs_auth"] is True

    def test_parse_validates_without_a_network_call(self, client):
        body = client.get(
            "/api/v1/parse", params={"url": "linkedin.com/in/ada-lovelace-000000000"}
        ).json()
        assert body["public_identifier"] == "ada-lovelace-000000000"
        assert body["canonical_url"] == "https://www.linkedin.com/in/ada-lovelace-000000000/"


class TestGetProfile:
    def test_returns_the_profile(self, client):
        response = client.get(
            "/api/v1/profile", params={"url": "https://www.linkedin.com/in/ada-lovelace-000000000/"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["profile"]["full_name"] == "Ada Lovelace"
        assert body["profile"]["experience"][0]["title"].startswith("Principal Engineer")
        assert body["meta"]["strategy"] == "stub"
        assert body["meta"]["completeness"] == 1.0

    def test_post_accepts_the_url_in_the_body(self, client):
        response = client.post(
            "/api/v1/profile", json={"url": "ada-lovelace-000000000", "refresh": True}
        )
        assert response.status_code == 200
        assert main.state["service"].calls[-1] == ("ada-lovelace-000000000", True)

    def test_a_missing_url_is_a_422(self, client):
        assert client.get("/api/v1/profile").status_code == 422

    def test_a_company_url_is_a_400_with_a_stable_code(self, client):
        response = client.get(
            "/api/v1/profile", params={"url": "https://www.linkedin.com/company/microsoft/"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_profile_url"

    def test_an_unknown_profile_is_a_404(self, rich):
        with TestClient(main.app) as tc:
            main.state["service"] = StubService(error=ProfileNotFound())
            response = tc.get("/api/v1/profile", params={"url": "nobody"})
        assert response.status_code == 404
        assert response.json()["error"] == "profile_not_found"

    def test_a_dead_cookie_is_a_503(self, rich):
        with TestClient(main.app) as tc:
            main.state["service"] = StubService(error=AuthenticationFailed())
            response = tc.get("/api/v1/profile", params={"url": "ada-lovelace-000000000"})
        assert response.status_code == 503
        assert response.json()["error"] == "linkedin_session_invalid"


class TestBatch:
    def test_reads_several_profiles(self, client):
        response = client.post(
            "/api/v1/profiles/batch",
            json={"urls": [PUBLIC_ID, PUBLIC_ID]},
        )
        body = response.json()
        assert body["requested"] == 2
        assert body["succeeded"] == 2
        assert body["results"][0]["profile"]["full_name"] == "Ada Lovelace"

    def test_one_failure_does_not_sink_the_batch(self, client):
        response = client.post(
            "/api/v1/profiles/batch",
            json={"urls": ["ada-lovelace-000000000", "https://www.linkedin.com/company/x/"]},
        )
        body = response.json()
        assert body["succeeded"] == 1
        assert body["failed"] == 1
        assert body["results"][1]["error"]["error"] == "invalid_profile_url"

    def test_the_batch_is_capped(self, client):
        response = client.post(
            "/api/v1/profiles/batch", json={"urls": ["ada-lovelace-000000000"] * 25}
        )
        assert response.json()["requested"] == 10


class TestAuth:
    def test_open_when_no_keys_are_set(self, client):
        response = client.get("/api/v1/profile", params={"url": PUBLIC_ID})
        assert response.status_code == 200

    def test_a_key_is_required_once_configured(self, monkeypatch, rich):
        from app.config import get_settings

        monkeypatch.setenv("API_KEYS", "secret-one,secret-two")
        get_settings.cache_clear()
        try:
            with TestClient(main.app) as tc:
                main.state["service"] = StubService(profile=rich)
                denied = tc.get("/api/v1/profile", params={"url": PUBLIC_ID})
                assert denied.status_code == 401
                ok = tc.get(
                    "/api/v1/profile",
                    params={"url": "ada-lovelace-000000000"},
                    headers={"X-API-Key": "secret-two"},
                )
                assert ok.status_code == 200
                bad = tc.get(
                    "/api/v1/profile",
                    params={"url": "ada-lovelace-000000000"},
                    headers={"X-API-Key": "wrong"},
                )
                assert bad.status_code == 401
        finally:
            get_settings.cache_clear()


class TestRateLimit:
    def test_the_caller_limit_returns_429_with_retry_after(self, monkeypatch, rich):
        import app.deps
        from app.config import get_settings

        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
        get_settings.cache_clear()
        app.deps._limiter = None
        try:
            with TestClient(main.app) as tc:
                main.state["service"] = StubService(profile=rich)
                params = {"url": "ada-lovelace-000000000"}
                assert tc.get("/api/v1/profile", params=params).status_code == 200
                assert tc.get("/api/v1/profile", params=params).status_code == 200
                third = tc.get("/api/v1/profile", params=params)
                assert third.status_code == 429
                assert third.json()["error"] == "rate_limited"
                assert int(third.headers["retry-after"]) > 0
        finally:
            get_settings.cache_clear()
            app.deps._limiter = None
