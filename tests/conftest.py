from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_addoption(parser):
    group = parser.getgroup("offline eval scripts")
    group.addoption(
        "--eval-scripts-dir", type=Path, default=REPO_ROOT / "eval_scripts" / "dev_set",
        help="Directory of eval scripts to run offline (default: eval_scripts/dev_set).",
    )
    group.addoption(
        "--answers-dir", type=Path, default=REPO_ROOT / "answers" / "example",
        help="Directory laid out as <task_id>/answer_1.md; tasks without a file use a default answer.",
    )
    group.addoption(
        "--golden-dir", type=Path, default=None,
        help="Directory of golden results. Defaults to tests/golden/offline for the dev set; "
             "other script directories are only checked for crashes unless this is given.",
    )
    group.addoption(
        "--update-golden", action="store_true",
        help="Write the current results as the new golden files instead of comparing.",
    )
    group.addoption(
        "--script-timeout", type=float, default=60.0,
        help="Seconds before a script run is killed and reported as hanging (default: 60).",
    )


def pytest_generate_tests(metafunc):
    if "eval_script" in metafunc.fixturenames:
        scripts_dir = metafunc.config.getoption("--eval-scripts-dir")
        scripts = sorted(scripts_dir.glob("*.py"))
        metafunc.parametrize("eval_script", scripts, ids=[s.stem for s in scripts])
