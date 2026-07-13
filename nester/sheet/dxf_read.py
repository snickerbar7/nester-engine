"""Read flat parts from a DXF file.

Validated against real Fusion 360 sheet-metal flat-pattern exports, which use a
consistent layer convention:

    OUTER_PROFILES     -> the cut outline (one closed loop per part)
    INTERIOR_PROFILES  -> holes / interior cutouts
    BEND, BEND_EXTENT  -> fold lines (NOT cuts) — dropped

Every geometry entity (LINE, ARC, CIRCLE, LWPOLYLINE, POLYLINE, SPLINE,
ELLIPSE) is converted to a flattened point ring via ``ezdxf.path``. Closed
entities (a closed polyline, a circle) are complete loops; open entities
(the LINE/ARC segments Fusion emits for an outline) are stitched together by
matching endpoints. Each outer loop becomes a :class:`FlatPart`; interior loops
geometrically inside it become its holes.

If a file has no OUTER_PROFILES layer (a non-standard export), we fall back to
treating the largest closed loop as the outline and loops inside it as holes,
and warn.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import ezdxf
from ezdxf import path as ezpath
from shapely.geometry import Polygon
from shapely.geometry import Point as ShPoint

from .model import FlatPart, Point, polygon_area

# DXF INSUNITS flag -> millimetres per unit.
_UNIT_TO_MM = {
    0: 1.0,     # unitless -> assume mm
    1: 25.4,    # inches
    2: 304.8,   # feet
    4: 1.0,     # millimetres
    5: 10.0,    # centimetres
    6: 1000.0,  # metres
}

OUTER_LAYER = "OUTER_PROFILES"
INNER_LAYER = "INTERIOR_PROFILES"
# Layers that are never cut geometry (fold lines, reference). Matched case-insensitively.
_DROP_LAYERS = {"bend", "bend_extent"}

# Flattening sagitta (max deviation of a chord from the true arc), mm. 0.1mm is
# well below laser tolerance and keeps point counts modest.
DEFAULT_SAGITTA = 0.1
# Endpoint-match tolerance for stitching open segments into loops, mm.
DEFAULT_STITCH_TOL = 0.05


class DxfReadError(ValueError):
    pass


@dataclass
class _Loop:
    ring: List[Point]
    layer: str
    closed: bool


def read_parts(
    path: str,
    sagitta: float = DEFAULT_SAGITTA,
    stitch_tol: float = DEFAULT_STITCH_TOL,
    qty: int = 1,
) -> List[FlatPart]:
    """Extract flat parts from one DXF file. Returns one part per outer loop."""
    try:
        doc = ezdxf.readfile(path)
    except (IOError, ezdxf.DXFError) as e:
        raise DxfReadError(f"{os.path.basename(path)}: cannot read DXF ({e})")

    scale = _UNIT_TO_MM.get(int(doc.units or 0), 1.0)
    msp = doc.modelspace()

    outer_loops: List[List[Point]] = []
    inner_loops: List[List[Point]] = []
    open_by_layer: dict[str, List[List[Point]]] = {OUTER_LAYER: [], INNER_LAYER: []}
    fallback_open: List[List[Point]] = []
    fallback_closed: List[List[Point]] = []
    saw_outer_layer = False

    for e in msp:
        layer = str(getattr(e.dxf, "layer", "") or "")
        if layer.lower() in _DROP_LAYERS:
            continue
        ring = _entity_ring(e, scale, sagitta)
        if ring is None or len(ring) < 2:
            continue
        closed = _is_closed(ring, stitch_tol)

        if layer == OUTER_LAYER:
            saw_outer_layer = True
            (outer_loops if closed else open_by_layer[OUTER_LAYER]).append(_dedup(ring))
        elif layer == INNER_LAYER:
            (inner_loops if closed else open_by_layer[INNER_LAYER]).append(_dedup(ring))
        else:
            (fallback_closed if closed else fallback_open).append(_dedup(ring))

    # Stitch the open segments on each cut layer into closed loops.
    outer_loops += _stitch(open_by_layer[OUTER_LAYER], stitch_tol)
    inner_loops += _stitch(open_by_layer[INNER_LAYER], stitch_tol)

    warnings: List[str] = []
    if not saw_outer_layer and not outer_loops:
        # Non-standard export: no OUTER_PROFILES layer. Recover from all geometry.
        loops = fallback_closed + _stitch(fallback_open, stitch_tol)
        loops = [r for r in loops if len(r) >= 3 and polygon_area(r) > 0]
        if not loops:
            raise DxfReadError(
                f"{os.path.basename(path)}: no closed contours found on any layer "
                f"(expected an {OUTER_LAYER} layer)."
            )
        loops.sort(key=polygon_area, reverse=True)
        outer_loops = [loops[0]]
        inner_loops = loops[1:]
        warnings.append(
            f"{os.path.basename(path)}: no {OUTER_LAYER} layer — used largest of "
            f"{len(loops)} contour(s) as the outline."
        )

    outer_loops = [r for r in outer_loops if len(r) >= 3 and polygon_area(r) > 1e-9]
    inner_loops = [r for r in inner_loops if len(r) >= 3 and polygon_area(r) > 1e-9]
    if not outer_loops:
        raise DxfReadError(
            f"{os.path.basename(path)}: found no usable outer contour "
            f"(entities on {OUTER_LAYER} did not close into a loop)."
        )

    parts = _build_parts(os.path.basename(path), outer_loops, inner_loops, qty)
    # Reader warnings (e.g. fallback layer recovery) are exposed on the function
    # object so the CLI can surface them without complicating the return type.
    read_parts.last_warnings = warnings  # type: ignore[attr-defined]
    return parts


read_parts.last_warnings = []  # type: ignore[attr-defined]


def _build_parts(
    fname: str,
    outer_loops: List[List[Point]],
    inner_loops: List[List[Point]],
    qty: int,
) -> List[FlatPart]:
    """Pair holes with the outer loop that contains them; make one part each."""
    outers = [(_oriented(r, ccw=True), Polygon(r)) for r in outer_loops]
    remaining = list(inner_loops)
    hole_map: List[List[List[Point]]] = [[] for _ in outers]

    for hole in remaining:
        pt = ShPoint(_rep_point(hole))
        placed = False
        for i, (_ring, poly) in enumerate(outers):
            try:
                if poly.contains(pt):
                    hole_map[i].append(_oriented(hole, ccw=False))
                    placed = True
                    break
            except Exception:  # pragma: no cover - shapely predicate on degenerate poly
                continue
        # A hole not inside any outer is ignored (stray interior geometry).

    multi = len(outers) > 1
    parts: List[FlatPart] = []
    for i, (ring, _poly) in enumerate(outers):
        name = f"{fname}#{i + 1}" if multi else fname
        parts.append(
            FlatPart(
                name=name,
                outer=tuple(ring),
                holes=tuple(tuple(h) for h in hole_map[i]),
                qty=qty,
            )
        )
    return parts


# --------------------------------------------------------------------------- #
# Entity -> point ring
# --------------------------------------------------------------------------- #

def _entity_ring(e, scale: float, sagitta: float) -> Optional[List[Point]]:
    """Flatten any supported entity to a list of (x, y) points in mm."""
    try:
        p = ezpath.make_path(e)
    except (TypeError, ValueError):
        return None
    if len(p) == 0:
        return None
    pts = [(v.x * scale, v.y * scale) for v in p.flattening(sagitta / scale if scale else sagitta)]
    return pts if len(pts) >= 2 else None


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def _is_closed(ring: Sequence[Point], tol: float) -> bool:
    return len(ring) >= 3 and _near(ring[0], ring[-1], tol)


def _near(a: Point, b: Point, tol: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def _dedup(ring: List[Point], tol: float = 1e-7) -> List[Point]:
    """Drop consecutive duplicate points and any repeated closing point."""
    out: List[Point] = []
    for p in ring:
        if not out or not _near(out[-1], p, tol):
            out.append(p)
    if len(out) >= 2 and _near(out[0], out[-1], tol):
        out.pop()
    return out


def _stitch(chains: List[List[Point]], tol: float) -> List[List[Point]]:
    """Greedily join open point-chains into closed rings by matching endpoints."""
    pool = [list(c) for c in chains if len(c) >= 2]
    rings: List[List[Point]] = []
    while pool:
        cur = pool.pop()
        extended = True
        while extended:
            if len(cur) >= 3 and _near(cur[0], cur[-1], tol):
                break  # closed
            extended = False
            for i, ch in enumerate(pool):
                if _near(cur[-1], ch[0], tol):
                    cur.extend(ch[1:])
                elif _near(cur[-1], ch[-1], tol):
                    cur.extend(reversed(ch[:-1]))
                elif _near(cur[0], ch[0], tol):
                    cur[:0] = list(reversed(ch))[1:]
                elif _near(cur[0], ch[-1], tol):
                    cur[:0] = ch[:-1]
                else:
                    continue
                pool.pop(i)
                extended = True
                break
        rings.append(_dedup(cur))
    # keep only rings that actually closed into a polygon
    return [r for r in rings if len(r) >= 3]


def _signed_area(ring: Sequence[Point]) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s * 0.5


def _oriented(ring: List[Point], ccw: bool) -> List[Point]:
    """Return the ring wound counter-clockwise (ccw=True) or clockwise."""
    a = _signed_area(ring)
    is_ccw = a > 0
    if is_ccw != ccw:
        return list(reversed(ring))
    return list(ring)


def _rep_point(ring: Sequence[Point]) -> Point:
    """A point guaranteed inside the ring (shapely representative point)."""
    try:
        rp = Polygon(ring).representative_point()
        return (rp.x, rp.y)
    except Exception:  # pragma: no cover
        # centroid fallback
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
