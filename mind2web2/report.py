"""An HTML page for browsing an agent's evaluation results: scores, rubric trees, and the evidence of each check.

:func:`write_report` reads ``<results_root>/<agent>/`` (layout in
:mod:`mind2web2.results`) and writes one self-contained HTML file, with no
external resources, that shows:

- the metrics saved by ``mind2web2 metrics`` or ``mind2web2 evaluate``, if any;
- a table of every task's score in every run, each cell linking to its
  answer, and each task's mean, which counts a run without a result as 0, as
  the metrics do;
- for each answer, its latest result (badged "changed" when the metrics
  found that the answer changed after it): the judge, the rubric tree with each
  node's status and score, and for each leaf that the eval script verified,
  the claim, the pages it was checked against, the judge's votes, and its
  reasoning (``VerificationNode.evidence``); then the rejected judge requests,
  the extracted information, and the answer text.

Every text taken from results and answers is HTML-escaped and only ``http(s)``
URLs become links; the page's Content Security Policy allows no resource and
only its own script, so that a page quoting untrusted answers and judge output
cannot run anything else.
"""
from __future__ import annotations

import html
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import results
from .submission import answer_run

#: File name of the report in ``<results_root>/<agent>/``.
REPORT_FILE = "report.html"


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
    ``answer_<k>.md`` that evaluation keeps next to it.
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
            entries.append(AnswerEntry(task_dir.name, answer_name, run, result, result_file, answer_text, problem))
    return sorted(entries, key=lambda e: (e.task_id, e.run))


def write_report(results_root: Path, agent: str, output: Optional[Path] = None,
                 task_ids: Optional[Iterable[str]] = None) -> Path:
    """Write the report of ``agent``'s results to ``output`` (default ``<results_root>/<agent>/report.html``).

    Returns the path written.  Raises ``FileNotFoundError`` when the agent has
    no answer folder in the results.
    """
    entries = collect_answers(results_root, agent, task_ids)
    if not entries:
        raise FileNotFoundError(f"no evaluated answers of {agent!r} under {Path(results_root) / agent}")
    metrics = None
    metrics_path = Path(results_root) / agent / "metrics.json"
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metrics = None
    output = Path(output) if output is not None else Path(results_root) / agent / REPORT_FILE
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(agent, entries, metrics), encoding="utf-8")
    return output


# --------------------------------------------------------------------------- rendering

def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url) -> str:
    """``url`` as a link when it is an http(s) URL, otherwise as escaped text."""
    text = "" if url is None else str(url)
    if text.lower().startswith(("http://", "https://")):
        return f'<a href="{_e(text)}" target="_blank" rel="noreferrer noopener">{_e(text)}</a>'
    return f"<code>{_e(text)}</code>"


def _anchor(entry: AnswerEntry) -> str:
    return "a-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in f"{entry.task_id}-{entry.run}")


def _score_class(score: Optional[float]) -> str:
    if score is None:
        return "none"
    if score >= 1 - 1e-6:
        return "pass"
    return "partial" if score > 0 else "fail"


def _score(entry: AnswerEntry) -> Optional[float]:
    try:
        return float(entry.result["final_score"]) if entry.result is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _metrics_html(metrics: Optional[dict]) -> str:
    if not metrics:
        return ""

    def stat(key: str, fmt: str = "{:.4f}") -> str:
        value = metrics.get(key)
        if not isinstance(value, dict) or value.get("mean") is None:
            return "–"
        return f"{fmt.format(value['mean'])} ± {fmt.format(value.get('std') or 0)}"

    pass_at_k = metrics.get("pass_at_k") or {}
    cells = [("Partial Completion", stat("partial_completion")), ("Success Rate", stat("success_rate")),
             (f"Pass@{pass_at_k.get('k', 'k')}",
              f"{pass_at_k['value']:.4f}" if pass_at_k.get("value") is not None else "–"),
             ("Time (min)", stat("time_minutes", "{:.2f}")),
             ("Answer length (words)", stat("answer_length_words", "{:.0f}"))]
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
    return (
        '<section class="card"><h2>Metrics</h2>'
        f'<p class="muted">From metrics.json: {_e(metrics.get("num_tasks"))} tasks × '
        f'{_e(metrics.get("num_runs"))} runs'
        + (f" · {_e(', '.join(counts))}" if counts else "") + "</p>"
        '<div class="metrics">'
        + "".join(f'<div><span class="muted">{_e(k)}</span><b>{_e(v)}</b></div>' for k, v in cells)
        + "</div>" + "".join(f'<p class="note">{_e(w)}</p>' for w in warnings) + "</section>"
    )


_MEAN_NOTE = "Changed answers, answers without a result, and runs without an answer count as 0, as in the metrics"


def _matrix_html(entries: list[AnswerEntry], stale: set[tuple[str, int]]) -> str:
    """The table of every task's score in every run, and the task's mean over the runs.

    A pair in ``stale`` (the metrics' ``stale_results``) shows "changed"
    instead of its score.  As in the metrics, a changed answer, an answer
    without a result, and a run without an answer count as 0 in the mean.
    """
    runs = sorted({e.run for e in entries})
    by_task: dict[str, dict[int, AnswerEntry]] = {}
    for entry in entries:
        by_task.setdefault(entry.task_id, {})[entry.run] = entry
    head = "".join(f"<th class=num>run {r}</th>" for r in runs)
    rows = []
    for task_id, cells in by_task.items():
        tds = []
        scores = []
        below = False
        for r in runs:
            entry = cells.get(r)
            if entry is None:
                tds.append('<td class="cell none">·</td>')
                scores.append(0.0)
                below = True
                continue
            score = None if (task_id, r) in stale else _score(entry)
            scores.append(score or 0.0)
            below = below or score is None or score < 1 - 1e-6
            label = f"{score:.2f}" if score is not None else "changed" if (task_id, r) in stale else "no result"
            tds.append(f'<td class="cell {_score_class(score)}"><a href="#{_anchor(entry)}">{label}</a></td>')
        mean = f"{sum(scores) / len(scores):.3f}"
        rows.append(f'<tr data-task="{_e(task_id)}" data-below="{int(below)}">'
                    f'<td><code>{_e(task_id)}</code></td>{"".join(tds)}<td class=num>{mean}</td></tr>')
    return ('<section class="card"><h2>Scores</h2><div class="scroll"><table class="matrix">'
            f'<thead><tr><th>task</th>{head}<th class=num title="{_MEAN_NOTE}">mean</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


def _check_html(check: dict) -> str:
    passed = check.get("passed")
    verdict = "passed" if passed else "failed"
    votes = check.get("votes") or []
    vote_text = f" · {sum(bool(v) for v in votes)} of {len(votes)} votes pass" if len(votes) > 1 else ""
    source = _link(check["url"]) if check.get("url") else '<span class="muted">no source: judged from the answer</span>'
    parts = [f'<div class="check"><div><span class="chip {verdict}">{verdict}</span> {source}'
             f'<span class="muted">{_e(vote_text)}</span></div>']
    if check.get("note"):
        parts.append(f'<div class="note">{_e(check["note"])}</div>')
    if check.get("reasoning"):
        parts.append(f'<div class="reasoning">{_e(check["reasoning"])}</div>')
    parts.append("</div>")
    return "".join(parts)


def _evidence_html(evidence: dict, status: str) -> str:
    rows = [f'<div class="claim"><span class="muted">claim</span> {_e(evidence.get("claim"))}</div>']
    if evidence.get("skipped_because"):
        rows.append(f'<div class="note">Skipped: check <code>{_e(evidence["skipped_because"])}</code>, '
                    f"which it depends on, did not pass.</div>")
    if evidence.get("error"):
        rows.append(f'<div class="note">Failed with an error: {_e(evidence["error"])}</div>')
    checks = evidence.get("checks") or []
    rows.extend(_check_html(c) for c in checks)
    sources = evidence.get("sources") or []
    unchecked = [u for u in sources if u not in {c.get("url") for c in checks}]
    if unchecked and not evidence.get("skipped_because"):
        rows.append('<div class="muted small">Not checked (another source already supported the claim, '
                    "or the check stopped): " + ", ".join(_link(u) for u in unchecked) + "</div>")
    open_attr = " open" if status in ("failed", "skipped") else ""
    return f'<details class="evidence"{open_attr}><summary>evidence</summary>{"".join(rows)}</details>'


def _node_html(node: dict) -> str:
    status = str(node.get("status", ""))
    score = node.get("score")
    tags = [str(node.get("strategy", ""))] + (["critical"] if node.get("critical") else [])
    score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else ""
    head = (f'<div class="node-head"><span class="chip {_e(status)}">{_e(status)}</span>'
            f'<code class="id">{_e(node.get("id"))}</code><span class="score">{score_text}</span>'
            f'<span class="tags">{_e(" · ".join(t for t in tags if t))}</span>'
            f'<span class="desc">{_e(node.get("desc"))}</span></div>')
    evidence = _evidence_html(node["evidence"], status) if node.get("evidence") else ""
    children = node.get("children") or []
    kids = f'<ul>{"".join(_node_html(c) for c in children)}</ul>' if children else ""
    return f'<li class="node s-{_e(status)}">{head}{evidence}{kids}</li>'


def _answer_html(entry: AnswerEntry, stale: set[tuple[str, int]]) -> str:
    """One answer's collapsed section; an answer in ``stale`` is badged "changed" and counts as below 1.0."""
    changed = (entry.task_id, entry.run) in stale
    score = None if changed else _score(entry)
    title = f"{entry.task_id} / {entry.answer_name}"
    label = f"{score:.3f}" if score is not None else "changed" if changed else "no result"
    badge = f'<span class="badge {_score_class(score)}">{label}</span>'
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
        breakdown = (result.get("eval_breakdown") or [{}])[0]
        tree = breakdown.get("verification_tree")
        if tree:
            if not _has_evidence(tree):
                body.append('<p class="muted small">This result was saved without the evidence of its checks; '
                            "evaluate the answer again with <code>--overwrite</code> to record it.</p>")
            body.append(f'<ul class="tree">{_node_html(tree)}</ul>')
        rejections = usage.get("rejections") or []
        if rejections:
            items = "".join(f"<li><code>{_e(r.get('check'))}</code> {_link(r['url']) if r.get('url') else ''}: "
                            f"{_e(r.get('reason'))}</li>" for r in rejections)
            body.append(f'<details><summary>{len(rejections)} judge requests rejected for their content</summary>'
                        f"<ul>{items}</ul></details>")
        info = breakdown.get("info")
        if info:
            body.append('<details><summary>extracted information</summary><pre class="json">'
                        f"{_e(json.dumps(info, ensure_ascii=False, indent=2))}</pre></details>")
    if entry.answer_text is not None:
        body.append(f'<details><summary>answer text ({len(entry.answer_text):,} characters)</summary>'
                    f'<pre class="answer">{_e(entry.answer_text)}</pre></details>')
    below = "1" if score is None or score < 1 - 1e-6 else "0"
    return (f'<details class="answer-card card" id="{_anchor(entry)}" data-task="{_e(entry.task_id)}" '
            f'data-below="{below}"><summary><span class="title">{_e(title)}</span>{badge}</summary>'
            f'{"".join(body)}</details>')


def _has_evidence(node: dict) -> bool:
    return bool(node.get("evidence")) or any(_has_evidence(c) for c in node.get("children") or [])


_CSS = """
:root{--bg:#f6f7f7;--card:#fff;--ink:#17201d;--muted:#5f6d68;--rule:#d9e0dd;--pass:#1b7f4a;--pass-bg:#e2f3e9;
--partial:#8a5a00;--partial-bg:#fbefd6;--fail:#b3261e;--fail-bg:#fbe6e4;--none-bg:#eceff0;--accent:#2f5da8;
--code:#eef2f1}
@media (prefers-color-scheme:dark){:root{--bg:#0f1513;--card:#161e1b;--ink:#e3ebe8;--muted:#90a19b;--rule:#28332f;
--pass:#62c790;--pass-bg:#15301f;--partial:#e2ab4a;--partial-bg:#352812;--fail:#f07b70;--fail-bg:#3a1916;
--none-bg:#1d2623;--accent:#8aadea;--code:#111a17}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:24px 16px 64px}
main{max-width:1100px;margin:0 auto;display:grid;gap:16px}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:0 0 10px}
a{color:var(--accent)}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
code{background:var(--code);padding:0 4px;border-radius:4px;word-break:break-word}
.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:14px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.metrics div{display:grid}.metrics b{font-size:17px;font-variant-numeric:tabular-nums}
.toolbar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
.toolbar input[type=search]{padding:6px 10px;border:1px solid var(--rule);border-radius:6px;background:var(--card);
color:var(--ink);min-width:220px}
.toolbar button{padding:6px 10px;border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink);
cursor:pointer}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{padding:5px 8px;border-bottom:1px solid var(--rule);text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
.cell{text-align:center;font-variant-numeric:tabular-nums}.cell a{color:inherit;text-decoration:none;display:block}
.pass{background:var(--pass-bg);color:var(--pass)}.partial{background:var(--partial-bg);color:var(--partial)}
.fail{background:var(--fail-bg);color:var(--fail)}.none{background:var(--none-bg);color:var(--muted)}
.answer-card>summary{cursor:pointer;display:flex;gap:12px;align-items:center;justify-content:space-between;
font-weight:600}
.answer-card[open]>summary{margin-bottom:10px}
.badge{font:600 13px ui-monospace,monospace;padding:2px 8px;border-radius:999px}
ul.tree,ul.tree ul{list-style:none;margin:0;padding-left:18px;border-left:1px solid var(--rule)}
ul.tree{padding-left:0;border-left:0}
.node{margin:4px 0}.node-head{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.chip{font:600 11px/1.6 ui-monospace,monospace;padding:0 6px;border-radius:4px;text-transform:uppercase}
.chip.passed{background:var(--pass-bg);color:var(--pass)}.chip.failed{background:var(--fail-bg);color:var(--fail)}
.chip.partial{background:var(--partial-bg);color:var(--partial)}
.chip.skipped,.chip.initialized{background:var(--none-bg);color:var(--muted)}
.id{font-weight:600}.score{font-variant-numeric:tabular-nums}.tags{color:var(--muted);font-size:12px}
.desc{color:var(--muted);font-size:13.5px;flex-basis:100%}
details.evidence{margin:4px 0 6px;padding:6px 10px;border:1px dashed var(--rule);border-radius:8px}
details.evidence>summary{cursor:pointer;color:var(--muted);font-size:12.5px}
.claim{margin:6px 0}.check{margin:6px 0;padding-left:10px;border-left:3px solid var(--rule)}
.reasoning{white-space:pre-wrap;font-size:13.5px;margin-top:2px}.note{color:var(--partial);font-size:13.5px}
pre.answer,pre.json{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:10px;border-radius:8px;
max-height:520px;overflow:auto}
details>summary{cursor:pointer}
.hidden{display:none!important}
"""

_JS = """
(function(){
  var q=document.getElementById('filter'), below=document.getElementById('below');
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
  document.getElementById('expand').addEventListener('click',function(){
    document.querySelectorAll('.answer-card:not(.hidden)').forEach(function(d){d.open=true;});
  });
  document.getElementById('collapse').addEventListener('click',function(){
    document.querySelectorAll('.answer-card').forEach(function(d){d.open=false;});
  });
  document.querySelectorAll('a[href^="#a-"]').forEach(function(a){
    a.addEventListener('click',function(){
      var t=document.getElementById(a.getAttribute('href').slice(1)); if(t) t.open=true;
    });
  });
})();
"""


def render_report(agent: str, entries: list[AnswerEntry], metrics: Optional[dict] = None,
                  generated: Optional[datetime] = None) -> str:
    """The report page for ``entries`` (see the module docstring)."""
    generated = generated or datetime.now().astimezone()
    stale = {(s.get("task_id"), s.get("run")) for s in (metrics or {}).get("stale_results") or []}
    nonce = secrets.token_urlsafe(16)
    scored = sum(_score(e) is not None for e in entries)
    csp = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; img-src data:; "
           "base-uri 'none'; form-action 'none'")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(agent)} · Mind2Web 2 results</title><style>{_CSS}</style></head><body><main>"
        f'<header><h1>{_e(agent)}</h1><p class="muted">Mind2Web 2 evaluation results · {len(entries)} answers in '
        f"{len({e.task_id for e in entries})} tasks, {scored} with a result · generated "
        f"{_e(generated.strftime('%Y-%m-%d %H:%M %Z'))}</p></header>"
        + _metrics_html(metrics)
        + '<div class="toolbar card">'
        '<input id="filter" type="search" placeholder="Filter tasks" aria-label="Filter tasks">'
        '<label><input id="below" type="checkbox"> only answers below 1.0</label>'
        '<button id="expand" type="button">Open all</button><button id="collapse" type="button">Close all</button>'
        '<span class="muted small">Failed and skipped checks show their evidence open.</span></div>'
        + _matrix_html(entries, stale)
        + "".join(_answer_html(e, stale) for e in entries)
        + f'</main><script nonce="{nonce}">{_JS}</script></body></html>'
    )
