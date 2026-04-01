"""Home screen with Play, Create, and Settings buttons."""

import tkinter as tk
from tkinter import ttk

from LAUNCHER.screens import Screen


class HomeScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)

    outer = ttk.Frame(self, padding=30)
    outer.pack(expand=True)

    ttk.Label(outer, text="Slippi AI",
              font=("Arial", 22, "bold")).pack(pady=(0, 4))
    ttk.Label(outer, text="Melee Bot Launcher",
              foreground="gray", font=("TkDefaultFont", 10)).pack(pady=(0, 30))

    btn_style = {"font": ("Arial", 14, "bold"), "width": 22, "height": 2,
                 "cursor": "hand2", "relief": "groove", "borderwidth": 2}

    tk.Button(outer, text="\u25b6  Play", bg="#4CAF50", fg="white",
              command=lambda: navigator.navigate_to("play"),
              **btn_style).pack(pady=8)

    tk.Button(outer, text="\u2692  Create", bg="#2196F3", fg="white",
              command=lambda: navigator.navigate_to("create"),
              **btn_style).pack(pady=8)

    tk.Button(outer, text="\U0001F916  Agents", bg="#9C27B0", fg="white",
              command=lambda: navigator.navigate_to("agents"),
              **btn_style).pack(pady=8)

    tk.Button(outer, text="\u2630  History", bg="#FF9800", fg="white",
              command=lambda: navigator.navigate_to("history"),
              **btn_style).pack(pady=8)

    tk.Button(outer, text="\u2699  Settings", bg="#757575", fg="white",
              command=lambda: navigator.navigate_to("settings"),
              **btn_style).pack(pady=8)
