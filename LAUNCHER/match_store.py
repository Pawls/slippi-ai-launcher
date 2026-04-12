"""Match history persistence: record, query, and update match sessions."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path


def _store_path() -> Path:
    """Default location for the match history file."""
    return Path(os.path.abspath(__file__)).parent / "match_history.json"


class MatchStore:
    """JSON-backed match history store.

    Each record is a dict with fields:
        id, timestamp_start, timestamp_end, duration_seconds,
        mode, agent_path, agent_name, ai_character,
        stage, input_delay, sample_temperature,
        connect_code, player_slot, result, user_notes
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _store_path()

    # ── Low-level I/O ───────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not self._path.is_file():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, records: list[dict]):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8")
        tmp.replace(self._path)

    # ── Public API ──────────────────────────────────────────────────────

    def start_match(self, **metadata) -> str:
        """Create a new match record. Returns the match ID."""
        match_id = uuid.uuid4().hex[:12]
        record = {
            "id": match_id,
            "timestamp_start": datetime.now().isoformat(),
            "timestamp_end": None,
            "duration_seconds": None,
            "mode": metadata.get("mode", ""),
            "agent_path": metadata.get("agent_path", ""),
            "agent_name": metadata.get("agent_name", ""),
            "ai_character": metadata.get("ai_character", ""),
            "stage": metadata.get("stage", ""),
            "input_delay": metadata.get("input_delay", 0),
            "sample_temperature": metadata.get("sample_temperature", 1.0),
            "connect_code": metadata.get("connect_code", ""),
            "player_slot": metadata.get("player_slot", 1),
            "result": None,
            "user_notes": "",
        }
        records = self._load()
        records.append(record)
        self._save(records)
        return match_id

    MIN_RECORDED_SECONDS = 30

    def end_match(self, match_id: str):
        """Set end timestamp and compute duration. Drop matches that were
        too short to count as actually played."""
        if not match_id:
            return
        records = self._load()
        for i, rec in enumerate(records):
            if rec.get("id") == match_id:
                now = datetime.now()
                rec["timestamp_end"] = now.isoformat()
                duration = None
                try:
                    start = datetime.fromisoformat(rec["timestamp_start"])
                    duration = round((now - start).total_seconds(), 1)
                    rec["duration_seconds"] = duration
                except (ValueError, KeyError):
                    pass
                if duration is not None and duration < self.MIN_RECORDED_SECONDS:
                    records.pop(i)
                break
        self._save(records)

    def update_match(self, match_id: str, **fields):
        """Update specific fields (result, user_notes) on a match."""
        if not match_id:
            return
        allowed = {"result", "user_notes"}
        records = self._load()
        for rec in records:
            if rec.get("id") == match_id:
                for k, v in fields.items():
                    if k in allowed:
                        rec[k] = v
                break
        self._save(records)

    def delete_match(self, match_id: str):
        """Remove a match record."""
        if not match_id:
            return
        records = self._load()
        records = [r for r in records if r.get("id") != match_id]
        self._save(records)

    def get_all(self) -> list[dict]:
        """Return all records, newest first."""
        records = self._load()
        records.reverse()
        return records

    def get_stats(self) -> dict:
        """Compute summary statistics."""
        records = self._load()
        total = len(records)
        total_seconds = sum(
            r.get("duration_seconds") or 0 for r in records)

        wins = sum(1 for r in records if r.get("result") == "win")
        losses = sum(1 for r in records if r.get("result") == "loss")
        rated = wins + losses

        # Games per agent (by basename)
        by_agent: dict[str, int] = {}
        for r in records:
            name = os.path.basename(r.get("agent_path", "") or "")
            if name:
                by_agent[name] = by_agent.get(name, 0) + 1

        # Games per character
        by_char: dict[str, int] = {}
        for r in records:
            ch = r.get("ai_character", "") or ""
            if ch:
                by_char[ch] = by_char.get(ch, 0) + 1

        return {
            "total_games": total,
            "total_seconds": total_seconds,
            "wins": wins,
            "losses": losses,
            "rated_games": rated,
            "win_rate": wins / rated if rated else 0.0,
            "by_agent": dict(sorted(
                by_agent.items(), key=lambda x: -x[1])),
            "by_character": dict(sorted(
                by_char.items(), key=lambda x: -x[1])),
        }
