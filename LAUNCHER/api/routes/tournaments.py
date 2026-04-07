"""Tournament API: create and manage round-robin tournaments."""

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.tournament_store import TournamentStore

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


class TournamentCreate(BaseModel):
    name: str
    agents: list[dict]
    stage: str
    num_games: int


class TournamentUpdate(BaseModel):
    status: str | None = None
    name: str | None = None


class MatchupUpdate(BaseModel):
    status: str | None = None
    winner_index: int | None = None
    started: str | None = None
    finished: str | None = None


@router.get("/")
def list_tournaments():
    return get_state().tournament_store.get_all()


@router.get("/{tid}")
def get_tournament(tid: str):
    rec = get_state().tournament_store.get_tournament(tid)
    if rec is None:
        return {"error": "not found"}, 404
    return rec


@router.get("/{tid}/leaderboard")
def get_leaderboard(tid: str):
    rec = get_state().tournament_store.get_tournament(tid)
    if rec is None:
        return {"error": "not found"}, 404
    return TournamentStore.compute_leaderboard(rec)


@router.post("/")
def create_tournament(body: TournamentCreate):
    tid = get_state().tournament_store.create_tournament(
        body.name, body.agents, body.stage, body.num_games)
    return {"id": tid}


@router.put("/{tid}")
def update_tournament(tid: str, body: TournamentUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    get_state().tournament_store.update_tournament(tid, **fields)
    return {"ok": True}


@router.put("/{tid}/matchups/{matchup_id}")
def update_matchup(tid: str, matchup_id: str, body: MatchupUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    get_state().tournament_store.update_matchup(tid, matchup_id, **fields)
    return {"ok": True}


@router.delete("/{tid}")
def delete_tournament(tid: str):
    get_state().tournament_store.delete_tournament(tid)
    return {"ok": True}
