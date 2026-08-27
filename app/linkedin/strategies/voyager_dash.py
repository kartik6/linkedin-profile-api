"""Strategy 2: the current dash and GraphQL routes.

The web app now reads a profile in two steps:

  1. Resolve the vanity name to a profile URN.
       GET /voyager/api/identity/dash/profiles
           ?q=memberIdentity&memberIdentity={vanity}&decorationId=...
  2. Ask for the entities that hang off that URN.

We ask for a decoration that already includes the sections, so one call is
usually enough. A decoration id is a server side projection name. LinkedIn
versions them with a numeric suffix and retires old ones, so we try a short
list and keep the first that answers.

The GraphQL route is the newest shape. Its queryId is a build hash that
changes whenever LinkedIn ships the web app, so we read it from settings.
That way an operator can repair this path with an environment variable and a
restart, with no code change and no redeploy of a new image.
"""

from __future__ import annotations

import logging

from app.errors import LinkedInAPIError, ProfileNotFound
from app.linkedin.client import LinkedInClient
from app.linkedin.entities import EntityPool
from app.linkedin.normalize import completeness, from_entity_pool
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.urls import ProfileRef

log = logging.getLogger(__name__)

_DECORATIONS = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-90",
    "com.linkedin.voyager.dash.deco.identity.profile.WebTopCardCore-6",
    "com.linkedin.voyager.dash.deco.identity.profile.TopCardComplete-1",
)

# The sections we ask for one by one when the bundled decoration comes back thin.
_SECTIONS = (
    "EXPERIENCE",
    "EDUCATION",
    "SKILLS",
    "LICENSES_AND_CERTIFICATIONS",
    "LANGUAGES",
    "PROJECTS",
    "HONORS",
    "VOLUNTEERING_EXPERIENCE",
    "PUBLICATIONS",
    "COURSES",
)


class VoyagerDashStrategy(Strategy):
    name = "voyager_dash"
    needs_auth = True
    description = "Current dash REST route, with a GraphQL fallback for each section."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        pool = EntityPool()
        warnings: list[str] = []
        last_error: Exception | None = None

        for decoration in _DECORATIONS:
            try:
                data = await client.get_json(
                    "/identity/dash/profiles",
                    params={
                        "q": "memberIdentity",
                        "memberIdentity": ref.public_identifier,
                        "decorationId": decoration,
                    },
                    referer=ref.canonical_url,
                )
            except ProfileNotFound:
                raise
            except LinkedInAPIError as exc:
                last_error = exc
                log.debug("Decoration %s failed: %s", decoration, exc)
                continue

            pool.merge(EntityPool.from_payload(data))
            if pool.by_type("Profile", "MiniProfile"):
                break

        if not len(pool):
            raise last_error or LinkedInAPIError("No dash decoration returned a profile.")

        profile = from_entity_pool(pool, ref)

        # Top up the thin sections through GraphQL, if we know the query hash.
        if completeness(profile) < 0.7:
            urn = profile.urn
            if urn:
                extra = await self._fetch_sections(client, ref, urn, warnings)
                if len(extra):
                    pool.merge(extra)
                    profile = from_entity_pool(pool, ref)
            else:
                warnings.append("Could not resolve a profile URN, so sections were skipped.")

        return StrategyResult(
            name=self.name,
            profile=profile,
            raw={"entity_types": pool.type_counts()},
            warnings=warnings,
        )

    async def _fetch_sections(
        self,
        client: LinkedInClient,
        ref: ProfileRef,
        urn: str,
        warnings: list[str],
    ) -> EntityPool:
        pool = EntityPool()
        query_id = client.settings.query_id_profile_components
        for section in _SECTIONS:
            variables = f"(profileUrn:{urn},sectionType:{section.lower()})"
            try:
                data = await client.get_json(
                    "/graphql",
                    params={"variables": variables, "queryId": query_id},
                    referer=ref.canonical_url,
                )
            except LinkedInAPIError as exc:
                warnings.append(f"Section {section.lower()} failed: {exc.code}")
                # A bad queryId fails every section. Stop after the first miss.
                break
            pool.merge(EntityPool.from_payload(data))
        return pool
