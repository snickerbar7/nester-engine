"""E15 — nesting small parts inside the holes of already-placed parts.

The strip packer sees only a part's outer boundary, so a 300 mm hole in a flange
leaves the shop as skeleton. The second pass reclaims it. Because a part cut out
of another part's hole comes off the table inside a slug, the pass is OFF unless
asked for, and every copy it places is flagged.

The pass itself is deterministic (a bounded first-fit search, no solver), so it
is tested directly on hand-built layouts. The end-to-end tests only assert
invariants — containment, clearance, no collisions, one level, conservation —
never a sheet count, since the underlying solve is time-budgeted and stochastic.
"""

import math

import pytest
from shapely.geometry import Polygon

from nester.sheet.holes import nest_into_holes
from nester.sheet.model import (
    FlatPart, Placement, SheetLayout, SheetSpec, transform,
)
from nester.sheet.pack import nest

GAP = 3.0


def rect(w, h, x=0.0, y=0.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def circle(r, cx, cy, n=48):
    return tuple((cx + r * math.cos(2 * math.pi * i / n),
                  cy + r * math.sin(2 * math.pi * i / n)) for i in range(n))


def _poly(pl):
    return Polygon(transform(pl.part.outer, pl.rotation, pl.x, pl.y))


def _host_holes(pl):
    return [Polygon(transform(h, pl.rotation, pl.x, pl.y)) for h in pl.part.holes]


def _sheet(spec, host_placements):
    lay = SheetLayout(index=0, spec=spec)
    lay.placements.extend(host_placements)
    return lay


# --------------------------------------------------------------------------- #
# Invariants every layout with in-hole placements must satisfy
# --------------------------------------------------------------------------- #

def _assert_in_hole_invariants(layout, gap, tol=1e-2):
    """Containment, clearance, one level, and valid back-references."""
    for i, pl in enumerate(layout.placements):
        if not pl.is_in_hole:
            continue
        # the back-reference is an index into THIS sheet's placements...
        assert isinstance(pl.in_hole_of, int)
        assert 0 <= pl.in_hole_of < len(layout.placements)
        assert pl.in_hole_of != i
        host = layout.placements[pl.in_hole_of]
        # ...and one level only: a guest never hosts a guest
        assert not host.is_in_hole, "a part in a hole must not itself host a part"

        guest = _poly(pl)
        holes = _host_holes(host)
        assert holes, "the host has no holes to have hosted anything"
        fits = [h for h in holes
                if h.contains(guest) and h.exterior.distance(guest) >= gap - tol]
        assert fits, (
            f"{pl.part.name} is not fully inside any hole of {host.part.name} "
            f"with {gap} mm of clearance")


def _assert_no_illegal_overlap(layout):
    """No two parts overlap — except a host and the guest inside its hole."""
    polys = [_poly(pl) for pl in layout.placements]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            a, b = layout.placements[i], layout.placements[j]
            if a.in_hole_of == j or b.in_hole_of == i:
                continue                       # host/guest pair: legitimate
            assert polys[i].intersection(polys[j]).area < 1e-6, (
                f"{a.part.name} overlaps {b.part.name}")


# --------------------------------------------------------------------------- #
# The pass itself, on hand-built layouts (deterministic)
# --------------------------------------------------------------------------- #

def _one_host_sheet(hole, host_size=200):
    spec = SheetSpec(width=400, height=400, margin=5, part_gap=GAP)
    host = FlatPart("marco.dxf", rect(host_size, host_size), holes=(hole,))
    return _sheet(spec, [Placement(part=host, x=5, y=5)])


def test_a_rectangular_hole_is_filled_and_every_copy_is_flagged():
    layout = _one_host_sheet(rect(120, 120, 40, 40))
    guest = FlatPart("chica.dxf", rect(40, 40), qty=6)
    remaining = {"g": 6}

    placed = nest_into_holes(layout, {"g": guest}, remaining, {"g": (0.0,)}, GAP)

    assert placed > 0
    assert remaining["g"] == 6 - placed              # demand is consumed exactly
    assert len(layout.placements) == 1 + placed
    guests = [pl for pl in layout.placements if pl.is_in_hole]
    assert len(guests) == placed
    assert all(pl.in_hole_of == 0 for pl in guests)
    assert layout.in_hole_count == placed
    _assert_in_hole_invariants(layout, GAP)
    _assert_no_illegal_overlap(layout)


def test_a_round_hole_works_too_not_just_rectangles():
    layout = _one_host_sheet(circle(70, 100, 100))
    guest = FlatPart("disco.dxf", rect(40, 40), qty=6)
    remaining = {"g": 6}

    placed = nest_into_holes(layout, {"g": guest}, remaining, {"g": None}, GAP)

    assert placed > 0
    _assert_in_hole_invariants(layout, GAP)
    _assert_no_illegal_overlap(layout)


def test_two_guests_in_one_hole_keep_the_part_gap_from_each_other():
    layout = _one_host_sheet(rect(120, 120, 40, 40))
    guest = FlatPart("chica.dxf", rect(40, 40), qty=4)
    nest_into_holes(layout, {"g": guest}, {"g": 4}, {"g": (0.0,)}, GAP)

    guests = [_poly(pl) for pl in layout.placements if pl.is_in_hole]
    assert len(guests) >= 2
    for i in range(len(guests)):
        for j in range(i + 1, len(guests)):
            assert guests[i].intersection(guests[j]).area < 1e-6
            assert guests[i].distance(guests[j]) >= GAP - 1e-3


def test_a_part_too_big_for_the_hole_is_never_placed_in_one():
    layout = _one_host_sheet(rect(120, 120, 40, 40))
    huge = FlatPart("grande.dxf", rect(150, 150), qty=2)
    remaining = {"b": 2}

    assert nest_into_holes(layout, {"b": huge}, remaining, {"b": None}, GAP) == 0
    assert remaining == {"b": 2}
    assert layout.placements[1:] == []
    assert layout.in_hole_count == 0


def test_a_part_that_only_just_misses_because_of_the_gap_is_not_placed():
    """119x119 fits a 120 mm hole geometrically, but not with 3 mm of clearance."""
    layout = _one_host_sheet(rect(120, 120, 40, 40))
    snug = FlatPart("justa.dxf", rect(119, 119), qty=1)
    assert nest_into_holes(layout, {"s": snug}, {"s": 1}, {"s": (0.0,)}, GAP) == 0
    # with no clearance demanded it does fit
    layout2 = _one_host_sheet(rect(120, 120, 40, 40))
    assert nest_into_holes(layout2, {"s": snug}, {"s": 1}, {"s": (0.0,)}, 0.0) == 1


def test_a_host_without_holes_hosts_nothing():
    spec = SheetSpec(width=400, height=400, margin=5, part_gap=GAP)
    solid = FlatPart("solida.dxf", rect(200, 200))
    layout = _sheet(spec, [Placement(part=solid, x=5, y=5)])
    guest = FlatPart("chica.dxf", rect(10, 10), qty=5)
    assert nest_into_holes(layout, {"g": guest}, {"g": 5}, {"g": None}, GAP) == 0


def test_nothing_left_to_place_means_nothing_happens():
    layout = _one_host_sheet(rect(120, 120, 40, 40))
    guest = FlatPart("chica.dxf", rect(10, 10), qty=1)
    assert nest_into_holes(layout, {"g": guest}, {"g": 0}, {"g": None}, GAP) == 0
    assert layout.placements[1:] == []


def test_a_guest_never_becomes_a_host_even_when_it_has_its_own_holes():
    """One level only: the 40x40 hole in a part nested in a hole is not a
    container, even though a 20x20 part is sitting right there unplaced."""
    layout = _one_host_sheet(rect(160, 160, 20, 20))
    catalog = {"aro": FlatPart("aro.dxf", rect(60, 60), holes=(rect(40, 40, 10, 10),)),
               "micro": FlatPart("micro.dxf", rect(20, 20))}
    remaining = {"aro": 4, "micro": 4}

    placed = nest_into_holes(layout, catalog, remaining,
                             {"aro": (0.0,), "micro": (0.0,)}, GAP)

    assert placed > 0
    aros = [pl for pl in layout.placements if pl.part.name == "aro.dxf"]
    assert aros, "the test is vacuous unless a hole-bearing guest was placed"
    # everything that landed points at the ORIGINAL host, never at a guest
    assert all(pl.in_hole_of == 0 for pl in layout.placements if pl.is_in_hole)
    for pl in layout.placements:
        if pl.is_in_hole:
            assert not layout.placements[pl.in_hole_of].is_in_hole
    # and no micro was tucked inside an aro's own hole
    for micro in [pl for pl in layout.placements if pl.part.name == "micro.dxf"]:
        for aro in aros:
            for hole in _host_holes(aro):
                assert not hole.contains(_poly(micro))
    _assert_in_hole_invariants(layout, GAP)
    _assert_no_illegal_overlap(layout)


def test_running_the_pass_twice_does_not_stack_parts_on_top_of_each_other():
    """The pass is re-entrant: a second call tops the sheet up, never double-books it.

    pack.nest() calls it once per sheet today, so this guards a top-up / resume
    / re-run path against silently emitting overlapping cuts.
    """
    layout = _one_host_sheet(rect(160, 160, 20, 20))
    guest = FlatPart("aro.dxf", rect(60, 60), qty=4)
    assert nest_into_holes(layout, {"g": guest}, {"g": 4}, {"g": (0.0,)}, GAP) > 0

    tiny = FlatPart("micro.dxf", rect(20, 20), qty=4)
    nest_into_holes(layout, {"t": tiny}, {"t": 4}, {"t": (0.0,)}, GAP)

    _assert_no_illegal_overlap(layout)


def test_the_roomiest_hole_gets_the_biggest_part():
    """Regions are served largest-first, parts largest-first."""
    spec = SheetSpec(width=600, height=400, margin=5, part_gap=GAP)
    small_host = FlatPart("chico.dxf", rect(100, 100), holes=(rect(60, 60, 20, 20),))
    big_host = FlatPart("grande.dxf", rect(300, 300), holes=(rect(200, 200, 50, 50),))
    layout = _sheet(spec, [Placement(part=small_host, x=5, y=5),
                           Placement(part=big_host, x=150, y=5)])
    catalog = {"big": FlatPart("pieza-grande.dxf", rect(120, 120)),
               "small": FlatPart("pieza-chica.dxf", rect(40, 40))}
    remaining = {"big": 1, "small": 1}

    nest_into_holes(layout, catalog, remaining, {"big": (0.0,), "small": (0.0,)}, GAP)

    where = {pl.part.name: pl.in_hole_of
             for pl in layout.placements if pl.is_in_hole}
    assert where["pieza-grande.dxf"] == 1        # only the 200 mm hole can hold it
    _assert_in_hole_invariants(layout, GAP)
    _assert_no_illegal_overlap(layout)


def test_a_rotated_host_carries_its_holes_with_it():
    """The container is the hole AS PLACED, not the hole in part coordinates."""
    spec = SheetSpec(width=400, height=400, margin=5, part_gap=GAP)
    host = FlatPart("marco.dxf", rect(200, 100), holes=(rect(120, 60, 40, 20),))
    # 90 CCW: the part lands in x -100..0, y 0..200 -> translate by (150, 5)
    layout = _sheet(spec, [Placement(part=host, x=150, y=5, rotation=90)])
    guest = FlatPart("chica.dxf", rect(40, 40), qty=2)

    placed = nest_into_holes(layout, {"g": guest}, {"g": 2}, {"g": (0.0,)}, GAP)
    assert placed > 0
    _assert_in_hole_invariants(layout, GAP)
    _assert_no_illegal_overlap(layout)


# --------------------------------------------------------------------------- #
# End to end through nest() — one pair of solves, shared by every test below
# --------------------------------------------------------------------------- #

def _hole_job():
    host = FlatPart("marco.dxf", rect(440, 440), holes=(rect(300, 300, 70, 70),), qty=2)
    guest = FlatPart("chica.dxf", rect(50, 50), qty=20)
    spec = SheetSpec(width=500, height=500, margin=5, part_gap=GAP)
    return [host, guest], spec


@pytest.fixture(scope="module")
def job_off():
    parts, spec = _hole_job()
    return nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0)


@pytest.fixture(scope="module")
def job_on():
    parts, spec = _hole_job()
    return nest(parts, spec, rotation="ortho", time_per_sheet=1, seed=0,
                nest_in_holes=True)


def test_hole_nesting_is_off_by_default(job_off):
    """Huge holes, and not one part in them, because nobody asked."""
    assert job_off.in_hole_count == 0
    assert all(s.in_hole_count == 0 for s in job_off.sheets)
    assert all(pl.in_hole_of is None and not pl.is_in_hole
               for s in job_off.sheets for pl in s.placements)


def test_the_flag_actually_puts_parts_in_holes(job_on):
    assert job_on.in_hole_count > 0
    assert sum(s.in_hole_count for s in job_on.sheets) == job_on.in_hole_count


def test_every_in_hole_placement_is_contained_with_clearance(job_on):
    for sheet in job_on.sheets:
        _assert_in_hole_invariants(sheet, GAP)


def test_no_two_parts_collide_except_a_host_and_its_guest(job_on):
    for sheet in job_on.sheets:
        _assert_no_illegal_overlap(sheet)


def test_in_hole_indices_are_local_to_their_own_sheet(job_on):
    for sheet in job_on.sheets:
        for pl in sheet.placements:
            if pl.is_in_hole:
                assert 0 <= pl.in_hole_of < len(sheet.placements)
                assert sheet.placements[pl.in_hole_of].part.holes


def test_guests_stay_inside_the_sheet_margins_like_any_other_part(job_on):
    for sheet in job_on.sheets:
        s = sheet.spec
        for pl in sheet.placements:
            x0, y0, x1, y1 = _poly(pl).bounds
            assert x0 >= s.margin - 1e-6 and y0 >= s.margin - 1e-6
            assert x1 <= s.width - s.margin + 1e-6
            assert y1 <= s.height - s.margin + 1e-6


def test_turning_the_flag_on_never_loses_a_part(job_off, job_on):
    off_total = sum(s.part_count for s in job_off.sheets)
    on_total = sum(s.part_count for s in job_on.sheets)
    assert on_total >= off_total
    # the whole placeable demand lands either way — the flag only moves parts
    assert on_total == off_total == 22
    assert job_on.unplaceable == job_off.unplaceable == []


def test_filling_holes_never_needs_more_sheets_than_leaving_them_empty(job_off, job_on):
    """It cannot come out worse: the pass only fills voids the solver could not
    see. (The counts themselves are stochastic; the ordering is not.)"""
    assert job_on.sheet_count <= job_off.sheet_count
    assert job_on.yield_pct >= job_off.yield_pct - 1e-9
