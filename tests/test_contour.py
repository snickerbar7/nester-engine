"""Tests for part silhouettes shipped over the API (nester.sheet.contour).

What matters: the shape stays the shape (holes survive, geometry stays within
tolerance), and the payload stays bounded (a hard point cap, whatever the input).
"""

import math

import pytest

from nester.sheet.contour import (
    DEFAULT_TOLERANCE,
    MAX_POINTS,
    douglas_peucker,
    part_contour,
    part_contour_with_origin,
    simplify_ring,
)
from nester.sheet.model import FlatPart


def circle(cx, cy, r, n):
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def gear(cx, cy, r, teeth, amp, n):
    """A high-curvature ring: Douglas-Peucker can't thin it much at 0.2mm."""
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n
        rr = r + amp * math.sin(teeth * a)
        out.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return out


def rect(w, h):
    return ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h))


# --- Douglas-Peucker -------------------------------------------------------- #

def test_dp_keeps_endpoints_and_drops_collinear_points():
    chain = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    assert douglas_peucker(chain, 0.2) == [(0, 0), (4, 0)]


def test_dp_keeps_a_vertex_outside_tolerance():
    chain = [(0, 0), (2, 1.0), (4, 0)]
    assert douglas_peucker(chain, 0.2) == chain          # 1.0mm bulge is kept
    assert douglas_peucker(chain, 2.0) == [(0, 0), (4, 0)]  # inside 2mm, gone


def test_dp_handles_a_long_chain_without_recursion_limits():
    chain = circle(0, 0, 1000, 20000)
    out = douglas_peucker(chain, 0.2)
    assert 3 < len(out) < len(chain)


# --- ring decimation -------------------------------------------------------- #

def test_a_rectangle_survives_decimation_unchanged():
    assert simplify_ring(rect(60, 30)) == list(rect(60, 30))


def test_a_flattened_circle_decimates_hard_but_stays_a_circle():
    ring = circle(0, 0, 100, 4000)
    out = simplify_ring(ring, DEFAULT_TOLERANCE, MAX_POINTS)
    assert len(out) <= MAX_POINTS
    assert len(out) < len(ring) / 10
    # every kept vertex is still on the circle
    for x, y in out:
        assert math.hypot(x, y) == pytest.approx(100, abs=1e-6)


def test_point_budget_is_a_hard_cap_even_when_geometry_resists():
    ring = gear(0, 0, 200, 90, 4.0, 6000)
    assert len(simplify_ring(ring, DEFAULT_TOLERANCE, MAX_POINTS)) <= MAX_POINTS
    assert len(simplify_ring(ring, DEFAULT_TOLERANCE, 40)) <= 40


def test_zero_tolerance_still_respects_the_cap_by_subsampling():
    ring = circle(0, 0, 50, 1000)
    out = simplify_ring(ring, 0.0, 60)
    assert 3 <= len(out) <= 60


def test_a_tiny_ring_is_never_decimated_below_a_polygon():
    tri = [(0, 0), (10, 0), (5, 8)]
    assert simplify_ring(tri, 100.0, 200) == tri


# --- part contours ---------------------------------------------------------- #

def test_contour_holes_are_preserved_one_per_hole():
    part = FlatPart("plate", tuple(rect(200, 120)),
                    holes=(tuple(circle(50, 60, 10, 400)),
                           tuple(circle(150, 60, 10, 400))))
    c = part_contour(part)
    assert len(c["holes"]) == 2
    for hole in c["holes"]:
        assert 3 <= len(hole) <= MAX_POINTS


def test_contour_origin_is_the_bbox_min_corner():
    # part drawn far from the origin: the contour comes back normalized
    outer = tuple((x + 1000.0, y + 500.0) for x, y in rect(80, 40))
    hole = tuple((x + 1000.0, y + 500.0) for x, y in circle(40, 20, 5, 200))
    part = FlatPart("offset", outer, holes=(hole,))
    c, origin = part_contour_with_origin(part)
    assert origin == pytest.approx((1000.0, 500.0))
    xs = [p[0] for p in c["outer"]]
    ys = [p[1] for p in c["outer"]]
    assert min(xs) == pytest.approx(0.0) and min(ys) == pytest.approx(0.0)
    assert max(xs) == pytest.approx(80.0) and max(ys) == pytest.approx(40.0)
    # holes move with the outer boundary, not independently
    hx = [p[0] for p in c["holes"][0]]
    assert min(hx) == pytest.approx(35.0, abs=1e-6)


def test_contour_is_json_shaped_lists_of_pairs():
    part = FlatPart("p", tuple(rect(10, 10)))
    c = part_contour(part)
    assert set(c) == {"outer", "holes"}
    assert all(isinstance(p, list) and len(p) == 2 for p in c["outer"])
    assert c["holes"] == []


def test_a_degenerate_hole_is_dropped_not_emitted_as_a_line():
    part = FlatPart("p", tuple(rect(100, 100)), holes=(((10, 10), (20, 10)),))
    assert part_contour(part)["holes"] == []


# --- contours in a nest result (what the web nest view draws) --------------- #

def _nest_result(rotation):
    """One sheet, one placed copy of a part drawn far from the DXF origin."""
    from nester.sheet.model import NestResult, Placement, SheetLayout, SheetSpec

    outer = tuple((x + 1000.0, y + 500.0) for x, y in rect(80, 40))
    hole = tuple((x + 1000.0, y + 500.0) for x, y in circle(40, 20, 6, 200))
    part = FlatPart("bracket.dxf", outer, holes=(hole,))
    spec = SheetSpec(width=2440, height=1220, margin=8, part_gap=3)
    layout = SheetLayout(index=0, spec=spec)
    layout.placements.append(Placement(part=part, x=-900.0, y=-450.0, rotation=rotation))
    return NestResult(spec=spec, sheets=[layout]), part


def test_nest_result_carries_each_unique_part_once():
    engine = pytest.importorskip("service.core.engine")
    result, _part = _nest_result(0.0)
    # place a second copy of the SAME part: the contour still ships once
    result.sheets[0].placements.append(
        type(result.sheets[0].placements[0])(
            part=result.sheets[0].placements[0].part, x=0.0, y=0.0, rotation=0.0))

    parts, origins = engine.sheet_part_index(result)
    assert len(parts) == 1
    assert parts[0]["name"] == "bracket.dxf"
    assert parts[0]["qty_placed"] == 2
    assert parts[0]["holes"] == 1
    assert origins["bracket.dxf"] == pytest.approx((1000.0, 500.0))


@pytest.mark.parametrize("rotation", [0.0, 90.0, 37.5, 180.0])
def test_contour_offset_reproduces_the_placed_geometry(rotation):
    """The contour is normalized to the part's bbox; `x`/`y` translate the RAW
    coordinates. `contour_offset_mm` is the bridge, and it must be exact."""
    engine = pytest.importorskip("service.core.engine")
    from nester.sheet.pack import transform

    result, part = _nest_result(rotation)
    parts, origins = engine.sheet_part_index(result)
    result_json = {"sheets": [{"parts": [{"name": part.name}]}]}
    engine._annotate_placements(result_json, result, origins)
    pl_json = result_json["sheets"][0]["parts"][0]

    # rebuild the placed outline from the CONTOUR + the reported offset
    ox, oy = pl_json["contour_offset_mm"]
    contour = parts[0]["contour"]["outer"]
    rebuilt = transform([tuple(p) for p in contour], rotation, ox, oy)
    truth = transform(part.outer, rotation, -900.0, -450.0)
    assert len(rebuilt) == len(truth)
    for got, want in zip(rebuilt, truth):
        assert got[0] == pytest.approx(want[0], abs=1e-3)
        assert got[1] == pytest.approx(want[1], abs=1e-3)

    # and the reported bbox is the placed bbox
    xs = [p[0] for p in truth]
    ys = [p[1] for p in truth]
    assert pl_json["bbox_mm"] == pytest.approx(
        [min(xs), min(ys), max(xs), max(ys)], abs=1e-3)
