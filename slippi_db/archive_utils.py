"""Shared archive helpers used by the dataset pipeline.

Two operations are exposed:

  archive_directory(source_dir, dest_archive, archive_format)
      Compress a directory of .slp files into a single archive. Replaces
      the ad-hoc ``7z a -t7z`` invocation that lived inline in
      prepare_local.py.

  transcode_archive(source_archive, dest_archive, target_format)
      Re-encode an existing .zip/.7z into a different format. Used to
      heal datasets that contain stranded .7z archives the upgrade step
      cannot read, and to convert pre-existing .7z files in a user's
      source directory at prepare time.

Both functions write to ``<dest>.tmp`` first and ``os.replace`` on
success, so an interrupted run never leaves a partial archive that
downstream "already exists" checks would skip forever.
"""

import os
import shutil
import subprocess
import tempfile

ARCHIVE_FORMATS = ("zip", "7z")


def _seven_zip_cmd() -> str:
    for candidate in ("7z", "7zz", "7z.exe"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError(
        "7z not found in PATH; install p7zip-full (Linux) or 7-Zip (Windows).")


def _validate_format(name: str, value: str) -> None:
    if value not in ARCHIVE_FORMATS:
        raise ValueError(
            f"{name} must be one of {ARCHIVE_FORMATS!r}, got {value!r}")


def _run_seven_zip(cmd: list[str], cwd: str | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"7z failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")


def archive_directory(source_dir: str, dest_archive: str,
                      archive_format: str = "zip") -> None:
    """Archive *source_dir* (recursive) into *dest_archive*.

    The on-disk layout matches what prepare_local.py has always produced:
    the source directory becomes a top-level entry inside the archive.
    """
    _validate_format("archive_format", archive_format)
    if not os.path.isdir(source_dir):
        raise ValueError(f"source_dir is not a directory: {source_dir}")

    seven_zip = _seven_zip_cmd()
    tmp_path = dest_archive + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    type_arg = f"-t{archive_format}"
    extra = ["-mx=5"] if archive_format == "7z" else []
    cmd = [seven_zip, "a", type_arg, *extra, tmp_path, source_dir]
    try:
        _run_seven_zip(cmd)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    os.replace(tmp_path, dest_archive)


def transcode_archive(source_archive: str, dest_archive: str,
                      target_format: str = "zip") -> None:
    """Convert *source_archive* to *target_format*, preserving entry layout.

    Extracts to a temp directory, then re-archives. Internal paths are
    preserved (e.g. an entry ``2024-04/Game.slp`` stays at
    ``2024-04/Game.slp`` in the output).
    """
    _validate_format("target_format", target_format)
    if not os.path.isfile(source_archive):
        raise ValueError(f"source_archive is not a file: {source_archive}")

    seven_zip = _seven_zip_cmd()
    tmp_path = dest_archive + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    with tempfile.TemporaryDirectory(prefix="slp-transcode-") as tmpdir:
        # `x` preserves directory structure; -y auto-confirms any prompts.
        _run_seven_zip(
            [seven_zip, "x", f"-o{tmpdir}", "-y", source_archive])

        items = os.listdir(tmpdir)
        if not items:
            raise RuntimeError(
                f"Source archive extracted empty: {source_archive}")

        type_arg = f"-t{target_format}"
        extra = ["-mx=5"] if target_format == "7z" else []
        # cwd=tmpdir keeps internal paths relative inside the new archive.
        cmd = [seven_zip, "a", type_arg, *extra, tmp_path, *items]
        try:
            _run_seven_zip(cmd, cwd=tmpdir)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    os.replace(tmp_path, dest_archive)
