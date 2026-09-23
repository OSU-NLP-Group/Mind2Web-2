"""All API routes for the Cache Manager web backend."""

from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from mind2web2.utils.cache_filesys import storage_key
from mind2web2.utils.url_tools import normalize_url_simple

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

# Batch capture state
_batch_queue: list[dict] = []   # [{task_id, url}, ...]
_batch_active: bool = False
_batch_total: int = 0
_batch_completed: int = 0

# SSE subscribers — each is an asyncio.Queue
_sse_queues: list[asyncio.Queue] = []


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
    text: str
    screenshot_base64: str  # JPEG base64
    actual_url: Optional[str] = None  # URL after redirects (may differ from url)

class ReviewRequest(BaseModel):
    url: str
    status: str  # "ok", "fixed", "skip", ""

class CaptureTargetRequest(BaseModel):
    task_id: str
    url: str

class AddUrlRequest(BaseModel):
    url: str

class AddPdfRequest(BaseModel):
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
    leaves the loaded cache in place.
    """
    global _cm, _url_issue_cache
    p = Path(req.path).resolve()
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")
    cm = CacheManager()
    try:
        ok, total = await asyncio.to_thread(cm.load_agent_cache, str(p))
        issue_cache = await asyncio.to_thread(_scan_issues, cm)
    except Exception as e:
        raise HTTPException(500, str(e))
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
                "issue_urls": summary.issue_urls,
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
    ct = cache.has(url)
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
    ct = cache.has(url)
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
async def receive_capture(req: CaptureRequest):
    """Receive captured content from the Chrome extension."""
    _require_loaded()
    cache = _cm.get_task_cache(req.task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {req.task_id}")

    try:
        screenshot_bytes = base64.b64decode(req.screenshot_base64)
    except Exception:
        raise HTTPException(400, "Invalid base64 screenshot data")

    text = req.text or ""

    # Update cache for the original URL
    success = _cm.update_url_content(req.task_id, req.url, text, screenshot_bytes)
    if not success:
        # Try adding as new URL
        success = _cm.add_url_to_task(req.task_id, req.url, text=text, screenshot=screenshot_bytes)
    if not success:
        raise HTTPException(500, "Failed to save capture")

    # If redirected to a different URL, also save for the actual URL
    if req.actual_url and req.actual_url != req.url:
        if not _cm.update_url_content(req.task_id, req.actual_url, text, screenshot_bytes):
            _cm.add_url_to_task(req.task_id, req.actual_url, text=text, screenshot=screenshot_bytes)
        # Mark redirect URL as reviewed too
        review_status = "recaptured" if _batch_active else "fixed"
        _cm.mark_url_reviewed(req.task_id, req.actual_url, review_status)

    # Mark review status: "recaptured" for batch (needs human review), "fixed" for single
    review_status = "recaptured" if _batch_active else "fixed"
    _cm.mark_url_reviewed(req.task_id, req.url, review_status)

    # Clear the flags and rescan the new content
    for url in {req.url, req.actual_url or req.url}:
        _cm.unflag_url(req.task_id, url)
        _refresh_issue(req.task_id, url)

    # Push SSE event to frontend
    await _push_event("capture_complete", {
        "task_id": req.task_id,
        "url": req.url,
    })

    # Advance batch queue if active
    if _batch_active:
        await _advance_batch()

    return {"ok": True, "task_id": req.task_id, "url": req.url}


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

@router.get("/review/{task_id}")
async def get_review(task_id: str):
    _require_loaded()
    reviewed = _cm.load_reviewed(task_id)
    return {"task_id": task_id, "reviewed": reviewed}


@router.post("/review/{task_id}")
async def set_review(task_id: str, req: ReviewRequest):
    _require_loaded()
    _cm.mark_url_reviewed(task_id, req.url, req.status)
    return {"ok": True}


@router.post("/flag/{task_id}")
async def flag_url(task_id: str, req: FlagRequest):
    """Flag a URL as needing a (re)capture: a definite issue, queued by batch recapture.

    Only ``flags.json`` changes; the stored page, which evaluation reads,
    stays as it is until a capture or upload replaces it.
    """
    _require_loaded()
    _require_url(task_id, req.url)
    _cm.flag_url(task_id, req.url)
    _cm.mark_url_reviewed(task_id, req.url, "")
    _refresh_issue(task_id, req.url)
    return {"ok": True}


@router.post("/reset/{task_id}")
async def reset_url(task_id: str, req: FlagRequest):
    """Delete a URL's stored page or failure record and flag the URL, which leaves it pending.

    Until the URL is captured again, evaluation treats it as not cached and
    captures it live.  Returns what was deleted: ``"web"``, ``"pdf"``, or
    ``"failed"``.
    """
    _require_loaded()
    _require_url(task_id, req.url)
    content_type = _cm.reset_url(task_id, req.url)
    if content_type is None:
        raise HTTPException(409, f"Nothing is stored for {req.url}; it is already pending")
    _cm.mark_url_reviewed(task_id, req.url, "")
    _refresh_issue(task_id, req.url)
    await _push_event("capture_complete", {"task_id": task_id, "url": req.url})
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

async def _advance_batch():
    """Pop the completed item and advance to the next URL in the batch queue."""
    global _batch_queue, _batch_active, _batch_completed, _batch_total, _capture_target

    # Pop the completed item
    if _batch_queue:
        _batch_queue.pop(0)
    _batch_completed += 1

    if _batch_queue:
        # Set next item as capture target
        nxt = _batch_queue[0]
        _capture_target = {"task_id": nxt["task_id"], "url": nxt["url"], "ts": time.time()}
        await _push_event("batch_progress", {
            "completed": _batch_completed,
            "total": _batch_total,
            "remaining": len(_batch_queue),
            "next": nxt,
        })
    else:
        # Batch complete
        _batch_active = False
        await _push_event("batch_complete", {
            "completed": _batch_completed,
            "total": _batch_total,
        })


@router.post("/capture/batch/start")
async def batch_start(req: BatchStartRequest):
    """Start a batch capture session with a queue of URLs.

    Filters to only definite-issue unreviewed URLs.
    """
    _require_loaded()
    global _batch_queue, _batch_active, _batch_total, _batch_completed, _capture_target

    # Filter: only definite-severity, unreviewed, web-only URLs (extension can't capture PDFs)
    queue = []
    for item in req.items:
        cache = _cm.get_task_cache(item.task_id)
        if cache and cache.has(item.url) == "pdf":
            continue
        issue_cache = _url_issue_cache.get(item.task_id, {})
        issue_info = issue_cache.get(item.url)
        if not issue_info or issue_info.get("severity") != "definite":
            continue
        reviewed = _cm.load_reviewed(item.task_id)
        if item.url in reviewed:
            continue
        queue.append({"task_id": item.task_id, "url": item.url})

    if not queue:
        return {"ok": True, "total": 0, "message": "No qualifying URLs to capture"}

    _batch_queue = queue
    _batch_active = True
    _batch_total = len(queue)
    _batch_completed = 0

    # Set first item as capture target
    first = _batch_queue[0]
    _capture_target = {"task_id": first["task_id"], "url": first["url"], "ts": time.time()}

    await _push_event("batch_started", {"total": _batch_total})
    return {"ok": True, "total": _batch_total}


@router.get("/capture/batch/status")
async def batch_status():
    """Get current batch capture status (polled by extension)."""
    if not _batch_active:
        return {"active": False}

    current = _batch_queue[0] if _batch_queue else None
    return {
        "active": True,
        "total": _batch_total,
        "completed": _batch_completed,
        "remaining": len(_batch_queue),
        "current": current,
    }


class CaptchaNotify(BaseModel):
    type: str = "unknown"

@router.post("/capture/batch/captcha")
async def batch_captcha_notify(req: CaptchaNotify):
    """Called by extension when CAPTCHA is detected during batch mode."""
    await _push_event("batch_captcha", {"captcha_type": req.type})
    return {"ok": True}


@router.post("/capture/batch/skip")
async def batch_skip():
    """Skip the current batch URL (e.g., capture failed, page unreachable)."""
    global _batch_active
    if not _batch_active:
        return {"ok": False, "message": "No active batch"}
    await _advance_batch()
    return {"ok": True, "remaining": len(_batch_queue)}


@router.post("/capture/batch/stop")
async def batch_stop():
    """Stop the current batch capture."""
    global _batch_queue, _batch_active, _batch_total, _batch_completed
    _batch_queue = []
    _batch_active = False
    _batch_total = 0
    _batch_completed = 0
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
    """Delete a URL from a task: its stored page, failure record, flag, and review status."""
    _require_loaded()
    _require_url(task_id, url)
    if not _cm.delete_url(task_id, url):
        raise HTTPException(500, "Failed to delete URL")
    _cm.mark_url_reviewed(task_id, url, "")
    _url_issue_cache.get(task_id, {}).pop(url, None)
    return {"ok": True}


@router.post("/urls/{task_id}/rename")
async def rename_url(task_id: str, req: RenameUrlRequest):
    """Change a URL's link.

    A stored page moves to the new URL, with its flag and review status.  A
    URL whose capture failed or that is pending leaves the new URL pending,
    since the new link has not been captured.
    """
    _require_loaded()
    old_url, new_url = req.old_url, _valid_url(req.new_url)
    state = _require_url(task_id, old_url)
    if _cm.url_state(task_id, new_url) is not None:
        raise HTTPException(409, f"New URL already exists: {new_url}")

    if state == "web":
        text, screenshot = _cm.get_url_content(task_id, old_url)
        success = _cm.add_url_to_task(task_id, new_url, text=text or "",
                                      screenshot=screenshot if screenshot is not None else _placeholder_jpeg())
    elif state == "pdf":
        _, pdf_bytes = _cm.get_url_content(task_id, old_url)
        if pdf_bytes is None:
            raise HTTPException(500, "Failed to read PDF content")
        success = _cm.add_url_to_task(task_id, new_url, pdf_bytes=pdf_bytes)
    else:
        success = _cm.add_pending_url(task_id, new_url)
    if not success:
        raise HTTPException(500, "Failed to create new URL")

    if state in ("web", "pdf"):
        if _cm.is_flagged(task_id, old_url):
            _cm.flag_url(task_id, new_url)
        old_status = _cm.load_reviewed(task_id).get(old_url, "")
        if old_status:
            _cm.mark_url_reviewed(task_id, new_url, old_status)

    _cm.delete_url(task_id, old_url)
    _cm.mark_url_reviewed(task_id, old_url, "")
    _url_issue_cache.get(task_id, {}).pop(old_url, None)
    _refresh_issue(task_id, new_url)
    return {"ok": True, "content_type": _cm.url_state(task_id, new_url)}


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
        raise HTTPException(409, "URL already exists in this task")
    _refresh_issue(task_id, url)
    return {"ok": True, "content_type": "pending"}


@router.post("/urls/{task_id}/pdf")
async def add_pdf_url(task_id: str, req: AddPdfRequest, file: UploadFile = File(...)):
    _require_loaded()
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {task_id}")
    pdf_bytes = await file.read()
    success = _cm.add_url_to_task(task_id, req.url, pdf_bytes=pdf_bytes)
    if not success:
        raise HTTPException(500, "Failed to add PDF")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MHTML upload
# ---------------------------------------------------------------------------

@router.post("/upload-mhtml/{task_id}")
async def upload_mhtml(task_id: str, url: str = Query(...), file: UploadFile = File(...)):
    """Upload an MHTML file to update a URL's cached content.

    Parses MHTML using Python's email module.
    Extracts text from the first HTML part; uses a placeholder screenshot.
    """
    _require_loaded()

    mhtml_bytes = await file.read()
    text = _extract_text_from_mhtml(mhtml_bytes)
    if not text:
        text = f"Content from MHTML upload for {url}"

    screenshot = _placeholder_jpeg()

    if not _cm.update_url_content(task_id, url, text, screenshot):
        if not _cm.add_url_to_task(task_id, url, text=text, screenshot=screenshot):
            raise HTTPException(500, "Failed to save MHTML content")

    _cm.unflag_url(task_id, url)
    _refresh_issue(task_id, url)

    review_status = "recaptured" if _batch_active else "fixed"
    _cm.mark_url_reviewed(task_id, url, review_status)

    await _push_event("capture_complete", {"task_id": task_id, "url": url})

    if _batch_active:
        await _advance_batch()

    return {"ok": True}


# ---------------------------------------------------------------------------
# PDF upload (replace existing content with PDF)
# ---------------------------------------------------------------------------

@router.post("/upload-pdf/{task_id}")
async def upload_pdf(task_id: str, url: str = Query(...), file: UploadFile = File(...)):
    """Upload a PDF to replace existing content for a URL.

    Handles content type switching (web -> pdf) with proper file cleanup.
    Removes any flags and marks the URL as fixed.
    """
    _require_loaded()
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {task_id}")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "Empty PDF file")

    if cache.has(url):
        success = _cm.replace_with_pdf(task_id, url, pdf_bytes)
    else:
        success = _cm.add_url_to_task(task_id, url, pdf_bytes=pdf_bytes)

    if not success:
        raise HTTPException(500, "Failed to save PDF")

    _cm.unflag_url(task_id, url)
    _refresh_issue(task_id, url)

    # Use "recaptured" in batch mode (needs human review), "fixed" otherwise
    review_status = "recaptured" if _batch_active else "fixed"
    _cm.mark_url_reviewed(task_id, url, review_status)

    # Push SSE event so frontend updates immediately
    await _push_event("capture_complete", {"task_id": task_id, "url": url})

    # Advance batch queue if active
    if _batch_active:
        await _advance_batch()

    return {"ok": True, "content_type": "pdf"}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@router.post("/scan")
async def scan_all():
    """Scan every URL for issues again, in a worker thread."""
    global _url_issue_cache
    _require_loaded()
    _url_issue_cache = await asyncio.to_thread(_scan_issues, _cm)
    issue_index = _issue_index(_url_issue_cache)
    return {"issue_count": len(issue_index), "issues": issue_index}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_url(task_id: str, url: str) -> str:
    """The state of ``url`` in the task (see ``CacheManager.url_state``); 404 if the task or URL is unknown."""
    if not _cm.get_task_cache(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    state = _cm.url_state(task_id, url)
    if state is None:
        raise HTTPException(404, f"URL not found: {url}")
    return state


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
        record = cm.get_task_cache(task_id).failure(url) or {}
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
    issue_cache = {}
    for task_id in cm.get_task_ids():
        for info in cm.get_task_urls(task_id):
            entry = _issue_entry(cm, task_id, info.url, info.content_type)
            if entry is not None:
                issue_cache.setdefault(task_id, {})[info.url] = entry
    return issue_cache


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


def _refresh_issue(task_id: str, url: str) -> None:
    """Recompute the issue-cache entry of one URL after it changed."""
    task_cache = _url_issue_cache.setdefault(task_id, {})
    entry = _issue_entry(_cm, task_id, url, _cm.url_state(task_id, url))
    if entry is None:
        task_cache.pop(url, None)
    else:
        task_cache[url] = entry


def _placeholder_jpeg() -> bytes:
    """A 1x1 white JPEG, stored as the screenshot of a page that has none (an MHTML upload)."""
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
