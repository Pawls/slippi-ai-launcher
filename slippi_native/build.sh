#!/bin/bash
# Build slippi_native and install into the active Python venv.
# Usage:
#   ./build.sh           # release build (optimized, use for training)
#   ./build.sh --debug   # debug build (fast compile, use for development)
set -e
cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
# Activate the venv so maturin and python3 are on PATH
source "$REPO_ROOT/.venv/bin/activate"

if [[ "$1" == "--debug" ]]; then
    echo "Building slippi_native (debug)..."
    maturin develop 2>&1
else
    echo "Building slippi_native (release)..."
    maturin develop --release 2>&1
fi

echo "Verifying install..."
python3 -c "import slippi_native; print('slippi_native OK')"
