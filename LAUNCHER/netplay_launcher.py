"""Shared netplay launch orchestration.

Used by both ``POST /play/launch/netplay`` (from the GUI's Play page) and
``POST /bot/launch`` (from the Discord-bot command surface). Keeps the
subprocess command shape identical between the two entry points so a match
spawned by the bot is indistinguishable from one spawned by the GUI button.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from LAUNCHER.api.training import process_manager
from LAUNCHER.config import (
    find_script,
    gecko_codes_path,
    load_gecko_codes_text,
)


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

    overrides: dict[str, object] = {
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
