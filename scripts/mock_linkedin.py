"""A stand in for LinkedIn, for local end to end runs.

It serves the same routes Voyager serves, from the test fixtures. Point the
service at it and every layer runs for real except the network hop to
LinkedIn itself:

    python scripts/mock_linkedin.py &
    LINKEDIN_BASE_URL=http://127.0.0.1:9100 LI_AT=x JSESSIONID=ajax:1 \
      uvicorn app.main:app --port 8080

Use `--mode` to rehearse a failure:
    all       every route works
    no-legacy the old profileView route is gone, so dash answers
    voyager-down  both API routes fail, so the page parser answers
    logged-out    only the public page answers
"""

from __future__ import annotations

import argparse
import json
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MODE = "all"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  mock-linkedin  {self.command} {self.path.split('?')[0]} -> {args[1]}")

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        path = self.path.split("?")[0]
        authed = "li_at=" in self.headers.get("cookie", "")

        if path.endswith("/profileView"):
            if MODE in ("no-legacy", "voyager-down", "logged-out"):
                return self._send(403, '{"message":"retired"}', "application/json")
            return self._send(
                200, (FIXTURES / "profile_view.json").read_text(), "application/json"
            )

        if path == "/voyager/api/identity/dash/profiles":
            if MODE in ("voyager-down", "logged-out"):
                return self._send(403, '{"message":"blocked"}', "application/json")
            return self._send(
                200,
                (FIXTURES / "dash_profile.json").read_text(),
                "application/vnd.linkedin.normalized+json+2.1",
            )

        if path == "/voyager/api/me":
            return self._send(
                200,
                json.dumps({"included": [{"entityUrn": "u", "publicIdentifier": "mockuser"}],
                            "data": {"miniProfile": "u"}}),
                "application/json",
            )

        if path.startswith("/in/"):
            if authed and MODE != "logged-out":
                return self._send(
                    200, (FIXTURES / "profile_page.html").read_text(), "text/html"
                )
            return self._send(200, (FIXTURES / "public_page.html").read_text(), "text/html")

        self._send(404, '{"message":"not found"}', "application/json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "no-legacy", "voyager-down", "logged-out"],
    )
    args = parser.parse_args()
    MODE = args.mode
    print(f"Mock LinkedIn on http://127.0.0.1:{args.port} in mode {args.mode}")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
