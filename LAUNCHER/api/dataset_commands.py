"""Dataset-pipeline command builder.

Mirror of LAUNCHER.api.training for the dataset-management subprocess
steps (prepare archives, upgrade replays, parse, compute stats, create
dataset, character filter). Each script has its own field schema so the
frontend can render a generic form and the backend can serialize flags
in the format each script expects.

Step 5 (filter by stats) is handled synchronously in
LAUNCHER.api.routes.dataset — it queries parsed.sqlite directly rather
than spawning a subprocess.

Step 6 (create dataset) is a chain — convert_sqlite_to_parsed.py then
make_local_dataset.py — wrapped by create_chain.py so process_manager
sees one PID with merged logs.
"""

import os
import sys
from typing import Any

from LAUNCHER.config import find_script

# Absolute path to the chain wrapper. Pre-resolved here (rather than via
# find_script) because the wrapper lives in this package, not in the
# slippi-ai repo. Using an absolute path means the subprocess doesn't need
# LAUNCHER on its sys.path.
_CREATE_CHAIN_SCRIPT = os.path.join(os.path.dirname(__file__), "create_chain.py")


# Each field entry:
#   name       — flag name, passed as --{name}
#   type       — "str" | "int" | "float" | "bool"
#   style      — "kv"   → always emit --name=value
#                "flag" → emit bare --name only when value is truthy (bools)
#   required   — validation hint; unset or empty required string => error
#   multi      — when truthy, the field accepts a list of values; the
#                builder emits one --name=value pair per non-empty entry
#                (target script must use absl DEFINE_multi_string or similar)
# Display-only hints consumed by the frontend (ignored by build_command):
#   label      — human label (defaults to name; also used in error messages)
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
                "label": "Source directories", "browse": "dir", "multi": True,
                "help": "Add one or more folders. Subdirs of .slp files should "
                        "be named yyyy-mm (e.g. 2024-04). Existing .7z/.zip "
                        "archives are copied as-is.",
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
    "create_dataset": {
        "label": "Create Dataset",
        "description": (
            "Convert parsed.sqlite to parsed.pkl, then build meta.json for "
            "training. Runs the two scripts in sequence. If a stats filter "
            "is applied (Step 5), only matching replays are included."
        ),
        # Special-cased in build_command: chains convert_sqlite_to_parsed.py
        # and make_local_dataset.py via the bundled create_chain.py wrapper.
        "_chain": True,
        "fields": [
            {
                "name": "root", "type": "str", "style": "kv", "required": True,
                "label": "Dataset root", "browse": "dir",
                "config_default": "dataset:dataset_root",
            },
            {
                "name": "winner_only", "type": "bool", "style": "kv", "default": True,
                "label": "Winner only",
                "help": "Only include the winning player from each game (recommended).",
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


def _coerce_one(value: Any, ftype: str) -> Any:
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


def _coerce(value: Any, ftype: str, multi: bool = False) -> Any:
    """Coerce a raw frontend value to the script's expected Python type.

    For multi-valued fields, returns a list with empty/None entries dropped,
    or None if no entries remain (so required-validation can flag it).
    """
    if not multi:
        return _coerce_one(value, ftype)
    if value is None:
        return None
    if not isinstance(value, list):
        value = [value]
    out = []
    for v in value:
        c = _coerce_one(v, ftype)
        if c is not None and c != "":
            out.append(c)
    return out if out else None


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

    # Step 6 chain: resolve both target scripts up front and pass them to
    # the bundled wrapper, which inherits process_manager's stdout/stderr
    # so log output from both subprocesses lands in a single feed.
    if spec.get("_chain") and script_key == "create_dataset":
        return _build_create_dataset_command(values, root, python)

    script_path = find_script(root, *spec["script_candidates"])
    if not script_path:
        candidates = ", ".join(spec["script_candidates"])
        raise ValueError(f"Cannot find {candidates} in {root}")

    cmd: list[str] = [python or sys.executable, script_path]

    for field in spec["fields"]:
        name = field["name"]
        ftype = field["type"]
        style = field["style"]
        multi = bool(field.get("multi"))
        raw = values.get(name)
        coerced = _coerce(raw, ftype, multi=multi)

        if coerced is None:
            if field.get("required"):
                label = field.get("label", name)
                raise ValueError(f"Missing required field: {label}")
            continue

        if style == "flag":
            if coerced:
                cmd.append(f"--{name}")
            continue

        # style == "kv"
        if multi:
            for v in coerced:
                cmd.append(f"--{name}={v}")
            continue
        if ftype == "bool":
            cmd.append(f"--{name}={'True' if coerced else 'False'}")
        else:
            cmd.append(f"--{name}={coerced}")

    return cmd


def _build_create_dataset_command(
    values: dict[str, Any], root: str, python: str | None,
) -> list[str]:
    """Resolve the chain wrapper command for Step 6 (Create Dataset)."""
    dataset_root = _coerce(values.get("root"), "str")
    if dataset_root is None:
        raise ValueError("Missing required field: Dataset root")
    convert = find_script(root, "slippi_db/scripts/convert_sqlite_to_parsed.py")
    if not convert:
        raise ValueError(
            "Cannot find slippi_db/scripts/convert_sqlite_to_parsed.py in " + root)
    make = find_script(root, "slippi_db/scripts/make_local_dataset.py")
    if not make:
        raise ValueError(
            "Cannot find slippi_db/scripts/make_local_dataset.py in " + root)

    winner_only = _coerce(values.get("winner_only", True), "bool")
    return [
        python or sys.executable,
        _CREATE_CHAIN_SCRIPT,
        f"--convert_script={convert}",
        f"--make_script={make}",
        f"--root={dataset_root}",
        f"--winner_only={'True' if winner_only else 'False'}",
    ]
