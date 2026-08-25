"""Core data structures for flat-sheet nesting.

All coordinates are in millimetres (the DXF reader normalizes via the file's
INSUNITS flag). This module is deliberately dependency-free — plain geometry
math (shoelace area, bounding box) so it is trivial to unit-test, exactly like
``nester.tube.model``. Shapely/spyrrow live in the reader and packer, not here.

Three things beyond a plain nest live here because the shop asks for them by
name:

* **Retazos de lámina** (E16) — :class:`ExtraSheet` is a physical offcut on the
  rack, consumed at most once, and a :class:`SheetLayout` records which stock it
  was cut from via ``source``. Sheets in one job therefore need not be the same
  size, so every area total sums the layouts instead of multiplying a count.
* **Sobrante recuperable** (E16) — :class:`Leftover` is the rectangle a sheet
  still has left, so the drop stops being an anonymous number and becomes the
  next job's retazo.
* **Piezas en barrenos** (E15) — a :class:`Placement` cut out of another part's
  hole carries ``in_hole_of``, because the operator has to know that slug is not
  scrap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..materials import weight_kg

Point = Tuple[float, float]
Contour = Tuple[Point, ...]  # ordered ring of vertices (not necessarily closed)

#: ``SheetLayout.source`` for a full sheet the shop has to buy.
NEW_SHEET = "nueva"

# Why a retazo offered to the job was never opened. Reported per piece so the
# shop can tell "nothing left fitted it" from "the job finished first" — the
# first is a fact about the rack, the second is a fact about the job.
REMNANT_NO_FIT = "no_fit"
REMNANT_TOO_SMALL = "too_small_for_margin"
REMNANT_JOB_ENDED = "job_ended"


def transform(points: Sequence[Point], deg: float, tx: float, ty: float) -> List[Point]:
    """Rotate ``points`` about the origin by ``deg`` (CCW), then translate.

    Matches spyrrow's PlacedItem convention (rotation first, translation after),
    which is how every :class:`Placement` in this package is to be reconstructed.
    """
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [(x * c - y * s + tx, x * s + y * c + ty) for (x, y) in points]


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
class ExtraSheet:
    """A leftover piece of sheet (retazo) offered to the nest as extra stock — E16.

    The 2D twin of ``nester.tube.model.ExtraStock``. ``label`` is the shop's
    identifier for that physical piece ("R-0007"), which is what the cut plan
    tells the operator to pull off the rack, so it must be unique within a job.
    Each piece is consumed at most once.

    A retazo is assumed rectangular — that is how sheet offcuts are actually
    stored and sheared. An irregular offcut has to be squared before it goes on
    the rack anyway.
    """

    width: float
    height: float
    label: str

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"{self.label}: remnant sheet must be > 0 in both axes, "
                f"got {self.width}x{self.height}"
            )
        if not self.label.strip():
            raise ValueError("remnant sheet label must not be empty")

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class Leftover:
    """The rectangle a sheet still has left over — a retazo candidate (E16).

    Position is absolute sheet coordinates (mm), origin bottom-left, so the plan
    can draw it and the shop can shear exactly that piece and put it back on the
    rack. It is what the sheet has left AFTER the part gap is respected, so it
    is a piece that can really be cut, not a bookkeeping remainder.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class SheetSpec:
    """One rectangular stock sheet definition + nesting allowances.

    ``margin`` is the unusable band along every edge (clamp / uneven edge);
    parts are nested inside the inset (usable) rectangle. ``part_gap`` is the
    minimum spacing between adjacent parts (kept >= kerf so cut lines never
    overlap). ``material``/``thickness`` define the stock group — parts of
    different material or thickness are never nested together.

    ``density`` (kg/m3) is what makes kilos possible (E8). Zero means the
    material was not recognized and no density was supplied: every weight then
    reads 0.0 and the report says it cannot weigh the job rather than inventing
    a number. Resolve it with :func:`nester.materials.density_for`.
    """

    width: float               # sheet extent in X (mm)
    height: float              # sheet extent in Y (mm)
    material: str = ""
    thickness: float = 0.0
    margin: float = 0.0        # edge margin, all four sides (mm)
    part_gap: float = 0.0      # minimum part-to-part spacing (mm)
    density: float = 0.0       # kg/m3; 0 = unknown, weights unavailable

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
    def can_weigh(self) -> bool:
        """True when this stock has both a thickness and a known density."""
        return self.thickness > 0 and self.density > 0

    @property
    def weight_kg(self) -> float:
        """Mass of one full sheet of this stock (kg); 0.0 when unknown."""
        return weight_kg(self.area, self.thickness, self.density)

    def resized(self, width: float, height: float) -> "SheetSpec":
        """Same stock and allowances, different sheet size — used for retazos."""
        return SheetSpec(
            width=width, height=height, material=self.material,
            thickness=self.thickness, margin=self.margin,
            part_gap=self.part_gap, density=self.density,
        )

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

    ``in_hole_of`` is set when this copy was nested INSIDE another part's hole
    (E15): it holds the index, within the same ``SheetLayout.placements``, of
    the part whose hole hosts it. The operator has to know — that slug is a
    part, not a drop, and it must not be thrown away with the skeleton.
    """

    part: FlatPart
    x: float
    y: float
    rotation: float = 0.0
    in_hole_of: Optional[int] = None

    @property
    def is_in_hole(self) -> bool:
        return self.in_hole_of is not None


@dataclass
class SheetLayout:
    """One physical stock sheet and everything nested on it.

    ``spec`` is this sheet's OWN stock: a job that consumes retazos mixes sizes,
    so per-sheet area, yield and weight all read from here rather than from the
    job's nominal sheet. ``source`` is :data:`NEW_SHEET` for a sheet to buy, or
    the retazo's label when it came off the rack.
    """

    index: int
    spec: SheetSpec
    placements: List[Placement] = field(default_factory=list)
    source: str = NEW_SHEET
    leftover: Optional[Leftover] = None

    @property
    def is_remnant(self) -> bool:
        return self.source != NEW_SHEET

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

    @property
    def in_hole_count(self) -> int:
        """Copies nested inside another part's hole on this sheet (E15)."""
        return sum(1 for p in self.placements if p.is_in_hole)

    @property
    def parts_weight_kg(self) -> float:
        """Mass of everything cut from this sheet (kg); 0.0 when unknown."""
        return weight_kg(self.used_area, self.spec.thickness, self.spec.density)

    @property
    def stock_weight_kg(self) -> float:
        """Mass of the sheet itself (kg); 0.0 when unknown."""
        return self.spec.weight_kg


@dataclass(frozen=True)
class SearchInfo:
    """What the sheet-count search actually did — reported, never inferred.

    The multi-sheet loop used to be an unbounded greedy walk: one sheet solved
    at a time, appended, closed forever. It could therefore never answer the
    question the shop is really asking ("how few sheets do I have to BUY?"), and
    a job that spent retazos could end up buying exactly as many new sheets as
    one that spent none. The search re-solves under a *ceiling* on new sheets
    and lowers that ceiling while the job stays feasible.

    ``area_floor_sheets`` is the physical lower bound (net part area minus what
    the rack can hold, over one new sheet's usable area) — no packing can beat
    it. ``ceiling_tried`` is every ceiling attempted, in order; ``ceiling_used``
    is the one that produced the returned layout. ``capped`` means the search
    stopped on its attempt/time/``max_new_sheets`` limit rather than because it
    had proved it could do no better: the result is the best FEASIBLE one seen,
    never a failure.
    """

    enabled: bool = False
    area_floor_sheets: int = 0
    ceiling_tried: Tuple[int, ...] = ()
    ceiling_used: Optional[int] = None
    attempts: int = 0
    capped: bool = False


@dataclass
class NestResult:
    """Nesting result for one stock group (single material/thickness)."""

    spec: SheetSpec
    sheets: List[SheetLayout] = field(default_factory=list)
    unplaceable: List[FlatPart] = field(default_factory=list)  # larger than a usable sheet
    # Parts the nesting engine cannot accept at all (degenerate contour). Each
    # entry is (part, ready-to-show message naming the file). They never reach
    # the solver and are never silently dropped — callers append ``messages``
    # to the job's ``errors[]``.
    invalid: List[Tuple[FlatPart, str]] = field(default_factory=list)
    # Retazos offered but never opened — either nothing left fitted them, or the
    # job ended first. Reported so the shop knows the rack was not silently
    # ignored, and so the piece stays available for the next job.
    remnants_unused: List[ExtraSheet] = field(default_factory=list)
    # {label: one of REMNANT_*} — WHY each unused retazo was never opened.
    remnant_reasons: Dict[str, str] = field(default_factory=dict)
    # How the sheet-count search behaved. Default = the old greedy walk.
    search: SearchInfo = field(default_factory=SearchInfo)

    @property
    def messages(self) -> List[str]:
        """User-facing reasons for every rejected part, in input order."""
        return [msg for _part, msg in self.invalid]

    @property
    def sheet_count(self) -> int:
        """TOTAL sheets used — new ones plus retazos."""
        return len(self.sheets)

    @property
    def new_sheets_needed(self) -> int:
        """Full sheets the shop has to BUY (retazos are already on the rack)."""
        return sum(1 for s in self.sheets if not s.is_remnant)

    @property
    def remnants_used(self) -> List[str]:
        """Labels of the retazos consumed, in sheet order."""
        return [s.source for s in self.sheets if s.is_remnant]

    @property
    def total_part_area(self) -> float:
        return sum(s.used_area for s in self.sheets)

    @property
    def total_sheet_area(self) -> float:
        """Area of the stock actually opened — sums the sheets, since a job
        that eats retazos has more than one sheet size in play."""
        return sum(s.spec.area for s in self.sheets)

    @property
    def new_sheet_area(self) -> float:
        """Area of the sheets to buy — what procurement pays for."""
        return sum(s.spec.area for s in self.sheets if not s.is_remnant)

    @property
    def in_hole_count(self) -> int:
        """Copies nested inside another part's holes across the job (E15)."""
        return sum(s.in_hole_count for s in self.sheets)

    @property
    def reclaimable(self) -> List[Tuple[int, Leftover]]:
        """(sheet number, rectangle) for every sheet with a usable offcut (E16)."""
        return [(s.index + 1, s.leftover) for s in self.sheets if s.leftover]

    @property
    def reclaimable_area(self) -> float:
        return sum(lo.area for _n, lo in self.reclaimable)

    @property
    def can_weigh(self) -> bool:
        return self.spec.can_weigh

    @property
    def parts_weight_kg(self) -> float:
        """Mass of every part cut in this job (kg); 0.0 when density is unknown."""
        return weight_kg(self.total_part_area, self.spec.thickness, self.spec.density)

    @property
    def stock_weight_kg(self) -> float:
        """Mass of all stock opened, retazos included (kg)."""
        return sum(s.stock_weight_kg for s in self.sheets)

    @property
    def new_stock_weight_kg(self) -> float:
        """Mass of the sheets to buy (kg) — the number that goes on a purchase order."""
        return sum(s.stock_weight_kg for s in self.sheets if not s.is_remnant)

    @property
    def drop_weight_kg(self) -> float:
        """Mass that does not leave as a part (kg)."""
        return max(0.0, self.stock_weight_kg - self.parts_weight_kg)

    @property
    def yield_pct(self) -> float:
        if not self.total_sheet_area:
            return 0.0
        return 100.0 * self.total_part_area / self.total_sheet_area

    # ----------------------------------------------------------------- #
    # Net vs gross (the design's "Métricas 2D")
    # ----------------------------------------------------------------- #
    # ``yield_pct`` above is the GROSS number and keeps its name, meaning and
    # value forever — the plan, the service and the web product all read it.
    # The net number below is the headline: it discounts the leftover that goes
    # BACK on the rack, exactly as the tube tool discounts a reclaimable retazo.
    # Without that, offering the job a retazo the shop already paid for ADDS to
    # the denominator and the headline yield drops — the product punishing the
    # shop for taking its own advice. Rule: no headline metric may get worse
    # because a job used material that was already bought.

    @property
    def consumed_area(self) -> float:
        """Stock opened MINUS what goes back to the rack (mm²)."""
        return max(self.total_sheet_area - self.reclaimable_area, 0.0)

    @property
    def waste_area(self) -> float:
        """Material that neither leaves as a part nor returns to the rack (mm²)."""
        return max(self.total_sheet_area - self.total_part_area - self.reclaimable_area, 0.0)

    @property
    def net_yield_pct(self) -> float:
        """The headline: parts over the material actually consumed."""
        consumed = self.consumed_area
        if not consumed:
            return 0.0
        return 100.0 * self.total_part_area / consumed

    @property
    def gross_yield_pct(self) -> float:
        """Parts over every square millimetre opened. Same value as ``yield_pct``."""
        return self.yield_pct

    @property
    def rack_stock_weight_kg(self) -> float:
        """Mass that came off the rack rather than off a purchase order (kg)."""
        return sum(s.stock_weight_kg for s in self.sheets if s.is_remnant)

    @property
    def reclaimable_weight_kg(self) -> float:
        """Mass of the offcuts booked back into inventory (kg)."""
        return weight_kg(self.reclaimable_area, self.spec.thickness, self.spec.density)

    @property
    def waste_weight_kg(self) -> float:
        """Mass really lost (kg) — drop minus what goes back on the rack."""
        return weight_kg(self.waste_area, self.spec.thickness, self.spec.density)
