#!/usr/bin/env bash

# Evaluate Ganondorf (RL checkpoint) vs top12 opponent.
# Reports KO diff per minute and frames-per-second over a fixed rollout.
#
# Usage:
#   ./runs/eval_ganondorf.sh                         # headless, max speed
#   ./runs/eval_ganondorf.sh --opponent.character=FALCO
#   ./runs/eval_ganondorf.sh --rollout_length=18000  # 5 minutes at 60fps
#
# Watch mode (requires WSLg or an X server on WSL2):
#   WATCH=1    ./runs/eval_ganondorf.sh              # real-time with GUI + audio

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Our current Ganondorf RL checkpoint (update to point at latest run)
GANON_CHECKPOINT="$PROJECT_ROOT/experiments/train_two/ganon_d18_v_top12_d21_run3/ganondorf_delay_18_vs_top12chars-1.pkl"

# Top12 opponent — use the co-trained checkpoint for the toughest test,
# or swap to the base IL model for a fixed reference point:
#   $PROJECT_ROOT/agents/top12_d21_imitation_3x768_v5.pkl
OPPONENT_CHECKPOINT="$PROJECT_ROOT/experiments/train_two/ganon_d18_v_top12_d21_run3/top12chars_delay_21_vs_ganondorf-2.pkl"

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

python scripts/run_evaluator.py \
  --dolphin.path="$DOLPHIN_PATH" \
  --dolphin.iso="$MELEE_ISO" \
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
