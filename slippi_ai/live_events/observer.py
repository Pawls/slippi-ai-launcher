"""Observer thread that consumes per-frame action-state tuples and
dispatches detected events to the launcher via loopback HTTP.

Lives inside the ``netplay.py`` subprocess. The main frame loop calls
``push_nowait(port, action, frame)`` directly after ``agent.step()`` —
this is a bounded-queue handoff that never blocks the hot path. A
single daemon worker thread drains the queue, runs every detector on
each tuple, and POSTs emitted events synchronously to the launcher.

The launcher attaches ``match_id`` and Discord context server-side, so
the event payload we send is intentionally minimal — the observer has
no business knowing match state.

Dispatch is synchronous inside the worker thread (not fire-and-forget)
so ``stop()`` can deterministically flush in-flight events before the
match-end sentinel is printed. Loopback HTTP on 127.0.0.1 is low
single-digit ms — the small dispatch latency is fine and the ordering
guarantee is worth it.

Design invariants:
- ``push_nowait`` never blocks, never raises, never touches I/O.
- Detectors run entirely in the worker thread, isolated from the frame
  loop's GIL turn.
- A failing detector or unreachable launcher cannot stall the worker or
  the game — all exceptions are logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Optional

from slippi_ai.live_events.detectors import (
    LiveEvent,
    build_detectors,
    DEFAULT_FPS,
)


_DEFAULT_QUEUE_MAXSIZE = 120  # ~2 seconds of frames at 60Hz
_DEFAULT_HTTP_TIMEOUT_SEC = 2.0
_DEFAULT_STOP_TIMEOUT_SEC = 3.0


class LiveEventObserver:
    """Owns the bounded queue, worker thread, and HTTP dispatch.

    Call ``start()`` once before the main loop, ``push_nowait`` every
    frame, and ``stop()`` exactly once before emitting the match-end
    sentinel. Re-starting a stopped observer is not supported.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        detector_config: Optional[dict] = None,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        http_timeout_sec: float = _DEFAULT_HTTP_TIMEOUT_SEC,
        fps: int = DEFAULT_FPS,
    ):
        self._endpoint_url = endpoint_url
        self._http_timeout = http_timeout_sec
        self._queue: "queue.Queue[Optional[tuple[int, int, int]]]" = queue.Queue(
            maxsize=queue_maxsize,
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._detectors = build_detectors(detector_config, fps=fps)
        self._drops = 0
        self._drop_logged = False
        self._dispatched = 0
        self._errors = 0

    @property
    def detector_count(self) -> int:
        return len(self._detectors)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spin up the worker thread. Returns True if started, False if
        there are no detectors to run (in which case ``push_nowait`` is
        a no-op and the hot-path cost stays at zero)."""
        if self._thread is not None:
            return True
        if not self._detectors:
            logging.info(
                "[live-events] no detectors enabled; observer inert")
            return False
        self._thread = threading.Thread(
            target=self._run,
            name="live-events-observer",
            daemon=True,
        )
        self._thread.start()
        logging.info(
            "[live-events] observer started: endpoint=%s detectors=%s",
            self._endpoint_url,
            [type(d).__name__ for d in self._detectors],
        )
        return True

    def stop(self, timeout: float = _DEFAULT_STOP_TIMEOUT_SEC) -> None:
        """Signal the worker to drain and exit, then join with a timeout.

        Called from the main thread just before ``[GAME_RESULT]`` is
        printed, so in-flight events land in the launcher's ring buffer
        before the match-end taunt fires. If ``timeout`` elapses with
        items still unprocessed, they are lost — acceptable because
        detectors are lossy by design and we can't block subprocess exit.
        """
        if self._thread is None:
            return
        self._stop_event.set()
        # Unblock the worker's queue.get() quickly.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        logging.info(
            "[live-events] observer stopped: dispatched=%d errors=%d drops=%d",
            self._dispatched, self._errors, self._drops,
        )
        self._thread = None

    # ── Producer (main thread) ─────────────────────────────────────────

    def push_nowait(self, port: int, action: int, frame: int) -> None:
        """Called every in-game frame from the main loop. Never blocks;
        drops oldest on full and logs at most once per match."""
        if self._thread is None:
            return
        try:
            self._queue.put_nowait((port, action, frame))
            return
        except queue.Full:
            pass
        # Make room by dropping the oldest tuple, then retry. If the
        # retry also fails (vanishingly rare — a racing stop()), just
        # drop this frame.
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait((port, action, frame))
        except queue.Full:
            pass
        self._drops += 1
        if not self._drop_logged:
            logging.warning(
                "[live-events] queue full — dropping oldest frames "
                "(will only log once this match)")
            self._drop_logged = True

    # ── Worker (daemon thread) ─────────────────────────────────────────

    def _run(self) -> None:
        while True:
            # Exit condition: stop was signaled AND the queue is empty.
            # Draining before exit ensures late events still fire.
            if self._stop_event.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                # Wakeup-only sentinel from stop(). Loop back to check
                # the exit condition.
                continue
            port, action, frame = item
            for det in self._detectors:
                try:
                    ev = det.feed(port, action, frame)
                except Exception as e:  # noqa: BLE001
                    self._errors += 1
                    logging.warning(
                        "[live-events] %s raised %s — continuing",
                        type(det).__name__, e)
                    continue
                if ev is None:
                    continue
                self._dispatch(ev)

    # ── Dispatch ───────────────────────────────────────────────────────

    def _dispatch(self, ev: LiveEvent) -> None:
        """Synchronous POST to the launcher endpoint. Errors are logged
        and swallowed — an unreachable launcher must not stall the
        worker."""
        payload = ev.to_dict()
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as e:
            self._errors += 1
            logging.warning("[live-events] encode failed: %s", e)
            return
        req = urllib.request.Request(
            self._endpoint_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
                if resp.status >= 400:
                    self._errors += 1
                    logging.warning(
                        "[live-events] %s returned %s",
                        self._endpoint_url, resp.status)
                else:
                    self._dispatched += 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._errors += 1
            logging.warning(
                "[live-events] dispatch to %s failed: %s",
                self._endpoint_url, e)
