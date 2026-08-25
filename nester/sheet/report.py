"""Output artifacts for a flat-sheet nesting run: a printable cut plan + JSON.

The PDF is an *operational* shop document, laid out to the Harriet Nester
"Plan de corte en lámina" artboard: A4/letter landscape, white paper, graphite
ink, the two type families approximated with the reportlab base-14 fonts
(Helvetica for prose, Courier for every figure, ID and kicker).

Sheets
  1        Portada — resumen de compra, cómo quedó cada hoja (mini nests drawn
           with the real silhouettes), the job's part list, material accounts,
           nesting parameters, warnings, sign-off.
  2..n+1   One drawing sheet per lámina — the nest drawn to scale with the TRUE
           placed silhouettes (outer contour + holes, even-odd fill), the sheet
           margin dashed, per-sheet KPIs, the parts on that sheet, an operator
           note and a tickable cut checklist.

The drawing is the trust element: a part is drawn as what the laser will cut,
never as its bounding rectangle. Geometry comes from the engine — placements are
reconstructed with :func:`nester.sheet.pack.transform` (rotate about the origin,
then translate), the same convention the nested DXF is written with, so paper
and machine can never disagree. Contours are decimated for drawing only
(:func:`nester.sheet.contour.simplify_ring`) at a tolerance derived from the
plot scale, so a 20k-point spline stays a few hundred points on paper.

Bilingual (es/en); the Spanish copy is the design's, the English a faithful
translation onto the same layout. Sections the engine has no data for (part
weight, file provenance) are omitted rather than invented.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Sequence, Tuple

from .contour import simplify_ring
from .model import FlatPart, NestResult, Point, SheetLayout, SheetSpec
from .pack import transform

from .._pdfstyle import (
    ACC_BAR, ACC_BG, ACC_BRD, ACC_TXT, DASH_RULE, FAINT, HAIR, HAIR2, INK, MID,
    MONO, MONO_B, PALETTE, PAPER, RULE, SANS, SANS_B, SOFT, ZEBRA, ellipsize,
    kicker, line, px, rect, signature_block, tw, txt, wrap,
)

try:  # optional dependency — JSON works without it
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    from reportlab.pdfgen.canvas import FILL_EVEN_ODD
    _HAVE_REPORTLAB = True
except ImportError:  # pragma: no cover
    _HAVE_REPORTLAB = False


# Drawing fidelity: how far a decimated contour may stray from the true one, in
# PDF points on the finished page. 0.18 pt ≈ 0.06 mm of ink — invisible, and it
# keeps a spline-heavy part to a few hundred segments per placement.
_DRAW_TOL_PT = 0.18
_DRAW_MAX_POINTS = 700
_TOL_BOUNDS = (0.02, 6.0)          # mm
# Label anchors are searched on a deliberately coarse copy of the contour: the
# search is O(grid × edges) and 2 mm of shape detail cannot move a label.
_ANCHOR_TOL = 2.0
# A sheet under this fraction of its area is worth a word to the operator.
_LOW_YIELD = 0.45


_LANG = {
    "es": {
        "title": "Plan de corte en lámina",
        "brand": "Harriet Nester",
        # ---- portada ----
        "buy": "RESUMEN DE COMPRA",
        "buy_sub": "un solo material y calibre en todo el trabajo",
        "h_material": "MATERIAL", "h_size": "MEDIDA", "h_sheets": "HOJAS",
        "h_pcs": "PIEZAS", "h_yield": "APROV.",
        "material_sub": "margen {margin} · separación {gap} · rotación {rot}",
        "material_unknown": "Lámina (material sin especificar)",
        "howitwent": "CÓMO QUEDÓ CADA HOJA",
        "mini_pcs": "{n} pzas",
        "mini_pcs1": "1 pza",
        "more_sheets": "+ {n} hoja(s) más · ver las hojas de dibujo",
        "parts_job": "PIEZAS DEL TRABAJO",
        "more_parts": "+ {n} pieza(s) más",
        "not_placed": "no cabe",
        "accounts": "CUENTAS DEL MATERIAL",
        "a_sheets": "HOJAS", "a_bought": "ÁREA COMPRADA",
        "a_parts": "ÁREA EN PIEZAS", "a_drop": "SOBRANTE",
        "a_yield": "APROVECHAMIENTO",
        "formula": "aprov. = área de piezas ÷ área de láminas = {parts} ÷ {stock} m² · "
                   "el sobrante no se descuenta: todavía no entra al inventario de retazos",
        "params": "PARÁMETROS DEL ANIDADO",
        "p_material": "Material", "p_thickness": "Espesor",
        "p_sheet": "Lámina", "p_margin": "Margen de lámina",
        "p_gap": "Separación entre piezas", "p_rot": "Rotación",
        "p_time": "Tiempo por lámina", "p_seed": "Semilla",
        "p_engine": "Motor", "p_engine_v": "spyrrow · sparrow",
        "p_parts": "Piezas a cortar", "p_unique": "Piezas distintas",
        "p_area": "Área de piezas",
        "warn": "ANTES DE CORTAR",
        "warn_sub": "revísalo antes de encender la máquina",
        "warn_toobig": "Piezas más grandes que la lámina",
        "warn_toobig_txt": "No caben en el área útil de {sheet} y quedaron fuera "
                           "del plan: {names}. Divídelas o pide una lámina mayor.",
        "warn_last": "La última hoja va al {pct}",
        "warn_last_txt": "Si puedes esperar otro trabajo del mismo material y "
                         "calibre, se anidan juntos y sube el aprovechamiento.",
        "warn_holes": "Los barrenos no se aprovechan",
        "warn_holes_txt": "El motor no anida piezas dentro de los barrenos de "
                          "otras piezas: el hueco de un barreno grande cuenta como sobrante.",
        "warn_drop": "El sobrante no entra al inventario",
        "warn_drop_txt": "Queda anotado en el plan, pero todavía no se registra "
                         "como retazo de lámina reutilizable.",
        "warn_generic": "Aviso del trabajo",
        "and_more": " y {n} más",
        "more_warn": "+ {n} aviso(s) más · ver el JSON del trabajo",
        "signoff": "FIRMAS",
        "sign": ["Revisó", "Cortó · operador", "Verificó · calidad", "Entregó · almacén"],
        "meta_date": "FECHA", "meta_sheets": "LÁMINAS", "meta_pieces": "PIEZAS",
        "meta_yield": "APROV.",
        # ---- hoja de dibujo ----
        "sheet_title": "Lámina {i} de {n}",
        "sheet_sub": "{material} · {size} · {n} piezas",
        "sheet_sub1": "{material} · {size} · 1 pieza",
        "scale_note": "escala 1:{scale} · origen en la esquina inferior izquierda · medidas en mm",
        "k_yield": "APROVECHAMIENTO", "k_parts": "PIEZAS",
        "k_area": "ÁREA EN PIEZAS", "k_drop": "SOBRANTE",
        "sheet_parts": "PIEZAS DE ESTA HOJA",
        "c_piece": "PIEZA", "c_desc": "DESCRIPCIÓN", "c_bbox": "ENCAJONADO",
        "c_qty": "CANT.",
        "operator": "PARA EL OPERADOR",
        "note_low": "Esta hoja va a menos de la mitad. Antes de cortarla, confirma "
                    "con el taller si conviene esperar otro trabajo del mismo "
                    "material y calibre.",
        "note_ok": "Carga la hoja con el margen de {margin} libre en los cuatro "
                   "lados. La separación entre piezas es de {gap}: no muevas "
                   "piezas a mano en el CAM o se pierde.",
        "note_nomargin": "Carga la hoja escuadrada contra los topes. La separación "
                         "entre piezas es de {gap}: no muevas piezas a mano en el "
                         "CAM o se pierde.",
        "control": "CONTROL DE CORTE",
        "ctl_loaded": "Hoja cargada y escuadrada",
        "ctl_program": "Programa {file} cargado",
        "ctl_counted": "Piezas contadas al descargar",
        # ---- chrome ----
        "sheet": "hoja {p} / {n}",
        "foot_cover": "hecho con {brand} · nester.harriet.com.mx · {date}",
        "foot_params": "margen {margin} · separación {gap} · rotación {rot}",
        "foot_seed": "motor spyrrow · semilla {seed}",
        "empty": "Ninguna pieza se pudo colocar en una lámina.",
        "rot_free": "libre", "rot_grain": "grano", "rot_fixed": "fija",
        "rot_ortho": "ortogonal",
        "pdf_name": "{slug}_Plan_de_Corte.pdf",
    },
    "en": {
        "title": "Sheet cut plan",
        "brand": "Harriet Nester",
        "buy": "PURCHASE SUMMARY",
        "buy_sub": "one material and gauge for the whole job",
        "h_material": "MATERIAL", "h_size": "SIZE", "h_sheets": "SHEETS",
        "h_pcs": "PARTS", "h_yield": "YIELD",
        "material_sub": "margin {margin} · gap {gap} · rotation {rot}",
        "material_unknown": "Sheet (material not specified)",
        "howitwent": "HOW EACH SHEET WORKED OUT",
        "mini_pcs": "{n} pcs",
        "mini_pcs1": "1 pc",
        "more_sheets": "+ {n} more sheet(s) · see the drawing sheets",
        "parts_job": "PARTS IN THIS JOB",
        "more_parts": "+ {n} more part(s)",
        "not_placed": "does not fit",
        "accounts": "MATERIAL ACCOUNTS",
        "a_sheets": "SHEETS", "a_bought": "AREA BOUGHT",
        "a_parts": "AREA IN PARTS", "a_drop": "LEFTOVER",
        "a_yield": "YIELD",
        "formula": "yield = part area ÷ sheet area = {parts} ÷ {stock} m² · "
                   "the leftover is not discounted: it is not remnant stock yet",
        "params": "NESTING PARAMETERS",
        "p_material": "Material", "p_thickness": "Thickness",
        "p_sheet": "Sheet", "p_margin": "Sheet margin",
        "p_gap": "Part-to-part gap", "p_rot": "Rotation",
        "p_time": "Time per sheet", "p_seed": "Seed",
        "p_engine": "Solver", "p_engine_v": "spyrrow · sparrow",
        "p_parts": "Parts to cut", "p_unique": "Distinct parts",
        "p_area": "Part area",
        "warn": "BEFORE YOU CUT",
        "warn_sub": "check this before starting the machine",
        "warn_toobig": "Parts larger than the sheet",
        "warn_toobig_txt": "They do not fit the {sheet} usable area and were left "
                           "out of the plan: {names}. Split them or order a bigger sheet.",
        "warn_last": "The last sheet runs at {pct}",
        "warn_last_txt": "If you can wait for another job in the same material and "
                         "gauge, nesting them together raises the yield.",
        "warn_holes": "Holes are not nested into",
        "warn_holes_txt": "The solver does not nest parts inside other parts' "
                          "holes: a big hole counts as leftover.",
        "warn_drop": "The leftover is not tracked",
        "warn_drop_txt": "It is reported on the plan, but it is not registered as "
                         "reusable sheet remnant stock yet.",
        "warn_generic": "Job warning",
        "and_more": " and {n} more",
        "more_warn": "+ {n} more warning(s) · see the job JSON",
        "signoff": "SIGN-OFF",
        "sign": ["Checked", "Cut · operator", "Verified · QA", "Delivered · stores"],
        "meta_date": "DATE", "meta_sheets": "SHEETS", "meta_pieces": "PARTS",
        "meta_yield": "YIELD",
        "sheet_title": "Sheet {i} of {n}",
        "sheet_sub": "{material} · {size} · {n} parts",
        "sheet_sub1": "{material} · {size} · 1 part",
        "scale_note": "scale 1:{scale} · origin at the bottom-left corner · measurements in mm",
        "k_yield": "YIELD", "k_parts": "PARTS",
        "k_area": "AREA IN PARTS", "k_drop": "LEFTOVER",
        "sheet_parts": "PARTS ON THIS SHEET",
        "c_piece": "PART", "c_desc": "DESCRIPTION", "c_bbox": "BOUNDING BOX",
        "c_qty": "QTY",
        "operator": "FOR THE OPERATOR",
        "note_low": "This sheet runs at less than half. Before cutting it, check "
                    "with the shop whether it is worth waiting for another job in "
                    "the same material and gauge.",
        "note_ok": "Load the sheet with the {margin} margin clear on all four "
                   "sides. The part-to-part gap is {gap}: do not drag parts by "
                   "hand in the CAM or it is lost.",
        "note_nomargin": "Load the sheet squared against the stops. The "
                         "part-to-part gap is {gap}: do not drag parts by hand in "
                         "the CAM or it is lost.",
        "control": "CUT CHECKLIST",
        "ctl_loaded": "Sheet loaded and squared",
        "ctl_program": "Program {file} loaded",
        "ctl_counted": "Parts counted when unloading",
        "sheet": "sheet {p} / {n}",
        "foot_cover": "made with {brand} · nester.harriet.com.mx · {date}",
        "foot_params": "margin {margin} · gap {gap} · rotation {rot}",
        "foot_seed": "spyrrow solver · seed {seed}",
        "empty": "No part could be placed on a sheet.",
        "rot_free": "free", "rot_grain": "grain", "rot_fixed": "fixed",
        "rot_ortho": "ortho",
        "pdf_name": "{slug}_Cut_Plan.pdf",
    },
}


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def write_reports(
    result: NestResult, out_dir: str, job_name: str, meta: dict,
    warnings: List[str] | None = None,
) -> List[str]:
    """Write the nest JSON (always) and the cut-plan PDF (if reportlab). Returns paths.

    ``warnings`` (e.g. "nested DXF-per-sheet not written: ...") is carried into
    the JSON's ``warnings`` field so a caller that failed to produce some OTHER
    artifact (E4) still surfaces it in the machine-readable output — and is also
    printed on the plan's "antes de cortar" panel so the shop sees it on paper.
    """
    os.makedirs(out_dir, exist_ok=True)
    slug = _slug(job_name)
    written: List[str] = []

    json_path = os.path.join(out_dir, f"{slug}_nido.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(_as_dict(result, job_name, meta, warnings), fh, indent=2, ensure_ascii=False)
    written.append(json_path)

    if _HAVE_REPORTLAB:
        lang = meta.get("lang", "es")
        L = _LANG.get(lang, _LANG["es"])
        pdf_path = os.path.join(out_dir, L["pdf_name"].format(slug=slug))
        _write_pdf(result, pdf_path, job_name, meta, L, slug, warnings)
        written.append(pdf_path)

    return written


def _slug(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "nest"


def _part_base(name: str) -> str:
    """Friendly part label from a source filename.

    NFC-normalized (macOS hands back decomposed 'n'+tilde), extension dropped,
    the ``_4pz`` quantity token dropped — but the ``#n`` discriminator a
    multi-part DXF carries is KEPT: those are genuinely different shapes and
    must not collapse to one label (or one colour).

    'Tapa_Lateral_4pz.dxf#2' -> 'Tapa_Lateral #2'
    """
    base = unicodedata.normalize("NFC", name)
    stem, _, tail = base.partition("#")
    stem = re.sub(r"\.dxf$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-]\d+\s*(?:pz[a-z]*|pcs)$", "", stem, flags=re.IGNORECASE)
    return f"{stem} #{tail}" if tail else stem


# --------------------------------------------------------------------------- #
# JSON  (schema frozen — the service and the web product read this)
# --------------------------------------------------------------------------- #

def _as_dict(result: NestResult, job_name: str, meta: dict,
             warnings: List[str] | None = None) -> dict:
    spec = result.spec
    return {
        "job": job_name,
        "generated": meta.get("generated", ""),
        "warnings": list(warnings or []),
        "params": {
            "material": spec.material,
            "thickness": spec.thickness,
            "sheet_width": spec.width,
            "sheet_height": spec.height,
            "margin": spec.margin,
            "part_gap": spec.part_gap,
            "rotation": meta.get("rotation", ""),
        },
        "totals": {
            "sheets": result.sheet_count,
            "yield_pct": round(result.yield_pct, 2),
            "parts_placed": sum(s.part_count for s in result.sheets),
            "unplaceable": len(result.unplaceable),
        },
        "sheets": [
            {
                "sheet": s.index + 1,
                "utilization_pct": round(s.utilization * 100, 2),
                "parts": [
                    {
                        "name": p.part.name,
                        "x": round(p.x, 3),
                        "y": round(p.y, 3),
                        "rotation": round(p.rotation, 3),
                    }
                    for p in s.placements
                ],
            }
            for s in result.sheets
        ],
        "unplaceable": [
            {"name": p.name, "width": round(p.size[0], 2), "height": round(p.size[1], 2)}
            for p in result.unplaceable
        ],
    }


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _num(v: float, d: int = 1) -> str:
    return f"{v:,.{d}f}"


def _mm(v: float) -> str:
    """Human length: whole mm without a trailing .0, else one decimal."""
    return f"{v:.0f} mm" if abs(v - round(v)) < 0.05 else f"{v:.1f} mm"


def _mmn(v: float) -> str:
    return f"{_num(v, 0)} mm" if abs(v - round(v)) < 0.05 else f"{_num(v, 1)} mm"


def _dim(w: float, h: float) -> str:
    return f"{_num(w, 0)} × {_num(h, 0)} mm"


def _m2(area_mm2: float) -> str:
    return f"{area_mm2 / 1e6:,.3f}"


def _pct(v: float) -> str:
    return f"{v:.1f} %"


def _rot_label(L, rot: str) -> str:
    return L.get(f"rot_{rot}", rot or "—")


def _material_label(L, spec: SheetSpec) -> str:
    mat = spec.material.strip() if spec.material else ""
    if not mat:
        mat = L["material_unknown"]
    if spec.thickness:
        return f"{mat} · {spec.thickness:g} mm"
    return mat


# --------------------------------------------------------------------------- #
# Plan model — one entry per DISTINCT part, with its ID, colour and silhouette
# --------------------------------------------------------------------------- #

@dataclass
class _Piece:
    pid: str                       # 'P-01'
    part: FlatPart
    color: Tuple[float, float, float]
    qty: int = 0
    per_sheet: Counter = field(default_factory=Counter)
    _rings: Dict[float, Tuple[List[Point], List[List[Point]]]] = field(default_factory=dict)
    _anchor: Tuple[float, float, float] | None = None

    @property
    def desc(self) -> str:
        return _part_base(self.part.name)

    @property
    def size(self) -> Tuple[float, float]:
        return self.part.size

    @property
    def anchor(self) -> Tuple[float, float, float]:
        """Where the part's ID can be stamped: ``(x, y, clearance)`` in the part's
        own frame, ``clearance`` being the distance from that point to the nearest
        edge (outer or hole).

        NOT the bounding-box centre: for an L-bracket or a ring the box centre is
        in the notch or the hole, so the label would sit on bare sheet. This is a
        cheap pole-of-inaccessibility — a grid search for the deepest interior
        point — computed once per distinct part in its unrotated frame and then
        carried through the placement transform like any other point.
        """
        if self._anchor is None:
            self._anchor = _deep_point(*self.rings(_ANCHOR_TOL))
        return self._anchor

    def rings(self, tol_mm: float) -> Tuple[List[Point], List[List[Point]]]:
        """Outer + hole rings decimated for drawing, cached per tolerance.

        Decimation happens in the part's own frame; placement is a rigid
        rotate-then-translate, so simplifying before placing is equivalent to
        simplifying after — and it is done once per part instead of once per copy.
        """
        key = round(max(_TOL_BOUNDS[0], min(tol_mm, _TOL_BOUNDS[1])), 3)
        if key not in self._rings:
            outer = simplify_ring(self.part.outer, key, _DRAW_MAX_POINTS)
            holes = [h for h in (simplify_ring(x, key, _DRAW_MAX_POINTS)
                                 for x in self.part.holes) if len(h) >= 3]
            self._rings[key] = (outer, holes)
        return self._rings[key]


def _point_in_ring(x: float, y: float, ring: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _dist_to_rings(x: float, y: float, rings: Sequence[Sequence[Point]]) -> float:
    best = float("inf")
    for ring in rings:
        n = len(ring)
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 <= 0.0:
                d2 = (x - ax) ** 2 + (y - ay) ** 2
            else:
                t = ((x - ax) * dx + (y - ay) * dy) / seg2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                d2 = (x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2
            if d2 < best:
                best = d2
    return best ** 0.5 if best < float("inf") else 0.0


def _deep_point(outer: Sequence[Point], holes: Sequence[Sequence[Point]],
                grid: int = 21) -> Tuple[float, float, float]:
    """Grid-search the interior point furthest from every edge (x, y, clearance)."""
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    all_rings = [outer] + list(holes)
    best = (0.5 * (x0 + x1), 0.5 * (y0 + y1), 0.0)
    for i in range(1, grid):
        px_ = x0 + (x1 - x0) * i / grid
        for j in range(1, grid):
            py_ = y0 + (y1 - y0) * j / grid
            if not _point_in_ring(px_, py_, outer):
                continue
            if any(_point_in_ring(px_, py_, h) for h in holes):
                continue
            d = _dist_to_rings(px_, py_, all_rings)
            if d > best[2]:
                best = (px_, py_, d)
    return best


def _build_pieces(result: NestResult) -> Tuple[List[_Piece], Dict[str, _Piece]]:
    """Assign a stable ID and colour per DISTINCT part; count copies per sheet.

    Identity is the source name (a multi-part DXF's ``#n`` included), so two
    different shapes never share a colour. IDs run biggest-part-first, which is
    the order the shop reads the drawing in.
    """
    seen: Dict[str, FlatPart] = {}
    for s in result.sheets:
        for pl in s.placements:
            seen.setdefault(pl.part.name, pl.part)
    for p in result.unplaceable:
        seen.setdefault(p.name, p)

    ranked = sorted(seen.values(), key=lambda p: (-p.area, p.name))
    by_name: Dict[str, _Piece] = {}
    for i, part in enumerate(ranked, start=1):
        by_name[part.name] = _Piece(pid=f"P-{i:02d}", part=part,
                                    color=PALETTE[(i - 1) % len(PALETTE)])
    for s in result.sheets:
        for pl in s.placements:
            pc = by_name[pl.part.name]
            pc.qty += 1
            pc.per_sheet[s.index] += 1
    return [by_name[p.name] for p in ranked], by_name


def _sheet_groups(layout: SheetLayout, by_name: Dict[str, _Piece]) -> List[Tuple[_Piece, int]]:
    counts: Counter = Counter(pl.part.name for pl in layout.placements)
    groups = [(by_name[n], k) for n, k in counts.items()]
    groups.sort(key=lambda g: (-g[1], g[0].pid))
    return groups


# --------------------------------------------------------------------------- #
# Silhouette drawing — the real shapes, never a bounding box
# --------------------------------------------------------------------------- #

def _shape_path(c, rings: Sequence[Sequence[Point]], deg: float, tx: float,
                ty: float, ox: float, oy: float, s: float):
    """One PDF path holding the outer ring and every hole, in page points.

    Filled even-odd, so a hole is a real hole: it lets whatever is behind the
    part show through instead of being painted over in paper-white.
    """
    p = c.beginPath()
    drawn = 0
    for ring in rings:
        pts = transform(ring, deg, tx, ty)
        if len(pts) < 3:
            continue
        p.moveTo(ox + pts[0][0] * s, oy + pts[0][1] * s)
        for (x, y) in pts[1:]:
            p.lineTo(ox + x * s, oy + y * s)
        p.close()
        drawn += 1
    return p if drawn else None


def _draw_piece(c, piece: _Piece, deg: float, tx: float, ty: float,
                ox: float, oy: float, s: float, tol_mm: float,
                stroke_w: float = 0.0) -> None:
    outer, holes = piece.rings(tol_mm)
    path = _shape_path(c, [outer] + holes, deg, tx, ty, ox, oy, s)
    if path is None:
        return
    c.setFillColorRGB(*piece.color)
    if stroke_w > 0:
        c.setStrokeColorRGB(*PAPER)
        c.setLineWidth(stroke_w)
        c.drawPath(path, stroke=1, fill=1, fillMode=FILL_EVEN_ODD)
    else:
        c.drawPath(path, stroke=0, fill=1, fillMode=FILL_EVEN_ODD)


def _draw_nest(c, layout: SheetLayout, by_name: Dict[str, _Piece], x0: float,
               y0: float, s: float, *, mini: bool, L=None) -> None:
    """Draw one sheet: border, margin band, then every placed silhouette.

    ``(x0, y0)`` is the bottom-left corner of the sheet on the page, ``s`` the
    scale in points per millimetre. Sheet coordinates and PDF coordinates both
    grow upward, so the mapping is a plain scale + offset.
    """
    spec = layout.spec
    w, h = spec.width * s, spec.height * s
    tol = max(_TOL_BOUNDS[0], min(_DRAW_TOL_PT / s if s else 1.0, _TOL_BOUNDS[1]))

    rect(c, x0, y0, w, h, fill=PAPER, stroke=INK, lw=px(1) if mini else px(1.5))
    if spec.margin > 0:
        m = spec.margin * s
        if not mini and w - 2 * m > 0 and h - 2 * m > 0:
            rect(c, x0 + m, y0 + m, w - 2 * m, h - 2 * m, stroke=DASH_RULE,
                 lw=px(1.2), dash=(px(6), px(4.5)))

    for pl in layout.placements:
        piece = by_name.get(pl.part.name)
        if piece is None:  # pragma: no cover - every placement is catalogued
            continue
        _draw_piece(c, piece, pl.rotation, pl.x, pl.y, x0, y0, s, tol,
                    stroke_w=px(0.6) if mini else px(1.1))

    if mini:
        return

    # part IDs, stamped only where the material actually has room for one
    fs = px(9)
    for pl in layout.placements:
        piece = by_name.get(pl.part.name)
        if piece is None:  # pragma: no cover
            continue
        ax, ay, clear = piece.anchor
        room = 2.0 * clear * s
        if room < tw(c, piece.pid, MONO_B, fs) + px(4) or room < px(12):
            continue
        lx, ly = transform([(ax, ay)], pl.rotation, pl.x, pl.y)[0]
        txt(c, x0 + lx * s, y0 + ly * s - fs * 0.36, piece.pid, MONO_B, fs,
            PAPER, align="c")


def _swatch(c, piece: _Piece, x: float, y: float, w: float, h: float) -> None:
    """The part's silhouette, shrunk into a table cell — the design's colour key."""
    pw, ph = piece.size
    if pw <= 0 or ph <= 0:
        rect(c, x, y, w, h, fill=piece.color)
        return
    s = min(w / pw, h / ph)
    bx0, by0, _, _ = piece.part.bbox
    ox = x + (w - pw * s) / 2 - bx0 * s
    oy = y + (h - ph * s) / 2 - by0 * s
    tol = max(_TOL_BOUNDS[0], min(_DRAW_TOL_PT / s if s else 1.0, _TOL_BOUNDS[1]))
    _draw_piece(c, piece, 0.0, 0.0, 0.0, ox, oy, s, tol)


def _scale_den(mm_total: float, pts: float) -> int:
    """The '1:N' denominator: real mm per mm of paper."""
    on_paper = pts / 72.0 * 25.4
    return max(1, round(mm_total / on_paper)) if on_paper > 0 else 1


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _write_pdf(result, path, job_name, meta, L, slug, warnings=None) -> None:
    """Render twice: pass 1 counts the sheets, pass 2 stamps 'hoja N / total'."""
    buf = BytesIO()
    total = _render(buf, result, job_name, meta, L, slug, warnings, total=None)
    with open(path, "wb") as fh:
        _render(fh, result, job_name, meta, L, slug, warnings, total=total)


def _render(dest, result: NestResult, job_name, meta, L, slug, warnings, total):
    PW, PH = landscape(A4)
    c = canvas.Canvas(dest, pagesize=(PW, PH))
    c.setTitle(f"{L['title']} — {job_name}")

    pad_l, pad_t, pad_b = px(34), px(26), px(16)
    cw = PW - 2 * pad_l
    spec = result.spec
    brand = meta.get("brand", L["brand"])
    monogram = meta.get("monogram", "HN")
    generated = meta.get("generated") or ""
    subtitle = meta.get("client") or meta.get("subtitle") or ""
    rot = _rot_label(L, str(meta.get("rotation", "")))

    pieces, by_name = _build_pieces(result)
    state = {"page": 0}

    foot_params = L["foot_params"].format(margin=_mm(spec.margin),
                                          gap=_mm(spec.part_gap), rot=rot)

    def footer(note: str = "") -> None:
        y = pad_b + px(20)
        line(c, pad_l, y, PW - pad_l, y, RULE)
        by = pad_b + px(5)
        fs = px(8.5)
        left = f"{job_name} · {subtitle}" if subtitle else job_name
        txt(c, pad_l, by, ellipsize(c, left, MONO, fs, cw * 0.4), MONO, fs, MID)
        stamp = L["sheet"].format(p=state["page"], n=total if total else "?")
        wstamp = tw(c, stamp, MONO_B, fs)
        txt(c, PW - pad_l, by, stamp, MONO_B, fs, INK, align="r")
        if note:
            txt(c, PW - pad_l - wstamp - px(14), by,
                ellipsize(c, note, MONO, fs, cw * 0.5), MONO, fs, FAINT, align="r")

    def page_break(note: str = "") -> None:
        footer(note)
        c.showPage()

    def header(title, sub, right1="", right2="") -> float:
        """The 2px-rule sheet header. Returns the y of the content top."""
        ts = px(15)
        base = PH - pad_t - ts * 0.80
        x = pad_l + txt(c, pad_l, base, title, SANS_B, ts, INK, track=-ts * 0.01)
        if sub:
            txt(c, x + px(12), base, ellipsize(c, sub, MONO, px(11), cw * 0.52),
                MONO, px(11), SOFT)
        rx = PW - pad_l
        if right2:
            rx -= txt(c, rx, base, right2, MONO, px(11), SOFT, align="r") + px(12)
        if right1:
            txt(c, rx, base, ellipsize(c, right1, MONO_B, px(11), cw * 0.25),
                MONO_B, px(11), MID, align="r")
        ry = base - px(9)
        line(c, pad_l, ry, PW - pad_l, ry, INK, px(2))
        return ry - px(12)

    # ---------------- sheet 1: portada ----------------
    state["page"] += 1
    _cover(c, L, result, pieces, meta, job_name, subtitle, brand, monogram,
           generated, rot, warnings, PW, PH, pad_l, pad_t, pad_b, cw, by_name)
    note = L["foot_seed"].format(seed=meta["seed"]) if meta.get("seed") is not None else brand
    page_break(L["foot_cover"].format(brand=brand, date=generated) if generated else note)

    # ---------------- one drawing sheet per lámina ----------------
    pad = max(2, len(str(result.sheet_count)))
    for layout in result.sheets:
        state["page"] += 1
        fname = f"{slug}_S{str(layout.index + 1).zfill(pad)}.dxf"
        n = layout.part_count
        top = header(
            L["sheet_title"].format(i=layout.index + 1, n=result.sheet_count),
            (L["sheet_sub1"] if n == 1 else L["sheet_sub"]).format(
                material=_material_label(L, spec), size=_dim(spec.width, spec.height), n=n),
            job_name, fname)
        _sheet_page(c, L, layout, by_name, meta, fname, pad_l, cw, top,
                    pad_b + px(28))
        page_break(foot_params)

    c.save()
    return state["page"]


# --------------------------------------------------------------------------- #
# Sheet 1 — portada
# --------------------------------------------------------------------------- #

def _cover(c, L, result, pieces, meta, job_name, subtitle, brand, monogram,
           generated, rot, warnings, PW, PH, pad_l, pad_t, pad_b, cw, by_name):
    spec = result.spec
    placed = sum(s.part_count for s in result.sheets)

    # ---- masthead ----
    top = PH - pad_t
    box = px(30)
    rect(c, pad_l, top - box, box, box, stroke=INK, lw=px(1.5))
    txt(c, pad_l + box / 2, top - box + px(10), monogram, MONO_B, px(11), INK,
        track=px(0.35), align="c")

    tx = pad_l + box + px(16)
    ts = px(24)
    base = top - ts * 0.78
    w = txt(c, tx, base, L["title"], SANS_B, ts, INK, track=-ts * 0.015)
    txt(c, tx + w + px(12), base, job_name, MONO_B, px(15), MID)
    if subtitle:
        txt(c, tx, base - px(15), ellipsize(c, subtitle, SANS, px(12.5), cw * 0.5),
            SANS, px(12.5), SOFT)

    metas: List[Tuple[str, str]] = []
    if generated:
        metas.append((L["meta_date"], generated.split(" ")[0]))
    metas += [(L["meta_sheets"], str(result.sheet_count)),
              (L["meta_pieces"], str(placed)),
              (L["meta_yield"], _pct(result.yield_pct))]
    mx = PW - pad_l
    for lb, v in reversed(metas):
        wv = max(tw(c, lb, MONO_B, px(8), px(8) * 0.12), tw(c, v, MONO_B, px(11.5)))
        txt(c, mx - wv, top - px(8), lb, MONO_B, px(8), FAINT, track=px(8) * 0.12)
        txt(c, mx - wv, top - px(21), v, MONO_B, px(11.5), INK)
        mx -= wv + px(26)

    ry = top - box - px(12)
    line(c, pad_l, ry, PW - pad_l, ry, INK, px(2))

    # ---- two-column body ----
    body_top = ry - px(15)
    body_bot = pad_b + px(28)
    right_w = px(300)
    gap = px(22)
    left_w = cw - right_w - gap
    right_x = pad_l + left_w + gap

    _params_column(c, L, result, pieces, meta, rot, warnings, right_x, right_w,
                   body_top, body_bot)
    line(c, right_x - px(11), body_bot, right_x - px(11), body_top, RULE)

    acc_h = px(52)
    y = body_top
    y = _buy_table(c, L, result, pieces, rot, pad_l, left_w, y)
    y -= px(15)
    _how_it_went(c, L, result, pieces, by_name, pad_l, left_w, y,
                 body_bot + acc_h + px(14))
    _accounts(c, L, result, pad_l, left_w, body_bot + acc_h)


def _buy_table(c, L, result, pieces, rot, x, w, y) -> float:
    spec = result.spec
    kicker(c, x, y, L["buy"])
    txt(c, x + tw(c, L["buy"], MONO_B, px(9), px(9) * 0.14) + px(10), y,
        L["buy_sub"], SANS, px(10.5), FAINT)
    y -= px(11)

    pad = px(11)
    c_yield = x + w - pad
    c_pcs = c_yield - px(96)
    c_sheets = c_pcs - px(78)
    c_size = c_sheets - px(84)

    head_h, row_h = px(19), px(30)
    top = y
    rect(c, x, top - head_h - row_h, w, head_h + row_h, stroke=INK, lw=px(1))
    rect(c, x, top - head_h, w, head_h, fill=INK)
    hy = top - head_h + px(6)
    hs = px(8.5)
    txt(c, x + pad, hy, L["h_material"], MONO_B, hs, PAPER, track=hs * 0.11)
    for lbl, cx in ((L["h_size"], c_size), (L["h_sheets"], c_sheets),
                    (L["h_pcs"], c_pcs), (L["h_yield"], c_yield)):
        txt(c, cx, hy, lbl, MONO_B, hs, PAPER, track=hs * 0.11, align="r")

    ry = top - head_h
    txt(c, x + pad, ry - px(13),
        ellipsize(c, _material_label(L, spec), SANS_B, px(13), c_size - x - pad - px(10)),
        SANS_B, px(13), INK)
    sub = L["material_sub"].format(margin=_mm(spec.margin), gap=_mm(spec.part_gap), rot=rot)
    txt(c, x + pad, ry - px(24),
        ellipsize(c, sub, MONO, px(9.5), c_size - x - pad - px(10)), MONO, px(9.5), SOFT)
    my = ry - px(19)
    placed = sum(s.part_count for s in result.sheets)
    txt(c, c_size, my, _dim(spec.width, spec.height), MONO, px(12), INK, align="r")
    txt(c, c_sheets, my, str(result.sheet_count), MONO_B, px(15), INK, align="r")
    txt(c, c_pcs, my, str(placed), MONO, px(12), INK, align="r")
    txt(c, c_yield, my, _pct(result.yield_pct), MONO_B, px(13), INK, align="r")
    return ry - row_h


def _how_it_went(c, L, result, pieces, by_name, x, w, y, floor) -> float:
    """The mini nests of every sheet, beside the job's part list."""
    spec = result.spec
    kicker(c, x, y, L["howitwent"])
    y -= px(11)

    band_w = w * 0.46
    list_x = x + band_w + px(14)
    list_w = w - band_w - px(14)

    aspect = spec.width / spec.height if spec.height else 1.0
    n = len(result.sheets)
    gap, label_h = px(10), px(30)
    avail = y - floor + px(8)

    if not n:                       # nothing could be nested — say so, plainly
        txt(c, x, y - px(10), L["empty"], SANS, px(11), ACC_TXT)
        _parts_list(c, L, pieces, list_x, list_w, y, floor)
        return floor

    # Pick the column count that draws the BIGGEST mini in the space available
    # (ties go to more columns, i.e. fewer rows). A mini nobody can read is not
    # worth the ink, and the grid's shape depends on the stock sheet's aspect.
    cols, cell_w, cell_h, best = 1, 0.0, 0.0, -1.0
    for k in range(1, min(n, 4) + 1):
        rows = (n + k - 1) // k
        cwid = (band_w - (k - 1) * gap) / k
        chgt = min(cwid / aspect if aspect else cwid, px(230))
        room = (avail - rows * (label_h + px(8))) / rows
        if chgt > room:
            chgt = room
            cwid = chgt * aspect
        cwid = min(cwid, chgt * aspect)
        if chgt > 0 and cwid * chgt >= best:
            cols, cell_w, cell_h, best = k, cwid, chgt, cwid * chgt
    row_h = cell_h + label_h + px(8)
    rows_room = max(1, int(avail // row_h))
    fit = cols * rows_room
    shown = result.sheets if n <= fit else result.sheets[:max(1, fit - 1)]

    for i, layout in enumerate(shown):
        r, k = divmod(i, cols)
        cx = x + k * (cell_w + gap)
        cy = y - (r + 1) * cell_h - r * (label_h + px(8))
        s = min(cell_w / spec.width, cell_h / spec.height)
        dw, dh = spec.width * s, spec.height * s
        _draw_nest(c, layout, by_name, cx + (cell_w - dw) / 2, cy, s, mini=True)
        ly = cy - px(10)
        mid = cx + cell_w / 2
        txt(c, mid, ly, f"L{layout.index + 1}", MONO_B, px(10.5), INK, align="c")
        txt(c, mid, ly - px(10), _pct(layout.utilization * 100), MONO, px(9.5),
            SOFT, align="c")
        pcs = layout.part_count
        txt(c, mid, ly - px(19),
            (L["mini_pcs1"] if pcs == 1 else L["mini_pcs"]).format(n=pcs),
            MONO, px(9), FAINT, align="c")

    rows_used = (len(shown) + cols - 1) // cols if shown else 0
    y_after = y - rows_used * row_h
    if len(shown) < n:
        txt(c, x, y_after + px(2), L["more_sheets"].format(n=n - len(shown)),
            MONO, px(9), FAINT)

    _parts_list(c, L, pieces, list_x, list_w, y, floor)
    return min(y_after, floor)


def _parts_list(c, L, pieces, x, w, y, floor) -> float:
    kicker(c, x, y, L["parts_job"])
    y -= px(12)

    sw_w, id_w, bbox_w, rep_w, qty_w = px(24), px(46), px(84), px(58), px(30)
    gap = px(7)
    desc_x = x + sw_w + id_w + 2 * gap
    desc_w = max(px(40), w - sw_w - id_w - bbox_w - rep_w - qty_w - 5 * gap)
    row_h = px(17)
    room = max(1, int((y - floor) // row_h))
    shown = pieces[:room] if len(pieces) > room else pieces
    if len(shown) < len(pieces):
        shown = pieces[:max(1, room - 1)]

    for pc in shown:
        y -= row_h
        ty = y + px(4)
        _swatch(c, pc, x, y + px(2), sw_w, px(13))
        txt(c, x + sw_w + gap, ty, pc.pid, MONO_B, px(10.5), INK)
        txt(c, desc_x, ty, ellipsize(c, pc.desc, SANS, px(10.5), desc_w),
            SANS, px(10.5), MID)
        bw, bh = pc.size
        txt(c, desc_x + desc_w + gap + bbox_w, ty, _dim(bw, bh), MONO, px(9),
            SOFT, align="r")
        # where the copies landed — or, for a part the solver could not place,
        # why there are none to land
        rep = (" ".join(f"L{i + 1}×{k}" for i, k in sorted(pc.per_sheet.items()))
               if pc.qty else L["not_placed"])
        txt(c, desc_x + desc_w + 2 * gap + bbox_w + rep_w, ty,
            ellipsize(c, rep, MONO, px(9), rep_w), MONO, px(9),
            FAINT if pc.qty else ACC_TXT, align="r")
        txt(c, x + w, ty, f"× {pc.qty}" if pc.qty else "—", MONO_B, px(11), INK,
            align="r")
        line(c, x, y, x + w, y, HAIR2)
    if len(shown) < len(pieces):
        y -= px(12)
        txt(c, x, y, L["more_parts"].format(n=len(pieces) - len(shown)), MONO,
            px(9), FAINT)
    return y


def _accounts(c, L, result, x, w, y) -> None:
    spec = result.spec
    line(c, x, y + px(30), x + w, y + px(30), RULE)
    kicker(c, x, y + px(18), L["accounts"])

    bought = result.total_sheet_area
    parts = result.total_part_area
    drop = max(0.0, bought - parts)

    cells = [
        (L["a_sheets"], str(result.sheet_count), False),
        (L["a_bought"], f"{_m2(bought)} m²", False),
        (L["a_parts"], f"{_m2(parts)} m²", False),
        (L["a_drop"], f"{_m2(drop)} m²", False),
        (L["a_yield"], _pct(result.yield_pct), True),
    ]
    cx = x
    for i, (lb, v, big) in enumerate(cells):
        vs = px(16) if big else px(12.5)
        cwid = max(tw(c, lb, MONO_B, px(8), px(8) * 0.1), tw(c, v, MONO_B, vs))
        txt(c, cx, y + px(3), lb, MONO_B, px(8), FAINT, track=px(8) * 0.1)
        txt(c, cx, y - px(11), v, MONO_B, vs, INK)
        cx += cwid + px(14)
        if i < len(cells) - 1:
            line(c, cx - px(7), y - px(13), cx - px(7), y + px(6), HAIR)

    txt(c, x, y - px(23),
        ellipsize(c, L["formula"].format(parts=_m2(parts), stock=_m2(bought)),
                  MONO, px(9), w), MONO, px(9), FAINT)


def _params_column(c, L, result, pieces, meta, rot, warnings, x, w, y, floor):
    spec = result.spec
    kicker(c, x, y, L["params"])
    y -= px(9)

    placed = sum(s.part_count for s in result.sheets)
    rows: List[Tuple[str, str]] = [
        (L["p_material"], ellipsize(c, spec.material or "—", MONO_B, px(12), w * 0.6)),
    ]
    if spec.thickness:
        rows.append((L["p_thickness"], _mm(spec.thickness)))
    rows += [
        (L["p_sheet"], _dim(spec.width, spec.height)),
        (L["p_margin"], _mm(spec.margin)),
        (L["p_gap"], _mm(spec.part_gap)),
        (L["p_rot"], rot),
    ]
    if meta.get("time_per_sheet"):
        rows.append((L["p_time"], f"{int(meta['time_per_sheet'])} s"))
    if meta.get("seed") is not None:
        rows.append((L["p_seed"], str(meta["seed"])))
    rows += [
        (L["p_unique"], str(len(pieces))),
        (L["p_parts"], str(placed)),
        (L["p_area"], f"{_m2(result.total_part_area)} m²"),
        (L["p_engine"], L["p_engine_v"]),
    ]
    for i, (lb, v) in enumerate(rows):
        y -= px(15)
        txt(c, x, y, lb, SANS, px(10.5), MID)
        txt(c, x + w, y, v, MONO_B, px(12), INK, align="r")
        if i < len(rows) - 1:
            line(c, x, y - px(5), x + w, y - px(5), HAIR2)
    y -= px(20)

    sign_top = floor + px(58)
    cards = _warning_cards(c, L, result, warnings)
    if cards:
        _warning_panel(c, L, cards, x, w, y, sign_top + px(24))

    line(c, x, sign_top + px(14), x + w, sign_top + px(14), RULE)
    kicker(c, x, sign_top + px(2), L["signoff"])
    signature_block(c, L["sign"], x, sign_top - px(14), w)


def _warning_cards(c, L, result, warnings) -> List[Tuple[str, str]]:
    """The cards the shop must read: real problems first, engine limits last."""
    cards: List[Tuple[str, str]] = []
    spec = result.spec
    if result.unplaceable:
        shown = result.unplaceable[:4]
        names = ", ".join(
            f"{_part_base(p.name)} ({_num(p.size[0], 0)}×{_num(p.size[1], 0)})"
            for p in shown)
        if len(result.unplaceable) > len(shown):
            names += L["and_more"].format(n=len(result.unplaceable) - len(shown))
        cards.append((L["warn_toobig"],
                      L["warn_toobig_txt"].format(
                          sheet=_dim(spec.usable_width, spec.usable_height),
                          names=names)))
    for w in (warnings or []):
        head, _, rest = str(w).partition(": ")
        cards.append((head, rest) if rest else (L["warn_generic"], head))
    if result.sheets:
        last = result.sheets[-1]
        if len(result.sheets) > 1 and last.utilization < _LOW_YIELD:
            cards.append((L["warn_last"].format(pct=_pct(last.utilization * 100)),
                          L["warn_last_txt"]))
    if any(p.part.holes for p in
           (pl for s in result.sheets for pl in s.placements)):
        cards.append((L["warn_holes"], L["warn_holes_txt"]))
    cards.append((L["warn_drop"], L["warn_drop_txt"]))
    return cards


def _warning_panel(c, L, cards, x, w, y, floor) -> float:
    kicker(c, x, y, L["warn"])
    txt(c, x + tw(c, L["warn"], MONO_B, px(9), px(9) * 0.14) + px(10), y,
        L["warn_sub"], SANS, px(10.5), FAINT)
    y -= px(11)
    for i, (title, body) in enumerate(cards, start=1):
        wrapped = wrap(c, body, SANS, px(10), w - px(40))
        lines = wrapped[:3]
        if len(wrapped) > 3 and lines:
            lines[-1] = ellipsize(c, lines[-1] + " …", SANS, px(10), w - px(40))
        h = px(11) + px(13) + px(13) * len(lines)
        if y - h < floor:
            txt(c, x, y - px(9), L["more_warn"].format(n=len(cards) - i + 1),
                MONO, px(9), ACC_TXT)
            y -= px(13)
            break
        rect(c, x, y - h, w, h, fill=ACC_BG, stroke=HAIR, lw=px(1))
        rect(c, x, y - h, px(3), h, fill=ACC_BAR)
        ny = y - px(15)
        rect(c, x + px(9), ny - px(3), px(14), px(13), stroke=ACC_BRD, lw=px(1))
        txt(c, x + px(16), ny, str(i), MONO_B, px(10), ACC_TXT, align="c")
        txt(c, x + px(30), ny, ellipsize(c, title, SANS_B, px(11), w - px(40)),
            SANS_B, px(11), INK)
        for j, ln in enumerate(lines):
            txt(c, x + px(30), ny - px(12) - j * px(13), ln, SANS, px(10), MID)
        y -= h + px(6)
    return y


# --------------------------------------------------------------------------- #
# Sheets 2..n+1 — one drawing per lámina
# --------------------------------------------------------------------------- #

def _sheet_page(c, L, layout, by_name, meta, fname, x, w, top, bottom) -> None:
    """The nest drawn to scale, plus the panel that explains it.

    Two arrangements, picked from the stock sheet's aspect: a tall sheet takes
    the artboard's drawing-left / panel-right split; a wide sheet (the common
    4×8 laid landscape) is drawn full width with the panel underneath, so the
    drawing is never squeezed into a letterbox.
    """
    spec = layout.spec
    groups = _sheet_groups(layout, by_name)
    cap_h = px(15)
    avail_h = top - bottom - cap_h
    aspect = (spec.width / spec.height) if spec.height else 1.0
    gap = px(22)
    panel_min = px(360)
    tall_max_w = w - panel_min - gap

    if avail_h * aspect <= tall_max_w:                       # drawing left
        dh = avail_h
        dw = dh * aspect
        s = dh / spec.height if spec.height else 1.0
        _draw_nest(c, layout, by_name, x, top - dh, s, mini=False, L=L)
        _scale_caption(c, L, spec, x, top - dh - px(11), dw)
        _sheet_panel(c, L, layout, groups, fname, x + dw + gap,
                     w - dw - gap, top, bottom, kpi_cols=2, split=False)
    else:                                                    # drawing on top
        # give the drawing every point the panel does not need — a nest read
        # from paper is only as good as how big it is printed
        panel_h = _panel_height(c, L, layout, groups, w, cols=4, split=True)
        dh = min(w / aspect if aspect else avail_h,
                 max(avail_h * 0.42, avail_h - panel_h - px(10)))
        dw = dh * aspect
        s = dw / spec.width if spec.width else 1.0
        dx = x + (w - dw) / 2
        _draw_nest(c, layout, by_name, dx, top - dh, s, mini=False, L=L)
        _scale_caption(c, L, spec, dx, top - dh - px(11), dw)
        _sheet_panel(c, L, layout, groups, fname, x, w,
                     top - dh - cap_h - px(10), bottom, kpi_cols=4, split=True)


def _panel_height(c, L, layout, groups, w, cols, split) -> float:
    """What the explanatory panel needs, so the drawing can claim the rest."""
    rows = (4 + cols - 1) // cols
    h = rows * px(38) + (rows - 1) * px(9) + px(13)
    table_h = px(11) + px(16) + len(groups) * px(17)
    note_w = (w - w * 0.58 - px(22)) if split else w
    note_h = px(12) + px(14) * len(wrap(c, _operator_body(L, layout), SANS,
                                        px(10.5), note_w))
    h += max(table_h, note_h) if split else table_h + px(13) + note_h
    return h + px(14) + px(34)                                # control block


def _scale_caption(c, L, spec, x, y, dw) -> None:
    s = L["scale_note"].format(scale=_scale_den(spec.width, dw))
    # centred under the drawing, unless the line is wider than the drawing —
    # then it would hang off the left edge of the sheet, so pin it left.
    if tw(c, s, MONO, px(9)) > dw:
        txt(c, x, y, s, MONO, px(9), FAINT)
    else:
        txt(c, x + dw / 2, y, s, MONO, px(9), FAINT, align="c")


def _sheet_panel(c, L, layout, groups, fname, x, w, top, bottom, kpi_cols, split):
    y = _kpi_grid(c, L, layout, x, w, top, cols=kpi_cols)
    y -= px(13)
    if split:
        table_w = w * 0.58
        side_x = x + table_w + px(22)
        side_w = w - table_w - px(22)
        _sheet_parts_table(c, L, groups, x, table_w, y, bottom)
        ny = _operator_note(c, L, layout, side_x, side_w, y)
        _control_block(c, L, fname, side_x, side_w, min(ny, bottom + px(46)))
    else:
        y = _sheet_parts_table(c, L, groups, x, w, y, bottom + px(76))
        y = _operator_note(c, L, layout, x, w, y - px(13))
        _control_block(c, L, fname, x, w, bottom + px(46))


def _kpi_grid(c, L, layout, x, w, y, cols=2) -> float:
    spec = layout.spec
    drop = max(0.0, spec.area - layout.used_area)
    cells = [(L["k_yield"], _pct(layout.utilization * 100)),
             (L["k_parts"], str(layout.part_count)),
             (L["k_area"], f"{_m2(layout.used_area)} m²"),
             (L["k_drop"], f"{_m2(drop)} m²")]
    gap = px(9)
    cw_ = (w - (cols - 1) * gap) / cols
    ch = px(38)
    rows = (len(cells) + cols - 1) // cols
    for i, (lb, v) in enumerate(cells):
        r, k = divmod(i, cols)
        cx = x + k * (cw_ + gap)
        cy = y - (r + 1) * ch - r * gap
        rect(c, cx, cy, cw_, ch, fill=ZEBRA, stroke=HAIR, lw=px(1))
        txt(c, cx + px(10), cy + ch - px(11), lb, MONO_B, px(8), FAINT,
            track=px(8) * 0.1)
        txt(c, cx + px(10), cy + px(9), v, MONO_B, px(15), INK)
    return y - rows * ch - (rows - 1) * gap


def _sheet_parts_table(c, L, groups, x, w, y, floor) -> float:
    kicker(c, x, y, L["sheet_parts"])
    y -= px(11)

    pad = px(9)
    sw_w, id_w, qty_w, bbox_w = px(24), px(50), px(46), px(88)
    gap = px(8)
    head_h, row_h = px(16), px(17)
    desc_x = x + pad + sw_w + id_w + 2 * gap
    desc_w = max(px(40), (x + w - pad - qty_w - bbox_w - 2 * gap) - desc_x)

    room = max(1, int((y - floor - head_h) // row_h))
    shown = groups if len(groups) <= room else groups[:max(1, room - 1)]
    box_h = head_h + len(shown) * row_h + (px(13) if len(shown) < len(groups) else 0)
    rect(c, x, y - box_h, w, box_h, stroke=HAIR, lw=px(1))
    rect(c, x, y - head_h, w, head_h, fill=INK)
    hy = y - head_h + px(5)
    hs = px(8)
    txt(c, x + pad + sw_w + gap, hy, L["c_piece"], MONO_B, hs, PAPER, track=hs * 0.1)
    txt(c, desc_x, hy, L["c_desc"], MONO_B, hs, PAPER, track=hs * 0.1)
    txt(c, x + w - pad - qty_w - gap, hy, L["c_bbox"], MONO_B, hs, PAPER,
        track=hs * 0.1, align="r")
    txt(c, x + w - pad, hy, L["c_qty"], MONO_B, hs, PAPER, track=hs * 0.1, align="r")
    y -= head_h

    for i, (pc, n) in enumerate(shown):
        ry = y - row_h
        if i % 2:
            rect(c, x + px(1), ry, w - px(2), row_h, fill=ZEBRA)
        ty = ry + row_h / 2 - px(3.5)
        _swatch(c, pc, x + pad, ry + px(2), sw_w, row_h - px(4))
        txt(c, x + pad + sw_w + gap, ty, pc.pid, MONO_B, px(10.5), INK)
        txt(c, desc_x, ty, ellipsize(c, pc.desc, SANS, px(10.5), desc_w),
            SANS, px(10.5), MID)
        bw, bh = pc.size
        txt(c, x + w - pad - qty_w - gap, ty, _dim(bw, bh), MONO, px(9), SOFT,
            align="r")
        txt(c, x + w - pad, ty, str(n), MONO_B, px(11), INK, align="r")
        line(c, x + px(1), ry, x + w - px(1), ry, HAIR2)
        y = ry
    if len(shown) < len(groups):
        txt(c, x + pad, y - px(9), L["more_parts"].format(n=len(groups) - len(shown)),
            MONO, px(9), FAINT)
        y -= px(13)
    return y


def _operator_body(L, layout) -> str:
    spec = layout.spec
    if layout.utilization < _LOW_YIELD:
        return L["note_low"]
    if spec.margin > 0:
        return L["note_ok"].format(margin=_mm(spec.margin), gap=_mm(spec.part_gap))
    return L["note_nomargin"].format(gap=_mm(spec.part_gap))


def _operator_note(c, L, layout, x, w, y) -> float:
    kicker(c, x, y, L["operator"])
    y -= px(12)
    for ln in wrap(c, _operator_body(L, layout), SANS, px(10.5), w):
        txt(c, x, y, ln, SANS, px(10.5), MID)
        y -= px(14)
    return y


def _control_block(c, L, fname, x, w, y) -> None:
    line(c, x, y + px(14), x + w, y + px(14), RULE)
    kicker(c, x, y + px(2), L["control"])
    items = [L["ctl_loaded"], L["ctl_program"].format(file=os.path.splitext(fname)[0]),
             L["ctl_counted"]]
    cx, cy = x, y - px(14)
    for it in items:
        wd = px(13) + px(7) + tw(c, it, MONO, px(10))
        if cx > x and cx + wd > x + w:
            cx = x
            cy -= px(16)
        rect(c, cx, cy - px(2), px(13), px(13), stroke=FAINT, lw=px(1))
        txt(c, cx + px(20), cy + px(1), it, MONO, px(10), MID)
        cx += wd + px(16)
