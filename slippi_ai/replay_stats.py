"""Compute per-replay gameplay statistics for dataset filtering.

Reuses efficient numpy implementations from reward.py where possible.
Conversion/neutral-win detection ported from slippi-js.
"""

import numpy as np
import melee

from slippi_ai.types import Game, Player
from slippi_ai.reward import (
  detect_l_cancel_misses,
  detect_wavedashes,
  process_deaths,
  process_damages,
  grabbed_ledge,
  got_shield_broken,
)

# ──────────────────────────────────────────────────────────────────────────────
# Action state sets
# ──────────────────────────────────────────────────────────────────────────────

# Damage / hitstun states (75-91)
_DAMAGE_STATES = frozenset(range(75, 92))

# Actionable grounded states where a player is in neutral
_ACTIONABLE_STATES = frozenset([
  melee.Action.STANDING.value,          # 14
  melee.Action.WALK_SLOW.value,         # 15
  melee.Action.WALK_MIDDLE.value,       # 16
  melee.Action.WALK_FAST.value,         # 17
  melee.Action.TURNING.value,           # 18
  melee.Action.DASHING.value,           # 20
  melee.Action.RUNNING.value,           # 21
  melee.Action.CROUCH_START.value,      # 39
  melee.Action.CROUCHING.value,         # 40
  melee.Action.CROUCH_END.value,        # 41
  melee.Action.LANDING.value,           # 42
  melee.Action.LANDING_SPECIAL.value,   # 43
  melee.Action.JUMPING_FORWARD.value,   # 24
  melee.Action.JUMPING_BACKWARD.value,  # 25
  melee.Action.FALLING.value,           # 29
  melee.Action.FALLING_FORWARD.value,   # 30
  melee.Action.FALLING_BACKWARD.value,  # 31
  melee.Action.FALLING_AERIAL.value,    # 32
  melee.Action.FALLING_AERIAL_FORWARD.value,   # 33
  melee.Action.FALLING_AERIAL_BACKWARD.value,  # 34
  melee.Action.SHIELD_START.value,      # 178
  melee.Action.SHIELD.value,            # 179
  melee.Action.SHIELD_RELEASE.value,    # 182
  melee.Action.EDGE_CATCHING.value,     # 252
])

_is_actionable = np.vectorize(lambda a: a in _ACTIONABLE_STATES)
_is_in_damage = np.vectorize(lambda a: a in _DAMAGE_STATES)

# Aerial attack actions
_AERIAL_ATTACKS = frozenset([
  melee.Action.NAIR.value,
  melee.Action.FAIR.value,
  melee.Action.BAIR.value,
  melee.Action.UAIR.value,
  melee.Action.DAIR.value,
])

# ──────────────────────────────────────────────────────────────────────────────
# L-cancel success detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_l_cancel_successes(player: Player) -> np.ndarray:
  """Detect successful L-cancels.

  A successful L-cancel transitions from an aerial attack directly to
  LANDING or LANDING_SPECIAL, skipping the aerial landing lag states.

  Returns:
    Length (T-1) boolean array.
  """
  action = player.action

  aerial_attacks = np.isin(action, list(_AERIAL_ATTACKS))

  success_landings = np.isin(action, [
    melee.Action.LANDING.value,          # 42
    melee.Action.LANDING_SPECIAL.value,  # 43
  ])

  return np.logical_and(aerial_attacks[:-1], success_landings[1:])

# ──────────────────────────────────────────────────────────────────────────────
# Conversion detection (ported from slippi-js)
# ──────────────────────────────────────────────────────────────────────────────

# Maximum gap between hits to be considered the same conversion
_CONVERSION_GAP = 45

def detect_conversions(attacker: Player, defender: Player) -> list[dict]:
  """Detect conversion sequences and classify their opening type.

  A conversion is a sequence of hits where consecutive hits are within
  45 frames of each other. Opening types follow slippi-js convention:
  - 'neutral': attacker was in an actionable state before the first hit
  - 'counter': attacker was in hitstun/damage before the first hit
  - 'trade': both players took damage around the same time

  Returns:
    List of dicts with keys: start_frame, end_frame, start_percent,
    end_percent, num_moves, did_kill, opening_type.
  """
  damage_dealt = process_damages(defender.percent)
  hit_frames = np.where(damage_dealt > 0)[0]

  if len(hit_frames) == 0:
    return []

  # Detect deaths for did_kill check
  deaths = process_deaths(defender.action)

  # Damage dealt to attacker (for trade detection)
  attacker_damage = process_damages(attacker.percent)

  conversions = []
  conv_start = hit_frames[0]
  conv_end = hit_frames[0]
  num_moves = 1

  for i in range(1, len(hit_frames)):
    gap = hit_frames[i] - conv_end
    if gap <= _CONVERSION_GAP:
      conv_end = hit_frames[i]
      num_moves += 1
    else:
      # Finalize current conversion
      conversions.append(_make_conversion(
        conv_start, conv_end, num_moves,
        attacker, defender, deaths, attacker_damage))
      conv_start = hit_frames[i]
      conv_end = hit_frames[i]
      num_moves = 1

  # Finalize last conversion
  conversions.append(_make_conversion(
    conv_start, conv_end, num_moves,
    attacker, defender, deaths, attacker_damage))

  return conversions


def _make_conversion(start, end, num_moves, attacker, defender,
                     deaths, attacker_damage) -> dict:
  """Build a conversion dict from frame indices."""
  start_percent = float(defender.percent[start])
  end_percent = float(defender.percent[min(end + 1, len(defender.percent) - 1)])

  # Check if defender died within _CONVERSION_GAP frames of last hit
  death_window_end = min(end + _CONVERSION_GAP, len(deaths))
  did_kill = bool(np.any(deaths[end:death_window_end]))

  # Classify opening type
  opening_type = _classify_opening(start, attacker, attacker_damage)

  return dict(
    start_frame=int(start),
    end_frame=int(end),
    start_percent=start_percent,
    end_percent=end_percent,
    num_moves=num_moves,
    did_kill=did_kill,
    opening_type=opening_type,
  )


def _classify_opening(hit_frame: int, attacker: Player,
                      attacker_damage: np.ndarray) -> str:
  """Classify the opening type of a conversion.

  Checks the attacker's state just before the first hit to determine
  if this was a neutral win, counter hit, or trade.
  """
  # Check for trade: attacker also took damage within a few frames
  trade_window = 10
  trade_start = max(0, hit_frame - trade_window)
  trade_end = min(hit_frame + trade_window, len(attacker_damage))
  if np.any(attacker_damage[trade_start:trade_end] > 0):
    return 'trade'

  # Check attacker's action state before the hit
  check_frame = max(0, hit_frame - 1)
  attacker_action = int(attacker.action[check_frame])

  if attacker_action in _DAMAGE_STATES:
    return 'counter'

  return 'neutral'

# ──────────────────────────────────────────────────────────────────────────────
# APM / input counting
# ──────────────────────────────────────────────────────────────────────────────

def compute_apm(player: Player, num_frames: int) -> tuple[float, float]:
  """Compute total APM and digital-only APM.

  Total APM counts button presses plus significant stick movements.
  Digital APM counts only button presses.

  Returns:
    (total_apm, digital_apm)
  """
  minutes = num_frames / 3600.0  # 60 fps
  if minutes <= 0:
    return 0.0, 0.0

  # Count digital button presses (0 -> 1 transitions)
  buttons = player.controller.buttons
  digital_presses = 0
  for btn_name in ('A', 'B', 'X', 'Y', 'Z', 'L', 'R', 'D_UP'):
    btn = getattr(buttons, btn_name)
    btn_arr = btn.astype(np.int8)
    transitions = np.maximum(btn_arr[1:] - btn_arr[:-1], 0)
    digital_presses += int(np.sum(transitions))

  # Count significant stick movements (>0.3 unit change)
  stick = player.controller.main_stick
  dx = np.abs(stick.x[1:] - stick.x[:-1])
  dy = np.abs(stick.y[1:] - stick.y[:-1])
  stick_moves = int(np.sum((dx > 0.3) | (dy > 0.3)))

  # C-stick movements
  cstick = player.controller.c_stick
  cdx = np.abs(cstick.x[1:] - cstick.x[:-1])
  cdy = np.abs(cstick.y[1:] - cstick.y[:-1])
  cstick_moves = int(np.sum((cdx > 0.3) | (cdy > 0.3)))

  total_inputs = digital_presses + stick_moves + cstick_moves
  return total_inputs / minutes, digital_presses / minutes

# ──────────────────────────────────────────────────────────────────────────────
# Action state counters
# ──────────────────────────────────────────────────────────────────────────────

def _count_transitions_into(action: np.ndarray, target_state: int) -> int:
  """Count edge-detected transitions into a specific action state."""
  is_target = action == target_state
  entered = np.logical_and(np.logical_not(is_target[:-1]), is_target[1:])
  return int(np.sum(entered))


def detect_dash_dances(player: Player) -> int:
  """Count dash dances (DASHING -> TURNING -> DASHING)."""
  action = player.action
  count = 0
  # Look for DASH -> TURN -> DASH pattern
  is_dash = action == melee.Action.DASHING.value
  is_turn = action == melee.Action.TURNING.value

  # Find turning frames preceded by dash and followed by dash
  for i in range(1, len(action) - 1):
    if is_turn[i] and is_dash[i - 1]:
      # Look ahead for next dash within 20 frames
      end = min(i + 20, len(action))
      if np.any(is_dash[i + 1:end]):
        count += 1
  return count


def count_actions(player: Player) -> dict[str, int]:
  """Count defensive and movement actions."""
  action = player.action

  # Wavedashes already counted by detect_wavedashes
  wavedashes = detect_wavedashes(player)

  return dict(
    roll_count=(
      _count_transitions_into(action, melee.Action.ROLL_FORWARD.value) +
      _count_transitions_into(action, melee.Action.ROLL_BACKWARD.value)
    ),
    spotdodge_count=_count_transitions_into(action, melee.Action.SPOTDODGE.value),
    airdodge_count=_count_transitions_into(action, melee.Action.AIRDODGE.value),
    ledgegrab_count=int(np.sum(grabbed_ledge(action))),
    wavedash_count=int(np.sum(wavedashes > 0)),
    dash_dance_count=detect_dash_dances(player),
  )

# ──────────────────────────────────────────────────────────────────────────────
# Top-level computation
# ──────────────────────────────────────────────────────────────────────────────

def compute_player_stats(player: Player, opponent: Player) -> dict:
  """Compute all stats for one player in a replay.

  Returns a flat dict of stat_name -> value.
  """
  num_frames = len(player.action)
  if num_frames < 60:  # Less than 1 second
    return _empty_stats(num_frames)

  minutes = num_frames / 3600.0

  # L-cancels
  l_misses = detect_l_cancel_misses(player)
  l_successes = detect_l_cancel_successes(player)
  total_l_attempts = int(np.sum(l_misses)) + int(np.sum(l_successes))
  l_cancel_rate = (
    int(np.sum(l_successes)) / total_l_attempts
    if total_l_attempts > 0 else None
  )

  # Wavedashes
  wavedashes = detect_wavedashes(player)
  wavedash_count = int(np.sum(wavedashes > 0))

  # Combat
  damage_dealt = float(np.sum(process_damages(opponent.percent)))
  kill_count = int(np.sum(process_deaths(opponent.action)))
  death_count = int(np.sum(process_deaths(player.action)))

  # Conversions and neutral game
  conversions = detect_conversions(player, opponent)
  opp_conversions = detect_conversions(opponent, player)

  conversion_count = len(conversions)
  neutral_wins = sum(1 for c in conversions if c['opening_type'] == 'neutral')
  counter_hits = sum(1 for c in conversions if c['opening_type'] == 'counter')
  opp_neutral_wins = sum(1 for c in opp_conversions if c['opening_type'] == 'neutral')

  total_neutral = neutral_wins + opp_neutral_wins
  neutral_win_ratio = neutral_wins / total_neutral if total_neutral > 0 else None

  counter_hit_ratio = (
    counter_hits / conversion_count if conversion_count > 0 else None
  )

  damage_per_opening = (
    damage_dealt / conversion_count if conversion_count > 0 else None
  )
  openings_per_kill = (
    conversion_count / kill_count if kill_count > 0 else None
  )

  # APM
  total_apm, digital_apm = compute_apm(player, num_frames)

  # Action counts
  actions = count_actions(player)

  return dict(
    l_cancel_rate=l_cancel_rate,
    l_cancel_attempts=total_l_attempts,
    wavedash_per_min=wavedash_count / minutes,
    apm=total_apm,
    digital_apm=digital_apm,
    total_damage_dealt=damage_dealt,
    kill_count=kill_count,
    death_count=death_count,
    conversion_count=conversion_count,
    damage_per_opening=damage_per_opening,
    openings_per_kill=openings_per_kill,
    neutral_win_ratio=neutral_win_ratio,
    counter_hit_ratio=counter_hit_ratio,
    num_frames=num_frames,
    **actions,
  )


def _empty_stats(num_frames: int) -> dict:
  """Return an empty stats dict for replays too short to analyze."""
  return dict(
    l_cancel_rate=None, l_cancel_attempts=0,
    wavedash_per_min=0.0, apm=0.0, digital_apm=0.0,
    total_damage_dealt=0.0, kill_count=0, death_count=0,
    conversion_count=0, damage_per_opening=None, openings_per_kill=None,
    neutral_win_ratio=None, counter_hit_ratio=None,
    roll_count=0, spotdodge_count=0, airdodge_count=0,
    ledgegrab_count=0, wavedash_count=0, dash_dance_count=0,
    num_frames=num_frames,
  )


def compute_replay_stats(game: Game) -> dict:
  """Compute stats for both players in a replay.

  Returns a dict with p0_* and p1_* prefixed keys for each player's stats.
  """
  p0_stats = compute_player_stats(game.p0, game.p1)
  p1_stats = compute_player_stats(game.p1, game.p0)

  result = {}
  for k, v in p0_stats.items():
    result[f'p0_{k}'] = v
  for k, v in p1_stats.items():
    result[f'p1_{k}'] = v
  return result


# All stat columns stored per player in SQLite
STAT_COLUMNS = [
  'l_cancel_rate', 'l_cancel_attempts',
  'wavedash_per_min',
  'apm', 'digital_apm',
  'total_damage_dealt',
  'kill_count', 'death_count',
  'conversion_count',
  'damage_per_opening', 'openings_per_kill',
  'neutral_win_ratio', 'counter_hit_ratio',
  'roll_count', 'spotdodge_count', 'airdodge_count',
  'ledgegrab_count', 'wavedash_count', 'dash_dance_count',
  'num_frames',
]
