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

# How we schedule the LRA+Start reset combo:
#   - Pre-hold L+R+A (no Start) for a few frames so Melee's input handler
#     has the reset-combo shoulders locked in before Start arrives. If
#     Start lands on the same frame as (or before) L+R+A, the game
#     registers it as "pause" first and the reset never triggers — the
#     agent just pops up the pause menu.
#   - Then hold all four for ~1.5s so the reset actually fires.
LRA_PRE_HOLD_FRAMES = 15
LRA_FULL_HOLD_FRAMES = 90
LRA_START_HOLD_FRAMES = LRA_PRE_HOLD_FRAMES + LRA_FULL_HOLD_FRAMES

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
    # First frame — can be any menu state (CSS, matchmaking screen,
    # postgame, etc.). We do NOT announce the match has started here;
    # Dolphin returns menu frames long before IN_GAME is reached, and an
    # early announcement was disarming the launcher's no-connect watchdog.
    gamestate = dolphin.step()

    with open(DOLPHIN.value['user_json_path']) as f:
      user_json = json.load(f)
    display_name = user_json['displayName']

    name_to_port = {
        # player.displayName: port for port, player in gamestate.players.items()
        unicodedata.normalize('NFKC', player.displayName): port for port, player in gamestate.players.items()
    }

    actual_port = name_to_port.get(display_name)
    if actual_port is None:
      # Before matchmaking lands a peer, the gamestate's player list may
      # not include our display name yet. Re-resolve once we actually
      # reach IN_GAME. Use a deferred binding: we'll recompute below.
      actual_port = port
    ports = list(gamestate.players) or [port]
    if actual_port in ports:
      ports.remove(actual_port)
    opponent_port = ports[0] if ports else (2 if actual_port == 1 else 1)
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

    # Track the last in-game stock/percent snapshot so we can determine a
    # winner when the menu transitions out of IN_GAME. libmelee's gamestate
    # only exposes live stocks, so we have to cache them ourselves.
    last_in_game = {}  # {port: (stock, percent)}
    saw_in_game = False
    saw_postgame = False  # separates natural game-end from mid-match disconnect
    match_started_announced = False
    game_result_reported = False

    while True:
      gamestate = dolphin.step()
      agent.step(gamestate)

      num_frames += 1

      # Record stocks/percent every frame while IN_GAME, then detect the
      # transition back to a menu and emit the winner. Announce
      # [MATCH_STARTED] only on the first IN_GAME frame (not on the very
      # first dolphin.step() — that returns menu frames during
      # matchmaking/CSS and would disarm the no-connect watchdog before
      # the game actually starts).
      menu = gamestate.menu_state
      if menu == melee.Menu.IN_GAME:
        if not saw_in_game:
          saw_in_game = True
          # Re-resolve the agent port now that the gamestate has real
          # player data — the initial resolution above happens before
          # matchmaking and is often wrong.
          try:
            resolved = {
                unicodedata.normalize('NFKC', p.displayName): pp
                for pp, p in gamestate.players.items()
            }
            if display_name in resolved:
              actual_port = resolved[display_name]
              others = [p for p in gamestate.players if p != actual_port]
              if others:
                opponent_port = others[0]
                agent.players = (actual_port, opponent_port)
          except Exception:
            pass
        if not match_started_announced:
          # Sentinel consumed by the launcher's bot watchdog. Fires on
          # the first IN_GAME frame so the no-connect timeout only disarms
          # once the match is genuinely in progress.
          print("[MATCH_STARTED]", flush=True)
          match_started_announced = True
        for p, ps in gamestate.players.items():
          last_in_game[p] = (ps.stock, ps.percent)
      else:
        if menu == melee.Menu.POSTGAME_SCORES:
          saw_postgame = True
        if saw_in_game and not game_result_reported and last_in_game:
          # Clean game-end reaches POSTGAME_SCORES; mid-match opponent
          # disconnect skips straight to CSS/main menu. Launcher keys on
          # ended= to decide whether to fire a taunt.
          _emit_game_result(
              actual_port, opponent_port, last_in_game,
              ended_cleanly=saw_postgame,
          )
          game_result_reported = True
          break

      if sentinel_path and num_frames % sentinel_poll_stride == 0:
        if os.path.exists(sentinel_path):
          logging.info('End-match sentinel seen — holding LRA+Start.')
          try:
            os.remove(sentinel_path)
          except OSError:
            pass
          # Rage-quit mid-match = treat as disconnect from the launcher's
          # POV (ended_cleanly=False). If nobody reached IN_GAME yet,
          # there's nothing meaningful to report.
          if saw_in_game and not game_result_reported and last_in_game:
            _emit_game_result(
                actual_port, opponent_port, last_in_game,
                ended_cleanly=False,
            )
            game_result_reported = True
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


def _emit_game_result(ai_port, human_port, last_in_game, ended_cleanly):
  """Print a [GAME_RESULT] sentinel the launcher parses to update the
  per-user W/L record. Winner = port with more stocks; on stock tie (time
  out) the lower % wins; a genuine double-KO reports 'draw'.

  ``ended_cleanly`` distinguishes a natural game end (POSTGAME_SCORES
  was reached) from a mid-match opponent disconnect. The launcher uses
  it to decide whether to fire a "you ran away" taunt or stay silent."""
  ai_stock, ai_pct = last_in_game.get(ai_port, (0, 0.0))
  hu_stock, hu_pct = last_in_game.get(human_port, (0, 0.0))
  if ai_stock > hu_stock:
    winner = 'ai'
  elif hu_stock > ai_stock:
    winner = 'human'
  elif ai_pct < hu_pct:
    winner = 'ai'
  elif hu_pct < ai_pct:
    winner = 'human'
  else:
    winner = 'draw'
  ended = 'clean' if ended_cleanly else 'disconnect'
  print(
      f"[GAME_RESULT] winner={winner} ended={ended} "
      f"ai_stocks={ai_stock} human_stocks={hu_stock} "
      f"ai_pct={ai_pct:.1f} human_pct={hu_pct:.1f}",
      flush=True,
  )


def _hold_lra_start(dolphin, port, frames):
  """Drive L+R+A+Start on the AI's controller to trigger Melee's in-game
  reset, returning both clients cleanly to the CSS instead of desyncing
  when the netplay subprocess dies.

  The combo is sequenced, not simultaneous: L+R+A come in first for
  ``LRA_PRE_HOLD_FRAMES``, then Start joins. Melee's pause logic would
  otherwise claim the Start press on the first frame before the reset
  combo was fully established, and the agent would just pop the pause
  menu instead of resetting."""
  controller = dolphin.controllers[port]
  for frame in range(frames):
    controller.release_all()
    controller.press_shoulder(melee.Button.BUTTON_L, 1.0)
    controller.press_shoulder(melee.Button.BUTTON_R, 1.0)
    controller.press_button(melee.Button.BUTTON_A)
    if frame >= LRA_PRE_HOLD_FRAMES:
      controller.press_button(melee.Button.BUTTON_START)
    controller.flush()
    dolphin.step()
  controller.release_all()
  controller.flush()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
