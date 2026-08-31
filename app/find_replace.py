"""
find_replace.py -- "Find & Replace" dialog (feature 3.1 from the audit
report): apply the same text change across many entries at once, with a
preview you can review and selectively opt out of before anything is
written.

Reuses the app's existing machinery end to end instead of inventing a
parallel path:
  - staged validation before any mutation (same shape as ai_import.py's
    ImportDialog._apply -- candidates are built and validated FIRST; only
    entries that pass are applied)
  - one MainApp._record_op() call for the whole batch, so the result is a
    single undoable/redoable step in the History tab like every other bulk
    operation in the app (Fix All, AI import, merge)
  - db_logic.build_id() / validate_entry() so a Brand/Model/Variant rename
    can never leave a stale id or collide with another entry

Fields covered:
  - Brand / Model / Variant / Driver Config: free-text find & replace
    (exact / contains / regex), optionally case-sensitive. Renaming
    Brand/Model/Variant rebuilds the id automatically.
  - Tag: a controlled-vocabulary RENAME (whole-tag match only -- tags are
    discrete tokens, not free text) across every entry that carries it.
    Leaving "Replace with" empty removes the tag instead of renaming it.
  - File Path: find & replace within each linked measurement file path
    (e.g. renaming a data-folder prefix across every entry that uses it).

Offline; no new dependencies.
"""

import re
import tkinter as tk
from tkinter import ttk, messagebox

import db_logic as L
import theme

APP_TITLE = "Database Tool"

# (field key, display label, kind) -- kind selects how matching/preview/
# apply behave: "text" for plain string fields, "tag" for the controlled
# vocabulary rename, "files" for the file-path list.
_FIELDS = [
    ("brand", "Brand", "text"),
    ("model", "Model", "text"),
    ("variant", "Variant", "text"),
    ("driver_config", "Driver Config", "text"),
    ("tag", "Tag (rename)", "tag"),
    ("files", "File Path", "files"),
]
_FIELD_KIND = {key: kind for key, _label, kind in _FIELDS}
_FIELD_LABEL = {key: label for key, label, _kind in _FIELDS}

_MODES = [("contains", "Contains"), ("exact", "Exact match"),
          ("regex", "Regex")]


def _apply_text(value, find, replace, mode, case_sensitive):
    """Apply one find/replace transform to a single string value."""
    if mode == "regex":
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            return re.sub(find, replace, value, flags=flags)
        except re.error:
            return value
    if mode == "exact":
        target = value if case_sensitive else value.lower()
        needle = find if case_sensitive else find.lower()
        return replace if target == needle else value
    # contains / substring
    if case_sensitive:
        return value.replace(find, replace)
    pattern = re.compile(re.escape(find), re.IGNORECASE)
    return pattern.sub(lambda _m: replace, value)


class FindReplaceDialog(tk.Toplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Find & Replace")
        self.configure(background=theme.BG_MAIN)
        self.transient(master)
        self.minsize(780, 560)
        self.geometry("900x620")

        self._rows_by_pos = {}     # pos -> (old_value, new_value)
        self.include = {}          # iid -> bool

        outer, card = theme.make_card(self)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(card, text="\u21c6  FIND & REPLACE",
                  style="CardHeader.TLabel").pack(anchor="w", padx=8,
                                                  pady=(8, 2))
        ttk.Label(
            card,
            text="Change one field across every matching entry at once. "
                 "Nothing is written until you press Apply -- review the "
                 "preview below and untick anything you don't want.",
            style="Card.TLabel", foreground=theme.TEXT_DIM,
            wraplength=780, justify="left").pack(anchor="w", padx=8,
                                                 pady=(0, 8))

        ctrl = ttk.Frame(card, style="CardFlat.TFrame")
        ctrl.pack(fill="x", padx=8, pady=(0, 4))
        for c in range(4):
            ctrl.columnconfigure(c, weight=1)

        ttk.Label(ctrl, text="Field", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=0, sticky="w")
        self.field_var = tk.StringVar(value=_FIELDS[0][0])
        field_combo = ttk.Combobox(
            ctrl, state="readonly", textvariable=self.field_var,
            values=[label for _key, label, _kind in _FIELDS])
        field_combo.current(0)
        field_combo.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        field_combo.bind("<<ComboboxSelected>>",
                         lambda e: self._on_field_changed(field_combo))
        self._field_combo = field_combo

        ttk.Label(ctrl, text="Match", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=1, sticky="w")
        self.mode_var = tk.StringVar(value="contains")
        self.mode_combo = ttk.Combobox(
            ctrl, state="readonly", textvariable=self.mode_var,
            values=[label for _key, label in _MODES])
        self.mode_combo.current(0)
        self.mode_combo.grid(row=1, column=1, sticky="ew", padx=6)

        self.case_var = tk.BooleanVar(value=False)
        self.case_chk = ttk.Checkbutton(ctrl, text="Case-sensitive",
                                        variable=self.case_var,
                                        style="Card.TCheckbutton")
        self.case_chk.grid(row=1, column=2, sticky="w", padx=6)

        ttk.Button(ctrl, text="Scan", style="Accent.TButton",
                   command=self._scan).grid(row=1, column=3, sticky="e")

        find_row = ttk.Frame(card, style="CardFlat.TFrame")
        find_row.pack(fill="x", padx=8, pady=(4, 6))
        find_row.columnconfigure(0, weight=1)
        find_row.columnconfigure(1, weight=1)
        ttk.Label(find_row, text="Find", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=0, sticky="w")
        ttk.Label(find_row, text="Replace with", style="Card.TLabel",
                  foreground=theme.TEXT_DIM).grid(row=0, column=1, sticky="w",
                                                  padx=(8, 0))
        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.find_entry = ttk.Entry(find_row, textvariable=self.find_var,
                                    font=theme.font(13))
        self.find_entry.grid(row=1, column=0, sticky="ew")
        self.replace_entry = ttk.Entry(find_row, textvariable=self.replace_var,
                                       font=theme.font(13))
        self.replace_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))

        self.status_lbl = ttk.Label(card, text="", style="Card.TLabel",
                                    foreground=theme.TEXT_DIM,
                                    wraplength=780, justify="left")
        self.status_lbl.pack(anchor="w", padx=8, pady=(0, 4))

        tree_frame = ttk.Frame(card, style="CardFlat.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tree = ttk.Treeview(tree_frame, show="tree headings",
                                 selectmode="browse",
                                 columns=("include", "change"))
        self.tree.heading("#0", text="Entry")
        self.tree.heading("include", text="Include")
        self.tree.heading("change", text="Old  \u2192  New")
        self.tree.column("#0", width=260, stretch=True)
        self.tree.column("include", width=70, anchor="center", stretch=False)
        self.tree.column("change", width=420, stretch=True)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Return>", self._on_space)

        action_row = ttk.Frame(card, style="CardFlat.TFrame")
        action_row.pack(fill="x", padx=8, pady=(2, 10))
        self.apply_btn = ttk.Button(action_row, text="Apply",
                                    style="Accent.TButton",
                                    command=self._apply, state="disabled")
        self.apply_btn.pack(side="left")
        ttk.Button(action_row, text="Close",
                   command=self.destroy).pack(side="left", padx=8)
        self.apply_lbl = ttk.Label(action_row, text="", style="Card.TLabel",
                                   foreground=theme.TEXT_DIM)
        self.apply_lbl.pack(side="left", padx=10)

        self._on_field_changed(field_combo)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.find_entry.focus_set()
        self.grab_set()

    # -- field-kind switching -----------------------------------------
    def _on_field_changed(self, field_combo):
        key = _FIELDS[field_combo.current()][0]
        self.field_var.set(key)
        kind = _FIELD_KIND[key]
        if kind == "tag":
            # tags are a controlled vocabulary: whole-tag rename only, no
            # substring/regex matching, no case option
            self.mode_var.set("exact")
            self.mode_combo.configure(state="disabled")
            self.case_var.set(True)
            self.case_chk.configure(state="disabled")
        else:
            self.mode_combo.configure(state="readonly")
            self.case_chk.configure(state="normal")

    def _mode_key(self):
        label = self.mode_var.get()
        for key, lbl in _MODES:
            if lbl == label or key == label:
                return key
        return "contains"

    # -- scan / preview --------------------------------------------------
    def _scan(self):
        field = self.field_var.get()
        kind = _FIELD_KIND[field]
        find = self.find_var.get()
        replace = self.replace_var.get()
        mode = self._mode_key()
        case_sensitive = self.case_var.get()

        if not find.strip() and kind != "tag":
            self.status_lbl.configure(text="Enter something to find.")
            self._render_rows({})
            return
        if kind == "tag" and not find:
            self.status_lbl.configure(text="Enter the exact tag to rename "
                                            "(case-sensitive).")
            self._render_rows({})
            return
        if kind == "tag" and replace and replace not in L.APPROVED_TAGS:
            self.status_lbl.configure(
                text="'{}' is not an approved tag -- leave 'Replace with' "
                     "empty to remove the tag instead of renaming "
                     "it.".format(replace))
            self._render_rows({})
            return

        rows = {}
        for pos, entry in enumerate(self.app.entries):
            if kind == "tag":
                tags = list(entry.get("tags") or [])
                if find not in tags:
                    continue
                if replace:
                    new_tags = [replace if t == find else t for t in tags]
                    # Deduplicate: renaming Warm->Budget when Budget already
                    # present would create ['Budget','Budget'] which always
                    # fails validation. Keep first occurrence, preserve order.
                    seen = set()
                    deduped = []
                    for t in new_tags:
                        if t not in seen:
                            seen.add(t)
                            deduped.append(t)
                    new_tags = deduped
                else:
                    new_tags = [t for t in tags if t != find]
                if new_tags != tags:
                    rows[pos] = (tags, new_tags)
            elif kind == "files":
                files = list(entry.get("files") or [])
                new_files = [_apply_text(f, find, replace, mode, case_sensitive)
                             for f in files]
                if new_files != files:
                    rows[pos] = (files, new_files)
            else:
                old = str(entry.get(field) or "")
                new = _apply_text(old, find, replace, mode, case_sensitive)
                if new != old:
                    rows[pos] = (old, new)

        self._render_rows(rows)
        if not rows:
            self.status_lbl.configure(
                text="No matches. Nothing would change.")
        else:
            self.status_lbl.configure(
                text="{} entr{} would change. Review below, untick "
                     "anything to skip, then Apply.".format(
                         len(rows), "y" if len(rows) == 1 else "ies"))

    @staticmethod
    def _fmt(value):
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) or "(empty)"
        return str(value) if value not in (None, "") else "(empty)"

    def _render_rows(self, rows):
        self.tree.delete(*self.tree.get_children())
        self.include.clear()
        self._rows_by_pos = dict(rows)
        self._iid_by_pos = {}
        for pos, (old_val, new_val) in sorted(rows.items()):
            entry = self.app.entries[pos]
            eid = entry.get("id") or "(no id) #{}".format(pos)
            iid = "r{}".format(pos)
            self._iid_by_pos[pos] = iid
            self.include[iid] = True
            change = "{}  \u2192  {}".format(self._fmt(old_val),
                                             self._fmt(new_val))
            self.tree.insert("", "end", iid=iid, text=eid,
                             values=("Yes", change))
        self.apply_btn.configure(state="normal" if rows else "disabled")

    # -- include toggling (mirrors ai_import.py's ImportDialog) ----------
    def _toggle(self, iid):
        if iid not in self.include:
            return
        self.include[iid] = not self.include[iid]
        self.tree.set(iid, "include", "Yes" if self.include[iid] else "No")

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle(row)

    def _on_space(self, _event=None):
        sel = self.tree.selection()
        if sel:
            self._toggle(sel[0])

    # -- apply -------------------------------------------------------------
    def _field_label(self):
        return _FIELD_LABEL[self.field_var.get()]

    def _apply(self):
        included = [pos for pos, iid in self._iid_by_pos.items()
                   if self.include.get(iid)]
        if not included:
            messagebox.showinfo(APP_TITLE, "Tick at least one row to apply.",
                                parent=self)
            return

        field = self.field_var.get()
        kind = _FIELD_KIND[field]
        app = self.app
        live_ids = {e.get("id") for e in app.entries if e.get("id")}

        staged = []       # [(pos, candidate_dict)]
        problems = []
        for pos in included:
            entry = app.entries[pos]
            old_val, new_val = self._rows_by_pos[pos]
            candidate = dict(entry)
            if kind == "tag":
                candidate["tags"] = list(new_val)
            elif kind == "files":
                candidate["files"] = list(new_val)
            else:
                candidate[field] = new_val
                if field in ("brand", "model", "variant"):
                    new_id = L.build_id(str(candidate.get("brand") or ""),
                                        str(candidate.get("model") or ""),
                                        str(candidate.get("variant") or ""))
                    if new_id:
                        candidate["id"] = new_id

            errors = L.validate_entry(candidate, existing_ids=live_ids,
                                      exclude_id=entry.get("id"))
            if errors:
                problems.append("{}: {}".format(
                    entry.get("id") or "(no id)", errors[0]))
                continue
            staged.append((pos, candidate))
            if candidate.get("id") != entry.get("id"):
                live_ids.discard(entry.get("id"))
                live_ids.add(candidate.get("id"))

        if not staged:
            messagebox.showwarning(
                APP_TITLE, "Nothing could be applied:\n\n- " +
                "\n- ".join(problems[:8]), parent=self)
            return

        if problems:
            plural = "y is" if len(problems) == 1 else "ies are"
            if not messagebox.askyesno(
                    APP_TITLE,
                    "{} entr{} invalid:\n\n- {}\n\nApply the remaining "
                    "{} valid change(s) anyway?".format(
                        len(problems), plural, "\n- ".join(problems[:8]),
                        len(staged)), parent=self):
                return

        changes = []
        for pos, candidate in staged:
            old_obj = app.entries[pos]
            clean = L.build_clean_entry(candidate)
            app.entries[pos] = clean
            changes.append({
                "pos_hint": pos,
                "ref_before": old_obj, "copy_before": app._deepcopy(old_obj),
                "ref_after": clean, "copy_after": app._deepcopy(clean),
            })

        desc = "Find & Replace: {} entr{} updated ({})".format(
            len(changes), "y" if len(changes) == 1 else "ies",
            self._field_label())
        app._record_op("find_replace", desc, changes)
        app.dirty = True
        app._mark_audit_dirty()
        app.populate_tree()
        app._reload_editor_if_affected({c["pos_hint"] for c in changes})
        app.refresh_spell_vocab()
        app._autosave()
        try:
            app._notify_db_changed()
        except Exception:                            # noqa: BLE001
            pass
        app.status_var.set(desc + ". Remember to Save As to keep them.")
        self.apply_lbl.configure(text="Applied.")
        self._scan()   # re-scan: applied rows now match zero remaining changes
