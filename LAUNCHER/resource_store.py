"""Resource monitoring persistence: record GPU/CPU/RAM snapshots per session."""

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path


def _store_path() -> Path:
    return Path(os.path.abspath(__file__)).parent / "resource_sessions.json"


# ── System probing ─────────────────────────────────────────────────────────

def query_gpu() -> dict | None:
    """Query nvidia-smi for GPU stats. Returns None if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw,name",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    # May have multiple GPUs; take first line
    parts = [p.strip() for p in out.split("\n")[0].split(",")]
    if len(parts) < 6:
        return None
    try:
        return {
            "gpu_util": float(parts[0]),
            "gpu_mem_used": float(parts[1]),
            "gpu_mem_total": float(parts[2]),
            "gpu_temp": float(parts[3]),
            "gpu_power": float(parts[4]),
            "gpu_name": parts[5],
        }
    except (ValueError, IndexError):
        return None


def query_cpu_ram() -> dict:
    """Read CPU and RAM stats from /proc (Linux). Falls back to zeros."""
    cpu = 0.0
    ram_used = 0.0
    ram_total = 0.0

    # CPU: read /proc/stat twice with a short interval
    try:
        def _read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()  # "cpu  user nice system idle ..."
            vals = [int(v) for v in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals)
            return idle, total

        idle1, total1 = _read_cpu()
        time.sleep(0.1)
        idle2, total2 = _read_cpu()

        d_total = total2 - total1
        d_idle = idle2 - idle1
        if d_total > 0:
            cpu = (1.0 - d_idle / d_total) * 100.0
    except (OSError, ValueError, IndexError):
        pass

    # RAM: read /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        ram_total = meminfo.get("MemTotal", 0) / 1024  # MB
        available = meminfo.get("MemAvailable", 0) / 1024
        ram_used = ram_total - available
    except (OSError, ValueError):
        pass

    return {
        "cpu_percent": round(cpu, 1),
        "ram_used": round(ram_used, 0),
        "ram_total": round(ram_total, 0),
    }


def take_snapshot() -> dict:
    """Collect a single resource snapshot (GPU + CPU + RAM)."""
    snap = {"timestamp": time.time()}
    gpu = query_gpu()
    if gpu:
        snap.update(gpu)
    else:
        snap.update(gpu_util=0, gpu_mem_used=0, gpu_mem_total=0,
                    gpu_temp=0, gpu_power=0, gpu_name="N/A")
    snap.update(query_cpu_ram())
    return snap


# ── Session store ──────────────────────────────────────────────────────────

class ResourceStore:
    """JSON-backed resource monitoring session store.

    Each session contains metadata and a list of timestamped snapshots.
    Sessions are stored in a list; samples are embedded within each session.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _store_path()

    def _load(self) -> list[dict]:
        if not self._path.is_file():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, sessions: list[dict]):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(sessions, indent=2, default=str), encoding="utf-8")
        tmp.replace(self._path)

    def create_session(self, label: str) -> str:
        """Start a new recording session. Returns session ID."""
        sid = uuid.uuid4().hex[:12]
        sessions = self._load()
        sessions.append({
            "id": sid,
            "label": label,
            "started": datetime.now().isoformat(),
            "ended": None,
            "samples": [],
        })
        self._save(sessions)
        return sid

    def add_sample(self, session_id: str, snapshot: dict):
        """Append a snapshot to a session."""
        sessions = self._load()
        for s in sessions:
            if s["id"] == session_id:
                s["samples"].append(snapshot)
                break
        self._save(sessions)

    def end_session(self, session_id: str):
        """Mark a session as ended."""
        sessions = self._load()
        for s in sessions:
            if s["id"] == session_id:
                s["ended"] = datetime.now().isoformat()
                break
        self._save(sessions)

    def get_all_meta(self) -> list[dict]:
        """Return all sessions with summary stats (no raw samples)."""
        sessions = self._load()
        result = []
        for s in sessions:
            samples = s.get("samples", [])
            summary = self._summarize(samples)
            result.append({
                "id": s["id"],
                "label": s["label"],
                "started": s["started"],
                "ended": s["ended"],
                "num_samples": len(samples),
                "duration_sec": summary["duration_sec"],
                "summary": summary,
            })
        result.reverse()  # newest first
        return result

    def get_session(self, session_id: str) -> dict | None:
        """Return a full session including samples."""
        sessions = self._load()
        for s in sessions:
            if s["id"] == session_id:
                s["summary"] = self._summarize(s.get("samples", []))
                return s
        return None

    def delete_session(self, session_id: str):
        sessions = self._load()
        sessions = [s for s in sessions if s["id"] != session_id]
        self._save(sessions)

    @staticmethod
    def _summarize(samples: list[dict]) -> dict:
        """Compute summary statistics from a list of snapshots."""
        if not samples:
            return {
                "duration_sec": 0,
                "avg_gpu_util": 0, "peak_gpu_util": 0,
                "avg_gpu_mem": 0, "peak_gpu_mem": 0,
                "gpu_mem_total": 0,
                "avg_gpu_temp": 0, "peak_gpu_temp": 0,
                "avg_gpu_power": 0, "peak_gpu_power": 0,
                "avg_cpu": 0, "peak_cpu": 0,
                "avg_ram": 0, "peak_ram": 0, "ram_total": 0,
            }

        def _avg(key):
            vals = [s.get(key, 0) for s in samples]
            return round(sum(vals) / len(vals), 1) if vals else 0

        def _peak(key):
            vals = [s.get(key, 0) for s in samples]
            return round(max(vals), 1) if vals else 0

        ts = [s.get("timestamp", 0) for s in samples]
        duration = max(ts) - min(ts) if len(ts) > 1 else 0

        return {
            "duration_sec": round(duration),
            "avg_gpu_util": _avg("gpu_util"),
            "peak_gpu_util": _peak("gpu_util"),
            "avg_gpu_mem": _avg("gpu_mem_used"),
            "peak_gpu_mem": _peak("gpu_mem_used"),
            "gpu_mem_total": samples[-1].get("gpu_mem_total", 0),
            "avg_gpu_temp": _avg("gpu_temp"),
            "peak_gpu_temp": _peak("gpu_temp"),
            "avg_gpu_power": _avg("gpu_power"),
            "peak_gpu_power": _peak("gpu_power"),
            "avg_cpu": _avg("cpu_percent"),
            "peak_cpu": _peak("cpu_percent"),
            "avg_ram": _avg("ram_used"),
            "peak_ram": _peak("ram_used"),
            "ram_total": samples[-1].get("ram_total", 0),
        }
