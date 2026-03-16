"""Fix RL checkpoint with mismatched opponent char/name lists.

Usage:
  python fix_rl_checkpoint.py <checkpoint.pkl>

This fixes a bug where check_allowed_chars() auto-filled opponent characters
without extending the name list to match, causing deserialization to fail
on restore.
"""

import itertools
import pickle
import sys


def show_current(chars, names):
    char_names = [c.name if hasattr(c, 'name') else str(c) for c in chars]
    print(f'  chars ({len(chars)}): {char_names}')
    print(f'  names ({len(names)}): {names}')


def show_name_map(state):
    """Show available names from the opponent model's name_map."""
    # name_map is at the top level of the state dict
    name_map = state.get('name_map')

    if name_map is None:
        print('  (name_map not found in this checkpoint)')
        return

    # Group by index to show canonical names + aliases
    by_index: dict[int, list[str]] = {}
    for name, idx in name_map.items():
        by_index.setdefault(idx, []).append(name)

    print(f'\n  Available names ({len(name_map)} entries, {len(by_index)} unique players):')
    for idx in sorted(by_index.keys()):
        aliases = by_index[idx]
        canonical = aliases[0]
        if len(aliases) > 1:
            others = ', '.join(aliases[1:])
            print(f'    [{idx:3d}] {canonical}  (aliases: {others})')
        else:
            print(f'    [{idx:3d}] {canonical}')


def fix_checkpoint(path: str):
    with open(path, 'rb') as f:
        state = pickle.load(f)

    rl_config = state.get('rl_config')
    if rl_config is None:
        print('No rl_config found in checkpoint.')
        return

    opponent = rl_config.get('opponent', {})
    other = opponent.get('other', {})

    chars = other.get('char')
    names = other.get('name')

    if chars is None and names is None:
        print('No opponent char/name lists found.')
        return

    print(f'\nCurrent opponent.other config:')
    show_current(chars or [], names or [])

    matched = chars is not None and names is not None and len(chars) == len(names)
    if matched:
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
    if matched:
        print('  [7] No changes (exit)')
    choice = input('\nChoice: ').strip()

    if choice == '1':
        if not chars:
            print('No chars to match against.')
            return
        if not names:
            names = ['Master Player']
        fixed_names = list(itertools.islice(itertools.cycle(names), len(chars)))
        other['name'] = fixed_names
        print(f'\nFixed:')
        show_current(chars, fixed_names)
    elif choice == '2':
        other['char'] = None
        other['name'] = ['Master Player']
        print('\nCleared both. chars=None, names=["Master Player"]')
        print('check_allowed_chars will re-derive chars and cycle names on next run.')
    elif choice == '3':
        other['char'] = None
        print(f'\nCleared chars. names kept: {names}')
    elif choice == '4':
        other['name'] = ['Master Player']
        print(f'\nCleared names to ["Master Player"]. chars kept.')
        if chars and len(chars) > 1:
            print('WARNING: This will still mismatch on restore.')
            print('         Use option [1] or [2] instead, or set names in your launch script.')
    elif choice == '5':
        if not chars:
            print('No chars set. Set chars first or use option [2] to clear both.')
            return
        char_names = [c.name if hasattr(c, 'name') else str(c) for c in chars]
        print(f'\nCurrent chars ({len(chars)}): {char_names}')
        print(f'Enter {len(chars)} names, comma-separated.')
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
        # Cycle to match char count
        fixed_names = list(itertools.islice(
            itertools.cycle(custom_names), len(chars)))
        other['name'] = fixed_names
        print(f'\nResult:')
        show_current(chars, fixed_names)
    elif choice == '6':
        show_name_map(state)
        print('\nReturning to options...')
        return fix_checkpoint(path)
    elif choice == '7' and matched:
        print('No changes.')
        return
    else:
        print('Invalid choice. No changes made.')
        return

    confirm = input('\nSave changes? [y/N]: ').strip().lower()
    if confirm != 'y':
        print('Aborted.')
        return

    with open(path, 'wb') as f:
        pickle.dump(state, f)

    print(f'Saved fixed checkpoint to {path}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <checkpoint.pkl>')
        sys.exit(1)
    fix_checkpoint(sys.argv[1])
