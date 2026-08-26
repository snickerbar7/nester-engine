"""Core data structures for tube nesting.

All lengths are in the same unit (mm by convention). The IGES parser decides
the unit it emits; the rest of the pipeline is unit-agnostic as long as inputs
are consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

NEW_BAR = "nuevo"   # BarLayout.source for a full purchased tramo

# Why a remnant (retazo) offered to the job was never opened. Reported per
# piece, mirroring the 2D reasons in ``nester.sheet.model``, so the shop can
# tell "nothing fitted it" from "spending it would have bought nothing".
REMNANT_NO_FIT = "no_fit"                     # nothing in the job fits it
REMNANT_TOO_SMALL = "too_small_for_trims"     # shorter than front+back trim
REMNANT_JOB_ENDED = "job_ended"               # the parts ran out first
# Declined on purpose: opening it would not have removed a tramo from the
# purchase order, so spending it would have cost a physical offcut for nothing.
# See ``nester.tube.packing.pack_profile``.
REMNANT_NO_GAIN = "no_gain"


@dataclass(frozen=True)
class Part:
    """A single cut piece to be produced."""

    name: str          # source identifier (e.g. IGES filename)
    profile: str       # profile/cross-section key used for grouping
    length: float      # required cut length

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(f"{self.name}: part length must be > 0, got {self.length}")


@dataclass(frozen=True)
class ExtraStock:
    """A leftover piece of bar (retazo) offered to the nest as extra stock.

    ``label`` is the shop's identifier for that physical piece ("R-0001") — it
    is what the cut plan tells the operator to pull off the rack, so it must be
    unique within a profile. Each piece can be used at most once.
    """

    length: float
    label: str

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(f"{self.label}: remnant length must be > 0, got {self.length}")
        if not self.label.strip():
            raise ValueError("remnant label must not be empty")


@dataclass(frozen=True)
class StockSpec:
    """Stock definition + the workshop cutting allowances for one profile.

    ``stock_length`` is the full purchased bar (tramo) — an unlimited supply.
    ``extra_stock`` is the finite pool of remnants the solver may consume
    instead of buying a new tramo; it is stored as a tuple so the spec stays
    hashable, but any iterable of :class:`ExtraStock` may be passed in.

    ``min_remnant`` is the shortest drop worth putting back on the rack (mm).
    It is a *reporting* threshold, not a cutting allowance: at or above it a
    bar's drop is a recoverable SOBRANTE, below it MERMA. It is deliberately
    NOT ``back_trim`` — the back trim is the chuck dead zone the machine cannot
    reach, which is a different thing. 0 (the default) means the tool does not
    classify the drop at all, and the net and gross yields coincide.
    """

    profile: str
    stock_length: float   # full purchased bar length
    kerf: float = 0.0      # material consumed by each saw cut
    front_trim: float = 0.0  # dead zone at clamp/loading end (unusable)
    back_trim: float = 0.0   # dead zone / required remnant at the far end
    extra_stock: Tuple[ExtraStock, ...] = ()   # remnants on the rack
    min_remnant: float = 0.0   # shortest drop worth keeping (0 = don't classify)

    def __post_init__(self) -> None:
        if not isinstance(self.extra_stock, tuple):
            object.__setattr__(self, "extra_stock", tuple(self.extra_stock))

    def usable_for(self, length: float) -> float:
        """Usable region of a bar of ``length`` (tramo or remnant alike).

        May be <= 0 for a remnant shorter than the trims — callers that offer
        such a piece must skip it; see :attr:`usable_length` for the raising
        variant used on the full tramo.
        """
        return length - self.front_trim - self.back_trim

    @property
    def usable_length(self) -> float:
        u = self.usable_for(self.stock_length)
        if u <= 0:
            raise ValueError(
                f"{self.profile}: usable length <= 0 "
                f"(stock={self.stock_length}, front={self.front_trim}, back={self.back_trim})"
            )
        return u


@dataclass
class Placement:
    """One part placed on a bar, with its cut window measured from the usable start."""

    part: Part
    start: float   # position where this part begins (after front_trim, from bar zero)
    end: float     # position where this part ends (cut line for the *next* kerf)


@dataclass
class BarLayout:
    """One physical bar — a new tramo or a remnant — and everything cut from it."""

    index: int
    spec: StockSpec
    placements: List[Placement] = field(default_factory=list)
    stock_length: float | None = None   # this bar's own length; None -> full tramo
    source: str = NEW_BAR               # NEW_BAR, or the remnant's label

    def __post_init__(self) -> None:
        if self.stock_length is None:
            self.stock_length = self.spec.stock_length

    @property
    def is_remnant(self) -> bool:
        return self.source != NEW_BAR

    @property
    def usable_length(self) -> float:
        """Usable region of THIS bar (its own length minus the trims)."""
        return self.spec.usable_for(self.stock_length)

    @property
    def used_length(self) -> float:
        """Sum of part lengths only (excludes kerf and trims)."""
        return sum(p.part.length for p in self.placements)

    @property
    def consumed_length(self) -> float:
        """Length consumed from the usable region, including kerf per part."""
        return sum(p.part.length + self.spec.kerf for p in self.placements)

    @property
    def remnant(self) -> float:
        """Usable material left over (the drop)."""
        return self.usable_length - self.consumed_length

    @property
    def leftover(self) -> float:
        """The part of the drop worth putting back on the rack (SOBRANTE, mm).

        0 when the job set no ``min_remnant`` or the drop is under it — that
        piece is MERMA, and the shop is not going to store it.
        """
        drop = self.remnant
        m = self.spec.min_remnant
        return drop if (m > 0 and drop + 1e-9 >= m) else 0.0

    @property
    def waste(self) -> float:
        """The part of the drop nobody keeps (MERMA, mm)."""
        return max(self.remnant - self.leftover, 0.0)

    @property
    def utilization(self) -> float:
        """Fraction of *this* bar turned into parts."""
        return self.used_length / self.stock_length if self.stock_length else 0.0


@dataclass
class ProfileResult:
    """Nesting result for a single profile group."""

    profile: str
    spec: StockSpec
    bars: List[BarLayout] = field(default_factory=list)
    unplaceable: List[Part] = field(default_factory=list)  # parts longer than usable length
    # Remnants offered but never opened — nothing fitted them, the job ended
    # first, or opening them would not have removed a tramo (``no_gain``).
    # Reported so the rack is never silently ignored and the piece stays
    # available for the next job.
    remnants_unused: List[ExtraStock] = field(default_factory=list)
    # {label: one of REMNANT_*} — WHY each unused remnant was never opened.
    remnant_reasons: Dict[str, str] = field(default_factory=dict)

    @property
    def bar_count(self) -> int:
        """TOTAL bars used — new tramos plus remnants."""
        return len(self.bars)

    @property
    def new_bars_needed(self) -> int:
        """Full tramos the shop has to BUY (remnants are already on the rack)."""
        return sum(1 for b in self.bars if not b.is_remnant)

    @property
    def remnants_used(self) -> List[str]:
        """Labels of the remnants consumed, in bar order."""
        return [b.source for b in self.bars if b.is_remnant]

    @property
    def total_part_length(self) -> float:
        return sum(b.used_length for b in self.bars)

    @property
    def total_stock_length(self) -> float:
        """Sum of the ACTUAL bar lengths used (tramos and remnants mixed)."""
        return sum(b.stock_length for b in self.bars)

    @property
    def new_stock_length(self) -> float:
        """Sum of the tramos actually BOUGHT — what procurement pays for."""
        return sum(b.stock_length for b in self.bars if not b.is_remnant)

    @property
    def yield_pct(self) -> float:
        if not self.total_stock_length:
            return 0.0
        return 100.0 * self.total_part_length / self.total_stock_length

    # ----------------------------------------------------------------- #
    # Net vs gross
    # ----------------------------------------------------------------- #
    # ``yield_pct`` above is the GROSS number and keeps its name, meaning and
    # value forever — Harriet's frozen /nest reads it. The net number below
    # discounts the drop that goes BACK on the rack, exactly as the 2D tool
    # discounts a reclaimable offcut. Without it, a shop that feeds the job its
    # own retazos ADDS to the denominator and watches the headline yield fall:
    # the product punishing a shop for taking its own advice.

    @property
    def total_drop_length(self) -> float:
        """Everything left over on the usable regions (SOBRANTE + MERMA, mm)."""
        return sum(b.remnant for b in self.bars)

    @property
    def reclaimable_length(self) -> float:
        """Drop that goes back on the rack as a usable SOBRANTE (mm)."""
        return sum(b.leftover for b in self.bars)

    @property
    def reclaimable(self) -> List[Tuple[int, float]]:
        """(bar number, length) for every bar leaving a keepable sobrante."""
        return [(b.index + 1, b.leftover) for b in self.bars if b.leftover > 0]

    @property
    def consumed_length(self) -> float:
        """Stock opened MINUS what goes back to the rack (mm).

        (Not to be confused with :attr:`BarLayout.consumed_length`, which is one
        bar's kerf-inclusive fill.)
        """
        return max(self.total_stock_length - self.reclaimable_length, 0.0)

    @property
    def waste_length(self) -> float:
        """Material that neither leaves as a part nor returns to the rack (mm).

        Kerf + dead zones + the drop too short to keep.
        """
        return max(self.total_stock_length - self.total_part_length
                   - self.reclaimable_length, 0.0)

    @property
    def net_yield_pct(self) -> float:
        """The honest headline: parts over the material actually consumed."""
        consumed = self.consumed_length
        if not consumed:
            return 0.0
        return 100.0 * self.total_part_length / consumed

    @property
    def gross_yield_pct(self) -> float:
        """Parts over every mm opened. Same value as ``yield_pct``."""
        return self.yield_pct
