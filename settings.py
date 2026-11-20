"""Loads .env into the environment. Imported by wsgi.py and gunicorn.conf.py."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv(path=ROOT / ".env"):
    """Read KEY=VALUE lines from .env into os.environ (real env vars win)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
