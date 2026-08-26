"""
tools_panel.py -- the "Export" notebook tab.

Two dead-simple cards that replace the standalone Database Compressor
and JSON Chunk Splitter utilities:

  COMPRESS  -> <folder>/database.json.gz   (fixed output name; the main
              IEM Tool web app looks for exactly that filename)
  SPLIT     -> token-budgeted *_chunk_N.json chunks for AI auditing

Both operate on the CURRENTLY LOADED database straight from memory
(unsaved edits included and flagged), so exporting never forces a Save
As round-trip first.
"""

import os
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import db_logic as L
import export_tools as EX
import ai_prompts
import theme


class ToolsPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self._font = theme.font(13)
        self._source_label = None

        cards = ttk.Frame(self, style="TFrame")
        cards.pack(fill="x", padx=10, pady=(10, 4))
        self._cards_host = cards
        self._compress_outer = self._build_compress_card(cards)
        self._split_outer = self._build_split_card(cards)
        self._cards_stacked = None
        self._cards_resp_after = None
        cards.bind("<Configure>", self._schedule_cards_layout, add="+")

        self._build_source_row()

        # AI working prompts (Add Entry / Audit Database), generated live
        # from the app's own rules so they can never drift out of sync
        self._build_prompts_card()

        # shared result/log console
        log_outer, log_wrap = theme.make_card(self)
        log_outer.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        ttk.Label(log_wrap, text="OUTPUT", style="CardHeader.TLabel").pack(
            anchor="w", padx=8, pady=(6, 2))
        self.log = tk.Text(
            log_wrap, height=9, bg=theme.BG_INPUT, fg=theme.TEXT_MAIN,
            insertbackground=theme.TEXT_MAIN, font=self._font, relief="flat",
            wrap="word", padx=10, pady=8,
        )
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.tag_configure("ok", foreground=theme.ACCENT_GREEN)
        self.log.tag_configure("fail", foreground=theme.ACCENT_RED)
        self.log.configure(state="disabled")

        self.refresh_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_compress_card(self, parent):
        card_outer, card = theme.make_card(parent)
        card_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        ttk.Label(card, text="\U0001F5DC  COMPRESS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        desc = "Compress database into GZIP for faster loading."
        desc_lbl = ttk.Label(card, text=desc, style="Card.TLabel", foreground=theme.TEXT_DIM,
                  justify="left")
        desc_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))
        theme.bind_dynamic_wrap(desc_lbl, source=card)

        ttk.Label(card, text="Destination folder", style="Card.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        self.gz_dir_var = tk.StringVar(value="")
        gz_entry = ttk.Entry(card, textvariable=self.gz_dir_var)
        gz_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(8, 4))
        ttk.Button(card, text="Browse...", command=self._browse_gz_dir).grid(
            row=3, column=2, sticky="ew", padx=(4, 8))

        # flexible filler: absorbs the height difference vs the Split card
        # (which has an extra "Max tokens / chunk" field) so both action
        # buttons pin to the bottom and always line up side-by-side.
        # padx=1 keeps it inside the card's 1px border: ttk frames draw
        # their border UNDER their children, so a zero-padding child
        # gridded sticky="nsew" would paint right over the outline and it
        # would visibly vanish along this band.
        filler = ttk.Frame(card, style="CardFlat.TFrame", height=1)
        filler.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=1)
        card.rowconfigure(4, weight=1)

        self.compress_btn = ttk.Button(card, text="Compress Database",
                                       style="Accent.TButton", width=22,
                                       command=self._run_compress)
        # no sticky: grid centers the button in the card instead of
        # stretching it edge-to-edge; width=22 makes both action buttons
        # (this and "Split Database") pixel-identical for a symmetrical row
        self.compress_btn.grid(row=5, column=0, columnspan=3,
                               padx=8, pady=(8, 2))

        self.gz_result_var = tk.StringVar(value="")
        gz_result_lbl = ttk.Label(card, textvariable=self.gz_result_var, style="Card.TLabel",
                  foreground=theme.ACCENT_GREEN)
        gz_result_lbl.grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        theme.bind_dynamic_wrap(gz_result_lbl, source=card)

        card.columnconfigure(1, weight=1)
        return card_outer

    def _build_split_card(self, parent):
        card_outer, card = theme.make_card(parent)
        card_outer.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        ttk.Label(card, text="\u2702  SPLIT INTO CHUNKS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        desc = "Split database into chunks for AI auditing."
        desc_lbl = ttk.Label(card, text=desc, style="Card.TLabel", foreground=theme.TEXT_DIM,
                  justify="left")
        desc_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))
        theme.bind_dynamic_wrap(desc_lbl, source=card)

        ttk.Label(card, text="Output folder", style="Card.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        self.split_dir_var = tk.StringVar(value="")
        split_entry = ttk.Entry(card, textvariable=self.split_dir_var)
        split_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(8, 4))
        ttk.Button(card, text="Browse...", command=self._browse_split_dir).grid(
            row=3, column=2, sticky="ew", padx=(4, 8))

        ttk.Label(card, text="Max tokens / chunk", style="Card.TLabel").grid(
            row=4, column=0, sticky="w", padx=8, pady=(6, 2))
        self.max_tokens_var = tk.IntVar(value=EX.DEFAULT_MAX_TOKENS)
        # lower bound matches _run_split's validation (>= 100)
        spin = ttk.Spinbox(card, from_=100, to=200000, increment=500,
                           textvariable=self.max_tokens_var, width=10)
        spin.grid(row=5, column=0, sticky="w", padx=8, pady=(0, 2))

        # flexible filler (see the Compress card): keeps the two action
        # buttons aligned on the same visual row. padx=1 keeps it inside
        # the card's 1px border (ttk draws borders UNDER children).
        filler = ttk.Frame(card, style="CardFlat.TFrame", height=1)
        filler.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=1)
        card.rowconfigure(6, weight=1)

        self.split_btn = ttk.Button(card, text="Split Database",
                                    style="Accent.TButton", width=22,
                                    command=self._run_split)
        # centered, same fixed width as "Compress Database" (see above)
        self.split_btn.grid(row=7, column=0, columnspan=3,
                            padx=8, pady=(8, 2))

        self.split_result_var = tk.StringVar(value="")
        split_result_lbl = ttk.Label(card, textvariable=self.split_result_var, style="Card.TLabel",
                  foreground=theme.ACCENT_GREEN)
        split_result_lbl.grid(row=8, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        theme.bind_dynamic_wrap(split_result_lbl, source=card)

        card.columnconfigure(1, weight=1)
        return card_outer

    # ------------------------------------------------------------------
    # Responsive layout: side-by-side when there's room for both cards to
    # show their contents without squeezing, stacked full-width otherwise.
    # A fixed 50/50 split used to force "Browse..." and the folder entry
    # into a card half as wide as the tab at any window size, which is
    # what pushed them past the pane's right edge on a half-screen snap.
    # ------------------------------------------------------------------
    _CARD_MIN_WIDTH = 360   # each card needs roughly this much to lay out cleanly

    def _schedule_cards_layout(self, _event=None):
        if self._cards_resp_after is not None:
            try:
                self.after_cancel(self._cards_resp_after)
            except Exception:
                pass
        self._cards_resp_after = self.after(120, self._layout_cards)

    def _layout_cards(self):
        self._cards_resp_after = None
        w = self._cards_host.winfo_width()
        stacked = w < (self._CARD_MIN_WIDTH * 2 + 20)
        if stacked == self._cards_stacked:
            return
        self._cards_stacked = stacked
        host = self._cards_host
        for c in range(2):
            host.columnconfigure(c, weight=0, uniform="")
        if stacked:
            host.columnconfigure(0, weight=1)
            self._compress_outer.grid(row=0, column=0, sticky="nsew", padx=0, pady=(0, 8))
            self._split_outer.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        else:
            host.columnconfigure(0, weight=1, uniform="toolcard")
            host.columnconfigure(1, weight=1, uniform="toolcard")
            self._compress_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=0)
            self._split_outer.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=0)

        # Force an immediate, full repaint of the host + both shadow-card
        # frames. Without this, jumping straight to full-screen (rather
        # than dragging the border) can leave a stray black square behind:
        # this callback runs 120ms after the window already reached its
        # new size, so the *previous* stacked/side-by-side geometry's 4px
        # offset-shadow (theme.CARD_SHADOW, drawn as a plain background
        # color, not a real widget with its own redraw guarantees) can be
        # left un-erased in a corner until something else repaints it.
        # update_idletasks() alone isn't reliably enough to invalidate the
        # old region on Windows here, so briefly forget/re-show each outer
        # frame to force Tk to redraw the area from scratch.
        for outer in (self._compress_outer, self._split_outer):
            info = outer.grid_info()
            outer.grid_remove()
            outer.update_idletasks()
            outer.grid(**info)
        host.update_idletasks()

    def _build_source_row(self):
        src = ttk.Frame(self, style="TFrame")
        src.pack(fill="x", padx=12, pady=(0, 0))
        ttk.Label(src, text="Source:", style="Dim.TLabel").pack(side="left")
        self._source_label = ttk.Label(src, text="", style="TLabel",
                                       foreground=theme.ACCENT_BLUE)
        self._source_label.pack(side="left", padx=6)

    # ------------------------------------------------------------------
    # AI prompts card (Add Entry / Audit Database)
    # ------------------------------------------------------------------
    def _build_prompts_card(self):
        outer, card = theme.make_card(self)
        outer.pack(fill="x", padx=10, pady=(6, 4))

        ttk.Label(card, text="\U0001F4DD  AI PROMPTS",
                  style="CardHeader.TLabel").pack(anchor="w", padx=8,
                                                  pady=(8, 2))
        hint = ttk.Label(
            card,
            text="Save a prompt as a file, or copy it to your clipboard.",
            style="Card.TLabel", foreground=theme.TEXT_DIM,
            justify="left")
        hint.pack(anchor="w", padx=8, pady=(0, 6))
        theme.bind_dynamic_wrap(hint, source=card)

        # both prompt buttons share one centered row, styled exactly like
        # the Compress/Split action buttons (Accent orange)
        row = ttk.Frame(card, style="CardFlat.TFrame")
        row.pack(fill="x", padx=8, pady=(2, 8))
        # edge columns absorb the spare width so the button group sits
        # centered in the card no matter how wide the tab gets
        row.columnconfigure(0, weight=1)
        row.columnconfigure(5, weight=1)

        for i, (name, filename, builder) in enumerate(ai_prompts.PROMPTS):
            btn = ttk.Button(row, text=name, style="Accent.TButton", width=16,
                             command=lambda fn=filename, b=builder:
                             self._save_prompt(fn, b))
            btn.grid(row=0, column=1 + i * 2,
                     sticky="e", padx=(24 if i else 0, 6), pady=4)
            # square clipboard button with a visual toast: the block flips
            # to green with a checkmark for a second after a successful
            # copy (the color swap is a dedicated style: ttk has no
            # widget-level foreground)
            clip = ttk.Button(row, text="\U0001F4CB", width=3,
                              style="Accent.TButton")
            clip.configure(command=lambda n=name, b=builder, w=clip:
                           self._copy_prompt(n, b, w))
            clip.grid(row=0, column=2 + i * 2, sticky="w", pady=4)

    def _copy_prompt(self, name, builder, btn):
        text = builder()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.status_var.set(
            "Copied the {} prompt to the clipboard ({} characters).".format(
                name, len(text)))
        # visual toast: orange -> green checkmark -> back (the color
        # swap is a dedicated style: ttk has no widget-level foreground)
        try:
            btn.configure(style="Accent.Toast.TButton", text="\u2714")
            btn.after(1000, lambda: btn.configure(
                style="Accent.TButton", text="\U0001F4CB"))
        except Exception:  # noqa: BLE001 - button may be gone at shutdown
            pass

    def _save_prompt(self, filename, builder):
        name = filename.replace("_PROMPT.md", "").replace("_", " ").title()
        path = filedialog.asksaveasfilename(
            title="Save the {} prompt".format(name),
            defaultextension=".md", initialfile=filename,
            filetypes=[("Markdown", "*.md"), ("Text file", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(builder())
        except OSError as e:
            messagebox.showerror(APP_TITLE_REF,
                                 "Could not write the prompt:\n{}".format(e))
            return
        self.app.status_var.set("Saved the {} prompt to {}".format(name, path))

    # ------------------------------------------------------------------
    # State refresh (MainApp calls this when the tab becomes visible and
    # after every load/save/commit so the summary is always current)
    # ------------------------------------------------------------------
    def _default_export_dir(self):
        if getattr(self.app, "data_root", None):
            return self.app.data_root
        if getattr(self.app, "db_path", None):
            return os.path.dirname(os.path.abspath(self.app.db_path))
        return ""

    def refresh_state(self):
        n = len(getattr(self.app, "entries", []) or [])
        has_db = bool(n)
        loaded = self.app.db_path or "(no file yet)"
        dirty_note = "   \u2022 UNSAVED EDITS INCLUDED" if getattr(self.app, "dirty", False) else ""
        if self._source_label is not None:
            self._source_label.configure(text="{} entr{} from {}{}".format(
                n, "y" if n == 1 else "ies", loaded, dirty_note))
        state = ["!disabled"] if has_db else ["disabled"]
        self.compress_btn.state(state)
        self.split_btn.state(state)
        if not self.gz_dir_var.get():
            self.gz_dir_var.set(self._default_export_dir())
        if not self.split_dir_var.get():
            base = self._default_export_dir()
            self.split_dir_var.set(os.path.join(base, "chunks") if base else "")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _browse_gz_dir(self):
        path = filedialog.askdirectory(title="Choose where database.json.gz goes",
                                       initialdir=self.gz_dir_var.get() or ".")
        if path:
            self.gz_dir_var.set(path)

    def _browse_split_dir(self):
        path = filedialog.askdirectory(title="Choose where chunk files go",
                                       initialdir=self.split_dir_var.get() or ".")
        if path:
            self.split_dir_var.set(path)

    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _require_source(self):
        """Ensure app.entries has content; offer to load a file otherwise.
        Loading goes through MainApp._load_from_path so it gets the SAME
        treatment as File > Open: crash-recovery prompt, coercion notes,
        waiver loading, spell vocab, tree rebuild, and a post-load audit --
        swapping app.entries in place here used to leave all of that out."""
        if getattr(self.app, "entries", None):
            return None
        path = filedialog.askopenfilename(
            title="Select a database.json to export",
            filetypes=[("JSON database", "*.json"), ("All files", "*.*")])
        if not path:
            return "cancel"
        self.app._load_from_path(path)
        if not getattr(self.app, "entries", None):
            return "cancel"     # load failed (error dialog already shown)
        self.refresh_state()
        return None

    def _run_in_thread(self, label, work, on_done):
        """Heavy work in a daemon thread; ALL tkinter access happens on the
        main thread via an after()-poll loop (same pattern as run_audit)."""
        self._log("=" * 46)
        self._log(label)
        result = {}

        def _compute():
            try:
                result["value"] = work()
            except Exception as e:  # noqa: BLE001
                import traceback
                result["error"] = "".join(traceback.format_exc(limit=3)).strip()

        th = threading.Thread(target=_compute, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.after(60, poll)
                return
            err = result.get("error")
            if err:
                self._log("ERROR: {}".format(err.splitlines()[-1]), tag="fail")
                messagebox.showerror(APP_TITLE_REF, err)
                return
            on_done(result.get("value"))

        poll()

    def _confirm_audit_unresolved(self):
        """Verify-before-export: the exporters serialize whatever is loaded,
        so warn when the Audit tab still shows unresolved findings instead
        of silently compressing/splitting a database mid-edit."""
        panel = getattr(self.app, "audit_panel", None)
        issues = getattr(panel, "issues", []) if panel is not None else []
        if not issues:
            return True
        try:
            live = [i for i in issues if not panel._waived(i)]
        except Exception:
            live = list(issues)
        if not live:
            return True
        errors = sum(1 for i in live if i.severity == "error")
        warnings = sum(1 for i in live if i.severity == "warning")
        return messagebox.askyesno(
            APP_TITLE_REF,
            "The audit still reports {} unresolved issue(s) "
            "({} error(s), {} warning(s)).\n\nExport the database anyway?".format(
                len(live), errors, warnings))

    def _run_compress(self):
        if self._require_source():
            return
        if not self._confirm_audit_unresolved():
            return
        dest = self.gz_dir_var.get().strip()
        if not dest:
            messagebox.showwarning(APP_TITLE_REF, "Pick a destination folder first.")
            return
        gz_target = os.path.join(dest, EX.GZ_NAME)
        if os.path.exists(gz_target):
            if not messagebox.askyesno(
                    APP_TITLE_REF,
                    "{} already exists.\n\nOverwrite it?".format(gz_target)):
                return
        # Independent copies: the worker thread serializes these while the
        # UI thread may commit edits to the live entry dicts. A shallow
        # list copy used to share the dicts, so an export could capture a
        # half-applied logical state. build_clean_entry returns a fresh,
        # schema-ordered dict with new tags/files lists.
        snapshot = [L.build_clean_entry(e) for e in self.app.entries]
        dirty = getattr(self.app, "dirty", False)
        self.compress_btn.state(["disabled"])

        def work():
            return EX.compress_to_gz(entries=snapshot, dest_dir=dest)

        def done(value):
            self.compress_btn.state(["!disabled"])
            gz_out, raw_size, gz_size = value
            ratio = (1 - gz_size / raw_size) * 100 if raw_size else 0.0
            note = "  (includes unsaved edits)" if dirty else ""
            msg = "{:,} -> {:,} bytes ({:.1f}% smaller){}".format(
                raw_size, gz_size, ratio, note)
            self._log("Wrote {}  [{}]".format(gz_out, msg), tag="ok")
            self.gz_result_var.set(msg)
            self.app.status_var.set("Compressed catalog -> {}".format(gz_out))

        self._run_in_thread("COMPRESSING to {} ...".format(gz_target), work, done)

    def _run_split(self):
        if self._require_source():
            return
        if not self._confirm_audit_unresolved():
            return
        out_dir = self.split_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning(APP_TITLE_REF, "Pick an output folder first.")
            return
        try:
            max_tokens = int(self.max_tokens_var.get())
            if max_tokens < 100:
                raise ValueError
        except Exception:  # noqa: BLE001
            messagebox.showerror(APP_TITLE_REF, "Max tokens must be a number >= 100.")
            return
        # Independent copies -- see _run_compress (worker thread must not
        # serialize dicts the UI thread may mutate mid-run).
        snapshot = [L.build_clean_entry(e) for e in self.app.entries]
        self.split_btn.state(["disabled"])

        def work():
            lines = []
            chunks, total = EX.split_into_chunks(
                entries=snapshot, output_dir=out_dir, max_tokens=max_tokens,
                log=lambda m: lines.append(m))
            return chunks, total, lines

        def done(value):
            self.split_btn.state(["!disabled"])
            chunks, _total, lines = value
            for line in lines:
                tag = "ok" if line.startswith(("Chunks created:", "Entries written:")) else None
                self._log(line, tag=tag)
            self.split_result_var.set("{} chunk file(s) in {}".format(chunks, out_dir))
            self.app.status_var.set(
                "Split database into {} chunk(s) -> {}".format(chunks, out_dir))

        self._run_in_thread("SPLITTING into {} ...".format(out_dir), work, done)


# Set by main.py right after import (avoids a circular import for the title).
APP_TITLE_REF = "Database Tool"


def bind_app_title(title):
    global APP_TITLE_REF
    APP_TITLE_REF = title
