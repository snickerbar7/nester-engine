"""Part silhouettes for API consumers — real shapes, small payloads.

The web product draws the TRUE outline of every flat part (outer boundary plus
holes), not a bounding rectangle: a drawn nest is the trust element, and a nest
of rectangles is a lie about what the laser will cut. The DXF reader flattens
arcs at a 0.1 mm sagitta, so a real part easily carries thousands of points —
far too many to ship per part, per placement, over HTTP.

This module is the bridge: it takes a :class:`~nester.sheet.model.FlatPart` and
returns a JSON-ready contour that is

  * **decimated** — Douglas-Peucker at ``tolerance`` mm (default 0.2 mm, an
    order of magnitude below any sheet-cutting tolerance), then escalated (the
    tolerance doubles) until every loop is at most ``max_points`` points. Only
    if doubling still can't get there does it fall back to uniform subsampling,
    so the point budget is a hard cap, not a hope;
  * **holes-preserving** — each interior loop is decimated independently and
    kept; a hole never gets merged away or dropped;
  * **origin-normalized** — coordinates are relative to the part's outer
    bounding-box minimum corner, so a contour is a reusable stamp: the same
    part placed 12 times ships its geometry once.

Pure geometry, no dependency beyond the stdlib — trivially unit-testable, and
usable from the engine, the CLI, or the service alike.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .model import Contour, FlatPart, Point

# Decimation defaults. 0.2 mm is well inside laser/plasma tolerance; 200 points
# per loop keeps a busy part's JSON in the low tens of kB.
DEFAULT_TOLERANCE = 0.2
MAX_POINTS = 200

# Coordinate rounding for the wire format: 0.001 mm — finer than any machine.
NDIGITS = 3

# Never decimate a loop below this; a triangle is the smallest real polygon.
_MIN_POINTS = 3
# Cap on tolerance escalation rounds (doubling) before uniform subsampling.
_MAX_ESCALATIONS = 24


def _perp_distance(p: Point, a: Point, b: Point) -> float:
    """Distance from ``p`` to the segment ``a``-``b`` (degenerate: to ``a``)."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def douglas_peucker(chain: Sequence[Point], tolerance: float) -> List[Point]:
    """Simplify an OPEN point chain, keeping both endpoints. Iterative (no recursion
    limit to trip over on a 20k-point spline)."""
    n = len(chain)
    if n <= 2 or tolerance <= 0:
        return list(chain)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        worst, worst_i = -1.0, -1
        a, b = chain[lo], chain[hi]
        for i in range(lo + 1, hi):
            d = _perp_distance(chain[i], a, b)
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance and worst_i > 0:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return [pt for i, pt in enumerate(chain) if keep[i]]


def _subsample(ring: Sequence[Point], max_points: int) -> List[Point]:
    """Last-resort uniform thinning so the point budget is a hard cap."""
    n = len(ring)
    if n <= max_points:
        return list(ring)
    step = n / float(max_points)
    out = [ring[int(i * step)] for i in range(max_points)]
    # int(i*step) can repeat on tiny budgets; keep the ring a valid polygon.
    dedup: List[Point] = []
    for p in out:
        if not dedup or p != dedup[-1]:
            dedup.append(p)
    return dedup if len(dedup) >= _MIN_POINTS else list(ring[:_MIN_POINTS])


def simplify_ring(
    ring: Sequence[Point],
    tolerance: float = DEFAULT_TOLERANCE,
    max_points: int = MAX_POINTS,
) -> List[Point]:
    """Decimate one CLOSED ring (implicitly closed — no repeated last point).

    Douglas-Peucker at ``tolerance``; if the result still exceeds ``max_points``
    the tolerance doubles until it fits, and uniform subsampling backstops that.
    """
    pts = list(ring)
    if len(pts) <= _MIN_POINTS:
        return pts
    if len(pts) <= max_points and tolerance <= 0:
        return pts

    tol = max(float(tolerance), 0.0)
    out = pts
    if tol > 0:
        # Simplify as an open chain that starts and ends on the same vertex, so
        # the closing edge is simplified like any other and the ring stays closed.
        for _ in range(_MAX_ESCALATIONS):
            simplified = douglas_peucker(pts + [pts[0]], tol)
            out = simplified[:-1] if len(simplified) > 1 else simplified
            if len(out) < _MIN_POINTS:
                out = pts if len(pts) <= max_points else _subsample(pts, max_points)
                break
            if len(out) <= max_points:
                break
            tol *= 2.0
    return out if len(out) <= max_points else _subsample(out, max_points)


def _round(pts: Sequence[Point], dx: float, dy: float, ndigits: int) -> List[List[float]]:
    return [[round(x - dx, ndigits), round(y - dy, ndigits)] for (x, y) in pts]


def contour_with_origin(
    outer: Sequence[Point],
    holes: Sequence[Contour] = (),
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_points: int = MAX_POINTS,
    ndigits: int = NDIGITS,
) -> Tuple[Dict[str, object], Point]:
    """The contour plus the ``(dx, dy)`` that was subtracted to normalize it.

    The contour is ``{"outer": [[x, y], ...], "holes": [[[x, y], ...], ...]}``
    in mm with its origin at the OUTER boundary's bounding-box minimum corner,
    so it is placement-independent. Callers doing placement maths need the
    offset as well, and it must come from the SAME decimation pass — simplifying
    can drop the vertex that held an extreme — hence one function returning both.
    """
    simple_outer = simplify_ring(outer, tolerance, max_points)
    dx = min(p[0] for p in simple_outer)
    dy = min(p[1] for p in simple_outer)
    out_holes: List[List[List[float]]] = []
    for hole in holes:
        h = simplify_ring(hole, tolerance, max_points)
        if len(h) >= _MIN_POINTS:
            out_holes.append(_round(h, dx, dy, ndigits))
    contour = {"outer": _round(simple_outer, dx, dy, ndigits), "holes": out_holes}
    return contour, (dx, dy)


def part_contour(
    part: FlatPart,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_points: int = MAX_POINTS,
    ndigits: int = NDIGITS,
) -> Dict[str, object]:
    """The JSON-ready silhouette of one flat part (outer + holes), mm."""
    contour, _origin = contour_with_origin(
        part.outer, part.holes, tolerance=tolerance,
        max_points=max_points, ndigits=ndigits)
    return contour


def part_contour_with_origin(
    part: FlatPart,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_points: int = MAX_POINTS,
    ndigits: int = NDIGITS,
) -> Tuple[Dict[str, object], Point]:
    """Silhouette + its normalization offset for one flat part.

    Placement maths: a placed copy's contour coordinates are
    ``rotate(contour, rotation) + contour_offset``, where ``contour_offset`` is
    ``rotate(origin, rotation) + (placement.x, placement.y)``.
    """
    return contour_with_origin(part.outer, part.holes, tolerance=tolerance,
                               max_points=max_points, ndigits=ndigits)
