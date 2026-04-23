"""Observer threading + dispatch tests.

Run the observer against an in-process HTTP server so we can verify the
producer-consumer handoff, queue backpressure (drop-oldest), and the
stop() flush semantics end-to-end without touching the real launcher.
"""

import http.server
import json
import socketserver
import threading
import time
import unittest
from typing import List

from slippi_ai.live_events.observer import LiveEventObserver


# Action state values duplicated from detectors.py so the test is
# self-contained and would catch accidental re-numbering.
SHIELD = 0xb3
GRAB = 0xd4
WAIT = 0xe


class _CollectingHandler(http.server.BaseHTTPRequestHandler):
    """Collects POST bodies into the server's ``received`` list."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {"_raw": body.decode("utf-8", errors="replace")}
        self.server.received.append(payload)  # type: ignore[attr-defined]
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):  # silence default stderr log
        pass


def _start_server() -> tuple[socketserver.TCPServer, str, List[dict]]:
    received: List[dict] = []
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _CollectingHandler)
    httpd.received = received  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}/bot/live-event", received


def _fire_shield_grab(obs: LiveEventObserver, port: int, base_frame: int) -> None:
    """Simulate one SHIELD→GRAB gesture as the main loop would: push
    several shield frames, then grab frames. Only the first grab frame
    triggers the detector's transition count."""
    for i in range(5):
        obs.push_nowait(port, SHIELD, base_frame + i)
    for i in range(5):
        obs.push_nowait(port, GRAB, base_frame + 5 + i)


class ObserverTest(unittest.TestCase):

    def setUp(self):
        self.httpd, self.url, self.received = _start_server()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _drain_for(self, seconds: float):
        """Sleep briefly so the worker thread can drain the queue and
        dispatch any pending events."""
        time.sleep(seconds)

    def test_dispatches_shield_grab_spam_event(self):
        obs = LiveEventObserver(
            endpoint_url=self.url,
            detector_config={
                # Shrink the window/count so the test triggers quickly.
                "shield_grab_spam": {
                    "count": 3, "window_sec": 60.0, "cooldown_sec": 60.0,
                },
                # Disable the others to keep the expected event count
                # deterministic.
                "roll_spam": {"enabled": False},
                "ledge_camp": {"enabled": False},
                "smash_spam": {"enabled": False},
            },
        )
        self.assertTrue(obs.start())
        # Three shield-grabs, 300 frames apart.
        for i in range(3):
            _fire_shield_grab(obs, port=2, base_frame=i * 300)
        obs.stop(timeout=3.0)

        self.assertGreaterEqual(len(self.received), 1)
        ev = self.received[0]
        self.assertEqual(ev["type"], "shield_grab_spam")
        self.assertEqual(ev["player_port"], 2)
        self.assertGreaterEqual(ev["stats"]["count"], 3)

    def test_no_detectors_enabled_means_observer_inert(self):
        obs = LiveEventObserver(
            endpoint_url=self.url,
            detector_config={
                "shield_grab_spam": {"enabled": False},
                "roll_spam": {"enabled": False},
                "ledge_camp": {"enabled": False},
                "smash_spam": {"enabled": False},
            },
        )
        self.assertFalse(obs.start())
        # push_nowait must be a cheap no-op when the thread never started.
        for i in range(1000):
            obs.push_nowait(2, SHIELD, i)
        obs.stop()  # must not hang or raise
        self.assertEqual(self.received, [])

    def test_push_nowait_never_blocks_under_backpressure(self):
        """Fill the queue faster than a slow-responding dispatch can
        drain it; pushes must never block and the main loop must keep
        going at full speed."""

        # Use a server that deliberately stalls to back the queue up.
        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(0.5)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a, **k):
                pass

        slow_httpd = socketserver.TCPServer(("127.0.0.1", 0), SlowHandler)
        slow_port = slow_httpd.server_address[1]
        t = threading.Thread(target=slow_httpd.serve_forever, daemon=True)
        t.start()
        try:
            obs = LiveEventObserver(
                endpoint_url=f"http://127.0.0.1:{slow_port}/bot/live-event",
                queue_maxsize=8,
                detector_config={
                    # Only shield-grab enabled, and tight threshold so
                    # the worker has events to dispatch (stalling on the
                    # slow server). Meanwhile we're slamming push_nowait.
                    "shield_grab_spam": {
                        "count": 2, "window_sec": 60.0, "cooldown_sec": 0.1,
                    },
                    "roll_spam": {"enabled": False},
                    "ledge_camp": {"enabled": False},
                    "smash_spam": {"enabled": False},
                },
            )
            obs.start()

            # Push 5000 frames as fast as possible. None of these should
            # take more than ~microseconds — push_nowait must not block.
            start = time.monotonic()
            for i in range(5000):
                # Alternate shield and grab so many transitions fire.
                obs.push_nowait(
                    2, SHIELD if (i // 10) % 2 == 0 else GRAB, i)
            elapsed = time.monotonic() - start
            # 5000 non-blocking put_nowaits should complete in well under
            # 100ms even on a slow CI box. If this fails we've broken the
            # fundamental perf guarantee.
            self.assertLess(elapsed, 0.5,
                f"push_nowait was too slow: {elapsed:.3f}s for 5000 pushes")
            obs.stop(timeout=1.0)
        finally:
            slow_httpd.shutdown()
            slow_httpd.server_close()


if __name__ == "__main__":
    unittest.main()
