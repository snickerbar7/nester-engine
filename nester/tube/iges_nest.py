"""Emit the nesting result as an IGES file you can open in CAD.

Each stock bar is drawn as a 3D rectangular-tube box (the bar's real
cross-section) running along X. Every cut piece is its own box, colored by
length (IGES color numbers), with the saw kerf as a small gap between pieces; the
leftover drop shows as the empty tail of the bar outline. Bars stack along Y,
profiles separated by a larger gap.

This is wireframe geometry (IGES Line entities, type 110) — enough to see the
whole nest laid out and verify it against the CAD model.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .model import ProfileResult

Pt = Tuple[float, float, float]

# IGES color numbers: 2 red, 3 green, 4 blue, 5 yellow, 6 magenta, 7 cyan.
_COLORS = [4, 2, 3, 6, 5, 7]


def write_nest_iges(
    results: List[ProfileResult],
    path: str,
    cross_sections: Dict[str, Tuple[float, float]],
) -> None:
    w = _IgesWriter()
    y_cursor = 0.0
    for r in results:
        W, H = cross_sections.get(r.profile, (50.0, 50.0))
        gap = max(30.0, W * 0.6)
        cmap = _length_colors(r)
        for bar in r.bars:
            yc = y_cursor
            # full bar outline (default color) — shows total length + drop
            w.box(0.0, r.spec.stock_length, yc, yc + W, 0.0, H, color=0)
            x0 = r.spec.front_trim
            for p in bar.placements:
                a = x0 + p.start
                b = x0 + p.end
                w.box(a, b, yc, yc + W, 0.0, H, color=cmap[round(p.part.length, 2)])
            y_cursor += W + gap
        y_cursor += gap  # extra space between profiles
    with open(path, "w", encoding="ascii", errors="replace") as fh:
        fh.write(w.dumps())


def _length_colors(r: ProfileResult) -> Dict[float, int]:
    lengths = sorted(
        {round(p.part.length, 2) for b in r.bars for p in b.placements}, reverse=True
    )
    return {ln: _COLORS[i % len(_COLORS)] for i, ln in enumerate(lengths)}


# --------------------------------------------------------------------------- #
# Minimal IGES wireframe writer
# --------------------------------------------------------------------------- #

class _IgesWriter:
    def __init__(self) -> None:
        self._lines: List[Tuple[Pt, Pt, int]] = []

    def line(self, a: Pt, b: Pt, color: int = 0) -> None:
        self._lines.append((a, b, color))

    def box(self, x0, x1, y0, y1, z0, z1, color=0) -> None:
        c = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),  # bottom
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),  # top
        ]
        edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),  # bottom loop
            (4, 5), (5, 6), (6, 7), (7, 4),  # top loop
            (0, 4), (1, 5), (2, 6), (3, 7),  # verticals
        ]
        for i, j in edges:
            self.line(c[i], c[j], color)

    def dumps(self) -> str:
        out: List[str] = []
        out.append(_rec("Nested tube layout - nester.tube", "S", 1))

        # Global: default delimiters, unit flag 2 = mm
        params = [""] * 24
        params[12] = "1.0"     # model space scale
        params[13] = "2"       # unit flag (mm)
        params[14] = "2HMM"    # unit name
        gstr = ",".join(params) + ";"
        gseq = 0
        for i in range(0, len(gstr), 72):
            gseq += 1
            out.append(_rec(gstr[i:i + 72], "G", gseq))

        n = len(self._lines)
        # Directory Entry: 2 lines per entity. PD pointer = i+1 (1 PD line each).
        dseq = 0
        for i, (_a, _b, color) in enumerate(self._lines):
            p_ptr = i + 1
            de1 = "".join(f"{v:>8}" for v in
                          [110, p_ptr, 0, 0, 0, 0, 0, 0]) + "00000000"
            de2 = "".join(f"{v:>8}" for v in
                          [110, 0, color, 1, 0, 0, 0]) + f"{'':>8}{0:>8}"
            dseq += 1
            out.append(_rec(de1[:72], "D", dseq))
            dseq += 1
            out.append(_rec(de2[:72], "D", dseq))

        # Parameter Data: one line per entity.
        for i, (a, b, _color) in enumerate(self._lines):
            data = (f"110,{_f(a[0])},{_f(a[1])},{_f(a[2])},"
                    f"{_f(b[0])},{_f(b[1])},{_f(b[2])};")
            out.append(_pd(data, de_ptr=2 * i + 1, seq=i + 1))

        # Terminate: counts of S, G, D, P records.
        term = f"{'S':>1}{1:>7}{'G':>1}{gseq:>7}{'D':>1}{2 * n:>7}{'P':>1}{n:>7}"
        out.append(_rec(term, "T", 1))
        return "".join(out)


def _f(v: float) -> str:
    """Compact IGES float: '6000.' not '6000.0', '50.8' as-is."""
    v = round(v, 3)
    if v == int(v):
        return f"{int(v)}."
    return f"{v:g}"


def _rec(data: str, section: str, seq: int) -> str:
    return f"{data:<72}{section}{seq:>7}\n"


def _pd(data: str, de_ptr: int, seq: int) -> str:
    # data 1-64, col 65 space + DE back-pointer 66-72, 'P', seq 74-80
    if len(data) > 64:
        data = data[:64]
    return f"{data:<64}{de_ptr:>8}P{seq:>7}\n"
