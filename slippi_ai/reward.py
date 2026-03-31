"""Calculate rewards."""

import dataclasses

import numpy as np

import melee
from slippi_ai.types import Game, Player

def is_dying(player_action: np.ndarray) -> np.ndarray:
  # See https://docs.google.com/spreadsheets/d/1JX2w-r2fuvWuNgGb6D3Cs4wHQKLFegZe2jhbBuIhCG8/edit#gid=13
  return player_action <= 0xA

def process_deaths(player_action: np.ndarray) -> np.ndarray:
  deaths = is_dying(player_action)
  # Players are in a dead action-state for many consecutive frames.
  # Prune all but the first frame of death
  return np.logical_and(np.logical_not(deaths[:-1]), deaths[1:])

def process_damages(damages: np.ndarray) -> np.ndarray:
  damages = damages.astype(np.float32)
  return np.maximum(damages[1:] - damages[:-1], 0)

def grabbed_ledge(player_action: np.ndarray) -> np.ndarray:
  is_ledge_grab = player_action == melee.Action.EDGE_CATCHING.value
  return np.logical_and(np.logical_not(is_ledge_grab[:-1]), is_ledge_grab[1:])

def get_bad_ledge_grabs(player: Player, opponent: Player) -> np.ndarray:
  ledge_grabs = grabbed_ledge(player.action)

  # Don't penalize if opponent is offstage
  opponent_direction = player.x < opponent.x  # True if opponent is right
  center_direction = player.x < 0  # True if center is right
  opponent_towards_center = opponent_direction == center_direction
  bad_ledge_grabs = np.logical_and(ledge_grabs, opponent_towards_center[:-1])

  # Also ok if opponent is invincible (like after respawn)
  bad_ledge_grabs = np.logical_and(
      bad_ledge_grabs, np.logical_not(opponent.invulnerable[:-1]))

  return bad_ledge_grabs

def normalize(xys, epsilon=1e-6):
  r = np.sqrt(np.sum(np.square(xys), axis=-1, keepdims=True))
  return xys / (r + epsilon)

def compute_approaching_factor(
    player: Player, opponent: Player) -> np.ndarray:
  """Measures how much we are approaching the opponent on each frame."""
  xy = np.stack([player.x, player.y], axis=-1)
  v = xy[1:] - xy[:-1]

  opp_xy = np.stack([opponent.x, opponent.y], axis=-1)
  dxy = normalize(opp_xy - xy)

  approach_factor = np.sum(v * dxy[:-1], axis=-1)

  # Player teleports when respawning.
  dying = is_dying(player.action)
  respawning = np.logical_and(dying[:-1], np.logical_not(dying[1:]))
  approach_factor = np.where(respawning, 0, approach_factor)

  return approach_factor

stage_to_edge_x = {
    stage.value: x for stage, x in melee.stages.EDGE_POSITION.items()
}
get_edge_x = np.vectorize(lambda x: stage_to_edge_x.get(x, 100))

# Above this height is considered offstage for the purposes of stalling.
# The highest top platform is at 54.4 on Battlefield.
MAX_STALLING_Y = 60

def amount_offstage(player: Player, stage: np.ndarray) -> np.ndarray:
  stage_xs = get_edge_x(stage[0])
  dx = np.maximum(np.abs(player.x) - stage_xs, 0)

  below = -np.minimum(player.y, 0)
  above = np.maximum(player.y - MAX_STALLING_Y, 0)
  dy = np.maximum(below, above)

  return np.sqrt(np.square(dx) + np.square(dy))


DEFAULT_STALLING_THRESHOLD = 20

def is_stalling_offstage(
    player: Player,
    stage: np.ndarray,
    threshold: float = DEFAULT_STALLING_THRESHOLD,
) -> np.ndarray:
  return amount_offstage(player, stage) > threshold

def is_aerial_shine(player: Player):
  is_fox = player.character == melee.Character.FOX.value
  is_falco = player.character == melee.Character.FALCO.value
  is_spacie = np.logical_or(is_fox, is_falco)

  # We only care about aerial shines.
  is_shine = player.action == melee.Action.DOWN_B_AIR

  return np.logical_and(is_spacie, is_shine)

def find_offstage_shine_stalls(player: Player, stage: np.ndarray):
  return np.logical_and(
      is_stalling_offstage(player, stage),
      is_aerial_shine(player))


# Shield break action states (205-211).
SHIELD_BREAK_ACTIONS = frozenset([
    melee.Action.SHIELD_BREAK_FLY.value,
    melee.Action.SHIELD_BREAK_FALL.value,
    melee.Action.SHIELD_BREAK_DOWN_U.value,
    melee.Action.SHIELD_BREAK_DOWN_D.value,
    melee.Action.SHIELD_BREAK_STAND_U.value,
    melee.Action.SHIELD_BREAK_STAND_D.value,
    melee.Action.SHIELD_BREAK_TEETER.value,
])

_is_shield_broken = np.vectorize(lambda a: a in SHIELD_BREAK_ACTIONS)

def got_shield_broken(player_action: np.ndarray) -> np.ndarray:
  """Detect the first frame of shield break (one event per break)."""
  broken = _is_shield_broken(player_action)
  return np.logical_and(np.logical_not(broken[:-1]), broken[1:])


def died_offstage(
    player: Player,
    stage: np.ndarray,
    threshold: float = DEFAULT_STALLING_THRESHOLD,
) -> np.ndarray:
  """Detect deaths where the player was offstage before dying.

  This catches SDs during edgeguards — deaths that occur when the
  player was far from the stage, as opposed to being killed onstage.
  """
  deaths = process_deaths(player.action)
  offstage = amount_offstage(player, stage) > threshold
  # Player was offstage on the frame before dying.
  return np.logical_and(deaths, offstage[:-1])


# Hitstun/knockback action states — player has no control
_HITSTUN_ACTIONS = set()
for _action in melee.Action:
  if _action.name.startswith('DAMAGE'):
    _HITSTUN_ACTIONS.add(_action.value)
_HITSTUN_ACTIONS.add(melee.Action.TUMBLING.value)

_is_in_hitstun = np.vectorize(lambda a: a in _HITSTUN_ACTIONS)

# Small threshold for detecting stage departures (just past the edge)
OFFSTAGE_DEPARTURE_THRESHOLD = 5


def voluntary_offstage_death(
    player: Player,
    stage: np.ndarray,
    departure_threshold: float = OFFSTAGE_DEPARTURE_THRESHOLD,
) -> np.ndarray:
  """Detect deaths following voluntary offstage excursions.

  Catches failed edgeguard SDs — deaths where the player left the
  stage under their own control (not knocked off by a hit).
  Does not penalize deaths from being knocked offstage by the opponent.
  """
  T = len(player.action)
  offstage = amount_offstage(player, stage) > departure_threshold
  deaths = process_deaths(player.action)  # length T-1
  hitstun = _is_in_hitstun(player.action)  # length T

  # Onstage→offstage transitions
  went_offstage = (~offstage[:-1]) & offstage[1:]

  # Forward-fill: track whether the current offstage excursion is voluntary.
  # Resets when the player returns to stage (including after respawn).
  voluntary_now = np.zeros(T - 1, dtype=bool)
  is_vol = False
  for i in range(T - 1):
    if not offstage[i + 1]:  # back on stage
      is_vol = False
    if went_offstage[i]:
      is_vol = not hitstun[i]
    voluntary_now[i] = is_vol

  return deaths & voluntary_now


def detect_l_cancel_misses(player: Player) -> np.ndarray:
  """Detect missed L-cancels (aerial attack landing with full lag).

  A missed L-cancel is detected as transitioning from an aerial attack
  action (NAIR/FAIR/BAIR/UAIR/DAIR) into the corresponding aerial
  landing lag action (NAIR_LANDING/FAIR_LANDING/etc). A successful
  L-cancel lands with halved lag and does not enter these states.

  Returns:
    Length (T-1) boolean array. True on frames where an L-cancel was missed.
  """
  action = player.action

  aerial_attacks = np.isin(action, [
      melee.Action.NAIR.value,
      melee.Action.FAIR.value,
      melee.Action.BAIR.value,
      melee.Action.UAIR.value,
      melee.Action.DAIR.value,
  ])

  aerial_landing_lag = np.isin(action, [
      melee.Action.NAIR_LANDING.value,
      melee.Action.FAIR_LANDING.value,
      melee.Action.BAIR_LANDING.value,
      melee.Action.UAIR_LANDING.value,
      melee.Action.DAIR_LANDING.value,
  ])

  # Transition: was in aerial attack, now in landing lag
  return np.logical_and(aerial_attacks[:-1], aerial_landing_lag[1:])


def detect_wavedashes(player: Player) -> np.ndarray:
  """Detect wavedash/waveland events and measure their quality.

  A wavedash/waveland is detected as entering LANDING_SPECIAL (43)
  from AIRDODGE (236). The quality is the horizontal speed gained,
  measured as the distance covered on the first frame of landing.

  Returns:
    Length (T-1) array of wavedash quality (horizontal displacement)
    per frame. Zero on non-wavedash frames.
  """
  action = player.action
  airdodge = action == melee.Action.AIRDODGE.value
  land_special = action == melee.Action.LANDING_SPECIAL.value

  # Transition: was in airdodge, now in landing special
  entered_land_special = np.logical_and(airdodge[:-1], land_special[1:])

  # Horizontal displacement on the landing frame measures quality.
  # A perfect wavedash has maximum displacement; a bad angle has less.
  dx = np.abs(player.x[1:] - player.x[:-1])

  return np.where(entered_land_special, dx, 0).astype(np.float32)


@dataclasses.dataclass
class RewardConfig:
  damage_ratio: float = 0.01
  ledge_grab_penalty: float = 0
  approaching_factor: float = 0
  stalling_penalty: float = 0  # per second
  stalling_threshold: float = DEFAULT_STALLING_THRESHOLD
  nana_ratio: float = 0.5
  shield_break_penalty: float = 0
  voluntary_offstage_death_penalty: float = 0
  wavedash_reward: float = 0
  l_cancel_miss_penalty: float = 0

def ko_diff(game: Game) -> np.ndarray:
  """Compute the KO difference (p0 KOs - p1 KOs) per frame."""
  p0_deaths = process_deaths(game.p0.action).astype(np.int32)
  p1_deaths = process_deaths(game.p1.action).astype(np.int32)

  return p1_deaths - p0_deaths

def compute_rewards(
    game: Game,
    damage_ratio: float = 0.01,
    ledge_grab_penalty: float = 0,
    approaching_factor: float = 0,
    stalling_penalty: float = 0,  # per second
    stalling_threshold: float = DEFAULT_STALLING_THRESHOLD,
    nana_ratio: float = 0.5,
    shield_break_penalty: float = 0,
    voluntary_offstage_death_penalty: float = 0,
    wavedash_reward: float = 0,
    l_cancel_miss_penalty: float = 0,
) -> np.ndarray:
  '''
    Args:
      game: nest of np.arrays of length T, from make_dataset.py
      damage_ratio: How much damage (percent) counts relative to stocks
    Returns:
      A length (T-1) np.array of rewards
  '''

  def compute_base_reward(player: Player) -> np.ndarray:
    deaths = process_deaths(player.action).astype(np.float32)
    damages = damage_ratio * process_damages(player.percent)
    return - (deaths + damages)

  def player_reward(player: Player, opponent: Player):
    reward = compute_base_reward(player)

    if nana_ratio != 0:
      if player.nana == ():
        raise ValueError("Nana data is required for nana_ratio != 0")
      nana_reward = nana_ratio * compute_base_reward(player.nana)
      nana_reward = np.where(player.nana.exists[1:], nana_reward, 0)
      reward += nana_reward

    bad_ledge_grabs = get_bad_ledge_grabs(player, opponent).astype(np.float32)
    reward -= ledge_grab_penalty * bad_ledge_grabs

    stalling = is_stalling_offstage(player, game.stage, stalling_threshold)[1:]
    reward -= (stalling_penalty / 60) * stalling.astype(np.float32)

    reward += approaching_factor * compute_approaching_factor(player, opponent)

    if shield_break_penalty != 0:
      shield_breaks = got_shield_broken(player.action).astype(np.float32)
      reward -= shield_break_penalty * shield_breaks

    if voluntary_offstage_death_penalty != 0:
      vol_offstage_deaths = voluntary_offstage_death(
          player, game.stage).astype(np.float32)
      reward -= voluntary_offstage_death_penalty * vol_offstage_deaths

    if wavedash_reward != 0:
      reward += wavedash_reward * detect_wavedashes(player)

    if l_cancel_miss_penalty != 0:
      l_cancel_misses = detect_l_cancel_misses(player).astype(np.float32)
      reward -= l_cancel_miss_penalty * l_cancel_misses

    return reward

  # Zero-sum rewards ensure there can be no collusion.
  rewards = player_reward(game.p0, game.p1) - player_reward(game.p1, game.p0)

  # sanity checks — bound accounts for kill (1) + voluntary_offstage_death_penalty (0.5)
  # + shield_break_penalty (0.5) + damage, doubled by zero-sum subtraction
  assert np.all(rewards > -5), f'reward too low: {rewards.min()}'
  assert np.all(rewards < 5), f'reward too high: {rewards.max()}'
  assert rewards.dtype == np.float32

  return rewards

def player_stats(
    player: Player,
    opponent: Player,
    stage: np.ndarray,
    stalling_threshold: float = DEFAULT_STALLING_THRESHOLD,
) -> dict:
  FPM = 60 * 60

  stats = dict(
      deaths=process_deaths(player.action).mean() * FPM,
      damages=process_damages(player.percent).mean() * FPM,
      ledge_grabs=get_bad_ledge_grabs(player, opponent).mean() * FPM,
      approaching_factor=compute_approaching_factor(player, opponent).mean(),
      stalling=is_stalling_offstage(player, stage, stalling_threshold).mean(),
      shield_breaks=got_shield_broken(player.action).mean() * FPM,
      voluntary_offstage_deaths=voluntary_offstage_death(player, stage).mean() * FPM,
  )

  wd = detect_wavedashes(player)
  wd_count = wd.astype(bool).sum()
  stats['wavedashes'] = wd.astype(bool).mean() * FPM
  stats['wavedash_quality'] = float(wd.sum() / max(wd_count, 1))

  lc_misses = detect_l_cancel_misses(player)
  stats['l_cancel_misses'] = lc_misses.mean() * FPM

  if player.nana != ():
    nana_deaths = process_deaths(player.nana.action).astype(np.float32)
    nana_deaths = np.where(player.nana.exists[1:], nana_deaths, 0)

    nana_damages = process_damages(player.nana.percent).astype(np.float32)
    nana_damages = np.where(player.nana.exists[1:], nana_damages, 0)

    stats.update(
        nana_deaths=nana_deaths.mean() * FPM,
        nana_damages=nana_damages.mean() * FPM,
    )

  return stats

def player_stats_from_game(game: Game, swap: bool = False, **kwargs) -> dict:
  p0, p1 = (game.p1, game.p0) if swap else (game.p0, game.p1)
  return player_stats(p0, p1, game.stage, **kwargs)

# TODO: test that the two ways of getting reward yield the same results
def get_reward(
    prev_state: melee.GameState,
    next_state: melee.GameState,
    own_port: int,
    opponent_port: int,
    damage_ratio: float = 0.01,
) -> float:
  """Reward implemented directly on gamestates."""

  def player_reward(port: int):
    players = [prev_state.players[port], next_state.players[port]]
    actions = np.array([p.action for p in players])
    deaths = process_deaths(actions).astype(np.float32).item()

    percents = np.array([p.percent for p in players])
    damage = damage_ratio * process_damages(percents).item()
    return - (deaths + damage)

  return player_reward(own_port) - player_reward(opponent_port)
