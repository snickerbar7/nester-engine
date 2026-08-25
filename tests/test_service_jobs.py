"""The async jobs API (E1/A5): lifecycle, progress, cancel, errors, durability.

The solve itself is a FAST fake (`engine.nest_sheet` is monkeypatched) — spyrrow
is exercised in test_pack2d.py. What's under test here is the job machinery:
statuses, the progress it reports, cancellation, the R2 record that survives a
restart, and per-client prefix scoping.
"""

import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from nester.sheet.model import NestResult, SheetSpec
from nester.sheet.pack import NestCancelled, NestProgress

from service.app import app
from service.core import engine, r2
from service.v1 import jobs

KEYS = "harriet:tok-h:records/,web:tok-w:web/"
WEB = {"Authorization": "Bearer tok-w"}
HARRIET = {"Authorization": "Bearer tok-h"}

DXF_FILES = [{"key": "web/u1/in/bracket_4pz.dxf", "filename": "bracket_4pz.dxf"}]
OUT = "web/u1/out"

JOB_BODY = {
    "files": DXF_FILES,
    "mode": "sheet",
    "out_prefix": OUT,
    "sheet_width_mm": 2440,
    "sheet_height_mm": 1220,
    "material": "acero",
    "thickness_mm": 2,
    "margin_mm": 8,
    "gap_mm": 3,
    "rotate": "grain",
    "time_per_sheet_s": 4,
    "seed": 11,
    "job_name": "laminas",
}


def native_result(sheets=2, parts=6):
    """What a real sheet solve returns (engine's native envelope)."""
    return {
        "mode": "sheet",
        "result": {
            "job": "laminas",
            "totals": {"sheets": sheets, "yield_pct": 61.2,
                       "parts_placed": parts, "unplaceable": 0},
            "sheets": [{"sheet": i + 1, "utilization_pct": 61.2, "parts": []}
                       for i in range(sheets)],
            "parts": [{"name": "bracket_4pz.dxf", "qty_placed": parts,
                       "contour": {"outer": [[0, 0], [10, 0], [10, 5]], "holes": []}}],
        },
        "artifacts": [{"key": f"{OUT}/laminas_Plan_de_Corte.pdf",
                       "filename": "laminas_Plan_de_Corte.pdf",
                       "content_type": "application/pdf", "size": 4096}],
        "errors": ["ignored.dxf: no closed contours found"],
        "warnings": ["nested DXF per sheet not written: disk full"],
    }


# --------------------------------------------------------------------------- #
# Fake solvers
# --------------------------------------------------------------------------- #

class GatedSolver:
    """Reports sheet 1, blocks until released, reports sheet 2, returns."""

    def __init__(self, sheets=2):
        self.sheets = sheets
        self.started = threading.Event()
        self.gate = threading.Event()
        self.kwargs = None

    def __call__(self, files, *, progress=None, should_cancel=None, **kw):
        self.kwargs = kw
        self.started.set()
        for i in range(1, self.sheets + 1):
            progress(NestProgress(sheets_done=i, sheets_total_estimate=self.sheets,
                                  parts_placed=i * 3, parts_total=self.sheets * 3,
                                  last_sheet_utilization_pct=61.2))
            if i == 1 and not self.gate.wait(10):
                raise AssertionError("gate never released")
        return native_result(self.sheets, self.sheets * 3)

    def release(self):
        self.gate.set()


class CancellableSolver:
    """Spins until the cancel flag flips, then signals like the real packer."""

    def __init__(self):
        self.started = threading.Event()

    def __call__(self, files, *, progress=None, should_cancel=None, **kw):
        self.started.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            if should_cancel():
                raise NestCancelled(NestResult(spec=SheetSpec(width=100, height=100)))
            time.sleep(0.01)
        raise AssertionError("cancel flag never set")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for R2 (the durable job record lives here)."""
    data = {}

    def put(key, body, content_type):
        data[key] = body

    def get(key):
        if key not in data:
            raise FileNotFoundError(key)
        return data[key]

    monkeypatch.setattr(r2, "put_bytes", put)
    monkeypatch.setattr(r2, "get_bytes", get)
    return data


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("NESTER_API_KEYS", KEYS)
    jobs.reset_registry()
    yield TestClient(app)
    jobs.reset_registry()


@pytest.fixture
def gated(monkeypatch):
    solver = GatedSolver()
    monkeypatch.setattr(engine, "nest_sheet", solver)
    yield solver
    solver.release()  # never leave an executor thread blocked


def wait_until(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def poll(client, job_id, params=None, headers=WEB):
    return client.get(f"/v1/jobs/{job_id}", params=params or {}, headers=headers).json()


def record(store, out_prefix=OUT):
    return json.loads(store[f"{out_prefix}/_job.json"].decode("utf-8"))


# --------------------------------------------------------------------------- #
# Lifecycle: queued -> running (with progress) -> done
# --------------------------------------------------------------------------- #

def test_post_jobs_returns_202_queued(client, gated):
    r = client.post("/v1/jobs", headers=WEB, json=JOB_BODY)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["out_prefix"] == OUT
    assert body["job_record_key"] == f"{OUT}/_job.json"


def test_job_goes_running_with_progress_then_done(client, gated):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]

    running = wait_until(lambda: (lambda b: b if b["status"] == "running"
                                  and b["progress"]["sheets_done"] == 1 else None)(
        poll(client, job_id)))
    prog = running["progress"]
    assert prog["sheets_total_estimate"] == 2
    assert prog["parts_placed"] == 3
    assert prog["parts_total"] == 6
    assert prog["elapsed_s"] >= 0
    assert "result" not in running

    gated.release()
    done = wait_until(lambda: (lambda b: b if b["status"] == "done" else None)(
        poll(client, job_id)))
    assert done["progress"]["sheets_done"] == 2
    assert done["progress"]["sheets_total_estimate"] == 2
    assert done["progress"]["parts_placed"] == 6
    assert done["result"]["totals"]["sheets"] == 2
    assert done["artifacts"][0]["filename"] == "laminas_Plan_de_Corte.pdf"
    assert done["errors"] == ["ignored.dxf: no closed contours found"]
    assert "nested DXF per sheet not written: disk full" in done["warnings"]


def test_job_result_carries_part_contours(client, gated):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    gated.release()
    done = wait_until(lambda: (lambda b: b if b["status"] == "done" else None)(
        poll(client, job_id)))
    part = done["result"]["parts"][0]
    assert part["name"] == "bracket_4pz.dxf"
    assert part["contour"]["outer"][0] == [0, 0]


def test_sheet_params_reach_the_engine_with_contours_on(client, gated):
    client.post("/v1/jobs", headers=WEB, json=JOB_BODY)
    gated.started.wait(5)
    kw = gated.kwargs
    assert kw["width"] == 2440 and kw["height"] == 1220
    assert kw["material"] == "acero" and kw["thickness"] == 2
    assert kw["margin"] == 8 and kw["gap"] == 3
    assert kw["rotate"] == "grain" and kw["time_per_sheet"] == 4 and kw["seed"] == 11
    assert kw["out_prefix"] == OUT and kw["job_name"] == "laminas"
    assert kw["include_contours"] is True
    gated.release()


def test_two_solves_run_at_once_and_the_third_stays_queued(client, monkeypatch):
    solver = GatedSolver()
    monkeypatch.setattr(engine, "nest_sheet", solver)
    try:
        ids = [client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
               for _ in range(jobs.MAX_CONCURRENT_SOLVES + 1)]
        wait_until(lambda: sum(poll(client, i)["status"] == "running" for i in ids)
                   == jobs.MAX_CONCURRENT_SOLVES)
        statuses = [poll(client, i)["status"] for i in ids]
        assert statuses.count("running") == jobs.MAX_CONCURRENT_SOLVES
        assert statuses.count("queued") == 1  # honestly queued, not silently dropped
    finally:
        solver.release()


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

def test_cancel_stops_a_running_job(client, monkeypatch):
    solver = CancellableSolver()
    monkeypatch.setattr(engine, "nest_sheet", solver)
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    assert solver.started.wait(5)

    r = client.delete(f"/v1/jobs/{job_id}", headers=WEB)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["pending"] is True  # the sheet in flight finishes its budget

    final = wait_until(lambda: (lambda b: b if b["status"] == "cancelled" else None)(
        poll(client, job_id)))
    assert "result" not in final


def test_cancel_before_the_solve_starts_never_runs_it(store):
    """Unit-level: a queued job cancels without ever entering the executor."""
    params = jobs.SheetJobParams(files=[], width=100, height=100, out_prefix=OUT)
    job = jobs.Job(job_id="j1", client_id="web", out_prefix=OUT,
                   job_name="n", params=params)
    assert jobs.cancel(job) == {"job_id": "j1", "status": "cancelled",
                                "pending": False,
                                "detail": "cancelled before the solve started"}
    assert job.status == "cancelled"
    assert record(store)["status"] == "cancelled"


def test_cancelling_an_unknown_job_is_404(client, gated):
    assert client.delete("/v1/jobs/nope", headers=WEB).status_code == 404


# --------------------------------------------------------------------------- #
# Error path
# --------------------------------------------------------------------------- #

def test_a_failing_solve_becomes_status_error_with_the_reason(client, monkeypatch):
    def boom(files, **kw):
        raise ValueError("no readable parts: bracket_4pz.dxf: no OUTER_PROFILES")

    monkeypatch.setattr(engine, "nest_sheet", boom)
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    body = wait_until(lambda: (lambda b: b if b["status"] == "error" else None)(
        poll(client, job_id)))
    assert "no readable parts" in body["error"]
    assert "result" not in body


# --------------------------------------------------------------------------- #
# Durability: the R2 record, and what a restart does
# --------------------------------------------------------------------------- #

def test_status_is_persisted_to_r2_under_the_out_prefix(client, gated, store):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    assert record(store)["job_id"] == job_id
    gated.release()
    wait_until(lambda: record(store)["status"] == "done")
    saved = record(store)
    assert saved["client_id"] == "web"          # ownership travels with the record
    assert saved["result"]["totals"]["sheets"] == 2
    assert saved["artifacts"][0]["size"] == 4096


def test_a_finished_job_survives_a_restart_via_out_prefix(client, gated, store):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    gated.release()
    wait_until(lambda: poll(client, job_id)["status"] == "done")

    jobs.reset_registry()  # the container restarted

    body = poll(client, job_id, {"out_prefix": OUT})
    assert body["status"] == "done"
    assert body["restored_from"] == "r2"
    assert body["result"]["totals"]["sheets"] == 2
    assert body["artifacts"][0]["filename"] == "laminas_Plan_de_Corte.pdf"


def test_a_restart_without_out_prefix_reports_lost(client, gated, store):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    gated.release()
    wait_until(lambda: poll(client, job_id)["status"] == "done")

    jobs.reset_registry()

    body = poll(client, job_id)
    assert body["status"] == "lost"
    assert "re-submit" in body["error"].lower()
    assert body["progress"]["sheets_done"] == 0


def test_a_solve_lost_mid_flight_reports_lost_not_running(client, gated, store):
    """The persisted record still says "running" — nothing is running. Say so."""
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    wait_until(lambda: record(store)["status"] == "running")

    jobs.reset_registry()  # container died mid-solve

    body = poll(client, job_id, {"out_prefix": OUT})
    assert body["status"] == "lost"
    assert "POST /v1/jobs" in body["error"]


def test_a_missing_job_record_reports_lost(client, gated, store):
    body = poll(client, "does-not-exist", {"out_prefix": OUT})
    assert body["status"] == "lost"


def test_persistence_failure_is_a_warning_not_a_dead_job(client, monkeypatch, gated):
    def refuse(*a, **kw):
        raise RuntimeError("R2 unreachable")

    monkeypatch.setattr(r2, "put_bytes", refuse)
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    gated.release()
    body = wait_until(lambda: (lambda b: b if b["status"] == "done" else None)(
        poll(client, job_id)))
    assert any("not persisted" in w for w in body["warnings"])
    assert body["result"]["totals"]["sheets"] == 2


# --------------------------------------------------------------------------- #
# Auth + prefix scoping
# --------------------------------------------------------------------------- #

def test_jobs_endpoints_require_a_token(client, gated):
    assert client.post("/v1/jobs", json=JOB_BODY).status_code == 401
    assert client.get("/v1/jobs/abc").status_code == 401
    assert client.delete("/v1/jobs/abc").status_code == 401


def test_submitting_with_a_foreign_out_prefix_is_403(client, gated):
    body = dict(JOB_BODY, out_prefix="records/co/out")
    assert client.post("/v1/jobs", headers=WEB, json=body).status_code == 403


def test_submitting_with_foreign_input_keys_is_403(client, gated):
    body = dict(JOB_BODY, files=[{"key": "records/co/a.dxf", "filename": "a.dxf"}])
    assert client.post("/v1/jobs", headers=WEB, json=body).status_code == 403


def test_polling_with_a_foreign_out_prefix_is_403(client, gated):
    r = client.get("/v1/jobs/whatever", params={"out_prefix": "records/co/out"},
                   headers=WEB)
    assert r.status_code == 403


def test_another_clients_job_is_not_visible(client, gated):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    # Harriet asks for the web client's job id: no leak, no access.
    assert poll(client, job_id, headers=HARRIET)["status"] == "lost"
    assert client.delete(f"/v1/jobs/{job_id}", headers=HARRIET).status_code == 404
    gated.release()


def test_a_restore_is_refused_for_another_clients_record(client, gated, store):
    job_id = client.post("/v1/jobs", headers=WEB, json=JOB_BODY).json()["job_id"]
    gated.release()
    wait_until(lambda: poll(client, job_id)["status"] == "done")
    jobs.reset_registry()
    # harriet can't even name web/'s prefix (403 first), and with its own prefix
    # there is no such record.
    assert client.get(f"/v1/jobs/{job_id}", params={"out_prefix": OUT},
                      headers=HARRIET).status_code == 403


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def test_tube_files_are_refused_and_pointed_at_the_sync_endpoint(client, gated):
    body = dict(JOB_BODY, mode="auto",
                files=[{"key": "web/u1/a.igs", "filename": "Base_2x2_C18_302.igs"}])
    r = client.post("/v1/jobs", headers=WEB, json=body)
    assert r.status_code == 422
    assert "/v1/nest" in r.json()["detail"]


def test_out_prefix_is_required(client, gated):
    body = {k: v for k, v in JOB_BODY.items() if k != "out_prefix"}
    assert client.post("/v1/jobs", headers=WEB, json=body).status_code == 422


def test_sheet_size_is_required(client, gated):
    body = {k: v for k, v in JOB_BODY.items() if k != "sheet_width_mm"}
    assert client.post("/v1/jobs", headers=WEB, json=body).status_code == 422


@pytest.mark.parametrize("override", [
    {"rotate": "sideways"},
    {"sheet_width_mm": 0},
    {"sheet_height_mm": -1},
    {"margin_mm": -2},
    {"gap_mm": -1},
    {"margin_mm": 700},          # eats the whole sheet
    {"time_per_sheet_s": 0},
    {"out_prefix": "   "},
    {"files": []},
])
def test_unusable_parameters_are_422(client, gated, override):
    r = client.post("/v1/jobs", headers=WEB, json=dict(JOB_BODY, **override))
    assert r.status_code == 422, override
