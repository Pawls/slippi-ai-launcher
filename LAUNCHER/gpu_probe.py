"""Detect whether TensorFlow can use a GPU for inference.

Mirrors the runtime check inside `scripts/eval_two.py` / `scripts/netplay.py` so
the UI can't disagree with what the launched process will actually do. Result
is cached for the session; the probe runs in a background thread.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class GpuProbeResult:
    available: bool
    reason: str  # short human-readable status, e.g. "RTX 3090" or "No NVIDIA GPU"


# ── Module-level cache ────────────────────────────────────────────────────────

_result: Optional[GpuProbeResult] = None
_lock = threading.Lock()
_in_flight: Optional[threading.Thread] = None
_listeners: list[Callable[[GpuProbeResult], None]] = []


def _nvidia_smi_name() -> Optional[str]:
    """Cheap precheck — returns the first GPU's name, or None."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return out.split("\n")[0].strip() or None


_TF_PROBE_SCRIPT = (
    "import os, sys\n"
    "os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')\n"
    "try:\n"
    "    import tensorflow as tf\n"
    "    gpus = tf.config.list_physical_devices('GPU')\n"
    "    print('1' if gpus else '0')\n"
    "except Exception:\n"
    "    print('0')\n"
)


def _tf_sees_gpu() -> bool:
    """Run TF import in a subprocess using the launcher's interpreter."""
    try:
        out = subprocess.check_output(
            [sys.executable, "-c", _TF_PROBE_SCRIPT],
            text=True, timeout=30, stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return out.endswith("1")


def _run_probe() -> GpuProbeResult:
    name = _nvidia_smi_name()
    if not name:
        return GpuProbeResult(False, "No NVIDIA GPU detected")
    if not _tf_sees_gpu():
        if sys.platform == "win32":
            return GpuProbeResult(
                False,
                f"{name} found, but TensorFlow cannot use it "
                "(native Windows TF has no GPU support; use WSL2)")
        return GpuProbeResult(
            False, f"{name} found, but TensorFlow cannot use it")
    return GpuProbeResult(True, name)


# ── Public API ────────────────────────────────────────────────────────────────

def cached() -> Optional[GpuProbeResult]:
    """Return the cached result without triggering a probe."""
    with _lock:
        return _result


def probe_async(callback: Optional[Callable[[GpuProbeResult], None]] = None) -> None:
    """Kick off the probe if not already running. Fires callback on completion.

    Callback is invoked from the probe thread; marshal to the UI thread via
    `widget.after(0, ...)` if needed.
    """
    global _in_flight
    with _lock:
        if _result is not None:
            if callback:
                callback(_result)
            return
        if callback:
            _listeners.append(callback)
        if _in_flight is not None:
            return

        def worker():
            global _in_flight
            result = _run_probe()
            with _lock:
                global _result
                _result = result
                listeners = list(_listeners)
                _listeners.clear()
                _in_flight = None
            for cb in listeners:
                try:
                    cb(result)
                except Exception:
                    pass

        _in_flight = threading.Thread(target=worker, daemon=True)
        _in_flight.start()


def attach_status_label(parent, prefix: str = "GPU:") -> "object":
    """Create and pack a small GPU status label into `parent`.

    Shows "Detecting..." until the background probe finishes, then the GPU
    name (green) or the reason it's unavailable (gray). Returns the frame.
    """
    import tkinter as tk
    from tkinter import ttk

    frame = ttk.Frame(parent)
    ttk.Label(frame, text=prefix, font=("TkDefaultFont", 9, "bold")).pack(
        side="left")
    status = ttk.Label(frame, text="Detecting...", foreground="gray")
    status.pack(side="left", padx=(6, 0))

    def apply(result: GpuProbeResult):
        color = "#2a7a2a" if result.available else "gray"
        status.configure(text=result.reason, foreground=color)

    cached_val = cached()
    if cached_val is not None:
        apply(cached_val)
    else:
        def on_result(r: GpuProbeResult):
            try:
                status.after(0, lambda: apply(r))
            except tk.TclError:
                pass  # widget destroyed before probe finished
        probe_async(on_result)

    return frame


def probe_sync(timeout: float = 30.0) -> GpuProbeResult:
    """Blocking probe — used at launch time for the pre-flight check."""
    cached_val = cached()
    if cached_val is not None:
        return cached_val
    done = threading.Event()
    holder: list[GpuProbeResult] = []

    def cb(r: GpuProbeResult):
        holder.append(r)
        done.set()

    probe_async(cb)
    done.wait(timeout=timeout)
    return holder[0] if holder else GpuProbeResult(False, "GPU probe timed out")
