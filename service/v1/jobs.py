"""Async jobs (E1 / A5) — 2D sheet solves that outlive an HTTP request.

A tube nest is First-Fit-Decreasing: milliseconds, so it stays synchronous on
``POST /v1/nest``. A sheet nest is minutes of stochastic search (``time_per_sheet``
seconds per sheet, several sheets), which no browser request should hold open.
So sheet nesting is a JOB: submit, poll, cancel.

WHAT THIS IS (and deliberately is not)
--------------------------------------
The service is ONE Render instance with **no database**. This module is sized to
exactly that:

  * **execution** — a module-level ``ThreadPoolExecutor`` with
    :data:`MAX_CONCURRENT_SOLVES` (2) workers. The solve runs in-process, in a
    thread, in the same container that answered ``POST /v1/jobs``. Beyond two
    concurrent solves, jobs sit in ``queued`` — honestly reported, not hidden.
  * **registry** — an in-memory dict (:data:`_REGISTRY`), lock-guarded, capped at
    :data:`MAX_REGISTRY` entries (oldest FINISHED jobs evicted first; a running
    job is never evicted).
  * **durability** — every status transition (and every completed sheet) is
    written to R2 as ``<out_prefix>/_job.json``, under the CALLER's own prefix.
    That file, not the registry, is the record that survives a restart. Writing
    it is best-effort: an R2 failure degrades durability, never the solve, and
    is reported in the job's ``warnings``.

LIMITATIONS — read before relying on this
-----------------------------------------
1. **A restart mid-solve loses the run.** The container is the worker; there is
   no queue to redeliver from. After a restart, ``GET /v1/jobs/{id}`` reports
   status ``"lost"`` with an explicit message, and the client re-submits. It
   never reports ``running`` for something nothing is running.
2. **Job ids are not an index.** With the registry gone, the service cannot map
   an id back to an out_prefix on its own. Pass ``?out_prefix=`` on GET and a
   FINISHED job is restored from ``_job.json`` (result and artifacts intact);
   without it — or if the persisted record was still mid-flight — the answer is
   ``"lost"``.
3. **Cancel is between sheets.** ``DELETE`` sets a flag the multi-sheet loop
   checks before starting the next sheet; the sheet in flight still burns its
   ``time_per_sheet`` budget (one spyrrow solve is an opaque call).
4. **Scale is one instance.** Two instances behind a load balancer would not see
   each other's registries. Don't scale out on this design.

WHAT A SHEET SOLVE NOW COSTS (read with limitation 3 above)
-----------------------------------------------------------
`minimize_sheets` (default TRUE) makes one job several full solves, not one.
The engine re-nests under a ceiling on how many NEW sheets it may buy and lowers
that ceiling while the job stays feasible; each attempt is its own multi-sheet
walk. Consequences for this module, all of them deliberate:

  * **elapsed time is a small multiple of the greedy time.** Typical jobs settle
    in 2-4 attempts; `sheet_search_budget_s` caps the whole search in wall clock
    and `max_new_sheets` caps how far it will look. Neither can fail a job — on
    either limit the best FEASIBLE nest found comes back with
    `result.totals.search.capped = true`, and if nothing was feasible at all the
    engine falls back to the unbounded greedy walk.
  * **`progress.sheets_done` restarts at zero on each attempt.** That is not the
    solve going backwards, so `progress` carries `attempt` and
    `new_sheet_ceiling` alongside it. A client that draws a bar must key it to
    the attempt, and `sheets_total_estimate` is now genuinely bounded (a ceiling
    is a real limit, not a projection) instead of only projected from density.
  * **cancel is still checked between sheets, and now also between attempts.**
    The sheet in flight still burns its `time_per_sheet` budget first, and
    `NestCancelled` carries the best complete nest found so far rather than the
    partial sheet stack of whichever attempt happened to be running.

`kerf_mm` rides the request but never reaches the solver: it is reported back on
`result.params.kerf_mm` so a client can check `gap_mm` against it. Kerf
compensation is the CAM's job and the engine does not do it.

UPGRADE PATH (when any of the above starts hurting)
---------------------------------------------------
Move the registry into a ``jobs`` table in Postgres (Neon is already the product
DB) with columns mirroring :class:`Job` — ``id, client_id, out_prefix, status,
progress jsonb, result jsonb, artifacts jsonb, error, created_at, updated_at``,
plus a ``heartbeat_at`` so a crashed worker's jobs can be reaped into ``lost``
by a sweeper instead of inferred from a missing registry entry. Then swap the
ThreadPoolExecutor for a Render background worker pulling ``queued`` rows
(``SELECT ... FOR UPDATE SKIP LOCKED``). The HTTP contract in
``service/v1/routes.py`` is written to survive that change untouched: same
statuses, same progress fields, same bodies — only ``lost`` becomes rarer, and
``?out_prefix=`` becomes unnecessary rather than wrong.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nester.sheet.pack import NestCancelled

from ..core import engine, r2
from ..core.engine import InFile

# Two concurrent solves: a sheet nest is CPU-bound in Rust (spyrrow releases the
# GIL), and the Render instance is small. More would just make every job slower.
MAX_CONCURRENT_SOLVES = 2

# In-memory registry cap. Finished jobs are evicted oldest-first; the durable
# record lives in R2, so eviction costs a poll a `?out_prefix=` round trip.
MAX_REGISTRY = 500

# The durable record, under the caller's own output prefix.
JOB_FILE = "_job.json"

QUEUED, RUNNING, DONE, ERROR, CANCELLED, LOST = (
    "queued", "running", "done", "error", "cancelled", "lost")
TERMINAL = (DONE, ERROR, CANCELLED)

LOST_MESSAGE = (
    "job not found in this instance's registry. The service is a single "
    "instance with no job database: a restart loses any solve that was in "
    "flight. Re-submit the job (POST /v1/jobs). If the job had already "
    "finished, poll again with ?out_prefix=<the job's out_prefix> to read its "
    "persisted result."
)


# --------------------------------------------------------------------------- #
# Job record
# --------------------------------------------------------------------------- #

@dataclass
class SheetJobParams:
    """Everything one sheet solve needs — the request, minus the HTTP.

    ``files`` are :class:`InFile`s, so each carries its own ``sets`` (juegos)
    multiplier straight through to the solve — nothing here needs to know.
    """

    files: List[InFile]
    width: float
    height: float
    material: str = ""
    thickness: float = 0.0
    margin: float = 8.0
    gap: float = 3.0
    rotate: str = "free"
    time_per_sheet: int = 4
    seed: int = 0
    # The rack and the two switches that change what a nest is allowed to do:
    # retazos to spend before buying (E16), parts nested in holes (E15), and the
    # shortest offcut side still worth reclaiming. `density` (kg/m3) is the
    # caller's override for what `material` resolves to; None = resolve it (E8).
    extra_sheets: List[Dict[str, Any]] = field(default_factory=list)
    nest_in_holes: bool = False
    min_remnant: float = 0.0
    density: Optional[float] = None
    # How hard to look for the FEWEST new sheets. `minimize_sheets` off is the
    # old greedy walk (a faster answer, not a better one); the other three bound
    # the search so one request cannot monopolise a worker. `kerf` is REPORTED
    # only -- kerf compensation stays the CAM's job, so it never reaches the
    # solver, it only rides through to result.params.kerf_mm.
    minimize_sheets: bool = True
    max_new_sheets: int = 40
    search_budget_s: float = 0.0
    min_hole_side: float = 30.0
    kerf: float = 0.0
    qty_regex: Optional[str] = None
    job_name: str = "nest"
    lang: str = "es"
    out_prefix: str = ""


@dataclass
class Job:
    job_id: str
    client_id: str
    out_prefix: str
    job_name: str
    params: SheetJobParams
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    sheets_done: int = 0
    sheets_total_estimate: int = 0
    parts_placed: int = 0
    parts_total: int = 0
    # Which pass of the sheet-count search is reporting. A second attempt
    # restarts `sheets_done` at zero, so a client drawing a progress bar has to
    # be told that is a NEW pass and not the solve going backwards.
    attempt: int = 1
    new_sheet_ceiling: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # Job-level remarks that are NOT failures: no density resolved, no
    # thickness, a retazo nothing fitted. They get their own channel because
    # ``warnings`` means "an artifact could not be produced" and a client that
    # shows warnings as problems must not show these that way.
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed_s(self) -> float:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(end - start, 0.0)


def to_body(job: Job) -> Dict[str, Any]:
    """The wire shape of a job — identical whether it came from memory or R2."""
    body: Dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "mode": "sheet",
        "job_name": job.job_name,
        "out_prefix": job.out_prefix,
        "created_at": round(job.created_at, 3),
        "updated_at": round(job.updated_at, 3),
        "progress": {
            "sheets_done": job.sheets_done,
            # ESTIMATE: the multi-sheet loop learns the total as it goes.
            "sheets_total_estimate": max(job.sheets_total_estimate, job.sheets_done),
            "parts_placed": job.parts_placed,
            "parts_total": job.parts_total,
            "elapsed_s": round(job.elapsed_s, 2),
            "attempt": job.attempt,
            "new_sheet_ceiling": job.new_sheet_ceiling,
        },
        "errors": list(job.errors),
        "warnings": list(job.warnings),
        "notes": list(job.notes),
    }
    if job.result is not None:
        body["result"] = job.result
        body["artifacts"] = list(job.artifacts)
    if job.error:
        body["error"] = job.error
    return body


def _from_body(body: Dict[str, Any]) -> Job:
    """Rebuild a (read-only) Job from a persisted ``_job.json``."""
    prog = body.get("progress") or {}
    job = Job(
        job_id=str(body.get("job_id", "")),
        client_id=str(body.get("client_id", "")),
        out_prefix=str(body.get("out_prefix", "")),
        job_name=str(body.get("job_name", "nest")),
        params=SheetJobParams(files=[], width=0.0, height=0.0),
        status=str(body.get("status", LOST)),
        created_at=float(body.get("created_at", 0.0) or 0.0),
        updated_at=float(body.get("updated_at", 0.0) or 0.0),
        sheets_done=int(prog.get("sheets_done", 0) or 0),
        sheets_total_estimate=int(prog.get("sheets_total_estimate", 0) or 0),
        parts_placed=int(prog.get("parts_placed", 0) or 0),
        parts_total=int(prog.get("parts_total", 0) or 0),
        attempt=int(prog.get("attempt", 1) or 1),
        new_sheet_ceiling=prog.get("new_sheet_ceiling"),
        result=body.get("result"),
        artifacts=list(body.get("artifacts") or []),
        errors=list(body.get("errors") or []),
        warnings=list(body.get("warnings") or []),
        notes=list(body.get("notes") or []),
        error=body.get("error"),
    )
    # elapsed_s is frozen at what was persisted, not recomputed from a live clock.
    job.started_at = job.created_at
    job.finished_at = job.created_at + float(prog.get("elapsed_s", 0.0) or 0.0)
    return job


# --------------------------------------------------------------------------- #
# Registry + executor (module-level: one per process, by design)
# --------------------------------------------------------------------------- #

_REGISTRY: "OrderedDict[str, Job]" = OrderedDict()
_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SOLVES,
                               thread_name_prefix="nest-job")


def _register(job: Job) -> None:
    with _LOCK:
        _REGISTRY[job.job_id] = job
        while len(_REGISTRY) > MAX_REGISTRY:
            for jid, existing in list(_REGISTRY.items()):
                if existing.status in TERMINAL:
                    del _REGISTRY[jid]
                    break
            else:  # pragma: no cover - only if every entry is still in flight
                break


def lookup(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _REGISTRY.get(job_id)


def reset_registry() -> None:
    """Drop every in-memory job. Used by tests to simulate a container restart."""
    with _LOCK:
        _REGISTRY.clear()


# --------------------------------------------------------------------------- #
# Durable record in R2 (the only thing that survives a restart)
# --------------------------------------------------------------------------- #

def job_key(out_prefix: str) -> str:
    return f"{out_prefix.rstrip('/')}/{JOB_FILE}"


def _persist(job: Job) -> None:
    """Best effort: a durability failure must never abort a solve."""
    body = to_body(job)
    body["client_id"] = job.client_id
    try:
        r2.put_bytes(job_key(job.out_prefix),
                     json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     "application/json")
    except Exception as e:
        note = f"job status not persisted to {job_key(job.out_prefix)}: {e}"
        if note not in job.warnings:
            job.warnings.append(note)


def load_persisted(out_prefix: str) -> Optional[Dict[str, Any]]:
    """Read ``<out_prefix>/_job.json``; None if it isn't there / isn't readable."""
    try:
        raw = r2.get_bytes(job_key(out_prefix))
    except Exception:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def restore(job_id: str, out_prefix: str, client_id: str) -> Optional[Job]:
    """A FINISHED job rebuilt from R2, or None if there's nothing trustworthy.

    A persisted record that is still ``queued``/``running`` means the worker
    died with it: that is a lost run, not a resumable one.
    """
    body = load_persisted(out_prefix)
    if not body:
        return None
    if body.get("job_id") != job_id:
        return None
    if body.get("client_id") and body.get("client_id") != client_id:
        return None
    if body.get("status") not in TERMINAL:
        return None
    return _from_body(body)


def lost_body(job_id: str) -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "status": LOST,
        "mode": "sheet",
        "progress": {"sheets_done": 0, "sheets_total_estimate": 0,
                     "parts_placed": 0, "parts_total": 0, "elapsed_s": 0.0,
                     "attempt": 0, "new_sheet_ceiling": None},
        "errors": [],
        "warnings": [],
        "error": LOST_MESSAGE,
    }


# --------------------------------------------------------------------------- #
# Submit / run / cancel
# --------------------------------------------------------------------------- #

def submit(client_id: str, params: SheetJobParams) -> Job:
    """Queue a sheet solve. Returns immediately with the job in ``queued``."""
    job = Job(
        job_id=uuid.uuid4().hex,
        client_id=client_id,
        out_prefix=params.out_prefix,
        job_name=params.job_name,
        params=params,
    )
    _register(job)
    _persist(job)
    _EXECUTOR.submit(_run, job)
    return job


def _touch(job: Job, status: Optional[str] = None) -> None:
    if status:
        job.status = status
    job.updated_at = time.time()


def _run(job: Job) -> None:
    """Worker body. Never raises — every outcome becomes a job status."""
    # Worker threads carry their own contextvar context; v1 always uses the
    # service's own env bucket pair (never a caller-supplied one).
    r2.caller_buckets.set(None)

    if job.cancel.is_set():  # cancelled while still queued
        job.finished_at = time.time()
        _touch(job, CANCELLED)
        _persist(job)
        return

    job.started_at = time.time()
    _touch(job, RUNNING)
    _persist(job)

    def on_progress(p: Any) -> None:
        job.sheets_done = p.sheets_done
        job.sheets_total_estimate = p.sheets_total_estimate
        job.parts_placed = p.parts_placed
        job.parts_total = p.parts_total
        # Additive, and inert when the search is off (attempt 1, no ceiling).
        job.attempt = getattr(p, "attempt", 1)
        job.new_sheet_ceiling = getattr(p, "new_sheet_ceiling", None)
        _touch(job)
        _persist(job)

    p = job.params
    try:
        native = engine.nest_sheet(
            p.files, width=p.width, height=p.height, material=p.material,
            thickness=p.thickness, margin=p.margin, gap=p.gap, rotate=p.rotate,
            time_per_sheet=p.time_per_sheet, seed=p.seed,
            extra_sheets=p.extra_sheets, nest_in_holes=p.nest_in_holes,
            min_remnant=p.min_remnant, density=p.density,
            minimize_sheets=p.minimize_sheets, max_new_sheets=p.max_new_sheets,
            search_budget_s=p.search_budget_s, min_hole_side=p.min_hole_side,
            kerf=p.kerf,
            qty_regex=p.qty_regex,
            job_name=p.job_name, lang=p.lang, out_prefix=p.out_prefix,
            include_contours=True,
            progress=on_progress,
            should_cancel=job.cancel.is_set,
        )
    except BaseException as e:  # noqa: BLE001 - a job never crashes the process
        job.finished_at = time.time()
        if _is_cancellation(e):
            _touch(job, CANCELLED)
        else:
            job.error = f"{type(e).__name__}: {e}"
            _touch(job, ERROR)
        _persist(job)
        return

    job.result = native.get("result")
    job.artifacts = list(native.get("artifacts") or [])
    job.errors = list(native.get("errors") or [])
    job.warnings += [w for w in (native.get("warnings") or []) if w not in job.warnings]
    job.notes += [n for n in (native.get("notes") or []) if n not in job.notes]
    totals = (job.result or {}).get("totals") or {}
    job.sheets_done = int(totals.get("sheets", job.sheets_done) or 0)
    job.sheets_total_estimate = job.sheets_done
    job.new_sheet_ceiling = ((totals.get("search") or {}).get("ceiling_used"))
    job.parts_placed = int(totals.get("parts_placed", job.parts_placed) or 0)
    job.parts_total = max(job.parts_total, job.parts_placed)
    job.finished_at = time.time()
    _touch(job, DONE)
    _persist(job)


def _is_cancellation(exc: BaseException) -> bool:
    """The packer's cancel signal (matched by type, or by name for a stand-in
    raised by a test double / a future engine that re-defines it)."""
    return isinstance(exc, NestCancelled) or type(exc).__name__ == "NestCancelled"


def cancel(job: Job) -> Dict[str, Any]:
    """Flag a job for cancellation. A queued job stops before it starts; a running
    one stops after the sheet in flight finishes its time budget."""
    was_running = job.status == RUNNING
    job.cancel.set()
    if job.status == QUEUED:
        job.finished_at = time.time()
        _touch(job, CANCELLED)
        _persist(job)
    return {
        "job_id": job.job_id,
        "status": CANCELLED,
        # Honest about WHEN: the sheet in flight still burns its time budget.
        "pending": was_running,
        "detail": ("cancel requested; the sheet being solved finishes its "
                   "time_per_sheet budget first") if was_running else
                  "cancelled before the solve started",
    }
