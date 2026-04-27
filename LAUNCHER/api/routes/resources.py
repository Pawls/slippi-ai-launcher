"""Resource monitoring API: GPU/CPU/RAM snapshots and sessions."""

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.resource_store import take_snapshot

router = APIRouter(prefix="/resources", tags=["resources"])


@router.get("/snapshot")
def get_snapshot():
    """Take a single resource snapshot (GPU + CPU + RAM)."""
    return take_snapshot()


@router.get("/sessions")
def list_sessions():
    """List all recording sessions (metadata only, no raw samples)."""
    return get_state().resource_store.get_all_meta()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    rec = get_state().resource_store.get_session(session_id)
    if rec is None:
        return {"error": "not found"}, 404
    return rec


class SessionCreate(BaseModel):
    label: str


@router.post("/sessions")
def create_session(body: SessionCreate):
    sid = get_state().resource_store.create_session(body.label)
    return {"id": sid}


@router.post("/sessions/{session_id}/sample")
def add_sample(session_id: str):
    """Take a snapshot and append it to the session."""
    snapshot = take_snapshot()
    get_state().resource_store.add_sample(session_id, snapshot)
    return snapshot


@router.post("/sessions/{session_id}/end")
def end_session(session_id: str):
    get_state().resource_store.end_session(session_id)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    get_state().resource_store.delete_session(session_id)
    return {"ok": True}
