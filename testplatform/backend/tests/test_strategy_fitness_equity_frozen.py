"""FROZEN BASELINE: the EQUITY (non-option) fitness path must never move.

WHY THIS FILE EXISTS. Track D added an OPTION-ONLY fitness metric
(``option_consistent_annual_return``) with a superlinear drawdown penalty. Non-option
backtests were RUNNING while that change was made, so their scores had to stay
bit-identical -- a re-ranking mid-grid silently invalidates every result already banked.

The numbers below are LITERALS captured by loading the module AS IT WAS AT COMMIT 0323d0d1
(before the option metric existed) and scoring a 38-case x 19-metric corpus. They are not
re-derived from the code under test, so this asserts against an external record rather than
against itself: if any equity metric's arithmetic moves by one ULP, a case here fails.

``repr()`` round-trip equality is used deliberately -- ``float(repr(x)) == x`` is exact in
CPython, so this is a BIT comparison, not an approximate one.

If you are changing an equity metric ON PURPOSE, regenerate these literals in the same
commit and say so in the message. Do not "fix" a failure by loosening the comparison.

*** NOT COMPARABLE ACROSS THIS COMMIT (2026-09-02, plan Task 14b item 6) ***
EIGHT literals were deliberately re-frozen, and a fitness produced before this commit cannot
be compared with one produced after it FOR THE THREE STRESSED CASES BELOW. Everything else in
this file is bit-identical, and that is asserted by the remaining ~800 unchanged literals.

WHAT CHANGED. ``stressed_results`` restated only ``annualized_return`` and ``max_drawdown``.
For ``total_return``/``return`` and ``calmar_ratio`` the stressed pass therefore re-read the
COPIED, UNSTRESSED value off ``dict(results)``, so ``min(base, stressed)`` was inert: the
--stress-spread-bps flag looked applied and changed nothing on exactly the metrics that rank
on total return. It now restates both from the same stressed path.

THE MOVED KEYS, with the arithmetic beside each entry below:
  stress_on|return, stress_on|total_return                      119.7 -> 5.625799999999988
  stress_on_thin|return, stress_on_thin|total_return            119.7 -> 5.625799999999988
  stress_mid_concentration|return, ...|total_return             1.3895... -> 0.2123...
  stress_mid_concentration|calmar, ...|calmar_ratio             0.01741... -> 0.06703...

WHY IT WAS SAFE TO RE-FREEZE NOW. The lift condition recorded beside the CAR/OCAR unification
("fold the two together when no grid is running") is about runs USING the affected metrics.
``_consistent_annual_return`` and ``_option_consistent_annual_return`` read
``annualized_return``/``adjusted_annualized_return``, ``max_drawdown``, ``equity_curve`` and
the trade rate -- verified by reading the function bodies, not assumed -- and NEITHER reads
``total_return`` or ``calmar_ratio``, so a CAR grid mid-flight is provably unaffected by this
change. ``robustness_metrics``'s spread screen likewise reads only ``annualized_return``.
The operator confirmed no grid is running with --fitness total_return/return/calmar_ratio.
NOTE for the next person: ``tools/run_screener_capband_matrix.py`` DEFAULTS to
``--fitness calmar_ratio``, so a run of that driver started before this commit is on the old
scale -- do not compare its results across it.

COVERAGE IS THE WHOLE VALUE HERE. A case the corpus does not contain is a change this file
cannot see -- the first version of it missed the profit-cap switch (no case had an adjusted
figure present with no cap active), and a mutation of that branch passed. When you add a
branch to the equity path, add a corpus case with it.
"""
import math

import pytest

from app.services.strategy_fitness import compute_fitness, robustness_metrics


def _curve(points):
    return [{"date": d, "equity": e} for d, e in points]

_C3 = _curve([("2020-01-02", 100_000.0), ("2020-12-31", 130_000.0),
              ("2021-12-31", 169_000.0), ("2022-12-30", 219_700.0)])
_CU = _curve([("2020-01-02", 100_000.0), ("2020-12-31", 150_000.0),
              ("2021-12-31", 165_000.0), ("2022-12-30", 247_500.0)])
_TR = [{"pnl": 100.0, "pnl_pct": 1.0, "exit_time": "2020-06-01"},
       {"pnl": -40.0, "pnl_pct": -0.4, "exit_time": "2021-06-01"},
       {"pnl": 500.0, "pnl_pct": 5.0, "exit_time": "2022-06-01"}]

# A book whose top-5 share lands strictly BETWEEN the concentration penalty's free (40%) and
# dead (100%) bounds -- 76.5%, giving conc_factor 0.2456. _TR above is 100% concentrated, where
# the factor clamps to 0.0 and stops depending on _CONC_EXP at all; with only that case a change
# to the exponent was invisible to this freeze.
_TR_MID = [{"pnl": p, "pnl_pct": p / 50.0,
            "exit_time": f"{2020 + i // 4}-{1 + (i % 4) * 3:02d}-15"}
           for i, p in enumerate([200.0, 150.0, 120.0, 100.0, 80.0, 60.0, 50.0, 40.0, 30.0, 20.0])]

def _base(**kw):
    d = {"total_trades": 300, "avg_trades_per_year": 100.0, "annualized_return": 30.0,
         "max_drawdown": -20.0, "sharpe_ratio": 1.4, "sortino_ratio": 2.1,
         "total_return": 119.7, "profit_factor": 1.8, "win_rate": 61.0,
         "calmar_ratio": 1.5, "sqn": 2.4}
    d.update(kw)
    return d

_ADJ = {"adjusted_annualized_return": 40.0, "adjusted_total_return": 55.0,
        "adjusted_calmar_ratio": 0.9, "adjusted_profit_factor": 1.2, "adjusted_sqn": 1.1}

CORPUS = {
    "plain": _base(),
    "dd_0": _base(max_drawdown=0.0),
    "dd_tiny": _base(max_drawdown=-0.4),
    "dd_5": _base(max_drawdown=-5.0),
    "dd_10": _base(max_drawdown=-10.0),
    "dd_30": _base(max_drawdown=-30.0),
    "dd_40": _base(max_drawdown=-40.0),
    "dd_80": _base(max_drawdown=-80.0),
    "dd_positive_sign": _base(max_drawdown=20.0),
    "dd_none": _base(max_drawdown=None),
    "curve_even": _base(equity_curve=_C3),
    "curve_partial_start_189d": _base(equity_curve=_curve([
        ("2020-06-25", 100_000.0), ("2020-12-31", 120_000.0), ("2021-12-31", 132_000.0)])),
    "curve_uneven": _base(equity_curve=_CU),
    "cap_basis": _base(profit_cap_pct=2000.0, **_ADJ),
    "cap_share": _base(profit_share_cap_pct=25.0, **_ADJ),
    # --- cap-switch coverage. Without these three a metric that ALWAYS preferred the adjusted
    # figure, or never did, scores identically to the real one: measured gap, a mutation of the
    # `if profit_cap_pct or profit_share_cap_pct` test survived the first version of this freeze.
    "adjusted_present_no_cap": _base(annualized_return=80.0, **_ADJ),
    "cap_with_adjusted_missing": _base(annualized_return=80.0, profit_cap_pct=2000.0),
    "cap_with_adjusted_none": _base(annualized_return=80.0, profit_cap_pct=2000.0,
                                    adjusted_annualized_return=None,
                                    adjusted_total_return=None, adjusted_calmar_ratio=None,
                                    adjusted_profit_factor=None, adjusted_sqn=None),
    "thin_11_9": _base(avg_trades_per_year=11.9),
    "thin_15": _base(avg_trades_per_year=15.0),
    "at_floor_12": _base(avg_trades_per_year=12.0),
    "over_60": _base(avg_trades_per_year=60.0),
    "tpy_zero": _base(avg_trades_per_year=0.0),
    "per_run_thresholds": _base(avg_trades_per_year=10.0, car_hard_min_trades_per_year=8.0,
                                car_min_trades_per_year=20.0),
    "zero_trades": _base(total_trades=0),
    "wiped": _base(account_wiped_out=True),
    "neg_base": _base(annualized_return=-15.0, max_drawdown=-35.0, total_return=-40.0,
                      calmar_ratio=-0.4, sqn=-1.1),
    "nan_metric": _base(sharpe_ratio=float("nan"), calmar_ratio=float("inf")),
    "trade_scale": _base(avg_trades_per_year=50.0, fitness_trade_scale=True,
                         fitness_trade_scale_cap=100.0, fitness_trade_scale_target=100.0),
    # target != cap, and a rate ABOVE the cap: exercises both clamps independently.
    "trade_scale_target_50": _base(avg_trades_per_year=140.0, fitness_trade_scale=True,
                                   fitness_trade_scale_cap=100.0,
                                   fitness_trade_scale_target=50.0),
    "trade_scale_on_negative": _base(annualized_return=-15.0, total_return=-40.0, sqn=-1.1,
                                     avg_trades_per_year=50.0, fitness_trade_scale=True),
    "win_rate_factor": _base(fitness_win_rate_factor=True),
    "win_rate_factor_low": _base(fitness_win_rate_factor=True, win_rate=20.0),
    "win_rate_factor_missing_rate": {k: v for k, v in
                                     _base(fitness_win_rate_factor=True).items()
                                     if k != "win_rate"},
    "win_rate_factor_on_negative": _base(fitness_win_rate_factor=True, win_rate=20.0,
                                         annualized_return=-15.0, total_return=-40.0),
    "robust_on": _base(equity_curve=_C3, trades=_TR, robust_fitness=True,
                       initial_capital=100_000.0),
    "stress_on": _base(equity_curve=_C3, trades=_TR, stress_spread_bps=40.0,
                       initial_capital=100_000.0),
    "robust_mid_concentration": _base(equity_curve=_C3, trades=_TR_MID, robust_fitness=True,
                                      initial_capital=100_000.0),
    "stress_mid_concentration": _base(equity_curve=_C3, trades=_TR_MID, robust_fitness=True,
                                      stress_spread_bps=40.0, initial_capital=100_000.0),
    "stress_on_thin": _base(equity_curve=_C3, trades=_TR, stress_spread_bps=40.0,
                            initial_capital=100_000.0, avg_trades_per_year=4.2),
    "no_tpy_no_curve": {k: v for k, v in _base().items() if k != "avg_trades_per_year"},
    "no_tpy_with_curve": {**{k: v for k, v in _base().items() if k != "avg_trades_per_year"},
                          "equity_curve": _C3, "total_trades": 95},
}

METRICS = ["sharpe", "sharpe_ratio", "return", "total_return", "profit_factor", "win_rate",
           "sortino", "sortino_ratio", "calmar", "calmar_ratio", "sqn",
           "max_drawdown", "max_dd", "drawdown",
           "consistent_annual_return", "car", "goal", "CAR", "Goal"]


# --- robustness-component corpus -------------------------------------------------------------
# robustness_metrics' three factors are MULTIPLIED into the fitness, so a case with
# conc_factor == 0.0 zeroes the product and makes the Monte-Carlo constants (seed, path count,
# negative-tail demerit) unobservable in the composed score. These lists are therefore frozen
# component-by-component instead. Each is chosen to land in a different region of the shape.
def _tr(pcts):
    return [{"pnl": p * 50.0, "pnl_pct": p,
             "exit_time": f"{2020 + i // 4}-{1 + (i % 4) * 3:02d}-15"}
            for i, p in enumerate(pcts)]


ROBUSTNESS_CORPUS = {
    # top5 76.5% -> conc_factor strictly between 0 and 1; every path profitable -> mc 1.0
    "mid_concentration": [{"pnl": p, "pnl_pct": p / 50.0,
                           "exit_time": f"{2020 + i // 4}-{1 + (i % 4) * 3:02d}-15"}
                          for i, p in enumerate([200.0, 150.0, 120.0, 100.0, 80.0, 60.0, 50.0,
                                                 40.0, 30.0, 20.0])],
    # mc_prob_neg 0.345 AND mc_p5 < 0 -> exercises the ramp AND the 0.5 negative-tail demerit
    "mixed_monte_carlo": _tr([8.0, -6.0, 7.0, -5.0, 6.0, -4.0, 5.0, -3.0]),
    # prob_neg >= 0.5 -> mc_factor pinned at 0.0
    "monte_carlo_ruined": _tr([30.0, -22.0, 18.0, -16.0, 14.0, -13.0, 9.0, -8.0, 6.0, -5.0]),
    # net <= 0 -> the early return, every factor left at 1.0
    "net_negative": _tr([-5.0, -4.0, 2.0, -3.0]),
    "single_trade": _tr([5.0]),
    "empty": [],
}

# "<corpus case>|<metric>" -> repr() of the fitness BEFORE the option metric was added.
# "RAISES:<ExceptionType>" records a case that raised.
GOLDEN = {
    'adjusted_present_no_cap|CAR': '80.0',
    'adjusted_present_no_cap|Goal': '80.0',
    'adjusted_present_no_cap|calmar': '1.5',
    'adjusted_present_no_cap|calmar_ratio': '1.5',
    'adjusted_present_no_cap|car': '80.0',
    'adjusted_present_no_cap|consistent_annual_return': '80.0',
    'adjusted_present_no_cap|drawdown': '20.0',
    'adjusted_present_no_cap|goal': '80.0',
    'adjusted_present_no_cap|max_dd': '20.0',
    'adjusted_present_no_cap|max_drawdown': '20.0',
    'adjusted_present_no_cap|profit_factor': '1.8',
    'adjusted_present_no_cap|return': '119.7',
    'adjusted_present_no_cap|sharpe': '1.4',
    'adjusted_present_no_cap|sharpe_ratio': '1.4',
    'adjusted_present_no_cap|sortino': '2.1',
    'adjusted_present_no_cap|sortino_ratio': '2.1',
    'adjusted_present_no_cap|sqn': '2.4',
    'adjusted_present_no_cap|total_return': '119.7',
    'adjusted_present_no_cap|win_rate': '61.0',
    'at_floor_12|CAR': '12.0',
    'at_floor_12|Goal': '12.0',
    'at_floor_12|calmar': '1.5',
    'at_floor_12|calmar_ratio': '1.5',
    'at_floor_12|car': '12.0',
    'at_floor_12|consistent_annual_return': '12.0',
    'at_floor_12|drawdown': '20.0',
    'at_floor_12|goal': '12.0',
    'at_floor_12|max_dd': '20.0',
    'at_floor_12|max_drawdown': '20.0',
    'at_floor_12|profit_factor': '1.8',
    'at_floor_12|return': '119.7',
    'at_floor_12|sharpe': '1.4',
    'at_floor_12|sharpe_ratio': '1.4',
    'at_floor_12|sortino': '2.1',
    'at_floor_12|sortino_ratio': '2.1',
    'at_floor_12|sqn': '2.4',
    'at_floor_12|total_return': '119.7',
    'at_floor_12|win_rate': '61.0',
    'cap_basis|CAR': '40.0',
    'cap_basis|Goal': '40.0',
    'cap_basis|calmar': '0.9',
    'cap_basis|calmar_ratio': '0.9',
    'cap_basis|car': '40.0',
    'cap_basis|consistent_annual_return': '40.0',
    'cap_basis|drawdown': '20.0',
    'cap_basis|goal': '40.0',
    'cap_basis|max_dd': '20.0',
    'cap_basis|max_drawdown': '20.0',
    'cap_basis|profit_factor': '1.2',
    'cap_basis|return': '55.0',
    'cap_basis|sharpe': '1.4',
    'cap_basis|sharpe_ratio': '1.4',
    'cap_basis|sortino': '2.1',
    'cap_basis|sortino_ratio': '2.1',
    'cap_basis|sqn': '1.1',
    'cap_basis|total_return': '55.0',
    'cap_basis|win_rate': '61.0',
    'cap_share|CAR': '40.0',
    'cap_share|Goal': '40.0',
    'cap_share|calmar': '0.9',
    'cap_share|calmar_ratio': '0.9',
    'cap_share|car': '40.0',
    'cap_share|consistent_annual_return': '40.0',
    'cap_share|drawdown': '20.0',
    'cap_share|goal': '40.0',
    'cap_share|max_dd': '20.0',
    'cap_share|max_drawdown': '20.0',
    'cap_share|profit_factor': '1.2',
    'cap_share|return': '55.0',
    'cap_share|sharpe': '1.4',
    'cap_share|sharpe_ratio': '1.4',
    'cap_share|sortino': '2.1',
    'cap_share|sortino_ratio': '2.1',
    'cap_share|sqn': '1.1',
    'cap_share|total_return': '55.0',
    'cap_share|win_rate': '61.0',
    'cap_with_adjusted_missing|CAR': '80.0',
    'cap_with_adjusted_missing|Goal': '80.0',
    'cap_with_adjusted_missing|calmar': '1.5',
    'cap_with_adjusted_missing|calmar_ratio': '1.5',
    'cap_with_adjusted_missing|car': '80.0',
    'cap_with_adjusted_missing|consistent_annual_return': '80.0',
    'cap_with_adjusted_missing|drawdown': '20.0',
    'cap_with_adjusted_missing|goal': '80.0',
    'cap_with_adjusted_missing|max_dd': '20.0',
    'cap_with_adjusted_missing|max_drawdown': '20.0',
    'cap_with_adjusted_missing|profit_factor': '1.8',
    'cap_with_adjusted_missing|return': '119.7',
    'cap_with_adjusted_missing|sharpe': '1.4',
    'cap_with_adjusted_missing|sharpe_ratio': '1.4',
    'cap_with_adjusted_missing|sortino': '2.1',
    'cap_with_adjusted_missing|sortino_ratio': '2.1',
    'cap_with_adjusted_missing|sqn': '2.4',
    'cap_with_adjusted_missing|total_return': '119.7',
    'cap_with_adjusted_missing|win_rate': '61.0',
    'cap_with_adjusted_none|CAR': '80.0',
    'cap_with_adjusted_none|Goal': '80.0',
    'cap_with_adjusted_none|calmar': '1.5',
    'cap_with_adjusted_none|calmar_ratio': '1.5',
    'cap_with_adjusted_none|car': '80.0',
    'cap_with_adjusted_none|consistent_annual_return': '80.0',
    'cap_with_adjusted_none|drawdown': '20.0',
    'cap_with_adjusted_none|goal': '80.0',
    'cap_with_adjusted_none|max_dd': '20.0',
    'cap_with_adjusted_none|max_drawdown': '20.0',
    'cap_with_adjusted_none|profit_factor': '1.8',
    'cap_with_adjusted_none|return': '119.7',
    'cap_with_adjusted_none|sharpe': '1.4',
    'cap_with_adjusted_none|sharpe_ratio': '1.4',
    'cap_with_adjusted_none|sortino': '2.1',
    'cap_with_adjusted_none|sortino_ratio': '2.1',
    'cap_with_adjusted_none|sqn': '2.4',
    'cap_with_adjusted_none|total_return': '119.7',
    'cap_with_adjusted_none|win_rate': '61.0',
    'curve_even|CAR': '30.0',
    'curve_even|Goal': '30.0',
    'curve_even|calmar': '1.5',
    'curve_even|calmar_ratio': '1.5',
    'curve_even|car': '30.0',
    'curve_even|consistent_annual_return': '30.0',
    'curve_even|drawdown': '20.0',
    'curve_even|goal': '30.0',
    'curve_even|max_dd': '20.0',
    'curve_even|max_drawdown': '20.0',
    'curve_even|profit_factor': '1.8',
    'curve_even|return': '119.7',
    'curve_even|sharpe': '1.4',
    'curve_even|sharpe_ratio': '1.4',
    'curve_even|sortino': '2.1',
    'curve_even|sortino_ratio': '2.1',
    'curve_even|sqn': '2.4',
    'curve_even|total_return': '119.7',
    'curve_even|win_rate': '61.0',
    'curve_partial_start_189d|CAR': '20.00000000000001',
    'curve_partial_start_189d|Goal': '20.00000000000001',
    'curve_partial_start_189d|calmar': '1.5',
    'curve_partial_start_189d|calmar_ratio': '1.5',
    'curve_partial_start_189d|car': '20.00000000000001',
    'curve_partial_start_189d|consistent_annual_return': '20.00000000000001',
    'curve_partial_start_189d|drawdown': '20.0',
    'curve_partial_start_189d|goal': '20.00000000000001',
    'curve_partial_start_189d|max_dd': '20.0',
    'curve_partial_start_189d|max_drawdown': '20.0',
    'curve_partial_start_189d|profit_factor': '1.8',
    'curve_partial_start_189d|return': '119.7',
    'curve_partial_start_189d|sharpe': '1.4',
    'curve_partial_start_189d|sharpe_ratio': '1.4',
    'curve_partial_start_189d|sortino': '2.1',
    'curve_partial_start_189d|sortino_ratio': '2.1',
    'curve_partial_start_189d|sqn': '2.4',
    'curve_partial_start_189d|total_return': '119.7',
    'curve_partial_start_189d|win_rate': '61.0',
    'curve_uneven|CAR': '8.181818181818187',
    'curve_uneven|Goal': '8.181818181818187',
    'curve_uneven|calmar': '1.5',
    'curve_uneven|calmar_ratio': '1.5',
    'curve_uneven|car': '8.181818181818187',
    'curve_uneven|consistent_annual_return': '8.181818181818187',
    'curve_uneven|drawdown': '20.0',
    'curve_uneven|goal': '8.181818181818187',
    'curve_uneven|max_dd': '20.0',
    'curve_uneven|max_drawdown': '20.0',
    'curve_uneven|profit_factor': '1.8',
    'curve_uneven|return': '119.7',
    'curve_uneven|sharpe': '1.4',
    'curve_uneven|sharpe_ratio': '1.4',
    'curve_uneven|sortino': '2.1',
    'curve_uneven|sortino_ratio': '2.1',
    'curve_uneven|sqn': '2.4',
    'curve_uneven|total_return': '119.7',
    'curve_uneven|win_rate': '61.0',
    'dd_0|CAR': '60.0',
    'dd_0|Goal': '60.0',
    'dd_0|calmar': '1.5',
    'dd_0|calmar_ratio': '1.5',
    'dd_0|car': '60.0',
    'dd_0|consistent_annual_return': '60.0',
    'dd_0|drawdown': '-0.0',
    'dd_0|goal': '60.0',
    'dd_0|max_dd': '-0.0',
    'dd_0|max_drawdown': '-0.0',
    'dd_0|profit_factor': '1.8',
    'dd_0|return': '119.7',
    'dd_0|sharpe': '1.4',
    'dd_0|sharpe_ratio': '1.4',
    'dd_0|sortino': '2.1',
    'dd_0|sortino_ratio': '2.1',
    'dd_0|sqn': '2.4',
    'dd_0|total_return': '119.7',
    'dd_0|win_rate': '61.0',
    'dd_10|CAR': '60.0',
    'dd_10|Goal': '60.0',
    'dd_10|calmar': '1.5',
    'dd_10|calmar_ratio': '1.5',
    'dd_10|car': '60.0',
    'dd_10|consistent_annual_return': '60.0',
    'dd_10|drawdown': '10.0',
    'dd_10|goal': '60.0',
    'dd_10|max_dd': '10.0',
    'dd_10|max_drawdown': '10.0',
    'dd_10|profit_factor': '1.8',
    'dd_10|return': '119.7',
    'dd_10|sharpe': '1.4',
    'dd_10|sharpe_ratio': '1.4',
    'dd_10|sortino': '2.1',
    'dd_10|sortino_ratio': '2.1',
    'dd_10|sqn': '2.4',
    'dd_10|total_return': '119.7',
    'dd_10|win_rate': '61.0',
    'dd_30|CAR': '20.0',
    'dd_30|Goal': '20.0',
    'dd_30|calmar': '1.5',
    'dd_30|calmar_ratio': '1.5',
    'dd_30|car': '20.0',
    'dd_30|consistent_annual_return': '20.0',
    'dd_30|drawdown': '30.0',
    'dd_30|goal': '20.0',
    'dd_30|max_dd': '30.0',
    'dd_30|max_drawdown': '30.0',
    'dd_30|profit_factor': '1.8',
    'dd_30|return': '119.7',
    'dd_30|sharpe': '1.4',
    'dd_30|sharpe_ratio': '1.4',
    'dd_30|sortino': '2.1',
    'dd_30|sortino_ratio': '2.1',
    'dd_30|sqn': '2.4',
    'dd_30|total_return': '119.7',
    'dd_30|win_rate': '61.0',
    'dd_40|CAR': '15.0',
    'dd_40|Goal': '15.0',
    'dd_40|calmar': '1.5',
    'dd_40|calmar_ratio': '1.5',
    'dd_40|car': '15.0',
    'dd_40|consistent_annual_return': '15.0',
    'dd_40|drawdown': '40.0',
    'dd_40|goal': '15.0',
    'dd_40|max_dd': '40.0',
    'dd_40|max_drawdown': '40.0',
    'dd_40|profit_factor': '1.8',
    'dd_40|return': '119.7',
    'dd_40|sharpe': '1.4',
    'dd_40|sharpe_ratio': '1.4',
    'dd_40|sortino': '2.1',
    'dd_40|sortino_ratio': '2.1',
    'dd_40|sqn': '2.4',
    'dd_40|total_return': '119.7',
    'dd_40|win_rate': '61.0',
    'dd_5|CAR': '60.0',
    'dd_5|Goal': '60.0',
    'dd_5|calmar': '1.5',
    'dd_5|calmar_ratio': '1.5',
    'dd_5|car': '60.0',
    'dd_5|consistent_annual_return': '60.0',
    'dd_5|drawdown': '5.0',
    'dd_5|goal': '60.0',
    'dd_5|max_dd': '5.0',
    'dd_5|max_drawdown': '5.0',
    'dd_5|profit_factor': '1.8',
    'dd_5|return': '119.7',
    'dd_5|sharpe': '1.4',
    'dd_5|sharpe_ratio': '1.4',
    'dd_5|sortino': '2.1',
    'dd_5|sortino_ratio': '2.1',
    'dd_5|sqn': '2.4',
    'dd_5|total_return': '119.7',
    'dd_5|win_rate': '61.0',
    'dd_80|CAR': '7.5',
    'dd_80|Goal': '7.5',
    'dd_80|calmar': '1.5',
    'dd_80|calmar_ratio': '1.5',
    'dd_80|car': '7.5',
    'dd_80|consistent_annual_return': '7.5',
    'dd_80|drawdown': '80.0',
    'dd_80|goal': '7.5',
    'dd_80|max_dd': '80.0',
    'dd_80|max_drawdown': '80.0',
    'dd_80|profit_factor': '1.8',
    'dd_80|return': '119.7',
    'dd_80|sharpe': '1.4',
    'dd_80|sharpe_ratio': '1.4',
    'dd_80|sortino': '2.1',
    'dd_80|sortino_ratio': '2.1',
    'dd_80|sqn': '2.4',
    'dd_80|total_return': '119.7',
    'dd_80|win_rate': '61.0',
    'dd_none|CAR': '60.0',
    'dd_none|Goal': '60.0',
    'dd_none|calmar': '1.5',
    'dd_none|calmar_ratio': '1.5',
    'dd_none|car': '60.0',
    'dd_none|consistent_annual_return': '60.0',
    'dd_none|drawdown': '-1000000000.0',
    'dd_none|goal': '60.0',
    'dd_none|max_dd': '-1000000000.0',
    'dd_none|max_drawdown': '-1000000000.0',
    'dd_none|profit_factor': '1.8',
    'dd_none|return': '119.7',
    'dd_none|sharpe': '1.4',
    'dd_none|sharpe_ratio': '1.4',
    'dd_none|sortino': '2.1',
    'dd_none|sortino_ratio': '2.1',
    'dd_none|sqn': '2.4',
    'dd_none|total_return': '119.7',
    'dd_none|win_rate': '61.0',
    'dd_positive_sign|CAR': '30.0',
    'dd_positive_sign|Goal': '30.0',
    'dd_positive_sign|calmar': '1.5',
    'dd_positive_sign|calmar_ratio': '1.5',
    'dd_positive_sign|car': '30.0',
    'dd_positive_sign|consistent_annual_return': '30.0',
    'dd_positive_sign|drawdown': '-20.0',
    'dd_positive_sign|goal': '30.0',
    'dd_positive_sign|max_dd': '-20.0',
    'dd_positive_sign|max_drawdown': '-20.0',
    'dd_positive_sign|profit_factor': '1.8',
    'dd_positive_sign|return': '119.7',
    'dd_positive_sign|sharpe': '1.4',
    'dd_positive_sign|sharpe_ratio': '1.4',
    'dd_positive_sign|sortino': '2.1',
    'dd_positive_sign|sortino_ratio': '2.1',
    'dd_positive_sign|sqn': '2.4',
    'dd_positive_sign|total_return': '119.7',
    'dd_positive_sign|win_rate': '61.0',
    'dd_tiny|CAR': '60.0',
    'dd_tiny|Goal': '60.0',
    'dd_tiny|calmar': '1.5',
    'dd_tiny|calmar_ratio': '1.5',
    'dd_tiny|car': '60.0',
    'dd_tiny|consistent_annual_return': '60.0',
    'dd_tiny|drawdown': '0.4',
    'dd_tiny|goal': '60.0',
    'dd_tiny|max_dd': '0.4',
    'dd_tiny|max_drawdown': '0.4',
    'dd_tiny|profit_factor': '1.8',
    'dd_tiny|return': '119.7',
    'dd_tiny|sharpe': '1.4',
    'dd_tiny|sharpe_ratio': '1.4',
    'dd_tiny|sortino': '2.1',
    'dd_tiny|sortino_ratio': '2.1',
    'dd_tiny|sqn': '2.4',
    'dd_tiny|total_return': '119.7',
    'dd_tiny|win_rate': '61.0',
    'nan_metric|CAR': '30.0',
    'nan_metric|Goal': '30.0',
    'nan_metric|calmar': '-1000000000.0',
    'nan_metric|calmar_ratio': '-1000000000.0',
    'nan_metric|car': '30.0',
    'nan_metric|consistent_annual_return': '30.0',
    'nan_metric|drawdown': '20.0',
    'nan_metric|goal': '30.0',
    'nan_metric|max_dd': '20.0',
    'nan_metric|max_drawdown': '20.0',
    'nan_metric|profit_factor': '1.8',
    'nan_metric|return': '119.7',
    'nan_metric|sharpe': '-1000000000.0',
    'nan_metric|sharpe_ratio': '-1000000000.0',
    'nan_metric|sortino': '2.1',
    'nan_metric|sortino_ratio': '2.1',
    'nan_metric|sqn': '2.4',
    'nan_metric|total_return': '119.7',
    'nan_metric|win_rate': '61.0',
    'neg_base|CAR': '-15.0',
    'neg_base|Goal': '-15.0',
    'neg_base|calmar': '-0.4',
    'neg_base|calmar_ratio': '-0.4',
    'neg_base|car': '-15.0',
    'neg_base|consistent_annual_return': '-15.0',
    'neg_base|drawdown': '35.0',
    'neg_base|goal': '-15.0',
    'neg_base|max_dd': '35.0',
    'neg_base|max_drawdown': '35.0',
    'neg_base|profit_factor': '1.8',
    'neg_base|return': '-40.0',
    'neg_base|sharpe': '1.4',
    'neg_base|sharpe_ratio': '1.4',
    'neg_base|sortino': '2.1',
    'neg_base|sortino_ratio': '2.1',
    'neg_base|sqn': '-1.1',
    'neg_base|total_return': '-40.0',
    'neg_base|win_rate': '61.0',
    'no_tpy_no_curve|CAR': '-100000000.0',
    'no_tpy_no_curve|Goal': '-100000000.0',
    'no_tpy_no_curve|calmar': '1.5',
    'no_tpy_no_curve|calmar_ratio': '1.5',
    'no_tpy_no_curve|car': '-100000000.0',
    'no_tpy_no_curve|consistent_annual_return': '-100000000.0',
    'no_tpy_no_curve|drawdown': '20.0',
    'no_tpy_no_curve|goal': '-100000000.0',
    'no_tpy_no_curve|max_dd': '20.0',
    'no_tpy_no_curve|max_drawdown': '20.0',
    'no_tpy_no_curve|profit_factor': '1.8',
    'no_tpy_no_curve|return': '119.7',
    'no_tpy_no_curve|sharpe': '1.4',
    'no_tpy_no_curve|sharpe_ratio': '1.4',
    'no_tpy_no_curve|sortino': '2.1',
    'no_tpy_no_curve|sortino_ratio': '2.1',
    'no_tpy_no_curve|sqn': '2.4',
    'no_tpy_no_curve|total_return': '119.7',
    'no_tpy_no_curve|win_rate': '61.0',
    'no_tpy_with_curve|CAR': '30.0',
    'no_tpy_with_curve|Goal': '30.0',
    'no_tpy_with_curve|calmar': '1.5',
    'no_tpy_with_curve|calmar_ratio': '1.5',
    'no_tpy_with_curve|car': '30.0',
    'no_tpy_with_curve|consistent_annual_return': '30.0',
    'no_tpy_with_curve|drawdown': '20.0',
    'no_tpy_with_curve|goal': '30.0',
    'no_tpy_with_curve|max_dd': '20.0',
    'no_tpy_with_curve|max_drawdown': '20.0',
    'no_tpy_with_curve|profit_factor': '1.8',
    'no_tpy_with_curve|return': '119.7',
    'no_tpy_with_curve|sharpe': '1.4',
    'no_tpy_with_curve|sharpe_ratio': '1.4',
    'no_tpy_with_curve|sortino': '2.1',
    'no_tpy_with_curve|sortino_ratio': '2.1',
    'no_tpy_with_curve|sqn': '2.4',
    'no_tpy_with_curve|total_return': '119.7',
    'no_tpy_with_curve|win_rate': '61.0',
    'over_60|CAR': '30.0',
    'over_60|Goal': '30.0',
    'over_60|calmar': '1.5',
    'over_60|calmar_ratio': '1.5',
    'over_60|car': '30.0',
    'over_60|consistent_annual_return': '30.0',
    'over_60|drawdown': '20.0',
    'over_60|goal': '30.0',
    'over_60|max_dd': '20.0',
    'over_60|max_drawdown': '20.0',
    'over_60|profit_factor': '1.8',
    'over_60|return': '119.7',
    'over_60|sharpe': '1.4',
    'over_60|sharpe_ratio': '1.4',
    'over_60|sortino': '2.1',
    'over_60|sortino_ratio': '2.1',
    'over_60|sqn': '2.4',
    'over_60|total_return': '119.7',
    'over_60|win_rate': '61.0',
    'per_run_thresholds|CAR': '15.0',
    'per_run_thresholds|Goal': '15.0',
    'per_run_thresholds|calmar': '1.5',
    'per_run_thresholds|calmar_ratio': '1.5',
    'per_run_thresholds|car': '15.0',
    'per_run_thresholds|consistent_annual_return': '15.0',
    'per_run_thresholds|drawdown': '20.0',
    'per_run_thresholds|goal': '15.0',
    'per_run_thresholds|max_dd': '20.0',
    'per_run_thresholds|max_drawdown': '20.0',
    'per_run_thresholds|profit_factor': '1.8',
    'per_run_thresholds|return': '119.7',
    'per_run_thresholds|sharpe': '1.4',
    'per_run_thresholds|sharpe_ratio': '1.4',
    'per_run_thresholds|sortino': '2.1',
    'per_run_thresholds|sortino_ratio': '2.1',
    'per_run_thresholds|sqn': '2.4',
    'per_run_thresholds|total_return': '119.7',
    'per_run_thresholds|win_rate': '61.0',
    'plain|CAR': '30.0',
    'plain|Goal': '30.0',
    'plain|calmar': '1.5',
    'plain|calmar_ratio': '1.5',
    'plain|car': '30.0',
    'plain|consistent_annual_return': '30.0',
    'plain|drawdown': '20.0',
    'plain|goal': '30.0',
    'plain|max_dd': '20.0',
    'plain|max_drawdown': '20.0',
    'plain|profit_factor': '1.8',
    'plain|return': '119.7',
    'plain|sharpe': '1.4',
    'plain|sharpe_ratio': '1.4',
    'plain|sortino': '2.1',
    'plain|sortino_ratio': '2.1',
    'plain|sqn': '2.4',
    'plain|total_return': '119.7',
    'plain|win_rate': '61.0',
    'robust_mid_concentration|CAR': '7.3673446010017605',
    'robust_mid_concentration|Goal': '7.3673446010017605',
    'robust_mid_concentration|calmar': '0.368367230050088',
    'robust_mid_concentration|calmar_ratio': '0.368367230050088',
    'robust_mid_concentration|car': '7.3673446010017605',
    'robust_mid_concentration|consistent_annual_return': '7.3673446010017605',
    'robust_mid_concentration|drawdown': '20.0',
    'robust_mid_concentration|goal': '7.3673446010017605',
    'robust_mid_concentration|max_dd': '20.0',
    'robust_mid_concentration|max_drawdown': '20.0',
    'robust_mid_concentration|profit_factor': '0.4420406760601056',
    'robust_mid_concentration|return': '29.395704957997022',
    'robust_mid_concentration|sharpe': '0.3438094147134155',
    'robust_mid_concentration|sharpe_ratio': '0.3438094147134155',
    'robust_mid_concentration|sortino': '0.5157141220701232',
    'robust_mid_concentration|sortino_ratio': '0.5157141220701232',
    'robust_mid_concentration|sqn': '0.5893875680801408',
    'robust_mid_concentration|total_return': '29.395704957997022',
    'robust_mid_concentration|win_rate': '14.980267355370245',
    'robust_on|CAR': '0.0',
    'robust_on|Goal': '0.0',
    'robust_on|calmar': '0.0',
    'robust_on|calmar_ratio': '0.0',
    'robust_on|car': '0.0',
    'robust_on|consistent_annual_return': '0.0',
    'robust_on|drawdown': '20.0',
    'robust_on|goal': '0.0',
    'robust_on|max_dd': '20.0',
    'robust_on|max_drawdown': '20.0',
    'robust_on|profit_factor': '0.0',
    'robust_on|return': '0.0',
    'robust_on|sharpe': '0.0',
    'robust_on|sharpe_ratio': '0.0',
    'robust_on|sortino': '0.0',
    'robust_on|sortino_ratio': '0.0',
    'robust_on|sqn': '0.0',
    'robust_on|total_return': '0.0',
    'robust_on|win_rate': '0.0',
    'stress_mid_concentration|CAR': '0.11538690082780359',
    'stress_mid_concentration|Goal': '0.11538690082780359',
    # RE-FROZEN 2026-09-02 (see the header). The stressed pass now restates calmar from
    # the stressed path's own components -- car 5.774429649249924 / max(|dd 0.0|, 1.0) =
    # 5.774429649249924 -- instead of re-reading the copied 1.5. It goes UP here, and that
    # is the corpus, not the metric: _base() hand-sets calmar_ratio 1.5, a number the _C3
    # curve does not produce, so the stressed (computed) figure is the larger of the two
    # and min() keeps the inner robustness-adjusted value. Ratio checks: 5.774429649249924
    # / 1.5 = 3.8496, and 0.06703110723957173 / 0.017412396888828355 = 3.8496.
    'stress_mid_concentration|calmar': '0.06703110723957173',
    'stress_mid_concentration|calmar_ratio': '0.06703110723957173',
    'stress_mid_concentration|car': '0.11538690082780359',
    'stress_mid_concentration|consistent_annual_return': '0.11538690082780359',
    'stress_mid_concentration|drawdown': '20.0',
    'stress_mid_concentration|goal': '0.11538690082780359',
    'stress_mid_concentration|max_dd': '20.0',
    'stress_mid_concentration|max_drawdown': '20.0',
    'stress_mid_concentration|profit_factor': '0.020894876266594028',
    # RE-FROZEN 2026-09-02: stressed total_return (18.29285463353683) x the same robustness
    # factors the unstressed 119.7 was multiplied by; 0.21234829673919076 / 1.389509271728503
    # = 0.15281 = 18.29285463353683 / 119.7.
    'stress_mid_concentration|return': '0.21234829673919076',
    'stress_mid_concentration|sharpe': '0.016251570429573134',
    'stress_mid_concentration|sharpe_ratio': '0.016251570429573134',
    'stress_mid_concentration|sortino': '0.0243773556443597',
    'stress_mid_concentration|sortino_ratio': '0.0243773556443597',
    'stress_mid_concentration|sqn': '0.02785983502212537',
    'stress_mid_concentration|total_return': '0.21234829673919076',
    'stress_mid_concentration|win_rate': '0.7081041401456865',
    'stress_on_thin|CAR': '-100000000.0',
    'stress_on_thin|Goal': '-100000000.0',
    'stress_on_thin|calmar': '1.5',
    'stress_on_thin|calmar_ratio': '1.5',
    'stress_on_thin|car': '-100000000.0',
    'stress_on_thin|consistent_annual_return': '-100000000.0',
    'stress_on_thin|drawdown': '20.0',
    'stress_on_thin|goal': '-100000000.0',
    'stress_on_thin|max_dd': '20.0',
    'stress_on_thin|max_drawdown': '20.0',
    'stress_on_thin|profit_factor': '1.8',
    # RE-FROZEN 2026-09-02: (final_equity - 100000) / 100000 * 100 on the +40bps path =
    # 5.625799999999988, and min(119.7, 5.6258) = 5.6258. Was the INERT copy of 119.7.
    'stress_on_thin|return': '5.625799999999988',
    'stress_on_thin|sharpe': '1.4',
    'stress_on_thin|sharpe_ratio': '1.4',
    'stress_on_thin|sortino': '2.1',
    'stress_on_thin|sortino_ratio': '2.1',
    'stress_on_thin|sqn': '2.4',
    'stress_on_thin|total_return': '5.625799999999988',
    'stress_on_thin|win_rate': '61.0',
    'stress_on|CAR': '0.9229173980911942',
    'stress_on|Goal': '0.9229173980911942',
    'stress_on|calmar': '1.5',
    'stress_on|calmar_ratio': '1.5',
    'stress_on|car': '0.9229173980911942',
    'stress_on|consistent_annual_return': '0.9229173980911942',
    'stress_on|drawdown': '20.0',
    'stress_on|goal': '0.9229173980911942',
    'stress_on|max_dd': '20.0',
    'stress_on|max_drawdown': '20.0',
    'stress_on|profit_factor': '1.8',
    # RE-FROZEN 2026-09-02: same arithmetic as stress_on_thin above (identical trades and
    # initial capital; the two cases differ only in avg_trades_per_year, which no
    # return-based metric reads).
    'stress_on|return': '5.625799999999988',
    'stress_on|sharpe': '1.4',
    'stress_on|sharpe_ratio': '1.4',
    'stress_on|sortino': '2.1',
    'stress_on|sortino_ratio': '2.1',
    'stress_on|sqn': '2.4',
    'stress_on|total_return': '5.625799999999988',
    'stress_on|win_rate': '61.0',
    'thin_11_9|CAR': '-100000000.0',
    'thin_11_9|Goal': '-100000000.0',
    'thin_11_9|calmar': '1.5',
    'thin_11_9|calmar_ratio': '1.5',
    'thin_11_9|car': '-100000000.0',
    'thin_11_9|consistent_annual_return': '-100000000.0',
    'thin_11_9|drawdown': '20.0',
    'thin_11_9|goal': '-100000000.0',
    'thin_11_9|max_dd': '20.0',
    'thin_11_9|max_drawdown': '20.0',
    'thin_11_9|profit_factor': '1.8',
    'thin_11_9|return': '119.7',
    'thin_11_9|sharpe': '1.4',
    'thin_11_9|sharpe_ratio': '1.4',
    'thin_11_9|sortino': '2.1',
    'thin_11_9|sortino_ratio': '2.1',
    'thin_11_9|sqn': '2.4',
    'thin_11_9|total_return': '119.7',
    'thin_11_9|win_rate': '61.0',
    'thin_15|CAR': '15.0',
    'thin_15|Goal': '15.0',
    'thin_15|calmar': '1.5',
    'thin_15|calmar_ratio': '1.5',
    'thin_15|car': '15.0',
    'thin_15|consistent_annual_return': '15.0',
    'thin_15|drawdown': '20.0',
    'thin_15|goal': '15.0',
    'thin_15|max_dd': '20.0',
    'thin_15|max_drawdown': '20.0',
    'thin_15|profit_factor': '1.8',
    'thin_15|return': '119.7',
    'thin_15|sharpe': '1.4',
    'thin_15|sharpe_ratio': '1.4',
    'thin_15|sortino': '2.1',
    'thin_15|sortino_ratio': '2.1',
    'thin_15|sqn': '2.4',
    'thin_15|total_return': '119.7',
    'thin_15|win_rate': '61.0',
    'tpy_zero|CAR': '-100000000.0',
    'tpy_zero|Goal': '-100000000.0',
    'tpy_zero|calmar': '1.5',
    'tpy_zero|calmar_ratio': '1.5',
    'tpy_zero|car': '-100000000.0',
    'tpy_zero|consistent_annual_return': '-100000000.0',
    'tpy_zero|drawdown': '20.0',
    'tpy_zero|goal': '-100000000.0',
    'tpy_zero|max_dd': '20.0',
    'tpy_zero|max_drawdown': '20.0',
    'tpy_zero|profit_factor': '1.8',
    'tpy_zero|return': '119.7',
    'tpy_zero|sharpe': '1.4',
    'tpy_zero|sharpe_ratio': '1.4',
    'tpy_zero|sortino': '2.1',
    'tpy_zero|sortino_ratio': '2.1',
    'tpy_zero|sqn': '2.4',
    'tpy_zero|total_return': '119.7',
    'tpy_zero|win_rate': '61.0',
    'trade_scale_on_negative|CAR': '-15.0',
    'trade_scale_on_negative|Goal': '-15.0',
    'trade_scale_on_negative|calmar': '0.75',
    'trade_scale_on_negative|calmar_ratio': '0.75',
    'trade_scale_on_negative|car': '-15.0',
    'trade_scale_on_negative|consistent_annual_return': '-15.0',
    'trade_scale_on_negative|drawdown': '20.0',
    'trade_scale_on_negative|goal': '-15.0',
    'trade_scale_on_negative|max_dd': '20.0',
    'trade_scale_on_negative|max_drawdown': '20.0',
    'trade_scale_on_negative|profit_factor': '0.9',
    'trade_scale_on_negative|return': '-40.0',
    'trade_scale_on_negative|sharpe': '0.7',
    'trade_scale_on_negative|sharpe_ratio': '0.7',
    'trade_scale_on_negative|sortino': '1.05',
    'trade_scale_on_negative|sortino_ratio': '1.05',
    'trade_scale_on_negative|sqn': '-1.1',
    'trade_scale_on_negative|total_return': '-40.0',
    'trade_scale_on_negative|win_rate': '30.5',
    'trade_scale_target_50|CAR': '30.0',
    'trade_scale_target_50|Goal': '30.0',
    'trade_scale_target_50|calmar': '3.0',
    'trade_scale_target_50|calmar_ratio': '3.0',
    'trade_scale_target_50|car': '30.0',
    'trade_scale_target_50|consistent_annual_return': '30.0',
    'trade_scale_target_50|drawdown': '20.0',
    'trade_scale_target_50|goal': '30.0',
    'trade_scale_target_50|max_dd': '20.0',
    'trade_scale_target_50|max_drawdown': '20.0',
    'trade_scale_target_50|profit_factor': '3.6',
    'trade_scale_target_50|return': '239.4',
    'trade_scale_target_50|sharpe': '2.8',
    'trade_scale_target_50|sharpe_ratio': '2.8',
    'trade_scale_target_50|sortino': '4.2',
    'trade_scale_target_50|sortino_ratio': '4.2',
    'trade_scale_target_50|sqn': '4.8',
    'trade_scale_target_50|total_return': '239.4',
    'trade_scale_target_50|win_rate': '122.0',
    'trade_scale|CAR': '30.0',
    'trade_scale|Goal': '30.0',
    'trade_scale|calmar': '0.75',
    'trade_scale|calmar_ratio': '0.75',
    'trade_scale|car': '30.0',
    'trade_scale|consistent_annual_return': '30.0',
    'trade_scale|drawdown': '20.0',
    'trade_scale|goal': '30.0',
    'trade_scale|max_dd': '20.0',
    'trade_scale|max_drawdown': '20.0',
    'trade_scale|profit_factor': '0.9',
    'trade_scale|return': '59.85',
    'trade_scale|sharpe': '0.7',
    'trade_scale|sharpe_ratio': '0.7',
    'trade_scale|sortino': '1.05',
    'trade_scale|sortino_ratio': '1.05',
    'trade_scale|sqn': '1.2',
    'trade_scale|total_return': '59.85',
    'trade_scale|win_rate': '30.5',
    'win_rate_factor_low|CAR': '12.0',
    'win_rate_factor_low|Goal': '12.0',
    'win_rate_factor_low|calmar': '0.6000000000000001',
    'win_rate_factor_low|calmar_ratio': '0.6000000000000001',
    'win_rate_factor_low|car': '12.0',
    'win_rate_factor_low|consistent_annual_return': '12.0',
    'win_rate_factor_low|drawdown': '20.0',
    'win_rate_factor_low|goal': '12.0',
    'win_rate_factor_low|max_dd': '20.0',
    'win_rate_factor_low|max_drawdown': '20.0',
    'win_rate_factor_low|profit_factor': '0.7200000000000001',
    'win_rate_factor_low|return': '47.88',
    'win_rate_factor_low|sharpe': '0.5599999999999999',
    'win_rate_factor_low|sharpe_ratio': '0.5599999999999999',
    'win_rate_factor_low|sortino': '0.8400000000000001',
    'win_rate_factor_low|sortino_ratio': '0.8400000000000001',
    'win_rate_factor_low|sqn': '0.96',
    'win_rate_factor_low|total_return': '47.88',
    'win_rate_factor_low|win_rate': '8.0',
    'win_rate_factor_missing_rate|CAR': '30.0',
    'win_rate_factor_missing_rate|Goal': '30.0',
    'win_rate_factor_missing_rate|calmar': '1.5',
    'win_rate_factor_missing_rate|calmar_ratio': '1.5',
    'win_rate_factor_missing_rate|car': '30.0',
    'win_rate_factor_missing_rate|consistent_annual_return': '30.0',
    'win_rate_factor_missing_rate|drawdown': '20.0',
    'win_rate_factor_missing_rate|goal': '30.0',
    'win_rate_factor_missing_rate|max_dd': '20.0',
    'win_rate_factor_missing_rate|max_drawdown': '20.0',
    'win_rate_factor_missing_rate|profit_factor': '1.8',
    'win_rate_factor_missing_rate|return': '119.7',
    'win_rate_factor_missing_rate|sharpe': '1.4',
    'win_rate_factor_missing_rate|sharpe_ratio': '1.4',
    'win_rate_factor_missing_rate|sortino': '2.1',
    'win_rate_factor_missing_rate|sortino_ratio': '2.1',
    'win_rate_factor_missing_rate|sqn': '2.4',
    'win_rate_factor_missing_rate|total_return': '119.7',
    'win_rate_factor_missing_rate|win_rate': '-1000000000.0',
    'win_rate_factor_on_negative|CAR': '-15.0',
    'win_rate_factor_on_negative|Goal': '-15.0',
    'win_rate_factor_on_negative|calmar': '0.6000000000000001',
    'win_rate_factor_on_negative|calmar_ratio': '0.6000000000000001',
    'win_rate_factor_on_negative|car': '-15.0',
    'win_rate_factor_on_negative|consistent_annual_return': '-15.0',
    'win_rate_factor_on_negative|drawdown': '20.0',
    'win_rate_factor_on_negative|goal': '-15.0',
    'win_rate_factor_on_negative|max_dd': '20.0',
    'win_rate_factor_on_negative|max_drawdown': '20.0',
    'win_rate_factor_on_negative|profit_factor': '0.7200000000000001',
    'win_rate_factor_on_negative|return': '-40.0',
    'win_rate_factor_on_negative|sharpe': '0.5599999999999999',
    'win_rate_factor_on_negative|sharpe_ratio': '0.5599999999999999',
    'win_rate_factor_on_negative|sortino': '0.8400000000000001',
    'win_rate_factor_on_negative|sortino_ratio': '0.8400000000000001',
    'win_rate_factor_on_negative|sqn': '0.96',
    'win_rate_factor_on_negative|total_return': '-40.0',
    'win_rate_factor_on_negative|win_rate': '8.0',
    'win_rate_factor|CAR': '36.6',
    'win_rate_factor|Goal': '36.6',
    'win_rate_factor|calmar': '1.83',
    'win_rate_factor|calmar_ratio': '1.83',
    'win_rate_factor|car': '36.6',
    'win_rate_factor|consistent_annual_return': '36.6',
    'win_rate_factor|drawdown': '20.0',
    'win_rate_factor|goal': '36.6',
    'win_rate_factor|max_dd': '20.0',
    'win_rate_factor|max_drawdown': '20.0',
    'win_rate_factor|profit_factor': '2.196',
    'win_rate_factor|return': '146.034',
    'win_rate_factor|sharpe': '1.708',
    'win_rate_factor|sharpe_ratio': '1.708',
    'win_rate_factor|sortino': '2.562',
    'win_rate_factor|sortino_ratio': '2.562',
    'win_rate_factor|sqn': '2.928',
    'win_rate_factor|total_return': '146.034',
    'win_rate_factor|win_rate': '74.42',
    'wiped|CAR': '-2000000000.0',
    'wiped|Goal': '-2000000000.0',
    'wiped|calmar': '-2000000000.0',
    'wiped|calmar_ratio': '-2000000000.0',
    'wiped|car': '-2000000000.0',
    'wiped|consistent_annual_return': '-2000000000.0',
    'wiped|drawdown': '-2000000000.0',
    'wiped|goal': '-2000000000.0',
    'wiped|max_dd': '-2000000000.0',
    'wiped|max_drawdown': '-2000000000.0',
    'wiped|profit_factor': '-2000000000.0',
    'wiped|return': '-2000000000.0',
    'wiped|sharpe': '-2000000000.0',
    'wiped|sharpe_ratio': '-2000000000.0',
    'wiped|sortino': '-2000000000.0',
    'wiped|sortino_ratio': '-2000000000.0',
    'wiped|sqn': '-2000000000.0',
    'wiped|total_return': '-2000000000.0',
    'wiped|win_rate': '-2000000000.0',
    'zero_trades|CAR': '-1000000000.0',
    'zero_trades|Goal': '-1000000000.0',
    'zero_trades|calmar': '-1000000000.0',
    'zero_trades|calmar_ratio': '-1000000000.0',
    'zero_trades|car': '-1000000000.0',
    'zero_trades|consistent_annual_return': '-1000000000.0',
    'zero_trades|drawdown': '-1000000000.0',
    'zero_trades|goal': '-1000000000.0',
    'zero_trades|max_dd': '-1000000000.0',
    'zero_trades|max_drawdown': '-1000000000.0',
    'zero_trades|profit_factor': '-1000000000.0',
    'zero_trades|return': '-1000000000.0',
    'zero_trades|sharpe': '-1000000000.0',
    'zero_trades|sharpe_ratio': '-1000000000.0',
    'zero_trades|sortino': '-1000000000.0',
    'zero_trades|sortino_ratio': '-1000000000.0',
    'zero_trades|sqn': '-1000000000.0',
    'zero_trades|total_return': '-1000000000.0',
    'zero_trades|win_rate': '-1000000000.0',
}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_equity_fitness_is_bit_identical_to_the_frozen_baseline(case):
    cname, metric = case.split("|")
    expected = GOLDEN[case]
    try:
        got = repr(compute_fitness(metric, dict(CORPUS[cname])))
    except Exception as e:  # noqa: BLE001 -- a raise is itself part of the frozen contract
        got = f"RAISES:{type(e).__name__}"
    assert got == expected, (
        f"EQUITY FITNESS MOVED for {cname}/{metric}: {expected} -> {got}. "
        f"Non-option grids are scored by this path; a change here silently re-ranks a run "
        f"already in progress."
    )


def test_the_corpus_actually_exercises_every_equity_metric():
    """Guard against the freeze quietly covering less than it claims.

    Without this, deleting entries from GOLDEN (or from METRICS) would make the parametrized
    test pass by testing nothing -- the classic way a regression net rots.
    """
    covered = {c.split("|")[1] for c in GOLDEN}
    assert covered == set(METRICS)
    assert {c.split("|")[0] for c in GOLDEN} == set(CORPUS)
    assert len(GOLDEN) == len(CORPUS) * len(METRICS)


def test_the_baseline_contains_real_numbers_not_only_sentinels():
    """A freeze made entirely of sentinels/errors would pass while proving nothing about the
    arithmetic. Require a healthy majority of ordinary finite scores."""
    finite = 0
    for v in GOLDEN.values():
        if v.startswith("RAISES:"):
            continue
        f = float(v)
        if math.isfinite(f) and abs(f) < 1e6:
            finite += 1
    assert finite > len(GOLDEN) * 0.6, f"only {finite}/{len(GOLDEN)} frozen values are real scores"


def test_the_corpus_covers_the_branches_that_are_easy_to_miss():
    """Named coverage for the specific conditionals a blanket corpus tends to skip -- each of
    these was chosen because a mutation of its branch would otherwise survive."""
    required = {
        "adjusted_present_no_cap",       # the cap switch, OFF side
        "cap_with_adjusted_missing",     # the cap switch fallback when no adjusted key exists
        "cap_with_adjusted_none",        # ...and when it exists but is None
        "trade_scale_target_50",         # target and cap clamps exercised independently
        "trade_scale_on_negative",       # the "positive fitness only" guard
        "win_rate_factor_missing_rate",  # the factor's own missing-value fallback
        "win_rate_factor_on_negative",   # the factor's sign guard
        "tpy_zero",
        "stress_on_thin",                # a sentinel must survive the stress re-score
        "dd_none",
    }
    assert required <= set(CORPUS)


# Frozen robustness COMPONENTS. Kept separate from GOLDEN because the three factors
# multiply into the fitness, so a case with conc_factor 0.0 hides every Monte-Carlo
# constant behind it -- measured: the MC seed, path count and negative-tail demerit
# could all be changed without the composed-fitness freeze noticing.
GOLDEN_ROBUSTNESS = {
    'empty|0.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
    'empty|40.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
    'mid_concentration|0.0': {'conc_factor': '0.24557815336672534', 'mc_factor': '1.0', 'mc_p5': '11.987549279574239', 'mc_prob_neg': '0.0', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': '23.529411764705884', 'top5_pct': '76.47058823529412'},
    'mid_concentration|40.0': {'conc_factor': '0.24557815336672534', 'mc_factor': '1.0', 'mc_p5': '11.987549279574239', 'mc_prob_neg': '0.0', 'spread_factor': '0.1924809883083308', 'spread_keep_pct': '19.24809883083308', 'top1_pct': '23.529411764705884', 'top5_pct': '76.47058823529412'},
    'mixed_monte_carlo|0.0': {'conc_factor': '0.0', 'mc_factor': '0.15500000000000003', 'mc_p5': '-16.653859422059547', 'mc_prob_neg': '0.345', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': '100.0', 'top5_pct': '287.5'},
    'mixed_monte_carlo|40.0': {'conc_factor': '0.0', 'mc_factor': '0.15500000000000003', 'mc_p5': '-16.653859422059547', 'mc_prob_neg': '0.345', 'spread_factor': '0.07573421537278024', 'spread_keep_pct': '7.5734215372780245', 'top1_pct': '100.0', 'top5_pct': '287.5'},
    'monte_carlo_ruined|0.0': {'conc_factor': '0.0', 'mc_factor': '0.0', 'mc_p5': '-57.56716277644737', 'mc_prob_neg': '0.518', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': '230.76923076923077', 'top5_pct': '592.3076923076923'},
    'monte_carlo_ruined|40.0': {'conc_factor': '0.0', 'mc_factor': '0.0', 'mc_p5': '-57.56716277644737', 'mc_prob_neg': '0.518', 'spread_factor': '0.007361965548700796', 'spread_keep_pct': '0.7361965548700796', 'top1_pct': '230.76923076923077', 'top5_pct': '592.3076923076923'},
    'net_negative|0.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
    'net_negative|40.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
    'single_trade|0.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
    'single_trade|40.0': {'conc_factor': '1.0', 'mc_factor': '1.0', 'mc_p5': 'None', 'mc_prob_neg': 'None', 'spread_factor': '1.0', 'spread_keep_pct': 'None', 'top1_pct': 'None', 'top5_pct': 'None'},
}


def _rob_results(trades):
    return {"trades": trades, "initial_capital": 100_000.0, "annualized_return": 30.0,
            "equity_curve": [{"date": "2020-01-02", "equity": 100_000.0},
                             {"date": "2022-12-30", "equity": 219_700.0}]}


@pytest.mark.parametrize("case", sorted(GOLDEN_ROBUSTNESS))
def test_robustness_components_are_bit_identical_to_the_frozen_baseline(case):
    rname, spread = case.split("|")
    expected = GOLDEN_ROBUSTNESS[case]
    try:
        comp = robustness_metrics(_rob_results(ROBUSTNESS_CORPUS[rname]), float(spread))
        got = {k: repr(v) for k, v in sorted(comp.items())}
    except Exception as e:  # noqa: BLE001
        got = f"RAISES:{type(e).__name__}"
    assert got == expected, f"ROBUSTNESS COMPONENT MOVED for {rname} @ {spread}bps"


def test_the_robustness_corpus_reaches_every_region_of_the_shape():
    """Each factor must be non-trivial SOMEWHERE, or the freeze above is decorative."""
    seen_conc, seen_mc = set(), set()
    for trades in ROBUSTNESS_CORPUS.values():
        c = robustness_metrics(_rob_results(trades))
        seen_conc.add(round(c["conc_factor"], 4))
        seen_mc.add(round(c["mc_factor"], 4))
    # a strictly-interior concentration factor (not just 0.0 / 1.0)
    assert any(0.0 < v < 1.0 for v in seen_conc), seen_conc
    # a strictly-interior MC factor, plus both endpoints
    assert any(0.0 < v < 1.0 for v in seen_mc), seen_mc
    assert 0.0 in seen_mc and 1.0 in seen_mc
