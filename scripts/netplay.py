"""Test a trained model."""

import collections
import gc
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
from slippi_ai.dolphin import is_game_state, is_menu_state
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
    'When this file appears, the agent holds Start+L+R+A to force-quit '
    'the active game and exits — used by the Play page "End Match" button.')

# How we schedule the Start+LRA mid-game quit combo:
#   - Press Start FIRST and hold it. The pause menu has to be open for
#     the quit to register; if L+R+A arrive first (or concurrently),
#     Melee interprets the input as the reset combo and the quit never
#     happens.
#   - Give Start a few frames on its own so the pause menu actually
#     renders before the shoulders join.
#   - Then stagger L → R → A onto the held set a couple frames apart,
#     so each button is definitively registered before the next joins.
#   - Press BOTH the analog shoulder (SET L 1.0) and the digital L/R
#     button. On a real controller the full-press past the click
#     actuates both; Dolphin's pipe treats them as independent, and
#     Melee's quit check reads the digital bit — analog alone is
#     invisible to it.
#   - Then hold all four for ~1.5s so the quit actually fires.
LRA_STAGGER_FRAMES = 2
LRA_PRE_HOLD_FRAMES = 15
LRA_FULL_HOLD_FRAMES = 90
LRA_START_HOLD_FRAMES = LRA_PRE_HOLD_FRAMES + LRA_FULL_HOLD_FRAMES

# Maximum delay the Dolphin build supports. Depends on gecko code.
# Standard Slippi caps at 9; the custom bot Dolphin supports up to 24.
MAX_DOLPHIN_DELAY = 24

# How long (wall-clock seconds) to wait without a fresh gamestate before we
# assume the peer has disconnected mid-match. Slippi stops emitting SLP
# frames when a peer drops, so "no frames arriving" is the only signal from
# the Python side. 5s is long enough to ride out a normal CSS load / stage
# transition (~1-2s) without false-positive.
POST_START_STALL_SECONDS = 5.0

# How long to watch the opponent's CSS slot show CONTROLLER_UNPLUGGED
# before deciding they've left. Needs a small debounce because right after
# POSTGAME_SCORES libmelee sometimes reports the opponent's character as
# UNKNOWN for a frame or two while the CSS reloads — the UNPLUGGED status
# is the stronger signal but still worth waiting on.
OPPONENT_GONE_GRACE_SECS = 3.0

# How long we'll sit on menu frames without seeing IN_GAME before giving
# up and freeing the bot for the next challenger. Covers the "opponent
# went AFK on the CSS" case. Matches the spirit of the challenge timeout.
POST_MATCH_MAX_MENU_SECS = 60.0

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
    menu_frames = 0
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
    rematch_boundary = False
    opp_unplugged_since = None
    post_match_menu_since = None
    # For rage-quit detection: track whether the previous frame was
    # IN_GAME so we can catch the exact transition back to a menu. If
    # the opponent ended the match abruptly (reset combo / exit to CSS)
    # but stayed connected, we want to tap out the in-game chat message
    # once, then let menu_helper drive the rematch as usual.
    prev_was_in_game = False
    taunted_this_game = False
    # Opponent's last in-game action-state; only emit a
    # [LIVE_EVENT_FRAME] sentinel on transition. Reset on first frame.
    last_opp_action = None
    start_time = time.monotonic()

    # ── Inference health stats ──
    # Local, GIL-only counters updated on each in-game frame around
    # agent.step(). All reads/writes are int/float ops — no thread
    # boundary, no I/O. Periodic flush is a single print() every
    # HEALTH_EMIT_INTERVAL_FRAMES (~2s of game time), so the hot path
    # is essentially free.
    HEALTH_EMIT_INTERVAL_FRAMES = 120
    # Frame budget: Melee runs at 60Hz (16.67ms/frame). 14ms keeps a
    # ~2.5ms safety margin for controller flush + Dolphin IO; anything
    # past that is at real risk of producing a stale input.
    INFER_OVERRUN_THRESHOLD_MS = 14.0
    infer_step_count = 0
    infer_step_total_ms = 0.0
    infer_step_max_ms = 0.0
    infer_overruns = 0
    last_health_emit_frame = 0

    def _emit_inference_health():
      """Print one [INFER_HEALTH] sentinel. Called at most every
      HEALTH_EMIT_INTERVAL_FRAMES in-game frames + once at shutdown.

      ``steps`` and ``mean_ms`` are cumulative since match start
      (stable long-run average); ``max_ms`` and ``overruns`` reset
      after each emit so the GUI card reflects the LAST interval, not
      lifetime worst — a one-off spike (e.g. screenshot tool stealing
      CPU) doesn't pin the card red after recovery."""
      nonlocal infer_step_max_ms, infer_overruns
      mean_ms = (
          infer_step_total_ms / infer_step_count
          if infer_step_count else 0.0)
      print(
          f"[INFER_HEALTH] steps={infer_step_count} "
          f"mean_ms={mean_ms:.2f} max_ms={infer_step_max_ms:.2f} "
          f"overruns={infer_overruns}",
          flush=True)
      # Reset interval stats AFTER emitting so the card sees the last
      # interval's worst case, not all-time worst.
      infer_step_max_ms = 0.0
      infer_overruns = 0

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

    # Disable Python's cyclic GC for the duration of the in-game
    # frame loop. Reference counting still cleans up per-frame
    # allocations (tuples, strings, etc.); we only lose collection
    # of reference cycles, which the hot path effectively doesn't
    # create. This eliminates multi-ms GC sweep pauses that were
    # the dominant remaining cause of step-time spikes after the
    # earlier launcher-side fixes — a standard real-time-Python
    # technique. Re-enabled in `finally` so anything outside this
    # loop is unaffected.
    gc.disable()

    while True:
      # next_gamestate() (not step()) so menu frames are visible to us.
      # We drive menu_helper_simple ourselves below — that's what step()
      # does internally — but intercepting the menu frames first lets us
      # bail when the peer has left instead of blindly navigating into a
      # doomed "searching for opponent" CSS state.
      #
      # console_timeout (set by the launcher via DOLPHIN_FLAGS) puts
      # libmelee into polling mode; next_gamestate then returns None (→
      # TimeoutError in libmelee's wrapper) when no frame arrives in that
      # window, letting us observe mid-game stalls.
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
          # On user-Stop while still in-game, force-quit via Start+L+R+A
          # (StartLRA quit combo, NOT LRAStart reset) before the menu-
          # wait below. Without this the End-Match button never actually
          # ends the active match — _graceful_exit would just sit on
          # IN_GAME frames until the menu_wait budget ran out. Gate on
          # prev_was_in_game because Start+L+R+A on CSS opens unrelated
          # overlays.
          if reason == 'sentinel' and prev_was_in_game:
            _hold_start_lra(dolphin, port, LRA_START_HOLD_FRAMES)
          # Wait for the game to settle to a menu, then (on a real
          # disconnect / rage-quit) tap out the chat message before
          # closing. Sentinel = user Stop, no taunt. TimeoutError is
          # tolerated throughout so a stalled peer doesn't crash us.
          _graceful_exit(
              dolphin, port, taunt=(reason != 'sentinel'))
          break
        continue

      now = time.monotonic()
      last_fresh_gamestate = now
      num_frames += 1

      menu = gamestate.menu_state
      if is_game_state(gamestate):
        # Back in-game — clear any menu-side disconnect streaks, and on a
        # rematch boundary flip per-game reporting state so the next
        # POSTGAME_SCORES emits its own [GAME_RESULT] instead of
        # short-circuiting on the previous game's.
        opp_unplugged_since = None
        post_match_menu_since = None
        if rematch_boundary:
          last_in_game.clear()
          game_result_reported = False
          saw_postgame = False
          taunted_this_game = False
          rematch_boundary = False

        prev_was_in_game = True
        # Re-resolve the agent ports BEFORE the first agent.step in
        # this main-loop iteration. Agent.step rebuilds its Parser on
        # gamestate.frame == -123 using a snapshot of self.players at
        # that moment — if we step before re-resolving, the parser
        # gets locked into stale ports for the entire game and the
        # neural network sees the wrong "self" / "opponent" features.
        # Symptom of stale Parser: bot stands around / does
        # nonsensical things despite fast inference. Pre-loop port
        # resolution can silently fall back to (1, 2), so this re-
        # resolve from a real in-game frame's player list is what
        # actually anchors the Parser to the right ports.
        if not saw_in_game:
          saw_in_game = True
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
          # Diagnostic sentinel — once per match. Lets the GUI / log
          # confirm the resolved port mapping without scraping
          # gamestate dumps.
          print(
              f"[PORT_RESOLVED] actual_port={actual_port} "
              f"opponent_port={opponent_port} "
              f"display_name={display_name!r}",
              flush=True)
        # Time the agent step locally — perf_counter is sub-µs and the
        # arithmetic below is a handful of int/float ops, so this adds
        # negligible cost to the hot path. agent.step() blocks on the
        # async-inference output queue, so this directly captures the
        # frame-budget pressure the user cares about.
        _t0 = time.perf_counter()
        agent.step(gamestate)
        _step_ms = (time.perf_counter() - _t0) * 1000.0
        infer_step_count += 1
        infer_step_total_ms += _step_ms
        if _step_ms > infer_step_max_ms:
          infer_step_max_ms = _step_ms
        if _step_ms > INFER_OVERRUN_THRESHOLD_MS:
          infer_overruns += 1
          # Spike trace — fires only on actual overruns (rare), so
          # zero per-frame cost in the common case. Goes to the
          # Output log so we can correlate spikes with surrounding
          # sentinels ([LIVE_EVENT_FRAME], [INFER_HEALTH], etc.) to
          # identify any remaining culprit beyond GC pauses.
          print(
              f"[INFER_SPIKE] frame={num_frames} step_ms={_step_ms:.2f}",
              flush=True)
        if num_frames - last_health_emit_frame >= HEALTH_EMIT_INTERVAL_FRAMES:
          last_health_emit_frame = num_frames
          _emit_inference_health()
        # Emit a [LIVE_EVENT_FRAME] sentinel only when the opponent's
        # action-state changes. The launcher parses these from stdout
        # and runs the detectors there — keeping detection out of this
        # subprocess avoids GIL contention with async TF inference,
        # which has only 1 frame of headroom and goes out of sync if a
        # second Python-heavy thread runs alongside it. The transition
        # filter keeps stdout volume low even though pros are rarely
        # idle.
        try:
          opp = gamestate.players.get(opponent_port)
          if opp is not None:
            a = int(opp.action.value)
            if a != last_opp_action:
              last_opp_action = a
              print(
                  f"[LIVE_EVENT_FRAME] port={opponent_port} action={a} "
                  f"frame={num_frames}",
                  flush=True)
        except Exception:
          # Defensive — extracting action from a malformed gamestate
          # might hypothetically fail. A single-frame drop is benign.
          pass
        if not match_started_announced:
          # Sentinel consumed by the launcher's bot watchdog. Fires on
          # the first IN_GAME frame so the no-connect timeout only disarms
          # once the match is genuinely in progress.
          print("[MATCH_STARTED]", flush=True)
          match_started_announced = True
        # Cache last in-game stocks/percent so [GAME_RESULT] can
        # determine the winner without libmelee's help (libmelee has
        # no winner attribute and POSTGAME_SCORES isn't guaranteed to
        # preserve in-game stock values; mid-match disconnects also
        # freeze frames entirely, leaving this cache as the only
        # record). Skip the full rebuild every frame — stocks change
        # only on KO (≤12 events per game), and percent only matters
        # as a time-out tiebreaker, so refresh at ~1Hz suffices.
        do_snap = num_frames % 60 == 0
        if not do_snap:
          for p, ps in gamestate.players.items():
            if last_in_game.get(p, (None,))[0] != ps.stock:
              do_snap = True
              break
        if do_snap:
          for p, ps in gamestate.players.items():
            last_in_game[p] = (ps.stock, ps.percent)
      else:
        # Menu frame. Emit the prior game's result on the first
        # POSTGAME_SCORES we see, then let menu_helper spam through to
        # the next rematch — unless the peer has actually left.

        # First menu frame after IN_GAME: check for "rage-quit stayed
        # connected". Signal: game ended on something other than
        # POSTGAME_SCORES, opponent slot is still plugged in, and neither
        # player had hit 0 stocks on the last in-game frame. Tap D-pad
        # right to fire the in-game chat message, then let the main loop
        # continue — menu_helper will drive the rematch as usual.
        if prev_was_in_game and saw_in_game and not taunted_this_game:
          opp = gamestate.players.get(opponent_port)
          opp_connected = (
              opp is not None
              and opp.controller_status !=
                  melee.ControllerStatus.CONTROLLER_UNPLUGGED
          )
          ai_st, _ = last_in_game.get(actual_port, (0, 0.0))
          hu_st, _ = last_in_game.get(opponent_port, (0, 0.0))
          if (
              menu != melee.Menu.POSTGAME_SCORES
              and opp_connected
              and ai_st > 0 and hu_st > 0
          ):
            logging.info(
                'Rage-quit detected (menu=%s, AI stocks=%d, human stocks=%d, '
                'peer still connected) — sending chat taunt.',
                menu.name, ai_st, hu_st)
            # Mark the rematch boundary and emit a result even though we
            # never see POSTGAME_SCORES — the game did end, just not
            # cleanly. Launcher keys on ended=disconnect to fire a taunt
            # webhook if configured.
            rematch_boundary = True
            if not game_result_reported:
              _emit_game_result(
                  actual_port, opponent_port, last_in_game,
                  ended_cleanly=False,
              )
              game_result_reported = True
            _spam_dpad_right(dolphin, port)
            taunted_this_game = True
        prev_was_in_game = False

        if menu == melee.Menu.POSTGAME_SCORES:
          saw_postgame = True
          rematch_boundary = True
          if saw_in_game and not game_result_reported and last_in_game:
            _emit_game_result(
                actual_port, opponent_port, last_in_game,
                ended_cleanly=True,
            )
            game_result_reported = True

        if saw_in_game:
          if post_match_menu_since is None:
            post_match_menu_since = now

          # --- Peer-left detection ---
          # PRESS_START is the title screen: only reached if they fully
          # backed out. CSS with an UNPLUGGED opponent slot is the
          # screenshotted state ("Press START to enter code" panel, no
          # "Searching…"). Debounce the UNPLUGGED check — right after
          # postgame the slot can briefly flicker while the CSS reloads.
          disconnect_reason = None
          if menu == melee.Menu.PRESS_START:
            disconnect_reason = 'peer_left_title'
          else:
            opp = gamestate.players.get(opponent_port)
            opp_unplugged = (
                opp is None
                or opp.controller_status ==
                    melee.ControllerStatus.CONTROLLER_UNPLUGGED
            )
            if menu == melee.Menu.SLIPPI_ONLINE_CSS and opp_unplugged:
              if opp_unplugged_since is None:
                opp_unplugged_since = now
              elif now - opp_unplugged_since >= OPPONENT_GONE_GRACE_SECS:
                disconnect_reason = 'peer_left_css'
            else:
              opp_unplugged_since = None

          if disconnect_reason is None and \
              now - post_match_menu_since >= POST_MATCH_MAX_MENU_SECS:
            disconnect_reason = 'idle_timeout'

          if disconnect_reason is not None:
            logging.info('Bot exiting: %s.', disconnect_reason)
            if not game_result_reported and last_in_game:
              _emit_game_result(
                  actual_port, opponent_port, last_in_game,
                  ended_cleanly=saw_postgame,
              )
              game_result_reported = True
            agent.stop()
            _graceful_exit(dolphin, port, taunt=True)
            break

        # Drive libmelee's menu helper for the rematch flow. Same calls
        # dolphin.step() would make internally; we just do them here so
        # the outer loop can inspect menu frames first.
        for i, (controller, player) in enumerate(dolphin._menuing_controllers):
          dolphin.menu_helper.menu_helper_simple(
              gamestate, controller,
              stage_selected=dolphin.stage,
              connect_code=dolphin._connect_code,
              autostart=dolphin._autostart and i == 0 and menu_frames > 30,
              swag=False,
              costume=i,
              **player.menuing_kwargs(),
          )
        menu_frames += 1

      exit_reason = _poll_sentinel_and_stall()
      if exit_reason is not None:
        reason, ended_cleanly = exit_reason
        logging.info('End-match %s — closing gracefully.', reason)
        if saw_in_game and not game_result_reported and last_in_game:
          _emit_game_result(
              actual_port, opponent_port, last_in_game,
              ended_cleanly=ended_cleanly,
          )
          game_result_reported = True
        agent.stop()
        # See the matching sentinel branch above: force-quit via the
        # Start+L+R+A combo when the user pressed End-Match while we
        # were still in-game; skip on menu frames where the combo would
        # open an unrelated overlay.
        if reason == 'sentinel' and prev_was_in_game:
          _hold_start_lra(dolphin, port, LRA_START_HOLD_FRAMES)
        _graceful_exit(dolphin, port, taunt=(reason != 'sentinel'))
        break

      if RUNTIME.value is not None and time.monotonic() - start_time >= RUNTIME.value:
        break

  finally:
    # Re-enable cyclic GC first so any cleanup work below runs with
    # full GC available. No-op if it was never disabled (e.g. early
    # exit before reaching the main loop).
    gc.enable()
    # Final inference-health summary so the launcher always has a
    # closing snapshot, even if the periodic emitter never reached
    # its first interval (very short matches).
    try:
      _emit_inference_health()
    except Exception:
      pass
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


def _spam_dpad_right(dolphin, port, warmup_frames=30, taps=4, tap_hold=5,
                     tap_gap=10, trailing_idle_frames=60):
  """On a rage-quit-stayed-connected, fire Slippi's in-game chat message
  at the quitter by tapping D-pad right on the post-match menu screen.
  Chat only registers on menus, and only once the post-match screen has
  rendered — hence the ``warmup_frames`` delay. Uses ``next_gamestate``
  (not ``step``) so menu_helper doesn't override the D-pad with its own
  START/A spam while we're taunting. Total runtime: warmup +
  taps*(tap_hold+tap_gap) frames ≈ 1.5s with the default knobs."""
  controller = dolphin.controllers[port]

  def advance():
    controller.flush()
    try:
      dolphin.next_gamestate()
    except TimeoutError:
      pass

  for _ in range(warmup_frames):
    controller.release_all()
    advance()

  for _ in range(taps):
    for _ in range(tap_hold):
      controller.release_all()
      controller.press_button(melee.Button.BUTTON_D_RIGHT)
      advance()
    for _ in range(tap_gap):
      controller.release_all()
      advance()

  # Hold silent for ~1s so the chat message has time to render and read
  # before menu_helper starts spamming START toward the next match.
  for _ in range(trailing_idle_frames):
    controller.release_all()
    advance()

  controller.release_all()
  controller.flush()


def _graceful_exit(dolphin, port, *, taunt: bool,
                   menu_wait_frames=300, settle_frames=10,
                   taps=2, tap_hold=5, tap_gap=15,
                   trailing_idle_frames=180):
  """Clean shutdown sequence for any disconnect / stop exit path:

  1. Advance up to ``menu_wait_frames`` (~5s) until the game reaches a
     menu state. Tolerates TimeoutError — on a mid-match peer disconnect
     SLP frames may have stopped entirely, in which case we just hold
     the loop and keep trying until frames resume (Dolphin clears its
     disconnect dialog) or the budget runs out.
  2. If we reach a menu AND ``taunt`` is True, press D-pad right ``taps``
     times to fire Slippi's in-game chat message at the opponent. Chat
     only registers on menus — that's why we wait in step 1 instead of
     just firing blindly.
  3. Idle ``trailing_idle_frames`` (~3s) so the chat message reads on
     the opponent's screen before Dolphin closes.

  Replaces the old ``_hold_lra_start``-on-exit pattern, which could
  crash when ``dolphin.step()`` timed out during a stall. LRA+Start
  wasn't buying us anything on exit paths anyway — the peer is gone or
  ignoring our inputs, so the reset combo is wasted motion."""
  controller = dolphin.controllers[port]

  def advance():
    controller.flush()
    try:
      return dolphin.next_gamestate()
    except TimeoutError:
      return None

  on_menu = False
  for _ in range(menu_wait_frames):
    controller.release_all()
    gs = advance()
    if gs is not None and is_menu_state(gs):
      on_menu = True
      break

  if on_menu:
    # Let the menu render a few frames so the first tap isn't eaten by
    # the transition animation.
    for _ in range(settle_frames):
      controller.release_all()
      advance()

  if taunt and on_menu:
    for _ in range(taps):
      for _ in range(tap_hold):
        controller.release_all()
        controller.press_button(melee.Button.BUTTON_D_RIGHT)
        advance()
      for _ in range(tap_gap):
        controller.release_all()
        advance()

  for _ in range(trailing_idle_frames):
    controller.release_all()
    advance()

  controller.release_all()
  controller.flush()


def _hold_start_lra(dolphin, port, frames):
  """Drive Start+L+R+A on the AI's controller to quit an in-progress
  Melee match from the pause menu. Start is pressed first and held so
  the pause menu actually opens; the shoulders join a few frames later
  so Melee doesn't treat the combo as a console reset (L+R+A+Start).

  See the comment block on ``LRA_PRE_HOLD_FRAMES`` for the timing and
  analog-vs-digital shoulder rationale."""
  controller = dolphin.controllers[port]
  add_start = 0
  add_l = LRA_PRE_HOLD_FRAMES
  add_r = LRA_PRE_HOLD_FRAMES + LRA_STAGGER_FRAMES
  add_a = LRA_PRE_HOLD_FRAMES + 2 * LRA_STAGGER_FRAMES
  for frame in range(frames):
    controller.release_all()
    if frame >= add_start:
      controller.press_button(melee.Button.BUTTON_START)
    if frame >= add_l:
      controller.press_shoulder(melee.Button.BUTTON_L, 1.0)
      controller.press_button(melee.Button.BUTTON_L)
    if frame >= add_r:
      controller.press_shoulder(melee.Button.BUTTON_R, 1.0)
      controller.press_button(melee.Button.BUTTON_R)
    if frame >= add_a:
      controller.press_button(melee.Button.BUTTON_A)
    controller.flush()
    dolphin.step()
  controller.release_all()
  controller.flush()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
