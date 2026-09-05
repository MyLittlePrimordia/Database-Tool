"""
db_logic.py -- FIXED VERSION (audit drop-in)
Core data model, validation, normalization and audit logic for the
IEM/Headphone Database Editor. No tkinter dependency.

Fixes applied vs. original (see audit report):
  F-C2/C3/C7/C10  Robust loading: encoding errors, Infinity/NaN, deep
                  recursion -> friendly DatabaseLoadError; every silent
                  coercion now emits a note.
  F-C1            TWS-zeroing and driver-config canonical-order rules are
                  now opt-in constants (default OFF = matches the prompts);
                  unknown driver tokens are reported explicitly instead.
  F-C4            Autosave listing sorted by mtime (suffix-aware), pruning
                  uses the same order.
  F-C4b           Recovery snapshot must be newer than the database file
                  itself (blocks stale prompts and phantom hijacks).
  F-C5            Audit-fix closures resolve targets by OBJECT IDENTITY
                  first; ambiguous duplicate IDs resolve to "stale" instead
                  of mutating the wrong twin. Fixes return the applied
                  position (or None).
  F-C15           Sanity bounds for impedance/sensitivity/price; non-string
                  tags no longer crash validation.
  misc            Dead line removed from describe_entry_change; log()
                  helper for windowed-exe-visible diagnostics.
"""

import os
import re
import io
import json
import math
import gzip
import time
import zlib
import shutil
import difflib
import datetime
import hashlib
import threading
import unicodedata

CURRENT_YEAR = datetime.datetime.now().year

# Upper bound for decompressed .json.gz payloads (the 100 MB raw-file cap
# cannot see inside a compressed archive).
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024

# L-4: rotate editor.log once it crosses this size.
LOG_ROTATE_BYTES = 1024 * 1024

# ==========================================================================
# BEHAVIOR SWITCHES (defaults match ADD ENTRY PROMPT.txt / AUDIT PROMPT.txt)
# ==========================================================================
ENFORCE_TWS_ZERO_SPECS = False       # prompts do NOT require TWS specs = 0
CANONICALIZE_DRIVER_ORDER = True     # always-on audit rule: legacy configs
                                     # that ignore the canonical sequence
                                     # (DD -> BA -> Planar -> EST -> MEMS ->
                                     # PZT -> BC) get flagged with an
                                     # auto-fix that reorders them

# sanity caps (defensive; prompts say whole integers, 0 if unverifiable)
IMPEDANCE_MAX = 200000     # ohms
# AUDIT PROMPT: electrostatic earspeakers are rated by 10 kHz capacitive
# reactance, "typically 100,000 ohms to 360,000 ohms" -- so the usual cap
# only applies to non-electrostatic entries (connector == "Electrostatic").
IMPEDANCE_MAX_ELECTROSTATIC = 360000
SENSITIVITY_MAX = 200      # dB/mW
PRICE_MAX = 10000000       # USD

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "DatabaseEditor")

# Short-lived memo for data-folder walks: audit + file panel often ask
# for the same directory within seconds of each other. Re-walking 11k
# files twice is pure wasted I/O.
# L-7: stored as a tuple and swapped in ONE assignment (slice-assign on
# the cache list) so a concurrent reader on another thread can never
# observe a torn mix of old and new fields (the previous dict.update of
# 4 keys was 4 separate stores).
_SCAN_CACHE = [None, 0.0, None, None]     # [root, time, on_disk, data_dir]


def scan_data_files(data_root):
    """Walk `data_root/data` for .txt measurement files. Memoized for
    ~2 s so concurrent callers (audit + file linker) share one walk."""
    import time
    now = time.time()
    cached_root, cached_time, cached_files, cached_dir = _SCAN_CACHE
    if cached_root == data_root and (now - cached_time) < 2.0 \
            and cached_files is not None:
        return list(cached_files), cached_dir
    data_dir = os.path.join(data_root, "data") if data_root else None
    on_disk = []
    if data_dir and os.path.isdir(data_dir):
        for root, _, files in os.walk(data_dir):
            for fn in files:
                if fn.lower().endswith(".txt"):
                    full = os.path.join(root, fn)
                    try:
                        rel = os.path.relpath(full, data_root).replace("\\", "/")
                    except ValueError:
                        continue
                    rel = re.sub(r"/+", "/", rel)
                    on_disk.append(rel)
        on_disk.sort()
    elif data_root and os.path.isdir(data_root) \
            and os.path.basename(os.path.normpath(data_root)).lower() == "data":
        # data_root itself is the data folder
        data_dir = data_root
        base_root = os.path.dirname(data_dir)
        for root, _, files in os.walk(data_dir):
            for fn in files:
                if fn.lower().endswith(".txt"):
                    full = os.path.join(root, fn)
                    try:
                        rel = os.path.relpath(full, base_root).replace("\\", "/")
                    except ValueError:
                        rel = os.path.join("data", os.path.relpath(full, data_dir)).replace("\\", "/")
                    rel = re.sub(r"/+", "/", rel)
                    on_disk.append(rel)
        on_disk.sort()
    else:
        data_dir = None
        on_disk = []
    _SCAN_CACHE[:] = (data_root, now, list(on_disk), data_dir)
    return on_disk, data_dir


def log(msg):
    """Best-effort diagnostic log visible even in --windowed builds."""
    try:
        print(msg)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "editor.log")
        # L-4: bound the log to ~1 MB so an unattended install can't let
        # it grow forever. Rotate once per process, on the first write
        # that crosses the cap (rewrite side is atomic like every other
        # writer in this module).
        try:
            if not getattr(log, "_rotated", False) and \
                    os.path.isfile(path) and \
                    os.path.getsize(path) > LOG_ROTATE_BYTES:
                os.replace(path, path + ".old")
            log._rotated = True
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


# --------------------------------------------------------------------------
# ATOMIC WRITE HELPERS (DL-3 / DL-4 / L-1 / L-2 / L-3)
# --------------------------------------------------------------------------
# Every persistence writer in this module (and the exporters) goes through
# replace_atomic: write to <path>.tmp, flush + fsync so the DATA blocks are
# durable before the rename (os.replace only guarantees the rename itself
# is atomic -- without fsync a power cut right after a "successful" save
# can still leave a zero-length or garbage target on NTFS), then replace.
#
# On Windows, os.replace raises PermissionError when another process holds
# the target open without FILE_SHARE_DELETE (the IEM Tool app reading
# database.json, a text editor, a second editor instance, antivirus...).
# A short bounded retry rides out transient open handles; when the file
# stays locked we re-raise with a specific, actionable message (DL-4).

REPLACE_RETRY_ATTEMPTS = 4
REPLACE_RETRY_DELAY_S = 0.3


class FileBusyError(OSError):
    """The target file is held open by another process -- the caller can
    present this message verbatim instead of a raw WinError string."""


def _fsync_path(path):
    """fsync a directory (POSIX; needed so the rename itself is durable).
    Windows does not support opening directories this way -- a no-op there
    (NTFS metadata journaling covers the rename)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_atomic(tmp_path, path):
    """Durable rename with bounded retry against transient Windows
    share-lock failures (DL-4). Raises FileBusyError when the target stays
    locked, PermissionError/OSError otherwise."""
    last_err = None
    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            _fsync_path(os.path.dirname(os.path.abspath(path)) or ".")
            return
        except PermissionError as e:
            # Windows: ERROR_SHARING_VIOLATION / ACCESS_DENIED while the
            # target is open in another process. Retry briefly -- readers
            # usually let go within milliseconds.
            last_err = e
            if attempt < REPLACE_RETRY_ATTEMPTS - 1:
                time.sleep(REPLACE_RETRY_DELAY_S)
        except OSError as e:
            # Non-Windows rename races (e.g. NFS silly-rename) also
            # benefit from one quick retry before giving up.
            last_err = e
            if attempt < REPLACE_RETRY_ATTEMPTS - 1:
                time.sleep(REPLACE_RETRY_DELAY_S)
    target = os.path.basename(path)
    raise FileBusyError(
        "Could not replace '{}' -- the file is open in another program "
        "(close the IEM Tool app / any editor showing it and try again).\n"
        "({})".format(target, last_err))


def write_text_atomic(path, text):
    """Write UTF-8 text to `path` atomically and durably (tmp + fsync +
    retrying replace). Cleans up its tmp file on any failure."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_atomic(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_bytes_atomic(path, payload):
    """Binary twin of write_text_atomic (gzip exports)."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        replace_atomic(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def snapshot_entries(entries):
    """M-7: independent, schema-clean copies of the entry list for handoff
    to a worker thread (audit / export). build_clean_entry returns a fresh
    dict with fresh tags/files lists, so the worker can never observe a
    half-applied UI-thread mutation."""
    return [build_clean_entry(e) for e in entries]


# --------------------------------------------------------------------------
# SCHEMA
# --------------------------------------------------------------------------
SCHEMA_FIELDS = [
    "id", "brand", "model", "variant", "year", "price_usd",
    "driver_type", "driver_config", "impedance", "sensitivity",
    "connector", "form_factor", "tags", "files",
]

SCHEMA_STR_FIELDS = ["id", "brand", "model", "variant", "driver_type",
                     "driver_config", "connector", "form_factor"]
SCHEMA_INT_FIELDS = ["year", "price_usd", "impedance", "sensitivity"]
SCHEMA_LIST_FIELDS = ["tags", "files"]

BLANK_ENTRY = {
    "id": "", "brand": "", "model": "", "variant": "", "year": 0,
    "price_usd": 0, "driver_type": "", "driver_config": "", "impedance": 0,
    "sensitivity": 0, "connector": "", "form_factor": "", "tags": [], "files": [],
}

# --------------------------------------------------------------------------
# DRIVERS
# --------------------------------------------------------------------------
# Canonical ordering sequence for generated/normalized driver_config
# strings. Matches ADD ENTRY PROMPT.txt exactly:
#   DD -> BA -> Planar -> EST -> MEMS -> PZT -> BC
DRIVER_TECH_ORDER = ["DD", "BA", "Planar", "EST", "MEMS", "PZT", "BC"]
DRIVER_TECH_LABELS = {
    "DD": "Dynamic Driver (DD)",
    "Planar": "Planar",
    "BA": "Balanced Armature (BA)",
    "BC": "Bone Conduction (BC)",
    "PZT": "Piezoelectric (PZT)",
    "MEMS": "MEMS",
    "EST": "Electrostatic (EST)",
}
ALLOWED_DRIVER_TYPES = ["", "DD", "BA", "BC", "Planar", "Hybrid", "Tribrid", "EST", "MEMS", "PZT"]

# --------------------------------------------------------------------------
# FORM FACTOR / CONNECTOR MATRIX
# --------------------------------------------------------------------------
FORM_FACTORS = [
    "IEM",
    "Wireless Earbuds (TWS)",
    "Earbuds (Wired)",
    "Wireless Over-Ear Headphones",
    "Over-Ear Headphones (Wired)",
]

CONNECTORS_ALL = [
    "Bluetooth", "2-pin", "QDC", "MMCX", "A2DC",
    "Fixed Cable", "Detachable Cable", "Proprietary", "Electrostatic",
]

FORM_CONNECTOR_MAP = {
    "IEM": ["2-pin", "QDC", "MMCX", "A2DC", "Fixed Cable", "Proprietary"],
    "Earbuds (Wired)": ["2-pin", "QDC", "MMCX", "A2DC", "Fixed Cable", "Proprietary"],
    "Wireless Earbuds (TWS)": ["Bluetooth"],
    "Wireless Over-Ear Headphones": ["Bluetooth"],
    "Over-Ear Headphones (Wired)": ["Detachable Cable", "Fixed Cable", "Electrostatic"],
}

TWS_FORM_FACTOR = "Wireless Earbuds (TWS)"

FORM_FACTOR_ICON = {
    "IEM": "iem",
    "Wireless Earbuds (TWS)": "tws",
    "Earbuds (Wired)": "earbud",
    "Wireless Over-Ear Headphones": "headset",
    "Over-Ear Headphones (Wired)": "headphone",
}
CONNECTOR_ICON = {
    "Bluetooth": "bluetooth", "2-pin": "2pin", "QDC": "qdc", "MMCX": "mmcx",
    "A2DC": "a2dc", "Fixed Cable": "fixed", "Detachable Cable": "detach",
    "Proprietary": "proprietary", "Electrostatic": "electro",
}
DRIVER_TYPE_ICON = {
    "DD": "dd", "BA": "ba", "BC": "bc", "Planar": "planar", "EST": "est",
    "MEMS": "mems", "PZT": "pzt", "Hybrid": "hybrid", "Tribrid": "tribrid",
}

# --------------------------------------------------------------------------
# TAGS
# --------------------------------------------------------------------------
TAG_GROUPS = {
    "Tonal Profiles & Sound Signature": [
        "Basshead", "Sub-Bass", "Punchy Bass", "Warm", "Neutral", "V-Shaped",
        "U-Shaped", "Balanced", "Bright", "Treblehead", "Dark", "Vocal-Focused",
    ],
    "Technicalities & Presentation": [
        "Detailed", "Resolving", "Technical", "Wide-Stage", "Good-Imaging",
        "Smooth", "Reference", "Analytical", "Fun", "Relaxed",
    ],
    "Use Cases": ["Gaming", "Competitive-Gaming", "Studio-Monitoring"],
    "Price Tier (auto-assigned)": ["Budget", "Mid-Tier", "Premium", "Flagship"],
    "Release Types": ["Collab", "Limited-Edition"],
}
APPROVED_TAGS = [t for grp in TAG_GROUPS.values() for t in grp]
PRICE_TIER_TAGS = TAG_GROUPS["Price Tier (auto-assigned)"]

TAG_CONFLICT_PAIRS = [
    frozenset(["V-Shaped", "U-Shaped"]),
    frozenset(["Neutral", "V-Shaped"]),
    frozenset(["V-Shaped", "Vocal-Focused"]),
    frozenset(["Dark", "Bright"]),
    frozenset(["Dark", "Treblehead"]),
    frozenset(["Warm", "Bright"]),
    frozenset(["Warm", "Analytical"]),
    frozenset(["Basshead", "Treblehead"]),
]

PRIMARY_TONALITY_GROUP = {"Neutral", "Balanced", "V-Shaped", "U-Shaped"}

MIN_TAGS = 4
MAX_TAGS = 12

# --------------------------------------------------------------------------
# MISSING-DATA AUDIT KNOBS (tune to taste; all can be flipped off)
# --------------------------------------------------------------------------
WARN_ZERO_YEAR = True             # year == 0  -> warning ("unknown" fallback)
WARN_ZERO_PRICE = True            # price == 0 -> warning
WARN_UNVERIFIED_DRIVERS = True    # driver_type AND driver_config both empty
WARN_ZERO_SPECS_NON_TWS = True    # impedance/sensitivity == 0 on wired forms
WARN_TWS_SPECS_NONZERO = True     # nonzero impedance/sensitivity on TWS
                                  # (AUDIT PROMPT: TWS must be 0/0; advisory)
NO_FILES_WARN_THRESHOLD = 25      # fileless entries: <=N -> warning row,
                                  #              >N -> info summary row
UNLINKED_ROW_CAP = 200            # unlinked files: <=N -> one info row per
                                  #              file, >N -> single summary
                                  #              row (keeps the audit list
                                  #              usable on huge data sets)

# --------------------------------------------------------------------------
# NORMALIZATION HELPERS
# --------------------------------------------------------------------------

def normalize_component(text):
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = text.replace("+", "_plus_")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _nonlatin_digest(text):
    """H-1: stable, short ASCII token derived from the raw UTF-8 bytes of a
    component whose ASCII fold is empty (e.g. a fully Japanese/Cyrillic/
    Greek brand). Two different non-Latin names hash to different tokens
    with overwhelming probability, so deterministic ID collisions between
    them are gone. Only ever used as a LAST RESORT component suffix --
    existing all-Latin IDs are byte-identical to before."""
    digest = hashlib.sha1(str(text).encode("utf-8", "surrogatepass")).hexdigest()
    return "x" + digest[:6]


def build_id(brand, model, variant):
    comps = []
    for raw in (brand, model):
        norm = normalize_component(raw)
        if norm:
            comps.append(norm)
        elif raw and str(raw).strip():
            # Non-Latin component: keep the ID unique per raw value instead
            # of silently dropping it (which made every Japanese-brand entry
            # with the same model collide on one ID).
            comps.append(_nonlatin_digest(raw))
    if variant and str(variant).strip():
        vnorm = normalize_component(variant)
        comps.append(vnorm if vnorm else _nonlatin_digest(variant))
    idstr = "_".join(c for c in comps if c)
    idstr = re.sub(r"_+", "_", idstr).strip("_")
    return idstr


def _brand_fold(brand):
    """Aggressive fold used ONLY to group brand spellings that are the same
    name with different casing/spacing/punctuation ('Moondrop' / 'MoonDrop'
    / 'Moon Drop' / 'Moon-Drop' all fold to 'moondrop'). Deliberately
    stricter than normalize_component (which keeps underscores as word
    separators): a brand-name typo check wants those treated as the same
    brand, not as siblings."""
    if not brand:
        return ""
    text = unicodedata.normalize("NFKD", str(brand))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def brand_spelling_fixes(entries):
    """Find brands whose spelling is inconsistent across the database
    (case, spacing, or punctuation variants of the same fold) and return
    {entry_index: (current_spelling, canonical_spelling, variant_summary)}
    for every entry NOT already using the majority spelling. The majority
    spelling (most entries; ties broken alphabetically for determinism)
    is treated as canonical. Buckets of size 1 (no inconsistency) are
    skipped entirely."""
    buckets = {}   # fold -> {exact_spelling: [idx, ...]}
    for idx, e in enumerate(entries):
        brand = (e.get("brand") or "").strip()
        if not brand:
            continue
        fold = _brand_fold(brand)
        if not fold:
            continue
        buckets.setdefault(fold, {}).setdefault(brand, []).append(idx)

    fixes = {}
    for fold, spellings in buckets.items():
        if len(spellings) < 2:
            continue
        ranked = sorted(spellings.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        canonical = ranked[0][0]
        summary = ", ".join("'{}' ({})".format(sp, len(idxs))
                             for sp, idxs in ranked)
        for spelling, idxs in ranked[1:]:
            for idx in idxs:
                fixes[idx] = (spelling, canonical, summary)
    return fixes


def price_tier_for(price_usd):
    try:
        p = float(price_usd)
        if not math.isfinite(p):
            return "Budget"
        p = int(p)
    except (TypeError, ValueError):
        p = 0
    if p >= 1500:
        return "Flagship"
    if p >= 500:
        return "Premium"
    if p >= 100:
        return "Mid-Tier"
    return "Budget"


def price_tier_basis(price_usd):
    """Price value used to determine the expected tier tag.

    When the price is a valid whole $5 multiple we use it as-is; otherwise
    we use the rounded value (the price the entry will have after the
    rounding fix). This keeps `validate_entry` and the audit engine
    agreeing on the expected tier and avoids oscillating fix messages for
    prices like 498 vs 500."""
    if isinstance(price_usd, str) and "_" in price_usd:
        return price_usd
    try:
        p_check = float(price_usd)
        if not math.isfinite(p_check) or p_check < 0:
            return price_usd
        if float(p_check).is_integer() and int(p_check) % 5 == 0:
            return p_check
        return round_price_to_5(p_check)
    except (TypeError, ValueError):
        return price_usd


def round_price_to_5(price_usd):
    try:
        p = float(price_usd)
        if not math.isfinite(p):
            return 0
    except (TypeError, ValueError):
        return 0
    if p <= 0:
        # Clamp non-positive input instead of rounding further away from
        # zero (round_price_to_5(-3) used to return -5).
        return 0
    return int(math.floor((p + 2.5) / 5.0) * 5)


def coerce_int(value, default=0):
    """Whole-number coercion that cannot raise. Non-finite floats and
    garbage fall back to `default`."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and "_" in value:
        return default
    try:
        f = float(value)
        if not math.isfinite(f):
            return default
        if f != int(f):
            f = math.floor(f + 0.5)   # half-up, matches price rounding
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return default


def is_valid_year(year):
    y = coerce_int(year, default=-1)
    if y == 0:
        return True
    return 1950 <= y <= CURRENT_YEAR + 1


def classify_driver(components):
    techs = [t for t, c in components.items() if c and c > 0]
    ordered = [t for t in DRIVER_TECH_ORDER if t in techs]
    if not ordered:
        return "", ""
    config = "+".join("{}{}".format(components[t], t) for t in ordered)
    if len(ordered) == 1:
        return ordered[0], config
    if len(ordered) == 2:
        return "Hybrid", config
    return "Tribrid", config


# Case-insensitive lookup for driver tech tokens ("1dd" == "1DD").
# Canonical spelling always wins on output, so a lowercase or mixed-case
# config is normalized instead of being treated as unknown data.
_TECH_BY_UPPER = {t.upper(): t for t in DRIVER_TECH_ORDER}


def _split_driver_token(part):
    """'2BA' -> (2, 'BA'); returns count 0 when the token is not a valid
    <count><tech> pair. Tech matching is case-insensitive."""
    m = re.match(r"^(\d+)([A-Za-z]+)$", part)
    if not m:
        return 0, None
    count, tech = m.groups()
    tech = _TECH_BY_UPPER.get(tech.upper())
    if tech is None:
        return 0, None
    return coerce_int(count, 0), tech


def parse_driver_config(config_str):
    result = {}
    if not config_str:
        return result
    parts = str(config_str).replace(" ", "").split("+")
    for part in parts:
        if not part:
            continue
        c, tech = _split_driver_token(part)
        if tech is not None and c > 0:
            result[tech] = c
    return result


def driver_config_unknown_tokens(config_str):
    """Tokens present in the string that carry no known tech/count meaning."""
    out = []
    for part in str(config_str).replace(" ", "").split("+"):
        if not part:
            continue
        c, tech = _split_driver_token(part)
        if tech is None or c <= 0:
            out.append(part)
    return out


def tag_conflicts(tag_set):
    seen = set()
    conflicts = []
    for pair in TAG_CONFLICT_PAIRS:
        if pair.issubset(tag_set):
            key = tuple(sorted(pair))
            if key not in seen:
                seen.add(key)
                conflicts.append(key)
    present_primary = [t for t in tag_set if t in PRIMARY_TONALITY_GROUP]
    if len(present_primary) > 1:
        key = tuple(sorted(present_primary))
        if key not in seen:
            seen.add(key)
            conflicts.append(key)
    return conflicts


def _join_tags(tags):
    """Crash-proof tag list formatting (tags may be non-strings in raw data)."""
    return ", ".join(str(t) for t in tags)


def validate_entry(entry, existing_ids=None, exclude_id=None):
    errors = []
    existing_ids = existing_ids or set()

    brand = (str(entry.get("brand") or "")).strip()
    model = (str(entry.get("model") or "")).strip()
    variant = (str(entry.get("variant") or "")).strip()
    if not brand:
        errors.append("Brand is required.")
    if not model:
        errors.append("Model is required.")

    expected_id = build_id(brand, model, variant)
    if not expected_id:
        errors.append("Could not build a valid ID from Brand/Model/Variant.")
    elif entry.get("id") != expected_id:
        errors.append(
            "ID does not match normalized Brand/Model/Variant "
            "(expected '{}').".format(expected_id)
        )
    if expected_id and expected_id in existing_ids and expected_id != exclude_id:
        errors.append("An entry with ID '{}' already exists.".format(expected_id))

    if not is_valid_year(entry.get("year", 0)):
        errors.append(
            "Year must be a 4-digit year between 1950 and {} (or 0 if unknown).".format(
                CURRENT_YEAR + 1
            )
        )

    price_raw = entry.get("price_usd", 0)
    if isinstance(price_raw, float) and not math.isfinite(price_raw):
        errors.append("Price must be a finite number.")
        price = -1
    else:
        if isinstance(price_raw, str) and "_" in price_raw:
            errors.append("Price must be a whole number (underscores not allowed, got '{}').".format(price_raw))
            price = -1
        elif isinstance(price_raw, str) and "." in price_raw:
            errors.append("Price must be a whole number (got '{}').".format(price_raw))
            price = coerce_int(price_raw, -1)
        elif isinstance(price_raw, str):
            try:
                price = int(price_raw.strip())
            except ValueError:
                errors.append("Price must be a whole number.")
                price = -1
        else:
            price = coerce_int(price_raw, -1)
    if 0 <= price <= PRICE_MAX:
        if price % 5 != 0:
            errors.append(
                "Price must be rounded to the nearest $5 (got ${}).".format(price)
            )
    elif price > PRICE_MAX:
        errors.append("Price ${} exceeds the sanity maximum of ${}.".format(price, PRICE_MAX))
    elif price < 0:
        errors.append("Price cannot be negative.")

    form_factor = (str(entry.get("form_factor") or "")).strip()
    connector = (str(entry.get("connector") or "")).strip()
    if not form_factor:
        errors.append("Form factor is required (must be one of: {}).".format(", ".join(FORM_FACTORS)))
    elif form_factor not in FORM_FACTORS:
        errors.append("Form factor '{}' is not one of the 5 approved values.".format(form_factor))

    if not connector:
        errors.append("Connector is required (must be one of: {}).".format(", ".join(CONNECTORS_ALL)))
    elif connector not in CONNECTORS_ALL:
        errors.append("Connector '{}' is not one of the 9 approved values.".format(connector))
    elif form_factor and form_factor in FORM_CONNECTOR_MAP and connector not in FORM_CONNECTOR_MAP[form_factor]:
        errors.append(
            "Connector '{}' is not valid for form factor '{}'.".format(connector, form_factor)
        )

    if ENFORCE_TWS_ZERO_SPECS and form_factor == TWS_FORM_FACTOR:
        for f, label in (("impedance", "Impedance"), ("sensitivity", "Sensitivity")):
            v = coerce_int(entry.get(f, 0), default=-1)
            if v != 0:
                errors.append(
                    "{} entries must have {} set to 0 (wireless: no DAC/amp chain).".format(
                        TWS_FORM_FACTOR, label)
                )

    driver_type = (str(entry.get("driver_type") or "")).strip()
    driver_config = (str(entry.get("driver_config") or "")).strip()
    if driver_type and driver_type not in ALLOWED_DRIVER_TYPES:
        errors.append("Driver type '{}' is not an approved value.".format(driver_type))
    if driver_config and re.search(r"\s", driver_config):
        errors.append("Driver config must not contain whitespace (got '{}').".format(driver_config))
    if driver_type and not driver_config:
        errors.append("Driver type '{}' present but driver_config is empty.".format(driver_type))
    if driver_config and not driver_type:
        parsed_tmp = parse_driver_config(driver_config)
        if parsed_tmp:
            errors.append("Driver config '{}' present but driver_type is empty.".format(driver_config))
    if driver_config:
        unknown = driver_config_unknown_tokens(driver_config)
        if unknown:
            errors.append("Driver config '{}' contains unknown token(s): {}.".format(
                driver_config, ", ".join(unknown)))
        parsed = parse_driver_config(driver_config)
        if parsed:
            expected_type, expected_config = classify_driver(parsed)
            if expected_type != driver_type:
                errors.append(
                    "Driver type '{}' does not match configuration '{}' "
                    "(expected '{}').".format(driver_type, driver_config, expected_type)
                )

    for f in ("impedance", "sensitivity"):
        label = f.capitalize()
        raw = entry.get(f, 0)
        if isinstance(raw, str) and "." in raw:
            errors.append("{} must be a whole number (got '{}').".format(label, raw))
        v = coerce_int(raw, default=-1)
        try:
            fv = float(raw)
            if not math.isfinite(fv):
                errors.append("{} must be a finite number.".format(label))
                continue
        except (TypeError, ValueError):
            errors.append("{} must be a whole number.".format(label))
            continue
        if v < 0:
            errors.append("{} cannot be negative.".format(label))
        # upper bounds are advisory (see audit Spec Sanity warnings), not
        # save-blockers: electrostatics legitimately exceed common ranges

    tags = entry.get("tags", []) or []
    if len(tags) != len(set(map(str, tags))):
        dup = sorted({str(t) for t in tags if list(map(str, tags)).count(str(t)) > 1})
        errors.append("Duplicate tag(s): {}".format(", ".join(dup)))
    unapproved = [t for t in tags if t not in APPROVED_TAGS]
    if unapproved:
        errors.append("Unapproved tag(s): {}".format(_join_tags(unapproved)))
    if len(tags) < MIN_TAGS:
        errors.append("At least {} tags are required (has {}).".format(MIN_TAGS, len(tags)))
    if len(tags) > MAX_TAGS:
        errors.append("At most {} tags are allowed (has {}).".format(MAX_TAGS, len(tags)))
    conflicts = tag_conflicts(set(map(str, tags)))
    for pair in conflicts:
        errors.append("Conflicting tags present: {}".format(" + ".join(pair)))
    tier_tags_present = [t for t in tags if t in PRICE_TIER_TAGS]
    if len(tier_tags_present) != 1:
        errors.append("Exactly one price-tier tag is required (Budget/Mid-Tier/Premium/Flagship).")
    else:
        basis = price_tier_basis(entry.get("price_usd", 0))
        expected_tier = price_tier_for(basis)
        if tier_tags_present[0] != expected_tier:
            errors.append(
                "Price-tier tag '{}' does not match price ${} (expected '{}').".format(
                    tier_tags_present[0], entry.get("price_usd", 0), expected_tier
                )
            )

    return errors


def build_clean_entry(source, notes=None, where=""):
    """Return a new dict with exactly SCHEMA_FIELDS in canonical order.
    When `notes` (a list) is supplied, every silent repair is reported as
    'where: message' so corruption can never be laundered quietly."""
    def note(msg):
        if notes is not None:
            notes.append("{}{}".format(where + ": " if where else "", msg))

    def _label(f):
        return "{} ('{}')".format(f, (source.get("id") or source.get("model") or "?"))

    out = {}
    for f in SCHEMA_FIELDS:
        val = source.get(f, BLANK_ENTRY[f])
        if f in SCHEMA_INT_FIELDS:
            orig = val
            if isinstance(orig, float) and not math.isfinite(orig):
                note("{} value {} (non-finite) reset to 0.".format(_label(f), orig))
                val = 0
            elif isinstance(val, str):
                sval = val.strip()
                if sval == "":
                    val = 0
                elif "_" in sval:
                    note("{} value '{}' contains underscores (not allowed); reset to 0.".format(_label(f), orig))
                    val = 0
                elif re.fullmatch(r"[+-]?\d+", sval) is None \
                        and "." in sval:
                    try:
                        fv = float(sval.replace(",", "."))
                        if not math.isfinite(fv):
                            raise ValueError
                        # Same half-up path as numeric floats so a value
                        # stored as text coerces identically to the same
                        # number stored as a float ("239.5" -> 240, not 239).
                        val = coerce_int(fv, 0)
                        note("{} value '{}' coerced to {}.".format(_label(f), orig, val))
                    except (ValueError, OverflowError):
                        note("{} value '{}' is not numeric; reset to 0.".format(_label(f), orig))
                        val = 0
                else:
                    val = coerce_int(sval, 0)
                    if str(val) != sval:
                        note("{} value '{}' coerced to {}.".format(_label(f), orig, val))
            elif isinstance(val, bool):
                val = int(val)
                note("{} boolean coerced to {}.".format(_label(f), val))
            elif not isinstance(val, int):
                new = coerce_int(val, 0)
                if new == 0 and val not in (0, "0", 0.0):
                    note("{} value '{}' is not numeric; reset to 0.".format(_label(f), orig))
                elif isinstance(orig, float) and not orig.is_integer():
                    # half-up rounding of a non-integer spec: report it so a
                    # silent value change can never hide inside a save/export
                    note("{} value {} coerced to {}.".format(_label(f), orig, new))
                val = new
        elif f in SCHEMA_STR_FIELDS:
            if val is None:
                val = ""
            elif isinstance(val, (list, dict)):
                note("{} structured value flattened to text.".format(_label(f)))
                val = str(val).strip()
            else:
                val = str(val).strip()
        elif f in SCHEMA_LIST_FIELDS:
            if not isinstance(val, list):
                if val not in (None, "", [], {}):
                    note("{} was {} (not a list); reset to [].".format(_label(f), type(val).__name__))
                val = []
            else:
                cleaned_list = []
                for x in val:
                    if x is None:
                        continue
                    if isinstance(x, (list, dict)):
                        note("{} item {} dropped (structured value).".format(_label(f), str(x)[:40]))
                        continue
                    s = str(x).strip()
                    if s:
                        cleaned_list.append(s)
                val = cleaned_list
        out[f] = val
    return out


def sort_key(entry):
    return (
        (entry.get("brand") or "").lower(),
        (entry.get("model") or "").lower(),
        (entry.get("variant") or "").lower(),
    )


def format_entry_label(entry):
    """Human-readable 'Brand Model [Variant]' display string (as opposed to
    the underscored id), used anywhere entries are listed for the user to
    pick from (e.g. the Import tab's link-to-entry dropdown)."""
    parts = [entry.get("brand") or "", entry.get("model") or ""]
    label = " ".join(p for p in parts if p).strip()
    variant = (entry.get("variant") or "").strip()
    if variant:
        label = "{}  [{}]".format(label, variant) if label else variant
    return label or (entry.get("id") or "(unnamed entry)")


def describe_entry_change(before, after, max_fields=3):
    if before is None and after is None:
        return ""
    if before is None:
        return "created"
    if after is None:
        return "removed"
    parts = []
    for f in SCHEMA_FIELDS:
        if f == "id":
            continue
        b, a = before.get(f), after.get(f)
        if b == a:
            continue
        if isinstance(b, list) or isinstance(a, list):
            parts.append("{}: {} -> {} item(s)".format(
                f.capitalize(), len(b or []), len(a or [])))
        elif f == "price_usd":
            try:
                parts.append("Price: ${} -> ${}".format(int(b), int(a)))
            except (TypeError, ValueError):
                parts.append("Price: {} -> {}".format(b, a))
        elif f in SCHEMA_INT_FIELDS:
            parts.append("{}: {} -> {}".format(f.capitalize(), b, a))
        else:
            parts.append("{}: '{}' -> '{}'".format(f.capitalize(), b, a))
        if len(parts) >= max_fields:
            remaining = sum(
                1 for g in SCHEMA_FIELDS
                if g != "id" and SCHEMA_FIELDS.index(g) > SCHEMA_FIELDS.index(f)
                and before.get(g) != after.get(g))
            if remaining:
                parts.append("(+{} more)".format(remaining))
            break
    return "; ".join(parts)

# --------------------------------------------------------------------------
# LOAD / SAVE
# --------------------------------------------------------------------------

class DatabaseLoadError(Exception):
    pass


def load_database(path):
    """Load + syntax-check a database JSON file. Raises DatabaseLoadError
    with a friendly, specific message on ANY failure mode.
    Gzip archives (.json.gz) are detected by magic bytes and decompressed
    transparently, so the exact file the website serves can be audited.
    Decompression is size-capped: a small .gz can legitimately expand far
    past the raw-file limit, and an unbounded read would let a corrupted
    or hostile archive exhaust memory."""
    try:
        size = os.path.getsize(path)
        if size > 100 * 1024 * 1024:
            raise DatabaseLoadError("File too large ({} MB) - refusing to load.".format(size // (1024*1024)))
    except OSError:
        pass
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
    except OSError as e:
        raise DatabaseLoadError("Could not open file:\n{}".format(e))

    if raw_bytes[:2] == b"\x1f\x8b":        # gzip magic number
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes)) as gz:
                # read one byte past the cap to detect oversized payloads
                raw_bytes = gz.read(MAX_DECOMPRESSED_BYTES + 1)
        except DatabaseLoadError:
            raise
        except (OSError, EOFError, zlib.error) as e:
            raise DatabaseLoadError(
                "File is gzip-compressed but could not be decompressed:\n{}".format(e))
        if len(raw_bytes) > MAX_DECOMPRESSED_BYTES:
            raise DatabaseLoadError(
                "Decompressed database exceeds the {} MB safety limit "
                "(corrupted or hostile .gz file).".format(
                    MAX_DECOMPRESSED_BYTES // (1024 * 1024)))

    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        line = raw_bytes[:e.start].count(b"\n") + 1
        raise DatabaseLoadError(
            "Invalid text encoding near byte {} (line {}):\n{}\n"
            "The file must be UTF-8 text.".format(e.start, line, e.reason))

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise DatabaseLoadError(
            "Invalid JSON syntax at line {}, column {}:\n{}".format(e.lineno, e.colno, e.msg))
    except RecursionError:
        raise DatabaseLoadError("JSON nesting is too deep (corrupted or hostile file).")

    if not isinstance(data, list):
        raise DatabaseLoadError("Database file must contain a JSON array of entries at the top level.")

    cleaned = []
    coercion_notes = []
    for i, item in enumerate(data):
        where = "Entry #{}".format(i)
        if not isinstance(item, dict):
            coercion_notes.append("{} was not a JSON object and was skipped.".format(i))
            continue
        extra = set(item.keys()) - set(SCHEMA_FIELDS)
        if extra:
            coercion_notes.append("{} had extra field(s) {} - they will be dropped on save.".format(
                i, ", ".join(sorted(extra))))
        cleaned.append(build_clean_entry(item, notes=coercion_notes, where=where))
    return cleaned, coercion_notes


def save_database(path, entries):
    """Sort `entries` IN PLACE (preserving object identities for the undo
    history), then atomically write a canonicalized copy."""
    entries.sort(key=sort_key)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    ordered = [build_clean_entry(e) for e in entries]
    tmp_path = "{}.tmp".format(path)
    try:
        # newline="\n": keep the canonical serialization byte-stable (LF)
        # on every platform -- Windows text mode would otherwise translate
        # to CRLF, so re-saving churned every line ending and the saved
        # bytes disagreed with the LF-canonical database.json.gz export.
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(ordered, f, indent=2, ensure_ascii=False)
            f.write("\n")
            # DL-3: flush + fsync BEFORE the rename so the data blocks are
            # durable; os.replace alone only makes the swap atomic.
            f.flush()
            os.fsync(f.fileno())
        replace_atomic(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return entries


# --------------------------------------------------------------------------
# BACKUP FOLDER
# --------------------------------------------------------------------------
# Everything backup/undo/ignore-related lives in one plain, visible folder
# next to the database, instead of scattered across a hidden autosave
# folder + loose files beside database.json.
BACKUP_DIR_NAME = "backup"
BACKUP_FILE_NAME = "database.json.bak"
AUTOSAVE_PREFIX = "autosave_"
AUTOSAVE_KEEP = 15
AUTOSAVE_SEEN_MARKER = ".autosave_seen"


def backup_dir_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_DIR_NAME)


def write_database_backup(db_path):
    """Copy the CURRENT on-disk database.json into backup/database.json.bak
    BEFORE it gets overwritten by a save. A single rolling file (not a
    timestamped history) -- one clear safety copy of "the database as it
    was right before your last save", not a folder of ambiguous snapshots.
    L-1: the copy is written to .bak.tmp and fsynced before replacing the
    previous backup, so an interruption can never corrupt BOTH the live
    database and its safety copy at once.
    Returns the backup path, or None when there was nothing to back up yet
    (first-ever save) or the copy failed (the save itself still proceeds --
    the periodic autosave snapshots are a second net)."""
    if not db_path or not os.path.isfile(db_path):
        return None
    bdir = backup_dir_for(db_path)
    try:
        os.makedirs(bdir, exist_ok=True)
        dest = os.path.join(bdir, BACKUP_FILE_NAME)
        tmp = dest + ".tmp"
        with open(db_path, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        replace_atomic(tmp, dest)
        return dest
    except OSError as e:
        log("Database backup failed: {}".format(e))
        try:
            os.remove(os.path.join(bdir, BACKUP_FILE_NAME + ".tmp"))
        except OSError:
            pass
        return None


def _autosave_sort_key(p):
    """Chronological ordering: modification time first, then a suffix-aware
    name comparison so same-second snapshots (_2, _10 ...) order correctly.

    Only a trailing "_<n>" that FOLLOWS the timestamp stamp counts as the
    sequence number; the digits inside the stamp itself (e.g. the time
    "..._101010") must not poison the comparison, or same-second files
    sorted inverted and pruning could drop the newest snapshot."""
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    name = os.path.basename(p)
    seq = 0
    m = re.search(r"_(\d+)\.json$", name)
    # The trailing "_<n>" is a real collision sequence ONLY when what
    # remains before it still ends with a full timestamp stamp. Otherwise
    # the matched digits ARE the stamp's own time part (e.g.
    # "autosave_20260824_101010.json") and must not poison the ordering.
    if m and re.search(r"\d{8}_\d{6}$", name[:m.start()]):
        seq = int(m.group(1))
    return (mtime, seq, name)


def _list_autosave_files(bdir):
    if not os.path.isdir(bdir):
        return []
    out = []
    for fn in os.listdir(bdir):
        if fn.startswith(AUTOSAVE_PREFIX) and fn.endswith(".json"):
            p = os.path.join(bdir, fn)
            if os.path.isfile(p):
                out.append(p)
    out.sort(key=_autosave_sort_key)
    return out


def write_autosave(db_path, entries, keep=AUTOSAVE_KEEP):
    bdir = backup_dir_for(db_path)
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(bdir, "{}{}.json".format(AUTOSAVE_PREFIX, stamp))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(bdir, "{}{}_{}.json".format(AUTOSAVE_PREFIX, stamp, n))
    save_database(path, entries)
    backups = _list_autosave_files(bdir)
    for old in backups[:-keep] if len(backups) > keep else []:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def latest_autosave(db_path):
    files = _list_autosave_files(backup_dir_for(db_path))
    return files[-1] if files else None


def autosave_seen_marker_path(db_path):
    return os.path.join(backup_dir_for(db_path), AUTOSAVE_SEEN_MARKER)


def mark_autosave_seen(db_path, backup_file=None):
    if backup_file is None:
        backup_file = latest_autosave(db_path) or ""
    try:
        with open(autosave_seen_marker_path(db_path), "w", encoding="utf-8") as f:
            f.write(os.path.basename(backup_file or ""))
    except OSError:
        pass


def unseen_autosave(db_path):
    """Newest autosave that is (a) unseen and (b) genuinely newer than the
    database file on disk. Prevents stale prompts after a successful save
    and blocks phantom/hijacked snapshots inside the backup folder."""
    latest = latest_autosave(db_path)
    if not latest:
        return None
    try:
        with open(autosave_seen_marker_path(db_path), "r", encoding="utf-8") as f:
            seen = f.read().strip()
    except OSError:
        seen = ""
    if seen == os.path.basename(latest):
        return None
    try:
        if os.path.isfile(db_path) and \
                os.path.getmtime(latest) <= os.path.getmtime(db_path):
            return None
    except OSError:
        pass
    return latest


# --------------------------------------------------------------------------
# PERSISTENT UNDO/REDO HISTORY  (backup/history.json)
# --------------------------------------------------------------------------
# Mirrors the in-memory op format the app already records (kind/desc/when/
# changes), minus the two keys that are only meaningful as LIVE Python
# object references within one running session (ref_before/ref_after).
# Those two keys exist purely as a same-session fast-path identity lookup
# in _find_slot(); the id-based fallback _find_slot already has is exactly
# what cross-session (file-based) replay uses, so persisting just the
# snapshots (copy_before/copy_after) plus pos_hint is enough to
# faithfully replay history after a restart. An op whose target entry can
# no longer be located (e.g. the id was removed by something other than
# undo/redo) is simply skipped when applied -- same as the existing
# same-session behavior; history is a convenience, not a ledger.
#
# FORMAT v2 (M-4): instead of embedding two FULL entry copies per change
# (which made a single Fix All of N entries serialize 2N complete entries
# -- history.json ballooned to tens of MB and was rewritten from scratch
# after every subsequent op), each change stores one op-level "id"
# capture plus a FIELD-LEVEL DIFF of only the fields that actually
# changed. An insert (add) stores the full new entry (it must, to be able
# to re-materialize the entry on undo); a delete stores the full old
# entry. Edits store {field: old_value} / {field: new_value} maps. This
# keeps v2 files 5-50x smaller for bulk ops while remaining fully
# replayable. v1 files (full copies, no version key or version==1) remain
# readable: load_history accepts both formats; only writes are v2.
HISTORY_FILENAME = "history.json"
HISTORY_VERSION = 2


def history_path_for(db_path):
    return os.path.join(backup_dir_for(db_path), HISTORY_FILENAME)


def _entry_diff(before, after):
    """{field: old_value} / {field: new_value} maps for the fields that
    differ between two entries (dict order = SCHEMA order)."""
    olds, news = {}, {}
    for f in SCHEMA_FIELDS:
        b = before.get(f) if before else None
        a = after.get(f) if after else None
        if b != a:
            olds[f] = b
            news[f] = a
    return olds, news


def _op_to_json(op):
    changes = []
    for ch in op.get("changes", []):
        cb, ca = ch.get("copy_before"), ch.get("copy_after")
        if cb is None:
            # insertion: keep the full new entry (undo must re-create it)
            changes.append({
                "pos_hint": ch.get("pos_hint"),
                "action": "add",
                "after": ca,
            })
        elif ca is None:
            # deletion: keep the full old entry (redo must re-create it)
            changes.append({
                "pos_hint": ch.get("pos_hint"),
                "action": "delete",
                "before": cb,
            })
        else:
            olds, news = _entry_diff(cb, ca)
            if not olds:
                continue            # no net change: nothing to persist
            changes.append({
                "pos_hint": ch.get("pos_hint"),
                "action": "edit",
                "id": cb.get("id"),
                "old": olds,
                "new": news,
            })
    return {
        "kind": op.get("kind", ""),
        "desc": op.get("desc", ""),
        "when": op.get("when", ""),
        "changes": changes,
    }


def _change_from_v2(raw_ch):
    """Rebuild an in-memory change dict (ref_before/ref_after/copy_before/
    copy_after) from one v2 change record. The rebuilt dicts can never
    identity-match a live entry, so _find_slot() correctly falls through
    to its id-based lookup -- exactly the cross-session behavior."""
    action = raw_ch.get("action")
    pos_hint = raw_ch.get("pos_hint") if isinstance(raw_ch.get("pos_hint"), int) else 0
    if action == "add":
        ca = raw_ch.get("after")
        return {"pos_hint": pos_hint, "ref_before": None, "copy_before": None,
                "ref_after": ca, "copy_after": ca} if isinstance(ca, dict) else None
    if action == "delete":
        cb = raw_ch.get("before")
        return {"pos_hint": pos_hint, "ref_before": cb, "copy_before": cb,
                "ref_after": None, "copy_after": None} if isinstance(cb, dict) else None
    if action == "edit":
        cb = raw_ch.get("before")     # absent in v2
        olds = raw_ch.get("old") if isinstance(raw_ch.get("old"), dict) else {}
        news = raw_ch.get("new") if isinstance(raw_ch.get("new"), dict) else {}
        if not news:
            return None
        # Re-materialize full before/after copies so the in-memory replay
        # path (which swaps whole entries) works unchanged: start from the
        # "after" image, then back-apply the old values.
        after = dict(news)
        after.setdefault("id", raw_ch.get("id"))
        before = dict(after)
        for f, v in olds.items():
            before[f] = v
        return {"pos_hint": pos_hint,
                "ref_before": before, "copy_before": before,
                "ref_after": after, "copy_after": after}
    return None


def _op_from_json(raw):
    if not isinstance(raw, dict):
        return None
    changes = raw.get("changes")
    if not isinstance(changes, list):
        return None
    out_changes = []
    for ch in changes:
        if not isinstance(ch, dict):
            return None
        if "action" in ch:
            # v2 record
            built = _change_from_v2(ch)
            if built is not None:
                out_changes.append(built)
            continue
        # v1 record (full copy_before/copy_after pair)
        cb, ca = ch.get("copy_before"), ch.get("copy_after")
        out_changes.append({
            "pos_hint": ch.get("pos_hint") if isinstance(ch.get("pos_hint"), int) else 0,
            # _apply_history_changes() distinguishes add/delete/edit by
            # None-ness of ref_before/ref_after, not just as an identity
            # fast-path -- so this must preserve that None <-> "no entry
            # existed there" invariant, not just blank both out. Reusing
            # the deserialized copy itself as the "ref" is safe: it can
            # never identity-match a live entry (harmless no-op check),
            # so _find_slot() correctly falls through to its id-based
            # lookup every time, which is exactly the intended
            # cross-session behavior.
            "ref_before": cb, "ref_after": ca,
            "copy_before": cb, "copy_after": ca,
        })
    return {
        "kind": str(raw.get("kind", "")),
        "desc": str(raw.get("desc", "")),
        "when": str(raw.get("when", "")),
        "changes": out_changes,
    }


def write_history(db_path, history, redo_stack):
    """Persist the undo/redo stacks atomically to backup/history.json.
    Best-effort: a failure here never blocks the edit that triggered it."""
    if not db_path:
        return
    path = history_path_for(db_path)
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": HISTORY_VERSION,
            "history": [_op_to_json(op) for op in history],
            "redo_stack": [_op_to_json(op) for op in redo_stack],
        }
        write_text_atomic(
            path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        log("History save failed: {}".format(e))


def load_history(db_path):
    """Returns (history, redo_stack) reconstructed from backup/history.json,
    or ([], []) when missing/corrupt (a clean start, never a hard error)."""
    if not db_path:
        return [], []
    path = history_path_for(db_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    history = [op for op in (_op_from_json(o) for o in data.get("history", []) or [])
               if op is not None]
    redo_stack = [op for op in (_op_from_json(o) for o in data.get("redo_stack", []) or [])
                  if op is not None]
    return history, redo_stack


# --------------------------------------------------------------------------
# AUDIT ENGINE
# --------------------------------------------------------------------------

class AuditIssue:
    def __init__(self, category, entry_index, entry_id, message, fix=None,
                 severity="warning", code="", subject=None):
        self.category = category
        self.entry_index = entry_index
        self.entry_id = entry_id
        self.message = message
        # fix(entries_list) -> position mutated (int) or None when stale
        self.fix = fix
        self.severity = severity
        # stable waiver identity: "code" classifies the finding kind (so a
        # waiver survives message/value changes), "subject" is what it
        # attaches to (entry id, or a file path for file-level findings)
        self.code = code or category.lower().replace(" ", "-")
        self.subject = subject if subject is not None else entry_id

    def waiver_key(self):
        return "{}|{}|{}".format(self.subject, self.category, self.code)

    def __repr__(self):
        return "<AuditIssue {} {} {}>".format(self.category, self.entry_id, self.message)


# --------------------------------------------------------------------------
# IGNORED AUDIT FINDINGS (user-waived issues, persisted in backup/ignored.json)
# --------------------------------------------------------------------------
WAIVER_FILENAME = "ignored.json"
# Pre-reorg location, kept only so existing installs migrate cleanly instead
# of silently losing their waivers the first time this version runs.
_LEGACY_WAIVER_FILENAME = "audit_waivers.json"


def waivers_path_for(db_path):
    return os.path.join(backup_dir_for(db_path), WAIVER_FILENAME)


def _legacy_waivers_path_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or "")),
                        _LEGACY_WAIVER_FILENAME)


def load_waivers(db_path):
    """Set of waiver keys persisted in the backup folder. Corrupt or
    missing files simply mean 'no waivers'. One-time migration: if the old
    beside-the-database file exists but the new one doesn't yet, read the
    old file and adopt it (the old file is left in place untouched -- only
    copied forward -- so nothing is destroyed if something goes wrong)."""
    path = waivers_path_for(db_path)
    if not os.path.isfile(path):
        legacy = _legacy_waivers_path_for(db_path)
        if os.path.isfile(legacy):
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    data = json.load(f)
                waived = data.get("waived", []) if isinstance(data, dict) else data
                if isinstance(waived, list):
                    migrated = {str(k) for k in waived if isinstance(k, str) and k}
                    save_waivers(db_path, migrated)
                    return migrated
            except (OSError, ValueError):
                pass
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("waived", [])
    if not isinstance(data, list):
        return set()
    return {str(k) for k in data if isinstance(k, str) and k}


def save_waivers(db_path, waivers):
    """Persist waiver keys atomically into the backup folder."""
    path = waivers_path_for(db_path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    payload = {"version": 1,
               "waived": sorted(str(w) for w in waivers if w)}
    write_text_atomic(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# FUZZY DUPLICATE DETECTION (soft audit)
# --------------------------------------------------------------------------
# Flags SUSPICIOUS near-duplicate pairs as warning-level findings; the user
# always decides (Merge... in the Audit tab combines them, Ignore/waiver
# dismisses the pair for good). Never auto-fixed.

# Generation/edition tokens that legitimately separate product generations
# ("Chu" vs "Chu II" are DIFFERENT products by design). When all that
# differs between two names is tokens from this set, the pair is NOT
# flagged. Edition words that are NOT here (e.g. "Pro", "Red", "MK2" is,
# "Studio" is not) still get flagged for manual review.
_GEN_TOKENS = {
    "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "8",
    "ii": "2", "iii": "3", "iv": "4", "vi": "6",
    "mk2": "mk2", "mk3": "mk3", "mk4": "mk4",
    "dsp": "dsp",
}

# SequenceMatcher ratio above which two same-brand names are considered
# "very similar" (typos, restyled spellings).
_DUP_MIN_RATIO = 0.84

# M-5: cap on pairwise duplicate findings emitted per shared measurement
# file. Beyond the cap the remaining pairs collapse into one summary row.
DUP_PAIR_CAP_PER_FILE = 50


def _dup_norm_name(entry):
    """Normalized 'model variant' string used for duplicate comparison.
    Roman-numeral generations are folded to digits so 'Chu II' and
    'Chu 2' compare equal (flagging them is correct: same product,
    inconsistent styling -- exactly what the ID rules forbid)."""
    parts = [str(entry.get("model") or ""), str(entry.get("variant") or "")]
    text = " ".join(p for p in parts if p).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = [_GEN_TOKENS.get(t, t) for t in text.split()]
    return " ".join(tokens)


def _dup_specs_match(a, b):
    """How many of (year, price_usd, driver_config) genuinely agree between
    the two entries (0..3). Identical specs on 'different' products raise
    the suspicion; differing specs hint at legit generations/variants.
    Missing/zero values on BOTH sides count as NON-matching: two entries
    that simply lack specs must not corroborate each other."""
    same = 0
    for f in ("year", "price_usd", "driver_config"):
        va, vb = a.get(f), b.get(f)

        def _missing(v):
            return v in (None, "", 0) or str(v).strip() == ""

        if _missing(va) or _missing(vb):
            continue
        try:
            if int(str(va)) == int(str(vb)):
                same += 1
        except (TypeError, ValueError):
            if str(va) == str(vb):
                same += 1
    return same


# "k67" vs "k167", "liberty 2" vs "liberty 3": one token differs and both
# differing tokens are the SAME model-number shape -- identical alpha
# prefix/suffix around digits that differ. That is a generation/model
# number, not a duplicate ("MACH 10" vs "MACH 80" are different products).
_MODEL_NUM_RE = re.compile(r"^([a-z]*)(\d{1,5})([a-z]*)$")


def _model_number_siblings(x, y):
    """True when x and y are model numbers of the same shape that differ
    only in their digits ('k67'/'k167', '2'/'3', 'mk2' is NOT -- letters
    inside keep it out via prefix/suffix mismatch... 'mk2' vs 'mk3' share
    prefix 'mk' so they ARE siblings by this rule, matching _GEN_TOKENS)."""
    mx = _MODEL_NUM_RE.match(x)
    my = _MODEL_NUM_RE.match(y)
    if not mx or not my:
        return False
    return ((mx.group(1), mx.group(3)) == (my.group(1), my.group(3))
            and mx.group(2) != my.group(2))


def _dup_prefix_pair(na, nb):
    """(shorter, longer, remainder-tokens) when one normalized name is a
    whole-word prefix of the other, else None."""
    if na == nb:
        return None
    if nb.startswith(na + " "):
        return na, nb, nb[len(na):].strip()
    if na.startswith(nb + " "):
        return nb, na, na[len(nb):].strip()
    return None


def find_duplicate_pairs(entries, referenced_map=None):
    """Scan for likely duplicate entries. Returns warning AuditIssues with
    a .pair_ids attribute (id_a, id_b) consumed by the Audit tab's
    Merge... action. Waiver subject is the sorted id pair, so dismissing
    one pair never hides another."""
    referenced_map = referenced_map or {}
    issues = []
    if len(entries) < 2:
        return issues

    seen_pairs = set()
    pending = []           # (rank, idx_a, idx_b, confidence, reasons)
    _RANK = {"high": 0, "medium": 1, "low": 2}

    def emit(idx_a, idx_b, confidence, reasons):
        key = (min(idx_a, idx_b), max(idx_a, idx_b))
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        pending.append((_RANK.get(confidence, 3), idx_a, idx_b,
                        confidence, list(reasons)))

    # -- signal 1: shared measurement files (strongest, any brand) --------
    # (Duplicate File Link already reports the overlap per entry; here it
    # upgrades the pair to a high-confidence duplicate candidate.)
    # M-5: one file linked to K entries would otherwise emit K*(K-1)/2
    # pair findings -- a mistakenly mass-linked file with hundreds of
    # entries made the audit list unusable. The first pairs are emitted
    # as normal; the remainder collapse into one summary row.
    shared_boost = set()
    shared_pairs_emitted = 0
    shared_overflow = []            # [(rel, n_entries)] for the summary row
    for rel, idxs in referenced_map.items():
        uniq = sorted(set(idxs))
        if len(uniq) < 2:
            continue
        pair_count = len(uniq) * (len(uniq) - 1) // 2
        if shared_pairs_emitted + pair_count > DUP_PAIR_CAP_PER_FILE:
            shared_overflow.append((rel, len(uniq)))
            continue
        shared_pairs_emitted += pair_count
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                key = (uniq[i], uniq[j])
                shared_boost.add(key)
                emit(uniq[i], uniq[j], "high",
                     ["both entries link measurement file '{}'".format(rel)])
    for rel, n_entries in shared_overflow:
        pending.append((0, -1, -1, "high",
                        ["mass-link", "{} entries share measurement file '{}' "
                         "-- review the Duplicate File Link rows for '{}' "
                         "instead of this pairwise list".format(n_entries, rel, rel)]))

    # -- signal 2: same-brand name similarity -----------------------------
    by_brand = {}
    for idx, e in enumerate(entries):
        brand = str(e.get("brand") or "").strip().lower()
        by_brand.setdefault(brand, []).append(idx)

    matcher = difflib.SequenceMatcher
    for brand, idxs in by_brand.items():
        if not brand or len(idxs) < 2:
            continue
        names = {}
        for i in idxs:
            try:
                names[i] = _dup_norm_name(entries[i])
            except Exception:                      # noqa: BLE001 - never abort audit
                names[i] = ""
        # cheap pairwise pre-filters keep the expensive SequenceMatcher
        # calls rare: at 5k entries a brand can hold hundreds of names and
        # a naive O(n^2) ratio() pass costs seconds.
        info = {}
        for i, na in names.items():
            toks = frozenset(na.split())
            info[i] = (na, len(na), toks)
        for ii in range(len(idxs)):
            for jj in range(ii + 1, len(idxs)):
                ia, ib = idxs[ii], idxs[jj]
                key = (min(ia, ib), max(ia, ib))
                if key in seen_pairs:
                    continue
                na, nb = info[ia][0], info[ib][0]
                if not na or not nb:
                    continue
                a, b = entries[ia], entries[ib]
                reasons = []
                confidence = None

                # generation guard: "Chu" vs "Chu II" is by design; a
                # pure-digit remainder ("Model 1" vs "Model 100") is a
                # different model number, also by design
                prefix = _dup_prefix_pair(na, nb)
                if prefix is not None:
                    _short, _long, rest = prefix
                    rest_tokens = set(rest.split())
                    if rest_tokens and (
                            rest_tokens.issubset(_GEN_TOKENS)
                            or all(t.isdigit() for t in rest_tokens)):
                        continue

                # mid-name model-number guard: when the token SETS differ
                # by exactly one token per side and those tokens are the
                # same model-number shape with different digits
                # ("liberty 2 pro"/"liberty 3 pro", "k67"/"k167"), the two
                # entries are distinct generations -- the trailing-name
                # guard above cannot see these because the number is not
                # name-final.
                ta_, tb_ = info[ia][2], info[ib][2]
                if ta_ != tb_:
                    da = ta_ - tb_
                    db_ = tb_ - ta_
                    if len(da) == 1 and len(db_) == 1 and \
                            _model_number_siblings(next(iter(da)),
                                                   next(iter(db_))):
                        continue

                # --- cheap rejection filters (no difflib yet) ----------
                la, lb = info[ia][1], info[ib][1]
                # length upper bound: ratio can never exceed 2*min/sum
                if 2.0 * min(la, lb) / (la + lb) < _DUP_MIN_RATIO \
                        and prefix is None:
                    continue
                # common prefix + digit-suffix rule: "model123" vs
                # "model456" (or "model1" vs "model11") share a prefix but
                # are different model numbers, not duplicates
                p = 0
                for ca, cb in zip(na, nb):
                    if ca != cb:
                        break
                    p += 1
                if p >= 3:
                    sa_, sb_ = na[p:], nb[p:]
                    if ((sa_.isdigit() or not sa_)
                            and (sb_.isdigit() or not sb_)
                            and (sa_ or sb_) and sa_ != sb_):
                        continue
                elif not (info[ia][2] & info[ib][2]):
                    # no shared token and little shared prefix: only a
                    # tiny edit (typo) can still matter
                    if abs(la - lb) > 2 or p < 2:
                        continue

                ratio = matcher(None, na, nb).ratio()
                if ratio >= _DUP_MIN_RATIO:
                    confidence = "high" if ratio >= 0.93 else "medium"
                    reasons.append(
                        "very similar names ({:.0f}% alike)".format(ratio * 100))
                elif prefix is not None:
                    # extends the name with a NON-generation word
                    # ("Chu" vs "Chu Pro"): worth a manual look
                    confidence = "low"
                    reasons.append(
                        "one name extends the other ('{}' vs '{}')".format(
                            prefix[0], prefix[1]))
                if confidence is None:
                    continue

                spec_n = _dup_specs_match(a, b)
                shares_files = ((ia, ib) in shared_boost
                                or (ib, ia) in shared_boost)
                similar_names = confidence in ("medium", "high") and \
                    any("alike" in r for r in reasons)
                if shares_files:
                    # strongest possible signal, any brand
                    confidence = "high"
                    reasons.append("entries also share linked measurement files")
                elif similar_names and spec_n == 3:
                    # typo/restyled double-entry of the SAME product:
                    # near-identical name AND year + price + driver all agree
                    confidence = "high"
                elif confidence == "high":
                    # Name shape alone is weak evidence: sibling models
                    # (K67/K167, Liberty 2/3 Pro) coincidentally share
                    # specs far too often to call them duplicates on
                    # similarity alone.
                    confidence = "medium"
                    reasons.append(
                        "similarity without corroborating specs")
                # prefix-extension findings ("Chu" vs "Chu Pro") stay LOW:
                # official variants legitimately extend names, so only a
                # shared measurement file (above) may escalate them.
                emit(ia, ib, confidence, reasons)

    # materialize findings best-first (high confidence at the top)
    pending.sort(key=lambda t: (t[0], t[1], t[2]))
    for rank, idx_a, idx_b, confidence, reasons in pending:
        if idx_a < 0 or idx_b < 0:
            # mass-link summary row (M-5): standalone, not a mergeable pair
            detail = reasons[1] if len(reasons) > 1 else "; ".join(reasons)
            issues.append(AuditIssue(
                "Possible Duplicate", -1, "(summary)",
                "Possible duplicate (high confidence): mass-linked "
                "measurement file. {} Review the Duplicate File Link rows "
                "for the file instead of this pairwise list.".format(detail),
                severity="warning", code="dup-masslink",
                subject="dup|mass|{}".format(detail[:80])))
            continue
        a, b = entries[idx_a], entries[idx_b]
        id_a = a.get("id") or "entry #{}".format(idx_a)
        id_b = b.get("id") or "entry #{}".format(idx_b)
        msg = ("Possible duplicate ({} confidence): '{}' and '{}' -- {}. "
               "Review both: 'Merge...' combines them, 'Ignore' dismisses "
               "this pair for good.".format(
                   confidence, id_a, id_b, "; ".join(reasons)))
        iss = AuditIssue(
            "Possible Duplicate", idx_a, id_a, msg,
            severity="warning", code="dup-pair",
            subject="dup|{}|{}".format(*sorted([id_a, id_b])))
        iss.pair_ids = (id_a, id_b)
        iss.confidence = confidence   # "high" / "medium" / "low" -- read by
                                       # the Audit tab's confidence filter
                                       # instead of re-parsing the message
        issues.append(iss)
    return issues


def run_full_audit(entries, data_root=None):
    """
    Returns list of AuditIssue. Fix closures capture the audited ENTRY
    OBJECT and resolve it at apply time by identity first, then by unique
    id; duplicate ids make the fix report stale instead of guessing.
    Each fix returns the list position it mutated, or None.
    """
    issues = []
    seen_ids = {}
    disk_index = None

    def _resolve(entries_list, target_obj, eid, frozen_idx):
        if target_obj is not None:
            for p, e in enumerate(entries_list):
                if e is target_obj:
                    return p
        tid = eid if eid and eid != "-" and not eid.startswith("(no id)") else None
        if tid:
            hits = [p for p, e in enumerate(entries_list) if e.get("id") == tid]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return -1                      # ambiguous: refuse to guess
        if 0 <= frozen_idx < len(entries_list) \
                and not entries_list[frozen_idx].get("id"):
            return frozen_idx
        return -1

    def make_fix(target_obj, eid, frozen_idx, mutator):
        """mutator(entry) applies the change to the located entry dict."""
        def fix(entries_list):
            p = _resolve(entries_list, target_obj, eid, frozen_idx)
            if p < 0:
                return None
            mutator(entries_list[p])
            return p
        return fix

    on_disk_set = None
    referenced_map = {}
    for idx, entry in enumerate(entries):
        for rel in entry.get("files", []) or []:
            if not isinstance(rel, str):
                continue                       # reported per-entry below
            norm = rel.replace("\\", "/")
            norm = re.sub(r"/+", "/", norm.strip())
            # "./data/x.txt" and "data/./x.txt" are the same file as
            # "data/x.txt"; without collapsing "." segments they would
            # false-positive as Missing File against the disk walk.
            while norm.startswith("./"):
                norm = norm[2:]
            norm = re.sub(r"/\./", "/", norm)
            if norm:
                referenced_map.setdefault(norm, []).append(idx)
    if data_root:
        on_disk, data_dir = scan_data_files(data_root)
        if on_disk:
            on_disk_set = set(on_disk)
            disk_index = {p.lower(): p for p in on_disk}
        elif data_dir is not None:
            # data dir exists but empty — keep empty set so Missing File
            # checks still run (on_disk_set stays empty rather than None)
            on_disk_set = set()
            disk_index = {}

    # brand-spelling prepass: needs to see every entry's brand before it can
    # tell majority from minority spelling, so it runs once here rather than
    # inline in the per-entry loop below (see brand_spelling_fixes).
    _brand_fixes = brand_spelling_fixes(entries)

    for idx, entry in enumerate(entries):
        eid = entry.get("id", "") or "(no id) #{}".format(idx)

        real_id = entry.get("id", "")
        if real_id:
            if real_id in seen_ids:
                first_idx = seen_ids[real_id]
                issues.append(AuditIssue(
                    "Duplicate ID", idx, eid,
                    "Duplicate id '{}' also used by entry #{}.".format(real_id, first_idx),
                    severity="error",
                    # Per-row subject: every twin shares the same entry_id,
                    # and an unscoped subject would make waiving one row
                    # hide ALL of them.
                    subject="{}@{}".format(real_id, idx),
                ))
            else:
                seen_ids[real_id] = idx

        expected_id = build_id(entry.get("brand", ""), entry.get("model", ""), entry.get("variant", ""))
        # build_id never emits leading/trailing underscores, so whenever the
        # stored id already matches it the underscore check is moot.
        if expected_id and entry.get("id") != expected_id:
            has_leading_trailing = entry.get("id", "").startswith("_") or \
                entry.get("id", "").endswith("_")
            if has_leading_trailing:
                msg = "ID '{}' should be '{}' (has leading/trailing underscore).".format(entry.get("id"), expected_id)
            else:
                msg = "ID '{}' should be '{}'.".format(entry.get("id"), expected_id)
            issues.append(AuditIssue(
                "ID Format", idx, eid, msg,
                fix=make_fix(entry, real_id or eid, idx,
                             lambda en, exp=expected_id: en.__setitem__("id", exp)),
            ))

        # H-1: surface non-Latin identity components explicitly. Their
        # ASCII fold is empty, so the generated id carries an opaque hash
        # suffix instead of the brand/model spelling. Never auto-fixed:
        # the correct romanization is a human decision (the auto-fix
        # would just re-apply the same hashed id).
        for fname, fval in (("brand", entry.get("brand")),
                            ("model", entry.get("model")),
                            ("variant", entry.get("variant"))):
            val = str(fval or "").strip()
            if val and not normalize_component(val):
                issues.append(AuditIssue(
                    "ID Format", idx, eid,
                    "{} '{}' has no Latin-alphabet spelling -- its id uses a "
                    "hash fallback ('{}'). Add an official romanization to "
                    "keep the id readable and stable.".format(
                        fname.capitalize(), val, expected_id or "(no id)"),
                    severity="warning", code="id-nonlatin",
                ))

        if idx in _brand_fixes:
            current, canonical, summary = _brand_fixes[idx]

            def _brand_mut(en, val=canonical):
                en["brand"] = val
                new_id = build_id(val, en.get("model", ""), en.get("variant", ""))
                if new_id:
                    en["id"] = new_id

            issues.append(AuditIssue(
                "Brand Spelling", idx, eid,
                "Brand '{}' is spelled inconsistently with other entries "
                "for the same brand ({}). Auto-fix renames it to the "
                "majority spelling '{}' and rebuilds the id.".format(
                    current, summary, canonical),
                fix=make_fix(entry, real_id or eid, idx, _brand_mut),
                severity="warning", code="brand-spelling",
            ))

        dc = entry.get("driver_config", "")
        has_ws = bool(dc and re.search(r"\s", dc))
        if has_ws:
            fixed_dc = re.sub(r"\s+", "", dc)
            issues.append(AuditIssue(
                "Driver Config", idx, eid,
                "driver_config '{}' contains whitespace.".format(dc),
                fix=make_fix(entry, real_id or eid, idx,
                             lambda en, val=fixed_dc: en.__setitem__("driver_config", val)),
                code="dc-whitespace",
            ))

        parsed = parse_driver_config(entry.get("driver_config", ""))
        dt = (entry.get("driver_type") or "").strip()
        dc_stripped = (entry.get("driver_config") or "").strip()

        unknown_tokens = driver_config_unknown_tokens(dc_stripped) if dc_stripped else []
        if unknown_tokens:
            clean_cfg = classify_driver(parsed)[1] if parsed else ""
            issues.append(AuditIssue(
                "Driver Config", idx, eid,
                "driver_config '{}' contains unknown token(s): {}. Auto-fix rewrites it to '{}'.".format(
                    dc_stripped, ", ".join(unknown_tokens), clean_cfg),
                fix=make_fix(entry, real_id or eid, idx,
                             lambda en, val=clean_cfg: en.__setitem__("driver_config", val)),
                severity="warning", code="dc-unknown",
            ))

        if dt and not dc_stripped:
            issues.append(AuditIssue(
                "Driver Type", idx, eid,
                "driver_type '{}' present but driver_config is empty.".format(dt),
                fix=make_fix(entry, real_id or eid, idx,
                             lambda en: en.__setitem__("driver_type", "")),
                severity="error", code="dt-no-config",
            ))
        elif dc_stripped and not dt:
            if parsed:
                expected_type, _ = classify_driver(parsed)
                issues.append(AuditIssue(
                    "Driver Type", idx, eid,
                    "driver_type missing but driver_config '{}' implies '{}'.".format(dc_stripped, expected_type),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, val=expected_type: en.__setitem__("driver_type", val)),
                    severity="error", code="dt-no-type",
                ))
        elif parsed:
            expected_type, expected_config = classify_driver(parsed)
            if dt != expected_type:
                issues.append(AuditIssue(
                    "Driver Type", idx, eid,
                    "driver_type '{}' does not match driver_config '{}' (expected '{}').".format(
                        dt, dc_stripped, expected_type),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, val=expected_type: en.__setitem__("driver_type", val)),
                    code="dt-mismatch",
                ))
            if CANONICALIZE_DRIVER_ORDER and not has_ws and dc_stripped != expected_config:
                issues.append(AuditIssue(
                    "Driver Config", idx, eid,
                    "driver_config '{}' not in canonical order (expected '{}').".format(
                        dc_stripped, expected_config),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, val=expected_config: en.__setitem__("driver_config", val)),
                    severity="info", code="dc-order",
                ))

        ff = (entry.get("form_factor") or "").strip()
        conn = (entry.get("connector") or "").strip()
        if not ff:
            issues.append(AuditIssue(
                "Form/Connector Mismatch", idx, eid,
                "Form factor is missing/empty.",
                severity="error", code="ff-missing"))
        elif ff not in FORM_FACTORS:
            issues.append(AuditIssue(
                "Form/Connector Mismatch", idx, eid,
                "Form factor '{}' is not an approved value.".format(ff),
                severity="error", code="ff-invalid"))
        if not conn:
            issues.append(AuditIssue(
                "Form/Connector Mismatch", idx, eid,
                "Connector is missing/empty.",
                severity="error", code="conn-missing"))
        elif conn not in CONNECTORS_ALL:
            issues.append(AuditIssue(
                "Form/Connector Mismatch", idx, eid,
                "Connector '{}' is not an approved value.".format(conn),
                severity="error", code="conn-invalid"))
        if ff in FORM_CONNECTOR_MAP and conn in CONNECTORS_ALL and conn not in FORM_CONNECTOR_MAP[ff]:
            issues.append(AuditIssue(
                "Form/Connector Mismatch", idx, eid,
                "Connector '{}' is not allowed for form factor '{}'. Allowed: {}".format(
                    conn, ff, ", ".join(FORM_CONNECTOR_MAP[ff])),
                severity="error", code="conn-matrix"))

        if ENFORCE_TWS_ZERO_SPECS and ff == TWS_FORM_FACTOR:
            imp_val = coerce_int(entry.get("impedance", 0), -1)
            sen_val = coerce_int(entry.get("sensitivity", 0), -1)
            if imp_val != 0 or sen_val != 0:
                def tws_mut(en):
                    en["impedance"] = 0
                    en["sensitivity"] = 0
                issues.append(AuditIssue(
                    "TWS Specs", idx, eid,
                    "TWS entries must have impedance/sensitivity = 0 (got {}/{}).".format(imp_val, sen_val),
                    fix=make_fix(entry, real_id or eid, idx, tws_mut),
                    severity="error",
                ))

        for field, label in (("impedance", "Impedance"), ("sensitivity", "Sensitivity")):
            raw = entry.get(field, 0)
            try:
                fv = float(raw)
                finite = math.isfinite(fv)
            except (TypeError, ValueError):
                fv = None
                finite = False
            if not finite:
                issues.append(AuditIssue(
                    "Spec Sanity", idx, eid,
                    "{} '{}' is not a usable number (reset to 0).".format(label, raw),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, f=field: en.__setitem__(f, 0)),
                    severity="error", code="spec-nan",
                ))
                continue
            if fv < 0:
                issues.append(AuditIssue(
                    "Spec Sanity", idx, eid,
                    "{} {} is negative (reset to 0).".format(label, raw),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, f=field: en.__setitem__(f, 0)),
                    severity="error", code="spec-negative",
                ))
            elif fv != int(fv):
                rounded = int(math.floor(fv + 0.5))
                issues.append(AuditIssue(
                    "Spec Sanity", idx, eid,
                    "{} must be a whole integer ({:.1f} -> {}).".format(label, fv, rounded),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, f=field, val=rounded: en.__setitem__(f, val)),
                    severity="warning", code="spec-float",
                ))
            elif field == "impedance":
                # Electrostatics are rated by 10 kHz capacitive reactance
                # (100k-360k per the AUDIT PROMPT) -- applies both to
                # full-size estat earspeakers (connector "Electrostatic")
                # and to electrostatic IEMs (driver_type "EST" on a wired
                # connector, e.g. STAX SR-001 with "Fixed Cable").
                is_est = ((entry.get("connector") or "").strip() == "Electrostatic"
                          or (entry.get("driver_type") or "").strip() == "EST")
                cap = IMPEDANCE_MAX_ELECTROSTATIC if is_est else IMPEDANCE_MAX
                if fv > cap:
                    issues.append(AuditIssue(
                        "Spec Sanity", idx, eid,
                        "{} {} is above the usual range (advisory).".format(label, int(fv)),
                        severity="warning", code="spec-range",
                    ))
            elif fv > SENSITIVITY_MAX:
                issues.append(AuditIssue(
                    "Spec Sanity", idx, eid,
                    "{} {} is above the usual range (advisory).".format(label, int(fv)),
                    severity="warning", code="spec-range",
                ))

        files = entry.get("files", []) or []
        if disk_index:
            fixed_paths = []
            changed_casing = False
            for pos, rel in enumerate(files):
                actual = disk_index.get(rel.lower()) if isinstance(rel, str) else None
                if actual and actual != rel:
                    fixed_paths.append((pos, actual))
                    changed_casing = True
            if changed_casing:
                def case_mut(en, updates=tuple(fixed_paths)):
                    lst = en.get("files", [])
                    for pos, actual in updates:
                        if 0 <= pos < len(lst) and str(lst[pos]).lower() == actual.lower():
                            lst[pos] = actual
                shown = ", ".join("'{}'->'{}'".format(o, a) for o, a in fixed_paths[:2])
                issues.append(AuditIssue(
                    "Path Casing", idx, eid,
                    "File path casing differs from disk: {}. Auto-fix restores the on-disk spelling.".format(shown),
                    fix=make_fix(entry, real_id or eid, idx, case_mut),
                    severity="warning",
                ))

        price = entry.get("price_usd", 0)
        tier_basis = price_tier_basis(price)
        expected_tier = price_tier_for(tier_basis)
        tags = entry.get("tags", []) or []
        present_tiers = [t for t in tags if t in PRICE_TIER_TAGS]

        def tier_mut(en, expected=expected_tier):
            # Replace the existing tier tag IN ITS ORIGINAL POSITION (and
            # drop any extra tier tags) instead of stripping all tiers and
            # appending at the end, so fixes produce minimal, readable diffs.
            out = []
            placed = False
            for t in en.get("tags", []):
                if t in PRICE_TIER_TAGS:
                    if not placed:
                        out.append(expected)
                        placed = True
                    # additional tier tags are dropped silently
                else:
                    out.append(t)
            if not placed:
                out.append(expected)
            en["tags"] = out

        if present_tiers != [expected_tier]:
            issues.append(AuditIssue(
                "Price Tier Tag", idx, eid,
                "Price tier tag(s) {} do not match price ${} (expected '{}').".format(
                    present_tiers or "(none)", tier_basis, expected_tier),
                fix=make_fix(entry, real_id or eid, idx, tier_mut),
            ))

        try:
            # Single coercion path (half-up via coerce_int) so the audit
            # message always agrees with validate_entry / build_clean_entry;
            # the old int(float(price)) branch truncated .5 values the other
            # way and could disagree by $1.
            p = coerce_int(price, None)
            if p is None or (isinstance(price, float) and not math.isfinite(price)):
                raise ValueError
            if p < 0:
                issues.append(AuditIssue(
                    "Price Rounding", idx, eid,
                    "Price ${} cannot be negative.".format(p),
                    severity="error", code="price-negative"))
            elif p % 5 != 0:
                rounded = round_price_to_5(p)
                issues.append(AuditIssue(
                    "Price Rounding", idx, eid,
                    "Price ${} is not a multiple of $5 (should be ${}).".format(p, rounded),
                    fix=make_fix(entry, real_id or eid, idx,
                                 lambda en, val=rounded: en.__setitem__("price_usd", val)),
                    code="price-rounding",
                ))
        except (TypeError, ValueError):
            issues.append(AuditIssue(
                "Price Rounding", idx, eid,
                "Price '{}' is not a valid integer.".format(price),
                severity="error", code="price-invalid"))

        if not is_valid_year(entry.get("year", 0)):
            issues.append(AuditIssue(
                "Year", idx, eid,
                "Year '{}' is not a valid 4-digit year.".format(entry.get("year")),
                severity="error"))

        # ---- missing / placeholder data -----------------------------------
        # Errors: required identity fields (never auto-fixable -- the value
        # is unknowable). Warnings: legal "unknown" fallbacks per the prompts
        # that the owner still wants surfaced for follow-up.
        if not (entry.get("brand") or "").strip():
            issues.append(AuditIssue(
                "Missing Field", idx, eid,
                "Brand is empty -- every entry requires a brand.",
                severity="error", code="brand-missing"))
        if not (entry.get("model") or "").strip():
            issues.append(AuditIssue(
                "Missing Field", idx, eid,
                "Model is empty -- every entry requires a model.",
                severity="error", code="model-missing"))

        if WARN_ZERO_YEAR and coerce_int(entry.get("year", 0)) == 0:
            issues.append(AuditIssue(
                "Missing Data", idx, eid,
                "Year is 0 (unknown) -- set the launch year when verified.",
                severity="warning", code="year-unknown"))
        if WARN_ZERO_PRICE and coerce_int(entry.get("price_usd", 0)) == 0:
            issues.append(AuditIssue(
                "Missing Data", idx, eid,
                "Price is $0 -- set the launch MSRP when verified.",
                severity="warning", code="price-unknown"))
        if WARN_UNVERIFIED_DRIVERS \
                and not (entry.get("driver_type") or "").strip() \
                and not (entry.get("driver_config") or "").strip():
            issues.append(AuditIssue(
                "Missing Data", idx, eid,
                "Driver type and config both empty (unverified) -- fill in "
                "when known.",
                severity="warning", code="drivers-unknown"))
        if WARN_ZERO_SPECS_NON_TWS and ff != TWS_FORM_FACTOR:
            for field, label in (("impedance", "Impedance"),
                                 ("sensitivity", "Sensitivity")):
                if coerce_int(entry.get(field, 0)) == 0:
                    issues.append(AuditIssue(
                        "Missing Data", idx, eid,
                        "{} is 0 on a non-TWS entry (unverified) -- set it "
                        "when known.".format(label),
                        severity="warning",
                        code="impedance-unknown" if field == "impedance"
                        else "sensitivity-unknown"))
        if WARN_TWS_SPECS_NONZERO and ff == TWS_FORM_FACTOR \
                and not ENFORCE_TWS_ZERO_SPECS:
            # AUDIT PROMPT: TWS entries must be impedance 0 / sensitivity 0
            # (no wired out path). Enforcement stays opt-in; this advisory
            # surfaces violations without blocking saves.
            bad = [label for field, label in
                   (("impedance", "Impedance"), ("sensitivity", "Sensitivity"))
                   if coerce_int(entry.get(field, 0), -1) != 0]
            if bad:
                issues.append(AuditIssue(
                    "Missing Data", idx, eid,
                    "{} must be 0 on {} (wireless: no DAC/amp chain).".format(
                        " and ".join(bad), TWS_FORM_FACTOR),
                    severity="warning", code="tws-nonzero"))

        conflicts = tag_conflicts(set(map(str, tags)))
        for pair in conflicts:
            issues.append(AuditIssue(
                "Tag Conflict", idx, eid,
                "Conflicting tags present: {}".format(" + ".join(pair)), severity="error"))

        if len(tags) < MIN_TAGS:
            issues.append(AuditIssue(
                "Tag Count", idx, eid,
                "Only {} tag(s) (minimum {}).".format(len(tags), MIN_TAGS), severity="error"))
        if len(tags) > MAX_TAGS:
            issues.append(AuditIssue(
                "Tag Count", idx, eid,
                "{} tags present (maximum {}).".format(len(tags), MAX_TAGS), severity="error"))

        if len(tags) != len(set(map(str, tags))):
            dup = sorted({str(t) for t in tags if list(map(str, tags)).count(str(t)) > 1})
            issues.append(AuditIssue(
                "Duplicate Tag", idx, eid,
                "Duplicate tag(s): {}".format(", ".join(dup)), severity="error"))

        unapproved = [t for t in tags if t not in APPROVED_TAGS]
        if unapproved:
            issues.append(AuditIssue(
                "Unapproved Tag", idx, eid,
                "Unapproved tag(s): {}".format(_join_tags(unapproved)), severity="error"))

        files = entry.get("files", []) or []
        if len(files) != len(set(map(str, files))):
            issues.append(AuditIssue(
                "Duplicate File", idx, eid,
                "Duplicate file path(s) within entry.", severity="error"))
        for pos, rel in enumerate(files):
            if not isinstance(rel, str) or not rel.strip():
                def _pop_bad_path(en, ppos=pos, old=rel):
                    lst = en.get("files", [])
                    # Guard on the captured VALUE as well as the position:
                    # earlier fixes may have shifted indices, so a bare
                    # pop(ppos) could silently delete a good path.
                    if 0 <= ppos < len(lst) and lst[ppos] == old:
                        lst.pop(ppos)
                    elif old in lst:
                        lst.remove(old)
                issues.append(AuditIssue(
                    "File Path", idx, eid,
                    "File path is empty or not a string: '{}'.".format(rel),
                    fix=make_fix(entry, real_id or eid, idx, _pop_bad_path),
                    severity="error", code="file-empty",
                ))
                continue
            if rel != rel.strip():
                def _strip_path(en, ppos=pos, old=rel):
                    lst = en.get("files", [])
                    if 0 <= ppos < len(lst) and lst[ppos] == old:
                        lst[ppos] = old.strip()
                    else:
                        for k, q in enumerate(lst):
                            if q == old:
                                lst[k] = old.strip()
                                break
                issues.append(AuditIssue(
                    "File Path", idx, eid,
                    "File path has leading/trailing whitespace: '{}'.".format(rel),
                    fix=make_fix(entry, real_id or eid, idx, _strip_path),
                    severity="error", code="file-ws",
                ))
            if "\\" in rel:
                def slash_mut(en, old=rel):
                    lst = en.get("files", [])
                    en["files"] = [q.replace("\\", "/") if q == old else q for q in lst]
                issues.append(AuditIssue(
                    "File Path", idx, eid,
                    "File path uses backslashes (should be forward slashes): '{}'.".format(rel),
                    fix=make_fix(entry, real_id or eid, idx, slash_mut),
                    severity="warning", code="file-backslash",
                ))
            if ".." in rel.split("/"):
                issues.append(AuditIssue(
                    "File Path", idx, eid,
                    "File path contains '..' traversal: '{}'.".format(rel),
                    severity="error", code="file-traversal"))

    # ---- entries without any measurement files (single summary row) ------
    no_files_idx = [i for i, e2 in enumerate(entries)
                    if not (e2.get("files") or [])]
    if no_files_idx:
        preview = ", ".join((entries[i].get("id") or "#{}".format(i))
                            for i in no_files_idx[:5])
        more = " ... (+{} more)".format(len(no_files_idx) - 5) \
            if len(no_files_idx) > 5 else ""
        n_nf = len(no_files_idx)
        issues.append(AuditIssue(
            "No Measurement Files", -1, "(summary)",
            "{} of {} {} no measurement files linked: {}{}.".format(
                n_nf,
                len(entries),
                "entry has" if n_nf == 1 else "entries have",
                preview, more),
            severity="warning"
            if n_nf <= NO_FILES_WARN_THRESHOLD else "info"))

    for rel, idxs in referenced_map.items():
        if len(idxs) > 1:
            ids = [entries[i].get("id", "#{}".format(i)) for i in idxs]
            for idx in idxs:
                eid = entries[idx].get("id", "") or "(no id) #{}".format(idx)
                issues.append(AuditIssue(
                    "Duplicate File Link", idx, eid,
                    "File '{}' is linked to multiple entries: {}".format(rel, ", ".join(ids)),
                    severity="warning",
                    # Scoped per entry so ignoring one row does not hide
                    # the same finding on every other linked entry.
                    subject="{}@{}".format(rel, idx)))

    if on_disk_set is not None:
        for rel, idxs in referenced_map.items():
            if rel not in on_disk_set:
                for idx in idxs:
                    eid = entries[idx].get("id", "") or "(no id) #{}".format(idx)
                    issues.append(AuditIssue(
                        "Missing File", idx, eid,
                        "Linked file not found on disk: {}".format(rel), severity="error"))
        referenced_set = set(referenced_map.keys())
        unlinked = sorted(on_disk_set - referenced_set)
        if len(unlinked) > UNLINKED_ROW_CAP:
            preview = ", ".join(unlinked[:5])
            issues.append(AuditIssue(
                "Unlinked File", -1, "(none)",
                "{} file(s) on disk are not linked to any entry "
                "(first 5: {}). Link the files or move them out of the data "
                "folder.".format(len(unlinked), preview),
                severity="info"))
        else:
            for rel in unlinked:
                issues.append(AuditIssue(
                    "Unlinked File", -1, "(none)",
                    "File on disk is not linked to any entry: {}".format(rel),
                    severity="info", subject=rel))

    # ---- fuzzy duplicate scan (soft; pairs are user-judged) --------------
    issues.extend(find_duplicate_pairs(entries, referenced_map))

    return issues
