"""Every eval script must run end to end offline and reproduce its golden rubric tree.

Each (script, policy) pair runs in its own subprocess via ``tests/offline_eval.py``
so that a script stuck in an infinite loop is killed and reported instead of
hanging the test session.  See that module for what each policy exercises.

The dev-set scripts are compared against ``tests/golden/offline``, where each file
lists one rubric node per line (see ``offline_eval.render_result``).  To check
another script directory, pass ``--eval-scripts-dir``; add ``--golden-dir`` to
compare against golden files kept outside the repository, and ``--update-golden``
to (re)write them.
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from offline_eval import POLICIES, render_result

from mind2web2.results import EVAL_DATE_VARIABLE

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
OFFLINE_EVAL = TESTS_DIR / "offline_eval.py"
DEV_SET_DIR = REPO_ROOT / "eval_scripts" / "dev_set"
DEFAULT_GOLDEN_DIR = TESTS_DIR / "golden" / "offline"
MAX_DIFF_LINES = 40
#: The scripts' environment, without a pinned evaluation date, which would change the date-dependent goldens.
_SCRIPT_ENV = {k: v for k, v in os.environ.items() if k != EVAL_DATE_VARIABLE}


def _golden_dir(config, script: Path) -> Path | None:
    golden_dir = config.getoption("--golden-dir")
    if golden_dir is None and script.parent.resolve() == DEV_SET_DIR.resolve():
        golden_dir = DEFAULT_GOLDEN_DIR
    return golden_dir


@pytest.mark.parametrize("policy", POLICIES)
def test_eval_script_offline(eval_script: Path, policy: str, request):
    config = request.config
    timeout = config.getoption("--script-timeout")
    cmd = [
        sys.executable, str(OFFLINE_EVAL),
        "--script", str(eval_script),
        "--policy", policy,
        "--answers-dir", str(config.getoption("--answers-dir")),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT, env=_SCRIPT_ENV)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{eval_script.name} did not finish within {timeout:.0f}s under policy "
                    f"{policy!r}; the script probably loops forever on this input")
    assert proc.returncode == 0, proc.stderr[-3000:]
    result = json.loads(proc.stdout)
    assert result["ok"], result["error"] + "\n" + "\n".join(result["traceback"])

    golden_dir = _golden_dir(config, eval_script)
    if golden_dir is None:
        return
    golden_file = golden_dir / f"{eval_script.stem}.{policy}.txt"
    observed = render_result(result["final_score"], result["tree"])
    if config.getoption("--update-golden"):
        golden_file.parent.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(observed, encoding="utf-8")
        return
    assert golden_file.exists(), f"missing golden file {golden_file}; run pytest with --update-golden"
    expected = golden_file.read_text(encoding="utf-8")
    if observed != expected:
        diff = list(difflib.unified_diff(expected.splitlines(), observed.splitlines(),
                                         "golden", "observed", lineterm=""))
        shown = "\n".join(diff[:MAX_DIFF_LINES])
        more = f"\n... {len(diff) - MAX_DIFF_LINES} more diff lines" if len(diff) > MAX_DIFF_LINES else ""
        pytest.fail(f"rubric result changed ({golden_file.name}):\n{shown}{more}")


CLOCK_PROBE = """\
from datetime import date, datetime

from mind2web2.evaluator import Evaluator
from mind2web2.verification_tree import AggregationStrategy

LOADED_ON = date.today().isoformat()


async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger,
                          model="o4-mini"):
    print("a script may print")
    evaluator = Evaluator()
    root = evaluator.initialize(
        task_id="clock_probe", strategy=AggregationStrategy.PARALLEL, agent_name=agent_name,
        answer_name=answer_name, client=client, answer=answer, global_cache=cache,
        global_semaphore=semaphore, logger=logger, default_model=model,
    )
    evaluator.add_custom_node(result=True, id=f"loaded_{LOADED_ON}", desc="date at import", parent=root)
    evaluator.add_custom_node(result=True, id=f"ran_{datetime.now():%Y-%m-%d_%H%M}",
                              desc="local time during the run", parent=root)
    return evaluator.get_summary()
"""


def test_scripts_see_a_fixed_clock_in_utc(tmp_path):
    """A script sees 2026-09-22 12:00 UTC, at import and while it runs, whatever the
    host's date and time zone, and what it prints stays out of the JSON on stdout."""
    script = tmp_path / "clock_probe.py"
    script.write_text(CLOCK_PROBE, encoding="utf-8")
    # 12:00 UTC is already the next day in Auckland, so a run in the host time zone fails
    env = {**os.environ, "TZ": "Pacific/Auckland"}
    cmd = [sys.executable, str(OFFLINE_EVAL), "--script", str(script), "--policy", "all_true"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    result = json.loads(proc.stdout)
    assert result["ok"], result["error"]
    assert [child["id"] for child in result["tree"]["children"]] == [
        "loaded_2026-09-22", "ran_2026-09-22_1200"]
