"""Run one real profile fetch from this machine, and report clearly.

No server, no ports, no background processes. It calls the same service code
the deployment runs, straight from your terminal, using the cookies in .env.

The point is to isolate one variable. The deployed copy runs from a Singapore
datacenter address. This runs from your own connection. Same code, same
cookies, different network path.

    python scripts/live_test.py kamal-sharma-2a654a191
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.cache import MemoryCache  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.errors import LinkedInAPIError  # noqa: E402
from app.linkedin.client import LinkedInClient  # noqa: E402
from app.linkedin.service import ProfileService  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'-' * 62}\n{title}\n{'-' * 62}")


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "kamal-sharma-2a654a191"
    settings = get_settings()

    rule("STEP 1  Credentials")
    if not settings.sessions:
        print("  FAIL  No LI_AT found.")
        print("        Open .env, paste both values, save, and run this again.")
        return 1
    li_at, jsid = settings.sessions[0]
    print(f"  ok    LI_AT loaded, {len(li_at)} characters")
    print(f"  ok    JSESSIONID loaded: {(jsid or '')[:18]}...")
    print(f"  ok    sections to fetch: {len(settings.sections)}")
    print(
        f"  ok    outbound rate: {settings.outbound_rps}/s, "
        f"jitter {settings.outbound_jitter_ms}ms"
    )

    client = LinkedInClient(settings)
    service = ProfileService(settings, client, MemoryCache())

    try:
        rule("STEP 2  Is the LinkedIn session alive?")
        status = await client.check_session()
        if not status.get("authenticated"):
            print(f"  FAIL  {status.get('error')}: {status.get('message')}")
            print("        The cookie is already dead. Log in again and re-copy it.")
            return 1
        print(f"  ok    authenticated as: {status.get('logged_in_as')}")

        rule(f"STEP 3  Fetching {target}")
        started = time.perf_counter()
        try:
            result = await service.get_profile(target, refresh=True)
        except LinkedInAPIError as exc:
            print(f"  FAIL  {exc.code}: {exc.message}")
            if exc.code == "linkedin_session_invalid":
                print("\n  LinkedIn revoked the session during the run.")
                print("  Same behaviour as the deployment, so the trigger is not the IP.")
            return 1
        elapsed = time.perf_counter() - started

        p, m = result.profile, result.meta
        print(f"  ok    HTTP 200 in {elapsed:.1f}s")

        rule("STEP 4  What came back")
        print(f"  name          : {p.full_name}")
        print(f"  headline      : {(p.headline or '')[:60]}")
        print(f"  location      : {p.location.country_code if p.location else None}")
        sizes = [a.width for a in p.profile_picture.artifacts] if p.profile_picture else None
        print(f"  photo sizes   : {sizes}")
        print()
        for name in ("experience", "education", "skills", "certifications", "languages"):
            items = getattr(p, name)
            mark = "ok  " if items else "none"
            print(f"  {mark}  {name:15}: {len(items)}")
        print()
        print(f"  completeness  : {m.completeness}   partial={m.partial}")
        for warning in m.warnings[:4]:
            print(f"  warning       : {warning[:90]}")

        rule("VERDICT")
        failed = [w for w in m.warnings if "failed" in w]
        if not failed and p.experience and p.education:
            print("  PASS  Every section came back. The code works from this network.")
            print("        The deployment failing means the datacenter address is the trigger.")
        elif p.experience and failed:
            print("  PARTIAL  Some sections failed, same as the deployment.")
            print("           So the call pattern, not the IP, is what LinkedIn objects to.")
        else:
            print("  See the warnings above.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
