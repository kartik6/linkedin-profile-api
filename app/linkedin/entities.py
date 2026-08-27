"""Index a normalized Voyager payload.

When we send `Accept: application/vnd.linkedin.normalized+json+2.1`, LinkedIn
answers with a flat entity graph:

    {
      "data":     {"*elements": ["urn:li:fsd_profile:ACoAA..."]},
      "included": [{"$type": "...profile.Profile",  "entityUrn": "urn:...", ...},
                   {"$type": "...profile.Position", "entityUrn": "urn:...", ...}]
    }

Every entity carries a `$type` and an `entityUrn`. Fields whose name starts
with `*` hold URNs that point at other entities in the same list.

This flat form is the same for the REST calls, the GraphQL calls and the JSON
embedded in the profile HTML. So one index serves all three strategies, and
one normalizer reads the index. That is why adding a strategy is cheap.

We match a type by its last segment, not by the full name. LinkedIn ships two
namespaces at once, `...voyager.identity.profile.Position` and
`...voyager.dash.identity.profile.Position`, and both mean the same thing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import Any


def type_suffix(entity: Any) -> str:
    if not isinstance(entity, dict):
        return ""
    return str(entity.get("$type", "")).rsplit(".", 1)[-1]


def collect_included(payload: Any, seen: set[int] | None = None) -> list[dict[str, Any]]:
    """Gather every `included` list found anywhere inside the payload."""
    seen = seen if seen is not None else set()
    out: list[dict[str, Any]] = []

    if isinstance(payload, dict):
        if id(payload) in seen:
            return out
        seen.add(id(payload))
        included = payload.get("included")
        if isinstance(included, list):
            out.extend(item for item in included if isinstance(item, dict))
        for key, value in payload.items():
            if key == "included":
                continue
            out.extend(collect_included(value, seen))
    elif isinstance(payload, list):
        for item in payload:
            out.extend(collect_included(item, seen))

    return out


class EntityPool:
    """A read only index over a list of Voyager entities."""

    def __init__(self, entities: list[dict[str, Any]] | None = None) -> None:
        self.by_urn: dict[str, dict[str, Any]] = {}
        self._by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._order: list[dict[str, Any]] = []
        for entity in entities or []:
            self.add(entity)

    # -- build ------------------------------------------------------------

    def add(self, entity: dict[str, Any]) -> None:
        if not isinstance(entity, dict):
            return
        urn = entity.get("entityUrn")
        if isinstance(urn, str):
            existing = self.by_urn.get(urn)
            if existing is not None:
                # A later copy usually holds more fields. Merge, do not replace.
                for key, value in entity.items():
                    if value is not None and existing.get(key) in (None, [], {}):
                        existing[key] = value
                return
            self.by_urn[urn] = entity
        suffix = type_suffix(entity)
        if suffix:
            self._by_type[suffix].append(entity)
        self._order.append(entity)

    @classmethod
    def from_payload(cls, payload: Any) -> EntityPool:
        return cls(collect_included(payload))

    def merge(self, other: EntityPool) -> EntityPool:
        for entity in other._order:
            self.add(entity)
        return self

    # -- read -------------------------------------------------------------

    def by_type(self, *suffixes: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for suffix in suffixes:
            out.extend(self._by_type.get(suffix, []))
        return out

    def first(self, *suffixes: str) -> dict[str, Any] | None:
        found = self.by_type(*suffixes)
        return found[0] if found else None

    def resolve(self, ref: Any) -> dict[str, Any] | None:
        """Follow a URN reference to the entity it points at."""
        if isinstance(ref, str):
            return self.by_urn.get(ref)
        if isinstance(ref, dict):
            return ref
        return None

    def resolve_many(self, ref: Any) -> list[dict[str, Any]]:
        if isinstance(ref, list):
            return [e for e in (self.resolve(r) for r in ref) if e]
        found = self.resolve(ref)
        return [found] if found else []

    def linked(self, entity: dict[str, Any], *field_names: str) -> dict[str, Any] | None:
        """Read a field that may be inline or a `*`-prefixed URN reference."""
        for name in field_names:
            if name in entity:
                found = self.resolve(entity[name])
                if found:
                    return found
            starred = f"*{name}"
            if starred in entity:
                found = self.resolve(entity[starred])
                if found:
                    return found
        return None

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._order)

    def type_counts(self) -> dict[str, int]:
        """Useful when a payload shape changes and you need to see what arrived."""
        return {k: len(v) for k, v in sorted(self._by_type.items())}
