from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, Mapping, Union, Optional

from tqdm import tqdm

from . import results
from .eval_toolkit import EvaluatorConfig, HarnessError, browser_for_run
from .llm_client.judge import DEFAULT_JUDGE_MODEL, JudgeError
from .submission import answer_run, list_answer_files, metadata_path
from .utils.cache_filesys import CacheFileSys
from .utils.load_eval_script import load_eval_script
from .utils.logging_setup import cleanup_logger, create_logger, logging_to

#: The run's log: each answer's outcome and each task's problems (see :mod:`mind2web2.utils.logging_setup`).
log = logging.getLogger(__name__)


class DualSemaphore:
    """Wrapper to hold both webpage and LLM semaphores."""

    def __init__(self, webpage_semaphore: asyncio.Semaphore, llm_semaphore: asyncio.Semaphore):
        self.webpage = webpage_semaphore
        self.llm = llm_semaphore
        # Default to webpage semaphore for backward compatibility
        self._default = webpage_semaphore

    async def __aenter__(self):
        """For backward compatibility with code expecting a single semaphore."""
        return await self._default.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """For backward compatibility with code expecting a single semaphore."""
        return await self._default.__aexit__(exc_type, exc_val, exc_tb)


# --------------------------------------------------------------------------- #
# Eval-script versions                                                        #
# --------------------------------------------------------------------------- #

_DATED_VERSION = re.compile(r"\d{4}_\d{2}_\d{2}")


class ScriptsNotFound(Exception):
    """No directory of eval scripts matches the requested version; the message lists the available ones."""


def resolve_scripts_dir(root: Union[str, Path], version: Optional[str] = None) -> Path:
    """Return the directory of eval scripts to run, ``<root>/<version>``.

    Eval scripts are released in version directories named by date
    (``YYYY_MM_DD``, as under ``evaluation_scripts/`` in the Hugging Face
    dataset), and the repository ships the public dev set as
    ``eval_scripts/dev_set``.  A version directory is a subdirectory of ``root``
    that holds ``.py`` files.  Without ``version``, the newest dated version is
    used; if no version is dated, the only version; and if ``root`` has no
    version directories but holds scripts itself, ``root``.

    Raises :class:`ScriptsNotFound` when ``root`` does not exist, the requested
    version does not exist, or several undated versions leave the choice open.
    """
    root = Path(root)
    if not root.is_dir():
        raise ScriptsNotFound(f"Eval scripts directory not found: {root}")
    versions = sorted(d.name for d in root.iterdir() if d.is_dir() and any(d.glob("*.py")))
    available = ", ".join(versions) or "none"
    if version is not None:
        if version in versions:
            return root / version
        raise ScriptsNotFound(f"No eval scripts for version {version!r} in {root} (available: {available})")
    dated = [v for v in versions if _DATED_VERSION.fullmatch(v)]
    if dated:
        return root / dated[-1]
    if len(versions) == 1:
        return root / versions[0]
    if not versions and any(root.glob("*.py")):
        return root
    raise ScriptsNotFound(f"Choose an eval-script version in {root} (available: {available})")


# --------------------------------------------------------------------------- #
# Single‑answer evaluation                                                    #
# --------------------------------------------------------------------------- #


async def _eval_one_answer(
        eval_fn,
        client,
        task_id: str,
        agent_name: str,
        answer_path: Path,
        cache: CacheFileSys,
        webpage_semaphore: asyncio.Semaphore,
        llm_semaphore: asyncio.Semaphore,
        output_dir: Path,
        script_sha256: Optional[str] = None,
):
    """Evaluate a single answer file and write its result JSON / logs.

    The result records the SHA-256 of the answer file (``answer_sha256``) and
    of the eval script (``eval_script_sha256``, given as ``script_sha256``),
    the framework's default :class:`EvaluatorConfig` settings
    (``evaluator_config``), such as the size limits of the screenshots sent to
    the judge, :data:`mind2web2.results.SCORING_VERSION`
    (``scoring_version``), and the value of
    :data:`mind2web2.results.EVAL_DATE_VARIABLE` (``eval_date``).

    Everything the evaluation logs, including the package's own logging during
    it, goes to the answer's log, ``logs/<timestamp>_<answer>.log`` and
    ``.jsonl`` in the answer's results folder; the result file carries the same
    timestamp.  Returns the result, or the exception that left the answer
    unscored, whose traceback is in the answer's log.
    """

    answer_name = answer_path.name
    log_dir = output_dir / agent_name / task_id / results.answer_base(answer_name) / "logs"
    logger, timestamp = create_logger(answer_name, str(log_dir), enable_console=False)

    result = None
    try:
        with logging_to(logger):  # the package's own logging during this evaluation goes to the answer's log
            answer_bytes = answer_path.read_bytes()
            answer_text = answer_bytes.decode("utf-8")
            model = _judge_model(client)
            logger.info(f"Evaluating {agent_name}/{task_id}/{answer_name} ({len(answer_text):,} characters) "
                        f"with the judge {model}",
                        extra={"task_id": task_id, "agent_name": agent_name, "answer_name": answer_name})

            dual_semaphore = DualSemaphore(webpage_semaphore, llm_semaphore)
            result: Dict = await eval_fn(
                client=client,
                answer=answer_text,
                agent_name=agent_name,
                answer_name=answer_name,
                cache=cache,
                semaphore=dual_semaphore,
                logger=logger,
                model=model,
            )

            # A judge request that failed for good leaves the score undetermined, even if
            # the eval script caught the error and carried on.
            usage = result.get("judge_usage") or {}
            if usage.get("failed_requests", 0):
                raise JudgeError(f"{usage['failed_requests']} judge request(s) failed; the answer is not scored")
            if usage.get("harness_failures", 0):
                raise HarnessError(f"{usage['harness_failures']} page load(s) failed because of the evaluation "
                                   f"environment; the answer is not scored")
            result["answer_sha256"] = hashlib.sha256(answer_bytes).hexdigest()  # what the result scored
            result["eval_script_sha256"] = script_sha256  # the script that scored it
            result["evaluator_config"] = EvaluatorConfig().as_dict()  # the defaults the script ran with
            result["scoring_version"] = results.SCORING_VERSION  # the framework logic that scored it
            result["eval_date"] = results.eval_date()  # the date that date-dependent scripts took as today

            rejected = len(usage.get("rejections", []))
            logger.info(f"Final score {float(result['final_score']):.3f}, from {usage.get('requests', 0)} judge "
                        f"requests" + (f" and {rejected} rejected ones" if rejected else ""),
                        extra={"final_score": result.get("final_score")})
    except Exception as exc:
        logger.error(f"Not scored: {type(exc).__name__}: {exc}", exc_info=exc)
        return exc
    finally:
        cleanup_logger(logger)

    try:
        _save_result_json(result, output_dir / agent_name / task_id, timestamp)
    except Exception as exc:
        log.error(f"{task_id}/{answer_name}: the result could not be saved: {exc}", exc_info=exc)
        return exc
    return result


def _judge_model(client) -> str:
    """The model eval scripts are told to use: the client's judge model, if it has one."""
    judge = getattr(client, "judge", None)
    return judge.model if judge is not None else DEFAULT_JUDGE_MODEL


def _reusable_result(result_file: Path, answer_path: Path, client,
                     script_sha256: str) -> tuple[Optional[Dict], str]:
    """Return ``(result, "")`` if ``result_file`` scored the current answer under the current settings.

    Otherwise return ``(None, reason)``.  The result must record the SHA-256 of
    the answer file as it is now, the same judge configuration as ``client``
    (``None`` for a client without one), ``script_sha256``, the SHA-256 of the
    eval script, the default :class:`EvaluatorConfig` settings as they are
    now, the current :data:`mind2web2.results.SCORING_VERSION`, and the
    current value of :data:`mind2web2.results.EVAL_DATE_VARIABLE`, as every
    result saved by :func:`_eval_one_answer` does; a result that does not
    record ``eval_date`` counts as made without it.  Settings that an eval
    script passes itself are part of the script, so its SHA-256 covers them.
    Changes to the task's cached pages are not detected.
    """
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
        answer_sha256 = hashlib.sha256(answer_path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        return None, f"its latest result or the answer cannot be read ({exc})"
    if result.get("answer_sha256") != answer_sha256:
        return None, "its latest result is for a different version of the answer, or does not record it"
    judge = getattr(client, "judge", None)
    configured = judge.describe() if judge is not None else None
    if result.get("judge") != configured:
        return None, f"its latest result was judged by {result.get('judge')}, not {configured}"
    if result.get("eval_script_sha256") != script_sha256:
        return None, "its latest result was produced by another version of the eval script, or does not record it"
    if result.get("evaluator_config") != EvaluatorConfig().as_dict():
        return None, ("its latest result was produced with other evaluator settings, such as screenshot limits, "
                      "or does not record them")
    if result.get("scoring_version") != results.SCORING_VERSION:
        return None, "its latest result was produced by another version of the scoring logic, or does not record it"
    if result.get("eval_date") != results.eval_date():
        return None, (f"its latest result was produced with {results.EVAL_DATE_VARIABLE}={result.get('eval_date')}, "
                      f"not {results.eval_date()}")
    return result, ""


def _save_result_json(result: Dict, agent_task_out_dir: Path, ts: str):
    """Write per‑answer result JSON to disk."""

    answer = result["answer_name"]
    save_dir = agent_task_out_dir / results.answer_base(answer) / "results"
    save_dir.mkdir(parents=True, exist_ok=True)

    with (save_dir / results.result_file_name(ts, answer)).open("w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=4)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


async def evaluate_task(
        client,
        task_id: str,
        agent_name: str,
        answer_dir: Union[str, Path],
        cache_dir: Union[str, Path],
        output_dir: Union[str, Path],
        script_path: Union[str, Path],
        overwrite: bool = False,
        max_concurrent_answers: int = 3,
        webpage_semaphore: Optional[asyncio.Semaphore] = None,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        progress: Optional[tqdm] = None,
) -> List[Dict]:
    """Evaluate all answers for a specific task and agent.

    Parameters
    ----------
    client : LLMClient
        The LLM client to use for evaluation
    task_id : str
        The task identifier
    agent_name : str
        The agent name to evaluate
    answer_dir : Union[str, Path]
        Base directory containing answers (structure: <answer_dir>/<agent_name>/<task_id>/answer_*.md)
    cache_dir : Union[str, Path]
        Directory for cache files
    output_dir : Union[str, Path]
        Directory for output results
    script_path : Union[str, Path]
        Path to the evaluation script
    overwrite : bool, default False
        Evaluate every answer again, even one whose latest result could be
        reused.  Without it, an answer's latest result is reused, with no judge
        request, when it records the SHA-256 of the current answer file, the
        judge configuration of ``client``, the SHA-256 of the eval script, the
        current default :class:`EvaluatorConfig` settings, the current
        :data:`mind2web2.results.SCORING_VERSION`, and the current value of
        :data:`mind2web2.results.EVAL_DATE_VARIABLE`.
        Changes to the task's cached pages, such as pages recaptured in the
        Cache Manager, are not detected; evaluate with ``overwrite`` after
        changing them.  Before an answer is evaluated, its
        earlier results move to ``results/superseded/`` (see
        :mod:`mind2web2.results`).
    max_concurrent_answers : int, default 3
        Maximum number of concurrent answer evaluations
    webpage_semaphore : Optional[asyncio.Semaphore], default None
        Semaphore for controlling concurrent webpage retrieval operations
    llm_semaphore : Optional[asyncio.Semaphore], default None
        Semaphore for controlling concurrent LLM API requests
    progress : Optional[tqdm], default None
        Progress bar advanced once per answer; without it, the task shows
        its own bar

    Returns
    -------
    List[Dict]
        List of evaluation results for all answers

    Each answer's outcome is logged to the ``mind2web2.eval_runner`` logger:
    at INFO when the answer was evaluated (its score) or could not be scored
    (at ERROR, with the reason and where its log is), and at DEBUG when its
    earlier result was reused.

    Live captures use the browser shared through
    :func:`mind2web2.eval_toolkit.shared_browser`, or else one browser for
    the task that is stopped at its end (:func:`mind2web2.eval_toolkit.browser_for_run`).
    """
    async with browser_for_run():
        return await _evaluate_task(
            client=client,
            task_id=task_id,
            agent_name=agent_name,
            answer_dir=answer_dir,
            cache_dir=cache_dir,
            output_dir=output_dir,
            script_path=script_path,
            overwrite=overwrite,
            max_concurrent_answers=max_concurrent_answers,
            webpage_semaphore=webpage_semaphore,
            llm_semaphore=llm_semaphore,
            progress=progress,
        )


async def _evaluate_task(
        client,
        task_id: str,
        agent_name: str,
        answer_dir: Union[str, Path],
        cache_dir: Union[str, Path],
        output_dir: Union[str, Path],
        script_path: Union[str, Path],
        overwrite: bool = False,
        max_concurrent_answers: int = 3,
        webpage_semaphore: Optional[asyncio.Semaphore] = None,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        progress: Optional[tqdm] = None,
) -> List[Dict]:
    """The body of :func:`evaluate_task`, which runs it inside a shared browser."""
    answer_root = Path(answer_dir) / agent_name / task_id
    output_root = Path(output_dir)
    cache_root = Path(cache_dir) / agent_name
    output_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    if not answer_root.exists():
        log.warning(f"{task_id}: no answers at {answer_root}")
        return []

    try:
        eval_fn = load_eval_script(script_path)
        script_sha256 = hashlib.sha256(Path(script_path).read_bytes()).hexdigest()
        cache = CacheFileSys(task_dir=str(cache_root / task_id))

        answer_paths = [a.path for a in list_answer_files(answer_root)]
        ignored = sorted(p.name for p in answer_root.iterdir()
                         if p.is_file() and p.suffix == ".md" and answer_run(p.name) is None)
        if ignored:
            log.warning(f"{task_id}: ignoring files not named answer_<k>.md: {ignored}")
        if not answer_paths:
            log.warning(f"{task_id}: no answer_<k>.md files in {answer_root}")
            return []
        log.debug(f"{task_id}: evaluating {len(answer_paths)} answers with {script_path}",
                  extra={"task_id": task_id, "answers": [p.name for p in answer_paths]})

        outer_semaphore = asyncio.Semaphore(max_concurrent_answers)
        webpage_semaphore = webpage_semaphore or asyncio.Semaphore(5)
        llm_semaphore = llm_semaphore or asyncio.Semaphore(30)

        async def _process_answer(ans_path: Path):
            async with outer_semaphore:
                answer_name = ans_path.name
                name = f"{task_id}/{answer_name}"
                context = {"task_id": task_id, "agent_name": agent_name, "answer_name": answer_name}

                # The answer folder keeps a copy of the answer (and its metadata) next to
                # its results, so that metrics can be computed from the results folder alone.
                answer_folder = output_root / agent_name / task_id / results.answer_base(answer_name)
                try:
                    res = await _reuse_or_evaluate(ans_path, answer_folder, name, context)
                except Exception as exc:  # also a failure to prepare the answer folder: this answer only
                    log.error(f"{name}: not scored: evaluating it raised {type(exc).__name__}: {exc}",
                              exc_info=exc, extra=context)
                    return exc
                if isinstance(res, dict):
                    log.info(f"{name}: {float(res['final_score']):.3f}",
                             extra={**context, "final_score": res.get("final_score")})
                else:
                    reason = (str(res).splitlines() or [""])[0][:300]
                    log.error(f"{name}: not scored ({type(res).__name__}: {reason}); "
                              f"see its log in {answer_folder / 'logs'}", extra={**context, "reason": str(res)})
                return res

        async def _reuse_or_evaluate(ans_path: Path, answer_folder: Path, name: str, context: dict):
            """The answer's reusable latest result, else the outcome of evaluating it (a result or an exception)."""
            answer_folder.mkdir(parents=True, exist_ok=True)

            def copy_answer() -> None:
                for src in (ans_path, metadata_path(ans_path)):
                    if src.exists():
                        shutil.copyfile(src, answer_folder / src.name)
                    else:  # a deleted metadata file must not live on in its copy
                        (answer_folder / src.name).unlink(missing_ok=True)

            # Reuse the latest result if it scored this answer with this judge, script, and settings
            result_dir = answer_folder / "results"
            latest = results.latest_result_file(result_dir)
            if latest and not overwrite:
                result, reason = _reusable_result(latest, ans_path, client, script_sha256)
                if result is not None:
                    copy_answer()
                    log.debug(f"{name}: {float(result['final_score']):.3f} (earlier result reused)",
                              extra={**context, "final_score": result.get("final_score"),
                                     "result_file": str(latest)})
                    return result
                log.debug(f"{name}: evaluating again, since {reason}", extra=context)

            # Earlier results move aside first, so that a failed evaluation leaves
            # the answer without a result instead of an outdated one.
            results.supersede_results(result_dir)
            copy_answer()
            return await _eval_one_answer(
                eval_fn,
                client,
                task_id,
                agent_name,
                ans_path,
                cache,
                webpage_semaphore,
                llm_semaphore,
                output_root,
                script_sha256,
            )

        tasks = [asyncio.create_task(_process_answer(p)) for p in answer_paths]
        bar = progress if progress is not None else tqdm(total=len(tasks), desc=task_id, unit="answer")
        ok_results: List[Dict] = []
        try:
            for coro in asyncio.as_completed(tasks):
                res = await coro
                if isinstance(res, dict):
                    ok_results.append(res)
                bar.update(1)
        finally:
            if progress is None:
                bar.close()

        log.debug(f"{task_id}: {len(ok_results)} of {len(answer_paths)} answers scored",
                  extra={"task_id": task_id, "scored": len(ok_results), "answers": len(answer_paths)})
        return ok_results

    except Exception as e:
        log.error(f"{task_id}: evaluating the task failed: {type(e).__name__}: {e}", exc_info=e,
                  extra={"task_id": task_id})
        raise


async def evaluate_tasks(
        client,
        agent_name: str,
        scripts: Mapping[str, Union[str, Path]],
        *,
        answer_dir: Union[str, Path],
        cache_dir: Union[str, Path],
        output_dir: Union[str, Path],
        overwrite: bool = False,
        max_concurrent_tasks: int = 3,
        max_concurrent_answers: int = 3,
        webpage_semaphore: Optional[asyncio.Semaphore] = None,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict[str, List[Dict]]:
    """Evaluate the answers of several tasks with :func:`evaluate_task`, ``max_concurrent_tasks`` at a time.

    ``scripts`` maps each task ID to its eval script.  The semaphores are shared
    by all tasks.  Returns, in the order of ``scripts``, each task's results as
    :func:`evaluate_task` returns them; a task whose evaluation raised is logged
    and maps to an empty list.  All tasks share one browser for live captures,
    as :func:`evaluate_task` describes, and one progress bar over their answers.
    """
    task_semaphore = asyncio.Semaphore(max_concurrent_tasks)
    webpage_semaphore = webpage_semaphore or asyncio.Semaphore(5)
    llm_semaphore = llm_semaphore or asyncio.Semaphore(30)

    counts = {task_id: len(list_answer_files(Path(answer_dir) / agent_name / task_id)) for task_id in scripts}

    async def evaluate_one(task_id: str, bar: tqdm):
        async with task_semaphore:
            task_bar = _CountingProgress(bar)
            try:
                return task_id, await evaluate_task(
                    client=client, task_id=task_id, agent_name=agent_name, answer_dir=answer_dir,
                    cache_dir=cache_dir, output_dir=output_dir, script_path=scripts[task_id],
                    overwrite=overwrite, max_concurrent_answers=max_concurrent_answers,
                    webpage_semaphore=webpage_semaphore, llm_semaphore=llm_semaphore, progress=task_bar,
                )
            except Exception:
                bar.update(max(counts[task_id] - task_bar.n, 0))  # the answers it did not get to
                return task_id, []  # evaluate_task logged why

    results: Dict[str, List[Dict]] = {}
    async with browser_for_run():
        with tqdm(total=sum(counts.values()), desc="Evaluating", unit="answer") as bar:
            for coro in asyncio.as_completed([evaluate_one(task_id, bar) for task_id in scripts]):
                task_id, task_results = await coro
                results[task_id] = task_results
    return {task_id: results[task_id] for task_id in scripts}


class _CountingProgress:
    """Advances a shared progress bar and counts the steps one task made on it."""

    def __init__(self, bar: tqdm):
        self.bar = bar
        self.n = 0

    def update(self, n: int = 1) -> None:
        self.n += n
        self.bar.update(n)

