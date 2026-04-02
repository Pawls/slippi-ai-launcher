"""GPU & Resource Monitor: live stats, session recording, and run comparison."""

import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from LAUNCHER.config import AppConfig
from LAUNCHER.resource_store import ResourceStore, take_snapshot
from LAUNCHER.screens import Screen


# ── Sparkline canvas widget ────────────────────────────────────────────────

class _Sparkline(tk.Canvas):
    """Mini line chart drawn on a Canvas. Fixed-size ring buffer of values."""

    def __init__(self, parent, width=220, height=50, max_points=120,
                 line_color="#4CAF50", fill_color="#4CAF5030",
                 bg="#1e1e1e", **kw):
        super().__init__(parent, width=width, height=height,
                         bg=bg, highlightthickness=0, **kw)
        self._cw = width
        self._ch = height
        self._line_color = line_color
        self._fill_color = fill_color
        self._max_points = max_points
        self._values: deque[float] = deque(maxlen=max_points)
        self._y_min = 0.0
        self._y_max = 100.0

    def set_range(self, y_min: float, y_max: float):
        self._y_min = y_min
        self._y_max = y_max

    def add_value(self, v: float):
        self._values.append(v)
        self._redraw()

    def clear(self):
        self._values.clear()
        self.delete("all")

    def _redraw(self):
        self.delete("all")
        n = len(self._values)
        if n < 2:
            return

        pad_y = 4
        usable_h = self._ch - 2 * pad_y
        y_range = self._y_max - self._y_min or 1.0

        def _x(i):
            return i / (self._max_points - 1) * self._cw

        def _y(v):
            frac = (v - self._y_min) / y_range
            return self._ch - pad_y - frac * usable_h

        # Build point list for fill polygon (area under curve)
        pts_line = []
        for i, v in enumerate(self._values):
            offset = self._max_points - n  # align to right
            pts_line.append((_x(i + offset), _y(v)))

        if pts_line:
            # Fill: close polygon to bottom
            fill_pts = (
                [(pts_line[0][0], self._ch)]
                + pts_line
                + [(pts_line[-1][0], self._h)]
            )
            flat = [c for pt in fill_pts for c in pt]
            self.create_polygon(flat, fill=self._fill_color, outline="")

            # Line
            flat_line = [c for pt in pts_line for c in pt]
            self.create_line(flat_line, fill=self._line_color, width=1.5,
                             smooth=True)


# ── Metric gauge: label + value + sparkline ────────────────────────────────

class _MetricGauge(ttk.Frame):
    """Single metric display: name, current value, sparkline chart."""

    def __init__(self, parent, label: str, unit: str = "%",
                 y_min: float = 0, y_max: float = 100,
                 line_color: str = "#4CAF50", **kw):
        super().__init__(parent, **kw)
        self._unit = unit

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text=label, font=("TkDefaultFont", 9)).pack(
            side="left")
        self._val_lbl = ttk.Label(
            top, text="--", font=("Consolas", 11, "bold"), width=12,
            anchor="e")
        self._val_lbl.pack(side="right")

        self._spark = _Sparkline(
            self, width=220, height=46, line_color=line_color,
            fill_color=line_color + "30")
        self._spark.set_range(y_min, y_max)
        self._spark.pack(fill="x", pady=(2, 0))

    def update_value(self, value: float, display: str | None = None):
        text = display if display else f"{value:.1f} {self._unit}"
        self._val_lbl.config(text=text)
        self._spark.add_value(value)

    def clear(self):
        self._val_lbl.config(text="--")
        self._spark.clear()


# ── Compare dialog ─────────────────────────────────────────────────────────

class _CompareDialog(tk.Toplevel):
    """Side-by-side comparison of two recording sessions."""

    def __init__(self, parent, session_a: dict, session_b: dict):
        super().__init__(parent)
        self.title("Compare Sessions")
        self.transient(parent)
        self.minsize(650, 420)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        # Header
        ttk.Label(frame, text="Session Comparison",
                  font=("TkDefaultFont", 13, "bold")).pack(anchor="w")

        sa = session_a.get("summary", {})
        sb = session_b.get("summary", {})

        # Comparison table
        cols = ("metric", "session_a", "session_b", "diff")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)
        tree.heading("metric", text="Metric")
        tree.heading("session_a", text=session_a.get("label", "A"))
        tree.heading("session_b", text=session_b.get("label", "B"))
        tree.heading("diff", text="Diff")
        tree.column("metric", width=160, anchor="w")
        tree.column("session_a", width=140, anchor="e")
        tree.column("session_b", width=140, anchor="e")
        tree.column("diff", width=100, anchor="e")
        tree.pack(fill="both", expand=True, pady=(8, 0))

        tree.tag_configure("better", foreground="#2e7d32")
        tree.tag_configure("worse", foreground="#c62828")
        tree.tag_configure("header", background="#e0e0e0",
                           font=("TkDefaultFont", 9, "bold"))

        def _add_header(text):
            tree.insert("", "end", values=(text, "", "", ""), tags=("header",))

        def _add_row(label, key_a, key_b, unit="", lower_better=False,
                     fmt=".1f"):
            va = sa.get(key_a, 0)
            vb = sb.get(key_b, 0)
            diff = vb - va
            diff_str = f"{diff:+{fmt}} {unit}".strip()
            # Determine which direction is "better"
            if lower_better:
                tag = "better" if diff < -0.5 else (
                    "worse" if diff > 0.5 else "")
            else:
                tag = "worse" if diff < -0.5 else (
                    "better" if diff > 0.5 else "")
            tree.insert("", "end", values=(
                label,
                f"{va:{fmt}} {unit}".strip(),
                f"{vb:{fmt}} {unit}".strip(),
                diff_str,
            ), tags=(tag,) if tag else ())

        _add_header("GPU")
        _add_row("Avg Utilization", "avg_gpu_util", "avg_gpu_util", "%")
        _add_row("Peak Utilization", "peak_gpu_util", "peak_gpu_util", "%")
        _add_row("Avg Memory", "avg_gpu_mem", "avg_gpu_mem", "MB")
        _add_row("Peak Memory", "peak_gpu_mem", "peak_gpu_mem", "MB")
        _add_row("Avg Temperature", "avg_gpu_temp", "avg_gpu_temp",
                 "\u00b0C", lower_better=True)
        _add_row("Peak Temperature", "peak_gpu_temp", "peak_gpu_temp",
                 "\u00b0C", lower_better=True)
        _add_row("Avg Power", "avg_gpu_power", "avg_gpu_power",
                 "W", lower_better=True)
        _add_row("Peak Power", "peak_gpu_power", "peak_gpu_power",
                 "W", lower_better=True)

        _add_header("System")
        _add_row("Avg CPU", "avg_cpu", "avg_cpu", "%")
        _add_row("Peak CPU", "peak_cpu", "peak_cpu", "%")
        _add_row("Avg RAM", "avg_ram", "avg_ram", "MB")
        _add_row("Peak RAM", "peak_ram", "peak_ram", "MB")

        _add_header("Duration")
        dur_a = sa.get("duration_sec", 0)
        dur_b = sb.get("duration_sec", 0)
        tree.insert("", "end", values=(
            "Recording time",
            _fmt_duration(dur_a),
            _fmt_duration(dur_b),
            "",
        ))

        # Close button
        ttk.Button(frame, text="Close", command=self.destroy).pack(
            anchor="e", pady=(8, 0))

        self.grab_set()
        self.focus_set()


# ── Helpers ────────────────────────────────────────────────────────────────

def _fmt_duration(seconds) -> str:
    if not seconds:
        return "--"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s"


def _fmt_time(iso_str: str) -> str:
    if not iso_str:
        return "--"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d  %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


# ── Main screen ────────────────────────────────────────────────────────────

class ResourceMonitorScreen(Screen):
    """GPU & system resource monitor with live updates, recording, and
    session comparison."""

    POLL_MS = 2000  # live poll interval in milliseconds

    def __init__(self, parent, navigator, cfg: AppConfig,
                 resource_store: ResourceStore):
        super().__init__(parent, navigator, cfg)
        self._store = resource_store
        self._monitoring = False
        self._recording = False
        self._record_session_id: str | None = None
        self._poll_after_id: str | None = None

        self._build_ui()

    def _build_ui(self):
        self._add_back_button()

        ttk.Label(self, text="GPU & Resource Monitor",
                  font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w", padx=10, pady=(4, 0))

        # ── Notebook: Live | Sessions ──────────────────────────────────
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        self._build_live_tab()
        self._build_sessions_tab()

    # ── Live monitor tab ───────────────────────────────────────────────

    def _build_live_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="Live Monitor")

        # Status / control bar
        ctrl = ttk.Frame(tab)
        ctrl.pack(fill="x", padx=6, pady=6)

        self._status_lbl = ttk.Label(ctrl, text="Stopped", foreground="gray")
        self._status_lbl.pack(side="left")

        self._gpu_name_lbl = ttk.Label(ctrl, text="", foreground="gray")
        self._gpu_name_lbl.pack(side="left", padx=(12, 0))

        self._rec_btn = tk.Button(
            ctrl, text="\u23fa  Record", bg="#f44336", fg="white",
            font=("TkDefaultFont", 9, "bold"), cursor="hand2",
            command=self._toggle_recording)
        self._rec_btn.pack(side="right", padx=(4, 0))

        self._rec_lbl = ttk.Label(ctrl, text="", foreground="#f44336")
        self._rec_lbl.pack(side="right")

        # Gauges: 2-column grid
        gauge_frame = ttk.Frame(tab)
        gauge_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        gauge_frame.columnconfigure(0, weight=1)
        gauge_frame.columnconfigure(1, weight=1)

        self._gpu_util_gauge = _MetricGauge(
            gauge_frame, "GPU Utilization", "%", 0, 100, "#4CAF50")
        self._gpu_util_gauge.grid(row=0, column=0, sticky="ew",
                                  padx=(0, 6), pady=4)

        self._gpu_mem_gauge = _MetricGauge(
            gauge_frame, "GPU Memory", "MB", 0, 24576, "#2196F3")
        self._gpu_mem_gauge.grid(row=0, column=1, sticky="ew",
                                 padx=(6, 0), pady=4)

        self._gpu_temp_gauge = _MetricGauge(
            gauge_frame, "GPU Temperature", "\u00b0C", 20, 100, "#FF9800")
        self._gpu_temp_gauge.grid(row=1, column=0, sticky="ew",
                                  padx=(0, 6), pady=4)

        self._gpu_power_gauge = _MetricGauge(
            gauge_frame, "GPU Power", "W", 0, 370, "#E91E63")
        self._gpu_power_gauge.grid(row=1, column=1, sticky="ew",
                                   padx=(6, 0), pady=4)

        self._cpu_gauge = _MetricGauge(
            gauge_frame, "CPU Utilization", "%", 0, 100, "#9C27B0")
        self._cpu_gauge.grid(row=2, column=0, sticky="ew",
                             padx=(0, 6), pady=4)

        self._ram_gauge = _MetricGauge(
            gauge_frame, "RAM Usage", "MB", 0, 65536, "#00897B")
        self._ram_gauge.grid(row=2, column=1, sticky="ew",
                             padx=(6, 0), pady=4)

    # ── Sessions tab ───────────────────────────────────────────────────

    def _build_sessions_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="Sessions")

        # Toolbar
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x", padx=6, pady=6)

        self._compare_btn = ttk.Button(
            toolbar, text="Compare Selected (2)",
            command=self._on_compare)
        self._compare_btn.pack(side="left")
        ttk.Button(
            toolbar, text="Delete Selected",
            command=self._on_delete_session).pack(side="left", padx=4)
        ttk.Button(
            toolbar, text="Refresh",
            command=self._populate_sessions).pack(side="left", padx=4)

        # Session list
        cols = ("label", "started", "duration", "samples",
                "avg_gpu", "peak_gpu", "peak_mem", "avg_cpu")
        self._sess_tree = ttk.Treeview(
            tab, columns=cols, show="headings", height=10,
            selectmode="extended")
        for col, hdr, w, anchor in [
            ("label", "Label", 160, "w"),
            ("started", "Started", 120, "w"),
            ("duration", "Duration", 80, "w"),
            ("samples", "Samples", 65, "center"),
            ("avg_gpu", "Avg GPU %", 80, "e"),
            ("peak_gpu", "Peak GPU %", 85, "e"),
            ("peak_mem", "Peak Mem MB", 95, "e"),
            ("avg_cpu", "Avg CPU %", 80, "e"),
        ]:
            self._sess_tree.heading(col, text=hdr)
            self._sess_tree.column(col, width=w, anchor=anchor, stretch=False)

        sess_scroll = ttk.Scrollbar(
            tab, orient="vertical", command=self._sess_tree.yview)
        self._sess_tree.configure(yscrollcommand=sess_scroll.set)
        self._sess_tree.pack(side="left", fill="both", expand=True,
                             padx=(6, 0), pady=(0, 6))
        sess_scroll.pack(side="right", fill="y", padx=(0, 6), pady=(0, 6))

        self._sess_tree.bind("<Double-1>", self._on_session_detail)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_enter(self):
        self._start_monitoring()
        self._populate_sessions()

    def on_leave(self):
        self._stop_monitoring()

    def _start_monitoring(self):
        if self._monitoring:
            return
        self._monitoring = True
        self._status_lbl.config(text="Monitoring", foreground="#4CAF50")
        self._poll()

    def _stop_monitoring(self):
        self._monitoring = False
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._status_lbl.config(text="Stopped", foreground="gray")

    def _poll(self):
        """Take a snapshot in a background thread, then update UI."""
        if not self._monitoring:
            return

        def _worker():
            snap = take_snapshot()
            if self._monitoring:
                self.after(0, lambda: self._update_gauges(snap))

        threading.Thread(target=_worker, daemon=True).start()
        self._poll_after_id = self.after(self.POLL_MS, self._poll)

    def _update_gauges(self, snap: dict):
        gpu_name = snap.get("gpu_name", "")
        if gpu_name and gpu_name != "N/A":
            self._gpu_name_lbl.config(text=gpu_name)

        mem_total = snap.get("gpu_mem_total", 1) or 1
        self._gpu_util_gauge.update_value(snap.get("gpu_util", 0))
        self._gpu_mem_gauge.update_value(
            snap.get("gpu_mem_used", 0),
            f"{snap.get('gpu_mem_used', 0):.0f} / {mem_total:.0f} MB")
        self._gpu_mem_gauge._spark.set_range(0, mem_total)
        self._gpu_temp_gauge.update_value(snap.get("gpu_temp", 0))
        self._gpu_power_gauge.update_value(snap.get("gpu_power", 0))
        self._cpu_gauge.update_value(snap.get("cpu_percent", 0))

        ram_total = snap.get("ram_total", 1) or 1
        self._ram_gauge.update_value(
            snap.get("ram_used", 0),
            f"{snap.get('ram_used', 0):.0f} / {ram_total:.0f} MB")
        self._ram_gauge._spark.set_range(0, ram_total)

        # If recording, save sample
        if self._recording and self._record_session_id:
            self._store.add_sample(self._record_session_id, snap)

    # ── Recording ──────────────────────────────────────────────────────

    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        label = simpledialog.askstring(
            "Record Session",
            "Label for this recording session:",
            initialvalue=f"Session {datetime.now():%m/%d %H:%M}",
            parent=self)
        if not label:
            return
        self._record_session_id = self._store.create_session(label.strip())
        self._recording = True
        self._rec_btn.config(text="\u23f9  Stop Recording", bg="#b71c1c")
        self._rec_lbl.config(text=f"REC: {label.strip()}")

    def _stop_recording(self):
        if self._record_session_id:
            self._store.end_session(self._record_session_id)
        self._recording = False
        self._record_session_id = None
        self._rec_btn.config(text="\u23fa  Record", bg="#f44336")
        self._rec_lbl.config(text="")
        self._populate_sessions()

    # ── Sessions list ──────────────────────────────────────────────────

    def _populate_sessions(self):
        self._sess_tree.delete(*self._sess_tree.get_children())
        for s in self._store.get_all_meta():
            sm = s.get("summary", {})
            self._sess_tree.insert("", "end", iid=s["id"], values=(
                s["label"],
                _fmt_time(s["started"]),
                _fmt_duration(sm.get("duration_sec", 0)),
                s["num_samples"],
                f"{sm.get('avg_gpu_util', 0):.1f}",
                f"{sm.get('peak_gpu_util', 0):.1f}",
                f"{sm.get('peak_gpu_mem', 0):.0f}",
                f"{sm.get('avg_cpu', 0):.1f}",
            ))

    def _on_compare(self):
        sel = self._sess_tree.selection()
        if len(sel) != 2:
            messagebox.showinfo(
                "Compare",
                "Select exactly 2 sessions to compare.",
                parent=self)
            return
        sa = self._store.get_session(sel[0])
        sb = self._store.get_session(sel[1])
        if not sa or not sb:
            return
        _CompareDialog(self, sa, sb)

    def _on_delete_session(self):
        sel = self._sess_tree.selection()
        if not sel:
            return
        n = len(sel)
        if not messagebox.askyesno(
                "Delete",
                f"Delete {n} session{'s' if n > 1 else ''}?",
                parent=self):
            return
        for sid in sel:
            self._store.delete_session(sid)
        self._populate_sessions()

    def _on_session_detail(self, _event=None):
        sel = self._sess_tree.selection()
        if not sel:
            return
        session = self._store.get_session(sel[0])
        if not session:
            return
        _SessionDetailDialog(self, session)


# ── Session detail dialog ──────────────────────────────────────────────────

class _SessionDetailDialog(tk.Toplevel):
    """Show detailed stats and time-series for a single session."""

    def __init__(self, parent, session: dict):
        super().__init__(parent)
        self.title(f"Session: {session.get('label', '?')}")
        self.transient(parent)
        self.minsize(550, 450)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        sm = session.get("summary", {})
        samples = session.get("samples", [])

        # Header
        ttk.Label(frame, text=session.get("label", ""),
                  font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(frame,
                  text=f"Started: {_fmt_time(session.get('started', ''))}  "
                       f"| Duration: {_fmt_duration(sm.get('duration_sec'))}  "
                       f"| Samples: {len(samples)}",
                  foreground="gray").pack(anchor="w", pady=(0, 8))

        # Summary stats table
        summary_lf = ttk.LabelFrame(frame, text="Summary", padding=6)
        summary_lf.pack(fill="x", pady=(0, 8))

        cols = ("metric", "average", "peak")
        tree = ttk.Treeview(summary_lf, columns=cols, show="headings",
                            height=8)
        tree.heading("metric", text="Metric")
        tree.heading("average", text="Average")
        tree.heading("peak", text="Peak")
        tree.column("metric", width=160, anchor="w")
        tree.column("average", width=120, anchor="e")
        tree.column("peak", width=120, anchor="e")
        tree.pack(fill="x")

        rows = [
            ("GPU Utilization",
             f"{sm.get('avg_gpu_util', 0):.1f} %",
             f"{sm.get('peak_gpu_util', 0):.1f} %"),
            ("GPU Memory",
             f"{sm.get('avg_gpu_mem', 0):.0f} / {sm.get('gpu_mem_total', 0):.0f} MB",
             f"{sm.get('peak_gpu_mem', 0):.0f} MB"),
            ("GPU Temperature",
             f"{sm.get('avg_gpu_temp', 0):.1f} \u00b0C",
             f"{sm.get('peak_gpu_temp', 0):.1f} \u00b0C"),
            ("GPU Power",
             f"{sm.get('avg_gpu_power', 0):.1f} W",
             f"{sm.get('peak_gpu_power', 0):.1f} W"),
            ("CPU",
             f"{sm.get('avg_cpu', 0):.1f} %",
             f"{sm.get('peak_cpu', 0):.1f} %"),
            ("RAM",
             f"{sm.get('avg_ram', 0):.0f} / {sm.get('ram_total', 0):.0f} MB",
             f"{sm.get('peak_ram', 0):.0f} MB"),
        ]
        for label, avg, peak in rows:
            tree.insert("", "end", values=(label, avg, peak))

        # Mini sparklines for session data
        if samples:
            chart_lf = ttk.LabelFrame(frame, text="Timeline", padding=6)
            chart_lf.pack(fill="both", expand=True, pady=(0, 4))

            chart_lf.columnconfigure(0, weight=1)
            chart_lf.columnconfigure(1, weight=1)

            gpu_spark = _Sparkline(chart_lf, width=240, height=60,
                                   line_color="#4CAF50")
            mem_total = sm.get("gpu_mem_total", 1) or 1
            mem_spark = _Sparkline(chart_lf, width=240, height=60,
                                   line_color="#2196F3")
            mem_spark.set_range(0, mem_total)

            ttk.Label(chart_lf, text="GPU Util %",
                      font=("TkDefaultFont", 8)).grid(
                row=0, column=0, sticky="w")
            gpu_spark.grid(row=1, column=0, sticky="ew", padx=(0, 4))

            ttk.Label(chart_lf, text="GPU Memory MB",
                      font=("TkDefaultFont", 8)).grid(
                row=0, column=1, sticky="w")
            mem_spark.grid(row=1, column=1, sticky="ew", padx=(4, 0))

            for s in samples:
                gpu_spark.add_value(s.get("gpu_util", 0))
                mem_spark.add_value(s.get("gpu_mem_used", 0))

        ttk.Button(frame, text="Close", command=self.destroy).pack(
            anchor="e", pady=(4, 0))

        self.grab_set()
        self.focus_set()
