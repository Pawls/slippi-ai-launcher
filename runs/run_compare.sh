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
# Watch modes (requires WSLg or an X server on WSL2):
#   WATCH=1    ./runs/run_compare.sh         # real-time with GUI + audio
#   WATCH=fast ./runs/run_compare.sh         # fast-forward with GUI, no audio

cd /home/pawl/melee/slippi-ai-launcher

export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

DOLPHIN_PATH="/home/pawl/melee/dolphin-ai/Slippi_Netplay_Mainline_ExiAI_NoLeak-x86_64.AppImage"
AGENT_PATH="/home/pawl/melee/agents/medium-v2"
ISO_PATH="/home/pawl/melee/melee.iso"

# --- Display mode ---
DOLPHIN_FLAGS=()
case "${WATCH:-}" in
  1|true)
    # Real-time: visible window, audio, normal game speed
    DOLPHIN_FLAGS+=(--dolphin.noheadless)
    ;;
  fast)
    # Fast-forward: visible window, EXI speed, no audio
    DOLPHIN_FLAGS+=(--dolphin.headless --dolphin.render)
    ;;
  *)
    # Default: headless, max speed, no GUI
    DOLPHIN_FLAGS+=(--dolphin.headless)
    ;;
esac

python scripts/compare_local_vs_netplay.py \
  --agent.path="$AGENT_PATH" \
  --dolphin.path="$DOLPHIN_PATH" \
  --dolphin.iso="$ISO_PATH" \
  "${DOLPHIN_FLAGS[@]}" \
  --use_gpu \
  --delays=0,2,19,20,21 \
  --num_games=1 \
  "$@"

