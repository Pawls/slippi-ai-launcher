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
# Detect environment and set sensible defaults for Linux/WSL and Windows shells.
_uname="$(uname -s 2>/dev/null || true)"
_is_wsl=false
_is_windows_shell=false
if [ -n "$WSL_DISTRO_NAME" ] || (grep -qiE 'microsoft' /proc/version 2>/dev/null); then
  _is_wsl=true
fi
if echo "$_uname" | grep -qiE 'mingw|msys|cygwin|nt'; then
  _is_windows_shell=true
fi

if [ "$_is_windows_shell" = true ]; then
  # Native Windows (Git Bash / MSYS / Cygwin) - prefer Windows-style paths
  export MELEE_ISO="${MELEE_ISO:-C:/Game Console Emulators/GC ISOs/Super Smash Bros. Melee v1.02 NTSC-U.iso}"
  export DOLPHIN_HEADLESS="${SLIPPI_DOLPHIN:-C:/MELEE/Slippi-ExiAi-NoGui/Slippi_Dolphin.exe}"
  export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-C:/Users/Paul/AppData/Roaming/Slippi Launcher/netplay/Slippi Dolphin.exe}"
  export AGENTS_DIR="${SLIPPI_AGENTS:-C:/MELEE/slippi-ai/agents}"
  export TMPDIR="${TMPDIR:-/tmp}"
else
  # Linux or WSL: prefer Linux-style paths (user can override via env or env.local.sh)
  export MELEE_ISO="${MELEE_ISO:-/home/pawl/melee/melee.iso}"
  export DOLPHIN_HEADLESS="${SLIPPI_DOLPHIN:-/home/pawl/melee/dolphin-ai/squashfs-root/AppRun}"
  export DOLPHIN_GUI="${SLIPPI_DOLPHIN_GUI:-/home/pawl/.config/Slippi Launcher/netplay/Slippi_Online-x86_64.AppImage}"
  export AGENTS_DIR="${SLIPPI_AGENTS:-/home/pawl/melee/agents}"
  export TMPDIR="${TMPDIR:-/dev/shm}"
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
