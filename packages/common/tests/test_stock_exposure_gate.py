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
from ba2_common.core.db import add_instance, update_instance
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
        # The broker's position book. A LIST is a successful fetch (``[]`` == flat);
        # ``None`` is the tri-state FETCH FAILURE -- see get_positions below.
        self.positions = []
        self.position_calls = 0
        # The BROKER's order book, keyed by broker_order_id. Empty means "the broker
        # agrees with the local row" -- get_order below echoes it. A test that needs the
        # broker to disagree (a fill the local row has not learned about) registers the
        # broker's version here; ``None`` models a fetch the broker could not answer.
        self.broker_orders = {}
        self.broker_order_reads = []

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
        self.position_calls += 1
        return self.positions

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        """The BROKER's view of ONE order, by broker id.

        Default: echo the local row, i.e. a broker in step with the platform -- which is
        what every pre-existing case in this file assumes, so their pending totals are
        unchanged. ``broker_orders`` overrides one id to model the gap the ceiling has to
        survive: the broker has filled it, the local row has not been refreshed yet.
        """
        self.broker_order_reads.append(order_id)
        if order_id in self.broker_orders:
            return self.broker_orders[order_id]
        matches = ts.orders_where(account_id=self.id, broker_order_id=order_id)
        return matches[0] if matches else None

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


@pytest.fixture
def account_records(monkeypatch):
    """The same, for ReadOnlyAccountInterface -- where the pending-entry diagnostics live."""
    import sys

    module = sys.modules["ba2_common.core.interfaces.ReadOnlyAccountInterface"]
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


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_invalid_entry_quote_is_refused_before_submission(bad):
    acct = _Acct(id_val=960, balance=10_000.0, snapshot=_snap(), settings=ON, price=bad)
    with ts.inmem_trades(), pytest.raises(ValueError, match="usable price"):
        acct.submit_order(_order(acct))
    assert acct.submitted == []


@pytest.mark.parametrize("field", ["limit_price", "quantity", "filled_qty"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_invalid_pending_entry_cannot_poison_headroom(field, bad):
    acct = _Acct(id_val=961, balance=10_000.0, snapshot=_snap(), settings=ON)
    with ts.inmem_trades():
        order_id = _with_entry_order(acct)
        order = ts.get_or_none(TradingOrder, order_id)
        setattr(order, field, bad)
        update_instance(order)
        with pytest.raises(ValueError, match="Cannot validate account exposure"):
            acct.submit_order(_order(acct))
    assert acct.submitted == []


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


def test_a_naked_sell_that_opens_a_short_is_gated():
    """A short is EXPOSURE, not a credit. The gate must not read "SELL" as "reducing":
    an opening SELL carries no transaction yet, exactly like an opening BUY."""
    acct = _finding_3_account(id_val=933)
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert acct.submitted == []


def test_adding_to_an_existing_same_side_position_is_gated():
    """The top-up path: a BUY against a transaction that is already long ADDS exposure,
    so it faces the ceiling like any other opening order."""
    acct = _finding_3_account(id_val=934)
    with ts.inmem_trades():
        txn_id = add_instance(Transaction(symbol="AAPL", quantity=100.0,
                                          side=OrderDirection.BUY,
                                          status=TransactionStatus.OPENED, open_price=100.0))
        add_instance(_order(acct, qty=100.0, status=OrderStatus.FILLED, transaction_id=txn_id))
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, qty=10.0, transaction_id=txn_id))
    assert acct.submitted == []


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


def test_a_working_short_entry_consumes_headroom_too():
    """The pending sum reads Transaction.side, so a SELL against a SHORT transaction is an
    ENTRY. Counting it as a reduction would hand a short seller unlimited room."""
    acct = _Acct(id_val=940, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        txn_id = add_instance(Transaction(symbol="MSFT", quantity=50.0,
                                          side=OrderDirection.SELL,
                                          status=TransactionStatus.WAITING, open_price=100.0))
        add_instance(_order(acct, symbol="MSFT", side=OrderDirection.SELL, qty=50.0,
                            status=OrderStatus.NEW, transaction_id=txn_id,
                            broker_order_id="brk-s", limit_price=100.0))
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_an_option_order_is_not_counted_in_the_pending_sum():
    """Options consume the same equity, but they are already in the broker's marked
    exposure once filled, and an unfilled one is sized by the OPTION sleeve -- pricing a
    contract at its underlying's quote (x quantity, no multiplier) would be a fiction."""
    acct = _Acct(id_val=941, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, asset_class=AssetClass.OPTION,
                          contract_symbol="MSFT260116C00100000", multiplier=100)
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)


def test_a_market_pending_entry_is_priced_from_the_current_quote():
    acct = _Acct(id_val=935, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON,
                 price=80.0)
    with ts.inmem_trades():
        _with_entry_order(acct, limit_price=None)      # a MARKET order has no limit price
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0 - 50 * 80.0)


@pytest.mark.parametrize("bad_price", [None, 0.0])
def test_an_unpriceable_pending_entry_raises_rather_than_being_skipped(bad_price):
    """Skipping it would understate the pending total -- a ceiling that fails OPEN, which
    is the whole failure mode this feature exists to close."""
    acct = _Acct(id_val=936, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON,
                 price=bad_price)
    with ts.inmem_trades():
        _with_entry_order(acct, limit_price=None)
        with pytest.raises(ValueError, match="no usable price for working order"):
            acct.get_stock_exposure_headroom()


def test_the_gate_refuses_when_a_pending_entry_cannot_be_priced(records):
    acct = _Acct(id_val=937, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON,
                 price=None)
    with ts.inmem_trades():
        _with_entry_order(acct, limit_price=None)
        with pytest.raises(ValueError, match="Cannot validate account exposure"):
            acct.submit_order(_order(acct, qty=1.0))
    assert acct.submitted == []
    assert any(lvl == logging.ERROR and "ACCOUNT EXPOSURE VALIDATION CANNOT RUN" in msg
               for lvl, msg in records)


def test_an_orphaned_transaction_is_counted_as_an_entry_and_says_so(account_records):
    """It can only REDUCE headroom, so the conservative reading is the safe one -- but a
    dangling transaction_id is a data defect and must not pass in silence."""
    acct = _Acct(id_val=938, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        order_id = _with_entry_order(acct)
        order = ts.store_get(TradingOrder, order_id)
        order.transaction_id = 424242            # a transaction that does not exist
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)
    assert any(lvl == logging.WARNING and "does not exist" in msg
               for lvl, msg in account_records)


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


def test_an_ibkr_shaped_account_refuses_at_the_multiplier_not_the_market_value():
    """IBKR publishes neither a margin multiplier nor market values. Margin on IBKR is the
    documented UNSUPPORTED path and was already refused before this feature existed:
    ``_stock_multiplier_from`` raises first, so the operator gets the message that names
    the real problem instead of a market-value complaint about a broker whose multiplier
    was never readable. This pins WHERE the refusal happens, not just that it happens."""
    acct = _Acct(id_val=939, balance=10_000.0,
                 snapshot=_snap(multiplier=None, buying_power=None,
                                long_mv=None, short_mv=None), settings=ON)
    with pytest.raises(ValueError, match="no usable stock margin multiplier"):
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


def test_two_concurrent_entries_serialise_through_the_persisted_order_row():
    """The same race by the PRODUCTION mechanism. Live, the broker does not re-mark the
    account between two submissions milliseconds apart; what actually changes is that the
    first order is now a WORKING row with a broker_order_id, which the pending sum picks
    up. This is therefore the variant that proves the pending term -- not just the lock --
    is what closes the race."""
    acct = _Acct(id_val=942, balance=10_000.0,
                 snapshot=_snap(long_mv=6_000.0, buying_power=20_000.0), settings=ON)
    acct.impl_delay = 0.05

    def _accept_at_broker(account, order):
        order.broker_order_id = f"brk-{order.id}"
        order.status = OrderStatus.NEW
        update_instance(order)

    acct.on_impl = _accept_at_broker

    outcomes = {}

    def _go(tag):
        try:
            acct.submit_order(_order(acct, qty=100.0, symbol=f"PSYM{tag}"))
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
    assert "pending $10,000.00" in outcomes[refused[0]], outcomes[refused[0]]


def test_the_pending_sum_finds_working_orders_on_the_real_sql_path():
    """Everything above runs inside ``inmem_trades()`` (the backtest store). LIVE takes the
    other half of ``orders_where``: a real ``status IN (...)`` / ``depends_on_order IS NULL``
    SELECT. A filter that is right in Python and wrong in SQL would be a ceiling that only
    works in tests, so the SQL branch gets its own pin."""
    acct = _Acct(id_val=943, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)

    txn_id = add_instance(Transaction(symbol="SQLA", quantity=50.0, side=OrderDirection.BUY,
                                      status=TransactionStatus.WAITING, open_price=100.0))
    entry_id = add_instance(_order(acct, symbol="SQLA", qty=50.0, status=OrderStatus.NEW,
                                   transaction_id=txn_id, broker_order_id="brk-sql",
                                   limit_price=100.0))
    # A protective leg (excluded by depends_on_order IS NULL) and a filled entry (excluded
    # by the status IN list) -- the two predicates the SQL path has to get right.
    add_instance(_order(acct, symbol="SQLA", side=OrderDirection.SELL, qty=50.0,
                        status=OrderStatus.NEW, transaction_id=txn_id,
                        broker_order_id="brk-sql-leg", limit_price=90.0,
                        depends_on_order=entry_id,
                        depends_order_status_trigger=OrderStatus.FILLED))
    add_instance(_order(acct, symbol="SQLA", qty=99.0, status=OrderStatus.FILLED,
                        transaction_id=txn_id, broker_order_id="brk-sql-done",
                        limit_price=100.0))

    assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)
    assert acct.get_stock_exposure_headroom(exclude_order_id=entry_id) == pytest.approx(18_000.0)


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


# ----- 9. an order with NO transaction: the untracked-close path ------------
#
# ``portfolio_allocation_service._sell_untracked_symbol`` sells a broker holding the
# platform has no transaction for. It passes is_closing_order=True on the FIRST submit,
# but every re-submit path that derives the flag from the transaction (the wash-trade
# retry, the UI's manual submit) gets False -- there is no transaction to read. Such an
# order then reached this gate looking exactly like a naked short and was refused on an
# account over its ceiling, stranding the row PENDING with no broker_order_id: the
# platform refusing to let an over-exposed account reduce its exposure.
#
# The broker's own book is the evidence, and the ONLY evidence available here.


def test_an_untracked_sell_into_a_long_the_broker_holds_is_not_gated():
    acct = _finding_3_account(id_val=944)
    acct.positions = [{"symbol": "AAPL", "qty": "10"}]
    with ts.inmem_trades():
        result = acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert result is not None
    assert len(acct.submitted) == 1


def test_an_untracked_buy_that_covers_a_short_the_broker_holds_is_not_gated():
    """The mirror: a short reports a NEGATIVE quantity, and buying it back reduces."""
    acct = _finding_3_account(id_val=945)
    acct.positions = [{"symbol": "AAPL", "qty": -10.0}]
    with ts.inmem_trades():
        acct.submit_order(_order(acct, side=OrderDirection.BUY, qty=10.0))
    assert len(acct.submitted) == 1


def test_an_untracked_sell_with_no_position_opens_a_short_and_is_gated():
    acct = _finding_3_account(id_val=946)
    acct.positions = []                       # a SUCCESSFUL fetch: genuinely flat
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert acct.submitted == []


def test_an_untracked_sell_larger_than_the_holding_is_gated():
    """20 against a long 10: ten shares reduce, the other ten OPEN a short. The order is
    one instruction and cannot be half-skipped, so it faces the ceiling as the (partial)
    open it is."""
    acct = _finding_3_account(id_val=947)
    acct.positions = [{"symbol": "AAPL", "qty": 10.0}]
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=20.0))
    assert acct.submitted == []


def test_an_untracked_sell_on_a_same_side_holding_is_gated():
    """Selling while already SHORT extends the short; the broker's book says nothing
    that makes it a reduction."""
    acct = _finding_3_account(id_val=948)
    acct.positions = [{"symbol": "AAPL", "qty": -5.0}]
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="stock exposure ceiling"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert acct.submitted == []


def test_an_unreadable_position_book_is_a_refusal_not_an_assumed_flat(records):
    """``get_positions()`` is TRI-STATE and ``None`` is a FETCH FAILURE. Reading it as
    "flat" would gate genuine reductions; reading it as "held" would wave through
    genuine opens. Neither is available, so the order is refused and says why."""
    acct = _finding_3_account(id_val=949)
    acct.positions = None
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="position book is unreadable"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert acct.submitted == []
    assert any(lvl == logging.ERROR and "ACCOUNT EXPOSURE VALIDATION CANNOT RUN" in msg
               for lvl, msg in records)


def test_a_position_row_with_no_readable_quantity_is_a_refusal():
    """A position the broker CONFIRMS but cannot size is not a flat one."""
    acct = _finding_3_account(id_val=950)
    acct.positions = [{"symbol": "AAPL", "qty": None}]
    with ts.inmem_trades():
        with pytest.raises(ValueError, match="position book is unreadable"):
            acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0))
    assert acct.submitted == []


def test_a_transaction_backed_order_never_reads_the_position_book():
    """The transaction answers the question, and the broker round trip is only paid on
    the path that has no other evidence."""
    acct = _finding_3_account(id_val=951)
    with ts.inmem_trades():
        txn_id = add_instance(Transaction(symbol="AAPL", quantity=100.0,
                                          side=OrderDirection.BUY,
                                          status=TransactionStatus.OPENED, open_price=100.0))
        acct.submit_order(_order(acct, side=OrderDirection.SELL, qty=10.0,
                                 transaction_id=txn_id))
    assert acct.position_calls == 0


# ----- 10. margin off returns BEFORE any store or broker read ---------------

def test_margin_off_reads_nothing_at_all(monkeypatch):
    """The design doc claims nothing but the submit lock is reachable with margin off.
    That was ALMOST true: the gate read the order's Transaction row before it ever
    checked ``margin_enabled``. The margin test is now the first statement in the body,
    so a backtest pays for nothing here -- no snapshot, no position book, no store read."""
    acct = _Acct(id_val=952, balance=10_000.0,
                 snapshot=_snap(long_mv=18_000.0, buying_power=2_000.0), settings=OFF)
    reads = []
    from ba2_common.core import trade_store

    monkeypatch.setattr(trade_store, "orders_where",
                        lambda *a, **k: reads.append("orders_where") or [])
    monkeypatch.setattr(trade_store, "get_or_none",
                        lambda *a, **k: reads.append("get_or_none") or None)
    with ts.inmem_trades():
        txn_id = add_instance(Transaction(symbol="AAPL", quantity=100.0,
                                          side=OrderDirection.BUY,
                                          status=TransactionStatus.OPENED, open_price=100.0))
        reads.clear()
        assert acct._validate_account_exposure(
            _order(acct, qty=10.0, transaction_id=txn_id)) == []
    assert acct.snapshot_calls == 0
    assert acct.position_calls == 0
    assert reads == []


# ----- 11. the broker reconciles the local row (2026-09-10 review, finding 2) ------
#
# The local orders table is refreshed on a schedule. A market order fills at the broker
# in milliseconds, so between the fill and the next refresh_orders() the row still says
# PENDING_NEW with a NULL filled_qty while the shares are ALREADY in the snapshot's
# long_market_value. The ceiling reads both, so those shares were charged twice and the
# headroom collapsed: production 2026-09-10 read $29.02 of room while $942.69 was real,
# and refused two funded entries (SAIL $466.02, AVAV $437.86) that never reached the
# broker. The pending sum now asks the BROKER what is still working.


def _broker_view(acct, *, status, quantity, filled_qty=None, broker_order_id="brk-1"):
    """What the BROKER says about one order -- the adapters return exactly this shape
    (a TradingOrder built by alpaca_order_to_tradingorder / tastytrade_order_to_tradingorder)."""
    return TradingOrder(account_id=acct.id, symbol="MSFT", quantity=quantity,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET,
                        status=status, filled_qty=filled_qty,
                        broker_order_id=broker_order_id)


def test_a_locally_pending_entry_the_broker_has_filled_is_not_pending():
    """The double count itself: the fill is in long_market_value, so counting the local
    row's untouched quantity charges the same shares to the ceiling twice."""
    acct = _Acct(id_val=953, balance=10_000.0, snapshot=_snap(long_mv=5_000.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PENDING_NEW, filled_qty=None)
        acct.broker_orders["brk-1"] = _broker_view(
            acct, status=OrderStatus.FILLED, quantity=50.0, filled_qty=50.0)
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_an_entry_cancelled_at_the_broker_is_not_pending_either():
    acct = _Acct(id_val=954, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PENDING_NEW, filled_qty=None)
        acct.broker_orders["brk-1"] = _broker_view(
            acct, status=OrderStatus.CANCELED, quantity=50.0, filled_qty=0.0)
        assert acct.get_stock_exposure_headroom() == pytest.approx(18_000.0)


def test_a_partial_fill_at_the_broker_counts_only_the_brokers_remainder():
    """The local row has learned nothing (filled_qty NULL); the broker has 20 of 50 done."""
    acct = _Acct(id_val=955, balance=10_000.0, snapshot=_snap(long_mv=2_000.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PENDING_NEW, filled_qty=None)
        acct.broker_orders["brk-1"] = _broker_view(
            acct, status=OrderStatus.PARTIALLY_FILLED, quantity=50.0, filled_qty=20.0)
        # 18,000 ceiling - 2,000 marked - 30 still working x 100
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)


def test_an_unreadable_broker_order_counts_the_local_remainder_and_warns(account_records):
    """``None`` is not "nothing is working". The local remainder OVER-states exposure,
    which refuses an entry instead of over-exposing the account -- and it says so."""
    acct = _Acct(id_val=956, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PENDING_NEW, filled_qty=None)
        acct.broker_orders["brk-1"] = None              # the single-order fetch failed
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)
    hits = [msg for lvl, msg in account_records
            if lvl == logging.WARNING and "could not read the broker's view" in msg]
    assert len(hits) == 1, account_records
    assert "brk-1" in hits[0] and "MSFT" in hits[0]


def test_an_unparseable_broker_status_is_unreadable_not_done(account_records):
    """UNKNOWN is what the adapters produce from a status they cannot map. Reading it as
    "done" would DROP real exposure from the total -- the direction that fails open."""
    acct = _Acct(id_val=957, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        _with_entry_order(acct, status=OrderStatus.PENDING_NEW, filled_qty=None)
        acct.broker_orders["brk-1"] = _broker_view(
            acct, status=OrderStatus.UNKNOWN, quantity=50.0, filled_qty=None)
        assert acct.get_stock_exposure_headroom() == pytest.approx(13_000.0)
    assert any(lvl == logging.WARNING and "could not read the broker's view" in msg
               for lvl, msg in account_records)


def test_exactly_one_broker_read_per_countable_working_order():
    """Live there are a handful of working entries at a time, and this runs per sizing
    decision: one round trip each, and none at all for an order that is already excluded."""
    acct = _Acct(id_val=958, balance=10_000.0, snapshot=_snap(long_mv=0.0), settings=ON)
    with ts.inmem_trades():
        parent = _with_entry_order(acct)                      # counted
        _with_entry_order(acct, status=OrderStatus.FILLED,    # excluded: locally done
                          filled_qty=50.0, broker_order_id="brk-done")
        _with_entry_order(acct, side=OrderDirection.SELL,     # excluded: protective leg
                          depends_on_order=parent,
                          depends_order_status_trigger=OrderStatus.FILLED,
                          broker_order_id="brk-leg")
        acct.broker_order_reads.clear()
        acct.get_stock_exposure_headroom()
    assert acct.broker_order_reads == ["brk-1"]


def test_margin_off_never_asks_the_broker_about_an_order():
    """BACKTEST PIN. The breakdown returns None before any of this, so a backtest pays
    for no snapshot, no store read and no broker round trip -- by construction."""
    acct = _Acct(id_val=959, balance=10_000.0, snapshot=_snap(long_mv=18_000.0), settings=OFF)
    with ts.inmem_trades():
        _with_entry_order(acct)
        assert acct.get_stock_exposure_headroom() is None
    assert acct.broker_order_reads == []
    assert acct.snapshot_calls == 0


def test_the_2026_09_10_double_count_that_refused_two_funded_entries():
    """The production shape, with the review's own numbers.

    Equity $2,004.14 x factor 1.8 = a $3,607.45 ceiling (Alpaca reports multiplier 4, so
    the platform's factor is the binding one). $1,703.40 was already marked, then NAVN,
    TTAN and CHWY filled for $955.02 -- into long_market_value -- while their local rows
    still read PENDING_NEW with a NULL filled_qty. Counting those rows as pending charged
    the $955.02 twice and left NEGATIVE headroom, so SAIL ($466.02) and AVAV ($437.86)
    were refused. The real figure, logged two minutes later once the orders refreshed,
    was $942.69 with no pending entries.
    """
    fills = [("NAVN", 22.0, 20.2050, "brk-navn"),
             ("TTAN", 8.0, 56.1594, "brk-ttan"),
             ("CHWY", 3.0, 20.4115, "brk-chwy")]
    filled_notional = sum(qty * price for _, qty, price, _ in fills)      # 955.02
    acct = _Acct(id_val=960, balance=2_004.14, settings=ON,
                 snapshot=_snap(multiplier=4.0, buying_power=3_944.31,
                                long_mv=1_703.40 + filled_notional))
    with ts.inmem_trades():
        for symbol, qty, price, broker_id in fills:
            _with_entry_order(acct, symbol=symbol, qty=qty, limit_price=price,
                              status=OrderStatus.PENDING_NEW, filled_qty=None,
                              broker_order_id=broker_id)
            acct.broker_orders[broker_id] = _broker_view(
                acct, status=OrderStatus.FILLED, quantity=qty, filled_qty=qty,
                broker_order_id=broker_id)
        breakdown = acct._stock_exposure_breakdown()

    assert breakdown.ceiling == pytest.approx(3_607.45, abs=0.01)
    assert breakdown.gross == pytest.approx(2_658.42, abs=0.01)
    assert breakdown.pending == 0.0, "the fills are in the gross; they are not also pending"
    assert breakdown.headroom == pytest.approx(949.03, abs=0.01)
    # Both refused entries fit in the real headroom; the double count left -$5.99.
    assert breakdown.headroom > 466.02 + 437.86
