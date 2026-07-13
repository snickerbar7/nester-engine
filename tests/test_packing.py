from nester.tube.model import Part, StockSpec
from nester.tube.packing import pack_profile, pack_all


def mk(profile, lengths):
    return [Part(name=f"{profile}-{i}", profile=profile, length=L) for i, L in enumerate(lengths)]


def test_single_bar_exact_fit():
    spec = StockSpec(profile="p", stock_length=1000)
    res = pack_profile(mk("p", [400, 600]), spec)
    assert res.bar_count == 1
    assert res.bars[0].remnant == 0
    assert res.yield_pct == 100.0


def test_kerf_consumes_material():
    spec = StockSpec(profile="p", stock_length=1000, kerf=10)
    # 400+10 + 600+10 = 1020 > 1000 -> needs two bars
    res = pack_profile(mk("p", [400, 600]), spec)
    assert res.bar_count == 2


def test_trims_reduce_usable():
    spec = StockSpec(profile="p", stock_length=1000, front_trim=30, back_trim=20)
    assert spec.usable_length == 950
    res = pack_profile(mk("p", [500, 500]), spec)  # 1000 > 950 usable
    assert res.bar_count == 2


def test_ffd_packs_tightly():
    spec = StockSpec(profile="p", stock_length=1000)
    parts = mk("p", [700, 600, 400, 300, 300])  # total 2300 -> >=3 bars
    res = pack_profile(parts, spec)
    assert res.bar_count == 3
    # every part placed, none lost
    placed = sum(len(b.placements) for b in res.bars)
    assert placed == 5


def test_cut_positions_account_for_kerf():
    spec = StockSpec(profile="p", stock_length=1000, kerf=5)
    res = pack_profile(mk("p", [300, 200]), spec)
    bar = res.bars[0]
    # first part starts at 0, second starts after 300 + 5 kerf = 305
    starts = sorted(p.start for p in bar.placements)
    assert starts == [0, 305]


def test_unplaceable_part_flagged():
    spec = StockSpec(profile="p", stock_length=1000, front_trim=100)
    res = pack_profile(mk("p", [950]), spec)  # usable 900 < 950
    assert res.bar_count == 0
    assert len(res.unplaceable) == 1


def test_pack_all_groups_by_profile():
    parts = mk("40x40x2", [500, 500]) + mk("d32", [300])
    specs = {
        "40x40x2": StockSpec(profile="40x40x2", stock_length=1000),
        "d32": StockSpec(profile="d32", stock_length=1000),
    }
    results = pack_all(parts, specs)
    assert {r.profile for r in results} == {"40x40x2", "d32"}


def test_pack_all_missing_spec_raises():
    import pytest
    parts = mk("p", [100])
    with pytest.raises(KeyError):
        pack_all(parts, {})
