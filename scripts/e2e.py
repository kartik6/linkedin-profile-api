"""Run the whole service against the mock LinkedIn and check the fallback chain.

This proves more than the unit tests do. It starts the real ASGI app, the real
HTTP client and the real strategies. Only LinkedIn itself is replaced.

Run:  python scripts/e2e.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else sys.executable
MOCK_PORT = 9101
API_PORT = 8099

# mode -> what the service must do. None means the request must fail.
EXPECTATIONS = {
    # Everything healthy.
    "all": ("voyager_dash", {"experience": 11, "skills": 20, "certifications": 12}),
    # LinkedIn demands its routing cookie first. The client must complete the
    # handshake and still return a full profile. This is the failure that broke
    # the first live deployment.
    "handshake": ("voyager_dash", {"experience": 11, "skills": 20, "certifications": 12}),
    # Sections all fail. The top card must still come back, marked partial.
    "thin": ("voyager_dash", {"experience": 10, "skills": 20, "certifications": 12}),
    # The decoration id is retired. One call per section instead, same result.
    "no-decoration": ("voyager_dash", {"experience": 11, "skills": 20, "certifications": 12}),
    # A stale cookie must be named as such, not reported as a missing profile.
    "dead": ("ERROR:linkedin_session_invalid", {}),
    # A bot check must be named as such.
    "challenge": ("ERROR:linkedin_challenge_required", {}),
}


def wait_for(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


def run_mode(mode: str) -> bool:
    mock = subprocess.Popen(
        [PYTHON, str(ROOT / "scripts" / "mock_linkedin.py"),
         "--port", str(MOCK_PORT), "--mode", mode],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    env = {
        **os.environ,
        "LINKEDIN_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
        "LI_AT": "fake-cookie",
        "JSESSIONID": "ajax:1234567890123456789",
        "OUTBOUND_RPS": "0",
        "OUTBOUND_JITTER_MS": "0",
        "RATE_LIMIT_PER_MINUTE": "0",
        "API_KEYS": "",
    }
    api = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app",
         "--port", str(API_PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        wait_for(f"http://127.0.0.1:{API_PORT}/health")
        import json

        url = (
            f"http://127.0.0.1:{API_PORT}/api/v1/profile"
            "?url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fadalovelace%2F&refresh=true"
        )
        expected_strategy, expected_counts = EXPECTATIONS[mode]
        problems = []

        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                body = json.load(response)
            status = 200
        except urllib.error.HTTPError as exc:
            body = json.load(exc)
            status = exc.code

        if expected_strategy.startswith("ERROR:"):
            wanted = expected_strategy.split(":", 1)[1]
            got = body.get("error")
            if got != wanted:
                problems.append(f"error was {got!r}, wanted {wanted!r}")
            print(
                f"  {'FAIL' if problems else 'ok  '} {mode:<11} http={status} "
                f"error={body.get('error')}"
            )
            for problem in problems:
                print(f"       - {problem}")
            return not problems

        profile, meta = body["profile"], body["meta"]
        if meta["strategy"] != expected_strategy:
            problems.append(f"strategy was {meta['strategy']}, wanted {expected_strategy}")
        if profile["full_name"] != "Ada Lovelace":
            problems.append(f"name was {profile['full_name']!r}")
        for section, count in expected_counts.items():
            if len(profile[section]) != count:
                problems.append(f"{section} had {len(profile[section])}, wanted {count}")

        mark = "FAIL" if problems else "ok  "
        print(
            f"  {mark} {mode:<11} strategy={meta['strategy']:<13}"
            f" complete={meta['completeness']:<5} experience={len(profile['experience']):<3}"
            f" skills={len(profile['skills']):<3} certs={len(profile['certifications'])}"
        )
        for problem in problems:
            print(f"       - {problem}")
        return not problems
    finally:
        for process in (api, mock):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    print("End to end, against the mock LinkedIn:")
    results = [run_mode(mode) for mode in EXPECTATIONS]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} modes passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
