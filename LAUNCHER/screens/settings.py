"""Settings screen and legacy SettingsDialog."""

import threading
import tkinter as tk
from tkinter import ttk

from LAUNCHER.config import AppConfig, build_path_fields, save_path_fields
from LAUNCHER.screens import Screen
from LAUNCHER.screens.setup import _check_wandb_auth


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
