"""Core data structures for tube nesting.

All lengths are in the same unit (mm by convention). The IGES parser decides
the unit it emits; the rest of the pipeline is unit-agnostic as long as inputs
are consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

NEW_BAR = "nuevo"   # BarLayout.source for a full purchased tramo


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
    ``extra_stock`` is the finite pool of remnants to consume BEFORE buying a
    new tramo; it is stored as a tuple so the spec stays hashable, but any
    iterable of :class:`ExtraStock` may be passed in.
    """

    profile: str
    stock_length: float   # full purchased bar length
    kerf: float = 0.0      # material consumed by each saw cut
    front_trim: float = 0.0  # dead zone at clamp/loading end (unusable)
    back_trim: float = 0.0   # dead zone / required remnant at the far end
    extra_stock: Tuple[ExtraStock, ...] = ()   # remnants on the rack

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
    def yield_pct(self) -> float:
        if not self.total_stock_length:
            return 0.0
        return 100.0 * self.total_part_length / self.total_stock_length
