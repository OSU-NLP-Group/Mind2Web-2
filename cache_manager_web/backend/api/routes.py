"""All API routes for the Cache Manager web backend."""

from __future__ import annotations
import asyncio
import base64
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from mind2web2.utils.cache_filesys import storage_key
from mind2web2.utils.page_info_retrieval import html_to_markdown
from mind2web2.utils.url_tools import normalize_url_simple

from .. import config
from ..models import CacheManager, KeywordDetector

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Shared application state (set by lifespan)
# ---------------------------------------------------------------------------

_cm: Optional[CacheManager] = None
_kd: Optional[KeywordDetector] = None

# Active capture target — tells the extension what task/URL we're capturing for
_capture_target: dict = {}  # {"task_id": ..., "url": ..., "ts": ...}

# Per-URL issue cache: {task_id: {url: {"issues": [...], "severity": "..."}}}, see _issue_entry()
_url_issue_cache: dict = {}

# Batch capture state; the batch has done _batch_total - len(_batch_queue) of its URLs
_batch_queue: list[dict] = []   # [{task_id, url}, ...], the head is the URL the batch waits for
_batch_active: bool = False
_batch_total: int = 0

# SSE subscribers — each is an asyncio.Queue
_sse_queues: list[asyncio.Queue] = []

# Held by /api/load, so that loads run one at a time
_load_lock = asyncio.Lock()


def set_app_state(cm: CacheManager, kd: KeywordDetector):
    global _cm, _kd
    _cm, _kd = cm, kd


def _require_loaded():
    if not _cm or not _cm.agent_path:
        raise HTTPException(400, "No cache folder loaded. POST /api/load first.")


async def _push_event(event_type: str, data: dict):
    """Push an SSE event to all connected frontends."""
    payload = json.dumps({"type": event_type, **data})
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_queues.remove(q)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LoadRequest(BaseModel):
    path: str

class CaptureRequest(BaseModel):
    task_id: str
    url: str
    screenshot_base64: str  # PNG or JPEG, base64; stored as JPEG
    html: Optional[str] = None  # the page's HTML, converted to text as the crawler converts it
    text: str = ""  # the page's text, stored when no HTML is sent
    actual_url: Optional[str] = None  # URL after redirects (may differ from url)
    visible_part_only: bool = False  # the full-page screenshot failed; this one shows the visible part of the tab
    batch: bool = False  # sent by a batch capture, which is stored only while the batch waits for url

REVIEW_STATUSES = ("ok", "")
"""The statuses a reviewer sets: "ok", or "" to clear the status; captures set "fixed" and "recaptured" themselves."""

class ReviewRequest(BaseModel):
    url: str
    status: str  # one of REVIEW_STATUSES

class CaptureTargetRequest(BaseModel):
    task_id: str
    url: str

class AddUrlRequest(BaseModel):
    url: str

class FlagRequest(BaseModel):
    url: str

class RenameUrlRequest(BaseModel):
    old_url: str
    new_url: str

class BatchItem(BaseModel):
    task_id: str
    url: str

class BatchStartRequest(BaseModel):
    items: list[BatchItem]

class BatchSkipRequest(BaseModel):
    task_id: str
    url: str


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@router.get("/events")
async def sse_stream():
    """Server-Sent Events stream for real-time updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sse_queues.append(queue)

    async def generate():
        try:
            # Initial heartbeat
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Load / Status
# ---------------------------------------------------------------------------

@router.post("/load")
async def load_cache(req: LoadRequest):
    """Load an agent's cache folder and scan it for issues.

    The folder is read and scanned in a worker thread into a new
    ``CacheManager``, which replaces the current one only when complete, so
    the server keeps answering (the extension polls it) and a failed load
    leaves the loaded cache in place.  Loads run one at a time.  The one
    wait is for review-state files: a route that changes one (a flag, a
    review status) waits, on the event loop, while the load writes one
    such file, which it does when it drops pending URLs whose page another
    process stored.

    The UI loads the loaded folder again whenever it is opened and on
    Refresh.  Such a reload keeps a running batch capture and the capture
    target, and the edits the current manager served while the folder was
    read, such as the batch's captures, are on disk (see ``CacheManager``);
    the tasks they changed are read and scanned again before the new manager
    replaces the current one, so that it lists them.  Loading another folder
    stops a running batch capture and clears the capture target, which name
    URLs of the folder loaded before.
    """
    global _cm, _url_issue_cache, _capture_target
    p = Path(req.path).resolve()
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")
    async with _load_lock:
        current = _cm if _cm is not None and _cm.agent_path is not None and _cm.agent_path.resolve() == p else None
        since = current.revision if current is not None else 0
        cm = CacheManager()
        try:
            ok, total = await asyncio.to_thread(cm.load_agent_cache, str(p))
            issue_cache = await asyncio.to_thread(_scan_issues, cm)
        except Exception as e:
            raise HTTPException(500, str(e))
        if current is not None:
            for task_id in current.tasks_changed_since(since):
                cm.reload_task(task_id)
                issue_cache[task_id] = _scan_task(cm, task_id)
        else:
            _capture_target = {}
            if _batch_active:
                await batch_stop()
        _cm, _url_issue_cache = cm, issue_cache
    return {
        "ok": True,
        "agent_name": _cm.agent_name,
        "agent_path": str(p),
        "loaded_tasks": ok,
        "total_tasks": total,
        "stats": _cm.get_statistics(),
        "task_issues": _task_issues(issue_cache),
        "issue_index": _issue_index(issue_cache),
    }


@router.get("/status")
async def get_status():
    if not _cm or not _cm.agent_path:
        return {"loaded": False}
    stats = _cm.get_statistics()
    return {
        "loaded": True,
        "agent_name": _cm.agent_name,
        "agent_path": str(_cm.agent_path),
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@router.get("/tasks")
async def list_tasks():
    _require_loaded()
    tasks = []
    for task_id in _cm.get_task_ids():
        summary = _cm.get_task_summary(task_id)
        if summary:
            reviewed = _cm.load_reviewed(task_id)
            task_issue_cache = _url_issue_cache.get(task_id, {})
            # "recaptured" doesn't count as fixed
            issue_reviewed = sum(
                1 for url in task_issue_cache
                if url in reviewed and reviewed[url] != "recaptured"
            )
            tasks.append({
                "task_id": summary.task_id,
                "total_urls": summary.total_urls,
                "web_urls": summary.web_urls,
                "pdf_urls": summary.pdf_urls,
                "failed_urls": summary.failed_urls,
                "pending_urls": summary.pending_urls,
                "reviewed_count": len(reviewed),
                "issue_count": len(task_issue_cache),
                "issue_reviewed_count": issue_reviewed,
            })
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/urls")
async def list_urls(task_id: str):
    _require_loaded()
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {task_id}")

    url_infos = _cm.get_task_urls(task_id)
    reviewed_map = _cm.load_reviewed(task_id)

    task_issue_cache = _url_issue_cache.get(task_id, {})
    urls = []
    for ui in url_infos:
        # Parse domain/path for display
        try:
            parsed = urlparse(ui.url)
            domain = parsed.netloc or ui.url[:50]
            if domain.startswith("www."):
                domain = domain[4:]
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
        except Exception:
            domain = ui.url[:40]
            path = ""

        # Use cached issue results (populated during /api/load)
        cached = task_issue_cache.get(ui.url)
        issues = cached["issues"] if cached else []
        severity = cached["severity"] if cached else ""

        urls.append({
            "url": ui.url,
            "content_type": ui.content_type,
            "domain": domain,
            "path": path,
            "issues": issues,
            "severity": severity,
            "reviewed": reviewed_map.get(ui.url, ""),
            "failure": ui.failure,
        })

    # Sort by domain then path
    urls.sort(key=lambda u: (u["domain"].lower(), u["path"].lower()))
    return {"task_id": task_id, "urls": urls, "total": len(urls),
            "reviewed_count": sum(1 for u in urls if u["reviewed"] in ("ok", "fixed", "skip"))}


# ---------------------------------------------------------------------------
# Content serving
# ---------------------------------------------------------------------------

@router.get("/content/{task_id}/text")
async def get_text(task_id: str, url: str = Query(...)):
    _require_loaded()
    text, _ = _cm.get_url_content(task_id, url, get_screenshot=False)
    if text is None:
        raise HTTPException(404, "Text not found")
    det = _kd.detect_issues(text)
    keywords, severity = det.matched_keywords, det.severity
    if _cm.is_flagged(task_id, url):
        keywords, severity = ["flagged", *keywords], "definite"
    return {"text": text, "issues": {
        "has_issues": bool(keywords or det.matched_patterns),
        "keywords": keywords,
        "patterns": det.matched_patterns,
        "severity": severity,
    }}


@router.get("/content/{task_id}/screenshot")
async def get_screenshot(task_id: str, url: str = Query(...)):
    _require_loaded()
    _, data = _cm.get_url_content(task_id, url)
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, "Task not found")
    ct = _cm.url_state(task_id, url)
    if ct != "web" or data is None:
        raise HTTPException(404, "Screenshot not found")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/content/{task_id}/pdf")
async def get_pdf(task_id: str, url: str = Query(...)):
    _require_loaded()
    _, data = _cm.get_url_content(task_id, url)
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, "Task not found")
    ct = _cm.url_state(task_id, url)
    if ct != "pdf" or data is None:
        raise HTTPException(404, "PDF not found")
    return Response(content=data, media_type="application/pdf")


# ---------------------------------------------------------------------------
# Capture (from Chrome extension)
# ---------------------------------------------------------------------------

@router.post("/capture/target")
async def set_capture_target(req: CaptureTargetRequest):
    """Set the active capture target (called by web UI when user clicks 'Open in Browser')."""
    global _capture_target
    _capture_target = {"task_id": req.task_id, "url": req.url, "ts": time.time()}
    return {"ok": True, "target": _capture_target}


@router.get("/capture/target")
async def get_capture_target():
    """Get the active capture target (called by extension)."""
    if not _capture_target:
        return {"active": False}
    # Expire after 30 minutes
    if time.time() - _capture_target.get("ts", 0) > 1800:
        return {"active": False}
    return {"active": True, **_capture_target}


@router.post("/capture")
async def receive_capture(req: CaptureRequest, request: Request):
    """Store a page captured by the Chrome extension, for ``url`` and, after a redirect, for ``actual_url`` too.

    ``url`` must be an http(s) URL (status 400 otherwise).  ``actual_url``
    is ignored unless it is an http(s) URL on another host than this
    server's, so a capture of the Cache Manager's own page is never stored
    under the Cache Manager's address.  The decoded screenshot may have at
    most ``MAX_SCREENSHOT_SIZE`` bytes and the HTML (or the text) at most
    ``MAX_TEXT_SIZE`` bytes (status 413 otherwise).

    With ``html``, the stored text is ``html_to_markdown(html)``, the text the
    crawler stores for the pages it captures, so a page's text has the same
    form whichever of the two captured it; ``text`` is stored only when no
    HTML is sent.  Each stored page's flag is cleared and its review status
    set to "recaptured" for a batch capture (a person still has to look at
    it) and "fixed" otherwise.  A reviewed URL no longer qualifies for the
    batch, so a URL captured by hand while it is queued is left out when the
    batch reaches it (see :func:`_skip_urls_that_no_longer_qualify`).

    A batch capture, sent with ``batch``, is stored only while ``url`` is
    the URL the batch waits for, the head of its queue, and then advances
    the batch.  Otherwise, for example because the reviewer captured or
    uploaded that URL by hand while the batch's tab loaded it, the capture
    is refused with status 409 and nothing is stored.

    Two captures are stored without being marked done, and the response and
    the capture_complete event carry a ``warning`` that says why:

    - With ``visible_part_only``, the full-page screenshot failed: each
      stored page is flagged, and its review status cleared, so that it
      stays a definite issue until a full-page capture.
    - A capture by hand whose stored text is empty (or only whitespace)
      keeps its flag and gets no review status: empty text is a definite
      issue, so the URL stays red, and a batch still queues it.  A batch
      capture of such a page is marked "recaptured" as usual, for a person
      to look at.

    Returns the URL the task lists the page under.
    """
    _require_loaded()
    cache = _cm.get_task_cache(req.task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {req.task_id}")
    url = _valid_url(req.url)
    actual_url = _redirect_url(req.actual_url, request)

    try:
        screenshot_bytes = base64.b64decode(req.screenshot_base64)
    except Exception:
        raise HTTPException(400, "Invalid base64 screenshot data")
    if len(screenshot_bytes) > config.MAX_SCREENSHOT_SIZE:
        raise HTTPException(413, f"The screenshot ({len(screenshot_bytes):,} bytes) exceeds the limit of "
                                 f"{config.MAX_SCREENSHOT_SIZE:,} bytes")
    page = req.html if req.html else (req.text or "")
    if len(page) > config.MAX_TEXT_SIZE or len(page.encode("utf-8")) > config.MAX_TEXT_SIZE:
        raise HTTPException(413, f"The page's {'HTML' if req.html else 'text'} exceeds the limit of "
                                 f"{config.MAX_TEXT_SIZE:,} bytes")

    text = await asyncio.to_thread(html_to_markdown, req.html) if req.html else (req.text or "")
    if req.batch:
        _refuse_a_stale_batch_capture(req.task_id, url)
    stored = _cm.store_page(req.task_id, url, text=text, screenshot=screenshot_bytes)
    if stored is None:
        raise HTTPException(500, "Failed to save capture")
    stored_urls = [stored]
    if actual_url and actual_url != url:
        redirected = _cm.store_page(req.task_id, actual_url, text=text, screenshot=screenshot_bytes)
        if redirected is not None and redirected != stored:
            stored_urls.append(redirected)

    empty_by_hand = not req.batch and not text.strip()
    for listed in stored_urls:
        if req.visible_part_only:
            _cm.flag_url(req.task_id, listed)
            _cm.mark_url_reviewed(req.task_id, listed, "")
        elif empty_by_hand:
            _cm.mark_url_reviewed(req.task_id, listed, "")
        else:
            _cm.unflag_url(req.task_id, listed)
            _cm.mark_url_reviewed(req.task_id, listed, "recaptured" if req.batch else "fixed")
    _refresh_issues(req.task_id, *stored_urls)

    warnings = []
    if req.visible_part_only:
        warnings.append("the full-page screenshot failed, so the screenshot shows only the visible part of the page; "
                        "capture it again for a full-page screenshot")
    if empty_by_hand:
        warnings.append("the captured page has no text, so the URL is not marked fixed; "
                        "capture it again once the page shows its content")
    response = {"ok": True, "task_id": req.task_id, "url": stored}
    event = {"task_id": req.task_id, "url": stored}
    if warnings:
        response["warning"] = event["warning"] = "; and ".join(warnings)
    await _push_event("capture_complete", event)
    await _advance_batch(done_head=req.batch)

    return response


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

@router.post("/review/{task_id}")
async def set_review(task_id: str, req: ReviewRequest):
    """Set a URL's review status to "ok", or clear it with "".

    Any other status is refused with status 400 ("fixed" and "recaptured"
    are set by captures and uploads), and a task or URL the task does not
    list with status 404.  A reviewed URL no longer qualifies for a batch.
    """
    _require_loaded()
    if req.status not in REVIEW_STATUSES:
        raise HTTPException(400, f"Unknown review status {req.status!r}; use one of {list(REVIEW_STATUSES)}")
    url = _listed_url(task_id, req.url)
    _cm.mark_url_reviewed(task_id, url, req.status)
    await _advance_batch()
    return {"ok": True}


@router.post("/flag/{task_id}")
async def flag_url(task_id: str, req: FlagRequest):
    """Flag a URL as needing a (re)capture: a definite issue, queued by batch recapture.

    Only ``flags.json`` changes; the stored page, which evaluation reads,
    stays as it is until a capture or upload replaces it.
    """
    _require_loaded()
    url = _listed_url(task_id, req.url)
    _cm.flag_url(task_id, url)
    _cm.mark_url_reviewed(task_id, url, "")
    _refresh_issues(task_id, url)
    return {"ok": True}


@router.post("/reset/{task_id}")
async def reset_url(task_id: str, req: FlagRequest):
    """Delete a URL's stored page or failure record, which leaves the URL pending (a definite issue).

    The URL's flag and review status are cleared.  Until the URL is captured
    again, evaluation treats it as not cached and captures it live.  Returns
    what was deleted: ``"web"``, ``"pdf"``, or ``"failed"``.
    """
    _require_loaded()
    url = _listed_url(task_id, req.url)
    content_type = _cm.reset_url(task_id, url)
    if content_type is None:
        raise HTTPException(409, f"Nothing is stored for {url}; it is already pending")
    _cm.mark_url_reviewed(task_id, url, "")
    _refresh_issues(task_id, url)
    await _push_event("capture_complete", {"task_id": task_id, "url": url})
    return {"ok": True, "content_type": content_type}


@router.get("/issues")
async def list_issues():
    """The current issue index and per-task issue summary, without scanning pages again."""
    _require_loaded()
    return {"issue_index": _issue_index(_url_issue_cache), "task_issues": _task_issues(_url_issue_cache)}


@router.get("/review-progress")
async def review_progress():
    """Get overall review progress across all tasks.

    Only counts URLs that have detected issues — clean URLs are excluded.
    """
    _require_loaded()
    total_issues = 0
    fixed_issues = 0
    for task_id in _cm.get_task_ids():
        task_issue_cache = _url_issue_cache.get(task_id, {})
        total_issues += len(task_issue_cache)
        if task_issue_cache:
            reviewed = _cm.load_reviewed(task_id)
            # "recaptured" doesn't count as fixed — still needs human review
            fixed_issues += sum(
                1 for url in task_issue_cache
                if url in reviewed and reviewed[url] != "recaptured"
            )
    return {"total": total_issues, "reviewed": fixed_issues}


# ---------------------------------------------------------------------------
# Batch Capture
# ---------------------------------------------------------------------------

def _qualifies(task_id: str, url: str) -> bool:
    """Whether a batch capture captures ``url`` of ``task_id``: listed, not a PDF, a definite issue, and not reviewed.

    ``url`` is as the task lists it.  A URL stops qualifying when it is
    deleted or renamed, gets a review status (a capture or upload stores
    one), or its page is stored as a PDF.
    """
    if _cm is None or _cm.url_state(task_id, url) in (None, "pdf"):
        return False
    issue = _url_issue_cache.get(task_id, {}).get(url)
    return bool(issue) and issue.get("severity") == "definite" and url not in _cm.load_reviewed(task_id)


def _skip_urls_that_no_longer_qualify() -> None:
    """Pop queue heads that no longer qualify (see :func:`_qualifies`), so that the head is a URL still to capture.

    Only the head is checked, when it becomes the head: a URL deep in the
    queue that stops and then starts qualifying again, for example when a
    reviewer clears its review status, stays queued.
    """
    while _batch_queue and not _qualifies(_batch_queue[0]["task_id"], _batch_queue[0]["url"]):
        _batch_queue.pop(0)


def _refuse_a_stale_batch_capture(task_id: str, url: str) -> None:
    """Refuse with status 409 a batch capture or upload of a URL that the batch does not wait for.

    The batch waits for the head of its queue.  It moves on without the URL
    its tab loaded when the reviewer captures, uploads, reviews, renames, or
    deletes that URL, when the URL is skipped, and when the batch finishes,
    is stopped, or is replaced by a new batch.
    """
    if not (_batch_active and _batch_queue and _batch_queue[0] == {"task_id": task_id, "url": url}):
        raise HTTPException(409, f"The batch capture does not wait for {url}; nothing was stored")


def _batch_counts() -> dict:
    """The batch's ``total``, ``completed`` (captured, skipped, or left out because it no longer qualified), and ``remaining``."""
    return {"total": _batch_total, "completed": _batch_total - len(_batch_queue), "remaining": len(_batch_queue)}


async def _advance_batch(done_head: bool = False) -> None:
    """Bring a running batch up to date after a change, and report its counts when they changed.

    With ``done_head``, the head was just captured or skipped and is popped.
    Heads that no longer qualify are then popped as well.  When URLs were
    popped, a batch_progress event reports the counts and the next URL, or,
    when the queue is empty, the batch ends with a batch_complete event.
    """
    global _batch_active
    if not _batch_active:
        return
    before = len(_batch_queue)
    if done_head and _batch_queue:
        _batch_queue.pop(0)
    _skip_urls_that_no_longer_qualify()
    if len(_batch_queue) == before:
        return
    if _batch_queue:
        await _push_event("batch_progress", {**_batch_counts(), "next": _batch_queue[0]})
    else:
        _batch_active = False
        await _push_event("batch_complete", {"completed": _batch_total, "total": _batch_total})


@router.post("/capture/batch/start")
async def batch_start(req: BatchStartRequest):
    """Start a batch capture of the given URLs that qualify (see :func:`_qualifies`), replacing any running batch.

    Returns the number of URLs queued.  The batch waits for each URL in
    turn, until the extension captures it or skips it, or it no longer
    qualifies.
    """
    _require_loaded()
    global _batch_queue, _batch_active, _batch_total

    queue = []
    for item in req.items:
        url = _cm.canonical_url(item.task_id, item.url)
        entry = {"task_id": item.task_id, "url": url}
        if entry not in queue and _qualifies(item.task_id, url):
            queue.append(entry)

    if not queue:
        return {"ok": True, "total": 0, "message": "No qualifying URLs to capture"}

    _batch_queue, _batch_active, _batch_total = queue, True, len(queue)
    await _push_event("batch_started", {"total": _batch_total})
    return {"ok": True, "total": _batch_total}


@router.get("/capture/batch/status")
async def batch_status():
    """Get current batch capture status (polled by extension)."""
    if not _batch_active:
        return {"active": False}

    return {"active": True, **_batch_counts(), "current": _batch_queue[0] if _batch_queue else None}


class CaptchaNotify(BaseModel):
    type: str = "unknown"

@router.post("/capture/batch/captcha")
async def batch_captcha_notify(req: CaptchaNotify):
    """Called by extension when CAPTCHA is detected during batch mode."""
    await _push_event("batch_captcha", {"captcha_type": req.type})
    return {"ok": True}


@router.post("/capture/batch/skip")
async def batch_skip(req: BatchSkipRequest):
    """Skip the URL the batch waits for, for example because its page cannot be loaded; nothing is stored for it.

    The request names the URL; if the batch no longer waits for it, the
    request is refused with status 409 and the batch is unchanged.
    """
    _refuse_a_stale_batch_capture(req.task_id, req.url)
    await _advance_batch(done_head=True)
    return {"ok": True, "remaining": len(_batch_queue)}


@router.post("/capture/batch/stop")
async def batch_stop():
    """Stop the current batch capture."""
    global _batch_queue, _batch_active, _batch_total
    _batch_queue, _batch_active, _batch_total = [], False, 0
    await _push_event("batch_stopped", {})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

@router.get("/answers/{task_id}")
async def list_answers(task_id: str):
    """The answer files of a task, from ``<answers>/<agent>/<task_id>/``.

    ``<answers>`` is ``CM_ANSWERS_DIR`` if set (``run.py --answers-dir``),
    else the ``answers`` directory next to the cache directory.
    """
    _require_loaded()
    if not _cm.agent_path:
        return {"files": []}

    answers_root = Path(os.environ.get("CM_ANSWERS_DIR") or _cm.agent_path.parent.parent / "answers")
    answers_dir = answers_root / _cm.agent_name / task_id
    if not answers_dir.is_dir():
        return {"files": []}

    answer_files = sorted(answers_dir.glob("answer_*.md"))
    if not answer_files:
        answer_files = sorted(answers_dir.glob("*.md"))

    files = []
    for f in answer_files:
        try:
            content = f.read_text(encoding="utf-8")
            files.append({"name": f.name, "content": content})
        except Exception:
            pass
    return {"files": files}


# ---------------------------------------------------------------------------
# URL management
# ---------------------------------------------------------------------------

@router.delete("/urls/{task_id}")
async def delete_url(task_id: str, url: str = Query(...)):
    """Delete a URL from a task: its stored page, failure record, pending entry, flag, and review status."""
    _require_loaded()
    url = _listed_url(task_id, url)
    _cm.mark_url_reviewed(task_id, url, "")
    if not _cm.delete_url(task_id, url):
        raise HTTPException(500, "Failed to delete URL")
    _refresh_issues(task_id, url)
    await _advance_batch()
    return {"ok": True}


@router.post("/urls/{task_id}/rename")
async def rename_url(task_id: str, req: RenameUrlRequest):
    """Change a URL's link; returns the new URL as the task lists it, and its content type.

    A stored page moves to the new URL, with its flag and review status.  A
    URL whose capture failed or that is pending leaves the new URL pending,
    since the new link has not been captured.  Nothing changes when the
    stored page cannot be read.
    """
    _require_loaded()
    old_url, new_url = _listed_url(task_id, req.old_url), _valid_url(req.new_url)
    state = _cm.url_state(task_id, old_url)
    if _cm.url_state(task_id, new_url) is not None:
        raise HTTPException(409, f"New URL already exists: {new_url}")

    if state in ("web", "pdf"):
        text, data = _cm.get_url_content(task_id, old_url)
        if data is None or (state == "web" and text is None):
            raise HTTPException(500, f"Cannot read the stored page of {old_url}")
        if state == "web":
            listed = _cm.store_page(task_id, new_url, text=text, screenshot=data)
        else:
            listed = _cm.store_page(task_id, new_url, pdf_bytes=data)
        if listed is None:
            raise HTTPException(500, "Failed to store the page under the new URL")
        _cm.move_review_state(task_id, old_url, listed)
    else:
        if not _cm.add_pending_url(task_id, new_url):  # another manager of the folder listed it meanwhile
            raise HTTPException(409, f"New URL already exists: {new_url}")
        listed = _cm.canonical_url(task_id, new_url)

    _cm.mark_url_reviewed(task_id, old_url, "")
    _cm.delete_url(task_id, old_url)
    _refresh_issues(task_id, listed, old_url)
    await _advance_batch()
    return {"ok": True, "url": listed, "content_type": _cm.url_state(task_id, listed)}


@router.post("/urls/{task_id}")
async def add_url(task_id: str, req: AddUrlRequest):
    """Add a URL to a task as pending: listed as not captured yet, with nothing stored.

    A capture by the extension, or a PDF or MHTML upload, then stores its
    page.  Until then, evaluation treats the URL as not cached.
    """
    _require_loaded()
    if not _cm.get_task_cache(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    url = _valid_url(req.url)
    if not _cm.add_pending_url(task_id, url):
        existing = _cm.canonical_url(task_id, url)
        raise HTTPException(409, "URL already exists in this task" + (f" as {existing}" if existing != url else ""))
    _refresh_issues(task_id, url)
    return {"ok": True, "url": url, "content_type": "pending"}


# ---------------------------------------------------------------------------
# MHTML upload
# ---------------------------------------------------------------------------

@router.post("/upload-mhtml/{task_id}")
async def upload_mhtml(task_id: str, url: str = Query(...), file: UploadFile = File(...)):
    """Store the text of a page saved as MHTML, with a 1x1 placeholder screenshot, since MHTML has none.

    The text comes from the first HTML part (else the first plain-text part)
    of the MIME archive.  A file with no text is refused with status 422, so
    that nothing is stored that was not captured.  The URL's flag is cleared
    and its review status set to "fixed", so a queued URL leaves a running
    batch capture.  ``url`` must be an http(s) URL (status 400 otherwise),
    and the file may have at most ``MAX_UPLOAD_SIZE`` bytes (status 413).
    """
    _require_loaded()
    if not _cm.get_task_cache(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    url = _valid_url(url)

    text = _extract_text_from_mhtml(await _read_upload(file))
    if not text:
        raise HTTPException(422, "The MHTML file has no text to store; capture the page with the extension "
                                 "or upload it as a PDF")
    stored = _cm.store_page(task_id, url, text=text, screenshot=_placeholder_jpeg())
    if stored is None:
        raise HTTPException(500, "Failed to save MHTML content")

    _cm.unflag_url(task_id, stored)
    _cm.mark_url_reviewed(task_id, stored, "fixed")
    _refresh_issues(task_id, stored)

    await _push_event("capture_complete", {"task_id": task_id, "url": stored})
    await _advance_batch()

    return {"ok": True, "url": stored}


# ---------------------------------------------------------------------------
# PDF upload (replace existing content with PDF)
# ---------------------------------------------------------------------------

@router.post("/upload-pdf/{task_id}")
async def upload_pdf(task_id: str, url: str = Query(...), file: UploadFile = File(...),
                     batch: bool = Query(False)):
    """Store an uploaded PDF for a URL, replacing its stored page of either type.

    A file without the PDF signature (``%PDF-`` in its first 1024 bytes),
    such as a login page saved in place of a paper, is refused with status
    422.  The URL's flag is cleared and its review status set to
    "recaptured" for an upload made by a batch capture and "fixed"
    otherwise.  The extension uploads the PDFs its batch finds with
    ``batch``; such an upload is stored only while ``url`` is the URL the
    batch waits for, and then advances the batch, and is otherwise refused
    with status 409, with nothing stored.  ``url`` must be an http(s) URL
    (status 400 otherwise), and the file may have at most
    ``MAX_UPLOAD_SIZE`` bytes (status 413).
    """
    _require_loaded()
    if not _cm.get_task_cache(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    url = _valid_url(url)

    pdf_bytes = await _read_upload(file)
    if b"%PDF-" not in pdf_bytes[:1024]:
        raise HTTPException(422, "Not a PDF file: it does not start with the PDF signature %PDF-")
    if batch:
        _refuse_a_stale_batch_capture(task_id, url)
    stored = _cm.store_page(task_id, url, pdf_bytes=pdf_bytes)
    if stored is None:
        raise HTTPException(500, "Failed to save PDF")

    _cm.unflag_url(task_id, stored)
    _cm.mark_url_reviewed(task_id, stored, "recaptured" if batch else "fixed")
    _refresh_issues(task_id, stored)

    await _push_event("capture_complete", {"task_id": task_id, "url": stored})
    await _advance_batch(done_head=batch)

    return {"ok": True, "url": stored, "content_type": "pdf"}



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listed_url(task_id: str, url: str) -> str:
    """The URL under which the task lists ``url`` (see ``CacheManager.canonical_url``); 404 if the task or URL is unknown."""
    if not _cm.get_task_cache(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    if _cm.url_state(task_id, url) is None:
        raise HTTPException(404, f"URL not found: {url}")
    return _cm.canonical_url(task_id, url)


def _valid_url(url: str) -> str:
    """``url`` without surrounding whitespace; 400 unless it is an http(s) URL that the cache can store."""
    url = url.strip()
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        storage_key(url), normalize_url_simple(url)
    except ValueError:
        raise HTTPException(400, f"Not an http(s) URL: {url!r}")
    return url


def _redirect_url(actual_url: Optional[str], request: Request) -> Optional[str]:
    """The URL a capture was redirected to, if the page is to be stored under it too; else ``None``.

    ``actual_url`` counts only when it is an http(s) URL that the cache can
    store (see :func:`_valid_url`) and its host is neither the ``Host`` of
    ``request`` nor an address of this machine (:func:`_names_this_machine`),
    whichever address the reviewer reached the server at, so a capture never
    adds the Cache Manager's own address to a task.  A host name with a
    trailing dot (``localhost.``) names the same host as without it.
    """
    if not actual_url:
        return None
    try:
        url = _valid_url(actual_url)
    except HTTPException:
        return None
    host = (urlparse(url).hostname or "").rstrip(".")
    if _names_this_machine(host) or host == (request.url.hostname or "").strip("[]").lower().rstrip("."):
        return None
    return url


def _names_this_machine(host: str) -> bool:
    """Whether ``host`` (lowercase, without brackets) is a loopback name or address, or the unspecified address.

    A server bound to a wildcard address answers at all of these, and no page
    that an answer cites is served from them.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


async def _read_upload(file: UploadFile) -> bytes:
    """The content of an uploaded file; status 413 if it has more than ``MAX_UPLOAD_SIZE`` bytes."""
    data = await file.read(config.MAX_UPLOAD_SIZE + 1)
    if len(data) > config.MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"The file exceeds the limit of {config.MAX_UPLOAD_SIZE:,} bytes")
    return data


def _issue_entry(cm: CacheManager, task_id: str, url: str, state: Optional[str]) -> Optional[dict]:
    """The issues of a URL in state ``state`` and their severity, or ``None`` if it has none.

    A pending URL, a failed capture, and a flagged URL are definite issues.
    A stored web page is also checked by the keyword detector, whose
    severity counts when there is no definite issue already.
    """
    if state is None:
        return None
    if state == "pending":
        return {"issues": ["not captured yet"], "severity": "definite"}
    issues = []
    if state == "failed":
        record = cm.get_task_cache(task_id).failure(url, ignore_case=False) or {}
        issues.append(f"capture failed: {record.get('reason', 'unknown reason')}")
    if cm.is_flagged(task_id, url):
        issues.append("flagged")
    severity = "definite" if issues else "possible"
    if state == "web":
        text, _ = cm.get_url_content(task_id, url, get_screenshot=False)
        det = _kd.detect_issues(text)
        if det.has_issues:
            issues += det.matched_keywords + det.matched_patterns
            if det.severity == "definite":
                severity = "definite"
    return {"issues": issues, "severity": severity} if issues else None


def _scan_issues(cm: CacheManager) -> dict:
    """The issue-cache entries (see :func:`_issue_entry`) of every URL of ``cm`` with issues, by task.

    Reads the text of every stored web page, so it runs in a worker thread.
    """
    return {task_id: entries for task_id in cm.get_task_ids() if (entries := _scan_task(cm, task_id))}


def _scan_task(cm: CacheManager, task_id: str) -> dict:
    """The issue-cache entries of the URLs of one task with issues, by URL; reads the text of its web pages."""
    entries = {}
    for info in cm.get_task_urls(task_id):
        entry = _issue_entry(cm, task_id, info.url, info.content_type)
        if entry is not None:
            entries[info.url] = entry
    return entries


def _issue_index(issue_cache: dict) -> list[dict]:
    """One item per URL with issues, by task ID: its task, URL, severity, number of issues, and first five issues."""
    return [{"task_id": task_id, "url": url, "severity": entry["severity"],
             "issue_count": len(entry["issues"]), "keywords": entry["issues"][:5]}
            for task_id in sorted(issue_cache) for url, entry in issue_cache[task_id].items()]


def _task_issues(issue_cache: dict) -> dict:
    """For each task with issues: their number, and "definite" if any of them is, else "possible"."""
    return {task_id: {"count": len(entries),
                      "severity": "definite" if any(e["severity"] == "definite" for e in entries.values())
                      else "possible"}
            for task_id, entries in issue_cache.items() if entries}


def _refresh_issues(task_id: str, *urls: str) -> None:
    """Bring the task's issue-cache entries up to date after an edit that changed ``urls``.

    The entries of ``urls`` are recomputed, those of URLs the task no longer
    lists are dropped, and listed pending and failed URLs without an entry,
    which are always definite issues, get one.  An edit can change the
    listing of URLs other than the ones it names: storing a page ends the
    listing of other spellings of a pending URL, and a change re-reads
    ``pending.json``, which can list URLs that another manager of the folder
    added.
    """
    listed = {info.url: info.content_type for info in _cm.get_task_urls(task_id)}
    task_cache = _url_issue_cache.setdefault(task_id, {})
    for url in [url for url in task_cache if url not in listed]:
        del task_cache[url]
    unscanned = [url for url, state in listed.items() if state in ("pending", "failed") and url not in task_cache]
    for url in dict.fromkeys([*urls, *unscanned]):
        entry = _issue_entry(_cm, task_id, url, listed.get(url))
        if entry is None:
            task_cache.pop(url, None)
        else:
            task_cache[url] = entry


def _placeholder_jpeg() -> bytes:
    """A 1x1 white JPEG, stored as the screenshot of an MHTML upload, which has none."""
    # Minimal 1x1 white JPEG
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
        "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ"
        "CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEA"
        "AAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIh"
        "MUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
        "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
        "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx"
        "8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
        "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
        "YnLRChYkNOEl8RcYI4Q/RFhHRUYnJCk2NzgpOkNERUZHSElKU1RVVldYWVpjZGVm"
        "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6"
        "wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEA"
        "PwD3+gD/2Q=="
    )


def _extract_text_from_mhtml(mhtml_bytes: bytes) -> str:
    """Extract text content from an MHTML file using Python's email module.

    MHTML is a MIME-encoded archive. We find the first text/html part,
    strip HTML tags, and return plain text.
    """
    import email
    import email.policy
    import re
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        """Minimal HTML-to-text converter."""
        def __init__(self):
            super().__init__()
            self._pieces = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style', 'noscript'):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ('script', 'style', 'noscript'):
                self._skip = False
            if tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                        'li', 'tr', 'td', 'th', 'blockquote', 'pre'):
                self._pieces.append('\n')

        def handle_data(self, data):
            if not self._skip:
                self._pieces.append(data)

        def get_text(self):
            raw = ''.join(self._pieces)
            # Collapse whitespace
            raw = re.sub(r'[ \t]+', ' ', raw)
            raw = re.sub(r'\n{3,}', '\n\n', raw)
            return raw.strip()

    try:
        # Parse MHTML as MIME message
        msg = email.message_from_bytes(mhtml_bytes, policy=email.policy.default)

        html_content = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/html':
                html_content = part.get_content()
                if isinstance(html_content, bytes):
                    html_content = html_content.decode('utf-8', errors='replace')
                break
            elif ct == 'text/plain' and html_content is None:
                html_content = part.get_content()
                if isinstance(html_content, bytes):
                    html_content = html_content.decode('utf-8', errors='replace')

        if not html_content:
            return ""

        extractor = _TextExtractor()
        extractor.feed(html_content)
        return extractor.get_text()
    except Exception as e:
        logger.warning(f"Failed to parse MHTML: {e}")
        return ""
