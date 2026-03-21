#!/usr/bin/env bash

# Compare tech skill execution at different online_delay settings.
# Runs headless AI-vs-AI locally and reports input mismatch rates,
# wavedash counts, L-cancel success, etc.
#
# Usage:
#   ./scripts/run_compare.sh                    # defaults: delays 0,2, 3 games
#   ./scripts/run_compare.sh --delays=0,2,3     # custom delays
#   ./scripts/run_compare.sh --num_games=5      # more games
#   ./scripts/run_compare.sh --use_gpu          # GPU inference

cd /home/pawl/melee/slippi-ai-launcher

export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

ISO_PATH="/home/pawl/melee/melee.iso"
DOLPHIN_PATH="/home/pawl/melee/dolphin-ai/Slippi_Netplay_Mainline_ExiAI_NoLeak-x86_64.AppImage"
AGENT_PATH="/home/pawl/melee/agents/medium-v2"

python scripts/compare_local_vs_netplay.py \
  --agent.path="$AGENT_PATH" \
  --dolphin.path="$DOLPHIN_PATH" \
  --dolphin.iso="$ISO_PATH" \
  "$@"
