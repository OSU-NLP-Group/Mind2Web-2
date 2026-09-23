"""Cache Manager: edits keep the cache consistent and never store content that was not captured, and only its own page may use the API."""
from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from cache_manager_web import run as run_script
from cache_manager_web.backend.api import routes
from cache_manager_web.backend.app import LocalRequestGuard, _host_name, app
from cache_manager_web.backend.models.cache_manager import CacheManager, ReviewStateError
from cache_manager_web.backend.models.keyword_detector import KeywordDetector
from mind2web2.utils.cache_filesys import CacheFileSys, storage_key

A, B = "https://example.com/a", "https://example.com/b"


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), (0, 128, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def client() -> TestClient:
    """A client of the app as the Cache Manager page at http://127.0.0.1:8000 reaches it."""
    return TestClient(app, base_url="http://127.0.0.1:8000")


def capture(url: str, text: str = "captured by hand") -> dict:
    return {"task_id": "task", "url": url, "text": text, "screenshot_base64": base64.b64encode(png_bytes()).decode()}


def url_states(c: TestClient) -> dict[str, tuple[str, list[str], str]]:
    """Each URL of the task: its content type, issues, and severity."""
    return {u["url"]: (u["content_type"], u["issues"], u["severity"])
            for u in c.get("/api/tasks/task/urls").json()["urls"]}


def url_file(tmp_path, name: str) -> list[str]:
    path = tmp_path / "agent" / "task" / name
    return json.loads(path.read_text()) if path.exists() else []


def flags(tmp_path) -> list[str]:
    return url_file(tmp_path, "flags.json")


def pending(tmp_path) -> list[str]:
    return url_file(tmp_path, "pending.json")


# ------------------------------------------------------------------ the CacheManager model

def test_edits_switch_content_types_reset_and_delete_pages(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web(A, "page a", png_bytes())
    cache.put_pdf("https://example.com/b.pdf", b"%PDF-1.4")
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (1, 1)

    assert manager.update_url_content("task", "http://www.example.com/b.pdf", "now a web page", png_bytes())
    assert manager.replace_with_pdf("task", "https://example.com/a/", b"%PDF-1.4 new")
    assert manager.get_url_content("task", "https://example.com/b.pdf", get_screenshot=False) == ("now a web page", None)
    assert manager.reset_url("task", A) == "pdf"
    assert manager.reset_url("task", A) is None  # nothing is left to delete
    assert manager.delete_url("task", "https://example.com/b.pdf")
    assert not manager.delete_url("task", "https://example.com/never-cached")

    assert json.loads((task_dir / "index.json").read_text()) == {}
    assert sorted(p.name for p in task_dir.iterdir()) == ["index.json", "pending.json"]
    assert [(info.url, info.content_type) for info in manager.get_task_urls("task")] == [(A, "pending")]
    summary = manager.get_task_summary("task")
    assert (summary.total_urls, summary.web_urls, summary.pdf_urls, summary.pending_urls) == (1, 0, 0, 1)


def summary_counts(manager: CacheManager, task_id: str) -> tuple[int, int, int, int]:
    summary = manager.get_task_summary(task_id)
    return summary.total_urls, summary.web_urls, summary.pdf_urls, summary.failed_urls


def test_failed_urls_are_listed_until_captured_or_deleted(tmp_path):
    cache = CacheFileSys(str(tmp_path / "agent" / "task"))
    cache.put_web(A, "page a", png_bytes())
    cache.record_failure("https://example.com/blocked", "blocked: HTTP 403", blocked=True)
    cache.record_failure("https://example.com/dead", "navigation failed: net::ERR_NAME_NOT_RESOLVED")
    CacheFileSys(str(tmp_path / "agent" / "only_failures")).record_failure("https://example.com/x", "HTTP 503")
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (2, 2)

    assert {info.url: info.content_type for info in manager.get_task_urls("task")} == {
        A: "web", "https://example.com/blocked": "failed", "https://example.com/dead": "failed"}
    assert summary_counts(manager, "task") == (3, 1, 0, 2)

    assert manager.update_url_content("task", "https://example.com/blocked", "captured by hand", png_bytes())
    assert manager.delete_url("task", "https://example.com/dead")
    assert {info.url: info.content_type for info in manager.get_task_urls("task")} == {
        A: "web", "https://example.com/blocked": "web"}
    assert summary_counts(manager, "task") == (2, 2, 0, 0)
    assert manager.get_statistics()["failed_urls"] == 1  # the other task's
    assert json.loads((tmp_path / "agent" / "task" / "failures.json").read_text()) == {}


def test_a_task_with_only_pending_urls_is_loaded_and_bare_flags_are_ignored(tmp_path):
    (tmp_path / "agent" / "task").mkdir(parents=True)
    (tmp_path / "agent" / "task" / "pending.json").write_text(json.dumps([A, "not a url"]))
    (tmp_path / "agent" / "task" / "flags.json").write_text(json.dumps([B]))  # a flag without a page
    (tmp_path / "agent" / "flags_only").mkdir()
    (tmp_path / "agent" / "flags_only" / "flags.json").write_text(json.dumps([A]))
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (1, 2)
    assert [(info.url, info.content_type) for info in manager.get_task_urls("task")] == [
        (A, "pending"), ("not a url", "pending")]
    assert manager.url_state("task", B) is None


def test_review_state_changes_by_two_managers_of_one_folder_are_all_kept(tmp_path):
    # While /api/load builds a new manager, the one it replaces keeps serving edits to the same files
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web(A, "page a", png_bytes())
    cache.put_web(B, "page b", png_bytes())
    p, q = "https://example.com/p", "https://example.com/q"
    (task_dir / "pending.json").write_text(json.dumps([p]))
    serving, loading = CacheManager(), CacheManager()
    serving.load_agent_cache(tmp_path / "agent")
    loading.load_agent_cache(tmp_path / "agent")

    serving.flag_url("task", A)
    serving.store_page("task", p, text="captured during the load", screenshot=png_bytes())
    serving.mark_url_reviewed("task", A, "skip")
    loading.flag_url("task", B)
    loading.add_pending_url("task", q)
    loading.mark_url_reviewed("task", B, "ok")

    assert (flags(tmp_path), pending(tmp_path)) == ([A, B], [q])
    assert url_file(tmp_path, "reviewed.json") == {A: "skip", B: "ok"}
    assert loading.is_flagged("task", A)  # a change re-reads the file it changes


def test_a_review_state_file_that_cannot_be_read_is_never_overwritten(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    manager = CacheManager()
    assert manager.load_agent_cache(tmp_path / "agent") == (1, 1)
    cut_short = '["https://example.com/a", '
    for name in ("flags.json", "pending.json", "reviewed.json"):
        (task_dir / name).write_text(cut_short)

    with pytest.raises(ReviewStateError, match="flags.json"):
        manager.flag_url("task", A)
    with pytest.raises(ReviewStateError, match="pending.json"):
        manager.add_pending_url("task", B)
    with pytest.raises(ReviewStateError, match="reviewed.json"):
        manager.mark_url_reviewed("task", A, "ok")
    assert [(task_dir / name).read_text() for name in ("flags.json", "pending.json", "reviewed.json")] == [
        cut_short] * 3
    assert CacheManager().load_agent_cache(tmp_path / "agent") == (0, 1)  # not loaded, so no edit can reach it

    with client() as c:
        (task_dir / "pending.json").unlink()
        (task_dir / "reviewed.json").unlink()
        (task_dir / "flags.json").write_text("[]")
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        (task_dir / "flags.json").write_text(cut_short)
        response = c.post("/api/flag/task", json={"url": A})
        assert response.status_code == 500 and "flags.json" in response.json()["detail"]
    assert (task_dir / "flags.json").read_text() == cut_short


# ------------------------------------------------------------------ the API

def test_failed_urls_are_definite_issues_and_a_capture_replaces_them(tmp_path):
    cache = CacheFileSys(str(tmp_path / "agent" / "task"))
    cache.put_web(A, "page a", png_bytes())
    cache.record_failure("https://example.com/blocked", "blocked: HTTP 403", blocked=True)

    with client() as c:
        loaded = c.post("/api/load", json={"path": str(tmp_path / "agent")}).json()
        assert [(i["url"], i["severity"], i["keywords"]) for i in loaded["issue_index"]] == [
            ("https://example.com/blocked", "definite", ["capture failed: blocked: HTTP 403"])]
        failed = next(u for u in c.get("/api/tasks/task/urls").json()["urls"] if u["content_type"] == "failed")
        assert (failed["url"], failed["failure"]["blocked"]) == ("https://example.com/blocked", True)

        assert c.post("/api/capture", json=capture("https://example.com/blocked")).json()["ok"]
        assert {url: state[0] for url, state in url_states(c).items()} == {
            A: "web", "https://example.com/blocked": "web"}
        assert c.get("/api/issues").json()["issue_index"] == []
        assert c.post("/api/scan").json()["issue_count"] == 0


def test_flagging_keeps_the_stored_page(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a mentions a captcha", png_bytes())
    stored = CacheFileSys(str(task_dir)).get_web(A)

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        assert url_states(c)[A] == ("web", ["captcha"], "possible")
        assert c.post("/api/flag/task", json={"url": A}).json() == {"ok": True}
        assert c.post("/api/flag/task", json={"url": B}).status_code == 404

        assert CacheFileSys(str(task_dir)).get_web(A) == stored  # evaluation still reads the captured page
        assert flags(tmp_path) == [A]
        issues = c.get(f"/api/content/task/text?url={A}").json()["issues"]
        assert (issues["keywords"], issues["severity"]) == (["flagged", "captcha"], "definite")
        assert c.get("/api/issues").json()["issue_index"][0]["keywords"] == ["flagged", "captcha"]

        c.post("/api/load", json={"path": str(tmp_path / "agent")})  # a flag outranks a possible keyword
        assert url_states(c)[A] == ("web", ["flagged", "captcha"], "definite")


def test_reset_deletes_the_stored_page_and_leaves_the_url_pending(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web(A, "page a", png_bytes())
    cache.record_failure(B, "HTTP 503")

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        for url in (A, B):
            assert c.post("/api/flag/task", json={"url": url}).json() == {"ok": True}
        assert flags(tmp_path) == [A, B]
        assert c.post("/api/reset/task", json={"url": A}).json() == {"ok": True, "content_type": "web"}
        assert c.post("/api/reset/task", json={"url": B}).json() == {"ok": True, "content_type": "failed"}
        assert c.post("/api/reset/task", json={"url": A}).status_code == 409  # already pending
        assert url_states(c) == {A: ("pending", ["not captured yet"], "definite"),
                                 B: ("pending", ["not captured yet"], "definite")}

        # Evaluation sees neither a page nor a failure, so it captures both live
        fresh = CacheFileSys(str(task_dir))
        assert (fresh.has(A), fresh.failure(A), fresh.has(B), fresh.failure(B)) == (None, None, None, None)
        assert json.loads((task_dir / "index.json").read_text()) == {}
        assert (pending(tmp_path), flags(tmp_path)) == ([A, B], [])

        assert c.post("/api/capture", json=capture(A)).json()["ok"]
        assert CacheFileSys(str(task_dir)).get_web(A, get_screenshot=False)[0] == "captured by hand"
        assert pending(tmp_path) == [B]
        assert url_states(c)[A] == ("web", [], "")


def test_added_urls_are_pending_and_can_be_renamed_or_deleted(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web(A, "page a", png_bytes())
    cache.record_failure(B, "HTTP 503")
    new = "https://example.com/new"

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        assert c.post("/api/urls/task", json={"url": f" {new} "}).json() == {"ok": True, "url": new,
                                                                              "content_type": "pending"}
        assert c.post("/api/urls/task", json={"url": new}).status_code == 409
        assert c.post("/api/urls/task", json={"url": "http://www.example.com/new/"}).status_code == 409
        assert c.post("/api/urls/task", json={"url": "http://www.example.com/a/"}).status_code == 409  # stored as A
        assert c.post("/api/urls/task", json={"url": B}).status_code == 409
        assert c.post("/api/urls/task", json={"url": "example.com/c"}).status_code == 400
        assert json.loads((task_dir / "index.json").read_text()) == {A: "web"}
        assert url_states(c)[new] == ("pending", ["not captured yet"], "definite")
        assert {i["url"] for i in c.get("/api/issues").json()["issue_index"]} == {B, new}

        # A pending URL keeps no content when renamed, nor does a failed one
        rename = {"old_url": new, "new_url": new + "2"}
        assert c.post("/api/urls/task/rename", json=rename).json() == {"ok": True, "url": new + "2",
                                                                       "content_type": "pending"}
        rename = {"old_url": B, "new_url": B + "2"}
        assert c.post("/api/urls/task/rename", json=rename).json() == {"ok": True, "url": B + "2",
                                                                       "content_type": "pending"}
        assert CacheFileSys(str(task_dir)).failures() == {}
        assert pending(tmp_path) == [B + "2", new + "2"]

        # A stored page moves with its flag and review status
        c.post("/api/flag/task", json={"url": A})
        c.post("/api/review/task", json={"url": A, "status": "skip"})
        rename = {"old_url": A, "new_url": A + "/moved"}
        assert c.post("/api/urls/task/rename", json=rename).json() == {"ok": True, "url": A + "/moved",
                                                                       "content_type": "web"}
        assert CacheFileSys(str(task_dir)).get_web(A + "/moved", get_screenshot=False)[0] == "page a"
        assert c.get("/api/review/task").json()["reviewed"] == {A + "/moved": "skip"}
        assert A + "/moved" in flags(tmp_path)

        assert c.delete("/api/urls/task", params={"url": new + "2"}).json() == {"ok": True}
        assert c.delete("/api/urls/task", params={"url": new + "2"}).status_code == 404
        assert set(url_states(c)) == {A + "/moved", B + "2"}


def test_edits_name_a_page_by_any_of_its_spellings(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    new = "https://example.com/new"

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        # A renamed page is listed under its stored URL, and its flag and review status follow it there
        c.post("/api/flag/task", json={"url": "http://www.example.com/a/"})
        c.post("/api/review/task", json={"url": A + "/", "status": "skip"})
        rename = {"old_url": A + "#top", "new_url": "https://example.com/moved/"}
        assert c.post("/api/urls/task/rename", json=rename).json()["url"] == "https://example.com/moved"
        assert (flags(tmp_path), url_file(tmp_path, "reviewed.json")) == (
            ["https://example.com/moved"], {"https://example.com/moved": "skip"})
        assert url_states(c) == {"https://example.com/moved": ("web", ["flagged"], "definite")}

        # One page is pending once, whatever the spelling, until a capture of any spelling stores it
        assert c.post("/api/urls/task", json={"url": new + "/"}).status_code == 200
        for spelling in (new, "http://www.example.com/new"):
            assert c.post("/api/urls/task", json={"url": spelling}).status_code == 409
        assert c.post("/api/capture", json=capture("http://www.example.com/new")).json()["url"] == new
        assert url_states(c)[new] == ("web", [], "")
        assert pending(tmp_path) == [] and url_file(tmp_path, "reviewed.json")[new] == "fixed"
        assert {i["url"] for i in c.get("/api/issues").json()["issue_index"]} == {"https://example.com/moved"}


def test_pending_urls_whose_page_another_process_stored_are_dropped_on_load(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    (task_dir / "pending.json").write_text(json.dumps(["https://example.com/new/"]))  # added in the Cache Manager
    stored = "http://www.example.com/new"  # evaluation captured it live, under the answer's spelling
    CacheFileSys(str(task_dir)).put_web(stored, "captured live by evaluation", png_bytes())

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        assert c.post("/api/reset/task", json={"url": stored}).json()["ok"]
        assert url_states(c) == {A: ("web", [], ""), stored: ("pending", ["not captured yet"], "definite")}
        assert c.delete("/api/urls/task", params={"url": stored}).json() == {"ok": True}
        assert set(url_states(c)) == {A}
        assert pending(tmp_path) == [] and c.get("/api/issues").json()["issue_index"] == []


def test_deleting_a_pending_url_deletes_its_other_spellings(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    (task_dir / "pending.json").write_text(json.dumps(["https://example.com/new", "http://www.example.com/new/"]))

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        assert c.delete("/api/urls/task", params={"url": "https://example.com/new"}).json() == {"ok": True}
        assert set(url_states(c)) == {A} and pending(tmp_path) == []


def test_pending_urls_another_manager_added_get_issue_entries_once_listed(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    c_url = "https://example.com/c"

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        (task_dir / "pending.json").write_text(json.dumps([B]))  # by the manager a load is replacing
        assert c.post("/api/urls/task", json={"url": c_url}).json()["ok"]
        assert pending(tmp_path) == [B, c_url]
        assert url_states(c) == {A: ("web", [], ""), B: ("pending", ["not captured yet"], "definite"),
                                 c_url: ("pending", ["not captured yet"], "definite")}


def test_uploads_store_only_captured_content(tmp_path):
    task_dir = tmp_path / "agent" / "task"
    CacheFileSys(str(task_dir)).put_web(A, "page a", png_bytes())
    mhtml = (b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=b\r\n\r\n--b\r\n"
             b"Content-Type: text/html\r\n\r\n<html><body><p>Saved page</p></body></html>\r\n--b--\r\n")
    empty_mhtml = mhtml.replace(b"<p>Saved page</p>", b"")

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        upload = lambda path, url, data: c.post(path, params={"url": url}, files={"file": ("f", data)})
        assert upload("/api/upload-mhtml/task", B, empty_mhtml).status_code == 422
        assert upload("/api/upload-pdf/task", B, b"<html>Log in to read this paper</html>").status_code == 422
        assert CacheFileSys(str(task_dir)).has(B) is None

        assert upload("/api/upload-mhtml/task", B, mhtml).json() == {"ok": True, "url": B}
        assert CacheFileSys(str(task_dir)).get_web(B, get_screenshot=False)[0] == "Saved page"
        assert upload("/api/upload-pdf/task", A + "/", b"%PDF-1.4 a paper").json()["url"] == A
        assert CacheFileSys(str(task_dir)).has(A) == "pdf"

        (task_dir / (CacheFileSys(str(task_dir))._path(storage_key(B), ".jpg").rsplit("/", 1)[1])).unlink()
        rename = {"old_url": B, "new_url": B + "/moved"}
        assert c.post("/api/urls/task/rename", json=rename).status_code == 500  # the screenshot is gone
        assert CacheFileSys(str(task_dir)).has(B + "/moved") is None


def test_loading_another_folder_stops_the_batch_and_clears_the_capture_target(tmp_path):
    CacheFileSys(str(tmp_path / "agent" / "task")).record_failure(A, "HTTP 503")
    CacheFileSys(str(tmp_path / "other" / "task")).record_failure(B, "HTTP 503")
    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        assert c.post("/api/capture/batch/start", json={"items": [{"task_id": "task", "url": A}]}).json()["total"] == 1
        # Opening the UI and Refresh load the loaded folder again, which keeps the batch
        c.post("/api/load", json={"path": str(tmp_path / "other" / ".." / "agent")})
        assert c.get("/api/capture/batch/status").json()["current"] == {"task_id": "task", "url": A}
        assert c.get("/api/capture/target").json()["url"] == A
        c.post("/api/load", json={"path": str(tmp_path / "other")})
        assert c.get("/api/capture/batch/status").json() == {"active": False}
        assert c.get("/api/capture/target").json() == {"active": False}


def test_edits_served_while_a_load_reads_the_folder_are_kept_and_listed(tmp_path, monkeypatch):
    task_dir = tmp_path / "agent" / "task"
    cache = CacheFileSys(str(task_dir))
    cache.put_web(A, "page a", png_bytes())
    cache.put_web(B, "page b", png_bytes())
    p, q = "https://example.com/p", "https://example.com/q"
    (task_dir / "pending.json").write_text(json.dumps([p]))

    class EditedWhileLoading(CacheManager):
        def load_agent_cache(self, agent_path):
            loaded = super().load_agent_cache(agent_path)
            routes._cm.flag_url("task", A)  # the manager being replaced still serves requests
            routes._cm.store_page("task", p, text="captured during the load", screenshot=png_bytes())
            return loaded

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        monkeypatch.setattr(routes, "CacheManager", EditedWhileLoading)
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        c.post("/api/flag/task", json={"url": B})
        c.post("/api/urls/task", json={"url": q})
        assert (flags(tmp_path), pending(tmp_path)) == ([A, B], [q])
        assert url_states(c) == {A: ("web", ["flagged"], "definite"), B: ("web", ["flagged"], "definite"),
                                 p: ("web", [], ""), q: ("pending", ["not captured yet"], "definite")}


def test_a_failed_load_keeps_the_loaded_cache(tmp_path, monkeypatch):
    CacheFileSys(str(tmp_path / "agent" / "task")).put_web(A, "page a", png_bytes())
    (tmp_path / "other").mkdir()

    class BrokenManager(CacheManager):
        def load_agent_cache(self, agent_path):
            raise OSError("disk error")

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "agent")})
        monkeypatch.setattr(routes, "CacheManager", BrokenManager)
        assert c.post("/api/load", json={"path": str(tmp_path / "other")}).status_code == 500
        assert c.get("/api/status").json()["agent_path"] == str((tmp_path / "agent").resolve())
        assert set(url_states(c)) == {A}


def test_answers_come_from_the_configured_directory(tmp_path, monkeypatch):
    CacheFileSys(str(tmp_path / "cache" / "agent" / "task")).put_web(A, "page a", png_bytes())
    answers = tmp_path / "elsewhere" / "agent" / "task"
    answers.mkdir(parents=True)
    (answers / "answer_1.md").write_text("See https://example.com/a")
    monkeypatch.setenv("CM_ANSWERS_DIR", str(tmp_path / "elsewhere"))

    with client() as c:
        c.post("/api/load", json={"path": str(tmp_path / "cache" / "agent")})
        assert c.get("/api/answers/task").json() == {"files": [{"name": "answer_1.md",
                                                                "content": "See https://example.com/a"}]}


# ------------------------------------------------------------------ requests from other web pages

def test_requests_from_other_web_pages_are_refused():
    with client() as c:
        assert c.get("/api/status").status_code == 200
        response = c.get("/api/status", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers  # other origins cannot read responses
        assert c.post("/api/capture/batch/stop", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/capture/batch/stop", headers={"Origin": "null"}).status_code == 403
        for origin in ("http://127.0.0.1:8000", "chrome-extension://abcdefgh", None):  # the page, the extension, a script
            headers = {"Origin": origin} if origin else {}
            assert c.post("/api/capture/batch/stop", headers=headers).status_code == 200

        for host in ("localhost:8000", "[::1]:8000", "LOCALHOST"):
            assert c.get("/api/status", headers={"Host": host}).status_code == 200
        assert c.get("/api/status", headers={"Host": "evil.example:8000"}).status_code == 403  # DNS rebinding


def test_no_other_site_can_frame_the_cache_manager():
    with client() as c:
        for response in (c.get("/"), c.get("/api/status"), c.get("/api/status", headers={"Host": "evil.example"})):
            assert response.headers["x-frame-options"] == "SAMEORIGIN"
            assert response.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_the_served_hosts_follow_the_bound_interface():
    assert run_script.allowed_hosts("127.0.0.1") == "127.0.0.1,localhost,::1"
    assert run_script.allowed_hosts("192.168.1.5") == "127.0.0.1,localhost,::1,192.168.1.5"
    assert run_script.allowed_hosts("0.0.0.0") == "127.0.0.1,localhost,::1,*"
    assert [_host_name(h) for h in ("127.0.0.1:8000", "[::1]:8000", "localhost", "[::1]")] == [
        "127.0.0.1", "::1", "localhost", "::1"]

    inner = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"), methods=["GET", "POST"])])
    wildcard = TestClient(LocalRequestGuard(inner, ["localhost", "*"]), base_url="http://192.168.1.5:8000")
    assert wildcard.get("/").text == "ok"
    assert wildcard.get("/", headers={"Host": "[fe80::1]:8000"}).text == "ok"
    assert wildcard.get("/", headers={"Host": "localhost:8000"}).text == "ok"
    assert wildcard.get("/", headers={"Host": "rebind.evil.example:8000"}).status_code == 403  # any IP, no names
    guarded = TestClient(LocalRequestGuard(inner, ["192.168.1.5"]), base_url="http://192.168.1.5:8000")
    assert guarded.post("/", headers={"Origin": "http://192.168.1.5:8000"}).status_code == 200
    assert guarded.post("/", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 403


# ------------------------------------------------------------------ issue severity

@pytest.fixture(scope="module")
def detector() -> KeywordDetector:
    return KeywordDetector()


def test_only_short_pages_are_definite_issues_by_their_wording(detector):
    short = detector.detect_issues("Access denied. You don't have permission to access this server.")
    assert (short.severity, short.matched_keywords) == ("definite", ["access denied"])

    article = "Cloudflare reported an outage today. " + "The rest of the article follows. " * 100
    long = detector.detect_issues(article)
    assert (long.has_issues, long.severity, long.matched_keywords) == (True, "possible", ["cloudflare"])

    assert detector.detect_issues("  \n").severity == "definite"
    both = detector.detect_issues("You have been blocked. Please solve the captcha.")
    assert both.matched_keywords == ["blocked", "you have been blocked", "captcha"]


def test_the_crawlers_refusal_wording_is_a_definite_issue(detector):
    result = detector.detect_issues("Please enable JavaScript and cookies to continue")
    assert result.severity == "definite"
    assert result.matched_patterns == ["bot check or access denied ('enable JavaScript and cookies to continue')"]
