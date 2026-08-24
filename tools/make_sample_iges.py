"""Generate minimal synthetic straight-tube IGES files for testing the parser.

Each file holds one or more Line entities (type 110). Not a real CAD export —
just enough valid IGES to exercise the reader until we have the user's actual
files. Usage:

    python tools/make_sample_iges.py 40x40x2 1200 samples/bracket_40x40x2_A.igs
"""

from __future__ import annotations

import sys
from typing import List, Sequence, Tuple

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


def build_lines(segments: Sequence[Segment]) -> str:
    """One Line entity (type 110) per (start, end) segment. Concatenated
    segments (e.g. a straight run + a swung-out arm) let a test simulate a
    BENT tube: the reader's bbox-longest-axis-as-length logic can't tell that
    apart from a straight one by length alone, but the minor bbox extents
    balloon — which is exactly what the E2 straightness check watches for.
    """
    out: List[str] = []

    # Start
    out.append(_line("Synthetic tube sample.", "S", 1))

    # Global: defaults for delimiters (leading ',,'), unit flag 2 = mm (param 14)
    params = [""] * 24
    params[12] = "1.0"     # param 13: model space scale
    params[13] = "2"       # param 14: unit flag (mm)
    params[14] = "2HMM"    # param 15: units name
    gstr = ",".join(params) + ";"
    gseq = 0
    for i in range(0, len(gstr), 72):
        gseq += 1
        out.append(_line(gstr[i:i + 72], "G", gseq))

    # Directory Entry + Parameter Data for each Line (type 110), one PD line each
    n = len(segments)
    for i, ((x1, y1, z1), (x2, y2, z2)) in enumerate(segments, start=1):
        p_start = i
        de1 = "".join(f"{v:>8}" for v in [110, p_start, 0, 0, 0, 0, 0, 0]) + "00000000"
        de2 = "".join(f"{v:>8}" for v in [110, 0, 0, 1, 0, 0, 0]) + f"{'':>8}{0:>8}"
        out.append(_line(de1[:72], "D", 2 * i - 1))
        out.append(_line(de2[:72], "D", 2 * i))

        pd = f"110,{x1:g},{y1:g},{z1:g},{x2:g},{y2:g},{z2:g};"
        out.append(_pd_line(pd, de_ptr=2 * i - 1, seq=i))

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
