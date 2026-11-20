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
