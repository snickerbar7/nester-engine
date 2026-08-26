"""E9 — nesting against remnant stock (retazos).

The shop's leftovers are extra bars the solver must spend BEFORE it tells
anyone to buy a new tramo. Each retazo is one physical piece: usable once,
trimmed like any other bar, and named in the plan so it can be pulled off
the rack.
"""

import json

import pytest

from nester.tube.model import ExtraStock, Part, StockSpec
from nester.tube.packing import pack_all, pack_profile


def mk(profile, lengths):
    return [Part(name=f"{profile}-{i}", profile=profile, length=L)
            for i, L in enumerate(lengths)]


def rem(*pairs):
    return tuple(ExtraStock(length=L, label=lb) for L, lb in pairs)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def test_extra_stock_defaults_empty_and_spec_stays_hashable():
    spec = StockSpec(profile="p", stock_length=6000)
    assert spec.extra_stock == ()
    assert hash(spec)  # frozen spec is still usable as a dict key / in a set


def test_extra_stock_accepts_a_list_and_stores_a_tuple():
    spec = StockSpec(profile="p", stock_length=6000,
                     extra_stock=[ExtraStock(length=2000, label="R-1")])
    assert spec.extra_stock == (ExtraStock(length=2000, label="R-1"),)


def test_extra_stock_rejects_nonsense():
    with pytest.raises(ValueError):
        ExtraStock(length=0, label="R-1")
    with pytest.raises(ValueError):
        ExtraStock(length=1000, label="  ")


# --------------------------------------------------------------------------- #
# Solver policy
# --------------------------------------------------------------------------- #

def test_remnant_is_used_before_a_new_bar():
    spec = StockSpec(profile="p", stock_length=6000, extra_stock=rem((1200, "R-0001")))
    res = pack_profile(mk("p", [1000]), spec)
    assert res.bar_count == 1
    assert res.new_bars_needed == 0          # nothing to buy
    assert res.remnants_used == ["R-0001"]
    bar = res.bars[0]
    assert bar.source == "R-0001" and bar.is_remnant
    assert bar.stock_length == 1200
    assert bar.remnant == 200


def test_smallest_fitting_remnant_is_chosen():
    """Big retazos are conserved for big parts."""
    spec = StockSpec(profile="p", stock_length=6000,
                     extra_stock=rem((5000, "R-BIG"), (1500, "R-SMALL"), (900, "R-TINY")))
    res = pack_profile(mk("p", [1000]), spec)
    assert res.remnants_used == ["R-SMALL"]  # 900 too short, 5000 wastefully long


def test_big_part_takes_the_big_remnant_and_small_parts_follow():
    spec = StockSpec(profile="p", stock_length=6000,
                     extra_stock=rem((4000, "R-BIG"), (1500, "R-SMALL")))
    res = pack_profile(mk("p", [3500, 1400]), spec)
    assert res.new_bars_needed == 0
    assert res.remnants_used == ["R-BIG", "R-SMALL"]
    assert [b.stock_length for b in res.bars] == [4000, 1500]


def test_remnant_too_short_for_every_part_is_left_alone():
    spec = StockSpec(profile="p", stock_length=6000, extra_stock=rem((300, "R-0001")))
    res = pack_profile(mk("p", [2000, 2000]), spec)
    assert res.remnants_used == []           # never opened
    assert res.new_bars_needed == 1
    assert all(b.source == "nuevo" for b in res.bars)


def test_remnant_shorter_than_the_trims_is_never_offered():
    spec = StockSpec(profile="p", stock_length=6000, front_trim=100, back_trim=300,
                     extra_stock=rem((350, "R-0001")))
    res = pack_profile(mk("p", [40]), spec)
    assert res.remnants_used == []
    assert res.new_bars_needed == 1


def test_remnant_used_only_once():
    spec = StockSpec(profile="p", stock_length=2000, extra_stock=rem((2000, "R-0001")))
    res = pack_profile(mk("p", [1900, 1900]), spec)
    assert res.remnants_used == ["R-0001"]
    assert res.new_bars_needed == 1
    assert res.bar_count == 2


def test_part_fitting_only_a_remnant_longer_than_the_tramo():
    """A 12 m leftover can hold a part no 6 m tramo ever could."""
    spec = StockSpec(profile="p", stock_length=6000, extra_stock=rem((12000, "R-LARGO")))
    res = pack_profile(mk("p", [8000]), spec)
    assert res.unplaceable == []
    assert res.remnants_used == ["R-LARGO"]
    assert res.bars[0].stock_length == 12000


def test_part_too_long_for_everything_is_unplaceable():
    spec = StockSpec(profile="p", stock_length=6000, extra_stock=rem((7000, "R-0001")))
    res = pack_profile(mk("p", [9000]), spec)
    assert [p.length for p in res.unplaceable] == [9000]
    assert res.bar_count == 0


def test_part_loses_its_only_remnant_to_a_longer_part_and_is_unplaceable():
    """Two 7 m parts, one 7.5 m retazo, 6 m tramos: the second has nowhere to
    go — it must be reported, never silently dropped."""
    spec = StockSpec(profile="p", stock_length=6000, extra_stock=rem((7500, "R-0001")))
    res = pack_profile(mk("p", [7000, 7000]), spec)
    assert res.remnants_used == ["R-0001"]
    assert len(res.unplaceable) == 1
    assert res.unplaceable[0].length == 7000
    assert sum(len(b.placements) for b in res.bars) == 1


def test_unplaceable_unchanged_without_remnants():
    spec = StockSpec(profile="p", stock_length=1000, front_trim=100)
    res = pack_profile(mk("p", [950]), spec)
    assert res.bar_count == 0 and len(res.unplaceable) == 1


# --------------------------------------------------------------------------- #
# Allowances on remnant bars
# --------------------------------------------------------------------------- #

def test_kerf_and_trims_apply_to_a_remnant_bar():
    spec = StockSpec(profile="p", stock_length=6000, kerf=5, front_trim=30, back_trim=70,
                     extra_stock=rem((2000, "R-0001")))
    res = pack_profile(mk("p", [900, 900]), spec)
    bar = res.bars[0]
    assert bar.source == "R-0001"
    assert bar.usable_length == 1900              # 2000 - 30 - 70
    # kerf-inclusive offsets within the usable region, exactly as on a tramo
    assert sorted(p.start for p in bar.placements) == [0, 905]
    assert bar.remnant == pytest.approx(1900 - (905 + 905))


def test_remnant_capacity_is_its_own_length_not_the_tramo():
    spec = StockSpec(profile="p", stock_length=3000, extra_stock=rem((2000, "R-0001")))
    res = pack_profile(mk("p", [1500, 1500, 1500]), spec)
    # 2 x 1500 fit a 3 m tramo but only ONE fits the 2 m retazo
    assert res.remnants_used == ["R-0001"]
    assert res.new_bars_needed == 1               # 2 tramos without the rack
    assert [len(b.placements) for b in res.bars] == [1, 2]


def test_yield_uses_the_actual_mixed_stock_lengths():
    spec = StockSpec(profile="p", stock_length=2500, extra_stock=rem((2000, "R-0001")))
    res = pack_profile(mk("p", [2000, 1000]), spec)
    assert res.total_stock_length == 4500         # 2000 retazo + 2500 tramo
    assert res.yield_pct == pytest.approx(100.0 * 3000 / 4500)


# --------------------------------------------------------------------------- #
# Determinism + grouping
# --------------------------------------------------------------------------- #

def test_same_input_twice_gives_an_identical_layout():
    def layout():
        spec = StockSpec(profile="p", stock_length=6000, kerf=3, back_trim=300,
                         extra_stock=rem((2400, "R-0001"), (2400, "R-0002"),
                                         (1000, "R-0003")))
        res = pack_profile(mk("p", [2000, 1800, 1500, 900, 900, 400]), spec)
        return [(b.source, b.stock_length,
                 [(p.part.name, p.start, p.end) for p in b.placements])
                for b in res.bars]

    assert layout() == layout()


def test_remnants_are_per_profile():
    parts = mk("40x40x2", [1000]) + mk("d32", [1000])
    specs = {
        "40x40x2": StockSpec(profile="40x40x2", stock_length=6000,
                             extra_stock=rem((1200, "R-0001"))),
        "d32": StockSpec(profile="d32", stock_length=6000),
    }
    by_profile = {r.profile: r for r in pack_all(parts, specs)}
    assert by_profile["40x40x2"].remnants_used == ["R-0001"]
    assert by_profile["d32"].remnants_used == []
    assert by_profile["d32"].new_bars_needed == 1


def test_no_empty_bars_in_the_layout():
    spec = StockSpec(profile="p", stock_length=6000,
                     extra_stock=rem((5000, "R-A"), (4000, "R-B"), (100, "R-C")))
    res = pack_profile(mk("p", [3000, 2000, 1000]), spec)
    assert all(b.placements for b in res.bars)
    assert [b.index for b in res.bars] == list(range(len(res.bars)))


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def test_json_reports_source_and_stock_length_per_bar(tmp_path):
    from nester.tube.report import write_reports

    spec = StockSpec(profile="p", stock_length=2500, extra_stock=rem((2400, "R-0001")))
    res = pack_profile(mk("p", [2000, 1500]), spec)
    written = write_reports([res], str(tmp_path), "job", {"generated": "", "lang": "es"})
    data = json.loads(open([w for w in written if w.endswith(".json")][0]).read())

    prof = data["profiles"][0]
    assert prof["new_bars_needed"] == 1
    assert prof["remnants_used"] == ["R-0001"]
    assert data["totals"]["new_bars_needed"] == 1
    layout = {b["source"]: b for b in prof["layout"]}
    assert set(layout) == {"R-0001", "nuevo"}
    assert layout["R-0001"]["stock_length"] == 2400
    assert layout["nuevo"]["stock_length"] == 2500


def test_pdf_draws_a_mixed_length_plan(tmp_path):
    """The PDF must survive bars of different lengths (each drawn at its own
    scale position) — a remnant longer than the tramo included."""
    from nester.tube.report import write_reports

    spec = StockSpec(profile="p", stock_length=6000, kerf=3, back_trim=300,
                     extra_stock=rem((2400, "R-0001"), (9000, "R-LARGO")))
    res = pack_profile(mk("p", [8000, 2000, 1500, 500]), spec)
    written = write_reports([res], str(tmp_path), "job", {"generated": "", "lang": "es"})
    pdfs = [w for w in written if w.endswith(".pdf")]
    if not pdfs:                      # reportlab is optional
        pytest.skip("reportlab not installed")
    assert open(pdfs[0], "rb").read(4) == b"%PDF"


def test_pdf_bar_label_names_the_source():
    from nester.tube.report import _LANG, _bar_label, _tramo_numbers

    spec = StockSpec(profile="p", stock_length=2500, extra_stock=rem((2400, "R-0001")))
    res = pack_profile(mk("p", [2000, 1500]), spec)
    nums = _tramo_numbers(res)
    labels = [_bar_label(_LANG["es"], b, nums) for b in res.bars]
    assert "SOBRANTE R-0001 (2400 mm)" in labels
    # tramo numbering counts only the bars actually bought
    assert "TRAMO 1" in labels


def test_iges_nest_boxes_each_bar_at_its_own_length(tmp_path):
    from nester.tube.iges_nest import write_nest_iges

    spec = StockSpec(profile="p", stock_length=2500, extra_stock=rem((2400, "R-0001")))
    res = pack_profile(mk("p", [2000, 1500]), spec)
    path = tmp_path / "nest.igs"
    write_nest_iges([res], str(path), {"p": (50.0, 50.0)})
    text = path.read_text()
    assert "2400." in text            # the retazo bar outline
    assert "2500." in text            # the tramo bar outline


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_remnant_flag_feeds_the_nest(tmp_path, capsys):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.make_sample_iges import build

    from nester.tube import cli as tube_cli

    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))
    out_dir = tmp_path / "out"
    rc = tube_cli.main([
        str(src), "--stock-length", "6000", "--remnant", "40x40x2=1500:R-0001",
        "--no-iges", "--out", str(out_dir), "--name", "job1",
    ])
    assert rc == 0
    data = json.loads((out_dir / "job1" / "job1_corte.json").read_text())
    prof = data["profiles"][0]
    assert prof["remnants_used"] == ["R-0001"]
    assert prof["new_bars_needed"] == 0
    assert prof["layout"][0]["stock_length"] == 1500


def test_cli_remnant_for_an_unknown_profile_warns_and_continues(tmp_path, capsys):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.make_sample_iges import build

    from nester.tube import cli as tube_cli

    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))
    rc = tube_cli.main([str(src), "--stock-length", "6000", "--no-iges",
                        "--remnant", "50X50X2=1500"])
    assert rc == 0
    assert "ignored" in capsys.readouterr().err
