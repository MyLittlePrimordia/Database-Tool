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
from main import ellipsize, ellipsize_path, entry_matches_query  # noqa: E402


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
# DL-3 / L-1 / L-2 / M-8: atomic persistence
# ===========================================================================
class TestAtomicPersistence:
    def test_save_leaves_no_tmp_and_round_trips(self, tmpdb):
        entries = [L.build_clean_entry(make_entry())]
        L.save_database(tmpdb, entries)
        assert not os.path.exists(tmpdb + ".tmp")
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
        assert not os.path.exists(bak + ".tmp")
        assert open(bak, "rb").read() == open(tmpdb, "rb").read()

    def test_curve_write_output_atomic(self, tmp_path):
        out = str(tmp_path / "curve.txt")
        CL.write_output(out, [(20.0, 50.5), (100.0, 52.0)])
        assert not os.path.exists(out + ".tmp")
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
        # unwritable target -> exception, no .tmp residue
        bad = str(tmp_path / "missing_dir" / "x.json")
        with pytest.raises(OSError):
            L.write_text_atomic(bad, "data")
        assert not os.path.exists(bad + ".tmp")


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
            L._SCAN_CACHE[:] = (None, 0.0, None, None)
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
