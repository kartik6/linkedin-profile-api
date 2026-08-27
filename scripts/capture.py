"""Hit the real LinkedIn with your own cookie and save what comes back.

Use this to debug against production, and to refresh the test fixtures after
LinkedIn changes a payload. It writes to ./captures, which .gitignore blocks,
because a real capture holds another person's personal data.

Run:  python scripts/capture.py https://www.linkedin.com/in/<name>/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.errors import LinkedInAPIError  # noqa: E402
from app.linkedin.client import LinkedInClient  # noqa: E402
from app.linkedin.entities import EntityPool  # noqa: E402
from app.linkedin.strategies import build  # noqa: E402
from app.linkedin.strategies.embedded_json import extract_payloads  # noqa: E402
from app.linkedin.urls import parse_profile_url  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "captures"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="A LinkedIn profile URL.")
    parser.add_argument("--raw", action="store_true", help="Also save the raw HTML page.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.sessions:
        print("No LI_AT is set. Put your cookies in .env first.")
        return 1

    ref = parse_profile_url(args.url)
    OUT.mkdir(exist_ok=True)
    client = LinkedInClient(settings)

    print(f"Reading {ref.canonical_url}\n")
    try:
        session = await client.check_session()
        print(f"Session: {json.dumps(session, indent=2)}\n")

        for strategy in build(settings.strategies):
            print(f"--- {strategy.name} ---")
            try:
                result = await strategy.fetch(client, ref)
            except LinkedInAPIError as exc:
                print(f"  failed: {exc.code}: {exc.message}\n")
                continue

            profile = result.profile
            path = OUT / f"{ref.public_identifier}.{strategy.name}.json"
            path.write_text(profile.model_dump_json(indent=2))
            print(
                f"  name={profile.full_name!r} experience={len(profile.experience)} "
                f"education={len(profile.education)} skills={len(profile.skills)} "
                f"certifications={len(profile.certifications)}"
            )
            if result.raw:
                print(f"  raw shape: {json.dumps(result.raw)[:300]}")
            print(f"  saved {path}\n")

        if args.raw:
            page = await client.get_html(client.page_url(ref.public_identifier))
            (OUT / f"{ref.public_identifier}.page.html").write_text(page)
            payloads = extract_payloads(page)
            pool = EntityPool()
            for payload in payloads:
                pool.merge(EntityPool.from_payload(payload))
            print(f"Page had {len(payloads)} embedded payloads.")
            print(f"Entity types: {json.dumps(pool.type_counts(), indent=2)}")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
