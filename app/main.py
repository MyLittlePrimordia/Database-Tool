#!/usr/bin/env python3
"""
Database Tool
-------------
All-in-one companion app for the offline IEM / headphone database
(database.json). Merges the four standalone utilities of the suite:

  - Database Editor      (edit, validate, audit, undo/redo)
  - Curve Converter      (Import Curves tab: convert .txt/.csv
                          measurements into the standard format, average
                          L/R and 1/2 pairs, save straight into the data
                          folder and optionally link them to the entry
                          being edited)
  - Database Compressor  (Export tab: gzip -> database.json.gz)
  - JSON Chunk Splitter  (Export tab: token-budgeted *_chunk_N.json)

Run directly with:  python main.py
Package as exe with PyInstaller (see README.md).
"""

import os
import sys
import json
import math
import time
import datetime
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import db_logic as L
import spell_logic as SP
import theme
from theme import (BG_MAIN, BG_PANEL, BG_CARD, BG_INPUT, BORDER,
                   BORDER_LIGHT, ACCENT_BLUE, ACCENT_ORANGE, ACCENT_PURPLE,
                   ACCENT_GREEN, ACCENT_RED, TEXT_MAIN, TEXT_DIM,
                   PREFERRED_FONTS, pick_emoji_font, pick_font_family)
import tools_panel
import curve_import
import win_drop

APP_TITLE = "Database Tool"

# serializes background autosave writes (one snapshot on disk at a time)
_autosave_lock = threading.Lock()
APP_VERSION = "2.0"

# ---------------------------------------------------------------------------
# THEME -- colors, fonts and font pickers live in theme.py (shared with the
# Import Curves / Export panels so every surface stays consistent).
# ---------------------------------------------------------------------------

# Display-only emoji shown next to each tag in the picker so tags are faster
# to scan visually. The underlying tag strings saved to database.json are
# never changed -- this is purely cosmetic in the UI.
TAG_EMOJI = {
    "Basshead": "💥",
    "Sub-Bass": "🌊",
    "Punchy Bass": "🥊",
    "Warm": "🌿",
    "Neutral": "⚖️",
    "V-Shaped": "🔺",
    "U-Shaped": "🧲",
    "Balanced": "☯️",
    "Bright": "✨",
    "Treblehead": "⚡",
    "Dark": "🌑",
    "Vocal-Focused": "🗣️",
    "Detailed": "💎",
    "Resolving": "🔍",
    "Technical": "🔬",
    "Wide-Stage": "🏟️",
    "Good-Imaging": "🔭",
    "Smooth": "🧈",
    "Reference": "📐",
    "Analytical": "🧠",
    "Fun": "🔥",
    "Relaxed": "😌",
    "Gaming": "🎮",
    "Competitive-Gaming": "🏆",
    "Studio-Monitoring": "🎛️",
    "Collab": "🤝",
    "Limited-Edition": "🌟",
}


def tag_label(tag):
    emoji = TAG_EMOJI.get(tag)
    return "{} {}".format(tag, emoji) if emoji else tag


# ---------------------------------------------------------------------------
# RIGHT-CLICK CONTEXT MENU FOR TEXT ENTRY WIDGETS
# ---------------------------------------------------------------------------

def entry_select_all(entry):
    entry.select_range(0, "end")
    entry.icursor("end")
    return "break"


def entry_copy(entry):
    try:
        sel = entry.selection_get()
    except Exception:
        sel = ""
    if sel:
        entry.clipboard_clear()
        entry.clipboard_append(sel)


def entry_cut(entry):
    try:
        sel = entry.selection_get()
    except Exception:
        sel = ""
    if sel:
        entry.clipboard_clear()
        entry.clipboard_append(sel)
        entry.delete("sel.first", "sel.last")


def entry_paste(entry):
    """Paste clipboard text at the cursor, replacing any selection."""
    try:
        text = entry.clipboard_get()
    except Exception:
        return
    if not text:
        return
    try:
        entry.delete("sel.first", "sel.last")
    except Exception:
        pass
    entry.insert(entry.index("insert"), text)


def entry_delete_selection(entry):
    """Delete selected text; with no selection, delete the character under
    the cursor (like Backspace-forward)."""
    try:
        if entry.selection_present():
            entry.delete("sel.first", "sel.last")
            return
    except Exception:
        pass
    pos = entry.index("insert")
    if pos < len(entry.get()):
        entry.delete(pos)


def attach_entry_context_menu(entry, extra_items=None):
    """Attach a right-click context menu (Cut / Copy / Paste / Delete /
    Select All) to an Entry widget. The menu is rebuilt on every right-click
    so Cut/Copy/Delete are enabled only when a selection exists and Paste
    only when the clipboard holds text.

    `extra_items(event, menu)`, when provided, is called first so callers
    can insert items at the top (used for spellcheck correction suggestions).

    Also binds Ctrl+A (select all), which Tk does not provide by default,
    complementing the native Ctrl+X / Ctrl+C / Ctrl+V shortcuts."""

    def _has_selection():
        try:
            return bool(entry.selection_present())
        except Exception:
            return False

    def _clipboard_has_text():
        try:
            return bool(entry.clipboard_get())
        except Exception:
            return False

    def _show_menu(event):
        has_sel = _has_selection()
        has_text = len(entry.get()) > 0
        menu = tk.Menu(entry, tearoff=0,
                       background=BG_CARD, foreground=TEXT_MAIN,
                       activebackground=BORDER_LIGHT, activeforeground=TEXT_MAIN,
                       font=(pick_font_family(), 10))
        if extra_items:
            try:
                extra_items(event, menu)
            except Exception:
                pass
        menu.add_command(label="Cut", state="normal" if has_sel else "disabled",
                         command=lambda: entry_cut(entry))
        menu.add_command(label="Copy", state="normal" if has_sel else "disabled",
                         command=lambda: entry_copy(entry))
        menu.add_command(label="Paste", state="normal" if _clipboard_has_text() else "disabled",
                         command=lambda: entry_paste(entry))
        menu.add_command(label="Delete", state="normal" if (has_sel or has_text) else "disabled",
                         command=lambda: entry_delete_selection(entry))
        menu.add_separator()
        menu.add_command(label="Select All", state="normal" if has_text else "disabled",
                         command=lambda: entry_select_all(entry))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    entry.bind("<Button-3>", _show_menu)
    # macOS aqua reports right-click as Button-2; only bind there so Linux
    # middle-click paste keeps working.
    if sys.platform == "darwin":
        entry.bind("<Button-2>", _show_menu)

    def _ctrl_a(_event=None):
        return entry_select_all(entry)

    entry.bind("<Control-a>", _ctrl_a)
    entry.bind("<Control-A>", _ctrl_a)


def resource_base():
    """Folder to look for bundled assets (icons)."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(exe_dir, "assets")):
            return exe_dir
        return getattr(sys, "_MEIPASS", exe_dir)
    return os.path.dirname(os.path.abspath(__file__))


def script_folder():
    """Folder the script/exe itself lives in (used for auto-detecting a
    database.json sitting right next to it)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class IconManager:
    def __init__(self):
        self.dir = os.path.join(resource_base(), "assets", "icons")
        self.cache = {}

    def get(self, name):
        if not name:
            return None
        if name in self.cache:
            return self.cache[name]
        # handle legacy typo: trybrid.png vs tribrid.png
        candidates = [name]
        if name == "tribrid":
            candidates.append("trybrid")
        elif name == "trybrid":
            candidates.append("tribrid")
        path = None
        for cand in candidates:
            p = os.path.join(self.dir, "{}.png".format(cand))
            if os.path.isfile(p):
                path = p
                break
        if not path:
            self.cache[name] = None
            return None
        try:
            img = tk.PhotoImage(file=path)
            # keep icons reasonably small in the UI
            w, h = img.width(), img.height()
            target = 20
            if w > target * 2:
                factor = max(1, w // target)
                img = img.subsample(factor, factor)
            self.cache[name] = img
            return img
        except Exception:
            self.cache[name] = None
            return None


ICONS = IconManager()


# ---------------------------------------------------------------------------
# TOUCH-STYLE DRAG SCROLLING
# ---------------------------------------------------------------------------
def _touch_press(widget, st, event):
    """Anchor the drag: remember pointer position and the current view
    fraction so every later motion can compute its target absolutely."""
    st["active"] = False
    st["root_y"] = getattr(event, "y_root", event.y)
    try:
        view = widget.yview()
        st["start_view"] = view[0]
        visible = view[1] - view[0]
        height = widget.winfo_height()
        st["px_per_frac"] = (height / visible) if 0 < visible < 1 and height > 1 else 0
    except Exception:
        st["start_view"] = 0.0
        st["px_per_frac"] = 0


def _touch_motion(widget, st, event):
    """One drag step. Returns 'break' while panning (suppresses the
    widget's native drag-selection), None for ordinary clicks/motions.

    The target view fraction is derived from TOTAL displacement since the
    press (absolute mapping), never from per-event increments -- this makes
    panning immune to duplicated events and jitter (no flicker)."""
    px_per_frac = st.get("px_per_frac", 0)
    if not px_per_frac:
        return None
    dy_root = getattr(event, "y_root", event.y) - st["root_y"]
    if not st["active"]:
        if abs(dy_root) < 5:
            return None
        st["active"] = True
        try:
            st["cursor"] = widget.cget("cursor")
            widget.configure(cursor="fleur")
        except Exception:
            pass
    frac = st["start_view"] - (dy_root / px_per_frac)
    widget.yview_moveto(min(1.0, max(0.0, frac)))
    return "break"


def _touch_release(widget, st, _event=None):
    if st["active"] and st["cursor"] is not None:
        try:
            widget.configure(cursor=st["cursor"])
        except Exception:
            pass
    st["active"] = False


def attach_touch_scroll(widget):
    """Smartphone-style panning: press on the content and drag vertically to
    scroll, no scrollbar grabbing needed. A short click still selects
    normally; once the drag passes a few pixels it turns into a pan.

    Works with tk.Listbox, ttk.Treeview and canvases alike (uses absolute
    yview positioning only). Returns the shared state dict so tests can
    drive _touch_* directly."""
    st = {"active": False, "root_y": 0, "start_view": 0.0,
          "px_per_frac": 0, "cursor": None}
    widget.bind("<ButtonPress-1>",
                lambda e: _touch_press(widget, st, e), add="+")
    widget.bind("<B1-Motion>", lambda e: _touch_motion(widget, st, e))
    widget.bind("<ButtonRelease-1>",
                lambda e: _touch_release(widget, st, e), add="+")
    return st


# widget classes that may start a canvas drag without stealing meaning from
# interactive controls (entries, buttons, checkboxes, spinboxes, etc.)
_PASSIVE_WIDGET_CLASSES = {"TFrame", "Frame", "TLabel", "Label", "Canvas"}


def attach_touch_scroll_canvas(canvas, root_widget=None):
    """Drag-panning for an editor-style scrollable canvas. The canvas itself
    only sees events on bare background, so the same pan behavior is also
    bound to every PASSIVE child (frames / labels) -- drags starting on text
    labels pan too. Interactive widgets are skipped entirely: clicking an
    entry, checkbox, or button behaves exactly as before.

    Returns the shared state dict."""
    st = {"active": False, "root_y": 0, "start_view": 0.0,
          "px_per_frac": 0, "cursor": None}
    canvas.bind("<ButtonPress-1>",
                lambda e: _touch_press(canvas, st, e), add="+")
    canvas.bind("<B1-Motion>", lambda e: _touch_motion(canvas, st, e))
    canvas.bind("<ButtonRelease-1>",
                lambda e: _touch_release(canvas, st, e), add="+")

    def walk(w):
        for child in w.winfo_children():
            try:
                cls = child.winfo_class()
            except Exception:
                continue
            if cls in _PASSIVE_WIDGET_CLASSES:
                child.bind("<ButtonPress-1>",
                           lambda e: _touch_press(canvas, st, e), add="+")
                child.bind("<B1-Motion>",
                           lambda e: _touch_motion(canvas, st, e))
                child.bind("<ButtonRelease-1>",
                           lambda e: _touch_release(canvas, st, e), add="+")
                walk(child)
    walk(root_widget if root_widget is not None else canvas)
    return st

# Bundled colored-emoji PNGs (Twemoji) for tags. Tk 8.6 renders emoji FONTS
# as monochrome outlines on Windows (GDI has no color glyphs), so real color
# requires actual images -- same approach as the driver/connector icons.
_TAG_ICON_CACHE = {}


def tag_icon(tag):
    """Cached PhotoImage of the tag's colored emoji PNG, or None."""
    if tag in _TAG_ICON_CACHE:
        return _TAG_ICON_CACHE[tag]
    path = os.path.join(resource_base(), "assets", "icons", "tags",
                        "{}.png".format(tag))
    img = None
    if os.path.isfile(path):
        try:
            img = tk.PhotoImage(file=path)
            factor = max(1, img.width() // 16)
            if factor > 1:
                img = img.subsample(factor, factor)
        except Exception:
            img = None
    _TAG_ICON_CACHE[tag] = img
    return img


def setup_styles(root):
    font_family = pick_font_family()
    root.option_add("*Font", "{{{}}} 10".format(font_family))
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG_MAIN, foreground=TEXT_MAIN,
                    fieldbackground=BG_INPUT, bordercolor=BORDER,
                    darkcolor=BG_PANEL, lightcolor=BG_PANEL, font=(font_family, 10))
    style.configure("TFrame", background=BG_MAIN)
    style.configure("Panel.TFrame", background=BG_PANEL)
    style.configure("Card.TFrame", background=BG_CARD, relief="solid", borderwidth=1)
    style.configure("TLabel", background=BG_MAIN, foreground=TEXT_MAIN, font=(font_family, 10))
    style.configure("Panel.TLabel", background=BG_PANEL, foreground=TEXT_MAIN)
    style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT_MAIN)
    style.configure("Dim.TLabel", background=BG_MAIN, foreground=TEXT_DIM)
    style.configure("Header.TLabel", background=BG_MAIN, foreground=ACCENT_ORANGE,
                     font=(font_family, 13, "bold"))
    style.configure("CardHeader.TLabel", background=BG_CARD, foreground=ACCENT_BLUE,
                     font=(font_family, 10, "bold"))
    style.configure("Status.TLabel", background=BG_PANEL, foreground=TEXT_DIM, font=(font_family, 9))

    style.configure("TButton", background=BG_CARD, foreground=TEXT_MAIN,
                     bordercolor=BORDER_LIGHT, focusthickness=1, padding=6)
    style.map("TButton", background=[("active", BORDER_LIGHT)])

    style.configure("Accent.TButton", background=ACCENT_ORANGE, foreground="#1a1a1a",
                     bordercolor=ACCENT_ORANGE, padding=6, font=(font_family, 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#f2a75a")])

    style.configure("Danger.TButton", background=ACCENT_RED, foreground="#1a1a1a", padding=6)
    style.map("Danger.TButton", background=[("active", "#ea7d76")])

    style.configure("Blue.TButton", background=ACCENT_BLUE, foreground="#0c1a2a", padding=6)
    style.map("Blue.TButton", background=[("active", "#7bb2e6")])

    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_MAIN,
                     bordercolor=BORDER_LIGHT, insertcolor=TEXT_MAIN)
    style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=TEXT_MAIN,
                     background=BG_INPUT, arrowcolor=TEXT_MAIN)
    style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)],
              foreground=[("readonly", TEXT_MAIN)])

    style.configure("TCheckbutton", background=BG_CARD, foreground=TEXT_MAIN)
    style.map("TCheckbutton", background=[("active", BG_CARD)])
    style.configure("Panel.TCheckbutton", background=BG_PANEL, foreground=TEXT_MAIN)

    style.configure("Treeview", background=BG_INPUT, fieldbackground=BG_INPUT,
                     foreground=TEXT_MAIN, bordercolor=BORDER, rowheight=22)
    style.configure("Treeview.Heading", background=BG_CARD, foreground=ACCENT_BLUE,
                     font=(font_family, 10, "bold"))
    style.map("Treeview", background=[("selected", ACCENT_BLUE)],
              foreground=[("selected", "#0c1a2a")])

    style.configure("TNotebook", background=BG_MAIN, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=BG_CARD, foreground=TEXT_MAIN, padding=(12, 6))
    style.map("TNotebook.Tab", background=[("selected", ACCENT_BLUE)],
              foreground=[("selected", "#0c1a2a")])

    style.configure("TPanedwindow", background=BG_MAIN)
    style.configure("Vertical.TScrollbar", background=BG_CARD, troughcolor=BG_MAIN,
                     bordercolor=BORDER, arrowcolor=TEXT_MAIN)
    style.configure("Horizontal.TScrollbar", background=BG_CARD, troughcolor=BG_MAIN,
                     bordercolor=BORDER, arrowcolor=TEXT_MAIN)
    style.configure("TSpinbox", fieldbackground=BG_INPUT, foreground=TEXT_MAIN,
                     bordercolor=BORDER_LIGHT, arrowcolor=TEXT_MAIN)

    # menubutton-based dropdown pickers must stay dark on hover/press too
    # (clam's default active state is near-white)
    style.configure("TMenubutton", background=BG_CARD, foreground=TEXT_MAIN,
                     arrowcolor=TEXT_MAIN, bordercolor=BORDER_LIGHT,
                     relief="flat", padding=(8, 4))
    style.map("TMenubutton",
              background=[("pressed", BG_INPUT), ("active", BORDER_LIGHT)],
              foreground=[("pressed", TEXT_MAIN), ("active", TEXT_MAIN)],
              arrowcolor=[("pressed", TEXT_MAIN), ("active", TEXT_MAIN)])
    return font_family


# ---------------------------------------------------------------------------
# AUTOCOMPLETE ENTRY WIDGET
# ---------------------------------------------------------------------------
ENTRY_TEXT_PAD_X = 8  # approx. left inner padding of a clam ttk.Entry (px)


class AutocompleteEntry(ttk.Frame):
    """A ttk.Entry with a filtered dropdown suggestion popup.

    When constructed with a `spell_checker`, it also performs live offline
    spellchecking on its contents: misspelled words get a thin red bar drawn
    directly beneath them, and right-clicking offers correction suggestions
    at the top of the context menu. Checks are debounced so typing stays
    responsive; the checker's dynamic vocabulary keeps known product names
    from ever being flagged."""

    SPELL_DEBOUNCE_MS = 350

    def __init__(self, parent, suggestions_provider, on_change=None,
                 spell_checker=None, **kwargs):
        super().__init__(parent, style="Card.TFrame")
        self.suggestions_provider = suggestions_provider  # callable(text) -> list[str]
        self.on_change = on_change
        self.spell_checker = spell_checker
        self.var = tk.StringVar()
        font_family = pick_font_family()
        kwargs.setdefault("font", (font_family, 10))
        self.entry = ttk.Entry(self, textvariable=self.var, **kwargs)
        self.entry.pack(fill="x")
        # Thin canvas directly under the entry used to draw red bars beneath
        # flagged words (monospace font => exact per-character pixel math).
        self.underline = tk.Canvas(self, height=3, background=BG_CARD,
                                   highlightthickness=0)
        self.underline.pack(fill="x")
        self._flags = []          # [(start, end, word)] currently flagged spans
        self._spell_after = None

        self.popup = None
        self.listbox = None
        self._suppress = False

        self.var.trace_add("write", self._on_var_write)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<Escape>", lambda e: self._hide_popup())
        self.entry.bind("<Down>", self._focus_listbox)
        self.entry.bind("<Return>", self._on_return)
        attach_entry_context_menu(self.entry, extra_items=self._spell_menu_items)

        # keep the underbars aligned when the widget resizes / text scrolls
        self.entry.bind("<Configure>", lambda e: self._draw_underlines())
        self.entry.bind("<ButtonRelease-1>", lambda e: self._draw_underlines())
        self.entry.bind("<KeyRelease-Left>", lambda e: self._draw_underlines())
        self.entry.bind("<KeyRelease-Right>", lambda e: self._draw_underlines())

        # close the suggestion popup when the app window is moved or resized
        # (overrideredirect Toplevels would otherwise float detached)
        self.winfo_toplevel().bind("<Configure>", self._on_window_move, add="+")

    def _on_window_move(self, _event=None):
        # unconditional: withdrawing a hidden popup is a harmless no-op
        self._hide_popup()

    def get(self):
        return self.var.get()

    def set(self, value):
        self._suppress = True
        self.var.set(value or "")
        self._suppress = False
        self._schedule_spell()

    # -- spellcheck ------------------------------------------------------
    def _schedule_spell(self):
        """Debounced spellcheck run."""
        if not self.spell_checker:
            return
        if self._spell_after:
            try:
                self.after_cancel(self._spell_after)
            except Exception:
                pass
            self._spell_after = None
        self._spell_after = self.after(self.SPELL_DEBOUNCE_MS, self._run_spell)

    def _run_spell(self):
        # cancel any queued run first so stale timers can't re-fire later
        if self._spell_after:
            try:
                self.after_cancel(self._spell_after)
            except Exception:
                pass
            self._spell_after = None
        sc = self.spell_checker
        if not sc or not sc.ready:
            return
        try:
            self._flags = sc.check_text(self.var.get())
        except Exception:
            self._flags = []
        self._draw_underlines()

    def _char_metrics(self):
        """(char_width_px, leftmost_visible_index) for the monospace font."""
        import tkinter.font as tkfont
        try:
            f = tkfont.Font(font=self.entry.cget("font"))
            cw = max(1, f.measure("n"))
        except Exception:
            cw = 9
        try:
            left_idx = self.entry.index("@0")
        except Exception:
            left_idx = 0
        return cw, left_idx

    def _draw_underlines(self):
        canvas = self.underline
        canvas.delete("all")
        if not self._flags:
            return
        width = self.entry.winfo_width()
        if width <= 1:
            return
        cw, left_idx = self._char_metrics()
        for start, end, _word in self._flags:
            x1 = ENTRY_TEXT_PAD_X + (start - left_idx) * cw
            x2 = ENTRY_TEXT_PAD_X + (end - left_idx) * cw
            if x2 <= 0 or x1 >= width:
                continue  # scrolled off-screen
            canvas.create_rectangle(max(0, x1), 0, min(width - 1, x2), 2,
                                    fill=ACCENT_RED, width=0)

    def _flag_at_column(self, col):
        """Flagged span containing character column `col` (or None)."""
        for start, end, word in self._flags:
            if start <= col < end:
                return (start, end, word)
        return None

    @staticmethod
    def _match_case(suggestion, original):
        if original.isupper():
            return suggestion.upper()
        if original[:1].isupper():
            return suggestion[:1].upper() + suggestion[1:]
        return suggestion

    def _replace_span(self, start, end, new_text):
        value = self.var.get()
        replaced = value[:start] + new_text + value[end:]
        self.var.set(replaced)
        try:
            self.entry.icursor(start + len(new_text))
            self.entry.focus_set()
        except Exception:
            pass
        self._hide_popup()

    def _spell_menu_items(self, event, menu):
        """Context-menu hook: correction suggestions for the word that was
        right-clicked, inserted above Cut/Copy/Paste."""
        sc = self.spell_checker
        if not sc or not sc.ready or not self._flags:
            return
        cw, left_idx = self._char_metrics()
        col = left_idx + int(round((event.x - ENTRY_TEXT_PAD_X) / float(cw)))
        hit = self._flag_at_column(col)
        if not hit:
            # small slop so clicking just beside a flagged word still resolves
            for start, end, word in self._flags:
                if col < start and (start - col) * cw <= 14:
                    hit = (start, end, word)
                    break
                if col >= end and (col - end) * cw <= 14:
                    hit = (start, end, word)
                    break
        if not hit:
            return
        start, end, word = hit
        suggs = sc.suggest(word, limit=5)
        if not suggs:
            menu.add_command(label="No suggestions for \u201c{}\u201d".format(word),
                             state="disabled")
        else:
            for s in suggs:
                fixed = self._match_case(s, word)
                menu.add_command(
                    label="\u27f2 {}".format(fixed),
                    background=BORDER, foreground="#ffd9a0",
                    command=lambda st=start, en=end, fx=fixed: self._replace_span(st, en, fx))
        menu.add_separator()

    def _on_var_write(self, *_):
        if self._suppress:
            return
        if self.on_change:
            self.on_change(self.var.get())
        self._update_popup()
        self._schedule_spell()

    def _update_popup(self):
        text = self.var.get().strip()
        if not text:
            self._hide_popup()
            return
        items = self.suggestions_provider(text)
        items = [i for i in items if i and i.lower() != text.lower()][:12]
        if not items:
            self._hide_popup()
            return
        self._show_popup(items)

    def _show_popup(self, items):
        if self.popup is None:
            self.popup = tk.Toplevel(self)
            self.popup.wm_overrideredirect(True)
            self.popup.attributes("-topmost", True)
            self.listbox = tk.Listbox(self.popup, background=BG_INPUT, foreground=TEXT_MAIN,
                                       selectbackground=ACCENT_BLUE, selectforeground="#0c1a2a",
                                       highlightthickness=1, highlightbackground=BORDER_LIGHT,
                                       activestyle="none", height=min(8, len(items)))
            self.listbox.pack(fill="both", expand=True)
            self.listbox.bind("<<ListboxSelect>>", self._on_select)
            self.listbox.bind("<Return>", self._on_select)
            self.listbox.bind("<Escape>", lambda e: self._hide_popup())
        self.listbox.delete(0, tk.END)
        for it in items:
            self.listbox.insert(tk.END, it)
        self.listbox.configure(height=min(8, len(items)))
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        w = max(self.entry.winfo_width(), 160)
        self.popup.geometry("{}x{}+{}+{}".format(w, min(8, len(items)) * 20, x, y))
        self.popup.deiconify()

    def _hide_popup(self):
        if self.popup is not None:
            self.popup.withdraw()

    def _focus_listbox(self, _event=None):
        if self.popup and self.popup.winfo_viewable():
            self.listbox.focus_set()
            if self.listbox.size() > 0:
                self.listbox.selection_set(0)
        return "break"

    def _on_select(self, _event=None):
        if not self.listbox.curselection():
            return
        value = self.listbox.get(self.listbox.curselection()[0])
        self.set(value)
        if self.on_change:
            self.on_change(value)
        self._hide_popup()
        self.entry.focus_set()
        self.entry.icursor(tk.END)
        return "break"

    def _on_return(self, _event=None):
        if self.popup and self.popup.winfo_viewable() and self.listbox.size() > 0:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(0)
            self._on_select()
            return "break"

    def _on_focus_out(self, _event=None):
        self.after(150, self._hide_popup)


# ---------------------------------------------------------------------------
# TEXT OVERFLOW HELPERS (ellipsization + delayed hover tooltip)
# ---------------------------------------------------------------------------
def ellipsize(text, max_chars):
    """Truncate `text` to `max_chars` display cells with a trailing ellipsis."""
    if not text:
        return text
    if max_chars is None or len(text) <= max_chars:
        return text
    if max_chars < 2:
        return text[:1]
    return text[:max_chars - 1].rstrip() + "\u2026"


class HoverTooltip:
    """Delayed tooltip that reveals the FULL (untruncated) row text for
    tree/listbox widgets. text_for(x, y) -> str; empty string hides."""

    def __init__(self, widget, text_for):
        self.widget = widget
        self.text_for = text_for
        self._after = None
        self._tw = None
        self._xy = (0, 0)
        widget.bind("<Motion>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel, add="+")
        widget.bind("<ButtonPress>", self._cancel, add="+")

    def _cancel_timer(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _schedule(self, event):
        self._cancel_timer()
        self._xy = (event.x, event.y)
        self._after = self.widget.after(400, self._show)

    def _cancel(self, _event=None):
        self._cancel_timer()
        if self._tw:
            try:
                self._tw.destroy()
            except Exception:
                pass
            self._tw = None

    def _show(self):
        self._after = None
        x, y = self._xy
        try:
            txt = self.text_for(x, y) or ""
        except Exception:
            txt = ""
        if not txt:
            self._cancel()
            return
        self._hide_window()
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tk.Label(tw, text=txt, justify="left", background="#20242f",
                 foreground="#e7e9f0", borderwidth=1, relief="solid",
                 font=(pick_font_family(), 9), wraplength=560
                 ).pack(ipadx=6, ipady=3)
        sx = self.widget.winfo_rootx() + x + 14
        sy = self.widget.winfo_rooty() + y + 22
        tw.wm_geometry("+%d+%d" % (sx, sy))
        tw.lift()
        self._tw = tw


# ---------------------------------------------------------------------------
# DRIVER CONFIG PANEL
# ---------------------------------------------------------------------------
class DriverConfigPanel(ttk.Frame):
    def __init__(self, parent, on_change=None):
        super().__init__(parent, style="Card.TFrame")
        self.on_change = on_change
        self.vars = {}
        self.counts = {}
        self.count_widgets = {}

        ttk.Label(self, text="DRIVER CONFIGURATION", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 4))
        ttk.Label(self, text="Check each driver technology used and enter its count.\n"
                              "The type (DD / Hybrid / Tribrid / etc.) is derived automatically.",
                  style="Card.TLabel", foreground=TEXT_DIM).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        row = 2
        col = 0
        for tech in L.DRIVER_TECH_ORDER:
            frame = ttk.Frame(self, style="Card.TFrame")
            frame.grid(row=row, column=col, sticky="w", padx=8, pady=3)
            var = tk.BooleanVar(value=False)
            self.vars[tech] = var
            icon = ICONS.get(L.DRIVER_TYPE_ICON.get(tech, tech.lower()))
            cb = ttk.Checkbutton(frame, text=L.DRIVER_TECH_LABELS[tech], variable=var, image=icon,
                                  compound="left" if icon else "none",
                                  command=lambda t=tech: self._toggle(t))
            cb.image = icon
            cb.pack(side="left")
            count_var = tk.StringVar(value="1")
            self.counts[tech] = count_var
            spin = ttk.Spinbox(frame, from_=1, to=16, width=4, textvariable=count_var,
                                state="disabled", command=self._recompute)
            spin.pack(side="left", padx=(6, 0))
            count_var.trace_add("write", lambda *a: self._recompute())
            self.count_widgets[tech] = spin
            col += 1
            if col >= 2:
                col = 0
                row += 1
        row += 1

        self.result_label = ttk.Label(self, text="Driver Type: (none)      Config: (none)",
                                       style="Card.TLabel", foreground=ACCENT_GREEN,
                                       font=(pick_font_family(), 10, "bold"))
        self.result_label.grid(row=row, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 8))

    def _toggle(self, tech):
        state = "normal" if self.vars[tech].get() else "disabled"
        self.count_widgets[tech].configure(state=state)
        self._recompute()

    def _recompute(self):
        components = {}
        for tech, var in self.vars.items():
            if var.get():
                try:
                    c = int(self.counts[tech].get())
                    if c < 1:
                        c = 1
                except (TypeError, ValueError):
                    c = 1
                components[tech] = c
        dtype, dconfig = L.classify_driver(components)
        label = "Driver Type: {}      Config: {}".format(dtype or "(unknown/unverified)",
                                                          dconfig or "(none)")
        self.result_label.configure(text=label)
        if self.on_change:
            self.on_change(dtype, dconfig)

    def get(self):
        components = {}
        for tech, var in self.vars.items():
            if var.get():
                try:
                    c = int(self.counts[tech].get())
                except (TypeError, ValueError):
                    c = 1
                components[tech] = max(1, c)
        return L.classify_driver(components)

    def set(self, driver_type, driver_config):
        parsed = L.parse_driver_config(driver_config)
        for tech, var in self.vars.items():
            if tech in parsed:
                var.set(True)
                self.counts[tech].set(str(parsed[tech]))
                self.count_widgets[tech].configure(state="normal")
            else:
                var.set(False)
                self.counts[tech].set("1")
                self.count_widgets[tech].configure(state="disabled")
        self._recompute()

    def clear(self):
        self.set("", "")


# ---------------------------------------------------------------------------
# TAG SELECTOR PANEL
# ---------------------------------------------------------------------------
class TagSelectorPanel(ttk.Frame):
    def __init__(self, parent, on_change=None, fr_provider=None):
        super().__init__(parent, style="Card.TFrame")
        self.on_change = on_change
        self.fr_provider = fr_provider   # callable -> (suggestions, info_text)
        self.vars = {tag: tk.BooleanVar(value=False) for tag in L.APPROVED_TAGS}
        self.current_price = 0
        self._auto_tier_tag = "Budget"
        self._suggestions = []

        ttk.Label(self, text="TAGS  (pick 4–12 total)", style="CardHeader.TLabel").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        self.count_label = ttk.Label(self, text="0 / 12 selected", style="Card.TLabel",
                                      foreground=TEXT_DIM)
        self.count_label.grid(row=0, column=1, sticky="e", padx=8)

        self.suggest_btn = ttk.Button(
            self, text="\u26a1 Suggest from FR Data",
            command=self._run_fr_suggestions,
            style="Blue.TButton")
        if fr_provider is None:
            self.suggest_btn.configure(state="disabled")
        self.suggest_btn.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 4))

        self.emoji_font = pick_emoji_font()

        # groups start below the suggestion button row
        r = 2
        for group_name, tags in L.TAG_GROUPS.items():
            ttk.Label(self, text=group_name, style="Card.TLabel", foreground=ACCENT_BLUE,
                      font=(pick_font_family(), 9, "bold")).grid(
                row=r, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
            r += 1
            if group_name.startswith("Price Tier"):
                self.tier_label = ttk.Label(self, text="Auto: Budget ($0-99)",
                                             style="Card.TLabel", foreground=ACCENT_ORANGE)
                self.tier_label.grid(row=r, column=0, columnspan=2, sticky="w", padx=16)
                r += 1
                continue
            col_frame = ttk.Frame(self, style="Card.TFrame")
            col_frame.grid(row=r, column=0, columnspan=2, sticky="w", padx=12)
            r += 1
            # alphabetical within each group for faster scanning
            for i, tag in enumerate(sorted(tags)):
                cell = ttk.Frame(col_frame, style="Card.TFrame")
                cell.grid(row=i // 3, column=i % 3, sticky="w", padx=4, pady=2)
                e_label = None
                icon = tag_icon(tag)          # colored PNG (preferred)
                if icon is not None:
                    e_label = ttk.Label(cell, image=icon,
                                         background=BG_CARD, cursor="hand2")
                    e_label.image = icon
                else:
                    emoji = TAG_EMOJI.get(tag)
                    if emoji and self.emoji_font:
                        e_label = ttk.Label(cell, text=emoji,
                                             font=(self.emoji_font, 10),
                                             background=BG_CARD, cursor="hand2")
                if e_label is not None:
                    e_label.pack(side="left")
                cb = ttk.Checkbutton(cell, text=tag, variable=self.vars[tag],
                                      command=lambda t=tag: self._on_toggle(t))
                cb.pack(side="left")
                if e_label is not None:
                    # clicking the emoji toggles the checkbox too
                    e_label.bind("<Button-1>", lambda _e, c=cb: c.invoke())

        # FR-analysis suggestion strip (populated by "Suggest from FR Data")
        r += 1
        self.fr_status = ttk.Label(self, text="", style="Card.TLabel",
                                    foreground=TEXT_DIM, wraplength=0,
                                    justify="left")
        self.fr_status.grid(row=r, column=0, columnspan=2, sticky="w", padx=8)
        r += 1
        self.fr_chips = ttk.Frame(self, style="Card.TFrame")
        self.fr_chips.grid(row=r, column=0, columnspan=2, sticky="w", padx=12,
                            pady=(2, 8))

    # -- FR suggestions ---------------------------------------------------
    def _run_fr_suggestions(self):
        if not self.fr_provider:
            return
        self.configure(cursor="watch")
        try:
            suggs, info = self.fr_provider()
        except ValueError as e:
            self.set_suggestions([], str(e))
            return
        except Exception as e:
            self.set_suggestions([], "FR analysis failed: {}".format(e))
            return
        finally:
            self.configure(cursor="")
        self.set_suggestions(suggs, info)

    def set_suggestions(self, suggestions, info_text):
        """Render clickable '+ Tag' chips for suggested tags not already
        selected. Chips respect all picker guardrails when applied."""
        self._suggestions = list(suggestions or [])
        for w in self.fr_chips.winfo_children():
            w.destroy()
        self.fr_status.configure(text=info_text)
        if not self._suggestions:
            return
        selected = self._selected_set()
        shown = 0
        base_text = info_text
        for s in self._suggestions:
            tag = s["tag"] if isinstance(s, dict) else s
            reason = s.get("reason", "") if isinstance(s, dict) else ""
            if tag in selected:
                continue
            icon = tag_icon(tag)
            kwargs = dict(text="+ {}".format(tag), cursor="hand2")
            if icon is not None:
                kwargs["image"] = icon
                kwargs["compound"] = "left"
            chip = ttk.Button(self.fr_chips, **kwargs)
            if icon is not None:
                chip.image = icon

            def _apply(t=tag):
                var = self.vars.get(t)
                if var is None:
                    return
                if not var.get():
                    var.set(True)
                    self._on_toggle(t)   # runs conflict/count guards
                self.set_suggestions(self._suggestions,
                                     self.fr_status.cget("text"))

            def _hover(_e, t=tag, r=reason):
                # keep it to one simple line
                self.fr_status.configure(
                    text="{} {}".format(t, "-- " + r if r else ""))

            def _leave(_e):
                self.fr_status.configure(text=base_text)

            chip.configure(command=_apply)
            chip.pack(side="left", padx=(0, 6), pady=2)
            chip.bind("<Enter>", _hover)
            chip.bind("<Leave>", _leave)
            shown += 1
        if shown == 0 and self._suggestions:
            self.fr_status.configure(
                text="{} -- all suggested tags already applied.".format(info_text))

    def clear_suggestions(self):
        self._suggestions = []
        for w in self.fr_chips.winfo_children():
            w.destroy()
        self.fr_status.configure(text="")

    def _on_toggle(self, tag):
        var = self.vars[tag]
        if var.get():
            # about to be checked -- validate
            selected = self._selected_set()
            selected.add(tag)
            # max count (tier tag added separately, doesn't count toward user cap issue much,
            # but count includes it since it's part of final tags -- reserve 1 slot for it)
            if len(selected) > L.MAX_TAGS - 1:
                var.set(False)
                messagebox.showwarning(
                    APP_TITLE,
                    "You can select at most {} tags (plus the automatic price tier tag).".format(
                        L.MAX_TAGS - 1))
                return
            conflicts = L.tag_conflicts(selected)
            if conflicts:
                var.set(False)
                other = [t for t in conflicts[0] if t != tag]
                messagebox.showwarning(
                    APP_TITLE,
                    "'{}' conflicts with '{}'. Uncheck the other tag first.".format(
                        tag, ", ".join(other) if other else "another selected tag"))
                return
        self._refresh_count()
        if self.on_change:
            self.on_change()

    def _selected_set(self):
        return {t for t, v in self.vars.items() if v.get()}

    def _refresh_count(self):
        n = len(self._selected_set()) + 1  # +1 for automatic price tier tag
        self.count_label.configure(text="{} / {} selected".format(n, L.MAX_TAGS))
        if n < L.MIN_TAGS:
            self.count_label.configure(foreground=ACCENT_RED)
        elif n > L.MAX_TAGS:
            self.count_label.configure(foreground=ACCENT_RED)
        else:
            self.count_label.configure(foreground=ACCENT_GREEN)

    def update_price(self, price_usd):
        self.current_price = price_usd
        tier = L.price_tier_for(price_usd)
        self._auto_tier_tag = tier
        ranges = {"Budget": "$0-99", "Mid-Tier": "$100-499",
                  "Premium": "$500-1499", "Flagship": "$1500+"}
        self.tier_label.configure(text="Auto: {} ({})".format(tier, ranges[tier]))
        self._refresh_count()

    def get_tags(self):
        tags = sorted(self._selected_set())
        tags.append(self._auto_tier_tag)
        return tags

    def set_tags(self, tags):
        tagset = set(tags or [])
        for tag, var in self.vars.items():
            var.set(tag in tagset and tag not in L.PRICE_TIER_TAGS)
        self._refresh_count()

    def clear(self):
        for var in self.vars.values():
            var.set(False)
        self._refresh_count()


# ---------------------------------------------------------------------------
# FILE LINKER PANEL
# ---------------------------------------------------------------------------
class FileLinkerPanel(ttk.Frame):
    def __init__(self, parent, get_data_root):
        super().__init__(parent, style="Card.TFrame")
        self.get_data_root = get_data_root
        self.linked = []
        self._all_files_cache = None
        self._cache_root = None
        # optional callback(n_linked) fired whenever the linked list changes;
        # used by MainApp to badge the Editor tab so auto-link feedback is
        # visible from any notebook tab.
        self.on_files_changed = None

        header = ttk.Frame(self, style="Card.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 4))
        ttk.Label(header, text="MEASUREMENT FILES (.txt)",
                  style="CardHeader.TLabel").pack(side="left")
        self.linked_count_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.linked_count_var,
                  style="Card.TLabel", foreground=ACCENT_GREEN).pack(
            side="left", padx=(10, 0))

        ttk.Label(self, text="Available", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=8)
        ttk.Label(self, text="Linked to this entry", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=8)

        self.search_var = tk.StringVar()
        search = ttk.Entry(self, textvariable=self.search_var)
        search.grid(row=2, column=0, sticky="ew", padx=8)
        attach_entry_context_menu(search)
        self._search_debounce = None
        def _on_search_change(*a):
            if self._search_debounce:
                try:
                    self.after_cancel(self._search_debounce)
                except Exception:
                    pass
            self._search_debounce = self.after(150, self._refresh_available)
        self.search_var.trace_add("write", _on_search_change)

        avail_frame = ttk.Frame(self, style="Card.TFrame")
        avail_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        avail_frame.rowconfigure(0, weight=1)
        avail_frame.columnconfigure(0, weight=1)
        self.available_list = tk.Listbox(avail_frame, background=BG_INPUT, foreground=TEXT_MAIN,
                                          selectbackground=ACCENT_BLUE, selectmode="extended",
                                          height=6, exportselection=False)
        self.available_list.grid(row=0, column=0, sticky="nsew")
        avail_scroll = ttk.Scrollbar(avail_frame, orient="vertical",
                                      command=self.available_list.yview)
        avail_scroll.grid(row=0, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=avail_scroll.set)
        avail_hscroll = ttk.Scrollbar(avail_frame, orient="horizontal",
                                       command=self.available_list.xview)
        avail_hscroll.grid(row=1, column=0, sticky="ew")
        self.available_list.configure(xscrollcommand=avail_hscroll.set)
        attach_touch_scroll(self.available_list)
        # Mouse wheel scrolls the list even without clicking into it first.
        self.available_list.bind("<MouseWheel>", self._on_mousewheel_available)
        self.available_list.bind("<Button-4>", self._on_mousewheel_available)
        self.available_list.bind("<Button-5>", self._on_mousewheel_available)

        btns = ttk.Frame(self, style="Card.TFrame")
        btns.grid(row=3, column=1, sticky="ns")
        ttk.Button(btns, text="Add >>", command=self._add_selected, width=8).pack(pady=4)
        ttk.Button(btns, text="<< Remove", command=self._remove_selected, width=8).pack(pady=4)
        ttk.Button(btns, text="Refresh", command=self._invalidate_cache, width=8).pack(pady=4)

        linked_frame = ttk.Frame(self, style="Card.TFrame")
        linked_frame.grid(row=3, column=2, sticky="nsew", padx=8, pady=4)
        linked_frame.rowconfigure(0, weight=1)
        linked_frame.columnconfigure(0, weight=1)
        self.linked_list = tk.Listbox(linked_frame, background=BG_INPUT, foreground=TEXT_MAIN,
                                       selectbackground=ACCENT_BLUE, selectmode="extended",
                                       height=6, exportselection=False)
        self.linked_list.grid(row=0, column=0, sticky="nsew")
        linked_scroll = ttk.Scrollbar(linked_frame, orient="vertical",
                                       command=self.linked_list.yview)
        linked_scroll.grid(row=0, column=1, sticky="ns")
        self.linked_list.configure(yscrollcommand=linked_scroll.set)
        linked_hscroll = ttk.Scrollbar(linked_frame, orient="horizontal",
                                        command=self.linked_list.xview)
        linked_hscroll.grid(row=1, column=0, sticky="ew")
        self.linked_list.configure(xscrollcommand=linked_hscroll.set)
        attach_touch_scroll(self.linked_list)
        self.linked_list.bind("<MouseWheel>", self._on_mousewheel_linked)
        self.linked_list.bind("<Button-4>", self._on_mousewheel_linked)
        self.linked_list.bind("<Button-5>", self._on_mousewheel_linked)

        # ellipsization state: full strings kept for tooltips / re-widen
        self._available_full = []
        self._linked_full = []
        self._avail_cap = None
        self._linked_cap = None
        self._cap_after = None
        for lb in (self.available_list, self.linked_list):
            lb.bind("<Configure>", self._schedule_caps, add="+")
        HoverTooltip(self.available_list,
                     lambda x, y: self._full_text_at(
                         self.available_list, self._available_full, y))
        HoverTooltip(self.linked_list,
                     lambda x, y: self._full_text_at(
                         self.linked_list, self._linked_full, y))

        # data-folder watcher state + lazy start
        self._last_walk = 0.0
        self._walk_thread = None
        self._poll_after = None
        self.after(1500, self._auto_poll)

        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(2, weight=1)

        self.hint = ttk.Label(self, text="", style="Card.TLabel", foreground=TEXT_DIM,
                               wraplength=380)
        self.hint.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

    def _invalidate_cache(self):
        self._all_files_cache = None
        self._cache_root = None
        self._refresh_available()

    @staticmethod
    def _scroll_amount(event):
        # Windows/macOS send <MouseWheel> with event.delta (+/-120 per notch);
        # X11/Linux send <Button-4> (up) / <Button-5> (down) instead.
        if getattr(event, "num", None) == 4:
            return -1
        if getattr(event, "num", None) == 5:
            return 1
        delta = getattr(event, "delta", 0)
        return -1 if delta > 0 else 1

    def _on_mousewheel_available(self, event):
        self.available_list.yview_scroll(self._scroll_amount(event), "units")
        return "break"

    def _on_mousewheel_linked(self, event):
        self.linked_list.yview_scroll(self._scroll_amount(event), "units")
        return "break"

    @staticmethod
    def _scan_data_dir(root):
        """Pure data-folder scan (no UI): returns (sorted_rel_paths,
        data_dir) where paths are forward-slash relative like database.json
        stores them. Returns ([], None) when the root has no 'data'
        subfolder. Safe to call from a worker thread."""
        if os.path.basename(os.path.normpath(root)).lower() == "data" and os.path.isdir(root):
            # selected the data folder itself -> rel against its parent so
            # stored paths stay 'data/...'
            data_dir = root
            base_root = os.path.dirname(root)
            results = []
            for r, _, files in os.walk(data_dir):
                for fn in files:
                    if fn.lower().endswith(".txt"):
                        full = os.path.join(r, fn)
                        try:
                            rel = os.path.relpath(full, base_root).replace("\\", "/")
                        except ValueError:
                            rel = os.path.join("data", os.path.relpath(full, data_dir)).replace("\\", "/")
                        results.append(rel)
            results.sort()
            return results, data_dir
        data_dir = os.path.join(root, "data")
        results = []
        if os.path.isdir(data_dir):
            for r, _, files in os.walk(data_dir):
                for fn in files:
                    if fn.lower().endswith(".txt"):
                        full = os.path.join(r, fn)
                        try:
                            rel = os.path.relpath(full, root).replace("\\", "/")
                        except ValueError:
                            continue
                        results.append(rel)
            results.sort()
            return results, data_dir
        return [], None

    def _all_files(self):
        root = self.get_data_root()
        if not root:
            self.hint.configure(text="No data folder set. Use File > Set Data Folder... to browse for .txt measurement files.")
            return []
        if self._all_files_cache is not None and self._cache_root == root:
            return self._all_files_cache
        results, data_dir = self._scan_data_dir(root)
        if data_dir is None:
            # hint if data subfolder missing
            self.hint.configure(text="No 'data' subfolder found under {}. Use File > Set Data Folder...".format(root))
            self._all_files_cache = []
            self._cache_root = root
            return []
        self._all_files_cache = results
        self._cache_root = root
        if os.path.basename(os.path.normpath(root)).lower() == "data":
            self.hint.configure(text="{} .txt files found under {} (selected data folder directly)".format(len(results), data_dir))
        else:
            self.hint.configure(text="{} .txt files found under {}".format(len(results), data_dir))
        return results

    def _refresh_available(self):
        query = self.search_var.get().strip().lower()
        self.available_list.delete(0, tk.END)
        linked_set = set(self.linked)
        all_files = self._all_files()
        kept = [rel for rel in all_files
                if rel not in linked_set
                and (not query or query in rel.lower())]
        self._available_full = kept
        self._render_truncated(self.available_list, kept, self._avail_cap)

    def _refresh_linked(self):
        self._linked_full = list(self.linked)
        self._render_truncated(self.linked_list, self._linked_full,
                               self._linked_cap)
        n = len(self.linked)
        self.linked_count_var.set(
            "{} file{} linked".format(n, "" if n == 1 else "s") if n else "")
        if self.on_files_changed:
            try:
                self.on_files_changed(n)
            except Exception:
                pass

    # -- overflow handling for the measurement lists -----------------------
    @staticmethod
    def _char_w(widget):
        import tkinter.font as tkfont
        try:
            fnt = tkfont.Font(font=widget.cget("font"))
            return max(4, fnt.measure("n"))
        except Exception:
            return 7

    def _schedule_caps(self, _event=None):
        if self._cap_after:
            try:
                self.after_cancel(self._cap_after)
            except Exception:
                pass
        self._cap_after = self.after(120, self._recompute_caps)

    def _recompute_caps(self):
        """Recompute per-list character budgets from the current pixel width
        and re-render with ellipsized strings (full text stays available via
        hover tooltip and the horizontal scrollbars)."""
        self._cap_after = None
        for lb, attr in ((self.available_list, "_avail_cap"),
                         (self.linked_list, "_linked_cap")):
            wpx = lb.winfo_width()
            setattr(self, attr,
                    max(4, int((wpx - 26) // self._char_w(lb))) if wpx > 40
                    else None)
        self._refresh_available()
        self._refresh_linked()

    def _render_truncated(self, lb, items, cap):
        y0 = lb.yview()[0]                     # keep scroll position stable
        sel = set(lb.curselection())
        lb.delete(0, tk.END)
        for n, it in enumerate(items):
            lb.insert(tk.END, ellipsize(it, cap) if cap else it)
        try:
            lb.yview_moveto(y0)
            for n in sorted(sel & set(range(len(items)))):
                lb.selection_set(n)
        except Exception:
            pass

    def _full_text_at(self, lb, full_items, y):
        idx = lb.nearest(y)
        if 0 <= idx < len(full_items):
            return full_items[idx]
        return ""

    def _add_selected(self):
        # Resolve from _available_full (the untruncated strings): the listbox
        # shows ellipsized text when paths overflow the widget width, and
        # storing that display text would corrupt entry["files"] with paths
        # that do not exist on disk. Indexes are positionally aligned.
        for i in self.available_list.curselection():
            if 0 <= i < len(self._available_full):
                rel = self._available_full[i]
            else:                       # defensive fallback (should not happen)
                rel = self.available_list.get(i)
            if rel not in self.linked:
                self.linked.append(rel)
        self._refresh_linked()
        self._refresh_available()

    def _remove_selected(self):
        for i in reversed(self.linked_list.curselection()):
            del self.linked[i]
        self._refresh_linked()
        self._refresh_available()

    def get_files(self):
        return list(self.linked)

    def set_files(self, files):
        self.linked = list(files or [])
        self._refresh_linked()
        self._refresh_available()

    def clear(self):
        self.set_files([])

    def refresh_root_changed(self):
        self._invalidate_cache()

    # -- data-folder watching (dependency-free polling) --------------------
    POLL_INTERVAL_MS = 8000        # while the panel is on screen
    MIN_REWALK_SECS = 4            # never rescan more often than this

    def poll_now(self, force=False):
        """Rescan the data folder if it may have changed. Cheap: a full walk
        of ~11k files takes ~0.2 s, and we throttle to MIN_REWALK_SECS."""
        root = self.get_data_root()
        if not root:
            return
        now = time.time()
        if not force and (now - self._last_walk) < self.MIN_REWALK_SECS:
            return
        self._last_walk = now
        # One walk at a time; the scan itself runs in a daemon thread so an
        # 11k-file data folder never blocks the UI thread (~0.2 s per walk).
        if getattr(self, "_walk_thread", None) is not None \
                and self._walk_thread.is_alive():
            return
        prev = tuple(self._all_files_cache or ())
        self._all_files_cache = None
        self._cache_root = None

        result = {}

        def _compute():
            try:
                result["cur"] = self._scan_data_dir(root)
            except Exception:  # noqa: BLE001 - treat as "no change"
                result["cur"] = ([], root)

        th = threading.Thread(target=_compute, daemon=True, name="file-walk")

        def _apply():
            res, _data_dir = result.get("cur", ([], None))
            if self.get_data_root() != root:
                return                      # user switched folder mid-walk
            cur = tuple(res or [])
            self._all_files_cache = list(cur)
            self._cache_root = root
            if cur != prev:
                self._refresh_available()
                try:
                    self.hint.configure(
                        text="Data folder updated - {} measurement file(s) found.".format(len(cur)))
                except Exception:  # noqa: BLE001 - widget may be gone
                    pass

        def poll():
            if th.is_alive():
                try:
                    self.after(60, poll)
                except Exception:  # noqa: BLE001 - shutting down
                    pass
                return
            try:
                _apply()
            except Exception:  # noqa: BLE001
                pass

        self._walk_thread = th
        th.start()
        try:
            self.after(60, poll)
        except Exception:  # noqa: BLE001
            pass

    def _auto_poll(self):
        try:
            if self.winfo_ismapped():
                self.poll_now()
        except Exception:
            pass
        self._poll_after = self.after(self.POLL_INTERVAL_MS, self._auto_poll)


# ---------------------------------------------------------------------------
# ICON DROPDOWN (readonly combobox replacement with images)
# ---------------------------------------------------------------------------
class IconCombobox(ttk.Frame):
    """A readonly dropdown that shows an icon next to every option.
    ttk.Combobox cannot render images, so this wraps a ttk.Menubutton whose
    menu entries use image+compound (tk menus support that natively).
    Behaves like the combobox it replaces: shares a StringVar, supports
    dynamic value lists and lock/disable."""

    def __init__(self, parent, values, icon_for, textvariable,
                 on_change=None, width=22, **kw):
        super().__init__(parent, style="Card.TFrame")
        self.icon_for = icon_for            # callable(value) -> PhotoImage|None
        self.textvariable = textvariable
        self.on_change = on_change
        self.values = []
        self._locked = False
        self.button = ttk.Menubutton(self, textvariable=textvariable,
                                      direction="flush", width=width)
        self.button.pack(fill="x")
        self.menu = tk.Menu(self.button, tearoff=0,
                            background=BG_CARD, foreground=TEXT_MAIN,
                            activebackground=BORDER_LIGHT,
                            activeforeground=TEXT_MAIN,
                            font=(pick_font_family(), 10))
        self.button.configure(menu=self.menu)
        self.set_values(values)

    def set_values(self, values):
        """Rebuild the dropdown list (keeps current selection if still valid)."""
        self.values = list(values)
        self.menu.delete(0, "end")
        for v in self.values:
            icon = self.icon_for(v)
            kwargs = dict(label=v, compound="left",
                          command=lambda vv=v: self._choose(vv))
            if icon is not None:
                kwargs["image"] = icon
            self.menu.add_command(**kwargs)
        if self.textvariable.get() not in self.values:
            self.textvariable.set(self.values[0] if self.values else "")

    def _choose(self, value):
        if self._locked:
            return
        if value != self.textvariable.get():
            self.textvariable.set(value)
            if self.on_change:
                self.on_change()

    def get(self):
        return self.textvariable.get()

    def set(self, value):
        if value in self.values:
            self.textvariable.set(value)

    def set_locked(self, locked):
        """Locked = selection visible but changing it is disallowed."""
        self._locked = locked
        state = "disabled" if locked else "normal"
        self.button.configure(state=state)

    def is_locked(self):
        return self._locked


# ---------------------------------------------------------------------------
# ENTRY EDITOR
# ---------------------------------------------------------------------------
class EntryEditor(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.original_id = None  # id of the entry currently loaded, for update-in-place
        self._build()

    def _build(self):
        font_family = pick_font_family()
        canvas = tk.Canvas(self, background=BG_MAIN, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner = ttk.Frame(canvas, style="TFrame")
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure("inner", width=canvas.winfo_width())
        inner.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", on_configure)

        def _wheel(event):
            # Only scroll editor canvas when event is on canvas
            # Use canvas binding instead of bind_all to avoid double-scroll
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass
        canvas.bind("<MouseWheel>", _wheel)
        # Linux scroll
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        pad = dict(padx=10, pady=4)

        # ---- identity row ----
        card = ttk.Frame(inner, style="Card.TFrame")
        card.pack(fill="x", padx=10, pady=8)
        ttk.Label(card, text="IDENTITY", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(8, 4))

        ttk.Label(card, text="Brand*", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=8)
        self.brand_entry = AutocompleteEntry(card, self.app.brand_suggestions,
                                              on_change=self._on_identity_change,
                                              spell_checker=self.app.speller)
        self.brand_entry.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))

        ttk.Label(card, text="Model*", style="Card.TLabel").grid(row=1, column=1, sticky="w", padx=8)
        self.model_entry = AutocompleteEntry(card, self.app.model_suggestions,
                                              on_change=self._on_identity_change,
                                              spell_checker=self.app.speller)
        self.model_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 6))

        ttk.Label(card, text="Variant", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=8)
        self.variant_entry = AutocompleteEntry(card, self.app.variant_suggestions,
                                                on_change=self._on_identity_change,
                                                spell_checker=self.app.speller)
        self.variant_entry.grid(row=2, column=2, sticky="ew", padx=8, pady=(0, 6))

        for c in range(3):
            card.columnconfigure(c, weight=1)

        ttk.Label(card, text="Auto-generated ID:", style="Card.TLabel", foreground=TEXT_DIM).grid(
            row=3, column=0, sticky="w", padx=8)
        self.id_var = tk.StringVar(value="")
        id_label = ttk.Label(card, textvariable=self.id_var, style="Card.TLabel",
                              foreground=ACCENT_GREEN, font=(font_family, 10, "bold"))
        id_label.grid(row=3, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        # ---- specs card (responsive: numeric fields on one row, the two
        # wide dropdowns on their own row so they never clip in half-screen
        # snapped windows) ----
        specs = ttk.Frame(inner, style="Card.TFrame")
        specs.pack(fill="x", padx=10, pady=8)
        ttk.Label(specs, text="SPECIFICATIONS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(8, 4))
        for col in range(6):
            specs.columnconfigure(col, weight=1, uniform="speccol")

        ttk.Label(specs, text="Year (0 = unknown)", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=8)
        self.year_var = tk.StringVar(value="0")
        year_entry = ttk.Entry(specs, textvariable=self.year_var, width=8)
        year_entry.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        year_entry.bind("<FocusOut>", self._validate_year)
        attach_entry_context_menu(year_entry)

        ttk.Label(specs, text="Price (USD)", style="Card.TLabel").grid(row=1, column=1, sticky="w", padx=8)
        self.price_var = tk.StringVar(value="0")
        price_entry = ttk.Entry(specs, textvariable=self.price_var, width=8)
        price_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 6))
        price_entry.bind("<FocusOut>", self._validate_price)
        attach_entry_context_menu(price_entry)

        ttk.Label(specs, text="Impedance (Ω)", style="Card.TLabel").grid(row=1, column=2, sticky="w", padx=8)
        self.impedance_var = tk.StringVar(value="0")
        self.impedance_entry = ttk.Entry(specs, textvariable=self.impedance_var, width=8)
        self.impedance_entry.grid(row=2, column=2, sticky="ew", padx=8, pady=(0, 6))
        attach_entry_context_menu(self.impedance_entry)

        ttk.Label(specs, text="Sensitivity (dB/mW)", style="Card.TLabel").grid(row=1, column=3, sticky="w", padx=8)
        self.sensitivity_var = tk.StringVar(value="0")
        self.sensitivity_entry = ttk.Entry(specs, textvariable=self.sensitivity_var, width=8)
        self.sensitivity_entry.grid(row=2, column=3, sticky="ew", padx=8, pady=(0, 6))
        attach_entry_context_menu(self.sensitivity_entry)

        self.year_hint = ttk.Label(specs, text="", style="Card.TLabel", foreground=ACCENT_RED)
        self.year_hint.grid(row=3, column=0, sticky="w", padx=8)
        self.price_hint = ttk.Label(specs, text="", style="Card.TLabel", foreground=ACCENT_ORANGE)
        self.price_hint.grid(row=3, column=1, sticky="w", padx=8)

        ttk.Label(specs, text="Form Factor", style="Card.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))
        self.form_var = tk.StringVar(value=L.FORM_FACTORS[0])
        self.form_picker = IconCombobox(
            specs, L.FORM_FACTORS,
            lambda v: ICONS.get(L.FORM_FACTOR_ICON.get(v, "")),
            # IconCombobox invokes on_change() with no arguments, so bind the
            # user-initiated flag here: picking TWS from the dropdown must
            # zero/lock the spec fields (see _on_form_change).
            self.form_var,
            on_change=lambda: self._on_form_change(user_initiated=True),
            width=16)
        self.form_picker.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))

        ttk.Label(specs, text="Connector", style="Card.TLabel").grid(
            row=4, column=2, columnspan=2, sticky="w", padx=8, pady=(4, 0))
        self.connector_var = tk.StringVar(value="")
        self.connector_picker = IconCombobox(
            specs, L.FORM_CONNECTOR_MAP[L.FORM_FACTORS[0]],
            lambda v: ICONS.get(L.CONNECTOR_ICON.get(v, "")),
            self.connector_var, width=12)
        self.connector_picker.grid(row=5, column=2, columnspan=2, sticky="ew", padx=8, pady=(0, 6))

        self.spec_hint = ttk.Label(specs, text="", style="Card.TLabel",
                                    foreground=ACCENT_ORANGE, wraplength=260,
                                    justify="left")
        self.spec_hint.grid(row=5, column=4, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        # ---- driver config ----
        self.driver_panel = DriverConfigPanel(inner)
        self.driver_panel.pack(fill="x", padx=10, pady=8)

        # ---- tags ----
        self.tag_panel = TagSelectorPanel(inner, fr_provider=self._fr_suggestions)
        self.tag_panel.pack(fill="x", padx=10, pady=8)

        # ---- files ----
        self.file_panel = FileLinkerPanel(inner, self.app.get_data_root)
        self.file_panel.pack(fill="x", padx=10, pady=8)

        # ---- action buttons ----
        actions = ttk.Frame(inner, style="TFrame")
        actions.pack(fill="x", padx=10, pady=(4, 20))
        ttk.Button(actions, text="Save Entry", style="Accent.TButton",
                   command=self._on_save).pack(side="left", padx=4)
        ttk.Button(actions, text="Clear / New", command=self.new_entry).pack(side="left", padx=4)
        self.validation_label = ttk.Label(actions, text="", style="TLabel", foreground=ACCENT_RED,
                                           wraplength=600)
        self.validation_label.pack(side="left", padx=12)

        # touch-style drag panning for the whole editor form (passive areas)
        attach_touch_scroll_canvas(canvas, inner)

        self._on_form_change()

    # -- helpers -------------------------------------------------------
    def _on_identity_change(self, _value=None):
        brand = self.brand_entry.get()
        model = self.model_entry.get()
        variant = self.variant_entry.get()
        self.id_var.set(L.build_id(brand, model, variant) or "(fill in brand & model)")

    def _on_form_change(self, _event=None, user_initiated=False):
        ff = self.form_var.get()
        allowed = L.FORM_CONNECTOR_MAP.get(ff, L.CONNECTORS_ALL)
        self.connector_picker.set_values(allowed)
        if len(allowed) == 1:
            self.connector_var.set(allowed[0])
            self.connector_picker.set_locked(True)
        else:
            self.connector_picker.set_locked(False)
            if self.connector_var.get() not in allowed:
                self.connector_var.set("")

        if ff == L.TWS_FORM_FACTOR:
            if user_initiated:
                if getattr(self, "_pre_tws_specs", None) is None:
                    self._pre_tws_specs = (self.impedance_var.get(),
                                           self.sensitivity_var.get())
                self.impedance_var.set("0")
                self.sensitivity_var.set("0")
                self.impedance_entry.configure(state="disabled")
                self.sensitivity_entry.configure(state="disabled")
                self.spec_hint.configure(
                    text="Locked to 0: TWS earbuds have no wired out path.")
            else:
                self.impedance_entry.configure(state="normal")
                self.sensitivity_entry.configure(state="normal")
                self.spec_hint.configure(text="")
        else:
            pre = getattr(self, "_pre_tws_specs", None)
            if pre is not None:
                self.impedance_var.set(pre[0])
                self.sensitivity_var.set(pre[1])
                self._pre_tws_specs = None
            self.impedance_entry.configure(state="normal")
            self.sensitivity_entry.configure(state="normal")
            self.spec_hint.configure(text="")

    def _validate_year(self, _event=None):
        raw = self.year_var.get().strip()
        if raw == "":
            raw = "0"
            self.year_var.set(raw)
        y = _int_input(raw, None)
        if y is None or not L.is_valid_year(y):
            self.year_hint.configure(text="Enter a valid 4-digit year (1950-{}) or 0 if unknown."
                                          .format(L.CURRENT_YEAR + 1))
        else:
            self.year_hint.configure(text="")

    def _validate_price(self, _event=None):
        raw = self.price_var.get().strip()
        if raw == "":
            raw = "0"
        p = _int_input(raw, None)
        if p is None:
            self.price_hint.configure(text="Price must be a whole number.")
            return
        if p < 0:
            # keep the last valid tier visible; the field itself stays as-is
            # so Save Entry's validation reports it instead of silently
            # treating the value as 0
            self.price_hint.configure(text="Price cannot be negative.")
            return
        rounded = L.round_price_to_5(p)
        if rounded != p:
            self.price_var.set(str(rounded))
            self.price_hint.configure(text="Rounded to nearest $5: ${}".format(rounded))
        else:
            self.price_hint.configure(text="")
        self.tag_panel.update_price(rounded)

    # -- FR tag suggestions -------------------------------------------------
    def _fr_suggestions(self):
        import fr_analysis as FA
        data_root = self.app.get_data_root()
        rel_files = self.file_panel.get_files()
        if not rel_files:
            raise ValueError("No measurement files linked to this entry yet.")
        if not data_root:
            raise ValueError("Set the data folder first (File > Set Data Folder).")

        votes = {}
        metric_sums = {}
        ok_count = err_count = 0
        errors = []
        for i, rel in enumerate(rel_files):
            try:
                full = FA.resolve_under_root(data_root, rel)
                pts = FA.parse_fr_file(full)
                res = FA.analyze_points(pts)
            except OSError:
                res = {"ok": False, "error": "file not found"}
            except ValueError as e:
                res = {"ok": False, "error": str(e)}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            if not res.get("ok"):
                err_count += 1
                errors.append("{} ({})".format(rel, res.get("error", "unreadable")))
                continue
            ok_count += 1
            for rank, s in enumerate(res["suggestions"]):
                tag = s["tag"]
                if tag in L.APPROVED_TAGS:
                    votes.setdefault(tag, [0, 100])
                    votes[tag][0] += 1
                    votes[tag][1] = min(votes[tag][1], rank)
            for k, v in res.get("metrics", {}).items():
                metric_sums.setdefault(k, []).append(v)

        if ok_count == 0:
            msg = "Could not analyze any linked file."
            if errors:
                msg += "\n" + "\n".join(errors[:4])
            raise ValueError(msg)

        need = ok_count // 2 + 1
        ordered = sorted(votes.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
        suggs = [{"tag": t, "reason": "voted by {}/{} file(s)".format(n, ok_count)}
                 for t, (n, _r) in ordered if n >= need]

        avg = {k: round(sum(vals) / len(vals), 1) for k, vals in metric_sums.items()}
        info = "FR vs 1 kHz:  {}   \u00b7   {} file(s) used".format(
            FA.summarize_metrics(avg), ok_count)
        if err_count:
            info += "   \u00b7   {} unreadable".format(err_count)
        return suggs, info

    # -- public API ------------------------------------------------------
    def new_entry(self):
        self.original_id = None
        self.brand_entry.set("")
        self.model_entry.set("")
        self.variant_entry.set("")
        self.id_var.set("(fill in brand & model)")
        self.year_var.set("0")
        self.price_var.set("0")
        self.impedance_var.set("0")
        self.sensitivity_var.set("0")
        self.form_var.set(L.FORM_FACTORS[0])
        self._pre_tws_specs = None
        self._on_form_change()
        self.driver_panel.clear()
        self.tag_panel.clear()
        self.tag_panel.update_price(0)
        self.file_panel.clear()
        self.tag_panel.clear_suggestions()
        self.validation_label.configure(text="")
        self.year_hint.configure(text="")
        self.price_hint.configure(text="")
        self._baseline_is_new = True
        self._capture_baseline()

    def load_entry(self, entry):
        self.original_id = entry.get("id")
        self.brand_entry.set(entry.get("brand", ""))
        self.model_entry.set(entry.get("model", ""))
        self.variant_entry.set(entry.get("variant", ""))
        self._on_identity_change()
        self.year_var.set(str(entry.get("year", 0)))
        self.price_var.set(str(entry.get("price_usd", 0)))
        self.impedance_var.set(str(entry.get("impedance", 0)))
        self.sensitivity_var.set(str(entry.get("sensitivity", 0)))
        ff = entry.get("form_factor") or L.FORM_FACTORS[0]
        self._pre_tws_specs = None
        self.form_var.set(ff)
        # user_initiated=False: a stored TWS entry keeps its real specs visible
        # and editable (the prompts allow them); the zero/lock only applies to
        # entries the user converts to TWS during this session (see below).
        self._on_form_change(user_initiated=False)
        self.connector_var.set(entry.get("connector", ""))
        self.driver_panel.set(entry.get("driver_type", ""), entry.get("driver_config", ""))
        self.tag_panel.set_tags(entry.get("tags", []))
        self.tag_panel.update_price(entry.get("price_usd", 0))
        self.file_panel.set_files(entry.get("files", []))
        self.tag_panel.clear_suggestions()
        self.validation_label.configure(text="")
        self.year_hint.configure(text="")
        self.price_hint.configure(text="")
        self._baseline_is_new = False
        self._capture_baseline()

        if L.ENFORCE_TWS_ZERO_SPECS and entry.get("form_factor") == L.TWS_FORM_FACTOR:
            try:
                bad = [f for f, v in (("Impedance", entry.get("impedance", 0)),
                                      ("Sensitivity", entry.get("sensitivity", 0)))
                       if int(float(v or 0)) != 0]
            except (TypeError, ValueError):
                bad = ["Impedance/Sensitivity"]
            if bad:
                messagebox.showwarning(
                    APP_TITLE,
                    "{}: this TWS entry has {} set to a non-zero value.".format(
                        entry.get("id"), " and ".join(bad)))

    def build_entry_dict(self):
        year = _int_input(self.year_var.get(), -1)
        price = _int_input(self.price_var.get(), -1)
        impedance = _int_input(self.impedance_var.get(), -1)
        sensitivity = _int_input(self.sensitivity_var.get(), -1)
        dtype, dconfig = self.driver_panel.get()
        brand = self.brand_entry.get().strip()
        model = self.model_entry.get().strip()
        variant = self.variant_entry.get().strip()
        return {
            "id": L.build_id(brand, model, variant),
            "brand": brand,
            "model": model,
            "variant": variant,
            "year": year,
            "price_usd": price,
            "driver_type": dtype,
            "driver_config": dconfig,
            "impedance": impedance,
            "sensitivity": sensitivity,
            "connector": self.connector_var.get(),
            "form_factor": self.form_var.get(),
            "tags": self.tag_panel.get_tags(),
            "files": self.file_panel.get_files(),
        }

    def _on_save(self):
        self._validate_year()
        self._validate_price()
        entry = self.build_entry_dict()
        errors = self.app.validate_and_commit(entry, self.original_id)
        if errors:
            self.validation_label.configure(text="Cannot save:\n- " + "\n- ".join(errors))
        else:
            self.validation_label.configure(text="")
            self.original_id = entry["id"]

    def form_is_dirty(self):
        base = getattr(self, "_baseline", None)
        if base is None:
            return False
        try:
            dtype, dconfig = self.driver_panel.get()
        except Exception:
            return False
        current = {
            "brand": self.brand_entry.get(),
            "model": self.model_entry.get(),
            "variant": self.variant_entry.get(),
            "year": self.year_var.get(),
            "price": self.price_var.get(),
            "impedance": self.impedance_var.get(),
            "sensitivity": self.sensitivity_var.get(),
            "form": self.form_var.get(),
            "connector": self.connector_var.get(),
            "driver": (dtype, dconfig),
            "tags": set(self.tag_panel.get_tags()),
            "files": list(self.file_panel.get_files()),
        }
        if current["files"] != base["files"]:
            return True
        current["files"] = base["files"] = None
        return current != base

    def _capture_baseline(self):
        try:
            dtype, dconfig = self.driver_panel.get()
            self._baseline = {
                "brand": self.brand_entry.get(),
                "model": self.model_entry.get(),
                "variant": self.variant_entry.get(),
                "year": self.year_var.get(),
                "price": self.price_var.get(),
                "impedance": self.impedance_var.get(),
                "sensitivity": self.sensitivity_var.get(),
                "form": self.form_var.get(),
                "connector": self.connector_var.get(),
                "driver": (dtype, dconfig),
                "tags": set(self.tag_panel.get_tags()),
                "files": list(self.file_panel.get_files()),
            }
        except Exception:
            self._baseline = None


# ---------------------------------------------------------------------------
# AUDIT PANEL
# ---------------------------------------------------------------------------
class AuditPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.issues = []
        self.group_by_entry = tk.BooleanVar(value=True)
        self._row_issues = {}      # leaf iid  -> AuditIssue
        self._group_items = {}     # group iid -> [AuditIssue, ...]

        top = ttk.Frame(self, style="TFrame")
        top.pack(fill="x", padx=10, pady=8)
        ttk.Button(top, text="Run Full Audit", style="Accent.TButton",
                   command=self.app.run_audit).pack(side="left", padx=4)
        ttk.Button(top, text="Fix Selected", command=self._fix_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Fix All Auto-Fixable", style="Blue.TButton",
                   command=self._fix_all).pack(side="left", padx=4)
        ttk.Button(top, text="Go to Entry", command=self._goto_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Export Report...", command=self._export).pack(side="left", padx=4)
        ttk.Checkbutton(top, text="Group by entry", variable=self.group_by_entry,
                        command=self.rerender).pack(side="left", padx=(14, 4))
        self.summary_label = ttk.Label(top, text="No audit run yet.", style="TLabel")
        self.summary_label.pack(side="left", padx=16)

        columns = ("category", "entry", "message", "fixable")
        self._base_headings = {"category": "Category", "entry": "Entry",
                               "message": "Issue", "fixable": "Auto-fixable"}
        self._sort_col = None
        self._sort_desc = False
        tree_frame = ttk.Frame(self, style="TFrame")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            self.tree.heading(col, text=self._base_headings[col],
                              command=lambda c=col: self._sort_by(c))
        def _col_width(col):
            return {"category": 140, "entry": 180,
                    "message": 520, "fixable": 90}[col]
        for col in columns:
            self.tree.column(col, width=_col_width(col),
                             anchor="center" if col == "fixable" else "w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        attach_touch_scroll(self.tree)
        # Mouse wheel scrolls the audit list even without clicking into it first.
        self.tree.bind("<MouseWheel>", self._on_mousewheel)
        self.tree.bind("<Button-4>", self._on_mousewheel)
        self.tree.bind("<Button-5>", self._on_mousewheel)

        self.tree.tag_configure("error", foreground=ACCENT_RED)
        self.tree.tag_configure("warning", foreground=ACCENT_ORANGE)
        self.tree.tag_configure("info", foreground=TEXT_DIM)
        self.tree.bind("<Double-1>", self._on_issue_activate)
        self.tree.bind("<Return>", self._on_issue_activate)

    def _on_mousewheel(self, event):
        # Windows/macOS send <MouseWheel> with event.delta (+/-120 per notch);
        # X11/Linux send <Button-4> (up) / <Button-5> (down) instead.
        if getattr(event, "num", None) == 4:
            amount = -1
        elif getattr(event, "num", None) == 5:
            amount = 1
        else:
            amount = -1 if getattr(event, "delta", 0) > 0 else 1
        self.tree.yview_scroll(amount, "units")
        return "break"

    # ------------------------------------------------------------------
    # rendering (flat OR grouped-by-entry, optionally sorted)
    # ------------------------------------------------------------------
    _SEV_RANK = {"error": 0, "warning": 1, "info": 2}

    def _sort_by(self, col):
        """Header click: ascending first, click again for descending."""
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        for c, base in self._base_headings.items():
            arrow = ""
            if c == self._sort_col:
                arrow = "  \u25b4" if not self._sort_desc else "  \u25be"
            self.tree.heading(c, text=base + arrow)
        self.rerender()

    def _sorted_issues(self):
        iss = list(self.issues)
        if not self._sort_col:
            return iss
        col = self._sort_col
        def key(i):
            if col == "fixable":
                return (1 if i.fix else 0,)
            v = getattr(i, "entry_id" if col == "entry" else col, "")
            return (str(v).lower(),)
        return sorted(iss, key=key, reverse=self._sort_desc)

    def show_issues(self, issues):
        self.issues = list(issues)
        self.rerender()
        errors = sum(1 for i in issues if i.severity == "error")
        warnings = sum(1 for i in issues if i.severity == "warning")
        infos = sum(1 for i in issues if i.severity == "info")
        self.summary_label.configure(
            text="{} issues found  ({} errors, {} warnings, {} info)".format(
                len(issues), errors, warnings, infos))

    def rerender(self):
        """Render the current issue list either flat or grouped by entry.
        Grouping coalesces ALL issues sharing the same entry signature into
        one parent (regardless of adjacency after sorting); file-level rows
        (summaries / unlinked files) always stay top-level. Display order
        follows the active column sort."""
        self.tree.delete(*self.tree.get_children())
        self._row_issues.clear()
        self._group_items.clear()

        def is_standalone(iss):
            return (not isinstance(iss.entry_index, int) or iss.entry_index < 0
                    or iss.entry_id in ("(none)", "(summary)"))

        ordered = self._sorted_issues()

        if not self.group_by_entry.get():
            for n, iss in enumerate(ordered):
                iid = "i{}".format(n)
                self._row_issues[iid] = iss
                self.tree.insert("", "end", iid=iid, values=(
                    iss.category, iss.entry_id, iss.message,
                    "Yes" if iss.fix else "No"), tags=(iss.severity,), open=True)
            return

        # Coalesce by entry signature, preserving first-seen display order;
        # standalone rows keep their own positions in the sequence.
        seq_items = []            # [(kind, payload)]  kind: 's' | 'g'
        buckets = {}
        for iss in ordered:
            if is_standalone(iss):
                seq_items.append(("s", [iss]))
                continue
            key = (iss.entry_id, iss.entry_index)
            if key not in buckets:
                buckets[key] = []
                seq_items.append(("g", buckets[key]))
            buckets[key].append(iss)

        seq = 0
        for kind, items in seq_items:
            if kind == "s":
                for iss in items:
                    iid = "i{}".format(seq); seq += 1
                    self._row_issues[iid] = iss
                    self.tree.insert("", "end", iid=iid, values=(
                        iss.category, iss.entry_id, iss.message,
                        "Yes" if iss.fix else "No"), tags=(iss.severity,),
                        open=True)
                continue
            worst = min(items, key=lambda i: self._SEV_RANK.get(i.severity, 9))
            nfix = sum(1 for i in items if i.fix)
            gid = "g{}".format(seq); seq += 1
            msg = worst.message
            if len(msg) > 140:
                msg = msg[:139] + "\u2026"
            parent = self.tree.insert("", "end", iid=gid, values=(
                "{} issue(s)".format(len(items)), worst.entry_id,
                msg + ("   [+]" if len(items) > 1 else ""),
                "{}/{} auto".format(nfix, len(items))),
                tags=(worst.severity,), open=False)
            self._group_items[parent] = items
            for iss in items:
                iid = "i{}".format(seq); seq += 1
                self._row_issues[iid] = iss
                self.tree.insert(parent, "end", iid=iid, values=(
                    iss.category, "", iss.message,
                    "Yes" if iss.fix else "No"), tags=(iss.severity,))

    # ------------------------------------------------------------------
    # selection helpers
    # ------------------------------------------------------------------
    def _issues_for_selection(self):
        out = []
        for iid in self.tree.selection():
            if iid in self._row_issues:
                out.append(self._row_issues[iid])
            elif iid in self._group_items:
                out.extend(self._group_items[iid])
        return out

    def _fix_selected(self):
        chosen = [i for i in self._issues_for_selection() if i.fix]
        self.app.apply_fixes(chosen)
        self.app.run_audit()

    def _goto_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE,
                                "Select an issue row first, then click "
                                "'Go to Entry' (or just double-click a row).")
            return
        iss = self._row_issues.get(sel[0]) or next(
            iter(self._group_items.get(sel[0], [])), None)
        if iss is not None:
            self.app.reveal_entry(iss)

    def _on_issue_activate(self, _event=None):
        """Double-click / Enter on an issue leaf jumps to that entry in the
        database tree (loads it in the editor). Group headers toggle."""
        iid = self.tree.focus()
        if iid and iid in self._row_issues:
            self.app.reveal_entry(self._row_issues[iid])
            return "break"
        return None   # group header: let Treeview expand/collapse natively

    def _fix_all(self):
        fixable = [i for i in self.issues if i.fix]
        if not fixable:
            messagebox.showinfo(APP_TITLE, "No auto-fixable issues found.")
            return
        if not messagebox.askyesno(APP_TITLE, "Apply {} automatic fixes?".format(len(fixable))):
            return
        self.app.apply_fixes(fixable)
        self.app.run_audit()

    def _export(self):
        if not self.issues:
            messagebox.showinfo(APP_TITLE, "Nothing to export. Run an audit first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                             filetypes=[("Text file", "*.txt")],
                                             initialfile="audit_report.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("IEM Database Audit Report - {}\n".format(datetime.datetime.now()))
                f.write("=" * 70 + "\n")
                for issue in self.issues:
                    f.write("[{}] {} :: {} :: {}\n".format(
                        issue.severity.upper(), issue.category, issue.entry_id, issue.message))
        except OSError as e:
            messagebox.showerror(APP_TITLE, "Could not write report:\n{}".format(e))
            return
        messagebox.showinfo(APP_TITLE, "Report saved to:\n{}".format(path))


# ---------------------------------------------------------------------------
# HISTORY PANEL (undo / redo)
# ---------------------------------------------------------------------------
class HistoryPanel(ttk.Frame):
    """Lists every tracked change newest-first with plain-language
    descriptions. Select any number of rows to undo or redo them."""

    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app

        top = ttk.Frame(self, style="TFrame")
        top.pack(fill="x", padx=10, pady=8)
        ttk.Button(top, text="Undo Selected", style="Accent.TButton",
                   command=self._undo_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Redo Selected", style="Blue.TButton",
                   command=self._redo_selected).pack(side="left", padx=4)
        ttk.Button(top, text="Clear History",
                   command=self._clear).pack(side="left", padx=4)
        self.summary_label = ttk.Label(top, text="", style="TLabel", foreground=TEXT_DIM)
        self.summary_label.pack(side="left", padx=16)

        columns = ("time", "action", "details")
        frame = ttk.Frame(self, style="TFrame")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree = ttk.Treeview(frame, columns=columns, show="tree headings",
                                  selectmode="extended")
        self.tree.heading("#0", text="#")
        self.tree.column("#0", width=50, anchor="center", stretch=False)
        self.tree.heading("time", text="Time")
        self.tree.column("time", width=90, anchor="w", stretch=False)
        self.tree.heading("action", text="Action")
        self.tree.column("action", width=520, anchor="w")
        self.tree.heading("details", text="")
        self.tree.column("details", width=60, anchor="center", stretch=False)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        attach_touch_scroll(self.tree)
        self.tree.bind("<MouseWheel>", self._on_mousewheel)
        self.tree.bind("<Button-4>", self._on_mousewheel)
        self.tree.bind("<Button-5>", self._on_mousewheel)

        self.tree.tag_configure("section", background=BG_CARD,
                                 foreground=ACCENT_ORANGE,
                                 font=(pick_font_family(), 9, "bold"))
        self.tree.tag_configure("op", foreground=TEXT_MAIN)
        self.tree.tag_configure("redoable", foreground=ACCENT_BLUE)

    def _on_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            amount = -1
        elif getattr(event, "num", None) == 5:
            amount = 1
        else:
            amount = -1 if getattr(event, "delta", 0) > 0 else 1
        self.tree.yview_scroll(amount, "units")
        return "break"

    KIND_VERB = {"edit": "Edited entry", "add": "Added entry",
                 "delete": "Deleted entry", "fixes": "Audit fixes"}

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        app = self.app
        n_undo = len(app.history)
        n_redo = len(app.redo_stack)
        self.summary_label.configure(
            text="{} undoable operation(s), {} redoable".format(n_undo, n_redo))
        if not n_undo and not n_redo:
            self.tree.insert("", "end", iid="sec:none", text="",
                             values=(" ", "No changes yet this session.", ""),
                             tags=("section",))
            return
        # undoable ops, newest at the top
        self.tree.insert("", "end", iid="sec:undo", text="",
                         values=(" ", "UNDOABLE  (most recent first)", ""),
                         tags=("section",))
        for display_no, i in enumerate(range(len(app.history) - 1, -1, -1)):
            op = app.history[i]
            self.tree.insert("", "end", iid="h:{}".format(i),
                             text=str(display_no + 1),
                             values=(op["when"], op["desc"], "undo"),
                             tags=("op",))
        if n_redo:
            self.tree.insert("", "end", iid="sec:redo", text="",
                             values=(" ", "REDOABLE  (undone operations)", ""),
                             tags=("section",))
            for j, op in enumerate(app.redo_stack):
                self.tree.insert("", "end", iid="r:{}".format(j),
                                 text="-", values=(op["when"], op["desc"], "redo"),
                                 tags=("redoable",))

    def _selected_ops(self):
        """Returns (undo_ops_in_application_order, redo_ops_in_order)."""
        undo_idx, redo_idx = [], []
        for iid in self.tree.selection():
            if iid.startswith("h:"):
                undo_idx.append(int(iid.split(":", 1)[1]))
            elif iid.startswith("r:"):
                redo_idx.append(int(iid.split(":", 1)[1]))
        # undo applies newest-first; redo applies oldest-first
        undo_idx.sort(reverse=True)
        redo_idx.sort()
        hist = self.app.history
        red = self.app.redo_stack
        return ([hist[i] for i in undo_idx if 0 <= i < len(hist)],
                [red[i] for i in redo_idx if 0 <= i < len(red)])

    def _undo_selected(self):
        undo_ops, redo_ops = self._selected_ops()
        if redo_ops and not undo_ops:
            messagebox.showinfo(APP_TITLE, "Those rows are already undone -- "
                                            "use 'Redo Selected' instead.")
            return
        if not undo_ops:
            messagebox.showinfo(APP_TITLE, "Select one or more operations to undo.")
            return
        if redo_ops:
            messagebox.showwarning(
                APP_TITLE,
                "Your selection mixes undoable and already-undone rows.\n\n"
                "Only the {} undoable row(s) will be processed.".format(len(undo_ops)))
        self.app.apply_history_ops(undo_ops, redo=False)
        self.refresh()

    def _redo_selected(self):
        undo_ops, redo_ops = self._selected_ops()
        if undo_ops and not redo_ops:
            messagebox.showinfo(APP_TITLE, "Those rows are not undone -- "
                                            "use 'Undo Selected' instead.")
            return
        if not redo_ops:
            messagebox.showinfo(APP_TITLE, "Select one or more undone "
                                           "operations to redo them.")
            return
        if undo_ops:
            messagebox.showwarning(
                APP_TITLE,
                "Your selection mixes undoable and already-undone rows.\n\n"
                "Only the {} redoable row(s) will be processed.".format(len(redo_ops)))
        self.app.apply_history_ops(redo_ops, redo=True)
        self.refresh()

    def _clear(self):
        if not self.app.history and not self.app.redo_stack:
            return
        if messagebox.askyesno(APP_TITLE,
                               "Forget all recorded history?\n\nYour database "
                               "entries are NOT affected -- this only clears "
                               "the undo/redo list."):
            self.app.history.clear()
            self.app.redo_stack.clear()
            self.refresh()


# ---------------------------------------------------------------------------
# MAIN APPLICATION
# ---------------------------------------------------------------------------
class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("{}  v{}".format(APP_TITLE, APP_VERSION))
        self._set_window_icon()
        self.geometry("1400x860")
        self.minsize(860, 540)   # half-screen snap stays fully usable
        self.configure(background=BG_MAIN)
        setup_styles(self)

        self.entries = []
        self.db_path = None
        self.data_root = None
        self.dirty = False
        self.editing_index = None  # index into self.entries currently loaded in editor, or None for "new"

        # undo/redo history (chronological ops; redo holds undone ops)
        self.history = []          # applied ops, oldest first
        self.redo_stack = []       # undone ops, in undo order
        self.HISTORY_MAX = 200     # cap so memory stays bounded
        self._audit_dirty = False  # mutations since last completed audit

        # offline spellchecker for the identity fields; dictionaries load in
        # a background thread so startup is never blocked
        self.speller = SP.SpellChecker(resource_base)
        self.speller.load_async()

        self._build_menu()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._try_auto_load()

        tools_panel.bind_app_title(APP_TITLE)

        # OS-level drag & drop onto the whole window (Windows; other
        # platforms fall back to the Browse buttons inside each tool).
        self.update_idletasks()
        self.drop_enabled = win_drop.enable_native_file_drop(self,
                                                             self._on_native_drop)

    def destroy(self):
        # Remove the native drop subclass while Tcl is still fully alive;
        # leaving it installed until Tk destroys the HWND can crash the
        # process if a message arrives during interpreter teardown.
        win_drop.disable_native_file_drop(self)
        super().destroy()

    # ------------------------------------------------------------------
    def _on_native_drop(self, paths):
        """Route OS drops: .txt/.csv -> Import Curves queue; a single
        .json/.gz -> offer to open as the database."""
        if not paths:
            return
        curves = [p for p in paths if curve_import.CL.is_curve_file(p)]
        dbs = [p for p in paths
               if os.path.isfile(p) and p.lower().endswith((".json", ".gz"))]
        if curves:
            self.notebook.select(self.curve_panel)
            self.curve_panel.add_paths(curves)
            if dbs:
                self.status_var.set(
                    "Queued {} curve file(s); dropped database file(s) ignored."
                    .format(len(curves)))
            return
        if len(dbs) == 1:
            if messagebox.askyesno(APP_TITLE,
                                   "Open the dropped database?\n\n{}".format(dbs[0])):
                self._load_from_path(dbs[0])
            return
        if dbs:
            messagebox.showinfo(APP_TITLE,
                                "Drop one database file at a time "
                                "({} received).".format(len(dbs)))
            return
        self.status_var.set(
            "Drop .txt/.csv measurement files (Import Curves tab) or a "
            "database.json.")

    def _mark_audit_dirty(self):
        """Flag audit results as stale; if the user is literally looking at
        the Audit tab right now, refresh immediately."""
        self._audit_dirty = True
        try:
            w = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if w is self.audit_panel and self.entries:
            self.run_audit()

    def refresh_spell_vocab(self):
        """Feed every Brand/Model/Variant in the loaded database to the
        spellchecker so product names are never flagged as typos."""
        texts = []
        for e in self.entries:
            texts.extend([e.get("brand", ""), e.get("model", ""), e.get("variant", "")])
        self.speller.replace_dynamic_vocab(texts)

    def _autosave(self):
        if not self.entries or not self.db_path:
            return
        if not hasattr(self, "_as_lock"):
            self._as_lock = threading.Lock()
            self._as_pending = None
            self._as_thread = None

        with self._as_lock:
            self._as_pending = (self.db_path, list(self.entries))

        if self._as_thread is not None and self._as_thread.is_alive():
            return                      # worker will pick up the newest pending

        def _worker():
            while True:
                with self._as_lock:
                    job = self._as_pending
                    self._as_pending = None
                if not job:
                    return
                db_path, snapshot = job
                try:
                    with _autosave_lock:
                        L.write_autosave(db_path, snapshot)
                except Exception as e:
                    L.log("Autosave failed: {}".format(e))

        self._as_thread = threading.Thread(target=_worker, daemon=True, name="autosave")
        self._as_thread.start()

    # ------------------------------------------------------------------
    # HISTORY / UNDO / REDO
    # ------------------------------------------------------------------
    @staticmethod
    def _deepcopy(entry):
        import copy
        return copy.deepcopy(entry) if entry is not None else None

    def _record_op(self, kind, desc, changes):
        """Store one completed mutation for the History tab.
        changes: list of {pos_hint, ref_before, copy_before,
                          ref_after,  copy_after} dicts (see _apply_history_op).
        """
        op = {
            "kind": kind,
            "desc": desc,
            "when": datetime.datetime.now().strftime("%H:%M:%S"),
            "changes": changes,
        }
        self.history.append(op)
        if len(self.history) > self.HISTORY_MAX:
            self.history = self.history[-self.HISTORY_MAX:]
        self.redo_stack.clear()   # new work invalidates the redo branch
        if self.history_panel is not None:
            self.history_panel.refresh()

    def _find_slot(self, target_ref, target_copy):
        """Locate an entry's current list position: object identity first,
        then id-field equality as a fallback (survives sorting)."""
        if target_ref is not None:
            for i, e in enumerate(self.entries):
                if e is target_ref:
                    return i
        tid = (target_copy or {}).get("id")
        if tid:
            for i, e in enumerate(self.entries):
                if e.get("id") == tid:
                    return i
        return -1

    def _apply_history_changes(self, op, redo):
        """Apply (or revert) every sub-change of `op` in place.

        Each change stores the transition ref_before -> ref_after (either
        side may be None for add/delete). Undoing walks it backwards,
        redoing forwards:
          - transition INTO nothing  -> remove the located entry
          - transition OUT of nothing-> insert a fresh copy
          - otherwise                -> replace content at its position
        Returns the set of affected list positions. Unresolvable
        sub-changes are skipped (history is convenience, not a ledger)."""
        affected = set()
        changes = op["changes"]
        # within one batch, invert order when reverting
        for ch in (changes if redo else reversed(changes)):
            pos_hint = ch["pos_hint"]
            if redo:
                out_of, into, insert_copy = ch["ref_before"], ch["ref_after"], ch["copy_after"]
            else:
                out_of, into, insert_copy = ch["ref_after"], ch["ref_before"], ch["copy_before"]

            if into is None:
                # ends without an entry -> removal
                pos = self._find_slot(out_of, ch["copy_before" if redo else "copy_after"])
                if 0 <= pos < len(self.entries):
                    del self.entries[pos]
                    affected.add(pos)
            elif out_of is None:
                # starts from nothing -> insertion
                pos = max(0, min(pos_hint, len(self.entries)))
                self.entries.insert(pos, self._deepcopy(insert_copy))
                affected.add(pos)
            else:
                # content replacement
                pos = self._find_slot(out_of, ch["copy_before" if redo else "copy_after"])
                if pos >= 0:
                    self.entries[pos] = self._deepcopy(insert_copy)
                    affected.add(pos)
        return affected

    def apply_history_ops(self, ops, redo):
        if not ops:
            return
        # Undo/redo reloads entries into the editor form (see below), which
        # would silently discard unsaved form edits. Guard them the same way
        # tree-selection and Add Entry do, BEFORE anything is mutated.
        if self.editor.form_is_dirty():
            verb = "Redo" if redo else "Undo"
            if not messagebox.askyesno(
                    APP_TITLE,
                    "The entry form has unsaved changes.\n\n{} will reload "
                    "the affected entries into the form and DISCARD those "
                    "edits.\n\nContinue?".format(verb)):
                return

        prev_ref = None
        if self.editing_index is not None and 0 <= self.editing_index < len(self.entries):
            prev_ref = self.entries[self.editing_index]

        all_affected = set()
        for op in ops:
            try:
                all_affected |= self._apply_history_changes(op, redo)
            except Exception as e:
                L.log("History {} failed: {}".format("redo" if redo else "undo", e))
            if redo:
                self._remove_op(self.redo_stack, op)
                self.history.append(op)
            else:
                self._remove_op(self.history, op)
                self.redo_stack.append(op)
        if all_affected:
            self.dirty = True
            self._mark_audit_dirty()
            self.populate_tree()
            new_idx = self._find_slot(prev_ref, prev_ref) if prev_ref is not None else -1
            if prev_ref is not None and new_idx >= 0:
                self.editing_index = new_idx
                self.editor.load_entry(self.entries[new_idx])
            else:
                self.editing_index = None
                self.editor.new_entry()
            self.editor.file_panel._refresh_available()
            self._autosave()
        self._notify_db_changed()
        verb = "Redid" if redo else "Undid"
        self.status_var.set("{} {} operation(s).".format(verb, len(ops)))
        if self.history_panel is not None:
            self.history_panel.refresh()

    def undo_last(self):
        if not self.history:
            messagebox.showinfo(APP_TITLE, "Nothing to undo yet.")
            return
        self.apply_history_ops([self.history[-1]], redo=False)

    def redo_last(self):
        if not self.redo_stack:
            messagebox.showinfo(APP_TITLE, "Nothing to redo.")
            return
        self.apply_history_ops([self.redo_stack[-1]], redo=True)

    # ------------------------------------------------------------------
    def _set_window_icon(self):
        """Use assets/icon.ico for the window/taskbar icon if present.
        This only affects the icon while the app is running from source
        or from a PyInstaller-built exe that wasn't given --icon; when
        built with --icon=assets/icon.ico (see README), Windows also
        uses it for the .exe file icon itself."""
        ico_path = os.path.join(resource_base(), "assets", "icon.ico")
        if os.path.isfile(ico_path):
            try:
                self.iconbitmap(default=ico_path)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Database...", command=self.open_database)
        filemenu.add_command(label="Set Data Folder...", command=self.set_data_folder)
        filemenu.add_separator()
        filemenu.add_command(label="Save As...", command=self.save_as)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filemenu)

        editmenu = tk.Menu(menubar, tearoff=0)
        editmenu.add_command(label="Add New Entry", command=self.add_entry)
        editmenu.add_command(label="Delete Selected Entry", command=self.delete_entry)
        editmenu.add_separator()
        editmenu.add_command(label="Undo Last Action", command=self.undo_last)
        editmenu.add_command(label="Redo Last Undone Action", command=self.redo_last)
        menubar.add_cascade(label="Edit", menu=editmenu)

        auditmenu = tk.Menu(menubar, tearoff=0)
        auditmenu.add_command(label="Run Full Audit", command=self.run_audit)
        menubar.add_cascade(label="Audit", menu=auditmenu)

        toolmenu = tk.Menu(menubar, tearoff=0)
        toolmenu.add_command(
            label="Convert Measurement Curves...",
            command=lambda: self.notebook.select(self.curve_panel))
        toolmenu.add_command(
            label="Compress to database.json.gz",
            command=self._open_export_tab)
        toolmenu.add_command(
            label="Split into AI Chunks",
            command=self._open_export_tab)
        menubar.add_cascade(label="Tools", menu=toolmenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)

    def _open_export_tab(self):
        self.notebook.select(self.tools_panel)

    def _build_layout(self):
        toolbar = ttk.Frame(self, style="Panel.TFrame")
        toolbar.pack(fill="x", side="top")
        ttk.Button(toolbar, text="Open Database", command=self.open_database).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Save As...", style="Accent.TButton", command=self.save_as).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Add Entry", command=self.add_entry).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Delete Entry", style="Danger.TButton", command=self.delete_entry).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Run Audit", style="Blue.TButton", command=self.run_audit).pack(side="left", padx=4, pady=6)

        ttk.Label(toolbar, text="Search:", style="Panel.TLabel").pack(side="left", padx=(20, 4))
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=4)
        attach_entry_context_menu(search_entry)
        self._search_debounce_id = None
        def _on_search_change(*a):
            if self._search_debounce_id:
                try:
                    self.after_cancel(self._search_debounce_id)
                except Exception:
                    pass
            self._search_debounce_id = self.after(150, self.populate_tree)
        self.search_var.trace_add("write", _on_search_change)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        self.paned = paned

        left = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=1)

        ttk.Label(left, text="DATABASE ENTRIES", style="Header.TLabel").pack(anchor="w", padx=8, pady=(8, 4))
        tree_frame = ttk.Frame(left, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.column("#0", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        attach_touch_scroll(self.tree)
        self._full_labels = {}
        self._ellipsis_after = None
        self.tree.bind("<Configure>", self._schedule_ellipsis, add="+")
        HoverTooltip(self.tree, self._tree_full_text_at)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        right = ttk.Frame(paned, style="TFrame")
        paned.add(right, weight=3)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.editor = EntryEditor(self.notebook, self)
        self._editor_tab_text = "  Editor  "
        self.notebook.add(self.editor, text=self._editor_tab_text)
        # live "N linked" badge on the Editor tab so auto-link feedback from
        # the Import Curves tab (or any other tab) is always visible
        self.editor.file_panel.on_files_changed = self._update_editor_tab_badge

        self.audit_panel = AuditPanel(self.notebook, self)
        self.notebook.add(self.audit_panel, text="  Audit  ")

        self.history_panel = None   # created just below, before any op recording
        self.history_panel = HistoryPanel(self.notebook, self)
        self.notebook.add(self.history_panel, text="  History  ")

        # companion tools (secondary to the editor by design)
        self.curve_panel = curve_import.CurveImportPanel(self.notebook, self)
        self.notebook.add(self.curve_panel, text="  Import Curves  ")

        self.tools_panel = tools_panel.ToolsPanel(self.notebook, self)
        self.notebook.add(self.tools_panel, text="  Export  ")

        status = ttk.Frame(self, style="Panel.TFrame")
        status.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="No database loaded.")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left", padx=8, pady=4)

    def _update_editor_tab_badge(self, n_linked):
        """Show the number of measurement files linked to the open entry
        form directly on the Editor tab (empty badge when zero)."""
        try:
            text = self._editor_tab_text
            if n_linked:
                text = "  Editor \u2022 {} linked  ".format(n_linked)
            idx = self.notebook.index(self.editor)
            if str(self.notebook.tab(idx, option="text")) != text:
                self.notebook.tab(idx, text=text)
        except Exception:
            pass

    def _show_about(self):
        messagebox.showinfo(
            APP_TITLE,
            "{} v{}\n\nAll-in-one companion for the IEM & headphone "
            "measurement database:\n"
            "  \u2022 Editor / Audit / History (this tab area)\n"
            "  \u2022 Import Curves - convert .txt/.csv measurements into "
            "the standard format and file them into your data folder\n"
            "  \u2022 Export - database.json.gz compression and AI-friendly "
            "chunk splitting\n\n"
            "Saving: always via Save As. Overwriting the loaded original is "
            "possible after a confirmation, and a safety snapshot of the "
            "original is kept in '.db_editor_backups' first.".format(
                APP_TITLE, APP_VERSION))

    def _on_close(self):
        if self.dirty:
            resp = messagebox.askyesnocancel(
                APP_TITLE,
                "You have unsaved changes. Save before exiting?\n\nYes = Save As..., No = Exit without saving, Cancel = Stay.")
            if resp is None:
                return
            if resp:
                self.save_as()
                # if still dirty after save attempt, stay
                if self.dirty:
                    return
        self.destroy()

    # ------------------------------------------------------------------
    # LOADING / SAVING
    # ------------------------------------------------------------------
    def _try_auto_load(self):
        candidates = [
            os.path.join(script_folder(), "database.json"),
            os.path.join(os.getcwd(), "database.json"),
        ]
        seen = set()
        for candidate in candidates:
            norm = os.path.normpath(candidate)
            if norm in seen:
                continue
            seen.add(norm)
            if os.path.isfile(candidate):
                self._load_from_path(candidate)
                return

    def open_database(self):
        path = filedialog.askopenfilename(
            title="Select database.json",
            filetypes=[("JSON database", "*.json *.json.gz"),
                       ("Gzip archive", "*.gz"),
                       ("All files", "*.*")])
        if not path:
            return
        self._load_from_path(path)

    def _load_from_path(self, path):
        # crash-recovery: offer the newest unseen autosave snapshot first
        load_from = path
        restored = False
        recovery = L.unseen_autosave(path)
        if recovery:
            try:
                stamp = datetime.datetime.fromtimestamp(
                    os.path.getmtime(recovery)).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                stamp = "unknown time"
            resp = messagebox.askyesnocancel(
                APP_TITLE,
                "A more recent autosaved recovery file was found:\n\n"
                "{}\n(saved {})\n\n"
                "Yes = Load the recovery file\n"
                "No = Continue with the selected database\n"
                "Cancel = Don't ask again about this backup".format(
                    os.path.basename(recovery), stamp))
            L.mark_autosave_seen(path)
            if resp:
                load_from = recovery
                restored = True
        try:
            entries, notes = L.load_database(load_from)
        except L.DatabaseLoadError as e:
            messagebox.showerror(APP_TITLE, "Failed to load database:\n\n{}".format(e))
            return
        except Exception as e:
            messagebox.showerror(APP_TITLE, "Unexpected error loading database:\n\n{}".format(e))
            return
        self.entries = entries
        self.db_path = path
        self.data_root = os.path.dirname(os.path.abspath(path))
        self.dirty = False
        self.editing_index = None
        self.editor.new_entry()
        self.refresh_spell_vocab()
        self.file_panel_root_changed()
        self.populate_tree()
        msg = "Loaded {} entries from {}{}".format(
            len(entries), path,
            "  (RECOVERED from autosave backup -- use Save As to keep it)" if restored else "")
        if notes:
            msg += "  ({} note(s))".format(len(notes))
            for n in notes:
                print(n)
            # show first few notes in dialog
            preview = "\n".join(notes[:10])
            if len(notes) > 10:
                preview += "\n... and {} more (see console)".format(len(notes)-10)
            messagebox.showwarning(APP_TITLE, "Notes while loading:\n\n" + preview)
        self.status_var.set(msg)
        self._notify_db_changed()
        # run audit without blocking the UI (threaded for large DBs / when a
        # data folder with thousands of measurement files is linked)
        self.run_audit(done=self._audit_done_popup)

    def _audit_done_popup(self, issue_count):
        if issue_count:
            messagebox.showinfo(
                APP_TITLE,
                "Database loaded.\n\nThe automatic audit found {} item(s) to review "
                "in the Audit tab.".format(issue_count))

    def set_data_folder(self):
        path = filedialog.askdirectory(title="Select the folder that contains the 'data' subfolder")
        if not path:
            return
        # Validate: allow either the parent of data/ or data/ itself
        norm = os.path.normpath(path)
        base = os.path.basename(norm).lower()
        data_candidate = os.path.join(path, "data") if base != "data" else path
        if not os.path.isdir(data_candidate):
            # try parent case where user selected data folder directly - already handled
            # otherwise warn
            if base != "data":
                resp = messagebox.askyesno(APP_TITLE,
                    "No 'data' subfolder found under:\n{}\n\nUse this folder anyway?".format(path))
                if not resp:
                    return
        # If user selected .../data, normalize to parent so audit uses parent as root
        if base == "data":
            # store parent as data_root so relative paths stay data/...
            self.data_root = os.path.dirname(norm)
        else:
            self.data_root = path
        self.file_panel_root_changed()
        self.status_var.set("Data folder set to: {}".format(self.data_root))
        self._notify_db_changed()

    def file_panel_root_changed(self):
        self.editor.file_panel.refresh_root_changed()

    def get_data_root(self):
        return self.data_root

    def _on_tab_changed(self, _event=None):
        """Auto-freshness hooks:
        - entering the Audit tab with unsaved-audit mutations -> silent rerun
        - entering the Editor tab  -> poll the data folder for new files"""
        try:
            widget = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if widget is self.audit_panel:
            if self._audit_dirty and self.entries:
                self.status_var.set(
                    "Changes made since the last audit - refreshing...")
                self.run_audit()
        elif widget is self.editor:
            try:
                self.editor.file_panel.poll_now()
            except Exception:
                pass
        elif widget is self.curve_panel:
            try:
                self.curve_panel.refresh_data_root()
            except Exception:
                pass
        elif widget is self.tools_panel:
            self.tools_panel.refresh_state()

    def _notify_db_changed(self):
        """Cheap refresh for panels that mirror database state (Export
        source summary, curve destination picker) after entries or paths
        change."""
        try:
            self.tools_panel.refresh_state()
        except Exception:
            pass
        try:
            self.curve_panel.refresh_data_root()
        except Exception:
            pass

    def save_as(self):
        if not self.entries:
            messagebox.showwarning(APP_TITLE, "Nothing to save -- no database loaded.")
            return
        try:
            issues = L.run_full_audit(self.entries, self.data_root)
        except Exception as e:
            messagebox.showwarning(APP_TITLE, "Audit failed before save:\n{}".format(e))
            issues = []
        blocking = [i for i in issues if i.severity == "error"]
        dup_ids = {}
        for idx, e in enumerate(self.entries):
            dup_ids.setdefault(e.get("id"), []).append(idx)
        dup_msgs = ["Duplicate ID '{}' used {} times.".format(k, len(v))
                    for k, v in dup_ids.items() if len(v) > 1]
        if dup_msgs:
            messagebox.showerror(APP_TITLE, "Cannot save -- fix these first:\n\n" + "\n".join(dup_msgs))
            return
        if blocking:
            proceed = messagebox.askyesno(
                APP_TITLE,
                "The audit found {} error-level issue(s) (see Audit tab).\n"
                "Save anyway?".format(len(blocking)))
            if not proceed:
                return
        initial = "database_edited.json"
        if self.db_path:
            base = os.path.splitext(os.path.basename(self.db_path))[0]
            initial = "{}_edited.json".format(base)
        path = filedialog.asksaveasfilename(
            title="Save database as...", defaultextension=".json",
            filetypes=[("JSON database", "*.json")], initialfile=initial,
            initialdir=self.data_root or ".")
        if not path:
            return
        overwriting_original = bool(self.db_path) and \
            os.path.normcase(os.path.abspath(path)) == \
            os.path.normcase(os.path.abspath(self.db_path))
        if overwriting_original:
            resp = messagebox.askyesno(
                APP_TITLE,
                "You picked the ORIGINAL database you loaded:\n\n{}\n\n"
                "Overwrite it?\n\nA safety copy of the original is saved "
                "into '.db_editor_backups' first.".format(path))
            if not resp:
                return
        try:
            current_id = None
            if self.editing_index is not None and 0 <= self.editing_index < len(self.entries):
                current_id = self.entries[self.editing_index].get("id")
            snapshot_note = ""
            if overwriting_original:
                snap = L.write_pre_overwrite_snapshot(self.db_path)
                if snap:
                    snapshot_note = "  (original backed up to {})".format(
                        os.path.basename(snap))
            ordered = L.save_database(path, self.entries)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "Failed to save:\n{}".format(e))
            return
        except Exception as e:
            messagebox.showerror(APP_TITLE, "Unexpected error while saving:\n{}".format(e))
            return
        self.entries = ordered
        self.dirty = False
        # FIX C6: everything is committed to disk now -- do not offer the
        # pre-save autosave as "recovery" on the next launch.
        L.mark_autosave_seen(self.db_path)
        if current_id:
            for i, e in enumerate(self.entries):
                if e.get("id") == current_id:
                    self.editing_index = i
                    break
            else:
                self.editing_index = None
        self.populate_tree()
        if self.editing_index is not None:
            iid = "entry:{}".format(self.editing_index)
            try:
                self.tree.selection_set(iid)
                self.tree.see(iid)
            except Exception:
                pass
        self.status_var.set("Saved {} entries to {}{}".format(
            len(ordered), path, snapshot_note))
        if overwriting_original:
            messagebox.showinfo(APP_TITLE,
                                "Saved (overwrote original):\n{}\n{}"
                                .format(path, snapshot_note))
        else:
            messagebox.showinfo(APP_TITLE, "Saved as:\n{}".format(path))
        self._notify_db_changed()

    # ------------------------------------------------------------------
    # TREE / SELECTION
    # ------------------------------------------------------------------
    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._full_labels = {}
        query = self.search_var.get().strip().lower()
        by_brand = {}
        for idx, e in enumerate(self.entries):
            hay = " ".join([e.get("brand", ""), e.get("model", ""), e.get("variant", ""), e.get("id", "")]).lower()
            if query and query not in hay:
                continue
            by_brand.setdefault(e.get("brand", "(no brand)"), []).append(idx)
        for brand in sorted(by_brand.keys(), key=str.lower):
            idxs = by_brand[brand]
            brand_text = "{}  ({})".format(brand, len(idxs))
            node = self.tree.insert("", "end", iid="brand:{}".format(brand),
                                     text=brand_text, open=bool(query))
            self._full_labels[node] = brand_text
            for idx in sorted(idxs, key=lambda i: L.sort_key(self.entries[i])):
                e = self.entries[idx]
                label = e.get("model", "")
                if e.get("variant"):
                    label += "  [{}]".format(e["variant"])
                label += "   -- {}".format(e.get("id", ""))
                iid = "entry:{}".format(idx)
                self._full_labels[iid] = label
                self.tree.insert(node, "end", iid=iid, text=label)
        self._apply_ellipsis()

    # -- overflow handling for the database tree ---------------------------
    def _schedule_ellipsis(self, _event=None):
        if self._ellipsis_after:
            try:
                self.after_cancel(self._ellipsis_after)
            except Exception:
                pass
        self._ellipsis_after = self.after(140, self._apply_ellipsis)

    def _apply_ellipsis(self):
        """Rewrite visible row text truncated to the current widget width.
        Full labels stay in self._full_labels (tooltips / re-widen)."""
        self._ellipsis_after = None
        wpx = self.tree.winfo_width()
        if wpx < 60 or not self._full_labels:
            return
        try:
            import tkinter.font as tkfont
            fnt = tkfont.Font(font=(pick_font_family(), 10))
            cw = max(4, fnt.measure("n"))
        except Exception:
            cw = 8
        budget_base = int((wpx - 52) // cw)      # padding + scrollbar margin
        if budget_base < 8:
            return
        for iid, full in list(self._full_labels.items()):
            depth = 1 if iid.startswith("brand:") else 2
            disp = ellipsize(full, budget_base - depth * 2)
            try:
                if self.tree.item(iid, "text") != disp:
                    self.tree.item(iid, text=disp)
            except Exception:
                pass

    def _tree_full_text_at(self, x, y):
        iid = self.tree.identify_row(y)
        if iid and iid in self._full_labels:
            return self._full_labels[iid]
        return ""

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("entry:"):
            return
        if getattr(self, "_sel_guard", False):
            # this is our own re-selection of the PREVIOUS row after the user
            # refused to discard edits -- do NOT reload it into the editor,
            # or the very text we just protected would be wiped.
            self._sel_guard = False
            return
        if self.editor.form_is_dirty():
            if not messagebox.askyesno(
                    APP_TITLE,
                    "The entry form has unsaved changes.\n\nDiscard them and open "
                    "'{}'?".format(self.entries[int(iid.split(':', 1)[1])].get("id", "?"))):
                prev = getattr(self, "_selected_iid", None)
                if prev:
                    self._sel_guard = True
                    try:
                        self.tree.selection_set(prev)
                        return "break"
                    except Exception:
                        self._sel_guard = False
                return "break"
        self._selected_iid = iid
        idx = int(iid.split(":", 1)[1])
        self.editing_index = idx
        self.editor.load_entry(self.entries[idx])
        # Internal re-selections (e.g. post-commit re-highlight) must NOT
        # yank the user out of whichever tab they're reading.
        if not getattr(self, "_quiet_select", False):
            self.notebook.select(self.editor)

    def reveal_entry(self, issue):
        """Jump from an Audit-tab issue to its entry: resolve by id, clear a
        hiding search filter, open the brand node, select/reveal in the tree,
        load it in the editor and switch there. Falls back to the frozen
        audit index when the id no longer matches anything."""
        eid = getattr(issue, "entry_id", "") or ""
        idx = None
        if eid and not eid.startswith("("):
            for i, e in enumerate(self.entries):
                if e.get("id") == eid:
                    idx = i
                    break
        if idx is None and isinstance(getattr(issue, "entry_index", None), int):
            j = issue.entry_index
            if 0 <= j < len(self.entries) and self.entries[j].get("id") == eid:
                idx = j
        if idx is None:
            self.status_var.set(
                "Entry '{}' no longer exists in the loaded database.".format(eid))
            return
        if self.search_var.get():
            # filter may be hiding this row -- lift it so the reveal works.
            # Cancel the trace-scheduled rebuild or it will fire after our
            # immediate repopulate and wipe the fresh selection.
            self.search_var.set("")
            if getattr(self, "_search_debounce_id", None):
                try:
                    self.after_cancel(self._search_debounce_id)
                except Exception:
                    pass
                self._search_debounce_id = None
            self.populate_tree()
        iid = "entry:{}".format(idx)
        parent = self.tree.parent(iid)
        if parent:
            self.tree.item(parent, open=True)
        try:
            self.tree.see(iid)
            self.tree.selection_set(iid)
        except Exception:
            pass
        # selection_set fires <<TreeviewSelect>>, which loads the entry in
        # the editor and switches to it (respecting the unsaved-changes
        # guard). Make sure the tab switch happens even when the form was
        # clean but the event path skipped it.
        if not self.editor.form_is_dirty():
            self.editing_index = idx
            self._selected_iid = iid
            self.editor.load_entry(self.entries[idx])
            self.notebook.select(self.editor)
        self.status_var.set("Jumped to '{}' from the Audit tab.".format(eid))

    # ------------------------------------------------------------------
    # ADD / DELETE / COMMIT
    # ------------------------------------------------------------------
    def add_entry(self):
        if self.editor.form_is_dirty() and not messagebox.askyesno(
                APP_TITLE, "The entry form has unsaved changes.\n\nDiscard them "
                           "and start a new entry?"):
            return
        self.editing_index = None
        self.editor.new_entry()
        self.notebook.select(self.editor)
        self.tree.selection_remove(self.tree.selection())
        self._selected_iid = None

    def delete_entry(self):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("entry:"):
            messagebox.showinfo(APP_TITLE, "Select an entry in the tree to delete.")
            return
        idx = int(sel[0].split(":", 1)[1])
        e = self.entries[idx]
        msg = "Delete entry '{}'?".format(e.get("id"))
        if self.editor.form_is_dirty():
            msg += "\n\n(The entry form also has unsaved changes which will be discarded.)"
        if not messagebox.askyesno(APP_TITLE, msg):
            return
        del self.entries[idx]
        self.dirty = True
        self._mark_audit_dirty()
        self.editing_index = None
        self._selected_iid = None
        self.editor.new_entry()
        self.populate_tree()
        self.status_var.set("Deleted entry '{}'. {} entries remain (unsaved).".format(e.get("id"), len(self.entries)))
        self._notify_db_changed()
        self._record_op("delete", "Deleted entry '{}' ({} {})".format(
            e.get("id"), e.get("brand", ""), e.get("model", "")), [{
            "pos_hint": idx,
            "ref_before": e, "copy_before": self._deepcopy(e),
            "ref_after": None, "copy_after": None,
        }])
        self._autosave()

    def validate_and_commit(self, entry, original_id):
        editor = self.editor
        baseline_new = bool(getattr(editor, "_baseline_is_new", False))
        editing = self.editing_index is not None \
            and 0 <= self.editing_index < len(self.entries)

        treat_as_add = editing and baseline_new \
            and entry["id"] != self.entries[self.editing_index].get("id")

        if editing and not treat_as_add:
            old_id = self.entries[self.editing_index].get("id")
            if entry["id"] != old_id:
                ok = messagebox.askyesno(
                    APP_TITLE,
                    "Replace the content of existing entry\n\n  '{}'\n\nwith the "
                    "form data for '{}'?".format(old_id, entry["id"]))
                if not ok:
                    return ["Cancelled - '{}' was left unchanged.".format(old_id)]

        if editing and not treat_as_add:
            existing_ids = {e.get("id") for i, e in enumerate(self.entries)
                            if i != self.editing_index}
        else:
            editing_for_dup = self.editing_index if (editing and not treat_as_add) else None
            existing_ids = {e.get("id") for i, e in enumerate(self.entries)
                            if editing_for_dup is None or i != editing_for_dup}

        errors = L.validate_entry(entry, existing_ids=existing_ids, exclude_id=None)
        if errors:
            return errors
        clean = L.build_clean_entry(entry)

        if editing and not treat_as_add:
            idx = self.editing_index
            old_obj = self.entries[idx]
            old_copy = self._deepcopy(old_obj)
            self.entries[idx] = clean
            detail = L.describe_entry_change(old_copy, clean)
            desc = "Edited '{}'{}".format(clean["id"],
                                          " -- {}".format(detail) if detail else "")
            self._record_op("edit", desc, [{
                "pos_hint": idx,
                "ref_before": old_obj, "copy_before": old_copy,
                "ref_after": clean, "copy_after": self._deepcopy(clean),
            }])
        else:
            self.entries.append(clean)
            self.editing_index = len(self.entries) - 1
            self._record_op("add", "Added entry '{}' ({} {})".format(
                clean["id"], clean.get("brand", ""), clean.get("model", "")), [{
                "pos_hint": self.editing_index,
                "ref_before": None, "copy_before": None,
                "ref_after": clean, "copy_after": self._deepcopy(clean),
            }])

        editor._baseline_is_new = False          # future saves EDIT this entry
        editor.original_id = clean["id"]
        editor._capture_baseline()               # committed state == new baseline
        for key in ("brand", "model", "variant"):
            if clean.get(key):
                self.speller.add_vocab(clean[key])
        self.dirty = True
        self._mark_audit_dirty()
        self.populate_tree()
        self.status_var.set("Saved entry '{}' ({} total entries, unsaved changes).".format(
            clean["id"], len(self.entries)))
        self._notify_db_changed()
        self._autosave()
        iid = "entry:{}".format(self.editing_index)
        # Visual-only re-highlight: suppress the handler entirely (guard is
        # consumed by the event whenever Tk delivers it), so no tab-yank.
        self._sel_guard = True
        try:
            self.tree.selection_set(iid)
            self.tree.see(iid)
        except Exception:
            self._sel_guard = False
        return []

    # ------------------------------------------------------------------
    # AUDIT
    # ------------------------------------------------------------------
    def run_audit(self, done=None):
        """Run the full audit. Heavy runs happen in a daemon thread that only
        computes; ALL tkinter access happens here on the main thread via an
        after()-poll loop, so this is safe during startup, shutdown, and any
        Tcl build."""
        if not self.entries:
            messagebox.showinfo(APP_TITLE, "No database loaded.")
            return

        def _compute():
            try:
                # iterate a snapshot: the UI thread may add/delete entries
                # while this audit runs (deleting from the live list under
                # an index-based loop would raise and abort the audit)
                return L.run_full_audit(list(self.entries), self.data_root), None
            except Exception as e:
                return [], e

        if len(self.entries) < 5000 and not self.data_root:
            issues, err = _compute()
            if err:
                messagebox.showwarning(APP_TITLE, "Audit failed:\n{}".format(err))
            else:
                self.audit_panel.show_issues(issues)
                self.notebook.select(self.audit_panel)
                self._audit_dirty = False
            if done:
                done(len(issues))
            return

        self.status_var.set("Running audit...")
        self.notebook.select(self.audit_panel)

        result = {}
        th = threading.Thread(
            target=lambda: result.update(zip(("issues", "err"), _compute())),
            daemon=True, name="audit")

        def poll():
            if th.is_alive():
                self.after(60, poll)
                return
            err = result.get("err")
            if err:
                messagebox.showwarning(APP_TITLE, "Audit failed:\n{}".format(err))
                self.status_var.set("Audit failed.")
            else:
                issues = result.get("issues", [])
                self.audit_panel.show_issues(issues)
                self.status_var.set("Audit complete: {} issues".format(len(issues)))
                self._audit_dirty = False
            if done:
                done(len(result.get("issues", []) if not err else []))

        th.start()
        self.after(60, poll)

    def apply_fixes(self, issues):
        fixable = [i for i in issues if i.fix]
        if not fixable:
            return
        before_snapshot = [self._deepcopy(e) for e in self.entries]
        applied = failed = stale = 0
        for issue in fixable:
            try:
                pos = issue.fix(self.entries)
                if isinstance(pos, int) and 0 <= pos < len(self.entries):
                    applied += 1
                else:
                    stale += 1
            except Exception as e:
                failed += 1
                L.log("Fix '{}' failed: {}".format(issue.category, e))

        changes = []
        for i, (before_e, after_e) in enumerate(zip(before_snapshot, self.entries)):
            if before_e != after_e:
                obj = self.entries[i]
                changes.append({
                    "pos_hint": i,
                    "ref_before": obj, "copy_before": before_e,
                    "ref_after": obj, "copy_after": self._deepcopy(obj),
                })
        if not changes:
            self.status_var.set(
                "No fixes applied -- all {} result(s) were stale for the "
                "current database. Re-run the audit.".format(len(fixable)))
            return
        n_entries = len({c["pos_hint"] for c in changes})
        desc = "Applied {} audit fix(es) across {} entr{}".format(
            len(changes), n_entries, "y" if n_entries == 1 else "ies")
        self._record_op("fixes", desc, changes)
        note = ""
        skipped = max(0, len(fixable) - len(changes))
        if failed or skipped:
            note = " ({} stale/failed)".format(skipped + failed)
        self.dirty = True
        self.populate_tree()
        self.status_var.set("Applied {} fix(es){}. Remember to Save As to keep them.".format(
            len(changes), note))
        self._notify_db_changed()
        self._autosave()

    # ------------------------------------------------------------------
    # AUTOCOMPLETE SUGGESTION PROVIDERS
    # ------------------------------------------------------------------
    def brand_suggestions(self, text):
        text = text.lower()
        found = {e.get("brand", "") for e in self.entries
                 if text in e.get("brand", "").lower()}
        found.discard("")
        return sorted(found, key=str.lower)

    def model_suggestions(self, text):
        text = text.lower()
        brand = self.editor.brand_entry.get().strip().lower() if hasattr(self, "editor") else ""
        matches, all_matches = [], []
        for e in self.entries:
            m = e.get("model", "")
            if not m:
                continue
            if text in m.lower():
                all_matches.append(m)
                if brand and e.get("brand", "").lower() == brand:
                    matches.append(m)
        pool = matches if matches else all_matches
        seen = sorted(set(pool), key=str.lower)
        return seen

    def variant_suggestions(self, text):
        text = text.lower()
        seen = set()
        for e in self.entries:
            v = e.get("variant", "")
            if v and text in v.lower():
                seen.add(v)
        return sorted(seen, key=str.lower)

    @staticmethod
    def _remove_op(op_list, op):
        for k in range(len(op_list)):
            if op_list[k] is op:
                del op_list[k]
                return True
        return False


def main():
    app = MainApp()
    app.mainloop()


def _int_input(raw, default=None):
    s = str(raw).strip().replace("_", "")
    if s == "":
        return default
    try:
        return int(s)
    except ValueError:
        return default

if __name__ == "__main__":
    main()