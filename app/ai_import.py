"""
ai_import.py -- "Import Entries": review & apply AI-produced database
changes (the round-trip half of the AI workflow).

Input formats accepted (auto-detected, can be mixed):
  * a JSON array of entry objects (the generated prompts' format)
  * a single JSON object
  * Notepad++-style SEARCH:/REPLACE: blocks (the legacy audit format)
  * loose objects scattered in prose, markdown fences, // comments, and
    trailing commas are all tolerated

Everything is classified against the CURRENT database into three buckets
-- NEW (id unknown), CHANGED (field-level diff vs. the existing entry),
DELETE (REPLACE block empty/null) -- shown for review, and only the
ticked rows are applied, as ONE undoable operation, after full
schema validation. The database is never touched before Apply.

Offline, stdlib-only.
"""

import json
import re
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import db_logic as L
import theme
import win_drop

APP_TITLE = "Database Tool"

SCHEMA_FIELDS = [name for name, _ in [
    ("id", ""), ("brand", ""), ("model", ""), ("variant", ""), ("year", 0),
    ("price_usd", 0), ("driver_type", ""), ("driver_config", ""),
    ("impedance", 0), ("sensitivity", 0), ("connector", ""),
    ("form_factor", ""), ("tags", []), ("files", [])]]


# ---------------------------------------------------------------------------
# PARSING (pure functions -- unit-testable without Tk)
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*(```|~~~)[a-zA-Z0-9_-]*\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SR_BLOCK_RE = re.compile(
    r"SEARCH\s*:\s*(?P<search>.*?)REPLACE\s*:\s*(?P<replace>.*?)(?=\n\s*SEARCH\s*:|\Z)",
    re.DOTALL | re.IGNORECASE)
# Explicit deletion marker at the start of a REPLACE region: bare `null`
# or an empty object (optionally spaced).
_EMPTY_REPLACE_RE = re.compile(r"^(?:null|\{\s*\})", re.IGNORECASE)


def _clean(text):
    """Strip markdown fences and full-line // comments (a '//' inside a
    JSON string never starts a line, so values survive)."""
    text = text.replace("\ufeff", "")
    text = _FENCE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    return text.strip()


def _loads_lenient(raw):
    """json.loads with trailing-comma tolerance. Returns (obj, None) or
    (None, error-string)."""
    raw = raw.strip()
    if not raw:
        return None, "empty"
    try:
        return json.loads(raw), None
    except ValueError:
        pass
    try:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", raw)), None
    except ValueError as e:
        return None, str(e)


def _balanced_objects(text):
    """Extract every balanced {...} JSON object from `text` as
    (obj, start, end) spans. Tolerates prose between objects; skips braces
    inside JSON strings."""
    found = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        end = -1
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            j += 1
        if end < 0:
            break              # unbalanced to EOF: give up on the remainder
        obj, _err = _loads_lenient(text[i:end])
        if obj is not None:
            found.append((obj, i, end))
        i = end
    return found


def parse_search_replace(text):
    """Notepad++-style blocks. Returns (replacements, standalone, errors):
    each replacement is (search_obj_or_None, replace_obj_or_None, "");
    standalone holds complete JSON objects found anywhere outside the
    paired SEARCH/REPLACE slots (e.g. an extracted new entry after a
    block)."""
    replacements = []
    standalone = []
    errors = []
    # Spans MUST be computed on the SAME cleaned text that
    # _balanced_objects() scans below: stripping fences/comment lines
    # shifts offsets, and mixing the two bases used to attribute REPLACE
    # objects to "standalone" whenever a // comment preceded the block
    # (the format our own generated prompt recommends).
    clean = _clean(text)
    spans = [(m.start("search"), m.end("search"),
              m.start("replace"), m.end("replace"))
             for m in _SR_BLOCK_RE.finditer(clean)]
    if not spans:
        return replacements, standalone, errors

    # Attribute content per block directly (no global positional matching):
    # - SEARCH region -> first balanced object is the target entry
    # - REPLACE region -> first object is the corrected entry; a bare
    #   `null` / `{}` is an EXPLICIT deletion marker (it produces no
    #   balanced object, so without this check a deletion followed by a
    #   standalone new entry made the new entry masquerade as the
    #   replacement). Anything past the first object in either region is
    #   standalone data, never silently dropped.
    covered = []
    for bi, (ss, se, rs, re_) in enumerate(spans):
        sc = [o for o, _s, _e in _balanced_objects(clean[ss:se])]
        r_txt = clean[rs:re_]
        stripped = r_txt.lstrip()
        dele = _EMPTY_REPLACE_RE.match(stripped)
        if dele is not None:
            replace_obj = None
            tail = r_txt[len(r_txt) - len(stripped) + dele.end():]
            extra = [o for o, _s, _e in _balanced_objects(tail)]
        else:
            robjs = [o for o, _s, _e in _balanced_objects(r_txt)]
            replace_obj = robjs[0] if robjs else None
            extra = robjs[1:]
        if not sc:
            errors.append("A SEARCH block contained no parseable JSON "
                          "entry; it was skipped.")
            standalone.extend(extra)
            continue
        replacements.append((sc[0], replace_obj, ""))
        standalone.extend(sc[1:])
        standalone.extend(extra)
        covered.append((ss, re_))

    # Any object OUTSIDE every block's span is a standalone entry (e.g. a
    # new entry appended after the last block).
    for obj, start, _end in _balanced_objects(clean):
        if not any(a <= start < b for a, b in covered):
            standalone.append(obj)
    return replacements, standalone, errors


def parse_ai_output(text):
    """Parse any supported AI reply. Returns a dict:
       {"objects": [entry, ...],                      # standalone entries
        "replacements": [(search, replace|None)],     # SEARCH/REPLACE pairs
        "errors": [str, ...]}"""
    cleaned = _clean(text)
    errors = []

    # 1) whole-text JSON (array or single object)
    obj, err = _loads_lenient(cleaned)
    if obj is not None:
        objects = []
        if isinstance(obj, list):
            objects = [o for o in obj if isinstance(o, dict)]
            errors.extend("Ignoring non-object array element #{}".format(i)
                          for i, o in enumerate(obj) if not isinstance(o, dict))
        elif isinstance(obj, dict):
            objects = [obj]
        return {"objects": objects, "replacements": [], "errors": errors}

    # 2) SEARCH/REPLACE blocks (+ standalone objects anywhere else)
    replacements, standalone, sr_errors = parse_search_replace(text)
    errors.extend(sr_errors)
    if replacements:
        return {"objects": [o for o in standalone if isinstance(o, dict)],
                "replacements": replacements, "errors": errors}

    # 3) loose objects in prose
    objects = [o for o, _s, _e in _balanced_objects(cleaned)
               if isinstance(o, dict)]
    if not objects and not errors:
        errors.append("No JSON entries found in the provided text.")
    return {"objects": objects, "replacements": [], "errors": errors}


# ---------------------------------------------------------------------------
# CLASSIFICATION against the live database
# ---------------------------------------------------------------------------
def _identity_key(entry):
    return (str(entry.get("brand") or "").strip().lower(),
            str(entry.get("model") or "").strip().lower(),
            str(entry.get("variant") or "").strip().lower())


def classify_against(entries, parsed):
    """Returns proposals:
       {"action": "new",      "entry": obj}
       {"action": "changed",  "pos": int, "old": dict, "new": dict,
        "changes": [(field, old_val, new_val), ...]}
       {"action": "delete",   "pos": int, "old": dict}
       {"action": "invalid",  "entry": obj, "error": str}
    """
    by_id = {}
    by_ident = {}
    for pos, e in enumerate(entries):
        eid = e.get("id")
        if eid:
            by_id.setdefault(eid, pos)
        by_ident.setdefault(_identity_key(e), pos)

    proposals = []
    claimed = set()          # positions already targeted by this import

    def target_of(obj):
        eid = obj.get("id")
        if eid and eid in by_id:
            return by_id[eid]
        return by_ident.get(_identity_key(obj), -1)

    def field_changes(old, new):
        changes = []
        for f in SCHEMA_FIELDS:
            ov, nv = old.get(f), new.get(f)
            if isinstance(ov, list) or isinstance(nv, list):
                if list(ov or []) != list(nv or []):
                    changes.append((f, ov, nv))
            elif str(ov) != str(nv):
                changes.append((f, ov, nv))
        return changes

    # SEARCH/REPLACE pairs first (they carry an explicit target)
    for search_obj, replace_obj, _note in parsed.get("replacements", []):
        if not isinstance(search_obj, dict):
            continue
        pos = target_of(search_obj)
        if pos < 0:
            # AI repaired the id inside REPLACE: try matching the REPLACE
            # content itself against the db (same product, fixed id)
            if isinstance(replace_obj, dict):
                pos = target_of({
                    "brand": replace_obj.get("brand"),
                    "model": replace_obj.get("model"),
                    "variant": replace_obj.get("variant"),
                })
        if pos < 0:
            if isinstance(replace_obj, dict):
                proposals.append({"action": "new", "entry": replace_obj})
            else:
                proposals.append({
                    "action": "invalid", "entry": search_obj,
                    "error": "SEARCH block matches no database entry"})
            continue
        if pos in claimed:
            proposals.append({
                "action": "invalid", "entry": search_obj,
                "error": "targeted by more than one block in this import"})
            continue
        claimed.add(pos)
        if not isinstance(replace_obj, dict) or not replace_obj:
            # REPLACE: null / {} -> proposed deletion (opt-in only).
            # The delete CLAIMS the position like every other action so
            # the same entry can never receive a second proposal.
            proposals.append({"action": "delete", "pos": pos,
                              "old": entries[pos]})
            continue
        changes = field_changes(entries[pos], replace_obj)
        if changes:
            proposals.append({"action": "changed", "pos": pos,
                              "old": entries[pos], "new": replace_obj,
                              "changes": changes})
        # no changes -> silently skip (AI agreed the entry is fine)

    # standalone objects (array / loose)
    for obj in parsed.get("objects", []):
        if not isinstance(obj, dict) or not obj:
            continue
        pos = target_of(obj)
        if pos >= 0:
            if pos in claimed:
                proposals.append({
                    "action": "invalid", "entry": obj,
                    "error": "targeted more than once in this import"})
                continue
            claimed.add(pos)
            changes = field_changes(entries[pos], obj)
            if changes:
                proposals.append({"action": "changed", "pos": pos,
                                  "old": entries[pos], "new": obj,
                                  "changes": changes})
        else:
            proposals.append({"action": "new", "entry": obj})
    return proposals


def _fmt_val(value):
    if isinstance(value, list):
        if not value:
            return "(empty)"
        return "[{}]".format(", ".join(str(v) for v in value))
    if value in (None, ""):
        return "(empty)"
    return str(value)


# ---------------------------------------------------------------------------
# DIALOG
# ---------------------------------------------------------------------------
class ImportDialog(tk.Toplevel):
    """Paste / drag & drop / browse AI output -> review proposed changes
    -> apply the ticked ones as one undoable operation."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.proposals = []
        self.include = {}          # tree item id -> bool (parent rows only)
        self.title("Import Entries")
        self.configure(background=theme.BG_MAIN)
        self.transient(app)
        self.minsize(760, 520)
        self.geometry("980x700")

        outer, card = theme.make_card(self)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(card, text="\u21EA  IMPORT AI CHANGES",
                  style="CardHeader.TLabel").pack(anchor="w", padx=8,
                                                  pady=(8, 2))
        ttk.Label(
            card,
            text="Paste an AI reply below (new entries as JSON, or corrected "
                 "entries, or SEARCH/REPLACE blocks), or drag & drop / browse "
                 "a .json file. Nothing is applied until you review and click "
                 "Apply -- the whole batch lands as a single undoable step.",
            style="Card.TLabel", foreground=theme.TEXT_DIM,
            wraplength=860, justify="left").pack(anchor="w", padx=8,
                                                 pady=(0, 6))

        # -- input phase ---------------------------------------------------
        self.text = tk.Text(card, height=9, bg=theme.BG_INPUT,
                            fg=theme.TEXT_MAIN,
                            insertbackground=theme.TEXT_MAIN,
                            font=theme.font(12), relief="flat",
                            wrap="none", padx=8, pady=6, undo=True)
        self.text.pack(fill="x", padx=8)
        attach_context_menu(self.text)

        btns = ttk.Frame(card, style="CardFlat.TFrame")
        btns.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(btns, text="Analyze", style="Accent.TButton",
                   command=self._analyze).pack(side="left")
        ttk.Button(btns, text="Browse...",
                   command=self._browse).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear",
                   command=lambda: self.text.delete("1.0", "end")
                   ).pack(side="left")
        self.parse_lbl = ttk.Label(btns, text="", style="Card.TLabel",
                                   foreground=theme.TEXT_DIM)
        self.parse_lbl.pack(side="left", padx=10)

        ttk.Separator(card).pack(fill="x", padx=8, pady=(6, 6))

        # -- review phase --------------------------------------------------
        self.summary_lbl = ttk.Label(card, text="Nothing analyzed yet.",
                                     style="Card.TLabel",
                                     foreground=theme.TEXT_DIM)
        self.summary_lbl.pack(anchor="w", padx=8)

        tree_frame = ttk.Frame(card, style="CardFlat.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(4, 4))
        self.tree = ttk.Treeview(tree_frame, show="tree headings",
                                 selectmode="browse",
                                 columns=("include", "detail"))
        self.tree.heading("#0", text="Entry / change")
        self.tree.heading("include", text="Include")
        self.tree.heading("detail", text="Detail")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.column("include", width=70, anchor="center", stretch=False)
        self.tree.column("detail", width=420, stretch=True)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("new", foreground=theme.ACCENT_GREEN)
        self.tree.tag_configure("changed", foreground=theme.ACCENT_BLUE)
        self.tree.tag_configure("delete", foreground=theme.ACCENT_RED)
        self.tree.tag_configure("invalid", foreground=theme.TEXT_DIM)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Return>", self._on_space)

        action_row = ttk.Frame(card, style="CardFlat.TFrame")
        action_row.pack(fill="x", padx=8, pady=(2, 10))
        self.apply_btn = ttk.Button(action_row, text="Apply Selected",
                                    style="Accent.TButton",
                                    command=self._apply, state="disabled")
        self.apply_btn.pack(side="left")
        ttk.Button(action_row, text="Close",
                   command=self.destroy).pack(side="left", padx=8)
        self.apply_lbl = ttk.Label(action_row, text="", style="Card.TLabel",
                                   foreground=theme.TEXT_DIM)
        self.apply_lbl.pack(side="left", padx=10)

        # OS drag & drop onto this dialog (Windows; Browse elsewhere)
        self._drop_enabled = win_drop.enable_native_file_drop(
            self, self._on_drop)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.text.focus_set()
    def destroy(self):
        if self._drop_enabled:
            win_drop.disable_native_file_drop(self)
        super().destroy()

    # -- input -------------------------------------------------------------
    def _on_drop(self, paths):
        for p in paths or []:
            if p.lower().endswith((".json", ".txt", ".md")):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        data = f.read()
                except OSError as e:
                    messagebox.showerror(APP_TITLE,
                                         "Could not read:\n{}\n\n{}".format(
                                             p, e), parent=self)
                    continue
                self.text.delete("1.0", "end")
                self.text.insert("1.0", data)
                self._analyze()
                return
        self.parse_lbl.configure(text="Drop a .json / .txt file to import.")

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Open an AI reply file",
            filetypes=[("JSON / text", "*.json *.txt *.md"),
                       ("All files", "*.*")])
        if not path:
            return
        self._on_drop([path])

    # -- analysis ----------------------------------------------------------
    def _analyze(self):
        raw = self.text.get("1.0", "end")
        if not raw.strip():
            self.parse_lbl.configure(text="Nothing to analyze -- paste or "
                                          "load an AI reply first.")
            return
        parsed = parse_ai_output(raw)
        self.proposals = classify_against(self.app.entries, parsed)
        self._render()

        bits = []
        n_new = sum(1 for p in self.proposals if p["action"] == "new")
        n_chg = sum(1 for p in self.proposals if p["action"] == "changed")
        n_del = sum(1 for p in self.proposals if p["action"] == "delete")
        n_bad = sum(1 for p in self.proposals if p["action"] == "invalid")
        if n_new:
            bits.append("{} new".format(n_new))
        if n_chg:
            bits.append("{} changed".format(n_chg))
        if n_del:
            bits.append("{} deletion(s) proposed".format(n_del))
        if n_bad:
            bits.append("{} unusable".format(n_bad))
        self.parse_lbl.configure(
            text=", ".join(bits) if bits else
            "No changes found (the AI output matches the current database).")
        for err in parsed.get("errors", [])[:2]:
            self.parse_lbl.configure(
                text=self.parse_lbl.cget("text") + "  |  " + err[:80])

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self.include.clear()
        n_ok = 0
        for idx, p in enumerate(self.proposals):
            action = p["action"]
            if action == "new":
                entry = p["entry"]
                eid = entry.get("id") or "(no id)"
                label = "NEW:  {}".format(eid)
                detail = "{} / {} / {}".format(
                    entry.get("brand") or "?", entry.get("model") or "?",
                    entry.get("variant") or "")
                tag = "new"
                default = True
            elif action == "changed":
                eid = p["old"].get("id") or "(no id)"
                label = "CHANGED:  {}  ({} field{})".format(
                    eid, len(p["changes"]),
                    "s" if len(p["changes"]) != 1 else "")
                detail = "; ".join("{}: {} -> {}".format(
                    f, _fmt_val(ov)[:40], _fmt_val(nv)[:40])
                    for f, ov, nv in p["changes"][:3])
                if len(p["changes"]) > 3:
                    detail += ", ..."
                tag = "changed"
                default = True
            elif action == "delete":
                eid = p["old"].get("id") or "(no id)"
                label = "DELETE:  {}".format(eid)
                detail = "AI output removed this entry -- opt in explicitly"
                tag = "delete"
                default = False
            else:
                eid = (p.get("entry") or {}).get("id") or "(unparseable)"
                label = "SKIP:  {}".format(eid)
                detail = p.get("error", "unusable")
                tag = "invalid"
                default = False
            pid = "p{}".format(idx)
            include_txt = "Yes" if default else "No"
            self.include[pid] = default and action in ("new", "changed")
            self.tree.insert("", "end", iid=pid, open=False,
                             text=label,
                             values=(include_txt, detail), tags=(tag,))
            if action in ("new", "changed"):
                n_ok += 1
            if action == "new":
                for f in SCHEMA_FIELDS:
                    if f in ("id",):
                        continue
                    v = _fmt_val(p["entry"].get(f))
                    if v != "(empty)":
                        self.tree.insert(pid, "end", text="    " + f,
                                         values=("", v))
            elif action == "changed":
                for f, ov, nv in p["changes"]:
                    self.tree.insert(pid, "end", text="    " + f,
                                     values=("",
                                             "{}  ->  {}".format(
                                                 _fmt_val(ov), _fmt_val(nv))))
            elif action == "invalid":
                self.tree.insert(pid, "end", text="    reason",
                                 values=("", p.get("error", "")))
        if n_ok:
            self.apply_btn.state(["!disabled"])
        else:
            self.apply_btn.state(["disabled"])
        self.apply_lbl.configure(text="" if n_ok else
                                 "Nothing importable was found.")

    # -- inclusion toggling -------------------------------------------------
    def _on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid in self.include:
            # let the tree open/close children first, then toggle include
            self.after(10, lambda: self._toggle(iid))
        return None

    def _on_space(self, _event=None):
        iid = self.tree.focus()
        if iid and iid in self.include:
            self._toggle(iid)
            return "break"
        return None

    def _toggle(self, iid):
        p = self.proposals[int(iid[1:])]
        if p["action"] not in ("new", "changed", "delete"):
            return
        self.include[iid] = not self.include.get(iid, False)
        self.tree.set(iid, "include", "Yes" if self.include[iid] else "No")

    # -- apply ---------------------------------------------------------------
    def _apply(self):
        included = [self.proposals[int(iid[1:])]
                    for iid, yes in self.include.items() if yes]
        if not included:
            messagebox.showinfo(APP_TITLE,
                                "Tick at least one entry to import.",
                                parent=self)
            return
        app = self.app
        # validate everything FIRST against the would-be database so a
        # single bad row cannot leave a half-applied batch behind
        live_ids = {e.get("id") for e in app.entries if e.get("id")}
        staged = []          # (proposal, clean_entry)
        problems = []
        for p in included:
            if p["action"] == "delete":
                staged.append((p, None))
                continue
            src = p["entry"] if p["action"] == "new" else p["new"]
            candidate = dict(src)
            if p["action"] == "changed":
                # keep AI-repaired ids, but rebuild when identity changed
                candidate.setdefault("id", p["old"].get("id"))
            candidate["id"] = L.build_id(str(candidate.get("brand") or ""),
                                         str(candidate.get("model") or ""),
                                         str(candidate.get("variant") or "")) \
                or candidate.get("id")
            exclude = p["old"].get("id") if p["action"] == "changed" else None
            errors = L.validate_entry(candidate, existing_ids=live_ids,
                                      exclude_id=exclude)
            if errors:
                problems.append("{}: {}".format(
                    candidate.get("id") or "(no id)", errors[0]))
                continue
            if p["action"] == "new":
                live_ids.add(candidate["id"])
            staged.append((p, candidate))
        if problems:
            if not messagebox.askyesno(
                    APP_TITLE,
                    "{} entr{} cannot be imported:\n\n- {}\n\n"
                    "Import the remaining valid entries anyway?".format(
                        len(problems), "y is" if len(problems) == 1 else
                        "ies are", "\n- ".join(problems[:8])),
                    parent=self):
                return

        # apply on the live list + build ONE history op.
        # ORDER MATTERS: every proposal's `pos` was captured against the
        # ORIGINAL list. Edits (in-place replacement) never shift
        # positions, so they go first in ascending order; deletions are
        # applied LAST in DESCENDING position order so each removal still
        # points at the right row no matter how many deletes ran before
        # it. The old interleaved loop deleted at pos N and then edited
        # pos M > N, silently hitting whatever shifted into M.
        changes = []
        applied_new = applied_changed = 0
        n_deleted = 0
        # Selection target for after the import: the FIRST new/changed
        # entry, tracked by id (not position) since later deletions in
        # this same batch can shift list indices out from under any
        # position captured earlier.
        first_touch_id = None

        def _edit_change(pos, old, clean):
            return {
                "pos_hint": pos,
                "ref_before": old, "copy_before": app._deepcopy(old),
                "ref_after": clean, "copy_after": app._deepcopy(clean),
            }

        for p, candidate in staged:
            if p["action"] != "new":
                continue
            clean = L.build_clean_entry(candidate)
            pos = len(app.entries)
            app.entries.append(clean)
            applied_new += 1
            if first_touch_id is None:
                first_touch_id = clean["id"]
            changes.append({
                "pos_hint": pos,
                "ref_before": None, "copy_before": None,
                "ref_after": clean, "copy_after": app._deepcopy(clean)})
        for p, candidate in staged:
            if p["action"] != "changed":
                continue
            clean = L.build_clean_entry(candidate)
            pos = p["pos"]
            old = app.entries[pos]
            app.entries[pos] = clean
            applied_changed += 1
            if first_touch_id is None:
                first_touch_id = clean["id"]
            changes.append(_edit_change(pos, old, clean))
        for p, _candidate in sorted(
                ((p, c) for p, c in staged if p["action"] == "delete"),
                key=lambda pc: pc[0]["pos"], reverse=True):
            pos = p["pos"]
            if not (0 <= pos < len(app.entries)) or \
                    app.entries[pos].get("id") != p["old"].get("id"):
                continue            # stale (defensive; cannot normally happen)
            old = app.entries[pos]
            del app.entries[pos]
            n_deleted += 1
            changes.append({
                "pos_hint": pos,
                "ref_before": old, "copy_before": app._deepcopy(old),
                "ref_after": None, "copy_after": None})

        if not changes:
            return
        n_touched = applied_new + applied_changed
        desc = "Imported {} entr{} from AI output{}".format(
            n_touched, "y" if n_touched == 1 else "ies",
            " ({} deleted)".format(n_deleted) if n_deleted else "")
        app._record_op("import", desc, changes)
        app.dirty = True
        app._mark_audit_dirty()
        app.populate_tree()
        app.refresh_spell_vocab()
        app._autosave()
        app._notify_db_changed()
        jump_note = ""
        if first_touch_id is not None:
            idx = next((i for i, e in enumerate(app.entries)
                       if e.get("id") == first_touch_id), None)
            if idx is not None:
                if app.search_var.get():
                    # a filter may be hiding the imported row -- lift it so
                    # the reveal works, same as reveal_entry() does for the
                    # Audit tab's "jump to entry".
                    app.search_var.set("")
                    if getattr(app, "_search_debounce_id", None):
                        try:
                            app.after_cancel(app._search_debounce_id)
                        except Exception:
                            pass
                        app._search_debounce_id = None
                    app.populate_tree()
                iid = "entry:{}".format(idx)
                parent = app.tree.parent(iid)
                if parent:
                    app.tree.item(parent, open=True)
                try:
                    # F-8: the row's brand page may not be materialized
                    # yet in the virtualized tree -- mount it first.
                    app._ensure_entry_visible(iid)
                    app.tree.see(iid)
                    app.tree.selection_set(iid)
                except Exception:
                    pass
                # selection_set fires <<TreeviewSelect>>, which loads the
                # entry into the editor and switches tabs (respecting the
                # unsaved-changes guard) -- but only re-do it explicitly if
                # that guard skipped it (form was already clean, no reload
                # needed a second time is harmless; a dirty form that the
                # user chose to keep should NOT be silently replaced).
                if not app.editor.form_is_dirty():
                    app.editing_index = idx
                    app._selected_iid = iid
                    app.editor.load_entry(app.entries[idx])
                    app.notebook.select(app.editor)
                if n_touched > 1:
                    jump_note = "  \u2022 jumped to 1 of {} imported entries".format(n_touched)
        app.status_var.set(desc + jump_note + ". Remember to Save to keep them.")
        self.destroy()


# ---------------------------------------------------------------------------
# small local helpers
# ---------------------------------------------------------------------------
def attach_context_menu(text_widget):
    """Cut/Copy/Paste/Select-All for the paste box (same menu as entries)."""
    menu = tk.Menu(text_widget, tearoff=0,
                   background=theme.BG_CARD, foreground=theme.TEXT_MAIN,
                   activebackground=theme.BORDER_LIGHT,
                   activeforeground=theme.TEXT_MAIN,
                   font=theme.font(13))

    def _cut():
        try:
            text_widget.clipboard_clear()
            text_widget.clipboard_append(text_widget.get("sel.first",
                                                         "sel.last"))
            text_widget.delete("sel.first", "sel.last")
        except Exception:  # noqa: BLE001
            pass

    def _copy():
        try:
            text_widget.clipboard_clear()
            text_widget.clipboard_append(text_widget.get("sel.first",
                                                         "sel.last"))
        except Exception:  # noqa: BLE001
            pass

    def _paste():
        try:
            text_widget.insert("insert", text_widget.clipboard_get())
        except Exception:  # noqa: BLE001
            pass

    def _popup(_event=None):
        menu.tk_popup(text_widget.winfo_pointerx(), text_widget.winfo_pointery())
        return "break"

    menu.add_command(label="Cut", command=_cut)
    menu.add_command(label="Copy", command=_copy)
    menu.add_command(label="Paste", command=_paste)
    menu.add_separator()
    menu.add_command(label="Select All",
                     command=lambda: (text_widget.tag_add("sel", "1.0", "end"),
                                      text_widget.mark_set("insert", "end")))
    text_widget.bind("<Button-3>", _popup)
    if sys.platform == "darwin":
        text_widget.bind("<Button-2>", _popup)
