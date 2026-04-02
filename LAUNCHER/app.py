"""Application entry point and autofill logic."""

import tkinter as tk

from LAUNCHER.config import (
  AppConfig,
  detect_root, slippi_iso, slippi_dolphin_dir,
  slippi_user_json, slippi_replays_dir, detect_agents_dir,
)
from LAUNCHER.agent_store import AgentStore
from LAUNCHER.match_store import MatchStore
from LAUNCHER.replay_store import ReplayStore
from LAUNCHER.screens import (
  Navigator,
  SetupScreen, HomeScreen, PlayScreen,
  CreateScreen, SettingsScreen,
  DatasetScreen, TrainILScreen, TrainRLScreen,
  EvaluateScreen, HistoryScreen, AgentLibraryScreen,
  ReplayBrowserScreen,
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
  replay_store = ReplayStore()
  agent_store = AgentStore(source="agents")
  experiment_store = AgentStore(
      path="experiment_library.json", source="experiments")
  match_store = MatchStore()

  win = tk.Tk()
  win.title("Slippi AI")
  win.minsize(600, 400)

  nav = Navigator(win, cfg)

  # Register all screens
  nav.register("setup",    SetupScreen(nav.container, nav, cfg))
  nav.register("home",     HomeScreen(nav.container, nav, cfg))
  nav.register("play",     PlayScreen(nav.container, nav, cfg, match_store))
  nav.register("create",   CreateScreen(nav.container, nav, cfg))
  nav.register("settings", SettingsScreen(nav.container, nav, cfg))
  nav.register("dataset", DatasetScreen(nav.container, nav, cfg))
  nav.register("train_il", TrainILScreen(nav.container, nav, cfg))
  nav.register("rl", TrainRLScreen(nav.container, nav, cfg))
  nav.register("evaluate", EvaluateScreen(nav.container, nav, cfg))
  nav.register("history",  HistoryScreen(nav.container, nav, cfg, match_store))
  nav.register("agents",   AgentLibraryScreen(nav.container, nav, cfg, agent_store, experiment_store, match_store))
  nav.register("replays",  ReplayBrowserScreen(nav.container, nav, cfg, replay_store))

  # Decide starting screen
  if cfg.getbool("app", "setup_complete"):
    nav.navigate_to("home")
  else:
    nav.navigate_to("setup")

  win.mainloop()
