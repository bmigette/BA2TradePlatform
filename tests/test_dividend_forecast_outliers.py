"""Dividend forecast outliers: a payment on a liquidation-shrunk share count must not explode the forecast."""
from datetime import date, datetime

import pytest

from ba2_trade_platform.ui.utils.dividend_forecast import (
    drop_tiny_quantity_outliers, forecast_dividends, is_outlier,
)

TODAY = date(2026, 10, 3)
DIVO = [(date(2026, 6, 30), 0.1544), (date(2026, 7, 31), 15.8699),
        (date(2026, 8, 31), 0.1959), (date(2026, 9, 30), 0.1577)]


def test_divo_series_forecasts_a_normal_per_share_amount():
    out = forecast_dividends(DIVO, 5.17573, TODAY)
    assert out, 'a monthly payer still forecasts'
    for ev in out:
        assert 0.15 <= ev['per_share'] <= 0.20
        assert ev['amount'] == pytest.approx(ev['per_share'] * 5.17573, abs=0.01)
    assert [e['date'] for e in out][:2] == [date(2026, 10, 30), date(2026, 11, 30)]


def test_cadence_still_uses_the_outlier_s_payment_date():
    # dropping the 07-31 DATE would leave a 62-day gap and kill the monthly cadence
    assert forecast_dividends(DIVO, 1, TODAY)


def test_variable_payers_use_the_median_not_the_mean():
    var = [(date(2026, 7, 15), 0.10), (date(2026, 8, 15), 0.30), (date(2026, 9, 15), 0.20)]
    out = forecast_dividends(var, 10, TODAY)
    assert out[0]['per_share'] == pytest.approx(0.20)               # median, not 0.2 mean-equal case
    skew = [(date(2026, 7, 15), 0.10), (date(2026, 8, 15), 0.11), (date(2026, 9, 15), 0.30)]
    assert forecast_dividends(skew, 10, TODAY)[0]['per_share'] == pytest.approx(0.11)   # mean would be 0.17


def test_outlier_rule_is_five_times_either_way_and_needs_two_others():
    assert is_outlier(15.0, [0.15, 0.19, 0.16]) is True
    assert is_outlier(0.01, [0.15, 0.19, 0.16]) is True             # below 1/5 of the median
    assert is_outlier(0.5, [0.15, 0.19, 0.16]) is False             # 3x is variation, not an outlier
    assert is_outlier(15.0, [0.15]) is False                         # one other value: no opinion


def test_a_genuine_step_up_in_the_latest_values_is_not_dropped_when_the_median_follows():
    step = [(date(2026, 4, 30), 0.10), (date(2026, 5, 31), 0.10), (date(2026, 6, 30), 0.40),
            (date(2026, 7, 31), 0.42), (date(2026, 8, 31), 0.41), (date(2026, 9, 30), 0.40)]
    assert forecast_dividends(step, 10, TODAY)[0]['per_share'] == pytest.approx(0.40, abs=0.02)


def test_tiny_quantity_outliers_are_dropped_but_tiny_quantity_alone_is_not():
    per = {'a': 0.15, 'b': 15.87, 'c': 0.19, 'd': 0.16}
    qty = {'a': 5.0, 'b': 0.05, 'c': 5.0, 'd': 5.0}
    assert set(drop_tiny_quantity_outliers(per, qty, 5.0)) == {'a', 'c', 'd'}
    # tiny quantity but a normal per-share value: kept
    assert 'b' in drop_tiny_quantity_outliers({'a': 0.15, 'b': 0.17, 'c': 0.19}, {'a': 5, 'b': 0.05, 'c': 5}, 5.0)
    # an outlier on a normal quantity: kept here (the forecast rule handles it)
    assert 'b' in drop_tiny_quantity_outliers(per, {'a': 5, 'b': 5, 'c': 5, 'd': 5}, 5.0)


def test_weekly_monthly_quarterly_unchanged():
    from datetime import timedelta
    weekly = [(date(2026, 9, 29) - timedelta(days=7 * i), 0.5) for i in range(6)]
    assert len(forecast_dividends(weekly, 2, TODAY)) >= 8
    quarterly = [(date(2026, 7, 20), 0.5), (date(2026, 4, 20), 0.5), (date(2026, 1, 20), 0.5)]
    assert forecast_dividends(quarterly, 10, TODAY)[0]['amount'] == 5.0


def test_overview_derivation_ignores_a_payment_made_on_a_liquidation_shrunk_position():
    import ba2_trade_platform.ui.pages.overview as ov
    from datetime import timedelta
    ov_labels = ov.get_labels_by_symbol
    ov.get_labels_by_symbol = lambda syms: {}
    try:
        tab = ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)
        pay = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)]
        qty_at = {pay[0]: 100.0, pay[1]: 0.5, pay[2]: 100.0, pay[3]: 100.0}
        # $0.16 per share; on 07-31 only 0.5 shares were held (after a forced sale) -> $0.08 paid
        divs = [{'symbol': 'XYZ', 'account_id': 2, 'amount': round(0.16 * qty_at[d], 4),
                 'drip_quantity': None, 'date': datetime(d.year, d.month, d.day)} for d in pay]
        trades = [{'symbol': 'XYZ', 'account_id': 2, 'side': 'SELL', 'qty': 99.5, 'price': 20,
                   'date': datetime(2026, 7, 30)},
                  {'symbol': 'XYZ', 'account_id': 2, 'side': 'BUY', 'qty': 99.5, 'price': 20,
                   'date': datetime(2026, 8, 1)}]
        out = tab._compute_dividend_forecast(divs, trades, {(2, 'XYZ'): 100.0})
        totals = sorted(round(v['total'], 2) for v in out.values())
        assert totals and all(t == pytest.approx(16.0, abs=0.1) for t in totals[:2])
    finally:
        ov.get_labels_by_symbol = ov_labels
