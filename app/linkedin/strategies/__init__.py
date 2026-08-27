"""The strategy registry. Settings pick the order at runtime."""

from __future__ import annotations

from app.linkedin.strategies.base import Strategy, StrategyResult
from app.linkedin.strategies.embedded_json import EmbeddedJSONStrategy
from app.linkedin.strategies.public_jsonld import PublicJSONLDStrategy
from app.linkedin.strategies.voyager_dash import VoyagerDashStrategy
from app.linkedin.strategies.voyager_profile_view import VoyagerProfileViewStrategy

REGISTRY: dict[str, type[Strategy]] = {
    VoyagerProfileViewStrategy.name: VoyagerProfileViewStrategy,
    VoyagerDashStrategy.name: VoyagerDashStrategy,
    EmbeddedJSONStrategy.name: EmbeddedJSONStrategy,
    PublicJSONLDStrategy.name: PublicJSONLDStrategy,
}


def build(names: list[str]) -> list[Strategy]:
    """Turn the configured names into strategy instances, in order."""
    out: list[Strategy] = []
    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown strategy {name!r}. Known: {sorted(REGISTRY)}")
        out.append(cls())
    return out


__all__ = ["REGISTRY", "Strategy", "StrategyResult", "build"]
