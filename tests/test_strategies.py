"""Each strategy against a mocked LinkedIn, plus the failures LinkedIn returns."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.errors import (
    AuthenticationFailed,
    ChallengeRequired,
    LinkedInAPIError,
    ProfileNotFound,
    UpstreamRateLimited,
)
from app.linkedin.client import LinkedInClient
from app.linkedin.strategies.embedded_json import EmbeddedJSONStrategy, extract_payloads
from app.linkedin.strategies.public_jsonld import PublicJSONLDStrategy
from app.linkedin.strategies.voyager_dash import VoyagerDashStrategy
from app.linkedin.strategies.voyager_profile_view import VoyagerProfileViewStrategy

PROFILE_VIEW_URL = (
    "https://www.linkedin.com/voyager/api/identity/profiles/adalovelace/profileView"
)
DASH_URL = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
PAGE_URL = "https://www.linkedin.com/in/adalovelace/"


@pytest.fixture
async def client(settings):
    c = LinkedInClient(settings)
    yield c
    await c.aclose()


class TestVoyagerProfileView:
    @respx.mock
    async def test_reads_a_profile(self, client, ref, profile_view):
        route = respx.get(PROFILE_VIEW_URL).mock(
            return_value=httpx.Response(200, json=profile_view)
        )
        result = await VoyagerProfileViewStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"
        assert len(result.profile.experience) == 2
        assert route.called

    @respx.mock
    async def test_sends_the_headers_voyager_requires(self, client, ref, profile_view):
        route = respx.get(PROFILE_VIEW_URL).mock(
            return_value=httpx.Response(200, json=profile_view)
        )
        await VoyagerProfileViewStrategy().fetch(client, ref)
        request = route.calls[0].request
        assert request.headers["csrf-token"] == "ajax:1234567890123456789"
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
        assert 'JSESSIONID="ajax:1234567890123456789"' in request.headers["cookie"]
        assert "li_at=fake-li-at-cookie" in request.headers["cookie"]

    @respx.mock
    async def test_401_means_the_cookie_is_dead(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(AuthenticationFailed):
            await VoyagerProfileViewStrategy().fetch(client, ref)

    @respx.mock
    async def test_404_means_no_such_profile(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(ProfileNotFound):
            await VoyagerProfileViewStrategy().fetch(client, ref)

    @respx.mock
    async def test_999_means_linkedin_thinks_we_are_a_bot(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(return_value=httpx.Response(999))
        with pytest.raises(ChallengeRequired):
            await VoyagerProfileViewStrategy().fetch(client, ref)

    @respx.mock
    async def test_redirect_to_a_checkpoint_is_a_challenge(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://www.linkedin.com/checkpoint/challenge/x"}
            )
        )
        with pytest.raises(ChallengeRequired):
            await VoyagerProfileViewStrategy().fetch(client, ref)

    @respx.mock
    async def test_429_is_reported_as_upstream_throttling(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(
            return_value=httpx.Response(429, headers={"retry-after": "30"})
        )
        with pytest.raises(UpstreamRateLimited) as caught:
            await VoyagerProfileViewStrategy().fetch(client, ref)
        assert caught.value.retry_after == 30

    @respx.mock
    async def test_a_dead_cookie_quarantines_the_session(self, client, ref):
        respx.get(PROFILE_VIEW_URL).mock(return_value=httpx.Response(403))
        with pytest.raises(AuthenticationFailed):
            await VoyagerProfileViewStrategy().fetch(client, ref)
        assert client.pool.sessions[0].healthy is False


class TestVoyagerDash:
    @respx.mock
    async def test_reads_a_profile(self, client, ref, dash_profile):
        respx.get(DASH_URL).mock(return_value=httpx.Response(200, json=dash_profile))
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"
        assert result.profile.experience[0].company.name == "Analytical Engines"

    @respx.mock
    async def test_tries_the_next_decoration_when_one_is_retired(
        self, client, ref, dash_profile
    ):
        calls = {"n": 0}

        def responder(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(400, json={"message": "unknown decoration"})
            return httpx.Response(200, json=dash_profile)

        respx.get(DASH_URL).mock(side_effect=responder)
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert calls["n"] == 2
        assert result.profile.full_name == "Ada Lovelace"


class TestEmbeddedJSON:
    @respx.mock
    async def test_reads_the_json_inlined_in_the_page(self, client, ref, profile_page):
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html=profile_page))
        result = await EmbeddedJSONStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"
        assert len(result.profile.experience) == 2
        assert len(result.profile.skills) == 2

    def test_merges_several_blocks_and_skips_a_broken_one(self, profile_page):
        payloads = extract_payloads(profile_page)
        assert len(payloads) == 2  # the third block is not JSON

    @respx.mock
    async def test_a_sign_in_wall_is_not_a_profile(self, client, ref):
        wall = "<html><body><div>Join now to view Ada's full profile</div></body></html>"
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html=wall))
        with pytest.raises(LinkedInAPIError):
            await EmbeddedJSONStrategy().fetch(client, ref)

    @respx.mock
    async def test_a_page_with_no_payload_fails_clearly(self, client, ref):
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<html></html>"))
        with pytest.raises(LinkedInAPIError, match="no embedded Voyager JSON"):
            await EmbeddedJSONStrategy().fetch(client, ref)


class TestPublicJSONLD:
    @respx.mock
    async def test_reads_the_schema_org_markup(self, client, ref, public_page):
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html=public_page))
        result = await PublicJSONLDStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"
        assert result.profile.experience[0].title == "Principal Engineer"
        assert result.profile.education[0].school.name.startswith("Indian Institute")
        assert result.warnings

    @respx.mock
    async def test_sends_no_cookie(self, client, ref, public_page):
        route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html=public_page))
        await PublicJSONLDStrategy().fetch(client, ref)
        assert "li_at" not in route.calls[0].request.headers.get("cookie", "")

    @respx.mock
    async def test_a_page_with_no_markup_is_a_miss(self, client, ref):
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<html></html>"))
        with pytest.raises(ProfileNotFound):
            await PublicJSONLDStrategy().fetch(client, ref)


class TestSessionCheck:
    """The /me response shape decides whether an operator sees a useful answer."""

    def test_resolves_a_starred_reference(self, settings):
        client = LinkedInClient(settings)
        entity = client._resolve_me(
            {
                "data": {"*miniProfile": "urn:li:fs_miniProfile:ACoAA"},
                "included": [
                    {"entityUrn": "urn:li:fs_miniProfile:ACoAA", "publicIdentifier": "adalovelace"}
                ],
            }
        )
        assert entity["publicIdentifier"] == "adalovelace"

    def test_resolves_an_inline_object(self, settings):
        client = LinkedInClient(settings)
        entity = client._resolve_me(
            {"data": {"miniProfile": {"publicIdentifier": "adalovelace"}}}
        )
        assert entity["publicIdentifier"] == "adalovelace"

    def test_falls_back_to_any_entity_with_an_identifier(self, settings):
        client = LinkedInClient(settings)
        entity = client._resolve_me(
            {"data": {}, "included": [{"publicIdentifier": "adalovelace"}]}
        )
        assert entity["publicIdentifier"] == "adalovelace"

    def test_missing_data_returns_empty(self, settings):
        assert LinkedInClient(settings)._resolve_me({}) == {}


class TestRedirects:
    @respx.mock
    async def test_a_plain_redirect_is_followed_not_treated_as_missing(
        self, client, ref, profile_page
    ):
        respx.get(PAGE_URL).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://www.linkedin.com/in/adalovelace"}
            )
        )
        respx.get("https://www.linkedin.com/in/adalovelace").mock(
            return_value=httpx.Response(200, html=profile_page)
        )
        result = await EmbeddedJSONStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"

    @respx.mock
    async def test_landing_on_a_checkpoint_is_still_a_challenge(self, client, ref):
        """We follow the redirect, so the landing URL is what condemns it."""
        checkpoint = "https://www.linkedin.com/checkpoint/challenge/y"
        respx.get(PAGE_URL).mock(
            return_value=httpx.Response(302, headers={"location": checkpoint})
        )
        respx.get(checkpoint).mock(
            return_value=httpx.Response(200, html="<html>Verify you are a human</html>")
        )
        with pytest.raises(ChallengeRequired):
            await EmbeddedJSONStrategy().fetch(client, ref)
