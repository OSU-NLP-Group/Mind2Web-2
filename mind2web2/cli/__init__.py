"""The ``mind2web2`` command, the entry point for running the benchmark on an agent's answers.

Each subcommand lives in its own module, which defines ``register(subparsers)``
to add its parser and a ``run(args) -> int`` handler that returns the exit
status.  Paths default to directories under the current working directory
(``answers/``, ``cache/``, ``eval_scripts/``, ``eval_results/``), matching the
repository layout.

Usage::

    uv run mind2web2 validate <agent_name> --task-list test_set.csv
    uv run mind2web2 cache <agent_name>
    uv run mind2web2 evaluate <agent_name>
    uv run mind2web2 metrics <agent_name>
"""
from __future__ import annotations

import argparse

from . import cache, evaluate, metrics, validate

COMMANDS = (validate, cache, evaluate, metrics)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mind2web2",
        description="Mind2Web 2: check a submission, cache the pages its answers cite, evaluate the "
                    "answers, and compute the leaderboard metrics.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for command in COMMANDS:
        command.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)
