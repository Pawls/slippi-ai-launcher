"""Evaluate screen: run games between agents or benchmark agent performance."""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from LAUNCHER.config import AppConfig, find_script, script_dir
from LAUNCHER.screens import Screen
from LAUNCHER.screens.log_viewer import OutputCapture, TrainingLogPanel
from LAUNCHER.screens.train_il import (
    CollapsibleSection,
    ConfigEditorPanel,
    FlagHelpDialog,
    _format_flag_value,
    _load_all_presets,
    _save_all_presets,
    _PRESETS_FILE,
    _use_scientific,
)
from LAUNCHER.screens.train_help import HELP as _HELP_REGISTRY

# ── Script registry ──────────────────────────────────────────────────────────

_SCRIPTS = {
    "eval_watch": dict(
        label="Watch Game",
        description=(
            "Run a visible game between two AI agents, or play against an AI "
            "yourself. Uses scripts/eval_two.py with Dolphin in GUI mode."),
        script="scripts/eval_two.py",
        flag_prefix="",  # flags are top-level (--p1.*, --dolphin.*, etc.)
    ),
    "eval_benchmark": dict(
        label="Benchmark",
        description=(
            "Run headless evaluation and report KO diff per minute, FPS, and "
            "timing stats. Uses scripts/run_evaluator.py."),
        script="scripts/run_evaluator.py",
        flag_prefix="",
    ),
}

# Fields shown in the basic (non-advanced) view.
_BASIC_FIELDS: dict[str, set[str]] = {
    "eval_watch": {
        "p1.type", "p1.character", "p1.level", "p1.ai.path", "p1.ai.name",
        "p2.type", "p2.character", "p2.level", "p2.ai.path", "p2.ai.name",
        "dolphin.path", "dolphin.iso", "dolphin.online_delay",
        "dolphin.stage", "dolphin.fullscreen",
        "num_games", "use_gpu",
    },
    "eval_benchmark": {
        "player.type", "player.character", "player.ai.path",
        "opponent.type", "opponent.character", "opponent.ai.path",
        "dolphin.path", "dolphin.iso",
        "rollout_length", "num_envs", "use_gpu",
    },
}

# Fields used only for testing/debugging.
_TESTING_FIELDS: set[str] = {
    "p1.ai.fake", "p2.ai.fake",
    "player.ai.fake", "opponent.ai.fake",
    "fake_envs", "tf_profile",
}


# ── Lazy flag-tree construction ──────────────────────────────────────────────

_tree_cache: dict[str, dict] = {}


def _get_flag_tree(script_key: str) -> dict:
    """Build the fancyflags Item tree for an eval script, caching the result."""
    if script_key not in _tree_cache:
        import fancyflags as ff
        from slippi_ai import eval_lib, flag_utils, utils
        from slippi_ai import dolphin as dolphin_lib

        if script_key == "eval_watch":
            # Mirror the flag structure from scripts/eval_two.py
            player_flags = utils.map_nt(lambda x: x, eval_lib.PLAYER_FLAGS)
            player_flags['ai']['async_inference'] = ff.Boolean(True)

            dolphin_config = dolphin_lib.DolphinConfig(
                headless=False,
                infinite_time=False,
                online_delay=2,
                path=os.environ.get('DOLPHIN_PATH'),
                iso=os.environ.get('ISO_PATH'),
            )

            tree = {
                "p1": player_flags,
                "p2": utils.map_nt(lambda x: x, player_flags),
                "dolphin": flag_utils.get_flags_from_default(dolphin_config),
                "num_games": ff.Integer(None, 'Number of games to play.'),
                "use_gpu": ff.Boolean(False, 'Use GPU for AI inference.'),
            }

        elif script_key == "eval_benchmark":
            # Mirror the flag structure from scripts/run_evaluator.py
            agent_flags = dict(
                eval_lib.BATCH_AGENT_FLAGS,
                jit_compile=ff.Boolean(True),
            )
            player_flags = dict(eval_lib.PLAYER_FLAGS, ai=agent_flags)

            dolphin_config = dolphin_lib.DolphinConfig(
                infinite_time=False,
                headless=True,
            )

            tree = {
                "player": player_flags,
                "opponent": utils.map_nt(lambda x: x, player_flags),
                "dolphin": flag_utils.get_flags_from_default(dolphin_config),
                "rollout_length": ff.Integer(60 * 60, 'Number of steps per rollout.'),
                "num_envs": ff.Integer(1, 'Number of environments.'),
                "use_gpu": ff.Boolean(False, 'Use GPU for inference.'),
                "self_play": ff.Boolean(False, 'Self play.'),
                "async_envs": ff.Boolean(False, 'Use async environments.'),
                "num_env_steps": ff.Integer(0, 'Number of environment steps to batch.'),
                "inner_batch_size": ff.Integer(1, 'Number of environments to run sequentially.'),
                "num_agent_steps": ff.Integer(0, 'Number of agent steps to batch.'),
                "fake_envs": ff.Boolean(False, 'Use fake environments.'),
                "tf_profile": ff.Boolean(False, 'Enable TF profiler.'),
            }
        else:
            tree = {}

        _tree_cache[script_key] = tree
    return _tree_cache[script_key]


# ── Built-in templates ───────────────────────────────────────────────────────

_BUILTIN_TEMPLATES: dict[str, dict[str, dict]] = {
    "eval_watch": {
        "Default (all defaults)": {},
        "Agent vs Human": {
            "p1.type": "human",
            "dolphin.online_delay": 2,
        },
        "Agent vs Agent": {
            "dolphin.online_delay": 2,
        },
        "Agent vs CPU (Lv 9)": {
            "p2.type": "cpu",
            "p2.character": "FOX",
            "p2.level": 9,
            "dolphin.online_delay": 2,
        },
    },
    "eval_benchmark": {
        "Default (all defaults)": {},
        "Quick Benchmark (2 min)": {
            "rollout_length": 7200,
            "use_gpu": True,
        },
        "Full Benchmark (headless, multi-env)": {
            "rollout_length": 7200,
            "num_envs": 4,
            "use_gpu": True,
            "async_envs": True,
            "num_env_steps": 4,
            "inner_batch_size": 4,
        },
    },
}


# ── Preset helpers ───────────────────────────────────────────────────────────

def _load_eval_presets() -> dict:
    """Load user presets and merge with built-in eval templates."""
    p = script_dir() / _PRESETS_FILE
    user = {}
    if p.is_file():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    merged = {}
    for script_key in _SCRIPTS:
        builtins = _BUILTIN_TEMPLATES.get(script_key, {})
        user_presets = user.get(script_key, {})
        merged[script_key] = {**builtins, **user_presets}
    return merged


def _save_eval_presets(data: dict):
    """Save user presets, excluding built-in templates."""
    p = script_dir() / _PRESETS_FILE
    full = {}
    if p.is_file():
        try:
            full = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    for script_key in _SCRIPTS:
        user_only = {
            name: values for name, values in data.get(script_key, {}).items()
            if name not in _BUILTIN_TEMPLATES.get(script_key, {})
        }
        full[script_key] = user_only
    p.write_text(
        json.dumps(full, indent=2, default=str), encoding="utf-8")


# ── Main screen ──────────────────────────────────────────────────────────────

class EvaluateScreen(Screen):
    """Evaluation screen for watching games or running benchmarks."""

    def __init__(self, parent, navigator, cfg: AppConfig):
        super().__init__(parent, navigator, cfg)
        self._add_back_button()

        self._current_script: str = cfg.get("evaluate", "last_script", "eval_watch")
        self._loaded = False
        self._proc: subprocess.Popen | None = None
        self._capture: OutputCapture | None = None

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # ── Script selector ──────────────────────────────────────────────
        sel_frame = ttk.LabelFrame(outer, text="Evaluation Mode", padding=8)
        sel_frame.pack(fill="x", pady=(0, 6))

        self._script_var = tk.StringVar(value=self._current_script)
        for key, info in _SCRIPTS.items():
            ttk.Radiobutton(
                sel_frame, text=info["label"],
                variable=self._script_var, value=key,
                command=self._on_script_change,
            ).pack(side="left", padx=12)

        self._desc_label = ttk.Label(
            sel_frame, text="", foreground="gray",
            font=("TkDefaultFont", 8))
        self._desc_label.pack(fill="x", anchor="w", pady=(4, 0))
        self._desc_label.bind("<Configure>",
                              lambda e: e.widget.config(wraplength=e.width))

        # ── Preset bar ───────────────────────────────────────────────────
        preset_frame = ttk.Frame(outer)
        preset_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(preset_frame, text="Preset:").pack(side="left")
        self._preset_var = tk.StringVar(
            value=cfg.get("evaluate", "last_preset", ""))
        self._preset_combo = ttk.Combobox(
            preset_frame, textvariable=self._preset_var,
            width=44, state="readonly")
        self._preset_combo.pack(side="left", padx=(4, 8))

        ttk.Button(preset_frame, text="Load",
                   command=self._load_preset).pack(side="left", padx=2)
        ttk.Button(preset_frame, text="Save",
                   command=self._save_preset).pack(side="left", padx=2)
        ttk.Button(preset_frame, text="Save As\u2026",
                   command=self._save_preset_as).pack(side="left", padx=2)
        ttk.Button(preset_frame, text="Delete",
                   command=self._delete_preset).pack(side="left", padx=2)
        ttk.Button(preset_frame, text="Reset Defaults",
                   command=self._reset_defaults).pack(side="left", padx=(12, 2))

        # ── Toggles row ──────────────────────────────────────────────────
        toggles = ttk.Frame(outer)
        toggles.pack(fill="x", pady=(0, 4))

        self._advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toggles, text="Show Advanced Settings",
            variable=self._advanced_var,
            command=self._on_advanced_toggle,
        ).pack(side="left")

        self._sci_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toggles, text="Scientific Notation (e.g. 1e-4)",
            variable=self._sci_var,
            command=self._on_sci_toggle,
        ).pack(side="left", padx=(16, 0))

        self._testing_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toggles, text="Show Testing Parameters",
            variable=self._testing_var,
            command=self._on_testing_toggle,
        ).pack(side="left", padx=(16, 0))

        # ── Execution panel (pack BEFORE canvas to avoid cavity issue) ────
        exec_frame = ttk.Frame(outer)
        exec_frame.pack(fill="x", side="bottom", pady=(4, 0))

        self._cmd_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(exec_frame, text="Show Command",
                        variable=self._cmd_var,
                        command=self._toggle_command).pack(side="left")

        self._log_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(exec_frame, text="Show Log",
                        variable=self._log_var,
                        command=self._toggle_log).pack(side="left", padx=(8, 0))

        self._status_var = tk.StringVar(value="Ready")
        self._status_label = ttk.Label(
            exec_frame, textvariable=self._status_var,
            foreground="gray", font=("TkDefaultFont", 8))
        self._status_label.pack(side="left", padx=(12, 0))

        self._run_btn = ttk.Button(
            exec_frame, text="Run", command=self._on_run)
        self._run_btn.pack(side="right")

        # Command preview (hidden by default)
        self._cmd_text = tk.Text(
            outer, height=4, wrap="word", state="disabled",
            font=("Consolas", 8))
        self._cmd_text_visible = False

        # ── Log panel (hidden by default) ───────────────────────────────
        self._log_panel = TrainingLogPanel(outer, script_type="eval")
        self._log_panel_visible = False

        # ── Scrollable config area (pack LAST so it fills remaining space)
        canvas_frame = ttk.Frame(outer)
        canvas_frame.pack(fill="both", expand=True, pady=(0, 6))

        self._canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.config(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner_frame = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner_frame, anchor="nw")

        self._inner_frame.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind_all("<Button-4>", self._on_mousewheel)
        self._canvas.bind_all("<Button-5>", self._on_mousewheel)

        # Loading overlay
        self._loading_label = ttk.Label(
            self._inner_frame, text="Loading configuration...",
            foreground="gray", font=("TkDefaultFont", 11))

        self._editor = ConfigEditorPanel(self._inner_frame)
        self._editor._project_root = cfg.get("paths", "slippi_ai_root")

    # ── Canvas helpers ───────────────────────────────────────────────────

    def _on_inner_configure(self, _event=None):
        content_bbox = self._canvas.bbox("all")
        if content_bbox:
            canvas_h = self._canvas.winfo_height()
            content_h = content_bbox[3] - content_bbox[1]
            region_h = max(content_h, canvas_h)
            self._canvas.config(
                scrollregion=(0, 0, content_bbox[2], region_h))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        self._on_inner_configure()

    def _on_mousewheel(self, event):
        if not self._canvas.winfo_ismapped():
            return
        if event.num == 4:
            self._canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(3, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_enter(self):
        if not self._loaded:
            self._load_script(self._current_script)

    def on_leave(self):
        self.cfg.set("evaluate", "last_script", self._current_script)
        self.cfg.set("evaluate", "last_preset", self._preset_var.get())
        self.cfg.save()

    # ── Script loading ───────────────────────────────────────────────────

    def _on_script_change(self):
        key = self._script_var.get()
        if key != self._current_script:
            self._current_script = key
            self._loaded = False
            self._load_script(key)

    def _load_script(self, key: str):
        info = _SCRIPTS[key]
        self._desc_label.config(text=info["description"])
        self._refresh_preset_list()

        # Show loading state
        self._editor.clear()
        self._loading_label.pack(pady=20)

        def do_load():
            tree = _get_flag_tree(key)
            self.after(0, lambda: self._finish_load(tree))

        threading.Thread(target=do_load, daemon=True).start()

    def _finish_load(self, tree: dict):
        self._loading_label.pack_forget()
        basic = _BASIC_FIELDS.get(self._current_script, set())
        self._editor.build(tree, basic_fields=basic, testing_fields=_TESTING_FIELDS)
        self._loaded = True

        # Try to load last-used preset
        preset_name = self._preset_var.get()
        if preset_name:
            self._apply_preset(preset_name)

    def _on_advanced_toggle(self):
        self._editor.set_advanced(self._advanced_var.get())

    def _on_sci_toggle(self):
        from LAUNCHER.screens import train_il
        train_il._use_scientific = self._sci_var.get()
        self._editor.refresh_float_formats()

    def _on_testing_toggle(self):
        self._editor.set_testing(self._testing_var.get())

    # ── Preset management ────────────────────────────────────────────────

    def _refresh_preset_list(self):
        all_presets = _load_eval_presets()
        names = list(all_presets.get(self._current_script, {}).keys())
        self._preset_combo["values"] = names
        if self._preset_var.get() not in names:
            self._preset_var.set(names[0] if names else "")

    def _apply_preset(self, name: str):
        all_presets = _load_eval_presets()
        data = all_presets.get(self._current_script, {}).get(name)
        if data is None:
            return
        self._editor.reset_to_defaults()
        if data:
            self._editor.set_all_values(data)

    def _load_preset(self):
        name = self._preset_var.get()
        if name:
            self._apply_preset(name)

    def _save_preset(self):
        name = self._preset_var.get()
        if not name:
            self._save_preset_as()
            return
        self._do_save_preset(name)

    def _save_preset_as(self):
        name = simpledialog.askstring(
            "Save Preset", "Preset name:",
            parent=self, initialvalue=self._preset_var.get())
        if name:
            name = name.strip()
            self._do_save_preset(name)
            self._preset_var.set(name)
            self._refresh_preset_list()

    def _do_save_preset(self, name: str):
        all_presets = _load_eval_presets()
        script_presets = all_presets.setdefault(self._current_script, {})
        script_presets[name] = self._editor.get_all_values()
        _save_eval_presets(all_presets)
        self._refresh_preset_list()

    def _delete_preset(self):
        name = self._preset_var.get()
        if not name:
            return
        if name in _BUILTIN_TEMPLATES.get(self._current_script, {}):
            messagebox.showinfo("Built-in Template",
                                "Built-in templates cannot be deleted. "
                                "Use 'Save As' to create your own copy.")
            return
        all_presets = _load_eval_presets()
        script_presets = all_presets.get(self._current_script, {})
        if name in script_presets:
            del script_presets[name]
            _save_eval_presets(all_presets)
            self._preset_var.set("")
            self._refresh_preset_list()

    def _reset_defaults(self):
        self._editor.reset_to_defaults()

    # ── Command building ─────────────────────────────────────────────────

    def _build_command(self) -> list[str] | None:
        root = self.cfg.get("paths", "slippi_ai_root")
        if not root:
            messagebox.showerror("Error", "Slippi AI root not set. Check Settings.")
            return None
        info = _SCRIPTS[self._current_script]
        script = find_script(root, info["script"])
        if not script:
            messagebox.showerror("Error", f"Cannot find {info['script']}")
            return None

        cmd = [sys.executable, script]
        for path, fw in self._editor.widgets.items():
            value = fw.get_value()
            if value != fw.default_value:
                # Eval scripts use flat flags (--p1.ai.path=..., --dolphin.path=...)
                # with no "config." prefix
                flag = ".".join(path)
                cmd.append(f"--{flag}={_format_flag_value(value)}")

        return cmd

    def _toggle_command(self):
        if self._cmd_var.get():
            cmd = self._build_command()
            if cmd:
                self._cmd_text.config(state="normal")
                self._cmd_text.delete("1.0", "end")
                self._cmd_text.insert("1.0", " \\\n  ".join(cmd))
                self._cmd_text.config(state="disabled")
            if not self._cmd_text_visible:
                self._cmd_text.pack(fill="x", pady=(4, 0))
                self._cmd_text_visible = True
        else:
            if self._cmd_text_visible:
                self._cmd_text.pack_forget()
                self._cmd_text_visible = False

    def _toggle_log(self):
        if self._log_var.get():
            if not self._log_panel_visible:
                self._log_panel.pack(fill="both", expand=False, pady=(4, 0))
                self._log_panel_visible = True
        else:
            if self._log_panel_visible:
                self._log_panel.pack_forget()
                self._log_panel_visible = False

    # ── Execution ────────────────────────────────────────────────────────

    def _on_run(self):
        if self._proc and self._proc.poll() is None:
            self._kill()
            return

        cmd = self._build_command()
        if cmd is None:
            return

        root = self.cfg.get("paths", "slippi_ai_root")

        env = os.environ.copy()
        env["OMP_NUM_THREADS"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        # Set script type for metric parsing
        self._log_panel.set_script_type("eval")
        self._log_panel.clear()

        # Auto-show log panel when running
        if not self._log_panel_visible:
            self._log_var.set(True)
            self._toggle_log()

        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(
                    cmd, cwd=root, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
            else:
                self._proc = subprocess.Popen(
                    cmd, cwd=root, env=env, start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
        except Exception as exc:
            self._status_var.set(f"Error: {exc}")
            self._status_label.config(foreground="red")
            return

        panel = self._log_panel
        self._capture = OutputCapture(
            self._proc,
            on_stdout=lambda line: panel.after(
                0, lambda l=line: panel.append_stdout(l)),
            on_stderr=lambda line: panel.after(
                0, lambda l=line: panel.append_stderr(l)),
            on_complete=lambda rc: self.after(0, self._on_complete),
        )

        self._run_btn.config(text="Stop")
        self._status_var.set("Running...")
        self._status_label.config(foreground="orange")

    def _on_complete(self):
        rc = self._proc.returncode if self._proc else -1
        self._proc = None
        self._capture = None
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
            self._capture = None
            self._run_btn.config(text="Run")
            self._status_var.set("Stopped")
            self._status_label.config(foreground="gray")
