#!/usr/bin/env bash

# Test multishine consistency across different online_delay settings.
# Sends frame-perfect inputs and counts how many shines land at each delay.
#
# Usage:
#   ./runs/run_multishine_test.sh                        # defaults
#   ./runs/run_multishine_test.sh --delays=0,1,2,3,4     # more delays
#   ./runs/run_multishine_test.sh --runtime=30            # longer test

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

python scripts/multishine_delay_test.py \
  --dolphin_executable_path="$DOLPHIN_HEADLESS" \
  --iso="$MELEE_ISO" \
  --delays=0,1,2,3,4 \
  --runtime=30 \
  "$@"
