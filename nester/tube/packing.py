"""1D cutting-stock solver.

First Fit Decreasing (FFD): sort parts longest-first, drop each into the first
open bar it still fits, otherwise start a new bar. Deterministic, fast, and
gives strong yield for workshop nesting. Good enough that the bottleneck is the
saw, not the math.

Each placed part consumes ``length + kerf`` of the usable region (one cut to
free it). This slightly over-reserves kerf on the last part of a bar, which is
the safe direction for a real saw.

Stock is a full tramo (unlimited) plus, optionally, the shop's remnants
(``StockSpec.extra_stock`` — E9). Within a run the SMALLEST remnant that fits a
part wins, so the big pieces stay free for the big parts, and every remnant is
one physical piece, usable at most once.

**A remnant is only opened when it removes a tramo** (``minimize_bars``, the
1D twin of the 2D rule in :func:`nester.sheet.pack._search`). The old solver
spent the rack unconditionally, which measurably cost the shop offcuts for
nothing: on a 6 m job with an 89.0% baseline, offering a 1000 / 2200 / 3000 mm
piece bought the SAME 20 tramos and dropped the reported yield to 88.2 / 87.3 /
86.8% — the bigger the offcut a shop offered, the harder the plan punished it.
So the packer now runs FFD three ways (it is milliseconds, so this is free):

1. with NO rack -> the baseline purchase ``B0``;
2. with the FULL rack -> ``B_full``. Not better than ``B0``? the whole rack is
   declined, reason ``no_gain``, and stays on the shelf where it is worth most;
3. better? then greedily drop pieces whose removal does not make the answer
   worse — LONGEST first, so what survives is the smallest set of the smallest
   pieces. One retazo alone may not remove a tramo while two together do, which
   is why the subset is found by re-packing rather than by testing each piece on
   its own.

"Better" is ``(unplaceable parts, tramos to buy)``, lexicographically: a
remnant that rescues a part no tramo could hold is worth opening even when the
purchase order does not shrink.

``minimize_bars=False`` restores the original unconditional spending.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from .model import (
    NEW_BAR, REMNANT_JOB_ENDED, REMNANT_NO_FIT, REMNANT_NO_GAIN,
    REMNANT_TOO_SMALL,
    BarLayout, ExtraStock, Part, Placement, ProfileResult, StockSpec,
)

_EPS = 1e-9

#: Deterministic bound on the trial packs the minimal-subset search may spend.
#: One trial is one full FFD run, so a rack of 200 pieces against a 500-part job
#: would otherwise cost ~6 s inside a SYNCHRONOUS /v1/nest. Pieces are tried
#: longest-first, so the budget is spent where burning an offcut costs most, and
#: anything the search never reaches stays in the kept set — i.e. spent, which is
#: the old behaviour and still never worse than the no-rack baseline (that
#: comparison is exact and always runs). A wall-clock budget was rejected on
#: purpose: the same input must always produce the same cut plan.
MAX_SUBSET_TRIALS = 64


def pack_profile(parts: Iterable[Part], spec: StockSpec,
                 *, minimize_bars: bool = True) -> ProfileResult:
    """Nest one profile's parts onto new tramos plus any remnants on the rack.

    With ``minimize_bars`` (the default) the rack is spent only where spending
    it removes a tramo from the purchase order; see the module docstring.
    """
    parts = list(parts)
    offered: Tuple[ExtraStock, ...] = tuple(spec.extra_stock)

    if not offered:
        return _pack(parts, spec, ())

    if not minimize_bars:
        result = _pack(parts, spec, offered)
        _record_unused(result, spec, parts, offered, declined=())
        return result

    baseline = _pack(parts, spec, ())          # B0 — buy everything
    full = _pack(parts, spec, offered)         # B_full — spend the whole rack

    if _cost(full) >= _cost(baseline):
        # The rack buys nothing. Hand back the no-rack plan and leave every
        # piece on the shelf: spending it would cost a physical offcut for a
        # purchase order that does not change.
        _record_unused(baseline, spec, parts, offered, declined=offered)
        return baseline

    # The rack IS worth opening — but maybe not all of it. Drop the longest
    # pieces first: whatever survives is the smallest set of the smallest
    # pieces that still achieves the answer.
    keep: List[ExtraStock] = sorted(offered, key=lambda e: -e.length)
    best = _cost(full)
    i = 0
    trials = 0
    while i < len(keep) and trials < MAX_SUBSET_TRIALS:
        trial = keep[:i] + keep[i + 1:]
        trials += 1
        cost = _cost(_pack(parts, spec, trial))
        if cost <= best:
            keep, best = trial, cost      # dropped: the next longest is now at i
        else:
            i += 1                        # load-bearing: keep it and move on

    kept = {id(e) for e in keep}
    # Re-pack in the ORIGINAL offer order so the layout does not depend on the
    # order the search happened to try things in.
    final_rack = tuple(e for e in offered if id(e) in kept)
    result = _pack(parts, spec, final_rack)
    _record_unused(result, spec, parts, offered,
                   declined=tuple(e for e in offered if id(e) not in kept))
    return result


def _cost(result: ProfileResult) -> Tuple[int, int]:
    """What a plan costs the shop: (parts it could not place, tramos to buy).

    Lexicographic, lower is better. Remnants are free — they are already paid
    for and already in the building — so they are deliberately absent.
    """
    return (len(result.unplaceable), result.new_bars_needed)


def _pack(parts: Sequence[Part], spec: StockSpec,
          rack: Sequence[ExtraStock]) -> ProfileResult:
    """One FFD run against exactly ``rack`` (plus unlimited new tramos)."""
    result = ProfileResult(profile=spec.profile, spec=spec)
    tramo_usable = spec.usable_length

    # Remnant pool: (usable, length, label), smallest usable first. A remnant
    # shorter than the trims has nothing to give, so it never enters the pool.
    pool: List[Tuple[float, float, str]] = sorted(
        (spec.usable_for(e.length), e.length, e.label)
        for e in rack
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


def _record_unused(
    result: ProfileResult,
    spec: StockSpec,
    parts: Sequence[Part],
    offered: Sequence[ExtraStock],
    declined: Sequence[ExtraStock],
) -> None:
    """Name every offered remnant the plan did not open, and say why.

    Reasons are checked physical-first: a piece too short to carry the trims, or
    that nothing in the job fits, is reported as such even when the search would
    also have declined it — the shop learns more from "nothing fits it" than
    from "it bought nothing".
    """
    used = set(result.remnants_used)
    declined_ids = {id(e) for e in declined}
    # The SHORTEST part decides whether anything at all could go on a piece.
    shortest = min((p.length for p in parts), default=0.0)
    for e in offered:
        if e.label in used:
            continue
        if spec.usable_for(e.length) <= 0:
            reason = REMNANT_TOO_SMALL
        elif shortest + spec.kerf > spec.usable_for(e.length) + _EPS:
            reason = REMNANT_NO_FIT
        elif id(e) in declined_ids:
            reason = REMNANT_NO_GAIN
        else:
            reason = REMNANT_JOB_ENDED
        result.remnants_unused.append(e)
        result.remnant_reasons[e.label] = reason


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


def pack_all(parts: Iterable[Part], specs: dict[str, StockSpec],
             *, minimize_bars: bool = True) -> List[ProfileResult]:
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
        results.append(pack_profile(by_profile[profile], specs[profile],
                                    minimize_bars=minimize_bars))
    return results
