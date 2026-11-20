"""Run the site locally without installing anything.

Run:  python3 server.py [port]      (default port 8000)

For production use gunicorn instead:  gunicorn wsgi:app
The app itself, and its routes, live in wsgi.py.
"""

import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from wsgi import app


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    with make_server("127.0.0.1", port, app, server_class=ThreadingWSGIServer) as server:
        print(f"Serving on http://localhost:{port}/  (Ctrl+C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
