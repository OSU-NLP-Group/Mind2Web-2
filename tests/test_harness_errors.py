"""Failures of the evaluation environment, and settings outside the eval script, never change a saved score.

A tokenizer that cannot be loaded or a browser that cannot be launched says
nothing about the answer, so the answer is left unscored instead of failing a
check.  The pinned evaluation date is part of what a saved result was scored
under.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import tiktoken

from mind2web2 import eval_toolkit, results
from mind2web2.eval_toolkit import HarnessError, Verifier, browser_for_run, shared_browser
from mind2web2.llm_client import JudgeUsage

from offline_eval import SyntheticCache
from test_judge_failures import TWO_SOURCES, JudgedClient, answer_log, evaluate, saved_results, toy_script
from test_judge_input_limits import ScriptedJudge, evaluator, page_cache

LONG_TEXT = "word " * 30_000  # 150,000 bytes: longer than the budget in bytes, so it is tokenized


@pytest.fixture
def no_tokenizer(monkeypatch):
    """The tokenizer's download fails, as on a host that cannot reach it."""
    def fail(name):
        raise OSError("the encoding could not be downloaded")
    eval_toolkit._text_encoding.cache_clear()
    monkeypatch.setattr(tiktoken, "get_encoding", fail)
    monkeypatch.setattr(SyntheticCache, "get_web", lambda self, url, get_screenshot=True: (LONG_TEXT, b""))
    yield
    eval_toolkit._text_encoding.cache_clear()


@pytest.mark.parametrize("swallow_errors", [False, True], ids=["propagated", "caught_by_the_script"])
def test_a_tokenizer_that_cannot_be_loaded_leaves_the_answer_unscored(tmp_path, monkeypatch, no_tokenizer,
                                                                        swallow_errors):
    evaluated, results_root = evaluate(tmp_path, monkeypatch, JudgedClient(), toy_script(TWO_SOURCES, swallow_errors))
    assert evaluated == []
    assert saved_results(results_root) == []
    log = answer_log(results_root)
    assert "The o200k_base tokenizer cannot be loaded" in log
    if swallow_errors:  # the script carried on, so eval_runner's check of the result caught it
        assert "page load(s) failed because of the evaluation environment" in log


class UnlaunchableBrowser:
    """A browser whose executable is missing: every capture fails before a page is opened."""

    instances: list["UnlaunchableBrowser"] = []

    def __init__(self, **kwargs):
        self.captures = 0
        self.stopped = False
        UnlaunchableBrowser.instances.append(self)

    async def capture(self, url, logger, wait_until="load"):
        self.captures += 1
        raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium")

    async def stop(self):
        self.stopped = True


async def _not_a_pdf(url):
    return False


def test_a_browser_that_cannot_be_launched_leaves_the_answer_unscored(tmp_path, monkeypatch):
    UnlaunchableBrowser.instances.clear()
    monkeypatch.setattr(eval_toolkit, "BatchBrowserManager", UnlaunchableBrowser)
    monkeypatch.setattr(eval_toolkit, "is_pdf", _not_a_pdf)
    monkeypatch.setattr(SyntheticCache, "has", lambda self, url: None)  # nothing cached: captured live
    monkeypatch.setattr(SyntheticCache, "failure", lambda self, url: None, raising=False)
    evaluated, results_root = evaluate(tmp_path, monkeypatch, JudgedClient(), toy_script(TWO_SOURCES, False))
    assert evaluated == []
    assert saved_results(results_root) == []
    assert "The browser failed while capturing" in answer_log(results_root)
    # evaluate_task shared one browser for the task and stopped it
    [browser] = UnlaunchableBrowser.instances
    assert browser.captures >= 1 and browser.stopped


def test_after_one_harness_failure_the_answer_loads_no_more_pages(tmp_path, monkeypatch):
    browser = UnlaunchableBrowser()
    monkeypatch.setattr(eval_toolkit, "is_pdf", _not_a_pdf)
    verifier = evaluator(Verifier, eval_toolkit.CacheFileSys(str(tmp_path)), ScriptedJudge(True))
    verifier.browser_manager = browser
    for _ in range(2):
        with pytest.raises(HarnessError):
            asyncio.run(verifier.get_page_info("https://example.com/missing"))
    assert browser.captures == 1
    assert verifier.usage.harness_failures == 1


def test_browser_for_run_uses_the_shared_browser_when_there_is_one(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("browser_for_run created a browser although one is shared")
    monkeypatch.setattr(eval_toolkit, "BatchBrowserManager", unexpected)
    shared = UnlaunchableBrowser()

    async def run():
        with shared_browser(shared):
            async with browser_for_run():
                return eval_toolkit._shared_browser.get()

    assert asyncio.run(run()) is shared
    assert not shared.stopped  # its creator stops it


def test_a_url_that_cannot_be_parsed_is_an_unavailable_page(tmp_path):
    judge = ScriptedJudge(True)
    verifier = evaluator(Verifier, page_cache(tmp_path), judge)
    assert asyncio.run(verifier.get_page_info("https://example.com]/page")) == (None, None)
    assert verifier.usage == JudgeUsage()  # neither a harness failure nor a judge request


def test_a_result_is_reused_only_under_the_same_pinned_evaluation_date(tmp_path, monkeypatch):
    script = toy_script(None, swallow_errors=False)
    monkeypatch.delenv(results.EVAL_DATE_VARIABLE, raising=False)
    evaluate(tmp_path, monkeypatch, JudgedClient(), script)
    [first] = saved_results(tmp_path / "eval_results")
    assert json.loads(first.read_text())["eval_date"] is None

    monkeypatch.setenv(results.EVAL_DATE_VARIABLE, "2025-05-01")
    client = JudgedClient()
    evaluate(tmp_path, monkeypatch, client, script)
    assert client.calls > 0  # evaluated again
    [second] = saved_results(tmp_path / "eval_results")
    assert json.loads(second.read_text())["eval_date"] == "2025-05-01"

    client = JudgedClient()
    evaluate(tmp_path, monkeypatch, client, script)
    assert client.calls == 0  # reused
