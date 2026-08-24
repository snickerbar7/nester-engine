"""Contract shapes: native engine vocabulary vs the frozen Harriet mapping.

The Harriet assertions are snapshot-style on the FIELD NAMES — Harriet parses
these keys (packages/domain/src/nesting/types.ts), so a rename here is a
production break, not a refactor.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("boto3")

from nester.tube.model import Part, StockSpec
from nester.tube.packing import pack_profile

from service.core.engine import profile_result_to_dict
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
        "profile", "bars_needed", "stock_length_mm", "usable_length_mm",
        "total_part_length_mm", "total_drop_mm", "yield_pct", "bars", "unplaceable",
    }
    assert set(d["bars"][0]) == {"bar_index", "pieces_mm", "drop_mm"}
    assert d["bars_needed"] == 1
    assert d["stock_length_mm"] == 6000
    assert d["usable_length_mm"] == 5700
    assert d["bars"][0]["bar_index"] == 1
    assert d["bars"][0]["pieces_mm"] == [2000.0, 1500.0, 1200.0]


def test_native_unplaceable_shape():
    parts = [Part(name="too_long", profile="p", length=9000.0)]
    res = pack_profile(parts, StockSpec(profile="p", stock_length=6000))
    d = profile_result_to_dict(res)
    assert d["bars_needed"] == 0
    assert d["unplaceable"] == [
        {"label": "too_long", "profile": "p", "qty": 1, "length_mm": 9000.0}]


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
