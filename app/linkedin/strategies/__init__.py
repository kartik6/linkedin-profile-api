"""The strategy registry.

Only one strategy survives. Three were removed after we tested them against
LinkedIn and found them dead. The evidence is in each removal:

  voyager_profile_view  /identity/profiles/{id}/profileView returns 410 Gone.
                        410 is deliberate: the route existed and was retired.

  embedded_json         Looked for <code id="bpr-guid-*"> blocks holding a
                        Voyager payload. The current profile page contains zero
                        <code> tags and zero "included" keys. It ships a Server
                        Driven UI tree instead, which has no domain entities.

  public_jsonld         Looked for schema.org markup. The page contains zero
                        application/ld+json blocks.

One strategy that works is worth more than four that might.
"""

from __future__ import annotations

from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.strategies.voyager_dash import VoyagerDashStrategy

REGISTRY: dict[str, type[Strategy]] = {
    VoyagerDashStrategy.name: VoyagerDashStrategy,
}


def build(names: list[str]) -> list[Strategy]:
    out: list[Strategy] = []
    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown strategy {name!r}. Known: {sorted(REGISTRY)}")
        out.append(cls())
    return out


__all__ = ["REGISTRY", "Strategy", "StrategyResult", "build"]
