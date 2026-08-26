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
import re
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
import fr_plot
import theme
import tools_panel
import curve_import
import ai_import
import win_drop

APP_TITLE = "Database Tool"

# serializes background autosave writes (one snapshot on disk at a time)
_autosave_lock = threading.Lock()
APP_VERSION = "2.0"

# ---------------------------------------------------------------------------
# THEME -- colors and fonts live in theme.py (shared with the Import
# Curves / Export panels so every surface stays consistent). Theme
# switching happens through the View menu; the app always renders in the
# OS's own default UI font.
# ---------------------------------------------------------------------------

# Display-only emoji shown next to each tag in the picker so tags are faster
# to scan visually. The underlying tag strings saved to database.json are
# never changed -- this is purely cosmetic in the UI.
TAG_EMOJI = {
    "Basshead": "\U0001F4A5",
    "Sub-Bass": "\U0001F30A",
    "Punchy Bass": "\U0001F94A",
    "Warm": "\U0001F33F",
    "Neutral": "\u2696\uFE0F",
    "V-Shaped": "\U0001F53A",
    "U-Shaped": "\U0001F9F2",
    "Balanced": "\u262F\uFE0F",
    "Bright": "\u2728",
    "Treblehead": "\u26A1",
    "Dark": "\U0001F311",
    "Vocal-Focused": "\U0001F5E3\uFE0F",
    "Detailed": "\U0001F48E",
    "Resolving": "\U0001F50D",
    "Technical": "\U0001F52C",
    "Wide-Stage": "\U0001F3DF\uFE0F",
    "Good-Imaging": "\U0001F52D",
    "Smooth": "\U0001F9C8",
    "Reference": "\U0001F4D0",
    "Analytical": "\U0001F9E0",
    "Fun": "\U0001F525",
    "Relaxed": "\U0001F60C",
    "Gaming": "\U0001F3AE",
    "Competitive-Gaming": "\U0001F3C6",
    "Studio-Monitoring": "\U0001F39B\uFE0F",
    "Collab": "\U0001F91D",
    "Limited-Edition": "\U0001F31F",
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
                       background=theme.BG_CARD, foreground=theme.TEXT_MAIN,
                       activebackground=theme.BORDER_LIGHT, activeforeground=theme.TEXT_MAIN,
                       font=theme.font(13))
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
    """Delegate to theme.apply_styles (the full retro style sheet lives in
    theme.py so theme/font switching can simply call it again)."""
    return theme.apply_styles(root)


def make_card(parent, style="Card.TFrame"):
    """IEM Tool-style offset-shadow card (implementation in theme.py)."""
    return theme.make_card(parent, style)


def restyle_app(root):
    """Full live re-theme: re-apply the ttk style sheet, retint every tk
    widget, then run the registered hooks (canvas art, menus, tree tags).

    A theme switch reconfigures a LOT of individual widgets one at a time
    (every raw tk widget's colors via retint(), every canvas redraw via
    the retheme hooks). Tk normally coalesces redraws until idle, but a
    change set this large still tends to paint in visible waves -- old
    colors here, new colors there, a frame or two apart -- which reads as
    a flicker even though nothing is actually wrong. Hiding the window for
    the duration (alpha 0 -> do everything -> alpha 1) means the person
    only ever sees the before frame and the after frame, never the
    in-between. -alpha is supported on Windows and macOS; where it isn't
    (some Linux window managers) the calls quietly no-op and the switch
    still applies, just without the anti-flicker step.

    The reveal itself is deferred a couple of scheduler turns: the menubar
    is recolored LAST (menus are restyled by a retheme hook), and its
    redraw lands on a later event-loop pass than update_idletasks().
    Restoring alpha immediately used to show that straggler menubar
    repaint as a visible flash across File/Edit/Audit/Tools/View/Help.
    Waiting ~2 frames lets every queued repaint -- menubar included --
    finish into the hidden buffer before the window comes back."""
    def _reveal():
        try:
            root.attributes("-alpha", 1.0)
        except Exception:
            pass

    try:
        root.attributes("-alpha", 0.0)
    except Exception:
        pass
    try:
        theme.apply_styles(root)
        theme.retint(root)
        theme.run_retheme_hooks()
        root.update_idletasks()
        # give Tk two idle turns to drain every pending repaint (widgets,
        # canvases AND the native menubar) while nothing is visible
        root.after(16, lambda: root.after(16, _reveal))
        return True
    except Exception:
        _reveal()
        return False


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
        # borderless: this widget sits INSIDE a shadow card, so a bordered
        # Card.TFrame surface here would draw a box around the entry
        super().__init__(parent, style="CardFlat.TFrame")
        self.suggestions_provider = suggestions_provider  # callable(text) -> list[str]
        self.on_change = on_change
        self.spell_checker = spell_checker
        self.var = tk.StringVar()
        kwargs.setdefault("font", theme.font(13))
        self.entry = ttk.Entry(self, textvariable=self.var, **kwargs)
        self.entry.pack(fill="x")
        # Thin canvas directly under the entry used to draw red bars beneath
        # flagged words (monospace font => exact per-character pixel math).
        self.underline = tk.Canvas(self, height=3, background=theme.BG_CARD,
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
                                    fill=theme.ACCENT_RED, width=0)

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
                    background=theme.BORDER,
                    foreground=theme.ACCENT_ORANGE,
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
            self.listbox = tk.Listbox(self.popup, background=theme.BG_INPUT, foreground=theme.TEXT_MAIN,
                                       selectbackground=theme.ACCENT_BLUE,
                                       selectforeground=theme.contrast_text(theme.ACCENT_BLUE),
                                       highlightthickness=1, highlightbackground=theme.BORDER,
                                       activestyle="none", height=min(8, len(items)),
                                       font=theme.font(13))
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
# TREE SEARCH (plain text + field mini-syntax)
# ---------------------------------------------------------------------------
_NUM_EXPR_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(\d+)(?:\s*(?:-|to)\s*(\d+))?$")

SEARCH_HELP = (
    "Search tips\n\n"
    "Plain text matches Brand / Model / Variant / ID.\n"
    "Field filters (space-separated, ANDed):\n"
    "  tag:bass          tag containing 'bass'\n"
    "  price:>500        price over $500  (also >= < <= = 100-400)\n"
    "  year:>2020        launch year after 2020\n"
    "  ff:tws            form factor contains 'tws'\n"
    "  driver:planar     driver type/config contains 'planar'\n"
    "  conn:mmcx         connector contains 'mmcx'\n"
    "  brand:moondrop    model:chu  variant:dsp  id:...\n"
    "Example:  moondrop tag:bass price:<100")


def _num_expr_ok(value, expr):
    """'price'/'year' filter: supports > >= < <= = and a-b ranges."""
    m = _NUM_EXPR_RE.match(expr)
    if not m:
        return str(value) == expr
    op = m.group(1) or "="
    a = int(m.group(2))
    b = int(m.group(3)) if m.group(3) else None
    if op == ">":
        return value > a
    if op == "<":
        return value < a
    if op == ">=":
        return value >= a
    if op == "<=":
        return value <= a
    return a <= value <= b if b is not None else value == a


def _token_matches(entry, key, val):
    """One 'key:value' filter token. Unknown keys fall back to a plain
    substring search over the usual haystack so old habits never break."""
    if key in ("tag", "tags"):
        return any(val in str(t).lower() for t in (entry.get("tags") or []))
    if key in ("ff", "form", "form_factor"):
        return val in (entry.get("form_factor") or "").lower()
    if key in ("conn", "connector"):
        return val in (entry.get("connector") or "").lower()
    if key in ("driver", "drv"):
        hay = "{} {}".format(entry.get("driver_type") or "",
                             entry.get("driver_config") or "").lower()
        return val in hay
    if key == "price":
        return _num_expr_ok(L.coerce_int(entry.get("price_usd", 0), 0), val)
    if key == "year":
        return _num_expr_ok(L.coerce_int(entry.get("year", 0), 0), val)
    if key in ("brand", "model", "variant", "id"):
        return val in (entry.get(key) or "").lower()
    # unknown key: search the combined haystack for the WHOLE token text
    hay = " ".join([entry.get("brand", ""), entry.get("model", ""),
                    entry.get("variant", ""), entry.get("id", "")]).lower()
    return ("{}:{}".format(key, val)) in hay


def entry_matches_query(entry, query):
    """Tree filter: whitespace-separated tokens, ALL must match. Plain
    tokens substring-match Brand/Model/Variant/ID; 'key:value' tokens
    target one field (see SEARCH_HELP)."""
    hay = " ".join([entry.get("brand", ""), entry.get("model", ""),
                    entry.get("variant", ""), entry.get("id", "")]).lower()
    for tok in query.split():
        if ":" in tok:
            key, _, val = tok.partition(":")
            if not _token_matches(entry, key.lower(), val.lower()):
                return False
        elif tok not in hay:
            return False
    return True


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


def ellipsize_path(text, max_chars):
    """Truncate a folder/file path from the FRONT, keeping the filename
    intact (...\u2026/ADEN/AB123_measurement.txt). Measurement paths
    mostly share the same leading "data/..." folder, so tail-ellipsizing
    (the general ellipsize() above) hid the one part -- the filename --
    that actually tells rows apart."""
    if not text:
        return text
    if max_chars is None or len(text) <= max_chars:
        return text
    if max_chars < 4:
        return ellipsize(text, max_chars)
    return "\u2026" + text[-(max_chars - 1):]


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
        self._hide_window()

    def _hide_window(self):
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
        tk.Label(tw, text=txt, justify="left", background=theme.BG_CARD,
                 foreground=theme.TEXT_MAIN, borderwidth=1, relief="solid",
                 font=theme.font(12), wraplength=560
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
        # borderless card surface: the make_card() wrapper supplies the
        # 1px border + offset shadow (Card.TFrame here would double it)
        super().__init__(parent, style="CardFlat.TFrame")
        self.on_change = on_change
        self.counts = {}          # tech -> StringVar("0".."16"); 0 = unused
        self.count_widgets = {}   # tech -> (minus_btn, plus_btn)
        self.labels = {}          # tech -> clickable icon+name label
        self._tech_frames = {}
        self._cols = 2            # current grid columns (responsive)

        ttk.Label(self, text="DRIVER CONFIGURATION", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 4))
        ttk.Label(self, text="Select the driver technology & count.",
                  style="Card.TLabel", foreground=theme.TEXT_DIM).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        for i, tech in enumerate(L.DRIVER_TECH_ORDER):
            frame = ttk.Frame(self, style="CardFlat.TFrame")
            frame.grid(row=2 + i // self._cols, column=i % self._cols,
                       sticky="w", padx=8, pady=3)
            self._tech_frames[tech] = frame
            icon = ICONS.get(L.DRIVER_TYPE_ICON.get(tech, tech.lower()))
            # the icon+name label doubles as a toggle: click = 0 <-> 1
            lbl = ttk.Label(frame, text=L.DRIVER_TECH_LABELS[tech],
                            image=icon, compound="left" if icon else "none",
                            style="Card.TLabel", cursor="hand2")
            lbl.image = icon
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda _e, t=tech: self._toggle(t))
            self.labels[tech] = lbl
            count_var = tk.StringVar(value="0")
            self.counts[tech] = count_var
            # horizontal -/+ stepper: [ - ] count [ + ]. 0 = driver unused;
            # pressing + to 1 enables it, back down to 0 removes it.
            stepper = ttk.Frame(frame, style="CardFlat.TFrame")
            minus = ttk.Button(stepper, text="\u2212", width=3,
                               command=lambda t=tech: self._bump(t, -1))
            minus.pack(side="left")
            val = ttk.Label(stepper, textvariable=count_var, style="Card.TLabel",
                            width=2, anchor="center")
            val.pack(side="left", padx=2)
            plus = ttk.Button(stepper, text="+", width=3,
                              command=lambda t=tech: self._bump(t, +1))
            plus.pack(side="left")
            stepper.pack(side="left", padx=(6, 0))
            self.count_widgets[tech] = (minus, plus)

        result_row = 2 + (len(L.DRIVER_TECH_ORDER) + self._cols - 1) // self._cols + 1
        self.result_label = ttk.Label(self, text="Driver Type: (none)      Config: (none)",
                                       style="Card.TLabel", foreground=theme.ACCENT_GREEN,
                                       font=theme.font(13, "bold"))
        self.result_label.grid(row=result_row, column=0, columnspan=4,
                               sticky="w", padx=8, pady=(8, 8))
        self.set("", "")

    def set_columns(self, cols):
        """Responsive: 2 columns on wide windows, 1 stacked column when the
        form is too narrow for both side-by-side rows to fit."""
        cols = 1 if cols <= 1 else 2
        if cols == self._cols:
            return
        self._cols = cols
        n = len(L.DRIVER_TECH_ORDER)
        for i, tech in enumerate(L.DRIVER_TECH_ORDER):
            self._tech_frames[tech].grid_configure(row=2 + i // cols,
                                                    column=i % cols)
        result_row = 2 + (n + cols - 1) // cols + 1
        self.result_label.grid_configure(row=result_row)

    def _count(self, tech):
        try:
            return max(0, min(16, int(self.counts[tech].get())))
        except (TypeError, ValueError):
            return 0

    def _refresh_row(self, tech):
        """Dim unused drivers; the minus button has nothing to remove at 0."""
        active = self._count(tech) > 0
        self.labels[tech].configure(
            foreground=theme.TEXT_MAIN if active else theme.TEXT_DIM)
        minus, _plus = self.count_widgets[tech]
        minus.state(["disabled"] if not active else ["!disabled"])

    def _bump(self, tech, delta):
        c = max(0, min(16, self._count(tech) + delta))
        self.counts[tech].set(str(c))
        self._refresh_row(tech)
        self._recompute()

    def _toggle(self, tech):
        # click the name: 0 <-> 1
        self.counts[tech].set("0" if self._count(tech) > 0 else "1")
        self._refresh_row(tech)
        self._recompute()

    def _recompute(self):
        components = {t: self._count(t) for t in self.counts
                      if self._count(t) > 0}
        dtype, dconfig = L.classify_driver(components)
        label = "Driver Type: {}      Config: {}".format(dtype or "(unknown/unverified)",
                                                          dconfig or "(none)")
        self.result_label.configure(text=label)
        if self.on_change:
            self.on_change(dtype, dconfig)

    def get(self):
        components = {t: self._count(t) for t in self.counts
                      if self._count(t) > 0}
        return L.classify_driver(components)

    def set(self, driver_type, driver_config):
        parsed = L.parse_driver_config(driver_config)
        for tech in self.counts:
            self.counts[tech].set(str(parsed.get(tech, 0)))
            self._refresh_row(tech)
        self._recompute()

    def clear(self):
        self.set("", "")


# ---------------------------------------------------------------------------
# TAG SELECTOR PANEL
# ---------------------------------------------------------------------------
class TagSelectorPanel(ttk.Frame):
    def __init__(self, parent, on_change=None, fr_provider=None):
        # borderless card surface: the make_card() wrapper supplies the
        # 1px border + offset shadow (Card.TFrame here would double it)
        super().__init__(parent, style="CardFlat.TFrame")
        self.on_change = on_change
        self.fr_provider = fr_provider   # callable -> (suggestions, info_text)
        self.vars = {tag: tk.BooleanVar(value=False) for tag in L.APPROVED_TAGS}
        self.current_price = 0
        self._auto_tier_tag = "Budget"
        self._suggestions = []
        self._cbs = {}            # tag -> ttk.Checkbutton (conflict dimming)
        self._emoji_labels = {}   # tag -> emoji label or None

        ttk.Label(self, text="TAGS  (pick 4-12 total)", style="CardHeader.TLabel").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        self.count_label = ttk.Label(self, text="0 / 12 selected", style="Card.TLabel",
                                      foreground=theme.TEXT_DIM)
        self.count_label.grid(row=0, column=1, sticky="e", padx=8)

        self.suggest_btn = ttk.Button(
            self, text="\u26a1 Suggest from FR Data",
            command=self._run_fr_suggestions,
            style="Blue.TButton")
        if fr_provider is None:
            self.suggest_btn.configure(state="disabled")
        self.suggest_btn.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 4))

        self.emoji_font = theme.pick_emoji_font()

        # groups start below the suggestion button row
        r = 2
        for group_name, tags in L.TAG_GROUPS.items():
            ttk.Label(self, text=group_name, style="Card.TLabel", foreground=theme.ACCENT_BLUE,
                      font=theme.font(12, "bold")).grid(
                row=r, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
            r += 1
            if group_name.startswith("Price Tier"):
                self.tier_label = ttk.Label(self, text="Auto: Budget ($0-99)",
                                             style="Card.TLabel", foreground=theme.ACCENT_ORANGE)
                self.tier_label.grid(row=r, column=0, columnspan=2, sticky="w", padx=16)
                r += 1
                continue
            # flat 4-column grid: emoji + checkbox + name packed tight so a
            # whole group fits on one or two rows (no boxes, no dividers).
            # Icons are COLOR emoji PNGs rendered from Segoe UI Emoji
            # (Tk's text emoji are monochrome); falls back to the plain
            # text glyph when color rendering is unavailable.
            col_frame = ttk.Frame(self, style="CardFlat.TFrame")
            col_frame.grid(row=r, column=0, columnspan=2, sticky="w", padx=12)
            r += 1
            # alphabetical within each group for faster scanning
            for i, tag in enumerate(sorted(tags)):
                cell = ttk.Frame(col_frame, style="CardFlat.TFrame")
                cell.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 10),
                          pady=1)
                emoji = TAG_EMOJI.get(tag)
                e_label = None
                if emoji:
                    photo = theme.emoji_photo(emoji, 16)
                    if photo is not None:
                        e_label = ttk.Label(cell, image=photo,
                                             style="Card.TLabel",
                                             cursor="hand2")
                        e_label.image = photo
                    elif self.emoji_font:
                        e_label = ttk.Label(cell, text=emoji,
                                             font=(self.emoji_font, 10),
                                             background=theme.BG_CARD,
                                             cursor="hand2")
                if e_label is not None:
                    e_label.pack(side="left")
                cb = ttk.Checkbutton(cell, text=tag, variable=self.vars[tag],
                                      style="Card.TCheckbutton",
                                      command=lambda t=tag: self._on_toggle(t))
                cb.pack(side="left")
                if e_label is not None:
                    # clicking the emoji toggles the checkbox too
                    e_label.bind("<Button-1>", lambda _e, c=cb: c.invoke())
                self._cbs[tag] = cb
                self._emoji_labels[tag] = e_label

        # FR-analysis suggestion strip (populated by "Suggest from FR Data")
        r += 1
        self.fr_status = ttk.Label(self, text="", style="Card.TLabel",
                                    foreground=theme.TEXT_DIM,
                                    justify="left")
        self.fr_status.grid(row=r, column=0, columnspan=2, sticky="w", padx=8)
        theme.bind_dynamic_wrap(self.fr_status, source=self)
        r += 1
        self.fr_chips = ttk.Frame(self, style="CardFlat.TFrame")
        self.fr_chips.grid(row=r, column=0, columnspan=2, sticky="w", padx=12,
                            pady=(2, 8))

        # conflict dimming must follow live theme switches (colors are
        # module-level and mutate on retheme)
        theme.add_retheme_hook(self._refresh_conflict_hints)
        self._refresh_conflict_hints()

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
        self._refresh_conflict_hints()
        if self.on_change:
            self.on_change()

    def _selected_set(self):
        return {t for t, v in self.vars.items() if v.get()}

    def _refresh_conflict_hints(self):
        """Inline guardrail: dim every UNCHECKED tag that would create a
        forbidden pair / second primary tonality if checked, so the picker
        communicates conflicts before the click instead of via a warning
        dialog after it. Selected tags always render normal.

        Dimming swaps to the Card.Dim.TCheckbutton style (ttk checkbuttons
        carry no widget-level foreground)."""
        selected = self._selected_set()
        for tag, cb in self._cbs.items():
            if self.vars[tag].get():
                dim = False
            else:
                candidate = set(selected)
                candidate.add(tag)
                dim = bool(L.tag_conflicts(candidate))
            want = "Card.Dim.TCheckbutton" if dim else "Card.TCheckbutton"
            try:
                if str(cb.cget("style")) != want:
                    cb.configure(style=want)
            except Exception:
                continue
            e_lbl = self._emoji_labels.get(tag)
            if e_lbl is not None and not e_lbl.cget("image"):
                # text-glyph fallback labels carry a foreground; image
                # labels do not
                try:
                    e_lbl.configure(foreground=theme.TEXT_DIM if dim
                                    else theme.TEXT_MAIN)
                except Exception:
                    pass

    def _refresh_count(self):
        n = len(self._selected_set()) + 1  # +1 for automatic price tier tag
        self.count_label.configure(text="{} / {} selected".format(n, L.MAX_TAGS))
        if n < L.MIN_TAGS:
            self.count_label.configure(foreground=theme.ACCENT_RED)
        elif n > L.MAX_TAGS:
            self.count_label.configure(foreground=theme.ACCENT_RED)
        else:
            self.count_label.configure(foreground=theme.ACCENT_GREEN)

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
        self._refresh_conflict_hints()

    def clear(self):
        for var in self.vars.values():
            var.set(False)
        self._refresh_count()
        self._refresh_conflict_hints()

    def destroy(self):
        try:
            theme.remove_retheme_hook(self._refresh_conflict_hints)
        except Exception:
            pass
        super().destroy()


# ---------------------------------------------------------------------------
# FILE LINKER PANEL
# ---------------------------------------------------------------------------
class FileLinkerPanel(ttk.Frame):
    def __init__(self, parent, get_data_root):
        # borderless card surface: the make_card() wrapper supplies the
        # 1px border + offset shadow
        super().__init__(parent, style="CardFlat.TFrame")
        self.get_data_root = get_data_root
        self.linked = []
        self._all_files_cache = None
        self._cache_root = None
        # optional callbacks fired on list selection changes (used by the
        # FR preview: linked selection -> emphasis, available -> ghost)
        self.on_linked_select = None
        self.on_available_select = None
        # optional callback(n_linked) fired whenever the linked list changes;
        # used by MainApp to badge the Editor tab so auto-link feedback is
        # visible from any notebook tab.
        self.on_files_changed = None

        header = ttk.Frame(self, style="CardFlat.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 4))
        ttk.Label(header, text="MEASUREMENT FILES (.txt)",
                  style="CardHeader.TLabel").pack(side="left")
        self.linked_count_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.linked_count_var,
                  style="Card.TLabel", foreground=theme.ACCENT_GREEN).pack(
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

        avail_frame = ttk.Frame(self, style="CardFlat.TFrame")
        avail_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        avail_frame.rowconfigure(0, weight=1)
        avail_frame.columnconfigure(0, weight=1)
        self.available_list = tk.Listbox(avail_frame, background=theme.BG_INPUT, foreground=theme.TEXT_MAIN,
                                          selectbackground=theme.ACCENT_BLUE, selectmode="extended",
                                          height=8, width=28, exportselection=False,
                                          font=theme.font(13))
        self.available_list.grid(row=0, column=0, sticky="nsew")
        avail_scroll = ttk.Scrollbar(avail_frame, orient="vertical",
                                      command=self.available_list.yview)
        avail_scroll.grid(row=0, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=avail_scroll.set)
        # no horizontal scrollbar: clipped paths marquee when selected
        attach_touch_scroll(self.available_list)
        # Mouse wheel scrolls the list even without clicking into it first.
        self.available_list.bind("<MouseWheel>", self._on_mousewheel_available)
        self.available_list.bind("<Button-4>", self._on_mousewheel_available)
        self.available_list.bind("<Button-5>", self._on_mousewheel_available)

        btns = ttk.Frame(self, style="CardFlat.TFrame")
        btns.grid(row=3, column=1, sticky="ns")
        # width is in characters, not pixels -- 8 was one short of fitting
        # "<< Remove" (9 chars), which is what clipped it to "<< Remov".
        ttk.Button(btns, text="Add >>", command=self._add_selected, width=10).pack(pady=4)
        ttk.Button(btns, text="<< Remove", command=self._remove_selected, width=10).pack(pady=4)
        ttk.Button(btns, text="Refresh", command=self._invalidate_cache, width=10).pack(pady=4)

        linked_frame = ttk.Frame(self, style="CardFlat.TFrame")
        linked_frame.grid(row=3, column=2, sticky="nsew", padx=8, pady=4)
        linked_frame.rowconfigure(0, weight=1)
        linked_frame.columnconfigure(0, weight=1)
        self.linked_list = tk.Listbox(linked_frame, background=theme.BG_INPUT, foreground=theme.TEXT_MAIN,
                                       selectbackground=theme.ACCENT_BLUE, selectmode="extended",
                                       height=8, width=28, exportselection=False,
                                       font=theme.font(13))
        self.linked_list.grid(row=0, column=0, sticky="nsew")
        linked_scroll = ttk.Scrollbar(linked_frame, orient="vertical",
                                       command=self.linked_list.yview)
        linked_scroll.grid(row=0, column=1, sticky="ns")
        self.linked_list.configure(yscrollcommand=linked_scroll.set)
        # no horizontal scrollbar: clipped paths marquee when selected
        attach_touch_scroll(self.linked_list)
        self.linked_list.bind("<MouseWheel>", self._on_mousewheel_linked)
        self.linked_list.bind("<Button-4>", self._on_mousewheel_linked)
        self.linked_list.bind("<Button-5>", self._on_mousewheel_linked)

        # selection hooks for the FR preview (arrow-key browsing included:
        # tk Listbox fires <<ListboxSelect>> on keyboard navigation too)
        self.available_list.bind(
            "<<ListboxSelect>>",
            lambda e: self.on_available_select and self._fire(self.on_available_select),
            add="+")
        self.linked_list.bind(
            "<<ListboxSelect>>",
            lambda e: self.on_linked_select and self._fire(self.on_linked_select),
            add="+")

        # ellipsization state: full strings kept for tooltips / re-widen
        self._available_full = []
        self._linked_full = []
        self._avail_shown_n = 0     # real rows currently rendered (footer excluded)
        self._linked_shown_n = 0
        self._avail_cap = None
        self._linked_cap = None
        self._cap_after = None
        for lb in (self.available_list, self.linked_list):
            lb.bind("<Configure>", self._schedule_caps, add="+")
        HoverTooltip(self.available_list,
                     lambda x, y: self._full_text_at(
                         self.available_list, self._available_full, y,
                         shown_n=getattr(self, "_avail_shown_n", None)))
        HoverTooltip(self.linked_list,
                     lambda x, y: self._full_text_at(
                         self.linked_list, self._linked_full, y))

        # marquee: the selected path auto-scrolls when too long for the box
        # (there are no horizontal scrollbars in this app)
        self._list_marquee_after = None
        self._list_marquee_pos = {}
        self.available_list.bind("<<ListboxSelect>>",
                                 lambda _e: self._start_list_marquee(), add="+")
        self.linked_list.bind("<<ListboxSelect>>",
                              lambda _e: self._start_list_marquee(), add="+")

        # data-folder watcher state + lazy start
        self._last_walk = 0.0
        self._walk_thread = None
        self._poll_after = None
        self.after(1500, self._auto_poll)

        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(2, weight=1)

        self.hint = ttk.Label(self, text="", style="Card.TLabel", foreground=theme.TEXT_DIM,
                               wraplength=380)
        self.hint.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        # keep the wraplength tied to the card's actual width instead of a
        # fixed guess, so a long hint never pushes the card wider than the
        # tab and spills text past the window edge on a narrow layout.
        self.bind("<Configure>", lambda e: self.hint.configure(
            wraplength=max(120, e.width - 16)), add="+")

    def _fire(self, cb):
        """Run an optional callback, never letting a plot hiccup break lists."""
        try:
            cb()
        except Exception:
            pass

    def _invalidate_cache(self):
        """Mark the scan stale and refresh through the background walker.
        Never walks synchronously: an 11k-file folder takes ~0.2 s warm and
        seconds cold, which froze the UI thread when called from search
        typing or right after a conversion."""
        self._all_files_cache = None
        self._cache_root = None
        self.poll_now(force=True)

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
        # Unknown or stale: never walk on the UI thread. Show the last known
        # snapshot (if any) and let the background walker refresh the list.
        self.poll_now(force=True)
        if self._all_files_cache is not None and self._cache_root == root:
            return self._all_files_cache
        try:
            self.hint.configure(text="Scanning data folder\u2026")
        except Exception:  # noqa: BLE001 - widget may be gone
            pass
        return []

    # Rendering cap for the Available list: an 11k-file data folder used
    # to be inserted into the Listbox row-by-row on EVERY entry switch
    # (~190 ms of UI stall per click). The listbox has no virtualization,
    # so we render the first matches plus a footer pointing at search.
    RENDER_CAP_ROWS = 400

    def _refresh_available(self):
        query = self.search_var.get().strip().lower()
        self.available_list.delete(0, tk.END)
        linked_set = set(self.linked)
        all_files = self._all_files()
        kept = [rel for rel in all_files
                if rel not in linked_set
                and (not query or query in rel.lower())]
        self._available_full = kept
        shown = kept[:self.RENDER_CAP_ROWS]
        footer = None
        if len(kept) > len(shown):
            footer = "... and {:,} more -- type above to narrow the list".format(
                len(kept) - len(shown))
        self._avail_shown_n = len(shown)
        self._render_truncated(self.available_list, shown, self._avail_cap,
                               footer=footer)

    def _refresh_linked(self):
        self._linked_full = list(self.linked)
        self._linked_shown_n = len(self._linked_full)
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

    def _render_truncated(self, lb, items, cap, footer=None):
        y0 = lb.yview()[0]                     # keep scroll position stable
        sel = set(lb.curselection())
        lb.delete(0, tk.END)
        for n, it in enumerate(items):
            lb.insert(tk.END, ellipsize_path(it, cap) if cap else it)
        if footer:
            # dimmed pseudo-row (never selectable for linking -- see the
            # _avail_shown_n guards in selection/marquee/tooltip paths)
            lb.insert(tk.END, footer)
            lb.itemconfigure(tk.END, foreground=theme.TEXT_DIM)
        try:
            lb.yview_moveto(y0)
            for n in sorted(sel & set(range(len(items)))):
                lb.selection_set(n)
        except Exception:
            pass

    def _full_text_at(self, lb, full_items, y, shown_n=None):
        idx = lb.nearest(y)
        limit = len(full_items) if shown_n is None else min(shown_n,
                                                            len(full_items))
        if 0 <= idx < limit:
            return full_items[idx]
        return ""

    # -- marquee for clipped measurement paths -----------------------------
    LIST_MARQUEE_TICK_MS = 160

    def _start_list_marquee(self):
        if self._list_marquee_after:
            try:
                self.after_cancel(self._list_marquee_after)
            except Exception:
                pass
        self._list_marquee_after = None
        self._list_marquee_pos = {}
        self._list_marquee_tick()

    def _list_marquee_tick(self):
        scrolling = False
        for key, lb, full_list, shown_n in (
                ("avail", self.available_list, self._available_full,
                 getattr(self, "_avail_shown_n", 0)),
                ("linked", self.linked_list, self._linked_full,
                 getattr(self, "_linked_shown_n",
                         len(self._linked_full or [])))):
            sel = lb.curselection()
            if not sel:
                continue
            idx = sel[0]
            if idx >= shown_n or idx >= len(full_list):
                continue                    # footer row / out of range
            full = full_list[idx]
            try:
                disp = lb.get(idx)
            except Exception:
                continue
            if not disp or disp == full:
                continue                    # fits: nothing to scroll
            budget = len(disp)
            pos = self._list_marquee_pos.get(key, 0)
            pos = (pos + 1) % max(1, len(full) + 4)
            self._list_marquee_pos[key] = pos
            window = full[pos:]
            if len(window) < budget:        # loop gap before wrapping
                window = window + "     " + full
            lb.delete(idx)
            lb.insert(idx, window[:budget])
            lb.selection_set(idx)
            scrolling = True
        if scrolling:
            self._list_marquee_after = self.after(
                self.LIST_MARQUEE_TICK_MS, self._list_marquee_tick)
        else:
            self._list_marquee_after = None

    def _add_selected(self):
        # Resolve from _available_full (the untruncated strings): the listbox
        # shows ellipsized text when paths overflow the widget width, and
        # storing that display text would corrupt entry["files"] with paths
        # that do not exist on disk. Indexes are positionally aligned; the
        # trailing "... and N more" footer row (index >= _avail_shown_n) is
        # never linkable.
        for i in self.available_list.curselection():
            if 0 <= i < min(len(self._available_full),
                            self._avail_shown_n):
                rel = self._available_full[i]
            else:                       # footer / defensive fallback
                continue
            if rel not in self.linked:
                self.linked.append(rel)
        self._refresh_linked()
        self._refresh_available()
        if self.on_linked_select:
            self._fire(self.on_linked_select)

    def _remove_selected(self):
        for i in reversed(self.linked_list.curselection()):
            del self.linked[i]
        self._refresh_linked()
        self._refresh_available()
        if self.on_linked_select:
            self._fire(self.on_linked_select)

    def get_files(self):
        return list(self.linked)

    def set_files(self, files):
        self.linked = list(files or [])
        self._refresh_linked()
        self._refresh_available()
        if self.on_linked_select:
            self._fire(self.on_linked_select)

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
        # Keep the previous snapshot visible while scanning (stale-until-
        # fresh); clearing it here used to push callers of _all_files() onto
        # a synchronous fallback walk on the UI thread.
        prev = tuple(self._all_files_cache or ()) \
            if self._cache_root == root else None

        result = {}

        def _compute():
            try:
                result["cur"] = self._scan_data_dir(root)
            except Exception:  # noqa: BLE001 - treat as "no change"
                result["cur"] = ([], None)

        th = threading.Thread(target=_compute, daemon=True, name="file-walk")

        def _apply():
            res, data_dir = result.get("cur", ([], None))
            if self.get_data_root() != root:
                return                      # user switched folder mid-walk
            cur = tuple(res or [])
            self._all_files_cache = list(cur)
            self._cache_root = root
            changed = prev is None or cur != prev
            if changed:
                self._refresh_available()
            try:
                if data_dir is None:
                    self.hint.configure(
                        text="No 'data' subfolder found under {}. "
                             "Use File > Set Data Folder...".format(root))
                elif changed or not cur:
                    self.hint.configure(
                        text="{} .txt files found under {}".format(len(cur), data_dir))
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
                 on_change=None, width=22, button_icon_for=None, **kw):
        # borderless: sits INSIDE the specs card (the card supplies borders)
        super().__init__(parent, style="CardFlat.TFrame")
        self.icon_for = icon_for            # callable(value) -> PhotoImage|None
        self.button_icon_for = button_icon_for
        self.textvariable = textvariable
        self.on_change = on_change
        self.values = []
        self._locked = False
        self._btn_image = None
        self.button = ttk.Menubutton(self, textvariable=textvariable,
                                      direction="flush", width=width)
        self.button.pack(fill="x", ipady=2)
        self.menu = tk.Menu(self.button, tearoff=0,
                            background=theme.BG_CARD, foreground=theme.TEXT_MAIN,
                            activebackground=theme.ACCENT_BLUE,
                            activeforeground=theme.contrast_text(theme.ACCENT_BLUE),
                            font=theme.font(13), borderwidth=1,
                            relief="solid")
        # NOT attached to the button: attaching lets the WM place the
        # popdown wherever it likes (offset / mis-sized on Windows). We post
        # it ourselves exactly below the button, left edges aligned, so the
        # dropdown lines up like a real combobox.
        self.button.bind("<Button-1>", self._post_menu)
        self.set_values(values)

    def _post_menu(self, _event=None):
        if self._locked:
            return
        self.button.update_idletasks()
        x = self.button.winfo_rootx()
        y = self.button.winfo_rooty() + self.button.winfo_height()
        try:
            self.menu.tk_popup(x, y)
        except Exception:
            self.menu.post(x, y)
        return "break"

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
        self._refresh_button_icon()

    def _refresh_button_icon(self):
        """Show the selected value's emoji on the closed button too."""
        if self.button_icon_for is None:
            return
        icon = self.button_icon_for(self.textvariable.get())
        if icon is not None:
            self._btn_image = icon     # keep a reference (GC)
            self.button.configure(image=icon, compound="left")
        else:
            self.button.configure(image=None, compound="none")

    def _choose(self, value):
        if self._locked:
            return
        if value != self.textvariable.get():
            self.textvariable.set(value)
            self._refresh_button_icon()
            if self.on_change:
                self.on_change()

    def get(self):
        return self.textvariable.get()

    def set(self, value):
        if value in self.values:
            self.textvariable.set(value)
            self._refresh_button_icon()

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
        canvas = tk.Canvas(self, background=theme.BG_MAIN, highlightthickness=0)
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
        card_outer, card = make_card(inner)
        card_outer.pack(fill="x", padx=10, pady=8)
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

        ttk.Label(card, text="Auto-generated ID:", style="Card.TLabel", foreground=theme.TEXT_DIM).grid(
            row=3, column=0, sticky="w", padx=8)
        self.id_var = tk.StringVar(value="")
        id_label = ttk.Label(card, textvariable=self.id_var, style="Card.TLabel",
                              foreground=theme.ACCENT_GREEN, font=theme.font(13, "bold"))
        id_label.grid(row=3, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        # ---- specs card (responsive: numeric fields lay out 4-across on
        # wide windows and 2x2 on narrow ones -- see _apply_spec_layout) ----
        specs_outer, specs = make_card(inner)
        specs_outer.pack(fill="x", padx=10, pady=8)
        ttk.Label(specs, text="SPECIFICATIONS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(8, 4))
        for col in range(6):
            specs.columnconfigure(col, weight=1, uniform="speccol")
        self._specs = specs

        def _spec_label(text):
            return ttk.Label(specs, text=text, style="Card.TLabel")

        self.year_var = tk.StringVar(value="0")
        self.price_var = tk.StringVar(value="0")
        self.impedance_var = tk.StringVar(value="0")
        self.sensitivity_var = tk.StringVar(value="0")

        year_lbl = _spec_label("Year")
        self.year_entry = ttk.Entry(specs, textvariable=self.year_var, width=8)
        self.year_entry.bind("<FocusOut>", self._validate_year)
        attach_entry_context_menu(self.year_entry)

        price_lbl = _spec_label("Price USD")
        self.price_entry = ttk.Entry(specs, textvariable=self.price_var, width=8)
        self.price_entry.bind("<FocusOut>", self._validate_price)
        attach_entry_context_menu(self.price_entry)

        imp_lbl = _spec_label("Impedance \u03A9")
        self.impedance_entry = ttk.Entry(specs, textvariable=self.impedance_var, width=8)
        attach_entry_context_menu(self.impedance_entry)

        sen_lbl = _spec_label("Sensitivity dB")
        self.sensitivity_entry = ttk.Entry(specs, textvariable=self.sensitivity_var, width=8)
        attach_entry_context_menu(self.sensitivity_entry)

        self.year_hint = ttk.Label(specs, text="", style="Card.TLabel", foreground=theme.ACCENT_RED)
        self.price_hint = ttk.Label(specs, text="", style="Card.TLabel", foreground=theme.ACCENT_ORANGE)

        ff_lbl = _spec_label("Form Factor")
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

        conn_lbl = _spec_label("Connector")
        self.connector_var = tk.StringVar(value="")
        self.connector_picker = IconCombobox(
            specs, L.FORM_CONNECTOR_MAP[L.FORM_FACTORS[0]],
            lambda v: ICONS.get(L.CONNECTOR_ICON.get(v, "")),
            self.connector_var, width=12)

        self.spec_hint = ttk.Label(specs, text="", style="Card.TLabel",
                                    foreground=theme.ACCENT_ORANGE, wraplength=260,
                                    justify="left")

        # widget refs used by the responsive re-layout
        self._spec_widgets = {
            "year": (year_lbl, self.year_entry),
            "price": (price_lbl, self.price_entry),
            "imp": (imp_lbl, self.impedance_entry),
            "sens": (sen_lbl, self.sensitivity_entry),
        }
        self._spec_dropdowns = ((ff_lbl, self.form_picker),
                                (conn_lbl, self.connector_picker))
        self._specs_narrow = None
        self._apply_spec_layout(False)

        # ---- driver config ----
        dp_outer, dp_card = make_card(inner)
        self.driver_panel = DriverConfigPanel(dp_card)
        self.driver_panel.pack(fill="both", expand=True)
        dp_outer.pack(fill="x", padx=10, pady=8)

        # ---- tags ----
        tg_outer, tg_card = make_card(inner)
        self.tag_panel = TagSelectorPanel(tg_card, fr_provider=self._fr_suggestions)
        self.tag_panel.pack(fill="both", expand=True)
        tg_outer.pack(fill="x", padx=10, pady=8)

        # ---- FR preview (live curves above the file linker) ----
        fr_outer, fr_card = make_card(inner)
        fr_outer.pack(fill="x", padx=10, pady=8)
        fr_head = ttk.Frame(fr_card, style="CardFlat.TFrame")
        fr_head.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(fr_head, text="\U0001F4C8  FR PREVIEW",
                  style="CardHeader.TLabel").pack(side="left")
        # color-chip legend (rebuilt on every refresh; duplicate basenames
        # get their source folder prefixed so curves are tellable apart)
        self.fr_legend = ttk.Frame(fr_head, style="CardFlat.TFrame")
        self.fr_legend.pack(side="left", padx=(12, 0))
        # Toggle buttons (checkbox drawn as a flat accent button):
        #   Show All    - draw every linked curve, one color each
        #   Average All - draw the averaged curve of ALL linked files
        # Both grey out while fewer than 2 files are linked.
        self.fr_avg_var = tk.BooleanVar(value=False)
        self.fr_avg_btn = ttk.Checkbutton(
            fr_head, text="Average All", style="Toggle.TCheckbutton",
            variable=self.fr_avg_var, command=self._refresh_fr_plot)
        self.fr_avg_btn.pack(side="right", padx=(0, 2))
        self.fr_show_all_var = tk.BooleanVar(value=True)
        self.fr_show_all_btn = ttk.Checkbutton(
            fr_head, text="Show All", style="Toggle.TCheckbutton",
            variable=self.fr_show_all_var, command=self._refresh_fr_plot)
        self.fr_show_all_btn.pack(side="right", padx=(0, 8))
        self.fr_plot = fr_plot.CurvePlot(fr_card, height=230)
        self.fr_plot.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        # ---- files ----
        fl_outer, fl_card = make_card(inner)
        self.file_panel = FileLinkerPanel(fl_card, self.app.get_data_root)
        self.file_panel.pack(fill="both", expand=True)
        fl_outer.pack(fill="x", padx=10, pady=8)
        # plot follows list interactions: linked selection -> emphasis,
        # available browsing -> dashed ghost preview of the unlinked file;
        # linked-count changes grey in/out the Show All / Average toggles
        self.file_panel.on_linked_select = self._refresh_fr_plot
        self.file_panel.on_available_select = self._refresh_fr_plot
        self.file_panel.on_files_changed = self._update_fr_buttons

        # ---- action buttons ----
        actions = ttk.Frame(inner, style="TFrame")
        actions.pack(fill="x", padx=10, pady=(4, 20))
        ttk.Button(actions, text="Save Entry", style="Accent.TButton",
                   command=self._on_save).pack(side="left", padx=4)
        self.save_btn = actions.winfo_children()[-1]
        ttk.Button(actions, text="Clear / New", command=self.new_entry).pack(side="left", padx=4)
        self.validation_label = ttk.Label(actions, text="", style="TLabel", foreground=theme.ACCENT_RED,
                                           justify="left")
        self.validation_label.pack(side="left", padx=12)
        # account for the two buttons already sitting in this row when
        # computing how much width is left for the message.
        theme.bind_dynamic_wrap(self.validation_label, source=actions, pad=240)

        # touch-style drag panning for the whole editor form (passive areas)
        attach_touch_scroll_canvas(canvas, inner)

        # Enter moves to the next field (Brand -> Model -> Variant -> specs
        # -> Connector). Disabled fields (TWS-locked impedance/sensitivity)
        # are skipped. AutocompleteEntry's own Return handler runs first,
        # so an open suggestion popup still consumes the key.
        chain = [self.brand_entry.entry, self.model_entry.entry,
                 self.variant_entry.entry, self.year_entry, self.price_entry,
                 self.impedance_entry, self.sensitivity_entry,
                 self.connector_picker.button, self.save_btn]
        for cur, nxt in zip(chain, chain[1:]):
            pos = chain.index(nxt)
            cur.bind("<Return>",
                     self._make_focus_next(chain[pos:]), add="+")

        # responsive layout: switch specs to 2x2 and driver rows to a single
        # column when the form gets too narrow for the wide layout
        self._resp_after = None
        canvas.bind("<Configure>", self._schedule_responsive, add="+")

        self._on_form_change()

    @staticmethod
    def _make_focus_next(candidates):
        """Return an event handler focusing the first enabled widget among
        `candidates` (used by the Enter-to-next-field chain)."""
        def _handler(_event=None):
            for w in candidates:
                try:
                    if str(w.cget("state")) != "disabled":
                        w.focus_set()
                        return "break"
                except Exception:
                    continue
            return "break"
        return _handler

    # -- responsive layout -------------------------------------------------
    def _schedule_responsive(self, event=None):
        if self._resp_after is not None:
            try:
                self.after_cancel(self._resp_after)
            except Exception:
                pass
        width = event.width if event is not None else self.winfo_width()
        self._resp_after = self.after(120, lambda: self._responsive(width))

    def _wide_specs_min_width(self):
        """Minimum card width the 4-across spec row needs, measured from
        the ACTUAL current font instead of a guessed pixel constant -- a
        fixed number only happened to work for whichever font was active
        when it was written, and clipped labels the moment someone picked
        a wider bundled font."""
        import tkinter.font as tkfont
        fm = tkfont.Font(font=theme.font(13))
        longest = max(fm.measure(t) for t in
                      ("Sensitivity dB", "Impedance \u03A9", "Price USD", "Year"))
        # 4 field columns + 2 hint columns, ~16px padding each, plus a
        # small safety margin so wrapping never lands right on the edge
        return int((longest + 16) * 6 * 1.08) + 24

    def _responsive(self, width):
        self._resp_after = None
        narrow = max(200, width) < self._wide_specs_min_width()
        if narrow != self._specs_narrow:
            self._apply_spec_layout(narrow)
        # 2-across driver rows need ~780px; below that stack them
        self.driver_panel.set_columns(2 if width >= 780 else 1)

    def _grid_pair(self, key, row, col, span=1):
        lbl, ent = self._spec_widgets[key]
        lbl.grid(row=row, column=col, columnspan=span, sticky="w", padx=8)
        ent.grid(row=row + 1, column=col, columnspan=span, sticky="ew",
                 padx=8, pady=(0, 6))

    def _apply_spec_layout(self, narrow):
        """WIDE: Year/Price/Impedance/Sensitivity on one 4-column row.
        NARROW: 2x2 grid so no label ever clips. Dropdown rows and hints
        re-grid accordingly.

        Columns are reconfigured from scratch every call: the two modes
        use a different number of "live" columns (4 vs 6), and leaving
        the other mode's uniform group in place used to force 4-6 equal
        slices of the card no matter how many of them actually held a
        widget -- e.g. narrow mode used only columns 0 and 2 out of 6
        uniform columns, so each field got squeezed into 1/6 of the
        card's width and every label clipped ("Impedance..." -> "IMPEDAI").
        Resetting first, then only weighting the columns this mode
        actually uses, is what keeps that from happening again."""
        g = self._specs
        for col in range(6):
            g.columnconfigure(col, weight=0, uniform="")
        if narrow:
            # 2 fields per row, each spanning 2 of 4 live columns so the
            # full card width splits evenly between exactly two boxes.
            for col in range(4):
                g.columnconfigure(col, weight=1, uniform="specs_narrow")
            self._grid_pair("year", 1, 0, span=2)
            self._grid_pair("price", 1, 2, span=2)
            self._grid_pair("imp", 3, 0, span=2)
            self._grid_pair("sens", 3, 2, span=2)
            self.year_hint.grid(row=5, column=0, columnspan=2, sticky="w", padx=8)
            self.price_hint.grid(row=5, column=2, columnspan=2, sticky="w", padx=8)
            ff_lbl, ff_pick = self._spec_dropdowns[0]
            cn_lbl, cn_pick = self._spec_dropdowns[1]
            ff_lbl.grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))
            ff_pick.grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
            cn_lbl.grid(row=6, column=2, columnspan=2, sticky="w", padx=8, pady=(4, 0))
            cn_pick.grid(row=7, column=2, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
            self.spec_hint.grid(row=8, column=0, columnspan=4, sticky="w", padx=8,
                                pady=(0, 6))
            self.spec_hint.configure(wraplength=320)
        else:
            # 4 fields + a hint column, all 6 columns live and equal.
            for col in range(6):
                g.columnconfigure(col, weight=1, uniform="specs_wide")
            self._grid_pair("year", 1, 0)
            self._grid_pair("price", 1, 1)
            self._grid_pair("imp", 1, 2)
            self._grid_pair("sens", 1, 3)
            self.year_hint.grid(row=3, column=0, sticky="w", padx=8)
            self.price_hint.grid(row=3, column=1, sticky="w", padx=8)
            ff_lbl, ff_pick = self._spec_dropdowns[0]
            cn_lbl, cn_pick = self._spec_dropdowns[1]
            ff_lbl.grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))
            ff_pick.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
            cn_lbl.grid(row=4, column=2, columnspan=2, sticky="w", padx=8, pady=(4, 0))
            cn_pick.grid(row=5, column=2, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
            self.spec_hint.grid(row=5, column=4, columnspan=2, sticky="w", padx=8,
                                pady=(0, 6))
            self.spec_hint.configure(wraplength=260)
        self._specs_narrow = narrow

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
    # -- FR preview plotting -------------------------------------------------
    def _load_linked_curve(self, rel):
        """(abs_path, raw_points) for a linked rel-path, or (None, None)."""
        root_dir = self.app.get_data_root()
        if not root_dir or not rel:
            return None, None
        try:
            import fr_analysis as FA
            full = FA.resolve_under_root(root_dir, rel)
        except Exception:               # noqa: BLE001 - audit flags bad paths
            return None, None
        pts = fr_plot.get_curve_points(full)
        return full, pts

    def _refresh_fr_plot(self, *_args):
        linked = self.file_panel.get_files()
        sel = set(self.file_panel.linked_list.curselection())
        self._update_fr_buttons(len(linked))
        pal = fr_plot.palette()
        show_all = self.fr_show_all_var.get() and len(linked) >= 2
        avg_on = self.fr_avg_var.get() and len(linked) >= 2

        # Duplicate basenames ("7HZ ETERNAL.txt" x4) get their source
        # folder prefixed so the legend actually tells curves apart.
        base_counts = {}
        for rel in linked:
            base_counts[os.path.basename(rel)] = \
                base_counts.get(os.path.basename(rel), 0) + 1

        def disp_name(rel):
            base = os.path.basename(rel)
            if base_counts.get(base, 0) > 1:
                parts = rel.replace("\\", "/").split("/")
                return "/".join(parts[-2:]) if len(parts) >= 2 else base
            return base

        def load_norm(rel):
            _f, pts = self._load_linked_curve(rel)
            norm = fr_plot.normalized(pts) if pts else []
            return fr_plot.smooth_octaves(norm) if norm else []

        series = []
        chips = []          # (color, label) for the legend
        for i, rel in enumerate(linked):
            if not show_all:
                if sel and i not in sel:
                    continue
                if not sel and series:
                    continue             # no selection -> first curve only
            norm = load_norm(rel)
            if not norm:
                continue
            color = pal[i % len(pal)]
            if sel and i in sel:
                width = 5                # emphasized: thickest, full color
            elif sel:
                width = 3                # others recede while a file is picked
                color = fr_plot.dim(color, 0.5)
            else:
                width = 4
            name = disp_name(rel)
            series.append({"name": name, "pts": norm,
                           "color": color, "width": width})
            chips.append((color, name))
        # Average All: mean of ALL linked curves, drawn last (on top) as a
        # thick dashed near-white line -- the "consensus" curve.
        avg = None
        if avg_on:
            all_norms = [n for n in (load_norm(r) for r in linked) if n]
            if len(all_norms) >= 2:
                avg = fr_plot.average(all_norms)
                if avg:
                    series.append({"name": "Average", "pts": avg,
                                   "color": theme.TEXT_MAIN, "width": 3,
                                   "dash": (7, 4)})
                    chips.append((theme.TEXT_MAIN, "Average"))
        # ghost: browsing the Available list previews an unlinked file dashed
        av = self.file_panel.available_list.curselection()
        if av and 0 <= av[0] < min(len(self.file_panel._available_full),
                                   getattr(self.file_panel,
                                           "_avail_shown_n", 0)):
            ghost_rel = self.file_panel._available_full[av[0]]
            gnorm = load_norm(ghost_rel)
            if gnorm:
                series.insert(0, {"name": "~ " + os.path.basename(ghost_rel),
                                  "pts": gnorm, "color": theme.TEXT_DIM,
                                  "width": 2, "dash": (5, 3)})
                chips.insert(0, (theme.TEXT_DIM,
                                 "~ " + os.path.basename(ghost_rel)))
        self._set_fr_legend(chips)
        if not series:
            self.fr_plot.set_data(
                [], msg="Link a measurement file - or browse the Available "
                        "list - to preview its curve")
            return
        self.fr_plot.set_data(series, avg=None)   # avg already a series

    def _update_fr_buttons(self, n_linked):
        """Show All / Average All grey out while fewer than 2 files are
        linked (a single curve needs neither)."""
        multi = n_linked >= 2
        for btn in (getattr(self, "fr_show_all_btn", None),
                    getattr(self, "fr_avg_btn", None)):
            if btn is None:
                continue
            try:
                btn.state(["!disabled"] if multi else ["disabled"])
            except Exception:
                pass

    def _set_fr_legend(self, chips):
        """Rebuild the color-chip legend: [swatch label] per drawn curve."""
        for w in self.fr_legend.winfo_children():
            w.destroy()
        shown = chips[:6]
        for color, label in shown:
            chip = ttk.Frame(self.fr_legend, style="CardFlat.TFrame")
            chip.pack(side="left", padx=(0, 10))
            swatch = tk.Label(chip, text="  ", background=color,
                              highlightthickness=1,
                              highlightbackground=theme.BORDER)
            swatch.pack(side="left", padx=(0, 4))
            short = label if len(label) <= 28 else label[:27] + "\u2026"
            tk.Label(chip, text=short, background=theme.BG_CARD,
                     foreground=theme.TEXT_DIM,
                     font=theme.font(12)).pack(side="left")
        if len(chips) > len(shown):
            tk.Label(self.fr_legend, text="(+{})".format(len(chips) - 6),
                     background=theme.BG_CARD, foreground=theme.TEXT_DIM,
                     font=theme.font(12)).pack(side="left")

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
        self._capture_baseline()
        self._refresh_fr_plot()

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
        self._refresh_fr_plot()
        self.tag_panel.clear_suggestions()
        self.validation_label.configure(text="")
        self.year_hint.configure(text="")
        self.price_hint.configure(text="")
        self._capture_baseline()
        # TWS spec violations are surfaced by the audit engine's advisory
        # warning (code "tws-nonzero"); no per-load modal here.

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
# MERGE DUPLICATE ENTRIES DIALOG
# ---------------------------------------------------------------------------
_MERGE_FIELDS = [
    ("brand", "Brand"),
    ("model", "Model"),
    ("variant", "Variant"),
    ("year", "Year"),
    ("price_usd", "Price USD"),
    ("driver_type", "Driver Type"),
    ("driver_config", "Driver Config"),
    ("impedance", "Impedance"),
    ("sensitivity", "Sensitivity"),
    ("connector", "Connector"),
    ("form_factor", "Form Factor"),
    ("tags", "Tags"),
]


class MergeDialog(tk.Toplevel):
    """Side-by-side merge for a flagged duplicate pair: pick a winner per
    field (tags are picked per side too -- unioning two tag sets could
    create forbidden conflicts). The price-tier tag is stripped from the
    chosen set and re-added automatically to match the winning price.
    Measurement files always merge as a union. The surviving entry keeps
    the first entry's list position; its id is rebuilt from the winning
    Brand/Model/Variant, and the second entry is deleted."""

    def __init__(self, master, app, pos_a, pos_b):
        super().__init__(master)
        self.app = app
        self.pos_a, self.pos_b = pos_a, pos_b
        self.a = app.entries[pos_a]
        self.b = app.entries[pos_b]
        self.merged_id = None
        self.title("Merge Duplicate Entries")
        self.configure(background=theme.BG_MAIN)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        id_a = self.a.get("id") or "(no id)"
        id_b = self.b.get("id") or "(no id)"

        outer, card = make_card(self)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        ttk.Label(card, text="\u2694  MERGE DUPLICATE ENTRIES",
                  style="CardHeader.TLabel").pack(anchor="w", padx=8,
                                                  pady=(8, 2))
        ttk.Label(
            card,
            text="Pick a winner for each field -- including tags, so "
                 "conflicting tag sets can never sneak into the merge. The "
                 "price-tier tag follows the winning price automatically, "
                 "and measurement files from both entries are combined. The "
                 "entry that is not kept is deleted. Everything applies as "
                 "a single undoable step.",
            style="Card.TLabel", foreground=theme.TEXT_DIM,
            wraplength=660, justify="left").pack(anchor="w", padx=8,
                                                 pady=(0, 8))

        grid = ttk.Frame(card, style="CardFlat.TFrame")
        grid.pack(fill="x", padx=8)
        for c, w in ((0, 0), (1, 1), (2, 0), (3, 0), (4, 1)):
            grid.columnconfigure(c, weight=w)

        ttk.Label(grid, text="Field", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=0, sticky="w",
                                                  padx=(0, 8))
        ttk.Label(grid, text="A:  " + id_a, style="Card.TLabel",
                  foreground=theme.ACCENT_BLUE).grid(row=0, column=1,
                                                     sticky="w", padx=(0, 4))
        ttk.Label(grid, text="Keep", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=2,
                                                  columnspan=2, sticky="w")
        ttk.Label(grid, text="B:  " + id_b, style="Card.TLabel",
                  foreground=theme.ACCENT_BLUE).grid(row=0, column=4,
                                                     sticky="w", padx=(8, 0))

        self._vars = {}
        for r, (key, label) in enumerate(_MERGE_FIELDS, start=1):
            # differing fields pop (amber field name, full-brightness
            # values); identical fields recede so the eye lands on the
            # decisions that actually matter
            differs = self.a.get(key) != self.b.get(key)
            name_fg = theme.ACCENT_ORANGE if differs else theme.TEXT_DIM
            val_fg = theme.TEXT_MAIN if differs else theme.TEXT_DIM
            ttk.Label(grid, text=label, style="Card.TLabel",
                      foreground=name_fg).grid(
                row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            va, vb = self._fmt(self.a.get(key)), self._fmt(self.b.get(key))
            ttk.Label(grid, text=va or "\u2014",
                      style="Card.TLabel",
                      foreground=val_fg if va else theme.TEXT_DIM,
                      wraplength=250, justify="left").grid(
                row=r, column=1, sticky="w", padx=(0, 4), pady=2)
            var = tk.StringVar(
                value=self._default_side(key, self.a.get(key), self.b.get(key)))
            self._vars[key] = var
            for side, col in (("A", 2), ("B", 3)):
                ttk.Radiobutton(grid, text=side, value=side, variable=var,
                                style="Card.TRadiobutton").grid(
                    row=r, column=col, sticky="w", padx=(2, 2))
            ttk.Label(grid, text=vb or "\u2014",
                      style="Card.TLabel",
                      foreground=val_fg if vb else theme.TEXT_DIM,
                      wraplength=250, justify="left").grid(
                row=r, column=4, sticky="w", padx=(8, 0), pady=2)

        # measurement files always merge as a union
        self.files_union = list(dict.fromkeys(
            list(self.a.get("files") or []) + list(self.b.get("files") or [])))
        union_row = len(_MERGE_FIELDS) + 1
        ttk.Label(grid, text="Files", style="Card.TLabel").grid(
            row=union_row, column=0, sticky="w", padx=(0, 8), pady=(6, 2))
        ttk.Label(grid, text="{} + {} -> {} unique (combined)".format(
            len(self.a.get("files") or []), len(self.b.get("files") or []),
            len(self.files_union)), style="Card.TLabel",
            foreground=theme.ACCENT_GREEN).grid(row=union_row, column=1,
                                                columnspan=4, sticky="w")
        ttk.Label(grid, text="(price-tier tag follows the winning price)",
                  style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=union_row + 1, column=0,
                                                  columnspan=5, sticky="w",
                                                  pady=(4, 0))
        # legend for the color coding
        ttk.Label(grid, text="\u25cf differing fields highlighted",
                  style="Card.TLabel",
                  foreground=theme.ACCENT_ORANGE).grid(
            row=union_row + 2, column=0, columnspan=5, sticky="w",
            pady=(6, 0))

        self.status_lbl = ttk.Label(card, text="", style="Card.TLabel",
                                    foreground=theme.ACCENT_RED,
                                    wraplength=660, justify="left")
        self.status_lbl.pack(anchor="w", padx=8, pady=(8, 0))

        btns = ttk.Frame(card, style="CardFlat.TFrame")
        btns.pack(fill="x", padx=8, pady=(8, 10))
        ttk.Button(btns, text="Merge", style="Accent.TButton",
                   command=self._apply).pack(side="left")
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side="left", padx=8)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        self.minsize(760, 0)
        self.grab_set()

    @staticmethod
    def _fmt(value):
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value) if value not in (None, "") else ""

    @staticmethod
    def _default_side(key, va, vb):
        """Non-empty beats empty; non-zero beats 0 on numeric fields; the
        richer tag set wins on ties; everything else keeps A (the entry
        the audit row pointed at)."""
        if key == "tags":
            la = len(va) if isinstance(va, list) else 0
            lb = len(vb) if isinstance(vb, list) else 0
            return "A" if la >= lb else "B"
        if key in ("year", "price_usd", "impedance", "sensitivity"):
            try:
                na, nb = int(str(va or 0)), int(str(vb or 0))
            except (TypeError, ValueError):
                na, nb = 0, 0
            if na == 0 and nb != 0:
                return "B"
            return "A"
        sa, sb = str(va or "").strip(), str(vb or "").strip()
        if sa and not sb:
            return "A"
        if sb and not sa:
            return "B"
        return "A"

    def _apply(self):
        merged = dict(self.a)
        for key, var in self._vars.items():
            merged[key] = self.b.get(key) if var.get() == "B" \
                else self.a.get(key)
        # tags: the chosen side's set, with the price-tier tag stripped
        # and re-added to match the WINNING price (unioning two tag sets
        # could create forbidden conflicts or double tiers)
        chosen_tags = list(merged.get("tags") or [])
        merged["tags"] = [t for t in chosen_tags if t not in L.PRICE_TIER_TAGS]
        merged["tags"].append(L.price_tier_for(merged.get("price_usd", 0)))
        merged["files"] = list(self.files_union)
        merged["id"] = L.build_id(str(merged.get("brand") or ""),
                                  str(merged.get("model") or ""),
                                  str(merged.get("variant") or ""))
        if not merged["id"]:
            self.status_lbl.configure(
                text="Cannot build a valid id from the winning "
                     "Brand/Model/Variant.")
            return
        others = {e.get("id") for i, e in enumerate(self.app.entries)
                  if i not in (self.pos_a, self.pos_b) and e.get("id")}
        errors = L.validate_entry(merged, existing_ids=others)
        if errors:
            self.status_lbl.configure(
                text="Cannot merge:\n- " + "\n- ".join(errors))
            return

        app = self.app
        pos_a, pos_b = self.pos_a, self.pos_b
        a_obj, b_obj = app.entries[pos_a], app.entries[pos_b]
        # Whether the editor is holding one of the two merged entries must
        # be decided BEFORE the list is mutated: afterwards neither old
        # object is in the list (pos_a holds the new merged dict, pos_b is
        # gone), so the old content-equality check was effectively dead
        # and a deleted twin could leave stale form data loaded.
        editor_held = app.editing_index in (pos_a, pos_b)
        merged_final = L.build_clean_entry(merged)
        app.entries[pos_a] = merged_final
        del app.entries[pos_b]
        changes = [{
            "pos_hint": pos_a,
            "ref_before": a_obj, "copy_before": app._deepcopy(a_obj),
            "ref_after": merged_final, "copy_after": app._deepcopy(merged_final),
        }, {
            "pos_hint": pos_b,
            "ref_before": b_obj, "copy_before": app._deepcopy(b_obj),
            "ref_after": None, "copy_after": None,
        }]
        app._record_op("merge", "Merged '{}' into '{}' (duplicate repair)"
                       .format(b_obj.get("id") or "?",
                               merged_final.get("id") or "?"), changes)
        app.dirty = True
        app._mark_audit_dirty()
        app.populate_tree()
        # if the editor is holding one of the merged entries, follow it
        new_pos = app._find_slot(None, {"id": merged_final["id"]})
        if editor_held and new_pos >= 0:
            app.editing_index = new_pos
            app.editor.load_entry(app.entries[new_pos])
        app.refresh_spell_vocab()
        app._autosave()
        app.status_var.set("Merged '{}' and '{}' into '{}'.".format(
            a_obj.get("id") or "?", b_obj.get("id") or "?", merged_final["id"]))
        self.merged_id = merged_final["id"]
        self.destroy()


# ---------------------------------------------------------------------------
# AUDIT PANEL
# ---------------------------------------------------------------------------
class AuditPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.issues = []
        # exclusive filter: unticked = ignored issues hidden; ticked = ONLY
        # ignored issues shown (ready to review / un-ignore)
        self.show_ignored_only = tk.BooleanVar(value=False)
        self._row_issues = {}      # leaf iid  -> AuditIssue
        self._group_items = {}     # group iid -> [AuditIssue, ...]

        # primary actions as buttons; row-specific actions (Go to Entry,
        # Merge Duplicate, Ignore/Un-ignore) live in the right-click
        # context menu so the tab stays uncluttered
        top = ttk.Frame(self, style="TFrame")
        top.pack(fill="x", padx=10, pady=8)
        btn_row = ttk.Frame(top, style="TFrame")
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Run Audit", style="Accent.Compact.TButton",
                   command=self.app.run_audit).pack(side="left", padx=(3, 2))
        ttk.Button(btn_row, text="Fix Selected", style="Compact.TButton",
                   command=self._fix_selected).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Fix All", style="Blue.Compact.TButton",
                   command=self._fix_all).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Export Report", style="Compact.TButton",
                   command=self._export).pack(side="left", padx=2)

        opt_row = ttk.Frame(top, style="TFrame")
        opt_row.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(opt_row, text="Ignored only",
                        variable=self.show_ignored_only,
                        command=self.rerender).pack(side="left", padx=(4, 4))

        # summary lives on its own row: packed alongside the checkbox it
        # used to get squeezed out of view (or overlap it) on a narrow
        # pane, since a Label has no natural stopping point to wrap at.
        summary_row = ttk.Frame(top, style="TFrame")
        summary_row.pack(fill="x", pady=(4, 0))
        self.summary_label = ttk.Label(summary_row, text="No audit run yet.", style="TLabel")
        self.summary_label.pack(side="left", padx=4)
        theme.bind_dynamic_wrap(self.summary_label, source=top)

        # tree+headings: the #0 tree column carries the hierarchy (group
        # header = entry id with real expand/collapse arrows, leaves =
        # issue category), exactly like the brand tree in the left column.
        columns = ("message", "fixable")
        self._base_headings = {"message": "Issue", "fixable": "Fixable"}
        self._sort_col = None
        self._sort_desc = False
        tree_frame = ttk.Frame(self, style="TFrame")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree = ttk.Treeview(tree_frame, columns=columns,
                                 show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Entry / Category")
        for col in columns:
            self.tree.heading(col, text=self._base_headings[col],
                              command=lambda c=col: self._sort_by(c))
        # initial widths; _fit_columns redistributes proportionally on every
        # resize so ALL columns stay visible (nothing clips off-pane)
        self.tree.column("#0", width=240, stretch=True)
        for col, width in (("message", 520), ("fixable", 90)):
            self.tree.column(col, width=width, stretch=True,
                             anchor="center" if col == "fixable" else "w")
        self.tree.bind("<Configure>", self._fit_columns, add="+")
        # vertical only: clipped issue text shows in full via the tooltip
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        # NO touch-style drag panning here: this is a MULTI-SELECT list, and
        # the pan handler swallows drags past 5px, which would kill the
        # native click & drag (and Ctrl/Shift+click) row selection. The
        # scrollbar and mouse wheel cover scrolling. Ctrl+A selects all.
        self.tree.bind("<Control-a>", self._select_all_issues)
        self.tree.bind("<Control-A>", self._select_all_issues)
        # Mouse wheel scrolls the audit list even without clicking into it first.
        self.tree.bind("<MouseWheel>", self._on_mousewheel)
        self.tree.bind("<Button-4>", self._on_mousewheel)
        self.tree.bind("<Button-5>", self._on_mousewheel)
        HoverTooltip(self.tree, self._issue_text_at)
        # right-click context menu (row-specific actions)
        self.tree.bind("<Button-3>", self._show_context_menu)
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._show_context_menu)

        self.tree.tag_configure("error", foreground=theme.ACCENT_RED)
        self.tree.tag_configure("warning", foreground=theme.ACCENT_ORANGE)
        self.tree.tag_configure("info", foreground=theme.TEXT_DIM)
        self.tree.tag_configure("waived", foreground=theme.TEXT_DIM)
        self.tree.bind("<Double-1>", self._on_issue_activate)
        self.tree.bind("<Return>", self._on_issue_activate)

    # column proportions (of the visible tree width): tree 26%,
    # issue 65%, fixable 9%, each with a usable floor
    _COL_FIT = (("#0", 0.26, 150), ("message", 0.65, 260), ("fixable", 0.09, 88))

    def _fit_columns(self, _event=None):
        """Redistribute column widths so the whole table always fits the
        pane -- no clipped AUTO-FIXABLE column at any window size."""
        w = self.tree.winfo_width() - 24      # scrollbar + border margin
        if w < 300:
            return
        for col, frac, floor in self._COL_FIT:
            self.tree.column(col, width=max(floor, int(w * frac)))

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

    def _issue_text_at(self, x, y):
        """Tooltip text for the row under the cursor: the full, untruncated
        issue description (the message column has no horizontal scrollbar)."""
        iid = self.tree.identify_row(y)
        if not iid:
            return None
        iss = self._row_issues.get(iid)
        if iss is not None:
            sev = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}.get(
                iss.severity, iss.severity.upper())
            return "[{}] {} \u00b7 {}\n{}".format(sev, iss.category,
                                                   iss.entry_id, iss.message)
        group = self._group_items.get(iid)
        if group:
            eid = group[0].entry_id if group else ""
            return "{}  ({} issue(s) -- double-click to jump)".format(
                eid, len(group))
        return None

    # ------------------------------------------------------------------
    # rendering (always grouped by entry, optionally sorted)
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
            return (str(getattr(i, col, "")).lower(),)
        return sorted(iss, key=key, reverse=self._sort_desc)

    def _waived(self, iss):
        return iss.waiver_key() in getattr(self.app, "waivers", set())

    def _display_issues(self):
        """Exclusive filter: normal mode shows active issues only;
        'Ignored only' shows ONLY the waived ones (review / un-ignore)."""
        iss = self._sorted_issues()
        if self.show_ignored_only.get():
            return [i for i in iss if self._waived(i)]
        return [i for i in iss if not self._waived(i)]

    def show_issues(self, issues):
        self.issues = list(issues)
        self.rerender()
        live = [i for i in issues if not self._waived(i)]
        hidden = len(issues) - len(live)
        errors = sum(1 for i in live if i.severity == "error")
        text = "{} Issue{} Found".format(len(live),
                                         "" if len(live) == 1 else "s")
        parts = []
        if errors:
            parts.append("{} Error{}".format(errors,
                                             "" if errors == 1 else "s"))
        if hidden:
            parts.append("{} Ignored".format(hidden))
        if parts:
            text += " ({})".format(", ".join(parts))
        self.summary_label.configure(text=text)
        # keep the tab badge in sync (also fires on Ignore/Un-ignore,
        # which re-call show_issues)
        try:
            self.app._update_audit_badge(len(live))
        except Exception:
            pass

    def rerender(self):
        """Render the current issue list grouped by entry. Grouping
        coalesces ALL issues sharing the same entry signature into one
        collapsible parent (regardless of adjacency after sorting);
        file-level rows (summaries / unlinked files) stay top-level.
        Display order follows the active column sort. In normal mode
        waived issues are hidden entirely; 'Ignored only' flips the
        filter to show just those (dim, with an [ignored] mark)."""
        # severity tag colors follow the live theme (rerender doubles as
        # the Audit tab's registered retheme hook)
        self.tree.tag_configure("error", foreground=theme.ACCENT_RED)
        self.tree.tag_configure("warning", foreground=theme.ACCENT_ORANGE)
        self.tree.tag_configure("info", foreground=theme.TEXT_DIM)
        self.tree.tag_configure("waived", foreground=theme.TEXT_DIM)
        self.tree.delete(*self.tree.get_children())
        self._row_issues.clear()
        self._group_items.clear()

        def is_standalone(iss):
            return (not isinstance(iss.entry_index, int) or iss.entry_index < 0
                    or iss.entry_id in ("(none)", "(summary)"))

        ordered = self._display_issues()

        def leaf_label(iss):
            cat = iss.category
            if self._waived(iss):
                cat += "  [ignored]"
            return cat

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
                    tags = (iss.severity, "waived") if self._waived(iss) \
                        else (iss.severity,)
                    label = iss.entry_id \
                        if iss.entry_id not in ("(none)", "(summary)") \
                        else iss.category
                    self.tree.insert("", "end", iid=iid, text=label,
                                     values=(iss.message,
                                             "Yes" if iss.fix else "No"),
                                     tags=tags, open=True)
                continue
            worst = min(items, key=lambda i: self._SEV_RANK.get(i.severity, 9))
            nfix = sum(1 for i in items if i.fix)
            gid = "g{}".format(seq); seq += 1
            msg = worst.message
            if len(msg) > 140:
                msg = msg[:139] + "\u2026"
            n_ign = sum(1 for i in items if self._waived(i))
            gcat = "{} issue(s)".format(len(items))
            if n_ign:
                gcat += "  [{} ignored]".format(n_ign)
            parent = self.tree.insert("", "end", iid=gid,
                                      text=worst.entry_id,
                                      values=(gcat + "  \u2014  " + msg,
                                              "{}/{} auto".format(nfix,
                                                                  len(items))),
                                      tags=(worst.severity,), open=False)
            self._group_items[parent] = items
            for iss in items:
                iid = "i{}".format(seq); seq += 1
                self._row_issues[iid] = iss
                tags = (iss.severity, "waived") if self._waived(iss) \
                    else (iss.severity,)
                self.tree.insert(parent, "end", iid=iid, text=leaf_label(iss),
                                 values=(iss.message,
                                         "Yes" if iss.fix else "No"),
                                 tags=tags)

    # ------------------------------------------------------------------
    # waiver actions
    # ------------------------------------------------------------------
    def _ignore_selected(self):
        chosen = self._issues_for_selection()
        if not chosen:
            return
        waived = getattr(self.app, "waivers", None)
        if waived is None:
            return
        added = blocked = 0
        for iss in chosen:
            if iss.severity == "error":
                blocked += 1        # errors are real rule violations: stay
                continue
            key = iss.waiver_key()
            if key not in waived:
                waived.add(key)
                added += 1
        if added:
            self.app.save_waivers()
        self.rerender()
        self.show_issues(self.issues)
        note = "  ({} error-level issue(s) cannot be ignored)".format(blocked) \
            if blocked else ""
        self.app.status_var.set(
            "Ignored {} issue(s) - they will not be flagged again on future "
            "audits.{}".format(added, note))

    def _unignore_selected(self):
        waived = getattr(self.app, "waivers", None)
        if waived is None:
            return
        chosen = self._issues_for_selection()
        removed = 0
        for iss in chosen:
            key = iss.waiver_key()
            if key in waived:
                waived.discard(key)
                removed += 1
        if removed:
            self.app.save_waivers()
        self.rerender()
        self.show_issues(self.issues)
        if removed:
            self.app.status_var.set(
                "Restored {} issue(s) to the active audit list.".format(removed))

    # ------------------------------------------------------------------
    # right-click context menu (row-specific actions)
    # ------------------------------------------------------------------
    def _build_context_menu(self):
        """Build the context menu for the CURRENT selection. Items enable/
        disable based on what the selection actually contains, so the menu
        only ever offers valid actions. Returns the tk.Menu (not posted)."""
        issues = self._issues_for_selection()

        has_entry = any(isinstance(iss.entry_index, int)
                        and iss.entry_index >= 0
                        and not iss.entry_id.startswith("(")
                        for iss in issues)
        has_pair = any(getattr(iss, "pair_ids", None) for iss in issues)
        has_ignorable = any(not self._waived(iss) and iss.severity != "error"
                            for iss in issues)
        has_waived = any(self._waived(iss) for iss in issues)

        menu = tk.Menu(self, tearoff=0,
                       background=theme.BG_CARD, foreground=theme.TEXT_MAIN,
                       activebackground=theme.ACCENT_BLUE,
                       activeforeground=theme.contrast_text(theme.ACCENT_BLUE),
                       font=theme.font(13))
        menu.add_command(label="Go to Entry",
                         state="normal" if has_entry else "disabled",
                         command=self._goto_selected)
        menu.add_command(label="Merge Duplicate...",
                         state="normal" if has_pair else "disabled",
                         command=self._merge_selected)
        menu.add_separator()
        menu.add_command(label="Ignore Selected",
                         state="normal" if has_ignorable else "disabled",
                         command=self._ignore_selected)
        menu.add_command(label="Un-ignore Selected",
                         state="normal" if has_waived else "disabled",
                         command=self._unignore_selected)
        return menu

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return                      # only offer actions ON an issue row
        # right-clicking a selected row keeps the whole multi-selection
        # (so batch actions act on it); clicking an unselected row makes
        # it the only selection
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        if not self._issues_for_selection():
            return
        menu = self._build_context_menu()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    # ------------------------------------------------------------------
    # selection helpers
    # ------------------------------------------------------------------
    def _select_all_issues(self, _event=None):
        """Ctrl+A: select every visible row, including issue rows nested
        inside group headers (Tk has no built-in Ctrl+A for trees)."""
        stack = list(self.tree.get_children(""))
        all_items = []
        while stack:
            iid = stack.pop()
            all_items.append(iid)
            stack.extend(self.tree.get_children(iid))
        if all_items:
            self.tree.selection_set(all_items)
        return "break"

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
        self.app.run_audit(switch_tab=False)

    def _goto_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        iss = self._row_issues.get(sel[0]) or next(
            iter(self._group_items.get(sel[0], [])), None)
        if iss is not None:
            self.app.reveal_entry(iss)

    # ------------------------------------------------------------------
    # duplicate-pair merging
    # ------------------------------------------------------------------
    def _merge_selected(self):
        pair = None
        for iss in self._issues_for_selection():
            if getattr(iss, "pair_ids", None):
                pair = iss.pair_ids
                break
        if not pair:
            return
        pos_a = self.app._find_slot(None, {"id": pair[0]})
        pos_b = self.app._find_slot(None, {"id": pair[1]})
        missing = [pid for pid, pos in zip(pair, (pos_a, pos_b)) if pos < 0]
        if missing:
            messagebox.showwarning(
                APP_TITLE,
                "No longer in the database: {}.\n\nRe-run the audit to "
                "refresh duplicate findings.".format(", ".join(missing)))
            return
        MergeDialog(self.winfo_toplevel(), self.app, pos_a, pos_b)

    def _on_issue_activate(self, _event=None):
        """Double-click / Enter on an issue leaf jumps to that entry in the
        database tree (loads it in the editor). Group headers toggle."""
        iid = self.tree.focus()
        if iid and iid in self._row_issues:
            self.app.reveal_entry(self._row_issues[iid])
            return "break"
        return None   # group header: let Treeview expand/collapse natively

    def _fix_all(self):
        # waived issues are deliberately left alone by Fix All: the user
        # said they are fine as-is (explicit selection still can fix them)
        fixable = [i for i in self.issues
                   if i.fix and not self._waived(i)]
        if not fixable:
            messagebox.showinfo(APP_TITLE, "No auto-fixable issues found.")
            return
        if not messagebox.askyesno(APP_TITLE, "Apply {} automatic fixes?".format(len(fixable))):
            return
        self.app.apply_fixes(fixable)
        self.app.run_audit(switch_tab=False)

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
                    mark = " [ignored]" if self._waived(issue) else ""
                    f.write("[{}] {} :: {} :: {}{}\n".format(
                        issue.severity.upper(), issue.category,
                        issue.entry_id, issue.message, mark))
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
        self.summary_label = ttk.Label(top, text="", style="TLabel", foreground=theme.TEXT_DIM)
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
        # NO touch-style drag panning: multi-select list (drag must select,
        # not pan). Scrollbar + mouse wheel cover scrolling; Ctrl+A selects
        # every undoable/redoable row at once.
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)
        self.tree.bind("<MouseWheel>", self._on_mousewheel)
        self.tree.bind("<Button-4>", self._on_mousewheel)
        self.tree.bind("<Button-5>", self._on_mousewheel)

        self.tree.tag_configure("section", background=theme.BG_CARD,
                                 foreground=theme.ACCENT_ORANGE,
                                 font=theme.font(12, "bold"))
        self.tree.tag_configure("op", foreground=theme.TEXT_MAIN)
        self.tree.tag_configure("redoable", foreground=theme.ACCENT_BLUE)

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
                 "delete": "Deleted entry", "fixes": "Audit fixes",
                 "merge": "Merged duplicates", "import": "Imported entries"}

    def _select_all_rows(self, _event=None):
        """Ctrl+A: select every row (Tk has no built-in tree Ctrl+A).
        Section header rows are included but _selected_ops ignores them."""
        stack = list(self.tree.get_children(""))
        all_items = []
        while stack:
            iid = stack.pop()
            all_items.append(iid)
            stack.extend(self.tree.get_children(iid))
        if all_items:
            self.tree.selection_set(all_items)
        return "break"

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


class StatusMarquee(tk.Canvas):
    """Bottom status bar: shows the status message, and if it is too long
    for the window it auto-scrolls (marquee) instead of clipping or needing
    a horizontal scrollbar. When the text fits, the loop drops to a cheap
    4 Hz idle heartbeat instead of repainting 20x/second."""

    TICK_MS = 50
    IDLE_MS = 250
    STEP_PX = 2
    PAUSE_TICKS = 24          # brief hold each time the text loops

    def __init__(self, master, textvariable, **kw):
        kw.setdefault("height", 26)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, background=theme.BG_PANEL, **kw)
        self.var = textvariable
        self._font = theme.font(12)
        self._item = self.create_text(8, 13, anchor="w", text="",
                                      fill=theme.TEXT_DIM, font=self._font,
                                      tags="t")
        self._offset = 0
        self._pause = 0
        self._text_w = None       # cached pixel width of the current text
        self.var.trace_add("write", lambda *a: self.reset())
        self.bind("<Configure>", lambda _e: self.reset())
        self._loop()

    def reset(self):
        self._offset = 0
        self._pause = 0
        self._render()

    def _render(self):
        self._font = theme.font(12)
        self.itemconfigure(self._item, text=self.var.get(),
                           fill=theme.TEXT_DIM, font=self._font)
        self._measure()

    def _measure(self):
        import tkinter.font as tkfont
        try:
            self._text_w = tkfont.Font(font=self._font).measure(self.var.get())
        except Exception:
            self._text_w = None

    def _loop(self):
        next_tick = self.IDLE_MS
        try:
            if self.winfo_ismapped():
                avail = max(1, self.winfo_width() - 16)
                tw = self._text_w
                if tw is None:
                    self._measure()
                    tw = self._text_w
                if tw is not None and tw > avail:
                    self._render()      # only while actually scrolling
                    if self._pause > 0:
                        self._pause -= 1
                    else:
                        self._offset -= self.STEP_PX
                        # scroll fully out, then loop from the right edge
                        limit = -(tw + 40)
                        if self._offset < limit:
                            self._offset = avail
                            self._pause = self.PAUSE_TICKS
                    self.coords(self._item, 8 + self._offset, 13)
                    next_tick = self.TICK_MS
                elif self._offset != 0:
                    self._offset = 0
                    self.coords(self._item, 8, 13)
        except Exception:
            pass
        self.after(next_tick, self._loop)


# ---------------------------------------------------------------------------
# MAIN APPLICATION
# ---------------------------------------------------------------------------
class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self._set_window_icon()
        self.geometry("1400x860")
        self.minsize(860, 540)   # half-screen snap stays fully usable
        self.configure(background=theme.BG_MAIN)
        setup_styles(self)

        self.entries = []
        self.db_path = None
        self.data_root = None
        self.dirty = False
        self.editing_index = None  # index into self.entries currently loaded in editor, or None for "new"
        # waived audit findings ("Ignore Selected" in the Audit tab);
        # persisted in audit_waivers.json beside the database file
        self.waivers = set()

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

    def _import_entries(self):
        """Open the Import Entries dialog (review & apply AI output)."""
        ai_import.ImportDialog(self)

    def _mark_audit_dirty(self):
        """Flag audit results as stale; if the user is literally looking at
        the Audit tab right now, refresh immediately."""
        self._audit_dirty = True
        try:
            w = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if w is self.audit_panel and self.entries:
            self.run_audit(switch_tab=False)

    def refresh_spell_vocab(self):
        """Feed every Brand/Model/Variant in the loaded database to the
        spellchecker so product names are never flagged as typos."""
        texts = []
        for e in self.entries:
            texts.extend([e.get("brand", ""), e.get("model", ""), e.get("variant", "")])
        self.speller.replace_dynamic_vocab(texts)

    # Coalescing window for autosave: bursts of commits (Fix All, imports,
    # rapid Save Entry clicks) collapse into ONE snapshot + disk write
    # instead of a full deep-copy of the database on the UI thread per
    # commit. Crash-recovery exposure is bounded to this many milliseconds.
    AUTOSAVE_DELAY_MS = 2500

    def _autosave(self):
        """Schedule a coalesced autosave. The snapshot itself is built on
        the timer tick (still on the UI thread, so entry dicts are stable)
        and serialized on the worker thread."""
        if not self.entries or not self.db_path:
            return
        if getattr(self, "_as_after", None):
            try:
                self.after_cancel(self._as_after)
            except Exception:
                pass
        try:
            self._as_after = self.after(self.AUTOSAVE_DELAY_MS,
                                        self._autosave_flush)
        except Exception:
            pass                        # shutting down

    def _autosave_flush(self):
        """Build one independent snapshot and hand it to the writer thread."""
        self._as_after = None
        if not self.entries or not self.db_path:
            return
        if not hasattr(self, "_as_lock"):
            self._as_lock = threading.Lock()
            self._as_pending = None
            self._as_thread = None

        with self._as_lock:
            # Independent entry copies: the autosave worker serializes these
            # on a background thread while the UI may commit edits to the
            # live dicts (same rationale as the Export tab's snapshots).
            self._as_pending = (self.db_path,
                                [L.build_clean_entry(e) for e in self.entries])

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

    def _autosave_cancel(self):
        after_id = getattr(self, "_as_after", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            self._as_after = None

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
        self._menus = [menubar]

        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Database...", command=self.open_database)
        filemenu.add_command(label="Set Data Folder...", command=self.set_data_folder)
        filemenu.add_separator()
        filemenu.add_command(label="Save As...", command=self.save_as)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filemenu)
        self._menus.append(filemenu)

        editmenu = tk.Menu(menubar, tearoff=0)
        editmenu.add_command(label="Add New Entry", command=self.add_entry)
        editmenu.add_command(label="Delete Selected Entry", command=self.delete_entry)
        editmenu.add_separator()
        editmenu.add_command(label="Undo Last Action", command=self.undo_last)
        editmenu.add_command(label="Redo Last Undone Action", command=self.redo_last)
        menubar.add_cascade(label="Edit", menu=editmenu)
        self._menus.append(editmenu)

        auditmenu = tk.Menu(menubar, tearoff=0)
        auditmenu.add_command(label="Run Full Audit", command=self.run_audit)
        menubar.add_cascade(label="Audit", menu=auditmenu)
        self._menus.append(auditmenu)

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
        self._menus.append(toolmenu)

        # View menu -- the home of appearance controls: theme only. The app
        # always renders in the OS's own default UI font (no bundled font
        # files to pick between, no per-platform registration -- see
        # theme.py's font section).
        viewmenu = tk.Menu(menubar, tearoff=0)
        self._theme_var = tk.StringVar(value=theme.current_theme_id)
        thememenu = tk.Menu(viewmenu, tearoff=0)
        for t in theme.THEMES:
            kwargs = dict(label=" {}".format(t["name"]),
                          value=t["id"], variable=self._theme_var,
                          command=lambda tid=t["id"]: self._set_theme(tid))
            img = theme.emoji_photo(t["emoji"], 14, root=self)
            if img is not None:
                kwargs.update(image=img, compound="left")
            thememenu.add_radiobutton(**kwargs)
        viewmenu.add_cascade(label="Theme", menu=thememenu)
        menubar.add_cascade(label="View", menu=viewmenu)
        self._menus.extend([viewmenu, thememenu])

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self._menus.append(helpmenu)
        self.config(menu=menubar)
        self._sync_menu_colors()

    def _sync_menu_colors(self):
        """Re-palette every tk.Menu (ttk can't style native menus)."""
        for m in getattr(self, "_menus", []):
            try:
                theme.style_menu(m)
            except Exception:
                pass

    def _open_export_tab(self):
        self.notebook.select(self.tools_panel)

    def _build_layout(self):
        self._build_header()

        toolbar = ttk.Frame(self, style="Panel.TFrame")
        toolbar.pack(fill="x", side="top")
        ttk.Button(toolbar, text="Open Database", command=self.open_database).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Save", style="Accent.TButton", command=self.save_as).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Add Entry", command=self.add_entry).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Import Entries",
                   command=self._import_entries).pack(side="left", padx=4, pady=6)
        ttk.Button(toolbar, text="Delete Entry", style="Danger.TButton", command=self.delete_entry).pack(side="left", padx=4, pady=6)

        ttk.Label(toolbar, text="Search:", style="Panel.TLabel").pack(side="left", padx=(20, 4))
        self.search_var = tk.StringVar()
        # fills all remaining toolbar space instead of a fixed char width,
        # so it stays usable at half-screen snap
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var)
        search_entry.pack(side="left", padx=4, fill="x", expand=True)
        attach_entry_context_menu(search_entry)
        # syntax help on hover (plain text + field filters)
        HoverTooltip(search_entry, lambda x, y: SEARCH_HELP)
        self._search_debounce_id = None
        def _on_search_change(*a):
            if self._search_debounce_id:
                try:
                    self.after_cancel(self._search_debounce_id)
                except Exception:
                    pass
            # populate_tree rebuilds every row; measured ~37 ms per 10k
            # entries, so coalescing keystroke bursts keeps large databases
            # smooth while staying responsive.
            self._search_debounce_id = self.after(220, self.populate_tree)
        self.search_var.trace_add("write", _on_search_change)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        self.paned = paned

        # ---- LEFT column: database entries (offset-shadow card) ----
        tree_outer, left = make_card(paned)
        paned.add(tree_outer, weight=1)
        try:
            paned.paneconfigure(tree_outer, minsize=190)
        except Exception:
            pass    # minsize is not supported on every Tk build

        # live entry counter in the tree header (kept current by
        # populate_tree, which runs after every add/delete/undo/fix/load)
        # "ENTRIES" rather than "DATABASE ENTRIES": the title bar already
        # says DATABASE TOOL immediately above, and the shorter label
        # leaves the live count room to stay on-screen at the narrowest
        # supported column width instead of being clipped to "(5,0...".
        self.entries_header_var = tk.StringVar(value="ENTRIES  (0)")
        entries_header = ttk.Label(left, textvariable=self.entries_header_var,
                                    style="CardHeader.TLabel")
        entries_header.pack(anchor="w", fill="x", padx=8, pady=(8, 4))
        # safety net if a very wide font or very narrow column still can't
        # fit it on one line: wrap onto a second line instead of clipping.
        entries_header.bind(
            "<Configure>",
            lambda e: entries_header.configure(wraplength=max(80, e.width)))
        tree_frame = ttk.Frame(left, style="CardFlat.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        # vertical only: long names ellipsize + show in full on hover /
        # via the tooltip instead of a horizontal scrollbar
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.column("#0", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        attach_touch_scroll(self.tree)
        self._full_labels = {}
        self._ellipsis_after = None
        self.tree.bind("<Configure>", self._schedule_ellipsis, add="+")
        HoverTooltip(self.tree, self._tree_full_text_at)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ---- RIGHT column: the tabbed workbench (offset-shadow card) ----
        nb_outer, nb_inner = make_card(paned)
        paned.add(nb_outer, weight=3)

        self.notebook = ttk.Notebook(nb_inner, style="Card.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # responsive tabs: shrink padding as the pane narrows so all five
        # tabs stay fully visible (no clipped labels) at any window size
        self._tab_pad_cur = None
        self._tab_pad_after = None
        self.notebook.bind("<Configure>", self._schedule_fit_tabs, add="+")

        self.editor = EntryEditor(self.notebook, self)
        self._editor_tab_text = "  Editor  "
        self.notebook.add(self.editor, text=self._editor_tab_text)

        self.audit_panel = AuditPanel(self.notebook, self)
        self.notebook.add(self.audit_panel, text="  Audit  ")

        self.history_panel = None   # created just below, before any op recording
        self.history_panel = HistoryPanel(self.notebook, self)
        self.notebook.add(self.history_panel, text="  History  ")

        # companion tools (secondary to the editor by design)
        self.curve_panel = curve_import.CurveImportPanel(self.notebook, self)
        self.notebook.add(self.curve_panel, text="  Import  ")
        # drag-panning for the Import tab's scrollable body (wired here, not
        # inside curve_import, to keep the module import graph acyclic)
        attach_touch_scroll_canvas(self.curve_panel.scroll_canvas,
                                   self.curve_panel.scroll_inner)

        self.tools_panel = tools_panel.ToolsPanel(self.notebook, self)
        self.notebook.add(self.tools_panel, text="  Export  ")

        # ttk panes start at their requested sizes, which can squeeze the
        # tree pane to near-zero; set the initial sash position once the
        # window is realized (only once -- user drags are never overwritten).
        # The width scales with the window (20%, clamped) so half-screen
        # snaps keep the workbench usable.
        self._sashes_set = False

        def _initial_sashes(_e=None):
            if self._sashes_set:
                return
            w = paned.winfo_width()
            if w < 700:
                return
            self._sashes_set = True
            try:
                paned.sashpos(0, max(210, min(300, int(w * 0.20))))
            except Exception:
                pass
        paned.bind("<Configure>", _initial_sashes, add="+")

        status = ttk.Frame(self, style="Panel.TFrame")
        status.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="No database loaded.")
        self.status_marquee = StatusMarquee(status,
                                            textvariable=self.status_var)
        self.status_marquee.pack(side="left", padx=8, pady=2,
                                 fill="x", expand=True)

        # app-wide shortcuts: Ctrl+S = Save Entry (editor commit),
        # Ctrl+N = new entry. Modal dialogs run with their own grab, so
        # these only ever fire while the main window has focus.
        self.bind("<Control-s>", self._on_ctrl_save)
        self.bind("<Control-S>", self._on_ctrl_save)
        self.bind("<Control-n>", self._on_ctrl_new)
        self.bind("<Control-N>", self._on_ctrl_new)

        # live-retheme hooks: canvas art + placed widgets the style engine
        # and retint walker cannot reach on their own
        theme.add_retheme_hook(self._on_retheme_hook)
        theme.add_retheme_hook(self.audit_panel.rerender)

    # ------------------------------------------------------------------
    # HEADER STRIP (solid pixel title; appearance controls live in the
    # View menu)
    # ------------------------------------------------------------------
    def _build_header(self):
        header = tk.Frame(self, background=theme.BG_PANEL, height=44)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        self.header = header

        self.header_title = tk.Label(
            header, text="\U0001F4BE  DATABASE TOOL",
            font=theme.title_font(), bg=theme.BG_PANEL, fg=theme.TEXT_MAIN)
        self.header_title.pack(side="left", padx=14, pady=6)

    def _on_ctrl_save(self, _event=None):
        """Ctrl+S: commit the editor form (same as the Save Entry button)."""
        if self.entries or self.editor.form_is_dirty() or \
                self.editor.brand_entry.get() or self.editor.model_entry.get():
            self.editor._on_save()
        return "break"

    def _on_ctrl_new(self, _event=None):
        self.add_entry()
        return "break"

    def _on_retheme_hook(self):
        """Registered as a live-retheme hook: refreshes the placed header
        widgets (fonts can't be retinted by the walker) and menus."""
        try:
            self.header_title.configure(font=theme.title_font(),
                                        bg=theme.BG_PANEL, fg=theme.TEXT_MAIN)
            self.header.configure(background=theme.BG_PANEL)
            if getattr(self, "status_marquee", None) is not None:
                self.status_marquee.configure(background=theme.BG_PANEL)
                self.status_marquee.reset()
            self._sync_menu_colors()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # RESPONSIVE TAB SIZING
    # ------------------------------------------------------------------
    def _schedule_fit_tabs(self, _event=None):
        if self._tab_pad_after is not None:
            try:
                self.after_cancel(self._tab_pad_after)
            except Exception:
                pass
        self._tab_pad_after = self.after(80, self._fit_tabs)

    def _fit_tabs(self):
        """Shrink tab padding as the notebook narrows so all five tabs stay
        fully visible (no clipped labels) at half-screen snap."""
        self._tab_pad_after = None
        w = self.notebook.winfo_width()
        px = 14 if w > 1150 else (9 if w > 920 else 4)
        if px != self._tab_pad_cur:
            self._tab_pad_cur = px
            theme.set_tab_pad(px)

    # ------------------------------------------------------------------
    # THEME SWITCHING (View menu)
    # ------------------------------------------------------------------
    def _set_theme(self, theme_id):
        if theme.set_theme(theme_id):
            if hasattr(self, "_theme_var"):
                self._theme_var.set(theme_id)
            restyle_app(self)

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
        # capture whatever the coalescing autosave had not flushed yet
        self._autosave_cancel()
        self._autosave_flush()
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
        self.waivers = L.load_waivers(path)
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
        # Post-load audit runs WITHOUT yanking the user off the Editor tab;
        # results surface via the Audit tab's live issue-count badge.
        self.run_audit(switch_tab=False)

    def _audit_done_popup(self, issue_count):
        # Kept for backward compatibility with external callers; the badge
        # on the Audit tab replaced the post-load modal.
        pass

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

    def save_waivers(self):
        """Persist the Audit tab's ignored-issue list beside the database."""
        if self.db_path:
            try:
                L.save_waivers(self.db_path, self.waivers)
            except OSError as e:
                L.log("Waiver save failed: {}".format(e))

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
                self.run_audit(switch_tab=False)
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
        # Advisory gate only: reuse the freshest cached audit results instead
        # of re-running the full audit (incl. a data-folder walk) on the UI
        # thread every save. The Audit tab auto-refreshes on mutations, so
        # stale results simply mean "no prompt", never a wrong save.
        issues = [] if getattr(self, "_audit_dirty", True) \
            else list(getattr(self.audit_panel, "issues", []) or [])
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
        # Adopt the saved file as the working database so autosave backups,
        # crash recovery, pre-overwrite snapshots, waiver storage and the
        # overwrite-original check all track the file the user actually
        # keeps editing. data_root is deliberately left alone: measurements
        # stay where they are, and relinking is an explicit action.
        self.db_path = path
        self.dirty = False
        # carry any audit waivers along to the saved location so the edited
        # database keeps its ignored-findings list
        try:
            L.save_waivers(path, self.waivers)
        except OSError:
            pass
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
    def populate_tree(self, restore_selection=True):
        # a rebuild invalidates the marquee row
        self._stop_tree_marquee()
        # Cancel any pending search-debounce rebuild: every mutation
        # funnels through here, so the state we are rendering right now is
        # the freshest. Without this, a queued debounce could fire AFTER a
        # commit/undo/delete/reveal and repopulate the tree over a freshly
        # made selection (same hazard reveal_entry already guards against).
        if getattr(self, "_search_debounce_id", None):
            try:
                self.after_cancel(self._search_debounce_id)
            except Exception:
                pass
            self._search_debounce_id = None
        # header always shows the TOTAL database size, never the filtered view
        self.entries_header_var.set("ENTRIES  ({:,})".format(len(self.entries)))
        # preserve the user's place across rebuilds: expanded brands, scroll
        # position, and (when the row didn't move) the selected entry
        prev_open = {iid for iid in self.tree.get_children("")
                     if self.tree.item(iid, "open")}
        try:
            prev_scroll = self.tree.yview()[0]
        except Exception:
            prev_scroll = 0.0
        prev_sel = self.tree.selection()
        prev_sel_iid = prev_sel[0] if prev_sel else None
        prev_sel_id = None
        if prev_sel_iid and prev_sel_iid.startswith("entry:"):
            try:
                prev_sel_id = self.entries[int(prev_sel_iid.split(":", 1)[1])].get("id")
            except Exception:
                prev_sel_id = None
        self.tree.delete(*self.tree.get_children())
        self._full_labels = {}
        query = self.search_var.get().strip().lower()
        by_brand = {}
        for idx, e in enumerate(self.entries):
            if query and not entry_matches_query(e, query):
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
                # Model [Variant] only -- the internal id stays out of the
                # row text (cleaner tree); it is still one lookup away and
                # is shown in the hover tooltip instead.
                label = e.get("model", "")
                if e.get("variant"):
                    label += "  [{}]".format(e["variant"])
                iid = "entry:{}".format(idx)
                self._full_labels[iid] = label
                self.tree.insert(node, "end", iid=iid, text=label)
        # restore what we captured (only where still valid after rebuild)
        for iid in self.tree.get_children(""):
            if iid in prev_open:
                self.tree.item(iid, open=True)
        if 0.0 < prev_scroll < 1.0:
            try:
                self.tree.yview_moveto(prev_scroll)
            except Exception:
                pass
        if restore_selection and prev_sel_iid:
            sel_ok = False
            if prev_sel_iid.startswith("entry:") and prev_sel_id is not None \
                    and self.tree.exists(prev_sel_iid):
                # same list slot must still hold the same entry, otherwise
                # indices shifted and silently re-selecting would be wrong
                try:
                    same = self.entries[
                        int(prev_sel_iid.split(":", 1)[1])].get("id") == prev_sel_id
                    if same:
                        self.tree.selection_set(prev_sel_iid)
                        sel_ok = True
                except Exception:
                    sel_ok = False
            if not sel_ok and prev_sel_iid.startswith("entry:"):
                pass    # row moved/gone: caller decides what to highlight
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
            fnt = tkfont.Font(font=theme.font(13))
            cw = max(4, fnt.measure("n"))
        except Exception:
            cw = 8
        # small reserved margin: rows use the full column width (minus the
        # scrollbar and a little padding); clipped names marquee when selected
        budget_base = int((wpx - 36) // cw)
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
        self._start_tree_marquee()

    # -- marquee: the selected row's name auto-scrolls when too long -------
    MARQUEE_TICK_MS = 160

    def _start_tree_marquee(self):
        self._stop_tree_marquee()
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        full = self._full_labels.get(iid, "")
        try:
            disp = self.tree.item(iid, "text")
        except Exception:
            return
        if not full or disp == full:
            return                      # fits: nothing to scroll
        self._marquee = {"iid": iid, "pos": 0}
        self._marquee_tick()

    def _marquee_tick(self):
        m = getattr(self, "_marquee", None)
        if not m:
            return
        iid = m["iid"]
        if not self.tree.exists(iid) or iid not in self.tree.selection():
            self._stop_tree_marquee()
            return
        full = self._full_labels.get(iid, "")
        try:
            disp = self.tree.item(iid, "text")
        except Exception:
            self._stop_tree_marquee()
            return
        budget = len(disp)
        if not budget or disp == full:
            self._stop_tree_marquee()
            return
        m["pos"] = (m["pos"] + 1) % max(1, len(full) + 4)
        window = full[m["pos"]:]
        if len(window) < budget:        # loop gap before wrapping around
            window = window + "     " + full
        try:
            self.tree.item(iid, text=window[:budget])
        except Exception:
            self._stop_tree_marquee()
            return
        self._marquee_after = self.after(self.MARQUEE_TICK_MS, self._marquee_tick)

    def _stop_tree_marquee(self):
        if getattr(self, "_marquee_after", None):
            try:
                self.after_cancel(self._marquee_after)
            except Exception:
                pass
        self._marquee_after = None
        m = getattr(self, "_marquee", None)
        self._marquee = None
        if m and self.tree.exists(m["iid"]):
            # restore the ellipsized (non-scrolling) form of the row
            full = self._full_labels.get(m["iid"], "")
            wpx = self.tree.winfo_width()
            try:
                import tkinter.font as tkfont
                cw = max(4, tkfont.Font(font=theme.font(13)).measure("n"))
            except Exception:
                cw = 8
            budget = max(8, int((wpx - 36) // cw)
                         - (1 if m["iid"].startswith("brand:") else 2))
            try:
                self.tree.item(m["iid"], text=ellipsize(full, budget))
            except Exception:
                pass

    def _tree_full_text_at(self, x, y):
        iid = self.tree.identify_row(y)
        if iid and iid in self._full_labels:
            # entry rows: full name + the internal id (kept out of the row
            # text itself, but still discoverable on hover)
            if iid.startswith("entry:"):
                try:
                    eid = self.entries[int(iid.split(":", 1)[1])].get("id", "")
                    return "{}\n{}".format(self._full_labels[iid], eid)
                except Exception:
                    pass
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
        # long names auto-scroll (marquee) while the row stays selected
        self._start_tree_marquee()
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
        # restore_selection=False: the deleted row's index was consumed by
        # the list shift, so re-selecting "the same iid" would load whatever
        # neighbor slid into it. Expansion + scroll still come back.
        self.populate_tree(restore_selection=False)
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
        editing = self.editing_index is not None \
            and 0 <= self.editing_index < len(self.entries)

        if editing:
            old_id = self.entries[self.editing_index].get("id")
            if entry["id"] != old_id:
                ok = messagebox.askyesno(
                    APP_TITLE,
                    "Replace the content of existing entry\n\n  '{}'\n\nwith the "
                    "form data for '{}'?".format(old_id, entry["id"]))
                if not ok:
                    return ["Cancelled - '{}' was left unchanged.".format(old_id)]
            existing_ids = {e.get("id") for i, e in enumerate(self.entries)
                            if i != self.editing_index}
        else:
            existing_ids = {e.get("id") for e in self.entries}

        errors = L.validate_entry(entry, existing_ids=existing_ids, exclude_id=None)
        if errors:
            return errors

        # AUDIT PROMPT zero-rule: impedance/sensitivity = 0 is "STRICTLY
        # FORBIDDEN" on wired entries (only TWS may be 0). validate_entry
        # deliberately does not block it (specs can legitimately be unknown
        # mid-research), but saving 0/0 silently used to ship unverified
        # data -- so confirm explicitly before committing.
        ff = (entry.get("form_factor") or "").strip()
        if ff and ff != L.TWS_FORM_FACTOR:
            missing = [label for field, label in
                       (("impedance", "Impedance"), ("sensitivity", "Sensitivity"))
                       if L.coerce_int(entry.get(field, 0), -1) == 0]
            if missing:
                ok = messagebox.askyesno(
                    APP_TITLE,
                    "{} is 0 on a wired entry ({}).\n\n"
                    "Per the audit rules this is STRICTLY FORBIDDEN and "
                    "indicates missing/unverified data -- research and "
                    "populate the real spec before saving.\n\n"
                    "Save anyway with unverified specs?".format(
                        " and ".join(missing), ff))
                if not ok:
                    return ["Cancelled -- fill in {} before saving.".format(
                        " and ".join(missing))]

        clean = L.build_clean_entry(entry)

        if editing:
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
    def run_audit(self, done=None, switch_tab=True):
        """Run the full audit. Heavy runs happen in a daemon thread that only
        computes; ALL tkinter access happens here on the main thread via an
        after()-poll loop, so this is safe during startup, shutdown, and any
        Tcl build.

        switch_tab=False keeps the user wherever they are (post-load audit,
        silent auto-refreshes); results still land via the Audit tab's
        issue-count badge."""
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

        # Small databases audit in well under a second even with a data
        # folder walk; doing it synchronously avoids the tab switch and
        # status flicker entirely. Large ones go to the worker thread.
        if len(self.entries) <= 2000:
            issues, err = _compute()
            if err:
                messagebox.showwarning(APP_TITLE, "Audit failed:\n{}".format(err))
            else:
                self.audit_panel.show_issues(issues)
                if switch_tab:
                    self.notebook.select(self.audit_panel)
                self._audit_dirty = False
            if done:
                done(len(issues))
            return

        self.status_var.set("Running audit...")
        if switch_tab:
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

    # ------------------------------------------------------------------
    # AUDIT TAB BADGE (live issue count on the tab label)
    # ------------------------------------------------------------------
    AUDIT_TAB_BASE = "  Audit  "

    def _update_audit_badge(self, live_issues):
        """Show unresolved issue count on the Audit tab so results are
        visible from any tab (replaces yanking the user to Audit after a
        load). Errors and warnings share one number; 0 hides the badge."""
        label = self.AUDIT_TAB_BASE
        if live_issues:
            label = "  Audit \u26a0 {}  ".format(live_issues)
        try:
            for i, tab in enumerate(self.notebook.tabs()):
                if str(self.audit_panel) == tab or \
                        self.notebook.tab(tab, "text").startswith("  Audit"):
                    self.notebook.tab(i, text=label)
                    break
        except Exception:
            pass

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
        not_applied = stale + failed
        if not_applied:
            # Counted from the actual fix results (stale = target moved or
            # gone; failed = raised), NOT derived from len(changes): several
            # fixes can land on the SAME entry and collapse into one change
            # record, which made the old derivation over-report skips.
            note = " ({} of {} skipped: {} stale, {} failed)".format(
                not_applied, len(fixable), stale, failed)
        self.dirty = True
        self.populate_tree()
        self._reload_editor_if_affected({c["pos_hint"] for c in changes})
        self.status_var.set("Applied {} fix(es){}. Remember to Save As to keep them.".format(
            len(changes), note))
        self._notify_db_changed()
        self._autosave()

    def _reload_editor_if_affected(self, positions):
        """Reload the editor form when the entry it is showing was mutated
        in place (audit fixes). Without this the form keeps the PRE-fix
        values and a later Save Entry silently reverts the repairs."""
        idx = self.editing_index
        if idx is None or not (0 <= idx < len(self.entries)):
            return
        if idx in positions:
            try:
                self.editor.load_entry(self.entries[idx])
            except Exception:
                L.log("Editor reload after fixes failed")

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
    s = str(raw).strip()
    # Reject Python-style digit separators ("2_023" -> 2023 used to slip
    # through); underscores are never valid in these spec fields.
    if "_" in s:
        return default
    if s == "":
        return default
    try:
        return int(s)
    except ValueError:
        return default

if __name__ == "__main__":
    main()