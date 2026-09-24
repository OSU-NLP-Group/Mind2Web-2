"""On-disk layout of evaluation results.

For every evaluated answer, evaluation (``mind2web2 evaluate``) writes::

    <results_root>/<agent_name>/<task_id>/<answer_base>/
    ├── <answer_name>                               # copy of the evaluated answer
    ├── logs/
    └── results/
        ├── <timestamp>_<answer_name>.json          # output of Evaluator.get_summary()
        └── superseded/                             # results of earlier evaluations

``<answer_base>`` is the answer file name without its extension (``answer_1``
for ``answer_1.md``) and ``<timestamp>`` is ``YYYYMMDD_HHMMSS``.  The newest
file directly in ``results/`` is the answer's result.  Before an answer is
evaluated again, its earlier results move to ``results/superseded/``
(:func:`supersede_results`), so an evaluation that fails leaves the answer
without a result instead of with the result of an earlier answer or judge.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_TIMESTAMP_RE = re.compile(r"(\d{8})_?(\d{6})")
SUPERSEDED_DIR = "superseded"

#: Version of the framework's scoring logic, recorded in every result as
#: ``scoring_version``.  A result is reused only under the current version, so
#: raise it in any change to the framework that can change scores without
#: changing an eval script or an :class:`~mind2web2.evaluator.EvaluatorConfig`
#: default: the prompts and page handling in ``eval_toolkit``, PDF rendering,
#: what a failed capture means to the judge, or how rubric scores aggregate.
SCORING_VERSION = 2


def answer_base(answer_name: str) -> str:
    """Strip the extension from an answer file name: ``answer_3.md`` -> ``answer_3``."""
    return answer_name.rsplit(".", 1)[0]


def answer_output_dir(results_root: Path, agent_name: str, task_id: str, answer_name: str) -> Path:
    """Directory holding the copied answer, logs, and results of one answer."""
    return Path(results_root) / agent_name / task_id / answer_base(answer_name)


def result_file_name(timestamp: str, answer_name: str) -> str:
    return f"{timestamp}_{answer_name}.json"


def _timestamp_of(path: Path) -> datetime:
    match = _TIMESTAMP_RE.search(path.name)
    if not match:
        return datetime.min
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")


def latest_result_file(result_dir: Path) -> Path | None:
    """Return the ``*.json`` file in ``result_dir`` with the newest timestamp in its name."""
    if not result_dir.is_dir():
        return None
    candidates = [p for p in result_dir.iterdir() if p.suffix == ".json" and p.is_file()]
    return max(candidates, key=_timestamp_of) if candidates else None


def supersede_results(result_dir: Path) -> None:
    """Move every result file in ``result_dir`` into its ``superseded/`` subdirectory."""
    if not result_dir.is_dir():
        return
    stale = [p for p in result_dir.iterdir() if p.suffix == ".json" and p.is_file()]
    if stale:
        (result_dir / SUPERSEDED_DIR).mkdir(exist_ok=True)
    for path in stale:
        path.replace(result_dir / SUPERSEDED_DIR / path.name)


def load_latest_result(results_root: Path, agent_name: str, task_id: str, answer_name: str) -> dict | None:
    """Return the newest result of an answer, or ``None`` if it has none (or it is unreadable)."""
    result_dir = answer_output_dir(results_root, agent_name, task_id, answer_name) / "results"
    latest = latest_result_file(result_dir)
    if latest is None:
        return None
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError: not UTF-8, or not JSON
        return None
