# Cache Manager Web

A browser-based tool for reviewing and batch-fixing cached webpages used in [Mind2Web 2](https://github.com/OSU-NLP-Group/Mind2Web-2) evaluation. Paired with a Chrome Extension, it can **automatically recapture hundreds of broken pages** (CAPTCHA walls, access denied, empty pages, etc.) with minimal manual effort.

<div align="center">
  <img src="../assets/cache_manager_ui.png" alt="Cache Manager Web UI" width="900"/>
  <p><em>Three-panel layout: tasks (left), URLs (center), and content preview (right) with issue detection and batch recapture.</em></p>
</div>

## Getting Started

### 1. Start the Server

```bash
# By agent name (looks up <cache-dir>/<agent_name>; --cache-dir defaults to cache/)
uv run python3 cache_manager_web/run.py <agent_name>

# By full path
uv run python3 cache_manager_web/run.py /path/to/cache/folder

# Options
uv run python3 cache_manager_web/run.py <agent_name> --port 8000 --no-browser
uv run python3 cache_manager_web/run.py <agent_name> --cache-dir /data/cache --answers-dir /data/answers
```

The web UI opens automatically in your browser. The Answer view reads `<answers-dir>/<agent_name>/<task_id>/answer_*.md`; without `--answers-dir`, it uses the `answers` directory next to the cache directory.

The server has no authentication, so it answers only requests addressed to `127.0.0.1`, `localhost`, or `::1`, and it refuses requests that change data when they come from any web page other than the Cache Manager itself (the Chrome extension is allowed). Pages you open while recapturing therefore cannot read or change the cache. `--host` binds another interface and serves that host name too; a wildcard such as `0.0.0.0` serves every interface, reached by this machine's IP address (other host names are refused), and anyone who can reach the port can use it, so use it only on a trusted network.

### 2. Install the Chrome Extension

1. Open `chrome://extensions` and enable **Developer mode**
2. Click **Load unpacked** and select the `cache_manager_web/extension/` folder
3. Pin the extension icon for easy access
4. If the Cache Manager does not run at `http://127.0.0.1:8000` (for example, with another `--port`), open the extension popup, expand **Settings**, and enter the address of the Cache Manager page

After pulling a new version of the extension, click its reload button in `chrome://extensions`.

A capture stores the page the way the crawler does. The page is laid out 1,100 CSS pixels wide, as in the crawler's browser window, and scrolled to the end and back to load content that appears on scrolling. The extension then stores a screenshot of the whole page and the page's HTML, which the Cache Manager converts to text with the crawler's converter. For the full-page screenshot, the extension briefly attaches Chrome's debugger to the tab, so Chrome shows a "Cache Manager Capture started debugging this browser" bar during each capture. If the full-page screenshot fails, for example because **Cancel** was clicked on that bar during the capture, the debugger did not answer in time, or a policy blocks the debugger, the extension stores a screenshot of only the visible part of the tab, and the Cache Manager shows a warning so that you can capture the page again. Starting Chrome with `--silent-debugger-extension-api` hides the bar.

### 3. Review & Fix

1. **Browse tasks** — select a task from the left panel to see its URLs
2. **Check issues** — red = definite issue, yellow = possible issue, green = reviewed OK. URLs the crawler could not capture are listed as `failed`, with the reason; URLs you added or reset are listed as `pending` (not captured yet). Both are definite issues, as are flagged URLs, pages with no text, and short pages (under 3,000 characters) with bot-check or access-denied wording. Longer pages with such wording are only possible issues, since articles may quote it. Capturing a URL with the extension (or uploading a PDF or MHTML file) stores its page under the listed URL and clears its failure record and flag
3. **Navigate quickly** — use `j`/`k` to move between URLs, `n`/`N` to jump across issues in all tasks
4. **Preview** — toggle between screenshot (`1`), extracted text (`2`), and agent answer (`3`) views

## Batch Recapture (One-Click Fix)

The most powerful feature — fix all broken pages at once:

1. Click **Batch Recapture** in the toolbar (queues every unreviewed red URL that is not a stored PDF)
2. Click the Chrome Extension icon → **Start Batch (auto)** or **Start Batch (pause on CAPTCHA)**
3. The extension automatically opens each URL, waits for it to load, captures the page, and advances to the next one
4. If a CAPTCHA is detected (Cloudflare, reCAPTCHA, hCaptcha, etc.), the auto mode captures the page anyway and moves on; the pause mode waits for you to solve it, then continues
5. Pages with very short content are auto-retried (up to 2 times)
6. After batch completes, review the recaptured URLs (shown in blue) and press `r` to confirm each

Only a capture of the URL the batch is waiting for advances the batch; capturing or uploading another URL by hand while a batch runs leaves the queue as it is. If the batch tab is closed or the extension is reloaded, the batch stays queued, and starting it again from the popup resumes it at that URL.

Opening the Cache Manager page again, in the same tab or another one, and clicking **Refresh** keep a running batch; opening another cache folder stops it.

## Single-Page Recapture

For pages that need manual intervention (login walls, complex anti-bot):

1. Select the URL and click **Recapture Live** (or press `u`)
2. The page opens in a new browser tab — solve any CAPTCHA or login
3. Click the extension icon → **Capture This Page**
4. The UI updates instantly via SSE

## URL Management

- **Flag** (`f`) — mark a URL for recapture (red). Its stored page is kept, and evaluation keeps using it, until a capture replaces it
- **Reset** (`x`) — delete the URL's stored page (or its failure record); it is listed as `pending` until captured again. Asks first
- **Edit** (`e`) — change the URL link. A stored page moves to the new URL, with its flag and review status; a failed or pending URL leaves the new URL `pending`
- **Add** (`a`) — add a URL the crawl missed; it is listed as `pending` until you capture it or upload a file. A URL the task already lists, in any spelling, is refused
- **Delete** (`d`) — remove a URL: its stored page, failure record, flag, and review status, and every pending spelling of its page. Asks first when a page or failure record is stored
- **Upload** — drag-and-drop `.pdf` or `.mhtml` files onto the preview panel. A file that is not a PDF, or an MHTML file without text, is refused

### What evaluation sees

Evaluation reads only the stored pages (`index.json` and their files) and the failure records (`failures.json`) of each task: a stored page is used as it is, a URL with a failure record counts as unavailable, and any other URL is captured live. The Cache Manager's own state, pending URLs (`pending.json`), flags (`flags.json`), and review statuses (`reviewed.json`), is never read by evaluation, and no Cache Manager action stores content that was not captured. So a flagged page is still evaluated from its stored content, and a `pending` URL is captured live until you capture it.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `↓` | Next URL |
| `k` / `↑` | Previous URL |
| `n` | Next issue (cross-task) |
| `N` | Previous issue (cross-task) |
| `r` | Mark as reviewed |
| `f` | Flag for recapture |
| `d` | Delete URL (asks first if a page or failure record is stored) |
| `x` | Reset: delete the stored page or failure record (asks first) |
| `e` | Edit URL |
| `a` | Add new URL |
| `o` | Open in browser |
| `u` | Recapture live |
| `1` / `2` / `3` | Screenshot / Text / Answer view |
| `Space` | Toggle Screenshot / Text |
| `?` | Full help & usage guide |

## Recommended Workflow

1. Run `uv run mind2web2 cache <agent>` to pre-cache all URLs
2. Start the Cache Manager and review the issue count per task
3. Use `n` to jump through flagged issues — quick-check screenshot vs text
4. Click **Batch Recapture** to auto-fix all red URLs at once
5. After batch, review blue URLs and press `r` to confirm each is fixed
6. For remaining stubborn pages, use **Recapture Live** one by one
7. For PDF pages misrecorded as web, use **Upload PDF** or drag-and-drop

## URL Color Legend

| Color | Meaning |
|-------|---------|
| Red | Definite issue — needs recapture |
| Yellow | Possible issue — check manually |
| Blue | Batch-recaptured — needs human review |
| Green | Reviewed OK / Fixed |
| Grey | No issues detected |
