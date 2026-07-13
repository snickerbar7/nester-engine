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

from dataclasses import dataclass
from typing import Dict, List, Tuple

Point = Tuple[float, float, float]

# IGES unit flag (Global param 15) -> millimetres per unit.
_UNIT_TO_MM = {
    1: 25.4,    # inches
    2: 1.0,     # millimetres
    3: 1.0,     # (custom name) — assume mm
    4: 304.8,   # feet
    5: 0.0254 ** -1,  # placeholder, rarely used
    6: 1000.0,  # metres
    8: 1.0,     # micrometres? rarely used — assume mm
    10: 304800.0,  # miles, absurd but defined
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

    pdelim, rdelim, unit_flag = _parse_global(sections["G"])
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

    if not points:
        raise IgesParseError(
            f"{path}: recognized no point-bearing geometry. "
            f"Entity types present: {sorted(set(types_seen))}. "
            f"Send this sample so we can add a handler for its entities."
        )

    scale = _UNIT_TO_MM.get(unit_flag, 1.0)
    extents = _bbox_extents(points)
    extents = [e * scale for e in extents]
    extents.sort(reverse=True)
    return TubeGeometry(
        cut_length=extents[0],
        cross_section=(extents[1], extents[2]),
        unit_flag=unit_flag,
        point_count=len(points),
        entity_types_seen=sorted(set(types_seen)),
    )


def _parse_global(glines: List[str]) -> Tuple[str, str, int]:
    """Return (param_delim, record_delim, unit_flag). Defaults: ',' ';' mm."""
    text = "".join(ln[:72] for ln in glines)
    pdelim, rdelim = ",", ";"

    # Params 1/2 optionally redefine the delimiters. A null field (the section
    # begins with the default comma, i.e. ",,...") means defaults. Otherwise the
    # field is a Hollerith constant — and a delimiter is ALWAYS a single char, so
    # only "1H<x>" counts; any longer Hollerith (e.g. a product-id like
    # "7Hunknown") is not a delimiter and we keep the default.
    def holler1(token: str, default: str) -> str:
        token = token.strip()
        if token.startswith("1H") and len(token) >= 3:
            return token[2]
        return default

    if not text.startswith(","):
        head = text.split(",", 2)
        if len(head) >= 1:
            pdelim = holler1(head[0], ",")
        if len(head) >= 2:
            rdelim = holler1(head[1], ";")

    unit_flag = 2  # default mm
    fields = text.split(pdelim)
    if len(fields) >= 14:  # Global parameter 14 -> 0-based index 13
        try:
            unit_flag = int(fields[13].strip() or "2")
        except ValueError:
            unit_flag = 2
    return pdelim, rdelim, unit_flag


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


def _bbox_extents(points: List[Point]) -> List[float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
