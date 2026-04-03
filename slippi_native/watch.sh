#!/bin/bash
# Watch Rust source files and rebuild on save.
# Uses debug builds for fast iteration. Pass --release for optimized builds.
# Usage:
#   ./watch.sh            # debug builds (fast compile)
#   ./watch.sh --release  # release builds (optimized)
set -e
cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
# Activate the venv so maturin and python3 are on PATH
source "$REPO_ROOT/.venv/bin/activate"

if [[ "$1" == "--release" ]]; then
    echo "Watching for changes (release builds)..."
    cargo watch -w src -s "maturin develop --release"
else
    echo "Watching for changes (debug builds)..."
    cargo watch -w src -s "maturin develop"
fi
