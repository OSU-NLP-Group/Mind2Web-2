# utils — Shared Utilities

## Modules

### cache_filesys.py — File-Based Webpage Cache
`CacheFileSys`: the cached pages of one task (one instance per task directory).

**Directory layout:**
```
task_dir/
├── index.json      # {storage key: "web"|"pdf"}
├── failures.json   # {storage key: {"reason", "blocked", "attempts", "time"}}
├── <md5(key)>.txt  # web page text (Markdown)
├── <md5(key)>.jpg  # web page screenshot
├── <md5(key)>.pdf  # PDF
```

**Key methods:**
- `put_web(url, text, screenshot)` / `put_pdf(url, pdf_bytes)`: store a page under `storage_key(url)` (fragment removed, percent-decoded, trailing slash removed), replacing any page under the same key whatever its type; return the page's URL as `lookup` returns it.
- `get_web(url)` → `(text, jpeg_bytes)`; `get_pdf(url)` → `pdf_bytes`; `has(url)` → `"web"` | `"pdf"` | `None`
- `lookup(url)` → the URL of the cached page `url` refers to, or `None`; `get_all_urls()` lists these URLs; `remove(url)` deletes a page and its files. A URL from `lookup` or `get_all_urls` passed to any method addresses the same page.
- `record_failure(url, reason, blocked=False)`, `failure(url)`, `failures()`, `clear_failure(url)`: URLs whose capture failed, matched like pages; `failures()` lists them by URL, as `lookup` returns URLs. `blocked` means the site refused the automated browser, so a person may still capture the page. Storing a page clears its URL's failure record, a record is ignored while a page is stored for its URL, and `remove` deletes such ignored records with the page.

**Persistence:** every change is on disk, fsynced, when it returns. Files are replaced atomically, and each change, including deleting the files of a replaced page, runs under an `flock` on the task directory with `index.json` and `failures.json` re-read and merged, so the crawler, an eval run, and the Cache Manager can write to one task at the same time, and an interrupted crawl keeps the pages it stored. There is no separate save step. An `index.json` or `failures.json` that exists but cannot be read raises `CacheIndexError` instead of being treated as empty.

**URL matching** (`lookup`). A key that `storage_key` would change again (a decoded `#` or `%XX`, or a trailing slash from `//`; a "raw" key) is found only through a URL whose storage key is that key, or is raw with the same form once UTM parameters, the scheme, and `www.` are disregarded. Other keys are found by these rules, first hit wins:
1. `url` is the key
2. `normalize_url_simple(url)` is the key
3. the key has the same normalized form (dictionary index; the earliest stored wins)
4. a surface variant of `url` is the key (scheme, `www.`, UTM suffixes, percent-encoding forms, trailing slash). Runs only when 1-3 miss; it finds keys whose normalized form changes under percent-decoding.

### page_info_retrieval.py — Browser Capture
**`BatchBrowserManager`**: one shared Chromium browser (patchright) for concurrent captures.
- `capture(url, logger)` → `Capture`: `screenshot_b64` (PNG) and `text` (the HTML converted to Markdown with `html2text`) on success; otherwise `error`, with `blocked=True` when the site refused the browser.
- At most `max_concurrent_pages` captures run at once. Each attempt gets a fresh browser context and `page_timeout` seconds (default 90), counted from when it gets a slot. Exceptions and timeouts are retried up to `max_retries` attempts in total; a disconnected browser is restarted, and the first attempt of a capture during which the browser disconnected does not count (another page may have crashed it). The context is closed outside the page timeout, so a finished capture is never lost to a slow close.
- Reported as failures without retrying: pages that did not load (DNS or connection errors, a download instead of a page, no response within `navigation_timeout`), refusals (`detect_block`), and HTTP 429 and 5xx error pages. Both apply only to pages with less than 3000 characters of text (`SHORT_PAGE_CHARS`): such a page is a refusal if its status is 401/403/407/999 or it reads like a bot check or access-denied notice, and an error page if its status is 429 or 5xx. A rate limit (429) is therefore an ordinary failure, which the crawler retries at the end of its run, not a refusal. A longer page is captured whatever its status, since some sites send real content with such statuses. A page still loading after `navigation_timeout` is captured as far as it loaded; pages with other statuses, such as 404, are captured as they render.
- Waits up to 15 s for a JavaScript bot check ("Just a moment...") to pass by itself, scrolls to trigger lazy loading, and captures through CDP (a screenshot up to 6000 CSS pixels tall, and `outerHTML`).

### logging_setup.py — Structured Logging
`create_logger(name, log_folder)` creates loggers with multiple handlers:
- **JSONL file**: Machine-readable structured logs
- **Readable file**: Human-readable format with timestamps
- **Console**: Colored structured output (optional)
- **Shared error handler**: Cross-logger error display for concurrent evaluation

Custom formatters:
- `ColoredStructuredFormatter`: Colored console output with op_id/node context
- `HumanReadableFormatter`: File logs with structured field display
- `CompactJsonFormatter`: Compact JSONL for machine parsing

### url_tools.py — URL Normalization & Extraction
- `normalize_url_simple(url)`: The form under which two URLs are the same page for cache lookups (UTM parameters and fragment removed, percent-decoded, trailing slash removed, `https`, no `www.`, lowercased)
- `normalize_url_keep_case(url)`: The same form without the lowercasing, under which the crawler merges spellings of one page; paths that differ in letter case can be different pages
- `remove_utm_parameters(url)`: Strip all `utm_*` query params
- `normalize_url_for_browser(url)`: Ensure URL has protocol for navigation
- `regex_find_urls(text)`: `http(s)://` and `www.` URLs in Markdown or plain text, in order of appearance; keeps balanced parentheses and brackets (Wikipedia titles, `?filter[type]=x`) and `|`, removes Markdown escapes, emphasis delimiters, and trailing punctuation, stops at CJK punctuation
- `URLs` Pydantic model: For LLM structured output of URL lists

### load_eval_script.py — Dynamic Script Loading
`load_eval_script(path)`: Dynamically imports a Python file and returns its `evaluate_answer` coroutine.
- Validates the function exists, is async, and has required parameters
- Uses unique module names to avoid namespace collisions

### misc.py — Small Helpers
- `normalize_url_markdown(url)`: Remove markdown escape chars from URLs
- `text_dedent(str)`: `textwrap.dedent().strip()`
- `encode_image(path)` / `encode_image_buffer(bytes)`: Base64 encoding
- `extract_doc_description(docstring)`: Extract description portion of a docstring
