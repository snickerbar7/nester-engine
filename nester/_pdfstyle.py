"""Print design tokens + the low-level drawing primitives the PDFs share.

Both cut plans (1D tubes, 2D sheet) are drawn to the same Harriet Nester
artboards: white paper, graphite ink, IBM Plex approximated with the reportlab
base-14 fonts — Helvetica for prose, Courier for every figure, ID and kicker.
This module is that vocabulary in one place: the ``print/*`` colour tokens, the
design-pixel unit, the piece palette, and eight stateless canvas helpers.

Nothing here knows what a bar or a sheet is. ``nester.tube.report`` still
carries its own (identical) copy of these; it is deliberately untouched, and can
migrate onto this module whenever it is next opened.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

RGB = Tuple[float, float, float]

# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

PX = 0.75                        # design pixel (96 dpi) -> PDF point


def px(v: float) -> float:
    """Design pixels -> PDF points."""
    return v * PX


# --------------------------------------------------------------------------- #
# print/* colour tokens (design-system README, "Color — documento impreso")
# --------------------------------------------------------------------------- #

PAPER: RGB = (1.0, 1.0, 1.0)
INK: RGB = (0.067, 0.075, 0.071)      # #111312
MID: RGB = (0.239, 0.251, 0.231)      # #3d403b
SOFT: RGB = (0.361, 0.373, 0.357)     # #5c5f5b
FAINT: RGB = (0.545, 0.557, 0.533)    # #8b8e88
RULE: RGB = (0.788, 0.800, 0.773)     # #c9ccc5
HAIR: RGB = (0.886, 0.894, 0.875)     # #e2e4df
HAIR2: RGB = (0.925, 0.933, 0.914)    # #eceee9
PANEL: RGB = (0.949, 0.953, 0.941)    # #f2f3f0
ZEBRA: RGB = (0.980, 0.984, 0.973)    # #fafbf8
DROP_BG: RGB = (0.984, 0.988, 0.980)  # #fbfcfa
ACC_BAR: RGB = (0.663, 0.486, 0.071)  # #a97c12
ACC_TXT: RGB = (0.478, 0.329, 0.020)  # #7a5405
ACC_BRD: RGB = (0.784, 0.682, 0.431)  # #c8ae6e
ACC_BG: RGB = (0.984, 0.980, 0.961)   # #fbfaf5
DASH_RULE: RGB = (0.725, 0.737, 0.710)  # #b9bcb5 — margin / dead-zone dashes

SANS, SANS_B = "Helvetica", "Helvetica-Bold"
MONO, MONO_B = "Courier", "Courier-Bold"

# Piece colours — the design's PRINT scale (oklch converted to sRGB), extended
# around the hue circle so a job with many distinct parts keeps them apart. They
# are darker than the on-screen scale so white text on top stays legible.
PALETTE: List[RGB] = [
    (0.119, 0.395, 0.668),   # oklch(.50 .13 252)
    (0.000, 0.524, 0.547),   # oklch(.56 .10 200)
    (0.135, 0.486, 0.269),   # oklch(.52 .12 152)
    (0.571, 0.469, 0.102),   # oklch(.58 .11  92)
    (0.778, 0.365, 0.149),   # oklch(.60 .15  45)
    (0.570, 0.288, 0.549),   # oklch(.52 .13 330)
    (0.408, 0.409, 0.689),   # oklch(.55 .11 282)
    (0.082, 0.530, 0.448),   # oklch(.56 .10 176)
    (0.389, 0.489, 0.130),   # oklch(.55 .12 124)
    (0.673, 0.411, 0.000),   # oklch(.58 .13  68)
    (0.000, 0.446, 0.541),   # oklch(.50 .11 215)
    (0.663, 0.306, 0.318),   # oklch(.54 .12  20)
]


# --------------------------------------------------------------------------- #
# Canvas primitives
# --------------------------------------------------------------------------- #

def tw(c, s: str, font: str, size: float, track: float = 0.0) -> float:
    """Width of ``s`` including letter-spacing."""
    return c.stringWidth(s, font, size) + track * max(0, len(str(s)) - 1)


def txt(c, x: float, y: float, s, font: str, size: float, color: RGB = INK,
        track: float = 0.0, align: str = "l") -> float:
    """Draw text; return its width. ``align`` is l/r/c, ``track`` letter-spacing.

    Always goes through a text object: letter-spacing (Tc) is part of the
    persistent text state, so a tracked run would otherwise leak its spacing
    into every later drawString on the page.
    """
    if s is None:
        return 0.0
    s = str(s)
    w = tw(c, s, font, size, track)
    if align == "r":
        x -= w
    elif align == "c":
        x -= w / 2
    to = c.beginText(x, y)
    to.setFont(font, size)
    to.setFillColorRGB(*color)
    to.setCharSpace(track)
    to.textOut(s)
    c.drawText(to)
    return w


def kicker(c, x: float, y: float, s: str, color: RGB = SOFT,
           size: float = px(9)) -> float:
    """A section kicker: Mono bold, uppercase, wide tracking."""
    return txt(c, x, y, s, MONO_B, size, color, track=size * 0.14)


def rect(c, x: float, y: float, w: float, h: float, fill: RGB | None = None,
         stroke: RGB | None = None, lw: float = px(1), dash=None) -> None:
    if w <= 0 or h <= 0:
        return
    if dash:
        c.setDash(*dash)
    c.setLineWidth(lw)
    if fill:
        c.setFillColorRGB(*fill)
    if stroke:
        c.setStrokeColorRGB(*stroke)
    c.rect(x, y, w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)
    if dash:
        c.setDash()


def line(c, x1: float, y1: float, x2: float, y2: float, color: RGB = RULE,
         lw: float = px(1), dash=None) -> None:
    if dash:
        c.setDash(*dash)
    c.setStrokeColorRGB(*color)
    c.setLineWidth(lw)
    c.line(x1, y1, x2, y2)
    if dash:
        c.setDash()


def ellipsize(c, s: str, font: str, size: float, maxw: float) -> str:
    if tw(c, s, font, size) <= maxw:
        return s
    while s and tw(c, s + "…", font, size) > maxw:
        s = s[:-1]
    return s + "…"


def wrap(c, s: str, font: str, size: float, maxw: float) -> List[str]:
    out: List[str] = []
    line_ = ""
    for word in str(s).split():
        trial = f"{line_} {word}" if line_ else word
        if line_ and tw(c, trial, font, size) > maxw:
            out.append(line_)
            line_ = word
        else:
            line_ = trial
    if line_:
        out.append(line_)
    return out


def signature_block(c, L_labels: Sequence[str], x: float, y: float, w: float,
                    col_gap: float = px(14), row_h: float = px(29)) -> float:
    """Four sign-off rules in a 2-column grid, ``y`` = top of the first rule."""
    colw = (w - col_gap) / 2
    for i, lbl in enumerate(L_labels):
        cx = x + (i % 2) * (colw + col_gap)
        cy = y - (i // 2) * row_h
        line(c, cx, cy, cx + colw, cy, INK)
        txt(c, cx, cy - px(9), lbl, MONO, px(8.5), SOFT, track=px(8.5) * 0.08)
    return y - ((len(L_labels) + 1) // 2) * row_h
