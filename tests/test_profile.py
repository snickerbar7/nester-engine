import pytest

from nester.tube.profile import (
    profile_from_filename,
    quantity_from_filename,
    ProfileParseError,
)


@pytest.mark.parametrize("fname,expected", [
    ("bracket_40x40x2_A.igs", "40x40x2"),
    ("rail_50X30X3-part7.iges", "50x30x3"),
    ("chassis-D32_left.igs", "d32"),
    ("railOD25.4-part7.iges", "od25.4"),
    ("square_25x25.igs", "25x25"),
])
def test_default_convention(fname, expected):
    assert profile_from_filename(fname) == expected


def test_custom_regex():
    # profile is a leading SKU code before first underscore
    fname = "PRF7_whatever.igs"
    assert profile_from_filename(fname, r"(?P<profile>PRF\d+)") == "prf7"


def test_no_match_raises():
    with pytest.raises(ProfileParseError):
        profile_from_filename("no_profile_here.igs")


@pytest.mark.parametrize("fname,expected", [
    # real LED-structure naming: cross-section + gauge
    ("Base_Central_2x2_C18_302_4pz.iges", "2x2_c18"),
    ("Brazo_Pantalla_3x1.5_C18_151_6pz.iges", "3x1.5_c18"),
    ("Soporte_Horizontal_2x2_C18_3000_1pz.iges", "2x2_c18"),
    ("Mastil_Travesaño_2x2_C18_302_Barreno_4pz.iges", "2x2_c18"),
])
def test_profile_with_gauge(fname, expected):
    assert profile_from_filename(fname) == expected


@pytest.mark.parametrize("fname,expected", [
    ("Base_Central_2x2_C18_302_4pz.iges", 4),
    ("Mastil_Travesaño_2x2_C18_302_Normal_8pz.iges", 8),
    ("Soporte_Horizontal_2x2_C18_3000_1pz.iges", 1),
    ("plain_part_no_count.iges", 1),         # defaults to 1
    ("widget-6 pcs.iges", 6),
])
def test_quantity(fname, expected):
    assert quantity_from_filename(fname) == expected
