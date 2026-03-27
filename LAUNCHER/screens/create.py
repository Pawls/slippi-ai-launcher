"""Create screen: training workflow with numbered steps."""

from tkinter import ttk

from LAUNCHER.screens import Screen


_PLACEHOLDER_TITLES = {
  "dataset":  "Dataset Management",
  "train_il": "Train Imitation Model",
  "rl":       "Reinforcement Learning",
  "evaluate": "Evaluate",
}


class CreateScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)
    self._add_back_button()

    outer = ttk.Frame(self, padding=20)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="Create",
              font=("Arial", 18, "bold")).pack(pady=(0, 4))
    ttk.Label(outer, text="Train your own Melee AI agent",
              foreground="gray", font=("TkDefaultFont", 10)).pack(pady=(0, 20))

    steps = [
      ("1", "Dataset Management",    "dataset",  "Prepare and manage replay datasets for training"),
      ("2", "Train Imitation Model",  "train_il", "Train a policy via behavioral cloning from replays"),
      ("3", "Reinforcement Learning", "rl",       "Refine the imitation policy with self-play RL"),
      ("4", "Evaluate",               "evaluate", "Test trained agents against opponents"),
    ]

    for num, title, screen_name, desc in steps:
      card = ttk.Frame(outer, relief="groove", borderwidth=2, padding=12)
      card.pack(fill="x", pady=6)

      header = ttk.Frame(card)
      header.pack(fill="x")

      ttk.Label(header, text=num,
                font=("Arial", 16, "bold"), foreground="#2196F3",
                width=3).pack(side="left")
      title_frame = ttk.Frame(header)
      title_frame.pack(side="left", fill="x", expand=True)
      ttk.Label(title_frame, text=title,
                font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
      ttk.Label(title_frame, text=desc,
                foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w")

      ttk.Button(header, text="Open \u2192",
                 command=lambda s=screen_name: self.navigator.navigate_to(s)
                 ).pack(side="right")


class PlaceholderScreen(Screen):
  def __init__(self, parent, navigator, cfg, screen_key: str):
    super().__init__(parent, navigator, cfg)
    self._add_back_button()

    outer = ttk.Frame(self, padding=30)
    outer.pack(expand=True)

    title = _PLACEHOLDER_TITLES.get(screen_key, screen_key.replace("_", " ").title())
    ttk.Label(outer, text=title,
              font=("Arial", 16, "bold")).pack(pady=(0, 12))
    ttk.Label(outer, text="Coming soon",
              foreground="gray", font=("TkDefaultFont", 12)).pack()
