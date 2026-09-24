"""FastAPI application for the web-based Cache Manager."""

from __future__ import annotations
import ipaddress
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Iterable, Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import Headers

from . import config
from .models import CacheManager, KeywordDetector, ReviewStateError
from .config import FRONTEND_DIR, LOOPBACK_HOSTS
from .api.routes import router, set_app_state

logger = logging.getLogger(__name__)


class LocalRequestGuard:
    """ASGI middleware that refuses requests from web pages other than the Cache Manager's own.

    The API has no authentication, and every page open in the reviewer's
    browser, including the pages being recaptured, can send requests to this
    server.  Two checks keep them out:

    - The ``Host`` header must name one of ``allowed_hosts``; ``"*"`` among
      them also allows any IP address, but no other name.  A page that
      points its own domain at this machine (DNS rebinding) sends that
      domain as the host and is refused, even when the server listens on
      every interface.
    - A request that can change data (any method but GET, HEAD, and OPTIONS)
      and carries an ``Origin`` header must come from this server's own
      origin or from a Chrome extension.  Browsers send ``Origin`` with such
      requests, so a cross-site form post or fetch is refused.  Clients that
      are not browsers, such as scripts, send none and are allowed.

    Refused requests get status 403.  No CORS headers are sent, so pages on
    other origins cannot read responses either.  The extension does not need
    them: an extension's pages and service worker may fetch from the hosts
    in its ``host_permissions``.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app, allowed_hosts: Iterable[str]):
        self.app = app
        self.allowed_hosts = frozenset(host.strip().lower() for host in allowed_hosts if host.strip())

    async def __call__(self, scope, receive, send):
        reason = self.refusal(scope) if scope["type"] == "http" else None
        if reason:
            await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def refusal(self, scope) -> Optional[str]:
        """Why the request is refused, or ``None`` if it is allowed."""
        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        name = _host_name(host)
        if name not in self.allowed_hosts and not ("*" in self.allowed_hosts and _is_ip_address(name)):
            return (f"Host {host!r} is not served; the served hosts are {', '.join(sorted(self.allowed_hosts))} "
                    "(see run.py --host; '*' stands for any IP address)")
        origin = headers.get("origin")
        if (scope["method"] not in self.SAFE_METHODS and origin is not None
                and origin.lower() != f"{scope['scheme']}://{host}"
                and not origin.startswith("chrome-extension://")):
            return f"Requests from {origin} are not accepted"
        return None


def _is_ip_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class FrameGuard:
    """ASGI middleware that lets no other site show the Cache Manager in a frame.

    Every response carries ``Content-Security-Policy: frame-ancestors 'self'``
    and ``X-Frame-Options: SAMEORIGIN``, so a page on another origin cannot
    lay the UI under its own content to make the reviewer click its buttons
    (clickjacking), while the UI's own PDF preview frame still loads.
    """

    HEADERS = [(b"content-security-policy", b"frame-ancestors 'self'"), (b"x-frame-options", b"SAMEORIGIN")]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self.HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeGuard:
    """ASGI middleware that refuses capture and upload requests whose body is too large, before reading it.

    For the paths that ``config.max_request_size`` limits, a request must
    declare its body size in ``Content-Length`` (status 411 otherwise;
    browsers send it for every body the Cache Manager page and the extension
    send), and a declared size over the limit is refused with status 413.
    The routes also check each field against its own limit
    (``MAX_SCREENSHOT_SIZE``, ``MAX_TEXT_SIZE``, ``MAX_UPLOAD_SIZE``).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = config.max_request_size(scope["path"]) if scope["type"] == "http" else None
        if limit is not None and scope["method"] == "POST":
            length = Headers(scope=scope).get("content-length")
            if length is None or not length.isdigit():
                await JSONResponse({"detail": "The request must declare its size in Content-Length"},
                                   status_code=411)(scope, receive, send)
                return
            if int(length) > limit:
                await JSONResponse({"detail": f"The request body ({int(length):,} bytes) exceeds the limit of "
                                              f"{limit:,} bytes"}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _host_name(host: str) -> str:
    """The host name of a ``Host`` header value, without its port and the brackets of an IPv6 address."""
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def allowed_hosts_from_env() -> list[str]:
    """``CM_ALLOWED_HOSTS`` (comma-separated; ``"*"`` for any IP address), or the loopback host names if it is unset."""
    value = os.environ.get("CM_ALLOWED_HOSTS")
    return value.split(",") if value else list(LOOPBACK_HOSTS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize models on startup."""
    cm = CacheManager()
    kd = KeywordDetector()
    set_app_state(cm, kd)

    # Auto-load cache folder if specified via env
    initial_folder = os.environ.get("CM_INITIAL_CACHE_FOLDER")
    if initial_folder and Path(initial_folder).is_dir():
        try:
            cm.load_agent_cache(initial_folder)
            logger.info(f"Auto-loaded cache from {initial_folder}")
        except Exception as e:
            logger.warning(f"Failed to auto-load cache: {e}")

    yield


app = FastAPI(title="Cache Manager", lifespan=lifespan)

app.add_middleware(BodySizeGuard)
app.add_middleware(LocalRequestGuard, allowed_hosts=allowed_hosts_from_env())
app.add_middleware(FrameGuard)  # added last, so it runs first and marks refusals too

# API routes
app.include_router(router, prefix="/api")


@app.exception_handler(ReviewStateError)
async def review_state_error(request, exc: ReviewStateError):
    """Answer 500 with the error's message, which names the review-state file that cannot be read and what to do."""
    return JSONResponse({"detail": str(exc)}, status_code=500)


# Serve frontend static files
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_index():
    """Serve the main SPA page."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    """Return an empty favicon to avoid 404 in browser console."""
    return Response(content=b"", media_type="image/x-icon")
