"""The ACCOUNT-WIDE stock exposure ceiling (2026-09-09 review, findings 2 and 3).

The margin design promises an account deploys "at most balance x margin_factor".
Nothing enforced that: ``get_tradable_balance`` published the ceiling and every
consumer then subtracted its OWN bookkeeping from it, so

  * finding 3 -- equity $10,000, factor 1.8, $18,000 already held elsewhere and
    $2,000 of broker buying power left: a fresh expert still reported $2,000
    available and its entry passed, taking the account to $20,000 -- the broker's
    gross capacity, not the platform's ceiling; and
  * finding 2 -- a PROFITABLE position is charged to the expert at entry cost, so at
    equity $11,800 holding 180 shares now worth $110 the expert reported $3,240 of
    room against $1,440 of real headroom.

Both are the same missing subtraction: ceiling - what the BROKER says is already
deployed - what already-working entries will add.

SCOPE: every line of this feature is reachable only with ``margin_enabled`` True.
Backtests always run with it off, so ``get_stock_exposure_headroom`` returns None
without reading the snapshot and the gate never fires -- pinned by the call-counter
test below, because "the backtest is unchanged" must be true by construction and not
by luck.

Log assertions monkeypatch the module's own ``logger`` rather than using ``caplog``:
``ba2_common.logger`` sets ``propagate = False``, so caplog sees nothing and every
log pin would pass vacuously.
"""
import logging
import threading
import time

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.interfaces.ReadOnlyAccountInterface import stock_exposure_headroom
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


ON = {"margin_enabled": True, "margin_factor": 1.8}
OFF = {"margin_enabled": False, "margin_factor": 1.8}


class _Acct(AccountInterface):
    """A trading account with every abstract stubbed, a canned snapshot and a fake
    broker leg, built bare (no ``__init__`` chain) exactly as
    ``test_margin_tradable_balance._Stub`` does."""

    def __init__(self, *, id_val, balance, snapshot, settings, price=100.0):
        self.id = id_val
        self._balance = balance
        self._snap = snapshot
        self._stored = settings
        self._price = price
        self.snapshot_calls = 0
        self.submitted = []
        self.impl_delay = 0.0
        self.on_impl = None

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    # --- read-only surface --------------------------------------------------
    def get_account_snapshot(self):
        self.snapshot_calls += 1
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {"buying_power": self._snap.buying_power}

    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
        # Overridden above the caching layer on purpose: the cache is global and
        # would leak a price between tests.
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: self._price for s in symbol_or_symbols}
        return self._price

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
        return self._price

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []

    # --- trading surface ----------------------------------------------------
    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        if self.impl_delay:
            time.sleep(self.impl_delay)
        if self.on_impl is not None:
            self.on_impl(self, trading_order)
        self.submitted.append(trading_order)
        return trading_order

    def cancel_order(self, order_id):
        return None

    def modify_order(self, order_id):
        return None

    def adjust_tp(self, transaction, new_tp_price, source=""):
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        return True

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        return True


def _snap(*, multiplier=2.0, buying_power=20_000.0, long_mv=0.0, short_mv=0.0, equity=None):
    return AccountSnapshot(margin_multiplier=multiplier, buying_power=buying_power,
                           long_market_value=long_mv, short_market_value=short_mv,
                           equity=equity)


def _order(acct, *, side=OrderDirection.BUY, qty=10.0, symbol="AAPL", **kw):
    return TradingOrder(account_id=acct.id, symbol=symbol, quantity=qty, side=side,
                        order_type=OrderType.MARKET, **kw)


@pytest.fixture
def records(monkeypatch):
    """``(levelno, message)`` for every line AccountInterface logs. See module docstring."""
    import sys

    module = sys.modules["ba2_common.core.interfaces.AccountInterface"]
    seen = []
    for name, level in (("debug", logging.DEBUG), ("info", logging.INFO),
                        ("warning", logging.WARNING), ("error", logging.ERROR)):
        monkeypatch.setattr(
            module.logger, name,
            lambda msg, *a, _lvl=level, **k: seen.append((_lvl, str(msg))))
    return seen


# ----- 1. the pure function -------------------------------------------------

def test_headroom_is_ceiling_minus_gross_minus_pending():
    assert stock_exposure_headroom(18_000.0, 19_800.0, 0.0) == -1_800.0
    assert stock_exposure_headroom(18_000.0, 12_000.0, 5_000.0) == 1_000.0
    assert stock_exposure_headroom(18_000.0, 0.0, 0.0) == 18_000.0


def test_headroom_is_not_clamped_at_zero():
    """A negative answer is the OVERSHOOT and must survive: clamping it to 0.0 would
    read as "exactly full", indistinguishable from a healthy fully-deployed account."""
    assert stock_exposure_headroom(10_000.0, 25_000.0, 0.0) == -15_000.0


# ----- 2. margin off is not merely equal: it is not reached -----------------

def test_margin_off_returns_none_without_reading_the_snapshot():
    acct = _Acct(id_val=911, balance=10_000.0, snapshot=_snap(long_mv=18_000.0), settings=OFF)
    assert acct.get_stock_exposure_headroom() is None
    assert acct.snapshot_calls == 0, "margin off must not cost a broker round trip"


# ----- 3. review finding 2 --------------------------------------------------

def test_finding_2_profitable_position_leaves_1440_of_headroom():
    """equity 11,800 x factor 1.8 = 21,240 ceiling; the broker marks the holding at
    19,800 (180 shares @ 110). The expert's own books charge it at COST (18,000) and
    so reported 3,240 -- the real room is 1,440."""
    acct = _Acct(id_val=912, balance=11_800.0,
                 snapshot=_snap(long_mv=19_800.0, buying_power=20_000.0), settings=ON)
    assert acct.get_stock_exposure_headroom() == pytest.approx(1_440.0)


def test_a_short_position_is_exposure_not_a_credit():
    """short_market_value is NEGATIVE per the snapshot contract; a short still CONSUMES
    the ceiling, so it is added by magnitude."""
    acct = _Acct(id_val=913, balance=10_000.0,
                 snapshot=_snap(long_mv=8_000.0, short_mv=-5_000.0), settings=ON)
    assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0 - 13_000.0)


# ----- 4. review finding 3 --------------------------------------------------

def _finding_3_account(id_val=914):
    return _Acct(id_val=id_val, balance=10_000.0,
                 snapshot=_snap(long_mv=18_000.0, buying_power=2_000.0), settings=ON)


def test_finding_3_a_full_account_has_zero_headroom():
    assert _finding_3_account().get_stock_exposure_headroom() == pytest.approx(0.0)


def test_finding_3_a_new_entry_is_refused_at_the_ceiling(records):
    acct = _finding_3_account(id_val=915)
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, qty=10.0))
    assert acct.submitted == [], "the order must never reach the broker"
    assert any(lvl == logging.ERROR and "stock exposure ceiling" in msg for lvl, msg in records)


def test_the_refusal_names_ceiling_gross_pending_and_the_remedy():
    acct = _finding_3_account(id_val=916)
    with ts.inmem_trades():
        with pytest.raises(ValueError) as excinfo:
            acct.submit_order(_order(acct, qty=10.0))
    message = str(excinfo.value)
    for fragment in ("$1,000.00", "$0.00", "$18,000.00", "factor 1.8", "margin_factor"):
        assert fragment in message, f"{fragment!r} missing from: {message}"


def test_a_sell_that_reduces_an_existing_long_is_not_refused():
    acct = _finding_3_account(id_val=917)
    with ts.inmem_trades():
        txn_id = add_instance(Transaction(symbol="AAPL", quantity=100.0,
                                          side=OrderDirection.BUY,
                                          status=TransactionStatus.OPENED, open_price=100.0))
        add_instance(_order(acct, qty=100.0, status=OrderStatus.FILLED, transaction_id=txn_id))
        result = acct.submit_order(
            _order(acct, side=OrderDirection.SELL, qty=10.0, transaction_id=txn_id))
    assert result is not None
    assert len(acct.submitted) == 1


def test_a_closing_order_is_never_refused():
    acct = _finding_3_account(id_val=918)
    with ts.inmem_trades():
        acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0),
                          is_closing_order=True)
    assert len(acct.submitted) == 1


def test_an_option_order_is_not_measured_against_the_stock_ceiling():
    acct = _finding_3_account(id_val=919)
    with ts.inmem_trades():
        acct.submit_order(_order(acct, qty=1.0, asset_class=AssetClass.OPTION,
                                 contract_symbol="AAPL260116C00100000", multiplier=100))
    assert len(acct.submitted) == 1


# ----- 5. pending broker-working entries ------------------------------------

def _with_entry_order(acct, **order_kw):
    """A working BUY entry of 50 @ 100 on ``acct``; returns its id."""
    txn_id = add_instance(Transaction(symbol="MSFT", quantity=50.0, side=OrderDirection.BUY,
                                      status=TransactionStatus.WAITING, open_price=100.0))
    kw = dict(symbol="MSFT", qty=50.0, status=OrderStatus.NEW, transaction_id=txn_id,
              broker_order_id="brk-1", limit_price=100.0)
    kw.update(order_kw)
    return add_instance(_order(acct, **kw))


def test_a_working_entry_order_consumes_headroom():
    acct = _Acct(id_val=920, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)
        _with_entry_order(acct)
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_the_order_under_validation_is_excluded_from_its_own_pending_total():
    acct = _Acct(id_val=921, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        order_id = _with_entry_order(acct)
        assert acct.get_stock_exposure_headroom(exclude_order_id=order_id) == pytest.approx(18_000.0)


def test_a_protective_leg_is_not_an_entry():
    acct = _Acct(id_val=922, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        parent_id = _with_entry_order(acct, status=OrderStatus.FILLED, broker_order_id="brk-p")
        _with_entry_order(acct, side=OrderDirection.SELL, depends_on_order=parent_id,
                          depends_order_status_trigger=OrderStatus.FILLED,
                          broker_order_id="brk-leg")
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)


def test_a_filled_order_is_not_pending_it_is_already_in_the_market_value():
    acct = _Acct(id_val=923, balance=10_000.0, snapshot=_snap(long_mv=5_000.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.FILLED)
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_an_order_that_never_reached_the_broker_is_not_pending():
    acct = _Acct(id_val=924, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, broker_order_id=None)
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)


def test_a_partially_filled_entry_counts_only_its_remaining_quantity():
    acct = _Acct(id_val=925, balance=10_000.0, snapshot=_snap(long_mv=2_000.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PARTIALLY_FILLED, filled_qty=20.0)
        # 18,000 ceiling - 2,000 already held - 30 remaining x 100
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_another_accounts_working_entry_does_not_count():
    acct = _Acct(id_val=926, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    other = _Acct(id_val=927, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(other)
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)


# ----- 6. an unreadable exposure is a refusal, never a pass -----------------

def test_no_published_market_value_raises():
    acct = _Acct(id_val=928, balance=10_000.0,
                 snapshot=_snap(long_mv=None), settings=ON)
    with pytest.raises(ValueError, match="no long/short market value"):
        acct.get_stock_exposure_headroom()


def test_a_non_finite_market_value_raises():
    acct = _Acct(id_val=929, balance=10_000.0,
                 snapshot=_snap(long_mv=float("nan")), settings=ON)
    with pytest.raises(ValueError, match="non-finite"):
        acct.get_stock_exposure_headroom()


def test_the_gate_refuses_rather_than_skipping_when_exposure_cannot_be_read(records):
    acct = _Acct(id_val=930, balance=10_000.0, snapshot=_snap(long_mv=None), settings=ON)
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="Cannot validate account exposure"):
            acct.submit_order(_order(acct, qty=1.0))
    assert acct.submitted == [], "an unrun risk check must not open a position"
    assert any(lvl == logging.ERROR and "ACCOUNT EXPOSURE VALIDATION CANNOT RUN" in msg
               for lvl, msg in records)


# ----- 7 and 8. the per-account submit lock ---------------------------------

def test_two_concurrent_entries_cannot_both_spend_the_same_headroom():
    """Without the lock both threads read the SAME pre-trade snapshot, both see
    $12,000 of room, and the account ends at $26,000 against an $18,000 ceiling."""
    acct = _Acct(id_val=931, balance=10_000.0,
                 snapshot=_snap(long_mv=6_000.0, buying_power=20_000.0), settings=ON)
    acct.impl_delay = 0.05

    def _fill(account, order):
        # the broker marks the new shares as soon as the order is accepted
        account._snap.long_market_value += order.quantity * 100.0

    acct.on_impl = _fill

    outcomes = {}

    def _go(tag):
        try:
            acct.submit_order(_order(acct, qty=100.0, symbol=f"SYM{tag}"))
            outcomes[tag] = "submitted"
        except ValueError as e:
            outcomes[tag] = f"refused: {e}"

    threads = [threading.Thread(target=_go, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "submit_order deadlocked"

    submitted = [tag for tag, out in outcomes.items() if out == "submitted"]
    refused = [tag for tag, out in outcomes.items() if out != "submitted"]
    assert len(submitted) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert "stock exposure ceiling" in outcomes[refused[0]]
    assert acct._snap.long_market_value == pytest.approx(16_000.0)


def test_the_submit_lock_is_reentrant_so_a_leg_does_not_deadlock():
    acct = _Acct(id_val=932, balance=10_000.0,
                 snapshot=_snap(long_mv=0.0, buying_power=20_000.0), settings=ON)
    legs = []

    def _submit_a_leg(account, order):
        if legs:
            return
        legs.append(order)
        # re-enters submit_order on the SAME thread, exactly as the TP/SL block does
        account.submit_order(_order(account, side=OrderDirection.SELL, qty=1.0),
                             is_closing_order=True)

    acct.on_impl = _submit_a_leg
    done = threading.Event()

    def _go():
        acct.submit_order(_order(acct, qty=10.0))
        done.set()

    thread = threading.Thread(target=_go)
    thread.start()
    thread.join(timeout=30)
    assert done.is_set(), "a re-entrant leg submission deadlocked the account lock"
    assert len(acct.submitted) == 2
