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
import sys
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

LEGACY_CONFIG_FILE = "slippi_gui_config.ini"
GECKO_CODES_FILE = "gecko_codes.txt"


def _environment_tag() -> str:
  """Short tag identifying the runtime environment.

  Used to namespace per-environment files (notably the GUI config INI) so that
  a Windows backend and a WSL backend rooted at the same on-disk directory
  don't clobber each other's selections, paths, or history.
  """
  if sys.platform == "win32":
    return "win32"
  if sys.platform == "darwin":
    return "darwin"
  if sys.platform.startswith("linux"):
    return "wsl" if _is_wsl() else "linux"
  return sys.platform


def _is_wsl() -> bool:
  """True when running inside the Windows Subsystem for Linux."""
  if sys.platform != "linux":
    return False
  try:
    with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
      content = f.read().lower()
  except OSError:
    return False
  return "microsoft" in content or "wsl" in content


def _config_file_for_env() -> str:
  """Per-environment GUI config filename, e.g. ``slippi_gui_config.wsl.ini``."""
  return f"slippi_gui_config.{_environment_tag()}.ini"


# Backwards-compatible alias. Most callers should not need this — the active
# path is computed in ``AppConfig.__init__``.
CONFIG_FILE = _config_file_for_env()

# In-process cache of parsed model metadata, keyed by (abs_path, mtime).
# Each .pkl is large (40MB+) and unpickling implicitly imports TensorFlow, so a
# repeated parse during one backend run is expensive. We only cache the small
# extracted bits — never the full state — to keep memory bounded. Persistent
# caching across restarts lives in the agent_store JSON records.
#
# Value shape: {"delay": int|None, "chars": list[str]|None, "names": list[str]|None}
_MODEL_INFO_CACHE: dict[str, dict] = {}


def _parse_state_cached(pkl_path: str) -> dict:
  """Parse metadata fields from a pickle, memoized by (path, mtime).

  Returns a dict with ``delay``, ``chars``, ``names`` keys (any may be None).
  On any failure the result is an empty dict, which is also cached so we
  don't keep retrying a broken file.
  """
  try:
    mtime = os.path.getmtime(pkl_path)
  except OSError:
    return {}
  key = f"{os.path.abspath(pkl_path)}|{mtime}"
  cached = _MODEL_INFO_CACHE.get(key)
  if cached is not None:
    return cached

  result: dict = {"delay": None, "chars": None, "names": None}
  try:
    with open(pkl_path, "rb") as f:
      state = pickle.load(f)
  except Exception:
    _MODEL_INFO_CACHE[key] = result
    return result

  try:
    result["delay"] = int(state["config"]["policy"]["delay"])
  except Exception:
    pass

  try:
    result["chars"] = state["config"]["dataset"].get("allowed_characters")
  except Exception:
    pass

  try:
    name_map = state.get("name_map")
    if isinstance(name_map, dict):
      result["names"] = [str(k).strip() for k in name_map.keys() if str(k).strip()]
  except Exception:
    pass

  _MODEL_INFO_CACHE[key] = result
  return result

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
  "mew2":        "mewtwo",
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

def _wsl_slippi_launcher_dir() -> Path | None:
  """Locate the Windows host's Slippi Launcher AppData folder from inside WSL.

  Scans ``/mnt/c/Users/<name>/AppData/Roaming/Slippi Launcher`` for each user
  directory and returns the first match. Lets the WSL backend's auto-detect
  feature pick up the user's existing Windows Slippi installation without any
  Windows-side interop calls.
  """
  if not _is_wsl():
    return None
  users = Path("/mnt/c/Users")
  if not users.is_dir():
    return None
  skip = {"Public", "Default", "Default User", "All Users", "desktop.ini"}
  try:
    candidates = list(users.iterdir())
  except OSError:
    return None
  for user_dir in candidates:
    if user_dir.name in skip or not user_dir.is_dir():
      continue
    candidate = user_dir / "AppData" / "Roaming" / "Slippi Launcher"
    if candidate.is_dir():
      return candidate
  return None


def _translate_windows_path(p: str) -> str:
  """Convert ``C:\\Users\\Foo`` style paths to ``/mnt/c/Users/Foo`` when in WSL.

  Returns the input unchanged on non-WSL platforms or for paths that aren't
  Windows-rooted. Used to normalise paths read from the Windows host's Slippi
  Launcher Settings file so that the WSL backend can actually open them.
  """
  if not p or not _is_wsl():
    return p
  m = re.match(r"^([a-zA-Z]):[\\/](.*)$", p)
  if not m:
    return p
  drive = m.group(1).lower()
  rest = m.group(2).replace("\\", "/")
  return f"/mnt/{drive}/{rest}"


def _slippi_launcher_dir() -> Path | None:
  appdata = os.environ.get("APPDATA", "")
  if appdata:
    p = Path(appdata) / "Slippi Launcher"
    if p.is_dir():
      return p
  # WSL fallback: read the Windows host's APPDATA via /mnt/c.
  wsl = _wsl_slippi_launcher_dir()
  if wsl is not None:
    return wsl
  # Native Linux: Slippi Launcher (Electron) stores userData in
  # $XDG_CONFIG_HOME/Slippi Launcher (defaulting to ~/.config/Slippi Launcher).
  if sys.platform.startswith("linux"):
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    bases = []
    if xdg:
      bases.append(Path(xdg) / "Slippi Launcher")
    bases.append(Path.home() / ".config" / "Slippi Launcher")
    for p in bases:
      if p.is_dir():
        return p
  # macOS: Electron userData lives under ~/Library/Application Support.
  if sys.platform == "darwin":
    p = Path.home() / "Library" / "Application Support" / "Slippi Launcher"
    if p.is_dir():
      return p
  return None


def _dir_has_dolphin(path: Path) -> bool:
  """True if ``path`` looks like a Dolphin install (contains a recognised exe).

  Recognises Windows ``Slippi Dolphin.exe`` / ``Dolphin.exe``, Linux
  ``dolphin-emu`` and any ``*.AppImage`` (the format Slippi Launcher ships on
  Linux), and macOS ``Slippi Dolphin.app``.
  """
  if not path.is_dir():
    return False
  for name in ("Slippi Dolphin.exe", "Dolphin.exe", "dolphin-emu",
               "Slippi Dolphin.app"):
    if (path / name).exists():
      return True
  try:
    for f in path.iterdir():
      if f.suffix == ".AppImage" and f.is_file():
        return True
  except OSError:
    pass
  return False


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
  return _translate_windows_path(data.get("settings", {}).get("isoPath", ""))


def slippi_replays_dir() -> str:
  data = _read_slippi_settings()
  return _translate_windows_path(data.get("settings", {}).get("rootSlpPath", ""))


def slippi_dolphin_dir() -> str:
  if "SLIPPI_DOLPHIN" in os.environ:
    return os.environ["SLIPPI_DOLPHIN"]
  base = _slippi_launcher_dir()
  if base:
    netplay = base / "netplay"
    if _dir_has_dolphin(netplay):
      return str(netplay)
  return ""


def slippi_playback_dolphin_dir() -> str:
  """Locate the Slippi *playback* Dolphin folder.

  Slippi Launcher installs two side-by-side builds:
      <launcher>/netplay/   - online play (rejects ``-i comm.json``)
      <launcher>/playback/  - replay playback (used to play and upgrade SLPs)

  Both the in-app replay browser and the upgrade flow need the playback
  build. The netplay build prints "Unknown option 'i'" because that flag is
  Slippi-Playback-specific.

  Honours ``SLIPPI_PLAYBACK_DOLPHIN`` as an explicit override.
  """
  if "SLIPPI_PLAYBACK_DOLPHIN" in os.environ:
    return os.environ["SLIPPI_PLAYBACK_DOLPHIN"]
  base = _slippi_launcher_dir()
  if base:
    playback = base / "playback"
    if _dir_has_dolphin(playback):
      return str(playback)
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


def slippi_available_gfx_backends() -> list[str]:
  """Detect available GFX backends from the Slippi Dolphin installation.

  On Windows, scans for video backend DLLs in the Dolphin directory.
  On other platforms, returns common backends for that OS.
  """
  _DLL_TO_BACKEND = {
    "libvideo_d3d11.dll":   "D3D11",
    "libvideo_d3d12.dll":   "D3D12",
    "libvideo_ogl.dll":     "OGL",
    "libvideo_vulkan.dll":  "Vulkan",
    "libvideo_software.dll": "Software Renderer",
    "libvideo_null.dll":    "Null",
  }
  dolphin_dir = slippi_dolphin_dir()
  if dolphin_dir and sys.platform == "win32":
    d = Path(dolphin_dir)
    found = [name for dll, name in _DLL_TO_BACKEND.items()
             if (d / dll).exists()]
    if found:
      return found
  # Fallback: platform-appropriate defaults
  if sys.platform == "win32":
    return ["D3D11", "D3D12", "OGL", "Vulkan"]
  return ["OGL", "Vulkan"]

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
  chars = _parse_state_cached(pkl_path).get("chars")

  if chars is None:
    return CHARACTERS

  if isinstance(chars, str):
    chars = chars.strip().lower()

    if chars == "all":
      return CHARACTERS

    parsed = [c.strip() for c in chars.split(",") if c.strip()]
    return parsed if parsed else CHARACTERS

  return None


def read_names_list(pkl_path: str) -> list[str] | None:
  parsed = _parse_state_cached(pkl_path).get("names")
  return parsed if parsed else None


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
  return _parse_state_cached(pkl_path).get("delay")


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
  _SECTIONS = ("paths", "local", "netplay", "options", "app", "dataset", "train_il", "train_rl", "evaluate")

  def __init__(self):
    self._c = configparser.ConfigParser()
    self.path = script_dir() / _config_file_for_env()

    # First-run migration: if there's no per-environment file yet but the
    # legacy ``slippi_gui_config.ini`` exists, seed the new file from it and
    # write it out immediately. This lets users upgrade in place without
    # losing their paths/selections, and lets a single on-disk repo host both
    # a Windows and a WSL backend with independent state going forward. The
    # legacy file is left untouched as a safety net.
    if not self.path.exists():
      legacy = script_dir() / LEGACY_CONFIG_FILE
      if legacy.exists():
        try:
          self._c.read(legacy)
          self._ensure()
          with open(self.path, "w") as f:
            self._c.write(f)
        except Exception:
          # Reset and fall through to a clean init.
          self._c = configparser.ConfigParser()

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
# Tkinter UI helpers (moved to config_ui.py, re-exported for compatibility)
# ──────────────────────────────────────────────────────────────────────────────

def __getattr__(name):
  """Lazy re-export of tkinter-specific helpers from config_ui."""
  _UI_NAMES = {
      "PATH_ROWS", "build_path_fields", "save_path_fields",
      "min_col_width",
  }
  if name in _UI_NAMES:
    from LAUNCHER import config_ui
    return getattr(config_ui, name)
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
