"""Core data structures for tube nesting.

All lengths are in the same unit (mm by convention). The IGES parser decides
the unit it emits; the rest of the pipeline is unit-agnostic as long as inputs
are consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

NEW_BAR = "nuevo"   # BarLayout.source for a full purchased tramo

# Rectangular-profile faces, numbered 1..4 around the cross-section in the
# part's OWN (un-rotated) reference frame. Part.orientation_deg is what maps
# them onto the tube as actually clocked — see nester.tube.packing.clearance.
_VALID_FACES = (1, 2, 3, 4)
_VALID_ENDS = ("start", "far")

# Why a gap between two pieces is what it is (docs/PLAN-orientacion-tubo.md
# §2) — reported on Placement.gap_reason so the plan never prints a bare
# number for a hueco wider or narrower than plain kerf.
GAP_INTERLEAVED = "interleaved"       # end features present, no shared faces -> gap shrinks to kerf
GAP_SHARED_FACES = "shared_faces"     # protrusions collide -> gap grows to clear them
GAP_EXTRA = "extra"                   # the shop asked for extra room after the previous piece

# Foreman-register Spanish for each reason — a STATE label about how the two
# neighbouring ends are clocked (Placement.gap_reason), never a claim about
# what was charged. "Caras compartidas" only means the faces line up; whether
# that costs anything depends on StockSpec.shared_face_penalty_mm, which
# defaults to 0 (nester.tube.packing.clearance) — so this label must never be
# read as "this joint reserved extra mm to clear a collision".
GAP_REASON_ES: Dict[str, str] = {
    GAP_INTERLEAVED: "entrelazadas: las lengüetas no comparten cara",
    GAP_SHARED_FACES: "caras compartidas: mismas caras que la pieza anterior",
    GAP_EXTRA: "separación extra pedida por el taller",
}

# Spanish label for each ITEMIZED gap charge (Placement.gap_terms / the CLI's
# --json "gap_story"). Unlike GAP_REASON_ES above, every entry here is a real
# millimetre figure that was actually reserved: "kerf" is the one term that is
# (almost) always present; "shared_face_penalty" and "extra" only ever appear
# when their mm is > 0. This is deliberately a SEPARATE vocabulary from
# GAP_REASON_ES — a joint can be *labelled* "shared_faces" while charging
# nothing for it (the default, unmeasured penalty is 0), and gap_story_es must
# never say "caras compartidas" next to a number that was never reserved.
GAP_TERM_ES: Dict[str, str] = {
    "kerf": "ranura de corte (kerf)",
    "shared_face_penalty": "caras compartidas",
    "extra": "extra del taller",
}


def gap_story_es(terms: Tuple[Tuple[str, float], ...]) -> str:
    """A gap's itemized mm charges joined into one phrase.

    ``terms`` is the ordered ``(label_key, mm)`` sequence on
    ``Placement.gap_terms`` — kerf first (the baseline), then whichever of
    ``shared_face_penalty`` / ``extra`` actually added millimetres (see
    ``nester.tube.packing._append``). '' when there is nothing beyond plain
    kerf to explain — the common case, and every ordinary cut stays silent
    exactly as it did before this feature existed. In particular, a joint
    whose faces merely share (``StockSpec.shared_face_penalty_mm`` at its
    default of 0) or merely interleave produces '' here: the state is still
    visible on ``Placement.gap_reason`` / ``GAP_REASON_ES``, it just never
    earns a millimetre line it didn't cost.
    """
    if len(terms) <= 1:
        return ""
    parts = []
    for i, (key, mm) in enumerate(terms):
        label = GAP_TERM_ES.get(key, key)
        sign = "" if i == 0 else "+"
        parts.append(f"{label} {sign}{mm:g} mm")
    return " · ".join(parts)


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
class EndFeature:
    """A protrusion (e.g. a welded tongue) at one end of a part, on given faces.

    ``faces`` are the rectangular-profile faces (1..4) it occupies in the
    part's OWN reference frame, BEFORE ``Part.orientation_deg`` is applied.
    Empty ``faces`` (the default) means a plain, flat end — nothing protrudes,
    and it never contributes to a neighbour's clearance.
    """

    protrusion_mm: float = 0.0
    faces: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.faces, tuple):
            object.__setattr__(self, "faces", tuple(self.faces))
        if self.protrusion_mm < 0:
            raise ValueError(f"protrusion_mm must be >= 0, got {self.protrusion_mm}")
        bad = [f for f in self.faces if f not in _VALID_FACES]
        if bad:
            raise ValueError(f"face(s) must be 1..4, got {bad}")


@dataclass(frozen=True)
class Part:
    """A single cut piece to be produced."""

    name: str          # source identifier (e.g. IGES filename)
    profile: str       # profile/cross-section key used for grouping
    length: float      # required cut length

    # How the shop clocks this piece in the tube (docs/PLAN-orientacion-tubo.md).
    # 0.0 = as it came from CAD. Rectangular profiles use 0/90/180/270 in
    # practice; a round tube's orientation is continuous, so a non-multiple of
    # 90 is NOT rejected — it just has no meaningful face to rotate onto.
    orientation_deg: float = 0.0
    # Extra space the shop demands AFTER this piece, on top of kerf/features.
    extra_gap_mm: float = 0.0
    # Per end ("start"/"far") protrusion feature. Empty/absent = a plain,
    # square end — and then this piece behaves exactly as it did before these
    # fields existed (see nester.tube.packing.clearance).
    end_features: Dict[str, EndFeature] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.length <= 0:
            raise ValueError(f"{self.name}: part length must be > 0, got {self.length}")
        object.__setattr__(self, "orientation_deg", self.orientation_deg % 360.0)
        if self.extra_gap_mm < 0:
            raise ValueError(f"{self.name}: extra_gap_mm must be >= 0, got {self.extra_gap_mm}")
        bad_ends = set(self.end_features) - set(_VALID_ENDS)
        if bad_ends:
            raise ValueError(
                f"{self.name}: end_features key(s) must be 'start'/'far', got {sorted(bad_ends)}")

    @property
    def start_feature(self) -> EndFeature:
        return self.end_features.get("start", EndFeature())

    @property
    def far_feature(self) -> EndFeature:
        return self.end_features.get("far", EndFeature())


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

    ``shared_face_penalty_mm`` is the extra clearance to reserve, on top of
    kerf, between two neighbours whose end features land on the SAME face
    after ``Part.orientation_deg`` is applied (docs/PLAN-orientacion-tubo.md
    §2). It defaults to 0 DELIBERATELY: no one has measured how much room two
    same-oriented tongues actually need, and this round found the engine had
    invented a number (summing the two protrusions) without ever validating it
    against a real part — a second, independent guess (12.0 mm, from a design
    round) did not even agree with the engine's own estimate, which is the
    signal that neither was grounded. This is a MACHINE allowance, so it lives
    here beside ``kerf``/``front_trim``/``back_trim`` — not on the part —
    exactly like those, if the real cost is ever measured it is a single
    number dropped in here, not a per-part fudge.
    """

    profile: str
    stock_length: float   # full purchased bar length
    kerf: float = 0.0      # material consumed by each saw cut
    front_trim: float = 0.0  # dead zone at clamp/loading end (unusable)
    back_trim: float = 0.0   # dead zone / required remnant at the far end
    extra_stock: Tuple[ExtraStock, ...] = ()   # remnants on the rack
    min_remnant: float = 0.0   # shortest drop worth keeping (0 = don't classify)
    # Shop-set clearance for same-face end features; 0 = not measured, not
    # charged (see the docstring above). See nester.tube.packing.clearance.
    shared_face_penalty_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.extra_stock, tuple):
            object.__setattr__(self, "extra_stock", tuple(self.extra_stock))
        if self.shared_face_penalty_mm < 0:
            raise ValueError(
                f"{self.profile}: shared_face_penalty_mm must be >= 0, "
                f"got {self.shared_face_penalty_mm}")

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
    # The gap actually reserved BEFORE this piece, between the previous
    # piece's far end and this piece's start end. 0.0 for the first piece on a
    # bar — nothing precedes it there but front_trim, which is outside the
    # usable region and not this field's concern.
    gap_before: float = 0.0
    # Why gap_before is what it is: () for the first piece on a bar; otherwise
    # any combination of GAP_INTERLEAVED / GAP_SHARED_FACES / GAP_EXTRA. See
    # nester.tube.packing.clearance. A STATE label, not a charge — see
    # GAP_REASON_ES vs GAP_TERM_ES above.
    gap_reason: Tuple[str, ...] = ()
    # gap_before broken into what it actually charges: (("kerf", mm), ...) plus
    # "shared_face_penalty" / "extra" whenever they contributed real mm. () for
    # the first piece on a bar. Feeds gap_story_es(); see
    # nester.tube.packing._append.
    gap_terms: Tuple[Tuple[str, float], ...] = ()


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
        """Length consumed from the usable region, including the trailing kerf
        reserved to release the last piece from the remaining stock.

        Each :class:`Placement` already carries its true, pair-dependent
        ``start``/``end`` (set by :func:`nester.tube.packing._append`), so
        this is simply "where the last cut ends" plus one more kerf — not a
        fresh sum over uniform per-part reservations. With no end features and
        no ``extra_gap_mm`` on any part, every gap equals ``kerf`` and this is
        numerically IDENTICAL to the old ``sum(length + kerf for ...)``.
        """
        if not self.placements:
            return 0.0
        return self.placements[-1].end + self.spec.kerf

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
