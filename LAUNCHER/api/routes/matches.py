"""Match history API: record and query match sessions."""

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state

router = APIRouter(prefix="/matches", tags=["matches"])


class MatchStart(BaseModel):
    mode: str = ""
    agent_path: str = ""
    agent_name: str = ""
    ai_character: str = ""
    stage: str = ""
    input_delay: int = 0
    sample_temperature: float = 1.0
    connect_code: str = ""
    player_slot: int = 1


class MatchUpdate(BaseModel):
    result: str | None = None
    user_notes: str | None = None


@router.get("/")
def list_matches():
    return get_state().match_store.get_all()


@router.get("/stats")
def match_stats():
    return get_state().match_store.get_stats()


@router.post("/")
def start_match(body: MatchStart):
    match_id = get_state().match_store.start_match(**body.model_dump())
    return {"id": match_id}


@router.post("/{match_id}/end")
def end_match(match_id: str):
    get_state().match_store.end_match(match_id)
    return {"ok": True}


@router.put("/{match_id}")
def update_match(match_id: str, body: MatchUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    get_state().match_store.update_match(match_id, **fields)
    return {"ok": True}


@router.delete("/{match_id}")
def delete_match(match_id: str):
    get_state().match_store.delete_match(match_id)
    return {"ok": True}
