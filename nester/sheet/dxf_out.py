"""Write a nested layout back out as one DXF per sheet — the machine deliverable.

Each sheet becomes a clean DXF with the parts placed at their nested positions,
preserving the Fusion cut-layer convention so the shop's CAM reads it directly:

    OUTER_PROFILES     -> each part's outer cut boundary
    INTERIOR_PROFILES  -> each part's holes
    SHEET              -> the stock sheet outline (reference; not a cut)

No lead-ins, kerf compensation, or cut ordering are added — those belong to the
shop's CAM. Geometry only, in millimetres.
"""

from __future__ import annotations

import os
from typing import List

import ezdxf

from .model import NestResult, SheetLayout
from .pack import transform

_SHEET_LAYER = "SHEET"
_OUTER_LAYER = "OUTER_PROFILES"
_INNER_LAYER = "INTERIOR_PROFILES"


def write_nested_dxf(sheet: SheetLayout, path: str, draw_sheet: bool = True) -> str:
    """Write one sheet's nested parts to ``path`` as a DXF."""
    doc = ezdxf.new(setup=True)
    doc.units = 4  # millimetres
    msp = doc.modelspace()
    for name, color in ((_OUTER_LAYER, 7), (_INNER_LAYER, 7), (_SHEET_LAYER, 8)):
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    if draw_sheet:
        w, h = sheet.spec.width, sheet.spec.height
        msp.add_lwpolyline(
            [(0, 0), (w, 0), (w, h), (0, h)], close=True, dxfattribs={"layer": _SHEET_LAYER}
        )

    for pl in sheet.placements:
        outer = transform(pl.part.outer, pl.rotation, pl.x, pl.y)
        msp.add_lwpolyline(outer, close=True, dxfattribs={"layer": _OUTER_LAYER})
        for hole in pl.part.holes:
            hp = transform(hole, pl.rotation, pl.x, pl.y)
            msp.add_lwpolyline(hp, close=True, dxfattribs={"layer": _INNER_LAYER})

    doc.saveas(path)
    return path


def write_all_sheets(result: NestResult, out_dir: str, slug: str, draw_sheet: bool = True) -> List[str]:
    """Write one DXF per sheet as ``<slug>_S01.dxf`` … Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    paths: List[str] = []
    n = result.sheet_count
    width = max(2, len(str(n)))
    for sheet in result.sheets:
        fname = f"{slug}_S{str(sheet.index + 1).zfill(width)}.dxf"
        paths.append(write_nested_dxf(sheet, os.path.join(out_dir, fname), draw_sheet))
    return paths
