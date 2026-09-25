"""An HTML page for browsing an agent's evaluation results: scores, rubric trees, and the evidence of each check.

:func:`write_report` reads ``<results_root>/<agent>/`` (layout in
:mod:`mind2web2.results`) and writes one self-contained HTML file, with no
external resources.  The page has a sidebar (a task filter, switches, and
every task with its mean score and a dot per run) and a main column with:

- the metrics saved by ``mind2web2 metrics`` or ``mind2web2 evaluate``, if
  any, each with a bar per run;
- two charts: how the answers' scores are distributed (full, partial, zero,
  no result) and how many checks passed, failed, or were skipped;
- a table of every task's score in every run, each cell linking to its
  answer, and each task's mean, which counts a run without a result as 0, as
  the metrics do (over runs 1 to the highest run of any task, or the metrics'
  ``num_runs`` if higher); it can be sorted by task or by mean;
- a section per task, naming the check that failed most often among its
  scored runs, with a card per answer.  A collapsed card shows the score, a square per check
  marked by its status (filled, slashed, or hollow), and the checks that
  lost points.  An open card
  starts with those failed checks and their evidence (the claim, the page and
  a thumbnail of its cached screenshot, the judge's votes and reasoning),
  then shows the rubric tree next to the answer text, then the rejected judge
  requests and the extracted information.

The latest result of each answer is shown, badged "changed" when the metrics
found that the answer changed after it and the result is not newer than the
metrics.  A check inside a step that a sequential node cut off (a step after
its first step below 1.0) counts as skipped, since its outcome did not reach
the score.  Thumbnails come from the page cache
(``<cache_root>/<agent>/<task_id>/``) when ``cache_root`` is given; they cover
the top of the page, and past :data:`THUMBNAIL_BUDGET_BYTES` the page adds no
new ones: each check left without one says so, and a note above the tasks
links to the first.  A
result of an unexpected shape shows as much as it can: its card says why
the rest cannot be shown, and the rest of the page is unaffected.

Every text taken from results and answers is HTML-escaped and only ``http(s)``
URLs become links; the page's Content Security Policy allows no external
resource (only inline styles and ``data:`` images) and only its own script,
identified by a nonce, so that a page quoting untrusted answers and judge
output cannot load or run anything else.  Charts are inline SVG and work
without the script; the script adds the filter, the switches, sorting, and
the keys ``/`` (filter), ``j`` and ``k`` (next and previous answer).
"""
from __future__ import annotations

import base64
import html
import io
import json
import logging
import hashlib
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from . import results
from .submission import answer_run

logger = logging.getLogger(__name__)

#: File name of the report in ``<results_root>/<agent>/``.
REPORT_FILE = "report.html"

#: Width in pixels of the thumbnail of a page's cached screenshot, and the height of the page top it shows.
THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT = 480, 720

#: Total size of the thumbnails one page embeds; failed checks past it show no thumbnail.
THUMBNAIL_BUDGET_BYTES = 8_000_000


@dataclass
class AnswerEntry:
    """One answer folder of the results: the answer's latest result and its text, where available."""

    task_id: str
    answer_name: str  # answer_<k>.md
    run: int
    result: Optional[dict]
    result_file: Optional[Path]
    answer_text: Optional[str]
    problem: Optional[str] = None  # why the result could not be read


def collect_answers(results_root: Path, agent: str, task_ids: Optional[Iterable[str]] = None) -> list[AnswerEntry]:
    """Every answer folder under ``<results_root>/<agent>/<task_id>/``, ordered by task and run.

    With ``task_ids``, only those tasks.  An answer folder is one named
    ``answer_<k>``; its result is the newest file in its ``results/`` folder
    (:func:`mind2web2.results.latest_result_file`), and its text the copy of
    ``answer_<k>.md`` that evaluation keeps next to it.  A result that cannot
    be read or is not a JSON object is ``None``, with the reason in ``problem``.
    """
    agent_dir = Path(results_root) / agent
    wanted = set(task_ids) if task_ids is not None else None
    entries = []
    task_dirs = sorted(p for p in agent_dir.iterdir() if p.is_dir()) if agent_dir.is_dir() else []
    for task_dir in task_dirs:
        if wanted is not None and task_dir.name not in wanted:
            continue
        for folder in task_dir.iterdir():
            run = answer_run(f"{folder.name}.md")
            if run is None or not folder.is_dir():
                continue
            answer_name = f"{folder.name}.md"
            text_path = folder / answer_name
            try:
                answer_text = text_path.read_text(encoding="utf-8") if text_path.is_file() else None
            except (OSError, UnicodeDecodeError):
                answer_text = None
            result_file = results.latest_result_file(folder / "results")
            result, problem = None, None
            if result_file is not None:
                try:
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    problem = f"{result_file.name} cannot be read: {exc}"
                if result is not None and not isinstance(result, dict):
                    result, problem = None, f"{result_file.name} cannot be read: not a JSON object"
            entries.append(AnswerEntry(task_dir.name, answer_name, run, result, result_file, answer_text, problem))
    return sorted(entries, key=lambda e: (e.task_id, e.run))


def write_report(results_root: Path, agent: str, output: Optional[Path] = None,
                 task_ids: Optional[Iterable[str]] = None, cache_root: Optional[Path] = None) -> Path:
    """Write the report of ``agent``'s results to ``output`` (default ``<results_root>/<agent>/report.html``).

    With ``cache_root``, failed checks show a thumbnail of the page they were
    checked against, read from ``<cache_root>/<agent>/<task_id>/``; the cache
    is only read.  With ``task_ids``, the task means still count runs up to
    the highest run of any of the agent's tasks, so a task's mean does not
    depend on which tasks the report includes.  Returns the path written.
    Raises ``FileNotFoundError`` when the agent has no answer folder in the
    results.
    """
    entries = collect_answers(results_root, agent, task_ids)
    if not entries:
        raise FileNotFoundError(f"no evaluated answers of {agent!r} under {Path(results_root) / agent}")
    metrics, metrics_time = None, None
    metrics_path = Path(results_root) / agent / "metrics.json"
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics_time = metrics_path.stat().st_mtime
        except (OSError, ValueError):
            metrics = None
        if not isinstance(metrics, dict):
            metrics, metrics_time = None, None
    thumbnails = Thumbnails(Path(cache_root) / agent) if cache_root is not None else None
    output = Path(output) if output is not None else Path(results_root) / agent / REPORT_FILE
    output.parent.mkdir(parents=True, exist_ok=True)
    num_runs = _highest_run(Path(results_root) / agent) if task_ids is not None else None
    output.write_text(render_report(agent, entries, metrics, thumbnails=thumbnails, metrics_time=metrics_time,
                                    num_runs=num_runs), encoding="utf-8")
    return output


def _highest_run(agent_dir: Path) -> int:
    """The highest run of any answer folder under ``agent_dir``, from the folder names alone."""
    runs = [answer_run(f"{folder.name}.md") for task_dir in agent_dir.iterdir() if task_dir.is_dir()
            for folder in task_dir.iterdir() if folder.is_dir()]
    return max((r for r in runs if r is not None), default=0)


class Thumbnails:
    """Thumbnails of cached screenshots for the pages of one agent's caches, each embedded once in the page.

    :meth:`get` looks a URL up in ``<agent_cache>/<task_id>/`` as evaluation
    does, converts the top :data:`THUMBNAIL_HEIGHT` pixels of its screenshot,
    scaled to :data:`THUMBNAIL_WIDTH` pixels wide, to a JPEG, and returns the
    ID and height of an SVG symbol that :meth:`defs_html` defines; every use of
    a thumbnail refers to that one symbol, so a page cited by many answers is
    embedded once.  It returns ``None`` for a URL without a cached screenshot
    (including PDFs), for a task without a cache directory (none is created),
    for an image that cannot be read, and, once the thumbnails so far reach
    :data:`THUMBNAIL_BUDGET_BYTES`, for every cached page not already
    embedded; that sets :attr:`over_budget`, and :meth:`dropped` tells such a
    page apart from one without a screenshot.
    """

    def __init__(self, agent_cache: Path, budget: int = THUMBNAIL_BUDGET_BYTES):
        self.agent_cache = Path(agent_cache)
        self.budget = budget
        self.used = 0
        self.over_budget = False
        self.first_dropped: Optional[str] = None  # anchor of the answer where the first thumbnail was left out
        self._dropped: set[tuple[str, str]] = set()
        self._caches: dict[str, object] = {}
        self._done: dict[tuple[str, str], Optional[tuple[str, int]]] = {}
        self._images: list[tuple[str, int, str]] = []  # symbol ID, height, base64 JPEG

    def get(self, task_id: str, url: str) -> Optional[tuple[str, int]]:
        key = (task_id, url)
        if key not in self._done:
            self._done[key] = self._make(task_id, url)
        return self._done[key]

    def dropped(self, task_id: str, url: str) -> bool:
        """Whether :meth:`get` returned ``None`` for this page only because the budget was used up."""
        return (task_id, url) in self._dropped

    def _cache(self, task_id: str):
        if task_id not in self._caches:
            from .utils.cache_filesys import CacheFileSys  # imported here: the report needs no cache otherwise
            task_dir = self.agent_cache / task_id
            cache = None
            if task_dir.is_dir():
                try:
                    cache = CacheFileSys(str(task_dir))
                except Exception as exc:  # an unreadable cache only costs the thumbnails
                    logger.warning("No thumbnails for task %s: %s", task_id, exc)
            self._caches[task_id] = cache
        return self._caches[task_id]

    def defs_html(self) -> str:
        """The SVG symbols of the thumbnails returned by :meth:`get`, for the end of the page."""
        symbols = "".join(f'<symbol id="{sid}" viewBox="0 0 {THUMBNAIL_WIDTH} {height}"><image width="{THUMBNAIL_WIDTH}" '
                          f'height="{height}" href="data:image/jpeg;base64,{data}"/></symbol>'
                          for sid, height, data in self._images)
        return f'<svg class="defs" aria-hidden="true" width="0" height="0">{symbols}</svg>' if symbols else ""

    def _make(self, task_id: str, url: str) -> Optional[tuple[str, int]]:
        cache = self._cache(task_id)
        if cache is None:
            return None
        try:
            if cache.has(url) != "web":
                return None
            if self.over_budget:
                self._dropped.add((task_id, url))
                return None
            _, screenshot = cache.get_web(url, get_screenshot=True)
            if not screenshot:
                return None
            from PIL import Image
            with Image.open(io.BytesIO(screenshot)) as image:
                image = image.convert("RGB")
                height = round(image.height * THUMBNAIL_WIDTH / image.width)
                image = image.resize((THUMBNAIL_WIDTH, max(height, 1)), Image.LANCZOS)
                image = image.crop((0, 0, THUMBNAIL_WIDTH, min(image.height, THUMBNAIL_HEIGHT)))
                thumb_height = image.height
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=60, optimize=True)
        except Exception as exc:
            logger.warning("No thumbnail of %s in task %s: %s", url, task_id, exc)
            return None
        data = base64.b64encode(buffer.getvalue()).decode()
        if self.used + len(data) > self.budget:
            self.over_budget = True
            self._dropped.add((task_id, url))
            return None
        self.used += len(data)
        symbol = (f"th{len(self._images)}", thumb_height)
        self._images.append((*symbol, data))
        return symbol


# --------------------------------------------------------------------------- data

_EPS = 1e-6

#: The largest ``num_runs`` from ``metrics.json`` the report accepts; a larger value is taken as a damaged file.
_MAX_RECORDED_RUNS = 100


def _score(entry: AnswerEntry) -> Optional[float]:
    try:
        return float(entry.result["final_score"]) if entry.result is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _shown_score(entry: AnswerEntry, stale: set[tuple[str, int]]) -> Optional[float]:
    """The score the report shows: none for a changed answer, whose result scored an older text."""
    return None if (entry.task_id, entry.run) in stale else _score(entry)


def _tree(entry: AnswerEntry) -> Optional[dict]:
    """The answer's rubric tree, with the checks inside cut-off steps marked (see :func:`_skip_within`), or
    ``None`` when the result has no tree of the expected shape."""
    breakdown = entry.result.get("eval_breakdown") if entry.result is not None else None
    if not isinstance(breakdown, list) or not breakdown or not isinstance(breakdown[0], dict):
        return None
    tree = breakdown[0].get("verification_tree")
    if not isinstance(tree, dict):
        return None
    _skip_within(tree)
    return tree


def _kids(node: dict) -> list[dict]:
    """``node``'s children that are nodes; a result of another shape has none."""
    children = node.get("children")
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _below_one(node: dict) -> bool:
    try:
        return float(node.get("score")) < 1.0
    except (TypeError, ValueError):
        return True


def _skip_within(node: dict, within: Optional[str] = None) -> None:
    """Mark the nodes inside the steps a sequential node cut off as skipped, naming the step in ``_within``.

    A sequential node scores every step after its first step below 1.0 as 0
    and marks those steps skipped; the checks verified inside such a step keep
    their own status, which did not reach the score.  Each cut-off step
    records the step it came after in ``_after``.  Other skipped nodes (an
    aggregate that scored 0 without a failed child) are not cut-off steps, so
    the checks inside them keep their status.  Marking is idempotent.
    """
    if within is not None and node.get("status") != "skipped":
        node["status"], node["_within"] = "skipped", within
    kids = _kids(node)
    cut = None
    if within is None and node.get("strategy") == "sequential":
        cut = next((i for i, child in enumerate(kids) if _below_one(child)), None)
    for i, child in enumerate(kids):
        if cut is not None and i > cut:
            child["_after"] = str(kids[cut].get("id"))
            for grandchild in _kids(child):
                _skip_within(grandchild, str(child.get("id")))
        else:
            _skip_within(child, within)


def _leaves(node: dict) -> list[dict]:
    """The leaves of ``node``'s tree in reading order; ``node`` itself when it has no children."""
    kids = _kids(node)
    if not kids:
        return [node]
    return [leaf for child in kids for leaf in _leaves(child)]


def _has_evidence(node: dict) -> bool:
    return bool(node.get("evidence")) or any(_has_evidence(c) for c in _kids(node))


def _score_class(score: Optional[float]) -> str:
    if score is None:
        return "none"
    if score >= 1 - _EPS:
        return "pass"
    return "partial" if score > 0 else "fail"


# --------------------------------------------------------------------------- small pieces

def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url) -> str:
    """``url`` as a link whose host stands out when it is an http(s) URL, otherwise as escaped text."""
    text = "" if url is None else str(url)
    if not text.lower().startswith(("http://", "https://")):
        return f"<code>{_e(text)}</code>"
    try:
        host = urlsplit(text).netloc
    except ValueError:  # e.g. an unbalanced "[" in the host
        return f"<code>{_e(text)}</code>"
    rest = text[text.find(host) + len(host):] if host and host in text else ""
    shown = f"<b>{_e(host)}</b>{_e(rest)}" if host else _e(text)
    return f'<a class="url" href="{_e(text)}" target="_blank" rel="noreferrer noopener">{shown}</a>'


def _anchor(entry: AnswerEntry) -> str:
    return f"a-{_slug(entry.task_id)}-{entry.run}"


def _task_anchor(task_id: str) -> str:
    return "t-" + _slug(task_id)


def _slug(text: str) -> str:
    """``text`` with every character but letters, digits, ``-`` and ``_`` replaced by ``-``; a replacement adds a
    short hash of ``text``, so that two task IDs never share a slug."""
    slug = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in text)
    return slug if slug == text else f"{slug}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def _bar(score: Optional[float]) -> str:
    """A horizontal bar filled to ``score`` (0 to 1)."""
    width = 0 if score is None else max(0.0, min(1.0, score)) * 100
    return (f'<span class="bar" aria-hidden="true"><span class="fill {_score_class(score)}" '
            f'style="width:{width:.1f}%"></span></span>')


def _strip(leaves: list[dict]) -> str:
    """A square per check, marked by its status (filled when passed, slashed when failed, hollow when skipped),
    each titled with the check's ID; the strip's label lists every check and status for screen readers."""
    squares = "".join(f'<i class="sq {_e(str(leaf.get("status", "")))}" '
                      f'title="{_e(leaf.get("id"))}: {_e(leaf.get("status"))}"></i>' for leaf in leaves)
    label = "checks: " + ", ".join(f"{leaf.get('id')} {leaf.get('status')}" for leaf in leaves)
    return f'<span class="strip" role="img" aria-label="{_e(label)}">{squares}</span>'


def _spark(values: list, top: Optional[float] = None) -> str:
    """A bar per run, scaled to ``top`` (default: the largest value), as inline SVG."""
    numbers = [v for v in values if isinstance(v, (int, float))]
    if len(values) < 2 or not numbers:
        return ""
    top = top or max(numbers) or 1
    width, height, gap = 14, 30, 4
    bars = []
    for i, value in enumerate(values):
        x = i * (width + gap)
        if not isinstance(value, (int, float)):
            bars.append(f'<rect x="{x}" y="{height - 2}" width="{width}" height="2" class="sp-none">'
                        f"<title>run {i + 1}: no value</title></rect>")
            continue
        h = max(2.0, height * min(value / top, 1.0))
        bars.append(f'<rect x="{x}" y="{height - h:.1f}" width="{width}" height="{h:.1f}" rx="2" class="sp">'
                    f"<title>run {i + 1}: {value:.4g}</title></rect>")
    total = len(values) * (width + gap) - gap
    return (f'<svg class="spark" width="{total}" height="{height}" viewBox="0 0 {total} {height}" '
            f'role="img" aria-label="value per run">{"".join(bars)}</svg>')


def _stacked(title: str, segments: list[tuple[str, str, int]]) -> str:
    """A 100% stacked bar of ``(label, css class, count)`` segments with a legend, as inline SVG."""
    total = sum(n for _, _, n in segments)
    if not total:
        return ""
    x, rects = 0.0, []
    for label, cls, n in segments:
        if not n:
            continue
        w = 1000 * n / total
        rects.append(f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="22" class="{cls}">'
                     f"<title>{_e(label)}: {n}</title></rect>")
        x += w
    legend = "".join(f'<span><i class="key {cls}"></i>{_e(label)} <b>{n}</b></span>'
                     for label, cls, n in segments if n)
    return (f'<figure class="stacked"><figcaption>{_e(title)}</figcaption>'
            f'<svg viewBox="0 0 1000 22" preserveAspectRatio="none" role="img" aria-label="{_e(title)}">'
            f'{"".join(rects)}</svg><div class="legend">{legend}</div></figure>')


# --------------------------------------------------------------------------- sections

def _metrics_html(metrics: Optional[dict]) -> str:
    if not metrics:
        return ""

    def stat(key: str, fmt: str = "{:.4f}") -> tuple[str, list]:
        value = metrics.get(key)
        if not isinstance(value, dict) or value.get("mean") is None:
            return "–", []
        return f"{fmt.format(value['mean'])} ± {fmt.format(value.get('std') or 0)}", value.get("per_run") or []

    pass_at_k = metrics.get("pass_at_k") or {}
    tiles = [("Partial Completion", *stat("partial_completion"), 1.0),
             ("Success Rate", *stat("success_rate"), 1.0),
             (f"Pass@{pass_at_k.get('k', 'k')}",
              f"{pass_at_k['value']:.4f}" if pass_at_k.get("value") is not None else "–", [], 1.0),
             ("Time (min)", *stat("time_minutes", "{:.2f}"), None),
             ("Answer length (words)", *stat("answer_length_words", "{:.0f}"), None)]
    counts = []
    for key, label in (("missing_answers", "missing answers"), ("missing_results", "answers without a result"),
                       ("stale_results", "answers changed since their result"),
                       ("rejected_requests", "answers with rejected judge requests")):
        if metrics.get(key):
            counts.append(f"{len(metrics[key])} {label}")
    warnings = []
    for key, what in (("judge_models", "different judge models"),
                      ("served_models", "different models, as the server reported them")):
        if len(metrics.get(key) or {}) > 1:
            models = ", ".join(f"{model}: {n}" for model, n in metrics[key].items())
            warnings.append(f"Results come from {what} ({models}); re-evaluate with one judge before comparing "
                            f"scores.")
    judges = ", ".join(metrics.get("judge_models") or {})
    tile_html = "".join(
        f'<div class="tile"><span class="muted">{_e(name)}</span><b>{_e(value)}</b>{_spark(per_run, top)}</div>'
        for name, value, per_run, top in tiles)
    return (
        '<section class="card" id="metrics"><h2>Metrics</h2>'
        f'<p class="muted">From metrics.json: {_e(metrics.get("num_tasks"))} tasks × '
        f'{_e(metrics.get("num_runs"))} runs' + (f" · judged by {_e(judges)}" if judges else "")
        + (f" · {_e(', '.join(counts))}" if counts else "") + "</p>"
        f'<div class="tiles">{tile_html}</div>'
        + "".join(f'<p class="note">{_e(w)}</p>' for w in warnings) + "</section>"
    )


def _charts_html(entries: list[AnswerEntry], stale: set[tuple[str, int]]) -> str:
    buckets = Counter()
    statuses = Counter()
    for entry in entries:
        score = _shown_score(entry, stale)
        buckets["none" if score is None else _score_class(score)] += 1
        tree = _tree(entry)
        if tree is not None and score is not None:
            statuses.update(str(leaf.get("status", "")) for leaf in _leaves(tree))
    answers = _stacked("Answers by score", [("1.0", "pass", buckets["pass"]),
                                            ("between 0 and 1", "partial", buckets["partial"]),
                                            ("0", "fail", buckets["fail"]),
                                            ("no result or changed", "none", buckets["none"])])
    checks = _stacked("Checks of the scored answers", [("passed", "pass", statuses["passed"]),
                                                       ("failed", "fail", statuses["failed"]),
                                                       ("skipped", "none", statuses["skipped"])])
    return f'<section class="card charts" id="charts">{answers}{checks}</section>'


_MEAN_NOTE = "Changed answers, answers without a result, and runs without an answer count as 0, as in the metrics"


def _by_task(entries: list[AnswerEntry]) -> dict[str, dict[int, AnswerEntry]]:
    by_task: dict[str, dict[int, AnswerEntry]] = {}
    for entry in entries:
        by_task.setdefault(entry.task_id, {})[entry.run] = entry
    return by_task


def _task_summary(cells: dict[int, AnswerEntry], runs: list[int],
                  stale: set[tuple[str, int]]) -> tuple[float, bool, list[Optional[float]]]:
    """A task's mean over ``runs`` (a run without an answer or a result counts as 0), whether it is below 1.0,
    and its shown score per run (``None`` where there is none)."""
    shown = [(_shown_score(cells[r], stale) if r in cells else None) for r in runs]
    mean = sum(s or 0.0 for s in shown) / len(runs)
    below = any(s is None or s < 1 - _EPS for s in shown)
    return mean, below, shown


def _matrix_html(entries: list[AnswerEntry], stale: set[tuple[str, int]], runs: list[int]) -> str:
    """The table of every task's score in every run, and the task's mean over the runs.

    A pair in ``stale`` (the metrics' ``stale_results``) shows "changed"
    instead of its score.  As in the metrics, a changed answer, an answer
    without a result, and a run without an answer count as 0 in the mean,
    over ``runs``.
    """
    head = "".join(f"<th class=num>run {r}</th>" for r in runs)
    rows = []
    for task_id, cells in _by_task(entries).items():
        mean, below, shown = _task_summary(cells, runs, stale)
        tds = []
        for r, score in zip(runs, shown):
            entry = cells.get(r)
            if entry is None:
                tds.append('<td class="cell none" title="no answer">·</td>')
                continue
            label = f"{score:.2f}" if score is not None else "changed" if (task_id, r) in stale else "no result"
            tds.append(f'<td class="cell {_score_class(score)}"><a href="#{_anchor(entry)}">{label}</a></td>')
        rows.append(f'<tr data-task="{_e(task_id)}" data-below="{int(below)}" data-mean="{mean:.6f}">'
                    f'<td><a class="task-link" href="#{_task_anchor(task_id)}"><code>{_e(task_id)}</code></a></td>'
                    f'{"".join(tds)}<td class=num>{mean:.3f}</td><td class="meanbar">{_bar(mean)}</td></tr>')
    return ('<section class="card" id="scores"><div class="head-row"><h2>Scores</h2>'
            '<span class="sorter muted small">sort by <button type="button" data-sort="task" aria-pressed="true">'
            'task</button>'
            '<button type="button" data-sort="mean" aria-pressed="false">mean</button></span></div>'
            '<div class="scroll"><table class="matrix">'
            f'<thead><tr><th>task</th>{head}<th class=num title="{_MEAN_NOTE}">mean</th><th></th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


def _nav_html(entries: list[AnswerEntry], stale: set[tuple[str, int]], runs: list[int]) -> str:
    items = []
    for task_id, cells in _by_task(entries).items():
        mean, below, shown = _task_summary(cells, runs, stale)
        dots = "".join(f'<i class="dot {_score_class(s)}" title="run {r}: '
                       f'{"none" if s is None else f"{s:.2f}"}"></i>' for r, s in zip(runs, shown))
        items.append(f'<li data-task="{_e(task_id)}" data-below="{int(below)}"><a href="#{_task_anchor(task_id)}">'
                     f'<span class="name">{_e(task_id)}</span><span class="dots">{dots}</span>'
                     f'<span class="num">{mean:.2f}</span></a></li>')
    return f'<ul class="tasknav">{"".join(items)}</ul>'


def _votes_html(votes: list) -> str:
    if len(votes) < 2:
        return ""
    dots = "".join(f'<i class="vote {"yes" if v else "no"}"></i>' for v in votes)
    return (f'<span class="votes" title="{sum(bool(v) for v in votes)} of {len(votes)} votes pass">{dots}'
            f'<span class="muted">{sum(bool(v) for v in votes)}/{len(votes)}</span></span>')


def _check_html(check: dict, thumbnail=None) -> str:
    """One judgment of a check; ``thumbnail`` is a symbol from :meth:`Thumbnails.get`, or ``"dropped"`` when the
    thumbnail budget left this page out."""
    passed = check.get("passed")
    verdict = "passed" if passed else "failed"
    source = _link(check["url"]) if check.get("url") else '<span class="muted">no source: judged from the answer</span>'
    parts = [f'<div class="check {verdict}"><div class="check-head"><span class="chip {verdict}">{verdict}</span> '
             f'{source} {_votes_html(check.get("votes") or [])}</div>']
    body = []
    if check.get("note"):
        body.append(f'<div class="note">{_e(check["note"])}</div>')
    if check.get("reasoning"):
        body.append(f'<div class="reasoning">{_e(check["reasoning"])}</div>')
    if thumbnail == "dropped":
        body.append(f'<div class="muted small">No thumbnail: the report\'s {THUMBNAIL_BUDGET_BYTES // 1_000_000} MB '
                    "of thumbnails is used up.</div>")
        thumbnail = None
    if thumbnail:
        parts.append(f'<div class="check-body with-thumb"><div>{"".join(body)}</div>'
                     f'<figure class="thumb"><svg viewBox="0 0 {THUMBNAIL_WIDTH} {thumbnail[1]}" role="img" '
                     f'aria-label="top of the cached screenshot of the page"><use href="#{thumbnail[0]}"/></svg>'
                     f"<figcaption>top of the cached screenshot</figcaption></figure></div>")
    else:
        parts.append(f'<div class="check-body">{"".join(body)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _evidence_rows(evidence: dict, thumbnail_of=None) -> list[str]:
    rows = [f'<div class="claim"><span class="muted">claim</span> {_e(evidence.get("claim"))}</div>']
    if evidence.get("skipped_because"):
        rows.append(f'<div class="note">Skipped: check <code>{_e(evidence["skipped_because"])}</code>, '
                    f"which it depends on, did not pass.</div>")
    if evidence.get("error"):
        rows.append(f'<div class="note">Failed with an error: {_e(evidence["error"])}</div>')
    checks = evidence.get("checks") or []
    for check in checks:
        thumb = thumbnail_of(check) if thumbnail_of is not None and not check.get("passed") else None
        rows.append(_check_html(check, thumb))
    sources = evidence.get("sources") or []
    unchecked = [u for u in sources if u not in {c.get("url") for c in checks}]
    if unchecked and not evidence.get("skipped_because"):
        rows.append('<div class="muted small">Not checked (another source already supported the claim, '
                    "or the check stopped): " + ", ".join(_link(u) for u in unchecked) + "</div>")
    return rows


def _node_html(node: dict) -> str:
    status = str(node.get("status", ""))
    score = node.get("score")
    children = _kids(node)
    tags = [str(node.get("strategy", ""))] + (["critical"] if node.get("critical") else [])
    score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else ""
    head = (f'<div class="node-head"><span class="chip {_e(status)}">{_e(status)}</span>'
            f'<code class="id">{_e(node.get("id"))}</code><span class="score">{score_text}</span>'
            f'<span class="tags">{_e(" · ".join(t for t in tags if t))}</span>'
            f'<span class="desc">{_e(node.get("desc"))}</span></div>')
    evidence = ""
    found = node.get("evidence")
    if node.get("_within"):
        evidence = (f'<div class="skipnote">skipped: inside <code>{_e(node["_within"])}</code>, which was skipped'
                    "</div>")
        if found:
            evidence += (f'<details class="evidence"><summary>evidence</summary>'
                         f'{"".join(_evidence_rows(found))}</details>')
    elif found and status == "skipped" and found.get("skipped_because"):
        evidence = (f'<div class="skipnote">skipped: depends on <code>{_e(found["skipped_because"])}</code>, '
                    f"which did not pass</div>")
    elif status == "skipped" and node.get("_after"):
        evidence = (f'<div class="skipnote">skipped: comes after <code>{_e(node["_after"])}</code>, which scored '
                    "below 1.0</div>")
        if found:
            evidence += (f'<details class="evidence"><summary>evidence</summary>'
                         f'{"".join(_evidence_rows(found))}</details>')
    elif found:
        evidence = (f'<details class="evidence"><summary>evidence</summary>'
                    f'{"".join(_evidence_rows(found))}</details>')
    kids = f'<ul>{"".join(_node_html(c) for c in children)}</ul>' if children else ""
    leaf = "" if children else " leaf"
    return f'<li class="node s-{_e(status)}{leaf}">{head}{evidence}{kids}</li>'


def _lost_html(entry: AnswerEntry, failed: list[dict], thumbnails: Optional[Thumbnails]) -> str:
    """The failed checks of an answer, each with its evidence and, for a failed page check, a thumbnail."""
    def thumbnail_of(check: dict):
        if thumbnails is None or not check.get("url"):
            return None
        found = thumbnails.get(entry.task_id, check["url"])
        if found is None and thumbnails.dropped(entry.task_id, check["url"]):
            if thumbnails.first_dropped is None:
                thumbnails.first_dropped = _anchor(entry)
            return "dropped"
        return found

    items = []
    for leaf in failed:
        rows = _evidence_rows(leaf["evidence"], thumbnail_of) if leaf.get("evidence") else []
        items.append(f'<li><div class="node-head"><span class="chip failed">failed</span>'
                     f'<code class="id">{_e(leaf.get("id"))}</code>'
                     f'<span class="tags">{"critical" if leaf.get("critical") else ""}</span>'
                     f'<span class="desc">{_e(leaf.get("desc"))}</span></div>{"".join(rows)}</li>')
    return (f'<section class="lost"><h4>Where points were lost ({len(failed)} failed '
            f'check{"s" if len(failed) != 1 else ""})</h4><ul>{"".join(items)}</ul></section>')


def _answer_html(entry: AnswerEntry, stale: set[tuple[str, int]], thumbnails: Optional[Thumbnails]) -> str:
    """One answer's collapsed card; an answer in ``stale`` is badged "changed" and counts as below 1.0."""
    changed = (entry.task_id, entry.run) in stale
    score = _shown_score(entry, stale)
    label = f"{score:.3f}" if score is not None else "changed" if changed else "no result"
    badge = f'<span class="badge {_score_class(score)}">{label}</span>'
    tree = _tree(entry)
    leaves = _leaves(tree) if tree is not None else []
    tally = Counter(str(leaf.get("status", "")) for leaf in leaves)
    failed = [leaf for leaf in leaves if leaf.get("status") == "failed"]
    summary_line = ""
    if leaves:
        counts = " · ".join(f"{tally[s]} {s}" for s in ("passed", "failed", "skipped") if tally[s])
        lost = ""
        if failed:
            shown = ", ".join(f"<code>{_e(leaf.get('id'))}</code>" for leaf in failed[:3])
            lost = f' · <span class="lostids">lost at {shown}{" …" if len(failed) > 3 else ""}</span>'
        summary_line = f'<span class="sumline">{_strip(leaves)}<span class="muted small">{counts}{lost}</span></span>'
    body = []
    if changed:
        body.append('<p class="note">The answer file changed after this result was saved, so the metrics count '
                    "it as 0; evaluate the answer again to score its current text. The result below is the old "
                    "one.</p>")
    result = entry.result
    if result is None:
        problem = entry.problem or ("This answer has no result: its evaluation failed or has not run. "
                                    "The reason is in its log, in the logs/ folder next to it.")
        body.append(f'<p class="note">{_e(problem)}</p>')
    else:
        judge = result.get("judge") or {}
        usage = result.get("judge_usage") or {}
        facts = [f"judge {judge.get('model') or result.get('judge_model')}"]
        if judge.get("reasoning_effort"):
            facts.append(f"reasoning effort {judge['reasoning_effort']}")
        if usage.get("requests") is not None:
            facts.append(f"{usage['requests']} judge requests")
        if result.get("eval_date"):
            facts.append(f"evaluation date {result['eval_date']}")
        if entry.result_file is not None:
            facts.append(entry.result_file.name)
        body.append(f'<p class="muted small">{_e(" · ".join(str(f) for f in facts))}</p>')
        if failed:
            body.append(_lost_html(entry, failed, thumbnails))
    panes = []
    if tree is not None:
        note = ("" if _has_evidence(tree) else
                '<p class="muted small">This result was saved without the evidence of its checks; evaluate the '
                "answer again with <code>--overwrite</code> to record it.</p>")
        panes.append(f'<div class="pane-tree"><h4>Rubric</h4>{note}<ul class="tree">{_node_html(tree)}</ul></div>')
    if entry.answer_text is not None:
        panes.append(f'<details class="pane-answer"><summary>answer text ({len(entry.answer_text):,} characters)'
                     f'</summary><pre class="answer">{_e(entry.answer_text)}</pre></details>')
    if panes:
        body.append(f'<div class="panes">{"".join(panes)}</div>')
    if result is not None:
        usage = result.get("judge_usage") or {}
        rejections = usage.get("rejections") or []
        if rejections:
            items = "".join(f"<li><code>{_e(r.get('check'))}</code> {_link(r['url']) if r.get('url') else ''}: "
                            f"{_e(r.get('reason'))}</li>" for r in rejections)
            body.append(f'<details><summary>{len(rejections)} judge requests rejected for their content</summary>'
                        f"<ul>{items}</ul></details>")
        breakdown = (result.get("eval_breakdown") or [{}])[0]
        info = breakdown.get("info") if isinstance(breakdown, dict) else None
        if info:
            body.append('<details><summary>extracted information</summary><pre class="json">'
                        f"{_e(json.dumps(info, ensure_ascii=False, indent=2))}</pre></details>")
    below = "1" if score is None or score < 1 - _EPS else "0"
    return (f'<details class="answer-card card" id="{_anchor(entry)}" data-task="{_e(entry.task_id)}" '
            f'data-below="{below}"><summary><span class="title">{_e(entry.answer_name)}'
            f'<span class="muted small"> run {entry.run}</span></span>{summary_line}'
            f'<span class="scorebox">{_bar(score)}{badge}</span></summary>'
            f'<div class="card-body">{"".join(body)}</div></details>')


def _safe_answer_html(entry: AnswerEntry, stale: set[tuple[str, int]], thumbnails: Optional[Thumbnails]) -> str:
    """:func:`_answer_html`, or a card saying why the answer cannot be shown when its result has an unexpected
    shape, so that one result cannot stop the whole report."""
    try:
        return _answer_html(entry, stale, thumbnails)
    except Exception as exc:
        logger.warning("Report: answer %s of task %s not shown: %r", entry.answer_name, entry.task_id, exc)
        return (f'<details class="answer-card card" id="{_anchor(entry)}" data-task="{_e(entry.task_id)}" '
                f'data-below="1"><summary><span class="title">{_e(entry.answer_name)}<span class="muted small"> run '
                f'{entry.run}</span></span><span class="scorebox"><span class="badge none">not shown</span></span>'
                f'</summary><div class="card-body"><p class="note">This result cannot be shown: '
                f"{_e(type(exc).__name__)}: {_e(exc)}</p></div></details>")


def _task_html(task_id: str, cells: dict[int, AnswerEntry], runs: list[int], stale: set[tuple[str, int]],
               thumbnails: Optional[Thumbnails]) -> str:
    mean, below, _ = _task_summary(cells, runs, stale)
    failures = Counter()
    position: dict[str, int] = {}  # a check's earliest position in the trees, for ties
    scored = 0
    for entry in cells.values():
        tree = _tree(entry)
        if tree is None or _shown_score(entry, stale) is None:
            continue
        scored += 1
        leaves = _leaves(tree)
        for i, leaf in enumerate(leaves):
            position[str(leaf.get("id"))] = min(i, position.get(str(leaf.get("id")), i))
        failures.update({str(leaf.get("id")) for leaf in leaves if leaf.get("status") == "failed"})
    common = ""
    if failures:
        check = min(failures, key=lambda c: (-failures[c], position[c]))
        n = failures[check]
        common = (f'<span class="muted small">most frequent failure: <code>{_e(check)}</code> failed in {n} of '
                  f"{scored} scored run{'s' if scored != 1 else ''}</span>")
    cards = "".join(_safe_answer_html(cells[r], stale, thumbnails) for r in sorted(cells))
    return (f'<section class="task" id="{_task_anchor(task_id)}" data-task="{_e(task_id)}" data-below="{int(below)}">'
            f'<div class="task-head"><h3><code>{_e(task_id)}</code></h3><span class="num">mean {mean:.3f}</span>'
            f"{_bar(mean)}{common}</div>{cards}</section>")


_CSS = """
:root{--bg:#f5f6f4;--card:#fff;--ink:#17201d;--ink2:#34403b;--muted:#5f6d68;--rule:#dde3e0;--pass:#1b7f4a;
--pass-bg:#dff1e6;--pass-fill:#2e9e62;--partial:#8a5a00;--partial-bg:#fbeed2;--partial-fill:#e0a526;--fail:#b3261e;
--fail-bg:#fbe4e1;--fail-fill:#d9493f;--none-bg:#eceff0;--none-fill:#b9c2bf;--accent:#2f5da8;--code:#eef2f0;
--side:#eef1ee;--shadow:0 1px 2px rgba(0,0,0,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1513;--card:#161e1b;--ink:#e3ebe8;--ink2:#c6d2cd;--muted:#90a19b;
--rule:#28332f;--pass:#62c790;--pass-bg:#15301f;--pass-fill:#3fae72;--partial:#e2ab4a;--partial-bg:#352812;
--partial-fill:#c98f22;--fail:#f07b70;--fail-bg:#3a1916;--fail-fill:#d5584d;--none-bg:#1d2623;--none-fill:#4a5753;
--accent:#8aadea;--code:#111a17;--side:#121916;--shadow:none}}
*{box-sizing:border-box}
html{scroll-padding-top:12px}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.layout{display:grid;grid-template-columns:1fr;min-height:100vh}
.side{background:var(--side);border-bottom:1px solid var(--rule);padding:16px}
main{padding:20px 16px 72px;display:grid;gap:16px;min-width:0;max-width:1320px;width:100%;margin:0 auto}
main>*,.card-body>*,.panes>*{min-width:0}
@media (min-width:1000px){.layout{grid-template-columns:280px minmax(0,1fr)}
.side{position:sticky;top:0;height:100vh;overflow:auto;border-bottom:0;border-right:1px solid var(--rule)}
main{padding:28px 32px 96px}}
h1{font-size:26px;line-height:1.2;margin:0}h2{font-size:18px;margin:0 0 10px}h3{font-size:16px;margin:0}
h4{font-size:14px;margin:0 0 8px;color:var(--ink2)}
a{color:var(--accent)}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
code{background:var(--code);padding:0 4px;border-radius:4px;overflow-wrap:anywhere}
.muted{color:var(--muted)}.small{font-size:13px}.num{font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}
.brand{font:600 12px system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.side .agent{font-weight:700;font-size:17px;margin:2px 0 12px;overflow-wrap:anywhere}
.controls{display:grid;gap:8px;margin-bottom:14px}
.controls input[type=search]{width:100%;padding:7px 10px;border:1px solid var(--rule);border-radius:8px;
background:var(--card);color:var(--ink);font:inherit}
.controls label{display:flex;gap:8px;align-items:center;font-size:14px;cursor:pointer}
.btns{display:flex;gap:6px}
button{padding:5px 10px;border:1px solid var(--rule);border-radius:7px;background:var(--card);color:var(--ink);
cursor:pointer;font:inherit;font-size:13px}button:hover{border-color:var(--accent)}
button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
.tasknav{list-style:none;margin:0;padding:0;display:grid;gap:2px;max-height:40vh;overflow:auto}
@media (min-width:1000px){.tasknav{max-height:none}}
.tasknav a{display:grid;grid-template-columns:minmax(0,1fr) auto 3.2em;gap:8px;align-items:center;padding:4px 8px;
border-radius:7px;text-decoration:none;color:var(--ink);font-size:13.5px}
.tasknav a:hover{background:var(--card)}
.tasknav .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.tasknav .num{text-align:right;color:var(--muted)}
.dots{display:flex;gap:3px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.keys{margin-top:14px;font-size:12px;color:var(--muted)}
kbd{font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid var(--rule);border-bottom-width:2px;border-radius:4px;
padding:0 4px;background:var(--card)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{display:grid;gap:2px;align-content:start;padding:10px 12px;border:1px solid var(--rule);border-radius:10px}
.tile b{font-size:19px;font-variant-numeric:tabular-nums}
.spark{margin-top:4px}.spark .sp{fill:var(--accent);opacity:.75}.spark .sp-none{fill:var(--none-fill)}
.charts{display:grid;gap:14px}
.stacked{margin:0}.stacked figcaption{font-weight:600;font-size:14px;margin-bottom:6px}
.stacked svg{width:100%;height:22px;border-radius:6px;display:block}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:6px;font-size:13px;color:var(--muted)}
.legend b{color:var(--ink);font-variant-numeric:tabular-nums}
.key{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
rect.pass,.key.pass,.dot.pass,.fill.pass,.sq.passed{fill:var(--pass-fill);background:var(--pass-fill)}
rect.partial,.key.partial,.dot.partial,.fill.partial,.sq.partial{fill:var(--partial-fill);background:var(--partial-fill)}
rect.fail,.key.fail,.dot.fail,.fill.fail,.sq.failed{fill:var(--fail-fill);background:var(--fail-fill)}
rect.none,.key.none,.dot.none,.fill.none,.sq.skipped,.sq.initialized{fill:var(--none-fill);background:var(--none-fill)}
.sq.failed,.dot.fail{background:linear-gradient(135deg,var(--fail-fill) 40%,#fff 40%,#fff 60%,var(--fail-fill) 60%)}
.sq.skipped,.sq.initialized,.dot.none{background:transparent;box-shadow:inset 0 0 0 1.5px var(--none-fill)}
.head-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.sorter button{margin-left:6px;padding:2px 8px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{padding:5px 8px;border-bottom:1px solid var(--rule);text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}th.num,td.num{text-align:right}
.matrix td:first-child{white-space:nowrap}.task-link{text-decoration:none}
.cell{text-align:center;font-variant-numeric:tabular-nums;width:4.5em}
.cell a{color:inherit;text-decoration:none;display:block}
.cell.pass{background:var(--pass-bg);color:var(--pass)}.cell.partial{background:var(--partial-bg);color:var(--partial)}
.cell.fail{background:var(--fail-bg);color:var(--fail)}.cell.none{background:var(--none-bg);color:var(--muted)}
.meanbar{width:22%;min-width:90px}
.bar{display:inline-block;width:100%;min-width:60px;height:8px;border-radius:99px;background:var(--none-bg);
overflow:hidden;vertical-align:middle}.bar .fill{display:block;height:100%;border-radius:99px}
.task{display:grid;gap:10px;scroll-margin-top:12px}
.task-head{display:grid;grid-template-columns:auto auto minmax(80px,200px) 1fr;gap:6px 14px;align-items:center;
padding:6px 2px 0}
@media (max-width:640px){.task-head{grid-template-columns:1fr auto}.task-head .bar{grid-column:1/-1}
.task-head>.muted{grid-column:1/-1}}
.answer-card{padding:0;scroll-margin-top:12px}
.answer-card>summary{cursor:pointer;display:grid;grid-template-columns:minmax(9em,auto) minmax(0,1fr) minmax(140px,220px);
gap:6px 16px;align-items:center;padding:12px 18px;list-style:none}
.answer-card>summary::-webkit-details-marker{display:none}
.answer-card>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:12px}
@media (max-width:760px){.answer-card>summary{grid-template-columns:1fr auto}.answer-card .sumline{grid-column:1/-1;order:3}}
.answer-card .title{font-weight:600}
.answer-card[open]>summary{border-bottom:1px solid var(--rule)}
.sumline{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;min-width:0}
.strip{display:flex;flex-wrap:wrap;gap:2px;max-width:100%}.sq{width:9px;height:9px;border-radius:2px;display:inline-block}
.lostids code{font-size:12px}
.scorebox{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}
.badge{font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;padding:2px 8px;border-radius:999px}
.badge.pass{background:var(--pass-bg);color:var(--pass)}.badge.partial{background:var(--partial-bg);color:var(--partial)}
.badge.fail{background:var(--fail-bg);color:var(--fail)}.badge.none{background:var(--none-bg);color:var(--muted)}
.card-body{padding:12px 18px 16px;display:grid;gap:12px}
.lost{border:1px solid var(--fail-bg);background:color-mix(in srgb,var(--fail-bg) 45%,var(--card));border-radius:10px;
padding:12px 14px}
.lost>ul{list-style:none;margin:0;padding:0;display:grid;gap:12px}
.panes{display:grid;gap:16px;align-items:start}
@media (min-width:1280px){.panes{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)}
.pane-answer{position:sticky;top:12px}.pane-answer pre.answer{max-height:calc(100vh - 80px)}}
ul.tree,ul.tree ul{list-style:none;margin:0;padding-left:18px;border-left:1px solid var(--rule)}
ul.tree{padding-left:0;border-left:0}
.node{margin:4px 0}.node-head{display:flex;flex-wrap:wrap;gap:2px 10px;align-items:baseline}
.chip{font:600 11px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;padding:0 6px;border-radius:4px;text-transform:uppercase}
.chip.passed{background:var(--pass-bg);color:var(--pass)}.chip.failed{background:var(--fail-bg);color:var(--fail)}
.chip.partial{background:var(--partial-bg);color:var(--partial)}
.chip.skipped,.chip.initialized{background:var(--none-bg);color:var(--muted)}
.id{font-weight:600}.score{font-variant-numeric:tabular-nums}.tags{color:var(--muted);font-size:12px}
.desc{color:var(--muted);font-size:13.5px;flex-basis:100%}
.skipnote{color:var(--muted);font-size:12.5px;margin:0 0 4px}.skipnote code{font-size:12px}
.hide-skipped li.leaf.s-skipped{display:none}
details.evidence{margin:4px 0 6px;padding:6px 10px;border:1px dashed var(--rule);border-radius:8px}
details.evidence>summary{cursor:pointer;color:var(--muted);font-size:12.5px}
.claim{margin:6px 0}.check{margin:6px 0;padding-left:10px;border-left:3px solid var(--rule)}
.check.failed{border-left-color:var(--fail-fill)}.check.passed{border-left-color:var(--pass-fill)}
.check-head{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}
a.url{overflow-wrap:anywhere;text-decoration:none}a.url b{font-weight:600}a.url:hover{text-decoration:underline}
.votes{display:inline-flex;gap:3px;align-items:center;font-size:12px}
.vote{width:8px;height:8px;border-radius:50%;display:inline-block}.vote.yes{background:var(--pass-fill)}
.vote.no{background:transparent;border:1.5px solid var(--fail-fill)}
.with-thumb{display:grid;grid-template-columns:minmax(0,1fr) 200px;gap:12px;align-items:start}
@media (max-width:640px){.with-thumb{grid-template-columns:1fr}}
.thumb{margin:0}.thumb svg{width:100%;max-width:240px;height:auto;display:block;border:1px solid var(--rule);
border-radius:6px;background:var(--code)}.defs{position:absolute}
.thumb figcaption{font-size:11.5px;color:var(--muted)}
.reasoning{white-space:pre-wrap;font-size:13.5px;margin-top:2px}.note{color:var(--partial);font-size:13.5px}
pre.answer,pre.json{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:10px 12px;
border-radius:8px;max-height:560px;overflow:auto;margin:6px 0 0}
details>summary{cursor:pointer}
.pane-answer>summary{font-weight:600;font-size:14px;color:var(--ink2)}
.hidden{display:none!important}
"""

_JS = """
(function(){
  var q=document.getElementById('filter'), below=document.getElementById('below'),
      skipped=document.getElementById('hide-skipped');
  function apply(){
    var text=(q.value||'').toLowerCase(), onlyBelow=below.checked;
    document.querySelectorAll('[data-task]').forEach(function(el){
      var task=el.getAttribute('data-task').toLowerCase();
      var hide=text && task.indexOf(text)<0;
      if(!hide && onlyBelow) hide=el.getAttribute('data-below')!=='1';
      el.classList.toggle('hidden', !!hide);
    });
  }
  q.addEventListener('input',apply); below.addEventListener('change',apply);
  skipped.addEventListener('change',function(){document.body.classList.toggle('hide-skipped',skipped.checked);});
  function cards(){return Array.prototype.filter.call(document.querySelectorAll('.answer-card'),
    function(d){return d.offsetParent!==null;});}
  document.getElementById('expand').addEventListener('click',function(){
    cards().forEach(function(d){d.open=true;});
  });
  document.getElementById('collapse').addEventListener('click',function(){
    document.querySelectorAll('.answer-card').forEach(function(d){d.open=false;});
  });
  function openTarget(id){var t=document.getElementById(id); if(t&&t.tagName==='DETAILS') t.open=true;}
  document.querySelectorAll('a[href^="#a-"]').forEach(function(a){
    a.addEventListener('click',function(){openTarget(a.getAttribute('href').slice(1));});
  });
  if(location.hash.indexOf('#a-')===0) openTarget(location.hash.slice(1));
  var wide=window.matchMedia('(min-width:1280px)');
  document.querySelectorAll('.answer-card').forEach(function(card){
    card.addEventListener('toggle',function(){
      var pane=card.querySelector('.pane-answer'); if(card.open&&pane&&wide.matches) pane.open=true;
    });
  });
  var tbody=document.querySelector('.matrix tbody');
  document.querySelectorAll('[data-sort]').forEach(function(b){
    b.addEventListener('click',function(){
      var key=b.getAttribute('data-sort'), rows=Array.prototype.slice.call(tbody.rows);
      rows.sort(function(x,y){
        if(key==='mean') return parseFloat(x.getAttribute('data-mean'))-parseFloat(y.getAttribute('data-mean'));
        return x.getAttribute('data-task')<y.getAttribute('data-task')?-1:1;
      });
      rows.forEach(function(r){tbody.appendChild(r);});
      document.querySelectorAll('[data-sort]').forEach(function(o){o.setAttribute('aria-pressed',o===b);});
    });
  });
  document.addEventListener('keydown',function(ev){
    var tag=(ev.target.tagName||'').toLowerCase();
    if(tag==='input'||tag==='textarea'||ev.metaKey||ev.ctrlKey||ev.altKey) return;
    if(ev.key==='/'){ev.preventDefault(); q.focus(); return;}
    if(ev.key!=='j'&&ev.key!=='k') return;
    var list=cards(); if(!list.length) return;
    var step=ev.key==='j'?1:-1, i=list.indexOf(document.activeElement.closest&&document.activeElement.closest('.answer-card'));
    if(i>=0){i+=step;}
    else if(step>0){for(i=0;i<list.length;i++){if(list[i].getBoundingClientRect().top>1) break;}}
    else{for(i=list.length-1;i>=0;i--){if(list[i].getBoundingClientRect().top<-1) break;}}
    var card=list[Math.max(0,Math.min(list.length-1,i))];
    card.scrollIntoView({block:'start'}); card.querySelector('summary').focus({preventScroll:true});
  });
})();
"""


def _stale_pairs(entries: list[AnswerEntry], metrics: Optional[dict], metrics_time: Optional[float]) -> set:
    """The (task, run) pairs the metrics list as changed since their result, minus those whose result file is
    newer than the metrics (``metrics_time``), which scored the answer again after the metrics were computed.

    This relies on file modification times: results copied without them (``cp`` without ``-p``) can look newer
    than the metrics and hide a changed answer, until ``mind2web2 metrics`` runs again."""
    listed = {(s.get("task_id"), s.get("run")) for s in (metrics or {}).get("stale_results") or []
              if isinstance(s, dict)}
    if metrics_time is None:
        return listed
    newer = set()
    for entry in entries:
        try:
            if entry.result_file is not None and entry.result_file.stat().st_mtime > metrics_time:
                newer.add((entry.task_id, entry.run))
        except OSError:
            pass
    return listed - newer


def render_report(agent: str, entries: list[AnswerEntry], metrics: Optional[dict] = None,
                  generated: Optional[datetime] = None, thumbnails: Optional[Thumbnails] = None,
                  metrics_time: Optional[float] = None, num_runs: Optional[int] = None) -> str:
    """The report page for ``entries`` (see the module docstring).

    ``thumbnails`` supplies the page thumbnails.  ``metrics_time`` is the
    modification time of ``metrics``' file; a result newer than it is not
    shown as changed.  The task means count runs 1 to the highest of
    ``num_runs``, the metrics' ``num_runs``, and the runs in ``entries``.
    """
    generated = generated or datetime.now().astimezone()
    stale = _stale_pairs(entries, metrics, metrics_time)
    nonce = secrets.token_urlsafe(16)
    scored = sum(_score(e) is not None for e in entries)
    recorded_runs = (metrics or {}).get("num_runs")
    if not isinstance(recorded_runs, int) or not 0 < recorded_runs <= _MAX_RECORDED_RUNS:
        recorded_runs = 0  # absent, or not a run count the metrics would write
    top = max([num_runs or 0, recorded_runs] + [e.run for e in entries])
    runs = list(range(1, top + 1))
    tasks = "".join(_task_html(task_id, cells, runs, stale, thumbnails)
                    for task_id, cells in _by_task(entries).items())
    budget_note = ""
    if thumbnails is not None and thumbnails.first_dropped is not None:
        budget_note = (f'<p class="note">Page thumbnails stop after {THUMBNAIL_BUDGET_BYTES // 1_000_000} MB: from '
                       f'<a href="#{thumbnails.first_dropped}">this answer</a> on, failed checks show a thumbnail only '
                       "of a page already shown. Write the report of fewer tasks with <code>--task</code> to see "
                       "theirs.</p>")
    csp = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; img-src data:; "
           "base-uri 'none'; form-action 'none'")
    side = (
        '<aside class="side"><div class="brand">Mind2Web 2 results</div>'
        f'<div class="agent">{_e(agent)}</div><div class="controls">'
        '<input id="filter" type="search" placeholder="Filter tasks" aria-label="Filter tasks">'
        '<label><input id="below" type="checkbox"> only answers below 1.0</label>'
        '<label><input id="hide-skipped" type="checkbox"> hide skipped checks</label>'
        '<div class="btns"><button id="expand" type="button">Open all</button>'
        '<button id="collapse" type="button">Close all</button></div></div>'
        + _nav_html(entries, stale, runs)
        + '<p class="keys"><kbd>/</kbd> filter · <kbd>j</kbd> <kbd>k</kbd> next and previous answer</p></aside>'
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(agent)} · Mind2Web 2 results</title><style>{_CSS}</style></head><body>"
        f'<div class="layout">{side}<main>'
        f'<header><h1>{_e(agent)}</h1><p class="muted">Mind2Web 2 evaluation results · {len(entries)} answers in '
        f"{len({e.task_id for e in entries})} tasks, {scored} with a result · generated "
        f"{_e(generated.strftime('%Y-%m-%d %H:%M %Z'))}</p></header>"
        + _metrics_html(metrics)
        + _charts_html(entries, stale)
        + _matrix_html(entries, stale, runs)
        + budget_note
        + tasks
        + "</main></div>" + (thumbnails.defs_html() if thumbnails is not None else "")
        + f'<script nonce="{nonce}">{_JS}</script></body></html>'
    )
