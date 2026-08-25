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
import zlib
import shutil
import datetime
import unicodedata

CURRENT_YEAR = datetime.datetime.now().year

# Upper bound for decompressed .json.gz payloads (the 100 MB raw-file cap
# cannot see inside a compressed archive).
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024

# ==========================================================================
# BEHAVIOR SWITCHES (defaults match ADD ENTRY PROMPT.txt / AUDIT PROMPT.txt)
# ==========================================================================
ENFORCE_TWS_ZERO_SPECS = False       # prompts do NOT require TWS specs = 0
CANONICALIZE_DRIVER_ORDER = False    # opt-in: DRIVER_TECH_ORDER now matches
                                     # the prompt's canonical sequence, so
                                     # newly generated configs are already
                                     # canonical; this flag only rewrites
                                     # legacy data during audits

# sanity caps (defensive; prompts say whole integers, 0 if unverifiable)
IMPEDANCE_MAX = 200000     # ohms (electrostatics reach ~145k)
SENSITIVITY_MAX = 200      # dB/mW
PRICE_MAX = 10000000       # USD

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "DatabaseEditor")


def log(msg):
    """Best-effort diagnostic log visible even in --windowed builds."""
    try:
        print(msg)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "editor.log"), "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


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


def build_id(brand, model, variant):
    comps = [normalize_component(brand), normalize_component(model)]
    if variant and variant.strip():
        comps.append(normalize_component(variant))
    idstr = "_".join(c for c in comps if c)
    idstr = re.sub(r"_+", "_", idstr).strip("_")
    return idstr


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


def parse_driver_config(config_str):
    result = {}
    if not config_str:
        return result
    parts = str(config_str).replace(" ", "").split("+")
    for part in parts:
        if not part:
            continue
        m = re.match(r"^(\d+)([A-Za-z]+)$", part)
        if not m:
            continue
        count, tech = m.groups()
        if tech in DRIVER_TECH_ORDER:
            c = coerce_int(count, 0)
            if c > 0:
                result[tech] = c
    return result


def driver_config_unknown_tokens(config_str):
    """Tokens present in the string that carry no known tech/count meaning."""
    out = []
    for part in str(config_str).replace(" ", "").split("+"):
        if not part:
            continue
        m = re.match(r"^(\d+)([A-Za-z]+)$", part)
        if not m or m.group(2) not in DRIVER_TECH_ORDER:
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
        if isinstance(price_raw, str) and "." in price_raw:
            errors.append("Price must be a whole number (got '{}').".format(price_raw))
            price = coerce_int(price_raw, -1)
        elif isinstance(price_raw, str):
            try:
                price = int(price_raw.strip().replace("_", ""))
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
        expected_tier = price_tier_for(entry.get("price_usd", 0))
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
                elif re.fullmatch(r"[+-]?\d+", sval.replace("_", "")) is None \
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
                    val = coerce_int(sval.replace("_", ""), 0)
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
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return entries


# --------------------------------------------------------------------------
# AUTOSAVE BACKUPS
# --------------------------------------------------------------------------
AUTOSAVE_DIR_NAME = ".db_editor_backups"
AUTOSAVE_PREFIX = "autosave_"
AUTOSAVE_KEEP = 15
AUTOSAVE_SEEN_MARKER = ".autosave_seen"


def backup_dir_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), AUTOSAVE_DIR_NAME)


OVERWRITE_SNAPSHOT_PREFIX = "pre_overwrite_"
OVERWRITE_SNAPSHOT_KEEP = 10


def write_pre_overwrite_snapshot(db_path, keep=OVERWRITE_SNAPSHOT_KEEP):
    """Copy the original database into the backup folder BEFORE the user
    deliberately overwrites it via Save As. Returns the snapshot path, or
    None when the copy fails (overwriting is then still allowed -- autosave
    history already provides a second net). Keeps the newest `keep`
    snapshots so the folder cannot grow without bound."""
    bdir = backup_dir_for(db_path)
    try:
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(bdir, "{}{}_{}".format(
            OVERWRITE_SNAPSHOT_PREFIX, stamp, os.path.basename(db_path)))
        shutil.copy2(db_path, dest)
    except OSError as e:
        log("Pre-overwrite snapshot failed: {}".format(e))
        return None
    snaps = []
    try:
        snaps = [os.path.join(bdir, fn) for fn in os.listdir(bdir)
                 if fn.startswith(OVERWRITE_SNAPSHOT_PREFIX)]
    except OSError:
        pass
    snaps.sort(key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0,
                              p), reverse=True)
    for stale in snaps[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass
    return dest


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
# AUDIT WAIVERS (user-ignored findings, persisted beside the database)
# --------------------------------------------------------------------------
WAIVER_FILENAME = "audit_waivers.json"


def waivers_path_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or "")),
                        WAIVER_FILENAME)


def load_waivers(db_path):
    """Set of waiver keys persisted next to the database. Corrupt or
    missing files simply mean 'no waivers'."""
    path = waivers_path_for(db_path)
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
    """Persist waiver keys atomically beside the database."""
    path = waivers_path_for(db_path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    payload = {"version": 1,
               "waived": sorted(str(w) for w in waivers if w)}
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


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
            if norm:
                referenced_map.setdefault(norm, []).append(idx)
    if data_root:
        data_dir = os.path.join(data_root, "data")
        if os.path.isdir(data_dir):
            on_disk = []
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
            on_disk_set = set(on_disk)
            disk_index = {p.lower(): p for p in on_disk}

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
            elif fv > (IMPEDANCE_MAX if field == "impedance" else SENSITIVITY_MAX):
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
        try:
            p_check = float(price)
            tier_basis = p_check if (math.isfinite(p_check) and float(p_check).is_integer()
                                     and int(p_check) % 5 == 0) \
                else round_price_to_5(p_check)
            if p_check < 0 or not math.isfinite(p_check):
                tier_basis = price
        except (TypeError, ValueError):
            tier_basis = price
        expected_tier = price_tier_for(tier_basis)
        tags = entry.get("tags", []) or []
        present_tiers = [t for t in tags if t in PRICE_TIER_TAGS]

        def tier_mut(en, expected=expected_tier):
            cur_tags = [t for t in en.get("tags", []) if t not in PRICE_TIER_TAGS]
            cur_tags.append(expected)
            en["tags"] = cur_tags

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

    return issues
