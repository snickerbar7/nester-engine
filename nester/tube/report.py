"""Output artifacts for a nesting run: a printable cut-plan PDF + machine JSON.

The PDF is an *operational* shop document, laid out to the Harriet Nester
"Plan de corte" artboard: A4/letter landscape, white paper, graphite ink, two
type families approximated with the reportlab base-14 fonts (Helvetica for
prose, Courier for every figure/ID/kicker).

Sheets
  1        Portada — purchase summary, how every bar worked out, warnings,
           material accounts, nesting parameters, source files, sign-off.
  2..n     Dibujo de tramos — three bars per sheet, drawn to scale with a metre
           ruler, running positions under every cut and a tickable sequence.
  n+1..    Lista de cortes — every cut in machine order, two columns, plus the
           part summary (colour -> source file).
  last..   Etiquetas — one cut-out label per piece.

Bilingual (es/en); the Spanish copy is the design's, the English is a faithful
translation onto the same layout.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Tuple

from .model import BarLayout, ProfileResult, StockSpec

# Lazy import of reportlab so JSON-only runs work without it installed.
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm as MM
    from reportlab.pdfgen import canvas
    _HAVE_REPORTLAB = True
except ImportError:  # pragma: no cover
    _HAVE_REPORTLAB = False


# --------------------------------------------------------------------------- #
# Design tokens
# --------------------------------------------------------------------------- #

PX = 0.75                       # design pixel (96 dpi) -> PDF point


def px(v: float) -> float:
    return v * PX


# print/* tokens from the design system
PAPER = (1.0, 1.0, 1.0)
INK = (0.067, 0.075, 0.071)     # #111312
MID = (0.239, 0.251, 0.231)     # #3d403b
SOFT = (0.361, 0.373, 0.357)    # #5c5f5b
FAINT = (0.545, 0.557, 0.533)   # #8b8e88
RULE = (0.788, 0.800, 0.773)    # #c9ccc5
HAIR = (0.886, 0.894, 0.875)    # #e2e4df
HAIR2 = (0.925, 0.933, 0.914)   # #eceee9
PANEL = (0.949, 0.953, 0.941)   # #f2f3f0
ZEBRA = (0.980, 0.984, 0.973)   # #fafbf8
DROP_BG = (0.984, 0.988, 0.980) # #fbfcfa
ACC_BAR = (0.663, 0.486, 0.071)  # #a97c12
ACC_TXT = (0.478, 0.329, 0.020)  # #7a5405
ACC_BRD = (0.784, 0.682, 0.431)  # #c8ae6e
ACC_BG = (0.984, 0.980, 0.961)   # #fbfaf5
HATCH_A = (0.725, 0.737, 0.710)  # #b9bcb5
HATCH_B = (0.906, 0.914, 0.894)  # #e7e9e4

SANS, SANS_B = "Helvetica", "Helvetica-Bold"
MONO, MONO_B = "Courier", "Courier-Bold"

# Piece colours — the design's print scale (oklch converted to sRGB), extended
# around the hue circle so a job with many distinct lengths keeps them apart.
_PALETTE = [
    (0.119, 0.395, 0.668),   # oklch(.50 .13 252)
    (0.000, 0.524, 0.547),   # oklch(.56 .10 200)
    (0.135, 0.486, 0.269),   # oklch(.52 .12 152)
    (0.571, 0.469, 0.102),   # oklch(.58 .11  92)
    (0.778, 0.365, 0.149),   # oklch(.60 .15  45)
    (0.570, 0.288, 0.549),   # oklch(.52 .13 330)
    (0.408, 0.409, 0.689),   # oklch(.55 .11 282)
    (0.082, 0.530, 0.448),   # oklch(.56 .10 176)
    (0.389, 0.489, 0.130),   # oklch(.55 .12 124)
    (0.673, 0.411, 0.000),   # oklch(.58 .13  68)
    (0.000, 0.446, 0.541),   # oklch(.50 .11 215)
    (0.663, 0.306, 0.318),   # oklch(.54 .12  20)
]


_LANG = {
    "es": {
        "title": "Plan de corte",
        "brand": "Harriet Nester",
        # cover
        "buy": "RESUMEN DE COMPRA",
        "buy_sub": "esto es lo que hay que pedir al proveedor",
        "h_material": "MATERIAL", "h_tramo": "TRAMO", "h_qty": "CANTIDAD",
        "h_pcs": "PIEZAS", "h_yield": "APROV.",
        "buy_total": "Total a comprar",
        "buy_units": "{n} tramos",
        "buy_unit1": "1 tramo",
        "buy_note": "sobrante {scrap}",
        "buy_remnants": "Retazos usados",
        "material_sub": "kerf {kerf} · zona muerta {ft} / {bt}",
        "material_sub_nozm": "kerf {kerf} · sin zona muerta",
        "howitwent": "CÓMO QUEDÓ EL MATERIAL",
        "more_bars": "+ {n} tramo(s) más · ver las hojas de dibujo",
        "lg_dead": "zona muerta {v}",
        "lg_drop": "sobrante",
        "warn": "ANTES DE CORTAR",
        "warn_sub": "revísalo antes de encender la sierra",
        "warn_toolong": "Piezas más largas que el tramo",
        "warn_toolong_txt": "No caben en un solo tramo de {stock} y quedaron fuera del "
                            "plan: {names}. Empálmalas o pide un tramo más largo.",
        "and_more": " y {n} más",
        "more_warn": "+ {n} aviso(s) más · ver el JSON del trabajo",
        "warn_generic": "Aviso del trabajo",
        "accounts": "CUENTAS DEL MATERIAL",
        "a_bought": "COMPRADO", "a_remnant": "DE RETAZO", "a_parts": "EN PIEZAS",
        "a_kerf": "KERF", "a_dead": "ZONA MUERTA", "a_drop": "SOBRANTE",
        "a_yield": "APROV.",
        "formula": "aprov. = {parts} ÷ {stock} = {pct} · el sobrante de cada tramo "
                   "queda en el rack, no está descontado",
        "params": "PARÁMETROS DEL ANIDADO",
        "p_profile": "Perfil / material", "p_stock": "Tramo comercial",
        "p_kerf": "Kerf", "p_front": "Zona muerta de entrada",
        "p_back": "Zona muerta de salida", "p_usable": "Útil por tramo",
        "p_remnants": "Retazos ofrecidos", "p_solver": "Algoritmo",
        "p_solver_v": "First Fit Decreasing", "p_pieces": "Piezas a cortar",
        "p_linear": "Longitud lineal",
        "files": "ARCHIVOS DE ORIGEN",
        "files_pcs": "{n} pieza(s) · {len} c/u",
        "more_files": "+ {n} archivo(s) más",
        "signoff": "FIRMAS",
        "sign": ["Revisó", "Cortó · operador", "Verificó · calidad", "Entregó · almacén"],
        # bar drawings
        "draw": "Dibujo de tramos",
        "draw_sub": "{profile} · tramo {stock} · escala 1:{scale} · cortar de izquierda a derecha",
        "draw_range": "tramos {a} – {b} de {n}",
        "bar_meta": "{n} cortes · aprov. {pct} · sobrante {drop}",
        "bar_meta1": "1 corte · aprov. {pct} · sobrante {drop}",
        "seq": "SECUENCIA",
        "dead_short": "ZM",
        # cut list
        "cutlist": "Lista de cortes",
        "cutlist_sub": "{n} cortes en orden de máquina · acumulado medido desde el tope",
        "cutlist_hint": "marca la casilla al cortar",
        "c_bar": "TRAMO", "c_pos": "POS", "c_part": "PIEZA",
        "c_desc": "DESCRIPCIÓN", "c_len": "LONGITUD", "c_acc": "ACUM.",
        "guide": "RESUMEN POR PIEZA · color → archivo de origen",
        "total": "TOTAL",
        "total_cuts": "{n} cortes de sierra",
        # labels
        "labels": "Etiquetas",
        "labels_sub": "{n} etiquetas · recorta por la línea punteada y pégalas al salir de la sierra",
        # bar labels / footer
        "bar_new": "TRAMO {i}",
        "bar_remnant": "RETAZO {label} ({stock})",
        "meta_date": "FECHA", "meta_profiles": "PERFILES",
        "meta_bars": "TRAMOS A COMPRAR", "meta_pieces": "PIEZAS",
        "sheet": "hoja {p} / {n}",
        "foot_note": "medidas en mm · acumulado desde el tope, incluye kerf {kerf}",
        "foot_cover": "hecho con {brand} · nester.harriet.com.mx · {date}",
    },
    "en": {
        "title": "Cut plan",
        "brand": "Harriet Nester",
        "buy": "PURCHASE SUMMARY",
        "buy_sub": "this is what to order from the supplier",
        "h_material": "MATERIAL", "h_tramo": "STOCK", "h_qty": "QUANTITY",
        "h_pcs": "PIECES", "h_yield": "YIELD",
        "buy_total": "Total to buy",
        "buy_units": "{n} bars",
        "buy_unit1": "1 bar",
        "buy_note": "drop {scrap}",
        "buy_remnants": "Remnants used",
        "material_sub": "kerf {kerf} · dead zone {ft} / {bt}",
        "material_sub_nozm": "kerf {kerf} · no dead zone",
        "howitwent": "HOW THE MATERIAL WORKED OUT",
        "more_bars": "+ {n} more bar(s) · see the drawing sheets",
        "lg_dead": "dead zone {v}",
        "lg_drop": "drop",
        "warn": "BEFORE YOU CUT",
        "warn_sub": "check this before starting the saw",
        "warn_toolong": "Parts longer than the stock bar",
        "warn_toolong_txt": "They do not fit a single {stock} bar and were left out "
                            "of the plan: {names}. Splice them or order longer stock.",
        "and_more": " and {n} more",
        "more_warn": "+ {n} more warning(s) · see the job JSON",
        "warn_generic": "Job warning",
        "accounts": "MATERIAL ACCOUNTS",
        "a_bought": "BOUGHT", "a_remnant": "FROM REMNANT", "a_parts": "IN PARTS",
        "a_kerf": "KERF", "a_dead": "DEAD ZONE", "a_drop": "DROP",
        "a_yield": "YIELD",
        "formula": "yield = {parts} ÷ {stock} = {pct} · each bar's drop stays on "
                   "the rack, it is not discounted here",
        "params": "NESTING PARAMETERS",
        "p_profile": "Profile / material", "p_stock": "Stock bar",
        "p_kerf": "Kerf", "p_front": "Front dead zone",
        "p_back": "Back dead zone", "p_usable": "Usable per bar",
        "p_remnants": "Remnants offered", "p_solver": "Solver",
        "p_solver_v": "First Fit Decreasing", "p_pieces": "Pieces to cut",
        "p_linear": "Linear length",
        "files": "SOURCE FILES",
        "files_pcs": "{n} piece(s) · {len} each",
        "more_files": "+ {n} more file(s)",
        "signoff": "SIGN-OFF",
        "sign": ["Checked", "Cut · operator", "Verified · QA", "Delivered · stores"],
        "draw": "Bar drawings",
        "draw_sub": "{profile} · stock {stock} · scale 1:{scale} · cut left to right",
        "draw_range": "bars {a} – {b} of {n}",
        "bar_meta": "{n} cuts · yield {pct} · drop {drop}",
        "bar_meta1": "1 cut · yield {pct} · drop {drop}",
        "seq": "SEQUENCE",
        "dead_short": "DZ",
        "cutlist": "Cut list",
        "cutlist_sub": "{n} cuts in machine order · running position measured from the stop",
        "cutlist_hint": "tick the box as you cut",
        "c_bar": "BAR", "c_pos": "POS", "c_part": "PART",
        "c_desc": "DESCRIPTION", "c_len": "LENGTH", "c_acc": "RUNNING",
        "guide": "PART SUMMARY · colour → source file",
        "total": "TOTAL",
        "total_cuts": "{n} saw cuts",
        "labels": "Labels",
        "labels_sub": "{n} labels · cut along the dotted line and stick them on as they leave the saw",
        "bar_new": "BAR {i}",
        "bar_remnant": "REMNANT {label} ({stock})",
        "meta_date": "DATE", "meta_profiles": "PROFILES",
        "meta_bars": "BARS TO BUY", "meta_pieces": "PIECES",
        "sheet": "sheet {p} / {n}",
        "foot_note": "measurements in mm · running position from the stop, kerf {kerf} included",
        "foot_cover": "made with {brand} · nester.harriet.com.mx · {date}",
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
    losing it once the job "completes" — and is also printed on the cut plan's
    "antes de cortar" panel so the shop sees it on paper.
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
        _write_pdf(results, pdf_path, job_name, meta, lang, warnings)
        written.append(pdf_path)

    return written


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", name).strip("_")
    return s or "nest"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _num(v: float, d: int = 1) -> str:
    """1900 -> '1,900.0'  (thousands separated, fixed decimals)."""
    return f"{v:,.{d}f}"


def _mm(v: float) -> str:
    """Human length: whole mm without trailing .0, else 1 decimal."""
    return f"{v:.0f} mm" if abs(v - round(v)) < 0.05 else f"{v:.1f} mm"


def _mmn(v: float) -> str:
    """Thousands-separated length with unit: '6,000 mm' / '862.5 mm'."""
    return (f"{_num(v, 0)} mm" if abs(v - round(v)) < 0.05 else f"{_num(v, 1)} mm")


def _len1(v: float) -> str:
    """Piece length as the design prints it, always one decimal: '1,900.0'."""
    return _num(v, 1)


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


def _longest_bar(r: ProfileResult) -> float:
    """Longest bar drawn for this profile — a remnant may exceed the tramo."""
    return max([r.spec.stock_length] + [b.stock_length for b in r.bars])


def _tramo_numbers(r: ProfileResult) -> Dict[int, int]:
    """bar.index -> TRAMO ordinal. Remnants don't consume a tramo number, so
    the operator's "TRAMO 3" is the third bar he actually buys."""
    nums: Dict[int, int] = {}
    n = 0
    for b in r.bars:
        if not b.is_remnant:
            n += 1
            nums[b.index] = n
    return nums


def _bar_label(L: Dict[str, str], bar: BarLayout, nums: Dict[int, int]) -> str:
    if bar.is_remnant:
        return L["bar_remnant"].format(label=bar.source, stock=_mm(bar.stock_length))
    return L["bar_new"].format(i=nums[bar.index])


def _remnant_items(results: List[ProfileResult]) -> List[str]:
    """'R-0001 (2140 mm)' for every remnant consumed, in plan order."""
    return [f"{b.source} ({_mm(b.stock_length)})"
            for r in results for b in r.bars if b.is_remnant]


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


# --------------------------------------------------------------------------- #
# JSON  (schema frozen — the service and the web product read this)
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
            "new_bars_needed": sum(r.new_bars_needed for r in results),
        },
        "profiles": [
            {
                "profile": r.profile,
                "bars": r.bar_count,
                "new_bars_needed": r.new_bars_needed,
                "remnants_used": r.remnants_used,
                "stock_length": r.spec.stock_length,
                "usable_length": r.spec.usable_length,
                "yield_pct": round(r.yield_pct, 2),
                "layout": [
                    {
                        "bar": b.index + 1,
                        "stock_length": b.stock_length,
                        "source": b.source,
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
# Plan model — what the sheets actually draw
# --------------------------------------------------------------------------- #

@dataclass
class _Piece:
    """One distinct cut length within the job: its stable ID and colour."""
    pid: str
    profile: str
    length: float
    color: Tuple[float, float, float]
    qty: int = 0
    sources: Counter = field(default_factory=Counter)


@dataclass
class _Cut:
    bar_tag: str          # 'T1' / 'R-0001' — short column value
    pos: int              # 1-based position on that bar
    piece: _Piece
    start: float          # absolute mm from the bar's zero (front trim added back)
    end: float
    folio: str
    desc: str


@dataclass
class _Bar:
    result: ProfileResult
    bar: BarLayout
    label: str            # 'TRAMO 2' / 'RETAZO R-0001 (2400 mm)'
    tag: str              # 'T2' / 'R-0001'
    cuts: List[_Cut]


def _build_plan(results: List[ProfileResult], L, job_tag: str):
    """Assign stable piece IDs/colours and flatten the nest into bars + cuts."""
    pieces: Dict[Tuple[str, float], _Piece] = {}
    n = 0
    for r in results:
        lengths = sorted({round(p.part.length, 2)
                          for b in r.bars for p in b.placements}, reverse=True)
        for ln in lengths:
            n += 1
            pieces[(r.profile, ln)] = _Piece(
                pid=f"P-{n:02d}", profile=r.profile, length=ln,
                color=_PALETTE[(n - 1) % len(_PALETTE)])

    bars: List[_Bar] = []
    folio_seq: Counter = Counter()
    for r in results:
        nums = _tramo_numbers(r)
        ft = r.spec.front_trim
        for b in r.bars:
            tag = f"T{nums[b.index]}" if not b.is_remnant else b.source
            cuts: List[_Cut] = []
            for i, p in enumerate(b.placements, start=1):
                piece = pieces[(r.profile, round(p.part.length, 2))]
                piece.qty += 1
                base = _part_base(p.part.name)
                piece.sources[base] += 1
                folio_seq[piece.pid] += 1
                cuts.append(_Cut(
                    bar_tag=tag, pos=i, piece=piece,
                    start=ft + p.start, end=ft + p.end,
                    folio=f"{job_tag}-{piece.pid.replace('-', '')}-{folio_seq[piece.pid]:02d}",
                    desc=base))
            bars.append(_Bar(result=r, bar=b, label=_bar_label(L, b, nums),
                             tag=tag, cuts=cuts))
    return list(pieces.values()), bars


# --------------------------------------------------------------------------- #
# Low-level drawing helpers
# --------------------------------------------------------------------------- #

def _tw(c, s, font, size, track=0.0) -> float:
    return c.stringWidth(s, font, size) + track * max(0, len(s) - 1)


def _t(c, x, y, s, font, size, color=INK, track=0.0, align="l") -> float:
    """Draw text; returns its width. align l/r/c. ``track`` is letter-spacing."""
    if s is None:
        return 0.0
    s = str(s)
    w = _tw(c, s, font, size, track)
    if align == "r":
        x -= w
    elif align == "c":
        x -= w / 2
    # Always go through a text object: letter-spacing (Tc) is part of the
    # persistent text state, so a tracked run would otherwise leak its spacing
    # into every later drawString on the page.
    to = c.beginText(x, y)
    to.setFont(font, size)
    to.setFillColorRGB(*color)
    to.setCharSpace(track)
    to.textOut(s)
    c.drawText(to)
    return w


def _kicker(c, x, y, s, color=SOFT, size=px(9)) -> float:
    return _t(c, x, y, s, MONO_B, size, color, track=size * 0.14)


def _rect(c, x, y, w, h, fill=None, stroke=None, lw=px(1), dash=None):
    if w <= 0 or h <= 0:
        return
    if dash:
        c.setDash(*dash)
    c.setLineWidth(lw)
    if fill:
        c.setFillColorRGB(*fill)
    if stroke:
        c.setStrokeColorRGB(*stroke)
    c.rect(x, y, w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)
    if dash:
        c.setDash()


def _line(c, x1, y1, x2, y2, color=RULE, lw=px(1), dash=None):
    if dash:
        c.setDash(*dash)
    c.setStrokeColorRGB(*color)
    c.setLineWidth(lw)
    c.line(x1, y1, x2, y2)
    if dash:
        c.setDash()


def _hatch(c, x, y, w, h):
    """The design's 45° dead-zone hatch, clipped to the rect."""
    if w <= 0 or h <= 0:
        return
    c.saveState()
    p = c.beginPath()
    p.rect(x, y, w, h)
    c.clipPath(p, stroke=0, fill=0)
    _rect(c, x, y, w, h, fill=HATCH_B)
    c.setStrokeColorRGB(*HATCH_A)
    c.setLineWidth(px(1.4))
    step = px(4.2)
    d = w + h
    t = -h
    while t < w + step:
        c.line(x + t, y, x + t + h, y + h)
        t += step
    c.restoreState()


def _ellipsize(c, s, font, size, maxw) -> str:
    if _tw(c, s, font, size) <= maxw:
        return s
    while s and _tw(c, s + "…", font, size) > maxw:
        s = s[:-1]
    return s + "…"


def _wrap(c, s, font, size, maxw) -> List[str]:
    out, line = [], ""
    for word in s.split():
        trial = f"{line} {word}" if line else word
        if line and _tw(c, trial, font, size) > maxw:
            out.append(line)
            line = word
        else:
            line = trial
    if line:
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _write_pdf(results, path, job_name, meta, lang, warnings=None):
    """Render twice: pass 1 counts the sheets, pass 2 stamps 'hoja N / total'."""
    buf = BytesIO()
    total = _render(buf, results, job_name, meta, lang, warnings, total=None)
    with open(path, "wb") as fh:
        _render(fh, results, job_name, meta, lang, warnings, total=total)


def _render(dest, results, job_name, meta, lang, warnings, total):
    L = _LANG.get(lang, _LANG["es"])
    PW, PH = landscape(A4)
    c = canvas.Canvas(dest, pagesize=(PW, PH))
    c.setTitle(f"{L['title']} — {job_name}")

    pad_l, pad_t, pad_b = px(34), px(26), px(16)
    cw = PW - 2 * pad_l
    brand = meta.get("brand", L["brand"])
    monogram = meta.get("monogram", "HN")
    job_tag = re.sub(r"[^A-Z0-9]", "", _slug(job_name).upper())[:6] or "JOB"
    generated = meta.get("generated") or ""
    subtitle = meta.get("client") or meta.get("subtitle") or ""

    pieces, bars = _build_plan(results, L, job_tag)
    cuts = [ct for b in bars for ct in b.cuts]
    state = {"page": 0}

    # ---------------- shared chrome ----------------
    def footer(note=""):
        y = pad_b + px(20)
        _line(c, pad_l, y, PW - pad_l, y, RULE)
        by = pad_b + px(5)
        fs = px(8.5)
        left = f"{job_name} · {subtitle}" if subtitle else job_name
        _t(c, pad_l, by, left, MONO, fs, MID)
        sheet = L["sheet"].format(p=state["page"], n=total if total else "?")
        wsheet = _tw(c, sheet, MONO_B, fs)
        _t(c, PW - pad_l, by, sheet, MONO_B, fs, INK, align="r")
        if note:
            _t(c, PW - pad_l - wsheet - px(14), by, note, MONO, fs, FAINT, align="r")

    def page_break(note=""):
        footer(note)
        c.showPage()

    def start_page():
        state["page"] += 1

    def header(title, sub, right1="", right2=""):
        """The 2px-rule sheet header. Returns the y of the content top."""
        ts = px(15)
        base = PH - pad_t - ts * 0.80
        x = pad_l + _t(c, pad_l, base, title, SANS_B, ts, INK, track=-ts * 0.01)
        if sub:
            _t(c, x + px(12), base, sub, MONO, px(11), SOFT)
        rx = PW - pad_l
        if right2:
            rx -= _t(c, rx, base, right2, MONO, px(11), SOFT, align="r") + px(12)
        if right1:
            _t(c, rx, base, right1, MONO_B, px(11), MID, align="r")
        ry = base - px(9)
        _line(c, pad_l, ry, PW - pad_l, ry, INK, px(2))
        return ry - px(12)

    # ---------------- sheet 1: portada ----------------
    start_page()
    _cover(c, L, results, pieces, bars, cuts, meta, job_name, subtitle,
           brand, monogram, generated, warnings, PW, PH, pad_l, pad_t, cw)
    page_break(L["foot_cover"].format(brand=brand, date=generated) if generated
               else brand)

    # ---------------- sheets 2..: bar drawings ----------------
    per_page = 3
    kerf_txt = _mmn(meta.get("kerf", 0) or 0)
    for r in results:
        rbars = [b for b in bars if b.result is r]
        longest = _longest_bar(r)
        # spread the bars evenly over the sheets they need (4 bars -> 2 + 2,
        # not 3 + 1) so no sheet is left almost empty
        n_sheets = max(1, math.ceil(len(rbars) / per_page))
        size = max(1, math.ceil(len(rbars) / n_sheets))
        chunks = [rbars[i:i + size] for i in range(0, len(rbars), size)]
        seen = 0
        for chunk in chunks:
            start_page()
            a = seen + 1
            b_ = seen + len(chunk)
            seen = b_
            draw_w = cw
            scale_den = max(1, round(longest / (draw_w / 72.0 * 25.4)))
            top = header(
                L["draw"],
                L["draw_sub"].format(profile=_profile_label(r.profile),
                                     stock=_mmn(r.spec.stock_length),
                                     scale=scale_den),
                job_name,
                L["draw_range"].format(a=a, b=b_, n=len(rbars)))
            bottom = pad_b + px(28)
            _draw_bars_page(c, L, chunk, longest, pad_l, draw_w, top, bottom)
            page_break(L["foot_note"].format(kerf=kerf_txt))

    # ---------------- cut list ----------------
    rows = _cutlist_entries(cuts, len(results) > 1)
    for pg in _cutlist_pages(c, rows, pieces, PH, pad_t, pad_b):
        start_page()
        top = header(L["cutlist"],
                     L["cutlist_sub"].format(n=len(cuts)),
                     job_name, L["cutlist_hint"])
        _draw_cutlist_page(c, L, pg, pieces, cuts, rows, pad_l, cw, top,
                           pad_b + px(28))
        page_break(L["foot_note"].format(kerf=kerf_txt))

    # ---------------- labels ----------------
    if cuts:
        cols, rows = 6, 6
        per = cols * rows
        for i in range(0, len(cuts), per):
            start_page()
            top = header(L["labels"], L["labels_sub"].format(n=len(cuts)), job_name, "")
            _draw_labels_page(c, L, cuts[i:i + per], pad_l, cw, top,
                              pad_b + px(28), cols, rows, len(results) > 1)
            page_break("")

    c.save()
    return state["page"]


# --------------------------------------------------------------------------- #
# Sheet 1 — portada
# --------------------------------------------------------------------------- #

def _cover(c, L, results, pieces, bars, cuts, meta, job_name, subtitle,
           brand, monogram, generated, warnings, PW, PH, pad_l, pad_t, cw):
    # ---- masthead ----
    top = PH - pad_t
    box = px(30)
    _rect(c, pad_l, top - box, box, box, stroke=INK, lw=px(1.5))
    _t(c, pad_l + box / 2, top - box + px(10), monogram, MONO_B, px(11), INK,
       track=px(0.35), align="c")

    tx = pad_l + box + px(16)
    ts = px(24)
    base = top - ts * 0.78
    w = _t(c, tx, base, L["title"], SANS_B, ts, INK, track=-ts * 0.015)
    _t(c, tx + w + px(12), base, job_name, MONO_B, px(15), MID)
    if subtitle:
        _t(c, tx, base - px(15), subtitle, SANS, px(12.5), SOFT)

    # meta columns, right aligned
    total_new = sum(r.new_bars_needed for r in results)
    metas = []
    if generated:
        metas.append((L["meta_date"], generated.split(" ")[0]))
    metas += [(L["meta_profiles"], str(len(results))),
              (L["meta_bars"], str(total_new)),
              (L["meta_pieces"], str(len(cuts)))]
    mx = PW - pad_l
    for lb, v in reversed(metas):
        wv = max(_tw(c, lb, MONO_B, px(8), px(8) * 0.12),
                 _tw(c, v, MONO_B, px(11.5)))
        _t(c, mx - wv, top - px(8), lb, MONO_B, px(8), FAINT, track=px(8) * 0.12)
        _t(c, mx - wv, top - px(21), v, MONO_B, px(11.5), INK)
        mx -= wv + px(26)

    ry = top - box - px(12)
    _line(c, pad_l, ry, PW - pad_l, ry, INK, px(2))

    # ---- two-column body ----
    body_top = ry - px(15)
    body_bot = pad_l * 0 + px(16) + px(28)     # above the footer rule
    right_w = px(292)
    gap = px(22)
    left_w = cw - right_w - gap
    right_x = pad_l + left_w + gap

    _right_column(c, L, results, pieces, bars, cuts, meta,
                  right_x, right_w, body_top, body_bot)
    _line(c, right_x - px(11), body_bot, right_x - px(11), body_top, RULE)

    cards = _warning_cards(L, results, warnings)
    acc_h = px(52)
    # room the strip list must leave for the warning panel below it
    cards_h = (px(13) + min(len(cards), 3) * px(58)) if cards else 0.0

    y = body_top
    y = _buy_table(c, L, results, bars, pad_l, left_w, y)
    y -= px(15)
    y = _how_it_went(c, L, results, pieces, bars, meta, pad_l, left_w, y,
                     body_bot + acc_h + px(16) + cards_h)
    if cards:
        y = _warning_panel(c, L, cards, pad_l, left_w, y - px(13),
                           body_bot + acc_h + px(10))

    _accounts(c, L, results, bars, meta, pad_l, left_w, body_bot + acc_h)


def _buy_table(c, L, results, bars, x, w, y) -> float:
    _kicker(c, x, y, L["buy"])
    _t(c, x + _tw(c, L["buy"], MONO_B, px(9), px(9) * 0.14) + px(10), y,
       L["buy_sub"], SANS, px(10.5), FAINT)
    y -= px(11)

    # column right edges
    pad = px(11)
    c_qty = x + w - pad          # APROV.
    c_pcs = c_qty - px(108)      # PIEZAS
    c_cnt = c_pcs - px(58)       # CANTIDAD
    c_stk = c_cnt - px(92)       # TRAMO

    rows = len(results)
    rem_items = _remnant_items(results)
    head_h = px(19)
    row_h = px(28)
    tot_h = px(22)
    rem_h = px(17) if rem_items else 0
    box_h = head_h + rows * row_h + tot_h + rem_h
    top = y
    _rect(c, x, top - box_h, w, box_h, stroke=INK, lw=px(1))

    # header (negative)
    _rect(c, x, top - head_h, w, head_h, fill=INK)
    hy = top - head_h + px(6)
    hs = px(8.5)
    _t(c, x + pad, hy, L["h_material"], MONO_B, hs, PAPER, track=hs * 0.11)
    for lbl, cx in ((L["h_tramo"], c_stk), (L["h_qty"], c_cnt),
                    (L["h_pcs"], c_pcs), (L["h_yield"], c_qty)):
        _t(c, cx, hy, lbl, MONO_B, hs, PAPER, track=hs * 0.11, align="r")

    ry = top - head_h
    for i, r in enumerate(results):
        pcs = sum(len(b.placements) for b in r.bars)
        n = r.new_bars_needed
        _t(c, x + pad, ry - px(13), _profile_label(r.profile), SANS_B, px(13), INK)
        sub = (L["material_sub"].format(kerf=_mmn(r.spec.kerf),
                                        ft=_mmn(r.spec.front_trim),
                                        bt=_mmn(r.spec.back_trim))
               if (r.spec.front_trim or r.spec.back_trim)
               else L["material_sub_nozm"].format(kerf=_mmn(r.spec.kerf)))
        _t(c, x + pad, ry - px(24), _ellipsize(c, sub, MONO, px(9.5), c_stk - x - pad - px(8)),
           MONO, px(9.5), SOFT)
        my = ry - px(18)
        _t(c, c_stk, my, _mmn(r.spec.stock_length), MONO, px(12), INK, align="r")
        _t(c, c_cnt, my, L["buy_unit1"] if n == 1 else L["buy_units"].format(n=n),
           MONO_B, px(13), INK, align="r")
        _t(c, c_pcs, my, str(pcs), MONO, px(12), INK, align="r")
        _t(c, c_qty, my, f"{r.yield_pct:.1f} %", MONO_B, px(13), INK, align="r")
        ry -= row_h
        if i < len(results) - 1:
            _line(c, x, ry, x + w, ry, HAIR)

    # totals
    _rect(c, x, ry - tot_h, w, tot_h, fill=PANEL)
    _line(c, x, ry, x + w, ry, INK, px(1.5))
    ty = ry - px(14)
    total_new = sum(r.new_bars_needed for r in results)
    total_pcs = sum(len(b.placements) for r in results for b in r.bars)
    total_scrap = sum(b.remnant for r in results for b in r.bars)
    _t(c, x + pad, ty, L["buy_total"], SANS_B, px(11), INK)
    _t(c, c_stk, ty, "—", MONO, px(11), SOFT, align="r")
    _t(c, c_cnt, ty, L["buy_unit1"] if total_new == 1 else L["buy_units"].format(n=total_new),
       MONO_B, px(12), INK, align="r")
    _t(c, c_pcs, ty, str(total_pcs), MONO_B, px(12), INK, align="r")
    _t(c, c_qty, ty, L["buy_note"].format(scrap=_mmn(total_scrap)),
       MONO, px(9), SOFT, align="r")
    ry -= tot_h

    if rem_items:
        _line(c, x, ry, x + w, ry, HAIR)
        by = ry - px(11.5)
        _rect(c, x + pad, by - px(2), px(9), px(9), fill=DROP_BG, stroke=SOFT,
              lw=px(1), dash=(px(1.6), px(1.6)))
        lbl = L["buy_remnants"] + ": "
        lx = x + pad + px(14)
        lx += _t(c, lx, by, lbl, MONO_B, px(9), MID)
        _t(c, lx, by, _ellipsize(c, " · ".join(rem_items), MONO, px(9.5),
                                 x + w - pad - lx), MONO, px(9.5), SOFT)
        ry -= rem_h
    return ry


def _how_it_went(c, L, results, pieces, bars, meta, x, w, y, floor) -> float:
    _kicker(c, x, y, L["howitwent"])
    y -= px(11)

    lab_w = px(74)
    pct_w = px(46)
    drop_w = px(150)
    bar_x = x + lab_w + px(9)
    bar_w = w - lab_w - px(9) - pct_w - drop_w - px(18)

    # measure the legend first — it is what the strip list has to leave room for
    fs = px(9.5)
    keys = [f"{p.pid} \u00b7 {_len1(p.length)} \u00d7 {p.qty}" for p in pieces]
    ft = meta.get("front_trim", 0) or 0
    bt = meta.get("back_trim", 0) or 0
    if ft or bt:
        keys.append(L["lg_dead"].format(v=_mmn(ft + bt)))
    keys.append(L["lg_drop"])
    lines, run = 1, 0.0
    for k in keys:
        kw = px(14) + _tw(c, k, MONO, fs) + px(14)
        if run and run + kw > w:
            lines += 1
            run = kw
        else:
            run += kw
    legend_h = px(13) + lines * px(12)

    # the strip list breathes into whatever room the sheet has left
    multi = len(results) > 1
    head_h = px(14) if multi else 0.0
    avail = max(0.0, y - floor - legend_h - head_h * len(results))
    row_h = px(19)
    if bars and avail / len(bars) > row_h:
        row_h = min(px(30), avail / len(bars))
    room = int(avail // row_h)
    shown = bars if room >= len(bars) else bars[:max(0, room - 2)]
    hidden = len(bars) - len(shown)

    seen_profiles: set = set()
    for b in shown:
        r = b.result
        if multi and r.profile not in seen_profiles:
            seen_profiles.add(r.profile)
            y -= px(3)
            _t(c, x, y - px(7), _profile_label(r.profile), MONO_B, px(8.5), FAINT,
               track=px(8.5) * 0.11)
            _line(c, x + _tw(c, _profile_label(r.profile), MONO_B, px(8.5),
                             px(8.5) * 0.11) + px(8), y - px(4), x + w, y - px(4), HAIR2)
            y -= px(11)
        longest = _longest_bar(r)
        bh = min(px(17), row_h - px(4))
        by = y - bh - (row_h - bh) / 2
        bw = bar_w * (b.bar.stock_length / longest)
        ty = by + bh / 2 - px(3.5)
        _t(c, x, ty, _ellipsize(c, b.tag if b.bar.is_remnant else b.label,
                                MONO, px(9.5), lab_w),
           MONO, px(9.5), MID)
        _bar_strip(c, bar_x, by, bw, bh, b, mini=True)
        used = b.bar.usable_length
        pct = (100.0 * b.bar.used_length / used) if used > 0 else 0.0
        _t(c, bar_x + bar_w + px(9) + pct_w, ty, f"{pct:.1f} %",
           MONO_B, px(10), INK, align="r")
        _t(c, x + w, ty,
           f"{L['lg_drop']} {_num(b.bar.remnant, 1)} mm", MONO, px(9.5), SOFT, align="r")
        y -= row_h

    if hidden > 0:
        _t(c, bar_x, y - px(9), L["more_bars"].format(n=hidden), MONO, px(9), FAINT)
        y -= px(14)

    # legend: swatch + P-xx · length × qty, then dead zone + drop keys
    y -= px(3)
    lx, ly = x, y - px(9)

    def key(sw_draw, text):
        nonlocal lx, ly
        tw = _tw(c, text, MONO, fs)
        if lx + px(14) + tw > x + w:
            lx, ly = x, ly - px(12)
        sw_draw(lx, ly)
        _t(c, lx + px(14), ly, text, MONO, fs, MID)
        lx += px(14) + tw + px(14)

    for p in pieces:
        key(lambda sx, sy, col=p.color: _rect(c, sx, sy - px(1), px(9), px(9), fill=col),
            f"{p.pid} · {_len1(p.length)} × {p.qty}")
    if ft or bt:
        key(lambda sx, sy: _hatch(c, sx, sy - px(1), px(9), px(9)),
            L["lg_dead"].format(v=_mmn(ft + bt)))
    key(lambda sx, sy: _rect(c, sx, sy - px(1), px(9), px(9), fill=DROP_BG,
                             stroke=SOFT, lw=px(1), dash=(px(1.6), px(1.6))),
        L["lg_drop"])
    return ly - px(10)


def _warning_cards(L, results, warnings) -> List[Tuple[str, str]]:
    cards: List[Tuple[str, str]] = []
    for r in results:
        if r.unplaceable:
            shown = r.unplaceable[:4]
            names = ", ".join(f"{p.name} ({_mmn(p.length)})" for p in shown)
            if len(r.unplaceable) > len(shown):
                names += L["and_more"].format(n=len(r.unplaceable) - len(shown))
            cards.append((L["warn_toolong"],
                          L["warn_toolong_txt"].format(
                              stock=_mmn(r.spec.stock_length), names=names)))
    for w in (warnings or []):
        head, _, rest = str(w).partition(": ")
        cards.append((head, rest) if rest else (L["warn_generic"], head))
    return cards


def _warning_panel(c, L, cards, x, w, y, floor) -> float:
    _kicker(c, x, y, L["warn"])
    _t(c, x + _tw(c, L["warn"], MONO_B, px(9), px(9) * 0.14) + px(10), y,
       L["warn_sub"], SANS, px(10.5), FAINT)
    y -= px(11)
    for i, (title, body) in enumerate(cards, start=1):
        wrapped = _wrap(c, body, SANS, px(10.5), w - px(40))
        lines = wrapped[:2]
        if len(wrapped) > 2 and lines:
            lines[-1] = _ellipsize(c, lines[-1] + " …", SANS, px(10.5), w - px(40))
        h = px(11) + px(13) + px(14) * len(lines)
        if y - h < floor:
            _t(c, x, y - px(9), L["more_warn"].format(n=len(cards) - i + 1),
               MONO, px(9), ACC_TXT)
            y -= px(13)
            break
        _rect(c, x, y - h, w, h, fill=ACC_BG, stroke=HAIR, lw=px(1))
        _rect(c, x, y - h, px(3), h, fill=ACC_BAR)
        ny = y - px(15)
        _rect(c, x + px(9), ny - px(3), px(14), px(13), stroke=ACC_BRD, lw=px(1))
        _t(c, x + px(16), ny, str(i), MONO_B, px(10), ACC_TXT, align="c")
        _t(c, x + px(30), ny, _ellipsize(c, title, SANS_B, px(11), w - px(40)),
           SANS_B, px(11), INK)
        for j, ln in enumerate(lines):
            _t(c, x + px(30), ny - px(13) - j * px(14), ln, SANS, px(10.5), MID)
        y -= h + px(6)
    return y


def _accounts(c, L, results, bars, meta, x, w, y):
    _line(c, x, y + px(30), x + w, y + px(30), RULE)
    _kicker(c, x, y + px(18), L["accounts"])

    new_len = sum(b.bar.stock_length for b in bars if not b.bar.is_remnant)
    rem_len = sum(b.bar.stock_length for b in bars if b.bar.is_remnant)
    parts = sum(r.total_part_length for r in results)
    kerf_total = sum(len(b.bar.placements) * b.result.spec.kerf for b in bars)
    dead = sum(b.result.spec.front_trim + b.result.spec.back_trim for b in bars)
    drop = sum(b.bar.remnant for b in bars)
    stock = new_len + rem_len
    yld = 100.0 * parts / stock if stock else 0.0

    cells = [(L["a_bought"], f"{_num(new_len, 0)} mm", False)]
    if rem_len:
        cells.append((L["a_remnant"], f"{_num(rem_len, 0)} mm", False))
    cells.append((L["a_parts"], f"{_num(parts, 1)} mm", False))
    if kerf_total:
        cells.append((L["a_kerf"], f"{_num(kerf_total, 1)} mm", False))
    if dead:
        cells.append((L["a_dead"], f"{_num(dead, 0)} mm", False))
    cells.append((L["a_drop"], f"{_num(drop, 1)} mm", False))
    cells.append((L["a_yield"], f"{yld:.1f} %", True))

    cx = x
    for i, (lb, v, big) in enumerate(cells):
        vs = px(16) if big else px(12.5)
        cwid = max(_tw(c, lb, MONO_B, px(8), px(8) * 0.1), _tw(c, v, MONO_B, vs))
        _t(c, cx, y + px(3), lb, MONO_B, px(8), FAINT, track=px(8) * 0.1)
        _t(c, cx, y - px(11), v, MONO_B, vs, INK)
        cx += cwid + px(14)
        if i < len(cells) - 1:
            _line(c, cx - px(7), y - px(13), cx - px(7), y + px(6), HAIR)

    _t(c, x, y - px(23),
       _ellipsize(c, L["formula"].format(parts=_num(parts, 1), stock=_num(stock, 0),
                                         pct=f"{yld:.1f} %"), MONO, px(9), w),
       MONO, px(9), FAINT)


def _right_column(c, L, results, pieces, bars, cuts, meta, x, w, y, floor):
    _kicker(c, x, y, L["params"])
    y -= px(9)

    r0 = results[0] if results else None
    profs = " · ".join(_profile_label(r.profile) for r in results)
    stocks = " · ".join(sorted({_mmn(r.spec.stock_length) for r in results}))
    usable = " · ".join(sorted({_mmn(r.spec.usable_length) for r in results}))
    n_rem = sum(len(r.spec.extra_stock) for r in results)
    rows = [
        (L["p_profile"], _ellipsize(c, profs, MONO_B, px(12), w * 0.62)),
        (L["p_stock"], stocks),
        (L["p_kerf"], _mmn(r0.spec.kerf if r0 else 0)),
        (L["p_front"], _mmn(r0.spec.front_trim if r0 else 0)),
        (L["p_back"], _mmn(r0.spec.back_trim if r0 else 0)),
        (L["p_usable"], _ellipsize(c, usable, MONO_B, px(12), w * 0.62)),
    ]
    if n_rem:
        rows.append((L["p_remnants"], str(n_rem)))
    rows += [
        (L["p_pieces"], str(len(cuts))),
        (L["p_linear"], _mmn(sum(p.piece.length for p in cuts))),
        (L["p_solver"], L["p_solver_v"]),
    ]
    for i, (lb, v) in enumerate(rows):
        y -= px(15)
        _t(c, x, y, lb, SANS, px(10.5), MID)
        _t(c, x + w, y, v, MONO_B, px(12), INK, align="r")
        if i < len(rows) - 1:
            _line(c, x, y - px(5), x + w, y - px(5), HAIR2)
    y -= px(20)

    # ---- source files ----
    files: Dict[str, List[float]] = {}
    for ct in cuts:
        files.setdefault(ct.desc, []).append(ct.piece.length)
    if files and y > floor + px(120):
        _kicker(c, x, y, L["files"])
        y -= px(13)
        room = int((y - floor - px(96)) // px(21))
        items = sorted(files.items())
        shown = items[:room] if room < len(items) else items
        for name, lens in shown:
            _t(c, x, y, _ellipsize(c, name, MONO, px(9.5), w), MONO, px(9.5), INK)
            uniq = sorted(set(round(v, 2) for v in lens), reverse=True)
            lt = _mmn(uniq[0]) if len(uniq) == 1 else f"{len(uniq)}×L"
            _t(c, x, y - px(11), L["files_pcs"].format(n=len(lens), len=lt),
               MONO, px(9), SOFT)
            y -= px(21)
        if len(items) > len(shown):
            _t(c, x, y, L["more_files"].format(n=len(items) - len(shown)),
               MONO, px(9), FAINT)
            y -= px(13)

    # ---- sign-off, pinned to the bottom ----
    sy = floor + px(58)
    _line(c, x, sy + px(14), x + w, sy + px(14), RULE)
    _kicker(c, x, sy + px(2), L["signoff"])
    colw = (w - px(14)) / 2
    for i, lbl in enumerate(L["sign"]):
        cx = x + (i % 2) * (colw + px(14))
        cy = sy - px(14) - (i // 2) * px(29)
        _line(c, cx, cy, cx + colw, cy, INK)
        _t(c, cx, cy - px(9), lbl, MONO, px(8.5), SOFT, track=px(8.5) * 0.08)


# --------------------------------------------------------------------------- #
# Bar geometry (shared by the mini strips and the full drawings)
# --------------------------------------------------------------------------- #

def _bar_strip(c, x, y, w, h, b: _Bar, mini: bool, L=None):
    """The bar itself: dead zones hatched, pieces coloured, drop dashed."""
    spec: StockSpec = b.result.spec
    stock = b.bar.stock_length
    s = w / stock if stock else 0.0
    ft, bt = spec.front_trim * s, spec.back_trim * s

    _rect(c, x, y, w, h, fill=PAPER)
    if ft > 0:
        _hatch(c, x, y, ft, h)
        _line(c, x + ft, y, x + ft, y + h, INK, px(1))
    if bt > 0:
        _hatch(c, x + w - bt, y, bt, h)
        _line(c, x + w - bt, y, x + w - bt, y + h, INK, px(1))
    if not mini and ft > px(14):
        _t(c, x + ft / 2, y + h / 2 - px(3), L["dead_short"], MONO_B, px(8), SOFT,
           track=px(0.4), align="c")
    if not mini and bt > px(14):
        _t(c, x + w - bt / 2, y + h / 2 - px(3), L["dead_short"], MONO_B, px(8), SOFT,
           track=px(0.4), align="c")

    last = x + ft
    for ct in b.cuts:
        sx = x + ct.start * s
        sw = (ct.end - ct.start) * s
        _rect(c, sx, y, sw, h, fill=ct.piece.color)
        _line(c, sx + sw, y, sx + sw, y + h, PAPER, px(1) if mini else px(1.4))
        last = sx + sw
        if not mini:
            lt = _len1(ct.piece.length)
            if sw > _tw(c, lt, MONO_B, px(11)) + px(6):
                if sw > _tw(c, ct.piece.pid, MONO, px(8.5)) + px(14):
                    _t(c, sx + sw / 2, y + h / 2 + px(4), ct.piece.pid, MONO,
                       px(8.5), (1, 1, 1), align="c")
                    _t(c, sx + sw / 2, y + h / 2 - px(9), lt, MONO_B, px(11),
                       (1, 1, 1), align="c")
                else:
                    _t(c, sx + sw / 2, y + h / 2 - px(4), lt, MONO_B, px(11),
                       (1, 1, 1), align="c")
            else:
                c.saveState()
                c.translate(sx + sw / 2, y + h + px(3))
                c.rotate(90)
                _t(c, 0, -px(3), lt, MONO, px(7), MID)
                c.restoreState()

    # drop region
    dx0, dx1 = last, x + w - bt
    if dx1 - dx0 > 0.4:
        _rect(c, dx0, y, dx1 - dx0, h, fill=DROP_BG)
        _line(c, dx0, y, dx0, y + h, SOFT, px(1), dash=(px(2), px(2)))
        if not mini:
            lt = _num(b.bar.remnant, 1)
            if dx1 - dx0 > _tw(c, lt, MONO, px(9.5)) + px(8):
                _t(c, (dx0 + dx1) / 2, y + h / 2 - px(3), lt, MONO, px(9.5),
                   SOFT, align="c")
    _rect(c, x, y, w, h, stroke=INK, lw=px(1) if mini else px(1.5))


def _ruler(c, x, y, w, stock):
    """Metre ruler above a bar drawing (labels sit above the ticks)."""
    s = w / stock if stock else 0.0
    major = 1000.0 if stock >= 2000 else 500.0
    minor = major / 4.0
    t = 0.0
    while t <= stock + 0.5:
        xt = x + t * s
        is_major = abs(t / major - round(t / major)) < 1e-6
        is_half = abs((t % major) / (major / 2) - 1) < 1e-6
        hgt = px(7) if is_major else (px(5) if is_half else px(3))
        _line(c, xt, y, xt, y + hgt, MID if is_major else HATCH_A, px(1))
        if is_major:
            _t(c, xt if t == 0 else xt, y + px(9),
               f"{t / 1000:g} m", MONO, px(8), FAINT,
               align="l" if t == 0 else "c")
        t += minor


def _draw_bars_page(c, L, chunk, longest, x, w, top, bottom):
    """Up to three bar blocks, distributed over the sheet like the artboard."""
    n = len(chunk)
    if not n:
        return
    base = px(150)                        # heading + ruler + bar + marks + chips
    # a short page gets taller bars rather than a big hole at the bottom
    extra = min(px(46), max(0.0, top - bottom - n * base) / n * 0.55)
    slack = max(0.0, top - bottom - n * (base + extra))
    gap = min(px(110), slack / (n - 1)) if n > 1 else 0.0
    y = top
    for b in chunk:
        y = _draw_bar_block(c, L, b, longest, x, w, y, px(72) + extra)
        y -= gap


def _draw_bar_block(c, L, b: _Bar, longest, x, w, y, bar_h=px(72)) -> float:
    used = b.bar.usable_length
    pct = (100.0 * b.bar.used_length / used) if used > 0 else 0.0
    # heading
    hy = y - px(10)
    hx = x + _t(c, x, hy, b.label, MONO_B, px(13), INK, track=px(13) * 0.05) + px(11)
    key = "bar_meta1" if len(b.cuts) == 1 else "bar_meta"
    meta = L[key].format(n=len(b.cuts), pct=f"{pct:.1f} %",
                         drop=f"{_num(b.bar.remnant, 1)} mm")
    hx += _t(c, hx, hy, meta, MONO, px(11), SOFT) + px(11)
    counts = Counter(ct.piece.pid for ct in b.cuts)
    lista = " · ".join(f"{n}× {_len1(next(ct.piece.length for ct in b.cuts if ct.piece.pid == p))}"
                       for p, n in counts.items())
    lw_ = _tw(c, lista, MONO, px(10))
    _t(c, x + w, hy, _ellipsize(c, lista, MONO, px(10), w * 0.42), MONO, px(10),
       FAINT, align="r")
    _line(c, hx, hy + px(3), x + w - min(lw_, w * 0.42) - px(11), hy + px(3), HAIR)

    bw = w * (b.bar.stock_length / longest)
    ry = hy - px(20)
    _ruler(c, x, ry, bw, b.bar.stock_length)
    by = ry - px(4) - bar_h
    _bar_strip(c, x, by, bw, bar_h, b, mini=False, L=L)

    # running positions under each cut
    s = bw / b.bar.stock_length if b.bar.stock_length else 0.0
    marks = [b.result.spec.front_trim] + [ct.end for ct in b.cuts]
    last_x = -1e9
    for i, m in enumerate(marks):
        mx = x + m * s
        _line(c, mx, by - px(4), mx, by, FAINT, px(1))
        lbl = _num(m, 1)
        lwid = _tw(c, lbl, MONO, px(7.5))
        lx = mx if i == 0 else mx - lwid / 2
        if lx > last_x + px(2):
            _t(c, lx, by - px(12), lbl, MONO, px(7.5), FAINT)
            last_x = lx + lwid

    # sequence chips, led by the SECUENCIA kicker on the first line
    cy = by - px(24)
    seq_w = _tw(c, L["seq"], MONO_B, px(8), px(8) * 0.11) + px(8)
    _kicker(c, x, cy - px(4), L["seq"], FAINT, px(8))
    cx = x + seq_w
    fs = px(8.5)
    for ct in b.cuts:
        parts = (f"{ct.pos:02d}", ct.piece.pid, _len1(ct.piece.length),
                 f"-> {_num(ct.end, 1)}")
        wchip = (px(9) + px(5) + _tw(c, parts[0], MONO, fs) + px(5)
                 + _tw(c, parts[1], MONO, px(9.5)) + px(5)
                 + _tw(c, parts[2], MONO_B, px(10)) + px(5)
                 + _tw(c, parts[3], MONO, fs) + px(14))
        if cx + wchip > x + w:
            cx = x
            cy -= px(19)
        _rect(c, cx, cy - px(13), wchip, px(15), fill=DROP_BG, stroke=HAIR, lw=px(1))
        _rect(c, cx, cy - px(13), px(3), px(15), fill=ct.piece.color)
        ix = cx + px(7)
        _rect(c, ix, cy - px(6), px(9), px(9), stroke=FAINT, lw=px(1))
        ix += px(9) + px(5)
        ix += _t(c, ix, cy - px(4), parts[0], MONO, fs, FAINT) + px(5)
        ix += _t(c, ix, cy - px(4), parts[1], MONO, px(9.5), INK) + px(5)
        ix += _t(c, ix, cy - px(4), parts[2], MONO_B, px(10), INK) + px(5)
        _t(c, ix, cy - px(4), parts[3], MONO, fs, FAINT)
        cx += wchip + px(6)
    return cy - px(20)


# --------------------------------------------------------------------------- #
# Cut list sheets
# --------------------------------------------------------------------------- #

_ROW_H = px(15.5)
_HEAD_H = px(17)


def _guide_height(pieces) -> float:
    return px(20) + px(13) + len(pieces) * px(15) + px(20)


def _cutlist_entries(cuts, multi: bool) -> List:
    """Cut rows, with a profile band inserted whenever the material changes."""
    if not multi:
        return list(cuts)
    out: List = []
    seen = None
    for ct in cuts:
        if ct.piece.profile != seen:
            seen = ct.piece.profile
            out.append(_profile_label(seen))
        out.append(ct)
    return out


def _cutlist_pages(c, cuts, pieces, PH, pad_t, pad_b) -> List[List[List]]:
    """Split the rows into pages of two columns; the guide rides the last one."""
    if not cuts:
        return []
    body = (PH - pad_t - px(40)) - (pad_b + px(28)) - _HEAD_H
    per_col = max(4, int(body // _ROW_H))
    guide_rows = int(math.ceil(_guide_height(pieces) / _ROW_H))

    right_cap = max(1, per_col - guide_rows)     # the guide rides the right column
    pages: List[List[List]] = []
    i = 0
    n = len(cuts)
    while i < n:
        rest = n - i
        if rest <= per_col + right_cap:           # last sheet: balance the columns
            half = min(per_col, max(rest - right_cap, int(math.ceil(rest / 2))))
        else:
            half = per_col
        pages.append([cuts[i:i + half], cuts[i + half:i + half + per_col]])
        i += half + len(pages[-1][1])
    return pages


def _draw_cutlist_page(c, L, page, pieces, all_cuts, all_rows, x, w, top, bottom):
    colw = (w - px(26)) / 2
    last = _is_last_chunk(page, all_rows)
    guide_h = _guide_height(pieces) if last else 0.0
    avail = top - bottom - _HEAD_H
    row_h = px(22)
    for ci, col in enumerate(page):
        if col:
            room = avail - (guide_h if ci == 1 else 0.0)
            row_h = min(row_h, max(_ROW_H, room / len(col)))
    for ci, col in enumerate(page):
        cx = x + ci * (colw + px(26))
        y = _cut_column(c, L, col, cx, colw, top, row_h)
        if ci == 1 and last:
            _guide_block(c, L, pieces, all_cuts, cx, colw, y - px(9))


def _is_last_chunk(page, all_rows) -> bool:
    flat = [ct for col in page for ct in col]
    return bool(flat) and flat[-1] is all_rows[-1]


_COLS = (px(15), px(46), px(26), px(52))     # box, bar, pos, piece
_GAP = px(7)
_LEN_W, _ACC_W = px(70), px(64)


def _cut_column(c, L, rows, x, w, y, row_h=_ROW_H) -> float:
    pad = px(8)
    _rect(c, x, y - _HEAD_H, w, _HEAD_H, fill=INK)
    hy = y - _HEAD_H + px(5)
    hs = px(8)
    cx = x + pad + _COLS[0] + _GAP
    for lbl, cwid in ((L["c_bar"], _COLS[1]), (L["c_pos"], _COLS[2]),
                      (L["c_part"], _COLS[3])):
        _t(c, cx, hy, lbl, MONO_B, hs, PAPER, track=hs * 0.1)
        cx += cwid + _GAP
    _t(c, cx, hy, L["c_desc"], MONO_B, hs, PAPER, track=hs * 0.1)
    _t(c, x + w - pad - _ACC_W - _GAP, hy, L["c_len"], MONO_B, hs, PAPER,
       track=hs * 0.1, align="r")
    _t(c, x + w - pad, hy, L["c_acc"], MONO_B, hs, PAPER, track=hs * 0.1, align="r")
    y -= _HEAD_H

    desc_x = x + pad + sum(_COLS) + 4 * _GAP
    desc_w = (x + w - pad - _ACC_W - _GAP - _LEN_W - px(8)) - desc_x
    for i, ct in enumerate(rows):
        ry = y - row_h
        if isinstance(ct, str):                  # profile band
            _rect(c, x, ry, w, row_h, fill=PANEL)
            _line(c, x, ry, x + w, ry, RULE)
            _t(c, x + pad, ry + row_h / 2 - px(3), ct, MONO_B, px(8.5), MID,
               track=px(8.5) * 0.11)
            y = ry
            continue
        if i % 2:
            _rect(c, x, ry, w, row_h, fill=ZEBRA)
        _line(c, x, ry, x + w, ry, HAIR2)
        ty = ry + row_h / 2 - px(3.5)
        bx = x + pad
        _rect(c, bx, ty - px(2), px(11), px(11), stroke=FAINT, lw=px(1))
        bx += _COLS[0] + _GAP
        _t(c, bx, ty, ct.bar_tag, MONO, px(9.5), MID)
        bx += _COLS[1] + _GAP
        _t(c, bx, ty, f"{ct.pos:02d}", MONO, px(9.5), FAINT)
        bx += _COLS[2] + _GAP
        _rect(c, bx, ty - px(1), px(8), px(8), fill=ct.piece.color)
        _t(c, bx + px(13), ty, ct.piece.pid, MONO, px(10), INK)
        _t(c, desc_x, ty, _ellipsize(c, ct.desc, SANS, px(10), desc_w),
           SANS, px(10), MID)
        _t(c, x + w - pad - _ACC_W - _GAP, ty, _len1(ct.piece.length),
           MONO_B, px(11), INK, align="r")
        _t(c, x + w - pad, ty, _num(ct.end, 1), MONO, px(10), SOFT, align="r")
        y = ry
    return y


def _guide_block(c, L, pieces, all_cuts, x, w, y):
    _line(c, x, y, x + w, y, INK, px(1.5))
    y -= px(13)
    _kicker(c, x, y, L["guide"])
    y -= px(6)
    pad = px(8)
    for p in pieces:
        y -= px(15)
        _rect(c, x + pad, y - px(1), px(9), px(9), fill=p.color)
        _t(c, x + pad + px(16), y, p.pid, MONO, px(10), INK)
        srcs = " · ".join(f"{nm} ({n})" for nm, n in
                          sorted(p.sources.items(), key=lambda kv: -kv[1]))
        _t(c, x + pad + px(68), y, _ellipsize(c, srcs, SANS, px(10), w - px(152)),
           SANS, px(10), MID)
        _t(c, x + w - pad - px(52), y, _len1(p.length), MONO_B, px(11), INK, align="r")
        _t(c, x + w - pad, y, f"× {p.qty}", MONO, px(10), SOFT, align="r")
    y -= px(18)
    _rect(c, x, y - px(3), w, px(18), fill=PANEL)
    _line(c, x, y + px(15), x + w, y + px(15), RULE)
    _t(c, x + pad + px(16), y + px(2), L["total"], MONO_B, px(10), INK)
    _t(c, x + pad + px(68), y + px(2),
       L["total_cuts"].format(n=len(all_cuts)), SANS, px(10), MID)
    _t(c, x + w - pad - px(52), y + px(2),
       _num(sum(ct.piece.length for ct in all_cuts), 1), MONO_B, px(11), INK, align="r")
    _t(c, x + w - pad, y + px(2), "mm", MONO, px(10), SOFT, align="r")


# --------------------------------------------------------------------------- #
# Label sheets
# --------------------------------------------------------------------------- #

def _draw_labels_page(c, L, cuts, x, w, top, bottom, cols, rows, multi=False):
    cell_w = w / cols
    cell_h = min(px(114), (top - bottom) / rows)
    for i, ct in enumerate(cuts):
        r, k = divmod(i, cols)
        cx = x + k * cell_w
        cy = top - (r + 1) * cell_h
        _rect(c, cx, cy, cell_w, cell_h, stroke=FAINT, lw=px(1),
              dash=(px(2.4), px(2.4)))
        px_, py = cx + px(8), cy + cell_h - px(8)
        _rect(c, px_, py - px(26), px(8), px(26), fill=ct.piece.color)
        tx = px_ + px(13)
        tw = cell_w - (tx - cx) - px(8)
        _t(c, tx, py - px(8), ct.piece.pid, MONO, px(9), INK, track=px(0.2))
        _t(c, tx, py - px(18), _ellipsize(c, ct.desc, SANS, px(8), tw),
           SANS, px(8), SOFT)
        _t(c, cx + px(8), cy + px(24), _len1(ct.piece.length), MONO_B, px(17), INK,
           track=-px(0.17))
        stamp = f"{ct.bar_tag} · {ct.pos:02d}"
        if multi:
            stamp = f"{_profile_label(ct.piece.profile)} · {stamp}"
        _t(c, cx + px(8), cy + px(9),
           _ellipsize(c, stamp, MONO, px(8), cell_w * 0.56), MONO, px(8), SOFT)
        _t(c, cx + cell_w - px(8), cy + px(9),
           _ellipsize(c, ct.folio, MONO, px(7.5), cell_w * 0.42),
           MONO, px(7.5), FAINT, align="r")
