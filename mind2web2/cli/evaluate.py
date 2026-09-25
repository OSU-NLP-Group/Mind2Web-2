"""``mind2web2 evaluate``: score an agent's answers with the tasks' eval scripts.

Each task's eval script (``<eval-scripts-dir>/<version>/<task_id>.py``; see
:func:`mind2web2.eval_runner.resolve_scripts_dir` for how the version is
chosen) scores every ``answer_<k>.md`` of the task, and the result is written
to ``<results-dir>/<agent>/<task_id>/`` (layout in :mod:`mind2web2.results`).
Pages come from ``<cache-dir>/<agent>/<task_id>/``, which ``mind2web2 cache``
fills; a page missing from the cache is captured live and stored.  All live
captures of a run share one browser with at most ``--max-pages`` pages open.

An answer is not evaluated again when its latest result scored the same
answer file with the same judge configuration, the same eval script, the
same evaluator settings, the same scoring version
(:data:`mind2web2.results.SCORING_VERSION`), and the same
``MIND2WEB2_EVAL_DATE`` (:data:`mind2web2.results.EVAL_DATE_VARIABLE`), unless
``--overwrite``; changes to the cached pages are not detected.  Before an answer is evaluated again, its earlier results
move to ``results/superseded/``.  Selected tasks without answers are skipped.

Without ``--task-list`` or ``--task``, the tasks are those the agent has
answers for (task directories with ``answer_<k>.md`` files) and the
eval-script version has scripts for; the others are counted and skipped, since
an agent's answers may cover tasks whose scripts are not available locally.
With either option, a selected task without an eval script is listed, and
its answers are left without a result.
Unless ``--task`` selects tasks, the run ends by printing and saving the
metrics over the selected tasks with ``--num-runs`` runs per task (by default
the highest run index found), exactly as ``mind2web2 metrics`` computes them;
they record the task list, if one was given, and include a leaderboard entry
only for a task list with 3 runs.  It then writes the HTML page of the
agent's results, ``<results-dir>/<agent>/report.html`` (``mind2web2 report``).

Exits with status 0 when every answer of the selected tasks has a result; 1
when some answer has none, because its evaluation raised, a judge request
failed for good, or its task has no eval script, and when no selected task has
answers or the task list cannot be read; and 2 when no eval-script version
matches or the judge client cannot be created.
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
from ..llm_client import DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_REASONING_EFFORT, JudgeConfig, LLMClient
from ..metrics import collect_records, compute_metrics, format_report, save_metrics
from ..report import write_report
from ..submission import TaskInfo, list_answer_files
from ..utils.logging_setup import close_run_logging, configure_run_logging
from ..utils.page_info_retrieval import BatchBrowserManager

log = logging.getLogger(__name__)


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "evaluate", help="Score an agent's answers with the tasks' eval scripts.",
        description="Run each task's eval script on the agent's answers and write one result per answer "
                    "to <results-dir>/<agent>/<task_id>/, then print and save the metrics and write the HTML report "
                    "(report.html) of the results. An answer whose "
                    "latest result scored the same answer with the same judge, script, and settings is skipped unless "
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
    _common.add_task_filter(parser, default="the tasks the agent has answers for (task directories under "
                                            "<answers-dir>/<agent>/ with answer_<k>.md files) that have an "
                                            "eval script in the eval-script version")
    parser.add_argument("--num-runs", type=_common.positive_int, default=None,
                        help="Runs per task in the metrics printed at the end (the leaderboard uses 3); every "
                             "answer is evaluated regardless. Default: the highest run index found.")

    judge = parser.add_argument_group("judge")
    judge.add_argument("--llm-provider", choices=["openai", "azure_openai"], default="openai",
                       help="LLM provider of the judge (default: %(default)s).")
    judge.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                       help="Judge model; every judge request uses it, whatever model an eval script names "
                            "(default: %(default)s).")
    judge.add_argument("--judge-reasoning-effort", default=None,
                       help="reasoning_effort sent with every judge request, for reasoning models "
                            f"(default: {DEFAULT_JUDGE_REASONING_EFFORT!r} for {DEFAULT_JUDGE_MODEL}, "
                            "the model's own default for any other model).")
    judge.add_argument("--judge-temperature", type=float, default=None,
                       help="temperature sent with every judge request, for non-reasoning models "
                            "(default: not sent).")
    judge.add_argument("--judge-base-url", default=None,
                       help="OpenAI-compatible endpoint for the openai provider, e.g. a LiteLLM proxy "
                            "(default: $OPENAI_BASE_URL, else the OpenAI API).")

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument("--max-tasks", type=_common.positive_int, default=3,
                         help="Tasks evaluated at once (default: %(default)s).")
    runtime.add_argument("--max-answers", type=_common.positive_int, default=3,
                         help="Answers of one task evaluated at once (default: %(default)s).")
    runtime.add_argument("--max-pages", type=_common.positive_int, default=5,
                         help="Pages captured live at once, across the run (default: %(default)s).")
    runtime.add_argument("--max-llm-requests", type=_common.positive_int, default=30,
                         help="Judge requests in flight at once (default: %(default)s).")
    runtime.add_argument("--headless", action="store_true",
                         help="Capture live pages in a browser without a window.")
    runtime.add_argument("--overwrite", action="store_true",
                         help="Evaluate answers again even if their latest result scored the same answer "
                              "file with the same judge, eval script, evaluator settings, scoring version, "
                              "and MIND2WEB2_EVAL_DATE (earlier results move to results/superseded/).")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    try:
        scripts_dir = resolve_scripts_dir(args.eval_scripts_dir, args.eval_version)
    except ScriptsNotFound as exc:
        print(exc, file=sys.stderr)
        return 2
    agent_dir = args.answers_dir / args.agent
    try:
        tasks = _common.selected_tasks(args, _common.answer_task_ids(agent_dir))
    except (OSError, ValueError) as exc:
        print(f"Cannot read the task list: {exc}", file=sys.stderr)
        return 1
    answered = [t.task_id for t in tasks if list_answer_files(agent_dir / t.task_id)]
    unanswered = len(tasks) - len(answered)
    without_script = 0
    if args.task_list is None and not args.tasks:
        # The default selection, over which the metrics are computed: the tasks with answers and an eval script.
        tasks = [t for t in tasks if t.task_id in answered and (scripts_dir / f"{t.task_id}.py").is_file()]
        without_script, answered = len(answered) - len(tasks), [t.task_id for t in tasks]
    if not answered:
        if without_script:
            print(f"None of the {without_script} tasks that agent {args.agent!r} has answers for has an eval "
                  f"script in {scripts_dir}.", file=sys.stderr)
        else:
            print(f"No answer_<k>.md files found for the selected tasks of agent {args.agent!r} under "
                  f"{args.answers_dir}.", file=sys.stderr)
        return 1
    missing = [task_id for task_id in answered if not (scripts_dir / f"{task_id}.py").is_file()]
    scripts = {task_id: scripts_dir / f"{task_id}.py" for task_id in answered if task_id not in missing}

    judge = JudgeConfig(model=args.judge_model, reasoning_effort=args.judge_reasoning_effort,
                        temperature=args.judge_temperature)
    try:
        client = LLMClient(provider=args.llm_provider, is_async=True, base_url=args.judge_base_url, judge=judge)
    except Exception as exc:
        print(f"Cannot create the {args.llm_provider} judge client: {exc}", file=sys.stderr)
        return 2

    run_log = configure_run_logging(args.results_dir / args.agent / "logs", "evaluate")
    try:
        start = (f"Evaluating {len(scripts)} tasks of {args.agent!r} with the eval scripts in {scripts_dir} "
                 f"(judge: {_describe_judge(judge)}); results go to {args.results_dir / args.agent}")
        print(start)
        log.info(start, extra={"console": False})
        if without_script:
            print(f"Skipping {without_script} tasks that have answers but no eval script in {scripts_dir}; "
                  f"pass --task-list or --task to evaluate a given set of tasks.")
        if unanswered:
            print(f"Skipping {unanswered} tasks that have no answer_<k>.md files.")
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
            end = (f"{unscored} answers have no result; the reasons are in {run_log}.log, and each answer's "
                   f"log in the logs/ folder of its results. Running the command again evaluates only them.")
        else:
            end = f"Every answer has a result. The run's log: {run_log}.log"
        print(end)
        log.info(end, extra={"console": False})

        if scripts and not args.tasks:
            _report_metrics(args, tasks)
        if scripts:
            _write_report(args)
        return 1 if unscored else 0
    finally:
        close_run_logging()


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


def _write_report(args: argparse.Namespace) -> None:
    """Write the HTML page of the agent's results (``mind2web2 report``), over every task with results.

    Failed checks show thumbnails of the pages in the run's cache (``args.cache_dir``).
    """
    try:
        path = write_report(args.results_dir, args.agent, cache_root=args.cache_dir)
    except FileNotFoundError as exc:  # no answer has been evaluated
        print(f"Report not written: {exc}", file=sys.stderr)
        return
    except Exception as exc:  # the report is an extra; the evaluation and its metrics stand without it
        log.error("The report could not be written", exc_info=True, extra={"console": False})
        print(f"Report not written: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    print(f"Browse the results and the evidence of each check: {path}")


def _describe_judge(judge: JudgeConfig) -> str:
    """The judge model and the request parameters it is called with, as the start line shows them."""
    effort = judge.reasoning_effort if judge.reasoning_effort is not None else "not sent"
    temperature = judge.temperature if judge.temperature is not None else "not sent"
    return f"{judge.model}, reasoning_effort: {effort}, temperature: {temperature}"


def _report_metrics(args: argparse.Namespace, tasks: list[TaskInfo]) -> None:
    """Print and save the metrics over the selected tasks, as ``mind2web2 metrics`` computes them.

    The runs per task are ``--num-runs``, by default the highest run index found.
    """
    try:
        records, num_runs = collect_records(args.agent, [t.task_id for t in tasks], args.answers_dir,
                                            args.results_dir, args.num_runs)
    except (OSError, ValueError) as exc:  # includes MetadataError and answers that are not UTF-8
        print(f"Metrics not computed: {exc}", file=sys.stderr)
        return
    metrics = compute_metrics(records, tasks, num_runs, args.agent, args.task_list)
    print(format_report(metrics))
    if args.task_list is None:
        print("Scored over the evaluated tasks; pass --task-list to score a full split, where tasks "
              "without answers count as 0.")
    if args.num_runs is None and num_runs != 3:
        print(f"Scored {num_runs} runs, the highest run index found; pass --num-runs 3 to score the "
              f"leaderboard's three runs.")
    print(f"Saved {save_metrics(metrics, args.results_dir, args.agent)}")
