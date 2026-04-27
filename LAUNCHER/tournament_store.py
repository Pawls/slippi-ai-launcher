"""Tournament persistence: create, run, and query round-robin tournaments."""

import json
import os
import uuid
from datetime import datetime
from itertools import combinations
from pathlib import Path


def _store_path() -> Path:
    return Path(os.path.abspath(__file__)).parent / "tournament_history.json"


class TournamentStore:
    """JSON-backed tournament store.

    Each tournament record contains:
        id, name, created, status, stage, num_games, agents, matchups, leaderboard
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

    def create_tournament(self, name: str, agents: list[dict],
                          stage: str, num_games: int) -> str:
        """Create a round-robin tournament. Returns tournament ID.

        agents: list of dicts with keys: agent_path, character, delay, name
        """
        tid = uuid.uuid4().hex[:12]
        # Generate all pairwise matchups
        matchups = []
        for a, b in combinations(range(len(agents)), 2):
            for game_num in range(num_games):
                matchups.append({
                    "id": uuid.uuid4().hex[:8],
                    "p1_index": a,
                    "p2_index": b,
                    "game_num": game_num + 1,
                    "status": "pending",  # pending | running | done | error
                    "winner_index": None,  # index into agents, or None for draw
                    "started": None,
                    "finished": None,
                })

        record = {
            "id": tid,
            "name": name,
            "created": datetime.now().isoformat(),
            "status": "pending",  # pending | running | paused | done
            "stage": stage,
            "num_games": num_games,
            "agents": agents,
            "matchups": matchups,
        }

        records = self._load()
        records.append(record)
        self._save(records)
        return tid

    def get_all(self) -> list[dict]:
        records = self._load()
        records.reverse()
        return records

    def get_tournament(self, tid: str) -> dict | None:
        for rec in self._load():
            if rec["id"] == tid:
                return rec
        return None

    def update_tournament(self, tid: str, **fields):
        allowed = {"status", "name"}
        records = self._load()
        for rec in records:
            if rec["id"] == tid:
                for k, v in fields.items():
                    if k in allowed:
                        rec[k] = v
                break
        self._save(records)

    def update_matchup(self, tid: str, matchup_id: str, **fields):
        allowed = {"status", "winner_index", "started", "finished"}
        records = self._load()
        for rec in records:
            if rec["id"] == tid:
                for m in rec["matchups"]:
                    if m["id"] == matchup_id:
                        for k, v in fields.items():
                            if k in allowed:
                                m[k] = v
                        break
                break
        self._save(records)

    def delete_tournament(self, tid: str):
        records = self._load()
        records = [r for r in records if r["id"] != tid]
        self._save(records)

    @staticmethod
    def compute_leaderboard(tournament: dict) -> list[dict]:
        """Compute leaderboard from tournament matchups.

        Returns list of dicts sorted by points (win=3, draw=1, loss=0):
            agent_index, name, wins, losses, draws, points, games
        """
        agents = tournament["agents"]
        n = len(agents)
        stats = [{"wins": 0, "losses": 0, "draws": 0} for _ in range(n)]

        for m in tournament["matchups"]:
            if m["status"] != "done":
                continue
            p1 = m["p1_index"]
            p2 = m["p2_index"]
            w = m.get("winner_index")
            if w == p1:
                stats[p1]["wins"] += 1
                stats[p2]["losses"] += 1
            elif w == p2:
                stats[p2]["wins"] += 1
                stats[p1]["losses"] += 1
            else:
                stats[p1]["draws"] += 1
                stats[p2]["draws"] += 1

        board = []
        for i, agent in enumerate(agents):
            s = stats[i]
            games = s["wins"] + s["losses"] + s["draws"]
            points = s["wins"] * 3 + s["draws"]
            board.append({
                "agent_index": i,
                "name": agent.get("name") or os.path.basename(
                    agent.get("agent_path", "")),
                "character": agent.get("character", ""),
                "wins": s["wins"],
                "losses": s["losses"],
                "draws": s["draws"],
                "points": points,
                "games": games,
            })

        board.sort(key=lambda x: (-x["points"], -x["wins"], x["losses"]))
        return board
