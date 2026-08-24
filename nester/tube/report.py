"""Output artifacts for a nesting run: a printable cut-plan PDF + machine JSON.

The PDF is built as an *operational* cut order, not just a diagram:
a "what to buy" summary box, each stock bar drawn to scale with a metre ruler,
parts colored by length (same length = same color across bars), a per-profile
cut list, labeled scrap, and a footer. Bilingual (es/en).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from typing import Dict, List

from .model import BarLayout, ProfileResult, StockSpec

# Lazy import of reportlab so JSON-only runs work without it installed.
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm as MM
    from reportlab.pdfgen import canvas
    _HAVE_REPORTLAB = True
except ImportError:  # pragma: no cover
    _HAVE_REPORTLAB = False

# Distinct, print-friendly palette (parts are colored by length).
_PALETTE = [
    (0.16, 0.47, 0.71), (0.89, 0.55, 0.16), (0.27, 0.62, 0.38),
    (0.78, 0.30, 0.43), (0.48, 0.40, 0.71), (0.55, 0.55, 0.22),
    (0.20, 0.63, 0.66), (0.83, 0.42, 0.24), (0.42, 0.46, 0.54),
    (0.65, 0.34, 0.62),
]

_LANG = {
    "es": {
        "title": "Plan de Corte",
        "buy": "Resumen de compra",
        "buy_bars": "{n} barra(s) de {stock}",
        "buy_pcs": "{pcs} pzas",
        "buy_yld": "aprov. {yld:.0f}%",
        "buy_total": "Total: {bars} barra(s)  ·  sobrante total {scrap}",
        "per_bar": "Barra {i} — {pcs} pzas — sobrante {drop}",
        "cutlist": "Lista de corte",
        "guide": "Guía de piezas  (color → pieza)",
        "too_long": "⚠ demasiado largo para la barra: {names}",
        "usable": "útil {u} por barra",
        "scrap": "sobrante {d}",
        "footer": "Generado {date} · {job} · pág {p}",
        "params": "{nprof} perfil(es) · {bars} barra(s) · corte {kerf} · refrentado {ft}/{bt}",
        "scale_note": "escala: regla en metros",
    },
    "en": {
        "title": "Cut plan",
        "buy": "Shopping summary",
        "buy_bars": "{n} bar(s) of {stock}",
        "buy_pcs": "{pcs} pcs",
        "buy_yld": "yield {yld:.0f}%",
        "buy_total": "Total: {bars} bar(s)  ·  total scrap {scrap}",
        "per_bar": "Bar {i} — {pcs} pcs — drop {drop}",
        "cutlist": "Cut list",
        "guide": "Parts guide  (color → part)",
        "too_long": "⚠ too long for stock: {names}",
        "usable": "usable {u} per bar",
        "scrap": "drop {d}",
        "footer": "Generated {date} · {job} · p.{p}",
        "params": "{nprof} profile(s) · {bars} bar(s) · kerf {kerf} · trim {ft}/{bt}",
        "scale_note": "scale: ruler in metres",
    },
}


def write_reports(
    results: List[ProfileResult],
    out_dir: str,
    job_name: str,
    meta: Dict,
    warnings: List[str] | None = None,
) -> List[str]:
    """Write <job>_corte.json (always) and <job>_Plan_de_Corte.pdf (if reportlab).

    ``warnings`` (e.g. "IGES nest-layout not written: ...") is carried into the
    JSON's ``warnings`` field so a caller that failed to produce some OTHER
    artifact (E4) still surfaces it in the machine-readable output, rather than
    losing it once the job "completes".
    """
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    slug = _slug(job_name)
    lang = meta.get("lang", "es")

    json_path = os.path.join(out_dir, f"{slug}_corte.json")
    with open(json_path, "w") as fh:
        json.dump(_as_dict(results, job_name, meta, warnings), fh, indent=2)
    written.append(json_path)

    if _HAVE_REPORTLAB:
        fname = "Plan_de_Corte" if lang == "es" else "Cut_Plan"
        pdf_path = os.path.join(out_dir, f"{slug}_{fname}.pdf")
        _write_pdf(results, pdf_path, job_name, meta, lang)
        written.append(pdf_path)

    return written


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", name).strip("_")
    return s or "nest"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _mm(v: float) -> str:
    """Human length: whole mm without trailing .0, else 1 decimal."""
    return f"{v:.0f} mm" if abs(v - round(v)) < 0.05 else f"{v:.1f} mm"


def _profile_label(slug: str) -> str:
    """'2x2_c18' -> '2×2 · Cal.18'; '3x1.5_c18' -> '3×1.5 · Cal.18'; 'd32' -> 'Ø32'."""
    s = slug
    gauge = ""
    m = re.search(r"_c(\d+)$", s)
    if m:
        gauge = f" · Cal.{m.group(1)}"
        s = s[: m.start()]
    s = s.replace("x", "×")
    s = re.sub(r"^o?d", "Ø", s)
    return s + gauge


def _color_for_lengths(r: ProfileResult) -> Dict[float, tuple]:
    lengths = sorted(
        {round(p.part.length, 2) for b in r.bars for p in b.placements}, reverse=True
    )
    return {L: _PALETTE[i % len(_PALETTE)] for i, L in enumerate(lengths)}


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def _as_dict(results: List[ProfileResult], job_name: str, meta: Dict, warnings: List[str] | None = None) -> dict:
    return {
        "job": job_name,
        "generated": meta.get("generated"),
        "params": {k: meta[k] for k in ("kerf", "front_trim", "back_trim") if k in meta},
        "warnings": list(warnings or []),
        "totals": {
            "profiles": len(results),
            "stock_bars": sum(r.bar_count for r in results),
        },
        "profiles": [
            {
                "profile": r.profile,
                "bars": r.bar_count,
                "stock_length": r.spec.stock_length,
                "usable_length": r.spec.usable_length,
                "yield_pct": round(r.yield_pct, 2),
                "layout": [
                    {
                        "bar": b.index + 1,
                        "remnant": round(b.remnant, 3),
                        "cuts": [
                            {"part": p.part.name, "length": p.part.length,
                             "start": round(p.start, 3), "end": round(p.end, 3)}
                            for p in b.placements
                        ],
                    }
                    for b in r.bars
                ],
                "unplaceable": [{"part": p.name, "length": p.length} for p in r.unplaceable],
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _write_pdf(results, path, job_name, meta, lang):
    L = _LANG.get(lang, _LANG["es"])
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(path, pagesize=(page_w, page_h))
    margin = 15 * MM
    draw_w = page_w - 2 * margin
    state = {"page": 1}

    def footer():
        c.setFont("Helvetica", 7)
        c.setFillGray(0.5)
        c.drawString(margin, 9 * MM,
                     L["footer"].format(date=meta.get("generated", ""),
                                        job=job_name, p=state["page"]))
        c.setFillGray(0)

    def new_page(y):
        footer()
        c.showPage()
        state["page"] += 1
        return page_h - margin

    y = page_h - margin

    # ---- title ----
    title_job = job_name.replace("_", " ")
    c.setFont("Helvetica-Bold", 17)
    c.drawString(margin, y, f"{L['title']} — {title_job}")
    y -= 15
    total_bars = sum(r.bar_count for r in results)
    c.setFont("Helvetica", 9)
    c.setFillGray(0.35)
    c.drawString(margin, y, L["params"].format(
        nprof=len(results), bars=total_bars, kerf=_mm(meta.get("kerf", 0)),
        ft=_mm(meta.get("front_trim", 0)), bt=_mm(meta.get("back_trim", 0))))
    if meta.get("generated"):
        c.drawRightString(page_w - margin, y, meta["generated"])
    c.setFillGray(0)
    y -= 16

    # ---- shopping summary box ----
    y = _draw_summary(c, L, results, margin, draw_w, y)
    y -= 22

    # ---- per profile ----
    bar_h = 14 * MM
    for pi, r in enumerate(results):
        if y - 46 * MM < margin:
            y = new_page(y)
        if pi > 0:                       # section separator for hierarchy
            c.setStrokeGray(0.85)
            c.setLineWidth(0.6)
            c.line(margin, y + 6, page_w - margin, y + 6)
            c.setStrokeGray(0)
        y = _draw_profile_header(c, L, r, margin, y)
        cmap = _color_for_lengths(r)
        scale = draw_w / r.spec.stock_length
        block_h = 9 + bar_h + 14 + 9   # ruler labels + bar + caption + gap
        for bar in r.bars:
            if y - block_h < margin:
                y = new_page(y)
            y -= 9                                          # room for ruler labels
            top = _draw_ruler(c, L, margin, y, draw_w, r.spec.stock_length)
            _draw_bar(c, margin, top - bar_h, draw_w, bar_h, scale, r, bar, cmap)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(margin, top - bar_h - 11, L["per_bar"].format(
                i=bar.index + 1, pcs=len(bar.placements), drop=_mm(bar.remnant)))
            y = top - bar_h - 14 - 9

        y = _draw_cutlist(c, L, r, margin, draw_w, y)
        y -= 4
        y = _draw_legend(c, L, r, margin, draw_w, y, cmap, new_page)
        if r.unplaceable:
            c.setFont("Helvetica-Bold", 8)
            c.setFillColorRGB(0.7, 0.1, 0.1)
            names = ", ".join(f"{p.name}({_mm(p.length)})" for p in r.unplaceable)
            c.drawString(margin, y, L["too_long"].format(names=names))
            c.setFillColorRGB(0, 0, 0)
            y -= 14
        y -= 16

    footer()
    c.showPage()
    c.save()


def _draw_summary(c, L, results, margin, draw_w, y) -> float:
    pad = 7
    line_h = 13
    n_lines = len(results) + 1
    box_h = pad * 2 + 16 + n_lines * line_h
    top = y
    c.setFillColorRGB(0.96, 0.97, 0.99)
    c.setStrokeColorRGB(0.80, 0.84, 0.90)
    c.setLineWidth(0.8)
    c.roundRect(margin, top - box_h, draw_w, box_h, 4, stroke=1, fill=1)
    c.setFillGray(0)

    # column x positions (aligned across rows)
    c1 = margin + pad                # profile label
    c2 = c1 + 92                     # bars · stock
    c3 = c2 + 118                    # pieces
    c4 = c3 + 60                     # yield

    ty = top - pad - 11
    c.setFont("Helvetica-Bold", 10)
    c.drawString(c1, ty, L["buy"])
    ty -= 16
    total_scrap = 0.0
    total_bars = 0
    for r in results:
        total_bars += r.bar_count
        total_scrap += sum(b.remnant for b in r.bars)
        pcs = sum(len(b.placements) for b in r.bars)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(c1, ty, _profile_label(r.profile))
        c.setFont("Helvetica", 9.5)
        c.drawString(c2, ty, L["buy_bars"].format(n=r.bar_count, stock=_mm(r.spec.stock_length)))
        c.drawString(c3, ty, L["buy_pcs"].format(pcs=pcs))
        c.drawString(c4, ty, L["buy_yld"].format(yld=r.yield_pct))
        ty -= line_h
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(c1, ty, L["buy_total"].format(bars=total_bars, scrap=_mm(total_scrap)))
    return top - box_h


def _draw_profile_header(c, L, r, margin, y) -> float:
    c.setFillColorRGB(*_PALETTE[0])
    c.circle(margin + 3, y + 3, 3, stroke=0, fill=1)
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(margin + 12, y,
                 f"{_profile_label(r.profile)}    {r.bar_count} × {_mm(r.spec.stock_length)}"
                 f"    ·    {r.yield_pct:.1f}%")
    y -= 11
    c.setFont("Helvetica", 7.5)
    c.setFillGray(0.45)
    c.drawString(margin + 12, y, L["usable"].format(u=_mm(r.spec.usable_length)))
    c.setFillGray(0)
    return y - 13


def _draw_ruler(c, L, x, y, w, stock_len) -> float:
    """Metre ruler above the bar. Returns y unchanged (bar drawn just below)."""
    scale = w / stock_len
    c.setStrokeGray(0.6)
    c.setLineWidth(0.4)
    c.setFont("Helvetica", 6)
    c.setFillGray(0.5)
    step = 1000
    t = 0
    while t <= stock_len + 1:
        xt = x + t * scale
        c.line(xt, y, xt, y - 3)
        lbl = "0" if t == 0 else f"{t // 1000}m"
        c.drawCentredString(xt, y + 2, lbl)
        # minor tick at +500
        if t + 500 <= stock_len + 1:
            xm = x + (t + 500) * scale
            c.line(xm, y, xm, y - 1.5)
        t += step
    c.setFillGray(0)
    c.setStrokeGray(0)
    return y - 6


def _part_base(name: str) -> str:
    """Friendly part label: drop the qty-copy suffix, extension, and the
    redundant profile/gauge/qty tokens, and NFC-normalize so decomposed
    filenames (macOS NFD 'n'+tilde) render as 'ñ'.

    'Mastil_Travesaño_2x2_C18_302_Barreno_4pz.iges #1/4' -> 'Mastil_Travesaño_302_Barreno'
    """
    base = name.split(" #")[0]
    base = re.sub(r"\.(iges?|igs)$", "", base, flags=re.IGNORECASE)
    base = unicodedata.normalize("NFC", base)
    # strip "_2x2_C18" / "_3x1.5_C18" profile+gauge token
    base = re.sub(r"_\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?(?:_C\d+)?",
                  "", base, flags=re.IGNORECASE)
    # strip trailing "_4pz"/"_8PZ"
    base = re.sub(r"_\d+\s*pz[a-z]*$", "", base, flags=re.IGNORECASE)
    return base


def _draw_legend(c, L, r, margin, draw_w, y, cmap, new_page) -> float:
    # length -> {part_base: count}
    bylen: Dict[float, Counter] = {}
    for b in r.bars:
        for p in b.placements:
            ln = round(p.part.length, 2)
            bylen.setdefault(ln, Counter())[_part_base(p.part.name)] += 1

    if y - (16 + 12 * len(bylen)) < margin:
        y = new_page(y)

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(margin, y, f"{L['guide']}:")
    y -= 14

    # aligned columns: [swatch] [num right-aligned] mm [×count right-aligned] [names]
    sw = 9
    text_x = margin + sw + 6
    num_r = text_x + 42        # right edge of the length number
    mm_x = num_r + 3           # "mm" label
    cnt_r = mm_x + 44          # right edge of the ×count
    names_x = cnt_r + 14
    maxw = draw_w - (names_x - margin)
    for ln in sorted(bylen, reverse=True):
        if y - 12 < margin:
            y = new_page(y)
        parts = bylen[ln]
        total = sum(parts.values())
        c.setFillColorRGB(*cmap[ln])
        c.rect(margin, y - sw + 2, sw, sw, stroke=0, fill=1)
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(num_r, y, f"{ln:g}")
        c.setFont("Helvetica", 8)
        c.drawString(mm_x, y, "mm")
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(cnt_r, y, f"×{total}")
        # part names, wrapped at " · " boundaries
        c.setFont("Helvetica", 8)
        c.setFillGray(0.3)
        items = [f"{nm} ({n})" for nm, n in sorted(parts.items(), key=lambda kv: -kv[1])]
        line = ""
        for it in items:
            trial = f"{line}  ·  {it}" if line else it
            if line and c.stringWidth(trial, "Helvetica", 8) > maxw:
                c.drawString(names_x, y, line)
                y -= 11
                if y - 11 < margin:
                    y = new_page(y)
                line = it
            else:
                line = trial
        if line:
            c.drawString(names_x, y, line)
        c.setFillGray(0)
        y -= 12
    return y


def _draw_bar(c, x, y, w, h, scale, r, bar: BarLayout, cmap) -> None:
    spec: StockSpec = r.spec
    ft = spec.front_trim * scale
    bt = spec.back_trim * scale

    c.setLineWidth(0.8)
    c.setFillGray(1)
    c.rect(x, y, w, h, stroke=1, fill=1)

    # dead zones
    c.setFillGray(0.80)
    if ft > 0:
        c.rect(x, y, ft, h, stroke=0, fill=1)
    if bt > 0:
        c.rect(x + w - bt, y, bt, h, stroke=0, fill=1)

    usable_x0 = x + ft
    last_end = usable_x0
    for p in bar.placements:
        seg_x = usable_x0 + p.start * scale
        seg_w = p.part.length * scale
        col = cmap[round(p.part.length, 2)]
        c.setFillColorRGB(*col)
        c.rect(seg_x, y, seg_w, h, stroke=0, fill=1)
        # thin separators
        c.setStrokeGray(1)
        c.setLineWidth(0.5)
        c.rect(seg_x, y, seg_w, h, stroke=1, fill=0)
        last_end = seg_x + seg_w
        # label
        c.setFont("Helvetica-Bold", 7)
        label = f"{p.part.length:g}"
        if seg_w > 24:
            c.setFillGray(1)
            c.drawCentredString(seg_x + seg_w / 2, y + h / 2 - 3, label)
        else:
            c.setFillGray(0.1)
            c.saveState()
            c.translate(seg_x + seg_w / 2, y + h + 3)
            c.rotate(90)
            c.drawString(0, -2, label)
            c.restoreState()

    # scrap label in the drop region
    drop_x0 = last_end
    drop_x1 = x + w - bt
    if drop_x1 - drop_x0 > 28:
        c.setFont("Helvetica-Oblique", 7)
        c.setFillGray(0.5)
        c.drawCentredString((drop_x0 + drop_x1) / 2, y + h / 2 - 3,
                            _scrap_text(bar.remnant))

    c.setStrokeGray(0)
    c.setFillGray(0)
    c.setLineWidth(0.8)
    c.rect(x, y, w, h, stroke=1, fill=0)


def _scrap_text(d: float) -> str:
    return f"{d:.0f} mm" if abs(d - round(d)) < 0.05 else f"{d:.1f} mm"


def _draw_cutlist(c, L, r, margin, draw_w, y) -> float:
    counts = Counter(round(p.part.length, 2) for b in r.bars for p in b.placements)
    parts = " · ".join(f"{counts[ln]}× {ln:g}" for ln in sorted(counts, reverse=True))
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, y, f"{L['cutlist']}:")
    c.setFont("Helvetica", 8)
    c.setFillGray(0.25)
    # wrap if long
    maxw = draw_w - 60
    if c.stringWidth(parts, "Helvetica", 8) <= maxw:
        c.drawString(margin + 56, y, parts)
        y -= 14
    else:
        items = parts.split(" · ")
        line = ""
        first = True
        for it in items:
            trial = (line + " · " + it) if line else it
            if c.stringWidth(trial, "Helvetica", 8) > maxw:
                c.drawString(margin + 56, y, line)
                y -= 11
                line = it
                first = False
            else:
                line = trial
        if line:
            c.drawString(margin + 56, y, line)
            y -= 14
    c.setFillGray(0)
    return y
