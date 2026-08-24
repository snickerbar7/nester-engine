"""Tests for nester.tube.iges: bbox extraction + the E2 straightness check.

E2: read_tube's cut length is the bbox longest axis, which is only correct for
a STRAIGHT tube — for a bent one it silently returns the chord. check_straight
compares the measured minor bbox extents (the cross-section) against the
nominal profile dims parsed from the filename and raises when they don't fit,
naming the part as bent/non-straight rather than nesting it wrong.

Synthetic files are built with tools/make_sample_iges.build_lines: a straight
tube is one Line entity; a "bent" one is two Line segments end-to-end that
swing off-axis, so the bbox's minor extent balloons the way a real bent tube's
would, while the reader still (wrongly, if unchecked) reports a plausible
"length" from the longest axis.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_sample_iges import build, build_lines  # noqa: E402

from nester.tube.iges import check_straight, read_tube  # noqa: E402
from nester.tube.profile import parse_profile_dims, profile_from_filename  # noqa: E402
from nester.tube.cli import _load_parts  # noqa: E402


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# --------------------------------------------------------------------------- #
# parse_profile_dims
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("profile,expected", [
    ("40x40x2", (40.0, 40.0)),
    ("50x30x3", (50.0, 30.0)),
    ("25x25", (25.0, 25.0)),
    ("d32", (32.0,)),
    ("od25.4", (25.4,)),
    ("2x2_c18", (2.0, 2.0)),
])
def test_parse_profile_dims(profile, expected):
    assert parse_profile_dims(profile) == expected


def test_parse_profile_dims_unrecognized_returns_none():
    assert parse_profile_dims("not-a-profile") is None


# --------------------------------------------------------------------------- #
# check_straight
# --------------------------------------------------------------------------- #

def test_straight_tube_passes(tmp_path):
    path = _write(tmp_path, "40x40x2_ok.igs", build(1200))
    geo = read_tube(path)
    dims = parse_profile_dims("40x40x2")
    check_straight(geo, dims, path)  # should not raise


def test_bent_tube_raises(tmp_path):
    # main run along X, then a 300mm swing along Y -- far outside a 40x40 tube.
    segments = [((0, 0, 0), (1000, 0, 0)), ((1000, 0, 0), (1000, 300, 0))]
    path = _write(tmp_path, "40x40x2_bent.igs", build_lines(segments))
    geo = read_tube(path)
    dims = parse_profile_dims("40x40x2")
    with pytest.raises(ValueError, match="bent"):
        check_straight(geo, dims, path)


def test_bent_round_tube_raises(tmp_path):
    segments = [((0, 0, 0), (800, 0, 0)), ((800, 0, 0), (800, 200, 0))]
    path = _write(tmp_path, "d32_bent.igs", build_lines(segments))
    geo = read_tube(path)
    dims = parse_profile_dims("d32")
    with pytest.raises(ValueError, match="bent"):
        check_straight(geo, dims, path)


def test_no_profile_dims_skips_check(tmp_path):
    # A slightly-off-axis but still small wobble shouldn't matter here since
    # profile_dims=None means "can't check" (gap E5), not "assume straight".
    segments = [((0, 0, 0), (1000, 0, 0)), ((1000, 0, 0), (1000, 500, 0))]
    path = _write(tmp_path, "weird_bent.igs", build_lines(segments))
    geo = read_tube(path)
    check_straight(geo, None, path)  # should not raise


def test_small_modeling_noise_within_tolerance(tmp_path):
    # A straight tube modeled with a tiny wobble (e.g. rounding) should not
    # trip the check -- tolerance is max(10% nominal, 2mm).
    segments = [((0, 0, 0), (1000, 1.0, 0))]
    path = _write(tmp_path, "40x40x2_noisy.igs", build_lines(segments))
    geo = read_tube(path)
    dims = parse_profile_dims("40x40x2")
    check_straight(geo, dims, path)  # should not raise


# --------------------------------------------------------------------------- #
# wired through _load_parts (the seam shared by the CLI and service/engine.py)
# --------------------------------------------------------------------------- #

def test_load_parts_skips_bent_tube_with_error(tmp_path):
    good = _write(tmp_path, "40x40x2_a.igs", build(1200))
    segments = [((0, 0, 0), (1000, 0, 0)), ((1000, 0, 0), (1000, 300, 0))]
    bent = _write(tmp_path, "40x40x2_bent.igs", build_lines(segments))

    from nester.tube.profile import DEFAULT_PROFILE_REGEX
    parts, errors, cross = _load_parts([good, bent], DEFAULT_PROFILE_REGEX, None)

    assert len(parts) == 1
    assert parts[0].name == "40x40x2_a.igs"
    assert len(errors) == 1
    assert "bent" in errors[0]
    assert "40x40x2_bent.igs" in errors[0]


def test_load_parts_straight_tube_has_no_errors(tmp_path):
    good = _write(tmp_path, "40x40x2_a.igs", build(1200))
    from nester.tube.profile import DEFAULT_PROFILE_REGEX
    parts, errors, cross = _load_parts([good], DEFAULT_PROFILE_REGEX, None)
    assert len(parts) == 1
    assert errors == []
