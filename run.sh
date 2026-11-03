#!/usr/bin/env bash
# ==============================================================================
# GlocalInduct All-in-One Platform Controller
# Run the platform with a single command:
#   ./run.sh
# ==============================================================================

set -e
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# Terminal Colors
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_BLUE='\033[34m'
C_YELLOW='\033[33m'
C_RED='\033[31m'
C_CYAN='\033[36m'

log_info()    { echo -e "${C_BLUE}ℹ${C_RESET} $1"; }
log_success() { echo -e "${C_GREEN}✔${C_RESET} $1"; }
log_warn()    { echo -e "${C_YELLOW}⚠${C_RESET} $1"; }
log_error()   { echo -e "${C_RED}✖${C_RESET} $1"; }

HOST_IP=$(ip -4 addr show enp37s0f1 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || echo "10.40.14.96")
PORT="${PORT:-8008}"
CLI_MODEL_OVERRIDE=""

# Parse optional leading --model / -m flag (e.g. ./run.sh --model qwen3.8 infer "...")
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model|-m)
            CLI_MODEL_OVERRIDE="$2"
            shift 2
            ;;
        --model=*)
            CLI_MODEL_OVERRIDE="${1#*=}"
            shift 1
            ;;
        *)
            break
            ;;
    esac
done

apply_model_override() {
    local target="$1"
    if [ -z "$target" ]; then
        return
    fi
    case "${target,,}" in
        gpt-4o|gpt4o|openai)
            export GLOCALINDUCT_MODEL="gpt-4o"
            export GLOCALINDUCT_PROVIDER="openai"
            export GLOCALINDUCT_GENERATION_MODEL="gpt-4o"
            export GLOCALINDUCT_EVALUATION_MODEL="gpt-4o"
            export GLOCALINDUCT_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"
            ;;
        qwen3.8|qwen3_8|ollama)
            export GLOCALINDUCT_MODEL="qwen3.8"
            export GLOCALINDUCT_PROVIDER="ollama"
            export GLOCALINDUCT_GENERATION_MODEL="qwen3.8"
            export GLOCALINDUCT_EVALUATION_MODEL="qwen3.8"
            export GLOCALINDUCT_BASE_URL="${OLLAMA_BASE_URL:-http://ollama:11434/v1}"
            ;;
        *)
            export GLOCALINDUCT_MODEL="$target"
            export GLOCALINDUCT_PROVIDER="ollama"
            export GLOCALINDUCT_GENERATION_MODEL="$target"
            export GLOCALINDUCT_EVALUATION_MODEL="$target"
            export GLOCALINDUCT_BASE_URL="${OLLAMA_BASE_URL:-http://ollama:11434/v1}"
            ;;
    esac
}

switch_env_model() {
    local target="$1"
    if [ -z "$target" ]; then
        log_error "Usage: ./run.sh use <gpt-4o|qwen3.8>"
        exit 1
    fi
    check_env
    case "${target,,}" in
        gpt-4o|gpt4o|openai)
            sed -i 's/^GLOCALINDUCT_PROVIDER=.*/GLOCALINDUCT_PROVIDER=openai/' .env
            sed -i 's/^GLOCALINDUCT_GENERATION_MODEL=.*/GLOCALINDUCT_GENERATION_MODEL=gpt-4o/' .env
            sed -i 's/^GLOCALINDUCT_EVALUATION_MODEL=.*/GLOCALINDUCT_EVALUATION_MODEL=gpt-4o/' .env
            sed -i 's|^GLOCALINDUCT_BASE_URL=.*|GLOCALINDUCT_BASE_URL=https://api.openai.com/v1|' .env
            log_success "Switched .env active model to OpenAI gpt-4o"
            ;;
        qwen3.8|qwen3_8|ollama)
            sed -i 's/^GLOCALINDUCT_PROVIDER=.*/GLOCALINDUCT_PROVIDER=ollama/' .env
            sed -i 's/^GLOCALINDUCT_GENERATION_MODEL=.*/GLOCALINDUCT_GENERATION_MODEL=qwen3.8/' .env
            sed -i 's/^GLOCALINDUCT_EVALUATION_MODEL=.*/GLOCALINDUCT_EVALUATION_MODEL=qwen3.8/' .env
            sed -i 's|^GLOCALINDUCT_BASE_URL=.*|GLOCALINDUCT_BASE_URL=http://ollama:11434/v1|' .env
            log_success "Switched .env active model to Ollama qwen3.8"
            ;;
        *)
            sed -i 's/^GLOCALINDUCT_PROVIDER=.*/GLOCALINDUCT_PROVIDER=ollama/' .env
            sed -i "s/^GLOCALINDUCT_GENERATION_MODEL=.*/GLOCALINDUCT_GENERATION_MODEL=$target/" .env
            sed -i "s/^GLOCALINDUCT_EVALUATION_MODEL=.*/GLOCALINDUCT_EVALUATION_MODEL=$target/" .env
            sed -i 's|^GLOCALINDUCT_BASE_URL=.*|GLOCALINDUCT_BASE_URL=http://ollama:11434/v1|' .env
            log_success "Switched .env active model to Ollama $target"
            ;;
    esac
}

# 1. Environment Verification
check_env() {
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            log_warn ".env file not found. Initializing from .env.example..."
            cp .env.example .env
        else
            log_error "No .env or .env.example file found."
            exit 1
        fi
    fi

    # Export variables from .env
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a

    apply_model_override "$CLI_MODEL_OVERRIDE"

    PORT="${PORT:-8008}"
    ACTIVE_PROVIDER="${GLOCALINDUCT_PROVIDER:-ollama}"
    ACTIVE_MODEL="${GLOCALINDUCT_GENERATION_MODEL:-qwen3.8}"

    # If using Ollama, verify Docker Ollama container & model availability
    if [ "$ACTIVE_PROVIDER" = "ollama" ]; then
        if ! docker ps --format '{{.Names}}' | grep -q '^glocalinduct-ollama$'; then
            log_info "Starting Docker Ollama service (glocalinduct-ollama)..."
            docker compose up -d ollama
            sleep 2
        fi
        if ! docker exec glocalinduct-ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qE "^${ACTIVE_MODEL}(:|$)"; then
            log_warn "Model '$ACTIVE_MODEL' not found in Docker Ollama."
            log_info "Pulling $ACTIVE_MODEL in Docker Ollama..."
            docker exec glocalinduct-ollama ollama pull "$ACTIVE_MODEL" || log_warn "Could not auto-pull $ACTIVE_MODEL. Run: docker exec glocalinduct-ollama ollama pull $ACTIVE_MODEL"
        fi
    fi
}

# 2. Check Docker
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is required but not installed or not in PATH."
        exit 1
    fi
    if ! docker compose version &> /dev/null; then
        log_error "docker compose (v2) is required."
        exit 1
    fi
}

# 3. Ingestion & Split
ensure_data_ingested() {
    if [ ! -f "workspace/staging/split_manifest.json" ]; then
        log_info "Ingesting data and generating dataset partitions..."
        docker compose run --rm glocalinduct glocalinduct ingest
        docker compose run --rm glocalinduct glocalinduct split
        log_success "Data ingestion and partitioning complete."
    else
        log_success "Dataset already ingested and partitioned."
    fi
}

# 4. Start Web Service
start_service() {
    check_docker
    check_env
    ensure_data_ingested

    log_info "Starting GlocalInduct web container..."
    docker compose up -d

    # Wait for server to respond
    log_info "Waiting for service to become ready on port ${PORT}..."
    local attempts=0
    local max_attempts=20
    while [ $attempts -lt $max_attempts ]; do
        if curl -s -f "http://127.0.0.1:${PORT}" > /dev/null 2>&1 || curl -s "http://127.0.0.1:${PORT}" | grep -q "GlocalInduct" 2>/dev/null; then
            break
        fi
        sleep 1
        attempts=$((attempts + 1))
    done

    echo ""
    echo -e "${C_BOLD}${C_GREEN}================================================================${C_RESET}"
    echo -e "${C_BOLD}${C_GREEN}     🚀 GlocalInduct Platform is Running Successfully!          ${C_RESET}"
    echo -e "${C_BOLD}${C_GREEN}================================================================${C_RESET}"
    echo ""
    echo -e "  ${C_BOLD}Network Access URL:${C_RESET}   ${C_CYAN}${C_BOLD}http://${HOST_IP}:${PORT}${C_RESET}"
    echo -e "  ${C_BOLD}Localhost Access:${C_RESET}     ${C_CYAN}http://localhost:${PORT}${C_RESET}"
    echo ""
    echo -e "  ${C_BOLD}Active Provider:${C_RESET}      ${C_GREEN}${ACTIVE_PROVIDER}${C_RESET}"
    echo -e "  ${C_BOLD}Active Model:${C_RESET}         ${C_GREEN}${ACTIVE_MODEL} (temperature: ${GLOCALINDUCT_TEMPERATURE:-0.0})${C_RESET}"
    echo -e "  ${C_BOLD}Host Port:${C_RESET}            ${C_YELLOW}${PORT}${C_RESET}"
    echo ""
    echo -e "${C_BOLD}Useful Commands:${C_RESET}"
    echo -e "  • Run full pipeline:    ${C_BLUE}./run.sh pipeline [SAMPLE_LIMIT]${C_RESET}"
    echo -e "  • Build memory loop:    ${C_BLUE}./run.sh build-memory [SAMPLE_LIMIT]${C_RESET}"
    echo -e "  • Run evaluation:       ${C_BLUE}./run.sh evaluate development M1 [LIMIT]${C_RESET}"
    echo -e "  • Run live inference:   ${C_BLUE}./run.sh infer \"Your English sentence here\"${C_RESET}"
    echo -e "  • Inspect memory:       ${C_BLUE}./run.sh inspect M1${C_RESET}"
    echo -e "  • View live logs:       ${C_BLUE}./run.sh logs${C_RESET}"
    echo -e "  • Stop the platform:    ${C_BLUE}./run.sh stop${C_RESET}"
    echo ""
}

# 5. Build Memory Pipeline (M0, M1)
cmd_build_memory() {
    check_docker
    check_env
    ensure_data_ingested

    local limit="$1"
    local limit_flag=""
    if [ -n "$limit" ] && [ "$limit" != "all" ] && [ "$limit" -gt 0 ] 2>/dev/null; then
        limit_flag="--limit $limit"
        log_info "Building memory using sample limit: $limit pairs"
    else
        log_info "Building memory across ALL dataset pairs (396 seed, 397 practice)..."
    fi

    log_info "Step 1/3: Building Seed Memory (Release M0) via Agent A1..."
    docker compose run --rm glocalinduct glocalinduct build-seed-memory --release M0 $limit_flag --overwrite

    log_info "Step 2/3: Collecting Practice Attempts via Agents A3, A4, A5..."
    docker compose run --rm glocalinduct glocalinduct collect-practice --release M0 $limit_flag

    log_info "Step 3/3: Updating Insights (Release M1) via Agent A2 & A5..."
    docker compose run --rm glocalinduct glocalinduct update-insights --base M0 --release M1 $limit_flag --overwrite

    log_success "Memory releases M0 and M1 built successfully!"
    echo ""
    log_info "Inspect M1 memory summary:"
    docker compose run --rm glocalinduct glocalinduct inspect-memory --release M1
}

# 6. Full End-to-End Pipeline (Ingest -> Memory M0/M1 -> Evaluate -> Inspect)
cmd_full_pipeline() {
    check_docker
    check_env

    local limit="$1"
    local limit_flag=""
    if [ -n "$limit" ] && [ "$limit" != "all" ] && [ "$limit" -gt 0 ] 2>/dev/null; then
        limit_flag="--limit $limit"
        echo -e "${C_BOLD}${C_CYAN}=== Launching GlocalInduct Pipeline (Sample: ${limit} pairs) ===${C_RESET}"
    else
        echo -e "${C_BOLD}${C_CYAN}=== Launching GlocalInduct Pipeline across ALL PAIRS (396 Seed, 397 Practice, 99 Dev) ===${C_RESET}"
    fi
    echo -e "Provider: ${C_GREEN}${ACTIVE_PROVIDER}${C_RESET} | Model: ${C_GREEN}${ACTIVE_MODEL}${C_RESET}"
    echo ""

    log_info ">>> Phase 1: Ingest & Split Dataset..."
    docker compose run --rm glocalinduct glocalinduct ingest
    docker compose run --rm glocalinduct glocalinduct split

    log_info ">>> Phase 2: Build Memory Loop (M0 -> Practice -> M1)..."
    cmd_build_memory "$limit"

    log_info ">>> Phase 3: Benchmark Evaluation on Development Split..."
    docker compose run --rm glocalinduct glocalinduct evaluate --partition development --release M1 $limit_flag

    log_info ">>> Phase 4: Sample Live Glocalization..."
    docker compose run --rm glocalinduct glocalinduct infer \
        --text "During the hot summer afternoon, the family gathered in the garden to enjoy refreshing iced drinks and sweet dates." \
        --release M1

    echo ""
    log_success "=== End-to-End Pipeline Completed Successfully! ==="
}

# 7. Command Dispatcher
COMMAND="${1:-start}"

case "$COMMAND" in
    start|up|serve|"")
        start_service
        ;;
    use|model)
        switch_env_model "$2"
        if docker ps --format '{{.Names}}' | grep -q '^glocalinduct$'; then
            log_info "Restarting running GlocalInduct container with new model..."
            docker compose down
            start_service
        fi
        ;;
    stop|down)
        check_docker
        log_info "Stopping GlocalInduct containers..."
        docker compose down
        log_success "Containers stopped."
        ;;
    restart)
        check_docker
        log_info "Restarting GlocalInduct..."
        docker compose down
        start_service
        ;;
    logs)
        check_docker
        docker compose logs -f
        ;;
    status)
        check_docker
        docker compose ps
        echo ""
        log_info "Checking memory database status:"
        docker compose run --rm glocalinduct glocalinduct inspect-memory || true
        ;;
    ingest)
        check_docker
        check_env
        log_info "Ingesting data..."
        docker compose run --rm glocalinduct glocalinduct ingest
        log_info "Splitting partitions..."
        docker compose run --rm glocalinduct glocalinduct split
        log_success "Done."
        ;;
    pipeline)
        cmd_full_pipeline "$2"
        ;;
    build-memory)
        cmd_build_memory "$2"
        ;;
    seed)
        check_docker
        check_env
        LIMIT_FLAG=""
        [ -n "$3" ] && [ "$3" != "all" ] && LIMIT_FLAG="--limit $3"
        docker compose run --rm glocalinduct glocalinduct build-seed-memory --release "${2:-M0}" $LIMIT_FLAG --overwrite
        ;;
    practice)
        check_docker
        check_env
        LIMIT_FLAG=""
        [ -n "$3" ] && [ "$3" != "all" ] && LIMIT_FLAG="--limit $3"
        docker compose run --rm glocalinduct glocalinduct collect-practice --release "${2:-M0}" $LIMIT_FLAG
        ;;
    insights)
        check_docker
        check_env
        LIMIT_FLAG=""
        [ -n "$4" ] && [ "$4" != "all" ] && LIMIT_FLAG="--limit $4"
        docker compose run --rm glocalinduct glocalinduct update-insights --base "${2:-M0}" --release "${3:-M1}" $LIMIT_FLAG --overwrite
        ;;
    inspect)
        check_docker
        check_env
        docker compose run --rm glocalinduct glocalinduct inspect-memory --release "${2:-M1}"
        ;;
    evaluate)
        check_docker
        check_env
        PARTITION="${2:-development}"
        RELEASE="${3:-M1}"
        LIMIT_FLAG=""
        [ -n "$4" ] && [ "$4" != "all" ] && LIMIT_FLAG="--limit $4"
        docker compose run --rm glocalinduct glocalinduct evaluate --partition "$PARTITION" --release "$RELEASE" $LIMIT_FLAG
        ;;
    infer)
        check_docker
        check_env
        TEXT="${2:-They engage in healthy debate to reach optimal solutions.}"
        RELEASE="${3:-M1}"
        CONTEXT="${4:-{\"domains\":[\"general\"],\"locale_scope\":\"general_arab\"}}"
        log_info "Running glocalization with ${ACTIVE_PROVIDER}/${ACTIVE_MODEL} on: \"$TEXT\" (release: $RELEASE)"
        docker compose run --rm glocalinduct glocalinduct infer \
            --text "$TEXT" \
            --context "$CONTEXT" \
            --release "$RELEASE"
        ;;
    help|--help|-h)
        echo "Usage: ./run.sh [--model gpt-4o|qwen3.8] [COMMAND]"
        echo ""
        echo "Available Commands:"
        echo "  start (default)              Ensure dataset ingested & start web platform"
        echo "  use <gpt-4o|qwen3.8>         Switch active model in .env (and restart container if running)"
        echo "  stop                         Stop web container"
        echo "  restart                      Restart web container"
        echo "  logs                         Stream live logs from web container"
        echo "  status                       Check status of container and memories"
        echo "  pipeline [LIMIT]             Run full end-to-end suite (Ingest -> M0/M1 -> Evaluate -> Infer)"
        echo "  build-memory [LIMIT]         Run memory learning loop (M0 seed -> practice -> M1 insights)"
        echo "  evaluate [PART] [REL] [LIM]  Run benchmark evaluation (e.g. ./run.sh evaluate development M1 5)"
        echo "  seed [REL] [LIMIT]           Build seed memory (default: M0)"
        echo "  practice [REL] [LIMIT]       Collect practice attempts (default: M0)"
        echo "  insights [BASE] [REL] [LIM]  Consolidate insights (default: base M0, release M1)"
        echo "  inspect [REL]                Inspect memory rules and manifest (default: M1)"
        echo "  infer \"TEXT\" [REL]           Run live glocalization inference on input text"
        echo "  ingest                       Run data ingestion and split partitioning"
        ;;
    *)
        log_error "Unknown command: $COMMAND"
        echo "Run './run.sh help' for available commands."
        exit 1
        ;;
esac
