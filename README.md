# Mind2Web 2 [NeurIPS'25 D&B]

Mind2Web 2 is a benchmark for agentic search systems, featuring Agent-as-a-Judge methodology for comprehensive, rigorous, and reliable assessment on **long-horizon** and complex tasks that involve **complex and real-time information synthesis**.

<div align="center">
  <img src="./assets/mind2web2_overview.jpg" alt="Mind2Web 2 Overview" width="800"/>
  <p><em>Mind2Web 2 features realistic and diverse long-horizon web search tasks and a novel Agent-as-a-Judge framework to evaluate complex, time-varying, and citation-backed answers.</em></p>
</div>

## Links

- [Homepage](https://osu-nlp-group.github.io/Mind2Web-2)
- [Leaderboard](https://osu-nlp-group.github.io/Mind2Web-2/#leaderboard)
- [Paper](https://arxiv.org/abs/2506.21506)
- [Dataset & Evaluation Scripts (HuggingFace)](https://huggingface.co/datasets/osunlp/Mind2Web-2)

## Updates

- **2025/10/23**: All evaluation scripts released for both public dev set and test set. See [Run Evaluation Locally](#run-evaluation-locally).
- **2025/07/17**: Submission guideline released. See [Submission Guideline](#submission-guideline).
- **2025/07/14**: Public development set scripts released.
- **2025/06/26**: GitHub repo is live. Manuscript on arXiv.

---

## Submission Guideline

### What You Need

Your agent should produce **markdown answers with URL citations** for each task. The answers should reference the URLs that support the facts in the response — our evaluation checks attribution quality.

> **Tip:** If you don't have an agent framework, try [Hugging Face's Open Deep Research](https://github.com/huggingface/open-deep-research) as a starting point. Use zero-shot or few-shot prompting to ensure the agent provides URL citations.

### Answer Format

Organize your agent's responses like this (see [answers/example](https://github.com/OSU-NLP-Group/Mind2Web-2/tree/main/answers/example)):

```
answers/<agent_name>/
├── <task_id>/
│   ├── answer_1.md
│   ├── answer_2.md
│   └── ...
└── ...
```

### How to Submit

There are two recommended paths:

| Option | What you provide | What we handle |
|--------|-----------------|----------------|
| **A. Full submission** (recommended) | Answers + webpage cache + evaluation results | We verify and publish |
| **B. Answers + cache** | Answers + webpage cache | We run evaluation |
| **C. Answers only** | Answers | We cache webpages and run evaluation |

We also encourage submitting average inference time and answer lengths for better understanding of agent efficiency.

**To submit:** Compress your `answers/<agent_name>/` directory (and `cache/<agent_name>/` if applicable) and email to **m2w2-leaderboard@googlegroups.com**.

The cache directory structure mirrors the answers:

```
cache/<agent_name>/
├── <task_id>/
│   ├── web_cache/    # Cached webpage text + screenshots
│   └── pdf_cache/    # Cached PDF files
└── ...
```

---

## Run Evaluation Locally

### Step 0: Environment Setup

#### Option A: Using uv (Recommended)

[uv](https://docs.astral.sh/uv/) provides faster dependency resolution:

```bash
uv sync
source .venv/bin/activate   # Windows: .venv\Scripts\activate
rebrowser_playwright install
```

#### Option B: Using conda + pip

```bash
conda create -n mind2web2 python=3.11
conda activate mind2web2
pip install -e .
rebrowser_playwright install
```

### Step 1: Prepare Answers

Place your agent's responses in the `answers/` directory:

```
answers/<agent_name>/<task_id>/answer_*.md
```

### Step 2: Set API Keys

```bash
export OPENAI_API_KEY="YOUR_OPENAI_KEY"

# Optional: Azure OpenAI
export AZURE_OPENAI_API_KEY="YOUR_AZURE_OPENAI_API_KEY"
export AZURE_OPENAI_ENDPOINT_URL="YOUR_AZURE_OPENAI_ENDPOINT_URL"
export AZURE_OPENAI_API_VERSION="2025-03-01-preview"

# Optional: Required for tasks that use Google Maps
export GOOGLE_MAPS_API_KEY="YOUR_GOOGLE_MAPS_API_KEY"
```

### Step 3: Pre-cache Webpages (Recommended)

Pre-caching webpages significantly reduces evaluation time (live fetching during evaluation is slow):

```bash
./cache_all_answers.sh <agent_name>
```

Some pages may fail to cache automatically due to CAPTCHAs, anti-bot protection, or login walls. Use the **Cache Manager** web tool to review and fix these:

```bash
# Start the Cache Manager web UI
uv run python3 cache_manager_web/run.py cache/<agent_name>
```

The Cache Manager provides:
- **Issue detection** — automatically flags pages with problems (CAPTCHA, access denied, etc.)
- **Side-by-side preview** — view cached screenshots and extracted text
- **Live recapture** — open the page in a real browser, solve any verification, then capture back using the Chrome extension
- **Progress tracking** — mark URLs as reviewed and track completion

See [`cache_manager_web/README.md`](cache_manager_web/README.md) for full documentation, keyboard shortcuts, and Chrome extension setup.

### Step 4: Run Evaluation

Download the evaluation scripts from [HuggingFace](https://huggingface.co/datasets/osunlp/Mind2Web-2), then:

```bash
# Evaluate all tasks
python run_eval.py --agent_name <agent_name>

# Evaluate a specific task
python run_eval.py --agent_name <agent_name> --task_id <task_id>

# Example
python run_eval.py --agent_name example --task_id yu_lineage
```

<details>
<summary><b>Advanced options</b></summary>

| Flag | Description | Default |
|------|-------------|---------|
| `--agent_name` | Agent name (required) | — |
| `--task_id` | Evaluate a specific task only | all tasks |
| `--answer_folder` | Path to answer directory | `answers/` |
| `--cache_root` | Path to webpage cache | `cache/` |
| `--eval_scripts_root` | Evaluation scripts directory | `eval_scripts/` |
| `--eval_results_root` | Output directory for results | `eval_results/` |
| `--eval_version` | Evaluation script version | `2025_07_14` |
| `--llm_provider` | `openai` or `azure_openai` | `openai` |
| `--max_concurrent_tasks` | Max parallel task evaluations | `2` |
| `--max_concurrent_answers` | Max parallel answer evaluations per task | `3` |
| `--max_webpage_retrieval` | Max parallel webpage fetches | `5` |
| `--max_llm_requests` | Max parallel LLM API calls | `30` |
| `--dump_cache` | Persist cache to disk | `True` |
| `--overwrite` | Overwrite existing results | `False` |

</details>

---

## Citation

If you find this work useful, please consider citing:

```bibtex
@inproceedings{
    gou2025mind2web2,
    title={Mind2Web 2: Evaluating Agentic Search with Agent-as-a-Judge},
    author={Boyu Gou and Zanming Huang and Yuting Ning and Yu Gu and Michael Lin and Botao Yu and Andrei Kopanev and Weijian Qi and Yiheng Shu and Jiaman Wu and Chan Hee Song and Bernal Jimenez Gutierrez and Yifei Li and Zeyi Liao and Hanane Nour Moussa and TIANSHU ZHANG and Jian Xie and Tianci Xue and Shijie Chen and Boyuan Zheng and Kai Zhang and Zhaowei Cai and Viktor Rozgic and Morteza Ziyadi and Huan Sun and Yu Su},
    booktitle={The Thirty-ninth Annual Conference on Neural Information Processing Systems Datasets and Benchmarks Track},
    year={2025},
    url={https://openreview.net/forum?id=AUaW6DS9si}
}
```
