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
        self._build_queue_section(body)
        self._build_curve_preview_card(body)
        self._build_destination_card(body)
        self._build_preview_card(body)
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
        ttk.Button(header, text="Clear all", command=self._clear_files).pack(
            side="right", padx=2)

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
        # clicking a queued file previews its raw curve
        for w in (row, lbl):
            w.bind("<Button-1>", lambda e, i=idx: self._select_queue_row(i))
        self._queue_rows.append((row, lbl))

    def _remove_file(self, path):
        try:
            self.files.remove(path)
        except ValueError:
            pass
        self._refresh_queue()

    # ------------------------------------------------------------------
    # Curve preview (live plot of the selected queued file / plan)
    # ------------------------------------------------------------------
    def _build_curve_preview_card(self, host):
        card_outer, card = theme.make_card(host)
        card_outer.pack(fill="x", padx=10, pady=6)
        head = ttk.Frame(card, style="CardFlat.TFrame")
        head.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(head, text="\U0001F4C9  CURVE PREVIEW",
                  style="CardHeader.TLabel").pack(side="left")
        self.preview_info = tk.Label(head, text="", bg=theme.BG_CARD, fg=theme.TEXT_DIM,
                                     font=theme.font(12), anchor="w")
        self.preview_info.pack(side="left", padx=(12, 0))
        ttk.Label(head, text="click a queued file or a planned-output row",
                  style="Card.TLabel", foreground=theme.TEXT_DIM).pack(side="right")
        self.preview_plot = fr_plot.CurvePlot(card, height=190)
        self.preview_plot.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self._show_preview_empty()

    def _show_preview_empty(self):
        self.preview_plot.clear(
            msg="Click a queued file above (or a planned-output row below) "
                "to see its curve")
        self.preview_info.configure(text="")

    def _select_queue_row(self, idx):
        if not (0 <= idx < len(self.files)):
            return
        self._sel_queue = idx
        self._sel_plan = -1
        for i, (row, lbl) in enumerate(self._queue_rows):
            bg = "#26304a" if i == idx else theme.BG_INPUT
            row.configure(bg=bg)
            lbl.configure(bg=bg)
        self._render_plan_selection()
        pts = fr_plot.get_curve_points(self.files[idx])
        norm = fr_plot.normalized(pts) if pts else []
        name = os.path.basename(self.files[idx])
        if norm:
            self.preview_plot.set_data(
                [{"name": name, "pts": norm,
                  "color": fr_plot.palette()[0], "width": 2}])
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
            if not norm:
                continue
            series.append({"name": os.path.basename(p), "pts": norm,
                           "color": fr_plot.palette()[k % len(fr_plot.palette())],
                           "width": 2})
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
            pa = fr_plot.get_curve_points(plan.sources[0][0])
            pb = fr_plot.get_curve_points(plan.sources[1][0])
            avg = fr_plot.average_raw(pa, pb) if pa and pb else None
            if avg:
                names.append("average")
        self.preview_plot.set_data(series, avg=avg)
        self.preview_info.configure(
            text=label + "   \u00b7   " + "  +  ".join(names[:4])
            + ("  (+{})".format(len(names) - 4) if len(names) > 4 else ""),
            fg=theme.TEXT_DIM)

    def _render_queue_selection(self):
        for i, (row, lbl) in enumerate(self._queue_rows):
            bg = "#26304a" if i == self._sel_queue else theme.BG_INPUT
            row.configure(bg=bg)
            lbl.configure(bg=bg)

    def _render_plan_selection(self):
        for i, widgets in enumerate(self._plan_rows):
            bg = "#26304a" if i == self._sel_plan else widgets[-1]
            for w in widgets[:-1]:
                try:
                    w.configure(bg=bg)
                except Exception:      # ttk.Checkbutton has no bg option
                    pass

    # ------------------------------------------------------------------
    # Destination
    # ------------------------------------------------------------------
    def _build_destination_card(self, host):
        card_outer, card = theme.make_card(host)
        card_outer.pack(fill="x", padx=10, pady=6)

        ttk.Label(card, text="\U0001F4C2  DESTINATION", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 0))

        ttk.Label(card, text="Data folder:", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=8, pady=(6, 2))
        self.root_var = tk.StringVar(value="(not set)")
        root_lbl = ttk.Label(card, textvariable=self.root_var, style="Card.TLabel",
                  foreground=theme.ACCENT_BLUE, justify="left")
        root_lbl.grid(row=1, column=1, columnspan=2,
                      sticky="w", padx=4, pady=(6, 2))
        # long absolute paths used to force this column wide enough to show
        # the whole string on one line, which pushed "Change..." (and the
        # rest of the card) past the tab's right edge -- wrap instead.
        theme.bind_dynamic_wrap(root_lbl, source=card, min_wrap=160)
        ttk.Button(card, text="Change...", command=self._change_root,
                   width=10).grid(row=1, column=3, sticky="e", padx=8, pady=(6, 2))

        ttk.Label(card, text="Sub-folder:", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=8, pady=(4, 2))
        self.subfolder_var = tk.StringVar(value="")
        self.subfolder_combo = ttk.Combobox(card, textvariable=self.subfolder_var)
        self.subfolder_combo.grid(row=2, column=1, columnspan=2, sticky="ew",
                                  padx=4, pady=(4, 2))
        self.subfolder_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_exists_markers())
        ttk.Button(card, text="\u2795 New Folder...", command=self._new_subfolder,
                   width=15).grid(row=2, column=3, sticky="ew", padx=8, pady=(4, 2))
        # column 1 absorbs the extra width instead of the folder-path label
        # or the sub-folder combo forcing the card wider than the tab.
        card.columnconfigure(1, weight=1)

        self.autolink_var = tk.BooleanVar(value=False)
        self.autolink_check = ttk.Checkbutton(
            card, text="Link converted files into the entry being edited",
            variable=self.autolink_var, style="Card.TCheckbutton",
            command=self._autolink_toggled)
        self.autolink_check.grid(row=3, column=0, columnspan=3, sticky="w",
                                 padx=8, pady=(6, 2))

        hint = ("\u26a0 Set the data folder first (File \u25b8 Set Data Folder...)"
                if not self.app.get_data_root() else "")
        self.dest_hint = ttk.Label(card, text=hint, style="Card.TLabel",
                                   foreground=theme.ACCENT_ORANGE,
                                   justify="left")
        self.dest_hint.grid(row=4, column=0, columnspan=4, sticky="w",
                            padx=8, pady=(2, 8))
        theme.bind_dynamic_wrap(self.dest_hint, source=card)

        card.columnconfigure(1, weight=1)

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

    def _autolink_toggled(self):
        self._update_convert_state()

    # ------------------------------------------------------------------
    # Output preview
    # ------------------------------------------------------------------
    def _build_preview_card(self, host):
        card_outer, card = theme.make_card(host)
        card_outer.pack(fill="x", padx=10, pady=6)

        # The action button lives in the CARD HEADER (not below the output
        # list) so it stays visible even before any scrolling -- it used to
        # sit under the list at the bottom of the tab and was routinely
        # clipped off-screen entirely.
        header = ttk.Frame(card, style="CardFlat.TFrame")
        header.pack(fill="x", padx=8, pady=(8, 0))
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
                  text="Rename freely before converting. \u26a0 marks files that already exist.",
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

        taken = {}
        for i, plan in enumerate(plans):
            name = plan.suggested_name
            stem, ext = os.path.splitext(name)
            final = "{}{}".format(stem, ext.lower())
            count = taken.get(final.lower(), 0)
            if count:
                final = "{} ({}){}".format(stem, count + 1, ext.lower())
            taken[final.lower()] = count + 1

            row_bg = theme.BG_CARD if i % 2 == 0 else "#232736"
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

            name_var = tk.StringVar(value=final)
            entry = tk.Entry(row, textvariable=name_var, bg=theme.BG_INPUT, fg=theme.TEXT_MAIN,
                             insertbackground=theme.TEXT_MAIN, relief="flat",
                             font=self._font, width=34)
            entry.pack(side="left", padx=(0, 6), pady=3, ipady=2)
            entry.bind("<FocusOut>", lambda e, v=name_var: self._on_name_edit(v))
            entry.bind("<Return>", lambda e, v=name_var: self._on_name_edit(v))

            exists_lbl = tk.Label(row, text="", bg=row_bg, fg=theme.ACCENT_RED,
                                  font=theme.font(12, "bold"))
            exists_lbl.pack(side="left", padx=(0, 6))

            srcs = "  +  ".join(plan.source_names)
            if len(srcs) > 58:
                srcs = srcs[:57] + "\u2026"
            src_lbl = tk.Label(row, text=srcs, bg=row_bg, fg=theme.TEXT_DIM,
                               font=theme.font(12), anchor="w",
                               cursor="hand2")
            src_lbl.pack(side="left", fill="x", expand=True, padx=(0, 8))

            # clicking a planned-output row previews its curve(s); the Entry
            # and Checkbutton keep their own click behavior
            for w in (row, badge, exists_lbl, src_lbl):
                w.bind("<Button-1>", lambda e, i=i: self._select_plan_row(i))

            self._name_vars.append(name_var)
            self._include_vars.append((inc_var, plan))
            self._exists_labels.append(exists_lbl)
            self._plan_rows.append((row, cb, badge, exists_lbl, src_lbl, row_bg))

        self._refresh_exists_markers()
        self._update_convert_state()

    def _planned_target(self, name_value):
        """Full absolute path for a planned output name in the chosen
        sub-folder (or straight inside data/ when no sub-folder picked)."""
        data_dir = self._data_dir()
        if not data_dir:
            return None
        sub = _sanitize_folder(self.subfolder_var.get().strip())
        clean_name = CL.sanitize_filename(name_value.strip())
        if not clean_name.lower().endswith(".txt"):
            clean_name += ".txt"
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
        clean = CL.sanitize_filename(name_var.get().strip())
        if not clean.lower().endswith(".txt"):
            clean += ".txt"
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
        jobs = []       # [(plan, target_path)]
        for (inc_var, plan), name_var in zip(self._include_vars, self._name_vars):
            if not inc_var.get():
                continue
            target = self._planned_target(name_var.get())
            if target:
                jobs.append((plan, target))
        if not jobs:
            return

        existing = [t for _p, t in jobs if os.path.exists(t)]
        overwrite = True
        if existing:
            overwrite = messagebox.askyesno(
                "Database Tool",
                "{} planned output file(s) already exist.\n\nOverwrite them?"
                .format(len(existing)))
            if not overwrite:
                jobs = [(p, t) for p, t in jobs if t not in existing]
                if not jobs:
                    self._log("[SKIPPED] Nothing converted - all targets existed.")
                    return

        for _plan, target in jobs:
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

        result = {"written": []}
        log_q = _q.Queue()

        def work_logged():
            def sink(msg):
                log_q.put(msg)
            written = []
            try:
                for plan, target in jobs:
                    written.extend(CL.convert_plan(plan, target, log=sink))
            except Exception as e:  # noqa: BLE001 - last-resort guard so the
                log_q.put("[FAILED]  {}".format(e))  # UI can never stall
            result["written"] = written
            log_q.put(None)

        th = threading.Thread(target=work_logged, daemon=True)

        def poll():
            while True:
                try:
                    msg = log_q.get_nowait()
                except _q.Empty:
                    break
                if msg is None:
                    self._finish_convert(result["written"])
                    return
                self._log(msg)
            self.after(80, poll)

        th.start()
        self.after(80, poll)

    def _finish_convert(self, written):
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

        linked_note = ""
        if self.autolink_var.get():
            n_linked = self._link_written(written)
            if n_linked:
                # Say WHICH form received the links: a brand-new entry that
                # has never been saved is a perfectly valid target (the
                # links live in the form until Save Entry commits them).
                target = self.app.editor.original_id or "(new unsaved entry)"
                linked_note = ("  \u2022 {} linked to {} -- click Save Entry "
                               "in the Editor to keep them".format(
                                   n_linked, target))

        dest = os.path.dirname(written[0])
        self.convert_status.set("Wrote {} file(s) to {}{}".format(
            ok_count, dest, linked_note))
        self.app.status_var.set(
            "Imported {} measurement file(s) -> {}".format(ok_count, dest))
        if hasattr(os, "startfile"):
            try:
                os.startfile(dest)  # noqa: S606 - explorer shortcut
            except Exception:  # noqa: BLE001
                pass

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

    # ------------------------------------------------------------------
    # Log console
    # ------------------------------------------------------------------
    def _build_log(self, host):
        wrap_outer, wrap = theme.make_card(host)
        wrap_outer.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        ttk.Label(wrap, text="CONVERSION LOG", style="CardHeader.TLabel").pack(
            anchor="w", padx=8, pady=(6, 2))
        self.log = tk.Text(wrap, height=7, bg=theme.BG_INPUT, fg=theme.TEXT_MAIN,
                           insertbackground=theme.TEXT_MAIN, font=self._font,
                           relief="flat", wrap="word", padx=10, pady=8)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.tag_configure("ok", foreground=theme.ACCENT_GREEN)
        self.log.tag_configure("fail", foreground=theme.ACCENT_RED)
        self.log.configure(state="disabled")

    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")
