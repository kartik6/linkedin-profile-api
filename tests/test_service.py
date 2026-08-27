"""The orchestrator: fall through the strategies, then merge what came back."""

from __future__ import annotations

import pytest

from app.cache import MemoryCache
from app.errors import (
    AllStrategiesFailed,
    AuthenticationFailed,
    InvalidProfileURL,
    LinkedInAPIError,
    ProfileNotFound,
)
from app.linkedin.client import LinkedInClient
from app.linkedin.normalize import from_profile_view
from app.linkedin.service import ProfileService
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.models import Company, Experience, Profile, Skill


class FakeStrategy(Strategy):
    """A strategy that returns or raises whatever a test needs."""

    def __init__(self, name, profile=None, error=None, needs_auth=True):
        self.name = name
        self.needs_auth = needs_auth
        self._profile = profile
        self._error = error
        self.calls = 0

    async def fetch(self, client, ref):
        self.calls += 1
        if self._error:
            raise self._error
        return StrategyResult(name=self.name, profile=self._profile)


def build_service(settings, strategies):
    service = ProfileService(settings, LinkedInClient(settings), MemoryCache())
    service.strategies = strategies
    return service


@pytest.fixture
def rich(profile_view, ref):
    return from_profile_view(profile_view, ref)


class TestFallback:
    async def test_stops_at_the_first_good_result(self, settings, rich):
        first = FakeStrategy("first", profile=rich)
        second = FakeStrategy("second", profile=rich)
        service = build_service(settings, [first, second])

        response = await service.get_profile("adalovelace")

        assert response.meta.strategy == "first"
        assert second.calls == 0
        assert response.meta.completeness == 1.0
        assert response.meta.partial is False

    async def test_moves_on_when_a_strategy_fails(self, settings, rich):
        broken = FakeStrategy("broken", error=AuthenticationFailed())
        working = FakeStrategy("working", profile=rich)
        service = build_service(settings, [broken, working])

        response = await service.get_profile("adalovelace")

        assert response.meta.strategy == "working"
        assert response.meta.strategies_tried == ["broken", "working"]
        assert any("linkedin_session_invalid" in w for w in response.meta.warnings)

    async def test_merges_two_thin_results(self, settings):
        top_card = Profile(first_name="Ada", last_name="Lovelace", headline="Engineer")
        sections = Profile(
            skills=[Skill(name="Rust")],
            experience=[Experience(title="Engineer", company=Company(name="Acme"))],
        )
        service = build_service(
            settings,
            [FakeStrategy("a", profile=top_card), FakeStrategy("b", profile=sections)],
        )

        response = await service.get_profile("adalovelace")

        assert response.profile.full_name is None or response.profile.first_name == "Ada"
        assert response.profile.skills[0].name == "Rust"
        assert response.profile.experience[0].company.name == "Acme"
        assert response.meta.partial is True

    async def test_names_the_missing_sections(self, settings):
        thin = Profile(first_name="Ada", skills=[Skill(name="Rust")])
        service = build_service(settings, [FakeStrategy("thin", profile=thin)])

        response = await service.get_profile("adalovelace")

        warning = " ".join(response.meta.warnings)
        assert "experience" in warning
        assert "certifications" in warning

    async def test_one_strategy_raising_an_unexpected_error_is_contained(
        self, settings, rich
    ):
        exploding = FakeStrategy("exploding", error=ZeroDivisionError("bad parser"))
        working = FakeStrategy("working", profile=rich)
        service = build_service(settings, [exploding, working])

        response = await service.get_profile("adalovelace")
        assert response.meta.strategy == "working"

    async def test_every_strategy_failing_raises_once(self, settings):
        service = build_service(
            settings,
            [
                FakeStrategy("a", error=AuthenticationFailed()),
                FakeStrategy("b", error=LinkedInAPIError("boom")),
            ],
        )
        with pytest.raises(AllStrategiesFailed) as caught:
            await service.get_profile("adalovelace")
        assert caught.value.detail["errors"] == {
            "a": "linkedin_session_invalid",
            "b": "internal_error",
        }

    async def test_agreed_not_found_becomes_a_404(self, settings):
        service = build_service(
            settings,
            [
                FakeStrategy("a", error=ProfileNotFound()),
                FakeStrategy("b", error=ProfileNotFound()),
            ],
        )
        with pytest.raises(ProfileNotFound):
            await service.get_profile("nobody-here")

    async def test_skips_authenticated_strategies_with_no_cookie(self):
        from app.config import Settings

        settings = Settings(li_at=None, jsessionid=None, outbound_rps=0)
        public = FakeStrategy(
            "public", profile=Profile(first_name="Ada"), needs_auth=False
        )
        private = FakeStrategy("private", profile=Profile(first_name="Ada"))
        service = build_service(settings, [private, public])

        response = await service.get_profile("adalovelace")

        assert private.calls == 0
        assert response.meta.strategy == "public"
        assert any("no LinkedIn cookie" in w for w in response.meta.warnings)

    async def test_a_bad_url_never_reaches_linkedin(self, settings, rich):
        strategy = FakeStrategy("s", profile=rich)
        service = build_service(settings, [strategy])
        with pytest.raises(InvalidProfileURL):
            await service.get_profile("https://www.linkedin.com/company/microsoft/")
        assert strategy.calls == 0


class TestCaching:
    async def test_second_call_uses_the_cache(self, settings, rich):
        strategy = FakeStrategy("s", profile=rich)
        service = build_service(settings, [strategy])

        first = await service.get_profile("adalovelace")
        second = await service.get_profile("adalovelace")

        assert strategy.calls == 1
        assert first.meta.cached is False
        assert second.meta.cached is True
        assert second.profile.full_name == "Ada Lovelace"

    async def test_refresh_skips_the_cache(self, settings, rich):
        strategy = FakeStrategy("s", profile=rich)
        service = build_service(settings, [strategy])

        await service.get_profile("adalovelace")
        await service.get_profile("adalovelace", refresh=True)

        assert strategy.calls == 2

    async def test_the_cache_key_ignores_url_shape(self, settings, rich):
        strategy = FakeStrategy("s", profile=rich)
        service = build_service(settings, [strategy])

        await service.get_profile("https://www.linkedin.com/in/adalovelace/")
        await service.get_profile("https://in.linkedin.com/in/AdaLovelace?trk=x")

        assert strategy.calls == 1
