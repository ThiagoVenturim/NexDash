#!/usr/bin/env bash
# Cross-platform execution script for NexDash on Linux and macOS.
# Runs NexDash locally, in Docker Compose, or executes test suites.

set -euo pipefail

MODE="local"
PORT="8000"
DB_URL=""

usage() {
    cat <<EOF
Usage: ./scripts/run.sh [mode] [options]

Modes:
    local       Run NexDash server directly (default)
    docker      Run NexDash and services via Docker Compose
    test        Run pytest test suite

Options:
    -p, --port PORT     Port to bind locally (default: 8000)
    -d, --db-url URL    Database URL (sets DB_URL environment variable)
    -h, --help          Show this help message
EOF
    exit 0
}

# Parse positional argument or flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        local|docker|test)
            MODE="$1"
            shift
            ;;
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        -d|--db-url)
            DB_URL="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "[ERROR] Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [[ -n "$DB_URL" ]]; then
    export DB_URL="$DB_URL"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

PYTHON_EXE="python3"
if [[ -f "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_EXE="$REPO_ROOT/.venv/bin/python"
elif command -v python &>/dev/null; then
    PYTHON_EXE="python"
fi

case "$MODE" in
    docker)
        echo "[INFO] Starting NexDash with Docker Compose..."
        docker compose up --build
        ;;
    test)
        echo "[INFO] Running test suite..."
        "$PYTHON_EXE" -m pytest
        ;;
    local)
        echo "[INFO] Starting NexDash locally on port $PORT..."
        "$PYTHON_EXE" -m nexdash --port "$PORT"
        ;;
esac
