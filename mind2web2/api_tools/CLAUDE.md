# api_tools — External API Integrations

Tools for interacting with external services during evaluation. These are used by eval scripts that need data beyond what's in the cached webpages.

## Modules

### tool_arxiv.py — ArXiv Paper Lookup
`ArxivTool` wraps the `arxiv` Python library:
- `is_arxiv_pdf_link(link)`: Check if URL is an arXiv PDF
- `get_arxiv_id_from_pdf_link(link)`: Extract arXiv ID from URL
- `search_arxiv_by_id(id)`: Async search by arXiv ID
- `search_arxiv_by_title(title)`: Async search by title
- Uses `asyncio.to_thread()` for the synchronous arxiv library

### tool_googlemap.py — Google Maps Geocoding & Routing
`GoogleMapsTool` wraps the `googlemaps` Python client:
- `get_city_name(address, level)`: Geocode address to city/sublocality name
- `get_address_information(address)`: Full geocoding result
- `calculate_distance(addr1, addr2, mode)`: Driving/walking/transit distance in meters
- `calculate_travel_time(addr1, addr2, mode)`: Travel time in seconds
- Requires `GOOGLE_MAPS_API_KEY` env var

### tool_pdf.py — PDF Detection, Download & Parsing
All network calls are asynchronous (`httpx`), bounded in time, and never block the event loop.

**`is_pdf(url)`**: true when the URL looks like a PDF (`.pdf` suffix and path or query patterns such as `arxiv.org/pdf/`; no network). Otherwise one streamed GET, bounded by 10 s, checks for a PDF `Content-Type` or the `%PDF-` signature in the first KiB. Network errors and timeouts count as "not a PDF".

**`PDFParser`**:
- `fetch(url)` → the PDF bytes, or `None` unless the response body starts with `%PDF-`, so that an HTML page behind a PDF-looking URL is loaded in the browser instead. arXiv URLs are retried on `export.arxiv.org`. A download is abandoned after 60 s or beyond 100 MB.
- `extract(source)`: a URL, file path, or bytes → `(images_b64_list, text)`, or `(None, None)` when the PDF cannot be obtained or parsed. Renders up to 50 pages as JPEG and extracts the text of up to 100 pages with PyMuPDF, in a worker thread.

## Usage Context
These tools are primarily used in:
- `eval_toolkit.py`'s `BaseEvaluator.get_page_info()` for PDF detection + parsing
- `batch_answer_cache.py` for pre-caching PDF content
- Individual eval scripts that need Google Maps or arXiv data
