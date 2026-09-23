"""The ``mind2web2`` command, the entry point for working with a submission.

Each subcommand lives in its own module, which defines ``register(subparsers)``
to add its parser and a ``run(args) -> int`` handler that returns the exit
status.  Paths default to directories under the current working directory
(``answers/``, ``eval_results/``), matching the repository layout.

Usage::

    uv run mind2web2 validate <agent_name> --task-list test_set.csv
    uv run mind2web2 metrics <agent_name>
"""
from __future__ import annotations

import argparse

from . import metrics, validate

COMMANDS = (validate, metrics)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mind2web2",
        description="Mind2Web 2: check a submission and compute its leaderboard metrics.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for command in COMMANDS:
        command.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)
