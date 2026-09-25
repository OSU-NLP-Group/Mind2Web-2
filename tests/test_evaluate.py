"""Running evaluation: the eval-script version, the shared browser, and the ``mind2web2 evaluate`` command.

Eval scripts are toy scripts, the judge is a fake client, and webpages come
from the synthetic cache of ``offline_eval``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from mind2web2 import Evaluator, cli, eval_runner, eval_toolkit
from mind2web2.cli import evaluate as evaluate_command
from mind2web2.eval_runner import ScriptsNotFound, resolve_scripts_dir
from mind2web2.eval_toolkit import shared_browser
from mind2web2.llm_client import DEFAULT_JUDGE_MODEL, JudgeConfig, JudgeError

from offline_eval import FakeLLMClient, SyntheticCache

LOGGER = logging.getLogger("test")
REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ eval-script versions

def make_versions(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "task.py").write_text("")


def test_the_newest_dated_version_is_the_default(tmp_path):
    make_versions(tmp_path, "2025_07_14", "2025_10_23", "dev_set")
    (tmp_path / "2099_01_01").mkdir()  # holds no scripts
    assert resolve_scripts_dir(tmp_path) == tmp_path / "2025_10_23"
    assert resolve_scripts_dir(tmp_path, "dev_set") == tmp_path / "dev_set"


def test_a_single_undated_version_or_a_flat_directory_is_used(tmp_path):
    assert resolve_scripts_dir(REPO / "eval_scripts") == REPO / "eval_scripts" / "dev_set"
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "task.py").write_text("")
    assert resolve_scripts_dir(flat) == flat


def test_an_unresolvable_version_lists_the_available_ones(tmp_path):
    make_versions(tmp_path, "dev_set", "custom")
    with pytest.raises(ScriptsNotFound, match=r"available: custom, dev_set"):
        resolve_scripts_dir(tmp_path)
    with pytest.raises(ScriptsNotFound, match=r"'2025_07_14' .*available: custom, dev_set"):
        resolve_scripts_dir(tmp_path, "2025_07_14")
    with pytest.raises(ScriptsNotFound, match="not found"):
        resolve_scripts_dir(tmp_path / "missing")


# ------------------------------------------------------------------ shared browser

class RecordingBrowser:
    """Stands in for ``BatchBrowserManager``; no live capture happens in these tests."""

    instances: list["RecordingBrowser"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stopped = 0
        RecordingBrowser.instances.append(self)

    async def stop(self):
        self.stopped += 1


def new_evaluator(**kwargs) -> Evaluator:
    evaluator = Evaluator()
    evaluator.initialize(task_id="task", agent_name="agent", answer_name="answer_1.md",
                         client=FakeLLMClient("all_true"), task_description="Name a source.", answer="A",
                         global_cache=SyntheticCache(), global_semaphore=asyncio.Semaphore(1), logger=LOGGER,
                         **kwargs)
    return evaluator


def test_evaluators_in_a_shared_browser_block_use_it_and_leave_it_running():
    browser = RecordingBrowser()

    async def create_and_close():
        evaluator = new_evaluator()
        await evaluator.close()
        return evaluator.extractor.browser_manager, evaluator.verifier.browser_manager

    async def main():
        with shared_browser(browser):
            return await asyncio.gather(asyncio.create_task(create_and_close()), create_and_close())

    assert all(manager is browser for pair in asyncio.run(main()) for manager in pair)
    assert browser.stopped == 0


def test_an_evaluator_outside_the_block_creates_and_stops_its_own_browser(monkeypatch):
    monkeypatch.setattr(eval_toolkit, "_new_browser", RecordingBrowser)
    evaluator = new_evaluator()
    asyncio.run(evaluator.close())
    own = evaluator.extractor.browser_manager
    assert isinstance(own, RecordingBrowser) and evaluator.verifier.browser_manager is own
    assert own.stopped == 1

    given = RecordingBrowser()
    evaluator = new_evaluator(browser_manager=given)
    asyncio.run(evaluator.close())
    assert evaluator.extractor.browser_manager is given and given.stopped == 0


# ------------------------------------------------------------------ the evaluate command

TOY_SCRIPT = '''
from mind2web2 import Evaluator

async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger, model="o4-mini"):
    evaluator = Evaluator()
    root = evaluator.initialize(
        task_id="toy", agent_name=agent_name, answer_name=answer_name, client=client,
        task_description="Name a source.", answer=answer, global_cache=cache,
        global_semaphore=semaphore, logger=logger, default_model=model,
    )
    leaf = evaluator.add_leaf(id="claim", desc="The answer names a source.", parent=root)
    await evaluator.verify(claim="The answer names a source.", node=leaf, sources="https://a.example/1")
    return evaluator.get_summary()
'''


class FakeJudge(FakeLLMClient):
    """Stands in for ``LLMClient``: records its constructor arguments and accepts every claim."""

    instances: list["FakeJudge"] = []

    def __init__(self, **kwargs):
        super().__init__("all_true")
        self.kwargs = kwargs
        self.judge = kwargs["judge"]
        FakeJudge.instances.append(self)


class BrokenJudge(FakeJudge):
    async def async_response(self, count_token: bool = False, **kwargs):
        self.calls += 1
        raise JudgeError("judge unavailable")


@pytest.fixture
def run_evaluate(tmp_path, monkeypatch):
    """Answers for tasks t1 and t2 (two runs each), scripts for both, and a runner of the command."""
    monkeypatch.setattr(eval_runner, "CacheFileSys", lambda task_dir: SyntheticCache())
    monkeypatch.setattr(evaluate_command, "BatchBrowserManager", RecordingBrowser)
    monkeypatch.setattr(evaluate_command, "LLMClient", FakeJudge)

    def no_own_browser():
        raise AssertionError("evaluators must use the shared browser")

    monkeypatch.setattr(eval_toolkit, "_new_browser", no_own_browser)
    for task_id in ("t1", "t2"):
        (tmp_path / "scripts" / "2025_10_23" / f"{task_id}.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "scripts" / "2025_10_23" / f"{task_id}.py").write_text(TOY_SCRIPT)
        for run in (1, 2):
            answer = tmp_path / "answers" / "agent" / task_id / f"answer_{run}.md"
            answer.parent.mkdir(parents=True, exist_ok=True)
            answer.write_text("Source: https://a.example/1")

    def run(*options: str) -> int:
        return cli.main(["evaluate", "agent", "--answers-dir", str(tmp_path / "answers"),
                         "--results-dir", str(tmp_path / "results"), "--cache-dir", str(tmp_path / "cache"),
                         "--eval-scripts-dir", str(tmp_path / "scripts"), *options])

    return run


def test_evaluate_scores_every_answer_and_saves_the_metrics(tmp_path, run_evaluate, capsys):
    judge_x = ("--judge-model", "judge-x", "--judge-reasoning-effort", "low")
    assert run_evaluate(*judge_x, "--max-pages", "2") == 0
    out = capsys.readouterr().out
    assert "(judge: judge-x, reasoning_effort: low, temperature: not sent)" in out
    assert "t1: 2/2 answers scored, mean score 1.000" in out and "t2: 2/2 answers scored" in out
    assert FakeJudge.instances[-1].kwargs["judge"] == JudgeConfig(model="judge-x", reasoning_effort="low")
    [browser] = [b for b in RecordingBrowser.instances if b.kwargs.get("max_concurrent_pages") == 2]
    assert browser.stopped == 1

    [result_file] = (tmp_path / "results" / "agent" / "t1" / "answer_2" / "results").glob("*.json")
    assert json.loads(result_file.read_text())["judge"]["model"] == "judge-x"
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert (metrics["num_tasks"], metrics["num_runs"], metrics["success_rate"]["mean"]) == (2, 2, 1.0)

    assert run_evaluate(*judge_x) == 0  # the results scored these answers with this judge: reused
    assert FakeJudge.instances[-1].calls == 0

    assert run_evaluate() == 0  # another judge: evaluated again, the earlier results kept aside
    assert FakeJudge.instances[-1].calls > 0
    assert FakeJudge.instances[-1].kwargs["judge"].reasoning_effort == "max"
    results_dir = tmp_path / "results" / "agent" / "t1" / "answer_2" / "results"
    assert [json.loads(p.read_text())["judge"]["model"] for p in results_dir.glob("*.json")] == [DEFAULT_JUDGE_MODEL]
    assert [json.loads(p.read_text())["judge"]["model"]
            for p in (results_dir / "superseded").glob("*.json")] == ["judge-x"]


def test_evaluate_exits_with_1_when_an_answer_has_no_result(tmp_path, run_evaluate, monkeypatch, capsys):
    (tmp_path / "scripts" / "2025_10_23" / "t2.py").unlink()
    task_list = tmp_path / "split.txt"
    task_list.write_text("t1\nt2\n")
    assert run_evaluate("--task-list", str(task_list)) == 1
    out = capsys.readouterr().out
    assert "t2: no eval script" in out and "2 answers have no result" in out
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert len(metrics["missing_results"]) == 2

    monkeypatch.setattr(evaluate_command, "LLMClient", BrokenJudge)
    assert run_evaluate("--overwrite", "--task", "t1") == 1
    assert "t1: 0/2 answers scored" in capsys.readouterr().out


def test_evaluate_without_a_task_selection_skips_tasks_without_eval_scripts(tmp_path, run_evaluate, capsys):
    (tmp_path / "scripts" / "2025_10_23" / "t2.py").unlink()
    assert run_evaluate() == 0
    out = capsys.readouterr().out
    assert "Skipping 1 tasks that have answers but no eval script" in out and "t2:" not in out
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert (metrics["num_tasks"], metrics["missing_results"]) == (1, [])

    scripts = tmp_path / "scripts" / "2025_10_23"
    (scripts / "t1.py").rename(scripts / "other.py")  # the version keeps a script, for no answered task
    assert run_evaluate() == 1
    assert "None of the 2 tasks that agent 'agent' has answers for has an eval script" in capsys.readouterr().err


def test_evaluate_leaves_directories_without_answer_files_out_of_the_default_selection(tmp_path, run_evaluate,
                                                                                      capsys):
    old_layout = tmp_path / "answers" / "agent" / "t3"
    old_layout.mkdir()
    (old_layout / "answer1.md").write_text("Source: https://a.example/1")
    (tmp_path / "scripts" / "2025_10_23" / "t3.py").write_text(TOY_SCRIPT)
    assert run_evaluate() == 0
    assert "Skipping 1 tasks that have no answer_<k>.md files" in capsys.readouterr().out
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert (metrics["num_tasks"], metrics["missing_answers"]) == (2, [])


def test_evaluate_scores_the_given_number_of_runs(tmp_path, run_evaluate, capsys):
    assert run_evaluate() == 0
    assert "Scored 2 runs, the highest run index found; pass --num-runs 3" in capsys.readouterr().out
    assert run_evaluate("--num-runs", "3") == 0
    assert "--num-runs" not in capsys.readouterr().out
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert (metrics["num_runs"], metrics["missing_answers"]) == (3, [{"task_id": "t1", "run": 3},
                                                                    {"task_id": "t2", "run": 3}])


def test_evaluate_exits_with_1_when_the_task_list_cannot_be_read(tmp_path, run_evaluate, capsys):
    assert run_evaluate("--task-list", str(tmp_path / "missing.csv")) == 1
    assert "Cannot read the task list" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--max-tasks", "--max-answers", "--max-pages", "--max-llm-requests",
                                    "--num-runs"])
def test_evaluate_counts_must_be_at_least_1(option, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["evaluate", "agent", option, "0"])
    assert exc.value.code == 2 and "must be at least 1" in capsys.readouterr().err


def test_each_command_states_its_default_task_selection(capsys):
    helps = {}
    for command in ("validate", "cache", "evaluate", "metrics"):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        helps[command] = " ".join(capsys.readouterr().out.split())
    assert "Default: the task directories under <answers-dir>/<agent>/." in helps["validate"]
    assert "Default: the tasks the agent has answers for (task directories" in helps["cache"]
    assert "with answer_<k>.md files) that have an eval script in the eval-script version." in helps["evaluate"]
    assert "or under <results-dir>/<agent>/ when the agent has no answers directory." in helps["metrics"]


def test_evaluate_selects_tasks(tmp_path, run_evaluate, capsys):
    assert run_evaluate("--task", "t2") == 0
    assert "t1:" not in capsys.readouterr().out
    assert not (tmp_path / "results" / "agent" / "metrics.json").exists()  # no metrics for a partial run

    task_list = tmp_path / "split.csv"
    task_list.write_text("task_id,task_description,domain,subdomain\nt1,d,A,x\nt2,d,A,x\nt3,d,B,y\n")
    assert run_evaluate("--task-list", str(task_list)) == 0
    assert "Skipping 1 tasks that have no answer_<k>.md files" in capsys.readouterr().out
    metrics = json.loads((tmp_path / "results" / "agent" / "metrics.json").read_text())
    assert metrics["num_tasks"] == 3 and metrics["missing_answers"] == [{"task_id": "t3", "run": 1},
                                                                       {"task_id": "t3", "run": 2}]


def test_evaluate_exits_with_2_without_a_matching_eval_script_version(run_evaluate, capsys):
    assert run_evaluate("--eval-version", "2099_01_01") == 2
    assert "available: 2025_10_23" in capsys.readouterr().err
