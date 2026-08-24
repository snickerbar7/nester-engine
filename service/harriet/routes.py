"""Routes + response mapping for the frozen Harriet contract.

Endpoints (all except /health require `Authorization: Bearer <token>`):

  GET  /health           -> liveness
  POST /extract          -> geometry -> parts (intent-independent, deterministic)
  POST /nest             -> parts -> cut plan (+ shop artifacts to R2)

Shapes here must not drift — they are what Harriet's `cut_plan` card parses.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX

from ..core import engine
from ..core.auth import Client, enforce_request_scope, require_client
from ..core.engine import InFile

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas — unchanged from the original service/app.py
# --------------------------------------------------------------------------- #

class FileRef(BaseModel):
    key: str = Field(..., description="R2 object key")
    filename: str = Field(..., description="original filename (profile+qty parsed from it)")


class ExtractRequest(BaseModel):
    files: List[FileRef]
    mode: str = Field("auto", description="tube | sheet | auto")
    profile_regex: str = DEFAULT_PROFILE_REGEX
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX
    unit: str = "mm"


class NestRequest(BaseModel):
    files: List[FileRef]
    mode: str = Field("auto", description="tube | sheet | auto")
    # tube stock
    stock_length: Optional[float] = None
    per_profile: Optional[Dict[str, float]] = None
    kerf: float = 0.0
    front_trim: float = 0.0
    back_trim: float = 0.0
    profile_regex: str = DEFAULT_PROFILE_REGEX
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX
    unit: str = "mm"
    # sheet stock
    sheet_width: Optional[float] = None
    sheet_height: Optional[float] = None
    material: str = ""
    thickness: float = 0.0
    margin: float = 8.0
    gap: float = 3.0
    rotate: str = "free"
    time_per_sheet: int = 4
    seed: int = 0
    # output
    job_name: str = "nest"
    lang: str = "es"
    out_prefix: Optional[str] = Field(
        None, description="R2 key prefix to write artifacts under; omit to skip artifacts")


def _infiles(files: List[FileRef]) -> List[InFile]:
    return [InFile(key=f.key, filename=f.filename) for f in files]


def _resolve_mode(mode: str, files: List[InFile]) -> str:
    return engine.infer_mode(files) if mode == "auto" else mode


# --------------------------------------------------------------------------- #
# Native (engine vocabulary) -> Harriet's camelCase NestResult
# --------------------------------------------------------------------------- #

def to_artifacts(arts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"key": a["key"], "filename": a["filename"],
         "contentType": a["content_type"], "size": a["size"]}
        for a in arts
    ]


def to_profile_plan(p: Dict[str, Any]) -> Dict[str, Any]:
    """Native profile result -> Harriet's ProfilePlan (types.ts)."""
    return {
        "profile": p["profile"],
        "bars": [
            {
                "barIndex": b["bar_index"],  # Harriet BarPlan.barIndex is 1-based
                "pieces": b["pieces_mm"],
                "drop": b["drop_mm"],
            }
            for b in p["bars"]
        ],
        "barsNeeded": p["bars_needed"],
        "totalPartLength": p["total_part_length_mm"],
        "totalDrop": p["total_drop_mm"],
        "yieldPct": p["yield_pct"],
        "usableLength": p["usable_length_mm"],
        "unplaceable": [
            {"length": u["length_mm"], "profile": u["profile"],
             "qty": u["qty"], "label": u["label"]}
            for u in p["unplaceable"]
        ],
    }


def to_extract_response(native: Dict[str, Any]) -> Dict[str, Any]:
    """Native extract -> Harriet's part list (unsuffixed length/width/height/area)."""
    if native["mode"] == "sheet":
        parts = [
            {"label": p["label"], "qty": p["qty"], "width": p["width_mm"],
             "height": p["height_mm"], "area": p["area_mm2"], "holes": p["holes"]}
            for p in native["parts"]
        ]
        return {"mode": "sheet", "parts": parts, "errors": native["errors"]}
    parts = [
        {"length": p["length_mm"], "profile": p["profile"],
         "qty": p["qty"], "label": p["label"]}
        for p in native["parts"]
    ]
    return {"mode": "tube", "parts": parts,
            "profiles": native["profiles"], "errors": native["errors"]}


def to_nest_response(native: Dict[str, Any]) -> Dict[str, Any]:
    """Native nest -> Harriet's NestResult envelope."""
    if native["mode"] == "sheet":
        # The 2D result is the engine's own JSON — Harriet passes it through.
        return {"mode": "sheet", "result": native["result"],
                "artifacts": to_artifacts(native["artifacts"]), "errors": native["errors"],
                "warnings": native.get("warnings", [])}
    result = {
        "profiles": [to_profile_plan(p) for p in native["result"]["profiles"]],
        "barsTotal": native["result"]["bars_total"],
    }
    return {"mode": "tube", "unit": native["unit"], "result": result,
            "artifacts": to_artifacts(native["artifacts"]), "errors": native["errors"],
            "warnings": native.get("warnings", [])}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("/health")
def health() -> Dict[str, object]:
    return {"ok": True, "service": "nester", "r2": bool(os.environ.get("R2_BUCKET_NAME"))}


@router.post("/extract")
def extract(req: ExtractRequest, client: Client = Depends(require_client)) -> Dict[str, object]:
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    enforce_request_scope(client, [f.key for f in files])
    mode = _resolve_mode(req.mode, files)
    try:
        if mode == "sheet":
            native = engine.extract_sheet(files, req.qty_regex)
        else:
            native = engine.extract_tube(files, req.profile_regex, req.qty_regex)
    except Exception as e:  # extraction failure -> 400 with the reason (data, never a stack)
        raise HTTPException(status_code=400, detail=str(e))
    return to_extract_response(native)


@router.post("/nest")
def nest(req: NestRequest, client: Client = Depends(require_client)) -> Dict[str, object]:
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    enforce_request_scope(client, [f.key for f in files], req.out_prefix)
    mode = _resolve_mode(req.mode, files)
    try:
        if mode == "sheet":
            if req.sheet_width is None or req.sheet_height is None:
                raise HTTPException(status_code=422, detail="sheet_width and sheet_height required")
            native = engine.nest_sheet(
                files, width=req.sheet_width, height=req.sheet_height, material=req.material,
                thickness=req.thickness, margin=req.margin, gap=req.gap, rotate=req.rotate,
                time_per_sheet=req.time_per_sheet, seed=req.seed, qty_regex=req.qty_regex,
                job_name=req.job_name, lang=req.lang, out_prefix=req.out_prefix,
            )
        else:
            if req.stock_length is None:
                raise HTTPException(status_code=422, detail="stock_length required for tube nesting")
            native = engine.nest_tube(
                files, stock_length=req.stock_length, per_profile=req.per_profile, kerf=req.kerf,
                front_trim=req.front_trim, back_trim=req.back_trim, profile_regex=req.profile_regex,
                qty_regex=req.qty_regex, unit=req.unit, job_name=req.job_name, lang=req.lang,
                out_prefix=req.out_prefix,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return to_nest_response(native)
