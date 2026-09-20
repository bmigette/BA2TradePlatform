"""Chart-side payoff view: breakevens, limits, moneyness (spec 2026-09-20, section 8).

The same fixture table the TypeScript view model is tested against. If these two ever
disagree, the React popup and the live popup would draw different curves for the same trade.

The payoff FORMULA itself is the engine's (`option_payoff.payoff_at`) and is tested there;
these tests pin what the chart layer adds -- the breakevens, the three-state limits, the
provenance rule, and the bands/segments the plot draws.
"""
from __future__ import annotations

import math

import pytest

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_payoff_chart import (
    ChartLeg, build_payoff_chart, chart_leg_from_row, moneyness, sample_curve,
    sign_segments, spot_vs_strike_percent, zone_bands,
)
from ba2_common.core.types import OrderDirection


def leg(kind='call', side=OrderDirection.BUY, premium=5.0, strike=100.0,
        ratio=1, multiplier=100.0, **chart):
    return ChartLeg(
        payoff=PayoffLeg(kind=kind, side=side, premium=premium, strike=strike,
                         ratio=ratio, multiplier=multiplier),
        **chart,
    )


SHORT = OrderDirection.SELL


def priced(legs, scope=None):
    result = build_payoff_chart(legs, scope)
    assert result.available, getattr(result, 'reason', 'unavailable')
    return result


def reason_of(legs, scope=None):
    result = build_payoff_chart(legs, scope)
    assert not result.available
    return result.reason


class TestSingleLegs:
    def test_long_call(self):
        chart = priced([leg()])
        assert chart.net_entry_debit == 500.0
        assert chart.breakevens == (105.0,)
        assert chart.max_loss.amount == 500.0
        assert chart.max_loss.describe(loss=True) == '$-500.00'
        assert chart.max_profit.unlimited
        assert chart.max_profit.describe() == 'Unlimited'
        # ITM is not profitable: at 102 the call is in the money and the position is down 300.
        assert chart.payoff_at(102) == pytest.approx(-300.0)

    def test_short_call(self):
        chart = priced([leg(side=SHORT)])
        assert chart.net_entry_debit == -500.0
        assert chart.breakevens == (105.0,)
        assert chart.max_profit.amount == 500.0
        assert chart.max_loss.unlimited

    def test_long_put(self):
        chart = priced([leg(kind='put')])
        assert chart.breakevens == (95.0,)
        assert chart.max_loss.amount == 500.0
        assert chart.max_profit.amount == 9500.0
        assert chart.payoff_at(96) == pytest.approx(-100.0)

    def test_short_put_is_bounded_by_a_non_negative_price(self):
        chart = priced([leg(kind='put', side=SHORT)])
        assert chart.max_profit.amount == 500.0
        assert chart.max_loss.amount == 9500.0
        assert not chart.max_loss.unlimited

    def test_ratio_scales_the_money(self):
        chart = priced([leg(ratio=2)])
        assert chart.net_entry_debit == 1000.0
        assert chart.max_loss.amount == 1000.0
        assert chart.payoff_at(102) == pytest.approx(-600.0)


class TestStructures:
    def test_call_debit_spread(self):
        chart = priced([leg(strike=95, premium=8), leg(strike=105, premium=2, side=SHORT)])
        assert chart.net_entry_debit == 600.0
        assert chart.breakevens == (101.0,)
        assert chart.max_profit.amount == 400.0
        assert chart.max_loss.amount == 600.0
        # The spec's exit scenario: recorded gross +370, expiration curve at the same spot +400.
        assert chart.payoff_at(108) == pytest.approx(400.0)

    def test_put_credit_spread(self):
        chart = priced([leg(kind='put', side=SHORT, premium=4),
                        leg(kind='put', strike=95, premium=1)])
        assert chart.net_entry_debit == -300.0
        assert chart.breakevens == (97.0,)
        assert chart.max_profit.amount == 300.0
        assert chart.max_loss.amount == 200.0

    def test_long_straddle_profits_at_both_tails(self):
        chart = priced([leg(premium=5), leg(kind='put', premium=4)])
        assert chart.breakevens == (91.0, 109.0)
        assert chart.max_loss.amount == 900.0
        assert chart.max_profit.unlimited
        assert chart.payoff_at(80) > 0 and chart.payoff_at(120) > 0
        assert chart.payoff_at(100) == pytest.approx(-900.0)

    def test_iron_condor(self):
        chart = priced([
            leg(kind='put', strike=90, premium=1),
            leg(kind='put', strike=95, premium=2, side=SHORT),
            leg(kind='call', strike=105, premium=2, side=SHORT),
            leg(kind='call', strike=110, premium=1),
        ])
        assert chart.net_entry_debit == -200.0
        assert chart.breakevens == (93.0, 107.0)
        assert chart.max_profit.amount == 200.0
        assert chart.max_loss.amount == 300.0
        assert chart.payoff_at(80) < 0 and chart.payoff_at(120) < 0
        assert chart.payoff_at(100) == pytest.approx(200.0)

    def test_a_1x2_ratio_reverses_the_tail(self):
        chart = priced([leg(premium=5), leg(strike=110, premium=2, side=SHORT, ratio=2)])
        assert chart.net_entry_debit == 100.0
        assert chart.max_loss.unlimited
        assert chart.max_profit.amount == 900.0
        assert chart.breakevens == (101.0, 119.0)

    def test_a_genuine_zero_premium_is_a_real_price(self):
        chart = priced([leg(premium=0)])
        assert chart.net_entry_debit == 0.0
        assert chart.breakevens == (100.0,)
        assert chart.flat_zero_intervals == ((0.0, 100.0),)


class TestProvenance:
    def test_an_unverified_multiplier_refuses_before_anything_else(self):
        # The engine's PayoffLeg DEFAULTS multiplier to 100.0, which is right for the rules
        # engine and exactly the assumption a popup must not inherit.
        reason = reason_of([leg(multiplier_recorded=False)])
        assert 'unverified' in reason

    def test_a_missing_multiplier_is_refused_not_priced_at_100(self):
        reason = reason_of([leg(multiplier=None, multiplier_recorded=True)])
        assert 'multiplier' in reason

    def test_a_recorded_multiplier_of_one_is_accepted(self):
        chart = priced([leg(multiplier=1)])
        assert chart.net_entry_debit == 5.0
        assert chart.max_loss.amount == 5.0

    def test_a_bad_leg_does_not_silently_leave_the_sum(self):
        broken = [leg(strike=95), leg(strike=105, multiplier=None, multiplier_recorded=True)]
        assert not build_payoff_chart(broken).available
        # ...but a good leg is still inspectable on its own.
        single = priced(broken, scope=0)
        assert len(single.legs) == 1
        assert single.net_entry_debit == 500.0


class TestCombinationLimits:
    def test_different_expiries_cannot_be_combined(self):
        legs = [leg(expiry='2026-09-18'), leg(strike=110, premium=2, side=SHORT, expiry='2026-10-16')]
        assert 'different expiries' in reason_of(legs)
        assert priced(legs, scope=1).legs[0].expiry == '2026-10-16'

    def test_different_underlyings_cannot_be_combined(self):
        legs = [leg(underlying='ACN'), leg(strike=110, premium=2, side=SHORT, underlying='MSFT')]
        assert 'different underlyings' in reason_of(legs)


class TestDomainAndBands:
    def test_the_domain_never_goes_negative(self):
        chart = priced([leg(kind='put')])
        assert chart.domain[0] >= 0
        assert chart.domain[0] < 95 < 100 < chart.domain[1]

    def test_bands_follow_the_breakevens(self):
        chart = priced([leg(premium=5), leg(kind='put', premium=4)])
        bands = zone_bands(chart)
        assert [sign for sign, _low, _high in bands] == ['profit', 'loss', 'profit']
        for sign, low, high in bands:
            expected = 'profit' if chart.payoff_at((low + high) / 2) >= 0 else 'loss'
            assert sign == expected

    def test_segments_meet_on_the_zero_line(self):
        chart = priced([leg(premium=5), leg(kind='put', premium=4)])
        segments = sign_segments(sample_curve(chart))
        assert [sign for sign, _points in segments] == ['profit', 'loss', 'profit']
        boundary = segments[0][1][-1]
        assert boundary[1] == pytest.approx(0.0, abs=1e-6)
        assert boundary[0] == pytest.approx(91.0, abs=1e-6)

    def test_sampling_includes_every_strike_and_breakeven(self):
        chart = priced([leg(premium=5), leg(kind='put', premium=4)])
        prices = [price for price, _pnl in sample_curve(chart)]
        for anchor in (100.0, 91.0, 109.0):
            assert any(abs(price - anchor) < 1e-6 for price in prices)
        assert prices == sorted(prices)


class TestRowAdapter:
    def test_a_raw_row_without_a_multiplier_is_not_certified(self):
        chart_leg = chart_leg_from_row({
            'side': 'buy', 'option_type': 'call', 'strike': 100,
            'entry_price': 5, 'size': 1,
        })
        assert chart_leg.multiplier_recorded is False
        assert not build_payoff_chart([chart_leg]).available

    def test_a_raw_row_with_a_multiplier_is_priced(self):
        chart_leg = chart_leg_from_row({
            'side': 'buy', 'option_type': 'call', 'strike': 100,
            'entry_price': 5, 'size': 1, 'multiplier': 100,
        })
        assert chart_leg.multiplier_recorded is True
        assert priced([chart_leg]).net_entry_debit == 500.0

    def test_the_direction_vocabulary_is_normalised(self):
        chart_leg = chart_leg_from_row({
            'side': 'SELL', 'option_type': 'put', 'strike': 100,
            'entry_price': 4, 'size': 1, 'multiplier': 100,
        })
        chart = priced([chart_leg])
        assert chart.net_entry_debit == -400.0
        assert chart.max_profit.amount == 400.0


class TestMoneyness:
    def test_calls_and_puts_are_classified_against_the_strike(self):
        assert moneyness('call', 100, 102) == 'ITM'
        assert moneyness('call', 100, 100) == 'ATM'
        assert moneyness('call', 100, 98) == 'OTM'
        assert moneyness('put', 100, 98) == 'ITM'
        assert moneyness('put', 100, 100) == 'ATM'
        assert moneyness('put', 100, 102) == 'OTM'

    def test_the_tolerance_is_floating_point_dust_not_a_band(self):
        assert moneyness('call', 100, 100 + 1e-12) == 'ATM'
        assert moneyness('call', 100, 100.001) == 'ITM'

    def test_unknown_rather_than_a_guess(self):
        assert moneyness('call', None, 100) == 'unknown'
        assert moneyness('call', 100, None) == 'unknown'
        assert moneyness(None, 100, 100) == 'unknown'
        assert moneyness('call', 0, 100) == 'unknown'
        assert moneyness('call', 100, math.nan) == 'unknown'

    def test_spot_vs_strike_is_a_percentage_of_the_strike(self):
        assert spot_vs_strike_percent(108, 101) == pytest.approx(6.9307, abs=1e-3)
        assert spot_vs_strike_percent(95, 100) == pytest.approx(-5.0)
        assert spot_vs_strike_percent(None, 100) is None
