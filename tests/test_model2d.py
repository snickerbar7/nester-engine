"""Unit tests for the flat-nesting data model — pure geometry math."""

import pytest

from nester.sheet.model import FlatPart, SheetSpec, polygon_area, bbox


def rect(w, h):
    return ((0, 0), (w, 0), (w, h), (0, h))


def test_polygon_area_square():
    assert polygon_area(rect(10, 20)) == pytest.approx(200.0)


def test_polygon_area_is_orientation_independent():
    cw = ((0, 0), (0, 10), (10, 10), (10, 0))
    assert polygon_area(cw) == pytest.approx(100.0)


def test_bbox_and_size():
    assert bbox(rect(30, 40)) == (0, 0, 30, 40)
    p = FlatPart("x", rect(30, 40))
    assert p.size == (30, 40)


def test_part_area_subtracts_holes():
    p = FlatPart("x", rect(100, 100), holes=(rect(10, 10), rect(20, 5)))
    assert p.outer_area == pytest.approx(10000.0)
    assert p.hole_area == pytest.approx(100.0 + 100.0)
    assert p.area == pytest.approx(10000.0 - 200.0)


def test_part_rejects_degenerate_outer():
    with pytest.raises(ValueError):
        FlatPart("bad", ((0, 0), (1, 1)))


def test_part_rejects_bad_qty():
    with pytest.raises(ValueError):
        FlatPart("bad", rect(5, 5), qty=0)


def test_sheet_usable_and_key():
    s = SheetSpec(width=2440, height=1220, material="steel", thickness=2.0, margin=10)
    assert s.usable_width == 2420
    assert s.usable_height == 1200
    assert s.area == 2440 * 1220
    assert s.key == "steel@2mm"


def test_sheet_rejects_margin_too_big():
    with pytest.raises(ValueError):
        SheetSpec(width=100, height=100, margin=60)
