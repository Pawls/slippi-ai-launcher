"""Compare tech skill execution between local and netplay conditions.

Runs eval_two locally at different online_delay settings and reports:
- Input mismatch rate (sent vs received controller states)
- Wavedash count and quality
- L-cancel success rate (detected via action state transitions)
- Per-frame action state logging for manual inspection

Usage:
  # Run local baseline (online_delay=0) vs netplay-simulated (online_delay=2)
  python scripts/compare_local_vs_netplay.py \
    --agent.path=/path/to/agent.pkl \
    --dolphin.path=/path/to/dolphin \
    --dolphin.iso=/path/to/SSBM.iso \
    --num_games=3

  # Custom delays to compare
  python scripts/compare_local_vs_netplay.py \
    --agent.path=/path/to/agent.pkl \
    --dolphin.path=/path/to/dolphin \
    --dolphin.iso=/path/to/SSBM.iso \
    --delays=0,2,3 \
    --num_games=3

  # Single delay (just collect stats without comparison)
  python scripts/compare_local_vs_netplay.py \
    --agent.path=/path/to/agent.pkl \
    --dolphin.path=/path/to/dolphin \
    --dolphin.iso=/path/to/SSBM.iso \
    --delays=2 \
    --num_games=5
"""

import dataclasses
import logging
import os
import sys

from absl import app
from absl import flags
import fancyflags as ff
import numpy as np
import tensorflow as tf

from slippi_ai import eval_lib, flag_utils, utils
from slippi_ai import dolphin as dolphin_lib

import melee

# --- Action state constants for tech skill detection ---

# Aerial attack actions
AERIAL_ACTIONS = frozenset([
    melee.Action.NAIR.value,       # 65
    melee.Action.FAIR.value,       # 66
    melee.Action.BAIR.value,       # 67
    melee.Action.UAIR.value,       # 68
    melee.Action.DAIR.value,       # 69
])

# Aerial landing lag actions (L-cancel halves these)
AERIAL_LANDING_ACTIONS = frozenset([
    melee.Action.NAIR_LANDING.value,  # 70
    melee.Action.FAIR_LANDING.value,  # 71
    melee.Action.BAIR_LANDING.value,  # 72
    melee.Action.UAIR_LANDING.value,  # 73
    melee.Action.DAIR_LANDING.value,  # 74
])

# Clean landing (no lag, or L-cancelled)
LANDING_ACTIONS = frozenset([
    melee.Action.LANDING.value,         # 42
    melee.Action.LANDING_SPECIAL.value, # 43
])

# Wavedash detection
AIRDODGE_ACTION = melee.Action.AIRDODGE.value  # 236
LANDING_SPECIAL_ACTION = melee.Action.LANDING_SPECIAL.value  # 43

# Crouch states
CROUCH_ACTIONS = frozenset([
    melee.Action.CROUCH_START.value,  # 39
    melee.Action.CROUCHING.value,     # 40
    melee.Action.CROUCH_END.value,    # 41
])


@dataclasses.dataclass
class TechStats:
    """Accumulated tech skill statistics for one run."""
    # Input tracking
    input_mismatches: int = 0
    input_comparisons: int = 0

    # Wavedash tracking
    wavedash_count: int = 0
    wavedash_quality_sum: float = 0.0

    # L-cancel tracking
    aerial_landings: int = 0       # total aerial -> landing transitions
    l_cancel_successes: int = 0    # landed with short lag (L-cancelled)
    l_cancel_failures: int = 0     # landed with full lag (missed L-cancel)

    # Frame counts
    total_frames: int = 0
    games_played: int = 0

    # Action state history for debugging (frame, action, x, y, percent)
    action_transitions: list = dataclasses.field(default_factory=list)

    @property
    def mismatch_rate(self) -> float:
        if self.input_comparisons == 0:
            return 0.0
        return self.input_mismatches / self.input_comparisons

    @property
    def wavedash_quality(self) -> float:
        if self.wavedash_count == 0:
            return 0.0
        return self.wavedash_quality_sum / self.wavedash_count

    @property
    def l_cancel_rate(self) -> float:
        if self.aerial_landings == 0:
            return 0.0
        return self.l_cancel_successes / self.aerial_landings

    @property
    def wavedashes_per_minute(self) -> float:
        if self.total_frames == 0:
            return 0.0
        return self.wavedash_count / (self.total_frames / 3600)


class TechSkillTracker:
    """Tracks tech skill execution frame-by-frame for one agent port."""

    def __init__(self, port: int):
        self.port = port
        self.stats = TechStats()
        self._prev_action = None
        self._prev_x = None
        self._prev_in_aerial = False
        self._landing_lag_frames = 0
        self._tracking_landing = False
        self._frame = 0

    def reset_game(self):
        """Reset per-game state (called at game start)."""
        self._prev_action = None
        self._prev_x = None
        self._prev_in_aerial = False
        self._landing_lag_frames = 0
        self._tracking_landing = False
        self.stats.games_played += 1

    def update(self, gamestate: melee.GameState):
        """Process one frame of game state."""
        player = gamestate.players.get(self.port)
        if player is None:
            return

        action = player.action.value
        x = player.position.x
        self._frame = gamestate.frame
        self.stats.total_frames += 1

        if self._prev_action is not None:
            # --- Wavedash detection ---
            if (self._prev_action == AIRDODGE_ACTION and
                action == LANDING_SPECIAL_ACTION):
                dx = abs(x - self._prev_x)
                self.stats.wavedash_count += 1
                self.stats.wavedash_quality_sum += dx
                self.stats.action_transitions.append(
                    (self._frame, 'WAVEDASH', f'quality={dx:.2f}'))

            # --- L-cancel detection ---
            # Detect: was in an aerial attack, now in a landing state
            if self._prev_action in AERIAL_ACTIONS:
                if action in AERIAL_LANDING_ACTIONS:
                    # Entered aerial landing lag -> missed L-cancel
                    self.stats.aerial_landings += 1
                    self.stats.l_cancel_failures += 1
                    self.stats.action_transitions.append(
                        (self._frame, 'L-CANCEL MISS',
                         f'from={melee.Action(self._prev_action).name}'))
                elif action in LANDING_ACTIONS:
                    # Clean landing from aerial -> successful L-cancel
                    self.stats.aerial_landings += 1
                    self.stats.l_cancel_successes += 1
                    self.stats.action_transitions.append(
                        (self._frame, 'L-CANCEL HIT',
                         f'from={melee.Action(self._prev_action).name}'))

            # Also catch cases where aerial -> some intermediate -> landing
            # (e.g., aerial attack is still active for multiple frames)
            if (self._prev_in_aerial and
                self._prev_action not in AERIAL_ACTIONS and
                action in AERIAL_LANDING_ACTIONS):
                # Was recently in aerial, now in landing lag
                self.stats.aerial_landings += 1
                self.stats.l_cancel_failures += 1

        self._prev_in_aerial = action in AERIAL_ACTIONS
        self._prev_action = action
        self._prev_x = x


def run_session(
    agent_flags: dict,
    dolphin_flags: dict,
    online_delay: int,
    num_games: int,
    use_gpu: bool,
) -> TechStats:
    """Run a full eval session at a given online_delay and collect stats."""

    logging.info(f'\n{"="*60}')
    logging.info(f'Starting session with online_delay={online_delay}')
    logging.info(f'{"="*60}')

    # Override delay for this session
    dolphin_flags = dict(dolphin_flags)
    dolphin_flags['online_delay'] = online_delay

    PORTS = (1, 2)

    # Build players - P1 is our agent, P2 is the opponent agent
    players_config = {}
    for p in PORTS:
        players_config[p] = eval_lib.get_player(
            type='ai', ai=agent_flags)

    agents = []
    trackers = []

    for port, opponent_port in zip(PORTS, reversed(PORTS)):
        player = players_config[port]
        if isinstance(player, dolphin_lib.AI):
            agent = eval_lib.build_agent(
                port=port,
                opponent_port=opponent_port,
                console_delay=online_delay,
                **agent_flags,
            )
            agent.start()
            agents.append(agent)
            eval_lib.update_character(player, agent.config)
            trackers.append(TechSkillTracker(port))

    # Force headless for automated comparison
    dolphin_flags['headless'] = True

    dolphin = dolphin_lib.Dolphin(
        players=players_config,
        **dolphin_lib.DolphinConfig.kwargs_from_flags(dolphin_flags),
    )

    for agent in agents:
        agent.set_controller(dolphin.controllers[agent._port])

    games_completed = 0

    try:
        for gamestate in dolphin.iter_gamestates(skip_menu_frames=False):
            if dolphin_lib.is_menu_state(gamestate):
                if games_completed == num_games:
                    break
                continue

            if gamestate.frame == -123:
                games_completed += 1
                logging.info(f'  Game {games_completed}/{num_games}')
                for agent in agents:
                    agent.input_mismatches = 0
                    agent.input_comparisons = 0
                for tracker in trackers:
                    tracker.reset_game()

            for agent in agents:
                agent.step(gamestate)

            for tracker in trackers:
                tracker.update(gamestate)

    finally:
        # Collect input mismatch stats from agents
        for agent, tracker in zip(agents, trackers):
            tracker.stats.input_mismatches += agent.input_mismatches
            tracker.stats.input_comparisons += agent.input_comparisons

        for agent in agents:
            agent.stop()
        dolphin.stop()

    # Return stats for port 1 (our primary agent)
    return trackers[0].stats


def print_stats(label: str, stats: TechStats):
    """Pretty-print tech skill statistics."""
    print(f'\n{"─"*50}')
    print(f'  {label}')
    print(f'{"─"*50}')
    print(f'  Games played:        {stats.games_played}')
    print(f'  Total frames:        {stats.total_frames}')
    print()
    print(f'  INPUT VERIFICATION')
    print(f'    Comparisons:       {stats.input_comparisons}')
    print(f'    Mismatches:        {stats.input_mismatches}')
    print(f'    Mismatch rate:     {stats.mismatch_rate:.6f} '
          f'({stats.mismatch_rate*100:.4f}%)')
    print()
    print(f'  WAVEDASHES')
    print(f'    Count:             {stats.wavedash_count}')
    print(f'    Per minute:        {stats.wavedashes_per_minute:.1f}')
    print(f'    Avg quality:       {stats.wavedash_quality:.2f}')
    print()
    print(f'  L-CANCELS')
    print(f'    Aerial landings:   {stats.aerial_landings}')
    print(f'    Successes:         {stats.l_cancel_successes}')
    print(f'    Failures:          {stats.l_cancel_failures}')
    print(f'    Success rate:      {stats.l_cancel_rate:.1%}')
    print()

    # Show first N action transitions for manual inspection
    transitions = stats.action_transitions[:20]
    if transitions:
        print(f'  TECH SKILL LOG (first {len(transitions)} events)')
        for frame, event, detail in transitions:
            print(f'    Frame {frame:>6}: {event:<16} {detail}')
    print()


def print_comparison(results: dict[int, TechStats]):
    """Print side-by-side comparison of results at different delays."""
    delays = sorted(results.keys())

    if len(delays) < 2:
        return

    print(f'\n{"="*60}')
    print(f'  COMPARISON SUMMARY')
    print(f'{"="*60}')

    header = f'{"Metric":<25}'
    for d in delays:
        header += f'  {"delay="+str(d):>12}'
    print(header)
    print('─' * (25 + 14 * len(delays)))

    rows = [
        ('Input mismatches',
         [f'{results[d].input_mismatches}' for d in delays]),
        ('Mismatch rate',
         [f'{results[d].mismatch_rate:.6f}' for d in delays]),
        ('Wavedash count',
         [f'{results[d].wavedash_count}' for d in delays]),
        ('Wavedash/min',
         [f'{results[d].wavedashes_per_minute:.1f}' for d in delays]),
        ('Wavedash quality',
         [f'{results[d].wavedash_quality:.2f}' for d in delays]),
        ('L-cancel attempts',
         [f'{results[d].aerial_landings}' for d in delays]),
        ('L-cancel rate',
         [f'{results[d].l_cancel_rate:.1%}' for d in delays]),
        ('Total frames',
         [f'{results[d].total_frames}' for d in delays]),
    ]

    for label, values in rows:
        row = f'{label:<25}'
        for v in values:
            row += f'  {v:>12}'
        print(row)

    # Flag concerning differences
    print()
    baseline = results[delays[0]]
    for d in delays[1:]:
        other = results[d]
        if other.mismatch_rate > baseline.mismatch_rate + 0.001:
            print(f'  WARNING: delay={d} has significantly higher mismatch '
                  f'rate ({other.mismatch_rate:.4%} vs {baseline.mismatch_rate:.4%})')
        if (baseline.wavedashes_per_minute > 0 and
            other.wavedashes_per_minute < baseline.wavedashes_per_minute * 0.8):
            print(f'  WARNING: delay={d} has significantly fewer wavedashes '
                  f'({other.wavedashes_per_minute:.1f} vs '
                  f'{baseline.wavedashes_per_minute:.1f}/min)')
        if (baseline.l_cancel_rate > 0 and
            other.l_cancel_rate < baseline.l_cancel_rate - 0.1):
            print(f'  WARNING: delay={d} has lower L-cancel rate '
                  f'({other.l_cancel_rate:.1%} vs {baseline.l_cancel_rate:.1%})')

    if all(results[d].input_mismatches == 0 for d in delays):
        print('  OK: No input mismatches at any delay setting.')

    print()


# --- Flag definitions ---

PORTS = (1, 2)

agent_flags = eval_lib.AGENT_FLAGS.copy()
agent_flags['async_inference'] = ff.Boolean(True)
AGENT = ff.DEFINE_dict('agent', **agent_flags)

dolphin_config = dolphin_lib.DolphinConfig(
    headless=True,
    infinite_time=False,
    online_delay=0,
    path=os.environ.get('DOLPHIN_PATH'),
    iso=os.environ.get('ISO_PATH'),
)
DOLPHIN = ff.DEFINE_dict(
    'dolphin', **flag_utils.get_flags_from_default(dolphin_config))

NUM_GAMES = flags.DEFINE_integer(
    'num_games', 3, 'Number of games per delay setting')
DELAYS = flags.DEFINE_string(
    'delays', '0,2',
    'Comma-separated online_delay values to compare (e.g. "0,2,3")')
USE_GPU = flags.DEFINE_boolean(
    'use_gpu', False, 'Use GPU for AI inference')

FLAGS = flags.FLAGS


def main(_):
    if USE_GPU.value:
        gpus = tf.config.list_physical_devices('GPU')
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
    else:
        eval_lib.disable_gpus()

    delays = [int(d.strip()) for d in DELAYS.value.split(',')]
    num_games = NUM_GAMES.value

    print(f'Comparing online_delay settings: {delays}')
    print(f'Games per setting: {num_games}')

    results = {}

    for delay in delays:
        stats = run_session(
            agent_flags=AGENT.value,
            dolphin_flags=DOLPHIN.value,
            online_delay=delay,
            num_games=num_games,
            use_gpu=USE_GPU.value,
        )
        results[delay] = stats
        print_stats(f'online_delay={delay}', stats)

    if len(delays) > 1:
        print_comparison(results)


if __name__ == '__main__':
    __spec__ = None
    app.run(main)
