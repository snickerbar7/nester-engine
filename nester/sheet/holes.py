"""Nest small parts inside the holes of already-placed parts — E15.

The Rust engine (spyrrow/jagua-rs) packs *simple* polygons: it sees a part's
outer boundary and nothing else, so a 300 mm hole in a flange is, to the solver,
solid material that happens to get cut away. On real jobs — flanges, bases,
electrical panels — that is a lot of sheet leaving the shop as skeleton.

This module is the second pass that reclaims it. The main nest runs first and
owns the layout; then, for every part on the sheet, each of its holes becomes a
little container and we try to drop the still-unplaced parts into it. Nothing
here changes what the solver decided — it only fills voids the solver could not
see, so a job can never come out worse for enabling it.

**Why a bespoke search instead of the engine.** A hole is an arbitrary closed
region, not a strip, and jagua-rs has no container-with-holes problem to pose
it to. The regions are also small and few, and the parts that fit in them are
small, so an honest bounded search — candidate translations on a grid, exact
containment tested with shapely — costs milliseconds and is trivially correct.
It is a first-fit, not an optimum: it never claims to be one.

**What the shop must know.** A part cut out of another part's hole is inside the
slug. Someone has to lift that slug off the table and *not* throw it in the drop
bin, and on machines that let slugs fall it needs a tab or a support. Every
placement made here is flagged (``Placement.in_hole_of``) so the plan can say so
in print. Because of that consequence the pass is OFF unless asked for.

Rules it will not break:

* clearance to the hole edge is the job's part gap — the hole rim is a cut line
  like any other, and two parts sharing a hole keep that gap between them too;
* only one level deep — a part nested in a hole does not itself host parts;
* a part is only ever placed if it is fully contained, tested exactly against
  the real contour, never against a bounding box.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .model import FlatPart, Placement, Point, SheetLayout, transform

# Angles tried for a freely-rotatable part. A hole is small and usually round or
# rectangular, so the useful orientations are the square ones plus the diagonals;
# sweeping a finer wheel multiplies cost without finding fits that matter.
_FREE_ANGLES: Tuple[float, ...] = (0.0, 90.0, 180.0, 270.0, 45.0, 135.0, 225.0, 315.0)

# Translation search resolution. The candidate grid is sized so the long side of
# the search window is cut into this many steps — bounded work per attempt, and
# finer than any gap a shop actually cuts to.
_GRID_STEPS = 40
_MIN_STEP = 0.5          # mm; no point searching below half a millimetre
_EPS = 1e-9


def _shapely():
    """Import shapely lazily; None when it is unavailable.

    The pass is optional, so a missing shapely degrades to "no hole nesting"
    rather than breaking a job that never asked for it.
    """
    try:
        from shapely.geometry import Polygon           # noqa: WPS433
        from shapely.prepared import prep              # noqa: WPS433
    except Exception:  # pragma: no cover - shapely is a declared dependency
        return None
    return Polygon, prep


def _clean(Polygon, ring: Sequence[Point]):
    """A valid shapely polygon from a raw ring, or None if it has no area."""
    if len(ring) < 3:
        return None
    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)          # heals self-touching rings from CAD
    if poly.is_empty or poly.area <= _EPS:
        return None
    return poly


def _occupants(Polygon, layout: SheetLayout) -> List[Tuple[int, object]]:
    """(host index, footprint) for every copy ALREADY sitting inside a hole.

    Without this the pass is not re-entrant: called a second time on the same
    layout it would rebuild an empty obstacle list and drop new parts exactly on
    top of the ones it placed before. ``pack.nest`` happens to call it once per
    sheet, but a top-up or resume path would silently emit overlapping cuts, so
    the guard belongs here rather than in the caller's discipline.
    """
    out: List[Tuple[int, object]] = []
    for pl in layout.placements:
        if not pl.is_in_hole:
            continue
        poly = _clean(Polygon, transform(pl.part.outer, pl.rotation, pl.x, pl.y))
        if poly is not None:
            out.append((pl.in_hole_of, poly))
    return out


def _regions(Polygon, layout: SheetLayout, gap: float) -> List[Tuple[int, object]]:
    """Every hole on the sheet, shrunk by the gap, as (host index, region).

    Hosts already nested inside a hole are skipped: one level only. The result is
    ordered biggest region first so the roomiest hole gets first pick of parts.
    """
    out: List[Tuple[int, object]] = []
    for host_idx, pl in enumerate(layout.placements):
        if pl.is_in_hole:
            continue
        for hole in pl.part.holes:
            ring = transform(hole, pl.rotation, pl.x, pl.y)
            poly = _clean(Polygon, ring)
            if poly is None:
                continue
            usable = poly.buffer(-gap) if gap > 0 else poly
            if usable.is_empty:
                continue
            # buffer() can split a pinched hole into several pockets.
            geoms = getattr(usable, "geoms", None)
            for piece in (list(geoms) if geoms is not None else [usable]):
                if piece.is_empty or piece.area <= _EPS:
                    continue
                out.append((host_idx, piece))
    out.sort(key=lambda pair: -pair[1].area)
    return out


def _angles(allowed: Optional[Sequence[float]]) -> Tuple[float, ...]:
    if allowed is None:
        return _FREE_ANGLES
    return tuple(allowed) or (0.0,)


def _rotated(part: FlatPart, deg: float) -> Tuple[List[Point], float, float, float, float]:
    """Part outline rotated about its origin, plus that outline's bbox."""
    pts = transform(part.outer, deg, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return pts, min(xs), min(ys), max(xs), max(ys)


def _try_place(Polygon, prep_region, region, part: FlatPart, deg: float,
               blockers: List[object], gap: float):
    """First translation at this angle that lands the part inside the region.

    Returns ``(x, y)`` in absolute sheet coordinates, or None. Candidates are
    swept bottom-left first so parts stack into a corner of the hole and leave
    one contiguous pocket instead of several useless slivers.
    """
    pts, rx0, ry0, rx1, ry1 = _rotated(part, deg)
    w, h = rx1 - rx0, ry1 - ry0
    bx0, by0, bx1, by1 = region.bounds
    span_x, span_y = (bx1 - bx0) - w, (by1 - by0) - h
    if span_x < -_EPS or span_y < -_EPS:
        return None                                     # cannot fit at this angle

    step = max(_MIN_STEP, max(span_x, span_y, 0.0) / _GRID_STEPS)
    nx = int(max(span_x, 0.0) / step) + 1
    ny = int(max(span_y, 0.0) / step) + 1

    for iy in range(ny):
        ty = (by0 - ry0) + min(iy * step, max(span_y, 0.0))
        for ix in range(nx):
            tx = (bx0 - rx0) + min(ix * step, max(span_x, 0.0))
            cand = Polygon([(x + tx, y + ty) for (x, y) in pts])
            if not prep_region.contains(cand):
                continue
            if gap > 0 and any(cand.distance(b) < gap - 1e-6 for b in blockers):
                continue
            return tx, ty
    return None


def nest_into_holes(
    layout: SheetLayout,
    catalog: Dict[str, FlatPart],
    remaining: Dict[str, int],
    orient: Dict[str, Optional[Tuple[float, ...]]],
    gap: float,
) -> int:
    """Fill this sheet's holes with still-unplaced parts. Returns copies placed.

    Mutates ``layout.placements`` (appending flagged placements) and ``remaining``
    (decrementing what it consumed), mirroring how the main loop harvests a solve.
    """
    mod = _shapely()
    if mod is None:
        return 0
    Polygon, prep = mod

    if not any(n > 0 for n in remaining.values()):
        return 0
    regions = _regions(Polygon, layout, gap)
    if not regions:
        return 0

    # Biggest part first: a hole that can take the big one should not be spent
    # on a washer. Areas are fixed, so this order is computed once.
    order = sorted(catalog, key=lambda pid: -catalog[pid].outer_area)
    smallest = min((catalog[pid].outer_area for pid in catalog), default=0.0)
    already = _occupants(Polygon, layout)

    placed = 0
    for host_idx, region in regions:
        if not any(n > 0 for n in remaining.values()):
            break
        if region.area < smallest:
            continue
        prep_region = prep(region)
        # Seed the obstacle list with whatever is already in this host's holes,
        # so a repeat call tops the sheet up instead of double-booking it.
        blockers: List[object] = [
            poly for owner, poly in already
            if owner == host_idx and region.intersects(poly)
        ]
        progress = True
        while progress:
            progress = False
            for pid in order:
                if remaining.get(pid, 0) <= 0:
                    continue
                part = catalog[pid]
                if part.outer_area > region.area:
                    continue
                for deg in _angles(orient.get(pid)):
                    hit = _try_place(Polygon, prep_region, region, part, deg,
                                     blockers, gap)
                    if hit is None:
                        continue
                    tx, ty = hit
                    layout.placements.append(
                        Placement(part=part, x=tx, y=ty, rotation=deg,
                                  in_hole_of=host_idx)
                    )
                    footprint = _clean(Polygon, transform(part.outer, deg, tx, ty))
                    if footprint is not None:
                        blockers.append(footprint)
                    remaining[pid] -= 1
                    placed += 1
                    progress = True
                    break
                if progress:
                    break                 # restart the size order after a placement
    return placed
