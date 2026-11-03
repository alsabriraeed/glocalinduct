# GlocalInduct

Experiential English → Arabic glocalization: five agent roles (A1 Pair Analyst, A2 Insight Curator,
A3 Adaptation Planner, A4 Arabic Generator, A5 Evaluator), two versioned memories (experiences and
insights, ExpeL-style ADD/EDIT/UPVOTE/DOWNVOTE), a deterministic controller that owns IDs, hashes,
span resolution, validation, budgets and final acceptance, and a paste-or-upload **Glocalize** prototype.

This is the implementation of `docs/GlocalInduct_ExpeL_Implementation_Guide.md` (specification) built
from the contract pack (`contracts.py`, prompts, schemas, `storage/schema.sql`, configs).

## What is here

```
configs/            default.yaml (pilot defaults), domain_policies.yaml (preservation policies)
prompts/            shared + nine role/mode prompts, used verbatim from the pack
schemas/            fourteen exported JSON Schemas
storage/schema.sql  immutable SQLite metadata schema
src/glocalinduct/
  contracts.py      typed agent payloads (pack, unchanged)
  config.py         loading + startup validation (null runtime fields reject real calls)
  ingestion/        extractors (txt/md/docx/pdf), NFC normalisation, exact-ID pairing, grouping, windows, splits
  validation/       span resolution, policy union, plan/candidate validators, decision logic, leakage guard
  memory/           SQLite repository, text stores, experience memory, insight transactions, releases
  retrieval/        local hashing embedder, scope gates, ranking, prompt budgeting
  providers/        fake (offline, deterministic), Anthropic (structured output), OpenAI-compatible
  agents/           structured-call runner (retries, schema correction, budgets), allowlisted bundles, A1–A5 wrappers
  workflows/        ingest, split, seed (M0), practice, consolidate (M1), infer, evaluate
  web/              FastAPI prototype + single-page UI
  cli.py            glocalinduct ingest | split | build-seed-memory | collect-practice | update-insights | infer | evaluate | inspect-memory | serve
tests/              58 tests: pack contract checks, milestones 1–4 acceptance, providers, web
docs/               guide, brief, DEPARTURES.md (ambiguity log), REQUIREMENT_TEST_MAP.md
```

## Quick start

```bash
pip install -e ".[dev,anthropic]"
python -m pytest -q                      # 58 tests, no network

# Prototype with the offline fake provider (placeholder Arabic, demonstrates the mechanics)
glocalinduct serve --port 8000           # open http://127.0.0.1:8000, paste or upload, click Glocalize

# Full learning loop on the 14-pair fixture corpus (fake provider)
# data/ already holds the 14-pair sample corpus (same files as tests/fixtures/corpus)
glocalinduct ingest && glocalinduct split
glocalinduct build-seed-memory --release M0
glocalinduct collect-practice --release M0
glocalinduct update-insights --base M0 --release M1
glocalinduct infer --text "Lina buys pancakes on Main Street for 3 dollars." \
    --context '{"domains":["education"],"locale_scope":"general_arab"}' --release M1
glocalinduct evaluate --partition test --release M1
glocalinduct inspect-memory --release M1
# or all of the above in one go, then serve: scripts/demo.sh
```

## Running with a real model

Deployment fields are deliberate nulls; startup refuses real calls until they are set.

```bash
export GLOCALINDUCT_API_KEY=...            # never stored in logs or manifests
export GLOCALINDUCT_PROVIDER=anthropic     # or openai | openrouter | ollama | vllm | openai_compatible
export GLOCALINDUCT_GENERATION_MODEL=<exact model id>
export GLOCALINDUCT_EVALUATION_MODEL=<exact model id>
export GLOCALINDUCT_CONTEXT_WINDOW=200000
export GLOCALINDUCT_RESERVED_OUTPUT=8000
glocalinduct serve
```

Or set the same keys under `runtime:` in `configs/default.yaml` (`base_url` for OpenAI-compatible
servers). The Anthropic adapter uses structured JSON output (`output_config.format`) and falls back to
prompt-only JSON on older SDKs; local Pydantic validation stays authoritative either way. The fake
provider is the only one exercised in tests; nothing here claims semantic quality for any model.

## How a request is processed (`infer`)

1. Extract and freeze the source (NFC, LF, typed blocks, SHA-256), build owner windows.
2. A3 `analyse_source` per window → merged, validated English-only SourceProfile.
3. Retrieve from the frozen release: exact/near-duplicate corpus groups excluded, scope gates, cosine ranking.
4. A3 `make_plan` per window → merged, reconciled, deterministically validated, then frozen (hash).
5. A4 `generate_arabic` per window → assembled in source order, every realization resolved, candidate hash.
6. A5 `candidate_quality` per window (+ cross-window pass) with no reference, rationale or prior verdict.
   *When a reference localization exists (practice loop, `evaluate`) the Quality evaluator is instead the
   reference comparison alone: no meaning / cultural-fit / Arabic-quality criteria are scored.*
7. Controller decision: `accepted` / `conservative` (safe retention disclosed) / `unresolved` (no Arabic).
   With a reference: `compatible` → accepted, `incompatible` → unresolved, `unknown` → unresolved.

Every stage, including raw provider responses, is persisted under `workspace/runs/<run_id>/` and in the
immutable object store. Deployment requests never write to memory.

## Status

Milestones 1–4 of the brief are implemented and covered by acceptance tests. Milestone 5 (a real pilot
with measured results) requires a configured provider and corpus; see `docs/DEPARTURES.md` for the
remaining limitations and every implementation choice that departs from or sharpens the guide.
