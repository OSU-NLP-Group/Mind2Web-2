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
from pydantic import BaseModel

from local_site import LocalSite, Route
from mind2web2.api_tools.tool_pdf import PDFParser
from mind2web2.crawl import cache_answers, crawl_one_page
from mind2web2.eval_toolkit import Extractor, Verifier, empty_extraction
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
REDIRECTING_PAGES = PAGES | {"/paper-link.pdf": Route(302, headers={"Location": "/paper"})}


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


class Laureates(BaseModel):
    university: str
    count: int
    names: list[str] = []


class Links(BaseModel):
    urls: list[str] = []


def test_extracting_from_an_unavailable_page_gives_empty_values(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.record_failure("https://example.com/laureates", "HTTP 503")
    extractor = Extractor(client=NoJudge(), task_description="task", answer="answer", global_cache=cache,
                          global_semaphore=asyncio.Semaphore(2), logger=LOGGER,
                          browser_manager=StubBrowser(Capture(error="unused")))
    result = asyncio.run(extractor.extract_from_url("List the laureates", "https://example.com/laureates", Laureates))
    assert (result.university, result.count, result.names) == (None, None, [])
    assert empty_extraction(Links) == Links()


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


def test_evaluation_stores_a_redirected_page_once_and_records_its_final_url(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    final = "https://example.org/landed"
    browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="Live page", status=200, final_url=final))
    with LocalSite(REDIRECTING_PAGES) as site:
        v = verifier(cache, browser)
        asyncio.run(v.get_page_info("https://example.com/moved"))
        _, pdf_text = asyncio.run(v.get_page_info(site.url("/paper-link.pdf")))
        link, paper = site.url("/paper-link.pdf"), site.url("/paper")

    assert "Findings of the paper" in pdf_text
    reopened = CacheFileSys(str(tmp_path))
    assert reopened.get_all_urls() == ["https://example.com/moved", link]
    assert reopened.redirects() == {final: "https://example.com/moved", paper: link}
    # Evaluating the final URL uses the stored page without capturing it again
    assert asyncio.run(verifier(reopened, browser).get_page_info(final))[1] == "Live page"
    assert browser.urls == ["https://example.com/moved"]


def test_evaluation_leaves_out_a_screenshot_that_cannot_be_decoded(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/a", "Page text", png_b64())
    (stored,) = tmp_path.glob("*.jpg")
    stored.write_bytes(b"not an image")
    v = verifier(cache, StubBrowser(Capture(error="must not be used")))

    assert asyncio.run(v.get_page_info("https://example.com/a")) == ([], "Page text")


# ------------------------------------------------------------------ crawler

def crawl(cache: CacheFileSys, browser: StubBrowser, url: str, retry_failed: bool = False) -> str:
    return asyncio.run(crawl_one_page(url, cache, PDFParser(), browser, LOGGER, retry_failed=retry_failed))


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


def test_crawler_retries_failures_of_the_run_once_but_not_refusals(tmp_path):
    calls: dict[str, int] = {}

    class ScriptedBrowser:
        """Fails "/article" once, always refuses "/forbidden", and captures everything else."""

        async def capture(self, url, logger):
            calls[url] = calls.get(url, 0) + 1
            if url.endswith("/forbidden"):
                return Capture(error="blocked: HTTP 403", blocked=True, status=403)
            if url.endswith("/article") and calls[url] == 1:
                return Capture(error="navigation failed: no response within 30s")
            return Capture(screenshot_b64=png_b64(), text=f"Page at {url}")

    with LocalSite(PAGES) as site:
        urls = [site.url(path) for path in ("/article", "/forbidden", "/other")]
        answer_dir = tmp_path / "answers" / "agent" / "task"
        answer_dir.mkdir(parents=True)
        (answer_dir / "answer_1.md").write_text("\n".join(urls))
        [report] = asyncio.run(cache_answers(
            "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache",
            browser=ScriptedBrowser(), extractor=None, logger=LOGGER, show_progress=False))

    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    assert [cache.has(url) for url in urls] == ["web", None, "web"]
    assert cache.failure(urls[1])["attempts"] == 1
    assert calls == {urls[0]: 2, urls[1]: 1, urls[2]: 1}
    assert (report.urls, dict(report.outcomes)) == (3, {"stored": 2, "blocked": 1})
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["failed_urls"] == {urls[1]: "blocked: HTTP 403"}


# ------------------------------------------------------------------ evaluation and crawler

def test_cache_writes_run_outside_the_event_loop(tmp_path, monkeypatch):
    """Every cache write takes a lock and fsyncs, so it runs in a worker thread,
    not on the event loop, where it would stall the captures in progress."""
    on_event_loop = []
    index_lock = CacheFileSys._index_lock

    def recording_index_lock(self):
        try:
            asyncio.get_running_loop()
            on_event_loop.append(True)
        except RuntimeError:  # no event loop runs in this thread
            on_event_loop.append(False)
        return index_lock(self)

    monkeypatch.setattr(CacheFileSys, "_index_lock", recording_index_lock)
    cache = CacheFileSys(str(tmp_path))
    failing = StubBrowser(Capture(error="HTTP 503", status=503))
    capturing = StubBrowser(Capture(screenshot_b64=png_b64(), text="Live page", status=200))
    with LocalSite(PAGES) as site:
        asyncio.run(verifier(cache, failing).get_page_info(site.url("/article")))
        asyncio.run(verifier(cache, capturing).get_page_info(site.url("/landing.pdf")))
        asyncio.run(verifier(cache, capturing).get_page_info(site.url("/paper")))
        assert crawl(cache, failing, site.url("/article?crawled")) == "failed"
        assert crawl(cache, capturing, site.url("/article?captured")) == "stored"
    assert on_event_loop == [False] * 5


def test_the_crawler_stores_a_redirected_page_once_and_finds_it_by_its_final_url(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    final = "https://example.org/landed"
    browser = StubBrowser(Capture(screenshot_b64=png_b64(), text="Landed", status=200, final_url=final))
    assert crawl(cache, browser, "https://example.com/moved") == "stored"
    assert crawl(cache, browser, final) == "cached"  # not captured again
    with LocalSite(REDIRECTING_PAGES) as site:
        assert crawl(cache, browser, site.url("/paper-link.pdf")) == "stored"
        assert crawl(cache, browser, site.url("/paper")) == "cached"
        link, paper = site.url("/paper-link.pdf"), site.url("/paper")

    assert browser.urls == ["https://example.com/moved"]
    assert CacheFileSys(str(tmp_path)).redirects() == {final: "https://example.com/moved", paper: link}
