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
    }

    async def check(site: LocalSite):
        parser = PDFParser()
        return {path: (await is_pdf(site.url(path)), await parser.fetch(site.url(path)) == pdf)
                for path in [*routes, "/gone.pdf"]}

    with LocalSite(routes) as site:
        assert asyncio.run(check(site)) == {
            "/paper": (True, True),          # PDF content type
            "/blob": (True, True),           # %PDF- signature
            "/landing.pdf": (True, False),   # looks like a PDF, but the download is HTML
            "/article": (False, False),
            "/gone.pdf": (True, False),      # 404
        }


def test_detection_gives_up_after_its_timeout_without_blocking_the_event_loop():
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
        result = await is_pdf(url, timeout=0.5)
        elapsed = time.monotonic() - start
        tick.cancel()
        return result, elapsed, max(gaps)

    with LocalSite({"/slow": Route(body=b"late", delay=3)}) as site:
        result, elapsed, max_gap = asyncio.run(run(site.url("/slow")))
    assert result is False
    assert elapsed < 1.5
    assert max_gap < 0.25
