"""Orchestration over the existing tube + sheet pipelines.

Downloads inputs from R2, runs the unchanged Nester algorithms, and maps the
tube result onto Harriet's `NestResult` contract. Artifacts (PDF / nested DXF /
IGES) are written to a temp dir and uploaded back to R2.

Nothing here re-implements a solver — it calls `nester.tube` / `nester.sheet`.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import r2

# --- tube pipeline (unchanged engine) ---
from nester.tube.cli import _load_parts as _tube_load_parts
from nester.tube.model import ProfileResult, StockSpec
from nester.tube.packing import pack_all
from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX
from nester.tube.report import write_reports as _tube_write_reports, _slug

# --- sheet pipeline (unchanged engine) ---
from nester.sheet.dxf_read import read_parts as _sheet_read_parts
from nester.sheet.model import SheetSpec
from nester.sheet.pack import nest as _sheet_nest
from nester.sheet.report import write_reports as _sheet_write_reports, _as_dict as _sheet_as_dict

_IGES_EXTS = (".igs", ".iges")
_STEP_EXTS = (".stp", ".step")


@dataclass
class InFile:
    key: str
    filename: str


def infer_mode(files: List[InFile]) -> str:
    exts = {os.path.splitext(f.filename)[1].lower() for f in files}
    if exts & {".dxf"}:
        return "sheet"
    return "tube"  # iges/step -> tube


# --------------------------------------------------------------------------- #
# Download inputs from R2, preserving the ORIGINAL filename (profile+qty parse)
# --------------------------------------------------------------------------- #

def _materialize(files: List[InFile], into: str) -> List[str]:
    paths: List[str] = []
    for f in files:
        # Keep the real filename so profile/qty regexes still work. Guard against
        # path traversal from a caller-supplied name.
        safe = os.path.basename(f.filename) or "part"
        dest = os.path.join(into, safe)
        with open(dest, "wb") as fh:
            fh.write(r2.get_bytes(f.key))
        paths.append(dest)
    return paths


# --------------------------------------------------------------------------- #
# EXTRACT — intent-independent, deterministic (feeds Harriet's perception path)
# --------------------------------------------------------------------------- #

def extract_tube(files: List[InFile], profile_regex: str, qty_regex: Optional[str]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        paths = _materialize(files, tmp)
        parts, errors, _cross = _tube_load_parts(paths, profile_regex, qty_regex)
    # Aggregate the expanded Part copies back to {length, profile, qty, label}.
    agg: Dict[Tuple[str, float, str], int] = {}
    order: List[Tuple[str, float, str]] = []
    for p in parts:
        base = p.name.split(" #")[0]  # _load_parts appends " #i/qty" per copy
        k = (base, round(p.length, 4), p.profile)
        if k not in agg:
            agg[k] = 0
            order.append(k)
        agg[k] += 1
    out_parts = [
        {"length": length, "profile": profile, "qty": agg[k], "label": label}
        for k in order
        for (label, length, profile) in [k]
    ]
    return {
        "mode": "tube",
        "parts": out_parts,
        "profiles": sorted({p["profile"] for p in out_parts}),
        "errors": errors,
    }


def extract_sheet(files: List[InFile], qty_regex: Optional[str]) -> Dict[str, Any]:
    from nester.sheet.cli import quantity_from_filename  # shared qty parser

    parts_out: List[Dict[str, Any]] = []
    errors: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        paths = _materialize(files, tmp)
        for path in paths:
            name = os.path.basename(path)
            qty = quantity_from_filename(path, qty_regex) if qty_regex else 1
            try:
                fps = _sheet_read_parts(path, qty=qty)
            except Exception as e:  # DxfReadError / OSError
                errors.append(f"{name}: {e}")
                continue
            for fp in fps:
                w, h = fp.size
                parts_out.append({
                    "label": fp.name,
                    "qty": fp.qty,
                    "width": round(w, 3),
                    "height": round(h, 3),
                    "area": round(fp.area, 3),
                    "holes": len(fp.holes),
                })
    return {"mode": "sheet", "parts": parts_out, "errors": errors}


# --------------------------------------------------------------------------- #
# NEST — tube result mapped onto Harriet's NestResult; artifacts to R2
# --------------------------------------------------------------------------- #

def _profile_result_to_plan(r: ProfileResult) -> Dict[str, Any]:
    """Map a Python ProfileResult onto Harriet's ProfilePlan (types.ts)."""
    total_drop = sum(b.remnant for b in r.bars)
    return {
        "profile": r.profile,
        "bars": [
            {
                "barIndex": b.index + 1,  # Harriet BarPlan.barIndex is 1-based
                "pieces": [round(p.part.length, 4) for p in b.placements],
                "drop": round(b.remnant, 4),
            }
            for b in r.bars
        ],
        "barsNeeded": r.bar_count,
        "totalPartLength": round(r.total_part_length, 4),
        "totalDrop": round(total_drop, 4),
        "yieldPct": round(r.yield_pct, 2),
        "usableLength": round(r.spec.usable_length, 4),
        "unplaceable": [
            {"length": round(p.length, 4), "profile": r.profile, "qty": 1, "label": p.name}
            for p in r.unplaceable
        ],
    }


def _build_specs(profiles, stock_length, per_profile, kerf, front_trim, back_trim):
    overrides = {k.strip().lower(): float(v) for k, v in (per_profile or {}).items()}
    return {
        prof: StockSpec(
            profile=prof,
            stock_length=overrides.get(prof, stock_length),
            kerf=kerf,
            front_trim=front_trim,
            back_trim=back_trim,
        )
        for prof in profiles
    }


def _upload_artifacts(local_files: List[str], out_prefix: str) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    for path in local_files:
        fname = os.path.basename(path)
        key = f"{out_prefix.rstrip('/')}/{fname}"
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        r2.put_bytes(key, data, ctype)
        arts.append({"key": key, "filename": fname, "contentType": ctype, "size": len(data)})
    return arts


def nest_tube(
    files: List[InFile],
    *,
    stock_length: float,
    per_profile: Optional[Dict[str, float]] = None,
    kerf: float = 0.0,
    front_trim: float = 0.0,
    back_trim: float = 0.0,
    profile_regex: str = DEFAULT_PROFILE_REGEX,
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX,
    unit: str = "mm",
    job_name: str = "nest",
    lang: str = "es",
    out_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        paths = _materialize(files, tmp)
        parts, errors, cross = _tube_load_parts(paths, profile_regex, qty_regex)
        if not parts:
            raise ValueError("no readable parts: " + "; ".join(errors) if errors else "no parts")
        specs = _build_specs({p.profile for p in parts}, stock_length, per_profile, kerf, front_trim, back_trim)
        results: List[ProfileResult] = pack_all(parts, specs)

        nest_result = {
            "profiles": [_profile_result_to_plan(r) for r in results],
            "barsTotal": sum(r.bar_count for r in results),
        }

        artifacts: List[Dict[str, Any]] = []
        if out_prefix:
            out_dir = os.path.join(tmp, "out")
            meta = {"generated": "", "kerf": kerf, "front_trim": front_trim,
                    "back_trim": back_trim, "lang": lang}
            written = _tube_write_reports(results, out_dir, job_name, meta)
            try:
                from nester.tube.iges_nest import write_nest_iges
                iges_path = os.path.join(out_dir, f"{_slug(job_name)}_nest.igs")
                write_nest_iges(results, iges_path, cross)
                written.append(iges_path)
            except Exception:
                pass  # IGES preview is best-effort; the plan + PDF still stand
            artifacts = _upload_artifacts(written, out_prefix)

    return {"mode": "tube", "unit": unit, "result": nest_result,
            "artifacts": artifacts, "errors": errors}


def nest_sheet(
    files: List[InFile],
    *,
    width: float,
    height: float,
    material: str = "",
    thickness: float = 0.0,
    margin: float = 8.0,
    gap: float = 3.0,
    rotate: str = "free",
    time_per_sheet: int = 4,
    seed: int = 0,
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX,
    job_name: str = "nest",
    lang: str = "es",
    out_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    from nester.sheet.cli import quantity_from_filename

    with tempfile.TemporaryDirectory() as tmp:
        paths = _materialize(files, tmp)
        parts = []
        errors: List[str] = []
        for path in paths:
            qty = quantity_from_filename(path, qty_regex) if qty_regex else 1
            try:
                parts += _sheet_read_parts(path, qty=qty)
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        if not parts:
            raise ValueError("no readable parts: " + "; ".join(errors) if errors else "no parts")

        spec = SheetSpec(width=width, height=height, material=material,
                         thickness=thickness, margin=margin, part_gap=gap)
        result = _sheet_nest(parts, spec, rotation=rotate, time_per_sheet=time_per_sheet, seed=seed)
        meta = {"generated": "", "lang": lang, "rotation": rotate}
        result_json = _sheet_as_dict(result, job_name, meta)

        artifacts: List[Dict[str, Any]] = []
        if out_prefix:
            out_dir = os.path.join(tmp, "out")
            written = _sheet_write_reports(result, out_dir, job_name, meta)
            try:
                from nester.sheet.dxf_out import write_all_sheets
                written += write_all_sheets(result, out_dir, _slug(job_name))
            except Exception:
                pass
            artifacts = _upload_artifacts(written, out_prefix)

    return {"mode": "sheet", "result": result_json, "artifacts": artifacts, "errors": errors}
