# Cache Manager Web

Web-based tool for reviewing and fixing cached web pages used by Mind2Web agents. Uses a browser-based UI + Chrome Extension.

## Architecture

```
cache_manager_web/
├── run.py                     # Entry point: starts FastAPI server, auto-opens browser
├── backend/
│   ├── app.py                 # FastAPI app, lifespan, LocalRequestGuard, FrameGuard, BodySizeGuard, static file serving
│   ├── config.py              # Constants (paths, size limits of captures and uploads)
│   ├── api/routes.py          # ALL API endpoints + SSE + MHTML parsing
│   └── models/
│       ├── cache_manager.py   # CacheManager — reads/writes cache directory structure
│       └── keyword_detector.py # KeywordDetector — scans text for issue keywords
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
    ├── settings.js            # Backend URL setting (chrome.storage.local), used by the popup and the service worker
    ├── background.js          # Service worker: capture (full-page screenshot via chrome.debugger), batch mode, CAPTCHA detection
    └── popup.html/js          # Extension popup UI with batch progress display and the backend URL setting
```

## Key Design Decisions

- **Evaluation reads only captured content**: evaluation uses a task's stored pages (`index.json`) and failure records (`failures.json`). The Cache Manager's own state is `pending.json` (URLs to capture that have neither a stored page nor a failure record, listed as `pending`, "not captured yet"), `flags.json` (stored pages that need a recapture), and `reviewed.json` (review statuses), which evaluation never reads. No action stores content that was not captured: Flag writes only `flags.json`; Reset deletes the stored page or failure record and makes the URL pending; Add URL only makes it pending; an MHTML upload without text, a PDF upload without the `%PDF-` signature, and a rename whose stored page cannot be read are refused. A flag on a URL with neither a stored page nor a failure record has no effect.
- **Review-state files are changed in place, never overwritten from memory**: a `CacheManager` answers lookups from the copy of `pending.json` and `flags.json` it read when loading (`reviewed.json` is read on every lookup), but every change re-reads the file, applies the change, and replaces the file atomically (`_write_atomic`) under a lock shared by all managers of the process and under the task's cache lock (`CacheFileSys.exclusive()`, the `flock` on the task directory that the cache's own writes take). The manager serving requests and the one a reload is building, and two Cache Manager servers on one folder, therefore keep each other's changes. A review-state file that cannot be read is never overwritten: its task is not loaded, and a change raises `ReviewStateError` (the API answers 500 with its message). Loading a task drops pending URLs that another process (e.g. evaluation) has stored a page or recorded a failure for; Delete and Reset drop every pending spelling of the page (the same `page_form`).
- **One entry per page, whatever the spelling**: `CacheManager.canonical_url()` maps a URL to the URL the task lists for its page (the stored page's URL from `CacheFileSys.lookup(ignore_case=False)`, else the failure record's URL from `failure_url(ignore_case=False)`, since letter case is never disregarded, as in the crawler; else a pending URL with the same `page_form`, the case-preserving form the crawler also groups spellings by, so pending URLs that differ in letter case are separate entries, as they are separate pages to the crawler), and every edit and every review-state file uses that URL. A page is stored under it, so a capture of another spelling, or of the URL after a redirect, updates the listed entry; `store_page()` moves the flag and review status when the cache stores the page under a different spelling (e.g. without a trailing slash), and Add answers 409 for another spelling of a listed page.
- **Request guard, no CORS**: the API has no authentication, and the reviewer's browser opens arbitrary pages while recapturing. `LocalRequestGuard` (app.py) answers only `Host` names in `CM_ALLOWED_HOSTS` (default: the loopback names; `run.py --host` extends it, and a wildcard bind adds `*`, which admits any IP address but no other name, so DNS rebinding is refused) and refuses requests other than GET, HEAD, and OPTIONS whose `Origin` is neither the app's own origin nor a `chrome-extension://` origin. No CORS headers are sent; the extension does not need them because of its `host_permissions`. `FrameGuard` adds `frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN` to every response, so other sites cannot frame the UI. Answers, written by the agents under review, are rendered with a pinned `marked` and sanitized with `DOMPurify` (both loaded with SRI hashes), which also removes `<style>` elements, `style` attributes, forms, and form controls (`ANSWER_SANITIZER` in preview.js), so an answer can neither lay content over the UI nor post a form to the API from the UI's own origin. Only http(s) URLs are opened in a tab (`isOpenable()` in main.js), without `window.opener`.
- **Captures and uploads are checked before anything is stored**: `/api/capture`, `/api/upload-pdf`, and `/api/upload-mhtml` answer 400 unless `url` is an http(s) URL the cache can store (`_valid_url()`), and `/api/capture` ignores an `actual_url` that is not one or whose host is the server's own (`_redirect_url()`), so a capture never adds the Cache Manager's address to a task. `BodySizeGuard` (app.py) refuses a request to these routes that does not declare its size in `Content-Length` (411) or declares more than `config.max_request_size()` (413) before its body is read; the routes then check each field: a decoded screenshot against `MAX_SCREENSHOT_SIZE` (100 MiB, above the 88 MB of an incompressible RGBA PNG of 1100x20000 pixels), the HTML or text against `MAX_TEXT_SIZE` (50 MiB), and an uploaded file against `MAX_UPLOAD_SIZE` (200 MiB), each with 413.
- **A capture is marked done only when it has content**: a capture sets the review status `"fixed"` (by hand) or `"recaptured"` (batch) and clears the flag, except that a capture by hand whose stored text is empty keeps its flag and gets no review status (empty text is a definite issue, so the URL stays red and a batch still queues it), and a `visible_part_only` capture is flagged. Both answer, and send in the `capture_complete` event, a `warning` that the UI shows; the extension then keeps the tab open. `POST /api/review` accepts only `"ok"` and `""` (clear), for URLs the task lists (404 otherwise).
- **Issue severity**: definite for pending, failed, and flagged URLs, empty text, and pages shorter than `SHORT_PAGE_CHARS` (3,000 characters, shared with the crawler's `detect_block()`) that match a definite keyword or pattern or `detect_block()`; possible for other keyword matches. `_issue_entry()` in routes.py computes a URL's issues; every edit recomputes the entries of the URLs it changed, and drops those of URLs no longer listed, with `_refresh_issues()`.
- **Load and scan off the event loop**: `/api/load` reads and scans the folder in a worker thread into a new `CacheManager`, which replaces the current one only when complete; loads run one at a time. The UI loads the loaded folder again on every page open and on Refresh, so such a reload keeps a running batch and the capture target; the tasks the current manager changed while the folder was read (`CacheManager.tasks_changed_since()`) are read and scanned again before the swap. Loading another folder stops a running batch and clears the capture target.
- **No build step**: Vanilla JS with ES modules. Files are served directly by FastAPI's StaticFiles.
- **No circular imports**: Components import shared actions from `actions.js`, NOT from `main.js`. This is critical — `main.js` imports components, so components must not import from `main.js`.
- **Selective state subscriptions**: `subscribe(fn, ['key1', 'key2'])` — components only re-render when their relevant keys change.
- **Chrome Extension for capture**: Uses a real browser session (not Playwright/Selenium) so it works on Cloudflare-protected and anti-bot pages.
- **Extension captures as the crawler does**: `captureFullPage()` in background.js repeats the crawler's capture steps through `chrome.debugger`. The page is laid out in a 1100×750 viewport (the middle of the crawler's window sizes) and scrolled to the end three times and back to the top (the crawler presses End and Home); the viewport is then resized to the page's content height, at most 6000 CSS pixels, and after 750 ms the page's `outerHTML` is read, the tab is brought to the front (`Page.bringToFront`, since Chrome may not render a background tab), and the screenshot is taken with `captureBeyondViewport`, which covers the whole page. `/api/capture` converts the HTML with the crawler's `html_to_markdown()`. Every DevTools command, every content script (including the PDF, CAPTCHA, and page-length checks), and the fallback screenshot has a timeout. When the full-page screenshot fails, the extension scrolls the same way and sends a `captureVisibleTab` screenshot (the visible part only) with `visible_part_only`; `/api/capture` flags such a page and clears its review status, so it stays a definite issue until a full-page capture, and the `capture_complete` event carries a warning that the UI shows. `captureVisiblePart()` makes the tab active again and keeps the screenshot only if the tab is the active tab of its window right before and right after it is taken; otherwise the capture fails. The backend URL is a setting in the popup (`settings.js`, default `http://127.0.0.1:8000`).
- **A capture by hand is for the URL selected in the UI, and is refused when that cannot be right**: the shortcut (Alt+Shift+C) and the popup's Capture This Page store the tab for the capture target, which the UI sets whenever a URL is selected. `capturePage()` refuses, storing nothing, a capture of the Cache Manager's own page (`isCacheManagerUrl()`), and a capture of a tab whose site differs from the target's (`sameSite()`: equal hosts ignoring `www.`, or one a subdomain of the other); the popup then shows why and offers Capture anyway, for a page that redirected to another site. A host comparison is used because the extension cannot tell which tab the UI opened for the target (it opens tabs without `window.opener`), and a tab of the same site as the target is almost always the target or its redirect. On the batch tab during a batch, a capture by hand never uses the capture target: while the batch waits for a CAPTCHA to be solved (pause mode), it is the batch's capture of its head (`captureBatchTabByHand()`, which stops CAPTCHA polling and runs `autoCaptureAndAdvance()`), and in any other state it is refused, since the batch captures that tab itself.
- **SSE for real-time updates**: When the extension captures a page, the frontend updates instantly.
- **contentVersion cache busting**: Screenshot URLs include `&v={contentVersion}` to force browser to re-fetch after capture.
- **MHTML parsing without Qt**: Uses Python's `email` module to parse MHTML (MIME format).

## Running

```bash
uv run python3 cache_manager_web/run.py zhoukai              # Agent name, under --cache-dir (default: cache/)
uv run python3 cache_manager_web/run.py /path/to/cache/folder # Full path
# Options: --cache-dir DIR  --answers-dir DIR  --port 8000  --host 127.0.0.1  --no-browser
```

`run.py` passes its settings to the app through environment variables, read when uvicorn imports it: `CM_INITIAL_CACHE_FOLDER`, `CM_ANSWERS_DIR`, and `CM_ALLOWED_HOSTS`. Tests must reach the app as `TestClient(app, base_url="http://127.0.0.1:8000")`, since the guard refuses TestClient's default host.

## Package Management

This project uses `uv`, not pip. Use `uv run`, `uv sync`, `uv add`.

## API Endpoints (routes.py)

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/load | Load cache folder and scan every URL for issues (in a worker thread); loading another folder stops a running batch |
| GET | /api/status | Current load status |
| GET | /api/tasks | Task list with summaries |
| GET | /api/tasks/{id}/urls | URLs (stored pages, then `failed` URLs with their failure record, then `pending` URLs), issues, reviewed status |
| GET | /api/issues | Issue index and per-task issue summary, from the issue cache (no rescan) |
| GET | /api/content/{id}/text | Text content + issues |
| GET | /api/content/{id}/screenshot | Screenshot JPEG |
| GET | /api/content/{id}/pdf | PDF content |
| POST/GET | /api/capture/target | Active capture target for extension |
| POST | /api/capture/batch/start | Start batch capture with URL queue |
| GET | /api/capture/batch/status | Batch progress (polled by extension) |
| POST | /api/capture/batch/skip | Skip the URL the batch waits for (on failure); names `task_id` and `url`, 409 when the batch no longer waits for it |
| POST | /api/capture/batch/stop | Stop batch capture |
| POST | /api/capture/batch/captcha | CAPTCHA detected notification |
| POST | /api/capture | Receive capture from extension (`url` must be http(s), else 400; `actual_url` is ignored unless http(s) on another host than the server's; 413 above the size limits; `html` is converted with `html_to_markdown()`; `text` is stored as is when no HTML is sent; `visible_part_only` flags the page, and a capture by hand with empty text is left unreviewed, both with a `warning`; with `batch`, a batch capture: stored only while the batch waits for `url`, else 409 and nothing stored) |
| POST | /api/flag/{id} | Flag URL for recapture (flags.json only; the stored page is kept) |
| POST | /api/reset/{id} | Delete the stored page or failure record; the URL becomes `pending` |
| POST | /api/review/{id} | Set review status `"ok"`, or clear it with `""` (400 for any other status; 404 for a task or URL not listed) |
| GET | /api/review-progress | Overall progress |
| GET | /api/answers/{id} | Answer markdown files |
| POST | /api/urls/{id} | Add URL to task as `pending` (409 if the task already lists its page, in any spelling) |
| POST | /api/urls/{id}/rename | Rename/edit URL link (moves a stored page with its flag and review status; a failed or pending URL leaves the new URL `pending`); returns the new URL as listed |
| DELETE | /api/urls/{id} | Delete URL: stored page, failure record, pending entries (every spelling of the page), flag, review status |
| POST | /api/upload-mhtml/{id} | Upload MHTML: its text, with a 1x1 placeholder screenshot (422 if it has no text; 400 unless `url` is http(s); 413 above `MAX_UPLOAD_SIZE`) |
| POST | /api/upload-pdf/{id} | Upload PDF (replaces content, switches type; 422 without the `%PDF-` signature; 400 unless `url` is http(s); 413 above `MAX_UPLOAD_SIZE`; `batch` query parameter as for `/api/capture`) |
| GET | /api/events | SSE stream |

## State Store (store.js)

Key state fields:
- `loaded`, `agentName`, `agentPath`, `stats` — cache status
- `tasks`, `taskIssues`, `selectedTaskId` — task list
- `urls`, `selectedUrl`, `urlTotal`, `urlReviewedCount` — URL list
- `previewMode`, `currentText`, `currentIssues`, `answers` — preview
- `issueIndex`, `issueCursor` — cross-task issue navigation
- `contentVersion` — incremented on capture to bust screenshot cache
- `batchActive`, `batchTotal`, `batchCompleted` — batch capture state
- `fitToWidth`, `zoomLevel` — screenshot zoom

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `↓` | Next URL |
| `k` / `↑` | Previous URL |
| `n` | Next issue (cross-task) |
| `N` | Previous issue (cross-task) |
| `r` / `Ctrl+Enter` | Mark as reviewed |
| `f` | Flag for recapture (red) |
| `d` / `Backspace` | Delete URL (asks first if a page or failure record is stored) |
| `x` | Reset: delete the stored page or failure record; the URL becomes pending (asks first) |
| `e` | Edit URL link |
| `a` | Add new URL |
| `o` | Open in browser |
| `u` / `Ctrl+U` | Recapture live |
| `1` / `2` / `3` | Screenshot / Text / Answer view |
| `Space` | Toggle Screenshot / Text |
| `Ctrl+O` | Open cache folder |
| `Ctrl+Wheel` | Zoom screenshot |
| `?` | Show shortcuts help |
| `Escape` | Close shortcuts modal |

## Common Tasks for Contributors

**Adding a new API endpoint**: Add to `backend/api/routes.py`, add client function in `frontend/js/api.js`.

**Adding a new UI action**: Add to `frontend/js/actions.js` (NOT main.js) if components need it. Components import from actions.js.

**Adding state**: Add default in `store.js`, subscribe in the relevant component with key list.

**Changing the extension**: Edit files in `extension/`, then reload in `chrome://extensions/`.

## Review Statuses

| Status | Meaning | Border Color | Counts as Fixed? |
|--------|---------|-------------|-----------------|
| `""` | Not reviewed | grey/yellow/red | No |
| `"ok"` | Reviewed OK (`r`, or viewing a URL without a definite issue that is not `"recaptured"`) | green | Yes |
| `"fixed"` | Captured or uploaded by hand (not set for a capture with empty text) | green | Yes |
| `"skip"` | Skipped; no action of this tool sets it, and it is shown when `reviewed.json` holds it | green | Yes |
| `"recaptured"` | Batch-recaptured, needs human review (confirmed with `r`; never marked `"ok"` by viewing) | blue | No |

## Batch Capture Features

- **Two modes**: Auto (captures CAPTCHA pages and moves on) and Pause-on-CAPTCHA (waits for manual solving)
- **Retry logic**: Pages with body < 200 chars are auto-retried up to 2 times
- **15s timeout**: Force-captures after 15s if page hasn't loaded
- **URL redirect handling**: `actual_url` field saves content for both original and redirected URLs; since the Cache Manager matches URLs in their own letter case, a redirect to a URL that differs in letter case from a listed page is stored as another page, never in its place
- **CAPTCHA detection**: Cloudflare, Turnstile, reCAPTCHA, hCaptcha, generic blocked pages
- **Rich popup UI**: Live progress bar, status badge, current URL, scrollable log
- **Skip on failure**: Failed captures skip and advance to prevent infinite loops
- **One rule for what the batch captures**: a URL qualifies when it is listed, not a stored PDF, a definite issue, and has no review status (`_qualifies()` in routes.py). `batch/start` queues the given URLs that qualify; a queued URL that stops qualifying (captured or uploaded by hand, which sets `"fixed"`; reviewed; renamed; deleted) is left out when it reaches the head (`_skip_urls_that_no_longer_qualify()`), so the batch never replaces a page fixed by hand with what its tab shows. The batch's `completed` is `total - remaining`: captured, skipped, and left out
- **Only a batch capture advances the batch**: the extension sends its captures and PDF uploads with `batch`; the backend stores them only while their URL is the head of the queue, gives them the `"recaptured"` status, and pops the head; otherwise it refuses them with 409 and stores nothing (`_refuse_a_stale_batch_capture()`). A skip names its URL and is checked the same way. Captures made by hand are not checked. The batch does not set the capture target, so a capture by hand in another tab keeps capturing for the URL selected in the UI; on the batch tab itself, a capture by hand is the batch's capture (see "A capture by hand is for the URL selected in the UI" above)
- **A batch capture is for the URL its tab loaded**: the extension remembers the task and URL it sent the batch tab to; if the batch's head differs once the page has loaded, it loads the head without capturing, and counts neither a capture nor a skip
- **Resuming**: a batch that the extension did not finish (the batch tab was closed, or the extension was reloaded) stays queued on the server; Start Batch resumes it at its head. The badge and the popup's progress show the server's `completed`
- **Stopping**: Stop batch in the UI's toolbar (shown while a batch is queued) and Stop in the popup call `/api/capture/batch/stop`; a queued batch that nobody runs otherwise keeps Batch Recapture disabled. The extension's run ends at its next status request, and a capture it sends for the stopped batch gets 409

## Gotchas

- `Ctrl+R` conflicts with browser refresh — don't use it as a shortcut.
- The extension needs `activeTab` + `scripting` + `tabs` + `<all_urls>` permissions for batch mode, `debugger` for full-page screenshots, and `storage` for the backend URL.
- While the extension's debugger is attached, Chrome shows a "started debugging this browser" bar. Clicking Cancel on it detaches the debugger, and that capture falls back to `captureVisibleTab`.
- Screenshot browser caching: always use `contentVersion` in screenshot URLs.
- MHTML upload uses Python's `email` module parser.
- `"recaptured"` status is NOT counted in progress — these URLs still need human review.
- `captureVisibleTab`, the fallback screenshot, captures whichever tab is active in a window, not a specific tab: `captureVisiblePart()` activates the target tab first and discards the screenshot, failing the capture, unless the tab is the active tab of its window both before and after it.
