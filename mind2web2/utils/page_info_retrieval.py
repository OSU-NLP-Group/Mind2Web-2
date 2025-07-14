# Standard library imports
import asyncio
import base64
import hashlib
import random
import time
from io import BytesIO
from logging import Logger
from pathlib import Path
from typing import Optional, Tuple, Union

# Third-party imports
from PIL import Image
from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)
from pydantic import HttpUrl

from mind2web2.api_tools.tool_pdf import is_pdf, format_url
from mind2web2.utils.logging_setup import create_logger


# ================================ Constants ================================

def make_blank_png_b64() -> str:
    # Create 1×1 RGBA fully transparent pixel
    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    # Convert to base64 and remove line breaks
    return base64.b64encode(buf.getvalue()).decode()


# Error handling constants
BLANK_IMG_B64 = make_blank_png_b64()
ERROR_TEXT = "\u26A0\ufe0f This URL could not be loaded (navigation error)."


# User-agent pools
DEFAULT_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/14.1.2 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
]


class PageManager:
    """
    Manage active Page within a BrowserContext, handling new pages, closures, crashes, navigations.
    """

    def __init__(self, context: BrowserContext, logger: Logger):
        self.context = context
        self.logger = logger
        self.current: Optional[Page] = None
        self.closing = False
        self._handlers = []
        # Listen for new page events on context
        handler = lambda page: asyncio.create_task(self._on_new_page(page))
        context.on('page', handler)
        self._handlers.append((context, 'page', handler))
        for pg in context.pages:
            asyncio.create_task(self._on_new_page(pg))

    async def _on_new_page(self, page: Page):
        if self.closing:
            return
        self.logger.debug(f'New page opened: {page.url}')
        self.current = page
        self._attach_handlers(page)

    def _attach_handlers(self, page: Page):
        for event in ('close', 'crash', 'framenavigated'):
            if event == 'close':
                cb = lambda: asyncio.create_task(self._on_close(page))
            elif event == 'crash':
                cb = lambda: asyncio.create_task(self._on_crash(page))
            else:
                cb = lambda frame: asyncio.create_task(self._on_navigate(page, frame))
            page.on(event, cb)
            self._handlers.append((page, event, cb))

    async def _on_close(self, page: Page):
        if self.closing:
            return
        self.logger.warning(f'Page closed: {page.url}')
        pages = self.context.pages
        if pages:
            await self._on_new_page(pages[-1])
        else:
            try:
                new_pg = await self.context.new_page()
                await self._on_new_page(new_pg)
            except Exception as e:
                self.logger.error(f'Failed to reopen page after close: {e}')

    async def _on_crash(self, page: Page):
        if self.closing:
            return
        self.logger.error(f'Page crashed: {page.url}, refreshing...')
        try:
            await page.reload()
        except Exception as e:
            self.logger.error(f'Reload after crash failed: {e}')

    async def _on_navigate(self, page: Page, frame):
        if self.closing:
            return
        if frame == page.main_frame:
            self.logger.debug(f'Frame navigated: {page.url}')
            self.current = page

    async def get(self) -> Page:
        if self.closing:
            raise RuntimeError('Context is closing')
        if not self.current or self.current.is_closed():
            # self.logger.info('No active page, creating a new one')
            page = await self.context.new_page()
            await self._on_new_page(page)
        return self.current

    def dispose(self):
        """Stop listening and prevent new page opens."""
        self.closing = True
        for emitter, event, cb in self._handlers:
            try:
                emitter.off(event, cb)
            except Exception:
                pass
        self._handlers.clear()


class BlockingPopupError(RuntimeError):
    """Raised when a blocking popup / human-verification overlay is detected."""
    pass


async def capture_page_content_async(
        url: HttpUrl,
        logger: Logger,
        wait_until: str = "load",
        headless: bool= True,
        user_data_dir: Union[str, Path] = None,
        grant_permissions: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    # ----------- prepare persistent context dir -----------
    # if user_data_dir is None:
    #     random_bytes = str(random.random()).encode("utf-8")
    #     hash_prefix = hashlib.sha256(random_bytes).hexdigest()[:6]
    #     user_data_dir = Path.cwd() / "tmp" / f"browser_context_{hash_prefix}"
    if user_data_dir:
        user_data_dir.mkdir(parents=True, exist_ok=True)

    target = format_url(url)

    user_agent = random.choice(DEFAULT_USER_AGENTS)
    headers = {"user-agent": user_agent}

    screenshot_b64=make_blank_png_b64()
    page_text=ERROR_TEXT


    # Set location. Set Language to English
    async with async_playwright() as p:
        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                locale='en-US',
                headless=headless,
                ignore_https_errors=True,
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
                    '--password-store=basic',
                    '--use-mock-keychain',
                ],
                extra_http_headers=headers,
                viewport={
                    "width": random.randint(1050, 1150),
                    "height": random.randint(700, 800),
                },
            )
            browser = None  # No separate browser object for persistent context
        else:
            browser = await p.chromium.launch(
                headless=headless,
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
                    '--password-store=basic',
                    '--use-mock-keychain',
                ],
            )
            context = await browser.new_context(
                locale='en-US',
                ignore_https_errors=True,
                extra_http_headers=headers,
                viewport={
                    "width": random.randint(1050, 1150),
                    "height": random.randint(700, 800),
                },
            )

        if grant_permissions:
            try:
                await context.grant_permissions(
                    [
                        "geolocation",
                        "notifications",
                        "camera",
                        "microphone",
                        "clipboard-read",
                        "clipboard-write",
                    ],
                    origin=target,
                )
            except Exception as e:
                logger.error(f'Failed to grant permissions: {e}')

        mgr = PageManager(context, logger)
        # page = await mgr.get()
        # cdp = await context.new_cdp_session(page)
        # await cdp.send("Page.enable")
        # await cdp.send("DOM.enable")
        # await cdp.send("Runtime.enable")

        start_ts = time.time()
        try:
            async def navigate():
                pg = await mgr.get()
                return await pg.goto(target, wait_until=wait_until, timeout=15000)

            try:
                await navigate()
            except Exception as e:
                logger.info(f"Navigation failed (Timeout is fine): {e}")

            # ---------- scroll & full-page capture ----------
            page = await mgr.get()
            for _ in range(3):
                await page.keyboard.press("End")
                await asyncio.sleep(random.uniform(1.0, 2.0))
            for _ in range(random.randint(5, 10)):
                await page.mouse.wheel(0, random.randint(-500, 500))
                await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.keyboard.press("Home")
            await asyncio.sleep(random.uniform(1.0, 2.0))

            try:
                page = await mgr.get()
                cdp = await context.new_cdp_session(page)
                metrics = await cdp.send("Page.getLayoutMetrics")
                css_vp = metrics["cssVisualViewport"]
                css_content = metrics["cssContentSize"]
                width = round(css_vp["clientWidth"])
                height = round(min(css_content["height"], 6000))
                scale = round(metrics.get("visualViewport", {}).get("scale", 1))
                await cdp.send(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "mobile": False,
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": scale,
                    },
                )
                await asyncio.sleep(random.uniform(2.1, 3.2))
                shot = await cdp.send(
                    "Page.captureScreenshot",
                    {"format": "png", "captureBeyondViewport": True},
                )
                screenshot_b64 = shot.get("data")
                text_res = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "document.documentElement.innerText",
                        "returnByValue": True,
                    },
                )
                page_text = text_res.get("result", {}).get("value")
                elapsed = time.time() - start_ts
                logger.debug(f"Completed capture for {target} in {elapsed:.2f}s")
                return screenshot_b64, page_text
            except Exception as e:
                logger.error(f"Error capturing content: {e}")
                return screenshot_b64, page_text
        finally:
            mgr.dispose()
            try:
                await cdp.detach()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            # Close browser if it was created (non-persistent mode)
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass



async def test_pdf_detection():
    """Test PDF detection functionality."""
    logger, _ = create_logger(__name__, r"tmp")
    
    # Test URLs
    test_urls = [
        "https://www.fhwa.dot.gov/policyinformation/statistics/2023/pdf/mv1.pdf",  # Should be PDF
        "https://arxiv.org/pdf/2301.00001.pdf",  # Should be PDF (arxiv)
        "https://www.google.com",  # Should NOT be PDF
        "https://example.com/document.pdf",  # Should be PDF by suffix
    ]
    
    print("🧪 Testing PDF detection functionality...")
    print("=" * 50)
    
    for url in test_urls:
        print(f"\n🔍 Testing: {url}")
        try:
            result = await is_pdf(url, logger)
            status = "✅ IS PDF" if result else "❌ NOT PDF"
            print(f"   Result: {status}")
        except Exception as e:
            print(f"   Error: {e}")
    
    print("\n" + "=" * 50)
    print("✅ PDF detection test completed!")

if __name__ == '__main__':
    # Run the async test function
    asyncio.run(test_pdf_detection())
    
    # Optional: Test webpage capture (commented out by default)
    # logger, _ = create_logger(__name__, r"tmp")
    # test_url = 'https://www.akc.org/dog-breeds/west-highland-white-terrier/'
    # shot_b64, text = asyncio.run(capture_page_content_async(test_url, logger, headless=False))
    # print(text)
    # if shot_b64:
    #     Path('screenshot.png').write_bytes(base64.b64decode(shot_b64))
    #     logger.info('Saved screenshot.png')
    # if text:
    #     Path('page.txt').write_text(text, encoding='utf-8')
    #     logger.info('Saved page.txt')
