"""On-disk layout of an agent submission, per-answer metadata, and task lists.

A submission is a directory ``<answers_root>/<agent_name>/`` with one
subdirectory per task::

    <agent_name>/
    └── <task_id>/
        ├── answer_1.md          # run 1: the agent's first independent attempt
        ├── answer_1.meta.json   # optional metadata about producing answer_1.md
        ├── answer_2.md          # run 2
        └── ...

``answer_<k>.md`` holds the agent's markdown answer, with URL citations, for
run ``k``; the paper evaluates three runs per task.  ``k`` is a positive
integer without leading zeros (``answer_1.md``, not ``answer_01.md`` or
``answer_0.md``), so that each run has exactly one file name.  Evaluation and
metrics consider only files with exactly this name.  ``answer_<k>.meta.json`` is
optional and records facts the judge never sees, currently the agent's
wall-clock inference time (see :class:`AnswerMetadata`).

A task list names the tasks a split consists of.  It is given as a CSV file
with a ``task_id`` column (``dev_set.csv`` / ``test_set.csv`` from the Hugging
Face dataset, which also carry ``domain`` and ``subdomain``), a text file with
one task ID per line, or a directory of eval scripts named ``<task_id>.py``.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ANSWER_FILE_RE = re.compile(r"^answer_([1-9]\d*)\.md$")
META_FILE_RE = re.compile(r"^answer_([1-9]\d*)\.meta\.json$")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class AnswerMetadata(BaseModel):
    """Contents of ``answer_<k>.meta.json``.  Keys other than those below are kept as-is."""

    model_config = ConfigDict(extra="allow")

    time_seconds: float | None = Field(
        default=None, ge=0,
        description="Wall-clock time the agent took to produce the answer, in seconds.",
    )


class MetadataError(ValueError):
    """An ``answer_<k>.meta.json`` file that is not valid JSON or not valid :class:`AnswerMetadata`."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def metadata_path(answer_path: Path) -> Path:
    """Path of the metadata file that belongs to an answer: ``answer_1.md`` -> ``answer_1.meta.json``."""
    return answer_path.with_name(f"{answer_path.stem}.meta.json")


@dataclass(frozen=True)
class AnswerFile:
    """One ``answer_<run>.md`` file of a task."""

    task_id: str
    run: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def meta_path(self) -> Path:
        return metadata_path(self.path)


@dataclass(frozen=True)
class TaskInfo:
    """A task of a split; ``domain`` and ``subdomain`` are known only from CSV task lists."""

    task_id: str
    domain: str | None = None
    subdomain: str | None = None


def answer_run(filename: str) -> int | None:
    """Return ``k`` for a file named ``answer_<k>.md``, else ``None``."""
    match = ANSWER_FILE_RE.match(filename)
    return int(match.group(1)) if match else None


def list_answer_files(task_dir: Path) -> list[AnswerFile]:
    """Return the ``answer_<k>.md`` files of a task directory, ordered by run."""
    if not task_dir.is_dir():
        return []
    answers = []
    for path in task_dir.iterdir():
        run = answer_run(path.name)
        if run is not None and path.is_file():
            answers.append(AnswerFile(task_dir.name, run, path))
    return sorted(answers, key=lambda a: a.run)


def load_metadata(answer: AnswerFile) -> AnswerMetadata | None:
    """Return the answer's metadata, or ``None`` if it has no ``.meta.json`` file.

    Raises :class:`MetadataError` if the file exists but is not valid metadata.
    """
    path = answer.meta_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(path, f"not valid JSON ({exc})") from exc
    try:
        return AnswerMetadata.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'value'}: {e['msg']}" for e in exc.errors())
        raise MetadataError(path, details) from exc


def count_words(text: str) -> int:
    """Answer length in words: the number of whitespace-separated tokens of the markdown, URLs and markup included."""
    return len(text.split())


def load_task_list(source: Path) -> list[TaskInfo]:
    """Read a task list from a CSV file, a text file, or a directory of eval scripts.

    See the module docstring for the accepted formats.  Duplicate task IDs are
    dropped, keeping the first occurrence.  Raises ``OSError`` if the source
    cannot be read and ``ValueError`` if it is not UTF-8 text or is a CSV file
    without a ``task_id`` column.
    """
    source = Path(source)
    if source.is_dir():
        tasks = [TaskInfo(p.stem) for p in sorted(source.glob("*.py"))]
    elif source.suffix.lower() == ".csv":
        with source.open(newline="", encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            try:
                if not reader.fieldnames or "task_id" not in reader.fieldnames:
                    raise ValueError(f"{source}: CSV task list needs a 'task_id' column")
                rows = [(row["task_id"] or "", row.get("domain"), row.get("subdomain")) for row in reader]
            except csv.Error as exc:
                raise ValueError(f"{source}: not a valid CSV file ({exc})") from exc
        tasks = [TaskInfo(task_id.strip(), domain or None, subdomain or None)
                 for task_id, domain, subdomain in rows if task_id.strip()]
    else:
        lines = source.read_text(encoding="utf-8").splitlines()
        tasks = [TaskInfo(line.strip()) for line in lines if line.strip() and not line.startswith("#")]
    seen: set[str] = set()
    unique = []
    for task in tasks:
        if task.task_id not in seen:
            seen.add(task.task_id)
            unique.append(task)
    return unique


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubmissionIssue:
    """A problem found by :func:`validate_submission`.

    ``level`` is ``"error"`` when the problem breaks evaluation or the metrics,
    and ``"warning"`` when it costs the agent points or makes a file be ignored.
    ``path`` is relative to the submission directory (``"."`` for the directory
    itself).
    """

    level: str
    path: str
    message: str


def validate_submission(
        agent_dir: Path,
        task_ids: list[str] | None = None,
        num_runs: int | None = None,
) -> list[SubmissionIssue]:
    """Check a submission directory against the layout described in the module docstring.

    ``task_ids`` is the split's task list; without it, every task directory is
    checked and none is reported missing.  ``num_runs`` is the number of runs
    each task should have and defaults to the highest run index found.

    Reported as errors: the directory does not exist, an answer is not UTF-8
    text, or a ``.meta.json`` file is not valid :class:`AnswerMetadata`.
    Reported as warnings: a task or a run has no answer (it scores 0), an
    answer is empty or cites no URL, an answer's run index exceeds
    ``num_runs`` (it is not scored), a task directory is not in the task list
    (it is not scored), and a file in a task directory is neither an answer
    nor an answer's metadata (evaluation ignores it).
    """
    agent_dir = Path(agent_dir)
    if not agent_dir.is_dir():
        return [SubmissionIssue("error", ".", f"submission directory {agent_dir} does not exist")]

    issues: list[SubmissionIssue] = []
    task_dirs = {p.name: p for p in agent_dir.iterdir() if p.is_dir() and not p.name.startswith(".")}
    answers = {task_id: list_answer_files(d) for task_id, d in task_dirs.items()}
    if num_runs is None:
        num_runs = max((a.run for files in answers.values() for a in files), default=0)

    if task_ids is None:
        expected = sorted(task_dirs)
    else:
        expected = list(task_ids)
        for task_id in sorted(set(task_dirs) - set(task_ids)):
            issues.append(SubmissionIssue("warning", task_id, "not in the task list; it is not scored"))

    for task_id in expected:
        if task_id not in task_dirs:
            issues.append(SubmissionIssue("warning", task_id, "no task directory; every run scores 0"))
            continue
        files = answers[task_id]
        runs = {a.run for a in files}
        missing = [r for r in range(1, num_runs + 1) if r not in runs]
        if missing:
            issues.append(SubmissionIssue(
                "warning", task_id, f"no answer for run(s) {', '.join(map(str, missing))}; they score 0"))
        for answer in files:
            issues.extend(_check_answer(answer, num_runs))
        known = {a.name for a in files} | {a.meta_path.name for a in files}
        for path in sorted(task_dirs[task_id].iterdir()):
            if path.name.startswith(".") or path.name in known:
                continue
            reason = ("metadata without a matching answer file" if META_FILE_RE.match(path.name)
                      else "not named answer_<k>.md or answer_<k>.meta.json with k = 1, 2, 3, ...; "
                           "evaluation ignores it")
            issues.append(SubmissionIssue("warning", f"{task_id}/{path.name}", reason))
    return issues


def _check_answer(answer: AnswerFile, num_runs: int) -> list[SubmissionIssue]:
    where = f"{answer.task_id}/{answer.name}"
    issues = []
    if answer.run > num_runs:
        issues.append(SubmissionIssue("warning", where, f"run {answer.run} exceeds {num_runs} runs; it is not scored"))
    try:
        text = answer.path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        issues.append(SubmissionIssue("error", where, "not UTF-8 text"))
    else:
        if not text.strip():
            issues.append(SubmissionIssue("warning", where, "empty answer; it scores 0"))
        elif not _URL_RE.search(text):
            issues.append(SubmissionIssue(
                "warning", where, "cites no URL; verifications against cited sources will fail"))
    try:
        load_metadata(answer)
    except MetadataError as exc:
        issues.append(SubmissionIssue("error", f"{answer.task_id}/{answer.meta_path.name}", exc.reason))
    return issues
