"""Replay browser API: scan, query, play, delete, and upgrade Slippi replays."""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.config import ensure_executable

router = APIRouter(prefix="/replays", tags=["replays"])

# Track background scan state
_scan_lock = threading.Lock()
_scan_progress: dict = {"running": False, "current": 0, "total": 0}
_cancel_event: threading.Event | None = None

# Track background upgrade state
_upgrade_lock = threading.Lock()
_upgrade_progress: dict = {
    "running": False,
    "current": 0,
    "total": 0,
    "filename": "",
    "upgraded": 0,
    "errors": [],
    "done": False,
    "cancelled": False,
}
_upgrade_cancel: threading.Event | None = None


def _find_dolphin_exe(dolphin_dir: str) -> str | None:
    """Locate the Dolphin executable in the given directory."""
    if not dolphin_dir:
        return None
    d = Path(dolphin_dir)
    for name in ("Slippi Dolphin.exe", "dolphin-emu", "Dolphin.exe"):
        candidate = d / name
        if candidate.exists():
            return str(candidate)
    # Linux: AppImage fallback
    try:
        for f in sorted(d.iterdir()):
            if f.suffix == ".AppImage" and f.is_file():
                return str(f)
    except OSError:
        pass
    return None


def _find_playback_dolphin_exe(configured_dir: str) -> str | None:
    """Locate the Slippi *playback* Dolphin build.

    Slippi Launcher installs two side-by-side builds:
        <launcher>/netplay/Slippi Dolphin.exe   - online play
        <launcher>/playback/Slippi Dolphin.exe  - replay playback

    Replays only work in the playback build (the netplay build rejects SLPs
    as "invalid recording files"). Most users have ``dolphin_dir`` pointed at
    the netplay folder, so look for the playback sibling first.
    """
    if not configured_dir:
        return None
    d = Path(configured_dir)
    # Already a playback dir.
    if d.name.lower() == "playback":
        return _find_dolphin_exe(str(d))
    # Sibling of netplay.
    if d.name.lower() == "netplay":
        sibling = d.parent / "playback"
        if sibling.is_dir():
            return _find_dolphin_exe(str(sibling))
    # Some installs nest playback inside dolphin_dir itself.
    nested = d / "playback"
    if nested.is_dir():
        return _find_dolphin_exe(str(nested))
    return None


# Error returned when no Slippi Playback Dolphin can be located. The frontend
# keys off ``kind`` to surface a "Configure in Settings" hint instead of a
# generic toast.
_PLAYBACK_REQUIRED_MSG = (
    "Slippi Playback Dolphin is required for this action. The netplay build "
    "of Dolphin doesn't support replay playback or upgrades. Set the "
    '"Slippi Playback Dolphin folder" in Settings.'
)


def _resolve_upgrade_slp_dolphin_exe(cfg) -> tuple[str | None, str | None]:
    """Return ``(exe_path, error_message)`` for the SLP-upgrade Dolphin.

    Prefers the dedicated ``upgrade_slp_dolphin`` setting (a file path to
    vladfi1's special playback build). If unset, falls back to the regular
    playback Dolphin resolution used for replay playback.
    """
    configured = cfg.get("paths", "upgrade_slp_dolphin")
    if configured:
        if os.path.isfile(configured):
            ensure_executable(configured)
            return configured, None
        return None, (
            f'Configured Upgrade SLP Dolphin executable "{configured}" does '
            "not exist. Update it in Settings."
        )
    return _resolve_playback_dolphin_exe(cfg)


def _resolve_playback_dolphin_exe(cfg) -> tuple[str | None, str | None]:
    """Return ``(exe_path, error_message)`` for the playback Dolphin.

    Resolution order:
      1. ``paths.playback_dolphin_dir`` from config (manually set or
         auto-detected and saved by the user).
      2. Sibling/nested ``playback`` derived from ``paths.dolphin_dir``,
         covering users who only configured the netplay folder and have
         the standard Slippi Launcher layout on disk.

    The netplay Dolphin is **not** used as a fallback — invoking it with
    ``-i`` produces an opaque "Unknown option 'i'" dialog from Dolphin
    itself, which is exactly what we're trying to avoid.
    """
    configured = cfg.get("paths", "playback_dolphin_dir")
    if configured:
        exe = _find_dolphin_exe(configured)
        if exe:
            ensure_executable(exe)
            return exe, None
        return None, (
            f'Configured Slippi Playback Dolphin folder "{configured}" does '
            "not contain a Dolphin executable. Update it in Settings."
        )

    dolphin_dir = cfg.get("paths", "dolphin_dir")
    if dolphin_dir:
        exe = _find_playback_dolphin_exe(dolphin_dir)
        if exe:
            ensure_executable(exe)
            return exe, None

    return None, _PLAYBACK_REQUIRED_MSG


def _replay_needs_upgrade(r: dict) -> bool:
    """Check if a cached replay dict needs upgrading.

    Mirrors slippi_db.upgrade_slp.needs_upgrade() but works on the
    metadata dict produced by ReplayStore rather than a peppi_py Game.
    """
    version_str = r.get("slippi_version")
    if not version_str:
        return False
    try:
        parts = tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return False

    if parts < (3, 2, 0):
        return True

    if parts < (3, 18, 0):
        stage = r.get("stage", "")
        if stage == "Fountain of Dreams":
            return True
        if stage == "Pokemon Stadium" and not r.get("is_frozen_ps", True):
            return True

    return False


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


# ── Replay actions ────────────────────────────────────────────────────────


class PlayRequest(BaseModel):
    path: str


@router.post("/play")
def play_replay(body: PlayRequest):
    """Launch a single replay in Slippi Playback Dolphin."""
    slp_path = body.path
    if not slp_path or not os.path.isfile(slp_path):
        return {"error": "Replay file not found."}

    cfg = get_state().cfg

    iso_path = cfg.get("paths", "iso")
    if not iso_path:
        return {"error": "Melee ISO path not configured. Set it in Settings."}

    dolphin_exe, err = _resolve_playback_dolphin_exe(cfg)
    if not dolphin_exe:
        return {"error": err, "kind": "playback_required"}

    # Slippi Playback Dolphin reads its replay queue from a JSON "comm file"
    # passed via -i. The legacy -m flag points at .dtm movies and rejects
    # .slp files as invalid recordings.
    comm = {
        "mode": "normal",
        "replay": slp_path,
        "isRealTimeMode": False,
        "outputOverlayFiles": False,
    }
    try:
        fd, comm_path = tempfile.mkstemp(suffix=".json", prefix="slippi_comm_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(comm, f)
    except OSError as exc:
        return {"error": f"Failed to create comm file: {exc}"}

    cmd = [dolphin_exe, "-i", comm_path, "-b", "-e", iso_path]
    try:
        if sys.platform == "win32":
            subprocess.Popen(cmd)
        else:
            subprocess.Popen(cmd, start_new_session=True)
    except Exception as exc:
        return {"error": f"Launch failed: {exc}"}

    return {"ok": True}


class OpenFolderRequest(BaseModel):
    path: str


@router.post("/open-folder")
def open_folder(body: OpenFolderRequest):
    """Open the folder containing the given replay in the OS file explorer."""
    folder = os.path.dirname(body.path)
    if not folder or not os.path.isdir(folder):
        return {"error": "Folder not found."}
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as exc:
        return {"error": f"Failed to open folder: {exc}"}
    return {"ok": True}


class DeleteRequest(BaseModel):
    paths: list[str]


@router.post("/delete")
def delete_replays(body: DeleteRequest):
    """Permanently delete the given replay files and remove them from cache."""
    deleted: list[str] = []
    failed: list[dict] = []
    for p in body.paths:
        if not p or not os.path.isfile(p):
            failed.append({"path": p, "error": "not found"})
            continue
        try:
            os.remove(p)
            deleted.append(p)
        except OSError as e:
            failed.append({"path": p, "error": str(e)})
    if deleted:
        get_state().replay_store.remove(deleted)
    return {"deleted": len(deleted), "failed": failed}


# ── Upgrade ───────────────────────────────────────────────────────────────


class UpgradeRequest(BaseModel):
    paths: list[str]
    backup: bool = True
    backup_dir: str = ""
    # Optional override: when set, skips the probe and forces this value
    # for `--platform headless` support. Pass `False` to opt into legacy
    # batch-mode after the user acknowledges a probe failure.
    supports_platform_headless: bool | None = None


@router.post("/upgrade/check")
def check_upgradable():
    """Return paths of cached replays that need upgrading."""
    cached = get_state().replay_store.get_cached()
    upgradable = [r["path"] for r in cached if _replay_needs_upgrade(r)]
    return {"paths": upgradable, "count": len(upgradable)}


@router.post("/upgrade/probe")
def probe_upgrade_dolphin():
    """Probe the configured upgrade Dolphin to determine whether it
    supports ``--platform headless``.

    The frontend should call this before ``/upgrade/start`` so it can
    prompt the user when the probe fails, rather than silently falling
    back to legacy batch mode.
    """
    cfg = get_state().cfg
    dolphin_exe, err = _resolve_upgrade_slp_dolphin_exe(cfg)
    if not dolphin_exe:
        return {"error": err, "kind": "playback_required"}

    try:
        from slippi_db.upgrade_slp import probe_dolphin
    except Exception as e:
        return {
            "dolphin_path": dolphin_exe,
            "supports_platform_headless": None,
            "version": None,
            "probe_error": f"upgrade module unavailable: {e}",
        }

    result = probe_dolphin(dolphin_exe)
    return {
        "dolphin_path": dolphin_exe,
        "supports_platform_headless": (
            None if result.error else result.supports_platform_headless
        ),
        "version": result.version,
        "probe_error": result.error,
    }


def _reset_upgrade_progress():
    _upgrade_progress.update({
        "running": False,
        "current": 0,
        "total": 0,
        "filename": "",
        "upgraded": 0,
        "errors": [],
        "done": False,
        "cancelled": False,
    })


@router.post("/upgrade/start")
def start_upgrade(body: UpgradeRequest):
    """Kick off a background upgrade of the given replay paths."""
    global _upgrade_cancel

    if not body.paths:
        return {"error": "No replays selected for upgrade."}

    cfg = get_state().cfg
    iso_path = cfg.get("paths", "iso")
    if not iso_path:
        return {"error": "Melee ISO path not configured. Set it in Settings."}

    dolphin_exe, err = _resolve_upgrade_slp_dolphin_exe(cfg)
    if not dolphin_exe:
        return {"error": err, "kind": "playback_required"}

    backup_dir: Path | None = None
    if body.backup:
        if not body.backup_dir.strip():
            return {"error": "Backup folder required when backup is enabled."}
        backup_dir = Path(body.backup_dir.strip())
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"error": f"Cannot create backup folder: {e}"}

    with _upgrade_lock:
        if _upgrade_progress["running"]:
            return {"status": "already_running"}
        _reset_upgrade_progress()
        _upgrade_progress["running"] = True
        _upgrade_progress["total"] = len(body.paths)
        _upgrade_cancel = threading.Event()

    cancel_ev = _upgrade_cancel
    paths = list(body.paths)

    override_headless_support = body.supports_platform_headless

    def _do_upgrade():
        try:
            from slippi_db.upgrade_slp import (
                upgrade_slp, DolphinConfig, needs_upgrade, DolphinTimeoutError,
                probe_dolphin,
            )
            import peppi_py
        except Exception as e:
            _upgrade_progress["errors"].append(
                {"file": "<import>", "error": f"upgrade module unavailable: {e}"}
            )
            _upgrade_progress["running"] = False
            _upgrade_progress["done"] = True
            return

        # Probe once upfront so a detection failure aborts the whole run
        # with a clear error, instead of silently falling back to legacy
        # batch mode for every file. Callers that already handled a probe
        # failure (e.g. the user chose to proceed anyway) pass an explicit
        # override in the request.
        if override_headless_support is not None:
            supports_headless = override_headless_support
        else:
            probe = probe_dolphin(dolphin_exe)
            if probe.error is not None:
                _upgrade_progress["errors"].append(
                    {"file": "<probe>",
                     "error": f"Dolphin probe failed: {probe.error}"}
                )
                _upgrade_progress["running"] = False
                _upgrade_progress["done"] = True
                return
            supports_headless = probe.supports_platform_headless

        dolphin_config = DolphinConfig(
            dolphin_path=dolphin_exe,
            ssbm_iso_path=iso_path,
        )

        upgraded = 0
        for i, slp_path in enumerate(paths):
            if cancel_ev.is_set():
                _upgrade_progress["cancelled"] = True
                break

            _upgrade_progress["current"] = i
            _upgrade_progress["filename"] = os.path.basename(slp_path)

            if not slp_path or not os.path.isfile(slp_path):
                continue

            # Verify upgrade still needed
            try:
                game = peppi_py.read_slippi(slp_path, skip_frames=True)
                if not needs_upgrade(game):
                    continue
            except Exception:
                continue

            # Backup
            if backup_dir is not None:
                try:
                    dest = backup_dir / os.path.basename(slp_path)
                    if not dest.exists():
                        shutil.copy2(slp_path, dest)
                except Exception as e:
                    logging.warning("Backup failed for %s: %s", slp_path, e)
                    _upgrade_progress["errors"].append(
                        {"file": os.path.basename(slp_path),
                         "error": f"backup failed: {e}"}
                    )
                    continue

            # Upgrade to temp file, then replace original
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    output_path = os.path.join(tmp_dir, "upgraded.slp")
                    upgrade_slp(
                        slp_path, output_path, dolphin_config,
                        in_memory=(sys.platform != "win32"),
                        time_limit=60,
                        supports_platform_headless=supports_headless,
                    )

                    if not os.path.isfile(output_path):
                        _upgrade_progress["errors"].append(
                            {"file": os.path.basename(slp_path),
                             "error": "upgrade produced no output"}
                        )
                        continue

                    shutil.move(output_path, slp_path)
                    upgraded += 1
                    _upgrade_progress["upgraded"] = upgraded

            except DolphinTimeoutError:
                _upgrade_progress["errors"].append(
                    {"file": os.path.basename(slp_path),
                     "error": "Dolphin timed out"}
                )
            except Exception as e:
                _upgrade_progress["errors"].append(
                    {"file": os.path.basename(slp_path),
                     "error": str(e)}
                )

        _upgrade_progress["current"] = _upgrade_progress["total"]
        _upgrade_progress["done"] = True
        _upgrade_progress["running"] = False

    threading.Thread(target=_do_upgrade, daemon=True).start()
    return {"status": "started", "total": len(paths)}


@router.get("/upgrade/status")
def upgrade_status():
    """Return current upgrade progress."""
    return dict(_upgrade_progress)


@router.post("/upgrade/cancel")
def cancel_upgrade():
    """Request cancellation of the running upgrade."""
    global _upgrade_cancel
    if _upgrade_cancel and _upgrade_progress["running"]:
        _upgrade_cancel.set()
        return {"status": "cancelling"}
    return {"status": "not_running"}
