"""``mind2web2 validate``: check an agent's answers against the submission layout.

Reports errors (problems that break evaluation or the metrics) and warnings
(problems that cost points or make a file be ignored), as defined by
:func:`mind2web2.submission.validate_submission`.  Exits with status 1 when
there is any error.
"""
from __future__ import annotations

import argparse
import sys

from . import _common
from ..submission import MetadataError, list_answer_files, load_metadata, validate_submission


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "validate", help="Check an agent's answers against the submission layout.",
        description="Check an agent's answers against the submission layout. Exits with status 1 "
                    "if there is an error (a problem that breaks evaluation or the metrics); "
                    "warnings mark problems that cost points or make a file be ignored.",
    )
    _common.add_agent(parser)
    _common.add_answers_dir(parser)
    _common.add_task_selection(parser, default="the task directories under <answers-dir>/<agent>/")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    agent_dir = args.answers_dir / args.agent
    try:
        tasks = _common.resolve_tasks(args.task_list, _common.answer_task_ids(agent_dir))
    except (OSError, ValueError) as exc:
        print(f"Cannot read the task list: {exc}", file=sys.stderr)
        return 1
    task_ids = [t.task_id for t in tasks] if args.task_list else None
    issues = validate_submission(agent_dir, task_ids, args.num_runs)

    answers = [a for t in tasks for a in list_answer_files(agent_dir / t.task_id)]
    runs = sorted({a.run for a in answers})
    timed = 0
    for answer in answers:
        try:
            metadata = load_metadata(answer)
        except MetadataError:
            continue  # already reported as an error
        if metadata is not None and metadata.time_seconds is not None:
            timed += 1
    run_range = f"runs {runs[0]}-{runs[-1]}" if runs else "no runs"
    print(f"Checked {agent_dir}: {len(tasks)} tasks, {len(answers)} answers ({run_range}), "
          f"{timed} with time metadata.")

    for issue in sorted(issues, key=lambda i: (i.level != "error", i.path)):
        print(f"{issue.level.upper():<8} {issue.path}: {issue.message}")
    errors = sum(issue.level == "error" for issue in issues)
    warnings = len(issues) - errors
    print(f"{errors} error(s), {warnings} warning(s).")
    return 1 if errors else 0
