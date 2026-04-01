"""Training log viewer: live stdout/stderr capture and metrics parsing."""

import os
import re
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk
from typing import Callable, IO, Optional


# ── Metric regex patterns per script type ───────────────────────────────────

_IL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Step",       re.compile(r"step=(\d+)")),
    ("Epoch",      re.compile(r"epoch=([0-9.]+)")),
    ("Steps/sec",  re.compile(r"sps=([0-9.]+)")),
    ("Min/sec",    re.compile(r"mps=([0-9.]+)")),
    ("Epoch/hr",   re.compile(r"eph=([0-9.e+-]+)")),
    ("Train Loss", re.compile(r"losses: train=([0-9.]+)")),
    ("Test Loss",  re.compile(r"losses: train=[0-9.]+ test=([0-9.]+)")),
    ("Data Time",  re.compile(r"timing: data=([0-9.]+)")),
    ("Step Time",  re.compile(r"timing:.*step=([0-9.]+)")),
    ("Eval KDPM",  re.compile(r"EVAL: kdpm=([0-9.-]+)")),
    ("Eval FPS",   re.compile(r"EVAL:.*fps=([0-9.]+)")),
]

_RL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Step",          re.compile(r"^Step: (\d+)", re.MULTILINE)),
    ("KO Diff/min",   re.compile(r"KO_diff_per_minute: ([0-9.-]+)")),
    ("Actor KL mean", re.compile(r"actor_kl: mean=([0-9.e+-]+)")),
    ("Actor KL max",  re.compile(r"actor_kl:.*max=([0-9.e+-]+)")),
    ("Teacher KL",    re.compile(r"teacher_kl: ([0-9.e+-]+)")),
    ("UEV",           re.compile(r"uev: ([0-9.-]+)")),
    ("Rollout",       re.compile(r"rollout: ([0-9.]+)")),
    ("Learn",         re.compile(r"learn: ([0-9.]+)")),
    ("Env",           re.compile(r"\benv: ([0-9.]+)")),
]

_EVAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("KO Diff/min", re.compile(r"KO_diff_per_minute: ([0-9.-]+)")),
    ("FPS",         re.compile(r"fps: ([0-9.]+)")),
]

_PATTERN_SETS: dict[str, list[tuple[str, re.Pattern]]] = {
    "il": _IL_PATTERNS,
    "rl": _RL_PATTERNS,
    "eval": _EVAL_PATTERNS,
}

# Maximum lines retained in the log text widget.
_MAX_LOG_LINES = 10_000


# ── OutputCapture ───────────────────────────────────────────────────────────

class OutputCapture:
    """Read subprocess stdout/stderr line-by-line and dispatch to callbacks.

    Spawns daemon threads for stdout, stderr, and wait.  All callbacks are
    invoked from background threads -- callers should marshal to the main
    thread (e.g. via ``widget.after``).
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
        on_complete: Callable[[int], None],
        log_file: Optional[IO] = None,
    ):
        self._proc = proc
        self._log_file = log_file
        self._on_complete = on_complete

        def _read_stream(stream, callback):
            try:
                for line in stream:
                    callback(line)
                    if self._log_file:
                        try:
                            self._log_file.write(line)
                            self._log_file.flush()
                        except Exception:
                            pass
            except Exception:
                pass

        def _wait():
            self._proc.wait()
            on_complete(self._proc.returncode)

        if proc.stdout:
            threading.Thread(
                target=_read_stream, args=(proc.stdout, on_stdout),
                daemon=True).start()
        if proc.stderr:
            threading.Thread(
                target=_read_stream, args=(proc.stderr, on_stderr),
                daemon=True).start()
        threading.Thread(target=_wait, daemon=True).start()


# ── TrainingLogPanel ────────────────────────────────────────────────────────

class TrainingLogPanel(ttk.Frame):
    """Composite widget with a Log tab and a Metrics tab."""

    def __init__(self, parent, script_type: str = "il", **kw):
        super().__init__(parent, **kw)
        self._script_type = script_type
        self._patterns = _PATTERN_SETS.get(script_type, _IL_PATTERNS)
        self._metric_values: dict[str, str] = {}
        self._prev_values: dict[str, str] = {}
        self._metric_rows: dict[str, str] = {}  # name -> treeview item id

        # ── Notebook with Log + Metrics tabs ────────────────────────────
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        # -- Log tab --
        log_frame = ttk.Frame(self._notebook)
        self._notebook.add(log_frame, text="Log")

        self._log_text = tk.Text(
            log_frame, height=12, wrap="none", state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#cccccc",
            insertbackground="#cccccc", selectbackground="#264f78")
        log_scroll_y = ttk.Scrollbar(
            log_frame, orient="vertical", command=self._log_text.yview)
        log_scroll_x = ttk.Scrollbar(
            log_frame, orient="horizontal", command=self._log_text.xview)
        self._log_text.config(
            yscrollcommand=log_scroll_y.set,
            xscrollcommand=log_scroll_x.set)

        log_scroll_y.pack(side="right", fill="y")
        log_scroll_x.pack(side="bottom", fill="x")
        self._log_text.pack(side="left", fill="both", expand=True)

        # Text tags for styling
        self._log_text.tag_config("stderr", foreground="#888888")
        self._log_text.tag_config("error", foreground="#f44747")
        self._log_text.tag_config("warning", foreground="#cca700")
        self._log_text.tag_config("metric", foreground="#4ec9b0")

        # -- Metrics tab --
        metrics_frame = ttk.Frame(self._notebook)
        self._notebook.add(metrics_frame, text="Metrics")

        self._tree = ttk.Treeview(
            metrics_frame, columns=("value", "trend"), show="headings",
            height=10)
        self._tree.heading("value", text="Current Value")
        self._tree.heading("trend", text="")
        self._tree.column("value", width=180, anchor="e")
        self._tree.column("trend", width=30, anchor="center")
        self._tree.pack(fill="both", expand=True, padx=4, pady=4)

        # Pre-populate metric rows
        self._init_metric_rows()

        # -- Button bar (below notebook) --
        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", pady=(2, 0))

        ttk.Button(btn_bar, text="Clear",
                   command=self.clear).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="Save Log...",
                   command=self._save_log).pack(side="left", padx=2)

        self._auto_scroll = True

    def _init_metric_rows(self):
        """Create one Treeview row per expected metric."""
        self._tree.delete(*self._tree.get_children())
        self._metric_rows.clear()
        self._metric_values.clear()
        self._prev_values.clear()
        for name, _pat in self._patterns:
            iid = self._tree.insert("", "end", text=name,
                                    values=("--", ""))
            # Treeview doesn't show 'text' with show="headings", so
            # use a first hidden column or just put name in values.
            self._metric_rows[name] = iid
        # Actually, with show="headings" the text column is hidden.
        # Re-do with a 'metric' column.
        self._tree.delete(*self._tree.get_children())
        self._tree["columns"] = ("metric", "value", "trend")
        self._tree.heading("metric", text="Metric")
        self._tree.heading("value", text="Value")
        self._tree.heading("trend", text="")
        self._tree.column("metric", width=140, anchor="w")
        self._tree.column("value", width=160, anchor="e")
        self._tree.column("trend", width=30, anchor="center")
        self._metric_rows.clear()
        for name, _pat in self._patterns:
            iid = self._tree.insert("", "end", values=(name, "--", ""))
            self._metric_rows[name] = iid

    # ── Public API ──────────────────────────────────────────────────────

    def set_script_type(self, script_type: str):
        """Switch metric parsing patterns (e.g. 'il', 'rl', 'eval')."""
        self._script_type = script_type
        self._patterns = _PATTERN_SETS.get(script_type, _IL_PATTERNS)
        self._init_metric_rows()

    def append_stdout(self, line: str):
        """Append a stdout line (call from main thread)."""
        tag = self._classify_line(line)
        self._append_line(line, tag)
        self._parse_metrics(line)

    def append_stderr(self, line: str):
        """Append a stderr line (call from main thread)."""
        self._append_line(line, "stderr")

    def clear(self):
        """Clear both the log and metrics display."""
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")
        self._metric_values.clear()
        self._prev_values.clear()
        self._init_metric_rows()

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _classify_line(line: str) -> str:
        """Pick a tag for a stdout line based on content."""
        lower = line.lower()
        if "error" in lower or "traceback" in lower:
            return "error"
        if "warning" in lower:
            return "warning"
        # Lines that look like metric output
        if any(k in line for k in ("step=", "Step:", "losses:", "KO_diff",
                                    "actor_kl:", "teacher_kl:", "uev:",
                                    "sps=", "timing:")):
            return "metric"
        return ""

    def _append_line(self, line: str, tag: str):
        """Insert a line into the log text widget."""
        self._log_text.config(state="normal")

        # Check auto-scroll: are we near the bottom?
        self._auto_scroll = self._log_text.yview()[1] >= 0.98

        tags = (tag,) if tag else ()
        self._log_text.insert("end", line, tags)

        # Prune old lines
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > _MAX_LOG_LINES:
            excess = line_count - _MAX_LOG_LINES
            self._log_text.delete("1.0", f"{excess + 1}.0")

        self._log_text.config(state="disabled")

        if self._auto_scroll:
            self._log_text.see("end")

    def _parse_metrics(self, line: str):
        """Extract metrics from a log line and update the Treeview."""
        for name, pattern in self._patterns:
            m = pattern.search(line)
            if m:
                value = m.group(1)
                old = self._metric_values.get(name)
                self._prev_values[name] = old if old is not None else value
                self._metric_values[name] = value

                # Compute trend arrow
                trend = ""
                try:
                    cur = float(value)
                    prev = float(self._prev_values[name])
                    if cur > prev:
                        trend = "\u25b2"   # ▲
                    elif cur < prev:
                        trend = "\u25bc"   # ▼
                except (ValueError, KeyError):
                    pass

                iid = self._metric_rows.get(name)
                if iid:
                    self._tree.item(iid, values=(name, value, trend))

    def _save_log(self):
        """Save the current log contents to a file."""
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save Log",
            defaultextension=".log",
            initialfile=f"training_{datetime.now():%Y%m%d_%H%M%S}.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                       ("All files", "*.*")])
        if path:
            content = self._log_text.get("1.0", "end-1c")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
