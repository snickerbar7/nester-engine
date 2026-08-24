"""Output for flat-sheet nesting: machine JSON (always) + a cut-plan PDF.

Mirrors ``nester.tube.report``: JSON is always written; the PDF is drawn only if
reportlab is importable. Strings are bilingual (es default / en) via a ``_LANG``
table. The PDF shows a shopping/summary box, one scaled diagram per sheet with
parts colored by source, a parts guide, and any unplaceable parts.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Dict, List, Sequence, Tuple

from .model import NestResult, Point, SheetLayout
from .pack import transform

try:  # optional dependency — JSON works without it
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm as MM
    from reportlab.pdfgen import canvas
    _HAVE_REPORTLAB = True
except ImportError:  # pragma: no cover
    _HAVE_REPORTLAB = False

# Print-friendly fill colors (RGB 0-1); same source file -> same color.
_PALETTE: List[Tuple[float, float, float]] = [
    (0.20, 0.40, 0.64), (0.85, 0.37, 0.34), (0.46, 0.63, 0.36),
    (0.85, 0.65, 0.27), (0.55, 0.44, 0.70), (0.34, 0.68, 0.67),
    (0.80, 0.52, 0.68), (0.55, 0.55, 0.55), (0.60, 0.42, 0.29),
    (0.36, 0.55, 0.80),
]

_LANG = {
    "es": {
        "title": "Plan de Corte — {job}",
        "params": "Material {mat} · lámina {sw:g}×{sh:g}mm · margen {margin:g} · sep {gap:g} · rot {rot}",
        "summary": "Resumen",
        "sheets": "Láminas",
        "yield": "Aprovechamiento",
        "parts_placed": "Piezas colocadas",
        "sheet_n": "Lámina {n} de {tot}",
        "util": "aprov. {u:.1f}%",
        "guide": "Guía de piezas",
        "guide_row": "{name} — {qty}× · {w:g}×{h:g}mm",
        "too_big": "⚠ No caben en la lámina: {names}",
        "footer": "Generado {date} · pág {page}",
        "pdf_name": "{slug}_Plan_de_Corte.pdf",
    },
    "en": {
        "title": "Cut Plan — {job}",
        "params": "Material {mat} · sheet {sw:g}×{sh:g}mm · margin {margin:g} · gap {gap:g} · rot {rot}",
        "summary": "Summary",
        "sheets": "Sheets",
        "yield": "Yield",
        "parts_placed": "Parts placed",
        "sheet_n": "Sheet {n} of {tot}",
        "util": "util {u:.1f}%",
        "guide": "Parts guide",
        "guide_row": "{name} — {qty}× · {w:g}×{h:g}mm",
        "too_big": "⚠ Too big for the sheet: {names}",
        "footer": "Generated {date} · p {page}",
        "pdf_name": "{slug}_Cut_Plan.pdf",
    },
}


def _slug(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "nest"


def _part_base(name: str) -> str:
    """Friendly label from a source filename (strip ext + copy suffix, NFC)."""
    base = unicodedata.normalize("NFC", name)
    base = re.sub(r"#\d+$", "", base)
    base = re.sub(r"\.(dxf)$", "", base, flags=re.IGNORECASE)
    return base


def write_reports(
    result: NestResult, out_dir: str, job_name: str, meta: dict, warnings: List[str] | None = None
) -> List[str]:
    """Write the nest JSON (always) and the cut-plan PDF (if reportlab). Returns paths.

    ``warnings`` (e.g. "nested DXF-per-sheet not written: ...") is carried into
    the JSON's ``warnings`` field so a caller that failed to produce some OTHER
    artifact (E4) still surfaces it in the machine-readable output.
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
        _write_pdf(result, pdf_path, job_name, meta, L)
        written.append(pdf_path)

    return written


def _as_dict(result: NestResult, job_name: str, meta: dict, warnings: List[str] | None = None) -> dict:
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
# PDF
# --------------------------------------------------------------------------- #

def _color_map(result: NestResult) -> Dict[str, Tuple[float, float, float]]:
    names = sorted({_part_base(p.part.name) for s in result.sheets for p in s.placements})
    return {n: _PALETTE[i % len(_PALETTE)] for i, n in enumerate(names)}


def _write_pdf(result: NestResult, path: str, job_name: str, meta: dict, L: dict) -> None:
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(path, pagesize=landscape(A4))
    margin = 15 * MM
    spec = result.spec
    cmap = _color_map(result)
    state = {"page": 1}

    def footer() -> None:
        c.setFont("Helvetica", 7)
        c.setFillGray(0.5)
        c.drawRightString(page_w - margin, margin * 0.6,
                          L["footer"].format(date=meta.get("generated", ""), page=state["page"]))
        c.setFillGray(0)

    def new_page(y: float) -> float:
        footer()
        c.showPage()
        state["page"] += 1
        return page_h - margin

    # Header
    y = page_h - margin
    c.setFont("Helvetica-Bold", 15)
    c.drawString(margin, y, L["title"].format(job=job_name))
    y -= 6 * MM
    c.setFont("Helvetica", 8.5)
    c.setFillGray(0.25)
    c.drawString(margin, y, L["params"].format(
        mat=spec.material or "?", sw=spec.width, sh=spec.height,
        margin=spec.margin, gap=spec.part_gap, rot=meta.get("rotation", "")))
    c.setFillGray(0)
    y -= 7 * MM

    # Summary line
    c.setFont("Helvetica-Bold", 10)
    parts_placed = sum(s.part_count for s in result.sheets)
    c.drawString(margin, y, (f"{L['sheets']}: {result.sheet_count}   ·   "
                             f"{L['yield']}: {result.yield_pct:.1f}%   ·   "
                             f"{L['parts_placed']}: {parts_placed}"))
    y -= 8 * MM

    # Sheets, largest-first-ish (natural order)
    draw_w = page_w - 2 * margin
    for sheet in result.sheets:
        block_h = _sheet_block_height(spec, draw_w)
        if y - block_h < margin + 12 * MM:
            y = new_page(y)
        y = _draw_sheet(c, sheet, margin, y, draw_w, cmap, L, MM, result.sheet_count)
        y -= 6 * MM

    # Parts guide
    if y - 30 * MM < margin:
        y = new_page(y)
    y = _draw_guide(c, result, cmap, margin, y, MM, L)

    # Unplaceable
    if result.unplaceable:
        names = ", ".join(f"{_part_base(p.name)}({p.size[0]:.0f}×{p.size[1]:.0f})"
                          for p in result.unplaceable)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColorRGB(0.7, 0.1, 0.1)
        c.drawString(margin, y, L["too_big"].format(names=names))
        c.setFillGray(0)

    footer()
    c.showPage()
    c.save()


def _sheet_scale(spec, draw_w: float, MM) -> float:
    """Points per mm so the sheet width fits the drawing width (cap the height)."""
    max_draw_h = 95 * MM  # keep a couple of sheets per page
    sx = draw_w / spec.width
    sy = max_draw_h / spec.height
    return min(sx, sy)


def _sheet_block_height(spec, draw_w: float) -> float:
    from reportlab.lib.units import mm as MM
    return spec.height * _sheet_scale(spec, draw_w, MM) + 8 * MM


def _draw_sheet(c, sheet: SheetLayout, x0: float, y_top: float, draw_w: float,
                cmap: dict, L: dict, MM, total: int) -> float:
    spec = sheet.spec
    scale = _sheet_scale(spec, draw_w, MM)
    sw, sh = spec.width * scale, spec.height * scale
    base_y = y_top - sh - 6 * MM  # bottom-left of the sheet rect

    # label
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0, y_top - 4 * MM, L["sheet_n"].format(n=sheet.index + 1, tot=total))
    c.setFont("Helvetica", 8)
    c.setFillGray(0.4)
    c.drawRightString(x0 + sw, y_top - 4 * MM, L["util"].format(u=sheet.utilization * 100))
    c.setFillGray(0)

    # sheet outline
    c.setLineWidth(0.8)
    c.setStrokeGray(0.3)
    c.rect(x0, base_y, sw, sh, stroke=1, fill=0)

    def to_pt(p: Point) -> Point:
        return (x0 + p[0] * scale, base_y + p[1] * scale)

    for pl in sheet.placements:
        color = cmap.get(_part_base(pl.part.name), (0.5, 0.5, 0.5))
        outer = [to_pt(p) for p in transform(pl.part.outer, pl.rotation, pl.x, pl.y)]
        _fill_poly(c, outer, color)
        # holes punched as page-white
        for hole in pl.part.holes:
            hp = [to_pt(p) for p in transform(hole, pl.rotation, pl.x, pl.y)]
            _fill_poly(c, hp, (1, 1, 1), stroke=color)

    return base_y


def _fill_poly(c, pts: Sequence[Point], rgb, stroke=None) -> None:
    if len(pts) < 3:
        return
    p = c.beginPath()
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    p.close()
    c.setFillColorRGB(*rgb)
    if stroke is not None:
        c.setStrokeColorRGB(*stroke)
        c.setLineWidth(0.4)
        c.drawPath(p, stroke=1, fill=1)
    else:
        c.drawPath(p, stroke=0, fill=1)


def _draw_guide(c, result: NestResult, cmap: dict, x0: float, y: float, MM, L: dict) -> float:
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, y, L["guide"])
    y -= 6 * MM
    # aggregate qty + size per source
    agg: Dict[str, Tuple[int, float, float]] = {}
    for s in result.sheets:
        for p in s.placements:
            base = _part_base(p.part.name)
            w, h = p.part.size
            n, _, _ = agg.get(base, (0, w, h))
            agg[base] = (n + 1, w, h)
    c.setFont("Helvetica", 8.5)
    for base in sorted(agg):
        n, w, h = agg[base]
        color = cmap.get(base, (0.5, 0.5, 0.5))
        c.setFillColorRGB(*color)
        c.rect(x0, y - 2.6 * MM, 4 * MM, 3.4 * MM, stroke=0, fill=1)
        c.setFillGray(0)
        c.drawString(x0 + 6 * MM, y - 2 * MM, L["guide_row"].format(name=base, qty=n, w=w, h=h))
        y -= 5 * MM
        if y < 18 * MM:
            break
    return y
