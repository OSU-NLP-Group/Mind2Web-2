"""``mind2web2 metrics``: compute an agent's leaderboard metrics from its evaluation results.

Prints Partial Completion, Success Rate, Pass@k, Time, and Answer Length (the
metrics of the paper and the leaderboard, defined in :mod:`mind2web2.metrics`)
and saves them, with per-run, per-task, and per-domain breakdowns and the
leaderboard entry, to ``<results-dir>/<agent>/metrics.json``.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import _common
from ..metrics import collect_records, compute_metrics, discover_tasks, format_report, save_metrics
from ..submission import MetadataError


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "metrics", help="Compute leaderboard metrics from evaluation results.",
        description="Compute Partial Completion, Success Rate, Pass@k, Time, and Answer Length from "
                    "an agent's evaluation results, print them, and save them with per-run, per-task, "
                    "and per-domain breakdowns to <results-dir>/<agent>/metrics.json. Answers that "
                    "are missing or have no evaluation result count as 0 in Partial Completion, "
                    "Success Rate, and Pass@k, and are listed.",
    )
    _common.add_agent(parser)
    _common.add_answers_dir(parser)
    _common.add_results_dir(parser)
    _common.add_task_selection(parser)
    parser.add_argument("--json", action="store_true", help="Print the metrics as JSON instead of a report.")
    parser.add_argument("--no-save", action="store_true", help="Do not write metrics.json.")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    discovered = discover_tasks(args.agent, args.answers_dir, args.results_dir)
    try:
        tasks = _common.resolve_tasks(args.task_list, discovered)
    except (OSError, ValueError) as exc:
        print(f"Cannot read the task list: {exc}", file=sys.stderr)
        return 1
    if not tasks:
        print(f"No tasks found for agent {args.agent!r} under {args.answers_dir} or {args.results_dir}.",
              file=sys.stderr)
        return 1
    try:
        records, num_runs = collect_records(
            args.agent, [t.task_id for t in tasks], args.answers_dir, args.results_dir, args.num_runs)
    except MetadataError as exc:
        print(f"Invalid answer metadata: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Cannot read an answer: {exc}", file=sys.stderr)
        return 1
    metrics = compute_metrics(records, tasks, num_runs, args.agent, args.task_list)

    if args.json:
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
    else:
        print(format_report(metrics))
    if not args.no_save:
        path = save_metrics(metrics, args.results_dir, args.agent)
        print(f"Saved {path}", file=sys.stderr)
    return 0
