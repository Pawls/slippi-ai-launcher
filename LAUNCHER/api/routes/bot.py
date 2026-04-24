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
import os
import threading
import time
import urllib.error
import urllib.parse
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
    QUEUE_TTL_SECONDS,
    get_bot_state,
    load_allowlist,
    load_models_config,
    rotate_api_token,
    save_allowlist,
    save_models_config,
)
from LAUNCHER.netplay_launcher import (
    clear_series_state,
    launch_netplay_session,
    touch_end_match_sentinel,
    write_series_state,
)


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


class LiveEventIn(BaseModel):
    """Payload the in-subprocess observer POSTs on each detected event.

    Fields match ``slippi_ai.live_events.LiveEvent.to_dict()``. The
    launcher enriches with ``id``, ``match_id``, ``timestamp``, and the
    challenger / channel context before forwarding or buffering."""
    type: str
    frame: int
    player_port: int
    stats: dict
    text_hint: str = ""
    severity: str = "medium"


class LiveEventsConfigRequest(BaseModel):
    """Full live-events config block. Sent by the GUI on save; the
    launcher validates via ``_merge_live_events_config`` so a partial
    payload (e.g. only the master toggle flipped) can't wipe thresholds."""
    enabled: bool | None = None
    max_per_match: int | None = None
    types: dict | None = None


# ── Helpers ────────────────────────────────────────────────────────────

def _onedrive_roots() -> list[str]:
    """Known OneDrive sync roots on this machine. Reads the env vars
    Windows sets (``OneDrive``, ``OneDriveConsumer``, ``OneDriveCommercial``)
    plus any extras that match the OneDrive naming convention. Returns
    absolute, normalised paths — comparisons use ``os.path.normcase``."""
    roots: list[str] = []
    for key in (
        "OneDrive", "OneDriveConsumer", "OneDriveCommercial",
        "OneDriveBusiness", "OneDrivePersonal",
    ):
        val = (os.environ.get(key) or "").strip()
        if val:
            roots.append(os.path.normcase(os.path.abspath(val)))
    # Catch any other "OneDrive - <org>" style env keys that Windows
    # creates for additional work/school accounts.
    for k, v in os.environ.items():
        if k.startswith("OneDrive") and v:
            p = os.path.normcase(os.path.abspath(v))
            if p not in roots:
                roots.append(p)
    return roots


def _is_onedrive_path(path: str) -> bool:
    """True if ``path`` is inside a known OneDrive sync root. Uses prefix
    match on normalised absolute paths so subfolders (incl. redirected
    Documents/Desktop/Pictures when OneDrive "Folder backup" is on) are
    detected.

    Note: OneDrive can also back up arbitrary folders — we only catch the
    conventional ones. A user who's explicitly backing up some
    non-OneDrive path gets a warning only from inside the GUI; this
    backend check is the last-resort gate."""
    if not path:
        return False
    try:
        norm = os.path.normcase(os.path.abspath(path))
    except (ValueError, OSError):
        return False
    # Heuristic 1: any path segment contains "OneDrive". Covers the case
    # where env vars are missing (fresh-boot, non-default install) but
    # the path literally says OneDrive.
    lower = norm.lower()
    if "\\onedrive" in lower or "/onedrive" in lower or lower.startswith("onedrive"):
        return True
    # Heuristic 2: known OneDrive sync roots from env vars.
    for root in _onedrive_roots():
        if norm == root or norm.startswith(root + os.sep):
            return True
    return False


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
    series_result: dict | None = None,
) -> None:
    """Append a match-end event to the ring buffer so polling consumers can
    drain it. Events without a ``channel_id`` are dropped because neither
    the push nor the poll bot would have a Discord channel to post in.

    ``series_result`` is the Bo5 set tally produced by the launcher's
    stdout tailer: ``{"ai_wins": N, "human_wins": M, "decided_by":
    "ai"|"human"|None, "last": [...]}``. None means the match wasn't
    part of a contested set; bots should fall back to per-game W/L.
    ``reason`` also covers queue events — ``"promoted"`` (a queued
    challenger's turn started) and ``"skipped"`` (a queued challenger
    was dropped, usually because their model became unapproved or
    their connect window timed out).
    """
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
            "series_result": series_result,
        })
        _taunt_next_id += 1


def _fire_taunt(
    reason: str,
    challenger_discord_id: str,
    challenger_tag: str,
    channel_id: str,
    winner: str | None,
    series_result: dict | None = None,
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
        "series_result": series_result,
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


# ── Live mid-match events ─────────────────────────────────────────────
#
# The netplay subprocess runs a cheap observer thread that detects
# notable opponent behaviors (shield-grab spam, roll spam, ledge camp,
# smash spam) and POSTs events here. We tag the event with the active
# match context, enforce per-type cooldowns and a per-match cap
# (authoritative — the observer also has its own per-detector cooldown,
# this is belt-and-suspenders against a buggy detector), append to a
# ring buffer for polling consumers (friend's LLM bot over Tailscale),
# and forward to the local bot's webhook for immediate Discord posts.
#
# Events that arrive with no active match (late-arriving drain after
# subprocess exit) are dropped silently — not an error.

_LIVE_BUFFER_MAX = 500
_live_lock = threading.Lock()
_live_events: Deque[dict] = deque(maxlen=_LIVE_BUFFER_MAX)
_live_next_id: int = 1
# (match_id, event_type) -> monotonic timestamp of last dispatch.
# Cleared per-match in ``_clear_live_event_state``.
_live_cooldowns: dict[tuple[str, str], float] = {}
# match_id -> number of events dispatched so far this match.
_live_counts: dict[str, int] = defaultdict(int)


def _clear_live_event_state(match_id: str) -> None:
    """Drop per-match cooldown/count bookkeeping. Called on match end so
    a fresh match starts with a clean cooldown slate and can fire up to
    ``max_per_match`` events again."""
    if not match_id:
        return
    with _live_lock:
        for key in list(_live_cooldowns):
            if key[0] == match_id:
                _live_cooldowns.pop(key, None)
        _live_counts.pop(match_id, None)


def _derive_live_event_url(taunt_url: str) -> str:
    """Derive the bot's live-event webhook URL from its taunt webhook
    URL by swapping the path — user only configures one host/port but
    live events land on ``/live-event`` instead of ``/taunt``."""
    if not taunt_url:
        return ""
    try:
        parts = urllib.parse.urlsplit(taunt_url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/live-event", "", ""))


def _record_live_event(event: dict) -> dict:
    """Assign a monotonic id and append to the ring buffer. Returns the
    event as stored (with ``id`` filled in)."""
    global _live_next_id
    with _live_lock:
        event["id"] = _live_next_id
        _live_next_id += 1
        _live_events.append(event)
    return event


def _forward_live_event_to_bot(event: dict) -> None:
    """POST the event to the configured bot webhook in a daemon thread.

    Mirrors ``_fire_taunt``'s fire-and-forget shape: a slow / unreachable
    bot must not back up the launcher's request handling. No-ops if no
    webhook is configured or no ``channel_id`` is attached."""
    if not event.get("channel_id"):
        return
    allow = load_allowlist()
    url = _derive_live_event_url((allow.get("taunt_webhook_url") or "").strip())
    if not url:
        return
    body = json.dumps(event).encode("utf-8")
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
                        "[bot-live] webhook %s returned %s", url, resp.status,
                    )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logging.warning("[bot-live] webhook %s failed: %s", url, e)

    threading.Thread(target=_send, daemon=True).start()


def _parse_game_result_line(line: str) -> str | None:
    """Extract the winner from a single [GAME_RESULT] stdout line, or
    None if the line isn't one or is malformed. Split out from
    ``_extract_game_result`` so the live stdout tailer can react to each
    game as it's reported, not just to the final result at exit."""
    idx = line.find(_GAME_RESULT_PREFIX)
    if idx < 0:
        return None
    tail = line[idx + len(_GAME_RESULT_PREFIX):]
    for tok in tail.split():
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        if k == "winner" and v in ("ai", "human", "draw"):
            return v
    return None


def _write_series_state_for_match() -> None:
    """Rewrite the Bo5 handshake file based on the current BotState.

    The file tells the netplay subprocess whether the current Dolphin
    session is in contested-set mode. The subprocess reads it on menu
    frames and decides when to fire "one more" or close out the set.
    Safe to call from any thread — the config and bot_state reads are
    both lock-protected.
    """
    cfg = get_state().cfg
    snap = get_bot_state().match_snapshot()
    if not snap:
        clear_series_state(cfg)
        return
    write_series_state(cfg, {
        "bo5_active": bool(snap.get("bo5_active")),
    })


def _series_result_payload(snap: dict, reason: str) -> dict | None:
    """Produce the ``series_result`` field for a taunt event. Returns
    None when the match wasn't contested (bo5 was never flipped on) so
    the Discord bot falls back to the existing per-game heckle.

    ``outcome`` values:
      - ``bot_won_set`` / ``bot_lost_set``: sliding-5 window clinched
        for one side before Dolphin exited.
      - ``set_cancelled``: Dolphin ended (timeout / user stop /
        disconnect) with neither side at threshold — the set is
        abandoned.
    """
    if not snap or not snap.get("bo5_active"):
        return None
    tally = snap.get("tally") or {}
    decided = tally.get("decided_by")
    if decided == "ai":
        outcome = "bot_won_set"
    elif decided == "human":
        outcome = "bot_lost_set"
    else:
        outcome = "set_cancelled"
    return {
        "ai_wins": int(tally.get("ai", 0)),
        "human_wins": int(tally.get("human", 0)),
        "draws": int(tally.get("draws", 0)),
        "last": list(tally.get("last") or []),
        "decided_by": decided,
        "outcome": outcome,
    }


def _promote_next_challenger() -> None:
    """Pop the head of the queue and start their match. Called from the
    match-exit completion hook, so no active match is running when this
    runs. Skips pending entries whose model became unapproved or whose
    launch fails, firing a ``skipped`` event each time so the challenger
    learns why they didn't get their turn. Writes the Bo5 handshake
    file appropriate to the new queue depth (bo5_active=True iff there
    are still queued players behind the promoted one).
    """
    bs_store = get_bot_state()
    cfg = get_state().cfg
    while True:
        p = bs_store.peek_next_pending()
        if p is None:
            clear_series_state(cfg)
            return

        entry = _find_approved(p.character, p.style_name)
        if not entry:
            popped = bs_store.pop_next_pending()
            if popped is None:
                continue
            bs_store.resolve(popped.challenge_id, "skipped")
            _record_taunt_event(
                "skipped", popped.challenger_discord_id,
                popped.challenger_tag, popped.channel_id, None,
            )
            _fire_taunt(
                "skipped", popped.challenger_discord_id,
                popped.challenger_tag, popped.channel_id, None,
            )
            continue

        popped = bs_store.pop_next_pending()
        if popped is None:
            continue

        launch_body = LaunchRequest(
            challenger_discord_id=popped.challenger_discord_id,
            challenger_tag=popped.challenger_tag,
            connect_code=popped.connect_code,
            character=popped.character,
            style_name=popped.style_name,
            channel_id=popped.channel_id,
        )
        match_id, err = _launch_for(entry, launch_body, headless=True)
        if err:
            logging.warning(
                "[bot-queue] promotion launch failed for %s: %s",
                popped.challenger_discord_id, err,
            )
            bs_store.resolve(popped.challenge_id, "skipped")
            _record_taunt_event(
                "skipped", popped.challenger_discord_id,
                popped.challenger_tag, popped.channel_id, None,
            )
            _fire_taunt(
                "skipped", popped.challenger_discord_id,
                popped.challenger_tag, popped.channel_id, None,
            )
            continue

        bs_store.set_match(ActiveMatch(
            match_id=match_id or "",
            challenger_discord_id=popped.challenger_discord_id,
            challenger_tag=popped.challenger_tag,
            character=popped.character,
            style_name=popped.style_name,
            connect_code=popped.connect_code,
            headless=True,
            started_at=datetime.now(timezone.utc).isoformat(),
            channel_id=popped.channel_id,
        ))
        # If more challengers are still queued behind the one we just
        # promoted, they're still contesting: start the new match in
        # Bo5 mode and publish the handshake file so the subprocess
        # picks it up on its first menu frame.
        if bs_store.queue_depth() > 0:
            bs_store.set_bo5_active(True)
        _write_series_state_for_match()
        bs_store.resolve(popped.challenge_id, "promoted", match_id=match_id)
        _record_taunt_event(
            "promoted", popped.challenger_discord_id,
            popped.challenger_tag, popped.channel_id, None,
        )
        _fire_taunt(
            "promoted", popped.challenger_discord_id,
            popped.challenger_tag, popped.channel_id, None,
        )
        return


def _start_match_watchdog(
    *,
    process_id: str,
    match_id: str | None,
    timeout_sec: int,
) -> None:
    """Poll the netplay subprocess's captured stdout for ``[MATCH_STARTED]``
    and for every ``[GAME_RESULT]`` line the subprocess emits.

    - ``[MATCH_STARTED]`` disarms the no-connect watchdog that otherwise
      kills a stuck-matchmaking session after ``timeout_sec``.
    - Each ``[GAME_RESULT]`` is appended to the active match's running
      Bo5 tally (``ActiveMatch.game_results``) so a subsequent
      ``/bot/launch`` getting queued sees the up-to-date set score.
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
        snap = bs_store.match_snapshot()
        active = snap or {}
        challenger_id = active.get("challenger_discord_id", "")
        challenger_tag = active.get("challenger_tag", "")
        channel_id = active.get("channel_id", "")
        ending_match_id = active.get("match_id", "")
        series_result = _series_result_payload(snap, reason)
        bs_store.clear_match(reason=reason)
        # Drop the per-match live-event cooldown / cap bookkeeping so a
        # subsequent match starts fresh.
        _clear_live_event_state(ending_match_id)
        # Fire on every match end so the bot can update its per-user W/L
        # record; the bot itself decides whether to post or stay silent
        # (e.g. a normal completion is usually a quiet record update).
        # Record first (for polling consumers) then push (for the locally
        # configured webhook). Both paths see the same event.
        _record_taunt_event(
            reason, challenger_id, challenger_tag, channel_id,
            winner, series_result=series_result,
        )
        _fire_taunt(
            reason, challenger_id, challenger_tag, channel_id,
            winner, series_result=series_result,
        )
        # Promotion runs after taunt so the outgoing match's summary
        # hits Discord before the next challenger's "you're up" ping.
        try:
            _promote_next_challenger()
        except Exception:
            # Never let a promotion bug prevent the completion callback
            # from returning — a stuck callback breaks process_manager's
            # bookkeeping for future matches.
            logging.exception("[bot-queue] promotion flow crashed")

    process_manager.on_complete(process_id, _on_exit)

    def _watch():
        """Live tailer: runs for the life of the subprocess. Handles
        two separate duties:
          - Pre-start: bounded by ``timeout_sec`` no-connect watchdog.
          - Post-start: feed each [GAME_RESULT] into the Bo5 tally.
        Exits when the subprocess stops running."""
        deadline = time.monotonic() + timeout_sec
        log_offset = 0
        while True:
            info = process_manager.get(process_id)
            if info is None or info.status != "running":
                return
            new_lines = info.get_logs(log_offset)
            log_offset += len(new_lines)
            for line in new_lines:
                if not shared["started"] and _MATCH_STARTED_SENTINEL in line:
                    shared["started"] = True
                winner = _parse_game_result_line(line)
                if winner is not None:
                    bs_store.record_game_result(winner)
            # No-connect watchdog: only enforced until the subprocess
            # announces the match actually started.
            if not shared["started"] and time.monotonic() >= deadline:
                info = process_manager.get(process_id)
                if info is None or info.status != "running":
                    return
                shared["override"] = "timed_out"
                logging.warning(
                    "[bot-watchdog] no connect within %ss — killing %s "
                    "(match_id=%s)",
                    timeout_sec, process_id, match_id,
                )
                process_manager.stop(process_id)
                return
            time.sleep(_WATCHDOG_POLL_SEC)

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

    # Display kwargs: let the user tune the local Dolphin window they'll
    # spectate the bot through (internal resolution, audio backend, DSP
    # mode, or kill audio outright for CPU headroom). Only meaningful when
    # the process actually renders/plays audio, so gate on non-headless —
    # headless mode ignores all of these anyway.
    display_kwargs: dict[str, object] = {}
    if not use_headless:
        # Default to 1x native when the key is absent — fresh installs
        # (bot_models.json hasn't seen a UI save yet) would otherwise
        # inherit whatever resolution the Dolphin build defaults to
        # (3x on standard Slippi netplay), which fights the user's
        # intent to spectate at native. The UI exposes Auto (value 0)
        # as the escape hatch when you want Dolphin's default.
        res_raw = defaults.get("internal_resolution")
        if res_raw is None:
            res = 1
        else:
            try:
                res = int(res_raw)
            except (ValueError, TypeError):
                res = 0
        if res > 0:
            display_kwargs["internal_resolution"] = res

        audio_backend = str(defaults.get("audio_backend", "") or "").strip()
        if audio_backend:
            display_kwargs["audio_backend"] = audio_backend
        audio_emulation = str(defaults.get("audio_emulation", "") or "").strip()
        if audio_emulation:
            display_kwargs["audio_emulation"] = audio_emulation

        # Default audio off on fresh installs — the audio-thread CPU
        # contention was the root cause of the prior out-of-sync bug.
        # Explicit False from the user (re-enables audio) is respected.
        disable_audio_raw = defaults.get("disable_audio")
        if disable_audio_raw is None:
            disable_audio = True
        else:
            disable_audio = bool(disable_audio_raw)
        if disable_audio:
            display_kwargs["disable_audio"] = True

    # Replay dir override: refuse to launch into a OneDrive sync folder.
    # OneDrive syncs each .slp as it's being written, producing huge I/O
    # hitches that manifest as mid-match stalls — exactly the failure
    # mode that triggered this fix. The GUI warns as the user types;
    # this is the backend safety net in case they bypass it.
    replay_dir = str(defaults.get("replay_dir", "") or "").strip()
    if replay_dir:
        if _is_onedrive_path(replay_dir):
            return None, (
                f"replay_dir {replay_dir!r} is inside a OneDrive-synced "
                "folder. Background sync during a match causes heavy I/O "
                "lag; pick a non-OneDrive path (e.g. C:\\Melee\\replays)."
            )
        display_kwargs["replay_dir"] = replay_dir

    # Live-events config: only serialize when the master toggle is on.
    # Empty string keeps the subprocess flag absent, so netplay.py takes
    # the zero-cost branch and never starts an observer thread.
    live_events_cfg = load_allowlist().get("live_events") or {}
    live_events_json = (
        json.dumps(live_events_cfg) if live_events_cfg.get("enabled") else ""
    )

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
        # Stall detector in netplay.py needs polling mode to observe peer
        # disconnects — without it, dolphin.next_gamestate() blocks forever
        # once the peer stops sending frames.
        console_timeout=1.0,
        live_events_config=live_events_json,
        dolphin=dolphin,
        iso=iso,
        **display_kwargs,
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
            # 90s default covers "I saw the Discord ping, launching
            # Slippi now" with headroom. Reused for both initial
            # challengers and queue-promoted ones so a no-show promoted
            # player is skipped on the same clock.
            timeout_sec=int(defaults.get("challenge_timeout_sec", 90)),
        )
    return result.match_id, None


# ── Routes ─────────────────────────────────────────────────────────────

@router.get("/local-token")
def get_local_token():
    """Return the bearer token so the GUI can read it without the user
    copy-pasting from ``bot_allowlist.json``. The launcher binds to
    ``127.0.0.1`` — this endpoint is implicitly loopback-only."""
    return {"api_token": load_allowlist().get("api_token", "")}


# ── Live events (observer → launcher → bots) ──────────────────────────
#
# POST /bot/live-event — loopback-only, called by the observer thread
# inside the netplay subprocess. The launcher filters (master toggle,
# per-type enabled, per-match cooldown, per-match cap), enriches with
# match context, buffers for polling consumers, and forwards to the
# local bot webhook.
#
# GET /bot/live-events?since=<cursor> — token-authed, polled by remote
# LLM bots that can't easily host an inbound webhook (same pattern as
# /bot/taunts).

@router.post("/live-event", dependencies=[Depends(_require_loopback)])
def ingest_live_event(body: LiveEventIn):
    allow = load_allowlist()
    live_cfg = allow.get("live_events") or {}
    if not live_cfg.get("enabled", False):
        # Master toggle off — the observer shouldn't even have started,
        # but belt-and-suspenders. Silently no-op.
        return {"accepted": False, "reason": "disabled"}

    types_cfg = live_cfg.get("types") or {}
    type_cfg = types_cfg.get(body.type) or {}
    if not type_cfg.get("enabled", True):
        return {"accepted": False, "reason": "type_disabled"}

    snap = get_bot_state().snapshot()
    active = snap.get("match") or {}
    match_id = active.get("match_id", "")
    if not match_id:
        # Late arrival after the subprocess exited and clear_match ran,
        # or the subprocess somehow got ahead of set_match. Not an error
        # path — drop silently.
        return {"accepted": False, "reason": "no_active_match"}

    cooldown_sec = float(type_cfg.get("cooldown_sec", 30.0))
    max_per_match = int(live_cfg.get("max_per_match", 5) or 0)
    now = time.monotonic()

    with _live_lock:
        last_fire = _live_cooldowns.get((match_id, body.type))
        if last_fire is not None and (now - last_fire) < cooldown_sec:
            return {"accepted": False, "reason": "cooldown"}
        if max_per_match and _live_counts[match_id] >= max_per_match:
            return {"accepted": False, "reason": "cap"}
        _live_cooldowns[(match_id, body.type)] = now
        _live_counts[match_id] += 1

        enriched = {
            "type": body.type,
            "severity": body.severity,
            "frame": body.frame,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "match_id": match_id,
            "player_port": body.player_port,
            "stats": dict(body.stats),
            "text_hint": body.text_hint,
            "challenger_id": active.get("challenger_discord_id", ""),
            "challenger_tag": active.get("challenger_tag", ""),
            "channel_id": active.get("channel_id", ""),
        }
        global _live_next_id
        enriched["id"] = _live_next_id
        _live_next_id += 1
        _live_events.append(enriched)
        event_count = _live_counts[match_id]

    # Forward outside the lock so a slow bot webhook can't stall the
    # ingest path for concurrent events (rare at the configured rate,
    # but the isolation is cheap).
    _forward_live_event_to_bot(enriched)

    return {
        "accepted": True,
        "id": enriched["id"],
        "match_event_count": event_count,
    }


@router.get("/live-events", dependencies=[Depends(_require_token)])
def get_live_events(since: int = 0, limit: int = 100):
    """Drain events with id > ``since``. Remote bots poll this with
    their last seen id; the ring buffer (size 500) is generous enough
    that a brief network hiccup won't lose events for a sensibly-polled
    consumer."""
    if limit <= 0 or limit > _LIVE_BUFFER_MAX:
        limit = _LIVE_BUFFER_MAX
    with _live_lock:
        items = [ev for ev in _live_events if ev["id"] > since][:limit]
        cursor = items[-1]["id"] if items else since
    return {"events": items, "cursor": cursor}


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
            {"method": "GET",  "path": "/bot/taunts?since=<cursor>",
             "desc": "Drain match-end taunt events (ring buffer; polling alternative to the push webhook)"},
            {"method": "GET",  "path": "/bot/live-events?since=<cursor>",
             "desc": "Drain mid-match live events (shield-grab spam, roll spam, ledge camp, smash spam). Polling alternative for remote bots."},
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


@router.get("/integration/live-events", dependencies=[Depends(_require_loopback)])
def get_live_events_config():
    """Return the full live-events config for the GUI editor. The
    defaults come from the detectors module, so a fresh install sees
    sensible thresholds without the user having to author them."""
    return load_allowlist().get("live_events") or {}


@router.put("/integration/live-events", dependencies=[Depends(_require_loopback)])
def put_live_events_config(body: LiveEventsConfigRequest):
    """Persist the live-events config. Partial payloads are OK — the
    launcher's ``save_allowlist`` round-trips through
    ``_merge_live_events_config`` so unspecified fields inherit from
    the current config and malformed fields fall back to defaults."""
    payload = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    merged = save_allowlist({"live_events": payload})
    return merged.get("live_events") or {}


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


@router.get("/queue", dependencies=[Depends(_require_token)])
def get_queue():
    """Current queue depth + active challenger's set tally. The Discord
    bot's ``/queue`` slash command renders this; pending positions are
    1-indexed in the queue itself but the active match is reported
    separately so the caller can display "playing now" vs "in line"."""
    bs_store = get_bot_state()
    snap = bs_store.match_snapshot()
    active = None
    if snap:
        active = {
            "challenger_id": snap.get("challenger_discord_id", ""),
            "challenger_tag": snap.get("challenger_tag", ""),
            "set": _current_set_payload(snap),
        }
    return {
        "active": active,
        "queue": bs_store.queue_entries(),
    }


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

    entry = _find_approved(body.character, body.style_name)
    if not entry:
        raise HTTPException(status_code=404, detail={"reason": "unknown_model"})

    # Approval-gated flow: still single-slot. Queueing only applies to
    # plain "available" mode, where the launcher accepts whoever the bot
    # forwards. Mixing approval prompts with a FIFO queue is confusing
    # both for the user (who'd need to see and decide on every entry)
    # and for the bot (which would have to distinguish queued vs pending
    # on every poll response).
    if snap["state"] == "available_with_approval":
        if snap["match"] is not None:
            raise HTTPException(status_code=409, detail={"reason": "busy"})
        if snap["pending"]:
            raise HTTPException(status_code=409, detail={"reason": "pending_approval"})
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

    # Plain "available" — queue any additional challengers behind the
    # currently-active match instead of 409ing them away.
    active_match = snap.get("match") or {}
    if active_match:
        # Same user can't queue against themselves — they're already
        # playing. Return a distinct reason so the bot can tell the
        # user "you're already in a match" rather than "queued".
        active_challenger = active_match.get("challenger_discord_id", "")
        if active_challenger.lower() == body.challenger_discord_id.lower():
            raise HTTPException(
                status_code=409, detail={"reason": "already_active"})
        # Dedup: one queue slot per Discord user. A re-request returns
        # the existing entry's position / challenge_id so the bot UI
        # can poll the same challenge without creating a phantom spot.
        for existing in bs_store.queue_entries():
            if (existing["challenger_discord_id"].lower()
                    == body.challenger_discord_id.lower()):
                return {
                    "status": "queued",
                    "position": existing["position"],
                    "already_queued": True,
                    "challenge_id": existing["challenge_id"],
                    "expires_at": existing["expires_at"],
                    "current_set": _current_set_payload(
                        bs_store.match_snapshot()),
                }

        try:
            p = bs_store.add_pending(
                challenger_discord_id=body.challenger_discord_id,
                challenger_tag=body.challenger_tag or body.challenger_discord_id,
                connect_code=body.connect_code,
                character=body.character,
                style_name=body.style_name,
                channel_id=body.channel_id,
                # Queue entries are bounded by the promotion chain + the
                # 90s no-show watchdog, not by a wall-clock expiry.
                ttl_seconds=QUEUE_TTL_SECONDS,
            )
        except RuntimeError:
            raise HTTPException(status_code=409, detail={"reason": "queue_full"})
        # Flip the active match into Bo5 mode now that there's somebody
        # waiting. The subprocess picks this up on its next menu-frame
        # poll and starts applying the sliding-5 cutoff.
        bs_store.set_bo5_active(True)
        _write_series_state_for_match()
        position = bs_store.queue_depth()
        return {
            "status": "queued",
            "position": position,
            "challenge_id": p.challenge_id,
            "expires_at": p.expires_at,
            "current_set": _current_set_payload(bs_store.match_snapshot()),
        }

    # No active match — launch immediately. If someone is somehow in the
    # pending queue (e.g. launcher restart left a stale entry), the FIFO
    # promotion path owns them; a brand-new /bot/launch goes to whoever
    # called in.
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
    # Fresh session starts uncontested; clear any stale handshake file
    # so the subprocess doesn't inherit a leftover Bo5 flag.
    clear_series_state(get_state().cfg)
    return {"status": "launching", "match_id": match_id}


def _current_set_payload(snap: dict) -> dict:
    """Shape the set state for ``queued`` and ``/bot/queue`` responses.
    Presents the tally in human-readable terms (per-side wins + the
    last-N game list) so the Discord bot can render "bot leads 2-1" or
    similar without recomputing anything."""
    if not snap:
        return {
            "ai_wins": 0, "human_wins": 0, "draws": 0,
            "last": [], "bo5_active": False,
        }
    tally = snap.get("tally") or {}
    return {
        "ai_wins": int(tally.get("ai", 0)),
        "human_wins": int(tally.get("human", 0)),
        "draws": int(tally.get("draws", 0)),
        "last": list(tally.get("last") or []),
        "bo5_active": bool(snap.get("bo5_active", False)),
    }


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
    # If the withdrawal leaves nobody in line, the active challenger
    # can go back to playing indefinitely — flip Bo5 mode off and push
    # the handshake so the subprocess stops expecting a sliding-5 cutoff.
    if bs_store.queue_depth() == 0:
        bs_store.set_bo5_active(False)
        _write_series_state_for_match()
    return {"status": "withdrawn", "challenge_id": p.challenge_id}


@router.post("/end-match", dependencies=[Depends(_require_loopback)])
def end_match_bot():
    """GUI-initiated clean end of the active bot match. Loopback-only — a
    leaked Bearer token shouldn't let a remote caller end Paul's match
    mid-game. The Discord bot should not call this endpoint."""
    cfg = get_state().cfg
    path = touch_end_match_sentinel(cfg)
    return {"ok": True, "sentinel": str(path)}
