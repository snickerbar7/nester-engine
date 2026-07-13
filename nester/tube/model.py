"""Core data structures for tube nesting.

All lengths are in the same unit (mm by convention). The IGES parser decides
the unit it emits; the rest of the pipeline is unit-agnostic as long as inputs
are consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


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
class StockSpec:
    """Stock bar definition + the workshop cutting allowances for one profile."""

    profile: str
    stock_length: float   # full purchased bar length
    kerf: float = 0.0      # material consumed by each saw cut
    front_trim: float = 0.0  # dead zone at clamp/loading end (unusable)
    back_trim: float = 0.0   # dead zone / required remnant at the far end

    @property
    def usable_length(self) -> float:
        u = self.stock_length - self.front_trim - self.back_trim
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
    """One physical stock bar and everything cut from it."""

    index: int
    spec: StockSpec
    placements: List[Placement] = field(default_factory=list)

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
        return self.spec.usable_length - self.consumed_length

    @property
    def utilization(self) -> float:
        """Fraction of the *full* bar turned into parts."""
        return self.used_length / self.spec.stock_length if self.spec.stock_length else 0.0


@dataclass
class ProfileResult:
    """Nesting result for a single profile group."""

    profile: str
    spec: StockSpec
    bars: List[BarLayout] = field(default_factory=list)
    unplaceable: List[Part] = field(default_factory=list)  # parts longer than usable length

    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def total_part_length(self) -> float:
        return sum(b.used_length for b in self.bars)

    @property
    def total_stock_length(self) -> float:
        return self.bar_count * self.spec.stock_length

    @property
    def yield_pct(self) -> float:
        if not self.total_stock_length:
            return 0.0
        return 100.0 * self.total_part_length / self.total_stock_length
