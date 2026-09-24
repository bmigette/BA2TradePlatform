#!/usr/bin/env bash
# BA2 Test Platform start script (Linux/macOS) -- a thin wrapper around `ba2-test serve`.
# Usage: ./start.sh [backend|frontend|all] [extra `ba2-test serve` flags, e.g. --port 8001 --reload]
#
# Needs the test venv built by the installer at the monorepo root (./install.sh --test-only). The
# `ba2-test` command is looked up in this order: $BA2_TEST_BIN, `ba2-test` on PATH (an activated
# venv), then the installer's default location ~/ba2-venvs/test (or $BA2_VENV_BASE/ba2-venvs/test
# if the venvs were built with --base). Provider API keys are set in the UI (Settings -> API Keys);
# no .env file is required.

set -euo pipefail

case "${1:-all}" in
    backend)  MODE=back ;;
    frontend) MODE=front ;;
    all)      MODE=both ;;
    -h|--help|*)
        echo "Usage: $0 [backend|frontend|all] [ba2-test serve flags]"
        echo "  backend  - start only the API (http://localhost:8000, docs at /docs)"
        echo "  frontend - start only the Vite UI (http://localhost:5173)"
        echo "  all      - start both (default)"
        echo "Equivalent to: ba2-test serve --mode back|front|both [flags]"
        case "${1:-all}" in -h|--help) exit 0 ;; *) exit 1 ;; esac
        ;;
esac
[ $# -gt 0 ] && shift

VENV="${BA2_VENV_BASE:-$HOME}/ba2-venvs/test"
BIN="${BA2_TEST_BIN:-}"
if [ -z "$BIN" ]; then
    if command -v ba2-test >/dev/null 2>&1; then
        BIN="$(command -v ba2-test)"
    elif [ -x "$VENV/bin/ba2-test" ]; then
        BIN="$VENV/bin/ba2-test"
    elif [ -x "$VENV/Scripts/ba2-test.exe" ]; then   # Git Bash on Windows
        BIN="$VENV/Scripts/ba2-test.exe"
    fi
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    echo "[ERROR] ba2-test not found. Build the test venv first, from the monorepo root:" >&2
    echo "        ./install.sh --test-only --editable" >&2
    echo "        (or set BA2_TEST_BIN to the ba2-test executable)" >&2
    exit 1
fi

echo "[INFO] Starting BA2 Test Platform ($MODE) with $BIN"
exec "$BIN" serve --mode "$MODE" "$@"
