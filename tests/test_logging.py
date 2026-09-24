"""Logs of an evaluation: their two formats, which log a record goes to, and what ``mind2web2 evaluate`` writes.

The command's tests run it on toy eval scripts with a fake judge, as in ``test_evaluate``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys

import pytest

from mind2web2 import Evaluator, results
from mind2web2.eval_toolkit import Verifier
from mind2web2.llm_client import ContextLengthError
from mind2web2.cli import evaluate as evaluate_command
from mind2web2.eval_toolkit import shared_browser
from mind2web2.utils.logging_setup import (MAX_DETAIL_CHARS, JsonLinesFormatter, ReadableFormatter,
                                           cleanup_logger, close_run_logging, configure_run_logging,
                                           create_logger, logging_to)

from offline_eval import FakeLLMClient, SyntheticCache
from test_evaluate import BrokenJudge, RecordingBrowser, run_evaluate  # noqa: F401  (run_evaluate is a fixture)
from test_judge_input_limits import LOGGER, ScriptedJudge, evaluator, page_cache


def record(message: str, level: int = logging.INFO, exc_info=None, **extra) -> logging.LogRecord:
    rec = logging.LogRecord("mind2web2.test", level, __file__, 1, message, None, exc_info)
    rec.__dict__.update(extra)
    return rec


def raised() -> tuple:
    try:
        raise ValueError("broken page")
    except ValueError:
        return sys.exc_info()


# ------------------------------------------------------------------ formats

def test_the_readable_format_indents_the_detail_fields_and_the_traceback_below_the_line():
    text = ReadableFormatter().format(record("Check c1 passed", reasoning="It matches.\nSecond line.",
                                             claim="X is Y.", op_id="c1_1234", votes=[True, True],
                                             exc_info=raised()))
    lines = text.splitlines()
    assert lines[0].endswith(" INFO     Check c1 passed")
    # The detail fields follow in a fixed order, continuation lines indented under their value
    assert lines[1].strip() == "claim: X is Y."
    assert lines[2].strip() == "reasoning: It matches."
    assert lines[3].strip() == "Second line." and lines[3].startswith(" " * 25)
    assert "Traceback (most recent call last):" in text and "ValueError: broken page" in text
    # Other fields are left to the JSON Lines file
    assert "c1_1234" not in text and "votes" not in text


def test_the_readable_format_cuts_long_detail_values():
    text = ReadableFormatter().format(record("Extracted Facts from the answer", result={"text": "x" * 5000}))
    assert text.endswith(" …") and len(text.splitlines()[1].strip()) < MAX_DETAIL_CHARS + 20


def test_a_json_line_holds_every_field_and_the_traceback():
    entry = json.loads(JsonLinesFormatter().format(record("Check c1 failed", level=logging.ERROR, exc_info=raised(),
                                                          node_id="c1", votes=[False], console=False)))
    assert (entry["level"], entry["logger"], entry["message"]) == ("ERROR", "mind2web2.test", "Check c1 failed")
    assert (entry["node_id"], entry["votes"]) == ("c1", [False])
    assert "ValueError: broken page" in entry["traceback"]
    assert "T" in entry["time"] and "console" not in entry


# ------------------------------------------------------------------ routing

def read(stem, suffix: str) -> str:
    return (stem.parent / f"{stem.name}{suffix}").read_text(encoding="utf-8")


def test_package_records_during_an_answer_go_to_its_log_and_the_others_to_the_run_log(tmp_path):
    module_log = logging.getLogger("mind2web2.llm_client.base_client")
    answer_logger, timestamp = create_logger("answer_1.md", str(tmp_path / "answer"), enable_console=False)
    run_log = configure_run_logging(tmp_path / "run", "evaluate", console=False)
    try:
        module_log.warning("Before any answer")
        with logging_to(answer_logger):
            module_log.warning("Retrying the judge request")
            module_log.debug("Request details")
        module_log.info("After the answer")
    finally:
        close_run_logging()
        cleanup_logger(answer_logger)

    run_text = read(run_log, ".log")
    assert "Before any answer" in run_text and "After the answer" in run_text
    assert "Retrying" not in run_text and "Retrying" not in read(run_log, ".jsonl")
    answer_stem = tmp_path / "answer" / f"{timestamp}_answer_1.md"
    assert "Retrying the judge request" in read(answer_stem, ".log")
    # DEBUG goes only to the JSON Lines file
    assert "Request details" not in read(answer_stem, ".log")
    assert "Request details" in read(answer_stem, ".jsonl")


def test_warnings_of_other_loggers_are_routed_like_the_packages(tmp_path, capsys):
    """The OpenAI SDK, the browser library, and an eval script's own module logger are outside the package."""
    other = logging.getLogger("openai._base_client")
    answer_logger, timestamp = create_logger("answer_1.md", str(tmp_path / "answer"), enable_console=False)
    run_log = configure_run_logging(tmp_path / "run", "evaluate")
    try:
        other.warning("Outside any answer")
        other.info("Too detailed for the run log")
        with logging_to(answer_logger):
            other.warning("During the answer")
    finally:
        close_run_logging()
        cleanup_logger(answer_logger)
    other.warning("After the run")  # to the root logger's handlers as usual

    run_text = read(run_log, ".log")
    assert "Outside any answer" in run_text and "During the answer" not in run_text
    assert "Too detailed" not in read(run_log, ".jsonl")
    assert "During the answer" in read(tmp_path / "answer" / f"{timestamp}_answer_1.md", ".log")
    err = capsys.readouterr().err
    assert "Outside any answer" in err and "During the answer" not in err


def test_the_console_leaves_out_records_marked_for_the_files_only(tmp_path, capsys):
    run_log = configure_run_logging(tmp_path, "evaluate")
    try:
        logging.getLogger("mind2web2.cli").info("Printed by the command itself", extra={"console": False})
        logging.getLogger("mind2web2.cli").error("A task failed")
    finally:
        close_run_logging()
    err = capsys.readouterr().err
    assert "error: A task failed" in err and "Printed by the command" not in err
    assert "Printed by the command itself" in read(run_log, ".log")


def test_closing_the_run_log_lets_the_package_logger_propagate_again(tmp_path):
    package, root = logging.getLogger("mind2web2"), logging.getLogger()
    before = (package.level, package.propagate, list(package.handlers), list(root.handlers))
    configure_run_logging(tmp_path, "evaluate", console=False)
    assert package.propagate is False
    close_run_logging()
    assert (package.level, package.propagate, list(package.handlers), list(root.handlers)) == before


def test_answers_with_the_same_file_name_get_their_own_loggers_which_are_forgotten_when_closed(tmp_path):
    first, _ = create_logger("answer_1.md", str(tmp_path / "t1"), enable_console=False)
    second, _ = create_logger("answer_1.md", str(tmp_path / "t2"), enable_console=False)
    assert first is not second and first.handlers and second.handlers
    for logger in (first, second):
        cleanup_logger(logger)
    assert first.name not in logging.Logger.manager.loggerDict
    assert second.name not in logging.Logger.manager.loggerDict


# ------------------------------------------------------------------ checks

class RejectingEverything(FakeLLMClient):
    def _verdict(self, text: str) -> bool:
        return False


@pytest.mark.parametrize("client, outcome", [
    (FakeLLMClient("all_true"), r"Check sources passed: https://s\.example/\d supports the claim \(1 of 2 sources checked\)"),
    (RejectingEverything("all_true"), r"Check sources failed: none of the 2 sources supports the claim"),
])
def test_a_multi_url_check_logs_each_source_under_its_node_and_then_the_node_outcome(client, outcome, caplog):
    async def verify():
        evaluator = Evaluator()
        root = evaluator.initialize(task_id="task", agent_name="agent", answer_name="answer_1.md", client=client,
                                    task_description="Name a source.", answer="A", global_cache=SyntheticCache(),
                                    global_semaphore=asyncio.Semaphore(4), logger=logging.getLogger("test.checks"))
        leaf = evaluator.add_leaf(id="sources", desc="A source supports the claim.", parent=root)
        with shared_browser(RecordingBrowser()):
            await evaluator.verify(claim="X is Y.", node=leaf, sources=["https://s.example/1", "https://s.example/2"])

    with caplog.at_level(logging.INFO, logger="test.checks"):
        asyncio.run(verify())
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert any(m.startswith("Check sources against https://s.example/") and "votes pass" in m for m in messages)
    assert re.fullmatch(outcome, messages[-1])
    assert all(getattr(r, "node_id", None) == "sources" for r in caplog.records if r.levelno >= logging.INFO)


def test_a_check_whose_request_stays_too_long_logs_its_outcome(tmp_path, caplog):
    verifier = evaluator(Verifier, page_cache(tmp_path), ScriptedJudge(ContextLengthError("too long")))
    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        assert asyncio.run(verifier.simple_verify("X is Y.")) is False
    [outcome] = [r for r in caplog.records if r.levelno == logging.INFO]
    assert outcome.getMessage().endswith("failed: the request was too long for the judge")
    assert (outcome.status, outcome.passed, outcome.claim) == ("failed", False, "X is Y.")


# ------------------------------------------------------------------ mind2web2 evaluate

def test_evaluate_logs_each_answer_in_the_run_log_and_each_check_in_the_answer_log(tmp_path, run_evaluate):
    assert run_evaluate("--task", "t1") == 0
    [run_log] = (tmp_path / "results" / "agent" / "logs").glob("*_evaluate.log")
    run_text = run_log.read_text()
    assert "Evaluating 1 tasks of 'agent'" in run_text
    assert "t1/answer_1.md: 1.000" in run_text and "t1/answer_2.md: 1.000" in run_text

    [answer_log] = (tmp_path / "results" / "agent" / "t1" / "answer_1" / "logs").glob("*.log")
    lines = answer_log.read_text().splitlines()
    [check] = [i for i, line in enumerate(lines) if "Check claim against https://a.example/1 passed" in line]
    assert lines[check + 1].strip() == "claim: The answer names a source."
    assert lines[check + 2].strip() == "reasoning: offline"
    assert "Final score 1.000, from" in lines[-1]

    [answer_jsonl] = answer_log.parent.glob("*.jsonl")
    entries = [json.loads(line) for line in answer_jsonl.read_text().splitlines()]
    [entry] = [e for e in entries if e["message"].startswith("Check claim against")]
    assert (entry["node_id"], entry["url"], entry["passed"]) == ("claim", "https://a.example/1", True)
    assert entry["votes"] and all(entry["votes"])


def test_evaluate_reports_an_unscored_answer_on_the_console_and_keeps_its_traceback(tmp_path, run_evaluate,
                                                                                     monkeypatch, capsys):
    monkeypatch.setattr(evaluate_command, "LLMClient", BrokenJudge)
    assert run_evaluate("--task", "t1") == 1
    err = capsys.readouterr().err
    assert "error: t1/answer_1.md: not scored (JudgeError: judge unavailable)" in err

    [run_log] = (tmp_path / "results" / "agent" / "logs").glob("*_evaluate.log")
    assert "ERROR    t1/answer_2.md: not scored (JudgeError: judge unavailable)" in run_log.read_text()
    [answer_log] = (tmp_path / "results" / "agent" / "t1" / "answer_1" / "logs").glob("*.log")
    answer_text = answer_log.read_text()
    assert "Not scored: JudgeError: judge unavailable" in answer_text
    assert "Traceback (most recent call last):" in answer_text
    assert logging.getLogger("mind2web2").propagate is True


def test_a_failure_to_prepare_one_answer_leaves_only_that_answer_unscored(tmp_path, run_evaluate, monkeypatch,
                                                                         capsys):
    supersede = results.supersede_results

    def fail_for_one_answer(result_dir):
        if result_dir.parent.name == "answer_1":
            raise PermissionError(13, "Permission denied", str(result_dir))
        supersede(result_dir)

    monkeypatch.setattr(results, "supersede_results", fail_for_one_answer)
    assert run_evaluate("--task", "t1") == 1
    err = capsys.readouterr().err
    assert "t1/answer_1.md: not scored: evaluating it raised PermissionError" in err
    [run_log] = (tmp_path / "results" / "agent" / "logs").glob("*_evaluate.log")
    assert "t1/answer_2.md: 1.000" in run_log.read_text()
