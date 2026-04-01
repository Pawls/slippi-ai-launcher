"""Train Imitation screen: dynamic config UI for IL and Q-learning scripts."""

import enum
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import fancyflags as ff
from fancyflags._definitions import MultiItem

from LAUNCHER.config import AppConfig, CHARACTERS, find_script, script_dir
from LAUNCHER.screens import Screen
from LAUNCHER.screens.train_help import HELP as _HELP_REGISTRY

from typing import cast
from absl.flags import _argument_parser as ap

# ── Script registry ──────────────────────────────────────────────────────────

_SCRIPTS = {
    "imitation": dict(
        label="Imitation Learning",
        description=(
            "Train a policy via behavioral cloning from Slippi replays. "
            "Uses cross-entropy loss to match expert actions."),
        module="slippi_ai.train_lib",
        script="scripts/train.py",
    ),
    "q_learning": dict(
        label="Q-Learning",
        description=(
            "Train a Q-function on replay data to evaluate action quality. "
            "Can be combined with imitation learning objectives."),
        module="slippi_ai.train_q_lib",
        script="scripts/train_q.py",
    ),
}

_PRESETS_FILE = "train_presets.json"

# Fields shown in the basic (non-advanced) view, keyed by dot-path.
# Everything else is hidden behind the "Advanced Settings" toggle.
_BASIC_FIELDS: dict[str, set[str]] = {
    "imitation": {
        "dataset.data_dir", "dataset.allowed_characters",
        "dataset.allowed_opponents", "data.batch_size",
        "data.unroll_length", "data.num_workers",
        "learner.learning_rate", "policy.delay",
        "runtime.max_runtime", "expt_root", "restore_pickle",
    },
    "q_learning": {
        "dataset.data_dir", "dataset.allowed_characters",
        "dataset.allowed_opponents", "data.batch_size",
        "data.unroll_length", "data.num_workers",
        "learner.learning_rate", "learner.num_samples",
        "policy.delay", "runtime.max_runtime", "expt_root",
        "restore_pickle", "initialize_policies_from",
        "rl_evaluator.use", "rl_evaluator.opponent",
    },
}

# Fields used only for testing/debugging — hidden behind a separate toggle.
_TESTING_FIELDS: set[str] = {
    "is_test", "version", "data.cached", "data.balance_characters",
    "runtime.max_eval_steps", "runtime.eval_at_start",
    "rl_evaluator.use_fake_envs",
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
        config_cls: type = getattr(mod, "Config")
        _tree_cache[script_key] = flag_utils.get_flags_from_dataclass(config_cls)  # type: ignore[assignment]
    return _tree_cache[script_key]


# ── Built-in templates (always available, cannot be deleted) ─────────────────

_BUILTIN_TEMPLATES: dict[str, dict[str, dict]] = {
    "imitation": {
        "Default (all defaults)": {},
        "Imitation Example": {
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.eval_every_n": 5000,
            "runtime.num_eval_steps": 200,
            "dataset.data_dir": "",
            "dataset.meta_path": "",
            "dataset.allowed_characters": "fox",
            "dataset.allowed_opponents": "all",
            "data.batch_size": 512,
            "data.unroll_length": 80,
            "learner.learning_rate": 1e-4,
            "learner.reward_halflife": 4,
            "network.name": "tx_like",
            "network.tx_like.num_layers": 3,
            "network.tx_like.hidden_size": 512,
            "network.tx_like.ffw_multiplier": 2,
            "policy.train_value_head": False,
            "policy.delay": 18,
            "value_function.train_separate_network": True,
            "value_function.separate_network_config": True,
            "value_function.network.name": "tx_like",
            "value_function.network.tx_like.num_layers": 1,
            "value_function.network.tx_like.hidden_size": 512,
            "value_function.network.tx_like.ffw_multiplier": 2,
            "controller_head.name": "autoregressive",
            "controller_head.autoregressive.component_depth": 2,
            "controller_head.autoregressive.residual_size": 128,
        },
    },
    "q_learning": {
        "Default (all defaults)": {},
    },
}

# ── Preset persistence ───────────────────────────────────────────────────────

def _presets_path() -> Path:
    return script_dir() / _PRESETS_FILE


def _load_all_presets() -> dict:
    """Load user presets and merge with built-in templates."""
    p = _presets_path()
    user = {}
    if p.is_file():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Merge: built-ins first, then user presets (user can override names)
    merged = {}
    for script_key in _SCRIPTS:
        builtins = _BUILTIN_TEMPLATES.get(script_key, {})
        user_presets = user.get(script_key, {})
        merged[script_key] = {**builtins, **user_presets}
    return merged


def _save_all_presets(data: dict):
    """Save user presets, excluding built-in templates."""
    user_only = {}
    for script_key, presets in data.items():
        user_only[script_key] = {
            name: values for name, values in presets.items()
            if name not in _BUILTIN_TEMPLATES.get(script_key, {})
        }
    _presets_path().write_text(
        json.dumps(user_only, indent=2, default=str), encoding="utf-8")


# ── Collapsible section ─────────────────────────────────────────────────────

class CollapsibleSection(ttk.Frame):
    """A section with a toggle arrow that shows/hides its content."""

    def __init__(self, parent, text: str, collapsed: bool = True, **kw):
        super().__init__(parent, **kw)
        self._collapsed = collapsed

        header = ttk.Frame(self)
        header.pack(fill="x")

        self._arrow = ttk.Label(
            header, text="\u25b8" if collapsed else "\u25be",
            font=("TkDefaultFont", 10), cursor="hand2", width=2)
        self._arrow.pack(side="left")
        self._title = ttk.Label(
            header, text=text, font=("TkDefaultFont", 9, "bold"),
            cursor="hand2")
        self._title.pack(side="left")

        for w in (self._arrow, self._title):
            w.bind("<Button-1>", lambda _: self._toggle())

        self.content = ttk.Frame(self, padding=(16, 2, 0, 2))
        if not collapsed:
            self.content.pack(fill="x")

    def _toggle(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.content.pack_forget()
            self._arrow.config(text="\u25b8")
        else:
            self.content.pack(fill="x")
            self._arrow.config(text="\u25be")


# ── Flag help dialog ─────────────────────────────────────────────────────────

class FlagHelpDialog(tk.Toplevel):
    """Modal dialog showing detailed help for a training flag."""

    def __init__(self, parent, path: tuple[str, ...], item, enum_class=None):
        super().__init__(parent)
        self.title(f"Help: {'.'.join(path)}")
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("560x460")

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        flag_path = "config." + ".".join(path)
        dot_key = ".".join(path)

        # ── Flag name ────────────────────────────────────────────────────
        ttk.Label(outer, text=".".join(path),
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(4, 8))

        # ── Close button (Pack this BEFORE the canvas) ───────────────────
        close_frame = ttk.Frame(outer)
        close_frame.pack(fill="x", side="bottom", pady=(8, 0))
        ttk.Button(close_frame, text="Close",
                   command=self.destroy).pack(side="right")

        # ── Scrollable content (Pack this LAST) ──────────────────────────
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = ttk.Frame(canvas, padding=(4, 0, 12, 0))
        canvas_win = canvas.create_window((0, 0), window=content, anchor="nw")

        def _update_scroll_region(_event=None):
            bbox = canvas.bbox("all")
            if bbox:
                canvas_h = canvas.winfo_height()
                content_h = bbox[3] - bbox[1]
                region_h = max(content_h, canvas_h)
                canvas.config(scrollregion=(0, 0, bbox[2], region_h))

        content.bind("<Configure>", _update_scroll_region)
        canvas.bind("<Configure>",
                    lambda e: (canvas.itemconfig(canvas_win, width=e.width),
                               _update_scroll_region()))

        # ── Technical info ───────────────────────────────────────────────
        info_frame = ttk.LabelFrame(content, text="Flag Info", padding=10)
        info_frame.pack(fill="x", pady=(0, 10), padx=(0, 4))
        info_frame.columnconfigure(1, weight=1)

        rows = [("Command-line flag:", f"--{flag_path}")]
        rows.append(("Default value:", str(item.default)))
        rows.append(("Type:", type(item).__name__))
        if item._help_string:
            rows.append(("Description:", item._help_string))
        if enum_class is not None:
            rows.append(("Options:", ", ".join(m.name for m in enum_class)))

        for i, (label, value) in enumerate(rows):
            ttk.Label(info_frame, text=label,
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=i, column=0, sticky="nw", padx=(0, 8), pady=2)
            val_label = ttk.Label(info_frame, text=value)
            val_label.grid(row=i, column=1, sticky="ew", pady=2)
            # Dynamically update the wraplength when the label resizes
            val_label.bind("<Configure>", lambda e, lbl=val_label: lbl.config(wraplength=e.width))

        # ── Explanation ──────────────────────────────────────────────────
        help_entry = _HELP_REGISTRY.get(dot_key, {})
        explanation = help_entry.get("explanation", "")
        learn_link = help_entry.get("link", "")

        if explanation:
            expl_frame = ttk.LabelFrame(content, text="What does this do?",
                                        padding=10)
            expl_frame.pack(fill="x", pady=(0, 10), padx=(0, 4))

            expl_label = ttk.Label(expl_frame, text=explanation, justify="left")
            expl_label.pack(fill="x", anchor="w")
            expl_label.bind("<Configure>", lambda e: e.widget.config(wraplength=e.width))

            if learn_link:
                link_label = tk.Label(
                    expl_frame, text="Learn more \u2192",
                    foreground="blue", cursor="hand2",
                    font=("TkDefaultFont", 9, "underline"))
                link_label.pack(anchor="w", pady=(8, 0))
                link_label.bind("<Button-1>",
                                lambda _, url=learn_link: _open_url(url))
        else:
            ttk.Label(content,
                      text="No detailed explanation available yet for this flag.",
                      foreground="gray").pack(anchor="w", pady=(0, 10))

        self.bind("<Escape>", lambda _: self.destroy())
        self.update_idletasks()
        self.grab_set()

def _open_url(url: str):
    """Open a URL in the default browser."""
    import webbrowser
    webbrowser.open(url)


# ── Character picker dialog ──────────────────────────────────────────────────

# Fields that should get a character picker button
_CHARACTER_FIELDS = {"allowed_characters", "allowed_opponents"}


class CharacterPickerDialog(tk.Toplevel):
    """Modal with checkboxes for selecting Melee characters."""

    def __init__(self, parent, var: tk.StringVar):
        super().__init__(parent)
        self.title("Select Characters")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self._var = var

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # "All" toggle
        self._all_var = tk.BooleanVar(value=(var.get().strip().lower() == "all"
                                             or var.get().strip() == ""))
        all_check = ttk.Checkbutton(
            outer, text="All characters",
            variable=self._all_var, command=self._on_all_toggle)
        all_check.pack(anchor="w", pady=(0, 8))

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(0, 8))

        # Character checkboxes in a grid
        self._char_frame = ttk.Frame(outer)
        self._char_frame.pack(fill="both")

        current = set()
        if not self._all_var.get():
            current = {c.strip().lower() for c in var.get().split(",") if c.strip()}

        self._char_vars: dict[str, tk.BooleanVar] = {}
        cols = 4
        for i, char in enumerate(CHARACTERS):
            checked = self._all_var.get() or char in current
            bv = tk.BooleanVar(value=checked)
            self._char_vars[char] = bv
            cb = ttk.Checkbutton(self._char_frame, text=char, variable=bv)
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=4, pady=1)

        self._on_all_toggle()

        # Buttons
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill="x", pady=(12, 0))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="OK",
                   command=self._on_ok).pack(side="right")

        self.bind("<Escape>", lambda _: self.destroy())

    def _on_all_toggle(self):
        for child in self._char_frame.winfo_children():
            if isinstance(child, ttk.Checkbutton):
                child.state(["disabled"] if self._all_var.get() else ["!disabled"])

    def _on_ok(self):
        if self._all_var.get():
            self._var.set("all")
        else:
            selected = [c for c, bv in self._char_vars.items() if bv.get()]
            self._var.set(",".join(selected) if selected else "all")
        self.destroy()


# ── Flag widget ──────────────────────────────────────────────────────────────

class FlagWidget:
    """Wraps a single config leaf with a tk variable and widget."""

    def __init__(self, path: tuple[str, ...], item: ff.Item, var, widget,
                 default_value, is_bool: bool = False, is_enum: bool = False,
                 enum_class=None):
        self.path = path
        self.item = item
        self.var = var
        self.widget = widget
        self.default_value = default_value
        self.is_bool = is_bool
        self.is_enum = is_enum
        self.enum_class = enum_class

    def get_value(self):
        """Return the typed Python value from the widget."""
        if self.is_bool:
            return self.var.get()
        raw = self.var.get()
        if raw == "" or raw == "None":
            return None
        if self.is_enum and self.enum_class is not None:
            # Return the enum member
            try:
                return self.enum_class[raw]
            except KeyError:
                # Try case-insensitive
                for m in self.enum_class:
                    if m.name.upper() == raw.upper():
                        return m
                return raw
        if self.default_value is not None:
            try:
                if isinstance(self.default_value, int) and not isinstance(self.default_value, bool):
                    return int(raw)
                if isinstance(self.default_value, float):
                    return float(raw)
            except (ValueError, TypeError):
                pass
        return raw

    def set_value(self, value):
        """Set the widget from a Python value."""
        if self.is_bool:
            self.var.set(bool(value))
            return
        if self.is_enum and self.enum_class is not None and isinstance(value, enum.Enum):
            self.var.set(value.name)
            return
        if value is None:
            self.var.set("")
        elif isinstance(value, (list, tuple)):
            self.var.set(",".join(str(v) for v in value))
        elif isinstance(value, float):
            self.var.set(_smart_format(value))
        else:
            self.var.set(str(value))


# ── Numeric stepper ──────────────────────────────────────────────────────────

# Module-level flag toggled by the UI checkbox
_use_scientific: bool = False


def _smart_format(value: float) -> str:
    """Format a float, using ML-style scientific notation (1e-4) when enabled."""
    if value == 0:
        return "0"
    if _use_scientific:
        abs_val = abs(value)
        if abs_val < 0.01 or abs_val >= 10000:
            # ML-style: 1e-4, 3e-5, 5e+05
            import math
            exp = math.floor(math.log10(abs_val))
            coeff = value / (10 ** exp)
            if coeff == int(coeff):
                return f"{int(coeff)}e{exp}"
            return f"{coeff:g}e{exp}"
    return f"{value:g}"


def _make_spinbox(parent, var: tk.StringVar, is_float: bool = True,
                  width: int = 12, allow_negative: bool = False) -> ttk.Spinbox:
    """Create a Spinbox that handles None/empty and optional negativity."""
    lo = -1e15 if allow_negative else 0
    inc = 0.1 if is_float else 1

    def _on_step():
        raw = var.get().strip()
        if not raw or raw == "None":
            # Empty/None → start at 1 (or 0.1 for float)
            var.set("1" if not is_float else _smart_format(1.0))

    sb = ttk.Spinbox(parent, textvariable=var, width=width,
                     from_=lo, to=1e15, increment=inc,
                     command=_on_step)
    return sb


# ── Config editor panel ──────────────────────────────────────────────────────

class ConfigEditorPanel:
    """Dynamically builds config widgets from a fancyflags Item tree."""

    def __init__(self, parent_frame: ttk.Frame):
        self._parent = parent_frame
        self._widgets: dict[tuple[str, ...], FlagWidget] = {}
        self._sections: list[tk.Widget] = []
        self._advanced_widgets: list[tk.Widget] = []
        self._testing_widgets: list[tk.Widget] = []
        # Original build order per parent: [(widget, pack_kwargs), ...]
        self._pack_order: list[tuple[tk.Widget, dict]] = []
        self._basic_fields: set[str] = set()
        self._testing_fields: set[str] = set()
        self._show_advanced: bool = False
        self._show_testing: bool = False
        self._project_root: str = ""

    def clear(self):
        """Remove all widgets."""
        for w in self._sections:
            w.destroy()
        self._sections.clear()
        self._widgets.clear()
        self._advanced_widgets.clear()
        self._testing_widgets.clear()
        self._pack_order.clear()

    def build(self, tree: dict, path_prefix: tuple[str, ...] = (),
              basic_fields: set[str] | None = None,
              testing_fields: set[str] | None = None):
        """Build widgets from a fancyflags Item tree."""
        self.clear()
        self._basic_fields = basic_fields or set()
        self._testing_fields = testing_fields or set()
        self._build_level(self._parent, tree, path_prefix)
        # Apply current visibility
        self.set_advanced(self._show_advanced)
        self.set_testing(self._show_testing)

    @staticmethod
    def _has_basic_descendant(tree: dict, prefix: tuple[str, ...],
                              basic_fields: set[str]) -> bool:
        """Check if any leaf in this subtree is a basic field."""
        for key, value in tree.items():
            path = prefix + (key,)
            dot_path = ".".join(path)
            if isinstance(value, dict):
                if ConfigEditorPanel._has_basic_descendant(
                        value, path, basic_fields):
                    return True
            elif dot_path in basic_fields:
                return True
        return False

    def _build_level(self, parent, tree: dict, prefix: tuple[str, ...]):
        for key, value in tree.items():
            path = prefix + (key,)
            dot_path = ".".join(path)
            if isinstance(value, dict):
                section = CollapsibleSection(parent, text=key, collapsed=True)
                pack_kw = dict(fill="x", padx=2, pady=1)
                section.pack(**pack_kw)
                self._sections.append(section)
                self._pack_order.append((section, pack_kw))
                # If no basic fields in this subtree, mark entire section advanced
                if (self._basic_fields
                        and not self._has_basic_descendant(
                            value, path, self._basic_fields)):
                    self._advanced_widgets.append(section)
                self._build_level(section.content, value, path)
            elif isinstance(value, (ff.Item, MultiItem)):
                row = self._build_flag_row(parent, key, value, path)
                self._sections.append(row)
                # Categorize: testing > advanced > basic
                if dot_path in self._testing_fields:
                    self._testing_widgets.append(row)
                elif self._basic_fields and dot_path not in self._basic_fields:
                    self._advanced_widgets.append(row)
            # else: skip unsupported types

    # Keys that should get a file/directory browse button.
    # expt_dir is excluded — it's a subfolder name, not a path.
    _PATH_KEYWORDS = {"path", "pickle", "restore", "teacher",
                      "initialize", "meta_path", "data_dir",
                      "expt_root", "gecko_codes_file", "replay_dir"}

    @staticmethod
    def _is_path_field(key: str) -> bool:
        """Check if a field name looks like a file or directory path."""
        return any(kw in key for kw in ConfigEditorPanel._PATH_KEYWORDS)

    def _build_flag_row(self, parent, key: str, item, path: tuple[str, ...]) -> ttk.Frame:
        row = ttk.Frame(parent)
        pack_kw = dict(fill="x", padx=4, pady=1)
        row.pack(**pack_kw)
        self._pack_order.append((row, pack_kw))

        label = ttk.Label(row, text=key, width=28, anchor="w")
        label.grid(row=0, column=0, sticky="w")

        default = item.default
        is_bool = isinstance(item, ff.Boolean)
        is_enum = isinstance(item, ff.EnumClass)
        is_str_enum = isinstance(item, ff.Enum)
        enum_class = None

        # Column for extra buttons (browse, select, help)
        extras = ttk.Frame(row)
        extras.grid(row=0, column=2, sticky="w", padx=(4, 0))

        if is_bool:
            var = tk.BooleanVar(value=bool(default) if default is not None else False)
            widget = ttk.Checkbutton(row, variable=var)
            widget.grid(row=0, column=1, sticky="w")
        elif is_enum:
            parser = cast(ap.EnumClassParser, item._parser)
            enum_class = parser.enum_class
            values = [m.name for m in enum_class]
            var = tk.StringVar(value=default.name if default is not None else "")
            widget = ttk.Combobox(row, textvariable=var, values=values,
                                  state="readonly", width=20)
            widget.grid(row=0, column=1, sticky="w")
        elif is_str_enum:
            parser = cast(ap.EnumParser, item._parser)
            values = list(parser.enum_values)
            var = tk.StringVar(value=str(default) if default is not None else "")
            widget = ttk.Combobox(row, textvariable=var, values=values,
                                  state="readonly", width=20)
            widget.grid(row=0, column=1, sticky="w")
        elif isinstance(item, (ff.Sequence, ff.StringList)):
            val = ""
            if default is not None:
                val = ",".join(str(v) for v in default)
            var = tk.StringVar(value=val)
            widget = ttk.Entry(row, textvariable=var, width=24)
            widget.grid(row=0, column=1, sticky="w")
        elif isinstance(item, ff.Float):
            val = "" if default is None else _smart_format(default)
            neg = default is not None and default < 0
            var = tk.StringVar(value=val)
            widget = _make_spinbox(row, var, is_float=True, allow_negative=neg)
            widget.grid(row=0, column=1, sticky="w")
        elif isinstance(item, ff.Integer):
            val = "" if default is None else str(default)
            neg = default is not None and default < 0
            var = tk.StringVar(value=val)
            widget = _make_spinbox(row, var, is_float=False, allow_negative=neg)
            widget.grid(row=0, column=1, sticky="w")
        else:
            # String or unknown
            val = "" if default is None else str(default)
            var = tk.StringVar(value=val)
            widget = ttk.Entry(row, textvariable=var, width=24)
            widget.grid(row=0, column=1, sticky="w")

        # Browse button for path-like String fields
        if isinstance(item, ff.String) and self._is_path_field(key):
            _str_var: tk.StringVar = var  # type: ignore[assignment]
            def _browse(v: tk.StringVar = _str_var, k: str = key):
                # Start browsing from the current value or the project root
                initial = v.get().strip()
                if initial and os.path.exists(initial):
                    initial_dir = initial if os.path.isdir(initial) else os.path.dirname(initial)
                else:
                    initial_dir = self._project_root or ""

                is_dir = "root" in k
                if is_dir:
                    p = filedialog.askdirectory(
                        title=f"Select {k}", initialdir=initial_dir)
                else:
                    p = filedialog.askopenfilename(
                        title=f"Select {k}", initialdir=initial_dir,
                        filetypes=[("Pickle", "*.pkl"), ("All files", "*.*")])
                if p:
                    # Show relative path if within the project
                    if self._project_root:
                        try:
                            p = os.path.relpath(p, self._project_root)
                        except ValueError:
                            pass  # different drive on Windows
                    v.set(p)
            ttk.Button(extras, text="Browse\u2026", command=_browse
                       ).pack(side="left")

        # Character picker for allowed_characters / allowed_opponents
        if isinstance(item, ff.String) and key in _CHARACTER_FIELDS:
            _char_var: tk.StringVar = var  # type: ignore[assignment]
            ttk.Button(
                extras, text="Select\u2026",
                command=lambda v=_char_var: CharacterPickerDialog(
                    row.winfo_toplevel(), v)
            ).pack(side="left", padx=(4, 0))

        # Help button opens a modal with detailed explanation
        help_btn = ttk.Label(extras, text="?", foreground="white",
                             background="#4a90d9", cursor="hand2",
                             font=("TkDefaultFont", 8, "bold"),
                             width=2, anchor="center", relief="raised")
        help_btn.pack(side="left", padx=(4, 0))
        ec = enum_class  # capture for closure
        help_btn.bind("<Button-1>",
                      lambda _, p=path, it=item, e=ec:
                          FlagHelpDialog(row.winfo_toplevel(), p, it, e))

        fw = FlagWidget(
            path=path, item=item, var=var, widget=widget,
            default_value=default, is_bool=is_bool,
            is_enum=is_enum, enum_class=enum_class)
        self._widgets[path] = fw
        return row

    @property
    def widgets(self) -> dict[tuple[str, ...], FlagWidget]:
        return self._widgets

    def get_all_values(self) -> dict[str, object]:
        """Return all values as a dot-keyed dict for serialization."""
        result = {}
        for path, fw in self._widgets.items():
            key = ".".join(path)
            val = fw.get_value()
            if isinstance(val, enum.Enum):
                val = val.name
            result[key] = val
        return result

    def set_all_values(self, data: dict[str, object]):
        """Populate widgets from a dot-keyed dict."""
        for path, fw in self._widgets.items():
            key = ".".join(path)
            if key in data:
                fw.set_value(data[key])

    def reset_to_defaults(self):
        """Reset all widgets to their default values."""
        for fw in self._widgets.values():
            fw.set_value(fw.default_value)

    def set_advanced(self, show: bool):
        """Show or hide advanced (non-basic) widgets."""
        self._show_advanced = show
        self._repack_all()

    def set_testing(self, show: bool):
        """Show or hide testing/debugging-only widgets."""
        self._show_testing = show
        self._repack_all()

    def _repack_all(self):
        """Re-pack all widgets in their original build order, respecting visibility."""
        hidden = set()
        if not self._show_advanced:
            hidden.update(id(w) for w in self._advanced_widgets)
        if not self._show_testing:
            hidden.update(id(w) for w in self._testing_widgets)
        for widget, pack_kw in self._pack_order:
            widget.pack_forget()
        for widget, pack_kw in self._pack_order:
            if id(widget) not in hidden:
                widget.pack(**pack_kw)

    def refresh_float_formats(self):
        """Re-format all float widget values using the current notation."""
        for fw in self._widgets.values():
            if isinstance(fw.item, ff.Float):
                raw = fw.var.get().strip()
                if raw and raw != "None":
                    try:
                        fw.var.set(_smart_format(float(raw)))
                    except ValueError:
                        pass


# ── Format value for command line ────────────────────────────────────────────

def _format_flag_value(value) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, (list, tuple)):
        inner = ",".join(str(v) for v in value)
        return f"[{inner}]"
    return str(value)


# ── Main screen ──────────────────────────────────────────────────────────────

class TrainILScreen(Screen):
    """Dynamic training configuration screen for IL and Q-learning."""

    def __init__(self, parent, navigator, cfg: AppConfig):
        super().__init__(parent, navigator, cfg)
        self._add_back_button()

        self._current_script: str = cfg.get("train_il", "last_script", "imitation")
        self._loaded = False
        self._proc: subprocess.Popen | None = None

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
            value=cfg.get("train_il", "last_preset", ""))
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
        # Set scroll region to the actual content size, but never smaller
        # than the visible canvas height. This prevents the content from
        # being scrollable when it's shorter than the viewport, which
        # would otherwise allow scrolling into empty space.
        content_bbox = self._canvas.bbox("all")
        if content_bbox:
            canvas_h = self._canvas.winfo_height()
            content_h = content_bbox[3] - content_bbox[1]
            region_h = max(content_h, canvas_h)
            self._canvas.config(
                scrollregion=(0, 0, content_bbox[2], region_h))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        # Recompute scroll region when canvas resizes
        self._on_inner_configure()

    def _on_mousewheel(self, event):
        if not self._canvas.winfo_ismapped():
            return
        # Linux/X11 uses Button-4 (scroll up) and Button-5 (scroll down)
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
        self.cfg.set("train_il", "last_script", self._current_script)
        self.cfg.set("train_il", "last_preset", self._preset_var.get())
        self.cfg.save()

    # ── Script loading ───────────────────────────────────────────────────

    def _on_script_change(self):
        key = self._script_var.get()
        if key != self._current_script:
            self._current_script = key
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
        global _use_scientific
        _use_scientific = self._sci_var.get()
        self._editor.refresh_float_formats()

    def _on_testing_toggle(self):
        self._editor.set_testing(self._testing_var.get())

    # ── Preset management ────────────────────────────────────────────────

    def _refresh_preset_list(self):
        all_presets = _load_all_presets()
        names = list(all_presets.get(self._current_script, {}).keys())
        self._preset_combo["values"] = names
        if self._preset_var.get() not in names:
            self._preset_var.set(names[0] if names else "")

    def _apply_preset(self, name: str):
        all_presets = _load_all_presets()
        data = all_presets.get(self._current_script, {}).get(name)
        if data is None:
            return
        # Empty dict means "use all defaults"
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
        all_presets = _load_all_presets()
        script_presets = all_presets.setdefault(self._current_script, {})
        script_presets[name] = self._editor.get_all_values()
        _save_all_presets(all_presets)
        self._refresh_preset_list()

    def _delete_preset(self):
        name = self._preset_var.get()
        if not name:
            return
        # Prevent deleting built-in templates
        if name in _BUILTIN_TEMPLATES.get(self._current_script, {}):
            messagebox.showinfo("Built-in Template",
                                "Built-in templates cannot be deleted. "
                                "Use 'Save As' to create your own copy.")
            return
        all_presets = _load_all_presets()
        script_presets = all_presets.get(self._current_script, {})
        if name in script_presets:
            del script_presets[name]
            _save_all_presets(all_presets)
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

        # Also pass wandb flags if needed (disabled by default)
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

    # ── Execution ────────────────────────────────────────────────────────

    def _on_run(self):
        if self._proc and self._proc.poll() is None:
            self._kill()
            return

        cmd = self._build_command()
        if cmd is None:
            return

        root = self.cfg.get("paths", "slippi_ai_root")

        # Apply the same hardware optimizations as runs/env.sh
        env = os.environ.copy()
        env["OMP_NUM_THREADS"] = "1"
        env["TF_ENABLE_ONEDNN_OPTS"] = "1"

        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(cmd, cwd=root, env=env)
            else:
                self._proc = subprocess.Popen(
                    cmd, cwd=root, env=env, start_new_session=True)
        except Exception as exc:
            self._status_var.set(f"Error: {exc}")
            self._status_label.config(foreground="red")
            return

        self._run_btn.config(text="Stop")
        self._status_var.set("Running...")
        self._status_label.config(foreground="orange")
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        if self._proc:
            self._proc.wait()
        self.after(0, self._on_complete)

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
