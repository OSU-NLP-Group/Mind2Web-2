"""URL discovery for the crawl, and the ``mind2web2 cache`` command.

URLs come from the regular expression and a fake LLM extractor; pages come
from a local server, and the browser is replaced by a stub.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import threading
from urllib.parse import urlsplit

import pytest
from PIL import Image

from local_site import LocalSite, Route
from mind2web2 import cli, crawl
from mind2web2.api_tools.tool_pdf import PDFParser
from mind2web2.cli import cache as cache_command
from mind2web2.crawl import TaskUrls, discover_task_urls, group_url_variants
from mind2web2.utils.cache_filesys import CacheFileSys
from mind2web2.utils.page_info_retrieval import Capture

LOGGER = logging.getLogger("test")


class FakeExtractor:
    """Returns ``urls`` for every answer, reporting the extraction as ``complete``, and counts its calls."""

    def __init__(self, urls: list[str], models=("fake-model",), complete: bool = True):
        self.urls = urls
        self.models = models
        self.complete = complete
        self.calls = 0

    async def extract(self, answer_text, logger):
        self.calls += 1
        return list(self.urls), self.complete


def write_answers(tmp_path, texts: list[str]) -> None:
    task_dir = tmp_path / "answers" / "agent" / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    for k, text in enumerate(texts, 1):
        (task_dir / f"answer_{k}.md").write_text(text)


def discover(tmp_path, extractor=None, refresh=False) -> list[str]:
    """The task's URLs, as the crawl lists them."""
    return list(asyncio.run(discover_task_urls("agent", "task", answers_root=tmp_path / "answers",
                                               cache_root=tmp_path / "cache", extractor=extractor,
                                               logger=LOGGER, refresh=refresh)).urls)


# ------------------------------------------------------------------ URL discovery

def test_spellings_of_one_page_are_grouped_in_order_of_preference():
    urls = ["http://www.example.com/a/", "https://example.com/a", "https://example.com/a?utm_source=x",
            "https://other.org/b", "http://other.org/b", "https://example.com/A", "http://www.example.com/a/"]
    assert group_url_variants(urls) == [
        ["https://example.com/a", "https://example.com/a?utm_source=x", "http://www.example.com/a/"],
        ["https://other.org/b", "http://other.org/b"],
        ["https://example.com/A"],  # letter case can select another page
    ]
    assert group_url_variants(urls, preferred={"http://www.example.com/a/"})[0][0] == "http://www.example.com/a/"


def test_a_spelling_with_an_encoded_hash_is_not_grouped_with_the_page_it_decodes_to():
    """The cache stores ``?q=C%23`` under ``?q=C#``, which a lookup of ``?q=C`` never finds, and vice versa."""
    urls = ["https://example.com/search?q=C", "https://example.com/search?q=C%23",
            "https://example.com/search?q=C%23#top", "https://example.com/search?q=C#top",
            "http://www.example.com/search?q=C%23&utm_source=x"]
    assert group_url_variants(urls) == [
        ["https://example.com/search?q=C", "https://example.com/search?q=C#top"],
        # as the cache matches raw keys: scheme, www. and UTM parameters aside
        ["https://example.com/search?q=C%23", "https://example.com/search?q=C%23#top",
         "http://www.example.com/search?q=C%23&utm_source=x"],
    ]


def test_the_regex_spelling_wins_and_llm_urls_are_added(tmp_path):
    write_answers(tmp_path, ["See http://www.example.com/a/ for details."])
    found = asyncio.run(discover_task_urls(
        "agent", "task", answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", logger=LOGGER,
        extractor=FakeExtractor(["https://example.com/a", "https://example.org/extra"])))
    assert found == TaskUrls({"http://www.example.com/a/": ["https://example.com/a"], "https://example.org/extra": []})


def test_task_urls_are_merged_across_answers_and_listed_in_the_metadata_file(tmp_path):
    write_answers(tmp_path, ["https://b.org/2 and http://www.a.com/1/", "https://a.com/1 again"])
    assert discover(tmp_path) == ["https://a.com/1", "https://b.org/2"]
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["all_unique_urls"] == ["https://a.com/1", "https://b.org/2"]
    assert meta["urls"] == {"https://a.com/1": ["answer_1.md", "answer_2.md"], "https://b.org/2": ["answer_1.md"]}
    assert sorted(meta["answer_digests"]) == ["answer_1.md", "answer_2.md"]
    assert (meta["url_models"], meta["url_extraction_complete"]) == ([], True)


def test_the_url_list_is_reused_until_the_answers_change(tmp_path):
    extractor = FakeExtractor(["https://llm.example/x"])
    write_answers(tmp_path, ["https://a.com/1"])
    assert discover(tmp_path, extractor) == ["https://a.com/1", "https://llm.example/x"]
    assert discover(tmp_path, extractor) == ["https://a.com/1", "https://llm.example/x"]
    assert extractor.calls == 1

    write_answers(tmp_path, ["https://a.com/1", "https://c.net/3"])  # a new answer file
    assert discover(tmp_path, extractor) == ["https://a.com/1", "https://c.net/3", "https://llm.example/x"]
    assert extractor.calls == 3
    write_answers(tmp_path, ["https://d.io/4"])  # an edited answer file
    assert discover(tmp_path, extractor) == ["https://c.net/3", "https://d.io/4", "https://llm.example/x"]
    assert extractor.calls == 5
    discover(tmp_path, extractor, refresh=True)
    assert extractor.calls == 7

    (tmp_path / "cache" / "agent" / "task.json").write_text('{"all_unique_urls": ["https://a.c')  # truncated
    assert discover(tmp_path, extractor) == ["https://c.net/3", "https://d.io/4", "https://llm.example/x"]


def test_the_url_list_is_extracted_again_when_incomplete_or_extracted_differently(tmp_path):
    write_answers(tmp_path, ["https://a.com/1"])
    failing = FakeExtractor(["https://llm.example/x"], complete=False)
    assert discover(tmp_path, failing) == ["https://a.com/1", "https://llm.example/x"]  # used for this crawl
    assert json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())["url_extraction_complete"] is False

    extractor = FakeExtractor(["https://llm.example/y"])
    assert discover(tmp_path, extractor) == ["https://a.com/1", "https://llm.example/y"]
    assert discover(tmp_path, extractor) == ["https://a.com/1", "https://llm.example/y"]
    assert extractor.calls == 1

    assert discover(tmp_path) == ["https://a.com/1"]  # regex only
    other_models = FakeExtractor(["https://llm.example/z"], models=("other-model",))
    assert discover(tmp_path, other_models) == ["https://a.com/1", "https://llm.example/z"]
    assert json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())["url_models"] == ["other-model"]


class PerAnswerExtractor(FakeExtractor):
    """Returns the URLs listed for each answer text."""

    def __init__(self, by_text: dict[str, list[str]]):
        super().__init__([])
        self.by_text = by_text

    async def extract(self, answer_text, logger):
        self.calls += 1
        return list(self.by_text.get(answer_text, [])), self.complete


def test_a_spelling_the_regex_found_in_any_answer_is_preferred(tmp_path):
    first, second = "Source: http://www.example.org/page/", "Source: example.org/page"
    write_answers(tmp_path, [first, second])
    discover(tmp_path, PerAnswerExtractor({second: ["https://example.org/page"]}))  # only an LLM finds it in answer 2
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["all_unique_urls"] == ["http://www.example.org/page/"]
    assert meta["urls"] == {"http://www.example.org/page/": ["answer_1.md", "answer_2.md"]}
    assert meta["url_variants"] == {"http://www.example.org/page/": ["https://example.org/page"]}


def test_a_task_without_answer_files_keeps_its_url_list(tmp_path):
    task_dir = tmp_path / "answers" / "agent" / "task"
    (task_dir / "run1").mkdir(parents=True)
    (task_dir / "answer1.md").write_text("https://a.com/1")
    (task_dir / "run1" / "answer.md").write_text("https://a.com/2")
    meta_path = tmp_path / "cache" / "agent" / "task.json"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text('{"all_unique_urls": ["https://a.com/1"]}')
    assert discover(tmp_path) == []
    assert meta_path.read_text() == '{"all_unique_urls": ["https://a.com/1"]}'


def test_a_crawl_processes_a_bounded_number_of_urls_at_a_time(tmp_path, monkeypatch):
    active = peak = 0

    async def crawl_one_page(url, cache, pdf_parser, browser, logger, retry_failed=False, variants=()):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "stored"

    monkeypatch.setattr(crawl, "crawl_one_page", crawl_one_page)
    write_answers(tmp_path, [" ".join(f"https://example.com/{i}" for i in range(20))])
    [report] = asyncio.run(crawl.cache_answers(
        "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", browser=None,
        extractor=None, logger=LOGGER, show_progress=False, max_concurrent_urls=3))
    assert (report.urls, report.outcomes["stored"], peak) == (20, 20, 3)


# ------------------------------------------------------------------ spellings of one page

class CaseSensitiveSite:
    """Stands in for ``BatchBrowserManager`` on servers with case-sensitive paths.

    Serves ``pages`` by exact URL and a captured 404 page for any other URL;
    hosts in ``unresolvable`` do not resolve, and hosts in ``refusing``
    refuse the browser.
    """

    def __init__(self, pages: dict[str, str], unresolvable: tuple[str, ...] = (), refusing: tuple[str, ...] = ()):
        self.pages = pages
        self.unresolvable = unresolvable
        self.refusing = refusing
        self.urls: list[str] = []

    async def capture(self, url, logger):
        self.urls.append(url)
        host = urlsplit(url).hostname
        if host in self.unresolvable:
            return Capture(error="navigation failed: net::ERR_NAME_NOT_RESOLVED")
        if host in self.refusing:
            return Capture(error="blocked: HTTP 403", blocked=True, status=403)
        return Capture(screenshot_b64=png_b64(), text=self.pages.get(url, "404 Not Found"))


async def not_a_pdf(url, *args, **kwargs):
    return False


def crawl_task(tmp_path, monkeypatch, browser) -> crawl.TaskCrawl:
    monkeypatch.setattr(crawl, "is_pdf", not_a_pdf)
    [report] = asyncio.run(crawl.cache_answers(
        "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", browser=browser,
        extractor=None, logger=LOGGER, show_progress=False))
    return report


def served(tmp_path, url: str) -> str:
    """What evaluation reads from the task's cache for ``url``: the page text, or why the page is unavailable."""
    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    if cache.has(url):
        return cache.get_web(url, get_screenshot=False)[0]
    failure = cache.failure(url)
    return f"unavailable: {failure['reason']}" if failure else "not cached"


REAL, BROKEN = "https://www.docs.test/Docs/Page", "https://docs.test/docs/page"  # differ in letter case


def test_spellings_that_differ_in_letter_case_are_captured_as_different_pages(tmp_path, monkeypatch):
    write_answers(tmp_path, [f"Source: {REAL}", f"Source: {BROKEN}"])
    report = crawl_task(tmp_path, monkeypatch, CaseSensitiveSite({REAL: "The documentation page"}))
    assert (report.urls, dict(report.outcomes)) == (2, {"stored": 2})
    assert (served(tmp_path, REAL), served(tmp_path, BROKEN)) == ("The documentation page", "404 Not Found")


def test_a_page_cached_under_another_letter_case_is_not_this_page(tmp_path, monkeypatch):
    site = CaseSensitiveSite({REAL: "The documentation page"})
    write_answers(tmp_path, [f"Source: {BROKEN}"])
    crawl_task(tmp_path, monkeypatch, site)
    write_answers(tmp_path, [f"Source: {BROKEN}", f"Source: {REAL}"])  # an answer added after the first crawl
    assert dict(crawl_task(tmp_path, monkeypatch, site).outcomes) == {"cached": 1, "stored": 1}
    assert (served(tmp_path, REAL), served(tmp_path, BROKEN)) == ("The documentation page", "404 Not Found")


def test_a_failure_recorded_under_another_letter_case_does_not_skip_this_page(tmp_path, monkeypatch):
    site = CaseSensitiveSite({REAL: "The documentation page"}, unresolvable=("docs.test",))
    write_answers(tmp_path, [f"Source: {BROKEN}"])
    crawl_task(tmp_path, monkeypatch, site)
    write_answers(tmp_path, [f"Source: {BROKEN}", f"Source: {REAL}"])
    assert dict(crawl_task(tmp_path, monkeypatch, site).outcomes) == {"skipped": 1, "stored": 1}
    assert served(tmp_path, REAL) == "The documentation page"


WITH_WWW, BARE = "https://www.site.test/Docs/Page", "https://site.test/Docs/Page"


def test_when_a_capture_fails_the_other_spellings_of_the_page_are_tried(tmp_path, monkeypatch):
    site = CaseSensitiveSite({WITH_WWW: "The documentation page"}, unresolvable=("site.test",))
    write_answers(tmp_path, [f"Source: {WITH_WWW}", f"Source: {BARE}"])
    report = crawl_task(tmp_path, monkeypatch, site)
    assert (report.urls, dict(report.outcomes), site.urls) == (1, {"stored": 1}, [BARE, WITH_WWW])
    assert (served(tmp_path, WITH_WWW), served(tmp_path, BARE)) == ("The documentation page",) * 2
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["url_variants"] == {BARE: [WITH_WWW]}
    assert (meta["url_types"], meta["failed_urls"]) == ({BARE: "web"}, {})


def test_a_page_none_of_whose_spellings_can_be_captured_gets_one_failure_record(tmp_path, monkeypatch):
    site = CaseSensitiveSite({}, unresolvable=("site.test",), refusing=("www.site.test",))
    write_answers(tmp_path, [f"Source: {WITH_WWW}", f"Source: {BARE}"])
    report = crawl_task(tmp_path, monkeypatch, site)
    assert (dict(report.outcomes), site.urls) == ({"failed": 1}, [BARE, WITH_WWW] * 2)  # retried once: not all refused
    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    assert list(cache.failures()) == [BARE]
    record = cache.failure(WITH_WWW)
    assert (record["reason"], record["blocked"], record["attempts"]) == (
        f"navigation failed: net::ERR_NAME_NOT_RESOLVED; also tried {WITH_WWW}: blocked: HTTP 403", False, 2)

    site = CaseSensitiveSite({}, refusing=("site.test", "www.site.test"))
    assert dict(crawl_task(tmp_path, monkeypatch, site).outcomes) == {"skipped": 1}
    monkeypatch.setattr(crawl, "is_pdf", not_a_pdf)
    [report] = asyncio.run(crawl.cache_answers(
        "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", browser=site,
        extractor=None, logger=LOGGER, show_progress=False, retry_failed=True))
    assert (dict(report.outcomes), site.urls) == ({"blocked": 1}, [BARE, WITH_WWW])  # every spelling refused
    assert CacheFileSys(str(tmp_path / "cache" / "agent" / "task")).failure(BARE)["blocked"] is True


def test_retry_failed_also_retries_failures_that_evaluation_recorded(tmp_path, monkeypatch):
    """Evaluation records failures for the URLs it captures live, which the task's URL list does not contain."""
    listed, unlisted = "https://docs.test/listed", "https://docs.test/Seen-Only-In-Evaluation"
    write_answers(tmp_path, [f"Source: {listed}"])
    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    cache.record_failure(listed, "HTTP 503")
    cache.record_failure(unlisted, "navigation failed: no response within 30s")
    site = CaseSensitiveSite({listed: "Listed page", unlisted: "Page seen in evaluation"})

    assert dict(crawl_task(tmp_path, monkeypatch, site).outcomes) == {"skipped": 1}
    [report] = asyncio.run(crawl.cache_answers(
        "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", browser=site,
        extractor=None, logger=LOGGER, show_progress=False, retry_failed=True))
    assert (dict(report.outcomes), sorted(site.urls)) == ({"stored": 2}, sorted([listed, unlisted]))
    assert served(tmp_path, unlisted) == "Page seen in evaluation"
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["url_types"] == {listed: "web"}  # the metadata file lists the answers' URLs only

    # A task whose answers cite no URL still has its failure records retried.
    write_answers(tmp_path, ["No sources."])
    other = "https://docs.test/another-page-seen-in-evaluation"
    cache.record_failure(other, "HTTP 503")
    site = CaseSensitiveSite({other: "Another page"})
    [report] = asyncio.run(crawl.cache_answers(
        "agent", ["task"], answers_root=tmp_path / "answers", cache_root=tmp_path / "cache", browser=site,
        extractor=None, logger=LOGGER, show_progress=False, retry_failed=True, refresh_urls=True))
    assert (report.urls, dict(report.outcomes), site.urls) == (0, {"stored": 1}, [other])


def test_failure_records_are_written_off_the_event_loop(tmp_path, monkeypatch):
    threads = []
    record_failure = CacheFileSys.record_failure

    def recording(self, *args, **kwargs):
        threads.append(threading.current_thread())
        return record_failure(self, *args, **kwargs)

    monkeypatch.setattr(CacheFileSys, "record_failure", recording)
    monkeypatch.setattr(crawl, "is_pdf", not_a_pdf)
    browser = CaseSensitiveSite({}, unresolvable=("down.test",))
    outcome = asyncio.run(crawl.crawl_one_page("https://down.test/a", CacheFileSys(str(tmp_path)), PDFParser(),
                                               browser, LOGGER))
    assert outcome == "failed" and len(threads) == 1 and threads[0] is not threading.main_thread()


def test_an_unreadable_cache_or_metadata_file_fails_only_its_task(tmp_path, monkeypatch):
    for task_id in ("broken_index", "broken_meta", "fine"):
        answer = tmp_path / "answers" / "agent" / task_id / "answer_1.md"
        answer.parent.mkdir(parents=True)
        answer.write_text(f"Source: https://{task_id.replace('_', '-')}.test/page")
    (tmp_path / "cache" / "agent" / "broken_index").mkdir(parents=True)
    (tmp_path / "cache" / "agent" / "broken_index" / "index.json").write_text('{"https://a.c')  # truncated

    class Browser:
        async def capture(self, url, logger):
            if "broken-meta" in url:  # another process breaks the URL list during the crawl
                (tmp_path / "cache" / "agent" / "broken_meta.json").write_text('{"all_unique')
            return Capture(screenshot_b64=png_b64(), text=f"Page at {url}")

    monkeypatch.setattr(crawl, "is_pdf", not_a_pdf)
    reports = asyncio.run(crawl.cache_answers(
        "agent", ["broken_index", "broken_meta", "fine"], answers_root=tmp_path / "answers",
        cache_root=tmp_path / "cache", browser=Browser(), extractor=None, logger=LOGGER, show_progress=False))
    by_task = {r.task_id: r for r in reports}
    assert by_task["broken_index"].error.startswith("Cannot open the cache: Cannot read")
    assert by_task["broken_meta"].error.startswith("Cannot record the crawl results in broken_meta.json")
    assert (by_task["fine"].error, dict(by_task["fine"].outcomes)) == (None, {"stored": 1})
    meta = json.loads((tmp_path / "cache" / "agent" / "fine.json").read_text())
    assert meta["url_types"] == {"https://fine.test/page": "web"}


# ------------------------------------------------------------------ the cache command

def png_b64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 128, 255)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class StubBrowser:
    """Stands in for ``BatchBrowserManager``: refuses "/forbidden", raises on "/boom", captures the rest."""

    instances: list["StubBrowser"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.urls: list[str] = []
        self.stopped = 0
        StubBrowser.instances.append(self)

    async def capture(self, url, logger):
        self.urls.append(url)
        if url.endswith("/forbidden"):
            return Capture(error="blocked: HTTP 403", blocked=True, status=403)
        if url.endswith("/boom"):
            raise RuntimeError("unexpected")
        return Capture(screenshot_b64=png_b64(), text=f"Page at {url}")

    async def stop(self):
        self.stopped += 1


def run_cache(tmp_path, *options: str) -> int:
    return cli.main(["cache", "agent", "--answers-dir", str(tmp_path / "answers"),
                     "--cache-dir", str(tmp_path / "cache"), *options])


def task_row(out: str) -> list[int]:
    """The URL count and the outcome counts (cached, skipped, stored, failed, blocked, error) of the task."""
    [row] = re.findall(r"^task +([\d ]+)$", out, re.M)
    return [int(n) for n in row.split()]


def test_cache_command_stores_pages_and_reports_outcomes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache_command, "BatchBrowserManager", StubBrowser)
    with LocalSite({"/article": Route(body=b"<html><body>An article</body></html>")}) as site:
        article, forbidden = site.url("/article"), site.url("/forbidden")
        write_answers(tmp_path, [f"See {article} and {forbidden}."])

        assert run_cache(tmp_path, "--no-llm", "--max-pages", "2") == 0
        out = capsys.readouterr().out
        assert task_row(out) == [2, 0, 0, 1, 0, 1, 0]
        assert "1 URLs could not be captured" in out
        browser = StubBrowser.instances[-1]
        assert (browser.kwargs["max_concurrent_pages"], browser.kwargs["headless"], browser.stopped) == (2, False, 1)

        assert run_cache(tmp_path, "--no-llm") == 0  # a second crawl reuses the cache and the failure record
        assert task_row(capsys.readouterr().out) == [2, 1, 1, 0, 0, 0, 0]
        assert StubBrowser.instances[-1].urls == []

    cache = CacheFileSys(str(tmp_path / "cache" / "agent" / "task"))
    assert (cache.has(article), cache.failure(forbidden)["reason"]) == ("web", "blocked: HTTP 403")


def test_cache_command_exit_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache_command, "BatchBrowserManager", StubBrowser)
    with LocalSite({}) as site:
        write_answers(tmp_path, [f"See {site.url('/boom')}."])
        assert run_cache(tmp_path, "--no-llm") == 1  # a URL raised an unexpected error
    assert task_row(capsys.readouterr().out) == [1, 0, 0, 0, 0, 0, 1]

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert run_cache(tmp_path) == 2  # no API key for the URL-extraction models
    assert "--no-llm" in capsys.readouterr().err
    assert run_cache(tmp_path, "--no-llm", "--task", "other") == 1  # no answers for the task
    assert run_cache(tmp_path, "--no-llm", "--task-list", str(tmp_path / "missing.csv")) == 1
    assert "Cannot read the task list" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--max-pages", "--attempts"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_cache_command_counts_must_be_at_least_1(option, value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["cache", "agent", option, value])
    assert exc.value.code == 2 and "must be at least 1" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "soon"])
def test_cache_command_page_timeout_must_be_positive_seconds(value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["cache", "agent", "--page-timeout", value])
    assert exc.value.code == 2 and "--page-timeout" in capsys.readouterr().err
    assert cli.build_parser().parse_args(["cache", "agent", "--page-timeout", "2.5"]).page_timeout == 2.5


def test_cache_command_skips_tasks_without_answer_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache_command, "BatchBrowserManager", StubBrowser)
    old_layout = tmp_path / "answers" / "agent" / "old_layout"
    (old_layout / "run1").mkdir(parents=True)
    (old_layout / "answer1.md").write_text("https://a.example/1")
    (old_layout / "run1" / "answer.md").write_text("https://a.example/2")
    meta_path = tmp_path / "cache" / "agent" / "old_layout.json"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text('{"all_unique_urls": ["https://a.example/1"]}')

    assert run_cache(tmp_path, "--no-llm") == 1  # no selected task has answers
    assert "No answer_<k>.md files found" in capsys.readouterr().err
    with LocalSite({"/article": Route(body=b"<html><body>An article</body></html>")}) as site:
        write_answers(tmp_path, [f"See {site.url('/article')}."])
        assert run_cache(tmp_path, "--no-llm") == 0
    out = capsys.readouterr().out
    assert "Skipping 1 tasks that have no answer_<k>.md files" in out and "\nold_layout " not in out
    assert task_row(out) == [1, 0, 0, 1, 0, 0, 0]
    assert meta_path.read_text() == '{"all_unique_urls": ["https://a.example/1"]}'


def test_cache_command_reports_incomplete_url_extraction(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache_command, "BatchBrowserManager", StubBrowser)
    monkeypatch.setattr(cache_command, "LLMClient", lambda **kwargs: None)
    monkeypatch.setattr(cache_command, "LLMUrlExtractor", lambda client, models: FakeExtractor([], complete=False))
    with LocalSite({"/article": Route(body=b"<html><body>An article</body></html>")}) as site:
        write_answers(tmp_path, [f"See {site.url('/article')}."])
        assert run_cache(tmp_path) == 0
    out = capsys.readouterr().out
    assert re.search(r"^task +1 +0 +0 +1 +0 +0 +0  URL extraction incomplete$", out, re.M)
    assert "URL extraction was incomplete for 1 tasks" in out
