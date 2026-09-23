from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Union, Optional

from tqdm import tqdm

from . import results
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
        is_self_debug: bool = False,
):
    """Evaluate a single answer file and write its result JSON / logs."""

    answer_name = answer_path.name
    answer_base = results.answer_base(answer_name)

    # ---------- Create isolated logging ----------
    log_dir = output_dir / agent_name / task_id / answer_base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Use a more specific logger name to ensure uniqueness
    log_tag = f"{task_id}_{agent_name}_{answer_name}"
    if is_self_debug:
        log_tag += "_debug"

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
            "is_debug": is_self_debug,
            "operation": "eval_start"
        }
    )

    # ---------- Read answer ----------
    try:
        answer_text = answer_path.read_text(encoding="utf-8")
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
            _save_result_json(result, output_dir / agent_name / task_id, timestamp, is_self_debug)
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to save result for {agent_name}/{answer_name}: {e}")
        return e

    return result


def _judge_model(client) -> str:
    """The model eval scripts are told to use: the client's judge model, if it has one."""
    judge = getattr(client, "judge", None)
    return judge.model if judge is not None else DEFAULT_JUDGE_MODEL


def _save_result_json(result: Dict, agent_task_out_dir: Path, ts: str, is_debug: bool):
    """Write per‑answer result JSON to disk."""

    answer = result["answer_name"]
    save_dir = agent_task_out_dir / results.answer_base(answer) / "results"
    save_dir.mkdir(parents=True, exist_ok=True)

    with (save_dir / results.result_file_name(ts, answer, debug=is_debug)).open("w", encoding="utf-8") as fp:
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
        dump_cache: bool = True,
        is_self_debug: bool = False,
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
    dump_cache : bool, default True
        Whether to persist cache to disk
    is_self_debug : bool, default False
        Whether to add debug suffix to logs/results
    overwrite : bool, default False
        Whether to overwrite existing results
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

                def copy_answer(refresh: bool) -> None:
                    for src in (ans_path, metadata_path(ans_path)):
                        dst = answer_folder / src.name
                        if src.exists() and (refresh or not dst.exists()):
                            shutil.copyfile(src, dst)
                        elif not src.exists():
                            dst.unlink(missing_ok=True)  # a deleted metadata file must not live on in its copy

                # 5‑B. Result reuse check
                result_dir = answer_folder / "results"
                latest = results.latest_result_file(result_dir)
                if latest and not overwrite:
                    copy_answer(refresh=False)
                    main_logger.info(
                        f"⚠️ Using existing result for {agent_name}/{answer_name}",
                        extra={
                            "agent_name": agent_name,
                            "answer_name": answer_name,
                            "existing_result": str(latest),
                            "operation": "reuse_result"
                        }
                    )
                    main_logger.info(f"⚠️ Existing result -- {agent_name} {answer_name}")
                    try:
                        result = json.loads(latest.read_text(encoding="utf-8"))
                        main_logger.debug(
                            f"✅ Loaded existing result with score: {result.get('final_score')}",
                            extra={
                                "agent_name": agent_name,
                                "answer_name": answer_name,
                                "final_score": result.get('final_score'),
                                "operation": "existing_result_loaded"
                            }
                        )
                        return result
                    except Exception as exc:
                        main_logger.error(
                            f"❌ Failed to load existing result: {exc}",
                            extra={
                                "agent_name": agent_name,
                                "answer_name": answer_name,
                                "error": str(exc),
                                "operation": "existing_result_error"
                            }
                        )

                # 5‑C. Real evaluation
                copy_answer(refresh=True)
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
                        is_self_debug,
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
        # 7. Persist cache & save summary
        # ------------------------------------------------------------------
        cache.save()

        # Save summary for this agent/task combination
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

