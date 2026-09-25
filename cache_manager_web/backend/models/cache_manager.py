"""The Cache Manager's view of an agent's page caches, and the edits a reviewer makes to them.

Each task directory is a :class:`CacheFileSys`.  Evaluation reads only what
it stores: the pages (``index.json`` and their files), the URLs their
captures were redirected to (``redirects.json``), and the failure records of
automated captures (``failures.json``).  The Cache Manager keeps its own
review state next to them, which evaluation never reads:

- ``pending.json``: URLs to capture that have neither a stored page nor a
  failure record: URLs the reviewer added, and URLs whose page or failure
  record was reset.  A URL stops being pending once a page is stored for it,
  and loading a task drops the pending URLs for which another process, such
  as an evaluation run, stored a page or recorded a failure.  A pending URL
  that is only a final URL recorded for a stored page (a capture of another
  URL was redirected to it) stays in the file but is not listed, since it
  resolves to that page, and is listed again if that page is removed.
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

A manager reads ``pending.json`` and ``flags.json`` when it loads a task and
answers lookups from that copy; ``reviewed.json`` is read on every lookup.
Every change re-reads the file it changes, applies the change, and replaces
the file atomically, under a lock that all managers of the process share and
the task's cache lock (:meth:`CacheFileSys.exclusive`, an ``flock`` on the
task directory), and the manager's copy becomes what the file then holds.
Managers of one folder, in one process, such as the one serving requests and
the one a reload is building, or in several, therefore keep each other's
changes.  A review-state file that cannot be
read is never overwritten: a task with such a file is not loaded, and a
change to such a file raises :class:`ReviewStateError`.
"""

from __future__ import annotations
import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
import logging

# _write_atomic: the cache's atomic, fsynced file replacement, used for the review-state files too
from mind2web2.utils.cache_filesys import CacheFileSys, _write_atomic, page_form

logger = logging.getLogger(__name__)

PENDING_FILE = "pending.json"
FLAGS_FILE = "flags.json"
REVIEWED_FILE = "reviewed.json"
_RECORDS = {PENDING_FILE: "pending URLs", FLAGS_FILE: "flags", REVIEWED_FILE: "review statuses"}

_review_state_lock = threading.Lock()
"""Held while a review-state file is read, changed, and written back, by every manager of the process."""


class ReviewStateError(RuntimeError):
    """A task's ``pending.json``, ``flags.json``, or ``reviewed.json`` exists but cannot be read.

    The Cache Manager does not overwrite such a file, which would drop what
    it records: a task with such a file is not loaded, and a change to such a
    file fails.  Restore the file, or delete it to have the Cache Manager
    forget what it records.
    """

    def __init__(self, path: Path, reason: str):
        super().__init__(f"Cannot read {path} ({reason}); restore it, or delete it to have the Cache Manager "
                         f"forget the task's {_RECORDS.get(path.name, 'review state')}")


@dataclass
class TaskSummary:
    """Task cache summary information."""
    task_id: str
    total_urls: int  # stored pages, failed URLs, and pending URLs
    web_urls: int
    pdf_urls: int
    cache_path: str
    failed_urls: int = 0
    pending_urls: int = 0


@dataclass
class URLInfo:
    """A URL of a task and what the task holds for it."""
    url: str
    task_id: str
    content_type: str  # "web", "pdf", "failed" (the capture failed; nothing stored), or "pending" (not captured yet)
    failure: Optional[Dict[str, Any]] = None  # the failure record, for "failed" URLs


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
        self.revision = 0  # the number of changes this manager has made, see tasks_changed_since()
        self._task_revisions: Dict[str, int] = {}  # task_id -> the revision of this manager's latest change to it

    def load_agent_cache(self, agent_path: str | Path) -> Tuple[int, int]:
        """Load the task caches under ``agent_path``; returns ``(loaded tasks, task directories)``.

        A task is loaded when it has a stored page, a failure record, or a
        pending URL, and all its files can be read.  Loading a task drops
        from ``pending.json`` the pending URLs that have a stored page or a
        failure record, which another process stored or recorded.
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
                if self._load_task(task_id):
                    successful_tasks += 1
                    logger.debug(f"Loaded task {task_id} with {self.task_summaries[task_id].total_urls} URLs")
                else:
                    logger.debug(f"Skipped empty task {task_id}")

            except Exception as e:
                logger.warning(f"Failed to load task {task_id}: {e}")

        logger.info(f"Loaded {successful_tasks}/{len(task_dirs)} tasks from {self.agent_name}")
        return successful_tasks, len(task_dirs)

    def reload_task(self, task_id: str) -> bool:
        """Read a task directory of the loaded folder again; returns whether the task is loaded.

        This brings the task up to date after another manager of the folder
        changed it.  The task is read as :meth:`load_agent_cache` reads it, so
        it is dropped if it no longer has a stored page, a failure record, or
        a pending URL.  If one of its files cannot be read, the task keeps
        what was read before, and a warning is logged.
        """
        try:
            return self._load_task(task_id)
        except Exception as e:
            logger.warning(f"Failed to reload task {task_id}: {e}")
            return task_id in self.task_caches

    def _load_task(self, task_id: str) -> bool:
        """Read the directory of task ``task_id`` into this manager; returns whether the task is loaded.

        Pending URLs that have a stored page or a failure record are dropped
        from ``pending.json`` first.  Raises ``CacheIndexError`` or
        :class:`ReviewStateError`, leaving the manager as it was, if one of
        the task's files cannot be read.
        """
        task_dir = self.agent_path / task_id
        cache = CacheFileSys(str(task_dir))
        pending = _read_url_set(task_dir / PENDING_FILE)
        flags = _read_url_set(task_dir / FLAGS_FILE)
        _read_review_file(task_dir / REVIEWED_FILE, dict)
        if any(_stored_state(cache, url) is not None for url in pending):
            pending = _update_url_file(cache, PENDING_FILE, lambda urls: _still_pending(cache, urls))[1]
        loaded = self._has_content(cache) or bool(pending)
        if loaded:
            self.task_caches[task_id] = cache
            self._flags[task_id], self._pending[task_id] = flags, pending
        else:
            for per_task in (self.task_caches, self._flags, self._pending):
                per_task.pop(task_id, None)
        self._reindex_task(task_id)
        return loaded

    def tasks_changed_since(self, revision: int) -> List[str]:
        """The tasks this manager has changed since its :attr:`revision` was ``revision``, sorted.

        A change is a page or failure record this manager stored or deleted,
        or a change it made to a task's pending URLs or flags.  Changes to
        review statuses are not counted, since ``reviewed.json`` is read on
        every lookup.
        """
        return sorted(task_id for task_id, changed in self._task_revisions.items() if changed > revision)

    def _has_content(self, cache: CacheFileSys) -> bool:
        """Whether the task has any stored page or failed URL."""
        try:
            return bool(cache.get_all_urls() or cache.failures(ignore_case=False))
        except Exception:
            return False

    def _create_task_summary(self, task_id: str, cache: CacheFileSys) -> TaskSummary:
        """Create summary information for a task."""
        counts = cache.summary()
        failed = len(cache.failures(ignore_case=False))
        pending = len(self._pending_urls(task_id, cache))
        return TaskSummary(
            task_id=task_id,
            total_urls=counts["total_urls"] + failed + pending,
            web_urls=counts["web_pages"],
            pdf_urls=counts["pdf_pages"],
            cache_path=str(cache.task_dir),
            failed_urls=failed,
            pending_urls=pending,
        )

    def _pending_urls(self, task_id: str, cache: CacheFileSys) -> List[str]:
        """The task's pending URLs that have neither a stored page nor a failure record, sorted."""
        return sorted(url for url in self._pending.get(task_id, ()) if _stored_state(cache, url) is None)

    def _task_changed(self, task_id: str):
        """Record a change this manager made to a task (see :meth:`tasks_changed_since`) and reindex the task."""
        self._note_change(task_id)
        self._reindex_task(task_id)

    def _note_change(self, task_id: str):
        self.revision += 1
        self._task_revisions[task_id] = self.revision

    def _reindex_task(self, task_id: str):
        """Bring the task's summary and URL index up to date; a task that is not loaded has neither."""
        if self.task_summaries.pop(task_id, None) is not None:  # the task was indexed: drop its entries
            for url in list(self._url_index):
                infos = [info for info in self._url_index[url] if info.task_id != task_id]
                if infos:
                    self._url_index[url] = infos
                else:
                    del self._url_index[url]
        cache = self.get_task_cache(task_id)
        if cache:
            self.task_summaries[task_id] = self._create_task_summary(task_id, cache)
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
        url_infos += [URLInfo(url=url, task_id=task_id, content_type="failed", failure=record)
                      for url, record in cache.failures(ignore_case=False).items()]
        url_infos += [URLInfo(url=url, task_id=task_id, content_type="pending")
                      for url in self._pending_urls(task_id, cache)]
        return url_infos

    def canonical_url(self, task_id: str, url: str, follow_redirects: bool = True) -> str:
        """The URL under which the task lists the page that ``url`` names, or ``url`` if the task does not have it.

        That is the URL of the stored page that ``url`` refers to (see
        :meth:`CacheFileSys.lookup`), else the URL of its failure record (see
        :meth:`CacheFileSys.failure_url`), else a pending URL that names the
        same page (see :func:`page_form`).  Review state is recorded under
        this URL.  Letter case is never disregarded, as the crawler never
        disregards it: a server may serve different pages for URLs that
        differ in letter case, so a page stored under one is not the page of
        the other.  With ``follow_redirects=False``, a URL that only resolves
        to a page as the final URL of its capture (see
        :meth:`resolves_through_redirect`) is not taken for that page: its own
        failure record or pending entry, which the redirect hides from the
        listing, is found instead.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return url
        try:
            listed = (cache.lookup(url, ignore_case=False, follow_redirects=follow_redirects)
                      or cache.failure_url(url, ignore_case=False, follow_redirects=follow_redirects))
        except ValueError:  # cannot be parsed, so the cache has nothing for it
            listed = None
        if listed is not None:
            return listed
        form = page_form(url)
        if follow_redirects:
            return next((pending for pending in sorted(self._pending.get(task_id, ()))
                         if page_form(pending) == form and _stored_state(cache, pending) is None), url)
        return next((pending for pending in sorted(self._pending.get(task_id, ()))
                     if page_form(pending) == form and not _has_own_entry(cache, pending)), url)

    def resolves_through_redirect(self, task_id: str, url: str) -> bool:
        """Whether ``url`` is not listed itself but is a final URL recorded for a stored page (see
        :meth:`CacheFileSys.put_web`), so that it resolves to that page.

        Such a URL names the page for viewing, but an edit that deletes or
        changes an entry must not take it for the page: the reviewer never
        selected that page.
        """
        cache = self.get_task_cache(task_id)
        if not cache:
            return False
        try:
            return (cache.lookup(url, ignore_case=False) is not None
                    and cache.lookup(url, ignore_case=False, follow_redirects=False) is None)
        except ValueError:
            return False

    def url_state(self, task_id: str, url: str) -> Optional[str]:
        """``"web"`` or ``"pdf"`` for a stored page, ``"failed"``, ``"pending"``, or ``None`` if the task does not have ``url``."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None
        if state := _stored_state(cache, url):
            return state
        return "pending" if self.canonical_url(task_id, url) in self._pending.get(task_id, ()) else None

    def get_url_content(self, task_id: str, url: str, get_screenshot=True) -> Tuple[Optional[str], Optional[bytes]]:
        """Get content for URL (text, screenshot/pdf)."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None, None

        key = _stored_key(cache, url)
        content_type = cache.has(key) if key is not None else None
        if content_type == "web":
            try:
                text, screenshot = cache.get_web(key, get_screenshot)
                return text, screenshot
            except Exception as e:
                logger.error(f"Failed to get web content for {url}: {e}")
                return None, None
        elif content_type == "pdf":
            try:
                pdf_bytes = cache.get_pdf(key)
                return None, pdf_bytes
            except Exception as e:
                logger.error(f"Failed to get PDF content for {url}: {e}")
                return None, None

        return None, None

    def store_page(self, task_id: str, url: str, *, text: Optional[str] = None, screenshot: Optional[bytes] = None,
                   pdf_bytes: Optional[bytes] = None, final_url: Optional[str] = None) -> Optional[str]:
        """Store a page for ``url``: the PDF if ``pdf_bytes`` is given, else the text and screenshot.

        ``final_url`` is the URL the capture ended at after a redirect; it is
        recorded as the page's final URL (see :meth:`CacheFileSys.put_web`),
        so the page is stored once and the final URL refers to it, unless a
        page is stored under the final URL.  A capture named by a URL that is
        itself only a final URL recorded for another page is stored as that
        URL's own page, which takes the place of the redirect record; it never
        replaces the page captured there.

        The page is stored under the URL the task lists for ``url`` (see
        :meth:`canonical_url`): it replaces the stored page that ``url``
        refers to, whatever its content type, and otherwise takes the place
        of its failure record or pending entry, so the reviewer finds it
        where the URL was listed.  The review state of that URL moves to the
        stored page's URL, which differs when the listed spelling is not how
        the cache stores it (see :meth:`move_review_state`), and pending URLs
        that now refer to a stored page stop being pending.  Returns the
        stored page's URL, as the task lists it, or ``None`` if the task is
        unknown, no content is given, or storing failed.  Raises
        :class:`ReviewStateError`, after storing the page, if a review-state
        file cannot be read.
        """
        cache = self.get_task_cache(task_id)
        if not cache or (not pdf_bytes and (text is None or screenshot is None)):
            return None
        try:
            listed = self.canonical_url(task_id, url, follow_redirects=False)
            if pdf_bytes:
                stored = cache.put_pdf(listed, pdf_bytes, final_url)
            else:
                stored = cache.put_web(listed, text, screenshot, final_url)
        except Exception as e:
            logger.error(f"Failed to store a page for {url} in task {task_id}: {e}")
            return None
        self.move_review_state(task_id, listed, stored)
        self._update_url_set(task_id, PENDING_FILE, lambda urls: _still_pending(cache, urls))
        self._task_changed(task_id)
        logger.info(f"Stored a {'PDF' if pdf_bytes else 'web page'} for {stored} in task {task_id}")
        return stored

    def add_pending_url(self, task_id: str, url: str) -> bool:
        """Add ``url`` to a task as pending, with nothing stored; ``False`` if the task already has its page.

        Whether ``pending.json`` lists another spelling of the page (the same
        :func:`page_form`) is decided from the file as it is when the URL is
        added, so a spelling that another manager of the folder added and
        this one has not read yet is found too.
        """
        cache = self.get_task_cache(task_id)
        if cache is None or self.url_state(task_id, url) is not None:
            return False
        form = page_form(url)

        def add(urls: Set[str]) -> Set[str]:
            listed = any(page_form(pending) == form and _stored_state(cache, pending) is None for pending in urls)
            return urls if listed else urls | {url}

        before, after = self._update_url_set(task_id, PENDING_FILE, add)
        self._task_changed(task_id)
        return after != before

    def delete_url(self, task_id: str, url: str) -> bool:
        """Delete a URL from a task: its stored page, its failure record, its pending entries, and its flag.

        The pending entries deleted are every spelling of the URL's page that
        ``pending.json`` lists (the URLs with the same :func:`page_form`).
        Returns ``False`` if the task had none of these, and for a URL that
        only resolves to a page through a redirect record (see
        :meth:`resolves_through_redirect`), which is left alone.  Raises
        :class:`ReviewStateError` if a review-state file cannot be read.
        """
        cache = self.get_task_cache(task_id)
        if not cache or self.resolves_through_redirect(task_id, url):
            return False

        try:
            target = self.canonical_url(task_id, url)
            removed = cache.remove(target) if _stored_key(cache, target) is not None else None
            cleared = cache.clear_failure(target)
            flags_before, _ = self._update_url_set(task_id, FLAGS_FILE, lambda urls: urls - {target})
            pending_before, pending_after = self._update_url_set(
                task_id, PENDING_FILE, lambda urls: _other_pages(urls, target))
            if removed is None and not cleared and target not in flags_before and pending_before == pending_after:
                logger.warning(f"Cannot delete {url} from task {task_id}: the task does not have it")
                return False
            self._task_changed(task_id)
            logger.info(f"Deleted {target} from task {task_id}")
            return True
        except ReviewStateError:
            raise
        except Exception as e:
            logger.error(f"Failed to delete URL {url}: {e}")
            return False

    def reset_url(self, task_id: str, url: str) -> Optional[str]:
        """Delete what the cache holds for a URL, so that the URL is pending until captured again.

        What is deleted is the URL's stored page and its failure record; its
        flag, which was about the deleted page, is cleared.  The URL becomes
        the only pending entry of its page: other spellings of the page that
        ``pending.json`` lists (the URLs with the same :func:`page_form`)
        are dropped.  Returns ``"web"`` or ``"pdf"`` if a page was deleted,
        ``"failed"`` if only a failure record was, or ``None`` if the task had
        neither or ``url`` only resolves to a page through a redirect record
        (then nothing changes).  Raises :class:`ReviewStateError` if a
        review-state file cannot be read.  Evaluation treats a pending URL
        like any URL that is not cached: it captures the page live.
        """
        cache = self.get_task_cache(task_id)
        if not cache or self.resolves_through_redirect(task_id, url):
            return None

        try:
            target = self.canonical_url(task_id, url)
            content_type = cache.remove(target) if _stored_key(cache, target) is not None else None
            if not cache.clear_failure(target) and content_type is None:
                return None
            self._update_url_set(task_id, FLAGS_FILE, lambda urls: urls - {target})
            self._update_url_set(task_id, PENDING_FILE, lambda urls: _other_pages(urls, target) | {target})
            self._task_changed(task_id)
            logger.info(f"Reset {target} ({content_type or 'failed'}) in task {task_id}")
            return content_type or "failed"
        except ReviewStateError:
            raise
        except Exception as e:
            logger.error(f"Failed to reset URL {url}: {e}")
            return None

    # --- Reviewed status persistence ---

    def load_reviewed(self, task_id: str) -> Dict[str, str]:
        """The review status ("ok", "fixed", "skip", or "recaptured") of each reviewed URL of a task.

        Read from ``reviewed.json`` on every call.  If the file cannot be
        read, a warning is logged and the map is empty; changing a status then
        raises :class:`ReviewStateError`.
        """
        path = self._task_file(task_id, REVIEWED_FILE)
        if path is None:
            return {}
        try:
            return _read_review_file(path, dict)
        except ReviewStateError as e:
            logger.warning(str(e))
            return {}

    def mark_url_reviewed(self, task_id: str, url: str, status: str):
        """Set (or, with an empty ``status``, clear) the review status of a URL, recorded under its listed URL."""
        url = self.canonical_url(task_id, url)

        def mark(reviewed: Dict[str, str]):
            if status:
                reviewed[url] = status
            else:
                reviewed.pop(url, None)

        self._update_reviewed(task_id, mark)

    def move_review_state(self, task_id: str, old_url: str, new_url: str):
        """Move the flag and the review status recorded for ``old_url`` to ``new_url``, whose own are kept if it has them."""
        if old_url == new_url:
            return
        self._update_url_set(task_id, FLAGS_FILE,
                             lambda urls: (urls - {old_url}) | {new_url} if old_url in urls else urls)

        def move(reviewed: Dict[str, str]):
            if old_url in reviewed:
                reviewed.setdefault(new_url, reviewed.pop(old_url))

        self._update_reviewed(task_id, move)

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
        listed = self.canonical_url(task_id, url)
        self._update_url_set(task_id, FLAGS_FILE, lambda urls: urls | {listed})

    def unflag_url(self, task_id: str, url: str):
        """Remove the flag of a URL."""
        listed = self.canonical_url(task_id, url)
        self._update_url_set(task_id, FLAGS_FILE, lambda urls: urls - {listed})

    def is_flagged(self, task_id: str, url: str) -> bool:
        return self.canonical_url(task_id, url) in self._flags.get(task_id, set())

    # --- Review-state files ---

    def _task_file(self, task_id: str, name: str) -> Optional[Path]:
        """The path of a review-state file of a loaded task, or ``None`` if the task is not loaded."""
        cache = self.task_caches.get(task_id)
        return Path(cache.task_dir) / name if cache else None

    def _update_url_set(self, task_id: str, name: str,
                        update: Callable[[Set[str]], Set[str]]) -> Tuple[Set[str], Set[str]]:
        """Replace the URLs in the task's ``pending.json`` or ``flags.json`` (``name``) with ``update(urls)``.

        ``urls`` is what the file holds when the change is made (see
        :func:`_update_url_file`), and this manager's copy of the set becomes
        the result.  Returns the set before and after the change, both empty
        if the task is not loaded.  Raises :class:`ReviewStateError` if the
        file cannot be read.
        """
        cache = self.task_caches.get(task_id)
        if cache is None:
            return set(), set()
        before, after = _update_url_file(cache, name, update)
        (self._pending if name == PENDING_FILE else self._flags)[task_id] = after
        if after != before:
            self._note_change(task_id)
        return before, after

    def _update_reviewed(self, task_id: str, update: Callable[[Dict[str, str]], None]):
        """Apply ``update`` to the review statuses in the task's ``reviewed.json``, as the file holds them when the change is made.

        The file is read, changed, and written back under the lock that all
        managers of the process share and the task's cache lock, and written
        only if ``update`` changed the statuses (atomically; deleted when none
        is left).  Raises :class:`ReviewStateError` if the file cannot be read.
        """
        cache = self.task_caches.get(task_id)
        if cache is None:
            return
        path = Path(cache.task_dir) / REVIEWED_FILE
        with _review_state_lock, cache.exclusive():
            reviewed = _read_review_file(path, dict)
            before = dict(reviewed)
            update(reviewed)
            if reviewed != before:
                _write_review_file(path, reviewed)


def _update_url_file(cache: CacheFileSys, name: str,
                     update: Callable[[Set[str]], Set[str]]) -> Tuple[Set[str], Set[str]]:
    """Replace the URLs listed in the file ``name`` of ``cache``'s task with ``update(urls)``; returns the set before and after.

    The file is read, changed, and written back under the lock that all
    managers of the process share and the task's cache lock, so ``urls``
    includes every change made before, by this process or another, and it is
    written only if the set changes (atomically, as a sorted JSON list;
    deleted when the set is empty).  ``update`` may read ``cache`` but must
    not write to it (see :meth:`CacheFileSys.exclusive`).  Raises
    :class:`ReviewStateError` if the file cannot be read.
    """
    path = Path(cache.task_dir) / name
    with _review_state_lock, cache.exclusive():
        before = _read_url_set(path)
        after = update(set(before))
        if after != before:
            _write_review_file(path, sorted(after))
    return before, after


def _read_url_set(path: Path) -> Set[str]:
    """The strings in the JSON list in ``path``, or an empty set if the file does not exist.

    Raises :class:`ReviewStateError` if the file cannot be read or does not hold a JSON list.
    """
    return {url for url in _read_review_file(path, list) if isinstance(url, str)}


def _read_review_file(path: Path, kind: type) -> Any:
    """The JSON value, a list or a dict (``kind``), in the review-state file ``path``; empty if the file does not exist.

    Raises :class:`ReviewStateError` if the file cannot be read or holds another JSON value.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return kind()
    except (OSError, ValueError) as e:  # ValueError: not UTF-8, or not JSON
        raise ReviewStateError(path, str(e)) from e
    if not isinstance(data, kind):
        raise ReviewStateError(path, "not a JSON list" if kind is list else "not a JSON object")
    return data


def _write_review_file(path: Path, value: list | dict) -> None:
    """Replace the review-state file ``path`` with ``value`` as JSON, atomically, or delete it when ``value`` is empty."""
    if value:
        _write_atomic(str(path), json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))
    else:
        path.unlink(missing_ok=True)


def _still_pending(cache: CacheFileSys, urls: Set[str]) -> Set[str]:
    """The URLs of ``urls`` for which ``cache`` has neither a stored page nor a failure record.

    A redirect record does not count: a pending URL that resolves to a page
    only as the final URL of its capture is kept, so that it is listed again
    if that page is removed.
    """
    return {url for url in urls if not _has_own_entry(cache, url)}


def _has_own_entry(cache: CacheFileSys, url: str) -> bool:
    """Whether ``cache`` stores a page or a failure record for ``url`` itself, in its own letter case.

    Redirect records do not count, neither to find a page nor to hide a
    failure record; a URL that cannot be parsed has neither.
    """
    try:
        return (cache.lookup(url, ignore_case=False, follow_redirects=False) is not None
                or cache.failure_url(url, ignore_case=False, follow_redirects=False) is not None)
    except ValueError:
        return False


def _other_pages(urls: Set[str], url: str) -> Set[str]:
    """The URLs of ``urls`` that name another page than ``url`` does (another :func:`page_form`)."""
    form = page_form(url)
    return {other for other in urls if page_form(other) != form}


def _stored_key(cache: CacheFileSys, url: str) -> Optional[str]:
    """The URL of the page stored for ``url`` in its own letter case (see :meth:`CacheManager.canonical_url`), or
    ``None``, also for a URL that cannot be parsed."""
    try:
        return cache.lookup(url, ignore_case=False)
    except ValueError:
        return None


def _stored_state(cache: CacheFileSys, url: str) -> Optional[str]:
    """``"web"`` or ``"pdf"`` if a page is stored for ``url``, ``"failed"`` if its capture failed, else ``None``.

    A URL that cannot be parsed has neither, since nothing can be stored for it.
    """
    try:
        if (key := _stored_key(cache, url)) is not None:
            return cache.has(key)
        return "failed" if cache.failure(url, ignore_case=False) is not None else None
    except ValueError:
        return None
