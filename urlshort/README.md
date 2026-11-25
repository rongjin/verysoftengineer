# urlshort

Turns a URL into a 10-character code made of letters and digits.

```python
from urlshort import shorten, Shortener

shorten("https://example.com/a/very/long/link")   # -> 10-character code, same every time

s = Shortener()                 # remembers codes so you can look them up again
code = s.shorten("https://example.com")
s.expand(code)                  # -> "https://example.com"
```

The code is the first 10 base62 characters of the URL's SHA-256 hash. Only `http` and `https` URLs are accepted. `Shortener` keeps its mappings in memory, so they are lost when the program exits.

Run the tests with `pytest` from this folder.

## On the website

The site's URL shortener page also lives here. `wsgi.py` in the project root serves it and its API.

- `shorten.html`: the page, served at `/shorten.html`
- `db.py`: `PostgresShortener`, which stores short links in Postgres when `DATABASE_URL` is set
- `schema.sql`: the `short_links` table, if you want to create it by hand
