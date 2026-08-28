"""Build test fixtures from real captures, with the personal data removed.

Why this exists. The first version of this project shipped hand written
fixtures. They matched what we *believed* LinkedIn returns. Every test passed
and every test was meaningless, because the belief was wrong.

So fixtures now come from real captured responses. We keep the structure
exactly, byte for byte in shape, and replace only the values that identify a
person. The tests then check the parser against LinkedIn's real field names,
nesting and types, while the repository holds nobody's personal data.

Run:  python scripts/make_fixtures.py
Input:  captures/dash_query.json, captures/dash_direct.json, captures/sections.json
Output: tests/fixtures/*.json
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "captures"
FIXTURES = ROOT / "tests" / "fixtures"

FAKE_ID = "ACoAAAFAKEIDFAKEIDFAKEIDFAKEIDFAKEIDxxx"
FAKE_PUBLIC_ID = "ada-lovelace-000000000"

# Value replacements, applied by key name. The shape survives, the person does not.
REPLACE: dict[str, str] = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "publicIdentifier": FAKE_PUBLIC_ID,
    "headline": "Principal Engineer | Distributed Systems | Storage",
    "summary": "I build systems that stay up.\nCurrently focused on storage engines.",
    "a11yText": "Ada Lovelace",
    "companyName": "Analytical Engines",
    "schoolName": "Institute of Technology",
    "authority": "Certification Authority",
    "licenseNumber": "CERT-000-111",
    "title": "Principal Engineer",
    "name": "Distributed Systems",
    "fieldOfStudy": "Computer Science",
    "degreeName": "Bachelor of Technology",
    "grade": "8.9",
    "locationName": "Bengaluru, Karnataka, India",
    "geoLocationName": "Bengaluru, Karnataka, India",
    "trackingId": "AAAAAAAAAAAAAAAAAAAAAA==",
    "url": "https://example.com/credential",
    "description": "Redacted for the fixture.",
}

# Keys whose value is a locale map, for example {"en_US": "..."}.
MULTILOCALE = re.compile(r"^multiLocale(.+)$")

# These must stay distinct across entities. If every Skill were called the same
# thing, the deduplication in _pool_skills would collapse twenty into one and
# the fixture would hide a real behaviour instead of testing it.
DISTINCT = {"name", "title", "companyName", "schoolName", "authority", "licenseNumber"}

# Reference data, not personal data. "Full-time" and "Computer Software" say
# nothing about a person, and scrubbing them breaks the parsers that map these
# values onto our own enums and fields.
REFERENCE_TYPES = ("EmploymentType", "Industry", "Geo", "Country", "Locale", "Language")

# The exploratory probes used short ad hoc names. Fixtures use the real route
# segment, so a reader can map a fixture straight onto the URL that produced it.
CANONICAL_ROUTE = {
    "positions": "profilePositions",
    "educations": "profileEducations",
    "skills": "profileSkills",
    "posgroups": "profilePositionGroups",
}

REAL_ID_RE = re.compile(r"ACoAA[A-Za-z0-9_-]{10,}")
MEMBER_ID_RE = re.compile(r"urn:li:member:\d+")


def _value(key: str, counters: dict[str, int]) -> str:
    base = REPLACE[key]
    if key in DISTINCT:
        counters[key] = counters.get(key, 0) + 1
        return f"{base} {counters[key]}"
    return base


def scrub(node: Any, counters: dict[str, int] | None = None) -> Any:
    counters = {} if counters is None else counters

    if isinstance(node, dict):
        kind = str(node.get("$type", "")).rsplit(".", 1)[-1]
        if kind in REFERENCE_TYPES:
            # Keep every value, but still rewrite any identifier inside it.
            return {k: scrub(v, counters) if not isinstance(v, str) else
                    REAL_ID_RE.sub(FAKE_ID, v) for k, v in node.items()}

        # Resolve a dict's own replacements first, so `name` and
        # `multiLocaleName` in the same entity end up agreeing.
        local = {
            key: _value(key, counters)
            for key, value in node.items()
            if key in REPLACE and isinstance(value, str)
        }
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in local:
                out[key] = local[key]
                continue
            match = MULTILOCALE.match(key)
            if match:
                base = match.group(1)
                base = base[0].lower() + base[1:]
                if base in local and isinstance(value, dict):
                    out[key] = {loc: local[base] for loc in value}
                    continue
            out[key] = scrub(value, counters)
        return out
    if isinstance(node, list):
        return [scrub(item, counters) for item in node]
    if isinstance(node, str):
        node = REAL_ID_RE.sub(FAKE_ID, node)
        node = MEMBER_ID_RE.sub("urn:li:member:1000000", node)
        return node
    return node


def main() -> None:
    if not CAPTURES.exists():
        raise SystemExit(
            "No captures/ directory. Capture real responses first, see LEARNING-NOTES.md."
        )

    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = []

    for name in ("dash_query", "dash_direct", "full", "languages"):
        src = CAPTURES / f"{name}.json"
        if src.exists():
            body = scrub(json.loads(src.read_text()))
            target = "dash_full_decoration" if name == "full" else name
            (FIXTURES / f"{target}.json").write_text(json.dumps(body, indent=2))
            written.append(f"{target}.json")

    sections_src = CAPTURES / "sections.json"
    if sections_src.exists():
        raw = json.loads(sections_src.read_text())
        keep = {}
        for route, result in raw.items():
            if result.get("status") != 200:
                continue
            name = CANONICAL_ROUTE.get(route, route)
            keep[name] = scrub(json.loads(result["body"]))
        (FIXTURES / "sections.json").write_text(json.dumps(keep, indent=2))
        written.append(f"sections.json ({len(keep)} routes)")

    print(f"Wrote into {FIXTURES}:")
    for item in written:
        print(f"  {item}")

    # Guard. A fixture must never carry a real identifier.
    leaked = []
    for path in FIXTURES.glob("*.json"):
        text = path.read_text()
        for found in REAL_ID_RE.findall(text):
            if found != FAKE_ID:
                leaked.append((path.name, found))
    if leaked:
        raise SystemExit(f"Real identifiers survived scrubbing: {leaked[:5]}")
    print("Scrub check passed. No real identifiers remain.")


if __name__ == "__main__":
    main()
