"""Browser capture against a local server: what is captured, what is a failure, and what is a refusal.

Skipped when patchright's Chromium is not installed (``patchright install chromium``).
"""
from __future__ import annotations

import asyncio
import base64
import logging

import pytest

from local_site import HTML, LocalSite, Route, unused_port_url
from mind2web2.utils.page_info_retrieval import BatchBrowserManager, detect_block

LOGGER = logging.getLogger("test")

ARTICLE = ("<html><head><title>Article</title></head><body><h1>Mind2Web 2</h1>"
           + "<p>Agentic search systems are evaluated with rubric trees.</p>" * 80
           + "<p>Some sites ask you to verify you are a human.</p></body></html>").encode()


def challenge(handler) -> Route:
    """A bot check that passes by itself: a 403 page that sets a cookie and reloads into the content."""
    if "passed=1" in (handler.headers.get("Cookie") or ""):
        return Route(body=ARTICLE)
    return Route(403, b"<html><head><title>Just a moment...</title></head><body>Checking your browser"
                      b"<script>setTimeout(() => { document.cookie = 'passed=1; path=/'; location.reload(); }, 300)"
                      b"</script></body></html>")


ROUTES = {
    "/article": Route(body=ARTICLE),
    "/forbidden": Route(403, b"<html><head><title>403 Forbidden</title></head><body>Forbidden</body></html>"),
    "/challenge": Route(respond=challenge),
    "/robot-check": Route(body=b"<html><head><title>Robot or human?</title></head>"
                               b"<body>Activate and hold the button to confirm that you are human.</body></html>"),
    "/unavailable": Route(503, b"<html><head><title>503</title></head><body>Service Unavailable</body></html>"),
    "/missing": Route(404, b"<html><head><title>Not found</title></head><body>No such page</body></html>"),
    "/download": Route(body=b"PK\x03\x04 zip bytes",
                       headers={"Content-Type": "application/zip",
                                "Content-Disposition": "attachment; filename=data.zip"}),
    "/hang": Route(body=b"too late", delay=20),
}


def outcome(capture) -> dict:
    return {"ok": capture.ok, "blocked": capture.blocked, "status": capture.status,
            "error": capture.error.split(":")[0] if capture.error else None}


async def capture_all(urls: dict[str, str]) -> dict:
    async with BatchBrowserManager(headless=True, max_retries=1, max_concurrent_pages=4,
                                   page_timeout=30, navigation_timeout=2) as browser:
        results = await asyncio.gather(*(browser.capture(url, LOGGER) for url in urls.values()))
    return dict(zip(urls, results))


@pytest.fixture(scope="module")
def captures():
    with LocalSite(ROUTES) as site:
        urls = {path: site.url(path) for path in ROUTES} | {"refused": unused_port_url()}
        try:
            return asyncio.run(capture_all(urls))
        except Exception as e:  # patchright raises a plain Error when Chromium is missing
            if "Executable doesn't exist" in str(e):
                pytest.skip("patchright Chromium is not installed")
            raise


def test_capture_outcomes(captures):
    assert {name: outcome(capture) for name, capture in captures.items()} == {
        "/article": {"ok": True, "blocked": False, "status": 200, "error": None},
        "/forbidden": {"ok": False, "blocked": True, "status": 403, "error": "blocked"},
        "/challenge": {"ok": True, "blocked": False, "status": 200, "error": None},  # passed by itself
        "/robot-check": {"ok": False, "blocked": True, "status": 200, "error": "blocked"},
        "/unavailable": {"ok": False, "blocked": False, "status": 503, "error": "HTTP 503"},
        "/missing": {"ok": True, "blocked": False, "status": 404, "error": None},  # captured as it renders
        "/download": {"ok": False, "blocked": False, "status": None, "error": "navigation failed"},
        "/hang": {"ok": False, "blocked": False, "status": None, "error": "navigation failed"},
        "refused": {"ok": False, "blocked": False, "status": None, "error": "navigation failed"},
    }


def test_a_captured_page_has_its_text_and_a_png_screenshot(captures):
    article = captures["/article"]
    assert "Agentic search systems are evaluated with rubric trees." in article.text
    assert base64.b64decode(article.screenshot_b64)[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Agentic search systems" in captures["/challenge"].text


@pytest.mark.parametrize("status, title, text, blocked", [
    (429, "Example", "x" * 10_000, True),
    (200, "Just a moment...", "Checking your browser", True),
    (200, "Access Denied", "You don't have permission to access this page.", True),
    (200, "Access denied errors in SQL Server", "x" * 10_000, False),  # long article
    (200, "News", "Please enable JavaScript and cookies to continue", True),
    (200, "Contact us", "This site is protected by reCAPTCHA and the Google Privacy Policy.", False),
    (None, "Home", "Welcome", False),
])
def test_detect_block(status, title, text, blocked):
    assert (detect_block(status, title, text) is not None) == blocked
