"""Bot quit-sequence helpers in scripts/netplay.py.

Covers the unified shutdown path added when the End-Match sentinel was
wired up:

- Chat-pair constants (guard against direction swaps).
- Win32 HWND enumeration fallbacks on non-Windows / missing-PID paths.
- ``_close_dolphin_window_esc`` defensive behavior when the dolphin
  object has no subprocess.
- ``_dolphin_quit_sequence`` frame accounting + Z-press ordering using
  a fake controller that records every call.

Imports ``scripts.netplay`` directly — the module also pulls in the
ML training stack, which is slow to load (~3s) but has no effect on
these unit-level assertions.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import melee

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import netplay  # noqa: E402  — path fixup must come first


# ── Chat constants ────────────────────────────────────────────────────

class ChatConstantsTest(unittest.TestCase):
    """Guard against accidental direction swaps in the chat pairs.

    Slippi's default quickchat is directional — the right pair matters
    for the heckle to land on the intended message.
    """

    def test_gotta_go_is_down_then_up(self):
        self.assertEqual(
            netplay._CHAT_GOTTA_GO,
            (melee.Button.BUTTON_D_DOWN, melee.Button.BUTTON_D_UP),
        )

    def test_ggs_is_up_up(self):
        self.assertEqual(
            netplay._CHAT_GGS,
            (melee.Button.BUTTON_D_UP, melee.Button.BUTTON_D_UP),
        )

    def test_sorry_is_right_up(self):
        self.assertEqual(
            netplay._CHAT_SORRY,
            (melee.Button.BUTTON_D_RIGHT, melee.Button.BUTTON_D_UP),
        )

    def test_too_good_is_left_down(self):
        self.assertEqual(
            netplay._CHAT_TOO_GOOD,
            (melee.Button.BUTTON_D_LEFT, melee.Button.BUTTON_D_DOWN),
        )


# ── Win32 helpers ─────────────────────────────────────────────────────

class DolphinWindowHandlesTest(unittest.TestCase):

    @unittest.skipIf(sys.platform != "win32", "Windows-only helper")
    def test_nonexistent_pid_returns_empty(self):
        # PID 0 is reserved on Windows (System Idle Process); no visible
        # top-level window should report it as its owner.
        self.assertEqual(netplay._dolphin_window_handles(0), [])

    def test_non_win32_returns_empty(self):
        with mock.patch.object(netplay.sys, "platform", "linux"):
            self.assertEqual(netplay._dolphin_window_handles(12345), [])


class CloseDolphinWindowEscTest(unittest.TestCase):

    def test_missing_process_attr_does_not_raise(self):
        class _NoProc:
            pass
        # Expected path: log + silent return. Would previously raise
        # AttributeError if we touched ``_process.pid`` without guarding.
        netplay._close_dolphin_window_esc(_NoProc())

    def test_non_win32_platform_is_noop(self):
        # Fake dolphin with a valid-looking _process; helper should still
        # short-circuit because we're not on Windows.
        class _FakeProc:
            pid = 99999
        class _FakeDolphin:
            _process = _FakeProc()
        with mock.patch.object(netplay.sys, "platform", "darwin"):
            # Must not call into ctypes.windll (which doesn't exist on
            # non-Windows) — if the short-circuit is removed, this
            # would AttributeError on darwin/linux test runs.
            netplay._close_dolphin_window_esc(_FakeDolphin())


# ── Quit sequence frame accounting ────────────────────────────────────

class _FakeController:
    """Records every interaction in order for later assertion."""

    def __init__(self):
        self.calls: list[tuple] = []

    def press_button(self, button):
        self.calls.append(("press", button))

    def release_all(self):
        self.calls.append(("release",))

    def flush(self):
        self.calls.append(("flush",))


class _FakeDolphin:
    def __init__(self, controller):
        self.controllers = {1: controller}
        self.frames_advanced = 0
        self._process = None  # _close_dolphin_window_esc bails here

    def next_gamestate(self):
        self.frames_advanced += 1
        return None


class DolphinQuitSequenceTest(unittest.TestCase):

    def setUp(self):
        self.controller = _FakeController()
        self.dolphin = _FakeDolphin(self.controller)

    def test_total_frames_match_plan(self):
        # Plan: tap(12) + idle(30) + hold(120) + idle(180) = 342 frames.
        netplay._dolphin_quit_sequence(self.dolphin, 1)
        self.assertEqual(self.dolphin.frames_advanced, 342)

    def test_z_pressed_for_tap_plus_hold_only(self):
        netplay._dolphin_quit_sequence(self.dolphin, 1)
        z_presses = sum(
            1 for c in self.controller.calls
            if c == ("press", melee.Button.BUTTON_Z)
        )
        # tap_z_frames (12) + long_z_hold_frames (120) = 132 presses.
        self.assertEqual(z_presses, 132)

    def test_only_z_is_ever_pressed(self):
        netplay._dolphin_quit_sequence(self.dolphin, 1)
        for tag, *rest in self.controller.calls:
            if tag == "press":
                self.assertEqual(rest[0], melee.Button.BUTTON_Z)

    def test_sequence_order_tap_then_idle_then_hold_then_idle(self):
        # Slice the press frames by their first-occurrence index so we
        # can verify the tap block comes before the hold block with an
        # idle gap between them.
        netplay._dolphin_quit_sequence(self.dolphin, 1)
        press_indexes = [
            i for i, c in enumerate(self.controller.calls)
            if c == ("press", melee.Button.BUTTON_Z)
        ]
        self.assertEqual(len(press_indexes), 132)
        # First 12 are the tap, last 120 are the hold.
        tap_end = press_indexes[11]
        hold_start = press_indexes[12]
        # The 30-frame idle sits between the tap and the hold. Each
        # idle frame contributes [release, flush] (from advance) plus
        # one leading release — so the gap in raw-call indexes is
        # bounded below by 30 * 2 = 60.
        self.assertGreaterEqual(hold_start - tap_end, 60)

    def test_ends_with_release_and_flush_before_esc(self):
        netplay._dolphin_quit_sequence(self.dolphin, 1)
        # Final three entries are the explicit controller cleanup:
        # release_all + flush, sitting just before _close_dolphin_window_esc.
        self.assertEqual(self.controller.calls[-2], ("release",))
        self.assertEqual(self.controller.calls[-1], ("flush",))

    def test_calls_close_dolphin_window_esc_at_end(self):
        # Monkey-patch the close helper so we can observe it fires
        # exactly once, after the controller sequence completes.
        with mock.patch.object(
                netplay, "_close_dolphin_window_esc") as close_mock:
            netplay._dolphin_quit_sequence(self.dolphin, 1)
        close_mock.assert_called_once_with(self.dolphin)


if __name__ == "__main__":
    unittest.main()
