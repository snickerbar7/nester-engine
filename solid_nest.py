#!/usr/bin/env python
"""Solid nesting exporter — the real CAD deliverable.

Runs under the OpenCASCADE venv (.venv-cad). Reads a nest.json produced by the
main tool plus the original part IGES files, then for every cut places that
part's REAL solid (wall thickness, corner radius, holes, copes — everything the
laser must cut) at its nested position along its stock bar.

By default it writes ONE file per stock bar (each a 6 m tube starting at the
origin with its pieces laid in a line) as both STEP and B-rep IGES, into a
`bars/` subfolder. Pass --combined for a single file with all bars stacked.

    .venv-cad/bin/python solid_nest.py \
        --nest output/Pantallas_LED/Pantallas_LED_corte.json \
        --src "/path/to/Pantallas_LED" \
        --out output/Pantallas_LED/Pantallas_LED

Why the original solids (not generated boxes): the parts have features — bolt
holes, fishmouth notches, the 'Barreno' drilling — that a box can't represent.
We transform the source geometry, never recreate it. We also keep ONLY the solid
bodies from each source file; loose construction curves in the IGES would
otherwise import as stray sketches.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import unicodedata

from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer, IGESControl_Controller
from OCP.STEPControl import STEPControl_Writer, STEPControl_StepModelType
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Trsf, gp_Ax1, gp_Pnt, gp_Dir, gp_Vec
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.TopoDS import TopoDS_Compound, TopoDS
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.TopTools import TopTools_ListOfShape, TopTools_IndexedMapOfShape
from OCP.TopExp import TopExp
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _compound(shapes) -> TopoDS_Compound:
    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for s in shapes:
        builder.Add(comp, s)
    return comp


def read_solids(path):
    """Read an IGES part and return a compound of its SOLID bodies only.

    Drops free construction curves/wires (which would import as sketches).
    Falls back to shells, then the raw shape, if no solid is present.
    """
    reader = IGESControl_Reader()
    if reader.ReadFile(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"could not read {path}")
    reader.TransferRoots()
    shp = reader.OneShape()
    if shp.IsNull():
        raise RuntimeError(f"no shape in {path}")

    for kind in (TopAbs_SOLID, TopAbs_SHELL):
        found = []
        exp = TopExp_Explorer(shp, kind)
        while exp.More():
            found.append(exp.Current())
            exp.Next()
        if found:
            return _compound(found)
    return shp  # last resort: whatever transferred


def _bbox(shape):
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    return b.Get()  # xmin,ymin,zmin,xmax,ymax,zmax


def place(shape, target_x: float, bar_y: float = 0.0):
    """Rotate so the part's longest axis runs along +X, then translate its
    min-corner to (target_x, bar_y, 0). Returns (placed_shape, width_y)."""
    xmin, ymin, zmin, xmax, ymax, zmax = _bbox(shape)
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin

    rot = gp_Trsf()
    if dz >= dx and dz >= dy:        # length along Z -> +90° about Y maps Z->X
        rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), math.pi / 2)
    elif dy >= dx and dy >= dz:      # length along Y -> -90° about Z maps Y->X
        rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), -math.pi / 2)
    # else already along X

    s1 = BRepBuilderAPI_Transform(shape, rot, True).Shape()

    # Normalize the cross-section rotation so the wider side is along Y for every
    # piece. A rectangular tube (e.g. 3×1.5) must sit in the stock with a single
    # consistent orientation — parts modeled in different roles can otherwise end
    # up rotated 90° about their own axis relative to each other.
    _x0, ymin, zmin, _x1, ymax, zmax = _bbox(s1)
    if (zmax - zmin) > (ymax - ymin) + 1e-6:
        norm = gp_Trsf()
        norm.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.pi / 2)
        s1 = BRepBuilderAPI_Transform(s1, norm, True).Shape()

    xmin, ymin, zmin, xmax, ymax, zmax = _bbox(s1)
    tr = gp_Trsf()
    tr.SetTranslation(gp_Vec(target_x - xmin, bar_y - ymin, 0.0 - zmin))
    s2 = BRepBuilderAPI_Transform(s1, tr, True).Shape()
    return s2, (ymax - ymin)


def stock_tube(W, H, L, wall, radius, y_offset=0.0):
    """A hollow rounded-rectangle stock tube: outer W×H, length L along X, wall
    thickness `wall`, outer corner radius `radius`. Placed at (0, y_offset, 0).
    Translucent envelope so the drop shows as empty tube — set transparency in CAD.
    """
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), L, W, H).Shape()

    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(box, TopAbs_EDGE, edges)
    fil = BRepFilletAPI_MakeFillet(box)
    r = max(0.1, min(radius, min(W, H) / 2 - 0.1))
    added = 0
    for i in range(1, edges.Extent() + 1):
        e = TopoDS.Edge_s(edges.FindKey(i))
        vs = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(e, TopAbs_VERTEX, vs)
        if vs.Extent() < 2:
            continue
        p0 = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vs.FindKey(1)))
        p1 = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vs.FindKey(2)))
        if abs(p0.X() - p1.X()) > 1 and abs(p0.Y() - p1.Y()) < 1e-6 and abs(p0.Z() - p1.Z()) < 1e-6:
            fil.Add(r, e); added += 1
    rounded = fil.Shape() if added else box

    # hollow: drop the two end caps (normal along X), offset inward by `wall`
    ends = TopTools_ListOfShape()
    fexp = TopExp_Explorer(rounded, TopAbs_FACE)
    while fexp.More():
        f = TopoDS.Face_s(fexp.Current())
        s = BRepAdaptor_Surface(f)
        if s.GetType() == GeomAbs_Plane and abs(s.Plane().Axis().Direction().X()) > 0.99:
            ends.Append(f)
        fexp.Next()
    try:
        mt = BRepOffsetAPI_MakeThickSolid()
        mt.MakeThickSolidByJoin(rounded, ends, -abs(wall), 1e-3)
        tube = mt.Shape()
    except Exception:
        tube = rounded  # fall back to a solid rounded bar if hollowing fails

    if y_offset:
        tr = gp_Trsf(); tr.SetTranslation(gp_Vec(0.0, y_offset, 0.0))
        tube = BRepBuilderAPI_Transform(tube, tr, True).Shape()
    return tube


def resolve_paths(src_dir: str):
    out = {}
    for f in os.listdir(src_dir):
        if f.lower().endswith((".igs", ".iges")):
            out[_norm(f)] = os.path.join(src_dir, f)
    return out


def _placed_parts(bar, front_trim, name_to_path, cache, bar_y=0.0):
    shapes = []
    for cut in bar["cuts"]:
        base = _norm(cut["part"].split(" #")[0])
        path = name_to_path.get(base)
        if not path:
            print(f"  ! missing source file for {base}")
            continue
        if path not in cache:
            cache[path] = read_solids(path)
        placed, _w = place(cache[path], front_trim + float(cut["start"]), bar_y)
        shapes.append((placed, _w))
    return shapes


def build_per_bar(nest_path, src_dir, opts):
    data = json.load(open(nest_path))
    front_trim = float(data.get("params", {}).get("front_trim", 0) or 0)
    name_to_path = resolve_paths(src_dir)
    cache = {}
    bars = []          # (label, compound, count)
    profiles = {}      # profile -> (W, H, L) for spares
    for prof in data["profiles"]:
        L = float(prof.get("stock_length", 6000))
        for i, bar in enumerate(prof["layout"], start=1):
            shapes = _placed_parts(bar, front_trim, name_to_path, cache)
            solids = [s for s, _ in shapes]
            members = list(solids)
            if solids:
                _x0, y0, z0, _x1, y1, z1 = _bbox(_compound(solids))
                W, H = y1 - y0, z1 - z0
                profiles[prof["profile"]] = (W, H, L)
                if opts["envelope"]:
                    members.append(stock_tube(W, H, L, opts["wall"], opts["radius"]))
            bars.append((f"{prof['profile']}_bar{i}", _compound(members), len(solids)))
    return bars, profiles


def build_combined(nest_path, src_dir, opts):
    data = json.load(open(nest_path))
    front_trim = float(data.get("params", {}).get("front_trim", 0) or 0)
    name_to_path = resolve_paths(src_dir)
    cache = {}
    members = []
    n_parts = 0
    cursor = 0.0
    for prof in data["profiles"]:
        L = float(prof.get("stock_length", 6000))
        prof_w = 0.0
        for bar in prof["layout"]:
            shapes = _placed_parts(bar, front_trim, name_to_path, cache, bar_y=cursor)
            solids = [s for s, _ in shapes]
            members += solids
            n_parts += len(solids)
            if solids:
                _x0, y0, z0, _x1, y1, z1 = _bbox(_compound(solids))
                W, H = y1 - y0, z1 - z0
                prof_w = max(prof_w, W)
                if opts["envelope"]:
                    members.append(stock_tube(W, H, L, opts["wall"], opts["radius"], y_offset=cursor))
            cursor += prof_w + max(40.0, prof_w * 0.8)
        cursor += max(40.0, prof_w * 0.8)
    return _compound(members), n_parts


def write_step(comp, path: str) -> None:
    w = STEPControl_Writer()
    w.Transfer(comp, STEPControl_StepModelType.STEPControl_AsIs)
    if w.Write(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed: {path}")


def write_iges(comp, path: str, brep: bool = True) -> None:
    # Build 3D curves for all edges FIRST. Their absence is what made B-rep IGES
    # (186) fragment on import (the "L-shape"); with them, 186 imports as clean
    # SOLID bodies — verified in Fusion (one solid per piece). So default to
    # B-rep solids; trimmed surfaces (mode 0, type 144) is the fallback (imports
    # as many separate surface bodies — geometry fine, but not solids).
    from OCP.BRepLib import BRepLib
    BRepLib.BuildCurves3d_s(comp)
    IGESControl_Controller.Init_s()
    w = IGESControl_Writer("MM", 1 if brep else 0)
    w.AddShape(comp)
    w.ComputeModel()
    if not w.Write(path):
        raise RuntimeError(f"IGES write failed: {path}")


def shop_package(src_dir: str, out_dir: str, material: str):
    """The deliverable a 3D tube-laser shop actually wants: ONE clean IGES per
    unique part (features preserved, free construction curves stripped) + a
    manifest of quantities. The shop's CAM nests these on its own machine.
    """
    import csv
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nester.tube.profile import (
        profile_from_filename, quantity_from_filename, ProfileParseError,
    )

    os.makedirs(out_dir, exist_ok=True)
    rows, written = [], []
    for f in sorted(os.listdir(src_dir)):
        if not f.lower().endswith((".igs", ".iges")):
            continue
        shp = read_solids(os.path.join(src_dir, f))   # solids only — no stray curves
        out_path = os.path.join(out_dir, os.path.splitext(f)[0] + ".igs")
        write_iges(shp, out_path)                       # trimmed-surface IGES
        written.append(out_path)
        try:
            prof = profile_from_filename(f)
        except ProfileParseError:
            prof = "?"
        x0, y0, z0, x1, y1, z1 = _bbox(shp)
        length = round(sorted([x1 - x0, y1 - y0, z1 - z0], reverse=True)[0], 1)
        rows.append({"file": os.path.basename(out_path),
                     "part": os.path.splitext(f)[0],
                     "profile": prof, "length_mm": length,
                     "qty": quantity_from_filename(f), "material": material})

    with open(os.path.join(out_dir, "manifest.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "part", "profile", "length_mm", "qty", "material"])
        w.writeheader(); w.writerows(rows)
    return written, rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a solid (STEP+IGES) tube nest.")
    ap.add_argument("--nest", help="nest JSON from the main tool (nesting modes only)")
    ap.add_argument("--src", required=True, help="directory of the original part IGES files")
    ap.add_argument("--out", required=True, help="output path prefix (no extension)")
    ap.add_argument("--shop-package", action="store_true",
                    help="export one clean IGES per unique part + manifest.csv (what "
                         "a tube-laser shop wants); the shop nests them")
    ap.add_argument("--material", default="Acero al carbon Cal.18",
                    help="material string for the shop-package manifest")
    ap.add_argument("--combined", action="store_true",
                    help="one file with all bars stacked, instead of one file per bar")
    ap.add_argument("--envelope", action="store_true",
                    help="add a full stock-tube around each bar's pieces (off by "
                         "default — it overlaps the parts and blurs the cut lines)")
    ap.add_argument("--spare", type=int, default=0, metavar="N",
                    help="also write N spare uncut stock-tube file(s) per profile")
    ap.add_argument("--wall", type=float, default=1.2, help="stock-tube wall mm (Cal.18≈1.2)")
    ap.add_argument("--radius", type=float, default=2.0, help="stock-tube outer corner radius mm")
    ap.add_argument("--no-step", action="store_true")
    ap.add_argument("--no-iges", action="store_true")
    ap.add_argument("--iges-surfaces", action="store_true",
                    help="write IGES as trimmed surfaces (type 144) instead of the "
                         "default B-rep solids (186) — only if a tool can't read solid IGES")
    args = ap.parse_args()

    if args.shop_package:
        out_dir = os.path.join(os.path.dirname(args.out) or ".", "shop_package")
        written, rows = shop_package(args.src, out_dir, args.material)
        total_pcs = sum(r["qty"] for r in rows)
        print(f"shop package: {len(rows)} unique part(s), {total_pcs} pieces total")
        for r in rows:
            print(f"  {r['file']}  {r['profile']}  {r['length_mm']:g}mm  ×{r['qty']}")
        print(f"  manifest.csv + {len(written)} IGES in {out_dir}")
        return 0

    if not args.nest:
        ap.error("--nest is required unless --shop-package is given")

    opts = {"envelope": args.envelope, "wall": args.wall, "radius": args.radius}
    base = os.path.basename(args.out)
    written = []

    def emit(comp, stem):
        if not args.no_step:
            p = stem + ".step"; write_step(comp, p); written.append(p)
        if not args.no_iges:
            p = stem + ".igs"; write_iges(comp, p, brep=not args.iges_surfaces); written.append(p)

    if args.combined:
        comp, n = build_combined(args.nest, args.src, opts)
        print(f"placed {n} solid part(s) in a combined nest")
        emit(comp, args.out + "_combined")
    else:
        bars, profiles = build_per_bar(args.nest, args.src, opts)
        bars_dir = os.path.join(os.path.dirname(args.out) or ".", "bars")
        os.makedirs(bars_dir, exist_ok=True)
        print(f"{len(bars)} stock bar(s):")
        for label, comp, cnt in bars:
            emit(comp, os.path.join(bars_dir, f"{base}_{label}"))
            print(f"  {label}: {cnt} piece(s)")
        for prof, (W, H, L) in profiles.items():
            for s in range(1, args.spare + 1):
                tube = _compound([stock_tube(W, H, L, args.wall, args.radius)])
                emit(tube, os.path.join(bars_dir, f"{base}_{prof}_spare{s}"))
                print(f"  {prof}_spare{s}: full uncut {L:g}mm tube")

    print("wrote:")
    for p in written:
        print(f"  {p}  ({os.path.getsize(p):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
