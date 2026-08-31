"""1D cutting-stock solver.

First Fit Decreasing (FFD): sort parts longest-first, drop each into the first
open bar it still fits, otherwise start a new bar. Deterministic, fast, and
gives strong yield for workshop nesting. Good enough that the bottleneck is the
saw, not the math.

Each placed part consumes its own length plus the gap reserved AFTER it — the
saw kerf, always, plus two things a piece may declare for itself
(docs/PLAN-orientacion-tubo.md): a protruding end feature (a welded tongue,
say) that collides with the next piece's feature on a SHARED face, and any
extra gap the shop asks for by hand. With neither in play the gap is exactly
``kerf`` for every part, byte-identical to the tool before this existed. See
:func:`clearance`. This slightly over-reserves kerf on the last part of a bar
(nothing follows it, so its declared gap is never spent), which is the safe
direction for a real saw.

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

from typing import FrozenSet, Iterable, List, NamedTuple, Sequence, Tuple

from .model import (
    GAP_EXTRA, GAP_INTERLEAVED, GAP_SHARED_FACES,
    NEW_BAR, REMNANT_JOB_ENDED, REMNANT_NO_FIT, REMNANT_NO_GAIN,
    REMNANT_TOO_SMALL,
    BarLayout, EndFeature, ExtraStock, Part, Placement, ProfileResult, StockSpec,
)

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Pair-dependent clearance (docs/PLAN-orientacion-tubo.md §2)
# --------------------------------------------------------------------------- #

class Clearance(NamedTuple):
    """The gap between two consecutive pieces, and why it is that size."""

    mm: float
    reasons: Tuple[str, ...]


def _rotated_faces(feature: EndFeature, orientation_deg: float) -> FrozenSet[int]:
    """Which faces ``feature`` actually occupies once the part is clocked.

    Rotating a piece rotates which faces its end features occupy: a feature on
    faces [1, 3] at ``orientation_deg=90`` occupies [2, 4]. Only defined for a
    rectangular profile's 4 faces, which only rotate cleanly in 90-degree
    steps. A round tube's orientation is legitimately continuous (not
    rejected — see ``Part.orientation_deg``), but then there is no clean face
    to rotate onto, so the declared faces are used as-is rather than guessed
    at; in practice a round part has no faces declared at all.
    """
    if not feature.faces:
        return frozenset()
    steps = orientation_deg / 90.0
    if abs(steps - round(steps)) > 1e-6:
        return frozenset(feature.faces)
    shift = int(round(steps)) % 4
    return frozenset(((f - 1 + shift) % 4) + 1 for f in feature.faces)


def clearance(prev: Part, nxt: Part, kerf: float) -> Clearance:
    """The gap reserved between ``prev``'s far end and ``nxt``'s start end.

    ``prev`` must already be placed — this is never called for the first
    piece on a bar (see :func:`_append`, where that case reserves 0 up front
    and lets ``consumed_length``'s trailing kerf cover the eventual release
    cut). Implements the formula from docs/PLAN-orientacion-tubo.md §2::

        clearance(A->B) = kerf
                         + max over SHARED faces f of (protrusion_A[f] + protrusion_B[f])
                         + extra_gap requested by A

    "Shared" is evaluated AFTER each part's own ``orientation_deg`` is
    applied. A part's ``EndFeature`` carries one protrusion value for every
    face it names (a real tongue relieves the faces it's on uniformly), so the
    "max over shared faces" collapses to a single sum once any face is shared.

    With no end features and no ``extra_gap_mm`` this returns exactly
    ``kerf`` — the behaviour before this function existed.
    """
    prev_faces = _rotated_faces(prev.far_feature, prev.orientation_deg)
    nxt_faces = _rotated_faces(nxt.start_feature, nxt.orientation_deg)
    shared = prev_faces & nxt_faces

    reasons: List[str] = []
    face_term = 0.0
    if shared:
        face_term = prev.far_feature.protrusion_mm + nxt.start_feature.protrusion_mm
        reasons.append(GAP_SHARED_FACES)
    elif prev.far_feature.faces or nxt.start_feature.faces:
        reasons.append(GAP_INTERLEAVED)

    extra = max(prev.extra_gap_mm, 0.0)
    if extra > 0:
        reasons.append(GAP_EXTRA)

    return Clearance(kerf + face_term + extra, tuple(reasons))


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
        placed = False
        for bar in result.bars:
            # A bar in `result.bars` always has >= 1 placement (see the
            # assert below), so its last part is always defined here — the
            # gap this candidate would need is PAIR-dependent (§2), not the
            # flat kerf FFD used before orientation/end-features existed.
            gap = clearance(bar.placements[-1].part, part, spec.kerf).mm
            if bar.remnant + _EPS >= part.length + gap:
                _append(bar, part)
                placed = True
                break
        if placed:
            continue
        # A fresh bar: nothing precedes this piece there, so it only needs
        # its own length + kerf (the trailing reserve for its eventual
        # release cut — see BarLayout.consumed_length).
        need = part.length + spec.kerf
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
    """Place a part at the bar's current end.

    The first piece on a bar starts at 0 with no gap to report (front_trim
    already accounts for the bar's own start; nothing precedes this piece
    there). Otherwise the gap to the piece already at the bar's end is the
    real pair-dependent :func:`clearance` — kerf, plus any shared-face
    collision, plus the shop's requested extra gap.
    """
    if not bar.placements:
        bar.placements.append(Placement(part=part, start=0.0, end=part.length))
        return
    prev = bar.placements[-1]
    gap = clearance(prev.part, part, bar.spec.kerf)
    start = prev.end + gap.mm
    bar.placements.append(Placement(
        part=part, start=start, end=start + part.length,
        gap_before=gap.mm, gap_reason=gap.reasons))


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
