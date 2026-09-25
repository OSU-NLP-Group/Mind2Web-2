"""PDF detection, download, and parsing (network calls go to a local server)."""
import asyncio
import base64
import time

import pymupdf

from local_site import LocalSite, Route
from mind2web2.api_tools.tool_pdf import PDFParser, is_pdf


def _make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_extract_returns_text_and_one_image_per_page():
    pdf = _make_pdf(["First page about Mind2Web", "Second page"])
    images, text = asyncio.run(PDFParser().extract(pdf))
    assert "First page about Mind2Web" in text
    assert "Second page" in text
    assert len(images) == 2
    assert base64.b64decode(images[0])[:2] == b"\xff\xd8"  # JPEG


def test_extract_rejects_non_pdf_bytes():
    assert asyncio.run(PDFParser().extract(b"<html>not a pdf</html>")) == (None, None)
    assert asyncio.run(PDFParser().extract(b"%PDF-1.4 truncated")) == (None, None)


def test_detection_and_download_check_what_the_server_returns():
    pdf = _make_pdf(["A paper"])
    routes = {
        "/paper": Route(body=pdf, headers={"Content-Type": "application/pdf"}),
        "/blob": Route(body=pdf, headers={"Content-Type": "application/octet-stream"}),
        "/landing.pdf": Route(body=b"<html><body>Log in to read this paper</body></html>"),
        "/article": Route(body=b"<html><body>An article</body></html>"),
        "/link.pdf": Route(302, headers={"Location": "/paper"}),
    }

    async def check(site: LocalSite):
        parser = PDFParser()
        return {path: (await is_pdf(site.url(path)), await parser.fetch(site.url(path)) == pdf)
                for path in [*routes, "/gone.pdf"]}

    async def final_urls(site: LocalSite):
        parser = PDFParser()
        return {path: (await parser.fetch_with_final_url(site.url(path)))[1]
                for path in ("/paper", "/link.pdf", "/landing.pdf")}

    with LocalSite(routes) as site:
        assert asyncio.run(check(site)) == {
            "/paper": (True, True),          # PDF content type
            "/blob": (True, True),           # %PDF- signature
            "/landing.pdf": (True, False),   # looks like a PDF, but the download is HTML
            "/article": (False, False),
            "/link.pdf": (True, True),       # redirected to /paper
            "/gone.pdf": (True, False),      # 404
        }
        assert asyncio.run(final_urls(site)) == {
            "/paper": site.url("/paper"), "/link.pdf": site.url("/paper"), "/landing.pdf": None}


def test_detection_gives_up_after_its_timeout_without_blocking_the_event_loop():
    """A blocking download would stall the event loop until the timeout or the server's answer.

    The limits sit halfway between what a working probe takes and what a
    blocking one would, so that a loaded machine still passes.
    """
    timeout, server_delay = 2.0, 8.0

    async def run(url: str):
        gaps, last = [], time.monotonic()

        async def ticker():
            nonlocal last
            while True:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        start = time.monotonic()
        result = await is_pdf(url, timeout=timeout)
        elapsed = time.monotonic() - start
        tick.cancel()
        return result, elapsed, max(gaps)

    with LocalSite({"/slow": Route(body=b"late", delay=server_delay)}) as site:
        result, elapsed, max_gap = asyncio.run(run(site.url("/slow")))
    assert result is False
    assert elapsed < (timeout + server_delay) / 2  # gave up at the timeout, not at the server's answer
    assert max_gap < timeout / 2  # the event loop kept running while the probe waited
