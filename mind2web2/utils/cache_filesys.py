"""Per-task store of the webpages and PDFs that answers cite.

One :class:`CacheFileSys` holds the pages cached for one task of one agent::

    <task_dir>/
    ├── index.json      # {"<storage key>": "web" | "pdf", ...}
    ├── <stem>.txt      # web page: text (Markdown)
    ├── <stem>.jpg      # web page: screenshot
    └── <stem>.pdf      # PDF document

A page is stored under the :func:`storage_key` of its URL, and its files are
named by the MD5 hex digest of that key (``<stem>``).  Lookups accept other
surface forms of a stored URL; see :meth:`CacheFileSys.lookup`.

Every change is written to disk, and fsynced, before the call returns.
Each content file and ``index.json`` is replaced atomically (a page's text and
screenshot are two files, each replaced on its own), and each change,
from writing the content files to deleting files the change replaced, happens
under a lock, with ``index.json`` re-read and merged before it is written.
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


class _UrlIndex:
    """A set of storage keys that finds the key a URL refers to, by the rules of :meth:`CacheFileSys.lookup`.

    Keys keep the order they were added in, and where a rule matches several
    keys, the key added first wins.  Every rule except the last is a
    dictionary lookup, and the last (surface variants) runs only when the
    others miss.
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

    def find(self, url: str) -> Optional[str]:
        """The key ``url`` refers to, or ``None``; raises ``ValueError`` if ``url`` cannot be parsed."""
        key = storage_key(url)
        if _is_raw(key):
            if key in self._keys:
                return key
            raw_keys = self._raw_by_form.get(_raw_form(key))
            return raw_keys[0] if raw_keys else None
        if url in self._keys and not _is_raw(url):
            return url
        for normalize, by_form in ((normalize_url_keep_case, self._by_case),
                                   (normalize_url_simple, self._by_match)):
            form = normalize(url)
            if form in self._keys and not _is_raw(form):
                return form
            keys = by_form.get(form)
            if keys:
                return keys[0]
        return next((variant for variant in _surface_variants(url)
                     if variant in self._keys and not _is_raw(variant)), None)

    def _forms(self, key: str) -> List[Tuple[Dict[str, List[str]], str]]:
        """The indexes that find ``key`` by a form of the query, each with that form of ``key``."""
        if _is_raw(key):
            return [(self._raw_by_form, _raw_form(key))]
        return [(by_form, form) for by_form, form in (
            (self._by_case, _form_or_none(normalize_url_keep_case, key)),
            (self._by_match, _form_or_none(normalize_url_simple, key)),
        ) if form is not None]


def _form_or_none(normalize: Callable[[str], str], key: str) -> Optional[str]:
    try:
        return normalize(key)
    except ValueError:  # unparsable URL: reachable only by exact lookup
        return None


class CacheIndexError(RuntimeError):
    """A task's ``index.json`` exists but cannot be read.

    The index is the only record of which URL each cached file belongs to, so
    the cache refuses to load or change the task rather than start over and
    drop its entries.  Restore the file, or delete it to start the task's cache
    over.
    """

    def __init__(self, path: str, reason: str):
        super().__init__(f"Cannot read the cache index {path} ({reason}); restore it, "
                         f"or delete it to start this task's cache over")


class CacheFileSys:
    """The cached web pages (text and screenshot) and PDFs of one task, looked up by URL.

    ``task_dir`` is created if missing.  The index is read once, at
    construction; an entry whose files are missing or whose content type is
    unknown is ignored with a warning, and an index that cannot be read raises
    :class:`CacheIndexError`.  Pages that other processes store later become
    visible to a new instance.
    """

    def __init__(self, task_dir: str):
        self.task_dir = os.path.abspath(task_dir)
        self.index_file = os.path.join(self.task_dir, "index.json")
        self._types: Dict[str, ContentType] = {}  # storage key -> content type, in the order first stored
        self._pages = _UrlIndex()
        self._lock = threading.Lock()
        os.makedirs(self.task_dir, exist_ok=True)
        for key, content_type in self._read_index().items():
            if content_type not in FILE_EXTENSIONS:
                logger.warning("Ignoring index entry for %s: unknown content type %r", key, content_type)
            elif not all(os.path.exists(path) for path in self._paths(key, content_type)):
                logger.warning("Ignoring index entry for %s: its files are missing", key)
            else:
                self._add(key, content_type)

    # ------------------------------------------------------------------ lookup

    def lookup(self, url: str) -> Optional[str]:
        """The URL of the cached page that ``url`` refers to, or ``None`` if its page is not cached.

        Each page is stored under a key, the :func:`storage_key` of the URL it
        was stored with.  A key that :func:`storage_key` leaves unchanged is
        found by these rules, tried in order, the first hit winning:

        1. ``url`` itself is the key.
        2. Its case-preserving normalized form
           (:func:`~mind2web2.utils.url_tools.normalize_url_keep_case`) is the
           key, or the key has the same case-preserving normalized form; among
           several such keys, the one stored first wins.
        3. The same with the lowercased normalized form
           (:func:`~mind2web2.utils.url_tools.normalize_url_simple`).
        4. One of its surface variants (:func:`_surface_variants`) is the key.
           This finds keys whose normalized form differs from the query's
           because percent-decoding changed their structure, as with an
           encoded ``#`` or ``%``.

        Rule 2 comes before rule 3 so that, when pages are stored for URLs
        that differ only in letter case, which a server may serve as different
        pages, a URL finds the page stored with its own letter case, and a
        page stored with another letter case only when there is none.

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
        addresses the same page.  All rules but 4 are dictionary lookups, and
        rule 4 runs only when they miss.  Raises ``ValueError`` if ``url``
        cannot be parsed.
        """
        key = self._find_key(url)
        return _address(key) if key is not None else None

    def _find_key(self, url: str) -> Optional[str]:
        """The key of the page ``url`` refers to, by the rules of :meth:`lookup`."""
        return self._pages.find(url)

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
        return {"total_urls": len(types), "web_pages": types.count("web"), "pdf_pages": types.count("pdf")}

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
        """
        with self._index_lock():
            key = self._find_key(url)
            if key is None:
                return None
            on_disk = self._commit(self._read_index(), key, None)
            content_type = on_disk if on_disk in FILE_EXTENSIONS else None
            self._discard(key)
            if content_type is not None:
                self._delete_files(key, content_type)
        return content_type

    def _put(self, url: str, content_type: ContentType, files: Dict[str, bytes]) -> str:
        key = storage_key(url)
        with self._index_lock():
            index = self._read_index()  # an unreadable index raises before any file is written
            for ext, data in files.items():
                _write_atomic(self._path(key, ext), data)
            previous = self._commit(index, key, content_type) or self._types.get(key)
            self._add(key, content_type)
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

    def _read_index(self) -> Dict[str, str]:
        """The entries of ``index.json``, or none if it does not exist; raises :class:`CacheIndexError` if unreadable."""
        try:
            with open(self.index_file, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:  # ValueError: not UTF-8, or not JSON
            raise CacheIndexError(self.index_file, str(e)) from e
        if not isinstance(index, dict):
            raise CacheIndexError(self.index_file, "not a JSON object")
        return index

    def _commit(self, index: Dict[str, str], key: str, content_type: ContentType | None) -> Optional[str]:
        """Apply one entry change to ``index``, as just read from disk, and write it to ``index.json``.

        Reading the index under the same :meth:`_index_lock` as the write keeps
        the entries other writers committed.  ``content_type=None`` removes the
        entry.  Returns the entry's previous content type on disk.
        """
        previous = index.get(key)
        if content_type is None:
            index.pop(key, None)
        else:
            index[key] = content_type  # a replaced entry keeps its position
        _write_atomic(self.index_file, json.dumps(index, indent=2, ensure_ascii=False).encode('utf-8'))
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
