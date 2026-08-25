"""What the cut-plan PDF must actually say.

The PDF follows the "Plan de corte" artboard (portada · dibujo de tramos ·
lista de cortes · etiquetas). These tests read the rendered text back with
pypdf, so they pin the information the shop reads off the paper — not pixels.
"""

import pytest

from nester.tube.model import ExtraStock, Part, StockSpec
from nester.tube.packing import pack_profile
from nester.tube.report import write_reports

pypdf = pytest.importorskip("pypdf")
pytest.importorskip("reportlab")


def _mk(profile, lengths, name="part"):
    return [Part(name=f"{name}_{i}.igs", profile=profile, length=L)
            for i, L in enumerate(lengths)]


def _pdf(tmp_path, results, lang="es", warnings=None, job="job"):
    written = write_reports(results, str(tmp_path), job,
                            {"generated": "2026-01-01 10:00", "lang": lang,
                             "kerf": results[0].spec.kerf,
                             "front_trim": results[0].spec.front_trim,
                             "back_trim": results[0].spec.back_trim},
                            warnings=warnings)
    path = [w for w in written if w.endswith(".pdf")][0]
    reader = pypdf.PdfReader(path)
    return reader, [p.extract_text() for p in reader.pages]


def _plan():
    spec = StockSpec(profile="40x40x2_c14", stock_length=6000, kerf=3, back_trim=300)
    return pack_profile(_mk("40x40x2_c14", [1900, 1900, 1240, 862.5, 640, 385.5],
                            name="Poste_Vertical"), spec)


# --------------------------------------------------------------------------- #
# Sheet structure
# --------------------------------------------------------------------------- #

def test_pdf_has_the_four_designed_sheet_kinds(tmp_path):
    reader, pages = _pdf(tmp_path, [_plan()])
    blob = "\n".join(pages)
    assert "Plan de corte" in pages[0]          # portada
    assert "Dibujo de tramos" in blob           # per-bar drawings
    assert "Lista de cortes" in blob            # cut list
    assert "Etiquetas" in blob                  # cut-out labels
    # every sheet is numbered "hoja N / total", with the total matching reality
    assert f"hoja {len(reader.pages)} / {len(reader.pages)}" in pages[-1]


def test_cover_carries_the_purchase_and_accounting_panels(tmp_path):
    _, pages = _pdf(tmp_path, [_plan()])
    cover = pages[0]
    for kicker in ("RESUMEN DE COMPRA", "CÓMO QUEDÓ EL MATERIAL",
                   "CUENTAS DEL MATERIAL", "PARÁMETROS DEL ANIDADO",
                   "ARCHIVOS DE ORIGEN", "FIRMAS"):
        assert kicker in cover, kicker
    assert "Total a comprar" in cover
    assert "40×40×2 · Cal.14" in cover           # profile label, not the slug


def test_english_report_translates_the_same_layout(tmp_path):
    _, pages = _pdf(tmp_path, [_plan()], lang="en")
    blob = "\n".join(pages)
    assert "Cut plan" in pages[0]
    assert "PURCHASE SUMMARY" in pages[0]
    assert "MATERIAL ACCOUNTS" in pages[0]
    assert "Bar drawings" in blob
    assert "Cut list" in blob
    assert "Labels" in blob
    assert "RESUMEN DE COMPRA" not in blob


# --------------------------------------------------------------------------- #
# Invariants the plan must never lose
# --------------------------------------------------------------------------- #

def test_shopping_box_counts_only_new_bars_and_names_the_remnants(tmp_path):
    spec = StockSpec(profile="p", stock_length=6000, kerf=3,
                     extra_stock=(ExtraStock(2400, "R-0001"),))
    res = pack_profile(_mk("p", [2000, 1500, 1200]), spec)
    assert res.new_bars_needed == 1 and res.remnants_used == ["R-0001"]
    _, pages = _pdf(tmp_path, [res])
    cover = pages[0]
    assert "1 tramo" in cover                    # buy one, not two
    assert "Retazos usados" in cover
    assert "R-0001" in cover


def test_remnant_bar_is_drawn_at_its_own_length_and_labelled(tmp_path):
    spec = StockSpec(profile="p", stock_length=6000, kerf=3,
                     extra_stock=(ExtraStock(2400, "R-0001"),))
    res = pack_profile(_mk("p", [2000, 1500]), spec)
    _, pages = _pdf(tmp_path, [res])
    blob = "\n".join(pages)
    assert "RETAZO R-0001 (2400 mm)" in blob
    assert "TRAMO 1" in blob                     # the bought bar keeps ordinal 1


def test_running_positions_are_absolute_and_include_the_front_trim(tmp_path):
    spec = StockSpec(profile="p", stock_length=6000, kerf=2, front_trim=40)
    res = pack_profile(_mk("p", [1000, 1000]), spec)
    _, pages = _pdf(tmp_path, [res])
    blob = "\n".join(pages)
    # first cut lands at front_trim + 1000, not at 1000
    assert "1,040.0" in blob
    assert "2,042.0" in blob                     # second cut adds one kerf


def test_parts_guide_maps_colour_to_its_source_files(tmp_path):
    spec = StockSpec(profile="p", stock_length=6000, kerf=3)
    parts = (_mk("p", [800, 800], name="Base_Central")
             + _mk("p", [800], name="Tapa_Lateral"))
    res = pack_profile(parts, spec)
    _, pages = _pdf(tmp_path, [res])
    blob = "\n".join(pages)
    assert "RESUMEN POR PIEZA" in blob
    assert "Base_Central_0 (1)" in blob or "Base_Central_1 (1)" in blob
    assert "Tapa_Lateral_0 (1)" in blob


def test_unplaceable_parts_reach_the_warning_panel(tmp_path):
    spec = StockSpec(profile="p", stock_length=1000)
    res = pack_profile(_mk("p", [400, 5000]), spec)
    assert res.unplaceable
    _, pages = _pdf(tmp_path, [res])
    assert "ANTES DE CORTAR" in pages[0]
    assert "Piezas más largas que el tramo" in pages[0]


def test_artifact_warnings_are_printed_on_the_plan(tmp_path):
    _, pages = _pdf(tmp_path, [_plan()],
                    warnings=["IGES nest-layout not written (job_nest.igs): disk full"])
    assert "ANTES DE CORTAR" in pages[0]
    assert "disk full" in pages[0]


def test_multi_profile_plan_separates_materials(tmp_path):
    a = pack_profile(_mk("40x40x2", [1200, 1200], name="Larguero"),
                     StockSpec(profile="40x40x2", stock_length=6000, kerf=3))
    b = pack_profile(_mk("d32", [900], name="Tirante"),
                     StockSpec(profile="d32", stock_length=6000, kerf=3))
    _, pages = _pdf(tmp_path, [a, b])
    blob = "\n".join(pages)
    assert "40×40×2" in blob and "Ø32" in blob
    # both profiles get their own drawing sheet
    assert blob.count("Dibujo de tramos") == 2


def test_labels_sheet_has_one_tag_per_cut(tmp_path):
    res = _plan()
    n = sum(len(bar.placements) for bar in res.bars)
    _, pages = _pdf(tmp_path, [res], job="LED40")
    labels = [p for p in pages if "Etiquetas" in p][0]
    assert f"{n} etiquetas" in labels
    assert "LED40-P01-01" in labels              # job · piece · running folio
