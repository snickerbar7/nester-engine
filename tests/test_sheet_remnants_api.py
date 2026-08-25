"""The 2D service surface for retazos, holes and kilos (E16 / E15 / E8).

Two layers, kept apart on purpose:

* the HTTP boundary — what `POST /v1/jobs` accepts, what it refuses with 422,
  and that every accepted option reaches the solve unchanged (the solve itself
  is monkeypatched: spyrrow is exercised in test_pack2d.py);
* `engine.nest_sheet` — that the request options become a real `SheetSpec`
  density and real `ExtraSheet`s, that an unweighable job says so instead of
  inventing a number, and that a part nested inside another part's hole still
  gets the drawing data a client needs.

The 1D twin of the rack lives in test_remnants.py; nothing here touches it.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from nester.sheet.model import (
    ExtraSheet, FlatPart, Leftover, NestResult, Placement, SheetLayout,
)

from service.app import app
from service.core import engine, r2
from service.v1 import jobs, routes

KEYS = "harriet:tok-h:records/,web:tok-w:web/"
WEB = {"Authorization": "Bearer tok-w"}
HARRIET = {"Authorization": "Bearer tok-h"}

OUT = "web/u1/out"
JOB_BODY = {
    "files": [{"key": "web/u1/in/bracket_4pz.dxf", "filename": "bracket_4pz.dxf"}],
    "mode": "sheet",
    "out_prefix": OUT,
    "sheet_width_mm": 2440,
    "sheet_height_mm": 1220,
    "material": "acero",
    "thickness_mm": 2,
    "job_name": "laminas",
}


def body(**over):
    return {**JOB_BODY, **over}


# --------------------------------------------------------------------------- #
# Fixtures — an in-memory R2 and a solver that only records how it was called
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(monkeypatch):
    data = {}
    monkeypatch.setattr(r2, "put_bytes", lambda key, b, ctype: data.__setitem__(key, b))
    monkeypatch.setattr(r2, "get_bytes", lambda key: data[key])
    return data


@pytest.fixture
def solved(monkeypatch, store):
    """Records the kwargs `engine.nest_sheet` was called with."""
    seen = {}

    def spy(files, **kw):
        seen.update(kw)
        return {"mode": "sheet", "result": {"totals": {"sheets": 1, "parts_placed": 1}},
                "artifacts": [], "errors": [], "warnings": []}

    monkeypatch.setattr(engine, "nest_sheet", spy)
    return seen


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("NESTER_API_KEYS", KEYS)
    jobs.reset_registry()
    yield TestClient(app)
    jobs.reset_registry()


def submit(client, payload):
    return client.post("/v1/jobs", headers=WEB, json=payload)


def wait_for(seen, key, timeout=5.0):
    """The solve runs in the executor; give it a moment to be called."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if key in seen:
            return seen
        time.sleep(0.02)
    raise AssertionError(f"solver never called with {key!r}")


# --------------------------------------------------------------------------- #
# POST /v1/jobs — the retazo rack reaches the solve
# --------------------------------------------------------------------------- #

def test_extra_sheets_reach_the_solve_with_labels_trimmed(client, solved):
    r = submit(client, body(extra_sheets=[
        {"width_mm": 1200, "height_mm": 600, "label": " R-0007 "},
        {"width_mm": 900.5, "height_mm": 400, "label": "R-0008"},
    ]))
    assert r.status_code == 202
    kw = wait_for(solved, "extra_sheets")
    assert kw["extra_sheets"] == [
        {"width_mm": 1200.0, "height_mm": 600.0, "label": "R-0007"},
        {"width_mm": 900.5, "height_mm": 400.0, "label": "R-0008"},
    ]


def test_the_new_sheet_options_default_to_off(client, solved):
    """A request that says nothing about them must nest exactly as before."""
    assert submit(client, body()).status_code == 202
    kw = wait_for(solved, "extra_sheets")
    assert kw["extra_sheets"] == []
    assert kw["nest_in_holes"] is False
    assert kw["min_remnant"] == 0.0
    assert kw["density"] is None


def test_holes_and_min_remnant_and_density_reach_the_solve(client, solved):
    assert submit(client, body(nest_in_holes=True, min_remnant_mm=250,
                               density_kg_m3=7930)).status_code == 202
    kw = wait_for(solved, "nest_in_holes")
    assert kw["nest_in_holes"] is True
    assert kw["min_remnant"] == 250.0
    assert kw["density"] == 7930.0


# --------------------------------------------------------------------------- #
# ... and the ones that can't be cut are refused, with the reason
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("entry", [
    {"width_mm": 1200, "height_mm": 600, "label": "  "},      # nothing to pick
    {"width_mm": 0, "height_mm": 600, "label": "R-1"},        # not a piece of sheet
    {"width_mm": 1200, "height_mm": -5, "label": "R-1"},
    {"width_mm": 1200, "height_mm": 600},                     # label missing
    {"width_mm": 1200, "label": "R-1"},                       # height missing
])
def test_unusable_extra_sheets_are_422(client, solved, entry):
    r = submit(client, body(extra_sheets=[entry]))
    assert r.status_code == 422, entry


def test_repeated_retazo_label_is_422_even_in_a_different_case(client, solved):
    r = submit(client, body(extra_sheets=[
        {"width_mm": 1200, "height_mm": 600, "label": "R-7"},
        {"width_mm": 800, "height_mm": 400, "label": "r-7"},
    ]))
    assert r.status_code == 422
    assert "R-7" in r.json()["detail"]


def test_the_rack_is_capped(client, solved):
    rack = [{"width_mm": 100 + i, "height_mm": 100, "label": f"R-{i:04d}"}
            for i in range(routes.MAX_EXTRA_SHEETS + 1)]
    r = submit(client, body(extra_sheets=rack))
    assert r.status_code == 422
    assert str(routes.MAX_EXTRA_SHEETS) in r.json()["detail"]
    # exactly at the cap is fine
    assert submit(client, body(extra_sheets=rack[:-1])).status_code == 202


def test_a_retazo_for_a_job_is_never_an_error(client, solved):
    """E16's rule, mirrored from the 1D tool: offering stock is information."""
    assert submit(client, body(extra_sheets=[
        {"width_mm": 5000, "height_mm": 5000, "label": "R-HUGE"}])).status_code == 202


@pytest.mark.parametrize("payload,fragment", [
    ({"min_remnant_mm": -1}, "min_remnant_mm"),
    ({"density_kg_m3": 0}, "density_kg_m3"),
    ({"density_kg_m3": -7850}, "density_kg_m3"),
])
def test_nonsense_numbers_are_422_with_the_reason(client, solved, payload, fragment):
    r = submit(client, body(**payload))
    assert r.status_code == 422
    assert fragment in str(r.json()["detail"])


# --------------------------------------------------------------------------- #
# GET /v1/materials — the catalog, so the client never guesses a density
# --------------------------------------------------------------------------- #

def test_materials_lists_the_density_table(client):
    body_ = client.get("/v1/materials", headers=WEB).json()
    by_key = {m["key"]: m for m in body_["materials"]}
    assert by_key["acero"]["density_kg_m3"] == 7850
    assert by_key["aluminio"]["label"] == "Aluminio"
    assert set(by_key["acero"]) == {"key", "label", "density_kg_m3"}
    assert "resolved" not in body_          # only answered when asked


def test_materials_resolves_a_free_text_name(client):
    body_ = client.get("/v1/materials", headers=WEB,
                       params={"name": "lámina inox 304"}).json()
    assert body_["query"] == "lámina inox 304"
    assert body_["resolved"] == {"key": "inoxidable", "label": "Acero inoxidable",
                                 "density_kg_m3": 8000}


def test_an_unknown_material_resolves_to_null_not_to_a_guess(client):
    body_ = client.get("/v1/materials", headers=WEB,
                       params={"name": "unobtainium"}).json()
    assert body_["resolved"] is None
    assert body_["materials"]                # the catalog still comes back


def test_materials_needs_a_client(client):
    assert client.get("/v1/materials").status_code == 401


# --------------------------------------------------------------------------- #
# Harriet's frozen contract never learns any of this
# --------------------------------------------------------------------------- #

def test_harriet_nest_ignores_the_sheet_options(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, "nest_tube",
                        lambda files, **kw: (seen.update(kw), {
                            "mode": "tube", "unit": "mm",
                            "result": {"bars_total": 0, "new_bars_total": 0, "profiles": []},
                            "artifacts": [], "errors": []})[1])
    r = client.post("/nest", headers=HARRIET, json={
        "files": [{"key": "records/co/a.igs", "filename": "40x40x2_a.igs"}],
        "stock_length": 6000,
        "extra_sheets": [{"width_mm": 1200, "height_mm": 600, "label": "R-1"}],
        "nest_in_holes": True, "density_kg_m3": 7850,
    })
    assert r.status_code == 200
    assert not {"extra_sheets", "nest_in_holes", "density"} & set(seen)


# --------------------------------------------------------------------------- #
# engine.nest_sheet — options become real engine objects
# --------------------------------------------------------------------------- #

def _square(name, size=100.0, hole=None, qty=1):
    outer = ((0, 0), (size, 0), (size, size), (0, size))
    holes = ((hole,),) if hole else ()
    return FlatPart(name=name, outer=outer, holes=holes, qty=qty)


HOLE = ((20, 20), (80, 20), (80, 80), (20, 80))


class FakeNest:
    """Stands in for `nester.sheet.pack.nest`: records the call, returns a nest
    with a retazo sheet, a part cut from another part's hole, and a leftover."""

    def __init__(self):
        self.kwargs = {}
        self.spec = None

    def __call__(self, parts, spec, **kw):
        self.kwargs = kw
        self.spec = spec
        host = _square("host.dxf", 100.0, HOLE)
        slug = _square("slug.dxf", 40.0)
        layout = SheetLayout(
            index=0, spec=spec.resized(1200, 600), source="R-0007",
            placements=[Placement(part=host, x=10, y=10),
                        Placement(part=slug, x=40, y=40, rotation=90, in_hole_of=0)],
            leftover=Leftover(x=0, y=520, width=1200, height=80),
        )
        return NestResult(spec=spec, sheets=[layout],
                          remnants_unused=[ExtraSheet(400, 400, "R-0009")])


@pytest.fixture
def fake_nest(monkeypatch, tmp_path):
    """Run nest_sheet without R2, without a DXF reader and without spyrrow."""
    src = tmp_path / "bracket_2pz.dxf"
    src.write_text("")
    monkeypatch.setattr(engine, "_materialize", lambda files, into: [str(src)])
    monkeypatch.setattr(engine, "_sheet_read_parts",
                        lambda path, qty=1: [_square("bracket_2pz.dxf", qty=qty)])
    fake = FakeNest()
    monkeypatch.setattr(engine, "_sheet_nest", fake)
    return fake


FILES = [engine.InFile(key="web/u1/a.dxf", filename="bracket_2pz.dxf")]


def run_nest(**kw):
    return engine.nest_sheet(FILES, width=2440, height=1220, **kw)


def test_extra_sheets_become_extra_sheet_objects(fake_nest):
    run_nest(extra_sheets=[{"width_mm": 1200, "height_mm": 600, "label": "R-0007"}])
    assert fake_nest.kwargs["extra_sheets"] == (ExtraSheet(1200.0, 600.0, "R-0007"),)


def test_hole_and_remnant_options_reach_the_packer(fake_nest):
    run_nest(nest_in_holes=True, min_remnant=250.0, material="acero", thickness=2)
    assert fake_nest.kwargs["nest_in_holes"] is True
    assert fake_nest.kwargs["min_remnant"] == 250.0


def test_the_report_is_told_the_job_settings_it_cannot_read_off_the_stock(
        fake_nest, monkeypatch):
    """`nest_in_holes` / `min_remnant` change what the plan must SAY, but neither
    is on the SheetSpec — they travel in meta, which is the report's other input."""
    seen = {}
    real = engine._sheet_as_dict
    monkeypatch.setattr(engine, "_sheet_as_dict",
                        lambda result, job, meta, *a: (seen.update(meta),
                                                       real(result, job, meta, *a))[1])
    run_nest(nest_in_holes=True, min_remnant=250.0, material="acero", thickness=2)
    assert seen["nest_in_holes"] is True
    assert seen["min_remnant"] == 250.0
    assert seen["rotation"] == "free"


def test_density_comes_from_the_material_name(fake_nest):
    run_nest(material="acero inoxidable 304", thickness=2)
    assert fake_nest.spec.density == 8000
    assert fake_nest.spec.can_weigh is True


def test_an_explicit_density_wins_over_the_table(fake_nest):
    run_nest(material="acero", thickness=2, density=7930)
    assert fake_nest.spec.density == 7930


def test_an_unknown_material_weighs_nothing_and_says_why(fake_nest):
    out = run_nest(material="unobtainium", thickness=2)
    assert fake_nest.spec.density == 0.0
    assert fake_nest.spec.can_weigh is False
    # `notes`, not `warnings`: nothing failed to be produced. A caller can also
    # read it structurally off params.density_kg_m3 being null.
    assert any("unobtainium" in n for n in out["notes"])
    assert out["warnings"] == []


def test_a_material_with_no_thickness_says_why_too(fake_nest):
    out = run_nest(material="acero")
    assert any("espesor" in n for n in out["notes"])
    assert out["warnings"] == []


def test_a_zero_density_override_is_refused_not_treated_as_unknown(fake_nest):
    with pytest.raises(ValueError):
        run_nest(material="acero", density=0)


def test_a_retazo_left_on_the_rack_is_reported_as_a_note_not_a_warning(fake_nest):
    """An unopened retazo is information. It must not read as a failure —
    and it must be recoverable structurally, not only as prose."""
    out = run_nest(material="acero", thickness=2)
    assert any("R-0009" in n and "rack" in n for n in out["notes"])
    assert out["warnings"] == []
    labels = [r["label"] for r in out["result"]["remnants_unused"]]
    assert "R-0009" in labels


def test_every_placement_is_drawable_including_the_one_inside_a_hole(fake_nest):
    """The service composes contour data on top of the report's JSON: it must
    line up placement-for-placement, and an in-hole copy is a placement."""
    out = run_nest(material="acero", thickness=2, include_contours=True)
    placed = out["result"]["sheets"][0]["parts"]
    assert len(placed) == 2
    for entry in placed:
        assert len(entry["contour_offset_mm"]) == 2
        assert len(entry["bbox_mm"]) == 4
    # the slug sits where it was placed (rotated 90 about its own origin)
    x0, y0, x1, y1 = placed[1]["bbox_mm"]
    assert (round(x1 - x0), round(y1 - y0)) == (40, 40)
    assert {p["name"] for p in out["result"]["parts"]} == {"host.dxf", "slug.dxf"}
