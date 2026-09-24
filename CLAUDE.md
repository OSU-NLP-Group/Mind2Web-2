# Mind2Web-2: Evaluation Framework for Agentic Search

## Project Overview

Mind2Web-2 is a benchmark and evaluation toolkit for agentic search systems (e.g., OpenAI Deep Research). It evaluates how well AI agents can autonomously browse the web, gather information, and synthesize long-form answers with proper source attribution.

The core innovation is **Agent-as-a-Judge**: task-specific LLM judge agents that use tree-structured rubrics to automatically assess answer correctness and source attribution.

**Paper**: "Mind2Web 2: Evaluating Agentic Search with Agent-as-a-Judge" (NeurIPS'25 D&B)

## Architecture Overview

```
run_eval.py                  # CLI entrypoint for evaluation
batch_answer_cache.py        # Pre-caches webpages referenced in answers
cache_all_answers.sh         # Shell wrapper to batch-cache all answers for an agent

mind2web2/                   # Core Python package
├── evaluator.py             # High-level Evaluator class (extract + verify orchestration)
├── eval_toolkit.py          # Extractor & Verifier classes (LLM-powered extraction/verification)
├── verification_tree.py     # VerificationNode tree with aggregation strategies
├── eval_runner.py           # Async task/answer evaluation orchestrator
├── submission.py            # Answers layout, answer metadata, task lists, submission validation
├── results.py               # Results layout and latest-result lookup
├── metrics.py               # Leaderboard metrics (Partial Completion, Success Rate, Pass@k, Time, Answer Length)
├── cli/                     # The `mind2web2` command (validate, metrics)
├── api_tools/               # External API integrations (arXiv, Google Maps, PDF)
├── llm_client/              # Judge LLM client (OpenAI, Azure OpenAI, OpenAI-compatible servers)
├── utils/                   # Shared utilities (caching, logging, browser, paths)
└── prompts/                 # Prompt templates for LLM extraction

answers/                     # Agent answer files (markdown), organized by agent/task
eval_scripts/                # Per-task evaluation scripts (downloaded from HuggingFace)
cache/                       # Cached webpage content (text + screenshots)
eval_results/                # Evaluation output (JSON results + logs)
cache_manager/               # [DEPRECATED] PySide6 GUI for managing webpage cache
```

## Key Concepts

### Evaluation Pipeline
1. **Agent produces answers** as markdown files with URL citations in `answers/<agent>/<task>/answer_*.md`
2. **Webpages are cached** (text + screenshots) via `batch_answer_cache.py` to ensure reproducibility. URLs whose capture fails are recorded in the task's `failures.json`; evaluation treats them as unavailable and does not capture them again. `batch_answer_cache.py --retry_failed` crawls them again (every failure record, including those evaluation wrote for URLs it captured live), and the Cache Manager lists each one as an issue for a person to capture
3. **Evaluation scripts** (one per task) define `async def evaluate_answer(...)` that builds a verification tree
4. **The judge agent** extracts claims from answers, then verifies each claim against source URLs using LLM calls
5. **Metrics** (`mind2web2 metrics`) aggregate the per-answer scores into the leaderboard metrics

### Verification Tree
- Tree-structured rubric where each node is a `VerificationNode`
- Leaf nodes get binary pass/fail scores via LLM verification
- Two aggregation strategies: `PARALLEL` (weighted average) and `SEQUENTIAL` (short-circuit on failure)
- **Critical nodes**: if any critical child fails, the parent scores 0.0 (acts as a gate)

### Evaluator API (used in eval scripts)
```python
evaluator = Evaluator()
root = evaluator.initialize(task_id=..., agent_name=..., answer_name=...)

# Extract structured info from answer
info = await evaluator.extract(prompt, TemplateClass)

# Verify claims (with optional URL sources)
await evaluator.verify(claim="...", node=leaf_node, sources="https://...")

# Get final result
return evaluator.get_summary()
```

### Concurrency Model
- Async throughout (`asyncio`)
- Semaphores control concurrency at multiple levels: tasks, answers, webpage retrieval, LLM requests
- `DualSemaphore` wraps both webpage and LLM semaphores

## Environment Setup

```bash
uv sync                         # Install dependencies
patchright install               # Install browsers
export OPENAI_API_KEY="..."     # Required for evaluation
```

## Running Evaluation

```bash
# Evaluate all tasks for an agent
uv run run_eval.py --agent_name example

# Evaluate a specific task
uv run run_eval.py --agent_name example --task_id yu_lineage

# Check an agent's answers, and compute its leaderboard metrics from saved results
uv run mind2web2 validate example
uv run mind2web2 metrics example --task-list <split.csv> --num-runs 3
```

## Key Dependencies
- **patchright**: Stealth browser automation for webpage capture (undetected Playwright fork)
- **openai**: LLM API calls (OpenAI / Azure OpenAI)
- **pydantic**: Structured output parsing from LLM responses
- **PyMuPDF (fitz)**: PDF parsing and rendering
- **html2text**: HTML to markdown conversion

## Notes for Developers

- `cache_manager/` is deprecated and scheduled for removal. Do not invest effort there.
- Eval scripts are per-task Python files downloaded from HuggingFace; they are not part of this repo's source.
- The default judge model is `gpt-6-luna` (`DEFAULT_JUDGE_MODEL`, set with `run_eval.py --judge_model`); the paper used `o4-mini`. The judge client sends every request to the configured judge, and each result records the judge and its token usage.
- Cache lookups accept surface variants of a stored URL (scheme, `www.`, UTM parameters, encoding, trailing slash); the rules are in `CacheFileSys.lookup()` and `url_tools.normalize_url_simple()`. Cache writes are on disk as soon as `put_web` / `put_pdf` / `remove` return.
- Judge requests retry transient errors (connection errors, timeouts, HTTP 408/409/429/5xx) within a time budget; any other failure raises `JudgeError`, which must propagate so the answer is left unscored instead of losing points.
