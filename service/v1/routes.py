"""Routes for the neutral `/v1` contract.

  GET  /v1/health        -> liveness
  POST /v1/extract       -> geometry -> parts (tube or sheet; deterministic)
  POST /v1/nest          -> tube parts -> cut plan (+ artifacts to R2)

Requests and responses are snake_case and name their units (`stock_length_mm`).
Responses are the native `service.core.engine` shape, returned as-is: no
per-client renaming lives here either.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX

from ..core import engine, r2
from ..core.auth import Client, enforce_request_scope, require_client
from ..core.engine import InFile

router = APIRouter()

SHEET_NOT_IMPLEMENTED = (
    "2D sheet nesting is not available on POST /v1/nest: solves run for minutes "
    "and must go through the async jobs API (POST /v1/jobs), which is not "
    "released yet. POST /v1/extract with mode=sheet works today."
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class FileRef(BaseModel):
    key: str = Field(..., description="object key (opaque to the service)")
    filename: str = Field(..., description="original filename (profile+qty parsed from it)")


class ExtractRequest(BaseModel):
    files: List[FileRef]
    mode: str = Field("auto", description="tube | sheet | auto")
    profile_regex: str = DEFAULT_PROFILE_REGEX
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX


class NestRequest(BaseModel):
    files: List[FileRef]
    mode: str = Field("auto", description="tube | sheet | auto (sheet -> 501)")
    stock_length_mm: Optional[float] = None
    per_profile_stock_length_mm: Optional[Dict[str, float]] = Field(
        None, description="per-profile stock length override, keyed by profile")
    kerf_mm: float = 0.0
    front_trim_mm: float = 0.0
    back_trim_mm: float = 0.0
    profile_regex: str = DEFAULT_PROFILE_REGEX
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX
    job_name: str = "nest"
    lang: str = "es"
    out_prefix: Optional[str] = Field(
        None, description="key prefix to write artifacts under; omit to skip artifacts")


def _infiles(files: List[FileRef]) -> List[InFile]:
    return [InFile(key=f.key, filename=f.filename) for f in files]


def _resolve_mode(mode: str, files: List[InFile]) -> str:
    return engine.infer_mode(files) if mode == "auto" else mode


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("/health")
def health() -> Dict[str, object]:
    return {"ok": True, "service": "nester", "contract": "v1",
            "r2": bool(os.environ.get("R2_BUCKET_NAME"))}


@router.post("/extract")
def extract(req: ExtractRequest, client: Client = Depends(require_client)) -> Dict[str, Any]:
    # Sync endpoints run in a threadpool; a prior harriet request on this thread
    # may have left its caller bucket pair set. v1 always uses the env pair.
    r2.caller_buckets.set(None)
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    enforce_request_scope(client, [f.key for f in files])
    mode = _resolve_mode(req.mode, files)
    try:
        if mode == "sheet":
            return engine.extract_sheet(files, req.qty_regex)
        return engine.extract_tube(files, req.profile_regex, req.qty_regex)
    except Exception as e:  # extraction failure -> 400 with the reason (data, never a stack)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/nest")
def nest(req: NestRequest, client: Client = Depends(require_client)) -> Dict[str, Any]:
    # Sync endpoints run in a threadpool; a prior harriet request on this thread
    # may have left its caller bucket pair set. v1 always uses the env pair.
    r2.caller_buckets.set(None)
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    enforce_request_scope(client, [f.key for f in files], req.out_prefix)
    mode = _resolve_mode(req.mode, files)
    if mode == "sheet":
        raise HTTPException(status_code=501, detail=SHEET_NOT_IMPLEMENTED)
    if req.stock_length_mm is None:
        raise HTTPException(status_code=422, detail="stock_length_mm required for tube nesting")
    try:
        return engine.nest_tube(
            files, stock_length=req.stock_length_mm,
            per_profile=req.per_profile_stock_length_mm, kerf=req.kerf_mm,
            front_trim=req.front_trim_mm, back_trim=req.back_trim_mm,
            profile_regex=req.profile_regex, qty_regex=req.qty_regex, unit="mm",
            job_name=req.job_name, lang=req.lang, out_prefix=req.out_prefix,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
