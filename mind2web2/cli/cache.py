"""``mind2web2 cache``: store the webpages an agent's answers cite, so that evaluation reads them from disk.

For each task, the URLs of the task's ``answer_<k>.md`` files are extracted
(regex plus LLMs, or regex only with ``--no-llm``) and listed in
``<cache-dir>/<agent>/<task_id>.json``, and every page is stored in
``<cache-dir>/<agent>/<task_id>/``; see :mod:`mind2web2.crawl`.  Spellings of
one page are merged across the task's answers, so a page that several answers
cite is captured once; when its capture fails, its other spellings are tried.
Spellings that differ in letter case count as different pages.  The URL list
is reused while the task's answers and the URL models are unchanged, unless a
URL-extraction request failed when it was made.  URLs that are already cached
are skipped, and so are URLs whose capture failed in an earlier crawl unless
``--retry-failed``.  Selected tasks without ``answer_<k>.md`` files are
skipped, and their URL lists are left as they are.  All tasks share one
browser with at most ``--max-pages`` pages open, and at most twice that many
URLs (and at least 8) are processed at once.

The command prints one line per task with its URL count and how many URLs
ended in each outcome, and marks tasks whose URL extraction was incomplete
because an LLM request failed.  Failed captures are recorded in the task's
``failures.json``; the Cache Manager (``cache_manager_web/``) lists them for
review.  Exits with status 1 when no selected task has answers, the task list
cannot be read, URL discovery for a task raised, a task's cache or URL list
cannot be read or updated, or a URL raised an unexpected error; 2 when the
URL-extraction client cannot be created; and 0 otherwise, including when
captures failed or URL extraction was incomplete.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import _common
from ..crawl import DEFAULT_URL_MODELS, OUTCOMES, LLMUrlExtractor, TaskCrawl, cache_answers
from ..llm_client import LLMClient
from ..submission import list_answer_files
from ..utils.logging_setup import create_logger
from ..utils.page_info_retrieval import BatchBrowserManager


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "cache", help="Store the webpages cited in an agent's answers.",
        description="Extract the URLs of an agent's answers and store every page (text and screenshot, "
                    "or the PDF) in <cache-dir>/<agent>/<task_id>/, so that evaluation reads them from "
                    "disk. Failed captures are recorded for review in the Cache Manager.",
    )
    _common.add_agent(parser)
    _common.add_answers_dir(parser)
    _common.add_cache_dir(parser)
    _common.add_task_filter(parser, default="the tasks the agent has answers for (task directories under "
                                            "<answers-dir>/<agent>/ with answer_<k>.md files)")
    parser.add_argument("--max-pages", type=_common.positive_int, default=5,
                        help="Browser pages open at once, across all tasks (default: %(default)s).")
    parser.add_argument("--page-timeout", type=_common.positive_seconds, default=90.0,
                        help="Seconds one capture attempt may take (default: %(default)s).")
    parser.add_argument("--attempts", type=_common.positive_int, default=1,
                        help="Capture attempts per page when an attempt raises or times out (default: %(default)s).")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Crawl URLs whose capture failed in an earlier crawl again.")
    parser.add_argument("--headless", action="store_true",
                        help="Run the browser without a window. Some sites refuse headless browsers more often.")
    parser.add_argument("--no-llm", action="store_true",
                        help="Extract URLs with the regular expression only; no API key is needed.")
    parser.add_argument("--url-models", default=",".join(DEFAULT_URL_MODELS),
                        help="Comma-separated models that extract URLs; their results are merged "
                             "(default: %(default)s).")
    parser.add_argument("--refresh-urls", action="store_true",
                        help="Extract the URLs again even if <cache-dir>/<agent>/<task_id>.json lists them "
                             "for the task's current answers and URL models.")
    parser.add_argument("--llm-provider", choices=["openai", "azure_openai"], default="openai",
                        help="Provider of the URL-extraction models (default: %(default)s).")
    parser.add_argument("--llm-base-url", default=None,
                        help="OpenAI-compatible endpoint for the openai provider's URL-extraction requests, "
                             "e.g. a LiteLLM proxy (default: $OPENAI_BASE_URL, else the OpenAI API).")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    agent_dir = args.answers_dir / args.agent
    try:
        tasks = _common.selected_tasks(args, _common.answer_task_ids(agent_dir))
    except (OSError, ValueError) as exc:
        print(f"Cannot read the task list: {exc}", file=sys.stderr)
        return 1
    task_ids = [t.task_id for t in tasks if list_answer_files(agent_dir / t.task_id)]
    if not task_ids:
        print(f"No answer_<k>.md files found for the selected tasks of agent {args.agent!r} under "
              f"{args.answers_dir}.", file=sys.stderr)
        return 1

    extractor = None
    if not args.no_llm:
        try:
            client = LLMClient(provider=args.llm_provider, is_async=True, base_url=args.llm_base_url)
        except Exception as exc:
            print(f"Cannot create the {args.llm_provider} client for URL extraction ({exc}); "
                  f"set its API key or pass --no-llm.", file=sys.stderr)
            return 2
        models = [m.strip() for m in args.url_models.split(",") if m.strip()]
        extractor = LLMUrlExtractor(client, models)

    logger, _ = create_logger("mind2web2_cache", str(args.cache_dir / "logs"), enable_console=False)
    print(f"Caching {len(task_ids)} tasks of {args.agent!r} into {args.cache_dir / args.agent} "
          f"(URL extraction: {'regex' if extractor is None else 'regex + ' + ', '.join(extractor.models)}; "
          f"log: {args.cache_dir / 'logs'})")
    if len(task_ids) < len(tasks):
        print(f"Skipping {len(tasks) - len(task_ids)} tasks that have no answer_<k>.md files.")
    reports = asyncio.run(_crawl(args, task_ids, extractor, logger))

    print(_format_reports(reports))
    failed = sum(r.outcomes["failed"] + r.outcomes["blocked"] for r in reports)
    if failed:
        print(f"{failed} URLs could not be captured; review them with the Cache Manager: "
              f"uv run python cache_manager_web/run.py {args.agent}")
    incomplete = sum(not r.url_extraction_complete for r in reports)
    if incomplete:
        print(f"URL extraction was incomplete for {incomplete} tasks: a URL-extraction request failed (see the "
              f"log), so URLs may be missing. Running the command again extracts their URLs again.")
    return 1 if any(r.error or r.outcomes["error"] for r in reports) else 0


async def _crawl(args: argparse.Namespace, task_ids, extractor, logger) -> list[TaskCrawl]:
    browser = BatchBrowserManager(headless=args.headless, max_concurrent_pages=args.max_pages,
                                  max_retries=args.attempts, page_timeout=args.page_timeout)
    try:
        return await cache_answers(
            args.agent, task_ids, answers_root=args.answers_dir, cache_root=args.cache_dir, browser=browser,
            extractor=extractor, logger=logger, retry_failed=args.retry_failed, refresh_urls=args.refresh_urls,
            max_concurrent_urls=max(8, 2 * args.max_pages),
        )
    finally:
        await browser.stop()


def _format_reports(reports: list[TaskCrawl]) -> str:
    """One line per task (URL count, outcome counts, error or incomplete URL extraction), then the totals."""
    width = max([len(r.task_id) for r in reports] + [5])
    header = f"{'task':<{width}}  {'urls':>5}  " + "  ".join(f"{o:>7}" for o in OUTCOMES)
    lines = [header]
    for r in reports:
        counts = "  ".join(f"{r.outcomes[o]:>7}" for o in OUTCOMES)
        notes = [r.error] if r.error else []
        if not r.url_extraction_complete:
            notes.append("URL extraction incomplete")
        lines.append(f"{r.task_id:<{width}}  {r.urls:>5}  {counts}" + "".join(f"  {note}" for note in notes))
    total = sum(r.urls for r in reports)
    counts = "  ".join(f"{sum(r.outcomes[o] for r in reports):>7}" for o in OUTCOMES)
    lines.append(f"{'total':<{width}}  {total:>5}  {counts}")
    return "\n".join(lines)
