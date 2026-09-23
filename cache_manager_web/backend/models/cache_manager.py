"""The Cache Manager's view of an agent's page caches, and the edits a reviewer makes to them.

Each task directory is a :class:`CacheFileSys`.  Evaluation reads only what
it stores: the pages (``index.json`` and their files) and the failure records
of automated captures (``failures.json``).  The Cache Manager keeps its own
review state next to them, which evaluation never reads:

- ``flags.json``: URLs that need a (re)capture.  A flagged URL with a stored
  page is a page whose content looks wrong; a flagged URL with neither a
  stored page nor a failure record is *pending*, not captured yet (a URL the
  reviewer added, or whose page was reset).  A capture or upload clears the flag.
- ``reviewed.json``: the review status of each URL.

So a flag never changes what evaluation sees; only captures, uploads,
deletions, and resets change the stored pages.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
import logging

from mind2web2.utils.cache_filesys import CacheFileSys

logger = logging.getLogger(__name__)


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
    """Enhanced cache manager with better performance and organization."""

    def __init__(self):
        self.agent_path: Optional[Path] = None
        self.agent_name: str = ""
        self.task_caches: Dict[str, CacheFileSys] = {}
        self.task_summaries: Dict[str, TaskSummary] = {}
        self._url_index: Dict[str, List[URLInfo]] = {}  # url -> [URLInfo]
        self._flags: Dict[str, Set[str]] = {}  # task_id -> set of flagged URLs
        
    def load_agent_cache(self, agent_path: str | Path) -> Tuple[int, int]:
        """Load agent cache with improved error handling and progress tracking.
        
        Returns:
            Tuple of (successful_tasks, total_tasks)
        """
        self.agent_path = Path(agent_path)
        self.agent_name = self.agent_path.name
        self.task_caches.clear()
        self.task_summaries.clear()
        self._url_index.clear()
        self._flags.clear()
        
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
                flags = self._load_flags(task_dir)

                # Only load tasks with content
                if self._has_content(cache) or flags:
                    self.task_caches[task_id] = cache
                    self._flags[task_id] = flags
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
        """Flagged URLs with neither a stored page nor a failure record, sorted."""
        return sorted(url for url in self._flags.get(task_id, ()) if _stored_state(cache, url) is None)

    def _refresh_summary(self, task_id: str):
        """Recompute a task's summary after its cache changed."""
        cache = self.get_task_cache(task_id)
        if cache:
            self.task_summaries[task_id] = self._create_task_summary(task_id, cache)
    
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

    def url_state(self, task_id: str, url: str) -> Optional[str]:
        """``"web"`` or ``"pdf"`` for a stored page, ``"failed"``, ``"pending"``, or ``None`` if the task does not have ``url``."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None
        if state := _stored_state(cache, url):
            return state
        return "pending" if url in self._flags.get(task_id, ()) else None
    
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
    
    def update_url_content(self, task_id: str, url: str, text: str, screenshot: bytes) -> bool:
        """Update web content for URL. Cleans up old PDF files if switching type."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False

        try:
            # Prefer updating the canonical stored URL if it exists
            target_url = cache.lookup(url) or url
            # Replaces a PDF's files, and clears the URL's failure record
            cache.put_web(target_url, text, screenshot)
            self._index_single_url(task_id, target_url, "web")
            self._refresh_summary(task_id)
            
            logger.info(f"Updated content for {target_url} in task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update content for {url}: {e}")
            return False
    
    def add_pending_url(self, task_id: str, url: str) -> bool:
        """Add ``url`` to a task as pending: flagged, with nothing stored; ``False`` if the task already has it."""
        if self.get_task_cache(task_id) is None or self.url_state(task_id, url) is not None:
            return False
        self.flag_url(task_id, url)
        self._index_single_url(task_id, url, "pending")
        self._refresh_summary(task_id)
        return True

    def add_url_to_task(self, task_id: str, url: str, text: str = None,
                       screenshot: bytes = None, pdf_bytes: bytes = None) -> bool:
        """Store a page for ``url`` in a task: the PDF if ``pdf_bytes`` is given, else the text and screenshot."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False
        
        try:
            if pdf_bytes:
                cache.put_pdf(url, pdf_bytes)
                content_type = "pdf"
            elif text is not None and screenshot is not None:
                cache.put_web(url, text, screenshot)
                content_type = "web"
            else:
                return False

            self._index_single_url(task_id, url, content_type)
            self._refresh_summary(task_id)
            
            logger.info(f"Added {url} to task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to add URL {url}: {e}")
            return False
    
    def _index_single_url(self, task_id: str, url: str, content_type: str):
        """Index a single URL, or update the content type it is indexed with."""
        infos = self._url_index.setdefault(url, [])
        existing = next((info for info in infos if info.task_id == task_id), None)
        if existing is not None:
            existing.content_type = content_type
            existing.failure = None
        else:
            infos.append(URLInfo(url=url, task_id=task_id, content_type=content_type))
    
    def delete_url(self, task_id: str, url: str) -> bool:
        """Delete a URL from a task: its stored page, its failure record, and its flag."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False

        try:
            flagged = self.is_flagged(task_id, url)
            removed = cache.remove(url)
            cleared = cache.clear_failure(url)
            self.unflag_url(task_id, url)
            if removed is None and not cleared and not flagged:
                logger.warning(f"Cannot delete {url} from task {task_id}: the task does not have it")
                return False

            # Update our indexes
            if url in self._url_index:
                self._url_index[url] = [
                    info for info in self._url_index[url] 
                    if info.task_id != task_id
                ]
                if not self._url_index[url]:
                    del self._url_index[url]
            
            self._refresh_summary(task_id)
            logger.info(f"Deleted {url} from task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete URL {url}: {e}")
            return False
    
    def get_all_urls(self) -> List[str]:
        """Get all unique URLs across all tasks."""
        return list(self._url_index.keys())
    
    # --- Reviewed status persistence ---

    def _reviewed_path(self, task_id: str) -> Path:
        """Return path to the reviewed.json file for a task."""
        cache = self.task_caches.get(task_id)
        if cache:
            return Path(cache.task_dir) / "reviewed.json"
        return Path()

    def load_reviewed(self, task_id: str) -> Dict[str, str]:
        """Load reviewed statuses for a task.

        Returns:
            Dict mapping url -> status ("ok", "fixed", "skip").
        """
        path = self._reviewed_path(task_id)
        if path.exists():
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
        path = self._reviewed_path(task_id)
        if not path.parent.exists():
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(reviewed_map, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save reviewed.json for {task_id}: {e}")

    def mark_url_reviewed(self, task_id: str, url: str, status: str):
        """Mark a single URL as reviewed and persist."""
        reviewed = self.load_reviewed(task_id)
        if status:
            reviewed[url] = status
        else:
            reviewed.pop(url, None)
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

    # --- Flags persistence: URLs that need a (re)capture ---

    def _flags_path(self, task_id: str) -> Path:
        cache = self.task_caches.get(task_id)
        if cache:
            return Path(cache.task_dir) / "flags.json"
        return Path()

    @staticmethod
    def _load_flags(task_dir: Path) -> Set[str]:
        path = Path(task_dir) / "flags.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return set(data)
            except Exception as e:
                logger.warning(f"Failed to load {path}: {e}")
        return set()

    def _save_flags(self, task_id: str):
        path = self._flags_path(task_id)
        if not path.parent.exists():
            return
        flags = self._flags.get(task_id, set())
        try:
            if flags:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(sorted(flags), f, indent=2, ensure_ascii=False)
            elif path.exists():
                path.unlink()
        except Exception as e:
            logger.error(f"Failed to save flags.json for {task_id}: {e}")

    def flag_url(self, task_id: str, url: str):
        """Flag a URL as needing a (re)capture (persisted in flags.json); its stored page, if any, is kept."""
        if task_id not in self._flags:
            self._flags[task_id] = set()
        self._flags[task_id].add(url)
        self._save_flags(task_id)

    def unflag_url(self, task_id: str, url: str):
        """Remove flag from a URL."""
        if task_id in self._flags:
            self._flags[task_id].discard(url)
            self._save_flags(task_id)

    def is_flagged(self, task_id: str, url: str) -> bool:
        return url in self._flags.get(task_id, set())

    # --- Content type switching with file cleanup ---

    def replace_with_pdf(self, task_id: str, url: str, pdf_bytes: bytes) -> bool:
        """Replace existing content (web or pdf) with new PDF. Cleans up old files."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False

        try:
            target_url = cache.lookup(url) or url
            # Replaces a web page's files, and clears the URL's failure record
            cache.put_pdf(target_url, pdf_bytes)
            self._index_single_url(task_id, target_url, "pdf")
            self._refresh_summary(task_id)

            # Remove flag if it was flagged
            self.unflag_url(task_id, target_url)

            logger.info(f"Replaced {target_url} with PDF in task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to replace with PDF for {url}: {e}")
            return False

    def reset_url(self, task_id: str, url: str) -> Optional[str]:
        """Delete what the cache holds for a URL and flag the URL, so that it is pending until captured again.

        What is deleted is the URL's stored page and its failure record.
        Returns ``"web"`` or ``"pdf"`` if a page was deleted, ``"failed"`` if
        only a failure record was, or ``None`` if the task had neither (then
        nothing changes).  Evaluation treats a pending URL like any URL that
        is not cached: it captures the page live.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return None

        try:
            target_url = cache.lookup(url) or url
            content_type = cache.remove(target_url)
            if not cache.clear_failure(target_url) and content_type is None:
                return None
            self.flag_url(task_id, target_url)
            self._index_single_url(task_id, target_url, "pending")
            self._refresh_summary(task_id)
            logger.info(f"Reset {target_url} ({content_type or 'failed'}) in task {task_id}")
            return content_type or "failed"
        except Exception as e:
            logger.error(f"Failed to reset URL {url}: {e}")
            return None


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
