"""The evidence each check records in the rubric tree, and the HTML report of an agent's results.

Checks run with the fake judge and synthetic pages of ``offline_eval``; the
report's end-to-end tests run ``mind2web2 evaluate`` as in ``test_evaluate``.
"""
from __future__ import annotations

import asyncio
import logging
import re

from mind2web2 import Evaluator, cli
from mind2web2.eval_toolkit import shared_browser
from mind2web2.report import AnswerEntry, render_report

from offline_eval import FakeLLMClient, SyntheticCache
from test_evaluate import RecordingBrowser, run_evaluate  # noqa: F401  (run_evaluate is a fixture)


class FailingJudge(FakeLLMClient):
    def _verdict(self, text: str) -> bool:
        return False


def verify_all(client, calls):
    """Run ``calls(evaluator, root)`` with a fresh evaluator; return the root node."""
    async def main():
        evaluator = Evaluator()
        root = evaluator.initialize(task_id="task", agent_name="agent", answer_name="answer_1.md", client=client,
                                    task_description="Name a source.", answer="A", global_cache=SyntheticCache(),
                                    global_semaphore=asyncio.Semaphore(4), logger=logging.getLogger("test.report"))
        with shared_browser(RecordingBrowser()):
            await calls(evaluator, root)
        return root
    return asyncio.run(main())


# ------------------------------------------------------------------ evidence

def test_each_verified_leaf_records_its_claim_sources_votes_and_reasoning():
    async def calls(ev, root):
        seq = ev.add_sequential(id="steps", desc="Steps", parent=root)
        first = ev.add_leaf(id="named", desc="A source is named.", parent=seq)
        await ev.verify(claim="The answer names a source.", node=first)
        second = ev.add_leaf(id="cited", desc="The source supports it.", parent=seq)
        await ev.verify(claim="X is Y.", node=second, sources="https://s.example/1")

    root = verify_all(FakeLLMClient("all_true"), calls)
    named, cited = root.children[0].children
    assert named.evidence == {"claim": "The answer names a source.", "sources": [],
                              "checks": [{"url": None, "passed": True, "votes": [True, True],
                                          "reasoning": "offline"}]}
    assert cited.evidence["sources"] == ["https://s.example/1"]
    assert cited.evidence["checks"][0]["url"] == "https://s.example/1"
    assert root.evidence is None and root.children[0].evidence is None


def test_a_multi_url_check_records_every_source_checked_and_a_skipped_check_its_prerequisite():
    async def calls(ev, root):
        seq = ev.add_sequential(id="steps", desc="Steps", parent=root)
        sources = ev.add_leaf(id="sources", desc="A source supports it.", parent=seq)
        await ev.verify(claim="X is Y.", node=sources, sources=["https://s.example/1", "https://s.example/2"])
        later = ev.add_leaf(id="later", desc="Depends on it.", parent=seq)
        await ev.verify(claim="Z.", node=later, sources="https://s.example/3")

    root = verify_all(FailingJudge("all_true"), calls)
    sources, later = root.children[0].children
    assert sorted(c["url"] for c in sources.evidence["checks"]) == ["https://s.example/1", "https://s.example/2"]
    assert not any(c["passed"] for c in sources.evidence["checks"])
    assert later.status == "skipped"
    assert later.evidence == {"claim": "Z.", "sources": ["https://s.example/3"], "checks": [],
                              "skipped_because": "sources"}


# ------------------------------------------------------------------ report

def test_evaluate_writes_the_report_with_scores_and_evidence_and_no_summary_json(tmp_path, run_evaluate, capsys):
    assert run_evaluate() == 0
    report = tmp_path / "results" / "agent" / "report.html"
    assert f"Browse the results and the evidence of each check: {report}" in capsys.readouterr().out
    page = report.read_text()
    assert page.count('class="answer-card card"') == 4  # t1 and t2, two runs each
    assert 'href="#a-t1-2">1.00</a>' in page
    assert "The answer names a source." in page and 'href="https://a.example/1"' in page
    assert not list((tmp_path / "results").rglob("summary.json"))

    out = tmp_path / "t1.html"
    assert cli.main(["report", "agent", "--results-dir", str(tmp_path / "results"), "--task", "t1",
                     "--output", str(out)]) == 0
    assert out.read_text().count('class="answer-card card"') == 2


def test_report_without_evaluated_answers_exits_with_1(tmp_path, capsys):
    assert cli.main(["report", "nobody", "--results-dir", str(tmp_path)]) == 1
    assert "No report written" in capsys.readouterr().err


def entry(**result) -> AnswerEntry:
    return AnswerEntry("t1", "answer_1.md", 1, {"final_score": 0.0, **result}, None, "<b>answer</b>")


def test_the_report_escapes_answers_and_judge_output_and_links_only_http_urls():
    evidence = {"claim": "<img src=x onerror=alert(1)>", "sources": ["javascript:alert(1)"],
                "checks": [{"url": "javascript:alert(1)", "passed": False, "votes": [False],
                            "reasoning": "</div><script>alert(1)</script>"}]}
    tree = {"id": "root", "desc": "<svg onload=alert(1)>", "status": "failed", "score": 0.0,
            "strategy": "parallel", "critical": False, "children": [], "evidence": evidence}
    page = render_report("<agent>", [entry(eval_breakdown=[{"verification_tree": tree}])])
    assert "<script>alert" not in page and "<img src=x" not in page and "<svg onload" not in page
    assert 'href="javascript:' not in page and "<b>answer</b>" not in page
    nonce = re.search(r"script-src 'nonce-([^']+)'", page).group(1)
    assert re.findall(r"<script[^>]*>", page) == [f'<script nonce="{nonce}">']


def test_a_result_saved_without_evidence_says_so():
    tree = {"id": "root", "desc": "d", "status": "passed", "score": 1.0, "strategy": "parallel",
            "critical": False, "children": []}
    page = render_report("agent", [entry(final_score=1.0, eval_breakdown=[{"verification_tree": tree}]),
                                   AnswerEntry("t2", "answer_1.md", 1, None, None, None)])
    assert "saved without the evidence of its checks" in page
    assert "This answer has no result" in page
