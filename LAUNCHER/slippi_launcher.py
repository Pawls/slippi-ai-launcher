"""
Slippi AI Launcher
====================
Entry point for the Slippi AI GUI application.

Can be run directly:  python LAUNCHER/slippi_launcher.py
Or via the package:   python -m LAUNCHER
Or via launch.py:     python launch.py
"""

import sys
import os

# Ensure the repo root is on sys.path so "from LAUNCHER..." imports work
# when this script is run directly (not via python -m).
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
  sys.path.insert(0, _repo_root)

from LAUNCHER.app import main

if __name__ == "__main__":
  main()
