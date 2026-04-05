"""Train RL screen: dynamic config UI for single-agent and two-agent RL scripts."""

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
    _HAS_FF,
    _load_all_presets,
    _save_all_presets,
    _PRESETS_FILE,
    _use_scientific,
)
from LAUNCHER.screens.train_help import HELP as _HELP_REGISTRY

# ── Script registry ──────────────────────────────────────────────────────────

_SCRIPTS = {
    "rl_single": dict(
        label="Single-Agent RL",
        description=(
            "Train one agent via PPO self-play or against a frozen/CPU opponent. "
            "Uses slippi_ai/rl/run.py."),
        module="slippi_ai.rl.run_lib",
        config_attr="DEFAULT_CONFIG",
        script="slippi_ai/rl/run.py",
    ),
    "rl_train_two": dict(
        label="Two-Agent RL",
        description=(
            "Train two agents against each other simultaneously. Both agents "
            "learn via PPO. Uses slippi_ai/rl/train_two.py."),
        module="slippi_ai.rl.train_two_lib",
        config_attr="DEFAULT_CONFIG",
        script="slippi_ai/rl/train_two.py",
    ),
}

# Fields shown in the basic (non-advanced) view.
_BASIC_FIELDS: dict[str, set[str]] = {
    "rl_single": {
        "runtime.max_step", "runtime.max_runtime", "runtime.tag",
        "teacher", "restore",
        "dolphin.path", "dolphin.iso",
        "learner.learning_rate", "learner.policy_gradient_weight",
        "learner.kl_teacher_weight", "learner.reward_halflife",
        "actor.rollout_length", "actor.num_envs",
        "actor.gpu_inference",
        "agent.name", "agent.char",
        "opponent.type", "opponent.train",
        "opponent.other.path", "opponent.other.char",
        "optimizer_burnin_steps", "value_burnin_steps",
    },
    "rl_train_two": {
        "runtime.max_step", "runtime.max_runtime", "runtime.tag",
        "dolphin.path", "dolphin.iso",
        "p1.teacher", "p1.restore", "p1.name", "p1.char", "p1.label",
        "p2.teacher", "p2.restore", "p2.name", "p2.char", "p2.label",
        "learner.learning_rate", "learner.policy_gradient_weight",
        "learner.kl_teacher_weight", "learner.reward_halflife",
        "learner2.learning_rate",
        "actor.rollout_length", "actor.num_envs",
        "actor.gpu_inference",
        "optimizer_burnin_steps", "value_burnin_steps",
    },
}

# Fields used only for testing/debugging.
_TESTING_FIELDS: set[str] = {
    "actor.use_fake_envs", "override_delay",
    "dolphin.dump.enabled", "dolphin.dump.dir",
}


# ── Lazy imports for heavy modules ───────────────────────────────────────────

_tree_cache: dict[str, dict] = {}


def _get_flag_tree(script_key: str) -> dict:
    """Return the fancyflags Item tree for a script, caching the result."""
    if script_key not in _tree_cache:
        import importlib
        from slippi_ai import flag_utils
        info = _SCRIPTS[script_key]
        mod = importlib.import_module(info["module"])
        default_config = getattr(mod, info["config_attr"])
        _tree_cache[script_key] = flag_utils.get_flags_from_default(default_config)
    return _tree_cache[script_key]


# ── Built-in templates ───────────────────────────────────────────────────────

_BUILTIN_TEMPLATES: dict[str, dict[str, dict]] = {
    "rl_single": {
        "Default (all defaults)": {},
        "RL Example (from scripts/rl_example.sh)": {
            "runtime.tag": "fox_delay_18",
            "runtime.max_step": 10000,
            "runtime.log_interval": 300,
            "runtime.reset_every_n_steps": 512,
            "runtime.burnin_steps_after_reset": 5,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward_halflife": 4,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward.ledge_grab_penalty": 0.02,
            "learner.policy_gradient_weight": 5,
            "learner.kl_teacher_weight": 3e-3,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.minibatched": False,
            "teacher": "",
            "opponent.type": "SELF",
            "opponent.train": True,
            "actor.rollout_length": 240,
            "actor.num_envs": 96,
            "actor.inner_batch_size": 8,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "agent.name": "Master Player",
            "agent.batch_steps": 4,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
        "Single-Agent vs Frozen Opponent": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward.ledge_grab_penalty": 0.02,
            "learner.reward.stalling_penalty": 0.1,
            "learner.reward.stalling_threshold": 50.0,
            "learner.reward.voluntary_offstage_death_penalty": 0.6,
            "learner.reward_halflife": 8.0,
            "learner.reward.approaching_factor": 0.003,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "opponent.type": "OTHER",
            "opponent.train": False,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "agent.batch_steps": 4,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
    },
    "rl_train_two": {
        "Default (all defaults)": {},
        "Two-Agent Fresh Start": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "p1.batch_steps": 4,
            "p2.batch_steps": 4,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward_halflife": 8.0,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "learner1.reward.ledge_grab_penalty": 0.02,
            "learner1.reward.stalling_penalty": 0.1,
            "learner1.reward.stalling_threshold": 50.0,
            "learner1.reward.approaching_factor": 0.0,
            "learner1.reward.l_cancel_miss_penalty": 0,
            "learner1.reward.voluntary_offstage_death_penalty": 0.5,
            "learner2.learning_rate": 1e-5,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
        "Two-Agent Multi-Character": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "p1.batch_steps": 4,
            "p2.char": "FOX,FALCO,MARTH,SHEIK,JIGGLYPUFF,CPTFALCON,PEACH,YOSHI,POPO,LUIGI,PIKACHU,SAMUS",
            "p2.batch_steps": 4,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward_halflife": 8.0,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "learner1.reward.ledge_grab_penalty": 0.02,
            "learner1.reward.stalling_penalty": 0.1,
            "learner1.reward.stalling_threshold": 50.0,
            "learner1.reward.approaching_factor": 0.0,
            "learner1.reward.l_cancel_miss_penalty": 0,
            "learner1.reward.voluntary_offstage_death_penalty": 0.5,
            "learner2.learning_rate": 1e-5,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
    },
}


# ── Preset helpers (shared file with train_il) ──────────────────────────────

def _load_rl_presets() -> dict:
    """Load user presets and merge with built-in RL templates."""
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


def _save_rl_presets(data: dict):
    """Save user presets, excluding built-in templates."""
    # Load the full file first to preserve IL presets
    p = script_dir() / _PRESETS_FILE
    full = {}
    if p.is_file():
        try:
            full = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Update only RL script keys
    for script_key in _SCRIPTS:
        user_only = {
            name: values for name, values in data.get(script_key, {}).items()
            if name not in _BUILTIN_TEMPLATES.get(script_key, {})
        }
        full[script_key] = user_only
    p.write_text(
        json.dumps(full, indent=2, default=str), encoding="utf-8")


# ── Main screen ──────────────────────────────────────────────────────────────

class TrainRLScreen(Screen):
    """Dynamic training configuration screen for RL scripts."""

    def __init__(self, parent, navigator, cfg: AppConfig):
        super().__init__(parent, navigator, cfg)
        self._add_back_button()

        if not _HAS_FF:
            ttk.Label(
                self,
                text="Training dependencies not installed (fancyflags, absl).\n"
                     "Install them with: pip install -r LAUNCHER/requirements.txt",
                foreground="gray",
            ).pack(expand=True)
            return

        self._current_script: str = cfg.get("train_rl", "last_script", "rl_train_two")
        self._loaded = False
        self._proc: subprocess.Popen | None = None
        self._capture: OutputCapture | None = None

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # ── Script selector ──────────────────────────────────────────────
        sel_frame = ttk.LabelFrame(outer, text="Training Script", padding=8)
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
            value=cfg.get("train_rl", "last_preset", ""))
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

        # Command preview (hidden by default, packed above exec_frame)
        self._cmd_text = tk.Text(
            outer, height=4, wrap="word", state="disabled",
            font=("Consolas", 8))
        self._cmd_text_visible = False

        # ── Log panel (hidden by default, packed above exec_frame) ──────
        self._log_panel = TrainingLogPanel(outer, script_type="rl")
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
        # Linux/X11 (including WSL) uses Button-4/5 for scroll events
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
        self.cfg.set("train_rl", "last_script", self._current_script)
        self.cfg.set("train_rl", "last_preset", self._preset_var.get())
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
        all_presets = _load_rl_presets()
        names = list(all_presets.get(self._current_script, {}).keys())
        self._preset_combo["values"] = names
        if self._preset_var.get() not in names:
            self._preset_var.set(names[0] if names else "")

    def _apply_preset(self, name: str):
        all_presets = _load_rl_presets()
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
        all_presets = _load_rl_presets()
        script_presets = all_presets.setdefault(self._current_script, {})
        script_presets[name] = self._editor.get_all_values()
        _save_rl_presets(all_presets)
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
        all_presets = _load_rl_presets()
        script_presets = all_presets.get(self._current_script, {})
        if name in script_presets:
            del script_presets[name]
            _save_rl_presets(all_presets)
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
                flag = "config." + ".".join(path)
                cmd.append(f"--{flag}={_format_flag_value(value)}")

        # Also pass wandb flags (disabled by default from GUI)
        cmd.append("--wandb.mode=disabled")
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
        env["TF_ENABLE_ONEDNN_OPTS"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        # Set script type for metric parsing
        self._log_panel.set_script_type("rl")
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
