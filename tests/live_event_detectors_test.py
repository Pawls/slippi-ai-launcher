"""Detector unit tests.

Feed synthetic ``(port, action, frame)`` streams and assert the right
events emit at the right frames under sliding-window / cooldown /
family-transition edge cases. No gamestate or Dolphin needed.
"""

import unittest

from slippi_ai.live_events.detectors import (
    LiveEvent,
    ShieldGrabDetector,
    RollSpamDetector,
    LedgeCampDetector,
    SmashSpamDetector,
    build_detectors,
    DETECTOR_DEFAULTS,
)


# Action-state constants duplicated here so the tests are self-contained
# and would catch an accidental re-numbering in detectors.py.
SHIELD = 0xb3
SHIELD_STUN = 0xb5
GRAB = 0xd4
GRAB_RUNNING = 0xd6
ROLL_FORWARD = 0xe9
ROLL_BACKWARD = 0xea
EDGE_HANGING = 0xfd
EDGE_CATCHING = 0xfc
FSMASH_MID = 0x3c
FSMASH_HIGH = 0x3a
FSMASH_LOW = 0x3e
UPSMASH = 0x3f
DOWNSMASH = 0x40
WAIT = 0xe  # arbitrary non-relevant action
JAB = 0x2c  # arbitrary non-smash attack


def _feed_range(detector, port, action, start_frame, count):
    """Feed the same action for ``count`` consecutive frames. Returns any
    events emitted (there should normally be zero or one for this
    usage)."""
    out = []
    for f in range(start_frame, start_frame + count):
        ev = detector.feed(port, action, f)
        if ev is not None:
            out.append(ev)
    return out


def _hold(detector, port, action, frame, hold_frames=3):
    """Simulate entering an action and holding it for several frames —
    detectors must only count the first frame. Returns events emitted."""
    return _feed_range(detector, port, action, frame, hold_frames)


def _shield_grab(detector, port, frame, *, shield_hold=5, grab_hold=5):
    """A full SHIELD→GRAB gesture: shield for some frames, then grab.
    Returns any events emitted. The transition fires on the first grab
    frame."""
    out = []
    out += _feed_range(detector, port, SHIELD, frame, shield_hold)
    out += _feed_range(detector, port, GRAB, frame + shield_hold, grab_hold)
    return out


# ── Shield-grab spam ───────────────────────────────────────────────────

class ShieldGrabSpamTest(unittest.TestCase):

    def test_fires_on_fourth_transition_within_window(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        # 4 shield-grabs at 5-second intervals — all within a 30s window.
        results = []
        for i in range(4):
            results += _shield_grab(d, port=2, frame=i * 300)
        self.assertEqual(len(results), 1)
        ev = results[0]
        self.assertEqual(ev.type, "shield_grab_spam")
        self.assertEqual(ev.player_port, 2)
        self.assertGreaterEqual(ev.stats["count"], 4)

    def test_stays_silent_on_three(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        results = []
        for i in range(3):
            results += _shield_grab(d, port=2, frame=i * 300)
        self.assertEqual(results, [])

    def test_cooldown_suppresses_reemit(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        # Fire once.
        for i in range(4):
            _shield_grab(d, port=2, frame=i * 60)  # 4 in ~4 seconds
        # Add 3 more within cooldown — should stay silent because count
        # would exceed threshold again but we're in cooldown.
        more = []
        for i in range(3):
            more += _shield_grab(d, port=2, frame=400 + i * 30)
        self.assertEqual(more, [])

    def test_window_purges_old_transitions(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        # 3 shield-grabs at t=0,1,2s, then wait 40s, then 1 more. The
        # first three should age out — no fire on the 4th.
        for i in range(3):
            _shield_grab(d, port=2, frame=i * 60)
        results = _shield_grab(d, port=2, frame=40 * 60)
        self.assertEqual(results, [])

    def test_per_port_isolation(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        # Port 1 does 2 shield-grabs; port 2 does 4 — only port 2 fires.
        for i in range(2):
            _shield_grab(d, port=1, frame=i * 300)
        results = []
        for i in range(4):
            results += _shield_grab(d, port=2, frame=i * 300)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].player_port, 2)

    def test_ignores_non_shield_grab(self):
        d = ShieldGrabDetector(count=4, window_sec=30.0, cooldown_sec=30.0, fps=60)
        # Standing grabs without prior shield shouldn't count.
        results = []
        for i in range(5):
            results += _feed_range(d, port=2, action=WAIT, start_frame=i * 100, count=2)
            results += _feed_range(d, port=2, action=GRAB, start_frame=i * 100 + 2, count=5)
        self.assertEqual(results, [])


# ── Roll spam ──────────────────────────────────────────────────────────

class RollSpamTest(unittest.TestCase):

    def test_fires_on_sixth_roll(self):
        d = RollSpamDetector(count=6, window_sec=20.0, cooldown_sec=30.0, fps=60)
        results = []
        for i in range(6):
            # Alternate directions so each roll is a fresh transition.
            action = ROLL_FORWARD if i % 2 == 0 else ROLL_BACKWARD
            results += _hold(d, port=1, action=action, frame=i * 100, hold_frames=4)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].type, "roll_spam")
        self.assertGreaterEqual(results[0].stats["count"], 6)

    def test_held_roll_counts_once(self):
        d = RollSpamDetector(count=6, window_sec=20.0, cooldown_sec=30.0, fps=60)
        # One long forward-roll — should count as exactly one roll even
        # though the action persists for many frames.
        results = _feed_range(d, port=1, action=ROLL_FORWARD, start_frame=0, count=30)
        self.assertEqual(results, [])


# ── Ledge camp ─────────────────────────────────────────────────────────

class LedgeCampTest(unittest.TestCase):

    def test_fires_on_cumulative_dwell(self):
        d = LedgeCampDetector(dwell_sec=5.0, window_sec=15.0, cooldown_sec=60.0, fps=60)
        # Hang on ledge for 5s continuously → fire.
        results = _feed_range(d, port=1, action=EDGE_HANGING, start_frame=0, count=5 * 60)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].type, "ledge_camp")

    def test_brief_ledge_grab_silent(self):
        d = LedgeCampDetector(dwell_sec=5.0, window_sec=15.0, cooldown_sec=60.0, fps=60)
        # 1 second on ledge, then jump off.
        results = _feed_range(d, port=1, action=EDGE_HANGING, start_frame=0, count=60)
        results += _feed_range(d, port=1, action=WAIT, start_frame=60, count=600)
        self.assertEqual(results, [])

    def test_aggregates_across_multiple_visits(self):
        d = LedgeCampDetector(dwell_sec=5.0, window_sec=15.0, cooldown_sec=60.0, fps=60)
        # 2s on, 1s off, 2s on, 1s off, 2s on = 6s cumulative in 8s window.
        results = []
        frame = 0
        for on_secs, off_secs in [(2, 1), (2, 1), (2, 0)]:
            results += _feed_range(d, port=1, action=EDGE_HANGING, start_frame=frame, count=on_secs * 60)
            frame += on_secs * 60
            if off_secs:
                results += _feed_range(d, port=1, action=WAIT, start_frame=frame, count=off_secs * 60)
                frame += off_secs * 60
        self.assertEqual(len(results), 1)


# ── Smash spam ─────────────────────────────────────────────────────────

class SmashSpamTest(unittest.TestCase):

    def test_three_fsmashes_same_angle(self):
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        results = []
        for i in range(3):
            results += _feed_range(d, port=2, action=WAIT, start_frame=i * 120, count=30)
            results += _feed_range(d, port=2, action=FSMASH_MID, start_frame=i * 120 + 30, count=40)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].type, "smash_spam")
        self.assertEqual(results[0].stats["move"], "FSMASH")
        self.assertGreaterEqual(results[0].stats["count"], 3)

    def test_three_fsmashes_mixed_angles_all_fsmash_family(self):
        """FSMASH_HIGH → FSMASH_MID → FSMASH_LOW is still 3 FSMASHes."""
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        results = []
        angles = [FSMASH_HIGH, FSMASH_MID, FSMASH_LOW]
        for i, a in enumerate(angles):
            results += _feed_range(d, port=2, action=WAIT, start_frame=i * 120, count=30)
            results += _feed_range(d, port=2, action=a, start_frame=i * 120 + 30, count=40)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].stats["move"], "FSMASH")

    def test_different_family_resets_counter(self):
        """FSMASH, FSMASH, UPSMASH, FSMASH — no event (counter reset)."""
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        seq = [FSMASH_MID, FSMASH_MID, UPSMASH, FSMASH_MID]
        results = []
        for i, a in enumerate(seq):
            results += _feed_range(d, port=2, action=WAIT, start_frame=i * 120, count=30)
            results += _feed_range(d, port=2, action=a, start_frame=i * 120 + 30, count=40)
        self.assertEqual(results, [])

    def test_non_smash_actions_do_not_reset(self):
        """FSMASH, jab, FSMASH, roll, FSMASH still fires — non-smash
        actions between don't reset the consecutive counter."""
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        seq = [FSMASH_MID, JAB, FSMASH_MID, ROLL_FORWARD, FSMASH_MID]
        results = []
        for i, a in enumerate(seq):
            results += _feed_range(d, port=2, action=a, start_frame=i * 60, count=30)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].stats["move"], "FSMASH")

    def test_gap_expires_resets_counter(self):
        """Two FSMASHes, 11s idle, one FSMASH → no event."""
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        results = []
        results += _feed_range(d, port=2, action=FSMASH_MID, start_frame=0, count=40)
        results += _feed_range(d, port=2, action=WAIT, start_frame=40, count=60)
        results += _feed_range(d, port=2, action=FSMASH_MID, start_frame=100, count=40)
        # 11 seconds of idle (660 frames) — past the 10s gap threshold.
        results += _feed_range(d, port=2, action=WAIT, start_frame=140, count=11 * 60)
        results += _feed_range(d, port=2, action=FSMASH_MID, start_frame=140 + 11 * 60, count=40)
        self.assertEqual(results, [])

    def test_three_upsmashes(self):
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        results = []
        for i in range(3):
            results += _feed_range(d, port=1, action=WAIT, start_frame=i * 120, count=30)
            results += _feed_range(d, port=1, action=UPSMASH, start_frame=i * 120 + 30, count=40)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].stats["move"], "UPSMASH")

    def test_three_downsmashes(self):
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        results = []
        for i in range(3):
            results += _feed_range(d, port=1, action=WAIT, start_frame=i * 120, count=30)
            results += _feed_range(d, port=1, action=DOWNSMASH, start_frame=i * 120 + 30, count=40)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].stats["move"], "DOWNSMASH")

    def test_cooldown_suppresses_next_three(self):
        """After firing, 3 more FSMASHes within cooldown — should not
        re-fire."""
        d = SmashSpamDetector(count=3, gap_sec=10.0, cooldown_sec=30.0, fps=60)
        # First trigger.
        for i in range(3):
            _feed_range(d, port=2, action=WAIT, start_frame=i * 120, count=30)
            _feed_range(d, port=2, action=FSMASH_MID, start_frame=i * 120 + 30, count=40)
        # Next 3 FSMASHes within cooldown.
        results = []
        base = 3 * 120 + 60
        for i in range(3):
            results += _feed_range(d, port=2, action=WAIT, start_frame=base + i * 120, count=30)
            results += _feed_range(d, port=2, action=FSMASH_MID, start_frame=base + i * 120 + 30, count=40)
        self.assertEqual(results, [])


# ── Factory ────────────────────────────────────────────────────────────

class BuildDetectorsTest(unittest.TestCase):

    def test_builds_all_four_by_default(self):
        detectors = build_detectors()
        self.assertEqual(len(detectors), 4)
        types = {type(d).__name__ for d in detectors}
        self.assertEqual(types, {
            "ShieldGrabDetector", "RollSpamDetector",
            "LedgeCampDetector", "SmashSpamDetector",
        })

    def test_disabled_detector_not_built(self):
        detectors = build_detectors({"smash_spam": {"enabled": False}})
        self.assertEqual(len(detectors), 3)
        self.assertNotIn("SmashSpamDetector", {type(d).__name__ for d in detectors})

    def test_config_overrides_thresholds(self):
        detectors = build_detectors({
            "shield_grab_spam": {"count": 2, "window_sec": 5.0},
        })
        sg = next(d for d in detectors if type(d).__name__ == "ShieldGrabDetector")
        self.assertEqual(sg.count, 2)
        self.assertEqual(sg.window_sec, 5.0)

    def test_unknown_config_key_ignored(self):
        # Should not crash or raise.
        detectors = build_detectors({"nonexistent_detector": {"foo": "bar"}})
        self.assertEqual(len(detectors), 4)

    def test_defaults_are_complete(self):
        """Every detector name used by build_detectors must have an entry
        in DETECTOR_DEFAULTS, otherwise a disabled-by-default scenario
        would crash on KeyError."""
        self.assertEqual(
            set(DETECTOR_DEFAULTS.keys()),
            {"shield_grab_spam", "roll_spam", "ledge_camp", "smash_spam"},
        )


# ── LiveEvent dataclass ────────────────────────────────────────────────

class LiveEventTest(unittest.TestCase):

    def test_to_dict_roundtrip(self):
        ev = LiveEvent(
            type="shield_grab_spam",
            frame=3840,
            player_port=2,
            stats={"count": 4, "window_sec": 30.0},
            text_hint="4 shield-grabs in 30s",
        )
        d = ev.to_dict()
        self.assertEqual(d["type"], "shield_grab_spam")
        self.assertEqual(d["frame"], 3840)
        self.assertEqual(d["player_port"], 2)
        self.assertEqual(d["stats"]["count"], 4)
        self.assertEqual(d["severity"], "medium")
        # Ensure stats is a copy, not a reference.
        d["stats"]["count"] = 999
        self.assertEqual(ev.stats["count"], 4)


if __name__ == "__main__":
    unittest.main()
