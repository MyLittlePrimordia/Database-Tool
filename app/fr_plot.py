"""
fr_plot.py -- lightweight FR curve plotting for the Database Tool.

A pure-tkinter canvas plotter (no matplotlib / zero new dependencies) used
by the Editor's "FR PREVIEW" card and the Import Curves tab's
"CURVE PREVIEW" card:

  - log-frequency X axis, fixed 20 Hz - 20 kHz view
  - Y axis is dB relative to each curve's OWN 1 kHz level (same reference
    the tonal tag analyzer uses), window -30 .. +15 dB
  - per-pixel min/max decimation keeps treble spikes visible at any width
  - parsed curves are cached by (path, mtime, size) so browsing lists only
    pays a redraw, not a re-parse

The widget itself stays dumb: callers hand it finished series dicts
({"name", "pts", "color", "width", "dash"}) plus an optional dashed average
curve. Helper functions here (get_curve_points / normalized / average /
dim) do the parsing, normalization and styling math.
"""

import os
import bisect
import math
import tkinter as tk

from theme import (BG_INPUT, BORDER, BORDER_LIGHT, TEXT_DIM, TEXT_MAIN,
                   ACCENT_BLUE, ACCENT_GREEN, ACCENT_ORANGE, ACCENT_PURPLE,
                   ACCENT_RED, pick_font_family)

FMIN = 20.0
FMAX = 20000.0
DB_MIN = -30.0
DB_MAX = 20.0

# log-frequency mapping constants (squig.link style: each decade of
# 20-100-1k-10k gets an equal third of the plot width)
_LOG_MIN = math.log10(FMIN)
_LOG_SPAN = math.log10(FMAX) - _LOG_MIN

FREQ_TICKS = [(20, "20"), (50, "50"), (100, "100"), (200, "200"),
              (500, "500"), (1000, "1k"), (2000, "2k"), (5000, "5k"),
              (10000, "10k"), (20000, "20k")]


def f_to_frac(f):
    """Frequency -> 0..1 position on the log axis."""
    t = (math.log10(max(float(f), FMIN)) - _LOG_MIN) / _LOG_SPAN
    return max(0.0, min(1.0, t))


def frac_to_f(t):
    """0..1 position -> frequency (inverse of f_to_frac)."""
    t = max(0.0, min(1.0, t))
    return 10.0 ** (_LOG_MIN + t * _LOG_SPAN)

PALETTE = [ACCENT_BLUE, ACCENT_GREEN, ACCENT_ORANGE, ACCENT_PURPLE, ACCENT_RED]

# parsed-curve cache: abspath -> (mtime_ns, size, [(f, db), ...])
_CACHE = {}
_CACHE_MAX = 96


# ---------------------------------------------------------------------------
# color helpers
# ---------------------------------------------------------------------------
def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend(a, b, t):
    """Mix hex color a toward b by t (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(x + (y - x) * t))) for x, y in zip(ra, rb))


def dim(color, t=0.55):
    """Fade a curve color into the plot background (fake alpha for tk)."""
    return blend(color, BG_INPUT, t)


# ---------------------------------------------------------------------------
# curve math
# ---------------------------------------------------------------------------
def ref_db(points):
    """Reference level at ~1 kHz: median of the 900-1100 Hz band, falling
    back to the overall median so odd files still render."""
    vals = sorted(d for f, d in points if 900 <= f <= 1100)
    if not vals:
        vals = sorted(d for _, d in points)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def normalized(points):
    """Clip to the display range and re-reference at 1 kHz. Keeps order
    (input must be frequency-sorted; parse_curve_file guarantees that)."""
    pts = [(f, d) for f, d in points if FMIN <= f <= FMAX]
    r = ref_db(pts)
    if r is None:
        return []
    return [(f, d - r) for f, d in pts]


def get_curve_points(path, max_points=200000):
    """Parse any supported measurement file into [(freq, spl)] with caching.
    Returns [] when unreadable / no data rows. Huge inputs are strided down
    to max_points before caching."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    hit = _CACHE.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        import curve_logic as CL   # lazy: keeps this module import-light
        rows = CL.parse_curve_file(path)
    except Exception:              # noqa: BLE001 - a bad file just plots empty
        rows = []
    pts = [(f, s) for f, s, _phase in rows]
    if len(pts) > max_points:
        step = len(pts) / float(max_points)
        pts = [pts[int(i * step)] for i in range(max_points)]
    if len(_CACHE) >= _CACHE_MAX:
        # FIFO eviction of the oldest entry (dicts preserve insertion
        # order): clearing wholesale thrashed the cache when browsing
        # more than _CACHE_MAX distinct files.
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[path] = (st.st_mtime_ns, st.st_size, pts)
    return pts


def _interp_at(freqs, dbs, f):
    j = bisect.bisect_left(freqs, f)
    if j <= 0:
        return dbs[0]
    if j >= len(freqs):
        return dbs[-1]
    f0, f1 = freqs[j - 1], freqs[j]
    if f1 == f0:
        return dbs[j - 1]
    return dbs[j - 1] + (dbs[j] - dbs[j - 1]) * (f - f0) / (f1 - f0)


def average(curves):
    """Mean of already-normalized curves on the first curve's grid,
    restricted to the band covered by EVERY curve. Linear interpolation
    for the others; nothing is extrapolated past a source's range."""
    curves = [c for c in curves if c]
    if not curves:
        return []
    lo = max(c[0][0] for c in curves)
    hi = min(c[-1][0] for c in curves)
    if lo > hi:
        return []
    arrs = [list(zip(*c)) for c in curves]          # [(freqs, dbs), ...]
    base_f, base_d = arrs[0]
    out = []
    for i, f in enumerate(base_f):
        if f < lo or f > hi:
            continue
        total = base_d[i]                            # exact sample on base
        for freqs, dbs in arrs[1:]:
            total += _interp_at(freqs, dbs, f)
        out.append((f, total / len(arrs)))
    return out


def average_raw(pts_a, pts_b):
    """Exact same averaging math convert_plan uses (mean of raw SPL on the
    first file's grid restricted to the common band), returned normalized
    for display."""
    try:
        import curve_logic as CL
    except Exception:                              # noqa: BLE001
        return []
    if not pts_a or not pts_b:
        return []
    lo = max(pts_a[0][0], pts_b[0][0])
    hi = min(pts_a[-1][0], pts_b[-1][0])
    if lo > hi:
        return []
    fa = [f for f, _ in pts_a if lo <= f <= hi]
    sa = [d for f, d in pts_a if lo <= f <= hi]
    fb = [f for f, _ in pts_b]
    sb = [d for _, d in pts_b]
    interp_b = CL.interp_spl(fb, sb, fa)
    return normalized([(f, (a + b) / 2.0) for f, a, b in zip(fa, sa, interp_b)])


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------
class CurvePlot(tk.Canvas):
    PAD_L = 46
    PAD_R = 12
    PAD_T = 10
    PAD_B = 24

    def __init__(self, parent, height=230, **kw):
        super().__init__(parent, bg=BG_INPUT, highlightthickness=1,
                         highlightbackground=BORDER, height=height, **kw)
        self._series = []
        self._avg = None
        self._msg = ""
        # (freqs, dbs, color) per solid series, precomputed once per redraw
        # so the <Motion> handler never re-zips full point lists per event.
        self._hover_data = []
        family = pick_font_family()
        self._font_small = (family, 8)
        self.bind("<Configure>", lambda e: self.redraw(), add="+")

    # public API -----------------------------------------------------------
    def set_data(self, series, avg=None, msg=""):
        """series: list of dicts {name, pts(normalized), color, width, dash}.
        avg: optional normalized points drawn dashed on top."""
        self._series = series or []
        self._avg = avg
        self._msg = msg or ""
        self.redraw()

    def clear(self, msg=""):
        self.set_data([], msg=msg)

    def redraw(self):
        self.delete("all")
        w = int(self.winfo_width())
        h = int(self.winfo_height())
        if w < 80 or h < 70:
            return
        x0, x1 = self.PAD_L, w - self.PAD_R
        y0, y1 = self.PAD_T, h - self.PAD_B

        def x_of(f):
            return x0 + f_to_frac(f) * (x1 - x0)

        def y_of(db):
            t = (DB_MAX - db) / (DB_MAX - DB_MIN)
            return y0 + max(0.0, min(1.0, t)) * (y1 - y0)

        self._draw_grid(w, h, x_of, y_of)

        if not self._series:
            self._hover_data = []
            self.create_text(
                (x0 + x1) // 2, (y0 + y1) // 2, text=self._msg,
                fill=TEXT_DIM, font=self._font_small, justify="center")
            return

        for s in self._series:
            coords = self._polyline(s["pts"], x_of, y_of, w)
            if len(coords) >= 4:
                kw = {"fill": s["color"], "width": s.get("width", 2),
                      "tags": ("curve",)}
                if s.get("dash"):
                    kw["dash"] = s["dash"]
                self.create_line(*coords, **kw)

        # hover readout data: solid (non-dashed) series only
        self._hover_data = []
        for s in self._series:
            if s.get("dash") or not s["pts"]:
                continue
            fs, ds = zip(*s["pts"])
            self._hover_data.append((fs, ds, s["color"]))

        if self._avg:
            coords = self._polyline(self._avg, x_of, y_of, w)
            if len(coords) >= 4:
                self.create_line(*coords, fill=TEXT_MAIN, width=1,
                                 dash=(6, 4), tags=("avg",))

        # hover readout lives with the cursor position, cheap and useful
        self._bind_hover(x_of, y_of)

    # internals -------------------------------------------------------------
    def _draw_grid(self, w, h, x_of, y_of):
        x0, x1 = self.PAD_L, w - self.PAD_R
        y0, y1 = self.PAD_T, h - self.PAD_B
        grid = "#232838"
        label_w = 34 if w < 430 else 0
        for i, (f, lab) in enumerate(FREQ_TICKS):
            px = x_of(f)
            self.create_line(px, y0, px, y1, fill=grid)
            skip = (i % 2 == 1) and label_w
            if not skip:
                self.create_text(px, y1 + 10, text=lab, fill=TEXT_DIM,
                                 font=self._font_small)
        for db in range(int(DB_MIN), int(DB_MAX) + 1, 5):
            py = y_of(db)
            major = (db == 0)
            self.create_line(x0, py, x1, py,
                             fill=BORDER_LIGHT if major else grid)
            # label every 5 dB like squig.link does
            self.create_text(x0 - 8, py, text="{:+d}".format(db) if db
                             else "0", fill=TEXT_DIM,
                             font=self._font_small, anchor="e")

    def _polyline(self, pts, x_of, y_of, width):
        """Per-pixel-column min/max envelope: preserves peaks while capping
        the coordinate list at ~2 points per pixel."""
        if not pts:
            return ()
        buckets = {}
        for f, d in pts:
            b = int(x_of(f))
            py = y_of(d)
            cur = buckets.get(b)
            if cur is None:
                buckets[b] = [py, py]
            elif py < cur[0]:
                cur[0] = py
            elif py > cur[1]:
                cur[1] = py
        coords = []
        prev = None
        for b in sorted(buckets):
            lo, hi = buckets[b]
            if prev is not None and b - prev > 1 and coords:
                coords.extend((coords[-2], coords[-1]))
            coords.append(b)
            coords.append(lo)
            if hi != lo:
                coords.append(b)
                coords.append(hi)
            prev = b
        return tuple(coords)

    def _bind_hover(self, x_of, y_of):
        """Crosshair readout: nearest curve value under the mouse."""
        if getattr(self, "_hover_bound", False):
            return
        self._hover_bound = True

        def _move(event):
            self.delete("hover")
            if not self._hover_data:
                return
            plot_w = max(1, int(self.winfo_width()) - self.PAD_L - self.PAD_R)
            f = frac_to_f((event.x - self.PAD_L) / plot_w)
            best = None
            for fs, ds, col in self._hover_data:
                if fs[0] <= f <= fs[-1]:
                    d = _interp_at(fs, ds, f)
                    if best is None or abs(d) < abs(best[1]):
                        best = (f, d, col)
            if best is None:
                return
            f, d, col = best
            px = x_of(f)
            self.create_line(px, self.PAD_T, px,
                             int(self.winfo_height()) - self.PAD_B,
                             fill="#333a4e", tags=("hover",))
            self.create_text(
                event.x, max(10, event.y - 12),
                text="{:.0f} Hz  {:+.1f} dB".format(f, d),
                fill=col, font=self._font_small, tags=("hover",),
                anchor="w" if event.x < int(self.winfo_width()) - 120 else "e")

        self.bind("<Motion>", _move, add="+")
        self.bind("<Leave>", lambda e: self.delete("hover"), add="+")
