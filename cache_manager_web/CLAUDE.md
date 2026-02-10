# Cache Manager Web

Web-based tool for reviewing and fixing cached web pages used by Mind2Web agents. Replaces the PySide6 GUI (`cache_manager/`) with a browser-based UI + Chrome Extension.

## Architecture

```
cache_manager_web/
├── run.py                     # Entry point: starts FastAPI server, auto-opens browser
├── backend/
│   ├── app.py                 # FastAPI app, lifespan, CORS, static file serving
│   ├── config.py              # Constants (paths, limits)
│   └── api/routes.py          # ALL API endpoints + SSE + MHTML parsing
├── frontend/
│   ├── index.html             # Single-page app shell (no framework, no build step)
│   ├── css/style.css          # Complete design system with CSS custom properties
│   └── js/
│       ├── main.js            # Init, toolbar, keyboard shortcuts, SSE, drag-drop
│       ├── actions.js         # Shared actions (selectTask, selectUrl, toast, etc.)
│       ├── store.js           # Reactive state store with selective subscriptions
│       ├── api.js             # Fetch-based API client
│       └── components/
│           ├── task-panel.js  # Task list with search/filter
│           ├── url-list.js    # URL list with filters, progress bar
│           └── preview.js     # Screenshot/text/answer preview with zoom
└── extension/
    ├── manifest.json          # Chrome Extension Manifest V3
    ├── background.js          # Service worker (Alt+Shift+C capture)
    └── popup.html/js          # Extension popup UI
```

## Key Design Decisions

- **No build step**: Vanilla JS with ES modules. Files are served directly by FastAPI's StaticFiles.
- **No circular imports**: Components import shared actions from `actions.js`, NOT from `main.js`. This is critical — `main.js` imports components, so components must not import from `main.js`.
- **Selective state subscriptions**: `subscribe(fn, ['key1', 'key2'])` — components only re-render when their relevant keys change.
- **Chrome Extension for capture**: Uses a real browser session (not Playwright/Selenium) so it works on Cloudflare-protected and anti-bot pages.
- **SSE for real-time updates**: When the extension captures a page, the frontend updates instantly.
- **contentVersion cache busting**: Screenshot URLs include `&v={contentVersion}` to force browser to re-fetch after capture.
- **MHTML parsing without Qt**: Uses Python's `email` module to parse MHTML (MIME format), no PySide6 dependency.

## Reused from cache_manager/

The backend imports these from `cache_manager.models` (pure Python, no Qt):
- `CacheManager` — loads/reads/writes the cache directory structure
- `KeywordDetector` / `DetectionResult` — scans text for issue keywords (CAPTCHA, access denied, etc.)
- `reviewed.json` — per-task review status persistence

## Running

```bash
uv run python3 cache_manager_web/run.py /path/to/agent/cache/folder
# Options: --port 8000  --host 127.0.0.1  --no-browser
```

## Package Management

This project uses `uv`, not pip. Use `uv run`, `uv sync`, `uv add`.

## API Endpoints (routes.py)

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/load | Load cache folder, run issue scan |
| GET | /api/status | Current load status |
| GET | /api/tasks | Task list with summaries |
| GET | /api/tasks/{id}/urls | URLs with issue detection, reviewed status |
| GET | /api/content/{id}/text | Text content + issues |
| GET | /api/content/{id}/screenshot | Screenshot JPEG |
| GET | /api/content/{id}/pdf | PDF content |
| POST/GET | /api/capture/target | Active capture target for extension |
| POST | /api/capture | Receive capture from extension |
| POST | /api/review/{id} | Set review status |
| GET | /api/review-progress | Overall progress |
| GET | /api/answers/{id} | Answer markdown files |
| DELETE | /api/urls/{id} | Delete URL |
| POST | /api/upload-mhtml/{id} | Upload MHTML |
| GET | /api/events | SSE stream |

## State Store (store.js)

Key state fields:
- `loaded`, `agentName`, `agentPath`, `stats` — cache status
- `tasks`, `taskIssues`, `selectedTaskId` — task list
- `urls`, `selectedUrl`, `urlTotal`, `urlReviewedCount` — URL list
- `previewMode`, `currentText`, `currentIssues`, `answers` — preview
- `issueIndex`, `issueCursor` — cross-task issue navigation
- `contentVersion` — incremented on capture to bust screenshot cache
- `fitToWidth`, `zoomLevel` — screenshot zoom

## Common Tasks for Contributors

**Adding a new API endpoint**: Add to `backend/api/routes.py`, add client function in `frontend/js/api.js`.

**Adding a new UI action**: Add to `frontend/js/actions.js` (NOT main.js) if components need it. Components import from actions.js.

**Adding state**: Add default in `store.js`, subscribe in the relevant component with key list.

**Changing the extension**: Edit files in `extension/`, then reload in `chrome://extensions/`.

## Gotchas

- `Ctrl+R` conflicts with browser refresh — don't use it as a shortcut.
- `LiveWebLoader` in `cache_manager/utils/web_engine.py` depends on PySide6/Qt — never import it from the web backend.
- The extension needs `activeTab` + `scripting` + `tabs` permissions to capture pages.
- Screenshot browser caching: always use `contentVersion` in screenshot URLs.
- MHTML upload uses Python's `email` module parser, not Qt's WebEngine.
