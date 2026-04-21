"""Discord-bot presence and pending-challenge state.

Singleton in-memory store backed by ``bot_state.json`` so presence survives a
launcher restart. Tracks:

- current presence mode (``offline`` | ``available`` | ``available_with_approval``)
- the single active match, if any
- a small FIFO of pending approval requests (TTL-expired)

Thread-safety: all public methods take a lock — the FastAPI thread pool and
the process_manager completion callbacks both touch this module.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


_STATE_PATH = Path(os.path.abspath(__file__)).parent / "bot_state.json"
_ALLOWLIST_PATH = Path(os.path.abspath(__file__)).parent / "bot_allowlist.json"
_MODELS_PATH = Path(os.path.abspath(__file__)).parent / "bot_models.json"

VALID_STATES = ("offline", "available", "available_with_approval")
PENDING_TTL_SECONDS = 120
MAX_PENDING = 3
CHALLENGE_HISTORY_CAP = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass
class ActiveMatch:
    match_id: str
    challenger_discord_id: str
    challenger_tag: str
    character: str
    style_name: str
    connect_code: str
    headless: bool
    started_at: str
    channel_id: str = ""


@dataclass
class PendingChallenge:
    challenge_id: str
    challenger_discord_id: str
    challenger_tag: str
    connect_code: str
    character: str
    style_name: str
    created_at: str
    expires_at: str
    channel_id: str = ""


@dataclass
class ResolvedChallenge:
    """A pending challenge that was approved/denied/expired — kept briefly so
    the bot's poll for ``/bot/challenge/{id}`` can see the outcome after it
    leaves the pending queue."""
    challenge_id: str
    status: str  # "approved" | "denied" | "expired"
    match_id: str | None
    headless: bool | None
    resolved_at: str


@dataclass
class MatchOutcome:
    """Why the most recent bot match ended. Consumed by the Discord taunt
    webhook (feature D) and surfaced on ``/bot/status`` so the GUI can
    explain a mysteriously-ended match."""
    match_id: str
    challenger_discord_id: str
    challenger_tag: str
    reason: str  # "completed" | "timed_out" | "disconnected"
    finished_at: str
    channel_id: str = ""


@dataclass
class _BotState:
    presence: str = "offline"
    last_changed: str = field(default_factory=lambda: _iso(_now()))
    match: ActiveMatch | None = None
    pending: list[PendingChallenge] = field(default_factory=list)
    resolved: list[ResolvedChallenge] = field(default_factory=list)
    last_outcome: MatchOutcome | None = None


class BotStateStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = _BotState()
        self._load()

    # ── Persistence ─────────────────────────────────────────────────────

    def _load(self):
        if not _STATE_PATH.is_file():
            return
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        s = self._state
        s.presence = data.get("presence", "offline")
        if s.presence not in VALID_STATES:
            s.presence = "offline"
        s.last_changed = data.get("last_changed") or _iso(_now())
        m = data.get("match")
        s.match = ActiveMatch(**m) if m else None
        # Pending / resolved are ephemeral — drop on reload so we don't
        # resurrect stale approval prompts across restarts.
        s.pending = []
        s.resolved = []

    def _save(self):
        s = self._state
        payload = {
            "presence": s.presence,
            "last_changed": s.last_changed,
            "match": asdict(s.match) if s.match else None,
        }
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_STATE_PATH)

    # ── Presence ────────────────────────────────────────────────────────

    def get_presence(self) -> dict:
        with self._lock:
            self._expire_pending_locked()
            s = self._state
            return {
                "state": s.presence,
                "in_match": s.match is not None,
                "last_changed": s.last_changed,
            }

    def set_presence(self, state: str) -> dict:
        if state not in VALID_STATES:
            raise ValueError(f"invalid presence {state!r}")
        with self._lock:
            s = self._state
            if s.presence != state:
                s.presence = state
                s.last_changed = _iso(_now())
                # Leaving approval mode clears any unresolved pending queue.
                if state != "available_with_approval":
                    for p in s.pending:
                        s.resolved.append(ResolvedChallenge(
                            challenge_id=p.challenge_id,
                            status="expired",
                            match_id=None,
                            headless=None,
                            resolved_at=_iso(_now()),
                        ))
                    s.pending.clear()
                self._save()
            return self.get_presence()

    # ── Match lifecycle ─────────────────────────────────────────────────

    def set_match(self, match: ActiveMatch):
        with self._lock:
            self._state.match = match
            self._save()

    def clear_match(self, match_id: str | None = None, reason: str = "completed"):
        with self._lock:
            current = self._state.match
            if current and (match_id is None or current.match_id == match_id):
                self._state.last_outcome = MatchOutcome(
                    match_id=current.match_id,
                    challenger_discord_id=current.challenger_discord_id,
                    challenger_tag=current.challenger_tag,
                    reason=reason,
                    finished_at=_iso(_now()),
                    channel_id=current.channel_id,
                )
                self._state.match = None
                self._save()

    # ── Pending challenges ──────────────────────────────────────────────

    def add_pending(
        self,
        *,
        challenger_discord_id: str,
        challenger_tag: str,
        connect_code: str,
        character: str,
        style_name: str,
        channel_id: str = "",
    ) -> PendingChallenge:
        with self._lock:
            self._expire_pending_locked()
            if len(self._state.pending) >= MAX_PENDING:
                raise RuntimeError("pending queue full")
            now = _now()
            p = PendingChallenge(
                challenge_id=uuid.uuid4().hex[:12],
                challenger_discord_id=challenger_discord_id,
                challenger_tag=challenger_tag,
                connect_code=connect_code,
                character=character,
                style_name=style_name,
                created_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=PENDING_TTL_SECONDS)),
                channel_id=channel_id,
            )
            self._state.pending.append(p)
            return p

    def pop_pending(self, challenge_id: str) -> PendingChallenge | None:
        with self._lock:
            self._expire_pending_locked()
            for i, p in enumerate(self._state.pending):
                if p.challenge_id == challenge_id:
                    return self._state.pending.pop(i)
            return None

    def pop_pending_by_challenger(self, challenger_discord_id: str) -> PendingChallenge | None:
        """Find and remove the most recent pending challenge for a given
        challenger. Used by the withdraw flow — the bot only knows the
        Discord user, not the opaque challenge_id. If the same user has
        multiple outstanding (shouldn't happen — /bot/launch 409s on
        second attempt — but defensive), returns the newest."""
        target = challenger_discord_id.strip().lower()
        with self._lock:
            self._expire_pending_locked()
            for i in range(len(self._state.pending) - 1, -1, -1):
                p = self._state.pending[i]
                if p.challenger_discord_id.strip().lower() == target:
                    return self._state.pending.pop(i)
            return None

    def resolve(self, challenge_id: str, status: str,
                match_id: str | None = None, headless: bool | None = None):
        with self._lock:
            self._state.resolved.append(ResolvedChallenge(
                challenge_id=challenge_id,
                status=status,
                match_id=match_id,
                headless=headless,
                resolved_at=_iso(_now()),
            ))
            if len(self._state.resolved) > CHALLENGE_HISTORY_CAP:
                self._state.resolved = self._state.resolved[-CHALLENGE_HISTORY_CAP:]

    def lookup_challenge(self, challenge_id: str) -> dict:
        with self._lock:
            self._expire_pending_locked()
            for p in self._state.pending:
                if p.challenge_id == challenge_id:
                    return {"status": "pending", "match_id": None, "headless": None}
            for r in reversed(self._state.resolved):
                if r.challenge_id == challenge_id:
                    return {
                        "status": r.status,
                        "match_id": r.match_id,
                        "headless": r.headless,
                    }
            return {"status": "unknown", "match_id": None, "headless": None}

    def _expire_pending_locked(self):
        now = _now()
        kept: list[PendingChallenge] = []
        for p in self._state.pending:
            exp = datetime.fromisoformat(p.expires_at)
            if exp <= now:
                self._state.resolved.append(ResolvedChallenge(
                    challenge_id=p.challenge_id,
                    status="expired",
                    match_id=None,
                    headless=None,
                    resolved_at=_iso(now),
                ))
            else:
                kept.append(p)
        self._state.pending = kept

    # ── Full snapshot for GUI ───────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            self._expire_pending_locked()
            s = self._state
            return {
                "state": s.presence,
                "last_changed": s.last_changed,
                "match": asdict(s.match) if s.match else None,
                "pending": [asdict(p) for p in s.pending],
                "last_outcome": asdict(s.last_outcome) if s.last_outcome else None,
            }


# ── Singletons ──────────────────────────────────────────────────────────

_store: BotStateStore | None = None


def get_bot_state() -> BotStateStore:
    global _store
    if _store is None:
        _store = BotStateStore()
    return _store


# ── Allowlist / token helpers ───────────────────────────────────────────

def load_allowlist() -> dict:
    """Return the full bot auth payload: ``{api_token, allowed_discord_ids,
    allow_any_challenger, taunt_webhook_url, taunt_webhook_secret}``.

    ``allow_any_challenger`` is the "open to the whole server" toggle — when
    True the per-user allowlist is ignored and anyone whose challenge the
    bot forwards is accepted. The API token is still required, so this
    loosens only the second gate, not authentication."""
    if not _ALLOWLIST_PATH.is_file():
        payload = {
            "api_token": secrets.token_urlsafe(32),
            "allowed_discord_ids": [],
            "allow_any_challenger": False,
            "taunt_webhook_url": "",
            "taunt_webhook_secret": secrets.token_urlsafe(32),
        }
        _ALLOWLIST_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    try:
        data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "api_token": "",
            "allowed_discord_ids": [],
            "allow_any_challenger": False,
            "taunt_webhook_url": "",
            "taunt_webhook_secret": "",
        }
    return {
        "api_token": data.get("api_token", ""),
        "allowed_discord_ids": [str(x) for x in data.get("allowed_discord_ids", [])],
        "allow_any_challenger": bool(data.get("allow_any_challenger", False)),
        "taunt_webhook_url": data.get("taunt_webhook_url", ""),
        "taunt_webhook_secret": data.get("taunt_webhook_secret", ""),
    }


def save_allowlist(data: dict) -> dict:
    """Persist an allowlist payload. Missing fields fall back to current
    values so a partial write does not accidentally clear the token or
    the ID list. An explicit ``None`` for ``allowed_discord_ids`` is
    treated as an empty list, not a request to preserve the old one."""
    current = load_allowlist()
    if "allowed_discord_ids" in data:
        raw_ids = data["allowed_discord_ids"] or []
    else:
        raw_ids = current.get("allowed_discord_ids", [])
    # Generate a taunt secret on first save if none exists yet, so the
    # user doesn't have to discover the field to enable webhooks.
    existing_secret = current.get("taunt_webhook_secret") or secrets.token_urlsafe(32)
    merged = {
        "api_token": str(data.get("api_token") or current.get("api_token") or ""),
        "allowed_discord_ids": [str(x) for x in raw_ids],
        "allow_any_challenger": bool(
            data["allow_any_challenger"]
            if "allow_any_challenger" in data
            else current.get("allow_any_challenger", False)
        ),
        "taunt_webhook_url": str(
            data.get("taunt_webhook_url")
            if "taunt_webhook_url" in data
            else current.get("taunt_webhook_url", "")
        ),
        "taunt_webhook_secret": str(
            data.get("taunt_webhook_secret")
            if "taunt_webhook_secret" in data
            else existing_secret
        ),
    }
    _ALLOWLIST_PATH.write_text(
        json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def rotate_api_token() -> str:
    """Generate a new random api_token, persist it, and return it."""
    new_token = secrets.token_urlsafe(32)
    save_allowlist({"api_token": new_token})
    return new_token


def load_models_config() -> dict:
    """Return the approved-models config ``{approved_models, defaults}``."""
    if not _MODELS_PATH.is_file():
        return {"approved_models": [], "defaults": {}}
    try:
        data = json.loads(_MODELS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"approved_models": [], "defaults": {}}
    return {
        "approved_models": data.get("approved_models", []),
        "defaults": data.get("defaults", {}),
    }


def save_models_config(
    approved_models: list[dict] | None = None,
    defaults: dict | None = None,
) -> dict:
    """Persist ``approved_models`` and/or ``defaults`` to bot_models.json.

    Preserves any fields present in the on-disk file that we don't touch
    (e.g. ``_note``) so the hand-maintained comment doesn't get erased
    when the GUI saves. Writes atomically via a temp-file rename.
    """
    existing = {}
    if _MODELS_PATH.is_file():
        try:
            existing = json.loads(_MODELS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    if approved_models is not None:
        existing["approved_models"] = approved_models
    else:
        existing.setdefault("approved_models", [])
    if defaults is not None:
        existing["defaults"] = defaults
    else:
        existing.setdefault("defaults", {})

    tmp = _MODELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, _MODELS_PATH)
    return {
        "approved_models": existing.get("approved_models", []),
        "defaults": existing.get("defaults", {}),
    }
