"""The label growth simulator's arithmetic (``ui/utils/label_growth_sim.py``). Pure: no UI, no IO.

"If I had put $X into this label at the start of the period, split by its CURRENT shares, what would it be worth
today?" -- buy and hold, no rebalancing, two answers per horizon: price only, and price plus the distributions the
shares received (as cash, not reinvested).
"""
import math
from datetime import date

import pytest

from ba2_trade_platform.ui.utils import label_growth_sim as sim
from ba2_trade_platform.ui.utils.label_growth_sim import (
    DEFAULT_AMOUNT, HORIZONS, STATUS_INVESTED, STATUS_LATE, STATUS_NO_HISTORY, STATUS_ZERO_SHARE,
    DistributionHistory, growth_chart_options, history_start, horizon_start, simulate_label_growth,
)

AS_OF = date(2026, 11, 12)


def _none():
    """A symbol that genuinely paid nothing (fetched, empty)."""
    return DistributionHistory(events=())


def _paid(*events):
    return DistributionHistory(events=tuple(events))


def _run(shares, closes, distributions=None, *, amount=10_000.0, horizons=HORIZONS):
    if distributions is None:
        distributions = {s: _none() for s in shares}
    return simulate_label_growth(amount, shares, closes, distributions, as_of=AS_OF, horizons=horizons)


# ---------------------------------------------------------------------------
# horizon start dates
# ---------------------------------------------------------------------------

def test_the_defaults_are_ten_thousand_and_the_four_horizons():
    assert DEFAULT_AMOUNT == 10_000.0
    assert HORIZONS == ('ytd', '1y', '3y', '5y')


def test_horizon_starts():
    assert horizon_start('ytd', AS_OF) == date(2025, 12, 31)
    assert horizon_start('1y', AS_OF) == date(2025, 11, 12)
    assert horizon_start('3y', AS_OF) == date(2023, 11, 12)
    assert horizon_start('5y', AS_OF) == date(2021, 11, 12)


def test_a_29_february_folds_to_28():
    assert horizon_start('1y', date(2028, 2, 29)) == date(2027, 2, 28)


def test_an_unknown_horizon_is_refused():
    with pytest.raises(ValueError):
        horizon_start('2y', AS_OF)


def test_the_horizon_starts_agree_with_the_symbol_info_panel():
    """The YTD / 1y / 3y / 5y the info panel shows for one symbol and the ones simulated here are the SAME windows."""
    from ba2_providers.symbol_info import window_start_date
    for as_of in (AS_OF, date(2028, 2, 29), date(2027, 1, 2)):
        for h in HORIZONS:
            assert horizon_start(h, as_of) == window_start_date(h, as_of)


def test_history_is_fetched_from_before_the_longest_horizon():
    start = history_start(AS_OF)
    assert start < horizon_start('5y', AS_OF)
    assert (horizon_start('5y', AS_OF) - start).days == sim.FETCH_BUFFER_DAYS


# ---------------------------------------------------------------------------
# the YTD base and plain buy-and-hold
# ---------------------------------------------------------------------------

def test_ytd_buys_at_the_last_close_of_the_previous_year_not_the_first_of_january():
    closes = {'AAA': {'2025-12-30': 100.0, '2025-12-31': 110.0, '2026-01-02': 120.0, '2026-11-11': 165.0}}
    h = _run({'AAA': 100.0}, closes, horizons=('ytd',)).horizons['ytd']
    row = h.rows[0]
    assert row.buy_date == '2025-12-31'
    assert row.buy_price == 110.0
    assert row.status == STATUS_INVESTED
    assert h.price_value == pytest.approx(15_000.0)
    assert h.price_gain == pytest.approx(5_000.0)
    assert h.price_gain_pct == pytest.approx(50.0)
    assert h.end == '2026-11-11'


def test_when_the_31st_has_no_bar_the_last_earlier_close_is_the_base():
    closes = {'AAA': {'2025-12-30': 100.0, '2026-01-02': 120.0, '2026-11-11': 150.0}}
    row = _run({'AAA': 100.0}, closes, horizons=('ytd',)).horizons['ytd'].rows[0]
    assert (row.buy_date, row.buy_price) == ('2025-12-30', 100.0)


def test_the_split_follows_the_current_shares_without_rebalancing():
    closes = {'AAA': {'2025-11-12': 100.0, '2026-11-11': 200.0},     # doubles
              'BBB': {'2025-11-12': 50.0, '2026-11-11': 25.0}}       # halves
    h = _run({'AAA': 60.0, 'BBB': 40.0}, closes, horizons=('1y',)).horizons['1y']
    by = {r.symbol: r for r in h.rows}
    assert by['AAA'].invested == pytest.approx(6_000.0)
    assert by['AAA'].quantity == pytest.approx(60.0)
    assert by['AAA'].price_value == pytest.approx(12_000.0)
    assert by['BBB'].invested == pytest.approx(4_000.0)
    assert by['BBB'].price_value == pytest.approx(2_000.0)
    assert h.price_value == pytest.approx(14_000.0)
    assert h.price_gain_pct == pytest.approx(40.0)


def test_bars_after_as_of_are_ignored():
    closes = {'AAA': {'2025-11-12': 100.0, '2026-11-11': 200.0, '2026-11-13': 999.0}}
    h = _run({'AAA': 100.0}, closes, horizons=('1y',)).horizons['1y']
    assert h.rows[0].last_date == '2026-11-11'
    assert h.price_value == pytest.approx(20_000.0)


def test_each_horizon_starts_from_its_own_base():
    closes = {'AAA': {'2021-11-12': 50.0, '2023-11-10': 80.0, '2025-11-12': 100.0, '2025-12-31': 125.0,
                      '2026-11-11': 150.0}}
    res = _run({'AAA': 100.0}, closes)
    assert res.horizons['5y'].rows[0].buy_date == '2021-11-12'
    assert res.horizons['3y'].rows[0].buy_date == '2023-11-10'      # 12 Nov 2023 was a Sunday: last close before
    assert res.horizons['1y'].rows[0].buy_date == '2025-11-12'
    assert res.horizons['ytd'].rows[0].buy_date == '2025-12-31'
    assert res.horizons['5y'].price_value == pytest.approx(30_000.0)
    assert res.horizons['ytd'].price_value == pytest.approx(12_000.0)


# ---------------------------------------------------------------------------
# late starters and symbols with no history
# ---------------------------------------------------------------------------

def test_a_late_starter_sits_in_cash_until_its_first_bar_and_says_so():
    closes = {'OLD': {'2023-11-10': 100.0, '2026-11-11': 110.0},
              'NEW': {'2024-06-03': 20.0, '2025-01-02': 10.0, '2026-11-11': 30.0}}
    h = _run({'OLD': 50.0, 'NEW': 50.0}, closes, horizons=('3y',)).horizons['3y']
    by = {r.symbol: r for r in h.rows}
    assert by['NEW'].status == STATUS_LATE
    assert by['NEW'].buy_date == '2024-06-03'
    assert by['NEW'].buy_price == 20.0
    assert '2024-06-03' in by['NEW'].note
    assert by['NEW'].price_value == pytest.approx(5_000.0 / 20.0 * 30.0)
    assert by['OLD'].status == STATUS_INVESTED
    assert h.late_symbols == ['NEW']
    assert any('1 of 2 symbols did not exist for the whole period' in n for n in h.notes)
    # in the chart it is CASH until its first bar: flat at its slice on a date before it existed
    series = dict(zip(h.series_dates, h.series_price))
    assert series[h.series_dates[0]] == pytest.approx(10_000.0)
    assert series['2024-06-03'] == pytest.approx(5_000.0 + 5_000.0)     # OLD forward-filled at 100, NEW just bought
    assert series['2025-01-02'] == pytest.approx(5_000.0 + 250.0 * 10.0)


def test_a_symbol_with_no_price_history_stays_cash_and_is_marked_unmeasurable():
    closes = {'AAA': {'2025-11-12': 100.0, '2026-11-11': 120.0}}
    h = _run({'AAA': 50.0, 'GONE': 50.0}, closes, horizons=('1y',)).horizons['1y']
    by = {r.symbol: r for r in h.rows}
    gone = by['GONE']
    assert gone.status == STATUS_NO_HISTORY
    assert gone.invested == pytest.approx(5_000.0)
    assert gone.price_value == pytest.approx(5_000.0)          # cash, not a price
    assert gone.buy_price is None and gone.last_price is None and gone.quantity is None
    assert 'no price history' in gone.note.lower()
    assert h.no_history_symbols == ['GONE']
    assert any('GONE' in n and 'cash' in n for n in h.notes)
    assert h.price_value == pytest.approx(6_000.0 + 5_000.0)
    assert h.total_value == pytest.approx(11_000.0)


def test_a_symbol_whose_history_ends_early_is_flagged_stale():
    closes = {'AAA': {'2025-11-12': 100.0, '2026-11-11': 120.0},
              'DEAD': {'2025-11-12': 10.0, '2026-03-02': 12.0}}
    h = _run({'AAA': 50.0, 'DEAD': 50.0}, closes, horizons=('1y',)).horizons['1y']
    dead = {r.symbol: r for r in h.rows}['DEAD']
    assert dead.stale is True
    assert dead.last_date == '2026-03-02'
    assert '2026-03-02' in dead.note
    assert any('DEAD' in n and '2026-03-02' in n for n in h.notes)
    assert h.end == '2026-11-11'


def test_unusable_bars_are_dropped_and_counted_never_read_as_a_price():
    closes = {'AAA': {'2025-11-12': 100.0, '2026-05-01': 0.0, '2026-06-01': float('nan'), '2026-11-11': 120.0}}
    h = _run({'AAA': 100.0}, closes, horizons=('1y',)).horizons['1y']
    row = h.rows[0]
    assert row.last_price == 120.0
    assert '2 bar' in row.note
    assert all(math.isfinite(v) and v > 0 for v in h.series_price)


# ---------------------------------------------------------------------------
# distributions
# ---------------------------------------------------------------------------

def test_distributions_inside_the_window_are_cash_on_top_of_the_price_value():
    closes = {'INC': {'2025-11-12': 10.0, '2026-11-11': 9.0}}
    payouts = {'INC': _paid(('2025-11-12', 5.0),      # ex ON the buy day: bought at that close, already ex
                            ('2025-10-01', 5.0),      # before the buy
                            ('2026-01-15', 0.5),
                            ('2026-06-15', 0.25),
                            ('2026-11-11', 0.25),     # ex on the last bar: in
                            ('2026-12-15', 9.0))}     # declared, not yet ex: out
    h = _run({'INC': 100.0}, closes, payouts, horizons=('1y',)).horizons['1y']
    row = h.rows[0]
    assert row.quantity == pytest.approx(1_000.0)
    assert row.distributions_per_share == pytest.approx(1.0)
    assert row.distributions == pytest.approx(1_000.0)
    assert h.price_value == pytest.approx(9_000.0)               # price only: a loss
    assert h.price_gain_pct == pytest.approx(-10.0)
    assert h.distributions == pytest.approx(1_000.0)
    assert h.total_value == pytest.approx(10_000.0)              # with distributions: flat
    assert h.total_gain_pct == pytest.approx(0.0)
    assert h.series_total[-1] == pytest.approx(10_000.0)
    assert h.series_price[-1] == pytest.approx(9_000.0)


def test_a_late_starter_receives_only_the_distributions_after_its_first_bar():
    closes = {'NEW': {'2026-03-02': 10.0, '2026-11-11': 10.0}}
    payouts = {'NEW': _paid(('2026-02-01', 1.0), ('2026-04-01', 1.0))}
    row = _run({'NEW': 100.0}, closes, payouts, horizons=('1y',)).horizons['1y'].rows[0]
    assert row.status == STATUS_LATE
    assert row.distributions_per_share == pytest.approx(1.0)


def test_amounts_are_used_as_given_split_adjusted_on_the_price_basis():
    """A 4-for-1 split inside the window. The closes are split-adjusted (FMP ``close``), so the pre-split dividend must
    be the split-adjusted one (FMP ``adjDividend`` 0.205, not the as-declared 0.82): the service hands those in, and
    the arithmetic multiplies them by the share count bought at the adjusted close -- one basis throughout."""
    # FMP close, split-adjusted: 2020-08-06 raw 455.61 / 4 = 113.90; 2020-08-28 raw 499.23 / 4 = 124.81
    closes = {'AAPL': {'2020-08-06': 113.90, '2020-08-28': 124.81, '2025-08-05': 202.92}}
    payouts = {'AAPL': _paid(('2020-08-07', 0.205), ('2020-11-06', 0.205))}
    res = simulate_label_growth(11_390.0, {'AAPL': 100.0}, closes, payouts, as_of=date(2025, 8, 6),
                                horizons=('5y',))
    row = res.horizons['5y'].rows[0]                            # 5y base: 2020-08-06
    assert row.buy_date == '2020-08-06'
    assert row.quantity == pytest.approx(100.0)
    assert row.distributions == pytest.approx(100.0 * 0.205 * 2)   # NOT 100 * 0.82 for the pre-split payment
    assert row.price_value == pytest.approx(20_292.0)


def test_a_distribution_without_an_amount_makes_the_total_unknown_not_smaller():
    closes = {'INC': {'2025-11-12': 10.0, '2026-11-11': 10.0}, 'AAA': {'2025-11-12': 10.0, '2026-11-11': 11.0}}
    payouts = {'INC': _paid(('2026-01-15', 0.5), ('2026-02-15', None)), 'AAA': _none()}
    h = _run({'INC': 50.0, 'AAA': 50.0}, closes, payouts, horizons=('1y',)).horizons['1y']
    inc = {r.symbol: r for r in h.rows}['INC']
    assert inc.distributions is None and inc.total_value is None
    assert h.distributions is None and h.total_value is None and h.total_gain_pct is None
    assert h.series_total is None
    assert any('INC' in reason for reason in h.distributions_unknown)
    assert h.price_value == pytest.approx(5_000.0 + 5_500.0)       # the price-only answer still stands


def test_a_failed_distribution_fetch_makes_the_total_unknown_and_names_the_error():
    closes = {'INC': {'2025-11-12': 10.0, '2026-11-11': 10.0}}
    payouts = {'INC': DistributionHistory(events=None, error='FMP stock_dividend: HTTP 500')}
    h = _run({'INC': 100.0}, closes, payouts, horizons=('1y',)).horizons['1y']
    assert h.total_value is None
    assert any('HTTP 500' in reason for reason in h.distributions_unknown)


def test_a_symbol_missing_from_the_distribution_map_is_unknown_not_zero():
    closes = {'INC': {'2025-11-12': 10.0, '2026-11-11': 10.0}}
    h = _run({'INC': 100.0}, closes, {}, horizons=('1y',)).horizons['1y']
    assert h.total_value is None


def test_a_cash_slice_needs_no_distribution_history():
    """A no-history symbol and a zero share hold cash or nothing: no distribution history is needed for them."""
    closes = {'AAA': {'2025-11-12': 10.0, '2026-11-11': 10.0}}
    h = _run({'AAA': 50.0, 'GONE': 30.0, 'ZERO': 0.0}, closes, {'AAA': _none()}, horizons=('1y',)).horizons['1y']
    assert h.total_value == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# zero and odd shares, bad input
# ---------------------------------------------------------------------------

def test_a_zero_share_is_listed_but_gets_nothing():
    closes = {'AAA': {'2025-11-12': 10.0, '2026-11-11': 20.0}, 'ZZZ': {'2025-11-12': 1.0, '2026-11-11': 100.0}}
    h = _run({'AAA': 100.0, 'ZZZ': 0.0}, closes, horizons=('1y',)).horizons['1y']
    zzz = {r.symbol: r for r in h.rows}['ZZZ']
    assert zzz.status == STATUS_ZERO_SHARE
    assert zzz.invested == 0.0 and zzz.price_value == 0.0
    assert zzz.price_gain_pct is None
    assert h.price_value == pytest.approx(20_000.0)
    assert h.invested_count == 1


def test_shares_under_100_leave_the_rest_in_cash_and_say_so():
    closes = {s: {'2025-11-12': 10.0, '2026-11-11': 20.0} for s in ('A', 'B', 'C')}
    h = _run({'A': 33.33, 'B': 33.33, 'C': 33.33}, closes, horizons=('1y',)).horizons['1y']
    assert h.unassigned_pct == pytest.approx(0.01)
    assert h.unassigned_cash == pytest.approx(1.0)
    assert h.price_value == pytest.approx(9_999.0 * 2 + 1.0)
    assert any('99.99%' in n and 'cash' in n for n in h.notes)


def test_shares_at_100_leave_no_cash_note():
    closes = {'A': {'2025-11-12': 10.0, '2026-11-11': 20.0}}
    h = _run({'A': 100.0}, closes, horizons=('1y',)).horizons['1y']
    assert h.unassigned_cash == pytest.approx(0.0)
    assert not any('held as cash' in n for n in h.notes)


def test_shares_over_100_are_refused():
    with pytest.raises(ValueError, match='over 100'):
        _run({'A': 70.0, 'B': 40.0}, {})


@pytest.mark.parametrize('bad', [-1.0, float('nan'), float('inf')])
def test_a_negative_or_non_finite_share_is_refused(bad):
    with pytest.raises(ValueError):
        _run({'A': bad}, {})


@pytest.mark.parametrize('amount', [0.0, -5.0, float('nan'), None])
def test_a_non_positive_amount_is_refused(amount):
    with pytest.raises(ValueError):
        _run({'A': 100.0}, {'A': {'2025-11-12': 1.0}}, amount=amount)


def test_the_result_scales_with_the_amount():
    closes = {'A': {'2025-11-12': 10.0, '2026-11-11': 13.0}}
    small = _run({'A': 100.0}, closes, amount=1_000.0, horizons=('1y',)).horizons['1y']
    big = _run({'A': 100.0}, closes, amount=50_000.0, horizons=('1y',)).horizons['1y']
    assert small.price_gain_pct == pytest.approx(big.price_gain_pct)
    assert big.price_value == pytest.approx(65_000.0)


# ---------------------------------------------------------------------------
# the chart series and options
# ---------------------------------------------------------------------------

def test_the_series_starts_at_the_amount_and_ends_at_the_final_values():
    closes = {'A': {'2025-11-12': 10.0, '2026-01-02': 11.0, '2026-11-11': 12.0},
              'B': {'2025-11-12': 20.0, '2026-02-02': 19.0, '2026-11-11': 22.0}}
    payouts = {'A': _paid(('2026-01-05', 1.0)), 'B': _none()}
    h = _run({'A': 50.0, 'B': 50.0}, closes, payouts, horizons=('1y',)).horizons['1y']
    assert h.series_dates[0] == '2025-11-12'
    assert h.series_dates == ['2025-11-12', '2026-01-02', '2026-02-02', '2026-11-11']
    assert h.series_price[0] == pytest.approx(10_000.0)
    assert h.series_total[0] == pytest.approx(10_000.0)
    assert h.series_price[-1] == pytest.approx(h.price_value)
    assert h.series_total[-1] == pytest.approx(h.total_value)
    # the 2026-01-05 distribution shows up on the first bar on/after it, not before
    assert h.series_total[1] == pytest.approx(h.series_price[1])
    assert h.series_total[2] - h.series_price[2] == pytest.approx(500.0)


def test_chart_options_carry_both_series_on_one_axis():
    closes = {'A': {'2025-11-12': 10.0, '2026-11-11': 12.0}}
    h = _run({'A': 100.0}, closes, horizons=('1y',)).horizons['1y']
    opts = growth_chart_options(h)
    names = [s['name'] for s in opts['series']]
    assert names == [sim.SERIES_PRICE, sim.SERIES_TOTAL]
    assert not isinstance(opts['yAxis'], list)
    assert opts['tooltip']['trigger'] == 'axis'
    assert opts['xAxis']['data'] == h.series_dates


def test_chart_options_drop_the_total_line_when_distributions_are_unknown():
    closes = {'A': {'2025-11-12': 10.0, '2026-11-11': 12.0}}
    h = _run({'A': 100.0}, closes, {}, horizons=('1y',)).horizons['1y']
    assert [s['name'] for s in growth_chart_options(h)['series']] == [sim.SERIES_PRICE]


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def test_money_and_percent_formatting():
    assert sim.fmt_money(12345.678) == '$12,345.68'
    assert sim.fmt_money(-5.0) == '-$5.00'
    assert sim.fmt_money(None) == 'n/a'
    assert sim.fmt_signed_money(1500.0) == '+$1,500.00'
    assert sim.fmt_signed_money(-1500.0) == '-$1,500.00'
    assert sim.fmt_signed_pct(12.346) == '+12.35%'
    assert sim.fmt_signed_pct(-0.5) == '-0.50%'
    assert sim.fmt_signed_pct(None) == 'n/a'


# ---------------------------------------------------------------------------
# review 2026-11-12: "late start" must be truthful; a stale base is a data gap
# ---------------------------------------------------------------------------

def test_history_is_fetched_a_full_year_before_the_longest_horizon():
    """A symbol with no bar in the fetched window before a start date then has had no price for at least a year,
    so 'did not trade before X' is true; with a 14-day buffer a two-week halt read as 'did not exist'."""
    assert sim.FETCH_BUFFER_DAYS >= 365
    assert history_start(AS_OF) <= date(2020, 11, 12)


def test_a_late_starter_note_says_what_was_actually_observed():
    closes = {'NEW': {'2024-06-03': 20.0, '2026-11-11': 30.0}}
    row = _run({'NEW': 100.0}, closes, horizons=('3y',)).horizons['3y'].rows[0]
    assert row.status == STATUS_LATE
    assert history_start(AS_OF).isoformat() in row.note          # "no price since <fetch start>"
    assert '2024-06-03' in row.note


def test_a_base_close_far_before_the_start_is_flagged_as_a_data_gap_not_hidden():
    closes = {'HALT': {'2025-09-01': 50.0, '2026-02-02': 55.0, '2026-11-11': 60.0},
              'AAA': {'2025-11-12': 10.0, '2026-11-11': 11.0}}
    h = _run({'HALT': 50.0, 'AAA': 50.0}, closes, horizons=('1y',)).horizons['1y']
    halt = {r.symbol: r for r in h.rows}['HALT']
    assert halt.status == sim.STATUS_GAP
    assert halt.buy_date == '2025-09-01' and halt.buy_price == 50.0      # the last REAL close, never invented
    assert 'gap' in halt.note.lower() and '2025-09-01' in halt.note
    assert h.gap_symbols == ['HALT']
    assert any('HALT' in n and 'gap' in n.lower() for n in h.notes)
    assert h.price_value == pytest.approx(5_000.0 / 50.0 * 60.0 + 5_500.0)   # still invested, from that close


def test_a_holiday_weekend_before_the_start_is_not_a_gap():
    closes = {'AAA': {'2025-12-24': 100.0, '2026-11-11': 110.0}}          # 31 Dec base 7 days back: Christmas week
    row = _run({'AAA': 100.0}, closes, horizons=('ytd',)).horizons['ytd'].rows[0]
    assert row.status == STATUS_INVESTED
