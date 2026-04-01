"""
Configuration, constants, and shared helpers for the Slippi AI Launcher.

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
from pathlib import Path

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


def slippi_iso() -> str:
  if "MELEE_ISO" in os.environ:
    return os.environ["MELEE_ISO"]
  data = _read_slippi_settings()
  return data.get("settings", {}).get("isoPath", "")


def slippi_replays_dir() -> str:
  data = _read_slippi_settings()
  return data.get("settings", {}).get("rootSlpPath", "")


def slippi_dolphin_dir() -> str:
  if "SLIPPI_DOLPHIN" in os.environ:
    return os.environ["SLIPPI_DOLPHIN"]
  base = _slippi_launcher_dir()
  if base:
    netplay = base / "netplay"
    if (netplay / "Slippi Dolphin.exe").exists():
      return str(netplay)
  return ""


def slippi_user_json() -> str:
  if "SLIPPI_USER_JSON" in os.environ:
    return os.environ["SLIPPI_USER_JSON"]
  base = _slippi_launcher_dir()
  if base:
    p = base / "netplay" / "User" / "Slippi" / "user.json"
    if p.exists():
      return str(p)
  return ""


def slippi_gfx_backend() -> str:
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

def script_dir() -> Path:
  """Return the directory containing the LAUNCHER package."""
  return Path(os.path.abspath(__file__)).parent


def detect_root() -> str:
  if "SLIPPI_AI_ROOT" in os.environ:
    return os.environ["SLIPPI_AI_ROOT"]
  # Walk up from script location to find the repo root.
  for candidate in (script_dir(), script_dir().parent):
    if (candidate / "scripts").is_dir():
      return str(candidate)
  return ""


def detect_agents_dir(root: str) -> str:
  if "SLIPPI_AGENTS" in os.environ:
    return os.environ["SLIPPI_AGENTS"]
  if root:
    p = Path(root) / "agents"
    if p.is_dir():
      return str(p)
  return ""


def find_script(root: str, *candidates: str) -> str:
  """Return the first existing path among candidates relative to root."""
  for rel in candidates:
    p = Path(root) / rel
    if p.exists():
      return str(p)
  return ""

# ──────────────────────────────────────────────────────────────────────────────
# Agent helpers
# ──────────────────────────────────────────────────────────────────────────────

def read_allowed_characters(pkl_path: str) -> list[str] | None:
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


def read_names_list(pkl_path: str) -> list[str] | None:
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


def extract_delay_from_filename(filename: str) -> int | None:
  """Extracts delay from filename formats like '_d18_', 'delay_18', etc."""
  base = os.path.basename(filename).lower()
  match = re.search(r'(?:^|[_|-])d(?:elay)?_?(\d+)(?:[_|-|\.]|$)', base)
  if match:
    return int(match.group(1))
  return None


def extract_characters_from_filename(name: str) -> list[str] | None:
  name = name.lower()

  found = []

  for char in CHARACTERS:
    if char in name:
      found.append(char)

  for alias, canonical in _CHAR_ALIASES.items():
    if alias in name and canonical not in found:
      found.append(canonical)

  return found if found else None


def read_model_delay(pkl_path: str) -> int | None:
  try:
    with open(pkl_path, "rb") as f:
      state = pickle.load(f)
    return int(state["config"]["policy"]["delay"])
  except Exception:
    return None


def detect_character(rel_path: str) -> str:
  name = rel_path.lower().replace(os.sep, "_").replace("-", "_").replace(" ", "_")
  for alias, char in _CHAR_ALIASES.items():
    if alias in name:
      return char
  for char in CHARACTERS:
    if char in name:
      return char
  return "fox"


def list_agents(agents_dir: str) -> list:
  """Walk AGENTS_DIR recursively and return relative paths to all files >= 2MB."""
  if not agents_dir or not Path(agents_dir).is_dir():
    return []
  agents = []
  for root, dirs, files in os.walk(agents_dir):
    for f in files:
      full_path = os.path.join(root, f)
      try:
        if os.path.getsize(full_path) >= 2 * 1024 * 1024:
          file_rel = os.path.relpath(full_path, agents_dir)
          agents.append(file_rel)
      except OSError:
        pass
  return sorted(set(agents))

# ──────────────────────────────────────────────────────────────────────────────
# Gecko codes
# ──────────────────────────────────────────────────────────────────────────────

def gecko_codes_path() -> Path:
  return script_dir() / GECKO_CODES_FILE


def load_gecko_codes_text() -> str:
  p = gecko_codes_path()
  if p.exists():
    try:
      return p.read_text(encoding="utf-8")
    except Exception:
      pass
  return ""


def save_gecko_codes_text(text: str):
  p = gecko_codes_path()
  text = text.strip()
  if text:
    p.write_text(text + "\n", encoding="utf-8")
  elif p.exists():
    p.unlink()

# ──────────────────────────────────────────────────────────────────────────────
# AppConfig
# ──────────────────────────────────────────────────────────────────────────────

class AppConfig:
  _SECTIONS = ("paths", "local", "netplay", "options", "app", "dataset", "train_il", "train_rl")

  def __init__(self):
    self._c = configparser.ConfigParser()
    self.path = script_dir() / CONFIG_FILE
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
# Shared path-field builder (used by SetupScreen, SettingsScreen, SettingsDialog)
# ──────────────────────────────────────────────────────────────────────────────

import tkinter as tk
from tkinter import filedialog, ttk

PATH_ROWS = [
  ("slippi_ai_root", "Slippi-AI root directory",              "dir"),
  ("iso",            "Melee 1.02 ISO",                        "file_iso"),
  ("dolphin_dir",    "Slippi Dolphin folder",                 "dir"),
  ("dolphin_headless", "Headless Dolphin for training (optional)", "file_exe"),
  ("user_json",      "Slippi Online user.json (netplay only)","file_json"),
  ("agents_dir",     "Agents directory",                      "dir"),
  ("replays_dir",    "Replays directory (optional)",          "dir"),
  ("m_overlay",      "m'overlay executable (optional)",       "file_exe"),
]


def build_path_fields(parent, cfg: AppConfig) -> dict[str, tk.StringVar]:
  """Build path entry rows in the given parent frame. Returns StringVar dict."""
  root_val = cfg.get("paths", "slippi_ai_root") or detect_root()
  initial = {
    "slippi_ai_root": root_val,
    "iso":            cfg.get("paths", "iso")         or slippi_iso(),
    "dolphin_dir":    cfg.get("paths", "dolphin_dir") or slippi_dolphin_dir(),
    "dolphin_headless": cfg.get("paths", "dolphin_headless"),
    "user_json":      cfg.get("paths", "user_json")   or slippi_user_json(),
    "agents_dir":     cfg.get("paths", "agents_dir")  or detect_agents_dir(root_val),
    "replays_dir":    cfg.get("paths", "replays_dir") or slippi_replays_dir(),
    "m_overlay":      cfg.get("paths", "m_overlay"),
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
    elif ftype == "file_exe":
      cmd = lambda k=key: _browse_file(v, k, [("Executable", "*.exe *.EXE *.AppImage"), ("All", "*.*")])
    else:
      cmd = lambda k=key: _browse_file(v, k, [("JSON", "*.json"), ("All", "*.*")])
    ttk.Button(parent, text="Browse\u2026", command=cmd).grid(row=i, column=2, pady=3)

  return v


def _browse_dir(v: dict[str, tk.StringVar], key: str):
  p = filedialog.askdirectory()
  if p:
    v[key].set(p)
    if key == "slippi_ai_root" and not v["agents_dir"].get():
      v["agents_dir"].set(detect_agents_dir(p))


def _browse_file(v: dict[str, tk.StringVar], key: str, filetypes):
  p = filedialog.askopenfilename(filetypes=filetypes)
  if p:
    v[key].set(p)


def save_path_fields(v: dict[str, tk.StringVar], cfg: AppConfig):
  for k, sv in v.items():
    cfg.set("paths", k, sv.get().strip())
  cfg.save()
