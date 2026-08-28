"""The HTTP surface.

Design notes:

  - The response shape never leaks a LinkedIn field name. Callers depend on
    our schema, so we can change how we fetch without breaking them.
  - Every failure returns the same error body with a stable `error` code.
  - A partial profile returns 200 with `meta.partial` set to true. Callers
    almost always prefer six sections over an error.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.cache import build_cache, profile_key
from app.config import get_settings
from app.deps import check_rate_limit, require_api_key
from app.errors import LinkedInAPIError, RateLimited
from app.linkedin.client import LinkedInClient
from app.linkedin.service import ProfileService
from app.linkedin.strategies import REGISTRY
from app.linkedin.urls import parse_profile_url
from app.models import (
    BatchItem,
    BatchResponse,
    ErrorResponse,
    ProfileResponse,
)

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("app")

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = build_cache(settings.redis_url, settings.cache_max_entries)
    client = LinkedInClient(settings)
    state["client"] = client
    state["cache"] = cache
    state["service"] = ProfileService(settings, client, cache)
    log.info(
        "Started with %s LinkedIn session(s) and strategies %s.",
        len(client.pool.sessions),
        settings.strategies,
    )
    if not client.pool.configured:
        log.warning(
            "No LI_AT cookie is set. Every strategy needs one, so requests will "
            "fail with no_linkedin_session until you set LI_AT and JSESSIONID."
        )
    yield
    await client.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    description=(
        "Turn a LinkedIn profile URL into structured JSON.\n\n"
        "There is a console at [/](/) if you would rather paste a URL than "
        "write a request.\n\n"
        "Send `GET /api/v1/profile?url=https://www.linkedin.com/in/<name>/`.\n\n"
        "The service reads LinkedIn's own Voyager API with a logged in session "
        "cookie, and falls back through three more strategies when a route "
        "changes. Read `meta` on every response to see which strategy answered "
        "and how complete the result is."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    swagger_ui_parameters={"favicon": "/favicon.svg"},
    openapi_tags=[
        {"name": "profile", "description": "Read one profile or a small batch."},
        {"name": "ops", "description": "Health, session state and cache state."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def service() -> ProfileService:
    return state["service"]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@app.exception_handler(LinkedInAPIError)
async def handle_api_error(request: Request, exc: LinkedInAPIError) -> JSONResponse:
    headers = {}
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.status, content=exc.to_dict(), headers=headers)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class ProfileRequest(BaseModel):
    url: str = Field(
        description="A LinkedIn profile URL, or a bare public identifier.",
        examples=["https://www.linkedin.com/in/kartik-sharma-/"],
    )
    refresh: bool = Field(default=False, description="Skip the cache and refetch.")


class BatchRequest(BaseModel):
    urls: list[str] = Field(description="Profile URLs to read.", max_length=50)
    refresh: bool = False


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "The URL is not a profile URL."},
    401: {"model": ErrorResponse, "description": "The API key is missing or wrong."},
    404: {"model": ErrorResponse, "description": "LinkedIn has no such profile."},
    429: {"model": ErrorResponse, "description": "Rate limited."},
    502: {"model": ErrorResponse, "description": "Every strategy failed."},
    503: {"model": ErrorResponse, "description": "The LinkedIn session needs attention."},
}


STATIC = pathlib.Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """The console. A thin lens over the API, served from the same origin.

    Same origin matters: the page calls /api/v1/profile directly, so it needs
    no CORS handling and no second deployment.
    """
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    """Also served as a file, because /docs and /redoc cannot use a data URI."""
    return Response(
        (STATIC / "favicon.svg").read_text(encoding="utf-8"),
        media_type="image/svg+xml",
        headers={"cache-control": "public, max-age=86400"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> Response:
    """Browsers ask for this unprompted. Answer once rather than log a 404."""
    return Response(
        (STATIC / "favicon.svg").read_text(encoding="utf-8"),
        media_type="image/svg+xml",
        headers={"cache-control": "public, max-age=86400"},
    )


@app.get("/health", tags=["ops"], summary="Liveness probe")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": settings.version,
        "linkedin_session_configured": state["client"].pool.configured,
        "strategies": settings.strategies,
    }


@app.get("/api/v1/session", tags=["ops"], summary="Is the LinkedIn cookie still alive")
async def session_status(
    _: Annotated[str, Depends(require_api_key)],
) -> dict[str, Any]:
    """Call LinkedIn's /me route once and report what came back.

    Use this to tell a dead cookie apart from a broken parser.
    """
    return await state["client"].check_session()


@app.get("/api/v1/strategies", tags=["ops"], summary="List the fetch strategies")
async def strategies() -> dict[str, Any]:
    return {
        "order": settings.strategies,
        "available": {
            name: {
                "needs_auth": cls.needs_auth,
                "description": cls.description,
            }
            for name, cls in REGISTRY.items()
        },
    }


@app.get("/api/v1/cache", tags=["ops"], summary="Cache statistics")
async def cache_stats(_: Annotated[str, Depends(require_api_key)]) -> dict[str, Any]:
    return await state["cache"].stats()


@app.delete("/api/v1/cache/{public_identifier}", tags=["ops"], summary="Drop one cached profile")
async def cache_drop(
    public_identifier: str, _: Annotated[str, Depends(require_api_key)]
) -> dict[str, Any]:
    await state["cache"].delete(profile_key(public_identifier))
    return {"deleted": public_identifier}


@app.get(
    "/api/v1/profile",
    tags=["profile"],
    response_model=ProfileResponse,
    responses=_ERRORS,
    summary="Read one profile",
)
async def get_profile(
    request: Request,
    api_key: Annotated[str, Depends(require_api_key)],
    url: Annotated[
        str,
        Query(
            description="A LinkedIn profile URL, or a bare public identifier.",
            examples=["https://www.linkedin.com/in/kartik-sharma-/"],
        ),
    ],
    refresh: Annotated[bool, Query(description="Skip the cache and refetch.")] = False,
) -> ProfileResponse:
    check_rate_limit(request, api_key)
    return await service().get_profile(url, refresh=refresh)


@app.post(
    "/api/v1/profile",
    tags=["profile"],
    response_model=ProfileResponse,
    responses=_ERRORS,
    summary="Read one profile, with the URL in the body",
)
async def post_profile(
    request: Request,
    body: ProfileRequest,
    api_key: Annotated[str, Depends(require_api_key)],
) -> ProfileResponse:
    check_rate_limit(request, api_key)
    return await service().get_profile(body.url, refresh=body.refresh)


@app.post(
    "/api/v1/profiles/batch",
    tags=["profile"],
    response_model=BatchResponse,
    responses=_ERRORS,
    summary="Read several profiles",
)
async def post_batch(
    request: Request,
    body: BatchRequest,
    api_key: Annotated[str, Depends(require_api_key)],
) -> BatchResponse:
    check_rate_limit(request, api_key)

    urls = body.urls[: settings.batch_max_urls]
    semaphore = asyncio.Semaphore(settings.batch_concurrency)

    async def one(url: str) -> BatchItem:
        async with semaphore:
            try:
                result = await service().get_profile(url, refresh=body.refresh)
                return BatchItem(url=url, ok=True, profile=result.profile, meta=result.meta)
            except LinkedInAPIError as exc:
                return BatchItem(url=url, ok=False, error=exc.to_dict())

    results = await asyncio.gather(*(one(u) for u in urls))
    succeeded = sum(1 for r in results if r.ok)
    return BatchResponse(
        results=list(results),
        requested=len(urls),
        succeeded=succeeded,
        failed=len(results) - succeeded,
    )


@app.get(
    "/api/v1/diagnose",
    tags=["ops"],
    summary="Ask LinkedIn directly and report the raw answer",
)
async def diagnose(
    url: str,
    _: Annotated[str, Depends(require_api_key)],
) -> dict[str, Any]:
    """Report what LinkedIn answers for each route this service uses.

    The profile endpoint reports our own error codes, which say what we did
    about a failure but not what LinkedIn said. This reports the raw status,
    the redirect target and the first bytes of each body, so an operator can
    tell a restricted account apart from a retired route.
    """
    ref = parse_profile_url(url)
    client: LinkedInClient = state["client"]
    identifier = ref.public_identifier
    targets = [
        ("me", f"{client.voyager}/me", True),
        (
            "voyager_profile_view",
            f"{client.voyager}/identity/profiles/{identifier}/profileView",
            True,
        ),
        (
            "voyager_dash",
            f"{client.voyager}/identity/dash/profiles"
            f"?q=memberIdentity&memberIdentity={identifier}",
            True,
        ),
        ("profile_page_authenticated", client.page_url(identifier), True),
        ("profile_page_logged_out", client.page_url(identifier), False),
    ]
    probes = {}
    for name, target, authenticated in targets:
        raw = await client.probe(target, authenticated=authenticated, follow=False)
        # A raw probe reports the first hop verbatim. Following as well shows
        # whether the redirect chain terminates, and where.
        if raw.get("status") in (301, 302, 303, 307, 308):
            raw["followed"] = await client.probe(
                target, authenticated=authenticated, follow=True
            )
        probes[name] = raw
    return {"public_identifier": identifier, "probes": probes}


@app.get("/api/v1/parse", tags=["ops"], summary="Check that we can read a URL")
async def parse_only(url: str) -> dict[str, Any]:
    """Validate a URL without calling LinkedIn. Free and instant."""
    ref = parse_profile_url(url)
    return {
        "public_identifier": ref.public_identifier,
        "canonical_url": ref.canonical_url,
        "input_kind": ref.source,
    }
