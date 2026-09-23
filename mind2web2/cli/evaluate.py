"""``mind2web2 evaluate``: score an agent's answers with the tasks' eval scripts.

Each task's eval script (``<eval-scripts-dir>/<version>/<task_id>.py``; see
:func:`mind2web2.eval_runner.resolve_scripts_dir` for how the version is
chosen) scores every ``answer_<k>.md`` of the task, and the result is written
to ``<results-dir>/<agent>/<task_id>/`` (layout in :mod:`mind2web2.results`).
Pages come from ``<cache-dir>/<agent>/<task_id>/``, which ``mind2web2 cache``
fills; a page missing from the cache is captured live and stored.  All live
captures of a run share one browser with at most ``--max-pages`` pages open.

An answer is not evaluated again when its latest result scored the same
answer file with the same judge configuration, unless ``--overwrite``; before
an answer is evaluated again, its earlier results move to
``results/superseded/``.  Selected tasks without answers are skipped.  Unless
``--task`` selects tasks, the run ends by printing and saving the metrics over
the selected tasks, exactly as ``mind2web2 metrics`` computes them.

Exits with status 0 when every answer of the selected tasks has a result; 1
when some answer has none, because its evaluation raised, a judge request
failed for good, or its task has no eval script; and 2 when no eval-script
version matches or the judge client cannot be created.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import _common
from ..eval_runner import ScriptsNotFound, evaluate_tasks, resolve_scripts_dir
from ..eval_toolkit import shared_browser
from ..llm_client import DEFAULT_JUDGE_MODEL, JudgeConfig, LLMClient
from ..metrics import collect_records, compute_metrics, format_report, save_metrics
from ..submission import TaskInfo, list_answer_files
from ..utils.page_info_retrieval import BatchBrowserManager


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "evaluate", help="Score an agent's answers with the tasks' eval scripts.",
        description="Run each task's eval script on the agent's answers and write one result per answer "
                    "to <results-dir>/<agent>/<task_id>/, then print and save the metrics. An answer whose "
                    "latest result scored the same answer with the same judge is skipped unless "
                    "--overwrite. Exits with status 1 if some answer has no result afterwards.",
    )
    _common.add_agent(parser)
    _common.add_answers_dir(parser)
    _common.add_results_dir(parser)
    _common.add_cache_dir(parser)
    parser.add_argument("--eval-scripts-dir", type=Path, default=Path("eval_scripts"),
                        help="Directory of eval-script versions, each a subdirectory of <task_id>.py files "
                             "(default: %(default)s).")
    parser.add_argument("--eval-version", default=None,
                        help="Version (subdirectory) of the eval scripts. Default: the newest dated version "
                             "(YYYY_MM_DD), or the only version if none is dated.")
    _common.add_task_filter(parser)

    judge = parser.add_argument_group("judge")
    judge.add_argument("--llm-provider", choices=["openai", "azure_openai"], default="openai",
                       help="LLM provider of the judge (default: %(default)s).")
    judge.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                       help="Judge model; every judge request uses it, whatever model an eval script names "
                            "(default: %(default)s).")
    judge.add_argument("--judge-reasoning-effort", default=None,
                       help="reasoning_effort sent with every judge request, for reasoning models "
                            "(default: the model's own default).")
    judge.add_argument("--judge-temperature", type=float, default=None,
                       help="temperature sent with every judge request, for non-reasoning models "
                            "(default: not sent).")
    judge.add_argument("--judge-base-url", default=None,
                       help="OpenAI-compatible endpoint for the openai provider, e.g. a LiteLLM proxy "
                            "(default: $OPENAI_BASE_URL, else the OpenAI API).")

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument("--max-tasks", type=int, default=3,
                         help="Tasks evaluated at once (default: %(default)s).")
    runtime.add_argument("--max-answers", type=int, default=3,
                         help="Answers of one task evaluated at once (default: %(default)s).")
    runtime.add_argument("--max-pages", type=int, default=5,
                         help="Pages captured live at once, across the run (default: %(default)s).")
    runtime.add_argument("--max-llm-requests", type=int, default=30,
                         help="Judge requests in flight at once (default: %(default)s).")
    runtime.add_argument("--headless", action="store_true",
                         help="Capture live pages in a browser without a window.")
    runtime.add_argument("--overwrite", action="store_true",
                         help="Evaluate answers again even if their latest result scored the same answer "
                              "with the same judge (earlier results move to results/superseded/).")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    try:
        scripts_dir = resolve_scripts_dir(args.eval_scripts_dir, args.eval_version)
    except ScriptsNotFound as exc:
        print(exc, file=sys.stderr)
        return 2
    agent_dir = args.answers_dir / args.agent
    tasks = _common.selected_tasks(args, _common.answer_task_ids(agent_dir))
    answered = [t.task_id for t in tasks if list_answer_files(agent_dir / t.task_id)]
    if not answered:
        print(f"No answers found for the selected tasks of agent {args.agent!r} under {args.answers_dir}.",
              file=sys.stderr)
        return 1
    missing = [task_id for task_id in answered if not (scripts_dir / f"{task_id}.py").is_file()]
    scripts = {task_id: scripts_dir / f"{task_id}.py" for task_id in answered if task_id not in missing}

    try:
        client = LLMClient(
            provider=args.llm_provider, is_async=True, base_url=args.judge_base_url,
            judge=JudgeConfig(model=args.judge_model, reasoning_effort=args.judge_reasoning_effort,
                              temperature=args.judge_temperature),
        )
    except Exception as exc:
        print(f"Cannot create the {args.llm_provider} judge client: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per judge request otherwise
    print(f"Evaluating {len(scripts)} tasks of {args.agent!r} with the eval scripts in {scripts_dir} "
          f"(judge: {args.judge_model}); results go to {args.results_dir / args.agent}")
    if len(answered) < len(tasks):
        print(f"Skipping {len(tasks) - len(answered)} selected tasks that have no answers.")
    results = asyncio.run(_evaluate(args, client, scripts)) if scripts else {}

    unscored = 0
    for task_id in answered:
        num_answers = len(list_answer_files(agent_dir / task_id))
        if task_id in missing:
            print(f"  {task_id}: no eval script {scripts_dir / (task_id + '.py')}")
            unscored += num_answers
            continue
        scores = [float(r["final_score"]) for r in results[task_id]]
        unscored += num_answers - len(scores)
        mean = f", mean score {sum(scores) / len(scores):.3f}" if scores else ""
        print(f"  {task_id}: {len(scores)}/{num_answers} answers scored{mean}")
    if unscored:
        print(f"{unscored} answers have no result; their logs are under {args.results_dir / args.agent}. "
              f"Running the command again evaluates only them.")

    if scripts and not args.tasks:
        _report_metrics(args, tasks)
    return 1 if unscored else 0


async def _evaluate(args: argparse.Namespace, client: LLMClient, scripts: dict[str, Path]):
    browser = BatchBrowserManager(headless=args.headless, max_concurrent_pages=args.max_pages, max_retries=1)
    try:
        with shared_browser(browser):
            return await evaluate_tasks(
                client, args.agent, scripts, answer_dir=args.answers_dir, cache_dir=args.cache_dir,
                output_dir=args.results_dir, overwrite=args.overwrite, max_concurrent_tasks=args.max_tasks,
                max_concurrent_answers=args.max_answers, webpage_semaphore=asyncio.Semaphore(args.max_pages),
                llm_semaphore=asyncio.Semaphore(args.max_llm_requests),
            )
    finally:
        await browser.stop()


def _report_metrics(args: argparse.Namespace, tasks: list[TaskInfo]) -> None:
    """Print and save the metrics over the selected tasks, as ``mind2web2 metrics`` computes them."""
    try:
        records, num_runs = collect_records(args.agent, [t.task_id for t in tasks], args.answers_dir,
                                            args.results_dir)
    except (OSError, ValueError) as exc:  # includes MetadataError and answers that are not UTF-8
        print(f"Metrics not computed: {exc}", file=sys.stderr)
        return
    metrics = compute_metrics(records, tasks, num_runs, args.agent)
    print(format_report(metrics))
    if args.task_list is None:
        print("Scored over the tasks the agent has answers for; pass --task-list to score a full split, "
              "where tasks without answers count as 0.")
    print(f"Saved {save_metrics(metrics, args.results_dir, args.agent)}")
