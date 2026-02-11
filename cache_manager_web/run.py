#!/usr/bin/env python3
"""Entry point for the web-based Cache Manager.

Usage:
    python run.py [cache_folder_path] [--port 8000]

Opens the Cache Manager web UI in your default browser.
"""

import sys
import argparse
import webbrowser
from pathlib import Path

# Ensure project root is on the path so we can import mind2web2
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(description="Cache Manager Web UI")
    parser.add_argument("cache_folder", nargs="?", default=None,
                        help="Path to the agent cache folder to load on startup")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Store startup cache folder in environment so the app can read it
    import os
    if args.cache_folder:
        os.environ["CM_INITIAL_CACHE_FOLDER"] = str(Path(args.cache_folder).resolve())

    # Open browser after a short delay
    if not args.no_browser:
        import threading
        def open_browser():
            import time
            time.sleep(1.0)
            webbrowser.open(f"http://{args.host}:{args.port}")
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
