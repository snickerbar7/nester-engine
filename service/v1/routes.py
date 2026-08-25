"""Routes for the neutral `/v1` contract.

  GET    /v1/health      -> liveness
  POST   /v1/extract     -> geometry -> parts (tube or sheet; deterministic).
                            Sheet parts carry their REAL contour (outer + holes)
                            so a client can draw the silhouette, not a rectangle.
  POST   /v1/nest        -> tube parts -> cut plan (+ artifacts to R2)
  POST   /v1/jobs        -> 202: queue a 2D sheet nest (minutes of solve)
  GET    /v1/jobs/{id}   -> status · progress · result · artifacts
  DELETE /v1/jobs/{id}   -> request cancellation
  POST   /v1/uploads     -> presigned PUT URLs, so web clients can put CAD
                             files into R2 without holding R2 credentials
  POST   /v1/downloads   -> presigned GET URLs for the caller's own keys
                             (shop artifacts back out of R2)

Requests and responses are snake_case and name their units (`stock_length_mm`).
Responses are the native `service.core.engine` shape, returned as-is: no
per-client renaming lives here either.

Tube stays synchronous (FFD solves instantly); sheet is async by necessity —
see `service/v1/jobs.py` for how jobs run, what survives a restart, and why.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from nester.sheet.pack import ROTATION_MODES
from nester.tube.profile import DEFAULT_PROFILE_REGEX, DEFAULT_QTY_REGEX

from . import jobs
from ..core import engine, r2
from ..core.auth import Client, enforce_prefix, enforce_request_scope, require_client
from ..core.engine import InFile

router = APIRouter()

SHEET_NOT_IMPLEMENTED = (
    "2D sheet nesting is asynchronous: solves run for minutes, so POST /v1/nest "
    "does not accept mode=sheet. Submit it to the jobs API instead — "
    "POST /v1/jobs (202 + job_id), then poll GET /v1/jobs/{job_id}."
)

TUBE_IS_SYNC = (
    "POST /v1/jobs is for 2D sheet nesting only. Tube (1D) nesting solves "
    "instantly — call POST /v1/nest and get the plan in the response."
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


class ExtraStockRef(BaseModel):
    """One leftover piece of bar (retazo) offered to the nest as extra stock."""

    profile: str = Field(..., description="profile key as parsed from filenames (40x40x2)")
    length_mm: float = Field(..., description="the piece's real length in mm")
    label: str = Field(..., description="the shop's id for that piece (R-0001)")


class NestRequest(BaseModel):
    files: List[FileRef]
    mode: str = Field("auto", description="tube | sheet | auto (sheet -> 501)")
    stock_length_mm: Optional[float] = None
    per_profile_stock_length_mm: Optional[Dict[str, float]] = Field(
        None, description="per-profile stock length override, keyed by profile")
    extra_stock: List[ExtraStockRef] = Field(
        default_factory=list,
        description="remnants on the rack; the nest consumes them before buying "
                    "new bars. Entries for profiles not in the job are warned about, "
                    "not rejected.")
    kerf_mm: float = 0.0
    front_trim_mm: float = 0.0
    back_trim_mm: float = 0.0
    profile_regex: str = DEFAULT_PROFILE_REGEX
    qty_regex: Optional[str] = DEFAULT_QTY_REGEX
    job_name: str = "nest"
    lang: str = "es"
    out_prefix: Optional[str] = Field(
        None, description="key prefix to write artifacts under; omit to skip artifacts")


class JobRequest(NestRequest):
    """A 2D sheet nest, submitted for async execution.

    Same envelope as `POST /v1/nest` (files, regexes, job_name, lang) plus the
    sheet stock parameters, with two differences that are structural, not
    stylistic: `mode` must be sheet (tube is synchronous), and `out_prefix` is
    REQUIRED — it is both where the artifacts land and where the job's durable
    status record (`<out_prefix>/_job.json`) is written.
    """

    mode: str = Field("sheet", description="must resolve to sheet (tube -> use /v1/nest)")
    out_prefix: str = Field(
        ..., description="key prefix for artifacts AND the job record (_job.json)")
    sheet_width_mm: float = Field(..., description="stock sheet extent in X (mm)")
    sheet_height_mm: float = Field(..., description="stock sheet extent in Y (mm)")
    material: str = Field("", description="material name — labels the stock group")
    thickness_mm: float = 0.0
    margin_mm: float = Field(8.0, description="unusable band along every sheet edge")
    gap_mm: float = Field(3.0, description="minimum part-to-part spacing; keep >= kerf")
    rotate: str = Field("free", description="free | grain | fixed | ortho")
    time_per_sheet_s: int = Field(4, description="compute budget per sheet (seconds)")
    seed: int = Field(0, description="RNG seed — same seed, same nest")


class UploadFileRequest(BaseModel):
    key: str = Field(..., description="object key (opaque to the service)")
    content_type: str = Field(..., description="MIME type for the presigned PUT")


class UploadRequest(BaseModel):
    files: List[UploadFileRequest]


class DownloadFileRequest(BaseModel):
    key: str = Field(..., description="object key (opaque to the service)")
    filename: str = Field("", description="optional save-as name (Content-Disposition)")


class DownloadRequest(BaseModel):
    files: List[DownloadFileRequest]


UPLOAD_URL_EXPIRES_IN = 900
MAX_UPLOAD_FILES = 50


def _infiles(files: List[FileRef]) -> List[InFile]:
    return [InFile(key=f.key, filename=f.filename) for f in files]


def _resolve_mode(mode: str, files: List[InFile]) -> str:
    return engine.infer_mode(files) if mode == "auto" else mode


def _validate_extra_stock(entries: List[ExtraStockRef]) -> List[Dict[str, Any]]:
    """Reject unusable remnants (422). A remnant with no label can't be pulled
    off the rack, and a non-positive length isn't a piece of bar."""
    out: List[Dict[str, Any]] = []
    for e in entries:
        if not e.label.strip():
            raise HTTPException(status_code=422, detail="extra_stock: label must not be empty")
        if e.length_mm <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"extra_stock {e.label!r}: length_mm must be > 0, got {e.length_mm}")
        if not e.profile.strip():
            raise HTTPException(
                status_code=422, detail=f"extra_stock {e.label!r}: profile must not be empty")
        out.append({"profile": e.profile, "length_mm": e.length_mm, "label": e.label.strip()})
    return out


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
            # v1 clients DRAW the parts: ship the real silhouette, decimated.
            return engine.extract_sheet(files, req.qty_regex, include_contours=True)
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
    extra_stock = _validate_extra_stock(req.extra_stock)
    try:
        return engine.nest_tube(
            files, stock_length=req.stock_length_mm,
            per_profile=req.per_profile_stock_length_mm, kerf=req.kerf_mm,
            front_trim=req.front_trim_mm, back_trim=req.back_trim_mm,
            extra_stock=extra_stock,
            profile_regex=req.profile_regex, qty_regex=req.qty_regex, unit="mm",
            job_name=req.job_name, lang=req.lang, out_prefix=req.out_prefix,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------- #
# Async jobs — 2D sheet nesting (E1 / A5). See service/v1/jobs.py.
# --------------------------------------------------------------------------- #

def _owned_job(job_id: str, client: Client) -> Optional[jobs.Job]:
    """The in-memory job, if this client owns it. Another client's job is 404,
    not 403 — a caller shouldn't learn that someone else's id exists."""
    job = jobs.lookup(job_id)
    if job is None or job.client_id != client.client_id:
        return None
    return job


@router.post("/jobs", status_code=202)
def create_job(req: JobRequest, client: Client = Depends(require_client)) -> Dict[str, Any]:
    r2.caller_buckets.set(None)
    files = _infiles(req.files)
    if not files:
        raise HTTPException(status_code=422, detail="no files")
    if not req.out_prefix.strip():
        raise HTTPException(status_code=422, detail="out_prefix must not be empty")
    enforce_request_scope(client, [f.key for f in files], req.out_prefix)

    mode = _resolve_mode(req.mode, files)
    if mode != "sheet":
        raise HTTPException(status_code=422, detail=TUBE_IS_SYNC)
    if req.rotate not in ROTATION_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown rotate '{req.rotate}'. Options: {sorted(ROTATION_MODES)}")
    if req.sheet_width_mm <= 0 or req.sheet_height_mm <= 0:
        raise HTTPException(status_code=422, detail="sheet_width_mm and sheet_height_mm must be > 0")
    if req.margin_mm < 0 or req.gap_mm < 0:
        raise HTTPException(status_code=422, detail="margin_mm and gap_mm must be >= 0")
    if req.sheet_width_mm - 2 * req.margin_mm <= 0 or req.sheet_height_mm - 2 * req.margin_mm <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"margin_mm {req.margin_mm} leaves no usable area on a "
                   f"{req.sheet_width_mm:g}x{req.sheet_height_mm:g}mm sheet")
    if req.time_per_sheet_s < 1:
        raise HTTPException(status_code=422, detail="time_per_sheet_s must be >= 1")

    job = jobs.submit(client.client_id, jobs.SheetJobParams(
        files=files, width=req.sheet_width_mm, height=req.sheet_height_mm,
        material=req.material, thickness=req.thickness_mm, margin=req.margin_mm,
        gap=req.gap_mm, rotate=req.rotate, time_per_sheet=req.time_per_sheet_s,
        seed=req.seed, qty_regex=req.qty_regex, job_name=req.job_name,
        lang=req.lang, out_prefix=req.out_prefix,
    ))
    # "queued" is the ACK of acceptance, not a live read: with a free worker the
    # solve may already have started by the time this returns. GET is the truth.
    return {"job_id": job.job_id, "status": jobs.QUEUED,
            "out_prefix": job.out_prefix, "job_record_key": jobs.job_key(job.out_prefix)}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    out_prefix: Optional[str] = Query(
        None, description="the job's out_prefix — lets a FINISHED job be restored "
                          "from its persisted record after a service restart"),
    client: Client = Depends(require_client),
) -> Dict[str, Any]:
    r2.caller_buckets.set(None)
    job = _owned_job(job_id, client)
    if job is not None:
        return jobs.to_body(job)

    # Not in this instance's registry. Either it was evicted / the container
    # restarted after the job finished (recoverable from R2), or the run itself
    # died with the process (status "lost" — the client re-submits).
    if out_prefix:
        enforce_prefix(client, [out_prefix])
        restored = jobs.restore(job_id, out_prefix, client.client_id)
        if restored is not None:
            body = jobs.to_body(restored)
            body["restored_from"] = "r2"
            return body
    return jobs.lost_body(job_id)


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: str, client: Client = Depends(require_client)) -> Dict[str, Any]:
    r2.caller_buckets.set(None)
    job = _owned_job(job_id, client)
    if job is None:
        raise HTTPException(status_code=404, detail=jobs.LOST_MESSAGE)
    return jobs.cancel(job)


@router.post("/uploads")
def uploads(req: UploadRequest, client: Client = Depends(require_client)) -> Dict[str, Any]:
    # Sync endpoints run in a threadpool; a prior harriet request on this thread
    # may have left its caller bucket pair set. v1 always uses the env pair.
    r2.caller_buckets.set(None)
    files = req.files
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=422, detail=f"files must contain 1-{MAX_UPLOAD_FILES} entries")
    for f in files:
        if not f.content_type or not f.content_type.strip():
            raise HTTPException(
                status_code=422, detail=f"empty content_type for key {f.key!r}")
    enforce_request_scope(client, [f.key for f in files])
    return {"uploads": [
        {"key": f.key,
         "url": r2.presign_put(f.key, f.content_type, UPLOAD_URL_EXPIRES_IN),
         "expires_in": UPLOAD_URL_EXPIRES_IN}
        for f in files
    ]}


@router.post("/downloads")
def downloads(req: DownloadRequest, client: Client = Depends(require_client)) -> Dict[str, Any]:
    # Sync endpoints run in a threadpool; a prior harriet request on this thread
    # may have left its caller bucket pair set. v1 always uses the env pair.
    r2.caller_buckets.set(None)
    files = req.files
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=422, detail=f"files must contain 1-{MAX_UPLOAD_FILES} entries")
    enforce_request_scope(client, [f.key for f in files])
    return {"downloads": [
        {"key": f.key,
         "url": r2.presign_get(f.key, UPLOAD_URL_EXPIRES_IN, f.filename),
         "expires_in": UPLOAD_URL_EXPIRES_IN}
        for f in files
    ]}
