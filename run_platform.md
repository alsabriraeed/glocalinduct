# GlocalInduct Platform Execution Guide

This document outlines how to run, configure, and operate the **GlocalInduct** platform for English-to-Arabic experiential glocalization.

---

## 1. Architecture Overview

GlocalInduct uses an iterative, experiential learning architecture (ExpeL-style) to translate and culturally adapt English content into natural Modern Standard Arabic (MSA):

- **5 Agent Roles**:
  - **A1 (Pair Analyst)**: Analyzes English-Arabic training pairs to extract cultural adaptation strategies.
  - **A2 (Insight Curator)**: Clusters, refines, and upvotes/downvotes cross-lingual insights into reusable memory rules.
  - **A3 (Adaptation Planner)**: Profiles source English, identifies cultural entities, and drafts adaptation requirements.
  - **A4 (Arabic Generator)**: Generates cultural adaptations according to plan and retrieved memory rules.
  - **A5 (Blind Evaluator)**: Independently evaluates candidate quality, meaning preservation, and cultural naturalness.
- **2 Versioned Memories**:
  - **M0 (Seed Memory)**: Initial episodic memory constructed from training pairs.
  - **M1 (Consolidated Memory)**: Experiential memory augmented with practice iterations and curated insight rules.

---

## ⚡ Quick Start: Run Everything with One Command

You can run the entire platform using the all-in-one controller script:

```bash
./run.sh
```

This single command automatically:
1. Verifies your `.env` configuration (OpenAI `gpt-4o`, `temperature: 0.0`).
2. Checks that the dataset is ingested and partitioned into training/dev/test sets.
3. Starts the Docker web container on port `8080`.
4. Tests health and outputs the direct URLs:
   - **Network URL**: **`http://10.40.14.96:8080`**
   - **Local URL**: `http://localhost:8080`

### Useful `./run.sh` Subcommands

```bash
./run.sh              # Start the platform web service (default)
./run.sh logs         # Stream live logs from the web container
./run.sh status       # Check status of container and memories
./run.sh stop         # Stop the platform
./run.sh build-memory # Run the offline learning loop (M0 -> practice -> M1)
./run.sh infer "Text" # Run single inference from CLI
./run.sh help         # Show all available commands
```

---

## 2. Environment Configuration

The platform reads settings from `.env` in the project root (which is gitignored).

### `.env` Settings for OpenAI `gpt-4o` (Temperature: 0.0)

```env
# ===============================================================
# GlocalInduct Environment Configuration
# ===============================================================

# Provider selection: openai (also supports anthropic, ollama, vllm, openrouter, fake)
GLOCALINDUCT_PROVIDER=openai

# OpenAI API Key
OPENAI_API_KEY=sk-proj-...
GLOCALINDUCT_API_KEY=sk-proj-...

# Agent Model Slots (using gpt-4o for all generation and evaluation tasks)
GLOCALINDUCT_GENERATION_MODEL=gpt-4o
GLOCALINDUCT_EVALUATION_MODEL=gpt-4o
OPENAI_MODEL=gpt-4o

# OpenAI Base URL
OPENAI_BASE_URL=https://api.openai.com/v1
GLOCALINDUCT_BASE_URL=https://api.openai.com/v1

# Temperature 0.0 for deterministic reproducibility
GLOCALINDUCT_TEMPERATURE=0.0
MODEL_TEMPERATURE=0.0

# Token Limits for gpt-4o
GLOCALINDUCT_CONTEXT_WINDOW=128000
GLOCALINDUCT_RESERVED_OUTPUT=4096

# Web UI Host Port (8080 avoids conflict with host port 8000)
PORT=8080
```

---

## 3. Running with Docker (Recommended)

### A. Accessing the Web Workbench UI

Start the container in the background:
```bash
docker compose up -d
```

- **Local Machine Access**: [http://localhost:8080](http://localhost:8080)
- **Network / Remote Access**: **`http://10.40.14.96:8080`**

*(Note: Port `8080` on the host maps to port `8000` inside the container to avoid collision with port 8000 already occupied on the host).*

### B. Docker Service Management

- **View Live Logs**:
  ```bash
  docker compose logs -f
  ```
- **Check Container Status**:
  ```bash
  docker ps | grep glocalinduct
  ```
- **Stop the Web Service**:
  ```bash
  docker compose down
  ```
- **Rebuild Container Image** (if code or dependencies change):
  ```bash
  docker compose build
  ```

---

## 4. Pipeline & Memory Workflows (CLI via Docker)

All persistent memory, SQLite databases, and run artifacts are mounted to `./workspace/` on the host machine.

### Step 1: Ingest and Partition Dataset
Processes raw files in `data/english/`, `data/arabic/`, and `data/metadata.jsonl`:
```bash
docker compose run --rm glocalinduct glocalinduct ingest
docker compose run --rm glocalinduct glocalinduct split
```
- Deduplicates, canonicalizes Unicode NFC, computes hashes, and partitions pairs into **learning (80%)**, **dev (10%)**, and **test (10%)**.

### Step 2: Build Initial Seed Memory (`M0`)
Runs Agent A1 (`gpt-4o`) across seed pairs to build baseline episodic memory:
```bash
docker compose run --rm glocalinduct glocalinduct build-seed-memory --release M0
```

### Step 3: Run Practice Iterations
Runs Agents A3, A4, and A5 across practice pairs using `M0`:
```bash
docker compose run --rm glocalinduct glocalinduct collect-practice --release M0
```

### Step 4: Consolidate Insights into Release `M1`
Agent A2 reviews practice attempts, clusters lessons, upvotes useful patterns, downvotes failures, and commits new rules:
```bash
docker compose run --rm glocalinduct glocalinduct update-insights --base M0 --release M1
```

### Step 5: Inspect Learned Memory Rules
View the rules, importance scores, and supporting examples in memory `M1`:
```bash
docker compose run --rm glocalinduct glocalinduct inspect-memory --release M1
```

### Step 6: Run Single Inference via CLI
Glocalize an English sentence using memory release `M1`:
```bash
docker compose run --rm glocalinduct glocalinduct infer \
  --text "The coffee break chatter is when everyone catches up on office news." \
  --context '{"domains":["general"],"locale_scope":"general_arab"}' \
  --release M1
```

---

## 5. Running Locally (Native Python Environment)

If running without Docker:

### 1. Install Dependencies
```bash
pip install -e ".[dev,anthropic]"
```

### 2. Launch Local Web Server
```bash
PYTHONPATH=src python3 -m glocalinduct.cli serve --host 0.0.0.0 --port 8080
```
Then visit `http://10.40.14.96:8080`.

### 3. Run Pipeline Commands Locally
```bash
export PYTHONPATH=src
python3 -m glocalinduct.cli ingest
python3 -m glocalinduct.cli split
python3 -m glocalinduct.cli build-seed-memory --release M0
python3 -m glocalinduct.cli collect-practice --release M0
python3 -m glocalinduct.cli update-insights --base M0 --release M1
```

---

## 6. Directory Layout & Storage

| Path | Purpose |
| :--- | :--- |
| `data/english/` | English source documents / sentences (`.md`) |
| `data/arabic/` | Reference glocalized Arabic documents / sentences (`.md`) |
| `data/metadata.jsonl` | Metadata attributes (`pair_id`, `domains`, `genre`, `locale_scope`, etc.) |
| `configs/` | `default.yaml` (pipeline parameters), `domain_policies.yaml` (preservation policies) |
| `prompts/` | System and agent prompt templates (A1–A5) |
| `workspace/staging/` | SQLite database storing memory records, insights, and releases |
| `workspace/runs/` | Audit logs of all inference and evaluation runs |
| `workspace/releases/` | Versioned memory snapshots (`M0`, `M1`) |
