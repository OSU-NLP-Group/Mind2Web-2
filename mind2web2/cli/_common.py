"""Options shared by several subcommands, so their names, defaults, and help stay identical."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..submission import TaskInfo, load_task_list


def add_agent(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("agent", help="Agent name: the agent's directory name under the answers and results directories.")


def add_answers_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--answers-dir", type=Path, default=Path("answers"),
                        help="Directory holding <agent>/<task_id>/answer_<k>.md (default: %(default)s).")


def add_results_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--results-dir", type=Path, default=Path("eval_results"),
                        help="Directory the evaluation writes results to (default: %(default)s).")


def add_task_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-list", type=Path, default=None,
                        help="Tasks of the split: a CSV with a task_id column (dev_set.csv / test_set.csv "
                             "from the Hugging Face dataset), a text file with one task ID per line, or a "
                             "directory of eval scripts. Default: the tasks the agent has answers for.")
    parser.add_argument("--num-runs", type=int, default=None,
                        help="Runs per task (the paper uses 3). Default: the highest run index found.")


def resolve_tasks(task_list: Path | None, discovered: list[str]) -> list[TaskInfo]:
    """The tasks named by ``--task-list``, or the discovered task IDs when it is not given."""
    if task_list is None:
        return [TaskInfo(task_id) for task_id in discovered]
    return load_task_list(task_list)
