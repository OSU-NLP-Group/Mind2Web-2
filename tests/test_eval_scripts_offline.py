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
import subprocess
import sys
from pathlib import Path

import pytest

from offline_eval import POLICIES, render_result

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
OFFLINE_EVAL = TESTS_DIR / "offline_eval.py"
DEV_SET_DIR = REPO_ROOT / "eval_scripts" / "dev_set"
DEFAULT_GOLDEN_DIR = TESTS_DIR / "golden" / "offline"
MAX_DIFF_LINES = 40


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT)
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
