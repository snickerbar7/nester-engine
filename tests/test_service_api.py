"""End-to-end route behaviour: auth, key scoping, and each contract's response.

The engine itself is stubbed — R2 and the solvers are covered elsewhere; what's
under test here is the HTTP boundary.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from service.app import app
from service.core import engine

KEYS = "harriet:tok-h:records/,web:tok-w:web/"
HARRIET = {"Authorization": "Bearer tok-h"}
WEB = {"Authorization": "Bearer tok-w"}

TUBE_FILES = [{"key": "records/co/r1/a.igs", "filename": "Base_2x2_C18_302_4pz.igs"}]
WEB_FILES = [{"key": "web/u1/a.igs", "filename": "Base_2x2_C18_302_4pz.igs"}]

NATIVE_EXTRACT = {
    "mode": "tube",
    "parts": [{"label": "Base_2x2_C18_302_4pz.igs", "profile": "2x2_c18",
               "qty": 4, "length_mm": 302.0}],
    "profiles": ["2x2_c18"],
    "errors": [],
}

NATIVE_NEST = {
    "mode": "tube", "unit": "mm",
    "result": {
        "bars_total": 1,
        "new_bars_total": 1,
        "profiles": [{
            "profile": "2x2_c18", "bars_needed": 1, "new_bars_needed": 1,
            "remnants_used": [], "stock_length_mm": 6000.0,
            "usable_length_mm": 5700.0, "total_part_length_mm": 1208.0,
            "total_drop_mm": 4480.0, "yield_pct": 21.19,
            "bars": [{"bar_index": 1, "stock_length_mm": 6000.0, "source": "nuevo",
                      "pieces_mm": [302.0] * 4, "drop_mm": 4480.0}],
            "unplaceable": [],
        }],
    },
    "artifacts": [{"key": "records/co/r1/out/plan.pdf", "filename": "plan.pdf",
                   "content_type": "application/pdf", "size": 2048}],
    "errors": [],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("NESTER_API_KEYS", KEYS)
    monkeypatch.setattr(engine, "extract_tube", lambda *a, **k: dict(NATIVE_EXTRACT))
    monkeypatch.setattr(engine, "nest_tube", lambda *a, **k: dict(NATIVE_NEST))
    return TestClient(app)


# --- health ---------------------------------------------------------------- #

def test_health_needs_no_token(client):
    body = client.get("/health").json()
    assert body["ok"] is True and body["service"] == "nester" and "r2" in body


def test_v1_health(client):
    body = client.get("/v1/health").json()
    assert body["ok"] is True and body["contract"] == "v1"


# --- auth ------------------------------------------------------------------ #

def test_missing_or_wrong_token_is_401(client):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        assert client.post("/extract", json={"files": TUBE_FILES}, headers=headers).status_code == 401
        assert client.post("/v1/extract", json={"files": TUBE_FILES}, headers=headers).status_code == 401


def test_legacy_token_still_authenticates_as_harriet(monkeypatch):
    monkeypatch.delenv("NESTER_API_KEYS", raising=False)
    monkeypatch.setenv("NESTER_SERVICE_TOKEN", "legacy-tok")
    monkeypatch.setattr(engine, "extract_tube", lambda *a, **k: dict(NATIVE_EXTRACT))
    c = TestClient(app)
    # Authenticates, and is unscoped: any key is accepted.
    r = c.post("/extract", json={"files": [{"key": "anything/a.igs", "filename": "a_2x2_c18.igs"}]},
               headers={"Authorization": "Bearer legacy-tok"})
    assert r.status_code == 200


def test_fails_closed_when_no_keys_configured(monkeypatch):
    monkeypatch.delenv("NESTER_API_KEYS", raising=False)
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)
    c = TestClient(app)
    r = c.post("/extract", json={"files": TUBE_FILES}, headers=HARRIET)
    assert r.status_code == 503


# --- key scoping ----------------------------------------------------------- #

def test_key_outside_client_prefix_is_403(client):
    r = client.post("/extract", json={"files": TUBE_FILES}, headers=WEB)  # records/ key, web client
    assert r.status_code == 403
    r = client.post("/v1/extract", json={"files": WEB_FILES}, headers=HARRIET)
    assert r.status_code == 403


def test_out_prefix_outside_client_prefix_is_403(client):
    r = client.post("/v1/nest", headers=WEB, json={
        "files": WEB_FILES, "stock_length_mm": 6000, "out_prefix": "records/co/out"})
    assert r.status_code == 403


def test_key_inside_client_prefix_is_allowed(client):
    r = client.post("/v1/extract", json={"files": WEB_FILES}, headers=WEB)
    assert r.status_code == 200


# --- Harriet contract (frozen) --------------------------------------------- #

def test_harriet_extract_response_shape(client):
    body = client.post("/extract", json={"files": TUBE_FILES}, headers=HARRIET).json()
    assert body == {"mode": "tube",
                    "parts": [{"length": 302.0, "profile": "2x2_c18", "qty": 4,
                               "label": "Base_2x2_C18_302_4pz.igs"}],
                    "profiles": ["2x2_c18"], "errors": []}


def test_harriet_nest_response_is_camelcase(client):
    body = client.post("/nest", headers=HARRIET, json={
        "files": TUBE_FILES, "stock_length": 6000, "kerf": 3, "back_trim": 300,
        "out_prefix": "records/co/r1/out"}).json()
    assert set(body) == {"mode", "unit", "result", "artifacts", "errors", "warnings"}
    assert body["unit"] == "mm"
    assert body["result"]["barsTotal"] == 1
    prof = body["result"]["profiles"][0]
    assert set(prof) == {"profile", "bars", "barsNeeded", "totalPartLength",
                         "totalDrop", "yieldPct", "usableLength", "unplaceable"}
    assert prof["barsNeeded"] == 1 and prof["usableLength"] == 5700.0
    assert prof["bars"] == [{"barIndex": 1, "pieces": [302.0] * 4, "drop": 4480.0}]
    assert body["artifacts"][0]["contentType"] == "application/pdf"


def test_harriet_nest_ignores_extra_stock_entirely(client, monkeypatch):
    """E9 is /v1-only: the frozen contract neither accepts nor forwards it."""
    seen = {}
    monkeypatch.setattr(engine, "nest_tube",
                        lambda files, **kw: (seen.update(kw), dict(NATIVE_NEST))[1])
    body = client.post("/nest", headers=HARRIET, json={
        "files": TUBE_FILES, "stock_length": 6000,
        "extra_stock": [{"profile": "2x2_c18", "length_mm": 2140, "label": "R-0001"}],
    }).json()
    assert "extra_stock" not in seen
    prof = body["result"]["profiles"][0]
    assert set(prof) == {"profile", "bars", "barsNeeded", "totalPartLength",
                         "totalDrop", "yieldPct", "usableLength", "unplaceable"}
    assert set(prof["bars"][0]) == {"barIndex", "pieces", "drop"}


def test_harriet_nest_requires_stock_length(client):
    r = client.post("/nest", json={"files": TUBE_FILES}, headers=HARRIET)
    assert r.status_code == 422


# --- /v1 contract ---------------------------------------------------------- #

def test_v1_nest_returns_snake_case_engine_vocabulary(client):
    body = client.post("/v1/nest", headers=WEB, json={
        "files": WEB_FILES, "stock_length_mm": 6000, "kerf_mm": 3, "back_trim_mm": 300,
        "out_prefix": "web/u1/out"}).json()
    assert body["result"]["bars_total"] == 1
    prof = body["result"]["profiles"][0]
    assert set(prof) == {"profile", "bars_needed", "new_bars_needed", "remnants_used",
                         "stock_length_mm", "usable_length_mm",
                         "total_part_length_mm", "total_drop_mm", "yield_pct",
                         "bars", "unplaceable"}
    assert prof["bars"] == [{"bar_index": 1, "stock_length_mm": 6000.0,
                             "source": "nuevo", "pieces_mm": [302.0] * 4,
                             "drop_mm": 4480.0}]
    assert body["artifacts"][0]["content_type"] == "application/pdf"
    assert "contentType" not in body["artifacts"][0]


def test_v1_nest_passes_mm_params_through_to_the_engine(client, monkeypatch):
    seen = {}

    def spy(files, **kw):
        seen.update(kw)
        return dict(NATIVE_NEST)

    monkeypatch.setattr(engine, "nest_tube", spy)
    client.post("/v1/nest", headers=WEB, json={
        "files": WEB_FILES, "stock_length_mm": 6000, "kerf_mm": 3,
        "front_trim_mm": 10, "back_trim_mm": 300,
        "per_profile_stock_length_mm": {"2x2_c18": 6100}})
    assert seen["stock_length"] == 6000
    assert seen["kerf"] == 3
    assert seen["front_trim"] == 10
    assert seen["back_trim"] == 300
    assert seen["per_profile"] == {"2x2_c18": 6100}


def test_v1_nest_passes_extra_stock_through_to_the_engine(client, monkeypatch):
    seen = {}

    def spy(files, **kw):
        seen.update(kw)
        return dict(NATIVE_NEST)

    monkeypatch.setattr(engine, "nest_tube", spy)
    r = client.post("/v1/nest", headers=WEB, json={
        "files": WEB_FILES, "stock_length_mm": 6000,
        "extra_stock": [{"profile": "2x2_C18", "length_mm": 2140, "label": " R-0001 "}]})
    assert r.status_code == 200
    assert seen["extra_stock"] == [
        {"profile": "2x2_C18", "length_mm": 2140.0, "label": "R-0001"}]


def test_v1_nest_extra_stock_defaults_to_empty(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, "nest_tube",
                        lambda files, **kw: (seen.update(kw), dict(NATIVE_NEST))[1])
    client.post("/v1/nest", headers=WEB,
                json={"files": WEB_FILES, "stock_length_mm": 6000})
    assert seen["extra_stock"] == []


def test_v1_nest_rejects_unusable_extra_stock(client):
    bad = [
        {"profile": "2x2_c18", "length_mm": 2140, "label": "  "},   # no label
        {"profile": "2x2_c18", "length_mm": 0, "label": "R-1"},     # no length
        {"profile": "2x2_c18", "length_mm": -5, "label": "R-1"},
        {"profile": " ", "length_mm": 2140, "label": "R-1"},        # no profile
        {"profile": "2x2_c18", "length_mm": 2140},                  # label missing
    ]
    for entry in bad:
        r = client.post("/v1/nest", headers=WEB, json={
            "files": WEB_FILES, "stock_length_mm": 6000, "extra_stock": [entry]})
        assert r.status_code == 422, entry


def test_v1_nest_requires_stock_length_mm(client):
    r = client.post("/v1/nest", json={"files": WEB_FILES}, headers=WEB)
    assert r.status_code == 422


def test_v1_sheet_nest_is_501_pointing_at_the_jobs_api(client):
    r = client.post("/v1/nest", headers=WEB, json={
        "files": [{"key": "web/u1/a.dxf", "filename": "part_2pz.dxf"}],
        "stock_length_mm": 6000})
    assert r.status_code == 501
    assert "jobs" in r.json()["detail"].lower()


def test_v1_extract_returns_native_shape(client):
    body = client.post("/v1/extract", json={"files": WEB_FILES}, headers=WEB).json()
    assert body["parts"][0]["length_mm"] == 302.0
    assert "length" not in body["parts"][0]


# --- /v1/uploads ------------------------------------------------------------ #

@pytest.fixture
def presign_client(client, monkeypatch):
    # boto3 presigning is a local computation (no network call), so real signing
    # with dummy credentials is fine and exercises the actual boto3 code path.
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIAFAKEKEYID")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "fakesecretkey")
    monkeypatch.setenv("R2_BUCKET_NAME", "pub-bucket")
    monkeypatch.delenv("R2_PRIVATE_BUCKET_NAME", raising=False)
    from service.core import r2
    r2._client.cache_clear()
    yield client
    r2._client.cache_clear()


def test_v1_uploads_requires_auth(presign_client):
    r = presign_client.post("/v1/uploads", json={
        "files": [{"key": "web/u1/part.igs", "content_type": "model/iges"}]})
    assert r.status_code == 401


def test_v1_uploads_key_outside_client_prefix_is_403(presign_client):
    r = presign_client.post("/v1/uploads", headers=WEB, json={
        "files": [{"key": "records/co/r1/part.igs", "content_type": "model/iges"}]})
    assert r.status_code == 403


def test_v1_uploads_empty_files_is_422(presign_client):
    r = presign_client.post("/v1/uploads", headers=WEB, json={"files": []})
    assert r.status_code == 422


def test_v1_uploads_happy_path(presign_client):
    r = presign_client.post("/v1/uploads", headers=WEB, json={"files": [
        {"key": "web/jobs/abc/in/part.igs", "content_type": "model/iges"},
        {"key": "web/jobs/abc/in/other.dxf", "content_type": "image/vnd.dxf"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["uploads"]) == 2
    for entry, key in zip(body["uploads"], [
            "web/jobs/abc/in/part.igs", "web/jobs/abc/in/other.dxf"]):
        assert entry["key"] == key
        assert entry["expires_in"] == 900
        assert entry["url"].startswith("https://acct123.r2.cloudflarestorage.com/pub-bucket/")
        assert key in entry["url"]


# --- /v1/downloads ---------------------------------------------------------- #

def test_v1_downloads_requires_auth(presign_client):
    r = presign_client.post("/v1/downloads", json={
        "files": [{"key": "web/jobs/abc/out/plan.pdf"}]})
    assert r.status_code == 401


def test_v1_downloads_key_outside_client_prefix_is_403(presign_client):
    r = presign_client.post("/v1/downloads", headers=WEB, json={
        "files": [{"key": "records/company/plan.pdf"}]})
    assert r.status_code == 403


def test_v1_downloads_empty_files_is_422(presign_client):
    r = presign_client.post("/v1/downloads", headers=WEB, json={"files": []})
    assert r.status_code == 422


def test_v1_downloads_happy_path(presign_client):
    r = presign_client.post("/v1/downloads", headers=WEB, json={"files": [
        {"key": "web/jobs/abc/out/plan.pdf", "filename": "Plan_de_Corte.pdf"},
        {"key": "web/jobs/abc/out/nido_S01.dxf"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["downloads"]) == 2
    first, second = body["downloads"]
    assert first["key"] == "web/jobs/abc/out/plan.pdf"
    assert first["expires_in"] == 900
    assert first["url"].startswith("https://acct123.r2.cloudflarestorage.com/pub-bucket/")
    # save-as name flows through Content-Disposition
    assert "response-content-disposition=" in first["url"].lower()
    assert "Plan_de_Corte.pdf" in first["url"].replace("%22", '"').replace("%20", " ")
    # no filename -> no disposition override
    assert "response-content-disposition" not in second["url"].lower()
