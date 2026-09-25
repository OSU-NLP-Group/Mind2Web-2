"""Per-task store of the webpages and PDFs that answers cite.

One :class:`CacheFileSys` holds the pages cached for one task of one agent::

    <task_dir>/
    ├── index.json      # {"<storage key>": "web" | "pdf", ...}
    ├── failures.json   # {"<storage key>": {"reason", "blocked", "attempts", "time"}, ...}
    ├── redirects.json  # {"<storage key of a final URL>": "<storage key of the page captured there>", ...}
    ├── <stem>.txt      # web page: text (Markdown)
    ├── <stem>.jpg      # web page: screenshot
    └── <stem>.pdf      # PDF document

A page is stored under the :func:`storage_key` of its URL, and its files are
named by the MD5 hex digest of that key (``<stem>``).  Lookups accept other
surface forms of a stored URL; see :meth:`CacheFileSys.lookup`.
``failures.json`` records URLs whose capture failed, so that they are neither
evaluated against an error page nor silently missing; storing a page for a
URL clears its failure record, and removing the page deletes any failure
record for it too.  ``redirects.json`` records, for a page whose capture was
redirected, the URL it ended at (its final URL), so that the page is stored
once and found by either URL.

Every change is written to disk, and fsynced, before the call returns.
Each content file and each of the three JSON files is replaced atomically (a
page's text and screenshot are two files, each replaced on its own), and each
change, from writing the content files to deleting files the change replaced, happens
under a lock, with each JSON file re-read and merged before it is written.
Several processes (the crawler, an evaluation run, the Cache Manager) can
therefore write to the same task without dropping each other's entries, and a
process that is interrupted keeps every page it finished storing.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import io
import json
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Literal, Optional, Tuple
from urllib.parse import quote, quote_plus, unquote, urldefrag

from PIL import Image

from .url_tools import normalize_url_keep_case, normalize_url_simple, remove_utm_parameters

try:
    import fcntl
except ImportError:  # Windows: index updates are serialized only within one CacheFileSys instance.
    fcntl = None

ContentType = Literal["web", "pdf"]

FILE_EXTENSIONS: Dict[str, Tuple[str, ...]] = {"web": (".txt", ".jpg"), "pdf": (".pdf",)}
"""The files that make up a cached page of each content type."""

INDEX_FILE = "index.json"
FAILURES_FILE = "failures.json"
REDIRECTS_FILE = "redirects.json"

logger = logging.getLogger(__name__)


def storage_key(url: str) -> str:
    """The key a page fetched from ``url`` is stored under: fragment removed, percent-decoded, trailing slash removed."""
    url_no_frag, _ = urldefrag(url)
    decoded = unquote(url_no_frag)
    if decoded.endswith('/') and len(decoded) > 1 and not decoded.endswith('://'):
        decoded = decoded[:-1]
    return decoded


def _is_raw(key: str) -> bool:
    """Whether :func:`storage_key` would change ``key`` again.

    Such a key keeps what percent-decoding produced and a second pass would act
    on (a ``#``, or a ``%`` followed by two hex digits), or a trailing slash left
    from ``//``: the URL ``.../a%2520b`` is stored under ``.../a%20b``.
    """
    return storage_key(key) != key


def _raw_form(key: str) -> str:
    """The form by which raw keys match: UTM parameters removed, ``http`` made ``https``, and ``www.`` dropped.

    Unlike :func:`~mind2web2.utils.url_tools.normalize_url_simple`, it works
    on the string as is: no further decoding, no fragment removal, no
    lowercasing, so a raw key's ``#`` and ``%`` keep their meaning.
    """
    base, sep, query = key.partition("?")
    if sep:
        params = [p for p in query.split("&") if not p.lower().startswith("utm_")]
        base = base + ("?" + "&".join(params) if params else "")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base.replace("://www.", "://", 1)


def page_form(url: str) -> str:
    """The form by which two URLs of pages not yet stored are told to name the same page.

    It is ``url`` under :func:`~mind2web2.utils.url_tools.normalize_url_keep_case`
    (scheme, ``www.``, trailing slash, fragment, UTM parameters, and
    percent-encoding disregarded, letter case kept, since a server may serve
    different pages for URLs that differ in letter case, as :meth:`CacheFileSys.lookup`
    assumes).  A URL whose :func:`storage_key` would change again (an encoded
    ``#`` or ``%``, as in ``?q=C%23``) is instead its storage key under
    :func:`_raw_form`, the form by which the cache matches such keys, since
    normalizing it can give another page's URL (``?q=C``).  A URL that cannot
    be parsed is its own form.  The crawler groups a task's spellings by this
    form, and the Cache Manager keeps one pending entry per form.
    """
    try:
        key = storage_key(url)
        return _raw_form(key) if _is_raw(key) else normalize_url_keep_case(url)
    except ValueError:
        return url


def _address(key: str) -> str:
    """A URL whose storage key is ``key``: the key itself, or a raw key re-encoded (see :func:`_is_raw`)."""
    if not _is_raw(key):
        return key
    url = key.replace("%", "%25").replace("#", "%23")
    return url + "/" if key.endswith("/") else url


class _UrlIndex:
    """A set of storage keys that finds the key a URL refers to, by the rules of :meth:`CacheFileSys.lookup`.

    Keys keep the order they were added in, and where a rule matches several
    keys, the key added first wins.  Every rule except surface variants is a
    dictionary lookup, and surface variants are tried only when the rules
    before them miss.
    """

    def __init__(self, keys: Iterable[str] = ()):
        self._keys: Dict[str, None] = {}
        self._by_case: Dict[str, List[str]] = {}  # normalize_url_keep_case(key) -> keys
        self._by_match: Dict[str, List[str]] = {}  # normalize_url_simple(key) -> keys
        self._raw_by_form: Dict[str, List[str]] = {}  # _raw_form(raw key) -> raw keys
        for key in keys:
            self.add(key)

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> None:
        if key in self._keys:
            return
        self._keys[key] = None
        for by_form, form in self._forms(key):
            by_form.setdefault(form, []).append(key)

    def discard(self, key: str) -> None:
        if self._keys.pop(key, False) is False:
            return
        for by_form, form in self._forms(key):
            keys = by_form[form]
            keys.remove(key)
            if not keys:
                del by_form[form]

    def find(self, url: str, ignore_case: bool = True) -> Optional[str]:
        """The key ``url`` refers to, or ``None``; raises ``ValueError`` if ``url`` cannot be parsed.

        With ``ignore_case=False``, the rule that disregards letter case
        (rule 4 of :meth:`CacheFileSys.lookup`) is skipped, so that ``url``
        never finds a key that differs from it in letter case.
        """
        key = storage_key(url)
        if _is_raw(key):
            if key in self._keys:
                return key
            raw_keys = self._raw_by_form.get(_raw_form(key))
            return raw_keys[0] if raw_keys else None
        if url in self._keys and not _is_raw(url):
            return url
        found = self._find_by_form(normalize_url_keep_case, self._by_case, url)
        if found is None:
            found = next((variant for variant in _surface_variants(url)
                          if variant in self._keys and not _is_raw(variant)), None)
        if found is None and ignore_case:
            found = self._find_by_form(normalize_url_simple, self._by_match, url)
        return found

    def _find_by_form(self, normalize: Callable[[str], str], by_form: Dict[str, List[str]],
                      url: str) -> Optional[str]:
        """The key that is ``normalize(url)``, or else the first key added with that form in ``by_form``."""
        form = normalize(url)
        if form in self._keys and not _is_raw(form):
            return form
        keys = by_form.get(form)
        return keys[0] if keys else None

    def _forms(self, key: str) -> List[Tuple[Dict[str, List[str]], str]]:
        """The indexes that find ``key`` by a form of the query, each with that form of ``key``."""
        if _is_raw(key):
            return [(self._raw_by_form, _raw_form(key))]
        return [(by_form, form) for by_form, form in (
            (self._by_case, _form_or_none(normalize_url_keep_case, key)),
            (self._by_match, _form_or_none(normalize_url_simple, key)),
        ) if form is not None]


def _find_or_none(index: _UrlIndex, url: str) -> Optional[str]:
    """``index.find(url, ignore_case=False)``, or ``None`` also when ``url`` cannot be parsed."""
    try:
        return index.find(url, ignore_case=False)
    except ValueError:
        return None


def _form_or_none(normalize: Callable[[str], str], key: str) -> Optional[str]:
    try:
        return normalize(key)
    except ValueError:  # unparsable URL: reachable only by exact lookup
        return None


class _Records:
    """A snapshot of a task's failure records or redirect records by storage key, with a :class:`_UrlIndex` over their keys.

    Built once and never changed, so that a thread can read it while another
    changes the records and installs a new snapshot.
    """

    def __init__(self, records: Dict[str, Any]):
        self.records = records
        self._index = _UrlIndex(records)

    def find(self, url: str, ignore_case: bool = True) -> Optional[str]:
        """The key of the record ``url`` refers to, matched like pages (see :meth:`CacheFileSys.lookup`), or ``None``.

        Also ``None`` when ``url`` cannot be parsed.  ``ignore_case`` is as in
        :meth:`_UrlIndex.find`.
        """
        try:
            return self._index.find(url, ignore_case)
        except ValueError:
            return None


class CacheIndexError(RuntimeError):
    """A task's ``index.json``, ``failures.json``, or ``redirects.json`` exists but cannot be read.

    ``index.json`` is the only record of which URL each cached file belongs
    to, ``failures.json`` of which captures failed, and ``redirects.json`` of
    which final URLs the pages were captured at, so the cache refuses to load
    or change the task rather than start over and drop their entries.
    Restore the file, or delete it to have the cache forget what it recorded.
    """

    _FORGOTTEN = {INDEX_FILE: "cached pages", FAILURES_FILE: "failed captures",
                  REDIRECTS_FILE: "recorded redirects"}

    def __init__(self, path: str, reason: str):
        forgotten = self._FORGOTTEN.get(os.path.basename(path), "records")
        super().__init__(f"Cannot read {path} ({reason}); restore it, or delete it to have the cache "
                         f"forget the task's {forgotten}")


class CacheFileSys:
    """The cached web pages (text and screenshot) and PDFs of one task, looked up by URL.

    ``task_dir`` is created if missing.  The index is read once, at
    construction; an entry whose files are missing or whose content type is
    unknown is ignored with a warning, and an index that cannot be read raises
    :class:`CacheIndexError`.  Pages that other processes store later become
    visible to a new instance; until then, this instance also does not
    follow the redirect records pointing to them, so it treats their final
    URLs as it did before, failure records included.  The failure records are read at construction
    and again whenever this instance records or clears one, and the redirect
    records at construction and again whenever this instance stores or
    removes a page.
    """

    def __init__(self, task_dir: str):
        self.task_dir = os.path.abspath(task_dir)
        self.index_file = os.path.join(self.task_dir, INDEX_FILE)
        self.failures_file = os.path.join(self.task_dir, FAILURES_FILE)
        self.redirects_file = os.path.join(self.task_dir, REDIRECTS_FILE)
        self._types: Dict[str, ContentType] = {}  # storage key -> content type, in the order first stored
        self._pages = _UrlIndex()
        # Replaced as a whole, never changed in place: other threads may be reading it.
        self._failures = _Records({})
        self._redirects = _Records({})  # final URL's key -> page key; replaced as a whole, like _failures
        self._lock = threading.Lock()
        os.makedirs(self.task_dir, exist_ok=True)
        for key, content_type in self._read_json(self.index_file).items():
            if content_type not in FILE_EXTENSIONS:
                logger.warning("Ignoring index entry for %s: unknown content type %r", key, content_type)
            elif not all(os.path.exists(path) for path in self._paths(key, content_type)):
                logger.warning("Ignoring index entry for %s: its files are missing", key)
            else:
                self._add(key, content_type)
        self._failures = _Records(self._load_failures())
        self._redirects = _Records(self._load_redirects())

    # ------------------------------------------------------------------ lookup

    def lookup(self, url: str, ignore_case: bool = True, follow_redirects: bool = True) -> Optional[str]:
        """The URL of the cached page that ``url`` refers to, or ``None`` if its page is not cached.

        Each page is stored under a key, the :func:`storage_key` of the URL it
        was stored with.  A key that :func:`storage_key` leaves unchanged is
        found by these rules, tried in order, the first hit winning:

        1. ``url`` itself is the key.
        2. Its case-preserving normalized form
           (:func:`~mind2web2.utils.url_tools.normalize_url_keep_case`) is the
           key, or the key has the same case-preserving normalized form; among
           several such keys, the one stored first wins.
        3. One of its surface variants (:func:`_surface_variants`) is the key.
           This finds keys whose normalized form differs from the query's
           because percent-decoding changed their structure, as with an
           encoded ``#`` or ``%``.
        4. The key has the same lowercased normalized form
           (:func:`~mind2web2.utils.url_tools.normalize_url_simple`); among
           several such keys, the one stored first wins.

        Rules 1-3 respect letter case and rule 4 disregards it, so that, when
        pages are stored for URLs that differ only in letter case, which a
        server may serve as different pages, a URL finds the page stored with
        its own letter case, and a page stored with another letter case only
        when there is none.  With ``ignore_case=False``, rule 4 is skipped, so
        that ``url`` never finds a page stored under a URL that differs from it
        in letter case.

        A page whose capture ended at another URL than it was stored under (a
        redirect) is also found by that final URL (see :meth:`put_web`): when
        rules 1-3 find no page, they are applied to the final URLs recorded
        for stored pages, and a hit finds the page captured there.  Rule 4 is
        then applied to the pages, and after that to the final URLs.  A
        redirect recorded in the query's letter case therefore wins over a
        page stored under another letter case.  With
        ``follow_redirects=False``, the recorded final URLs are not used, so
        only a page stored for ``url`` itself is found; a caller about to
        delete or change what it finds uses this, so that naming a final URL
        never acts on the page captured there.

        A key that :func:`storage_key` would change again (:func:`_is_raw`) is
        found only for a ``url`` whose storage key is that key, or is raw too
        and has the same :func:`_raw_form` (UTM parameters, scheme, and
        ``www.`` disregarded; among several such keys, the one stored first
        wins).  Such a ``url`` finds no other key: the rules above would match
        it to another page, since ``.../search?q=C%23`` has the normalized form
        of ``.../search?q=C``.  For the same reason these checks come before
        the rules, so that a raw key never captures another page's URL.

        The URL returned is the key, or for a raw key a re-encoded form whose
        storage key is the key, so that passing it to any method of this class
        addresses the same page.  All rules but 3 are dictionary lookups, and
        rule 3 runs only when rules 1 and 2 miss.  Raises ``ValueError`` if
        ``url`` cannot be parsed.
        """
        key = self._find_key(url, ignore_case, follow_redirects)
        return _address(key) if key is not None else None

    def _find_key(self, url: str, ignore_case: bool = True, follow_redirects: bool = True) -> Optional[str]:
        """The key of the page ``url`` refers to, by the rules of :meth:`lookup`."""
        redirects = self._redirects
        for ignoring in (False, True) if ignore_case else (False,):
            key = self._pages.find(url, ignoring)
            if key is not None:
                return key
            if not follow_redirects:
                continue
            final = redirects.find(url, ignoring)
            if final is not None and redirects.records[final] in self._types:
                return redirects.records[final]
        return None

    def has(self, url: str) -> ContentType | None:
        """The content type cached for ``url`` ("web" or "pdf"), or ``None`` if it is not cached."""
        key = self._find_key(url)
        return self._types[key] if key is not None else None

    def get_all_urls(self) -> List[str]:
        """The URL of every cached page, as :meth:`lookup` returns it, in the order the pages were first stored."""
        return [_address(key) for key in self._types]

    def redirects(self) -> Dict[str, str]:
        """Every recorded final URL that :meth:`lookup` resolves through its record, with the URL of its page.

        Both URLs are as :meth:`lookup` returns URLs.  A record whose page
        is not stored, or whose final URL finds a stored page by the rules
        that respect letter case, is left out, since lookup does not use it.
        """
        return {_address(final): _address(key) for final, key in self._redirects.records.items()
                if key in self._types and self._stored_page(_address(final)) is None}

    def summary(self) -> Dict[str, Any]:
        types = list(self._types.values())
        return {"total_urls": len(types), "web_pages": types.count("web"), "pdf_pages": types.count("pdf"),
                "failed_urls": len(self.failures())}

    # ------------------------------------------------------------------ failures

    def failure(self, url: str, ignore_case: bool = True) -> Optional[Dict[str, Any]]:
        """The failure recorded for ``url``, or ``None``.

        Matched like pages (see :meth:`lookup`, also for ``ignore_case``).  A record has ``reason`` (text), ``blocked`` (the site
        refused an automated browser, so a person may still capture it),
        ``attempts``, and ``time`` (ISO 8601, UTC, of the latest attempt).  A
        record is ignored while a page is stored for its URL (found by the
        same rules, so with ``ignore_case=False`` only a page in the URL's own
        letter case hides it).  Storing a page
        deletes its URL's record, so this happens when a process that has not
        seen a page another process stored records a failure for its URL.
        """
        failures = self._failures
        key = failures.find(url, ignore_case)
        if key is None or self._is_stored(_address(key), ignore_case):
            return None
        return dict(failures.records[key])

    def failure_url(self, url: str, ignore_case: bool = True, follow_redirects: bool = True) -> Optional[str]:
        """The URL of the record :meth:`failure` returns for ``url`` (with the same ``ignore_case``), as
        :meth:`failures` lists it, or ``None``.

        With ``follow_redirects=False``, a record is hidden only by a page
        stored for its own URL, not by a redirect record that resolves its URL
        to a page (see :meth:`lookup`), so the record of a final URL is found.
        """
        key = self._failures.find(url, ignore_case)
        if key is None or self._is_stored(_address(key), ignore_case, follow_redirects):
            return None
        return _address(key)

    def failures(self, ignore_case: bool = True) -> Dict[str, Dict[str, Any]]:
        """Every failure record that :meth:`failure` returns (with the same ``ignore_case``), by its URL as
        :meth:`lookup` returns URLs."""
        return {_address(key): dict(record) for key, record in self._failures.records.items()
                if not self._is_stored(_address(key), ignore_case)}

    def record_failure(self, url: str, reason: str, *, blocked: bool = False) -> str:
        """Record that capturing ``url`` failed; returns the URL of the record, as :meth:`failures` lists it.

        A record already matching ``url`` by a rule that respects letter case
        is updated and its ``attempts`` count incremented; a URL that differs
        from every record in letter case gets its own record, since a server
        may serve it as another page.
        """
        with self._index_lock():
            failures = self._load_failures()
            # A URL that differs in letter case may be another page, so it gets its own record
            key = _Records(failures).find(url, ignore_case=False) or storage_key(url)
            attempts = failures.get(key, {}).get("attempts", 0) + 1
            record = {"reason": reason, "blocked": blocked, "attempts": attempts,
                      "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._update_json(self.failures_file, key, record)
            failures[key] = record
            self._failures = _Records(failures)
        return _address(key)

    def clear_failure(self, url: str) -> bool:
        """Delete the failure record matching ``url`` by a rule that respects letter case; returns whether there was one."""
        with self._index_lock():
            return self._clear_failure(url)

    # ------------------------------------------------------------------ read

    def get_web(self, url: str, get_screenshot: bool = True) -> Tuple[str, Optional[bytes]]:
        """The cached text and JPEG screenshot of a web page (screenshot ``None`` if not requested).

        Raises ``KeyError`` if no web page is cached for ``url``, and
        ``OSError`` if its files cannot be read, for example because another
        process removed the page after this instance read the index.
        """
        key = self._find_key(url)
        if key is None or self._types[key] != "web":
            raise KeyError(f"No web content found for URL: {url}")
        with open(self._path(key, ".txt"), 'r', encoding='utf-8') as f:
            text = f.read()
        screenshot = None
        if get_screenshot:
            with open(self._path(key, ".jpg"), 'rb') as f:
                screenshot = f.read()
        return text, screenshot

    def get_pdf(self, url: str) -> bytes:
        """The cached PDF bytes; raises ``KeyError`` if no PDF is cached for ``url``, ``OSError`` as :meth:`get_web`."""
        key = self._find_key(url)
        if key is None or self._types[key] != "pdf":
            raise KeyError(f"No PDF content found for URL: {url}")
        with open(self._path(key, ".pdf"), 'rb') as f:
            return f.read()

    # ------------------------------------------------------------------ write

    def put_web(self, url: str, text: str, screenshot: str | bytes, final_url: Optional[str] = None) -> str:
        """Store a web page's text and screenshot; returns its URL, as :meth:`lookup` returns it.

        The page is stored under ``storage_key(url)``, so passing a URL that
        :meth:`lookup` or :meth:`get_all_urls` returned replaces that page.  A
        page already stored under the key is replaced, whatever its content
        type.  ``screenshot`` is image bytes or a base64 string (optionally a
        ``data:image/...`` URL) and is saved as JPEG.

        ``final_url`` is the URL the content was served at, after redirects.
        When it is an http(s) URL of another page than ``url`` (another
        :func:`page_form`) and no page is stored for it (as :meth:`lookup`
        finds pages with ``ignore_case=False, follow_redirects=False``), it is
        recorded as this page's final URL, and :meth:`lookup` finds this page
        for it too, so a redirected capture is stored once.  A record that
        another page holds for the same final URL, in any spelling that
        respects letter case, is replaced: the latest capture that ended at a
        URL answers for it.  The final URL's failure record, if any, is kept:
        :meth:`failure` ignores it while the final URL resolves to this page,
        and it applies again once the page is removed.

        Storing a page also deletes the redirect records that pointed to the
        page stored under the same key before (they described an earlier
        capture), and the records whose final URL finds this page by the rules
        that respect letter case (the page now answers for it).
        """
        return self._put(url, "web", {".txt": text.encode("utf-8"), ".jpg": _to_jpeg(screenshot)}, final_url)

    def put_pdf(self, url: str, pdf_bytes: bytes, final_url: Optional[str] = None) -> str:
        """Store a PDF, keyed, replacing, and recording ``final_url`` like :meth:`put_web`; returns its URL, as
        :meth:`lookup` returns it."""
        return self._put(url, "pdf", {".pdf": pdf_bytes}, final_url)

    def remove(self, url: str) -> ContentType | None:
        """Delete the cached page ``url`` refers to; returns its content type, or ``None`` if nothing was cached.

        The page is found as :meth:`lookup` finds it.  The content type
        returned, and the files deleted, are those that ``index.json`` records
        at the time of the call, because another process may have replaced the
        page with one of the other type after this instance read the index.
        If another process has removed the page, no files are deleted and
        ``None`` is returned.

        Failure records that storing this page would have deleted (those
        whose URL finds the page itself by a rule that respects letter case),
        and that :meth:`failure` ignores while the page is stored, are deleted
        with the page, so that they do not reappear.  A failure record hidden
        only because its URL is a final URL recorded for the page is kept,
        and applies again.  Other failure records,
        including one for a URL that differs from the page's in letter case
        only and that a server may serve as another page, are kept.  The
        redirect records pointing to the page are deleted with it.
        """
        with self._index_lock():
            key = self._find_key(url)
            if key is None:
                return None
            failures = self._load_failures()
            redirects = self._load_redirects()  # an unreadable file raises before anything is deleted
            hidden = [failure_key for failure_key in failures
                      if self._stored_page(_address(failure_key)) == key]
            on_disk = self._update_json(self.index_file, key, None)
            content_type = on_disk if on_disk in FILE_EXTENSIONS else None
            self._discard(key)
            if content_type is not None:
                self._delete_files(key, content_type)
            for failure_key in hidden:
                self._update_json(self.failures_file, failure_key, None)
                del failures[failure_key]
            self._failures = _Records(failures)
            self._set_redirects(redirects, key)
        return content_type

    def _put(self, url: str, content_type: ContentType, files: Dict[str, bytes],
             final_url: Optional[str] = None) -> str:
        key = storage_key(url)
        final_key = _redirect_key(url, final_url)
        with self._index_lock():
            # An unreadable JSON file raises before any file is written
            index = self._read_json(self.index_file)
            self._read_json(self.failures_file)
            redirects = self._load_redirects()
            for ext, data in files.items():
                _write_atomic(self._path(key, ext), data)
            previous = self._update_json(self.index_file, key, content_type) or self._types.get(key)
            self._add(key, content_type)
            self._clear_failure(url)
            if final_key is not None and (_find_or_none(_UrlIndex(index), final_url) is not None
                                          or self._stored_page(final_url) is not None):
                final_key = None  # a page is stored for the final URL, and lookup finds it
            self._set_redirects(redirects, key, final_key)
            if previous in FILE_EXTENSIONS and previous != content_type:
                self._delete_files(key, previous)
        return _address(key)

    # ------------------------------------------------------------------ internals

    def _path(self, key: str, ext: str) -> str:
        return os.path.join(self.task_dir, hashlib.md5(key.encode('utf-8')).hexdigest() + ext)

    def _paths(self, key: str, content_type: str) -> List[str]:
        return [self._path(key, ext) for ext in FILE_EXTENSIONS[content_type]]

    def _delete_files(self, key: str, content_type: str) -> None:
        for path in self._paths(key, content_type):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def _add(self, key: str, content_type: ContentType) -> None:
        self._types[key] = content_type
        self._pages.add(key)

    def _discard(self, key: str) -> None:
        self._types.pop(key, None)
        self._pages.discard(key)

    def _clear_failure(self, url: str) -> bool:
        """Delete the failure record matching ``url``, whichever process wrote it.

        Must be called under :meth:`_index_lock`.
        """
        failures = self._load_failures()
        key = _Records(failures).find(url, ignore_case=False)
        if key is not None:
            self._update_json(self.failures_file, key, None)
            del failures[key]
        self._failures = _Records(failures)
        return key is not None

    def _load_failures(self) -> Dict[str, Dict[str, Any]]:
        return {key: record for key, record in self._read_json(self.failures_file).items()
                if isinstance(record, dict)}

    def _load_redirects(self) -> Dict[str, str]:
        return {final: key for final, key in self._read_json(self.redirects_file).items() if isinstance(key, str)}

    def _set_redirects(self, redirects: Dict[str, str], key: str, final_key: Optional[str] = None) -> None:
        """Install ``redirects`` without the records of page ``key``, plus ``final_key`` -> ``key``.

        The records of page ``key`` are those pointing to it and those whose
        final URL finds it by the rules that respect letter case.  Adding
        ``final_key`` also deletes the records of other spellings of it (their
        final URL finds ``final_key`` by those rules), so one final URL has
        one record.  ``redirects`` is the file's content, read under
        :meth:`_index_lock`, which must still be held.  The file is rewritten
        only when the records change.
        """
        page = _UrlIndex([key])
        kept = {final: target for final, target in redirects.items()
                if target != key and _find_or_none(page, _address(final)) is None}
        if final_key is not None:
            same_final = _UrlIndex([final_key])
            kept = {final: target for final, target in kept.items()
                    if _find_or_none(same_final, _address(final)) is None}
            kept[final_key] = key
        if kept != redirects:
            _write_atomic(self.redirects_file, _json_bytes(kept))
        self._redirects = _Records(kept)

    def _stored_key(self, url: str, ignore_case: bool = True, follow_redirects: bool = True) -> Optional[str]:
        """The key of the page ``url`` refers to, or ``None``, also when ``url`` cannot be parsed.

        ``ignore_case`` and ``follow_redirects`` are as in :meth:`lookup`.
        """
        try:
            return self._find_key(url, ignore_case, follow_redirects)
        except ValueError:
            return None

    def _stored_page(self, url: str) -> Optional[str]:
        """The key of the page stored for ``url`` by the rules that respect letter case, redirects left out, or ``None``."""
        return _find_or_none(self._pages, url)

    def _is_stored(self, url: str, ignore_case: bool = True, follow_redirects: bool = True) -> bool:
        return self._stored_key(url, ignore_case, follow_redirects) is not None

    @staticmethod
    def _read_json(path: str) -> Dict[str, Any]:
        """The JSON object in ``path``, or ``{}`` if the file does not exist; raises :class:`CacheIndexError` if unreadable."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:  # ValueError: not UTF-8, or not JSON
            raise CacheIndexError(path, str(e)) from e
        if not isinstance(data, dict):
            raise CacheIndexError(path, "not a JSON object")
        return data

    def _update_json(self, path: str, key: str, value: Any) -> Any:
        """Set (or, with ``value=None``, delete) one key of a JSON object file, keeping other writers' keys.

        Returns the key's previous value on disk.  The file is not rewritten
        when deleting a key it does not have.  Must be called under
        :meth:`_index_lock`, so that the read and the write see no other writer
        in between.
        """
        data = self._read_json(path)
        previous = data.get(key)
        if value is None:
            if key not in data:
                return None
            del data[key]
        else:
            data[key] = value  # a replaced entry keeps its position
        _write_atomic(path, _json_bytes(data))
        return previous

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the lock under which this class changes the task, for other files kept in the task directory.

        It is the lock of :meth:`_index_lock`: a thread lock of this instance
        and, on POSIX, an ``flock`` on the task directory, so a program that
        reads, changes, and writes back a file of its own in the task
        directory under it excludes every other process doing the same.  This
        instance's methods that write take the same lock, which is not
        reentrant, so they must not be called while it is held; methods that
        only read may be.
        """
        with self._index_lock():
            yield

    @contextmanager
    def _index_lock(self) -> Iterator[None]:
        """Serialize changes to the task across threads and, on POSIX, across processes.

        The cross-process lock is an advisory ``flock`` on the task directory,
        so no lock file is left in it.
        """
        with self._lock:
            if fcntl is None:
                yield
                return
            fd = os.open(self.task_dir, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing the descriptor releases the lock


def _redirect_key(url: str, final_url: Optional[str]) -> Optional[str]:
    """The storage key under which to record ``final_url`` as the final URL of the page stored for ``url``, or ``None``.

    ``None`` when ``final_url`` is missing, is not an http(s) URL (the
    browser's ``about:blank`` or an error page), or names the same page as
    ``url`` (the same :func:`page_form`), which :meth:`CacheFileSys.lookup`
    finds without a record.
    """
    if not final_url or not final_url.lower().startswith(("http://", "https://")):
        return None
    if page_form(final_url) == page_form(url):
        return None
    return storage_key(final_url)


def _json_bytes(data: Dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')


def _write_atomic(path: str, data: bytes) -> None:
    """Replace ``path`` with ``data``, fsyncing the file and its directory before returning.

    A concurrent reader sees either the old or the complete new content, and
    once this returns the new content survives a crash of the process or of
    the operating system.
    """
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, 'xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(directory)


def _fsync_directory(directory: str) -> None:
    """Make the renames in ``directory`` durable; a no-op where directories cannot be opened (Windows)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # some file systems do not support fsync on directories
        pass
    finally:
        os.close(fd)


def _to_jpeg(image_data: str | bytes, quality: int = 85) -> bytes:
    """Re-encode a screenshot (bytes or base64) as JPEG, flattening transparency onto white.

    Returns the decoded input unchanged if it cannot be read as an image.
    """
    if isinstance(image_data, str):
        if image_data.startswith('data:image/'):
            image_data = image_data.split(',', 1)[1]
        image_data = base64.b64decode(image_data)
    try:
        image = Image.open(io.BytesIO(image_data))
        if image.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode == 'P':
                image = image.convert('RGBA')
            background.paste(image, mask=image.split()[-1])
            image = background
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=quality, optimize=True)
        return buffer.getvalue()
    except Exception as e:
        logger.warning("Error converting image to JPG: %s", e)
        return image_data


_UTM_SUFFIXES = ("?utm_source=chatgpt.com", "?utm_source=openai.com")
# ``safe`` characters of the percent-encoding forms a URL may have been written in.
_QUOTE_SAFE_SETS = ("/", ":/?#", ":/?#@!$&'*+,;=", ":/?#[]@!$&'*+,;=", ":/?#[]@!$&'()*+,;=", ":/")


@functools.lru_cache(maxsize=4096)
def _surface_variants(url: str) -> Tuple[str, ...]:
    """Other ways ``url`` may have been written, for exact comparison with stored URLs.

    Combines: with or without the fragment and UTM parameters; with a
    ``utm_source=chatgpt.com`` / ``openai.com`` suffix; without ``www.``;
    with either scheme; in several percent-encoded forms and decoded; with the
    trailing slash toggled.
    """
    url_no_frag, _ = urldefrag(url)
    bases = [url, url_no_frag, remove_utm_parameters(url), remove_utm_parameters(url_no_frag)]
    for u in (url, url_no_frag):
        for suffix in _UTM_SUFFIXES:
            bases.append(u + suffix)
            if not u.endswith('/'):
                bases.append(u + '/' + suffix)
    for prefix in ("http://www.", "https://www."):
        if url.startswith(prefix):
            bases.append(prefix[:-len("www.")] + url[len(prefix):])
    bases += [swapped for swapped in map(_swap_scheme, bases) if swapped]

    variants: List[str] = []
    for base in dict.fromkeys(bases):
        try:
            forms = [base, *(quote(base, safe=safe) for safe in _QUOTE_SAFE_SETS),
                     quote_plus(base, safe=_QUOTE_SAFE_SETS[4]), unquote(base)]
        except (TypeError, UnicodeError):
            forms = [base]
        for form in forms:
            variants.append(form)
            toggled = _toggle_trailing_slash(form)
            if toggled is not None:
                variants.append(toggled)
    return tuple(dict.fromkeys(variants))


def _swap_scheme(url: str) -> Optional[str]:
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return None


def _toggle_trailing_slash(url: str) -> Optional[str]:
    if url.endswith('/'):
        return url[:-1] if len(url) > 1 and not url.endswith('://') else None
    return url + '/'
