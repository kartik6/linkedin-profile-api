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
    """A client whose sessions are already warmed.

    Warm up is tested on its own in TestWarmUp. Most tests are about what
    happens after it, so marking the session warmed keeps them focused.
    """
    c = LinkedInClient(settings)
    for session in c.pool.sessions:
        session.warmed = True
    yield c
    await c.aclose()


@pytest.fixture
async def cold_client(settings):
    """A client that has not warmed up yet."""
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

    @respx.mock
    async def test_sends_no_invented_client_identity(self, client, ref, top_card):
        """Never claim to be a client we are not.

        We used to send x-li-track announcing mpName voyager-web at version
        1.13.27340. Both were invented. LinkedIn's real client calls itself
        flagship-web 0.2.6975, so every request advertised a version that does
        not exist. A live session was revoked after three such calls.

        Announcing a nonexistent client is louder than announcing nothing.
        """
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        sent = {k.lower() for k in route.calls[0].request.headers}
        # These two are absent from the captured working request, so we must
        # not send them. x-li-track in particular announced mpName voyager-web
        # at a client version that no longer exists.
        for invented in ("x-li-track", "x-li-page-instance", "x-li-lang"):
            assert invented not in sent, f"{invented} was fabricated and must not be sent"

    @respx.mock
    async def test_carries_the_liap_authentication_flag(self, client, ref, top_card):
        """`liap=true` marks the session authenticated on www.linkedin.com.

        It is set at login, and we only ever copied li_at and JSESSIONID out of
        the browser, so we never had it. LinkedIn saw a session token with no
        matching authentication flag and cleared the session outright:

            Set-Cookie: liap=...; Expires=Thu, 01-Jan-1970; Max-Age=0
            Set-Cookie: li_at=...; Expires=Thu, 01-Jan-1970; Max-Age=0

        That is the logout that killed every run after three or four calls.
        """
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        cookie_header = route.calls[0].request.headers.get("cookie", "")
        assert "liap=true" in cookie_header

    @respx.mock
    async def test_client_hints_agree_with_the_user_agent(self, client, ref, top_card):
        """A Chrome/151 user agent with sec-ch-ua claiming 131 is a contradiction."""
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        headers = route.calls[0].request.headers
        assert "Chrome/151" in headers["user-agent"]
        assert 'v="151"' in headers["sec-ch-ua"]

    @respx.mock
    async def test_trace_headers_are_internally_consistent(self, client, ref, top_card):
        """traceparent embeds the pageforestid, and tracestate the span id."""
        route = respx.get(TOP_CARD_URL).mock(return_value=httpx.Response(200, json=top_card))
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json={"included": []})
            )
        await VoyagerDashStrategy().fetch(client, ref)

        h = route.calls[0].request.headers
        forest, parent, state = (
            h["x-li-pageforestid"], h["x-li-traceparent"], h["x-li-tracestate"]
        )
        version, trace_id, span_id, flags = parent.split("-")
        assert (version, flags) == ("00", "00")
        assert trace_id == forest
        assert state == f"LinkedIn={span_id}"


class TestWarmUp:
    """Collect LinkedIn's browser identity cookies before the first API call.

    Observed in production: a client that goes straight to a Voyager endpoint
    with only `li_at` gets a 302 back to the same URL, with LinkedIn re-issuing
    `li_at`, `li_a` and `liap` on every hop. That is session establishment, and
    it never terminates, because we never come back holding what it issued.

    A browser loads linkedin.com first and is handed `bcookie`, `bscookie` and
    `lidc`. We do the same.
    """

    @respx.mock
    async def test_loads_the_home_page_before_the_first_api_call(
        self, cold_client, ref, top_card, sections
    ):
        home = respx.get("https://www.linkedin.com/").mock(
            return_value=httpx.Response(
                200,
                html="<html></html>",
                headers=[
                    ("set-cookie", "bcookie=v=2&abc; Domain=.linkedin.com; Path=/"),
                    ("set-cookie", "lidc=b=OB1; Domain=.linkedin.com; Path=/"),
                ],
            )
        )
        mock_all(top_card, sections)

        await VoyagerDashStrategy().fetch(cold_client, ref)
        assert home.called

    @respx.mock
    async def test_copies_the_browser_cookies_into_the_session_jar(
        self, cold_client, ref, top_card, sections
    ):
        respx.get("https://www.linkedin.com/").mock(
            return_value=httpx.Response(
                200,
                html="<html></html>",
                headers=[
                    ("set-cookie", "bcookie=v=2&abc; Domain=.linkedin.com; Path=/"),
                    ("set-cookie", "bscookie=v=1&xyz; Domain=.linkedin.com; Path=/"),
                    ("set-cookie", "lidc=b=OB1; Domain=.linkedin.com; Path=/"),
                ],
            )
        )
        mock_all(top_card, sections)

        await VoyagerDashStrategy().fetch(cold_client, ref)

        held = cold_client.pool.sessions[0].status()["cookies_held"]
        assert "bcookie" in held
        assert "bscookie" in held
        assert "lidc" in held
        assert "li_at" in held

    @respx.mock
    async def test_it_happens_only_once_per_session(
        self, cold_client, ref, top_card, sections
    ):
        home = respx.get("https://www.linkedin.com/").mock(
            return_value=httpx.Response(200, html="<html></html>")
        )
        mock_all(top_card, sections)

        await VoyagerDashStrategy().fetch(cold_client, ref)
        await VoyagerDashStrategy().fetch(cold_client, ref)

        assert home.call_count == 1, "warm up must not repeat on every request"

    @respx.mock
    async def test_a_failed_warm_up_does_not_block_the_request(
        self, cold_client, ref, top_card, sections
    ):
        """The warm up is an optimisation, not a precondition."""
        respx.get("https://www.linkedin.com/").mock(side_effect=httpx.ConnectError("down"))
        mock_all(top_card, sections)

        result = await VoyagerDashStrategy().fetch(cold_client, ref)
        assert result.profile.full_name == "Ada Lovelace"


class TestDuplicateCookies:
    """A jar keys on (name, domain, path), so one name can be stored twice.

    We seed `li_at` host-scoped. LinkedIn sets its own with a Domain attribute.
    Both then go out in a single Cookie header, LinkedIn sees two conflicting
    session tokens, and answers 302 back to the same URL while re-issuing the
    cookie. That is an infinite loop, and it explains why only the very first
    request ever succeeded.
    """

    async def test_a_second_li_at_is_dropped_and_ours_survives(self, settings):
        client = LinkedInClient(settings)
        try:
            session = client.pool.sessions[0]
            jar = session.client(
                user_agent=settings.user_agent, timeout=settings.request_timeout_s
            ).cookies
            # LinkedIn re-issuing li_at under a different domain scope.
            jar.set("li_at", "linkedin-reissued-value", domain=".www.linkedin.com", path="/")
            assert len([c for c in jar.jar if c.name == "li_at"]) == 2

            dropped = session.reconcile_cookies()

            surviving = [c for c in jar.jar if c.name == "li_at"]
            assert len(surviving) == 1
            assert surviving[0].value == "fake-li-at-cookie"
            assert dropped
        finally:
            await client.aclose()

    async def test_non_auth_cookies_keep_the_most_specific_domain(self, settings):
        client = LinkedInClient(settings)
        try:
            session = client.pool.sessions[0]
            jar = session.client(
                user_agent=settings.user_agent, timeout=settings.request_timeout_s
            ).cookies
            jar.set("lidc", "old", domain="linkedin.com", path="/")
            jar.set("lidc", "new", domain=".www.linkedin.com", path="/")

            session.reconcile_cookies()

            surviving = [c for c in jar.jar if c.name == "lidc"]
            assert len(surviving) == 1
            assert surviving[0].value == "new"
        finally:
            await client.aclose()

    @respx.mock
    async def test_only_one_li_at_reaches_the_wire(self, client, ref, top_card, sections):
        route = respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(
                200,
                json=top_card,
                headers={"set-cookie": "li_at=reissued; Domain=.www.linkedin.com; Path=/"},
            )
        )
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json=sections.get(name, {"included": []}))
            )

        await VoyagerDashStrategy().fetch(client, ref)

        for call in route.calls:
            header = call.request.headers.get("cookie", "")
            names = [p.split("=", 1)[0].strip() for p in header.split(";") if p.strip()]
            assert names.count("li_at") <= 1, f"two session tokens went out: {header}"


class TestSessionRevoked:
    """LinkedIn deletes the session cookie when the token is no longer valid.

    Observed live: every route answered 302 back to its own URL while sending
    `Set-Cookie: li_at=...; Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0`
    for li_at, li_a and liap together. An expiry in the past is a deletion, not
    an assignment.

    Following that redirect just presents the same dead token again, so the
    client looped to the redirect limit and reported a generic transport error.
    The operator needs to be told to replace the cookie.
    """

    EXPIRED = (
        "li_at=x; Version=1; Path=/; Domain=.www.linkedin.com; "
        "Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0; Secure; SameSite=None; HttpOnly"
    )

    @respx.mock
    async def test_a_deleted_session_cookie_is_named_not_followed(self, client, ref):
        route = respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(
                302,
                headers=[("location", TOP_CARD_URL), ("set-cookie", self.EXPIRED)],
            )
        )
        with pytest.raises(AuthenticationFailed, match="no longer"):
            await VoyagerDashStrategy().fetch(client, ref)
        assert route.call_count == 1, "must not loop on a revoked session"

    @respx.mock
    async def test_it_quarantines_the_session(self, client, ref):
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(
                302, headers=[("location", TOP_CARD_URL), ("set-cookie", self.EXPIRED)]
            )
        )
        with pytest.raises(AuthenticationFailed):
            await VoyagerDashStrategy().fetch(client, ref)
        assert client.pool.sessions[0].healthy is False

    @respx.mock
    async def test_a_normal_cookie_refresh_is_not_mistaken_for_a_logout(
        self, client, ref, top_card, sections
    ):
        """A future expiry is a real assignment and must pass through."""
        respx.get(TOP_CARD_URL).mock(
            return_value=httpx.Response(
                200,
                json=top_card,
                headers=[
                    ("set-cookie", "li_at=fresh; Path=/; Expires=Thu, 01-Jan-2099 00:00:00 GMT")
                ],
            )
        )
        for name in SECTION_ROUTES:
            respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json=sections.get(name, {"included": []}))
            )
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert result.profile.full_name == "Ada Lovelace"


class TestSectionCoverage:
    """A section we never asked for must not look like a section the person lacks."""

    @respx.mock
    async def test_every_section_is_fetched_by_default(
        self, client, ref, top_card, sections
    ):
        mock_all(top_card, sections)
        routes = {
            name: respx.get(f"{BASE}/identity/dash/{name}").mock(
                return_value=httpx.Response(200, json=sections.get(name, {"included": []}))
            )
            for name in SECTION_ROUTES
        }
        await VoyagerDashStrategy().fetch(client, ref)
        missed = [name for name, route in routes.items() if not route.called]
        assert not missed, f"these sections were never requested: {missed}"

    @respx.mock
    async def test_projects_reach_the_profile(self, client, ref, top_card, sections):
        """Regression: profileProjects was dropped from the default list, so a
        real project silently never appeared in the response."""
        mock_all(top_card, sections)
        result = await VoyagerDashStrategy().fetch(client, ref)
        assert result.profile.projects

    @respx.mock
    async def test_a_trimmed_list_says_what_it_left_out(
        self, settings, ref, top_card, sections
    ):
        settings.sections = ["profilePositions"]
        client = LinkedInClient(settings)
        for session in client.pool.sessions:
            session.warmed = True
        try:
            mock_all(top_card, sections)
            result = await VoyagerDashStrategy().fetch(client, ref)
            note = " ".join(result.warnings)
            assert "not fetched" in note
            assert "profileProjects" in note
            assert "profilePositions" not in note
        finally:
            await client.aclose()
