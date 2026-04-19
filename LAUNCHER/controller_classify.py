"""Classify the controller used by each player in a Slippi replay.

Outputs one of:  GCC, GCC-Modded, Box, Keyboard, Unknown
plus a confidence in [0, 1] and the raw signal values that produced it.

The cascade prioritises physically-impossible-on-an-analog-pot signals
(single-frame stick velocity, analog-trigger entropy) over softer
heuristics (unique coordinate counts, notch grids), so short games and
idle players do not get misclassified the way the old unique-count
heuristic did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


CLASSIFIER_VERSION = 2

LABEL_GCC = "GCC"
LABEL_GCC_MODDED = "GCC-Modded"
LABEL_BOX = "Box"
LABEL_KEYBOARD = "Keyboard"
LABEL_UNKNOWN = "Unknown"


# ── Tuning constants ────────────────────────────────────────────────────────

MIN_FRAMES = 300                # ≤5 s → skip, not enough data
KINEMATIC_DELTA = 1.4           # single-frame |Δstick| beyond this is impossible on an analog pot
KINEMATIC_JUMPS_FOR_BOX = 4     # ≥ this many impossible jumps → definitely digital
TRIGGER_BAND = (0.05, 0.95)     # count unique trigger values strictly inside this band
TRIGGER_ANALOG_MIN = 20         # ≥ this many unique intermediate trigger values → analog slider present
CSTICK_ANALOG_MIN = 15          # ≥ this many unique cstick coords → analog stick
BUTTON_ONLY_KEYBOARD = 0.6      # fraction of active frames with (0,0) stick but button held
UNIQUE_RAW_GCC = 150            # tiebreaker upper band
UNIQUE_RAW_BOX = 100            # tiebreaker lower band
NOTCH_CLAMPED_MIN = 0.5         # fraction on Phob notch grid to suggest modded
STICK_NOISE_HIGH = 0.003        # std-dev of per-frame stick deltas on held frames: GCC floor


# ── Signal extraction ───────────────────────────────────────────────────────

def _to_np(arr) -> np.ndarray | None:
    """Convert a pyarrow Float/Int array to a numpy array, or None if missing."""
    if arr is None:
        return None
    try:
        return arr.to_numpy()
    except Exception:
        return None


def _extract_signals(pre) -> dict[str, float | int | None]:
    """Compute all classification signals for one player's pre-frame column."""
    jx = _to_np(pre.joystick.x)
    jy = _to_np(pre.joystick.y)
    cx = _to_np(pre.cstick.x)
    cy = _to_np(pre.cstick.y)
    l_trig = _to_np(pre.triggers_physical.l)
    r_trig = _to_np(pre.triggers_physical.r)
    buttons_phys = _to_np(pre.buttons_physical)
    raw_x = _to_np(getattr(pre, "raw_analog_x", None))
    raw_y = _to_np(getattr(pre, "raw_analog_y", None))

    n = len(jx) if jx is not None else 0
    signals: dict[str, Any] = {"frames": n, "has_raw": raw_x is not None and raw_y is not None}

    # Kinematic: single-frame impossible jumps. Use processed joystick because
    # it is always present. UCF only adjusts dashback / shield-drop angles; it
    # cannot create single-frame crossings greater than physical stick travel,
    # so this signal stays reliable on processed values.
    dx = np.abs(np.diff(jx))
    dy = np.abs(np.diff(jy))
    signals["impossible_velocity_jumps"] = int(
        np.sum(dx > KINEMATIC_DELTA) + np.sum(dy > KINEMATIC_DELTA)
    )

    # Trigger analog entropy: count unique intermediate (non-0, non-1) pressures.
    # Round to 3dp to suppress float noise.
    def _trigger_entropy(arr: np.ndarray | None) -> int:
        if arr is None:
            return 0
        mask = (arr > TRIGGER_BAND[0]) & (arr < TRIGGER_BAND[1])
        if not mask.any():
            return 0
        return int(len(np.unique(np.round(arr[mask], 3))))

    signals["trigger_analog_entropy"] = (
        _trigger_entropy(l_trig) + _trigger_entropy(r_trig)
    )

    # C-stick discreteness: unique pairs @ 2dp.
    if cx is not None and cy is not None:
        cstick_pairs = np.unique(np.column_stack((np.round(cx, 2), np.round(cy, 2))), axis=0)
        signals["cstick_unique_pairs"] = int(len(cstick_pairs))
    else:
        signals["cstick_unique_pairs"] = 0

    # Unique joystick coordinate count (legacy tiebreaker).
    stick_pairs = np.unique(np.column_stack((np.round(jx, 3), np.round(jy, 3))), axis=0)
    signals["unique_stick_pairs"] = int(len(stick_pairs))

    # Button-only frames: keyboards often hold a button with stick at (0,0).
    if buttons_phys is not None:
        neutral_stick = (np.round(jx, 2) == 0) & (np.round(jy, 2) == 0)
        any_button = buttons_phys != 0
        both = neutral_stick & any_button
        # active = any input at all
        active = (buttons_phys != 0) | (~neutral_stick)
        n_active = int(active.sum())
        signals["button_only_frames_ratio"] = (
            float(both.sum()) / n_active if n_active > 0 else 0.0
        )
    else:
        signals["button_only_frames_ratio"] = 0.0

    # Stick noise variance: stddev of per-frame deltas on "held cardinal" frames.
    # A GCC has ~±0.003 analog jitter even when fully pushed; a Box reads exact
    # constants; Phob-clamped reads between the two.
    held_mask = (np.abs(jx[:-1]) > 0.6) | (np.abs(jy[:-1]) > 0.6)
    if held_mask.any():
        signals["stick_noise_variance"] = float(
            np.std(np.concatenate([dx[held_mask], dy[held_mask]]))
        )
    else:
        signals["stick_noise_variance"] = 0.0

    # Notch-grid ratio (Phob): raw-analog only. Phob firmware rounds raw
    # values onto a small grid around the notches. We approximate by checking
    # for high concentration on discrete ticks when raw is present.
    if raw_x is not None and raw_y is not None and len(raw_x) > 0:
        # Phob raw analog is Int8 with Melee range ~±80.
        # Concentration on any exact integer bucket suggests quantization.
        hist = np.bincount(
            (raw_x.astype(np.int32) + 128),
            minlength=256,
        )
        top_buckets = np.sort(hist)[-16:].sum()
        total = hist.sum()
        signals["notch_grid_ratio"] = (
            float(top_buckets) / total if total > 0 else 0.0
        )
    else:
        signals["notch_grid_ratio"] = None

    return signals


# ── Classification cascade ──────────────────────────────────────────────────

@dataclass(slots=True)
class ClassificationResult:
    label: str
    confidence: float
    signals: dict[str, Any] = field(default_factory=dict)
    version: int = CLASSIFIER_VERSION


def _cascade(signals: dict[str, Any]) -> tuple[str, float]:
    jumps = signals["impossible_velocity_jumps"]
    trig_entropy = signals["trigger_analog_entropy"]
    cstick_unique = signals["cstick_unique_pairs"]
    stick_unique = signals["unique_stick_pairs"]
    btn_only = signals["button_only_frames_ratio"]
    noise = signals["stick_noise_variance"]
    notch = signals["notch_grid_ratio"]

    # 1. Kinematic: any analog-impossible single-frame jumps → Box.
    if jumps >= KINEMATIC_JUMPS_FOR_BOX:
        return LABEL_BOX, 0.99

    # 2. Keyboard: digital triggers, digital cstick, lots of button-only frames.
    if (
        btn_only >= BUTTON_ONLY_KEYBOARD
        and cstick_unique <= 5
        and trig_entropy == 0
    ):
        return LABEL_KEYBOARD, 0.9

    # 3. GCC (vanilla): analog triggers AND analog cstick AND healthy noise floor.
    if (
        trig_entropy >= TRIGGER_ANALOG_MIN
        and cstick_unique >= CSTICK_ANALOG_MIN
        and noise >= STICK_NOISE_HIGH
    ):
        return LABEL_GCC, 0.95

    # 4. GCC-Modded (Phob): analog triggers, analog cstick, but clamped stick
    #    noise and/or notch-grid quantization.
    if (
        trig_entropy >= TRIGGER_ANALOG_MIN
        and cstick_unique >= CSTICK_ANALOG_MIN
    ):
        clamped = noise < STICK_NOISE_HIGH
        notched = notch is not None and notch >= NOTCH_CLAMPED_MIN
        if clamped or notched:
            return LABEL_GCC_MODDED, 0.8
        return LABEL_GCC, 0.7  # borderline — analog but low signal

    # 5. Legacy tiebreaker.
    if stick_unique >= UNIQUE_RAW_GCC:
        return LABEL_GCC, 0.55
    if stick_unique <= UNIQUE_RAW_BOX:
        return LABEL_BOX, 0.55

    return LABEL_UNKNOWN, 0.3


def classify(port_frames) -> ClassificationResult | None:
    """Classify the controller used by one player. Returns None if input
    data is missing or the game is too short to analyse."""
    try:
        pre = port_frames.leader.pre
    except Exception:
        return None

    try:
        n = len(pre.joystick.x)
    except Exception:
        return None

    if n < MIN_FRAMES:
        return None

    signals = _extract_signals(pre)
    label, confidence = _cascade(signals)
    return ClassificationResult(
        label=label, confidence=confidence, signals=signals
    )
