"""The 1D twin of the 2D retazo doctrine — plus the net/gross split.

Two rules are pinned here, both measured before they were written:

1. **A remnant is opened only when it removes a tramo.** The old solver spent
   the rack unconditionally, which cost the shop physical offcuts for a purchase
   order that did not move — and, because the offcut entered the yield
   denominator, made the reported yield FALL. The bigger the piece a shop
   offered, the harder the plan punished it.
2. **The drop is classified** (``min_remnant``): at or above it a SOBRANTE that
   goes back on the rack, below it MERMA. The net yield discounts the sobrante;
   ``yield_pct`` stays the gross number Harriet's frozen contract reads.
"""

import json

import pytest

from nester.tube.model import (
    REMNANT_JOB_ENDED, REMNANT_NO_FIT, REMNANT_NO_GAIN, REMNANT_TOO_SMALL,
    ExtraStock, Part, StockSpec,
)
from nester.tube.packing import pack_profile

# The measured reproduction job: 89 pieces on 6 m stock, 20 tramos, 89.0%.
BOM = [(1850, 14), (1200, 22), (900, 18), (2400, 9), (640, 26)]


def bom_parts():
    return [Part(name=f"p{L}-{i}", profile="p", length=float(L))
            for L, n in BOM for i in range(n)]


def spec(*lengths, stock=6000, kerf=0.2, back=300, front=0.0, min_remnant=0.0):
    return StockSpec(
        profile="p", stock_length=stock, kerf=kerf,
        front_trim=front, back_trim=back, min_remnant=min_remnant,
        extra_stock=tuple(ExtraStock(length=float(L), label=f"R-{i:04d}")
                          for i, L in enumerate(lengths, start=1)))


def mk(*lengths):
    return [Part(name=f"part{i}", profile="p", length=float(L))
            for i, L in enumerate(lengths)]


# --------------------------------------------------------------------------- #
# Rule 1 — spend a remnant only when it buys a tramo back
# --------------------------------------------------------------------------- #

def test_baseline_is_what_the_job_costs_with_no_rack():
    base = pack_profile(bom_parts(), spec())
    assert base.new_bars_needed == 20
    assert round(base.yield_pct, 1) == 89.0


@pytest.mark.parametrize("mm", [1000, 1500, 2000, 2200, 2500, 3000])
def test_a_remnant_that_removes_no_tramo_is_declined(mm):
    """Measured: each of these used to be spent and cost up to -2.2pp yield."""
    base = pack_profile(bom_parts(), spec())
    r = pack_profile(bom_parts(), spec(mm))
    assert r.remnants_used == []                        # still on the rack
    assert r.remnant_reasons == {"R-0001": REMNANT_NO_GAIN}
    assert [e.length for e in r.remnants_unused] == [mm]
    assert r.new_bars_needed == base.new_bars_needed    # it bought nothing
    assert r.yield_pct == base.yield_pct                # so it costs nothing


@pytest.mark.parametrize("mm", [3400, 4000, 5000, 6000])
def test_a_remnant_that_removes_a_tramo_is_spent(mm):
    base = pack_profile(bom_parts(), spec())
    r = pack_profile(bom_parts(), spec(mm))
    assert r.remnants_used == ["R-0001"]
    assert r.new_bars_needed == base.new_bars_needed - 1


def test_no_offered_size_can_ever_lower_the_yield_without_buying_a_tramo():
    """The whole point, swept: every size either removes a tramo or is declined."""
    base = pack_profile(bom_parts(), spec())
    for mm in range(500, 6001, 100):
        r = pack_profile(bom_parts(), spec(mm))
        if r.new_bars_needed == base.new_bars_needed:
            assert r.remnants_used == []
            assert r.yield_pct == pytest.approx(base.yield_pct)
        else:
            assert r.new_bars_needed < base.new_bars_needed


def test_two_remnants_that_only_help_together_are_both_opened():
    """A single-pass 'test each piece alone' greedy would decline both.

    4 x 500 on 1 m stock is exactly 2 tramos. One 500 offcut leaves 1500 mm of
    parts — still 2 tramos, no gain. TWO leave 1000 — one tramo. So neither
    piece is worth anything alone and both are worth a tramo together.
    """
    parts = mk(500, 500, 500, 500)
    base = pack_profile(parts, spec(stock=1000, kerf=0, back=0))
    assert base.new_bars_needed == 2

    alone = pack_profile(parts, spec(500, stock=1000, kerf=0, back=0))
    assert alone.remnants_used == []                    # declined: no gain
    assert alone.new_bars_needed == 2

    both = pack_profile(parts, spec(500, 500, stock=1000, kerf=0, back=0))
    assert both.remnants_used == ["R-0001", "R-0002"]   # both, or neither
    assert both.new_bars_needed == 1


def test_only_the_minimal_subset_is_spent():
    """Three pieces fit the job; one is enough, so two stay on the rack."""
    r = pack_profile(mk(1000), spec(1500, 1200, 1100, kerf=0, back=0))
    assert r.new_bars_needed == 0
    assert r.remnants_used == ["R-0003"]                # the 1100 — smallest fit
    assert {e.label for e in r.remnants_unused} == {"R-0001", "R-0002"}
    assert set(r.remnant_reasons.values()) == {REMNANT_NO_GAIN}


def test_smallest_first_order_survives_among_the_pieces_actually_spent():
    """Big offcuts stay free for big parts — the order rule is unchanged."""
    r = pack_profile(mk(3500, 1400), spec(4000, 1500, kerf=0, back=0))
    assert r.new_bars_needed == 0
    assert [b.stock_length for b in r.bars] == [4000, 1500]


def test_a_remnant_that_rescues_an_unplaceable_part_is_worth_opening():
    """Fewer tramos is not the only gain: a part no tramo can hold counts."""
    r = pack_profile(mk(8000), spec(12000, kerf=0, back=0))
    assert r.unplaceable == []
    assert r.remnants_used == ["R-0001"]


def test_the_subset_search_is_bounded(monkeypatch):
    """A 200-piece rack must not turn a synchronous nest into a 6-second one.

    The accept/decline decision is always exact (2 packs); only the minimal
    -subset refinement is capped, and it spends its budget on the LONGEST
    pieces — the ones it costs most to burn.
    """
    import nester.tube.packing as pk

    real, calls = pk._pack, []

    def counted(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(pk, "_pack", counted)
    rack = [900 + 25 * i for i in range(120)]
    r = pk.pack_profile(mk(*([800] * 40)), spec(*rack, kerf=0, back=0))
    assert len(calls) <= pk.MAX_SUBSET_TRIALS + 3      # 2 probes + the refinement
    monkeypatch.undo()
    base = pack_profile(mk(*([800] * 40)), spec(kerf=0, back=0))
    assert r.new_bars_needed <= base.new_bars_needed   # never worse than no rack


def test_declining_never_makes_the_plan_worse_than_no_rack_at_all():
    for mm in (700, 2000, 3000, 3400, 5000):
        base = pack_profile(bom_parts(), spec())
        r = pack_profile(bom_parts(), spec(mm))
        assert r.new_bars_needed <= base.new_bars_needed
        assert r.yield_pct >= base.yield_pct - 1e-9


# --------------------------------------------------------------------------- #
# Why a piece was not opened — the four reasons, mirroring 2D
# --------------------------------------------------------------------------- #

def test_reason_no_fit_when_nothing_in_the_job_could_go_on_it():
    r = pack_profile(bom_parts(), spec(500))
    assert r.remnant_reasons == {"R-0001": REMNANT_NO_FIT}


def test_reason_too_small_when_the_trims_eat_the_whole_piece():
    r = pack_profile(mk(40), spec(350, stock=6000, kerf=0, front=100, back=300))
    assert r.remnant_reasons == {"R-0001": REMNANT_TOO_SMALL}


def test_reason_job_ended_when_the_rack_outlives_the_parts():
    """With the search off the rack is spent blind, so leftovers are just that."""
    r = pack_profile(mk(1000), spec(5000, 4000, kerf=0, back=0),
                     minimize_bars=False)
    assert r.remnants_used == ["R-0002"]                # smallest that fits
    assert r.remnant_reasons == {"R-0001": REMNANT_JOB_ENDED}


# --------------------------------------------------------------------------- #
# The escape hatch — the old behaviour, on purpose
# --------------------------------------------------------------------------- #

def test_no_minimize_bars_restores_unconditional_spending():
    base = pack_profile(bom_parts(), spec())
    r = pack_profile(bom_parts(), spec(3000), minimize_bars=False)
    assert r.remnants_used == ["R-0001"]                # spent for nothing
    assert r.new_bars_needed == base.new_bars_needed    # ... and it bought nothing
    assert r.yield_pct < base.yield_pct                 # the old penalty, intact


def test_cli_flag_turns_the_rule_off(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.make_sample_iges import build

    from nester.tube import cli as tube_cli

    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))

    def run(extra, name):
        out = tmp_path / "out"
        assert tube_cli.main([
            str(src), "--stock-length", "6000", "--no-iges",
            "--remnant", "40x40x2=5500:R-0001",
            "--out", str(out), "--name", name] + extra) == 0
        return json.loads((out / name / f"{name}_corte.json").read_text())

    # One 1200 mm part: a 5500 offcut removes the only tramo, so it IS opened.
    assert run([], "gain")["profiles"][0]["remnants_used"] == ["R-0001"]

    # Two parts that fit ONE 3 m tramo: the offcut buys nothing -> declined...
    src2 = tmp_path / "40x40x2_b.igs"
    src2.write_text(build(1200))
    out = tmp_path / "out"
    assert tube_cli.main([
        str(src), str(src2), "--stock-length", "3000", "--no-iges",
        "--remnant", "40x40x2=1300:R-0001",
        "--out", str(out), "--name", "decline"]) == 0
    prof = json.loads((out / "decline" / "decline_corte.json").read_text())["profiles"][0]
    assert prof["remnants_used"] == []
    assert prof["remnants_unused"] == [
        {"label": "R-0001", "length": 1300.0, "reason": "no_gain"}]

    # ... unless the shop asks for the old behaviour.
    assert tube_cli.main([
        str(src), str(src2), "--stock-length", "3000", "--no-iges",
        "--no-minimize-bars", "--remnant", "40x40x2=1300:R-0001",
        "--out", str(out), "--name", "forced"]) == 0
    forced = json.loads((out / "forced" / "forced_corte.json").read_text())["profiles"][0]
    assert forced["remnants_used"] == ["R-0001"]


# --------------------------------------------------------------------------- #
# Rule 2 — SOBRANTE vs MERMA, net vs gross
# --------------------------------------------------------------------------- #

def test_min_remnant_splits_the_drop_into_sobrante_and_merma():
    # 1900 fills its bar to 100 mm left (MERMA); 1000 leaves 1000 (SOBRANTE).
    r = pack_profile(mk(1900, 1000), spec(stock=2000, kerf=0, back=0,
                                          min_remnant=200))
    scrap, keep = r.bars[0], r.bars[1]
    assert keep.remnant == 1000 and keep.leftover == 1000 and keep.waste == 0
    assert scrap.remnant == 100 and scrap.leftover == 0 and scrap.waste == 100
    assert r.reclaimable_length == 1000
    assert r.reclaimable == [(2, 1000.0)]


def test_a_drop_exactly_at_the_threshold_is_kept():
    r = pack_profile(mk(800), spec(stock=1000, kerf=0, back=0, min_remnant=200))
    assert r.bars[0].leftover == 200


def test_net_discounts_what_goes_back_on_the_rack():
    r = pack_profile(mk(1900, 1000), spec(stock=2000, kerf=0, back=0,
                                          min_remnant=200))
    assert r.total_stock_length == 4000
    assert r.consumed_length == 3000                    # 4000 - 1000 returned
    assert r.net_yield_pct == pytest.approx(100.0 * 2900 / 3000)
    assert r.gross_yield_pct == r.yield_pct == pytest.approx(100.0 * 2900 / 4000)
    assert r.waste_length == pytest.approx(4000 - 2900 - 1000)
    assert r.net_yield_pct > r.gross_yield_pct


def test_without_a_min_remnant_net_equals_gross():
    """Contract safety: the default changes no number at all."""
    r = pack_profile(mk(1900, 1000), spec(stock=2000, kerf=0, back=0))
    assert r.reclaimable_length == 0
    assert r.net_yield_pct == r.gross_yield_pct == r.yield_pct
    assert r.waste_length == pytest.approx(4000 - 2900)


def test_min_remnant_is_not_the_back_trim():
    """The chuck dead zone is a different thing and must not classify the drop."""
    r = pack_profile(mk(1000), spec(stock=2000, kerf=0, back=300))
    assert r.spec.min_remnant == 0
    assert r.bars[0].remnant == 700 and r.bars[0].leftover == 0
    assert r.net_yield_pct == r.yield_pct


def test_using_the_rack_never_costs_the_shop():
    """The behaviour that punished the shop, pinned so it cannot come back.

    (The metric to watch is the GROSS yield and the purchase order. The net
    yield is layout-dependent — buying one tramo less also means one less
    long drop to reclaim — so it is not the invariant here.)
    """
    base = pack_profile(bom_parts(), spec(min_remnant=200))
    with_rack = pack_profile(bom_parts(), spec(3400, min_remnant=200))
    assert with_rack.new_bars_needed < base.new_bars_needed
    assert with_rack.new_stock_length < base.new_stock_length
    assert with_rack.yield_pct >= base.yield_pct


def test_json_carries_the_net_pair_and_the_declined_pieces(tmp_path):
    from nester.tube.report import write_reports

    # The measured job with a 3000 mm offcut offered: declined (no_gain), and
    # the bars that end with 200 mm or more report it as a keepable sobrante.
    r = pack_profile(bom_parts(), spec(3000, min_remnant=200))
    written = write_reports([r], str(tmp_path), "job",
                            {"generated": "", "lang": "es", "kerf": 0.2,
                             "front_trim": 0, "back_trim": 300,
                             "min_remnant": 200, "minimize_bars": True})
    data = json.loads(open([w for w in written if w.endswith(".json")][0]).read())
    prof = data["profiles"][0]
    assert prof["min_remnant"] == 200
    assert prof["yield_pct"] == prof["gross_yield_pct"]
    assert prof["reclaimable"] > 0
    assert prof["net_yield_pct"] > prof["gross_yield_pct"]
    assert prof["remnants_unused"] == [
        {"label": "R-0001", "length": 3000.0, "reason": "no_gain"}]
    for bar in prof["layout"]:
        assert bar["leftover"] + bar["waste"] == pytest.approx(bar["remnant"])
        assert bar["leftover"] in (0, bar["remnant"])
    assert data["params"]["min_remnant"] == 200
    assert data["params"]["minimize_bars"] is True
    assert data["totals"]["reclaimable"] == prof["reclaimable"]


def test_cli_min_remnant_reaches_the_plan(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.make_sample_iges import build

    from nester.tube import cli as tube_cli

    src = tmp_path / "40x40x2_a.igs"
    src.write_text(build(1200))
    out = tmp_path / "out"
    assert tube_cli.main([str(src), "--stock-length", "6000", "--no-iges",
                          "--min-remnant", "500", "--out", str(out),
                          "--name", "j"]) == 0
    prof = json.loads((out / "j" / "j_corte.json").read_text())["profiles"][0]
    assert prof["min_remnant"] == 500
    assert prof["reclaimable"] == 4800.0                # 6000 - 1200, kept
    assert prof["net_yield_pct"] == 100.0               # nothing consumed but the part
    assert prof["yield_pct"] == 20.0                    # gross, unchanged
