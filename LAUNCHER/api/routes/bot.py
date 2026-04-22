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

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.routes.play import (
    _dolphin_path_error,
    _resolve_agent_path,
    _resolve_dolphin_path,
    _plan_headless,
)
from LAUNCHER.api.training import process_manager
from LAUNCHER.bot_state import (
    ActiveMatch,
    get_bot_state,
    load_allowlist,
    load_models_config,
    rotate_api_token,
    save_allowlist,
    save_models_config,
)
from LAUNCHER.netplay_launcher import launch_netplay_session, touch_end_match_sentinel


_MATCH_STARTED_SENTINEL = "[MATCH_STARTED]"
_GAME_RESULT_PREFIX = "[GAME_RESULT]"
_WATCHDOG_POLL_SEC = 1.0


def _extract_game_result(log_lines) -> tuple[str | None, str | None]:
    """Scan a subprocess's captured stdout for the last GAME_RESULT sentinel
    and return ``(winner, ended)`` where:

    - ``winner`` is ``"ai" | "human" | "draw"`` if a game finished, else None
      (e.g. no-connect timeout never reached IN_GAME).
    - ``ended`` is ``"clean"`` when the game reached POSTGAME_SCORES naturally,
      ``"disconnect"`` when the opponent bailed mid-match, else None.

    The launcher's watchdog uses ``ended`` to override the match-end reason so
    a clean subprocess exit triggered by opponent disconnect still surfaces
    as ``disconnected`` (and fires the heckle) rather than silently recorded.
    """
    for line in reversed(log_lines):
        idx = line.find(_GAME_RESULT_PREFIX)
        if idx < 0:
            continue
        tail = line[idx + len(_GAME_RESULT_PREFIX):]
        # Parse "key=value" tokens, tolerant of leading whitespace and the
        # historical "winner=" prefix position.
        winner: str | None = None
        ended: str | None = None
        for tok in tail.split():
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            if k == "winner" and v in ("ai", "human", "draw"):
                winner = v
            elif k == "ended" and v in ("clean", "disconnect"):
                ended = v
        return winner, ended
    return None, None


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
    # Discord channel the launch was requested in. Carried through so the
    # taunt webhook (feature D) can reply to the same channel on timeout
    # or disconnect. Optional: a missing channel_id just skips the taunt.
    channel_id: str = ""


class ApproveRequest(BaseModel):
    challenge_id: str
    decision: str  # "approve" | "deny"
    headless: bool = True


class PresenceRequest(BaseModel):
    state: str


class AllowlistRequest(BaseModel):
    allowed_discord_ids: list[str]


class TauntWebhookRequest(BaseModel):
    taunt_webhook_url: str


class ChallengerModeRequest(BaseModel):
    allow_any_challenger: bool


class WithdrawRequest(BaseModel):
    challenger_discord_id: str


class ApprovedModel(BaseModel):
    character: str
    style_name: str
    agent_id: str
    source: str = "agents"
    replays: str = ""


class ModelsConfigRequest(BaseModel):
    approved_models: list[ApprovedModel] | None = None
    defaults: dict | None = None


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


# ── Taunt event log (polling companion to the push webhook) ───────────
#
# A remote bot that reaches this backend over Tailscale can't easily host
# an inbound webhook (the push flow requires the launcher to reach the
# bot). The ring buffer below is the pull alternative: every match-end
# event is appended with a monotonic id, and a remote bot polls
# ``GET /bot/taunts?since=<cursor>`` to drain new events. The push
# webhook is unchanged — both paths fire on every match end so a local
# bot (push) and a remote bot (poll) can each react.
_TAUNT_BUFFER_MAX = 200
_taunt_lock = threading.Lock()
_taunt_events: Deque[dict] = deque(maxlen=_TAUNT_BUFFER_MAX)
_taunt_next_id: int = 1


def _record_taunt_event(
    reason: str,
    challenger_discord_id: str,
    challenger_tag: str,
    channel_id: str,
    winner: str | None,
) -> None:
    """Append a match-end event to the ring buffer so polling consumers can
    drain it. Events without a ``channel_id`` are dropped because neither
    the push nor the poll bot would have a Discord channel to post in."""
    if not channel_id:
        return
    global _taunt_next_id
    with _taunt_lock:
        _taunt_events.append({
            "id": _taunt_next_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "winner": winner,
            "challenger_id": challenger_discord_id,
            "challenger_tag": challenger_tag,
            "channel_id": channel_id,
        })
        _taunt_next_id += 1


def _fire_taunt(
    reason: str,
    challenger_discord_id: str,
    challenger_tag: str,
    channel_id: str,
    winner: str | None,
) -> None:
    """POST the match outcome to the configured taunt webhook.

    Fires for every match end — completed, timed_out, disconnected — so the
    bot can maintain a per-user W/L record independent of whether it also
    wants to heckle. Fire-and-forget: runs in a daemon thread so a slow
    bot can't stall match cleanup. No-ops if no webhook configured or no
    channel_id on the match.
    """
    allow = load_allowlist()
    url = (allow.get("taunt_webhook_url") or "").strip()
    if not url:
        logging.info(
            "[bot-taunt] no webhook configured — skipping (reason=%s, channel=%s)",
            reason, channel_id,
        )
        return
    if not channel_id:
        logging.info(
            "[bot-taunt] no channel_id on match — skipping (reason=%s)", reason,
        )
        return

    payload = {
        "reason": reason,
        "winner": winner,
        "challenger_id": challenger_discord_id,
        "challenger_tag": challenger_tag,
        "channel_id": channel_id,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {allow.get('taunt_webhook_secret', '')}",
        },
        method="POST",
    )

    def _send():
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status >= 400:
                    logging.warning(
                        "[bot-taunt] webhook %s returned %s", url, resp.status,
                    )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logging.warning("[bot-taunt] webhook %s failed: %s", url, e)

    threading.Thread(target=_send, daemon=True).start()


def _start_match_watchdog(
    *,
    process_id: str,
    match_id: str | None,
    timeout_sec: int,
) -> None:
    """Poll the netplay subprocess's captured stdout for ``[MATCH_STARTED]``.

    If the sentinel doesn't appear within ``timeout_sec`` and the process
    is still running, we assume the opponent never connected, kill it,
    and tag the outcome as ``timed_out``. The completion callback reads
    the same shared state to decide ``completed`` vs ``disconnected``.
    """
    bs_store = get_bot_state()
    shared = {"started": False, "override": None}

    def _on_exit(info):
        winner, ended = _extract_game_result(info.log_lines)
        if shared["override"]:
            reason = shared["override"]
        elif ended == "disconnect":
            # Opponent bailed mid-match. Subprocess may have exited
            # cleanly (loop broke after [GAME_RESULT]) but the human
            # still dropped — surface as disconnected so the heckle
            # fires based on the running W/L record.
            reason = "disconnected"
        elif shared["started"] and info.status == "completed":
            reason = "completed"
        else:
            reason = "disconnected"
        snap = bs_store.snapshot()
        active = snap.get("match") or {}
        challenger_id = active.get("challenger_discord_id", "")
        challenger_tag = active.get("challenger_tag", "")
        channel_id = active.get("channel_id", "")
        bs_store.clear_match(reason=reason)
        # Fire on every match end so the bot can update its per-user W/L
        # record; the bot itself decides whether to post or stay silent
        # (e.g. a normal completion is usually a quiet record update).
        # Record first (for polling consumers) then push (for the locally
        # configured webhook). Both paths see the same event.
        _record_taunt_event(reason, challenger_id, challenger_tag, channel_id, winner)
        _fire_taunt(reason, challenger_id, challenger_tag, channel_id, winner)

    process_manager.on_complete(process_id, _on_exit)

    def _watch():
        deadline = time.monotonic() + timeout_sec
        log_offset = 0
        while time.monotonic() < deadline:
            info = process_manager.get(process_id)
            if info is None or info.status != "running":
                return
            new_lines = info.get_logs(log_offset)
            log_offset += len(new_lines)
            for line in new_lines:
                if _MATCH_STARTED_SENTINEL in line:
                    shared["started"] = True
                    return
            time.sleep(_WATCHDOG_POLL_SEC)
        info = process_manager.get(process_id)
        if info is None or info.status != "running" or shared["started"]:
            return
        shared["override"] = "timed_out"
        logging.warning(
            "[bot-watchdog] no connect within %ss — killing %s (match_id=%s)",
            timeout_sec, process_id, match_id,
        )
        process_manager.stop(process_id)

    threading.Thread(target=_watch, daemon=True).start()


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
        return None, _dolphin_path_error(use_bot_vs_human, headless)

    use_headless, wrap_xvfb, headless_err = _plan_headless(dolphin, headless)
    if headless_err:
        return None, headless_err

    abs_path = _resolve_agent_path(cfg, source, rec["agent_path"])

    # Pick a name from the model's cached name_map if available — helps
    # the netplay script load the right sub-policy for styled models.
    names = rec.get("names") or []
    agent_name = body.style_name if body.style_name in names else ""

    # Resolve delay: ``force_online_delay`` (local override) wins when set;
    # otherwise fall back to the committed defaults' delay/auto_delay pair.
    # Matches the force_lan_ip/force_port convention — blank/0 = disabled.
    force_delay_raw = defaults.get("force_online_delay")
    forced_delay: int | None = None
    if force_delay_raw not in (None, "", 0):
        try:
            forced_delay = int(force_delay_raw)
        except (ValueError, TypeError):
            forced_delay = None
    if forced_delay is not None:
        delay = forced_delay
        auto_delay = False
    else:
        delay = int(defaults.get("delay", 2))
        auto_delay = bool(defaults.get("auto_delay", True))

    result = launch_netplay_session(
        cfg=cfg,
        match_store=s.match_store,
        agent_path=rec["agent_path"],
        abs_agent_path=abs_path,
        agent_name=agent_name,
        character=body.character,
        connect_code=body.connect_code,
        delay=delay,
        auto_delay=auto_delay,
        sample_temperature=float(defaults.get("sample_temperature", 1.0)),
        save_replays=bool(defaults.get("save_replays", True)),
        # LAN test-mode overrides — blank strings disable, so production bot
        # traffic is unaffected. Set in Bot Models panel → Test mode (LAN)
        # to point the subprocess at a local opponent Dolphin for self-test.
        force_port=str(defaults.get("force_port", "") or ""),
        force_lan_ip=str(defaults.get("force_lan_ip", "") or ""),
        use_headless=use_headless,
        wrap_xvfb=wrap_xvfb,
        dolphin=dolphin,
        iso=iso,
        # No on_complete here — _start_match_watchdog installs its own
        # completion hook that decides the outcome reason and calls
        # clear_match() itself.
    )
    if result.error:
        return None, result.error
    if result.process_id:
        _start_match_watchdog(
            process_id=result.process_id,
            match_id=result.match_id,
            timeout_sec=int(defaults.get("challenge_timeout_sec", 180)),
        )
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
        "allow_any_challenger": allow.get("allow_any_challenger", False),
        "backend_url": base,
        "taunt_webhook_url": allow.get("taunt_webhook_url", ""),
        "taunt_webhook_secret": allow.get("taunt_webhook_secret", ""),
        "endpoints": [
            {"method": "GET",  "path": "/bot/status",
             "desc": "Poll presence, active match, pending challenges"},
            {"method": "GET",  "path": "/bot/roster",
             "desc": "List approved (character, style_name) combos"},
            {"method": "POST", "path": "/bot/launch",
             "desc": "Request a match — body: {challenger_discord_id, challenger_username?, character, style_name, connect_code, channel_id?}"},
            {"method": "POST", "path": "/bot/approve",
             "desc": "Approve or deny a pending challenge (auth bot only — GUI usually handles this)"},
        ],
    }


@router.post("/integration/challenger-mode", dependencies=[Depends(_require_loopback)])
def set_challenger_mode(body: ChallengerModeRequest):
    """Toggle the per-user allowlist gate. When ``allow_any_challenger`` is
    True, ``/bot/launch`` accepts any challenger whose request is forwarded
    by a token-auth'd bot — useful for opening up the bot to a whole
    Discord server without adding every user individually."""
    merged = save_allowlist({"allow_any_challenger": body.allow_any_challenger})
    return {"allow_any_challenger": merged["allow_any_challenger"]}


@router.post("/integration/taunt-webhook", dependencies=[Depends(_require_loopback)])
def set_taunt_webhook(body: TauntWebhookRequest):
    """Configure the URL the launcher POSTs to on match timeout/disconnect.
    Loopback-only — only the GUI running on the same machine should be able
    to redirect taunts to a new endpoint."""
    merged = save_allowlist({"taunt_webhook_url": body.taunt_webhook_url.strip()})
    return {
        "taunt_webhook_url": merged["taunt_webhook_url"],
        "taunt_webhook_secret": merged["taunt_webhook_secret"],
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


@router.post("/integration/force-clear-match", dependencies=[Depends(_require_loopback)])
def force_clear_match():
    """Unconditionally clear bot_state.match. Recovery for the case where
    the netplay subprocess died before its completion callback fired (or
    was killed out-of-band), leaving bot_state stuck with in_match=true
    and no real Dolphin running. The GUI's Stop button calls this after
    /play/end-match so the LRA+Start sentinel still gets a chance to
    cleanly quit any subprocess that's somehow still alive."""
    get_bot_state().clear_match(reason="aborted")
    return {"ok": True}


@router.get("/integration/models", dependencies=[Depends(_require_loopback)])
def get_models_config():
    """Return the current approved-model roster and defaults for the GUI
    editor. Annotates each entry with the resolved agent record (nickname,
    filename, missing flag) so the UI can render without a second round-trip
    to /agents/."""
    cfg = load_models_config()
    approved = []
    for entry in cfg.get("approved_models", []):
        rec = _agent_record(entry.get("agent_id", ""), entry.get("source", "agents"))
        approved.append({
            **entry,
            "agent_resolved": bool(rec and not rec.get("missing")),
            "agent_filename": (rec or {}).get("agent_path", ""),
            "agent_nickname": (rec or {}).get("nickname", ""),
        })
    return {
        "approved_models": approved,
        "defaults": cfg.get("defaults", {}),
    }


@router.put("/integration/models", dependencies=[Depends(_require_loopback)])
def put_models_config(body: ModelsConfigRequest):
    """Replace approved_models and/or defaults in bot_models.json.

    The GUI sends the full list on each save (simpler than diffing). Any
    non-schema fields (like the hand-written ``_note``) are preserved by
    ``save_models_config``.
    """
    approved = (
        [m.model_dump() for m in body.approved_models]
        if body.approved_models is not None else None
    )
    return save_models_config(
        approved_models=approved,
        defaults=body.defaults,
    )


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


@router.get("/taunts", dependencies=[Depends(_require_token)])
def list_taunts(since: int = 0):
    """Pull match-end events newer than ``since`` (defaults to all
    retained). Companion to the push webhook — a bot that can't host an
    inbound URL polls this every few seconds and tracks the returned
    ``cursor`` locally. Events older than the ring buffer window are
    dropped; a client that falls more than ``_TAUNT_BUFFER_MAX`` events
    behind will silently miss the overflow, which is acceptable for
    trash-talk."""
    with _taunt_lock:
        events = [e for e in _taunt_events if e["id"] > since]
        cursor = _taunt_next_id - 1
    return {"events": events, "cursor": cursor}


@router.post("/launch", dependencies=[Depends(_require_token)])
def launch(body: LaunchRequest):
    bs_store = get_bot_state()
    snap = bs_store.snapshot()

    if snap["state"] == "offline":
        raise HTTPException(status_code=409, detail={"reason": "offline"})

    allow = load_allowlist()
    if not allow.get("allow_any_challenger", False):
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
                channel_id=body.channel_id,
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
        channel_id=body.channel_id,
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
        channel_id=p.channel_id,
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
        channel_id=p.channel_id,
    ))
    bs_store.resolve(p.challenge_id, "approved", match_id=match_id, headless=body.headless)
    return {"status": "approved", "match_id": match_id, "headless": body.headless}


@router.get("/challenge/{challenge_id}", dependencies=[Depends(_require_token)])
def get_challenge(challenge_id: str):
    return get_bot_state().lookup_challenge(challenge_id)


@router.post("/challenge/withdraw", dependencies=[Depends(_require_token)])
def withdraw_challenge(body: WithdrawRequest):
    """Cancel the caller's own pending challenge. Keyed by Discord ID so
    the bot doesn't need to remember the opaque challenge_id across its
    own restarts; the bot is trusted to only send this for the Discord
    user who actually clicked /withdraw."""
    bs_store = get_bot_state()
    p = bs_store.pop_pending_by_challenger(body.challenger_discord_id)
    if not p:
        raise HTTPException(status_code=404, detail="no pending challenge")
    bs_store.resolve(p.challenge_id, "withdrawn")
    return {"status": "withdrawn", "challenge_id": p.challenge_id}


@router.post("/end-match", dependencies=[Depends(_require_loopback)])
def end_match_bot():
    """GUI-initiated clean end of the active bot match. Loopback-only — a
    leaked Bearer token shouldn't let a remote caller end Paul's match
    mid-game. The Discord bot should not call this endpoint."""
    cfg = get_state().cfg
    path = touch_end_match_sentinel(cfg)
    return {"ok": True, "sentinel": str(path)}
