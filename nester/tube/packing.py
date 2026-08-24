"""1D cutting-stock solver.

First Fit Decreasing (FFD): sort parts longest-first, drop each into the first
open bar it still fits, otherwise start a new bar. Deterministic, fast, and
gives strong yield for workshop nesting. Good enough that the bottleneck is the
saw, not the math.

Each placed part consumes ``length + kerf`` of the usable region (one cut to
free it). This slightly over-reserves kerf on the last part of a bar, which is
the safe direction for a real saw.

Stock is a full tramo (unlimited) plus, optionally, the shop's remnants
(``StockSpec.extra_stock`` — E9). When a part needs a bar that isn't open yet,
the SMALLEST remnant that fits it wins; only when no remnant fits does the plan
buy a new tramo. Smallest-first conserves the big remnants for the big parts,
and every remnant is a one-off physical piece, so it can be opened at most once.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .model import NEW_BAR, BarLayout, Part, Placement, ProfileResult, StockSpec

_EPS = 1e-9


def pack_profile(parts: Iterable[Part], spec: StockSpec) -> ProfileResult:
    """Nest one profile's parts onto new tramos plus any remnants on the rack."""
    result = ProfileResult(profile=spec.profile, spec=spec)
    tramo_usable = spec.usable_length

    # Remnant pool: (usable, length, label), smallest usable first. A remnant
    # shorter than the trims has nothing to give, so it never enters the pool.
    pool: List[Tuple[float, float, str]] = sorted(
        (spec.usable_for(e.length), e.length, e.label)
        for e in spec.extra_stock
        if spec.usable_for(e.length) > 0
    )
    biggest_usable = max([tramo_usable] + [u for u, _l, _lb in pool])

    # Separate parts that can never fit on ANY available bar.
    fits: List[Part] = []
    for p in parts:
        if p.length + spec.kerf > biggest_usable + _EPS:
            result.unplaceable.append(p)
        else:
            fits.append(p)

    # Longest first.
    fits.sort(key=lambda p: p.length, reverse=True)

    for part in fits:
        need = part.length + spec.kerf
        placed = False
        for bar in result.bars:
            if bar.remnant + _EPS >= need:
                _append(bar, part)
                placed = True
                break
        if placed:
            continue
        bar = _open_bar(result, spec, pool, need, tramo_usable)
        if bar is None:
            # Only an already-consumed remnant could ever have held it.
            result.unplaceable.append(part)
            continue
        _append(bar, part)
        result.bars.append(bar)

    # Bars (remnants included) are only ever opened for a part that goes on
    # them, so an empty bar in the layout would be a solver bug, not a plan.
    assert all(b.placements for b in result.bars), f"{spec.profile}: empty bar in layout"

    return result


def _open_bar(
    result: ProfileResult,
    spec: StockSpec,
    pool: List[Tuple[float, float, str]],
    need: float,
    tramo_usable: float,
) -> BarLayout | None:
    """Start a bar for a part that fits no open one: smallest fitting remnant,
    else a new tramo, else None (nothing left long enough)."""
    for i, (usable, length, label) in enumerate(pool):   # ascending usable
        if usable + _EPS >= need:
            pool.pop(i)                                  # a remnant is used once
            return BarLayout(index=len(result.bars), spec=spec,
                             stock_length=length, source=label)
    if need <= tramo_usable + _EPS:
        return BarLayout(index=len(result.bars), spec=spec,
                         stock_length=spec.stock_length, source=NEW_BAR)
    return None


def _append(bar: BarLayout, part: Part) -> None:
    """Place a part at the current consumed offset of the bar."""
    start = bar.consumed_length          # offset within usable region (kerf-inclusive)
    end = start + part.length
    bar.placements.append(Placement(part=part, start=start, end=end))


def pack_all(parts: Iterable[Part], specs: dict[str, StockSpec]) -> List[ProfileResult]:
    """Group parts by profile and nest each group against its stock spec.

    Raises KeyError if a part references a profile with no stock spec.
    """
    by_profile: dict[str, List[Part]] = {}
    for p in parts:
        by_profile.setdefault(p.profile, []).append(p)

    results: List[ProfileResult] = []
    for profile in sorted(by_profile):
        if profile not in specs:
            raise KeyError(
                f"No stock spec for profile '{profile}'. "
                f"Known: {sorted(specs)}"
            )
        results.append(pack_profile(by_profile[profile], specs[profile]))
    return results
