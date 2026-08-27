"""Rebuild usable image URLs from a LinkedIn VectorImage.

LinkedIn never sends a plain image URL. It sends a root URL plus one path
segment per size:

    {
      "rootUrl": "https://media.licdn.com/dms/image/v2/D5603AQ.../profile-displayphoto-shrink_",
      "artifacts": [
        {"width": 100, "height": 100,
         "fileIdentifyingUrlPathSegment": "100_100/0/1699?e=1735...&v=beta&t=abc"}
      ]
    }

A fetchable URL is rootUrl + fileIdentifyingUrlPathSegment. The segment also
carries a signature and an expiry, so the URL stops working after some hours.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.models import Image, ImageArtifact

# Places a VectorImage hides inside the many payload shapes LinkedIn uses.
_VECTOR_PATHS: tuple[tuple[str, ...], ...] = (
    (),
    ("com.linkedin.common.VectorImage",),
    ("vectorImage",),
    ("displayImageReference", "vectorImage"),
    ("displayImageReferenceResolutionResult", "vectorImage"),
    ("image", "com.linkedin.common.VectorImage"),
    ("image", "vectorImage"),
    ("picture", "com.linkedin.common.VectorImage"),
    ("profilePicture", "displayImageReference", "vectorImage"),
    ("backgroundImage", "displayImageReference", "vectorImage"),
    ("originalImage", "vectorImage"),
    ("logo", "vectorImage"),
    ("logoResolutionResult", "vectorImage"),
    ("logo", "image", "com.linkedin.common.VectorImage"),
)


def _walk(node: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _looks_like_vector(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("rootUrl"), str)
        and isinstance(node.get("artifacts"), list)
    )


def _expiry_from(url: str) -> datetime | None:
    """Read the `e=` query parameter. It holds a unix timestamp in seconds."""
    try:
        raw = parse_qs(urlparse(url).query).get("e", [None])[0]
        if raw:
            return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OSError, TypeError):
        pass
    return None


def parse_vector_image(node: Any) -> Image | None:
    """Find a VectorImage anywhere in `node` and turn it into an Image."""
    if not isinstance(node, dict):
        return None

    vector = None
    for path in _VECTOR_PATHS:
        found = _walk(node, path)
        if _looks_like_vector(found):
            vector = found
            break

    if vector is None:
        # Last resort: search one level deep for any nested VectorImage.
        for value in node.values():
            if _looks_like_vector(value):
                vector = value
                break
    if vector is None:
        return None

    root = vector["rootUrl"]
    artifacts: list[ImageArtifact] = []
    for art in vector["artifacts"]:
        if not isinstance(art, dict):
            continue
        segment = art.get("fileIdentifyingUrlPathSegment")
        if not segment:
            continue
        artifacts.append(
            ImageArtifact(
                width=art.get("width"),
                height=art.get("height"),
                url=f"{root}{segment}",
            )
        )

    if not artifacts:
        return None

    artifacts.sort(key=lambda a: (a.width or 0) * (a.height or 0))
    largest = artifacts[-1]
    return Image(
        url=largest.url,
        artifacts=artifacts,
        expires_at=_expiry_from(largest.url),
    )


def parse_plain_image_url(value: Any) -> Image | None:
    """Handle the few places where LinkedIn sends a ready made URL string."""
    if isinstance(value, str) and value.startswith("http"):
        return Image(
            url=value,
            artifacts=[ImageArtifact(url=value)],
            expires_at=_expiry_from(value),
        )
    return None


def extract_image(node: Any) -> Image | None:
    """Try both image shapes and return whichever one works."""
    return parse_vector_image(node) or parse_plain_image_url(node)
