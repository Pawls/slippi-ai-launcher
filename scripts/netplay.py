"""Test a trained model."""

import collections
import ctypes
import gc
import json
import os
import sys
import time
import unicodedata
import logging
from ctypes import wintypes

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
SERIES_STATE_PATH = flags.DEFINE_string(
    'series_state_path', None,
    'Optional path to a JSON handshake file the launcher rewrites when '
    'the challenger queue changes. The subprocess polls it on menu '
    'frames to learn whether it is in a Bo5 contested-set mode and '
    'should fire "one more" between games or chat-and-exit on a '
    'series-decider. Absent / unreadable file means the feature is '
    'disabled and no Bo5 chat is fired.')
NEXT_MATCH_STATE_PATH = flags.DEFINE_string(
    'next_match_state_path', None,
    'Optional path to a JSON handshake file the launcher writes when a '
    'queued challenger is promoted into this already-running subprocess. '
    'Only consumed when --persist_dolphin=True; the subprocess waits on '
    'this file between matches and swaps the bot character/agent to '
    'match the next challenger before re-entering matchmaking.')
PERSIST_DOLPHIN = flags.DEFINE_boolean(
    'persist_dolphin', False,
    'When True, keep this subprocess alive across queued challengers: '
    'after each match emit [MATCH_ENDED], wait for a new next-match '
    'handshake, swap the bot character/agent in-place, and play another '
    'match. Bounded by $MAX_MATCHES_PER_DOLPHIN (default 15) to '
    'mitigate libmelee memory growth.')

# Cap on matches per Dolphin before we force a clean subprocess/Dolphin
# restart. Primarily to mitigate libmelee's known memory growth. Exposed
# as an env override so testing can set a small N (e.g. 2) to exercise
# the reset path without actually playing 15 games.
MAX_MATCHES_PER_DOLPHIN = int(os.environ.get("MAX_MATCHES_PER_DOLPHIN", "15"))

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

# How long we'll sit on menu frames after a match without seeing
# IN_GAME before giving up and freeing the bot for the next challenger.
# Was 60s; reduced to 20s on 2026-04-27 because Slippi keeps the
# opponent slot reporting CONTROLLER_HUMAN with cached character data
# for the full 60s after a peer long-Z disconnects, defeating the 3s
# CSS+UNPLUGGED grace. 20s is well above any plausible rematch
# ready-up latency (humans are typically <10s) so this only fires when
# the peer is genuinely gone.
POST_MATCH_MAX_MENU_SECS = 20.0

# Hard timeout: once we've seen the opponent on the Slippi CSS, if we
# never reach IN_GAME within this window, force-cancel the search.
# Covers the case where the peer long-Z-disconnects mid-CSS and Slippi
# transitions the bot back into "Searching for <code>" without the
# opponent slot ever cleanly going UNPLUGGED for the 3s grace —
# libmelee's menu_helper keeps spinning the cursor and the existing
# detections all fail to fire. 25s is well over the ~2s a normal
# ready-up takes once both players reach CSS, so we don't false-trip
# during legitimate slow starts.
STUCK_ON_CSS_AFTER_OPP_SECS = 25.0

# Diagnostic emission cadence for the [CSS_WAIT] sentinel — once per
# second of wall-clock while saw_opponent_on_css and not saw_in_game.
# Lets us capture the actual menu_state / submenu / opp status during
# the buggy search-loop state so we can write a precise detection
# later. Cheap: one print() at 1Hz on menu frames only.
CSS_WAIT_DEBUG_INTERVAL_SECS = 1.0

FLAGS = flags.FLAGS


def main(_):
  # Single-agent, batch=1 real-time inference is dominated by kernel-launch
  # overhead on GPU and has caused buggy agent behavior; force CPU.
  eval_lib.disable_gpus()

  port = 1

  current_agent_path = AGENT.value['path']
  current_agent_name = str(AGENT.value.get('name', '') or '')
  agent_state = saving.load_state_from_disk(current_agent_path)

  # Auto-compute console_delay from the model's trained delay, like twitchbot.py.
  # Leave 1 frame of headroom for async inference.
  # If --dolphin.online_delay is explicitly set, use that instead.
  # In persist-dolphin mode the subprocess can outlive a character/style
  # swap, but the console's online_delay is baked into Dolphin at launch
  # and cannot change without restarting Dolphin — so we size it off the
  # initial agent and accept that a much-higher-delay replacement agent
  # will experience extra input lag. That's suboptimal but not broken.
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

  # ── Inference health stats (declared at function scope) ──
  # The per-match counters are reset at the top of every outer-loop
  # iteration; declaring them here lets `_emit_inference_health` close
  # over them with `nonlocal` and remain a stable function reference
  # the finally block can safely call.
  HEALTH_EMIT_INTERVAL_FRAMES = 120
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

  # Sentinel/stall poll state (declared at function scope so the
  # nested checker can mutate `last_sentinel_poll` via nonlocal). All
  # values are reset at the top of every outer-loop iteration.
  sentinel_path = END_MATCH_SENTINEL.value
  sentinel_poll_interval = 1.0
  last_sentinel_poll = time.monotonic()
  last_fresh_gamestate = time.monotonic()
  saw_in_game = False

  def _poll_sentinel_and_stall():
    """Shared exit-path check: returns a truthy tuple (reason,
    ended_cleanly) if we should break the per-match loop, else None.
    Uses wall-clock so it works both during normal play and during
    peer-disconnect stalls when frames stop arriving."""
    nonlocal last_sentinel_poll
    now = time.monotonic()
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

  try:
    # First frame — can be any menu state (CSS, matchmaking screen,
    # postgame, etc.). We do NOT announce the match has started here;
    # Dolphin returns menu frames long before IN_GAME is reached, and an
    # early announcement was disarming the launcher's no-connect watchdog.
    gamestate = dolphin.step()

    with open(DOLPHIN.value['user_json_path']) as f:
      user_json = json.load(f)
    display_name = user_json['displayName']

    # Resolve ports BEFORE the warm-up agent.step. The Parser inside
    # eval_lib.Agent indexes self.ports each frame and crashes on
    # None when build_agent's opponent_port=None default leaks
    # through; and even if it survived, the Parser locks ports on
    # gamestate.frame == -123 (first IN_GAME frame), so a stale
    # ``agent.players`` causes the bot to read the wrong "self" /
    # "opponent" features for the entire game. The first IN_GAME
    # frame inside the per-match loop re-resolves with real player
    # data (matchmaking lands a peer after this initial bootstrap),
    # but we still need a non-None placeholder here.
    name_to_port = {
        unicodedata.normalize('NFKC', p.displayName): pp
        for pp, p in gamestate.players.items()
    }
    actual_port = name_to_port.get(display_name)
    if actual_port is None:
      actual_port = port
    ports_now = list(gamestate.players) or [port]
    if actual_port in ports_now:
      ports_now.remove(actual_port)
    opponent_port = ports_now[0] if ports_now else (2 if actual_port == 1 else 1)
    agent.players = (actual_port, opponent_port)

    # Main loop bootstrap — start the agent + warm-up step ONCE per
    # subprocess. Subsequent matches in persist-dolphin mode continue
    # running on the same agent until a swap rebuilds it.
    agent.start()
    agent.step(gamestate)
    agent_started = True

    # Persist-mode bookkeeping — outside the per-match loop.
    next_match_state_path_val = NEXT_MATCH_STATE_PATH.value
    last_handshake_generation = -1
    total_match_count = 0
    # Set by terminal exit paths so the outer loop doesn't try to wait
    # for another handshake once the user has hit Stop, etc.
    pending_exit_sequence: str | None = None
    # prev_was_in_game is also used by the deferred sentinel-exit
    # outside the inner loop, so it lives at function scope.
    prev_was_in_game = False

    # Disable Python's cyclic GC for the duration of every per-match
    # frame loop. Reference counting still cleans up per-frame
    # allocations (tuples, strings, etc.); we only lose collection of
    # reference cycles, which the hot path effectively doesn't create.
    # This eliminates multi-ms GC sweep pauses that were the dominant
    # remaining cause of step-time spikes after the earlier
    # launcher-side fixes — a standard real-time-Python technique.
    # Re-enabled in `finally` so anything outside this loop is
    # unaffected. Stays disabled across the inter-match wait too —
    # that wait does no inference but also creates no cycles, so
    # there's nothing to lose by leaving GC off.
    gc.disable()

    while total_match_count < MAX_MATCHES_PER_DOLPHIN:
      # Initial port resolution from whatever gamestate we currently
      # hold (initial dolphin.step for match 0; the wait-loop's last
      # frame for match 1+). Often stale on match 1+; the first
      # IN_GAME frame inside the inner loop re-resolves with real
      # player data. MUST run BEFORE the agent warm-up step below —
      # eval_lib.Agent's Parser indexes self.ports each frame and a
      # None there crashes get_game with "TypeError: '<' not
      # supported between instances of 'NoneType' and 'int'".
      name_to_port = {
          unicodedata.normalize('NFKC', p.displayName): pp
          for pp, p in gamestate.players.items()
      }
      actual_port = name_to_port.get(display_name)
      if actual_port is None:
        actual_port = port
      ports = list(gamestate.players) or [port]
      if actual_port in ports:
        ports.remove(actual_port)
      opponent_port = ports[0] if ports else (2 if actual_port == 1 else 1)
      agent.players = (actual_port, opponent_port)

      if not agent_started:
        agent.start()
        agent.step(gamestate)
        agent_started = True

      # ── Per-match state init ────────────────────────────────────
      # All variables that should NOT carry across matches are reset
      # here. num_frames also resets so [INFER_SPIKE] /
      # [LIVE_EVENT_FRAME] / [INFER_HEALTH] frame counts are per-match
      # — matches the single-match semantics the launcher expects.
      last_sentinel_poll = time.monotonic()
      last_fresh_gamestate = time.monotonic()
      num_frames = 1
      menu_frames = 0

      # Track the last in-game stock/percent snapshot so we can
      # determine a winner when the menu transitions out of IN_GAME.
      # libmelee's gamestate only exposes live stocks, so we have to
      # cache them ourselves.
      last_in_game = {}  # {port: (stock, percent)}
      saw_in_game = False
      # Tracks whether the opponent's CSS slot has ever shown
      # plugged-in this match. Distinguishes "peer hasn't arrived yet"
      # (slot unplugged from the start, keep waiting) from "peer was
      # here and disconnected" (long-Z press, dropped connection).
      # Lets the peer-left detection below fire pre-match so a
      # Z-disconnect on the CSS bails in OPPONENT_GONE_GRACE_SECS
      # instead of the launcher's 90s no-connect deadline.
      saw_opponent_on_css = False
      # Wall-clock timestamp at which saw_opponent_on_css flipped True.
      # Used by the STUCK_ON_CSS_AFTER_OPP_SECS hard-timeout safety net
      # below to bail when libmelee's menu_helper auto-search masks the
      # other peer-left detections.
      saw_opponent_on_css_at = None
      # In persist-dolphin mode the opp slot can carry CACHED data from
      # the previous match (CONTROLLER_HUMAN with the prior opponent's
      # character) when match N+1 begins. Without this guard,
      # saw_opponent_on_css would flip True on the first frame of a new
      # match against the stale slot, then 25s later stuck_on_css would
      # falsely fire even though no real opponent ever connected.
      # Require the slot to have been UNPLUGGED at some point this
      # match before we believe a "plugged" reading. New peer arrivals
      # legitimately transition unplugged → plugged; cached carry-over
      # never goes through that transition.
      saw_opp_slot_unplugged = False
      # Last wall-clock time we emitted a [CSS_WAIT] diagnostic line.
      last_css_wait_emit = 0.0
      # True once we've fired the search-cancel quit sequence so we
      # don't re-fire it every menu frame while the bot is still
      # transitioning out of CSS.
      stuck_quit_armed = False
      saw_postgame = False  # separates natural game-end from mid-match disconnect
      match_started_announced = False
      game_result_reported = False
      rematch_boundary = False
      opp_unplugged_since = None
      # Counter of consecutive frames where the opponent slot has
      # appeared plugged-in. Used to filter out single-frame flicker
      # from libmelee's CSS reload that would otherwise reset the
      # opp_unplugged_since timer and prevent the 3s grace from ever
      # accumulating during the search-again loop.
      opp_present_frames = 0
      post_match_menu_since = None
      # For rage-quit detection: track whether the previous frame was
      # IN_GAME so we can catch the exact transition back to a menu. If
      # the opponent ended the match abruptly (reset combo / exit to
      # CSS) but stayed connected, we want to tap out the in-game chat
      # message once, then let menu_helper drive the rematch as usual.
      prev_was_in_game = False
      taunted_this_game = False
      # Opponent's last in-game action-state; only emit a
      # [LIVE_EVENT_FRAME] sentinel on transition. Reset per match so
      # the first frame of a new match always emits.
      last_opp_action = None
      start_time = time.monotonic()

      # ── Bo5 session state ──────────────────────────────────────────
      # Chronological per-game outcomes inside this challenger's
      # session — fed by every _emit_game_result call. Reset per match
      # in persist mode so each new challenger gets a fresh Bo5 tally.
      session_game_results: list[str] = []
      one_more_fired_for_rematch = False
      series_state_path_val = SERIES_STATE_PATH.value
      series_end_outcome: str | None = None
      forfeit_heckle_pending = False

      # ── Inference health stats reset ─────────────────────────────
      # Frame budget: Melee runs at 60Hz (16.67ms/frame). 14ms keeps a
      # ~2.5ms safety margin for controller flush + Dolphin IO;
      # anything past that is at real risk of producing a stale input.
      # Per-match reset preserves HEAD's "cumulative since match start"
      # semantics for steps/mean_ms.
      infer_step_count = 0
      infer_step_total_ms = 0.0
      infer_step_max_ms = 0.0
      infer_overruns = 0
      last_health_emit_frame = 0

      # Outcome of this match, populated at any break-site below.
      outcome_reason = 'completed'
      ended_cleanly = True

      while True:
        # ── HOT PATH ── this inner loop body runs every frame during
        # gameplay. The block below is intentionally identical to
        # HEAD's per-frame logic — the only persist-mode change at
        # break-sites is to set ``outcome_reason`` / ``ended_cleanly``
        # and break, deferring _sentinel_exit / _graceful_exit /
        # _dolphin_quit_sequence + agent.stop() to AFTER the inner
        # loop so we don't run them between matches when persist is on.
        try:
          gamestate = dolphin.next_gamestate()
        except TimeoutError:
          exit_reason = _poll_sentinel_and_stall()
          if exit_reason is not None:
            reason, ec = exit_reason
            if saw_in_game and not game_result_reported and last_in_game:
              # Stall means peer stopped sending frames mid-game —
              # count as a forfeit for the human. Sentinel (user Stop)
              # lets the natural stock/percent compute decide, since
              # it isn't a forfeit.
              forfeit = reason == 'stall'
              _emit_game_result(
                  actual_port, opponent_port, last_in_game,
                  ended_cleanly=ec,
                  force_winner='ai' if forfeit else None,
              )
              game_result_reported = True
            outcome_reason = reason
            ended_cleanly = ec
            break
          continue

        now = time.monotonic()
        last_fresh_gamestate = now
        num_frames += 1

        menu = gamestate.menu_state
        if is_game_state(gamestate):
          # Back in-game — clear any menu-side disconnect streaks, and
          # on a rematch boundary flip per-game reporting state so the
          # next POSTGAME_SCORES emits its own [GAME_RESULT] instead of
          # short-circuiting on the previous game's.
          opp_unplugged_since = None
          post_match_menu_since = None
          if rematch_boundary:
            last_in_game.clear()
            game_result_reported = False
            saw_postgame = False
            taunted_this_game = False
            rematch_boundary = False
            # A new game is starting — next time we're between games
            # we'll re-evaluate whether to fire "one more" for it.
            one_more_fired_for_rematch = False

          prev_was_in_game = True
          # Re-resolve the agent ports BEFORE the first agent.step in
          # this main-loop iteration. Agent.step rebuilds its Parser on
          # gamestate.frame == -123 using a snapshot of self.players at
          # that moment — if we step before re-resolving, the parser
          # gets locked into stale ports for the entire game and the
          # neural network sees the wrong "self" / "opponent" features.
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
          # Time the agent step locally — perf_counter is sub-µs and
          # the arithmetic below is a handful of int/float ops, so
          # this adds negligible cost to the hot path. agent.step()
          # blocks on the async-inference output queue, so this
          # directly captures the frame-budget pressure the user
          # cares about.
          _t0 = time.perf_counter()
          agent.step(gamestate)
          _step_ms = (time.perf_counter() - _t0) * 1000.0
          infer_step_count += 1
          infer_step_total_ms += _step_ms
          if _step_ms > infer_step_max_ms:
            infer_step_max_ms = _step_ms
          if _step_ms > INFER_OVERRUN_THRESHOLD_MS:
            infer_overruns += 1
            print(
                f"[INFER_SPIKE] frame={num_frames} step_ms={_step_ms:.2f}",
                flush=True)
          if num_frames - last_health_emit_frame >= HEALTH_EMIT_INTERVAL_FRAMES:
            last_health_emit_frame = num_frames
            _emit_inference_health()
          # Emit a [LIVE_EVENT_FRAME] sentinel only when the
          # opponent's action-state changes. The launcher parses these
          # from stdout and runs the detectors there — keeping
          # detection out of this subprocess avoids GIL contention
          # with async TF inference, which has only 1 frame of
          # headroom and goes out of sync if a second Python-heavy
          # thread runs alongside it.
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
            pass
          if not match_started_announced:
            print("[MATCH_STARTED]", flush=True)
            match_started_announced = True
          # Cache last in-game stocks/percent so [GAME_RESULT] can
          # determine the winner without libmelee's help. Skip the
          # full rebuild every frame — stocks change only on KO
          # (≤12 events per game), and percent only matters as a
          # time-out tiebreaker, so refresh at ~1Hz suffices.
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
          # POSTGAME_SCORES we see, then let menu_helper spam through
          # to the next rematch — unless the peer has actually left.

          # First menu frame after IN_GAME: check for "rage-quit
          # stayed connected".
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
              rematch_boundary = True
              if not game_result_reported:
                rq_winner = _emit_game_result(
                    actual_port, opponent_port, last_in_game,
                    ended_cleanly=False,
                    force_winner='ai',
                )
                game_result_reported = True
                session_game_results.append(rq_winner)
              taunted_this_game = True
              # Defer chat to the next CSS frame — chat inputs are
              # only registered on CSS.
              state = _read_series_state(series_state_path_val)
              if state.get('bo5_active'):
                _, _, decided = _window_tally_local(session_game_results)
                if decided is not None:
                  series_end_outcome = 'bot_won_forfeit'
                else:
                  forfeit_heckle_pending = True
              else:
                forfeit_heckle_pending = True
          prev_was_in_game = False

          if menu == melee.Menu.POSTGAME_SCORES:
            saw_postgame = True
            rematch_boundary = True
            if saw_in_game and not game_result_reported and last_in_game:
              pg_winner = _emit_game_result(
                  actual_port, opponent_port, last_in_game,
                  ended_cleanly=True,
              )
              game_result_reported = True
              session_game_results.append(pg_winner)
              state = _read_series_state(series_state_path_val)
              if state.get('bo5_active'):
                _, _, decided = _window_tally_local(session_game_results)
                if decided == 'ai':
                  series_end_outcome = 'bot_won'
                elif decided == 'human':
                  series_end_outcome = 'bot_lost'

          # Track opponent presence on CSS so the peer-left detection
          # below can fire pre-IN_GAME — distinguishes "peer hasn't
          # arrived yet" from "peer was here and bailed". Without this
          # flag, a long-Z disconnect during character select would
          # only be caught by the launcher's 90s no-connect watchdog.
          #
          # The saw_opp_slot_unplugged gate prevents persist-mode
          # match N+1 from inheriting match N's cached opp slot as
          # "opponent is here." Real arrivals always pass through an
          # UNPLUGGED state during the connection handshake; cached
          # carry-over does not. See 2026-04-27 [CSS_WAIT] capture.
          if menu == melee.Menu.SLIPPI_ONLINE_CSS:
            opp = gamestate.players.get(opponent_port)
            opp_plugged = (
                opp is not None
                and opp.controller_status !=
                    melee.ControllerStatus.CONTROLLER_UNPLUGGED)
            if not opp_plugged:
              saw_opp_slot_unplugged = True
            elif (saw_opp_slot_unplugged and not saw_opponent_on_css):
              saw_opponent_on_css = True
              saw_opponent_on_css_at = now

          # Diagnostic: when stuck on a menu after the opponent was
          # here, emit one [CSS_WAIT] line per second so we can see
          # exactly what state the bot is in. Fires both pre-match
          # (search-loop trap) and post-match (error-screen / stuck-
          # rematch). Cheap: one print per second on menu frames only,
          # no impact on the in-game hot path.
          if (saw_opponent_on_css
              and now - last_css_wait_emit >= CSS_WAIT_DEBUG_INTERVAL_SECS):
            last_css_wait_emit = now
            opp_dbg = gamestate.players.get(opponent_port)
            opp_status_name = (
                opp_dbg.controller_status.name
                if opp_dbg is not None else 'NO_SLOT')
            opp_char_name = (
                opp_dbg.character.name
                if opp_dbg is not None else 'NO_SLOT')
            sub = (gamestate.submenu.name
                   if hasattr(gamestate.submenu, 'name')
                   else str(gamestate.submenu))
            since = (now - saw_opponent_on_css_at
                     if saw_opponent_on_css_at else 0.0)
            print(
                f"[CSS_WAIT] menu={menu.name} submenu={sub} "
                f"opp_status={opp_status_name} opp_char={opp_char_name} "
                f"saw_in_game={saw_in_game} since={since:.1f}s",
                flush=True)

          # Peer-left detection. Triggers when EITHER a match has
          # started (saw_in_game) OR we've seen the opponent plugged
          # in on CSS at some point (saw_opponent_on_css). The
          # idle_timeout sub-check stays gated on saw_in_game alone —
          # it's specifically the "human went AFK on CSS after a
          # game" failsafe and shouldn't pre-empt the launcher's 90s
          # no-connect watchdog when no peer ever showed up.
          if saw_in_game or saw_opponent_on_css:
            if saw_in_game and post_match_menu_since is None:
              post_match_menu_since = now

            # --- Peer-left detection ---
            # PRESS_START is the title screen: only reached if they
            # fully backed out. CSS with an UNPLUGGED opponent slot
            # is the screenshotted state. Debounce the UNPLUGGED
            # check — right after postgame the slot can briefly
            # flicker while the CSS reloads.
            #
            # NAME_ENTRY_SUBMENU and MAIN_MENU are the "Slippi auto-
            # bumped us off CSS so libmelee's menu_helper is about to
            # dial the same connect code again" cases. When a peer
            # long-Z disconnects on CSS, Slippi's client doesn't sit
            # the bot on CSS with the opponent slot unplugged — it
            # navigates the bot back to the connect-code entry screen
            # (NAME_ENTRY_SUBMENU under SLIPPI_ONLINE_CSS) or all the
            # way to MAIN_MENU. libmelee's menu_helper_simple then
            # auto-re-enters the code (see melee.menuhelper line 64-99
            # for the routing). We have to catch this on the FIRST
            # frame of the transition; otherwise menu_helper races us
            # and starts a fresh search before our exit can run.
            # Gated on saw_opponent_on_css + not saw_in_game so we
            # don't false-positive on the initial pre-match navigation
            # (subprocess starts on MAIN_MENU and walks through
            # NAME_ENTRY before ever reaching CSS).
            disconnect_reason = None
            if menu == melee.Menu.PRESS_START:
              disconnect_reason = 'peer_left_title'
            elif (saw_opponent_on_css
                  and (menu == melee.Menu.MAIN_MENU
                       or gamestate.submenu ==
                          melee.SubMenu.NAME_ENTRY_SUBMENU)):
              # Originally gated on `not saw_in_game` to avoid false
              # positives during the initial pre-match navigation
              # through MAIN_MENU/NAME_ENTRY. That gate was wrong
              # post-match: 2026-04-27 [CSS_WAIT] capture confirmed
              # Slippi briefly bumps libmelee to NAME_ENTRY_SUBMENU
              # ~33s after a peer long-Z disconnects on the rematch
              # CSS, and that's the only state transition that
              # actually fires because Slippi keeps the opp slot
              # reporting CONTROLLER_HUMAN with cached character data
              # the whole time. The pre-match false-positive risk is
              # already neutralized by `saw_opponent_on_css` — that
              # flag only flips True once we observe the opp's slot
              # plugged in on CSS, which doesn't happen during the
              # bootstrap MAIN_MENU → NAME_ENTRY → CSS walk.
              disconnect_reason = 'peer_left_lobby'
            else:
              # Sticky-grace unplugged detection across ANY non-game
              # menu state. Was previously gated on
              # menu == SLIPPI_ONLINE_CSS, which missed the post-match
              # error screen ("Failed to create mm client") that
              # Slippi shows after a mid-match peer disconnect — the
              # 2026-04-27 test sat there for 60s before idle_timeout
              # caught it.
              #
              # opp_present_frames tracks consecutive opp-present
              # frames separately from menu state, so a menu
              # transition with opp still gone doesn't falsely reset
              # the grace timer (single-frame flicker still buffered
              # by the 10-frame debounce).
              opp = gamestate.players.get(opponent_port)
              opp_unplugged = (
                  opp is None
                  or opp.controller_status ==
                      melee.ControllerStatus.CONTROLLER_UNPLUGGED
              )
              if opp_unplugged:
                opp_present_frames = 0
                if opp_unplugged_since is None:
                  opp_unplugged_since = now
                elif now - opp_unplugged_since >= OPPONENT_GONE_GRACE_SECS:
                  disconnect_reason = 'peer_left_css'
              else:
                opp_present_frames += 1
                # Require ~10 frames (~1/6 s) of consecutive presence
                # before believing the opp is genuinely back. Tunable.
                if opp_present_frames >= 10:
                  opp_unplugged_since = None

            # Hard timeout: if we've seen the opponent on CSS but
            # haven't reached IN_GAME after STUCK_ON_CSS_AFTER_OPP_SECS,
            # libmelee's menu_helper auto-search has trapped us in a
            # cancel-prompt loop. Force the quit path. Catches the
            # screenshot's "Searching for PAWL#723" stuck state when
            # the more precise detections all miss.
            if (disconnect_reason is None
                and saw_opponent_on_css
                and not saw_in_game
                and saw_opponent_on_css_at is not None
                and now - saw_opponent_on_css_at >= STUCK_ON_CSS_AFTER_OPP_SECS):
              disconnect_reason = 'stuck_on_css'

            if (saw_in_game
                and disconnect_reason is None
                and post_match_menu_since is not None
                and now - post_match_menu_since >= POST_MATCH_MAX_MENU_SECS):
              disconnect_reason = 'idle_timeout'

            if disconnect_reason is not None:
              logging.info('Match ended: %s.', disconnect_reason)
              if not game_result_reported and last_in_game:
                force = None if saw_postgame else 'ai'
                _emit_game_result(
                    actual_port, opponent_port, last_in_game,
                    ended_cleanly=saw_postgame,
                    force_winner=force,
                )
                game_result_reported = True
              outcome_reason = 'disconnected'
              ended_cleanly = saw_postgame
              break

          # ── CSS-gated chat handlers ─────────────────────────────────
          if menu == melee.Menu.SLIPPI_ONLINE_CSS:
            if series_end_outcome is not None:
              if series_end_outcome == 'bot_won':
                _send_chat_sequence(dolphin, port, *_CHAT_SORRY)
              elif series_end_outcome == 'bot_lost':
                _send_chat_sequence(dolphin, port, *_CHAT_TOO_GOOD)
              elif series_end_outcome == 'bot_won_forfeit':
                _send_chat_sequence(dolphin, port, *_CHAT_LOL)
              _send_chat_sequence(dolphin, port, *_CHAT_GGS)
              outcome_reason = 'end_of_set'
              ended_cleanly = True
              break

            if forfeit_heckle_pending:
              _send_chat_sequence(dolphin, port, *_CHAT_LOL)
              forfeit_heckle_pending = False

          # Between-games "one more" announce.
          if (rematch_boundary and menu == melee.Menu.SLIPPI_ONLINE_CSS
              and not one_more_fired_for_rematch
              and session_game_results
              and _next_game_is_possible_decider(session_game_results)):
            state = _read_series_state(series_state_path_val)
            if state.get('bo5_active'):
              _send_chat_sequence(dolphin, port, *_CHAT_ONE_MORE)
              one_more_fired_for_rematch = True

          # Drive libmelee's menu helper for the rematch flow.
          for i, (controller, plr) in enumerate(dolphin._menuing_controllers):
            dolphin.menu_helper.menu_helper_simple(
                gamestate, controller,
                stage_selected=dolphin.stage,
                connect_code=dolphin._connect_code,
                autostart=dolphin._autostart and i == 0 and menu_frames > 30,
                swag=False,
                costume=i,
                **plr.menuing_kwargs(),
            )
          menu_frames += 1

        exit_reason = _poll_sentinel_and_stall()
        if exit_reason is not None:
          reason, ec = exit_reason
          logging.info('End-match %s — closing gracefully.', reason)
          if saw_in_game and not game_result_reported and last_in_game:
            forfeit = reason == 'stall'
            _emit_game_result(
                actual_port, opponent_port, last_in_game,
                ended_cleanly=ec,
                force_winner='ai' if forfeit else None,
            )
            game_result_reported = True
          outcome_reason = reason
          ended_cleanly = ec
          break

        if RUNTIME.value is not None and time.monotonic() - start_time >= RUNTIME.value:
          outcome_reason = 'runtime'
          ended_cleanly = True
          break

      # ── Per-match loop exited ─────────────────────────────────────
      # Decide actual outcome reason (promoting runtime / sentinel /
      # reset_threshold to terminal), emit [MATCH_ENDED], then either
      # continue the outer loop (wait for next challenger) or fall
      # through to terminal teardown.
      total_match_count += 1
      at_reset_threshold = (
          total_match_count >= MAX_MATCHES_PER_DOLPHIN
          and PERSIST_DOLPHIN.value
      )
      terminal_this_iteration = (
          outcome_reason in ('sentinel', 'runtime')
          or not PERSIST_DOLPHIN.value
          or at_reset_threshold
      )

      # Choose the reason tag we emit. The launcher's stdout tailer
      # keys its "exit fresh next time" path off reason=reset_threshold.
      emit_reason = 'reset_threshold' if at_reset_threshold else outcome_reason
      ended_str = 'clean' if ended_cleanly else 'disconnect'
      print(f"[MATCH_ENDED] reason={emit_reason} ended={ended_str}", flush=True)

      if terminal_this_iteration:
        # Terminal exit: arrange the appropriate Dolphin-closing
        # sequence to run AFTER the outer loop unwinds (kept outside
        # the per-match loop so persist-mode boundaries don't quit
        # Dolphin between matches).
        if outcome_reason == 'sentinel':
          pending_exit_sequence = 'sentinel'
        elif outcome_reason == 'end_of_set':
          pending_exit_sequence = 'quit_sequence'
        else:
          pending_exit_sequence = 'graceful'
        break

      # ── Persist-dolphin path: wait for the next-match handshake ──
      # Indefinite wait — exits only on sentinel (Stop), agent
      # rebuild failure, or threshold/exception above. End-match
      # sentinel polled inside _wait_for_next_match.
      #
      # Run the Z-press cleanup BEFORE waiting on disconnect outcomes
      # so the bot cancels any lingering search/error state ("Failed
      # to create mm client" + Z-to-clear hint, libmelee re-search
      # loop, etc.). Without this the bot sits on the error screen
      # through the wait and the next match starts from a degraded
      # connection state that cached opp slot data falsely satisfies
      # saw_opponent_on_css and trips stuck_on_css.
      if outcome_reason in (
          'disconnected', 'stall', 'peer_left_lobby', 'stuck_on_css'):
        try:
          _press_z_disconnect(dolphin, port)
        except Exception:
          # Defensive: a Z-press failure shouldn't prevent waiting for
          # the next match. Log and continue.
          logging.exception(
              '[persist] _press_z_disconnect raised — '
              'continuing to handshake wait')

      swap = _wait_for_next_match(
          dolphin, port,
          sentinel_path=sentinel_path,
          next_match_state_path=next_match_state_path_val,
          last_generation=last_handshake_generation,
      )
      if swap is None:
        # Sentinel during idle wait — user hit Stop.
        pending_exit_sequence = 'sentinel'
        break

      last_handshake_generation = int(
          swap.get('generation', last_handshake_generation + 1))
      new_char_str = str(swap.get('character') or '').strip().upper()
      new_agent_path = str(swap.get('agent_path') or '').strip()
      new_agent_name = str(swap.get('agent_name') or '').strip()

      swap_needs_agent_rebuild = (
          (new_agent_path and new_agent_path != current_agent_path)
          or (new_agent_name != current_agent_name)
      )
      if swap_needs_agent_rebuild:
        effective_path = new_agent_path or current_agent_path
        logging.info(
            '[persist] swapping agent → path=%s name=%r',
            effective_path, new_agent_name)
        try:
          agent.stop()
        except Exception:
          pass
        try:
          new_state = saving.load_state_from_disk(effective_path)
        except Exception:
          logging.exception(
              '[persist] failed to load new agent %s — exiting subprocess',
              effective_path)
          pending_exit_sequence = 'graceful'
          break
        eval_lib.update_character(player, new_state['config'])
        agent_kwargs = dict(AGENT.value)
        agent_kwargs['path'] = effective_path
        agent_kwargs['name'] = new_agent_name
        agent = eval_lib.build_agent(
            controller=dolphin.controllers[port],
            opponent_port=None,
            console_delay=console_delay,
            run_on_cpu=True,
            state=new_state,
            **agent_kwargs,
        )
        agent_started = False  # outer loop calls .start() + warm-up
        current_agent_path = effective_path
        current_agent_name = new_agent_name
      if new_char_str:
        try:
          desired = melee.Character[new_char_str]
        except KeyError:
          logging.warning(
              '[persist] unknown character %r in handshake — keeping %s',
              new_char_str, player.character.name)
          desired = player.character
        if desired != player.character:
          logging.info(
              '[persist] character swap %s → %s',
              player.character.name, desired.name)
          player.character = desired

      # Outer loop continues: next iteration plays a new match.

    # ── Outer loop exited ─────────────────────────────────────────
    # Run the deferred Dolphin-closing sequence for the last match's
    # outcome. Kept outside the finally because the exit sequences
    # need to issue controller inputs and advance Dolphin frames —
    # work the finally's hard teardown does not do.
    if pending_exit_sequence == 'sentinel':
      _sentinel_exit(dolphin, port, was_in_game=prev_was_in_game)
    elif pending_exit_sequence == 'quit_sequence':
      _dolphin_quit_sequence(dolphin, port)
    elif pending_exit_sequence == 'graceful':
      _graceful_exit(dolphin, port)

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


def _emit_game_result(ai_port, human_port, last_in_game, ended_cleanly,
                      *, force_winner=None):
  """Print a [GAME_RESULT] sentinel the launcher parses to update the
  per-user W/L record. Winner = port with more stocks; on stock tie (time
  out) the lower % wins; a genuine double-KO reports 'draw'.

  ``ended_cleanly`` distinguishes a natural game end (POSTGAME_SCORES
  was reached) from a mid-match opponent disconnect. The launcher uses
  it to decide whether to fire a "you ran away" taunt or stay silent.

  ``force_winner`` overrides the stock/percent computation — used on
  rage-quit / disconnect paths where the human bailed mid-game and the
  caller wants to record the outcome as a forfeit rather than whatever
  state the last in-game frame happened to hold. Returns the recorded
  winner so callers can feed it into the Bo5 sliding-window tally.
  """
  ai_stock, ai_pct = last_in_game.get(ai_port, (0, 0.0))
  hu_stock, hu_pct = last_in_game.get(human_port, (0, 0.0))
  if force_winner in ('ai', 'human', 'draw'):
    winner = force_winner
  elif ai_stock > hu_stock:
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
  return winner


# ── In-game chat commands (Slippi Online CSS d-pad sequences) ──────────
#
# Slippi's in-game chat menu opens with any d-pad direction (the "wheel"
# shows four phrases keyed to the four directions) and selects on the
# next d-pad input. Full sequence protocol: tap direction-1 to open the
# wheel, tap direction-2 to pick. Inputs only register while on a menu
# state, so callers must wait for CSS / POSTGAME_SCORES before firing.
#
# Mapping follows the user-visible Slippi in-game chat defaults:
#   Up    → Up    = "ggs"
#   Up    → Left  = "one more"
#   Right → Right = "lol"
#   Right → Up    = "sorry"
#   Left  → Down  = "too good"
#   Down  → Up    = "gotta go"
_CHAT_GGS = (melee.Button.BUTTON_D_UP, melee.Button.BUTTON_D_UP)
_CHAT_ONE_MORE = (melee.Button.BUTTON_D_UP, melee.Button.BUTTON_D_LEFT)
_CHAT_LOL = (melee.Button.BUTTON_D_RIGHT, melee.Button.BUTTON_D_RIGHT)
_CHAT_SORRY = (melee.Button.BUTTON_D_RIGHT, melee.Button.BUTTON_D_UP)
_CHAT_TOO_GOOD = (melee.Button.BUTTON_D_LEFT, melee.Button.BUTTON_D_DOWN)
_CHAT_GOTTA_GO = (melee.Button.BUTTON_D_DOWN, melee.Button.BUTTON_D_UP)


def _send_chat_sequence(dolphin, port, dir1, dir2, *,
                        warmup_frames=15, tap_hold=5, tap_gap=10,
                        trailing_idle_frames=60):
  """Fire a two-direction Slippi chat sequence (tap dir1, tap dir2) on
  the agent's controller. Must be called while a menu state is active —
  chat inputs are eaten during IN_GAME. Callers should advance frames
  to a menu first (see ``_graceful_exit`` for the wait-for-menu pattern).

  Uses ``next_gamestate`` (not ``step``) so libmelee's menu_helper doesn't
  clobber the d-pad inputs with its own navigation spam while the chat
  is firing. Total runtime with defaults: warmup + 2*(tap_hold+tap_gap)
  frames ≈ 0.8s — fast enough that menu_helper's START spam can't kick
  in mid-sequence but long enough for the game to render the wheel.
  """
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

  for direction in (dir1, dir2):
    for _ in range(tap_hold):
      controller.release_all()
      controller.press_button(direction)
      advance()
    for _ in range(tap_gap):
      controller.release_all()
      advance()

  for _ in range(trailing_idle_frames):
    controller.release_all()
    advance()

  controller.release_all()
  controller.flush()


def _read_series_state(path):
  """Read the launcher's Bo5 handshake file. Returns an empty dict on
  any failure (no file, malformed JSON, missing keys) so the caller
  can treat it as "feature off". Called on a menu-frame cadence so the
  common case must be cheap — a single small file read is fine, even
  unthrottled."""
  if not path:
    return {}
  try:
    with open(path, 'r', encoding='utf-8') as f:
      data = json.load(f)
  except (FileNotFoundError, json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def _read_next_match_state(path):
  """Read the launcher's next-match handshake file. Same pattern as
  ``_read_series_state``: returns {} on any failure so the caller can
  treat missing/malformed as "no handshake yet". Only ever called
  between matches (NOT on the in-game frame loop), so I/O is fine."""
  if not path:
    return {}
  try:
    with open(path, 'r', encoding='utf-8') as f:
      data = json.load(f)
  except (FileNotFoundError, json.JSONDecodeError, OSError):
    return {}
  return data if isinstance(data, dict) else {}


def _wait_for_next_match(
    dolphin, port, *,
    sentinel_path, next_match_state_path, last_generation, poll_interval=0.5,
):
  """Between-matches idle loop. Blocks until one of:

  - A new next-match handshake arrives (``generation`` strictly greater
    than ``last_generation``). Returns the handshake dict.
  - ``end_match_sentinel`` is touched by the launcher (Stop button).
    Returns ``None``.

  While blocking, keeps Dolphin stepping on menu frames with
  ``autostart=False`` so the bot sits on the Slippi Online CSS entry
  screen without re-entering matchmaking with whatever character the
  prior match was using. Flips ``_autostart=True`` once a handshake is
  consumed so the caller's main loop immediately drives matchmaking for
  the new challenger.

  This function is intentionally invoked ONLY between matches — never
  during gameplay — so the file-stat polling here cannot delay
  inference. The 0.5s poll interval is generous: nothing on the bot's
  side cares about handshake latency on the millisecond scale.
  """
  # Pause matchmaking navigation while we wait. Character/agent swaps
  # happen on CSS; we don't want menu_helper sprinting into a
  # direct-connect dial-out with the prior match's character.
  dolphin._autostart = False
  last_poll = 0.0
  while True:
    try:
      gs = dolphin.next_gamestate()
    except TimeoutError:
      gs = None
    now = time.monotonic()
    # Drive menu_helper so Dolphin stays responsive on CSS. autostart
    # is forced False above; menu_helper will sit idle at the lobby
    # screen without re-entering matchmaking.
    if gs is not None and is_menu_state(gs):
      for i, (controller, plr) in enumerate(dolphin._menuing_controllers):
        dolphin.menu_helper.menu_helper_simple(
            gs, controller,
            stage_selected=dolphin.stage,
            connect_code=dolphin._connect_code,
            autostart=False,
            swag=False,
            costume=i,
            **plr.menuing_kwargs(),
        )
    if now - last_poll >= poll_interval:
      last_poll = now
      if sentinel_path and os.path.exists(sentinel_path):
        try:
          os.remove(sentinel_path)
        except OSError:
          pass
        return None
      data = _read_next_match_state(next_match_state_path)
      gen = data.get('generation')
      if isinstance(gen, int) and gen > last_generation:
        # Remove the handshake file so a later stat doesn't
        # re-trigger. Loss-tolerant: if removal fails we still rely on
        # the generation check to suppress duplicates.
        try:
          os.remove(next_match_state_path)
        except (FileNotFoundError, OSError):
          pass
        dolphin._autostart = True  # resume matchmaking for next game
        return data


def _window_tally_local(results, window=5, threshold=3):
  """Local mirror of LAUNCHER.bot_state._window_tally. Intentionally
  duplicated so scripts/netplay.py stays self-contained — importing
  the launcher package from a training-script subprocess would pull in
  FastAPI / Pydantic just to check three integers."""
  tail = results[-window:]
  ai = sum(1 for r in tail if r == 'ai')
  human = sum(1 for r in tail if r == 'human')
  if ai >= threshold:
    decided = 'ai'
  elif human >= threshold:
    decided = 'human'
  else:
    decided = None
  return ai, human, decided


def _next_game_is_possible_decider(results, window=5, threshold=3):
  """Before the next game starts (i.e. the tally below is computed from
  the already-completed games), return True iff either side is within
  one game of clinching the window. Used to fire the "one more" chat
  only when the upcoming game could actually close out the series,
  not every single CSS cycle."""
  # Consider the last (window-1) completed games — the next game will
  # enter as the newest entry in a fresh window. If either side already
  # has >= threshold-1 wins in those last (window-1) games, the next
  # game could clinch for them.
  tail = results[-(window - 1):]
  ai = sum(1 for r in tail if r == 'ai')
  human = sum(1 for r in tail if r == 'human')
  return ai >= (threshold - 1) or human >= (threshold - 1)


# Win32 message constants used by _close_dolphin_window_esc. The
# synthetic ESC keystroke invokes Dolphin's ESC hotkey handler (bound to
# Exit in the user's config), which tears down Dolphin's helper
# processes cleanly — process.terminate() on its own orphans them.
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_ESCAPE = 0x1B


def _dolphin_window_handles(pid):
  """Return the visible top-level HWNDs owned by ``pid``. Windows-only;
  returns [] on any other platform or if PID has no matching windows."""
  if sys.platform != 'win32':
    return []
  user32 = ctypes.windll.user32
  hwnds = []
  WNDENUMPROC = ctypes.WINFUNCTYPE(
      ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

  def _cb(hwnd, _lparam):
    w_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
    if w_pid.value == pid and user32.IsWindowVisible(hwnd):
      hwnds.append(hwnd)
    return True

  user32.EnumWindows(WNDENUMPROC(_cb), 0)
  return hwnds


def _close_dolphin_window_esc(dolphin):
  """Post WM_KEYDOWN+WM_KEYUP with VK_ESCAPE to Dolphin's window(s) so
  Dolphin's own ESC-hotkey handler runs its graceful exit. Non-blocking:
  returns as soon as the messages are posted.

  Relies on Dolphin's config having ESC bound to Exit. If the binding
  isn't there, this is a no-op and the ``finally`` block's
  ``dolphin.stop()`` (which calls ``process.kill()``) is the fallback —
  same behavior as before this helper existed."""
  if sys.platform != 'win32':
    return
  try:
    pid = dolphin._process.pid
  except AttributeError:
    logging.info('[quit] dolphin._process not available — skipping ESC')
    return
  hwnds = _dolphin_window_handles(pid)
  if not hwnds:
    logging.info('[quit] no Dolphin HWND for pid=%d — skipping ESC', pid)
    return
  user32 = ctypes.windll.user32
  for hwnd in hwnds:
    user32.PostMessageW(hwnd, _WM_KEYDOWN, _VK_ESCAPE, 0)
    user32.PostMessageW(hwnd, _WM_KEYUP, _VK_ESCAPE, 0)


def _wait_for_menu(dolphin, port, *, max_frames=300):
  """Advance with the controller released until the game reports a menu
  state, or ``max_frames`` elapses. Returns True iff a menu state was
  observed. Tolerates TimeoutError — on peer-disconnect paths the frame
  stream may be stalled; we just keep trying until frames resume or we
  time out."""
  controller = dolphin.controllers[port]
  for _ in range(max_frames):
    controller.release_all()
    controller.flush()
    try:
      gs = dolphin.next_gamestate()
    except TimeoutError:
      continue
    if gs is not None and is_menu_state(gs):
      return True
  return False


def _press_z_disconnect(dolphin, port, *,
                        tap_z_frames=12,
                        post_tap_idle_frames=30,
                        long_z_hold_frames=120,
                        post_hold_idle_frames=180):
  """Z-press cleanup used both at terminal exit and between persist-
  mode matches after a peer-left event.

  1. Short Z tap (~0.2s) — cancels any Slippi Direct/Ranked search if
     we're on CSS, and clears the "Failed to create mm client" error
     screen ("Press Z to clear error" prompt).
  2. Idle ~0.5s so the tap registers and the UI settles.
  3. Long Z hold (~2s) — disconnects from the opponent on CSS so
     Slippi won't keep cycling back into auto-rematchmaking.
  4. Idle ~3s so the disconnect resolves on Dolphin's side.

  Uses ``next_gamestate`` (not ``step``) so libmelee's menu_helper
  doesn't fight the Z press with its own CSS navigation. Does NOT
  close Dolphin — caller decides whether to also fire the ESC.
  """
  controller = dolphin.controllers[port]

  def advance():
    controller.flush()
    try:
      dolphin.next_gamestate()
    except TimeoutError:
      pass

  for _ in range(tap_z_frames):
    controller.release_all()
    controller.press_button(melee.Button.BUTTON_Z)
    advance()

  for _ in range(post_tap_idle_frames):
    controller.release_all()
    advance()

  for _ in range(long_z_hold_frames):
    controller.release_all()
    controller.press_button(melee.Button.BUTTON_Z)
    advance()

  for _ in range(post_hold_idle_frames):
    controller.release_all()
    advance()

  controller.release_all()
  controller.flush()


def _dolphin_quit_sequence(dolphin, port, *,
                           tap_z_frames=12,
                           post_tap_idle_frames=30,
                           long_z_hold_frames=120,
                           post_hold_idle_frames=180):
  """Final-steps sequence used at every TERMINAL exit path. Same Z
  press as ``_press_z_disconnect`` followed by a synthetic ESC
  keystroke to Dolphin's window so helper processes aren't orphaned."""
  _press_z_disconnect(
      dolphin, port,
      tap_z_frames=tap_z_frames,
      post_tap_idle_frames=post_tap_idle_frames,
      long_z_hold_frames=long_z_hold_frames,
      post_hold_idle_frames=post_hold_idle_frames,
  )
  _close_dolphin_window_esc(dolphin)


def _graceful_exit(dolphin, port, *, menu_wait_frames=300,
                   settle_frames=10):
  """Silent exit path used when chat would not reach the peer (full peer
  disconnect, stall, idle timeout). Waits up to ``menu_wait_frames``
  (~5s) for a menu state, settles briefly, then runs the canonical quit
  sequence (short Z tap → long Z hold → ESC) so Dolphin tears down its
  helper processes cleanly before the subprocess exits."""
  controller = dolphin.controllers[port]
  _wait_for_menu(dolphin, port, max_frames=menu_wait_frames)
  for _ in range(settle_frames):
    controller.release_all()
    controller.flush()
    try:
      dolphin.next_gamestate()
    except TimeoutError:
      pass
  _dolphin_quit_sequence(dolphin, port)


def _sentinel_exit(dolphin, port, *, was_in_game):
  """Exit path for user-initiated Stop (``/play/end-match`` sentinel).
  When ``was_in_game`` is True, hold Start+L+R+A to force-quit the
  active game; Melee then transitions back to the Slippi CSS on its
  own. On CSS, fire the "gotta go" + "ggs" chats so the human sees a
  clean sign-off, then run the canonical quit sequence.

  Skip Start+L+R+A when already on a menu — pressing the combo on CSS
  opens unrelated overlays and would compete with menu_helper's
  navigation."""
  if was_in_game:
    _hold_start_lra(dolphin, port, LRA_START_HOLD_FRAMES)
  _wait_for_menu(dolphin, port)
  _send_chat_sequence(dolphin, port, *_CHAT_GOTTA_GO)
  _send_chat_sequence(dolphin, port, *_CHAT_GGS)
  _dolphin_quit_sequence(dolphin, port)



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
