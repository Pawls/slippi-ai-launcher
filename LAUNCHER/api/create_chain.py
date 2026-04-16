"""Wrapper that runs convert_sqlite_to_parsed.py then make_local_dataset.py.

Lives in the launcher (not slippi-ai) so the GUI's process_manager owns
a single PID with merged logs from both scripts. The wrapper itself is
standalone and stdlib-only — the launcher's build_command resolves the
two slippi-ai script paths up front and passes them as args, so this
script doesn't need LAUNCHER on its sys.path when invoked as a
subprocess.

Auto-detects ``stats_filter.json`` in the dataset root: when present,
make_local_dataset is invoked with ``--stats_filter=<path>`` so only
replays selected by Step 5 land in the generated dataset.
"""

import argparse
import os
import subprocess
import sys


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Convert parsed.sqlite to parsed.pkl and build meta.json")
    p.add_argument("--convert_script", required=True,
                   help="Absolute path to slippi_db/scripts/convert_sqlite_to_parsed.py")
    p.add_argument("--make_script", required=True,
                   help="Absolute path to slippi_db/scripts/make_local_dataset.py")
    p.add_argument("--root", required=True,
                   help="Dataset root containing parsed.sqlite")
    p.add_argument("--winner_only", default="True",
                   help='Pass --winner_only=False through to make_local_dataset.py.')
    args = p.parse_args()

    print(f"[1/2] Converting parsed.sqlite -> parsed.pkl in {args.root}", flush=True)
    rc = subprocess.run(
        [sys.executable, args.convert_script, f"--root={args.root}"]
    ).returncode
    if rc != 0:
        print(f"convert_sqlite_to_parsed.py exited {rc}; aborting chain.",
              file=sys.stderr, flush=True)
        return rc

    print(f"[2/2] Building meta.json", flush=True)
    cmd = [sys.executable, args.make_script, f"--root={args.root}"]
    if not _truthy(args.winner_only):
        cmd.append("--winner_only=False")
    sf = os.path.join(args.root, "stats_filter.json")
    if os.path.isfile(sf):
        cmd.append(f"--stats_filter={sf}")
        print(f"      applying stats_filter.json ({sf})", flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"make_local_dataset.py exited {rc}.",
              file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
