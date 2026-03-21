#!/usr/bin/env bash

# Test multishine consistency across different online_delay settings.
# Sends frame-perfect inputs and counts how many shines land at each delay.
#
# Usage:
#   ./scripts/run_multishine_test.sh                        # defaults
#   ./scripts/run_multishine_test.sh --delays=0,1,2,3,4     # more delays
#   ./scripts/run_multishine_test.sh --runtime=30            # longer test

cd /home/pawl/melee/slippi-ai-launcher

export OMP_NUM_THREADS=1
export TMPDIR=/dev/shm

ISO_PATH="/home/pawl/melee/melee.iso"
DOLPHIN_PATH="/home/pawl/melee/dolphin-ai/Slippi_Netplay_Mainline_ExiAI_NoLeak-x86_64.AppImage"

python scripts/multishine_delay_test.py \
  --dolphin_executable_path="$DOLPHIN_PATH" \
  --iso="$ISO_PATH" \
  "$@"
