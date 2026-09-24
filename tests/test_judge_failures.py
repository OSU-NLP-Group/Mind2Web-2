"""Results record the judge and the answer, and a judge request that fails for good leaves the answer unscored.

A small eval script is run through the real ``evaluate_task`` loop with fake
judges; webpages come from the synthetic cache of ``offline_eval``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from mind2web2 import EvaluatorConfig, eval_runner, results
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


class SecondClaimBreaksJudgeClient(JudgedClient):
    """A judge whose requests about the second claim fail for good."""

    async def async_response(self, count_token: bool = False, **kwargs):
        if "second source" in json.dumps(kwargs.get("messages", []), default=str):
            self.calls += 1
            raise JudgeError("judge unavailable")
        return await super().async_response(count_token=count_token, **kwargs)


def toy_script(sources, swallow_errors: bool, batch: bool = False) -> str:
    if batch:
        call = ("await evaluator.batch_verify([('The answer names a source.', sources, leaf, None), "
                "('The answer names a second source.', sources, second, None)])")
    else:
        call = "await evaluator.verify(claim='The answer names a source.', node=leaf, sources=sources)"
    body = (["    try:", f"        {call}", "    except Exception as exc:",
             "        logger.warning(f'The script caught {type(exc).__name__}')"]
            if swallow_errors else [f"    {call}"])
    return "\n".join([
        "from mind2web2 import Evaluator",
        "",
        "async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger, model='o4-mini'):",
        f"    sources = {sources!r}",
        "    evaluator = Evaluator()",
        "    root = evaluator.initialize(",
        "        task_id='toy', agent_name=agent_name, answer_name=answer_name, client=client,",
        "        task_description='Name a source.', answer=answer, global_cache=cache,",
        "        global_semaphore=semaphore, logger=logger, default_model=model,",
        "    )",
        "    leaf = evaluator.add_leaf(id='claim', desc='The answer names a source.', parent=root)",
        *(["    second = evaluator.add_leaf(id='second', desc='The answer names a second source.', parent=root)"]
          if batch else []),
        *body,
        "    return evaluator.get_summary()",
        "",
    ])


def evaluate(tmp_path: Path, monkeypatch, client, script: str, answer: str = "Source: https://a.example/1",
             overwrite: bool = False) -> tuple[list, Path]:
    """Evaluate ``answer`` as ``answer_1.md``; later calls reuse the same answers and results folders."""
    monkeypatch.setattr(eval_runner, "CacheFileSys", lambda task_dir: SyntheticCache())
    task_dir = tmp_path / "answers" / "agent" / "toy"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "answer_1.md").write_text(answer)
    script_path = tmp_path / "toy.py"
    script_path.write_text(script)
    results_root = tmp_path / "eval_results"
    evaluated = asyncio.run(eval_runner.evaluate_task(
        client=client, task_id="toy", agent_name="agent", answer_dir=tmp_path / "answers",
        cache_dir=tmp_path / "cache", output_dir=results_root, script_path=script_path, overwrite=overwrite,
    ))
    return evaluated, results_root


def saved_results(results_root: Path) -> list[Path]:
    return sorted(results_root.glob("agent/toy/answer_1/results/*.json"))


def superseded_results(results_root: Path) -> list[Path]:
    return sorted(results_root.glob("agent/toy/answer_1/results/superseded/*.json"))


def answer_log(results_root: Path) -> str:
    return "".join(p.read_text() for p in results_root.glob("agent/toy/answer_1/logs/*"))


@pytest.mark.parametrize("sources", [None, TWO_SOURCES])
def test_result_records_the_judge_and_its_usage(tmp_path, monkeypatch, sources):
    client = JudgedClient()
    evaluated, results_root = evaluate(tmp_path, monkeypatch, client, toy_script(sources, swallow_errors=False))
    [saved] = saved_results(results_root)
    result = json.loads(saved.read_text())
    assert result["final_score"] == evaluated[0]["final_score"] == 1.0
    assert result["answer_sha256"] == hashlib.sha256(b"Source: https://a.example/1").hexdigest()
    assert result["judge"] == {"model": "judge-x", "reasoning_effort": "low", "temperature": None}
    assert result["judge_model"] == result["extract_model"] == "judge-x"
    assert result["judge_usage"]["requests"] == client.calls > 0
    assert result["judge_usage"]["failed_requests"] == 0
    assert client.models_requested == {"judge-x"}  # the script was told to use the judge model


@pytest.mark.parametrize("sources", [None, TWO_SOURCES])
@pytest.mark.parametrize("batch", [False, True], ids=["verify", "batch_verify"])
def test_judge_failure_stops_the_script_and_leaves_the_answer_unscored(tmp_path, monkeypatch, sources, batch):
    evaluated, results_root = evaluate(tmp_path, monkeypatch, SecondClaimBreaksJudgeClient() if batch
                                       else BrokenJudgeClient(), toy_script(sources, False, batch))
    assert evaluated == []
    assert saved_results(results_root) == []
    log = answer_log(results_root)
    assert "JudgeError: judge unavailable" in log
    # The error reached eval_runner from the script, not from its check of the returned result
    assert "judge request(s) failed" not in log
    records, num_runs = collect_records("agent", ["toy"], tmp_path / "answers", results_root)
    metrics = compute_metrics(records, [TaskInfo("toy")], num_runs, "agent")
    assert metrics["missing_results"] == [{"task_id": "toy", "run": 1}]


@pytest.mark.parametrize("batch", [False, True], ids=["verify", "batch_verify"])
def test_judge_failure_swallowed_by_the_script_still_leaves_the_answer_unscored(tmp_path, monkeypatch, batch):
    client = SecondClaimBreaksJudgeClient() if batch else BrokenJudgeClient()
    evaluated, results_root = evaluate(tmp_path, monkeypatch, client, toy_script(TWO_SOURCES, True, batch))
    assert client.calls > 0
    assert evaluated == []
    assert saved_results(results_root) == []
    log = answer_log(results_root)
    assert "The script caught JudgeError" in log
    assert "judge request(s) failed; the answer is not scored" in log


def test_a_result_is_reused_only_for_the_same_answer_judge_and_script(tmp_path, monkeypatch):
    script = toy_script(None, swallow_errors=False)
    first, results_root = evaluate(tmp_path, monkeypatch, JudgedClient(), script)
    [result_file] = saved_results(results_root)
    assert json.loads(result_file.read_text())["eval_script_sha256"] == hashlib.sha256(script.encode()).hexdigest()

    client = JudgedClient()
    again, _ = evaluate(tmp_path, monkeypatch, client, script)
    assert client.calls == 0 and again == first  # same answer, judge, and script: reused

    def judged_by_other_judge() -> JudgedClient:
        client = JudgedClient()
        client.judge = JudgeConfig(model="judge-x", reasoning_effort="high")
        return client

    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, script)
    assert client.calls > 0  # only the judge changed
    assert superseded_results(results_root) == [result_file.parent / "superseded" / result_file.name]

    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, script, answer="Source: https://b.example/2")
    assert client.calls > 0  # only the answer changed
    [latest] = saved_results(results_root)
    assert json.loads(latest.read_text())["answer_sha256"] == hashlib.sha256(b"Source: https://b.example/2").hexdigest()

    revised = script + "# a revised script\n"
    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, revised, answer="Source: https://b.example/2")
    assert client.calls > 0  # only the eval script changed

    monkeypatch.setattr(EvaluatorConfig, "image_max_height", EvaluatorConfig.image_max_height + 1)
    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, revised, answer="Source: https://b.example/2")
    assert client.calls > 0  # only the default evaluator settings changed

    monkeypatch.setattr(results, "SCORING_VERSION", results.SCORING_VERSION + 1)
    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, revised, answer="Source: https://b.example/2")
    assert client.calls > 0  # only the scoring version changed

    client = judged_by_other_judge()
    evaluate(tmp_path, monkeypatch, client, revised, answer="Source: https://b.example/2", overwrite=True)
    assert client.calls > 0
    assert len(saved_results(results_root)) == 1


def test_after_a_failed_judge_request_no_further_requests_are_sent_for_the_answer(tmp_path, monkeypatch):
    script = "\n".join([
        "from mind2web2 import Evaluator",
        "",
        "async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger, model='o4-mini'):",
        "    evaluator = Evaluator()",
        "    root = evaluator.initialize(",
        "        task_id='toy', agent_name=agent_name, answer_name=answer_name, client=client,",
        "        task_description='Name two sources.', answer=answer, global_cache=cache,",
        "        global_semaphore=semaphore, logger=logger, default_model=model,",
        "    )",
        "    for i, claim in enumerate(['The answer names a source.', 'The answer names a second source.']):",
        "        leaf = evaluator.add_leaf(id=f'claim_{i}', desc=claim, parent=root)",
        "        try:",
        "            await evaluator.verify(claim=claim, node=leaf, sources=None)",
        "        except Exception as exc:",
        "            logger.warning(f'The script caught {type(exc).__name__}: {exc}')",
        "    return evaluator.get_summary()",
        "",
    ])
    client = BrokenJudgeClient()
    evaluated, results_root = evaluate(tmp_path, monkeypatch, client, script)
    assert evaluated == [] and saved_results(results_root) == []
    assert client.calls == 1  # the second verification sent no request
    # Its error names the first failure, in case it is the one that reaches the log
    assert ("An earlier judge request for this answer failed for good (JudgeError: judge unavailable)"
            in answer_log(results_root))


def test_a_failed_evaluation_does_not_leave_an_earlier_result_in_place(tmp_path, monkeypatch):
    script = toy_script(None, swallow_errors=False)
    _, results_root = evaluate(tmp_path, monkeypatch, JudgedClient(), script)
    evaluated, _ = evaluate(tmp_path, monkeypatch, BrokenJudgeClient(), script, answer="Source: https://b.example/2")
    assert evaluated == []
    assert saved_results(results_root) == [] and len(superseded_results(results_root)) == 1
    records, num_runs = collect_records("agent", ["toy"], tmp_path / "answers", results_root)
    assert compute_metrics(records, [TaskInfo("toy")], num_runs, "agent")["missing_results"] == [
        {"task_id": "toy", "run": 1}]
