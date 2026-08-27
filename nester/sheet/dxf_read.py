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

# DXF $INSUNITS flag -> millimetres per unit (the full documented table). Code 0
# ("unitless") is the one deliberate exception: it means no unit was declared,
# and the documented convention is to treat it as mm. Every OTHER declared code
# must be either mapped here or rejected loudly — never silently defaulted.
_UNIT_TO_MM = {
    0: 1.0,                      # unitless -> assume mm (documented convention)
    1: 25.4,                     # inches
    2: 304.8,                    # feet
    3: 1609344.0,                # miles
    4: 1.0,                      # millimetres
    5: 10.0,                     # centimetres
    6: 1000.0,                   # metres
    7: 1e6,                      # kilometres
    8: 2.54e-5,                  # microinches
    9: 0.0254,                   # mils
    10: 914.4,                   # yards
    11: 1e-7,                    # angstroms
    12: 1e-6,                    # nanometres
    13: 1e-3,                    # microns
    14: 100.0,                   # decimetres
    15: 10000.0,                 # decametres
    16: 100000.0,                # hectometres
    17: 1e12,                    # gigametres
    18: 1.495978707e14,          # astronomical units
    19: 9.4607304725808e18,      # light years
    20: 3.0856775814913673e19,   # parsecs
    21: 304.8006096012192,       # US survey feet
    22: 25.400050800101603,      # US survey inch
    23: 914.4018288036576,       # US survey yard
    24: 1609347.2186944375,      # US survey mile
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
# Coincidence tolerance for dropping a chain drawn twice in the same place, mm.
# Deliberately far tighter than DEFAULT_STITCH_TOL — see _drop_coincident.
COINCIDENT_TOL = 1e-6


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

    units_code = int(doc.units or 0)
    if units_code not in _UNIT_TO_MM:
        raise DxfReadError(
            f"{os.path.basename(path)}: unrecognized $INSUNITS code {units_code} "
            f"— refusing to guess a scale (declared unit is not in the DXF spec's "
            f"0-24 table)."
        )
    scale = _UNIT_TO_MM[units_code]
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

    # Stitch the open segments on each cut layer into closed loops via planar
    # face traversal (see _stitch). Dangling / unclosed input never becomes a
    # ring silently — it comes back as a warning naming the gap.
    fname = os.path.basename(path)
    warnings: List[str] = []
    outer_stitched, outer_warn = _stitch(open_by_layer[OUTER_LAYER], stitch_tol)
    inner_stitched, inner_warn = _stitch(open_by_layer[INNER_LAYER], stitch_tol)
    outer_loops += outer_stitched
    inner_loops += inner_stitched
    warnings += [f"{fname}: {OUTER_LAYER} {w}" for w in outer_warn]
    warnings += [f"{fname}: {INNER_LAYER} {w}" for w in inner_warn]

    if not saw_outer_layer and not outer_loops:
        # Non-standard export: no OUTER_PROFILES layer. Recover from all geometry.
        fallback_stitched, fallback_warn = _stitch(fallback_open, stitch_tol)
        warnings += [f"{fname}: (fallback) {w}" for w in fallback_warn]
        loops = fallback_closed + fallback_stitched
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


def _stitch(chains: List[List[Point]], tol: float) -> Tuple[List[List[Point]], List[str]]:
    """Decompose open point-chains into closed rings by planar face traversal.

    This is the standard way to turn a soup of line segments into polygon
    loops: cluster shared endpoints into nodes, turn each chain into a pair of
    directed half-edges, sort the half-edges leaving each node by direction,
    and walk faces by always continuing into the half-edge immediately
    clockwise from the current edge's twin. Two rings that only TOUCH at a
    corner (share a node) come out as two separate faces by construction,
    because the walk that enters a cut-vertex leaves it via the edge that is
    adjacent in ANGLE, not merely "some other chain at this point". A chain
    end that never pairs with anything within ``tol`` is a real degree-1 node:
    its face walk goes out and back along the same chain (zero signed area) and
    is dropped, and it is reported by name in the second return value instead
    of being silently absorbed into a plausible-looking ring.

    Returns ``(rings, warnings)``. ``rings`` are CCW, deduplicated, own their
    original coordinates (node clustering is for CONNECTIVITY ONLY — a
    sub-tolerance gap never perturbs a reported ring's geometry).
    """
    chains = [list(c) for c in chains if len(c) >= 2]
    if not chains:
        return [], []

    # 0. Drop coincident duplicate chains. Two parts BUTTED along a shared edge
    # are each drawn with their own copy of that edge, so the wall between them
    # exists twice. Two coincident half-edges leave a node at the IDENTICAL
    # angle, which makes the angular order around that node ambiguous and
    # collapses the traversal into the union's outer boundary — the butted parts
    # came back as one blob. Kept once, the wall is simply the edge those two
    # faces share, which is what it physically is.
    chains = _drop_coincident(chains, COINCIDENT_TOL)

    # 1. Cluster chain endpoints into nodes (union by proximity, tol-only).
    endpoints = [c[0] for c in chains] + [c[-1] for c in chains]
    node_of: List[int] = [-1] * len(endpoints)
    node_points: List[Point] = []
    node_members: List[List[Point]] = []
    for i, p in enumerate(endpoints):
        found = None
        for nid, rep in enumerate(node_points):
            if _near(p, rep, tol):
                found = nid
                break
        if found is None:
            found = len(node_points)
            node_points.append(p)
            node_members.append([])
        node_members[found].append(p)
        node_of[i] = found
    n = len(chains)

    def start_node(k: int) -> int:
        return node_of[k]

    def end_node(k: int) -> int:
        return node_of[n + k]

    # 2. Each chain -> two directed half-edges (forward, reverse). Half-edge
    # index 2k = forward (chain k as drawn), 2k+1 = reverse. twin(i) = i^1.
    he_start: List[int] = []
    he_end: List[int] = []
    he_pts: List[List[Point]] = []
    he_angle: List[float] = []
    for k, c in enumerate(chains):
        u, v = start_node(k), end_node(k)
        p0, p1 = c[0], c[1]
        he_start.append(u)
        he_end.append(v)
        he_pts.append(c)
        he_angle.append(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        rc = list(reversed(c))
        q0, q1 = rc[0], rc[1]
        he_start.append(v)
        he_end.append(u)
        he_pts.append(rc)
        he_angle.append(math.atan2(q1[1] - q0[1], q1[0] - q0[0]))

    def twin(i: int) -> int:
        return i ^ 1

    # 3. Half-edges leaving each node, sorted CCW by direction angle.
    by_node: dict[int, List[int]] = {}
    for i, u in enumerate(he_start):
        by_node.setdefault(u, []).append(i)
    for u in by_node:
        by_node[u].sort(key=lambda i: he_angle[i])

    # 4. Face traversal: from half-edge h ending at v, the next half-edge of
    # THIS face is the one immediately clockwise (predecessor in the CCW
    # order) from twin(h) in v's rotation. This is the standard rule that
    # traces faces with the face interior kept on the left, i.e. yields
    # CCW-wound boundaries for bounded faces (verified below: a lone closed
    # square comes back as itself, and two squares sharing one corner come
    # back as two separate rings with their true areas).
    used = [False] * len(he_start)
    rings: List[List[Point]] = []
    ambiguous = 0  # walks that never closed back onto themselves

    for start_i in range(len(he_start)):
        if used[start_i]:
            continue
        pts: List[Point] = [he_pts[start_i][0]]
        cur = start_i
        steps = 0
        limit = len(he_start) + 2
        closed_walk = False
        while True:
            used[cur] = True
            pts.extend(he_pts[cur][1:])
            v = he_end[cur]
            t = twin(cur)
            neighbors = by_node.get(v, [])
            if t not in neighbors:
                break  # inconsistent embedding; can't happen by construction
            pos = neighbors.index(t)
            nxt = neighbors[(pos - 1) % len(neighbors)]
            steps += 1
            if nxt == start_i:
                closed_walk = True
                break
            if steps > limit:
                break  # never returned to start: self-intersecting/ambiguous input
            cur = nxt

        if not closed_walk:
            ambiguous += 1
            continue  # never emit a ring built from an unclosed face walk

        ring = _dedup(pts)
        area = _signed_area(ring) if len(ring) >= 3 else 0.0
        if len(ring) >= 3 and area > 1e-9:
            rings.append(_oriented(ring, ccw=True))

    warnings = _dangling_warnings(chains, node_of, n)
    warnings += _weld_warnings(node_members)
    if ambiguous:
        warnings.append(
            f"has {ambiguous} contour(s) that could not be traced into a face "
            f"(self-intersecting or ambiguous input) — dropped rather than "
            f"guessed."
        )
    return rings, warnings


def _drop_coincident(chains: List[List[Point]], tol: float) -> List[List[Point]]:
    """Keep one copy of each chain; a chain drawn twice at the same place (in
    either direction) is a duplicate, not a second wall.

    The match is EXACT (``COINCIDENT_TOL``, a nanometre), deliberately not the
    stitch tolerance: butted parts carry byte-identical copies of the edge they
    share, whereas two walls a few hundredths apart are two real walls and
    dropping one of those would silently move material. Nothing is assumed
    about the geometry here, so no warning is owed.
    """
    kept: List[List[Point]] = []
    seen: dict = {}
    q = max(tol, 1e-9)
    for c in chains:
        # Bucket by the unordered endpoint pair so the comparison stays cheap.
        a = (round(c[0][0] / q), round(c[0][1] / q))
        b = (round(c[-1][0] / q), round(c[-1][1] / q))
        key = (a, b) if a <= b else (b, a)
        dup = False
        for other in seen.get(key, ()):
            if _same_chain(c, other, tol):
                dup = True
                break
        if dup:
            continue
        seen.setdefault(key, []).append(c)
        kept.append(c)
    return kept


def _same_chain(a: List[Point], b: List[Point], tol: float) -> bool:
    if len(a) != len(b):
        return False
    if all(_near(p, q, tol) for p, q in zip(a, b)):
        return True
    return all(_near(p, q, tol) for p, q in zip(a, reversed(b)))


def _weld_warnings(node_members: List[List[Point]]) -> List[str]:
    """Name the sub-tolerance WELDS — the genuine ambiguity, made audible.

    ``stitch_tol`` exists to close the small gaps CAD leaves inside ONE outline,
    and it cannot tell that apart from two distinct parts drawn a hair away from
    each other. That is a real ambiguity, not an oversight, and tightening the
    tolerance would break the real exports this reader is validated against. So
    the reader says what it did instead of staying silent.

    Only JUNCTIONS are reported: a node where four or more chain-ends meet AND
    the points that landed there were not actually coincident. Two ends meeting
    across a small gap is one outline being closed — exactly what the tolerance
    is for, and silent. Four ends meeting across a gap is two loops being welded
    together, which is where a part count can quietly go short.
    """
    welds = []
    for pts in node_members:
        if len(pts) < 4:
            continue
        rep = pts[0]
        d = max(math.hypot(p[0] - rep[0], p[1] - rep[1]) for p in pts)
        if d > 1e-9:
            welds.append((d, rep))
    if not welds:
        return []
    worst = max(welds)[0]
    where = ", ".join(f"({p[0]:.2f}, {p[1]:.2f})" for _d, p in sorted(welds)[:3])
    return [
        f"welded {len(welds)} junction(s) where 4+ contour ends met but were up "
        f"to {worst:.3f} mm apart (stitch tolerance) — at {where}. If those were "
        f"meant to be SEPARATE parts, they are now joined and the part count is "
        f"short; check it against the drawing."
    ]


def _dangling_warnings(
    chains: List[List[Point]],
    node_of: List[int],
    n: int,
) -> List[str]:
    """Name every open (degree-1) node with the nearest other open endpoint."""
    # A node's total incidence = # chain-ends coincident there (one outgoing
    # half-edge per chain-end). Degree 1 means only one chain touches that
    # point within tol: a real gap, not merely tolerance noise (anything
    # within tol was already merged into the same node by the caller).
    incidence: dict[int, int] = {}
    for u in node_of:
        incidence[u] = incidence.get(u, 0) + 1

    raw: List[Tuple[Point, int]] = []
    for k, c in enumerate(chains):
        raw.append((c[0], node_of[k]))
        raw.append((c[-1], node_of[n + k]))

    dangling = [(p, nid) for p, nid in raw if incidence.get(nid, 0) == 1]
    if not dangling:
        return []

    warnings: List[str] = []
    reported: set = set()
    reported_pairs: set = set()
    for i, (p, nid) in enumerate(dangling):
        if nid in reported:
            continue
        reported.add(nid)
        best = None
        best_d = None
        best_nid = None
        for j, (q, ojd) in enumerate(dangling):
            if ojd == nid:
                continue
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            if best_d is None or d < best_d:
                best_d = d
                best = q
                best_nid = ojd
        pair = frozenset((nid, best_nid)) if best_nid is not None else None
        if pair is not None and pair in reported_pairs:
            continue
        if pair is not None:
            reported_pairs.add(pair)
        if best is None:
            warnings.append(
                f"has an open contour — endpoint ({p[0]:.2f}, {p[1]:.2f}) never "
                f"reconnects (no other open endpoint in this layer); not closed, "
                f"contour dropped."
            )
        else:
            warnings.append(
                f"has an open contour — {best_d:.2f} mm gap between "
                f"({p[0]:.2f}, {p[1]:.2f}) and ({best[0]:.2f}, {best[1]:.2f}); "
                f"not closed, contour dropped."
            )
    return warnings


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
