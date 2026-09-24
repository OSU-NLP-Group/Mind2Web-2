"""Leaderboard metrics (paper Table 3) computed from evaluation results.

Notation: an agent is evaluated on a task list T with runs 1..k.  Run r of task
t is the answer file ``answer_<r>.md`` and s(t, r) in [0, 1] is the root score
of its rubric tree.

- Partial Completion of run r: the mean of s(t, r) over t in T.
- Success Rate of run r: the fraction of t in T with s(t, r) = 1.
- Both are reported as the mean over runs +/- the population standard
  deviation over runs.
- Pass@k: the fraction of t in T for which some run r <= k has s(t, r) = 1.
- Time (min): for each run, the mean self-reported inference time
  (``answer_<r>.meta.json``) over the answers that report one; then the mean
  +/- std over the runs that have any.
- Answer Length: for each run, the mean word count of the answer markdown
  over the answers present; then the mean +/- std over runs.

A (task, run) pair without an answer file, or whose answer has no evaluation
result, scores 0 in the first three metrics, and the report lists every such
pair so that it can be fixed.  Time and Answer Length describe the answers
themselves: an answer that exists counts toward them whether or not it has an
evaluation result, and a missing answer file does not.

The metrics record which tasks they cover (``task_selection``), and they
include a ``leaderboard_entry`` only when they are computed over a task list
with exactly 3 runs, the leaderboard's setting; otherwise it is ``None``.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .results import load_latest_result
from .submission import (
    AnswerFile, TaskInfo, count_words, list_answer_files, load_metadata,
)

SUCCESS_TOLERANCE = 1e-6
METRICS_FILE = "metrics.json"


def is_success(score: float) -> bool:
    """A root score is a full success when it equals 1 up to float rounding."""
    return score >= 1.0 - SUCCESS_TOLERANCE


@dataclass(frozen=True)
class AnswerRecord:
    """Everything the metrics need about run ``run`` of task ``task_id``.

    ``score`` is ``None`` when there is no answer or the answer has no
    evaluation result; ``word_count`` is ``None`` when there is no answer.
    """

    task_id: str
    run: int
    answer_present: bool
    score: float | None = None
    word_count: int | None = None
    time_seconds: float | None = None


def _mean_std(values: list[float]) -> dict:
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def discover_tasks(agent_name: str, answers_root: Path, results_root: Path) -> list[str]:
    """Return the IDs of the tasks an agent has answers for.

    These are the task directories under ``<answers_root>/<agent_name>/`` that
    contain at least one ``answer_<k>.md``, or, when the agent has no answers
    directory, the task directories under ``<results_root>/<agent_name>/`` that
    hold a copy of one (see :func:`discover_answers`).
    """
    for agent_dir in (Path(answers_root) / agent_name, Path(results_root) / agent_name):
        if agent_dir.is_dir():
            candidates = sorted(p.name for p in agent_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
            return [t for t in candidates if discover_answers(agent_name, t, answers_root, results_root)]
    return []


def task_selection(task_ids: Iterable[str], task_list: Path | None) -> dict:
    """Describe the tasks that metrics cover, as recorded in ``metrics.json``.

    ``source`` is ``"task_list"`` with the list's ``path`` when the tasks come
    from a task list, and ``"answers"`` (``path`` ``None``) when they are the
    tasks the agent has answers for.  ``task_ids_sha256`` is the SHA-256 of the
    sorted task IDs joined by newlines, so that two metrics files can be checked
    for covering the same tasks.
    """
    digest = hashlib.sha256("\n".join(sorted(task_ids)).encode("utf-8")).hexdigest()
    return {
        "source": "answers" if task_list is None else "task_list",
        "path": None if task_list is None else str(task_list),
        "task_ids_sha256": digest,
    }


def discover_answers(agent_name: str, task_id: str, answers_root: Path, results_root: Path) -> list[AnswerFile]:
    """Find a task's answer files.

    Answers come from ``<answers_root>/<agent_name>/<task_id>/``.  When the agent
    has no answers directory at all, the copies that ``run_eval.py`` stores next
    to the results are used instead, so metrics can be computed from a results
    folder alone.
    """
    agent_answers = Path(answers_root) / agent_name
    if agent_answers.is_dir():
        return list_answer_files(agent_answers / task_id)
    task_results = Path(results_root) / agent_name / task_id
    copies = []
    if task_results.is_dir():
        for answer_dir in task_results.iterdir():
            copies.extend(list_answer_files(answer_dir))
    return sorted(copies, key=lambda a: a.run)


def collect_records(
        agent_name: str,
        tasks: Iterable[str],
        answers_root: Path,
        results_root: Path,
        num_runs: int | None = None,
) -> tuple[list[AnswerRecord], int]:
    """Gather one :class:`AnswerRecord` per (task, run) and return them with the run count.

    ``num_runs`` defaults to the highest run index among the answers found.
    Raises :class:`~mind2web2.submission.MetadataError` if an answer's
    ``.meta.json`` file is invalid, and ``ValueError`` naming the file if an
    answer is not UTF-8 text.
    """
    task_ids = list(tasks)
    answers = {t: discover_answers(agent_name, t, answers_root, results_root) for t in task_ids}
    if num_runs is None:
        num_runs = max((a.run for files in answers.values() for a in files), default=1)

    records = []
    for task_id in task_ids:
        by_run = {a.run: a for a in answers[task_id]}
        for run in range(1, num_runs + 1):
            answer = by_run.get(run)
            if answer is None:
                records.append(AnswerRecord(task_id, run, answer_present=False))
                continue
            result = load_latest_result(results_root, agent_name, task_id, answer.name)
            score = float(result["final_score"]) if result and "final_score" in result else None
            metadata = load_metadata(answer)
            try:
                text = answer.path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{answer.path}: not UTF-8 text") from exc
            records.append(AnswerRecord(
                task_id=task_id,
                run=run,
                answer_present=True,
                score=score,
                word_count=count_words(text),
                time_seconds=metadata.time_seconds if metadata else None,
            ))
    return records, num_runs


def compute_metrics(
        records: list[AnswerRecord],
        tasks: list[TaskInfo],
        num_runs: int,
        agent_name: str = "",
        task_list: Path | None = None,
) -> dict:
    """Compute the leaderboard metrics; see the module docstring for definitions.

    ``records`` must contain one record per (task, run) for every task in
    ``tasks`` and every run in ``1..num_runs``, as returned by
    :func:`collect_records`.  ``task_list`` is the task list that ``tasks``
    came from, or ``None`` when they are the tasks the agent has answers for;
    it is recorded, not read.  The result is JSON-serializable.
    """
    table = {(r.task_id, r.run): r for r in records}
    task_ids = [t.task_id for t in tasks]
    runs = range(1, num_runs + 1)
    missing = [(t, r) for t in task_ids for r in runs if (t, r) not in table]
    if missing:
        raise ValueError(f"no record for (task, run) pairs {missing[:5]}")

    def score(task_id: str, run: int) -> float:
        value = table[(task_id, run)].score
        return 0.0 if value is None else value

    pc_per_run = [statistics.fmean(score(t, r) for t in task_ids) for r in runs]
    sr_per_run = [statistics.fmean(float(is_success(score(t, r))) for t in task_ids) for r in runs]
    passed = {t: any(is_success(score(t, r)) for r in runs) for t in task_ids}

    time_per_run: list[float | None] = []
    length_per_run: list[float | None] = []
    for run in runs:
        present = [table[(t, run)] for t in task_ids if table[(t, run)].answer_present]
        times = [rec.time_seconds / 60 for rec in present if rec.time_seconds is not None]
        time_per_run.append(statistics.fmean(times) if times else None)
        length_per_run.append(statistics.fmean(rec.word_count for rec in present) if present else None)

    answers_present = sum(1 for rec in records if rec.answer_present)
    answers_with_time = sum(1 for rec in records if rec.time_seconds is not None)
    timed_runs = [v for v in time_per_run if v is not None]
    measured_runs = [v for v in length_per_run if v is not None]

    metrics = {
        "agent_name": agent_name,
        "task_selection": task_selection(task_ids, task_list),
        "num_tasks": len(task_ids),
        "num_runs": num_runs,
        "partial_completion": {**_mean_std(pc_per_run), "per_run": pc_per_run},
        "success_rate": {**_mean_std(sr_per_run), "per_run": sr_per_run},
        "pass_at_k": {"k": num_runs, "value": statistics.fmean(float(v) for v in passed.values())},
        "time_minutes": (
            {**_mean_std(timed_runs), "per_run": time_per_run,
             "answers_with_time": answers_with_time, "answers": answers_present}
            if timed_runs else None
        ),
        "answer_length_words": (
            {**_mean_std(measured_runs), "per_run": length_per_run} if measured_runs else None
        ),
        "missing_answers": [
            {"task_id": rec.task_id, "run": rec.run} for rec in records if not rec.answer_present
        ],
        "missing_results": [
            {"task_id": rec.task_id, "run": rec.run}
            for rec in records if rec.answer_present and rec.score is None
        ],
        "per_task": {
            t: {"scores": [table[(t, r)].score for r in runs], "pass": passed[t]} for t in task_ids
        },
        "by_domain": _by_domain(tasks, runs, score),
    }
    metrics["leaderboard_entry"] = leaderboard_entry(metrics)
    return metrics


def _by_domain(tasks: list[TaskInfo], runs: range, score) -> dict | None:
    domains: dict[str, list[str]] = {}
    for task in tasks:
        if task.domain:
            domains.setdefault(task.domain, []).append(task.task_id)
    if not domains:
        return None
    out = {}
    for domain, task_ids in sorted(domains.items()):
        pc = [statistics.fmean(score(t, r) for t in task_ids) for r in runs]
        sr = [statistics.fmean(float(is_success(score(t, r))) for t in task_ids) for r in runs]
        out[domain] = {"num_tasks": len(task_ids),
                       "partial_completion": _mean_std(pc)["mean"],
                       "success_rate": _mean_std(sr)["mean"]}
    return out


def leaderboard_entry(metrics: dict) -> dict | None:
    """The ``eval_set`` block of an entry in the leaderboard's ``leaderboard_data.json``.

    Returns ``None`` unless the metrics cover a task list with exactly 3 runs,
    so that metrics over the tasks an agent happened to answer, or over another
    number of runs, cannot be mistaken for a leaderboard entry.  Whether the
    task list is a whole split is not checked: pass the split's own list.  Values are strings as on the
    leaderboard: two decimals, the answer length as an integer, and ``"-"``
    when unavailable.
    """
    if metrics["task_selection"]["source"] != "task_list" or metrics["num_runs"] != 3:
        return None
    time = metrics["time_minutes"]
    length = metrics["answer_length_words"]
    return {
        "partial_completion": f"{metrics['partial_completion']['mean']:.2f}",
        "success_rate": f"{metrics['success_rate']['mean']:.2f}",
        "pass3": f"{metrics['pass_at_k']['value']:.2f}",
        "time": f"{time['mean']:.2f}" if time else "-",
        "answer_length": f"{length['mean']:.0f}" if length else "-",
    }


def save_metrics(metrics: dict, results_root: Path, agent_name: str) -> Path:
    """Write metrics as JSON to ``<results_root>/<agent_name>/metrics.json`` and return that path."""
    path = Path(results_root) / agent_name / METRICS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def format_report(metrics: dict, max_listed: int = 20) -> str:
    """Render metrics as a human-readable text report."""
    def mean_std(block: dict, fmt: str) -> str:
        return f"{block['mean']:{fmt}} ± {block['std']:{fmt}}"

    def per_run(values: list, fmt: str) -> str:
        return "  ".join("-" if v is None else f"{v:{fmt}}" for v in values)

    k = metrics["pass_at_k"]["k"]
    selection = metrics["task_selection"]
    source = (f"from {selection['path']}" if selection["source"] == "task_list"
              else "that have answers")
    lines = [
        f"Agent {metrics['agent_name']!r}: {metrics['num_tasks']} tasks ({source}) x {metrics['num_runs']} runs",
        f"  Partial Completion  {mean_std(metrics['partial_completion'], '.4f')}"
        f"    per run: {per_run(metrics['partial_completion']['per_run'], '.4f')}",
        f"  Success Rate        {mean_std(metrics['success_rate'], '.4f')}"
        f"    per run: {per_run(metrics['success_rate']['per_run'], '.4f')}",
        f"  Pass@{k:<15d}{metrics['pass_at_k']['value']:.4f}",
    ]
    time = metrics["time_minutes"]
    if time:
        lines.append(f"  Time (min)          {mean_std(time, '.2f')}"
                     f"    ({time['answers_with_time']}/{time['answers']} answers report a time)")
    else:
        lines.append("  Time (min)          -    (no answer_<k>.meta.json with time_seconds)")
    length = metrics["answer_length_words"]
    if length:
        lines.append(f"  Answer Length       {mean_std(length, '.0f')} words")

    if metrics["by_domain"]:
        lines.append("  By domain (Partial Completion / Success Rate):")
        for domain, block in metrics["by_domain"].items():
            lines.append(f"    {domain:<28s} {block['partial_completion']:.4f} / "
                         f"{block['success_rate']:.4f}  ({block['num_tasks']} tasks)")

    for key, label in (("missing_answers", "Missing answer files"),
                       ("missing_results", "Answers without an evaluation result")):
        items = metrics[key]
        if items:
            lines.append(f"  {label} (scored 0): {len(items)}")
            for item in items[:max_listed]:
                lines.append(f"    {item['task_id']} run {item['run']}")
            if len(items) > max_listed:
                lines.append(f"    ... and {len(items) - max_listed} more")
    if metrics["leaderboard_entry"] is None:
        lines.append("  No leaderboard entry: it needs --task-list (the split's task list) and 3 runs.")
    return "\n".join(lines)

