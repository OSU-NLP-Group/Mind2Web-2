"""Where evaluation and the crawler get pages: the cache, failure records, PDF downloads, or the browser.

Network calls go to a local server; the browser is replaced by a stub that
returns a scripted capture.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging

import pymupdf
from PIL import Image

import batch_answer_cache as crawler
from local_site import LocalSite, Route
from mind2web2.api_tools.tool_pdf import PDFParser
from mind2web2.eval_toolkit import Verifier
from mind2web2.utils.cache_filesys import CacheFileSys
from mind2web2.utils.page_info_retrieval import Capture
from mind2web2.verification_tree import VerificationNode

LOGGER = logging.getLogger("test")


def png_b64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 128, 255)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


class StubBrowser:
    """Returns ``result`` for every capture and records the URLs it was asked for."""

    def __init__(self, result: Capture):
        self.result = result
        self.urls: list[str] = []

    async def capture(self, url, logger):
        self.urls.append(url)
        return self.result


class NoJudge:
    async def async_response(self, **kwargs):
        raise AssertionError("the judge must not be called")


def verifier(cache: CacheFileSys, browser: StubBrowser) -> Verifier:
    return Verifier(client=NoJudge(), task_description="task", answer="answer", global_cache=cache,
                    global_semaphore=asyncio.Semaphore(2), logger=LOGGER, browser_manager=browser)


PAGES = {
    "/article": Route(body=b"<html><body>An article</body></html>"),
    "/paper": Route(body=pdf_bytes("Findings of the paper"), headers={"Content-Type": "application/pdf"}),
    "/landing.pdf": Route(body=b"<html><body>Log in to read this paper</body></html>"),
}


# ------------------------------------------------------------------ evaluation

def test_evaluation_does_not_capture_a_url_whose_capture_failed(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.record_failure("https://example.com/blocked", "blocked: HTTP 403", blocked=True)
    browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="must not be used"))
    v = verifier(cache, browser)
    node = VerificationNode(id="source", desc="The page supports the claim")

    assert asyncio.run(v.get_page_info("https://example.com/blocked/")) == (None, None)
    assert asyncio.run(v.verify_by_url("claim", "https://example.com/blocked", node=node)) is False
    assert (node.score, node.status) == (0.0, "failed")
    assert browser.urls == []


def test_evaluation_records_a_failed_live_capture_and_does_not_retry_it(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    browser = StubBrowser(Capture(error="navigation failed: net::ERR_CONNECTION_REFUSED"))
    with LocalSite(PAGES) as site:
        url = site.url("/article")
        v = verifier(cache, browser)
        assert asyncio.run(v.get_page_info(url)) == (None, None)
        assert asyncio.run(v.get_page_info(url)) == (None, None)
    assert browser.urls == [url]
    assert CacheFileSys(str(tmp_path)).failure(url)["reason"] == "navigation failed: net::ERR_CONNECTION_REFUSED"


def test_evaluation_stores_live_captures_and_pdf_downloads(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="Live page", status=200))
    with LocalSite(PAGES) as site:
        v = verifier(cache, browser)
        shots, text = asyncio.run(v.get_page_info(site.url("/article")))
        _, pdf_text = asyncio.run(v.get_page_info(site.url("/paper")))
        asyncio.run(v.get_page_info(site.url("/landing.pdf")))
        urls = {path: site.url(path) for path in PAGES}

    assert text == "Live page" and base64.b64decode(shots[0])[:2] == b"\xff\xd8"
    assert "Findings of the paper" in pdf_text
    reopened = CacheFileSys(str(tmp_path))
    assert {path: reopened.has(url) for path, url in urls.items()} == {
        "/article": "web", "/paper": "pdf", "/landing.pdf": "web"}
    assert browser.urls == [urls["/article"], urls["/landing.pdf"]]  # the fake PDF was loaded in the browser


# ------------------------------------------------------------------ crawler

def crawl(cache: CacheFileSys, browser: StubBrowser, url: str, retry_failed: bool = False) -> str:
    return asyncio.run(crawler.crawl_one_page(url, cache, PDFParser(), browser, LOGGER, retry_failed=retry_failed))


def test_crawler_records_failures_and_retries_them_only_when_asked(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    with LocalSite(PAGES) as site:
        url = site.url("/article")
        assert crawl(cache, StubBrowser(Capture(error="blocked: HTTP 403", blocked=True, status=403)), url) == "blocked"
        record = cache.failure(url)
        assert (record["reason"], record["blocked"]) == ("blocked: HTTP 403", True)

        browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="Captured on retry"))
        assert crawl(cache, browser, url) == "skipped"
        assert browser.urls == [] and cache.has(url) is None
        assert crawl(cache, browser, url, retry_failed=True) == "stored"
        assert crawl(cache, browser, url) == "cached"
    assert browser.urls == [url]
    assert cache.get_web(url, get_screenshot=False)[0] == "Captured on retry"
    assert cache.failure(url) is None


def test_crawler_downloads_pdfs_and_loads_fake_pdfs_in_the_browser(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="Log in to read this paper"))
    with LocalSite(PAGES) as site:
        paper, landing = site.url("/paper"), site.url("/landing.pdf")
        crawl(cache, browser, paper)
        crawl(cache, browser, landing)
    assert (cache.has(paper), cache.has(landing)) == ("pdf", "web")
    assert browser.urls == [landing]


def test_crawler_retries_failures_of_the_run_once_but_not_refusals(tmp_path, monkeypatch):
    calls: dict[str, int] = {}

    class ScriptedBrowser:
        """Fails "/article" once, always refuses "/forbidden", and captures everything else."""

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def capture(self, url, logger):
            calls[url] = calls.get(url, 0) + 1
            if url.endswith("/forbidden"):
                return Capture(error="blocked: HTTP 403", blocked=True, status=403)
            if url.endswith("/article") and calls[url] == 1:
                return Capture(error="navigation failed: no response within 30s")
            return Capture(screenshot_b64=png_b64(), text=f"Page at {url}")

    monkeypatch.setattr(crawler, "BatchBrowserManager", ScriptedBrowser)
    with LocalSite(PAGES) as site:
        urls = [site.url(path) for path in ("/article", "/forbidden", "/other")]
        (tmp_path / "cache" / "agent").mkdir(parents=True)
        (tmp_path / "cache" / "agent" / "task.json").write_text(json.dumps(
            {"all_unique_urls": urls, "urls": {url: ["answer_1.md"] for url in urls}}))
        asyncio.run(crawler.process_cache("agent", "task", answers_root=tmp_path / "answers",
                                          cache_root=tmp_path / "cache", logger=LOGGER))

    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    assert [cache.has(url) for url in urls] == ["web", None, "web"]
    assert cache.failure(urls[1])["attempts"] == 1
    assert list(calls.values()) == [2, 1, 1]
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["failed_urls"] == {urls[1]: "blocked: HTTP 403"}
