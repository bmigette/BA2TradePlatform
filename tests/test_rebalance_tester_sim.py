"""The allocator's rebalance tester: the pure arithmetic (``ui/utils/rebalance_tester_sim.py``)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_trade_platform.ui.utils import rebalance_tester_sim as sim
from ba2_trade_platform.ui.utils.label_growth_sim import DistributionHistory

AS_OF = date(2024, 12, 31)
DAYS = pd.bdate_range('2023-06-01', '2024-12-31')


def _series(days, value_at):
    return {d.date().isoformat(): float(value_at(d)) for d in days}


def _flat(price=100.0, days=DAYS):
    return _series(days, lambda d: price)


def _a_double_then_back(days=DAYS):
    """A: 100, doubles on 2024-01-31 (a month end), back to 100 on 2024-02-15, flat after."""
    def px(d):
        iso = d.date().isoformat()
        return 200.0 if '2024-01-31' <= iso < '2024-02-15' else 100.0
    return _series(days, px)


def _no_payouts(*symbols):
    return {s: DistributionHistory(events=()) for s in symbols}


def _run(weights, closes, distributions=None, **params):
    params.setdefault('lookback_years', 1)
    params.setdefault('cost_bps', 0.0)
    return sim.run_rebalance_test(weights, closes,
                                  distributions if distributions is not None else _no_payouts(*closes),
                                  sim.RebalanceParams(**params), as_of=AS_OF)


def _variant(result, key):
    return next(v for v in result.variants if v.key == key)


TWO = {'A': _a_double_then_back(DAYS[DAYS >= '2024-01-02']), 'B': _flat(days=DAYS[DAYS >= '2024-01-02'])}


# ---------------------------------------------------------------------------
# hold
# ---------------------------------------------------------------------------

def test_hold_never_trades_and_is_the_weighted_buy_and_hold():
    closes = {'A': _series(DAYS, lambda d: 100 + (d - DAYS[0]).days * 0.1), 'B': _flat()}
    res = _run({'A': 60, 'B': 40}, closes, cost_bps=50.0)
    hold = _variant(res, sim.HOLD)
    assert hold.turnover_pct_per_year == 0.0 and hold.rebalances == 0 and hold.offsets == 1
    frame = pd.DataFrame({s: pd.Series(c) for s, c in closes.items()})
    frame = frame[frame.index >= res.window.start]
    expected = 0.6 * frame['A'].iloc[-1] / frame['A'].iloc[0] + 0.4 * frame['B'].iloc[-1] / frame['B'].iloc[0]
    assert hold.total_return_pct == pytest.approx((expected - 1) * 100)
    assert res.curves[sim.HOLD][-1] == pytest.approx(expected * 100)


def test_simulate_variant_hold_equity_has_no_trades():
    prices = pd.DataFrame({'X': [1.0, 2.0, 3.0], 'Y': [1.0, 1.0, 0.5]},
                          index=pd.to_datetime(['2024-01-30', '2024-01-31', '2024-02-01']))
    eq, turnover, count = sim.simulate_variant(prices, np.array([0.5, 0.5]), None, cost_bps=100)
    assert list(eq) == pytest.approx([1.0, 1.5, 1.75])
    assert turnover == 0.0 and count == 0


# ---------------------------------------------------------------------------
# a hand-computed two-asset rebalance
# ---------------------------------------------------------------------------

def test_two_assets_monthly_rebalance_by_hand():
    """50/50. A doubles into the January month-end: 1.0 + 0.5 = 1.5, rebalanced to 0.75 / 0.75. A halves on
    2024-02-15: 0.375 + 0.75 = 1.125. February's month end trades back to 0.5625 each; nothing moves after. Hold ends
    at 1.0 (A back where it started)."""
    res = _run({'A': 50, 'B': 50}, TWO)
    assert res.refusal is None
    assert res.window.start == '2024-01-02' and res.window.end == '2024-12-31'
    hold, monthly = _variant(res, sim.HOLD), _variant(res, 1)
    assert hold.total_return_pct == pytest.approx(0.0)
    assert monthly.total_return_pct == pytest.approx(12.5)
    assert monthly.d_total_pct == pytest.approx(12.5)
    # turnover: 0.5 / 1.5 in January and 0.375 / 1.125 in February, then nothing; per year of a 364-day window
    years = 364 / 365.25
    assert monthly.turnover_pct_per_year == pytest.approx((1 / 3 + 1 / 3) / years * 100)
    assert monthly.rebalances == 11                          # Jan..Nov month ends (December's is the last row)
    # drawdown: hold falls from 1.5 to 1.0 (-33.3%), monthly from 1.5 to 1.125 (-25%)
    assert hold.max_drawdown_pct == pytest.approx(-100 / 3)
    assert monthly.max_drawdown_pct == pytest.approx(-25.0)
    assert monthly.d_drawdown_pct == pytest.approx(-25.0 + 100 / 3)       # positive = shallower


def test_costs_reduce_equity_by_the_traded_value():
    free = _variant(_run({'A': 50, 'B': 50}, TWO, cost_bps=0.0), 1)
    paid = _variant(_run({'A': 50, 'B': 50}, TWO, cost_bps=10.0), 1)
    assert paid.total_return_pct < free.total_return_pct
    # by hand: January trades 0.5 of 1.5 -> 1.5 - 0.0005; A halves; February trades back with its own cost
    c = 0.001
    after_jan = 1.5 - c * 0.5
    a, b = after_jan / 2, after_jan / 2
    a = a / 2
    tot = a + b
    traded = abs(tot / 2 - a) + abs(tot / 2 - b)
    final = tot - c * traded
    assert paid.total_return_pct == pytest.approx((final - 1) * 100)
    hold_free = _variant(_run({'A': 50, 'B': 50}, TWO, cost_bps=0.0), sim.HOLD)
    hold_paid = _variant(_run({'A': 50, 'B': 50}, TWO, cost_bps=10.0), sim.HOLD)
    assert hold_paid.total_return_pct == hold_free.total_return_pct           # hold never pays


# ---------------------------------------------------------------------------
# tilt
# ---------------------------------------------------------------------------

def test_tilt_direction_on_the_weights():
    target = np.array([0.5, 0.5])
    up_flat = np.array([1.0, 0.0])
    assert sim.tilt_weights(target, up_flat, 0.0, 0.0) == pytest.approx([0.5, 0.5])
    assert sim.tilt_weights(target, up_flat, 0.5, 0.0) == pytest.approx([0.6, 0.4])      # follows the winner
    assert sim.tilt_weights(target, up_flat, -0.5, 0.0)[0] < 0.5                          # leans against it
    down_flat = np.array([-0.5, 0.0])
    assert sim.tilt_weights(target, down_flat, 0.0, 1.0)[0] < 0.5                          # less to the loser
    assert sim.tilt_weights(target, down_flat, 0.0, -1.0)[0] > 0.5                         # more to the loser
    assert sim.tilt_weights(target, down_flat, 5.0, 0.0) == pytest.approx([0.5, 0.5])     # winners' k: losers untouched
    # the factor is clipped to 0.25 .. 3
    assert sim.tilt_weights(target, np.array([10.0, 0.0]), 1.0, 0.0) == pytest.approx([0.75, 0.25])
    assert sim.tilt_weights(target, np.array([-0.9, 0.0]), 0.0, 10.0) == pytest.approx([0.2, 0.8])


def test_tilt_toward_the_winner_by_hand():
    """k_winners = 0.5: January's rebalance puts 1.5 * 0.6 = 0.9 in A and 0.6 in B; A halves -> 1.05."""
    res = _run({'A': 50, 'B': 50}, TWO, k_winners=0.5)
    assert _variant(res, 1).total_return_pct == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# offsets
# ---------------------------------------------------------------------------

def test_every_start_offset_is_run_and_its_range_reported():
    """Every 3 months: offset 0 rebalances at the end of March (nothing has drifted: 0%), offset 1 at the end of
    January (12.5%, like monthly), offset 2 at the end of February (A already back: 0%)."""
    res = _run({'A': 50, 'B': 50}, TWO)
    q = _variant(res, 3)
    assert q.offsets == 3
    assert q.total_return_pct == pytest.approx(12.5 / 3)
    years = 364 / 365.25
    assert q.annualised_min_pct == pytest.approx(0.0, abs=1e-9)
    assert q.annualised_max_pct == pytest.approx((1.125 ** (1 / years) - 1) * 100)
    assert res.curves[3][-1] == pytest.approx(100.0)                # the chart draws offset 0
    assert _variant(res, 12).offsets == 12 and _variant(res, 6).offsets == 6 and _variant(res, 1).offsets == 1
    assert 'starts' in sim.table_rows(res)[2]['range'] and sim.table_rows(res)[1]['range'] == '—'


def test_offset_out_of_range_is_refused():
    prices = pd.DataFrame({'X': [1.0, 1.0]}, index=pd.to_datetime(['2024-01-30', '2024-01-31']))
    with pytest.raises(sim.RebalanceInputError):
        sim.simulate_variant(prices, np.array([1.0]), 3, offset=3)


# ---------------------------------------------------------------------------
# total return
# ---------------------------------------------------------------------------

def test_distributions_are_reinvested():
    closes = {'A': _flat(), 'B': _flat()}
    payouts = {'A': DistributionHistory(events=(('2024-06-03', 2.0),)), 'B': DistributionHistory(events=())}
    res = _run({'A': 50, 'B': 50}, closes, payouts)
    assert _variant(res, sim.HOLD).total_return_pct == pytest.approx(1.0)      # 50% of a 2% payout
    levels, unknown = sim.total_return_levels([('2024-01-02', 10.0), ('2024-01-05', 9.0)],
                                              (('2024-01-04', 1.0),))          # ex-date between bars: next bar
    assert list(levels) == pytest.approx([10.0, 10.0]) and unknown == []


def test_a_failed_or_unknown_distribution_inside_the_window_refuses():
    closes = {'A': _flat(), 'B': _flat()}
    failed = {'A': DistributionHistory(events=None, error='HTTP 503'), 'B': DistributionHistory(events=())}
    res = _run({'A': 50, 'B': 50}, closes, failed)
    assert res.variants == [] and 'A: the distribution history could not be fetched (HTTP 503)' in res.refusal
    unknown = {'A': DistributionHistory(events=(('2024-06-03', None),)), 'B': DistributionHistory(events=())}
    res = _run({'A': 50, 'B': 50}, closes, unknown)
    assert res.variants == [] and 'A (2024-06-03)' in res.refusal
    # an unknown amount BEFORE the window does not matter
    old = {'A': DistributionHistory(events=(('2023-08-01', None),)), 'B': DistributionHistory(events=())}
    assert _run({'A': 50, 'B': 50}, closes, old).refusal is None


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

def test_a_late_listed_member_is_excluded_and_named():
    late = DAYS[DAYS >= '2024-06-03']
    closes = {'A': _flat(), 'B': _flat(), 'C': _flat(days=late)}
    res = _run({'A': 35, 'B': 35, 'C': 30}, closes)
    w = res.window
    assert res.refusal is None
    assert w.start == '2024-01-01' and w.lookback_start == '2023-12-31'
    assert w.members == ['A', 'B'] and w.late == [('C', '2024-06-03')]
    assert w.coverage_pct == pytest.approx(70.0) and w.candidates == 3
    assert w.weights == pytest.approx({'A': 0.5, 'B': 0.5})
    assert any('C (first price 2024-06-03)' in n for n in res.notes)
    assert '2 of 3 member(s)' in sim.window_text(w) and '70.0%' in sim.window_text(w)


def test_the_window_moves_to_where_listed_members_cover_60_percent():
    late = DAYS[DAYS >= '2024-06-03']
    closes = {'A': _flat(), 'B': _flat(), 'C': _flat(days=late)}
    res = _run({'A': 25, 'B': 25, 'C': 50}, closes)
    assert res.refusal is None
    assert res.window.start == '2024-06-03' and res.window.members == ['A', 'B', 'C']
    assert res.window.coverage_pct == pytest.approx(100.0)


def test_lookback_caps_the_window():
    closes = {'A': _flat(days=pd.bdate_range('2019-01-01', '2024-12-31')),
              'B': _flat(days=pd.bdate_range('2019-01-01', '2024-12-31'))}
    for years, start in ((1, '2024-01-01'), (3, '2021-12-31'), (5, '2019-12-31')):
        res = _run({'A': 1, 'B': 1}, closes, lookback_years=years)
        assert res.window.start == start, years


def test_zero_weight_and_no_history_members_are_out_and_said():
    closes = {'A': _flat(), 'B': _flat()}
    res = _run({'A': 40, 'B': 40, 'Z': 0, 'GONE': 20}, closes)
    assert res.refusal is None
    assert res.window.zero_weight == ['Z'] and res.window.no_history == ['GONE']
    assert res.window.coverage_pct == pytest.approx(80.0)
    assert any('GONE' in n for n in res.notes) and any('Z' in n for n in res.notes)


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def test_fewer_than_two_members_is_refused():
    res = _run({'A': 70, 'C': 30}, {'A': _flat(), 'C': _flat(days=DAYS[DAYS >= '2024-06-03'])})
    assert res.variants == [] and 'at least 2' in res.refusal
    assert res.window.members == ['A']
    assert any('C (first price 2024-06-03)' in n for n in res.notes)          # named on a refusal too


def test_a_window_under_six_months_is_refused():
    late = DAYS[DAYS >= '2024-09-02']
    res = _run({'A': 25, 'B': 25, 'C': 50}, {'A': _flat(), 'B': _flat(), 'C': _flat(days=late)})
    assert res.variants == [] and 'shorter than about 6 months' in res.refusal
    assert res.window.start == '2024-09-02'


def test_coverage_never_reached_is_refused():
    res = _run({'A': 30, 'GONE': 70}, {'A': _flat()})
    assert res.variants == [] and '60%' in res.refusal


def test_bad_parameters_raise():
    closes = {'A': _flat(), 'B': _flat()}
    for bad in (dict(lookback_years=2), dict(cost_bps=-1.0), dict(cost_bps=None), dict(k_winners=float('nan')),
                dict(k_losers=None)):
        with pytest.raises(sim.RebalanceInputError):
            _run({'A': 1, 'B': 1}, closes, **bad)
    with pytest.raises(sim.RebalanceInputError):
        _run({'A': -1, 'B': 1}, closes)


# ---------------------------------------------------------------------------
# scopes and display
# ---------------------------------------------------------------------------

def test_account_weights_are_label_target_times_in_label_share():
    weights, skipped = sim.account_weights(
        {'Growth': 60.0, 'Income': 40.0, 'Empty': 10.0, 'Off': 0.0},
        {'Growth': {'AAPL': 30.0, 'MSFT': 30.0}, 'Income': {'JEPI': 75.0, 'MSFT': 25.0, 'Z': 0.0},
         'Empty': {'X': 0.0}, 'Off': {'Y': 100.0}})
    assert weights == pytest.approx({'AAPL': 30.0, 'MSFT': 30.0 + 10.0, 'JEPI': 30.0})
    assert skipped == ['Empty']


def test_chart_and_table_carry_every_variant():
    res = _run({'A': 50, 'B': 50}, TWO)
    opts = sim.chart_options(res)
    assert [s['name'] for s in opts['series']] == [sim.VARIANT_NAMES[k] for k in (sim.HOLD, 1, 3, 6, 12)]
    assert opts['series'][0]['lineStyle']['type'] == 'dashed'
    assert len(opts['xAxis']['data']) == len(opts['series'][0]['data'])
    rows = sim.table_rows(res)
    assert [r['variant'] for r in rows] == [sim.VARIANT_NAMES[k] for k in (sim.HOLD, 1, 3, 6, 12)]
    assert rows[0]['d_total'] == '—' and rows[1]['d_total'] == '+12.50 pp'


# ---------------------------------------------------------------------------
# review 2026-11-13
# ---------------------------------------------------------------------------

def test_two_payouts_on_one_ex_date_with_one_unknown_refuse_instead_of_crashing():
    """``sorted`` on (date, amount) compared None with a float and raised TypeError."""
    levels, unknown = sim.total_return_levels([('2024-01-02', 10.0), ('2024-01-03', 10.0)],
                                              (('2024-01-03', 1.0), ('2024-01-03', None)))
    assert unknown == ['2024-01-03'] and list(levels) == pytest.approx([10.0, 11.0])
    closes = {'A': _flat(), 'B': _flat()}
    payouts = {'A': DistributionHistory(events=(('2024-06-03', 1.0), ('2024-06-03', None))),
               'B': DistributionHistory(events=())}
    res = _run({'A': 50, 'B': 50}, closes, payouts)
    assert res.variants == []
    assert 'has no split-adjusted amount' in res.refusal and 'A (2024-06-03)' in res.refusal


def test_unusable_closes_are_counted_and_named_not_dropped_silently():
    closes = {'A': dict(_flat(), **{'2024-03-01': float('nan'), '2024-03-04': 0.0, '2024-03-05': -1.0}),
              'B': dict(_flat(), **{'2024-07-01': None})}
    res = _run({'A': 50, 'B': 50}, closes)
    assert res.refusal is None
    assert res.window.dropped_bars == {'A': 3, 'B': 1}
    assert any(n.startswith('A: 3 bar(s) with no usable close') for n in res.notes)
    assert any(n.startswith('B: 1 bar(s) with no usable close') for n in res.notes)


def test_a_frequency_that_never_rebalances_is_said_in_the_table_and_the_notes():
    # The 1-year window: Every 12 months at offset 0 (the chart) would rebalance at the 12th month-end, which is the
    # window's last row -- so its charted line is hold's, while other start months do rebalance.
    res = _run({'A': 50, 'B': 50}, TWO)
    yearly = _variant(res, 12)
    assert yearly.charted_offset_without_rebalance and 0 < yearly.offsets_without_rebalance < yearly.offsets
    assert any(n.startswith('Every 12 months: the charted start (offset 0) has no rebalance') for n in res.notes)
    row = next(r for r in sim.table_rows(res) if r['variant'] == 'Every 12 months')
    assert f"(none at {yearly.offsets_without_rebalance} of 12 starts)" in row['rebalances']
    # A frame inside one month has no month-end at all: every frequency IS hold, and says so.
    prices = pd.DataFrame({'X': [1.0, 2.0, 1.5], 'Y': [1.0, 1.0, 1.0]},
                          index=pd.to_datetime(['2024-03-04', '2024-03-05', '2024-03-06']))
    rows, _curves = sim.simulate_frame(prices, {'X': 1, 'Y': 1}, sim.RebalanceParams(cost_bps=0.0))
    result = sim.RebalanceTestResult(params=sim.RebalanceParams(), window=None, variants=rows)
    for r in sim.table_rows(result)[1:]:
        assert r['rebalances'] == 'none in this window (= hold)'
    assert all(n.endswith('so every figure equals hold.') for n in sim._rebalance_count_notes(rows))


def test_tilt_at_the_second_rebalance_uses_the_return_since_the_previous_one():
    """k_winners = 1, monthly. A: 100 -> 200 (31 Jan) -> 300 (15 Feb) -> 600 (15 Mar), B flat.
    31 Jan: r_A = +1 -> factor 2 -> 2/3 of 1.5 in A: A 1.0, B 0.5. 15 Feb: A 1.5, total 2.0.
    29 Feb: r_A since 31 Jan = +0.5 (NOT +2 since the start) -> factor 1.5 -> A 0.6 of 2.0 = 1.2, B 0.8.
    15 Mar: A doubles -> 2.4 + 0.8 = 3.2. (Measured since the start, factor 3 -> 0.75: 3.0 + 0.5 = 3.5.)"""
    days = DAYS[DAYS >= '2024-01-02']

    def a(d):
        iso = d.date().isoformat()
        return 100.0 if iso < '2024-01-31' else 200.0 if iso < '2024-02-15' else 300.0 if iso < '2024-03-15' \
            else 600.0
    res = _run({'A': 50, 'B': 50}, {'A': _series(days, a), 'B': _flat(days=days)}, k_winners=1.0)
    assert _variant(res, 1).total_return_pct == pytest.approx(220.0)


def test_no_look_ahead_a_prefix_run_matches_the_full_run_on_the_shared_rows():
    """Every decision uses rows up to its own: running on a prefix gives the same equity on every shared row except the
    prefix's last (which is never a month-end there, as its month is not over)."""
    rng = np.random.default_rng(7)
    idx = pd.bdate_range('2022-01-03', '2024-06-28')
    prices = pd.DataFrame(np.exp(np.cumsum(rng.normal(0, 0.02, size=(len(idx), 3)), axis=0)) * 100,
                          index=idx, columns=['X', 'Y', 'Z'])
    target = np.array([0.5, 0.3, 0.2])
    for months, offset in ((1, 0), (3, 1), (6, 5), (12, 4)):
        kw = dict(cost_bps=15.0, k_winners=0.7, k_losers=-0.4)
        full, _t, _n = sim.simulate_variant(prices, target, months, offset, **kw)
        for cut in (100, 333, 517):
            part, _t2, _n2 = sim.simulate_variant(prices.iloc[:cut], target, months, offset, **kw)
            assert part[:cut - 1] == pytest.approx(full[:cut - 1], rel=1e-12), (months, offset, cut)


def test_a_distribution_on_the_first_window_day_is_excluded():
    """The first row is the base: a payout that lands on it is in the base price, not in any return of the window."""
    closes = {'A': _flat(), 'B': _flat()}
    res = _run({'A': 50, 'B': 50}, closes, {'A': DistributionHistory(events=(('2024-01-01', 5.0),)),
                                           'B': DistributionHistory(events=())})
    assert res.window.start == '2024-01-01'
    assert _variant(res, sim.HOLD).total_return_pct == pytest.approx(0.0)
    # ...and an UNKNOWN amount on that day does not refuse either: it is outside the window's returns
    res = _run({'A': 50, 'B': 50}, closes, {'A': DistributionHistory(events=(('2024-01-01', None),)),
                                           'B': DistributionHistory(events=())})
    assert res.refusal is None
