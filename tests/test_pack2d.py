"""Tests for the 2D nester: valid (non-overlapping, in-bounds) layouts + unplaceable.

These drive the real spyrrow engine with a tiny 1s budget, so they are a little
slower than the pure-math tests but still fast.
"""

import math

import pytest

from nester.sheet.model import FlatPart, SheetSpec
from nester.sheet.pack import nest, transform


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


def test_transform_rotation_then_translation():
    # 90° CCW about origin sends (10,0) -> (0,10), then +(5,5) -> (5,15)
    out = transform([(10, 0)], 90, 5, 5)[0]
    assert out[0] == pytest.approx(5, abs=1e-9)
    assert out[1] == pytest.approx(15, abs=1e-9)
