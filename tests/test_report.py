import unicodedata

from nester.tube.report import _part_base, _profile_label, _slug

COMBINING_TILDE = "̃"


def test_part_base_strips_profile_qty_ext():
    n = "Base_Central_2x2_C18_302_4pz.iges #2/4"
    assert _part_base(n) == "Base_Central_302"


def test_part_base_keeps_descriptor():
    n = "Mastil_2x2_C18_1121_Atras_Inferior_2pz.iges"
    assert _part_base(n) == "Mastil_1121_Atras_Inferior"


def test_part_base_nfc_normalizes_decomposed_enye():
    # macOS NFD: 'n' + U+0303 combining tilde (how the real file is stored)
    decomposed = unicodedata.normalize(
        "NFD", "Mastil_Travesaño_2x2_C18_302_Normal_8pz.iges")
    assert COMBINING_TILDE in decomposed       # sanity: input really is decomposed
    out = _part_base(decomposed)
    assert COMBINING_TILDE not in out          # combining mark gone
    assert out == unicodedata.normalize("NFC", "Mastil_Travesaño_302_Normal")


def test_profile_label():
    assert _profile_label("2x2_c18") == "2×2 · Cal.18"
    assert _profile_label("3x1.5_c18") == "3×1.5 · Cal.18"


def test_slug():
    assert _slug("Pantallas LED / v2") == "Pantallas_LED_v2"
