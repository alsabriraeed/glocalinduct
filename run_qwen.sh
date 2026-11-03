#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

CONFIG="${CONFIG:-configs/default.yaml}"

docker exec \
  -e GLOCALINDUCT_PROVIDER=ollama \
  -e GLOCALINDUCT_MODEL=qwen3.8 \
  -e GLOCALINDUCT_GENERATION_MODEL=qwen3.8 \
  -e GLOCALINDUCT_EVALUATION_MODEL=qwen3.8 \
  -e GLOCALINDUCT_EVAL_MODEL=qwen3.8 \
  -e GLOCALINDUCT_BASE_URL=http://ollama:11434/v1 \
  -e GLOCALINDUCT_TRACE_SESSION=ses_qwen_live \
  glocalinduct bash -c "
    glocalinduct split --config ${CONFIG} --overwrite &&
    glocalinduct seed-memory --config ${CONFIG} --release M0 --overwrite &&
    glocalinduct online --config ${CONFIG} --base-release M0 --release M1 &&
    glocalinduct inspect-memory --config ${CONFIG} --release M1 &&
    glocalinduct evaluate --config ${CONFIG} --partition development --releases M0,M1 &&
    glocalinduct evaluate --config ${CONFIG} --partition test --releases M0,M1
  "

