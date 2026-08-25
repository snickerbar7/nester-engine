"""Guards around the Rust nesting engine: pre-flight + panic containment.

Background — a real production job died in ~1s with

    PanicException: called `Result::unwrap()` on an `Err` value:
    Offset resulted in an empty polygon

from ``jagua-rs`` (via ``spyrrow``). Two independent failure families were
measured against spyrrow 0.9 / jagua-rs 0.7:

1. **Degenerate item geometry** — a contour with <3 distinct points or no area
   makes jagua-rs panic while *building* the instance ("Simple polygon has no
   area"). Per-part; the rest of the job is fine.
2. **Strip seed collapse** — jagua-rs seeds the strip rectangle at
   ``Σ(item area × demand) / strip_height`` and then offsets it inward by
   ``min_items_separation / 2`` per side. The solve survives iff
   ``Σarea / strip_height > separation``. A small job on a tall sheet (the real
   failure: three small parts, 915×2440 mm, gap 3) fails that test. Job-level,
   nothing to do with any single part being thin.

Notably NOT a cause: thin parts. The engine offsets items *outward*, which can
never empty a polygon — :func:`test_hairline_far_narrower_than_the_gap_still_nests`
pins that down so the wrong hypothesis is not re-introduced.
"""


import pytest
from shapely.geometry import Polygon

from nester.sheet.model import FlatPart, SheetSpec
from nester.sheet.pack import (
    NestError, _safe_strip_height, nest, transform, validate_part,
)


def rect(w, h):
    return ((0, 0), (w, 0), (w, h), (0, h))


def _placed_polys(sheet):
    return [Polygon(transform(pl.part.outer, pl.rotation, pl.x, pl.y))
            for pl in sheet.placements]


def _assert_valid(result, spec):
    """In bounds, inside the margin, non-overlapping, and the gap is honored."""
    for sheet in result.sheets:
        polys = _placed_polys(sheet)
        for poly in polys:
            x0, y0, x1, y1 = poly.bounds
            assert x0 >= spec.margin - 1e-6 and y0 >= spec.margin - 1e-6
            assert x1 <= spec.width - spec.margin + 1e-6
            assert y1 <= spec.height - spec.margin + 1e-6
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                assert polys[i].intersection(polys[j]).area < 1e-6
                assert polys[i].distance(polys[j]) >= spec.part_gap - 1e-3


# --------------------------------------------------------------------------- #
# Layer 1a — degenerate geometry never reaches the solver
# --------------------------------------------------------------------------- #

def test_collinear_zero_area_contour_is_rejected_by_name():
    bad = FlatPart("PLACA-COLINEAL.dxf", ((0, 0), (100, 0), (200, 0)), qty=1)
    good = FlatPart("PLACA-OK.dxf", rect(60, 40), qty=2)
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=3)

    result = nest([bad, good], spec, rotation="ortho", time_per_sheet=1, seed=0)

    assert [p.name for p, _ in result.invalid] == ["PLACA-COLINEAL.dxf"]
    assert "PLACA-COLINEAL.dxf" in result.messages[0]
    assert "no encierra área" in result.messages[0]
    # the rest of the job proceeded — nothing silently dropped
    assert sum(s.part_count for s in result.sheets) == 2
    _assert_valid(result, spec)


def test_self_touching_bowtie_is_rejected():
    """A bowtie's lobes cancel in the shoelace sum — jagua-rs calls it area-less."""
    bowtie = FlatPart("MONO.dxf", ((0, 0), (100, 100), (100, 0), (0, 100)), qty=1)
    assert validate_part(bowtie) is not None
    assert "cruza sobre sí mismo" in validate_part(bowtie)


def test_ring_with_fewer_than_three_distinct_points_is_rejected():
    """Duplicated vertices can satisfy FlatPart's >=3 check while being a line."""
    sliver = FlatPart("SLIVER.dxf", ((0, 0), (0, 0), (50, 0), (50, 0)), qty=1)
    reason = validate_part(sliver)
    assert reason is not None and "menos de 3 puntos distintos" in reason


def test_non_finite_coordinates_are_rejected():
    part = FlatPart("NAN.dxf", ((0, 0), (10, 0), (float("nan"), 10)), qty=1)
    reason = validate_part(part)
    assert reason is not None and "NaN" in reason


def test_a_healthy_part_passes_pre_flight():
    assert validate_part(FlatPart("OK.dxf", rect(10, 10))) is None


def test_all_parts_invalid_raises_a_clean_nest_error():
    parts = [
        FlatPart("A.dxf", ((0, 0), (10, 0), (20, 0)), qty=1),
        FlatPart("B.dxf", ((0, 0), (0, 0), (5, 0), (5, 0)), qty=1),
    ]
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=3)
    with pytest.raises(NestError) as e:
        nest(parts, spec, rotation="fixed", time_per_sheet=1, seed=0)
    assert "A.dxf" in str(e.value) and "B.dxf" in str(e.value)


def test_hairline_far_narrower_than_the_gap_still_nests():
    """Item separation offsets OUTWARD — a thin strip is not degenerate.

    Pins the corrected root cause: the production failure was never about the
    thin solera. 0.5 mm wide with a 3 mm gap must nest, not be excluded.
    """
    part = FlatPart("SOLERA-FINA.dxf", rect(180, 0.5), qty=40)
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=3)
    result = nest([part], spec, rotation="grain", time_per_sheet=1, seed=0)
    assert result.invalid == []
    assert sum(s.part_count for s in result.sheets) == 40
    _assert_valid(result, spec)


# --------------------------------------------------------------------------- #
# Layer 1b — the strip seed can never collapse (the real production failure)
# --------------------------------------------------------------------------- #

def test_strip_height_is_untouched_when_the_seed_is_already_safe():
    # Σarea / uh = 100 >> sep
    assert _safe_strip_height(items_area=100_000, uh=1000, sep=3, min_part_height=50) == 1000


def test_strip_height_is_shortened_when_the_seed_would_collapse():
    # Σarea / uh = 2.0 <= sep=3 -> jagua-rs would offset the strip to nothing
    h = _safe_strip_height(items_area=2000, uh=1000, sep=3, min_part_height=50)
    assert h is not None and h < 1000
    assert 2000 / h > 3  # the seed width now clears the separation


def test_strip_height_gives_up_when_no_height_can_host_the_parts():
    # a 2x2 part with a 3 mm gap: every safe height is shorter than the part
    assert _safe_strip_height(items_area=4, uh=1000, sep=3, min_part_height=2) is None


def test_separation_off_means_no_guard_at_all():
    assert _safe_strip_height(items_area=1, uh=1000, sep=0, min_part_height=5) == 1000


def test_small_job_on_a_tall_sheet_nests_instead_of_panicking():
    """Minimal synthetic reproduction of the production job that panicked.

    Three small parts (≈1.1k, ≈3.0k, ≈1.8k mm²) on a 915×2440 mm sheet with an
    8 mm margin and a 3 mm gap. Total area 5 962 mm² against
    ``gap × usable_height`` = 7 272 mm², so the jagua-rs strip seed
    (area/height ≈ 2.46 mm) is thinner than the 3 mm it gets offset by — the
    exact "Offset resulted in an empty polygon" condition.
    """
    parts = [
        FlatPart("LAMINA-A.dxf", rect(38, 30), qty=1),      # 1 140 mm²
        FlatPart("LAMINA-B.dxf", rect(80, 38), qty=1),      # 3 040 mm²
        FlatPart("SOLERA-C.dxf", rect(180, 10.16), qty=1),  # 1 829 mm²
    ]
    spec = SheetSpec(width=915, height=2440, margin=8, part_gap=3)

    total_area = sum(p.outer_area * p.qty for p in parts)
    assert total_area <= spec.part_gap * spec.usable_height, "repro no longer reproduces"

    result = nest(parts, spec, rotation="free", time_per_sheet=1, seed=0)

    assert result.invalid == [] and result.unplaceable == []
    assert result.sheet_count == 1
    assert sum(s.part_count for s in result.sheets) == 3
    _assert_valid(result, spec)


def test_a_single_tiny_part_on_a_tall_sheet_nests():
    """One 38×38 part on the production sheet — seed width 0.6 mm vs a 3 mm gap."""
    spec = SheetSpec(width=915, height=2440, margin=8, part_gap=3)
    result = nest([FlatPart("UNA.dxf", rect(38, 38))], spec,
                  rotation="free", time_per_sheet=1, seed=0)
    assert sum(s.part_count for s in result.sheets) == 1
    _assert_valid(result, spec)


def test_parts_smaller_than_the_gap_fall_back_to_the_shelf_packer():
    """No safe strip height exists here, so the engine is skipped entirely."""
    parts = [FlatPart("MICRO-A.dxf", rect(2, 2)), FlatPart("MICRO-B.dxf", rect(2, 2))]
    spec = SheetSpec(width=100, height=100, margin=5, part_gap=3)
    assert _safe_strip_height(8.0, spec.usable_height, 3.0, 2.0) is None

    result = nest(parts, spec, rotation="free", time_per_sheet=1, seed=0)
    assert sum(s.part_count for s in result.sheets) == 2
    _assert_valid(result, spec)


# --------------------------------------------------------------------------- #
# Layer 2 — a Rust panic can never escape as a raw unwrap string
# --------------------------------------------------------------------------- #

class _FakePanic(BaseException):
    """Stand-in for pyo3_runtime.PanicException — a BaseException, by design."""


_FakePanic.__name__ = "PanicException"


def _explode(exc):
    class _Instance:
        def __init__(self, *_a, **_k):
            pass

        def solve(self, _config):
            raise exc

    return _Instance


def test_engine_panic_becomes_a_nest_error_naming_the_parameters(monkeypatch):
    import spyrrow
    boom = _FakePanic("called `Result::unwrap()` on an `Err` value: "
                      "Offset resulted in an empty polygon")
    monkeypatch.setattr(spyrrow, "StripPackingInstance", _explode(boom))

    spec = SheetSpec(width=915, height=2440, margin=8, part_gap=3)
    with pytest.raises(NestError) as e:
        nest([FlatPart("X.dxf", rect(200, 200), qty=4)], spec,
             rotation="free", time_per_sheet=1, seed=0)

    msg = str(e.value)
    assert "pánico interno del motor" in msg
    assert "Offset resulted in an empty polygon" in msg  # the cause is preserved
    assert "915×2440" in msg and "margen 8" in msg and "separación 3" in msg
    assert "rotación 'free'" in msg
    assert isinstance(e.value.__cause__, BaseException)


def test_plain_engine_exception_also_becomes_a_nest_error(monkeypatch):
    import spyrrow
    monkeypatch.setattr(spyrrow, "StripPackingInstance",
                        _explode(ValueError("bad instance")))
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=3)
    with pytest.raises(NestError) as e:
        nest([FlatPart("X.dxf", rect(50, 50), qty=4)], spec,
             rotation="free", time_per_sheet=1, seed=0)
    assert "error del motor" in str(e.value) and "bad instance" in str(e.value)


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_control_flow_exceptions_are_not_swallowed(monkeypatch, control):
    import spyrrow
    monkeypatch.setattr(spyrrow, "StripPackingInstance", _explode(control()))
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=3)
    with pytest.raises(control):
        nest([FlatPart("X.dxf", rect(50, 50), qty=4)], spec,
             rotation="free", time_per_sheet=1, seed=0)
