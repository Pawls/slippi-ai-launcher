"""Train Imitation screen: dynamic config UI for IL and Q-learning scripts."""

import enum
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import fancyflags as ff
from fancyflags._definitions import MultiItem

from LAUNCHER.config import AppConfig, find_script, script_dir
from LAUNCHER.screens import Screen
from LAUNCHER.screens.train_help import HELP as _HELP_REGISTRY

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


# ── Lazy imports for heavy modules ───────────────────────────────────────────

_module_cache: dict[str, object] = {}
_tree_cache: dict[str, dict] = {}


def _import_module(dotted: str):
    """Import a module by dotted name, caching the result."""
    if dotted not in _module_cache:
        import importlib
        _module_cache[dotted] = importlib.import_module(dotted)
    return _module_cache[dotted]


def _get_flag_tree(script_key: str) -> dict:
    """Return the fancyflags Item tree for a script, caching the result."""
    if script_key not in _tree_cache:
        from slippi_ai import flag_utils
        info = _SCRIPTS[script_key]
        mod = _import_module(info["module"])
        _tree_cache[script_key] = flag_utils.get_flags_from_dataclass(mod.Config)
    return _tree_cache[script_key]


# ── Preset persistence ───────────────────────────────────────────────────────

def _presets_path() -> Path:
    return script_dir() / _PRESETS_FILE


def _load_all_presets() -> dict:
    p = _presets_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_all_presets(data: dict):
    _presets_path().write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8")


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
        self.grab_set()
        self.resizable(True, True)
        self.geometry("520x420")

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        flag_path = "config." + ".".join(path)
        dot_key = ".".join(path)

        # ── Flag name ────────────────────────────────────────────────────
        ttk.Label(outer, text=".".join(path),
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(4, 8))

        # Scrollable content
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = ttk.Frame(canvas)
        canvas_win = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>",
                     lambda _: canvas.config(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(canvas_win, width=e.width))

        # ── Technical info ───────────────────────────────────────────────
        info_frame = ttk.LabelFrame(content, text="Flag Info", padding=8)
        info_frame.pack(fill="x", pady=(0, 8))

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
                row=i, column=0, sticky="nw", padx=(0, 8), pady=1)
            val_label = ttk.Label(info_frame, text=value, wraplength=380)
            val_label.grid(row=i, column=1, sticky="w", pady=1)

        # ── Explanation ──────────────────────────────────────────────────
        help_entry = _HELP_REGISTRY.get(dot_key, {})
        explanation = help_entry.get("explanation", "")
        learn_link = help_entry.get("link", "")

        if explanation:
            expl_frame = ttk.LabelFrame(content, text="What does this do?",
                                        padding=8)
            expl_frame.pack(fill="x", pady=(0, 8))

            expl_label = ttk.Label(expl_frame, text=explanation,
                                   wraplength=440, justify="left")
            expl_label.pack(anchor="w")

            if learn_link:
                link_label = tk.Label(
                    expl_frame, text="Learn more \u2192",
                    foreground="blue", cursor="hand2",
                    font=("TkDefaultFont", 9, "underline"))
                link_label.pack(anchor="w", pady=(6, 0))
                link_label.bind("<Button-1>",
                                lambda _, url=learn_link: _open_url(url))
        else:
            ttk.Label(content,
                      text="No detailed explanation available yet for this flag.",
                      foreground="gray").pack(anchor="w", pady=(0, 8))

        # ── Close button ─────────────────────────────────────────────────
        close_frame = ttk.Frame(outer)
        close_frame.pack(fill="x", side="bottom", pady=(8, 0))
        ttk.Button(close_frame, text="Close",
                   command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda _: self.destroy())


def _open_url(url: str):
    """Open a URL in the default browser."""
    import webbrowser
    webbrowser.open(url)


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
        else:
            self.var.set(str(value))


# ── Config editor panel ──────────────────────────────────────────────────────

class ConfigEditorPanel:
    """Dynamically builds config widgets from a fancyflags Item tree."""

    def __init__(self, parent_frame: ttk.Frame):
        self._parent = parent_frame
        self._widgets: dict[tuple[str, ...], FlagWidget] = {}
        self._sections: list[tk.Widget] = []

    def clear(self):
        """Remove all widgets."""
        for w in self._sections:
            w.destroy()
        self._sections.clear()
        self._widgets.clear()

    def build(self, tree: dict, path_prefix: tuple[str, ...] = ()):
        """Build widgets from a fancyflags Item tree."""
        self.clear()
        self._build_level(self._parent, tree, path_prefix)

    def _build_level(self, parent, tree: dict, prefix: tuple[str, ...]):
        for key, value in tree.items():
            path = prefix + (key,)
            if isinstance(value, dict):
                section = CollapsibleSection(parent, text=key, collapsed=True)
                section.pack(fill="x", padx=2, pady=1)
                self._sections.append(section)
                self._build_level(section.content, value, path)
            elif isinstance(value, (ff.Item, MultiItem)):
                row = self._build_flag_row(parent, key, value, path)
                self._sections.append(row)
            # else: skip unsupported types

    def _build_flag_row(self, parent, key: str, item, path: tuple[str, ...]) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=4, pady=1)

        label = ttk.Label(row, text=key, width=28, anchor="w")
        label.pack(side="left")

        default = item.default
        is_bool = isinstance(item, ff.Boolean)
        is_enum = isinstance(item, ff.EnumClass)
        is_str_enum = isinstance(item, ff.Enum)
        enum_class = None

        if is_bool:
            var = tk.BooleanVar(value=bool(default) if default is not None else False)
            widget = ttk.Checkbutton(row, variable=var)
            widget.pack(side="left")
        elif is_enum:
            enum_class = item._parser.enum_class
            values = [m.name for m in enum_class]
            var = tk.StringVar(value=default.name if default is not None else "")
            widget = ttk.Combobox(row, textvariable=var, values=values,
                                  state="readonly", width=20)
            widget.pack(side="left")
        elif is_str_enum:
            values = list(item._parser.enum_values)
            var = tk.StringVar(value=str(default) if default is not None else "")
            widget = ttk.Combobox(row, textvariable=var, values=values,
                                  state="readonly", width=20)
            widget.pack(side="left")
        elif isinstance(item, (ff.Sequence, ff.StringList)):
            val = ""
            if default is not None:
                val = ",".join(str(v) for v in default)
            var = tk.StringVar(value=val)
            widget = ttk.Entry(row, textvariable=var, width=30)
            widget.pack(side="left")
        else:
            # Integer, Float, String, or unknown
            val = "" if default is None else str(default)
            var = tk.StringVar(value=val)
            width = 10 if isinstance(item, (ff.Integer, ff.Float)) else 30
            widget = ttk.Entry(row, textvariable=var, width=width)
            widget.pack(side="left")

        # Help button opens a modal with detailed explanation
        help_btn = ttk.Label(row, text="?", foreground="white",
                             background="#4a90d9", cursor="hand2",
                             font=("TkDefaultFont", 8, "bold"),
                             width=2, anchor="center", relief="raised")
        help_btn.pack(side="left", padx=(6, 0))
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
            font=("TkDefaultFont", 8), wraplength=500)
        self._desc_label.pack(anchor="w", pady=(4, 0))

        # ── Preset bar ───────────────────────────────────────────────────
        preset_frame = ttk.Frame(outer)
        preset_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(preset_frame, text="Preset:").pack(side="left")
        self._preset_var = tk.StringVar(
            value=cfg.get("train_il", "last_preset", ""))
        self._preset_combo = ttk.Combobox(
            preset_frame, textvariable=self._preset_var,
            width=24, state="readonly")
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

        # ── Scrollable config area ───────────────────────────────────────
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

        # Loading overlay
        self._loading_label = ttk.Label(
            self._inner_frame, text="Loading configuration...",
            foreground="gray", font=("TkDefaultFont", 11))

        self._editor = ConfigEditorPanel(self._inner_frame)

        # ── Execution panel ──────────────────────────────────────────────
        exec_frame = ttk.Frame(outer)
        exec_frame.pack(fill="x", pady=(4, 0))

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

        # Command preview (hidden by default)
        self._cmd_text = tk.Text(
            outer, height=4, wrap="word", state="disabled",
            font=("Consolas", 8))
        self._cmd_text_visible = False

    # ── Canvas helpers ───────────────────────────────────────────────────

    def _on_inner_configure(self, _event=None):
        self._canvas.config(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        # Only scroll if our canvas is visible
        if self._canvas.winfo_ismapped():
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
        self._editor.build(tree)
        self._loaded = True

        # Try to load last-used preset
        preset_name = self._preset_var.get()
        if preset_name:
            self._apply_preset(preset_name)

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
        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(cmd, cwd=root)
            else:
                self._proc = subprocess.Popen(
                    cmd, cwd=root, start_new_session=True)
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
