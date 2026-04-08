"""Replay browser API: scan and query Slippi replay metadata."""

import threading

from fastapi import APIRouter

from LAUNCHER.api.app import get_state

router = APIRouter(prefix="/replays", tags=["replays"])

# Track background scan state
_scan_lock = threading.Lock()
_scan_progress: dict = {"running": False, "current": 0, "total": 0}
_cancel_event: threading.Event | None = None


@router.get("/")
def list_replays():
    """Return cached replay metadata without rescanning."""
    return get_state().replay_store.get_cached()


@router.post("/scan")
def start_scan(detect_box: bool = False, scan_dir: str = ""):
    """Trigger a background replay scan. Returns immediately.

    scan_dir: override directory to scan. Falls back to config replays_dir.
    """
    global _cancel_event

    s = get_state()
    replays_dir = scan_dir or s.cfg.get("paths", "replays_dir")
    if not replays_dir:
        return {"error": "replays_dir not configured"}, 400

    with _scan_lock:
        if _scan_progress["running"]:
            return {"status": "already_running"}
        _scan_progress["running"] = True
        _scan_progress["current"] = 0
        _scan_progress["total"] = 0
        _cancel_event = threading.Event()

    cancel_ev = _cancel_event

    def _do_scan():
        def _progress(current, total):
            _scan_progress["current"] = current
            _scan_progress["total"] = total

        try:
            s.replay_store.scan(
                replays_dir,
                progress_cb=_progress,
                detect_box=detect_box,
                cancel_event=cancel_ev,
            )
        finally:
            _scan_progress["running"] = False

    threading.Thread(target=_do_scan, daemon=True).start()
    return {"status": "started"}


@router.post("/scan/cancel")
def cancel_scan():
    """Cancel a running scan."""
    global _cancel_event
    if _cancel_event and _scan_progress["running"]:
        _cancel_event.set()
        return {"status": "cancelled"}
    return {"status": "not_running"}


@router.get("/scan/status")
def scan_status():
    """Check the progress of a background scan."""
    return dict(_scan_progress)


@router.get("/has-input-type")
def has_input_type():
    """Check if cached replays have input type (box/GCC) data."""
    return {"has_data": get_state().replay_store.has_input_type_data()}
