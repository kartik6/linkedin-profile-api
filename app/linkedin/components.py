"""Read the rendered card tree that the LinkedIn web app uses today.

Newer profile responses do not always ship typed Position or Education
entities. They ship a display tree instead, which LinkedIn calls tetris:

    Card -> components -> entityComponent
              titleV2   -> "Senior Engineer"
              subtitle  -> "Acme - Full-time"
              caption   -> "May 2021 - Present - 3 yrs"
              metadata  -> "Bengaluru, India"
              subComponents -> nested roles and the description

This module reads that tree. It is a fallback. Typed entities are better,
because the display tree merges several facts into one string, and those
strings follow the language of our own login session.
"""

from __future__ import annotations

import re
from typing import Any

from app.linkedin.text import text_of

_SEPARATOR = re.compile(r"\s+[·|]\s+|\s+-\s+|\s+—\s+")

# Section names LinkedIn uses in the card URN, for example
# "urn:li:fsd_profileCard:(ACoAA...,EXPERIENCE,en_US)".
_SECTION_RE = re.compile(r",([A-Z_]+),")


def card_section(card: dict[str, Any]) -> str | None:
    urn = card.get("entityUrn")
    if not isinstance(urn, str):
        return None
    match = _SECTION_RE.search(urn)
    return match.group(1).lower() if match else None


def split_parts(value: str | None) -> list[str]:
    """Break 'Acme - Full-time' into ['Acme', 'Full-time']."""
    if not value:
        return []
    return [p.strip() for p in _SEPARATOR.split(value) if p.strip()]


def walk_entities(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Collect every entityComponent under a node, deepest last."""
    found: list[dict[str, Any]] = []
    if depth > 12:
        return found
    if isinstance(node, dict):
        entity = node.get("entityComponent")
        if isinstance(entity, dict):
            found.append(entity)
        for key, value in node.items():
            if key == "entityComponent":
                continue
            found.extend(walk_entities(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(walk_entities(item, depth + 1))
    return found


def read_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Pull the four display strings plus any nested description."""
    description_parts: list[str] = []
    sub = entity.get("subComponents")
    if sub:
        for fixed in _fixed_list_texts(sub):
            description_parts.append(fixed)

    return {
        "title": text_of(entity.get("titleV2")) or text_of(entity.get("title")),
        "subtitle": text_of(entity.get("subtitle")),
        "caption": text_of(entity.get("caption")),
        "metadata": text_of(entity.get("metadata")),
        "description": "\n".join(description_parts) or None,
        "image": entity.get("image"),
        "sub_entities": walk_entities(sub) if sub else [],
    }


def _fixed_list_texts(node: Any, depth: int = 0) -> list[str]:
    """Collect the free text blocks LinkedIn stores under fixedListComponent."""
    out: list[str] = []
    if depth > 10:
        return out
    if isinstance(node, dict):
        text_component = node.get("textComponent")
        if isinstance(text_component, dict):
            value = text_of(text_component.get("text"))
            if value:
                out.append(value)
        for key, value in node.items():
            if key == "textComponent":
                continue
            out.extend(_fixed_list_texts(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            out.extend(_fixed_list_texts(item, depth + 1))
    return out
