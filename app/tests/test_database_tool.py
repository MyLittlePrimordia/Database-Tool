# -*- coding: utf-8 -*-
"""
F2 -- automated schema-validation & regression test suite.

Pure-logic only (no Tk display needed): every module under test either
has no tkinter dependency (db_logic, curve_logic, fr_analysis, ai_import,
export_tools, spell_logic) or is imported for its pure helpers (main's
ellipsize / entry_matches_query -- importing main.py does not create a
Tk root; that only happens in main()).

Run:  python -m pytest tests -q        (from the app/ folder)
"""

import copy
import json
import gzip as gzmod
import os
import sys
import tempfile
import shutil

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import db_logic as L                      # noqa: E402
import curve_logic as CL                  # noqa: E402
import export_tools as EX                 # noqa: E402
import fr_analysis as FA                  # noqa: E402
import ai_import as AI                    # noqa: E402
import main as MAIN                       # noqa: E402
from main import ellipsize, ellipsize_path, entry_matches_query  # noqa: E402


def tmp_residue(directory, prefix=""):
    """Staging files left behind by an atomic write.

    TEST-001: every writer in db_logic stages to
    `<target>.<pid>.<seq>.tmp` (see _unique_tmp), NOT `<target>.tmp`. The
    old assertions checked `path + ".tmp"`, a name that can never exist, so
    the "no partial file survives" guarantee was never actually verified --
    a regression that stopped cleaning up its staging file would have passed
    CI. Match the real pattern by globbing the directory instead."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.endswith(".tmp") and n.startswith(prefix))


def make_entry(**over):
    base = {
        "id": "moondrop_chu", "brand": "Moondrop", "model": "Chu",
        "variant": "", "year": 2023, "price_usd": 20,
        "driver_type": "DD", "driver_config": "1DD", "impedance": 28,
        "sensitivity": 120, "connector": "2-pin", "form_factor": "IEM",
        "tags": ["Budget", "Warm", "Smooth", "Relaxed"],
        "files": ["data/MOONDROP/CHU.txt"],
    }
    base.update(over)
    return base


@pytest.fixture()
def tmpdb(tmp_path):
    """A temp database path + its cleanup."""
    return str(tmp_path / "database.json")


# ===========================================================================
# H-1: ID building -- Latin stability + non-Latin collision avoidance
# ===========================================================================
class TestBuildId:
    def test_latin_ids_unchanged(self):
        assert L.build_id("Moondrop", "Chu", "III") == "moondrop_chu_iii"
        assert L.build_id("Moon Drop", "Hype 2", "") == "moon_drop_hype_2"
        assert L.build_id("Truthear", "Zero", "Red") == "truthear_zero_red"
        assert L.build_id("Bose", "QuietComfort 35", "II") == \
            "bose_quietcomfort_35_ii"

    def test_nonlatin_components_get_unique_fallback(self):
        sony = u"\u30bd\u30cb\u30fc"
        a = L.build_id(sony, "Chu", "")
        b = L.build_id(sony, "Aria", "")
        assert a and b and a != b
        assert a.startswith("x")

    def test_nonlatin_deterministic(self):
        sony = u"\u30bd\u30cb\u30fc"
        assert L.build_id(sony, "Chu", "") == L.build_id(sony, "Chu", "")

    def test_two_different_nonlatin_brands_do_not_collide(self):
        a = L.build_id(u"\u30bd\u30cb\u30fc", u"\u30d1\u30a4\u30aa\u30f3", "")
        b = L.build_id(u"\u30cf\u30a4\u30d0\u30fc", u"\u30d1\u30a4\u30aa\u30f3", "")
        assert a and b and a != b

    def test_nonlatin_variant(self):
        assert L.build_id("Sony", "WH-1000", u"\u56db").startswith("sony_wh_1000_x")

    def test_id_format_validation(self):
        errs = L.validate_entry(make_entry(id="wrong_id"))
        assert any("does not match" in e for e in errs)


# ===========================================================================
# Price math -- rounding boundaries + tier mapping (Phase 3 verification)
# ===========================================================================
class TestPriceMath:
    def test_round_to_5_boundaries(self):
        assert L.round_price_to_5(0) == 0
        assert L.round_price_to_5(1) == 0
        assert L.round_price_to_5(2) == 0
        assert L.round_price_to_5(2.5) == 5          # half rounds UP
        assert L.round_price_to_5(3) == 5
        assert L.round_price_to_5(7) == 5
        assert L.round_price_to_5(7.5) == 10
        assert L.round_price_to_5(498) == 500
        assert L.round_price_to_5(-3) == 0            # clamped, not -5

    def test_tier_thresholds(self):
        assert L.price_tier_for(0) == "Budget"
        assert L.price_tier_for(99) == "Budget"
        assert L.price_tier_for(100) == "Mid-Tier"
        assert L.price_tier_for(499) == "Mid-Tier"
        assert L.price_tier_for(500) == "Premium"
        assert L.price_tier_for(1499) == "Premium"
        assert L.price_tier_for(1500) == "Flagship"

    def test_tier_basis_agrees_with_rounding(self):
        # the oscillation case from the audit: 498 rounds to 500 -> Premium
        assert L.price_tier_for(L.price_tier_basis(498)) == "Premium"
        assert L.price_tier_for(L.price_tier_basis(500)) == "Premium"

    def test_coerce_int_half_up(self):
        assert L.coerce_int(239.5) == 240
        assert L.coerce_int("239.5") == 240
        assert L.coerce_int(float("nan")) == 0
        assert L.coerce_int("2_023") == 0              # separators rejected

    def test_validate_price_rejects_non_multiple(self):
        errs = L.validate_entry(make_entry(price_usd=22))
        assert any("nearest $5" in e for e in errs)


# ===========================================================================
# F2 regression: the three price-tier consumers must agree. A tier tag
# appended from the RAW price (instead of the $5-rounded price_tier_basis
# that validate_entry uses) made merging any duplicate pair with an
# unrounded winning price (e.g. 498) fail validation with a spurious
# "Price-tier tag ... does not match price" error.
# ===========================================================================
class TestTierConsumerAgreement:
    @staticmethod
    def _entry_with_tier(price, tier):
        return make_entry(price_usd=price,
                          tags=["Warm", "Smooth", "Relaxed", "Fun", tier])

    def test_basis_tier_never_conflicts_with_validator(self):
        """A tier computed through price_tier_basis (what validate_entry,
        MergeDialog and TagSelectorPanel must all use) can never produce a
        tier-mismatch error -- including just-below-boundary prices where
        the rounding crosses a tier edge (98->100, 498->500, 1498->1500)."""
        for p in (0, 20, 98, 99, 100, 103, 497, 498, 499, 500, 1497,
                  1498, 1499, 1500, 10000):
            tier = L.price_tier_for(L.price_tier_basis(p))
            errs = L.validate_entry(self._entry_with_tier(p, tier))
            tier_errs = [e for e in errs if "Price-tier tag" in e]
            assert not tier_errs, "price %s -> tier %s: %s" % (p, tier, tier_errs)

    def test_raw_tier_conflicts_for_unrounded_prices(self):
        """Documents the old bug: skipping price_tier_basis gives prices
        just under a tier boundary the WRONG tier, which the validator
        rejects. If this ever fails, tier boundaries moved and BOTH tier
        consumers need re-checking -- do not just delete this test."""
        for p in (98, 99, 498, 499, 1498, 1499):
            raw_tier = L.price_tier_for(p)          # basis step deliberately skipped
            errs = L.validate_entry(self._entry_with_tier(p, raw_tier))
            assert any("Price-tier tag" in e for e in errs), \
                "price %s: raw tier %s no longer conflicts (rules changed?)" % (p, raw_tier)

    def test_main_py_tier_sites_route_through_basis(self):
        """Static pin on the UI side: every price_tier_for() call in
        main.py must derive its argument from price_tier_basis, so the
        merge dialog and the tag picker can never drift away from what
        validate_entry expects."""
        import re as _re
        import main as MAIN
        with open(MAIN.__file__, encoding="utf-8") as f:
            src = f.read()
        args = _re.findall(r"price_tier_for\s*\(\s*([^)]+)", src)
        assert args, "no price_tier_for() calls found in main.py"
        for arg in args:
            assert "price_tier_basis" in arg, \
                "main.py calls price_tier_for(%s) without price_tier_basis" % arg


# ===========================================================================
# DL-3 / L-1 / L-2 / M-8: atomic persistence
# ===========================================================================
class TestAtomicPersistence:
    def test_save_leaves_no_tmp_and_round_trips(self, tmpdb):
        entries = [L.build_clean_entry(make_entry())]
        L.save_database(tmpdb, entries)
        assert tmp_residue(os.path.dirname(tmpdb), "database.json") == []
        loaded, notes = L.load_database(tmpdb)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "moondrop_chu"
        assert notes == []

    def test_save_is_byte_stable(self, tmpdb):
        """Canonical serialization must be byte-identical across re-saves
        (LF newlines, fixed field order, sorted entries)."""
        e2 = L.build_clean_entry(make_entry(id="aaa_first", brand="Aaa",
                                            model="First", tags=["Budget"]))
        entries = [L.build_clean_entry(make_entry()), e2]
        L.save_database(tmpdb, entries)
        first = open(tmpdb, "rb").read()
        # reload from disk (a second party's view) and save again
        loaded, _ = L.load_database(tmpdb)
        L.save_database(tmpdb, loaded)
        second = open(tmpdb, "rb").read()
        assert first == second

    def test_save_sorts_by_brand_model_variant(self, tmpdb):
        entries = [
            L.build_clean_entry(make_entry(id="zzz", brand="Zzz", model="Z")),
            L.build_clean_entry(make_entry(id="aaa", brand="Aaa", model="A")),
        ]
        ordered = L.save_database(tmpdb, entries)
        assert [e["brand"] for e in ordered] == ["Aaa", "Zzz"]

    def test_backup_is_atomic_and_copies_current(self, tmpdb):
        entries = [L.build_clean_entry(make_entry())]
        L.save_database(tmpdb, entries)
        bak = L.write_database_backup(tmpdb)
        assert bak and os.path.isfile(bak)
        assert tmp_residue(os.path.dirname(bak),
                           os.path.basename(bak)) == []
        assert open(bak, "rb").read() == open(tmpdb, "rb").read()

    def test_curve_write_output_atomic(self, tmp_path):
        out = str(tmp_path / "curve.txt")
        CL.write_output(out, [(20.0, 50.5), (100.0, 52.0)])
        assert tmp_residue(str(tmp_path), "curve.txt") == []
        assert open(out, encoding="utf-8").read().splitlines()[0] == \
            "20.000000\t50.500"

    def test_chunk_split_round_trips(self, tmp_path):
        entries = [L.build_clean_entry(make_entry(
            id="e{}".format(i), brand="B", model="M{}".format(i)))
            for i in range(30)]
        out_dir = str(tmp_path / "chunks")
        n, total = EX.split_into_chunks(
            entries=entries, output_dir=out_dir, max_tokens=100,
            log=lambda m: None)
        assert total == 30 and n >= 2
        loaded = 0
        for fn in sorted(os.listdir(out_dir)):
            assert not fn.endswith(".tmp")
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                loaded += len(json.load(f))
        assert loaded == 30

    def test_gz_export_valid_gzip(self, tmp_path):
        entries = [L.build_clean_entry(make_entry())]
        gz, raw, gzsz = EX.compress_to_gz(entries=entries, dest_dir=str(tmp_path))
        assert gzsz < raw
        with gzmod.open(gz, "rb") as f:
            assert len(json.load(f)) == 1

    def test_write_text_atomic_removes_tmp_on_failure(self, tmp_path):
        # unwritable target (parent dir does not exist) -> the open() fails,
        # so nothing is ever staged. Assert against the REAL staging pattern
        # (TEST-001) rather than a name that cannot occur.
        bad_dir = str(tmp_path / "missing_dir")
        bad = os.path.join(bad_dir, "x.json")
        with pytest.raises(OSError):
            L.write_text_atomic(bad, "data")
        assert not os.path.exists(bad_dir)
        assert tmp_residue(str(tmp_path), "x.json") == []

    def test_write_text_atomic_cleans_up_after_a_mid_write_failure(
            self, tmp_path, monkeypatch):
        """TEST-001: prove the cleanup branch, not just the open() branch.

        Force os.replace to fail AFTER the staging file has been written and
        fsynced -- the case that used to leave `<target>.<pid>.<seq>.tmp`
        sitting next to the database. Also asserts the original file is left
        byte-for-byte intact, which is the property the whole atomic-write
        design exists to provide."""
        target = str(tmp_path / "data.json")
        with open(target, "wb") as f:
            f.write(b'["original"]')

        def boom(src, dst):
            raise L.FileBusyError("simulated: file locked by another program")

        monkeypatch.setattr(L, "replace_atomic", boom)
        with pytest.raises(L.FileBusyError):
            L.write_text_atomic(target, '["replacement"]')

        assert open(target, "rb").read() == b'["original"]'
        assert tmp_residue(str(tmp_path), "data.json") == []

    def test_save_database_does_not_reorder_the_caller_list_on_failure(
            self, tmpdb, monkeypatch):
        """BUG-002 regression guard.

        save_database used to `entries.sort(...)` the caller's list as its
        first statement, so a FAILED save (IEM Tool holding the file) left
        the in-memory list reordered with no tree rebuild -- after which
        every `entry:N` id and editing_index addressed a different record
        and the next "Save Entry" overwrote the wrong product."""
        entries = [
            L.build_clean_entry(make_entry(id="zenn_alpha", brand="Zenn",
                                           model="Alpha")),
            L.build_clean_entry(make_entry(id="aaa_newest", brand="Aaa",
                                           model="Newest")),
        ]
        before = [e["id"] for e in entries]
        assert before != sorted(before), "fixture must start unsorted"

        def boom(*a, **k):
            raise L.FileBusyError("simulated: file locked")

        monkeypatch.setattr(L, "replace_atomic", boom)
        with pytest.raises(L.FileBusyError):
            L.save_database(tmpdb, entries)
        assert [e["id"] for e in entries] == before, \
            "a failed save must not reorder the caller's list"

    def test_save_database_writes_sorted_and_returns_sorted(self, tmpdb):
        """The file on disk is still in sort_key order now that the caller's
        list is no longer sorted in place (BUG-002 fix)."""
        entries = [
            L.build_clean_entry(make_entry(id="zenn_alpha", brand="Zenn",
                                           model="Alpha")),
            L.build_clean_entry(make_entry(id="aaa_newest", brand="Aaa",
                                           model="Newest")),
        ]
        ordered = L.save_database(tmpdb, entries)
        assert [e["brand"] for e in ordered] == ["Aaa", "Zenn"]
        loaded, _ = L.load_database(tmpdb)
        assert [e["brand"] for e in loaded] == ["Aaa", "Zenn"]


# ===========================================================================
# M-4: history v2 (field diffs) with v1 compatibility
# ===========================================================================
class TestHistoryV2:
    def _op(self, changes):
        return {"kind": "fixes", "desc": "d", "when": "12:00:00",
                "changes": changes}

    def test_add_delete_edit_round_trip(self, tmpdb):
        e = L.build_clean_entry(make_entry())
        e_mod = L.build_clean_entry(make_entry(price_usd=25))
        hist = [self._op([
            {"pos_hint": 0, "ref_before": None, "copy_before": None,
             "ref_after": e, "copy_after": copy.deepcopy(e)},
            {"pos_hint": 1, "ref_before": e, "copy_before": copy.deepcopy(e),
             "ref_after": None, "copy_after": None},
            {"pos_hint": 0, "ref_before": e, "copy_before": copy.deepcopy(e),
             "ref_after": e_mod, "copy_after": copy.deepcopy(e_mod)},
        ])]
        L.write_history(tmpdb, hist, [])
        with open(L.history_path_for(tmpdb), encoding="utf-8") as f:
            raw = json.load(f)
        assert raw["version"] == L.HISTORY_VERSION
        # edit changes must be stored as FIELD DIFFS, not full copies
        edit_rec = raw["history"][0]["changes"][2]
        assert set(edit_rec["old"].keys()) == {"price_usd"}
        assert set(edit_rec["new"].keys()) == {"price_usd"}
        # replay
        h2, r2 = L.load_history(tmpdb)
        assert len(h2) == 1 and len(h2[0]["changes"]) == 3
        ch = h2[0]["changes"]
        assert ch[0]["copy_before"] is None          # add
        assert ch[1]["copy_after"] is None           # delete
        assert ch[2]["copy_after"]["price_usd"] == 25
        assert ch[2]["copy_before"]["price_usd"] == 20

    def test_bulk_fix_op_is_compact(self, tmpdb):
        """M-4 acceptance: a 300-entry Fix All must NOT embed 600 full
        entry copies -- edits are field diffs."""
        changes = []
        for i in range(300):
            before = L.build_clean_entry(make_entry(id="e{}".format(i)))
            after = L.build_clean_entry(make_entry(id="e{}".format(i), price_usd=99))
            changes.append({"pos_hint": i, "ref_before": before,
                            "copy_before": before, "ref_after": after,
                            "copy_after": after})
        L.write_history(tmpdb, [self._op(changes)], [])
        size = os.path.getsize(L.history_path_for(tmpdb))
        # 300 one-field diffs serialized must stay far below the v1 cost
        # (300 x 2 full entries ~ 400+ KB); assert a sane ceiling.
        assert size < 100 * 1024, size

    def test_v1_history_still_loads(self, tmpdb):
        e = L.build_clean_entry(make_entry())
        v1 = {"history": [{"kind": "edit", "desc": "d", "when": "t", "changes": [
            {"pos_hint": 0, "copy_before": e, "copy_after": dict(e, price_usd=25)}]}],
            "redo_stack": []}
        os.makedirs(L.backup_dir_for(tmpdb), exist_ok=True)
        with open(L.history_path_for(tmpdb), "w", encoding="utf-8") as f:
            json.dump(v1, f)
        h, r = L.load_history(tmpdb)
        assert len(h) == 1 and len(h[0]["changes"]) == 1

    def test_corrupt_history_is_clean_start(self, tmpdb):
        os.makedirs(L.backup_dir_for(tmpdb), exist_ok=True)
        with open(L.history_path_for(tmpdb), "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert L.load_history(tmpdb) == ([], [])

    def test_cross_session_edit_replay_is_marked_partial(self, tmpdb):
        """C-1 regression: a v2 'edit' change only carries the fields that
        changed (+ id) -- it must be flagged 'partial' so the in-memory
        replay path (main._apply_history_changes) merges those fields onto
        the live entry instead of replacing the whole entry with a husk
        that has lost brand/tags/files/etc."""
        raw_ch = {"action": "edit", "id": "moondrop_chu",
                  "old": {"price_usd": 20}, "new": {"price_usd": 25}}
        ch = L._change_from_v2(raw_ch)
        assert ch["partial"] is True
        assert set(ch["copy_before"].keys()) == {"price_usd", "id"}
        assert set(ch["copy_after"].keys()) == {"price_usd", "id"}
        # Simulate the merge that _apply_history_changes now performs for
        # partial changes (see main.py) and confirm the full entry survives.
        live_entry = L.build_clean_entry(make_entry())
        assert live_entry["price_usd"] == 20
        live_entry.update(copy.deepcopy(ch["copy_after"]))   # simulate redo
        assert live_entry["price_usd"] == 25
        assert live_entry["brand"] == "Moondrop"              # not dropped
        assert live_entry["tags"] == ["Budget", "Warm", "Smooth", "Relaxed"]
        assert live_entry["files"] == ["data/MOONDROP/CHU.txt"]
        live_entry.update(copy.deepcopy(ch["copy_before"]))  # simulate undo
        assert live_entry["price_usd"] == 20
        assert live_entry["brand"] == "Moondrop"

    def test_add_delete_changes_are_not_partial(self, tmpdb):
        """add/delete v2 changes already carry the full entry -- they must
        NOT be marked partial (that would incorrectly merge instead of
        insert/remove)."""
        e = L.build_clean_entry(make_entry())
        add_ch = L._change_from_v2({"action": "add", "after": e})
        del_ch = L._change_from_v2({"action": "delete", "before": e})
        assert not add_ch.get("partial")
        assert not del_ch.get("partial")


# ===========================================================================
# M-5: duplicate-pair emission cap
# ===========================================================================
class TestDupPairCap:
    def _masslinked(self, n):
        es = [L.build_clean_entry(make_entry(
            id="e{}".format(i), brand="B", model="M{}".format(i),
            files=["data/shared.txt"])) for i in range(n)]
        rm = {}
        for i in range(n):
            rm.setdefault("data/shared.txt", []).append(i)
        return L.find_duplicate_pairs(es, rm)

    def test_under_cap_emits_all_pairs(self):
        iss = self._masslinked(5)          # 10 pairs
        pairs = [i for i in iss if getattr(i, "pair_ids", None)]
        assert len(pairs) == 10

    def test_over_cap_collapses_to_summary(self):
        iss = self._masslinked(60)         # 1770 pairs -> summary row
        pairs = [i for i in iss if getattr(i, "pair_ids", None)]
        mass = [i for i in iss if i.code == "dup-masslink"]
        assert len(pairs) == 0
        assert len(mass) == 1


# ===========================================================================
# H-1: id-nonlatin audit finding
# ===========================================================================
class TestIdNonLatinAudit:
    def test_warning_fires_for_nonlatin_brand(self):
        sony = u"\u30bd\u30cb\u30fc"
        es = [L.build_clean_entry(make_entry(
            id=L.build_id(sony, "Chu", ""), brand=sony, model="Chu"))]
        issues = L.run_full_audit(es)
        hits = [i for i in issues if i.code == "id-nonlatin"]
        assert hits and hits[0].severity == "warning"
        assert "Chu" in hits[0].message or "Brand" in hits[0].message

    def test_no_warning_for_latin_entries(self):
        issues = L.run_full_audit([L.build_clean_entry(make_entry())])
        assert not [i for i in issues if i.code == "id-nonlatin"]


# ===========================================================================
# Duplicate File Link audit findings (exact + case-insensitive passes)
# ===========================================================================
class TestDuplicateFileLinkAudit:
    @staticmethod
    def _dup_rows(issues):
        return [i for i in issues
                if i.category == "Duplicate File Link"
                and i.code != "duplicate-file-ci"]

    @staticmethod
    def _ci_rows(issues):
        return [i for i in issues if i.code == "duplicate-file-ci"]

    def test_exact_duplicate_link_warns_per_entry(self):
        es = [L.build_clean_entry(make_entry(
                  id="7hz_zero", brand="7Hz", model="Zero",
                  files=["data/ADEN/7HZ ZERO.txt"])),
              L.build_clean_entry(make_entry(
                  id="moondrop_chu",
                  files=["data/ADEN/7HZ ZERO.txt"]))]
        issues = L.run_full_audit(es)
        rows = self._dup_rows(issues)
        assert len(rows) == 2                       # one row per entry
        assert all(r.severity == "warning" for r in rows)
        assert all(not r.fix for r in rows)         # never auto-fixed
        # no case-insensitive extras: exact pass already covered it
        assert self._ci_rows(issues) == []

    def test_case_variant_duplicate_link_flagged(self):
        # same file on a case-insensitive disk (Windows/macOS), different
        # exact spellings per entry -- must be flagged, not missed
        es = [L.build_clean_entry(make_entry(
                  id="7hz_zero", brand="7Hz", model="Zero",
                  files=["data/ADEN/7HZ ZERO.txt"])),
              L.build_clean_entry(make_entry(
                  id="moondrop_chu",
                  files=["data/aden/7hz zero.txt"]))]
        issues = L.run_full_audit(es)
        rows = self._ci_rows(issues)
        assert len(rows) == 2
        assert all(r.severity == "warning" for r in rows)
        assert all(not r.fix for r in rows)         # non-auto-fixable
        # message names both spellings so the user knows what to unlink
        assert all("7HZ ZERO.txt" in r.message
                   and "7hz zero.txt" in r.message for r in rows)
        # waivers stay per-entry: subject scoped to fold + entry index
        assert len({r.subject for r in rows}) == 2

    def test_unique_files_across_entries_are_clean(self):
        es = [L.build_clean_entry(make_entry(files=["data/ADEN/7HZ ZERO.txt"])),
              L.build_clean_entry(make_entry(
                  id="moondrop_chu",
                  files=["data/MOONDROP/CHU.txt"]))]
        issues = L.run_full_audit(es)
        assert not [i for i in issues if i.category == "Duplicate File Link"]

    def test_no_ci_row_when_only_one_entry_uses_both_spellings(self):
        # fold collision inside a single entry is a repair/dedupe problem,
        # not a cross-entry duplicate link
        es = [L.build_clean_entry(make_entry(
                  files=["data/ADEN/7HZ ZERO.txt",
                         "data/aden/7hz zero.txt"])),
              L.build_clean_entry(make_entry(
                  id="moondrop_chu",
                  files=["data/MOONDROP/CHU.txt"]))]
        issues = L.run_full_audit(es)
        assert self._ci_rows(issues) == []

    def test_ci_row_fires_when_same_fold_spans_three_entries(self):
        es = [L.build_clean_entry(make_entry(
                  id="a_one", brand="A", model="One",
                  files=["data/SRC/A.TXT"])),
              L.build_clean_entry(make_entry(
                  id="b_two", brand="B", model="Two",
                  files=["data/SRC/a.txt"])),
              L.build_clean_entry(make_entry(
                  id="c_three", brand="C", model="Three",
                  files=["Data/src/A.txt"]))]
        issues = L.run_full_audit(es)
        rows = self._ci_rows(issues)
        assert len(rows) == 3                       # one row per entry
        # folded path normalization (backslash, //, ./) feeds the fold too
        assert all("data/src/a.txt" in r.message for r in rows)


# ===========================================================================
# Schema robustness: load_database edge cases
# ===========================================================================
class TestLoadDatabase:
    def test_duplicate_ids_flagged_by_audit(self):
        es = [L.build_clean_entry(make_entry()),
              L.build_clean_entry(make_entry())]
        issues = L.run_full_audit(es)
        assert any(i.category == "Duplicate ID" for i in issues)

    def test_nonfinite_values_reset_with_note(self, tmpdb):
        raw = '[{"id":"x","brand":"B","model":"M","year":2020,' \
              '"price_usd":1e999,"impedance":0,"sensitivity":0}]'
        with open(tmpdb, "w", encoding="utf-8") as f:
            f.write(raw)
        loaded, notes = L.load_database(tmpdb)
        assert loaded[0]["price_usd"] == 0
        assert any("non-finite" in n for n in notes)

    def test_extra_fields_reported_and_dropped(self, tmpdb):
        raw = json.dumps([dict(make_entry(), bogus_field="oops")])
        with open(tmpdb, "w", encoding="utf-8") as f:
            f.write(raw)
        loaded, notes = L.load_database(tmpdb)
        assert "bogus_field" not in loaded[0]
        assert any("bogus_field" in n for n in notes)

    def test_utf8_international_names_round_trip(self, tmpdb):
        e = L.build_clean_entry(make_entry(
            id=L.build_id(u"\u30bd\u30cb\u30fc", "Chu", ""),
            brand=u"\u30bd\u30cb\u30fc", model="Chu"))
        L.save_database(tmpdb, [e])
        loaded, _ = L.load_database(tmpdb)
        assert loaded[0]["brand"] == u"\u30bd\u30cb\u30fc"

    def test_invalid_json_raises_friendly(self, tmpdb):
        with open(tmpdb, "w", encoding="utf-8") as f:
            f.write("{broken")
        with pytest.raises(L.DatabaseLoadError) as exc:
            L.load_database(tmpdb)
        assert "line" in str(exc.value)

    def test_utf8_bom_tolerated(self, tmpdb):
        with open(tmpdb, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps([make_entry()]).encode("utf-8"))
        loaded, _ = L.load_database(tmpdb)
        assert len(loaded) == 1

    def test_gz_database_loads(self, tmpdb):
        entries = [L.build_clean_entry(make_entry())]
        raw = EX.serialize_canonical(entries)
        with open(tmpdb + ".gz", "wb") as f:
            f.write(gzmod.compress(raw))
        loaded, _ = L.load_database(tmpdb + ".gz")
        assert len(loaded) == 1


# ===========================================================================
# L-7: scan cache
# ===========================================================================
class TestScanCache:
    def test_returns_list_and_handles_missing_root(self, tmp_path):
        def reset_memo():
            # the memo deliberately coalesces audit + file-panel walks
            # within ~2 s; the test needs each sub-case to actually walk.
            # L-1: the record lives in ONE list slot as a single tuple.
            L._SCAN_CACHE[:] = [(None, 0.0, None, None)]
        reset_memo()
        files, data_dir = L.scan_data_files(str(tmp_path))
        assert files == [] and data_dir is None
        # data dir present but empty
        reset_memo()
        (tmp_path / "data").mkdir()
        files, data_dir = L.scan_data_files(str(tmp_path))
        assert files == [] and data_dir is not None
        # one .txt file found, forward slashes
        reset_memo()
        (tmp_path / "data" / "a.txt").write_text("20 50\n")
        files, _ = L.scan_data_files(str(tmp_path))
        assert files == ["data/a.txt"]


# ===========================================================================
# Curve parsing / math (Phase 3 + L-8)
# ===========================================================================
class TestCurveLogic:
    def test_date_triple_dropped_when_sweep_corroborates(self, tmp_path):
        p = tmp_path / "d.txt"
        p.write_text("2023,05,01\n20\t50.5\n100\t52\n1000\t48\n")
        freqs = [r[0] for r in CL.parse_curve_file(str(p))]
        assert 2023 not in freqs
        assert 20 in freqs and 1000 in freqs

    def test_all_triple_file_keeps_rows(self, tmp_path):
        p = tmp_path / "t.txt"
        p.write_text("2000 5 10\n2001 6 11\n")
        assert len(CL.parse_curve_file(str(p))) == 2

    def test_sorts_and_dedupes(self, tmp_path):
        p = tmp_path / "s.txt"
        p.write_text("100\t60\n20\t50\n100\t61\n1000\t40\n")
        rows = CL.parse_curve_file(str(p))
        assert [r[0] for r in rows] == [20, 100, 1000]
        assert rows[1][1] == 60          # first occurrence wins

    def test_pair_roles(self):
        assert CL.group_key_and_role("NAME_1") == ("NAME", "1")
        assert CL.group_key_and_role("NAME (2)") == ("NAME", "2")
        assert CL.group_key_and_role("NAME L") == ("NAME", "L")
        assert CL.group_key_and_role("Hype 2") == ("Hype 2", None)

    def test_interp_spl_endpoints_clamp(self):
        assert CL.interp_spl([10, 20], [1, 2], [5, 10, 15, 20, 25]) == \
            [1, 1, 1.5, 2, 2]

    def test_average_group_overlap_only(self):
        a = [(20, 50), (100, 60), (1000, 40)]
        b = [(100, 62), (1000, 42)]      # starts at 100: no 20 Hz
        avg = CL.average_group([a, b])
        assert [f for f, _ in avg] == [100, 1000]
        assert avg[0][1] == 61

    def test_fr_analysis_parse_and_bands(self, tmp_path):
        p = tmp_path / "fr.txt"
        lines = []
        for i in range(20, 200):          # dense 20..199 Hz is not enough;
            lines.append("{}\t{:.2f}".format(i, 50 + i * 0.01))
        # add the bands analyze_points needs
        for f in (500, 900, 1000, 1100, 1500, 2500, 3000, 3500, 6000, 10000):
            lines.append("{}\t{:.2f}".format(f, 50 + f * 0.001))
        p.write_text("\n".join(lines))
        pts = FA.parse_fr_file(str(p))
        assert len(pts) > 20
        res = FA.analyze_points(pts)
        assert res["ok"]
        assert "bass_shelf" in res["metrics"]


# ===========================================================================
# Undo/redo replay logic shape (db_logic format only; the GUI stack is
# exercised end-to-end in the app, this pins the serialization contract)
# ===========================================================================
class TestHistoryReplayContract:
    def test_op_round_trip_preserves_change_kinds(self, tmpdb):
        e = L.build_clean_entry(make_entry())
        op = {"kind": "edit", "desc": "d", "when": "t", "changes": [
            {"pos_hint": 0, "ref_before": e, "copy_before": copy.deepcopy(e),
             "ref_after": None, "copy_after": None}]}
        L.write_history(tmpdb, [op], [])
        (h, redo), _ = L.load_history(tmpdb), None
        assert h[0]["changes"][0]["copy_after"] is None


# ===========================================================================
# ai_import parsing
# ===========================================================================
class TestAiImportParsing:
    def test_plain_array(self):
        out = AI.parse_ai_output(json.dumps([make_entry()]))
        assert len(out["objects"]) == 1 and not out["replacements"]

    def test_search_replace_blocks(self):
        text = "SEARCH:\n" + json.dumps(make_entry()) + \
               "\nREPLACE:\n" + json.dumps(make_entry(price_usd=25))
        out = AI.parse_ai_output(text)
        assert len(out["replacements"]) == 1

    def test_replace_null_is_deletion_marker(self):
        text = "SEARCH:\n" + json.dumps(make_entry()) + "\nREPLACE:\nnull"
        out = AI.parse_ai_output(text)
        assert out["replacements"][0][1] is None

    def test_fenced_prose(self):
        text = "Here you go:\n```json\n" + json.dumps([make_entry()]) + "\n```"
        out = AI.parse_ai_output(text)
        assert len(out["objects"]) == 1

    def test_classify_new_and_changed(self):
        existing = [make_entry()]
        parsed = {"objects": [make_entry(price_usd=25),
                              make_entry(id="other", brand="X", model="Y")],
                  "replacements": []}
        props = AI.classify_against(existing, parsed)
        kinds = sorted(p["action"] for p in props)
        assert kinds == ["changed", "new"]

    def test_apply_rename_collisions_are_rejected_not_duplicated(self):
        """C-2 regression: two CHANGED proposals renamed to the same new id
        must not both be staged.

        TEST-002: this used to be a hand-typed COPY of _apply's staging loop,
        so the real code could regress and CI would stay green. It now calls
        the shared ai_import.stage_candidates() that ImportDialog._apply
        itself calls -- one implementation, one test."""
        live_ids = {"a_1", "a_2"}

        def renamed_entry():
            return make_entry(brand="Common", model="Same",
                               price_usd=10, tags=["Budget", "Warm",
                                                     "Smooth", "Relaxed"])

        included = [
            {"action": "changed", "old": {"id": "a_1"}, "new": renamed_entry()},
            {"action": "changed", "old": {"id": "a_2"}, "new": renamed_entry()},
        ]
        staged, problems, _stats = AI.stage_candidates(
            included, included, {}, lambda pid, p: None, live_ids)
        staged_ids = [c["id"] for _p, c in staged]
        assert len(staged) == 1                 # second one caught, not duplicated
        assert len(problems) == 1
        assert len(staged_ids) == len(set(staged_ids))  # no duplicate ids staged
        # the survivor must hold the SHARED new id, and the vacated id must
        # have been released -- that live_ids bookkeeping is the whole point
        assert staged_ids == [L.build_id("Common", "Same", "")]
        assert "a_1" in live_ids or "a_2" in live_ids

    def test_apply_releases_a_vacated_id_for_a_later_rename(self):
        """The other half of the live_ids bookkeeping, which the C-2 test
        above cannot reach.

        `live_ids.add()` stops a later proposal taking an id this one just
        took. `live_ids.discard()` stops a later proposal being BLOCKED by an
        id this one just vacated. Only the first is covered by the test
        above, so a missing discard() used to pass CI while breaking a
        perfectly valid batch: X renames away, Y renames into X's old id,
        and Y is wrongly rejected as a duplicate.

        Ids here are what build_id actually produces -- with invented ids the
        scenario cannot occur, because validate_entry never sees a collision.
        """
        x_id = L.build_id("Acme", "One", "")
        y_id = L.build_id("Beta", "Two", "")

        def entry(brand, model):
            return make_entry(brand=brand, model=model, price_usd=10,
                              tags=["Budget", "Warm", "Smooth", "Relaxed"])

        included = [
            # X is renamed away, vacating x_id
            {"action": "changed", "old": {"id": x_id},
             "new": entry("Common", "Same")},
            # Y is renamed INTO the id X just vacated -- must be ALLOWED
            {"action": "changed", "old": {"id": y_id},
             "new": entry("Acme", "One")},
        ]
        live_ids = {x_id, y_id}
        staged, problems, _stats = AI.stage_candidates(
            included, included, {}, lambda pid, p: None, live_ids)
        assert problems == [], \
            "a valid rename was rejected: {}".format(problems)
        assert [c["id"] for _p, c in staged] == \
            [L.build_id("Common", "Same", ""), x_id]

    # -- brand spelling normalization (Import Entries) ----------------------
    def test_canonical_spellings_majority_wins(self):
        entries = [make_entry(brand="7HZ", id="7hz_sal_notes"),
                   make_entry(brand="7HZ", model="Timeless",
                              id="7hz_timeless"),
                   make_entry(brand="7Hz", model="Zero",
                              id="7hz_zero")]
        canon = L.brand_canonical_spellings(entries)
        assert canon[L._brand_fold("7HZ")] == "7HZ"

    def test_canonical_spellings_tie_breaks_alphabetically(self):
        entries = [make_entry(brand="Moondrop", id="moondrop_a"),
                   make_entry(brand="MoonDrop", model="Aria",
                              id="moondrop_aria")]
        canon = L.brand_canonical_spellings(entries)
        assert canon[L._brand_fold("moondrop")] == "MoonDrop"

    def test_canonical_spellings_different_names_never_merge(self):
        entries = [make_entry(brand="ISN", id="isn_hype"),
                   make_entry(brand="ISN Audio", model="Hype 2",
                              id="isn_audio_hype_2")]
        canon = L.brand_canonical_spellings(entries)
        assert canon.get(L._brand_fold("ISN")) == "ISN"
        assert canon.get(L._brand_fold("ISN Audio")) == "ISN Audio"
        assert L._brand_fold("ISN") != L._brand_fold("ISN Audio")

    def test_normalize_then_classify_folds_case_only_brand(self):
        """The Import flow normalizes brands BEFORE classify_against, so a
        case-only brand difference stops showing up as a spurious CHANGED
        row and the entry lands under the database's spelling."""
        existing = [make_entry(brand="7HZ", model="Zero", id="7hz_zero")]
        incoming = make_entry(brand="7Hz", model="Zero", id="7hz_zero",
                              price_usd=45)
        parsed = {"objects": [incoming], "replacements": []}
        # TEST-003: call the SAME function ImportDialog._analyze calls.
        # This used to be an inline copy of _normalize_brands' body, so the
        # shipped dialog could regress with CI still green.
        n = AI.normalize_brand_spellings(existing, parsed)
        assert n == 1
        assert incoming["brand"] == "7HZ"
        props = AI.classify_against(existing, parsed)
        assert len(props) == 1
        assert props[0]["action"] == "changed"
        assert all(f != "brand" for f, _o, _n in props[0]["changes"])

    def test_normalize_never_rewrites_unrelated_brands(self):
        """An AI reply for a brand the database doesn't spell at all, or a
        NEARBY but differently-folded name, must pass through untouched."""
        existing = [make_entry(brand="ISN Audio", id="isn_audio_hype")]
        parsed = {"objects": [make_entry(brand="ISN", model="Hype 2",
                                         id="isn_hype_2")],
                  "replacements": []}
        n = AI.normalize_brand_spellings(existing, parsed)
        assert n == 0
        assert parsed["objects"][0]["brand"] == "ISN"


# ===========================================================================
# Import auto-fix (price rounding + tier) + per-field merge
# ===========================================================================
class TestImportAutoFix:
    def test_price_rounding_fixed(self):
        assert AI._normalize_price_value(799) == 800
        assert AI._normalize_price_value(498) == 500
        assert AI._normalize_price_value("799") == 800

    def test_price_leaves_non_roundable_for_validator(self):
        assert AI._normalize_price_value(800) is None
        assert AI._normalize_price_value(0) is None
        assert AI._normalize_price_value(-5) is None
        assert AI._normalize_price_value("799.99") is None
        assert AI._normalize_price_value("7_99") is None
        assert AI._normalize_price_value(float("nan")) is None
        assert AI._normalize_price_value(True) is None

    def test_tier_fixed_to_rounded_price(self):
        tags, changed, _note = AI._fix_tier_tags(
            ["Mid-Tier", "Warm", "Smooth", "Relaxed"], 799)
        assert changed and "Premium" in tags and "Mid-Tier" not in tags
        # position preserved, extras dropped
        tags, changed, _note = AI._fix_tier_tags(
            ["Budget", "Premium", "Warm"], 800)
        assert changed and tags.count("Premium") == 1
        tags, changed, _note = AI._fix_tier_tags(["Warm"], 20)
        assert changed and "Budget" in tags
        _tags, changed, _note = AI._fix_tier_tags(
            ["Premium", "Warm"], 800)
        assert not changed

    def test_799_entry_imports_without_price_errors(self):
        src = make_entry(price_usd=799,
                         tags=["Mid-Tier", "Warm", "Smooth", "Relaxed"])
        cand, notes = AI.normalize_import_entry(src)
        assert cand["price_usd"] == 800
        assert "Premium" in cand["tags"]
        assert notes
        cand["id"] = L.build_id(cand["brand"], cand["model"],
                                cand["variant"])
        errs = L.validate_entry(cand, existing_ids=set())
        assert not [e for e in errs
                    if "nearest $5" in e or "Price-tier tag" in e]

    def test_normalize_is_idempotent_and_field_aware(self):
        src = make_entry(price_usd=799,
                         tags=["Mid-Tier", "Warm", "Smooth", "Relaxed"])
        cand, _notes = AI.normalize_import_entry(src)
        cand2, notes2 = AI.normalize_import_entry(cand)
        assert cand2 == cand and notes2 == []
        untouched, _n = AI.normalize_import_entry(
            src, only_fields={"brand"})
        assert untouched["price_usd"] == 799

    def test_normalize_parsed_bucket_mutates_before_classify(self):
        parsed = {"objects": [make_entry(price_usd=799)],
                  "replacements": []}
        n, _notes = AI._normalize_import_prices(parsed)
        assert n == 1
        assert parsed["objects"][0]["price_usd"] == 800

    def test_field_merge_changed_and_new(self):
        old = make_entry()
        new = make_entry(price_usd=25)
        p = {"action": "changed", "pos": 0, "old": old, "new": new,
             "changes": [("price_usd", 20, 25)]}
        merged = AI.build_field_merged_candidate(p, {"price_usd"})
        assert merged["price_usd"] == 25
        assert AI.build_field_merged_candidate(p, set()) is None
        pn = {"action": "new", "entry": new}
        part = AI.build_field_merged_candidate(pn, {"price_usd"})
        assert part["price_usd"] == 25
        assert part["tags"] == []  # deselected fields fall back to blank
        assert AI.build_field_merged_candidate(pn, set()) is None


# ===========================================================================
# L-5: CJK-aware ellipsization
# ===========================================================================
class TestEllipsize:
    def test_short_unchanged(self):
        assert ellipsize("Moondrop", 20) == "Moondrop"

    def test_latin_truncation(self):
        out = ellipsize("Moondrop Chu III DSP Edition", 12)
        assert out.endswith(u"\u2026")
        assert len(out) <= 12

    def test_cjk_counts_double_width(self):
        # 6 CJK chars == 12 display cells: budget 12 must NOT truncate
        text = u"\u30bd\u30cb\u30fc\u30d8\u30c3\u30c9\u30d5\u30a9\u30f3"
        out = ellipsize(text, 18)          # 9 chars = 18 cells: fits
        assert out == text
        out = ellipsize(text, 9)           # 9 cells = only ~4 chars fit
        assert out.endswith(u"\u2026")

    def test_ellipsize_path_keeps_tail(self):
        out = ellipsize_path("data/SOMEBRAND/averylongmeasurementfile.txt", 30)
        assert out.startswith(u"\u2026")
        assert out.endswith("file.txt")


# ===========================================================================
# Search filter mini-syntax
# ===========================================================================
class TestSearchQuery:
    def test_plain_substring(self):
        assert entry_matches_query(make_entry(), "moondrop")
        assert not entry_matches_query(make_entry(), "sennheiser")

    def test_field_filters(self):
        assert entry_matches_query(make_entry(), "price:<100")
        assert not entry_matches_query(make_entry(), "price:>100")
        assert entry_matches_query(make_entry(), "tag:warm")
        assert entry_matches_query(make_entry(), "ff:iem")
        assert entry_matches_query(make_entry(), "year:=2023")
        assert entry_matches_query(make_entry(), "price:20-30")

    def test_unknown_key_falls_back_to_haystack(self):
        # unknown 'key:' tokens search the WHOLE 'key:value' text as a
        # literal substring of the haystack (documented fallback), so
        # 'custom:moondrop' matches a brand literally containing that
        # string, and a bare 'anything:moondrop' does not.
        assert not entry_matches_query(make_entry(), "anything:moondrop")
        assert entry_matches_query(make_entry(), "id:moondrop")
        assert entry_matches_query(make_entry(brand="custom:note"),
                                  "custom:note")


# ===========================================================================
# Driver parsing / classification
# ===========================================================================
class TestDriverLogic:
    def test_parse_and_classify(self):
        assert L.parse_driver_config("1DD+2BA") == {"DD": 1, "BA": 2}
        assert L.classify_driver({"DD": 1, "BA": 2}) == ("Hybrid", "1DD+2BA")
        assert L.classify_driver({"BA": 4}) == ("BA", "4BA")
        assert L.classify_driver({"DD": 1, "BA": 2, "EST": 2}) == \
            ("Tribrid", "1DD+2BA+2EST")

    def test_canonical_order(self):
        _t, cfg = L.classify_driver({"EST": 2, "DD": 1, "BA": 2})
        assert cfg == "1DD+2BA+2EST"

    def test_unknown_tokens_detected(self):
        assert L.driver_config_unknown_tokens("1DD+2microPE") == ["2microPE"]
        assert L.driver_config_unknown_tokens("1DD+2BA") == []

    def test_case_insensitive_tokens(self):
        assert L.parse_driver_config("1dd+2ba") == {"DD": 1, "BA": 2}


# ===========================================================================
# Tag rules
# ===========================================================================
class TestTagRules:
    def test_conflicts(self):
        assert L.tag_conflicts({"V-Shaped", "U-Shaped"})
        assert L.tag_conflicts({"Neutral", "V-Shaped"})
        assert not L.tag_conflicts({"Warm", "Smooth"})

    def test_validate_conflict_rejected(self):
        errs = L.validate_entry(make_entry(tags=["Budget", "V-Shaped", "U-Shaped", "Bright"]))
        assert any("Conflicting tags" in e for e in errs)

    def test_exactly_one_tier_required(self):
        errs = L.validate_entry(make_entry(tags=["Warm", "Smooth", "Relaxed", "Fun"]))
        assert any("price-tier" in e for e in errs)

    def test_tier_must_match_price(self):
        errs = L.validate_entry(make_entry(tags=["Flagship", "Warm", "Smooth", "Relaxed"]))
        assert any("Price-tier tag" in e for e in errs)

# ===========================================================================
# Tree virtualization + import jump (needs a display; skipped headless).
# The F-8 virtualized tree inserts brand nodes WITHOUT entry rows: brands
# need a placeholder child or ttk shows no disclosure arrow (unexpandable
# tree), and ImportDialog._apply must mount the row BEFORE touching it
# (tree.parent/see/selection_set raise TclError on unknown iids -- the
# old order aborted _apply, so the dialog never closed and no jump
# happened even though the import itself had landed).
# ===========================================================================
class _StubEditor:
    def __init__(self):
        self.loaded = []
        self.original_id = None

    def form_is_dirty(self):
        return False

    def load_entry(self, entry):
        self.loaded.append(dict(entry))


class _StubNotebook:
    def __init__(self):
        self.selected = []

    def select(self, tab):
        self.selected.append(tab)


@pytest.fixture()
def tk_tree_app():
    """Minimal stand-in exposing what MainApp.populate_tree /
    _ensure_entry_visible / ImportDialog._apply touch, with a REAL
    ttk.Treeview so virtualization + selection behave for real."""
    import tkinter as tk
    from tkinter import ttk
    import types
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("no display for Tk integration tests")
    root.withdraw()
    try:
        app = types.SimpleNamespace()
        app.entries = []
        app.tree = ttk.Treeview(root, show="tree")
        app.tree.pack()
        app.search_var = tk.StringVar(value="")
        app.status_var = tk.StringVar(value="")
        app.entries_header_var = tk.StringVar(value="")
        app._search_debounce_id = None
        app._full_labels = {}
        app._disp_cache = {}
        app._ellipsis_fp = None
        app._ellipsis_after = None
        app._all_brand_nodes = {}
        app._materialized = set()
        app._marquee_after = None
        app._marquee = None
        app.editing_index = None
        app._selected_iid = None
        app.dirty = False
        app.editor = _StubEditor()
        app.notebook = _StubNotebook()
        app.ops = []
        app._stop_tree_marquee = lambda: MAIN.MainApp._stop_tree_marquee(app)
        app._apply_ellipsis = lambda: MAIN.MainApp._apply_ellipsis(app)
        app._on_brand_expand = lambda e=None: MAIN.MainApp._on_brand_expand(
            app, e)
        app._materialize_brand = lambda brand_iid, idxs: \
            MAIN.MainApp._materialize_brand(app, brand_iid, idxs)
        app._safe_sort_key = lambda i: MAIN.MainApp._safe_sort_key(app, i)
        app._ensure_entry_visible = lambda iid: \
            MAIN.MainApp._ensure_entry_visible(app, iid)
        app.populate_tree = lambda restore_selection=True: \
            MAIN.MainApp.populate_tree(app, restore_selection)
        app._deepcopy = MAIN.MainApp._deepcopy
        app._record_op = lambda k, d, c: app.ops.append((k, d, c))
        app._mark_audit_dirty = lambda: None
        app.refresh_spell_vocab = lambda: None
        app._autosave = lambda: None
        app._notify_db_changed = lambda: None
        yield app
    finally:
        try:
            root.destroy()
        except Exception:
            pass


class TestVirtualizedTree:
    def test_brand_nodes_carry_placeholder_for_arrow(self, tk_tree_app):
        app = tk_tree_app
        app.entries = [make_entry(brand="Moondrop", model="Chu",
                                  id="moondrop_chu")]
        app.populate_tree()
        brands = app.tree.get_children("")
        assert brands == ("brand:Moondrop",)
        kids = app.tree.get_children(brands[0])
        # exactly one placeholder child -> ttk renders the disclosure
        # arrow; no entry rows mounted yet (lazy)
        assert len(kids) == 1 and kids[0].startswith("placeholder:")
        assert not app.tree.exists("entry:0")

    def test_brand_expansion_mounts_rows(self, tk_tree_app):
        app = tk_tree_app
        app.entries = [make_entry(brand="Moondrop", model="Chu",
                                  id="moondrop_chu"),
                       make_entry(brand="Moondrop", model="Aria",
                                  id="moondrop_aria")]
        app.populate_tree()
        node = app.tree.get_children("")[0]
        app.tree.item(node, open=True)
        app._on_brand_expand()
        kids = set(app.tree.get_children(node))
        assert kids == {"entry:0", "entry:1"}
        assert app.tree.exists("entry:0")

    def test_expand_event_mounts_before_open_flag_flips(self, tk_tree_app):
        """Tk fires <<TreeviewOpen>> BEFORE flipping the node's -open
        flag on real clicks (focus is already on the clicked brand, but
        -open still reads false). The handler must mount via focus: a
        scan for open nodes sees nothing and the brand expands empty
        (only rendering when some LATER expansion re-runs the handler).
        This replicates the exact event-time state of an arrow click."""
        app = tk_tree_app
        app.entries = [make_entry(brand="Moondrop", model="Chu",
                                  id="moondrop_chu"),
                       make_entry(brand="7HZ", model="Zero",
                                  id="7hz_zero")]
        app.populate_tree()
        moondrop = "brand:Moondrop"
        seven = "brand:7HZ"
        assert set(app.tree.get_children("")) == {moondrop, seven}
        # arrow-click state: focus moved, -open NOT yet flipped
        app.tree.focus(moondrop)
        assert not app.tree.item(moondrop, "open")
        app.tree.event_generate("<<TreeviewOpen>>")
        app._on_brand_expand()
        assert set(app.tree.get_children(moondrop)) == {"entry:0"}
        # the untouched brand stays lazy (placeholder only)
        assert not app.tree.exists("entry:1")
        assert list(app.tree.get_children(seven))[0].startswith(
            "placeholder:")

    def test_brand_with_malformed_values_still_mounts(self, tk_tree_app):
        """Real-world databases carry non-string scalars (model: 2,
        variant: null from hand edits / AI output). Pre-fix these raised
        inside _materialize_brand AFTER the placeholder was deleted, so
        the brand expanded to zero rows forever."""
        app = tk_tree_app
        app.entries = [make_entry(brand="Weird", model=2, id="weird_2"),
                       make_entry(brand="Weird", model="Ok", variant=3,
                                  id="weird_ok"),
                       make_entry(brand="Weird", model=None, variant="X",
                                  id="weird_x"),
                       make_entry(brand="Weird", model=None, id="weird_noname")]
        app.populate_tree()
        node = app.tree.get_children("")[0]
        app.tree.item(node, open=True)
        app._on_brand_expand()
        kids = set(app.tree.get_children(node))
        assert kids == {"entry:0", "entry:1", "entry:2", "entry:3"}

    def test_sort_key_survives_non_string_values(self):
        assert L.sort_key({"brand": "B", "model": 2,
                           "variant": None}) == ("b", "2", "")
        assert L.sort_key({"brand": ["X"], "model": None}) == \
            ("['x']", "", "")
        assert L.format_entry_label({"brand": "B", "model": 2,
                                     "variant": None,
                                     "id": "x"}) == "B 2"

    def test_import_apply_closes_dialog_and_selects_new_entry(
            self, tk_tree_app, monkeypatch):
        """End-to-end of the reported flaw: a NEW entry under a brand
        whose rows were never mounted. Pre-fix, tree.parent() raised
        TclError here, so the import landed but the dialog stayed open
        and nothing was selected."""
        import tkinter as tk
        app = tk_tree_app
        app.entries = [make_entry(brand="Moondrop", model="Chu",
                                  id="moondrop_chu")]
        monkeypatch.setattr(AI.win_drop, "enable_native_file_drop",
                            lambda self, cb: False)
        root = app.tree.winfo_toplevel()
        dlg = AI.ImportDialog.__new__(AI.ImportDialog)
        tk.Toplevel.__init__(dlg, root)
        dlg.app = app
        new_entry = make_entry(brand="7HZ", model="Zero", variant="",
                               id="7hz_zero", price_usd=45)
        dlg.proposals = [{"action": "new", "entry": new_entry}]
        dlg.include = {"p0": True}
        closed = []
        dlg.destroy = lambda: closed.append(True)
        # the 7HZ brand exists in the db after staging but its page was
        # never mounted -- the exact shape that used to crash parent()
        dlg._apply()
        assert closed, "dialog must close after a successful apply"
        assert app.tree.selection() == ("entry:1",)
        assert app.editor.loaded and \
            app.editor.loaded[-1]["id"] == "7hz_zero"
        assert app.notebook.selected and \
            app.notebook.selected[-1] is app.editor
        try:
            root.update_idletasks()
            dlg.destroy()
        except Exception:
            pass


# ===========================================================================
# DESIGN-007/008 -- legibility of accent colours used as TEXT
# ===========================================================================


class TestAccentContrast:
    """The accents are authored as BACKGROUNDS (filled buttons, the selected
    tab). Reusing one verbatim as a FOREGROUND is what made card headers and
    audit severity text unreadable -- the shared severity red measured
    2.75:1 on Parchment's own card. These lock in the fix.

    Pure colour maths, no Tk needed."""

    def test_contrast_ratio_matches_wcag_reference_values(self):
        import theme
        assert theme.contrast_ratio("#ffffff", "#000000") == pytest.approx(
            21.0, abs=0.01)
        assert theme.contrast_ratio("#000000", "#ffffff") == pytest.approx(
            21.0, abs=0.01)
        # identical colours have no contrast at all
        assert theme.contrast_ratio("#123456", "#123456") == pytest.approx(
            1.0, abs=0.01)

    def test_every_theme_keeps_accent_text_above_aa_on_its_card(self):
        """The whole point of the _TEXT variants. Before the fix this
        failed for the light themes (Parchment 2.85:1, Ember 2.9:1)."""
        import theme
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            for key in ("accent", "accent_green", "accent_red",
                        "accent_amber", "accent_violet"):
                got = theme.ensure_contrast(p[key], p["bg_card"])
                assert theme.contrast_ratio(got, p["bg_card"]) >= 4.5, (
                    "{}: {} as text on its own card is {:.2f}:1".format(
                        t["id"], key,
                        theme.contrast_ratio(got, p["bg_card"])))

    def test_accent_backdrop_is_never_altered_by_the_text_variant(self):
        """ensure_contrast must return a NEW colour for text use and leave
        the fill alone -- otherwise accent-filled buttons and the selected
        tab would change hue along with the labels."""
        import theme
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            for key in ("accent", "accent_green", "accent_red"):
                text = theme.ensure_contrast(p[key], p["bg_card"])
                assert theme._palette_dict(t)[key] == p[key]

    def test_legible_on_all_clears_every_surface(self):
        """A single colour has to work as text on four backgrounds at once.
        Circuit (4.28:1) and Arcade (4.47:1) shipped text_secondary just
        under AA on the card, which is why this helper exists."""
        import theme
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            surfaces = (p["bg_card"], p["bg_sidebar"], p["bg_body"],
                        p["bg_input"])
            got = theme._legible_on_all(p["text_secondary"], surfaces)
            for s in surfaces:
                assert theme.contrast_ratio(got, s) >= 4.5, (
                    "{}: {} on {} is {:.2f}:1".format(
                        t["id"], got, s, theme.contrast_ratio(got, s)))

    def test_set_theme_actually_applies_it(self, monkeypatch):
        """Exercises the CALL SITE, not just the helper.

        Testing _legible_on_all() directly leaves a regression in set_theme
        -- assigning the raw text_secondary and never calling the helper --
        completely invisible to the test above, while secondary text drops
        back under AA for two themes. Assert on the live module global that
        every real code path reads."""
        import theme
        monkeypatch.setattr(theme, "_save_settings", lambda: None)
        original = theme.current_theme_id
        try:
            for t in theme.THEMES:
                theme.set_theme(t["id"])
                for s in (theme.BG_CARD, theme.BG_PANEL, theme.BG_MAIN,
                          theme.BG_INPUT):
                    assert theme.contrast_ratio(theme.TEXT_DIM, s) >= 4.5, (
                        "{}: TEXT_DIM {} on {} is {:.2f}:1".format(
                            t["id"], theme.TEXT_DIM, s,
                            theme.contrast_ratio(theme.TEXT_DIM, s)))
        finally:
            theme.set_theme(original)


class TestDerivedColoursRemap:
    """REGRESSION: retint() remaps a tk widget by looking its CURRENT colour
    up in an old->new table built from the palette. Derived colours
    (TEXT_DIM, ACCENT_*_TEXT) are computed, not authored, so they were
    absent from that table -- meaning any tk.Label given one kept the
    previous theme's colour on every switch, forever.

    ttk widgets cannot catch this: apply_styles() restyles them wholesale, so
    the bug lives exclusively in tk.Label / tk.Canvas code and is invisible
    in the stylesheet. That is exactly why it needs a test rather than a
    read-through."""

    def test_derived_colours_are_in_the_retint_table(self, monkeypatch):
        import theme
        monkeypatch.setattr(theme, "_save_settings", lambda: None)
        ids = [t["id"] for t in theme.THEMES]
        original = theme.current_theme_id
        try:
            for a in range(len(ids)):
                for b in range(len(ids)):
                    if a == b:
                        continue
                    theme.set_theme(ids[a])
                    before = theme.TEXT_DIM, theme.ACCENT_RED_TEXT
                    theme.set_theme(ids[b])
                    table = {k.lower(): v.lower()
                             for k, v in (theme._prev_palette or {}).items()}
                    for old_val, new_val in zip(before, (
                            theme.TEXT_DIM, theme.ACCENT_RED_TEXT)):
                        if old_val.lower() != new_val.lower():
                            assert old_val.lower() in table, (
                                "{}->{}: {} missing from the retint "
                                "table".format(ids[a], ids[b], old_val))
                            assert table[old_val.lower()] == new_val.lower()
        finally:
            theme.set_theme(original)

    def test_switching_theme_twice_is_idempotent(self, monkeypatch):
        """Guards the retint table against accumulating entries: a chained
        A->B->C switch must not leave A's colour mapped onto C's."""
        import theme
        monkeypatch.setattr(theme, "_save_settings", lambda: None)
        ids = [t["id"] for t in theme.THEMES]
        original = theme.current_theme_id
        try:
            theme.set_theme(ids[0])
            theme.set_theme(ids[1])
            theme.set_theme(ids[2])
            theme.set_theme(ids[2])
            assert theme.current_theme_id == ids[2]
        finally:
            theme.set_theme(original)


# ===========================================================================
# DESIGN-009 / DESIGN-010 -- text diet
# ===========================================================================


class TestDriverSummarySeparator:
    """DESIGN-009: the driver summary joined its two facts with SIX literal
    spaces. A run of spaces is a fake table column: it does not align under
    a proportional font, does not survive a font-size change, and lands in
    the label's accessible name as noise. Checked on the real formatter, so
    the placeholder set at construction and the value recomputed on every
    keystroke cannot drift apart again."""

    def test_no_run_of_spaces_in_any_driver_summary(self):
        import re
        import main
        for dt, dc in (("", ""), ("DD", "1DD"), ("BA+DD", "2DD+1BA"),
                       ("DD", "1DD+2BA"), ("BC", "1BC"),
                       ("Hybrid", "3DD+1BA+1BC")):
            text = main.DriverConfigPanel._driver_summary_text(dt, dc)
            assert not re.search(r"\S {3,}\S", text), (
                "driver summary uses spaces for layout: {!r}".format(text))

    def test_both_facts_always_present(self):
        import main
        for dt, dc in (("", ""), ("DD", "1DD"), ("BA+DD", "2DD+1BA")):
            text = main.DriverConfigPanel._driver_summary_text(dt, dc)
            assert "Driver Type:" in text, text
            assert "Config:" in text, text

    def test_empty_values_get_readable_placeholders(self):
        import main
        text = main.DriverConfigPanel._driver_summary_text(None, None)
        assert "(unknown/unverified)" in text, text
        assert "(none)" in text, text
        # the falsy-but-present values must render identically to None
        assert main.DriverConfigPanel._driver_summary_text("", "") == text

    def test_uses_the_apps_own_separator(self):
        """The separator should be the one this codebase already uses
        elsewhere, not a new invention."""
        import main
        text = main.DriverConfigPanel._driver_summary_text("DD", "1DD")
        assert "\u00b7" in text, text


class TestTabLabelsAreNotPaddedWithSpaces:
    """DESIGN-010: tab labels were built as "  Editor  ". Two problems.
    The spaces are a fixed floor that theme.set_tab_pad() can never reclaim
    (it resizes the STYLE padding, not the label text), and they leak into
    the label's accessible name.

    The regression risk is the audit BADGE: _update_audit_badge() rewrites
    the Audit tab's label and used to locate it with
    .startswith("  Audit  ") -- a magic prefix. Unpadding the labels without
    fixing that lookup would silently stop the live issue count from ever
    appearing on any tab, which is why these check the badge, not just the
    literal strings."""

    LABELS = ("Editor", "Audit", "History", "Import", "Export")

    def _notebook(self, monkeypatch):
        import tkinter as tk
        import theme
        monkeypatch.setattr(theme, "_save_settings", lambda: None)
        try:
            root = tk.Tk()
        except Exception:
            import pytest
            pytest.skip("no display for Tk integration tests")
        root.withdraw()
        return root

    def test_no_source_literal_pads_a_tab_label(self):
        """Guards the whole family of call sites, including ones added
        later. Reads the SOURCE rather than a live window so it also covers
        labels that are only set at runtime.

        Comments are stripped first: the DESIGN-010 note deliberately quotes
        the old padded form, and a naive text scan would match its own
        documentation and fail forever."""
        import io
        import re
        import tokenize
        import main as MAIN
        with open(MAIN.__file__, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        # keep code + string literals, drop comments
        kept = [t.string for t in toks
                if t.type not in (tokenize.COMMENT, tokenize.NL,
                                  tokenize.NEWLINE, tokenize.INDENT,
                                  tokenize.DEDENT)]
        src = " ".join(kept)
        for name in self.LABELS:
            bad = re.search(r'"\s\s{}\s\s"'.format(name), src)
            assert bad is None, (
                "tab label {!r} is padded with literal spaces".format(name))
        # and the badge base must match the unpadded form
        import main
        assert main.MainApp.AUDIT_TAB_BASE == "Audit", \
            main.MainApp.AUDIT_TAB_BASE

    def test_badge_label_is_built_from_the_base(self):
        """The badge must not re-introduce padding of its own."""
        import main
        base = main.MainApp.AUDIT_TAB_BASE
        assert base == base.strip(), repr(base)
        for n in (0, 1, 7, 42):
            label = base if not n else "{} \u26a0 {}".format(base, n)
            assert label == label.strip(), repr(label)

    def test_badge_prefix_lookup_matches_unpadded_label(self):
        """The fallback lookup is a startswith on the label text. It has to
        match the label the notebook actually carries, or the badge is
        written to the wrong tab (or nowhere)."""
        import main
        base = main.MainApp.AUDIT_TAB_BASE
        for label in (base, "{} \u26a0 7".format(base)):
            assert label.startswith("Audit"), repr(label)


# ===========================================================================
# DESIGN-011 -- legible label colour for text sitting ON an accent fill
# ===========================================================================


class TestContrastTextMeasuresInsteadOfGuessing:
    """contrast_text() used a 0.6 perceived-luminance cutoff to choose
    black or white. That is a guess, and it guessed wrong for most of the
    palette: it returned white on #dd6b20 (3.39:1) when black gives 6.20:1,
    so every Accent and Danger button was under AA in all nine themes.

    These lock in "measure, don't threshold"."""

    def test_picks_the_colour_that_actually_measures_better(self):
        import theme
        # the fill that broke the old heuristic
        assert theme.contrast_text("#dd6b20") == "#000000"
        # a genuinely dark fill must still get light text
        assert theme.contrast_text("#8262c8") == "#ffffff"

    def test_never_returns_worse_than_the_alternative(self):
        import theme
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            for key in ("accent", "accent_green", "accent_red",
                        "accent_amber", "accent_violet"):
                fill = p[key]
                got = theme.contrast_ratio(theme.contrast_text(fill), fill)
                best = max(theme.contrast_ratio("#000000", fill),
                           theme.contrast_ratio("#ffffff", fill))
                assert got >= best - 0.01, (
                    "{}: {} on {} gives {:.2f}:1, best possible {:.2f}:1"
                    .format(t["id"], theme.contrast_text(fill), fill,
                            got, best))

    def test_button_label_clears_aa_in_every_theme(self):
        import theme
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            for key in ("accent", "accent_green", "accent_red",
                        "accent_amber", "accent_violet"):
                fill = p[key]
                r = theme.contrast_ratio(theme.contrast_text(fill), fill)
                assert r >= 4.5, (
                    "{}: label on {} is only {:.2f}:1".format(
                        t["id"], fill, r))

    def test_old_threshold_rule_would_have_failed(self):
        """Non-vacuous guard: re-implement the removed heuristic and show it
        really does produce a sub-AA label, so this test is not just
        restating the implementation."""
        import theme

        def old_rule(hex_color):
            r, g, b = theme._rgb(hex_color)
            bright = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            return "#000000" if bright > 0.6 else "#ffffff"

        failures = 0
        for t in theme.THEMES:
            p = theme._palette_dict(t)
            for key in ("accent", "accent_green", "accent_red",
                        "accent_amber", "accent_violet"):
                if theme.contrast_ratio(old_rule(p[key]), p[key]) < 4.5:
                    failures += 1
        assert failures > 0, (
            "the old heuristic no longer fails anywhere -- the finding this "
            "test guards may no longer apply")

    def test_titlebar_darkness_test_is_decoupled(self):
        """style_titlebar() used to decide whether the panel was dark by
        comparing contrast_text(BG_PANEL) to the literal "#ffffff". That
        silently depended on contrast_text returning one of exactly two
        values; once it started nudging its result the title bar would flip
        to ForceDark on a light theme. Assert the coupling is gone."""
        import io
        import main as MAIN
        import theme as T
        src = io.open(T.__file__, encoding="utf-8-sig").read()
        assert 'contrast_text(BG_PANEL) == "#ffffff"' not in src, (
            "the title bar still infers panel darkness by string-comparing "
            "contrast_text's result")
        # and the replacement must be a real brightness test
        assert "0.299" in src, "expected a perceived-brightness test"


class TestNoRawAccentAsTextInStylesheet:
    """The DESIGN-007 sweep covered module call sites but never theme.py's
    OWN style definitions, where Compact.Toast.TButton set a raw
    ACCENT_GREEN as its label colour while inheriting the card background
    -- i.e. accent-as-text, 2.8-3.9:1, in the one file that exists to
    prevent exactly that.

    These read the values back from a LIVE ttk.Style rather than grepping
    the source: the stylesheet is assembled from many styles, some sharing a
    variable (Blue.TButton's label is `sel_fg`, which is
    contrast_text(ACCENT_BLUE)), so matching on source text would be both
    brittle and wrong."""

    FILLED = ("Accent.TButton", "Danger.TButton", "Blue.TButton",
              "Accent.Compact.TButton", "Blue.Compact.TButton",
              "Accent.Toast.TButton")

    @pytest.fixture()
    def styled(self):
        import tkinter as tk
        from tkinter import ttk as TTK
        import theme
        try:
            root = tk.Tk()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        root.withdraw()
        original = theme.current_theme_id
        # the app's styles only exist once the stylesheet has been applied;
        # against a bare root every lookup falls through to the Tk default
        theme.apply_styles(root)
        yield TTK.Style(root), theme, original
        try:
            theme.set_theme(original)
            theme.apply_styles(root)
        except Exception:
            pass
        root.destroy()

    def test_toast_style_uses_the_text_variant(self, styled):
        """The style sets no background, so it renders on the card surface.

        Asserting "differs from the raw accent" would be wrong: for 6 of the
        9 palettes the raw green already clears AA on the card, so
        ensure_contrast legitimately returns it unchanged and the two values
        are identical. What must hold is that the style uses the DERIVED
        variant and that the result is legible -- which is a stronger claim
        and holds in every theme."""
        st, theme, original = styled
        for t in theme.THEMES:
            if t["id"] != original:
                theme.set_theme(t["id"])
                theme.apply_styles(st.master)
            fg = st.lookup("Compact.Toast.TButton", "foreground")
            assert fg is not None, "Compact.Toast.TButton has no foreground"
            assert fg.lower() == theme.ACCENT_GREEN_TEXT.lower(), (
                "{}: expected the legible variant {}, got {}".format(
                    t["id"], theme.ACCENT_GREEN_TEXT, fg))
            bg = st.lookup("TButton", "background")
            r = theme.contrast_ratio(fg, bg)
            assert r >= 4.5, "{}: toast label {} on {} is {:.2f}:1".format(
                t["id"], fg, bg, r)

    @pytest.mark.parametrize("name", FILLED)
    def test_filled_button_label_clears_aa_in_every_theme(self, styled, name):
        st, theme, original = styled
        for t in theme.THEMES:
            if t["id"] != original:
                theme.set_theme(t["id"])
                theme.apply_styles(st.master)
            fg = st.lookup(name, "foreground")
            bg = st.lookup(name, "background")
            assert fg and bg, "{} has no resolved colours".format(name)
            r = theme.contrast_ratio(fg, bg)
            assert r >= 4.5, "{} in {}: {} on {} is {:.2f}:1".format(
                name, t["id"], fg, bg, r)


# ===========================================================================
# PROMPT CONTRACTS
#
# The two .txt documents in the project root are the authoritative spec:
#   ADD ENTRY PROMPT.txt  -- rules for producing entries
#   AUDIT DATABASE PROMPT.txt -- rules for auditing/repairing them
# These tests hold db_logic to every rule in them that is mechanically
# decidable from a single entry.
# ===========================================================================


class TestPromptSchemaContract:
    """Both prompts: 'Do NOT add, remove, rename, or reorder any fields'
    (add-entry) / 'Every entry must maintain this exact schema' (audit)."""

    EXPECTED = ["id", "brand", "model", "variant", "year", "price_usd",
                "driver_type", "driver_config", "impedance", "sensitivity",
                "connector", "form_factor", "tags", "files"]

    def test_exact_field_list_and_order(self):
        import db_logic as L
        assert list(L.SCHEMA_FIELDS) == self.EXPECTED, L.SCHEMA_FIELDS

    def test_unknown_fields_are_reported_not_silently_dropped(self):
        """A key outside the schema used to vanish without a word, which
        contradicts build_clean_entry's own promise that 'corruption can
        never be laundered quietly'. A typo'd field name turned a populated
        spec into the default -- 0 for impedance, which the prompts forbid
        outright on wired gear -- and the original value was unrecoverable.
        Both prompts treat an unrecognised field as a schema violation."""
        import db_logic as L
        notes = []
        L.build_clean_entry(dict(self._entry(), nickname="x"), notes, "e")
        assert any("nickname" in n for n in notes), notes

    def test_misspelled_field_names_the_intended_one(self):
        import db_logic as L
        for bad, want in (("impedence", "impedance"),
                          ("modle", "model"),
                          ("price", "price_usd")):
            notes = []
            L.build_clean_entry(dict(self._entry(), **{bad: 1}), notes, "e")
            assert notes, "no note for {!r}".format(bad)
            assert want in notes[0], "{!r} -> {}".format(bad, notes[0])

    def test_a_clean_entry_produces_no_notes(self):
        import db_logic as L
        notes = []
        L.build_clean_entry(self._entry(), notes, "e")
        assert notes == [], notes

    @staticmethod
    def _entry(**o):
        import db_logic as L
        e = dict(L.BLANK_ENTRY)
        e.update({"id": "moondrop_chu", "brand": "Moondrop", "model": "Chu",
                  "variant": "", "year": 2023, "price_usd": 20,
                  "driver_type": "DD", "driver_config": "1DD",
                  "impedance": 28, "sensitivity": 120, "connector": "2-pin",
                  "form_factor": "IEM",
                  "tags": ["Budget", "Warm", "Smooth", "Relaxed"],
                  "files": []})
        e.update(o)
        return e


class TestPromptIdRules:
    """Both prompts: lowercase alphanumeric + underscores only, no trailing
    underscore, brand_model[_variant]."""

    @pytest.mark.parametrize("bad", [
        "Moondrop_Chu", "moondrop-chu", "moondrop chu", "moondrop.chu",
        "moondrop/chu", "moondrop'chu", "moondrop:chu", "moondrop_chu_",
    ])
    def test_malformed_ids_are_rejected(self, bad):
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(id=bad)
        assert L.validate_entry(copy.deepcopy(e)), \
            "accepted malformed id {!r}".format(bad)

    def test_build_id_omits_an_empty_variant(self):
        import db_logic as L
        assert L.build_id("Moondrop", "Chu", "") == "moondrop_chu"
        assert L.build_id("Moondrop", "Chu", "III") == "moondrop_chu_iii"

    def test_build_id_strips_punctuation(self):
        import db_logic as L
        assert L.build_id("Moondrop", "Wan'er", "S/G") == "moondrop_wan_er_s_g"

    def test_trailing_underscore_is_repaired(self):
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(id="moondrop_chu_")
        ents = [copy.deepcopy(e)]
        for issue in L.run_full_audit(ents):
            if issue.fix:
                issue.fix(ents)
        assert ents[0]["id"] == "moondrop_chu", ents[0]["id"]


class TestPromptConnectorMatrix:
    """AUDIT: '"Over-Ear Headphones (Wired)" ... They use "Fixed Cable",
    "Detachable Cable", "Proprietary", or "Electrostatic".'

    "Proprietary" was missing from FORM_CONNECTOR_MAP, so spec-compliant
    over-ear entries were rejected on load, the option was absent from the
    editor dropdown, and the audit flagged correct data as an invalid
    pairing."""

    def test_proprietary_is_valid_for_wired_over_ear(self):
        import db_logic as L
        allowed = L.FORM_CONNECTOR_MAP["Over-Ear Headphones (Wired)"]
        assert "Proprietary" in allowed, allowed

    def test_a_proprietary_over_ear_entry_validates(self):
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(
            id="psb_m4u_4", brand="PSB", model="M4U 4",
            form_factor="Over-Ear Headphones (Wired)", connector="Proprietary",
            impedance=32, sensitivity=98, driver_type="Dynamic",
        )
        e["driver_type"] = "DD"
        assert not L.validate_entry(copy.deepcopy(e)), \
            L.validate_entry(copy.deepcopy(e))

    def test_every_prompt_pairing_is_exactly_enforced(self):
        """The whole matrix, both directions: allowed combos validate, and
        the hard-forbidden ones do not."""
        import copy
        import db_logic as L
        for ff, allowed in L.FORM_CONNECTOR_MAP.items():
            for conn in L.CONNECTORS_ALL:
                e = TestPromptSchemaContract._entry(
                    form_factor=ff, connector=conn)
                if ff.startswith("Wireless"):
                    e.update({"impedance": 0, "sensitivity": 0,
                              "driver_config": "", "driver_type": ""})
                errs = L.validate_entry(copy.deepcopy(e))
                if conn in allowed:
                    assert not errs, "{} + {} should be valid: {}".format(
                        ff, conn, errs)
                else:
                    assert errs, "{} + {} should be rejected".format(ff, conn)

    def test_wired_over_ear_still_forbids_bluetooth(self):
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(
            form_factor="Over-Ear Headphones (Wired)", connector="Bluetooth")
        assert L.validate_entry(copy.deepcopy(e))


class TestPromptTagConflictGuidance:
    """AUDIT: 'FORBIDDEN CONFLICTING PAIRS (MUST REMOVE/RESOLVE)' followed by
    a per-pair resolution. The app detects every pair but used to report only
    'Conflicting tags present: X + Y', leaving the user to consult the prompt
    for how to settle it.

    The resolution is surfaced, NOT auto-applied: six of the eight turn on an
    acoustic judgement ('keep the dominant treble character') that the app
    cannot make without the raw curve, and silently deleting a tonal tag
    would destroy data the user may have measured deliberately."""

    def test_every_forbidden_pair_has_guidance(self):
        import db_logic as L
        for pair in L.TAG_CONFLICT_PAIRS:
            r = L.tag_conflict_resolution(pair)
            assert r, "no resolution for {}".format(sorted(pair))

    def test_guidance_reaches_the_audit_message(self):
        import copy
        import db_logic as L
        for pair in L.TAG_CONFLICT_PAIRS:
            a, b = sorted(pair)
            e = TestPromptSchemaContract._entry(
                tags=["Budget", a, b, "Smooth"])
            msgs = [i.message for i in L.run_full_audit([copy.deepcopy(e)])
                    if i.category == "Tag Conflict"]
            assert msgs, "no issue for {} + {}".format(a, b)
            assert "resolve:" in msgs[0], msgs[0]

    def test_guidance_reaches_the_validator(self):
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(
            tags=["Budget", "V-Shaped", "U-Shaped", "Smooth"])
        errs = [x for x in L.validate_entry(copy.deepcopy(e))
                if "onflicting" in x]
        assert errs and "resolve:" in errs[0], errs

    def test_explicitly_listed_pair_uses_its_own_rule(self):
        import db_logic as L
        assert "weaker supported tag" in L.tag_conflict_resolution(
            ("Neutral", "V-Shaped"))
        assert "dominant treble" in L.tag_conflict_resolution(
            ("Dark", "Bright"))

    def test_unlisted_primary_tonality_pair_gets_generic_guidance(self):
        import db_logic as L
        r = L.tag_conflict_resolution(("Neutral", "Balanced"))
        assert r and "at most one primary tonal" in r.lower(), r

    def test_non_conflicts_have_no_guidance(self):
        import db_logic as L
        assert L.tag_conflict_resolution(("Warm", "Fun")) is None

    def test_tonal_conflicts_are_not_silently_auto_fixed(self):
        """Only mechanically-derivable repairs get a Fix All. A tonal
        conflict must stay flagged for a human."""
        import copy
        import db_logic as L
        e = TestPromptSchemaContract._entry(
            tags=["Budget", "V-Shaped", "U-Shaped", "Smooth"])
        ents = [copy.deepcopy(e)]
        for issue in L.run_full_audit(ents):
            if issue.fix:
                issue.fix(ents)
        assert ents[0]["tags"] == e["tags"], \
            "a tonal conflict was auto-resolved: {}".format(ents[0]["tags"])


class TestGeneratedPromptsMatchTheContracts:
    """The app GENERATES both prompts at runtime (ai_prompts). Those are what
    an AI actually receives, so a divergence from the authoritative .txt
    files would ship a prompt that contradicts the user's own documents."""

    def _generated(self):
        import ai_prompts as AP
        return AP.build_add_entry_prompt(), AP.build_audit_prompt()

    def test_all_controlled_vocabularies_appear_in_both_prompts(self):
        import db_logic as L
        add, aud = self._generated()
        for label, items in (("tag", L.APPROVED_TAGS),
                             ("connector", L.CONNECTORS_ALL),
                             ("form factor", L.FORM_FACTORS)):
            for item in items:
                assert item in add, "{} {!r} missing from add-entry".format(
                    label, item)
                assert item in aud, "{} {!r} missing from audit".format(
                    label, item)
        for dt in L.ALLOWED_DRIVER_TYPES:
            if not dt:
                continue
            assert dt in add and dt in aud, "driver type {!r}".format(dt)

    def test_the_schema_is_stated_in_both_prompts(self):
        import db_logic as L
        add, aud = self._generated()
        for f in L.SCHEMA_FIELDS:
            assert '"{}"'.format(f) in add, f
            assert '"{}"'.format(f) in aud, f

    def test_the_canonical_driver_order_is_stated_in_order(self):
        add, aud = self._generated()
        for name, txt in (("add-entry", add), ("audit", aud)):
            assert ("DD -> BA -> Planar -> EST -> MEMS -> PZT -> BC" in txt
                    or "DD \u2192 BA \u2192 Planar \u2192 EST \u2192 MEMS \u2192 "
                       "PZT \u2192 BC" in txt), \
                "{} prompt does not state the canonical order".format(name)

    def test_both_prompts_pair_proprietary_with_wired_over_ear(self):
        """Guards the matrix bug at its other site: the generated prompts
        must not repeat the omission the validator had."""
        add, aud = self._generated()
        for name, txt in (("add-entry", add), ("audit", aud)):
            i = txt.find("Over-Ear Headphones (Wired)")
            assert i >= 0, "{} prompt never mentions wired over-ear".format(
                name)
            seg = txt[i:i + 400]
            assert "Proprietary" in seg, \
                "{} prompt omits Proprietary for wired over-ear".format(name)


class TestDriverConfigRepairConverges:
    """The prompts make BOTH rules mandatory: driver_config must have no
    whitespace around '+' AND must follow the canonical order.

    The order check used to be suppressed whenever whitespace was present
    ('and not has_ws'). Because the audit list is computed once up front, a
    spaced, out-of-order config like '1BA + 1DD' reported ONLY the
    whitespace; the whitespace repair de-spaced it to '1BA+1DD' and the
    ordering violation was never fixed in that pass -- so Fix All left the
    entry non-compliant and the user had to re-run the audit to discover it.
    """

    CASES = [("1BA+1DD", "1DD+1BA"),
             ("1BA + 1DD", "1DD+1BA"),
             ("1BC+1EST+4BA+1DD", "1DD+4BA+1EST+1BC"),
             ("1BC + 1EST + 4BA + 1DD", "1DD+4BA+1EST+1BC"),
             ("1DD", "1DD"),
             ("1DD+1BA+1Planar", "1DD+1BA+1Planar")]

    @staticmethod
    def _entry(cfg, dt="Hybrid"):
        e = TestPromptSchemaContract._entry(driver_config=cfg,
                                            driver_type=dt)
        return e

    @pytest.mark.parametrize("cfg,want", CASES)
    def test_fix_all_reaches_the_canonical_form_in_one_pass(self, cfg, want):
        import copy
        import db_logic as L
        ents = [copy.deepcopy(self._entry(cfg))]
        for issue in L.run_full_audit(ents):
            if issue.fix:
                issue.fix(ents)
        assert ents[0]["driver_config"] == want, \
            "{} -> {} (want {})".format(cfg, ents[0]["driver_config"], want)

    @pytest.mark.parametrize("cfg,want", CASES)
    def test_both_repairs_converge_in_either_order(self, cfg, want):
        """The two repairs write the same field. If they disagreed, applying
        them in the wrong order would clobber the other's result."""
        import copy
        import itertools
        import db_logic as L
        issues = [i for i in L.run_full_audit([self._entry(cfg)]) if i.fix
                  and i.code in ("dc-whitespace", "dc-order")]
        if len(issues) < 2:
            pytest.skip("only one repair applies to {!r}".format(cfg))
        for perm in itertools.permutations(issues):
            ents = [copy.deepcopy(self._entry(cfg))]
            for issue in perm:
                issue.fix(ents)
            assert ents[0]["driver_config"] == want, (
                "{} in order {} -> {} (want {})".format(
                    cfg, [i.code for i in perm], ents[0]["driver_config"],
                    want))

    def test_ordering_violation_is_reported_alongside_whitespace(self):
        import copy
        import db_logic as L
        codes = {i.code for i in
                 L.run_full_audit([copy.deepcopy(self._entry("1BA + 1DD"))])}
        assert "dc-whitespace" in codes, codes
        assert "dc-order" in codes, codes

    def test_a_correctly_ordered_spaced_config_is_only_a_whitespace_problem(self):
        """'1DD + 1BA' is already canonical; calling it an ordering error
        would be misleading and would duplicate the whitespace issue."""
        import copy
        import db_logic as L
        codes = {i.code for i in
                 L.run_full_audit([copy.deepcopy(self._entry("1DD + 1BA"))])}
        assert "dc-whitespace" in codes, codes
        assert "dc-order" not in codes, codes

    def test_whitespace_message_names_the_ordering_problem_when_present(self):
        import copy
        import db_logic as L
        msgs = [i.message for i in
                L.run_full_audit([copy.deepcopy(self._entry("1BA + 1DD"))])
                if i.code == "dc-whitespace"]
        assert msgs and "canonical order" in msgs[0], msgs
        msgs = [i.message for i in
                L.run_full_audit([copy.deepcopy(self._entry("1DD + 1BA"))])
                if i.code == "dc-whitespace"]
        assert msgs and "canonical order" not in msgs[0], msgs


class TestMenuBarMnemonicUnderline:
    """REGRESSION (user-reported): every menu-bar label rendered with a
    stray "_" in front of its name -- "_File", "_View", "_Help".

    The label text was built as "  {}  ".format(label_text) while the
    mnemonic underline was set with underline=0. Index 0 of that string is a
    SPACE, not the mnemonic, so Tk dutifully underlined the padding and drew
    the underscore where the user saw it. The Alt+letter bindings worked
    throughout, which is why the intent was never in doubt; only the drawn
    underline was in the wrong place.

    Same root cause as the space-padded tab labels (DESIGN-010): padding
    baked into the string instead of the widget.
    """

    @staticmethod
    def _font(lbl):
        """A measurable Font for whatever cget("font") returned.

        cget gives back a spec that may be a font NAME string, a tuple, or an
        existing Font; nametofont only resolves registered names, so wrap
        whatever came back in a Font instead of assuming one shape.
        """
        from tkinter import font as tkfont
        spec = lbl.cget("font")
        if isinstance(spec, tkfont.Font):
            return spec
        try:
            return tkfont.Font(root=lbl, font=spec)
        except Exception:
            return tkfont.nametofont("TkDefaultFont")

    @pytest.fixture()
    def bar(self):
        import tkinter as tk
        import main as MAIN
        try:
            app = MAIN.MainApp()
        except Exception:
            import pytest
            pytest.skip("no display for Tk integration tests")
        # must be mapped: a withdrawn toplevel reports stale reqwidth
        app.deiconify()
        app.update()
        app.update_idletasks()
        yield app, list(app._menu_bar_labels)
        try:
            app.destroy()
        except Exception:
            pass

    @staticmethod
    def _no_underline(u):
        """Tk reports "no underline" as an EMPTY STRING from cget(), not as
        the -1 you pass in -- both mean the same thing, so both are accepted.
        Getting this wrong makes a correct fix look broken."""
        return u in ("", -1) or u is None

    def test_no_underline_is_drawn_at_rest(self, bar):
        """The user's complaint: the bar permanently read as
        "_File"/"_Edit"/"_Audit". Windows hides mnemonic underlines until Alt
        is pressed, and so does this now."""
        _app, labels = bar
        for lbl in labels:
            text = str(lbl.cget("text"))
            u = lbl.cget("underline")
            assert self._no_underline(u), (
                "{!r} draws an underline at rest (underline={!r}) -- the bar "
                "must look clean until Alt is held".format(text, u))

    def test_alt_reveals_the_mnemonic_letter(self, bar):
        app, labels = bar
        app.focus_force()
        app.update()
        for seq in ("<KeyPress-Alt_L>", "<KeyPress-Alt_R>"):
            assert app.bind_all(seq), "{} is not bound".format(seq)
        app.event_generate("<KeyPress-Alt_L>")
        app.update()
        for lbl in labels:
            text = str(lbl.cget("text"))
            u = lbl.cget("underline")
            assert isinstance(u, int) and 0 <= u < len(text), (
                "{!r}: Alt did not reveal a mnemonic (underline={!r})".format(
                    text, u))
            assert text[u].isalpha(), (
                "{!r}: Alt underlines {!r}, not a letter".format(
                    text, text[u]))
            # the marked letter must be the one the Alt shortcut uses
            assert text[u].lower() == text[0].lower(), (
                "{!r}: marks {!r} but the shortcut is Alt+{}".format(
                    text, text[u], text[0].lower()))
        app.event_generate("<KeyRelease-Alt_L>")
        app.update()
        for lbl in labels:
            text = str(lbl.cget("text"))
            assert self._no_underline(lbl.cget("underline")), (
                "{!r}: underline stayed on after Alt was released".format(
                    text))

    def test_releasing_alt_is_guaranteed_even_if_swallowed(self, bar):
        """A KeyRelease can be eaten by a popup or a focus change, which
        would leave the underlines stuck on. Focus loss and a click both
        clear them."""
        app, labels = bar
        app.focus_force()
        app.update()
        assert app.bind_all("<FocusOut>"), "<FocusOut> not bound"
        assert app.bind_all("<ButtonPress>"), "<ButtonPress> not bound"
        app.event_generate("<KeyPress-Alt_L>")
        app.update()
        assert any(not self._no_underline(lbl.cget("underline"))
                   for lbl in labels), "Alt did not reveal anything"
        app.event_generate("<FocusOut>")
        app.update()
        for lbl in labels:
            text = str(lbl.cget("text"))
            assert self._no_underline(lbl.cget("underline")), (
                "{!r}: focus loss left the underline stuck on".format(text))

    def test_rebuilding_the_bar_does_not_stack_alt_handlers(self, bar):
        """bind_all ADDS, so a rebuild would otherwise leave several copies
        of every handler behind."""
        app, _labels = bar
        seqs = ("<KeyPress-Alt_L>", "<KeyRelease-Alt_L>", "<FocusOut>")
        before = {s: len(app.bind_all(s)) for s in seqs}
        app._build_custom_menu_bar()
        app.update()
        after = {s: len(app.bind_all(s)) for s in seqs}
        for s in seqs:
            assert after[s] <= before[s], (
                "{} handlers grew from {} to {} on rebuild".format(
                    s, before[s], after[s]))

    def test_bar_labels_are_not_padded_with_literal_spaces(self, bar):
        _app, labels = bar
        for lbl in labels:
            text = str(lbl.cget("text"))
            assert text == text.strip(), \
                "{!r} has padding baked into the string".format(text)
            assert "  " not in text, \
                "{!r} contains a run of spaces".format(text)

    def test_underline_flag_tracks_the_label(self, bar):
        """The label's own underline option and the font it resolves to must
        agree; a mismatch is what made the underline land in the wrong place."""
        _app, labels = bar
        for lbl in labels:
            text = str(lbl.cget("text"))
            u = lbl.cget("underline")
            if self._no_underline(u):
                # no underline requested -> the font must not force one
                assert int(self._font(lbl).actual().get("underline", 0)) == 0, (
                    "{!r}: label wants no underline but its font is "
                    "underlined".format(text))
            else:
                assert int(self._font(lbl).actual().get("underline", 0)) == u, (
                    "{!r}: label underline {} but font underline {}".format(
                        text, u,
                        self._font(lbl).actual().get("underline")))

    def test_fixing_the_underline_did_not_shift_the_layout(self, bar):
        """Padding moved from the string into padx, so padx must be 10 --
        the value that reproduces the old "  File  " + padx=2 width in this
        font (2*2 + four 4px spaces = 20px, i.e. padx=10).

        Asserted on padx rather than on absolute label widths: absolute
        widths move with the theme and the OS font metrics, and a Label's
        reqwidth also includes its own border/highlight, none of which this
        fix controls. What this fix controls is that the padding is widget
        padding and nothing else.
        """
        _app, labels = bar
        for lbl in labels:
            text = str(lbl.cget("text"))
            pad = lbl.cget("padx")
            pad = pad if isinstance(pad, int) else (
                pad[0] if isinstance(pad, (tuple, list)) else 0)
            assert pad == 10, (
                "{!r}: padx is {}, expected 10 -- the pre-fix layout was "
                "padx=2 plus four literal spaces".format(text, pad))
            # and the label must be no wider than its text plus that padding
            # plus its own border, i.e. no hidden slack
            text_px = self._font(lbl).measure(text)
            slack = lbl.winfo_reqwidth() - text_px - 2 * pad
            assert 0 <= slack <= 6, (
                "{!r}: reqwidth {} leaves {}px unexplained beyond text + "
                "2*padx".format(text, lbl.winfo_reqwidth(), slack))

    def test_alt_letter_is_bound_via_bind_all(self, bar):
        app, labels = bar
        for lbl, ch in zip(labels, "featvh"):
            text = str(lbl.cget("text"))
            assert text.lower().startswith(ch), \
                "{!r} does not start with its expected initial {!r}".format(
                    text, ch)
            # bind_all lives on the "all" bindtag; widget.bind() is a
            # different table and would always look empty here
            assert app.bind_all("<Alt-{}>".format(ch)), \
                "Alt+{} is not bound".format(ch)

    def test_every_bar_item_keeps_its_keyboard_and_mouse_routes(self, bar):
        _app, labels = bar
        for lbl in labels:
            text = str(lbl.cget("text"))
            assert lbl.cget("takefocus") in (1, "1"), \
                "{!r} is skipped by Tab".format(text)
            assert lbl.cget("cursor") == "hand2", \
                "{!r} has no hand cursor".format(text)
            for seq in ("<Button-1>", "<Enter>", "<Leave>",
                        "<Key-space>", "<Return>", "<FocusIn>", "<FocusOut>"):
                assert lbl.bind(seq), \
                    "{!r} has no handler for {}".format(text, seq)

class TestImageCachesSurviveRootRecreation:
    """REGRESSION: a rebuilt window crashed with
    '_tkinter.TclError: image "pyimageN" does not exist'.

    A tk.PhotoImage belongs to the Tk interpreter that created it and is dead
    the moment that root is destroyed. All three icon caches were keyed only
    by what they were loading -- (emoji, size), a tag name, an icon name --
    with nothing recording WHICH root built the image, so any consumer
    running after a root was recreated was handed images belonging to a dead
    interpreter.

    Within one process the app never hit this, because it has a single root
    for its whole lifetime; it surfaced as soon as a test created a second
    window. The caches are module-level singletons, so they outlive any one
    root and have to tolerate that.
    """

    def test_a_second_window_builds(self):
        """The observable symptom: MainApp could not even be constructed
        twice in one process."""
        import tkinter as tk
        import main as MAIN
        try:
            first = tk.Tk()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        first.withdraw()
        first.update()
        first.destroy()
        try:
            app = MAIN.MainApp()
        except Exception as exc:
            pytest.fail("a second MainApp failed to build: {!r}".format(exc))
        app.destroy()

    def test_emoji_cache_entries_record_their_root(self):
        import tkinter as tk
        import theme
        try:
            root = tk.Tk()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        root.withdraw()
        try:
            photo = theme.emoji_photo("\U0001F50A", 14, root=root)
            if photo is None:
                pytest.skip("colour emoji unavailable in this environment")
            key = ("\U0001F50A", 14)
            entry = theme._EMOJI_PHOTO_CACHE[key]
            assert isinstance(entry, tuple) and len(entry) == 2, (
                "cache entry is {!r}; it must record (root, photo) so a "
                "stale image is never reused".format(entry))
            assert entry[0] is root, "entry did not record its root"
        finally:
            root.destroy()

    def test_icon_manager_cache_entries_record_their_root(self):
        import tkinter as tk
        import main as MAIN
        try:
            root = tk.Tk()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        root.withdraw()
        try:
            import db_logic as L
            name = next(iter(L.FORM_FACTOR_ICON.values()))
            MAIN.ICONS.get(name)
            entry = MAIN.ICONS.cache.get(name)
            if entry is None:
                pytest.skip("no icon asset for {!r}".format(name))
            assert isinstance(entry, tuple) and len(entry) == 2, (
                "IconManager cache entry is {!r}; it must record "
                "(root, image)".format(entry))
            assert entry[0] is root, "entry did not record its root"
        finally:
            root.destroy()

    def test_tag_icon_cache_entries_record_their_root(self):
        import tkinter as tk
        import main as MAIN
        try:
            root = tk.Tk()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        root.withdraw()
        try:
            MAIN.tag_icon("Budget")
            entry = MAIN._TAG_ICON_CACHE.get("Budget")
            if entry is None:
                pytest.skip("no tag icon asset available")
            assert isinstance(entry, tuple) and len(entry) == 2, (
                "_TAG_ICON_CACHE entry is {!r}; it must record "
                "(root, image)".format(entry))
            assert entry[0] is root, "entry did not record its root"
        finally:
            root.destroy()


# ---------------------------------------------------------------------------
# Post-audit review fixes
# ---------------------------------------------------------------------------
class TestCoerceIntCommaHandling:
    """A comma is removed only when it is a real thousands separator.
    Deleting a decimal comma silently rescaled values by 10-100x."""

    def test_thousands_separators_still_accepted(self):
        import db_logic as L
        assert L.coerce_int("$1,299") == 1299
        assert L.coerce_int("1,299") == 1299
        assert L.coerce_int("$1,299.50") == 1300
        assert L.coerce_int("12,345,678") == 12345678
        assert L.coerce_int("1,299 ohm") == 1299

    def test_decimal_commas_are_rejected_not_rescaled(self):
        import db_logic as L
        for text in ("12,50", "1,5", "0,5", "1,29,999", "1,2999", ",500"):
            assert L.coerce_int(text) == 0, text
            assert L.coerce_int(text, default=-1) == -1, text

    def test_plain_values_unchanged(self):
        import db_logic as L
        assert L.coerce_int("1299") == 1299
        assert L.coerce_int("18 ohm") == 18
        assert L.coerce_int("high") == 0


class TestGlobalShortcutsRespectTextFields:
    """Ctrl+Z / Ctrl+Y / Ctrl+H are bound on the toplevel, so without a
    focus guard they fire from inside a form field and roll back the last
    committed database operation while the user is only fixing a typo."""

    @pytest.fixture()
    def app(self):
        import main as MAIN
        try:
            a = MAIN.MainApp()
        except Exception:
            pytest.skip("no display for Tk integration tests")
        a.deiconify()
        a.update()
        yield a
        try:
            a.destroy()
        except Exception:
            pass

    def _spy(self, app):
        calls = []
        app.undo_last = lambda: calls.append("undo")
        app.redo_last = lambda: calls.append("redo")
        app.open_find_replace = lambda: calls.append("find")
        return calls

    def test_undo_redo_and_ctrl_h_are_ignored_inside_a_text_field(self, app):
        calls = self._spy(app)
        app.editor.brand_entry.entry.focus_force()
        app.update()
        for seq in ("<Control-z>", "<Control-y>", "<Control-h>"):
            app.editor.brand_entry.entry.event_generate(seq)
            app.update()
        assert calls == []

    def test_ctrl_f_still_opens_find_from_a_text_field(self, app):
        calls = self._spy(app)
        app.editor.brand_entry.entry.focus_force()
        app.update()
        app.editor.brand_entry.entry.event_generate("<Control-f>")
        app.update()
        assert calls == ["find"]

    def test_shortcuts_still_work_when_focus_is_not_in_a_text_field(self, app):
        calls = self._spy(app)
        app.notebook.focus_force()
        app.update()
        app.event_generate("<Control-z>")
        app.event_generate("<Control-y>")
        app.event_generate("<Control-h>")
        app.update()
        assert calls == ["undo", "redo", "find"]
