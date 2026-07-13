"""FastAPI surface for the Nester engine.

Endpoints (all except /health require `Authorization: Bearer $NESTER_SERVICE_TOKEN`):

  GET  /health           -> liveness
  POST /extract          -> geometry -> parts (intent-independent, deterministic)
  POST /nest             -> parts -> cut plan (+ shop artifacts to R2)

Run:  uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8000}
"""

from __future__ import annotations

import hmac
import os
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import engine
from .engine import InFile
from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX

app = FastAPI(title="Harriet Nester", version="1.0.0")


# --------------------------------------------------------------------------- #
# Auth — constant-time bearer check; fail closed if the token isn't configured.
# --------------------------------------------------------------------------- #

def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("NESTER_SERVICE_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="service token not configured")
    prefix = "Bearer "
    got = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


# --------------------------------------------------------------------------- #
# Schemas
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
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> Dict[str, object]:
    return {"ok": True, "service": "nester", "r2": bool(os.environ.get("R2_BUCKET_NAME"))}


@app.post("/extract", dependencies=[Depends(require_token)])
def extract(req: ExtractRequest) -> Dict[str, object]:
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    mode = _resolve_mode(req.mode, files)
    try:
        if mode == "sheet":
            return engine.extract_sheet(files, req.qty_regex)
        return engine.extract_tube(files, req.profile_regex, req.qty_regex)
    except Exception as e:  # extraction failure -> 400 with the reason (data, never a stack)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/nest", dependencies=[Depends(require_token)])
def nest(req: NestRequest) -> Dict[str, object]:
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    mode = _resolve_mode(req.mode, files)
    try:
        if mode == "sheet":
            if req.sheet_width is None or req.sheet_height is None:
                raise HTTPException(status_code=422, detail="sheet_width and sheet_height required")
            return engine.nest_sheet(
                files, width=req.sheet_width, height=req.sheet_height, material=req.material,
                thickness=req.thickness, margin=req.margin, gap=req.gap, rotate=req.rotate,
                time_per_sheet=req.time_per_sheet, seed=req.seed, qty_regex=req.qty_regex,
                job_name=req.job_name, lang=req.lang, out_prefix=req.out_prefix,
            )
        if req.stock_length is None:
            raise HTTPException(status_code=422, detail="stock_length required for tube nesting")
        return engine.nest_tube(
            files, stock_length=req.stock_length, per_profile=req.per_profile, kerf=req.kerf,
            front_trim=req.front_trim, back_trim=req.back_trim, profile_regex=req.profile_regex,
            qty_regex=req.qty_regex, unit=req.unit, job_name=req.job_name, lang=req.lang,
            out_prefix=req.out_prefix,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
