"""What a judge request contains for a page, and what happens when the judge rejects a request.

Pages come from a ``CacheFileSys`` in a temporary directory; the judge is a
scripted fake, so no request leaves the process.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging

import pymupdf
import pytest
from PIL import Image
from pydantic import BaseModel

from mind2web2 import eval_toolkit
from mind2web2.eval_toolkit import (
    TRUNCATION_MARKER, BinaryEvalResult, EvaluatorConfig, Extractor, Screenshots, Verifier, empty_extraction,
    split_screenshot, truncate_to_tokens,
)
from mind2web2.llm_client import ContextLengthError, JudgeContentError
from mind2web2.metrics import collect_records, compute_metrics, format_report
from mind2web2.submission import TaskInfo
from mind2web2.utils.cache_filesys import CacheFileSys
from mind2web2.verification_tree import VerificationNode

from test_judge_failures import JudgedClient, evaluate, saved_results, toy_script

LOGGER = logging.getLogger("test")
URL = "https://example.com/page"


class CharEncoding:
    """Stands in for the tokenizer: one token per character."""

    def encode(self, text, disallowed_special=()):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)


class FourCharEncoding(CharEncoding):
    """Stands in for the tokenizer: one token per four characters, so a text has fewer tokens than bytes."""

    def encode(self, text, disallowed_special=()):
        return [text[i:i + 4] for i in range(0, len(text), 4)]


class ScriptedJudge:
    """Answers with the given outcomes in order, repeating the last one, and records every request's messages.

    An outcome is an exception to raise, a bool for a verification verdict, or
    a model instance for an extraction.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[list] = []

    async def async_response(self, count_token: bool = False, **kwargs):
        self.requests.append(kwargs["messages"])
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bool):
            outcome = BinaryEvalResult(reasoning="scripted", result=outcome)
        return (outcome, {"input_tokens": 1, "output_tokens": 1}) if count_token else outcome

    def page_texts(self) -> list[str]:
        """Each request's messages as JSON text, to search for the page text it contained."""
        return [json.dumps(messages, default=str) for messages in self.requests]


def page_cache(tmp_path, text: str = "Page text", screenshot: bytes | None = None) -> CacheFileSys:
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(URL, text, screenshot or png(40, 30))
    return cache


def png(width: int, height: int, color=(220, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def evaluator(cls, cache, judge, config: EvaluatorConfig | None = None):
    return cls(client=judge, task_description="task", answer="answer", global_cache=cache,
               global_semaphore=asyncio.Semaphore(2), logger=LOGGER, config=config)


# ------------------------------------------------------------------ screenshots

def striped_png(width: int, height: int) -> bytes:
    """A red PNG whose bottom 500 rows are green, with a black band at rows 1950-1959."""
    image = Image.new("RGB", (width, height), (220, 0, 0))
    image.paste((0, 160, 0), (0, height - 500, width, height))
    image.paste((0, 0, 0), (0, 1950, width, 1960))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_long_screenshot_reaches_the_judge_as_overlapping_parts_of_at_most_1100_by_2000_pixels(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    sizes = {"tiny": (1000, 1500), "short": (1000, 3000), "wide": (1400, 9000), "long": (1000, 12000)}
    for name, size in sizes.items():
        cache.put_web(f"https://example.com/{name}", "text", striped_png(*size))
    doc = pymupdf.open()
    doc.new_page(width=600, height=4000)  # rendered at 144 DPI: 1200 x 8000 pixels
    cache.put_pdf("https://example.com/poster.pdf", doc.tobytes())
    doc.close()
    v = evaluator(Verifier, cache, ScriptedJudge(AssertionError("the judge must not be called")))

    judged = {}
    for name in [*sizes, "poster.pdf"]:
        shots, _ = asyncio.run(v.get_page_info(f"https://example.com/{name}"))
        judged[name] = (shots.split, [Image.open(io.BytesIO(base64.b64decode(s))) for s in shots])

    assert {name: (split, [im.size for im in parts]) for name, (split, parts) in judged.items()} == {
        "tiny": (False, [(1000, 1500)]),
        "short": (True, [(1000, 2000), (1000, 1100)]),                       # rows 0-2000, 1900-3000
        "wide": (True, [(1100, 2000)] * 3 + [(1100, 1371)]),                 # scaled to 1100 x 7071
        "long": (True, [(1000, 2000)] * 5),                                  # rows 0-9600 of 12000
        "poster.pdf": (True, [(1100, 2000)] * 3 + [(1100, 1633)]),           # scaled to 1100 x 7333
    }
    short = judged["short"][1]
    # The overlap: the black band at rows 1950-1959 is in both parts
    assert short[0].getpixel((500, 1955))[0] < 60 and short[1].getpixel((500, 55))[0] < 60
    # The short screenshot is complete (its green bottom arrives); the long one loses rows below 9600
    assert short[1].getpixel((500, 1099))[1] > 100
    assert judged["long"][1][4].getpixel((500, 1999))[1] < 60


def test_split_screenshot_makes_at_most_max_parts():
    image = Image.new("RGB", (10, 100))
    assert [p.size[1] for p in split_screenshot(image, 40, 10, 5)] == [40, 40, 40]   # 0-40, 30-70, 60-100
    assert [p.size[1] for p in split_screenshot(image, 40, 10, 2)] == [40, 40]
    assert split_screenshot(image, 100, 10, 5) == [image]


def test_the_request_says_how_the_parts_of_a_split_screenshot_fit_together(tmp_path):
    v = evaluator(Verifier, page_cache(tmp_path), ScriptedJudge(True))
    whole = v._build_message_content("PROMPT", ["img"])
    parts = Screenshots(["img1", "img2"])
    parts.split = True
    split = v._build_message_content("PROMPT", parts)

    assert whole[0]["text"] == "PROMPT\n\nBelow are rendered page screenshots to provide non-textual context:"
    assert split[0]["text"].startswith("PROMPT\n\nBelow are rendered page screenshots to provide non-textual context. "
                                       "A screenshot taller than 2000 pixels is split into consecutive parts")
    assert [part["type"] for part in split] == ["text", "image_url", "image_url"]


# ------------------------------------------------------------------ page text

def test_a_text_within_the_budget_in_bytes_is_not_tokenized(monkeypatch):
    def no_tokenizer():
        raise AssertionError("the tokenizer must not be loaded")
    monkeypatch.setattr(eval_toolkit, "_text_encoding", no_tokenizer)
    assert truncate_to_tokens("short page\n\ntext", 100) == "short page\n\ntext"


def test_page_text_is_cut_off_by_tokens_and_keeps_its_line_breaks():
    try:
        encoding = eval_toolkit._text_encoding()
    except Exception as exc:  # the encoding is downloaded on first use
        pytest.skip(f"tokenizer unavailable: {exc}")
    text = "第一行内容，包含一些中文。\n" * 20_000
    cut = truncate_to_tokens(text, 1_000)

    assert cut.endswith(TRUNCATION_MARKER)
    kept = cut[: -len(TRUNCATION_MARKER)]
    assert text.startswith(kept) and "\n" in kept
    assert 990 <= len(encoding.encode(kept)) <= 1_000


def test_get_page_info_cuts_the_page_text_to_the_token_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_toolkit, "_text_encoding", CharEncoding)
    config = EvaluatorConfig()
    config.max_text_tokens = 100
    v = evaluator(Verifier, page_cache(tmp_path, text="x" * 500), ScriptedJudge(True), config)

    _, text = asyncio.run(v.get_page_info(URL))
    assert text == "x" * 100 + TRUNCATION_MARKER


# ------------------------------------------------------------------ rejected requests

def test_a_rejected_verification_fails_its_check_and_is_recorded(tmp_path):
    judge = ScriptedJudge(JudgeContentError("refused"), True)
    v = evaluator(Verifier, page_cache(tmp_path), judge)
    node = VerificationNode(id="claim", desc="claim")

    assert asyncio.run(v.verify_by_url("claim", URL, node, majority_vote=False)) is False
    assert (node.score, node.status) == (0.0, "failed")
    [rejection] = v.usage.rejections
    assert (rejection["url"], rejection["reason"]) == (URL, "refused")
    assert v.usage.failed_requests == 0
    # The answer's later requests are still sent
    assert asyncio.run(v.verify_by_url("claim", URL, None, majority_vote=False)) is True
    assert len(judge.requests) == 2


def test_under_majority_voting_a_rejected_trial_is_a_failed_vote(tmp_path):
    judge = ScriptedJudge(JudgeContentError("refused"), True, True)
    v = evaluator(Verifier, page_cache(tmp_path), judge)
    assert asyncio.run(v.verify_by_url("claim", URL, None, majority_vote=True, num_trials=3)) is True
    assert len(judge.requests) == 3 and len(v.usage.rejections) == 1

    judge = ScriptedJudge(JudgeContentError("refused"))
    v = evaluator(Verifier, page_cache(tmp_path), judge)
    assert asyncio.run(v.verify_by_url("claim", URL, None, majority_vote=True, num_trials=3)) is False
    assert len(judge.requests) == 2 and len(v.usage.rejections) == 2  # two failed votes decide


def test_a_request_too_long_for_the_judge_is_sent_again_with_half_the_page_text(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_toolkit, "_text_encoding", CharEncoding)
    judge = ScriptedJudge(ContextLengthError("too long"), True)
    v = evaluator(Verifier, page_cache(tmp_path, text="y" * 4_000), judge)

    assert asyncio.run(v.verify_by_url("claim", URL, None, majority_vote=False)) is True
    first, second = judge.page_texts()
    assert "y" * 4_000 in first
    assert "y" * 2_000 + json.dumps(TRUNCATION_MARKER)[1:-1] in second and "y" * 2_001 not in second
    assert v.usage.rejections == []


def test_each_shorter_attempt_halves_the_tokens_of_the_page_text(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_toolkit, "_text_encoding", FourCharEncoding)
    judge = ScriptedJudge(ContextLengthError("too long"), True)
    v = evaluator(Verifier, page_cache(tmp_path, text="y" * 4_000), judge)  # 1,000 tokens

    assert asyncio.run(v.verify_by_url("claim", URL, None, majority_vote=False)) is True
    first, second = judge.page_texts()
    assert "y" * 4_000 in first
    assert "y" * 2_000 + json.dumps(TRUNCATION_MARKER)[1:-1] in second and "y" * 2_001 not in second  # 500 tokens


def test_a_request_still_too_long_after_two_shorter_attempts_fails_its_check(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_toolkit, "_text_encoding", CharEncoding)
    judge = ScriptedJudge(ContextLengthError("too long"))
    v = evaluator(Verifier, page_cache(tmp_path, text="y" * 4_000), judge)
    node = VerificationNode(id="claim", desc="claim")

    assert asyncio.run(v.verify_by_url("claim", URL, node, majority_vote=True, num_trials=3)) is False
    assert len(judge.requests) == 3  # the full text, then 2,000 and 1,000 characters
    assert node.status == "failed"
    [rejection] = v.usage.rejections
    assert (rejection["url"], rejection["reason"]) == (URL, "too long")
    assert v.usage.failed_requests == 0


class Facts(BaseModel):
    name: str
    year: int | None = None


def test_a_rejected_extraction_gives_empty_values_and_is_recorded(tmp_path):
    judge = ScriptedJudge(JudgeContentError("filtered"))
    e = evaluator(Extractor, page_cache(tmp_path), judge)

    assert asyncio.run(e.extract_from_url("Extract the facts.", URL, Facts)) == empty_extraction(Facts)
    judge.outcomes = [ContextLengthError("too long")]
    assert asyncio.run(e.simple_extract("Extract the facts.", Facts)) == empty_extraction(Facts)
    assert [(r["url"], r["reason"]) for r in e.usage.rejections] == [(URL, "filtered"), (None, "too long")]
    assert e.usage.failed_requests == 0


class RejectingJudgeClient(JudgedClient):
    """A judge that rejects every verification request and answers the others."""

    async def async_response(self, count_token: bool = False, **kwargs):
        if kwargs.get("response_format") is BinaryEvalResult:
            self.calls += 1
            raise JudgeContentError("the request was flagged by the content filter")
        return await super().async_response(count_token=count_token, **kwargs)


def test_an_answer_with_rejected_requests_is_scored_and_listed_in_the_metrics(tmp_path, monkeypatch):
    evaluated, results_root = evaluate(tmp_path, monkeypatch, RejectingJudgeClient(),
                                       toy_script(None, swallow_errors=False))
    [saved] = saved_results(results_root)
    result = json.loads(saved.read_text())
    assert result["final_score"] == evaluated[0]["final_score"] == 0.0
    assert result["judge_usage"]["failed_requests"] == 0
    assert [r["reason"] for r in result["judge_usage"]["rejections"]] == [
        "the request was flagged by the content filter"] * 2  # two failed votes of three

    records, runs = collect_records("agent", ["toy"], tmp_path / "answers", results_root)
    metrics = compute_metrics(records, [TaskInfo("toy", "A")], runs)
    assert metrics["rejected_requests"] == [{"task_id": "toy", "run": 1, "requests": 2}]
    assert metrics["missing_results"] == []
    assert "toy run 1: 2 requests" in format_report(metrics)
