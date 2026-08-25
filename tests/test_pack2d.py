"""Tests for the 2D nester: valid (non-overlapping, in-bounds) layouts + unplaceable.

These drive the real spyrrow engine with a tiny 1s budget, so they are a little
slower than the pure-math tests but still fast.
"""

import math

import pytest

from nester.sheet.model import FlatPart, SheetSpec
from nester.sheet.pack import NestCancelled, NestProgress, nest, transform


def rect(w, h):
    return ((0, 0), (w, 0), (w, h), (0, h))


def _poly_overlap(a, b):
    from shapely.geometry import Polygon
    pa, pb = Polygon(a), Polygon(b)
    return pa.intersection(pb).area


def _assert_valid(result, spec):
    for sheet in result.sheets:
        placed = []
        for pl in sheet.placements:
            pts = transform(pl.part.outer, pl.rotation, pl.x, pl.y)
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            assert min(xs) >= -1e-6 and min(ys) >= -1e-6
            assert max(xs) <= spec.width + 1e-6
            assert max(ys) <= spec.height + 1e-6
            placed.append(pts)
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                assert _poly_overlap(placed[i], placed[j]) < 1e-3


def test_nest_is_overlap_free_and_in_bounds():
    parts = [FlatPart("a", rect(80, 40), qty=4), FlatPart("b", rect(50, 50), qty=4)]
    spec = SheetSpec(width=300, height=200, margin=5, part_gap=2)
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)
    assert sum(s.part_count for s in result.sheets) == 8
    assert result.sheet_count >= 1
    _assert_valid(result, spec)


def test_part_larger_than_sheet_is_unplaceable():
    parts = [FlatPart("big", rect(400, 400), qty=1), FlatPart("ok", rect(20, 20), qty=1)]
    spec = SheetSpec(width=300, height=200, margin=5)
    result = nest(parts, spec, rotation="free", time_per_sheet=1, seed=0)
    names = [p.name for p in result.unplaceable]
    assert "big" in names
    assert sum(s.part_count for s in result.sheets) == 1  # only "ok" placed


def test_yield_is_positive_and_bounded():
    parts = [FlatPart("a", rect(60, 60), qty=6)]
    spec = SheetSpec(width=200, height=200, margin=5, part_gap=1)
    result = nest(parts, spec, rotation="fixed", time_per_sheet=1, seed=0)
    assert 0 < result.yield_pct <= 100


# --------------------------------------------------------------------------- #
# Progress + cancellation (the async jobs API rides on these)
# --------------------------------------------------------------------------- #

def _multi_sheet_job():
    """20 parts that cannot fit one 200x200 sheet -> at least two sheets."""
    parts = [FlatPart("a", rect(80, 40), qty=20)]
    spec = SheetSpec(width=200, height=200, margin=5, part_gap=2)
    return parts, spec


def test_progress_is_reported_once_per_completed_sheet():
    parts, spec = _multi_sheet_job()
    ticks = []
    result = nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                  progress=ticks.append)

    assert len(ticks) == result.sheet_count >= 2
    assert all(isinstance(t, NestProgress) for t in ticks)
    # sheets_done counts up one at a time and ends at the real total
    assert [t.sheets_done for t in ticks] == list(range(1, result.sheet_count + 1))
    assert ticks[-1].sheets_done == result.sheet_count
    # parts placed is monotonic and ends at everything demanded
    assert [t.parts_placed for t in ticks] == sorted(t.parts_placed for t in ticks)
    assert ticks[-1].parts_placed == sum(s.part_count for s in result.sheets) == 20
    assert all(t.parts_total == 20 for t in ticks)
    # the estimate is an estimate, but never claims fewer sheets than are done,
    # and the final tick knows the truth
    assert all(t.sheets_total_estimate >= t.sheets_done for t in ticks)
    assert ticks[-1].sheets_total_estimate == result.sheet_count
    assert 0 < ticks[0].last_sheet_utilization_pct <= 100


def test_unplaceable_parts_are_excluded_from_the_progress_denominator():
    parts = [FlatPart("big", rect(400, 400), qty=3), FlatPart("ok", rect(20, 20), qty=2)]
    spec = SheetSpec(width=300, height=200, margin=5)
    ticks = []
    nest(parts, spec, rotation="free", time_per_sheet=1, seed=0, progress=ticks.append)
    assert ticks[-1].parts_total == 2  # only the placeable demand is the target


def test_cancel_between_sheets_raises_with_the_work_so_far():
    parts, spec = _multi_sheet_job()
    state = {"stop": False}

    def after_first_sheet(_p):
        state["stop"] = True

    with pytest.raises(NestCancelled) as excinfo:
        nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
             progress=after_first_sheet, should_cancel=lambda: state["stop"])

    partial = excinfo.value.partial
    assert partial.sheet_count == 1          # the sheet already solved is kept
    assert partial.sheets[0].part_count > 0


def test_cancel_before_the_first_sheet_yields_an_empty_partial():
    parts, spec = _multi_sheet_job()
    with pytest.raises(NestCancelled) as excinfo:
        nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
             should_cancel=lambda: True)
    assert excinfo.value.partial.sheet_count == 0


def test_callbacks_are_optional_and_change_nothing():
    parts, spec = _multi_sheet_job()
    a = nest(parts, spec, rotation="fixed", time_per_sheet=1, seed=7)
    b = nest(parts, spec, rotation="fixed", time_per_sheet=1, seed=7,
             progress=lambda _p: None, should_cancel=lambda: False)
    assert a.sheet_count == b.sheet_count
    assert [s.part_count for s in a.sheets] == [s.part_count for s in b.sheets]


def test_transform_rotation_then_translation():
    # 90° CCW about origin sends (10,0) -> (0,10), then +(5,5) -> (5,15)
    out = transform([(10, 0)], 90, 5, 5)[0]
    assert out[0] == pytest.approx(5, abs=1e-9)
    assert out[1] == pytest.approx(15, abs=1e-9)
