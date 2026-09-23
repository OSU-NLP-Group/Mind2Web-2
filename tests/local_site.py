"""A local HTTP server with scripted routes, so that network code is tested without the internet."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional

HTML = {"Content-Type": "text/html; charset=utf-8"}


@dataclass
class Route:
    """A scripted response: sent ``delay`` seconds after the request arrives.

    ``respond``, if set, picks the response from the request instead
    (for example by its cookies).
    """

    status: int = 200
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=lambda: dict(HTML))
    delay: float = 0.0
    respond: Optional[Callable[[BaseHTTPRequestHandler], "Route"]] = None


class LocalSite:
    """Serves ``routes`` (by path) on 127.0.0.1 while used as a context manager; other paths get a 404."""

    def __init__(self, routes: Dict[str, Route]):
        site = self
        self.routes = routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                route = site.routes.get(self.path.split("?")[0]) or Route(404, b"<title>Not found</title>Not found")
                if route.respond is not None:
                    route = route.respond(self)
                time.sleep(route.delay)
                try:
                    self.send_response(route.status)
                    for name, value in route.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(route.body)))
                    self.end_headers()
                    self.wfile.write(route.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up waiting

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "LocalSite":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"


def unused_port_url() -> str:
    """A local URL on which nothing listens, so connections are refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/"
