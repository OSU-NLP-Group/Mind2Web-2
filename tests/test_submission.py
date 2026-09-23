"""Submission layout: task lists, answer metadata, validation, and the ``mind2web2 validate`` command."""
from __future__ import annotations

from pathlib import Path

import pytest

from mind2web2.cli import main as cli_main
from mind2web2.submission import (
    AnswerFile, MetadataError, TaskInfo, list_answer_files, load_metadata, load_task_list, validate_submission,
)

CITED = "The answer, see https://example.com/page"


def make_task(agent_dir: Path, task_id: str, files: dict[str, str | bytes]) -> None:
    task_dir = agent_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = task_dir / name
        path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)


def test_task_list_formats(tmp_path):
    csv_file = tmp_path / "split.csv"
    csv_file.write_text('task_id,task_description,domain,subdomain\n'
                        'a,"Find, with commas",Shopping,Deals\nb,x,,\na,dup,Other,Other\n')
    assert load_task_list(csv_file) == [TaskInfo("a", "Shopping", "Deals"), TaskInfo("b")]

    txt_file = tmp_path / "tasks.txt"
    txt_file.write_text("# comment\nb\n\na\nb\n")
    assert [t.task_id for t in load_task_list(txt_file)] == ["b", "a"]

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("z.py", "y.py", "notes.txt"):
        (scripts / name).write_text("")
    assert [t.task_id for t in load_task_list(scripts)] == ["y", "z"]


def test_csv_task_list_needs_task_id_column(tmp_path):
    csv_file = tmp_path / "split.csv"
    csv_file.write_text("id,description\na,x\n")
    with pytest.raises(ValueError, match="task_id"):
        load_task_list(csv_file)


def test_unreadable_csv_task_lists_raise_value_error(tmp_path):
    short_rows = tmp_path / "short.csv"
    short_rows.write_text("domain,task_id\nShopping,a\nTravel\n")  # the second row has no task_id
    assert load_task_list(short_rows) == [TaskInfo("a", "Shopping")]
    oversized = tmp_path / "oversized.csv"
    oversized.write_text("task_id,notes\na," + "x" * 200_000 + "\n")  # beyond the csv field size limit
    with pytest.raises(ValueError, match="not a valid CSV file"):
        load_task_list(oversized)


def test_only_answer_k_md_files_are_answers(tmp_path):
    make_task(tmp_path, "t", {"answer_2.md": CITED, "answer_10.md": CITED, "answer.md": CITED,
                              "answer_1.md.bak": CITED, "answer_1.meta.json": "{}"})
    assert [(a.run, a.name) for a in list_answer_files(tmp_path / "t")] == [(2, "answer_2.md"), (10, "answer_10.md")]


def test_run_numbers_start_at_1_without_leading_zeros(tmp_path):
    """``answer_01.md`` would be a second file for run 1, and run 0 is never scored."""
    make_task(tmp_path, "t", {"answer_1.md": CITED, "answer_2.md": CITED, "answer_01.md": CITED,
                              "answer_0.md": CITED, "answer_01.meta.json": "{}"})
    assert [a.name for a in list_answer_files(tmp_path / "t")] == ["answer_1.md", "answer_2.md"]
    issues = validate_submission(tmp_path)
    assert {(i.level, i.path) for i in issues} == {
        ("warning", "t/answer_0.md"), ("warning", "t/answer_01.md"), ("warning", "t/answer_01.meta.json")}
    assert all("evaluation ignores it" in i.message for i in issues)


def test_metadata_is_validated(tmp_path):
    make_task(tmp_path, "t", {"answer_1.md": CITED, "answer_1.meta.json": '{"time_seconds": 42.5, "model": "x"}',
                              "answer_2.md": CITED, "answer_2.meta.json": '{"time_seconds": -1}',
                              "answer_3.md": CITED, "answer_3.meta.json": "not json"})
    first, second, third = list_answer_files(tmp_path / "t")
    metadata = load_metadata(first)
    assert metadata.time_seconds == 42.5 and metadata.model_extra == {"model": "x"}
    with pytest.raises(MetadataError, match="time_seconds"):
        load_metadata(second)
    with pytest.raises(MetadataError, match="not valid JSON"):
        load_metadata(third)
    assert load_metadata(AnswerFile("t", 4, tmp_path / "t" / "answer_4.md")) is None


def test_validate_submission_reports_each_problem(tmp_path):
    agent_dir = tmp_path / "agent"
    make_task(agent_dir, "complete", {f"answer_{k}.md": CITED for k in (1, 2, 3)})
    make_task(agent_dir, "gappy", {"answer_1.md": CITED, "answer_3.md": "   ",
                                   "answer_3.meta.json": '{"time_seconds": "slow"}',
                                   "answer_4.meta.json": "{}", "notes.md": "x", ".DS_Store": "x"})
    make_task(agent_dir, "uncited", {"answer_1.md": "No links here.", "answer_2.md": CITED,
                                     "answer_3.md": b"\xff\xfe binary"})
    make_task(agent_dir, "extra", {"answer_1.md": CITED})

    issues = validate_submission(agent_dir, ["complete", "gappy", "uncited", "absent"], num_runs=3)
    found = {(i.level, i.path) for i in issues}
    assert found == {
        ("warning", "extra"),                        # not in the task list
        ("warning", "absent"),                       # no task directory
        ("warning", "gappy"),                        # run 2 missing
        ("warning", "gappy/answer_3.md"),            # empty
        ("error", "gappy/answer_3.meta.json"),       # time_seconds is not a number
        ("warning", "gappy/answer_4.meta.json"),     # metadata without an answer
        ("warning", "gappy/notes.md"),               # ignored by evaluation
        ("warning", "uncited/answer_1.md"),          # cites no URL
        ("error", "uncited/answer_3.md"),            # not UTF-8
    }


def test_validate_submission_without_task_list_checks_every_task(tmp_path):
    agent_dir = tmp_path / "agent"
    make_task(agent_dir, "a", {"answer_1.md": CITED, "answer_2.md": CITED})
    make_task(agent_dir, "b", {"answer_1.md": CITED})
    issues = validate_submission(agent_dir)
    assert [(i.path, i.message) for i in issues] == [("b", "no answer for run(s) 2; they score 0")]
    assert validate_submission(tmp_path / "missing")[0].level == "error"


def test_validate_command_exit_status(tmp_path, capsys):
    agent_dir = tmp_path / "answers" / "agent"
    make_task(agent_dir, "a", {"answer_1.md": CITED, "answer_1.meta.json": '{"time_seconds": 3}'})
    argv = ["validate", "agent", "--answers-dir", str(tmp_path / "answers")]
    assert cli_main(argv) == 0
    out = capsys.readouterr().out
    assert "1 tasks, 1 answers (runs 1-1), 1 with time metadata" in out
    assert "0 error(s), 0 warning(s)." in out

    make_task(agent_dir, "a", {"answer_2.md": CITED, "answer_2.meta.json": "[]"})
    assert cli_main(argv) == 1
    assert "ERROR    a/answer_2.meta.json" in capsys.readouterr().out


def test_validate_command_reports_an_unreadable_task_list(tmp_path, capsys):
    make_task(tmp_path / "answers" / "agent", "a", {"answer_1.md": CITED})
    argv = ["validate", "agent", "--answers-dir", str(tmp_path / "answers"),
            "--task-list", str(tmp_path / "missing.csv")]
    assert cli_main(argv) == 1
    assert "Cannot read the task list" in capsys.readouterr().err
