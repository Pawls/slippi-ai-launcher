"""Screen navigation framework and screen exports."""

import tkinter as tk
from tkinter import ttk

from LAUNCHER.config import AppConfig


class Screen(tk.Frame):
  """Base class for all navigable screens."""

  def __init__(self, parent, navigator: "Navigator", cfg: AppConfig, **kw):
    super().__init__(parent, **kw)
    self.navigator = navigator
    self.cfg = cfg

  def on_enter(self):
    """Called when this screen becomes visible."""

  def on_leave(self):
    """Called when navigating away from this screen."""

  def _add_back_button(self, parent=None):
    target = parent or self
    btn = ttk.Button(target, text="\u2190 Back", command=self.navigator.go_back)
    btn.pack(anchor="w", padx=10, pady=(10, 0))
    return btn


class Navigator:
  """Manages screen stack and transitions using tkraise()."""

  def __init__(self, win: tk.Tk, cfg: AppConfig):
    self.win = win
    self.cfg = cfg
    self._stack: list[str] = []
    self._screens: dict[str, Screen] = {}
    self._current: str | None = None

    self.container = tk.Frame(win)
    self.container.pack(fill="both", expand=True)
    self.container.grid_rowconfigure(0, weight=1)
    self.container.grid_columnconfigure(0, weight=1)

  def register(self, name: str, screen: Screen):
    screen.grid(row=0, column=0, sticky="nsew")
    self._screens[name] = screen

  def navigate_to(self, name: str):
    if self._current and self._current != name:
      self._screens[self._current].on_leave()
      self._stack.append(self._current)
    self._current = name
    self._screens[name].on_enter()
    self._screens[name].tkraise()

  def go_back(self):
    if self._stack:
      prev = self._stack.pop()
      if self._current:
        self._screens[self._current].on_leave()
      self._current = prev
      self._screens[prev].on_enter()
      self._screens[prev].tkraise()

  def navigate_home(self):
    if self._current:
      self._screens[self._current].on_leave()
    self._stack.clear()
    self._current = "home"
    self._screens["home"].on_enter()
    self._screens["home"].tkraise()


# Re-export all screens for convenient import
from LAUNCHER.screens.setup import SetupScreen
from LAUNCHER.screens.home import HomeScreen
from LAUNCHER.screens.play import PlayScreen
from LAUNCHER.screens.create import CreateScreen, PlaceholderScreen
from LAUNCHER.screens.settings import SettingsScreen
from LAUNCHER.screens.dataset import DatasetScreen
from LAUNCHER.screens.train_il import TrainILScreen
from LAUNCHER.screens.train_rl import TrainRLScreen
from LAUNCHER.screens.evaluate import EvaluateScreen
from LAUNCHER.screens.history import HistoryScreen

__all__ = [
  "Screen", "Navigator",
  "SetupScreen", "HomeScreen", "PlayScreen",
  "CreateScreen", "PlaceholderScreen", "SettingsScreen",
  "DatasetScreen", "TrainILScreen", "TrainRLScreen",
  "EvaluateScreen", "HistoryScreen",
]
