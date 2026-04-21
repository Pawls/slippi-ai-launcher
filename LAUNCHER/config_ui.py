"""Tkinter-specific config helpers: path field builders and treeview utilities.

Split from config.py to keep the core config module UI-agnostic.
"""

import tkinter as tk
from tkinter import filedialog, ttk

from LAUNCHER.config import (
    AppConfig,
    detect_agents_dir, detect_root,
    slippi_dolphin_dir, slippi_iso, slippi_replays_dir, slippi_user_json,
)

PATH_ROWS = [
  ("slippi_ai_root", "Slippi-AI root directory",              "dir"),
  ("iso",            "Melee 1.02 ISO",                        "file_iso"),
  ("dolphin_dir",    "Slippi Dolphin folder",                 "dir"),
  ("dolphin_headless", "Headless Dolphin for training (optional)", "file_exe"),
  ("bot_vs_human_exe", "Bot vs Human Dolphin executable (optional)", "file_exe"),
  ("bot_vs_human_headless_exe", "Bot vs Human Dolphin (headless) executable (optional)", "file_exe"),
  ("user_json",      "Slippi Online user.json (netplay only)","file_json"),
  ("agents_dir",     "Agents directory",                      "dir"),
  ("replays_dir",    "Replays directory (optional)",          "dir"),
  ("m_overlay",      "m'overlay executable (optional)",       "file_exe"),
]


def build_path_fields(parent, cfg: AppConfig) -> dict[str, tk.StringVar]:
  """Build path entry rows in the given parent frame. Returns StringVar dict."""
  root_val = cfg.get("paths", "slippi_ai_root") or detect_root()
  initial = {
    "slippi_ai_root": root_val,
    "iso":            cfg.get("paths", "iso")         or slippi_iso(),
    "dolphin_dir":    cfg.get("paths", "dolphin_dir") or slippi_dolphin_dir(),
    "dolphin_headless": cfg.get("paths", "dolphin_headless"),
    "bot_vs_human_exe": cfg.get("paths", "bot_vs_human_exe"),
    "bot_vs_human_headless_exe": cfg.get("paths", "bot_vs_human_headless_exe"),
    "user_json":      cfg.get("paths", "user_json")   or slippi_user_json(),
    "agents_dir":     cfg.get("paths", "agents_dir")  or detect_agents_dir(root_val),
    "replays_dir":    cfg.get("paths", "replays_dir") or slippi_replays_dir(),
    "m_overlay":      cfg.get("paths", "m_overlay"),
  }

  v: dict[str, tk.StringVar] = {}
  for k, val in initial.items():
    v[k] = tk.StringVar(value=val)

  for i, (key, label, ftype) in enumerate(PATH_ROWS):
    ttk.Label(parent, text=label).grid(row=i, column=0, sticky="w", pady=3)
    ttk.Entry(parent, textvariable=v[key], width=52).grid(
      row=i, column=1, padx=6, pady=3)
    if ftype == "dir":
      cmd = lambda k=key: _browse_dir(v, k)
    elif ftype == "file_iso":
      cmd = lambda k=key: _browse_file(v, k, [("ISO", "*.iso *.ISO"), ("All", "*")])
    elif ftype == "file_exe":
      cmd = lambda k=key: _browse_file(v, k, [("All files", "*"), ("Executable", "*.exe *.EXE *.AppImage")])
    else:
      cmd = lambda k=key: _browse_file(v, k, [("JSON", "*.json"), ("All", "*")])
    ttk.Button(parent, text="Browse\u2026", command=cmd).grid(row=i, column=2, pady=3)

  return v


def _initial_dir(v: dict[str, tk.StringVar], key: str) -> str | None:
  """Return the existing directory for initialdir, or None."""
  import os
  current = v[key].get().strip()
  if not current:
    return None
  if os.path.isdir(current):
    return current
  parent = os.path.dirname(current)
  if parent and os.path.isdir(parent):
    return parent
  return None


def _browse_dir(v: dict[str, tk.StringVar], key: str):
  p = filedialog.askdirectory(initialdir=_initial_dir(v, key))
  if p:
    v[key].set(p)
    if key == "slippi_ai_root" and not v["agents_dir"].get():
      v["agents_dir"].set(detect_agents_dir(p))


def _browse_file(v: dict[str, tk.StringVar], key: str, filetypes):
  p = filedialog.askopenfilename(
      initialdir=_initial_dir(v, key), filetypes=filetypes)
  if p:
    v[key].set(p)


def save_path_fields(v: dict[str, tk.StringVar], cfg: AppConfig):
  for k, sv in v.items():
    cfg.set("paths", k, sv.get().strip())
  cfg.save()


# ── Treeview column helpers ─────────────────────────────────────────────────

def min_col_width(heading: str, *, padding: int = 24) -> int:
  """Return the minimum pixel width needed to display *heading* in a Treeview.

  Uses tkinter font metrics to measure the heading text, plus *padding* pixels
  for the sort arrow and internal cell padding.
  """
  import tkinter.font as tkfont
  # ttk Treeview headings default to TkDefaultFont.
  font = tkfont.nametofont("TkDefaultFont")
  return font.measure(heading) + padding
