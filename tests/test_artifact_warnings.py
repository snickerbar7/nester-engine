"""Tests for E4: an artifact-write failure (IGES nest-layout / per-sheet DXF)
must not be silently swallowed. It should:
  1. Not crash the job -- the cut-plan PDF + JSON still get produced.
  2. Land in the machine-readable JSON's "warnings" field.
  3. Be printed to stderr by the CLI.
"""

import json
import sys
from pathlib import Path

import ezdxf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_sample_iges import build  # noqa: E402

from nester.tube import cli as tube_cli  # noqa: E402
import nester.tube.iges_nest as tube_iges_nest  # noqa: E402

from nester.sheet import cli as sheet_cli  # noqa: E402
import nester.sheet.dxf_out as sheet_dxf_out  # noqa: E402


def _boom(*args, **kwargs):
    raise RuntimeError("disk full")


# --------------------------------------------------------------------------- #
# Tube: IGES nest-layout write failure
# --------------------------------------------------------------------------- #

def test_tube_iges_write_failure_surfaces_as_warning(tmp_path, monkeypatch, capsys):
    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))
    out_dir = tmp_path / "out"

    monkeypatch.setattr(tube_iges_nest, "write_nest_iges", _boom)

    rc = tube_cli.main([
        str(src), "--stock-length", "6000", "--out", str(out_dir), "--name", "job1",
    ])
    assert rc == 0  # job still completes

    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "disk full" in captured.err

    job_dir = out_dir / "job1"
    json_path = job_dir / "job1_corte.json"
    assert json_path.exists()  # JSON still written
    data = json.loads(json_path.read_text())
    assert data["warnings"]
    assert any("disk full" in w for w in data["warnings"])
    assert any("IGES" in w for w in data["warnings"])

    # the PDF (a different artifact) still stands
    assert (job_dir / "job1_Plan_de_Corte.pdf").exists()
    # the failed artifact is genuinely absent, not silently faked
    assert not (job_dir / "job1_nest.igs").exists()


def test_tube_iges_write_success_leaves_warnings_empty(tmp_path):
    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))
    out_dir = tmp_path / "out"

    rc = tube_cli.main([
        str(src), "--stock-length", "6000", "--out", str(out_dir), "--name", "job1",
    ])
    assert rc == 0

    job_dir = out_dir / "job1"
    data = json.loads((job_dir / "job1_corte.json").read_text())
    assert data["warnings"] == []
    assert (job_dir / "job1_nest.igs").exists()


# --------------------------------------------------------------------------- #
# Sheet: nested-DXF-per-sheet write failure
# --------------------------------------------------------------------------- #

def _make_square_dxf(path, size=60.0):
    doc = ezdxf.new(setup=True)
    doc.units = 4  # mm
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (size, 0), (size, size), (0, size)],
        close=True, dxfattribs={"layer": "OUTER_PROFILES"},
    )
    doc.saveas(path)


def test_sheet_dxf_write_failure_surfaces_as_warning(tmp_path, monkeypatch, capsys):
    src = tmp_path / "part.dxf"
    _make_square_dxf(src)
    out_dir = tmp_path / "out"

    monkeypatch.setattr(sheet_dxf_out, "write_all_sheets", _boom)

    rc = sheet_cli.main([
        str(src), "--sheet", "300x300", "--time", "1",
        "--out", str(out_dir), "--name", "job1",
    ])
    assert rc == 0  # job still completes

    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "disk full" in captured.err

    job_dir = out_dir / "job1"
    json_path = job_dir / "job1_nido.json"
    assert json_path.exists()  # JSON still written
    data = json.loads(json_path.read_text())
    assert data["warnings"]
    assert any("disk full" in w for w in data["warnings"])

    # the PDF (a different artifact) still stands
    assert (job_dir / "job1_Plan_de_Corte.pdf").exists()
    # the failed artifact is genuinely absent
    assert not list(job_dir.glob("job1_S*.dxf"))


def test_sheet_dxf_write_success_leaves_warnings_empty(tmp_path):
    src = tmp_path / "part.dxf"
    _make_square_dxf(src)
    out_dir = tmp_path / "out"

    rc = sheet_cli.main([
        str(src), "--sheet", "300x300", "--time", "1",
        "--out", str(out_dir), "--name", "job1",
    ])
    assert rc == 0

    job_dir = out_dir / "job1"
    data = json.loads((job_dir / "job1_nido.json").read_text())
    assert data["warnings"] == []
    assert list(job_dir.glob("job1_S*.dxf"))
