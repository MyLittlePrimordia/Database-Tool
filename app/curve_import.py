"""
curve_import.py -- the "Import Curves" notebook tab.

Replaces the standalone Curve Converter app and wires it directly into
the editor's data-folder workflow:

  1. Queue raw .txt/.csv measurements (drag & drop anywhere on the
     window, or Select files...).
  2. Pick/create the destination sub-folder inside the linked data
     folder - exactly where database.json's "files" entries point.
  3. Review the planned outputs (pairs auto-detected + averaged,
     filenames editable before anything is written).
  4. Convert, optionally auto-linking the new files into the entry
     currently being edited.
"""

import os
import sys
import threading
import queue as _q

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import curve_logic as CL
import db_logic as L
import fr_plot
import theme


def _sanitize_folder(name):
    cleaned = CL.sanitize_filename(name)
    return "" if cleaned == "curve.txt" else cleaned


class CurveImportPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self._font = theme.font(13)
        self.files = []            # queued raw input paths
        self._plans = []           # GroupPlans for the current queue
        self._busy = False
        self._queue_rows = []      # [(row, label)] for click-to-preview
        self._sel_queue = -1       # index into self.files shown in the plot
        self._queue_sel = set()    # multi-select (normcased paths)
        self._queue_anchor = None  # shift-range anchor row index
        self._plan_rows = []       # [(row, bg_widgets)] for click-to-preview
        self._sel_plan = -1        # index into self._plans shown in the plot

        # Scrollable body: the five stacked cards are taller than the tab
        # viewport on normal screens, which clipped the PLANNED OUTPUTS card
        # and its "Convert & Save" button below the fold with no way to
        # reach them (same scrollable-canvas pattern as the Editor tab).
        self.scroll_canvas = tk.Canvas(self, background=theme.BG_MAIN,
                                       highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical",
                            command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=vsb.set)
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.scroll_inner = ttk.Frame(self.scroll_canvas, style="TFrame")
        self.scroll_canvas.create_window(
            (0, 0), window=self.scroll_inner, anchor="nw", tags="inner")

        def _on_configure(_event=None):
            self.scroll_canvas.configure(
                scrollregion=self.scroll_canvas.bbox("all"))
            self.scroll_canvas.itemconfigure(
                "inner", width=self.scroll_canvas.winfo_width())
        self.scroll_inner.bind("<Configure>", _on_configure)
        self.scroll_canvas.bind("<Configure>", _on_configure)
        # Mouse-wheel scrolling when the pointer is over the canvas
        # background (child widgets keep their own wheel behavior).
        self.scroll_canvas.bind(
            "<MouseWheel>",
            lambda e: self.scroll_canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"))
        self.scroll_canvas.bind(
            "<Button-4>", lambda e: self.scroll_canvas.yview_scroll(-1, "units"))
        self.scroll_canvas.bind(
            "<Button-5>", lambda e: self.scroll_canvas.yview_scroll(1, "units"))

        body = self.scroll_inner
        queue_wrap = self._build_queue_section(body)
        self._build_curve_preview_card(queue_wrap)
        self._build_options_and_outputs_card(body)
        self._build_log(body)

        self.refresh_data_root()

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    def _build_queue_section(self, host):
        wrap_outer, wrap = theme.make_card(host)
        wrap_outer.pack(fill="x", padx=10, pady=(10, 6))

        header = ttk.Frame(wrap, style="CardFlat.TFrame")
        header.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(header, text="\U0001F4C8  RAW CURVE FILES (.txt / .csv)",
                  style="CardHeader.TLabel").pack(side="left")
        ttk.Button(header, text="Select files...", command=self._pick_files).pack(
            side="right", padx=2)
        ttk.Button(header, text="Clear all", style="Danger.TButton",
                   command=self._clear_files).pack(side="right", padx=2)
        self.remove_sel_btn = ttk.Button(header, text="Remove selected",
                                         style="Danger.TButton",
                                         command=self._remove_queued_selected,
                                         state="disabled")
        self.remove_sel_btn.pack(side="right", padx=2)

        # Native OS drag & drop is a Windows-only ctypes hook (win_drop);
        # on macOS/Linux it silently no-ops, so say "Select files..." there
        # instead of pointing users at a gesture that does nothing.
        if sys.platform.startswith("win"):
            drop_hint = "Drag measurement files here to convert them."
        else:
            drop_hint = ("Drag & drop is Windows-only -- use 'Select "
                         "files...' instead.")
        drop_label = ttk.Label(wrap, text=drop_hint,
                  style="Card.TLabel", foreground=theme.TEXT_DIM,
                  justify="left")
        drop_label.pack(anchor="w", fill="x", padx=8, pady=(0, 4))
        theme.bind_dynamic_wrap(drop_label, source=wrap)

        # scrollable row list (same pattern as the converter's queue list)
        outer = tk.Frame(wrap, bg=theme.BG_INPUT, highlightthickness=1,
                         highlightbackground=theme.BG_CARD, highlightcolor=theme.BG_CARD)
        outer.pack(fill="x", padx=8, pady=(0, 8))
        self.queue_canvas = tk.Canvas(outer, bg=theme.BG_INPUT, highlightthickness=0,
                                      height=96)
        vsb = ttk.Scrollbar(outer, orient="vertical",
                            command=self.queue_canvas.yview)
        self.queue_inner = tk.Frame(self.queue_canvas, bg=theme.BG_INPUT)
        self.queue_inner.bind("<Configure>", lambda e:
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all")))
        self._queue_window = self.queue_canvas.create_window(
            (0, 0), window=self.queue_inner, anchor="nw")
        self.queue_canvas.bind("<Configure>", lambda e:
            self.queue_canvas.itemconfigure(self._queue_window, width=e.width))
        self.queue_canvas.configure(yscrollcommand=vsb.set)
        self.queue_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.count_var = tk.StringVar(value="0 file(s) queued")
        ttk.Label(wrap, textvariable=self.count_var, style="Card.TLabel",
                  foreground=theme.TEXT_DIM).pack(anchor="e", padx=8, pady=(0, 6))
        return wrap

    def add_paths(self, paths, quiet=False):
        """Add paths to the queue (deduped). Called by drag & drop and by
        the file browser."""
        accepted = [p for p in paths if CL.is_curve_file(p)]
        skipped = len(paths) - len(accepted)
        if not accepted:
            if not quiet and paths:
                self._log("[SKIPPED] Drop contained no .txt/.csv files.")
            return False
        before = len(self.files)
        # normcase: Windows paths differing only in case are the SAME file;
        # without it the same drop could be queued twice (e.g. "C:\A.txt"
        # vs "c:\a.txt" via different shells)
        self.files = sorted(set(self.files) |
                            {os.path.normcase(os.path.abspath(p)) for p in accepted})
        self._refresh_queue()
        added = len(self.files) - before
        note = "  ({} non-curve item(s) ignored)".format(skipped) if skipped else ""
        self._log("[OK] Queued {} curve file(s) ({} total){}".format(
            added, len(self.files), note))
        return True

    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="Select raw curve files",
            filetypes=[("Curve files", "*.txt *.csv"), ("All files", "*.*")])
        if paths:
            self.add_paths(paths)

    def _clear_files(self):
        self.files = []
        self._refresh_queue()

    def _refresh_queue(self):
        for w in self.queue_inner.winfo_children():
            w.destroy()
        n = len(self.files)
        self.count_var.set("{} file(s) queued".format(n))
        self._queue_rows = []
        self._sel_queue = -1
        # prune multi-select to paths still queued
        alive = set(self.files)
        self._queue_sel &= alive
        self._update_remove_sel_btn()
        if not n:
            tk.Label(self.queue_inner,
                     text="No files queued yet - use \"Select files...\" or "
                          "drag & drop .txt/.csv files onto this window",
                     bg=theme.BG_INPUT, fg=theme.TEXT_DIM, font=self._font, anchor="w",
                 ).pack(fill="x", padx=10, pady=8)
            self._set_plans([])
            self._show_preview_empty()
            return
        for i, path in enumerate(self.files):
            self._add_queue_row(path, i)
        self._render_queue_selection()
        self._set_plans(CL.plan_groups(self.files))

    def _add_queue_row(self, path, idx):
        row = tk.Frame(self.queue_inner, bg=theme.BG_INPUT)
        row.pack(fill="x")
        lbl = tk.Label(row, text=os.path.basename(path), bg=theme.BG_INPUT,
                       fg=theme.TEXT_MAIN, font=self._font, anchor="w",
                       cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=2)
        x_btn = tk.Label(row, text="\u2715", bg=theme.BG_INPUT, fg=theme.TEXT_DIM,
                         font=theme.font(13, "bold"), cursor="hand2", padx=4)
        x_btn.pack(side="right", padx=(4, 10))
        x_btn.bind("<Button-1>", lambda e, p=path: self._remove_file(p))
        x_btn.bind("<Enter>", lambda e, b=x_btn: b.configure(fg=theme.ACCENT_RED))
        x_btn.bind("<Leave>", lambda e, b=x_btn: b.configure(fg=theme.TEXT_DIM))
        # clicking a queued file previews its raw curve; Ctrl+click toggles
        # it in the multi-select, Shift+click selects a range (plain click
        # selects just that row AND previews it)
        for w in (row, lbl):
            w.bind("<Button-1>",
                   lambda e, i=idx: self._select_queue_row(
                       i,
                       "toggle" if e.state & 0x0004 else
                       ("range" if e.state & 0x0001 else "only")))
        self._queue_rows.append((row, lbl))

    def _remove_file(self, path):
        try:
            self.files.remove(path)
        except ValueError:
            pass
        self._refresh_queue()

    def _remove_queued_selected(self):
        if not self._queue_sel:
            return
        self.files = [p for p in self.files if p not in self._queue_sel]
        self._queue_sel.clear()
        self._refresh_queue()

    def _update_remove_sel_btn(self):
        try:
            self.remove_sel_btn.state(
                ["!disabled"] if self._queue_sel else ["disabled"])
        except Exception:
            pass

    # selection-highlight color derived from the live palette (the old
    # hardcoded #26304a was unreadable under light themes)
    def _queue_sel_bg(self):
        return theme.blend(theme.BG_INPUT, theme.ACCENT_BLUE, 0.35)

    # ------------------------------------------------------------------
    # Curve preview (live plot of the selected queued file / plan)
    # ------------------------------------------------------------------
    def _build_curve_preview_card(self, host):
        # Folded into the RAW CURVE FILES card (no separate card of its own)
        # -- one fewer top-level section for a new user to parse. `host` is
        # that card's own inner frame.
        ttk.Separator(host, orient="horizontal").pack(fill="x", padx=8, pady=(2, 4))
        head = ttk.Frame(host, style="CardFlat.TFrame")
        head.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(head, text="Preview", style="CardHeader.TLabel").pack(side="left")
        self.preview_info = tk.Label(head, text="", bg=theme.BG_CARD, fg=theme.TEXT_DIM,
                                     font=theme.font(12), anchor="w")
        self.preview_info.pack(side="left", padx=(12, 0))
        ttk.Label(head, text="click a queued file or a planned-output row",
                  style="Card.TLabel", foreground=theme.TEXT_DIM).pack(side="right")
        self.preview_plot = fr_plot.CurvePlot(host, height=130)
        self.preview_plot.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self._show_preview_empty()

    def _show_preview_empty(self):
        self.preview_plot.clear(
            msg="Click a queued file above (or a planned-output row below) "
                "to see its curve")
        self.preview_info.configure(text="")

    def _select_queue_row(self, idx, how="only"):
        """Queue-row selection with multi-select support.

        how='only'  -> single-select + preview (plain click)
        how='toggle'-> Ctrl+click: add/remove this row from the selection
        how='range' -> Shift+click: extend from the last anchor row"""
        if not (0 <= idx < len(self.files)):
            return
        path = self.files[idx]
        if how == "toggle":
            if path in self._queue_sel:
                self._queue_sel.discard(path)
            else:
                self._queue_sel.add(path)
            self._queue_anchor = idx
        elif how == "range" and getattr(self, "_queue_anchor", None) is not None:
            # Explorer-style: shift+click REPLACES the selection with the
            # anchor..clicked range
            lo, hi = sorted((self._queue_anchor, idx))
            self._queue_sel = {self.files[k] for k in range(lo, hi + 1)}
        else:
            self._queue_sel.clear()
            self._queue_sel.add(path)
            self._queue_anchor = idx
            self._sel_plan = -1
            self._render_plan_selection()
            self._preview_queue_idx(idx)
        self._sel_queue = idx if len(self._queue_sel) == 1 else -1
        self._render_queue_selection()
        self._update_remove_sel_btn()

    def _preview_queue_idx(self, idx):
        pts = fr_plot.get_curve_points(self.files[idx])
        norm = fr_plot.normalized(pts) if pts else []
        norm = fr_plot.smooth_octaves(norm) if norm else []
        name = os.path.basename(self.files[idx])
        if norm:
            self.preview_plot.set_data(
                [{"name": name, "pts": norm,
                  "color": fr_plot.palette()[0], "width": 4}])
            self.preview_info.configure(
                text="RAW  \u00b7  {}".format(name),
                fg=theme.TEXT_DIM)
        else:
            self.preview_plot.clear(msg="No parsable data rows in this file")
            self.preview_info.configure(
                text="RAW  \u00b7  {}  (no data)".format(name), fg=theme.ACCENT_RED)

    def _select_plan_row(self, idx):
        if not (0 <= idx < len(self._plans)):
            return
        plan = self._plans[idx]
        self._sel_plan = idx
        self._sel_queue = -1
        self._render_queue_selection()
        self._render_plan_selection()
        series = []
        names = []
        for k, (p, _role) in enumerate(plan.sources):
            pts = fr_plot.get_curve_points(p)
            norm = fr_plot.normalized(pts) if pts else []
            norm = fr_plot.smooth_octaves(norm) if norm else []
            if not norm:
                continue
            series.append({"name": os.path.basename(p), "pts": norm,
                           "color": fr_plot.palette()[k % len(fr_plot.palette())],
                           "width": 4})
            names.append(os.path.basename(p))
        badge = ("PAIR\u2192AVG" if plan.averaged else
                 ("SOLO" if len(plan.sources) == 1 else "GROUP"))
        label = "{}  \u00b7  {}".format(badge, plan.suggested_name)
        if not series:
            self.preview_plot.clear(msg="No parsable data rows in this plan")
            self.preview_info.configure(text=label + "  (no data)",
                                        fg=theme.ACCENT_RED)
            return
        avg = None
        if plan.averaged and len(series) == 2:
            # exact same math convert_plan uses: mean on raw SPL, first grid
            # (smoothed for display, like every preview curve)
            pa = fr_plot.get_curve_points(plan.sources[0][0])
            pb = fr_plot.get_curve_points(plan.sources[1][0])
            avg = fr_plot.average_raw(pa, pb) if pa and pb else None
            avg = fr_plot.smooth_octaves(avg) if avg else None
            if avg:
                names.append("average")
        self.preview_plot.set_data(series, avg=avg)
        self.preview_info.configure(
            text=label + "   \u00b7   " + "  +  ".join(names[:4])
            + ("  (+{})".format(len(names) - 4) if len(names) > 4 else ""),
            fg=theme.TEXT_DIM)

    def _render_queue_selection(self):
        sel_bg = self._queue_sel_bg()
        for i, (row, lbl) in enumerate(self._queue_rows):
            if not (0 <= i < len(self.files)):
                continue
            bg = sel_bg if self.files[i] in self._queue_sel else theme.BG_INPUT
            row.configure(bg=bg)
            lbl.configure(bg=bg)

    def _render_plan_selection(self):
        sel_bg = self._queue_sel_bg()
        for i, widgets in enumerate(self._plan_rows):
            bg = sel_bg if i == self._sel_plan else widgets[-1]
            for w in widgets[:-1]:
                try:
                    w.configure(bg=bg)
                except Exception:      # ttk.Checkbutton has no bg option
                    pass

    # ------------------------------------------------------------------
    # Destination + Planned Outputs -- one shared card (used to be two),
    # since Destination is really just a few settings, not its own
    # first-class step in the workflow.
    # ------------------------------------------------------------------
    def _build_options_and_outputs_card(self, host):
        card_outer, card = theme.make_card(host)
        card_outer.pack(fill="x", padx=10, pady=6)
        self._build_destination_section(card)
        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=8, pady=(2, 4))
        self._build_output_section(card)

    def _build_destination_section(self, host):
        sub = ttk.Frame(host, style="CardFlat.TFrame")
        sub.pack(fill="x", padx=8, pady=(8, 2))

        ttk.Label(sub, text="Data folder:", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        self.root_var = tk.StringVar(value="(not set)")
        root_lbl = ttk.Label(sub, textvariable=self.root_var, style="Card.TLabel",
                  foreground=theme.ACCENT_BLUE, justify="left")
        root_lbl.grid(row=0, column=1, columnspan=2,
                      sticky="w", padx=4, pady=(0, 2))
        # long absolute paths used to force this column wide enough to show
        # the whole string on one line, which pushed "Change..." (and the
        # rest of the row) past the tab's right edge -- wrap instead.
        theme.bind_dynamic_wrap(root_lbl, source=sub, min_wrap=160)
        ttk.Button(sub, text="Change...", command=self._change_root,
                   width=10).grid(row=0, column=3, sticky="e", pady=(0, 2))

        ttk.Label(sub, text="Sub-folder:", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=2)
        self.subfolder_var = tk.StringVar(value="")
        self.subfolder_combo = ttk.Combobox(sub, textvariable=self.subfolder_var)
        self.subfolder_combo.grid(row=1, column=1, columnspan=2, sticky="ew",
                                  padx=4, pady=2)
        self.subfolder_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_exists_markers())
        ttk.Button(sub, text="\u2795 New Folder...", style="Blue.TButton",
                   command=self._new_subfolder, width=15).grid(
                       row=1, column=3, sticky="ew", pady=2)
        # column 1 absorbs the extra width instead of the folder-path label
        # or the sub-folder combo forcing the row wider than the tab.
        sub.columnconfigure(1, weight=1)

        # Which entry converted files link to is now chosen per planned
        # output (see the "Link to" dropdown next to each row below,
        # default "Current Entry") -- this used to be one global checkbox
        # that could only ever target the entry open in the Editor.
        self.openfolder_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            sub, text="Open output folder after converting",
            variable=self.openfolder_var, style="Card.TCheckbutton"
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self.remove_source_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            sub, text="Remove source files after conversion",
            variable=self.remove_source_var, style="Card.TCheckbutton"
        ).grid(row=2, column=2, columnspan=2, sticky="e", pady=(6, 2))

        hint = ("\u26a0 Set the data folder first (File \u25b8 Set Data Folder...)"
                if not self.app.get_data_root() else "")
        self.dest_hint = ttk.Label(sub, text=hint, style="Card.TLabel",
                                   foreground=theme.ACCENT_ORANGE,
                                   justify="left")
        self.dest_hint.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))
        theme.bind_dynamic_wrap(self.dest_hint, source=sub)

    def _data_dir(self):
        root = self.app.get_data_root()
        if not root:
            return None
        if os.path.basename(os.path.normpath(root)).lower() == "data":
            return root
        d = os.path.join(root, "data")
        return d if os.path.isdir(d) else None

    def refresh_data_root(self):
        """Re-read the app's data root; rebuild the sub-folder dropdown."""
        data_dir = self._data_dir()
        root = self.app.get_data_root()
        if root and not data_dir:
            self.root_var.set("{}  (no 'data' subfolder yet - one will be created)"
                              .format(root))
        elif root:
            self.root_var.set(root)
        else:
            self.root_var.set("(not set)")
        values = []
        if data_dir and os.path.isdir(data_dir):
            values = sorted(
                e for e in os.listdir(data_dir)
                if os.path.isdir(os.path.join(data_dir, e)) and not e.startswith("."))
        current = self.subfolder_var.get()
        self.subfolder_combo.configure(values=[""] + values)
        if current and current not in values and current != "":
            self.subfolder_combo.configure(values=[current] + [""] + values)
        self.dest_hint.configure(
            text="" if data_dir else
            "\u26a0 Set the data folder first (File \u25b8 Set Data Folder...)")
        self._refresh_exists_markers()

    def _change_root(self):
        self.app.set_data_folder()
        self.refresh_data_root()

    def _new_subfolder(self):
        data_dir = self._data_dir()
        if not data_dir:
            messagebox.showwarning(
                "Database Tool",
                "Set the data folder first (File > Set Data Folder...).")
            return
        name = simpledialog.askstring(
            "New source folder",
            "Name of the new sub-folder inside:\n{}\n\n"
            "(Tip: use the brand name, e.g. 'MOONDROP')".format(data_dir),
            parent=self)
        if not name:
            return
        name = _sanitize_folder(name.strip())
        if not name:
            messagebox.showwarning("Database Tool", "That folder name is not usable.")
            return
        path = os.path.join(data_dir, *[
            seg for seg in (_sanitize_folder(s) for s in name.split("/")) if seg])
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Database Tool", "Could not create folder:\n{}".format(e))
            return
        rel_seg = os.path.relpath(path, data_dir).replace("\\", "/")
        self.subfolder_var.set(rel_seg)
        self.refresh_data_root()
        self._log("[OK] Created folder: {}".format(path))
        # make the new folder visible to the file linker immediately
        self._poke_file_linker()

    # ------------------------------------------------------------------
    # Planned outputs (folded into the same card as Destination above)
    # ------------------------------------------------------------------
    def _build_output_section(self, host):
        card = host
        # The action button lives in the section HEADER (not below the
        # output list) so it stays visible even before any scrolling -- it
        # used to sit under the list at the bottom of the tab and was
        # routinely clipped off-screen entirely.
        header = ttk.Frame(card, style="CardFlat.TFrame")
        header.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(header, text="\U0001F4DD  PLANNED OUTPUTS",
                  style="CardHeader.TLabel").pack(side="left")
        self.convert_btn = ttk.Button(header, text="Convert & Save",
                                      style="Accent.TButton",
                                      command=self._run_convert)
        self.convert_btn.pack(side="right")
        self.convert_status = tk.StringVar(value="")
        convert_status_lbl = ttk.Label(header, textvariable=self.convert_status,
                  style="Card.TLabel", foreground=theme.ACCENT_BLUE,
                  justify="right")
        convert_status_lbl.pack(side="right", padx=12)
        theme.bind_dynamic_wrap(convert_status_lbl, source=header, min_wrap=100)
        rename_hint = ttk.Label(card,
                  text="Rename the file, pick who it links to \u00b7 \u26a0 marks "
                       "files that already exist.",
                  style="Card.TLabel", foreground=theme.TEXT_DIM, justify="left")
        rename_hint.pack(anchor="w", fill="x", padx=8)
        theme.bind_dynamic_wrap(rename_hint, source=card)

        outer = tk.Frame(card, bg=theme.BG_CARD, highlightthickness=1,
                         highlightbackground=theme.BG_CARD, highlightcolor=theme.BG_CARD)
        outer.pack(fill="x", padx=8, pady=(4, 8))
        self.preview_canvas = tk.Canvas(outer, bg=theme.BG_CARD, highlightthickness=0,
                                        height=150)
        vsb = ttk.Scrollbar(outer, orient="vertical",
                            command=self.preview_canvas.yview)
        self.preview_inner = tk.Frame(self.preview_canvas, bg=theme.BG_CARD)
        self.preview_inner.bind("<Configure>", lambda e:
            self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all")))
        self._preview_window = self.preview_canvas.create_window(
            (0, 0), window=self.preview_inner, anchor="nw")
        self.preview_canvas.bind("<Configure>", lambda e:
            self.preview_canvas.itemconfigure(self._preview_window, width=e.width))
        self.preview_canvas.configure(yscrollcommand=vsb.set)
        self.preview_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _set_plans(self, plans):
        self._plans = plans
        self._name_vars = []
        self._include_vars = []
        self._exists_labels = []
        self._plan_rows = []
        self._link_vars = []
        self._sel_plan = -1
        for w in self.preview_inner.winfo_children():
            w.destroy()
        self._render_queue_selection()   # queue highlight survives rebuild

        if not plans:
            tk.Label(self.preview_inner,
                     text="Planned outputs appear here once files are queued.",
                     bg=theme.BG_CARD, fg=theme.TEXT_DIM, font=self._font, anchor="w",
                     ).pack(fill="x", padx=10, pady=8)
            self._update_convert_state()
            return

        # "Link to" dropdown choices: same list for every row, built once
        # here rather than per-row. "Current Entry" first (the default --
        # whatever's open in the Editor right now), then every database
        # entry sorted alphabetically by its human-readable "Brand Model
        # [Variant]" label (not the underscored id) so users can quickly
        # find the entry they want to redirect a file to.
        entries_sorted = sorted(self.app.entries, key=L.sort_key)
        link_labels = ["Current Entry"] + [L.format_entry_label(e) for e in entries_sorted]
        self._entry_link_map = {L.format_entry_label(e): e.get("id") for e in entries_sorted}

        taken = {}
        for i, plan in enumerate(plans):
            name = plan.suggested_name
            stem, ext = os.path.splitext(name)
            final = "{}{}".format(stem, ext.lower())
            count = taken.get(final.lower(), 0)
            if count:
                final = "{} ({}){}".format(stem, count + 1, ext.lower())
            taken[final.lower()] = count + 1
            display_name = os.path.splitext(final)[0]

            row_bg = theme.BG_CARD if i % 2 == 0 else \
                theme.blend(theme.BG_CARD, theme.TEXT_MAIN, 0.04)
            row = tk.Frame(self.preview_inner, bg=row_bg, cursor="hand2")
            row.pack(fill="x")

            inc_var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(row, variable=inc_var, bg=row_bg, activebackground=row_bg,
                                highlightthickness=0, bd=0, command=self._update_convert_state)
            cb.pack(side="left", padx=(8, 2), pady=4)

            badge_text, badge_fg = (
                ("PAIR \u2192 AVG", theme.ACCENT_GREEN) if plan.averaged
                else (("SOLO", theme.TEXT_DIM) if len(plan.sources) == 1
                      else ("GROUP", theme.ACCENT_ORANGE)))
            badge = tk.Label(row, text=badge_text, bg=row_bg, fg=badge_fg,
                             font=theme.font(12, "bold"), width=11, anchor="center",
                             cursor="hand2")
            badge.pack(side="left", padx=(0, 6))

            name_var = tk.StringVar(value=display_name)
            entry = tk.Entry(row, textvariable=name_var, bg=theme.BG_INPUT, fg=theme.TEXT_MAIN,
                              insertbackground=theme.TEXT_MAIN, relief="flat",
                              font=self._font, width=22)
            entry.pack(side="left", padx=(0, 2), pady=3, ipady=2)
            entry.bind("<FocusOut>", lambda e, v=name_var: self._on_name_edit(v))
            entry.bind("<Return>", lambda e, v=name_var: self._on_name_edit(v))

            ext_lbl = tk.Label(row, text=".txt", bg=row_bg, fg=theme.TEXT_DIM,
                                 font=self._font, anchor="w")
            ext_lbl.pack(side="left", padx=(0, 6))

            link_var = tk.StringVar(value=link_labels[0])
            link_combo = ttk.Combobox(row, textvariable=link_var, values=link_labels,
                                      state="readonly", width=20, font=self._font)
            link_combo.pack(side="left", padx=(0, 6), pady=3)

            exists_lbl = tk.Label(row, text="", bg=row_bg, fg=theme.ACCENT_RED,
                                  font=theme.font(12, "bold"))
            exists_lbl.pack(side="left", padx=(0, 6))

            srcs = "  +  ".join(plan.source_names)
            if len(srcs) > 40:
                srcs = srcs[:39] + "\u2026"
            src_lbl = tk.Label(row, text=srcs, bg=row_bg, fg=theme.TEXT_DIM,
                               font=theme.font(12), anchor="w",
                               cursor="hand2")
            src_lbl.pack(side="left", fill="x", expand=True, padx=(0, 8))

            # clicking a planned-output row previews its curve(s); the Entry,
            # Checkbutton and "Link to" combobox keep their own click behavior
            for w in (row, badge, ext_lbl, exists_lbl, src_lbl):
                w.bind("<Button-1>", lambda e, i=i: self._select_plan_row(i))

            self._name_vars.append(name_var)
            self._include_vars.append((inc_var, plan))
            self._exists_labels.append(exists_lbl)
            self._link_vars.append(link_var)
            self._plan_rows.append((row, cb, badge, ext_lbl, exists_lbl, src_lbl, row_bg))

        self._refresh_exists_markers()
        self._update_convert_state()

    def _planned_target(self, name_value):
        """Full absolute path for a planned output name in the chosen
        sub-folder (or straight inside data/ when no sub-folder picked)."""
        data_dir = self._data_dir()
        if not data_dir:
            return None
        sub = _sanitize_folder(self.subfolder_var.get().strip())
        stem = os.path.splitext(CL.sanitize_filename(name_value.strip()))[0]
        clean_name = "{}.txt".format(stem)
        parts = [seg for seg in sub.split("/") if seg]
        return os.path.join(data_dir, *parts, clean_name) if parts else \
            os.path.join(data_dir, clean_name)

    def _relative_for_db(self, full_path):
        """Forward-slash relative path as stored in database.json entries."""
        root = self.app.get_data_root()
        if not root:
            return None
        base = os.path.dirname(root) \
            if os.path.basename(os.path.normpath(root)).lower() == "data" else root
        try:
            return os.path.relpath(full_path, base).replace("\\", "/")
        except ValueError:
            return None

    def _refresh_exists_markers(self):
        if not hasattr(self, "_exists_labels"):
            return
        for exists_lbl, (inc_var, _plan), name_var in zip(
                self._exists_labels, self._include_vars, self._name_vars):
            target = self._planned_target(name_var.get())
            exists_lbl.configure(
                text="\u26a0 exists" if target and os.path.exists(target) else "")

    def _on_name_edit(self, name_var):
        clean = os.path.splitext(CL.sanitize_filename(name_var.get().strip()))[0]
        if clean != name_var.get():
            name_var.set(clean)
        self._refresh_exists_markers()
        self._update_convert_state()

    def _duplicate_names(self):
        seen = set()
        dupes = set()
        for (inc_var, _plan), name_var in zip(self._include_vars, self._name_vars):
            if not inc_var.get():
                continue
            key = name_var.get().strip().lower()
            if key in seen:
                dupes.add(key)
            seen.add(key)
        return dupes

    def _update_convert_state(self):
        if self._busy:
            self.convert_btn.state(["disabled"])
            return
        data_dir_ok = bool(self._data_dir())
        included = sum(1 for v, _p in getattr(self, "_include_vars", []) if v.get())
        problems = []
        if not self.files:
            problems.append("queue some files first")
        elif not included:
            problems.append("no outputs selected")
        if not data_dir_ok:
            problems.append("set the data folder first")
        if self._duplicate_names():
            problems.append("fix duplicate output names")
        self.convert_btn.state(["disabled"] if problems else ["!disabled"])
        self.convert_status.set("" if not problems else
                                "Cannot convert: " + "; ".join(problems))

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------
    def _run_convert(self):
        if self._busy:
            return
        jobs = []       # [(plan, target_path, link_entry_id_or_None, source_paths)]
        for (inc_var, plan), name_var, link_var in zip(
                self._include_vars, self._name_vars, self._link_vars):
            if not inc_var.get():
                continue
            target = self._planned_target(name_var.get())
            if target:
                link_label = link_var.get()
                link_id = self._entry_link_map.get(link_label)  # None == "Current Entry"
                sources = [p for p, _role in plan.sources]
                jobs.append((plan, target, link_id, sources))
        if not jobs:
            return

        existing = [t for _p, t, _lid, _src in jobs if os.path.exists(t)]
        overwrite = True
        if existing:
            overwrite = messagebox.askyesno(
                "Database Tool",
                "{} planned output file(s) already exist.\n\nOverwrite them?"
                .format(len(existing)))
            if not overwrite:
                jobs = [j for j in jobs if j[1] not in existing]
                if not jobs:
                    self._log("[SKIPPED] Nothing converted - all targets existed.")
                    return

        for _plan, target, _lid, _src in jobs:
            parent = os.path.dirname(target)
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Database Tool",
                                     "Cannot create destination folder:\n{}".format(e))
                return

        self._busy = True
        self.convert_btn.state(["disabled"])
        self.convert_status.set("Converting...")

        result = {"written": [], "written_by_target": []}
        log_q = _q.Queue()

        def work_logged():
            def sink(msg):
                log_q.put(msg)
            written = []
            written_by_target = []   # [(link_id_or_None, [out_paths], [source_paths])]
            try:
                for plan, target, link_id, sources in jobs:
                    out = CL.convert_plan(plan, target, log=sink)
                    written.extend(out)
                    if out:
                        written_by_target.append((link_id, out, sources))
            except Exception as e:  # noqa: BLE001 - last-resort guard so the
                log_q.put("[FAILED]  {}".format(e))  # UI can never stall
            result["written"] = written
            result["written_by_target"] = written_by_target
            log_q.put(None)

        th = threading.Thread(target=work_logged, daemon=True)

        def poll():
            while True:
                try:
                    msg = log_q.get_nowait()
                except _q.Empty:
                    break
                if msg is None:
                    self._finish_convert(result["written"], result["written_by_target"])
                    return
                self._log(msg)
            self.after(80, poll)

        th.start()
        self.after(80, poll)

    def _finish_convert(self, written, written_by_target=()):
        self._busy = False
        ok_count = len(written)
        self._log("-" * 46)
        self._log("Done: {} file(s) written".format(ok_count),
                  tag="ok" if ok_count else None)
        self.convert_btn.state(["!disabled"])
        self._update_convert_state()

        if not written:
            self.convert_status.set("Nothing was written.")
            return

        # make the new files visible to the measurement-file linker
        self._poke_file_linker()

        current_written = []       # rows left on "Current Entry"
        other_targets = {}         # entry_id -> [written paths]
        converted_sources = []     # every source file whose job succeeded
        for link_id, out_paths, source_paths in written_by_target:
            converted_sources.extend(source_paths)
            if link_id is None:
                current_written.extend(out_paths)
            else:
                other_targets.setdefault(link_id, []).extend(out_paths)

        notes = []
        n_linked_current = self._link_written(current_written) if current_written else 0
        if n_linked_current:
            target = self.app.editor.original_id or "(new unsaved entry)"
            notes.append("{} linked to {} (current entry) -- click Save Entry "
                        "to keep them".format(n_linked_current, target))

        n_linked_other, other_ids = self._link_to_other_entries(other_targets)
        if n_linked_other:
            notes.append("{} linked to {} other entr{}".format(
                n_linked_other, len(other_ids), "y" if len(other_ids) == 1 else "ies"))

        if self.remove_source_var.get() and converted_sources:
            removed = self._remove_converted_sources(converted_sources)
            if removed:
                notes.append("{} source file(s) removed from the queue".format(removed))

        linked_note = ("  \u2022 " + "; ".join(notes)) if notes else ""
        dest = os.path.dirname(written[0])
        self.convert_status.set("Wrote {} file(s) to {}{}".format(
            ok_count, dest, linked_note))
        self.app.status_var.set(
            "Imported {} measurement file(s) -> {}".format(ok_count, dest))
        if self.openfolder_var.get() and hasattr(os, "startfile"):
            try:
                os.startfile(dest)  # noqa: S606 - explorer shortcut
            except Exception:  # noqa: BLE001
                pass

    def _remove_converted_sources(self, source_paths):
        """Drop the queued source files that fed a successful conversion
        (opt-in via 'Remove source files after conversion'). Only removes
        entries still present in the queue -- never touches files a plan
        didn't actually convert."""
        norm_sources = {os.path.normcase(os.path.abspath(p)) for p in source_paths}
        before = len(self.files)
        self.files = [p for p in self.files if p not in norm_sources]
        removed = before - len(self.files)
        if removed:
            self._refresh_queue()
        return removed

    def _poke_file_linker(self):
        # One forced background rescan is enough: _invalidate_cache now
        # delegates to the same walker, so calling both used to trigger a
        # duplicate (and formerly UI-blocking) scan.
        try:
            self.app.editor.file_panel.poll_now(force=True)
        except Exception:  # noqa: BLE001
            pass

    def _link_written(self, written):
        """Append successfully written files to the currently edited
        entry's measurement list. Works for entries that only exist in the
        form (not yet saved to the database) -- the links are form state
        until Save Entry commits them. Returns how many were newly linked."""
        rels = []
        for w in written:
            rel = self._relative_for_db(w)
            if rel:
                rels.append(rel)
        if not rels:
            self._log("[SKIPPED] Could not link files: no data folder set "
                      "(File > Set Data Folder...).")
            return 0
        panel = self.app.editor.file_panel
        current = panel.get_files()
        fresh = [r for r in rels if r not in current]
        if not fresh:
            self._log("[SKIPPED] Already linked to this entry: {}".format(
                ", ".join(rels)))
            return 0
        panel.set_files(current + fresh)
        self._log("[OK] Linked to entry form: {}".format(", ".join(fresh)),
                  tag="ok")
        return len(fresh)

    def _link_to_other_entries(self, other_targets):
        """other_targets: {entry_id: [full_output_paths]}. Appends the new
        relative paths to each target entry's saved 'files' list (deduped),
        as ONE undoable history op even when several different entries are
        touched in the same conversion batch -- this is what lets paired/
        averaged measurement files be linked to DIFFERENT entries in a
        single Convert & Save click, not just the entry open in the
        Editor. Returns (n_files_linked, [entry_ids_touched])."""
        if not other_targets:
            return 0, []
        app = self.app
        changes = []
        n_linked = 0
        touched = []
        for entry_id, paths in other_targets.items():
            idx = next((i for i, e in enumerate(app.entries)
                       if e.get("id") == entry_id), None)
            if idx is None:
                self._log("[SKIPPED] Target entry '{}' no longer exists.".format(entry_id))
                continue
            rels = [r for r in (self._relative_for_db(p) for p in paths) if r]
            if not rels:
                self._log("[SKIPPED] Could not link to '{}': no data folder set.".format(entry_id))
                continue
            old = app.entries[idx]
            current_files = list(old.get("files") or [])
            fresh = [r for r in rels if r not in current_files]
            if not fresh:
                self._log("[SKIPPED] Already linked to {}: {}".format(
                    entry_id, ", ".join(rels)))
                continue
            new_entry = app._deepcopy(old)
            new_entry["files"] = current_files + fresh
            app.entries[idx] = new_entry
            changes.append({
                "pos_hint": idx,
                "ref_before": old, "copy_before": app._deepcopy(old),
                "ref_after": new_entry, "copy_after": app._deepcopy(new_entry),
            })
            n_linked += len(fresh)
            touched.append(entry_id)
            self._log("[OK] Linked to {}: {}".format(entry_id, ", ".join(fresh)), tag="ok")
        if changes:
            desc = "Linked {} file(s) to {} other entr{} via Import".format(
                n_linked, len(touched), "y" if len(touched) == 1 else "ies")
            app._record_op("link_files", desc, changes)
            app.dirty = True
            app._mark_audit_dirty()
            app.populate_tree()
            app._autosave()
            app._notify_db_changed()
        return n_linked, touched

    # ------------------------------------------------------------------
    # Log console -- collapsed to a one-line status strip by default; the
    # common case is "did it work, yes/no", not staring at a scrolling
    # console, so the full log is one click away instead of always-open.
    # ------------------------------------------------------------------
    def _build_log(self, host):
        wrap_outer, wrap = theme.make_card(host)
        wrap_outer.pack(fill="x", padx=10, pady=(0, 10))
        header = ttk.Frame(wrap, style="CardFlat.TFrame")
        header.pack(fill="x", padx=8, pady=(6, 2))
        self.log_summary_var = tk.StringVar(value="No conversions yet.")
        summary_lbl = ttk.Label(header, textvariable=self.log_summary_var,
                                style="Card.TLabel", foreground=theme.TEXT_DIM,
                                justify="left")
        summary_lbl.pack(side="left", fill="x", expand=True)
        theme.bind_dynamic_wrap(summary_lbl, source=header, min_wrap=140)
        self.log_toggle_btn = ttk.Button(header, text="Show details \u25be",
                                         width=16, command=self._toggle_log)
        self.log_toggle_btn.pack(side="right")

        self.log_body = ttk.Frame(wrap, style="CardFlat.TFrame")
        # not packed yet -- starts collapsed
        self.log = tk.Text(self.log_body, height=7, bg=theme.BG_INPUT, fg=theme.TEXT_MAIN,
                           insertbackground=theme.TEXT_MAIN, font=self._font,
                           relief="flat", wrap="word", padx=10, pady=8)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.tag_configure("ok", foreground=theme.ACCENT_GREEN)
        self.log.tag_configure("fail", foreground=theme.ACCENT_RED)
        self.log.configure(state="disabled")
        self._log_expanded = False

    def _toggle_log(self):
        self._log_expanded = not self._log_expanded
        if self._log_expanded:
            self.log_body.pack(fill="both", expand=True)
            self.log_toggle_btn.configure(text="Hide details \u25b4")
        else:
            self.log_body.pack_forget()
            self.log_toggle_btn.configure(text="Show details \u25be")

    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")
        # first line of a multi-line message (e.g. "Done: N file(s)
        # written") is usually the meaningful summary line for the
        # collapsed strip; a raw per-file [OK]/[SKIPPED] line is still
        # shown too since it's the most recent activity either way.
        self.log_summary_var.set(text.splitlines()[0] if text else "")
