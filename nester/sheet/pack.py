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

**Engine guards.** ``spyrrow`` is a pyo3 extension: a Rust ``unwrap`` failure
arrives as ``PanicException``, a *BaseException*, which slips past every
``except Exception`` in the call chain. Two layers keep that out of the user's
face — a pre-flight that refuses the geometry and the strip dimensions jagua-rs
panics on (:func:`validate_part`, :func:`_safe_strip_height`), and a
``BaseException`` boundary on the solve itself (:func:`_solve_strip`) that turns
anything left into a :class:`NestError` naming the job parameters.

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

from .model import (
    FlatPart, NestResult, Placement, Point, SheetLayout, SheetSpec, polygon_area,
)

# Named rotation policies -> allowed orientation angles (degrees). None = free.
ROTATION_MODES: Dict[str, Optional[Tuple[float, ...]]] = {
    "free": None,
    "grain": (0.0, 180.0),
    "fixed": (0.0,),
    "ortho": (0.0, 90.0, 180.0, 270.0),
}

_EPS = 1e-6

# --------------------------------------------------------------------------- #
# What the Rust engine refuses — measured against spyrrow 0.9 / jagua-rs 0.7
# --------------------------------------------------------------------------- #
# jagua-rs validates every item polygon on ``solve()`` and *panics* (it is a
# ``.unwrap()`` behind pyo3, not a Python exception) on:
#     "Simple polygon must have at least 3 points"
#     "Simple polygon has no area"
# Anything at or below this area in mm² counts as degenerate here.
_MIN_PART_AREA = 1e-6

# jagua-rs seeds the strip as a rectangle ``seed_width × strip_height`` with
# ``seed_width = Σ(item polygon area × demand) / strip_height``, then offsets
# that rectangle INWARD by ``min_items_separation / 2`` on every side. If the
# offset empties the rectangle it panics with "Offset resulted in an empty
# polygon" (jagua-rs src/probs/spp/entities/strip.rs). Measured exactly: the
# solve survives iff ``Σarea / strip_height > separation`` (and the height
# itself clears the separation). This factor keeps us off that knife edge.
_STRIP_SAFETY = 1.05

# pyo3 panics surface as ``pyo3_runtime.PanicException``, which derives from
# BaseException — a bare ``except Exception`` sails straight past it. The module
# is synthesized by pyo3 and is not always importable, so this is best-effort and
# the call site catches BaseException regardless.
try:  # pragma: no cover - depends on how the extension was built
    from pyo3_runtime import PanicException as _PanicException  # type: ignore
except Exception:  # pragma: no cover
    _PanicException = None  # type: ignore[assignment]


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
        # Pre-flight FIRST: degenerate geometry panics the Rust engine (a process
        # -level unwrap, not an exception), so it must never reach the solver.
        reason = validate_part(part)
        if reason is not None:
            result.invalid.append((part, reason))
            continue
        if not _fits_sheet(part, uw, uh, allowed):
            result.unplaceable.append(part)
            continue
        catalog[pid] = part
        remaining[pid] = part.qty
        orient[pid] = allowed

    if result.invalid and not catalog and not result.unplaceable:
        raise NestError(
            "ninguna pieza tiene geometría utilizable para nestear: "
            + "; ".join(result.messages)
        )

    parts_total = sum(remaining.values())  # placeable demand

    sheet_index = 0
    while any(v > 0 for v in remaining.values()):
        if sheet_index >= max_sheets:  # pragma: no cover - runaway guard
            raise NestError(f"exceeded max_sheets={max_sheets}; aborting.")
        if should_cancel is not None and should_cancel():
            # Between sheets is the only safe interruption point: a spyrrow solve
            # is one opaque call with its own time budget.
            raise NestCancelled(result)

        live = [pid for pid in remaining if remaining[pid] > 0]
        gap = spec.part_gap or 0.0
        layout = SheetLayout(index=sheet_index, spec=spec)

        # The engine seeds its strip from the queued area; too little area on a
        # tall strip and the separation offset empties it (Rust panic). Pick a
        # strip height that cannot trip it — or skip the engine entirely.
        items_area = sum(remaining[pid] * catalog[pid].outer_area for pid in live)
        min_h = max(_min_presentable_height(catalog[pid], orient[pid]) for pid in live)
        strip_h = _safe_strip_height(items_area, uh, gap, min_h)

        if strip_h is None:
            if not _shelf_fill(layout, catalog, remaining, orient, uw, uh, margin, gap):
                raise NestError(
                    f"no se pudo acomodar ninguna pieza en una hoja de "
                    f"{spec.width:g}×{spec.height:g} mm con margen {spec.margin:g} mm "
                    f"y separación {gap:g} mm."
                )
        else:
            items = [
                Item(pid, list(catalog[pid].outer), demand=remaining[pid],
                     allowed_orientations=(list(orient[pid]) if orient[pid] is not None else None))
                for pid in live
            ]
            config = StripPackingConfig(
                min_items_separation=(spec.part_gap or None),
                total_computation_time=max(int(time_per_sheet), 1),
                seed=seed + sheet_index,
            )
            solution = _solve_strip(
                lambda: StripPackingInstance(f"sheet{sheet_index}", strip_h, items), config,
                spec=spec, rotation=rotation, items=len(items),
                copies=sum(remaining[pid] for pid in live),
                strip_height=strip_h, sheet_index=sheet_index,
            )

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
                _force_single(layout, catalog, remaining, orient, uw, uh, margin, gap)

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


def _solve_strip(instance_factory, config, *, spec: SheetSpec, rotation: str,
                 items: int, copies: int, strip_height: float, sheet_index: int):
    """Run one spyrrow solve with a hard boundary around Rust panics.

    ``pyo3`` turns a Rust panic into ``PanicException``, a **BaseException** — it
    slips through every ``except Exception`` in the call chain and surfaces to
    the user as a raw ``Result::unwrap()`` string. Pre-flight (:func:`validate_part`,
    :func:`_safe_strip_height`) rules out the panics we know about; this is the
    net for the ones we don't. ``KeyboardInterrupt``/``SystemExit``/``GeneratorExit``
    are re-raised untouched — they are control flow, not engine failures.
    """
    try:
        return instance_factory().solve(config)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except NestError:
        raise
    except BaseException as e:  # noqa: BLE001 - deliberate: PanicException is a BaseException
        detail = " ".join(str(e).split())[:300] or type(e).__name__
        kind = ("pánico interno del motor" if _is_panic(e) else "error del motor")
        raise NestError(
            f"{kind} de nido (spyrrow/jagua-rs) en la hoja {sheet_index + 1}: {detail}. "
            f"Parámetros: hoja {spec.width:g}×{spec.height:g} mm, margen {spec.margin:g} mm, "
            f"separación {spec.part_gap:g} mm, rotación '{rotation}', "
            f"franja de {strip_height:g} mm, {items} pieza(s) distinta(s) / {copies} copia(s). "
            f"Probá bajar la separación (--gap) o el margen, o revisá que los contornos "
            f"del DXF cierren sin cruzarse."
        ) from e


def _is_panic(e: BaseException) -> bool:
    if _PanicException is not None and isinstance(e, _PanicException):
        return True
    return type(e).__name__ == "PanicException"


def validate_part(part: FlatPart) -> Optional[str]:
    """Reason ``part`` cannot be handed to the engine, or None if it is fine.

    This mirrors, in Python, the checks jagua-rs performs *inside* a Rust
    ``unwrap`` — reaching them means a process-level panic instead of an error,
    so every one of them has to be caught here first. The message is the
    user-facing Spanish string and already names the source file (a part's
    ``name`` is its DXF filename, ``file.dxf#2`` when a file holds several).
    """
    ring = part.outer
    if any(not math.isfinite(c) for pt in ring for c in pt):
        return (f"{part.name}: el contorno tiene coordenadas inválidas "
                f"(NaN o infinito) — el nido la excluyó")
    distinct = _distinct_points(ring)
    if distinct < 3:
        return (f"{part.name}: el contorno tiene menos de 3 puntos distintos "
                f"({distinct}) — el nido la excluyó")
    if polygon_area(ring) <= _MIN_PART_AREA:
        return (f"{part.name}: el contorno no encierra área (puntos colineales "
                f"o contorno que se cruza sobre sí mismo) — el nido la excluyó")
    return None


def _distinct_points(ring: Sequence[Point], tol: float = 1e-9) -> int:
    """Number of vertices left after collapsing consecutive duplicates (cyclic)."""
    out: List[Point] = []
    for p in ring:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > tol:
            out.append(p)
    while len(out) >= 2 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return len(out)


def _safe_strip_height(
    items_area: float, uh: float, sep: float, min_part_height: float
) -> Optional[float]:
    """Strip height whose jagua-rs seed rectangle survives the separation offset.

    Returns ``uh`` when the full usable height is already safe, a reduced height
    when it is not, or ``None`` when no height that still hosts the parts can be
    made safe (then the caller must not call the engine at all).

    Why: the seed width is ``items_area / strip_height``, shrunk by ``sep`` in
    total by the container offset. A tall sheet plus a small job drives that
    seed below ``sep`` and the Rust side panics before the first placement — the
    exact failure a 915×2440 sheet with three small parts and a 3 mm gap hits.
    Shortening the strip *raises* the seed width; the layout stays valid because
    a shorter strip is a subset of the usable sheet.
    """
    if sep <= 0:
        return uh
    if items_area > sep * uh * _STRIP_SAFETY:
        return uh
    h = items_area / (sep * _STRIP_SAFETY)
    if h <= sep * _STRIP_SAFETY or h < min_part_height:
        return None
    return h


def _min_presentable_height(part: FlatPart, allowed: Optional[Sequence[float]]) -> float:
    """Smallest strip height that can still hold this part, over its orientations."""
    angles: Sequence[float] = (0.0, 90.0) if allowed is None else (tuple(allowed) or (0.0,))
    return min(_rot_size(part, a)[1] for a in angles)


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


def _shelf_fill(
    layout: SheetLayout,
    catalog: Dict[str, FlatPart],
    remaining: Dict[str, int],
    orient: Dict[str, Optional[Tuple[float, ...]]],
    uw: float,
    uh: float,
    margin: float,
    gap: float,
    max_items: Optional[int] = None,
) -> int:
    """Deterministic bounding-box shelf packing — the engine-free fallback.

    Rows left-to-right, bottom-to-top, each part in its shortest fitting
    orientation, ``gap`` between neighbours and inside the margin. Yield is worse
    than spyrrow's silhouette packing, but the layout is always valid and it
    needs no Rust. Used to guarantee forward progress when a solve harvested
    nothing, and as the fallback when the engine cannot be invoked safely at all
    (see :func:`_safe_strip_height`). Mutates ``remaining``; returns how many
    copies it placed.
    """
    placed = 0
    cur_x = cur_y = row_h = 0.0
    order = sorted((pid for pid in remaining if remaining[pid] > 0),
                   key=lambda pid: -catalog[pid].area)
    for pid in order:
        part = catalog[pid]
        allowed = orient[pid]
        angles: Sequence[float] = (0.0, 90.0) if allowed is None else (tuple(allowed) or (0.0,))
        while remaining[pid] > 0:
            if max_items is not None and placed >= max_items:
                return placed
            best: Optional[Tuple[float, float, float]] = None
            for a in angles:
                w, h = _rot_size(part, a)
                if w > uw + _EPS or h > uh + _EPS:
                    continue
                if best is None or h < best[2]:
                    best = (a, w, h)
            if best is None:
                break  # cannot fit any orientation (prefiltered — defensive)
            a, w, h = best
            if cur_x > _EPS and cur_x + w > uw + _EPS:
                cur_y += row_h + gap  # start a new row
                cur_x = 0.0
                row_h = 0.0
            if cur_y + h > uh + _EPS:
                return placed  # sheet full
            pts = transform(part.outer, a, 0.0, 0.0)
            minx = min(p[0] for p in pts)
            miny = min(p[1] for p in pts)
            layout.placements.append(
                Placement(part=part, x=margin + cur_x - minx, y=margin + cur_y - miny, rotation=a)
            )
            remaining[pid] -= 1
            placed += 1
            cur_x += w + gap
            row_h = max(row_h, h)
    return placed


def _force_single(
    layout: SheetLayout,
    catalog: Dict[str, FlatPart],
    remaining: Dict[str, int],
    orient: Dict[str, Optional[Tuple[float, ...]]],
    uw: float,
    uh: float,
    margin: float,
    gap: float = 0.0,
) -> None:
    """Place exactly one remaining part on this (empty) sheet to guarantee progress."""
    _shelf_fill(layout, catalog, remaining, orient, uw, uh, margin, gap, max_items=1)
