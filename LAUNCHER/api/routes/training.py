"""Training process API: launch, stop, and monitor training/eval scripts."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.training import SCRIPTS, build_command, process_manager
from LAUNCHER.api.training_presets import (
    BUILTIN_PRESETS,
    delete_user_preset,
    is_builtin,
    merged_presets,
    save_user_preset,
)

router = APIRouter(prefix="/training", tags=["training"])


class LaunchRequest(BaseModel):
    script_key: str
    config_overrides: dict[str, object] = {}


class SavePresetRequest(BaseModel):
    script_key: str
    name: str
    values: dict[str, object]


@router.get("/scripts")
def list_scripts():
    """List available training/eval scripts."""
    return {
        key: {"label": info["label"], "script": info["script"]}
        for key, info in SCRIPTS.items()
    }


@router.get("/presets")
def list_presets():
    """Return built-in + user training presets, grouped by script key.

    Shape: {script_key: {name: {"values": {...}, "builtin": bool}}}
    """
    return merged_presets()


@router.post("/presets")
def save_preset(body: SavePresetRequest):
    """Create or overwrite a user preset."""
    name = body.name.strip()
    if not name:
        return {"error": "Preset name is required."}
    if body.script_key not in BUILTIN_PRESETS:
        return {"error": f"Unknown script: {body.script_key}"}
    save_user_preset(body.script_key, name, body.values)
    return {"saved": True, "script_key": body.script_key, "name": name}


@router.delete("/presets/{script_key}/{name}")
def delete_preset(script_key: str, name: str):
    """Delete a user preset. Built-in presets cannot be deleted."""
    if is_builtin(script_key, name):
        # A user override of a built-in name may still exist; delete that, but
        # leave the built-in in place. Distinguish by checking if delete_user_preset
        # actually removed something.
        removed = delete_user_preset(script_key, name)
        if removed:
            return {"deleted": True, "restored_builtin": True}
        return {"error": f"'{name}' is a built-in preset and cannot be deleted."}
    removed = delete_user_preset(script_key, name)
    if not removed:
        return {"error": f"Preset '{name}' not found."}
    return {"deleted": True, "restored_builtin": False}


@router.post("/preview-command")
def preview_command(body: LaunchRequest):
    """Preview the CLI command without launching it."""
    s = get_state()
    root = s.cfg.get("paths", "slippi_ai_root")
    try:
        cmd = build_command(body.script_key, body.config_overrides, root)
        return {"command": cmd, "command_str": " \\\n  ".join(cmd)}
    except ValueError as e:
        return {"error": str(e)}


@router.post("/launch")
def launch_process(body: LaunchRequest):
    """Launch a training/eval subprocess."""
    s = get_state()
    try:
        info = process_manager.launch(
            body.script_key, body.config_overrides, s.cfg)
        return {
            "id": info.id,
            "script_key": info.script_key,
            "status": info.status,
        }
    except ValueError as e:
        return {"error": str(e)}


@router.post("/{process_id}/stop")
def stop_process(process_id: str):
    """Stop a running process."""
    stopped = process_manager.stop(process_id)
    return {"stopped": stopped}


@router.get("/processes")
def list_processes():
    """List all tracked processes."""
    return process_manager.list_all()


@router.get("/{process_id}")
def get_process(process_id: str):
    """Get process status."""
    info = process_manager.get(process_id)
    if info is None:
        return {"error": "not found"}
    return {
        "id": info.id,
        "script_key": info.script_key,
        "status": info.status,
        "return_code": info.return_code,
        "started": info.started,
        "log_count": len(info.log_lines),
    }


@router.get("/{process_id}/logs")
def get_logs(process_id: str, offset: int = 0):
    """Get process log lines starting from offset."""
    info = process_manager.get(process_id)
    if info is None:
        return {"error": "not found"}
    lines = info.get_logs(offset)
    return {
        "lines": lines,
        "offset": offset,
        "total": offset + len(lines),
    }


@router.get("/{process_id}/logs/stream")
def stream_logs(process_id: str):
    """Stream process logs as server-sent events (SSE)."""
    import asyncio
    import time

    info = process_manager.get(process_id)
    if info is None:
        return {"error": "not found"}

    def event_generator():
        offset = 0
        while True:
            lines = info.get_logs(offset)
            for line in lines:
                yield f"data: {line}\n\n"
                offset += 1

            if info.status in ("completed", "failed", "stopped"):
                # Drain any remaining lines
                final = info.get_logs(offset)
                for line in final:
                    yield f"data: {line}\n\n"
                yield f"event: done\ndata: {info.status}\n\n"
                break

            time.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )
