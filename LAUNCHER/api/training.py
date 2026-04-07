"""Training command builder and process manager.

Extracts the command-building logic from the tkinter training screens
into a UI-agnostic module that both the tkinter GUI and the API can use.
"""

import enum
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from LAUNCHER.config import AppConfig, find_script


# ── Flag value formatting (extracted from train_il.py) ───────────────────────

def format_flag_value(value) -> str:
    """Format a Python value for use as a CLI flag argument."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, (list, tuple)):
        inner = ",".join(str(v) for v in value)
        return f"[{inner}]"
    return str(value)


# ── Script registries (extracted from screen modules) ────────────────────────

SCRIPTS = {
    # Imitation learning
    "imitation": {
        "label": "Imitation Learning",
        "script": "scripts/train.py",
        "flag_prefix": "config.",
        "wandb_flag": True,
    },
    "q_learning": {
        "label": "Q-Learning",
        "script": "scripts/train_q.py",
        "flag_prefix": "config.",
        "wandb_flag": True,
    },
    # Reinforcement learning
    "rl_single": {
        "label": "Single-Agent RL",
        "script": "slippi_ai/rl/run.py",
        "flag_prefix": "config.",
        "wandb_flag": True,
    },
    "rl_train_two": {
        "label": "Two-Agent RL",
        "script": "slippi_ai/rl/train_two.py",
        "flag_prefix": "config.",
        "wandb_flag": True,
    },
    # Evaluation
    "eval_watch": {
        "label": "Watch Game",
        "script": "scripts/eval_two.py",
        "flag_prefix": "",
        "wandb_flag": False,
    },
    "eval_benchmark": {
        "label": "Benchmark",
        "script": "scripts/run_evaluator.py",
        "flag_prefix": "",
        "wandb_flag": False,
    },
}


def build_command(
    script_key: str,
    config_overrides: dict[str, object],
    root: str,
    python: str | None = None,
) -> list[str]:
    """Build the CLI command for a training/eval script.

    Args:
        script_key: Key from SCRIPTS registry.
        config_overrides: Flat dict of flag paths to values (only non-default).
        root: Path to the slippi-ai repo root.
        python: Python executable path. Defaults to sys.executable.

    Returns:
        Command list suitable for subprocess.Popen.

    Raises:
        ValueError: If script_key is unknown or script file not found.
    """
    if script_key not in SCRIPTS:
        raise ValueError(f"Unknown script: {script_key}")

    info = SCRIPTS[script_key]
    script = find_script(root, info["script"])
    if not script:
        raise ValueError(f"Cannot find {info['script']} in {root}")

    cmd = [python or sys.executable, script]
    prefix = info["flag_prefix"]

    for flag_path, value in config_overrides.items():
        flag = prefix + flag_path
        cmd.append(f"--{flag}={format_flag_value(value)}")

    if info.get("wandb_flag"):
        cmd.append("--wandb.mode=disabled")

    return cmd


# ── Process manager ──────────────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    """Tracks a running training/eval subprocess."""
    id: str
    script_key: str
    cmd: list[str]
    proc: subprocess.Popen
    started: float = field(default_factory=time.time)
    status: str = "running"  # running | stopped | completed | failed
    return_code: int | None = None
    log_lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, line: str):
        with self._lock:
            self.log_lines.append(line)

    def get_logs(self, offset: int = 0) -> list[str]:
        with self._lock:
            return self.log_lines[offset:]


class ProcessManager:
    """Manages training/eval subprocesses.

    Thread-safe singleton that can be shared between the API and tkinter UI.
    """

    def __init__(self):
        self._processes: dict[str, ProcessInfo] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def launch(
        self,
        script_key: str,
        config_overrides: dict[str, object],
        cfg: AppConfig,
    ) -> ProcessInfo:
        """Launch a training/eval subprocess. Returns ProcessInfo."""
        root = cfg.get("paths", "slippi_ai_root")
        if not root:
            raise ValueError("slippi_ai_root not configured")

        cmd = build_command(script_key, config_overrides, root)

        env = os.environ.copy()
        env["OMP_NUM_THREADS"] = "1"
        env["TF_ENABLE_ONEDNN_OPTS"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        if sys.platform == "win32":
            proc = subprocess.Popen(
                cmd, cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        else:
            proc = subprocess.Popen(
                cmd, cwd=root, env=env, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)

        with self._lock:
            self._counter += 1
            pid = f"proc_{self._counter}"

        info = ProcessInfo(
            id=pid,
            script_key=script_key,
            cmd=cmd,
            proc=proc,
        )

        with self._lock:
            self._processes[pid] = info

        # Start log capture threads
        def _capture_stream(stream, prefix=""):
            for line in stream:
                info.append_log(prefix + line.rstrip("\n"))
            stream.close()

        threading.Thread(
            target=_capture_stream, args=(proc.stdout,), daemon=True
        ).start()
        threading.Thread(
            target=_capture_stream, args=(proc.stderr, "[stderr] "), daemon=True
        ).start()

        # Monitor for completion
        def _wait():
            proc.wait()
            info.return_code = proc.returncode
            info.status = "completed" if proc.returncode == 0 else "failed"

        threading.Thread(target=_wait, daemon=True).start()

        return info

    def stop(self, process_id: str) -> bool:
        """Stop a running process. Returns True if it was running."""
        with self._lock:
            info = self._processes.get(process_id)
        if info is None or info.status != "running":
            return False

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(info.proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(os.getpgid(info.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                info.proc.kill()
            except Exception:
                pass

        info.status = "stopped"
        return True

    def get(self, process_id: str) -> ProcessInfo | None:
        with self._lock:
            return self._processes.get(process_id)

    def list_all(self) -> list[dict]:
        """Return summary info for all tracked processes."""
        with self._lock:
            result = []
            for info in self._processes.values():
                # Poll to update status
                if info.status == "running" and info.proc.poll() is not None:
                    info.return_code = info.proc.returncode
                    info.status = (
                        "completed" if info.return_code == 0 else "failed")
                result.append({
                    "id": info.id,
                    "script_key": info.script_key,
                    "status": info.status,
                    "return_code": info.return_code,
                    "started": info.started,
                    "log_count": len(info.log_lines),
                })
            return result


# Global process manager instance
process_manager = ProcessManager()
