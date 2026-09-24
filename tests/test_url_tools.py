"""The URL normalization that decides when two URLs are the same page."""
from __future__ import annotations

from mind2web2.utils.url_tools import normalize_url_keep_case, normalize_url_simple


def test_normalized_form_ignores_surface_differences():
    forms = [
        "https://example.com/Page",
        "http://www.example.com/page/",
        "https://example.com/page?utm_source=chatgpt.com#section",
        "https://EXAMPLE.com/%50age",
    ]
    assert {normalize_url_simple(u) for u in forms} == {"https://example.com/page"}
    assert normalize_url_simple("https://example.com/p?id=1&utm_medium=x") == "https://example.com/p?id=1"


def test_case_preserving_form_differs_from_the_normalized_form_only_in_letter_case():
    url = "http://www.Example.com/Docs/Page/?utm_source=x#top"
    assert normalize_url_keep_case(url) == "https://Example.com/Docs/Page"
    assert normalize_url_simple(url) == normalize_url_keep_case(url).lower()
