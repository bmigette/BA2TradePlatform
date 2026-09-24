"""Behaviour-distinct TOP-N selection (``app.services.distinct_topn``).

WHY. A converged GA (stage-1 option job 1, gen 34) had 11 distinct PARAMETER sets in its top 20
that all produced the identical backtest -- fitness 14.129, +1588%, -34.3% maxDD, 351 trades --
because they differed only in inert genes. The end-of-job TOP-N dedupes by fitness/params, so it
persists near-identical strategies. ``select_behaviour_distinct`` picks the best N that actually
BEHAVE differently. These tests pin: clones collapse to one pick carrying the clone count, the
fitness order is preserved, the min-difference tolerances drop near-duplicates, sentinel /
unmeasured / low-trade genomes are never picks, and asking for more than exists returns what
exists.
"""
from __future__ import annotations

import pytest

from app.services.distinct_topn import (
    Tolerances,
    annualise,
    behaviour_fingerprint,
    labelled_calendar_year_returns,
    select_behaviour_distinct,
)
from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    STALLED_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
)

_OFF = Tolerances(return_rel_pct=0.0, dd_pts=0.0, trades_rel_pct=0.0)


def _rec(fit, ret, dd, trades, *, key=None, gene=0):
    return {"params": {"g": gene}, "fitness": fit, "key": key or f"k{gene}", "trades": trades,
            "total_return": ret, "max_drawdown": dd}


def test_clones_collapse_to_one_pick_with_clone_count():
    results = [
        _rec(14.129, 1588.04, -34.30, 351, gene=1),
        _rec(14.129, 1588.04, -34.30, 351, gene=2),   # inert-gene clone
        _rec(14.129, 1588.041, -34.299, 351, gene=3),  # same after rounding to 2dp
        _rec(9.0, 400.0, -20.0, 120, gene=4),
    ]
    picks = select_behaviour_distinct(results, 10, min_trades=30, tolerances=_OFF)
    assert [p.params["g"] for p in picks] == [1, 4]
    assert picks[0].clones == 2 and picks[1].clones == 0
    assert picks[0].rank == 1 and picks[1].rank == 2


def test_tie_keeps_first_seen():
    results = [_rec(5.0, 100.0, -10.0, 50, gene=7), _rec(5.0, 100.0, -10.0, 50, gene=8)]
    picks = select_behaviour_distinct(results, 5, min_trades=30, tolerances=_OFF)
    assert len(picks) == 1 and picks[0].params["g"] == 7 and picks[0].clones == 1


def test_ranking_by_fitness_preserved():
    results = [_rec(3.0, 50.0, -5.0, 40, gene=1), _rec(9.0, 900.0, -40.0, 300, gene=2),
               _rec(6.0, 300.0, -25.0, 200, gene=3)]
    picks = select_behaviour_distinct(results, 3, min_trades=30, tolerances=_OFF)
    assert [p.fitness for p in picks] == [9.0, 6.0, 3.0]
    assert [p.rank for p in picks] == [1, 2, 3]


def test_min_difference_thresholds_exclude_near_duplicates():
    results = [
        _rec(10.0, 1000.0, -30.0, 300, gene=1),
        _rec(9.9, 1010.0, -30.5, 302, gene=2),   # 1% ret, 0.5pt DD, 0.7% trades -> near-dup
        _rec(9.8, 1100.0, -30.0, 300, gene=3),   # 9% relative return -> distinct
        _rec(9.7, 1000.0, -33.0, 300, gene=4),   # 3pt DD -> distinct
        _rec(9.6, 1000.0, -30.0, 330, gene=5),   # 9% trades -> distinct
    ]
    picks = select_behaviour_distinct(results, 10, min_trades=30, tolerances=Tolerances())
    assert [p.params["g"] for p in picks] == [1, 3, 4, 5]
    assert picks[0].near_duplicates == 1
    # With every tolerance off, only EXACT fingerprints collapse.
    picks_off = select_behaviour_distinct(results, 10, min_trades=30, tolerances=_OFF)
    assert len(picks_off) == 5


def test_near_duplicate_must_differ_from_every_selected_pick():
    # gene 3 differs from pick 1 (return) but is within tolerance of pick 2 -> dropped.
    results = [_rec(10.0, 1000.0, -30.0, 300, gene=1), _rec(9.0, 1200.0, -30.0, 300, gene=2),
               _rec(8.0, 1210.0, -30.2, 301, gene=3)]
    picks = select_behaviour_distinct(results, 10, min_trades=30, tolerances=Tolerances())
    assert [p.params["g"] for p in picks] == [1, 2]
    assert picks[1].near_duplicates == 1


def test_sentinel_unmeasured_and_low_trade_genomes_excluded():
    stats: dict = {}
    results = [
        _rec(ZERO_TRADE_SENTINEL, 0.0, 0.0, 0, gene=1),
        _rec(LOW_TRADE_SENTINEL, 5.0, -1.0, 3, gene=2),
        _rec(WIPED_OUT_SENTINEL, -100.0, -100.0, 90, gene=3),
        _rec(STALLED_SENTINEL, 0.0, 0.0, 0, gene=4),
        {"params": {"g": 5}, "fitness": 50.0, "status": "stalled", "trades": 99,
         "total_return": 1.0, "max_drawdown": -1.0},
        _rec("nan", 10.0, -1.0, 99, gene=6),
        _rec(float("nan"), 10.0, -1.0, 99, gene=7),
        _rec(20.0, 900.0, -10.0, 12, gene=8),    # great fitness, below the 30-trade gate
        _rec(20.0, None, -10.0, 60, gene=9),     # no metrics to fingerprint
        _rec(2.0, 20.0, -5.0, 45, gene=10),
    ]
    picks = select_behaviour_distinct(results, 10, min_trades=30, tolerances=_OFF, stats=stats)
    assert [p.params["g"] for p in picks] == [10]
    assert stats["excluded_unmeasured"] == 7
    assert stats["excluded_low_trades"] == 1
    assert stats["excluded_no_metrics"] == 1
    # Explicitly allowed: min_trades=0 lets the low-trade genome in (it ranks first).
    picks0 = select_behaviour_distinct(results, 10, min_trades=0, tolerances=_OFF)
    assert [p.params["g"] for p in picks0] == [8, 10]


def test_n_larger_than_distinct_count_returns_what_exists():
    results = [_rec(5.0, 100.0, -10.0, 50, gene=i) for i in range(6)]
    picks = select_behaviour_distinct(results, 10, min_trades=30, tolerances=_OFF)
    assert len(picks) == 1 and picks[0].clones == 5
    assert select_behaviour_distinct([], 10, min_trades=30, tolerances=_OFF) == []


def test_n_caps_picks_but_clone_counts_cover_the_whole_list():
    results = [_rec(9.0, 900.0, -40.0, 300, gene=1), _rec(8.0, 400.0, -20.0, 200, gene=2),
               _rec(1.0, 900.0, -40.0, 300, gene=3)]  # clone of pick 1, far down the list
    picks = select_behaviour_distinct(results, 1, min_trades=30, tolerances=_OFF)
    assert len(picks) == 1 and picks[0].clones == 1


def test_car_is_annualised_over_the_window():
    results = [_rec(9.0, 300.0, -40.0, 300, gene=1)]
    (p,) = select_behaviour_distinct(results, 1, min_trades=30, tolerances=_OFF, years=2.0)
    assert p.car == pytest.approx(100.0)          # 4x over 2 years = 2x/yr
    assert annualise(-100.0, 3.0) == -100.0
    assert annualise(50.0, None) is None
    (q,) = select_behaviour_distinct(results, 1, min_trades=30, tolerances=_OFF)
    assert q.car is None


def test_fingerprint_rounds_to_two_decimals():
    assert behaviour_fingerprint(351, 1588.0412, -34.2999) == (351, 1588.04, -34.3)


def test_labelled_calendar_year_returns():
    curve = [{"date": "2020-01-02", "equity": 100.0}, {"date": "2020-12-31", "equity": 150.0},
             {"date": "2021-06-30", "equity": 140.0}, {"date": "2021-12-31", "equity": 120.0}]
    got = labelled_calendar_year_returns(curve)
    assert [y for y, _ in got] == ["2020", "2021"]
    assert got[0][1] == pytest.approx(50.0) and got[1][1] == pytest.approx(-20.0)
    assert labelled_calendar_year_returns([]) == []


def test_zero_tolerance_ignores_only_that_axis():
    # DD differs by 10 pts, return/trades identical-ish: distinct by default, a near-duplicate
    # once the DD axis is ignored.
    results = [_rec(10.0, 1000.0, -30.0, 300, gene=1), _rec(9.0, 1001.0, -40.0, 300, gene=2)]
    assert len(select_behaviour_distinct(results, 5, min_trades=30,
                                         tolerances=Tolerances())) == 2
    no_dd = Tolerances(return_rel_pct=5.0, dd_pts=0.0, trades_rel_pct=5.0)
    assert len(select_behaviour_distinct(results, 5, min_trades=30, tolerances=no_dd)) == 1
