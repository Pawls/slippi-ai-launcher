"""Interactive CLI to fix RL checkpoints with mismatched opponent char/name lists.

Usage:
  python scripts/fix_rl_checkpoint.py <checkpoint.pkl>

Repair logic lives in ``LAUNCHER.checkpoint_ops`` so the launcher's "Repair
checkpoint" UI shares the same modes as this CLI.
"""

import os
import sys

# When invoked as ``python scripts/fix_rl_checkpoint.py``, Python only puts
# the script's directory on sys.path — not the repo root — so the LAUNCHER
# package isn't importable. Add the repo root explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from LAUNCHER.checkpoint_ops import (
    inspect_checkpoint, apply_repair,
    MODE_CYCLE_NAMES, MODE_CLEAR_BOTH, MODE_CLEAR_CHARS, MODE_CLEAR_NAMES,
    MODE_CUSTOM_NAMES,
)


def _show_current(info: dict):
    print(f'  chars ({len(info["chars"])}): {info["chars"]}')
    print(f'  names ({len(info["names"])}): {info["names"]}')


def _show_name_map(info: dict):
    entries = info.get("name_map") or []
    if not entries:
        print('  (name_map not found in this checkpoint)')
        return
    print(f'\n  Available names ({len(entries)} unique players):')
    for entry in entries:
        idx = entry["index"]
        canon = entry["canonical"]
        aliases = entry["aliases"]
        if aliases:
            print(f'    [{idx:3d}] {canon}  (aliases: {", ".join(aliases)})')
        else:
            print(f'    [{idx:3d}] {canon}')


def _interactive_loop(path: str):
    info = inspect_checkpoint(path)

    if not info["has_rl_config"]:
        print('No rl_config found in checkpoint.')
        return

    print('\nCurrent opponent.other config:')
    _show_current(info)

    if info["matched"]:
        print('\nChar and name lists already match.')

    print('\nNote: Names are fed into the neural network as input features.')
    print('They affect opponent playstyle (e.g. "Hax" plays differently from "Zain").')
    print('"Master Player" is a valid generic style. Empty/blank names are NOT safe.\n')

    print('Options:')
    print('  [1] Cycle names to match chars (extend short list)')
    print('  [2] Clear both chars and names (reset to None/default)')
    print('      (check_allowed_chars will re-derive them on next run)')
    print('  [3] Clear chars only (keep names)')
    print('  [4] Clear names only (reset to ["Master Player"])')
    print('  [5] Set custom names (comma-separated, must match char count)')
    print('  [6] Show available names from the model\'s name_map')
    if info["matched"]:
        print('  [7] No changes (exit)')
    choice = input('\nChoice: ').strip()

    mode_for_choice = {
        '1': MODE_CYCLE_NAMES,
        '2': MODE_CLEAR_BOTH,
        '3': MODE_CLEAR_CHARS,
        '4': MODE_CLEAR_NAMES,
    }
    custom_names: list[str] | None = None

    if choice == '6':
        _show_name_map(info)
        print('\nReturning to options...')
        return _interactive_loop(path)
    if choice == '7' and info["matched"]:
        print('No changes.')
        return
    if choice == '5':
        if not info["chars"]:
            print('No chars set. Set chars first or use option [2] to clear both.')
            return
        print(f'\nCurrent chars ({len(info["chars"])}): {info["chars"]}')
        print(f'Enter {len(info["chars"])} names, comma-separated.')
        print('Names will be cycled if you provide fewer than needed.')
        print('Example: Hax,Zain,Amsa,Cody,Frenzy,S2J,Moky,Nez,Ginger,Axe,KJH,Nicki')
        raw = input('\nNames: ').strip()
        if not raw:
            print('No names entered. No changes made.')
            return
        custom_names = [n.strip() for n in raw.split(',') if n.strip()]
        if not custom_names:
            print('No valid names parsed. No changes made.')
            return
        mode = MODE_CUSTOM_NAMES
    elif choice in mode_for_choice:
        mode = mode_for_choice[choice]
    else:
        print('Invalid choice. No changes made.')
        return

    confirm = input('\nSave changes? [y/N]: ').strip().lower()
    if confirm != 'y':
        print('Aborted.')
        return

    try:
        result = apply_repair(path, mode, custom_names=custom_names)
    except ValueError as e:
        print(f'Repair failed: {e}')
        return

    print('\nResult:')
    _show_current(result)
    print(f'Saved fixed checkpoint to {path}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <checkpoint.pkl>')
        sys.exit(1)
    _interactive_loop(sys.argv[1])
