"""What the flat-sheet cut plan must actually say — and draw.

The PDF follows the "Plan de corte en lámina" artboard (portada + one drawing
sheet per lámina). These tests read the rendered text back with pypdf and poke
at the page content streams, so they pin the information the shop reads off the
paper — the sheet inventory, the numbers the engine produced, and the fact that
the parts are drawn as REAL silhouettes with real holes, not as rectangles.
"""

import math
import re

import pytest

from nester.sheet.model import FlatPart, NestResult, Placement, SheetLayout, SheetSpec
from nester.sheet.report import _deep_point, _part_base, write_reports

pypdf = pytest.importorskip("pypdf")
pytest.importorskip("reportlab")


# --------------------------------------------------------------------------- #
# Fixtures — a nest built by hand, so the tests own every number
# --------------------------------------------------------------------------- #

def _ring(r, n=64, cx=None, cy=None):
    cx = r if cx is None else cx
    cy = r if cy is None else cy
    return tuple((cx + r * math.cos(2 * math.pi * i / n),
                  cy + r * math.sin(2 * math.pi * i / n)) for i in range(n))


def _L(w, h, t):
    return ((0, 0), (w, 0), (w, t), (t, t), (t, h), (0, h))


def _rect(w, h, x=0.0, y=0.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


ANILLO = FlatPart(name="Anillo_Grande.dxf", outer=_ring(200),
                  holes=(_ring(120, cx=200, cy=200),), qty=6)
TAPA = FlatPart(name="Tapa_Lateral_4pz.dxf", outer=_L(320, 240, 70), qty=4)
PLACA = FlatPart(name="Placa_Base.dxf", outer=_rect(400, 250),
                 holes=(_rect(60, 40, 40, 40),), qty=3)
GIGANTE = FlatPart(name="Panel_Gigante.dxf", outer=_rect(2900, 1400), qty=1)

SPEC = SheetSpec(width=2440, height=1220, material="Lámina negra",
                 thickness=2, margin=8, part_gap=3)

META = {"generated": "2026-01-01 10:00", "lang": "es", "rotation": "free",
        "seed": 7, "time_per_sheet": 4}


def _layout(index, items):
    """items = [(part, x, y, rot), ...]"""
    return SheetLayout(index=index, spec=SPEC,
                       placements=[Placement(part=p, x=x, y=y, rotation=r)
                                   for (p, x, y, r) in items])


def _nest(sheets=3, unplaceable=()):
    plan = [
        [(ANILLO, 100, 100, 0.0), (ANILLO, 550, 100, 0.0),
         (TAPA, 1000, 100, 0.0), (PLACA, 1400, 100, 0.0)],
        [(ANILLO, 100, 100, 0.0), (ANILLO, 550, 100, 90.0),
         (TAPA, 1000, 100, 180.0)],
        [(ANILLO, 100, 100, 0.0), (ANILLO, 550, 600, 0.0),
         (TAPA, 1000, 100, 0.0), (TAPA, 1400, 400, 90.0),
         (PLACA, 1800, 100, 0.0), (PLACA, 1800, 500, 0.0)],
    ][:sheets]
    return NestResult(spec=SPEC,
                      sheets=[_layout(i, items) for i, items in enumerate(plan)],
                      unplaceable=list(unplaceable))


def _pdf(tmp_path, result, lang="es", warnings=None, job="Tablero_CFE"):
    meta = dict(META, lang=lang)
    written = write_reports(result, str(tmp_path), job, meta, warnings=warnings)
    path = [w for w in written if w.endswith(".pdf")][0]
    reader = pypdf.PdfReader(path)
    return reader, [p.extract_text() for p in reader.pages], path


# --------------------------------------------------------------------------- #
# Document structure
# --------------------------------------------------------------------------- #

def test_portada_plus_one_drawing_sheet_per_lamina(tmp_path):
    reader, pages, _ = _pdf(tmp_path, _nest(sheets=3))
    assert len(reader.pages) == 4                      # portada + 3 láminas
    assert "Plan de corte en lámina" in pages[0]
    for i in (1, 2, 3):
        assert f"Lámina {i} de 3" in pages[i]
    # every sheet is numbered "hoja N / total", with the total matching reality
    assert "hoja 4 / 4" in pages[-1]


def test_sheet_inventory_names_every_nested_dxf(tmp_path):
    _, pages, _ = _pdf(tmp_path, _nest(sheets=3), job="job one")
    blob = "\n".join(pages)
    for n in ("job_one_S01.dxf", "job_one_S02.dxf", "job_one_S03.dxf"):
        assert n in blob, n
    # and the operator's checklist points at the same programs
    assert "Programa job_one_S02 cargado" in pages[2]


def test_single_sheet_job_still_gets_its_drawing_sheet(tmp_path):
    reader, pages, _ = _pdf(tmp_path, _nest(sheets=1))
    assert len(reader.pages) == 2
    assert "Lámina 1 de 1" in pages[1]


# --------------------------------------------------------------------------- #
# The portada carries the design's panels, filled from engine numbers
# --------------------------------------------------------------------------- #

def test_cover_carries_the_designed_panels(tmp_path):
    _, pages, _ = _pdf(tmp_path, _nest())
    cover = pages[0]
    for k in ("RESUMEN DE COMPRA", "CÓMO QUEDÓ CADA LÁMINA", "PIEZAS DEL TRABAJO",
              "CUENTAS DEL MATERIAL", "PARÁMETROS DEL ANIDADO",
              "ANTES DE CORTAR", "FIRMAS"):
        assert k in cover, k
    assert "Lámina negra · 2 mm" in cover          # material + gauge, one group
    assert "2,440 × 1,220 mm" in cover


def test_cover_totals_come_from_the_engine_not_from_prose(tmp_path):
    result = _nest()
    _, pages, _ = _pdf(tmp_path, result)
    cover = pages[0]
    bought = result.total_sheet_area / 1e6
    parts = result.total_part_area / 1e6
    assert f"{parts:,.3f} m²" in cover              # área en piezas
    assert f"{bought:,.3f} m²" in cover             # área comprada
    assert f"{bought - parts:,.3f} m²" in cover     # sobrante
    assert f"{result.yield_pct:.1f} %" in cover     # aprovechamiento
    assert str(result.sheet_count) in cover
    # the accounting identity is spelled out, with the same two numbers
    assert f"{parts:,.3f} ÷ {bought:,.3f} m²" in cover


def test_cover_part_list_shows_ids_quantities_and_where_they_landed(tmp_path):
    _, pages, _ = _pdf(tmp_path, _nest())
    cover = pages[0]
    assert "P-01" in cover and "P-02" in cover and "P-03" in cover
    assert "Anillo_Grande" in cover
    assert "Tapa_Lateral" in cover                  # the _4pz token is stripped
    assert "L1×2" in cover                          # two anillos on lámina 1
    assert "× 6" in cover                           # six anillos in the job


def test_cover_prints_the_parameters_it_actually_nested_with(tmp_path):
    _, pages, _ = _pdf(tmp_path, _nest())
    cover = pages[0]
    for row in ("Margen de lámina", "Separación entre piezas", "Rotación",
                "Semilla", "Motor"):
        assert row in cover, row
    assert "8 mm" in cover and "3 mm" in cover
    assert "libre" in cover                         # rotation mode, translated


def test_no_seed_no_seed_row(tmp_path):
    """Data the caller did not supply is omitted, never invented."""
    meta = {"generated": "", "lang": "es", "rotation": "free"}
    written = write_reports(_nest(), str(tmp_path), "job", meta)
    pages = [p.extract_text() for p in
             pypdf.PdfReader([w for w in written if w.endswith(".pdf")][0]).pages]
    assert "Semilla" not in pages[0]
    assert "Tiempo por lámina" not in pages[0]


# --------------------------------------------------------------------------- #
# Drawing sheets
# --------------------------------------------------------------------------- #

def test_each_drawing_sheet_reports_its_own_numbers(tmp_path):
    result = _nest()
    _, pages, _ = _pdf(tmp_path, result)
    for i, layout in enumerate(result.sheets, start=1):
        page = pages[i]
        for k in ("APROVECHAMIENTO", "PIEZAS", "ÁREA EN PIEZAS", "SOBRANTE",
                  "PIEZAS DE ESTA LÁMINA", "PARA EL OPERADOR", "CONTROL DE CORTE"):
            assert k in page, (i, k)
        assert f"{layout.utilization * 100:.1f} %" in page
        assert f"{layout.used_area / 1e6:,.3f} m²" in page
        assert "escala 1:" in page


def test_sheet_part_table_counts_the_copies_on_that_sheet(tmp_path):
    result = _nest()
    _, pages, _ = _pdf(tmp_path, result)
    # lámina 3 carries 2 anillos, 2 tapas and 2 placas
    page = pages[3]
    assert "Anillo_Grande" in page and "Placa_Base" in page
    assert result.sheets[2].part_count == 6
    assert "6 piezas" in page                       # the header subtitle


def test_low_yield_sheet_tells_the_operator_to_stop_and_think(tmp_path):
    lean = NestResult(spec=SPEC, sheets=[_layout(0, [(TAPA, 100, 100, 0.0)])])
    _, pages, _ = _pdf(tmp_path, lean)
    assert "menos de la mitad" in pages[1]


# --------------------------------------------------------------------------- #
# Real shapes — the drawing is the trust element
# --------------------------------------------------------------------------- #

def _ops(reader, page_index):
    return reader.pages[page_index].get_contents().get_data().decode("latin-1")


def _linetos(reader, page_index):
    """How many lineTo operators the page draws — reportlab emits one path per
    line, so this counts real drawn geometry."""
    return len(re.findall(r"\bl\b", _ops(reader, page_index)))


def _shape_subpaths(reader, page_index):
    """Subpaths in the richest even-odd path on the page — the placed part.

    A part WITH a hole reaches the page as one path carrying TWO subpaths (outer
    + hole, filled even-odd so the sheet shows through); a part without a hole
    carries one. Counting subpaths says exactly that, and unlike counting line
    segments it does not move when the drawing is laid out a little smaller —
    contour flattening is scale-derived, so a segment-count ratio silently
    tracks page layout instead of geometry.
    """
    paths = re.findall(r"[\d.]+ [\d.]+ [\d.]+ rg\s(.*?)B\*",
                       _ops(reader, page_index), re.S)
    if not paths:
        return 0
    richest = max(paths, key=lambda seg: len(re.findall(r"\bl\b", seg)))
    return len(re.findall(r"\bm\b", richest))


def test_parts_are_drawn_as_real_silhouettes_not_rectangles(tmp_path):
    """A ring nested on a sheet must reach the page as a many-segment path with
    a second subpath for its hole — a bounding rectangle would be 4 lines."""
    one = NestResult(spec=SPEC, sheets=[_layout(0, [(ANILLO, 300, 300, 0.0)])])
    reader, _, _ = _pdf(tmp_path, one)
    ops = _ops(reader, 1)
    assert _linetos(reader, 1) > 100, ops     # 64-gon outer + 64-gon hole
    # one path, filled even-odd — the hole is punched, not painted over
    assert "f*" in ops or "B*" in ops
    # ... and that path carries two subpaths (outer + hole)
    shape = ops.split(".119 .395 .668 rg")[1].split("B*")[0]
    assert len(re.findall(r"\bm\b", shape)) == 2, shape[:400]


def test_holes_are_not_drawn_when_the_part_has_none(tmp_path):
    """Same part count, fewer subpaths: the hole in the ring is real geometry."""
    solid = FlatPart(name="Disco.dxf", outer=_ring(200), qty=1)
    with_hole = NestResult(spec=SPEC, sheets=[_layout(0, [(ANILLO, 300, 300, 0.0)])])
    without = NestResult(spec=SPEC, sheets=[_layout(0, [(solid, 300, 300, 0.0)])])
    a, _, _ = _pdf(tmp_path / "a", with_hole)
    b, _, _ = _pdf(tmp_path / "b", without)
    assert _shape_subpaths(a, 1) == 2          # outer + hole
    assert _shape_subpaths(b, 1) == 1          # outer only
    # both still reach the page as real silhouettes: a bounding box would be
    # four lines, so anything in the dozens is the flattened circle itself
    assert _linetos(a, 1) > 100        # 64-gon outer + 64-gon hole
    assert _linetos(b, 1) > 20         # one 64-gon, no hole


def test_rotated_placements_are_drawn_rotated(tmp_path):
    """The page geometry follows the engine's rotate-then-translate convention:
    the same part at 0° and at 37° cannot produce the same path."""
    up = NestResult(spec=SPEC, sheets=[_layout(0, [(TAPA, 300, 300, 0.0)])])
    tilt = NestResult(spec=SPEC, sheets=[_layout(0, [(TAPA, 300, 300, 37.0)])])
    a, _, _ = _pdf(tmp_path / "a", up)
    b, _, _ = _pdf(tmp_path / "b", tilt)
    assert _ops(a, 1) != _ops(b, 1)


def test_label_anchor_lands_inside_the_material(tmp_path):
    """The part ID is stamped at the deepest interior point, not the bbox centre
    — the centre of an L-bracket's box is bare sheet."""
    outer = _L(320, 240, 70)
    x, y, clear = _deep_point(list(outer), [])
    assert clear > 0
    # inside the L, and not in the notch (which spans x>70 and y>70)
    assert not (x > 70 and y > 70)
    ring_x, ring_y, ring_clear = _deep_point(list(_ring(200)),
                                             [list(_ring(120, cx=200, cy=200))])
    d = math.hypot(ring_x - 200, ring_y - 200)
    assert 120 < d < 200                     # in the band, not in the hole


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #

def test_unplaceable_parts_are_named_on_paper(tmp_path):
    _, pages, _ = _pdf(tmp_path, _nest(unplaceable=[GIGANTE]))
    cover = pages[0]
    assert "Piezas más grandes que la lámina" in cover
    assert "Panel_Gigante" in cover
    assert "no cabe" in cover                # tagged in the part list too


def test_caller_warnings_reach_the_paper_and_the_json(tmp_path):
    msg = "nested DXF-per-sheet not written: disk full"
    written = write_reports(_nest(), str(tmp_path), "job", META, warnings=[msg])
    import json
    data = json.load(open([w for w in written if w.endswith(".json")][0]))
    assert data["warnings"] == [msg]
    pages = [p.extract_text() for p in
             pypdf.PdfReader([w for w in written if w.endswith(".pdf")][0]).pages]
    assert "nested DXF-per-sheet not written" in pages[0]


def test_engine_limits_are_stated_when_the_options_are_off(tmp_path):
    """A limitation card is about THIS run, not about the engine forever.

    With hole nesting off, no minimum remnant and no density, the plan says all
    three plainly — and each card points at the switch that removes it.
    """
    _, pages, _ = _pdf(tmp_path, _nest())
    cover = pages[0]
    assert "Los barrenos de este trabajo no se aprovecharon" in cover
    assert "El sobrante no entra al inventario" in cover
    assert "Sin kilos en este trabajo" in cover


def test_hole_nesting_turns_the_limit_into_an_instruction(tmp_path):
    """Once parts are nested into holes, the card must tell the operator what
    to DO with the slug — and must stop claiming the engine cannot do it."""
    result = _nest(sheets=1)
    sheet = result.sheets[0]
    # a small part cut out of the ring's bore
    sheet.placements.append(
        Placement(part=PLACA, x=180, y=180, rotation=0.0, in_hole_of=0))
    meta = dict(META, nest_in_holes=True)
    written = write_reports(result, str(tmp_path), "job", meta)
    pages = [p.extract_text() for p in
             pypdf.PdfReader([w for w in written if w.endswith(".pdf")][0]).pages]
    cover, drawing = pages[0], pages[1]
    assert "salen dentro del barreno de otra" in cover
    assert "NO es chatarra" in cover
    assert "Los barrenos de este trabajo no se aprovecharon" not in cover
    # the operator's copy of the instruction lives on the drawing sheet
    assert "no lo tires con el esqueleto" in drawing
    # flagged in that sheet's part list, as its own row. The marker must be a
    # WORD: the base-14 PDF fonts are WinAnsi-encoded and silently drop an
    # arrow glyph, which is exactly how this was caught.
    assert "EN BARRENO" in drawing


def test_reclaimable_leftover_replaces_the_untracked_drop_card(tmp_path):
    from nester.sheet.model import Leftover
    result = _nest(sheets=1)
    result.sheets[0].leftover = Leftover(x=1900, y=0, width=540, height=1220)
    meta = dict(META, min_remnant=200)
    written = write_reports(result, str(tmp_path), "job", meta)
    pages = [p.extract_text() for p in
             pypdf.PdfReader([w for w in written if w.endswith(".pdf")][0]).pages]
    cover = pages[0]
    assert "Sobrante recuperable" in cover
    assert "El sobrante no entra al inventario" not in cover


def test_weights_appear_once_the_material_has_a_density(tmp_path):
    spec = SheetSpec(width=2440, height=1220, material="Lámina negra",
                     thickness=2, margin=8, part_gap=3, density=7850)
    result = _nest(sheets=1)
    result.spec = spec
    for s in result.sheets:
        s.spec = spec
    written = write_reports(result, str(tmp_path), "job", META)
    pages = [p.extract_text() for p in
             pypdf.PdfReader([w for w in written if w.endswith(".pdf")][0]).pages]
    cover = pages[0]
    assert "Sin kilos en este trabajo" not in cover
    assert "kg" in cover
    assert "7,850 kg/m³" in cover               # the density is shown, not hidden


# --------------------------------------------------------------------------- #
# Language parity
# --------------------------------------------------------------------------- #

def test_english_plan_translates_the_same_layout(tmp_path):
    reader, pages, path = _pdf(tmp_path, _nest(unplaceable=[GIGANTE]), lang="en")
    assert path.endswith("_Cut_Plan.pdf")
    cover = pages[0]
    assert "Sheet cut plan" in cover
    for k in ("PURCHASE SUMMARY", "HOW EACH SHEET WORKED OUT", "PARTS IN THIS JOB",
              "MATERIAL ACCOUNTS", "NESTING PARAMETERS", "BEFORE YOU CUT",
              "SIGN-OFF"):
        assert k in cover, k
    assert "Parts larger than the sheet" in cover
    assert "Sheet 1 of 3" in pages[1]
    assert "PARTS ON THIS SHEET" in pages[1]
    assert "CUT CHECKLIST" in pages[1]
    assert "scale 1:" in pages[1]
    assert "sheet 4 / 4" in pages[-1]
    blob = "\n".join(pages)
    assert "RESUMEN DE COMPRA" not in blob
    assert "Lámina 1 de 3" not in blob


def test_spanish_and_english_plans_have_the_same_shape(tmp_path):
    es, _, _ = _pdf(tmp_path / "es", _nest())
    en, _, _ = _pdf(tmp_path / "en", _nest(), lang="en")
    assert len(es.pages) == len(en.pages)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

def test_part_base_keeps_the_multi_part_discriminator():
    assert _part_base("Tapa_Lateral_4pz.dxf") == "Tapa_Lateral"
    assert _part_base("Tapa_Lateral.dxf#2") == "Tapa_Lateral #2"
    assert _part_base("Refuerzo_Ñandú.dxf") == "Refuerzo_Ñandú"


def test_a_job_where_nothing_fits_still_produces_an_honest_plan(tmp_path):
    """No sheet pages to draw — but the plan must still say why, not go blank."""
    nothing = NestResult(spec=SPEC, sheets=[], unplaceable=[GIGANTE])
    reader, pages, _ = _pdf(tmp_path, nothing)
    assert len(reader.pages) == 1
    assert "Ninguna pieza cupo en la lámina" in pages[0]
    assert "Panel_Gigante" in pages[0]
    assert "0.000 m²" in pages[0]
