"""Test a trained model."""

import collections
import json
import os
import unicodedata
import logging

from absl import app
from absl import flags
import fancyflags as ff

import melee
from slippi_ai import eval_lib, types, utils, saving
from slippi_ai import dolphin as dolphin_lib
from slippi_db.parse_libmelee import get_controller

agent_flags = eval_lib.AGENT_FLAGS.copy()
agent_flags['async_inference'] = ff.Boolean(True)

AGENT = ff.DEFINE_dict('agent', **agent_flags)
CHAR = flags.DEFINE_enum_class('char', melee.Character.FOX, melee.Character, 'Character to use for AI player')

dolphin_flags = dolphin_lib.DOLPHIN_FLAGS.copy()
dolphin_flags.update(
    online_delay=ff.Integer(None),
    connect_code=ff.String(None, required=True),
    user_json_path=ff.String(None, required=True),
    # blocking_input=ff.Boolean(False),
)
DOLPHIN = ff.DEFINE_dict('dolphin', **dolphin_flags)

RUNTIME = flags.DEFINE_integer('runtime', None, 'Runtime in seconds.')
END_MATCH_SENTINEL = flags.DEFINE_string(
    'end_match_sentinel', None,
    'Optional path the launcher touches to request a clean match end. '
    'When this file appears, the agent holds L+R+A+Start for ~60 frames '
    'and exits — used by the Play page "End Match" button.')

# How many frames to hold the LRA+Start combo once the sentinel fires.
# Slippi reset only needs the combo held for ~1s; 90 frames (1.5s at 60fps)
# gives a margin for dropped frames without letting the agent keep playing.
LRA_START_HOLD_FRAMES = 90

# Maximum delay the Dolphin build supports. Depends on gecko code.
# Standard Slippi caps at 9; the custom bot Dolphin supports up to 24.
MAX_DOLPHIN_DELAY = 24

FLAGS = flags.FLAGS

def main(_):
  # Single-agent, batch=1 real-time inference is dominated by kernel-launch
  # overhead on GPU and has caused buggy agent behavior; force CPU.
  eval_lib.disable_gpus()

  port = 1

  agent_state = saving.load_state_from_disk(AGENT.value['path'])

  # Auto-compute console_delay from the model's trained delay, like twitchbot.py.
  # Leave 1 frame of headroom for async inference.
  # If --dolphin.online_delay is explicitly set, use that instead.
  policy_delay = agent_state['config']['policy']['delay']
  dolphin_kwargs = dict(DOLPHIN.value)
  if dolphin_kwargs['online_delay'] is None:
    console_delay = max(policy_delay - 1, 0)
    console_delay = min(console_delay, MAX_DOLPHIN_DELAY)
    dolphin_kwargs['online_delay'] = console_delay
    logging.info(
        f'Auto-computed online_delay={console_delay} from '
        f'policy.delay={policy_delay} (max {MAX_DOLPHIN_DELAY})')
  else:
    console_delay = dolphin_kwargs['online_delay']
    logging.info(f'Using explicit online_delay={console_delay}')

  player = dolphin_lib.AI(
      character=CHAR.value,
  )
  eval_lib.update_character(player, agent_state['config'])

  dolphin = dolphin_lib.Dolphin(
      players={port: player},
      **dolphin_kwargs,
  )

  # Warm up agent before starting game to prevent initial hiccup.
  agent = eval_lib.build_agent(
      controller=dolphin.controllers[port],
      opponent_port=None,  # will be set later
      console_delay=console_delay,
      run_on_cpu=True,
      state=agent_state,
      **AGENT.value,
  )

  try:
    # Start game
    gamestate = dolphin.step()

    # Sentinel consumed by the launcher's bot watchdog — tells the launcher
    # the opponent actually connected and frame 1 is in hand, so the
    # no-connect timeout can stop arming.
    print("[MATCH_STARTED]", flush=True)

    with open(DOLPHIN.value['user_json_path']) as f:
      user_json = json.load(f)
    display_name = user_json['displayName']

    name_to_port = {
        # player.displayName: port for port, player in gamestate.players.items()
        unicodedata.normalize('NFKC', player.displayName): port for port, player in gamestate.players.items()
    }

    actual_port = name_to_port[display_name]
    ports = list(gamestate.players)
    ports.remove(actual_port)
    opponent_port = ports[0]
    agent.players = (actual_port, opponent_port)

    # Main loop
    agent.start()
    agent.step(gamestate)

    num_frames = 1
    sentinel_path = END_MATCH_SENTINEL.value
    # Poll the sentinel once per second (60 frames). A stat() per frame is
    # pointless overhead on a syscall that only serves a human button click —
    # up-to-1s latency is imperceptible to the user and the LRA+Start hold
    # itself takes another 1.5s after detection.
    sentinel_poll_stride = 60

    while True:
      gamestate = dolphin.step()
      agent.step(gamestate)

      num_frames += 1

      if sentinel_path and num_frames % sentinel_poll_stride == 0:
        if os.path.exists(sentinel_path):
          logging.info('End-match sentinel seen — holding LRA+Start.')
          try:
            os.remove(sentinel_path)
          except OSError:
            pass
          agent.stop()
          _hold_lra_start(dolphin, port, LRA_START_HOLD_FRAMES)
          break

      if RUNTIME.value is not None and num_frames >= RUNTIME.value * 60:
        break

  finally:
    try:
      agent.stop()
    except Exception:
      pass
    dolphin.stop()


def _hold_lra_start(dolphin, port, frames):
  """Send L+R+A+Start on the AI's controller for ``frames`` frames, then
  release. This triggers Slippi's mid-match reset so both clients return
  to CSS cleanly instead of the opponent seeing a desync when the netplay
  subprocess terminates."""
  controller = dolphin.controllers[port]
  for _ in range(frames):
    controller.release_all()
    controller.press_shoulder(melee.Button.BUTTON_L, 1.0)
    controller.press_shoulder(melee.Button.BUTTON_R, 1.0)
    controller.press_button(melee.Button.BUTTON_A)
    controller.press_button(melee.Button.BUTTON_START)
    controller.flush()
    dolphin.step()
  controller.release_all()
  controller.flush()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
