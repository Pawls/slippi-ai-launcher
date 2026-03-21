"""Test multishine consistency across different online_delay settings.

Sends frame-perfect multishine inputs (identical to run_multishine.py) and
measures how many shines Fox actually lands at each delay.  Under delay=0
the inputs arrive on time and multishines should be near-perfect.  Higher
delays shift the input timing and cause failures.

Usage:
  python scripts/multishine_delay_test.py \
    --dolphin_executable_path=/path/to/dolphin \
    --iso=/path/to/SSBM.iso

  # Custom delays and runtime
  python scripts/multishine_delay_test.py \
    --dolphin_executable_path=/path/to/dolphin \
    --iso=/path/to/SSBM.iso \
    --delays=0,1,2,3,4 \
    --runtime=30
"""

import argparse
import sys

import melee
from slippi_ai import dolphin, techskill

# Action state values for Fox
SHINE_ACTIONS = frozenset([
    melee.Action.DOWN_B_GROUND_START.value,  # 0x168
    melee.Action.DOWN_B_GROUND.value,        # 0x169
    melee.Action.DOWN_B_AIR.value,           # 0x16e
])
KNEE_BEND = melee.Action.KNEE_BEND.value     # 0x18 (jumpsquat)
STANDING = melee.Action.STANDING.value       # 0x0e


class MultishineTracker:
    """Track multishine success/failure for one port."""

    def __init__(self, port: int):
        self.port = port
        self.shine_count = 0
        self.jump_count = 0
        self.total_frames = 0
        self._prev_action = None
        self._prev_in_shine = False

    def update(self, gamestate: melee.GameState):
        player = gamestate.players.get(self.port)
        if player is None:
            return

        action = player.action.value
        self.total_frames += 1

        in_shine = action in SHINE_ACTIONS

        # Count each new shine entry
        if in_shine and not self._prev_in_shine:
            self.shine_count += 1

        # Count each jumpsquat entry (new jump)
        if action == KNEE_BEND and self._prev_action != KNEE_BEND:
            self.jump_count += 1

        self._prev_in_shine = in_shine
        self._prev_action = action


def run_one_delay(dolphin_path, iso_path, delay, runtime_seconds):
    """Run multishines at a specific online_delay and return stats."""
    players = {
        port: dolphin.AI(melee.Character.FOX)
        for port in (1, 2)
    }

    console = dolphin.Dolphin(
        path=dolphin_path,
        iso=iso_path,
        players=players,
        online_delay=delay,
        headless=True,
        emulation_speed=0,  # unlimited speed in headless
    )

    agents = []
    trackers = []
    for port in [1, 2]:
        agents.append(techskill.MultiShine(port, console.controllers[port]))
        trackers.append(MultishineTracker(port))

    num_frames = 0
    target_frames = runtime_seconds * 60

    try:
        while num_frames < target_frames:
            gamestate = console.step()
            for agent in agents:
                agent.step(gamestate)
            for tracker in trackers:
                tracker.update(gamestate)
            num_frames += 1
    finally:
        console.stop()

    return trackers[0]  # report for port 1


def main():
    parser = argparse.ArgumentParser(
        description='Test multishine consistency across online_delay settings')
    parser.add_argument('--dolphin_executable_path', '-e', default=None,
                        help='The directory where dolphin is')
    parser.add_argument('--iso', default=None, type=str,
                        help='Path to melee iso.')
    parser.add_argument('--runtime', default=15, type=int,
                        help='Runtime in seconds per delay setting.')
    parser.add_argument('--delays', default='0,1,2,3',
                        help='Comma-separated online_delay values to test.')

    args = parser.parse_args()

    delays = [int(d.strip()) for d in args.delays.split(',')]

    print(f'Multishine delay test')
    print(f'Delays: {delays}')
    print(f'Runtime per delay: {args.runtime}s')
    print()

    results = {}

    for delay in delays:
        print(f'Running delay={delay} ...', end=' ', flush=True)
        tracker = run_one_delay(
            args.dolphin_executable_path, args.iso, delay, args.runtime)
        results[delay] = tracker
        print(f'done  ({tracker.shine_count} shines, '
              f'{tracker.jump_count} jumps in {tracker.total_frames} frames)')

    # Summary table
    print(f'\n{"="*60}')
    print(f'  MULTISHINE DELAY TEST RESULTS')
    print(f'{"="*60}')
    print(f'  {"Delay":>5}  {"Shines":>8}  {"Jumps":>8}  '
          f'{"Shines/s":>9}  {"Frames":>8}')
    print(f'  {"─"*5}  {"─"*8}  {"─"*8}  {"─"*9}  {"─"*8}')

    baseline_shines = None
    for delay in delays:
        t = results[delay]
        shines_per_sec = t.shine_count / (t.total_frames / 60) if t.total_frames else 0
        marker = ''
        if baseline_shines is None:
            baseline_shines = t.shine_count
        elif baseline_shines > 0:
            pct = t.shine_count / baseline_shines * 100
            marker = f'  ({pct:.0f}% of delay=0)'
        print(f'  {delay:>5}  {t.shine_count:>8}  {t.jump_count:>8}  '
              f'{shines_per_sec:>9.2f}  {t.total_frames:>8}{marker}')

    # Recommendation
    print()
    if len(delays) > 1:
        best = max(delays, key=lambda d: results[d].shine_count)
        print(f'  Best delay for multishine consistency: {best}')
        if baseline_shines and baseline_shines > 0:
            for delay in delays[1:]:
                drop = (1 - results[delay].shine_count / baseline_shines) * 100
                if drop > 5:
                    print(f'  WARNING: delay={delay} drops {drop:.1f}% of shines '
                          f'vs delay=0')
    print()


if __name__ == '__main__':
    main()
