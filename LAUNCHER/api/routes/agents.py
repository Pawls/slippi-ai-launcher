"""Agent store API: browse, edit, and sync trained agents."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.checkpoint_ops import (
    REPAIR_MODES, apply_repair, inspect_checkpoint, strip_one,
)

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentUpdate(BaseModel):
    nickname: str | None = None
    notes: str | None = None
    training_type: str | None = None


class RepairRequest(BaseModel):
    mode: str
    custom_names: list[str] | None = None


def _scan_dir(source: str) -> str:
    """Resolve the on-disk root for ``source`` (``agents`` or ``experiments``).

    Centralized because three endpoints below need to translate an agent's
    ``agent_path`` (relative) into a full path; the resolution differs
    between agents and experiments.
    """
    s = get_state()
    if source == "experiments":
        root = s.cfg.get("paths", "slippi_ai_root") or ""
        return os.path.join(root, "experiments") if root else ""
    return s.cfg.get("paths", "agents_dir") or ""


def _resolve_agent_path(agent_id: str, source: str) -> str:
    """Find the full filesystem path for ``agent_id``. Raises 404 if missing."""
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    rec = next((r for r in store.get_all() if r.get("id") == agent_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="agent not found")

    rel_path = rec["agent_path"]
    scan_dir = _scan_dir(source)
    full = os.path.join(scan_dir, rel_path) if scan_dir else rel_path
    if not os.path.isfile(full):
        raise HTTPException(
            status_code=404,
            detail=f"checkpoint file missing on disk: {full}")
    return full


@router.get("/")
def list_agents(source: str = "agents"):
    """List all agents. source='agents' or 'experiments'."""
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    return store.get_all()


@router.get("/{agent_id}")
def get_agent(agent_id: str, source: str = "agents"):
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    for rec in store.get_all():
        if rec.get("id") == agent_id:
            return rec
    return {"error": "not found"}, 404


@router.put("/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate, source: str = "agents"):
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    store.update_agent(agent_id, **fields)
    return {"ok": True}


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, source: str = "agents"):
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    store.delete_agent(agent_id)
    return {"ok": True}


@router.post("/sync")
def sync_agents(source: str = "agents"):
    """Rescan the agents or experiments directory and sync the store."""
    s = get_state()
    if source == "experiments":
        root = s.cfg.get("paths", "slippi_ai_root")
        scan_dir = os.path.join(root, "experiments") if root else ""
        store = s.experiment_store
    else:
        scan_dir = s.cfg.get("paths", "agents_dir")
        store = s.agent_store
    store.sync(scan_dir)
    return {"ok": True, "count": len(store.get_all())}


@router.get("/{agent_id}/stats")
def agent_stats(agent_id: str, source: str = "agents"):
    """Get win/loss stats for an agent."""
    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    for rec in store.get_all():
        if rec.get("id") == agent_id:
            from LAUNCHER.agent_store import AgentStore
            return AgentStore.get_stats_for_agent(
                rec["agent_path"], s.match_store)
    return {"error": "not found"}, 404


@router.get("/{agent_id}/metadata")
def agent_metadata(agent_id: str, source: str = "agents"):
    """Get model metadata: detected character (from filename) and cached names list.

    Names are cached on the agent_store record (populated during sync, or
    lazy-filled here for legacy records). This avoids re-parsing the .pkl —
    and re-importing TensorFlow — every time the Play screen loads.
    """
    from LAUNCHER.config import detect_character

    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    rec = next((r for r in store.get_all() if r.get("id") == agent_id), None)
    if rec is None:
        return {"error": "not found"}

    rel_path = rec["agent_path"]

    # Fast path: record already carries both cached lists. ``agent_names``
    # (RL-baked override) is treated as a separate field because its absence
    # on legacy records must trigger a re-parse — an empty list on a parsed
    # record is the "confirmed imitation-only" signal.
    cached_names = rec.get("names")
    cached_agent_names = rec.get("agent_names")
    if cached_names is not None and cached_agent_names is not None:
        return {
            "primary_character": detect_character(rel_path),
            "names": cached_names,
            "agent_names": cached_agent_names,
        }

    # Legacy record without one or both fields: parse the pickle once and
    # persist both together.
    scan_dir = _scan_dir(source)
    full_path = os.path.join(scan_dir, rel_path) if scan_dir else rel_path
    parsed = store.ensure_parsed_fields(agent_id, full_path)

    return {
        "primary_character": detect_character(rel_path),
        "names": parsed.get("names", []),
        "agent_names": parsed.get("agent_names", []),
    }


# ── Repair RL checkpoint ────────────────────────────────────────────────

@router.get("/{agent_id}/repair-info")
def get_repair_info(agent_id: str, source: str = "agents"):
    """Inspect a checkpoint for RL repair: current chars/names + name_map.

    Returns ``{has_rl_config, chars, names, matched, name_map}``. The frontend
    uses this to decide which repair modes to enable and to render the
    available-names list users can choose from.
    """
    full = _resolve_agent_path(agent_id, source)
    try:
        return inspect_checkpoint(full)
    except (OSError, EOFError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"could not read checkpoint: {e}")


@router.post("/{agent_id}/repair")
def repair_agent(agent_id: str, body: RepairRequest, source: str = "agents"):
    """Apply a repair mode to the agent's checkpoint and persist the result."""
    if body.mode not in REPAIR_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown repair mode {body.mode!r}; must be one of {list(REPAIR_MODES)}")
    full = _resolve_agent_path(agent_id, source)
    try:
        result = apply_repair(full, body.mode, custom_names=body.custom_names)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


# ── Export for sharing (strip to policy-only) ───────────────────────────

@router.post("/{agent_id}/export")
def export_agent(agent_id: str, source: str = "agents"):
    """Strip the agent to policy-only weights and write to an ``exports/``
    subfolder of the source directory. Returns the new file's path + sizes.

    The output filename gets a ``_stripped`` suffix so users can tell the
    inference-only copy apart from the original training checkpoint.
    """
    full_src = _resolve_agent_path(agent_id, source)
    scan_dir = _scan_dir(source)
    if not scan_dir:
        raise HTTPException(
            status_code=400,
            detail="No source directory configured — set paths.agents_dir first.")

    base = os.path.basename(full_src)
    stem, ext = os.path.splitext(base)
    dst = os.path.join(scan_dir, "exports", f"{stem}_stripped{ext}")

    try:
        info = strip_one(full_src, dst)
    except (OSError, KeyError, EOFError) as e:
        # KeyError surfaces if the checkpoint shape is unexpected (no
        # ``state.policy``) — surface that to the user instead of crashing.
        raise HTTPException(
            status_code=400,
            detail=f"could not strip checkpoint: {e}")

    return {"ok": True, **info}
