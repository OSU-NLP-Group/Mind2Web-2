"""URL extraction from answer text, and the URL normalizations of the page cache and the crawler."""
import re
from typing import List, Optional
from urllib.parse import urldefrag, unquote, urlparse, parse_qs, urlencode, urlunparse

import validators
from pydantic import BaseModel

class URLs(BaseModel):
    urls: List[str]

def _is_valid_url(u: str) -> bool:
    return validators.url(u) is True

def remove_utm_parameters(url: str) -> str:
    """Remove all UTM tracking parameters from URL."""
    parsed = urlparse(url)

    # If there are no query parameters, return original URL
    if not parsed.query:
        return url

    # Parse query parameters
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Filter out all utm_* parameters
    filtered_params = {k: v for k, v in params.items() if not k.startswith('utm_')}

    # Reconstruct query string
    new_query = urlencode(filtered_params, doseq=True)

    # Reconstruct URL
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))



def normalize_url_simple(url: str) -> str:
    """The form under which two URLs count as the same page for cache lookups.

    UTM parameters and the fragment are removed, the URL is percent-decoded,
    a trailing slash is removed, ``http`` becomes ``https``, ``www.`` is
    dropped, and the result is lowercased: it is
    :func:`normalize_url_keep_case`, lowercased.
    """
    return normalize_url_keep_case(url).lower()


def normalize_url_keep_case(url: str) -> str:
    """The form under which the crawler merges spellings of one page; it keeps the letter case of the URL.

    UTM parameters and the fragment are removed, the URL is percent-decoded,
    a trailing slash is removed, ``http`` becomes ``https``, and ``www.`` is
    dropped.  UTM parameters are removed both before and after decoding, so
    encoded ones are removed too.  Letter case is kept because a server may
    serve different pages for paths that differ only in case.
    """

    url=remove_utm_parameters(url)
    # Remove fragment
    url_no_frag, _ = urldefrag(url)

    # Decode URL encoding
    decoded = unquote(url_no_frag)

    # Remove trailing slash (except for root)
    if decoded.endswith('/') and len(decoded) > 1 and not decoded.endswith('://'):
        decoded = decoded[:-1]

    # Remove all UTM parameters
    parsed = urlparse(decoded)
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Filter out all utm_* parameters
        filtered_params = {k: v for k, v in params.items() if not k.startswith('utm_')}
        # Reconstruct query string
        new_query = urlencode(filtered_params, doseq=True)
        decoded = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))

    # Normalize scheme
    if decoded.startswith('http://'):
        decoded = 'https://' + decoded[7:]

    # Remove www prefix for comparison
    if '://www.' in decoded:
        decoded = decoded.replace('://www.', '://')

    return decoded



def normalize_url_for_browser(url: str) -> str:
    """``url`` without UTM parameters, with ``https://`` prepended if it has no ``http``, ``https``, or ``ftp`` scheme."""
    url=remove_utm_parameters(url)
    if not url.startswith(('http://', 'https://', 'ftp://')):
        return f'https://{url}'
    return url

# A URL runs until whitespace, a delimiter that cannot appear unencoded in a URL,
# or CJK / typographic punctuation.  A backslash may escape ASCII punctuation
# (Markdown), e.g. ``some\_page``; the escape is removed after matching.
_URL_CHAR = (
    r"(?:\\[!-/:-@\[-`{-~]"
    r"|[^\s<>\"`{}\\^\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u201c\u201d\u00ab\u00bb\u2026])"
)
_URL_START = r"(?:https?://|(?<![\w/.@-])www\.)"
_URL_RE = re.compile(rf"{_URL_START}{_URL_CHAR}+", re.IGNORECASE)
_URL_START_RE = re.compile(_URL_START, re.IGNORECASE)
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_TRAILING_PUNCTUATION = ".,;:!?*'|("
_CLOSING = {")": "(", "]": "["}
_EMPHASIS = ("~~", "__", "_")  # closing delimiters of Markdown emphasis that a URL can absorb


def _cut_url(match: str) -> str:
    r"""The part of a regex match that is the URL.

    The URL ends before the first closing parenthesis or bracket that it did
    not open, as in ``[text](https://a.com/x)`` or ``(see https://a.com)``,
    and before a ``|`` that starts another URL, as in a Markdown table
    without spaces.  Markdown-escaped characters (``\(``) count like the
    characters they escape.
    """
    depth = {"(": 0, "[": 0}
    i = 0
    while i < len(match):
        char = match[i]
        if char == "\\" and i + 1 < len(match):
            char = match[i + 1]
            step = 2
        else:
            step = 1
        if char in depth:
            depth[char] += 1
        elif char in _CLOSING:
            if depth[_CLOSING[char]] == 0:
                return match[:i]
            depth[_CLOSING[char]] -= 1
        elif char == "|" and _URL_START_RE.match(match, i + step):
            return match[:i]
        i += step
    return match


def _cut_before_bracket_or_pipe(raw: str) -> Optional[str]:
    r"""``raw`` cut before its first ``[`` or ``|`` outside the query, or ``None`` if it has none.

    ``validators.url`` accepts these two characters only in the query;
    elsewhere they are text that follows the URL, such as a footnote marker
    (``page[1]``) or a Markdown table cell border (``page|text``).
    Markdown-escaped characters (``\|``) count like the characters they
    escape.  The cut leaves at least one character after ``https://`` or
    ``www.``, so the ``[`` that opens an IPv6 host is kept.
    """
    host_start = _URL_START_RE.match(raw).end()
    part = "path"  # then "query" after "?", and "fragment" after "#"
    i = host_start
    while i < len(raw):
        escaped = raw[i] == "\\" and i + 1 < len(raw)
        char = raw[i + 1] if escaped else raw[i]
        if char == "?" and part == "path":
            part = "query"
        elif char == "#":
            part = "fragment"
        elif char in "[|" and part != "query" and i > host_start:
            return raw[:i]
        i += 2 if escaped else 1
    return None


def _trim_url(url: str, preceding: str) -> str:
    """Strip sentence punctuation and opening parentheses, which no URL ends with, from the end of a URL,
    and the Markdown emphasis delimiter (``_``, ``__``, ``~~``) that closes one ``preceding``, the text
    before the URL, opened."""
    emphasis = next((d for d in _EMPHASIS if preceding.endswith(d)), None)
    while url:
        if url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
        elif emphasis and url.endswith(emphasis):
            url = url[:-len(emphasis)]
            emphasis = None
        else:
            break
    return url


def _clean_url(raw: str, preceding: str) -> str:
    """``raw`` with Markdown escapes removed and trimmed (:func:`_trim_url`); a ``www.`` URL gets ``https://``."""
    url = _trim_url(_MARKDOWN_ESCAPE_RE.sub(r"\1", raw), preceding)
    return "https://" + url if url.lower().startswith("www.") else url


def regex_find_urls(text: str) -> List[str]:
    """Every ``http(s)://`` and ``www.`` URL in ``text`` (Markdown or plain), in order of first appearance.

    Handles Markdown links and autolinks, parentheses inside URLs (kept when
    balanced, as in Wikipedia titles), ``[`` and ``|`` in the query
    (``?filter[type]=x``, ``?family=A|B``), Markdown backslash escapes and
    emphasis around URLs, trailing sentence punctuation, and URLs followed
    directly by CJK punctuation.  A URL that ``validators.url`` rejects is
    cut before its first ``[`` or ``|`` outside the query, where such a
    character is text after the URL, as in a footnote marker (``page[1]``) or
    a table cell border (``page|text``); the text after the cut is searched
    for further URLs.  ``www.`` URLs get an ``https://`` scheme.  Only URLs
    that ``validators.url`` accepts are returned.
    """
    urls: List[str] = []
    pos = 0
    while (match := _URL_RE.search(text, pos)) is not None:
        preceding = text[max(match.start() - 2, 0):match.start()]
        raw = _cut_url(match.group())
        url = _clean_url(raw, preceding)
        if not _is_valid_url(url) and (shorter := _cut_before_bracket_or_pipe(raw)) is not None:
            raw, url = shorter, _clean_url(shorter, preceding)
        pos = match.start() + max(len(raw), 1)  # a cut match is scanned again from the cut
        if _is_valid_url(url):
            urls.append(url)
    return list(dict.fromkeys(urls))
