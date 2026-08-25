"""2D irregular nesting: pack flat parts onto fixed-size stock sheets.

The heavy lifting is done by ``spyrrow`` (the Rust ``sparrow``/``jagua-rs``
engine), which solves *strip packing*: given a fixed strip height it places
irregular shapes to minimize the used width. Real stock is a *fixed-size sheet*
(width x height), so we wrap the strip packer in a greedy multi-sheet loop:

    while parts remain:
        solve strip packing at height = usable sheet height
        harvest every placed copy whose bounding box fits within the usable
            sheet width -> that becomes one physical sheet
        return the overflow to the pool and pack the next sheet

Each solve is dense (spyrrow packs bottom-left-ish), so the harvested left
block is a valid, tight sheet; parts spilling past the sheet's right edge roll
to the next sheet. One solve per sheet.

Rotation is honored per part via ``allowed_orientations`` (None = free,
(0,180) = grain-locked, (0,) = fixed). Part-to-part spacing maps to spyrrow's
``min_items_separation``; the sheet edge margin is handled by shrinking the
usable rectangle and offsetting placements back out by the margin.

**Progress and cancellation** (optional, additive — the CLI passes neither):
``nest(..., progress=cb, should_cancel=fn)`` calls ``cb(NestProgress)`` once per
completed sheet and consults ``fn()`` between sheets. There is no mid-sheet
interruption: one spyrrow solve is an opaque, time-budgeted call, so a cancel
lands after the sheet in flight finishes its ``time_per_sheet`` budget. Cancel
raises :class:`NestCancelled`, which carries the sheets solved so far.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .model import FlatPart, NestResult, Placement, Point, SheetLayout, SheetSpec

# Named rotation policies -> allowed orientation angles (degrees). None = free.
ROTATION_MODES: Dict[str, Optional[Tuple[float, ...]]] = {
    "free": None,
    "grain": (0.0, 180.0),
    "fixed": (0.0,),
    "ortho": (0.0, 90.0, 180.0, 270.0),
}

_EPS = 1e-6


class NestError(RuntimeError):
    pass


class NestCancelled(NestError):
    """Raised when ``should_cancel()`` returned True between sheets.

    ``partial`` holds the sheets already solved — never silently discarded, so a
    caller can still show (or keep) the work that was paid for.
    """

    def __init__(self, partial: NestResult, message: str = "nesting cancelled") -> None:
        super().__init__(message)
        self.partial = partial


@dataclass(frozen=True)
class NestProgress:
    """One progress tick: the state after a sheet finished solving.

    ``sheets_total_estimate`` is exactly that — an ESTIMATE. The multi-sheet
    loop cannot know the total up front (each solve is stochastic), so it is
    projected from the net part area placed per sheet so far against the area
    still queued. It is monotonically >= ``sheets_done`` and can move in either
    direction as the real density becomes known.
    """

    sheets_done: int
    sheets_total_estimate: int
    parts_placed: int
    parts_total: int              # placeable demand (unplaceable parts excluded)
    last_sheet_utilization_pct: float


ProgressCallback = Callable[[NestProgress], None]


def _estimate_total_sheets(sheets_done: int, placed_area: float,
                           remaining_area: float) -> int:
    """Project the sheet total from the average net area landed per sheet."""
    if remaining_area <= 0:
        return sheets_done
    per_sheet = placed_area / sheets_done if sheets_done else 0.0
    if per_sheet <= 0:
        return sheets_done + 1
    return sheets_done + max(1, math.ceil(remaining_area / per_sheet))


def nest(
    parts: Sequence[FlatPart],
    spec: SheetSpec,
    *,
    rotation: str = "free",
    time_per_sheet: int = 4,
    seed: int = 0,
    max_sheets: int = 1000,
    progress: Optional[ProgressCallback] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> NestResult:
    """Nest ``parts`` onto sheets of ``spec``. One stock group (material/thickness).

    ``rotation`` is a key of :data:`ROTATION_MODES` (the job-wide default; a part
    may override via its own ``allowed_orientations``). ``time_per_sheet`` is the
    per-solve compute budget in seconds — more time, better density.

    ``progress`` is called with a :class:`NestProgress` after each sheet is
    solved; ``should_cancel`` is consulted before each sheet and raises
    :class:`NestCancelled` (carrying the partial result) when it returns True.
    Both are optional and change nothing about the layout produced.
    """
    try:
        from spyrrow import Item, StripPackingConfig, StripPackingInstance
    except ImportError as e:  # pragma: no cover
        raise NestError(
            "spyrrow is not installed in this env. Install with: "
            ".venv/bin/pip install spyrrow  (needs Python 3.11+)."
        ) from e

    if rotation not in ROTATION_MODES:
        raise NestError(f"unknown rotation mode '{rotation}'. Options: {sorted(ROTATION_MODES)}")
    job_orient = ROTATION_MODES[rotation]

    uw, uh = spec.usable_width, spec.usable_height
    margin = spec.margin
    result = NestResult(spec=spec)

    # Index parts and split off any that can't fit a single usable sheet.
    catalog: Dict[str, FlatPart] = {}
    remaining: Dict[str, int] = {}
    orient: Dict[str, Optional[Tuple[float, ...]]] = {}
    for i, part in enumerate(parts):
        pid = str(i)
        allowed = part.allowed_orientations if part.allowed_orientations is not None else job_orient
        if not _fits_sheet(part, uw, uh, allowed):
            result.unplaceable.append(part)
            continue
        catalog[pid] = part
        remaining[pid] = part.qty
        orient[pid] = allowed

    parts_total = sum(remaining.values())  # placeable demand

    sheet_index = 0
    while any(v > 0 for v in remaining.values()):
        if sheet_index >= max_sheets:  # pragma: no cover - runaway guard
            raise NestError(f"exceeded max_sheets={max_sheets}; aborting.")
        if should_cancel is not None and should_cancel():
            # Between sheets is the only safe interruption point: a spyrrow solve
            # is one opaque call with its own time budget.
            raise NestCancelled(result)

        items = [
            Item(pid, list(catalog[pid].outer), demand=remaining[pid],
                 allowed_orientations=(list(orient[pid]) if orient[pid] is not None else None))
            for pid in remaining if remaining[pid] > 0
        ]
        instance = StripPackingInstance(f"sheet{sheet_index}", uh, items)
        config = StripPackingConfig(
            min_items_separation=(spec.part_gap or None),
            total_computation_time=max(int(time_per_sheet), 1),
            seed=seed + sheet_index,
        )
        solution = instance.solve(config)

        layout = SheetLayout(index=sheet_index, spec=spec)
        harvested: Dict[str, int] = {}
        for pi in solution.placed_items:
            part = catalog[pi.id]
            tx, ty = pi.translation
            maxx = _placed_max_x(part.outer, pi.rotation, tx)
            if maxx <= uw + _EPS:
                layout.placements.append(
                    Placement(part=part, x=tx + margin, y=ty + margin, rotation=pi.rotation)
                )
                harvested[pi.id] = harvested.get(pi.id, 0) + 1

        if not harvested:
            # Safety net: the dense packing oriented everything past the sheet
            # width. Force one placeable part onto its own sheet.
            _force_single(layout, catalog, remaining, orient, uw, uh, margin)

        for pid, n in harvested.items():
            remaining[pid] -= n
        result.sheets.append(layout)
        sheet_index += 1

        if progress is not None:
            placed_area = result.total_part_area
            remaining_area = sum(n * catalog[pid].area
                                 for pid, n in remaining.items() if n > 0)
            progress(NestProgress(
                sheets_done=len(result.sheets),
                sheets_total_estimate=_estimate_total_sheets(
                    len(result.sheets), placed_area, remaining_area),
                parts_placed=sum(s.part_count for s in result.sheets),
                parts_total=parts_total,
                last_sheet_utilization_pct=round(layout.utilization * 100, 2),
            ))

    return result


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def transform(points: Sequence[Point], deg: float, tx: float, ty: float) -> List[Point]:
    """Rotate ``points`` about the origin by ``deg`` (CCW), then translate.

    Matches spyrrow's PlacedItem convention (rotation first, translation after).
    """
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [(x * c - y * s + tx, x * s + y * c + ty) for (x, y) in points]


def _placed_max_x(outer: Sequence[Point], deg: float, tx: float) -> float:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return max(x * c - y * s + tx for (x, y) in outer)


def _rot_size(part: FlatPart, deg: float) -> Tuple[float, float]:
    pts = transform(part.outer, deg, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def _fits_sheet(
    part: FlatPart, uw: float, uh: float, allowed: Optional[Sequence[float]]
) -> bool:
    """True if some allowed orientation fits the part within a usable sheet.

    For free rotation we approximate by testing the axis-aligned bbox at 0° and
    90° (a sheet part that needs an oblique angle to fit at all is pathological).
    """
    angles: Sequence[float] = allowed if allowed else (0.0,)
    if allowed is None:  # free rotation
        angles = (0.0, 90.0)
    for a in angles:
        w, h = _rot_size(part, a)
        if w <= uw + _EPS and h <= uh + _EPS:
            return True
    return False


def _force_single(
    layout: SheetLayout,
    catalog: Dict[str, FlatPart],
    remaining: Dict[str, int],
    orient: Dict[str, Optional[Tuple[float, ...]]],
    uw: float,
    uh: float,
    margin: float,
) -> None:
    """Place exactly one remaining part on this (empty) sheet to guarantee progress."""
    for pid, n in remaining.items():
        if n <= 0:
            continue
        part = catalog[pid]
        allowed = orient[pid]
        angles: Sequence[float] = (0.0, 90.0) if allowed is None else (allowed or (0.0,))
        for a in angles:
            pts = transform(part.outer, a, 0.0, 0.0)
            minx = min(p[0] for p in pts)
            miny = min(p[1] for p in pts)
            w, h = _rot_size(part, a)
            if w <= uw + _EPS and h <= uh + _EPS:
                # shift so the rotated shape sits at the usable origin
                layout.placements.append(
                    Placement(part=part, x=margin - minx, y=margin - miny, rotation=a)
                )
                remaining[pid] -= 1
                return
    # nothing placeable remained (shouldn't happen — prefiltered) — leave empty.
