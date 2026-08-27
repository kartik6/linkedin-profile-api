"""A stand in for LinkedIn, built from the real captured fixtures.

It serves the routes we verified by hand, and it can reproduce the failures we
actually hit in production. Point the service at it and every layer runs for
real except the network hop to LinkedIn:

    python scripts/mock_linkedin.py --mode all &
    LINKEDIN_BASE_URL=http://127.0.0.1:9100 LI_AT=x JSESSIONID=ajax:1 \
      uvicorn app.main:app --port 8080

Modes:
    all         every route answers 200
    handshake   the first call to each route answers 302 to itself, with a
                Set-Cookie, exactly as LinkedIn does when `lidc` is missing.
                This is the failure that broke the first deployment.
    dead        401 everywhere, as a stale li_at behaves
    challenge   302 to /checkpoint/challenge, as a bot check behaves
    thin        the top card answers, every section fails with 500
"""

from __future__ import annotations

import argparse
import json
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MODE = "all"
SEEN: set[str] = set()


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  mock-linkedin  {self.path.split('?')[0]} -> {args[1]}")

    def _send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if MODE == "dead":
            return self._send(401, '{"status":401}')

        if MODE == "challenge":
            return self._send(
                302, "", {"location": "https://www.linkedin.com/checkpoint/challenge/x"}
            )

        # Reproduce LinkedIn's routing cookie handshake: 302 to the same URL
        # with a Set-Cookie, once per route, then answer normally.
        if MODE == "handshake" and path not in SEEN:
            SEEN.add(path)
            return self._send(
                302,
                "",
                {
                    "location": f"http://{self.headers.get('host')}{self.path}",
                    "set-cookie": "lidc=b=OB1:s=O:r=O; Path=/; Domain=localhost",
                },
            )

        if path == "/voyager/api/identity/dash/profiles":
            if query.get("q", [""])[0] != "memberIdentity":
                return self._send(400, '{"status":400}')
            return self._send(200, json.dumps(load("dash_query.json")))

        if path.startswith("/voyager/api/identity/dash/profile"):
            route = path.rsplit("/", 1)[-1]
            if MODE == "thin":
                return self._send(500, '{"status":500}')
            if query.get("q", [""])[0] != "viewee":
                return self._send(400, '{"status":400}')
            sections = load("sections.json")
            body = sections.get(route, {"data": {}, "included": []})
            return self._send(200, json.dumps(body))

        if path == "/voyager/api/me":
            return self._send(200, json.dumps(load("dash_query.json")))

        # The retired route, answering the way LinkedIn really answers it.
        if path.endswith("/profileView"):
            return self._send(410, '{"data":{"status":410},"included":[]}')

        self._send(404, '{"status":404}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument(
        "--mode", default="all",
        choices=["all", "handshake", "dead", "challenge", "thin"],
    )
    args = parser.parse_args()
    MODE = args.mode
    print(f"Mock LinkedIn on http://127.0.0.1:{args.port} in mode {args.mode}")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
