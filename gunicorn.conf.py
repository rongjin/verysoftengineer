# Loaded automatically when you run `gunicorn wsgi:app` from this folder.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from settings import DATABASE_URL  # noqa: E402  (also loads .env)

# Without DATABASE_URL, short links are kept in memory, so all requests must reach
# the same process; more workers would each have their own links and randomly 404.
# With DATABASE_URL (see .env.example) links live in Postgres and more workers are safe.
workers = 2 if DATABASE_URL else 1
threads = 4

# Set PORT to change the port, e.g. `PORT=9000 gunicorn wsgi:app`.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
accesslog = "-"
