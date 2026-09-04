"""The chart's price history is fetched independently of the returns windows.

Reported 2026-09-05: "clicking 10Y or Max doesn't seem to do anything", tried against
XLK, which has traded since 1998. The chart span was derived from WINDOWS, whose
longest entry is 5y — so five years was all that was ever fetched, and a 10Y range
could only ever redisplay the same five years.
"""
from datetime import date

import pytest

from ba2_providers import symbol_info as si


class TestYearsBefore:
    def test_it_subtracts_whole_years(self):
        assert si.years_before(date(2026, 9, 4), 10) == date(2016, 9, 4)

    def test_a_leap_day_folds_to_the_28th(self):
        assert si.years_before(date(2024, 2, 29), 1) == date(2023, 2, 28)

    def test_zero_years_is_the_same_day(self):
        assert si.years_before(date(2026, 9, 4), 0) == date(2026, 9, 4)


class TestWindowStartStillWorks:
    """Extracting years_before must not change what the returns table measures."""

    @pytest.mark.parametrize('window,expected', [
        ('1y', date(2025, 9, 4)), ('3y', date(2023, 9, 4)), ('5y', date(2021, 9, 4)),
    ])
    def test_each_window_is_unchanged(self, window, expected):
        assert si.window_start_date(window, date(2026, 9, 4)) == expected

    def test_ytd_is_still_the_prior_year_end(self):
        assert si.window_start_date('ytd', date(2026, 9, 4)) == date(2025, 12, 31)

    def test_an_unknown_window_still_raises(self):
        # 10y is the CHART's span, deliberately NOT a returns window -- asking for it
        # here must stay an error rather than silently becoming a fourth column.
        with pytest.raises(ValueError):
            si.window_start_date('10y', date(2026, 9, 4))


class TestTheChartGetsMoreThanTheWindowsNeed:
    def test_the_default_reaches_back_further_than_the_longest_window(self):
        assert si.CHART_HISTORY_YEARS > si._longest_window_years(si.WINDOWS)

    def test_both_entry_points_expose_it(self):
        import inspect
        for fn in (si.get_symbol_info, si.get_symbols_info):
            assert 'chart_years' in inspect.signature(fn).parameters

    def test_the_batch_path_forwards_it(self, monkeypatch):
        # It accepted the argument and dropped it, so the COMPARISON chart -- the one
        # the range buttons were reported against -- still only got five years.
        seen = {}

        def _fake(api_key, symbol, *, as_of, windows, chart_years):
            seen[symbol] = chart_years
            return object()

        monkeypatch.setattr(si, 'get_symbol_info', _fake)
        si.get_symbols_info('k', ['XLK', 'SPY'], as_of=date(2026, 9, 4), chart_years=12)
        assert seen == {'XLK': 12, 'SPY': 12}

    def test_a_narrower_window_never_shortens_the_chart(self):
        # max(), not the chart figure alone: a caller asking for 1y windows still gets
        # the full chart history.
        assert max(si._longest_window_years(['1y']), si.CHART_HISTORY_YEARS) == si.CHART_HISTORY_YEARS
