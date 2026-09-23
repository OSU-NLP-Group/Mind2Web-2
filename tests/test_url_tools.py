"""URL extraction from answers and the normalization that decides when two URLs are the same page."""
from __future__ import annotations

import pytest

from mind2web2.utils.url_tools import normalize_url_simple, regex_find_urls


@pytest.mark.parametrize("text, expected", [
    ("[Python](https://en.wikipedia.org/wiki/Python_(programming_language))",
     ["https://en.wikipedia.org/wiki/Python_(programming_language)"]),
    ("See https://en.wikipedia.org/wiki/Python_(programming_language).",
     ["https://en.wikipedia.org/wiki/Python_(programming_language)"]),
    ("(source: https://example.com/page)", ["https://example.com/page"]),
    (r"https://example.com/some\_page\_name", ["https://example.com/some_page_name"]),
    ("Visit https://example.com:8080/path?q=1.", ["https://example.com:8080/path?q=1"]),
    ("https://en.wikipedia.org/wiki/Ender's_Game", ["https://en.wikipedia.org/wiki/Ender's_Game"]),
    ("参见https://example.com/a，以及 https://example.com/b。", ["https://example.com/a", "https://example.com/b"]),
    ("**https://example.com/bold**", ["https://example.com/bold"]),
    ("<https://example.com/auto>", ["https://example.com/auto"]),
    ("www.example.com/page and http://www.example.com/x",
     ["https://www.example.com/page", "http://www.example.com/x"]),
    ("[https://a.com/x](https://a.com/x)", ["https://a.com/x"]),
    ("“https://example.com/q”", ["https://example.com/q"]),
    ("https://www.google.com/maps/place/X/@1.2,3.4,15z?entry=ttu",
     ["https://www.google.com/maps/place/X/@1.2,3.4,15z?entry=ttu"]),
    ("| https://example.com/t | x |", ["https://example.com/t"]),
    ("https://example.com/a?b=c&d=e#frag, then https://example.com/a?b=c&d=e#frag",
     ["https://example.com/a?b=c&d=e#frag"]),
    ("https://example.com/dir/ and 'https://y.com/b'", ["https://example.com/dir/", "https://y.com/b"]),
    ('[t](https://x.com/a "Title")', ["https://x.com/a"]),
    ("no links here, just www and https://", []),
    ("[filter](https://www.example.com/search?filters[type]=book)",
     ["https://www.example.com/search?filters[type]=book"]),
    ("[fonts](https://fonts.googleapis.com/css?family=Roboto|Open+Sans)",
     ["https://fonts.googleapis.com/css?family=Roboto|Open+Sans"]),
    ("|https://a.com/x|https://b.com/y|", ["https://a.com/x", "https://b.com/y"]),
    ("[https://a.com/x](https://b.com/y)", ["https://a.com/x", "https://b.com/y"]),
    ("[t](https://a.com/x)(see https://b.com/y)", ["https://a.com/x", "https://b.com/y"]),
    (r"[w](https://en.wikipedia.org/wiki/Foo_\(bar\))", ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    ("_https://example.com/ital_, ~~https://example.com/strike~~ and https://example.com/a_",
     ["https://example.com/ital", "https://example.com/strike", "https://example.com/a_"]),
])
def test_regex_find_urls(text, expected):
    assert regex_find_urls(text) == expected


def test_normalized_form_ignores_surface_differences():
    forms = [
        "https://example.com/Page",
        "http://www.example.com/page/",
        "https://example.com/page?utm_source=chatgpt.com#section",
        "https://EXAMPLE.com/%50age",
    ]
    assert {normalize_url_simple(u) for u in forms} == {"https://example.com/page"}
    assert normalize_url_simple("https://example.com/p?id=1&utm_medium=x") == "https://example.com/p?id=1"
