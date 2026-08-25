"""JUEGOS (sets): a per-file multiplier on the filename-parsed quantity.

The real failure this fixes: quantity can only be read from the filename, so a
shop that uploads ``PIEZA_2pz.dxf`` and needs 100 of them has no way to say so.
``sets`` is that missing lever — effective qty = qty_from_name x sets, applied
BEFORE nesting so the raw material scales with it.

What's under test: the multiplication itself (both modes), the seams it travels
through (tube ``_load_parts``, sheet ``read_parts(qty=)``, the CLIs' ``--sets``,
the /v1 FileRef, the jobs API), the extract fields a UI needs to show its
arithmetic, and — most important — that none of it reaches frozen Harriet.
"""

import json
import sys
from pathlib import Path

import ezdxf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_sample_iges import build  # noqa: E402

from nester.sheet import cli as sheet_cli  # noqa: E402
from nester.sheet.model import FlatPart, NestResult, SheetSpec  # noqa: E402
from nester.tube import cli as tube_cli  # noqa: E402
from nester.tube.profile import (  # noqa: E402
    MAX_SETS,
    parse_sets_args,
    resolve_qty,
    sets_for_path,
)

TUBE_NAME = "Base_2x2_C18_302_2pz.igs"      # 2 pieces per file, profile 2x2_c18
TUBE_SINGLE = "Base_2x2_C18_302.igs"        # no qty token -> 1 piece


# --------------------------------------------------------------------------- #
# The multiplier itself
# --------------------------------------------------------------------------- #

def test_resolve_qty_multiplies_filename_qty_by_sets():
    assert resolve_qty(TUBE_NAME, sets={TUBE_NAME: 50}) == (2, 50, 100)


def test_resolve_qty_without_sets_is_todays_behaviour():
    assert resolve_qty(TUBE_NAME) == (2, 1, 2)
    assert resolve_qty(TUBE_SINGLE) == (1, 1, 1)


def test_absent_filename_qty_defaults_to_one_then_multiplies():
    assert resolve_qty(TUBE_SINGLE, sets={TUBE_SINGLE: 7}) == (1, 7, 7)


def test_no_qty_mode_pins_filename_qty_to_one_but_keeps_sets():
    # qty_regex=None is --no-qty: the name is ignored, sets was still deliberate.
    assert resolve_qty(TUBE_NAME, None, {TUBE_NAME: 3}) == (1, 3, 3)


def test_sets_are_keyed_by_full_path_or_basename():
    full = "/tmp/jobs/x/" + TUBE_NAME
    assert sets_for_path(full, {full: 4}) == 4
    assert sets_for_path(full, {TUBE_NAME: 4}) == 4
    assert sets_for_path(full, {"other.igs": 4}) == 1
    assert sets_for_path(full, None) == 1


@pytest.mark.parametrize("bad", ["nosign", "=5", "a.igs=x", "a.igs=0", "a.igs=-2",
                                 f"a.igs={MAX_SETS + 1}"])
def test_parse_sets_args_rejects_malformed_or_out_of_range(bad):
    with pytest.raises(ValueError):
        parse_sets_args([bad])


def test_parse_sets_args_accepts_the_repeatable_form():
    assert parse_sets_args([f"{TUBE_NAME}=50", f"{TUBE_SINGLE}=2"]) == {
        TUBE_NAME: 50, TUBE_SINGLE: 2}
    assert parse_sets_args([f"a.igs={MAX_SETS}"]) == {"a.igs": MAX_SETS}
    assert parse_sets_args([]) == {}


# --------------------------------------------------------------------------- #
# Tube (1D)
# --------------------------------------------------------------------------- #

def _igs(tmp_path, name, length=302.0):
    p = tmp_path / name
    p.write_text(build(length))
    return str(p)


def test_tube_sets_multiply_a_single_copy_file(tmp_path):
    path = _igs(tmp_path, TUBE_SINGLE)
    parts, errors, _cross = tube_cli._load_parts(
        [path], tube_cli.DEFAULT_PROFILE_REGEX, tube_cli.DEFAULT_QTY_REGEX,
        {TUBE_SINGLE: 3})
    assert not errors
    assert len(parts) == 3
    assert [p.name for p in parts] == [f"{TUBE_SINGLE} #{i}/3" for i in (1, 2, 3)]


def test_tube_sets_multiply_a_multi_copy_filename(tmp_path):
    path = _igs(tmp_path, TUBE_NAME)
    parts, _e, _c = tube_cli._load_parts(
        [path], tube_cli.DEFAULT_PROFILE_REGEX, tube_cli.DEFAULT_QTY_REGEX,
        {TUBE_NAME: 3})
    assert len(parts) == 6          # 2 from the name x 3 juegos
    assert parts[0].name.endswith("#1/6")
    assert all(p.profile == "2x2_c18" for p in parts)


def test_tube_without_sets_is_unchanged(tmp_path):
    path = _igs(tmp_path, TUBE_NAME)
    a, _e, _c = tube_cli._load_parts(
        [path], tube_cli.DEFAULT_PROFILE_REGEX, tube_cli.DEFAULT_QTY_REGEX)
    b, _e, _c = tube_cli._load_parts(
        [path], tube_cli.DEFAULT_PROFILE_REGEX, tube_cli.DEFAULT_QTY_REGEX, {})
    assert [p.name for p in a] == [p.name for p in b] == [
        f"{TUBE_NAME} #1/2", f"{TUBE_NAME} #2/2"]


def test_tube_sets_only_touch_the_named_file(tmp_path):
    a = _igs(tmp_path, TUBE_NAME)
    b = _igs(tmp_path, "Other_3x3_C18_500.igs", length=500.0)
    parts, _e, _c = tube_cli._load_parts(
        [a, b], tube_cli.DEFAULT_PROFILE_REGEX, tube_cli.DEFAULT_QTY_REGEX,
        {TUBE_NAME: 4})
    assert sum(1 for p in parts if p.profile == "2x2_c18") == 8
    assert sum(1 for p in parts if p.profile == "3x3_c18") == 1


def _cli_json(capsys, argv):
    assert tube_cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_tube_cli_sets_flag_scales_the_bars_to_buy(tmp_path, capsys):
    # 2000mm pieces on a 6000mm bar: 3 per bar. 2 in the name x 6 juegos = 12.
    path = _igs(tmp_path, "Base_2x2_C18_2000_2pz.igs", length=2000.0)
    base = [path, "--stock-length", "6000", "--json"]

    plain = _cli_json(capsys, base)["profiles"][0]
    assert plain["bars"] == 1

    scaled = _cli_json(capsys, base + ["--sets", "Base_2x2_C18_2000_2pz.igs=6"])["profiles"][0]
    assert sum(len(b["cuts"]) for b in scaled["layout"]) == 12
    assert scaled["bars"] == 4          # raw material scales with the juegos


def test_tube_cli_sets_for_an_unknown_file_is_a_warning_not_an_error(tmp_path, capsys):
    path = _igs(tmp_path, TUBE_NAME)
    assert tube_cli.main([path, "--stock-length", "6000", "--json",
                          "--sets", "not_in_job.igs=5"]) == 0
    assert "not_in_job.igs" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["nosign", "a.igs=0", f"a.igs={MAX_SETS + 1}"])
def test_tube_cli_rejects_a_bad_sets_value(tmp_path, bad):
    path = _igs(tmp_path, TUBE_NAME)
    with pytest.raises(SystemExit):
        tube_cli.main([path, "--stock-length", "6000", "--json", "--sets", bad])


# --------------------------------------------------------------------------- #
# Sheet (2D)
# --------------------------------------------------------------------------- #

def _dxf(tmp_path, name, rects=((0, 0, 60, 30),)):
    doc = ezdxf.new(setup=True)
    doc.units = 4  # mm
    msp = doc.modelspace()
    for (x, y, w, h) in rects:
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                           close=True, dxfattribs={"layer": "OUTER_PROFILES"})
    p = tmp_path / name
    doc.saveas(p)
    return str(p)


def test_sheet_sets_multiply_every_part_of_a_multi_part_dxf(tmp_path):
    # One DXF, two distinct outlines, "_2pz" in the name, x 3 juegos.
    path = _dxf(tmp_path, "panel_2pz.dxf",
                rects=((0, 0, 60, 30), (200, 0, 40, 40)))
    parts, errors, _w = sheet_cli._load_parts(
        [path], sheet_cli.DEFAULT_QTY_REGEX, {"panel_2pz.dxf": 3})
    assert not errors
    assert len(parts) == 2                      # two distinct parts, not copies
    assert [p.qty for p in parts] == [6, 6]     # each multiplied: 2 x 3


def test_sheet_without_sets_is_unchanged(tmp_path):
    path = _dxf(tmp_path, "panel_2pz.dxf")
    parts, _e, _w = sheet_cli._load_parts([path], sheet_cli.DEFAULT_QTY_REGEX)
    assert [p.qty for p in parts] == [2]


def test_sheet_cli_sets_flag(tmp_path, capsys):
    path = _dxf(tmp_path, "panel_2pz.dxf")
    rc = sheet_cli.main([path, "--sheet", "1000x1000", "--time", "1", "--json",
                         "--sets", "panel_2pz.dxf=4"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["totals"]["parts_placed"] == 8   # 2 x 4 juegos


def test_sheet_cli_sets_for_an_unknown_file_is_a_warning(tmp_path, capsys):
    path = _dxf(tmp_path, "panel_2pz.dxf")
    assert sheet_cli.main([path, "--sheet", "1000x1000", "--time", "1", "--json",
                           "--sets", "ghost.dxf=2"]) == 0
    assert "ghost.dxf" in capsys.readouterr().err


def test_unique_part_contours_still_dedupe_at_a_high_count():
    """result.parts[] is one entry per UNIQUE part however many copies nest."""
    from nester.sheet.pack import nest
    from service.core.engine import sheet_part_index

    part = FlatPart("panel_2pz.dxf", ((0, 0), (80, 0), (80, 40), (0, 40)), qty=12)
    spec = SheetSpec(width=400, height=300, margin=5, part_gap=2)
    result = nest([part], spec, rotation="ortho", time_per_sheet=1, seed=0)

    index, _origins = sheet_part_index(result)
    assert len(index) == 1                       # sent once, not 12 times
    assert index[0]["name"] == "panel_2pz.dxf"
    assert index[0]["qty_placed"] == 12          # ... but demand is the full count
    assert sum(s.part_count for s in result.sheets) == 12


# --------------------------------------------------------------------------- #
# Service layer: engine seams
# --------------------------------------------------------------------------- #

@pytest.fixture
def r2_files(monkeypatch):
    """Serve InFile keys out of a dict instead of R2."""
    from service.core import engine

    store: dict[str, bytes] = {}
    monkeypatch.setattr(engine.r2, "get_bytes", lambda key: store[key])
    return store


def _infile(store, filename, data, sets=1):
    from service.core.engine import InFile

    key = f"web/u1/{filename}"
    store[key] = data.encode("latin-1") if isinstance(data, str) else data
    return InFile(key=key, filename=filename, sets=sets)


def test_extract_tube_reports_qty_from_name_sets_and_effective_qty(r2_files):
    from service.core import engine

    f = _infile(r2_files, TUBE_NAME, build(302.0), sets=50)
    body = engine.extract_tube([f], engine.DEFAULT_PROFILE_REGEX, engine.DEFAULT_QTY_REGEX)
    part = body["parts"][0]
    assert part["qty_from_name"] == 2
    assert part["sets"] == 50
    assert part["qty"] == 100          # EFFECTIVE — existing consumers keep working
    assert part["profile"] == "2x2_c18"


def test_extract_tube_without_sets_reports_sets_one(r2_files):
    from service.core import engine

    f = _infile(r2_files, TUBE_NAME, build(302.0))
    part = engine.extract_tube([f], engine.DEFAULT_PROFILE_REGEX,
                               engine.DEFAULT_QTY_REGEX)["parts"][0]
    assert (part["qty_from_name"], part["sets"], part["qty"]) == (2, 1, 2)


def test_extract_sheet_reports_the_same_three_fields_per_part(tmp_path, r2_files):
    from service.core import engine

    path = _dxf(tmp_path, "panel_2pz.dxf", rects=((0, 0, 60, 30), (200, 0, 40, 40)))
    f = _infile(r2_files, "panel_2pz.dxf", Path(path).read_bytes(), sets=25)
    body = engine.extract_sheet([f], engine.DEFAULT_QTY_REGEX)
    assert len(body["parts"]) == 2
    for p in body["parts"]:
        assert (p["qty_from_name"], p["sets"], p["qty"]) == (2, 25, 50)


def test_nest_tube_buys_bars_for_the_multiplied_demand(r2_files):
    from service.core import engine

    def bars(sets):
        f = _infile(r2_files, "Base_2x2_C18_2000_2pz.igs", build(2000.0), sets=sets)
        body = engine.nest_tube([f], stock_length=6000.0)
        return body["result"]["profiles"][0]

    one = bars(1)
    assert one["bars_needed"] == 1 and sum(len(b["pieces_mm"]) for b in one["bars"]) == 2

    six = bars(6)
    assert sum(len(b["pieces_mm"]) for b in six["bars"]) == 12
    assert six["bars_needed"] == 4


def test_nest_sheet_hands_the_packer_the_multiplied_demand(tmp_path, r2_files, monkeypatch):
    from service.core import engine

    seen = {}

    def fake_nest(parts, spec, **kw):
        seen["qty"] = [p.qty for p in parts]
        return NestResult(spec=spec)

    monkeypatch.setattr(engine, "_sheet_nest", fake_nest)
    path = _dxf(tmp_path, "panel_2pz.dxf")
    f = _infile(r2_files, "panel_2pz.dxf", Path(path).read_bytes(), sets=30)
    engine.nest_sheet([f], width=2440, height=1220)
    assert seen["qty"] == [60]          # 2 x 30, before a single sheet is solved


# --------------------------------------------------------------------------- #
# Service layer: HTTP contract
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from service.app import app  # noqa: E402
from service.core import engine as svc_engine  # noqa: E402
from service.v1 import jobs  # noqa: E402

KEYS = "harriet:tok-h:records/,web:tok-w:web/"
WEB = {"Authorization": "Bearer tok-w"}
HARRIET = {"Authorization": "Bearer tok-h"}

V1_FILES = [{"key": "web/u1/a.igs", "filename": TUBE_NAME, "sets": 50}]
DXF_FILES = [{"key": "web/u1/panel_2pz.dxf", "filename": "panel_2pz.dxf", "sets": 25}]

JOB_BODY = {
    "files": DXF_FILES, "mode": "sheet", "out_prefix": "web/u1/out",
    "sheet_width_mm": 2440, "sheet_height_mm": 1220, "rotate": "grain",
    "time_per_sheet_s": 1, "job_name": "juegos",
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("NESTER_API_KEYS", KEYS)
    jobs.reset_registry()
    return TestClient(app)


def _capture(monkeypatch, name, native):
    """Replace an engine entry point and record the InFiles it was handed."""
    seen: list = []

    def fake(files, *a, **k):
        seen.extend(files)
        return dict(native)

    monkeypatch.setattr(svc_engine, name, fake)
    return seen


def test_v1_extract_passes_sets_through_to_the_engine(api, monkeypatch):
    seen = _capture(monkeypatch, "extract_tube",
                    {"mode": "tube", "parts": [], "profiles": [], "errors": []})
    r = api.post("/v1/extract", json={"files": V1_FILES}, headers=WEB)
    assert r.status_code == 200
    assert [f.sets for f in seen] == [50]


def test_v1_nest_passes_sets_through_to_the_engine(api, monkeypatch):
    seen = _capture(monkeypatch, "nest_tube",
                    {"mode": "tube", "unit": "mm",
                     "result": {"bars_total": 0, "new_bars_total": 0, "profiles": []},
                     "artifacts": [], "errors": []})
    r = api.post("/v1/nest", headers=WEB,
                 json={"files": V1_FILES, "stock_length_mm": 6000})
    assert r.status_code == 200
    assert [f.sets for f in seen] == [50]


def test_sets_defaults_to_one_when_the_client_omits_it(api, monkeypatch):
    seen = _capture(monkeypatch, "extract_tube",
                    {"mode": "tube", "parts": [], "profiles": [], "errors": []})
    body = {"files": [{"key": "web/u1/a.igs", "filename": TUBE_NAME}]}
    assert api.post("/v1/extract", json=body, headers=WEB).status_code == 200
    assert [f.sets for f in seen] == [1]


@pytest.mark.parametrize("bad", [0, -1, MAX_SETS + 1])
def test_out_of_range_sets_is_422_on_every_v1_endpoint(api, bad):
    files = [{"key": "web/u1/a.igs", "filename": TUBE_NAME, "sets": bad}]
    assert api.post("/v1/extract", json={"files": files}, headers=WEB).status_code == 422
    assert api.post("/v1/nest", headers=WEB, json={
        "files": files, "stock_length_mm": 6000}).status_code == 422
    assert api.post("/v1/jobs", headers=WEB,
                    json={**JOB_BODY, "files": files}).status_code == 422


def test_sets_at_the_limits_are_accepted(api, monkeypatch):
    seen = _capture(monkeypatch, "extract_tube",
                    {"mode": "tube", "parts": [], "profiles": [], "errors": []})
    for n in (1, MAX_SETS):
        files = [{"key": "web/u1/a.igs", "filename": TUBE_NAME, "sets": n}]
        assert api.post("/v1/extract", json={"files": files}, headers=WEB).status_code == 200
    assert [f.sets for f in seen] == [1, MAX_SETS]


def test_jobs_api_carries_sets_all_the_way_to_the_solve(api, monkeypatch):
    seen: list = []
    done = __import__("threading").Event()

    def fake_nest_sheet(files, **kw):
        seen.extend(files)
        done.set()
        return {"mode": "sheet",
                "result": {"job": "juegos",
                           "totals": {"sheets": 1, "yield_pct": 50.0,
                                      "parts_placed": 50, "unplaceable": 0},
                           "sheets": []},
                "artifacts": [], "errors": [], "warnings": []}

    monkeypatch.setattr(svc_engine, "nest_sheet", fake_nest_sheet)
    monkeypatch.setattr(jobs, "_persist", lambda job: None)

    r = api.post("/v1/jobs", json=JOB_BODY, headers=WEB)
    assert r.status_code == 202
    assert done.wait(5)
    assert [(f.filename, f.sets) for f in seen] == [("panel_2pz.dxf", 25)]


# --------------------------------------------------------------------------- #
# Frozen Harriet contract: sets does not exist there
# --------------------------------------------------------------------------- #

def test_harriet_never_receives_a_sets_multiplier(api, monkeypatch):
    """Even if a caller sends `sets`, the frozen contract ignores it (sets=1)."""
    seen = _capture(monkeypatch, "extract_tube",
                    {"mode": "tube", "parts": [], "profiles": [], "errors": []})
    body = {"files": [{"key": "records/co/a.igs", "filename": TUBE_NAME, "sets": 99}]}
    assert api.post("/extract", json=body, headers=HARRIET).status_code == 200
    assert [f.sets for f in seen] == [1]


def test_harriet_extract_response_is_byte_identical(api, monkeypatch):
    """The new native fields must not leak into Harriet's part shape."""
    native = {
        "mode": "tube",
        "parts": [{"label": TUBE_NAME, "profile": "2x2_c18", "qty": 2,
                   "length_mm": 302.0, "qty_from_name": 2, "sets": 1}],
        "profiles": ["2x2_c18"],
        "errors": [],
    }
    monkeypatch.setattr(svc_engine, "extract_tube", lambda *a, **k: dict(native))
    body = api.post("/extract", headers=HARRIET, json={
        "files": [{"key": "records/co/a.igs", "filename": TUBE_NAME}]}).json()
    assert body == {
        "mode": "tube",
        "parts": [{"length": 302.0, "profile": "2x2_c18", "qty": 2, "label": TUBE_NAME}],
        "profiles": ["2x2_c18"],
        "errors": [],
    }


def test_harriet_sheet_extract_response_is_byte_identical(api, monkeypatch):
    native = {
        "mode": "sheet",
        "parts": [{"label": "panel_2pz.dxf", "qty": 2, "qty_from_name": 2, "sets": 1,
                   "width_mm": 60.0, "height_mm": 30.0, "area_mm2": 1800.0, "holes": 0}],
        "errors": [],
    }
    monkeypatch.setattr(svc_engine, "extract_sheet", lambda *a, **k: dict(native))
    body = api.post("/extract", headers=HARRIET, json={
        "mode": "sheet",
        "files": [{"key": "records/co/a.dxf", "filename": "panel_2pz.dxf"}]}).json()
    assert body == {
        "mode": "sheet",
        "parts": [{"label": "panel_2pz.dxf", "qty": 2, "width": 60.0,
                   "height": 30.0, "area": 1800.0, "holes": 0}],
        "errors": [],
    }
