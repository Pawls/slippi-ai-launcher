"""Config API: read/write INI-based app configuration."""

from fastapi import APIRouter
from pydantic import BaseModel

from LAUNCHER.api.app import get_state
from LAUNCHER.config import (
    CHARACTERS, STAGES,
    detect_root, detect_agents_dir,
    slippi_iso, slippi_dolphin_dir, slippi_user_json, slippi_replays_dir,
    slippi_gfx_backend, slippi_available_gfx_backends,
)

router = APIRouter(prefix="/config", tags=["config"])


class ConfigValue(BaseModel):
    value: str


class ConfigBulk(BaseModel):
    values: dict[str, str]


@router.get("/")
def get_all_config():
    """Return all config sections and their key-value pairs."""
    s = get_state()
    result = {}
    for section in s.cfg._SECTIONS:
        result[section] = dict(s.cfg._c.items(section))
    return result


# ── Meta endpoints (must be defined BEFORE /{section}/{key} catch-all) ──────

@router.get("/meta/paths-complete")
def paths_complete():
    return {"complete": get_state().cfg.paths_complete()}


@router.get("/meta/characters")
def get_characters():
    return CHARACTERS


@router.get("/meta/stages")
def get_stages():
    return STAGES


@router.get("/meta/auto-detect")
def auto_detect_paths():
    """Return auto-detected paths from the Slippi Launcher installation."""
    root = detect_root()
    return {
        "slippi_ai_root": root,
        "iso": slippi_iso(),
        "dolphin_dir": slippi_dolphin_dir(),
        "user_json": slippi_user_json(),
        "replays_dir": slippi_replays_dir(),
        "agents_dir": detect_agents_dir(root),
        "gfx_backend": slippi_gfx_backend(),
        "available_gfx_backends": slippi_available_gfx_backends(),
    }


# ── Dynamic section/key endpoints ──────────────────────────────────────────

@router.get("/{section}/{key}")
def get_config_value(section: str, key: str):
    s = get_state()
    return {"value": s.cfg.get(section, key)}


@router.put("/{section}/{key}")
def set_config_value(section: str, key: str, body: ConfigValue):
    s = get_state()
    s.cfg.set(section, key, body.value)
    s.cfg.save()
    return {"ok": True}


@router.put("/{section}")
def set_config_section(section: str, body: ConfigBulk):
    """Set multiple keys in a section at once."""
    s = get_state()
    for k, v in body.values.items():
        s.cfg.set(section, k, v)
    s.cfg.save()
    return {"ok": True}
