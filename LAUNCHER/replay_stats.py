"""Compute per-replay gameplay stats for the Advanced Filters feature.

v1 scope: APM (actionable-only), L-cancel success rate, stock-comeback
factor, and 0-to-death count. All computed in a single frame walker so
the expensive peppi frame materialization is paid once per replay.

v2 ideas (not yet implemented): wavedash count, tech-chase wins,
edgeguard attempts, neutral wins, opening-type breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


STATS_VERSION = 1

FRAMES_PER_SECOND = 60
ZERO_TO_DEATH_MAX_START_PERCENT = 15.0  # combo-start percent considered "0"


# Melee action-state IDs. "Non-actionable" = player cannot input commands
# this frame, so inputs here would be mashing/DI and should not count
# toward APM. Ranges taken from libmelee's ActionState enum.
#
#   0x00–0x0D  DEAD_*, REBIRTH_*, ENTRY_*
#   0x4B–0x5B  DAMAGE_* (hitstun)
#   0xDF–0xFE  CAPTURE_*, THROWN_* (grabbed / thrown)
_NON_ACTIONABLE_RANGES = (
    (0x00, 0x0D),
    (0x4B, 0x5B),
    (0xDF, 0xFE),
)


def _actionable_mask(states: np.ndarray) -> np.ndarray:
    mask = np.ones_like(states, dtype=bool)
    for lo, hi in _NON_ACTIONABLE_RANGES:
        mask &= ~((states >= lo) & (states <= hi))
    return mask


# L-cancel status codes from the Slippi spec (post-frame event 0x38).
L_CANCEL_SUCCESS = 1
L_CANCEL_FAIL = 2


@dataclass(slots=True)
class PerPlayerStats:
    apm: float
    l_cancel_pct: float | None          # None when the player never landed an aerial
    l_cancel_success: int
    l_cancel_fail: int
    zero_to_death: int
    actionable_frames: int


@dataclass(slots=True)
class ReplayStats:
    version: int
    per_player: dict[int, PerPlayerStats]      # keyed by port (1-based)
    stock_comeback_winner_port: int | None
    stock_comeback_max_deficit: int            # 0..3

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "per_player": {
                str(port): {
                    "apm": round(s.apm, 1),
                    "l_cancel_pct": (
                        None if s.l_cancel_pct is None
                        else round(s.l_cancel_pct, 3)
                    ),
                    "l_cancel_success": s.l_cancel_success,
                    "l_cancel_fail": s.l_cancel_fail,
                    "zero_to_death": s.zero_to_death,
                    "actionable_frames": s.actionable_frames,
                }
                for port, s in self.per_player.items()
            },
            "stock_comeback": {
                "winner_port": self.stock_comeback_winner_port,
                "max_deficit": self.stock_comeback_max_deficit,
            },
        }


# ── Per-player stat helpers ─────────────────────────────────────────────────

def _actionable_apm(states: np.ndarray, buttons: np.ndarray) -> tuple[float, int]:
    actionable = _actionable_mask(states)

    # Rising edges on any physical-button bit.
    edges = np.zeros_like(buttons, dtype=bool)
    edges[1:] = (buttons[1:].astype(np.int64) & ~buttons[:-1].astype(np.int64)) != 0
    counted = int((edges & actionable).sum())

    actionable_frames = int(actionable.sum())
    if actionable_frames <= 0:
        return 0.0, 0
    apm = counted * 60.0 * FRAMES_PER_SECOND / actionable_frames
    return apm, actionable_frames


def _l_cancel_stats(l_cancel_arr: np.ndarray | None) -> tuple[int, int, float | None]:
    if l_cancel_arr is None or len(l_cancel_arr) == 0:
        return 0, 0, None
    success = int((l_cancel_arr == L_CANCEL_SUCCESS).sum())
    fail = int((l_cancel_arr == L_CANCEL_FAIL).sum())
    total = success + fail
    if total == 0:
        return 0, 0, None
    return success, fail, success / total


def _zero_to_death_count(
    stocks: np.ndarray,
    percents: np.ndarray,
    combo_counts: np.ndarray,
) -> int:
    """Count stock losses that ended a combo which started at low percent.

    A "combo" starts when the defender's post-frame combo_count transitions
    from 0 to >0. If the defender dies before the combo ends AND the combo
    started while the defender was below ZERO_TO_DEATH_MAX_START_PERCENT,
    count it as a 0-to-death.
    """
    n = len(stocks)
    if n < 2:
        return 0

    count = 0
    combo_start_percent: float | None = None
    for i in range(1, n):
        prev_combo = int(combo_counts[i - 1])
        curr_combo = int(combo_counts[i])
        if prev_combo == 0 and curr_combo > 0:
            combo_start_percent = float(percents[i - 1])
        elif curr_combo == 0:
            combo_start_percent = None

        if int(stocks[i]) < int(stocks[i - 1]):
            if (
                combo_start_percent is not None
                and combo_start_percent <= ZERO_TO_DEATH_MAX_START_PERCENT
            ):
                count += 1
            combo_start_percent = None
    return count


# ── Public entry point ──────────────────────────────────────────────────────

def compute(game, placements: dict[int, int]) -> ReplayStats | None:
    """Compute stats for a parsed peppi_py game.

    ``placements`` maps port number (1-based) → placement (0 = winner,
    1 = loser, …). Pass the same dict `_parse_replay` already builds.
    """
    frames = game.frames
    if frames is None or not frames.ports:
        return None

    # Map peppi port-index → controller port (1-based as stored in start.players).
    port_of_idx: dict[int, int] = {}
    for idx, p in enumerate(game.start.players):
        port = p.port.value if hasattr(p.port, "value") else p.port
        port_of_idx[idx] = port

    per_player: dict[int, PerPlayerStats] = {}
    all_stocks: dict[int, np.ndarray] = {}

    for idx, port_frames in enumerate(frames.ports):
        port = port_of_idx.get(idx)
        if port is None:
            continue

        pre = port_frames.leader.pre
        post = port_frames.leader.post

        try:
            states = post.state.to_numpy()
            buttons = pre.buttons_physical.to_numpy()
            stocks = post.stocks.to_numpy()
            percents = post.percent.to_numpy()
            combos = post.combo_count.to_numpy()
        except Exception:
            continue

        apm, actionable_frames = _actionable_apm(states, buttons)
        l_succ, l_fail, l_rate = _l_cancel_stats(
            post.l_cancel.to_numpy() if post.l_cancel is not None else None
        )
        ztd = _zero_to_death_count(stocks, percents, combos)

        per_player[port] = PerPlayerStats(
            apm=apm,
            l_cancel_pct=l_rate,
            l_cancel_success=l_succ,
            l_cancel_fail=l_fail,
            zero_to_death=ztd,
            actionable_frames=actionable_frames,
        )
        all_stocks[port] = stocks

    winner_port = next(
        (port for port, place in placements.items() if place == 0),
        None,
    )
    max_deficit = 0
    if winner_port is not None and winner_port in all_stocks and len(all_stocks) >= 2:
        winner_stocks = all_stocks[winner_port]
        alive = winner_stocks >= 1
        if alive.any():
            for port, opp_stocks in all_stocks.items():
                if port == winner_port:
                    continue
                m = min(len(winner_stocks), len(opp_stocks))
                deficit = opp_stocks[:m][alive[:m]] - winner_stocks[:m][alive[:m]]
                if deficit.size > 0:
                    max_deficit = max(max_deficit, int(deficit.max()))

    return ReplayStats(
        version=STATS_VERSION,
        per_player=per_player,
        stock_comeback_winner_port=winner_port,
        stock_comeback_max_deficit=max(0, max_deficit),
    )
