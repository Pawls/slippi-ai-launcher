"""Dataset-pipeline command builder.

Mirror of LAUNCHER.api.training for the dataset-management subprocess
steps (prepare archives, upgrade replays, parse, compute stats, character
filter). Each script has its own field schema so the frontend can render
a generic form and the backend can serialize flags in the format each
script expects.

Step 5 (filter by stats) is handled synchronously in
LAUNCHER.api.routes.dataset — it queries parsed.sqlite directly rather
than spawning a subprocess.

Step 6 (create dataset) chains convert_sqlite_to_parsed.py +
make_local_dataset.py. A wrapper script will land separately; for now
this registry only covers the single-script steps.
"""

import sys
from typing import Any

from LAUNCHER.config import find_script


# Each field entry:
#   name       — flag name, passed as --{name}
#   type       — "str" | "int" | "float" | "bool"
#   style      — "kv"   → always emit --name=value
#                "flag" → emit bare --name only when value is truthy (bools)
#   required   — validation hint; unset or empty required string => error
# Display-only hints consumed by the frontend (ignored by build_command):
#   label      — human label (defaults to name)
#   help       — small helper text rendered under the field
#   browse     — "dir" | "file_iso"  → render a Browse button
#   options    — list[str]           → render as a <select>
#   default    — initial value
#   config_default — "section:key" lookup against AppConfig (frontend resolves
#                    via /config/ on mount; takes precedence over default)
DATASET_SCRIPTS: dict[str, dict[str, Any]] = {
    "prepare": {
        "label": "Prepare Archives",
        "description": (
            "Accepts a directory with any mix of .7z/.zip archives and "
            "subdirectories of loose .slp files. Archives are copied; "
            "directories are compressed into .7z. Requires the 7z command-line "
            "tool for loose files."
        ),
        "script_candidates": ("slippi_db/scripts/prepare_local.py",),
        "fields": [
            {
                "name": "slp_root", "type": "str", "style": "kv", "required": True,
                "label": "Source directory", "browse": "dir",
                "help": "Subdirs of .slp files should be named yyyy-mm "
                        "(e.g. 2024-04). Existing .7z/.zip archives are copied as-is.",
                "config_default": "dataset:source_dir",
            },
            {
                "name": "zip_root", "type": "str", "style": "kv", "required": True,
                "label": "Output directory", "browse": "dir",
                "help": "Existing archives in Raw/ will be skipped.",
                "config_default": "dataset:dataset_root",
            },
        ],
    },
    "upgrade_slps": {
        "label": "Upgrade Replays (Optional)",
        "description": (
            "Replays from Slippi versions before 3.2.0 may be missing stage "
            "event data. Upgrading uses Dolphin to re-record them. This is "
            "time-consuming but improves data quality. Most recent replays "
            "(2023+) don't need this. Note: operates on .zip archives only "
            "(not .7z)."
        ),
        "script_candidates": ("slippi_db/scripts/upgrade_slps.py",),
        "fields": [
            {
                "name": "root", "type": "str", "style": "kv", "required": True,
                "label": "Dataset root", "browse": "dir",
                "config_default": "dataset:dataset_root",
            },
            {
                "name": "dolphin", "type": "str", "style": "kv", "required": True,
                "label": "Dolphin path", "browse": "dir",
                "config_default": "paths:dolphin_dir",
            },
            {
                "name": "iso", "type": "str", "style": "kv", "required": True,
                "label": "ISO path", "browse": "file_iso",
                "config_default": "paths:iso",
            },
            {
                "name": "threads", "type": "int", "style": "kv",
                "label": "Threads", "default": 4,
            },
            {
                "name": "skip_existing", "type": "bool", "style": "flag",
                "label": "Skip existing", "default": True,
            },
            {
                "name": "dry_run", "type": "bool", "style": "flag",
                "label": "Dry run",
            },
        ],
    },
    "parse": {
        "label": "Parse Replays",
        "description": (
            "Parse .slp files from archives into parquet format and build "
            "the parsed.sqlite database."
        ),
        "script_candidates": ("slippi_db/parse_local.py",),
        "fields": [
            {
                "name": "root", "type": "str", "style": "kv", "required": True,
                "label": "Dataset root", "browse": "dir",
                "config_default": "dataset:dataset_root",
            },
            {
                "name": "threads", "type": "int", "style": "kv",
                "label": "Threads", "default": 4,
            },
            {
                "name": "compression", "type": "str", "style": "kv",
                "label": "Compression",
                "options": ["zlib", "snappy", "gzip", "brotli", "lz4", "zstd", "none"],
                "default": "zlib",
                "config_default": "dataset:compression",
            },
            {
                "name": "reprocess", "type": "bool", "style": "flag",
                "label": "Reprocess",
            },
            {
                "name": "reparse_missing_netplay", "type": "bool", "style": "flag",
                "label": "Re-parse missing netplay",
            },
            {
                "name": "reparse_missing_parsed", "type": "bool", "style": "flag",
                "label": "Re-parse missing parsed",
            },
            {
                "name": "dry_run", "type": "bool", "style": "flag",
                "label": "Dry run",
            },
        ],
    },
    "compute_stats": {
        "label": "Compute Replay Stats",
        "description": (
            "Analyze parsed replays to compute per-player quality metrics "
            "(L-cancel rate, APM, neutral wins, conversions, etc). Results "
            "are stored in parsed.sqlite for fast filtering."
        ),
        "script_candidates": ("slippi_db/scripts/compute_replay_stats.py",),
        "fields": [
            {
                "name": "root", "type": "str", "style": "kv", "required": True,
                "label": "Dataset root", "browse": "dir",
                "config_default": "dataset:dataset_root",
            },
            {
                "name": "threads", "type": "int", "style": "kv",
                "label": "Threads", "default": 4,
            },
            {
                "name": "recompute", "type": "bool", "style": "flag",
                "label": "Recompute all",
            },
        ],
    },
    "filter_by_character": {
        "label": "Filter by Character (Optional)",
        "description": (
            "Create a character-specific dataset subset with symlinks to "
            "original parquet files."
        ),
        "script_candidates": ("slippi_db/scripts/filter_by_character.py",),
        "fields": [
            {
                "name": "root", "type": "str", "style": "kv", "required": True,
                "label": "Dataset root", "browse": "dir",
                "config_default": "dataset:dataset_root",
            },
            {
                "name": "characters", "type": "str", "style": "kv", "required": True,
                "label": "Characters",
                "help": "Comma-separated, e.g. fox,marth,sheik",
            },
            {
                "name": "output", "type": "str", "style": "kv",
                "label": "Output dir", "browse": "dir",
                "help": "Leave blank for auto-generated name (Dataset-CHARACTER)",
            },
            {
                "name": "both", "type": "bool", "style": "flag",
                "label": "Both players must match",
            },
            {
                "name": "limit", "type": "int", "style": "kv",
                "label": "Limit",
            },
        ],
    },
}


def _coerce(value: Any, ftype: str) -> Any:
    if value is None or value == "":
        return None
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if ftype == "int":
        return int(value)
    if ftype == "float":
        return float(value)
    return str(value)


def build_command(
    script_key: str,
    values: dict[str, Any],
    root: str,
    python: str | None = None,
) -> list[str]:
    """Build argv for a dataset pipeline script.

    Raises ValueError on unknown script_key, missing script file, or
    unsatisfied required fields.
    """
    if script_key not in DATASET_SCRIPTS:
        raise ValueError(f"Unknown dataset script: {script_key}")

    spec = DATASET_SCRIPTS[script_key]
    script_path = find_script(root, *spec["script_candidates"])
    if not script_path:
        candidates = ", ".join(spec["script_candidates"])
        raise ValueError(f"Cannot find {candidates} in {root}")

    cmd: list[str] = [python or sys.executable, script_path]

    for field in spec["fields"]:
        name = field["name"]
        ftype = field["type"]
        style = field["style"]
        raw = values.get(name)
        coerced = _coerce(raw, ftype)

        if coerced is None:
            if field.get("required"):
                raise ValueError(f"Missing required field: {name}")
            continue

        if style == "flag":
            if coerced:
                cmd.append(f"--{name}")
            continue

        # style == "kv"
        if ftype == "bool":
            cmd.append(f"--{name}={'True' if coerced else 'False'}")
        else:
            cmd.append(f"--{name}={coerced}")

    return cmd
