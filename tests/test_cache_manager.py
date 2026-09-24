"""Cache Manager model: page edits keep the task's index, files, and counts consistent."""
from __future__ import annotations

import io
import json

from PIL import Image

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
