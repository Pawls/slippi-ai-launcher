"""
Slippi AI Launcher
====================
Place this file (and slippi_gui_config.ini) anywhere inside the slippi-ai
repo — including a subdirectory like APP/ — and it will locate the repo root
automatically.

Auto-configuration: reads %APPDATA%\\Slippi Launcher\\Settings to pre-fill
the ISO path, replay directory, Dolphin folder, and user.json with no extra
setup required.

Environment variable overrides (all optional):
  SLIPPI_AI_ROOT    path to the slippi-ai repo root
  SLIPPI_AGENTS     path to agents directory
  MELEE_ISO         path to Melee 1.02 .iso  (overrides Settings file)
  SLIPPI_DOLPHIN    path to the Slippi Dolphin *folder*
  SLIPPI_USER_JSON  path to Slippi Online user.json
"""

import configparser
import json
import os
import pickle
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = "slippi_gui_config.ini"
GECKO_CODES_FILE = "gecko_codes.txt"

# Cache for parsed model metadata to avoid repeatedly loading large pickle files
_MODEL_INFO_CACHE: dict[str, tuple[int | None, list[str] | None, list[str] | None]] = {}

CHAR_LOADING = ["Loading..."]

# Canonical character names accepted by slippi-ai scripts
CHARACTERS = [
  "fox", "falco", "marth", "sheik", "jigglypuff", "cptfalcon",
  "peach", "yoshi", "popo", "pikachu", "samus", "luigi",
  "ganondorf", "link", "zelda", "ness", "mewtwo", "gameandwatch",
  "roy", "pichu", "bowser", "doc", "ylink", "kirby", "dk",
]

_CHAR_ALIASES = {
  "falcon":      "cptfalcon",
  "captain":     "cptfalcon",
  "puff":        "jigglypuff",
  "jiggs":       "jigglypuff",
  "ganon":       "ganondorf",
  "iceclimbers": "popo",
  "ics":         "popo",
  "pika":        "pikachu",
  "g&w":         "gameandwatch",
  "gnw":         "gameandwatch",
  "younglink":   "ylink",
}

STAGES = [
  "RANDOM_STAGE",
  "FINAL_DESTINATION",
  "BATTLEFIELD",
  "FOUNTAIN_OF_DREAMS",
  "DREAMLAND",
  "POKEMON_STADIUM",
  "YOSHIS_STORY",
]

# ──────────────────────────────────────────────────────────────────────────────
# Slippi Launcher Settings reader
# ──────────────────────────────────────────────────────────────────────────────

def _slippi_launcher_dir() -> Path | None:
  appdata = os.environ.get("APPDATA", "")
  if not appdata:
    return None
  p = Path(appdata) / "Slippi Launcher"
  return p if p.is_dir() else None


def _read_slippi_settings() -> dict:
  """Parse %APPDATA%\\Slippi Launcher\\Settings and return the JSON dict."""
  base = _slippi_launcher_dir()
  if base is None:
    return {}
  settings_file = base / "Settings"
  if not settings_file.exists():
    return {}
  try:
    with open(settings_file, encoding="utf-8") as f:
      return json.load(f)
  except Exception:
    return {}


def _slippi_iso() -> str:
  if "MELEE_ISO" in os.environ:
    return os.environ["MELEE_ISO"]
  data = _read_slippi_settings()
  return data.get("settings", {}).get("isoPath", "")


def _slippi_replays_dir() -> str:
  data = _read_slippi_settings()
  return data.get("settings", {}).get("rootSlpPath", "")


def _slippi_dolphin_dir() -> str:
  if "SLIPPI_DOLPHIN" in os.environ:
    return os.environ["SLIPPI_DOLPHIN"]
  base = _slippi_launcher_dir()
  if base:
    netplay = base / "netplay"
    if (netplay / "Slippi Dolphin.exe").exists():
      return str(netplay)
  return ""


def _slippi_user_json() -> str:
  if "SLIPPI_USER_JSON" in os.environ:
    return os.environ["SLIPPI_USER_JSON"]
  base = _slippi_launcher_dir()
  if base:
    p = base / "netplay" / "User" / "Slippi" / "user.json"
    if p.exists():
      return str(p)
  return ""


def _slippi_gfx_backend() -> str:
  """Read the GFX backend from the user's netplay Dolphin.ini."""
  base = _slippi_launcher_dir()
  if base is None:
    return ""
  ini_path = base / "netplay" / "User" / "Config" / "Dolphin.ini"
  if not ini_path.exists():
    return ""
  try:
    c = configparser.ConfigParser()
    c.read(ini_path, encoding="utf-8")
    return c.get("Core", "GFXBackend", fallback="")
  except Exception:
    return ""

# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def _script_dir() -> Path:
  return Path(os.path.abspath(__file__)).parent


def _detect_root() -> str:
  if "SLIPPI_AI_ROOT" in os.environ:
    return os.environ["SLIPPI_AI_ROOT"]
  # Walk up from script location to find the repo root.
  for candidate in (_script_dir(), _script_dir().parent):
    # Look for the 'scripts' folder, a much safer indicator of the repo root
    if (candidate / "scripts").is_dir():
      return str(candidate)
  return ""


def _detect_agents_dir(root: str) -> str:
  if "SLIPPI_AGENTS" in os.environ:
    return os.environ["SLIPPI_AGENTS"]
  if root:
    p = Path(root) / "agents"
    if p.is_dir():
      return str(p)
  return ""


def _find_script(root: str, *candidates: str) -> str:
  """Return the first existing path among candidates relative to root."""
  for rel in candidates:
    p = Path(root) / rel
    if p.exists():
      return str(p)
  return ""

# ──────────────────────────────────────────────────────────────────────────────
# Agent helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_allowed_characters(pkl_path: str) -> list[str] | None:
  try:
    with open(pkl_path, "rb") as f:
      state = pickle.load(f)

    chars = state["config"]["dataset"].get("allowed_characters")

    if chars is None:
      return CHARACTERS

    if isinstance(chars, str):
      chars = chars.strip().lower()

      if chars == "all":
        return CHARACTERS

      parsed = [c.strip() for c in chars.split(",") if c.strip()]
      return parsed if parsed else CHARACTERS

    return None

  except Exception:
    return CHARACTERS

def _read_names_list(pkl_path: str) -> list[str] | None:
  try:
    with open(pkl_path, "rb") as f:
      state = pickle.load(f)

    names_data = state.get("name_map")

    if names_data is None:
      return None

    parsed = []

    if isinstance(names_data, dict):
      parsed = [str(k).strip() for k in names_data.keys() if str(k).strip()]

    return parsed if parsed else None

  except Exception:
    return None

def _extract_delay_from_filename(filename: str) -> int | None:
  """Extracts delay from filename formats like '_d18_', 'delay_18', etc."""
  base = os.path.basename(filename).lower()
  match = re.search(r'(?:^|[_|-])d(?:elay)?_?(\d+)(?:[_|-|\.]|$)', base)
  if match:
    return int(match.group(1))
  return None

def _extract_characters_from_filename(name: str) -> list[str] | None:
  name = name.lower()

  found = []

  for char in CHARACTERS:
    if char in name:
      found.append(char)

  for alias, canonical in _CHAR_ALIASES.items():
    if alias in name and canonical not in found:
      found.append(canonical)

  return found if found else None

def _read_model_delay(pkl_path: str) -> int | None:
  try:
    with open(pkl_path, "rb") as f:
      state = pickle.load(f)
    return int(state["config"]["policy"]["delay"])
  except Exception:
    return None

def _detect_character(rel_path: str) -> str:
  name = rel_path.lower().replace(os.sep, "_").replace("-", "_").replace(" ", "_")
  for alias, char in _CHAR_ALIASES.items():
    if alias in name:
      return char
  for char in CHARACTERS:
    if char in name:
      return char
  return "fox"


def _list_agents(agents_dir: str) -> list:
  """Walk AGENTS_DIR recursively and return relative paths to all files >= 2MB."""
  if not agents_dir or not Path(agents_dir).is_dir():
    return []
  agents = []
  for root, dirs, files in os.walk(agents_dir):
    for f in files:
      full_path = os.path.join(root, f)
      try:
        # File must be >= 2 Megabytes
        if os.path.getsize(full_path) >= 2 * 1024 * 1024:
          file_rel = os.path.relpath(full_path, agents_dir)
          agents.append(file_rel)
      except OSError:
        pass
  return sorted(set(agents))

# ──────────────────────────────────────────────────────────────────────────────
# AppConfig
# ──────────────────────────────────────────────────────────────────────────────

class AppConfig:
  _SECTIONS = ("paths", "local", "netplay", "options", "app")

  def __init__(self):
    self._c = configparser.ConfigParser()
    self.path = _script_dir() / CONFIG_FILE
    self._ensure()
    self._c.read(self.path)

  def _ensure(self):
    for s in self._SECTIONS:
      if not self._c.has_section(s):
        self._c.add_section(s)

  def save(self):
    self._ensure()
    with open(self.path, "w") as f:
      self._c.write(f)

  def get(self, s, k, fallback=""):        return self._c.get(s, k, fallback=fallback)
  def set(self, s, k, v):                  self._ensure(); self._c.set(s, k, str(v))
  def getbool(self, s, k, fallback=False): return self._c.getboolean(s, k, fallback=fallback)
  def getint(self, s, k, fallback=0):      return self._c.getint(s, k, fallback=fallback)
  def getfloat(self, s, k, fallback=1.0):  return self._c.getfloat(s, k, fallback=fallback)

  def paths_complete(self) -> bool:
    return bool(self.get("paths", "slippi_ai_root") and
                self.get("paths", "iso"))

# ──────────────────────────────────────────────────────────────────────────────
# Navigation framework
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# Settings dialog (kept for potential reuse)
# ──────────────────────────────────────────────────────────────────────────────

PATH_ROWS = [
  ("slippi_ai_root", "Slippi-AI root directory",              "dir"),
  ("iso",            "Melee 1.02 ISO",                        "file_iso"),
  ("dolphin_dir",    "Slippi Dolphin folder (netplay only)",  "dir"),
  ("user_json",      "Slippi Online user.json (netplay only)","file_json"),
  ("agents_dir",     "Agents directory",                      "dir"),
  ("replays_dir",    "Replays directory (optional)",          "dir"),
]


def _build_path_fields(parent, cfg: AppConfig) -> dict[str, tk.StringVar]:
  """Build path entry rows in the given parent frame. Returns StringVar dict."""
  root_val = cfg.get("paths", "slippi_ai_root") or _detect_root()
  initial = {
    "slippi_ai_root": root_val,
    "iso":            cfg.get("paths", "iso")         or _slippi_iso(),
    "dolphin_dir":    cfg.get("paths", "dolphin_dir") or _slippi_dolphin_dir(),
    "user_json":      cfg.get("paths", "user_json")   or _slippi_user_json(),
    "agents_dir":     cfg.get("paths", "agents_dir")  or _detect_agents_dir(root_val),
    "replays_dir":    cfg.get("paths", "replays_dir") or _slippi_replays_dir(),
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
      cmd = lambda k=key: _browse_file(v, k, [("ISO", "*.iso *.ISO"), ("All", "*.*")])
    else:
      cmd = lambda k=key: _browse_file(v, k, [("JSON", "*.json"), ("All", "*.*")])
    ttk.Button(parent, text="Browse\u2026", command=cmd).grid(row=i, column=2, pady=3)

  return v


def _browse_dir(v: dict[str, tk.StringVar], key: str):
  p = filedialog.askdirectory()
  if p:
    v[key].set(p)
    if key == "slippi_ai_root" and not v["agents_dir"].get():
      v["agents_dir"].set(_detect_agents_dir(p))


def _browse_file(v: dict[str, tk.StringVar], key: str, filetypes):
  p = filedialog.askopenfilename(filetypes=filetypes)
  if p:
    v[key].set(p)


def _save_path_fields(v: dict[str, tk.StringVar], cfg: AppConfig):
  for k, sv in v.items():
    cfg.set("paths", k, sv.get().strip())
  cfg.save()


class SettingsDialog(tk.Toplevel):
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
    self._v = _build_path_fields(fields_frame, cfg)

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
    _save_path_fields(self._v, self._cfg)
    self.destroy()

# ──────────────────────────────────────────────────────────────────────────────
# Gecko Codes dialog
# ──────────────────────────────────────────────────────────────────────────────

def _gecko_codes_path() -> Path:
  return _script_dir() / GECKO_CODES_FILE

def _load_gecko_codes_text() -> str:
  p = _gecko_codes_path()
  if p.exists():
    try:
      return p.read_text(encoding="utf-8")
    except Exception:
      pass
  return ""

def _save_gecko_codes_text(text: str):
  p = _gecko_codes_path()
  text = text.strip()
  if text:
    p.write_text(text + "\n", encoding="utf-8")
  elif p.exists():
    p.unlink()

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

    existing = _load_gecko_codes_text()
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
    _save_gecko_codes_text(content)
    self.destroy()

# ──────────────────────────────────────────────────────────────────────────────
# Shared agent selector widget
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
    agents = _list_agents(self._cfg.get("paths", "agents_dir"))
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

    # Instantly update the character so the user isn't blocked
    self._char_var.set(_detect_character(agent))
    if not self._name_none_var.get():
      self._name_var.set("Loading...")
    self._delay_hint.config(text="AI Delay: Calculating...", foreground="orange")

    agents_dir = self._cfg.get("paths", "agents_dir")
    full_path = str(Path(agents_dir) / agent)

    # Check cache first
    if full_path in _MODEL_INFO_CACHE:
      delay, names, chars = _MODEL_INFO_CACHE[full_path]
      self._update_model_info(delay, names, chars)
      return

    def fetch_model_info():
      delay = _extract_delay_from_filename(agent)
      chars = _extract_characters_from_filename(agent)

      if delay is None:
        delay = _read_model_delay(full_path)

      if chars is None:
        chars = _read_allowed_characters(full_path)

      names = _read_names_list(full_path)

      _MODEL_INFO_CACHE[full_path] = (delay, names, chars)
      self.after(0, lambda: self._update_model_info(delay, names, chars))

    # Run file parsing in the background so the UI doesn't freeze
    threading.Thread(target=fetch_model_info, daemon=True).start()

  def _on_name_none_toggle(self):
    if self._name_none_var.get():
      self._name_combo.config(state="disabled")
    else:
      self._name_combo.config(state="readonly")

  def _update_delay_hint(self, delay):
    if delay is not None:
      ms_delay = int(round(delay * 1000 / 60))
      self._delay_hint.config(text=f"AI trained with {delay} frames ({ms_delay}ms) of delay", foreground="blue")
    else:
      self._delay_hint.config(text="AI Delay: Unknown", foreground="red")

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
    # update delay text
    if delay is not None:
      ms_delay = int(round(delay * 1000 / 60))
      self._delay_hint.config(
        text=f"AI trained with {delay} frames ({ms_delay}ms) of delay",
        foreground="blue"
      )
    else:
      self._delay_hint.config(text="AI Delay: Unknown", foreground="red")

    # update name list
    if names:
      self._name_combo["values"] = names
      if not self._name_none_var.get():
        current = self._name_var.get()
        if current not in names or current == "Loading...":
          self._name_var.set(names[0])

    # update character list
    if chars:
      self._char_combo["values"] = chars
      if self._char_var.get() not in chars:
        self._char_var.set(chars[0])

# ──────────────────────────────────────────────────────────────────────────────
# Setup screen (first launch)
# ──────────────────────────────────────────────────────────────────────────────

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
    self._path_vars = _build_path_fields(paths_frame, cfg)

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
    _save_path_fields(self._path_vars, self.cfg)
    self.cfg.set("app", "setup_complete", "True")
    self.cfg.save()
    self.navigator.navigate_home()

# ──────────────────────────────────────────────────────────────────────────────
# Home screen
# ──────────────────────────────────────────────────────────────────────────────

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

    tk.Button(outer, text="\u2699  Settings", bg="#757575", fg="white",
              command=lambda: navigator.navigate_to("settings"),
              **btn_style).pack(pady=8)

# ──────────────────────────────────────────────────────────────────────────────
# Play screen (wraps existing SlippiLauncher)
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
# Create screen (training workflow)
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# Settings screen
# ──────────────────────────────────────────────────────────────────────────────

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
    self._path_vars = _build_path_fields(self._paths_frame, self.cfg)

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
    _save_path_fields(self._path_vars, self.cfg)
    self.navigator.go_back()

# ──────────────────────────────────────────────────────────────────────────────
# Placeholder screen for Create sub-steps
# ──────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_TITLES = {
  "dataset":  "Dataset Management",
  "train_il": "Train Imitation Model",
  "rl":       "Reinforcement Learning",
  "evaluate": "Evaluate",
}

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

# ──────────────────────────────────────────────────────────────────────────────
# Main launcher (Play screen content)
# ──────────────────────────────────────────────────────────────────────────────

class SlippiLauncher:

  def __init__(self, parent: tk.Frame, win: tk.Tk, cfg: AppConfig):
    self._parent = parent
    self._win = win
    self._cfg = cfg
    self._proc: subprocess.Popen | None = None
    self._build()

  # ── UI ───────────────────────────────────────────────────────────────────

  def _build(self):
    outer = ttk.Frame(self._parent, padding=10)
    outer.pack(fill="both", expand=True)

    # Mode selector ───────────────────────────────────────────────────────
    mode_frame = ttk.LabelFrame(outer, text="Mode", padding=6)
    mode_frame.pack(fill="x", pady=(0, 8))

    self._mode_var = tk.StringVar(value=self._cfg.get("options", "last_mode", "local"))
    ttk.Radiobutton(mode_frame, text="Local Play",
                    variable=self._mode_var, value="local",
                    command=self._on_mode_change).pack(side="left", padx=12)
    ttk.Radiobutton(mode_frame, text="Netplay",
                    variable=self._mode_var, value="netplay",
                    command=self._on_mode_change).pack(side="left", padx=12)

    # Agent selector (shared) ─────────────────────────────────────────────
    # Two AgentSelectors, one per mode; only one is shown at a time
    self._local_agent = AgentSelector(outer, self._cfg, "local")
    self._netplay_agent = AgentSelector(outer, self._cfg, "netplay")

    # Netplay connection panel ─────────────────────────────────────────────
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

    # Options panel ────────────────────────────────────────────────────────
    opts = ttk.LabelFrame(outer, text="Options", padding=8)
    opts.pack(fill="x", pady=(0, 6))

    # Shared options
    self._fullscreen_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "fullscreen", True))
    ttk.Checkbutton(opts, text="Fullscreen",
                    variable=self._fullscreen_var).grid(row=0, column=0, sticky="w")

    self._infinite_time_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "infinite_time", False))
    ttk.Checkbutton(opts, text="Infinite time",
                    variable=self._infinite_time_var).grid(
      row=0, column=1, sticky="w", padx=(16, 0))

    # Netplay-only options (will be shown/hidden)
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

    # Stage (netplay-only)
    self._stage_lbl = ttk.Label(opts, text="Stage")
    self._stage_lbl.grid(row=1, column=0, sticky="w", pady=(8, 0))
    self._stage_var = tk.StringVar(
      value=self._cfg.get("options", "stage", "RANDOM_STAGE"))
    self._stage_combo = ttk.Combobox(opts, textvariable=self._stage_var,
                                     values=STAGES, width=22, state="readonly")
    self._stage_combo.grid(row=1, column=1, columnspan=2, sticky="w",
                           padx=(8, 0), pady=(8, 0))

    # Temperature (netplay-only)
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

    # GFX backend (local-only)
    self._gfx_lbl = ttk.Label(opts, text="GFX Backend:")
    self._gfx_lbl.grid(row=3, column=0, sticky="w", pady=(8, 0))
    detected = self._cfg.get("options", "gfx_backend") or _slippi_gfx_backend()
    self._gfx_var = tk.StringVar(value=detected)
    self._gfx_combo = ttk.Combobox(
      opts, textvariable=self._gfx_var, width=22)
    self._gfx_combo.grid(row=3, column=1, columnspan=2, sticky="w",
                         padx=(8, 0), pady=(8, 0))
    gfx_hint = ttk.Label(opts, text="(auto-detected from Dolphin)",
                          foreground="gray", font=("TkDefaultFont", 8))
    gfx_hint.grid(row=3, column=3, sticky="w", padx=(6, 0), pady=(8, 0))

    # Local copy-home option
    self._copy_home_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "copy_home_directory", True))
    self._copy_home_cb = ttk.Checkbutton(
      opts, text="Copy home dir  (use your Dolphin controller config)",
      variable=self._copy_home_var)
    self._copy_home_cb.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))

    # Use GPU for AI inference (local-only)
    self._use_gpu_var = tk.BooleanVar(
      value=self._cfg.getbool("options", "use_gpu", False))
    self._use_gpu_cb = ttk.Checkbutton(
      opts, text="Use GPU for AI inference  (reduces CPU contention)",
      variable=self._use_gpu_var)
    self._use_gpu_cb.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

    # Gecko codes button (shared, both modes)
    gecko_row = ttk.Frame(opts)
    gecko_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
    ttk.Button(gecko_row, text="Gecko Codes\u2026",
               command=self._open_gecko_codes).pack(side="left")
    self._gecko_hint = ttk.Label(gecko_row, text="", foreground="gray",
                                  font=("TkDefaultFont", 8))
    self._gecko_hint.pack(side="left", padx=(8, 0))
    self._update_gecko_hint()

    # Status + buttons ─────────────────────────────────────────────────────
    bottom = ttk.Frame(outer)
    bottom.pack(fill="x", pady=(8, 0))

    self._status_var = tk.StringVar(value="")
    ttk.Label(bottom, textvariable=self._status_var,
              foreground="gray", font=("TkDefaultFont", 8)).pack(side="left", pady=(10,0))

    btn_right = ttk.Frame(bottom)
    btn_right.pack(side="right")

    # Large green launch button
    self._launch_btn = tk.Button(btn_right, text="Launch eval_two.py",
                                 bg="green", fg="white", font=("Arial", 11, "bold"),
                                 padx=10, pady=4, cursor="hand2", command=self._launch)
    self._launch_btn.pack(side="left", padx=4)

    # Apply initial mode layout
    self._on_mode_change()

  # ── Mode switching ────────────────────────────────────────────────────────

  def _on_mode_change(self):
    mode = self._mode_var.get()

    self._local_agent.pack_forget()
    self._netplay_agent.pack_forget()
    self._conn_frame.pack_forget()

    if mode == "local":
      self._local_agent.pack(fill="x", pady=(0, 6))
      # Hide netplay-only widgets
      for w in (self._save_replays_cb, self._temp_lbl):
        w.grid_remove()
      # Show local-applicable widgets
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
      # Show netplay-only widgets
      for w in (self._save_replays_cb, self._disable_audio_cb,
                self._stage_lbl, self._stage_combo,
                self._temp_lbl):
        w.grid()
      self._copy_home_cb.grid_remove()
      self._use_gpu_cb.grid()
      self._gfx_lbl.grid_remove()
      self._gfx_combo.grid_remove()
      self._launch_btn.config(text="Launch netplay.py")

  # ── Callbacks ─────────────────────────────────────────────────────────────

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
    text = _load_gecko_codes_text().strip()
    if text:
      count = sum(1 for line in text.splitlines() if line.strip().startswith('$'))
      self._gecko_hint.config(text=f"{count} code(s) configured")
    else:
      self._gecko_hint.config(text="No custom codes")

  # ── Launch ─────────────────────────────────────────────────────────────────

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
    # Kill any lingering process from a previous run
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
      script = _find_script(root, "scripts/eval_two.py", "eval_two.py")
      if not script:
        messagebox.showerror("Error", "Cannot find eval_two.py in Slippi-AI root.")
        return

      agent_pkl = str(Path(agents) / agent_sel.agent)

      # Shift settings based on player slot selection
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
      script = _find_script(root, "scripts/netplay.py", "netplay.py")
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
        f"--dolphin.online_delay={agent_sel.delay}",
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

    # Attach custom gecko codes file if it exists
    gecko_path = _gecko_codes_path()
    if gecko_path.exists() and gecko_path.read_text(encoding="utf-8").strip():
      cmd.append(f"--dolphin.gecko_codes_file={gecko_path}")

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

  # ── Process watcher ────────────────────────────────────────────────────────

  def _kill_process_tree(self, proc):
    """Kill the process and all its children."""
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
    """Stop the running child process tree."""
    if self._proc:
      self._kill_process_tree(self._proc)

  def _watch_process(self):
    """Background thread: waits for the child process to exit, then resets UI."""
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

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def _autofill(cfg: AppConfig):
  root = cfg.get("paths", "slippi_ai_root") or _detect_root()
  fills = [
    ("slippi_ai_root", lambda: root),
    ("iso",            _slippi_iso),
    ("dolphin_dir",    _slippi_dolphin_dir),
    ("user_json",      _slippi_user_json),
    ("replays_dir",    _slippi_replays_dir),
    ("agents_dir",     lambda: _detect_agents_dir(root)),
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


if __name__ == "__main__":
  main()
