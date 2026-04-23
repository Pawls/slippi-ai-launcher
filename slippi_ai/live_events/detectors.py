"""Pure detectors for live gamestate events.

Each detector consumes ``(port, action, frame)`` tuples one at a time and
returns a ``LiveEvent`` the instant a threshold is crossed, or ``None``
otherwise. Action values are libmelee ``Action`` enum values (ints); the
observer extracts ``player.action.value`` before calling ``feed()``.

These detectors are lossy by design — if the observer drops a frame
under queue pressure, at worst one event is delayed by a frame.

No I/O, no libmelee imports: this module is a pure function of the
stream so it's trivially unit-testable without a running Dolphin.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


DEFAULT_FPS = 60


# ── Action-state groups (libmelee enum values) ─────────────────────────
# Verified against `.venv/Lib/site-packages/melee/enums.py`. Using raw
# ints so this module stays libmelee-free — the observer is responsible
# for extracting ``player.action.value`` before feeding.

SHIELD_STATES: frozenset[int] = frozenset({
    0xb2,  # SHIELD_START
    0xb3,  # SHIELD
    0xb4,  # SHIELD_RELEASE
    0xb5,  # SHIELD_STUN
    0xb6,  # SHIELD_REFLECT
})

GRAB_STATES: frozenset[int] = frozenset({
    0xd4,  # GRAB
    0xd6,  # GRAB_RUNNING
})

ROLL_STATES: frozenset[int] = frozenset({
    0xbc,  # GROUND_ROLL_FORWARD_UP
    0xbd,  # GROUND_ROLL_BACKWARD_UP
    0xc4,  # GROUND_ROLL_FORWARD_DOWN
    0xc5,  # GROUND_ROLL_BACKWARD_DOWN
    0xc6,  # GROUND_ROLL_SPOT_DOWN
    0xe9,  # ROLL_FORWARD
    0xea,  # ROLL_BACKWARD
    0x102, # EDGE_ROLL_SLOW
    0x103, # EDGE_ROLL_QUICK
})

LEDGE_STATES: frozenset[int] = frozenset({
    0xfc,  # EDGE_CATCHING
    0xfd,  # EDGE_HANGING
})

# Smash families: FSMASH covers all angle variants (HIGH/MID_HIGH/MID/MID_LOW/LOW)
# because they represent the same move choice — a player angling forward-smash
# up vs. down is still spamming forward-smash.
FSMASH_STATES: frozenset[int] = frozenset({
    0x3a,  # FSMASH_HIGH
    0x3b,  # FSMASH_MID_HIGH
    0x3c,  # FSMASH_MID
    0x3d,  # FSMASH_MID_LOW
    0x3e,  # FSMASH_LOW
})
UPSMASH_STATES: frozenset[int] = frozenset({0x3f})    # UPSMASH
DOWNSMASH_STATES: frozenset[int] = frozenset({0x40})  # DOWNSMASH

SMASH_STATES: frozenset[int] = FSMASH_STATES | UPSMASH_STATES | DOWNSMASH_STATES


def _smash_family(action: int) -> Optional[str]:
    if action in FSMASH_STATES:
        return "FSMASH"
    if action in UPSMASH_STATES:
        return "UPSMASH"
    if action in DOWNSMASH_STATES:
        return "DOWNSMASH"
    return None


# ── Event dataclass ────────────────────────────────────────────────────

@dataclass
class LiveEvent:
    type: str
    frame: int
    player_port: int
    stats: dict
    text_hint: str
    severity: str = "medium"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "frame": self.frame,
            "player_port": self.player_port,
            "stats": dict(self.stats),
            "text_hint": self.text_hint,
            "severity": self.severity,
        }


# ── Default thresholds ─────────────────────────────────────────────────
# Named so the launcher and GUI can present the same defaults without
# re-declaring them. Keep in sync with the plan's MVP numbers.

DETECTOR_DEFAULTS: dict = {
    "shield_grab_spam": {
        "enabled": True,
        "count": 4,
        "window_sec": 30.0,
        "cooldown_sec": 30.0,
    },
    "roll_spam": {
        "enabled": True,
        "count": 6,
        "window_sec": 20.0,
        "cooldown_sec": 30.0,
    },
    "ledge_camp": {
        "enabled": True,
        "dwell_sec": 5.0,
        "window_sec": 15.0,
        "cooldown_sec": 60.0,
    },
    "smash_spam": {
        "enabled": True,
        "count": 3,
        "gap_sec": 10.0,
        "cooldown_sec": 30.0,
    },
}


class _PerPortState:
    """Subclasses stash per-port scratch state here; lazy-init on demand."""

    def __init__(self):
        self._per_port: dict[int, dict] = {}

    def _get(self, port: int) -> dict:
        entry = self._per_port.get(port)
        if entry is None:
            entry = self._fresh_entry()
            self._per_port[port] = entry
        return entry

    def _fresh_entry(self) -> dict:
        raise NotImplementedError


# ── Shield-grab spam ───────────────────────────────────────────────────

class ShieldGrabDetector(_PerPortState):
    """Fire when a player transitions SHIELD→GRAB ``count`` times within
    ``window_sec``. Dedupes per transition: only the first frame of a
    continuous action counts. Cooldown prevents re-firing during a
    sustained spree."""

    def __init__(self, *, count: int = 4, window_sec: float = 30.0,
                 cooldown_sec: float = 30.0, fps: int = DEFAULT_FPS):
        super().__init__()
        self.count = int(count)
        self.window_frames = int(window_sec * fps)
        self.window_sec = float(window_sec)
        self.cooldown_frames = int(cooldown_sec * fps)

    def _fresh_entry(self) -> dict:
        return {
            "last_action": None,
            "events": deque(),
            "last_fire_frame": None,
        }

    def feed(self, port: int, action: int, frame: int) -> Optional[LiveEvent]:
        entry = self._get(port)
        last = entry["last_action"]
        entry["last_action"] = action
        if last is not None and last in SHIELD_STATES and action in GRAB_STATES and last != action:
            entry["events"].append(frame)
        cutoff = frame - self.window_frames
        while entry["events"] and entry["events"][0] < cutoff:
            entry["events"].popleft()
        last_fire = entry["last_fire_frame"]
        if last_fire is not None and frame - last_fire < self.cooldown_frames:
            return None
        if len(entry["events"]) >= self.count:
            entry["last_fire_frame"] = frame
            return LiveEvent(
                type="shield_grab_spam",
                frame=frame,
                player_port=port,
                stats={
                    "count": len(entry["events"]),
                    "window_sec": self.window_sec,
                },
                text_hint=f"{len(entry['events'])} shield-grabs in {int(self.window_sec)}s",
            )
        return None


# ── Roll spam ──────────────────────────────────────────────────────────

class RollSpamDetector(_PerPortState):
    """Fire on ``count`` distinct roll action transitions within
    ``window_sec``. Only counts a roll once at its first frame."""

    def __init__(self, *, count: int = 6, window_sec: float = 20.0,
                 cooldown_sec: float = 30.0, fps: int = DEFAULT_FPS):
        super().__init__()
        self.count = int(count)
        self.window_frames = int(window_sec * fps)
        self.window_sec = float(window_sec)
        self.cooldown_frames = int(cooldown_sec * fps)

    def _fresh_entry(self) -> dict:
        return {
            "last_action": None,
            "events": deque(),
            "last_fire_frame": None,
        }

    def feed(self, port: int, action: int, frame: int) -> Optional[LiveEvent]:
        entry = self._get(port)
        last = entry["last_action"]
        entry["last_action"] = action
        if action in ROLL_STATES and last != action:
            entry["events"].append(frame)
        cutoff = frame - self.window_frames
        while entry["events"] and entry["events"][0] < cutoff:
            entry["events"].popleft()
        last_fire = entry["last_fire_frame"]
        if last_fire is not None and frame - last_fire < self.cooldown_frames:
            return None
        if len(entry["events"]) >= self.count:
            entry["last_fire_frame"] = frame
            return LiveEvent(
                type="roll_spam",
                frame=frame,
                player_port=port,
                stats={
                    "count": len(entry["events"]),
                    "window_sec": self.window_sec,
                },
                text_hint=f"{len(entry['events'])} rolls in {int(self.window_sec)}s",
            )
        return None


# ── Ledge camp ─────────────────────────────────────────────────────────

class LedgeCampDetector(_PerPortState):
    """Fire when cumulative ledge-dwell exceeds ``dwell_sec`` within the
    last ``window_sec``. Tracks frames spent in EDGE_CATCHING / EDGE_HANGING
    as a sliding buffer — a player who grabs ledge briefly and drops does
    not trigger; a player who parks there for 5+ seconds (in aggregate)
    within 15 seconds does."""

    def __init__(self, *, dwell_sec: float = 5.0, window_sec: float = 15.0,
                 cooldown_sec: float = 60.0, fps: int = DEFAULT_FPS):
        super().__init__()
        self.dwell_frames = int(dwell_sec * fps)
        self.dwell_sec = float(dwell_sec)
        self.window_frames = int(window_sec * fps)
        self.window_sec = float(window_sec)
        self.cooldown_frames = int(cooldown_sec * fps)
        self.fps = fps

    def _fresh_entry(self) -> dict:
        return {
            "ledge_frames": deque(),
            "last_fire_frame": None,
        }

    def feed(self, port: int, action: int, frame: int) -> Optional[LiveEvent]:
        entry = self._get(port)
        if action in LEDGE_STATES:
            entry["ledge_frames"].append(frame)
        cutoff = frame - self.window_frames
        while entry["ledge_frames"] and entry["ledge_frames"][0] < cutoff:
            entry["ledge_frames"].popleft()
        last_fire = entry["last_fire_frame"]
        if last_fire is not None and frame - last_fire < self.cooldown_frames:
            return None
        if len(entry["ledge_frames"]) >= self.dwell_frames:
            entry["last_fire_frame"] = frame
            dwell = len(entry["ledge_frames"]) / self.fps
            return LiveEvent(
                type="ledge_camp",
                frame=frame,
                player_port=port,
                stats={
                    "dwell_sec": round(dwell, 1),
                    "window_sec": self.window_sec,
                },
                text_hint=f"{dwell:.1f}s on ledge in the last {int(self.window_sec)}s",
            )
        return None


# ── Smash spam ─────────────────────────────────────────────────────────

class SmashSpamDetector(_PerPortState):
    """Fire when the same grounded smash family (FSMASH / UPSMASH /
    DOWNSMASH) is thrown ``count`` times in a row with inter-smash gap
    less than ``gap_sec``.

    Detection fires on transition INTO a smash action-state — held frames
    of the same smash count once at their first frame. A different smash
    family (or a gap > gap_sec with no smash at all) resets the
    consecutive counter. Non-smash actions (jabs, tilts, aerials,
    specials, shields, rolls, getting hit) do NOT reset the counter —
    only a different smash family does. Hit/whiff is irrelevant: we
    detect the player entering the action, not the hitbox landing."""

    def __init__(self, *, count: int = 3, gap_sec: float = 10.0,
                 cooldown_sec: float = 30.0, fps: int = DEFAULT_FPS):
        super().__init__()
        self.count = int(count)
        self.gap_frames = int(gap_sec * fps)
        self.cooldown_frames = int(cooldown_sec * fps)

    def _fresh_entry(self) -> dict:
        return {
            "last_action": None,
            "last_family": None,
            "last_smash_frame": None,
            "consecutive": 0,
            "last_fire_frame": None,
        }

    def feed(self, port: int, action: int, frame: int) -> Optional[LiveEvent]:
        entry = self._get(port)
        last = entry["last_action"]
        entry["last_action"] = action
        family = _smash_family(action)
        if family is None or last == action:
            return None
        last_smash = entry["last_smash_frame"]
        gap_expired = (
            last_smash is None or (frame - last_smash) > self.gap_frames
        )
        if gap_expired or entry["last_family"] != family:
            entry["consecutive"] = 1
            entry["last_family"] = family
        else:
            entry["consecutive"] += 1
        entry["last_smash_frame"] = frame

        last_fire = entry["last_fire_frame"]
        if last_fire is not None and frame - last_fire < self.cooldown_frames:
            return None
        if entry["consecutive"] >= self.count:
            entry["last_fire_frame"] = frame
            n = entry["consecutive"]
            entry["consecutive"] = 0
            return LiveEvent(
                type="smash_spam",
                frame=frame,
                player_port=port,
                stats={
                    "move": family,
                    "count": n,
                },
                text_hint=f"{n} {family.lower()}es in a row",
            )
        return None


# ── Factory ────────────────────────────────────────────────────────────

def build_detectors(
    config: Optional[dict] = None, *, fps: int = DEFAULT_FPS,
) -> list:
    """Construct the enabled detectors from a config dict.

    Missing keys fall back to ``DETECTOR_DEFAULTS``. Detectors with
    ``enabled: false`` are omitted entirely — they never allocate state
    and never observe frames. Unknown keys in the config are ignored.
    """
    cfg = config or {}
    detectors: list = []

    def _merge(name: str) -> dict:
        base = dict(DETECTOR_DEFAULTS[name])
        override = cfg.get(name) or {}
        if isinstance(override, dict):
            base.update(override)
        return base

    sg = _merge("shield_grab_spam")
    if sg.get("enabled", True):
        detectors.append(ShieldGrabDetector(
            count=sg["count"],
            window_sec=sg["window_sec"],
            cooldown_sec=sg["cooldown_sec"],
            fps=fps,
        ))

    rs = _merge("roll_spam")
    if rs.get("enabled", True):
        detectors.append(RollSpamDetector(
            count=rs["count"],
            window_sec=rs["window_sec"],
            cooldown_sec=rs["cooldown_sec"],
            fps=fps,
        ))

    lc = _merge("ledge_camp")
    if lc.get("enabled", True):
        detectors.append(LedgeCampDetector(
            dwell_sec=lc["dwell_sec"],
            window_sec=lc["window_sec"],
            cooldown_sec=lc["cooldown_sec"],
            fps=fps,
        ))

    ss = _merge("smash_spam")
    if ss.get("enabled", True):
        detectors.append(SmashSpamDetector(
            count=ss["count"],
            gap_sec=ss["gap_sec"],
            cooldown_sec=ss["cooldown_sec"],
            fps=fps,
        ))

    return detectors
