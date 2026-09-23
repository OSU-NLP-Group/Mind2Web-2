"""Browser capture of web pages (screenshot and text) with patchright.

:class:`BatchBrowserManager` shares one Chromium instance between concurrent
captures.  :meth:`BatchBrowserManager.capture` loads a URL in a fresh browser
context and returns a :class:`Capture`: the page's screenshot and its text
(the HTML converted to Markdown), or the reason the capture failed.
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from logging import Logger
from typing import Optional
from urllib.parse import urlparse

import html2text
from patchright.async_api import Browser, BrowserContext, Page, Response, async_playwright
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError


def html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown."""
    h = html2text.HTML2Text()
    h.ignore_links = True      # Ignore hyperlinks
    h.ignore_emphasis = True   # Ignore bold/italic emphasis
    h.images_to_alt = True     # Convert images to alt text
    h.body_width = 0
    return h.handle(html)


# User-agent pools
DEFAULT_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/14.1.2 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
]

BLOCK_STATUSES = frozenset({401, 403, 407, 429, 999})
"""HTTP statuses of a refusal rather than content (999 is LinkedIn's)."""
SHORT_PAGE_CHARS = 3000
"""Only pages with less text than this (in characters) can be judged a refusal or an error page."""
# Page titles of bot checks and access-denied pages (Cloudflare, Akamai, Imperva, PerimeterX, ...)
_BLOCK_TITLE = re.compile(
    r"^\s*(just a moment|checking your browser|attention required|access denied|"
    r"access to this page has been denied|pardon our interruption|verify you are (a )?human|"
    r"are you a robot|robot or human)", re.IGNORECASE)
# Wording of bot checks and access-denied pages
_BLOCK_TEXT = re.compile(
    r"(verify|confirm) (that )?you are (a )?human|are you a robot|(you are|you're) not a robot|"
    r"unusual traffic from your computer|enable javascript and cookies to continue|"
    r"you have been blocked|request unsuccessful\. incapsula|complete the security check|"
    r"automated access to amazon data", re.IGNORECASE)
# JavaScript bot checks that a real browser usually passes by itself within seconds
_JS_CHALLENGE_TITLE = re.compile(r"just a moment|checking your browser", re.IGNORECASE)
_CHALLENGE_WAIT_MS = 15_000


def detect_block(status: Optional[int], title: str, text: str) -> Optional[str]:
    """Why a loaded page is a refusal (bot check, access denied) rather than content, or ``None``.

    ``status`` is the HTTP status of the displayed document, ``title`` its
    title, and ``text`` its text.  Only a page with less text than
    :data:`SHORT_PAGE_CHARS` can be a refusal: a longer page is content even
    with a refusal status, since some sites send their pages with one, and
    even if it uses a refusal's wording, since articles may quote it.  A
    shorter page is a refusal if its status is in :data:`BLOCK_STATUSES`, or
    its title or text reads like a bot check or an access-denied notice.
    """
    if len(text) >= SHORT_PAGE_CHARS:
        return None
    if status in BLOCK_STATUSES:
        return f"HTTP {status}"
    if _BLOCK_TITLE.search(title or ""):
        return f"bot check or access denied (page title {title.strip()[:80]!r})"
    match = _BLOCK_TEXT.search(text)
    if match:
        return f"bot check or access denied ({match.group(0)!r})"
    return None


@dataclass
class Capture:
    """The outcome of capturing one URL.

    On success, ``screenshot_b64`` (base64 PNG) and ``text`` are set and
    ``error`` is ``None``.  On failure, ``error`` says why; ``blocked`` is true
    when the site refused the automated browser (see :func:`detect_block`),
    so that a person may still be able to capture the page.  ``status`` is
    the HTTP status of the displayed document, when one was received.
    """

    screenshot_b64: Optional[str] = None
    text: Optional[str] = None
    error: Optional[str] = None
    blocked: bool = False
    status: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class BatchBrowserManager:
    """One shared Chromium browser for concurrent page captures.

    At most ``max_concurrent_pages`` captures run at once; the others wait for
    a slot.  Each attempt gets a fresh browser context and is bounded by
    ``page_timeout`` seconds, counted from when it gets its slot, so waiting
    in the queue never uses up a URL's time.  An attempt that times out or
    raises is retried up to ``max_retries`` attempts in total, and the
    browser is restarted if it disconnected; an attempt during which the
    browser disconnected is repeated once without counting, since another
    page may have crashed the shared browser.  These outcomes are reported
    without retrying:

    * the page did not load (DNS or connection errors, a download instead of
      a page, nothing received within ``navigation_timeout``);
    * the site refused the browser (see :func:`detect_block`);
    * the server answered with an HTTP 5xx status and a page with less text
      than :data:`SHORT_PAGE_CHARS`, an error page.

    A page that is still loading after ``navigation_timeout`` is captured as
    far as it has loaded.  Pages with other statuses, such as 404, are
    captured as they render.
    """

    def __init__(self, headless: bool = True, max_retries: int = 3, max_concurrent_pages: int = 10,
                 page_timeout: float = 90.0, navigation_timeout: float = 30.0):
        self.headless = headless
        self.max_retries = max(1, max_retries)
        self.max_concurrent_pages = max_concurrent_pages
        self.page_timeout = page_timeout
        self.navigation_timeout = navigation_timeout
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._browser_lock = asyncio.Lock()
        self._page_semaphore = asyncio.Semaphore(max_concurrent_pages)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def start(self):
        """Launch the browser if it is not running."""
        if self.browser is None:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-site-isolation-trials",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--ignore-certificate-errors",
                    "--safebrowsing-disable-auto-save",
                    "--safebrowsing-disable-download-protection",
                    "--password-store=basic",
                    "--use-mock-keychain",
                ]
            )

    async def stop(self):
        """Close the browser and the driver; each step is bounded so that a hung browser cannot block shutdown."""
        browser, playwright = self.browser, self.playwright
        self.browser = self.playwright = None
        for close in ((browser.close if browser else None), (playwright.stop if playwright else None)):
            if close is not None:
                try:
                    await asyncio.wait_for(close(), 15)
                except Exception:
                    pass

    async def capture(self, url: str, logger: Logger, wait_until: str = "load") -> Capture:
        """Load ``url`` and capture its screenshot and text; see :class:`Capture`."""
        logger.info(f"Start collecting page {url}")
        async with self._page_semaphore:
            error = "not attempted"
            attempt, repeated = 1, False
            while attempt <= self.max_retries:
                async with self._browser_lock:
                    await self.start()
                browser = self.browser
                context: Optional[BrowserContext] = None
                try:
                    async with asyncio.timeout(self.page_timeout):
                        context = await browser.new_context(
                            locale='en-US',
                            ignore_https_errors=True,
                            extra_http_headers={"user-agent": random.choice(DEFAULT_USER_AGENTS)},
                            viewport={"width": random.randint(1050, 1150), "height": random.randint(700, 800)},
                        )
                        return await self._capture_in(context, url, logger, wait_until)
                except TimeoutError:
                    error = f"timed out after {self.page_timeout:.0f}s"
                except Exception as e:
                    error = f"{type(e).__name__}: {e}".splitlines()[0]
                finally:
                    if context is not None:  # outside the timeout, so that closing cannot discard a finished capture
                        try:
                            await asyncio.wait_for(context.close(), 10)
                        except Exception:
                            pass
                if not browser.is_connected():
                    async with self._browser_lock:
                        if self.browser is browser:
                            logger.warning("Browser disconnected; it will be restarted")
                            await self.stop()
                    if not repeated:  # the shared browser failed, possibly because of another page
                        repeated = True
                        logger.warning(f"Attempt {attempt}/{self.max_retries} for {url} is repeated "
                                       f"because the browser disconnected: {error}")
                        continue
                logger.warning(f"Attempt {attempt}/{self.max_retries} failed for {url}: {error}")
                attempt += 1
            return Capture(error=error)

    async def _capture_in(self, context: BrowserContext, url: str, logger: Logger, wait_until: str) -> Capture:
        """Load ``url`` in a new page of ``context`` and capture it; the caller closes ``context``."""
        await _grant_permissions(context, url, logger)
        page = await context.new_page()
        # Status of the displayed document: the last main-frame document response,
        # which follows redirects and the reload after a passed bot check
        document_status: dict[str, int] = {}
        page.on("response", lambda response: _record_document_status(page, response, document_status))

        try:
            await page.goto(url, wait_until=wait_until, timeout=self.navigation_timeout * 1000)
        except PlaywrightTimeoutError:
            if _nothing_loaded(page):
                return Capture(error=f"navigation failed: no response within {self.navigation_timeout:.0f}s")
            logger.info(f"Navigation timed out; capturing what has loaded: {url}")
        except PlaywrightError as e:
            message = str(e).splitlines()[0]
            if "Download is starting" in message or _nothing_loaded(page):
                return Capture(error=f"navigation failed: {message}")
            logger.info(f"Navigation error ({message}); capturing what has loaded: {url}")
        if _nothing_loaded(page):
            return Capture(error="navigation failed: the browser showed an error page")

        await _wait_for_js_challenge(page)

        # Scroll to trigger lazy-loaded content
        for _ in range(3):
            await page.keyboard.press("End")
            await asyncio.sleep(random.uniform(0.3, 0.8))
        await page.keyboard.press("Home")
        await asyncio.sleep(random.uniform(0.3, 0.8))

        screenshot_b64, page_html = await _capture_screenshot_and_html(context, page)
        text = await asyncio.to_thread(html_to_markdown, page_html)  # slow on large pages

        status = document_status.get("status")
        block = detect_block(status, await page.title(), text)
        if block is not None:
            return Capture(error=f"blocked: {block}", blocked=True, status=status)
        if status is not None and status >= 500 and len(text) < SHORT_PAGE_CHARS:
            return Capture(error=f"HTTP {status}", status=status)
        return Capture(screenshot_b64=screenshot_b64, text=text, status=status)


async def _grant_permissions(context: BrowserContext, url: str, logger: Logger) -> None:
    parsed = urlparse(url)
    try:
        await context.grant_permissions(
            ["geolocation", "notifications", "camera", "microphone", "clipboard-read", "clipboard-write"],
            origin=f"{parsed.scheme}://{parsed.netloc}",
        )
    except Exception as e:
        logger.debug(f'Failed to grant permissions: {e}')


def _record_document_status(page: Page, response: Response, document_status: dict[str, int]) -> None:
    try:
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            document_status["status"] = response.status
    except PlaywrightError:  # the frame is already gone
        pass


def _nothing_loaded(page: Page) -> bool:
    """Whether the page shows no response from the site: still blank, or Chromium's own error page."""
    return page.url in ("", "about:blank") or page.url.startswith("chrome-error://")


async def _wait_for_js_challenge(page: Page) -> None:
    """Give a JavaScript bot check (e.g. Cloudflare's "Just a moment...") time to pass by itself."""
    try:
        if not _JS_CHALLENGE_TITLE.search(await page.title()):
            return
        await page.wait_for_function(
            f"() => !/{_JS_CHALLENGE_TITLE.pattern}/i.test(document.title)", timeout=_CHALLENGE_WAIT_MS)
    except PlaywrightError:
        pass  # timed out, or the check reloaded the page while it was being watched
    try:
        await page.wait_for_load_state("load", timeout=_CHALLENGE_WAIT_MS)
    except PlaywrightError:
        pass  # still on the bot check: detect_block reports it


async def _capture_screenshot_and_html(context: BrowserContext, page: Page) -> tuple[str, str]:
    """A screenshot of the page up to 6000 CSS pixels tall, and its HTML, via the DevTools protocol."""
    cdp = await context.new_cdp_session(page)
    await cdp.send("Page.enable")
    await cdp.send("DOM.enable")
    await cdp.send("Runtime.enable")

    metrics = await cdp.send("Page.getLayoutMetrics")
    css_vp = metrics["cssVisualViewport"]
    css_content = metrics["cssContentSize"]
    width = round(css_vp["clientWidth"])
    height = round(min(css_content["height"], 6000))
    scale = round(metrics.get("visualViewport", {}).get("scale", 1))
    await cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"mobile": False, "width": width, "height": height, "deviceScaleFactor": scale},
    )
    await asyncio.sleep(random.uniform(0.5, 1.0))  # let the page settle after resizing

    shot_result, html_result = await asyncio.gather(
        cdp.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}),
        cdp.send("Runtime.evaluate", {"expression": "document.documentElement.outerHTML", "returnByValue": True}),
    )
    return shot_result.get("data"), html_result.get("result", {}).get("value", "")

