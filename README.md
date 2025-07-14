# Mind2Web 2

Mind2Web 2 is a benchmark for agentic search systems, featuring Agent-as-a-Judge methodology for comprehensive, rigorous, and reliable assessment.

<div align="center">
  <img src="./assets/mind2web2_overview.jpg" alt="Mind2Web 2 Overview" width="800"/>
  <p><em>Mind2Web 2 features realistic and diverse long-horizon web search tasks and a novel Agent-as-a-Judge framework to evaluate complex, time-varying, and citation-backed answers.</em></p>
</div>

## 🔗 Links

- [🏠 Homepage](https://osu-nlp-group.github.io/Mind2Web-2)
- [📖 Paper](https://arxiv.org/abs/2506.21506)
- [😊 Dataset (Tasks)](https://huggingface.co/datasets/osunlp/Mind2Web-2)

## 🆕 Updates

- **2025/06/26**: The GitHub repo is live. The manuscript is now on arXiv.

## ⚙️ Environment Setup

### Option 1: Using uv (Recommended)

If you have [uv](https://docs.astral.sh/uv/) installed, it provides faster dependency resolution and installation:

```bash
# Automatically create virtual environment and install all dependencies
uv sync

# Activate the virtual environment
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install browsers for Playwright
playwright install
```

### Option 2: Using conda + pip

```bash
# Create and activate conda environment
conda create -n mind2web2 python=3.11
conda activate mind2web2

# Install the package in development mode
pip install -e .

# Install browsers for Playwright
playwright install
```

## 📁 Repo Structure

```
Mind2Web2-polish/
├── dataset/                 # Evaluation data and results
├── mind2web2/              # Main package
│   ├── api_tools/          # External API tools
│   ├── llm_client/         # LLM client implementations
│   ├── utils/              # Utility functions
│   ├── eval_runner.py      # Evaluation execution
│   ├── eval_toolkit.py     # Evaluation toolkit and utilities
│   ├── evaluator.py        # Core evaluation logic
│   └── verification_tree.py # Rubric tree implementation
├── pyproject.toml          # Package configuration
└── README.md              # This file
```

## 🚀 Run Evaluation

### 1. Prepare Your Data

Organize your agent's responses in the following directory structure:

```
answers/
└── <your_agent_name>/
    └── <task_id>/
        ├── answer_1.md
        ├── answer_2.md
        └── ...
```

Each answer file should contain your agent's response in markdown format.

### 2. Set up API Keys

Configure the necessary API keys for evaluation:

```bash
# Set up environment variables for OpenAI API
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"

# (Optional) Environment variables for Azure OpenAI
export AZURE_OPENAI_API_KEY="YOUR_AZURE_OPENAI_API_KEY"
export AZURE_OPENAI_ENDPOINT_URL="YOUR_AZURE_OPENAI_ENDPOINT_URL"
export AZURE_OPENAI_API_VERSION="2025-03-01-preview"

# Tool APIs for certain evaluation tasks
export GOOGLE_MAPS_API_KEY="YOUR_GOOGLE_MAPS_API_KEY"
```

### 3. Precache Webpages (Optional but Recommended)

*Note: This step is not required but highly recommended for reducing evaluation latency, as fetching webpages on-the-fly during evaluation can be very slow.*

Before running evaluation, you may want to precache the webpages to improve performance:

```bash
# Coming Soon!
```

We also provide a lightweight script to fix errors in precached webpages (e.g., pages blocked by human verification):

```bash
# Coming Soon!
```

### 4. Run Evaluation

Execute the evaluation using the `run_eval.py` script:

#### Basic Usage

```bash
# Evaluate all tasks for a specific agent
python run_eval.py --agent_name <your_agent_name>

# Evaluate a specific task
python run_eval.py --agent_name <your_agent_name> --task_id <task_id>
```

for example:

```bash
python run_eval.py --agent_name example

python run_eval.py --agent_name example --task_id yu_lineage
```

#### Advanced Configuration

```bash
python run_eval.py \
    --agent_name <your_agent_name> \
    --answer_folder answers \
    --llm_provider openai \
    --max_concurrent_tasks 2 \
    --max_concurrent_answers 3 \
    --max_webpage_retrieval 5 \
    --max_llm_requests 30 \
    --overwrite
```

#### Command Line Options

- `--agent_name`: Name of your agent (required)
- `--answer_folder`: Path to directory containing answer files (required)
- `--task_id`: Specific task to evaluate (optional, evaluates all tasks if not provided)
- `--llm_provider`: LLM provider (`openai` or `azure_openai`, default: `openai`)
- `--max_concurrent_tasks`: Maximum concurrent task evaluations (default: 2)
- `--max_concurrent_answers`: Maximum concurrent answer evaluations per task (default: 3)
- `--max_webpage_retrieval`: Maximum concurrent webpage retrievals (default: 5)
- `--max_llm_requests`: Maximum concurrent LLM API requests (default: 30)
- `--dump_cache`: Persist cache to disk (default: True)
- `--overwrite`: Overwrite existing results
- `--self_debug`: Add debug suffix to logs/result files

#### Path Overrides (Optional)

```bash
python run_eval.py \
    --agent_name <your_agent_name> \
    --answer_folder /path/to/your/answers \
    --eval_scripts_root /custom/eval/scripts \
    --eval_results_root /custom/results \
    --cache_root /custom/cache
```

The evaluation will:
1. Load evaluation scripts from `eval_scripts/`
2. Process answers without precached webpages (fetching on-demand)
3. Save results to `eval_results/`
4. Generate merged results for all tasks when evaluating multiple tasks


## 📝 Citation

If you find this work useful, please consider starring our repo and citing our papers:

```bibtex
@misc{gou2025mind2web2,
    title = {Mind2Web 2: Evaluating Agentic Search with Agent-as-a-Judge}, 
    author = {Boyu Gou and Zanming Huang and Yuting Ning and Yu Gu and Michael Lin and Weijian Qi and Andrei Kopanev and Botao Yu and Bernal Jiménez Gutiérrez and Yiheng Shu and Chan Hee Song and Jiaman Wu and Shijie Chen and Hanane Nour Moussa and Tianshu Zhang and Jian Xie and Yifei Li and Tianci Xue and Zeyi Liao and Kai Zhang and Boyuan Zheng and Zhaowei Cai and Viktor Rozgic and Morteza Ziyadi and Huan Sun and Yu Su},
    year = {2025},
    eprint = {2506.21506},
    archivePrefix = {arXiv},
    primaryClass = {cs.AI}
}
```
