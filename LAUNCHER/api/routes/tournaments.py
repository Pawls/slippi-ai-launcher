"""Tournament API: create and manage round-robin tournaments."""

import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.config import find_script
from LAUNCHER.tournament_store import TournamentStore

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


class TournamentCreate(BaseModel):
    name: str
    agents: list[dict]
    stage: str
    num_games: int


class TournamentUpdate(BaseModel):
    status: str | None = None
    name: str | None = None


class MatchupUpdate(BaseModel):
    status: str | None = None
    winner_index: int | None = None
    started: str | None = None
    finished: str | None = None


@router.get("/")
def list_tournaments():
    return get_state().tournament_store.get_all()


@router.get("/{tid}")
def get_tournament(tid: str):
    rec = get_state().tournament_store.get_tournament(tid)
    if rec is None:
        return {"error": "not found"}, 404
    return rec


@router.get("/{tid}/leaderboard")
def get_leaderboard(tid: str):
    rec = get_state().tournament_store.get_tournament(tid)
    if rec is None:
        return {"error": "not found"}, 404
    return TournamentStore.compute_leaderboard(rec)


@router.post("/")
def create_tournament(body: TournamentCreate):
    tid = get_state().tournament_store.create_tournament(
        body.name, body.agents, body.stage, body.num_games)
    return {"id": tid}


@router.put("/{tid}")
def update_tournament(tid: str, body: TournamentUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    get_state().tournament_store.update_tournament(tid, **fields)
    return {"ok": True}


@router.put("/{tid}/matchups/{matchup_id}")
def update_matchup(tid: str, matchup_id: str, body: MatchupUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    get_state().tournament_store.update_matchup(tid, matchup_id, **fields)
    return {"ok": True}


@router.delete("/{tid}")
def delete_tournament(tid: str):
    get_state().tournament_store.delete_tournament(tid)
    return {"ok": True}


# ── Tournament runner ──────────────────────────────────────────────────────


def _parse_winner(output: str, p1_index: int, p2_index: int) -> int | None:
    """Parse eval_two.py stdout to determine the winner.

    Looks for explicit "P1 wins" / "P2 wins" / "draw" lines, then falls back
    to parsing a "ko_diff: N" pattern. Returns None for a draw.
    """
    for line in reversed(output.split("\n")):
        ll = line.strip().lower()
        if "p1 wins" in ll or "player 1 wins" in ll:
            return p1_index
        if "p2 wins" in ll or "player 2 wins" in ll:
            return p2_index
        if "draw" in ll and ("result" in ll or "game" in ll):
            return None

    ko_match = re.search(r"ko[_ ]?diff[:\s]+([+-]?\d+)", output.lower())
    if ko_match:
        diff = int(ko_match.group(1))
        if diff > 0:
            return p1_index
        if diff < 0:
            return p2_index
    return None


class TournamentRunner:
    """Manages background tournament match execution.

    One runner per tournament; sequential matchups, supports cancellation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._procs: dict[str, subprocess.Popen] = {}

    def is_running(self, tid: str) -> bool:
        with self._lock:
            return tid in self._cancel_events

    def start(self, tid: str) -> bool:
        with self._lock:
            if tid in self._cancel_events:
                return False
            cancel = threading.Event()
            self._cancel_events[tid] = cancel

        threading.Thread(target=self._run, args=(tid, cancel), daemon=True).start()
        return True

    def stop(self, tid: str) -> bool:
        with self._lock:
            cancel = self._cancel_events.get(tid)
            proc = self._procs.get(tid)
        if not cancel:
            return False
        cancel.set()
        if proc is not None:
            self._kill(proc)
        return True

    def _kill(self, proc: subprocess.Popen):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _run(self, tid: str, cancel: threading.Event):
        s = get_state()
        cfg = s.cfg
        store = s.tournament_store

        try:
            tournament = store.get_tournament(tid)
            if tournament is None:
                return

            root = cfg.get("paths", "slippi_ai_root")
            iso = cfg.get("paths", "iso")
            dolphin = cfg.get("paths", "dolphin_dir")
            agents_dir = cfg.get("paths", "agents_dir")
            user_json = cfg.get("paths", "user_json")
            if not (root and iso and dolphin and agents_dir):
                store.update_tournament(tid, status="paused")
                return

            script = find_script(root, "scripts/eval_two.py")
            if not script:
                store.update_tournament(tid, status="paused")
                return

            store.update_tournament(tid, status="running")

            agents = tournament["agents"]
            stage = tournament["stage"]

            # Re-fetch matchups each iteration so external updates are picked up
            for matchup in tournament["matchups"]:
                if cancel.is_set():
                    break
                if matchup["status"] == "done":
                    continue

                p1 = agents[matchup["p1_index"]]
                p2 = agents[matchup["p2_index"]]
                p1_pkl = str(Path(agents_dir) / p1["agent_path"])
                p2_pkl = str(Path(agents_dir) / p2["agent_path"])
                delay = max(int(p1.get("delay", 2)), int(p2.get("delay", 2)))

                store.update_matchup(
                    tid, matchup["id"],
                    status="running",
                    started=datetime.now().isoformat())

                cmd = [
                    sys.executable, script,
                    f"--dolphin.path={dolphin}",
                    f"--dolphin.iso={iso}",
                    f"--dolphin.online_delay={delay}",
                    f"--dolphin.stage={stage}",
                    "--dolphin.headless",
                    "--dolphin.infinite_time",
                    f"--p1.ai.path={p1_pkl}",
                    f"--p1.character={p1['character']}",
                    f"--p2.ai.path={p2_pkl}",
                    f"--p2.character={p2['character']}",
                    "--num_games=1",
                ]
                if user_json:
                    cmd.append(f"--dolphin.user_json_path={user_json}")

                try:
                    if sys.platform == "win32":
                        proc = subprocess.Popen(
                            cmd, cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    else:
                        proc = subprocess.Popen(
                            cmd, cwd=root, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                    with self._lock:
                        self._procs[tid] = proc

                    stdout, _ = proc.communicate()

                    with self._lock:
                        self._procs.pop(tid, None)

                    if cancel.is_set():
                        # Roll back the running matchup so it can be retried
                        store.update_matchup(
                            tid, matchup["id"],
                            status="pending", started=None)
                        break

                    output = stdout.decode(errors="replace") if stdout else ""
                    winner = _parse_winner(
                        output, matchup["p1_index"], matchup["p2_index"])
                    store.update_matchup(
                        tid, matchup["id"],
                        status="done",
                        winner_index=winner,
                        finished=datetime.now().isoformat())
                except Exception:
                    store.update_matchup(
                        tid, matchup["id"],
                        status="error",
                        finished=datetime.now().isoformat())
                    with self._lock:
                        self._procs.pop(tid, None)

        finally:
            with self._lock:
                self._cancel_events.pop(tid, None)
                self._procs.pop(tid, None)

            t = store.get_tournament(tid)
            if t is not None:
                pending = sum(1 for m in t["matchups"] if m["status"] != "done")
                final = "done" if pending == 0 else "paused"
                store.update_tournament(tid, status=final)


_runner = TournamentRunner()


@router.post("/{tid}/run")
def run_tournament(tid: str):
    """Start running pending matchups in the background."""
    rec = get_state().tournament_store.get_tournament(tid)
    if rec is None:
        return {"error": "not found"}, 404
    started = _runner.start(tid)
    if not started:
        return {"status": "already_running"}
    return {"status": "started"}


@router.post("/{tid}/stop")
def stop_tournament(tid: str):
    """Cancel a running tournament."""
    stopped = _runner.stop(tid)
    return {"stopped": stopped}


@router.get("/{tid}/run-status")
def run_status(tid: str):
    """Check whether a tournament is currently running."""
    return {"running": _runner.is_running(tid)}
