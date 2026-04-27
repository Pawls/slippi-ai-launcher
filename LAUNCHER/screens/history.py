"""Match History screen: browse past games and view stats."""

import os
import tkinter as tk
from tkinter import messagebox, ttk

from LAUNCHER.config import AppConfig, min_col_width
from LAUNCHER.match_store import MatchStore
from LAUNCHER.screens import Screen
from LAUNCHER.widgets import selectable_value


def _fmt_duration(seconds) -> str:
    """Format seconds as h:mm:ss or mm:ss."""
    if seconds is None:
        return "--"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _fmt_time(iso_str: str) -> str:
    """Format an ISO timestamp to a short display string."""
    if not iso_str:
        return "--"
    # "2026-04-01T14:30:22.123456" -> "Apr 01  14:30"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d  %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


def _fmt_stage(stage: str) -> str:
    """Clean up stage constant names for display."""
    if not stage:
        return "--"
    return stage.replace("_", " ").title()


def _fmt_result(result) -> str:
    if result == "win":
        return "W"
    if result == "loss":
        return "L"
    if result == "draw":
        return "D"
    return "-"


# ── Match detail / edit dialog ──────────────────────────────────────────────

class MatchDetailDialog(tk.Toplevel):
    """View and edit details of a single match."""

    def __init__(self, parent, match_store: MatchStore, record: dict,
                 on_update=None):
        super().__init__(parent)
        self.title("Match Details")
        self.transient(parent)
        self.resizable(False, False)
        self._store = match_store
        self._record = record
        self._on_update = on_update

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        # ── Info grid ─────────────────────────────────────────────────
        info = ttk.LabelFrame(frame, text="Match Info", padding=8)
        info.pack(fill="x", pady=(0, 8))

        fields = [
            ("Date", _fmt_time(record.get("timestamp_start", ""))),
            ("Mode", record.get("mode", "--")),
            ("Agent", os.path.basename(record.get("agent_path", "") or "")),
            ("Character", (record.get("ai_character") or "--").title()),
            ("Stage", _fmt_stage(record.get("stage", ""))),
            ("Duration", _fmt_duration(record.get("duration_seconds"))),
            ("Input Delay", str(record.get("input_delay", "--"))),
            ("Temperature", str(record.get("sample_temperature", "--"))),
        ]
        if record.get("connect_code"):
            fields.append(("Connect Code", record["connect_code"]))

        info.columnconfigure(1, weight=1)
        for i, (label, value) in enumerate(fields):
            ttk.Label(info, text=f"{label}:", anchor="e",
                      width=14).grid(row=i, column=0, sticky="e", padx=(0, 6))
            selectable_value(info, value).grid(
                row=i, column=1, sticky="ew")

        # ── Editable result ───────────────────────────────────────────
        edit = ttk.LabelFrame(frame, text="Result", padding=6)
        edit.pack(fill="x", pady=(0, 8))

        self._result_var = tk.StringVar(value=record.get("result") or "")
        for text, val in [("Win", "win"), ("Loss", "loss"),
                          ("Draw", "draw"), ("None", "")]:
            ttk.Radiobutton(edit, text=text, variable=self._result_var,
                            value=val).pack(side="left", padx=6)

        # ── Notes ─────────────────────────────────────────────────────
        ttk.Label(frame, text="Notes:").pack(anchor="w")
        self._notes = tk.Text(frame, height=3, width=44, wrap="word",
                              font=("TkDefaultFont", 9))
        self._notes.pack(fill="x", pady=(2, 8))
        existing_notes = record.get("user_notes") or ""
        if existing_notes:
            self._notes.insert("1.0", existing_notes)

        # ── Buttons ───────────────────────────────────────────────────
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Save",
                   command=self._on_save).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Delete Match",
                   command=self._on_delete).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side="right", padx=2)

        self.grab_set()
        self.focus_set()

    def _on_save(self):
        result = self._result_var.get() or None
        notes = self._notes.get("1.0", "end").strip()
        self._store.update_match(
            self._record["id"], result=result, user_notes=notes)
        if self._on_update:
            self._on_update()
        self.destroy()

    def _on_delete(self):
        if messagebox.askyesno(
                "Delete Match",
                "Remove this match from history?",
                parent=self):
            self._store.delete_match(self._record["id"])
            if self._on_update:
                self._on_update()
            self.destroy()


# ── History screen ──────────────────────────────────────────────────────────

class HistoryScreen(Screen):
    """Scrollable match history with stats summary."""

    def __init__(self, parent, navigator, cfg: AppConfig,
                 match_store: MatchStore):
        super().__init__(parent, navigator, cfg)
        self._store = match_store
        self._records: list[dict] = []
        self._add_back_button()

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Match History",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")

        # ── Stats summary bar ───────────────────────────────────────────
        self._stats_frame = ttk.Frame(outer)
        self._stats_frame.pack(fill="x", pady=(4, 8))

        self._stats_label = ttk.Label(
            self._stats_frame, text="", foreground="gray",
            font=("TkDefaultFont", 9))
        self._stats_label.pack(side="left")

        # ── Treeview ────────────────────────────────────────────────────
        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)

        columns = ("date", "mode", "agent", "character",
                   "stage", "duration", "result")
        self._tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse")

        self._tree.heading("date", text="Date")
        self._tree.heading("mode", text="Mode")
        self._tree.heading("agent", text="Agent")
        self._tree.heading("character", text="Character")
        self._tree.heading("stage", text="Stage")
        self._tree.heading("duration", text="Duration")
        self._tree.heading("result", text="Result")

        for col, hdr, w, anchor in [
            ("date", "Date", 120, "w"),
            ("mode", "Mode", 65, "center"),
            ("agent", "Agent", 160, "w"),
            ("character", "Character", 90, "w"),
            ("stage", "Stage", 120, "w"),
            ("duration", "Duration", 70, "center"),
            ("result", "Result", 50, "center"),
        ]:
            self._tree.column(col, width=max(w, min_col_width(hdr)),
                              anchor=anchor)

        # Tag-based row coloring for results
        self._tree.tag_configure("win", foreground="#2e7d32")
        self._tree.tag_configure("loss", foreground="#c62828")
        self._tree.tag_configure("draw", foreground="#f57f17")

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        # Double-click to edit
        self._tree.bind("<Double-1>", self._on_double_click)

        # ── Bottom bar ──────────────────────────────────────────────────
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))

        ttk.Button(bottom, text="Edit Selected",
                   command=self._edit_selected).pack(side="left", padx=2)
        ttk.Button(bottom, text="Delete Selected",
                   command=self._delete_selected).pack(side="left", padx=2)

        self._detail_label = ttk.Label(
            bottom, text="", foreground="gray",
            font=("TkDefaultFont", 8))
        self._detail_label.pack(side="right")

        # Show notes on selection
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_enter(self):
        self._refresh()

    # ── Data loading ─────────────────────────────────────────────────────

    def _refresh(self):
        self._records = self._store.get_all()
        self._populate_tree()
        self._update_stats()

    def _populate_tree(self):
        self._tree.delete(*self._tree.get_children())
        for rec in self._records:
            result = rec.get("result") or ""
            tag = result if result in ("win", "loss", "draw") else ""
            self._tree.insert("", "end", iid=rec["id"], values=(
                _fmt_time(rec.get("timestamp_start", "")),
                rec.get("mode", "--"),
                os.path.basename(rec.get("agent_path", "") or ""),
                (rec.get("ai_character") or "--").title(),
                _fmt_stage(rec.get("stage", "")),
                _fmt_duration(rec.get("duration_seconds")),
                _fmt_result(result),
            ), tags=(tag,) if tag else ())

    def _update_stats(self):
        stats = self._store.get_stats()
        total = stats["total_games"]
        if total == 0:
            self._stats_label.config(
                text="No matches recorded yet. Play a game to get started!")
            return

        hours = stats["total_seconds"] / 3600
        parts = [f"{total} games"]
        if hours >= 1:
            parts.append(f"{hours:.1f}h played")
        else:
            parts.append(f"{stats['total_seconds'] / 60:.0f}m played")

        if stats["rated_games"]:
            pct = stats["win_rate"] * 100
            parts.append(
                f"{stats['wins']}W {stats['losses']}L ({pct:.0f}%)")

        # Top agent
        if stats["by_agent"]:
            top_agent = next(iter(stats["by_agent"]))
            parts.append(f"Top: {top_agent}")

        self._stats_label.config(text="  |  ".join(parts))

    # ── Selection & editing ──────────────────────────────────────────────

    def _get_selected_record(self) -> dict | None:
        sel = self._tree.selection()
        if not sel:
            return None
        match_id = sel[0]
        for rec in self._records:
            if rec.get("id") == match_id:
                return rec
        return None

    def _on_select(self, _event=None):
        rec = self._get_selected_record()
        if rec and rec.get("user_notes"):
            self._detail_label.config(
                text=f"Notes: {rec['user_notes'][:80]}")
        else:
            self._detail_label.config(text="")

    def _on_double_click(self, _event):
        self._edit_selected()

    def _edit_selected(self):
        rec = self._get_selected_record()
        if not rec:
            return
        MatchDetailDialog(
            self.winfo_toplevel(), self._store, rec,
            on_update=self._refresh)

    def _delete_selected(self):
        rec = self._get_selected_record()
        if not rec:
            return
        if messagebox.askyesno(
                "Delete Match",
                "Remove this match from history?",
                parent=self):
            self._store.delete_match(rec["id"])
            self._refresh()
