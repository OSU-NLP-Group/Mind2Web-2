"""Cache the webpages that an agent's answers cite, so that evaluation reads them from disk.

A crawl has two stages for each task:

1. **URL discovery.** Every ``answer_<k>.md`` of the task is scanned with the
   regular expression of :func:`~mind2web2.utils.url_tools.regex_find_urls` and,
   optionally, with LLMs (:class:`LLMUrlExtractor`), which also recover URLs
   written without a scheme or split across lines.  Spellings of one URL that
   normalize to the same form are merged, preferring what the regex found, then
   ``https``, then the shortest spelling.  The result is written to
   ``<cache_root>/<agent>/<task_id>.json``::

       {"agent_name", "task_id", "total_unique_urls",
        "all_unique_urls": [url, ...],                 # case-insensitively sorted
        "urls": {url: [answer file, ...]},             # most-cited first
        "answer_digests": {answer file: sha256},       # the answers the URLs came from
        "url_types": {url: "web" | "pdf"},             # filled in after the crawl
        "cached_url_count", "failed_urls": {url: reason}}

   A later crawl reuses this file instead of extracting again while the task's
   answer files are the same files with the same content (``answer_digests``),
   unless asked to refresh it.

2. **Capture.** Each URL is stored in the task's cache
   (``<cache_root>/<agent>/<task_id>/``, a :class:`CacheFileSys`): PDFs are
   downloaded, everything else is captured in the browser.  A capture that fails
   is recorded as a failure in the cache instead of being stored.

All tasks of a crawl share one browser, so ``max_concurrent_pages`` of the
:class:`BatchBrowserManager` bounds the pages open across the whole crawl.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from logging import Logger
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tqdm import tqdm

from .api_tools.tool_pdf import PDFParser, is_pdf
from .llm_client import DEFAULT_JUDGE_MODEL, LLMClient
from .prompts.cache_prompts import llm_extraction_prompts
from .submission import list_answer_files
from .utils.cache_filesys import CacheFileSys
from .utils.page_info_retrieval import BatchBrowserManager
from .utils.url_tools import URLs, normalize_url_simple, regex_find_urls, remove_utm_parameters

#: Models that :class:`LLMUrlExtractor` asks by default.  Two different models
#: miss different URLs, so their union has higher recall than either one.
DEFAULT_URL_MODELS = (DEFAULT_JUDGE_MODEL, "gpt-4.1")

#: What :func:`crawl_one_page` can report for a URL.
OUTCOMES = ("cached", "skipped", "stored", "failed", "blocked", "error")


# --------------------------------------------------------------------------- #
# URL discovery                                                               #
# --------------------------------------------------------------------------- #

class LLMUrlExtractor:
    """Extracts the URLs of an answer with several LLMs and returns their union.

    A model whose request fails contributes nothing (the failure is logged), so
    one unavailable model lowers recall without stopping the crawl.
    """

    def __init__(self, client: LLMClient, models: Sequence[str] = DEFAULT_URL_MODELS,
                 max_concurrent_requests: int = 30):
        self.client = client
        self.models = tuple(models)
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def extract(self, answer_text: str, logger: Logger) -> List[str]:
        results = await asyncio.gather(*(self._extract_with(model, answer_text, logger) for model in self.models))
        return list(dict.fromkeys(url for urls in results for url in urls))

    async def _extract_with(self, model: str, answer_text: str, logger: Logger) -> List[str]:
        try:
            async with self._semaphore:
                result: URLs = await self.client.async_response(
                    model=model,
                    messages=[{"role": "system", "content": llm_extraction_prompts},
                              {"role": "user", "content": answer_text}],
                    response_format=URLs,
                )
            return result.urls or []
        except Exception as exc:
            logger.warning(f"URL extraction with {model} failed: {exc}")
            return []


def filter_url_variants(urls: Iterable[str], priorities: Optional[Dict[str, int]] = None) -> List[str]:
    """Keep one spelling of each URL: one per :func:`normalize_url_simple` form.

    Within a group, the spelling with the lowest ``priorities`` value wins (a URL
    missing from ``priorities`` counts as 1 when ``priorities`` is given), then an
    ``https`` spelling, then the shortest, then the alphabetically first.
    Groups are returned in order of their first spelling in ``urls``.
    """
    groups: Dict[str, List[str]] = {}
    for url in urls:
        groups.setdefault(normalize_url_simple(url), []).append(url)
    lookup = priorities or {}
    default = 1 if priorities else 0
    return [
        min(group, key=lambda u: (lookup.get(u, default), 0 if u.startswith("https://") else 1, len(u), u.lower()))
        for group in groups.values()
    ]


async def extract_answer_urls(answer_text: str, extractor: Optional[LLMUrlExtractor], logger: Logger) -> List[str]:
    """The distinct URLs of one answer: regex matches first, then what the LLMs add."""
    found_by_regex = regex_find_urls(answer_text)
    found_by_llm = await extractor.extract(answer_text, logger) if extractor is not None else []
    priorities = {url: 0 for url in found_by_regex}
    for url in found_by_llm:
        priorities.setdefault(url, 1)
    return filter_url_variants(found_by_regex + found_by_llm, priorities)


def _sorted_ci(items: Iterable[str]) -> List[str]:
    return sorted(items, key=str.lower)


async def discover_task_urls(
        agent: str,
        task_id: str,
        *,
        answers_root: Path,
        cache_root: Path,
        extractor: Optional[LLMUrlExtractor],
        logger: Logger,
        refresh: bool = False,
) -> List[str]:
    """Return the task's URLs, from its metadata file or by extracting them from its answers.

    The metadata file is reused when it was written for the answer files the
    task has now, with the same content.  Otherwise (the answers changed, or
    the file is missing or unreadable), or with ``refresh``, the answers are
    scanned again and the file is rewritten.  See the module docstring for the
    file's layout.
    """
    meta_path = cache_root / agent / f"{task_id}.json"
    answers = list_answer_files(answers_root / agent / task_id)
    texts = [a.path.read_text(encoding="utf-8") for a in answers]
    digests = {a.path.name: hashlib.sha256(text.encode("utf-8")).hexdigest() for a, text in zip(answers, texts)}
    if meta_path.exists() and not refresh:
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except ValueError:
            meta = {}
        if meta.get("answer_digests") == digests:
            return meta["all_unique_urls"]
        logger.info(f"[{agent}/{task_id}] {meta_path.name} does not match the current answers; "
                    f"extracting their URLs again")

    per_answer = await asyncio.gather(*(extract_answer_urls(text, extractor, logger) for text in texts))

    sources: Dict[str, List[str]] = {}
    for answer, urls in zip(answers, per_answer):
        for url in urls:
            sources.setdefault(url, []).append(answer.path.name)
    # Different answers may spell the same page differently: keep one spelling per task.
    representative = {normalize_url_simple(url): url for url in filter_url_variants(sources)}
    merged: Dict[str, set] = {}
    for url, files in sources.items():
        merged.setdefault(representative[normalize_url_simple(url)], set()).update(files)

    by_citations = sorted(merged.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    all_urls = _sorted_ci(merged)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "agent_name": agent,
        "task_id": task_id,
        "total_unique_urls": len(all_urls),
        "all_unique_urls": all_urls,
        "urls": {url: _sorted_ci(files) for url, files in by_citations},
        "answer_digests": digests,
        "url_types": {},
    }, ensure_ascii=False, indent=2), "utf-8")
    logger.info(f"[{agent}/{task_id}] {len(all_urls)} URLs in {len(answers)} answers -> {meta_path}")
    return all_urls


# --------------------------------------------------------------------------- #
# Capture                                                                     #
# --------------------------------------------------------------------------- #

async def crawl_one_page(
        url: str,
        cache: CacheFileSys,
        pdf_parser: PDFParser,
        browser_manager: BatchBrowserManager,
        logger: Logger,
        retry_failed: bool = False,
) -> str:
    """Cache one page: download it if it is a PDF, otherwise capture it in the shared browser.

    URLs that are already cached are skipped, and so are URLs with a failure
    record unless ``retry_failed``.  A failed capture is recorded in the
    cache.  Every step has its own time limit, so no URL can stall the crawl.

    Returns what happened: ``"cached"`` (already cached), ``"skipped"`` (a
    failure record, not retried), ``"stored"``, ``"failed"``, ``"blocked"``
    (the site refused the browser), or ``"error"`` (an unexpected exception,
    which is logged).
    """
    try:
        if cache.has(url):
            return "cached"
        if not retry_failed and cache.failure(url) is not None:
            return "skipped"
        url = remove_utm_parameters(url)
        logger.info(f"Crawling {url}")
        if await is_pdf(url):
            await asyncio.sleep(0.2 * random.random())
            pdf_bytes = await pdf_parser.fetch(url)
            if pdf_bytes is not None:
                await asyncio.to_thread(cache.put_pdf, url, pdf_bytes)
                return "stored"
            logger.info(f"{url} did not return a PDF; loading it in the browser")

        capture = await browser_manager.capture(url, logger)
        if capture.ok:
            await asyncio.to_thread(cache.put_web, url, capture.text, capture.screenshot_b64)
            return "stored"
        logger.warning(f"Could not capture {url}: {capture.error}")
        cache.record_failure(url, capture.error, blocked=capture.blocked)
        return "blocked" if capture.blocked else "failed"
    except Exception:
        logger.error(f"Error crawling {url}", exc_info=True)
        return "error"


@dataclass
class TaskCrawl:
    """What a crawl did with one task: its URL count and how many URLs ended in each outcome."""

    task_id: str
    urls: int = 0
    outcomes: Counter = field(default_factory=Counter)
    error: Optional[str] = None  # set when URL discovery for the task failed


async def cache_answers(
        agent: str,
        task_ids: Sequence[str],
        *,
        answers_root: Path,
        cache_root: Path,
        browser: BatchBrowserManager,
        extractor: Optional[LLMUrlExtractor],
        logger: Logger,
        retry_failed: bool = False,
        refresh_urls: bool = False,
        show_progress: bool = True,
) -> List[TaskCrawl]:
    """Discover and capture the URLs of ``task_ids`` for ``agent``, all through ``browser``.

    URLs of every task are queued together, so the browser's page limit is the
    only bound on concurrent captures.  After the pass over all URLs, the URLs
    that failed in this crawl for a reason other than a refusal are retried
    once: such failures are often transient (a slow or overloaded site), while
    a refusal needs a person with a browser.  Finally each task's metadata file
    gains the content type of every cached URL and the reason of every failure.
    """
    reports = {task_id: TaskCrawl(task_id) for task_id in task_ids}

    async def discover(task_id: str) -> Tuple[str, List[str]]:
        try:
            urls = await discover_task_urls(agent, task_id, answers_root=answers_root, cache_root=cache_root,
                                            extractor=extractor, logger=logger, refresh=refresh_urls)
        except Exception as exc:
            logger.error(f"[{agent}/{task_id}] URL discovery failed", exc_info=True)
            reports[task_id].error = f"URL discovery failed: {type(exc).__name__}: {exc}"
            urls = []
        return task_id, urls

    task_urls = dict(await asyncio.gather(*(discover(task_id) for task_id in task_ids)))
    caches = {task_id: CacheFileSys(str(cache_root / agent / task_id)) for task_id, urls in task_urls.items() if urls}
    pdf_parser = PDFParser()
    outcomes: Dict[Tuple[str, str], str] = {}

    async def crawl_all(jobs: List[Tuple[str, str]], retry: bool, desc: str) -> None:
        with tqdm(total=len(jobs), desc=desc, unit="url", ncols=80, disable=not show_progress) as bar:
            async def crawl(job: Tuple[str, str]) -> None:
                task_id, url = job
                outcomes[job] = await crawl_one_page(url, caches[task_id], pdf_parser, browser, logger,
                                                     retry_failed=retry)
                bar.update(1)

            await asyncio.gather(*(crawl(job) for job in jobs))

    await crawl_all([(t, url) for t, urls in task_urls.items() for url in urls], retry_failed, "Crawling")
    transient = [job for job, outcome in outcomes.items() if outcome == "failed"]
    if transient:
        logger.info(f"Retrying {len(transient)} URLs whose capture failed in this crawl")
        await crawl_all(transient, True, "Retrying")

    for (task_id, _), outcome in outcomes.items():
        reports[task_id].outcomes[outcome] += 1
    for task_id, urls in task_urls.items():
        reports[task_id].urls = len(urls)
        if urls:
            _record_crawl_results(cache_root / agent / f"{task_id}.json", caches[task_id], urls, logger)
    return [reports[task_id] for task_id in task_ids]


def _record_crawl_results(meta_path: Path, cache: CacheFileSys, urls: List[str], logger: Logger) -> None:
    """Add the content type of each cached URL and the reason of each failed one to the metadata file."""
    url_types: Dict[str, str] = {}
    failed_urls: Dict[str, str] = {}
    for url in urls:
        content_type = cache.has(url)
        if content_type:
            url_types[url] = content_type
        elif (failure := cache.failure(url)) is not None:
            failed_urls[url] = failure["reason"]
    meta = json.loads(meta_path.read_text("utf-8"))
    meta.update({"url_types": url_types, "cached_url_count": len(url_types), "failed_urls": failed_urls})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if failed_urls:
        logger.warning(f"{len(failed_urls)} of {len(urls)} URLs in {meta_path.stem} could not be captured; "
                       f"see failures.json in {cache.task_dir}")
