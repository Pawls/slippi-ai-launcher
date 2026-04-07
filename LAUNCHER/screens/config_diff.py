"""Config diff viewer: compare two training presets side-by-side."""

import tkinter as tk
from tkinter import ttk

from LAUNCHER.config import AppConfig
from LAUNCHER.config_ui import min_col_width
from LAUNCHER.screens import Screen
from LAUNCHER.screens.train_il import (
    _BUILTIN_TEMPLATES as _IL_BUILTINS,
    _load_all_presets as _load_il_presets,
    _PRESETS_FILE,
)
from LAUNCHER.screens.train_rl import (
    _BUILTIN_TEMPLATES as _RL_BUILTINS,
    _load_rl_presets,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_SCRIPT_LABELS = {
    "imitation": "IL",
    "q_learning": "Q-Learn",
    "rl_single": "RL Single",
    "rl_train_two": "RL Two",
}

# Cache for flattened default values per script key.
_defaults_cache: dict[str, dict[str, object]] = {}


def _flatten_defaults(tree: dict, prefix: str = "") -> dict[str, object]:
    """Flatten a fancyflags Item tree into {dot_path: default_value}."""
    result = {}
    for k, v in tree.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten_defaults(v, path))
        else:
            result[path] = v.default
    return result


def _get_defaults(script_key: str) -> dict[str, object]:
    """Return flattened default config for a script (lazy, cached)."""
    if script_key not in _defaults_cache:
        try:
            if script_key in ("imitation", "q_learning"):
                from LAUNCHER.screens.train_il import _get_flag_tree
            else:
                from LAUNCHER.screens.train_rl import _get_flag_tree
            tree = _get_flag_tree(script_key)
            _defaults_cache[script_key] = _flatten_defaults(tree)
        except Exception:
            _defaults_cache[script_key] = {}
    return _defaults_cache[script_key]


def _collect_presets() -> list[dict]:
    """Return a flat list of all presets across IL and RL scripts.

    Each entry: {"key": display_key, "script": script_key, "name": preset_name,
                 "data": {flat config dict}, "builtin": bool}
    """
    results = []

    # IL presets
    il_presets = _load_il_presets()
    for script_key in ("imitation", "q_learning"):
        builtins = _IL_BUILTINS.get(script_key, {})
        for name, data in il_presets.get(script_key, {}).items():
            label = _SCRIPT_LABELS.get(script_key, script_key)
            results.append(dict(
                key=f"[{label}] {name}",
                script=script_key,
                name=name,
                data=data,
                builtin=name in builtins,
            ))

    # RL presets
    rl_presets = _load_rl_presets()
    for script_key in ("rl_single", "rl_train_two"):
        builtins = _RL_BUILTINS.get(script_key, {})
        for name, data in rl_presets.get(script_key, {}).items():
            label = _SCRIPT_LABELS.get(script_key, script_key)
            results.append(dict(
                key=f"[{label}] {name}",
                script=script_key,
                name=name,
                data=data,
                builtin=name in builtins,
            ))

    return results


def _format_value(v) -> str:
    """Format a config value for display."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, float):
        if v != 0 and (abs(v) < 0.01 or abs(v) >= 1e6):
            return f"{v:.2e}"
        return str(v)
    if v is None:
        return "None"
    return str(v)


def _section_key(dot_path: str) -> str:
    """Extract the top-level section from a dot-path config key."""
    parts = dot_path.split(".")
    return parts[0] if len(parts) > 1 else "(top-level)"


# ── Config Diff Screen ──────────────────────────────────────────────────────

class ConfigDiffScreen(Screen):
    """Compare two training presets side-by-side with differences highlighted."""

    def __init__(self, parent, navigator, cfg: AppConfig):
        super().__init__(parent, navigator, cfg)
        self._add_back_button()

        self._presets: list[dict] = []

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Config Diff Viewer",
                  font=("Arial", 14, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Compare two training presets side-by-side",
                  foreground="gray").pack(anchor="w", pady=(0, 8))

        # ── Selector row ─────────────────────────────────────────────────
        sel = ttk.Frame(outer)
        sel.pack(fill="x", pady=(0, 8))

        ttk.Label(sel, text="Preset A:").grid(row=0, column=0, sticky="w")
        self._var_a = tk.StringVar()
        self._combo_a = ttk.Combobox(
            sel, textvariable=self._var_a, width=48, state="readonly")
        self._combo_a.grid(row=0, column=1, padx=(4, 16), sticky="ew")

        ttk.Label(sel, text="Preset B:").grid(row=0, column=2, sticky="w")
        self._var_b = tk.StringVar()
        self._combo_b = ttk.Combobox(
            sel, textvariable=self._var_b, width=48, state="readonly")
        self._combo_b.grid(row=0, column=3, padx=(4, 16), sticky="ew")

        ttk.Button(sel, text="Compare", command=self._compare).grid(
            row=0, column=4, padx=(4, 0))

        sel.columnconfigure(1, weight=1)
        sel.columnconfigure(3, weight=1)

        # ── Filter row ───────────────────────────────────────────────────
        filt = ttk.Frame(outer)
        filt.pack(fill="x", pady=(0, 8))

        self._diff_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt, text="Show differences only",
                        variable=self._diff_only_var,
                        command=self._compare).pack(side="left")

        self._group_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt, text="Group by section",
                        variable=self._group_var,
                        command=self._compare).pack(side="left", padx=(16, 0))

        # ── Summary label ────────────────────────────────────────────────
        self._summary_var = tk.StringVar(value="Select two presets to compare.")
        ttk.Label(outer, textvariable=self._summary_var,
                  foreground="gray", font=("TkDefaultFont", 9)).pack(
            anchor="w", pady=(0, 4))

        # ── Legend ───────────────────────────────────────────────────────
        legend = ttk.Frame(outer)
        legend.pack(fill="x", pady=(0, 4))
        for color, text in [("#fff3cd", "Changed"),
                            ("#d4edda", "Only in A (B = default)"),
                            ("#cce5ff", "Only in B (A = default)")]:
            swatch = tk.Frame(legend, bg=color, width=14, height=14,
                              highlightbackground="gray", highlightthickness=1)
            swatch.pack(side="left", padx=(0, 2))
            swatch.pack_propagate(False)
            ttk.Label(legend, text=text,
                      font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 12))

        # ── Diff treeview ────────────────────────────────────────────────
        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)

        cols = ("key", "value_a", "value_b")
        self._tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="browse")
        self._tree.heading("key", text="Config Key", anchor="w",
                           command=lambda: self._sort("key"))
        self._tree.heading("value_a", text="Preset A", anchor="w",
                           command=lambda: self._sort("value_a"))
        self._tree.heading("value_b", text="Preset B", anchor="w",
                           command=lambda: self._sort("value_b"))
        self._tree.column("key", width=max(280, min_col_width("Config Key")),
                          minwidth=180, anchor="w")
        self._tree.column("value_a", width=max(200, min_col_width("Preset A")),
                          minwidth=100, anchor="w")
        self._tree.column("value_b", width=max(200, min_col_width("Preset B")),
                          minwidth=100, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                            command=self._tree.xview)
        self._tree.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        # Tag styles for highlighting
        self._tree.tag_configure("diff", background="#fff3cd")
        self._tree.tag_configure("only_a", background="#d4edda")
        self._tree.tag_configure("only_b", background="#cce5ff")
        self._tree.tag_configure("same", background="")
        self._tree.tag_configure("section", font=("TkDefaultFont", 9, "bold"))

        self._sort_col = "key"
        self._sort_reverse = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_enter(self):
        self._presets = _collect_presets()
        keys = [p["key"] for p in self._presets]
        self._combo_a["values"] = keys
        self._combo_b["values"] = keys

        # Preserve selections if still valid
        if self._var_a.get() not in keys:
            self._var_a.set(keys[0] if keys else "")
        if self._var_b.get() not in keys:
            self._var_b.set(keys[1] if len(keys) > 1 else keys[0] if keys else "")

    # ── Compare logic ────────────────────────────────────────────────────

    def _resolve_value(self, data: dict, defaults: dict, key: str):
        """Return (display_string, is_from_preset).

        If the key is in data, returns (formatted_value, True).
        If not, looks up the default and returns (formatted_default, False).
        """
        if key in data:
            return _format_value(data[key]), True
        if key in defaults:
            return _format_value(defaults[key]) + " (default)", False
        return "(unknown)", False

    def _compare(self):
        key_a = self._var_a.get()
        key_b = self._var_b.get()
        if not key_a or not key_b:
            return

        preset_a = next((p for p in self._presets if p["key"] == key_a), None)
        preset_b = next((p for p in self._presets if p["key"] == key_b), None)
        if not preset_a or not preset_b:
            return

        data_a = preset_a["data"]
        data_b = preset_b["data"]
        diff_only = self._diff_only_var.get()
        group = self._group_var.get()

        # Load defaults for the script types (lazy, cached)
        defaults_a = _get_defaults(preset_a["script"])
        defaults_b = _get_defaults(preset_b["script"])

        # Update column headers
        self._tree.heading("value_a", text=preset_a["name"], anchor="w",
                           command=lambda: self._sort("value_a"))
        self._tree.heading("value_b", text=preset_b["name"], anchor="w",
                           command=lambda: self._sort("value_b"))

        # Collect all keys from both presets
        all_keys = sorted(set(data_a.keys()) | set(data_b.keys()))

        # Build rows
        rows = []
        num_diff = 0
        num_only_a = 0
        num_only_b = 0
        for k in all_keys:
            in_a = k in data_a
            in_b = k in data_b

            val_a, _ = self._resolve_value(data_a, defaults_a, k)
            val_b, _ = self._resolve_value(data_b, defaults_b, k)

            if in_a and in_b:
                if str(data_a[k]) == str(data_b[k]):
                    tag = "same"
                else:
                    tag = "diff"
                    num_diff += 1
            elif in_a:
                tag = "only_a"
                num_only_a += 1
            else:
                tag = "only_b"
                num_only_b += 1

            if diff_only and tag == "same":
                continue
            rows.append((k, val_a, val_b, tag))

        # Populate treeview
        self._tree.delete(*self._tree.get_children())

        if group:
            # Group by top-level section
            sections: dict[str, list] = {}
            for k, va, vb, tag in rows:
                sec = _section_key(k)
                sections.setdefault(sec, []).append((k, va, vb, tag))

            for sec in sorted(sections.keys()):
                self._tree.insert("", "end", values=(f"── {sec} ──", "", ""),
                                  tags=("section",))
                for k, va, vb, tag in sections[sec]:
                    self._tree.insert("", "end", values=(k, va, vb),
                                      tags=(tag,))
        else:
            for k, va, vb, tag in rows:
                self._tree.insert("", "end", values=(k, va, vb), tags=(tag,))

        # Summary
        total = len(all_keys)
        parts = []
        if num_diff:
            parts.append(f"{num_diff} changed")
        if num_only_a:
            parts.append(f"{num_only_a} only in A")
        if num_only_b:
            parts.append(f"{num_only_b} only in B")
        same = total - num_diff - num_only_a - num_only_b
        if same:
            parts.append(f"{same} identical")

        if key_a == key_b:
            self._summary_var.set(f"Comparing preset to itself ({total} keys).")
        elif not all_keys:
            self._summary_var.set("Both presets are empty (all defaults).")
        else:
            self._summary_var.set(
                f"{total} keys total: " + ", ".join(parts) + ".")

    # ── Sorting ──────────────────────────────────────────────────────────

    def _sort(self, col: str):
        if col == self._sort_col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False

        items = []
        for iid in self._tree.get_children():
            vals = self._tree.item(iid, "values")
            tags = self._tree.item(iid, "tags")
            items.append((vals, tags))

        # Separate section headers and data rows
        sections_and_rows = []
        current_section = None
        current_rows = []

        for vals, tags in items:
            if "section" in tags:
                if current_section is not None:
                    sections_and_rows.append((current_section, current_rows))
                current_section = (vals, tags)
                current_rows = []
            else:
                current_rows.append((vals, tags))

        if current_section is not None:
            sections_and_rows.append((current_section, current_rows))
        elif current_rows:
            sections_and_rows.append((None, current_rows))

        self._tree.delete(*self._tree.get_children())

        col_idx = {"key": 0, "value_a": 1, "value_b": 2}.get(col, 0)

        for sec_header, sec_rows in sections_and_rows:
            if sec_header is not None:
                self._tree.insert("", "end", values=sec_header[0],
                                  tags=sec_header[1])
            sec_rows.sort(key=lambda x: x[0][col_idx],
                          reverse=self._sort_reverse)
            for vals, tags in sec_rows:
                self._tree.insert("", "end", values=vals, tags=tags)
