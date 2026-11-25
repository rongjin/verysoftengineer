"""WSGI app for the site and the URL shortener.

Production:  gunicorn wsgi:app          (settings come from gunicorn.conf.py)
Local:       python3 server.py [port]

Routes
  GET  /, /index.html, /shorten.html, /tickets.html   the static pages
  POST /api/shorten   body {"url": "..."} -> {"code": "...", "short_url": "..."}
  POST /api/expand    body {"short_url": "..."} (full short link or bare code) -> {"code": "...", "url": "..."}
  GET  /<code>        302 redirect to the original URL

Ticket booking demo (ticketMaster/tickets.html; buyer is a random id the page makes up)
  GET  /api/tickets/seats?buyer=...   the seat map
  POST /api/tickets/hold      {"buyer", "seat"}     hold a seat for HOLD_SECONDS
  POST /api/tickets/release   {"buyer", "seat"}
  POST /api/tickets/buy       {"buyer", "seats": [...]}   all or nothing
  POST /api/tickets/reset     make every seat available again
  POST /api/tickets/simulate  {"safe": true|false}  streams NDJSON progress, then the result
"""

import json
import logging
import re
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "urlshort" / "src"))
sys.path.insert(0, str(ROOT / "urlshort"))
sys.path.insert(0, str(ROOT / "ticketMaster"))
from urlshort import CODE_LENGTH, Shortener  # noqa: E402

import tickets  # noqa: E402

PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/shorten.html": "urlshort/shorten.html",
    "/tickets.html": "ticketMaster/tickets.html",
}
CODE_RE = re.compile(rf"^/([0-9A-Za-z]{{{CODE_LENGTH}}})$")
MAX_BODY = 8 * 1024
BAD_URL = "Enter a full URL starting with http:// or https://"
BAD_SHORT = f"Enter a short link, or its {CODE_LENGTH}-character code"
BARE_CODE_RE = re.compile(rf"^[0-9A-Za-z]{{{CODE_LENGTH}}}$")
log = logging.getLogger("wsgi")

from settings import DATABASE_URL  # noqa: E402  (also loads .env)

if DATABASE_URL:
    # Links are stored in Postgres, so they survive restarts and are shared by all workers.
    from db import PostgresShortener

    from tickets_db import PostgresSeatStore

    shortener = PostgresShortener(DATABASE_URL)
    seat_store = PostgresSeatStore(shortener.pool, DATABASE_URL)
else:
    # In-memory store: run a single worker so every request sees the same links and seats.
    shortener = Shortener()
    seat_store = tickets.MemorySeatStore()
seat_store.create_event(tickets.MAIN_EVENT)
_sim_slot = threading.Semaphore(1)  # one 1000-buyer run at a time per process


def _respond(start_response, status, body=b"", content_type="text/plain; charset=utf-8", headers=()):
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body))), *headers])
    return [body]


def _json(start_response, status, data):
    return _respond(start_response, status, json.dumps(data).encode(), "application/json")


def _read_json(environ):
    """Return the parsed JSON body, or None if it is missing or too large."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None
    if not 0 < length <= MAX_BODY:
        return None
    return json.loads(environ["wsgi.input"].read(length))


def _code_from(short):
    """Pull the code out of a short link like https://host/AbC123xYz9, or accept a bare code."""
    short = short.strip()
    if "/" in short:
        if "://" not in short:
            short = "http://" + short  # allow "host/code" without a scheme
        short = urlsplit(short).path.strip("/")
    if not BARE_CODE_RE.match(short):
        raise ValueError(short)
    return short


def _expand(environ, start_response):
    try:
        body = _read_json(environ)
        if body is None:
            return _json(start_response, "400 Bad Request", {"error": "Request body missing or too large"})
        code = _code_from(body["short_url"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return _json(start_response, "400 Bad Request", {"error": BAD_SHORT})
    try:
        url = shortener.expand(code)
    except KeyError:
        return _json(start_response, "404 Not Found", {"error": "No link found for that short URL"})
    except Exception:
        log.exception("could not look up short link")
        return _json(start_response, "503 Service Unavailable", {"error": "Could not look up the link. Try again shortly."})
    return _json(start_response, "200 OK", {"code": code, "url": url})


def _shorten(environ, start_response):
    try:
        body = _read_json(environ)
        if body is None:
            return _json(start_response, "400 Bad Request", {"error": "Request body missing or too large"})
        url = body["url"]
        code = shortener.shorten(url)
    except (ValueError, KeyError, TypeError):
        return _json(start_response, "400 Bad Request", {"error": BAD_URL})
    except Exception:
        log.exception("could not save short link")
        return _json(start_response, "503 Service Unavailable", {"error": "Could not save the link. Try again shortly."})
    host = environ.get("HTTP_HOST") or f"{environ['SERVER_NAME']}:{environ['SERVER_PORT']}"
    short_url = f"{environ['wsgi.url_scheme']}://{host}/{code}"
    return _json(start_response, "200 OK", {"code": code, "short_url": short_url})


BUYER_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
HOLD_REFUSED = {
    "sold": "That seat is already sold.",
    "held": "Someone else is holding that seat. If they don't buy it, it comes back when their hold runs out.",
    "mine": "You're already holding that seat.",
    "limit": f"You can hold up to {tickets.MAX_HOLDS} seats at a time.",
    "unknown": "No such seat.",
}


def _seat_map(buyer):
    out = []
    for seat, status, holder, left in seat_store.seats(tickets.MAIN_EVENT):
        item = {"seat": seat, "status": status}
        if status == "held":
            item["mine"] = holder == buyer
            if item["mine"]:
                item["expires_in"] = round(left, 2)
        out.append(item)
    return {"seats": out, "hold_seconds": tickets.HOLD_SECONDS, "max_holds": tickets.MAX_HOLDS,
            "store": seat_store.kind}


def _ticket_body(environ, want_seat=True):
    """Return (buyer, seat-or-seats) from the request body, or raise ValueError with a message."""
    try:
        body = _read_json(environ)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ValueError("Request body missing or not JSON")
    buyer = body.get("buyer")
    if not isinstance(buyer, str) or not BUYER_RE.match(buyer):
        raise ValueError("Missing buyer id")
    if want_seat:
        seat = body.get("seat")
        if seat not in tickets.SEAT_SET:
            raise ValueError("No such seat")
        return buyer, seat
    seats = body.get("seats")
    if not isinstance(seats, list) or not seats or len(seats) > tickets.MAX_HOLDS or \
            not all(x in tickets.SEAT_SET for x in seats):
        raise ValueError("Pick one or more seats to buy")
    return buyer, list(dict.fromkeys(seats))


def _tickets(environ, start_response, action):
    try:
        if action == "seats":
            buyer = (parse_qs(environ.get("QUERY_STRING", "")).get("buyer") or [""])[0]
            return _json(start_response, "200 OK", _seat_map(buyer))
        if action == "reset":
            seat_store.reset(tickets.MAIN_EVENT)
            return _json(start_response, "200 OK", _seat_map(""))
        try:
            buyer, seat = _ticket_body(environ, want_seat=action != "buy")
        except ValueError as e:
            return _json(start_response, "400 Bad Request", {"error": str(e)})
        if action == "hold":
            try:
                left = seat_store.hold(tickets.MAIN_EVENT, seat, buyer, tickets.HOLD_SECONDS, tickets.MAX_HOLDS)
            except tickets.SeatUnavailable as e:
                return _json(start_response, "409 Conflict", {"error": HOLD_REFUSED[e.reason], "reason": e.reason})
            return _json(start_response, "200 OK", {"seat": seat, "expires_in": round(left, 2)})
        if action == "release":
            seat_store.release(tickets.MAIN_EVENT, seat, buyer)
            return _json(start_response, "200 OK", {"seat": seat})
        try:
            seat_store.buy(tickets.MAIN_EVENT, seat, buyer)
        except tickets.HoldLost as e:
            return _json(start_response, "409 Conflict", {
                "error": f"Your hold on {', '.join(e.seats)} ran out, so nothing was charged. Pick again.",
                "seats": e.seats})
        return _json(start_response, "200 OK", {"seats": seat})
    except Exception:
        log.exception("ticket request failed: %s", action)
        return _json(start_response, "503 Service Unavailable", {"error": "Could not reach the seat database. Try again shortly."})


def _simulate(environ, start_response):
    try:
        body = _read_json(environ) or {}
    except ValueError:
        body = {}
    safe = not (isinstance(body, dict) and body.get("safe") is False)
    if not _sim_slot.acquire(blocking=False):
        return _json(start_response, "429 Too Many Requests", {"error": "A simulation is already running. Try again in a few seconds."})
    try:
        rush = tickets.Rush(seat_store, buyers=1000, safe=safe, on_done=_sim_slot.release).start()
    except Exception:
        _sim_slot.release()
        raise
    start_response("200 OK", [("Content-Type", "application/x-ndjson"), ("Cache-Control", "no-store"),
                              ("X-Accel-Buffering", "no")])

    def stream():
        line = lambda d: (json.dumps(d) + "\n").encode()  # noqa: E731
        yield line({"type": "start", "store": seat_store.kind, "buyers": rush.buyers, "safe": safe})
        while not rush.done.wait(0.15):
            yield line({"type": "progress", **rush.progress()})
        if rush.error is not None:
            log.error("simulation failed", exc_info=rush.error)
            yield line({"type": "error", "error": "The simulation could not reach the seat database. Try again shortly."})
        else:
            yield line({"type": "result", **rush.progress(), "result": rush.result})

    return stream()


def app(environ, start_response):
    method = environ["REQUEST_METHOD"]
    path = environ.get("PATH_INFO") or "/"

    if path == "/api/shorten":
        if method != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "Use POST"})
        return _shorten(environ, start_response)

    if path == "/api/expand":
        if method != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "Use POST"})
        return _expand(environ, start_response)

    if path.startswith("/api/tickets/"):
        action = path[len("/api/tickets/"):]
        if action == "seats":
            if method not in ("GET", "HEAD"):
                return _json(start_response, "405 Method Not Allowed", {"error": "Use GET"})
            return _tickets(environ, start_response, action)
        if action not in ("hold", "release", "buy", "reset", "simulate"):
            return _json(start_response, "404 Not Found", {"error": "Not found"})
        if method != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "Use POST"})
        if action == "simulate":
            return _simulate(environ, start_response)
        return _tickets(environ, start_response, action)

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
        except Exception:
            log.exception("could not look up short link")
            return _respond(start_response, "503 Service Unavailable", b"Could not look up the link. Try again shortly.\n")
        return _respond(start_response, "302 Found", headers=[("Location", url)])

    return _respond(start_response, "404 Not Found", b"Not found\n")


application = app  # the name gunicorn and other WSGI servers look for by default
