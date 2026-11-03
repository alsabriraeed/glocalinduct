#!/usr/bin/env bash
# Rebuild the demo memories (M0, M1) from data/ with the configured provider, then serve the prototype.
set -e
cd "$(dirname "$0")"
glocalinduct ingest && glocalinduct split
glocalinduct build-seed-memory --release M0
glocalinduct collect-practice --release M0
glocalinduct update-insights --base M0 --release M1
glocalinduct inspect-memory --release M1 | head -40
glocalinduct serve --port "${PORT:-8000}"
