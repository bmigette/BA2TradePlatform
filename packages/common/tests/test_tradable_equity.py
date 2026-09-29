"""``get_tradable_equity``: the account's EQUITY (cash + marked positions) in deployable dollars.

WHY IT EXISTS (2026-09-28). A rebalancing expert sizes a TARGET BOOK -- "this name should be
w% of what I own" -- so it needs what the account is worth, not what it has left to spend.
``get_tradable_balance`` reads ``get_balance()``, which is equity at every live broker and
CASH on the backtest account ("finding 6", deliberately unchanged for the classic RM). Sized
on cash, FactorRanker's second rebalance saw a fully invested $100k book as $279 and sold all
of it; live, it kept the book. This accessor reads the one figure both runtimes publish with
the same meaning, ``get_account_snapshot().equity``, and applies margin exactly as
``get_tradable_balance`` does.

The stub is the ``_Stub`` of test_margin_finite_inputs.py: no DB, no broker.
"""
import math

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Stub(ReadOnlyAccountInterface):
    def __init__(self, *, balance, snapshot, settings):
        self.id = 5
        self._balance = balance
        self._snap = snapshot
        self._stored = settings

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        return None

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []


ON = {"margin_enabled": True, "margin_factor": 2.0}
OFF = {"margin_enabled": False, "margin_factor": 2.0}


def _snap(*, equity, cash, multiplier=2.0, buying_power=None):
    return AccountSnapshot(cash=cash, equity=equity, net_liquidation=equity,
                           buying_power=buying_power if buying_power is not None else cash,
                           margin_multiplier=multiplier, long_market_value=equity - cash,
                           short_market_value=0.0)


def test_margin_off_it_is_the_published_equity_not_the_balance():
    """The backtest shape: get_balance() is the $279 of cash left after investing, the
    snapshot's equity is the $100,000 book. The book is what a rebalance sizes."""
    acct = _Stub(balance=279.0, snapshot=_snap(equity=100_000.0, cash=279.0), settings=OFF)

    assert acct.get_tradable_balance() == pytest.approx(279.0)     # unchanged (finding 6)
    assert acct.get_tradable_equity() == pytest.approx(100_000.0)


def test_at_a_live_broker_it_equals_the_tradable_balance():
    """Live, get_balance() IS equity, so the new accessor moves nothing there."""
    acct = _Stub(balance=50_000.0, snapshot=_snap(equity=50_000.0, cash=10_000.0), settings=OFF)

    assert acct.get_tradable_equity() == acct.get_tradable_balance() == pytest.approx(50_000.0)


def test_margin_on_applies_the_same_effective_factor_as_the_tradable_balance():
    acct = _Stub(balance=2_000.0,
                 snapshot=_snap(equity=2_000.0, cash=1_000.0, multiplier=2.0, buying_power=2_000.0),
                 settings=ON)

    assert acct.get_tradable_equity() == pytest.approx(4_000.0)
    assert acct.get_tradable_equity() == pytest.approx(acct.get_tradable_balance())


@pytest.mark.parametrize("bad", [None, math.nan, math.inf])
def test_an_unpublished_or_non_finite_equity_is_refused(bad):
    """Unknown is not zero and not the cash figure: a guessed book is a guessed trade."""
    acct = _Stub(balance=1_000.0, snapshot=_snap(equity=1_000.0, cash=1_000.0), settings=OFF)
    acct._EQUITY_RETRY_DELAY_S = 0.0
    acct._snap.equity = bad

    with pytest.raises(ValueError, match="equity"):
        acct.get_tradable_equity()


class _FlakyStub(_Stub):
    """A broker whose first snapshot read fails (all-None, as Alpaca returns it)."""

    def __init__(self, *, reads, **kw):
        super().__init__(**kw)
        self._reads = list(reads)
        self.snapshot_reads = 0

    def get_account_snapshot(self):
        self.snapshot_reads += 1
        return self._reads.pop(0) if self._reads else self._snap


@pytest.mark.parametrize("settings", [OFF, ON], ids=["margin-off", "margin-on"])
def test_a_failed_snapshot_read_gets_one_retry_like_get_balance(settings):
    good = _snap(equity=2_000.0, cash=1_000.0, multiplier=2.0, buying_power=2_000.0)
    acct = _FlakyStub(reads=[AccountSnapshot()], balance=2_000.0, snapshot=good,
                      settings=settings)
    acct._EQUITY_RETRY_DELAY_S = 0.0

    expected = 4_000.0 if settings is ON else 2_000.0
    assert acct.get_tradable_equity() == pytest.approx(expected)
    assert acct.snapshot_reads == 2


def test_two_failed_reads_raise_and_serve_nothing_stale():
    acct = _FlakyStub(reads=[AccountSnapshot(), AccountSnapshot()], balance=2_000.0,
                      snapshot=_snap(equity=2_000.0, cash=2_000.0), settings=OFF)
    acct._EQUITY_RETRY_DELAY_S = 0.0

    with pytest.raises(ValueError, match="published no equity"):
        acct.get_tradable_equity()
    assert acct.snapshot_reads == 2
