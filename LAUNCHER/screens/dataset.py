"""Dataset Management screen: full pipeline from raw .slp files to training dataset."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from LAUNCHER.config import AppConfig, find_script
from LAUNCHER.screens import Screen


# ──────────────────────────────────────────────────────────────────────────────
# StepCard: reusable widget for a single pipeline step
# ──────────────────────────────────────────────────────────────────────────────

class StepCard(ttk.LabelFrame):
  """A pipeline step with input fields, Run/Stop button, and status."""

  def __init__(self, parent, win: tk.Tk, title: str, description: str, **kw):
    super().__init__(parent, text=title, padding=10, **kw)
    self._win = win
    self._proc: subprocess.Popen | None = None

    # Description
    if description:
      ttk.Label(self, text=description,
                foreground="gray", font=("TkDefaultFont", 8),
                wraplength=500, justify="left").pack(anchor="w", pady=(0, 6))

    # Content frame for step-specific widgets
    self.content = ttk.Frame(self)
    self.content.pack(fill="x")

    # Bottom row: status + run button
    bottom = ttk.Frame(self)
    bottom.pack(fill="x", pady=(8, 0))

    self._status_var = tk.StringVar(value="Ready")
    self._status_label = ttk.Label(bottom, textvariable=self._status_var,
                                    foreground="gray", font=("TkDefaultFont", 8))
    self._status_label.pack(side="left")

    self._run_btn = ttk.Button(bottom, text="Run", command=self._on_run)
    self._run_btn.pack(side="right")

    # Override this in subclass or set after init
    self._build_command = None

  def set_command_builder(self, fn):
    """Set a callable that returns a list[str] command or None on validation error."""
    self._build_command = fn

  def _on_run(self):
    if self._proc and self._proc.poll() is None:
      self._kill()
      return

    if self._build_command is None:
      return

    cmd = self._build_command()
    if cmd is None:
      return

    self._run(cmd)

  def _run(self, cmd):
    root = self._get_cwd()
    try:
      if sys.platform == "win32":
        self._proc = subprocess.Popen(cmd, cwd=root)
      else:
        self._proc = subprocess.Popen(cmd, cwd=root, start_new_session=True)
    except Exception as exc:
      self._status_var.set(f"Error: {exc}")
      self._status_label.config(foreground="red")
      return

    self._run_btn.config(text="Stop")
    self._status_var.set("Running...")
    self._status_label.config(foreground="orange")
    threading.Thread(target=self._watch, daemon=True).start()

  def _get_cwd(self) -> str:
    """Return the working directory for subprocess. Override if needed."""
    # Default: try to find repo root from config
    return os.getcwd()

  def _watch(self):
    if self._proc:
      self._proc.wait()
    self._win.after(0, self._on_complete)

  def _on_complete(self):
    rc = self._proc.returncode if self._proc else -1
    self._proc = None
    self._run_btn.config(text="Run")
    if rc == 0:
      self._status_var.set("Complete")
      self._status_label.config(foreground="green")
    else:
      self._status_var.set(f"Failed (exit code {rc})")
      self._status_label.config(foreground="red")

  def _kill(self):
    if self._proc:
      try:
        if sys.platform == "win32":
          subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
          import signal
          os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
      except Exception:
        try:
          self._proc.kill()
        except Exception:
          pass
      self._proc = None
      self._run_btn.config(text="Run")
      self._status_var.set("Stopped")
      self._status_label.config(foreground="gray")


def _has_windows_fs() -> bool:
  """Check if a Windows filesystem is accessible via /mnt/."""
  try:
    # Check for at least one drive letter directory under /mnt/
    if not os.path.isdir("/mnt"):
      return False
    return any(
      len(d) == 1 and d.isalpha() and os.path.isdir(os.path.join("/mnt", d))
      for d in os.listdir("/mnt")
    )
  except Exception:
    return False

_HAS_WINDOWS_FS = _has_windows_fs()


def _dir_field(parent, label: str, var: tk.StringVar, row: int,
               hint: str = "", windows_browse: bool = False,
               project_root: str = "") -> None:
  """Add a label + entry + browse button row to a grid."""
  ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
  ttk.Entry(parent, textvariable=var, width=44).grid(
    row=row, column=1, padx=4, pady=2)
  btn_frame = ttk.Frame(parent)
  btn_frame.grid(row=row, column=2, pady=2)
  ttk.Button(btn_frame, text="Browse\u2026",
             command=lambda: _pick_dir(var, initialdir=project_root or None)
             ).pack(side="left")
  if _HAS_WINDOWS_FS and windows_browse:
    ttk.Button(btn_frame, text="Windows\u2026",
               command=lambda: _pick_dir(var, initialdir="/mnt/")).pack(
      side="left", padx=(2, 0))
  if hint:
    ttk.Label(parent, text=hint, foreground="gray",
              font=("TkDefaultFont", 7)).grid(
      row=row + 1, column=0, columnspan=3, sticky="w", padx=(4, 0))


def _pick_dir(var: tk.StringVar, initialdir: str | None = None):
  kw = {}
  if initialdir:
    kw["initialdir"] = initialdir
  p = filedialog.askdirectory(**kw)
  if p:
    var.set(p)


# ──────────────────────────────────────────────────────────────────────────────
# DatasetScreen
# ──────────────────────────────────────────────────────────────────────────────

class DatasetScreen(Screen):
  def __init__(self, parent, navigator, cfg):
    super().__init__(parent, navigator, cfg)
    self._add_back_button()

    # Scrollable container
    canvas = tk.Canvas(self, highlightthickness=0)
    scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
    self._scroll_frame = ttk.Frame(canvas, padding=20)

    self._scroll_frame.bind(
      "<Configure>",
      lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # Bind mousewheel
    def _on_mousewheel(event):
      canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    # Linux scroll
    canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
    canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

    outer = self._scroll_frame
    win = navigator.win

    ttk.Label(outer, text="Dataset Management",
              font=("Arial", 18, "bold")).pack(pady=(0, 4))
    ttk.Label(outer, text="Prepare your replay dataset for training",
              foreground="gray", font=("TkDefaultFont", 10)).pack(pady=(0, 16))

    repo_root = cfg.get("paths", "slippi_ai_root")

    # ── Step 1: Prepare Archives ──────────────────────────────────────────
    step1 = StepCard(outer, win,
      "1. Prepare Archives",
      "Accepts a directory with any mix of .7z/.zip archives and subdirectories "
      "of loose .slp files. Archives are copied; directories are compressed into .7z. "
      "Requires the 7z command-line tool for loose files.")
    step1.pack(fill="x", pady=(0, 10))

    c = step1.content
    self._source_dir = tk.StringVar(value=cfg.get("dataset", "source_dir"))
    _dir_field(c, "Source directory:", self._source_dir, 0,
               "Subdirs of .slp files should be named yyyy-mm (e.g. 2024-04). "
               "Existing .7z/.zip archives will be copied as-is.",
               windows_browse=True, project_root=repo_root)
    self._output_dir = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Output directory:", self._output_dir, 2,
               "Existing archives in Raw/ will be skipped.",
               project_root=repo_root)

    def build_prepare_cmd():
      src = self._source_dir.get().strip()
      out = self._output_dir.get().strip()
      if not src or not out:
        self._status_msg(step1, "Please fill in both directories", "red")
        return None
      script = find_script(repo_root, "slippi_db/scripts/prepare_local.py")
      if not script:
        self._status_msg(step1, "Cannot find prepare_local.py", "red")
        return None
      return [sys.executable, script, f"--slp_root={src}", f"--zip_root={out}"]

    step1.set_command_builder(build_prepare_cmd)
    step1._get_cwd = lambda: repo_root

    # ── Step 2: Upgrade Replays ───────────────────────────────────────────
    step2 = StepCard(outer, win,
      "2. Upgrade Replays (Optional)",
      "Replays from Slippi versions before 3.2.0 may be missing stage event data. "
      "Upgrading uses Dolphin to re-record them. This is time-consuming but improves "
      "data quality. Most recent replays (2023+) don't need this. "
      "Note: operates on .zip archives only (not .7z).")
    step2.pack(fill="x", pady=(0, 10))

    c = step2.content
    self._upgrade_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Dataset root:", self._upgrade_root, 0, project_root=repo_root)
    self._upgrade_dolphin = tk.StringVar(
      value=cfg.get("paths", "dolphin_dir"))
    _dir_field(c, "Dolphin path:", self._upgrade_dolphin, 1, project_root=repo_root)
    self._upgrade_iso = tk.StringVar(value=cfg.get("paths", "iso"))
    ttk.Label(c, text="ISO path:").grid(row=2, column=0, sticky="w", pady=2)
    ttk.Entry(c, textvariable=self._upgrade_iso, width=44).grid(
      row=2, column=1, padx=4, pady=2)
    ttk.Button(c, text="Browse\u2026",
               command=lambda: self._pick_file(self._upgrade_iso,
                 [("ISO", "*.iso *.ISO"), ("All", "*.*")])).grid(
      row=2, column=2, pady=2)

    opts_frame = ttk.Frame(c)
    opts_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Label(opts_frame, text="Threads:").pack(side="left")
    self._upgrade_threads = tk.IntVar(
      value=cfg.getint("dataset", "threads", 4))
    ttk.Spinbox(opts_frame, textvariable=self._upgrade_threads,
                from_=1, to=32, width=4).pack(side="left", padx=(4, 12))
    self._upgrade_skip = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts_frame, text="Skip existing",
                    variable=self._upgrade_skip).pack(side="left")
    self._upgrade_dry = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts_frame, text="Dry run",
                    variable=self._upgrade_dry).pack(side="left", padx=(8, 0))

    def build_upgrade_cmd():
      root = self._upgrade_root.get().strip()
      dolphin = self._upgrade_dolphin.get().strip()
      iso = self._upgrade_iso.get().strip()
      if not root or not dolphin or not iso:
        self._status_msg(step2, "Please fill in all fields", "red")
        return None
      script = find_script(repo_root, "slippi_db/scripts/upgrade_slps.py")
      if not script:
        self._status_msg(step2, "Cannot find upgrade_slps.py", "red")
        return None
      cmd = [sys.executable, script,
             f"--root={root}", f"--dolphin={dolphin}", f"--iso={iso}",
             f"--threads={self._upgrade_threads.get()}"]
      if self._upgrade_skip.get():
        cmd.append("--skip_existing")
      if self._upgrade_dry.get():
        cmd.append("--dry_run")
      return cmd

    step2.set_command_builder(build_upgrade_cmd)
    step2._get_cwd = lambda: repo_root

    # ── Step 3: Parse Replays ─────────────────────────────────────────────
    step3 = StepCard(outer, win,
      "3. Parse Replays",
      "Parse .slp files from archives into parquet format and build the parsed.sqlite database.")
    step3.pack(fill="x", pady=(0, 10))

    c = step3.content
    self._parse_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Dataset root:", self._parse_root, 0, project_root=repo_root)

    opts_frame = ttk.Frame(c)
    opts_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Label(opts_frame, text="Threads:").pack(side="left")
    self._parse_threads = tk.IntVar(
      value=cfg.getint("dataset", "threads", 4))
    ttk.Spinbox(opts_frame, textvariable=self._parse_threads,
                from_=1, to=32, width=4).pack(side="left", padx=(4, 12))
    ttk.Label(opts_frame, text="Compression:").pack(side="left")
    self._parse_compression = tk.StringVar(
      value=cfg.get("dataset", "compression", "zlib"))
    ttk.Combobox(opts_frame, textvariable=self._parse_compression,
                 values=["zlib", "snappy", "gzip", "brotli", "lz4", "zstd", "none"],
                 width=8, state="readonly").pack(side="left", padx=(4, 0))

    checks_frame = ttk.Frame(c)
    checks_frame.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
    self._parse_reprocess = tk.BooleanVar(value=False)
    ttk.Checkbutton(checks_frame, text="Reprocess",
                    variable=self._parse_reprocess).pack(side="left")
    self._parse_missing_netplay = tk.BooleanVar(value=False)
    ttk.Checkbutton(checks_frame, text="Re-parse missing netplay",
                    variable=self._parse_missing_netplay).pack(side="left", padx=(8, 0))
    self._parse_missing_parsed = tk.BooleanVar(value=False)
    ttk.Checkbutton(checks_frame, text="Re-parse missing parsed",
                    variable=self._parse_missing_parsed).pack(side="left", padx=(8, 0))
    self._parse_dry = tk.BooleanVar(value=False)
    ttk.Checkbutton(checks_frame, text="Dry run",
                    variable=self._parse_dry).pack(side="left", padx=(8, 0))

    def build_parse_cmd():
      root = self._parse_root.get().strip()
      if not root:
        self._status_msg(step3, "Please set dataset root", "red")
        return None
      script = find_script(repo_root, "slippi_db/parse_local.py")
      if not script:
        self._status_msg(step3, "Cannot find parse_local.py", "red")
        return None
      cmd = [sys.executable, script,
             f"--root={root}",
             f"--threads={self._parse_threads.get()}",
             f"--compression={self._parse_compression.get()}"]
      if self._parse_reprocess.get():
        cmd.append("--reprocess")
      if self._parse_missing_netplay.get():
        cmd.append("--reparse_missing_netplay")
      if self._parse_missing_parsed.get():
        cmd.append("--reparse_missing_parsed")
      if self._parse_dry.get():
        cmd.append("--dry_run")
      return cmd

    step3.set_command_builder(build_parse_cmd)
    step3._get_cwd = lambda: repo_root

    # ── Step 4: Compute Replay Stats ─────────────────────────────────────
    step4 = StepCard(outer, win,
      "4. Compute Replay Stats",
      "Analyze parsed replays to compute per-player quality metrics "
      "(L-cancel rate, APM, neutral wins, conversions, etc). "
      "Results are stored in parsed.sqlite for fast filtering.")
    step4.pack(fill="x", pady=(0, 10))

    c = step4.content
    self._stats_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Dataset root:", self._stats_root, 0, project_root=repo_root)

    opts_frame = ttk.Frame(c)
    opts_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Label(opts_frame, text="Threads:").pack(side="left")
    self._stats_threads = tk.IntVar(
      value=cfg.getint("dataset", "threads", 4))
    ttk.Spinbox(opts_frame, textvariable=self._stats_threads,
                from_=1, to=32, width=4).pack(side="left", padx=(4, 12))
    self._stats_recompute = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts_frame, text="Recompute all",
                    variable=self._stats_recompute).pack(side="left")

    def build_stats_cmd():
      root = self._stats_root.get().strip()
      if not root:
        self._status_msg(step4, "Please set dataset root", "red")
        return None
      script = find_script(repo_root,
        "slippi_db/scripts/compute_replay_stats.py")
      if not script:
        self._status_msg(step4, "Cannot find compute_replay_stats.py", "red")
        return None
      cmd = [sys.executable, script,
             f"--root={root}",
             f"--threads={self._stats_threads.get()}"]
      if self._stats_recompute.get():
        cmd.append("--recompute")
      return cmd

    step4.set_command_builder(build_stats_cmd)
    step4._get_cwd = lambda: repo_root

    # ── Step 5: Filter by Stats ───────────────────────────────────────────
    step5_frame = ttk.LabelFrame(outer, text="5. Filter Replays by Stats",
                                  padding=10)
    step5_frame.pack(fill="x", pady=(0, 10))

    ttk.Label(step5_frame,
              text="Set minimum quality thresholds. Matching replay count updates live.",
              foreground="gray", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 6))

    filter_content = ttk.Frame(step5_frame)
    filter_content.pack(fill="x")

    self._filter_stats_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(filter_content, "Dataset root:", self._filter_stats_root, 0,
               project_root=repo_root)

    # Count label
    self._match_count_var = tk.StringVar(value="Load stats to see counts")
    ttk.Label(filter_content, textvariable=self._match_count_var,
              font=("TkDefaultFont", 10, "bold")).grid(
      row=1, column=0, columnspan=3, sticky="w", pady=(8, 8))

    # Filter sliders
    self._stat_filters: dict[str, tuple[tk.DoubleVar, ttk.Label]] = {}
    filter_defs = [
      ("min_l_cancel_rate",   "Min L-cancel rate:",     0.0, 1.0, 0.0, 0.05),
      ("min_apm",             "Min APM:",               0, 600, 0, 10),
      ("min_wavedash_per_min","Min wavedash/min:",       0, 30, 0, 1),
      ("min_neutral_win",     "Min neutral win ratio:",  0.0, 1.0, 0.0, 0.05),
      ("min_damage_dealt",    "Min damage dealt:",       0, 2000, 0, 50),
      ("min_game_length_sec", "Min game length (sec):",  0, 480, 0, 10),
    ]

    slider_frame = ttk.Frame(filter_content)
    slider_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))

    for i, (key, label, from_, to_, default, resolution) in enumerate(filter_defs):
      ttk.Label(slider_frame, text=label, width=22, anchor="w").grid(
        row=i, column=0, sticky="w", pady=2)
      var = tk.DoubleVar(value=default)
      scale = ttk.Scale(slider_frame, from_=from_, to=to_, variable=var,
                        orient="horizontal", length=200,
                        command=lambda *a: self._schedule_filter_update())
      scale.grid(row=i, column=1, sticky="ew", padx=4, pady=2)
      val_label = ttk.Label(slider_frame, text=str(default), width=8)
      val_label.grid(row=i, column=2, pady=2)
      self._stat_filters[key] = (var, val_label)

    slider_frame.columnconfigure(1, weight=1)

    # Buttons
    btn_frame = ttk.Frame(filter_content)
    btn_frame.grid(row=3, column=0, columnspan=3, pady=(8, 0))
    ttk.Button(btn_frame, text="Reset Filters",
               command=self._reset_filters).pack(side="left", padx=4)
    ttk.Button(btn_frame, text="Apply to Dataset",
               command=self._apply_stats_filter).pack(side="left", padx=4)

    self._filter_timer_id = None

    # ── Step 6: Create Dataset ────────────────────────────────────────────
    step6 = StepCard(outer, win,
      "6. Create Dataset",
      "Convert parsed.sqlite to parsed.pkl, then generate meta.json for training. "
      "Runs two scripts automatically in sequence. "
      "If a stats filter was applied in Step 5, only matching replays are included.")
    step6.pack(fill="x", pady=(0, 10))

    c = step6.content
    self._create_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Dataset root:", self._create_root, 0, project_root=repo_root)

    opts_frame = ttk.Frame(c)
    opts_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
    self._winner_only = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts_frame, text="Winner only",
                    variable=self._winner_only).pack(side="left")

    def run_create_dataset():
      root = self._create_root.get().strip()
      if not root:
        self._status_msg(step6, "Please set dataset root", "red")
        return

      convert_script = find_script(repo_root,
        "slippi_db/scripts/convert_sqlite_to_parsed.py")
      make_script = find_script(repo_root,
        "slippi_db/scripts/make_local_dataset.py")

      if not convert_script or not make_script:
        self._status_msg(step6, "Cannot find required scripts", "red")
        return

      def run_chain():
        step6._win.after(0, lambda: step6._status_var.set("Converting sqlite to pkl..."))
        step6._win.after(0, lambda: step6._status_label.config(foreground="orange"))

        cmd1 = [sys.executable, convert_script, f"--root={root}"]
        p1 = subprocess.run(cmd1, cwd=repo_root)
        if p1.returncode != 0:
          step6._win.after(0, lambda: step6._status_var.set(
            f"Convert failed (exit code {p1.returncode})"))
          step6._win.after(0, lambda: step6._status_label.config(foreground="red"))
          step6._win.after(0, lambda: step6._run_btn.config(text="Run"))
          return

        step6._win.after(0, lambda: step6._status_var.set("Creating meta.json..."))

        cmd2 = [sys.executable, make_script, f"--root={root}"]
        if not self._winner_only.get():
          cmd2.append("--winner_only=False")
        # Apply stats filter if it exists
        stats_filter_path = os.path.join(root, "stats_filter.json")
        if os.path.isfile(stats_filter_path):
          cmd2.append(f"--stats_filter={stats_filter_path}")
        p2 = subprocess.run(cmd2, cwd=repo_root)

        def finish():
          if p2.returncode == 0:
            step6._status_var.set("Complete")
            step6._status_label.config(foreground="green")
          else:
            step6._status_var.set(f"make_local_dataset failed (exit code {p2.returncode})")
            step6._status_label.config(foreground="red")
          step6._run_btn.config(text="Run")

        step6._win.after(0, finish)

      step6._run_btn.config(text="Running...")
      step6._run_btn.config(state="disabled")
      step6._status_var.set("Starting...")
      step6._status_label.config(foreground="orange")
      threading.Thread(target=run_chain, daemon=True).start()

    step6._run_btn.config(command=lambda: run_create_dataset())

    # ── Step 7: Filter by Character ───────────────────────────────────────
    step7 = StepCard(outer, win,
      "7. Filter by Character (Optional)",
      "Create a character-specific dataset subset with symlinks to original parquet files.")
    step5.pack(fill="x", pady=(0, 10))

    c = step7.content
    self._filter_root = tk.StringVar(value=cfg.get("dataset", "dataset_root"))
    _dir_field(c, "Dataset root:", self._filter_root, 0, project_root=repo_root)

    ttk.Label(c, text="Characters:").grid(row=1, column=0, sticky="w", pady=2)
    self._filter_chars = tk.StringVar()
    ttk.Entry(c, textvariable=self._filter_chars, width=44).grid(
      row=1, column=1, padx=4, pady=2)
    ttk.Label(c, text="e.g. fox,marth,sheik", foreground="gray",
              font=("TkDefaultFont", 7)).grid(
      row=2, column=0, columnspan=3, sticky="w", padx=(4, 0))

    self._filter_output = tk.StringVar()
    _dir_field(c, "Output dir:", self._filter_output, 3,
               "Leave blank for auto-generated name (Dataset-CHARACTER)",
               project_root=repo_root)

    opts_frame = ttk.Frame(c)
    opts_frame.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
    self._char_filter_both = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts_frame, text="Both players must match",
                    variable=self._char_filter_both).pack(side="left")
    ttk.Label(opts_frame, text="Limit:").pack(side="left", padx=(12, 4))
    self._filter_limit = tk.StringVar()
    ttk.Entry(opts_frame, textvariable=self._filter_limit, width=8).pack(side="left")

    def build_char_filter_cmd():
      root = self._filter_root.get().strip()
      chars = self._filter_chars.get().strip()
      if not root or not chars:
        self._status_msg(step7, "Please set dataset root and characters", "red")
        return None
      script = find_script(repo_root,
        "slippi_db/scripts/filter_by_character.py")
      if not script:
        self._status_msg(step7, "Cannot find filter_by_character.py", "red")
        return None
      cmd = [sys.executable, script,
             f"--root={root}", f"--characters={chars}"]
      output = self._filter_output.get().strip()
      if output:
        cmd.append(f"--output={output}")
      if self._char_filter_both.get():
        cmd.append("--both")
      limit = self._filter_limit.get().strip()
      if limit:
        cmd.append(f"--limit={limit}")
      return cmd

    step7.set_command_builder(build_char_filter_cmd)
    step7._get_cwd = lambda: repo_root

    self._steps = [step1, step2, step3, step4, step6, step7]

  def on_leave(self):
    # Persist settings
    cfg = self.cfg
    cfg.set("dataset", "source_dir", self._source_dir.get())
    # Use parse_root as the canonical dataset_root (most commonly set)
    root = self._parse_root.get().strip()
    if root:
      cfg.set("dataset", "dataset_root", root)
    cfg.set("dataset", "threads", str(self._parse_threads.get()))
    cfg.set("dataset", "compression", self._parse_compression.get())
    cfg.save()

  # ── Filter by Stats helpers ──────────────────────────────────────────

  def _schedule_filter_update(self):
    """Debounce slider changes - update count after 200ms of no changes."""
    # Update displayed values immediately
    for key, (var, label) in self._stat_filters.items():
      val = var.get()
      if 'rate' in key or 'ratio' in key or 'neutral' in key:
        label.config(text=f"{val:.2f}")
      else:
        label.config(text=f"{val:.0f}")

    if self._filter_timer_id is not None:
      self.after_cancel(self._filter_timer_id)
    self._filter_timer_id = self.after(200, self._update_filter_count)

  def _update_filter_count(self):
    """Query SQLite with current filter thresholds and update count."""
    self._filter_timer_id = None
    root = self._filter_stats_root.get().strip()
    if not root:
      self._match_count_var.set("Set dataset root first")
      return

    db_path = os.path.join(root, "parsed.sqlite")
    if not os.path.isfile(db_path):
      self._match_count_var.set("parsed.sqlite not found")
      return

    try:
      conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)

      # Check if replay_stats table exists
      tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_stats'"
      ).fetchall()
      if not tables:
        conn.close()
        self._match_count_var.set("Run Step 4 first to compute stats")
        return

      # Get total training replays
      total = conn.execute(
        "SELECT COUNT(*) FROM replays WHERE is_training = 1"
      ).fetchone()[0]

      # Build filter query
      filters = self._stat_filters
      conditions = ["r.is_training = 1"]
      params = []

      min_l = filters["min_l_cancel_rate"][0].get()
      if min_l > 0:
        conditions.append("(rs.l_cancel_rate >= ? OR rs.l_cancel_rate IS NULL)")
        params.append(min_l)

      min_apm = filters["min_apm"][0].get()
      if min_apm > 0:
        conditions.append("rs.apm >= ?")
        params.append(min_apm)

      min_wd = filters["min_wavedash_per_min"][0].get()
      if min_wd > 0:
        conditions.append("rs.wavedash_per_min >= ?")
        params.append(min_wd)

      min_nw = filters["min_neutral_win"][0].get()
      if min_nw > 0:
        conditions.append("(rs.neutral_win_ratio >= ? OR rs.neutral_win_ratio IS NULL)")
        params.append(min_nw)

      min_dmg = filters["min_damage_dealt"][0].get()
      if min_dmg > 0:
        conditions.append("rs.total_damage_dealt >= ?")
        params.append(min_dmg)

      min_len = filters["min_game_length_sec"][0].get()
      if min_len > 0:
        conditions.append("rs.num_frames >= ?")
        params.append(min_len * 60)  # Convert seconds to frames

      where = " AND ".join(conditions)
      query = f"""
        SELECT COUNT(DISTINCT rs.slp_md5)
        FROM replay_stats rs
        JOIN replays r ON rs.slp_md5 = r.slp_md5
        WHERE {where}
      """
      matching = conn.execute(query, params).fetchone()[0]
      conn.close()

      self._match_count_var.set(f"Matching: {matching:,} / {total:,} replays")

    except Exception as e:
      self._match_count_var.set(f"Error: {e}")

  def _reset_filters(self):
    """Reset all filter sliders to their defaults (no filtering)."""
    for key, (var, label) in self._stat_filters.items():
      var.set(0.0)
      label.config(text="0" if 'rate' not in key and 'ratio' not in key else "0.00")
    self._update_filter_count()

  def _apply_stats_filter(self):
    """Write stats_filter.json with the list of matching slp_md5 values."""
    root = self._filter_stats_root.get().strip()
    if not root:
      return

    db_path = os.path.join(root, "parsed.sqlite")
    if not os.path.isfile(db_path):
      return

    try:
      conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)

      filters = self._stat_filters
      conditions = ["r.is_training = 1"]
      params = []

      min_l = filters["min_l_cancel_rate"][0].get()
      if min_l > 0:
        conditions.append("(rs.l_cancel_rate >= ? OR rs.l_cancel_rate IS NULL)")
        params.append(min_l)

      min_apm = filters["min_apm"][0].get()
      if min_apm > 0:
        conditions.append("rs.apm >= ?")
        params.append(min_apm)

      min_wd = filters["min_wavedash_per_min"][0].get()
      if min_wd > 0:
        conditions.append("rs.wavedash_per_min >= ?")
        params.append(min_wd)

      min_nw = filters["min_neutral_win"][0].get()
      if min_nw > 0:
        conditions.append("(rs.neutral_win_ratio >= ? OR rs.neutral_win_ratio IS NULL)")
        params.append(min_nw)

      min_dmg = filters["min_damage_dealt"][0].get()
      if min_dmg > 0:
        conditions.append("rs.total_damage_dealt >= ?")
        params.append(min_dmg)

      min_len = filters["min_game_length_sec"][0].get()
      if min_len > 0:
        conditions.append("rs.num_frames >= ?")
        params.append(min_len * 60)

      where = " AND ".join(conditions)
      query = f"""
        SELECT DISTINCT rs.slp_md5
        FROM replay_stats rs
        JOIN replays r ON rs.slp_md5 = r.slp_md5
        WHERE {where}
      """
      rows = conn.execute(query, params).fetchall()
      conn.close()

      md5_list = [r[0] for r in rows]
      filter_path = os.path.join(root, "stats_filter.json")
      with open(filter_path, "w") as f:
        json.dump(md5_list, f)

      self._match_count_var.set(
        f"Applied: {len(md5_list):,} replays saved to stats_filter.json")

    except Exception as e:
      self._match_count_var.set(f"Error applying filter: {e}")

  @staticmethod
  def _status_msg(card: StepCard, msg: str, color: str):
    card._status_var.set(msg)
    card._status_label.config(foreground=color)

  @staticmethod
  def _pick_file(var: tk.StringVar, filetypes):
    p = filedialog.askopenfilename(filetypes=filetypes)
    if p:
      var.set(p)
