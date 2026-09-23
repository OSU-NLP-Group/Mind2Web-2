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

from PIL import Image

from local_site import LocalSite, Route
from mind2web2 import cli
from mind2web2.cli import cache as cache_command
from mind2web2.crawl import discover_task_urls, extract_answer_urls, filter_url_variants
from mind2web2.utils.cache_filesys import CacheFileSys
from mind2web2.utils.page_info_retrieval import Capture

LOGGER = logging.getLogger("test")


class FakeExtractor:
    """Returns ``urls`` for every answer and counts its calls."""

    models = ("fake-model",)

    def __init__(self, urls: list[str]):
        self.urls = urls
        self.calls = 0

    async def extract(self, answer_text, logger):
        self.calls += 1
        return list(self.urls)


def write_answers(tmp_path, texts: list[str]) -> None:
    task_dir = tmp_path / "answers" / "agent" / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    for k, text in enumerate(texts, 1):
        (task_dir / f"answer_{k}.md").write_text(text)


def discover(tmp_path, extractor=None, refresh=False) -> list[str]:
    return asyncio.run(discover_task_urls("agent", "task", answers_root=tmp_path / "answers",
                                          cache_root=tmp_path / "cache", extractor=extractor,
                                          logger=LOGGER, refresh=refresh))


# ------------------------------------------------------------------ URL discovery

def test_one_spelling_is_kept_per_page():
    urls = ["http://www.example.com/a/", "https://example.com/a", "https://example.com/a?utm_source=x",
            "https://other.org/b", "http://other.org/b"]
    assert filter_url_variants(urls) == ["https://example.com/a", "https://other.org/b"]


def test_the_regex_spelling_wins_and_llm_urls_are_added():
    extractor = FakeExtractor(["https://example.com/a", "https://example.org/extra"])
    urls = asyncio.run(extract_answer_urls("See http://www.example.com/a/ for details.", extractor, LOGGER))
    assert urls == ["http://www.example.com/a/", "https://example.org/extra"]


def test_task_urls_are_merged_across_answers_and_listed_in_the_metadata_file(tmp_path):
    write_answers(tmp_path, ["https://b.org/2 and http://www.a.com/1/", "https://a.com/1 again"])
    assert discover(tmp_path) == ["https://a.com/1", "https://b.org/2"]
    meta = json.loads((tmp_path / "cache" / "agent" / "task.json").read_text())
    assert meta["all_unique_urls"] == ["https://a.com/1", "https://b.org/2"]
    assert meta["urls"] == {"https://a.com/1": ["answer_1.md", "answer_2.md"], "https://b.org/2": ["answer_1.md"]}
    assert sorted(meta["answer_digests"]) == ["answer_1.md", "answer_2.md"]


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
