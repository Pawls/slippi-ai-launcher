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
# Committed starter roster. bot_models.json itself is gitignored so each
# user's approved-model picks don't travel in commits; the example file
# is the fallback a fresh clone sees until the user saves for the first
# time (at which point a real bot_models.json is written alongside it).
_MODELS_EXAMPLE_PATH = Path(os.path.abspath(__file__)).parent / "bot_models.example.json"
_LOCAL_OVERRIDES_PATH = Path(os.path.abspath(__file__)).parent / "bot_local.json"

# Defaults keys whose values are per-machine test knobs — they must NOT be
# committed to bot_models.json (which is version-controlled) because a
# stale force_lan_ip or aggressive online_delay override would silently
# break another user's setup. These are transparently split out to the
# gitignored bot_local.json at save time and merged back on load.
_LOCAL_OVERRIDE_KEYS = ("force_lan_ip", "force_port", "force_online_delay")

VALID_STATES = ("offline", "available", "available_with_approval")
# TTL for approval-gated pending — the GUI operator must approve or
# deny within this window or the challenge auto-expires. Only relevant
# to the approval flow; queue entries don't need a wall-clock TTL (see
# QUEUE_TTL_SECONDS).
PENDING_TTL_SECONDS = 120
# Queue entries are bounded by the promotion chain, not by wall-clock:
# whoever's at position N waits for the N-1 reservations ahead of them
# plus the 90s no-show watchdog. A queued user who disappears entirely
# is caught by that same watchdog the moment their turn comes up. We
# still stamp an expires_at for schema consistency, but pick a value
# far enough out that _expire_pending_locked never fires on a queue
# entry in practice.
QUEUE_TTL_SECONDS = 365 * 24 * 3600
MAX_PENDING = 8
CHALLENGE_HISTORY_CAP = 32

# Per-challenger series cap. Sliding-5 window over session game results: once
# either side has 3+ wins within the last 5 played games AND another player
# is queued, the current challenger's set ends after the current game.
BO5_WINDOW = 5
BO5_WIN_THRESHOLD = 3

# Default per-match cap on live-event dispatches. Authoritative in the
# launcher so an observer bug can't spam the Discord bot. User-tunable
# via the "Live reactions" GUI panel.
LIVE_EVENTS_DEFAULT_MAX_PER_MATCH = 5


def _default_live_events_config() -> dict:
    """Build the canonical live-events config. Master toggle defaults to
    off (user must opt in); per-detector toggles default to on so
    enabling live events doesn't require per-type fiddling. Thresholds
    come from the detectors module so the two stay in lockstep."""
    # Imported lazily to avoid pulling slippi_ai into bot_state's import
    # chain when nothing needs it — tests and small GUI-only callers
    # don't benefit from loading tf/numpy via the slippi_ai package.
    from slippi_ai.live_events.detectors import DETECTOR_DEFAULTS
    return {
        "enabled": False,
        "max_per_match": LIVE_EVENTS_DEFAULT_MAX_PER_MATCH,
        "types": {name: dict(params) for name, params in DETECTOR_DEFAULTS.items()},
    }


def _merge_live_events_config(raw) -> dict:
    """Self-heal a stored live-events config against current defaults.

    Missing master fields fall back to defaults. Missing per-type keys
    are filled in so a user who just enabled a new (future) detector
    doesn't have to re-save. Unknown keys inside ``types`` are dropped
    so stale detector config can't accumulate."""
    defaults = _default_live_events_config()
    if not isinstance(raw, dict):
        return defaults
    merged = dict(defaults)
    if "enabled" in raw:
        merged["enabled"] = bool(raw["enabled"])
    if "max_per_match" in raw:
        try:
            merged["max_per_match"] = max(0, int(raw["max_per_match"]))
        except (TypeError, ValueError):
            pass
    raw_types = raw.get("types")
    if isinstance(raw_types, dict):
        merged_types: dict = {}
        for name, default_params in defaults["types"].items():
            params = dict(default_params)
            stored = raw_types.get(name)
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k not in params:
                        continue
                    if k == "enabled":
                        params[k] = bool(v)
                    else:
                        try:
                            # Thresholds are all numeric (int or float).
                            params[k] = type(default_params[k])(v)
                        except (TypeError, ValueError):
                            pass
            merged_types[name] = params
        merged["types"] = merged_types
    return merged


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _window_tally(results: list[str], window: int = BO5_WINDOW) -> dict:
    """Tally the last ``window`` game outcomes. Returns wins per side, the
    full last-``window`` list for UI display, and ``decided_by`` pointing
    at whichever side (if any) has reached ``BO5_WIN_THRESHOLD`` inside
    that window. Draws count as games-played but not as wins, matching
    Melee's own double-KO handling — the series continues until one side
    clinches 3 non-draw wins in the window.
    """
    tail = results[-window:]
    ai = sum(1 for r in tail if r == "ai")
    human = sum(1 for r in tail if r == "human")
    draws = sum(1 for r in tail if r == "draw")
    if ai >= BO5_WIN_THRESHOLD:
        decided_by: str | None = "ai"
    elif human >= BO5_WIN_THRESHOLD:
        decided_by = "human"
    else:
        decided_by = None
    return {
        "ai": ai,
        "human": human,
        "draws": draws,
        "last": tail,
        "decided_by": decided_by,
    }


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
    # Chronological per-game outcomes inside this Dolphin session, each
    # "ai" | "human" | "draw". Populated by the launcher's stdout tailer
    # on every [GAME_RESULT] line the subprocess emits. Used with the
    # sliding-5 window to decide when a contested set is over.
    game_results: list[str] = field(default_factory=list)
    # Flipped True as soon as a second challenger queues. The subprocess
    # polls the companion .bot_series_state file for the same bit and
    # switches into Bo5 mode.
    bo5_active: bool = False
    # Set True by the stdout tailer once the "end series" instruction has
    # been handed off to the subprocess, so a second game-result arriving
    # during shutdown doesn't re-fire the series-end machinery.
    bo5_signalled: bool = False


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
    reason: str  # "completed" | "timed_out" | "disconnected" | "aborted"
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
        if m:
            # Defensive: ignore stored fields that the current ActiveMatch
            # dataclass no longer knows about (field was renamed or removed
            # between launcher versions). Missing fields fall back to the
            # dataclass defaults.
            known = {f.name for f in ActiveMatch.__dataclass_fields__.values()}
            s.match = ActiveMatch(**{k: v for k, v in m.items() if k in known})
        else:
            s.match = None
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

    def record_game_result(self, winner: str) -> dict:
        """Append a game outcome to the active match's series tally and
        return a fresh snapshot. No-op (returns an empty dict) if there
        is no active match — the tailer can race the exit callback.

        ``winner`` must be one of "ai", "human", or "draw"; anything else
        is silently dropped so a malformed [GAME_RESULT] line doesn't
        corrupt the tally.
        """
        if winner not in ("ai", "human", "draw"):
            return {}
        with self._lock:
            m = self._state.match
            if m is None:
                return {}
            m.game_results.append(winner)
            self._save()
            return {
                "match_id": m.match_id,
                "challenger_discord_id": m.challenger_discord_id,
                "channel_id": m.channel_id,
                "bo5_active": m.bo5_active,
                "bo5_signalled": m.bo5_signalled,
                "game_results": list(m.game_results),
                "tally": _window_tally(m.game_results),
            }

    def mark_series_signalled(self) -> None:
        """Remember that the subprocess has been told to end the series.
        Keeps subsequent game results from re-triggering the end-of-set
        hand-off (the shutdown chat sequence takes ~3s and one more game
        result can sneak through)."""
        with self._lock:
            m = self._state.match
            if m is not None and not m.bo5_signalled:
                m.bo5_signalled = True
                self._save()

    def set_bo5_active(self, active: bool) -> dict:
        """Toggle Bo5 mode on the active match. Returns the post-toggle
        snapshot so the caller can synchronise the companion state file
        without a second lock acquisition."""
        with self._lock:
            m = self._state.match
            if m is None:
                return {}
            changed = m.bo5_active != active
            m.bo5_active = active
            if changed:
                self._save()
            return {
                "match_id": m.match_id,
                "bo5_active": m.bo5_active,
                "bo5_signalled": m.bo5_signalled,
                "game_results": list(m.game_results),
                "tally": _window_tally(m.game_results),
            }

    def match_snapshot(self) -> dict:
        """Read-only snapshot of the active match's set state. Used to
        build ``current_set`` responses for newly-queued challengers and
        the ``/bot/queue`` endpoint without cross-thread races."""
        with self._lock:
            m = self._state.match
            if m is None:
                return {}
            return {
                "match_id": m.match_id,
                "challenger_discord_id": m.challenger_discord_id,
                "challenger_tag": m.challenger_tag,
                "channel_id": m.channel_id,
                "bo5_active": m.bo5_active,
                "bo5_signalled": m.bo5_signalled,
                "game_results": list(m.game_results),
                "tally": _window_tally(m.game_results),
            }

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
        ttl_seconds: int = PENDING_TTL_SECONDS,
    ) -> PendingChallenge:
        """Append a pending entry. ``ttl_seconds`` defaults to the short
        approval-gated window; the queue flow overrides it with
        ``QUEUE_TTL_SECONDS`` so queue reservations aren't killed by
        wall-clock expiry — the promotion watchdog is what cleans them
        up."""
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
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
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

    def peek_next_pending(self) -> PendingChallenge | None:
        """Head of the queue without removing. Used by the promotion flow
        when we want to validate a challenger before popping them (e.g.
        their model might have become unapproved since they queued)."""
        with self._lock:
            self._expire_pending_locked()
            return self._state.pending[0] if self._state.pending else None

    def pop_next_pending(self) -> PendingChallenge | None:
        """Remove and return the head of the queue, or ``None`` if empty."""
        with self._lock:
            self._expire_pending_locked()
            if not self._state.pending:
                return None
            return self._state.pending.pop(0)

    def queue_depth(self) -> int:
        with self._lock:
            self._expire_pending_locked()
            return len(self._state.pending)

    def queue_entries(self) -> list[dict]:
        """Full queue contents with 1-indexed positions for ``/bot/queue``."""
        with self._lock:
            self._expire_pending_locked()
            return [
                {
                    "position": i + 1,
                    "challenge_id": p.challenge_id,
                    "challenger_discord_id": p.challenger_discord_id,
                    "challenger_tag": p.challenger_tag,
                    "character": p.character,
                    "style_name": p.style_name,
                    "channel_id": p.channel_id,
                    "expires_at": p.expires_at,
                }
                for i, p in enumerate(self._state.pending)
            ]

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
            "live_events": _default_live_events_config(),
            "persist_dolphin": False,
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
            "live_events": _default_live_events_config(),
            "persist_dolphin": False,
        }
    # Self-heal a missing or blank taunt_webhook_secret. Older allowlist
    # files (pre-D) were created without the field, and we need a stable
    # secret so the bot can verify incoming webhook POSTs. Generating on
    # load means the GUI shows a real secret on first open instead of
    # "(none)", and the value persists to disk for future reads.
    secret = data.get("taunt_webhook_secret") or ""
    needs_rewrite = False
    if not secret:
        secret = secrets.token_urlsafe(32)
        data["taunt_webhook_secret"] = secret
        needs_rewrite = True
    # Self-heal a missing live_events block. Allowlist files predating
    # live reactions won't have it; drop defaults in and persist so the
    # GUI sees a populated structure on first open.
    if not isinstance(data.get("live_events"), dict):
        data["live_events"] = _default_live_events_config()
        needs_rewrite = True
    live_events_merged = _merge_live_events_config(data.get("live_events"))
    if needs_rewrite:
        try:
            data["live_events"] = live_events_merged
            _ALLOWLIST_PATH.write_text(
                json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
    return {
        "api_token": data.get("api_token", ""),
        "allowed_discord_ids": [str(x) for x in data.get("allowed_discord_ids", [])],
        "allow_any_challenger": bool(data.get("allow_any_challenger", False)),
        "taunt_webhook_url": data.get("taunt_webhook_url", ""),
        "taunt_webhook_secret": secret,
        "live_events": live_events_merged,
        "persist_dolphin": bool(data.get("persist_dolphin", False)),
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
    # live_events is always validated through _merge_live_events_config
    # on the way out so a partial GUI save (e.g. just toggling enabled)
    # can't corrupt the per-type threshold block. Missing top-level
    # keys in the payload fall back to the CURRENT stored config, not
    # to defaults — otherwise saving a threshold silently disables the
    # master toggle (and vice versa).
    if "live_events" in data:
        partial = data["live_events"] if isinstance(data["live_events"], dict) else {}
        existing = _merge_live_events_config(current.get("live_events"))
        composed = dict(existing)
        if "enabled" in partial:
            composed["enabled"] = partial["enabled"]
        if "max_per_match" in partial:
            composed["max_per_match"] = partial["max_per_match"]
        if "types" in partial and isinstance(partial["types"], dict):
            merged_types = {
                name: dict(params)
                for name, params in (existing.get("types") or {}).items()
            }
            for name, params in partial["types"].items():
                if not isinstance(params, dict):
                    continue
                base_params = dict(merged_types.get(name) or {})
                for k, v in params.items():
                    base_params[k] = v
                merged_types[name] = base_params
            composed["types"] = merged_types
        live_events_merged = _merge_live_events_config(composed)
    else:
        live_events_merged = _merge_live_events_config(current.get("live_events"))
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
        "live_events": live_events_merged,
        "persist_dolphin": bool(
            data["persist_dolphin"]
            if "persist_dolphin" in data
            else current.get("persist_dolphin", False)
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


def _load_local_overrides() -> dict:
    """Read per-machine overrides from bot_local.json (gitignored). Only
    keys in ``_LOCAL_OVERRIDE_KEYS`` are honored so a stray key can't
    silently override a committed default."""
    if not _LOCAL_OVERRIDES_PATH.is_file():
        return {}
    try:
        data = json.loads(_LOCAL_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _LOCAL_OVERRIDE_KEYS if k in data}


def _save_local_overrides(overrides: dict) -> None:
    """Persist only the override keys to bot_local.json, dropping empty
    values so the file stays minimal. Writes atomically via temp-file
    rename; swallowed errors keep the save call from crashing the config
    endpoint if the disk is read-only."""
    filtered: dict = {}
    for k in _LOCAL_OVERRIDE_KEYS:
        if k not in overrides:
            continue
        v = overrides[k]
        # Empty string / None / 0 — treat as "not set". The loader treats
        # missing and empty identically, so writing them would just waste
        # disk and make the file noisier to read.
        if v is None or v == "" or v == 0:
            continue
        filtered[k] = v
    if not filtered:
        try:
            _LOCAL_OVERRIDES_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return
    tmp = _LOCAL_OVERRIDES_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
        os.replace(tmp, _LOCAL_OVERRIDES_PATH)
    except OSError:
        pass


def _read_models_file() -> dict:
    """Read bot_models.json, falling back to bot_models.example.json when
    the user hasn't written a real config yet. First-run behavior: the
    example is used read-only until save_models_config materializes a
    real bot_models.json."""
    for path in (_MODELS_PATH, _MODELS_EXAMPLE_PATH):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def load_models_config() -> dict:
    """Return the approved-models config ``{approved_models, defaults}``,
    with per-machine overrides from bot_local.json merged on top of the
    committed defaults so the caller gets a single unified view."""
    data = _read_models_file()
    committed_defaults = data.get("defaults", {}) or {}
    merged = {**committed_defaults, **_load_local_overrides()}
    return {
        "approved_models": data.get("approved_models", []),
        "defaults": merged,
    }


def save_models_config(
    approved_models: list[dict] | None = None,
    defaults: dict | None = None,
) -> dict:
    """Persist ``approved_models`` and/or ``defaults``.

    Keys in ``_LOCAL_OVERRIDE_KEYS`` are split out to bot_local.json
    (gitignored) so per-machine test knobs never end up in a commit.
    Everything else persists to bot_models.json. Preserves unknown fields
    on disk (e.g. ``_note``) so hand-maintained comments survive a GUI
    save. Writes atomically via temp-file rename.
    """
    # Seed from the example on first save so _note and any starter
    # entries survive the user's initial edit — subsequent saves read
    # their own bot_models.json and the example is ignored.
    existing = _read_models_file()
    if not isinstance(existing, dict):
        existing = {}

    if approved_models is not None:
        existing["approved_models"] = approved_models
    else:
        existing.setdefault("approved_models", [])

    if defaults is not None:
        committed = {
            k: v for k, v in defaults.items() if k not in _LOCAL_OVERRIDE_KEYS
        }
        local = {k: defaults[k] for k in _LOCAL_OVERRIDE_KEYS if k in defaults}
        existing["defaults"] = committed
        _save_local_overrides(local)
    else:
        existing.setdefault("defaults", {})

    # Strip any lingering override keys from the committed defaults — they
    # may have been written before the split existed.
    committed_defaults = existing.get("defaults", {}) or {}
    for k in _LOCAL_OVERRIDE_KEYS:
        committed_defaults.pop(k, None)
    existing["defaults"] = committed_defaults

    tmp = _MODELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, _MODELS_PATH)

    # Rebuild the merged view for the response so the GUI sees the same
    # shape it sent.
    merged = {**committed_defaults, **_load_local_overrides()}
    return {
        "approved_models": existing.get("approved_models", []),
        "defaults": merged,
    }
