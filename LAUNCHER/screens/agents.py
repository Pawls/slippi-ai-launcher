"""Agent Library screen: browse, edit, and compare trained agents."""

import os
import tkinter as tk
from tkinter import messagebox, ttk

from LAUNCHER.agent_store import AgentStore
from LAUNCHER.config import AppConfig, CHARACTERS, min_col_width
from LAUNCHER.match_store import MatchStore
from LAUNCHER.screens import Screen
from LAUNCHER.widgets import selectable_value

_ALL_CHAR_COUNT = len(CHARACTERS)


def _fmt_chars(chars: list) -> str:
    if not chars:
        return "--"
    if len(chars) >= _ALL_CHAR_COUNT:
        return "All"
    if len(chars) > 3:
        return ", ".join(c.title() for c in chars[:3]) + f" +{len(chars) - 3}"
    return ", ".join(c.title() for c in chars)


def _full_chars(chars: list) -> str:
    """Full character list for tooltips."""
    if not chars:
        return ""
    if len(chars) >= _ALL_CHAR_COUNT:
        return "All characters"
    return ", ".join(c.title() for c in chars)


def _fmt_delay(delay) -> str:
    if delay is None:
        return "--"
    return str(delay)


def _fmt_winrate(stats: dict) -> str:
    total = stats.get("total", 0)
    if total == 0:
        return "--"
    pct = stats["win_rate"] * 100
    return f"{stats['wins']}W {stats['losses']}L ({pct:.0f}%)"


def _fmt_size(mb) -> str:
    if not mb:
        return "--"
    return f"{mb:.1f} MB"


# ── Treeview tooltip ───────────────────────────────────────────────────────

class _TreeTooltip:
    """Hover tooltip for Treeview cells. Only redraws when the cell changes."""

    def __init__(self, tree: ttk.Treeview, get_text):
        self._tree = tree
        self._get_text = get_text
        self._tip: tk.Toplevel | None = None
        self._current_cell: tuple[str, str] = ("", "")
        tree.bind("<Motion>", self._on_motion)
        tree.bind("<Leave>", self._hide)

    def _on_motion(self, event):
        row = self._tree.identify_row(event.y)
        col = self._tree.identify_column(event.x)
        cell = (row, col)
        if cell == self._current_cell:
            return  # same cell, keep existing tooltip
        if not row or not col:
            self._hide()
            return
        text = self._get_text(row, col)
        if not text:
            self._hide()
            return
        self._show(event, text, cell)

    def _show(self, event, text: str, cell: tuple[str, str]):
        self._hide()
        self._current_cell = cell
        tip = tk.Toplevel(self._tree)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        lbl = tk.Label(tip, text=text, background="#ffffe0", relief="solid",
                       borderwidth=1, font=("TkDefaultFont", 9),
                       wraplength=350, justify="left")
        lbl.pack()
        self._tip = tip

    def _hide(self, _event=None):
        self._current_cell = ("", "")
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ── Agent detail / edit dialog ─────────────────────────────────────────────

class AgentDetailDialog(tk.Toplevel):
    """View and edit details of a single agent."""

    def __init__(self, parent, agent_store: AgentStore, match_store: MatchStore,
                 record: dict, agents_dir: str, on_update=None):
        super().__init__(parent)
        self.title("Agent Details")
        self.transient(parent)
        self.resizable(False, False)
        self._store = agent_store
        self._match_store = match_store
        self._record = record
        self._agents_dir = agents_dir
        self._on_update = on_update

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        # ── Info grid ─────────────────────────────────────────────────
        info = ttk.LabelFrame(frame, text="Agent Info", padding=8)
        info.pack(fill="x", pady=(0, 8))

        stats = agent_store.get_stats_for_agent(
            record["agent_path"], match_store)

        fields = [
            ("File", record.get("agent_path", "")),
            ("Characters", _full_chars(record.get("characters", [])) or "--"),
            ("Delay", _fmt_delay(record.get("delay"))),
            ("Training", record.get("training_type") or "--"),
            ("Size", _fmt_size(record.get("file_size_mb"))),
            ("Record", _fmt_winrate(stats)),
            ("Games", str(stats.get("total", 0))),
            ("Added", record.get("date_added", "")[:10]),
        ]
        if record.get("missing"):
            fields.append(("Status", "FILE MISSING"))

        info.columnconfigure(1, weight=1)
        for i, (label, value) in enumerate(fields):
            ttk.Label(info, text=f"{label}:", anchor="e",
                      width=12).grid(row=i, column=0, sticky="e", padx=(0, 6))
            sv = selectable_value(info, value)
            sv.grid(row=i, column=1, sticky="ew")

        # ── Editable nickname ─────────────────────────────────────────
        edit = ttk.LabelFrame(frame, text="Nickname", padding=6)
        edit.pack(fill="x", pady=(0, 8))

        self._nick_var = tk.StringVar(value=record.get("nickname") or "")
        ttk.Entry(edit, textvariable=self._nick_var, width=40).pack(
            fill="x", padx=4)

        # ── Training type ─────────────────────────────────────────────
        type_frame = ttk.LabelFrame(frame, text="Training Type", padding=6)
        type_frame.pack(fill="x", pady=(0, 8))

        self._type_var = tk.StringVar(
            value=record.get("training_type") or "")
        for text, val in [("IL", "IL"), ("RL", "RL"), ("Unknown", "")]:
            ttk.Radiobutton(type_frame, text=text, variable=self._type_var,
                            value=val).pack(side="left", padx=6)

        # ── Notes ─────────────────────────────────────────────────────
        ttk.Label(frame, text="Notes:").pack(anchor="w")
        self._notes = tk.Text(frame, height=4, width=44, wrap="word",
                              font=("TkDefaultFont", 9))
        self._notes.pack(fill="x", pady=(2, 8))
        existing = record.get("notes") or ""
        if existing:
            self._notes.insert("1.0", existing)

        # ── Buttons ───────────────────────────────────────────────────
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Save",
                   command=self._on_save).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Delete File",
                   command=self._on_delete_file).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Remove Entry",
                   command=self._on_remove).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side="right", padx=2)

        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _on_save(self):
        nickname = self._nick_var.get().strip()
        notes = self._notes.get("1.0", "end").strip()
        training_type = self._type_var.get()
        self._store.update_agent(
            self._record["id"],
            nickname=nickname, notes=notes, training_type=training_type)
        if self._on_update:
            self._on_update()
        self.destroy()

    def _on_remove(self):
        if messagebox.askyesno(
                "Remove Entry",
                "Remove this agent from the library?\n"
                "(The file will NOT be deleted.)",
                parent=self):
            self._store.delete_agent(self._record["id"])
            if self._on_update:
                self._on_update()
            self.destroy()

    def _on_delete_file(self):
        if messagebox.askyesno(
                "Delete Agent File",
                f"Permanently delete this agent file?\n\n"
                f"{self._record['agent_path']}\n\n"
                "This cannot be undone.",
                parent=self):
            self._store.delete_file_and_record(
                self._record["id"], self._agents_dir)
            if self._on_update:
                self._on_update()
            self.destroy()


# ── Compare dialog ─────────────────────────────────────────────────────────

class CompareDialog(tk.Toplevel):
    """Side-by-side comparison of two agents."""

    def __init__(self, parent, match_store: MatchStore,
                 rec_a: dict, rec_b: dict):
        super().__init__(parent)
        self.title("Compare Agents")
        self.transient(parent)
        self.resizable(True, False)
        self.minsize(500, 300)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        stats_a = AgentStore.get_stats_for_agent(
            rec_a["agent_path"], match_store)
        stats_b = AgentStore.get_stats_for_agent(
            rec_b["agent_path"], match_store)

        name_a = rec_a.get("nickname") or os.path.basename(
            rec_a["agent_path"])
        name_b = rec_b.get("nickname") or os.path.basename(
            rec_b["agent_path"])

        # Header row
        for col, text in enumerate(["", name_a, name_b]):
            weight = "bold" if col > 0 else "normal"
            ttk.Label(frame, text=text, font=("TkDefaultFont", 10, weight),
                      anchor="center").grid(
                row=0, column=col, padx=8, pady=(0, 8), sticky="ew")

        rows = [
            ("File", rec_a["agent_path"], rec_b["agent_path"]),
            ("Characters", _fmt_chars(rec_a.get("characters", [])),
             _fmt_chars(rec_b.get("characters", []))),
            ("Delay", _fmt_delay(rec_a.get("delay")),
             _fmt_delay(rec_b.get("delay"))),
            ("Training", rec_a.get("training_type") or "--",
             rec_b.get("training_type") or "--"),
            ("Size", _fmt_size(rec_a.get("file_size_mb")),
             _fmt_size(rec_b.get("file_size_mb"))),
            ("Record", _fmt_winrate(stats_a), _fmt_winrate(stats_b)),
            ("Games", str(stats_a.get("total", 0)),
             str(stats_b.get("total", 0))),
        ]

        for i, (label, val_a, val_b) in enumerate(rows, start=1):
            ttk.Label(frame, text=f"{label}:", anchor="e",
                      width=12).grid(row=i, column=0, sticky="e", padx=(0, 8))
            ttk.Label(frame, text=val_a, anchor="w", wraplength=200).grid(
                row=i, column=1, sticky="w", padx=8)
            ttk.Label(frame, text=val_b, anchor="w", wraplength=200).grid(
                row=i, column=2, sticky="w", padx=8)

        # Notes comparison
        notes_row = len(rows) + 1
        ttk.Label(frame, text="Notes:", anchor="e",
                  width=12).grid(row=notes_row, column=0, sticky="ne",
                                 padx=(0, 8), pady=(8, 0))
        for col, rec in enumerate([rec_a, rec_b], start=1):
            notes = rec.get("notes") or "(none)"
            lbl = ttk.Label(frame, text=notes, anchor="nw",
                            wraplength=200, foreground="gray")
            lbl.grid(row=notes_row, column=col, sticky="nw",
                     padx=8, pady=(8, 0))

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

        ttk.Button(frame, text="Close", command=self.destroy).grid(
            row=notes_row + 1, column=0, columnspan=3, pady=(16, 0))

        self.wait_visibility()
        self.grab_set()
        self.focus_set()


# ── Agent Library screen ───────────────────────────────────────────────────

class AgentLibraryScreen(Screen):
    """Browse, edit, and compare trained agents."""

    def __init__(self, parent, navigator, cfg: AppConfig,
                 agent_store: AgentStore, experiment_store: AgentStore,
                 match_store: MatchStore):
        super().__init__(parent, navigator, cfg)
        self._agent_store = agent_store
        self._experiment_store = experiment_store
        self._match_store = match_store
        self._records: list[dict] = []
        self._add_back_button()

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # ── Header ──────────────────────────────────────────────────────
        header = ttk.Frame(outer)
        header.pack(fill="x")

        ttk.Label(header, text="Agent Library",
                  font=("TkDefaultFont", 14, "bold")).pack(
            side="left", anchor="w")

        ttk.Button(header, text="Rescan", command=self._rescan).pack(
            side="right", padx=4)

        # ── Stats summary bar ───────────────────────────────────────────
        self._stats_label = ttk.Label(
            outer, text="", foreground="gray",
            font=("TkDefaultFont", 9))
        self._stats_label.pack(anchor="w", pady=(4, 8))

        # ── Filter bar ──────────────────────────────────────────────────
        filter_frame = ttk.Frame(outer)
        filter_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(filter_frame, text="Filter:").pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(filter_frame, textvariable=self._filter_var,
                  width=30).pack(side="left", padx=(4, 8))

        ttk.Label(filter_frame, text="Type:").pack(side="left")
        self._type_filter = tk.StringVar(value="All")
        type_combo = ttk.Combobox(
            filter_frame, textvariable=self._type_filter,
            values=["All", "IL", "RL"], state="readonly", width=6)
        type_combo.pack(side="left", padx=4)
        type_combo.bind("<<ComboboxSelected>>",
                        lambda _: self._apply_filter())

        self._incl_experiments = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_frame, text="Include experiments",
            variable=self._incl_experiments,
            command=self._rescan).pack(side="left", padx=(12, 0))

        # ── Treeview ────────────────────────────────────────────────────
        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "characters", "delay", "type",
                   "record", "size", "notes")
        self._tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings",
            selectmode="extended")

        self._tree.heading("name", text="Agent",
                           command=lambda: self._sort_by("name"))
        self._tree.heading("characters", text="Characters",
                           command=lambda: self._sort_by("characters"))
        self._tree.heading("delay", text="Delay",
                           command=lambda: self._sort_by("delay"))
        self._tree.heading("type", text="Type",
                           command=lambda: self._sort_by("type"))
        self._tree.heading("record", text="Record",
                           command=lambda: self._sort_by("record"))
        self._tree.heading("size", text="Size",
                           command=lambda: self._sort_by("size"))
        self._tree.heading("notes", text="Notes")

        for col, hdr, w, anchor in [
            ("name", "Agent", 200, "w"),
            ("characters", "Characters", 140, "w"),
            ("delay", "Delay", 50, "center"),
            ("type", "Type", 50, "center"),
            ("record", "Record", 120, "center"),
            ("size", "Size", 70, "e"),
            ("notes", "Notes", 160, "w"),
        ]:
            self._tree.column(col, width=max(w, min_col_width(hdr)),
                              anchor=anchor)

        self._tree.tag_configure("missing", foreground="gray")

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)

        # Tooltip for truncated columns (characters, notes, file path)
        self._row_records: dict[str, dict] = {}
        _TreeTooltip(self._tree, self._tooltip_text)

        # ── Bottom bar ──────────────────────────────────────────────────
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))

        ttk.Button(bottom, text="Edit Selected",
                   command=self._edit_selected).pack(side="left", padx=2)
        ttk.Button(bottom, text="Compare (2)",
                   command=self._compare_selected).pack(side="left", padx=2)
        ttk.Button(bottom, text="Delete Selected",
                   command=self._delete_selected).pack(side="left", padx=2)

        self._detail_label = ttk.Label(
            bottom, text="", foreground="gray",
            font=("TkDefaultFont", 8))
        self._detail_label.pack(side="right")

        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # Sort state
        self._sort_col = "name"
        self._sort_reverse = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _experiments_dir(self) -> str:
        root = self.cfg.get("paths", "slippi_ai_root")
        if not root:
            return ""
        exp = os.path.join(root, "experiments")
        return exp if os.path.isdir(exp) else ""

    def on_enter(self):
        self._agent_store.sync(self.cfg.get("paths", "agents_dir"))
        if self._incl_experiments.get():
            self._experiment_store.sync(self._experiments_dir())
        self._refresh()

    # ── Data loading ─────────────────────────────────────────────────────

    def _rescan(self):
        self._agent_store.sync(self.cfg.get("paths", "agents_dir"))
        if self._incl_experiments.get():
            self._experiment_store.sync(self._experiments_dir())
        self._refresh()

    def _store_for(self, rec: dict) -> AgentStore:
        """Return the store that owns a record."""
        if rec.get("source") == "experiments":
            return self._experiment_store
        return self._agent_store

    def _refresh(self):
        self._records = self._agent_store.get_all()
        if self._incl_experiments.get():
            self._records.extend(self._experiment_store.get_all())
        self._apply_filter()
        self._update_stats()

    def _apply_filter(self):
        text = self._filter_var.get().lower().strip()
        type_filter = self._type_filter.get()

        filtered = []
        for rec in self._records:
            # Text filter: match against name, nickname, characters, notes
            if text:
                searchable = " ".join([
                    rec.get("agent_path", ""),
                    rec.get("nickname", ""),
                    " ".join(rec.get("characters", [])),
                    rec.get("notes", ""),
                ]).lower()
                if text not in searchable:
                    continue
            # Type filter
            if type_filter != "All":
                if rec.get("training_type") != type_filter:
                    continue
            filtered.append(rec)

        self._populate_tree(filtered)

    def _populate_tree(self, records: list[dict]):
        self._tree.delete(*self._tree.get_children())
        self._row_records.clear()
        for rec in records:
            stats = AgentStore.get_stats_for_agent(
                rec["agent_path"], self._match_store)
            display_name = rec.get("nickname") or os.path.basename(
                rec["agent_path"])
            tag = "missing" if rec.get("missing") else ""
            notes_preview = (rec.get("notes") or "")[:50]

            self._tree.insert("", "end", iid=rec["id"], values=(
                display_name,
                _fmt_chars(rec.get("characters", [])),
                _fmt_delay(rec.get("delay")),
                rec.get("training_type") or "--",
                _fmt_winrate(stats),
                _fmt_size(rec.get("file_size_mb")),
                notes_preview,
            ), tags=(tag,) if tag else ())
            self._row_records[rec["id"]] = rec

    def _tooltip_text(self, row_id: str, col: str) -> str:
        """Return tooltip text for a cell, or empty to suppress."""
        rec = self._row_records.get(row_id)
        if not rec:
            return ""
        # col is "#1", "#2", etc.
        col_map = {"#2": "characters", "#7": "notes"}
        field = col_map.get(col, "")
        if field == "characters":
            full = _full_chars(rec.get("characters", []))
            # Only show tooltip if truncated
            short = _fmt_chars(rec.get("characters", []))
            if full and full != short:
                return full
        if field == "notes":
            notes = rec.get("notes", "")
            if len(notes) > 50:
                return notes
        return ""

    def _update_stats(self):
        total = len(self._records)
        missing = sum(1 for r in self._records if r.get("missing"))
        il = sum(1 for r in self._records
                 if r.get("training_type") == "IL")
        rl = sum(1 for r in self._records
                 if r.get("training_type") == "RL")

        parts = [f"{total} agents"]
        if il:
            parts.append(f"{il} IL")
        if rl:
            parts.append(f"{rl} RL")
        if missing:
            parts.append(f"{missing} missing")

        self._stats_label.config(
            text="  |  ".join(parts) if total else
            "No agents found. Check your agents directory in Settings.")

    # ── Sorting ──────────────────────────────────────────────────────────

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False

        def sort_key(rec):
            if col == "name":
                return (rec.get("nickname") or
                        os.path.basename(rec["agent_path"])).lower()
            if col == "characters":
                return _fmt_chars(rec.get("characters", []))
            if col == "delay":
                d = rec.get("delay")
                return d if d is not None else 999
            if col == "type":
                return rec.get("training_type") or "zzz"
            if col == "record":
                stats = AgentStore.get_stats_for_agent(
                    rec["agent_path"], self._match_store)
                return stats.get("total", 0)
            if col == "size":
                return rec.get("file_size_mb") or 0
            return ""

        self._records.sort(key=sort_key, reverse=self._sort_reverse)
        self._apply_filter()

    # ── Selection & editing ──────────────────────────────────────────────

    def _get_selected_records(self) -> list[dict]:
        sel = self._tree.selection()
        if not sel:
            return []
        by_id = {r["id"]: r for r in self._records}
        return [by_id[s] for s in sel if s in by_id]

    def _on_select(self, _event=None):
        recs = self._get_selected_records()
        if len(recs) == 1 and recs[0].get("notes"):
            self._detail_label.config(
                text=f"Notes: {recs[0]['notes'][:80]}")
        elif len(recs) == 2:
            self._detail_label.config(text="2 agents selected - Compare")
        else:
            self._detail_label.config(text="")

    def _on_double_click(self, _event):
        self._edit_selected()

    def _base_dir_for(self, rec: dict) -> str:
        if rec.get("source") == "experiments":
            return self._experiments_dir()
        return self.cfg.get("paths", "agents_dir")

    def _edit_selected(self):
        recs = self._get_selected_records()
        if len(recs) != 1:
            return
        rec = recs[0]
        AgentDetailDialog(
            self.winfo_toplevel(), self._store_for(rec), self._match_store,
            rec, self._base_dir_for(rec), on_update=self._refresh)

    def _compare_selected(self):
        recs = self._get_selected_records()
        if len(recs) != 2:
            messagebox.showinfo(
                "Compare",
                "Select exactly 2 agents to compare.\n\n"
                "Hold Ctrl and click to select multiple.",
                parent=self)
            return
        CompareDialog(
            self.winfo_toplevel(), self._match_store,
            recs[0], recs[1])

    def _delete_selected(self):
        recs = self._get_selected_records()
        if not recs:
            return
        names = "\n".join(
            rec.get("nickname") or os.path.basename(rec["agent_path"])
            for rec in recs)
        if messagebox.askyesno(
                "Remove from Library",
                f"Remove {len(recs)} agent(s) from the library?\n\n"
                f"{names}\n\n"
                "(Files will NOT be deleted.)",
                parent=self):
            for rec in recs:
                self._store_for(rec).delete_agent(rec["id"])
            self._refresh()
