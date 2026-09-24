"""Cache Manager: page edits keep the task's index, files, and counts consistent, and failed URLs are reviewable."""
from __future__ import annotations

import base64
import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from cache_manager_web.backend.app import app
from cache_manager_web.backend.models.cache_manager import CacheManager
from mind2web2.utils.cache_filesys import CacheFileSys


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), (0, 128, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_edits_switch_content_types_and_delete_pages(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web("https://example.com/a", "page a", png_bytes())
    cache.put_pdf("https://example.com/b.pdf", b"%PDF-1.4")
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (1, 1)

    assert manager.update_url_content("task", "http://www.example.com/b.pdf", "now a web page", png_bytes())
    assert manager.replace_with_pdf("task", "https://example.com/a/", b"%PDF-1.4 new")
    assert manager.get_url_content("task", "https://example.com/b.pdf", get_screenshot=False) == ("now a web page", None)
    assert manager.reset_url("task", "https://example.com/a") == "pdf"
    assert manager.delete_url("task", "https://example.com/b.pdf")
    assert not manager.delete_url("task", "https://example.com/never-cached")

    assert json.loads((task_dir / "index.json").read_text()) == {"https://example.com/a": "pdf"}
    assert sorted(p.suffix for p in task_dir.iterdir()) == [".json", ".pdf"]
    summary = manager.get_task_summary("task")
    assert (summary.total_urls, summary.web_urls, summary.pdf_urls) == (1, 0, 1)


def summary_counts(manager: CacheManager, task_id: str) -> tuple[int, int, int, int]:
    summary = manager.get_task_summary(task_id)
    return summary.total_urls, summary.web_urls, summary.pdf_urls, summary.failed_urls


def test_failed_urls_are_listed_until_captured_or_deleted(tmp_path):
    cache = CacheFileSys(str(tmp_path / "agent" / "task"))
    cache.put_web("https://example.com/a", "page a", png_bytes())
    cache.record_failure("https://example.com/blocked", "blocked: HTTP 403", blocked=True)
    cache.record_failure("https://example.com/dead", "navigation failed: net::ERR_NAME_NOT_RESOLVED")
    CacheFileSys(str(tmp_path / "agent" / "only_failures")).record_failure("https://example.com/x", "HTTP 503")
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (2, 2)

    assert {info.url: info.content_type for info in manager.get_task_urls("task")} == {
        "https://example.com/a": "web", "https://example.com/blocked": "failed", "https://example.com/dead": "failed"}
    assert summary_counts(manager, "task") == (3, 1, 0, 2)

    assert manager.update_url_content("task", "https://example.com/blocked", "captured by hand", png_bytes())
    assert manager.delete_url("task", "https://example.com/dead")
    assert {info.url: info.content_type for info in manager.get_task_urls("task")} == {
        "https://example.com/a": "web", "https://example.com/blocked": "web"}
    assert summary_counts(manager, "task") == (2, 2, 0, 0)
    assert manager.get_statistics()["failed_urls"] == 1  # the other task's
    assert json.loads((tmp_path / "agent" / "task" / "failures.json").read_text()) == {}


def test_failed_urls_are_definite_issues_and_a_capture_replaces_them(tmp_path):
    cache = CacheFileSys(str(tmp_path / "agent" / "task"))
    cache.put_web("https://example.com/a", "page a", png_bytes())
    cache.record_failure("https://example.com/blocked", "blocked: HTTP 403", blocked=True)

    with TestClient(app) as client:
        loaded = client.post("/api/load", json={"path": str(tmp_path / "agent")}).json()
        assert [(i["url"], i["severity"], i["keywords"]) for i in loaded["issue_index"]] == [
            ("https://example.com/blocked", "definite", ["capture failed: blocked: HTTP 403"])]
        failed = next(u for u in client.get("/api/tasks/task/urls").json()["urls"] if u["content_type"] == "failed")
        assert (failed["url"], failed["failure"]["blocked"]) == ("https://example.com/blocked", True)

        capture = {"task_id": "task", "url": "https://example.com/blocked", "text": "captured by hand",
                   "screenshot_base64": base64.b64encode(png_bytes()).decode()}
        assert client.post("/api/capture", json=capture).json()["ok"]
        urls = client.get("/api/tasks/task/urls").json()["urls"]
        assert {u["url"]: u["content_type"] for u in urls} == {
            "https://example.com/a": "web", "https://example.com/blocked": "web"}
        assert client.post("/api/scan").json()["issue_count"] == 0
