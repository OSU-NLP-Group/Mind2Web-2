"""All API routes for the Cache Manager web backend."""

from __future__ import annotations
import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from cache_manager.models import CacheManager, KeywordDetector

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Shared application state (set by lifespan)
# ---------------------------------------------------------------------------

_cm: Optional[CacheManager] = None
_kd: Optional[KeywordDetector] = None

# Active capture target — tells the extension what task/URL we're capturing for
_capture_target: dict = {}  # {"task_id": ..., "url": ..., "ts": ...}

# Per-URL issue cache: {task_id: {url: {"issues": [...], "severity": "..."}}}
_url_issue_cache: dict = {}

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

class ReviewRequest(BaseModel):
    url: str
    status: str  # "ok", "fixed", "skip", ""

class CaptureTargetRequest(BaseModel):
    task_id: str
    url: str

class AddUrlRequest(BaseModel):
    url: str
    text: Optional[str] = None
    screenshot_base64: Optional[str] = None

class AddPdfRequest(BaseModel):
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
    p = Path(req.path).resolve()
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")
    try:
        ok, total = _cm.load_agent_cache(str(p))
        stats = _cm.get_statistics()

        # Run issue scan
        issues_map = {}
        try:
            issues_map = _kd.scan_all_text_content(_cm)
        except Exception as e:
            logger.warning(f"Issue scan failed: {e}")

        # Build per-URL issue cache and issue index
        global _url_issue_cache
        _url_issue_cache = {}
        issue_index = []
        for task_id in sorted(issues_map.keys()):
            _url_issue_cache[task_id] = {}
            for url, det in issues_map[task_id]:
                _url_issue_cache[task_id][url] = {
                    "issues": det.matched_keywords + det.matched_patterns,
                    "severity": det.severity,
                }
                issue_index.append({
                    "task_id": task_id,
                    "url": url,
                    "severity": det.severity,
                    "issue_count": det.issue_count,
                    "keywords": det.matched_keywords[:5],
                })

        # Build task issue summaries
        task_issues = {}
        for task_id, results in issues_map.items():
            worst = "possible"
            for _, det in results:
                if det.severity == "definite":
                    worst = "definite"
                    break
            task_issues[task_id] = {"count": len(results), "severity": worst}

        return {
            "ok": True,
            "agent_name": _cm.agent_name,
            "agent_path": str(p),
            "loaded_tasks": ok,
            "total_tasks": total,
            "stats": stats,
            "task_issues": task_issues,
            "issue_index": issue_index,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


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
            issue_reviewed = sum(1 for url in task_issue_cache if url in reviewed)
            tasks.append({
                "task_id": summary.task_id,
                "total_urls": summary.total_urls,
                "web_urls": summary.web_urls,
                "pdf_urls": summary.pdf_urls,
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
    # Detect issues
    det = _kd.detect_issues(text)
    return {"text": text, "issues": {
        "has_issues": det.has_issues,
        "keywords": det.matched_keywords,
        "patterns": det.matched_patterns,
        "severity": det.severity,
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

    # Update cache
    success = _cm.update_url_content(req.task_id, req.url, text, screenshot_bytes)
    if not success:
        # Try adding as new URL
        success = _cm.add_url_to_task(req.task_id, req.url, text=text, screenshot=screenshot_bytes)
    if not success:
        raise HTTPException(500, "Failed to save capture")

    # Auto-mark as reviewed
    _cm.mark_url_reviewed(req.task_id, req.url, "fixed")

    # Invalidate issue cache for this URL (content changed)
    if req.task_id in _url_issue_cache:
        _url_issue_cache[req.task_id].pop(req.url, None)

    # Push SSE event to frontend
    await _push_event("capture_complete", {
        "task_id": req.task_id,
        "url": req.url,
    })

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
            fixed_issues += sum(1 for url in task_issue_cache if url in reviewed)
    return {"total": total_issues, "reviewed": fixed_issues}


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

@router.get("/answers/{task_id}")
async def list_answers(task_id: str):
    _require_loaded()
    if not _cm.agent_path:
        return {"files": []}

    # Look for answers in answers/<agent_name>/<task_id>/
    answers_dir = _cm.agent_path.parent / "answers" / _cm.agent_name / task_id
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
    _require_loaded()
    if _cm.delete_url(task_id, url):
        return {"ok": True}
    raise HTTPException(500, "Failed to delete URL")


@router.post("/urls/{task_id}")
async def add_url(task_id: str, req: AddUrlRequest):
    _require_loaded()
    cache = _cm.get_task_cache(task_id)
    if not cache:
        raise HTTPException(404, f"Task not found: {task_id}")
    if cache.has(req.url):
        raise HTTPException(409, "URL already exists in this task")

    text = req.text or f"Placeholder content for {req.url}"
    screenshot = None
    if req.screenshot_base64:
        screenshot = base64.b64decode(req.screenshot_base64)
    else:
        # Create minimal placeholder JPEG
        screenshot = _placeholder_jpeg()

    success = _cm.add_url_to_task(task_id, req.url, text=text, screenshot=screenshot)
    if not success:
        raise HTTPException(500, "Failed to add URL")
    return {"ok": True}


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

    Parses MHTML using Python's email module (no PySide6/Qt dependency).
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

    _cm.mark_url_reviewed(task_id, url, "fixed")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@router.post("/scan")
async def scan_all():
    _require_loaded()
    issues_map = _kd.scan_all_text_content(_cm)
    issue_index = []
    for task_id in sorted(issues_map.keys()):
        for url, det in issues_map[task_id]:
            issue_index.append({
                "task_id": task_id,
                "url": url,
                "severity": det.severity,
                "issue_count": det.issue_count,
                "keywords": det.matched_keywords[:5],
            })
    return {"issue_count": len(issue_index), "issues": issue_index}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _placeholder_jpeg() -> bytes:
    """Generate a tiny valid JPEG placeholder."""
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
