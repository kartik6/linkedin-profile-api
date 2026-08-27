"""Strategy 3: read the JSON that LinkedIn ships inside the profile page.

This is the most durable path we have, and it is the interesting one.

LinkedIn server renders the profile page. To avoid a second round trip, it
inlines the exact Voyager responses the page needs, inside hidden elements:

    <code style="display:none" id="bpr-guid-3921884">
      {"data":{...},"included":[{"$type":"...Position", ...}, ...]}
    </code>

So the page carries the same entity graph the REST and GraphQL routes return.
We fetch one HTML document with the session cookie, pull every <code> block,
keep the ones that parse as JSON with an `included` list, and merge them into
one pool. The normalizer then treats it like any other Voyager payload.

Why this survives changes that break the other strategies:

  - No decoration id. LinkedIn retires those.
  - No GraphQL queryId. LinkedIn rotates those on every web release.
  - No fixed route. The page URL is the public profile URL itself.

It costs one larger response, so it is not first in the order. It is the one
that keeps working when the others stop.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from app.errors import LinkedInAPIError, ProfileNotFound
from app.linkedin.client import LinkedInClient
from app.linkedin.entities import EntityPool
from app.linkedin.normalize import from_entity_pool
from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.urls import ProfileRef

log = logging.getLogger(__name__)

_CODE_BLOCK_RE = re.compile(
    r"<code[^>]*>\s*(\{.*?\}|\[.*?\])\s*</code>", re.DOTALL | re.IGNORECASE
)
_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>\s*(\{.*?\})\s*</script>',
    re.DOTALL | re.IGNORECASE,
)
_AUTHWALL_MARKERS = ("authwall", "join now to view", "sign in to view", "checkpoint/challenge")


def extract_payloads(page_html: str) -> list[dict[str, Any]]:
    """Return every embedded JSON object that carries an entity list."""
    payloads: list[dict[str, Any]] = []

    for pattern in (_CODE_BLOCK_RE, _JSON_SCRIPT_RE):
        for match in pattern.finditer(page_html):
            raw = match.group(1)
            parsed = _loads(raw)
            if isinstance(parsed, dict) and (
                isinstance(parsed.get("included"), list) or "data" in parsed
            ):
                payloads.append(parsed)

    return payloads


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(html.unescape(raw))
    except json.JSONDecodeError:
        return None


class EmbeddedJSONStrategy(Strategy):
    name = "embedded_json"
    needs_auth = True
    description = "Fetch the profile page and read the Voyager JSON embedded in it."

    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        page = await client.get_html(
            client.page_url(ref.public_identifier),
            referer=f"{client.base_url}/feed/",
        )

        lowered = page[:6000].lower()
        if any(marker in lowered for marker in _AUTHWALL_MARKERS):
            raise LinkedInAPIError(
                "The profile page rendered a sign in wall instead of the profile."
            )

        payloads = extract_payloads(page)
        if not payloads:
            raise LinkedInAPIError("The profile page carried no embedded Voyager JSON.")

        pool = EntityPool()
        for payload in payloads:
            pool.merge(EntityPool.from_payload(payload))

        if not pool.by_type("Profile", "MiniProfile"):
            raise ProfileNotFound("The embedded JSON held no profile entity.")

        profile = from_entity_pool(pool, ref)
        return StrategyResult(
            name=self.name,
            profile=profile,
            raw={"payloads": len(payloads), "entity_types": pool.type_counts()},
        )
