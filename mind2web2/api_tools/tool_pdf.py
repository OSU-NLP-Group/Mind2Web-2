"""PDF detection, download, and parsing, for caching cited pages and for evaluation.

* :func:`is_pdf` decides whether a URL serves a PDF, from the URL itself or
  from the headers and first bytes of a single streamed request.
* :meth:`PDFParser.fetch` downloads a PDF and returns its bytes only if they
  really are a PDF, so that an HTML page behind a PDF-looking URL is loaded in
  a browser instead of being stored as a broken PDF.
* :meth:`PDFParser.extract` renders a PDF (URL, local path, or bytes) into page
  screenshots and text, or returns ``(None, None)``.

All network calls are asynchronous and bounded in time; none blocks the event
loop.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
from io import BytesIO
from typing import List, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

import httpx
import pymupdf
from PIL import Image

from ..utils.url_tools import normalize_url_for_browser

_log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"  # PDF file header
UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
USER_AGENT_STRINGS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 OPR/109.0.0.0',
]

PROBE_TIMEOUT_SECONDS = 10.0
"""Upper bound for :func:`is_pdf`'s network check."""
DOWNLOAD_TIMEOUT_SECONDS = 60.0
"""Upper bound for one PDF download."""
MAX_PDF_BYTES = 100 * 1024 * 1024
"""Larger downloads are abandoned."""
PROBE_BYTES = 1024


def is_pdf_by_suffix(url: str) -> bool:
    """Check if URL likely points to PDF based on path/query patterns."""
    parsed = urlparse(url.lower())
    path = unquote(parsed.path)

    # Direct .pdf extension
    if path.endswith('.pdf'):
        return True

    # Common PDF URL patterns
    pdf_patterns = [
        'arxiv.org/pdf/',
        '/download/pdf',
        '/fulltext.pdf',
        '/article/pdf',
        '/content/pdf',
        'type=pdf',
        'format=pdf',
        'download=pdf',
        '.pdf?',
        '/pdf/',
        'pdfviewer',
    ]

    url_lower = url.lower()
    return any(pattern in url_lower for pattern in pdf_patterns)


async def is_pdf(url: str, logger: Optional[logging.Logger] = None,
                 timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Whether ``url`` serves a PDF.

    True when the URL looks like a PDF (:func:`is_pdf_by_suffix`).  Otherwise
    a single streamed GET, bounded by ``timeout`` seconds in total, reads the
    response headers and first KiB: a PDF ``Content-Type`` or the ``%PDF-``
    signature means a PDF.  Network errors and timeouts count as "not a PDF",
    so the caller loads the URL in a browser.
    """
    log = logger or _log
    url = normalize_url_for_browser(url)
    if is_pdf_by_suffix(url):
        log.debug(f"URL pattern indicates PDF: {url}")
        return True
    try:
        return await asyncio.wait_for(_probe_is_pdf(url), timeout)
    except Exception as e:  # timeouts, network and protocol errors
        log.debug(f"PDF probe failed for {url}: {type(e).__name__}: {e}")
        return False


async def _probe_is_pdf(url: str) -> bool:
    headers = {"User-Agent": random.choice(USER_AGENT_STRINGS), "Accept": "*/*"}
    async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=PROBE_TIMEOUT_SECONDS) as client:
        async with client.stream("GET", url, headers=headers) as response:
            if "pdf" in response.headers.get("content-type", "").lower():
                return True
            head = b""
            async for chunk in response.aiter_bytes():
                head += chunk
                if len(head) >= PROBE_BYTES:
                    break
            return head.lstrip().startswith(PDF_MAGIC)


def _is_pdf_bytes(data: Optional[bytes]) -> bool:
    return bool(data) and data.lstrip().startswith(PDF_MAGIC)


class PDFParser:
    """Download and parse PDFs; failures return ``None`` values instead of raising."""

    # Default limits
    MAX_PAGES: int = 100
    MAX_IMAGE_PAGES: int = 50
    RENDER_DPI: int = 144
    JPEG_QUALITY: int = 70

    # ------------------ Public API ------------------
    async def extract(
            self,
            source: Union[str, bytes, BytesIO],
    ) -> Tuple[Optional[List[str]], Optional[str]]:
        """Page screenshots and text of a PDF.

        ``source`` is a URL, a local file path, or the PDF bytes.  Returns
        ``(images, text)``: base64 JPEG renderings of the first
        ``MAX_IMAGE_PAGES`` pages and the plain text of the first ``MAX_PAGES``
        pages.  Returns ``(None, None)`` when the PDF cannot be obtained or
        parsed, including when a URL does not serve a PDF.
        """
        try:
            if isinstance(source, (bytes, BytesIO)):
                data = source.getvalue() if isinstance(source, BytesIO) else source
            elif isinstance(source, str) and source.lower().startswith(("http://", "https://")):
                data = await self.fetch(source)
            else:  # Local file
                data = await asyncio.to_thread(lambda p: open(p, "rb").read(), str(source))
            if not _is_pdf_bytes(data):
                return None, None
            # Parsing is CPU-intensive and synchronous: run it in a thread
            return await asyncio.to_thread(self._extract_from_bytes, data)
        except Exception as e:
            _log.warning(f"PDF extraction failed: {type(e).__name__}: {e}")
            return None, None

    async def fetch(self, url: str) -> Optional[bytes]:
        """Download the PDF at ``url``; ``None`` unless the response body is a PDF.

        An arXiv URL that returns something else is retried on
        ``export.arxiv.org``.  A download is abandoned after
        ``DOWNLOAD_TIMEOUT_SECONDS`` or beyond ``MAX_PDF_BYTES``.
        """
        data = await self._download(url)
        if not _is_pdf_bytes(data) and "arxiv.org" in url:
            data = await self._download(url.replace("://arxiv.org", "://export.arxiv.org"))
        return data if _is_pdf_bytes(data) else None

    # ------------------ Internal Implementation ------------------
    async def _download(self, url: str) -> Optional[bytes]:
        headers = {
            "User-Agent": UA_CHROME,
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        }
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(follow_redirects=True, verify=False,
                                             timeout=DOWNLOAD_TIMEOUT_SECONDS) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        chunks: List[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_PDF_BYTES:
                                _log.warning(f"Abandoned download of {url}: larger than {MAX_PDF_BYTES} bytes")
                                return None
                            chunks.append(chunk)
                        return b"".join(chunks)
        except Exception as e:  # timeouts, network errors, HTTP error statuses
            _log.info(f"Download failed for {url}: {type(e).__name__}: {e}")
            return None

    def _extract_from_bytes(self, data: bytes) -> Tuple[Optional[List[str]], Optional[str]]:
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError):
            return None, None

        imgs: List[str] = []
        texts: List[str] = []
        zoom = self.RENDER_DPI / 72
        with doc:
            max_pages = min(self.MAX_PAGES, doc.page_count)
            max_img_pages = min(self.MAX_IMAGE_PAGES, doc.page_count)
            for i in range(max_pages):
                page = doc.load_page(i)
                texts.append(page.get_text("text"))

                if i < max_img_pages:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

                    buf = BytesIO()
                    img.save(buf, "JPEG", quality=self.JPEG_QUALITY,
                             optimize=True, progressive=True)
                    imgs.append(base64.b64encode(buf.getvalue()).decode())
        return imgs, "\n".join(texts)
