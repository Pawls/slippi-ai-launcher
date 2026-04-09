"""Training process API: launch, stop, and monitor training/eval scripts."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.training import SCRIPTS, build_command, process_manager
from LAUNCHER.api.training_presets import BUILTIN_PRESETS

router = APIRouter(prefix="/training", tags=["training"])


class LaunchRequest(BaseModel):
    script_key: str
    config_overrides: dict[str, object] = {}


@router.get("/scripts")
def list_scripts():
    """List available training/eval scripts."""
    return {
        key: {"label": info["label"], "script": info["script"]}
        for key, info in SCRIPTS.items()
    }


@router.get("/presets")
def list_presets():
    """Return all built-in training presets, grouped by script key."""
    return BUILTIN_PRESETS


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
