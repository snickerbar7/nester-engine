"""Correctness fix: `part_gap == 0` used to disable overlap detection in the
free-area/hole top-up search (`nester/sheet/holes.py`), not just the gap
clearance on top of it.

``_try_place`` guarded its ONLY overlap check with ``gap > 0 and ...``. At
``gap == 0`` — a legal value: it's ``SheetSpec.part_gap``'s own default — the
condition short-circuited to False and ``blockers`` (the record of what this
same top-up pass had already placed in the region) was never consulted, so
every copy of a part landed on the SAME bottom-left candidate and stacked
exactly on top of each other. This reaches production through
``nest_into_free_area`` (on by default) and ``nest_into_holes``.

The fix separates the two constraints: overlap (``intersection(...).area >
_EPS``) is rejected unconditionally, and the gap distance is an ADDITIONAL
check only meaningful when ``gap > 0``.

Everything here measures overlap geometrically — reconstructing each
placement's real contour with ``nester.sheet.pack.transform`` and intersecting
polygons — never by trusting a field the engine computed about itself.
"""

from __future__ import annotations

import random
from typing import List, Optional, Tuple

import pytest
from shapely.geometry import Polygon

from nester.sheet.model import ExtraSheet, FlatPart, SheetSpec
from nester.sheet.pack import nest, transform


def rect(w: float, h: float, x: float = 0.0, y: float = 0.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def _footprint(pl) -> Polygon:
    return Polygon(transform(pl.part.outer, pl.rotation, pl.x, pl.y))


def _overlapping_pairs(result) -> int:
    """Count pairs of placements on the same sheet whose real contours overlap.

    A part placed inside another's hole is legitimately "inside" its host's
    outer footprint — that is the whole point of E15 — so a host/guest pair is
    not a collision. Any other overlap is a real physical defect.

    The 1e-3 mm^2 tolerance matches ``test_sheet_search.py``'s
    ``_assert_no_collisions``: spyrrow's own strip-packing solve places some
    rectangles at angles a hair off 0/90/180/270 degrees, which can leave two
    edge-touching parts intersecting by a sub-nanometre sliver (observed as
    low as 1e-9 mm^2) that is float noise, not a defect. Real overlaps from the
    gap=0 bug this file regresses were 3-4 orders of magnitude larger than
    this tolerance (whole parts stacked on each other), so it does not mask
    anything the bug produced.
    """
    total = 0
    for sheet in result.sheets:
        polys = [_footprint(pl) for pl in sheet.placements]
        hosts = [pl.in_hole_of for pl in sheet.placements]
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if hosts[j] == i or hosts[i] == j:
                    continue
                if polys[i].intersection(polys[j]).area > 1e-3:
                    total += 1
    return total


# --------------------------------------------------------------------------- #
# 1. Regression: the exact reproduction from the bug report.
# --------------------------------------------------------------------------- #

_REPRO_PARTS = [
    FlatPart("a.dxf", rect(700, 520), qty=9),
    FlatPart("b.dxf", rect(380, 300), qty=14),
    FlatPart("c.dxf", rect(120, 90), qty=40),
]


@pytest.mark.parametrize("seed", [1, 3, 7, 11])
def test_gap_zero_produces_zero_overlaps_on_the_reported_job(seed):
    """Before the fix this seed set produced 300-435 overlapping pairs, every
    seed, not intermittently — see the module docstring. It must now be zero.
    """
    spec = SheetSpec(width=2440, height=1220, margin=10, part_gap=0.0)
    result = nest(_REPRO_PARTS, spec, rotation="free", time_per_sheet=3,
                  seed=seed, minimize_sheets=True)

    total_placed = sum(s.part_count for s in result.sheets)
    assert total_placed == 63, "conservation: all 63 copies must land somewhere"
    assert _overlapping_pairs(result) == 0


def test_gap_three_is_unchanged_by_the_fix():
    """The gap > 0 path was already correct; pin it stays that way: same job,
    same seed, gap=3 -> 3 sheets, 63 placements, zero overlap, exactly as
    before the fix (the guard there was never wrong).
    """
    spec = SheetSpec(width=2440, height=1220, margin=10, part_gap=3.0)
    result = nest(_REPRO_PARTS, spec, rotation="free", time_per_sheet=3,
                  seed=1, minimize_sheets=True)

    assert result.sheet_count == 3
    assert sum(s.part_count for s in result.sheets) == 63
    assert _overlapping_pairs(result) == 0


# --------------------------------------------------------------------------- #
# 2. Property test: random jobs, several gaps, several seeds.
# --------------------------------------------------------------------------- #

def _gen_job(rng: random.Random):
    """One small, deterministic-given-rng random nesting job.

    Kept small on purpose (few part types, modest quantities, time_per_sheet=1,
    minimize_sheets off) so the whole property sweep stays cheap enough for CI
    — see the runtime note below the test.
    """
    n_types = rng.randint(2, 3)
    parts: List[FlatPart] = []
    for i in range(n_types):
        w = rng.uniform(60, 320)
        h = rng.uniform(60, 320)
        qty = rng.randint(1, 5)
        outer = rect(w, h)
        holes: Tuple = ()
        # Occasionally give a part a rectangular hole so nest_into_holes (E15)
        # gets exercised too, not just the free-area top-up.
        if rng.random() < 0.4 and w > 120 and h > 120:
            hw, hh = w * 0.4, h * 0.4
            hx, hy = (w - hw) / 2, (h - hh) / 2
            holes = (rect(hw, hh, hx, hy),)
        parts.append(FlatPart(f"part{i}.dxf", outer, holes=holes, qty=qty))

    sheet_w = rng.uniform(800, 1400)
    sheet_h = rng.uniform(600, 1000)
    margin = rng.choice([0.0, 5.0, 10.0])
    spec = SheetSpec(width=sheet_w, height=sheet_h, margin=margin,
                      part_gap=0.0)  # part_gap set by the caller per-run

    extra_sheets: List[ExtraSheet] = []
    if rng.random() < 0.4:
        # A retazo smaller than the nominal sheet, so a placement landing on it
        # genuinely exercises "own sheet's usable area, not the job's nominal
        # one".
        rw = rng.uniform(300, sheet_w * 0.8)
        rh = rng.uniform(300, sheet_h * 0.8)
        extra_sheets.append(ExtraSheet(width=rw, height=rh, label="R-01"))

    return parts, spec, extra_sheets


def _usable_rect(sheet_spec) -> Tuple[float, float, float, float]:
    m = sheet_spec.margin
    return (m, m, sheet_spec.width - m, sheet_spec.height - m)


@pytest.mark.parametrize("gap", [0.0, 0.5, 3.0, 8.0])
@pytest.mark.parametrize("seed", [101, 202, 303])
def test_property_no_overlap_gap_respected_in_bounds_conserved(gap, seed):
    """The four invariants a nest must never violate, over random jobs:

    1. no two placed parts on a sheet overlap (real contours);
    2. the part-to-part gap is respected when gap > 0;
    3. every part sits inside ITS OWN sheet's usable area (sheet.spec, which
       differs from the job's nominal sheet when a retazo was consumed);
    4. conservation: every demanded part is placed exactly qty times, or
       reported in unplaceable.
    """
    rng = random.Random(seed * 1000 + round(gap * 10))
    parts, base_spec, extra_sheets = _gen_job(rng)
    spec = SheetSpec(width=base_spec.width, height=base_spec.height,
                      margin=base_spec.margin, part_gap=gap)

    result = nest(parts, spec, rotation="ortho", time_per_sheet=1,
                  seed=seed, minimize_sheets=False, nest_in_holes=True,
                  fill_free_area=True, extra_sheets=extra_sheets)

    assert result.invalid == []

    # 1 + 2: overlap and gap, pairwise, per sheet.
    for sheet in result.sheets:
        polys = [_footprint(pl) for pl in sheet.placements]
        hosts = [pl.in_hole_of for pl in sheet.placements]
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if hosts[j] == i or hosts[i] == j:
                    continue
                assert polys[i].intersection(polys[j]).area <= 1e-3, (
                    f"overlap at gap={gap} seed={seed}")
                if gap > 0:
                    assert polys[i].distance(polys[j]) >= gap - 1e-2, (
                        f"gap violated at gap={gap} seed={seed}")

        # 3: every placement inside ITS OWN sheet's usable area.
        x0, y0, x1, y1 = _usable_rect(sheet.spec)
        for pl, poly in zip(sheet.placements, polys):
            pb = poly.bounds
            assert pb[0] >= x0 - 1e-2 and pb[1] >= y0 - 1e-2
            assert pb[2] <= x1 + 1e-2 and pb[3] <= y1 + 1e-2

    # 4: conservation — placed + unplaceable == demanded, per part name.
    demanded = {p.name: p.qty for p in parts}
    placed_count = {name: 0 for name in demanded}
    for sheet in result.sheets:
        for pl in sheet.placements:
            placed_count[pl.part.name] += 1
    unplaceable_count = {name: 0 for name in demanded}
    for p in result.unplaceable:
        unplaceable_count[p.name] += p.qty

    for name, qty in demanded.items():
        assert placed_count[name] + unplaceable_count[name] == qty, (
            f"{name}: placed {placed_count[name]} + unplaceable "
            f"{unplaceable_count[name]} != demanded {qty} "
            f"(gap={gap} seed={seed})")


# Measured locally: the 4-gap x 3-seed property sweep (12 cases, each
# time_per_sheet=1, minimize_sheets=False) plus the 5 regression cases above
# run in well under a minute total — see the PR/commit message for the number
# from this machine. Kept off hypothesis (not a project dependency) in favor
# of a plain seeded `random.Random` generator so the case set is exactly
# reproducible without adding a new dependency.
