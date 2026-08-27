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

# mode -> the strategy that must answer, and the sections it must return.
EXPECTATIONS = {
    "all": ("voyager_profile_view", {"experience": 2, "skills": 4}),
    "no-legacy": ("voyager_dash", {"experience": 2, "skills": 2}),
    "voyager-down": ("embedded_json", {"experience": 2, "skills": 2}),
    "logged-out": ("public_jsonld", {"experience": 1, "skills": 0}),
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
        with urllib.request.urlopen(url, timeout=20) as response:
            body = json.load(response)

        expected_strategy, expected_counts = EXPECTATIONS[mode]
        profile, meta = body["profile"], body["meta"]
        problems = []

        if meta["strategy"] != expected_strategy:
            problems.append(f"strategy was {meta['strategy']}, wanted {expected_strategy}")
        if profile["full_name"] != "Ada Lovelace":
            problems.append(f"name was {profile['full_name']!r}")
        for section, count in expected_counts.items():
            if len(profile[section]) != count:
                problems.append(f"{section} had {len(profile[section])}, wanted {count}")

        mark = "FAIL" if problems else "ok  "
        print(
            f"  {mark} {mode:<14} strategy={meta['strategy']:<22}"
            f" complete={meta['completeness']:<5} experience={len(profile['experience'])}"
            f" skills={len(profile['skills'])}"
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
