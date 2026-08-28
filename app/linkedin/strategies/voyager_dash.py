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

import asyncio
import logging

from app.errors import (
    AuthenticationFailed,
    ChallengeRequired,
    LinkedInAPIError,
    ProfileNotFound,
)
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

# A Rest.li decoration is a server side projection. This one expands the URN
# references the plain call leaves dangling, so one request brings back Geo,
# Industry, EmploymentType, Company and School as real entities rather than
# pointers. Verified 2026-08-28: 120 KB, 129 entities, and it is where the
# location name "Greater Bengaluru Area" comes from.
#
# It truncates collections though — 10 of 11 positions, 20 of 39 skills — so
# it is used for resolution, not for completeness. The section routes still
# supply the full lists.
#
# The -96 is a version. LinkedIn retires these, so a failure here is expected
# eventually and must not be fatal.
FULL_DECORATION = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96"

# Rest.li pages collections. Ask for a large page, then follow up if the
# reported total is larger than what came back.
PAGE_SIZE = 100
MAX_PAGES = 5

# Which section route owns an entity, keyed by its URN prefix. Used to turn a
# truncated collection back into the call that will complete it.
URN_ROUTE = {
    "fsd_profilePosition": "profilePositions",
    "fsd_profilePositionGroup": "profilePositions",
    "fsd_profileEducation": "profileEducations",
    "fsd_skill": "profileSkills",
    "fsd_profileSkill": "profileSkills",
    "fsd_profileCertification": "profileCertifications",
    "fsd_profileLanguage": "profileLanguages",
    "fsd_profileProject": "profileProjects",
    "fsd_profileHonor": "profileHonors",
    "fsd_profilePublication": "profilePublications",
    "fsd_profileCourse": "profileCourses",
    "fsd_profilePatent": "profilePatents",
    "fsd_profileOrganization": "profileOrganizations",
    "fsd_profileTestScore": "profileTestScores",
    "fsd_profileVolunteerExperience": "profileVolunteerExperiences",
    # fsd_profileTreasuryMedia is deliberately absent. It holds attachments we
    # do not parse, so a truncated one is not worth a request.
}


def truncated_routes(pool: EntityPool) -> list[str]:
    """Name the sections the decoration could not finish.

    The decoration returns a CollectionResponse per section, each carrying
    `paging.total` and the element URNs it actually included. Measured across
    two real profiles: 33 of 36 collections came back complete, and one profile
    needed no follow up at all.

    So rather than refetching every section, we read the paging and refetch
    only what is short. A profile that fits in one response costs one request
    instead of fourteen.
    """
    routes: list[str] = []
    for entity in pool.by_type("CollectionResponse"):
        paging = entity.get("paging") or {}
        total = paging.get("total")
        elements = entity.get("*elements") or []
        if not isinstance(total, int) or len(elements) >= total:
            continue
        if not elements:
            continue
        prefix = str(elements[0]).split(":(")[0].rsplit(":", 1)[-1]
        route = URN_ROUTE.get(prefix)
        if route and route not in routes:
            routes.append(route)
    return routes


class VoyagerDashStrategy(Strategy):
    name = "voyager_dash"
    needs_auth = True
    description = "Typed Voyager collections: one call for the top card, one per section."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        pool, urn, decorated = await self._top_card(client, ref)
        routes = self._routes(client, pool, decorated)
        warnings = await self._sections(client, ref, urn, pool, routes)
        profile = from_entity_pool(pool, ref)
        return StrategyResult(
            name=self.name,
            profile=profile,
            raw={"profile_urn": urn, "entity_types": pool.type_counts()},
            warnings=warnings,
        )

    async def _top_card(
        self, client: LinkedInClient, ref: ProfileRef
    ) -> tuple[EntityPool, str, bool]:
        params = {"q": "memberIdentity", "memberIdentity": ref.public_identifier}
        decorated = True
        try:
            data = await client.get_json(
                "/identity/dash/profiles",
                params={**params, "decorationId": FULL_DECORATION},
                referer=ref.canonical_url,
            )
        except (AuthenticationFailed, ChallengeRequired):
            # Not the decoration's fault. Retrying without it would spend a
            # second request on a session LinkedIn has already rejected.
            raise
        except LinkedInAPIError as exc:
            # The decoration id carries a version number. When LinkedIn retires
            # it we lose the resolved names, not the profile.
            log.warning("Decoration %s failed (%s). Falling back.", FULL_DECORATION, exc.code)
            decorated = False
            data = await client.get_json(
                "/identity/dash/profiles", params=params, referer=ref.canonical_url
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
        return pool, urn, decorated

    def _routes(
        self, client: LinkedInClient, pool: EntityPool, decorated: bool
    ) -> list[str]:
        """Decide which section calls are actually needed."""
        if client.settings.sections:
            return client.settings.sections
        if not decorated:
            # No paging to read, so we cannot tell complete from truncated.
            return list(SECTION_ROUTES)
        return truncated_routes(pool)

    async def _sections(
        self,
        client: LinkedInClient,
        ref: ProfileRef,
        urn: str,
        pool: EntityPool,
        wanted: list[str],
    ) -> list[str]:
        """Top up the short sections. One failure costs one section, never the profile."""
        warnings: list[str] = []
        if not wanted:
            return warnings

        # An explicit SECTIONS list can leave real data out. Say so, because an
        # unrequested section and a section the person lacks both come back
        # empty and are otherwise indistinguishable.
        if client.settings.sections:
            skipped = [r for r in SECTION_ROUTES if r not in wanted]
            if skipped:
                warnings.append(
                    "These sections were not fetched, so their absence says nothing "
                    "about the profile: " + ", ".join(skipped) + "."
                )

        async def one(route: str) -> tuple[str, EntityPool | None, str | None]:
            try:
                section = EntityPool()
                start = 0
                for _ in range(MAX_PAGES):
                    data = await client.get_json(
                        f"/identity/dash/{route}",
                        # Pass the URN raw. httpx percent encodes the colons
                        # into %3A, which is the form we verified against
                        # LinkedIn. Pre-encoding here double encodes to %253A.
                        params={
                            "q": "viewee",
                            "profileUrn": urn,
                            "start": start,
                            "count": PAGE_SIZE,
                        },
                        referer=ref.canonical_url,
                    )
                    section.merge(EntityPool.from_payload(data))
                    paging = (data.get("data") or {}).get("paging") or {}
                    total = paging.get("total")
                    returned = len(paging.get("*elements") or []) or len(
                        (data.get("data") or {}).get("*elements") or []
                    )
                    start += returned or PAGE_SIZE
                    if not returned or not isinstance(total, int) or start >= total:
                        break
                return route, section, None
            except LinkedInAPIError as exc:
                return route, None, exc.code
            except Exception:  # noqa: BLE001 - a bad section must not sink the profile
                log.exception("Section %s raised for %s", route, ref.public_identifier)
                return route, None, "unexpected_error"

        # Concurrent again. The burst theory that made this sequential was
        # wrong: the session revocations were a missing liap cookie. There are
        # rarely more than three of these now, and the shared limiter still
        # paces whatever goes out.
        results = await asyncio.gather(*(one(route) for route in wanted))
        for route, section_pool, error in results:
            if section_pool is not None:
                pool.merge(section_pool)
            else:
                warnings.append(f"Section {route} failed with {error}.")
        return warnings
