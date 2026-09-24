# mind2web2 Package

Core evaluation framework for the Mind2Web-2 benchmark.

## Module Responsibilities

### evaluator.py — High-Level Evaluation Orchestrator
The `Evaluator` class is the primary API that task-specific eval scripts interact with. It provides:
- `initialize()`: Sets up the verification tree root, creates Extractor + Verifier instances
- `extract()`: Routes extraction to either answer-based or URL-based extraction
- `verify()`: Routes verification based on source type (none, single URL, multiple URLs)
- `batch_verify()`: Parallel verification of multiple claims
- `add_custom_node()` / `add_parallel()` / `add_sequential()` / `add_leaf()`: Build the verification tree
- `get_summary()`: Returns the final evaluation result dict

**Key patterns:**
- Automatic sequential dependency detection: nodes in `SEQUENTIAL` parents auto-skip if predecessors fail
- Critical node gating: if a `critical=True` sibling fails, dependent nodes are skipped
- Thread-safe unique ID generation for nodes

### eval_toolkit.py — Extractor & Verifier (LLM-Powered)
Two classes that do the actual LLM-based work:

**`Extractor`**: Extracts structured info from answer text or webpage content
- `simple_extract()`: From the answer markdown
- `extract_from_url()`: From a webpage (fetches text + screenshot, passes to LLM)

**`Verifier`**: Verifies claims as true/false using LLM
- `simple_verify()`: Factual/logical check without external evidence
- `verify_by_url()`: Check claim against a single webpage
- `verify_by_urls()`: Check claim against multiple URLs (first-success short-circuit)
- Uses majority vote (default 3 trials) for robustness

**`BaseEvaluator`**: Shared parent with page retrieval, image processing, LLM call management. `call_llm_with_semaphore()` records each judge request in the answer's `JudgeUsage` (shared by Extractor and Verifier). A `JudgeError` is never converted into a failed verification or an empty extraction; it propagates, and `eval_runner` refuses to save a result for an answer with any failed judge request.

**`create_evaluator()`**: Factory function to create paired Extractor + Verifier instances

### verification_tree.py — Rubric Tree Data Structure
`VerificationNode` (Pydantic model) with:
- Binary leaf scores (0.0 or 1.0)
- `AggregationStrategy.PARALLEL`: gate-then-average (critical nodes gate, soft nodes averaged)
- `AggregationStrategy.SEQUENTIAL`: short-circuit on first failure
- `compute_score(mutate=True)`: Recursive score computation with write-back

### eval_runner.py — Async Execution Engine
- `evaluate_task()`: Evaluates the `answer_<k>.md` files of one task (loads eval script, manages cache, runs answers concurrently) and keeps a copy of each answer and its `.meta.json` next to its results. It reuses an answer's latest result only when the result records the same answer (SHA-256 of the file), the same judge configuration, the same eval script (SHA-256), the same default `EvaluatorConfig` settings, and the same `results.SCORING_VERSION`; raise `SCORING_VERSION` in any framework change that can change scores without changing a script or an `EvaluatorConfig` default; changes to cached pages are not detected; before evaluating an answer again it moves the earlier results to `results/superseded/`
- `_eval_one_answer()`: Evaluates a single answer file
- `DualSemaphore`: Wrapper holding both webpage and LLM semaphores

### submission.py, results.py, metrics.py — Submissions, Results, Leaderboard Metrics
- `submission.py`: the answers layout (`<agent>/<task_id>/answer_<k>.md` with k = 1, 2, 3, ..., optional `answer_<k>.meta.json`), task lists (CSV with a `task_id` column, text file, or eval-script directory), and `validate_submission()`
- `results.py`: the results layout (`<results>/<agent>/<task_id>/answer_<k>/results/<timestamp>_answer_<k>.md.json`) and lookup of an answer's latest result
- `metrics.py`: Partial Completion, Success Rate, Pass@k, Time, and Answer Length over a task list; a missing answer or result counts as 0 in the first three and is listed

### cli/ — The `mind2web2` Command
One module per subcommand (`validate`, `metrics`); each defines `register(subparsers)` and `run(args) -> int`. Shared options live in `cli/_common.py`.

## Data Flow

```
run_eval.py
  → eval_runner.evaluate_task()
    → load_eval_script() → dynamically load per-task evaluate_answer()
    → _eval_one_answer()
      → eval_fn(client, answer, cache, semaphore, logger, model)
        → Evaluator.initialize() + extract() + verify()
        → Evaluator.get_summary() → result dict
    → save results JSON + summary
```

## Eval Script Contract
Each task's eval script must define:
```python
async def evaluate_answer(client, answer, agent_name, answer_name, cache, semaphore, logger, model="o4-mini") -> dict
```
It typically creates an `Evaluator`, builds a verification tree, runs extractions/verifications, and returns `evaluator.get_summary()`. `eval_runner` always passes `model` (the configured judge model), so the default in the signature is not used.
