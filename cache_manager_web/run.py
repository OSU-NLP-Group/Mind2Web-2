#!/usr/bin/env python3
"""Entry point for the web-based Cache Manager.

Usage:
    uv run python cache_manager_web/run.py [agent_name_or_path] [--port 8000]

Examples:
    uv run python cache_manager_web/run.py zhoukai                # Agent name (resolves to <cache-dir>/zhoukai)
    uv run python cache_manager_web/run.py /path/to/agent/cache   # Full path

Opens the Cache Manager web UI in your default browser.  The server accepts
requests only for the loopback host names unless ``--host`` names another
interface; see ``LocalRequestGuard`` in ``backend/app.py``.
"""

import os
import sys
import argparse
import webbrowser
from pathlib import Path

# Ensure project root is on the path so we can import mind2web2
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from cache_manager_web.backend.config import DEFAULT_PORT, LOOPBACK_HOSTS  # noqa: E402

WILDCARD_HOSTS = ("0.0.0.0", "::", "")


def resolve_cache_folder(arg: str, cache_root: Path) -> Path:
    """Resolve an agent name or path to a cache folder.

    If the argument is an existing directory, use it directly.
    Otherwise, treat it as an agent name under ``cache_root``.
    """
    path = Path(arg)

    # If it's already a valid directory path, use it as-is
    if path.is_dir():
        return path.resolve()

    # Treat as agent name: look under cache root
    agent_path = cache_root / arg
    if agent_path.is_dir():
        return agent_path.resolve()

    # Not found — list available agents and exit
    available = sorted(d.name for d in cache_root.iterdir() if d.is_dir()) if cache_root.is_dir() else []
    msg = f"Cache folder not found: '{arg}'"
    if available:
        msg += f"\nAvailable agents: {', '.join(available)}"
    else:
        msg += f"\nNo agent folders found in {cache_root}"
    print(msg, file=sys.stderr)
    sys.exit(1)


def allowed_hosts(host: str) -> str:
    """The ``CM_ALLOWED_HOSTS`` value for a server bound to ``host``: the loopback names, plus ``host`` itself,
    or plus ``"*"`` (any IP address) when ``host`` is a wildcard address."""
    extra = "*" if host in WILDCARD_HOSTS else host.strip("[]").lower()
    return ",".join(dict.fromkeys([*LOOPBACK_HOSTS, extra]))


def main():
    parser = argparse.ArgumentParser(description="Cache Manager Web UI")
    parser.add_argument("agent", nargs="?", default=None,
                        help="Agent name (e.g. 'zhoukai'), looked up under --cache-dir, or the path of an agent's cache folder")
    parser.add_argument("--cache-dir", type=Path, default=project_root / "cache",
                        help="Directory holding one cache folder per agent (default: %(default)s)")
    parser.add_argument("--answers-dir", type=Path, default=None,
                        help="Directory holding one answers folder per agent, shown in the Answer view "
                             "(default: the 'answers' directory next to the cache directory)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to run on (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind to (default: %(default)s). A wildcard address such as 0.0.0.0 "
                             "lets any machine that can reach the port read and change the cache.")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # The app reads its settings from the environment when uvicorn imports it
    if args.agent:
        cache_folder = resolve_cache_folder(args.agent, args.cache_dir)
        os.environ["CM_INITIAL_CACHE_FOLDER"] = str(cache_folder)
    if args.answers_dir:
        os.environ["CM_ANSWERS_DIR"] = str(args.answers_dir.resolve())
    os.environ["CM_ALLOWED_HOSTS"] = allowed_hosts(args.host)
    if args.host in WILDCARD_HOSTS:
        print(f"Warning: --host {args.host!r} serves every network interface, and the Cache Manager has no "
              "authentication: anyone who can reach the port can read and change the cache. Other machines "
              "must use this machine's IP address; host names other than localhost are refused.", file=sys.stderr)

    # Open browser after a short delay
    if not args.no_browser:
        import threading
        browser_host = "127.0.0.1" if args.host in WILDCARD_HOSTS else args.host
        if ":" in browser_host and not browser_host.startswith("["):
            browser_host = f"[{browser_host}]"  # an IPv6 address

        def open_browser():
            import time
            time.sleep(1.0)
            webbrowser.open(f"http://{browser_host}:{args.port}")
        threading.Thread(target=open_browser, daemon=True).start()

    import uvicorn
    uvicorn.run(
        "cache_manager_web.backend.app:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
