"""Read a text value out of the many wrappers LinkedIn uses."""

from __future__ import annotations

from typing import Any

_PREFERRED_LOCALES = ("en_US", "en_GB", "en")


def text_of(node: Any, *keys: str) -> str | None:
    """Return clean text from a string, a TextViewModel or a locale map.

    LinkedIn writes the same idea in at least four shapes:
        "Engineer"
        {"text": "Engineer"}
        {"text": {"text": "Engineer"}}
        {"en_US": "Engineer", "de_DE": "Ingenieur"}
    """
    if keys:
        if not isinstance(node, dict):
            return None
        for key in keys:
            found = text_of(node.get(key))
            if found:
                return found
        return None

    if node is None:
        return None
    if isinstance(node, str):
        cleaned = node.strip()
        return cleaned or None
    if isinstance(node, int | float):
        return str(node)
    if isinstance(node, list):
        for item in node:
            found = text_of(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        for key in ("text", "value", "localized", "string"):
            if key in node:
                found = text_of(node[key])
                if found:
                    return found
        for locale in _PREFERRED_LOCALES:
            if locale in node:
                found = text_of(node[locale])
                if found:
                    return found
        # A locale map with no English entry. Take the first value.
        if node and all(isinstance(k, str) and "_" in k for k in node):
            return text_of(next(iter(node.values())))
    return None


def first_text(node: Any, *key_groups: str) -> str | None:
    """Try each key in turn on the same node and return the first hit."""
    for key in key_groups:
        found = text_of(node, key)
        if found:
            return found
    return None
