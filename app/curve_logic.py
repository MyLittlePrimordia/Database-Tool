"""
curve_logic.py -- measurement curve parsing / pairing / averaging.
Ported from the standalone Curve Converter app (pure logic, no GUI) so
the Database Tool can convert raw .txt / .csv measurements into the
standardized two-column Freq(Hz) / SPL(dB) format.

Behavior (unchanged from the standalone app):
  1. Every line of an input file is independently checked for "does this
     look like a numeric frequency + amplitude [+ phase] row?" - any
     delimiter (space/tab/comma/semicolon) works, a header is NOT
     required, and metadata/comments are ignored wherever they appear.
  2. Files whose names end in an EXPLICITLY delimited 1/2 ("(1)/(2)",
      "[1]/[2]", "_1_/_2_", "-1/-2", ...) or L/R are grouped as pairs.
      A bare space before the digit deliberately does NOT count: product
      model names legitimately end in " 1"/" 2" (Hype 2, MACH 2, ...),
      so those files convert solo instead of being mistaken for a pair.
      A group is only averaged when both roles come from the SAME naming
      convention, and only when the two curves differ by less than
      PAIR_MISMATCH_WARN_DB on average (a filename match alone can't tell
      a genuine stereo pair from two unrelated measurements).
  3. Paired curves are linearly interpolated onto the first file's
     frequency grid and averaged; solo files convert as-is.
"""

import os
import re
import traceback

# The separator before a pairing digit must be EXPLICIT punctuation
# (underscore / dash / bracket / paren). A plain space does not qualify:
# too many product models genuinely end in " 1" or " 2", and treating
# those as pair roles risks averaging two different products together.
NUMBERED_SUFFIX_RE = re.compile(r"[_\-\(\[]+([12])[\s_\-\)\]]*$")
LR_SUFFIX_RE = re.compile(r"[\s_\-]([LR])$", re.IGNORECASE)
FIELD_SPLIT_RE = re.compile(r"[\t,]+|\s+")
FIELD_WS_RE = re.compile(r"\s+")
DECIMAL_COMMA_RE = re.compile(r"^-?\d+,\d+$")

CURVE_EXTENSIONS = (".txt", ".csv")

# A "pair" is only ever a genuine stereo/repeat-measurement pair when both
# roles come from the SAME naming convention - never a mix of the two.
VALID_PAIR_ROLE_SETS = ({"1", "2"}, {"L", "R"})

MIN_FREQ_HZ = 1.0
MAX_FREQ_HZ = 200000.0

PAIR_MISMATCH_WARN_DB = 10.0


def is_curve_file(path):
    """True when `path` looks like a raw curve file we accept (.txt/.csv)."""
    return os.path.isfile(path) and path.lower().endswith(CURVE_EXTENSIONS)


def group_key_and_role(stem):
    """Given a filename stem (no extension), return (group_key, role) where
    role is '1', '2', 'L', 'R', or None if this file doesn't look paired
    (group_key is then just the stem itself, and it converts alone).
    """
    m = NUMBERED_SUFFIX_RE.search(stem)
    if m:
        role = m.group(1)
        key = stem[: m.start()].strip()
        return key, role

    m = LR_SUFFIX_RE.search(stem)
    if m:
        role = m.group(1).upper()
        key = stem[: m.start()].strip()
        return key, role

    return stem, None


def _normalize_decimal_comma(field):
    """If `field` looks like a European decimal-comma number (e.g. "20,5"
    with no dot anywhere in it), convert it to plain-dot notation ("20.5")
    so float() parses the intended value instead of silently truncating
    it at the comma.
    """
    if DECIMAL_COMMA_RE.match(field):
        return field.replace(",", ".")
    return field


def _split_fields(stripped):
    """Split one already-stripped line into fields, handling several
    delimiter conventions without letting them collide with each other:

      - Semicolon-delimited lines (";" present): split on ";". Since
        semicolon is unambiguously the field delimiter here, any comma
        left inside a field is safe to treat as a decimal separator.
      - Tab-delimited lines ("\\t" present, no ";"): split on tabs first
        (the strong delimiter), then split each chunk further on plain
        whitespace (handles stray alignment spaces), then treat a
        comma inside any resulting field as a decimal separator.
      - Anything else: fall back to the original combined comma/
        whitespace split. This supports comma-delimited CSV exports like
        "20,112.594" (freq/SPL separated by a bare comma, each value
        itself using a dot decimal) - here comma genuinely IS the
        delimiter, so it must NOT be reinterpreted as a decimal mark.
    """
    if ";" in stripped:
        chunks = [c.strip() for c in stripped.split(";") if c.strip()]
    elif "\t" in stripped:
        chunks = [c.strip() for c in stripped.split("\t") if c.strip()]
    else:
        return [f for f in FIELD_SPLIT_RE.split(stripped) if f]

    fields = []
    for chunk in chunks:
        for sub in FIELD_WS_RE.split(chunk):
            if sub:
                fields.append(_normalize_decimal_comma(sub))
    return fields


def _looks_like_date_triple(nums):
    """'2023,05,01'-style metadata lines: three BARE-INTEGER fields whose
    first looks like a calendar year and whose next two fit month/day.
    A real measurement row satisfying every constraint at once (integer
    frequency in 1900-2100, integer month-range SPL, integer day-range
    phase) is vanishingly unlikely, while CSV date stamps are common."""
    if len(nums) != 3:
        return False
    freq, spl, phase = nums
    for v in (freq, spl, phase):
        if not float(v).is_integer():
            return False
    return (1900 <= freq <= 2100 and 1 <= spl <= 12 and 1 <= phase <= 31)


def try_parse_data_row(line):
    """Look at one line and decide if it's a real curve data row: freq,
    amplitude, and optionally phase. Returns (freq, spl, phase) or None
    if the line isn't a data row (header, comment, metadata, blank...).
    """
    stripped = line.strip()
    if not stripped:
        return None

    fields = _split_fields(stripped)
    if len(fields) < 2:
        return None

    nums = []
    for f in fields[:3]:
        try:
            nums.append(float(f))
        except ValueError:
            break

    if len(nums) < 2:
        return None

    if _looks_like_date_triple(nums):
        return None

    freq, spl = nums[0], nums[1]
    phase = nums[2] if len(nums) > 2 else None

    if not (MIN_FREQ_HZ <= freq <= MAX_FREQ_HZ):
        return None

    return freq, spl, phase


def _sort_and_dedupe_rows(rows):
    """Return `rows` sorted ascending by frequency, with duplicate
    frequency entries collapsed (first occurrence wins). interp_spl's
    endpoint-clamping and binary search both assume ascending, unique
    frequencies - some sources export rows high-to-low or out of order,
    which would otherwise make interpolation silently wrong.
    """
    rows_sorted = sorted(rows, key=lambda r: r[0])
    out = []
    last_freq = None
    for row in rows_sorted:
        if last_freq is not None and row[0] == last_freq:
            continue
        out.append(row)
        last_freq = row[0]
    return out


def parse_curve_file(path):
    """Parse a raw curve file from ANY source regardless of header,
    delimiter, or surrounding metadata. Returns a list of
    (freq, spl, phase_or_None) tuples sorted ascending by frequency with
    duplicate frequencies collapsed.
    """
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            row = try_parse_data_row(line)
            if row is not None:
                rows.append(row)
    return _sort_and_dedupe_rows(rows)


def interp_spl(freqs, spls, target_freqs):
    """Linear-interpolate SPL values onto `target_freqs`."""
    out = []
    n = len(freqs)
    for tf in target_freqs:
        if tf <= freqs[0]:
            out.append(spls[0])
            continue
        if tf >= freqs[-1]:
            out.append(spls[-1])
            continue
        lo, hi = 0, n - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if freqs[mid] <= tf:
                lo = mid
            else:
                hi = mid
        f0, f1 = freqs[lo], freqs[hi]
        s0, s1 = spls[lo], spls[hi]
        if f1 == f0:
            out.append(s0)
        else:
            t = (tf - f0) / (f1 - f0)
            out.append(s0 + t * (s1 - s0))
    return out


def pair_mismatch_severity(rows_a, rows_b):
    """Mean absolute SPL difference between two curves over their
    overlapping frequency range. A real stereo pair (or two repeat
    measurement passes) differs by a fraction of a dB on average; two
    genuinely different measurements that merely share a "<name> 1"/
    "<name> 2" filename pattern typically differ by many dB.
    """
    freqs_a = [r[0] for r in rows_a]
    spls_a = [r[1] for r in rows_a]
    freqs_b = [r[0] for r in rows_b]
    spls_b = [r[1] for r in rows_b]
    interp_b_on_a = interp_spl(freqs_b, spls_b, freqs_a)
    diffs = [abs(a - b) for a, b in zip(spls_a, interp_b_on_a)]
    return sum(diffs) / len(diffs) if diffs else 0.0


def average_group(file_rows_list):
    """Average multiple parsed curves (list of row-lists) onto a common
    frequency grid (the grid of the first file). Returns list of
    (freq, avg_spl) tuples.
    """
    base_freqs = [r[0] for r in file_rows_list[0]]
    all_spls = []
    for rows in file_rows_list:
        freqs = [r[0] for r in rows]
        spls = [r[1] for r in rows]
        all_spls.append(interp_spl(freqs, spls, base_freqs))

    out = []
    for i, f in enumerate(base_freqs):
        vals = [spls[i] for spls in all_spls]
        avg_spl = sum(vals) / len(vals)
        out.append((f, avg_spl))
    return out


def write_output(path, freq_spl_rows):
    with open(path, "w", encoding="utf-8") as f:
        for freq, spl in freq_spl_rows:
            f.write("{:.6f}\t{:.3f}\n".format(freq, spl))


# --------------------------------------------------------------------------
# Group planning (queue -> output groups)
# --------------------------------------------------------------------------
class GroupPlan:
    """One planned output file: its group key, source files, whether the
    sources will be averaged into one curve, and the suggested name."""

    def __init__(self, key, sources, averaged, suggested_name):
        self.key = key
        self.sources = sources          # [(path, role), ...]
        self.averaged = averaged
        self.suggested_name = suggested_name   # e.g. "MOONDROP CHU.txt"

    @property
    def source_names(self):
        return [os.path.basename(p) for p, _role in self.sources]


def plan_groups(paths):
    """Group queued paths into GroupPlans using the pairing rules.
    Sorting the queue first keeps grouping + which file's frequency grid
    becomes the average target reproducible across runs.
    """
    groups = {}
    for path in sorted(paths):
        stem = os.path.splitext(os.path.basename(path))[0]
        key, role = group_key_and_role(stem)
        groups.setdefault(key, []).append((path, role))

    plans = []
    for key in sorted(groups.keys()):
        members = groups[key]
        paired_roles = {role for _p, role in members if role is not None}
        # Only treat two members as a genuine averaging pair when their
        # roles come from the SAME naming convention (both 1/2, or both
        # L/R) - never a mix, never more/fewer than 2.
        averaged = len(members) == 2 and paired_roles in VALID_PAIR_ROLE_SETS
        name_base = "{}.txt".format(key.upper())
        plans.append(GroupPlan(key, members, averaged, sanitize_filename(name_base)))
    return plans


_FILENAME_SANITIZER = r'[<>:"/\\|?*\x00-\x1f]'


def sanitize_filename(name):
    """Strip characters Windows filenames can't contain; collapse the
    whitespace left behind."""
    cleaned = re.sub(_FILENAME_SANITIZER, "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "curve.txt"


def convert_plan(plan, out_path, log=print):
    """Parse -> (average) -> write one GroupPlan. Returns the list of
    files actually written (empty when nothing could be converted).
    A filename-matched pair whose curves differ too much is demoted to
    separate conversions (logged as SKIPPED); the first file keeps the
    planned output name and any extra member gets its own derived name.
    """
    good = []       # [(path, rows)]
    for path, _role in plan.sources:
        try:
            rows = parse_curve_file(path)
        except Exception:  # noqa: BLE001
            err = traceback.format_exc(limit=1).strip().splitlines()[-1]
            log("[FAILED]  {}  ({})".format(os.path.basename(path), err))
            continue
        if not rows:
            log("[SKIPPED] {}  (no usable data rows found)".format(
                os.path.basename(path)))
            continue
        good.append((path, rows))

    if not good:
        return []

    if plan.averaged and len(good) == 2:
        mismatch = pair_mismatch_severity(good[0][1], good[1][1])
        if mismatch > PAIR_MISMATCH_WARN_DB:
            # Filenames look like a pair, but the actual curves are too
            # different to plausibly be two channels/passes of the same
            # unit - fall back to converting each file separately.
            log("[SKIPPED] {}  (looks paired by filename but the curves "
                 "differ by {:.1f} dB on average - too different to be a "
                 "stereo/repeat pair; converting each file separately)"
                 .format(plan.key.upper(), mismatch))
            plan.averaged = False
        else:
            try:
                averaged = average_group([rows for _p, rows in good])
                write_output(out_path, averaged)
                log("[OK]      {}  <-  averaged {} + {}".format(
                    os.path.basename(out_path),
                    os.path.basename(good[0][0]),
                    os.path.basename(good[1][0])))
                return [out_path]
            except Exception:  # noqa: BLE001
                err = traceback.format_exc(limit=1).strip().splitlines()[-1]
                log("[FAILED]  {}  ({})".format(os.path.basename(out_path), err))
                return []

    # Not a validated pair (or demoted above): write whatever parsed,
    # first file under the planned name, extras under their own names.
    written = []
    for n, (path, rows) in enumerate(good):
        if n == 0:
            target = out_path
        else:
            base = sanitize_filename(
                "{}.txt".format(os.path.splitext(os.path.basename(path))[0].upper()))
            target = os.path.join(os.path.dirname(out_path), base)
        try:
            freq_spl_rows = [(freq, spl) for freq, spl, _phase in rows]
            write_output(target, freq_spl_rows)
            log("[OK]      {}".format(os.path.basename(target)))
            written.append(target)
        except Exception:  # noqa: BLE001
            err = traceback.format_exc(limit=1).strip().splitlines()[-1]
            log("[FAILED]  {}  ({})".format(os.path.basename(target), err))
    return written
