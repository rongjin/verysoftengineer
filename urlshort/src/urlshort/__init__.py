"""Turn a URL into a short, deterministic 10-character code."""

import hashlib
from urllib.parse import urlsplit

__all__ = ["shorten", "Shortener", "CODE_LENGTH"]

CODE_LENGTH = 10
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _validate(url: str) -> str:
    if not isinstance(url, str):
        raise TypeError("url must be a string")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not a valid http(s) URL: {url!r}")
    return url


def shorten(url: str) -> str:
    """Return a 10-character base62 code for ``url``.

    The code is the first 10 base62 characters of the URL's SHA-256 hash,
    so the same URL always gives the same code.
    """
    url = _validate(url)
    n = int.from_bytes(hashlib.sha256(url.encode("utf-8")).digest(), "big")
    chars = []
    while n:
        n, r = divmod(n, 62)
        chars.append(_ALPHABET[r])
    return "".join(reversed(chars))[:CODE_LENGTH].rjust(CODE_LENGTH, "0")


class Shortener:
    """Keeps code -> URL mappings in memory so codes can be expanded again."""

    def __init__(self) -> None:
        self._urls: dict[str, str] = {}

    def shorten(self, url: str) -> str:
        url = _validate(url)
        code = shorten(url)
        existing = self._urls.setdefault(code, url)
        if existing != url:
            raise RuntimeError(f"code collision for {code!r}: {existing!r} vs {url!r}")
        return code

    def expand(self, code: str) -> str:
        """Return the original URL for ``code``; raises KeyError if unknown."""
        return self._urls[code]

    def __len__(self) -> int:
        return len(self._urls)
