"""Browser capture against a local server: what is captured, what is a failure, and what is a refusal.

Skipped when patchright's Chromium is not installed (``patchright install chromium``).
"""
from __future__ import annotations

import asyncio
import base64
import logging

import pytest

from local_site import LocalSite, Route, unused_port_url
from mind2web2.utils.page_info_retrieval import BatchBrowserManager, Capture, detect_block

LOGGER = logging.getLogger("test")

NAVIGATION_TIMEOUT = 8
"""Seconds; generous, so that a loaded machine still loads the local pages in time."""
CHALLENGE_SECONDS = 6
"""When the local bot check passes: after the scrolling and settling that precede a capture (at
most 4.2 s after the page loads), so that the capture shows the content only if it waits for the check."""

ARTICLE = ("<html><head><title>Article</title></head><body><h1>Mind2Web 2</h1>"
           + "<p>Agentic search systems are evaluated with rubric trees.</p>" * 80
           + "<p>Some sites ask you to verify you are a human.</p></body></html>").encode()


def challenge(handler) -> Route:
    """A bot check that passes by itself: a 403 page that, after :data:`CHALLENGE_SECONDS`, sets a
    cookie and reloads into the content."""
    if "passed=1" in (handler.headers.get("Cookie") or ""):
        return Route(body=ARTICLE)
    return Route(403, b"<html><head><title>Just a moment...</title></head><body>Checking your browser"
                      b"<script>setTimeout(() => { document.cookie = 'passed=1; path=/'; location.reload(); }, "
                      + str(CHALLENGE_SECONDS * 1000).encode() + b")</script></body></html>")


ROUTES = {
    "/article": Route(body=ARTICLE),
    "/moved": Route(302, headers={"Location": "/article"}),
    "/forbidden": Route(403, b"<html><head><title>403 Forbidden</title></head><body>Forbidden</body></html>"),
    "/article-sent-with-403": Route(403, ARTICLE),
    "/challenge": Route(respond=challenge),
    "/robot-check": Route(body=b"<html><head><title>Robot or human?</title></head>"
                               b"<body>Activate and hold the button to confirm that you are human.</body></html>"),
    "/rate-limited": Route(429, b"<html><head><title>429 Too Many Requests</title></head>"
                                b"<body>Too many requests</body></html>"),
    "/unavailable": Route(503, b"<html><head><title>503</title></head><body>Service Unavailable</body></html>"),
    "/article-sent-with-503": Route(503, ARTICLE),
    "/missing": Route(404, b"<html><head><title>Not found</title></head><body>No such page</body></html>"),
    "/download": Route(body=b"PK\x03\x04 zip bytes",
                       headers={"Content-Type": "application/zip",
                                "Content-Disposition": "attachment; filename=data.zip"}),
    "/hang": Route(body=b"too late", delay=3 * NAVIGATION_TIMEOUT),
}


def outcome(capture) -> dict:
    return {"ok": capture.ok, "blocked": capture.blocked, "status": capture.status,
            "error": capture.error.split(":")[0] if capture.error else None}


async def capture_all(urls: dict[str, str]) -> dict:
    async with BatchBrowserManager(headless=True, max_retries=1, max_concurrent_pages=6,
                                   page_timeout=60, navigation_timeout=NAVIGATION_TIMEOUT) as browser:
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
        "/moved": {"ok": True, "blocked": False, "status": 200, "error": None},  # the redirect's target
        "/forbidden": {"ok": False, "blocked": True, "status": 403, "error": "blocked"},
        "/article-sent-with-403": {"ok": True, "blocked": False, "status": 403, "error": None},
        "/challenge": {"ok": True, "blocked": False, "status": 200, "error": None},  # passed while waited for
        "/robot-check": {"ok": False, "blocked": True, "status": 200, "error": "blocked"},
        "/rate-limited": {"ok": False, "blocked": False, "status": 429, "error": "HTTP 429"},  # retried later
        "/unavailable": {"ok": False, "blocked": False, "status": 503, "error": "HTTP 503"},
        "/article-sent-with-503": {"ok": True, "blocked": False, "status": 503, "error": None},
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


def test_a_capture_reports_the_url_it_ended_at(captures):
    article_url = captures["/article"].final_url
    assert article_url.endswith("/article")
    assert captures["/moved"].final_url == article_url
    assert captures["/forbidden"].final_url is None  # set only on success


@pytest.mark.parametrize("status, title, text, blocked", [
    (429, "Too Many Requests", "Too many requests", False),  # rate limited: an ordinary failure
    (403, "Example", "x" * 10_000, False),  # long page: content, whatever its status
    (200, "Just a moment...", "Checking your browser", True),
    (200, "Access Denied", "You don't have permission to access this page.", True),
    (200, "Access denied errors in SQL Server", "x" * 10_000, False),  # long article
    (200, "News", "Please enable JavaScript and cookies to continue", True),
    (200, "Contact us", "This site is protected by reCAPTCHA and the Google Privacy Policy.", False),
    (None, "Home", "Welcome", False),
])
def test_detect_block(status, title, text, blocked):
    assert (detect_block(status, title, text) is not None) == blocked


# ------------------------------------------------------------------ retries, with a fake browser

class FakeContext:
    def __init__(self, close_seconds: float = 0.0):
        self.close_seconds = close_seconds
        self.closed = False

    async def close(self):
        await asyncio.sleep(self.close_seconds)
        self.closed = True


class FakeBrowser:
    def __init__(self, close_seconds: float = 0.0):
        self.connected = True
        self.close_seconds = close_seconds
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **kwargs):
        self.contexts.append(FakeContext(self.close_seconds))
        return self.contexts[-1]

    async def close(self):
        self.connected = False


def fake_manager(capture_in, max_retries: int = 1, page_timeout: float = 30, close_seconds: float = 0.0):
    """A manager whose browsers are fakes, launched anew after each disconnect, and whose page loads run ``capture_in``."""
    manager = BatchBrowserManager(max_retries=max_retries, page_timeout=page_timeout)
    manager.launched = []

    async def start():
        if manager.browser is None:
            manager.browser = FakeBrowser(close_seconds)
            manager.launched.append(manager.browser)

    manager.start = start
    manager._capture_in = capture_in
    return manager


def test_closing_the_context_does_not_count_against_the_page_timeout():
    async def capture_in(context, url, logger, wait_until):
        await asyncio.sleep(0.2)
        return Capture(screenshot_b64="c2NyZWVu", text="text", status=200)

    manager = fake_manager(capture_in, page_timeout=0.5, close_seconds=0.5)
    capture = asyncio.run(manager.capture("https://example.com", LOGGER))
    assert capture.ok
    assert [context.closed for context in manager.launched[0].contexts] == [True]


def test_an_attempt_the_browser_disconnected_during_is_repeated_once_without_counting():
    loads = []

    async def disconnect_once(context, url, logger, wait_until):
        loads.append(manager.browser)
        if len(loads) == 1:
            manager.browser.connected = False
            raise RuntimeError("Target page, context or browser has been closed")
        return Capture(screenshot_b64="c2NyZWVu", text="text", status=200)

    manager = fake_manager(disconnect_once, max_retries=1)
    assert asyncio.run(manager.capture("https://example.com", LOGGER)).ok
    assert loads == manager.launched and len(loads) == 2

    async def always_disconnect(context, url, logger, wait_until):
        manager.browser.connected = False
        raise RuntimeError("Target page, context or browser has been closed")

    manager = fake_manager(always_disconnect, max_retries=1)
    capture = asyncio.run(manager.capture("https://example.com", LOGGER))
    assert capture.error == "RuntimeError: Target page, context or browser has been closed"
    assert len(manager.launched) == 2
