"""Agent library persistence: metadata, notes, and nicknames for trained agents."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from LAUNCHER.config import (
    list_agents, extract_delay_from_filename, extract_characters_from_filename,
    read_model_delay, read_allowed_characters, read_names_list,
)


_STORE_DIR = Path(os.path.abspath(__file__)).parent


class AgentStore:
    """JSON-backed agent metadata store for a single directory.

    Each record is a dict with fields:
        id, agent_path, nickname, characters, delay, training_type,
        source, notes, date_added, file_size_mb, missing
    """

    def __init__(self, path: Path | str | None = None, source: str = "agents"):
        if path:
            p = Path(path)
            self._path = p if p.is_absolute() else _STORE_DIR / p
        else:
            self._path = _STORE_DIR / "agent_library.json"
        self._source = source

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

    # ── Sync with filesystem ────────────────────────────────────────────

    def sync(self, scan_dir: str):
        """Scan directory and add records for new agents, flag missing ones."""
        if not scan_dir or not Path(scan_dir).is_dir():
            return
        records = self._load()
        known = {r["agent_path"] for r in records}
        disk_agents = set(list_agents(scan_dir))

        for rel_path in sorted(disk_agents - known):
            full = os.path.join(scan_dir, rel_path)
            records.append(self._build_record(rel_path, full))

        for rec in records:
            rec["missing"] = rec["agent_path"] not in disk_agents

        self._save(records)

    def _build_record(self, rel_path: str, full_path: str) -> dict:
        delay = extract_delay_from_filename(rel_path)
        chars = extract_characters_from_filename(rel_path)

        if delay is None:
            delay = read_model_delay(full_path)
        if not chars:
            chars = read_allowed_characters(full_path) or []

        # Cache the in-pickle name_map so the Play screen never has to re-parse
        # the .pkl just to populate the AI name dropdown.
        names = read_names_list(full_path) or []

        training_type = self._guess_training_type(rel_path)

        try:
            size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 1)
        except OSError:
            size_mb = 0

        return {
            "id": uuid.uuid4().hex[:12],
            "agent_path": rel_path,
            "nickname": "",
            "characters": chars if isinstance(chars, list) else [],
            "delay": delay,
            "names": names,
            "training_type": training_type,
            "source": self._source,
            "notes": "",
            "date_added": datetime.now().isoformat(),
            "file_size_mb": size_mb,
            "missing": False,
        }

    def ensure_names(self, agent_id: str, full_path: str) -> list[str]:
        """Lazy-fill the cached names list for a record that predates the field.

        Returns the cached list (possibly empty). If the record already has
        ``names``, the value is returned without touching disk. Otherwise the
        pickle is parsed once and the result is persisted to the JSON store.
        """
        records = self._load()
        for rec in records:
            if rec.get("id") != agent_id:
                continue
            cached = rec.get("names")
            if cached is not None:
                return cached
            names = read_names_list(full_path) or []
            rec["names"] = names
            self._save(records)
            return names
        return []

    def _guess_training_type(self, rel_path: str) -> str:
        lower = rel_path.lower()
        if self._source == "experiments":
            return "RL"
        if "train_two" in lower or "_rl" in lower or "/rl/" in lower:
            return "RL"
        if "imitation" in lower or "_il" in lower:
            return "IL"
        if self._source == "agents":
            return "IL"
        return ""

    # ── Public API ──────────────────────────────────────────────────────

    def get_all(self) -> list[dict]:
        """Return all records, newest first."""
        records = self._load()
        records.reverse()
        return records

    def get_by_path(self, agent_path: str) -> dict | None:
        for rec in self._load():
            if rec["agent_path"] == agent_path:
                return rec
        return None

    def update_agent(self, agent_id: str, **fields):
        allowed = {"nickname", "notes", "training_type"}
        records = self._load()
        for rec in records:
            if rec.get("id") == agent_id:
                for k, v in fields.items():
                    if k in allowed:
                        rec[k] = v
                break
        self._save(records)

    def delete_agent(self, agent_id: str):
        records = self._load()
        records = [r for r in records if r.get("id") != agent_id]
        self._save(records)

    def delete_file_and_record(self, agent_id: str, base_dir: str):
        records = self._load()
        for rec in records:
            if rec.get("id") == agent_id:
                full = os.path.join(base_dir, rec["agent_path"])
                try:
                    os.remove(full)
                except OSError:
                    pass
                break
        records = [r for r in records if r.get("id") != agent_id]
        self._save(records)

    @staticmethod
    def get_stats_for_agent(agent_path: str, match_store) -> dict:
        all_matches = match_store.get_all()
        wins = losses = draws = 0
        for m in all_matches:
            m_path = m.get("agent_path", "")
            if (m_path == agent_path or
                    os.path.basename(m_path) == os.path.basename(agent_path)):
                result = m.get("result")
                if result == "win":
                    wins += 1
                elif result == "loss":
                    losses += 1
                elif result == "draw":
                    draws += 1
        total = wins + losses + draws
        rated = wins + losses
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_rate": wins / rated if rated else 0.0,
        }
