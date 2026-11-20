"""Small web server that serves the site and hooks shorten.html up to urlshort.

Run:  python3 server.py [port]      (default port 8000)

Routes
  GET  /, /index.html, /shorten.html   the static pages
  POST /api/shorten   body {"url": "..."} -> {"code": "...", "short_url": "..."}
  GET  /<code>        302 redirect to the original URL
"""

import json
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "urlshort" / "src"))
from urlshort import CODE_LENGTH, Shortener  # noqa: E402

PAGES = {"/": "index.html", "/index.html": "index.html", "/shorten.html": "shorten.html"}
CODE_RE = re.compile(rf"^/([0-9A-Za-z]{{{CODE_LENGTH}}})$")
MAX_BODY = 8 * 1024

shortener = Shortener()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in PAGES:
            body = (ROOT / PAGES[path]).read_bytes()
            return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
        m = CODE_RE.match(path)
        if m:
            try:
                url = shortener.expand(m.group(1))
            except KeyError:
                return self._send(HTTPStatus.NOT_FOUND, b"Unknown short link\n")
            return self._send(HTTPStatus.FOUND, headers={"Location": url})
        self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

    do_HEAD = do_GET

    def do_POST(self):
        if self.path != "/api/shorten":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Request body missing or too large"})
        try:
            url = json.loads(self.rfile.read(length))["url"]
            code = shortener.shorten(url)
        except (ValueError, KeyError, TypeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Enter a full URL starting with http:// or https://"})
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
        self._json(HTTPStatus.OK, {"code": code, "short_url": f"http://{host}/{code}"})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving on http://localhost:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
