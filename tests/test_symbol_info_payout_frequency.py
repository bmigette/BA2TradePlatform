"""How often a symbol distributes, derived from its ex-dividend dates.

Asked for from live use on 2026-09-05, comparing income ETFs (CAS, GIAX, SLVO,
TSMY): a 51.94% trailing yield paid WEEKLY and the same number paid annually are
very different instruments, and the comparison table could not tell them apart.

Derived rather than read from a provider field — FMP publishes no reliable frequency
for these funds, and the ex-dates are already fetched for the dividend bars.
"""
from datetime import date, timedelta

import pytest

from ba2_trade_platform.ui.components import symbol_info_panel as panel


class _Series:
    def __init__(self, ex_dates, unknown=False, why=""):
        self.dividends = [type("D", (), {"ex_date": d})() for d in ex_dates]
        self._unknown, self._why = unknown, why

    def is_unknown(self, field):
        return self._unknown and field == "dividends"

    def why(self, field):
        return self._why


def _info(ex_dates, failure="", **kw):
    # ``details`` is part of every real SymbolInfo; ``details["*"]`` is the
    # whole-symbol failure reason, which payout_frequency checks before anything else.
    return type("I", (), {"series": _Series(ex_dates, **kw), "symbol": "X",
                          "details": {"*": failure} if failure else {}})()


def _every(days, n, start=date(2025, 1, 2)):
    return [start + timedelta(days=days * i) for i in range(n)]


class TestNamedCadences:
    @pytest.mark.parametrize("days,name", [
        (7, "Weekly"), (14, "Bi-weekly"), (30, "Monthly"),
        (91, "Quarterly"), (182, "Semi-annual"), (365, "Annual"),
    ])
    def test_each_band(self, days, name):
        assert panel.payout_frequency(_info(_every(days, 6))).text.startswith(name)

    def test_the_measured_gap_is_shown_beside_the_name(self):
        # The name is a bucket; the number is the evidence for it.
        assert "~7d" in panel.payout_frequency(_info(_every(7, 6))).text

    def test_a_drifting_monthly_schedule_still_reads_monthly(self):
        # Real schedules wander: 28-35 days apart is one cadence, not five.
        drift = [date(2025, m, d) for m, d in
                 [(1, 2), (2, 3), (3, 1), (4, 4), (5, 2), (6, 6)]]
        assert panel.payout_frequency(_info(drift)).text.startswith("Monthly")


class TestTheThreeOutcomes:
    def test_a_non_payer_is_NOT_APPLICABLE_not_unknown(self):
        # The history was fetched and contains no distributions. That is a fact
        # about the fund, not a gap in our data, and must not read as n/a.
        cell = panel.payout_frequency(_info([]))
        assert cell.text == panel.NOT_APPLICABLE_TEXT
        assert cell.unknown is False
        assert "no distributions" in cell.note

    def test_a_whole_symbol_failure_is_unknown_not_a_non_payer(self):
        # Nothing came back at all, so an empty dividend list is an absence of
        # EVIDENCE about the fund, not evidence the fund pays nothing.
        cell = panel.payout_frequency(_info([], failure="total failure"))
        assert cell.unknown is True
        assert cell.reason == "total failure"

    def test_an_unfetchable_history_is_unknown_WITH_the_reason(self):
        cell = panel.payout_frequency(_info([], unknown=True, why="FMP timed out"))
        assert cell.unknown is True
        assert cell.text == panel.UNKNOWN_TEXT
        assert cell.reason == "FMP timed out"

    def test_a_single_dividend_cannot_answer_how_often(self):
        # One payment is a fact about the fund; it is still not a cadence.
        cell = panel.payout_frequency(_info([date(2025, 1, 2)]))
        assert cell.unknown is True
        assert "1 distribution" in cell.reason


class TestHonestEdges:
    def test_an_unnameable_median_reports_the_number_rather_than_giving_up(self):
        # The bands run contiguously to 450 days, so "unnameable" means LONGER than
        # annual -- a fund paying every ~500 days. "We measured 500" beats n/a.
        cell = panel.payout_frequency(_info(_every(500, 4)))
        assert "irregular" in cell.text and "500" in cell.text
        assert cell.unknown is False

    def test_63_days_is_called_quarterly_rather_than_irregular(self):
        # The bands are deliberately generous: a quarterly payer that drifts to 63
        # days is still quarterly, and inventing "irregular" for it would be noise.
        assert panel.payout_frequency(_info(_every(63, 6))).text.startswith("Quarterly")

    def test_it_reads_the_CURRENT_cadence_after_a_switch(self):
        # A fund that moved quarterly -> weekly must read weekly; averaging the whole
        # history would invent a cadence it has never kept.
        quarterly = _every(91, 6, start=date(2023, 1, 2))
        weekly = _every(7, 14, start=quarterly[-1] + timedelta(days=7))
        assert panel.payout_frequency(_info(quarterly + weekly)).text.startswith("Weekly")

    def test_unsorted_dates_are_handled(self):
        dates = _every(7, 6)
        assert panel.payout_frequency(_info(list(reversed(dates)))).text.startswith("Weekly")

    def test_duplicate_ex_dates_do_not_produce_a_zero_gap(self):
        # A zero-day gap would drag the median to "Weekly" on a quarterly payer.
        dates = _every(91, 5)
        assert panel.payout_frequency(_info(dates + [dates[-1]])).text.startswith("Quarterly")


class TestItIsOnBothTables:
    def test_the_overview_carries_it(self):
        labels = [l for l, _ in panel._comparison_specs()]
        assert panel.LABEL_PAYOUT_FREQ in labels

    def test_it_sits_with_the_income_figures_not_at_the_end(self):
        labels = [l for l, _ in panel._comparison_specs()]
        assert labels.index(panel.LABEL_PAYOUT_FREQ) == labels.index(panel.LABEL_TTM_DIV) + 1
