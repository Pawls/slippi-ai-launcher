#!/usr/bin/env bash
# Shared environment for all run scripts.
# Override any variable via env vars or by creating runs/env.local.sh (gitignored).
#
# Env var names match the LAUNCHER conventions:
#   SLIPPI_AI_ROOT, MELEE_ISO, SLIPPI_DOLPHIN, SLIPPI_DOLPHIN_GUI, SLIPPI_AGENTS

# --- Resolve project root from this file's location ---
_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_ROOT="${SLIPPI_AI_ROOT:-$_ENV_DIR/..}"

# --- Infrastructure paths ---
export MELEE_ISO="${MELEE_ISO:-/home/pawl/melee/melee.iso}"
export DOLPHIN_HEADLESS="${SLIPPI_DOLPHIN:-/home/pawl/melee/dolphin-ai/squashfs-root/AppRun}"
#export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-/home/pawl/melee/Slippi_Netplay_Mainline_NoGui_BvH-x86_64.AppImage}"
export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-/home/pawl/.config/Slippi Launcher/netplay/Slippi_Online-x86_64.AppImage}"
export AGENTS_DIR="${SLIPPI_AGENTS:-/home/pawl/melee/agents}"

# --- Hardware optimization flags ---
export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

# --- Source local overrides if present ---
if [[ -f "$_ENV_DIR/env.local.sh" ]]; then
  source "$_ENV_DIR/env.local.sh"
fi

# --- cd to project root ---
cd "$PROJECT_ROOT"
