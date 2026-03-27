"""Application entry point and autofill logic."""

import tkinter as tk

from LAUNCHER.config import (
  AppConfig,
  detect_root, slippi_iso, slippi_dolphin_dir,
  slippi_user_json, slippi_replays_dir, detect_agents_dir,
)
from LAUNCHER.screens import (
  Navigator,
  SetupScreen, HomeScreen, PlayScreen,
  CreateScreen, PlaceholderScreen, SettingsScreen,
)


def _autofill(cfg: AppConfig):
  root = cfg.get("paths", "slippi_ai_root") or detect_root()
  fills = [
    ("slippi_ai_root", lambda: root),
    ("iso",            slippi_iso),
    ("dolphin_dir",    slippi_dolphin_dir),
    ("user_json",      slippi_user_json),
    ("replays_dir",    slippi_replays_dir),
    ("agents_dir",     lambda: detect_agents_dir(root)),
  ]
  changed = False
  for key, fn in fills:
    if not cfg.get("paths", key):
      v = fn()
      if v:
        cfg.set("paths", key, v)
        changed = True
  if changed:
    cfg.save()


def main():
  cfg = AppConfig()
  _autofill(cfg)

  win = tk.Tk()
  win.title("Slippi AI")
  win.minsize(600, 400)

  nav = Navigator(win, cfg)

  # Register all screens
  nav.register("setup",    SetupScreen(nav.container, nav, cfg))
  nav.register("home",     HomeScreen(nav.container, nav, cfg))
  nav.register("play",     PlayScreen(nav.container, nav, cfg))
  nav.register("create",   CreateScreen(nav.container, nav, cfg))
  nav.register("settings", SettingsScreen(nav.container, nav, cfg))
  for key in ("dataset", "train_il", "rl", "evaluate"):
    nav.register(key, PlaceholderScreen(nav.container, nav, cfg, screen_key=key))

  # Decide starting screen
  if cfg.getbool("app", "setup_complete"):
    nav.navigate_to("home")
  else:
    nav.navigate_to("setup")

  win.mainloop()
