"""E8 — the material table that turns geometry into kilos.

The whole point of this module is that it never GUESSES. Two properties matter
more than the rest and are pinned hard here:

1. **The longest alias wins.** "acero inoxidable" must resolve to stainless,
   not to the "acero" inside it. Mis-reading stainless as mild steel is a ~2%
   error; reading "aluminio" as steel would be a 3x error, so the whole-word +
   longest-match rule is tested from several angles.
2. **Unknown means None**, never a fallback density — a job that cannot be
   weighed has to say so instead of inventing a number.
"""

import pytest

from nester.materials import (
    MATERIALS, Material, density_for, find_material, known_materials,
    material_label, weight_kg,
)


# --------------------------------------------------------------------------- #
# Resolution — the names a shop actually types
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, key, density", [
    ("acero", "acero", 7850),
    ("lámina negra", "acero", 7850),
    ("acero inoxidable 304", "inoxidable", 8000),
    ("inox", "inoxidable", 8000),
    ("aluminio 6061", "aluminio", 2700),
    ("galvanizada", "galvanizado", 7850),
    ("cobre", "cobre", 8960),
    ("latón", "laton", 8500),
    ("acrílico", "acrilico", 1180),
])
def test_the_shop_vocabulary_resolves(name, key, density):
    m = find_material(name)
    assert m is not None, name
    assert m.key == key
    assert m.density == density
    assert density_for(name) == density


@pytest.mark.parametrize("name, key", [
    ("steel", "acero"),
    ("stainless steel", "inoxidable"),
    ("aluminum", "aluminio"),
    ("aluminium", "aluminio"),
    ("copper", "cobre"),
    ("brass", "laton"),
    ("acrylic", "acrilico"),
    ("plywood", "triplay"),
])
def test_english_names_resolve_too(name, key):
    assert find_material(name).key == key


@pytest.mark.parametrize("name, key", [
    ("a36", "acero"),
    ("astm a36", "acero"),
    ("1018", "acero"),
    ("304", "inoxidable"),
    ("316l", "inoxidable"),
    ("aisi 304", "inoxidable"),
    ("6061", "aluminio"),
    ("5052", "aluminio"),
    ("c110", "cobre"),
    ("zintro", "galvanizado"),
])
def test_trade_and_grade_designations_resolve(name, key):
    assert find_material(name).key == key


# --------------------------------------------------------------------------- #
# Specificity — the longest alias wins (the one that matters)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, key, beats", [
    # two words beat the one word inside them
    ("acero inoxidable", "inoxidable", "acero"),
    ("acero inox", "inoxidable", "acero"),
    ("lamina galvanizada", "galvanizado", "acero"),   # "lamina negra" is acero
    ("lamina negra", "acero", "galvanizado"),
    ("acero al carbon", "acero", "inoxidable"),
    ("al 6061", "aluminio", "acero"),                 # not "al" -> nothing weird
])
def test_the_longest_alias_wins(name, key, beats):
    m = find_material(name)
    assert m is not None and m.key == key, f"{name!r} resolved to {m}"
    assert m.key != beats


def test_stainless_is_never_priced_as_mild_steel():
    """The headline case: 'acero inoxidable' contains 'acero'."""
    assert density_for("acero inoxidable") == 8000
    assert density_for("acero") == 7850
    assert density_for("acero inoxidable") != density_for("acero")
    assert material_label("acero inoxidable") == "Acero inoxidable"


def test_aluminium_is_never_confused_with_steel():
    """A 3x error if it were: 2700 vs 7850."""
    for name in ("aluminio", "aluminio 6061", "lamina de aluminio", "alum 5052"):
        assert density_for(name) == 2700, name


def test_extra_words_around_the_alias_do_not_break_resolution():
    assert find_material("placa de acero de 3 mm").key == "acero"
    assert find_material("lamina de acero inoxidable 304 cal 16").key == "inoxidable"
    assert find_material("perfil de aluminio 6063").key == "aluminio"


# --------------------------------------------------------------------------- #
# Normalization — accents, case, punctuation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "ACERO INOX.", "acero inox", "Acero Inox", "  acero   inox  ",
    "Acero-Inox", "ACERO  INOX", "acero inox!", "A. INOX",
])
def test_case_accents_and_punctuation_all_normalize_to_one_answer(name):
    m = find_material(name)
    assert m is not None and m.key == "inoxidable", name
    assert m is find_material("acero inoxidable")


@pytest.mark.parametrize("a, b", [
    ("lámina negra", "lamina negra"),
    ("latón", "laton"),
    ("acrílico", "acrilico"),
    ("Latón", "LATON"),
])
def test_accented_and_unaccented_spellings_are_the_same_material(a, b):
    assert find_material(a) is find_material(b) is not None


# --------------------------------------------------------------------------- #
# Unknown is a real answer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "unobtainium", "", "   ", "xyz123", "\t\n", "???", "material 7",
])
def test_unknown_names_resolve_to_nothing(name):
    assert find_material(name) is None
    assert density_for(name) is None
    assert material_label(name) is None


@pytest.mark.parametrize("name", [
    "acerola",        # contains "acero" but not as a whole word
    "cuadro",         # contains "cu"
    "hierroso",       # contains "hierro"
    "inoxidables",    # plural: not the alias token
    "aluminios",      # ditto
])
def test_a_mere_substring_of_an_alias_is_not_a_match(name):
    assert find_material(name) is None, f"{name!r} matched something"


def test_none_is_not_a_crash():
    assert find_material(None) is None
    assert density_for(None) is None


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #

def test_a_full_sheet_of_3mm_mild_steel_weighs_what_the_shop_expects():
    """2440x1220 mm x 3 mm at 7850 kg/m3 = 70.1 kg — a real, checkable number."""
    kg = weight_kg(2440 * 1220, 3.0, 7850)
    assert kg == pytest.approx(70.10, abs=0.01)
    # long-hand: 2.9768 m2 x 0.003 m x 7850 kg/m3
    assert kg == pytest.approx((2440 * 1220 * 1e-6) * (3.0 * 1e-3) * 7850)


def test_weight_scales_linearly_in_every_input():
    base = weight_kg(1_000_000, 2.0, 7850)
    assert weight_kg(2_000_000, 2.0, 7850) == pytest.approx(2 * base)
    assert weight_kg(1_000_000, 4.0, 7850) == pytest.approx(2 * base)
    assert weight_kg(1_000_000, 2.0, 15700) == pytest.approx(2 * base)


def test_one_cubic_metre_of_water_ish_is_one_tonne():
    # 1 m2 (1e6 mm2) x 1000 mm thick x 1000 kg/m3 = 1000 kg
    assert weight_kg(1e6, 1000.0, 1000.0) == pytest.approx(1000.0)


@pytest.mark.parametrize("area, thick, dens", [
    (0, 3, 7850), (2_976_800, 0, 7850), (2_976_800, 3, 0),
    (-1, 3, 7850), (2_976_800, -3, 7850), (2_976_800, 3, -7850),
    (0, 0, 0),
])
def test_a_missing_or_absurd_input_weighs_exactly_zero(area, thick, dens):
    """0.0 means 'cannot weigh', which the report prints as such."""
    assert weight_kg(area, thick, dens) == 0.0


def test_material_weight_kg_delegates_to_the_free_function():
    m = find_material("aluminio")
    assert m.weight_kg(2440 * 1220, 3.0) == pytest.approx(weight_kg(2440 * 1220, 3.0, 2700))
    # aluminium is ~2.9x lighter than steel for the same sheet
    steel = find_material("acero").weight_kg(2440 * 1220, 3.0)
    assert m.weight_kg(2440 * 1220, 3.0) == pytest.approx(steel * 2700 / 7850)


# --------------------------------------------------------------------------- #
# The catalog itself
# --------------------------------------------------------------------------- #

def test_known_materials_lists_every_entry_once():
    rows = known_materials()
    assert len(rows) == len(MATERIALS)
    keys = [k for k, _label, _d in rows]
    assert len(set(keys)) == len(keys)
    assert ("acero", "Acero al carbón", 7850) in rows


def test_every_catalog_entry_resolves_from_its_own_key_and_label():
    for m in MATERIALS:
        assert find_material(m.key) is m, m.key
        assert isinstance(m, Material)
        assert m.density > 0


def test_every_alias_resolves_to_its_own_material():
    """No alias may be shadowed by another material's alias."""
    shadowed = []
    for m in MATERIALS:
        for alias in (m.key,) + m.aliases:
            got = find_material(alias)
            if got is not m:
                shadowed.append((alias, m.key, got.key if got else None))
    assert shadowed == []


# --------------------------------------------------------------------------- #
# Specificity: the grade is the last word, and the grade is the answer
# --------------------------------------------------------------------------- #

def test_grade_430_written_out_in_full_is_not_read_as_304():
    """The rightmost match wins, so a fully spelled-out grade is not shadowed.

    Ranking by alias LENGTH gets this wrong in the expensive direction:
    'acero inoxidable' is a longer string than 'inoxidable 430', so 430 stock
    would be weighed at 8000 kg/m3 instead of 7700.
    """
    m = find_material("acero inoxidable 430")
    assert m is not None
    assert m.key == "inoxidable_430"
    assert m.density == 7700
    assert material_label("acero inoxidable 430") == "Acero inoxidable 430"


def test_the_short_spellings_of_430_do_resolve_correctly():
    """What DOES work today, so the bug above is scoped precisely."""
    for name in ("inoxidable 430", "inox 430", "ss430", "aisi 430", "430"):
        assert density_for(name) == 7700, name
