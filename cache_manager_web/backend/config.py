"""Configuration for the web-based cache manager."""

from __future__ import annotations
from pathlib import Path

# Paths
PACKAGE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PACKAGE_DIR / "frontend"

# Server
DEFAULT_PORT = 8000
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")  # the hosts served unless CM_ALLOWED_HOSTS says otherwise

# Capture and upload limits, in bytes (status 413 above them)
# A full-page PNG of 1100x20000 CSS pixels is at most about 88 MB even as incompressible RGBA
MAX_SCREENSHOT_SIZE = 100 * 1024 * 1024  # a decoded screenshot sent to /api/capture
MAX_TEXT_SIZE = 50 * 1024 * 1024  # the HTML, or else the text, sent to /api/capture (UTF-8)
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # an uploaded PDF or MHTML file


def max_request_size(path: str) -> int | None:
    """The largest request body accepted for ``path``, or ``None`` if the path has no limit.

    A capture carries a base64 screenshot (4/3 of its size) and HTML, which
    JSON escaping can enlarge; an upload carries one file in a multipart body.
    One MiB covers the other fields and the encoding overhead.
    """
    if path == "/api/capture":
        return MAX_SCREENSHOT_SIZE * 4 // 3 + 2 * MAX_TEXT_SIZE + (1 << 20)
    if path.startswith(("/api/upload-pdf/", "/api/upload-mhtml/")):
        return MAX_UPLOAD_SIZE + (1 << 20)
    return None
