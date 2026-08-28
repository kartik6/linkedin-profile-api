"""Read a profile through Voyager's typed entity collections.

Every route and argument here was verified by hand against LinkedIn on
2026-08-28. Nothing in this file is assumed.

The chain:

    profile URL
      -> vanity name, for example "kartik-sharma-"
      -> GET /identity/dash/profiles?q=memberIdentity&memberIdentity=<vanity>
         returns a CollectionResponse whose `included` holds one Profile
      -> read entityUrn from that Profile
      -> GET /identity/dash/profile<Section>s?q=viewee&profileUrn=<urn>
         once per section
      -> merge every `included` array into one pool and normalize

Two things worth knowing before you change this file.

`q=memberIdentity` accepts the vanity name directly. We tested the internal id
and the vanity name and both return the same 14 KB response. `q=publicIdentifier`
returns 400, so the query name really is `memberIdentity`.

`memberIdentity` takes the bare vanity name. Passing a full
`urn:li:fsd_profile:...` there returns 403 with a VoyagerUserVisibleException.

`profileUrn` takes the full URN, percent encoded on the wire as
`urn%3Ali%3Afsd_profile%3A...`. httpx does that encoding itself, so we pass the
URN raw. Encoding it before handing it to httpx double encodes it to `%253A`
and LinkedIn will not match it.

Routes we removed, and why:

  /identity/profiles/{id}/profileView   410 Gone. LinkedIn retired it.
  /identity/dash/profileCards/...       404 on every variant we tried.
  /identity/dash/profileComponents/...  404 on every variant we tried.

The profile page itself no longer helps either. It is server rendered as a
Server Driven UI tree, `proto.sdui.*`, which carries presentation rather than
domain entities. There is no Position or Education in it to read.
"""

from __future__ import annotations

import logging

from app.errors import LinkedInAPIError, ProfileNotFound
from app.linkedin.client import LinkedInClient
from app.linkedin.entities import EntityPool
from app.linkedin.normalize import from_entity_pool
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.urls import ProfileRef

log = logging.getLogger(__name__)

# Route segment -> the profile section it fills.
# Verified: all of these return 200. An absent section returns a valid empty
# collection of about 232 bytes, not an error.
SECTION_ROUTES: dict[str, str] = {
    "profilePositions": "experience",
    "profileEducations": "education",
    "profileSkills": "skills",
    "profileCertifications": "certifications",
    "profileLanguages": "languages",
    "profileProjects": "projects",
    "profileVolunteerExperiences": "volunteering",
    "profileHonors": "honors",
    "profilePublications": "publications",
    "profileCourses": "courses",
    "profilePatents": "patents",
    "profileOrganizations": "organizations",
    "profileTestScores": "test_scores",
}

# Deliberately not fetched. profilePositionGroups returns only companyName,
# companyUrn and dateRange, which Position already carries, and nothing links
# the two. It costs a request and adds no field.
SKIPPED_ROUTES = ("profilePositionGroups",)


class VoyagerDashStrategy(Strategy):
    name = "voyager_dash"
    needs_auth = True
    description = "Typed Voyager collections: one call for the top card, one per section."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        pool, urn = await self._top_card(client, ref)
        warnings = await self._sections(client, ref, urn, pool)
        profile = from_entity_pool(pool, ref)
        return StrategyResult(
            name=self.name,
            profile=profile,
            raw={"profile_urn": urn, "entity_types": pool.type_counts()},
            warnings=warnings,
        )

    async def _top_card(
        self, client: LinkedInClient, ref: ProfileRef
    ) -> tuple[EntityPool, str]:
        data = await client.get_json(
            "/identity/dash/profiles",
            params={"q": "memberIdentity", "memberIdentity": ref.public_identifier},
            referer=ref.canonical_url,
        )
        pool = EntityPool.from_payload(data)
        entity = pool.first("Profile", "MiniProfile")
        if entity is None:
            raise ProfileNotFound(
                f"No profile at /in/{ref.public_identifier}/.",
                detail={"entity_types": pool.type_counts()},
            )
        urn = entity.get("entityUrn")
        if not isinstance(urn, str):
            raise LinkedInAPIError("The profile carried no entityUrn, so sections cannot load.")
        return pool, urn

    async def _sections(
        self,
        client: LinkedInClient,
        ref: ProfileRef,
        urn: str,
        pool: EntityPool,
    ) -> list[str]:
        """Fetch every section. One failure costs one section, never the profile."""
        wanted = client.settings.sections or list(SECTION_ROUTES)
        warnings: list[str] = []

        # An empty section normally means the person has none. A section we
        # never asked for also comes back empty, and the two are
        # indistinguishable in the response unless we say so here.
        skipped = [route for route in SECTION_ROUTES if route not in wanted]
        if skipped:
            warnings.append(
                "These sections were not fetched, so their absence says nothing "
                "about the profile: " + ", ".join(skipped) + "."
            )

        async def one(route: str) -> tuple[str, EntityPool | None, str | None]:
            try:
                data = await client.get_json(
                    f"/identity/dash/{route}",
                    # Pass the URN raw. httpx percent encodes the colons into
                    # %3A, which is the form we verified against LinkedIn.
                    # Pre-encoding it here would double encode to %253A.
                    params={"q": "viewee", "profileUrn": urn},
                    referer=ref.canonical_url,
                )
                return route, EntityPool.from_payload(data), None
            except LinkedInAPIError as exc:
                return route, None, exc.code
            except Exception:  # noqa: BLE001 - a bad section must not sink the profile
                log.exception("Section %s raised for %s", route, ref.public_identifier)
                return route, None, "unexpected_error"

        # Sequential on purpose. Firing every section at once is what a
        # script looks like, and we watched LinkedIn revoke a live session in
        # the middle of that burst. The shared limiter paces us either way, so
        # this costs nothing in wall clock and looks far less mechanical.
        results = [await one(route) for route in wanted]
        for route, section_pool, error in results:
            if section_pool is not None:
                pool.merge(section_pool)
            else:
                warnings.append(f"Section {route} failed with {error}.")
        return warnings
