"""Minimal IGES reader: extract a straight tube's cut length from geometry.

Strategy (validated against real exports, extended as needed):

  1. Parse the IGES section layout (S/G/D/P/T) from the column-73 tag.
  2. Read the Global section to learn the parameter/record delimiters + units.
  3. Walk the Directory Entry section to map each entity to its Parameter Data.
  4. Pull 3D coordinates from every point-bearing entity we recognize.
  5. The cut length of a STRAIGHT tube is the longest axis of the point cloud's
     bounding box; the two shorter axes are its cross-section (handy for sanity
     checks even though grouping is filename-driven).

This deliberately handles the common entity types first. If a file yields no
points, it raises with the entity types it *did* see so we can add a handler —
send the sample and we extend, rather than silently returning a wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

Point = Tuple[float, float, float]

# IGES 5.3 unit flag (Global parameter 14) -> millimetres per unit.
# Flag 3 is deliberately absent: it means "the unit is named as a string in
# Global parameter 15", so there is no factor to tabulate — see _UNIT_NAME_TO_MM
# and _unit_scale below.
_UNIT_TO_MM = {
    1: 25.4,        # inches
    2: 1.0,         # millimetres
    4: 304.8,       # feet
    5: 1609344.0,   # miles
    6: 1000.0,      # metres
    7: 1_000_000.0,  # kilometres
    8: 0.0254,      # mils (0.001 in)
    9: 0.001,       # microns
    10: 10.0,       # centimetres
    11: 0.0000254,  # microinches
}

# Flag 3 = "units named in Global parameter 15". The spec fixes the spelling of
# the names for the tabulated units, so we resolve the name against exactly the
# same factors; a name we don't know raises rather than guessing millimetres.
_UNIT_NAME_TO_MM = {
    "IN": 25.4, "INCH": 25.4, "INCHES": 25.4,
    "MM": 1.0, "MILLIMETER": 1.0, "MILLIMETRE": 1.0,
    "FT": 304.8, "FEET": 304.8, "FOOT": 304.8,
    "MI": 1609344.0, "MILE": 1609344.0, "MILES": 1609344.0,
    "M": 1000.0, "METER": 1000.0, "METRE": 1000.0,
    "KM": 1_000_000.0, "KILOMETER": 1_000_000.0, "KILOMETRE": 1_000_000.0,
    "MIL": 0.0254, "MILS": 0.0254,
    "UM": 0.001, "MICRON": 0.001, "MICRONS": 0.001,
    "CM": 10.0, "CENTIMETER": 10.0, "CENTIMETRE": 10.0,
    "UIN": 0.0000254, "MICROINCH": 0.0000254,
}


# --------------------------------------------------------------------------- #
# Entity classification (E25)
#
# The reader pulls coordinates from the handled types below. Everything else it
# sees must be accounted for EXPLICITLY, because an unrecognized entity that
# carries geometry is geometry silently dropped from the bounding box — i.e. a
# cut length that is wrong while looking perfectly plausible.
#
# Three classes:
#   _HANDLED         — we read their coordinates.
#   _NO_OWN_GEOMETRY — structure / topology / annotation / property entities and
#                      analytic surfaces defined by REFERENCE to entities we do
#                      read. Their Parameter Data holds pointers, flags or
#                      colours, never model-space coordinates that could lie
#                      outside what the referenced geometry already spans. Safe
#                      to ignore, and silent on purpose: every real Fusion 360
#                      B-rep export is full of them, so warning about them would
#                      drown the signal.
#   everything else  — surfaced to the caller BY NAME as a note, and, when the
#                      type's Parameter Data is literally a coordinate list we
#                      cannot read (_LOST_GEOMETRY), an outright refusal.
# --------------------------------------------------------------------------- #

_HANDLED = {100, 110, 116, 126, 502}

_NO_OWN_GEOMETRY = {
    102,  # composite curve — pointers to its constituent curves
    108,  # plane — unbounded, defined by coefficients + a bounding curve pointer
    118,  # ruled surface — pointers to two curves
    120,  # surface of revolution — pointers to an axis + generatrix
    122,  # tabulated cylinder — pointer to a generatrix
    123,  # direction — a unit vector, not a location
    124,  # transformation matrix (NOT applied — pre-existing limitation, E2 scope)
    128,  # rational B-spline surface — trimmed by curves we read
    141, 142, 143, 144,  # boundary / curve on surface / bounded / trimmed surface
    186,  # manifold solid B-rep object — pointers to shells
    190, 192, 194, 196, 198,  # analytic surfaces (plane, cylinder, cone, sphere, torus)
    202, 206, 208, 210, 212, 214, 216, 218, 220, 222, 228,  # annotation/dimensions
    308, 314, 320,  # subfigure definition, colour, network subfigure
    402, 404, 406, 408, 410, 412, 414, 416, 418, 420, 422,  # associativity/property
    504, 508, 510, 514,  # edge list, loop, face, shell (B-rep topology)
}

# Their Parameter Data IS raw model-space coordinate data, and we cannot read
# it — so their presence is direct evidence that geometry was lost from the
# bounding box. Refuse, naming the types, rather than report a short length.
_LOST_GEOMETRY = {
    104,  # conic arc
    106,  # copious data (point/line strings) — very common, entirely coordinates
    112,  # parametric spline curve
    114,  # parametric spline surface
}


class IgesParseError(ValueError):
    pass


@dataclass
class TubeGeometry:
    cut_length: float            # longest bbox extent, in mm
    cross_section: Tuple[float, float]  # two shorter extents, in mm (descending)
    unit_flag: int
    point_count: int
    entity_types_seen: List[int]
    # Types present in the file that the reader neither read nor recognizes as
    # geometry-free (E25). Never silent: the CLI/service turn this into a note
    # naming the types. Empty for every export the reader is validated against.
    unhandled_types: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def read_tube(path: str) -> TubeGeometry:
    # IGES is a fixed-column BYTE format (80 bytes/line, section tag at byte 73).
    # Read as latin-1 so 1 byte == 1 char — UTF-8 would collapse multibyte chars
    # (e.g. 'ñ' in a filename/product-id field) and shift every column on that
    # line, breaking section detection and the Global delimiter parse.
    with open(path, "r", encoding="latin-1") as fh:
        lines = [ln.rstrip("\n") for ln in fh]
    return _parse(lines, path)


# --------------------------------------------------------------------------- #
# Section parsing
# --------------------------------------------------------------------------- #

def _parse(lines: List[str], path: str) -> TubeGeometry:
    sections: Dict[str, List[str]] = {"S": [], "G": [], "D": [], "P": [], "T": []}
    for ln in lines:
        if len(ln) < 73:
            continue
        tag = ln[72]
        if tag in sections:
            sections[tag].append(ln)

    if not sections["D"] or not sections["P"]:
        raise IgesParseError(f"{path}: no Directory/Parameter sections — not a valid IGES file?")

    pdelim, rdelim, unit_flag, unit_name = _parse_global(sections["G"], path)
    scale = _unit_scale(unit_flag, unit_name, path)
    pd_by_seq = _index_parameter_data(sections["P"])
    entities = _parse_directory(sections["D"])

    points: List[Point] = []
    types_seen: List[int] = []
    for ent in entities:
        types_seen.append(ent["type"])
        record = _collect_record(pd_by_seq, ent["p_start"], ent["p_count"], pdelim, rdelim)
        if record is None:
            continue
        points.extend(_extract_points(ent["type"], record))

    seen = sorted(set(types_seen))
    # E25: account for EVERY type the file contains. A type that is neither read
    # nor known to be geometry-free is either an outright refusal (its Parameter
    # Data is coordinates we cannot read — geometry demonstrably lost) or, at the
    # very least, a note naming it. It is never silent.
    lost = sorted(t for t in seen if t in _LOST_GEOMETRY)
    if lost:
        raise IgesParseError(
            f"{path}: entity type(s) {lost} carry model coordinates this reader "
            f"cannot read, so the bounding box would MISS that geometry and the "
            f"cut length would be short. Refusing rather than reporting a wrong "
            f"length — run /add-parser-support with this file. "
            f"Entity types present: {seen}."
        )
    unhandled = sorted(t for t in seen if t not in _HANDLED and t not in _NO_OWN_GEOMETRY)
    notes: List[str] = []
    if unhandled:
        notes.append(
            f"{path}: entity type(s) {unhandled} are not read by the IGES parser; "
            f"the reported length covers only the geometry that could be read. "
            f"Check it against the part before cutting (/add-parser-support)."
        )

    if not points:
        raise IgesParseError(
            f"{path}: recognized no point-bearing geometry. "
            f"Entity types present: {seen}. "
            f"Send this sample so we can add a handler for its entities."
        )

    extents = _bbox_extents(points)
    extents = [e * scale for e in extents]
    extents.sort(reverse=True)
    if extents[0] <= 0:
        raise IgesParseError(
            f"{path}: geometry extracted to zero length ({len(points)} point(s), "
            f"entity types {seen}) — nothing to cut. The file is degenerate or "
            f"its geometry is in entities this reader cannot see."
        )
    return TubeGeometry(
        cut_length=extents[0],
        cross_section=(extents[1], extents[2]),
        unit_flag=unit_flag,
        point_count=len(points),
        entity_types_seen=seen,
        unhandled_types=unhandled,
        notes=notes,
    )


def _unit_scale(unit_flag: int, unit_name: str | None, path: str) -> float:
    """Millimetres per model unit — or a refusal naming the flag.

    An unmapped flag NEVER silently means millimetres: getting this wrong scales
    every cut on the job (flag 1 vs 2 is 25.4x), and the wrong plan looks
    perfectly plausible. Flag 3 means "the unit is named in Global parameter
    15"; we resolve that name, and refuse a name we don't know.
    """
    if unit_flag == 3:
        name = (unit_name or "").strip().upper()
        if name in _UNIT_NAME_TO_MM:
            return _UNIT_NAME_TO_MM[name]
        raise IgesParseError(
            f"{path}: IGES unit flag 3 names its unit in Global parameter 15, and "
            f"that name is {name!r} — not a unit this reader knows. Refusing "
            f"rather than assuming millimetres."
        )
    if unit_flag in _UNIT_TO_MM:
        return _UNIT_TO_MM[unit_flag]
    raise IgesParseError(
        f"{path}: unknown IGES unit flag {unit_flag} (Global parameter 14). "
        f"Known flags are {sorted(_UNIT_TO_MM) + [3]}. Refusing rather than "
        f"assuming millimetres — the wrong unit scales every cut on the job."
    )


def _hollerith_at(text: str, i: int) -> Tuple[str, int] | None:
    """If a Hollerith constant starts at ``text[i]`` (``nH`` + n BYTES), return
    (its content, the index just past it). Otherwise None.

    IGES strings are counted, not quoted: ``19HACME, S.A. de C.V.`` contains
    commas that are NOT field separators. Splitting on the delimiter without
    this shifts every later field — which is how a valid file used to be read as
    inches instead of millimetres.
    """
    j = i
    while j < len(text) and text[j] == " ":
        j += 1
    k = j
    while k < len(text) and text[k].isdigit():
        k += 1
    if k == j or k >= len(text) or text[k] not in "Hh":
        return None
    n = int(text[j:k])
    start = k + 1
    return text[start:start + n], start + n


def _split_global(text: str, pdelim: str, rdelim: str) -> List[str]:
    """Split the Global section into fields, Hollerith-aware.

    A Hollerith field yields its CONTENT (delimiters inside it included, and
    never treated as separators); any other field yields its raw text.
    """
    fields: List[str] = []
    buf = ""
    i = 0
    n = len(text)
    while i < n:
        h = _hollerith_at(text, i)
        if h is not None and not buf.strip():
            content, nxt = h
            buf = content
            i = nxt
            continue
        c = text[i]
        if c == pdelim:
            fields.append(buf)
            buf = ""
            i += 1
            continue
        if c == rdelim:
            fields.append(buf)
            return fields
        buf += c
        i += 1
    fields.append(buf)
    return fields


def _parse_global(glines: List[str], path: str) -> Tuple[str, str, int, str | None]:
    """Return (param_delim, record_delim, unit_flag, unit_name).

    The unit flag is NEVER defaulted: a Global section that doesn't state one
    raises, because silently choosing millimetres is exactly the failure this
    reader exists to prevent.
    """
    text = "".join(ln[:72] for ln in glines)
    pdelim, rdelim = ",", ";"

    # Parameters 1/2 optionally redefine the delimiters, each as a single-char
    # Hollerith ("1H:"). A null field means the default. The delimiters have to
    # be read before the section can be split at all, so they are read by hand.
    i = 0
    h = _hollerith_at(text, i)
    if h is not None and len(h[0]) == 1:
        pdelim, i = h[0], h[1]
    if i < len(text) and text[i] == pdelim:
        i += 1
    h = _hollerith_at(text, i)
    if h is not None and len(h[0]) == 1:
        rdelim, i = h[0], h[1]
    if i < len(text) and text[i] == pdelim:
        i += 1

    fields = ["", ""] + _split_global(text[i:], pdelim, rdelim)

    unit_flag_raw = fields[13].strip() if len(fields) >= 14 else ""
    unit_name = fields[14] if len(fields) >= 15 else None
    if not unit_flag_raw:
        raise IgesParseError(
            f"{path}: the IGES Global section states no unit flag (parameter 14). "
            f"Refusing rather than assuming millimetres."
        )
    try:
        unit_flag = int(unit_flag_raw)
    except ValueError:
        raise IgesParseError(
            f"{path}: IGES Global parameter 14 (unit flag) is {unit_flag_raw!r}, "
            f"not an integer — the Global section could not be read reliably, so "
            f"the unit is unknown. Refusing rather than assuming millimetres."
        )
    return pdelim, rdelim, unit_flag, unit_name


def _index_parameter_data(plines: List[str]) -> Dict[int, str]:
    """Map each PD line's sequence number (cols 74-80) -> its data (cols 1-64)."""
    out: Dict[int, str] = {}
    for ln in plines:
        seq_txt = ln[73:80].strip()
        if not seq_txt.isdigit():
            continue
        out[int(seq_txt)] = ln[:64]
    return out


def _parse_directory(dlines: List[str]) -> List[dict]:
    """Each DE entity spans two 80-char lines of eight 8-char fields."""
    entities: List[dict] = []
    for i in range(0, len(dlines) - 1, 2):
        l1, l2 = dlines[i], dlines[i + 1]
        try:
            etype = int(l1[0:8].strip() or "0")
            p_start = int(l1[8:16].strip() or "0")
            p_count = int(l2[24:32].strip() or "0")
        except ValueError:
            continue
        if etype and p_start:
            entities.append({"type": etype, "p_start": p_start, "p_count": p_count})
    return entities


def _collect_record(
    pd_by_seq: Dict[int, str], p_start: int, p_count: int, pdelim: str, rdelim: str
) -> List[str] | None:
    """Concatenate an entity's PD lines and split into parameter tokens."""
    chunks: List[str] = []
    seq = p_start
    count = p_count if p_count > 0 else 1
    for _ in range(count):
        data = pd_by_seq.get(seq)
        if data is None:
            break
        chunks.append(data)
        seq += 1
    if not chunks:
        return None
    blob = "".join(chunks)
    blob = blob.split(rdelim, 1)[0]  # stop at record delimiter
    return [t.strip() for t in blob.split(pdelim)]


# --------------------------------------------------------------------------- #
# Per-entity coordinate extraction
# --------------------------------------------------------------------------- #

def _f(tokens: List[str], i: int) -> float:
    return float(tokens[i].replace("D", "E").replace("d", "e"))


def _extract_points(etype: int, t: List[str]) -> List[Point]:
    """t[0] is the repeated entity type number; data starts at t[1]."""
    try:
        if etype == 110 and len(t) >= 7:           # Line
            return [(_f(t, 1), _f(t, 2), _f(t, 3)), (_f(t, 4), _f(t, 5), _f(t, 6))]
        if etype == 116 and len(t) >= 4:           # Point
            return [(_f(t, 1), _f(t, 2), _f(t, 3))]
        if etype == 100 and len(t) >= 8:           # Circular arc (plane z=ZT)
            z = _f(t, 1)
            return [
                (_f(t, 2), _f(t, 3), z),  # center
                (_f(t, 4), _f(t, 5), z),  # start
                (_f(t, 6), _f(t, 7), z),  # end
            ]
        if etype == 126:                            # Rational B-Spline curve
            return _bspline_curve_points(t)
        if etype == 502:                            # Vertex list (B-rep)
            return _vertex_list_points(t)
    except (ValueError, IndexError):
        return []
    return []


def _bspline_curve_points(t: List[str]) -> List[Point]:
    """Type 126: K,M,PROP1..4, knots[A+1], weights[K+1], ctrlpts[(K+1)*3], ...
    where A = K + M + 1 -> knot count = A + 1 = K + M + 2."""
    K = int(float(t[1]))
    M = int(float(t[2]))
    n_ctrl = K + 1
    n_knots = K + M + 2
    base = 7 + n_knots + n_ctrl  # skip K,M,4 props (idx1..6), knots, weights
    pts: List[Point] = []
    for c in range(n_ctrl):
        i = base + c * 3
        pts.append((_f(t, i), _f(t, i + 1), _f(t, i + 2)))
    return pts


def _vertex_list_points(t: List[str]) -> List[Point]:
    """Type 502: N, then N (x,y,z) triples."""
    n = int(float(t[1]))
    pts: List[Point] = []
    for v in range(n):
        i = 2 + v * 3
        pts.append((_f(t, i), _f(t, i + 1), _f(t, i + 2)))
    return pts


def check_straight(geo: TubeGeometry, profile_dims: Tuple[float, ...] | None, path: str) -> None:
    """Raise if the measured cross-section can't plausibly be a straight tube
    of the nominal profile (E2).

    ``read_tube`` takes the bbox's longest axis as cut length, which is only
    correct for a STRAIGHT tube — for a BENT one it silently returns the
    chord. The two shorter bbox extents are the (measured) cross-section; a
    straight tube's should match the nominal profile within modeling noise. A
    bent 40x40 tube's minor extent balloons to hundreds of mm (the bend's
    sweep), so the tolerance only needs to absorb noise, not real geometry.

    ``profile_dims`` comes from ``profile.parse_profile_dims``: a single
    diameter for round profiles (compared against both measured extents), or
    ``(width, height)`` for square/rect. ``None`` means the filename profile
    didn't parse into dims — skip the check (that's gap E5, out of scope).
    """
    if profile_dims is None:
        return
    measured = sorted(geo.cross_section, reverse=True)
    nominal = [profile_dims[0], profile_dims[0]] if len(profile_dims) == 1 else sorted(profile_dims, reverse=True)
    for m, n in zip(measured, nominal):
        tol = max(0.10 * n, 2.0)
        if m > n + tol:
            raise ValueError(
                f"{path}: measured cross-section "
                f"{tuple(round(x, 1) for x in geo.cross_section)}mm exceeds the nominal "
                f"profile dimensions {tuple(nominal)}mm (tolerance {tol:.1f}mm). "
                f"This part appears bent/non-straight and cannot be nested as a straight tube."
            )


def _bbox_extents(points: List[Point]) -> List[float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
