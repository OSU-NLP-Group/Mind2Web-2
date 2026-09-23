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

Every change is on disk when the call returns.  Content files and
``index.json`` are replaced atomically, and ``index.json`` is re-read and
merged under a lock before each write.  Several processes (the crawler, an
evaluation run, the Cache Manager) can therefore write to the same task
without dropping each other's entries, and a process that is interrupted
keeps every page it finished storing.
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

logger = logging.getLogger(__name__)


def storage_key(url: str) -> str:
    """The key a page fetched from ``url`` is stored under: fragment removed, percent-decoded, trailing slash removed."""
    url_no_frag, _ = urldefrag(url)
    decoded = unquote(url_no_frag)
    if decoded.endswith('/') and len(decoded) > 1 and not decoded.endswith('://'):
        decoded = decoded[:-1]
    return decoded


class CacheFileSys:
    """The cached web pages (text and screenshot) and PDFs of one task, looked up by URL.

    ``task_dir`` is created if missing.  The index is read once, at
    construction; an entry whose files are missing or whose content type is
    unknown is ignored with a warning.  Pages that other processes store later
    become visible to a new instance.
    """

    def __init__(self, task_dir: str):
        self.task_dir = os.path.abspath(task_dir)
        self.index_file = os.path.join(self.task_dir, "index.json")
        self._types: Dict[str, ContentType] = {}
        self._keys_by_match: Dict[str, List[str]] = {}  # normalize_url_simple(key) -> keys, oldest first
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
        """The stored URL that ``url`` refers to, or ``None`` if its page is not cached.

        The rules are tried in order and the first hit wins:

        1. ``url`` itself is stored.
        2. Its normalized form (:func:`~mind2web2.utils.url_tools.normalize_url_simple`)
           is stored.
        3. A stored URL has the same normalized form; the one stored first wins.
        4. One of its surface variants (:func:`_surface_variants`) is stored.
           This finds stored URLs whose normalized form differs from the
           query's because percent-decoding changed their structure, as with
           an encoded ``#`` or ``%``.

        Rules 1-3 are dictionary lookups; rule 4 runs only when they miss.
        Raises ``ValueError`` if ``url`` cannot be parsed.
        """
        if url in self._types:
            return url
        match = normalize_url_simple(url)
        if match in self._types:
            return match
        keys = self._keys_by_match.get(match)
        if keys:
            return keys[0]
        return next((variant for variant in _surface_variants(url) if variant in self._types), None)

    def has(self, url: str) -> ContentType | None:
        """The content type cached for ``url`` ("web" or "pdf"), or ``None`` if it is not cached."""
        key = self.lookup(url)
        return self._types[key] if key is not None else None

    def has_web(self, url: str) -> bool:
        return self.has(url) == "web"

    def has_pdf(self, url: str) -> bool:
        return self.has(url) == "pdf"

    def get_all_urls(self) -> List[str]:
        """Every stored URL, in the order it was first stored."""
        return list(self._types)

    def summary(self) -> Dict[str, Any]:
        types = list(self._types.values())
        return {"total_urls": len(types), "web_pages": types.count("web"), "pdf_pages": types.count("pdf")}

    # ------------------------------------------------------------------ read

    def get_web(self, url: str, get_screenshot: bool = True) -> Tuple[str, Optional[bytes]]:
        """The cached text and JPEG screenshot of a web page (screenshot ``None`` if not requested).

        Raises ``KeyError`` if no web page is cached for ``url``.
        """
        key = self.lookup(url)
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
        key = self.lookup(url)
        if key is None or self._types[key] != "pdf":
            raise KeyError(f"No PDF content found for URL: {url}")
        with open(self._path(key, ".pdf"), 'rb') as f:
            return f.read()

    # ------------------------------------------------------------------ write

    def put_web(self, url: str, text: str, screenshot: str | bytes) -> str:
        """Store a web page's text and screenshot; returns the key it is stored under.

        The key is ``url`` itself if that is already a stored URL (so passing
        the result of :meth:`lookup` replaces that entry), otherwise
        ``storage_key(url)``.  A page already stored under the key is replaced,
        whatever its content type.  ``screenshot`` is image bytes or a base64
        string (optionally a ``data:image/...`` URL) and is saved as JPEG.
        """
        return self._put(url, "web", {".txt": text.encode("utf-8"), ".jpg": _to_jpeg(screenshot)})

    def put_pdf(self, url: str, pdf_bytes: bytes) -> str:
        """Store a PDF, keyed and replacing like :meth:`put_web`; returns the key it is stored under."""
        return self._put(url, "pdf", {".pdf": pdf_bytes})

    def remove(self, url: str) -> ContentType | None:
        """Delete the cached page ``url`` refers to; returns its content type, or ``None`` if nothing was cached."""
        key = self.lookup(url)
        if key is None:
            return None
        with self._index_lock():
            self._commit(key, None)
            content_type = self._types.get(key)
            self._discard(key)
        if content_type is not None:
            self._delete_files(key, content_type)
        return content_type

    def _put(self, url: str, content_type: ContentType, files: Dict[str, bytes]) -> str:
        key = url if url in self._types else storage_key(url)
        for ext, data in files.items():
            _write_atomic(self._path(key, ext), data)
        with self._index_lock():
            previous = self._commit(key, content_type) or self._types.get(key)
            self._add(key, content_type)
        if previous in FILE_EXTENSIONS and previous != content_type:
            self._delete_files(key, previous)
        return key

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
            match = _match_key(key)
            if match is not None:
                self._keys_by_match.setdefault(match, []).append(key)
        self._types[key] = content_type

    def _discard(self, key: str) -> None:
        if self._types.pop(key, None) is None:
            return
        match = _match_key(key)
        keys = self._keys_by_match.get(match, [])
        if key in keys:
            keys.remove(key)
            if not keys:
                del self._keys_by_match[match]

    def _read_index(self) -> Dict[str, str]:
        try:
            with open(self.index_file, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read %s: %s. Treating it as empty.", self.index_file, e)
            return {}
        if not isinstance(index, dict):
            logger.warning("Ignoring %s: expected a JSON object", self.index_file)
            return {}
        return index

    def _commit(self, key: str, content_type: ContentType | None) -> Optional[str]:
        """Write one entry change to ``index.json``, keeping the entries other writers committed.

        ``content_type=None`` removes the entry.  Returns the entry's previous
        content type on disk.  Must be called under :meth:`_index_lock`.
        """
        index = self._read_index()
        previous = index.get(key)
        if content_type is None:
            index.pop(key, None)
        else:
            index[key] = content_type  # a replaced entry keeps its position
        _write_atomic(self.index_file, json.dumps(index, indent=2, ensure_ascii=False).encode('utf-8'))
        return previous

    @contextmanager
    def _index_lock(self) -> Iterator[None]:
        """Serialize index updates across threads and, on POSIX, across processes.

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
    """Replace ``path`` with ``data``; a concurrent reader sees either the old or the complete new content."""
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, 'xb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


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
