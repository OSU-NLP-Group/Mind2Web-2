"""Leaderboard metrics computed from evaluation results, and the ``mind2web2 metrics`` command."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from pathlib import Path

import pytest

from mind2web2 import eval_runner
from mind2web2.api_tools import tool_googlemap
from mind2web2.cli import main as cli_main
from mind2web2.metrics import collect_records, compute_metrics, discover_tasks, format_report, is_success
from mind2web2.results import answer_output_dir, latest_result_file, result_file_name
from mind2web2.submission import TaskInfo

from offline_eval import FakeGoogleMapsTool, FakeLLMClient, SyntheticCache

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT = "agent"

# (task, run) -> (word count, final score or None for "no result", time in seconds or None).
# Task t3 has no answer for run 3.
SUBMISSION = {
    ("t1", 1): (10, 1.0, 120),
    ("t1", 2): (20, 0.5, 60),
    ("t1", 3): (30, 1.0, None),
    ("t2", 1): (40, 0.0, None),
    ("t2", 2): (50, 0.9999999, None),  # a success up to float rounding
    ("t2", 3): (60, None, None),       # answer present, evaluation result missing
    ("t3", 1): (70, 0.25, None),
    ("t3", 2): (80, 0.75, None),
}
TASKS = [TaskInfo("t1", "A"), TaskInfo("t2", "A"), TaskInfo("t3", "B")]


def answer_text(words: int) -> str:
    return " ".join(["https://example.com/source"] + ["word"] * (words - 1))


def write_result(results_root: Path, task: str, run: int, score: float, timestamp: str) -> None:
    result_dir = answer_output_dir(results_root, AGENT, task, f"answer_{run}.md") / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    result = {"agent_name": AGENT, "answer_name": f"answer_{run}.md", "final_score": score}
    (result_dir / result_file_name(timestamp, f"answer_{run}.md")).write_text(json.dumps(result))


def build_submission(root: Path) -> tuple[Path, Path]:
    answers_root, results_root = root / "answers", root / "eval_results"
    for (task, run), (words, score, seconds) in SUBMISSION.items():
        task_dir = answers_root / AGENT / task
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / f"answer_{run}.md").write_text(answer_text(words))
        if seconds is not None:
            (task_dir / f"answer_{run}.meta.json").write_text(json.dumps({"time_seconds": seconds}))
        if score is not None:
            write_result(results_root, task, run, score, "20260102_120000")
    write_result(results_root, "t1", 2, 0.1, "20260101_120000")  # older result, superseded
    return answers_root, results_root


def test_success_tolerates_float_rounding():
    assert is_success(1.0) and is_success(0.9999999)
    assert not is_success(0.999)


def test_metrics_match_hand_computed_values(tmp_path):
    answers_root, results_root = build_submission(tmp_path)
    records, num_runs = collect_records(AGENT, [t.task_id for t in TASKS], answers_root, results_root)
    assert num_runs == 3
    metrics = compute_metrics(records, TASKS, num_runs, AGENT, task_list=Path("split.csv"))

    pc = metrics["partial_completion"]
    assert pc["per_run"] == pytest.approx([1.25 / 3, 2.2499999 / 3, 1 / 3])
    assert pc["mean"] == pytest.approx(0.5, abs=1e-7)
    assert pc["std"] == pytest.approx(math.sqrt(sum((v - pc["mean"]) ** 2 for v in pc["per_run"]) / 3))

    assert metrics["success_rate"]["per_run"] == pytest.approx([1 / 3] * 3)
    assert metrics["success_rate"]["std"] == pytest.approx(0.0)
    assert metrics["pass_at_k"] == {"k": 3, "value": pytest.approx(2 / 3)}

    time = metrics["time_minutes"]
    assert time["per_run"] == [2.0, 1.0, None]
    assert (time["mean"], time["std"]) == (pytest.approx(1.5), pytest.approx(0.5))
    assert (time["answers_with_time"], time["answers"]) == (2, 8)

    length = metrics["answer_length_words"]
    assert length["per_run"] == pytest.approx([40, 50, 45])
    assert length["std"] == pytest.approx(math.sqrt(50 / 3))

    assert metrics["missing_answers"] == [{"task_id": "t3", "run": 3}]
    assert metrics["missing_results"] == [{"task_id": "t2", "run": 3}]
    assert metrics["per_task"]["t1"] == {"scores": [1.0, 0.5, 1.0], "pass": True}
    assert metrics["by_domain"]["A"]["partial_completion"] == pytest.approx((0.5 + 0.74999995 + 0.5) / 3)
    assert metrics["by_domain"]["B"]["success_rate"] == 0.0
    assert metrics["leaderboard_entry"] == {
        "partial_completion": "0.50", "success_rate": "0.33", "pass3": "0.67",
        "time": "1.50", "answer_length": "45",
    }


def test_tasks_without_answers_count_as_zero(tmp_path):
    answers_root, results_root = build_submission(tmp_path)
    tasks = TASKS + [TaskInfo("t4", "B")]
    records, num_runs = collect_records(AGENT, [t.task_id for t in tasks], answers_root, results_root)
    metrics = compute_metrics(records, tasks, num_runs, AGENT)
    assert metrics["partial_completion"]["mean"] == pytest.approx(0.5 * 3 / 4, abs=1e-7)
    assert {"task_id": "t4", "run": 2} in metrics["missing_answers"]


def test_answer_copies_next_to_results_replace_a_missing_answers_dir(tmp_path):
    answers_root, results_root = build_submission(tmp_path)
    for path in (answers_root / AGENT).glob("*/*"):
        copy_dir = results_root / AGENT / path.parent.name / path.name.split(".")[0]
        copy_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, copy_dir / path.name)

    def metrics() -> dict:
        records, num_runs = collect_records(AGENT, [t.task_id for t in TASKS], answers_root, results_root)
        return compute_metrics(records, TASKS, num_runs, AGENT)

    expected = metrics()
    shutil.rmtree(answers_root)
    assert metrics() == expected


def test_leaderboard_entry_needs_a_task_list_and_three_runs(tmp_path):
    answers_root, results_root = build_submission(tmp_path)
    split = Path("split.csv")

    def metrics(task_ids: list[str], task_list: Path | None, num_runs: int | None = None) -> dict:
        records, runs = collect_records(AGENT, task_ids, answers_root, results_root, num_runs)
        return compute_metrics(records, [TaskInfo(t) for t in task_ids], runs, AGENT, task_list)

    answered = metrics(["t1", "t2", "t3"], None)
    assert answered["leaderboard_entry"] is None
    assert answered["task_selection"]["source"] == "answers"
    assert answered["task_selection"]["path"] is None
    assert "No leaderboard entry" in format_report(answered)

    listed = metrics(["t3", "t1", "t2"], split)
    assert listed["task_selection"] == {**answered["task_selection"], "source": "task_list", "path": "split.csv"}
    assert "No leaderboard entry" not in format_report(listed)

    assert metrics(["t1", "t2", "t3"], split, num_runs=2)["leaderboard_entry"] is None
    assert metrics(["t2"], split)["leaderboard_entry"]["time"] == "-"  # no answer reports a time


def test_tasks_are_discovered_only_where_there_are_answers(tmp_path):
    answers_root, results_root = build_submission(tmp_path)
    (answers_root / AGENT / "t9").mkdir()
    (answers_root / AGENT / "t9" / "notes.txt").write_text("not an answer")
    assert discover_tasks(AGENT, answers_root, results_root) == ["t1", "t2", "t3"]


def test_latest_result_is_chosen_by_timestamp(tmp_path):
    for name in ("20260102_000000_answer_1.md.json", "20251231_235959_answer_1.md_debug.json"):
        (tmp_path / name).write_text("{}")
    assert latest_result_file(tmp_path).name == "20260102_000000_answer_1.md.json"
    assert latest_result_file(tmp_path / "missing") is None


def test_metrics_command_prints_report_and_saves_json(tmp_path, capsys):
    answers_root, results_root = build_submission(tmp_path)
    task_list = tmp_path / "split.csv"
    task_list.write_text("task_id,task_description,domain,subdomain\n"
                         "t1,d,A,x\nt2,d,A,x\nt3,d,B,y\n")
    argv = ["metrics", AGENT, "--answers-dir", str(answers_root), "--results-dir", str(results_root),
            "--task-list", str(task_list)]
    assert cli_main(argv) == 0
    out = capsys.readouterr().out
    assert "Partial Completion  0.5000" in out
    assert "t2 run 3" in out
    saved = json.loads((results_root / AGENT / "metrics.json").read_text())
    assert saved["leaderboard_entry"]["pass3"] == "0.67"
    assert saved["task_selection"]["path"] == str(task_list)

    assert cli_main(argv + ["--json", "--no-save"]) == 0
    assert json.loads(capsys.readouterr().out)["num_tasks"] == 3


def test_metrics_command_fails_without_tasks(tmp_path, capsys):
    argv = ["metrics", AGENT, "--answers-dir", str(tmp_path / "a"), "--results-dir", str(tmp_path / "r")]
    assert cli_main(argv) == 1
    assert "No tasks found" in capsys.readouterr().err


def test_metrics_command_reports_bad_input_in_one_line(tmp_path, capsys):
    answers_root, results_root = build_submission(tmp_path)
    argv = ["metrics", AGENT, "--answers-dir", str(answers_root), "--results-dir", str(results_root)]

    with pytest.raises(SystemExit) as exit_info:
        cli_main(argv + ["--num-runs", "0"])
    assert exit_info.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err

    assert cli_main(argv + ["--task-list", str(tmp_path / "missing.csv")]) == 1
    assert "Cannot read the task list" in capsys.readouterr().err

    (answers_root / AGENT / "t1" / "answer_1.md").write_bytes(b"\xff\xfe not UTF-8")
    assert cli_main(argv + ["--no-save"]) == 1
    err = capsys.readouterr().err
    assert "Cannot read an answer" in err and "answer_1.md: not UTF-8 text" in err


def test_metrics_read_the_results_evaluate_task_writes(tmp_path, monkeypatch):
    """Run the real evaluation loop offline and compute metrics from what it saved."""
    monkeypatch.setattr(eval_runner, "CacheFileSys", lambda task_dir: SyntheticCache())
    monkeypatch.setattr(tool_googlemap, "GoogleMapsTool", FakeGoogleMapsTool)
    answers_root = tmp_path / "answers"
    shutil.copytree(REPO_ROOT / "answers" / "example" / "yu_lineage", answers_root / "example" / "yu_lineage")
    (answers_root / "example" / "yu_lineage" / "answer_1.meta.json").write_text('{"time_seconds": 90}')
    results_root = tmp_path / "eval_results"

    evaluated = asyncio.run(eval_runner.evaluate_task(
        client=FakeLLMClient("hash"), task_id="yu_lineage", agent_name="example",
        answer_dir=answers_root, cache_dir=tmp_path / "cache", output_dir=results_root,
        script_path=REPO_ROOT / "eval_scripts" / "dev_set" / "yu_lineage.py",
    ))
    scores = {r["answer_name"]: r["final_score"] for r in evaluated}
    assert sorted(scores) == ["answer_1.md", "answer_2.md", "answer_3.md"]
    assert (results_root / "example" / "yu_lineage" / "answer_1" / "answer_1.meta.json").exists()

    records, num_runs = collect_records("example", ["yu_lineage"], answers_root, results_root)
    metrics = compute_metrics(records, [TaskInfo("yu_lineage")], num_runs, "example")
    assert metrics["per_task"]["yu_lineage"]["scores"] == [scores[f"answer_{k}.md"] for k in (1, 2, 3)]
    assert metrics["missing_results"] == []
    assert metrics["time_minutes"]["per_run"] == [1.5, None, None]

    # A deleted metadata file does not live on in the copy next to the results
    (answers_root / "example" / "yu_lineage" / "answer_1.meta.json").unlink()
    asyncio.run(eval_runner.evaluate_task(
        client=FakeLLMClient("hash"), task_id="yu_lineage", agent_name="example",
        answer_dir=answers_root, cache_dir=tmp_path / "cache", output_dir=results_root,
        script_path=REPO_ROOT / "eval_scripts" / "dev_set" / "yu_lineage.py",
    ))
    assert not (results_root / "example" / "yu_lineage" / "answer_1" / "answer_1.meta.json").exists()

def test_a_result_for_a_replaced_answer_is_not_used(tmp_path):
    answers_root, results_root = tmp_path / "answers", tmp_path / "eval_results"
    task_dir = answers_root / AGENT / "t1"
    task_dir.mkdir(parents=True)
    # run -> (the answer file now, the answer text the result recorded; None: a result without a digest)
    runs = {1: ("the evaluated answer", "the evaluated answer"),
            2: ("the replacement", "the answer before it was replaced"),
            3: ("an answer scored by an older framework", None)}
    for run, (text, recorded) in runs.items():
        (task_dir / f"answer_{run}.md").write_text(text)
        write_result(results_root, "t1", run, 1.0, "20260102_120000")
        if recorded is not None:
            [path] = (answer_output_dir(results_root, AGENT, "t1", f"answer_{run}.md") / "results").glob("*.json")
            result = json.loads(path.read_text())
            result["answer_sha256"] = hashlib.sha256(recorded.encode()).hexdigest()
            path.write_text(json.dumps(result))

    records, num_runs = collect_records(AGENT, ["t1"], answers_root, results_root)
    assert [(r.run, r.score, r.stale_result) for r in records] == [(1, 1.0, False), (2, None, True), (3, 1.0, False)]
    metrics = compute_metrics(records, [TaskInfo("t1")], num_runs, AGENT)
    assert metrics["stale_results"] == [{"task_id": "t1", "run": 2}]
    assert metrics["missing_results"] == []
    assert metrics["partial_completion"]["per_run"] == [1.0, 0.0, 1.0]
    assert "Answers changed since their evaluation result (scored 0): 1" in format_report(metrics)
