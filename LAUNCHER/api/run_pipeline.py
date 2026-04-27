"""End-to-end Prepare -> Upgrade -> Parse orchestrator.

Runs the three pipeline steps in sequence as a single subprocess so the
launcher's process_manager sees one PID with merged stdout. The parent
build_command resolves the slippi-ai script paths up front and passes
them as args, so this wrapper does not depend on LAUNCHER being on the
sys.path; it does import ``slippi_db`` (which is a real package) for
the in-process .7z->.zip self-heal pass.

Re-run safety is delegated to the underlying scripts: prepare dedups by
archive name in Raw/, upgrade by (archive, name) in upgrades.sqlite,
parse by slp_md5 in parsed.sqlite. The orchestrator itself adds:

  * a lockfile at <root>/.pipeline.lock to refuse concurrent runs
  * a free-space precheck before any work touches disk
  * a self-healing pre-upgrade pass that transcodes stranded .7z
    archives in Raw/ to .zip (the upgrade step only reads .zip)
  * a one-line summary read from upgrades.sqlite after the upgrade run
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys

from slippi_db import archive_utils

LOCK_FILENAME = ".pipeline.lock"


def _read_pid(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # tasklist exits 0 if the PID exists, even if the process is a zombie.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock(root: str) -> str:
    os.makedirs(root, exist_ok=True)
    lock_path = os.path.join(root, LOCK_FILENAME)
    if os.path.exists(lock_path):
        existing = _read_pid(lock_path)
        if existing and _pid_alive(existing):
            raise RuntimeError(
                f"Pipeline already running for this dataset (PID {existing}). "
                f"If that's wrong, delete {lock_path} and retry.")
        # Stale lock — overwrite.
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return lock_path


def _release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _sum_source_size(sources: list[str]) -> int:
    total = 0
    for src in sources:
        if not os.path.isdir(src):
            continue
        for dirpath, _, filenames in os.walk(src):
            for f in filenames:
                p = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    return total


def _disk_precheck(root: str, sources: list[str]) -> None:
    src_size = _sum_source_size(sources)
    if src_size == 0:
        # Nothing to copy — let the prepare step report "no work" itself.
        return
    free = shutil.disk_usage(root).free
    needed = int(src_size * 1.5)
    if free < needed:

        def _gb(n: int) -> str:
            return f"{n / (2**30):.1f} GB"

        raise RuntimeError(
            f"Not enough free space in {root}: need ~{_gb(needed)} "
            f"(1.5x the {_gb(src_size)} of source data), have {_gb(free)}.")


def _transcode_stranded_archives(root: str) -> None:
    """Convert any Raw/*.7z to Raw/*.zip in place.

    Idempotent — does nothing if no .7z files are in Raw/. The upgrade
    step only reads .zip; without this pass, .7z archives left over
    from earlier runs (when prepare's default was 7z) would silently
    bypass upgrade.
    """
    raw_dir = os.path.join(root, "Raw")
    if not os.path.isdir(raw_dir):
        return
    sevenz = [f for f in os.listdir(raw_dir) if f.lower().endswith(".7z")]
    if not sevenz:
        return
    print(
        f"[pre-upgrade] Transcoding {len(sevenz)} stranded .7z archive(s) "
        f"in Raw/ to .zip so the upgrade step can read them.",
        flush=True)
    for fname in sorted(sevenz):
        src = os.path.join(raw_dir, fname)
        dst = os.path.join(raw_dir, os.path.splitext(fname)[0] + ".zip")
        if os.path.exists(dst):
            print(f"  SKIPPING: {fname} (matching .zip already exists)",
                  flush=True)
            continue
        print(f"  TRANSCODING: {fname} -> {os.path.basename(dst)}", flush=True)
        try:
            archive_utils.transcode_archive(src, dst, target_format="zip")
        except Exception as e:
            print(f"  WARNING: transcode of {fname} failed: {e}; leaving as-is.",
                  flush=True)
            continue
        try:
            os.remove(src)
        except OSError as e:
            print(f"  WARNING: could not remove {fname}: {e}", flush=True)


def _print_upgrade_summary(root: str) -> None:
    db_path = os.path.join(root, "upgrades.sqlite")
    if not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT result, COUNT(*) FROM upgrades GROUP BY result"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"[upgrade summary] could not read upgrades.sqlite: {e}",
              flush=True)
        return
    counts = {result: n for result, n in rows}
    total = sum(counts.values())
    ok = counts.get("success", 0)
    err = counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    print(
        f"[upgrade summary] {ok}/{total} ok, {err} errors, {skipped} skipped",
        flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run prepare -> upgrade -> parse end to end.")
    p.add_argument("--prepare_script", required=True,
                   help="Absolute path to slippi_db/scripts/prepare_local.py")
    p.add_argument("--upgrade_script", required=True,
                   help="Absolute path to slippi_db/scripts/upgrade_slps.py")
    p.add_argument("--parse_script", required=True,
                   help="Absolute path to slippi_db/parse_local.py")
    p.add_argument("--source", action="append", default=[], required=True,
                   help="Source directory containing .slp files; may be "
                        "repeated to combine multiple folders into one dataset.")
    p.add_argument("--root", required=True, help="Dataset destination root.")
    p.add_argument("--dolphin", required=True, help="Path to Dolphin install.")
    p.add_argument("--iso", required=True, help="Path to SSBM ISO.")
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args()

    if not args.source:
        print("error: at least one --source is required.", file=sys.stderr)
        return 2

    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)

    try:
        _disk_precheck(root, args.source)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        lock_path = _acquire_lock(root)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        # ── Step 1: Prepare ────────────────────────────────────────────────
        print(f"[1/3] Prepare: {len(args.source)} source dir(s) -> {root}/Raw/",
              flush=True)
        prepare_cmd = [
            sys.executable, args.prepare_script,
            f"--zip_root={root}",
            "--archive_format=zip",
        ]
        for src in args.source:
            prepare_cmd.append(f"--slp_root={src}")
        rc = subprocess.run(prepare_cmd).returncode
        if rc != 0:
            print(f"prepare_local.py exited {rc}; aborting pipeline.",
                  file=sys.stderr, flush=True)
            return rc

        # Self-heal any pre-existing stranded .7z archives in Raw/ before
        # upgrade — those would otherwise be silently skipped.
        _transcode_stranded_archives(root)

        # ── Step 2: Upgrade ────────────────────────────────────────────────
        print(f"[2/3] Upgrade: replays in {root}/Raw/ -> {root}/Upgraded/",
              flush=True)
        upgrade_cmd = [
            sys.executable, args.upgrade_script,
            f"--root={root}",
            f"--dolphin={args.dolphin}",
            f"--iso={args.iso}",
            f"--threads={args.threads}",
        ]
        rc = subprocess.run(upgrade_cmd).returncode
        if rc != 0:
            print(f"upgrade_slps.py exited {rc}; aborting pipeline.",
                  file=sys.stderr, flush=True)
            return rc
        _print_upgrade_summary(root)

        # ── Step 3: Parse ──────────────────────────────────────────────────
        print(f"[3/3] Parse: building parsed.sqlite + Parsed/ in {root}",
              flush=True)
        parse_cmd = [
            sys.executable, args.parse_script,
            f"--root={root}",
            f"--threads={args.threads}",
        ]
        rc = subprocess.run(parse_cmd).returncode
        if rc != 0:
            print(f"parse_local.py exited {rc}.", file=sys.stderr, flush=True)
        else:
            print("Pipeline complete.", flush=True)
        return rc
    finally:
        _release_lock(lock_path)


if __name__ == "__main__":
    sys.exit(main())
