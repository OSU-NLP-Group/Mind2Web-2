# utils — Shared Utilities

## Modules

### cache_filesys.py — File-Based Webpage Cache
`CacheFileSys`: the cached pages of one task (one instance per task directory).

**Directory layout:**
```
task_dir/
├── index.json      # {storage key: "web"|"pdf"}
├── <md5(key)>.txt  # web page text (Markdown)
├── <md5(key)>.jpg  # web page screenshot
├── <md5(key)>.pdf  # PDF
```

**Key methods:**
- `put_web(url, text, screenshot)` / `put_pdf(url, pdf_bytes)`: store a page under `storage_key(url)` (fragment removed, percent-decoded, trailing slash removed), replacing any page under the same key whatever its type; return the page's URL as `lookup` returns it.
- `get_web(url)` → `(text, jpeg_bytes)`; `get_pdf(url)` → `pdf_bytes`; `has(url)` → `"web"` | `"pdf"` | `None`
- `lookup(url)` → the URL of the cached page `url` refers to, or `None`; `get_all_urls()` lists these URLs; `remove(url)` deletes a page and its files. A URL from `lookup` or `get_all_urls` passed to any method addresses the same page.

**Persistence:** every put and remove is on disk, fsynced, when it returns. Files are replaced atomically, and each change, including deleting the files of a replaced page, runs under an `flock` on the task directory with `index.json` re-read and merged, so the crawler, an eval run, and the Cache Manager can write to one task at the same time, and an interrupted crawl keeps the pages it stored. There is no separate save step. An `index.json` that exists but cannot be read raises `CacheIndexError` instead of being treated as empty.

**URL matching** (`lookup`). A key that `storage_key` would change again (a decoded `#` or `%XX`, or a trailing slash from `//`; a "raw" key) is found only through a URL whose storage key is that key, or is raw with the same form once UTM parameters, the scheme, and `www.` are disregarded. Other keys are found by these rules, first hit wins:
1. `url` is the key
2. `normalize_url_simple(url)` is the key
3. the key has the same normalized form (dictionary index; the earliest stored wins)
4. a surface variant of `url` is the key (scheme, `www.`, UTM suffixes, percent-encoding forms, trailing slash). Runs only when 1-3 miss; it finds keys whose normalized form changes under percent-decoding.

### page_info_retrieval.py — Browser-Based Web Capture
**`BatchBrowserManager`**: Manages a shared Chromium browser (via patchright) for concurrent page capture.
- `capture_page(url, logger)` → `(screenshot_b64, text_content)`
- Uses CDP (Chrome DevTools Protocol) for efficient screenshot + HTML capture
- Converts HTML to markdown via `html2text`
- Scrolls pages to trigger lazy-loaded content
- Auto-restarts browser on crashes
- Concurrency controlled by internal semaphore (`max_concurrent_pages`)

**`PageManager`**: Manages active pages within a browser context, handles page close/crash/navigation events.

### path_config.py — Centralized Path Management
`PathConfig` dataclass holding all project-relative directories:
- `project_root`, `answers_root`, `eval_scripts_root`, `eval_results_root`, `cache_root`
- `default_script_for(task_id)` → `eval_scripts/<version>/<task_id>.py`
- `apply_overrides()`: Override any path via CLI args

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
- `normalize_url_simple(url)`: The form under which two URLs are the same page, for cache lookups and crawl deduplication (UTM parameters and fragment removed, percent-decoded, trailing slash removed, `https`, no `www.`, lowercased)
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
