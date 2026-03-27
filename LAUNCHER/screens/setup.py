"""First-launch setup screen with path verification and wandb authentication."""

import threading
import tkinter as tk
from tkinter import ttk

from LAUNCHER.config import AppConfig, build_path_fields, save_path_fields
from LAUNCHER.screens import Screen


def _check_wandb_auth() -> tuple[bool, str]:
  """Check if wandb is authenticated. Returns (is_authenticated, username)."""
  try:
    import wandb
    api = wandb.Api()
    viewer = api.viewer
    return True, viewer
  except Exception:
    return False, ""


class SetupScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)

    outer = ttk.Frame(self, padding=20)
    outer.pack(fill="both", expand=True)

    # Title
    ttk.Label(outer, text="Welcome to Slippi AI",
              font=("Arial", 18, "bold")).pack(pady=(0, 4))
    ttk.Label(outer, text="Let's verify your setup",
              foreground="gray", font=("TkDefaultFont", 10)).pack(pady=(0, 16))

    # Path fields
    ttk.Label(outer, text="Paths",
              font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 4))
    ttk.Label(outer, text="Auto-filled from Slippi Launcher \u2014 only edit if needed.",
              foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 8))

    paths_frame = ttk.Frame(outer)
    paths_frame.pack(fill="x", pady=(0, 16))
    self._path_vars = build_path_fields(paths_frame, cfg)

    # Wandb section
    wandb_frame = ttk.LabelFrame(outer, text="Weights & Biases (optional)", padding=8)
    wandb_frame.pack(fill="x", pady=(0, 16))

    self._wandb_status = ttk.Label(wandb_frame, text="Checking...", foreground="gray")
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

    ttk.Label(wandb_frame,
              text="Get your API key at wandb.ai/authorize",
              foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(4, 0))

    # Continue button
    btn_frame = ttk.Frame(outer)
    btn_frame.pack(pady=(8, 0))
    tk.Button(btn_frame, text="Continue", bg="#4CAF50", fg="white",
              font=("Arial", 12, "bold"), padx=20, pady=6,
              cursor="hand2", command=self._continue).pack()

  def on_enter(self):
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
        text="Not connected \u2014 enter API key or skip",
        foreground="orange")

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

  def _continue(self):
    save_path_fields(self._path_vars, self.cfg)
    self.cfg.set("app", "setup_complete", "True")
    self.cfg.save()
    self.navigator.navigate_home()
