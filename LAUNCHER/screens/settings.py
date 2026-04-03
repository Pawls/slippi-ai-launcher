"""Settings screen and legacy SettingsDialog."""

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from LAUNCHER.config import AppConfig, build_path_fields, save_path_fields
from LAUNCHER.screens import Screen
from LAUNCHER.screens.setup import _check_wandb_auth


def _find_netplay_exe(dolphin_dir: str) -> str | None:
  """Locate the netplay Dolphin executable in the given directory."""
  if not dolphin_dir:
    return None
  d = Path(dolphin_dir)
  # Windows: look for the .exe
  exe = d / "Slippi Dolphin.exe"
  if exe.exists():
    return str(exe)
  # Linux / WSL: look for an AppImage
  for f in sorted(d.iterdir()):
    if f.suffix == ".AppImage" and f.is_file():
      return str(f)
  return None


def _launch_dolphin(exe_path: str) -> None:
  """Launch a Dolphin executable, handling WSL→Windows if needed."""
  if "microsoft" in os.uname().release.lower() and exe_path.endswith(".exe"):
    # WSL: convert to Windows path and launch via cmd.exe
    win_path = subprocess.check_output(
      ["wslpath", "-w", exe_path], text=True).strip()
    subprocess.Popen(["cmd.exe", "/C", "start", "", win_path])
  else:
    subprocess.Popen([exe_path])


class SettingsScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)
    self._add_back_button()

    outer = ttk.Frame(self, padding=20)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="Settings",
              font=("Arial", 18, "bold")).pack(pady=(0, 16))

    # Paths section
    ttk.Label(outer, text="Paths",
              font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 4))
    ttk.Label(outer, text="Auto-filled from Slippi Launcher \u2014 only edit if needed.",
              foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 8))

    paths_frame = ttk.Frame(outer)
    paths_frame.pack(fill="x", pady=(0, 16))
    self._path_vars: dict[str, tk.StringVar] = {}

    ttk.Label(
      outer,
      text="Env overrides: SLIPPI_AI_ROOT  MELEE_ISO  SLIPPI_DOLPHIN  "
           "SLIPPI_USER_JSON  SLIPPI_AGENTS",
      foreground="gray", font=("TkDefaultFont", 8), wraplength=500,
    ).pack(anchor="w", pady=(0, 12))

    # Wandb section
    wandb_frame = ttk.LabelFrame(outer, text="Weights & Biases", padding=8)
    wandb_frame.pack(fill="x", pady=(0, 16))

    self._wandb_status = ttk.Label(wandb_frame, text="", foreground="gray")
    self._wandb_status.pack(anchor="w")

    key_frame = ttk.Frame(wandb_frame)
    key_frame.pack(fill="x", pady=(8, 0))
    ttk.Label(key_frame, text="API Key:").pack(side="left")
    self._wandb_key_var = tk.StringVar()
    self._wandb_key_entry = ttk.Entry(key_frame, textvariable=self._wandb_key_var,
                                       width=40, show="*")
    self._wandb_key_entry.pack(side="left", padx=(8, 8))
    self._wandb_connect_btn = ttk.Button(key_frame, text="Connect",
                                          command=self._connect_wandb)
    self._wandb_connect_btn.pack(side="left")

    # Dolphin section
    dolphin_frame = ttk.LabelFrame(outer, text="Dolphin Configuration", padding=8)
    dolphin_frame.pack(fill="x", pady=(0, 16))

    ttk.Label(dolphin_frame,
              text="Open Dolphin to configure graphics, controls, etc.",
              foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 6))

    dolphin_btn_frame = ttk.Frame(dolphin_frame)
    dolphin_btn_frame.pack(anchor="w")
    ttk.Button(dolphin_btn_frame, text="Open Netplay Dolphin",
               command=self._open_netplay_dolphin).pack(side="left", padx=(0, 8))
    ttk.Button(dolphin_btn_frame, text="Open Headless Dolphin",
               command=self._open_headless_dolphin).pack(side="left")

    # Buttons
    btn_frame = ttk.Frame(outer)
    btn_frame.pack(pady=(8, 0))
    ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=6)
    ttk.Button(btn_frame, text="Re-run Setup",
               command=lambda: navigator.navigate_to("setup")).pack(side="left", padx=6)

    self._paths_frame = paths_frame

  def on_enter(self):
    # Rebuild path fields each time to reflect current config
    for w in self._paths_frame.winfo_children():
      w.destroy()
    self._path_vars = build_path_fields(self._paths_frame, self.cfg)

    # Check wandb
    self._wandb_key_entry.config(state="normal")
    self._wandb_connect_btn.config(state="normal")
    threading.Thread(target=self._check_wandb, daemon=True).start()

  def _check_wandb(self):
    ok, user = _check_wandb_auth()
    self.after(0, lambda: self._update_wandb_status(ok, user))

  def _update_wandb_status(self, ok: bool, user: str):
    if ok:
      self._wandb_status.config(
        text=f"\u2713 Connected as: {user}", foreground="green")
      self._wandb_key_entry.config(state="disabled")
      self._wandb_connect_btn.config(state="disabled")
    else:
      self._wandb_status.config(
        text="Not connected", foreground="orange")

  def _connect_wandb(self):
    key = self._wandb_key_var.get().strip()
    if not key:
      return
    self._wandb_connect_btn.config(state="disabled")
    self._wandb_status.config(text="Connecting...", foreground="gray")

    def do_login():
      try:
        import wandb
        wandb.login(key=key, relogin=True)
        ok, user = _check_wandb_auth()
        self.after(0, lambda: self._update_wandb_status(ok, user))
      except Exception as e:
        self.after(0, lambda: self._wandb_status.config(
          text=f"Failed: {e}", foreground="red"))
        self.after(0, lambda: self._wandb_connect_btn.config(state="normal"))

    threading.Thread(target=do_login, daemon=True).start()

  def _open_netplay_dolphin(self):
    dolphin_dir = self._path_vars.get("dolphin_dir")
    d = dolphin_dir.get().strip() if dolphin_dir else ""
    exe = _find_netplay_exe(d)
    if not exe:
      messagebox.showwarning(
        "Dolphin Not Found",
        "Could not find the netplay Dolphin executable.\n"
        "Make sure the Slippi Dolphin folder path is set correctly.")
      return
    try:
      _launch_dolphin(exe)
    except Exception as e:
      messagebox.showerror("Launch Failed", str(e))

  def _open_headless_dolphin(self):
    headless_var = self._path_vars.get("dolphin_headless")
    exe = headless_var.get().strip() if headless_var else ""
    if not exe or not Path(exe).exists():
      messagebox.showwarning(
        "Dolphin Not Found",
        "Headless Dolphin path is not set or the file does not exist.\n"
        "Set the path in the Paths section above.")
      return
    try:
      _launch_dolphin(exe)
    except Exception as e:
      messagebox.showerror("Launch Failed", str(e))

  def _save(self):
    save_path_fields(self._path_vars, self.cfg)
    self.navigator.go_back()


class SettingsDialog(tk.Toplevel):
  """Legacy modal settings dialog (kept for backward compatibility)."""

  def __init__(self, parent, cfg: AppConfig):
    super().__init__(parent)
    self.title("Settings \u2014 Paths")
    self.resizable(False, False)
    self.grab_set()
    self._cfg = cfg

    f = ttk.Frame(self, padding=12)
    f.pack(fill="both", expand=True)

    ttk.Label(f, text="Paths auto-filled from Slippi Launcher \u2014 only edit if needed.",
              foreground="gray", font=("TkDefaultFont", 8)).grid(
      row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

    fields_frame = ttk.Frame(f)
    fields_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    self._v = build_path_fields(fields_frame, cfg)

    ttk.Label(
      f,
      text="Env overrides: SLIPPI_AI_ROOT  MELEE_ISO  SLIPPI_DOLPHIN  "
           "SLIPPI_USER_JSON  SLIPPI_AGENTS",
      foreground="gray", font=("TkDefaultFont", 8), wraplength=500,
    ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

    bf = ttk.Frame(f)
    bf.grid(row=3, column=0, columnspan=3, pady=(12, 0))
    ttk.Button(bf, text="Save",   command=self._save).pack(side="left", padx=6)
    ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    self.transient(parent)
    self.wait_window()

  def _save(self):
    save_path_fields(self._v, self._cfg)
    self.destroy()
