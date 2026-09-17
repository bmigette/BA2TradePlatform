"""TastyTrade's own open P/L, instead of one the platform re-derives from mid-quotes.

MEASURED on the live account, 2026-09-17, against the broker's own position screen:

    cost basis      ours 7,635.40   broker 7,635.40    identical to the cent
    market value    ours 7,546.76   broker 7,479.85    +66.91
    floating P/L    ours   -88.64   broker   -155.55   +66.91

Every cost basis agreed exactly, so the whole gap is VALUATION. It comes from deriving each
position's value as ``mark_price x qty`` -- ``mark_price`` being the bid/ask MIDPOINT. On a
book of thin ETFs the midpoint is not where the broker marks: CAS, quoted 22.55/37.99 with
ZERO volume that day, carried a mark of 29.82 against the broker's 25.08 and moved the
account total by $18.96 on its own.

TastyTrade publishes no open-P/L field anywhere -- not on a position, not on the balances
(verified against the SDK models, tastytrade 12.4.1: positions carry only `realized_day_gain`
and `realized_today`; AccountBalance has no gain/profit/unrealized field at all). What it does
publish is `long_equity_value`, its own valuation of the equity book -- which differed from
the platform's own mid-quote sum by $31, proving the broker does not mark at its own
`mark_price` either.
"""
from types import SimpleNamespace

import pytest

from tastytrade.order import InstrumentType as TTInstrumentType

from tests.test_tastytrade_account import _bare_account


def _pos(symbol, qty, avg, *, instrument=TTInstrumentType.EQUITY, multiplier=1, mark=None):
    return SimpleNamespace(symbol=symbol, quantity=qty, average_open_price=avg,
                           instrument_type=instrument, multiplier=multiplier,
                           mark_price=mark, close_price=None)


def _wire(acct, *, positions, long_equity, short_equity=0):
    balances = SimpleNamespace(long_equity_value=long_equity, short_equity_value=short_equity)

    async def _balances(_session):
        return balances

    async def _positions(_session, include_marks=False):
        return list(positions)

    acct._check_authentication = lambda: True
    acct._account.get_balances = _balances
    acct._account.get_positions = _positions
    return acct


class TestTheBrokersOwnNumber:
    def test_it_is_the_brokers_equity_value_minus_the_cost_basis(self):
        acct = _wire(_bare_account(),
                     positions=[_pos("CAS", 4, 25.66, mark=29.825),
                                _pos("PSQL", 5, 7.8682, mark=7.395)],
                     long_equity=200.00)
        # cost basis = 4*25.66 + 5*7.8682 = 102.64 + 39.341 = 141.981
        assert acct.get_broker_floating_pl() == pytest.approx(200.00 - 141.981)

    def test_it_ignores_mark_price_entirely(self):
        """THE DEFECT: mark_price is the bid/ask midpoint and is not the broker's mark.

        Same book, same cost basis, wildly different marks -- the answer must not move,
        because the valuation now comes from the broker rather than from the quotes.
        """
        answers = []
        for mark in (29.825, 22.55, 37.99):
            acct = _wire(_bare_account(), positions=[_pos("CAS", 4, 25.66, mark=mark)],
                         long_equity=100.34)
            answers.append(acct.get_broker_floating_pl())
        assert answers[0] == answers[1] == answers[2] == pytest.approx(100.34 - 102.64)
        assert answers[0] == pytest.approx(-2.30, abs=0.01), "the broker's own CAS figure"

    def test_options_are_excluded_from_the_cost_basis(self):
        """long_equity_value covers equity only; an option's cost basis differenced against
        it would show as P/L that never existed."""
        acct = _wire(_bare_account(),
                     positions=[_pos("CAS", 4, 25.66),
                                _pos("SPY  260116C00500000", 2, 3.50,
                                     instrument=TTInstrumentType.EQUITY_OPTION, multiplier=100)],
                     long_equity=100.34)
        assert acct.get_broker_floating_pl() == pytest.approx(100.34 - 102.64)

    def test_the_multiplier_and_absolute_quantity_are_honoured(self):
        acct = _wire(_bare_account(), positions=[_pos("X", -3, 10.0, multiplier=2)],
                     long_equity=0.0)
        assert acct.get_broker_floating_pl() == pytest.approx(-60.0)

    def test_a_zero_quantity_row_contributes_nothing(self):
        acct = _wire(_bare_account(),
                     positions=[_pos("CAS", 4, 25.66), _pos("GONE", 0, 999.0)],
                     long_equity=100.34)
        assert acct.get_broker_floating_pl() == pytest.approx(100.34 - 102.64)


class TestItRefusesRatherThanGuesses:
    def test_short_equity_defers_instead_of_risking_a_sign_error(self):
        """A sign error here would not look wrong -- it would look like a P/L."""
        acct = _wire(_bare_account(), positions=[_pos("CAS", 4, 25.66)],
                     long_equity=100.34, short_equity=250.0)
        assert acct.get_broker_floating_pl() is None

    def test_a_missing_equity_value_is_unknown_not_zero(self):
        acct = _wire(_bare_account(), positions=[_pos("CAS", 4, 25.66)], long_equity=None)
        assert acct.get_broker_floating_pl() is None

    def test_a_failed_fetch_is_unknown_not_zero(self):
        acct = _bare_account()
        acct._check_authentication = lambda: True

        async def _boom(*a, **k):
            raise RuntimeError("TastyTrade 503")

        acct._account.get_balances = _boom
        acct._account.get_positions = _boom
        assert acct.get_broker_floating_pl() is None

    def test_unauthenticated_is_unknown_not_zero(self):
        acct = _wire(_bare_account(), positions=[], long_equity=100.0)
        acct._check_authentication = lambda: False
        assert acct.get_broker_floating_pl() is None

    def test_a_flat_account_is_zero_not_none(self):
        """Genuinely flat is a MEASURED zero and must not be confused with unknown."""
        acct = _wire(_bare_account(), positions=[], long_equity=0.0)
        assert acct.get_broker_floating_pl() == pytest.approx(0.0)


def test_every_other_broker_still_answers_none_by_default():
    """The hook is opt-in: the base returns None so Alpaca et al. keep summing per-position."""
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    assert ReadOnlyAccountInterface.get_broker_floating_pl(object()) is None
