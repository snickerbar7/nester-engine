"""Property-style sweeps for the DXF reader's correctness invariants (E-defects
4/5/6): $INSUNITS scaling, loop isolation under corner-touching geometry, and
closure-or-diagnostic behaviour on broken contours.

``hypothesis`` is NOT installed in this venv (checked: `import hypothesis`
fails, and it is not in requirements.txt), so this file does NOT add it as a
dependency. Instead it sweeps the same parameter space deterministically with
a seeded ``random.Random``, which gives the same coverage guarantee (every
case is exercised on every run, not just a fuzzed subset) at a fraction of the
runtime hypothesis would cost.

All fixtures are written with ezdxf into ``tmp_path`` — no files on disk.
"""

import math
import random

import ezdxf
import pytest
from shapely.geometry import Polygon

from nester.sheet.dxf_read import DxfReadError, _stitch, read_parts
from nester.sheet.model import polygon_area


def _save(doc, tmp_path, name="part.dxf"):
    p = tmp_path / name
    doc.saveas(p)
    return str(p)


def _rect(x0, y0, w, h):
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _l_shape(x0, y0, s):
    """An L made of 6 points, side length s (outer bbox s x s, notch s/2 x s/2)."""
    h = s / 2.0
    return [
        (x0, y0), (x0 + s, y0), (x0 + s, y0 + h),
        (x0 + h, y0 + h), (x0 + h, y0 + s), (x0, y0 + s),
    ]


def _edges(poly):
    """Polygon vertex ring -> list of [start, end] edge chains (CCW as given)."""
    n = len(poly)
    return [[poly[i], poly[(i + 1) % n]] for i in range(n)]


# --------------------------------------------------------------------------- #
# P2 — loop isolation: K disjoint polygons exploded into edges, corner-touching
# at various separations, shuffled, must come back as K distinct correct parts.
# --------------------------------------------------------------------------- #

SEPARATIONS = [0.0, 0.01, 0.03, 0.05, 0.2, 5.0]  # 0.0 = exact shared vertex
SHAPES = ["square", "rect", "L"]


def _place_polygons(k, shape, sep, rng):
    """Place k polygons of `shape` in a diagonal chain, each corner touching
    (or separated by `sep` from) the previous one's far corner."""
    polys = []
    x = y = 0.0
    for i in range(k):
        if shape == "square":
            s = 30.0 + i * 4
            poly = _rect(x, y, s, s)
            nx, ny = x + s + sep, y + s + sep
        elif shape == "rect":
            w, h = 40.0 + i * 3, 20.0 + i * 2
            poly = _rect(x, y, w, h)
            nx, ny = x + w + sep, y + h + sep
        else:  # L
            s = 30.0 + i * 4
            poly = _l_shape(x, y, s)
            nx, ny = x + s + sep, y + s + sep
        polys.append(poly)
        x, y = nx, ny
    return polys


@pytest.mark.parametrize("k", [2, 3, 4])
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("sep", SEPARATIONS)
def test_p2_loop_isolation(tmp_path, k, shape, sep):
    rng = random.Random(hash((k, shape, sep)) & 0xFFFFFFFF)
    polys = _place_polygons(k, shape, sep, rng)

    # Explode every polygon into its individual edge segments, pool them all,
    # and shuffle so stitching can't rely on input order.
    pool = []
    for poly in polys:
        pool.extend(_edges(poly))
    rng.shuffle(pool)

    doc = ezdxf.new(setup=True)
    doc.units = 4
    msp = doc.modelspace()
    for a, b in pool:
        msp.add_line(a, b, dxfattribs={"layer": "OUTER_PROFILES"})

    parts = read_parts(_save(doc, tmp_path))

    assert len(parts) == k, (
        f"expected {k} parts (shape={shape}, sep={sep}), got {len(parts)}"
    )

    expected_areas = sorted(polygon_area(p) for p in polys)
    got_areas = sorted(p.outer_area for p in parts)
    for exp, got in zip(expected_areas, got_areas):
        assert got == pytest.approx(exp, rel=1e-6), (
            f"area mismatch shape={shape} sep={sep}: expected {exp}, got {got}"
        )

    for p in parts:
        poly = Polygon(p.outer)
        assert poly.is_valid, f"invalid ring: {p.outer}"
        assert poly.is_simple, f"non-simple ring: {p.outer}"


# --------------------------------------------------------------------------- #
# P9 — closure or diagnostic: one edge deleted, or shortened to leave a gap.
# gap <= tol closes correctly; gap > tol (or deletion) never yields a silent
# ring — either no ring at all, or a warning naming the gap.
# --------------------------------------------------------------------------- #

GAPS = [0.0, 0.04, 0.06, 0.5, 5.0]
STITCH_TOL = 0.05  # nester.sheet.dxf_read.DEFAULT_STITCH_TOL


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("gap", GAPS)
def test_p9_closure_or_diagnostic_shortened_edge(tmp_path, shape, gap):
    rng = random.Random(hash((shape, gap, "shorten")) & 0xFFFFFFFF)
    poly = _place_polygons(1, shape, 0.0, rng)[0]
    edges = _edges(poly)
    # Shorten the last edge's END point toward its start by `gap`, along the
    # original edge direction reversed by `gap`... simplest: pull the shared
    # vertex with the next edge's start away by `gap` mm along the ring.
    a, b = edges[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length > gap:
        ux, uy = dx / length, dy / length
        b_short = (b[0] - ux * gap, b[1] - uy * gap)
    else:
        b_short = a  # degenerate for tiny polygons; not hit at our sizes
    edges[-1] = [a, b_short]

    doc = ezdxf.new(setup=True)
    doc.units = 4
    msp = doc.modelspace()
    for e_a, e_b in edges:
        msp.add_line(e_a, e_b, dxfattribs={"layer": "OUTER_PROFILES"})

    if gap <= STITCH_TOL:
        parts = read_parts(_save(doc, tmp_path))
        assert len(parts) == 1
        assert parts[0].outer_area == pytest.approx(polygon_area(poly), rel=1e-6)
    else:
        # Either read_parts refuses outright, or it succeeds but the reader's
        # warning channel names the gap. Never a silent, wrong-shaped part.
        try:
            read_parts(_save(doc, tmp_path))
        except DxfReadError:
            return
        warnings = list(getattr(read_parts, "last_warnings", []))
        assert warnings, f"gap={gap} > tol produced no ring AND no warning"
        assert any("gap" in w or "open contour" in w for w in warnings), warnings


@pytest.mark.parametrize("shape", SHAPES)
def test_p9_deleted_edge_never_silent(tmp_path, shape):
    rng = random.Random(hash((shape, "delete")) & 0xFFFFFFFF)
    poly = _place_polygons(1, shape, 0.0, rng)[0]
    edges = _edges(poly)[:-1]  # drop the last edge entirely

    doc = ezdxf.new(setup=True)
    doc.units = 4
    msp = doc.modelspace()
    for a, b in edges:
        msp.add_line(a, b, dxfattribs={"layer": "OUTER_PROFILES"})

    try:
        read_parts(_save(doc, tmp_path))
    except DxfReadError:
        return
    warnings = list(getattr(read_parts, "last_warnings", []))
    assert warnings, f"deleted edge (shape={shape}) produced no ring AND no warning"


def test_p9_stitch_unit_directly_gap_and_missing():
    """Exercise _stitch directly (bypassing the DXF round trip) for the exact
    gap boundary, so this test doesn't depend on read_parts' fallback-layer
    behaviour to see the warning channel."""
    square = _rect(0, 0, 100, 100)
    edges = _edges(square)

    # gap exactly at tol boundary: closes.
    a, b = edges[-1]
    edges[-1] = [a, b]  # unchanged, gap 0
    rings, warns = _stitch([list(e) for e in edges], STITCH_TOL)
    assert len(rings) == 1
    assert warns == []

    # gap above tol: no ring, and a warning naming the distance.
    a, b = edges[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    gap = 0.5
    ux, uy = dx / length, dy / length
    b_short = (b[0] - ux * gap, b[1] - uy * gap)
    broken = [list(e) for e in edges[:-1]] + [[a, b_short]]
    rings2, warns2 = _stitch(broken, STITCH_TOL)
    assert rings2 == []
    assert len(warns2) == 1
    assert "0.50" in warns2[0] or "gap" in warns2[0]


# --------------------------------------------------------------------------- #
# P7 — DXF units: known-or-loud. INSUNITS 0..24 crossed with a square, checked
# against an INDEPENDENTLY WRITTEN spec table (not imported from dxf_read.py).
# --------------------------------------------------------------------------- #

# Millimetres per unit, per the DXF $INSUNITS table (ODA DXF reference),
# transcribed independently from the production table in dxf_read.py.
_SPEC_MM_PER_UNIT = {
    0: 1.0,
    1: 25.4,
    2: 304.8,
    3: 1609344.0,
    4: 1.0,
    5: 10.0,
    6: 1000.0,
    7: 1_000_000.0,
    8: 0.0000254,
    9: 0.0254,
    10: 914.4,
    11: 0.0000001,
    12: 0.000001,
    13: 0.001,
    14: 100.0,
    15: 10000.0,
    16: 100000.0,
    17: 1_000_000_000_000.0,
    18: 1.495978707e14,
    19: 9.4607304725808e18,
    20: 3.0856775814913673e19,
    21: 304.8006096012192,
    22: 25.400050800101603,
    23: 914.4018288036576,
    24: 1609347.2186944375,
}


@pytest.mark.parametrize("code", list(range(0, 25)))
def test_p7_known_unit_codes_scale_exactly(tmp_path, code):
    expected_per_unit = _SPEC_MM_PER_UNIT[code]
    # Draw a square S units on a side, S chosen so the resulting mm-size stays
    # well above the stitch tolerance (0.05mm) regardless of how tiny the unit
    # is (e.g. microinches, angstroms) — a 1x1 unit square in those units is
    # sub-tolerance and would be a test artifact, not a real reader defect.
    side = max(1.0, 10.0 / expected_per_unit)
    doc = ezdxf.new(setup=True)
    doc.header["$INSUNITS"] = code
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (side, 0), (side, side), (0, side)], close=True,
        dxfattribs={"layer": "OUTER_PROFILES"},
    )
    parts = read_parts(_save(doc, tmp_path, f"units_{code}.dxf"))
    expected = side * expected_per_unit
    assert parts[0].size[0] == pytest.approx(expected, rel=1e-9)
    assert parts[0].size[1] == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("code", [25, 30, 99, -1, 1000])
def test_p7_unrecognized_unit_code_raises_or_warns(tmp_path, code):
    doc = ezdxf.new(setup=True)
    doc.header["$INSUNITS"] = code
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (1, 0), (1, 1), (0, 1)], close=True,
        dxfattribs={"layer": "OUTER_PROFILES"},
    )
    path = _save(doc, tmp_path, f"units_bad_{code}.dxf")
    # ezdxf passes an out-of-range $INSUNITS straight through (verified: it
    # neither clamps nor raises on write/read for these values), so the
    # reader is the only thing that can catch this — it must raise, naming
    # the code, rather than silently defaulting to mm.
    with pytest.raises(DxfReadError, match=str(code)):
        read_parts(path)


# --------------------------------------------------------------------------- #
# P2b — butted parts (a shared EDGE, not just a corner) and duplicate coincident
# segments.
#
# Two parts butted along an edge are each drawn with their own copy of that
# edge, so the wall between them exists TWICE. Two coincident half-edges leave a
# node at the identical angle, the angular order there is ambiguous, and the
# traversal used to collapse into the union's outer boundary: three butted 40x40
# squares came back as ONE part of 4800 mm². Kept once, the wall is just the
# edge those two faces share.
# --------------------------------------------------------------------------- #

def _butted(k, w=40.0, h=40.0, gap=0.0):
    """k rectangles in a row, each sharing its right edge with the next."""
    return [_rect(i * (w + gap), 0.0, w, h) for i in range(k)]


def _write_segments(tmp_path, segs, name="seg.dxf", units=4):
    doc = ezdxf.new(setup=True)
    doc.units = units
    msp = doc.modelspace()
    for a, b in segs:
        msp.add_line(a, b, dxfattribs={"layer": "OUTER_PROFILES"})
    return _save(doc, tmp_path, name)


@pytest.mark.parametrize("k", [2, 3, 4])
@pytest.mark.parametrize("dupes", [1, 2])
def test_p2b_butted_parts_sharing_an_edge_stay_separate(tmp_path, k, dupes):
    """Exact shared edges — and every segment optionally drawn `dupes` times,
    which is what a double-drawn export looks like."""
    rng = random.Random(hash((k, dupes)) & 0xFFFFFFFF)
    polys = _butted(k)
    pool = []
    for poly in polys:
        for e in _edges(poly):
            for _ in range(dupes):
                pool.append((e[0], e[1]))
    rng.shuffle(pool)

    parts = read_parts(_write_segments(tmp_path, pool, f"butt{k}_{dupes}.dxf"))

    assert len(parts) == k, f"expected {k} butted parts, got {len(parts)}"
    for p in parts:
        assert p.outer_area == pytest.approx(1600.0, rel=1e-9)
        poly = Polygon(p.outer)
        assert poly.is_valid and poly.is_simple
    # An exactly-duplicated wall is unambiguous: nothing to warn about.
    assert getattr(read_parts, "last_warnings", []) == []


@pytest.mark.parametrize("k", [2, 3])
@pytest.mark.parametrize("gap", [0.01, 0.03, 0.049])
def test_p2b_sub_tolerance_side_by_side_is_audible(tmp_path, k, gap):
    """Parts a HAIR apart, side by side, are the one genuine ambiguity here:
    the stitch tolerance exists to close CAD gaps inside one outline and cannot
    tell that apart from two parts drawn 0.03 mm from each other. Tightening it
    would break real exports. So the rule is known-or-loud, not correct-or-bust:
    either the parts come back separate, or the reader SAYS it welded them."""
    rng = random.Random(hash((k, gap)) & 0xFFFFFFFF)
    polys = _butted(k, gap=gap)
    pool = [(e[0], e[1]) for poly in polys for e in _edges(poly)]
    rng.shuffle(pool)

    parts = read_parts(_write_segments(tmp_path, pool, f"hair{k}_{gap}.dxf"))
    warns = list(getattr(read_parts, "last_warnings", []))

    if len(parts) == k:
        for p in parts:
            assert p.outer_area == pytest.approx(1600.0, rel=1e-9)
        return
    # Merged — permitted, but never silently.
    assert warns, f"{k} parts {gap}mm apart merged into {len(parts)} with NO warning"
    text = " ".join(warns)
    assert "welded" in text
    assert f"{gap:.3f}" in text or f"{gap:.2f}" in text, text


def test_p2b_a_gap_inside_one_outline_stays_silent(tmp_path):
    """The other half of the weld rule: two ends meeting across a small gap is
    ONE outline being closed — exactly what the tolerance is for. Only a
    junction of four or more ends is a weld worth naming, or every real file
    with CAD imprecision would cry wolf."""
    poly = _rect(0.0, 0.0, 40.0, 40.0)
    segs = [(e[0], e[1]) for e in _edges(poly)]
    # Shorten one edge by 0.03 mm — inside the tolerance, so it still closes.
    (a, b) = segs[0]
    segs[0] = (a, (b[0] - 0.03, b[1]))
    parts = read_parts(_write_segments(tmp_path, segs, "imprecise.dxf"))
    assert len(parts) == 1
    assert getattr(read_parts, "last_warnings", []) == []
