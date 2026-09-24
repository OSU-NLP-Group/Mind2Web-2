"""Logs of an evaluation: one per evaluation of an answer, one per command run, and the console.

Two kinds of log files, each written in two formats:

- **An answer's log** (:func:`create_logger`): everything that happened while
  one answer was evaluated, in ``<results>/<agent>/<task>/<answer>/logs/``.
  The eval script and the evaluators write to it through the logger they are
  given.
- **A run's log** (:func:`configure_run_logging`): what a command did across
  tasks, such as each answer's outcome, in ``<results>/<agent>/logs/`` for
  ``mind2web2 evaluate``.  The command's console output is the same record at
  INFO and above, except records logged with ``extra={"console": False}``,
  which go to the files only (for what the command also prints itself).

Each log is a readable ``.log`` file (INFO and above, one line per event, with
the claim, the judge's reasoning, an extraction's result, or an error's
traceback indented below it; :class:`ReadableFormatter`) and a ``.jsonl`` file
(DEBUG and above, one JSON object per event with every field;
:class:`JsonLinesFormatter`).

The package's own modules (the judge client, the page cache, the browser)
log through ordinary module loggers under ``mind2web2``.  While a command's
logging is configured, a record they emit during an answer's evaluation (see
:func:`logging_to`) goes to that answer's log instead of the run's, so a judge
request's retries appear next to the check that made it.  Without
:func:`configure_run_logging`, as when the package is used as a library, those
module loggers propagate to the root logger as usual.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Iterator, Optional

#: Fields shown below an event's line in the readable format, in this order.
DETAIL_FIELDS = ("claim", "reasoning", "result", "error")
#: Longest value of a detail field in the readable format; the JSON Lines file keeps the whole value.
MAX_DETAIL_CHARS = 2000
_DETAIL_INDENT = " " * 23

# Attributes every LogRecord has; the others are the fields a call passed in ``extra``.
_RECORD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

_answer_logger: ContextVar[Optional[Logger]] = ContextVar("mind2web2_answer_logger", default=None)


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in vars(record).items()
            if k not in _RECORD_ATTRIBUTES and k != "console" and v is not None}


def _detail_text(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + " …"


class ReadableFormatter(logging.Formatter):
    """``HH:MM:SS.mmm LEVEL    message``, then the record's detail fields and traceback, indented.

    The detail fields are those of :data:`DETAIL_FIELDS` that the call passed
    in ``extra`` (a long value is cut after :data:`MAX_DETAIL_CHARS`
    characters); other fields appear only in the JSON Lines file, so the
    message must say what happened on its own.
    """

    def format(self, record: logging.LogRecord) -> str:
        time = f"{self.formatTime(record, '%H:%M:%S')}.{int(record.msecs):03d}"
        lines = [f"{time} {record.levelname:<8} {record.getMessage()}"]
        extras = _extras(record)
        for name in DETAIL_FIELDS:
            if name in extras:
                detail = _detail_text(extras[name]).replace("\n", "\n" + _DETAIL_INDENT + "  ")
                lines.append(f"{_DETAIL_INDENT}{name}: {detail}")
        if record.exc_info:
            lines.append(self.formatException(record.exc_info))
        return "\n".join(lines)


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per record: ``time`` (ISO 8601 with milliseconds), ``level``, ``logger``, ``message``,
    every field the call passed in ``extra``, and ``traceback`` when the record carries an exception."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """The message alone, prefixed with a colored ``warning:`` or ``error:`` for those levels.

    Colors are used only on a terminal and when ``NO_COLOR`` is unset.
    """

    _PREFIXES = {logging.WARNING: ("warning: ", "\033[33m"), logging.ERROR: ("error: ", "\033[31m"),
                 logging.CRITICAL: ("error: ", "\033[31m")}

    def __init__(self, stream=None):
        super().__init__()
        stream = stream or sys.stderr
        self.color = hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")

    def format(self, record: logging.LogRecord) -> str:
        prefix, color = self._PREFIXES.get(record.levelno, ("", ""))
        if prefix and self.color:
            prefix = f"{color}{prefix}\033[0m"
        return prefix + record.getMessage()


class TqdmConsoleHandler(logging.Handler):
    """Writes records to stderr through ``tqdm.write``, so that they print above the progress bars."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from tqdm import tqdm
            tqdm.write(self.format(record), file=sys.stderr)
        except Exception:
            self.handleError(record)


def _file_handlers(stem: Path) -> list[logging.Handler]:
    """A readable ``<stem>.log`` handler at INFO and a ``<stem>.jsonl`` handler at DEBUG."""
    stem.parent.mkdir(parents=True, exist_ok=True)
    readable = logging.FileHandler(f"{stem}.log", encoding="utf-8")
    readable.setLevel(logging.INFO)
    readable.setFormatter(ReadableFormatter())
    json_lines = logging.FileHandler(f"{stem}.jsonl", encoding="utf-8")
    json_lines.setLevel(logging.DEBUG)
    json_lines.setFormatter(JsonLinesFormatter())
    return [readable, json_lines]


_LOGGER_PREFIX = "mind2web2-log:"
_logger_ids = itertools.count(1)


def create_logger(lgr_nm: str, log_folder: str, enable_console: bool = True) -> tuple[Logger, str]:
    """A new logger that writes ``<log_folder>/<timestamp>_<lgr_nm>.log`` and ``.jsonl``, and its timestamp.

    ``timestamp`` is the local time as ``YYYYmmdd_HHMMSS``; an answer's result
    file carries the same timestamp as the log of the evaluation that made it.
    The logger does not propagate, so its records go only to its files and,
    with ``enable_console``, to the console at INFO and above.  Call
    :func:`cleanup_logger` when it is no longer needed, to close its files.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # A top-level name (no dots), outside the package's logger hierarchy, and unique in the process:
    # answers of different tasks share file names and can start in the same second
    logger = logging.getLogger(f"{_LOGGER_PREFIX}{next(_logger_ids)}:{lgr_nm}".replace(".", "_"))
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in _file_handlers(Path(log_folder) / f"{timestamp}_{lgr_nm}"):
        logger.addHandler(handler)
    if enable_console:
        console = TqdmConsoleHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(ConsoleFormatter())
        logger.addHandler(console)
    return logger, timestamp


def cleanup_logger(logger: Logger) -> None:
    """Remove and close every handler of ``logger``; a logger made by :func:`create_logger` is also forgotten."""
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    if logger.name.startswith(_LOGGER_PREFIX):
        logging.Logger.manager.loggerDict.pop(logger.name, None)


@contextmanager
def logging_to(logger: Logger) -> Iterator[None]:
    """Inside this block (and in asyncio tasks and threads started in it), the package's module loggers
    write to ``logger`` instead of the run's log, while :func:`configure_run_logging` is in effect."""
    token = _answer_logger.set(logger)
    try:
        yield
    finally:
        _answer_logger.reset(token)


class _ToAnswerLog(logging.Handler):
    """Sends a record to the log of the answer being evaluated, if any (see :func:`logging_to`)."""

    def emit(self, record: logging.LogRecord) -> None:
        target = _answer_logger.get()
        if target is not None and target.isEnabledFor(record.levelno):
            target.handle(record)


def _for_console(record: logging.LogRecord) -> bool:
    return getattr(record, "console", True)


class _OutsideAnswers(logging.Filter):
    """Passes only the records emitted outside an answer's evaluation; those inside go to its log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _answer_logger.get() is None


_PACKAGE = "mind2web2"
_run_handlers: list[logging.Handler] = []
_saved_package_state: Optional[tuple[int, bool]] = None


def configure_run_logging(log_dir: Optional[Path], name: str, console: bool = True) -> Optional[Path]:
    """Send the package's logging to a run log in ``log_dir`` and, with ``console``, to stderr.

    The run log is ``<log_dir>/<timestamp>_<name>.log`` and ``.jsonl``
    (no files without ``log_dir``); its path without the suffix is returned.
    Records of the ``mind2web2`` loggers emitted during an answer's evaluation
    go to that answer's log instead (:func:`logging_to`).  The ``mind2web2``
    logger stops propagating to the root logger until
    :func:`close_run_logging`, which a command calls when it ends.
    """
    global _saved_package_state
    close_run_logging()
    package = logging.getLogger(_PACKAGE)
    _saved_package_state = (package.level, package.propagate)
    package.setLevel(logging.DEBUG)
    package.propagate = False

    outside_answers = _OutsideAnswers()
    handlers: list[logging.Handler] = [_ToAnswerLog()]
    stem = None
    if log_dir is not None:
        stem = Path(log_dir) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
        handlers += _file_handlers(stem)
    if console:
        console_handler = TqdmConsoleHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(ConsoleFormatter())
        console_handler.addFilter(_for_console)
        handlers.append(console_handler)
    for handler in handlers[1:]:
        handler.addFilter(outside_answers)
    for handler in handlers:
        package.addHandler(handler)
    _run_handlers[:] = handlers
    return stem


def close_run_logging() -> None:
    """Undo :func:`configure_run_logging`: close the run log and let the ``mind2web2`` logger propagate again."""
    global _saved_package_state
    package = logging.getLogger(_PACKAGE)
    for handler in _run_handlers:
        package.removeHandler(handler)
        handler.close()
    _run_handlers.clear()
    if _saved_package_state is not None:
        package.setLevel(_saved_package_state[0])
        package.propagate = _saved_package_state[1]
        _saved_package_state = None
