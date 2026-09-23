"""Results record the judge, and a judge request that fails for good leaves the answer unscored.

A small eval script is run through the real ``evaluate_task`` loop with fake
judges; webpages come from the synthetic cache of ``offline_eval``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mind2web2 import eval_runner
from mind2web2.llm_client import JudgeConfig, JudgeError
from mind2web2.metrics import collect_records, compute_metrics
from mind2web2.submission import TaskInfo

from offline_eval import FakeLLMClient, SyntheticCache

JUDGE = JudgeConfig(model="judge-x", reasoning_effort="low")
TWO_SOURCES = ["https://a.example/1", "https://b.example/2"]


class JudgedClient(FakeLLMClient):
    """A fake client with a judge configuration, like the one run_eval.py builds."""

    def __init__(self) -> None:
        super().__init__("all_true")
        self.judge = JUDGE


class BrokenJudgeClient(JudgedClient):
    """A judge whose every request fails for good."""

    async def async_response(self, count_token: bool = False, **kwargs):
        self.calls += 1
        raise JudgeError("judge unavailable")


def toy_script(sources, swallow_errors: bool) -> str:
    call = f"await evaluator.verify(claim='The answer names a source.', node=leaf, sources={sources!r})"
    body = ["    try:", f"        {call}", "    except Exception:", "        pass"] if swallow_errors else [f"    {call}"]
    return "\n".join([
        "from mind2web2 import Evaluator",
        "",
        "async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger, model='o4-mini'):",
        "    evaluator = Evaluator()",
        "    root = evaluator.initialize(",
        "        task_id='toy', agent_name=agent_name, answer_name=answer_name, client=client,",
        "        task_description='Name a source.', answer=answer, global_cache=cache,",
        "        global_semaphore=semaphore, logger=logger, default_model=model,",
        "    )",
        "    leaf = evaluator.add_leaf(id='claim', desc='The answer names a source.', parent=root)",
        *body,
        "    return evaluator.get_summary()",
        "",
    ])


def evaluate(tmp_path: Path, monkeypatch, client, script: str) -> tuple[list, Path]:
    monkeypatch.setattr(eval_runner, "CacheFileSys", lambda task_dir: SyntheticCache())
    task_dir = tmp_path / "answers" / "agent" / "toy"
    task_dir.mkdir(parents=True)
    (task_dir / "answer_1.md").write_text("Source: https://a.example/1")
    script_path = tmp_path / "toy.py"
    script_path.write_text(script)
    results_root = tmp_path / "eval_results"
    evaluated = asyncio.run(eval_runner.evaluate_task(
        client=client, task_id="toy", agent_name="agent", answer_dir=tmp_path / "answers",
        cache_dir=tmp_path / "cache", output_dir=results_root, script_path=script_path,
    ))
    return evaluated, results_root


def saved_results(results_root: Path) -> list[Path]:
    return sorted(results_root.glob("agent/toy/answer_1/results/*.json"))


def answer_log(results_root: Path) -> str:
    return "".join(p.read_text() for p in results_root.glob("agent/toy/answer_1/logs/*"))


@pytest.mark.parametrize("sources", [None, TWO_SOURCES])
def test_result_records_the_judge_and_its_usage(tmp_path, monkeypatch, sources):
    client = JudgedClient()
    evaluated, results_root = evaluate(tmp_path, monkeypatch, client, toy_script(sources, swallow_errors=False))
    [saved] = saved_results(results_root)
    result = json.loads(saved.read_text())
    assert result["final_score"] == evaluated[0]["final_score"] == 1.0
    assert result["judge"] == {"model": "judge-x", "reasoning_effort": "low", "temperature": None}
    assert result["judge_model"] == result["extract_model"] == "judge-x"
    assert result["judge_usage"]["requests"] == client.calls > 0
    assert result["judge_usage"]["failed_requests"] == 0
    assert client.models_requested == {"judge-x"}  # the script was told to use the judge model


@pytest.mark.parametrize("sources", [None, TWO_SOURCES])
def test_judge_failure_leaves_the_answer_unscored(tmp_path, monkeypatch, sources):
    evaluated, results_root = evaluate(tmp_path, monkeypatch, BrokenJudgeClient(),
                                       toy_script(sources, swallow_errors=False))
    assert evaluated == []
    assert saved_results(results_root) == []
    assert "judge unavailable" in answer_log(results_root)
    records, num_runs = collect_records("agent", ["toy"], tmp_path / "answers", results_root)
    metrics = compute_metrics(records, [TaskInfo("toy")], num_runs, "agent")
    assert metrics["missing_results"] == [{"task_id": "toy", "run": 1}]


def test_judge_failure_swallowed_by_the_script_still_leaves_the_answer_unscored(tmp_path, monkeypatch):
    client = BrokenJudgeClient()
    evaluated, results_root = evaluate(tmp_path, monkeypatch, client, toy_script(TWO_SOURCES, swallow_errors=True))
    assert client.calls > 0
    assert evaluated == []
    assert saved_results(results_root) == []
    assert "judge request(s) failed; the answer is not scored" in answer_log(results_root)
