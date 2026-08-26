"""E23 — the sheet-count search, the free-area top-up, and the net/gross totals.

What is under test is a DECISION, not a layout: how few new sheets the job can
be done in, and whether the shop's rack is worth opening to get there. The
underlying solve is stochastic and wall-clock budgeted, so nothing here asserts
a coordinate; it asserts the invariants the product hangs on —

* the search never buys more than the greedy walk it replaces;
* offering a job material the shop already paid for may not make any headline
  number worse (the failure this whole round exists to fix);
* a part topped up into a sheet's free area is an ORDINARY part — flagging it
  ``in_hole_of`` would tell the operator to go looking for a slug that is not
  there;
* running out of attempts, time or ceiling returns the best FEASIBLE nest, never
  a failure.

The pure-arithmetic parts (totals, region filtering) are tested exactly, on
hand-built results, because they are pure arithmetic.
"""

import math

import pytest
from shapely.geometry import Polygon

from nester.sheet.holes import (
    DEFAULT_MIN_HOLE_SIDE, nest_into_free_area, nest_into_holes,
)
from nester.sheet.model import (
    ExtraSheet, FlatPart, Leftover, NestResult, Placement, SheetLayout, SheetSpec,
)
from nester.sheet.pack import (
    DEFAULT_MAX_NEW_SHEETS, MAX_SEARCH_ATTEMPTS, nest, transform,
)
from nester.sheet.result_json import as_dict


def rect(w, h, x=0.0, y=0.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _poly(pl):
    return Polygon(transform(pl.part.outer, pl.rotation, pl.x, pl.y))


def _assert_no_collisions(layout):
    polys = [_poly(pl) for pl in layout.placements]
    hosts = {i: pl.in_hole_of for i, pl in enumerate(layout.placements)}
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if hosts[j] == i or hosts[i] == j:
                continue                     # a guest sits inside its host
            assert polys[i].intersection(polys[j]).area < 1e-3


# --------------------------------------------------------------------------- #
# The search itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seed", [0, 2, 5])
def test_the_search_never_buys_more_than_the_greedy_walk(seed):
    """The floor under everything: turning the search on may not cost a sheet.

    Note what is NOT claimed here. On rectangle-heavy jobs spyrrow already packs
    each sheet to the area floor, so the greedy walk usually cannot be beaten on
    COUNT and the ceiling ladder's job is to PROVE that rather than improve it
    (see test_the_ceiling_search_proves_the_count_is_at_the_floor). What the
    search does improve on such a job is where the tail sits — which is the next
    test — and whether the rack gets spent for nothing.
    """
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=3)
    parts = [FlatPart("placa.dxf", rect(470, 470), qty=4),
             FlatPart("tira.dxf", rect(460, 40), qty=8),
             FlatPart("taco.dxf", rect(90, 90), qty=12)]

    greedy = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=seed,
                  minimize_sheets=False, min_remnant=200)
    searched = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=seed,
                    min_remnant=200)

    assert sum(s.part_count for s in searched.sheets) == 24
    assert sum(s.part_count for s in greedy.sheets) == 24
    assert searched.new_sheets_needed <= greedy.new_sheets_needed
    assert searched.search.enabled and searched.search.attempts >= 1
    for layout in searched.sheets:
        _assert_no_collisions(layout)


def test_the_free_area_top_up_pulls_the_tail_forward():
    """The measurable win on a job the greedy walk already counts correctly.

    spyrrow can only be handed a fresh strip, so parts it leaves in the gaps of
    sheet 1 are stranded there forever. The top-up puts them back, which
    consolidates the tail onto fewer columns of the LAST sheet and turns what
    was scattered drop into one shearable offcut.
    """
    spec = SheetSpec(width=1200, height=900, margin=8, part_gap=4)
    parts = [FlatPart("placa.dxf", rect(560, 420), qty=6),
             FlatPart("taco.dxf", rect(150, 110), qty=24)]

    greedy = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  minimize_sheets=False, min_remnant=150)
    searched = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                    min_remnant=150)

    assert sum(s.part_count for s in searched.sheets) == 30
    # same money, and the last sheet is emptier -> a bigger piece goes back
    assert searched.new_sheets_needed <= greedy.new_sheets_needed
    assert searched.sheets[-1].part_count <= greedy.sheets[-1].part_count
    assert searched.net_yield_pct >= greedy.net_yield_pct - 1e-9
    for layout in searched.sheets:
        _assert_no_collisions(layout)


def test_the_ceiling_search_proves_the_count_is_at_the_floor():
    """When the ladder stops at the area floor it has not merely failed to do
    better — it has proved no packing can. The plan is entitled to say so."""
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=3)
    parts = [FlatPart("placa.dxf", rect(470, 470), qty=9)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)

    assert result.new_sheets_needed == result.search.area_floor_sheets
    assert result.search.ceiling_used == result.new_sheets_needed
    assert result.search.capped is False


def test_the_search_never_reports_a_count_below_the_area_floor():
    """The floor is physics: net part area over one sheet's usable area. A
    result under it would mean the nest overlapped something."""
    spec = SheetSpec(width=600, height=600, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(280, 280), qty=9)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)

    assert result.search.area_floor_sheets >= 1
    assert result.new_sheets_needed >= result.search.area_floor_sheets
    assert result.search.ceiling_used == result.new_sheets_needed


def test_minimize_sheets_off_is_the_old_greedy_walk_untouched():
    spec = SheetSpec(width=500, height=400, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(80, 40), qty=20)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=1,
                  minimize_sheets=False)

    assert result.search.enabled is False
    assert result.search.attempts == 0
    assert result.search.ceiling_tried == ()
    assert sum(s.part_count for s in result.sheets) == 20


def test_the_search_is_bounded_and_returns_the_best_feasible_result():
    """A ceiling too low to be met is not a failure. With max_new_sheets pinned
    to 1 on a job that needs several, the search runs out of road, falls back to
    the walk that always worked, flags capped — and still cuts every part."""
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(180, 130), qty=12)]

    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  max_new_sheets=1)

    assert result.search.capped is True
    assert result.new_sheets_needed > 1            # the ceiling could not be met
    assert sum(s.part_count for s in result.sheets) == 12   # nothing dropped
    assert not result.unplaceable
    # -1 marks the unbounded fallback, so a real ceiling of 0 stays unambiguous
    assert -1 in result.search.ceiling_tried
    assert result.search.ceiling_used is None
    assert result.search.attempts <= MAX_SEARCH_ATTEMPTS


def test_a_zero_second_budget_is_not_a_budget():
    """0 means "no cap", not "no time" — a job must never come back empty
    because someone left the default in."""
    spec = SheetSpec(width=500, height=400, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(80, 40), qty=12)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  search_budget_s=0)
    assert sum(s.part_count for s in result.sheets) == 12


def test_the_search_stays_within_its_attempt_budget():
    spec = SheetSpec(width=900, height=700, margin=5, part_gap=3)
    parts = [FlatPart("a.dxf", rect(300, 200), qty=30),
             FlatPart("b.dxf", rect(120, 90), qty=40)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=5)
    assert 1 <= result.search.attempts <= MAX_SEARCH_ATTEMPTS
    assert len(result.search.ceiling_tried) == result.search.attempts


# --------------------------------------------------------------------------- #
# The invariant this round exists for
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seed", [0, 3, 11])
def test_offering_a_rack_never_makes_the_job_worse(seed):
    """No headline metric and no purchase count may get worse because the job
    used material the shop had already paid for.

    Both directions are checked: the rack must not raise sheets_to_buy, and it
    must not lower net yield. The old engine failed the second one by spending
    offcuts that bought nothing back.
    """
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=3)
    parts = [FlatPart("placa.dxf", rect(480, 480), qty=6)]
    rack = [ExtraSheet(520, 400, "R-a"), ExtraSheet(300, 250, "R-b")]

    plain = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=seed,
                 min_remnant=200)
    racked = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=seed,
                  extra_sheets=rack, min_remnant=200)

    assert racked.new_sheets_needed <= plain.new_sheets_needed
    assert racked.net_yield_pct >= plain.net_yield_pct - 1e-9
    # and every offcut is still accounted for, opened or not
    assert (set(racked.remnants_used) | {e.label for e in racked.remnants_unused}
            == {"R-a", "R-b"})


def test_a_declined_retazo_says_why_and_is_reported_in_the_json():
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(80, 40), qty=40)]
    rack = [ExtraSheet(400, 300, "R-a")]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=rack, min_remnant=150)

    assert result.remnants_used == []
    body = as_dict(result, "job", {"min_remnant": 150})
    assert body["remnants_unused"] == [
        {"label": "R-a", "width": 400, "height": 300, "reason": "no_gain"}]


# --------------------------------------------------------------------------- #
# The free-area top-up
# --------------------------------------------------------------------------- #

def _sheet_with_one_part(w=500, h=400, gap=3.0):
    spec = SheetSpec(width=w, height=h, margin=10, part_gap=gap)
    host = FlatPart("host.dxf", rect(200, 200), holes=(rect(120, 120, 40, 40),))
    layout = SheetLayout(index=0, spec=spec,
                         placements=[Placement(part=host, x=10, y=10)])
    return spec, layout


def test_free_area_placements_are_ordinary_parts_not_slugs():
    """A part dropped into the empty part of a sheet is NOT inside a slug. If it
    carried in_hole_of the plan would tell the operator to fish it out of a
    cut-out that does not exist, and it would inflate parts_in_holes."""
    spec, layout = _sheet_with_one_part()
    guest = FlatPart("guest.dxf", rect(60, 60))
    catalog = {"g": guest}
    remaining = {"g": 4}
    orient = {"g": (0.0, 90.0)}

    placed = nest_into_free_area(
        layout, catalog, remaining, orient, spec.part_gap,
        (10, 10, spec.width - 10, spec.height - 10), min_side=0.0)

    assert placed == 4 and remaining["g"] == 0
    guests = layout.placements[1:]
    assert all(pl.in_hole_of is None for pl in guests)
    assert layout.in_hole_count == 0
    _assert_no_collisions(layout)


def test_free_area_respects_the_gap_and_the_usable_rectangle():
    spec, layout = _sheet_with_one_part(gap=5.0)
    guest = FlatPart("guest.dxf", rect(70, 70))
    remaining = {"g": 6}
    nest_into_free_area(layout, {"g": guest}, remaining, {"g": (0.0,)},
                        spec.part_gap,
                        (10, 10, spec.width - 10, spec.height - 10), min_side=0.0)

    host = _poly(layout.placements[0])
    for pl in layout.placements[1:]:
        poly = _poly(pl)
        assert poly.distance(host) >= spec.part_gap - 1e-6
        x0, y0, x1, y1 = poly.bounds
        assert x0 >= 10 - 1e-6 and y0 >= 10 - 1e-6
        assert x1 <= spec.width - 10 + 1e-6 and y1 <= spec.height - 10 + 1e-6


def test_free_area_only_ever_appends():
    """It may lose a placement to a coarse grid; it may never move or drop one
    that the solver already made."""
    spec, layout = _sheet_with_one_part()
    before = [(pl.part.name, pl.x, pl.y, pl.rotation) for pl in layout.placements]
    guest = FlatPart("guest.dxf", rect(50, 50))
    nest_into_free_area(layout, {"g": guest}, {"g": 3}, {"g": None},
                        spec.part_gap,
                        (10, 10, spec.width - 10, spec.height - 10))
    after = [(pl.part.name, pl.x, pl.y, pl.rotation) for pl in layout.placements]
    assert after[:len(before)] == before


def test_min_hole_side_keeps_a_bolt_hole_from_counting_as_surface():
    """A washer would 'fit' a 25 mm hole. Treating that as usable surface costs
    a full grid sweep per hole and produces a part inside a slug for nothing."""
    spec = SheetSpec(width=400, height=400, margin=5, part_gap=1)
    host = FlatPart("host.dxf", rect(200, 200), holes=(rect(25, 25, 80, 80),))
    tiny = FlatPart("tiny.dxf", rect(10, 10))

    def run(min_side):
        layout = SheetLayout(index=0, spec=spec,
                             placements=[Placement(part=host, x=5, y=5)])
        remaining = {"t": 2}
        n = nest_into_holes(layout, {"t": tiny}, remaining, {"t": (0.0,)},
                            spec.part_gap, min_side)
        return n, remaining["t"]

    assert run(0.0)[0] > 0                     # the hole is physically usable
    assert run(DEFAULT_MIN_HOLE_SIDE) == (0, 2)   # 25 mm < 30 mm: not surface


def test_min_hole_side_is_carried_from_the_job_into_the_pass():
    spec = SheetSpec(width=1000, height=800, margin=5, part_gap=2)
    host = FlatPart("host.dxf", rect(300, 300), holes=(rect(28, 28, 130, 130),), qty=4)
    tiny = FlatPart("tiny.dxf", rect(12, 12), qty=8)

    strict = nest([host, tiny], spec, rotation="ortho", time_per_sheet=1, seed=0,
                  nest_in_holes=True, min_hole_side=30.0)
    loose = nest([host, tiny], spec, rotation="ortho", time_per_sheet=1, seed=0,
                 nest_in_holes=True, min_hole_side=0.0)

    assert strict.in_hole_count == 0
    assert loose.in_hole_count >= strict.in_hole_count


# --------------------------------------------------------------------------- #
# Totals arithmetic — exact, on a hand-built result
# --------------------------------------------------------------------------- #

def _hand_built():
    """One new 1000x1000 sheet and one 400x400 retazo, with known areas.

    parts     = 200x200 x2 (new sheet) + 100x100 (retazo)  = 90 000 mm2
    stock     = 1 000 000 + 160 000                        = 1 160 000 mm2
    returned  = a 300x300 offcut on the new sheet          = 90 000 mm2
    """
    spec = SheetSpec(width=1000, height=1000, margin=0, part_gap=0,
                     material="acero", thickness=2, density=8000)
    big = FlatPart("big.dxf", rect(200, 200))
    small = FlatPart("small.dxf", rect(100, 100))
    new = SheetLayout(index=0, spec=spec, placements=[
        Placement(part=big, x=0, y=0), Placement(part=big, x=300, y=0)])
    new.leftover = Leftover(x=600, y=0, width=300, height=300)
    rem = SheetLayout(index=1, spec=spec.resized(400, 400), source="R-1",
                      placements=[Placement(part=small, x=0, y=0)])
    return NestResult(spec=spec, sheets=[new, rem])


def test_the_net_and_gross_totals_are_the_designs_formulas():
    r = _hand_built()
    assert r.total_part_area == 90_000
    assert r.total_sheet_area == 1_160_000
    assert r.reclaimable_area == 90_000
    assert r.consumed_area == 1_160_000 - 90_000
    assert r.waste_area == 1_160_000 - 90_000 - 90_000
    assert r.gross_yield_pct == pytest.approx(100 * 90_000 / 1_160_000)
    assert r.net_yield_pct == pytest.approx(100 * 90_000 / 1_070_000)
    # gross is the frozen number under its original name; net is strictly kinder
    assert r.yield_pct == r.gross_yield_pct
    assert r.net_yield_pct > r.gross_yield_pct


def test_the_weights_split_bought_from_rack_and_returned_from_lost():
    r = _hand_built()
    # 8000 kg/m3 x 2 mm  ->  0.016 kg per 1000 mm2
    kg = lambda mm2: mm2 * 2 * 8000 / 1e12 * 1000
    assert r.parts_weight_kg == pytest.approx(kg(90_000))
    assert r.rack_stock_weight_kg == pytest.approx(kg(160_000))
    assert r.reclaimable_weight_kg == pytest.approx(kg(90_000))
    assert r.waste_weight_kg == pytest.approx(kg(980_000))
    # the frozen drop is everything that does not leave as a part; waste is
    # that MINUS what goes back on the rack. They are not the same number.
    assert r.drop_weight_kg == pytest.approx(kg(1_070_000))
    assert r.waste_weight_kg < r.drop_weight_kg


def test_the_json_carries_the_new_totals_and_never_renames_the_old_ones():
    r = _hand_built()
    body = as_dict(r, "job", {"min_remnant": 200, "kerf": 0.2,
                              "minimize_sheets": True, "max_new_sheets": 40,
                              "min_hole_side": 30})
    t = body["totals"]
    assert t["yield_pct"] == round(r.yield_pct, 2)           # FROZEN
    assert t["drop_kg"] == round(r.drop_weight_kg, 3)        # FROZEN
    assert t["net_yield_pct"] == round(r.net_yield_pct, 2)
    assert t["part_area_mm2"] == 90_000
    assert t["stock_area_mm2"] == 1_160_000
    assert t["new_stock_area_mm2"] == 1_000_000
    assert t["consumed_area_mm2"] == 1_070_000
    assert t["waste_area_mm2"] == 980_000
    assert {"from_rack_kg", "leftover_kg", "waste_kg"} <= set(t)
    assert body["params"]["kerf_mm"] == 0.2
    assert body["params"]["minimize_sheets"] is True
    assert body["params"]["min_hole_side_mm"] == 30
    assert [s["part_area_mm2"] for s in body["sheets"]] == [80_000, 10_000]
    assert [s["leftover_kind"] for s in body["sheets"]] == ["rack", "scrap"]


def test_weights_stay_absent_when_the_job_cannot_be_weighed():
    """Absent, never zero: an invented kilo figure becomes a wrong purchase."""
    r = _hand_built()
    r.spec = SheetSpec(width=1000, height=1000, margin=0, part_gap=0)
    body = as_dict(r, "job", {"min_remnant": 0})
    for key in ("parts_kg", "drop_kg", "from_rack_kg", "leftover_kg", "waste_kg"):
        assert key not in body["totals"]
    # min_remnant 0 turns leftover reporting off entirely — that is "not
    # measured", not "there is no offcut"
    assert all(s["leftover_kind"] is None for s in body["sheets"])


def test_unplaceable_parts_keep_their_quantity_on_the_way_out():
    spec = SheetSpec(width=300, height=200, margin=5)
    parts = [FlatPart("grande.dxf", rect(400, 400), qty=7),
             FlatPart("ok.dxf", rect(20, 20), qty=1)]
    result = nest(parts, spec, rotation="free", time_per_sheet=1, seed=0)
    body = as_dict(result, "job", {})

    assert [(u["name"], u["qty"]) for u in body["unplaceable"]] == [("grande.dxf", 7)]


def test_the_search_block_is_reported():
    spec = SheetSpec(width=500, height=400, margin=5, part_gap=2)
    result = nest([FlatPart("a.dxf", rect(80, 40), qty=10)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0)
    search = as_dict(result, "job", {})["totals"]["search"]

    assert search["enabled"] is True
    assert search["attempts"] == len(search["ceiling_tried"]) >= 1
    assert search["ceiling_used"] == result.new_sheets_needed
    assert search["capped"] is False
    assert search["area_floor_sheets"] >= 1
