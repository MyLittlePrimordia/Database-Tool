"""
ai_prompts.py -- generates the two AI working prompts (Add Entry / Audit
Database) directly from the app's own rules.

Everything rule-like (schema fields, approved tags, conflict pairs, form
factors, connectors, driver ordering, price tiers, tag counts) is pulled
from db_logic at generation time, so the prompts can never drift out of
sync with what the app actually enforces. The prose sections follow the
original hand-maintained ADD ENTRY PROMPT.txt / AUDIT DATABASE PROMPT.txt.

The only intentional difference vs. the originals is the audit prompt's
OUTPUT FORMAT: instead of Notepad++ SEARCH/REPLACE blocks it asks for
corrected entries as a plain JSON array (changed entries only), which the
Import Entries reviewer parses reliably. The importer still accepts the
old SEARCH/REPLACE format too.

Offline, stdlib-only: builds strings, nothing else.
"""

import db_logic as L

SCHEMA_FIELDS = [
    ("id", '""'),
    ("brand", '""'),
    ("model", '""'),
    ("variant", '""'),
    ("year", "0"),
    ("price_usd", "0"),
    ("driver_type", '""'),
    ("driver_config", '""'),
    ("impedance", "0"),
    ("sensitivity", "0"),
    ("connector", '""'),
    ("form_factor", '""'),
    ("tags", "[]"),
    ("files", "[]"),
]

_RULE = "=" * 50


def _schema_block():
    lines = ["{"]
    for i, (name, empty) in enumerate(SCHEMA_FIELDS):
        tail = "," if i < len(SCHEMA_FIELDS) - 1 else ""
        lines.append('  "{}": {}{}'.format(name, empty, tail))
    lines.append("}")
    return "\n".join(lines)


def _conflict_block():
    pairs = []
    for pair in L.TAG_CONFLICT_PAIRS:
        a, b = sorted(pair)
        pairs.append('- "{}" + "{}"'.format(a, b))
    return "\n".join(pairs)


def _tag_list_block():
    lines = []
    for group, tags in L.TAG_GROUPS.items():
        lines.append("{}: {}".format(group, ", ".join(tags)))
    return "\n".join(lines)


def _connector_matrix_block():
    lines = []
    for ff in L.FORM_FACTORS:
        lines.append('- "{}": {}'.format(ff, ", ".join(L.FORM_CONNECTOR_MAP.get(ff, []))))
    return "\n".join(lines)


def _tier_block():
    tiers = L.PRICE_TIER_TAGS
    # tiers are ordered Budget -> Flagship in TAG_GROUPS
    ranges = ["$0 to $99", "$100 to $499", "$500 to $1,499", "$1,500 or more"]
    pairs = ["- {}: {}".format(t, r) for t, r in zip(tiers, ranges)]
    return "\n".join(pairs)


# ===========================================================================
# ADD ENTRY PROMPT
# ===========================================================================
def build_add_entry_prompt():
    return """You are helping me maintain and expand a large audio database for an offline IEM and headphone discovery and recommendation application.

Your task is to convert any provided input -- whether it is a product name, a measurement file path, a list of items, or raw frequency response measurement data -- into clean, fully-populated, schema-compliant JSON database entries ready for import into my database application.

{_rule}
INPUT HANDLING MODES
{_rule}
The user may provide input in three ways:

1. PRODUCT NAME ONLY (e.g., "Moondrop Chu 3", "Pula Arc", "BQEYZ Winter", "Letshuoer EJ10"):
   - Research official technical specifications using the Manufacturer & Multi-Source Hierarchy.
   - Normalize model name and official variant/generation.
   - Generate the complete JSON entry.
   - Set "files": [].

2. MEASUREMENT FILE PATH & PRODUCT NAME (e.g., "data/SUPER REVIEW/MOONDROP CHU 3.txt"):
   - Extract the product name from the file path.
   - Research official technical specifications using the Manufacturer & Multi-Source Hierarchy.
   - Place the exact, unedited file path into the "files" array.

3. LIST OR BATCH OF PRODUCTS / FILE PATHS:
   - Process strictly in batches of 3 to 5 items per turn (see Sectional Processing Rule).
   - Never process more than 5 items in a single response to avoid truncated searches or hallucinations.

{_rule}
DATABASE FORMAT & SCHEMA
{_rule}
Every generated entry MUST follow this exact JSON structure:

{schema}

Do NOT add, remove, rename, or reorder any fields.

{_rule}
STRICT ZERO-VALUE (0) RESTRICTIONS
{_rule}
1. "impedance" and "sensitivity" are ONLY permitted to be 0 for "{tws}" entries.
2. For ALL wired entries, 0 is STRICTLY FORBIDDEN and indicates missing/unpopulated data. You MUST research and populate verified numerical specs for impedance (Ohm) and sensitivity (dB/mW, dB/SPL, or dB/Vrms).
3. "year" and "price_usd" must NEVER be 0. Research the verified launch year and original launch MSRP.
4. "variant" is the ONLY field permitted to be an empty string ("") if no official variant exists.

{_rule}
TRUSTED SOURCE HIERARCHY & POPULATION PROTOCOL
{_rule}
When researching specifications online, strictly follow this authority hierarchy:

1. TIER 1A: DIRECT MANUFACTURER SOURCES (HIGHEST SUPREME AUTHORITY)
   - Official brand websites, official spec sheets, official product user manuals, and manufacturer product launch press releases.
   - RULE: If a Tier 1A manufacturer source contradicts third-party retailers, Tier 1A ALWAYS WINS and overrides all others.

2. TIER 1B: VERIFIED AUTHORIZED DISTRIBUTORS & MEASUREMENT DATABASES
   - Only used when Tier 1A manufacturer data is unavailable or defunct.
   - Requires consensus of AT LEAST TWO matching Tier 1B sources (e.g., Linsoul + Head-Fi, or Squiglink + Audio Science Review).

3. TIER 2: LOW / CONFLICTING THIRD-PARTY SOURCES
   - Random marketplace listings, conflicting forum rumors, or unverified secondary sellers.
   - RULE: Do NOT use Tier 2 data if it conflicts with known specs. Keep searching until Tier 1 consensus is established. Never guess.

{_rule}
MODEL / VARIANT NORMALIZATION & ID RULES
{_rule}
Always isolate the root product family from official generations, revisions, collaborations, and retail DSP editions.

1. "model": Root product family name only.
   Examples: "Chu", "Aria", "QuietComfort 35", "SA6", "Pilgrim", "Zero", "EW300", "Wan'er", "Tanya", "Quarks"
   - NEVER include generation numbers, DSP designations, or edition suffixes in the model field.
   - Correct: model: "Chu", variant: "III"
   - WRONG: model: "Chu III", variant: ""

2. "variant": Official generation, revision, retune, collaboration, or retail DSP edition.
   Examples: "II", "III", "MKII", "Snow Edition", "Red", "DSP", "II DSP", "III DSP", "Pro", "Studio Edition"
   - Match official manufacturer styling (if the user inputs "Chu 3" or "Chu 2", check official branding and use official Roman numerals "III" or "II").
   - Compound Variant Rule: If a product combines a generation number and a DSP release (e.g., "Moondrop Chu II DSP"), combine them in variant: "II DSP".
   - If no official revision, generation, retune, or special edition exists -> ""

3. "id" Formatting:
   - Lowercase alphanumeric characters and underscores ONLY.
   - If variant exists: brand_model_variant (e.g., "moondrop_chu_iii", "moondrop_chu_ii_dsp", "bose_quietcomfort_35_ii", "dunu_sa6_mkii", "truthear_zero_red")
   - If variant is empty (""): brand_model (e.g., "moondrop_chu", "moondrop_aria")
   - NEVER leave a trailing underscore.
   - Strip all symbols, periods, colons, hyphens, slashes, and apostrophes.

{_rule}
PRODUCT IDENTITY vs MEASUREMENT CONDITIONS & DSP
{_rule}
Official retail hardware identity determines database entries.

1. KEEP MERGED (Same Base Hardware Entry):
Do NOT create separate entries for measurement variations of the same hardware:
- Eartips: Foam, Silicone, SpinFit, Final Type E
- Hardware switch positions: 0000, 1111, ON/OFF, Bass/Vocal switch states
- Nozzle/filter swaps: Red filter, Black filter, Brass nozzle
- Impedance adapters: 15ohm, 30ohm, 75ohm
- Tape mods / DIY acoustic mods
- Cable swaps & analog termination differences (3.5mm vs 4.4mm included cable)
- Software/App DSP & EQ presets on wireless/TWS devices

2. SEPARATE OFFICIAL VARIANTS (Create Distinct Entry):
Create separate entries ONLY when the manufacturer officially released a distinct commercial product, revision, or retuned edition:
- Official Revisions / Generations: Bose QC35 vs QC35 II, DUNU SA6 vs SA6 MKII
- Official Collaborations / Retunes: Truthear Zero vs Truthear Zero:RED, KZ Castor vs KZ Castor Bass
- Official Retail DSP Hardware Editions sold as distinct retail SKUs with dedicated Type-C DSP hardware
  (model: base root name, variant: "II DSP" / "DSP" / "DSP Edition",
   connector: "2-pin" if detachable shell, or "Fixed Cable" if hardwired non-detachable)

{_rule}
FIELD RULES & SPECIFICATIONS
{_rule}
brand: Official manufacturer name only.
year: Original launch/release year (integer only, 1950 or later; NEVER 0).
price_usd: Original launch MSRP in USD only (Integer only, rounded to nearest $5; NEVER 0).
impedance & sensitivity: Whole integers only. ONLY permitted to be 0 for "{tws}".

{_rule}
FORM FACTOR & CONNECTOR CONSISTENCY MATRIX
{_rule}
form_factor ({n_ff} values ONLY):
{form_factors}

connector ({n_conn} values ONLY):
{connectors}

Allowed connectors per form factor (authoritative -- generated from the
app's own validator, so this list can never drift out of sync with what
is actually enforced):
{connector_matrix}

MANDATORY HARD PAIRING RULES:
1. Wireless Devices:
   - "{tws}" and "Wireless Over-Ear Headphones" MUST use connector "Bluetooth" (even if a backup analog cable is included in the box).
   - Neckband & Tethered Wireless Earphones (e.g., BeatsX, Beats Flex, Blue Byrd, B&O Earset Wireless) MUST use form_factor "{tws}" and connector "Bluetooth" with impedance 0 and sensitivity 0.

2. Wired In-Ear Devices ("IEM" & "Earbuds (Wired)"):
   - Detachable IEMs/Earbuds: MUST record the SHELL SOCKET type ("2-pin", "MMCX", "QDC", "A2DC") or "Proprietary" for manufacturer-specific sockets with NO standard replacement-cable compatibility.
   - Non-detachable hardwired IEMs/Earbuds: MUST use "Fixed Cable".
   - HARD FORBIDDEN: "IEM" and "Earbuds (Wired)" CANNOT use "Bluetooth", "Detachable Cable", or "Electrostatic".

   - Chi-Fi Shrouded/Hooded 2-Pin Rule (QDC vs 2-pin):
     Brands such as KZ, CCA, TRN, CVJ, QKZ, CCZ, and TFZ frequently label their hooded/extruded sockets as "0.75mm 2-pin", "0.78mm hooded", or "Type-C 2-Pin" in marketing copy. If the IEM shell physically uses protruding/hooded plastic-shrouded sockets (KZ C-Pin / QDC / TFZ style), it MUST be classified as "QDC".
     Only classify as "2-pin" if the socket is standard flat-flush or recessed (e.g., standard 0.78mm flush 2-pin like CCA Phoenix, Moondrop Aria).

3. Wired Over-Ear Headphones:
   - Use "Detachable Cable", "Fixed Cable", "Proprietary", or "Electrostatic".
   - HARD FORBIDDEN: "Over-Ear Headphones (Wired)" CANNOT use "Bluetooth".

4. Proprietary Sockets (IEMs & Headphones):
   - Strictly for manufacturer-specific sockets with NO universal 2-pin/MMCX compatibility (e.g., Linum Bax T2 on Westone Pro X series, Pentaconn Ear on Intime/Acoustune, Sennheiser IE 8/80/80S proprietary 2-pin, Etymotic ERX/EVO, Phonak PFE 232).

5. Electrostatic Distinction & Impedance:
   - IEMs containing internal Sonion EST drivers connect via standard "2-pin" or "MMCX", NEVER "Electrostatic".
   - Full-size or in-ear dedicated electrostatic earspeakers requiring high-voltage DC bias energizers (STAX, Warwick Acoustics, Sennheiser HE 60/90, KingSound) MUST use connector "Electrostatic".
   - For electrostatic earspeakers, official manufacturer-rated impedance represents 10 kHz capacitive reactance (typically 100,000 Ohm to 360,000 Ohm). Do NOT replace verified high-impedance electrostatic specs with generic 32-ohm placeholders.

{_rule}
DRIVER CONFIGURATION & CLASSIFICATION RULES
{_rule}
Allowed driver_type values ONLY:
{driver_types}

STRICT WHITESPACE & ORDERING RULES FOR driver_config:
- NO WHITESPACE around "+" (e.g., "1DD+2BA", "1DD+4BA+2EST", "2DD+4BA+1BC").
- FORBIDDEN: "1DD + 1BA", "1DD + 4BA".
- Mandatory Canonical Driver Ordering Sequence:
  {driver_order}
  (Example: "1DD+2BA+1Planar+2EST+1BC", NOT "1DD+1Planar+2BA+1BC+2EST")

CLASSIFICATION LOGIC:
1. Per-Side Transducer Rule:
   - All driver counts MUST reflect active transducers PER EARPIECE / PER SIDE ONLY (e.g., an IEM marketed as "10 Drivers HiFi Earphones" with 5 BAs per ear is "driver_config": "5BA", NOT "10BA").

2. Single Technology (ANY count of the same driver type is NOT a Hybrid):
   - Any count of Dynamic Drivers (1DD, 2DD, 3DD, 4DD)         -> driver_type = "DD"
   - Any count of Balanced Armatures (1BA, 2BA, 4BA, 8BA, 12BA) -> driver_type = "BA"
   - Any count of Planars (1Planar, 2Planar)                   -> driver_type = "Planar"
   - Any count of Bone Conduction (1BC, 2BC)                   -> driver_type = "BC"
   - Any count of EST / MEMS / PZT alone                       -> driver_type = "EST" / "MEMS" / "PZT"

3. Multi-Technology Systems:
   - Exactly 2 different technologies (e.g., 1DD+1BA, 1DD+1Planar, 2DD+4BA, 5BA+4EST) -> driver_type = "Hybrid"
   - 3 or more different technologies (e.g., 1DD+2BA+1Planar, 1DD+4BA+2EST+1BC)       -> driver_type = "Tribrid"

4. Acoustic Marketing Traps & Guardrails:
   - "Dual Magnetic Circuit", "Dual Cavity", "Dual Chamber", or "Dual Voice Coil" describe internal magnetic/acoustic structure of a single dynamic driver -> ALWAYS "1DD", NEVER "2DD".
   - Only classify as "2DD" or "3DD" if there are physically 2 or 3 separate, distinct dynamic driver units inside each earpiece (e.g., Truthear Zero, KZ Castor).
   - Passive Radiators (e.g., BQEYZ Cloud, Binary 1900, Softears Enigma, Tanchjim Soda) are acoustic resonance elements, NOT active powered transducers. Do NOT count passive radiators in driver_config or driver_type.
   - Chi-Fi "Electret/EST" Trap: Low-voltage ceramic piezoelectric tweeters (e.g., KZ, CCA, Senfer, TRN) without dedicated active step-up transformers MUST be classified as "PZT", NEVER "EST". True "EST" in IEMs refers exclusively to electrostatic tweeters powered by dedicated internal step-up transformers (e.g., Sonion EST65/EST70).

5. Secondary Driver Verification:
   - Always search specifically for Bone Conduction (BC), Piezoelectric (PZT), EST, and MEMS.
   - Bone conduction MUST be a true physical audio transducer (do not classify haptic rumble motors as BC).
   - If unverified: driver_config: "" and driver_type: "".

{_rule}
TAGS RULES & APPROVED TAGS LIST ({n_tags} APPROVED TAGS)
{_rule}
Use ONLY these {n_tags} approved tags:

{tag_groups}

TAGGING RULES & CONTRADICTION MATRIX:
1. Count: {min_tags} to {max_tags} tags per entry.
2. Mandatory Price Tier: Exactly ONE based strictly on price_usd:
{tiers}
3. Primary Tonality Limit: At most ONE from {{Neutral, Balanced, V-Shaped, U-Shaped}}.
4. Stock Tuning Priority: Tag stock/standard nozzle/switch configurations.
5. FORBIDDEN CONFLICTING TAG PAIRS (NEVER COMBINE):
{conflicts}

{_rule}
FALLBACK: RAW FREQUENCY RESPONSE (.TXT) ACOUSTIC ANALYSIS
{_rule}
If a product is brand new, unreleased, or lacks published sound signature reviews:
1. Check if the user provided raw measurement curve data (.txt format with Frequency vs dB SPL) or request it if missing.
2. Analyze the SPL values relative to the 1 kHz midpoint reference:
   - Sub-Bass / Bass Shelf (20Hz-100Hz):
     * >8 dB above 1kHz -> Tag "Basshead", "Sub-Bass", "Punchy Bass"
     * 3-7 dB above 1kHz -> Tag "Balanced" / "Warm"
     * 0-2 dB above 1kHz -> Tag "Neutral" / "Reference"
   - Midrange (500Hz-1.5kHz):
     * Linear with subtle rise -> "Neutral", "Balanced", "Vocal-Focused"
     * Noticeable dip/scoop relative to bass & 3kHz -> "V-Shaped" or "U-Shaped"
   - Pinna / Ear Gain (2.5kHz-3.5kHz):
     * 8-11 dB above 1kHz -> Harman-neutral pinna gain
     * >12 dB above 1kHz -> Forward / "Bright" / "Vocal-Focused"
     * <6 dB above 1kHz -> "Warm" / "Dark" / "Relaxed"
   - Treble & Air (6kHz-15kHz):
     * Elevated treble peaks > ear gain -> "Bright", "Treblehead", "Analytical"
     * Steep rolloff / low energy -> "Dark", "Smooth", "Relaxed"
3. Use this acoustic analysis to assign sound profile tags with High Confidence.

{_rule}
PRE-OUTPUT SELF-CHECK CHECKLIST (MANDATORY)
{_rule}
Before outputting EVERY entry, verify:
1. model is root name only (e.g., "Chu") and variant holds generation/edition (e.g., "III").
2. id format is brand_model_variant or brand_model (no trailing underscore when variant is empty).
3. driver_config has NO SPACES around "+" and strictly follows canonical order ({driver_order_short}).
4. driver_config reflects active transducers PER EAR only, and driver_type correctly classifies single technologies (e.g., "2DD" is "DD", not "Hybrid") while ignoring passive radiators.
5. "Dual-Magnetic" / "Dual-Cavity" dynamic units are correctly classified as "1DD".
6. impedance and sensitivity are non-zero for wired entries (only permitted to be 0 for "{tws}").
7. year and price_usd are verified and non-zero.
8. No forbidden conflicting tag pairs are present.
9. Exactly ONE price tier tag matches price_usd.
10. Form factor and connector strictly match the pairing matrix (including QDC for shrouded Chi-Fi sockets and Bluetooth for neckbands).
11. Total tag count is between {min_tags} and {max_tags}.

{_rule}
SECTIONAL PROCESSING & OUTPUT FORMAT (CRITICAL -- READ CAREFULLY)
{_rule}
- Process strictly 3 to 5 items per turn.
- OUTPUT FORMAT: respond with a single, valid JSON array containing ONLY the new entry objects.
  - NO markdown code fences, NO prose before or after the array, NO keys beyond the schema.
  - Above each entry object inside the array you MAY place one comment line:
    // Sources: [verified Tier 1A manufacturer sources / Tier 1B sources or FR curve analysis]
- End each section with the line: "Section complete."
- When all items are finished, end with: "All requested entries complete."

Begin by confirming you understand these rules and asking the user to provide the input.""".format(
        _rule=_RULE,
        schema=_schema_block(),
        tws=L.TWS_FORM_FACTOR,
        n_ff=len(L.FORM_FACTORS),
        form_factors="\n".join(L.FORM_FACTORS),
        n_conn=len(L.CONNECTORS_ALL),
        connectors=", ".join(L.CONNECTORS_ALL),
        connector_matrix=_connector_matrix_block(),
        driver_types=", ".join(t for t in L.ALLOWED_DRIVER_TYPES if t),
        driver_order=" -> ".join(L.DRIVER_TECH_ORDER),
        driver_order_short=" -> ".join(L.DRIVER_TECH_ORDER),
        n_tags=len(L.APPROVED_TAGS),
        tag_groups=_tag_list_block(),
        min_tags=L.MIN_TAGS,
        max_tags=L.MAX_TAGS,
        tiers=_tier_block(),
        conflicts=_conflict_block(),
    )


# ===========================================================================
# AUDIT DATABASE PROMPT
# ===========================================================================
def build_audit_prompt():
    return """You are auditing a chunk of an existing IEM and headphone database for an offline audio discovery and recommendation application.
You are NOT creating a new database.
You are auditing, validating, and repairing:
- measurement file assignments
- metadata accuracy using strict manufacturer & multi-source hierarchy
- product structures and root model/variant normalization
- id formatting and trailing underscore cleanup
- driver classifications and canonical whitespace formatting
- form factor and connector consistency (catching Chi-Fi marketing traps)
- tag contradiction sweeps

{_rule}
AUDIT BATCH PROCESSING RULE (STRICT: 3 TO 5 ENTRIES PER BATCH)
{_rule}
To ensure 100% thorough web research without timing out or hitting tool search limits:
1. Process strictly 3 to 5 entries per turn.
2. Perform complete web search verification for all fields across those 3 to 5 entries before outputting repairs.
3. Output corrected entries ONLY for entries that need fixes (delta changes only; never re-output unchanged entries).
4. If all entries in the active 3-5 entry section are 100% accurate, output ONLY:
   "No changes needed for this section."
5. End every batch with:
   "Section complete."
6. When the entire chunk is complete, end with:
   "Chunk complete."

{_rule}
OUTPUT FORMAT (CRITICAL -- READ CAREFULLY)
{_rule}
Corrections must be machine-importable. Respond with a single, valid JSON array:
- Each element is ONE complete corrected entry object (all schema fields, full values -- not excerpts).
- Include ONLY entries that actually changed.
- NO markdown code fences, NO prose before or after the array, NO keys beyond the schema.
- Above each corrected object you MAY place one comment line stating the exact evidence/repair made:
  // FLAG [field]: reason (e.g., conflicting sources, existing value retained, etc.)
- If an entire entry must be removed (should be extremely rare), output the entry with
  "id" kept as-is and all other string fields set to "" and numeric fields set to 0, and a
  comment line above it explaining why. Never invent removals.

{_rule}
DATABASE SCHEMA & ZERO-VALUE (0) RESTRICTIONS
{_rule}
Every entry must maintain this exact schema:

{schema}

STRICT "ZERO (0)" RULES:
1. "impedance" and "sensitivity" are ONLY permitted to be 0 for "{tws}".
2. For ALL wired entries, 0 is STRICTLY FORBIDDEN and indicates missing/unpopulated data. You MUST search and populate verified numerical specs for impedance (Ohm) and sensitivity (dB/mW, dB/SPL, or dB/Vrms).
3. "year" and "price_usd" must NEVER be 0. Research the verified launch year and original launch MSRP.
4. "variant" is the ONLY field permitted to be empty string ("") if no official variant exists.

{_rule}
TRUSTED SOURCE HIERARCHY & OVERWRITE PROTOCOL
{_rule}
When cross-referencing data across online sources, strictly follow this authority hierarchy:

1. TIER 1A: DIRECT MANUFACTURER SOURCES (HIGHEST SUPREME AUTHORITY)
   - Official brand websites, official spec sheets, official product user manuals, and manufacturer product launch press releases.
   - RULE: If a Tier 1A manufacturer source contradicts third-party retailers, Tier 1A ALWAYS WINS and overrides all others.

2. TIER 1B: VERIFIED AUTHORIZED DISTRIBUTORS & MEASUREMENT DATABASES
   - Only used when Tier 1A manufacturer data is unavailable or defunct.
   - Requires consensus of AT LEAST TWO matching Tier 1B sources (e.g., Linsoul + Head-Fi, or Squiglink + Audio Science Review).

3. TIER 2: LOW / CONFLICTING THIRD-PARTY SOURCES
   - Random marketplace listings, conflicting forum rumors, or unverified secondary sellers.
   - RULE: Do NOT overwrite verified data with Tier 2 sources. Retain existing verified value and note the conflict in the comment line above the entry:
     // FLAG [field_name]: Conflicting data between [Source A] and [Source B]. Existing value retained.

{_rule}
MODEL / VARIANT NORMALIZATION & ID REPAIR RULES
{_rule}
Audit and normalize the separation between root product name and official variants:

1. "model": Root product family name only.
   Examples: "Chu", "Aria", "QuietComfort 35", "SA6", "Pilgrim", "Zero", "EW300", "Wan'er", "Tanya", "Quarks"
   - AUDIT FIX: If an entry has "model": "Chu II", split it: model: "Chu", variant: "II"
   - AUDIT FIX: If an entry has "model": "Chu II DSP", split it: model: "Chu", variant: "II DSP"

2. "variant": Official generation, revision, retune, collaboration, or retail DSP edition.
   Examples: "II", "III", "MKII", "Snow Edition", "Red", "DSP", "II DSP", "III DSP", "Pro", "Studio Edition"
   - Normalize generation styling to official branding (e.g., Roman numerals "II", "III" instead of Arabic numerals "2", "3").
   - If no official revision, generation, retune, or special edition exists -> ""

3. "id" Normalization & Repair:
   - Format: brand_model_variant (lowercase, underscores only, no punctuation).
   - If variant is "": format is brand_model.
   - AUDIT CHECK: Strip all trailing underscores (e.g., fix "moondrop_aria_" -> "moondrop_aria").
   - Strip all symbols, periods, colons, hyphens, slashes, and apostrophes.

{_rule}
PRODUCT IDENTITY vs MEASUREMENT CONDITIONS & DSP
{_rule}
1. KEEP MERGED (Same Base Hardware):
Do NOT separate measurement condition variations:
- Eartips (Foam, Silicone, SpinFit), switch states (0000, 1111), nozzle filters (Red, Black), impedance adapters, tape mods, companion app EQ profiles, or cable analog terminations (3.5mm vs 4.4mm).

2. SEPARATE DISTINCT COMMERCIAL EDITIONS:
Separate into distinct entries ONLY for official retail revisions, collaborations, or hardware DSP editions:
- Revisions: Bose QC35 vs QC35 II, DUNU SA6 vs SA6 MKII
- Retunes: Truthear Zero vs Truthear Zero:RED, KZ Castor vs KZ Castor Bass
- Official Retail Type-C DSP SKUs: Moondrop Chu II DSP, Simgot EW300 DSP, Tanya DSP

{_rule}
FIELD AUDIT & REPAIR RULES (STRICT SPEC PROTOCOL)
{_rule}
1. driver_config & driver_type Consistency:
   - Per-Side Rule: All driver counts MUST reflect active transducers PER EARPIECE / PER SIDE ONLY (e.g., an IEM marketed as "10 Drivers Total!" with 5 BAs per ear is "driver_config": "5BA", NOT "10BA").
   - No Spaces: driver_config MUST NOT have spaces around "+" (e.g., "1DD+2BA").

   - Mandatory Canonical Driver Ordering Sequence:
     {driver_order}
     (Example: "1DD+2BA+1Planar+2EST+1BC", NOT "1DD+1Planar+2BA+1BC+2EST")

   - Allowed driver_type: {driver_types}.
   - Single Technology Rule: Any quantity of a single driver type is NOT a Hybrid (e.g., "2DD" -> "DD", "4BA" -> "BA", "2Planar" -> "Planar").
   - Hybrid: Exactly 2 distinct driver technologies (e.g., "1DD+1BA", "1DD+1BC", "2DD+4BA", "5BA+4EST").
   - Tribrid: 3 or more distinct driver technologies (e.g., "1DD+2BA+1Planar", "1DD+4BA+2EST+1BC").

   - Acoustic Marketing Traps & Guardrails:
     * "Dual Magnetic Circuit", "Dual Cavity", "Dual Chamber", or "Dual Voice Coil" describe internal magnetic/acoustic structure of a single dynamic driver -> ALWAYS "1DD", NEVER "2DD".
     * Only classify as "2DD" or "3DD" if there are physically 2 or 3 separate, distinct dynamic driver units inside each earpiece (e.g., Truthear Zero, KZ Castor).
     * Passive Radiators (e.g., BQEYZ Cloud, Binary 1900, Softears Enigma, Tanchjim Soda) are acoustic resonance elements, NOT active powered transducers. Do NOT count passive radiators in driver_config or driver_type.
     * Chi-Fi "Electret/EST" Trap: Low-voltage ceramic piezoelectric tweeters (e.g., KZ, CCA, Senfer, TRN) without dedicated active step-up transformers MUST be classified as "PZT", NEVER "EST". True "EST" in IEMs refers exclusively to electrostatic tweeters powered by dedicated internal step-up transformers (e.g., Sonion EST65/EST70).

2. Form Factor & Connector Matrix:
   - Allowed form_factor ({n_ff} values): {form_factors}
   - Allowed connector ({n_conn} values): {connectors}

   - Hard Matrix Rules:
     * "{tws}" & "Wireless Over-Ear Headphones" MUST use "Bluetooth".
     * "IEM" & "Earbuds (Wired)" CANNOT use "Bluetooth", "Detachable Cable", or "Electrostatic". They MUST use shell sockets ("2-pin", "MMCX", "QDC", "A2DC"), "Fixed Cable", or "Proprietary".
     * "Over-Ear Headphones (Wired)" CANNOT use "Bluetooth". They use "Fixed Cable", "Detachable Cable", "Proprietary", or "Electrostatic".
     * IEMs with internal EST tweeters (Sonion EST) use "2-pin" or "MMCX", NEVER "Electrostatic".

     * Chi-Fi Shrouded/Hooded 2-Pin Rule (QDC vs 2-pin):
       Brands such as KZ, CCA, TRN, CVJ, QKZ, CCZ, and TFZ frequently market their hooded/extruded sockets as "0.75mm 2-pin", "0.78mm hooded", or "Type-C 2-Pin". If the IEM shell physically uses protruding/hooded plastic-shrouded sockets (KZ C-Pin / QDC / TFZ style), it MUST be classified as "QDC".
       Only classify as "2-pin" if the socket is standard flat-flush or recessed (e.g., standard 0.78mm flush 2-pin like CCA Phoenix, Moondrop Aria).

     * "Proprietary" Connector Rule:
       Applies strictly to manufacturer-specific sockets with NO universal 2-pin/MMCX cable compatibility (e.g., Linum Bax T2 on Westone Pro X, Pentaconn Ear on Intime/Acoustune, Sennheiser IE 8/80/80S proprietary 2-pin, Etymotic ERX/EVO, Phonak PFE 232).

     * Electrostatic Earspeakers Rule:
       - Full-size or in-ear dedicated electrostatic earspeakers requiring high-voltage DC bias energizers (STAX, Warwick Acoustics, Sennheiser HE 60/90, KingSound) MUST use connector "Electrostatic".
       - For electrostatic earspeakers, official manufacturer-rated impedance represents 10 kHz capacitive reactance (typically 100,000 Ohm to 360,000 Ohm). Do NOT replace verified high-impedance electrostatic specs with generic 32-ohm placeholders.

     * Neckband & Tethered Wireless Earphones: Wireless tethered/neckband earphones (e.g., BeatsX, Beats Flex, Blue Byrd, B&O Earset Wireless) MUST use form_factor "{tws}" and connector "Bluetooth" with impedance 0 and sensitivity 0.

3. Price & Price Tiers:
   - price_usd: Launch MSRP (integer, rounded to nearest $5).
   - Exactly ONE mandatory tier tag matching price_usd:
{tiers}

{_rule}
TAG AUDIT & CONFLICT RESOLUTION MATRIX
{_rule}
Use ONLY these {n_tags} approved tags:

{tag_groups}

FORBIDDEN CONFLICTING PAIRS (MUST REMOVE/RESOLVE):
{conflicts}

Tag Rules:
- Enforce tag count between {min_tags} and {max_tags} tags.
- Enforce at most ONE primary tonal descriptor from {{Neutral, Balanced, V-Shaped, U-Shaped}}.
- Remove duplicate tags and unapproved synonyms.

{_rule}
FALLBACK: RAW FREQUENCY RESPONSE (.TXT) AUDIT ANALYSIS
{_rule}
When auditing tags for new or obscure models where written reviews are scarce:
- Inspect the linked raw measurement file (.txt) if available in the database/chunk.
- Calculate deltas relative to 1kHz:
  * Bass Shelf (20-100Hz) >8dB vs 1kHz -> Verify "Basshead" / "Sub-Bass"
  * Pinna Gain (2.5-3.5kHz) <6dB vs 1kHz -> Verify "Warm" / "Dark" (reject "Bright")
  * Pinna Gain (2.5-3.5kHz) >12dB vs 1kHz -> Verify "Bright" / "Vocal-Focused"
  * Midrange scooped relative to bass & treble -> Verify "V-Shaped" / "U-Shaped" (reject "Neutral")
- Align the tags strictly to the physical acoustic measurements.

{_rule}
FILE INTEGRITY & EXTRACTION RULES
{_rule}
- Preserve exact file paths (never rename, re-capitalize, or alter paths).
- If a measurement file inside the active batch belongs to a different product:
  1. Remove it from the current entry.
  2. Output the new standalone entry for the target model containing that file as part of your JSON array.
  3. Add a comment line above it: // Extracted cross-chunk file [file_path] into [target_id].

{_rule}
FINAL AUDIT PROTOCOL
{_rule}
1. Research every product online using Tier 1A manufacturer sources first before evaluating metadata.
2. Change a field ONLY when High Confidence evidence proves it is incorrect.
3. Never guess. Populate missing fields (like 0 values on wired gear) only with verified specs.

Begin by confirming you understand these rules and asking the user to say "Start".""".format(
        _rule=_RULE,
        schema=_schema_block(),
        tws=L.TWS_FORM_FACTOR,
        n_ff=len(L.FORM_FACTORS),
        form_factors=", ".join(L.FORM_FACTORS),
        n_conn=len(L.CONNECTORS_ALL),
        connectors=", ".join(L.CONNECTORS_ALL),
        driver_types=", ".join(t for t in L.ALLOWED_DRIVER_TYPES if t),
        driver_order=" -> ".join(L.DRIVER_TECH_ORDER),
        n_tags=len(L.APPROVED_TAGS),
        tag_groups=_tag_list_block(),
        min_tags=L.MIN_TAGS,
        max_tags=L.MAX_TAGS,
        tiers=_tier_block(),
        conflicts=_conflict_block(),
    )


PROMPTS = [
    ("Add Entry", "ADD_ENTRY_PROMPT.md", build_add_entry_prompt),
    ("Audit Database", "AUDIT_DATABASE_PROMPT.md", build_audit_prompt),
]
