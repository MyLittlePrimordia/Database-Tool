"""
spell_logic.py
Offline spellcheck engine for the Database Editor identity fields
(Brand / Model / Variant). Pure logic, no tkinter dependency, so it can be
unit-tested independently of the GUI.

Design:
- KNOWN words = bundled English dictionary (assets/spell/full_english.txt,
  ~370k words) UNION a common-words list (common_english.txt, used for
  ranking suggestions) UNION hardcoded audio/hobbyist domain terms UNION a
  dynamic vocabulary built from the database itself (brands / models /
  variants), so product names already in the DB are never flagged.
- A token is only FLAGGED when it is unknown AND at least one plausible
  correction exists within Damerau-Levenshtein distance <= MAX_DIST in the
  fast pools (common + domain + dynamic). This keeps legitimate invented
  brand names from being permanently underlined while still catching real
  typos that have an obvious neighbour ("Anlysis" -> "Analysis").
- Suggestions are ranked by edit distance, then common-word frequency rank,
  then alphabetically. The expensive full-dictionary fuzzy scan runs only on
  demand (right-click) behind a character-bigram prefilter and a time budget.
"""

import os
import re
import threading
import time
import unicodedata

MAX_DIST = 2            # max Damerau-Levenshtein distance to count as a typo match
MIN_TOKEN_LEN = 3       # tokens shorter than this are never flagged (V2, MK, SA6...)
SUGGEST_TIME_BUDGET = 0.15  # F-C8: runs on the UI thread - keep worst-case stall small

# Audio / hobbyist vocabulary that is valid in Brand/Model/Variant fields and
# improves suggestion quality even if missing from generic word lists.
DOMAIN_TERMS = frozenset({
    "audio", "audiophile", "acoustic", "acoustics", "iem", "iems",
    "earbud", "earbuds", "earphone", "earphones",
    "headphone", "headphones", "headset", "headsets",
    "impedance", "sensitivity", "sensitivities", "planar", "planars",
    "armature", "armatures", "electrostatic", "piezoelectric", "piezo",
    "mems", "tribrid", "hybrid", "hybrids", "driver", "drivers",
    "dynamic", "dynamics", "conduction", "nozzle", "nozzles",
    "eartip", "eartips", "cable", "cables", "connector", "connectors",
    "detachable", "proprietary", "tonality", "soundstage", "imaging",
    "treble", "midrange", "mids", "bass", "monitor", "monitors",
    "studio", "wireless", "bluetooth", "tws", "edition", "editions",
    "limited", "custom", "customs", "ultra", "plus", "mini", "nano",
    "lite", "classic", "retro", "titanium", "carbon", "copper", "silver",
    "gold", "brass", "resin", "shell", "shells", "faceplate", "faceplates",
    "universal", "anniversary", "collab", "collaboration", "premium",
    "flagship", "budget", "reference", "tuning", "retune", "remastered",
})

# One letter to start, inner letters/apostrophes/hyphens, must end on a letter.
# Unicode-aware: matches accented letters too so spans align with raw text.
TOKEN_RE = re.compile(r"[^\W\d_](?:[^\W\d_'’\-]*[^\W\d_])?", re.UNICODE)


def fold_ascii(text):
    """Lowercase + strip accents ('Café' -> 'cafe') for membership checks."""
    text = unicodedata.normalize("NFKD", str(text))
    return text.encode("ascii", "ignore").decode("ascii").lower()


def alpha_core(token):
    """Letters only ('Wan'er' -> 'waner'); used as the fuzzy-search key."""
    return re.sub(r"[^a-z]", "", fold_ascii(token))


def _vocab_token_keys(token):
    """Indexable keys for one raw token: folded form plus letters-only core."""
    keys = set()
    folded = fold_ascii(token)
    if folded:
        keys.add(folded)
    core = alpha_core(token)
    if core:
        keys.add(core)
    return keys


def damerau_distance(a, b):
    """Optimal-string-alignment Damerau-Levenshtein distance."""
    la, lb = len(a), len(b)
    if abs(la - lb) > MAX_DIST:
        return MAX_DIST + 1
    if a == b:
        return 0
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2 = prev
        prev = cur
    return prev[lb]


class SpellChecker:
    """Offline spellchecker with database-derived vocabulary."""

    def __init__(self, base_dir_provider=None):
        self._base = base_dir_provider or (
            lambda: os.path.dirname(os.path.abspath(__file__)))
        self._known = set(DOMAIN_TERMS)
        self._ranks = {}          # word -> common-rank (lower == more common)
        self._dynamic = set()     # vocab derived from the loaded database
        self._pool_cache = None   # cached small-pool buckets for fast suggestions
        self._sorted_known = None  # cached sorted(self._known) for deterministic scans
        self._flag_cache = {}     # memoized per-token flag results
        self._full_loaded = False
        self._ready = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Dictionary loading
    # ------------------------------------------------------------------
    def load_sync(self):
        """Read both dictionary files OUTSIDE the lock (a 4 MB word list can
        take hundreds of ms on slow disks; holding the lock that long stalled
        UI-thread callers such as add_vocab), then merge under the lock."""
        base = self._base()
        cpath = os.path.join(base, "assets", "spell", "common_english.txt")
        fpath = os.path.join(base, "assets", "spell", "full_english.txt")
        local_ranks = {}
        rank = 0
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip().lower()
                    if w and w not in local_ranks:
                        local_ranks[w] = rank
                        rank += 1
        except OSError:
            pass
        local_full = set()
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip()
                    if w:
                        local_full.add(w)
        except OSError:
            pass
        with self._lock:
            for w, r in local_ranks.items():
                if w not in self._ranks:
                    self._ranks[w] = r
                    self._known.add(w)
            self._known.update(local_full)
            self._known.update(self._dynamic)
            self._full_loaded = True
            self._ready = True
            self._pool_cache = None
            self._sorted_known = None
            self._flag_cache = {}

    def load_async(self):
        t = threading.Thread(target=self.load_sync, daemon=True)
        t.start()
        return t

    @property
    def ready(self):
        return self._ready

    def wait_ready(self, timeout=10.0):
        """Block until dictionaries finish loading (used by tests)."""
        deadline = time.time() + timeout
        while not self._ready and time.time() < deadline:
            time.sleep(0.02)
        return self._ready

    # ------------------------------------------------------------------
    # Dynamic vocabulary (from the database)
    # ------------------------------------------------------------------
    @staticmethod
    def _vocab_keys(text):
        """All indexable keys for a free-text value: the folded whole value
        plus each alpha sub-token of length >= MIN_TOKEN_LEN."""
        keys = set()
        folded = fold_ascii(text)
        if folded:
            keys.add(folded)
        for m in TOKEN_RE.finditer(str(text)):
            keys.update(_vocab_token_keys(m.group(0)))
        return keys

    def replace_dynamic_vocab(self, texts):
        """Rebuild the database-derived vocabulary from scratch."""
        dyn = set()
        for text in texts or []:
            dyn.update(self._vocab_keys(text))
        with self._lock:
            self._dynamic = dyn
            # Stale entries from a previously loaded DB only cause *fewer*
            # false flags, never errors, so union into known.
            self._known.update(dyn)
            self._pool_cache = None
            self._sorted_known = None
            self._flag_cache = {}

    def add_vocab(self, text):
        """Add one free-text value (e.g. after saving a new entry)."""
        keys = self._vocab_keys(text)
        with self._lock:
            self._dynamic.update(keys)
            self._known.update(keys)
            self._pool_cache = None
            self._sorted_known = None
            self._flag_cache = {}

    # ------------------------------------------------------------------
    # Checking
    # ------------------------------------------------------------------
    def _is_known(self, token_lower):
        if token_lower in self._known:
            return True
        folded = fold_ascii(token_lower)
        return folded != token_lower and folded in self._known

    def _fast_candidates(self, core, exclude=None):
        """Distance-bounded candidates from the small pools (common list,
        domain terms, dynamic DB vocab). Returns {word: distance}.

        Uses a length-bucketed pool plus a character-set overlap pre-filter
        so the Damerau-Levenshtein DP only runs on plausible neighbours --
        this must stay fast enough to call on every debounced keystroke."""
        best = {}
        cset = set(core)
        strict_chars = len(core) >= 4
        lo = max(1, len(core) - MAX_DIST)
        hi = len(core) + MAX_DIST
        buckets = self._pool_buckets()
        for length in range(lo, hi + 1):
            for w, wset in buckets.get(length, ()):
                if w == exclude:
                    continue
                if strict_chars and not (wset & cset):
                    continue
                d = damerau_distance(core, w)
                if d <= MAX_DIST:
                    cur = best.get(w)
                    if cur is None or d < cur:
                        best[w] = d
        return best

    def _pool_buckets(self):
        """Small candidate pools indexed by word length -> [(word, charset)]."""
        if self._pool_cache is None:
            buckets = {}
            for w in list(self._ranks.keys()) + list(DOMAIN_TERMS) + list(self._dynamic):
                if not w or len(w) > 28:
                    continue
                buckets.setdefault(len(w), []).append((w, frozenset(w)))
            self._pool_cache = buckets
        return self._pool_cache

    def check_text(self, text):
        """Return [(start, end, word), ...] spans of flagged tokens in text.
        Empty until dictionaries finish loading; tokens containing digits,
        or shorter than MIN_TOKEN_LEN, are never flagged.

        Results are memoized per token (cleared on vocabulary changes) so
        repeated debounced re-checks stay cheap."""
        out = []
        if not text:
            return out
        if not self._ready:
            return out
        for m in TOKEN_RE.finditer(text):
            tok = m.group(0)
            if len(tok) < MIN_TOKEN_LEN:
                continue
            low = tok.lower()
            if self._is_known(low):
                continue
            core = alpha_core(tok)
            if len(core) < MIN_TOKEN_LEN:
                continue
            cached = self._flag_cache.get(core)
            if cached is None:
                cached = bool(self._fast_candidates(core))
                self._flag_cache[core] = cached
                if len(self._flag_cache) > 20000:
                    self._flag_cache.clear()
            if cached:
                out.append((m.start(), m.end(), tok))
        return out

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------
    def _full_dict_scan(self, core, budget=SUGGEST_TIME_BUDGET):
        """On-demand scan of the full bundled dictionary, pruned by length
        and character-bigram overlap, under a wall-clock budget.

        Iterates a cached sorted snapshot of the dictionary so suggestion
        sets are deterministic across runs (plain set iteration order is
        not). Yields (word, distance) pairs so callers never recompute the
        distance."""
        found = []
        bigrams = {core[i:i + 2] for i in range(len(core) - 1)}
        if self._sorted_known is None:
            self._sorted_known = sorted(self._known)
        words = self._sorted_known
        start = time.time()
        checked = 0
        for w in words:
            checked += 1
            # F-C8: check far more often so worst-case stall stays small,
            # and stop once we already have plenty of candidates.
            if checked % 10000 == 0 and time.time() - start > budget:
                break
            if len(found) >= 60:
                break
            if abs(len(w) - len(core)) > MAX_DIST:
                continue
            if len(w) >= 5 and bigrams:
                wb = {w[i:i + 2] for i in range(len(w) - 1)}
                if not wb & bigrams:
                    continue
            d = damerau_distance(core, w)
            if d <= MAX_DIST:
                found.append((w, d))
                if time.time() - start > budget:
                    break
        return found

    def suggest(self, word, limit=5):
        """Up to `limit` correction candidates for `word`, best first.
        Ranked by edit distance, then common-word frequency, then A-Z.
        F-C8: one global deadline covers BOTH phases so the right-click
        menu can never stall the UI thread beyond the budget."""
        core = alpha_core(word)
        if not core or not self._ready:
            return []
        deadline = time.time() + SUGGEST_TIME_BUDGET
        dists = dict(self._fast_candidates(core, exclude=core))
        remaining = deadline - time.time()
        if len(dists) < limit and self._full_loaded and remaining > 0.01:
            for w, d in self._full_dict_scan(core, budget=remaining):
                if w != core and w not in dists:
                    dists[w] = d
                if time.time() > deadline:
                    break
        ranked = sorted(
            dists.items(),
            key=lambda kv: (kv[1], self._ranks.get(kv[0], 10 ** 9), kv[0]),
        )
        return [w for w, _ in ranked[:limit]]

    def known_word(self, word):
        """True if word is a known English/domain/DB term (for tests/UI)."""
        return self._is_known((word or "").lower())
