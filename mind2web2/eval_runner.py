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
from .eval_toolkit import EvaluatorConfig
from .llm_client.judge import DEFAULT_JUDGE_MODEL, JudgeError
from .metrics import is_success
from .submission import answer_run, list_answer_files, metadata_path
from .utils.cache_filesys import CacheFileSys
from .utils.load_eval_script import load_eval_script
from .utils.logging_setup import create_logger, cleanup_logger


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
    and the framework's default :class:`EvaluatorConfig` settings
    (``evaluator_config``), such as the size limits of the screenshots sent to
    the judge.
    """

    answer_name = answer_path.name
    answer_base = results.answer_base(answer_name)

    # ---------- Create isolated logging ----------
    log_dir = output_dir / agent_name / task_id / answer_base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Use a more specific logger name to ensure uniqueness
    log_tag = f"{task_id}_{agent_name}_{answer_name}"

    # Important: Disable console output in concurrent environments to avoid log confusion
    logger, timestamp = create_logger(
        log_tag,
        str(log_dir),
        enable_console=False  # Disable console output during concurrency, only output to file
    )

    # Add structured log for task start
    logger.info(
        f"🚀 Starting evaluation for {agent_name}/{answer_name}",
        extra={
            "task_id": task_id,
            "agent_name": agent_name,
            "answer_name": answer_name,
            "answer_base": answer_base,
            "operation": "eval_start"
        }
    )

    # ---------- Read answer ----------
    try:
        answer_bytes = answer_path.read_bytes()
        answer_text = answer_bytes.decode("utf-8")
        logger.debug(
            f"Answer loaded: {len(answer_text)} characters",
            extra={"answer_length": len(answer_text)}
        )
    except Exception as e:
        logger.error(f"Failed to read answer file: {e}")
        return e

    result = None
    try:
        # Create a dual semaphore wrapper for the eval function
        dual_semaphore = DualSemaphore(webpage_semaphore, llm_semaphore)

        logger.info("🔄 Starting evaluation function")

        result: Dict = await eval_fn(
            client=client,
            answer=answer_text,
            agent_name=agent_name,
            answer_name=answer_name,
            cache=cache,
            semaphore=dual_semaphore,
            logger=logger,
            model=_judge_model(client),
        )

        # A judge request that failed for good leaves the score undetermined, even if
        # the eval script caught the error and carried on.
        failed_requests = (result.get("judge_usage") or {}).get("failed_requests", 0)
        if failed_requests:
            raise JudgeError(f"{failed_requests} judge request(s) failed; the answer is not scored")
        result["answer_sha256"] = hashlib.sha256(answer_bytes).hexdigest()  # what the result scored
        result["eval_script_sha256"] = script_sha256  # the script that scored it
        result["evaluator_config"] = EvaluatorConfig().as_dict()  # the defaults the script ran with

        logger.info(
            f"✅ Evaluation completed with score: {result.get('final_score', 'unknown')}",
            extra={
                "final_score": result.get("final_score"),
                "operation": "eval_complete"
            }
        )

    except Exception as exc:
        logger.exception(
            "❌ Evaluation raised an exception",
            extra={
                "error_type": type(exc).__name__,
                "operation": "eval_error"
            }
        )
        return exc
    finally:
        # Clean up logger resources
        try:
            cleanup_logger(logger)
        except Exception:
            pass  # Cleanup failure should not affect main flow

    # ---------- Save result ----------
    try:
        if result is not None:
            _save_result_json(result, output_dir / agent_name / task_id, timestamp)
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to save result for {agent_name}/{answer_name}: {e}")
        return e

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
    eval script, and the default :class:`EvaluatorConfig` settings as they are
    now, as every result saved by :func:`_eval_one_answer` does.  Settings that
    an eval script passes itself are part of the script, so its SHA-256 covers
    them.  Changes to the task's cached pages are not detected.
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
        judge configuration of ``client``, the SHA-256 of the eval script, and
        the current default :class:`EvaluatorConfig` settings.
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

    Returns
    -------
    List[Dict]
        List of evaluation results for all answers
    """

    # ------------------------------------------------------------------
    # 0. Setup paths & ensure dirs exist
    # ------------------------------------------------------------------
    answer_root = Path(answer_dir) / agent_name / task_id
    output_root = Path(output_dir)
    cache_root = Path(cache_dir) / agent_name

    output_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    # Check if answer directory exists
    if not answer_root.exists():
        logging.getLogger(__name__).warning(f"No answers found for {agent_name}/{task_id} at {answer_root}")
        return []

    # ------------------------------------------------------------------
    # 1. Create main task logger (for overall progress tracking)
    # ------------------------------------------------------------------
    main_log_dir = output_root / agent_name / task_id / "main_logs"
    main_log_dir.mkdir(parents=True, exist_ok=True)
    main_logger, main_timestamp = create_logger(
        f"main_{task_id}_{agent_name}",
        str(main_log_dir),
        enable_console=True  # Main logger can output to console
    )

    try:
        main_logger.info(
            f"🎯 Starting task evaluation: {task_id} for agent: {agent_name}",
            extra={
                "task_id": task_id,
                "agent_name": agent_name,
                "max_concurrent_answers": max_concurrent_answers,
                "operation": "task_start"
            }
        )

        # ------------------------------------------------------------------
        # 2. Load eval script & cache
        # ------------------------------------------------------------------
        main_logger.info("📜 Loading evaluation script")
        eval_fn = load_eval_script(script_path)
        script_sha256 = hashlib.sha256(Path(script_path).read_bytes()).hexdigest()

        cache_path = cache_root / f"{task_id}"
        cache = CacheFileSys(task_dir=str(cache_path))
        main_logger.info(f"💾 Cache loaded from {cache_path}")

        # ------------------------------------------------------------------
        # 3. Collect answer files
        # ------------------------------------------------------------------
        answer_paths = [a.path for a in list_answer_files(answer_root)]
        ignored = sorted(p.name for p in answer_root.iterdir()
                         if p.is_file() and p.suffix == ".md" and answer_run(p.name) is None)
        if ignored:
            main_logger.warning(f"Ignoring files not named answer_<k>.md: {ignored}")
        main_logger.info(
            f"📁 Found {len(answer_paths)} answer files to evaluate",
            extra={
                "answer_count": len(answer_paths),
                "answer_paths": [p.name for p in answer_paths]
            }
        )
        main_logger.info(f"-->> Answer Root: {answer_root}")
        main_logger.info(f"-->> Answers to Eval: {[p.name for p in answer_paths]}")

        if not answer_paths:
            main_logger.warning(f"No answer files found in {answer_root}")
            return []

        ok_results: List[Dict] = []

        # ------------------------------------------------------------------
        # 4. Concurrency control
        # ------------------------------------------------------------------
        # Use an outer semaphore to control concurrent answer evaluations
        outer_semaphore = asyncio.Semaphore(max_concurrent_answers)

        # Create default semaphores if not provided
        if webpage_semaphore is None:
            webpage_semaphore = asyncio.Semaphore(5)  # Default webpage limit
        if llm_semaphore is None:
            llm_semaphore = asyncio.Semaphore(30)  # Default LLM limit

        # ------------------------------------------------------------------
        # 5. Define per‑answer coroutine
        # ------------------------------------------------------------------
        async def _process_answer(ans_path: Path):
            async with outer_semaphore:  # Control concurrent answer evaluations
                answer_name = ans_path.name
                answer_base = results.answer_base(answer_name)

                main_logger.info(
                    f"👉 Processing {agent_name}/{answer_name}",
                    extra={
                        "agent_name": agent_name,
                        "answer_name": answer_name,
                        "operation": "answer_start"
                    }
                )
                main_logger.info(f"👉 Starting {agent_name} {answer_name}")

                # 5‑A. The answer folder keeps a copy of the answer (and its metadata) next to
                # its results, so that metrics can be computed from the results folder alone.
                answer_folder = output_root / agent_name / task_id / answer_base
                answer_folder.mkdir(parents=True, exist_ok=True)

                def copy_answer() -> None:
                    for src in (ans_path, metadata_path(ans_path)):
                        if src.exists():
                            shutil.copyfile(src, answer_folder / src.name)

                # 5‑B. Reuse the latest result if it scored this answer with this judge, script, and settings
                result_dir = answer_folder / "results"
                latest = results.latest_result_file(result_dir)
                if latest and not overwrite:
                    result, reason = _reusable_result(latest, ans_path, client, script_sha256)
                    if result is not None:
                        copy_answer()
                        main_logger.info(
                            f"⚠️ Using existing result for {agent_name}/{answer_name}",
                            extra={
                                "agent_name": agent_name,
                                "answer_name": answer_name,
                                "existing_result": str(latest),
                                "final_score": result.get("final_score"),
                                "operation": "reuse_result"
                            }
                        )
                        return result
                    main_logger.info(f"🔁 Evaluating {agent_name}/{answer_name} again: {reason}")

                # 5‑C. Real evaluation.  Earlier results move aside first, so that a failed
                # evaluation leaves the answer without a result instead of an outdated one.
                results.supersede_results(result_dir)
                copy_answer()
                try:
                    res = await _eval_one_answer(
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

                    if isinstance(res, dict):
                        main_logger.info(
                            f"✅ Successfully evaluated {agent_name}/{answer_name}",
                            extra={
                                "agent_name": agent_name,
                                "answer_name": answer_name,
                                "final_score": res.get('final_score'),
                                "operation": "answer_complete"
                            }
                        )
                    else:
                        main_logger.error(
                            f"❌ Evaluation failed for {agent_name}/{answer_name}: {res}",
                            extra={
                                "agent_name": agent_name,
                                "answer_name": answer_name,
                                "error": str(res),
                                "operation": "answer_error"
                            }
                        )

                    return res
                except Exception as exc:
                    main_logger.exception(
                        f"💥 Unexpected error evaluating {agent_name}/{answer_name}",
                        extra={
                            "agent_name": agent_name,
                            "answer_name": answer_name,
                            "error_type": type(exc).__name__,
                            "operation": "answer_exception"
                        }
                    )
                    return exc

        # ------------------------------------------------------------------
        # 6. Kick off evaluations
        # ------------------------------------------------------------------
        main_logger.info(f"🚀 Starting concurrent evaluation of {len(answer_paths)} answers")
        tasks = [asyncio.create_task(_process_answer(p)) for p in answer_paths]

        completed_count = 0
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc=f"[{task_id}/{agent_name}] Evaluating"):
            res = await coro
            completed_count += 1

            if isinstance(res, dict):
                ok_results.append(res)
                main_logger.debug(
                    f"✅ [{completed_count}/{len(tasks)}] Completed evaluation for {res.get('agent_name')}/{res.get('answer_name')}",
                    extra={
                        "completed_count": completed_count,
                        "total_count": len(tasks),
                        "agent_name": res.get('agent_name'),
                        "answer_name": res.get('answer_name'),
                        "final_score": res.get('final_score'),
                        "operation": "progress_update"
                    }
                )
            else:
                main_logger.error(
                    f"❌ [{completed_count}/{len(tasks)}] Evaluation failed with error: {res}",
                    extra={
                        "completed_count": completed_count,
                        "total_count": len(tasks),
                        "error": str(res),
                        "operation": "progress_error"
                    }
                )

        # ------------------------------------------------------------------
        # 7. Save summary for this agent/task combination
        # ------------------------------------------------------------------
        _save_agent_task_summary(output_root / agent_name / task_id, ok_results)
        main_logger.info("📊 Summary saved successfully")

        main_logger.info(
            f"🎉 Task evaluation completed: {len(ok_results)}/{len(answer_paths)} successful results",
            extra={
                "task_id": task_id,
                "agent_name": agent_name,
                "successful_count": len(ok_results),
                "total_count": len(answer_paths),
                "success_rate": len(ok_results) / len(answer_paths) if answer_paths else 0,
                "operation": "task_complete"
            }
        )

        return ok_results

    except Exception as e:
        main_logger.exception(
            f"💥 Task evaluation failed: {e}",
            extra={
                "task_id": task_id,
                "agent_name": agent_name,
                "error_type": type(e).__name__,
                "operation": "task_error"
            }
        )
        raise
    finally:
        # Clean up main logger
        try:
            cleanup_logger(main_logger)
        except Exception:
            pass


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
    and maps to an empty list.
    """
    task_semaphore = asyncio.Semaphore(max_concurrent_tasks)
    webpage_semaphore = webpage_semaphore or asyncio.Semaphore(5)
    llm_semaphore = llm_semaphore or asyncio.Semaphore(30)

    async def evaluate_one(task_id: str):
        async with task_semaphore:
            try:
                return task_id, await evaluate_task(
                    client=client, task_id=task_id, agent_name=agent_name, answer_dir=answer_dir,
                    cache_dir=cache_dir, output_dir=output_dir, script_path=scripts[task_id],
                    overwrite=overwrite, max_concurrent_answers=max_concurrent_answers,
                    webpage_semaphore=webpage_semaphore, llm_semaphore=llm_semaphore,
                )
            except Exception:
                logging.getLogger(__name__).exception(f"Evaluation of task {task_id} failed")
                return task_id, []

    results: Dict[str, List[Dict]] = {}
    with tqdm(total=len(scripts), desc="Evaluating tasks", unit="task") as bar:
        for coro in asyncio.as_completed([evaluate_one(task_id) for task_id in scripts]):
            task_id, task_results = await coro
            results[task_id] = task_results
            bar.update(1)
    return {task_id: results[task_id] for task_id in scripts}


# --------------------------------------------------------------------------- #
# Summary helpers                                                             #
# --------------------------------------------------------------------------- #


def _save_agent_task_summary(agent_task_dir: Path, task_results: List[Dict]):
    """Save summary for a specific agent/task combination."""
    if not task_results:
        return

    summary = []
    for res in sorted(task_results, key=lambda x: x.get("answer_name", "")):
        summary.append({
            "answer_name": res["answer_name"],
            "score": float(res["final_score"]),
            "status": "success" if res["final_score"] > 0 else "failed",
            "success": is_success(float(res["final_score"])),
        })

    with (agent_task_dir / "summary.json").open("w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=4)

