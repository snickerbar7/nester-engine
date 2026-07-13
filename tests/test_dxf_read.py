"""Tests for the DXF reader: layer filtering, stitching, hole assignment.

Sample DXFs are built in-memory with ezdxf into a tmp path — no fixtures on disk.
"""

import ezdxf
import pytest

from nester.sheet.dxf_read import read_parts, DxfReadError


def _save(doc, tmp_path, name="part.dxf"):
    p = tmp_path / name
    doc.saveas(p)
    return str(p)


def test_stitches_open_segments_and_drops_bend(tmp_path):
    """A square drawn as 4 separate LINEs on OUTER_PROFILES + a hole + a bend line."""
    doc = ezdxf.new(setup=True)
    doc.units = 4  # mm
    msp = doc.modelspace()
    W = H = 100.0
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        msp.add_line(a, b, dxfattribs={"layer": "OUTER_PROFILES"})
    msp.add_circle((50, 50), 10, dxfattribs={"layer": "INTERIOR_PROFILES"})
    msp.add_line((0, 50), (100, 50), dxfattribs={"layer": "BEND"})  # must be ignored

    parts = read_parts(_save(doc, tmp_path))
    assert len(parts) == 1
    p = parts[0]
    assert p.size[0] == pytest.approx(W, abs=1e-6)
    assert p.size[1] == pytest.approx(H, abs=1e-6)
    assert len(p.holes) == 1  # the circle; the bend line contributed nothing


def test_closed_lwpolyline_outer(tmp_path):
    doc = ezdxf.new(setup=True)
    doc.units = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (60, 0), (60, 30), (0, 30)], close=True,
                       dxfattribs={"layer": "OUTER_PROFILES"})
    parts = read_parts(_save(doc, tmp_path))
    assert len(parts) == 1
    assert parts[0].size == pytest.approx((60, 30))
    assert parts[0].holes == ()


def test_unit_scaling_inches_to_mm(tmp_path):
    doc = ezdxf.new(setup=True)
    doc.units = 1  # inches
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True,
                       dxfattribs={"layer": "OUTER_PROFILES"})
    parts = read_parts(_save(doc, tmp_path))
    # 1 inch -> 25.4 mm
    assert parts[0].size[0] == pytest.approx(25.4, abs=1e-6)


def test_qty_passthrough(tmp_path):
    doc = ezdxf.new(setup=True)
    doc.units = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True,
                       dxfattribs={"layer": "OUTER_PROFILES"})
    parts = read_parts(_save(doc, tmp_path), qty=7)
    assert parts[0].qty == 7


def test_no_contours_raises(tmp_path):
    doc = ezdxf.new(setup=True)
    doc.units = 4
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "BEND"})
    with pytest.raises(DxfReadError):
        read_parts(_save(doc, tmp_path))
