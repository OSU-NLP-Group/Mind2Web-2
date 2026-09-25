"""Cache the webpages that an agent's answers cite, so that evaluation reads them from disk.

A crawl has two stages for each task:

1. **URL discovery.** Every ``answer_<k>.md`` of the task is scanned with the
   regular expression of :func:`~mind2web2.utils.url_tools.regex_find_urls` and,
   optionally, with LLMs (:class:`LLMUrlExtractor`), which also recover URLs
   written without a scheme or split across lines.  The spellings that the
   task's answers use for one page are grouped (:func:`group_url_variants`):
   spellings that differ only in scheme, ``www.``, a trailing slash, the
   fragment, UTM parameters, or percent-encoding.  Spellings that differ in
   letter case are never grouped, since a server may serve different pages for
   them, and a spelling with an encoded ``#`` or ``%`` (``?q=C%23``) is grouped
   only with spellings whose storage keys the cache matches to its own (keys
   that differ at most in scheme, ``www.``, and UTM parameters), since decoding
   it can give another page's URL (``?q=C``).  Each group is listed once, under its
   preferred spelling: one that the regular expression found in any of the
   answers, then an ``https`` one, then the shortest.  The result is written to ``<cache_root>/<agent>/<task_id>.json``::

       {"agent_name", "task_id", "total_unique_urls",
        "all_unique_urls": [url, ...],                 # case-insensitively sorted
        "urls": {url: [answer file, ...]},             # answers citing any spelling of url; most-cited first
        "url_variants": {url: [spelling, ...]},        # url's other spellings, in order of preference
        "answer_digests": {answer file: sha256},       # the answers the URLs came from
        "url_models": [model, ...],                    # the LLMs that extracted URLs; [] for regex only
        "url_extraction_complete": bool,               # false if an LLM request failed
        "url_types": {url: "web" | "pdf"},             # filled in after the crawl
        "cached_url_count", "failed_urls": {url: reason}}

   ``url_variants`` lists only the URLs that have other spellings.  A later
   crawl reuses this file instead of extracting again while the task's answer
   files are the same files with the same content (``answer_digests``), the URL
   models are the same, the file has ``url_variants``, and the extraction was
   complete, unless asked to refresh it.  An incomplete list is still written,
   so that it can be reviewed, and is used for the crawl that wrote it.  A task
   without answer files has no URLs, and its file is left as it is.

2. **Capture.** Each URL is stored in the task's cache
   (``<cache_root>/<agent>/<task_id>/``, a :class:`CacheFileSys`): PDFs are
   downloaded, everything else is captured in the browser.  When the capture of
   a URL fails, its other spellings are tried in order, and the first one
   captured is stored under its own spelling.  The spellings of a group share
   the normalized form by which the cache's lookup matches URLs, so the page
   is found under each of them.  A URL none of whose spellings can be captured
   is recorded as a failure in the cache instead of being stored.

All tasks of a crawl share one browser, so ``max_concurrent_pages`` of the
:class:`BatchBrowserManager` bounds the pages open across the whole crawl, and
at most ``max_concurrent_urls`` URLs are processed at once, which bounds the
PDF checks and downloads that run outside the browser.
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
from typing import Collection, Dict, Iterable, List, Optional, Sequence, Tuple

from tqdm import tqdm

from .api_tools.tool_pdf import PDFParser, is_pdf
from .llm_client import DEFAULT_JUDGE_MODEL, LLMClient
from .prompts.cache_prompts import llm_extraction_prompts
from .submission import list_answer_files
from .utils.cache_filesys import CacheFileSys, _raw_form, storage_key
from .utils.page_info_retrieval import BatchBrowserManager, Capture
from .utils.url_tools import URLs, normalize_url_keep_case, regex_find_urls, remove_utm_parameters

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

    A model whose request fails contributes nothing (the failure is logged, and
    :meth:`extract` reports the extraction as incomplete), so one unavailable
    model lowers recall without stopping the crawl.  A URL that a model
    returns but that cannot be parsed, such as one with an unclosed IPv6
    bracket, is logged and dropped.
    """

    def __init__(self, client: LLMClient, models: Sequence[str] = DEFAULT_URL_MODELS,
                 max_concurrent_requests: int = 30):
        self.client = client
        self.models = tuple(models)
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def extract(self, answer_text: str, logger: Logger) -> Tuple[List[str], bool]:
        """The union of the URLs the models found, and whether every model's request succeeded."""
        results = await asyncio.gather(*(self._extract_with(model, answer_text, logger) for model in self.models))
        urls = list(dict.fromkeys(url for found in results if found is not None for url in found))
        return urls, all(found is not None for found in results)

    async def _extract_with(self, model: str, answer_text: str, logger: Logger) -> Optional[List[str]]:
        """The URLs ``model`` found, or ``None`` if its request failed."""
        try:
            async with self._semaphore:
                result: URLs = await self.client.async_response(
                    model=model,
                    messages=[{"role": "system", "content": llm_extraction_prompts},
                              {"role": "user", "content": answer_text}],
                    response_format=URLs,
                )
            urls = result.urls or []
        except Exception as exc:
            logger.warning(f"URL extraction with {model} failed: {exc}")
            return None
        unparsable = [url for url in urls if not _parses(url)]
        if unparsable:
            logger.warning(f"Dropped {len(unparsable)} URL(s) that {model} returned but that cannot be parsed: "
                           f"{unparsable}")
        return [url for url in urls if url not in unparsable]


def _page_form(url: str) -> str:
    """The form by which spellings of one page are told apart from other pages' (see :func:`group_url_variants`).

    It is ``url`` under :func:`~mind2web2.utils.url_tools.normalize_url_keep_case`,
    except for a URL whose storage key
    :func:`~mind2web2.utils.cache_filesys.storage_key` would change again (an
    encoded ``#`` or ``%``, as in ``?q=C%23``): its form is that storage key
    with UTM parameters removed, ``http`` made ``https``, and ``www.`` dropped,
    the form by which the cache matches such keys, since normalizing the URL
    can give another page's URL (``?q=C``).  A URL that cannot be parsed is its own form.
    """
    try:
        key = storage_key(url)
        return _raw_form(key) if storage_key(key) != key else normalize_url_keep_case(url)
    except ValueError:
        return url


def group_url_variants(urls: Iterable[str], preferred: Collection[str] = ()) -> List[List[str]]:
    """Group the spellings in ``urls`` that name one page, each group in order of preference.

    Two spellings name one page when
    :func:`~mind2web2.utils.url_tools.normalize_url_keep_case` gives them the
    same form: when they differ only in scheme, ``www.``, a trailing slash,
    the fragment, UTM parameters, or percent-encoding.  Spellings that differ
    in letter case are never grouped, since a server may serve different pages
    for them, and a spelling with an encoded ``#`` or ``%`` (``?q=C%23``) is
    grouped only with spellings whose storage keys the cache matches to its
    own (keys that differ at most in scheme, ``www.``, and UTM parameters),
    since decoding it can give another page's URL (``?q=C``).  Within a group, spellings in ``preferred`` come first, then
    ``https`` spellings, then the shortest, then the alphabetically first.
    Groups are returned in order of their first spelling in ``urls``, and a
    spelling given several times appears once.  A spelling that cannot be
    parsed forms a group of its own.
    """
    groups: Dict[str, List[str]] = {}
    for url in dict.fromkeys(urls):
        groups.setdefault(_page_form(url), []).append(url)
    return [sorted(group, key=lambda u: (u not in preferred, not u.startswith("https://"), len(u), u.lower()))
            for group in groups.values()]


async def extract_answer_urls(answer_text: str, extractor: Optional[LLMUrlExtractor],
                              logger: Logger) -> Tuple[List[str], List[str], bool]:
    """The URLs that the regular expression finds in one answer, those the LLMs find, and whether every LLM answered."""
    found_by_llm, complete = await extractor.extract(answer_text, logger) if extractor is not None else ([], True)
    return regex_find_urls(answer_text), found_by_llm, complete


def _sorted_ci(items: Iterable[str]) -> List[str]:
    return sorted(items, key=str.lower)


@dataclass
class TaskUrls:
    """The URLs of one task's answers, as :func:`discover_task_urls` returns them."""

    urls: Dict[str, List[str]]  # each URL to capture -> the other spellings of its page, in order of preference
    complete: bool = True  # false when a URL-extraction request failed, so URLs may be missing


async def discover_task_urls(
        agent: str,
        task_id: str,
        *,
        answers_root: Path,
        cache_root: Path,
        extractor: Optional[LLMUrlExtractor],
        logger: Logger,
        refresh: bool = False,
) -> TaskUrls:
    """Return the task's URLs, from its metadata file or by extracting them from its answers.

    Each URL comes with the other spellings of its page that the answers use
    (see :func:`group_url_variants`).  The metadata file is reused when it was
    written for the answer files the task has now, with the same content, by
    the same URL models (none when ``extractor`` is ``None``), lists the other
    spellings of its URLs (``url_variants``), and no LLM request failed while
    writing it.  Otherwise (or when the file is missing or unreadable, or with
    ``refresh``), the answers are scanned again and the file is rewritten.  A
    task without answer files has no URLs, and its file is not touched.  See
    the module docstring for the file's layout.
    """
    meta_path = cache_root / agent / f"{task_id}.json"
    answers = list_answer_files(answers_root / agent / task_id)
    if not answers:
        return TaskUrls({})
    texts = [a.path.read_text(encoding="utf-8") for a in answers]
    digests = {a.path.name: hashlib.sha256(text.encode("utf-8")).hexdigest() for a, text in zip(answers, texts)}
    url_models = list(extractor.models) if extractor is not None else []
    if meta_path.exists() and not refresh:
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("answer_digests") != digests:
            reason = "does not match the current answers"
        elif meta.get("url_models") != url_models:
            reason = f"lists the URLs found by {meta.get('url_models')}, not {url_models}"
        elif meta.get("url_extraction_complete") is not True:
            reason = "is incomplete: a URL-extraction request failed when it was written"
        elif not isinstance(meta.get("url_variants"), dict):
            reason = "does not list the other spellings of its URLs"
        else:
            variants = meta["url_variants"]
            return TaskUrls({url: list(variants.get(url, [])) for url in meta["all_unique_urls"]})
        logger.info(f"[{agent}/{task_id}] {meta_path.name} {reason}; extracting the URLs again")

    extracted = await asyncio.gather(*(extract_answer_urls(text, extractor, logger) for text in texts))
    complete = all(ok for _, _, ok in extracted)
    cited_by: Dict[str, set] = {}
    found_by_regex: set = set()
    for answer, (by_regex, by_llm, _) in zip(answers, extracted):
        found_by_regex.update(by_regex)
        for url in by_regex + by_llm:
            cited_by.setdefault(url, set()).add(answer.path.name)
    # Answers may spell one page differently: list it once, under its preferred spelling across the answers.
    groups = {group[0]: group for group in group_url_variants(cited_by, preferred=found_by_regex)}
    files = {url: set().union(*(cited_by[spelling] for spelling in group)) for url, group in groups.items()}

    by_citations = sorted(files.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    all_urls = _sorted_ci(groups)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "agent_name": agent,
        "task_id": task_id,
        "total_unique_urls": len(all_urls),
        "all_unique_urls": all_urls,
        "urls": {url: _sorted_ci(answer_files) for url, answer_files in by_citations},
        "url_variants": {url: groups[url][1:] for url in all_urls if len(groups[url]) > 1},
        "answer_digests": digests,
        "url_models": url_models,
        "url_extraction_complete": complete,
        "url_types": {},
    }, ensure_ascii=False, indent=2), "utf-8")
    logger.info(f"[{agent}/{task_id}] {len(all_urls)} URLs in {len(answers)} answers -> {meta_path}")
    if not complete:
        logger.warning(f"[{agent}/{task_id}] Some URL-extraction requests failed, so URLs may be missing; "
                       f"the next crawl extracts the URLs again")
    return TaskUrls({url: groups[url][1:] for url in all_urls}, complete)


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
        variants: Sequence[str] = (),
) -> str:
    """Cache one page: download it if it is a PDF, otherwise capture it in the shared browser.

    ``variants`` are other spellings of the page (see :func:`group_url_variants`).
    When the capture of ``url`` fails, they are tried in order, and the first
    one captured is stored.  If none can be captured, one failure is recorded
    in the cache, under ``url``, with the reason of each spelling tried; it
    counts as a refusal only when every spelling was refused.

    The page is skipped when the cache holds a page for ``url`` and, unless
    ``retry_failed``, when it holds a failure record for it, both looked up
    with ``ignore_case=False`` (see :meth:`CacheFileSys.lookup`): a page or
    record under a URL that differs in letter case does not count, since it
    may be a different page.  Every step has its own time limit, so no URL
    can stall the crawl.

    Returns what happened: ``"cached"`` (already cached), ``"skipped"`` (a
    failure record, not retried), ``"stored"``, ``"failed"``, ``"blocked"``
    (every spelling was refused), or ``"error"`` (an unexpected exception,
    which is logged).
    """
    try:
        if cache.lookup(url, ignore_case=False) is not None:
            return "cached"
        if not retry_failed and cache.failure(url, ignore_case=False) is not None:
            return "skipped"
        url = remove_utm_parameters(url)
        failed: List[Tuple[str, Capture]] = []
        for spelling in dict.fromkeys([url, *map(remove_utm_parameters, variants)]):
            capture = await _store_page(spelling, cache, pdf_parser, browser_manager, logger)
            if capture is None:
                if failed:
                    logger.info(f"Stored {url} from its spelling {spelling}")
                return "stored"
            failed.append((spelling, capture))
        reason = "; ".join([failed[0][1].error] + [f"also tried {s}: {c.error}" for s, c in failed[1:]])
        blocked = all(c.blocked for _, c in failed)
        await asyncio.to_thread(cache.record_failure, url, reason, blocked=blocked)
        return "blocked" if blocked else "failed"
    except Exception:
        logger.error(f"Error crawling {url}", exc_info=True)
        return "error"


async def _store_page(url: str, cache: CacheFileSys, pdf_parser: PDFParser,
                      browser_manager: BatchBrowserManager, logger: Logger) -> Optional[Capture]:
    """Store the PDF that ``url`` serves, or else its capture in the browser.

    Returns ``None`` when the page was stored, and the failed :class:`Capture` otherwise.
    """
    logger.info(f"Crawling {url}")
    if await is_pdf(url):
        await asyncio.sleep(0.2 * random.random())
        pdf_bytes = await pdf_parser.fetch(url)
        if pdf_bytes is not None:
            await asyncio.to_thread(cache.put_pdf, url, pdf_bytes)
            return None
        logger.info(f"{url} did not return a PDF; loading it in the browser")
    capture = await browser_manager.capture(url, logger)
    if capture.ok:
        await asyncio.to_thread(cache.put_web, url, capture.text, capture.screenshot_b64)
        return None
    logger.warning(f"Could not capture {url}: {capture.error}")
    return capture


def _parses(url: str) -> bool:
    """Whether :func:`~mind2web2.utils.url_tools.normalize_url_keep_case` can parse ``url``."""
    try:
        normalize_url_keep_case(url)
        return True
    except ValueError:
        return False


@dataclass
class TaskCrawl:
    """What a crawl did with one task: its URL count, how many URLs ended in each outcome, and what went wrong."""

    task_id: str
    urls: int = 0
    outcomes: Counter = field(default_factory=Counter)
    url_extraction_complete: bool = True  # false when a URL-extraction request failed, so URLs may be missing
    #: Set when URL discovery raised, the task's cache could not be opened, or the
    #: crawl results could not be written to the task's metadata file.
    error: Optional[str] = None


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
        max_concurrent_urls: int = 16,
) -> List[TaskCrawl]:
    """Discover and capture the URLs of ``task_ids`` for ``agent``, all through ``browser``.

    URLs of every task are queued together and processed ``max_concurrent_urls``
    at a time, which bounds the PDF checks and downloads; the browser's page
    limit bounds the captures among them, so ``max_concurrent_urls`` should
    exceed it to keep the browser busy.  After the pass over all URLs, the URLs
    that failed in this crawl for a reason other than a refusal are retried
    once: such failures are often transient (a slow, overloaded, or
    rate-limiting site), while a refusal needs a person with a browser.  Finally each task's metadata file
    gains the content type of every cached URL and the reason of every failure.

    With ``retry_failed``, the failure records of each task's cache whose URLs
    are not in the task's URL list are crawled too, also for a task whose
    answers cite no URL: evaluation records them for the URLs it captures
    live, and they would otherwise stay for good.

    A task whose URL discovery raises, whose cache cannot be opened (an
    unreadable ``index.json`` or ``failures.json``), or whose metadata file
    cannot be updated after the crawl gets an ``error`` in its report; the
    other tasks are crawled as usual.
    """
    reports = {task_id: TaskCrawl(task_id) for task_id in task_ids}
    caches: Dict[str, CacheFileSys] = {}

    async def discover(task_id: str) -> Tuple[str, Dict[str, List[str]]]:
        report = reports[task_id]
        try:
            found = await discover_task_urls(agent, task_id, answers_root=answers_root, cache_root=cache_root,
                                             extractor=extractor, logger=logger, refresh=refresh_urls)
        except Exception as exc:
            logger.error(f"[{agent}/{task_id}] URL discovery failed", exc_info=True)
            report.error = f"URL discovery failed: {type(exc).__name__}: {exc}"
            return task_id, {}
        report.urls, report.url_extraction_complete = len(found.urls), found.complete
        if found.urls or (retry_failed and (cache_root / agent / task_id).is_dir()):
            try:
                caches[task_id] = CacheFileSys(str(cache_root / agent / task_id))
            except Exception as exc:  # CacheIndexError when index.json or failures.json cannot be read
                logger.error(f"[{agent}/{task_id}] Cannot open the cache", exc_info=True)
                report.error = f"Cannot open the cache: {exc}"
                return task_id, {}
        return task_id, found.urls

    task_urls = dict(await asyncio.gather(*(discover(task_id) for task_id in task_ids)))
    pdf_parser = PDFParser()
    outcomes: Dict[Tuple[str, str], str] = {}
    url_slots = asyncio.Semaphore(max_concurrent_urls)

    async def crawl_all(jobs: List[Tuple[str, str]], retry: bool, desc: str) -> None:
        with tqdm(total=len(jobs), desc=desc, unit="url", ncols=80, disable=not show_progress) as bar:
            async def crawl(job: Tuple[str, str]) -> None:
                task_id, url = job
                async with url_slots:
                    outcomes[job] = await crawl_one_page(url, caches[task_id], pdf_parser, browser, logger,
                                                         retry_failed=retry,
                                                         variants=task_urls[task_id].get(url, ()))
                bar.update(1)

            await asyncio.gather(*(crawl(job) for job in jobs))

    jobs = [(t, url) for t, urls in task_urls.items() for url in urls]
    if retry_failed:
        for task_id, urls in task_urls.items():
            if task_id not in caches:
                continue
            listed = {_page_form(url) for url in urls}
            unlisted = [url for url in caches[task_id].failures() if _page_form(url) not in listed]
            if unlisted:
                logger.info(f"[{agent}/{task_id}] Also retrying {len(unlisted)} failed URLs "
                            f"that are not in the task's URL list")
                jobs += [(task_id, url) for url in unlisted]
    await crawl_all(jobs, retry_failed, "Crawling")
    transient = [job for job, outcome in outcomes.items() if outcome == "failed"]
    if transient:
        logger.info(f"Retrying {len(transient)} URLs whose capture failed in this crawl")
        await crawl_all(transient, True, "Retrying")

    for (task_id, _), outcome in outcomes.items():
        reports[task_id].outcomes[outcome] += 1
    for task_id, urls in task_urls.items():
        if not urls:
            continue
        meta_path = cache_root / agent / f"{task_id}.json"
        try:
            _record_crawl_results(meta_path, caches[task_id], urls, logger)
        except Exception as exc:
            logger.error(f"[{agent}/{task_id}] Cannot record the crawl results in {meta_path}", exc_info=True)
            reports[task_id].error = (f"Cannot record the crawl results in {meta_path.name}: "
                                      f"{type(exc).__name__}: {exc}")
    return [reports[task_id] for task_id in task_ids]


def _record_crawl_results(meta_path: Path, cache: CacheFileSys, urls: Collection[str], logger: Logger) -> None:
    """Add the content type of each cached URL and the reason of each failed one to the metadata file.

    A URL counts as cached, or as failed, as in :func:`crawl_one_page`.
    """
    url_types: Dict[str, str] = {}
    failed_urls: Dict[str, str] = {}
    for url in urls:
        stored = cache.lookup(url, ignore_case=False)
        if stored is not None:
            url_types[url] = cache.has(stored)
        elif (failure := cache.failure(url, ignore_case=False)) is not None:
            failed_urls[url] = failure["reason"]
    meta = json.loads(meta_path.read_text("utf-8"))
    meta.update({"url_types": url_types, "cached_url_count": len(url_types), "failed_urls": failed_urls})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if failed_urls:
        logger.warning(f"{len(failed_urls)} of {len(urls)} URLs in {meta_path.stem} could not be captured; "
                       f"see failures.json in {cache.task_dir}")
