"""Orchestration over the existing tube + sheet pipelines.

Downloads inputs from R2, runs the unchanged Nester algorithms, and returns the
NATIVE result shape: snake_case, the engine's own vocabulary (`bars_needed`,
`yield_pct`, `stock_length_mm`, ...). Artifacts (PDF / nested DXF / IGES) are
written to a temp dir and uploaded back to R2 under the caller's opaque prefix.

Nothing here re-implements a solver — it calls `nester.tube` / `nester.sheet`.
Nothing here knows about a specific client: contract packages adapt this shape.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import r2  # object keys are opaque strings supplied by the caller

# --- tube pipeline (unchanged engine) ---
from nester.tube.cli import _load_parts as _tube_load_parts
from nester.tube.model import ExtraStock, ProfileResult, StockSpec
from nester.tube.packing import pack_all
from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX, normalize_profile
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
# EXTRACT — intent-independent, deterministic (geometry in, parts out)
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
        {"label": label, "profile": profile, "qty": agg[k], "length_mm": length}
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
                    "width_mm": round(w, 3),
                    "height_mm": round(h, 3),
                    "area_mm2": round(fp.area, 3),
                    "holes": len(fp.holes),
                })
    return {"mode": "sheet", "parts": parts_out, "errors": errors}


# --------------------------------------------------------------------------- #
# NEST — native (engine-vocabulary) result; artifacts to R2
# --------------------------------------------------------------------------- #

def profile_result_to_dict(r: ProfileResult) -> Dict[str, Any]:
    """One packed profile in the engine's own vocabulary (snake_case, mm)."""
    total_drop = sum(b.remnant for b in r.bars)
    return {
        "profile": r.profile,
        "bars_needed": r.bar_count,          # TOTAL bars used (tramos + remnants)
        "new_bars_needed": r.new_bars_needed,  # full tramos to BUY
        "remnants_used": r.remnants_used,      # remnant labels, in bar order
        "stock_length_mm": round(r.spec.stock_length, 4),
        "usable_length_mm": round(r.spec.usable_length, 4),
        "total_part_length_mm": round(r.total_part_length, 4),
        "total_drop_mm": round(total_drop, 4),
        "yield_pct": round(r.yield_pct, 2),
        "bars": [
            {
                "bar_index": b.index + 1,  # 1-based: bar 1 is the first bar off the rack
                "stock_length_mm": round(b.stock_length, 4),
                "source": b.source,        # "nuevo" for a tramo, else the remnant label
                "pieces_mm": [round(p.part.length, 4) for p in b.placements],
                "drop_mm": round(b.remnant, 4),
            }
            for b in r.bars
        ],
        "unplaceable": [
            {"label": p.name, "profile": r.profile, "qty": 1, "length_mm": round(p.length, 4)}
            for p in r.unplaceable
        ],
    }


def _build_specs(profiles, stock_length, per_profile, kerf, front_trim, back_trim,
                 extra_stock=None):
    """Stock spec per profile, plus warnings for remnants nothing in the job matches.

    ``extra_stock`` entries are ``{"profile", "length_mm", "label"}``; their
    profile is normalized the same way parsed filenames are, and an entry that
    matches no profile in the job is a warning, never an error — the job is
    still nestable without that retazo.
    """
    overrides = {normalize_profile(k.strip()): float(v)
                 for k, v in (per_profile or {}).items()}

    extras: Dict[str, List[ExtraStock]] = {}
    warnings: List[str] = []
    for e in extra_stock or []:
        raw = str(e["profile"]).strip()
        prof = normalize_profile(raw)
        label = str(e["label"])
        if prof not in profiles:
            warnings.append(
                f"retazo {label} ignorado: perfil '{raw}' no está en el trabajo")
            continue
        extras.setdefault(prof, []).append(
            ExtraStock(length=float(e["length_mm"]), label=label))

    specs = {
        prof: StockSpec(
            profile=prof,
            stock_length=overrides.get(prof, stock_length),
            kerf=kerf,
            front_trim=front_trim,
            back_trim=back_trim,
            extra_stock=tuple(extras.get(prof, ())),
        )
        for prof in profiles
    }
    return specs, warnings


def _upload_artifacts(local_files: List[str], out_prefix: str) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    for path in local_files:
        fname = os.path.basename(path)
        key = f"{out_prefix.rstrip('/')}/{fname}"
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        r2.put_bytes(key, data, ctype)
        arts.append({"key": key, "filename": fname, "content_type": ctype, "size": len(data)})
    return arts


def nest_tube(
    files: List[InFile],
    *,
    stock_length: float,
    per_profile: Optional[Dict[str, float]] = None,
    kerf: float = 0.0,
    front_trim: float = 0.0,
    back_trim: float = 0.0,
    extra_stock: Optional[List[Dict[str, Any]]] = None,
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
        specs, stock_warnings = _build_specs(
            {p.profile for p in parts}, stock_length, per_profile, kerf,
            front_trim, back_trim, extra_stock)
        results: List[ProfileResult] = pack_all(parts, specs)

        nest_result = {
            "bars_total": sum(r.bar_count for r in results),
            "new_bars_total": sum(r.new_bars_needed for r in results),
            "profiles": [profile_result_to_dict(r) for r in results],
        }

        artifacts: List[Dict[str, Any]] = []
        warnings: List[str] = list(stock_warnings)
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
            except Exception as e:
                # IGES preview is best-effort; the plan + PDF still stand,
                # but the caller must hear that a deliverable is missing.
                warnings.append(f"IGES nest layout not written: {e}")
            artifacts = _upload_artifacts(written, out_prefix)

    return {"mode": "tube", "unit": unit, "result": nest_result,
            "artifacts": artifacts, "errors": errors, "warnings": warnings}


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
        warnings: List[str] = []
        if out_prefix:
            out_dir = os.path.join(tmp, "out")
            written = _sheet_write_reports(result, out_dir, job_name, meta)
            try:
                from nester.sheet.dxf_out import write_all_sheets
                written += write_all_sheets(result, out_dir, _slug(job_name))
            except Exception as e:
                # Nested DXFs are the shop deliverable — never lose the reason.
                warnings.append(f"nested DXF per sheet not written: {e}")
            artifacts = _upload_artifacts(written, out_prefix)

    return {"mode": "sheet", "result": result_json, "artifacts": artifacts,
            "errors": errors, "warnings": warnings}
