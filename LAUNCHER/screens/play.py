"""Play screen: wraps the SlippiLauncher for local and netplay modes."""

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from LAUNCHER.config import (
  AppConfig, CHARACTERS, STAGES,
  _MODEL_INFO_CACHE,
  list_agents, detect_character,
  extract_delay_from_filename, extract_characters_from_filename,
  read_model_delay, read_allowed_characters, read_names_list,
  find_script, slippi_gfx_backend,
  gecko_codes_path, load_gecko_codes_text, save_gecko_codes_text,
)
from LAUNCHER.screens import Screen


# ──────────────────────────────────────────────────────────────────────────────
# ToolTip
# ──────────────────────────────────────────────────────────────────────────────

class ToolTip:
  """Creates a hover tooltip for a given Tkinter widget."""
  def __init__(self, widget, text):
    self.widget = widget
    self.text = text
    self.tipwindow = None
    self.id = None
    self.widget.bind("<Enter>", self.enter)
    self.widget.bind("<Leave>", self.leave)

  def enter(self, event=None):
    self.id = self.widget.after(500, self.showtip)

  def leave(self, event=None):
    if self.id:
      self.widget.after_cancel(self.id)
      self.id = None
    self.hidetip()

  def showtip(self, event=None):
    x, y, _, _ = self.widget.bbox("insert")
    x += self.widget.winfo_rootx() + 25
    y += self.widget.winfo_rooty() + 20
    self.tipwindow = tw = tk.Toplevel(self.widget)
    tw.wm_overrideredirect(True)
    tw.wm_geometry(f"+{x}+{y}")
    label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                     background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                     font=("TkDefaultFont", 8))
    label.pack(ipadx=1)

  def hidetip(self):
    if self.tipwindow:
      self.tipwindow.destroy()
      self.tipwindow = None


# ──────────────────────────────────────────────────────────────────────────────
# Gecko Codes dialog
# ──────────────────────────────────────────────────────────────────────────────

class GeckoCodesDialog(tk.Toplevel):
  PLACEHOLDER = (
      "$No Music [Dan Salvato]\n"
      "04B664F0 00000000\n"
      "\n"
      "$Another Code [Author]\n"
      "XXXXXXXX YYYYYYYY\n"
      "XXXXXXXX YYYYYYYY"
  )

  def __init__(self, parent):
    super().__init__(parent)
    self.title("Custom Gecko Codes")
    self.resizable(True, True)
    self.grab_set()

    f = ttk.Frame(self, padding=12)
    f.pack(fill="both", expand=True)

    ttk.Label(
        f,
        text="Enter gecko codes in INI format (same as GALE01r2.ini).\n"
             "These are injected into Dolphin before each launch.",
        wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 6))

    self._text = tk.Text(f, width=56, height=16, font=("Consolas", 10))
    self._text.pack(fill="both", expand=True)

    existing = load_gecko_codes_text()
    if existing:
      self._text.insert("1.0", existing)
    else:
      self._text.insert("1.0", self.PLACEHOLDER)
      self._text.config(foreground="gray")

    self._text.bind("<FocusIn>", self._on_focus)

    bf = ttk.Frame(f)
    bf.pack(pady=(10, 0))
    ttk.Button(bf, text="Save", command=self._save).pack(side="left", padx=6)
    ttk.Button(bf, text="Clear", command=self._clear).pack(side="left", padx=6)
    ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    self.transient(parent)
    self.wait_window()

  def _on_focus(self, _=None):
    if self._text.cget("foreground") == "gray":
      self._text.delete("1.0", "end")
      self._text.config(foreground="black")

  def _clear(self):
    self._text.delete("1.0", "end")
    self._text.config(foreground="black")

  def _save(self):
    content = self._text.get("1.0", "end")
    if self._text.cget("foreground") == "gray":
      content = ""
    save_gecko_codes_text(content)
    self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Agent selector widget
# ──────────────────────────────────────────────────────────────────────────────

class AgentSelector(ttk.LabelFrame):
  """Reusable agent + character picker used by both mode panels."""

  def __init__(self, parent, cfg: AppConfig, section: str, **kw):
    super().__init__(parent, text="Configuration", padding=8, **kw)
    self._cfg = cfg
    self._section = section

    # Agent Selection
    ttk.Label(self, text="Select AI Agent (>= 2MB):", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    self._agent_var = tk.StringVar(value=cfg.get(section, "last_agent"))
    self._agent_combo = ttk.Combobox(self, textvariable=self._agent_var, width=54, state="readonly")
    self._agent_combo.grid(row=1, column=0, columnspan=2, sticky="ew")
    self._agent_combo.bind("<<ComboboxSelected>>", self._on_selected)
    ttk.Button(self, text="\u21bb", width=3, command=self.refresh).grid(row=1, column=2, padx=(4, 0))

    # Delay Calculation Hint
    self._delay_hint = ttk.Label(self, text="AI Delay: Select a file...", foreground="blue", font=("TkDefaultFont", 9))
    self._delay_hint.grid(row=2, column=0, columnspan=3, pady=(4, 8))

    # Name Override with "None" checkbox
    ttk.Label(self, text="Name:").grid(row=3, column=0, sticky="w", pady=(4, 0))
    name_frame = ttk.Frame(self)
    name_frame.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(4, 0))
    self._name_none_var = tk.BooleanVar(
      value=cfg.getbool(section, "name_none", False)
    )
    self._name_none_cb = ttk.Checkbutton(
      name_frame, text="None", variable=self._name_none_var,
      command=self._on_name_none_toggle
    )
    self._name_none_cb.pack(side="left")
    self._name_var = tk.StringVar(value=cfg.get(section, "name", ""))
    self._name_combo = ttk.Combobox(
      name_frame,
      textvariable=self._name_var,
      values=list(),
      width=20,
      height=20,
    )
    self._name_combo.pack(side="left", padx=(8, 0))
    self._on_name_none_toggle()

    # Character Override
    ttk.Label(self, text="Character:").grid(row=4, column=0, sticky="w", pady=(4, 0))
    self._char_var = tk.StringVar(value=cfg.get(section, "character", "fox"))
    self._char_combo = ttk.Combobox(
      self,
      textvariable=self._char_var,
      values=CHARACTERS,
      width=20,
      height=20,
    )
    self._char_combo.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

    # Input Delay Override
    if section == "netplay":
      delay_label_text = "Bot Input Delay (Frames):"
      tooltip_text = "The number of frames of input delay for the bot. Match this to the AI model's trained delay."
    else:
      delay_label_text = "Human Input Delay (Frames):"
      tooltip_text = "A 2-frame delay is recommended to simulate standard Slippi Online netplay latency for the human player."
    delay_lbl = ttk.Label(self, text=delay_label_text)
    delay_lbl.grid(row=5, column=0, sticky="w", pady=(8, 0))
    self._delay_var = tk.IntVar(value=cfg.getint(section, "delay", 2))
    delay_spin = ttk.Spinbox(self, textvariable=self._delay_var, from_=0, to=30, width=6)
    delay_spin.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

    ToolTip(delay_lbl, tooltip_text)
    ToolTip(delay_spin, tooltip_text)

    # Player Slot (Local Only)
    if section == "local":
      self._player_slot_var = tk.IntVar(value=cfg.getint(section, "player_slot", 1))
      ttk.Label(self, text="Choose Your Player Slot:", font=("TkDefaultFont", 9, "bold")).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 4))
      ttk.Radiobutton(self, text="Player 1 (Bot is P2)", variable=self._player_slot_var, value=1).grid(row=7, column=0, columnspan=2, sticky="w", padx=16, pady=2)
      ttk.Radiobutton(self, text="Player 2 (Bot is P1)", variable=self._player_slot_var, value=2).grid(row=8, column=0, columnspan=2, sticky="w", padx=16, pady=2)
    else:
      self._player_slot_var = None

    self.refresh()

  def refresh(self):
    agents = list_agents(self._cfg.get("paths", "agents_dir"))
    self._agent_combo["values"] = agents
    if self._agent_var.get() not in agents and agents:
      self._agent_var.set(agents[0])
      self._on_selected()
    elif self._agent_var.get() in agents:
      self._on_selected()

  def _on_selected(self, _=None):
    agent = self._agent_var.get()
    if not agent:
      return

    self._char_var.set(detect_character(agent))
    if not self._name_none_var.get():
      self._name_var.set("Loading...")
    self._delay_hint.config(text="AI Delay: Calculating...", foreground="orange")

    agents_dir = self._cfg.get("paths", "agents_dir")
    full_path = str(Path(agents_dir) / agent)

    if full_path in _MODEL_INFO_CACHE:
      delay, names, chars = _MODEL_INFO_CACHE[full_path]
      self._update_model_info(delay, names, chars)
      return

    def fetch_model_info():
      delay = extract_delay_from_filename(agent)
      chars = extract_characters_from_filename(agent)

      if delay is None:
        delay = read_model_delay(full_path)

      if chars is None:
        chars = read_allowed_characters(full_path)

      names = read_names_list(full_path)

      _MODEL_INFO_CACHE[full_path] = (delay, names, chars)
      self.after(0, lambda: self._update_model_info(delay, names, chars))

    threading.Thread(target=fetch_model_info, daemon=True).start()

  def _on_name_none_toggle(self):
    if self._name_none_var.get():
      self._name_combo.config(state="disabled")
    else:
      self._name_combo.config(state="readonly")

  @property
  def agent(self) -> str:
    return self._agent_var.get()

  @property
  def name(self) -> str:
    if self._name_none_var.get():
      return ""
    return self._name_var.get()

  @property
  def character(self) -> str:
    return self._char_var.get()

  @property
  def delay(self) -> int:
    return self._delay_var.get()

  def save_prefs(self):
    self._cfg.set(self._section, "last_agent", self.agent)
    self._cfg.set(self._section, "name",  self._name_var.get())
    self._cfg.set(self._section, "name_none", str(self._name_none_var.get()))
    self._cfg.set(self._section, "character",  self.character)
    self._cfg.set(self._section, "delay",      str(self.delay))
    if self._player_slot_var:
      self._cfg.set(self._section, "player_slot", str(self._player_slot_var.get()))

  def _update_model_info(self, delay, names, chars):
    if delay is not None:
      ms_delay = int(round(delay * 1000 / 60))
      self._delay_hint.config(
        text=f"AI trained with {delay} frames ({ms_delay}ms) of delay",
        foreground="blue"
      )
    else:
      self._delay_hint.config(text="AI Delay: Unknown", foreground="red")

    if names:
      self._name_combo["values"] = names
      if not self._name_none_var.get():
        current = self._name_var.get()
        if current not in names or current == "Loading...":
          self._name_var.set(names[0])

    if chars:
      self._char_combo["values"] = chars
      if self._char_var.get() not in chars:
        self._char_var.set(chars[0])


# ──────────────────────────────────────────────────────────────────────────────
# Play screen
# ──────────────────────────────────────────────────────────────────────────────

class PlayScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)
    self._add_back_button()
    self.launcher = SlippiLauncher(self, navigator.win, cfg)

  def on_enter(self):
    self.launcher._local_agent.refresh()
    self.launcher._netplay_agent.refresh()


# ──────────────────────────────────────────────────────────────────────────────
# SlippiLauncher (Play screen content)
# ──────────────────────────────────────────────────────────────────────────────

class SlippiLauncher:

  def __init__(self, parent: tk.Frame, win: tk.Tk, cfg: AppConfig):
    self._parent = parent
    self._win = win
    self._cfg = cfg
    self._proc: subprocess.Popen | None = None
    self._build()

  def _build(self):
    outer = ttk.Frame(self._parent, padding=10)
    outer.pack(fill="both", expand=True)

    # Mode selector
    mode_frame = ttk.LabelFrame(outer, text="Mode", padding=6)
    mode_frame.pack(fill="x", pady=(0, 8))

    self._mode_var = tk.StringVar(value=self._cfg.get("options", "last_mode", "local"))
    ttk.Radiobutton(mode_frame, text="Local Play",
                    variable=self._mode_var, value="local",
                    command=self._on_mode_change).pack(side="left", padx=12)
    ttk.Radiobutton(mode_frame, text="Netplay",
                    variable=self._mode_var, value="netplay",
                    command=self._on_mode_change).pack(side="left", padx=12)

    # Agent selectors
    self._local_agent = AgentSelector(outer, self._cfg, "local")
    self._netplay_agent = AgentSelector(outer, self._cfg, "netplay")

    # Netplay connection panel
    self._conn_frame = ttk.LabelFrame(outer, text="Connection", padding=8)
    ttk.Label(self._conn_frame,
              text="Opponent connect code:").grid(row=0, column=0, sticky="w")
    self._code_var = tk.StringVar(value=self._cfg.get("netplay", "connect_code"))
    self._code_history = self._load_code_history()
    self._code_combo = ttk.Combobox(
      self._conn_frame, textvariable=self._code_var,
      values=self._code_history, width=14)
    self._code_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
    self._code_var.trace_add("write", self._uppercase_code)
    self._code_combo.bind("<KeyRelease>", self._autocomplete_code)

    ttk.Label(self._conn_frame,
              text="Force netplay port:").grid(row=1, column=0, sticky="w", pady=(4, 0))
    self._netplay_port_var = tk.StringVar(
      value=self._cfg.get("netplay", "netplay_port", ""))
    ttk.Entry(self._conn_frame, textvariable=self._netplay_port_var,
              width=8).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

    ttk.Label(self._conn_frame,
              text="Force LAN IP:").grid(row=2, column=0, sticky="w", pady=(4, 0))
    self._lan_ip_var = tk.StringVar(
      value=self._cfg.get("netplay", "lan_ip", ""))
    ttk.Entry(self._conn_frame, textvariable=self._lan_ip_var,
              width=16).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

    # Options panel
    opts = ttk.LabelFrame(outer, text="Options", padding=8)
    opts.pack(fill="x", pady=(0, 6))

    self._fullscreen_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "fullscreen", True))
    ttk.Checkbutton(opts, text="Fullscreen",
                    variable=self._fullscreen_var).grid(row=0, column=0, sticky="w")

    self._infinite_time_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "infinite_time", False))
    ttk.Checkbutton(opts, text="Infinite time",
                    variable=self._infinite_time_var).grid(
      row=0, column=1, sticky="w", padx=(16, 0))

    self._save_replays_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "save_replays"))
    self._save_replays_cb = ttk.Checkbutton(
      opts, text="Save replays", variable=self._save_replays_var)
    self._save_replays_cb.grid(row=0, column=2, sticky="w", padx=(16, 0))

    self._disable_audio_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "disable_audio"))
    self._disable_audio_cb = ttk.Checkbutton(
      opts, text="Disable audio", variable=self._disable_audio_var)
    self._disable_audio_cb.grid(row=0, column=3, sticky="w", padx=(16, 0))

    self._stage_lbl = ttk.Label(opts, text="Stage")
    self._stage_lbl.grid(row=1, column=0, sticky="w", pady=(8, 0))
    self._stage_var = tk.StringVar(
      value=self._cfg.get("options", "stage", "RANDOM_STAGE"))
    self._stage_combo = ttk.Combobox(opts, textvariable=self._stage_var,
                                     values=STAGES, width=22, state="readonly")
    self._stage_combo.grid(row=1, column=1, columnspan=2, sticky="w",
                           padx=(8, 0), pady=(8, 0))

    self._temp_lbl = ttk.Label(opts, text="Sample temperature")
    self._temp_lbl.grid(row=2, column=0, sticky="w", pady=(8, 0))
    temp_row = ttk.Frame(opts)
    temp_row.grid(row=2, column=1, columnspan=3, sticky="w",
                  padx=(8, 0), pady=(8, 0))
    self._temp_var = tk.DoubleVar(
      value=self._cfg.getfloat("options", "sample_temperature", 1.0))
    self._temp_spin = ttk.Spinbox(temp_row, textvariable=self._temp_var,
                                   from_=0.1, to=3.0, increment=0.1,
                                   width=6, format="%.1f")
    self._temp_spin.pack(side="left")
    ttk.Label(temp_row, text="(1.0 = normal,  >1 = more random)",
              foreground="gray", font=("TkDefaultFont", 8)).pack(
      side="left", padx=(6, 0))

    self._gfx_lbl = ttk.Label(opts, text="GFX Backend:")
    self._gfx_lbl.grid(row=3, column=0, sticky="w", pady=(8, 0))
    detected = self._cfg.get("options", "gfx_backend") or slippi_gfx_backend()
    self._gfx_var = tk.StringVar(value=detected)
    self._gfx_combo = ttk.Combobox(
      opts, textvariable=self._gfx_var, width=22)
    self._gfx_combo.grid(row=3, column=1, columnspan=2, sticky="w",
                         padx=(8, 0), pady=(8, 0))
    gfx_hint = ttk.Label(opts, text="(auto-detected from Dolphin)",
                          foreground="gray", font=("TkDefaultFont", 8))
    gfx_hint.grid(row=3, column=3, sticky="w", padx=(6, 0), pady=(8, 0))

    self._copy_home_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "copy_home_directory", True))
    self._copy_home_cb = ttk.Checkbutton(
      opts, text="Copy home dir  (use your Dolphin controller config)",
      variable=self._copy_home_var)
    self._copy_home_cb.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))

    self._use_gpu_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "use_gpu", False))
    self._use_gpu_cb = ttk.Checkbutton(
      opts, text="Use GPU for AI inference  (reduces CPU contention)",
      variable=self._use_gpu_var)
    self._use_gpu_cb.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

    gecko_row = ttk.Frame(opts)
    gecko_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
    ttk.Button(gecko_row, text="Gecko Codes\u2026",
               command=self._open_gecko_codes).pack(side="left")
    self._gecko_hint = ttk.Label(gecko_row, text="", foreground="gray",
                                  font=("TkDefaultFont", 8))
    self._gecko_hint.pack(side="left", padx=(8, 0))
    self._update_gecko_hint()

    # Status + launch button
    bottom = ttk.Frame(outer)
    bottom.pack(fill="x", pady=(8, 0))

    self._status_var = tk.StringVar(value="")
    ttk.Label(bottom, textvariable=self._status_var,
              foreground="gray", font=("TkDefaultFont", 8)).pack(side="left", pady=(10,0))

    btn_right = ttk.Frame(bottom)
    btn_right.pack(side="right")

    self._launch_btn = tk.Button(btn_right, text="Launch eval_two.py",
                                 bg="green", fg="white", font=("Arial", 11, "bold"),
                                 padx=10, pady=4, cursor="hand2", command=self._launch)
    self._launch_btn.pack(side="left", padx=4)

    self._on_mode_change()

  # ── Mode switching ──────────────────────────────────────────────────────

  def _on_mode_change(self):
    mode = self._mode_var.get()

    self._local_agent.pack_forget()
    self._netplay_agent.pack_forget()
    self._conn_frame.pack_forget()

    if mode == "local":
      self._local_agent.pack(fill="x", pady=(0, 6))
      for w in (self._save_replays_cb, self._temp_lbl):
        w.grid_remove()
      for w in (self._disable_audio_cb,
                self._stage_lbl, self._stage_combo):
        w.grid()
      self._copy_home_cb.grid()
      self._use_gpu_cb.grid()
      self._gfx_lbl.grid()
      self._gfx_combo.grid()
      self._launch_btn.config(text="Launch eval_two.py")
    else:
      self._netplay_agent.pack(fill="x", pady=(0, 6))
      self._conn_frame.pack(fill="x", pady=(0, 6))
      for w in (self._save_replays_cb, self._disable_audio_cb,
                self._stage_lbl, self._stage_combo,
                self._temp_lbl):
        w.grid()
      self._copy_home_cb.grid_remove()
      self._use_gpu_cb.grid()
      self._gfx_lbl.grid_remove()
      self._gfx_combo.grid_remove()
      self._launch_btn.config(text="Launch netplay.py")

  # ── Callbacks ───────────────────────────────────────────────────────────

  def _uppercase_code(self, *_):
    v = self._code_var.get()
    u = v.upper()
    if v != u:
      self._code_var.set(u)

  def _load_code_history(self) -> list[str]:
    raw = self._cfg.get("netplay", "connect_code_history", "")
    if not raw:
      current = self._cfg.get("netplay", "connect_code", "")
      return [current] if current else []
    return [c.strip() for c in raw.split(",") if c.strip()]

  def _save_code_to_history(self, code: str):
    code = code.strip().upper()
    if not code:
      return
    history = self._code_history[:]
    if code in history:
      history.remove(code)
    history.insert(0, code)
    history = history[:20]
    self._cfg.set("netplay", "connect_code_history", ",".join(history))
    self._code_combo["values"] = history
    self._code_history = history

  def _autocomplete_code(self, event=None):
    typed = self._code_var.get().upper()
    if not typed:
      self._code_combo["values"] = self._code_history
      return
    filtered = [c for c in self._code_history if c.startswith(typed)]
    self._code_combo["values"] = filtered if filtered else self._code_history

  def _open_gecko_codes(self):
    GeckoCodesDialog(self._win)
    self._update_gecko_hint()

  def _update_gecko_hint(self):
    text = load_gecko_codes_text().strip()
    if text:
      count = sum(1 for line in text.splitlines() if line.strip().startswith('$'))
      self._gecko_hint.config(text=f"{count} code(s) configured")
    else:
      self._gecko_hint.config(text="No custom codes")

  # ── Launch ──────────────────────────────────────────────────────────────

  def _validate(self) -> bool:
    mode = self._mode_var.get()
    agent = (self._local_agent if mode == "local" else self._netplay_agent).agent
    if not agent:
      messagebox.showerror("Error", "Please select an agent.")
      return False
    if mode == "netplay" and not self._code_var.get().strip():
      messagebox.showerror("Error", "Please enter the opponent's connect code.")
      return False
    required = [("slippi_ai_root", "Slippi-AI root"), ("iso", "Melee ISO")]
    if mode == "netplay":
      required += [("dolphin_dir",  "Slippi Dolphin folder"),
                   ("user_json",    "Slippi Online user.json")]
    missing = [lbl for key, lbl in required if not self._cfg.get("paths", key)]
    if missing:
      messagebox.showerror(
        "Missing paths",
        "Please configure in Settings:\n\n" +
        "\n".join(f"  \u2022 {m}" for m in missing))
      return False
    return True

  def _save_prefs(self):
    c = self._cfg
    mode = self._mode_var.get()
    c.set("options", "last_mode",          mode)
    c.set("options", "fullscreen",         str(self._fullscreen_var.get()))
    c.set("options", "infinite_time",      str(self._infinite_time_var.get()))
    c.set("options", "save_replays",       str(self._save_replays_var.get()))
    c.set("options", "disable_audio",      str(self._disable_audio_var.get()))
    c.set("options", "stage",              self._stage_var.get())
    c.set("options", "sample_temperature", f"{self._temp_var.get():.1f}")
    c.set("options", "copy_home_directory", str(self._copy_home_var.get()))
    c.set("options", "use_gpu",             str(self._use_gpu_var.get()))
    c.set("options", "gfx_backend",         self._gfx_var.get())

    if mode == "local":
      self._local_agent.save_prefs()
    else:
      self._netplay_agent.save_prefs()
      c.set("netplay", "connect_code", self._code_var.get())
      c.set("netplay", "netplay_port", self._netplay_port_var.get().strip())
      c.set("netplay", "lan_ip", self._lan_ip_var.get().strip())
      self._save_code_to_history(self._code_var.get())
    c.save()

  def _launch(self):
    if self._proc and self._proc.poll() is None:
      self._kill_process_tree(self._proc)
      self._proc = None

    if not self._validate():
      return
    self._save_prefs()

    mode   = self._mode_var.get()
    cfg    = self._cfg
    root   = cfg.get("paths", "slippi_ai_root")
    agents = cfg.get("paths", "agents_dir")

    if mode == "local":
      agent_sel = self._local_agent
      script = find_script(root, "scripts/eval_two.py", "eval_two.py")
      if not script:
        messagebox.showerror("Error", "Cannot find eval_two.py in Slippi-AI root.")
        return

      agent_pkl = str(Path(agents) / agent_sel.agent)
      player_slot = agent_sel._player_slot_var.get()
      ai_port = "p2" if player_slot == 1 else "p1"
      human_port = "p1" if player_slot == 1 else "p2"

      cmd = [
        sys.executable, script,
        f"--dolphin.path={cfg.get('paths', 'dolphin_dir')}",
        f"--dolphin.iso={cfg.get('paths', 'iso')}",
        f"--dolphin.online_delay={agent_sel.delay}",
        f"--dolphin.stage={self._stage_var.get()}",
        f"--{ai_port}.ai.sample_temperature={self._temp_var.get():.1f}",
        f"--{human_port}.type=human",
        f"--{ai_port}.ai.path={agent_pkl}",
        f"--{ai_port}.character={agent_sel.character}",
      ]
      if agent_sel.name:
        cmd.append(f"--{ai_port}.ai.name={agent_sel.name}")
      if self._fullscreen_var.get():    cmd.append("--dolphin.fullscreen")
      if self._infinite_time_var.get(): cmd.append("--dolphin.infinite_time")
      if self._copy_home_var.get():     cmd.append("--dolphin.copy_home_directory")
      if self._save_replays_var.get():  cmd.append("--dolphin.save_replays")
      if self._disable_audio_var.get(): cmd.append("--dolphin.disable_audio")
      if self._use_gpu_var.get():       cmd.append("--use_gpu")
      gfx = self._gfx_var.get().strip()
      if gfx:
        cmd.append(f"--dolphin.gfx_backend={gfx}")

    else:  # netplay
      agent_sel = self._netplay_agent
      script = find_script(root, "scripts/netplay.py", "netplay.py")
      if not script:
        messagebox.showerror("Error", "Cannot find netplay.py in Slippi-AI root.")
        return

      agent_pkl = str(Path(agents) / agent_sel.agent)
      cmd = [
        sys.executable, script,
        f"--agent.path={agent_pkl}",
        f"--agent.sample_temperature={self._temp_var.get():.1f}",
        f"--agent.name={agent_sel.name}",
        f"--char={agent_sel.character}",
        f"--dolphin.path={cfg.get('paths', 'dolphin_dir')}",
        f"--dolphin.iso={cfg.get('paths', 'iso')}",
        f"--dolphin.connect_code={self._code_var.get().strip()}",
        f"--dolphin.user_json_path={cfg.get('paths', 'user_json')}",
        f"--dolphin.stage={self._stage_var.get()}",
      ]
      np_port = self._netplay_port_var.get().strip()
      if np_port:
        cmd.append(f"--dolphin.netplay_port={np_port}")
      lip = self._lan_ip_var.get().strip()
      if lip:
        cmd.append(f"--dolphin.lan_ip={lip}")
      if self._fullscreen_var.get():    cmd.append("--dolphin.fullscreen")
      if self._save_replays_var.get():  cmd.append("--dolphin.save_replays")
      if self._disable_audio_var.get(): cmd.append("--dolphin.disable_audio")
      if self._infinite_time_var.get(): cmd.append("--dolphin.infinite_time")
      if self._use_gpu_var.get():       cmd.append("--use_gpu")

    gecko = gecko_codes_path()
    if gecko.exists() and gecko.read_text(encoding="utf-8").strip():
      cmd.append(f"--dolphin.gecko_codes_file={gecko}")

    try:
      if sys.platform == "win32":
        self._proc = subprocess.Popen(cmd, cwd=root)
      else:
        self._proc = subprocess.Popen(cmd, cwd=root, start_new_session=True)
    except Exception as exc:
      messagebox.showerror("Launch failed", str(exc))
      return

    self._set_running(True)
    threading.Thread(target=self._watch_process, daemon=True).start()

  # ── Process watcher ─────────────────────────────────────────────────────

  def _kill_process_tree(self, proc):
    try:
      if sys.platform == "win32":
        subprocess.run(
          ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      else:
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
      try:
        proc.kill()
      except Exception:
        pass

  def _stop(self):
    if self._proc:
      self._kill_process_tree(self._proc)

  def _watch_process(self):
    if self._proc:
      self._proc.wait()
    self._win.after(0, self._on_process_exit)

  def _on_process_exit(self):
    if self._proc:
      self._kill_process_tree(self._proc)
    self._proc = None
    self._set_running(False)

  def _set_running(self, running: bool):
    if running:
      self._launch_btn.config(
        text="\u25a0  Stop", bg="red", fg="white", command=self._stop)
      self._status_var.set("Running \u2014 close Dolphin or click Stop.")
    else:
      mode = self._mode_var.get()
      label = "Launch netplay.py" if mode == "netplay" else "Launch eval_two.py"
      self._launch_btn.config(
        text=label, bg="green", fg="white", command=self._launch, state="normal")
      self._status_var.set("")
