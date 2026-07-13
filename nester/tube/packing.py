"""1D cutting-stock solver.

First Fit Decreasing (FFD): sort parts longest-first, drop each into the first
open bar it still fits, otherwise start a new bar. Deterministic, fast, and
gives strong yield for workshop nesting. Good enough that the bottleneck is the
saw, not the math.

Each placed part consumes ``length + kerf`` of the usable region (one cut to
free it). This slightly over-reserves kerf on the last part of a bar, which is
the safe direction for a real saw.
"""

from __future__ import annotations

from typing import Iterable, List

from .model import BarLayout, Part, Placement, ProfileResult, StockSpec


def pack_profile(parts: Iterable[Part], spec: StockSpec) -> ProfileResult:
    """Nest one profile's parts onto bars of a single stock length."""
    result = ProfileResult(profile=spec.profile, spec=spec)
    usable = spec.usable_length

    # Separate parts that can never fit on a single bar.
    fits: List[Part] = []
    for p in parts:
        if p.length + spec.kerf > usable + 1e-9:
            result.unplaceable.append(p)
        else:
            fits.append(p)

    # Longest first.
    fits.sort(key=lambda p: p.length, reverse=True)

    for part in fits:
        need = part.length + spec.kerf
        placed = False
        for bar in result.bars:
            if bar.remnant + 1e-9 >= need:
                _append(bar, part)
                placed = True
                break
        if not placed:
            bar = BarLayout(index=len(result.bars), spec=spec)
            _append(bar, part)
            result.bars.append(bar)

    return result


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
