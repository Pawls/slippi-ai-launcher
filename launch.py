"""Launch the Slippi AI GUI."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "LAUNCHER" / "slippi_launcher.py"), run_name="__main__")
