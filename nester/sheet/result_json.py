"""The machine-readable nest — the JSON schema the service and the web app read.

It lives in its own module (rather than inside ``report.py`` next to the PDF)
because it is a CONTRACT, not a rendering: the web product, the async jobs API
and the shop's own tooling all parse it, while nothing outside this repo cares
how a page is drawn. ``report.py`` re-exports it as ``_as_dict`` so every
existing import keeps working.

Rules that hold across every change to this file:

* **additive only.** No key is ever renamed, retyped or repurposed. In
  particular ``totals.yield_pct`` and ``totals.drop_kg`` keep the exact meaning
  and value they have always had — the net/waste numbers are NEW keys beside
  them, never a redefinition of the old ones;
* **weights are absent, never zero,** when the stock has no thickness or no
  known density. An absent key means "cannot be weighed". There is deliberately
  no fallback density: an invented kilo figure becomes a wrong purchase order;
* **what to BUY is its own number.** ``totals.sheets`` is what was opened,
  ``totals.sheets_to_buy`` is what procurement pays for. With a rack in play
  those differ, and only the second one goes on a purchase order.

The net-vs-gross pair is the design's "Métricas 2D" model:

    areaPz        = Σ placed part areas               -> part_area_mm2
    areaTotal     = Σ (w × h) over each sheet's OWN spec -> stock_area_mm2
    areaDevuelta  = Σ reclaimable leftovers           -> reclaimable_area_mm2
    areaConsumida = areaTotal − areaDevuelta          -> consumed_area_mm2
    neto  = areaPz / areaConsumida × 100              -> net_yield_pct
    bruto = areaPz / areaTotal × 100                  -> gross_yield_pct
    merma = areaTotal − areaPz − areaDevuelta         -> waste_area_mm2

The net denominator discounts the leftover that goes BACK on the rack, which is
the same rule the tube tool applies to a reclaimable retazo. It is what stops a
job from being punished for using material the shop had already paid for.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import NestResult

__all__ = ["as_dict"]


def _leftover_kind(result: NestResult, sheet, min_remnant: float) -> Optional[str]:
    """Whether this sheet's drop goes back on the rack, or is really lost.

    ``None`` when the job never asked (``min_remnant`` 0 turns the reporting
    off) — an honest "not measured", not a claim that there is no offcut.
    """
    if min_remnant <= 0:
        return None
    return "rack" if sheet.leftover else "scrap"


def as_dict(result: NestResult, job_name: str, meta: dict,
            warnings: List[str] | None = None) -> dict:
    """Serialize a :class:`NestResult`. See the module docstring for the rules."""
    spec = result.spec
    min_remnant = float(meta.get("min_remnant", 0) or 0)
    totals: Dict[str, Any] = {
        "sheets": result.sheet_count,
        "sheets_to_buy": result.new_sheets_needed,
        "remnants_used": result.remnants_used,
        # FROZEN: gross yield, under its original name and meaning.
        "yield_pct": round(result.yield_pct, 2),
        "parts_placed": sum(s.part_count for s in result.sheets),
        "parts_in_holes": result.in_hole_count,
        "unplaceable": len(result.unplaceable),
        "reclaimable_area_mm2": round(result.reclaimable_area, 2),
        # The areas the two yields are computed from, so a client can show the
        # formula instead of a bare percentage.
        "part_area_mm2": round(result.total_part_area, 2),
        "stock_area_mm2": round(result.total_sheet_area, 2),
        "new_stock_area_mm2": round(result.new_sheet_area, 2),
        "consumed_area_mm2": round(result.consumed_area, 2),
        "waste_area_mm2": round(result.waste_area, 2),
        "net_yield_pct": round(result.net_yield_pct, 2),
        "gross_yield_pct": round(result.gross_yield_pct, 2),
    }
    if result.can_weigh:
        totals.update({
            "parts_kg": round(result.parts_weight_kg, 3),
            "stock_kg": round(result.stock_weight_kg, 3),
            "to_buy_kg": round(result.new_stock_weight_kg, 3),
            # FROZEN: everything that does not leave as a part.
            "drop_kg": round(result.drop_weight_kg, 3),
            # NEW, and not the same thing: what came off the rack, what goes
            # back on it, and what is really lost (drop minus the returned
            # offcuts).
            "from_rack_kg": round(result.rack_stock_weight_kg, 3),
            "leftover_kg": round(result.reclaimable_weight_kg, 3),
            "waste_kg": round(result.waste_weight_kg, 3),
        })
    s_info = result.search
    totals["search"] = {
        "enabled": bool(s_info.enabled),
        "area_floor_sheets": s_info.area_floor_sheets,
        "ceiling_tried": list(s_info.ceiling_tried),
        "ceiling_used": s_info.ceiling_used,
        "attempts": s_info.attempts,
        "capped": bool(s_info.capped),
    }
    return {
        "job": job_name,
        "generated": meta.get("generated", ""),
        "warnings": list(warnings or []),
        "params": {
            "material": spec.material,
            "thickness": spec.thickness,
            "density_kg_m3": spec.density or None,
            "sheet_width": spec.width,
            "sheet_height": spec.height,
            "margin": spec.margin,
            "part_gap": spec.part_gap,
            # Reported so a client can compare gap against kerf. The engine does
            # NOT apply kerf compensation — that stays the CAM's job.
            "kerf_mm": float(meta.get("kerf", 0) or 0),
            "rotation": meta.get("rotation", ""),
            "nest_in_holes": bool(meta.get("nest_in_holes", False)),
            "min_remnant_mm": min_remnant,
            "fill_free_area": bool(meta.get("fill_free_area", False)),
            "minimize_sheets": bool(meta.get("minimize_sheets", False)),
            "max_new_sheets": int(meta.get("max_new_sheets", 0) or 0),
            "min_hole_side_mm": float(meta.get("min_hole_side", 0) or 0),
        },
        "totals": totals,
        "sheets": [
            {
                "sheet": s.index + 1,
                "source": s.source,
                "width": s.spec.width,
                "height": s.spec.height,
                "utilization_pct": round(s.utilization * 100, 2),
                "part_area_mm2": round(s.used_area, 2),
                "leftover": (
                    {
                        "x": round(s.leftover.x, 2), "y": round(s.leftover.y, 2),
                        "width": round(s.leftover.width, 2),
                        "height": round(s.leftover.height, 2),
                    } if s.leftover else None
                ),
                "leftover_kind": _leftover_kind(result, s, min_remnant),
                "parts": [
                    {
                        "name": p.part.name,
                        "x": round(p.x, 3),
                        "y": round(p.y, 3),
                        "rotation": round(p.rotation, 3),
                        "in_hole_of": p.in_hole_of,
                    }
                    for p in s.placements
                ],
            }
            for s in result.sheets
        ],
        "remnants_unused": [
            {"label": e.label, "width": e.width, "height": e.height,
             "reason": result.remnant_reasons.get(e.label, "job_ended")}
            for e in result.remnants_unused
        ],
        "reclaimable": [
            {"sheet": n, "x": round(lo.x, 2), "y": round(lo.y, 2),
             "width": round(lo.width, 2), "height": round(lo.height, 2)}
            for n, lo in result.reclaimable
        ],
        "unplaceable": [
            {"name": p.name, "width": round(p.size[0], 2), "height": round(p.size[1], 2),
             "qty": p.qty}
            for p in result.unplaceable
        ],
    }
