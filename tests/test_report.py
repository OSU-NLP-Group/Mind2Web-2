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
from mind2web2.report import AnswerEntry, Thumbnails, render_report
from mind2web2.utils.cache_filesys import CacheFileSys

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


def test_a_check_that_fails_with_an_error_records_its_sources_and_no_earlier_evidence():
    async def calls(ev, root):
        leaf = ev.add_leaf(id="cited", desc="The source supports it.", parent=root)
        await ev.verify(claim="X is Y.", node=leaf, sources="https://s.example/1")

        async def broken(**kwargs):
            raise RuntimeError("the page parser crashed")
        ev.verifier.verify_by_url = broken
        await ev.verify(claim="X is Z.", node=leaf, sources="https://s.example/2")  # the same node again
        other = ev.add_leaf(id="other", desc="Unreadable sources.", parent=root)
        await ev.verify(claim="W.", node=other, sources=42)

    root = verify_all(FakeLLMClient("all_true"), calls)
    cited, other = root.children
    assert cited.evidence == {"claim": "X is Z.", "sources": ["https://s.example/2"], "checks": [],
                              "error": "RuntimeError: the page parser crashed"}
    assert (other.evidence["sources"], other.evidence["error"].split(":")[0]) == ([], "TypeError")


class FailsTheFirstStep(FakeLLMClient):
    def _verdict(self, text: str) -> bool:
        return "The first step holds." not in text


def test_a_leaf_that_passed_before_an_earlier_step_failed_records_why_it_counts_as_skipped():
    async def calls(ev, root):
        seq = ev.add_sequential(id="steps", desc="Steps", parent=root)
        first = ev.add_leaf(id="first", desc="First step.", parent=seq)
        second = ev.add_leaf(id="second", desc="Second step.", parent=seq)
        await ev.verify(claim="The second step holds.", node=second)  # verified out of order
        await ev.verify(claim="The first step holds.", node=first)

    root = verify_all(FailsTheFirstStep("all_true"), calls)
    root.compute_score(mutate=True)
    first, second = root.children[0].children
    assert (first.status, second.status) == ("failed", "skipped")
    assert second.evidence["checks"][0]["passed"] is True
    assert second.evidence["skipped_because"] == "first"


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


def test_a_report_that_fails_to_render_does_not_stop_evaluate(tmp_path, monkeypatch, capsys):
    import argparse
    from mind2web2.cli import evaluate as evaluate_command

    def broken(results_root, agent, cache_root=None):
        raise KeyError("final_score")
    monkeypatch.setattr(evaluate_command, "write_report", broken)
    evaluate_command._write_report(argparse.Namespace(results_dir=tmp_path, agent="agent", cache_dir=None))
    assert "Report not written: KeyError: 'final_score'" in capsys.readouterr().err


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


def test_the_report_shows_what_the_metrics_flag_changed_answers_and_mixed_judges():
    tree = {"id": "root", "desc": "d", "status": "passed", "score": 1.0, "strategy": "parallel",
            "critical": False, "children": []}
    metrics = {"num_tasks": 1, "num_runs": 1, "stale_results": [{"task_id": "t1", "run": 1}],
               "judge_models": {"judge-a": 1, "judge-b": 1}, "served_models": {}}
    page = render_report("agent", [entry(final_score=1.0, eval_breakdown=[{"verification_tree": tree}])], metrics)
    assert "1 answers changed since their result" in page
    assert "Results come from different judge models (judge-a: 1, judge-b: 1)" in page
    assert '>changed</a>' in page and ">1.00</a>" not in page
    assert "with <code>--overwrite</code> to record it" in page
    assert '<span class="badge none">changed</span>' in page and "The answer file changed" in page
    assert 'data-below="0"' not in page  # the changed answer counts as below 1.0 in the filter


def test_the_mean_of_a_task_counts_runs_without_a_result_as_0():
    tree = {"id": "root", "desc": "d", "status": "passed", "score": 1.0, "strategy": "parallel",
            "critical": False, "children": []}
    scored = AnswerEntry("t1", "answer_1.md", 1, {"final_score": 1.0, "eval_breakdown": [{"verification_tree": tree}]},
                         None, "a")
    unscored = AnswerEntry("t1", "answer_2.md", 2, None, None, "b")
    other_task = AnswerEntry("t2", "answer_1.md", 1, {"final_score": 0.5}, None, "c")
    perfect_once = AnswerEntry("t3", "answer_1.md", 1, {"final_score": 1.0}, None, "d")
    page = render_report("agent", [scored, unscored, other_task, perfect_once])
    means = re.findall(r'<td class=num>([0-9.]+)</td><td class="meanbar">', page)
    assert means == ["0.500", "0.250", "0.500"]  # t2 and t3 have no answer in run 2
    # A run without an answer puts the task below 1.0 for the filter, as its mean says
    assert '<tr data-task="t3" data-below="1"' in page


def leaf(node_id: str, status: str, score: float, evidence: dict) -> dict:
    return {"id": node_id, "desc": f"{node_id} holds", "status": status, "score": score, "strategy": "parallel",
            "critical": True, "children": [], "evidence": evidence}


def lineage(page_passes: bool = False, url: str = "https://a.example/page") -> dict:
    """A tree whose first check passes, whose page check fails (unless ``page_passes``), and whose third is skipped."""
    children = [
        leaf("named", "passed", 1.0, {"claim": "The answer names X.", "sources": [],
                                      "checks": [{"url": None, "passed": True, "votes": [True], "reasoning": "yes"}]}),
        leaf("supported", "passed" if page_passes else "failed", 1.0 if page_passes else 0.0,
             {"claim": "The page says X.", "sources": [url],
              "checks": [{"url": url, "passed": page_passes, "votes": [page_passes, page_passes],
                          "reasoning": "The page does not mention X."}]}),
        leaf("dated", "skipped", 0.0, {"claim": "X is from 2020.", "sources": [], "checks": [],
                                       "skipped_because": "supported"}),
    ]
    return {"id": "root", "desc": "root", "status": "partial", "score": 1 / 3, "strategy": "sequential",
            "critical": False, "children": children}


def scored(task_id: str, run: int, tree: dict, score: float) -> AnswerEntry:
    return AnswerEntry(task_id, f"answer_{run}.md", run,
                       {"final_score": score, "eval_breakdown": [{"verification_tree": tree}]}, None, "answer")


def test_an_answer_card_leads_with_the_checks_that_lost_points():
    page = render_report("agent", [scored("t1", 1, lineage(), 1 / 3)])
    summary = page[page.index('<details class="answer-card'):page.index('<div class="card-body">')]
    assert summary.count('<i class="sq ') == 3 and '<i class="sq failed" title="supported: failed">' in summary
    assert "1 passed · 1 failed · 1 skipped" in summary and "lost at <code>supported</code>" in summary
    assert 'aria-label="checks: named passed, supported failed, dated skipped"' in summary
    lost = page[page.index('<section class="lost">'):page.index("</section>", page.index('<section class="lost">'))]
    assert "Where points were lost (1 failed check)" in lost
    assert "The page says X." in lost and "The page does not mention X." in lost and "named" not in lost
    assert 'skipped: depends on <code>supported</code>' in page  # a skipped check takes one line in the tree
    assert "most frequent failure: <code>supported</code> failed in 1 of 1 scored run" in page


def test_the_charts_count_answers_by_score_and_checks_by_status():
    page = render_report("agent", [scored("t1", 1, lineage(), 1 / 3), scored("t1", 2, lineage(True), 1.0),
                                   AnswerEntry("t2", "answer_1.md", 1, None, None, None)])
    answers = page[page.index("Answers by score"):page.index("Checks of the scored answers")]
    assert "1.0 <b>1</b>" in answers and "between 0 and 1 <b>1</b>" in answers
    assert "no result or changed <b>1</b>" in answers and "<title>0:" not in answers
    checks = page[page.index("Checks of the scored answers"):page.index('id="scores"')]
    assert "passed <b>3</b>" in checks and "failed <b>1</b>" in checks and "skipped <b>2</b>" in checks


def test_a_failed_page_check_shows_a_thumbnail_of_its_cached_screenshot_embedded_once(tmp_path):
    from PIL import Image
    import io
    image = io.BytesIO()
    Image.new("RGB", (1100, 3000), "white").save(image, format="PNG")
    CacheFileSys(str(tmp_path / "agent" / "t1")).put_web("https://a.example/page", "text", image.getvalue())
    thumbnails = Thumbnails(tmp_path / "agent")
    entries = [scored("t1", 1, lineage(), 1 / 3), scored("t1", 2, lineage(), 1 / 3),
               scored("t1", 3, lineage(url="https://a.example/not-cached"), 1 / 3),
               scored("t9", 1, lineage(), 1 / 3)]
    page = render_report("agent", entries, thumbnails=thumbnails)
    assert page.count("<symbol ") == 1 and page.count("data:image/jpeg;base64,") == 1
    assert page.count('<use href="#th0"/>') == 2  # runs 1 and 2 cite the page; run 3's page is not cached
    assert '<symbol id="th0" viewBox="0 0 480 720">' in page  # scaled to 480 wide, the top 720 pixels
    assert not (tmp_path / "agent" / "t9").exists()  # a task without a cache gets no thumbnail and no cache


def test_thumbnails_stop_at_their_budget_and_the_page_says_so(tmp_path):
    from PIL import Image
    import io
    cache = CacheFileSys(str(tmp_path / "agent" / "t1"))
    for n in range(2):
        image = io.BytesIO()
        Image.effect_noise((1100, 800), 60 + n).convert("RGB").save(image, format="PNG")
        cache.put_web(f"https://a.example/{n}", "text", image.getvalue())
    first = Thumbnails(tmp_path / "agent")
    first.get("t1", "https://a.example/0")
    thumbnails = Thumbnails(tmp_path / "agent", budget=first.used + 100)  # room for the first page only
    entries = [scored("t1", 1, lineage(url="https://a.example/0"), 1 / 3),
               scored("t1", 2, lineage(url="https://a.example/0"), 1 / 3),
               scored("t1", 3, lineage(url="https://a.example/1"), 1 / 3)]
    page = render_report("agent", entries, thumbnails=thumbnails)
    assert page.count("<symbol ") == 1 and page.count('<use href="#th0"/>') == 2 and thumbnails.over_budget
    assert "Page thumbnails stop after" in page
