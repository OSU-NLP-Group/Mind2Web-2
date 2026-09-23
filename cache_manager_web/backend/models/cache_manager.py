"""The Cache Manager's view of an agent's page caches, and the edits a reviewer makes to them.

Each task directory is a :class:`CacheFileSys`.  Evaluation reads only what
it stores: the pages (``index.json`` and their files) and the failure records
of automated captures (``failures.json``).  The Cache Manager keeps its own
review state next to them, which evaluation never reads:

- ``pending.json``: URLs to capture that have neither a stored page nor a
  failure record: URLs the reviewer added, and URLs whose page or failure
  record was reset.  A URL stops being pending once a page is stored for it.
- ``flags.json``: URLs whose stored page looks wrong and needs a recapture.
  A capture or upload clears the flag.  A flag on a URL that has neither a
  stored page nor a failure record has no effect.
- ``reviewed.json``: the review status of each URL.

Each URL in these files is recorded as the task lists it (see
:meth:`CacheManager.canonical_url`), so an edit that names a page by another
spelling, such as the URL a capture was redirected to or a URL typed with a
trailing slash, updates the same entry.  Flags and review statuses never
change what evaluation sees; only captures, uploads, deletions, and resets
change the stored pages.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
import logging

from mind2web2.utils.cache_filesys import CacheFileSys, storage_key
from mind2web2.utils.url_tools import normalize_url_simple

logger = logging.getLogger(__name__)

PENDING_FILE = "pending.json"
FLAGS_FILE = "flags.json"
REVIEWED_FILE = "reviewed.json"


@dataclass
class TaskSummary:
    """Task cache summary information."""
    task_id: str
    total_urls: int  # stored pages, failed URLs, and pending URLs
    web_urls: int
    pdf_urls: int
    issue_urls: int
    cache_path: str
    failed_urls: int = 0
    pending_urls: int = 0


@dataclass
class URLInfo:
    """URL information with metadata."""
    url: str
    task_id: str
    content_type: str  # "web", "pdf", "failed" (the capture failed; nothing stored), or "pending" (not captured yet)
    has_issues: bool = False
    issues: List[str] = None
    failure: Optional[Dict[str, Any]] = None  # the failure record, for "failed" URLs

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class CacheManager:
    """An agent's page caches, one per task, with the Cache Manager's review state for each task."""

    def __init__(self):
        self.agent_path: Optional[Path] = None
        self.agent_name: str = ""
        self.task_caches: Dict[str, CacheFileSys] = {}
        self.task_summaries: Dict[str, TaskSummary] = {}
        self._url_index: Dict[str, List[URLInfo]] = {}  # url -> [URLInfo]
        self._flags: Dict[str, Set[str]] = {}  # task_id -> flagged URLs
        self._pending: Dict[str, Set[str]] = {}  # task_id -> pending URLs

    def load_agent_cache(self, agent_path: str | Path) -> Tuple[int, int]:
        """Load the task caches under ``agent_path``; returns ``(loaded tasks, task directories)``.

        A task is loaded when it has a stored page, a failure record, or a
        pending URL.
        """
        self.agent_path = Path(agent_path)
        self.agent_name = self.agent_path.name
        self.task_caches.clear()
        self.task_summaries.clear()
        self._url_index.clear()
        self._flags.clear()
        self._pending.clear()

        if not self.agent_path.exists():
            raise FileNotFoundError(f"Agent path not found: {agent_path}")

        if not self.agent_path.is_dir():
            raise ValueError(f"Path is not a directory: {agent_path}")

        task_dirs = [d for d in self.agent_path.iterdir() if d.is_dir()]
        successful_tasks = 0

        for task_dir in task_dirs:
            task_id = task_dir.name
            try:
                cache = CacheFileSys(str(task_dir))
                pending = _load_url_set(task_dir / PENDING_FILE)
                if self._has_content(cache) or pending:
                    self.task_caches[task_id] = cache
                    self._flags[task_id] = _load_url_set(task_dir / FLAGS_FILE)
                    self._pending[task_id] = pending
                    summary = self._create_task_summary(task_id, cache)
                    self.task_summaries[task_id] = summary
                    self._index_task_urls(task_id, cache)
                    successful_tasks += 1
                    logger.debug(f"Loaded task {task_id} with {summary.total_urls} URLs")
                else:
                    logger.debug(f"Skipped empty task {task_id}")

            except Exception as e:
                logger.warning(f"Failed to load task {task_id}: {e}")

        logger.info(f"Loaded {successful_tasks}/{len(task_dirs)} tasks from {self.agent_name}")
        return successful_tasks, len(task_dirs)

    def _has_content(self, cache: CacheFileSys) -> bool:
        """Whether the task has any stored page or failed URL."""
        try:
            return bool(cache.get_all_urls() or cache.failures())
        except Exception:
            return False

    def _create_task_summary(self, task_id: str, cache: CacheFileSys) -> TaskSummary:
        """Create summary information for a task."""
        counts = cache.summary()
        pending = len(self._pending_urls(task_id, cache))
        return TaskSummary(
            task_id=task_id,
            total_urls=counts["total_urls"] + counts["failed_urls"] + pending,
            web_urls=counts["web_pages"],
            pdf_urls=counts["pdf_pages"],
            issue_urls=0,  # Will be calculated by keyword detector
            cache_path=str(cache.task_dir),
            failed_urls=counts["failed_urls"],
            pending_urls=pending,
        )

    def _pending_urls(self, task_id: str, cache: CacheFileSys) -> List[str]:
        """The task's pending URLs that have neither a stored page nor a failure record, sorted."""
        return sorted(url for url in self._pending.get(task_id, ()) if _stored_state(cache, url) is None)

    def _task_changed(self, task_id: str):
        """Bring the task's summary and URL index up to date after its cache or review state changed."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return
        self.task_summaries[task_id] = self._create_task_summary(task_id, cache)
        for url in list(self._url_index):
            infos = [info for info in self._url_index[url] if info.task_id != task_id]
            if infos:
                self._url_index[url] = infos
            else:
                del self._url_index[url]
        self._index_task_urls(task_id, cache)

    def _index_task_urls(self, task_id: str, cache: CacheFileSys):
        """Index all URLs in a task for efficient lookup."""
        for url_info in self.get_task_urls(task_id, cache):
            self._url_index.setdefault(url_info.url, []).append(url_info)

    def get_task_ids(self) -> List[str]:
        """Get sorted list of task IDs."""
        return sorted(self.task_caches.keys())

    def get_task_cache(self, task_id: str) -> Optional[CacheFileSys]:
        """Get cache for specific task."""
        return self.task_caches.get(task_id)

    def get_task_summary(self, task_id: str) -> Optional[TaskSummary]:
        """Get summary for specific task."""
        return self.task_summaries.get(task_id)

    def get_task_urls(self, task_id: str, cache: Optional[CacheFileSys] = None) -> List[URLInfo]:
        """Every URL of a task: its stored pages, then the URLs whose capture failed, then pending URLs."""
        cache = cache or self.get_task_cache(task_id)
        if not cache:
            return []
        url_infos = [URLInfo(url=url, task_id=task_id, content_type=cache.has(url))
                     for url in cache.get_all_urls()]
        url_infos += [URLInfo(url=url, task_id=task_id, content_type="failed", has_issues=True,
                              issues=[f"capture failed: {record.get('reason', 'unknown reason')}"],
                              failure=record)
                      for url, record in cache.failures().items()]
        url_infos += [URLInfo(url=url, task_id=task_id, content_type="pending", has_issues=True,
                              issues=["not captured yet"])
                      for url in self._pending_urls(task_id, cache)]
        return url_infos

    def canonical_url(self, task_id: str, url: str) -> str:
        """The URL under which the task lists the page that ``url`` names, or ``url`` if the task does not have it.

        That is the URL of the stored page that ``url`` refers to (see
        :meth:`CacheFileSys.lookup`), else the URL of its failure record (see
        :meth:`CacheFileSys.failure_url`), else a pending URL that names the
        same page (see :func:`_page_form`).  Review state is recorded under
        this URL.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return url
        try:
            listed = cache.lookup(url) or cache.failure_url(url)
        except ValueError:  # cannot be parsed, so the cache has nothing for it
            listed = None
        if listed is not None:
            return listed
        form = _page_form(url)
        return next((pending for pending in sorted(self._pending.get(task_id, ()))
                     if _page_form(pending) == form and _stored_state(cache, pending) is None), url)

    def url_state(self, task_id: str, url: str) -> Optional[str]:
        """``"web"`` or ``"pdf"`` for a stored page, ``"failed"``, ``"pending"``, or ``None`` if the task does not have ``url``."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None
        if state := _stored_state(cache, url):
            return state
        return "pending" if self.canonical_url(task_id, url) in self._pending.get(task_id, ()) else None

    def find_url_across_tasks(self, url: str) -> List[URLInfo]:
        """Find URL across all tasks."""
        return self._url_index.get(url, [])

    def get_url_content(self, task_id: str, url: str, get_screenshot=True) -> Tuple[Optional[str], Optional[bytes]]:
        """Get content for URL (text, screenshot/pdf)."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None, None

        content_type = cache.has(url)
        if content_type == "web":
            try:
                text, screenshot = cache.get_web(url, get_screenshot)
                return text, screenshot
            except Exception as e:
                logger.error(f"Failed to get web content for {url}: {e}")
                return None, None
        elif content_type == "pdf":
            try:
                pdf_bytes = cache.get_pdf(url)
                return None, pdf_bytes
            except Exception as e:
                logger.error(f"Failed to get PDF content for {url}: {e}")
                return None, None

        return None, None

    def store_page(self, task_id: str, url: str, *, text: Optional[str] = None, screenshot: Optional[bytes] = None,
                   pdf_bytes: Optional[bytes] = None) -> Optional[str]:
        """Store a page for ``url``: the PDF if ``pdf_bytes`` is given, else the text and screenshot.

        The page is stored under the URL the task lists for ``url`` (see
        :meth:`canonical_url`): it replaces the stored page that ``url``
        refers to, whatever its content type, and otherwise takes the place
        of its failure record or pending entry, so the reviewer finds it
        where the URL was listed.  The review state of that URL moves to the
        stored page's URL, which differs when the listed spelling is not how
        the cache stores it (see :meth:`move_review_state`), and pending URLs
        that now refer to a stored page stop being pending.  Returns the
        stored page's URL, as the task lists it, or ``None`` if the task is
        unknown, no content is given, or storing failed.
        """
        cache = self.get_task_cache(task_id)
        if not cache or (not pdf_bytes and (text is None or screenshot is None)):
            return None
        try:
            listed = self.canonical_url(task_id, url)
            if pdf_bytes:
                stored = cache.put_pdf(listed, pdf_bytes)
            else:
                stored = cache.put_web(listed, text, screenshot)
        except Exception as e:
            logger.error(f"Failed to store a page for {url} in task {task_id}: {e}")
            return None
        self.move_review_state(task_id, listed, stored)
        pending = self._pending.get(task_id, set())
        resolved = {p for p in pending if _stored_state(cache, p) is not None}
        if resolved:
            pending -= resolved
            self._save_url_set(task_id, PENDING_FILE, pending)
        self._task_changed(task_id)
        logger.info(f"Stored a {'PDF' if pdf_bytes else 'web page'} for {stored} in task {task_id}")
        return stored

    def update_url_content(self, task_id: str, url: str, text: str, screenshot: bytes) -> bool:
        """Store a web page for ``url`` with :meth:`store_page`; returns whether it was stored."""
        return self.store_page(task_id, url, text=text, screenshot=screenshot) is not None

    def replace_with_pdf(self, task_id: str, url: str, pdf_bytes: bytes) -> bool:
        """Store a PDF for ``url`` with :meth:`store_page` and clear its flag; returns whether it was stored."""
        stored = self.store_page(task_id, url, pdf_bytes=pdf_bytes)
        if stored is not None:
            self.unflag_url(task_id, stored)
        return stored is not None

    def add_pending_url(self, task_id: str, url: str) -> bool:
        """Add ``url`` to a task as pending, with nothing stored; ``False`` if the task already has its page."""
        if self.get_task_cache(task_id) is None or self.url_state(task_id, url) is not None:
            return False
        self._pending.setdefault(task_id, set()).add(url)
        self._save_url_set(task_id, PENDING_FILE, self._pending[task_id])
        self._task_changed(task_id)
        return True

    def delete_url(self, task_id: str, url: str) -> bool:
        """Delete a URL from a task: its stored page, its failure record, its pending entry, and its flag.

        Returns ``False`` if the task had none of them.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return False

        try:
            target = self.canonical_url(task_id, url)
            removed = cache.remove(target)
            cleared = cache.clear_failure(target)
            flagged = self._discard(task_id, FLAGS_FILE, self._flags, target)
            pending = self._discard(task_id, PENDING_FILE, self._pending, target)
            if removed is None and not cleared and not flagged and not pending:
                logger.warning(f"Cannot delete {url} from task {task_id}: the task does not have it")
                return False
            self._task_changed(task_id)
            logger.info(f"Deleted {target} from task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete URL {url}: {e}")
            return False

    def reset_url(self, task_id: str, url: str) -> Optional[str]:
        """Delete what the cache holds for a URL, so that the URL is pending until captured again.

        What is deleted is the URL's stored page and its failure record; its
        flag, which was about the deleted page, is cleared.  Returns ``"web"``
        or ``"pdf"`` if a page was deleted, ``"failed"`` if only a failure
        record was, or ``None`` if the task had neither (then nothing
        changes).  Evaluation treats a pending URL like any URL that is not
        cached: it captures the page live.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return None

        try:
            target = self.canonical_url(task_id, url)
            content_type = cache.remove(target)
            if not cache.clear_failure(target) and content_type is None:
                return None
            self._discard(task_id, FLAGS_FILE, self._flags, target)
            self._pending.setdefault(task_id, set()).add(target)
            self._save_url_set(task_id, PENDING_FILE, self._pending[task_id])
            self._task_changed(task_id)
            logger.info(f"Reset {target} ({content_type or 'failed'}) in task {task_id}")
            return content_type or "failed"
        except Exception as e:
            logger.error(f"Failed to reset URL {url}: {e}")
            return None

    def get_all_urls(self) -> List[str]:
        """Get all unique URLs across all tasks."""
        return list(self._url_index.keys())

    # --- Reviewed status persistence ---

    def load_reviewed(self, task_id: str) -> Dict[str, str]:
        """The review status ("ok", "fixed", "skip", or "recaptured") of each reviewed URL of a task."""
        path = self._task_file(task_id, REVIEWED_FILE)
        if path and path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.warning(f"Failed to load reviewed.json for {task_id}: {e}")
        return {}

    def save_reviewed(self, task_id: str, reviewed_map: Dict[str, str]):
        """Save reviewed statuses for a task."""
        path = self._task_file(task_id, REVIEWED_FILE)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(reviewed_map, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save reviewed.json for {task_id}: {e}")

    def mark_url_reviewed(self, task_id: str, url: str, status: str):
        """Set (or, with an empty ``status``, clear) the review status of a URL, recorded under its listed URL."""
        url = self.canonical_url(task_id, url)
        reviewed = self.load_reviewed(task_id)
        if status:
            reviewed[url] = status
        elif url not in reviewed:
            return
        else:
            del reviewed[url]
        self.save_reviewed(task_id, reviewed)

    def move_review_state(self, task_id: str, old_url: str, new_url: str):
        """Move the flag and the review status recorded for ``old_url`` to ``new_url``, whose own are kept if it has them."""
        if old_url == new_url:
            return
        if self._discard(task_id, FLAGS_FILE, self._flags, old_url):
            self._flags.setdefault(task_id, set()).add(new_url)
            self._save_url_set(task_id, FLAGS_FILE, self._flags[task_id])
        reviewed = self.load_reviewed(task_id)
        if old_url in reviewed:
            status = reviewed.pop(old_url)
            reviewed.setdefault(new_url, status)
            self.save_reviewed(task_id, reviewed)

    def get_statistics(self) -> Dict[str, int]:
        """Get overall statistics."""
        total_urls = len(self._url_index)
        total_tasks = len(self.task_caches)
        total_web = sum(1 for infos in self._url_index.values()
                       for info in infos if info.content_type == "web")
        total_pdf = sum(1 for infos in self._url_index.values()
                       for info in infos if info.content_type == "pdf")
        total_failed = sum(1 for infos in self._url_index.values()
                           for info in infos if info.content_type == "failed")
        total_pending = sum(1 for infos in self._url_index.values()
                            for info in infos if info.content_type == "pending")

        return {
            "total_tasks": total_tasks,
            "total_urls": total_urls,
            "web_urls": total_web,
            "pdf_urls": total_pdf,
            "failed_urls": total_failed,
            "pending_urls": total_pending,
        }

    # --- Flags: stored pages that need a recapture ---

    def flag_url(self, task_id: str, url: str):
        """Flag a URL's stored page as needing a recapture (persisted in flags.json); the page is kept."""
        self._flags.setdefault(task_id, set()).add(self.canonical_url(task_id, url))
        self._save_url_set(task_id, FLAGS_FILE, self._flags[task_id])

    def unflag_url(self, task_id: str, url: str):
        """Remove the flag of a URL."""
        self._discard(task_id, FLAGS_FILE, self._flags, self.canonical_url(task_id, url))

    def is_flagged(self, task_id: str, url: str) -> bool:
        return self.canonical_url(task_id, url) in self._flags.get(task_id, set())

    # --- Review-state files ---

    def _task_file(self, task_id: str, name: str) -> Optional[Path]:
        """The path of a review-state file of a loaded task, or ``None`` if the task is not loaded."""
        cache = self.task_caches.get(task_id)
        return Path(cache.task_dir) / name if cache else None

    def _discard(self, task_id: str, name: str, sets: Dict[str, Set[str]], url: str) -> bool:
        """Remove ``url`` from a task's set in ``sets``, saved as file ``name``; returns whether it was there."""
        urls = sets.get(task_id)
        if not urls or url not in urls:
            return False
        urls.discard(url)
        self._save_url_set(task_id, name, urls)
        return True

    def _save_url_set(self, task_id: str, name: str, urls: Set[str]):
        """Write ``urls`` to the task's file ``name`` as a sorted JSON list, or delete the file when empty."""
        path = self._task_file(task_id, name)
        if not path:
            return
        try:
            if urls:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(sorted(urls), f, indent=2, ensure_ascii=False)
            elif path.exists():
                path.unlink()
        except Exception as e:
            logger.error(f"Failed to save {name} for {task_id}: {e}")


def _load_url_set(path: Path) -> Set[str]:
    """The URLs listed in a JSON file, or an empty set if it is missing or not a JSON list."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {url for url in data if isinstance(url, str)}
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
    return set()


def _page_form(url: str) -> str:
    """The form by which the Cache Manager tells that two URLs not yet stored name the same page.

    It is the URL's normalized form (:func:`normalize_url_simple`), which the
    cache also matches stored pages by, except for a URL whose storage key
    :func:`storage_key` would change again (a percent-decoded ``#`` or
    ``%XX``): such a URL is matched only by its storage key, because
    normalizing it can turn it into another page's URL, as with
    ``.../search?q=C%23`` and ``.../search?q=C``.  A URL that cannot be parsed
    is its own form.
    """
    try:
        key = storage_key(url)
        return key if storage_key(key) != key else normalize_url_simple(url)
    except ValueError:
        return url


def _stored_state(cache: CacheFileSys, url: str) -> Optional[str]:
    """``"web"`` or ``"pdf"`` if a page is stored for ``url``, ``"failed"`` if its capture failed, else ``None``.

    A URL that cannot be parsed has neither, since nothing can be stored for it.
    """
    try:
        if content_type := cache.has(url):
            return content_type
        return "failed" if cache.failure(url) is not None else None
    except ValueError:
        return None
