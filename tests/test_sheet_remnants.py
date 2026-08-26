"""E16 — retazos de lámina (extra sheet stock) + the reclaimable offcut.

Two halves of the same idea: the rack is stock too, and the drop is next week's
rack. The policy mirrors the tube tool's E9 (see ``tests/test_remnants.py``):
each offcut is one physical piece, usable ONCE, spent SMALLEST FIRST so a big
retazo stays free for the big part that has nowhere else to go, and a piece that
ends up holding nothing is left on the rack instead of being burned.

The 2D engine is wall-clock budgeted and stochastic, so nothing here asserts an
exact sheet count or a placement coordinate. It asserts invariants: consumption
order, conservation of the rack, per-sheet containment against each sheet's OWN
size, and area accounting that sums real sheets instead of multiplying a count.
``reclaimable_rectangle`` is pure geometry, so it is tested on hand-built
layouts, exactly.
"""

import pytest
from shapely.geometry import Polygon

from nester.sheet.model import (
    NEW_SHEET, ExtraSheet, FlatPart, Leftover, Placement, SheetLayout, SheetSpec,
)
from nester.sheet.pack import nest, reclaimable_rectangle, transform


def rect(w, h, x=0.0, y=0.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _poly(pl):
    return Polygon(transform(pl.part.outer, pl.rotation, pl.x, pl.y))


def _assert_each_sheet_holds_its_own_stock(result):
    """Every part is inside the margins of the sheet IT sits on.

    A retazo sheet is smaller than the job's nominal sheet, so checking against
    ``result.spec`` would pass while the plan told the shop to cut past the edge
    of the piece on the rack. This checks ``sheet.spec``.
    """
    for sheet in result.sheets:
        s = sheet.spec
        polys = [_poly(pl) for pl in sheet.placements]
        for poly in polys:
            x0, y0, x1, y1 = poly.bounds
            assert x0 >= s.margin - 1e-6, sheet.source
            assert y0 >= s.margin - 1e-6, sheet.source
            assert x1 <= s.width - s.margin + 1e-6, sheet.source
            assert y1 <= s.height - s.margin + 1e-6, sheet.source
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                assert polys[i].intersection(polys[j]).area < 1e-6


def _assert_rack_is_conserved(result, extra_sheets):
    """Every offcut offered is either opened or still on the rack — never both,
    never neither."""
    used = list(result.remnants_used)
    unused = [e.label for e in result.remnants_unused]
    assert len(used) + len(unused) == len(extra_sheets)
    assert sorted(used + unused) == sorted(e.label for e in extra_sheets)
    assert set(used).isdisjoint(unused)
    assert len(set(used)) == len(used)          # each piece consumed at most once


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def test_extra_sheet_rejects_nonsense():
    with pytest.raises(ValueError):
        ExtraSheet(0, 100, "R-1")
    with pytest.raises(ValueError):
        ExtraSheet(100, -1, "R-1")
    with pytest.raises(ValueError):
        ExtraSheet(100, 100, "   ")


def test_extra_sheet_area():
    assert ExtraSheet(200, 150, "R-1").area == 30000


def test_resized_keeps_the_stock_and_changes_only_the_size():
    spec = SheetSpec(width=2440, height=1220, material="acero", thickness=3,
                     margin=8, part_gap=3, density=7850)
    small = spec.resized(600, 400)
    assert (small.width, small.height) == (600, 400)
    assert small.material == "acero" and small.thickness == 3
    assert small.margin == 8 and small.part_gap == 3 and small.density == 7850
    assert small.key == spec.key                    # same stock group


def test_resized_below_the_margins_refuses_to_exist():
    """A 12x12 offcut with an 8 mm margin has no usable area at all."""
    spec = SheetSpec(width=2440, height=1220, margin=8, part_gap=3)
    with pytest.raises(ValueError):
        spec.resized(12, 12)


# --------------------------------------------------------------------------- #
# Consumption policy
# --------------------------------------------------------------------------- #

def test_a_fitting_retazo_is_spent_before_any_new_sheet_is_bought():
    """Two parts that fit the smallest offcut: nothing to buy, hand-checked area."""
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    extra = [ExtraSheet(200, 200, "R-S"), ExtraSheet(400, 400, "R-B")]
    result = nest([FlatPart("a.dxf", rect(50, 50), qty=2)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, extra_sheets=extra)

    assert result.sheet_count == 1
    assert result.new_sheets_needed == 0
    assert result.remnants_used == ["R-S"]          # smallest that works
    assert [e.label for e in result.remnants_unused] == ["R-B"]   # stays on the rack
    assert result.sheets[0].is_remnant and result.sheets[0].source == "R-S"
    assert result.sheets[0].spec.width == 200 and result.sheets[0].spec.height == 200
    # the real sheet area, NOT sheet_count x spec.area
    assert result.total_sheet_area == 200 * 200
    assert result.total_sheet_area != result.sheet_count * spec.area
    assert result.new_sheet_area == 0
    # min_remnant defaults to off, so no sheet claims a reclaimable offcut
    assert all(s.leftover is None for s in result.sheets)
    assert result.reclaimable == [] and result.reclaimable_area == 0
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


def test_retazos_are_consumed_smallest_area_first():
    """Order is the whole policy: big offcuts stay free for big parts.

    Asserted on the greedy walk, which spends the rack unconditionally, so the
    ORDER is isolated from the separate question of WHETHER to spend it (see
    test_the_rack_is_declined_when_it_would_not_save_a_purchase).
    """
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    extra = [ExtraSheet(400, 300, "R-BIG"),      # 120 000 mm2
             ExtraSheet(200, 150, "R-SMALL"),    #  30 000 mm2
             ExtraSheet(300, 200, "R-MID")]      #  60 000 mm2
    result = nest([FlatPart("a.dxf", rect(80, 40), qty=80)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, extra_sheets=extra,
                  minimize_sheets=False)

    assert result.remnants_used == ["R-SMALL", "R-MID", "R-BIG"]
    # the retazo sheets come first, in that order, and each is its own size
    sizes = {s.source: (s.spec.width, s.spec.height) for s in result.sheets}
    assert sizes["R-SMALL"] == (200, 150)
    assert sizes["R-MID"] == (300, 200)
    assert sizes["R-BIG"] == (400, 300)
    assert result.new_sheets_needed >= 1
    assert result.new_sheets_needed == result.sheet_count - 3
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


def test_retazos_that_do_save_a_purchase_are_still_spent_smallest_first():
    """The order rule survives the decision rule: six parts need two new sheets
    on their own, and the two offcuts between them buy one of those back."""
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    extra = [ExtraSheet(600, 600, "R-BIG"), ExtraSheet(500, 500, "R-SMALL")]
    parts = [FlatPart("placa.dxf", rect(480, 480), qty=6)]

    plain = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)
    assert plain.new_sheets_needed == 2                 # 4 per sheet, 6 parts

    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=extra)
    assert result.new_sheets_needed == 1                # the rack bought one back
    assert result.remnants_used == ["R-SMALL", "R-BIG"]
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


def test_the_rack_is_declined_when_it_would_not_save_a_purchase():
    """A retazo spent for nothing is a real loss: same purchase, and the shop is
    down a physical offcut. So the nest leaves it where it is worth most.

    This is the invariant the whole search exists for — using material that was
    already paid for may never make a headline number worse.
    """
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    extra = [ExtraSheet(400, 300, "R-BIG"), ExtraSheet(200, 150, "R-SMALL")]
    parts = [FlatPart("a.dxf", rect(80, 40), qty=80)]   # comfortably one sheet

    plain = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)
    racked = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=extra, min_remnant=100)

    assert racked.new_sheets_needed <= plain.new_sheets_needed
    assert racked.remnants_used == []
    assert {e.label for e in racked.remnants_unused} == {"R-BIG", "R-SMALL"}
    # and it says WHY, so the plan can tell the shop the rack was considered
    assert set(racked.remnant_reasons.values()) == {"no_gain"}
    _assert_rack_is_conserved(racked, extra)


def test_total_area_sums_the_real_sheets_not_a_count_times_the_nominal_sheet():
    spec = SheetSpec(width=1000, height=800, margin=5, part_gap=2)   # 800 000 mm2
    extra = [ExtraSheet(500, 400, "R-ok")]                           # 200 000 mm2
    # Six parts need two new sheets on their own, so the offcut is worth
    # opening — which is the only condition under which it is opened at all.
    parts = [FlatPart("placa.dxf", rect(400, 300), qty=6)]
    assert nest(parts, spec, rotation="ortho", time_per_sheet=1,
                seed=0).new_sheets_needed == 2
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=extra)

    assert result.remnants_used == ["R-ok"]
    assert result.new_sheets_needed >= 1
    expected = 200_000 + 800_000 * result.new_sheets_needed
    assert result.total_sheet_area == pytest.approx(expected)
    assert result.total_sheet_area < result.sheet_count * spec.area
    assert result.new_sheet_area == pytest.approx(800_000 * result.new_sheets_needed)
    # yield is measured against the stock actually opened
    assert result.yield_pct == pytest.approx(
        100.0 * result.total_part_area / expected)
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


def test_a_retazo_nothing_fits_is_left_on_the_rack_never_burned_empty():
    spec = SheetSpec(width=1000, height=800, margin=5, part_gap=2)
    extra = [ExtraSheet(80, 60, "R-tiny"), ExtraSheet(500, 400, "R-ok")]
    result = nest([FlatPart("placa.dxf", rect(400, 300), qty=1)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, extra_sheets=extra)

    assert "R-tiny" not in result.remnants_used
    assert "R-tiny" in [e.label for e in result.remnants_unused]
    assert result.remnants_used == ["R-ok"]
    assert all(s.placements for s in result.sheets)      # no empty sheet emitted
    assert [s.index for s in result.sheets] == list(range(result.sheet_count))
    _assert_rack_is_conserved(result, extra)


def test_a_retazo_smaller_than_its_own_margins_is_skipped_gracefully():
    """9x9 mm with a 5 mm margin has no usable area — a warning case, not a crash."""
    spec = SheetSpec(width=1000, height=800, margin=5, part_gap=2)
    extra = [ExtraSheet(9, 9, "R-nano"), ExtraSheet(500, 400, "R-ok")]
    result = nest([FlatPart("placa.dxf", rect(400, 300), qty=1)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, extra_sheets=extra)

    assert [e.label for e in result.remnants_unused][:1] == ["R-nano"]
    assert "R-nano" not in result.remnants_used
    assert result.remnants_used == ["R-ok"]
    _assert_rack_is_conserved(result, extra)


def test_the_rack_that_outlives_the_job_is_reported_intact():
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    extra = [ExtraSheet(300, 300, "R-1"), ExtraSheet(400, 400, "R-2"),
             ExtraSheet(500, 500, "R-3")]
    result = nest([FlatPart("a.dxf", rect(60, 60), qty=2)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, extra_sheets=extra)

    assert result.remnants_used == ["R-1"]
    assert [e.label for e in result.remnants_unused] == ["R-2", "R-3"]
    assert result.new_sheets_needed == 0
    _assert_rack_is_conserved(result, extra)


def test_no_retazos_at_all_changes_nothing():
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=2)
    parts = [FlatPart("a.dxf", rect(80, 40), qty=6)]
    a = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=3, extra_sheets=[])
    assert a.sheet_count == a.new_sheets_needed >= 1
    assert a.remnants_used == [] and a.remnants_unused == []
    assert all(s.source == NEW_SHEET and not s.is_remnant for s in a.sheets)
    assert a.total_sheet_area == a.sheet_count * spec.area


def test_a_part_too_big_for_every_stock_is_reported_not_dropped():
    """The yardstick is the LARGEST stock on offer, new sheet or retazo.

    Only a part that fits none of them is unplaceable — and it must be
    REPORTED, with the rest of the job proceeding around it.
    """
    spec = SheetSpec(width=200, height=200, margin=5, part_gap=2)
    extra = [ExtraSheet(150, 150, "R-SMALL"), ExtraSheet(600, 500, "R-BIG")]
    parts = [FlatPart("gigante.dxf", rect(900, 700), qty=1),
             FlatPart("chica.dxf", rect(60, 60), qty=1)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=extra)

    assert [p.name for p in result.unplaceable] == ["gigante.dxf"]
    placed = [pl.part.name for s in result.sheets for pl in s.placements]
    assert placed == ["chica.dxf"]               # nothing silently dropped
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


def test_a_part_only_the_big_retazo_can_hold_still_lands():
    """The big offcut is kept free precisely for this."""
    spec = SheetSpec(width=200, height=200, margin=5, part_gap=2)
    extra = [ExtraSheet(150, 150, "R-SMALL"), ExtraSheet(600, 500, "R-BIG")]
    parts = [FlatPart("grande.dxf", rect(400, 300), qty=1),
             FlatPart("chica.dxf", rect(60, 60), qty=1)]
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  extra_sheets=extra)

    assert result.unplaceable == []              # 400x300 fits no NEW 200x200 sheet
    assert "R-BIG" in result.remnants_used
    placed = [pl.part.name for s in result.sheets for pl in s.placements]
    assert sorted(placed) == ["chica.dxf", "grande.dxf"]
    _assert_rack_is_conserved(result, extra)
    _assert_each_sheet_holds_its_own_stock(result)


# --------------------------------------------------------------------------- #
# reclaimable_rectangle — pure geometry, exact
# --------------------------------------------------------------------------- #

def _layout(sheet_w, sheet_h, boxes, margin=0.0, gap=0.0):
    """A hand-built sheet: ``boxes`` are (w, h, x, y) placed as-is."""
    spec = SheetSpec(width=sheet_w, height=sheet_h, margin=margin, part_gap=gap)
    lay = SheetLayout(index=0, spec=spec)
    for i, (w, h, x, y) in enumerate(boxes):
        lay.placements.append(
            Placement(part=FlatPart(f"p{i}.dxf", rect(w, h)), x=x, y=y))
    return lay


def test_no_leftover_is_reported_when_the_feature_is_off():
    lay = _layout(300, 200, [(100, 50, 5, 5)])
    assert reclaimable_rectangle(lay, gap=2, min_side=0) is None
    assert reclaimable_rectangle(lay, gap=2, min_side=-1) is None


def test_the_bigger_of_the_two_guillotine_bands_wins_top():
    # part occupies x 5..105, y 5..55 on a 300x200 sheet, gap 2
    #   right band: x=107, 193 x 200 = 38 600
    #   top band:   y=57,  300 x 143 = 42 900   <- bigger
    lay = _layout(300, 200, [(100, 50, 5, 5)])
    lo = reclaimable_rectangle(lay, gap=2, min_side=50)
    assert lo == Leftover(x=0.0, y=57.0, width=300.0, height=143.0)
    assert lo.area == 42_900


def test_the_bigger_of_the_two_guillotine_bands_wins_right():
    # part occupies x 5..105, y 5..155 on a 300x400 sheet, gap 2
    #   right band: x=107, 193 x 400 = 77 200   <- bigger
    #   top band:   y=157, 300 x 243 = 72 900
    lay = _layout(300, 400, [(100, 150, 5, 5)])
    lo = reclaimable_rectangle(lay, gap=2, min_side=50)
    assert lo == Leftover(x=107.0, y=0.0, width=193.0, height=400.0)
    assert lo.area == 77_200


def test_nothing_clears_min_side_means_no_leftover():
    """A sheet packed to within 20 mm of both edges has no shearable offcut."""
    lay = _layout(300, 200, [(280, 180, 5, 5)])
    assert reclaimable_rectangle(lay, gap=2, min_side=50) is None
    # the same sheet DOES have an offcut if the shop will keep a 10 mm strip
    assert reclaimable_rectangle(lay, gap=2, min_side=10) is not None


def test_a_band_narrower_than_min_side_loses_to_the_wider_one():
    # right band is only 300-(290+2)=8 mm wide; the top band is 300 x 143
    lay = _layout(300, 200, [(285, 50, 5, 5)])
    lo = reclaimable_rectangle(lay, gap=2, min_side=50)
    assert lo == Leftover(x=0.0, y=57.0, width=300.0, height=143.0)


def test_the_leftover_never_touches_a_placed_part():
    """Geometric proof, on a messy multi-part sheet."""
    lay = _layout(600, 400, [(100, 80, 5, 5), (60, 200, 110, 5),
                             (150, 90, 180, 100), (40, 40, 20, 120)])
    gap, min_side = 3.0, 40.0
    lo = reclaimable_rectangle(lay, gap=gap, min_side=min_side)
    assert lo is not None
    box = Polygon(rect(lo.width, lo.height, lo.x, lo.y))
    for pl in lay.placements:
        assert box.intersection(_poly(pl)).area == 0
        assert box.distance(_poly(pl)) >= gap - 1e-9
    # and it stays inside the sheet
    assert lo.x >= 0 and lo.y >= 0
    assert lo.x + lo.width <= lay.spec.width + 1e-9
    assert lo.y + lo.height <= lay.spec.height + 1e-9
    assert lo.width >= min_side and lo.height >= min_side


def test_the_gap_is_kept_off_the_last_part():
    lay = _layout(300, 200, [(100, 50, 0, 0)])
    assert reclaimable_rectangle(lay, gap=0, min_side=10).y == 50
    assert reclaimable_rectangle(lay, gap=10, min_side=10).y == 60


def test_a_rotated_part_is_measured_by_its_real_extent():
    """A 90-degree placement is 50 wide x 100 tall, not 100 x 50."""
    spec = SheetSpec(width=300, height=200)
    lay = SheetLayout(index=0, spec=spec)
    # rotate 90 CCW then translate: the ring lands in x 50..100, y 0..100
    lay.placements.append(
        Placement(part=FlatPart("r.dxf", rect(100, 50)), x=100, y=0, rotation=90))
    lo = reclaimable_rectangle(lay, gap=0, min_side=50)
    assert lo == Leftover(x=100.0, y=0.0, width=200.0, height=200.0)


# --------------------------------------------------------------------------- #
# min_remnant end to end
# --------------------------------------------------------------------------- #

def test_min_remnant_populates_a_leftover_the_shop_could_actually_shear():
    spec = SheetSpec(width=1000, height=1000, margin=5, part_gap=2)
    result = nest([FlatPart("a.dxf", rect(80, 40), qty=4)], spec,
                  rotation="ortho", time_per_sheet=1, seed=0, min_remnant=100)

    assert result.reclaimable, "a nearly empty 1x1 m sheet must have an offcut"
    for _n, lo in result.reclaimable:
        assert lo.width >= 100 and lo.height >= 100
    assert result.reclaimable_area > 0
    sheet = result.sheets[0]
    box = Polygon(rect(sheet.leftover.width, sheet.leftover.height,
                       sheet.leftover.x, sheet.leftover.y))
    for pl in sheet.placements:
        assert box.intersection(_poly(pl)).area == 0
