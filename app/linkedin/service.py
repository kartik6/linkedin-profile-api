"""Run the strategies in order and return the best profile we can build.

The rule is simple. Walk the configured strategies. Stop at the first result
that is complete enough. If nothing is complete enough, merge everything we
did get and return that, with a warning that says which sections are missing.

A partial answer beats an error for this kind of service. The caller can see
exactly what is missing through `meta.completeness` and `meta.warnings`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from app.cache import Cache, profile_key
from app.config import Settings
from app.errors import (
    AllStrategiesFailed,
    LinkedInAPIError,
    ProfileNotFound,
)
from app.linkedin.client import LinkedInClient
from app.linkedin.normalize import completeness, merge_profiles, missing_sections
from app.linkedin.strategies import Strategy, StrategyResult, build
from app.linkedin.urls import ProfileRef, parse_profile_url
from app.models import Meta, Profile, ProfileResponse

log = logging.getLogger(__name__)

# Stop early once a result reaches this score. Below it, keep trying and merge.
GOOD_ENOUGH = 0.8


class ProfileService:
    def __init__(self, settings: Settings, client: LinkedInClient, cache: Cache) -> None:
        self.settings = settings
        self.client = client
        self.cache = cache
        self.strategies: list[Strategy] = build(settings.strategies)

    async def get_profile(self, url: str, *, refresh: bool = False) -> ProfileResponse:
        started = time.perf_counter()
        ref = parse_profile_url(url)
        key = profile_key(ref.public_identifier)

        if not refresh:
            cached = await self.cache.get(key)
            if cached:
                response = ProfileResponse.model_validate(cached)
                response.meta.cached = True
                response.meta.duration_ms = int((time.perf_counter() - started) * 1000)
                return response

        response = await self._fetch(ref, started)
        await self.cache.set(
            key, response.model_dump(mode="json"), self.settings.cache_ttl_s
        )
        return response

    async def _fetch(self, ref: ProfileRef, started: float) -> ProfileResponse:
        results: list[StrategyResult] = []
        tried: list[str] = []
        warnings: list[str] = []
        errors: dict[str, str] = {}
        not_found_votes = 0
        have_session = self.client.pool.configured

        for strategy in self.strategies:
            if strategy.needs_auth and not have_session:
                warnings.append(
                    f"Skipped {strategy.name}: the server has no LinkedIn cookie."
                )
                continue

            tried.append(strategy.name)
            try:
                result = await strategy.fetch(self.client, ref)
            except ProfileNotFound as exc:
                not_found_votes += 1
                errors[strategy.name] = exc.code
                log.info("%s: profile not found for %s", strategy.name, ref.public_identifier)
                continue
            except LinkedInAPIError as exc:
                errors[strategy.name] = exc.code
                log.info("%s failed for %s: %s", strategy.name, ref.public_identifier, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad strategy must not end the run
                errors[strategy.name] = "unexpected_error"
                log.exception("%s raised for %s: %s", strategy.name, ref.public_identifier, exc)
                continue

            results.append(result)
            warnings.extend(result.warnings)
            score = completeness(result.profile)
            log.info("%s scored %.2f for %s", strategy.name, score, ref.public_identifier)
            if score >= GOOD_ENOUGH:
                break

        if not results:
            if not_found_votes and not_found_votes == len(tried):
                raise ProfileNotFound(
                    f"No profile at /in/{ref.public_identifier}/.", detail=errors
                )
            raise AllStrategiesFailed(
                "Every strategy failed. See detail for the reason from each.",
                detail={"tried": tried, "errors": errors},
            )

        # Merge, richest first, so the best source wins each field.
        results.sort(key=lambda r: completeness(r.profile), reverse=True)
        profile: Profile = results[0].profile
        for other in results[1:]:
            profile = merge_profiles(profile, other.profile)

        missing = missing_sections(profile)
        if missing:
            warnings.append(
                "These sections came back empty: " + ", ".join(missing) + ". "
                "The profile owner may hide them, or our login may not see them."
            )
        for name, code in errors.items():
            warnings.append(f"Strategy {name} failed with {code}.")

        return ProfileResponse(
            profile=profile,
            meta=Meta(
                strategy=results[0].name,
                strategies_tried=tried,
                cached=False,
                fetched_at=datetime.now(UTC),
                duration_ms=int((time.perf_counter() - started) * 1000),
                completeness=completeness(profile),
                partial=bool(missing),
                warnings=warnings,
            ),
        )
