"""Shared netplay launch orchestration.

Used by both ``POST /play/launch/netplay`` (from the GUI's Play page) and
``POST /bot/launch`` (from the Discord-bot command surface). Keeps the
subprocess command shape identical between the two entry points so a match
spawned by the bot is indistinguishable from one spawned by the GUI button.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from LAUNCHER.api.training import process_manager
from LAUNCHER.config import (
    find_script,
    gecko_codes_path,
    load_gecko_codes_text,
)


_END_MATCH_SENTINEL_NAME = ".end_match"
_SERIES_STATE_NAME = ".bot_series_state"


def end_match_sentinel_path(cfg) -> Path:
    """Shared location for the end-match sentinel. Lives in the user's
    Slippi replays directory when configured so it's already on a disk the
    user owns; otherwise falls back to the OS temp dir so unit-style
    launches (no Slippi installed) still work."""
    replays = (cfg.get("paths", "replays_dir") or "").strip()
    base = replays if replays and os.path.isdir(replays) else tempfile.gettempdir()
    return Path(base) / _END_MATCH_SENTINEL_NAME


def series_state_path(cfg) -> Path:
    """Shared location for the Bo5 series-state handshake file. Written
    by the launcher when the queue or game tally changes, polled by the
    netplay subprocess to decide when to fire "one more" between games
    and when to close out the series after the current game finishes.
    Same base directory as the end-match sentinel for symmetry."""
    replays = (cfg.get("paths", "replays_dir") or "").strip()
    base = replays if replays and os.path.isdir(replays) else tempfile.gettempdir()
    return Path(base) / _SERIES_STATE_NAME


def clear_end_match_sentinel(cfg) -> None:
    """Best-effort removal of a stale sentinel before launching a match.
    A leftover sentinel from a crashed prior run would otherwise make the
    next match quit itself in the first 10 frames. Also removes any
    stale series-state file so the next match doesn't inherit the last
    session's Bo5 flag."""
    for path in (end_match_sentinel_path(cfg), series_state_path(cfg)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def touch_end_match_sentinel(cfg, payload: str | None = None) -> Path:
    """Create the sentinel file so the running netplay subprocess sees it
    on its next poll. Returns the path actually written.

    If ``payload`` is provided it's written into the file verbatim —
    netplay.py's poller reads the body and parses it as JSON to pull
    optional chat-sequence instructions (e.g. ``{"chat": "sorry_ggs",
    "press_z": true}``). A bare touch (no payload) keeps the historical
    "exit, no taunt" semantics used by the GUI's End Match button.
    """
    path = end_match_sentinel_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        path.touch()
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def write_series_state(cfg, state: dict) -> Path:
    """Atomically overwrite the series-state handshake file.

    The subprocess polls this on a shallow cadence (once per menu frame)
    so the write is frequent but cheap — atomic replace avoids the
    reader ever seeing a half-written JSON object.
    """
    path = series_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    import json as _json
    tmp.write_text(_json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)
    return path


def clear_series_state(cfg) -> None:
    """Remove the series-state file between matches so a fresh match
    doesn't resurrect the prior session's Bo5 flag."""
    try:
        series_state_path(cfg).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


@dataclass
class LaunchResult:
    process_id: str | None
    status: str | None
    match_id: str | None
    error: str | None = None


def launch_netplay_session(
    *,
    cfg,
    match_store,
    agent_path: str,          # path as stored in agent record (may be relative)
    abs_agent_path: str,      # resolved absolute path for the subprocess
    agent_name: str = "",
    character: str = "",
    connect_code: str,
    delay: int = 2,
    auto_delay: bool = True,
    sample_temperature: float = 1.0,
    force_port: str = "",
    force_lan_ip: str = "",
    stage: str = "RANDOM_STAGE",
    fullscreen: bool = False,
    infinite_time: bool = False,
    save_replays: bool = True,
    disable_audio: bool = False,
    use_headless: bool = False,
    wrap_xvfb: bool = False,
    gfx_backend: str = "",
    audio_backend: str = "",
    # Dolphin internal resolution multiplier. 1 = native (640x528). Only
    # meaningful when rendering; the bot route gates passing this on
    # non-headless launches.
    internal_resolution: int | None = None,
    # DSP emulation mode. 'HLE' (fast) or 'LLE' (accurate). '' leaves
    # Dolphin's default in place.
    audio_emulation: str = "",
    # Override where the subprocess writes .slp files. Passed through to
    # libmelee which writes it into Dolphin.ini's SlippiReplayDir key.
    # Empty string falls back to whatever the Dolphin build defaults to.
    replay_dir: str = "",
    # Seconds for libmelee's polling-mode frame wait. Required by the
    # netplay.py stall detector; pass 0 / None to keep blocking behavior.
    console_timeout: float | None = None,
    # JSON-encoded live-events observer config. Empty string means the
    # master toggle is off (or the feature is unconfigured) and the flag
    # is omitted entirely so netplay.py's per-frame cost stays at zero.
    live_events_config: str = "",
    dolphin: str,
    iso: str,
    on_complete: Callable[[str], None] | None = None,
) -> LaunchResult:
    """Spawn the netplay subprocess and record the match.

    Callers are responsible for validating paths, resolving the agent path,
    picking the dolphin exe, and deciding the headless strategy before
    calling this. Keeping those steps out of here means both the GUI route
    and the bot route can share the exact ``process_manager.launch`` call
    without either having to know the other's validation branches.
    """
    root = cfg.get("paths", "slippi_ai_root")
    if not find_script(root, "scripts/netplay.py"):
        return LaunchResult(None, None, None, error=(
            f"scripts/netplay.py not found under slippi_ai_root ({root!r})."
        ))

    user_json = cfg.get("paths", "user_json")

    clear_end_match_sentinel(cfg)
    sentinel_path = end_match_sentinel_path(cfg)
    state_path = series_state_path(cfg)

    overrides: dict[str, object] = {
        "end_match_sentinel": str(sentinel_path),
        "series_state_path": str(state_path),
        "agent.path": abs_agent_path,
        "agent.sample_temperature": round(sample_temperature, 2),
        "char": character or "fox",
        "dolphin.path": dolphin,
        "dolphin.iso": iso,
        "dolphin.connect_code": connect_code,
        "dolphin.stage": stage,
    }
    if agent_name:
        overrides["agent.name"] = agent_name
    if user_json:
        overrides["dolphin.user_json_path"] = user_json
    if not auto_delay:
        overrides["dolphin.online_delay"] = delay
    if force_port:
        overrides["dolphin.netplay_port"] = force_port
    if force_lan_ip:
        overrides["dolphin.lan_ip"] = force_lan_ip
    if gfx_backend:
        overrides["dolphin.gfx_backend"] = gfx_backend
    if audio_backend:
        overrides["dolphin.audio_backend"] = audio_backend
    if internal_resolution is not None:
        overrides["dolphin.internal_resolution"] = int(internal_resolution)
    if audio_emulation:
        overrides["dolphin.audio_emulation"] = audio_emulation
    if replay_dir:
        overrides["dolphin.replay_dir"] = replay_dir
    if console_timeout is not None and console_timeout > 0:
        overrides["dolphin.console_timeout"] = float(console_timeout)
    if live_events_config:
        overrides["live_events_config"] = live_events_config

    gecko_path = gecko_codes_path()
    if gecko_path.exists() and load_gecko_codes_text().strip():
        overrides["dolphin.gecko_codes_file"] = str(gecko_path)

    bool_flags = {
        "dolphin.fullscreen": fullscreen,
        "dolphin.infinite_time": infinite_time,
        "dolphin.disable_audio": disable_audio,
        "dolphin.save_replays": save_replays,
        "dolphin.headless": use_headless,
    }
    for flag, enabled in bool_flags.items():
        if enabled:
            overrides[flag] = True

    try:
        info = process_manager.launch(
            "netplay", overrides, cfg, wrap_xvfb=wrap_xvfb)
    except ValueError as e:
        return LaunchResult(None, None, None, error=str(e))

    match_id = None
    try:
        match_id = match_store.start_match(
            mode="netplay",
            agent_path=agent_path,
            agent_name=agent_name,
            ai_character=character,
            stage=stage,
            input_delay=delay,
            sample_temperature=sample_temperature,
            connect_code=connect_code,
            player_slot=1,
        )
    except Exception:
        pass

    if match_id:
        def _finalise(_info, _mid=match_id):
            try:
                match_store.end_match(_mid)
            except Exception:
                pass
            if on_complete:
                try:
                    on_complete(_mid)
                except Exception:
                    pass
        process_manager.on_complete(info.id, _finalise)
    elif on_complete:
        process_manager.on_complete(info.id, lambda _i: on_complete(""))

    return LaunchResult(
        process_id=info.id,
        status=info.status,
        match_id=match_id,
    )
