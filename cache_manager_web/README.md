# Cache Manager Web

A web-based tool for reviewing and managing cached web pages used by Mind2Web agents. Features a three-panel interface with task browsing, URL inspection, and content preview — plus a Chrome extension for live page capture.

## Why This Tool Exists

When agents answer questions, they reference URLs that need to be cached locally. The automated caching (`batch_answer_cache.py`) sometimes fails because pages require CAPTCHA verification, have anti-bot protection, or are behind login walls. This tool lets a human reviewer:

1. **Identify problematic URLs** — automatic keyword detection flags pages with issues (e.g., "access denied", "verify you are human")
2. **Review cached content** — view screenshots and extracted text side by side
3. **Recapture failed pages** — open the URL in a real browser, pass verification manually, then capture the page content using the Chrome extension
4. **Track progress** — mark URLs as reviewed and see completion status per task

## Architecture

```
cache_manager_web/
├── run.py                  # Entry point — starts FastAPI server
├── backend/
│   ├── app.py              # FastAPI app with lifespan, CORS, static file serving
│   ├── config.py           # Configuration constants
│   └── api/
│       └── routes.py       # All REST API endpoints + SSE
├── frontend/
│   ├── index.html          # Single-page app shell
│   ├── css/style.css       # Full design system
│   └── js/
│       ├── main.js         # Init, toolbar, keyboard shortcuts, drag-drop
│       ├── actions.js      # Shared actions (select task/URL, toast, etc.)
│       ├── store.js        # Reactive state store with selective subscriptions
│       ├── api.js          # Backend API client
│       └── components/
│           ├── task-panel.js   # Task list with search and filter
│           ├── url-list.js     # URL list with filters and progress bar
│           └── preview.js      # Screenshot/text/answer preview
└── extension/
    ├── manifest.json       # Chrome Extension Manifest V3
    ├── background.js       # Service worker for keyboard shortcut capture
    ├── popup.html/js       # Extension popup UI
    └── icons/              # Extension icon
```

**Key design decisions:**
- **No build step** — vanilla JS with ES modules, works directly in the browser
- **Chrome Extension for capture** — uses a real browser session (not Playwright/Selenium), so it works on Cloudflare-protected sites and other anti-bot pages
- **SSE for real-time updates** — when the extension captures a page, the web UI updates instantly
- **Reuses existing models** — `CacheManager` and `KeywordDetector` from `cache_manager.models`

## Quick Start

### 1. Start the server

```bash
# From the project root directory
uv run python3 cache_manager_web/run.py /path/to/agent/cache/folder

# Options:
#   --port 8000       Port number (default: 8000)
#   --host 127.0.0.1  Host to bind (default: 127.0.0.1)
#   --no-browser      Don't auto-open the browser
```

The cache folder should be a directory like `JudyAgent/` that contains task subdirectories, each with cached URL content.

The server will:
- Load all tasks and URLs from the cache folder
- Scan for issues using keyword detection
- Open `http://127.0.0.1:8000` in your default browser

### 2. Install the Chrome Extension (optional, for recapturing)

1. Open Chrome and go to `chrome://extensions/`
2. Enable **Developer mode** (toggle in top-right)
3. Click **Load unpacked**
4. Select the `cache_manager_web/extension/` directory
5. Pin the extension for easy access

The extension is only needed if you want to recapture pages that failed automated caching.

## Usage Guide

### Reviewing Cached Content

1. **Select a task** from the left panel — tasks with issues show colored dots (red = definite issues, yellow = possible, green = clean)
2. **Select a URL** from the middle panel — URLs are color-coded by status:
   - Red border: definite issue detected
   - Yellow border: possible issue
   - Green border: reviewed
   - Gray border: unreviewed, no issues
3. **Preview content** in the right panel:
   - **Screenshot** — the cached page screenshot (fit-to-width by default, zoom with Ctrl+Wheel)
   - **Text** — the extracted text content
   - **Answer** — the agent's answer file (if available), with the current URL highlighted

### Issue Navigation

Use **N** / **Shift+N** to jump between URLs with detected issues across all tasks. The issue counter in the toolbar shows your position.

### Recapturing a Page

When a page has issues (e.g., CAPTCHA, blocked):

1. Select the URL in the web UI
2. Click **Recapture Live** (or press **Ctrl+U**)
3. The page opens in a new browser tab
4. Pass any verification (solve CAPTCHA, log in, etc.)
5. Click the **Cache Manager Capture** extension button (or press **Alt+Shift+C**)
6. The captured content is sent back to the server and saved automatically

### Marking Progress

- **Ctrl+Enter** — mark current URL as reviewed
- The progress bar shows how many URLs in the current task have been reviewed
- The toolbar shows overall review progress across all tasks
- Review status is saved to `reviewed.json` in each task directory and persists across sessions

### Keyboard Shortcuts

Press **?** to see all keyboard shortcuts. Key ones:

| Shortcut | Action |
|----------|--------|
| `↑`/`k`, `↓`/`j` | Navigate URLs |
| `N` / `Shift+N` | Next/previous issue |
| `1` / `2` / `3` | Screenshot / Text / Answer view |
| `Space` | Toggle Screenshot ↔ Text |
| `Ctrl+Enter` | Mark as reviewed |
| `Ctrl+U` | Recapture live |
| `Ctrl+O` | Open cache folder |
| `Ctrl+Wheel` | Zoom screenshot |

### Filters

The URL panel has several filters:
- **All / Web / PDF** — filter by content type
- **Issues** — show only URLs with detected issues
- **Todo** — show only unreviewed URLs

### MHTML Upload

You can also upload MHTML files (saved from Chrome's "Save as MHTML") by:
- Clicking **Upload MHTML** in the preview footer
- Dragging and dropping `.mhtml` files onto the preview panel

## API Reference

The backend exposes a REST API at `http://127.0.0.1:8000/api/`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/load` | Load a cache folder |
| `GET` | `/api/status` | Current loading status |
| `GET` | `/api/tasks` | List all tasks |
| `GET` | `/api/tasks/{id}/urls` | List URLs for a task |
| `GET` | `/api/content/{id}/text?url=...` | Get text content |
| `GET` | `/api/content/{id}/screenshot?url=...` | Get screenshot JPEG |
| `GET` | `/api/content/{id}/pdf?url=...` | Get PDF content |
| `POST` | `/api/capture` | Receive capture from extension |
| `POST/GET` | `/api/capture/target` | Set/get active capture target |
| `POST` | `/api/review/{id}` | Set review status |
| `GET` | `/api/review-progress` | Overall review progress |
| `GET` | `/api/answers/{id}` | List answer files |
| `DELETE` | `/api/urls/{id}?url=...` | Delete a URL |
| `POST` | `/api/upload-mhtml/{id}?url=...` | Upload MHTML |
| `POST` | `/api/scan` | Re-scan all tasks for issues |
| `GET` | `/api/events` | SSE stream for real-time updates |

## Dependencies

- **Python 3.10+**
- **FastAPI** + **uvicorn** — web framework and ASGI server
- **python-multipart** — for file upload handling
- **cache_manager.models** — reuses `CacheManager` and `KeywordDetector` from the existing codebase

All dependencies are managed via `uv` and `pyproject.toml` in the project root.

## Relationship to cache_manager (PySide6)

The original `cache_manager/` is a PySide6 desktop GUI. This `cache_manager_web/` is a web-based alternative that:

- Reuses the same data models (`CacheManager`, `KeywordDetector`)
- Reads/writes the same cache directory format
- Is fully compatible — you can switch between the two tools
- Adds the Chrome Extension for page capture (the PySide6 version used Qt WebEngine)
- Has no Qt/PySide6 dependency for the web version
