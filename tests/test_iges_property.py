"""Property tests for the IGES reader (E25): known-or-loud.

The through-line of this round is a single bug class — *a reader that invents
plausible geometry instead of refusing*. These three sweeps pin the three places
the IGES reader used to do that:

  P4  the unit table (an unmapped flag used to mean millimetres)
  P5  the Global section (a comma inside a Hollerith string used to shift every
      field, land the unit flag on a float, and fall back to millimetres)
  P6  entity coverage (geometry in an unhandled entity type used to be dropped
      silently whenever ANY other entity yielded a point)

``hypothesis`` is not a dependency of this repo, so these are seeded
deterministic sweeps rather than generative property tests — same coverage, no
new dependency, and reproducible failures.
"""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_sample_iges import build_lines  # noqa: E402

from nester.tube.iges import IgesParseError, _parse  # noqa: E402


def _read(text: str, path: str = "fuzz.igs"):
    return _parse(text.split("\n"), path)


# --------------------------------------------------------------------------- #
# P4 — unit table completeness
#
# SPEC_FACTOR is written from the IGES 5.3 spec (Global parameter 14), NOT
# imported from nester.tube.iges._UNIT_TO_MM. A test that imports the table it
# is checking only proves the table agrees with itself.
# --------------------------------------------------------------------------- #

SPEC_FACTOR = {
    1: 25.4,          # inches
    2: 1.0,           # millimetres
    # 3 = "unit named in Global parameter 15" — no factor of its own
    4: 304.8,         # feet
    5: 1609344.0,     # miles       (5280 ft * 304.8)
    6: 1000.0,        # metres
    7: 1000000.0,     # kilometres
    8: 0.0254,        # mils        (0.001 inch)
    9: 0.001,         # microns
    10: 10.0,         # centimetres
    11: 0.0000254,    # microinches (1e-6 inch)
}


def _lengths(n=6, seed=4):
    rnd = random.Random(seed)
    return [10 ** rnd.uniform(-3, 6) for _ in range(n)]


@pytest.mark.parametrize("flag", sorted(SPEC_FACTOR))
def test_p4_unit_flag_scales_per_spec(flag):
    for L in _lengths():
        geo = _read(build_lines([((0.0, 0.0, 0.0), (L, 0.0, 0.0))], unit_flag=flag))
        assert geo.unit_flag == flag
        assert math.isclose(geo.cut_length, L * SPEC_FACTOR[flag], rel_tol=1e-9), (
            f"flag {flag}: {geo.cut_length} != {L} * {SPEC_FACTOR[flag]}")


@pytest.mark.parametrize("flag", [0, 12, 13, 99, -1, 255])
def test_p4_unknown_unit_flag_raises_naming_it(flag):
    with pytest.raises(IgesParseError) as e:
        _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))], unit_flag=flag))
    assert str(flag) in str(e.value)
    # And it must refuse rather than quietly meaning millimetres.
    assert "millimetres" in str(e.value) or "millimeters" in str(e.value)


def test_p4_missing_unit_flag_raises():
    text = build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))], unit_flag="")
    with pytest.raises(IgesParseError) as e:
        _read(text)
    assert "unit flag" in str(e.value)


@pytest.mark.parametrize("name,factor", [
    ("MM", 1.0), ("IN", 25.4), ("FT", 304.8), ("M", 1000.0),
    ("CM", 10.0), ("MIL", 0.0254), ("UIN", 0.0000254),
])
def test_p4_flag3_resolves_the_named_unit(name, factor):
    """Flag 3 = 'the unit is NAMED in Global parameter 15'. Resolve the name."""
    geo = _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))],
                            unit_flag=3, unit_name=name))
    assert math.isclose(geo.cut_length, 100.0 * factor, rel_tol=1e-12)


def test_p4_flag3_unknown_name_raises_naming_it():
    with pytest.raises(IgesParseError) as e:
        _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))],
                          unit_flag=3, unit_name="SMOOTS"))
    assert "SMOOTS" in str(e.value)


# --------------------------------------------------------------------------- #
# P5 — Global-section fuzz
#
# IGES strings are COUNTED (nH + n bytes), not quoted, so a sender product id
# like "ACME, S.A. de C.V." legitimately contains the field delimiter. Splitting
# on the delimiter shifted every later field: the unit flag landed on a float,
# int() raised, and the reader silently fell back to millimetres — 25.4x short
# on every cut of a perfectly valid file.
# --------------------------------------------------------------------------- #

_ALPHABET = list(",;H0123456789 ñéABCdef.-/")


def _rand_hollerith(rnd, max_len=24):
    return "".join(rnd.choice(_ALPHABET) for _ in range(rnd.randint(0, max_len)))


@pytest.mark.parametrize("seed", range(40))
def test_p5_global_fuzz_unit_flag_survives_hollerith_strings(seed):
    rnd = random.Random(seed)
    flag = rnd.choice(sorted(SPEC_FACTOR))
    # The Hollerith string slots of the Global section: product id, file name,
    # native system id, preprocessor version, author, organisation.
    strings = {i: _rand_hollerith(rnd) for i in (3, 4, 5, 6, 21, 22)}
    L = 137.5
    text = build_lines([((0.0, 0.0, 0.0), (L, 0.0, 0.0))],
                       unit_flag=flag, global_strings=strings)
    geo = _read(text)
    assert geo.unit_flag == flag, f"strings={strings!r}"
    assert math.isclose(geo.cut_length, L * SPEC_FACTOR[flag], rel_tol=1e-9)


@pytest.mark.parametrize("product_id", [
    "ACME, S.A. de C.V.",
    "Fusion 360; build 2.0.21",
    "12H not a hollerith",
    "ñoño, é, HHH",
    "",
])
def test_p5_delimiters_inside_a_string_do_not_shift_the_fields(product_id):
    geo = _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))],
                            unit_flag=1, product_id=product_id))
    assert geo.unit_flag == 1
    assert math.isclose(geo.cut_length, 2540.0, rel_tol=1e-12)


def test_p5_custom_delimiters_are_followed():
    """Global parameters 1/2 may redefine the delimiters; the parse must follow
    them, and a comma inside a string must then be ordinary text."""
    from nester.tube.iges import _parse_global

    pid = "ACME, S.A. de C.V."
    fields = [""] * 24
    fields[2] = f"{len(pid)}H{pid}"
    fields[12] = "1.0"
    fields[13] = "1"
    fields[14] = "2HIN"
    body = "1H:" + ":" + "1H#" + ":" + ":".join(fields[2:]) + "#"
    glines = [f"{body[i:i + 72]:<72}G{n:>7}" for n, i in enumerate(range(0, len(body), 72), 1)]
    pdelim, rdelim, flag, name = _parse_global(glines, "custom.igs")
    assert (pdelim, rdelim, flag, name) == (":", "#", 1, "IN")


# --------------------------------------------------------------------------- #
# P6 — an unrecognized entity is never silent
#
# The old guard only fired when NO entity yielded a point, so a 3 m run inside a
# type-106 Copious Data entity plus a 100 mm detail Line reported cut_length =
# 100.0 with no warning at all.
# --------------------------------------------------------------------------- #

# Entity types that carry model coordinates in their own Parameter Data and that
# the reader does not read. Split by what the reader is expected to do.
_REFUSE_TYPES = [104, 106, 112, 114]     # PD is literally a coordinate list
_NOTE_TYPES = [132, 125, 138]            # unclassified: must at least be named


def _pd_with_extent(etype: int, x: float) -> str:
    """A plausible PD blob for `etype` whose coordinates reach out to x."""
    return f"{etype},1,2,0.,0.,0.,{x:.12g},0.,0."


@pytest.mark.parametrize("seed", range(12))
def test_p6_unhandled_entity_types_are_never_silent(seed):
    rnd = random.Random(seed)
    n_recognized = rnd.randint(1, 3)
    short = 100.0
    long_extent = 3000.0
    segs = [((0.0, 0.0, 0.0), (short, 0.0, 0.0)) for _ in range(n_recognized)]
    n_unhandled = rnd.randint(1, 3)
    types = [rnd.choice(_REFUSE_TYPES + _NOTE_TYPES) for _ in range(n_unhandled)]
    extras = [(t, _pd_with_extent(t, long_extent)) for t in types]

    try:
        geo = _read(build_lines(segs, extra_entities=extras))
    except IgesParseError as e:
        # Refusal is allowed — but it must NAME the types it refused over.
        assert any(str(t) in str(e) for t in types), str(e)
        return
    # Otherwise the length must either cover the unhandled geometry, or the
    # result must name the unhandled types. Never silence.
    covers = geo.cut_length >= long_extent - 1e-6
    named = bool(geo.unhandled_types) and all(
        any(str(t) in n for n in geo.notes) for t in set(geo.unhandled_types))
    assert covers or named, (geo.cut_length, geo.unhandled_types, geo.notes)
    assert set(types) <= set(geo.entity_types_seen) | set(geo.unhandled_types)


def test_p6_copious_data_beside_a_short_line_is_refused_by_name():
    """The measured reproduction: a 3 m run in a 106 + a 100 mm detail Line."""
    with pytest.raises(IgesParseError) as e:
        _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))],
                          extra_entities=[(106, _pd_with_extent(106, 3000.0))]))
    assert "106" in str(e.value)


def test_p6_structural_entities_stay_silent():
    """The other half of the constraint: real B-rep exports are full of
    topology/annotation entities that carry no coordinates of their own. Naming
    those would drown the signal, so they must NOT produce a note."""
    extras = [(t, f"{t},0,0") for t in (402, 504, 508, 510, 514, 186, 128, 314)]
    geo = _read(build_lines([((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))],
                            extra_entities=extras))
    assert geo.unhandled_types == []
    assert geo.notes == []


def test_p6_zero_length_geometry_is_a_clean_error():
    """Degenerate geometry used to escape as a bare ValueError from the model
    layer (an unhandled traceback); it must be a named parse error instead."""
    with pytest.raises(IgesParseError) as e:
        _read(build_lines([((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))]))
    assert "zero length" in str(e.value)
