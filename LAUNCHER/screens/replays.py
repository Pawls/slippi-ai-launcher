"""Replay Browser screen: browse, filter, and watch .slp replay files."""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from LAUNCHER.config import AppConfig
from LAUNCHER.replay_store import (
    ReplayStore, normalize_fullwidth, char_abbrev,
)
from LAUNCHER.screens import Screen
from LAUNCHER.widgets import selectable_value


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


def _fmt_code(code: str) -> str:
    """Format connect code for compact display: 'PAWL#723' -> 'Pawl'."""
    if not code:
        return ""
    # Slippi stores connect codes with fullwidth ＃ (U+FF03), not ASCII #
    code = normalize_fullwidth(code)
    tag = code.split("#")[0] if "#" in code else code
    return tag.capitalize()


def _player_label(p: dict, mode: str) -> str:
    """Get display label for a player.

    mode: "code" shows formatted connect code, "name" shows netplay name.
    Falls back through code -> name -> tag if preferred field is empty.
    """
    if mode == "name":
        label = p.get("name") or _fmt_code(p.get("connect_code", "")) or p.get("name_tag")
    else:
        label = _fmt_code(p.get("connect_code", "")) or p.get("name") or p.get("name_tag")
    return label or ""


def _players_summary(players: list[dict], mode: str = "code",
                     is_teams: bool = False) -> str:
    """Format players for the table cell.

    Singles: 'Mekk (Ganon) vs Wobblez (ICs)'
    Doubles: 'Mekk (Ganon) & Wobblez (ICs)\\nvs n0ne (CFalcon) & Hbox (Puff)'
    """
    def fmt_one(p):
        char = char_abbrev(p.get("character", "?"))
        label = _player_label(p, mode)
        if label:
            return f"{label} ({char})"
        return char

    if is_teams and len(players) == 4:
        # Group by team
        teams: dict[int, list[dict]] = {}
        for p in players:
            team = p.get("team", 0)
            teams.setdefault(team, []).append(p)

        team_strs = []
        for _tid, members in sorted(teams.items()):
            team_strs.append(" & ".join(fmt_one(p) for p in members))
        return "\nvs ".join(team_strs)

    parts = [fmt_one(p) for p in players]
    return " vs ".join(parts)


def _fmt_input_type(r: dict) -> str:
    """Format per-player input types for the table cell.

    e.g. 'GCC vs Box', 'Both GCC', 'Both Box', or '--' if unknown.
    """
    players = r.get("players", [])
    types = [p.get("input_type") for p in players]
    known = [t for t in types if t]
    if not known:
        return "--"
    if len(known) == 1:
        return known[0]
    if len(set(known)) == 1:
        return f"Both {known[0]}"
    return " vs ".join(known)


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


def _replay_needs_upgrade(r: dict) -> bool:
    """Check if a replay needs upgrading based on its parsed metadata.

    Mirrors the logic in slippi_db.upgrade_slp.needs_upgrade() but works
    on the replay dict produced by ReplayStore rather than a peppi_py Game.
    """
    version_str = r.get("slippi_version")
    if not version_str:
        return False
    try:
        parts = tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return False

    if parts < (3, 2, 0):
        return True

    if parts < (3, 18, 0):
        stage = r.get("stage", "")
        if stage == "Fountain of Dreams":
            return True
        if stage == "Pokemon Stadium" and not r.get("is_frozen_ps", True):
            return True

    return False


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
        parts.append(char_abbrev(p.get("character", "")))
        parts.append(p.get("name", ""))
        code = p.get("connect_code", "")
        parts.append(code)
        parts.append(_fmt_code(code))
        parts.append(p.get("name_tag", ""))
        parts.append(p.get("input_type", ""))
    return " ".join(parts).lower()


def _normalize_search(query: str) -> list[str]:
    """Split search query into terms, stripping parens and brackets."""
    cleaned = query
    for ch in "()[]{}":
        cleaned = cleaned.replace(ch, " ")
    return [t for t in cleaned.lower().split() if t]


# All columns: (key, label, default_on, width, anchor)
ALL_COLUMNS = [
    ("date",     "Date",     True,  140, "w"),
    ("players",  "Players",  True,  300, "w"),
    ("stage",    "Stage",    True,  140, "w"),
    ("duration", "Duration", True,   70, "center"),
    ("end",      "End",      True,  100, "center"),
    ("console",  "Console",  False, 100, "w"),
    ("platform", "Platform", False,  80, "center"),
    ("slippi_v", "Slippi",   False,  60, "center"),
    ("input",    "Input",    False, 120, "center"),
]

# Columns that require a rescan with extra analysis when first enabled
_RESCAN_COLUMNS = {"input"}

# Legacy compat aliases
OPTIONAL_COLUMNS = [(k, l, d, w, a) for k, l, d, w, a in ALL_COLUMNS if not d]
CORE_COLUMNS = [(k, l, w, a) for k, l, d, w, a in ALL_COLUMNS if d]


# ── Multi-select dropdown ───────────────────────────────────────────────────

class CheckCombo(ttk.Frame):
    """A button that opens a dropdown with checkboxes for multi-select."""

    def __init__(self, parent, label: str, on_change=None, **kw):
        super().__init__(parent, **kw)
        self._label_text = label
        self._on_change = on_change
        self._values: list[str] = []
        self._vars: dict[str, tk.BooleanVar] = {}
        self._popup: tk.Toplevel | None = None

        self._btn = ttk.Button(self, text=f"{label}: All",
                               command=self._toggle_popup)
        self._btn.pack(fill="x")

    def set_values(self, values: list[str]):
        """Set the available values (resets all to unchecked = All)."""
        self._values = sorted(values)
        self._vars = {v: tk.BooleanVar(value=False) for v in self._values}
        self._update_label()

    def get_selected(self) -> set[str]:
        """Return selected values, or empty set meaning 'All'."""
        sel = {k for k, v in self._vars.items() if v.get()}
        return sel

    def clear(self):
        for v in self._vars.values():
            v.set(False)
        self._update_label()

    def _update_label(self):
        sel = self.get_selected()
        if not sel:
            self._btn.config(text=f"{self._label_text}: All")
        elif len(sel) == 1:
            self._btn.config(text=f"{self._label_text}: {next(iter(sel))}")
        else:
            self._btn.config(text=f"{self._label_text}: {len(sel)} selected")

    def _toggle_popup(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None
            return
        self._show_popup()

    def _show_popup(self):
        if not self._values:
            return

        self._popup = popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.transient(self.winfo_toplevel())

        # Position below the button
        x = self._btn.winfo_rootx()
        y = self._btn.winfo_rooty() + self._btn.winfo_height()
        popup.geometry(f"+{x}+{y}")

        frame = ttk.Frame(popup, relief="solid", borderwidth=1)
        frame.pack(fill="both", expand=True)

        # Scrollable if many items
        if len(self._values) > 12:
            canvas = tk.Canvas(frame, width=180, height=300)
            scrollbar = ttk.Scrollbar(frame, orient="vertical",
                                      command=canvas.yview)
            inner = ttk.Frame(canvas)
            inner.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            target = inner
        else:
            target = frame

        # Select All / Clear buttons
        btn_row = ttk.Frame(target)
        btn_row.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(btn_row, text="All", width=5,
                   command=lambda: self._select_all(True)).pack(side="left", padx=1)
        ttk.Button(btn_row, text="None", width=5,
                   command=lambda: self._select_all(False)).pack(side="left", padx=1)

        for val in self._values:
            ttk.Checkbutton(
                target, text=val, variable=self._vars[val],
                command=self._on_check
            ).pack(anchor="w", padx=6, pady=1)

        # Close when clicking elsewhere
        popup.bind("<FocusOut>", self._on_focus_out)
        popup.focus_set()

    def _select_all(self, state: bool):
        for v in self._vars.values():
            v.set(state)
        self._on_check()

    def _on_check(self):
        self._update_label()
        if self._on_change:
            self._on_change()

    def _on_focus_out(self, event):
        # Only close if focus went outside the popup
        if self._popup:
            try:
                focus = self._popup.focus_get()
                if focus and str(focus).startswith(str(self._popup)):
                    return
            except Exception:
                pass
            self._popup.after(100, self._maybe_close)

    def _maybe_close(self):
        if self._popup and self._popup.winfo_exists():
            try:
                focus = self._popup.focus_get()
                if focus and str(focus).startswith(str(self._popup)):
                    return
            except Exception:
                pass
            self._popup.destroy()
            self._popup = None


# ── Column settings dialog ──────────────────────────────────────────────────

class ColumnSettingsDialog(tk.Toplevel):
    def __init__(self, parent, current: set[str], on_apply=None):
        super().__init__(parent)
        self.title("Column Settings")
        self.transient(parent)
        self.resizable(False, False)
        self._on_apply = on_apply

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Visible Columns",
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 8))

        self._vars: dict[str, tk.BooleanVar] = {}
        for key, label, _default, _w, _a in ALL_COLUMNS:
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
        if not enabled:
            messagebox.showwarning(
                "No Columns Selected",
                "At least one column must be visible.",
                parent=self)
            return
        if self._on_apply:
            self._on_apply(enabled)
        self.destroy()


# ── Upgrade dialog ──────────────────────────────────────────────────────────

class UpgradeDialog(tk.Toplevel):
    """Dialog to upgrade old Slippi replays with backup option and progress."""

    def __init__(self, parent, replays: list[dict], *,
                 dolphin_exe: str, iso_path: str,
                 on_complete=None):
        super().__init__(parent)
        self.title("Upgrade Replays")
        self.transient(parent)
        self.resizable(False, False)
        self._replays = replays
        self._dolphin_exe = dolphin_exe
        self._iso_path = iso_path
        self._on_complete = on_complete
        self._upgrading = False
        self._cancel_requested = False
        self._upgraded_count = 0

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        n = len(replays)
        ttk.Label(
            frame,
            text=f"{n} replay{'s' if n != 1 else ''} can be upgraded",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            frame, wraplength=420,
            text="Upgrading re-processes replays through Dolphin to add "
                 "missing data (player names, platform heights, stage "
                 "transformations). This requires Dolphin and your Melee ISO.",
        ).pack(anchor="w", pady=(0, 12))

        # Backup option
        self._backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Back up original files before upgrading (recommended)",
            variable=self._backup_var,
        ).pack(anchor="w", pady=(0, 4))

        self._backup_dir_frame = ttk.Frame(frame)
        self._backup_dir_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(self._backup_dir_frame, text="Backup folder:").pack(
            side="left")
        default_backup = str(
            Path(replays[0].get("path", "")).parent.parent / "slp_backups")
        self._backup_dir_var = tk.StringVar(value=default_backup)
        ttk.Entry(
            self._backup_dir_frame, textvariable=self._backup_dir_var,
            width=40,
        ).pack(side="left", padx=(6, 4), fill="x", expand=True)
        ttk.Button(
            self._backup_dir_frame, text="Browse...",
            command=self._browse_backup_dir,
        ).pack(side="left")

        # Progress
        self._progress_frame = ttk.Frame(frame)
        self._progress_label = ttk.Label(self._progress_frame, text="")
        self._progress_label.pack(anchor="w")
        self._progress_bar = ttk.Progressbar(
            self._progress_frame, mode="determinate")
        self._progress_bar.pack(fill="x", pady=(4, 0))
        self._detail_label = ttk.Label(
            self._progress_frame, text="", foreground="gray",
            font=("TkDefaultFont", 8))
        self._detail_label.pack(anchor="w", pady=(2, 0))

        # Buttons
        self._btn_frame = ttk.Frame(frame)
        self._btn_frame.pack(fill="x", pady=(8, 0))

        self._cancel_btn = ttk.Button(
            self._btn_frame, text="Cancel", command=self._on_cancel)
        self._cancel_btn.pack(side="right", padx=2)
        self._start_btn = ttk.Button(
            self._btn_frame, text="Start Upgrade", command=self._on_start)
        self._start_btn.pack(side="right", padx=2)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _browse_backup_dir(self):
        d = filedialog.askdirectory(
            initialdir=self._backup_dir_var.get() or None)
        if d:
            self._backup_dir_var.set(d)

    def _on_start(self):
        if self._upgrading:
            return

        # Validate backup dir if backup enabled
        if self._backup_var.get():
            backup_dir = self._backup_dir_var.get().strip()
            if not backup_dir:
                messagebox.showwarning(
                    "Backup Folder Required",
                    "Please specify a backup folder or uncheck the backup option.")
                return

        self._upgrading = True
        self._start_btn.config(state="disabled")
        self._cancel_btn.config(text="Cancel Upgrade")
        self._progress_frame.pack(fill="x", pady=(0, 8),
                                  before=self._btn_frame)

        threading.Thread(target=self._run_upgrades, daemon=True).start()

    def _run_upgrades(self):
        """Run upgrades in a background thread."""
        from slippi_db.upgrade_slp import (
            upgrade_slp, DolphinConfig, needs_upgrade, DolphinTimeoutError,
        )
        import peppi_py

        replays = self._replays
        do_backup = self._backup_var.get()
        backup_dir = Path(self._backup_dir_var.get().strip()) if do_backup else None
        dolphin_config = DolphinConfig(
            dolphin_path=self._dolphin_exe,
            ssbm_iso_path=self._iso_path,
        )

        total = len(replays)
        upgraded = 0
        errors = []

        for i, replay in enumerate(replays):
            if self._cancel_requested:
                break

            slp_path = replay.get("path", "")
            if not slp_path or not os.path.isfile(slp_path):
                continue

            filename = os.path.basename(slp_path)
            self.after(0, lambda i=i, f=filename, t=total:
                       self._update_progress(i, t, f))

            # Verify with peppi that upgrade is still needed
            try:
                game = peppi_py.read_slippi(slp_path, skip_frames=True)
                if not needs_upgrade(game):
                    continue
            except Exception:
                continue

            # Backup
            if do_backup and backup_dir:
                try:
                    # Preserve relative structure under the replays dir
                    rel = os.path.basename(slp_path)
                    dest = backup_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(slp_path, dest)
                except Exception as e:
                    logging.warning(f"Backup failed for {slp_path}: {e}")
                    errors.append((filename, f"backup failed: {e}"))
                    continue

            # Upgrade to temp file, then replace original
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    output_path = os.path.join(tmp_dir, "upgraded.slp")
                    upgrade_slp(
                        slp_path, output_path, dolphin_config,
                        in_memory=True, time_limit=60)

                    # Verify the upgraded file exists and is valid
                    if not os.path.isfile(output_path):
                        errors.append((filename, "upgrade produced no output"))
                        continue

                    # Replace original with upgraded version
                    shutil.move(output_path, slp_path)
                    upgraded += 1

            except DolphinTimeoutError:
                errors.append((filename, "Dolphin timed out"))
            except Exception as e:
                errors.append((filename, str(e)))

        self._upgraded_count = upgraded
        self.after(0, lambda: self._on_done(upgraded, errors, total))

    def _update_progress(self, current: int, total: int, filename: str):
        self._progress_bar["maximum"] = total
        self._progress_bar["value"] = current
        self._progress_label.config(
            text=f"Upgrading {current + 1} of {total}...")
        self._detail_label.config(text=filename)

    def _on_done(self, upgraded: int, errors: list, total: int):
        self._upgrading = False
        self._cancel_btn.config(text="Close")
        self._start_btn.pack_forget()

        if self._cancel_requested:
            self._progress_label.config(
                text=f"Cancelled. Upgraded {upgraded} of {total} replays.")
        elif errors:
            self._progress_label.config(
                text=f"Done. Upgraded {upgraded} of {total} replays "
                     f"({len(errors)} error{'s' if len(errors) != 1 else ''}).")
            self._detail_label.config(
                text=f"First error: {errors[0][0]}: {errors[0][1]}")
        else:
            self._progress_label.config(
                text=f"Successfully upgraded {upgraded} replay"
                     f"{'s' if upgraded != 1 else ''}.")
            self._detail_label.config(text="")

        self._progress_bar["value"] = total

    def _on_cancel(self):
        if self._upgrading:
            self._cancel_requested = True
            self._cancel_btn.config(state="disabled")
            self._progress_label.config(text="Cancelling...")
        else:
            if self._on_complete:
                self._on_complete(self._upgraded_count)
            self.destroy()


# ── Delete short replays dialog ────────────────────────────────────────────

class DeleteShortReplaysDialog(tk.Toplevel):
    """Dialog to delete replays shorter than a specified duration."""

    def __init__(self, parent, replays: list[dict], *, on_complete=None):
        super().__init__(parent)
        self.title("Delete Short Replays")
        self.transient(parent)
        self.resizable(False, False)
        self._replays = replays
        self._on_complete = on_complete
        self._deleted_count = 0

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Delete Short Replays",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            frame, wraplength=400,
            text="Delete replay files shorter than a specified duration. "
                 "This is useful for removing incomplete games and "
                 "accidental starts. Files are permanently deleted.",
        ).pack(anchor="w", pady=(0, 12))

        # Duration threshold
        thresh_frame = ttk.Frame(frame)
        thresh_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(thresh_frame, text="Delete replays shorter than:").pack(
            side="left")
        self._seconds_var = tk.IntVar(value=30)
        spin = ttk.Spinbox(
            thresh_frame, from_=5, to=600, increment=5,
            textvariable=self._seconds_var, width=6,
            command=self._update_preview)
        spin.pack(side="left", padx=(6, 4))
        spin.bind("<KeyRelease>", lambda _: self._update_preview())
        ttk.Label(thresh_frame, text="seconds").pack(side="left")

        # Preview
        self._preview_label = ttk.Label(frame, text="", foreground="gray")
        self._preview_label.pack(anchor="w", pady=(0, 8))
        self._update_preview()

        # Progress
        self._progress_frame = ttk.Frame(frame)
        self._progress_label = ttk.Label(self._progress_frame, text="")
        self._progress_label.pack(anchor="w")
        self._progress_bar = ttk.Progressbar(
            self._progress_frame, mode="determinate")
        self._progress_bar.pack(fill="x", pady=(4, 0))

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(8, 0))

        self._close_btn = ttk.Button(
            btn_frame, text="Cancel", command=self._on_close)
        self._close_btn.pack(side="right", padx=2)
        self._delete_btn = ttk.Button(
            btn_frame, text="Delete", command=self._on_delete)
        self._delete_btn.pack(side="right", padx=2)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _get_threshold(self) -> int:
        try:
            return max(1, self._seconds_var.get())
        except (tk.TclError, ValueError):
            return 30

    def _matching_replays(self) -> list[dict]:
        threshold = self._get_threshold()
        return [
            r for r in self._replays
            if (r.get("duration_seconds") or 0) < threshold
            and r.get("path") and os.path.isfile(r["path"])
        ]

    def _update_preview(self):
        matches = self._matching_replays()
        n = len(matches)
        threshold = self._get_threshold()
        self._preview_label.config(
            text=f"{n} replay{'s' if n != 1 else ''} shorter than "
                 f"{threshold}s found ({len(self._replays)} total)")
        self._delete_btn.config(
            state="normal" if n > 0 else "disabled")

    def _on_delete(self):
        matches = self._matching_replays()
        if not matches:
            return

        n = len(matches)
        threshold = self._get_threshold()
        if not messagebox.askyesno(
            "Confirm Deletion",
            f"Permanently delete {n} replay{'s' if n != 1 else ''} "
            f"shorter than {threshold} seconds?\n\n"
            f"This cannot be undone.",
            parent=self,
        ):
            return

        self._delete_btn.config(state="disabled")
        self._progress_frame.pack(fill="x", pady=(0, 8),
                                  before=self._close_btn.master)

        deleted = 0
        errors = 0
        for i, r in enumerate(matches):
            path = r["path"]
            self._progress_bar["maximum"] = n
            self._progress_bar["value"] = i + 1
            self._progress_label.config(
                text=f"Deleting {i + 1} of {n}...")
            self.update_idletasks()
            try:
                os.remove(path)
                deleted += 1
            except OSError:
                errors += 1

        self._deleted_count = deleted
        self._progress_label.config(
            text=f"Deleted {deleted} replay{'s' if deleted != 1 else ''}"
                 + (f" ({errors} failed)" if errors else "")
                 + ".")
        self._progress_bar["value"] = n
        self._delete_btn.pack_forget()
        self._close_btn.config(text="Close")

    def _on_close(self):
        if self._on_complete:
            self._on_complete(self._deleted_count)
        self.destroy()


# ── Replay detail dialog ────────────────────────────────────────────────────

class ReplayDetailDialog(tk.Toplevel):
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

        info.columnconfigure(1, weight=1)
        for i, (label, value) in enumerate(fields):
            ttk.Label(info, text=f"{label}:", anchor="e",
                      width=14).grid(row=i, column=0, sticky="e", padx=(0, 6))
            selectable_value(info, value).grid(
                row=i, column=1, sticky="ew")

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
            input_type = p.get("input_type")
            input_str = f"  [{input_type}]" if input_type else ""
            parts.append(f"- {ptype}{place_str}{stock_str}{input_str}")
            selectable_value(players_frame, " ".join(parts)).pack(
                fill="x", anchor="w")

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

        # Load column visibility prefs
        saved = cfg.get("app", "replay_visible_columns")
        if not saved:
            # Migrate from old key (only stored optional columns)
            old = cfg.get("app", "replay_columns")
            if old:
                defaults = {k for k, _, d, _, _ in ALL_COLUMNS if d}
                self._visible_cols: set[str] = defaults | set(old.split(","))
            else:
                self._visible_cols = {k for k, _, d, _, _ in ALL_COLUMNS if d}
        else:
            self._visible_cols = set(saved.split(",")) if saved else set()

        # Load saved column widths
        saved_widths = cfg.get("app", "replay_column_widths")
        self._col_widths: dict[str, int] = {}
        if saved_widths:
            for pair in saved_widths.split(","):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    try:
                        self._col_widths[k] = int(v)
                    except ValueError:
                        pass

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
            value=cfg.get("app", "replay_browse_dir")
                   or cfg.get("paths", "replays_dir"))
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
        self._search_entry = ttk.Entry(
            filter_frame, textvariable=self._search_var, width=24)
        self._search_entry.grid(row=0, column=1, padx=(0, 12))

        self._char_filter = CheckCombo(
            filter_frame, "Character", on_change=self._apply_filters)
        self._char_filter.grid(row=0, column=2, padx=(0, 8))

        self._stage_filter = CheckCombo(
            filter_frame, "Stage", on_change=self._apply_filters)
        self._stage_filter.grid(row=0, column=3, padx=(0, 8))

        self._console_filter = CheckCombo(
            filter_frame, "Console", on_change=self._apply_filters)
        self._console_filter.grid(row=0, column=4, padx=(0, 8))

        self._platform_filter = CheckCombo(
            filter_frame, "Platform", on_change=self._apply_filters)
        self._platform_filter.grid(row=0, column=5, padx=(0, 8))

        # Duration filter
        dur_frame = ttk.Frame(filter_frame)
        dur_frame.grid(row=0, column=6, padx=(0, 8))
        ttk.Label(dur_frame, text="Duration:").pack(side="left")
        self._dur_min_var = tk.StringVar()
        self._dur_min_var.trace_add("write", lambda *_: self._apply_filters())
        ttk.Entry(dur_frame, textvariable=self._dur_min_var,
                  width=5).pack(side="left", padx=(4, 0))
        ttk.Label(dur_frame, text="-").pack(side="left", padx=2)
        self._dur_max_var = tk.StringVar()
        self._dur_max_var.trace_add("write", lambda *_: self._apply_filters())
        ttk.Entry(dur_frame, textvariable=self._dur_max_var,
                  width=5).pack(side="left")
        ttk.Label(dur_frame, text="sec").pack(side="left", padx=(2, 0))

        ttk.Button(filter_frame, text="Clear",
                   command=self._clear_filters).grid(row=0, column=7)

        # Upgrade banner (hidden initially)
        self._upgrade_banner = ttk.Frame(outer)
        self._upgrade_banner_inner = tk.Frame(
            self._upgrade_banner, bg="#fff3cd", padx=10, pady=6)
        self._upgrade_banner_inner.pack(fill="x")

        self._upgrade_label = tk.Label(
            self._upgrade_banner_inner, bg="#fff3cd", fg="#664d03",
            font=("TkDefaultFont", 9))
        self._upgrade_label.pack(side="left")

        ttk.Button(
            self._upgrade_banner_inner, text="Dismiss",
            command=self._dismiss_upgrade_banner
        ).pack(side="right", padx=(4, 0))
        ttk.Button(
            self._upgrade_banner_inner, text="Upgrade...",
            command=self._start_upgrade_dialog
        ).pack(side="right")

        self._upgradable_replays: list[dict] = []
        self._upgrade_dismissed = False

        # "Upgrade available" restore button (hidden initially)
        self._upgrade_restore_btn = ttk.Button(
            outer, text="\u26a0 Upgradable replays available — click to review",
            command=self._restore_upgrade_banner)

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
        ttk.Button(bottom, text="Copy Path",
                   command=self._copy_selected_path).pack(side="left", padx=2)
        ttk.Button(bottom, text="Delete Selected",
                   command=self._delete_selected).pack(side="left", padx=2)

        sep = ttk.Separator(bottom, orient="vertical")
        sep.pack(side="left", fill="y", padx=8, pady=2)

        ttk.Label(bottom, text="Show by:").pack(side="left")
        self._mode_var = tk.StringVar(value=self._player_mode)
        ttk.Radiobutton(bottom, text="Code", variable=self._mode_var,
                        value="code",
                        command=self._on_mode_change).pack(side="left", padx=2)
        ttk.Radiobutton(bottom, text="Name", variable=self._mode_var,
                        value="name",
                        command=self._on_mode_change).pack(side="left", padx=2)

        ttk.Button(bottom, text="Columns...",
                   command=self._open_column_settings).pack(side="left", padx=(8, 2))
        ttk.Button(bottom, text="Delete Short Replays...",
                   command=self._open_delete_short).pack(side="left", padx=2)

        self._count_label = ttk.Label(
            bottom, text="", foreground="gray",
            font=("TkDefaultFont", 9))
        self._count_label.pack(side="right")

        # Sort state
        self._sort_col = "date"
        self._sort_reverse = True

        # Progress bar (hidden initially)
        self._progress = ttk.Progressbar(outer, mode="determinate")

        # Keyboard shortcuts
        self.bind_all("<Control-f>", self._focus_search)

    # ── Tree building ────────────────────────────────────────────────────

    def _build_tree(self):
        for w in self._tree_frame.winfo_children():
            w.destroy()

        self._col_defs = [
            (key, label, width, anchor)
            for key, label, _default, width, anchor in ALL_COLUMNS
            if key in self._visible_cols
        ]

        col_keys = [c[0] for c in self._col_defs]
        self._tree = ttk.Treeview(
            self._tree_frame, columns=col_keys, show="headings",
            selectmode="extended")

        for key, label, default_w, anchor in self._col_defs:
            width = self._col_widths.get(key, default_w)
            self._tree.heading(key, text=label,
                               command=lambda k=key: self._sort_by(k))
            self._tree.column(key, width=width, anchor=anchor)

        self._tree.tag_configure("rage_quit", foreground="#c62828")
        self._tree.tag_configure("lras", foreground="#f57f17")
        self._tree.tag_configure("teams", foreground="#1565C0")

        scrollbar = ttk.Scrollbar(
            self._tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<ButtonRelease-1>", self._on_column_resize)
        self._tree.bind("<Return>", lambda _: self._show_details())
        self._tree.bind("<Delete>", lambda _: self._delete_selected())

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_enter(self):
        current_dir = (self.cfg.get("app", "replay_browse_dir")
                       or self.cfg.get("paths", "replays_dir"))
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
        initial = self._dir_var.get().strip()
        d = filedialog.askdirectory(
            initialdir=initial if initial and os.path.isdir(initial) else None)
        if d:
            self._dir_var.set(d)
            self.cfg.set("app", "replay_browse_dir", d)
            self.cfg.save()

    # ── Scanning ─────────────────────────────────────────────────────────

    def _scan(self, detect_box: bool | None = None):
        if self._scanning:
            return
        replays_dir = self._dir_var.get().strip()
        if not replays_dir or not os.path.isdir(replays_dir):
            messagebox.showwarning(
                "No Replays Directory",
                "Please set a valid replays directory.")
            return

        # Persist the directory choice (browser-specific, not settings replays_dir)
        self.cfg.set("app", "replay_browse_dir", replays_dir)
        self.cfg.save()

        # Auto-enable box detection when the input column is visible
        if detect_box is None:
            detect_box = "input" in self._visible_cols

        self._scanning = True
        self._scan_btn.config(state="disabled")
        self._status_label.config(text="Scanning...")
        self._progress.pack(fill="x", pady=(4, 0))
        self._progress["value"] = 0

        def do_scan():
            def progress_cb(current, total):
                if total > 0:
                    self.after(0, lambda: self._update_progress(current, total))

            results = self._store.scan(replays_dir, progress_cb=progress_cb,
                                       detect_box=detect_box)
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
        self._check_upgradable()

    # ── Filtering ────────────────────────────────────────────────────────

    def _rebuild_filter_options(self):
        chars = set()
        stages = set()
        consoles = set()
        platforms = set()
        for r in self._replays:
            for p in r.get("players", []):
                chars.add(p.get("character", ""))
            stages.add(r.get("stage", ""))
            if r.get("console_nick"):
                consoles.add(r["console_nick"])
            if r.get("played_on"):
                platforms.add(r["played_on"].title())

        chars.discard("")
        stages.discard("")

        self._char_filter.set_values(list(chars))
        self._stage_filter.set_values(list(stages))
        self._console_filter.set_values(list(consoles))
        self._platform_filter.set_values(list(platforms))

    def _parse_dur_filter(self, var: tk.StringVar) -> int | None:
        val = var.get().strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    def _apply_filters(self):
        terms = _normalize_search(self._search_var.get())
        char_sel = self._char_filter.get_selected()
        stage_sel = self._stage_filter.get_selected()
        console_sel = self._console_filter.get_selected()
        platform_sel = self._platform_filter.get_selected()
        dur_min = self._parse_dur_filter(self._dur_min_var)
        dur_max = self._parse_dur_filter(self._dur_max_var)

        filtered = []
        for r in self._replays:
            if char_sel:
                player_chars = {p.get("character", "") for p in r.get("players", [])}
                if not char_sel & player_chars:
                    continue

            if stage_sel:
                if r.get("stage") not in stage_sel:
                    continue

            if console_sel:
                if (r.get("console_nick") or "") not in console_sel:
                    continue

            if platform_sel:
                if (r.get("played_on") or "").title() not in platform_sel:
                    continue

            if dur_min is not None:
                dur = r.get("duration_seconds") or 0
                if dur < dur_min:
                    continue

            if dur_max is not None:
                dur = r.get("duration_seconds") or 0
                if dur > dur_max:
                    continue

            if terms:
                haystack = _search_haystack(r)
                if not all(t in haystack for t in terms):
                    continue

            filtered.append(r)

        self._filtered = filtered
        self._sort_and_populate()

    def _focus_search(self, _event=None):
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")
        return "break"

    def _clear_filters(self):
        self._search_var.set("")
        self._char_filter.clear()
        self._stage_filter.clear()
        self._console_filter.clear()
        self._platform_filter.clear()
        self._dur_min_var.set("")
        self._dur_max_var.set("")
        self._apply_filters()

    # ── Upgrade detection & banner ──────────────────────────────────────

    def _check_upgradable(self):
        """After scan, check for replays that can be upgraded."""
        self._upgradable_replays = [
            r for r in self._replays if _replay_needs_upgrade(r)
        ]
        if self._upgradable_replays and not self._upgrade_dismissed:
            self._show_upgrade_banner()
        else:
            self._hide_upgrade_banner()

    def _show_upgrade_banner(self):
        n = len(self._upgradable_replays)
        self._upgrade_label.config(
            text=f"\u26a0 {n} replay{'s' if n != 1 else ''} from an older "
                 f"Slippi version can be upgraded to restore missing data "
                 f"(player names, stage events).")
        # Pack banner before the treeview
        self._upgrade_restore_btn.pack_forget()
        self._upgrade_banner.pack(fill="x", pady=(0, 4),
                                  before=self._tree_frame)

    def _hide_upgrade_banner(self):
        self._upgrade_banner.pack_forget()

    def _dismiss_upgrade_banner(self):
        self._upgrade_dismissed = True
        self._hide_upgrade_banner()
        if self._upgradable_replays:
            self._upgrade_restore_btn.pack(fill="x", pady=(0, 4),
                                           before=self._tree_frame)

    def _restore_upgrade_banner(self):
        self._upgrade_dismissed = False
        self._upgrade_restore_btn.pack_forget()
        if self._upgradable_replays:
            self._show_upgrade_banner()

    def _start_upgrade_dialog(self):
        """Open the upgrade dialog."""
        if not self._upgradable_replays:
            return

        dolphin_dir = self.cfg.get("paths", "dolphin_dir")
        iso_path = self.cfg.get("paths", "iso")

        if not dolphin_dir or not iso_path:
            messagebox.showerror(
                "Configuration Required",
                "Dolphin path and Melee ISO must be configured in Settings "
                "before upgrading replays.")
            return

        # Find dolphin executable
        dolphin_exe = None
        dolphin_path = Path(dolphin_dir)
        for name in ("Slippi Dolphin.exe", "dolphin-emu", "Dolphin.exe"):
            candidate = dolphin_path / name
            if candidate.exists():
                dolphin_exe = str(candidate)
                break

        if not dolphin_exe:
            messagebox.showerror(
                "Dolphin Not Found",
                f"Cannot find Dolphin executable in {dolphin_dir}")
            return

        # Check for copy_slp_metadata
        if not shutil.which("copy_slp_metadata"):
            messagebox.showerror(
                "Missing Dependency",
                "'copy_slp_metadata' was not found in PATH.\n\n"
                "This tool is required to preserve replay metadata during "
                "upgrades. Install it from the slippi-db package.")
            return

        UpgradeDialog(
            self.winfo_toplevel(),
            self._upgradable_replays,
            dolphin_exe=dolphin_exe,
            iso_path=iso_path,
            on_complete=self._on_upgrade_complete,
        )

    def _on_upgrade_complete(self, upgraded_count: int):
        """Called when upgrades finish; rescan to refresh data."""
        self._hide_upgrade_banner()
        self._upgrade_restore_btn.pack_forget()
        if upgraded_count > 0:
            self._status_label.config(
                text=f"Upgraded {upgraded_count} replay"
                     f"{'s' if upgraded_count != 1 else ''}. Rescanning...")
            self._scan()

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
        elif col == "players":
            return _players_summary(
                r.get("players", []), self._player_mode,
                r.get("is_teams", False))
        elif col == "stage":
            return r.get("stage") or ""
        elif col == "duration":
            return r.get("duration_seconds") or 0
        elif col == "end":
            return r.get("end_type") or ""
        elif col == "console":
            return r.get("console_nick") or ""
        elif col == "platform":
            return r.get("played_on") or ""
        elif col == "slippi_v":
            return r.get("slippi_version") or ""
        elif col == "input":
            return _fmt_input_type(r)
        return ""

    def _sort_and_populate(self):
        self._filtered.sort(key=self._sort_key, reverse=self._sort_reverse)
        self._populate_tree()

    # ── Tree population ──────────────────────────────────────────────────

    def _row_values(self, r: dict) -> tuple:
        players_text = _players_summary(
            r.get("players", []), self._player_mode,
            r.get("is_teams", False))
        # For teams rows with newlines, replace with " vs " for single-line
        # Treeview display (detail dialog shows full format)
        players_text = players_text.replace("\n", " ")

        col_map = {
            "date": _fmt_time(r.get("played_at", "")),
            "players": players_text,
            "stage": r.get("stage", "--"),
            "duration": _fmt_duration(r.get("duration_seconds")),
            "end": _fmt_end_type(r),
            "console": r.get("console_nick") or "--",
            "platform": (r.get("played_on") or "--").title(),
            "slippi_v": r.get("slippi_version") or "--",
            "input": _fmt_input_type(r),
        }
        return tuple(col_map.get(c[0], "--") for c in self._col_defs)

    def _row_tag(self, r: dict) -> str:
        end_type = r.get("end_type") or ""
        if end_type.startswith("rage_quit"):
            return "rage_quit"
        if end_type.startswith("lras"):
            return "lras"
        if r.get("is_teams"):
            return "teams"
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
            self.winfo_toplevel(), self._visible_cols,
            on_apply=self._apply_column_settings)

    def _apply_column_settings(self, enabled: set[str]):
        newly_enabled = enabled - self._visible_cols
        self._visible_cols = enabled
        self._save_column_prefs()
        self._build_tree()
        self._sort_and_populate()

        # Prompt rescan if a column requiring extra analysis was just enabled
        if newly_enabled & _RESCAN_COLUMNS:
            if not self._store.has_input_type_data():
                if messagebox.askyesno(
                    "Rescan Required",
                    "The Input column requires controller analysis that "
                    "hasn't been computed yet.\n\n"
                    "Rescan replays now? (This may take a moment.)",
                ):
                    self._scan(detect_box=True)

    def _on_column_resize(self, _event):
        """Detect column width changes and persist them."""
        if not self._tree:
            return
        changed = False
        for key, _label, default_w, _anchor in self._col_defs:
            current_w = self._tree.column(key, "width")
            saved_w = self._col_widths.get(key, default_w)
            if current_w != saved_w:
                self._col_widths[key] = current_w
                changed = True
        if changed:
            self._save_column_prefs()

    def _save_column_prefs(self):
        """Persist visible columns and column widths to config."""
        self.cfg.set("app", "replay_visible_columns",
                     ",".join(sorted(self._visible_cols)))
        pairs = [f"{k}:{v}" for k, v in sorted(self._col_widths.items())]
        self.cfg.set("app", "replay_column_widths",
                     ",".join(pairs) if pairs else "")
        self.cfg.save()

    # ── Delete short replays ────────────────────────────────────────────

    def _open_delete_short(self):
        if not self._replays:
            messagebox.showinfo(
                "No Replays",
                "Scan a replays directory first.")
            return
        DeleteShortReplaysDialog(
            self.winfo_toplevel(), self._replays,
            on_complete=self._on_delete_short_complete)

    def _on_delete_short_complete(self, deleted_count: int):
        if deleted_count > 0:
            self._status_label.config(
                text=f"Deleted {deleted_count} replay"
                     f"{'s' if deleted_count != 1 else ''}. Rescanning...")
            self._scan()

    # ── Selection & actions ──────────────────────────────────────────────

    def _get_selected_replay(self) -> dict | None:
        sel = self._tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            return self._filtered[idx]
        return None

    def _get_selected_replays(self) -> list[dict]:
        sel = self._tree.selection()
        if not sel:
            return []
        replays = []
        for item in sel:
            idx = int(item)
            if 0 <= idx < len(self._filtered):
                replays.append(self._filtered[idx])
        return replays

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

    def _copy_selected_path(self):
        replays = self._get_selected_replays()
        if not replays:
            return
        paths = [r.get("path", "") for r in replays if r.get("path")]
        if paths:
            self.clipboard_clear()
            self.clipboard_append("\n".join(paths))
            n = len(paths)
            self._status_label.config(
                text=f"Copied {n} path{'s' if n != 1 else ''} to clipboard")

    def _delete_selected(self):
        replays = self._get_selected_replays()
        if not replays:
            return
        n = len(replays)
        paths = [r.get("path", "") for r in replays
                 if r.get("path") and os.path.isfile(r["path"])]
        if not paths:
            return
        if not messagebox.askyesno(
            "Confirm Deletion",
            f"Permanently delete {n} selected replay"
            f"{'s' if n != 1 else ''}?\n\n"
            f"This cannot be undone.",
        ):
            return
        deleted = 0
        for p in paths:
            try:
                os.remove(p)
                deleted += 1
            except OSError:
                pass
        if deleted > 0:
            self._status_label.config(
                text=f"Deleted {deleted} replay"
                     f"{'s' if deleted != 1 else ''}. Rescanning...")
            self._scan()

    # ── Replay playback ──────────────────────────────────────────────────

    def _launch_replay(self, replay: dict):
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
