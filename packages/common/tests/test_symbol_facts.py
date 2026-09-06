"""Storing the broker's per-symbol facts, per account.

Asked for from live use on 2026-09-05: the allocator's symbol table could not say
whether a symbol was fractionable or what leverage it carried, so a rounded-down order
had no visible explanation on the page that produced it.
"""
import itertools

import pytest
from datetime import datetime, timezone

from ba2_common.core.account_types import MarginInfo
from ba2_common.core.symbol_facts import leverage_of, load_symbol_facts, save_symbol_facts


# The package conftest configures ONE temp DB for the whole session, so rows persist
# between tests. Each test gets its own account id rather than a cleanup hook: the
# table is keyed on (account_id, symbol), so a fresh id IS a fresh namespace, and it
# also exercises the per-account scoping the table exists for.
_NEXT_ACCOUNT = itertools.count(1000)


@pytest.fixture
def acct():
    return next(_NEXT_ACCOUNT)


def _info(symbol, **over):
    base = dict(symbol=symbol, bp_factor=1.0, marginable=True, fractionable=True,
                tradable=True, initial_margin_rate=0.5, maintenance_margin_rate=0.3,
                min_order_size=1.0, min_trade_increment=0.001,
                min_fractional_notional=None, source='asset')
    base.update(over)
    return MarginInfo(**base)


class TestLeverageOf:
    def test_a_reg_t_rate_is_two_to_one(self):
        assert leverage_of(0.5) == pytest.approx(2.0)

    def test_full_payment_is_one_times(self):
        assert leverage_of(1.0) == pytest.approx(1.0)

    def test_an_unpublished_rate_is_unknown_not_one(self):
        # 1.0 would state that the broker requires full payment. It said nothing.
        assert leverage_of(None) is None

    def test_a_nonsense_rate_is_refused_rather_than_dividing_to_infinity(self):
        assert leverage_of(0.0) is None
        assert leverage_of(-0.5) is None


class TestRoundTrip:
    def test_every_carried_field_survives(self, acct):
        save_symbol_facts(acct, {'AAPL': _info('AAPL')})
        row = load_symbol_facts(acct)['AAPL']
        assert (row.fractionable, row.marginable, row.tradable) == (True, True, True)
        assert row.bp_factor == pytest.approx(1.0)
        assert row.initial_margin_rate == pytest.approx(0.5)
        assert row.maintenance_margin_rate == pytest.approx(0.3)
        assert row.min_trade_increment == pytest.approx(0.001)
        assert row.source == 'asset'
        assert row.fetched_at is not None

    def test_the_three_flags_keep_all_three_states(self, acct):
        # The whole contract: False is the broker saying no, None is nobody saying.
        save_symbol_facts(acct, {
            'YES': _info('YES', fractionable=True, tradable=True),
            'NO': _info('NO', fractionable=False, tradable=False),
            'QUIET': _info('QUIET', fractionable=None, tradable=None),
        })
        rows = load_symbol_facts(acct)
        assert rows['YES'].fractionable is True
        assert rows['NO'].fractionable is False
        assert rows['QUIET'].fractionable is None
        assert rows['QUIET'].tradable is None

    def test_a_second_save_updates_rather_than_duplicating(self, acct):
        save_symbol_facts(acct, {'AAPL': _info('AAPL', fractionable=True)})
        save_symbol_facts(acct, {'AAPL': _info('AAPL', fractionable=False)})
        rows = load_symbol_facts(acct)
        assert len(rows) == 1
        assert rows['AAPL'].fractionable is False

    def test_facts_are_scoped_to_the_account(self, acct):
        # The same ticker is fractionable at one broker and not at another; that is
        # the reason this table is keyed on (account, symbol) at all.
        save_symbol_facts(acct, {'AAPL': _info('AAPL', fractionable=True)})
        save_symbol_facts(acct + 1, {'AAPL': _info('AAPL', fractionable=False)})
        assert load_symbol_facts(acct)['AAPL'].fractionable is True
        assert load_symbol_facts(acct + 1)['AAPL'].fractionable is False

    def test_symbols_are_normalised_on_the_way_in(self, acct):
        save_symbol_facts(acct, {' aapl ': _info('AAPL')})
        assert 'AAPL' in load_symbol_facts(acct)

    def test_lookup_normalises_too(self, acct):
        save_symbol_facts(acct, {'AAPL': _info('AAPL')})
        assert 'AAPL' in load_symbol_facts(acct, [' aapl '])


class TestAbsence:
    def test_a_symbol_never_fetched_is_simply_missing(self, acct):
        save_symbol_facts(acct, {'AAPL': _info('AAPL')})
        assert load_symbol_facts(acct, ['MSFT']) == {}

    def test_an_empty_answer_writes_nothing(self, acct):
        assert save_symbol_facts(acct, {}) == 0

    def test_a_symbol_omitted_from_a_later_fetch_keeps_its_last_answer(self, acct):
        # The broker omitting a symbol means "no answer this time", not "forget it".
        # Deleting here would turn one flaky lookup into permanent unknown.
        save_symbol_facts(acct, {'AAPL': _info('AAPL'), 'MSFT': _info('MSFT')})
        save_symbol_facts(acct, {'AAPL': _info('AAPL', fractionable=False)})
        rows = load_symbol_facts(acct)
        assert set(rows) == {'AAPL', 'MSFT'}
        assert rows['MSFT'].fractionable is True

    def test_asking_for_no_symbols_returns_nothing_rather_than_everything(self, acct):
        save_symbol_facts(acct, {'AAPL': _info('AAPL')})
        assert load_symbol_facts(acct, []) == {}
        assert load_symbol_facts(acct, ['  ']) == {}
