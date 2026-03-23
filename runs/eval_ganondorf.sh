#!/usr/bin/env bash

# Evaluate Ganondorf (RL checkpoint) vs top12 opponent.
# Reports KO diff per minute and frames-per-second over a fixed rollout.
#
# Usage:
#   ./runs/eval_ganondorf.sh                         # headless, max speed
#   ./runs/eval_ganondorf.sh --opponent.character=FALCO
#   ./runs/eval_ganondorf.sh --rollout_length=18000  # 5 minutes at 60fps
#
# Watch modes (requires WSLg or an X server on WSL2):
#   WATCH=1    ./runs/eval_ganondorf.sh              # real-time with GUI + audio
#   WATCH=fast ./runs/eval_ganondorf.sh              # fast-forward with GUI, no audio

cd /home/pawl/melee/slippi-ai-launcher

export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

ISO_PATH="/home/pawl/melee/melee.iso"
DOLPHIN_PATH="/home/pawl/melee/dolphin-ai/squashfs-root/AppRun"

# Our current Ganondorf RL checkpoint (update to point at latest run)
GANON_CHECKPOINT="/home/pawl/melee/slippi-ai-launcher/experiments/train_two/ganon_d18_v_top12_d21_run3/ganondorf_delay_18_vs_top12chars-1.pkl"

# Top12 opponent — use the co-trained checkpoint for the toughest test,
# or swap to the base IL model for a fixed reference point:
#   /home/pawl/melee/slippi-ai-launcher/agents/top12_d21_imitation_3x768_v5.pkl
OPPONENT_CHECKPOINT="/home/pawl/melee/slippi-ai-launcher/experiments/train_two/ganon_d18_v_top12_d21_run3/top12chars_delay_21_vs_ganondorf-2.pkl"

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

python scripts/run_evaluator.py \
  --dolphin.path="$DOLPHIN_PATH" \
  --dolphin.iso="$ISO_PATH" \
  "${DOLPHIN_FLAGS[@]}" \
  --player.character=GANONDORF \
  --player.ai.path="$GANON_CHECKPOINT" \
  --player.ai.batch_steps=4 \
  --opponent.character=FOX \
  --opponent.ai.path="$OPPONENT_CHECKPOINT" \
  --opponent.ai.batch_steps=4 \
  --rollout_length=7200 \
  --use_gpu \
  "$@"
