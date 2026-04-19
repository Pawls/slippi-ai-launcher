"""Discord-bot command surface.

Exposes a small HTTP API that an external Discord/LLM bot calls to:
- poll presence so it knows when Paul is ready to accept challenges
- look up which ``(character, style_name)`` combos are available
- request a match launch against a Discord challenger's connect code
- poll the outcome of an approval-gated challenge

The Play page GUI toggles presence and (in ``available_with_approval`` mode)
approves / denies pending challenges via the same router.

Auth: all endpoints require ``Authorization: Bearer <api_token>`` where the
token lives in ``LAUNCHER/bot_allowlist.json`` (auto-generated on first run).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.routes.play import (
    _resolve_agent_path,
    _resolve_dolphin_path,
    _plan_headless,
)
from LAUNCHER.bot_state import (
    ActiveMatch,
    get_bot_state,
    load_allowlist,
    load_models_config,
    rotate_api_token,
    save_allowlist,
)
from LAUNCHER.netplay_launcher import launch_netplay_session


router = APIRouter(prefix="/bot", tags=["bot"])


# ── Auth dependency ────────────────────────────────────────────────────

def _require_token(authorization: str | None = Header(default=None)):
    expected = load_allowlist().get("api_token", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="bot_allowlist.json has no api_token",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def _require_loopback(request: Request):
    """Gate admin endpoints (token rotation, allowlist editing) so a leaked
    Bearer token can't be used to grant new Discord IDs access. The
    launcher already binds 127.0.0.1 — this is belt-and-suspenders in
    case someone fronts it with a reverse proxy in the future."""
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="loopback only")


# ── Models ─────────────────────────────────────────────────────────────

class LaunchRequest(BaseModel):
    challenger_discord_id: str
    challenger_tag: str = ""
    challenger_username: str = ""
    connect_code: str
    character: str
    style_name: str


class ApproveRequest(BaseModel):
    challenge_id: str
    decision: str  # "approve" | "deny"
    headless: bool = True


class PresenceRequest(BaseModel):
    state: str


class AllowlistRequest(BaseModel):
    allowed_discord_ids: list[str]


# ── Helpers ────────────────────────────────────────────────────────────

def _find_approved(character: str, style_name: str) -> dict | None:
    """Case-insensitive lookup in bot_models.json."""
    cfg = load_models_config()
    c = character.strip().lower()
    n = style_name.strip().lower()
    for entry in cfg.get("approved_models", []):
        if (entry.get("character", "").strip().lower() == c and
                entry.get("style_name", "").strip().lower() == n):
            return entry
    return None


def _agent_record(agent_id: str, source: str) -> dict | None:
    s = get_state()
    store = s.agent_store if source != "experiments" else s.experiment_store
    for rec in store.get_all():
        if rec.get("id") == agent_id:
            return rec
    return None


def _roster() -> list[dict]:
    """Group approved models by character, filter to entries whose agent_id
    still resolves in the store (drops silently if someone deleted a model)."""
    cfg = load_models_config()
    by_char: dict[str, list[str]] = defaultdict(list)
    for entry in cfg.get("approved_models", []):
        rec = _agent_record(entry.get("agent_id", ""), entry.get("source", "agents"))
        if not rec or rec.get("missing"):
            continue
        by_char[entry.get("character", "")].append(entry.get("style_name", ""))
    return [
        {"character": c, "styles": styles}
        for c, styles in sorted(by_char.items())
        if c
    ]


def _launch_for(entry: dict, body: LaunchRequest, headless: bool) -> tuple[str | None, str | None]:
    """Actually spawn Dolphin for an approved-model entry. Returns
    ``(match_id, error)`` — ``error`` is a user-safe string or ``None``."""
    s = get_state()
    cfg = s.cfg
    models_cfg = load_models_config()
    defaults = models_cfg.get("defaults", {}) or {}

    iso = cfg.get("paths", "iso")
    root = cfg.get("paths", "slippi_ai_root")
    if not root or not iso:
        return None, "slippi_ai_root and iso must be configured"

    source = entry.get("source", "agents")
    rec = _agent_record(entry["agent_id"], source)
    if not rec:
        return None, "approved model no longer in agent store"

    use_bot_vs_human = bool(defaults.get("use_bot_vs_human", True))
    dolphin = _resolve_dolphin_path(cfg, use_bot_vs_human, headless)
    if not dolphin:
        return None, "Dolphin path not configured"

    use_headless, wrap_xvfb, headless_err = _plan_headless(dolphin, headless)
    if headless_err:
        return None, headless_err

    abs_path = _resolve_agent_path(cfg, source, rec["agent_path"])

    # Pick a name from the model's cached name_map if available — helps
    # the netplay script load the right sub-policy for styled models.
    names = rec.get("names") or []
    agent_name = body.style_name if body.style_name in names else ""

    result = launch_netplay_session(
        cfg=cfg,
        match_store=s.match_store,
        agent_path=rec["agent_path"],
        abs_agent_path=abs_path,
        agent_name=agent_name,
        character=body.character,
        connect_code=body.connect_code,
        delay=int(defaults.get("delay", 2)),
        auto_delay=bool(defaults.get("auto_delay", True)),
        sample_temperature=float(defaults.get("sample_temperature", 1.0)),
        save_replays=bool(defaults.get("save_replays", True)),
        use_headless=use_headless,
        wrap_xvfb=wrap_xvfb,
        dolphin=dolphin,
        iso=iso,
        on_complete=lambda _mid: get_bot_state().clear_match(),
    )
    if result.error:
        return None, result.error
    return result.match_id, None


# ── Routes ─────────────────────────────────────────────────────────────

@router.get("/local-token")
def get_local_token():
    """Return the bearer token so the GUI can read it without the user
    copy-pasting from ``bot_allowlist.json``. The launcher binds to
    ``127.0.0.1`` — this endpoint is implicitly loopback-only."""
    return {"api_token": load_allowlist().get("api_token", "")}


# ── Integration-setup surface (GUI-only) ───────────────────────────────
# The Play page exposes a Discord-integration panel backed by these
# three endpoints. All gated to loopback so a leaked API token can't be
# used to grant a new Discord ID access.

@router.get("/integration", dependencies=[Depends(_require_loopback)])
def get_integration(request: Request):
    """Return everything a bot author needs to connect: the API token,
    the current allowlist, the backend URL the bot should target, and a
    list of endpoints the bot will call."""
    allow = load_allowlist()
    base = str(request.base_url).rstrip("/")
    return {
        "api_token": allow.get("api_token", ""),
        "allowed_discord_ids": allow.get("allowed_discord_ids", []),
        "backend_url": base,
        "endpoints": [
            {"method": "GET",  "path": "/bot/status",
             "desc": "Poll presence, active match, pending challenges"},
            {"method": "GET",  "path": "/bot/roster",
             "desc": "List approved (character, style_name) combos"},
            {"method": "POST", "path": "/bot/launch",
             "desc": "Request a match — body: {challenger_discord_id, challenger_username?, character, style_name, connect_code}"},
            {"method": "POST", "path": "/bot/approve",
             "desc": "Approve or deny a pending challenge (auth bot only — GUI usually handles this)"},
        ],
    }


@router.post("/integration/allowlist", dependencies=[Depends(_require_loopback)])
def set_allowlist(body: AllowlistRequest):
    """Replace the full list of Discord user IDs permitted to challenge."""
    merged = save_allowlist({"allowed_discord_ids": body.allowed_discord_ids})
    return {"allowed_discord_ids": merged["allowed_discord_ids"]}


@router.post("/integration/rotate-token", dependencies=[Depends(_require_loopback)])
def rotate_token():
    """Generate a fresh API token. The previously-issued token stops working
    immediately, so the bot author must be notified to paste the new one."""
    return {"api_token": rotate_api_token()}


@router.get("/presence", dependencies=[Depends(_require_token)])
def get_presence():
    return get_bot_state().get_presence()


@router.post("/presence", dependencies=[Depends(_require_token)])
def set_presence(body: PresenceRequest):
    try:
        return get_bot_state().set_presence(body.state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/roster", dependencies=[Depends(_require_token)])
def get_roster():
    bs = get_bot_state().get_presence()
    return {
        "state": bs["state"],
        "in_match": bs["in_match"],
        "roster": _roster(),
    }


@router.get("/status", dependencies=[Depends(_require_token)])
def get_status():
    return get_bot_state().snapshot()


@router.post("/launch", dependencies=[Depends(_require_token)])
def launch(body: LaunchRequest):
    bs_store = get_bot_state()
    snap = bs_store.snapshot()

    if snap["state"] == "offline":
        raise HTTPException(status_code=409, detail={"reason": "offline"})

    allow = load_allowlist()
    allowed = {x.lower() for x in allow.get("allowed_discord_ids", [])}
    candidates = {body.challenger_discord_id.lower()}
    if body.challenger_username:
        candidates.add(body.challenger_username.lower())
    if not (candidates & allowed):
        raise HTTPException(status_code=403, detail={"reason": "not_allowed"})

    if snap["match"] is not None:
        raise HTTPException(status_code=409, detail={"reason": "busy"})
    if snap["pending"]:
        raise HTTPException(status_code=409, detail={"reason": "pending_approval"})

    entry = _find_approved(body.character, body.style_name)
    if not entry:
        raise HTTPException(status_code=404, detail={"reason": "unknown_model"})

    if snap["state"] == "available_with_approval":
        try:
            p = bs_store.add_pending(
                challenger_discord_id=body.challenger_discord_id,
                challenger_tag=body.challenger_tag or body.challenger_discord_id,
                connect_code=body.connect_code,
                character=body.character,
                style_name=body.style_name,
            )
        except RuntimeError:
            raise HTTPException(status_code=409, detail={"reason": "queue_full"})
        return {
            "status": "pending_approval",
            "challenge_id": p.challenge_id,
            "poll_url": f"/bot/challenge/{p.challenge_id}",
            "expires_at": p.expires_at,
        }

    # available → immediate launch, headless by default
    match_id, err = _launch_for(entry, body, headless=True)
    if err:
        raise HTTPException(status_code=500, detail=err)

    bs_store.set_match(ActiveMatch(
        match_id=match_id or "",
        challenger_discord_id=body.challenger_discord_id,
        challenger_tag=body.challenger_tag or body.challenger_discord_id,
        character=body.character,
        style_name=body.style_name,
        connect_code=body.connect_code,
        headless=True,
        started_at=datetime.now(timezone.utc).isoformat(),
    ))
    return {"status": "launching", "match_id": match_id}


@router.post("/approve", dependencies=[Depends(_require_token)])
def approve(body: ApproveRequest):
    bs_store = get_bot_state()
    p = bs_store.pop_pending(body.challenge_id)
    if not p:
        raise HTTPException(status_code=404, detail="challenge not found or expired")

    if body.decision == "deny":
        bs_store.resolve(p.challenge_id, "denied")
        return {"status": "denied"}

    if body.decision != "approve":
        raise HTTPException(status_code=400, detail="decision must be approve|deny")

    entry = _find_approved(p.character, p.style_name)
    if not entry:
        bs_store.resolve(p.challenge_id, "denied")
        raise HTTPException(status_code=404, detail="model no longer approved")

    # Reject if something started between pending-create and approve.
    if bs_store.snapshot()["match"] is not None:
        bs_store.resolve(p.challenge_id, "denied")
        raise HTTPException(status_code=409, detail={"reason": "busy"})

    launch_body = LaunchRequest(
        challenger_discord_id=p.challenger_discord_id,
        challenger_tag=p.challenger_tag,
        connect_code=p.connect_code,
        character=p.character,
        style_name=p.style_name,
    )
    match_id, err = _launch_for(entry, launch_body, headless=body.headless)
    if err:
        bs_store.resolve(p.challenge_id, "denied")
        raise HTTPException(status_code=500, detail=err)

    bs_store.set_match(ActiveMatch(
        match_id=match_id or "",
        challenger_discord_id=p.challenger_discord_id,
        challenger_tag=p.challenger_tag,
        character=p.character,
        style_name=p.style_name,
        connect_code=p.connect_code,
        headless=body.headless,
        started_at=datetime.now(timezone.utc).isoformat(),
    ))
    bs_store.resolve(p.challenge_id, "approved", match_id=match_id, headless=body.headless)
    return {"status": "approved", "match_id": match_id, "headless": body.headless}


@router.get("/challenge/{challenge_id}", dependencies=[Depends(_require_token)])
def get_challenge(challenge_id: str):
    return get_bot_state().lookup_challenge(challenge_id)
