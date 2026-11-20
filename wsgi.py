"""WSGI app for the site and the URL shortener.

Production:  gunicorn wsgi:app          (settings come from gunicorn.conf.py)
Local:       python3 server.py [port]

Routes
  GET  /, /index.html, /shorten.html   the static pages
  POST /api/shorten   body {"url": "..."} -> {"code": "...", "short_url": "..."}
  GET  /<code>        302 redirect to the original URL
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "urlshort" / "src"))
from urlshort import CODE_LENGTH, Shortener  # noqa: E402

PAGES = {"/": "index.html", "/index.html": "index.html", "/shorten.html": "shorten.html"}
CODE_RE = re.compile(rf"^/([0-9A-Za-z]{{{CODE_LENGTH}}})$")
MAX_BODY = 8 * 1024
BAD_URL = "Enter a full URL starting with http:// or https://"

# In-memory store: run a single worker process so every request sees the same links.
shortener = Shortener()


def _respond(start_response, status, body=b"", content_type="text/plain; charset=utf-8", headers=()):
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body))), *headers])
    return [body]


def _json(start_response, status, data):
    return _respond(start_response, status, json.dumps(data).encode(), "application/json")


def _shorten(environ, start_response):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = -1
    if not 0 < length <= MAX_BODY:
        return _json(start_response, "400 Bad Request", {"error": "Request body missing or too large"})
    try:
        url = json.loads(environ["wsgi.input"].read(length))["url"]
        code = shortener.shorten(url)
    except (ValueError, KeyError, TypeError):
        return _json(start_response, "400 Bad Request", {"error": BAD_URL})
    host = environ.get("HTTP_HOST") or f"{environ['SERVER_NAME']}:{environ['SERVER_PORT']}"
    short_url = f"{environ['wsgi.url_scheme']}://{host}/{code}"
    return _json(start_response, "200 OK", {"code": code, "short_url": short_url})


def app(environ, start_response):
    method = environ["REQUEST_METHOD"]
    path = environ.get("PATH_INFO") or "/"

    if path == "/api/shorten":
        if method != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "Use POST"})
        return _shorten(environ, start_response)

    if method not in ("GET", "HEAD"):
        return _respond(start_response, "405 Method Not Allowed", b"Method not allowed\n")

    if path in PAGES:
        body = (ROOT / PAGES[path]).read_bytes()
        return _respond(start_response, "200 OK", body, "text/html; charset=utf-8")

    m = CODE_RE.match(path)
    if m:
        try:
            url = shortener.expand(m.group(1))
        except KeyError:
            return _respond(start_response, "404 Not Found", b"Unknown short link\n")
        return _respond(start_response, "302 Found", headers=[("Location", url)])

    return _respond(start_response, "404 Not Found", b"Not found\n")


application = app  # the name gunicorn and other WSGI servers look for by default
