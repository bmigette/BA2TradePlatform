"""Live-side Portfolio Allocation service tests.

Uses tests/conftest.py's in-memory SQLite (autouse `patch_db_engine`) and a
duck-typed FakeAccount -- no broker, no NiceGUI.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from ba2_trade_platform.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
    MARKET_HOURS_SOURCE_BROKER, MARKET_HOURS_SOURCE_UNAVAILABLE,
    CashTransfer, MarginInfo, MarketHours, OrderImpact,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.interfaces.AccountInterface import (
    AccountInterface as _RealAccountInterface,
)
from ba2_trade_platform.core.models import (
    PortfolioAllocationRun, PortfolioIncomeEvent, TradingOrder, Transaction,
)
from ba2_trade_platform.core.portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP,
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE,
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    VALUATION_MODE_COST, VALUATION_MODE_MARKET,
    AllocationPlan, AllocationRow, BaseSnapshot, PositionFetchFailed, PositionState,
    compute_base_notional,
)
from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.types import (
    ActivityLogSeverity, ActivityLogType, BrokerOrderErrorReason,
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.core import portfolio_allocation_service as svc


#: Marks "the caller never passed is_closing_order at all". A preview must state
#: its intent EXPLICITLY -- relying on the seam's False default is how a close got
#: priced as a short open (commit 1d099e8).
NOT_PASSED = "<not passed>"

#: Marks "the broker advanced the status but reported NO fill quantity", which
#: lands in the DB as a NULL ``filled_qty``. ``None`` cannot spell this in
#: ``FakeAccount.fills`` -- there it already means "this order's OWN quantity" --
#: and it is not the same thing as a 0.0 either: 0.0 is a measurement of zero
#: shares, this is the absence of a measurement. Both real adapters produce it:
#: AlpacaAccount.py:2928 only writes a quantity the broker actually sent, and
#: TastyTradeAccount.py:1473 compares ``float(db or 0.0) != float(broker or 0.0)``
#: so its ``_fills_summary`` answer of ``(0.0, None)`` never overwrites the NULL.
NO_FILL_QTY_REPORTED = "<no filled_qty reported>"


class FakePosition:
    """Minimal stand-in for a broker Position row.

    ``side`` defaults to None -- "this row did not say" -- because that is what a
    hand-built row means and what the sign normalisation must TRUST. The two live
    brokers do stamp it, and they disagree about the signs underneath it: Alpaca
    passes its own negative numbers through while TastyTrade stores ``qty=abs_qty``
    and puts the direction here.
    """

    def __init__(self, symbol, qty, cost_basis, market_value, side=None):
        self.symbol = symbol
        self.qty = qty
        self.cost_basis = cost_basis
        self.market_value = market_value
        self.side = side


class FakeAccount:
    """Duck-typed stand-in for AccountInterface. No DB lookups, no broker."""

    supports_trading = True

    # OPT-L1's exit guard, taken from the REAL AccountInterface rather than stubbed
    # out. ``TransactionHelper.adjust_quantity_with_tpsl`` now asks every account
    # whether the shares it is about to trim are pledged as cover for an open short
    # call, and a double that answered "nothing is pledged" on its own authority
    # would be asserting the fact under test. Bound here unchanged, the real
    # capability check answers for itself: ``_can_hold_options`` is an isinstance
    # against ``OptionsAccountInterface``, this duck-typed double is not one, and an
    # account that cannot hold options has nothing pledged BY CONSTRUCTION. The
    # cover accessors are therefore never reached, which is why the double needs
    # none of them — and if the guard ever stopped checking the capability first,
    # every trim test here would AttributeError rather than quietly pass.
    _can_hold_options = _RealAccountInterface._can_hold_options
    cover_refusal_for_equity_sale = _RealAccountInterface.cover_refusal_for_equity_sale

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.positions = []          # list[FakePosition]; None means FETCH FAILED
        self.prices = {}             # symbol -> float
        self.margin = {}             # symbol -> MarginInfo
        self.impacts = {}            # symbol -> OrderImpact
        self.cash_transfers = []     # list[CashTransfer]; None = the broker gave nothing back
        #: [(start_date, end_date)] the income sync asked the broker for.
        self.cash_transfer_calls = []
        self.submitted = []          # [(symbol, side, quantity, comment)]
        self.previewed = []          # [(symbol, quantity, is_closing_order)]
        self.preview_raises = False
        self.margin_raises = False
        self.closed = []             # [transaction_id]
        #: Quantities the broker refuses UNAMBIGUOUSLY -- it answered, and the
        #: answer was "no". Stamped the way _handle_order_submit_error does.
        self.reject_quantities = set()
        #: quantity -> message for a failure that PROVES NOTHING. The adapter
        #: classified it UNKNOWN (a lost response, a socket timeout) and returned
        #: None, which is indistinguishable from a clean rejection.
        self.ambiguous_quantities = {}
        self.washtrade_symbols = set()
        #: ONE timeline across every branch. `submitted` and `closed` are per-path
        #: lists and cannot show that a close happened BEFORE a buy; the
        #: sells-before-buys invariant is about exactly that interleaving.
        self.events = []             # [('close', txn_id) | ('submit', symbol)]
        #: symbol -> the `is_closing_order` value the caller actually passed.
        self.submit_closing_flags = []
        self.raise_quantities = {}   # quantity -> Exception raised by submit_order
        #: quantity -> reason the adapter writes onto the PERSISTED row only, the
        #: way AccountInterface._handle_order_submit_error does (it updates
        #: `fresh_order`, never the caller's detached object), then returns None.
        self.db_reject_quantities = {}
        self.terminal_statuses = {}  # symbol -> OrderStatus the broker comes back with
        self.partial_fills = {}      # symbol -> filled quantity (< submitted quantity)
        self.close_failures = {}     # transaction_id -> failure message
        self.close_raises = {}       # transaction_id -> Exception
        #: Symbols the broker ACCEPTS but has not filled -- the normal shape of a
        #: market order placed before the open, and the only outcome that is a
        #: success with no fill to show for it.
        self.accepted_symbols = set()
        #: When True EVERY submission is refused, whatever its quantity. Used by
        #: the tests that drive the REAL TransactionHelper, which builds its own
        #: orders and so has no quantity for the caller to pre-register.
        self.reject_everything = False
        #: transaction_id -> False returned by cancel_order (the broker refused
        #: to pull the TP/SL leg, so nothing will ever trigger off its cancel).
        self.cancel_failures = set()
        self.canceled = []           # [order_id] the caller asked us to cancel
        # Market hours: OPEN by default at a FROZEN instant, so every pre-existing
        # submission test keeps meaning what it meant and none of them depends on
        # the wall clock. Reassign to simulate a closed or unavailable market.
        self.market_hours = MarketHours(
            is_open=True, source=MARKET_HOURS_SOURCE_BROKER, as_of=FROZEN_NOW,
            open_at=None, close_at=MARKET_CLOSE, next_open=None, next_close=MARKET_CLOSE)
        self.market_hours_error = None
        #: What the BROKER reports on the next ``refresh_orders()``, by symbol:
        #:     symbol -> (OrderStatus, filled_qty, open_price)
        #: A ``filled_qty`` of None means "that order's OWN quantity", which is how
        #: a close of several transactions fills each leg at its own size. A symbol
        #: with no entry is left exactly as ``submit_order`` left the DB row -- which
        #: is how "still working at the broker" is expressed, and it is the ordinary
        #: outcome, not an edge case.
        self.fills = {}
        self.refresh_calls = []
        self.refresh_raises = None   # set to an Exception to simulate an outage

    def get_market_hours(self, *, now=None):
        """Mirrors ReadOnlyAccountInterface.get_market_hours' signature exactly."""
        if self.market_hours_error is not None:
            raise self.market_hours_error
        return self.market_hours

    def get_positions(self):
        return self.positions

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        if isinstance(symbol_or_symbols, str):
            return self.prices.get(symbol_or_symbols)
        return {s: self.prices.get(s) for s in symbol_or_symbols}

    def get_symbol_margin_info(self, symbols):
        if self.margin_raises:
            raise RuntimeError("broker margin lookup exploded")
        return {s: self.margin[s] for s in symbols if s in self.margin}

    def preview_order_impact(self, trading_order, is_closing_order=NOT_PASSED):
        self.previewed.append((trading_order.symbol, trading_order.quantity,
                               is_closing_order))
        if self.preview_raises:
            raise RuntimeError("broker precheck exploded")
        return self.impacts.get(trading_order.symbol)

    def get_cash_transfers(self, start_date=None, end_date=None):
        self.cash_transfer_calls.append((start_date, end_date))
        if self.cash_transfers is None:
            return None
        return list(self.cash_transfers)

    def _handle_submit_error(self, trading_order, reason: str):
        """What ``AccountInterface._handle_order_submit_error`` really does.

        It re-reads the row from the DATABASE, stamps it ERROR, appends
        ``[<classified reason>] <broker message>`` to its comment and returns
        None. The caller's own object never sees any of it.
        """
        fresh = get_instance(TradingOrder, trading_order.id)
        fresh.status = OrderStatus.ERROR
        fresh.comment = f"{fresh.comment} | {reason}" if fresh.comment else reason
        update_instance(fresh)
        return None

    def submit_order(self, trading_order, tp_price=None, sl_price=None,
                     is_closing_order=NOT_PASSED):
        # AccountInterface.submit_order persists an unsaved order BEFORE it goes
        # anywhere near the broker (AccountInterface.py:335, and again in every
        # _submit_order_impl's `if trading_order.id is None`), and every failure
        # path below then writes onto that PERSISTED row. TransactionHelper hands
        # this seam brand-new TradingOrder objects, so without this the helper's
        # own orders would never exist in the database at all.
        if trading_order.id is None:
            trading_order.status = OrderStatus.PENDING
            trading_order.id = add_instance(trading_order, expunge_after_flush=True)
        self.submitted.append((trading_order.symbol, trading_order.side,
                               trading_order.quantity, trading_order.comment))
        self.submit_closing_flags.append(is_closing_order)
        self.events.append(('submit', trading_order.symbol))
        if trading_order.quantity in self.raise_quantities:
            raise self.raise_quantities[trading_order.quantity]
        if trading_order.quantity in self.db_reject_quantities:
            # The reason lands on the DATABASE row, not on this object -- and
            # WITHOUT a classified reason, the way an adapter that never called
            # _handle_order_submit_error leaves it.
            fresh = get_instance(TradingOrder, trading_order.id)
            reason = self.db_reject_quantities[trading_order.quantity]
            fresh.comment = f"{fresh.comment} | {reason}" if fresh.comment else reason
            update_instance(fresh)
            return None
        if trading_order.quantity in self.ambiguous_quantities:
            return self._handle_submit_error(
                trading_order,
                f"[{BrokerOrderErrorReason.UNKNOWN.value}] "
                f"{self.ambiguous_quantities[trading_order.quantity]}")
        if self.reject_everything or trading_order.quantity in self.reject_quantities:
            return self._handle_submit_error(
                trading_order,
                f"[{BrokerOrderErrorReason.INSUFFICIENT_FUNDS.value}] broker rejected")
        if trading_order.symbol in self.washtrade_symbols:
            # PERSISTED, matching AccountInterface.submit_order's own gate
            # (AccountInterface.py:396-397: sets the status, then
            # update_instance immediately) -- callers that re-read the order
            # from the DB (portfolio_allocation_service._order_is_washtrade_locked)
            # must see the lock, not the PENDING this method wrote a few lines up.
            trading_order.status = OrderStatus.WASHTRADE_LOCKED
            update_instance(trading_order)
            return trading_order
        if trading_order.symbol in self.accepted_symbols:
            trading_order.status = OrderStatus.ACCEPTED
            return trading_order
        if trading_order.symbol in self.partial_fills:
            # A terminal status ON TOP of a partial fill is the "cancelled after
            # filling some of it" case, which is exactly when a retry must not run.
            trading_order.filled_qty = self.partial_fills[trading_order.symbol]
            trading_order.status = self.terminal_statuses.get(
                trading_order.symbol, OrderStatus.PARTIALLY_FILLED)
            return trading_order
        if trading_order.symbol in self.terminal_statuses:
            trading_order.status = self.terminal_statuses[trading_order.symbol]
            return trading_order
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        return trading_order

    def cancel_order(self, order_id):
        """TransactionHelper pulls the live TP/SL legs through this seam.

        Returns False for the ids in ``cancel_failures``: a leg the broker would
        not pull is a leg that never reaches CANCELED, so nothing triggers off it.
        """
        self.canceled.append(order_id)
        if order_id in self.cancel_failures:
            return False
        fresh = get_instance(TradingOrder, order_id)
        fresh.status = OrderStatus.CANCELED
        update_instance(fresh)
        return True

    def close_transaction(self, transaction_id):
        """Persist a real MARKET close order, as the live path does, and return its id.

        A hardcoded ``close_order_id`` would point at no row at all, and the run's
        fill measurement reads its orders back out of the DB: the SELL side of a
        close would be invisible, so ``net_buy_value`` would come out too high and
        the run would over-consume the income ledger.
        """
        self.events.append(('close', transaction_id))
        if transaction_id in self.close_raises:
            raise self.close_raises[transaction_id]
        if transaction_id in self.close_failures:
            return {'success': False, 'message': self.close_failures[transaction_id]}
        self.closed.append(transaction_id)
        txn = get_instance(Transaction, transaction_id)
        close_order_id = add_instance(TradingOrder(
            account_id=self.id, symbol=txn.symbol, quantity=abs(txn.quantity or 0.0),
            side=OrderDirection.SELL, order_type=OrderType.MARKET, good_for='day',
            status=OrderStatus.NEW, open_type=OrderOpenType.MANUAL,
            transaction_id=transaction_id, comment="closing order",
        ))
        return {'success': True, 'message': 'closed', 'canceled_count': 0,
                'deleted_count': 0, 'close_order_id': close_order_id}

    def _write_order(self, order_id, **fields):
        """Write broker truth onto our DB row, the way a real refresh does."""
        row = get_instance(TradingOrder, order_id)
        if row is None:
            return
        for name, value in fields.items():
            setattr(row, name, value)
        update_instance(row)

    def refresh_orders(self, heuristic_mapping=False, fetch_all=False):
        """Apply ``self.fills`` to this account's DB rows, keyed by symbol.

        Mirrors what ``AlpacaAccount.refresh_orders`` does: it writes the broker's
        status, ``filled_qty`` and ``filled_avg_price`` onto the persisted row.
        """
        self.refresh_calls.append({'heuristic_mapping': heuristic_mapping,
                                   'fetch_all': fetch_all})
        if self.refresh_raises is not None:
            raise self.refresh_raises
        with get_db() as session:
            rows = session.exec(select(TradingOrder).where(
                TradingOrder.account_id == self.id)).all()
            by_id = {row.id: (row.symbol, row.quantity) for row in rows}
        for order_id, (symbol, quantity) in by_id.items():
            reported = self.fills.get(symbol)
            if reported is None:
                continue
            status, filled_qty, open_price = reported
            if filled_qty is NO_FILL_QTY_REPORTED:
                # The broker said nothing about the quantity, so the row keeps its
                # NULL. Written explicitly rather than skipped so the test reads as
                # "the refresh ran and still left it NULL".
                resolved = None
            elif filled_qty is None:
                resolved = float(quantity or 0.0)
            else:
                resolved = filled_qty
            self._write_order(order_id, status=status, filled_qty=resolved,
                              open_price=open_price)
        return True


class FakeReadOnlyAccount:
    """A broker seam with NO preview_order_impact at all (ReadOnlyAccountInterface).

    ``preview_order_impact`` lives on AccountInterface only, so a bare call here
    raises AttributeError instead of yielding "this broker cannot preview".
    """

    supports_trading = False

    def __init__(self, account_id: int = 2):
        self.id = account_id


def make_open_transaction(account_id: int, symbol: str, quantity: float) -> int:
    """Persist an OPENED Transaction linked to `account_id` via a filled order."""
    txn_id = add_instance(Transaction(
        symbol=symbol, quantity=quantity, side=OrderDirection.BUY,
        open_price=100.0, status=TransactionStatus.OPENED,
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=symbol, quantity=quantity,
        side=OrderDirection.BUY, order_type=OrderType.MARKET, good_for='day',
        status=OrderStatus.FILLED, open_type=OrderOpenType.MANUAL,
        transaction_id=txn_id,
    ))
    return txn_id


def make_active_tp_order(account_id: int, txn_id: int, symbol: str,
                         quantity: float) -> int:
    """An ACCEPTED take-profit leg hanging off the transaction's entry order.

    ``TransactionHelper`` recognises a TP/SL leg by ``depends_on_order`` being
    set, and only a NON-terminal one counts, so this is the shape that makes
    ``adjust_quantity_with_tpsl`` take its triggered-chain path instead of the
    bare one.
    """
    with get_db() as session:
        entry = session.exec(select(TradingOrder).where(
            TradingOrder.transaction_id == txn_id,
            TradingOrder.depends_on_order.is_(None),
        )).first()
        entry_id = entry.id
    return add_instance(TradingOrder(
        account_id=account_id, symbol=symbol, quantity=quantity,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        limit_price=999.0, status=OrderStatus.ACCEPTED,
        depends_on_order=entry_id, transaction_id=txn_id,
        comment="TP leg",
    ))


def txn_quantity(txn_id: int) -> float:
    return float(get_instance(Transaction, txn_id).quantity or 0.0)


def make_row(symbol, side, delta, value, bp_cost, price=100.0):
    return AllocationRow(
        symbol=symbol, price=price, delta_quantity=delta, side=side,
        estimated_value=value, bp_cost=bp_cost, bp_factor=1.0,
    )


# ---------------------------------------------------------------------------
# build_position_states
# ---------------------------------------------------------------------------

def test_build_position_states_raises_when_get_positions_returns_none():
    account = FakeAccount()
    account.positions = None  # broker fetch FAILED -- not a flat account
    with pytest.raises(PositionFetchFailed):
        svc.build_position_states(account, ["AAPL"])


def test_build_position_states_maps_quantity_cost_basis_and_price():
    account = FakeAccount()
    account.positions = [FakePosition("AAPL", 10.0, 1500.0, 1600.0)]
    account.prices = {"AAPL": 160.0, "NVDA": 900.0}

    states = svc.build_position_states(account, ["aapl", "NVDA"])

    assert set(states) == {"AAPL", "NVDA"}
    assert states["AAPL"].quantity == pytest.approx(10.0)
    assert states["AAPL"].cost_basis == pytest.approx(1500.0)
    assert states["AAPL"].price == pytest.approx(160.0)
    # Managed but not held -> flat, still priced, still plannable.
    assert states["NVDA"].quantity == pytest.approx(0.0)
    assert states["NVDA"].cost_basis == pytest.approx(0.0)
    assert states["NVDA"].price == pytest.approx(900.0)


def test_build_position_states_lists_open_transaction_ids_oldest_first():
    account = FakeAccount(account_id=7)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    account.prices = {"AAPL": 106.0}
    first = make_open_transaction(7, "AAPL", 20.0)
    second = make_open_transaction(7, "AAPL", 10.0)

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].transaction_ids == [first, second]


def test_build_position_states_ignores_another_accounts_transactions():
    account = FakeAccount(account_id=7)
    account.positions = [FakePosition("AAPL", 10.0, 1000.0, 1060.0)]
    account.prices = {"AAPL": 106.0}
    mine = make_open_transaction(7, "AAPL", 10.0)
    make_open_transaction(8, "AAPL", 99.0)  # a different broker account

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].transaction_ids == [mine]


def test_build_position_states_leaves_an_unpriced_symbol_at_none():
    account = FakeAccount()
    account.positions = [FakePosition("AAPL", 10.0, 1500.0, 1600.0)]
    account.prices = {}  # broker returned no quote at all

    states = svc.build_position_states(account, ["AAPL"])

    # No fabricated price: the engine skips the row with REASON_NO_PRICE.
    assert states["AAPL"].price is None


def test_build_position_states_on_a_genuinely_flat_account_is_not_a_failure():
    account = FakeAccount()
    account.positions = []  # FLAT, and the fetch SUCCEEDED
    account.prices = {"AAPL": 160.0}

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].quantity == pytest.approx(0.0)
    assert states["AAPL"].price == pytest.approx(160.0)


# ---------------------------------------------------------------------------
# build_position_states: shorts carry ONE signed representation, whichever
# broker reported them -- the same invariant ``positions_by_symbol`` already
# holds. This path feeds ``compute_base_notional`` and therefore every label
# target on the account, so a short read as a long inflates the whole base.
# ---------------------------------------------------------------------------

def test_build_position_states_signs_a_tastytrade_short_negative():
    """TastyTrade stamps ``qty=abs_qty``, a POSITIVE cost basis and a POSITIVE
    market value, and records the direction in ``side``
    (``TastyTradeAccount.py:520-547``). Read raw, its short reads as a long."""
    account = FakeAccount()
    account.positions = [FakePosition("TSLA", 10.0, 1500.0, 1800.0,
                                      side=OrderDirection.SELL)]
    account.prices = {"TSLA": 180.0}

    states = svc.build_position_states(account, ["TSLA"])

    assert states["TSLA"].quantity == pytest.approx(-10.0)
    assert states["TSLA"].cost_basis == pytest.approx(-1500.0)
    assert states["TSLA"].market_value == pytest.approx(-1800.0)


def test_build_position_states_does_not_flip_an_already_signed_alpaca_short():
    """Alpaca passes the broker's own NEGATIVE signs straight through
    (``alpaca_position_to_position``) and still stamps ``side=SELL``, so forcing
    the sign has to be idempotent: ``-abs(...)``, never a bare negation."""
    account = FakeAccount()
    account.positions = [FakePosition("TSLA", -10.0, -1500.0, -1800.0,
                                      side=OrderDirection.SELL)]
    account.prices = {"TSLA": 180.0}

    states = svc.build_position_states(account, ["TSLA"])

    assert states["TSLA"].quantity == pytest.approx(-10.0)
    assert states["TSLA"].cost_basis == pytest.approx(-1500.0)
    assert states["TSLA"].market_value == pytest.approx(-1800.0)


def test_build_position_states_leaves_a_long_exactly_as_the_broker_reported_it():
    """Only a SHORT forces a sign. No broker's numbers are ever "corrected" on
    the strength of a metadata field."""
    account = FakeAccount()
    account.positions = [FakePosition("AAPL", 10.0, 1500.0, 1800.0,
                                      side=OrderDirection.BUY)]
    account.prices = {"AAPL": 180.0}

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].quantity == pytest.approx(10.0)
    assert states["AAPL"].cost_basis == pytest.approx(1500.0)
    assert states["AAPL"].market_value == pytest.approx(1800.0)


def test_build_position_states_will_not_re_sign_a_long_from_its_side_field():
    """The sign rule is one-way: a SHORT forces its numbers negative, a LONG
    forces nothing at all.

    Pinned with a CONTRADICTORY row -- ``side`` says long, the numbers say short --
    because that is the only shape that can tell the two rules apart: on an
    ordinary long, "leave it alone" and "force it positive" agree. Trusting the
    metadata field over the money would let one mislabelled row silently flip a
    real short into a long in the allocation base, which is the very failure this
    normalisation exists to prevent. Mutation-checked: making the side
    authoritative for both directions passes every other test in this file.
    """
    account = FakeAccount()
    account.positions = [FakePosition("AAPL", -10.0, -1500.0, -1800.0,
                                      side=OrderDirection.BUY)]
    account.prices = {"AAPL": 180.0}

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].quantity == pytest.approx(-10.0)
    assert states["AAPL"].cost_basis == pytest.approx(-1500.0)
    assert states["AAPL"].market_value == pytest.approx(-1800.0)


def test_build_position_states_without_a_side_trusts_the_signs_it_was_given():
    """An unknown/absent side means "this row did not say"; inventing a direction
    from it would rewrite a broker's own numbers on no evidence."""
    account = FakeAccount()
    account.positions = [FakePosition("TSLA", -10.0, -1500.0, -1800.0)]
    account.prices = {"TSLA": 180.0}

    states = svc.build_position_states(account, ["TSLA"])

    assert states["TSLA"].quantity == pytest.approx(-10.0)
    assert states["TSLA"].cost_basis == pytest.approx(-1500.0)
    assert states["TSLA"].market_value == pytest.approx(-1800.0)


def test_a_tastytrade_short_REDUCES_the_base_notional_instead_of_inflating_it():
    """THE money consequence. ``build_position_states`` feeds
    ``compute_base_notional``, which feeds ``BaseSnapshot.base_notional`` and so
    every label target on the account. Read raw, a 2,000 short ADDS 2,000 to the
    base instead of removing it -- a 4,000 error on a 13,000 book, and every
    target is then a share of the wrong number.
    """
    account = FakeAccount()
    account.positions = [
        FakePosition("AAPL", 10.0, 5000.0, 5000.0, side=OrderDirection.BUY),
        # TastyTrade shape: magnitudes only, direction in `side`.
        FakePosition("TSLA", 10.0, 2000.0, 2000.0, side=OrderDirection.SELL),
    ]
    account.prices = {"AAPL": 500.0, "TSLA": 200.0}

    states = svc.build_position_states(account, ["AAPL", "TSLA"])

    for mode in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        base = compute_base_notional(10_000.0, states, ["AAPL", "TSLA"],
                                     valuation_mode=mode)
        # 10,000 buying power + 5,000 long - 2,000 short. NOT 17,000.
        assert base == pytest.approx(13_000.0), mode


def test_the_base_notional_is_the_same_short_whichever_broker_reported_it():
    """The two brokers' shapes must not produce two different bases."""
    tastytrade = FakeAccount()
    tastytrade.positions = [FakePosition("TSLA", 10.0, 2000.0, 2000.0,
                                         side=OrderDirection.SELL)]
    tastytrade.prices = {"TSLA": 200.0}
    alpaca = FakeAccount()
    alpaca.positions = [FakePosition("TSLA", -10.0, -2000.0, -2000.0,
                                     side=OrderDirection.SELL)]
    alpaca.prices = {"TSLA": 200.0}

    bases = [compute_base_notional(10_000.0,
                                   svc.build_position_states(account, ["TSLA"]),
                                   ["TSLA"], valuation_mode=VALUATION_MODE_COST)
             for account in (tastytrade, alpaca)]

    assert bases == [pytest.approx(8_000.0), pytest.approx(8_000.0)]


@pytest.mark.parametrize("qty, cost, value, side", [
    # TastyTrade short: magnitudes, direction in `side`.
    (10.0, 1500.0, 1800.0, OrderDirection.SELL),
    # Alpaca short: already signed, and still stamped SELL.
    (-10.0, -1500.0, -1800.0, OrderDirection.SELL),
    # A long, from either broker.
    (10.0, 1500.0, 1800.0, OrderDirection.BUY),
    # No side at all: both paths must trust the signs they were given.
    (-10.0, -1500.0, -1800.0, None),
])
def test_the_two_normalisation_paths_agree_on_the_same_broker_position(
        qty, cost, value, side):
    """THE invariant whose absence caused this bug, and the one that will catch
    the next divergence.

    ``ui/utils/portfolio_allocation_view.positions_by_symbol`` (the page) and
    ``portfolio_allocation_service.build_position_states`` (the wizard, the base
    notional, the solvers) normalise the SAME broker row. They disagreed for two
    releases because only one of them read ``side``.
    """
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    row = FakePosition("TSLA", qty, cost, value, side=side)
    account = FakeAccount()
    account.positions = [row]
    account.prices = {"TSLA": 180.0}

    page = positions_by_symbol([row])["TSLA"]
    service = svc.build_position_states(account, ["TSLA"])["TSLA"]

    assert service.quantity == pytest.approx(page.quantity)
    assert service.cost_basis == pytest.approx(page.cost_basis)
    assert service.market_value == pytest.approx(page.market_value)


# ---------------------------------------------------------------------------
# fetch_margin_info / precheck_plan
# ---------------------------------------------------------------------------

def test_fetch_margin_info_omits_symbols_the_broker_cannot_describe():
    account = FakeAccount()
    account.margin = {"AAPL": MarginInfo(symbol="AAPL", bp_factor=1.0, marginable=True)}
    info = svc.fetch_margin_info(account, ["AAPL", "NVDA"])
    assert set(info) == {"AAPL"}


def test_fetch_margin_info_returns_empty_when_the_broker_raises():
    account = FakeAccount()
    account.margin_raises = True
    # No margin data is survivable (the engine falls back to default_bp_factor);
    # propagating the error would kill the whole dry run.
    assert svc.fetch_margin_info(account, ["AAPL"]) == {}


def test_precheck_plan_without_broker_support_returns_the_same_plan():
    account = FakeAccount()  # every preview_order_impact() returns None
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )
    assert svc.precheck_plan(account, plan, available_buying_power=10_000.0,
                             margin={}) is plan


def test_precheck_plan_replaces_the_estimate_with_the_broker_buying_power_cost():
    account = FakeAccount()
    # Broker says this buy really costs 3200 of BP, not the estimated 1600.
    account.impacts = {"AAPL": OrderImpact(symbol="AAPL", change_in_buying_power=-3200.0)}
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )

    result = svc.precheck_plan(account, plan, available_buying_power=10_000.0,
                               margin={})

    assert result is not plan
    assert result.rows[0].bp_cost == pytest.approx(3200.0)
    assert result.required_buying_power == pytest.approx(3200.0)


def test_precheck_plan_re_solves_on_an_impact_that_frees_buying_power():
    account = FakeAccount()
    # bp_cost is 0.0 -- a REAL zero (this order frees BP), not "no impact". Guards
    # against gating the re-solve on `impact.bp_cost` instead of `impact is None`,
    # which would drop exactly the rows that give headroom back.
    account.impacts = {"AAPL": OrderImpact(symbol="AAPL", change_in_buying_power=+500.0)}
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )

    result = svc.precheck_plan(account, plan, available_buying_power=10_000.0,
                               margin={})

    assert result is not plan
    assert result.rows[0].bp_cost == pytest.approx(0.0)


def test_precheck_plan_previews_a_buy_as_an_opening_order():
    account = FakeAccount()
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0,
    )

    svc.precheck_plan(account, plan, available_buying_power=10_000.0, margin={})

    # Stated EXPLICITLY, not left to the seam's default: the preview has to price
    # exactly what submission would send, and an allocation buy always opens.
    assert account.previewed == [("AAPL", 10.0, False)]


def test_precheck_plan_does_not_preview_sells():
    account = FakeAccount()
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0),
              make_row("MSFT", OrderDirection.SELL, -4, 400.0, 0.0)],
        available_buying_power=10_000.0,
    )

    svc.precheck_plan(account, plan, available_buying_power=10_000.0, margin={})

    # Sells free buying power and never scale, so a sell impact cannot change the
    # plan -- while a rejected close preview WOULD zero the row via
    # apply_order_impacts and silently hold a position the user asked to exit.
    assert [symbol for symbol, _qty, _closing in account.previewed] == ["AAPL"]


def test_precheck_plan_survives_a_broker_that_raises():
    account = FakeAccount()
    account.preview_raises = True
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )

    result = svc.precheck_plan(account, plan, available_buying_power=10_000.0,
                               margin={})

    # A failed precheck is "not asked", never a zero impact.
    assert result is plan


def test_precheck_plan_on_a_read_only_account_does_not_raise():
    account = FakeReadOnlyAccount()
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0,
    )

    # preview_order_impact is an AccountInterface method; a bare call on a
    # read-only account raises AttributeError instead of meaning "cannot preview".
    assert svc.precheck_plan(account, plan, available_buying_power=10_000.0,
                             margin={}) is plan


def test_precheck_plan_re_solves_on_the_brokers_fractional_grid():
    account = FakeAccount()
    account.impacts = {"AAPL": OrderImpact(symbol="AAPL", change_in_buying_power=-1000.0)}
    margin = {"AAPL": MarginInfo(symbol="AAPL", bp_factor=1.0, fractionable=True,
                                 min_trade_increment=0.5)}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1000.0, 1000.0)
    row.fractional = True
    plan = AllocationPlan(
        rows=[row], available_buying_power=430.0, required_buying_power=1000.0,
        total_buy_value=1000.0, allow_fractional=True,
    )

    result = svc.precheck_plan(account, plan, available_buying_power=430.0,
                               margin=margin)

    # 430/1000 of 10 shares is 4.3, which is NOT on the broker's 0.5 grid. The
    # solved margin dict has to reach the re-solve or the order is rejected.
    assert result.rows[0].delta_quantity == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# submit_plan -- ordering
# ---------------------------------------------------------------------------

def test_submit_plan_submits_every_sell_before_any_buy():
    account = FakeAccount(account_id=3)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    txn_id = make_open_transaction(3, "MSFT", 5.0)
    current = {
        "MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                              transaction_ids=[txn_id]),
    }
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0),
        ],
        available_buying_power=10_000.0,
    )
    plan.rows[0].target_quantity = 10.0
    plan.rows[1].target_quantity = 0.0

    svc.submit_plan(account, plan, current, run_tag="17", allow_fractional=False)

    # MSFT is a full close (target 0) -> close_transaction, and it happened before
    # the AAPL buy reached submit_order. The BUY is listed FIRST in plan.rows, so
    # only the interleaving proves the ordering.
    assert account.closed == [txn_id]
    assert [s[0] for s in account.submitted] == ["AAPL"]
    assert account.events == [('close', txn_id), ('submit', "AAPL")]


def test_submit_plan_orders_buys_by_descending_estimated_value():
    account = FakeAccount(account_id=4)
    account.positions = []
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0),
            make_row("KO", OrderDirection.BUY, 10.0, 600.0, 600.0, price=60.0),
        ],
        available_buying_power=10_000.0,
    )
    for row in plan.rows:
        row.target_quantity = row.delta_quantity

    svc.submit_plan(account, plan, {}, run_tag="18", allow_fractional=False)

    assert [s[0] for s in account.submitted] == ["NVDA", "AAPL", "KO"]


def test_submit_plan_still_submits_the_buys_after_a_sell_fails():
    """Buys are scaled to fit the buying power the account has BEFORE the sells
    (``_apply_bp_scaling`` is handed ``available_buying_power``), so a refused
    close cannot make them overspend -- and abandoning the buys would leave the
    account further from target than doing nothing. The failure is reported, the
    ordering still holds."""
    account = FakeAccount(account_id=41)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    txn_id = make_open_transaction(41, "MSFT", 5.0)
    account.close_failures = {txn_id: "position is held for another order"}
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[txn_id])}
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0),
        ],
        available_buying_power=10_000.0,
    )
    plan.rows[0].target_quantity = 10.0
    plan.rows[1].target_quantity = 0.0

    outcomes = svc.submit_plan(account, plan, current, run_tag="42", allow_fractional=False)

    assert account.events == [('close', txn_id), ('submit', "AAPL")]
    by_symbol = {o.symbol: o for o in outcomes}
    assert by_symbol["MSFT"].status == svc.OUTCOME_FAILED
    assert "held for another order" in by_symbol["MSFT"].message
    assert by_symbol["AAPL"].status == svc.OUTCOME_SUBMITTED


def test_submit_plan_still_submits_the_buys_after_a_sell_explodes():
    """One row raising must not abort the run half-way: every remaining row would
    silently go unattempted while the outcome table showed nothing about them."""
    account = FakeAccount(account_id=43)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    txn_id = make_open_transaction(43, "MSFT", 5.0)
    account.close_raises = {txn_id: RuntimeError("broker connection reset")}
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[txn_id])}
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0),
        ],
        available_buying_power=10_000.0,
    )
    plan.rows[0].target_quantity = 10.0
    plan.rows[1].target_quantity = 0.0

    outcomes = svc.submit_plan(account, plan, current, run_tag="44", allow_fractional=False)

    assert account.events == [('close', txn_id), ('submit', "AAPL")]
    by_symbol = {o.symbol: o for o in outcomes}
    assert by_symbol["MSFT"].status == svc.OUTCOME_FAILED
    assert "connection reset" in by_symbol["MSFT"].message
    assert by_symbol["AAPL"].status == svc.OUTCOME_SUBMITTED


# ---------------------------------------------------------------------------
# submit_plan -- the NEW-position branch
# ---------------------------------------------------------------------------

def test_submit_plan_new_order_comment_never_contains_the_word_closing():
    account = FakeAccount(account_id=5)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="19", allow_fractional=False)

    comment = account.submitted[0][3]
    assert "19" in comment
    assert "closing" not in comment.lower()
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].action == ACTION_NEW
    assert outcomes[0].order_ids


def test_the_run_comment_can_never_say_closing_even_if_the_run_tag_does():
    """close_transaction re-detects an existing close order with
    ``order_type == MARKET and 'closing' in order.comment.lower()``, and every
    allocation order is a MARKET order. A comment containing it would make every
    future close on that symbol believe a close order already exists -- so the
    caller-supplied run_tag, the one field that could smuggle it in, is scrubbed
    rather than trusted."""
    assert "closing" not in svc.RUN_COMMENT_FMT.lower()
    rendered = svc._run_comment("Closing-Run", OrderDirection.BUY, "AAPL")
    assert "closing" not in rendered.lower()
    assert "AAPL" in rendered


def test_submit_plan_sends_a_new_order_through_the_public_submit_order_seam():
    """Not ``_submit_order_impl``: the public method is what runs order validation,
    creates the Transaction for a bare MARKET order and applies the wash-trade
    gate. ``is_closing_order`` is stated EXPLICITLY -- an allocation buy always
    OPENS, and a close mispriced as a short open is commit 1d099e8."""
    account = FakeAccount(account_id=45)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    svc.submit_plan(account, plan, {}, run_tag="46", allow_fractional=False)

    assert account.submit_closing_flags == [False]


def test_submit_plan_reports_washtrade_locked_instead_of_treating_it_as_success():
    account = FakeAccount(account_id=6)
    account.positions = []
    account.washtrade_symbols = {"AAPL"}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="20", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_WASHTRADE_LOCKED


def test_submit_plan_hard_failure_reports_the_reason_left_on_the_order_comment():
    account = FakeAccount(account_id=8)
    account.positions = []
    account.reject_quantities = {10.0}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="21", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "broker rejected" in outcomes[0].message


def test_submit_plan_reads_the_rejection_reason_off_the_persisted_order_row():
    """AccountInterface._handle_order_submit_error writes the broker's words onto
    the DATABASE row (`fresh_order`), never onto the caller's detached object.
    Reading only the in-memory comment reports our own run stamp back as if it
    were the failure reason."""
    account = FakeAccount(account_id=47)
    account.positions = []
    account.db_reject_quantities = {10.0: "insufficient buying power (40310000)"}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="48", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "insufficient buying power (40310000)" in outcomes[0].message
    # ...and the run stamp is not reported as if it were the broker's complaint.
    assert not outcomes[0].message.startswith("Portfolio allocation run")


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.ERROR,
                                    OrderStatus.CANCELED, OrderStatus.EXPIRED])
def test_submit_plan_does_not_call_a_terminally_dead_order_submitted(status):
    """The adapter returning an object is not the same as the broker accepting it.
    An outcome table that says "submitted" for a REJECTED order is the run lying
    about what happened at the broker."""
    account = FakeAccount(account_id=49)
    account.positions = []
    account.terminal_statuses = {"AAPL": status}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="50", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert status.value in outcomes[0].message


def test_submit_plan_reports_a_partial_fill_as_partial_not_as_a_clean_submit():
    account = FakeAccount(account_id=51)
    account.positions = []
    account.partial_fills = {"AAPL": 4.0}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="52", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_PARTIAL
    assert outcomes[0].quantity == pytest.approx(10.0)     # what was SENT
    assert outcomes[0].filled_quantity == pytest.approx(4.0)  # what actually FILLED


def test_submit_plan_never_surfaces_a_raw_broker_exception():
    account = FakeAccount(account_id=53)
    account.positions = []
    account.raise_quantities = {10.0: RuntimeError("SSLError: connection reset by peer")}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="54", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "connection reset by peer" in outcomes[0].message
    assert outcomes[0].order_ids  # the row we persisted is still reported


# ---------------------------------------------------------------------------
# submit_plan -- the ADJUST branch.
#
# NOTHING here monkeypatches TransactionHelper. The three bugs this section now
# pins (a rejected add reported as submitted, a trim that sends nothing, a
# multi-transaction trim the helper refuses outright) all live INSIDE the helper
# or in the contract between it and the service, and every one of them survived
# a suite whose ADJUST tests replaced the helper with a lambda that returned
# success. The broker is faked at the ACCOUNT seam instead.
# ---------------------------------------------------------------------------

def _trim_plan(symbol="AAPL", delta=-25.0, target=5.0, price=106.0):
    row = make_row(symbol, OrderDirection.SELL, delta, abs(delta) * price, 0.0,
                   price=price)
    row.target_quantity = target
    return AllocationPlan(rows=[row], available_buying_power=10_000.0)


def test_submit_plan_trim_on_a_position_with_no_tpsl_legs_reaches_the_broker():
    """C2. A position this feature opened has NO active TP/SL legs --
    ``_submit_new_order`` attaches none -- so the helper's triggered chain has
    nothing to hang off. The partial-close order it creates is left PENDING, and
    TradeManager's ``_check_all_waiting_trigger_orders`` only ever looks at
    WAITING_TRIGGER rows (its PENDING sweep DELETES them). Nothing would ever
    send it, while the run reported the trim as submitted and wrote the
    transaction down to the target."""
    account = FakeAccount(account_id=9)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    txn_id = make_open_transaction(9, "AAPL", 30.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="22",
                               allow_fractional=False)

    assert [(s[0], s[1], s[2]) for s in account.submitted] == [
        ("AAPL", OrderDirection.SELL, 25.0)]
    assert outcomes[0].action == ACTION_ADJUST
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].quantity == pytest.approx(25.0)
    assert txn_quantity(txn_id) == pytest.approx(5.0)


def test_submit_plan_trim_the_broker_refuses_does_not_write_the_transaction_down():
    """C2, the other half. The sell never happened, so the position is still 30
    shares. Writing it down to 5 anyway makes every later valuation, every later
    delta and the next run's dry run wrong -- and reports the row as submitted."""
    account = FakeAccount(account_id=91)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    account.reject_everything = True
    txn_id = make_open_transaction(91, "AAPL", 30.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="91",
                               allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert txn_quantity(txn_id) == pytest.approx(30.0)


def test_submit_plan_trim_arms_the_triggered_chain_when_there_is_a_live_tpsl_leg():
    """The designed path, pinned so the C2 fix cannot swallow it: with a live
    TP/SL leg the partial close is created WAITING_TRIGGER on that leg's cancel
    and TradeManager submits it when the cancel lands. Sending it from here as
    well would put TWO sells against one position."""
    account = FakeAccount(account_id=92)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    txn_id = make_open_transaction(92, "AAPL", 30.0)
    tp_id = make_active_tp_order(92, txn_id, "AAPL", 30.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="92",
                               allow_fractional=False)

    assert account.canceled == [tp_id]
    assert account.submitted == []          # the chain is armed, not fired
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    chain = [get_instance(TradingOrder, i) for i in outcomes[0].order_ids]
    assert [o.status for o in chain] == [OrderStatus.WAITING_TRIGGER]
    assert txn_quantity(txn_id) == pytest.approx(5.0)


def test_submit_plan_trim_whose_tpsl_cancel_the_broker_refuses_sends_nothing():
    """C2 again, by a different route. The partial close waits on that ONE leg
    reaching CANCELED. A cancel the broker refuses leaves it waiting forever --
    nothing is sent, and the transaction must not be written down for it."""
    account = FakeAccount(account_id=93)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    txn_id = make_open_transaction(93, "AAPL", 30.0)
    tp_id = make_active_tp_order(93, txn_id, "AAPL", 30.0)
    account.cancel_failures = {tp_id}
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="93",
                               allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert txn_quantity(txn_id) == pytest.approx(30.0)
    # ...and the stranded close order can never fire later.
    stranded = [get_instance(TradingOrder, i) for i in outcomes[0].order_ids]
    assert all(o.status == OrderStatus.CANCELED for o in stranded), stranded


def test_submit_plan_add_to_position_the_broker_refused_is_not_reported_submitted():
    """C1. ``adjust_quantity_with_tpsl`` submitted the add order, got None back
    -- the row already stamped ERROR by ``_handle_order_submit_error`` -- and
    carried on to write the transaction UP as though it had filled. The account
    holds 10 shares; the run said 15 and spent the income ledger for 5 shares
    that do not exist."""
    account = FakeAccount(account_id=94)
    account.positions = [FakePosition("AAPL", 10.0, 1060.0, 1060.0)]
    account.reject_everything = True
    txn_id = make_open_transaction(94, "AAPL", 10.0)
    row = make_row("AAPL", OrderDirection.BUY, 5.0, 530.0, 530.0, price=106.0)
    row.target_quantity = 15.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="94",
                               allow_fractional=False)

    assert outcomes[0].action == ACTION_ADJUST
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert txn_quantity(txn_id) == pytest.approx(10.0)
    # The refused row is still reported, so the audit can point at it.
    assert outcomes[0].order_ids
    refused = get_instance(TradingOrder, outcomes[0].order_ids[0])
    assert refused.status == OrderStatus.ERROR


def test_submit_plan_add_to_position_wash_trade_locked_is_reported_locked_not_failed():
    """BUG FIX 2026-09-04: this add-to-position used to come back OUTCOME_FAILED,
    on a leg that is actually still PENDING at our end -- TradeManager retries a
    WASHTRADE_LOCKED order the moment its blocker clears, so it is not dead the
    way a broker rejection is. Reporting it FAILED told the user nothing more
    would happen, right before the platform bought the position back on its own
    with no further warning."""
    account = FakeAccount(account_id=96)
    account.positions = [FakePosition("AAPL", 10.0, 1060.0, 1060.0)]
    account.washtrade_symbols = {"AAPL"}
    txn_id = make_open_transaction(96, "AAPL", 10.0)
    row = make_row("AAPL", OrderDirection.BUY, 5.0, 530.0, 530.0, price=106.0)
    row.target_quantity = 15.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="96",
                               allow_fractional=False)

    assert outcomes[0].action == ACTION_ADJUST
    assert outcomes[0].status == svc.OUTCOME_WASHTRADE_LOCKED
    assert txn_quantity(txn_id) == pytest.approx(10.0)
    # The locked order is still reported, so a later fill can be traced to it --
    # and it is genuinely still armed, not a dead end.
    assert outcomes[0].order_ids
    locked = get_instance(TradingOrder, outcomes[0].order_ids[0])
    assert locked.status == OrderStatus.WASHTRADE_LOCKED


def test_submit_plan_add_to_position_the_broker_took_is_reported_submitted():
    """The companion to C1: the accepted add still works, end to end."""
    account = FakeAccount(account_id=95)
    account.positions = [FakePosition("AAPL", 10.0, 1060.0, 1060.0)]
    txn_id = make_open_transaction(95, "AAPL", 10.0)
    row = make_row("AAPL", OrderDirection.BUY, 5.0, 530.0, 530.0, price=106.0)
    row.target_quantity = 15.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="95",
                               allow_fractional=False)

    assert [(s[0], s[1], s[2]) for s in account.submitted] == [
        ("AAPL", OrderDirection.BUY, 5.0)]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert txn_quantity(txn_id) == pytest.approx(15.0)


def test_submit_plan_trim_across_two_transactions_closes_the_one_it_exhausts():
    """I1. ``split_delta_fifo`` returns [(t1,-20),(t2,-5)] for 30 shares held as
    20 + 10 -- the first leg EXACTLY exhausts t1, by construction, whenever a
    trim spans more than one transaction. ``adjust_quantity_with_tpsl`` is a
    PARTIAL-close API and refuses ``close_qty >= current_qty``, so that leg used
    to be rejected outright: the trim under-sold by 20 shares and could never
    converge, while the dry run promised the full 25."""
    account = FakeAccount(account_id=55)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    first = make_open_transaction(55, "AAPL", 20.0)
    second = make_open_transaction(55, "AAPL", 10.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="56",
                               allow_fractional=False)

    # The exhausting leg goes through close_transaction; the remainder through
    # the adjust path. Together they really do sell 25.
    assert account.closed == [first]
    assert [(s[0], s[2]) for s in account.submitted] == [("AAPL", 5.0)]
    assert txn_quantity(second) == pytest.approx(5.0)
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].quantity == pytest.approx(25.0)


def test_submit_plan_adjust_reports_partial_when_one_leg_of_the_split_fails():
    """I3. Half the trim really went out. Reporting the row FAILED values those
    sells at ZERO in the run's fill measurement, and the run then over-consumes
    the income ledger by the whole amount they raised."""
    account = FakeAccount(account_id=96)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    first = make_open_transaction(96, "AAPL", 20.0)
    second = make_open_transaction(96, "AAPL", 10.0)
    account.close_failures = {first: "held for another order"}
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="97",
                               allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_PARTIAL
    assert outcomes[0].quantity == pytest.approx(5.0)   # what really went out
    assert "held for another order" in outcomes[0].message
    assert txn_quantity(first) == pytest.approx(20.0)
    assert txn_quantity(second) == pytest.approx(5.0)


def test_submit_plan_adjust_reports_failed_when_every_leg_fails():
    account = FakeAccount(account_id=98)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    first = make_open_transaction(98, "AAPL", 20.0)
    second = make_open_transaction(98, "AAPL", 10.0)
    account.close_failures = {first: "held for another order"}
    account.reject_everything = True
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, _trim_plan(), current, run_tag="99",
                               allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].quantity == pytest.approx(0.0)
    assert txn_quantity(first) == pytest.approx(20.0)
    assert txn_quantity(second) == pytest.approx(10.0)


def test_submit_plan_adjust_with_no_remaining_transaction_quantity_is_skipped():
    """Every open transaction is already at zero: there is nothing to trim, and
    sending a zero-quantity adjustment is an order the broker cancels."""
    account = FakeAccount(account_id=57)
    empty = make_open_transaction(57, "AAPL", 0.0)

    row = make_row("AAPL", OrderDirection.SELL, -5.0, 530.0, 0.0, price=106.0)
    row.target_quantity = 5.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[empty])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="58", allow_fractional=False)

    assert account.submitted == [] and account.closed == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED


# ---------------------------------------------------------------------------
# submit_plan -- the CLOSE branch
# ---------------------------------------------------------------------------

def test_submit_plan_close_keeps_going_after_one_transaction_refuses():
    """Three transactions in one symbol: a refusal on the first must not leave the
    other two open with the run reporting a single failure and nothing else.

    I3: 2 of the 3 closes really went to the broker, so the row is PARTIAL, not
    FAILED. A row whose order ids are dropped is valued at ZERO, so calling this
    FAILED tells the ledger those two sells raised nothing and the run
    over-consumes the income they actually funded."""
    account = FakeAccount(account_id=59)
    a = make_open_transaction(59, "MSFT", 2.0)
    b = make_open_transaction(59, "MSFT", 2.0)
    c = make_open_transaction(59, "MSFT", 1.0)
    account.close_failures = {a: "held for another order"}

    row = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    row.target_quantity = 0.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[a, b, c])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="60", allow_fractional=False)

    assert account.closed == [b, c]
    assert outcomes[0].action == ACTION_CLOSE
    assert outcomes[0].status == svc.OUTCOME_PARTIAL
    assert outcomes[0].quantity == pytest.approx(3.0)   # b + c, not the full 5
    assert outcomes[0].transaction_ids == [b, c]
    assert "held for another order" in outcomes[0].message


def test_submit_plan_close_records_the_order_the_broker_was_given():
    """I2. ``close_transaction`` hands back ``close_order_id`` (documented at
    AccountInterface.py:1567). Throwing it away leaves ``order_ids`` -- the
    column that says "every TradingOrder this run created" -- with no trace of
    the orders that closed the positions."""
    account = FakeAccount(account_id=100)
    txn_id = make_open_transaction(100, "MSFT", 5.0)
    row = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    row.target_quantity = 0.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="100", allow_fractional=False)

    # The id of the MARKET close order the fake persisted, and it must be a REAL
    # row: the run reads its orders back out of the DB to measure what filled.
    close_order_id = outcomes[0].order_ids[0]
    assert get_instance(TradingOrder, close_order_id).symbol == "MSFT"
    assert get_instance(TradingOrder, close_order_id).order_type == OrderType.MARKET


def test_submit_plan_close_keeps_going_after_one_transaction_explodes():
    account = FakeAccount(account_id=61)
    a = make_open_transaction(61, "MSFT", 2.0)
    b = make_open_transaction(61, "MSFT", 3.0)
    account.close_raises = {a: RuntimeError("broker timeout")}

    row = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    row.target_quantity = 0.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[a, b])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="62", allow_fractional=False)

    assert account.closed == [b]
    assert outcomes[0].status == svc.OUTCOME_PARTIAL
    assert outcomes[0].quantity == pytest.approx(3.0)
    assert "broker timeout" in outcomes[0].message


def test_submit_plan_close_reports_failed_when_every_transaction_refuses():
    account = FakeAccount(account_id=101)
    a = make_open_transaction(101, "MSFT", 2.0)
    b = make_open_transaction(101, "MSFT", 3.0)
    account.close_failures = {a: "held", b: "held"}

    row = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    row.target_quantity = 0.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[a, b])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="102", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].quantity == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# submit_plan -- the SKIP branch, and agreement with the dry run
# ---------------------------------------------------------------------------

def test_submit_plan_reports_skipped_rows_without_touching_the_broker():
    account = FakeAccount(account_id=10)
    account.positions = []
    skipped = AllocationRow(symbol="TSLA", price=None, delta_quantity=0.0, side=None,
                            skipped=True, reasons=["no price - skipped"])
    plan = AllocationPlan(rows=[skipped], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="23", allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].action == ACTION_SKIP
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert "no price" in outcomes[0].message


def test_submit_plan_leaves_a_sub_minimum_fractional_row_exactly_where_the_dry_run_left_it():
    """L2: the broker refuses a fractional order under $5, so the engine zeroed the
    DELTA and the dry run showed the row as suppressed with the broker's own
    reason. Submission has to agree: no order, no close, and the same sentence."""
    account = FakeAccount(account_id=63)
    account.positions = [FakePosition("SCHD", 3.0, 72.0, 75.0)]
    txn_id = make_open_transaction(63, "SCHD", 3.0)
    reason = REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(value=1.95, minimum=5)
    row = AllocationRow(symbol="SCHD", price=25.0, delta_quantity=0.0, side=None,
                        current_quantity=3.0, target_quantity=3.0, fractional=True,
                        reasons=[reason])
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0,
                          allow_fractional=True)
    current = {"SCHD": PositionState(symbol="SCHD", quantity=3.0, price=25.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="64", allow_fractional=True)

    assert account.submitted == []
    assert account.events == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert outcomes[0].message == reason


def test_submit_plan_refuses_a_plan_solved_on_a_different_fractional_setting():
    """``plan.allow_fractional`` is the setting the DRY RUN was computed with. If
    submission is handed a different one, ``plan_quantity_attempts`` sends a
    quantity the user never reviewed -- 2.0 shares where the table said 2.5.
    Refused before a single order goes out, not half way through."""
    account = FakeAccount(account_id=65)
    account.positions = []
    row = make_row("NVDA", OrderDirection.BUY, 2.5, 2250.0, 2250.0, price=900.0)
    row.target_quantity = 2.5
    row.fractional = True
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0,
                          allow_fractional=True)

    with pytest.raises(ValueError, match="allow_fractional"):
        svc.submit_plan(account, plan, {}, run_tag="66", allow_fractional=False)

    assert account.submitted == []


# ---------------------------------------------------------------------------
# submit_plan -- the UNACTIONABLE branch
#
# The equity filter (OPT-L2) keeps option transactions out of
# ``PositionState.transaction_ids``, and ``decide_symbol_action`` gates "held" on
# that list -- so for a symbol whose only transactions are option-classed the
# filter changed the ACTION, not merely the ids walked. The run then had nothing
# to do AND said "nothing to do", which is what a symbol already at target says.
# An assigned wheel is exactly that shape: 20 such transactions in the live DB,
# 13 of them open.
# ---------------------------------------------------------------------------

def _option_only_state(symbol="AAPL", shares=100.0, price=160.0, option_ids=(41, 42)):
    """100 shares at the broker, and every transaction behind them filtered out.

    ``transaction_ids`` EMPTY is what ``build_position_states`` produces for an
    assigned wheel once the equity filter has run.
    """
    return PositionState(symbol=symbol, quantity=shares, price=price,
                         transaction_ids=[],
                         unactionable_transaction_ids=list(option_ids))


def _exit_row(symbol="AAPL", delta=-100.0, target=0.0, price=160.0):
    row = make_row(symbol, OrderDirection.SELL, delta, abs(delta) * price, 0.0,
                   price=price)
    row.target_quantity = target
    return row


def test_submit_plan_an_exit_the_equity_planner_cannot_route_is_not_nothing_to_do():
    """The account holds the shares, the user set the label to 0%, and every open
    transaction for the symbol is one this planner does not act on. Reporting that
    as "skipped: nothing to do" is the unknown-reads-as-zero pattern wearing a UI
    label -- the run cannot act, and says what it would say if there were nothing
    to act on."""
    account = FakeAccount(account_id=700)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": _option_only_state()},
                               run_tag="701", allow_fractional=False)

    assert account.submitted == [] and account.closed == [] and account.events == []
    assert outcomes[0].status == svc.OUTCOME_UNACTIONABLE
    assert outcomes[0].status != svc.OUTCOME_SKIPPED
    assert outcomes[0].action == svc.ACTION_UNACTIONABLE
    # The size of the thing that did not happen, and the ids to go and look at.
    assert outcomes[0].quantity == pytest.approx(100.0)
    assert outcomes[0].transaction_ids == [41, 42]
    message = outcomes[0].message
    assert "nothing to do" not in message
    assert "100 share(s) of AAPL" in message
    assert "OPTION" in message
    assert "41, 42" in message


def test_submit_plan_the_same_exit_without_the_filtered_ids_is_the_OLD_silence():
    """The BEFORE, pinned. Identical broker shares, identical row, identical empty
    ``transaction_ids`` -- the ONLY difference is that the filtered-out ids were
    thrown away instead of carried. That one field is what separated "the account
    holds 100 shares this run cannot reach" from "nothing to do", and dropping it
    again brings the silence straight back."""
    account = FakeAccount(account_id=702)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)
    forgotten = PositionState(symbol="AAPL", quantity=100.0, price=160.0,
                              transaction_ids=[], unactionable_transaction_ids=[])

    outcomes = svc.submit_plan(account, plan, {"AAPL": forgotten},
                               run_tag="703", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert outcomes[0].message == "nothing to do"


def test_submit_plan_a_TRIM_of_an_option_only_holding_is_unactionable_too():
    """The trim path shares the defect, because it shares the gate: ``held`` is
    False for the same reason, so "reduce AAPL to 60%" went just as quiet as "set
    AAPL to 0%". ``target_quantity`` is what tells the two apart, and neither of
    them can be routed."""
    account = FakeAccount(account_id=704)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    row = _exit_row(delta=-40.0, target=60.0)
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": _option_only_state()},
                               run_tag="705", allow_fractional=False)

    assert account.submitted == [] and account.closed == []
    assert outcomes[0].status == svc.OUTCOME_UNACTIONABLE
    assert "100 share(s) of AAPL" in outcomes[0].message


def test_submit_plan_a_row_already_at_target_stays_quiet_even_with_options_behind_it():
    """DISCRIMINATOR. A row at target has a zero delta and no side, and there is
    nothing wrong with it -- even on a symbol that DOES carry filtered-out option
    transactions. Turning every such row loud would bury the one that matters."""
    account = FakeAccount(account_id=706)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    at_target = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=0.0,
                              side=None, current_quantity=100.0, target_quantity=100.0)
    plan = AllocationPlan(rows=[at_target], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": _option_only_state()},
                               run_tag="707", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert outcomes[0].message == "no delta"


def test_submit_plan_a_genuine_nothing_to_do_still_reads_as_nothing_to_do():
    """DISCRIMINATOR, and the exact sentence the loud path must not borrow. A SELL
    on a symbol the account is FLAT in reaches ``_submit_row``, decides SKIP and
    has no reason of its own -- there is genuinely nothing to sell and nothing to
    look at, so "nothing to do" is the truth here and must stay."""
    account = FakeAccount(account_id=720)
    account.positions = []
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)
    flat = PositionState(symbol="AAPL", quantity=0.0, price=160.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": flat}, run_tag="721",
                               allow_fractional=False)

    assert account.submitted == [] and account.closed == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert outcomes[0].message == "nothing to do"


def test_submit_plan_a_normal_equity_close_says_nothing_about_options():
    """DISCRIMINATOR. No transaction was filtered out, so there is nothing to
    mention: the row keeps the clean empty Detail it has always had. A note that
    appeared on every close would stop being read by the second run."""
    account = FakeAccount(account_id=708)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    txn_id = make_open_transaction(708, "AAPL", 100.0)
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=100.0, price=160.0,
                                     transaction_ids=[txn_id])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="709",
                               allow_fractional=False)

    assert account.closed == [txn_id]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].message == ""


def test_submit_plan_a_close_that_leaves_option_legs_behind_says_so():
    """The covered-call tell, restored. Before the equity filter this row reported
    ``partially_filled`` with the option refusal spelled out; after it, a clean
    green ``submitted`` with an empty Detail, because the leg was no longer in the
    list to fail on. The shares moved and the option did not, and that is the fact
    the row stopped mentioning. The STATUS is untouched -- the equity sale really
    was submitted."""
    account = FakeAccount(account_id=710)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    txn_id = make_open_transaction(710, "AAPL", 100.0)
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=100.0, price=160.0,
                                     transaction_ids=[txn_id],
                                     unactionable_transaction_ids=[41])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="711",
                               allow_fractional=False)

    assert account.closed == [txn_id]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert "1 open OPTION transaction(s) on AAPL" in outcomes[0].message
    assert "41" in outcomes[0].message
    assert "still open" in outcomes[0].message


def test_submit_plan_a_TRIM_that_leaves_option_legs_behind_says_so_as_well():
    """Same note on the ADJUST path: a partial reduction of a covered-call symbol
    sells part of the cover and leaves the option exactly where it was."""
    account = FakeAccount(account_id=712)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    txn_id = make_open_transaction(712, "AAPL", 100.0)
    row = _exit_row(delta=-40.0, target=60.0)
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=100.0, price=160.0,
                                     transaction_ids=[txn_id],
                                     unactionable_transaction_ids=[41])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="713",
                               allow_fractional=False)

    assert outcomes[0].action == ACTION_ADJUST
    assert "1 open OPTION transaction(s) on AAPL" in outcomes[0].message


def test_submit_plan_a_BUY_into_an_option_only_holding_still_opens():
    """DISCRIMINATOR. Only the SELL side had nowhere to go: a top-up submits
    ``delta_quantity`` through a brand new equity transaction, which is correct
    and cannot double-buy. Refusing it would break a working path for the sake of
    a message."""
    account = FakeAccount(account_id=714)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    buy = make_row("AAPL", OrderDirection.BUY, 25.0, 4_000.0, 4_000.0, price=160.0)
    buy.target_quantity = 125.0
    plan = AllocationPlan(rows=[buy], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": _option_only_state()},
                               run_tag="715", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].action == ACTION_NEW
    assert [(s, q) for s, _side, q, _c in account.submitted] == [("AAPL", 25.0)]


def test_submit_row_never_falls_through_to_a_BUY_on_an_action_it_does_not_know(
        monkeypatch):
    """The dispatch used to END in ``return _open_symbol(...)``, so any action the
    chain did not recognise placed a MARKET BUY. With a fourth action now in the
    vocabulary that fall-through is a live hazard, not a hypothetical."""
    account = FakeAccount(account_id=716)
    account.positions = []
    monkeypatch.setattr(svc, "decide_symbol_action", lambda row, state: "banana")
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {"AAPL": _option_only_state()},
                               run_tag="717", allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "banana" in outcomes[0].message


def test_run_allocation_names_an_unactionable_row_in_the_one_line_summary(activity):
    """The activity line is all most people read, and it names every outcome the
    vocabulary has for exactly this reason. A run whose only row could not be
    acted on is not a SUCCESS either -- nothing reached the broker."""
    account = FakeAccount(account_id=718)
    account.positions = [FakePosition("AAPL", 100.0, 15_000.0, 16_000.0)]
    plan = AllocationPlan(rows=[_exit_row()], available_buying_power=10_000.0)

    svc.run_allocation(account, plan, {"AAPL": _option_only_state()}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert "1 unactionable" in activity[0]["description"]
    assert activity[0]["severity"] != ActivityLogSeverity.SUCCESS
    assert activity[0]["data"]["rows"][0]["status"] == svc.OUTCOME_UNACTIONABLE
    assert activity[0]["data"]["rows"][0]["transaction_ids"] == [41, 42]


def test_run_allocation_still_calls_a_clean_run_a_success(activity):
    """DISCRIMINATOR for the severity change: an unactionable count of ZERO must
    not demote an ordinary run."""
    account = FakeAccount(account_id=719)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}

    svc.run_allocation(account, _buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert activity[0]["severity"] == ActivityLogSeverity.SUCCESS
    assert "0 unactionable" in activity[0]["description"]


# ---------------------------------------------------------------------------
# Task 73: fractional shares with a one-shot whole-share fallback
# ---------------------------------------------------------------------------

def _fractional_plan(symbol="NVDA", delta=2.5, price=900.0):
    row = make_row(symbol, OrderDirection.BUY, delta, abs(delta) * price,
                   abs(delta) * price, price=price)
    row.target_quantity = delta
    row.fractional = True
    return AllocationPlan(rows=[row], available_buying_power=10_000.0,
                          allow_fractional=True)


def test_submit_plan_retries_whole_shares_once_when_the_fractional_order_is_rejected():
    """I4's other side: the fallback still fires on a PROVEN refusal. A
    classified reason such as ``[insufficient_funds]`` can only be produced by
    parsing a REPLY from the broker, and a reply that says "no" is proof the
    order was not taken."""
    account = FakeAccount(account_id=11)
    account.positions = []
    account.reject_quantities = {2.5}   # broker refuses the fractional quantity

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="30",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5, 2.0]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].path == "whole"
    assert outcomes[0].quantity == pytest.approx(2.0)


def test_submit_plan_reports_the_fractional_path_when_it_is_accepted():
    account = FakeAccount(account_id=12)
    account.positions = []

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="31",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].path == "fractional"


def test_submit_plan_fractional_order_is_sent_good_for_day_as_a_market_order():
    account = FakeAccount(account_id=13)
    account.positions = []
    sent = []
    original = account.submit_order

    def spy(order, tp_price=None, sl_price=None, is_closing_order=False):
        sent.append((order.good_for, order.order_type))
        return original(order, tp_price, sl_price, is_closing_order)

    account.submit_order = spy

    svc.submit_plan(account, _fractional_plan(), {}, run_tag="32", allow_fractional=True)

    assert sent == [('day', OrderType.MARKET)]


def test_submit_plan_reports_skipped_not_failed_when_the_whole_share_floor_is_zero():
    account = FakeAccount(account_id=14)
    account.positions = []
    row = make_row("BRK.A", OrderDirection.BUY, 0.4, 260_000.0, 260_000.0, price=650_000.0)
    row.target_quantity = 0.4
    plan = AllocationPlan(rows=[row], available_buying_power=500_000.0,
                          allow_fractional=False)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="33", allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert "whole share" in outcomes[0].message


def test_submit_plan_does_not_retry_whole_shares_when_submit_order_raised():
    """I4. An exception out of ``submit_order`` proves NOTHING about broker state.

    ``AccountInterface.submit_order`` documents "Returns None **or raises an
    exception** if failed" and says nothing about whether the order was placed;
    it also runs code AFTER ``_submit_order_impl`` has handed back a LIVE order
    (the protective-leg block), so a raise is not evidence the broker is empty.
    Retrying on it can place a SECOND order for the same intent, and no amount of
    under-investing is worth that."""
    account = FakeAccount(account_id=67)
    account.positions = []
    account.raise_quantities = {2.5: RuntimeError(
        "HTTPSConnectionPool: Read timed out waiting for the order response")}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="68",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "check the broker" in outcomes[0].message


def test_submit_plan_does_not_retry_whole_shares_after_an_ambiguous_failure():
    """I4, the probed case. The fractional BUY reached the broker and was
    accepted, but the HTTP response was lost. ``_classify_order_error`` returns
    UNKNOWN for every error nobody has characterised -- which is exactly where a
    socket read timeout lands -- and ``_handle_order_submit_error`` then stamps
    the row ERROR and returns None, indistinguishable from a clean rejection.
    Firing the whole-share fallback here buys the position TWICE."""
    account = FakeAccount(account_id=104)
    account.positions = []
    account.ambiguous_quantities = {2.5: "connection reset while reading the response"}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="105",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].path == "fractional"
    assert "connection reset" in outcomes[0].message
    assert "check the broker" in outcomes[0].message


def test_submit_plan_does_not_retry_when_the_rejection_carries_no_classification():
    """An adapter that wrote words onto the row without going through
    ``_handle_order_submit_error`` left no ``[reason]`` tag, so there is nothing
    to reason from. Absence of evidence is not evidence of a refusal."""
    account = FakeAccount(account_id=106)
    account.positions = []
    account.db_reject_quantities = {2.5: "the broker said something we cannot parse"}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="107",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_FAILED


def test_submit_plan_reports_the_last_failure_when_the_whole_share_retry_also_fails():
    account = FakeAccount(account_id=69)
    account.positions = []
    account.reject_quantities = {2.5, 2.0}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="70",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5, 2.0]
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].path == "whole"
    assert outcomes[0].quantity == pytest.approx(2.0)


def test_submit_plan_does_not_retry_whole_shares_on_top_of_a_partial_fill():
    """Cancelled AFTER filling 1.5 of the 2.5. Retrying at 2.0 would buy 3.5
    shares of a 2.5-share target -- an OVERSHOOT created by the recovery path."""
    account = FakeAccount(account_id=71)
    account.positions = []
    account.partial_fills = {"NVDA": 1.5}
    account.terminal_statuses = {"NVDA": OrderStatus.CANCELED}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="72",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].filled_quantity == pytest.approx(1.5)


def test_submit_plan_never_sends_a_fractional_quantity_for_a_non_fractionable_symbol():
    """``row.fractional`` is False when the broker does not split the symbol. The
    engine already sized it whole; submission must not re-introduce the fraction
    even if the delta carries one."""
    account = FakeAccount(account_id=73)
    account.positions = []
    row = make_row("BRK.A", OrderDirection.BUY, 2.5, 1_625_000.0, 1_625_000.0,
                   price=650_000.0)
    row.target_quantity = 2.5
    row.fractional = False
    plan = AllocationPlan(rows=[row], available_buying_power=5_000_000.0,
                          allow_fractional=True)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="74", allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.0]
    assert outcomes[0].path == "whole"


def test_submit_plan_never_puts_a_fraction_on_a_non_market_order():
    """L1 end to end: whatever path a fractional row takes, every order that
    reaches the broker with a fractional quantity is a MARKET order good for the
    day -- the only shape either broker accepts one on."""
    account = FakeAccount(account_id=75)
    account.positions = []
    account.reject_quantities = {2.5}
    seen = []
    original = account.submit_order

    def spy(order, tp_price=None, sl_price=None, is_closing_order=False):
        seen.append((order.quantity, order.order_type, order.good_for))
        return original(order, tp_price, sl_price, is_closing_order)

    account.submit_order = spy

    svc.submit_plan(account, _fractional_plan(), {}, run_tag="76", allow_fractional=True)

    assert len(seen) == 2
    for quantity, order_type, good_for in seen:
        if quantity != float(int(quantity)):
            assert order_type == OrderType.MARKET
            assert good_for == 'day'


def test_submit_plan_reports_both_order_rows_the_fallback_created():
    """The rejected fractional attempt leaves a persisted TradingOrder behind
    (marked ERROR by the adapter). The run audit has to be able to find it, so
    the outcome carries both ids -- not just the one that worked."""
    account = FakeAccount(account_id=77)
    account.positions = []
    account.reject_quantities = {2.5}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="78",
                               allow_fractional=True)

    assert len(outcomes[0].order_ids) == 2
    rejected, accepted = (get_instance(TradingOrder, i) for i in outcomes[0].order_ids)
    assert rejected.quantity == pytest.approx(2.5)
    assert accepted.quantity == pytest.approx(2.0)


def test_submit_plan_does_not_follow_an_accepted_fractional_order_with_a_second_one():
    """The broker ACCEPTED 2.5 shares and has not filled any of them yet, which
    is the ordinary shape of a market order placed before the open. Nothing has
    filled, so the "don't top up a partial fill" guard cannot help here: it is the
    SUCCESS check that has to stop the loop. Without it the run buys 4.5 shares
    against a 2.5-share target."""
    account = FakeAccount(account_id=79)
    account.positions = []
    account.accepted_symbols = {"NVDA"}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="80",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].path == "fractional"
    assert outcomes[0].filled_quantity is None  # accepted is not filled


def test_submit_plan_does_not_retry_whole_shares_after_a_washtrade_lock():
    """A locked order is not a rejected one: it is PENDING at our end and will be
    retried on the next refresh once the blocker clears. Sending a whole-share
    order behind it queues a SECOND order for the same symbol, and both will
    eventually fill."""
    account = FakeAccount(account_id=81)
    account.positions = []
    account.washtrade_symbols = {"NVDA"}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="82",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].status == svc.OUTCOME_WASHTRADE_LOCKED


# ---------------------------------------------------------------------------
# Task 74: the income ledger -- broker sync + the page's read wrappers.
#
# The clock is frozen through ``svc._today`` rather than by patching
# ``datetime``: the window start, the window end and the display cutoff are all
# derived from that ONE read, so freezing it is what makes "the 30-day window"
# a testable statement instead of a race against midnight.
# ---------------------------------------------------------------------------

FROZEN_TODAY = date(2026, 5, 14)


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(svc, "_today", lambda: FROZEN_TODAY)
    return FROZEN_TODAY


def deposit(external_id, day, amount=5_000.0):
    return CashTransfer(external_id=external_id, event_date=date(2026, 5, day),
                        event_type=CASH_TRANSFER_DEPOSIT, amount=amount)


def income_rows():
    with get_db() as session:
        return session.exec(select(PortfolioIncomeEvent)).all()


def test_sync_income_events_writes_deposits_and_dividends(frozen_today):
    account = FakeAccount(account_id=21)
    account.cash_transfers = [
        deposit("csd-1", 1),
        CashTransfer(external_id="div-1", event_date=date(2026, 5, 5),
                     event_type=CASH_TRANSFER_DIVIDEND, amount=42.5, symbol="AAPL"),
    ]

    written = svc.sync_income_events(account)

    assert written == 2
    rows = income_rows()
    assert {r.external_id for r in rows} == {"csd-1", "div-1"}
    assert {r.symbol for r in rows} == {None, "AAPL"}


def test_sync_income_events_skips_withdrawals(frozen_today):
    account = FakeAccount(account_id=22)
    account.cash_transfers = [
        CashTransfer(external_id="csw-1", event_date=date(2026, 5, 2),
                     event_type=CASH_TRANSFER_WITHDRAWAL, amount=-1_000.0),
    ]

    assert svc.sync_income_events(account) == 0
    assert income_rows() == []


def test_sync_income_events_skips_a_reversed_deposit(frozen_today):
    """A DEPOSIT can arrive NEGATIVE (a reversed ACH). ``CashTransfer.is_income``
    is the rule -- filtering on the event TYPE alone would file a -1,000 reversal
    as income and hand the next run a negative event to spend."""
    account = FakeAccount(account_id=26)
    account.cash_transfers = [
        CashTransfer(external_id="csd-rev", event_date=date(2026, 5, 2),
                     event_type=CASH_TRANSFER_DEPOSIT, amount=-1_000.0),
    ]

    assert svc.sync_income_events(account) == 0
    assert income_rows() == []


def test_sync_income_events_is_idempotent_on_the_broker_activity_id(frozen_today):
    account = FakeAccount(account_id=23)
    account.cash_transfers = [deposit("csd-9", 1)]

    assert svc.sync_income_events(account) == 1
    assert svc.sync_income_events(account) == 0

    assert len(income_rows()) == 1


def test_sync_income_events_overwrites_the_amount_instead_of_summing_it(frozen_today):
    """The page re-syncs the WHOLE 30-day window on every load, so an event is
    presented again and again. Accumulating instead of restating would inflate the
    ledger on every page load until it funded a run out of thin air."""
    account = FakeAccount(account_id=28)
    account.cash_transfers = [deposit("csd-1", 1, amount=1_000.0)]

    for _ in range(3):
        svc.sync_income_events(account)

    assert [r.amount for r in income_rows()] == [pytest.approx(1_000.0)]
    assert svc.get_open_income_total(28) == pytest.approx(1_000.0)


def test_sync_income_events_follows_a_restated_amount_down(frozen_today):
    """The other half of "overwrite, never sum": a DIVNRA tax leg restates a
    dividend NET of withholding, and the ledger has to follow it down."""
    account = FakeAccount(account_id=29)
    account.cash_transfers = [
        CashTransfer(external_id="div-1", event_date=date(2026, 5, 5),
                     event_type=CASH_TRANSFER_DIVIDEND, amount=100.0, symbol="KO"),
    ]
    svc.sync_income_events(account)

    account.cash_transfers[0].amount = 85.0
    assert svc.sync_income_events(account) == 0

    assert svc.get_open_income_total(29) == pytest.approx(85.0)


def test_sync_income_events_does_not_reset_what_a_run_already_consumed(frozen_today):
    """Money already spent stays spent across a re-sync; otherwise every page load
    would hand a finished run's income back to the next one."""
    account = FakeAccount(account_id=30)
    account.cash_transfers = [deposit("csd-1", 1, amount=1_000.0)]
    svc.sync_income_events(account)
    row = income_rows()[0]
    row.consumed_amount = 400.0
    update_instance(row)

    svc.sync_income_events(account)

    assert income_rows()[0].consumed_amount == pytest.approx(400.0)
    assert svc.get_open_income_total(30) == pytest.approx(600.0)


def test_sync_income_events_does_not_count_a_fully_consumed_event_as_new(frozen_today):
    """"New" means "not in the ledger", not "not in the OPEN ledger". A deposit a
    run has spent in full is still there, and re-reporting it as newly discovered
    on every load makes the count meaningless."""
    account = FakeAccount(account_id=36)
    account.cash_transfers = [deposit("csd-1", 1, amount=1_000.0)]
    svc.sync_income_events(account)
    row = income_rows()[0]
    row.consumed_amount = 1_000.0
    update_instance(row)

    assert svc.sync_income_events(account) == 0
    assert len(income_rows()) == 1


def test_sync_income_events_counts_only_the_events_it_had_never_seen(frozen_today):
    account = FakeAccount(account_id=37)
    account.cash_transfers = [deposit("csd-1", 1)]
    svc.sync_income_events(account)

    account.cash_transfers.append(deposit("csd-2", 3, amount=250.0))

    assert svc.sync_income_events(account) == 1
    assert len(income_rows()) == 2


def test_sync_income_events_counts_an_event_older_than_the_window_only_once(frozen_today):
    """A broker is free to ignore our date bounds. An event outside the window
    that we nevertheless persist must not be rediscovered as "new" on every single
    page load -- the known-set cutoff has to cover what is actually being written."""
    account = FakeAccount(account_id=38)
    account.cash_transfers = [
        CashTransfer(external_id="csd-old", event_date=date(2026, 1, 4),
                     event_type=CASH_TRANSFER_DEPOSIT, amount=900.0),
    ]

    assert svc.sync_income_events(account) == 1
    assert svc.sync_income_events(account) == 0


def test_sync_income_events_asks_the_broker_for_exactly_the_display_window(frozen_today):
    account = FakeAccount(account_id=39)

    svc.sync_income_events(account)

    assert account.cash_transfer_calls == [
        (FROZEN_TODAY - timedelta(days=svc.INCOME_WINDOW_DAYS), FROZEN_TODAY)]
    assert svc.INCOME_WINDOW_DAYS == 30


def test_sync_income_events_returns_zero_when_the_broker_call_fails(frozen_today):
    """A broker outage must not look like "there was no income"; it is logged and
    the existing ledger is left alone."""
    account = FakeAccount(account_id=27)
    add_instance(PortfolioIncomeEvent(
        account_id=27, external_id="csd-1", event_date=date(2026, 5, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    def _boom(start_date=None, end_date=None):
        raise RuntimeError("gateway timeout")

    account.get_cash_transfers = _boom
    assert svc.sync_income_events(account) == 0
    assert svc.get_open_income_total(27) == pytest.approx(5_000.0)


def test_sync_income_events_tolerates_a_broker_that_returns_nothing(frozen_today):
    """Unlike ``get_positions()``, this seam does NOT distinguish failure from
    emptiness (``ReadOnlyAccountInterface.get_cash_transfers``), so ``None`` is
    "no movements", not a fetch failure to raise on."""
    account = FakeAccount(account_id=40)
    account.cash_transfers = None

    assert svc.sync_income_events(account) == 0
    assert income_rows() == []


def test_get_open_income_total_sums_only_what_is_left():
    """The figure the page shows next to Invest. Consumption itself is exercised
    through ``run_allocation`` in Task 75 -- the ledger is only ever spent on
    behalf of a run, so there is nothing account-level to call here."""
    add_instance(PortfolioIncomeEvent(
        account_id=25, external_id="a", event_date=date(2026, 5, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0, consumed_amount=300.0))
    add_instance(PortfolioIncomeEvent(
        account_id=25, external_id="b", event_date=date(2026, 5, 5),
        event_type=CASH_TRANSFER_DIVIDEND, amount=500.0, consumed_amount=150.0))

    assert svc.get_open_income_total(25) == pytest.approx(350.0)


def test_get_open_income_total_is_scoped_to_one_account():
    add_instance(PortfolioIncomeEvent(
        account_id=41, external_id="a", event_date=date(2026, 5, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0))
    add_instance(PortfolioIncomeEvent(
        account_id=42, external_id="a", event_date=date(2026, 5, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=900.0))

    assert svc.get_open_income_total(41) == pytest.approx(300.0)


def test_get_recent_income_events_returns_display_dicts_newest_first(frozen_today):
    add_instance(PortfolioIncomeEvent(
        account_id=43, external_id="old", event_date=date(2026, 5, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0, consumed_amount=100.0))
    add_instance(PortfolioIncomeEvent(
        account_id=43, external_id="new", event_date=date(2026, 5, 10),
        event_type=CASH_TRANSFER_DIVIDEND, amount=42.0, symbol="AAPL"))

    events = svc.get_recent_income_events(43)

    assert [e["external_id"] for e in events] == ["new", "old"]
    assert events[1]["open_amount"] == pytest.approx(200.0)
    assert events[0]["symbol"] == "AAPL"
    assert events[1]["symbol"] is None


def test_get_recent_income_events_excludes_events_older_than_the_window(frozen_today):
    add_instance(PortfolioIncomeEvent(
        account_id=44, external_id="ancient", event_date=date(2026, 1, 4),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0))
    add_instance(PortfolioIncomeEvent(
        account_id=44, external_id="inside", event_date=date(2026, 5, 10),
        event_type=CASH_TRANSFER_DEPOSIT, amount=42.0))

    assert [e["external_id"] for e in svc.get_recent_income_events(44)] == ["inside"]


def test_get_recent_income_events_never_reports_a_negative_open_amount(frozen_today):
    """Reachable: a DIVNRA tax leg restates a dividend BELOW what a run already
    spent of it. ``consumed_amount`` is deliberately left alone as the true record
    of the spend, so the panel must show 0 left, never a negative."""
    add_instance(PortfolioIncomeEvent(
        account_id=45, external_id="div-1", event_date=date(2026, 5, 10),
        event_type=CASH_TRANSFER_DIVIDEND, amount=85.0, consumed_amount=100.0,
        symbol="KO"))

    events = svc.get_recent_income_events(45)

    assert events[0]["open_amount"] == pytest.approx(0.0)
    assert svc.get_open_income_total(45) == pytest.approx(0.0)


def test_get_recent_income_events_is_empty_for_an_account_with_no_income(frozen_today):
    assert svc.get_recent_income_events(46) == []


# ---------------------------------------------------------------------------
# Task 75: what a run actually committed, the run row, and the activity log.
# ---------------------------------------------------------------------------


def make_base(buying_power=10_000.0, managed_value=5_000.0, cash=4_000.0):
    return BaseSnapshot(
        available_buying_power=buying_power,
        managed_value=managed_value,
        base_notional=buying_power + managed_value,
        default_bp_factor=1.0,
        cash=cash,
    )


def the_run():
    with get_db() as session:
        return session.exec(select(PortfolioAllocationRun)).one()


@pytest.fixture
def activity(monkeypatch):
    """Capture log_activity instead of queueing it onto the async worker."""
    calls = []
    monkeypatch.setattr(
        svc, "log_activity",
        lambda severity, activity_type, description, data=None,
        source_expert_id=None, source_account_id=None: calls.append({
            "severity": severity, "type": activity_type, "description": description,
            "data": data, "account_id": source_account_id,
        }))
    return calls


# -- run_allocation ---------------------------------------------------------


def _buy_plan(symbol="AAPL", quantity=10.0, price=160.0, **plan_kwargs):
    row = make_row(symbol, OrderDirection.BUY, quantity, quantity * price,
                   quantity * price, price=price)
    row.target_quantity = quantity
    plan_kwargs.setdefault("available_buying_power", 10_000.0)
    return AllocationPlan(rows=[row], **plan_kwargs)


def test_run_allocation_persists_a_run_row_carrying_the_plan_and_the_order_ids(activity):
    account = FakeAccount(account_id=31)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 161.0)}
    plan = _buy_plan(base_notional=15_000.0, total_buy_value=1600.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.id == result["run_id"]
    assert run.account_id == 31
    assert run.mode == ALLOCATION_MODE_REBALANCE
    assert run.base_notional == pytest.approx(15_000.0)
    # 10 @ 161 FILLED -- the FILL price, not the plan's 160 quote.
    assert run.filled_buy_value == pytest.approx(1610.0)
    assert run.order_ids == result["outcomes"][0].order_ids
    assert run.plan_json["rows"][0]["symbol"] == "AAPL"


def test_run_allocation_carries_the_scope_label_of_an_invest_run(activity):
    account = FakeAccount(account_id=47)
    account.positions = []

    svc.run_allocation(account, _buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_INVEST_LABEL, scope_label="ARK26")

    run = the_run()
    assert run.mode == ALLOCATION_MODE_INVEST_LABEL
    assert run.scope_label == "ARK26"


def test_run_allocation_stamps_the_run_id_into_every_order_comment(activity):
    account = FakeAccount(account_id=32)
    account.positions = []

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    comment = account.submitted[0][3]
    assert str(result["run_id"]) in comment
    assert "closing" not in comment.lower()


def test_run_allocation_partial_failure_records_only_what_filled(activity):
    account = FakeAccount(account_id=33)
    account.positions = []
    account.reject_quantities = {4.0}
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    ok = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    ok.target_quantity = 10.0
    bad = make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0)
    bad.target_quantity = 4.0
    plan = AllocationPlan(rows=[ok, bad], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.filled_buy_value == pytest.approx(1600.0)
    statuses = {o.symbol: o.status for o in result["outcomes"]}
    assert statuses["AAPL"] == svc.OUTCOME_SUBMITTED
    assert statuses["NVDA"] == svc.OUTCOME_FAILED


def test_run_allocation_finalises_a_run_in_which_every_row_failed(activity):
    """A run that submitted nothing must still be finalised. Left unstamped it
    sits in ``get_unconsumed_runs()`` looking like a run that died mid-submit --
    the one signal that is supposed to mean "a human has to check the broker"."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_unconsumed_runs

    account = FakeAccount(account_id=48)
    account.positions = []
    account.reject_quantities = {10.0}

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["filled_buy_value"] == pytest.approx(0.0)
    assert the_run().filled_buy_value == pytest.approx(0.0)
    assert get_unconsumed_runs(48) == []


def test_run_allocation_consumes_income_up_to_the_filled_net_buy_value(activity):
    account = FakeAccount(account_id=34)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=34, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_INVEST_LABEL, scope_label="ARK26")

    assert result["income_consumed"] == pytest.approx(1600.0)
    assert svc.get_open_income_total(34) == pytest.approx(3_400.0)


def test_run_allocation_reports_a_ledger_shortfall_without_calling_it_an_error(activity):
    """Buying power, not the ledger, is the feasibility constraint: a run may buy
    more than the income it has, and the ledger simply gives up everything it has."""
    account = FakeAccount(account_id=49)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=49, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=500.0))

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["filled_buy_value"] == pytest.approx(1600.0)
    assert result["income_consumed"] == pytest.approx(500.0)
    assert svc.get_open_income_total(49) == pytest.approx(0.0)


def test_run_allocation_of_a_rebalance_funded_by_its_own_sells_consumes_no_income(activity):
    account = FakeAccount(account_id=50)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0),
                     # None -> each MSFT order fills at its OWN quantity.
                     "MSFT": (OrderStatus.FILLED, None, 400.0)}
    add_instance(PortfolioIncomeEvent(account_id=50, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    txn_id = make_open_transaction(50, "MSFT", 5.0)
    buy = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    buy.target_quantity = 10.0
    sell = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    sell.target_quantity = 0.0
    plan = AllocationPlan(rows=[buy, sell], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[txn_id])}

    result = svc.run_allocation(account, plan, current, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["filled_sell_value"] == pytest.approx(2000.0)
    assert result["income_consumed"] == pytest.approx(0.0)
    assert svc.get_open_income_total(50) == pytest.approx(5_000.0)


def test_run_allocation_logs_the_activity_against_the_account(activity):
    account = FakeAccount(account_id=51)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=51, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_INVEST_LABEL, scope_label="ARK26")

    assert len(activity) == 1
    entry = activity[0]
    assert entry["severity"] == ActivityLogSeverity.SUCCESS
    assert entry["type"] == ActivityLogType.ORDER_SUBMITTED
    assert entry["account_id"] == 51
    assert str(result["run_id"]) in entry["description"]
    assert "ARK26" in entry["description"]
    assert entry["data"]["run_id"] == result["run_id"]
    assert entry["data"]["income_consumed"] == pytest.approx(1600.0)
    assert entry["data"]["rows"][0]["symbol"] == "AAPL"


def test_run_allocation_logs_a_failure_when_nothing_reached_the_broker(activity):
    """WARNING would read as "mostly fine" for a run in which every order was
    refused. ActivityLogSeverity.FAILURE exists for exactly this."""
    account = FakeAccount(account_id=52)
    account.positions = []
    account.reject_quantities = {10.0}

    svc.run_allocation(account, _buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert activity[0]["severity"] == ActivityLogSeverity.FAILURE


def test_run_allocation_spends_the_income_that_funded_a_cancelled_partial_fill(activity):
    """C3 end to end. 6 of 10 shares filled and the broker cancelled the rest.
    The run IS stamped, so it never reaches ``get_unconsumed_runs()`` either --
    valuing it at zero means nothing will EVER reconcile the money that left."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_unconsumed_runs

    account = FakeAccount(account_id=103)
    account.positions = []
    account.partial_fills = {"AAPL": 6.0}
    account.terminal_statuses = {"AAPL": OrderStatus.CANCELED}
    account.fills = {"AAPL": (OrderStatus.CANCELED, 6.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=103, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["outcomes"][0].status == svc.OUTCOME_FAILED
    assert result["outcomes"][0].filled_quantity == pytest.approx(6.0)
    assert result["filled_buy_value"] == pytest.approx(960.0)
    assert result["income_consumed"] == pytest.approx(960.0)
    assert svc.get_open_income_total(103) == pytest.approx(4_040.0)
    assert get_unconsumed_runs(103) == []


def test_run_allocation_does_not_spend_income_on_an_add_the_broker_refused(activity):
    """C1 end to end. The add never happened, so no income may be consumed for
    it -- and the transaction must still say 10 shares, not 15."""
    account = FakeAccount(account_id=108)
    account.positions = [FakePosition("AAPL", 10.0, 1060.0, 1060.0)]
    account.reject_everything = True
    txn_id = make_open_transaction(108, "AAPL", 10.0)
    add_instance(PortfolioIncomeEvent(account_id=108, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 5.0, 530.0, 530.0, price=106.0)
    row.target_quantity = 15.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[txn_id])}

    result = svc.run_allocation(account, plan, current, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["filled_buy_value"] == pytest.approx(0.0)
    assert result["income_consumed"] == pytest.approx(0.0)
    assert svc.get_open_income_total(108) == pytest.approx(5_000.0)
    assert txn_quantity(txn_id) == pytest.approx(10.0)


def test_run_allocation_will_not_settle_a_run_whose_fill_reported_no_quantity(activity):
    """THE income double-spend, end to end.

    The broker says FILLED and gives a price but no quantity. Read as a measured
    zero, the run settles, consumes 0 and takes its one-shot ``income_consumed_at``
    stamp -- so the 5,000 still reads as unallocated even though 1,600 of stock was
    just bought with it, and it can never be reconciled afterwards because the
    stamp is gone. Unsettled + unstamped is the only recoverable answer.
    """
    from ba2_trade_platform.core.portfolio_allocation_store import get_unconsumed_runs

    account = FakeAccount(account_id=111)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, NO_FILL_QTY_REPORTED, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=111, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    result = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["settled"] is False
    assert result["filled_buy_value"] == pytest.approx(0.0)
    assert result["income_consumed"] == pytest.approx(0.0)
    assert svc.get_open_income_total(111) == pytest.approx(5_000.0)
    # Recoverable: still listed, still unstamped, so a later pass can finish it.
    assert [r.id for r in get_unconsumed_runs(111)] == [result["run_id"]]
    assert the_run().income_consumed_at is None


def test_the_run_summary_says_what_filled_and_that_the_income_is_not_consumed(
        activity):
    """The one line most people read. A run that could not be measured shows
    "0 submitted ... 0 failed", so without the money figure and the unsettled
    sentence it reads as a clean success while the deposit still sits unallocated.
    Both halves are asserted here because both are what the user acts on."""
    account = FakeAccount(account_id=113)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, NO_FILL_QTY_REPORTED, 160.0)}

    svc.run_allocation(account, _buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    description = activity[0]["description"]
    assert "0.00 bought / 0.00 sold (filled)" in description
    assert "income not consumed yet" in description
    assert "refresh FAILED" not in description


def test_a_measured_run_says_what_it_bought_and_claims_nothing_is_outstanding(
        activity):
    """The contrast case, so the sentence above cannot be hard-coded: a run that
    really was measured reports its money and says nothing about waiting."""
    account = FakeAccount(account_id=114)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}

    svc.run_allocation(account, _buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    description = activity[0]["description"]
    assert "1600.00 bought / 0.00 sold (filled)" in description
    assert "income not consumed yet" not in description


def test_the_income_a_null_quantity_run_finally_measures_is_consumed_exactly_once(
        activity):
    """Idempotence across passes. Once the broker admits the quantity, the deferred
    run consumes its 1,600 ONE time; every later drain re-measures the same settled
    run and must take nothing more. The guard is ``income_consumed_at``, checked and
    set inside the same ``BEGIN IMMEDIATE`` transaction as the ledger writes."""
    account = FakeAccount(account_id=112)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, NO_FILL_QTY_REPORTED, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=112, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    first = svc.run_allocation(account, _buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert svc.get_open_income_total(112) == pytest.approx(5_000.0)

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    assert svc.reconcile_unconsumed_runs(account) == [first["run_id"]]
    assert svc.get_open_income_total(112) == pytest.approx(3_400.0)

    for _ in range(3):
        assert svc.reconcile_unconsumed_runs(account) == []
    assert svc.get_open_income_total(112) == pytest.approx(3_400.0)
    run = get_instance(PortfolioAllocationRun, first["run_id"])
    assert run.income_consumed_amount == pytest.approx(1_600.0)


def test_run_allocation_counts_the_sells_a_partly_successful_close_really_made(activity):
    """I3 end to end. Two of three transactions closed. Reporting the row FAILED
    values those two sells at ZERO, so the run treats the buys as unfunded and
    consumes the income ledger for money the sells had already raised."""
    account = FakeAccount(account_id=109)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0),
                     "MSFT": (OrderStatus.FILLED, None, 400.0)}
    a = make_open_transaction(109, "MSFT", 2.0)
    b = make_open_transaction(109, "MSFT", 2.0)
    c = make_open_transaction(109, "MSFT", 1.0)
    account.close_failures = {a: "held for another order"}
    add_instance(PortfolioIncomeEvent(account_id=109, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    buy = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    buy.target_quantity = 10.0
    sell = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    sell.target_quantity = 0.0
    plan = AllocationPlan(rows=[buy, sell], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[a, b, c])}

    result = svc.run_allocation(account, plan, current, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    # 3 of the 5 shares really were sold: 3 * 400 = 1200 against a 1600 buy.
    assert result["filled_sell_value"] == pytest.approx(1200.0)
    assert result["income_consumed"] == pytest.approx(400.0)
    assert svc.get_open_income_total(109) == pytest.approx(4_600.0)


def test_run_allocation_logs_a_warning_when_a_row_only_partly_went_out(activity):
    """A run in which a symbol only half executed is not a SUCCESS: the account
    is not where the user approved it should be, and the one-line summary is all
    most people ever read."""
    account = FakeAccount(account_id=110)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    a = make_open_transaction(110, "MSFT", 2.0)
    b = make_open_transaction(110, "MSFT", 3.0)
    account.close_failures = {a: "held for another order"}
    sell = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    sell.target_quantity = 0.0
    plan = AllocationPlan(rows=[sell], available_buying_power=10_000.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[a, b])}

    svc.run_allocation(account, plan, current, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert activity[0]["severity"] == ActivityLogSeverity.WARNING
    assert "1 partially filled" in activity[0]["description"]


def test_run_allocation_logs_a_warning_when_only_some_rows_failed(activity):
    account = FakeAccount(account_id=53)
    account.positions = []
    account.reject_quantities = {4.0}
    ok = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    ok.target_quantity = 10.0
    bad = make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0)
    bad.target_quantity = 4.0
    plan = AllocationPlan(rows=[ok, bad], available_buying_power=10_000.0)

    svc.run_allocation(account, plan, {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert activity[0]["severity"] == ActivityLogSeverity.WARNING


def test_run_allocation_submits_on_the_plans_own_fractional_setting(activity, monkeypatch):
    """Never on a flag the caller passes alongside: that is the setting the DRY
    RUN was solved with, and the two disagreeing sends quantities nobody saw."""
    seen = {}

    def _fake_submit(account, plan, current, *, run_tag, allow_fractional, on_order_id):
        seen.update(run_tag=run_tag, allow_fractional=allow_fractional)
        return []

    monkeypatch.setattr(svc, "submit_plan", _fake_submit)
    account = FakeAccount(account_id=54)
    plan = _buy_plan()
    plan.allow_fractional = True

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert seen == {"run_tag": str(result["run_id"]), "allow_fractional": True}
    assert the_run().allow_fractional is True


def test_run_allocation_records_an_empty_run_when_submission_refuses_the_plan(
        activity, monkeypatch):
    """``submit_plan`` validates BEFORE its first order and catches per row, so a
    raise out of it means nothing went out. Leaving the run unstamped would put a
    phantom into ``get_unconsumed_runs()`` -- the queue that is supposed to mean
    "money may have moved and only the broker knows"."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_unconsumed_runs

    def _refuse(*args, **kwargs):
        raise ValueError("the dry run the user approved is not what would be sent")

    monkeypatch.setattr(svc, "submit_plan", _refuse)
    account = FakeAccount(account_id=55)
    add_instance(PortfolioIncomeEvent(account_id=55, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))

    with pytest.raises(ValueError):
        svc.run_allocation(account, _buy_plan(), {}, make_base(),
                           mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.filled_buy_value == pytest.approx(0.0)
    assert get_unconsumed_runs(55) == []
    assert svc.get_open_income_total(55) == pytest.approx(5_000.0)


def test_finalising_a_run_twice_never_consumes_the_income_twice():
    """A retried submit must not spend the ledger again. The guard is
    ``portfolio_allocation_run.income_consumed_at``, checked and set in the same
    transaction as the ledger writes, so there is no window to lose money in."""
    from ba2_trade_platform.core.portfolio_allocation_store import (
        finalise_allocation_run, record_allocation_run,
    )
    add_instance(PortfolioIncomeEvent(account_id=35, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    run = record_allocation_run(35, ALLOCATION_MODE_REBALANCE, {})

    for _ in range(2):
        finalised = finalise_allocation_run(run.id, filled_buy_value=1600.0,
                                            filled_sell_value=0.0, order_ids=[101])
        assert finalised.income_consumed_amount == pytest.approx(1600.0)

    assert svc.get_open_income_total(35) == pytest.approx(3_400.0)


def test_run_allocation_finalises_a_run_that_had_nothing_to_submit(activity):
    """No order rows at all -- every row was suppressed by the engine. The run
    still has to be stamped: ``get_unconsumed_runs()`` means "money may have moved
    and only the broker knows", and a run that never created an order is the exact
    opposite of that. Finalising is also what proves it consumed no income."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_unconsumed_runs

    account = FakeAccount(account_id=56)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=56, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    suppressed = AllocationRow(
        symbol="SCHD", price=3.0, delta_quantity=0.0, side=None, fractional=True,
        reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(value=1.95, minimum=5.0)])
    plan = AllocationPlan(rows=[suppressed], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert account.submitted == [] and account.closed == []
    assert result["order_ids"] == []
    assert get_unconsumed_runs(56) == []
    assert svc.get_open_income_total(56) == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# The fractional choice is remembered
# ---------------------------------------------------------------------------

def test_remember_fractional_choice_persists_the_flag():
    from ba2_trade_platform.core.portfolio_allocation_store import get_allocation_config

    svc.remember_fractional_choice(4_101, False)
    assert get_allocation_config(4_101).allow_fractional is False
    svc.remember_fractional_choice(4_101, True)
    assert get_allocation_config(4_101).allow_fractional is True


def test_remember_fractional_choice_never_raises(monkeypatch):
    """Forgetting a preference must not take down a submission."""
    import sys
    module = sys.modules[svc.__name__]
    errors = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    monkeypatch.setattr("ba2_common.core.portfolio_allocation_store.set_allocation_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))

    svc.remember_fractional_choice(4_242, True)

    assert any("fractional choice" in e for e in errors), errors


def test_run_allocation_remembers_the_fractional_choice_it_ran_with():
    from ba2_trade_platform.core.portfolio_allocation_store import get_allocation_config

    account = FakeAccount(account_id=41)
    account.positions = []
    plan = AllocationPlan(rows=[], available_buying_power=1_000.0, allow_fractional=False)

    svc.run_allocation(account, plan, {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    assert get_allocation_config(41).allow_fractional is False


# ---------------------------------------------------------------------------
# Market-hours gating of the live Submit path
# ---------------------------------------------------------------------------
# Every market-hours test freezes the clock explicitly. 2026-08-20 17:00 UTC is
# 13:00 ET, mid-session; the close is 20:00 UTC == 16:00 ET.
FROZEN_NOW = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
MARKET_CLOSE = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)
NEXT_OPEN = datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)


def _closed_hours(status=None, detail=None):
    return MarketHours(is_open=False, source=MARKET_HOURS_SOURCE_BROKER,
                       as_of=FROZEN_NOW, open_at=NEXT_OPEN, close_at=None,
                       next_open=NEXT_OPEN, next_close=None,
                       status=status, detail=detail)


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages emitted by the service module.

    NOT caplog. Two independent reasons it cannot be used here:
      * ba2_trade_platform.logger installs its own handler and sets
        propagate = False, so caplog's ROOT handler never sees the record; and
      * tests/test_penny_gainers_fix.py replaces
        sys.modules["ba2_trade_platform.logger"] with a MagicMock at import time, so
        under a full-suite collection even re-enabling propagation patches a mock.
    Patching the module-under-test's own ``logger`` is immune to both.
    """
    import sys
    module = sys.modules[svc.__name__]
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def test_fetch_market_hours_returns_what_the_broker_published():
    account = FakeAccount(account_id=51)
    hours = svc.fetch_market_hours(account)
    assert hours is not None
    assert hours.is_open is True
    assert hours.is_known is True


def test_fetch_market_hours_returns_none_when_the_seam_raises():
    account = FakeAccount(account_id=52)
    account.market_hours_error = RuntimeError("clock endpoint 503")
    assert svc.fetch_market_hours(account) is None


def test_fetch_market_hours_returns_none_when_the_account_has_no_seam():
    class NoSeam:
        id = 53
    assert svc.fetch_market_hours(NoSeam()) is None


def test_fetch_market_hours_logs_the_failure_rather_than_swallowing_it(monkeypatch):
    errors = _capture_errors(monkeypatch)

    account = FakeAccount(account_id=54)
    account.market_hours_error = RuntimeError("clock endpoint 503")
    svc.fetch_market_hours(account)

    assert any("market hours" in e.lower() for e in errors), errors


def test_run_allocation_refuses_to_submit_while_the_market_is_closed():
    account = FakeAccount(account_id=55)
    account.positions = []
    account.market_hours = _closed_hours()
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1_600.0, 1_600.0, price=160.0)
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None
    assert result["outcomes"] == []
    assert account.submitted == []
    assert "closed" in result["blocked_reason"].lower()


def test_a_blocked_run_quotes_the_brokers_own_word_for_why():
    """D4: the gate is regular-session only, so a user blocked at 17:00 must be told
    that extended hours is not "open" here, not left guessing."""
    account = FakeAccount(account_id=59)
    account.positions = []
    account.market_hours = _closed_hours(status="Extended")
    plan = AllocationPlan(rows=[], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert "Extended" in result["blocked_reason"]


def test_run_allocation_blocked_by_a_closed_market_writes_no_run_row():
    """No run row means no order comments to stamp and, above all, no income
    consumed -- finalise_allocation_run is one-shot."""
    account = FakeAccount(account_id=1_056)
    account.positions = []
    account.market_hours = _closed_hours()
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)

    svc.run_allocation(account, plan, {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationRun).where(
            PortfolioAllocationRun.account_id == 1_056)).all()
    assert rows == []


def test_run_allocation_refuses_when_market_hours_cannot_be_read_at_all():
    """Unknown is not open. The alternative is submitting on a guess."""
    account = FakeAccount(account_id=57)
    account.positions = []
    account.market_hours_error = RuntimeError("clock endpoint 503")
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert account.submitted == []
    assert "not confirmed open" in result["blocked_reason"]


def test_run_allocation_refuses_an_unavailable_answer_even_though_it_is_not_none():
    """source=UNAVAILABLE carries is_open=False so the money path fails closed, and
    is_known=False so nobody may read it as "the market is shut"."""
    account = FakeAccount(account_id=60)
    account.positions = []
    account.market_hours = MarketHours(is_open=False, as_of=FROZEN_NOW,
                                       source=MARKET_HOURS_SOURCE_UNAVAILABLE,
                                       detail="broker and calendar both failed")
    plan = AllocationPlan(rows=[], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert "not confirmed open" in result["blocked_reason"]


def test_a_blocked_run_consumes_no_income_and_leaves_the_ledger_alone():
    """The reason the gate is the FIRST statement: finalise_allocation_run is
    one-shot, so a run created for orders that were all refused would mark that
    income spent forever."""
    account = FakeAccount(account_id=1_057)
    account.positions = []
    account.market_hours = _closed_hours()
    add_instance(PortfolioIncomeEvent(account_id=1_057, external_id="dep-1",
                                      event_date=date(2026, 5, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 10.0, 1_600.0,
                                         1_600.0, price=160.0)],
                          available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["income_consumed"] == 0.0
    assert svc.get_open_income_total(1_057) == pytest.approx(5_000.0)


def test_a_blocked_run_returns_every_key_an_unblocked_one_does():
    """The two branches must never disagree on shape: a caller reading
    result["filled_buy_value"] must not KeyError only when the market was open."""
    blocked_account = FakeAccount(account_id=61)
    blocked_account.positions = []
    blocked_account.market_hours = _closed_hours()
    open_account = FakeAccount(account_id=62)
    open_account.positions = []

    blocked = svc.run_allocation(blocked_account, AllocationPlan(rows=[]), {},
                                 make_base(), mode=ALLOCATION_MODE_REBALANCE)
    allowed = svc.run_allocation(open_account, AllocationPlan(rows=[]), {},
                                 make_base(), mode=ALLOCATION_MODE_REBALANCE)

    assert set(blocked) == set(allowed)
    assert blocked["filled_buy_value"] == 0.0
    assert blocked["filled_sell_value"] == 0.0
    assert blocked["settled"] is True
    assert blocked["working_order_ids"] == []
    assert allowed["blocked"] is False
    assert allowed["blocked_reason"] is None


def test_run_allocation_with_an_open_market_is_not_blocked():
    account = FakeAccount(account_id=58)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 1.0, 160.0)}
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is False
    assert result["blocked_reason"] is None
    assert result["run_id"] is not None
    assert result["settled"] is True
    assert result["filled_buy_value"] == pytest.approx(160.0)
    assert [s[0] for s in account.submitted] == ["AAPL"]


def _capture_warnings(monkeypatch):
    import sys
    module = sys.modules[svc.__name__]
    messages = []
    monkeypatch.setattr(module.logger, "warning",
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def test_the_two_unknown_market_hours_causes_are_distinguishable_in_the_log(monkeypatch):
    """Both block, and both are right to block -- but "this account class has no
    market-hours seam" is a code defect that will block EVERY submission from now
    on, while "the broker returned nothing" is a transient. Reporting them with the
    same sentence makes the permanent one look like the transient one."""
    warnings = _capture_warnings(monkeypatch)

    class NoSeam:
        id = 5_301

    assert svc.fetch_market_hours(NoSeam()) is None
    no_seam = list(warnings)
    assert len(no_seam) == 1, no_seam
    assert "5301" in no_seam[0] and "seam" in no_seam[0]

    warnings.clear()
    silent = FakeAccount(account_id=5_302)
    silent.market_hours = None
    assert svc.fetch_market_hours(silent) is None
    assert len(warnings) == 1, warnings
    assert "5302" in warnings[0] and "seam" not in warnings[0]


# ---------------------------------------------------------------------------
# The income ledger consumes FILLED value, never submitted value
# ---------------------------------------------------------------------------

def test_the_ledger_consumes_only_the_order_that_actually_filled(activity):
    """THE regression test. Two orders go out and the broker takes both; one fills,
    the other comes back REJECTED. Priced at plan value (the old behaviour) this
    consumes 1600 + 3600 = 5200 of a 6000 deposit. Priced at FILLED value it
    consumes 1600, and the 3600 that was never spent is still there to spend."""
    account = FakeAccount(account_id=40)
    account.positions = []
    account.fills = {
        "AAPL": (OrderStatus.FILLED, 10.0, 160.0),
        "NVDA": (OrderStatus.REJECTED, 0.0, None),
    }
    add_instance(PortfolioIncomeEvent(account_id=40, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    good = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    good.target_quantity = 10.0
    doomed = make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0)
    doomed.target_quantity = 4.0
    plan = AllocationPlan(rows=[good, doomed], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["income_consumed"] == pytest.approx(1600.0)
    assert svc.get_open_income_total(40) == pytest.approx(4_400.0)
    run = the_run()
    assert run.filled_buy_value == pytest.approx(1600.0)
    assert run.is_income_consumed is True


def test_a_partial_fill_consumes_only_the_filled_portion_and_defers_the_stamp(activity):
    account = FakeAccount(account_id=41)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.PARTIALLY_FILLED, 4.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=41, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    # The 640 that filled is recorded, but the order can still fill more, so the
    # ledger is untouched and the run waits in the recovery view.
    run = the_run()
    assert run.filled_buy_value == pytest.approx(640.0)
    assert run.is_income_consumed is False
    assert result["income_consumed"] == 0.0
    assert result["settled"] is False
    assert result["working_order_ids"] == run.order_ids
    assert svc.get_open_income_total(41) == pytest.approx(6_000.0)
    assert [r.id for r in svc.get_unconsumed_runs(41)] == [run.id]


def test_an_order_still_working_at_the_broker_leaves_the_run_recoverable(activity):
    account = FakeAccount(account_id=42)
    account.positions = []
    account.fills = {}   # the broker says nothing -> the row stays as submitted
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["settled"] is False
    assert result["income_consumed"] == 0.0
    assert [r.id for r in svc.get_unconsumed_runs(42)] == [result["run_id"]]


def test_reconcile_unconsumed_runs_finalises_a_run_once_its_orders_settle(activity):
    """The recovery drain. Yesterday's run left an order working; today it filled."""
    account = FakeAccount(account_id=43)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=43, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    first = svc.run_allocation(account, plan, {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert svc.get_open_income_total(43) == pytest.approx(6_000.0)

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 158.0)}
    reconciled = svc.reconcile_unconsumed_runs(account)

    assert reconciled == [first["run_id"]]
    assert svc.get_open_income_total(43) == pytest.approx(6_000.0 - 1580.0)
    assert svc.get_unconsumed_runs(43) == []


def test_reconcile_leaves_a_run_alone_while_its_orders_are_still_working(activity):
    account = FakeAccount(account_id=44)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    first = svc.run_allocation(account, plan, {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert svc.reconcile_unconsumed_runs(account) == []
    assert [r.id for r in svc.get_unconsumed_runs(44)] == [first["run_id"]]


def test_reconcile_is_a_no_op_and_asks_the_broker_nothing_when_nothing_is_pending():
    """It runs at the top of EVERY allocation run and on every income refresh, so
    the empty case must not cost a broker round trip."""
    account = FakeAccount(account_id=45)

    assert svc.reconcile_unconsumed_runs(account) == []
    assert account.refresh_calls == []


def test_a_protective_leg_is_neither_a_fill_nor_a_reason_to_wait():
    """adjust_quantity_with_tpsl returns its rebuilt TP/SL legs in orders_created
    (TransactionHelper.py:771/787/809). A SELL_STOP sitting unfilled for weeks must
    not count as a sale, and must not stall the run's income forever."""
    account = FakeAccount(account_id=46)
    account.positions = []
    market_id = add_instance(TradingOrder(
        account_id=46, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=10.0, open_price=160.0))
    stop_id = add_instance(TradingOrder(
        account_id=46, symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, good_for='gtc', status=OrderStatus.NEW,
        open_type=OrderOpenType.MANUAL, stop_price=140.0))

    fills = svc.collect_order_fills(46, [market_id, stop_id])

    assert [f.order_id for f in fills] == [market_id]
    totals = svc.measure_filled_values(fills)
    assert totals.settled is True
    assert totals.buy_value == pytest.approx(1600.0)
    assert totals.sell_value == 0.0


def test_collect_order_fills_reports_a_vanished_order_row_as_still_working():
    """An id in the run with no row behind it is an inconsistency, not an
    emptiness: stalling is the only safe reading, and it must be logged."""
    fills = svc.collect_order_fills(46, [9_999_999])

    assert [f.order_id for f in fills] == [9_999_999]
    assert fills[0].status is None
    assert svc.measure_filled_values(fills).settled is False


def test_collect_order_fills_hands_a_null_filled_qty_through_as_unknown():
    """A NULL ``filled_qty`` must reach the ledger AS a NULL.

    ``float(filled_qty or 0.0)`` turned it into a measured fill of zero shares one
    line above ``float(open_price) if open_price is not None else None`` -- the
    sibling field, on the very next line, already got this right. The whole income
    double-spend lived in that asymmetry.
    """
    order_id = add_instance(TradingOrder(
        account_id=46, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=None, open_price=160.0))

    fills = svc.collect_order_fills(46, [order_id])

    assert fills[0].filled_quantity is None
    assert fills[0].fill_price == pytest.approx(160.0)
    totals = svc.measure_filled_values(fills)
    assert totals.buy_value == 0.0
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [order_id]


def test_collect_order_fills_still_reads_a_real_zero_as_a_measured_zero():
    """The broker DID answer, and the answer was "nothing filled". That is a
    measurement, and it must not be confused with the NULL above."""
    order_id = add_instance(TradingOrder(
        account_id=46, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.REJECTED,
        open_type=OrderOpenType.MANUAL, filled_qty=0.0, open_price=None))

    fills = svc.collect_order_fills(46, [order_id])

    assert fills[0].filled_quantity == 0.0
    totals = svc.measure_filled_values(fills)
    assert totals.settled is True
    assert totals.unmeasurable_order_ids == []


def test_collect_order_fills_never_reads_another_accounts_order():
    """The query is scoped by account_id as well as by id. Without that, an id
    collision across accounts would price this run from someone else's fill."""
    other = add_instance(TradingOrder(
        account_id=999, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=10.0, open_price=160.0))

    fills = svc.collect_order_fills(46, [other])

    assert fills[0].status is None          # not visible == not measurable
    assert svc.measure_filled_values(fills).buy_value == 0.0


def test_a_full_close_counts_its_close_order_on_the_sell_side(activity):
    """_close_symbol has to hand back close_order_id, or the SELL side of a close is
    invisible and net_buy_value comes out too high -- over-consuming income."""
    account = FakeAccount(account_id=46_1)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    account.fills = {"MSFT": (OrderStatus.FILLED, 5.0, 398.0)}
    txn_id = make_open_transaction(46_1, "MSFT", 5.0)
    current = {"MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                                     transaction_ids=[txn_id])}
    row = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    row.target_quantity = 0.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    svc.run_allocation(account, plan, current, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.filled_sell_value == pytest.approx(1990.0)
    assert run.filled_buy_value == 0.0
    assert run.net_buy_value == 0.0
    assert run.is_income_consumed is True


def test_a_failed_refresh_never_consumes_income_against_stale_rows(activity, monkeypatch):
    """If the broker cannot be reached, our rows say whatever they said before.
    Consuming against that is guessing with money."""
    errors = _capture_errors(monkeypatch)

    account = FakeAccount(account_id=47)
    account.positions = []
    account.refresh_raises = RuntimeError("connection reset")
    add_instance(PortfolioIncomeEvent(account_id=47, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["income_consumed"] == 0.0
    assert result["settled"] is False
    assert svc.get_open_income_total(47) == pytest.approx(6_000.0)
    assert any("connection reset" in e for e in errors), errors


def test_a_failed_refresh_stops_a_reconcile_pass_rather_than_pricing_it_stale(
        activity, monkeypatch):
    """Same rule on the recovery path: a run left pending is safer than a run
    consumed against rows the broker never confirmed."""
    account = FakeAccount(account_id=48_1)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=48_1, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    first = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                       available_buying_power=10_000.0),
                               {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    account.refresh_raises = RuntimeError("connection reset")

    assert svc.reconcile_unconsumed_runs(account) == []
    assert [r.id for r in svc.get_unconsumed_runs(48_1)] == [first["run_id"]]
    assert svc.get_open_income_total(48_1) == pytest.approx(6_000.0)


def test_the_post_submit_refresh_asks_for_every_order(activity):
    """fetch_all=True, mirroring TradeManager.py:1607 -- a freshly submitted order
    is not in the 'open orders' window on every broker."""
    account = FakeAccount(account_id=48)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    svc.run_allocation(account, plan, {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert account.refresh_calls == [{'heuristic_mapping': False, 'fetch_all': True}]


def test_refresh_is_called_without_kwargs_for_a_broker_that_takes_none():
    """IBKRAccount.refresh_orders(self) takes no arguments (IBKRAccount.py:541), so
    refresh_orders(fetch_all=True) is a TypeError there."""
    calls = []

    class NoKwargsAccount:
        id = 49

        def refresh_orders(self):
            calls.append("bare")
            return True

    assert svc.refresh_orders_from_broker(NoKwargsAccount()) is True
    assert calls == ["bare"]


def test_refresh_passes_fetch_all_to_a_broker_that_only_takes_kwargs():
    """TastyTradeAccount.refresh_orders(self, **kwargs) (:988). fetch_all must
    still go in, or a market order that filled instantly is never seen."""
    calls = []

    class KwargsOnlyAccount:
        id = 50

        def refresh_orders(self, **kwargs):
            calls.append(kwargs)
            return True

    assert svc.refresh_orders_from_broker(KwargsOnlyAccount()) is True
    assert calls == [{"fetch_all": True}]


def test_a_typeerror_raised_inside_a_brokers_refresh_is_not_retried_bare(monkeypatch):
    """The signature is INSPECTED, not the TypeError caught: catching it and
    retrying without arguments would run a broker's whole refresh twice."""
    calls = []

    class ExplodingAccount:
        id = 51

        def refresh_orders(self, heuristic_mapping=False, fetch_all=False):
            calls.append(fetch_all)
            raise TypeError("something inside the adapter, not the signature")

    assert svc.refresh_orders_from_broker(ExplodingAccount()) is False
    assert calls == [True]


# ---------------------------------------------------------------------------
# The market gate stays FIRST, above everything this task added
# ---------------------------------------------------------------------------

def test_a_blocked_run_reports_the_full_money_contract_and_writes_nothing():
    """The blocked branch, preserved: money zeroed, settled True (nothing was
    submitted, so there is nothing to wait for), no working orders, no run row."""
    account = FakeAccount(account_id=59)
    account.positions = []
    account.market_hours = _closed_hours()
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None
    assert result["outcomes"] == []
    assert result["order_ids"] == []
    assert result["filled_buy_value"] == 0.0
    assert result["filled_sell_value"] == 0.0
    assert result["settled"] is True
    assert result["working_order_ids"] == []
    assert result["income_consumed"] == 0.0
    assert "closed" in result["blocked_reason"].lower()
    assert account.submitted == []
    assert account.refresh_calls == []


def test_a_blocked_run_does_not_reconcile_and_so_touches_no_earlier_run(activity):
    """The gate is the FIRST statement, above reconcile_unconsumed_runs. A blocked
    attempt must not spend an EARLIER run's income either."""
    account = FakeAccount(account_id=60)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=60, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    first = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                       available_buying_power=10_000.0),
                               {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)
    assert [r.id for r in svc.get_unconsumed_runs(60)] == [first["run_id"]]

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    account.market_hours = _closed_hours()
    blocked = svc.run_allocation(account, AllocationPlan(rows=[],
                                                         available_buying_power=1.0),
                                 {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    assert blocked["blocked"] is True
    assert [r.id for r in svc.get_unconsumed_runs(60)] == [first["run_id"]]
    assert svc.get_open_income_total(60) == pytest.approx(6_000.0)


def test_an_earlier_deferred_run_is_reconciled_before_the_next_run_spends(activity):
    """Step 1 of run_allocation. Without it this run's budget is computed against
    income the previous run has already deployed, and spends it twice."""
    account = FakeAccount(account_id=62_1)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=62_1, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    first = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                       available_buying_power=10_000.0),
                               {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)
    assert svc.get_open_income_total(62_1) == pytest.approx(6_000.0)

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    svc.run_allocation(account, AllocationPlan(rows=[], available_buying_power=10_000.0),
                       {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    from ba2_trade_platform.core.portfolio_allocation_store import get_recent_runs
    assert get_recent_runs(62_1)[-1].id == first["run_id"]
    assert svc.get_open_income_total(62_1) == pytest.approx(4_400.0)


def test_a_failed_refresh_forces_unsettled_even_when_the_rows_already_look_final():
    """The dangerous shape: an EARLIER refresh left the rows FILLED, today's refresh
    failed, and the row therefore looks like proof. It is not -- the broker was
    never asked, so the fill figure is recorded for the audit but must not be
    allowed to stamp the ledger."""
    account = FakeAccount(account_id=52_1)
    order_id = add_instance(TradingOrder(
        account_id=52_1, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=10.0, open_price=160.0))
    account.refresh_raises = RuntimeError("connection reset")

    totals, refreshed = svc.measure_run_fills(account, [order_id])

    assert totals.buy_value == pytest.approx(1600.0)
    assert totals.settled is False
    assert refreshed is False


def test_a_successful_refresh_lets_a_settled_row_settle():
    """The other half of the pair, so the forcing above cannot just be hardcoded."""
    account = FakeAccount(account_id=52_2)
    order_id = add_instance(TradingOrder(
        account_id=52_2, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=10.0, open_price=160.0))

    totals, refreshed = svc.measure_run_fills(account, [order_id])

    assert totals.buy_value == pytest.approx(1600.0)
    assert totals.settled is True
    assert refreshed is True


def test_the_reconcile_runs_after_the_market_gate_not_before_it(monkeypatch):
    """Ordering, pinned directly: a blocked attempt must touch nothing at all, and
    a reconcile above the gate would still refresh orders and spend the ledger."""
    order = []
    monkeypatch.setattr(svc, "reconcile_unconsumed_runs",
                        lambda account: order.append("reconcile") or [])
    real_gate = svc._market_blocked_reason
    monkeypatch.setattr(svc, "_market_blocked_reason",
                        lambda hours: order.append("gate") or real_gate(hours))

    account = FakeAccount(account_id=53_1)
    account.positions = []
    account.market_hours = _closed_hours()

    svc.run_allocation(account, AllocationPlan(rows=[]), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE)

    assert order == ["gate"]


# ---------------------------------------------------------------------------
# The deferral is drained and reported, not just logged (decision D3)
# ---------------------------------------------------------------------------

def test_describe_unconsumed_runs_counts_the_runs_and_their_working_orders(activity):
    account = FakeAccount(account_id=70)
    account.positions = []
    account.fills = {}   # nothing settles -> the run defers
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    result = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                        available_buying_power=10_000.0),
                                {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    described = svc.describe_unconsumed_runs(70)

    assert described["run_ids"] == [result["run_id"]]
    assert described["working_order_ids"] == result["working_order_ids"]


def test_describe_unconsumed_runs_is_empty_for_a_clean_account():
    assert svc.describe_unconsumed_runs(71) == {"run_ids": [], "working_order_ids": []}


def test_describe_unconsumed_runs_never_calls_the_broker(activity):
    """It renders a panel. A DB read is fine there; a broker round trip is not."""
    account = FakeAccount(account_id=72)
    account.positions = []
    account.fills = {}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    svc.run_allocation(account, AllocationPlan(rows=[row], available_buying_power=10_000.0),
                       {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)
    account.refresh_calls.clear()

    svc.describe_unconsumed_runs(72)

    assert account.refresh_calls == []


def test_syncing_income_also_drains_the_deferred_runs(activity):
    """The income panel's Refresh (and the page's load call) is the hook. Without
    it a quarterly rebalancer's deferred income stays open for a quarter."""
    account = FakeAccount(account_id=73)
    account.positions = []
    account.fills = {}
    add_instance(PortfolioIncomeEvent(account_id=73, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    first = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                       available_buying_power=10_000.0),
                               {}, make_base(), mode=ALLOCATION_MODE_REBALANCE)
    assert [r.id for r in svc.get_unconsumed_runs(73)] == [first["run_id"]]

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    svc.sync_income_events(account)

    assert svc.get_unconsumed_runs(73) == []
    assert svc.get_open_income_total(73) == pytest.approx(4_400.0)


def test_a_reconcile_failure_never_stops_the_income_sync(activity, monkeypatch):
    """The sync is what refills the ledger display. A broken drain must degrade to
    "income not consumed yet", not to an empty income panel."""
    errors = _capture_errors(monkeypatch)
    monkeypatch.setattr(svc, "reconcile_unconsumed_runs",
                        lambda account: (_ for _ in ()).throw(RuntimeError("drain broke")))
    account = FakeAccount(account_id=74)
    account.cash_transfers = [CashTransfer(
        external_id="dep-9", event_date=date(2026, 8, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=1_000.0)]

    assert svc.sync_income_events(account) == 1
    assert svc.get_open_income_total(74) == pytest.approx(1_000.0)
    assert any("drain broke" in e for e in errors), errors


# ---------------------------------------------------------------------------
# I1: an order id must be DURABLE before the order can possibly fill.
#
# record_allocation_run wrote no order ids and _finalise_run was the only writer,
# so between them -- across the whole submission loop, the slow and failure-prone
# part -- the run row said "this run created no orders". Anything that killed the
# process or raised in there (an OperationalError out of collect_order_fills, a
# restart) left a run whose orders REALLY REACHED THE BROKER with order_ids=[].
# The recovery drain then measured it as "filled nothing", stamped it, and dropped
# it out of get_unconsumed_runs() forever. Real money, no ledger.
# ---------------------------------------------------------------------------

def _one_buy_plan(symbol="AAPL", quantity=10.0, price=160.0):
    row = make_row(symbol, OrderDirection.BUY, quantity, quantity * price,
                   quantity * price, price=price)
    row.target_quantity = quantity
    return AllocationPlan(rows=[row], available_buying_power=10_000.0)


def _run_order_ids(run_id: int):
    return list(get_instance(PortfolioAllocationRun, run_id).order_ids or [])


def test_a_new_orders_id_is_on_the_run_row_before_it_is_sent_to_the_broker(activity):
    """The durability point: BEFORE submit_order, not after the loop.

    _submit_new_order persists the TradingOrder and only then hands it to the
    broker, so the row id exists while the order is still incapable of filling.
    That instant -- and no later one -- is when it has to reach the run row.
    """
    account = FakeAccount(account_id=101)
    account.positions = []
    seen = {}

    original_submit = account.submit_order

    def _spy(trading_order, **kwargs):
        # What the run row said at the moment the order went to the broker.
        run = the_run()
        seen['ids_at_submit'] = list(run.order_ids or [])
        seen['order_id'] = trading_order.id
        return original_submit(trading_order, **kwargs)

    account.submit_order = _spy
    svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert seen['order_id'] in seen['ids_at_submit'], (
        f"order {seen['order_id']} was sent to the broker while the run row listed "
        f"{seen['ids_at_submit']} - a crash here loses it forever")


def test_a_close_order_reaches_the_run_row_as_soon_as_the_broker_names_it(activity):
    """A close's order id is minted inside the adapter, so the earliest durable
    point is the instant close_transaction hands it back -- which must be before
    the NEXT row is attempted, not after the whole loop."""
    account = FakeAccount(account_id=102)
    txn_a = make_open_transaction(102, "AAPL", 10.0)
    txn_b = make_open_transaction(102, "MSFT", 4.0)
    account.positions = [FakePosition("AAPL", 10.0, 1000.0, 1200.0),
                         FakePosition("MSFT", 4.0, 1200.0, 1600.0)]
    seen = []
    original_close = account.close_transaction

    def _spy(transaction_id):
        seen.append((transaction_id, list(the_run().order_ids or [])))
        return original_close(transaction_id)

    account.close_transaction = _spy
    rows = [make_row("AAPL", OrderDirection.SELL, -10.0, -1200.0, 0.0, price=120.0),
            make_row("MSFT", OrderDirection.SELL, -4.0, -1600.0, 0.0, price=400.0)]
    for row in rows:
        row.target_quantity = 0.0
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, cost_basis=1000.0,
                                     price=120.0, transaction_ids=[txn_a]),
               "MSFT": PositionState(symbol="MSFT", quantity=4.0, cost_basis=1200.0,
                                     price=400.0, transaction_ids=[txn_b])}
    result = svc.run_allocation(account, AllocationPlan(rows=rows,
                                                        available_buying_power=10_000.0),
                                current, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    # By the time the SECOND close was attempted, the FIRST close's order was
    # already recorded against the run.
    assert len(seen) == 2
    assert seen[1][1], "the first close's order id had not been persisted yet"
    assert set(seen[1][1]) <= set(result["order_ids"])


def test_a_run_that_dies_between_submitting_and_measuring_keeps_its_order_ids(
        activity, monkeypatch):
    """The exact incident: measurement explodes AFTER the orders are at the broker.

    Nothing rolls back a broker order, so the run row is the only record that they
    exist. If it says [] the drain prices the run at zero, stamps it, and the money
    is invisible for good.
    """
    account = FakeAccount(account_id=103)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    add_instance(PortfolioIncomeEvent(account_id=103, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    monkeypatch.setattr(svc, "collect_order_fills",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("database is locked")))

    with pytest.raises(RuntimeError):
        svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                           mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.income_consumed_at is None       # still recoverable
    assert run.order_ids, "the run forgot the order it really sent to the broker"
    assert [r.symbol for r in account.get_positions() or []] == []
    submitted_ids = [o.id for o in _account_orders(103)]
    assert sorted(run.order_ids) == sorted(submitted_ids)

    # ...and the drain now prices it correctly instead of stamping a zero.
    monkeypatch.undo()
    assert svc.reconcile_unconsumed_runs(account) == [run.id]
    assert svc.get_open_income_total(103) == pytest.approx(6_000.0 - 1600.0)


def _account_orders(account_id: int):
    with get_db() as session:
        rows = list(session.exec(select(TradingOrder).where(
            TradingOrder.account_id == account_id)).all())
        session.expunge_all()
        return rows


def test_the_backstop_swallowing_a_rows_ids_does_not_erase_them_from_the_run(
        activity, monkeypatch):
    """``_submit_row``'s backstop exists for the unforeseen -- and it returns an
    outcome with an EMPTY id list.

    ``finalise_allocation_run`` restates order_ids WHOLESALE, so a final list built
    from the outcomes alone would delete from the run row an order that had already
    been persisted and sent. The run's ids are therefore seeded from what was
    RECORDED during submission, not from what the outcomes survived to report.
    """
    _capture_errors(monkeypatch)
    account = FakeAccount(account_id=109)
    txn_id = make_open_transaction(109, "AAPL", 10.0)
    account.positions = [FakePosition("AAPL", 10.0, 1000.0, 1200.0)]
    real_close_symbol = svc._close_symbol

    def _close_then_die(*args, **kwargs):
        real_close_symbol(*args, **kwargs)          # the order really goes out
        raise RuntimeError("something nobody foresaw, which is what a backstop is for")

    monkeypatch.setattr(svc, "_close_symbol", _close_then_die)
    row = make_row("AAPL", OrderDirection.SELL, -10.0, -1200.0, 0.0, price=120.0)
    row.target_quantity = 0.0
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, cost_basis=1000.0,
                                     price=120.0, transaction_ids=[txn_id])}

    result = svc.run_allocation(account, AllocationPlan(rows=[row],
                                                        available_buying_power=10_000.0),
                                current, make_base(), mode=ALLOCATION_MODE_REBALANCE)

    assert result["outcomes"][0].order_ids == []     # the backstop lost them
    assert result["order_ids"], "the close order the run really placed was dropped"
    assert _run_order_ids(result["run_id"]) == result["order_ids"]


def test_a_durability_write_that_fails_neither_breaks_nor_silences_the_run(
        activity, monkeypatch):
    """The per-order write is bookkeeping, not the money path.

    Raising out of it would abort a submission (or, after a close, lose a whole
    row's outcome) over a JSON column. So it is swallowed -- but LOUDLY, and the
    id is kept in memory first, so the finalise call still puts it on the row.
    """
    errors = _capture_errors(monkeypatch)
    account = FakeAccount(account_id=108)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    import ba2_common.core.portfolio_allocation_store as store_module
    monkeypatch.setattr(store_module, "append_run_order_ids",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("database is locked")))

    result = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert [o.status for o in result["outcomes"]] == [svc.OUTCOME_SUBMITTED]
    assert result["order_ids"]
    assert _run_order_ids(result["run_id"]) == result["order_ids"]
    assert any("could not record order" in e.lower() for e in errors), errors


def test_a_run_that_raised_after_creating_orders_is_not_stamped_as_having_taken_nothing(
        activity, monkeypatch):
    """submit_plan's backstop used to stamp FilledTotals() -- 'this run took
    nothing' -- on ANY raise out of it. Once an order id has been recorded that is
    a lie, and it is a permanent one: the stamp is one-shot."""
    account = FakeAccount(account_id=104)
    account.positions = []
    errors = _capture_errors(monkeypatch)
    plan = _one_buy_plan()

    real_submit_row = svc._submit_row
    calls = []

    def _explode_after_the_first(*args, **kwargs):
        calls.append(1)
        outcome = real_submit_row(*args, **kwargs)
        raise RuntimeError("the loop died holding a live order")

    monkeypatch.setattr(svc, "_submit_row", _explode_after_the_first)
    with pytest.raises(RuntimeError):
        svc.run_allocation(account, plan, {}, make_base(),
                           mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    run = the_run()
    assert run.income_consumed_at is None
    assert run.order_ids, "the order that reached the broker was forgotten"
    assert any("leaving it UNCONSUMED" in e or "recoverable" in e.lower()
               for e in errors), errors


# ---------------------------------------------------------------------------
# I2: two mutations that turn a recoverable run into a permanent zero-stamp.
# ---------------------------------------------------------------------------

def test_a_reconcile_pass_whose_refresh_failed_prices_nothing_at_all(
        activity, monkeypatch):
    """Kills the mutation that deletes reconcile's `if not
    refresh_orders_from_broker(...): return []` guard.

    The pre-existing test only passed because ITS run was unmeasurable anyway.
    Here the DB rows already look FINAL -- a FILLED market order -- so a pass that
    skipped the guard would happily consume the ledger against numbers the broker
    was never asked about. Stale rows and true ones are indistinguishable from
    inside; the only defence is refusing to price a pass whose refresh failed.
    """
    account = FakeAccount(account_id=106)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.ACCEPTED, 0.0, None)}   # still working
    add_instance(PortfolioIncomeEvent(account_id=106, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    first = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [r.id for r in svc.get_unconsumed_runs(106)] == [first["run_id"]]

    # The broker later filled it and our row was updated out of band, so the rows
    # LOOK final -- but this pass's refresh fails, so we have not confirmed a thing.
    for order in _account_orders(106):
        account._write_order(order.id, status=OrderStatus.FILLED, filled_qty=10.0,
                             open_price=160.0)
    _capture_errors(monkeypatch)
    account.refresh_raises = RuntimeError("broker 503")

    assert svc.reconcile_unconsumed_runs(account) == []
    assert svc.get_open_income_total(106) == pytest.approx(6_000.0)
    assert [r.id for r in svc.get_unconsumed_runs(106)] == [first["run_id"]]
    assert get_instance(PortfolioAllocationRun,
                        first["run_id"]).income_consumed_at is None


def test_a_reconcile_pass_that_defers_a_run_leaves_its_order_ids_intact(activity):
    """Kills the mutation that passes `order_ids=[]` at reconcile's finalise call.

    finalise_allocation_run RESTATES order_ids wholesale, so a pass that hands it
    an empty list ERASES the run's only record of what it sent -- and the very next
    pass then measures no orders, calls that settled, consumes nothing and stamps
    the run. Two passes is the shape of the bug, so two passes is the test.
    """
    account = FakeAccount(account_id=107)
    account.positions = []
    account.fills = {}                       # nothing reported: still working
    add_instance(PortfolioIncomeEvent(account_id=107, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    first = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    submitted = _run_order_ids(first["run_id"])
    assert submitted

    # Pass 1: still working, so nothing is consumed -- and nothing is forgotten.
    assert svc.reconcile_unconsumed_runs(account) == []
    assert _run_order_ids(first["run_id"]) == submitted

    # Pass 2: it filled. The run can only be priced from the ids pass 1 preserved.
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    assert svc.reconcile_unconsumed_runs(account) == [first["run_id"]]
    assert svc.get_open_income_total(107) == pytest.approx(6_000.0 - 1600.0)


# ---------------------------------------------------------------------------
# MINOR: sync_income_events drained BEFORE it upserted the window, so a
# backdated event discovered by that very refresh could not fund the run it was
# meant to -- and the drain stamped the run first, permanently.
# ---------------------------------------------------------------------------

def test_a_backdated_deposit_found_by_this_refresh_funds_the_run_it_should(
        activity, frozen_today):
    """The dividend/deposit and the fill turn up in the same Refresh.

    Draining first meant the run was measured against an EMPTY ledger, consumed
    nothing and was stamped -- and the stamp is one-shot, so the deposit that
    funded it stayed 100% unallocated for good while the position sat in the book.
    """
    account = FakeAccount(account_id=110)
    account.positions = []
    account.fills = {}                       # the buy is still working when it runs
    first = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [r.id for r in svc.get_unconsumed_runs(110)] == [first["run_id"]]
    assert svc.get_open_income_total(110) == pytest.approx(0.0)

    # One Refresh later: the broker reports the (backdated) deposit AND the fill.
    account.cash_transfers = [CashTransfer(
        external_id="dep-backdated", event_date=date(2026, 8, 2),
        event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0)]
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}

    assert svc.sync_income_events(account) == 1

    assert svc.get_unconsumed_runs(110) == []
    assert svc.get_open_income_total(110) == pytest.approx(6_000.0 - 1600.0)


def test_the_drain_still_runs_when_the_brokers_income_call_fails(activity,
                                                                 frozen_today,
                                                                 monkeypatch):
    """Moving the drain after the upsert must not park it behind the early return
    that a failed get_cash_transfers takes: the drain is DB-only and is the one
    thing that can still make progress while the broker's activity feed is down."""
    _capture_errors(monkeypatch)
    account = FakeAccount(account_id=111)
    account.positions = []
    account.fills = {}
    add_instance(PortfolioIncomeEvent(account_id=111, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=6_000.0))
    first = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [r.id for r in svc.get_unconsumed_runs(111)] == [first["run_id"]]

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    account.get_cash_transfers = lambda **kw: (_ for _ in ()).throw(
        RuntimeError("activity endpoint 503"))

    assert svc.sync_income_events(account) == 0

    assert svc.get_unconsumed_runs(111) == []
    assert svc.get_open_income_total(111) == pytest.approx(6_000.0 - 1600.0)


# ---------------------------------------------------------------------------
# MINOR: "0 order(s) still working" is what a FAILED broker refresh looked like.
# ---------------------------------------------------------------------------

def test_a_run_whose_refresh_failed_says_so_in_its_result(activity, monkeypatch):
    """The dangerous shape: our rows already look FILLED, so nothing is "working",
    and the refresh that would have confirmed them failed.

    ``measure_run_fills`` forces ``settled=False`` -- our rows are not evidence --
    but ``working_order_ids`` is EMPTY, so a caller reading only those two told the
    user "0 order(s) still working": a run with nothing outstanding, rather than
    one nobody has been able to price.
    """
    _capture_errors(monkeypatch)
    account = FakeAccount(account_id=112)
    account.positions = []
    # The adapter marks the row FILLED as it submits, the way a market order that
    # crossed immediately leaves it; the confirming refresh then dies.
    original_submit = account.submit_order

    def _submit_and_mark_filled(trading_order, **kwargs):
        result = original_submit(trading_order, **kwargs)
        account._write_order(trading_order.id, status=OrderStatus.FILLED,
                             filled_qty=trading_order.quantity, open_price=160.0)
        return result

    account.submit_order = _submit_and_mark_filled
    account.refresh_raises = RuntimeError("broker 503")

    result = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["settled"] is False
    assert result["working_order_ids"] == []
    assert result["refresh_failed"] is True
    assert result["income_consumed"] == 0.0

    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        working_orders_notice,
    )
    text, severity = working_orders_notice(
        settled=result["settled"], working_order_ids=result["working_order_ids"],
        refresh_failed=result["refresh_failed"])
    assert "0 order(s)" not in text
    assert "FAILED" in text and severity == "negative"


def test_a_run_whose_refresh_worked_does_not_claim_it_failed(activity):
    account = FakeAccount(account_id=113)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}

    result = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["settled"] is True
    assert result["refresh_failed"] is False


def test_a_blocked_run_reports_refresh_failed_too():
    """Key parity: the caller reads one dict either way, and a missing key is a
    KeyError in the submit handler."""
    account = FakeAccount(account_id=114)
    account.market_hours = _closed_hours()

    result = svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    assert result["blocked"] is True
    assert result["refresh_failed"] is False      # nothing was asked, nothing failed


def test_measure_run_fills_reports_whether_the_refresh_actually_happened():
    account = FakeAccount(account_id=115)
    order_id = add_instance(TradingOrder(
        account_id=115, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, good_for='day', status=OrderStatus.FILLED,
        open_type=OrderOpenType.MANUAL, filled_qty=10.0, open_price=160.0))

    totals, refreshed = svc.measure_run_fills(account, [order_id])
    assert refreshed is True and totals.settled is True

    account.refresh_raises = RuntimeError("broker 503")
    totals, refreshed = svc.measure_run_fills(account, [order_id])
    assert refreshed is False and totals.settled is False


# ---------------------------------------------------------------------------
# MINOR: get_unconsumed_runs(limit=20) silently capped the drain AND the panel.
# ---------------------------------------------------------------------------

def _defer_runs(account, count: int):
    """``count`` runs, each left unconsumed with one order still working."""
    account.fills = {}
    return [svc.run_allocation(account, _one_buy_plan(), {}, make_base(),
                               mode=ALLOCATION_MODE_REBALANCE)["run_id"]
            for _ in range(count)]


def test_one_reconcile_pass_drains_more_than_twenty_deferred_runs(activity):
    """25 deferred runs used to leave 5 behind on every pass, forever -- and the
    income that funded them showed as unallocated the whole time."""
    account = FakeAccount(account_id=116)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=116, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=60_000.0))
    run_ids = _defer_runs(account, 25)
    assert len(svc.get_unconsumed_runs(116, limit=None)) == 25

    account.fills = {"AAPL": (OrderStatus.FILLED, 10.0, 160.0)}
    consumed = svc.reconcile_unconsumed_runs(account)

    assert sorted(consumed) == sorted(run_ids)
    assert svc.get_unconsumed_runs(116, limit=None) == []


def test_the_panel_counts_every_deferred_run_not_just_the_first_twenty(activity):
    account = FakeAccount(account_id=117)
    account.positions = []
    run_ids = _defer_runs(account, 25)

    described = svc.describe_unconsumed_runs(117)

    assert sorted(described["run_ids"]) == sorted(run_ids)
    assert len(described["working_order_ids"]) == 25


def test_get_unconsumed_runs_still_honours_an_explicit_limit(activity):
    """The cap is still available -- it is just no longer the DEFAULT that the
    money paths silently inherit."""
    account = FakeAccount(account_id=118)
    account.positions = []
    _defer_runs(account, 25)

    assert len(svc.get_unconsumed_runs(118, limit=5)) == 5
    assert len(svc.get_unconsumed_runs(118, limit=None)) == 25


# ---------------------------------------------------------------------------
# W1: market valuation + a HELD symbol with no quote = a refused run.
#
# The wizard disables Submit as a courtesy; THIS is the enforcement, exactly as
# it is for market hours. The dialog can sit open across a quote outage, and the
# base it submits against is the one the LAST solve produced.
# ---------------------------------------------------------------------------

def _open_account(account_id):
    """A FakeAccount with no positions; its market hours default to OPEN."""
    account = FakeAccount(account_id=account_id)
    account.positions = []
    return account


def _base_with_an_unpriced_holding(symbols=("DARK",)):
    return BaseSnapshot(
        available_buying_power=5_000.0, managed_value=0.0, base_notional=5_000.0,
        default_bp_factor=1.0, valuation_mode=VALUATION_MODE_MARKET, cash=5_000.0,
        unpriced_held_symbols=list(symbols))


def test_run_allocation_refuses_a_base_that_could_not_price_a_held_symbol():
    account = _open_account(8_101)
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1_600.0, 1_600.0, price=160.0)
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, _base_with_an_unpriced_holding(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None
    assert result["outcomes"] == []
    assert account.submitted == []
    assert "DARK" in result["blocked_reason"]


def test_a_run_refused_for_an_unpriced_holding_writes_no_run_row():
    """Same contract as the market-hours refusal: no run row means no stamped order
    comments and, above all, no income consumed -- finalise_allocation_run is
    one-shot, so a run created for orders that were never sent would mark that
    income spent forever."""
    account = _open_account(8_102)
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)

    svc.run_allocation(account, plan, {}, _base_with_an_unpriced_holding(),
                       mode=ALLOCATION_MODE_REBALANCE)

    with get_db() as session:
        assert session.exec(select(PortfolioAllocationRun)).all() == []


def test_a_run_refused_for_an_unpriced_holding_returns_every_key_an_allowed_one_does():
    refused = svc.run_allocation(
        _open_account(8_103), AllocationPlan(rows=[]), {},
        _base_with_an_unpriced_holding(), mode=ALLOCATION_MODE_REBALANCE)
    allowed = svc.run_allocation(
        _open_account(8_104), AllocationPlan(rows=[]), {}, make_base(),
        mode=ALLOCATION_MODE_REBALANCE)

    assert refused["blocked"] is True
    assert set(refused) == set(allowed)
    assert refused["filled_buy_value"] == 0.0
    assert refused["filled_sell_value"] == 0.0
    assert refused["settled"] is True
    assert refused["working_order_ids"] == []
    assert refused["refresh_failed"] is False
    assert allowed["blocked"] is False


def test_a_cost_mode_base_is_never_refused_for_an_unpriced_holding():
    """``held_symbols_without_price`` returns [] in cost mode, so the list on the base
    is empty and nothing here can fire. Pinned because the guard is the live half of
    the valuation flip and must not leak into the escape hatch from it."""
    account = _open_account(8_105)
    base = BaseSnapshot(available_buying_power=5_000.0, managed_value=5_000.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_COST, cash=5_000.0)

    result = svc.run_allocation(account, AllocationPlan(rows=[]), {}, base,
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is False


def test_the_unpriced_holding_refusal_is_checked_before_the_market_gate():
    """Both refuse; the one the user can ACT on is the one they are told about. A
    closed market is something you wait out, a failed quote is something you retry."""
    account = FakeAccount(account_id=8_106)
    account.positions = []
    account.market_hours = _closed_hours()

    result = svc.run_allocation(account, AllocationPlan(rows=[]), {},
                                _base_with_an_unpriced_holding(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert "DARK" in result["blocked_reason"]


# ---------------------------------------------------------------------------
# BUG FIX 2026-09-04: decision 3 ("label targets must total 100%; Submit is
# blocked otherwise") had no enforcement anywhere before this. compute_allocation
# deliberately does not renormalise a bad target set, and nothing called
# validate_label_targets before Submit -- so a REBALANCE plan whose label or
# symbol percentages did not total 100% solved and SENT anyway, over- or
# under-deploying the account with no warning. run_allocation is the real
# enforcement (the wizard's own check is only the polite half); these pin it
# server-side, independent of any UI.
# ---------------------------------------------------------------------------

from ba2_trade_platform.core.portfolio_allocation import (  # noqa: E402
    ALLOCATION_BASIS_BUDGET, LabelTarget, OrderDirection, SymbolTarget,
)


def _rebalance_plan(labels, *, buying_power=10_000.0):
    row = make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0, price=160.0)
    return AllocationPlan(rows=[row], available_buying_power=buying_power,
                          labels=labels)


def test_run_allocation_refuses_a_rebalance_whose_label_targets_overshoot_100():
    account = FakeAccount(account_id=201)
    account.positions = []
    labels = [LabelTarget("ARK26", 70.0, [SymbolTarget("AAPL", 100.0)]),
             LabelTarget("NASDAQ30", 70.0, [SymbolTarget("MSFT", 100.0)])]

    result = svc.run_allocation(account, _rebalance_plan(labels), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None
    assert "100%" in result["blocked_reason"]
    assert account.submitted == []               # NOTHING reached the broker


def test_run_allocation_refuses_a_rebalance_whose_label_targets_undershoot_100():
    account = FakeAccount(account_id=202)
    account.positions = []
    labels = [LabelTarget("ARK26", 40.0, [SymbolTarget("AAPL", 100.0)])]

    result = svc.run_allocation(account, _rebalance_plan(labels), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None


def test_run_allocation_refuses_a_label_whose_symbol_weights_do_not_total_100():
    """Even when the LABEL total is exactly 100 -- the per-label symbol split can
    still be wrong on its own, and it must be caught too."""
    account = FakeAccount(account_id=203)
    account.positions = []
    labels = [LabelTarget("ARK26", 100.0, [SymbolTarget("AAPL", 60.0),
                                           SymbolTarget("MSFT", 60.0)])]

    result = svc.run_allocation(account, _rebalance_plan(labels), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is True
    assert result["run_id"] is None


def test_run_allocation_allows_a_rebalance_whose_targets_total_exactly_100(activity):
    account = FakeAccount(account_id=204)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 1.0, 160.0)}
    labels = [LabelTarget("ARK26", 100.0, [SymbolTarget("AAPL", 100.0)])]

    result = svc.run_allocation(account, _rebalance_plan(labels), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is False
    assert result["run_id"] is not None


def test_the_target_gate_never_reconciles_or_writes_a_run_row_when_it_refuses():
    """Same contract as the base/market gates: a blocked attempt must write
    NOTHING, not even a run row, so the reconcile step never sees it."""
    account = FakeAccount(account_id=205)
    account.positions = []
    labels = [LabelTarget("ARK26", 40.0, [SymbolTarget("AAPL", 100.0)])]

    svc.run_allocation(account, _rebalance_plan(labels), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE)

    assert svc.get_unconsumed_runs(account.id) == []


def test_the_target_gate_is_skipped_for_an_invest_label_run(activity):
    """Decision 3's 100% rule is a REBALANCE rule about dividing the whole
    investable pool. An INVEST_LABEL run spends an explicit amount on ONE label
    and has its own gate (invest_validation_messages, checked before the dry run
    even opens) -- this must not double-refuse it."""
    account = FakeAccount(account_id=206)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 1.0, 100.0)}
    row = make_row("AAPL", OrderDirection.BUY, 1.0, 100.0, 100.0, price=100.0)
    # A single-label INVEST_LABEL plan's own target_pct is not meaningful the way
    # a REBALANCE's is (compute_label_investment ignores it), so an arbitrary
    # non-100 value here must not block the run.
    labels = [LabelTarget("ARK26", 40.0, [SymbolTarget("AAPL", 100.0)])]
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0,
                          labels=labels, allocation_basis=ALLOCATION_BASIS_BUDGET)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_INVEST_LABEL, scope_label="ARK26")

    assert result["blocked"] is False


def test_the_target_gate_is_skipped_when_the_plan_carries_no_labels_at_all(activity):
    """A plan with an EMPTY label list is not "0% of 100%" -- the page already
    refuses to open a dry run with zero managed labels, so this is a different,
    already-handled situation and must not be misreported as a bad total."""
    account = FakeAccount(account_id=207)
    account.positions = []
    account.fills = {"AAPL": (OrderStatus.FILLED, 1.0, 160.0)}
    plan = AllocationPlan(rows=[make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0,
                                         price=160.0)],
                          available_buying_power=10_000.0)  # labels defaults to []

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is False


# ---------------------------------------------------------------------------
# BUG FIX 2026-09-04: WASHTRADE_LOCKED is not settled. run_allocation must NOT
# consume income for a run whose only order is locked -- the order is still
# armed and TradeManager may resubmit and fill it hours later. Consuming at 0
# now would mean nothing ever charges the ledger for that later fill, and the
# next rebalance would deploy the same income again.
# ---------------------------------------------------------------------------

def test_run_allocation_does_not_consume_income_for_a_washtrade_locked_run(activity):
    account = FakeAccount(account_id=210)
    account.positions = []
    account.washtrade_symbols = {"AAPL"}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE)

    assert result["blocked"] is False
    assert result["settled"] is False
    assert result["income_consumed"] == 0.0
    assert result["run_id"] in [r.id for r in svc.get_unconsumed_runs(account.id)]


def test_leg_status_combinations():
    """Direct coverage of the four-way split, including the new ``locked`` axis."""
    assert svc._leg_status(10.0, 0, 0) == svc.OUTCOME_SUBMITTED
    assert svc._leg_status(10.0, 1, 0) == svc.OUTCOME_PARTIAL
    assert svc._leg_status(0.0, 1, 0) == svc.OUTCOME_FAILED
    # Every non-succeeding leg was a wash-trade lock, nothing genuinely failed.
    assert svc._leg_status(0.0, 0, 2) == svc.OUTCOME_WASHTRADE_LOCKED
    # A lock alongside a real success is still "some of it happened".
    assert svc._leg_status(10.0, 0, 1) == svc.OUTCOME_PARTIAL
    # A lock alongside a REAL failure must not read as "just pending" -- the
    # dead leg needs the user's attention.
    assert svc._leg_status(0.0, 1, 1) == svc.OUTCOME_FAILED
    assert svc._leg_status(10.0, 1, 1) == svc.OUTCOME_PARTIAL
