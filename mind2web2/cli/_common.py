"""Options shared by several subcommands, so their names, defaults, and help stay identical."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..submission import TaskInfo, load_task_list


def positive_int(text: str) -> int:
    """``argparse`` type for a count: an integer of at least 1."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def positive_seconds(text: str) -> float:
    """``argparse`` type for a duration: a finite number of seconds greater than 0."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"must be a number of seconds greater than 0, got {text}")
    return value


def add_agent(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("agent", help="Agent name: the agent's directory name under the answers, cache, and results directories.")


def add_answers_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--answers-dir", type=Path, default=Path("answers"),
                        help="Directory holding <agent>/<task_id>/answer_<k>.md (default: %(default)s).")


def add_results_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--results-dir", type=Path, default=Path("eval_results"),
                        help="Directory the evaluation writes results to (default: %(default)s).")


def add_cache_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"),
                        help="Directory of the page caches, one per <agent>/<task_id> (default: %(default)s).")


def _task_list_help(default: str) -> str:
    return ("Tasks of the split: a CSV with a task_id column (dev_set.csv / test_set.csv from the Hugging Face "
            "dataset), a text file with one task ID per line, or a directory of eval scripts. "
            f"Default: {default}.")


def add_task_selection(parser: argparse.ArgumentParser, default: str) -> None:
    """``--task-list`` and ``--num-runs``.

    ``default`` says which tasks the command processes without ``--task-list``.
    """
    parser.add_argument("--task-list", type=Path, default=None, help=_task_list_help(default))
    parser.add_argument("--num-runs", type=positive_int, default=None,
                        help="Runs per task (the leaderboard uses 3). Default: the highest run index found.")


def add_task_filter(parser: argparse.ArgumentParser, default: str) -> None:
    """``--task-list`` or ``--task``: which of the agent's tasks a command processes.

    ``default`` says which tasks the command processes without either option.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--task-list", type=Path, default=None, help=_task_list_help(default))
    group.add_argument("--task", dest="tasks", action="append", metavar="TASK_ID", default=None,
                       help="Process only this task; repeat the option for several tasks.")


def answer_task_ids(agent_dir: Path) -> list[str]:
    """The task directories under an agent's answers directory, sorted; empty if it does not exist."""
    if not agent_dir.is_dir():
        return []
    return sorted(p.name for p in agent_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def selected_tasks(args: argparse.Namespace, discovered: list[str]) -> list[TaskInfo]:
    """The tasks given with ``--task``, else those of ``--task-list``, else the ``discovered`` task IDs."""
    if args.tasks:
        return [TaskInfo(task_id) for task_id in dict.fromkeys(args.tasks)]
    return resolve_tasks(args.task_list, discovered)


def resolve_tasks(task_list: Path | None, discovered: list[str]) -> list[TaskInfo]:
    """The tasks named by ``--task-list``, or the discovered task IDs when it is not given.

    Raises ``OSError`` or ``ValueError`` if the task list cannot be read (see
    :func:`~mind2web2.submission.load_task_list`).
    """
    if task_list is None:
        return [TaskInfo(task_id) for task_id in discovered]
    return load_task_list(task_list)
