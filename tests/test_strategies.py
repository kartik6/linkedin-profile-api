"""The Voyager strategy and the HTTP client, against a mocked LinkedIn.

The routes asserted here were verified by hand on 2026-08-28. If LinkedIn moves
one, these tests keep passing while production fails, so treat them as a guard
against regressions in our code, not as proof that LinkedIn has not changed.
`scripts/capture.py` is what checks the latter.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.errors import (
    AuthenticationFailed,
    ChallengeRequired,
    ProfileNotFound,
    UpstreamRateLimited,
)
from app.linkedin.client import LinkedInClient
from app.linkedin.strategies.voyager_dash import SECTION_ROUTES, VoyagerDashStrategy
from tests.conftest import PROFILE_URN

BASE = "https://www.linkedin.com/voyager/api"
TOP_CARD_URL = f"{BASE}/identity/dash/profiles"


@pytest.fixture
async def client(settings):
    c = LinkedInClient(settings)
    yield c
    await c.aclose()


def mock_all(top_card, sections):
    """Serve the top card and every section from the captured fixtures."""
    respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
    for route in SECTION_ROUTES:
        body = sections.get(route, {"data": {}, "included": []})
        respx.get(f"{BASE}/identity/dash/{route}").mock(
            return_value=httpx.Response(200, json=body)
        )


class TestHappyPath:
    @respx.mock
    async def test_builds_a_full_profile(self, client, ref, top_card, sections):
        mock_all(top_card, sections)
        result = await VoyagerDashStrategy().fetch(client, ref)
        p = result.profile
        assert p.full_name == "Ada Lovelace"
        assert len(p.experience) == 11
        assert len(p.skills) == 20
        assert len(p.certifications) == 12
        assert not result.warnings

    @respx.mock
    async def test_top_card_is_queried_by_vanity_name(self, client, ref, top_card, sections):
        """Verified: q=memberIdentity accepts the vanity name directly.

        q=publicIdentifier returns 400, and passing a full URN here returns 403.
        """
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json=sections.get(name, {"included": []}))
            )
        await VoyagerDashStrategy().fetch(client, ref)

        params = route.calls[0].request.url.params
        assert params["q"] == "memberIdentity"
        assert params["memberIdentity"] == ref.public_identifier
        assert "urn:li:" not in params["memberIdentity"]

    @respx.mock
    async def test_sections_are_queried_by_encoded_profile_urn(
        self, client, ref, top_card, sections
    ):
        """Verified: sections use q=viewee with a url encoded profileUrn."""
        respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        positions = respx.get(f"{BASE}/identity/dash/profilePositions").mock(
            return_value=httpx.Response(200, json=sections["profilePositions"])
        )
        for name in SECTION_ROUTES:
            if name != "profilePositions":
                respx.get(f"{BASE}/identity/dash/{name}").mock(
                    return_value=httpx.Response(200, json={"included": []})
                )
        await VoyagerDashStrategy().fetch(client, ref)

        params = positions.calls[0].request.url.params
        assert params["q"] == "viewee"
        assert params["profileUrn"] == PROFILE_URN

    @respx.mock
    async def test_position_groups_are_not_fetched(self, client, ref, top_card, sections):
        """They carry no field Position lacks, so the call is pure cost."""
        mock_all(top_card, sections)
        skipped = respx.get(f"{BASE}/identity/dash/profilePositionGroups")
        await VoyagerDashStrategy().fetch(client, ref)
        assert not skipped.called


class TestSectionFailures:
    @respx.mock
    async def test_one_failed_section_does_not_sink_the_profile(
        self, client, ref, top_card, sections
    ):
        mock_all(top_card, sections)
        respx.get(f"{BASE}/identity/dash/profileSkills").mock(
            return_value=httpx.Response(500)
        )
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert result.profile.skills == []
        assert len(result.profile.experience) == 11
        assert any("profileSkills" in w for w in result.warnings)

    @respx.mock
    async def test_an_empty_section_is_not_a_failure(self, client, ref, top_card, sections):
        """A person with no patents gets a valid empty collection, about 232 bytes."""
        mock_all(top_card, sections)
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert result.profile.patents == []
        assert not any("profilePatents" in w for w in result.warnings)


class TestTopCardFailures:
    @respx.mock
    async def test_no_profile_entity_is_a_404(self, client, ref):
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(200, json={"data": {}, "included": []})
        )
        with pytest.raises(ProfileNotFound):
            await VoyagerDashStrategy().fetch(client, ref)

    @respx.mock
    async def test_401_means_the_cookie_is_dead(self, client, ref):
        respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(AuthenticationFailed):
            await VoyagerDashStrategy().fetch(client, ref)

    @respx.mock
    async def test_999_means_linkedin_thinks_we_are_a_bot(self, client, ref):
        respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(999))
        with pytest.raises(ChallengeRequired):
            await VoyagerDashStrategy().fetch(client, ref)

    @respx.mock
    async def test_429_reports_upstream_throttling(self, client, ref):
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(429, headers={"retry-after": "30"})
        )
        with pytest.raises(UpstreamRateLimited) as caught:
            await VoyagerDashStrategy().fetch(client, ref)
        assert caught.value.retry_after == 30

    @respx.mock
    async def test_a_dead_cookie_quarantines_the_session(self, client, ref):
        respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(403))
        with pytest.raises(AuthenticationFailed):
            await VoyagerDashStrategy().fetch(client, ref)
        assert client.pool.sessions[0].healthy is False


class TestRoutingCookieHandshake:
    """The bug that made every live request fail.

    LinkedIn answers a request that lacks its routing cookie with a 302 back to
    the same URL, plus `Set-Cookie: lidc=...`. A browser stores the cookie and
    retries, and the retry returns 200.

    The old client sent a fixed cookie dict on every request and followed no
    redirects, so it never stored `lidc` and was redirected forever. Worse, it
    mapped the redirect to `profile_not_found`, which hid the real cause.

    Each session now owns an httpx client, and therefore a cookie jar.
    """

    @respx.mock
    async def test_a_302_to_the_same_url_is_completed_not_reported_as_missing(
        self, client, ref, top_card, sections
    ):
        calls = {"n": 0}

        def responder(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    302,
                    headers={
                        "location": str(request.url),
                        "set-cookie": "lidc=b=OB1; Path=/; Domain=.linkedin.com",
                    },
                )
            return httpx.Response(200, json=top_card)

        respx.get(TOP_CARD_URL).mock(side_effect=responder)
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json=sections.get(name, {"included": []}))
            )

        result = await VoyagerDashStrategy().fetch(client, ref)
        assert calls["n"] == 2, "the redirect should have been followed"
        assert result.profile.full_name == "Ada Lovelace"

    @respx.mock
    async def test_the_routing_cookie_is_kept_for_later_requests(self, client, ref, top_card):
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(
                200,
                json=top_card,
                headers={"set-cookie": "lidc=b=OB1; Path=/; Domain=.linkedin.com"},
            )
        )
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        jar = client.pool.sessions[0].status()["cookies_held"]
        assert "lidc" in jar
        assert "li_at" in jar

    @respx.mock
    async def test_a_redirect_to_a_checkpoint_is_still_a_challenge(self, client, ref):
        checkpoint = "https://www.linkedin.com/checkpoint/challenge/x"
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(302, headers={"location": checkpoint})
        )
        respx.get(checkpoint).mock(
            return_value=httpx.Response(200, html="<html>Verify you are human</html>")
        )
        with pytest.raises(ChallengeRequired):
            await VoyagerDashStrategy().fetch(client, ref)


class TestVoyagerHeaders:
    @respx.mock
    async def test_sends_the_csrf_pair_linkedin_requires(self, client, ref, top_card):
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        request = route.calls[0].request
        assert request.headers["csrf-token"] == "ajax:1234567890123456789"
        assert 'JSESSIONID="ajax:1234567890123456789"' in request.headers["cookie"]
        assert "li_at=fake-li-at-cookie" in request.headers["cookie"]
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
