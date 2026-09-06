"""Per-share dividend growth, derived from the ex-dividend history.

Asked for on 2026-09-05 while comparing wheel income funds: a high trailing yield on
a SHRINKING distribution is a different proposition from the same yield on a rising
one, and the table could not tell them apart.
"""
from datetime import date, timedelta

import pytest

from ba2_trade_platform.ui.components import symbol_info_panel as panel

AS_OF = date(2026, 9, 5)


class _Series:
    def __init__(self, events, unknown=False, why=""):
        self.dividends = [type("D", (), {"ex_date": d, "chart_amount": a})()
                          for d, a in events]
        self._unknown, self._why = unknown, why

    def is_unknown(self, field):
        return self._unknown and field == "dividends"

    def why(self, field):
        return self._why


def _info(events, failure="", **kw):
    return type("I", (), {"series": _Series(events, **kw), "symbol": "X", "as_of": AS_OF,
                          "details": {"*": failure} if failure else {}})()


def _month_back(day, months):
    """``day`` shifted back whole CALENDAR months, clamped to the 28th."""
    y, m = day.year, day.month - months
    while m <= 0:
        m += 12; y -= 1
    return date(y, m, min(day.day, 28))


def _monthly(amount, n, end=AS_OF):
    """``n`` monthly payments of ``amount``, on the same day each month.

    Calendar months, NOT 30-day steps: 30-day spacing drifts a payment into the
    trailing year every twelfth month, which shows +8.33% of phantom growth on a flat
    payer. Real monthly funds pay on a month anchor, and the fixture has to match or
    it tests the drift rather than the code.
    """
    return [(_month_back(end, i), amount) for i in range(n)]


class TestGrowth:
    def test_a_flat_payer_grows_zero(self):
        assert panel.dividend_growth(_info(_monthly(1.0, 60)), 1).text == "+0.00%"

    def test_a_doubling_reads_as_100_percent_over_one_year(self):
        assert panel.dividend_growth(
            _info(_monthly(2.0, 12) + _monthly(1.0, 24, _month_back(AS_OF, 12))),
            1).text == "+100.00%"

    def test_a_cut_reads_negative(self):
        assert panel.dividend_growth(
            _info(_monthly(0.5, 12) + _monthly(1.0, 24, _month_back(AS_OF, 12))),
            1).text.startswith("-50.")

    def test_the_three_year_figure_is_ANNUALISED_not_cumulative(self):
        # 2x over 3 years is ~26%/yr, not 100%. Only the annualised number is
        # comparable against the 1Y figure printed beside it.
        cell = panel.dividend_growth(
            _info(_monthly(2.0, 12) + _monthly(1.0, 24, _month_back(AS_OF, 36))), 3)
        assert cell.text.startswith("+25.") or cell.text.startswith("+26.")

    def test_the_note_shows_both_totals_it_divided(self):
        note = panel.dividend_growth(_info(_monthly(1.0, 60)), 1).note
        assert "last 12m" in note and "12m ending" in note


class TestCadenceChangesDoNotFakeGrowth:
    def test_weekly_to_monthly_at_the_same_annual_total_is_flat(self):
        # THE reason this uses trailing-12m TOTALS rather than per-payment amounts:
        # per payment, weekly 1.0 -> monthly 4.33 looks like a 333% rise.
        recent = _monthly(4.3333, 12)
        # 70 weeks so the history reaches past the base window's far edge; at 52 the
        # first payment lands ~23.7 months back and the fund reads as too young.
        older = [(_month_back(AS_OF, 12) - timedelta(days=7 * i), 1.0) for i in range(70)]
        pct = float(panel.dividend_growth(_info(recent + older), 1).text.strip("%+"))
        assert abs(pct) < 2.0


class TestHonestRefusals:
    def test_a_fund_younger_than_the_window_is_NOT_given_a_growth_rate(self):
        # TSMY's real case: first paid 2024-10, so a 1Y comparison would measure a
        # full year against a partial one and report the listing as growth.
        young = _monthly(1.0, 14)
        cell = panel.dividend_growth(_info(young), 1)
        assert cell.unknown is True
        assert "first distribution" in cell.reason

    def test_the_three_year_window_refuses_a_two_year_old_fund(self):
        two_years = _monthly(1.0, 24)
        assert panel.dividend_growth(_info(two_years), 3).unknown is True

    def test_a_zero_base_is_refused_rather_than_dividing(self):
        recent = _monthly(1.0, 12)
        ancient = [(_month_back(AS_OF, 48), 1.0)]      # nothing in the base year
        cell = panel.dividend_growth(_info(recent + ancient), 1)
        assert cell.unknown is True
        assert "no base to grow from" in cell.reason

    def test_a_missing_amount_refuses_rather_than_understating(self):
        events = _monthly(1.0, 60)
        events[0] = (events[0][0], None)
        cell = panel.dividend_growth(_info(events), 1)
        assert cell.unknown is True
        assert "no amount" in cell.reason

    def test_a_non_payer_is_not_applicable_not_unknown(self):
        cell = panel.dividend_growth(_info([]), 1)
        assert cell.text == panel.NOT_APPLICABLE_TEXT
        assert cell.unknown is False

    def test_a_whole_symbol_failure_carries_its_reason(self):
        cell = panel.dividend_growth(_info([], failure="total failure"), 1)
        assert cell.unknown is True and cell.reason == "total failure"

    def test_an_unfetchable_history_is_unknown(self):
        cell = panel.dividend_growth(_info([], unknown=True, why="FMP timed out"), 1)
        assert cell.unknown is True and cell.reason == "FMP timed out"


class TestItIsOnTheComparison:
    def test_both_windows_are_rows(self):
        labels = [l for l, _ in panel._comparison_specs()]
        assert panel.DIV_GROWTH_LABELS[1] in labels
        assert panel.DIV_GROWTH_LABELS[3] in labels

    def test_they_sit_with_the_income_figures(self):
        labels = [l for l, _ in panel._comparison_specs()]
        assert labels.index(panel.DIV_GROWTH_LABELS[1]) == labels.index(panel.LABEL_PAYOUT_FREQ) + 1
