import pytest

from urlshort import CODE_LENGTH, Shortener, shorten


def test_code_is_10_base62_chars():
    code = shorten("https://example.com/a/very/long/path?x=1")
    assert len(code) == CODE_LENGTH == 10
    assert code.isalnum() and code.isascii()


def test_deterministic_and_strips_whitespace():
    assert shorten("https://example.com") == shorten("  https://example.com \n")


def test_different_urls_differ():
    codes = {shorten(f"https://example.com/{i}") for i in range(10_000)}
    assert len(codes) == 10_000


@pytest.mark.parametrize("bad", ["", "example.com", "ftp://example.com", "https://"])
def test_rejects_invalid(bad):
    with pytest.raises(ValueError):
        shorten(bad)


def test_rejects_non_string():
    with pytest.raises(TypeError):
        shorten(None)


def test_shortener_round_trip():
    s = Shortener()
    url = "https://github.com/rongjin/verysoftengineer"
    code = s.shorten(url)
    assert code == shorten(url)
    assert s.expand(code) == url
    assert s.shorten(url) == code and len(s) == 1
    with pytest.raises(KeyError):
        s.expand("0000000000")
