"""
curve_compare.py -- "Compare Curves" dialog (feature 3.4 from the audit
report): overlay any two database entries' frequency-response curves on one
plot for a quick visual sanity check.

Read-only: this dialog never mutates the database. It reuses fr_plot's
existing CurvePlot widget verbatim, and the exact same file-resolution /
parsing / averaging path the Editor's own "FR PREVIEW" card already uses
(fr_analysis.resolve_under_root -> fr_plot.get_curve_points ->
fr_plot.normalized -> fr_plot.average), so a future change to any of that
math only ever needs to happen in one place.

Reachable from:
  - Tools menu -> "Compare Curves..." (blank picker)
  - the Audit tab's "Possible Duplicate" rows, via the right-click context
    menu's "Compare Curves..." action (pre-fills both sides of the pair --
    the single most useful sanity check when triaging a duplicate finding:
    do these two products actually measure the same?)

Offline; no new dependencies.
"""

import tkinter as tk
from tkinter import ttk

import db_logic as L
import fr_plot
import theme

try:
    import fr_analysis as FA
except Exception:                                  # noqa: BLE001
    FA = None


def _label_for(entry):
    """'Brand Model Variant  --  id', unique because ids are unique and
    every label embeds its own id."""
    parts = [entry.get("brand") or "", entry.get("model") or ""]
    variant = entry.get("variant") or ""
    if variant:
        parts.append(variant)
    name = " ".join(p for p in parts if p).strip() or entry.get("id") or "(unnamed)"
    return "{}  \u2014  {}".format(name, entry.get("id") or "?")


class CurveCompareDialog(tk.Toplevel):
    """Pick any two entries and overlay their (averaged, if multiple files
    are linked) measurement curves on one CurvePlot."""

    def __init__(self, master, app, id_a=None, id_b=None):
        super().__init__(master)
        self.app = app
        self.title("Compare Curves")
        self.configure(background=theme.BG_MAIN)
        self.transient(master)
        self.minsize(720, 520)
        self.geometry("860x600")

        self._by_label = {}
        for e in sorted(app.entries, key=L.sort_key):
            self._by_label[_label_for(e)] = e.get("id")
        labels = list(self._by_label.keys())

        outer, card = theme.make_card(self)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(card, text="\u2248  COMPARE CURVES",
                  style="CardHeader.TLabel").pack(anchor="w", padx=8,
                                                  pady=(8, 2))
        ttk.Label(
            card,
            text="Pick any two entries to overlay their measurement curves "
                 "(averaged across every linked file). Read-only -- handy "
                 "for sanity-checking a possible-duplicate finding, or just "
                 "comparing two products.",
            style="Card.TLabel", foreground=theme.TEXT_DIM,
            wraplength=760, justify="left").pack(anchor="w", padx=8,
                                                 pady=(0, 8))

        pick_row = ttk.Frame(card, style="CardFlat.TFrame")
        pick_row.pack(fill="x", padx=8, pady=(0, 4))
        pick_row.columnconfigure(0, weight=1)
        pick_row.columnconfigure(2, weight=1)

        self.var_a = tk.StringVar()
        self.var_b = tk.StringVar()
        self.combo_a = ttk.Combobox(pick_row, textvariable=self.var_a,
                                    values=labels, state="normal")
        self.combo_b = ttk.Combobox(pick_row, textvariable=self.var_b,
                                    values=labels, state="normal")
        ttk.Label(pick_row, text="A", style="Card.TLabel",
                  foreground=theme.ACCENT_BLUE).grid(row=0, column=0,
                                                     sticky="w")
        self.combo_a.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(pick_row, text="\u21c4 Swap",
                   command=self._swap).grid(row=1, column=1, padx=4)
        ttk.Label(pick_row, text="B", style="Card.TLabel",
                  foreground=theme.ACCENT_ORANGE).grid(row=0, column=2,
                                                       sticky="w")
        self.combo_b.grid(row=1, column=2, sticky="ew", padx=(8, 0))
        # F-5: A-B difference trace. Styled like the Editor tab's FR
        # PREVIEW toggles (Toggle.TCheckbutton) so the dialog matches the
        # rest of the app; disabled until BOTH sides have usable curves.
        self.diff_var = tk.BooleanVar(value=False)
        self.diff_btn = ttk.Checkbutton(
            pick_row, text="A \u2212 B", style="Toggle.TCheckbutton",
            variable=self.diff_var, command=self._refresh)
        self.diff_btn.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 0))

        self.info_lbl = ttk.Label(card, text="", style="Card.TLabel",
                                  foreground=theme.TEXT_DIM,
                                  wraplength=760, justify="left")
        self.info_lbl.pack(anchor="w", padx=8, pady=(6, 4))

        self.plot = fr_plot.CurvePlot(card, height=360)
        self.plot.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        btns = ttk.Frame(card, style="CardFlat.TFrame")
        btns.pack(fill="x", padx=8, pady=(0, 10))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        for combo in (self.combo_a, self.combo_b):
            combo.bind("<<ComboboxSelected>>", lambda e: self._refresh())
            combo.bind("<Return>", lambda e: self._refresh())
            combo.bind("<FocusOut>", lambda e: self._refresh())

        # preselect if ids were provided (e.g. from a duplicate-pair audit row)
        self._preselect(self.var_a, id_a)
        self._preselect(self.var_b, id_b)

        self.bind("<Escape>", lambda _e: self.destroy())
        self._refresh()
        self.update_idletasks()
        self.grab_set()

    def _preselect(self, var, target_id):
        if not target_id:
            return
        for label, eid in self._by_label.items():
            if eid == target_id:
                var.set(label)
                return

    def _swap(self):
        a, b = self.var_a.get(), self.var_b.get()
        self.var_a.set(b)
        self.var_b.set(a)
        self._refresh()

    def _entry_for_label(self, label):
        eid = self._by_label.get(label)
        if not eid:
            return None
        for e in self.app.entries:
            if e.get("id") == eid:
                return e
        return None

    def _curve_for(self, entry):
        """Resolve + parse + normalize + average every file linked to
        `entry`. Returns (points, n_files_used, n_files_total)."""
        root = self.app.get_data_root()
        files = list(entry.get("files") or []) if entry else []
        if not root or not files or FA is None:
            return [], 0, len(files)
        norms = []
        for rel in files:
            try:
                full = FA.resolve_under_root(root, rel)
            except Exception:                       # noqa: BLE001
                continue
            pts = fr_plot.get_curve_points(full)
            if not pts:
                continue
            norm = fr_plot.normalized(pts)
            if norm:
                norms.append(fr_plot.smooth_octaves(norm))
        if not norms:
            return [], 0, len(files)
        if len(norms) == 1:
            return norms[0], 1, len(files)
        return fr_plot.average(norms), len(norms), len(files)

    def _diff_curve(self, pts_a, pts_b):
        """F-5: A-B on A's frequency grid over the overlap band. Returns
        [(freq, a_minus_b)] (raw dB difference -- NOT re-normalized, so
        the trace shows both tonal deviation AND level mismatch between
        the two datasets), or [] when the ranges do not overlap."""
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
        out = []
        for f, a in zip(fa, sa):
            b = fr_plot._interp_at(fb, sb, f)
            out.append((f, a - b))
        return out

    def _refresh(self):
        entry_a = self._entry_for_label(self.var_a.get())
        entry_b = self._entry_for_label(self.var_b.get())
        if entry_a is None and entry_b is None:
            self.plot.set_data(
                [], msg="Pick two entries above to compare their curves.")
            self.info_lbl.configure(text="")
            return

        pal = fr_plot.palette()
        series = []
        info_parts = []
        curves = {}                # "A"/"B" -> normalized points
        for entry, color, tag in ((entry_a, pal[0], "A"), (entry_b, pal[1], "B")):
            if entry is None:
                continue
            pts, used, total = self._curve_for(entry)
            label = "{}: {}".format(tag, entry.get("id") or "?")
            if not pts:
                info_parts.append(
                    "{} -- no usable measurement data ({} file(s) "
                    "linked).".format(label, total))
                continue
            info_parts.append(
                "{} -- {} of {} file(s) averaged.".format(label, used, total))
            series.append({"name": label, "pts": pts, "color": color,
                           "width": 4})
            curves[tag] = pts

        # F-5: A-B difference trace on top of the two curves
        if self.diff_var.get() and "A" in curves and "B" in curves:
            diff = self._diff_curve(curves["A"], curves["B"])
            if diff:
                series.append({
                    "name": "A \u2212 B", "pts": diff,
                    "color": theme.ACCENT_PURPLE,
                    "width": 3, "dash": (5, 3)})
                info_parts.append(
                    "A \u2212 B: max {:+.1f} dB \u00b7 mean {:+.1f} dB".format(
                        max(d for _, d in diff),
                        sum(d for _, d in diff) / len(diff)))

        # F-5: the toggle only does anything with BOTH curves present
        try:
            self.diff_btn.state(
                ["!disabled"] if len(curves) == 2 else ["disabled"])
        except Exception:
            pass

        self.info_lbl.configure(
            text="   |   ".join(info_parts) if info_parts else
            "Pick two entries above to compare their curves.")
        if not series:
            self.plot.set_data([], msg="Neither entry has usable measurement data.")
            return
        self.plot.set_data(series)
