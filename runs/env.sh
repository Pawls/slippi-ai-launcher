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
# Detect whether we're running under WSL or a native Windows bash (MSYS/Cygwin).
_is_wsl=false
if [ -n "$WSL_DISTRO_NAME" ] || grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  _is_wsl=true
fi

if [ "$_is_wsl" = true ]; then
  # WSL: prefer /mnt/c/ style paths and use project-relative agents dir when possible
  export MELEE_ISO="${MELEE_ISO:-/mnt/c/Game Console Emulators/GC ISOs/Super Smash Bros. Melee v1.02 NTSC-U.iso}"
  export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-/mnt/c/Users/Paul/AppData/Roaming/Slippi Launcher/netplay/Slippi Dolphin.exe}"
  export AGENTS_DIR="${SLIPPI_AGENTS:-$PROJECT_ROOT/agents}"
else
  # Native Windows (Git Bash / MSYS / Cygwin)
  export MELEE_ISO="${MELEE_ISO:-C:/Game Console Emulators/GC ISOs/Super Smash Bros. Melee v1.02 NTSC-U.iso}"
  #export DOLPHIN_HEADLESS="${SLIPPI_DOLPHIN:-}"  # No headless dolphin build on Windows yet
  export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-C:/Users/Paul/AppData/Roaming/Slippi Launcher/netplay/Slippi Dolphin.exe}"
  export AGENTS_DIR="${SLIPPI_AGENTS:-C:/MELEE/slippi-ai/agents}"
fi

# --- Hardware optimization flags ---
export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR="${TMPDIR:-/tmp}"

# --- Source local overrides if present ---
if [[ -f "$_ENV_DIR/env.local.sh" ]]; then
  source "$_ENV_DIR/env.local.sh"
fi

# --- cd to project root ---
cd "$PROJECT_ROOT"
