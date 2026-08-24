"""
fr_analysis.py -- FIXED VERSION (audit drop-in)
Frequency-response (.txt) acoustic analysis for tonal tag suggestions.

Fixes applied vs. original (see audit report):
  F-C9   Input guards: file-size cap, per-file point cap, and a
        resolve_under_root() helper so linked paths cannot escape the
        data folder. All failures raise ValueError with friendly text
        that the GUI already displays.
Analysis thresholds unchanged.
"""

import os
import re

# Numeric token: optional sign, digits with . or , decimal (or bare
# fraction like ".5"), and an OPTIONAL exponent ("2.5e3"). Without the
# exponent group, scientific-notation rows split into garbage tokens
# ("2.5e3" -> 2.5 + 3) and produced fake low-frequency data points.
_NUM_RE = re.compile(r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][-+]?\d+)?")

MAX_FILE_BYTES = 20 * 1024 * 1024      # 20 MB is generous for FR sweeps
MAX_POINTS = 500000


def resolve_under_root(data_root, rel):
    """Join `rel` onto `data_root`, refusing paths that escape the root.

    Returns the normalized absolute path. Raises ValueError on traversal
    or absolute-path input."""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("Empty measurement file path.")
    rel_norm = rel.replace("\\", "/").strip()
    if rel_norm.startswith("/") or re.match(r"^[A-Za-z]:", rel_norm):
        raise ValueError("Measurement path must be relative: {}".format(rel))
    parts = [p for p in rel_norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("Measurement path must stay inside the data folder: {}".format(rel))
    return os.path.join(data_root, *parts)


def parse_fr_file(path):
    """Parse 'frequency dB' pairs from a measurement .txt file.

    Tolerant of headers, comments (# // ;), tabs, commas and semicolons.
    Returns a frequency-sorted list of (freq_hz, db) float tuples.
    Raises OSError on unreadable files, ValueError when the input is too
    large; returns [] if no data rows found."""
    try:
        size = os.path.getsize(path)
    except OSError:
        raise
    if size > MAX_FILE_BYTES:
        raise ValueError(
            "Measurement file is {} MB (limit {} MB): {}".format(
                size // (1024 * 1024), MAX_FILE_BYTES // (1024 * 1024),
                os.path.basename(path)))

    points = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            nums = _NUM_RE.findall(line.replace(";", ",").replace("\t", ","))
            parts = [p for p in re.split(r"[,\s]+", line) if p]
            vals = None
            if len(parts) == 2 and len(_NUM_RE.findall(parts[0])) == 1 \
                    and len(_NUM_RE.findall(parts[1])) == 1:
                try:
                    vals = (float(parts[0].replace(",", ".")),
                            float(parts[1].replace(",", ".")))
                except ValueError:
                    vals = None
            if vals is None:
                nums = [_to_float(n) for n in nums]
                nums = [n for n in nums if n is not None]
                if len(nums) >= 2:
                    vals = (nums[0], nums[1])
            if vals is None:
                continue
            freq, db = vals
            if 2.0 <= freq <= 100000.0 and -120.0 <= db <= 140.0:
                points.append((freq, db))
                if len(points) > MAX_POINTS:
                    raise ValueError(
                        "Measurement file has more than {} data points: {}".format(
                            MAX_POINTS, os.path.basename(path)))
    points.sort(key=lambda p: p[0])
    return points


def _to_float(token):
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def _band(points, lo, hi):
    vals = [db for f, db in points if lo <= f <= hi]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _band_peak(points, lo, hi):
    vals = [db for f, db in points if lo <= f <= hi]
    return max(vals) if vals else None


def _ref_1k(points):
    vals = sorted(db for f, db in points if 900 <= f <= 1100)
    if not vals:
        vals = sorted(db for f, db in points if 700 <= f <= 1300)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def analyze_points(points):
    out = {"ok": False, "metrics": {}, "suggestions": []}
    if not points or len(points) < 20:
        out["error"] = "Not enough data points."
        return out
    fmin, fmax = points[0][0], points[-1][0]
    ref = _ref_1k(points)
    if ref is None:
        out["error"] = "No samples near 1 kHz to reference against."
        return out

    m = {}
    bass = _band(points, 20, 100)
    if bass is not None:
        m["bass_shelf"] = round(bass - ref, 1)
    mid = _band(points, 500, 1500)
    if mid is not None:
        m["mid_level"] = round(mid - ref, 1)
    pinna_band = _band(points, 2500, 3500)
    pinna_peak = _band_peak(points, 2500, 3500)
    if pinna_band is not None:
        m["pinna_gain"] = round(pinna_band - ref, 1)
    if pinna_peak is not None:
        m["pinna_peak"] = round(pinna_peak - ref, 1)
    treb_hi = min(15000, fmax)
    treble_coverage = (min(treb_hi, 15000) - 6000) / (15000 - 6000)
    treb_avg = _band(points, 6000, treb_hi)
    treb_peak = _band_peak(points, 6000, treb_hi)
    if treb_avg is not None and treble_coverage >= 0.25:
        m["treble_avg"] = round(treb_avg - ref, 1)
    elif treb_avg is not None:
        # band covers <25% of 6-15 kHz: report but do not feed scoring
        m["treble_avg_partial"] = round(treb_avg - ref, 1)
    if treb_peak is not None:
        m["treble_peak"] = round(treb_peak - ref, 1)

    sugg = []

    def add(tag, reason):
        sugg.append({"tag": tag, "reason": reason})

    bs = m.get("bass_shelf")
    if bs is not None:
        if bs > 8:
            add("Basshead", "Bass shelf {:+.1f} dB above 1 kHz (>8)".format(bs))
            add("Sub-Bass", "Deep-bass elevated shelf")
            add("Punchy Bass", "Elevated low-end energy")
        elif bs >= 3:
            add("Warm", "Moderate bass shelf {:+.1f} dB (3-8)".format(bs))
            add("Balanced", "Bass present but controlled ({:+.1f} dB)".format(bs))
        elif bs >= 0:
            add("Neutral", "Flat bass shelf {:+.1f} dB vs 1 kHz".format(bs))
            add("Reference", "Near-linear low end")

    pg = m.get("pinna_gain", m.get("pinna_peak"))
    ta = m.get("treble_avg")
    tp = m.get("treble_peak")
    warm_bass = bs is not None and bs >= 3
    score = 0.0
    if pg is not None:
        score += (pg - 9.0) / 3.0
    if ta is not None:
        score += (ta + 2.0) / 6.0
    if tp is not None and pg is not None and tp - pg > 6:
        score += 0.4
    bright = score > 1.0 and not (warm_bass and score < 2.0)
    dark = score < -0.9

    if bright:
        add("Bright", "Forward upper range (brightness score {:+.1f})".format(score))
        if pg is not None and pg > 12:
            add("Vocal-Focused", "Pinna gain {:+.1f} dB (>12)".format(pg))
        if tp is not None and pg is not None and tp - pg > 6:
            add("Treblehead", "Sharp treble peak {:+.1f} dB over ear gain".format(tp))
        elif ta is not None and pg is not None and ta > pg:
            add("Analytical", "Treble energy at/above ear gain ({:+.1f} dB)".format(ta))
    elif dark:
        add("Smooth", "Receding highs (brightness score {:+.1f})".format(score))
        add("Dark", "Low high-frequency energy")
        if pg is not None and pg < 6:
            add("Relaxed", "Soft pinna gain {:+.1f} dB (<6)".format(pg))

    ml = m.get("mid_level")
    if ml is not None and bs is not None and pg is not None:
        floor = min(bs, pg - 7.0)
        if ml + 3.0 < floor:
            shape = "V-Shaped" if bs >= 6 else "U-Shaped"
            add(shape, "Midrange sits {:.1f} dB below the bass/pinna shoulders".format(ml))
        elif abs(ml) <= 3 and pg is not None and 8 <= pg <= 12 and not bright and not dark:
            add("Balanced", "Linear midrange with Harman-like pinna gain")

    primary_priority = ("V-Shaped", "U-Shaped", "Neutral", "Balanced")
    kept_primary = next((p for p in primary_priority
                         if any(s["tag"] == p for s in sugg)), None)
    if kept_primary is not None:
        sugg[:] = [s for s in sugg
                   if s["tag"] not in ("Neutral", "Balanced", "V-Shaped", "U-Shaped")
                   or s["tag"] == kept_primary]

    if pg is not None and pg < 6:
        sugg[:] = [s for s in sugg
                   if s["tag"] not in ("Bright", "Treblehead", "Analytical",
                                        "Vocal-Focused")]
        if not any(s["tag"] in ("Smooth", "Dark", "Relaxed", "Warm")
                   for s in sugg):
            add("Relaxed", "Soft pinna gain {:+.1f} dB (<6)".format(pg))

    tags_now = {s["tag"] for s in sugg}
    if "Warm" in tags_now:
        sugg[:] = [s for s in sugg
                   if s["tag"] not in ("Bright", "Analytical")]

    tags_now = {s["tag"] for s in sugg}
    if "Basshead" in tags_now:
        sugg[:] = [s for s in sugg if s["tag"] != "Treblehead"]
    if kept_primary == "V-Shaped":
        sugg[:] = [s for s in sugg if s["tag"] != "Vocal-Focused"]

    seen = set()
    uniq = []
    for s in sugg:
        if s["tag"] not in seen:
            seen.add(s["tag"])
            uniq.append(s)
    out["ok"] = True
    out["metrics"] = m
    out["suggestions"] = uniq
    out["coverage"] = "{:.0f}Hz - {:.0f}Hz".format(fmin, fmax)
    return out


def summarize_metrics(metrics):
    order = [("bass_shelf", "Bass"), ("mid_level", "Mid"),
             ("pinna_gain", "Pinna"), ("treble_avg", "Treble")]
    parts = ["{} {:+.1f}".format(lbl, metrics[k]) for k, lbl in order
             if k in metrics]
    return " | ".join(parts) + (" dB" if parts else "")
