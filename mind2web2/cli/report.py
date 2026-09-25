"""``mind2web2 report``: write an HTML page for browsing an agent's evaluation results.

The page (:mod:`mind2web2.report`) shows the saved metrics with charts, every
task's score in every run, and for each answer the checks that lost points and
its rubric tree with the evidence of each check: the claim, the pages checked
(failed checks with a thumbnail of the page from ``--cache-dir``), the judge's
votes and reasoning.  It is written to ``<results-dir>/<agent>/report.html``
unless ``--output`` names another file; ``mind2web2 evaluate`` writes the same
page when it ends.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import _common
from ..report import REPORT_FILE, write_report


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "report", help="Write an HTML page for browsing evaluation results.",
        description="Write a self-contained HTML page with the agent's metrics and charts, every task's "
                    "score in every run, and for each answer the checks that lost points and its rubric tree "
                    "with the evidence of each check (the claim, the pages checked, the judge's votes and "
                    f"reasoning), to <results-dir>/<agent>/{REPORT_FILE}. Failed checks show a thumbnail of "
                    "the page from the cache in --cache-dir.",
    )
    _common.add_agent(parser)
    _common.add_results_dir(parser)
    _common.add_cache_dir(parser)
    parser.add_argument("--task", dest="tasks", action="append", metavar="TASK_ID", default=None,
                        help="Include only this task; repeat the option for several tasks. Default: every task "
                             "with results.")
    parser.add_argument("--output", type=Path, default=None,
                        help=f"File to write (default: <results-dir>/<agent>/{REPORT_FILE}).")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    try:
        path = write_report(args.results_dir, args.agent, args.output, args.tasks, cache_root=args.cache_dir)
    except FileNotFoundError as exc:
        print(f"No report written: {exc}.", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0
