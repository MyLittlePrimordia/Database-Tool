"""
theme.py -- IEM Tool-matched theming for the Database Tool.

Everything visual lives here so every surface stays consistent:

  * THEMES       -- the 9 palettes ported 1:1 from IEM Tool's app.css
                    (.theme-* custom properties), including the exact
                    surface tones, text tones and per-theme accent.
  * FONT         -- the OS's own default UI font (no bundled TTFs, no
                    per-platform registration -- see resolve_font_family()
                    for why this is the zero-dependency choice).
  * apply_styles -- the full ttk style sheet (hard 1px black borders, flat
                    blocks, accent selection) matching IEM Tool's
                    "no rounded corners, no blur, offset shadows" look.
  * retint       -- live theme switching: walks every tk (non-ttk) widget
                    and remaps old-palette colors to the new palette; ttk
                    widgets restyle themselves via apply_styles.
  * settings     -- theme/font/scale choice persisted to a user-level
                    settings.json and restored on startup.

The module-level color constants (BG_MAIN, ACCENT_BLUE, ...) are MUTATED by
set_theme(), so consumers must reference them as `theme.BG_MAIN`
(module attribute access) rather than `from theme import BG_MAIN`, which
would freeze the value at import time.
"""

import json
import os
import sys

import tkinter as tk
import tkinter.font as tkfont

# ===========================================================================
# PALETTES -- ported from IEM Tool (css/app.css + app-core.js builtInThemes)
# ===========================================================================
# All 9 themes share the same hard black borders and the same green/red/
# amber/violet accents; they differ in the 5 surface tones, the 2 text
# tones and the single accent color that drives selection/highlights.

_ACCENT_GREEN = "#38a169"
_ACCENT_RED = "#e53e3e"
_ACCENT_AMBER = "#dd6b20"
_ACCENT_VIOLET = "#805ad5"
_BORDER_BLACK = "#000000"

THEMES = [
    {"id": "slate", "name": "Slate", "emoji": "\U0001F579",
     "bg_body": "#111115", "bg_window": "#16161c", "bg_card": "#202028",
     "bg_sidebar": "#0d0d10", "bg_input": "#181822",
     "text_main": "#f0f0f4", "text_secondary": "#8c8c9e",
     "accent": "#6488b0"},
    {"id": "parchment", "name": "Parchment", "emoji": "\U0001F4DC",
     "bg_body": "#cdb98c", "bg_window": "#d4c093", "bg_card": "#e2d2a8",
     "bg_sidebar": "#bda87d", "bg_input": "#e6d8b0",
     "text_main": "#1a1105", "text_secondary": "#4a3722",
     "accent": "#c85a0e"},
    {"id": "ember", "name": "Ember", "emoji": "\U0001F534",
     "bg_body": "#181111", "bg_window": "#201616", "bg_card": "#2c1e1e",
     "bg_sidebar": "#130d0d", "bg_input": "#171010",
     "text_main": "#f5ecec", "text_secondary": "#a88080",
     "accent": "#c84b4b"},
    {"id": "circuit", "name": "Circuit", "emoji": "\U0001F535",
     "bg_body": "#101520", "bg_window": "#161c2b", "bg_card": "#20283b",
     "bg_sidebar": "#0d111a", "bg_input": "#121724",
     "text_main": "#ecf2f8", "text_secondary": "#788ca8",
     "accent": "#457cb4"},
    {"id": "byte", "name": "Byte", "emoji": "\U0001F4DF",
     "bg_body": "#111812", "bg_window": "#162018", "bg_card": "#202d23",
     "bg_sidebar": "#0d130e", "bg_input": "#121a13",
     "text_main": "#ecf5ed", "text_secondary": "#7ea383",
     "accent": "#489a58"},
    {"id": "cartridge", "name": "Cartridge", "emoji": "\U0001F7E0",
     "bg_body": "#191410", "bg_window": "#211a15", "bg_card": "#2e251e",
     "bg_sidebar": "#13100d", "bg_input": "#18130f",
     "text_main": "#f7f0eb", "text_secondary": "#aa8e80",
     "accent": "#c8733a"},
    {"id": "arcade", "name": "Arcade", "emoji": "\U0001F47E",
     "bg_body": "#14111d", "bg_window": "#1b1728", "bg_card": "#272138",
     "bg_sidebar": "#100e18", "bg_input": "#14111f",
     "text_main": "#f2edf8", "text_secondary": "#9284a8",
     "accent": "#8262c8"},
    {"id": "blush", "name": "Blush", "emoji": "\U0001F338",
     "bg_body": "#1a1116", "bg_window": "#22161d", "bg_card": "#301e28",
     "bg_sidebar": "#140e13", "bg_input": "#191016",
     "text_main": "#f8edf4", "text_secondary": "#ac8497",
     "accent": "#c85a95"},
    {"id": "bit", "name": "Bit", "emoji": "\U0001FA99",
     "bg_body": "#18150d", "bg_window": "#201c11", "bg_card": "#2e2918",
     "bg_sidebar": "#13110a", "bg_input": "#1a160e",
     "text_main": "#f7f4e8", "text_secondary": "#ab9d78",
     "accent": "#ca9f33"},
]

THEME_BY_ID = {t["id"]: t for t in THEMES}

# Palette keys that vary per theme; retint builds its old->new map from
# these (accent-constant keys are identical in every theme, so remapping
# them is a no-op that costs nothing and stays future-proof).
_PALETTE_KEYS = ("bg_body", "bg_window", "bg_card", "bg_sidebar", "bg_input",
                 "text_main", "text_secondary", "accent",
                 "accent_green", "accent_red", "accent_amber", "accent_violet",
                 "border", "border_light")


def _palette_dict(t):
    return {
        "bg_body": t["bg_body"], "bg_window": t["bg_window"],
        "bg_card": t["bg_card"], "bg_sidebar": t["bg_sidebar"],
        "bg_input": t["bg_input"],
        "text_main": t["text_main"], "text_secondary": t["text_secondary"],
        "accent": t["accent"],
        "accent_green": _ACCENT_GREEN, "accent_red": _ACCENT_RED,
        "accent_amber": _ACCENT_AMBER, "accent_violet": _ACCENT_VIOLET,
        "border": _BORDER_BLACK, "border_light": _BORDER_BLACK,
    }


# ===========================================================================
# CURRENT PALETTE -- module-level constants (mutated by set_theme).
# Reference these as theme.XXX, never `from theme import XXX`.
# ===========================================================================
current_theme_id = "slate"

BG_MAIN = "#111115"        # body / outermost surface        (--bg-body)
BG_WINDOW = "#16161c"      # reserved (kept for parity with IEM Tool)
BG_PANEL = "#0d0d10"       # rails, toolbar, header, status  (--bg-sidebar)
BG_CARD = "#202028"        # cards                           (--bg-card)
BG_INPUT = "#181822"       # inputs, trees, consoles         (--bg-input)
BORDER = "#000000"         # hard 1px borders                (--border-color)
BORDER_LIGHT = "#000000"   # legacy lighter-border slot (black everywhere,
                           # kept so existing call sites keep working)
ACCENT_BLUE = "#6488b0"    # the theme's single accent       (--accent-blue)
ACCENT_GREEN = _ACCENT_GREEN
ACCENT_RED = _ACCENT_RED
ACCENT_ORANGE = _ACCENT_AMBER
ACCENT_PURPLE = _ACCENT_VIOLET
TEXT_MAIN = "#f0f0f4"
TEXT_DIM = "#8c8c9e"
OK_COLOR = _ACCENT_GREEN

_prev_palette = None       # snapshot for retint()'s old->new color map


# ===========================================================================
# SMALL COLOR HELPERS
# ===========================================================================
def _rgb(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def blend(a, b, t):
    """Blend hex color a toward b by t in [0, 1]."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#{:02x}{:02x}{:02x}".format(
        int(ra + (rb - ra) * t), int(ga + (gb - ga) * t),
        int(ba + (bb - ba) * t))


def lighten(color, t=0.15):
    return blend(color, "#ffffff", t)


def contrast_text(hex_color):
    """Black or white per perceived luminance (same rule as IEM Tool's
    getContrastTextColor) so accent-filled buttons/tabs stay readable in
    every theme, including the light Parchment palette."""
    r, g, b = _rgb(hex_color)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6 \
        else "#ffffff"


def grid_line_color():
    """Subtle blueprint-grid line color for the header strip: the panel
    tone lifted a few percent toward the text color."""
    return blend(BG_PANEL, TEXT_MAIN, 0.055)


# ===========================================================================
# SETTINGS PERSISTENCE (theme / font / font size)
# ===========================================================================
def _settings_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DatabaseTool")
    base = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "databasetool")


SETTINGS_PATH = os.path.join(_settings_dir(), "settings.json")

_settings = {"theme": "slate"}


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _settings.update({k: data[k] for k in _settings if k in data})
    except Exception:
        pass


def _save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2)
    except Exception:
        pass


_load_settings()


# ===========================================================================
# FONT -- native OS default only (zero bundled font files/dependencies).
# The app used to ship its own TTFs and privately register them per
# platform; that meant Windows-only registration code, per-OS fallback
# chains, and a visible flicker while the font picker rebuilt/relayout the
# whole UI. Using Tk's own default UI font keeps the app looking native
# on Windows/macOS/Linux with no bundled assets and no per-platform code.
# ===========================================================================
FONT_BASE_PX = 13          # role sizes, used by font()
FONT_SMALL_PX = 12
FONT_HEADER_PX = 16
FONT_TITLE_PX = 20

_family_cache = {"family": None}

_emoji_font_cache = None


def resolve_font_family(root=None):
    """The single OS default UI font -- resolved once and cached. No
    bundled TTFs, no per-platform registration: Tk's own "TkDefaultFont"
    already IS the native font on whichever OS the app is running on."""
    if _family_cache["family"] is None:
        try:
            fam = tkfont.nametofont("TkDefaultFont").actual("family")
        except Exception:
            fam = "Helvetica"
        _family_cache["family"] = fam
    return _family_cache["family"]


# Every distinct (size_px, weight) ever requested gets ONE persistent
# tkinter.font.Font object, cached here for life. Any widget given one of
# these as its `font=` keeps a live reference to it (this is how Tk fonts
# work), so a theme switch -- see refresh_all_fonts() -- instantly repaints
# every widget everywhere, including raw tk widgets (Listbox, Canvas text,
# Toplevel tooltips) that ttk style changes and the color-only retint()
# walker can never reach.
_font_obj_cache = {}

# key -> (family, px, weight) currently baked into the cached Font object.
# Tracked OURSELVES rather than asked back from Tk: Tk 9 normalizes pixel
# sizes into points internally (configure size=-13 -> actual() reports 10),
# so round-tripping through actual() can never verify a match -- every
# redundant configure() invalidates every widget using that font and forces
# a redraw cascade (the menubar most visibly). With this ledger, a value
# that is already applied costs nothing.
_font_applied = {}


def font(size_px=FONT_BASE_PX, weight="normal"):
    """Live Tk font at the given pixel size. Returns the SAME Font object
    on every call with the same (size_px, weight) -- widgets that used it
    keep updating for the lifetime of the app. Sizes are PIXELS (negative
    Tk size)."""
    family = resolve_font_family()
    px = max(7, int(round(size_px)))
    tk_weight = "bold" if weight == "bold" else "normal"
    key = (size_px, weight)
    target = (family, px, tk_weight)
    fnt = _font_obj_cache.get(key)
    if fnt is not None:
        try:
            # Reconfigure ONLY on an actual change: configuring a tkfont.Font
            # invalidates every widget using it, forcing Tk to redraw them
            # (menus -- i.e. the whole menubar -- are the most visible
            # casualties). Theme switches must be font-neutral now that the
            # family is fixed.
            if _font_applied.get(key) != target:
                fnt.configure(family=family, size=-px, weight=tk_weight)
                _font_applied[key] = target
            return fnt
        except Exception:
            _font_obj_cache.pop(key, None)   # widget/root was destroyed
            _font_applied.pop(key, None)
    try:
        fnt = tkfont.Font(family=family, size=-px, weight=tk_weight)
        _font_obj_cache[key] = fnt
        _font_applied[key] = target
        return fnt
    except Exception:
        # No default Tk root yet (e.g. a probe call before MainApp exists)
        # -- fall back to a plain tuple; every real widget is built after
        # the root exists so this path is only hit by early sizing probes.
        return (family, -px, weight) if tk_weight == "bold" else (family, -px)


def refresh_all_fonts():
    """Re-point every font() object at the current family in place. Kept
    so apply_styles()'s existing call site still works, and so a future
    OS-font change (there is currently only ever one) repaints instantly.
    No-ops per-object when the family/size/weight already match -- see
    the _font_applied ledger in font() for why spurious reconfiguration
    causes visible flicker."""
    family = resolve_font_family()
    dead = []
    for key, fnt in _font_obj_cache.items():
        size_px, weight = key
        px = max(7, int(round(size_px)))
        tk_weight = "bold" if weight == "bold" else "normal"
        target = (family, px, tk_weight)
        try:
            if _font_applied.get(key) == target:
                continue
            fnt.configure(family=family, size=-px, weight=tk_weight)
            _font_applied[key] = target
        except Exception:
            dead.append(key)
    for k in dead:
        _font_obj_cache.pop(k, None)
        _font_applied.pop(k, None)


def font_family():
    return resolve_font_family()


def header_font():
    return font(FONT_HEADER_PX, "bold")


def small_font():
    return font(FONT_SMALL_PX)


def title_font():
    return font(FONT_TITLE_PX, "bold")


def mono_font():
    """Legacy helper (kept for compatibility): base-size font."""
    return font(FONT_BASE_PX)


def pick_emoji_font():
    """Prefer an OS font with COLOR emoji glyphs (the UI fonts contain no
    emoji, and tk falls back inconsistently)."""
    global _emoji_font_cache
    if _emoji_font_cache:
        return _emoji_font_cache
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
        for f in ("Segoe UI Emoji",       # Windows 10/11 (color)
                  "Apple Color Emoji",    # macOS
                  "Noto Color Emoji"):    # Linux
            if f in families:
                _emoji_font_cache = f
                return f
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# COLOR EMOJI -> PNG RENDERING
# ---------------------------------------------------------------------------
# Tk renders text emojis as monochrome outlines on Windows, but Pillow can
# rasterize the COLR glyphs of "Segoe UI Emoji" in full color. We render
# each needed emoji once to a transparent PNG (cached on disk) and use it
# as an image everywhere the UI shows an emoji (tags, pickers, menus).
_EMOJI_PHOTO_CACHE = {}
_EMOJI_PIL_FONT = None
_EMOJI_PIL_TRIED = False


def _emoji_pil_font(size):
    global _EMOJI_PIL_FONT, _EMOJI_PIL_TRIED
    if not _EMOJI_PIL_TRIED:
        _EMOJI_PIL_TRIED = True
        try:
            from PIL import ImageFont
            for fp in (r"C:\Windows\Fonts\seguiemj.ttf",
                       "/System/Library/Fonts/Apple Color Emoji.ttc",
                       "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"):
                if os.path.isfile(fp):
                    _EMOJI_PIL_FONT = ImageFont.truetype(fp, size)
                    break
        except Exception:
            _EMOJI_PIL_FONT = None
    return _EMOJI_PIL_FONT


def render_emoji_png(emoji, size, out_path):
    """Rasterize `emoji` in color to a transparent PNG. Returns True on
    success (False -> caller falls back to the text glyph)."""
    try:
        from PIL import Image, ImageDraw
        font = _emoji_pil_font(size * 2)   # 2x then downscale = crisper
        if font is None:
            return False
        img = Image.new("RGBA", (size * 2 + 8, size * 2 + 8), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            draw.text((4, 4), emoji, font=font, embedded_color=True)
        except Exception:
            return False
        bbox = img.getbbox()
        if not bbox:
            return False
        img = img.crop(bbox)
        img.thumbnail((size, size), Image.NEAREST)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - img.width) // 2,
                           (size - img.height) // 2), img)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        canvas.save(out_path)
        return True
    except Exception:
        return False


def emoji_photo(emoji, size=18, root=None):
    """tk.PhotoImage of a COLOR emoji (transparent background), cached per
    (emoji, size). Returns None when color rendering is unavailable -- the
    caller should fall back to the plain text glyph."""
    if not emoji:
        return None
    key = (emoji, size)
    if key in _EMOJI_PHOTO_CACHE:
        return _EMOJI_PHOTO_CACHE[key]
    import tkinter as tk
    try:
        if root is None:
            root = tk._get_default_root()
    except Exception:
        return None
    if root is None:
        return None
    slug = "".join("{:04X}".format(ord(c)) for c in emoji)
    path = os.path.join(_settings_dir(), "emoji_cache",
                        "e{}_{}.png".format(slug, size))
    if not os.path.isfile(path):
        if not render_emoji_png(emoji, size, path):
            return None
    try:
        photo = tk.PhotoImage(file=path, master=root)
        _EMOJI_PHOTO_CACHE[key] = photo
        return photo
    except Exception:
        return None


# ===========================================================================
# THEME / FONT SWITCHING
# ===========================================================================
def current_theme():
    return THEME_BY_ID.get(current_theme_id, THEMES[0])


def set_theme(theme_id):
    """Switch the live palette. Returns the old->new color map for
    retint(). Call apply_styles(root) + retint(root) afterwards."""
    global current_theme_id, _prev_palette
    global BG_MAIN, BG_WINDOW, BG_PANEL, BG_CARD, BG_INPUT
    global BORDER, BORDER_LIGHT, ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED
    global ACCENT_ORANGE, ACCENT_PURPLE, TEXT_MAIN, TEXT_DIM, OK_COLOR

    t = THEME_BY_ID.get(theme_id)
    if not t or t["id"] == current_theme_id:
        # unknown id, or the theme is already active: switching to the
        # same palette must NOT trigger the caller's full restyle (and its
        # repaint cascade) -- return falsy so the work is skipped entirely.
        return {}
    new = _palette_dict(t)
    old = _palette_dict(current_theme())
    _prev_palette = {old[k]: new[k] for k in _PALETTE_KEYS}

    current_theme_id = t["id"]
    BG_MAIN = new["bg_body"]
    BG_WINDOW = new["bg_window"]
    BG_PANEL = new["bg_sidebar"]
    BG_CARD = new["bg_card"]
    BG_INPUT = new["bg_input"]
    BORDER = new["border"]
    BORDER_LIGHT = new["border_light"]
    ACCENT_BLUE = new["accent"]
    ACCENT_GREEN = new["accent_green"]
    ACCENT_RED = new["accent_red"]
    ACCENT_ORANGE = new["accent_amber"]
    ACCENT_PURPLE = new["accent_violet"]
    TEXT_MAIN = new["text_main"]
    TEXT_DIM = new["text_secondary"]
    OK_COLOR = new["accent_green"]

    _settings["theme"] = current_theme_id
    _save_settings()
    return dict(_prev_palette)


def refresh_font():
    """Re-resolve the OS default font and repaint. Kept as the equivalent
    hook the old font-switcher used to call; there is nothing to choose
    anymore, so this only matters if the OS's own default font can change
    out from under a running app (rare, but harmless to support)."""
    _family_cache["family"] = None
    resolve_font_family()
    # forget what is baked into the cached objects so refresh_all_fonts()
    # genuinely re-applies the (possibly new) family everywhere
    _font_applied.clear()
    refresh_all_fonts()


# ===========================================================================
# RETHEME HOOKS + LIVE RETINT
# ===========================================================================
_retheme_hooks = []

# tk (non-ttk) color options the walker remaps.
_TK_COLOR_OPTS = ("background", "foreground", "highlightbackground",
                  "highlightcolor", "insertbackground", "selectbackground",
                  "selectforeground", "activebackground", "activeforeground",
                  "disabledforeground", "readonlybackground")


def add_retheme_hook(fn):
    """Register a zero-arg callback invoked after every theme/font/size
    switch (used for canvas art the style engine can't reach: the header
    grid, FR plots, audit tree tag colors, menus...)."""
    if fn not in _retheme_hooks:
        _retheme_hooks.append(fn)


def remove_retheme_hook(fn):
    try:
        _retheme_hooks.remove(fn)
    except ValueError:
        pass


def run_retheme_hooks():
    for fn in list(_retheme_hooks):
        try:
            fn()
        except Exception:
            pass


def retint(root):
    """Walk every tk (non-ttk) widget under root and remap option values
    that match the previous palette to the current palette. ttk widgets
    are skipped -- they restyle via apply_styles()."""
    mapping = _prev_palette or {}
    if not mapping:
        return
    norm = {str(k).lower(): v for k, v in mapping.items()}
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            children = w.winfo_children()
        except Exception:
            children = []
        stack.extend(children)
        if isinstance(w, ttk_Widget):
            continue
        for opt in _TK_COLOR_OPTS:
            try:
                val = w.cget(opt)
            except Exception:
                continue
            new = norm.get(str(val).lower())
            if new:
                try:
                    w[opt] = new
                except Exception:
                    pass


# Imported late so the walker can detect ttk widgets without a hard cycle
# at module import (tkinter import cost is already paid by consumers).
from tkinter import ttk as _ttk_mod
ttk_Widget = _ttk_mod.Widget


# ===========================================================================
# STYLE SHEET -- the retro "IEM Tool" ttk theme
# ===========================================================================
CARD_SHADOW = 4             # px of hard black offset shadow behind cards
GRID_CELL = 24              # px cell size for the header blueprint grid


def bind_dynamic_wrap(label, source=None, pad=16, min_wrap=120):
    """Keep a Label's wraplength tied to its actual container width instead
    of a fixed pixel guess. A fixed wraplength= only fits the window size
    it was written for; on any narrower window/tab the label wraps at the
    old width regardless of how little room it's actually been given, so
    its parent frame (packed/gridded to hug its children) grows past the
    visible pane and drags the whole tab wider than the window -- e.g. the
    Import/Export tabs' "Browse..." buttons and hint text spilling off the
    right edge on a half-screen snap.

    `source` is the widget whose width should drive the wrap (usually the
    label's own parent/card, since the label itself hasn't been laid out
    yet the first time this runs). Defaults to the label's parent."""
    target = source if source is not None else label.master

    def _update(_event=None):
        w = target.winfo_width()
        if w > 1:
            label.configure(wraplength=max(min_wrap, w - pad))
    target.bind("<Configure>", _update, add="+")
    label.after(60, _update)
    return _update


def make_card(parent, style="Card.TFrame"):
    """IEM Tool-style card: a hard black outer frame with the card surface
    packed inside leaving CARD_SHADOW px visible at the right/bottom --
    the same '1px border + 4px 4px 0px 0px #000000' offset-shadow look the
    main app draws with CSS. Returns (outer, card); pack/grid the OUTER,
    build content inside the card frame.

    NOTE: widgets cannot be re-parented in Tk, and packing an existing
    widget into a SIBLING frame via pack(in_=...) silently breaks painting
    (the widget reports as mapped but never renders). Panels that should
    sit in a shadow card must be constructed with the card frame as their
    parent -- use the borderless "CardFlat.TFrame" style for the panel
    itself so the border is not doubled."""
    outer = tk.Frame(parent, background=BORDER, highlightthickness=0, bd=0)
    card = _ttk_mod.Frame(outer, style=style)
    card.pack(fill="both", expand=True,
              padx=(0, CARD_SHADOW), pady=(0, CARD_SHADOW))
    return outer, card


_style = None     # live ttk.Style reference (set by apply_styles)


def set_tab_pad(px):
    """Responsive tab sizing: shrink/grow tab padding as the window
    resizes so all tabs stay visible without clipping."""
    if _style is None:
        return
    for name in ("TNotebook.Tab", "Card.TNotebook.Tab"):
        try:
            _style.configure(name, padding=(px, 8))
            _style.map(name,
                       padding=[("selected", (px, 8)), ("active", (px, 8))],
                       expand=[("selected", (0, 0, 0, 0))])
        except Exception:
            pass


def apply_styles(root):
    """(Re)configure every ttk style + root-wide font defaults from the
    current palette (theme switch). Safe to call repeatedly."""
    fam = resolve_font_family(root)
    # re-point every previously-handed-out Font object at the (possibly
    # new) family/scale BEFORE building the ttk style fonts below, so raw
    # tk widgets (Listbox, Canvas, tooltips, the header title...) update
    # in the same call as the ttk style sheet -- one visible repaint, not
    # a partial one followed by a delayed second pass.
    refresh_all_fonts()
    base = font(FONT_BASE_PX)
    small = font(FONT_SMALL_PX)
    header = font(FONT_HEADER_PX, "bold")
    base_bold = font(FONT_BASE_PX, "bold")

    root.option_add("*Font", base)
    # ttk.Combobox popdowns are Tk listboxes: theme them via options.
    root.option_add("*TCombobox*Listbox.background", BG_INPUT)
    root.option_add("*TCombobox*Listbox.foreground", TEXT_MAIN)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_BLUE)
    root.option_add("*TCombobox*Listbox.selectForeground",
                    contrast_text(ACCENT_BLUE))
    root.option_add("*TCombobox*Listbox.font", small)
    root.option_add("*TCombobox*Listbox.relief", "flat")
    root.option_add("*TCombobox*Listbox.borderWidth", 1)

    style = _ttk_mod.Style(root)
    global _style
    _style = style
    try:
        style.theme_use("clam")
    except Exception:
        pass

    sel_fg = contrast_text(ACCENT_BLUE)

    style.configure(".", background=BG_MAIN, foreground=TEXT_MAIN,
                    fieldbackground=BG_INPUT, bordercolor=BORDER,
                    darkcolor=BORDER, lightcolor=BORDER, font=base)

    # -- surfaces ----------------------------------------------------------
    style.configure("TFrame", background=BG_MAIN)
    style.configure("Panel.TFrame", background=BG_PANEL)
    style.configure("Card.TFrame", background=BG_CARD, relief="solid",
                    borderwidth=1, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    # borderless card surface: for panels that live INSIDE a make_card()
    # shadow card (the card supplies the border + offset shadow)
    style.configure("CardFlat.TFrame", background=BG_CARD, relief="flat",
                    borderwidth=0)

    # -- labels ------------------------------------------------------------
    style.configure("TLabel", background=BG_MAIN, foreground=TEXT_MAIN,
                    font=base)
    style.configure("Panel.TLabel", background=BG_PANEL, foreground=TEXT_MAIN)
    style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT_MAIN)
    style.configure("Dim.TLabel", background=BG_MAIN, foreground=TEXT_DIM)
    style.configure("Header.TLabel", background=BG_PANEL,
                    foreground=ACCENT_BLUE, font=header)
    style.configure("CardHeader.TLabel", background=BG_CARD,
                    foreground=ACCENT_BLUE, font=base_bold)
    style.configure("Status.TLabel", background=BG_PANEL,
                    foreground=TEXT_DIM, font=small)

    # -- buttons: flat blocks with a visible outline, accent hover ----------
    # The outline is a mid grey (derived from the palette) so buttons stay
    # visible on every surface in every theme; pure black borders vanish
    # against the dark toolbars.
    btn_edge = blend(BG_CARD, TEXT_MAIN, 0.38)

    style.configure("TButton", background=BG_CARD, foreground=TEXT_MAIN,
                    bordercolor=btn_edge, lightcolor=btn_edge,
                    darkcolor=btn_edge,
                    focusthickness=1, padding=(10, 6), font=base,
                    relief="flat")
    style.map("TButton",
              background=[("disabled", BG_CARD), ("pressed", ACCENT_BLUE),
                          ("active", ACCENT_BLUE)],
              foreground=[("disabled", TEXT_DIM), ("pressed", sel_fg),
                          ("active", sel_fg)],
              bordercolor=[("disabled", BORDER)],
              lightcolor=[("disabled", BORDER)],
              darkcolor=[("disabled", BORDER)])

    style.configure("Accent.TButton", background=ACCENT_ORANGE,
                    foreground=contrast_text(ACCENT_ORANGE),
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(10, 6), font=base_bold, relief="flat")
    style.map("Accent.TButton",
              background=[("disabled", BG_CARD), ("pressed", lighten(ACCENT_ORANGE, 0.18)),
                          ("active", lighten(ACCENT_ORANGE, 0.18))],
              foreground=[("disabled", TEXT_DIM),
                          ("pressed", contrast_text(ACCENT_ORANGE)),
                          ("active", contrast_text(ACCENT_ORANGE))])

    style.configure("Danger.TButton", background=ACCENT_RED,
                    foreground=contrast_text(ACCENT_RED),
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(10, 6), font=base, relief="flat")
    style.map("Danger.TButton",
              background=[("disabled", BG_CARD), ("pressed", lighten(ACCENT_RED, 0.18)),
                          ("active", lighten(ACCENT_RED, 0.18))],
              foreground=[("disabled", TEXT_DIM),
                          ("pressed", contrast_text(ACCENT_RED)),
                          ("active", contrast_text(ACCENT_RED))])

    style.configure("Blue.TButton", background=ACCENT_BLUE,
                    foreground=sel_fg, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER,
                    padding=(10, 6), font=base, relief="flat")
    style.map("Blue.TButton",
              background=[("disabled", BG_CARD), ("pressed", lighten(ACCENT_BLUE, 0.18)),
                          ("active", lighten(ACCENT_BLUE, 0.18))],
              foreground=[("disabled", TEXT_DIM), ("pressed", sel_fg),
                          ("active", sel_fg)])

    # -- compact action-button variants ------------------------------------
    # Same colors/hover behavior as their full-size siblings above, but:
    # - width=0 removes the hidden ~11-character minimum width Tk 9 ships
    #   as the button default (short labels used to render ~83px wide no
    #   matter what), so buttons hug their text;
    # - padding is slightly tighter.
    # Used where many actions must share a single row (the Audit tab's
    # seven-button toolbar) so it fits even at half-screen snap.
    style.configure("Compact.TButton", padding=(7, 5), width=0)
    style.configure("Accent.Compact.TButton",
                    background=ACCENT_ORANGE,
                    foreground=contrast_text(ACCENT_ORANGE),
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(7, 5), font=base_bold, relief="flat", width=0)
    style.map("Accent.Compact.TButton",
              background=[("disabled", BG_CARD), ("pressed", lighten(ACCENT_ORANGE, 0.18)),
                          ("active", lighten(ACCENT_ORANGE, 0.18))],
              foreground=[("disabled", TEXT_DIM),
                          ("pressed", contrast_text(ACCENT_ORANGE)),
                          ("active", contrast_text(ACCENT_ORANGE))])
    style.configure("Blue.Compact.TButton", background=ACCENT_BLUE,
                    foreground=sel_fg, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER,
                    padding=(7, 5), relief="flat", width=0)
    style.map("Blue.Compact.TButton",
              background=[("disabled", BG_CARD), ("pressed", lighten(ACCENT_BLUE, 0.18)),
                          ("active", lighten(ACCENT_BLUE, 0.18))],
              foreground=[("disabled", TEXT_DIM), ("pressed", sel_fg),
                          ("active", sel_fg)])

    # green checkmark toast (clipboard-copy feedback, Export tab): ttk
    # buttons have no widget-level foreground, so the color swap is a
    # dedicated style the button switches to for a second
    style.configure("Compact.Toast.TButton", padding=(7, 5), width=0,
                    foreground=ACCENT_GREEN)
    style.map("Compact.Toast.TButton",
              foreground=[("pressed", ACCENT_GREEN), ("active", ACCENT_GREEN)])

    # toast variant for ACCENT (orange) buttons: the whole block flashes
    # green with white text, then reverts to the orange Accent look
    style.configure("Accent.Toast.TButton",
                    background=ACCENT_GREEN,
                    foreground=contrast_text(ACCENT_GREEN),
                    padding=(10, 6), font=base_bold, relief="flat",
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("Accent.Toast.TButton",
              background=[("pressed", ACCENT_GREEN), ("active", ACCENT_GREEN)],
              foreground=[("pressed", contrast_text(ACCENT_GREEN)),
                          ("active", contrast_text(ACCENT_GREEN))])

    # -- inputs ------------------------------------------------------------
    # font= set explicitly on every input style rather than left to
    # inherit ttk's built-in class default: relying on inheritance worked
    # by coincidence (TEntry/TCombobox/TSpinbox happen to fall back to the
    # same font ttk uses elsewhere), but it's not guaranteed across Tk
    # versions/platforms, and being explicit costs nothing.
    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_MAIN,
                    insertcolor=TEXT_MAIN, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER,
                    selectbackground=ACCENT_BLUE, selectforeground=sel_fg,
                    padding=(5, 4), font=base)
    style.map("TEntry",
              fieldbackground=[("disabled", BG_CARD), ("readonly", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)],
              selectbackground=[("focus", ACCENT_BLUE)],
              selectforeground=[("focus", sel_fg)])

    style.configure("TCombobox", fieldbackground=BG_INPUT,
                    foreground=TEXT_MAIN, background=BG_CARD,
                    arrowcolor=TEXT_MAIN, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(5, 4),
                    font=base)
    style.map("TCombobox",
              fieldbackground=[("readonly", BG_INPUT), ("disabled", BG_CARD)],
              foreground=[("readonly", TEXT_MAIN), ("disabled", TEXT_DIM)],
              arrowcolor=[("disabled", TEXT_DIM)])

    style.configure("TSpinbox", fieldbackground=BG_INPUT,
                    foreground=TEXT_MAIN, insertcolor=TEXT_MAIN,
                    arrowcolor=TEXT_MAIN, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER,
                    selectbackground=ACCENT_BLUE, selectforeground=sel_fg,
                    padding=(4, 4), font=base)
    style.map("TSpinbox",
              fieldbackground=[("disabled", BG_CARD)],
              foreground=[("disabled", TEXT_DIM)])

    # -- checkbuttons (indicator: dark box, accent when checked) -----------
    style.configure("TCheckbutton", background=BG_MAIN, foreground=TEXT_MAIN,
                    focuscolor=BG_MAIN, font=base, indicatorcolor=BG_INPUT,
                    indicatormargin=0, padding=2)
    style.map("TCheckbutton",
              background=[("active", BG_MAIN)],
              indicatorcolor=[("selected", ACCENT_BLUE), ("!selected", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)])
    style.configure("Card.TCheckbutton", background=BG_CARD,
                    foreground=TEXT_MAIN, focuscolor=BG_CARD, font=base,
                    indicatorcolor=BG_INPUT, indicatormargin=0, padding=2)
    style.map("Card.TCheckbutton",
              background=[("active", BG_CARD)],
              indicatorcolor=[("selected", ACCENT_BLUE), ("!selected", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)])
    style.configure("Panel.TCheckbutton", background=BG_PANEL,
                    foreground=TEXT_MAIN, focuscolor=BG_PANEL, font=base,
                    indicatorcolor=BG_INPUT, indicatormargin=0, padding=2)
    style.map("Panel.TCheckbutton",
              background=[("active", BG_PANEL)],
              indicatorcolor=[("selected", ACCENT_BLUE), ("!selected", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)])

    # -- radiobuttons (same treatment; used by the merge dialog) -----------
    style.configure("TRadiobutton", background=BG_MAIN, foreground=TEXT_MAIN,
                    focuscolor=BG_MAIN, font=base, indicatorcolor=BG_INPUT,
                    indicatormargin=0, padding=2)
    style.map("TRadiobutton",
              background=[("active", BG_MAIN)],
              indicatorcolor=[("selected", ACCENT_BLUE), ("!selected", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)])
    style.configure("Card.TRadiobutton", background=BG_CARD,
                    foreground=TEXT_MAIN, focuscolor=BG_CARD, font=base,
                    indicatorcolor=BG_INPUT, indicatormargin=0, padding=2)
    style.map("Card.TRadiobutton",
              background=[("active", BG_CARD)],
              indicatorcolor=[("selected", ACCENT_BLUE), ("!selected", BG_INPUT)],
              foreground=[("disabled", TEXT_DIM)])

    # -- treeview ------------------------------------------------------------
    style.configure("Treeview", background=BG_INPUT, fieldbackground=BG_INPUT,
                    foreground=TEXT_MAIN, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, font=base,
                    rowheight=FONT_BASE_PX + 12)
    style.configure("Treeview.Heading", background=BG_CARD,
                    foreground=ACCENT_BLUE, font=base_bold,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    relief="flat", padding=(6, 6))
    style.map("Treeview",
              background=[("selected", ACCENT_BLUE)],
              foreground=[("selected", sel_fg)])
    style.map("Treeview.Heading", background=[("active", BG_CARD)])

    # -- notebook tabs -------------------------------------------------------
    # Tabs are flat blocks: the selected tab is highlighted with the accent
    # and NEVER changes size or shows the dashed keyboard-focus ring.
    # - padding is mapped to the SAME value for every state (clam otherwise
    #   alters tab geometry on selection)
    # - focuscolor is mapped to the tab's own background so the dashed
    #   focus outline that appears on click is invisible
    style.configure("TNotebook", background=BG_MAIN, bordercolor=BG_MAIN,
                    lightcolor=BG_MAIN, darkcolor=BG_MAIN, tabmargins=(8, 6, 8, 0))
    style.configure("TNotebook.Tab", background=BG_PANEL,
                    foreground=TEXT_DIM, padding=(14, 8), font=base_bold,
                    bordercolor=BG_PANEL, lightcolor=BG_PANEL,
                    darkcolor=BG_PANEL, focuscolor=BG_PANEL)
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT_BLUE), ("active", BG_CARD)],
              foreground=[("selected", sel_fg), ("active", TEXT_MAIN)],
              padding=[("selected", (14, 8)), ("active", (14, 8))],
              focuscolor=[("selected", ACCENT_BLUE), ("active", BG_CARD)],
              expand=[("selected", (0, 0, 0, 0))])

    # Notebook variant that sits INSIDE a shadow card (tab strip blends
    # with the card surface instead of the body background).
    style.configure("Card.TNotebook", background=BG_CARD, bordercolor=BG_CARD,
                    lightcolor=BG_CARD, darkcolor=BG_CARD, tabmargins=(4, 4, 4, 0))
    style.configure("Card.TNotebook.Tab", background=BG_PANEL,
                    foreground=TEXT_DIM, padding=(14, 8), font=base_bold,
                    bordercolor=BG_PANEL, lightcolor=BG_PANEL,
                    darkcolor=BG_PANEL, focuscolor=BG_PANEL)
    style.map("Card.TNotebook.Tab",
              background=[("selected", ACCENT_BLUE), ("active", BG_INPUT)],
              foreground=[("selected", sel_fg), ("active", TEXT_MAIN)],
              padding=[("selected", (14, 8)), ("active", (14, 8))],
              focuscolor=[("selected", ACCENT_BLUE), ("active", BG_INPUT)],
              expand=[("selected", (0, 0, 0, 0))])

    # -- scrollbars: solid flat bars like IEM Tool. The arrow buttons are
    # removed via a custom layout (trough + thumb only). Thickness is
    # driven by -arrowsize (even with no arrow elements in the layout) and
    # follows the UI font scale so bars stay chunky at every size.
    scroll_thumb = blend(BG_INPUT, TEXT_MAIN, 0.30)
    sb_thick = 14
    for orient in ("Vertical", "Horizontal"):
        name = "{}.TScrollbar".format(orient)
        try:
            style.layout(name, [
                ("{}.Scrollbar.trough".format(orient), {"children":
                    [("{}.Scrollbar.thumb".format(orient),
                      {"expand": "1", "sticky": "nswe"})],
                 "sticky": "ns" if orient == "Vertical" else "we"})])
        except Exception:
            pass    # layout already replaced (re-theme call)
        style.configure(name, background=scroll_thumb, troughcolor=BG_MAIN,
                        bordercolor=BORDER, lightcolor=scroll_thumb,
                        darkcolor=scroll_thumb, gripcount=0, relief="flat",
                        arrowsize=sb_thick)
        style.map(name,
                  background=[("pressed", ACCENT_BLUE), ("active", scroll_thumb)])

    # -- paned sash ----------------------------------------------------------
    style.configure("TPanedwindow", background=BG_MAIN, bordercolor=BORDER)
    try:
        style.configure("TPanedwindow", sashthickness=8)
    except Exception:
        pass

    # -- menubutton-based dropdown pickers ------------------------------------
    style.configure("TMenubutton", background=BG_CARD, foreground=TEXT_MAIN,
                    arrowcolor=TEXT_MAIN, bordercolor=btn_edge,
                    lightcolor=btn_edge, darkcolor=btn_edge, relief="flat",
                    padding=(8, 4), font=base)
    style.map("TMenubutton",
              background=[("pressed", BG_INPUT), ("active", ACCENT_BLUE)],
              foreground=[("pressed", TEXT_MAIN), ("active", sel_fg)],
              arrowcolor=[("pressed", TEXT_MAIN), ("active", sel_fg)])
    return fam


def style_menu(menu):
    """Apply the retro palette to a tk.Menu (the ttk engine can't style
    native menus). Call for the menubar and every cascade/dropdown."""
    menu.configure(background=BG_PANEL, foreground=TEXT_MAIN,
                   activebackground=ACCENT_BLUE,
                   activeforeground=contrast_text(ACCENT_BLUE),
                   borderwidth=1, relief="flat",
                   disabledforeground=TEXT_DIM, font=font(FONT_BASE_PX))
    return menu


# ---------------------------------------------------------------------------
# STARTUP STATE: apply the persisted theme to the module palette right now,
# so the very first apply_styles() already uses the saved colors. The
# prev-palette snapshot is cleared because no widgets exist yet -- they will
# be BUILT with this palette, so there is no old->new shift to retint.
# ---------------------------------------------------------------------------
set_theme(_settings.get("theme") if _settings.get("theme") in THEME_BY_ID
          else "slate")
_prev_palette = None