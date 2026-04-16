"""Dataset pipeline API: prepare archives, upgrade, parse, stats, char filter.

Each subprocess step (prepare, upgrade, parse, compute_stats,
filter_by_character) is managed by the shared process_manager from
routes.training — identical launch/stop/logs semantics, just a different
command registry.

Step 5 (filter by stats) is handled in-process: it queries the
parsed.sqlite database directly to return live counts and to write
``stats_filter.json`` for use by Step 6.
"""

import json
import os
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.api.dataset_commands import DATASET_SCRIPTS, build_command
from LAUNCHER.api.training import process_manager

router = APIRouter(prefix="/dataset", tags=["dataset"])


class DatasetRunRequest(BaseModel):
    script_key: str
    values: dict[str, object] = {}


class StatsFilters(BaseModel):
    """Minimum-quality thresholds. Zero means "no filter" for that field."""
    min_l_cancel_rate: float = 0.0
    min_apm: float = 0.0
    min_wavedash_per_min: float = 0.0
    min_neutral_win: float = 0.0
    min_damage_dealt: float = 0.0
    min_game_length_sec: float = 0.0


class StatsRequest(BaseModel):
    root: str
    filters: StatsFilters = StatsFilters()


def _build_stats_where(filters: StatsFilters) -> tuple[str, list]:
    """Build the WHERE clause + params for a parsed.sqlite stats query.

    Mirrors the launcher's Tk implementation: NULL stats pass the
    L-cancel-rate and neutral-win-ratio filters (those metrics aren't
    meaningful on every replay); APM/wavedash/damage/length filter NULLs
    out so a missing value doesn't masquerade as a high one.
    """
    conditions = ["r.is_training = 1"]
    params: list = []
    if filters.min_l_cancel_rate > 0:
        conditions.append("(rs.l_cancel_rate >= ? OR rs.l_cancel_rate IS NULL)")
        params.append(filters.min_l_cancel_rate)
    if filters.min_apm > 0:
        conditions.append("rs.apm >= ?")
        params.append(filters.min_apm)
    if filters.min_wavedash_per_min > 0:
        conditions.append("rs.wavedash_per_min >= ?")
        params.append(filters.min_wavedash_per_min)
    if filters.min_neutral_win > 0:
        conditions.append("(rs.neutral_win_ratio >= ? OR rs.neutral_win_ratio IS NULL)")
        params.append(filters.min_neutral_win)
    if filters.min_damage_dealt > 0:
        conditions.append("rs.total_damage_dealt >= ?")
        params.append(filters.min_damage_dealt)
    if filters.min_game_length_sec > 0:
        # Tk version converts seconds to frames at 60 fps before comparing.
        conditions.append("rs.num_frames >= ?")
        params.append(filters.min_game_length_sec * 60)
    return " AND ".join(conditions), params


def _open_stats_db(root: str) -> tuple[sqlite3.Connection | None, str | None]:
    """Open parsed.sqlite read-only. Returns (conn, error_msg)."""
    if not root:
        return None, "Dataset root not specified"
    db_path = os.path.join(root, "parsed.sqlite")
    if not os.path.isfile(db_path):
        return None, "parsed.sqlite not found — run Parse Replays (step 3) first"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return None, f"Cannot open parsed.sqlite: {e}"
    # Stats table is populated by Step 4 — flag the absence explicitly so the
    # frontend can prompt the user instead of just showing zero matches.
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_stats'"
        ).fetchall()
        if not rows:
            conn.close()
            return None, "replay_stats table missing — run Compute Stats (step 4) first"
    except sqlite3.Error as e:
        conn.close()
        return None, f"Cannot read schema: {e}"
    return conn, None


@router.get("/scripts")
def list_scripts():
    """List available dataset pipeline scripts + their field schemas.

    The frontend renders a generic form per step from this.
    """
    return {
        key: {
            "label": spec["label"],
            "description": spec.get("description", ""),
            "fields": spec["fields"],
        }
        for key, spec in DATASET_SCRIPTS.items()
    }


@router.post("/preview-command")
def preview_command(body: DatasetRunRequest):
    """Preview the CLI command without launching."""
    s = get_state()
    root = s.cfg.get("paths", "slippi_ai_root")
    if not root:
        return {"error": "slippi_ai_root not configured"}
    try:
        cmd = build_command(body.script_key, body.values, root)
        return {"command": cmd, "command_str": " \\\n  ".join(cmd)}
    except ValueError as e:
        return {"error": str(e)}


@router.post("/launch")
def launch(body: DatasetRunRequest):
    """Launch a dataset pipeline step as a managed subprocess."""
    s = get_state()
    root = s.cfg.get("paths", "slippi_ai_root")
    if not root:
        return {"error": "slippi_ai_root not configured"}
    try:
        cmd = build_command(body.script_key, body.values, root)
    except ValueError as e:
        return {"error": str(e)}
    try:
        info = process_manager.launch_cmd(cmd, body.script_key, s.cfg)
    except ValueError as e:
        return {"error": str(e)}
    return {
        "id": info.id,
        "script_key": info.script_key,
        "status": info.status,
    }


@router.post("/{process_id}/stop")
def stop(process_id: str):
    return {"stopped": process_manager.stop(process_id)}


@router.get("/processes")
def list_processes():
    """List dataset processes. Filters the shared manager to this router's keys."""
    all_procs = process_manager.list_all()
    keys = set(DATASET_SCRIPTS.keys())
    return [p for p in all_procs if p["script_key"] in keys]


@router.get("/{process_id}")
def get_process(process_id: str):
    info = process_manager.get(process_id)
    if info is None:
        return {"error": "not found"}
    return {
        "id": info.id,
        "script_key": info.script_key,
        "status": info.status,
        "return_code": info.return_code,
        "started": info.started,
        "log_count": len(info.log_lines),
    }


@router.get("/{process_id}/logs")
def get_logs(process_id: str, offset: int = 0):
    info = process_manager.get(process_id)
    if info is None:
        return {"error": "not found"}
    lines = info.get_logs(offset)
    return {
        "lines": lines,
        "offset": offset,
        "total": offset + len(lines),
    }


# ── Step 5: Filter by Stats (in-process SQLite) ─────────────────────────────

@router.post("/stats/count")
def stats_count(body: StatsRequest):
    """Return the number of training replays matching the given thresholds.

    Used by the frontend's slider UI to show a live count as the user
    adjusts thresholds — fast because it's just a COUNT() query.
    """
    conn, err = _open_stats_db(body.root)
    if err:
        return {"error": err}
    try:
        where, params = _build_stats_where(body.filters)
        total = conn.execute(
            "SELECT COUNT(*) FROM replays WHERE is_training = 1"
        ).fetchone()[0]
        matching = conn.execute(
            f"""SELECT COUNT(DISTINCT rs.slp_md5)
                FROM replay_stats rs
                JOIN replays r ON rs.slp_md5 = r.slp_md5
                WHERE {where}""",
            params,
        ).fetchone()[0]
        return {"matching": matching, "total": total}
    except sqlite3.Error as e:
        return {"error": f"Query failed: {e}"}
    finally:
        conn.close()


@router.post("/stats/apply")
def stats_apply(body: StatsRequest):
    """Write ``stats_filter.json`` containing every matching slp_md5.

    Step 6 (Create Dataset) reads this file when present and includes only
    the listed replays in the generated meta.json.
    """
    conn, err = _open_stats_db(body.root)
    if err:
        return {"error": err}
    try:
        where, params = _build_stats_where(body.filters)
        rows = conn.execute(
            f"""SELECT DISTINCT rs.slp_md5
                FROM replay_stats rs
                JOIN replays r ON rs.slp_md5 = r.slp_md5
                WHERE {where}""",
            params,
        ).fetchall()
    except sqlite3.Error as e:
        return {"error": f"Query failed: {e}"}
    finally:
        conn.close()

    md5s = [r[0] for r in rows]
    out_path = os.path.join(body.root, "stats_filter.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(md5s, f)
    except OSError as e:
        return {"error": f"Cannot write {out_path}: {e}"}
    return {"count": len(md5s), "path": out_path}


@router.post("/stats/clear")
def stats_clear(body: StatsRequest):
    """Delete ``stats_filter.json`` so Step 6 falls back to all replays."""
    out_path = os.path.join(body.root, "stats_filter.json")
    if not os.path.isfile(out_path):
        return {"removed": False, "path": out_path}
    try:
        os.remove(out_path)
    except OSError as e:
        return {"error": f"Cannot remove {out_path}: {e}"}
    return {"removed": True, "path": out_path}


@router.get("/stats/applied")
def stats_applied(root: str):
    """Report whether ``stats_filter.json`` exists and how many entries.

    Surfaces the persisted state on page load so the user knows whether
    Step 6 will be filtered without re-running Apply.
    """
    out_path = os.path.join(root, "stats_filter.json")
    if not os.path.isfile(out_path):
        return {"exists": False, "path": out_path, "count": 0}
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"exists": True, "path": out_path, "count": len(data)}
        return {"exists": True, "path": out_path, "count": 0}
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"Cannot read {out_path}: {e}"}
