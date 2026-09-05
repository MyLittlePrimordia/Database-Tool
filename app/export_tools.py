"""
export_tools.py -- database export helpers shared by the Export tab.

compress_database_bytes(): gzip the canonical JSON serialization of a
list of entries straight to <dest>/database.json.gz (the fixed output
name the IEM Tool web app expects -- never renamed, whatever the source
file was called).

split_entries(): token-budgeted chunking of a top-level JSON array into
*_chunk_N.json files, ported from the standalone JSON Chunk Splitter
(chars/4 heuristic, stale-chunk cleanup).

Both accept either an in-memory entry list (unsaved edits included) or a
path to any .json file, so exports never force a Save As round-trip.
"""

import json
import os
import gzip

import db_logic as L

GZ_NAME = "database.json.gz"          # FIXED name - the main app looks for it
DEFAULT_MAX_TOKENS = 50000


# --------------------------------------------------------------------------
# Source resolution
# --------------------------------------------------------------------------
def resolve_source(entries=None, external_path=None):
    """Return the entry list to export. `external_path` wins when given;
    otherwise `entries` is used as-is (may contain unsaved edits)."""
    if external_path:
        loaded, _notes = L.load_database(external_path)
        return loaded, external_path
    if entries is None or not isinstance(entries, list):
        raise ValueError("No database is loaded and no file was selected.")
    if not entries:
        raise ValueError("The loaded database has no entries.")
    return entries, None


def serialize_canonical(entries):
    """Canonical bytes exactly as Save As would write them: entries sorted
    by Brand -> Model -> Variant with schema fields in fixed order."""
    ordered = [L.build_clean_entry(e) for e in
               sorted(entries, key=L.sort_key)]
    text = json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# Compress
# --------------------------------------------------------------------------
def compress_to_gz(entries=None, external_path=None, dest_dir=None):
    """Write <dest_dir>/database.json.gz from the resolved source.
    Returns (gz_path, raw_size, gz_size)."""
    src_entries, src_path = resolve_source(entries, external_path)
    raw = serialize_canonical(src_entries)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    else:
        dest_dir = (os.path.dirname(os.path.abspath(src_path))
                    if src_path else os.getcwd())
    gz_path = os.path.join(dest_dir, GZ_NAME)

    payload = gzip.compress(raw, compresslevel=9)
    # M-8: atomic + durable (fsync before replace) so a compression run
    # interrupted at the worst moment can never leave a truncated .gz
    # for the IEM Tool to download.
    L.write_bytes_atomic(gz_path, payload)
    return gz_path, len(raw), len(payload)


# --------------------------------------------------------------------------
# Split
# --------------------------------------------------------------------------
def count_tokens(item, indent=None):
    """Rough dependency-free token estimate (~4 chars/token). Pass the same
    `indent` used when writing so the estimate reflects the actual bytes on
    disk (chunks are written with indent=2; compact JSON underestimates by
    ~30-40%)."""
    return max(1, len(json.dumps(item, ensure_ascii=False, indent=indent)) // 4)


def split_into_chunks(entries=None, external_path=None,
                      output_dir=None, max_tokens=DEFAULT_MAX_TOKENS, log=print):
    """Split into <output_dir>/<base>_chunk_N.json files under a token
    budget. base is 'database' for in-memory sources, else the source
    file's basename. Stale chunks for the same base are removed first.
    Returns (chunk_count, total_entries)."""
    src_entries, src_path = resolve_source(entries, external_path)

    if not isinstance(src_entries, list):
        raise ValueError("Database must be a top-level JSON array [].")

    # Canonicalize exactly like the gzip export so both exporters always
    # agree on schema order and never leak unknown fields into chunks
    # (in-memory sources may carry unsaved edits; external files were
    # already cleaned by load_database). Sorted by Brand -> Model -> Variant
    # like compress_to_gz, so AI-audit batches per chunk are coherent.
    src_entries = [L.build_clean_entry(e) for e in
                   sorted(src_entries, key=L.sort_key)]

    filename_only = (os.path.splitext(os.path.basename(src_path))[0]
                     if src_path else "database")
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    elif src_path:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(src_path)),
                                  "chunks")
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = "chunks"
        os.makedirs(output_dir, exist_ok=True)

    # Clear stale chunk files from a previous run for this database name.
    prefix = "{}_chunk_".format(filename_only)
    for file in os.listdir(output_dir):
        if file.startswith(prefix) and file.endswith(".json"):
            try:
                os.remove(os.path.join(output_dir, file))
            except OSError:
                pass

    total_entries = len(src_entries)
    log("Entries found: {:,}".format(total_entries))
    log("Splitting...")

    number = 1
    current_chunk = []
    current_chunk_jsons = []
    current_tokens = 0
    chunks_created = 0
    entries_written = 0

    def flush():
        nonlocal number, current_chunk, current_chunk_jsons, current_tokens
        nonlocal chunks_created, entries_written
        if not current_chunk:
            return
        out_name = "{}_chunk_{}.json".format(filename_only, number)
        out_path = os.path.join(output_dir, out_name)
        # Reuse per-item serializations to avoid re-encoding each entry.
        # json.dump(chunk) would re-serialize every item; joining cached
        # indent=2 strings is equivalent and saves ~50% of JSON work.
        # Each item_json is indent=2 for a top-level object; inside a list
        # it needs 2 extra spaces.
        def _indent_item(s):
            return "\n".join(("  " + line) if line else line for line in s.split("\n"))
        # M-8: write chunks atomically too -- an interrupted split run
        # used to leave a truncated chunk that a later AI audit batch
        # would happily ingest.
        text = "[\n" + ",\n".join(
            _indent_item(j) for j in current_chunk_jsons) + "\n]\n"
        L.write_text_atomic(out_path, text)
        log("  Created: {}  ({} entries, ~{} tokens)".format(
            out_name, len(current_chunk), current_tokens))
        chunks_created += 1
        entries_written += len(current_chunk)
        number += 1
        current_chunk = []
        current_chunk_jsons = []
        current_tokens = 0

    for index, item in enumerate(src_entries):
        # indent=2 matches flush()'s serialization so the token budget is
        # computed against what actually lands on disk. Cache the dump to
        # reuse in flush().
        item_json = json.dumps(item, ensure_ascii=False, indent=2)
        item_tokens = max(1, len(item_json) // 4)
        if current_chunk and current_tokens + item_tokens > max_tokens:
            flush()
        current_chunk.append(item)
        current_chunk_jsons.append(item_json)
        current_tokens += item_tokens
        if index % 200 == 0:
            log("  Processing {}/{} | Current chunk: ~{} tokens".format(
                index + 1, total_entries, current_tokens))

    flush()

    log("-" * 40)
    log("Chunks created: {}".format(chunks_created))
    log("Entries written: {}".format(entries_written))
    log("Output: {}".format(output_dir))
    return chunks_created, total_entries
