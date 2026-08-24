"""
theme.py -- shared colors, fonts, and font pickers for the Database Tool
UI. Imported by main.py and by the panel modules (tools_panel,
curve_import) so every surface stays visually consistent.
"""

BG_MAIN = "#12141c"
BG_PANEL = "#181b26"
BG_CARD = "#1f2330"
BG_INPUT = "#0e1017"
BORDER = "#33405a"
BORDER_LIGHT = "#4a5f85"
ACCENT_BLUE = "#5b9bd9"
ACCENT_ORANGE = "#e8963c"
ACCENT_PURPLE = "#8a6ae8"
ACCENT_GREEN = "#4fbf82"
ACCENT_RED = "#e05a52"
TEXT_MAIN = "#e7e9f0"
TEXT_DIM = "#8892a8"

PREFERRED_FONTS = ["Consolas", "Courier New", "DejaVu Sans Mono", "Menlo", "Monaco"]

# Curve-import status badges reuse the suite's OK/SKIP/FAILED palette.
OK_COLOR = ACCENT_GREEN


def pick_emoji_font():
    """Prefer an OS font with COLOR emoji glyphs. The app's monospace fonts
    (Consolas etc.) contain no emoji, which is why emojis previously showed
    as monochrome outlines -- tk falls back inconsistently. Rendering them
    in an explicit emoji font restores color on Win10/11+."""
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
        for f in ("Segoe UI Emoji",       # Windows 10/11 (color)
                  "Apple Color Emoji",    # macOS
                  "Noto Color Emoji"):    # Linux
            if f in families:
                return f
    except Exception:
        pass
    return None


_FONT_FAMILY_CACHE = None


def pick_font_family():
    """Resolve once, then cache: tkfont.families() enumerates every font on
    the system and this is called per widget build AND per right-click menu /
    tooltip show -- re-enumerating each time adds avoidable UI latency."""
    global _FONT_FAMILY_CACHE
    if _FONT_FAMILY_CACHE:
        return _FONT_FAMILY_CACHE
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
        for f in PREFERRED_FONTS:
            if f in families:
                _FONT_FAMILY_CACHE = f
                return f
        # enumeration succeeded but no preferred font exists: the fallback
        # is then a stable property of this system, so cache it too
        _FONT_FAMILY_CACHE = "Courier"
    except Exception:
        # no default root yet (or Tk not ready): retry on a later call
        return "Courier"
    return _FONT_FAMILY_CACHE


def mono_font():
    return (pick_font_family(), 10)
