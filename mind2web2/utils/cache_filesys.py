"""Per-task store of the webpages and PDFs that answers cite.

One :class:`CacheFileSys` holds the pages cached for one task of one agent::

    <task_dir>/
    ├── index.json      # {"<storage key>": "web" | "pdf", ...}
    ├── failures.json   # {"<storage key>": {"reason", "blocked", "attempts", "time"}, ...}
    ├── <stem>.txt      # web page: text (Markdown)
    ├── <stem>.jpg      # web page: screenshot
    └── <stem>.pdf      # PDF document

A page is stored under the :func:`storage_key` of its URL, and its files are
named by the MD5 hex digest of that key (``<stem>``).  Lookups accept other
surface forms of a stored URL; see :meth:`CacheFileSys.lookup`.
``failures.json`` records URLs whose capture failed, so that they are neither
evaluated against an error page nor silently missing; storing a page for a
URL clears its failure record, and removing the page deletes any failure
record for it too.

Every change is written to disk, and fsynced, before the call returns.
Content files and the two JSON files are replaced atomically, and each change,
from writing the content files to deleting files the change replaced, happens
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
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple
from urllib.parse import quote, quote_plus, unquote, urldefrag

from PIL import Image

from .url_tools import normalize_url_simple, remove_utm_parameters

try:
    import fcntl
except ImportError:  # Windows: index updates are serialized within one process only.
    fcntl = None

ContentType = Literal["web", "pdf"]

FILE_EXTENSIONS: Dict[str, Tuple[str, ...]] = {"web": (".txt", ".jpg"), "pdf": (".pdf",)}
"""The files that make up a cached page of each content type."""

INDEX_FILE = "index.json"
FAILURES_FILE = "failures.json"

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


def _address(key: str) -> str:
    """A URL whose storage key is ``key``: the key itself, or a raw key re-encoded (see :func:`_is_raw`)."""
    if not _is_raw(key):
        return key
    url = key.replace("%", "%25").replace("#", "%23")
    return url + "/" if key.endswith("/") else url


class CacheIndexError(RuntimeError):
    """A task's ``index.json`` or ``failures.json`` exists but cannot be read.

    ``index.json`` is the only record of which URL each cached file belongs
    to, and ``failures.json`` of which captures failed, so the cache refuses to
    load or change the task rather than start over and drop their entries.
    Restore the file, or delete it to have the cache forget what it recorded.
    """

    def __init__(self, path: str, reason: str):
        forgotten = "cached pages" if os.path.basename(path) == INDEX_FILE else "failed captures"
        super().__init__(f"Cannot read {path} ({reason}); restore it, or delete it to have the cache "
                         f"forget the task's {forgotten}")


class CacheFileSys:
    """The cached web pages (text and screenshot) and PDFs of one task, looked up by URL.

    ``task_dir`` is created if missing.  The index is read once, at
    construction; an entry whose files are missing or whose content type is
    unknown is ignored with a warning, and an index that cannot be read raises
    :class:`CacheIndexError`.  Pages that other processes store later become
    visible to a new instance.  The failure records are read at construction
    and again whenever this instance records or clears one.
    """

    def __init__(self, task_dir: str):
        self.task_dir = os.path.abspath(task_dir)
        self.index_file = os.path.join(self.task_dir, INDEX_FILE)
        self.failures_file = os.path.join(self.task_dir, FAILURES_FILE)
        self._types: Dict[str, ContentType] = {}
        self._keys_by_match: Dict[str, List[str]] = {}  # normalize_url_simple(key) -> keys, oldest first
        self._raw_keys_by_form: Dict[str, List[str]] = {}  # _raw_form(raw key) -> raw keys, oldest first
        # Replaced as a whole, never changed in place: other threads may be iterating it.
        self._failures: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        os.makedirs(self.task_dir, exist_ok=True)
        for key, content_type in self._read_json(self.index_file).items():
            if content_type not in FILE_EXTENSIONS:
                logger.warning("Ignoring index entry for %s: unknown content type %r", key, content_type)
            elif not all(os.path.exists(path) for path in self._paths(key, content_type)):
                logger.warning("Ignoring index entry for %s: its files are missing", key)
            else:
                self._add(key, content_type)
        self._failures = self._load_failures()

    # ------------------------------------------------------------------ lookup

    def lookup(self, url: str) -> Optional[str]:
        """The URL of the cached page that ``url`` refers to, or ``None`` if its page is not cached.

        Each page is stored under a key, the :func:`storage_key` of the URL it
        was stored with.  A key that :func:`storage_key` leaves unchanged is
        found by these rules, tried in order, the first hit winning:

        1. ``url`` itself is the key.
        2. Its normalized form (:func:`~mind2web2.utils.url_tools.normalize_url_simple`)
           is the key.
        3. The key has the same normalized form; among several, the one stored
           first wins.
        4. One of its surface variants (:func:`_surface_variants`) is the key.
           This finds keys whose normalized form differs from the query's
           because percent-decoding changed their structure, as with an
           encoded ``#`` or ``%``.

        A key that :func:`storage_key` would change again (:func:`_is_raw`) is
        found only for a ``url`` whose storage key is that key, or is raw too
        and has the same :func:`_raw_form` (UTM parameters, scheme, and
        ``www.`` disregarded; among several such keys, the one stored first
        wins).  These checks come before the rules above, which would let such
        a key capture other pages: the key of ``.../search?q=C%23`` has the
        normalized form of ``.../search?q=C``.

        The URL returned is the key, or for a raw key a re-encoded form whose
        storage key is the key, so that passing it to any method of this class
        addresses the same page.  All rules but 4 are dictionary lookups, and
        rule 4 runs only when they miss.  Raises ``ValueError`` if ``url``
        cannot be parsed.
        """
        key = self._find_key(url)
        return _address(key) if key is not None else None

    def _find_key(self, url: str) -> Optional[str]:
        """The key of the page ``url`` refers to, by the rules of :meth:`lookup`."""
        key = storage_key(url)
        if _is_raw(key):
            if key in self._types:
                return key
            raw_keys = self._raw_keys_by_form.get(_raw_form(key))
            if raw_keys:
                return raw_keys[0]
        if url in self._types and not _is_raw(url):
            return url
        match = normalize_url_simple(url)
        if match in self._types and not _is_raw(match):
            return match
        keys = self._keys_by_match.get(match)
        if keys:
            return keys[0]
        return next((variant for variant in _surface_variants(url)
                     if variant in self._types and not _is_raw(variant)), None)

    def has(self, url: str) -> ContentType | None:
        """The content type cached for ``url`` ("web" or "pdf"), or ``None`` if it is not cached."""
        key = self._find_key(url)
        return self._types[key] if key is not None else None

    def has_web(self, url: str) -> bool:
        return self.has(url) == "web"

    def has_pdf(self, url: str) -> bool:
        return self.has(url) == "pdf"

    def get_all_urls(self) -> List[str]:
        """The URL of every cached page, as :meth:`lookup` returns it, in the order the pages were first stored."""
        return [_address(key) for key in self._types]

    def summary(self) -> Dict[str, Any]:
        types = list(self._types.values())
        return {"total_urls": len(types), "web_pages": types.count("web"), "pdf_pages": types.count("pdf"),
                "failed_urls": len(self.failures())}

    # ------------------------------------------------------------------ failures

    def failure(self, url: str) -> Optional[Dict[str, Any]]:
        """The failure recorded for ``url``, or ``None``.

        Matched like pages (see :meth:`lookup`), by rules 1-3 and the rule for
        raw keys.  A record has ``reason`` (text), ``blocked`` (the site
        refused an automated browser, so a person may still capture it),
        ``attempts``, and ``time`` (ISO 8601, UTC, of the latest attempt).  A
        record is ignored while a page is stored for its URL.  Storing a page
        deletes its URL's record, so this happens when a process that has not
        seen a page another process stored records a failure for its URL.
        """
        failures = self._failures
        key = self._failure_key(url, failures)
        if key is None or self._is_stored(_address(key)):
            return None
        return dict(failures[key])

    def failure_url(self, url: str) -> Optional[str]:
        """The URL of the record :meth:`failure` returns for ``url``, as :meth:`failures` lists it, or ``None``."""
        key = self._failure_key(url, self._failures)
        if key is None or self._is_stored(_address(key)):
            return None
        return _address(key)

    def failures(self) -> Dict[str, Dict[str, Any]]:
        """Every failure record that :meth:`failure` returns, by its URL as :meth:`lookup` returns URLs."""
        return {_address(key): dict(record) for key, record in self._failures.items()
                if not self._is_stored(_address(key))}

    def record_failure(self, url: str, reason: str, *, blocked: bool = False) -> str:
        """Record that capturing ``url`` failed; returns the URL of the record, as :meth:`failures` lists it.

        A record already matching ``url`` is updated and its ``attempts``
        count incremented.
        """
        with self._index_lock():
            failures = self._load_failures()
            key = self._failure_key(url, failures) or storage_key(url)
            attempts = failures.get(key, {}).get("attempts", 0) + 1
            record = {"reason": reason, "blocked": blocked, "attempts": attempts,
                      "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._update_json(self.failures_file, key, record)
            failures[key] = record
            self._failures = failures
        return _address(key)

    def clear_failure(self, url: str) -> bool:
        """Delete the failure record matching ``url``; returns whether there was one."""
        with self._index_lock():
            return self._clear_failure(url)

    # ------------------------------------------------------------------ read

    def get_web(self, url: str, get_screenshot: bool = True) -> Tuple[str, Optional[bytes]]:
        """The cached text and JPEG screenshot of a web page (screenshot ``None`` if not requested).

        Raises ``KeyError`` if no web page is cached for ``url``.
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
        """The cached PDF bytes; raises ``KeyError`` if no PDF is cached for ``url``."""
        key = self._find_key(url)
        if key is None or self._types[key] != "pdf":
            raise KeyError(f"No PDF content found for URL: {url}")
        with open(self._path(key, ".pdf"), 'rb') as f:
            return f.read()

    # ------------------------------------------------------------------ write

    def put_web(self, url: str, text: str, screenshot: str | bytes) -> str:
        """Store a web page's text and screenshot; returns its URL, as :meth:`lookup` returns it.

        The page is stored under ``storage_key(url)``, so passing a URL that
        :meth:`lookup` or :meth:`get_all_urls` returned replaces that page.  A
        page already stored under the key is replaced, whatever its content
        type.  ``screenshot`` is image bytes or a base64 string (optionally a
        ``data:image/...`` URL) and is saved as JPEG.
        """
        return self._put(url, "web", {".txt": text.encode("utf-8"), ".jpg": _to_jpeg(screenshot)})

    def put_pdf(self, url: str, pdf_bytes: bytes) -> str:
        """Store a PDF, keyed and replacing like :meth:`put_web`; returns its URL, as :meth:`lookup` returns it."""
        return self._put(url, "pdf", {".pdf": pdf_bytes})

    def remove(self, url: str) -> ContentType | None:
        """Delete the cached page ``url`` refers to; returns its content type, or ``None`` if nothing was cached.

        The page is found as :meth:`lookup` finds it.  The content type
        returned, and the files deleted, are those that ``index.json`` records
        at the time of the call, because another process may have replaced the
        page with one of the other type after this instance read the index.
        If another process has removed the page, no files are deleted and
        ``None`` is returned.

        Failure records that :meth:`failure` ignores because this page is
        stored for their URL are deleted with the page, so that they do not
        reappear.  Other failure records are left to :meth:`clear_failure`.
        """
        with self._index_lock():
            key = self._find_key(url)
            if key is None:
                return None
            failures = self._load_failures()
            hidden = [failure_key for failure_key in failures
                      if self._stored_key(_address(failure_key)) == key]
            on_disk = self._update_json(self.index_file, key, None)
            content_type = on_disk if on_disk in FILE_EXTENSIONS else None
            self._discard(key)
            if content_type is not None:
                self._delete_files(key, content_type)
            for failure_key in hidden:
                self._update_json(self.failures_file, failure_key, None)
                del failures[failure_key]
            self._failures = failures
        return content_type

    def _put(self, url: str, content_type: ContentType, files: Dict[str, bytes]) -> str:
        key = storage_key(url)
        with self._index_lock():
            for path in (self.index_file, self.failures_file):
                self._read_json(path)  # an unreadable JSON file raises before any file is written
            for ext, data in files.items():
                _write_atomic(self._path(key, ext), data)
            previous = self._update_json(self.index_file, key, content_type) or self._types.get(key)
            self._add(key, content_type)
            self._clear_failure(url)
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
        if key not in self._types:
            by_form, form = self._form_index(key)
            if form is not None:
                by_form.setdefault(form, []).append(key)
        self._types[key] = content_type

    def _discard(self, key: str) -> None:
        if self._types.pop(key, None) is None:
            return
        by_form, form = self._form_index(key)
        keys = by_form.get(form, [])
        if key in keys:
            keys.remove(key)
            if not keys:
                del by_form[form]

    def _form_index(self, key: str) -> Tuple[Dict[str, List[str]], Optional[str]]:
        """The index that finds ``key`` by a form of the query (see :meth:`lookup`), and that form of ``key``."""
        if _is_raw(key):
            return self._raw_keys_by_form, _raw_form(key)
        return self._keys_by_match, _match_key(key)

    @staticmethod
    def _failure_key(url: str, failures: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """The key of the record in ``failures`` that ``url`` refers to, matched like :meth:`_find_key` without rule 4."""
        key = storage_key(url)
        if key in failures:
            return key
        if _is_raw(key):
            raw_form = _raw_form(key)
            found = next((k for k in failures if _is_raw(k) and _raw_form(k) == raw_form), None)
            if found is not None:
                return found
        if url in failures and not _is_raw(url):
            return url
        match = _match_key(url)
        if match is None:
            return None
        return next((k for k in failures if not _is_raw(k) and _match_key(k) == match), None)

    def _clear_failure(self, url: str) -> bool:
        """Delete the failure record matching ``url``, whichever process wrote it.

        Must be called under :meth:`_index_lock`.
        """
        failures = self._load_failures()
        key = self._failure_key(url, failures)
        if key is not None:
            self._update_json(self.failures_file, key, None)
            del failures[key]
        self._failures = failures
        return key is not None

    def _load_failures(self) -> Dict[str, Dict[str, Any]]:
        return {key: record for key, record in self._read_json(self.failures_file).items()
                if isinstance(record, dict)}

    def _stored_key(self, url: str) -> Optional[str]:
        """The key of the page ``url`` refers to, or ``None``, also when ``url`` cannot be parsed."""
        try:
            return self._find_key(url)
        except ValueError:
            return None

    def _is_stored(self, url: str) -> bool:
        return self._stored_key(url) is not None

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
        _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'))
        return previous

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


def _match_key(key: str) -> Optional[str]:
    try:
        return normalize_url_simple(key)
    except ValueError:  # unparsable URL: reachable only by exact lookup
        return None


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
