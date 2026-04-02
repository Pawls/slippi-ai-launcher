"""Replay Browser screen: browse, filter, and watch .slp replay files."""

import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from LAUNCHER.config import AppConfig
from LAUNCHER.replay_store import ReplayStore, normalize_fullwidth
from LAUNCHER.screens import Screen


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "--"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _fmt_time(iso_str: str) -> str:
    if not iso_str:
        return "--"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%d  %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


def _player_label(p: dict, mode: str) -> str:
    """Format a single player for display.

    mode: "code" (default) shows connect code, "name" shows netplay name.
    Falls back through code -> name -> tag if the preferred field is empty.
    """
    if mode == "name":
        label = p.get("name") or p.get("connect_code") or p.get("name_tag")
    else:
        label = p.get("connect_code") or p.get("name") or p.get("name_tag")
    return label or ""


def _players_summary(players: list[dict], mode: str = "code") -> str:
    """Format player list as 'Char (Label) vs Char (Label)'."""
    parts = []
    for p in players:
        char = p.get("character", "?")
        label = _player_label(p, mode)
        if label:
            parts.append(f"{char} ({label})")
        else:
            parts.append(char)
    return " vs ".join(parts)


def _matchup(players: list[dict]) -> str:
    """Short matchup string like 'Fox vs Marth'."""
    return " vs ".join(p.get("character", "?") for p in players)


def _fmt_end_type(r: dict) -> str:
    """Format end type for display, including rage quit detection."""
    end_type = r.get("end_type") or r.get("end_method") or "--"

    if end_type.startswith("rage_quit_p"):
        port = end_type[-1]
        return f"Rage Quit (P{port})"
    if end_type.startswith("lras_p"):
        port = end_type[-1]
        return f"LRAS (P{port})"
    if end_type == "no_contest":
        return "No Contest"

    return end_type.replace("_", " ").title()


def _search_haystack(r: dict) -> str:
    """Build a single lowercase string from all searchable fields."""
    parts = [
        r.get("filename", ""),
        r.get("stage", ""),
        r.get("console_nick", ""),
        r.get("played_on", ""),
    ]
    for p in r.get("players", []):
        parts.append(p.get("character", ""))
        parts.append(p.get("name", ""))
        parts.append(p.get("connect_code", ""))
        parts.append(p.get("name_tag", ""))
    return " ".join(parts).lower()


def _normalize_search(query: str) -> list[str]:
    """Split search query into individual terms, stripping parens and brackets."""
    # Strip common grouping chars so "Ganon(PAWL#723)" becomes "Ganon PAWL#723"
    cleaned = query
    for ch in "()[]{}":
        cleaned = cleaned.replace(ch, " ")
    return [t for t in cleaned.lower().split() if t]


# Optional columns the user can toggle
# (key, heading_text, default_visible, width, anchor)
OPTIONAL_COLUMNS = [
    ("console",  "Console",  False, 100, "w"),
    ("platform", "Platform", False,  80, "center"),
    ("slippi_v", "Slippi",   False,  60, "center"),
]

# Core columns always shown
CORE_COLUMNS = [
    ("date",     "Date",     140, "w"),
    ("matchup",  "Matchup",  160, "w"),
    ("stage",    "Stage",    140, "w"),
    ("duration", "Duration",  70, "center"),
    ("players",  "Players",  220, "w"),
    ("end",      "End",      100, "center"),
]


# ── Column settings dialog ──────────────────────────────────────────────────

class ColumnSettingsDialog(tk.Toplevel):
    """Let the user toggle optional columns."""

    def __init__(self, parent, current: set[str], on_apply=None):
        super().__init__(parent)
        self.title("Column Settings")
        self.transient(parent)
        self.resizable(False, False)
        self._on_apply = on_apply

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Optional Columns",
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 8))

        self._vars: dict[str, tk.BooleanVar] = {}
        for key, label, _default, _w, _a in OPTIONAL_COLUMNS:
            var = tk.BooleanVar(value=key in current)
            self._vars[key] = var
            ttk.Checkbutton(frame, text=label, variable=var).pack(
                anchor="w", pady=2)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(12, 0))
        ttk.Button(btn_frame, text="Apply",
                   command=self._apply).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side="right", padx=2)

        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _apply(self):
        enabled = {k for k, v in self._vars.items() if v.get()}
        if self._on_apply:
            self._on_apply(enabled)
        self.destroy()


# ── Replay detail dialog ────────────────────────────────────────────────────

class ReplayDetailDialog(tk.Toplevel):
    """Show full details of a single replay."""

    def __init__(self, parent, replay: dict, launch_cb=None):
        super().__init__(parent)
        self.title("Replay Details")
        self.transient(parent)
        self.resizable(False, False)
        self._replay = replay
        self._launch_cb = launch_cb

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        # Info grid
        info = ttk.LabelFrame(frame, text="Replay Info", padding=8)
        info.pack(fill="x", pady=(0, 8))

        fields = [
            ("File", replay.get("filename", "--")),
            ("Date", _fmt_time(replay.get("played_at", ""))),
            ("Stage", replay.get("stage", "--")),
            ("Duration", _fmt_duration(replay.get("duration_seconds"))),
            ("End", _fmt_end_type(replay)),
            ("Teams", "Yes" if replay.get("is_teams") else "No"),
        ]
        if replay.get("console_nick"):
            fields.append(("Console", replay["console_nick"]))
        if replay.get("played_on"):
            fields.append(("Platform", replay["played_on"].title()))
        if replay.get("slippi_version"):
            fields.append(("Slippi Version", replay["slippi_version"]))

        for i, (label, value) in enumerate(fields):
            ttk.Label(info, text=f"{label}:", anchor="e",
                      width=14).grid(row=i, column=0, sticky="e", padx=(0, 6))
            ttk.Label(info, text=value, anchor="w").grid(
                row=i, column=1, sticky="w")

        # Players
        players_frame = ttk.LabelFrame(frame, text="Players", padding=8)
        players_frame.pack(fill="x", pady=(0, 8))

        for p in replay.get("players", []):
            port = p.get("port", "?")
            char = p.get("character", "?")
            ptype = p.get("type", "?").title()
            code = p.get("connect_code") or ""
            name = p.get("name") or ""
            tag = p.get("name_tag") or ""
            placement = replay.get("placements", {}).get(port)
            place_str = f"  #{placement + 1}" if placement is not None else ""

            end_stocks = p.get("end_stocks")
            end_pct = p.get("end_percent")
            stock_str = ""
            if end_stocks is not None:
                stock_str = f"  ({end_stocks} stocks"
                if end_pct is not None:
                    stock_str += f", {end_pct:.0f}%"
                stock_str += ")"

            port_label = port + 1 if isinstance(port, int) else port
            parts = [f"P{port_label}: {char}"]
            if code:
                parts.append(f"[{code}]")
            if name:
                parts.append(f"({name})")
            if tag:
                parts.append(f'"{tag}"')
            parts.append(f"- {ptype}{place_str}{stock_str}")
            ttk.Label(players_frame, text=" ".join(parts)).pack(anchor="w")

        # Path
        ttk.Label(frame, text="Path:", foreground="gray",
                  font=("TkDefaultFont", 8)).pack(anchor="w")
        path_var = tk.StringVar(value=replay.get("path", ""))
        path_entry = ttk.Entry(frame, textvariable=path_var, state="readonly",
                               width=60, font=("TkDefaultFont", 8))
        path_entry.pack(fill="x", pady=(0, 8))

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        if self._launch_cb:
            ttk.Button(btn_frame, text="Watch Replay",
                       command=self._on_watch).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Open Folder",
                   command=self._open_folder).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Close",
                   command=self.destroy).pack(side="right", padx=2)

        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _on_watch(self):
        if self._launch_cb:
            self._launch_cb(self._replay)
        self.destroy()

    def _open_folder(self):
        path = self._replay.get("path", "")
        if not path:
            return
        folder = os.path.dirname(path)
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            pass


# ── Replay Browser screen ───────────────────────────────────────────────────

class ReplayBrowserScreen(Screen):
    """Browse and watch Slippi replay files."""

    def __init__(self, parent, navigator, cfg: AppConfig,
                 replay_store: ReplayStore):
        super().__init__(parent, navigator, cfg)
        self._store = replay_store
        self._replays: list[dict] = []
        self._filtered: list[dict] = []
        self._scanning = False
        self._add_back_button()

        # Load prefs
        saved = cfg.get("app", "replay_columns")
        if saved:
            self._opt_cols: set[str] = set(saved.split(",")) if saved else set()
        else:
            self._opt_cols = {k for k, _, default, _, _ in OPTIONAL_COLUMNS if default}

        # Player display mode: "code" or "name"
        self._player_mode = cfg.get("app", "replay_player_mode") or "code"

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))

        ttk.Label(header, text="Replay Browser",
                  font=("TkDefaultFont", 14, "bold")).pack(side="left")

        self._status_label = ttk.Label(
            header, text="", foreground="gray",
            font=("TkDefaultFont", 9))
        self._status_label.pack(side="right")

        # Directory selector + scan button
        dir_frame = ttk.Frame(outer)
        dir_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(dir_frame, text="Replays folder:").pack(side="left")
        self._dir_var = tk.StringVar(
            value=cfg.get("paths", "replays_dir"))
        self._dir_entry = ttk.Entry(dir_frame, textvariable=self._dir_var,
                                    width=48)
        self._dir_entry.pack(side="left", padx=(6, 4), fill="x", expand=True)
        ttk.Button(dir_frame, text="Browse...",
                   command=self._browse_dir).pack(side="left", padx=(0, 4))
        self._scan_btn = ttk.Button(dir_frame, text="Scan",
                                    command=self._scan)
        self._scan_btn.pack(side="left")

        # Filter bar
        filter_frame = ttk.LabelFrame(outer, text="Filters", padding=6)
        filter_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(filter_frame, text="Search:").grid(
            row=0, column=0, sticky="w", padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filters())
        ttk.Entry(filter_frame, textvariable=self._search_var,
                  width=24).grid(row=0, column=1, padx=(0, 12))

        ttk.Label(filter_frame, text="Character:").grid(
            row=0, column=2, sticky="w", padx=(0, 4))
        self._char_var = tk.StringVar(value="All")
        self._char_combo = ttk.Combobox(
            filter_frame, textvariable=self._char_var, state="readonly",
            width=16, values=["All"])
        self._char_combo.grid(row=0, column=3, padx=(0, 12))
        self._char_combo.bind("<<ComboboxSelected>>",
                              lambda _: self._apply_filters())

        ttk.Label(filter_frame, text="Stage:").grid(
            row=0, column=4, sticky="w", padx=(0, 4))
        self._stage_var = tk.StringVar(value="All")
        self._stage_combo = ttk.Combobox(
            filter_frame, textvariable=self._stage_var, state="readonly",
            width=18, values=["All"])
        self._stage_combo.grid(row=0, column=5, padx=(0, 12))
        self._stage_combo.bind("<<ComboboxSelected>>",
                               lambda _: self._apply_filters())

        ttk.Button(filter_frame, text="Clear",
                   command=self._clear_filters).grid(row=0, column=6)

        # Treeview container
        self._tree_frame = ttk.Frame(outer)
        self._tree_frame.pack(fill="both", expand=True)

        self._tree: ttk.Treeview | None = None
        self._build_tree()

        # Bottom bar
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))

        ttk.Button(bottom, text="Watch Selected",
                   command=self._watch_selected).pack(side="left", padx=2)
        ttk.Button(bottom, text="Details",
                   command=self._show_details).pack(side="left", padx=2)
        ttk.Button(bottom, text="Open Folder",
                   command=self._open_selected_folder).pack(side="left", padx=2)

        sep = ttk.Separator(bottom, orient="vertical")
        sep.pack(side="left", fill="y", padx=8, pady=2)

        ttk.Label(bottom, text="Show players by:").pack(side="left")
        self._mode_var = tk.StringVar(value=self._player_mode)
        ttk.Radiobutton(bottom, text="Code", variable=self._mode_var,
                        value="code",
                        command=self._on_mode_change).pack(side="left", padx=2)
        ttk.Radiobutton(bottom, text="Name", variable=self._mode_var,
                        value="name",
                        command=self._on_mode_change).pack(side="left", padx=2)

        ttk.Button(bottom, text="Columns...",
                   command=self._open_column_settings).pack(side="left", padx=(8, 2))

        self._count_label = ttk.Label(
            bottom, text="", foreground="gray",
            font=("TkDefaultFont", 9))
        self._count_label.pack(side="right")

        # Sort state
        self._sort_col = "date"
        self._sort_reverse = True

        # Progress bar (hidden initially)
        self._progress = ttk.Progressbar(outer, mode="determinate")

    # ── Tree building (rebuilt when columns change) ──────────────────────

    def _build_tree(self):
        for w in self._tree_frame.winfo_children():
            w.destroy()

        self._col_defs = list(CORE_COLUMNS)
        for key, label, _default, width, anchor in OPTIONAL_COLUMNS:
            if key in self._opt_cols:
                self._col_defs.append((key, label, width, anchor))

        col_keys = [c[0] for c in self._col_defs]
        self._tree = ttk.Treeview(
            self._tree_frame, columns=col_keys, show="headings",
            selectmode="browse")

        for key, label, width, anchor in self._col_defs:
            self._tree.heading(key, text=label,
                               command=lambda k=key: self._sort_by(k))
            self._tree.column(key, width=width, anchor=anchor)

        # Color-code rage quit rows
        self._tree.tag_configure("rage_quit", foreground="#c62828")
        self._tree.tag_configure("lras", foreground="#f57f17")

        scrollbar = ttk.Scrollbar(
            self._tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_enter(self):
        current_dir = self.cfg.get("paths", "replays_dir")
        if current_dir and not self._dir_var.get():
            self._dir_var.set(current_dir)

        cached = self._store.get_cached()
        if cached:
            self._replays = cached
            self._rebuild_filter_options()
            self._apply_filters()
        elif self._dir_var.get():
            self._scan()

    # ── Player display mode ──────────────────────────────────────────────

    def _on_mode_change(self):
        self._player_mode = self._mode_var.get()
        self.cfg.set("app", "replay_player_mode", self._player_mode)
        self.cfg.save()
        self._populate_tree()

    # ── Directory ────────────────────────────────────────────────────────

    def _browse_dir(self):
        d = filedialog.askdirectory()
        if d:
            self._dir_var.set(d)
            self.cfg.set("paths", "replays_dir", d)
            self.cfg.save()

    # ── Scanning ─────────────────────────────────────────────────────────

    def _scan(self):
        if self._scanning:
            return
        replays_dir = self._dir_var.get().strip()
        if not replays_dir or not os.path.isdir(replays_dir):
            messagebox.showwarning(
                "No Replays Directory",
                "Please set a valid replays directory.")
            return

        self._scanning = True
        self._scan_btn.config(state="disabled")
        self._status_label.config(text="Scanning...")
        self._progress.pack(fill="x", pady=(4, 0))
        self._progress["value"] = 0

        def do_scan():
            def progress_cb(current, total):
                if total > 0:
                    self.after(0, lambda: self._update_progress(current, total))

            results = self._store.scan(replays_dir, progress_cb=progress_cb)
            self.after(0, lambda: self._on_scan_done(results))

        threading.Thread(target=do_scan, daemon=True).start()

    def _update_progress(self, current, total):
        if total > 0:
            self._progress["maximum"] = total
            self._progress["value"] = current
            self._status_label.config(text=f"Scanning... {current}/{total}")

    def _on_scan_done(self, results: list[dict]):
        self._scanning = False
        self._scan_btn.config(state="normal")
        self._progress.pack_forget()
        self._replays = results
        self._rebuild_filter_options()
        self._apply_filters()
        self._status_label.config(text=f"{len(results)} replays found")

    # ── Filtering ────────────────────────────────────────────────────────

    def _rebuild_filter_options(self):
        chars = set()
        stages = set()
        for r in self._replays:
            for p in r.get("players", []):
                chars.add(p.get("character", ""))
            stages.add(r.get("stage", ""))

        chars.discard("")
        stages.discard("")

        self._char_combo["values"] = ["All"] + sorted(chars)
        self._stage_combo["values"] = ["All"] + sorted(stages)

    def _apply_filters(self):
        terms = _normalize_search(self._search_var.get())
        char_filter = self._char_var.get()
        stage_filter = self._stage_var.get()

        filtered = []
        for r in self._replays:
            if char_filter != "All":
                player_chars = [p.get("character", "") for p in r.get("players", [])]
                if char_filter not in player_chars:
                    continue

            if stage_filter != "All":
                if r.get("stage") != stage_filter:
                    continue

            if terms:
                haystack = _search_haystack(r)
                if not all(t in haystack for t in terms):
                    continue

            filtered.append(r)

        self._filtered = filtered
        self._sort_and_populate()

    def _clear_filters(self):
        self._search_var.set("")
        self._char_var.set("All")
        self._stage_var.set("All")
        self._apply_filters()

    # ── Sorting ──────────────────────────────────────────────────────────

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = col == "date"
        self._sort_and_populate()

    def _sort_key(self, r: dict):
        col = self._sort_col
        if col == "date":
            return r.get("played_at") or ""
        elif col == "matchup":
            return _matchup(r.get("players", []))
        elif col == "stage":
            return r.get("stage") or ""
        elif col == "duration":
            return r.get("duration_seconds") or 0
        elif col == "players":
            return _players_summary(r.get("players", []), self._player_mode)
        elif col == "end":
            return r.get("end_type") or ""
        elif col == "console":
            return r.get("console_nick") or ""
        elif col == "platform":
            return r.get("played_on") or ""
        elif col == "slippi_v":
            return r.get("slippi_version") or ""
        return ""

    def _sort_and_populate(self):
        self._filtered.sort(key=self._sort_key, reverse=self._sort_reverse)
        self._populate_tree()

    # ── Tree population ──────────────────────────────────────────────────

    def _row_values(self, r: dict) -> tuple:
        col_map = {
            "date": _fmt_time(r.get("played_at", "")),
            "matchup": _matchup(r.get("players", [])),
            "stage": r.get("stage", "--"),
            "duration": _fmt_duration(r.get("duration_seconds")),
            "players": _players_summary(r.get("players", []), self._player_mode),
            "end": _fmt_end_type(r),
            "console": r.get("console_nick") or "--",
            "platform": (r.get("played_on") or "--").title(),
            "slippi_v": r.get("slippi_version") or "--",
        }
        return tuple(col_map.get(c[0], "--") for c in self._col_defs)

    def _row_tag(self, r: dict) -> str:
        end_type = r.get("end_type") or ""
        if end_type.startswith("rage_quit"):
            return "rage_quit"
        if end_type.startswith("lras"):
            return "lras"
        return ""

    def _populate_tree(self):
        self._tree.delete(*self._tree.get_children())
        for i, r in enumerate(self._filtered):
            tag = self._row_tag(r)
            self._tree.insert("", "end", iid=str(i),
                              values=self._row_values(r),
                              tags=(tag,) if tag else ())
        self._count_label.config(
            text=f"Showing {len(self._filtered)} of {len(self._replays)}")

    # ── Column settings ─────────────────────────────────────────────────

    def _open_column_settings(self):
        ColumnSettingsDialog(
            self.winfo_toplevel(), self._opt_cols,
            on_apply=self._apply_column_settings)

    def _apply_column_settings(self, enabled: set[str]):
        self._opt_cols = enabled
        self.cfg.set("app", "replay_columns",
                     ",".join(sorted(enabled)) if enabled else "")
        self.cfg.save()
        self._build_tree()
        self._sort_and_populate()

    # ── Selection & actions ──────────────────────────────────────────────

    def _get_selected_replay(self) -> dict | None:
        sel = self._tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            return self._filtered[idx]
        return None

    def _on_double_click(self, _event):
        self._show_details()

    def _show_details(self):
        r = self._get_selected_replay()
        if not r:
            return
        ReplayDetailDialog(
            self.winfo_toplevel(), r,
            launch_cb=self._launch_replay)

    def _watch_selected(self):
        r = self._get_selected_replay()
        if r:
            self._launch_replay(r)

    def _open_selected_folder(self):
        r = self._get_selected_replay()
        if not r:
            return
        folder = os.path.dirname(r.get("path", ""))
        if not folder:
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            pass

    # ── Replay playback ──────────────────────────────────────────────────

    def _launch_replay(self, replay: dict):
        """Launch Slippi Dolphin to play back a replay file."""
        slp_path = replay.get("path", "")
        if not slp_path or not os.path.isfile(slp_path):
            messagebox.showerror("Error", "Replay file not found.")
            return

        dolphin_dir = self.cfg.get("paths", "dolphin_dir")
        if not dolphin_dir:
            messagebox.showerror(
                "Error",
                "Dolphin path not configured. Set it in Settings.")
            return

        iso_path = self.cfg.get("paths", "iso")
        if not iso_path:
            messagebox.showerror(
                "Error",
                "Melee ISO path not configured. Set it in Settings.")
            return

        dolphin_exe = None
        dolphin_path = Path(dolphin_dir)
        for name in ("Slippi Dolphin.exe", "dolphin-emu", "Dolphin.exe"):
            candidate = dolphin_path / name
            if candidate.exists():
                dolphin_exe = str(candidate)
                break

        if not dolphin_exe:
            messagebox.showerror(
                "Error",
                f"Cannot find Dolphin executable in {dolphin_dir}")
            return

        cmd = [dolphin_exe, "-i", slp_path, "-e", iso_path]

        try:
            if sys.platform == "win32":
                subprocess.Popen(cmd)
            else:
                subprocess.Popen(cmd, start_new_session=True)
            self._status_label.config(text="Launching replay...")
        except Exception as exc:
            messagebox.showerror("Launch failed", str(exc))
