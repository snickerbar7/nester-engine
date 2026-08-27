"""Generate minimal synthetic straight-tube IGES files for testing the parser.

Each file holds one or more Line entities (type 110). Not a real CAD export —
just enough valid IGES to exercise the reader until we have the user's actual
files. Usage:

    python tools/make_sample_iges.py 40x40x2 1200 samples/bracket_40x40x2_A.igs
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float, float]
Segment = Tuple[Point, Point]


def _line(data: str, section: str, seq: int) -> str:
    """One 80-char IGES record: data in 1-72, section letter at 73, seq in 74-80."""
    return f"{data:<72}{section}{seq:>7}\n"


def _pd_line(data: str, de_ptr: int, seq: int) -> str:
    """Parameter-Data record: data 1-64, col 65 space, DE back-pointer 66-72, 'P', seq 74-80."""
    return f"{data:<64}{de_ptr:>8}P{seq:>7}\n"


def build(length_mm: float) -> str:
    """A single straight Line entity along X, from (0,0,0) to (length,0,0)."""
    return build_lines([((0.0, 0.0, 0.0), (length_mm, 0.0, 0.0))])


def build_lines(
    segments: Sequence[Segment],
    unit_flag: int | str = 2,
    unit_name: str = "MM",
    product_id: str | None = None,
    extra_entities: Sequence[Tuple[int, str]] = (),
    global_strings: Optional[Dict[int, str]] = None,
) -> str:
    """One Line entity (type 110) per (start, end) segment. Concatenated
    segments (e.g. a straight run + a swung-out arm) let a test simulate a
    BENT tube: the reader's bbox-longest-axis-as-length logic can't tell that
    apart from a straight one by length alone, but the minor bbox extents
    balloon — which is exactly what the E2 straightness check watches for.

    ``unit_flag`` / ``unit_name`` write Global parameters 14/15 verbatim (pass a
    string to write a malformed flag on purpose). ``product_id`` writes Global
    parameter 3 as a proper Hollerith constant — pass something containing the
    delimiter (``"ACME, S.A. de C.V."``) to exercise the Hollerith-aware Global
    parser. ``extra_entities`` is [(entity_type, parameter_data_without_the
    trailing delimiter)], appended after the Lines, so a test can put geometry
    in an entity type the reader does not handle.
    """
    out: List[str] = []

    # Start
    out.append(_line("Synthetic tube sample.", "S", 1))

    # Global: defaults for delimiters (leading ',,'), unit flag 2 = mm (param 14)
    params = [""] * 24
    if product_id is not None:
        params[2] = f"{len(product_id.encode('latin-1'))}H{product_id}"
    # Arbitrary Hollerith text in any 1-based Global parameter slot: the byte
    # count is what makes a string field parseable, so it is computed here.
    for idx, text in (global_strings or {}).items():
        params[idx - 1] = f"{len(text.encode('latin-1'))}H{text}"
    params[12] = "1.0"     # param 13: model space scale
    params[13] = str(unit_flag)   # param 14: unit flag
    params[14] = f"{len(unit_name)}H{unit_name}" if unit_name else ""
    gstr = ",".join(params) + ";"
    gseq = 0
    for i in range(0, len(gstr), 72):
        gseq += 1
        out.append(_line(gstr[i:i + 72], "G", gseq))

    # Directory Entry + Parameter Data per entity, one PD line each.
    records = [(110, f"110,{x1:.12g},{y1:.12g},{z1:.12g},{x2:.12g},{y2:.12g},{z2:.12g}")
               for ((x1, y1, z1), (x2, y2, z2)) in segments]
    records += [(etype, pd) for etype, pd in extra_entities]
    n = len(records)
    for i, (etype, pd) in enumerate(records, start=1):
        p_start = i
        de1 = "".join(f"{v:>8}" for v in [etype, p_start, 0, 0, 0, 0, 0, 0]) + "00000000"
        de2 = "".join(f"{v:>8}" for v in [etype, 0, 0, 1, 0, 0, 0]) + f"{'':>8}{0:>8}"
        out.append(_line(de1[:72], "D", 2 * i - 1))
        out.append(_line(de2[:72], "D", 2 * i))
        out.append(_pd_line(pd + ";", de_ptr=2 * i - 1, seq=i))

    # Terminate: counts of S, G, D, P lines
    term = f"{'S':>1}{1:>7}{'G':>1}{gseq:>7}{'D':>1}{2 * n:>7}{'P':>1}{n:>7}"
    out.append(_line(term, "T", 1))

    return "".join(out)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: make_sample_iges.py <profile> <length_mm> <out_path>", file=sys.stderr)
        return 2
    _profile, length, out_path = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    with open(out_path, "w") as fh:
        fh.write(build(length))
    print(f"wrote {out_path}  ({length:g}mm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
