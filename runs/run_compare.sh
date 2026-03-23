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
# --cpu_opponent \
# --cpu_opponent_character=BOWSER \
# --cpu_opponent_level=9 \

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

AGENT_PATH="$AGENTS_DIR/medium-v2"

# --- Display mode ---
DOLPHIN_FLAGS=()
case "${WATCH:-}" in
  1|true)
    # Real-time: visible window (requires regular Dolphin, not EXI_AI)
    DOLPHIN_PATH="$DOLPHIN_GUI"
    DOLPHIN_FLAGS+=(--dolphin.headless=False --dolphin.gfx_backend=Vulkan --dolphin.disable_audio)
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
  --dolphin.iso="$MELEE_ISO" \
  --dolphin.stage=BATTLEFIELD \
  "${DOLPHIN_FLAGS[@]}" \
  --dolphin.emulation_speed=0 \
  --dolphin.overclock=4.0 \
  --delays=2,17,20,21 \
  --num_games=2 \
  "$@"
