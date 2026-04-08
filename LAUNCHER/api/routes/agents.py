"""Agent store API: browse, edit, and sync trained agents."""

import os

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentUpdate(BaseModel):
    nickname: str | None = None
    notes: str | None = None
    training_type: str | None = None


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
    """Get model metadata: detected character (from filename) and names list (from pickle)."""
    from LAUNCHER.config import detect_character, read_names_list

    s = get_state()
    store = s.agent_store if source == "agents" else s.experiment_store
    rec = next((r for r in store.get_all() if r.get("id") == agent_id), None)
    if rec is None:
        return {"error": "not found"}

    rel_path = rec["agent_path"]
    scan_dir = (s.cfg.get("paths", "agents_dir") if source == "agents"
                else os.path.join(s.cfg.get("paths", "slippi_ai_root") or "", "experiments"))
    full_path = os.path.join(scan_dir, rel_path) if scan_dir else rel_path

    return {
        "primary_character": detect_character(rel_path),
        "names": read_names_list(full_path) or [],
    }
