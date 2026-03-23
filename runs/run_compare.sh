#!/usr/bin/env bash

# Compare tech skill execution at different online_delay settings.
# Runs headless AI-vs-AI locally and reports input mismatch rates,
# wavedash counts, L-cancel success, etc.
#
# Usage:
#   ./runs/run_compare.sh                    # defaults: headless, max speed
#   ./runs/run_compare.sh --delays=0,2,3     # custom delays
#   ./runs/run_compare.sh --num_games=5      # more games
#
# Watch mode (requires WSLg or an X server on WSL2):
#   WATCH=1    ./runs/run_compare.sh         # real-time with GUI, ~60fps

cd /home/pawl/melee/slippi-ai-launcher

export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

# EXI_AI build for headless (fast), regular Slippi for watch mode (has video)
DOLPHIN_HEADLESS="/home/pawl/melee/dolphin-ai/squashfs-root/AppRun"
DOLPHIN_GUI="/home/pawl/.config/Slippi Launcher/netplay/Slippi_Online-x86_64.AppImage"
AGENT_PATH="/home/pawl/melee/agents/medium-v2"
ISO_PATH="/home/pawl/melee/melee.iso"

# --- Display mode ---
DOLPHIN_FLAGS=()
case "${WATCH:-}" in
  1|true)
    # Real-time: visible window (requires regular Dolphin)
    # blocking_input=False lets Dolphin run at native speed instead of
    # waiting for Python inference every frame (~30fps → ~60fps)
    DOLPHIN_PATH="$DOLPHIN_GUI"
    DOLPHIN_FLAGS+=(--dolphin.headless=False --dolphin.blocking_input=False --dolphin.disable_audio)
    ;;
  *)
    # Default: headless, max speed, no GUI (EXI_AI build)
    DOLPHIN_PATH="$DOLPHIN_HEADLESS"
    DOLPHIN_FLAGS+=(--dolphin.headless)
    ;;
esac

python scripts/compare_local_vs_netplay.py \
  --agent.path="$AGENT_PATH" \
  --dolphin.path="$DOLPHIN_PATH" \
  --dolphin.iso="$ISO_PATH" \
  "${DOLPHIN_FLAGS[@]}" \
  --delays=21 \
  --num_games=1 \
  "$@"

