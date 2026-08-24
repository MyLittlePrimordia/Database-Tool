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
from theme import (BG_INPUT, TEXT_MAIN, ACCENT_GREEN, ACCENT_BLUE,
                   ACCENT_RED, pick_font_family)


class ToolsPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="TFrame")
        self.app = app
        self._font = (pick_font_family(), 10)
        self._source_label = None

        cards = ttk.Frame(self, style="TFrame")
        cards.pack(fill="x", padx=10, pady=(10, 4))
        self._build_compress_card(cards)
        self._build_split_card(cards)
        cards.columnconfigure(0, weight=1, uniform="toolcard")
        cards.columnconfigure(1, weight=1, uniform="toolcard")

        self._build_source_row()

        # shared result/log console
        log_wrap = ttk.Frame(self, style="Card.TFrame")
        log_wrap.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        ttk.Label(log_wrap, text="OUTPUT", style="CardHeader.TLabel").pack(
            anchor="w", padx=8, pady=(6, 2))
        self.log = tk.Text(
            log_wrap, height=9, bg=BG_INPUT, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, font=self._font, relief="flat",
            wrap="word", padx=10, pady=8,
        )
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.tag_configure("ok", foreground=ACCENT_GREEN)
        self.log.tag_configure("fail", foreground=ACCENT_RED)
        self.log.configure(state="disabled")

        self.refresh_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_compress_card(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        ttk.Label(card, text="\U0001F5DC  COMPRESS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        desc = ("Gzip (level 9) the database for faster website loading.\n"
                "Output is ALWAYS named database.json.gz - the name the "
                "main app looks for.")
        ttk.Label(card, text=desc, style="Card.TLabel", foreground="#8892a8",
                  wraplength=380, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))

        ttk.Label(card, text="Destination folder", style="Card.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        self.gz_dir_var = tk.StringVar(value="")
        gz_entry = ttk.Entry(card, textvariable=self.gz_dir_var)
        gz_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(8, 4))
        ttk.Button(card, text="Browse...", command=self._browse_gz_dir).grid(
            row=3, column=2, sticky="ew", padx=(4, 8))

        self.compress_btn = ttk.Button(card, text="Compress to database.json.gz",
                                       style="Accent.TButton",
                                       command=self._run_compress)
        self.compress_btn.grid(row=4, column=0, columnspan=3, sticky="ew",
                               padx=8, pady=(8, 2))

        self.gz_result_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.gz_result_var, style="Card.TLabel",
                  foreground=ACCENT_GREEN, wraplength=380).grid(
            row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        card.columnconfigure(1, weight=1)

    def _build_split_card(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        ttk.Label(card, text="\u2702  SPLIT INTO CHUNKS", style="CardHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        desc = ("Split the database into AI-context-sized *_chunk_N.json "
                "files for LLM-assisted auditing. Old chunks with the "
                "same name are cleaned up automatically.")
        ttk.Label(card, text=desc, style="Card.TLabel", foreground="#8892a8",
                  wraplength=380, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))

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

        self.split_btn = ttk.Button(card, text="Split into chunks",
                                    style="Accent.TButton",
                                    command=self._run_split)
        self.split_btn.grid(row=6, column=0, columnspan=3, sticky="ew",
                            padx=8, pady=(8, 2))

        self.split_result_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.split_result_var, style="Card.TLabel",
                  foreground=ACCENT_GREEN, wraplength=380).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        card.columnconfigure(1, weight=1)

    def _build_source_row(self):
        src = ttk.Frame(self, style="TFrame")
        src.pack(fill="x", padx=12, pady=(0, 0))
        ttk.Label(src, text="Source:", style="Dim.TLabel").pack(side="left")
        self._source_label = ttk.Label(src, text="", style="TLabel",
                                       foreground=ACCENT_BLUE)
        self._source_label.pack(side="left", padx=6)

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
        The load must go through the SAME state reset as File > Open (tree,
        spell vocab, undo history, editor form, panels) -- swapping
        app.entries in place here used to leave stale history ops and a
        desynced UI behind."""
        if getattr(self.app, "entries", None):
            return None
        path = filedialog.askopenfilename(
            title="Select a database.json to export",
            filetypes=[("JSON database", "*.json"), ("All files", "*.*")])
        if not path:
            return "cancel"
        try:
            loaded, _notes = L.load_database(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE_REF, "Could not load:\n{}".format(e))
            return "cancel"
        app = self.app
        app.entries = loaded
        if not app.db_path:
            app.db_path = path
        if hasattr(app, "data_root"):
            app.data_root = os.path.dirname(os.path.abspath(path))
        app.dirty = False
        app.editing_index = None
        # unsaved-change history cannot refer across databases
        del app.history[:]
        del app.redo_stack[:]
        try:
            app.editor.new_entry()
        except Exception:  # noqa: BLE001 - editor may not exist yet in tests
            pass
        if hasattr(app, "refresh_spell_vocab"):
            app.refresh_spell_vocab()
        if hasattr(app, "populate_tree"):
            app.populate_tree()
        if hasattr(app, "_notify_db_changed"):
            app._notify_db_changed()
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

    def _run_compress(self):
        if self._require_source():
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
        snapshot = list(self.app.entries)
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
        snapshot = list(self.app.entries)
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
