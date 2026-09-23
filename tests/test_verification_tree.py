"""Score aggregation of rubric trees (paper §3.3: gate-then-average, sequential short-circuit)."""
import pytest

from mind2web2.verification_tree import AggregationStrategy, VerificationNode


def leaf(node_id: str, score: float, *, critical: bool = False) -> VerificationNode:
    return VerificationNode(id=node_id, desc=node_id, critical=critical, score=score,
                            status="passed" if score == 1.0 else "failed")


def parent(node_id: str, children, *, critical: bool = False,
           strategy: AggregationStrategy = AggregationStrategy.PARALLEL) -> VerificationNode:
    node = VerificationNode(id=node_id, desc=node_id, critical=critical, strategy=strategy)
    for child in children:
        node.add_node(child)
    return node


def test_parallel_averages_non_critical_children():
    root = parent("root", [leaf("a", 1.0), leaf("b", 0.0), leaf("c", 1.0), leaf("d", 1.0)])
    assert root.compute_score() == pytest.approx(0.75)


def test_failed_critical_child_gates_parent_to_zero():
    root = parent("root", [leaf("gate", 0.0, critical=True), leaf("a", 1.0), leaf("b", 1.0)])
    assert root.compute_score() == 0.0


def test_passed_critical_children_leave_average_of_non_critical():
    root = parent("root", [leaf("gate", 1.0, critical=True), leaf("a", 1.0), leaf("b", 0.0)])
    assert root.compute_score() == pytest.approx(0.5)


def test_only_critical_children_all_passed_scores_one():
    root = parent("root", [leaf("g1", 1.0, critical=True), leaf("g2", 1.0, critical=True)])
    assert root.compute_score() == 1.0


def test_nested_critical_gate_propagates():
    passed_gate = parent("gate", [leaf("g1", 1.0, critical=True), leaf("g2", 1.0, critical=True)],
                         critical=True)
    failed_gate = parent("gate", [leaf("g1", 1.0, critical=True), leaf("g2", 0.0, critical=True)],
                         critical=True)
    soft = parent("soft", [leaf("x", 1.0), leaf("y", 0.0)])
    assert parent("root", [passed_gate, soft]).compute_score() == pytest.approx(0.5)
    assert parent("root", [failed_gate, soft]).compute_score() == 0.0


def test_sequential_zeroes_children_after_first_imperfect_child():
    steps = [leaf("s1", 1.0), parent("s2", [leaf("s2a", 1.0), leaf("s2b", 0.0)]), leaf("s3", 1.0)]
    root = parent("root", steps, strategy=AggregationStrategy.SEQUENTIAL)
    # s2 scores 0.5, so s3 is zeroed even though it passed on its own: (1 + 0.5 + 0) / 3.
    assert root.compute_score(mutate=True) == pytest.approx(0.5)
    assert root.children[2].score == 0.0
    assert root.children[2].status == "skipped"


def test_compute_score_without_mutation_matches_mutating_result():
    def build():
        steps = [leaf("s1", 1.0), leaf("s2", 0.0), leaf("s3", 1.0)]
        return parent("root", steps, strategy=AggregationStrategy.SEQUENTIAL)

    pure_tree, mutated_tree = build(), build()
    pure = pure_tree.compute_score(mutate=False)
    assert pure == mutated_tree.compute_score(mutate=True) == pytest.approx(1 / 3)
    # The pure computation leaves the tree untouched.
    assert pure_tree.children[2].score == 1.0
    assert pure_tree.score == 0.0


def test_parent_status_reflects_children():
    assert parent("p", [leaf("a", 1.0), leaf("b", 1.0)]).compute_score(mutate=True) == 1.0
    passed = parent("p", [leaf("a", 1.0)])
    passed.compute_score(mutate=True)
    assert passed.status == "passed"
    failed = parent("p", [leaf("a", 0.0)])
    failed.compute_score(mutate=True)
    assert failed.status == "failed"
    partial = parent("p", [leaf("a", 1.0), leaf("b", 0.0)])
    partial.compute_score(mutate=True)
    assert partial.status == "partial"


def test_critical_parent_rejects_non_critical_child():
    gate = VerificationNode(id="gate", desc="gate", critical=True)
    with pytest.raises(ValueError):
        gate.add_node(leaf("soft", 1.0))
