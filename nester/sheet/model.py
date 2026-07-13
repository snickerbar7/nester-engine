"""Core data structures for flat-sheet nesting.

All coordinates are in millimetres (the DXF reader normalizes via the file's
INSUNITS flag). This module is deliberately dependency-free — plain geometry
math (shoelace area, bounding box) so it is trivial to unit-test, exactly like
``nester.tube.model``. Shapely/spyrrow live in the reader and packer, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Contour = Tuple[Point, ...]  # ordered ring of vertices (not necessarily closed)


def polygon_area(ring: Sequence[Point]) -> float:
    """Unsigned area of a simple polygon via the shoelace formula (mm²)."""
    n = len(ring)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def bbox(ring: Sequence[Point]) -> Tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of a point ring."""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class FlatPart:
    """A single flat part to cut: one outer boundary plus zero or more holes.

    ``qty`` is the number of identical copies to nest (passed straight through
    to the packer as demand). ``allowed_orientations`` is the set of rotation
    angles (degrees) the part may take — ``None`` means free rotation, an empty
    tuple or ``(0.0,)`` means fixed, ``(0.0, 180.0)`` models a grain constraint.
    It defaults to unset here and is resolved from job settings at pack time.
    """

    name: str
    outer: Contour
    holes: Tuple[Contour, ...] = ()
    qty: int = 1
    allowed_orientations: Optional[Tuple[float, ...]] = None

    def __post_init__(self) -> None:
        if len(self.outer) < 3:
            raise ValueError(f"{self.name}: outer boundary needs >= 3 points, got {len(self.outer)}")
        if self.qty < 1:
            raise ValueError(f"{self.name}: qty must be >= 1, got {self.qty}")

    @property
    def outer_area(self) -> float:
        return polygon_area(self.outer)

    @property
    def hole_area(self) -> float:
        return sum(polygon_area(h) for h in self.holes)

    @property
    def area(self) -> float:
        """Net material area of one copy (outer minus holes), mm²."""
        return max(self.outer_area - self.hole_area, 0.0)

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return bbox(self.outer)

    @property
    def size(self) -> Tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return x1 - x0, y1 - y0


@dataclass(frozen=True)
class SheetSpec:
    """One rectangular stock sheet definition + nesting allowances.

    ``margin`` is the unusable band along every edge (clamp / uneven edge);
    parts are nested inside the inset (usable) rectangle. ``part_gap`` is the
    minimum spacing between adjacent parts (kept >= kerf so cut lines never
    overlap). ``material``/``thickness`` define the stock group — parts of
    different material or thickness are never nested together.
    """

    width: float               # sheet extent in X (mm)
    height: float              # sheet extent in Y (mm)
    material: str = ""
    thickness: float = 0.0
    margin: float = 0.0        # edge margin, all four sides (mm)
    part_gap: float = 0.0      # minimum part-to-part spacing (mm)

    def __post_init__(self) -> None:
        if self.usable_width <= 0 or self.usable_height <= 0:
            raise ValueError(
                f"sheet usable area <= 0 (w={self.width}, h={self.height}, margin={self.margin})"
            )

    @property
    def usable_width(self) -> float:
        return self.width - 2 * self.margin

    @property
    def usable_height(self) -> float:
        return self.height - 2 * self.margin

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def key(self) -> str:
        """Stock group key: parts only nest together within one key."""
        mat = self.material or "?"
        return f"{mat}@{self.thickness:g}mm"


@dataclass
class Placement:
    """One placed copy of a part on a sheet.

    ``rotation`` is applied first (about the part's local origin, degrees CCW),
    then the part is translated by ``(x, y)`` — matching spyrrow's PlacedItem
    convention. ``x``/``y`` are absolute sheet coordinates (mm), including the
    sheet margin offset already added in.
    """

    part: FlatPart
    x: float
    y: float
    rotation: float = 0.0


@dataclass
class SheetLayout:
    """One physical stock sheet and everything nested on it."""

    index: int
    spec: SheetSpec
    placements: List[Placement] = field(default_factory=list)

    @property
    def used_area(self) -> float:
        """Net part area placed on this sheet (mm²)."""
        return sum(p.part.area for p in self.placements)

    @property
    def utilization(self) -> float:
        """Fraction of the full sheet turned into parts."""
        return self.used_area / self.spec.area if self.spec.area else 0.0

    @property
    def part_count(self) -> int:
        return len(self.placements)


@dataclass
class NestResult:
    """Nesting result for one stock group (single material/thickness)."""

    spec: SheetSpec
    sheets: List[SheetLayout] = field(default_factory=list)
    unplaceable: List[FlatPart] = field(default_factory=list)  # larger than a usable sheet

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def total_part_area(self) -> float:
        return sum(s.used_area for s in self.sheets)

    @property
    def total_sheet_area(self) -> float:
        return self.sheet_count * self.spec.area

    @property
    def yield_pct(self) -> float:
        if not self.total_sheet_area:
            return 0.0
        return 100.0 * self.total_part_area / self.total_sheet_area
