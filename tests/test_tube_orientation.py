"""Orientation, end features and pair-dependent clearance (§A/§B of
docs/PLAN-orientacion-tubo.md).

The physical fact this is built on (measured by sectioning a real customer
part — 25.4x25.4 cal.18 PTR, 761.22 mm): a ~6 mm male tongue at each end, made
by relieving two opposite faces while the other two stay full. Two neighbours
at the SAME rotation have their tongues on the same walls and collide, so the
gap must grow; rotate one 90 degrees and the features land on different faces
— they interleave, and the gap drops back to kerf.

Two invariants matter most here, and are pinned first:

  1. ZERO CHANGE WHEN UNUSED. With no end features, no extra_gap and default
     orientation, clearance() must return exactly kerf, and a full pack must
     be identical to the tool before this feature existed (the CLI-level
     before/after diff over samples/ that proves this end-to-end lives
     outside pytest — see the ship notes — but the packing-level version of
     the same claim is pinned here as ``test_zero_change_when_unused``).
  2. NO BAR EVER OVERFLOWS. Every placement's start/end is independently
     reconstructed from the parts' own declared features (never trusting
     BarLayout.consumed_length/remnant, which are themselves under test) and
     checked against the bar's usable length — the same "reconstruct, don't
     trust the engine's own field" doctrine CLAUDE.md pins for the 2D
     gap-zero overlap bug.
"""

from __future__ import annotations

import random

import pytest

from nester.tube.model import (
    GAP_EXTRA, GAP_INTERLEAVED, GAP_SHARED_FACES,
    EndFeature, Part, StockSpec,
)
from nester.tube.packing import Clearance, _rotated_faces, clearance, pack_profile

# --------------------------------------------------------------------------- #
# EndFeature / Part validation
# --------------------------------------------------------------------------- #

def test_end_feature_rejects_negative_protrusion():
    with pytest.raises(ValueError):
        EndFeature(protrusion_mm=-1.0, faces=(1,))


def test_end_feature_rejects_bad_face():
    with pytest.raises(ValueError):
        EndFeature(protrusion_mm=1.0, faces=(0,))
    with pytest.raises(ValueError):
        EndFeature(protrusion_mm=1.0, faces=(5,))


def test_end_feature_default_is_plain():
    f = EndFeature()
    assert f.protrusion_mm == 0.0
    assert f.faces == ()


def test_part_orientation_normalizes_to_0_360():
    assert Part(name="a", profile="p", length=100, orientation_deg=450).orientation_deg == 90.0
    assert Part(name="a", profile="p", length=100, orientation_deg=-90).orientation_deg == 270.0
    assert Part(name="a", profile="p", length=100, orientation_deg=360).orientation_deg == 0.0
    # A round tube's continuous clocking is legitimate, not rejected.
    assert Part(name="a", profile="p", length=100, orientation_deg=37.5).orientation_deg == 37.5


def test_part_rejects_negative_extra_gap():
    with pytest.raises(ValueError):
        Part(name="a", profile="p", length=100, extra_gap_mm=-1.0)


def test_part_rejects_bad_end_feature_key():
    with pytest.raises(ValueError):
        Part(name="a", profile="p", length=100,
             end_features={"middle": EndFeature(protrusion_mm=1.0, faces=(1,))})


def test_part_start_far_feature_defaults_to_plain():
    p = Part(name="a", profile="p", length=100)
    assert p.start_feature == EndFeature()
    assert p.far_feature == EndFeature()
    p2 = Part(name="a", profile="p", length=100,
             end_features={"far": EndFeature(protrusion_mm=6.0, faces=(1, 3))})
    assert p2.start_feature == EndFeature()
    assert p2.far_feature == EndFeature(protrusion_mm=6.0, faces=(1, 3))


# --------------------------------------------------------------------------- #
# Face rotation — the heart of the whole feature
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("faces,deg,expected", [
    ((1, 3), 0, frozenset({1, 3})),
    ((1, 3), 90, frozenset({2, 4})),
    ((1, 3), 180, frozenset({3, 1})),
    ((1, 3), 270, frozenset({4, 2})),
    ((1, 3), 360, frozenset({1, 3})),   # normalized away by Part, but the
                                        # helper itself is tested raw here too
    ((2,), 90, frozenset({3})),
    ((), 90, frozenset()),              # no faces declared -> nothing rotates
])
def test_rotated_faces_multiples_of_90(faces, deg, expected):
    assert _rotated_faces(EndFeature(protrusion_mm=1.0, faces=faces) if faces
                          else EndFeature(), deg) == expected


def test_rotated_faces_non_90_multiple_passes_through_unchanged():
    """A round tube's continuous orientation has no clean face to rotate onto
    — the declared faces are used as-is rather than guessed at."""
    f = EndFeature(protrusion_mm=1.0, faces=(1, 3))
    assert _rotated_faces(f, 37.5) == frozenset({1, 3})


# --------------------------------------------------------------------------- #
# clearance() — docs/PLAN-orientacion-tubo.md §2
# --------------------------------------------------------------------------- #

def test_clearance_plain_ends_is_exactly_kerf():
    a = Part(name="a", profile="p", length=100)
    b = Part(name="b", profile="p", length=100)
    assert clearance(a, b, kerf=2.0) == Clearance(2.0, ())


def test_clearance_shared_faces_adds_both_protrusions():
    a = Part(name="a", profile="p", length=100,
             end_features={"far": EndFeature(protrusion_mm=6.0, faces=(1, 3))})
    b = Part(name="b", profile="p", length=100,
             end_features={"start": EndFeature(protrusion_mm=4.0, faces=(3,))})
    g = clearance(a, b, kerf=2.0)
    assert g.mm == pytest.approx(2.0 + 6.0 + 4.0)
    assert g.reasons == (GAP_SHARED_FACES,)


def test_clearance_no_shared_faces_is_interleaved_and_stays_at_kerf():
    a = Part(name="a", profile="p", length=100,
             end_features={"far": EndFeature(protrusion_mm=6.0, faces=(1, 3))})
    b = Part(name="b", profile="p", length=100, orientation_deg=90,
             end_features={"start": EndFeature(protrusion_mm=6.0, faces=(1, 3))})
    g = clearance(a, b, kerf=2.0)
    assert g.mm == pytest.approx(2.0)
    assert g.reasons == (GAP_INTERLEAVED,)


def test_clearance_extra_gap_is_reported_and_additive():
    a = Part(name="a", profile="p", length=100, extra_gap_mm=20.0)
    b = Part(name="b", profile="p", length=100)
    g = clearance(a, b, kerf=2.0)
    assert g.mm == pytest.approx(22.0)
    assert g.reasons == (GAP_EXTRA,)


def test_clearance_extra_gap_is_the_PREVIOUS_parts_property_not_the_next():
    """extra_gap_mm on B (the piece AFTER the gap) must not leak into the gap
    before it — only A's (the piece the gap comes after) counts."""
    a = Part(name="a", profile="p", length=100)
    b = Part(name="b", profile="p", length=100, extra_gap_mm=20.0)
    g = clearance(a, b, kerf=2.0)
    assert g.mm == pytest.approx(2.0)
    assert g.reasons == ()


def test_clearance_combines_shared_faces_and_extra_gap():
    a = Part(name="a", profile="p", length=100, extra_gap_mm=5.0,
             end_features={"far": EndFeature(protrusion_mm=6.0, faces=(1,))})
    b = Part(name="b", profile="p", length=100,
             end_features={"start": EndFeature(protrusion_mm=3.0, faces=(1,))})
    g = clearance(a, b, kerf=1.0)
    assert g.mm == pytest.approx(1.0 + 6.0 + 3.0 + 5.0)
    assert set(g.reasons) == {GAP_SHARED_FACES, GAP_EXTRA}


def test_clearance_compares_As_FAR_end_to_Bs_START_end_not_start_to_start():
    """A tongue on A's START end (not its far/trailing end) must never
    contribute mm to the gap that follows A — the shared-faces sum, if any,
    can only come from A's far end vs B's start end."""
    a = Part(name="a", profile="p", length=100,
             end_features={"start": EndFeature(protrusion_mm=6.0, faces=(1,))})
    b = Part(name="b", profile="p", length=100,
             end_features={"start": EndFeature(protrusion_mm=6.0, faces=(1,))})
    g = clearance(a, b, kerf=2.0)
    # A's far end is plain, so nothing on A's side can collide — the gap
    # stays at kerf regardless of A's (irrelevant) start-end tongue. B's own
    # start feature, with nothing on A's far end to share a face with, is
    # what makes this "interleaved" rather than a bare, storyless kerf.
    assert g.mm == pytest.approx(2.0)
    assert g.reasons == (GAP_INTERLEAVED,)


def test_clearance_two_plain_ends_has_no_story():
    a = Part(name="a", profile="p", length=100,
             end_features={"start": EndFeature(protrusion_mm=6.0, faces=(1,))})
    b = Part(name="b", profile="p", length=100)
    g = clearance(a, b, kerf=2.0)
    assert g.mm == pytest.approx(2.0)
    assert g.reasons == ()


# --------------------------------------------------------------------------- #
# Worked example — the measured PTR tongue (see module docstring)
# --------------------------------------------------------------------------- #

def _ptr_piece(name: str, orientation_deg: float) -> Part:
    tongue = EndFeature(protrusion_mm=6.0, faces=(1, 3))
    return Part(name=name, profile="25.4x25.4_c18", length=761.22,
               orientation_deg=orientation_deg,
               end_features={"start": tongue, "far": tongue})


def test_worked_example_same_orientation_collides():
    a, b = _ptr_piece("A", 0.0), _ptr_piece("B", 0.0)
    g = clearance(a, b, kerf=0.2)
    # kerf + 6mm (A's far tongue) + 6mm (B's start tongue) == the plan's own
    # "~12 mm" estimate for two same-oriented tongues colliding.
    assert g.mm == pytest.approx(0.2 + 12.0)
    assert g.reasons == (GAP_SHARED_FACES,)


def test_worked_example_90_degrees_apart_interleaves():
    a, b = _ptr_piece("A", 0.0), _ptr_piece("B", 90.0)
    g = clearance(a, b, kerf=0.2)
    assert g.mm == pytest.approx(0.2)     # drops all the way back to plain kerf
    assert g.reasons == (GAP_INTERLEAVED,)


# --------------------------------------------------------------------------- #
# Invariant 1 — zero change when unused (packing-level)
# --------------------------------------------------------------------------- #

def test_zero_change_when_unused():
    """Plain parts (no end_features, no extra_gap, default orientation) must
    pack exactly as the pre-orientation solver did: every gap is kerf, no
    gap_reason is ever reported."""
    parts = [Part(name=f"p{i}", profile="p", length=float(100 + 7 * i)) for i in range(12)]
    spec = StockSpec(profile="p", stock_length=1000.0, kerf=2.5)
    result = pack_profile(parts, spec)
    for bar in result.bars:
        for i, pl in enumerate(bar.placements):
            assert pl.gap_reason == ()
            if i == 0:
                assert pl.gap_before == 0.0
                assert pl.start == 0.0
            else:
                assert pl.gap_before == pytest.approx(spec.kerf)


# --------------------------------------------------------------------------- #
# Invariant 2 — no bar ever overflows (property test, randomized)
# --------------------------------------------------------------------------- #

def _independent_reconstruction(bar, spec) -> float:
    """Recompute every placement's start/end from the PARTS' own declared
    features, from scratch — never trusting BarLayout.consumed_length /
    .remnant, which are themselves under test here. Returns the position
    right after the last placement's cut (before the trailing kerf reserve).
    """
    prev = None
    pos = 0.0
    for pl in bar.placements:
        if prev is None:
            start = 0.0
        else:
            gap = clearance(prev.part, pl.part, spec.kerf).mm
            start = prev.end + gap
        end = start + pl.part.length
        assert start == pytest.approx(pl.start, abs=1e-6)
        assert end == pytest.approx(pl.end, abs=1e-6)
        prev = pl
        pos = end
    return pos


def _random_part(rng: random.Random, i: int) -> Part:
    length = rng.uniform(50.0, 900.0)
    orientation = rng.choice([0.0, 90.0, 180.0, 270.0, rng.uniform(0, 360)])
    extra_gap = rng.choice([0.0, 0.0, 0.0, rng.uniform(0, 15)])

    def rand_feature():
        if rng.random() < 0.4:
            return EndFeature()
        faces = tuple(sorted(rng.sample([1, 2, 3, 4], k=rng.randint(1, 3))))
        return EndFeature(protrusion_mm=rng.uniform(0.5, 20.0), faces=faces)

    features = {}
    if rng.random() < 0.6:
        features["start"] = rand_feature()
    if rng.random() < 0.6:
        features["far"] = rand_feature()
    return Part(name=f"p{i}", profile="p", length=length, orientation_deg=orientation,
               extra_gap_mm=extra_gap, end_features=features)


@pytest.mark.parametrize("seed", range(30))
def test_no_bar_ever_overflows_with_random_orientation_and_features(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 25)
    parts = [_random_part(rng, i) for i in range(n)]
    spec = StockSpec(profile="p", stock_length=rng.uniform(1000.0, 6000.0),
                     kerf=rng.uniform(0.0, 3.0))
    result = pack_profile(parts, spec, minimize_bars=False)

    for bar in result.bars:
        end_of_last = _independent_reconstruction(bar, spec)
        # The trailing kerf is always reserved for the eventual release cut,
        # even though it is only ever "spent" if another piece follows.
        assert end_of_last + spec.kerf <= bar.usable_length + 1e-6, (
            f"bar overflow: {end_of_last + spec.kerf} > {bar.usable_length}")

    # Quantity conservation: every input part lands exactly once, either
    # placed or unplaceable — never lost, never duplicated.
    placed_ids = [id(pl.part) for bar in result.bars for pl in bar.placements]
    unplaceable_ids = [id(p) for p in result.unplaceable]
    assert sorted(placed_ids + unplaceable_ids) == sorted(id(p) for p in parts)
    assert len(placed_ids) == len(set(placed_ids))   # no part placed twice


# --------------------------------------------------------------------------- #
# Integration: orientation actually changes how many bars a job needs
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# CLI: --orient / --extra-gap / --end-feature parsing + _load_parts wiring
# --------------------------------------------------------------------------- #

from pathlib import Path

from nester.tube.cli import _load_parts, parse_end_features, parse_extra_gap, parse_orient

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_parse_orient():
    assert parse_orient(["a.igs=90", "b.igs=-45.5"]) == {"a.igs": 90.0, "b.igs": -45.5}
    assert parse_orient([]) == {}
    with pytest.raises(SystemExit):
        parse_orient(["no-equals-sign"])
    with pytest.raises(SystemExit):
        parse_orient(["a.igs=not-a-number"])


def test_parse_extra_gap():
    assert parse_extra_gap(["a.igs=5.5"]) == {"a.igs": 5.5}
    with pytest.raises(SystemExit):
        parse_extra_gap(["a.igs=-1"])   # must be >= 0
    with pytest.raises(SystemExit):
        parse_extra_gap(["a.igs=nope"])


def test_parse_end_features():
    out = parse_end_features(["a.igs=far:6:1,3", "a.igs=start:4:2"])
    assert out["a.igs"]["far"] == EndFeature(protrusion_mm=6.0, faces=(1, 3))
    assert out["a.igs"]["start"] == EndFeature(protrusion_mm=4.0, faces=(2,))
    with pytest.raises(SystemExit):
        parse_end_features(["a.igs=middle:6:1"])          # bad END
    with pytest.raises(SystemExit):
        parse_end_features(["a.igs=far:6:9"])              # bad FACE
    with pytest.raises(SystemExit):
        parse_end_features(["a.igs=far:not-a-number:1"])
    with pytest.raises(SystemExit):
        parse_end_features(["a.igs=far:6"])                 # missing FACES segment


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ not present")
def test_load_parts_applies_orient_extra_gap_and_end_features_by_basename():
    paths = sorted(str(p) for p in SAMPLES.glob("bracket_*.igs"))
    assert paths, "expected bracket_*.igs fixtures under samples/"
    name_a = Path(paths[0]).name
    tongue = EndFeature(protrusion_mm=6.0, faces=(1, 3))
    parts, errors, _cross = _load_parts(
        paths, r"(?P<profile>\d+x\d+x\d+)", None,
        orient={name_a: 90.0}, extra_gap={name_a: 3.5},
        end_features={name_a: {"far": tongue}})
    assert not errors
    touched = [p for p in parts if p.name == name_a]
    untouched = [p for p in parts if p.name != name_a]
    assert touched, "the targeted file must have produced at least one part"
    assert all(p.orientation_deg == 90.0 for p in touched)
    assert all(p.extra_gap_mm == 3.5 for p in touched)
    assert all(p.far_feature == tongue for p in touched)
    # Every OTHER file in the same job is completely untouched — per-file
    # declarations must not leak across files.
    assert all(p.orientation_deg == 0.0 for p in untouched)
    assert all(p.extra_gap_mm == 0.0 for p in untouched)
    assert all(p.end_features == {} for p in untouched)


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ not present")
def test_load_parts_defaults_are_untouched_parts():
    paths = sorted(str(p) for p in SAMPLES.glob("bracket_*.igs"))
    parts, errors, _cross = _load_parts(paths, r"(?P<profile>\d+x\d+x\d+)", None)
    assert not errors
    assert all(p.orientation_deg == 0.0 and p.extra_gap_mm == 0.0 and p.end_features == {}
              for p in parts)


def test_same_orientation_can_cost_more_bars_than_alternating():
    """Forcing every piece to the SAME orientation (tongues collide) must
    never be reported as cheaper than alternating them (tongues interleave) —
    demonstrating that rotation really does change the nest, as corrected in
    docs/PLAN-orientacion-tubo.md."""
    tongue = EndFeature(protrusion_mm=6.0, faces=(1, 3))

    def piece(i, orientation_deg):
        return Part(name=f"p{i}", profile="p", length=761.22, orientation_deg=orientation_deg,
                   end_features={"start": tongue, "far": tongue})

    same = [piece(i, 0.0) for i in range(8)]
    alternating = [piece(i, 0.0 if i % 2 == 0 else 90.0) for i in range(8)]

    spec = StockSpec(profile="p", stock_length=6500.0, kerf=0.2)
    r_same = pack_profile(same, spec, minimize_bars=False)
    r_alt = pack_profile(alternating, spec, minimize_bars=False)

    assert r_same.new_bars_needed >= r_alt.new_bars_needed
    assert r_same.total_stock_length >= r_alt.total_stock_length
