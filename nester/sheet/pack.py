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
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

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


def nest(
    parts: Sequence[FlatPart],
    spec: SheetSpec,
    *,
    rotation: str = "free",
    time_per_sheet: int = 4,
    seed: int = 0,
    max_sheets: int = 1000,
) -> NestResult:
    """Nest ``parts`` onto sheets of ``spec``. One stock group (material/thickness).

    ``rotation`` is a key of :data:`ROTATION_MODES` (the job-wide default; a part
    may override via its own ``allowed_orientations``). ``time_per_sheet`` is the
    per-solve compute budget in seconds — more time, better density.
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

    sheet_index = 0
    while any(v > 0 for v in remaining.values()):
        if sheet_index >= max_sheets:  # pragma: no cover - runaway guard
            raise NestError(f"exceeded max_sheets={max_sheets}; aborting.")

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
