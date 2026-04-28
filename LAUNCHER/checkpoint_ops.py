"""Programmatic operations on agent checkpoint .pkl files.

Two distinct operations live here so the CLIs (``scripts/fix_rl_checkpoint.py``,
``scripts/strip_models.py``) and the launcher API can share them:

* ``inspect_checkpoint`` / ``apply_repair`` — patch the
  ``rl_config.opponent.other`` char/name lists when they get out of sync,
  which would otherwise crash on restore. Documented failure mode for users
  who hand-edit configs or who hit the legacy ``check_allowed_chars`` bug
  that auto-filled chars without extending names.

* ``strip_one`` — drop everything from a checkpoint's ``state`` dict except
  the ``policy`` key, producing an inference-only model that's much smaller
  to share. Used by both the batch CLI and the GUI's "Export for sharing"
  action.
"""

import itertools
import os
import pickle
from typing import Any


# ── Repair modes (string constants over enums so the API can use them
# directly without serialization fuss) ─────────────────────────────────
MODE_CYCLE_NAMES = "cycle_names"
MODE_CLEAR_BOTH = "clear_both"
MODE_CLEAR_CHARS = "clear_chars"
MODE_CLEAR_NAMES = "clear_names"
MODE_CUSTOM_NAMES = "custom_names"

REPAIR_MODES = (
    MODE_CYCLE_NAMES,
    MODE_CLEAR_BOTH,
    MODE_CLEAR_CHARS,
    MODE_CLEAR_NAMES,
    MODE_CUSTOM_NAMES,
)


def _char_names(chars: list[Any]) -> list[str]:
    return [c.name if hasattr(c, 'name') else str(c) for c in chars]


def _name_map_summary(state: dict) -> list[dict]:
    """Group ``state['name_map']`` by index, returning one entry per player.

    Each entry: ``{"index": int, "canonical": str, "aliases": [str]}``.
    Empty list if the checkpoint has no name_map.
    """
    name_map = state.get('name_map')
    if not name_map:
        return []
    by_index: dict[int, list[str]] = {}
    for name, idx in name_map.items():
        by_index.setdefault(idx, []).append(name)
    return [
        {"index": idx, "canonical": names[0], "aliases": names[1:]}
        for idx, names in sorted(by_index.items())
    ]


def inspect_checkpoint(path: str) -> dict:
    """Return the current opponent.other config + name_map for ``path``.

    Shape (every field always present so the GUI doesn't have to branch):

        {
            "has_rl_config": bool,
            "chars": ["FOX", ...],     # opponent.other.char list, [] if None
            "names": [...],            # opponent.other.name list, [] if None
            "matched": bool,           # len(chars)==len(names) and len>0
            "name_map": [{"index", "canonical", "aliases"}, ...],
        }
    """
    with open(path, 'rb') as f:
        state = pickle.load(f)
    return _summarize(state)


def _summarize(state: dict) -> dict:
    rl_config = state.get('rl_config')
    if not rl_config:
        return {
            "has_rl_config": False,
            "chars": [],
            "names": [],
            "matched": False,
            "name_map": _name_map_summary(state),
        }
    other = rl_config.get('opponent', {}).get('other', {})
    chars = other.get('char') or []
    names = other.get('name') or []
    return {
        "has_rl_config": True,
        "chars": _char_names(chars),
        "names": list(names),
        "matched": bool(chars) and len(chars) == len(names),
        "name_map": _name_map_summary(state),
    }


def apply_repair(
    path: str,
    mode: str,
    custom_names: list[str] | None = None,
    save: bool = True,
) -> dict:
    """Apply a repair ``mode`` to the checkpoint at ``path``.

    For ``MODE_CUSTOM_NAMES``, ``custom_names`` must be non-empty; the list
    is cycled to match the existing char count. Other modes ignore it.

    Returns the post-repair summary plus a ``"changed": bool`` flag.
    Raises ``ValueError`` on invalid mode / missing prerequisites.
    """
    if mode not in REPAIR_MODES:
        raise ValueError(f"Unknown repair mode: {mode}")

    with open(path, 'rb') as f:
        state = pickle.load(f)

    rl_config = state.get('rl_config')
    if not rl_config:
        raise ValueError("No rl_config in checkpoint — nothing to repair.")

    opponent = rl_config.setdefault('opponent', {})
    other = opponent.setdefault('other', {})
    chars = other.get('char')
    names = other.get('name')

    if mode == MODE_CYCLE_NAMES:
        if not chars:
            raise ValueError("No opponent chars to cycle names against.")
        seed = list(names) if names else ['Master Player']
        other['name'] = list(itertools.islice(itertools.cycle(seed), len(chars)))
    elif mode == MODE_CLEAR_BOTH:
        other['char'] = None
        other['name'] = ['Master Player']
    elif mode == MODE_CLEAR_CHARS:
        other['char'] = None
    elif mode == MODE_CLEAR_NAMES:
        other['name'] = ['Master Player']
    elif mode == MODE_CUSTOM_NAMES:
        if not chars:
            raise ValueError("No opponent chars set — cannot apply custom names.")
        if not custom_names:
            raise ValueError("Custom names list is required for this mode.")
        cleaned = [n.strip() for n in custom_names if n and n.strip()]
        if not cleaned:
            raise ValueError("Custom names list is empty after trimming.")
        other['name'] = list(itertools.islice(
            itertools.cycle(cleaned), len(chars)))

    if save:
        with open(path, 'wb') as f:
            pickle.dump(state, f)

    result = _summarize(state)
    result["changed"] = True
    return result


# ── Strip ──────────────────────────────────────────────────────────────

def strip_one(src_path: str, dst_path: str) -> dict:
    """Write an inference-only copy of ``src_path`` to ``dst_path``.

    Drops every key in ``state`` except ``policy``. Returns size info so
    callers can surface the savings.
    """
    os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
    with open(src_path, 'rb') as f:
        combined_state = pickle.load(f)
    combined_state['state'] = {'policy': combined_state['state']['policy']}
    with open(dst_path, 'wb') as f:
        pickle.dump(combined_state, f)
    return {
        "src_path": src_path,
        "dst_path": dst_path,
        "src_size": os.path.getsize(src_path),
        "dst_size": os.path.getsize(dst_path),
    }
