"""Strategy 1: the classic profileView document.

    GET /voyager/api/identity/profiles/{publicIdentifier}/profileView

This one call returns every section of the profile in a single nested
document. It is the oldest Voyager route and the cheapest by far, so we try it
first. LinkedIn has turned it off for some accounts and some regions, and when
it is off it answers 403 rather than 404. The orchestrator then moves on.
"""

from __future__ import annotations

from app.linkedin.client import LinkedInClient
from app.linkedin.normalize import from_profile_view
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.urls import ProfileRef


class VoyagerProfileViewStrategy(Strategy):
    name = "voyager_profile_view"
    needs_auth = True
    description = "Single legacy REST call that returns every section at once."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        data = await client.get_json(
            f"/identity/profiles/{ref.public_identifier}/profileView",
            accept="application/json",
            referer=ref.canonical_url,
        )
        profile = from_profile_view(data, ref)
        return StrategyResult(name=self.name, profile=profile, raw=data)
