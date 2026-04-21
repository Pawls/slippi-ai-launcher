"""Entry point for the API server: python -m LAUNCHER.api"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def _project_root() -> Path:
    """Path to the slippi-ai repo root (contains ``slippi_ai/``, ``scripts/``)."""
    # __main__.py lives at <root>/LAUNCHER/api/__main__.py
    return Path(__file__).resolve().parent.parent.parent


def _repair_venv_shebangs() -> None:
    """Rewrite stale shebang lines in ``.venv/bin/*`` after a folder move.

    ``pip`` / ``setuptools`` bake the absolute path to the venv's Python
    interpreter into every console-script wrapper (``pip``, ``fastapi``,
    ``tensorboard`` etc.) as the ``#!`` line. Moving the venv leaves those
    wrappers pointing at a non-existent path, so ``.venv/bin/pip`` fails with
    ``bad interpreter: No such file or directory`` even though
    ``.venv/bin/python`` (a symlink) still works.

    We look for any ``#!<dir>/python…`` shebang whose directory isn't the
    current ``.venv/bin`` and rewrite just that prefix. Symlinks and
    non-shebang files are skipped. No-op when everything is already correct.
    """
    venv_bin = _project_root() / ".venv" / "bin"
    if not venv_bin.is_dir():
        return
    target_dir = str(venv_bin)
    repaired = 0
    for entry in venv_bin.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            data = entry.read_bytes()
        except OSError:
            continue
        if not data.startswith(b"#!"):
            continue
        nl = data.find(b"\n")
        if nl < 0:
            continue
        try:
            shebang = data[:nl].decode("ascii")
        except UnicodeDecodeError:
            continue
        m = re.match(r"^#!(.+)/(python[\w.]*)\s*$", shebang)
        if not m:
            continue
        dir_part, python_binary = m.group(1), m.group(2)
        if dir_part == target_dir:
            continue
        new_shebang = f"#!{target_dir}/{python_binary}".encode("ascii")
        try:
            entry.write_bytes(new_shebang + data[nl:])
            repaired += 1
        except OSError:
            pass
    if repaired:
        print(
            f"[LAUNCHER] rewrote {repaired} stale shebang(s) under {target_dir}",
            file=sys.stderr, flush=True,
        )


def _repair_editable_install_if_stale() -> None:
    """Re-run ``pip install -e .`` when the editable install's absolute paths
    no longer match the current project directory.

    Setuptools' PEP 660 editable installs hardcode absolute package paths into
    a generated finder module under ``site-packages``. If the user moves or
    renames the project directory after install (common when reorganising
    into a parent folder), those paths dangle — Python silently falls back to
    treating ``slippi_ai`` as an empty namespace package, and subsequent
    ``from slippi_ai import eval_lib`` fails with a confusing "unknown
    location" ImportError. The fix is identical every time (reinstall
    editable from the new location), so we detect and auto-repair on boot
    rather than forcing each user to diagnose it.

    Skipped silently on any unexpected condition — we'd rather boot with a
    stale install and surface the real error later than block startup on a
    heuristic that misfires.
    """
    try:
        import site
        root = _project_root()
        # Find the finder module that setuptools generates for this package.
        # Name pattern: __editable___<pkg>_<version>_finder.py
        finder_path = None
        for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
            p = Path(site_dir)
            if not p.is_dir():
                continue
            matches = list(p.glob("__editable___slippi_ai_*_finder.py"))
            if matches:
                finder_path = matches[0]
                break
        if finder_path is None:
            return  # Not an editable install (wheel install, or not installed).

        text = finder_path.read_text(encoding="utf-8", errors="ignore")
        # If every hardcoded path lives under the current project root we're
        # fine. Otherwise the install points somewhere stale.
        #
        # Setuptools writes paths as Python string literals — on Unix:
        # ``'/home/you/project/slippi_ai'``; on Windows with escaped
        # backslashes: ``'C:\\MELEE\\...\\slippi_ai'``. Capture both by
        # extracting every single-quoted token and normalising separators
        # before comparing.
        import re
        raw = re.findall(r"'([^'\n]+)'", text)
        def _norm(p: str) -> str:
            # Decode the escaped backslashes setuptools writes into the
            # source literal, then canonicalise slashes and case for the
            # Windows-insensitive filesystem.
            return p.replace("\\\\", "\\").replace("\\", "/").lower()
        def _is_absolute(p: str) -> bool:
            # Windows (C:\…, C:/…) or POSIX (/…). Bare module names like
            # 'slippi_ai' appear in the finder too and must be ignored.
            if p.startswith("/"):
                return True
            return len(p) >= 2 and p[1] == ":"
        expected_prefix = _norm(str(root))
        project_paths = [
            _norm(p) for p in raw
            if ("slippi_ai" in p or "slippi_db" in p) and _is_absolute(p)
        ]
        if project_paths and all(p.startswith(expected_prefix) for p in project_paths):
            return  # Healthy.

        print(
            f"[LAUNCHER] editable install paths point outside {expected_prefix!r}; "
            f"re-running pip install -e . to repair...",
            file=sys.stderr, flush=True,
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".",
             "--no-deps", "--no-build-isolation", "--quiet"],
            cwd=str(root),
            check=False,
        )
    except Exception as e:
        print(f"[LAUNCHER] editable-install repair check skipped: {e}",
              file=sys.stderr, flush=True)


_repair_editable_install_if_stale()
_repair_venv_shebangs()

import uvicorn  # noqa: E402

from LAUNCHER.api.app import create_app  # noqa: E402

app = create_app()


def main(host: str | None = None, port: int | None = None):
    parser = argparse.ArgumentParser(
        prog="python -m LAUNCHER.api",
        description="Slippi AI Launcher API server",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SLIPPI_API_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 to accept "
             "connections from another host, e.g. a Windows frontend talking "
             "to a backend running inside WSL).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SLIPPI_API_PORT", "8000")),
        help="Listen port (default: 8000).",
    )
    args = parser.parse_args()

    final_host = host if host is not None else args.host
    final_port = port if port is not None else args.port
    uvicorn.run(app, host=final_host, port=final_port)


if __name__ == "__main__":
    main()
