"""Shared GUI widgets for the Slippi-AI Launcher."""

import tkinter as tk
from tkinter import ttk


def selectable_value(parent, text, width=None, font=None, wraplength=None):
    """Create a read-only Entry that allows text selection and Ctrl+C copy.

    Use this instead of ttk.Label when the user should be able to
    select and copy the displayed value.
    """
    var = tk.StringVar(value=text)
    kw = dict(textvariable=var, state="readonly")
    if width is not None:
        kw["width"] = width
    if font is not None:
        kw["font"] = font
    entry = ttk.Entry(parent, **kw)
    entry._sv_ref = var  # prevent garbage collection of the StringVar
    return entry


def selectable_text(parent, text, height=4, width=None, font=None):
    """Create a read-only Text widget that allows selection and copy.

    Use this for multi-line content that should be selectable but not
    editable (e.g. explanations, descriptions).
    """
    kw = dict(wrap="word", height=height)
    if width is not None:
        kw["width"] = width
    if font is not None:
        kw["font"] = font
    tw = tk.Text(parent, **kw)
    tw.insert("1.0", text)
    # Allow selection and copy, block all other input
    tw.bind("<Key>", _readonly_key_filter)
    tw.configure(cursor="arrow")
    return tw


def _readonly_key_filter(event):
    """Allow Ctrl+C / Ctrl+A in a Text widget, block everything else."""
    if event.state & 0x4:  # Ctrl held
        if event.keysym in ("c", "C", "a", "A"):
            return  # allow
    return "break"
