"""Play API: launch local or netplay matches against AI agents."""

import os
import sys

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.training import process_manager
from LAUNCHER.config import (
    dolphin_supports_headless_platform,
    ensure_executable,
    find_script,
    gecko_codes_path,
    have_xvfb_run,
    load_gecko_codes_text,
)


router = APIRouter(prefix="/play", tags=["play"])

CONNECT_CODE_HISTORY_KEY = "connect_code_history"
MAX_HISTORY = 20


class LocalPlayRequest(BaseModel):
    agent_path: str
    source: str = "agents"  # "agents" or "experiments"
    character: str = ""
    name: str = ""
    delay: int = 2
    sample_temperature: float = 1.0
    use_gpu: bool = False
    player_slot: int = 1  # 1 = human is P1, 2 = human is P2

    stage: str = "RANDOM_STAGE"
    fullscreen: bool = True
    infinite_time: bool = False
    disable_audio: bool = False
    headless: bool = False
    copy_home_directory: bool = True
    gfx_backend: str = ""

    use_bot_vs_human: bool = False  # Use bot_vs_human Dolphin instead of standard


class NetplayRequest(BaseModel):
    agent_path: str
    source: str = "agents"  # "agents" or "experiments"
    character: str = ""
    name: str = ""
    delay: int = 2
    auto_delay: bool = True  # Use model's trained delay
    sample_temperature: float = 1.0
    use_gpu: bool = False

    connect_code: str = ""
    force_port: str = ""
    force_lan_ip: str = ""

    stage: str = "RANDOM_STAGE"
    fullscreen: bool = False
    infinite_time: bool = False
    save_replays: bool = True
    disable_audio: bool = False
    headless: bool = False
    gfx_backend: str = ""

    use_bot_vs_human: bool = True  # Netplay typically uses bvh Dolphin


def _resolve_dolphin_path(cfg, use_bot_vs_human: bool, headless: bool) -> str | None:
    """Pick the right Dolphin executable based on user options."""
    if use_bot_vs_human:
        bvh = cfg.get("paths", "bot_vs_human_exe")
        if bvh and os.path.exists(bvh):
            ensure_executable(bvh)
            return bvh
    if headless:
        hl = cfg.get("paths", "dolphin_headless")
        if hl and os.path.exists(hl):
            ensure_executable(hl)
            return hl
    path = cfg.get("paths", "dolphin_dir")
    ensure_executable(path)
    return path


def _resolve_agent_path(cfg, source: str, rel_path: str) -> str:
    """Resolve a relative agent path (as stored in agent_store) to an absolute
    filesystem path based on the source's scan dir.

    The eval/netplay subprocess runs with cwd=slippi_ai_root, so historically
    the experiments source happens to work via cwd-relative resolution
    (`experiments/foo.pkl`), but the agents source needs explicit joining
    against `paths.agents_dir` since that may be anywhere on disk.
    """
    if not rel_path or os.path.isabs(rel_path):
        return rel_path
    if source == "experiments":
        root = cfg.get("paths", "slippi_ai_root") or ""
        return os.path.join(root, "experiments", rel_path) if root else rel_path
    base = cfg.get("paths", "agents_dir") or ""
    return os.path.join(base, rel_path) if base else rel_path


def _add_optional_flag(cmd: list[str], flag: str, value: bool):
    if value:
        cmd.append(f"--{flag}")


def _plan_headless(dolphin: str, headless_requested: bool) -> tuple[bool, bool, str | None]:
    """Decide how to honor a "Run without Display" request.

    Returns ``(use_headless_flag, wrap_xvfb, error)``:
      - ``use_headless_flag``: pass ``--dolphin.headless=True`` downstream
        (Slippi's custom ``-platform headless`` Qt plugin path).
      - ``wrap_xvfb``: prefix the command with ``xvfb-run`` so Dolphin renders
        normally to a virtual display instead (fallback for builds that lack
        the headless Qt plugin).
      - ``error``: set if neither path is viable; caller should abort launch.
    """
    if not headless_requested or sys.platform == "win32":
        return headless_requested, False, None
    if dolphin_supports_headless_platform(dolphin):
        return True, False, None
    if have_xvfb_run():
        return False, True, None
    return False, False, (
        "The selected Dolphin build does not support headless mode, and "
        "`xvfb-run` is not installed. Install xvfb (`sudo apt install xvfb`) "
        "or uncheck \"Run without Display\"."
    )


@router.post("/launch/local")
def launch_local(body: LocalPlayRequest):
    """Launch a local play game (eval_two.py) against an AI agent."""
    s = get_state()
    cfg = s.cfg
    root = cfg.get("paths", "slippi_ai_root")
    iso = cfg.get("paths", "iso")
    if not root or not iso:
        return {"error": "slippi_ai_root and iso must be configured"}

    script = find_script(root, "scripts/eval_two.py")
    if not script:
        return {"error": "eval_two.py not found in slippi_ai_root"}

    dolphin = _resolve_dolphin_path(cfg, body.use_bot_vs_human, body.headless)
    if not dolphin:
        return {"error": "Dolphin path not configured"}

    use_headless, wrap_xvfb, headless_err = _plan_headless(dolphin, body.headless)
    if headless_err:
        return {"error": headless_err}

    # Player slot determines which port the bot vs human occupies
    ai_port = "p2" if body.player_slot == 1 else "p1"
    human_port = "p1" if body.player_slot == 1 else "p2"

    abs_agent_path = _resolve_agent_path(cfg, body.source, body.agent_path)

    cmd_parts: list[tuple[str, object]] = [
        ("dolphin.path", dolphin),
        ("dolphin.iso", iso),
        ("dolphin.online_delay", body.delay),
        ("dolphin.stage", body.stage),
        (f"{ai_port}.ai.sample_temperature", round(body.sample_temperature, 2)),
        (f"{human_port}.type", "human"),
        (f"{ai_port}.ai.path", abs_agent_path),
    ]
    if body.character:
        cmd_parts.append((f"{ai_port}.character", body.character))
    if body.name:
        cmd_parts.append((f"{ai_port}.ai.name", body.name))
    if body.gfx_backend:
        cmd_parts.append(("dolphin.gfx_backend", body.gfx_backend))

    user_json = cfg.get("paths", "user_json")
    if user_json:
        cmd_parts.append(("dolphin.user_json_path", user_json))

    gecko_path = gecko_codes_path()
    if gecko_path.exists() and load_gecko_codes_text().strip():
        cmd_parts.append(("dolphin.gecko_codes_file", str(gecko_path)))

    overrides = {k: v for k, v in cmd_parts}

    # Boolean flags handled separately (they have no value, just presence)
    bool_flags: dict[str, bool] = {
        "dolphin.fullscreen": body.fullscreen,
        "dolphin.infinite_time": body.infinite_time,
        "dolphin.disable_audio": body.disable_audio,
        "dolphin.copy_home_directory": body.copy_home_directory,
        "dolphin.headless": use_headless,
        "use_gpu": body.use_gpu,
    }
    for flag, enabled in bool_flags.items():
        if enabled:
            overrides[flag] = True

    try:
        info = process_manager.launch(
            "eval_watch", overrides, cfg, wrap_xvfb=wrap_xvfb)
    except ValueError as e:
        return {"error": str(e)}

    # Record the match
    match_id = None
    try:
        match_id = s.match_store.start_match(
            mode="local",
            agent_path=body.agent_path,
            agent_name=body.name,
            ai_character=body.character,
            stage=body.stage,
            input_delay=body.delay,
            sample_temperature=body.sample_temperature,
            connect_code="",
            player_slot=body.player_slot,
        )
    except Exception:
        pass

    if match_id:
        _end_match_when_done(info.id, match_id)

    return {
        "id": info.id,
        "status": info.status,
        "match_id": match_id,
    }


@router.post("/launch/netplay")
def launch_netplay(body: NetplayRequest):
    """Launch a netplay game (netplay.py) against another player using an AI."""
    s = get_state()
    cfg = s.cfg
    root = cfg.get("paths", "slippi_ai_root")
    iso = cfg.get("paths", "iso")
    if not root or not iso:
        return {"error": "slippi_ai_root and iso must be configured"}

    script = find_script(root, "scripts/netplay.py")
    if not script:
        return {"error": "netplay.py not found in slippi_ai_root"}

    if not body.connect_code:
        return {"error": "Opponent connect code is required"}

    dolphin = _resolve_dolphin_path(cfg, body.use_bot_vs_human, body.headless)
    if not dolphin:
        return {"error": "Dolphin path not configured"}

    use_headless, wrap_xvfb, headless_err = _plan_headless(dolphin, body.headless)
    if headless_err:
        return {"error": headless_err}

    user_json = cfg.get("paths", "user_json")

    abs_agent_path = _resolve_agent_path(cfg, body.source, body.agent_path)

    overrides: dict[str, object] = {
        "agent.path": abs_agent_path,
        "agent.sample_temperature": round(body.sample_temperature, 2),
        "char": body.character or "fox",
        "dolphin.path": dolphin,
        "dolphin.iso": iso,
        "dolphin.connect_code": body.connect_code,
        "dolphin.stage": body.stage,
    }
    if body.name:
        overrides["agent.name"] = body.name
    if user_json:
        overrides["dolphin.user_json_path"] = user_json
    if not body.auto_delay:
        overrides["dolphin.online_delay"] = body.delay
    if body.force_port:
        overrides["dolphin.netplay_port"] = body.force_port
    if body.force_lan_ip:
        overrides["dolphin.lan_ip"] = body.force_lan_ip
    if body.gfx_backend:
        overrides["dolphin.gfx_backend"] = body.gfx_backend

    gecko_path = gecko_codes_path()
    if gecko_path.exists() and load_gecko_codes_text().strip():
        overrides["dolphin.gecko_codes_file"] = str(gecko_path)

    bool_flags = {
        "dolphin.fullscreen": body.fullscreen,
        "dolphin.infinite_time": body.infinite_time,
        "dolphin.disable_audio": body.disable_audio,
        "dolphin.save_replays": body.save_replays,
        "dolphin.headless": use_headless,
        "use_gpu": body.use_gpu,
    }
    for flag, enabled in bool_flags.items():
        if enabled:
            overrides[flag] = True

    try:
        info = process_manager.launch(
            "netplay", overrides, cfg, wrap_xvfb=wrap_xvfb)
    except ValueError as e:
        return {"error": str(e)}

    # Add to connect code history
    _add_to_connect_history(cfg, body.connect_code)

    # Record the match
    match_id = None
    try:
        match_id = s.match_store.start_match(
            mode="netplay",
            agent_path=body.agent_path,
            agent_name=body.name,
            ai_character=body.character,
            stage=body.stage,
            input_delay=body.delay,
            sample_temperature=body.sample_temperature,
            connect_code=body.connect_code,
            player_slot=1,
        )
    except Exception:
        pass

    if match_id:
        _end_match_when_done(info.id, match_id)

    return {
        "id": info.id,
        "status": info.status,
        "match_id": match_id,
    }


def _end_match_when_done(process_id: str, match_id: str) -> None:
    """Register a completion callback that finalises the match record once the
    launched process exits — so `duration_seconds` gets computed regardless of
    whether the GUI is still open."""
    def _finalise(_info):
        try:
            get_state().match_store.end_match(match_id)
        except Exception:
            pass
    process_manager.on_complete(process_id, _finalise)


def _add_to_connect_history(cfg, code: str):
    """Add a connect code to history (most recent first, deduplicated)."""
    if not code:
        return
    raw = cfg.get("app", CONNECT_CODE_HISTORY_KEY)
    history = [c.strip() for c in raw.split(",") if c.strip()] if raw else []
    if code in history:
        history.remove(code)
    history.insert(0, code)
    history = history[:MAX_HISTORY]
    cfg.set("app", CONNECT_CODE_HISTORY_KEY, ",".join(history))
    cfg.save()


@router.get("/connect-codes")
def get_connect_codes():
    """Return the connect code history (most recent first)."""
    cfg = get_state().cfg
    raw = cfg.get("app", CONNECT_CODE_HISTORY_KEY)
    if not raw:
        return {"codes": []}
    return {"codes": [c.strip() for c in raw.split(",") if c.strip()]}


@router.delete("/connect-codes/{code}")
def delete_connect_code(code: str):
    """Remove a connect code from history."""
    cfg = get_state().cfg
    raw = cfg.get("app", CONNECT_CODE_HISTORY_KEY)
    history = [c.strip() for c in raw.split(",") if c.strip()] if raw else []
    if code in history:
        history.remove(code)
        cfg.set("app", CONNECT_CODE_HISTORY_KEY, ",".join(history))
        cfg.save()
    return {"ok": True, "codes": history}
