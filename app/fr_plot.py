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

import collections
import os
import bisect
import math
import tkinter as tk

import theme

# Anti-aliased curve rendering: Tk's Canvas has no anti-aliasing primitive
# at all (create_line's smooth=True only rounds corners with more spline
# segments -- every line is still drawn one aliased/jagged pixel at a
# time), which is why curves looked thin and pixelated next to a browser
# tool like squig.link. Pillow (already a dependency -- see theme.py's
# emoji rendering) fixes this properly: draw each curve onto a
# transparent image at _SS times the target resolution, then downsample
# with LANCZOS, which anti-aliases every edge as a side effect. If Pillow
# isn't importable for some reason, we fall back to the old pure-Tk path
# below (_draw_curves_tk) so the app still works, just less smoothly.
try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL_OK = True
except Exception:                              # noqa: BLE001
    _PIL_OK = False

_SS = 3   # supersample factor for the Pillow-rendered curve path

FMIN = 20.0
FMAX = 20000.0
# Default Y window -- used only when there is no data to autoscale from
# (empty state). With data, the window adapts to the curve (see
# _nice_bounds) so curves fill the plot like squig.link instead of
# swimming in a fixed 50 dB frame.
DB_MIN = -30.0
DB_MAX = 20.0
DB_WINDOW_FLOOR = 20.0     # never zoom tighter than this many dB total
DB_PAD = 3.0               # headroom above/below the data, in dB

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


def _nice_bounds(lo, hi):
    """squig.link-style adaptive Y window: pad the data range, round
    outward to the nearest 5 dB and enforce a minimum span so a near-flat
    curve is not blown up into visual noise."""
    lo -= DB_PAD
    hi += DB_PAD
    if hi - lo < DB_WINDOW_FLOOR:
        mid = (hi + lo) / 2.0
        lo = mid - DB_WINDOW_FLOOR / 2.0
        hi = mid + DB_WINDOW_FLOOR / 2.0
    lo = max(-60.0, 5.0 * math.floor(lo / 5.0))
    hi = min(40.0, 5.0 * math.ceil(hi / 5.0))
    return lo, hi

# Fixed 10-color categorical palette (Okabe-Ito-inspired hues chosen for
# maximum mutual separation, incl. common color-vision types). Resolved at
# call time; on LIGHT themes every hue is darkened so curves keep contrast
# against pale surfaces (bright-on-dark, deep-on-light).
_PALETTE_BASE = ["#4fc3f7",   # sky blue
                 "#ffb74d",   # amber
                 "#81c784",   # green
                 "#e57373",   # red
                 "#ba68c8",   # purple
                 "#f06292",   # pink
                 "#4db6ac",   # teal
                 "#aed581",   # lime
                 "#7986cb",   # indigo
                 "#ffd54f"]   # yellow


def _bg_is_light():
    r, g, b = _rgb(theme.BG_INPUT)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0 > 0.5


def palette():
    """Curve colors, resolved at call time so theme switches re-palette
    the next redraw. Up to 10 distinct series before colors repeat."""
    if _bg_is_light():
        return [blend(c, "#000000", 0.35) for c in _PALETTE_BASE]
    return list(_PALETTE_BASE)

# parsed-curve cache: normcase abspath -> (mtime_ns, size, [(f, db), ...])
_CACHE = collections.OrderedDict()
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
    return blend(color, theme.BG_INPUT, t)


# ---------------------------------------------------------------------------
# curve math
# ---------------------------------------------------------------------------
def ref_db(points):
    """Reference level at ~1 kHz: median of the 900-1100 Hz band, falling
    back to 700-1300 Hz (same widening the analyzer uses), then to the
    overall median so odd files still render. L-10: plot and analysis now
    agree on when a 1 kHz reference exists; a file with neither band
    still plots (overall median) rather than rendering empty, but the
    widening step keeps the two paths in sync for every realistic file."""
    vals = sorted(d for f, d in points if 900 <= f <= 1100)
    if not vals:
        vals = sorted(d for f, d in points if 700 <= f <= 1300)
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


def get_curve_points(path, max_points=20000):
    """Parse any supported measurement file into [(freq, spl)] with caching.
    Returns [] when unreadable / no data rows. Huge inputs are strided down
    to max_points before caching (plots decimate per pixel column anyway,
    and averaging interpolates, so 20k points loses nothing visible while
    keeping worst-case cache memory bounded: 96 curves x 20k points)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = os.path.normcase(os.path.abspath(path))
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        try:
            _CACHE.move_to_end(key)
        except Exception:
            pass
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
        try:
            _CACHE.popitem(last=False)
        except Exception:
            _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = (st.st_mtime_ns, st.st_size, pts)
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


def smooth_octaves(pts, frac=1 / 12.0):
    """Fractional-octave display smoothing: each sample's dB becomes the
    mean over a +/- frac/2 octave window (the standard FR-tool treatment,
    e.g. squig.link's 1/12). Display-only -- never written back to files.
    Frequencies are preserved exactly; a flat curve is a fixed point."""
    n = len(pts)
    if n < 5:
        return list(pts)
    lfs = [math.log2(f) for f, _d in pts]
    half = frac / 2.0
    out = []
    lo = hi = 0
    for i, (f, _d) in enumerate(pts):
        while lfs[lo] < lfs[i] - half:
            lo += 1
        if hi <= i:
            hi = i + 1
        while hi < n and lfs[hi] <= lfs[i] + half:
            hi += 1
        total = 0.0
        for k in range(lo, hi):
            total += pts[k][1]
        out.append((f, total / (hi - lo)))
    return out


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
        super().__init__(parent, bg=theme.BG_INPUT, highlightthickness=1,
                         highlightbackground=theme.BORDER, height=height, **kw)
        self._series = []
        self._avg = None
        self._msg = ""
        # (freqs, dbs, color) per solid series, precomputed once per redraw
        # so the <Motion> handler never re-zips full point lists per event.
        self._hover_data = []
        self._font_small = theme.font(12)
        self._curve_photo = None    # keep a ref -- PhotoImage has no owner
        self.bind("<Configure>", lambda e: self.redraw(), add="+")
        # live retheme: redraw with the new palette (unhooked on destroy)
        theme.add_retheme_hook(self.redraw)

    def destroy(self):
        try:
            theme.remove_retheme_hook(self.redraw)
        except Exception:
            pass
        super().destroy()

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

        # adaptive Y window: autoscale to the plotted data (snapped to
        # 5 dB) so curves fill the vertical space like squig.link
        all_d = [d for s in self._series for _, d in s["pts"]]
        if self._avg:
            all_d += [d for _, d in self._avg]
        if all_d:
            db_min, db_max = _nice_bounds(min(all_d), max(all_d))
        else:
            db_min, db_max = DB_MIN, DB_MAX
        self._db_min, self._db_max = db_min, db_max
        span = max(1.0, db_max - db_min)

        def x_of(f):
            return x0 + f_to_frac(f) * (x1 - x0)

        def y_of(db):
            t = (db_max - db) / span
            return y0 + max(0.0, min(1.0, t)) * (y1 - y0)

        # hover crosshair always uses the CURRENT mapping (a resize rebuilds
        # these; see _bind_hover)
        self._x_of, self._y_of = x_of, y_of

        self._draw_grid(w, h, x_of, y_of)

        if not self._series:
            self._hover_data = []
            # width= wraps the message inside the plot area instead of
            # spilling across the full canvas on one line and overlapping
            # the Y-axis tick labels at the left/right margins.
            self.create_text(
                (x0 + x1) // 2, (y0 + y1) // 2, text=self._msg,
                fill=theme.TEXT_DIM, font=self._font_small, justify="center",
                width=max(120, (x1 - x0) - 20))
            return

        # self._avg (a separately-passed average curve -- curve_import.py's
        # preview uses this) is folded into one combined list alongside
        # self._series so both renderers below only need one code path.
        all_series = list(self._series)
        if self._avg:
            all_series.append({"name": "Average", "pts": self._avg,
                                "color": theme.TEXT_MAIN, "width": 3,
                                "dash": (7, 4)})

        if _PIL_OK:
            self._draw_curves_aa(all_series, x0, y0, x1, y1, x_of, y_of)
        else:
            self._draw_curves_tk(all_series, x_of, y_of, w)

        # hover readout data: solid (non-dashed) series only
        self._hover_data = []
        for s in all_series:
            if s.get("dash") or not s["pts"]:
                continue
            fs, ds = zip(*s["pts"])
            self._hover_data.append((fs, ds, s["color"]))

        # hover readout lives with the cursor position, cheap and useful
        self._bind_hover()

    def _draw_curves_tk(self, all_series, x_of, y_of, w):
        """Fallback path when Pillow isn't importable: the original
        pure-Tk renderer (no true anti-aliasing, just spline-rounded
        aliased segments)."""
        for s in all_series:
            coords = self._polyline(s["pts"], x_of, y_of, w)
            if len(coords) >= 4:
                kw = {"fill": s["color"], "width": s.get("width", 4),
                      "smooth": True, "splinesteps": 14, "tags": ("curve",),
                      "capstyle": "round", "joinstyle": "round"}
                if s.get("dash"):
                    kw["dash"] = s["dash"]
                self.create_line(*coords, **kw)

    def _draw_curves_aa(self, all_series, x0, y0, x1, y1, x_of, y_of):
        """Anti-aliased path: every curve is drawn onto one transparent
        Pillow image at _SS x the plot's pixel size, then downsampled
        with LANCZOS (this is what actually smooths the diagonal edges --
        see the _SS comment up top), and the result is blitted as a
        single image on top of the grid already drawn on the canvas."""
        pw, ph = max(1, x1 - x0), max(1, y1 - y0)
        img = Image.new("RGBA", (pw * _SS, ph * _SS), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for s in all_series:
            pts = self._envelope_points(s["pts"], x_of, y_of, x0, y0, _SS)
            if len(pts) < 2:
                continue
            width = max(1, int(round(s.get("width", 4) * _SS * 0.85)))
            color = _rgb(s["color"]) + (255,)
            dash = s.get("dash")
            if dash:
                self._draw_dashed(draw, pts, dash, color, width, _SS)
                continue
            try:
                draw.line(pts, fill=color, width=width, joint="curve")
            except TypeError:                  # older Pillow: no joint=
                draw.line(pts, fill=color, width=width)
            r = width / 2.0
            for px, py in (pts[0], pts[-1]):    # rounded end caps
                draw.ellipse((px - r, py - r, px + r, py + r), fill=color)
        small = img.resize((pw, ph), Image.LANCZOS)
        self._curve_photo = ImageTk.PhotoImage(small)
        self.create_image(x0, y0, anchor="nw", image=self._curve_photo,
                          tags=("curve",))

    def _envelope_points(self, pts, x_of, y_of, x0, y0, ss):
        """Same per-column min/max envelope idea as _polyline (preserves
        treble spikes while capping point count), but bucketed in the
        supersampled image's own coordinate space (image-local, i.e.
        shifted by the plot's x0/y0 origin) instead of native canvas
        pixels, so the extra resolution actually reaches the curve
        itself and not just the final downsample blur."""
        if not pts:
            return []
        buckets = {}
        for f, d in pts:
            bx = int((x_of(f) - x0) * ss)
            py = (y_of(d) - y0) * ss
            cur = buckets.get(bx)
            if cur is None:
                buckets[bx] = [py, py]
            elif py < cur[0]:
                cur[0] = py
            elif py > cur[1]:
                cur[1] = py
        out = []
        prev_bx = None
        for bx in sorted(buckets):
            lo, hi = buckets[bx]
            if prev_bx is not None and bx - prev_bx > ss and out:
                out.append((bx, out[-1][1]))     # bridge a sparse-data gap
            out.append((bx, lo))
            if hi != lo:
                out.append((bx, hi))
            prev_bx = bx
        return out

    def _draw_dashed(self, draw, pts, dash, color, width, ss):
        """Manually chop a polyline into dash/gap segments -- Pillow's
        ImageDraw.line has no dash= option -- scaled by the same ss
        factor as the points themselves so the dash pitch looks the same
        size regardless of the supersample factor."""
        on_len = dash[0] * ss
        off_len = dash[1] * ss if len(dash) > 1 else on_len
        on = True
        remaining = on_len
        seg = [pts[0]]
        for i in range(len(pts) - 1):
            x0_, y0_ = pts[i]
            x1_, y1_ = pts[i + 1]
            seg_len = math.hypot(x1_ - x0_, y1_ - y0_)
            t = 0.0
            while seg_len - t > 1e-6:
                step = min(remaining, seg_len - t)
                t += step
                remaining -= step
                frac = t / seg_len if seg_len else 1.0
                px = x0_ + (x1_ - x0_) * frac
                py = y0_ + (y1_ - y0_) * frac
                seg.append((px, py))
                if remaining <= 1e-6:
                    if on and len(seg) >= 2:
                        draw.line(seg, fill=color, width=width)
                    seg = [(px, py)]
                    on = not on
                    remaining = on_len if on else off_len
        if on and len(seg) >= 2:
            draw.line(seg, fill=color, width=width)

    # internals -------------------------------------------------------------
    def _draw_grid(self, w, h, x_of, y_of):
        x0, x1 = self.PAD_L, w - self.PAD_R
        y0, y1 = self.PAD_T, h - self.PAD_B
        grid = theme.blend(theme.BG_INPUT, theme.TEXT_MAIN, 0.08)
        label_w = 34 if w < 430 else 0
        for i, (f, lab) in enumerate(FREQ_TICKS):
            px = x_of(f)
            self.create_line(px, y0, px, y1, fill=grid)
            skip = (i % 2 == 1) and label_w
            if not skip:
                self.create_text(px, y1 + 10, text=lab, fill=theme.TEXT_DIM,
                                 font=self._font_small)
        db_min = getattr(self, "_db_min", DB_MIN)
        db_max = getattr(self, "_db_max", DB_MAX)
        lo = int(math.ceil(db_min / 5.0) * 5)
        hi = int(math.floor(db_max / 5.0) * 5)
        for db in range(lo, hi + 1, 5):
            py = y_of(db)
            major = (db == 0)
            self.create_line(x0, py, x1, py,
                             fill=theme.TEXT_DIM if major else grid)
            # label every 5 dB like squig.link does
            self.create_text(x0 - 8, py, text="{:+d}".format(db) if db
                             else "0", fill=theme.TEXT_DIM,
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

    def _bind_hover(self):
        """Crosshair readout: nearest curve value under the mouse. The
        bind() happens once; the coordinate mapping is read from
        self._x_of at event time so resizes never leave it stale."""
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
            # always the CURRENT mapping (rebuilt on every redraw/resize --
            # a stale closure used to drift the crosshair off-position)
            px = self._x_of(f)
            self.create_line(px, self.PAD_T, px,
                             int(self.winfo_height()) - self.PAD_B,
                             fill=theme.blend(theme.BG_INPUT, theme.ACCENT_BLUE, 0.25),
                             tags=("hover",))
            self.create_text(
                event.x, max(10, event.y - 12),
                text="{:.0f} Hz  {:+.1f} dB".format(f, d),
                fill=col, font=self._font_small, tags=("hover",),
                anchor="w" if event.x < int(self.winfo_width()) - 120 else "e")

        self.bind("<Motion>", _move, add="+")
        self.bind("<Leave>", lambda e: self.delete("hover"), add="+")
