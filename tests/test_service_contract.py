"""Contract shapes: native engine vocabulary vs the frozen Harriet mapping.

The Harriet assertions are snapshot-style on the FIELD NAMES — Harriet parses
these keys (packages/domain/src/nesting/types.ts), so a rename here is a
production break, not a refactor.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("boto3")

from nester.tube.model import ExtraStock, Part, StockSpec
from nester.tube.packing import pack_profile

from service.core.engine import _build_specs, profile_result_to_dict
from service.harriet.routes import to_extract_response, to_nest_response, to_profile_plan


def packed():
    parts = [Part(name=f"p{i}", profile="2x2_c18", length=L)
             for i, L in enumerate([2000.0, 1500.0, 1200.0])]
    spec = StockSpec(profile="2x2_c18", stock_length=6000, kerf=3, back_trim=300)
    return pack_profile(parts, spec)


# --- native (/v1) shape ---------------------------------------------------- #

def test_native_profile_is_snake_case_engine_vocabulary():
    d = profile_result_to_dict(packed())
    assert set(d) == {
        "profile", "bars_needed", "new_bars_needed", "remnants_used",
        "remnants_unused", "stock_length_mm", "usable_length_mm",
        "total_part_length_mm", "total_drop_mm", "yield_pct",
        "net_yield_pct", "gross_yield_pct", "min_remnant_mm",
        "reclaimable_mm", "waste_mm", "bars", "unplaceable",
    }
    assert set(d["bars"][0]) == {
        "bar_index", "stock_length_mm", "source", "pieces_mm", "pieces", "drop_mm",
        "leftover_mm", "waste_mm"}
    # The net/gross pair is additive: yield_pct keeps its exact value, and with
    # no min_remnant nothing is reclaimable, so net == gross == yield_pct.
    assert d["min_remnant_mm"] == 0
    assert d["reclaimable_mm"] == 0
    assert d["gross_yield_pct"] == d["yield_pct"] == d["net_yield_pct"]
    assert d["waste_mm"] == round(6000 - d["total_part_length_mm"], 4)
    assert d["bars_needed"] == 1
    assert d["new_bars_needed"] == 1
    assert d["remnants_used"] == []
    assert d["stock_length_mm"] == 6000
    assert d["usable_length_mm"] == 5700
    assert d["bars"][0]["bar_index"] == 1
    assert d["bars"][0]["stock_length_mm"] == 6000
    assert d["bars"][0]["source"] == "nuevo"
    assert d["bars"][0]["pieces_mm"] == [2000.0, 1500.0, 1200.0]
    # ``pieces`` is additive detail alongside ``pieces_mm`` (unchanged above).
    # Nothing declared (no orientation, no end_features) -> defaults, not
    # absent fields.
    assert set(d["bars"][0]["pieces"][0]) == {
        "length_mm", "label", "orientation_deg", "end_features",
        "gap_before", "gap_reason"}
    assert [p["length_mm"] for p in d["bars"][0]["pieces"]] == [2000.0, 1500.0, 1200.0]
    assert [p["label"] for p in d["bars"][0]["pieces"]] == ["p0", "p1", "p2"]
    assert all(p["orientation_deg"] == 0.0 for p in d["bars"][0]["pieces"])
    assert all(p["end_features"] == {} for p in d["bars"][0]["pieces"])
    # First piece on a bar: no joint precedes it.
    assert d["bars"][0]["pieces"][0]["gap_before"] == 0.0
    assert d["bars"][0]["pieces"][0]["gap_reason"] == []
    # Later pieces DO carry the joint before them: kerf is always charged in
    # gap_before, but a plain kerf-only gap between two ordinary parts isn't
    # one of the NAMED reasons (those flag interleaving/shared-face/extra-gap
    # charges), so gap_reason stays empty here — matching report.py exactly.
    assert d["bars"][0]["pieces"][1]["gap_before"] == 3.0
    assert d["bars"][0]["pieces"][1]["gap_reason"] == []


def test_native_profile_pieces_carry_orientation_and_end_features():
    """The 'tramo desdoblado' view needs the per-piece detail report.py already
    writes to the artifact — orientation_deg, end_features, the joint before."""
    from nester.tube.model import EndFeature

    parts = [
        Part(name="p0", profile="2x2_c18", length=2000.0, orientation_deg=90.0,
             end_features={"start": EndFeature(protrusion_mm=6.0, faces=(1, 3))}),
        Part(name="p1", profile="2x2_c18", length=1500.0),
    ]
    spec = StockSpec(profile="2x2_c18", stock_length=6000, kerf=3, back_trim=300)
    d = profile_result_to_dict(pack_profile(parts, spec))
    p0 = d["bars"][0]["pieces"][0]
    assert p0["label"] == "p0"
    assert p0["orientation_deg"] == 90.0
    assert p0["end_features"] == {"start": {"protrusion_mm": 6.0, "faces": [1, 3]}}


def test_native_profile_reports_remnant_stock():
    """E9: a bar cut from a retazo names it, and it doesn't count as a purchase."""
    parts = [Part(name=f"p{i}", profile="2x2_c18", length=L)
             for i, L in enumerate([2000.0, 1500.0, 1200.0])]
    # 2.5 m tramos: here the retazo genuinely removes one from the order (3 -> 2)
    # so it is opened. One that bought nothing would be declined and reported
    # under remnants_unused with reason "no_gain".
    spec = StockSpec(profile="2x2_c18", stock_length=2500, kerf=3, back_trim=300,
                     extra_stock=(ExtraStock(length=2400.0, label="R-0001"),))
    d = profile_result_to_dict(pack_profile(parts, spec))
    assert d["bars_needed"] == 3            # total bars used
    assert d["new_bars_needed"] == 2        # two tramos to buy, not three
    assert d["remnants_used"] == ["R-0001"]
    by_source = {b["source"]: b for b in d["bars"]}
    assert set(by_source) == {"nuevo", "R-0001"}
    assert by_source["R-0001"]["stock_length_mm"] == 2400.0
    assert by_source["R-0001"]["pieces_mm"] == [2000.0]


def test_native_unplaceable_shape():
    parts = [Part(name="too_long", profile="p", length=9000.0)]
    res = pack_profile(parts, StockSpec(profile="p", stock_length=6000))
    d = profile_result_to_dict(res)
    assert d["bars_needed"] == 0
    assert d["unplaceable"] == [
        {"label": "too_long", "profile": "p", "qty": 1, "length_mm": 9000.0}]


# --- extra stock -> specs (E9) --------------------------------------------- #

def _specs(profiles, extra):
    return _build_specs(profiles, 6000, None, 3, 0, 300, extra)


def test_extra_stock_lands_on_the_matching_profile_spec():
    specs, warnings = _specs(
        {"2x2_c18", "d32"},
        [{"profile": "2x2_c18", "length_mm": 2140, "label": "R-0001"},
         {"profile": "2x2_c18", "length_mm": 900, "label": "R-0002"}])
    assert warnings == []
    assert specs["2x2_c18"].extra_stock == (
        ExtraStock(length=2140.0, label="R-0001"),
        ExtraStock(length=900.0, label="R-0002"))
    assert specs["d32"].extra_stock == ()


def test_extra_stock_profile_is_normalized_like_a_parsed_filename():
    specs, warnings = _specs(
        {"2x2_c18"}, [{"profile": "2X2_C18", "length_mm": 2140, "label": "R-0001"}])
    assert warnings == []
    assert specs["2x2_c18"].extra_stock[0].label == "R-0001"


def test_extra_stock_for_a_profile_not_in_the_job_is_a_warning_not_an_error():
    specs, warnings = _specs(
        {"2x2_c18"}, [{"profile": "50x50x2", "length_mm": 2140, "label": "R-0001"}])
    assert specs["2x2_c18"].extra_stock == ()
    assert warnings == [
        "retazo R-0001 ignorado: perfil '50x50x2' no está en el trabajo"]


# --- frozen Harriet shape -------------------------------------------------- #

def test_harriet_profile_plan_field_names_unchanged():
    native = profile_result_to_dict(packed())
    plan = to_profile_plan(native)
    assert set(plan) == {
        "profile", "bars", "barsNeeded", "totalPartLength", "totalDrop",
        "yieldPct", "usableLength", "unplaceable",
    }
    assert set(plan["bars"][0]) == {"barIndex", "pieces", "drop"}
    assert plan["barsNeeded"] == native["bars_needed"]
    assert plan["usableLength"] == native["usable_length_mm"]
    assert plan["totalPartLength"] == native["total_part_length_mm"]
    assert plan["totalDrop"] == native["total_drop_mm"]
    assert plan["yieldPct"] == native["yield_pct"]
    assert plan["bars"][0]["barIndex"] == 1
    assert plan["bars"][0]["pieces"] == native["bars"][0]["pieces_mm"]
    assert plan["bars"][0]["drop"] == native["bars"][0]["drop_mm"]


def test_harriet_plan_ignores_the_new_remnant_fields():
    """E9 is native-only: Harriet's camelCase shape must not grow fields."""
    parts = [Part(name="p0", profile="p", length=2000.0)]
    spec = StockSpec(profile="p", stock_length=6000,
                     extra_stock=(ExtraStock(length=2400.0, label="R-0001"),))
    native = profile_result_to_dict(pack_profile(parts, spec))
    assert native["remnants_used"] == ["R-0001"]      # the nest did use it
    plan = to_profile_plan(native)
    assert set(plan) == {
        "profile", "bars", "barsNeeded", "totalPartLength", "totalDrop",
        "yieldPct", "usableLength", "unplaceable",
    }
    assert set(plan["bars"][0]) == {"barIndex", "pieces", "drop"}
    assert plan["barsNeeded"] == 1                    # still TOTAL bars used


def test_harriet_unplaceable_field_names_unchanged():
    res = pack_profile([Part(name="too_long", profile="p", length=9000.0)],
                       StockSpec(profile="p", stock_length=6000))
    plan = to_profile_plan(profile_result_to_dict(res))
    assert plan["unplaceable"] == [
        {"length": 9000.0, "profile": "p", "qty": 1, "label": "too_long"}]


def test_harriet_nest_envelope_unchanged():
    native = {
        "mode": "tube", "unit": "mm",
        "result": {"bars_total": 1, "profiles": [profile_result_to_dict(packed())]},
        "artifacts": [{"key": "records/c/r/plan.pdf", "filename": "plan.pdf",
                       "content_type": "application/pdf", "size": 12}],
        "errors": [],
    }
    out = to_nest_response(native)
    assert set(out) == {"mode", "unit", "result", "artifacts", "errors", "warnings"}
    assert set(out["result"]) == {"profiles", "barsTotal"}
    assert out["result"]["barsTotal"] == 1
    assert out["artifacts"] == [{"key": "records/c/r/plan.pdf", "filename": "plan.pdf",
                                 "contentType": "application/pdf", "size": 12}]


def test_harriet_sheet_nest_passes_engine_json_through():
    native = {"mode": "sheet", "result": {"job": "j", "totals": {"sheets": 2}},
              "artifacts": [], "errors": ["a.dxf: boom"]}
    out = to_nest_response(native)
    assert out == {"mode": "sheet", "result": {"job": "j", "totals": {"sheets": 2}},
                   "artifacts": [], "errors": ["a.dxf: boom"], "warnings": []}


def test_harriet_extract_field_names_unchanged():
    tube = to_extract_response({
        "mode": "tube",
        "parts": [{"label": "a.igs", "profile": "2x2_c18", "qty": 4, "length_mm": 302.0}],
        "profiles": ["2x2_c18"], "errors": [],
    })
    assert tube == {"mode": "tube",
                    "parts": [{"length": 302.0, "profile": "2x2_c18", "qty": 4, "label": "a.igs"}],
                    "profiles": ["2x2_c18"], "errors": []}

    sheet = to_extract_response({
        "mode": "sheet",
        "parts": [{"label": "a.dxf", "qty": 2, "width_mm": 100.0, "height_mm": 50.0,
                   "area_mm2": 4800.0, "holes": 3}],
        "errors": [],
    })
    assert sheet == {"mode": "sheet",
                     "parts": [{"label": "a.dxf", "qty": 2, "width": 100.0, "height": 50.0,
                                "area": 4800.0, "holes": 3}],
                     "errors": []}
