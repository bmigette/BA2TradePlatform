"""The cached per-symbol market stats (yield, 1Y/3Y total return).

Asked for on 2026-09-05: the allocator's ⓘ tooltip should show a symbol's dividend
yield and 1Y/3Y return. The provider's own cache is an in-memory 24h TTL, so without
this table every restart re-fetches several FMP calls per symbol and a 35-symbol page
pays that on first render.
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.symbol_stats import (
    STALE_AFTER, load_symbol_stats, save_symbol_stats, stale_symbols,
)

# The package conftest shares ONE temp DB across the session, so each test uses its
# own symbol namespace rather than a cleanup hook.
_N = itertools.count(1)


@pytest.fixture
def sym():
    return lambda base: f"{base}{next(_N)}"


class TestRoundTrip:
    def test_every_field_survives(self, sym):
        s = sym("AAA")
        save_symbol_stats({s: {'dividend_yield_pct': 69.73, 'total_return_1y_pct': 34.95,
                               'total_return_3y_pct': 126.25, 'company_name': 'X ETF'}})
        row = load_symbol_stats([s])[s]
        assert row.dividend_yield_pct == pytest.approx(69.73)
        assert row.total_return_1y_pct == pytest.approx(34.95)
        assert row.total_return_3y_pct == pytest.approx(126.25)
        assert row.company_name == 'X ETF'
        assert row.fetched_at is not None

    def test_a_second_save_updates_rather_than_duplicating(self, sym):
        s = sym("BBB")
        save_symbol_stats({s: {'dividend_yield_pct': 1.0}})
        save_symbol_stats({s: {'dividend_yield_pct': 2.0}})
        rows = load_symbol_stats([s])
        assert len(rows) == 1 and rows[s].dividend_yield_pct == pytest.approx(2.0)

    def test_symbols_are_normalised(self, sym):
        s = sym("CCC")
        save_symbol_stats({f'  {s.lower()} ': {'dividend_yield_pct': 3.0}})
        assert s in load_symbol_stats([f' {s.lower()} '])


class TestUnknownIsNotZero:
    def test_a_non_payer_stores_a_real_zero(self, sym):
        s = sym("DDD")
        save_symbol_stats({s: {'dividend_yield_pct': 0.0}})
        assert load_symbol_stats([s])[s].dividend_yield_pct == 0.0

    def test_an_unfetched_figure_stays_None(self, sym):
        s = sym("EEE")
        save_symbol_stats({s: {'total_return_1y_pct': 5.0}})
        row = load_symbol_stats([s])[s]
        assert row.total_return_1y_pct == pytest.approx(5.0)
        assert row.total_return_3y_pct is None      # not 0.0

    def test_a_failed_fetch_is_stored_WITH_its_reason(self, sym):
        # Stored rather than left absent, so the next pass does not retry it at once
        # and the UI can say why instead of showing a blank.
        s = sym("FFF")
        save_symbol_stats({s: {'error': 'no entry for this symbol'}})
        row = load_symbol_stats([s])[s]
        assert row.error == 'no entry for this symbol'
        assert row.dividend_yield_pct is None


class TestStaleness:
    def test_a_symbol_with_no_row_is_stale(self, sym):
        assert stale_symbols([sym("GGG")])

    def test_a_fresh_row_is_not_stale(self, sym):
        s = sym("HHH")
        save_symbol_stats({s: {'dividend_yield_pct': 1.0}})
        assert stale_symbols([s]) == []

    def test_an_old_row_is_stale(self, sym):
        s = sym("III")
        save_symbol_stats({s: {'dividend_yield_pct': 1.0}})
        later = datetime.now(timezone.utc) + STALE_AFTER + timedelta(minutes=1)
        assert stale_symbols([s], now=later) == [s]

    def test_a_naive_timestamp_is_read_as_UTC_not_local(self, sym):
        # SQLite stores no zone; the row was written as UTC. Reading it as local time
        # would make every row look hours stale (or fresh) depending on the offset.
        s = sym("JJJ")
        save_symbol_stats({s: {'dividend_yield_pct': 1.0}})
        just_inside = datetime.now(timezone.utc) + STALE_AFTER - timedelta(minutes=5)
        assert stale_symbols([s], now=just_inside) == []

    def test_the_order_is_stable(self, sym):
        a, b = sym("KKK"), sym("LLL")
        assert stale_symbols([b, a]) == stale_symbols([a, b])

    def test_blanks_are_ignored(self):
        assert stale_symbols(['', '   ', None]) == []


class TestAbsence:
    def test_saving_nothing_writes_nothing(self):
        assert save_symbol_stats({}) == 0

    def test_asking_for_no_symbols_returns_nothing_not_everything(self, sym):
        s = sym("MMM")
        save_symbol_stats({s: {'dividend_yield_pct': 1.0}})
        assert load_symbol_stats([]) == {}

    def test_a_symbol_omitted_from_a_later_save_keeps_its_row(self, sym):
        a, b = sym("NNN"), sym("OOO")
        save_symbol_stats({a: {'dividend_yield_pct': 1.0}, b: {'dividend_yield_pct': 2.0}})
        save_symbol_stats({a: {'dividend_yield_pct': 9.0}})
        rows = load_symbol_stats([a, b])
        assert rows[b].dividend_yield_pct == pytest.approx(2.0)
