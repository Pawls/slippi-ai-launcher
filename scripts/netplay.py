"""Test a trained model."""

import collections
import json
import os
import time
import unicodedata
import logging

from absl import app
from absl import flags
import fancyflags as ff

import melee
from slippi_ai import eval_lib, types, utils, saving
from slippi_ai import dolphin as dolphin_lib
from slippi_ai.dolphin import is_game_state
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
#   - Stagger L → R → A onto the held set a couple frames apart, so each
#     button is definitively registered before the next joins.
#   - Pre-hold L+R+A (no Start) for a few frames so Melee's input handler
#     has the reset-combo shoulders locked in before Start arrives. If
#     Start lands on the same frame as (or before) L+R+A, the game
#     registers it as "pause" first and the reset never triggers — the
#     agent just pops up the pause menu.
#   - Press BOTH the analog shoulder (SET L 1.0) and the digital L/R
#     button. On a real controller the full-press past the click
#     actuates both; Dolphin's pipe treats them as independent, and
#     Melee's reset check reads the digital bit — analog alone is
#     invisible to it, which is why earlier attempts paused.
#   - Then hold all four for ~1.5s so the reset actually fires.
LRA_STAGGER_FRAMES = 2
LRA_PRE_HOLD_FRAMES = 15
LRA_FULL_HOLD_FRAMES = 90
LRA_START_HOLD_FRAMES = LRA_PRE_HOLD_FRAMES + LRA_FULL_HOLD_FRAMES

# Maximum delay the Dolphin build supports. Depends on gecko code.
# Standard Slippi caps at 9; the custom bot Dolphin supports up to 24.
MAX_DOLPHIN_DELAY = 24

# How long (wall-clock seconds) to wait without a fresh gamestate before we
# assume the peer has disconnected and quit. Slippi doesn't transition the
# emulated menu_state on disconnect — it shows a Dolphin-level dialog and
# stops emitting SLP frames — so the only signal we can key off from the
# Python side is "no frames arriving." 5s is long enough to ride out a
# normal CSS load / stage transition (~1-2s) without false-positive.
POST_START_STALL_SECONDS = 5.0

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
    # Wall-clock sentinel + stall polling. Frame count is unreliable as a
    # clock here: during a peer disconnect Dolphin stops emitting frames
    # entirely, so we'd never hit the stride. Using time.monotonic means
    # both the Stop-button poll and the stall detector keep ticking even
    # when the game itself has gone quiet.
    sentinel_poll_interval = 1.0
    last_sentinel_poll = time.monotonic()
    last_fresh_gamestate = time.monotonic()

    # Track the last in-game stock/percent snapshot so we can determine a
    # winner when the menu transitions out of IN_GAME. libmelee's gamestate
    # only exposes live stocks, so we have to cache them ourselves.
    last_in_game = {}  # {port: (stock, percent)}
    saw_in_game = False
    saw_postgame = False  # separates natural game-end from mid-match disconnect
    match_started_announced = False
    game_result_reported = False
    start_time = time.monotonic()

    def _poll_sentinel_and_stall():
      """Shared exit-path check: returns a truthy tuple (reason, ended_cleanly)
      if we should break the main loop, else None. Uses wall-clock so it
      works both during normal play and during peer-disconnect stalls when
      frames stop arriving."""
      now = time.monotonic()
      nonlocal last_sentinel_poll
      if sentinel_path and now - last_sentinel_poll >= sentinel_poll_interval:
        last_sentinel_poll = now
        if os.path.exists(sentinel_path):
          try:
            os.remove(sentinel_path)
          except OSError:
            pass
          return ('sentinel', False)
      # Only start the stall clock after we've seen at least one IN_GAME
      # frame; before then, a slow CSS/matchmaking load would false-trip.
      if saw_in_game and now - last_fresh_gamestate >= POST_START_STALL_SECONDS:
        logging.info(
            'No fresh gamestate for %.1fs after match start — assuming '
            'peer disconnected.', now - last_fresh_gamestate)
        return ('stall', False)
      return None

    while True:
      # Key fix: use next_gamestate() (not step()) inside the main loop.
      # step() auto-navigates menus via menu_helper and only returns IN_GAME
      # frames — which means after match 1 ends, step() would happily drive
      # the AI back into CSS for a rematch, and the outer loop would never
      # observe the transition. next_gamestate() returns whatever frame
      # Slippi emitted, menu or not, so we can break on the first post-match
      # frame and exit cleanly.
      #
      # console_timeout (set by the launcher via DOLPHIN_FLAGS) puts
      # libmelee into polling mode; next_gamestate then returns None (→
      # TimeoutError in libmelee's wrapper) when no frame arrives in that
      # window, letting us observe stalls.
      try:
        gamestate = dolphin.next_gamestate()
      except TimeoutError:
        exit_reason = _poll_sentinel_and_stall()
        if exit_reason is not None:
          reason, ended_cleanly = exit_reason
          if saw_in_game and not game_result_reported and last_in_game:
            _emit_game_result(
                actual_port, opponent_port, last_in_game,
                ended_cleanly=ended_cleanly,
            )
            game_result_reported = True
          agent.stop()
          # LRA+Start on disconnect/stall serves double duty: on a clean
          # sentinel press it resets the remote peer out of postgame; on
          # a stall the peer is gone anyway but the hold dismisses our
          # own Dolphin's "Disconnected" dialog so the subsequent
          # dolphin.stop() doesn't leave a zombie modal.
          _hold_lra_start(dolphin, port, LRA_START_HOLD_FRAMES)
          break
        continue

      last_fresh_gamestate = time.monotonic()
      num_frames += 1

      # Record stocks/percent every frame while IN_GAME, then detect the
      # transition back to a menu and emit the winner. Announce
      # [MATCH_STARTED] only on the first IN_GAME frame (not on the very
      # first dolphin.step() — that returns menu frames during
      # matchmaking/CSS and would disarm the no-connect watchdog before
      # the game actually starts).
      menu = gamestate.menu_state
      if is_game_state(gamestate):
        # Only feed the agent in-game frames. It's trained for in-match
        # inputs; a menu gamestate with no live players would either
        # no-op or fail a shape check.
        agent.step(gamestate)
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
          # First post-match menu frame. Clean game-end reaches
          # POSTGAME_SCORES first; mid-match opponent disconnect skips
          # to CSS/main menu. Launcher keys on ended= to decide whether
          # to fire a taunt. One match per launch — we break out rather
          # than letting libmelee's menu_helper drive us into a rematch.
          _emit_game_result(
              actual_port, opponent_port, last_in_game,
              ended_cleanly=saw_postgame,
          )
          game_result_reported = True
          break

      exit_reason = _poll_sentinel_and_stall()
      if exit_reason is not None:
        reason, ended_cleanly = exit_reason
        logging.info('End-match %s — holding LRA+Start.', reason)
        if saw_in_game and not game_result_reported and last_in_game:
          _emit_game_result(
              actual_port, opponent_port, last_in_game,
              ended_cleanly=ended_cleanly,
          )
          game_result_reported = True
        agent.stop()
        _hold_lra_start(dolphin, port, LRA_START_HOLD_FRAMES)
        break

      if RUNTIME.value is not None and time.monotonic() - start_time >= RUNTIME.value:
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

  See the comment block on ``LRA_PRE_HOLD_FRAMES`` for the timing and
  analog-vs-digital shoulder rationale."""
  controller = dolphin.controllers[port]
  add_l = 0
  add_r = LRA_STAGGER_FRAMES
  add_a = 2 * LRA_STAGGER_FRAMES
  add_start = LRA_PRE_HOLD_FRAMES
  for frame in range(frames):
    controller.release_all()
    if frame >= add_l:
      controller.press_shoulder(melee.Button.BUTTON_L, 1.0)
      controller.press_button(melee.Button.BUTTON_L)
    if frame >= add_r:
      controller.press_shoulder(melee.Button.BUTTON_R, 1.0)
      controller.press_button(melee.Button.BUTTON_R)
    if frame >= add_a:
      controller.press_button(melee.Button.BUTTON_A)
    if frame >= add_start:
      controller.press_button(melee.Button.BUTTON_START)
    controller.flush()
    dolphin.step()
  controller.release_all()
  controller.flush()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
