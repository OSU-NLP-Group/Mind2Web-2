"""URL extraction from answer text, and the URL normalization shared by the page cache and the crawler."""
import re
from typing import List
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
    """The form under which two URLs count as the same page for cache lookups and crawl deduplication.

    UTM parameters and the fragment are removed, the URL is percent-decoded,
    a trailing slash is removed, ``http`` becomes ``https``, ``www.`` is
    dropped, and the result is lowercased.  UTM parameters are removed both
    before and after decoding, so encoded ones are removed too.
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

    return decoded.lower()



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
    r"|[^\s<>\"`{}|\\^\[\]\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u201c\u201d\u00ab\u00bb\u2026])"
)
_URL_RE = re.compile(rf"(?:https?://|(?<![\w/.@-])www\.){_URL_CHAR}+", re.IGNORECASE)
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_TRAILING_PUNCTUATION = ".,;:!?*'"


def _trim_url(url: str) -> str:
    """Strip sentence punctuation and unbalanced closing parentheses from the end of a matched URL."""
    while url:
        if url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
        elif url[-1] == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        else:
            break
    return url


def regex_find_urls(text: str) -> List[str]:
    """Every ``http(s)://`` and ``www.`` URL in ``text`` (Markdown or plain), in order of first appearance.

    Handles Markdown links and autolinks, parentheses inside URLs (kept when
    balanced, as in Wikipedia titles), Markdown backslash escapes, trailing
    sentence punctuation, and URLs followed directly by CJK punctuation.
    ``www.`` URLs get an ``https://`` scheme.  Only URLs that
    ``validators.url`` accepts are returned.
    """
    urls: List[str] = []
    for match in _URL_RE.finditer(text):
        url = _trim_url(_MARKDOWN_ESCAPE_RE.sub(r"\1", match.group()))
        if url.lower().startswith("www."):
            url = "https://" + url
        if _is_valid_url(url):
            urls.append(url)
    return list(dict.fromkeys(urls))
