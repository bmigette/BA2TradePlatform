"""Live-side Portfolio Allocation service tests.

Uses tests/conftest.py's in-memory SQLite (autouse `patch_db_engine`) and a
duck-typed FakeAccount -- no broker, no NiceGUI.
"""
from datetime import date, timedelta

import pytest
from sqlmodel import select

from ba2_trade_platform.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
    CashTransfer, MarginInfo, OrderImpact,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import (
    PortfolioAllocationRun, PortfolioIncomeEvent, TradingOrder, Transaction,
)
from ba2_trade_platform.core.portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP,
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE,
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    AllocationPlan, AllocationRow, BaseSnapshot, PositionFetchFailed, PositionState,
)
from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.core import portfolio_allocation_service as svc


#: Marks "the caller never passed is_closing_order at all". A preview must state
#: its intent EXPLICITLY -- relying on the seam's False default is how a close got
#: priced as a short open (commit 1d099e8).
NOT_PASSED = "<not passed>"


class FakePosition:
    """Minimal stand-in for a broker Position row."""

    def __init__(self, symbol, qty, cost_basis, market_value):
        self.symbol = symbol
        self.qty = qty
        self.cost_basis = cost_basis
        self.market_value = market_value


class FakeAccount:
    """Duck-typed stand-in for AccountInterface. No DB lookups, no broker."""

    supports_trading = True

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
        self.reject_quantities = set()
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

    def submit_order(self, trading_order, tp_price=None, sl_price=None,
                     is_closing_order=NOT_PASSED):
        self.submitted.append((trading_order.symbol, trading_order.side,
                               trading_order.quantity, trading_order.comment))
        self.submit_closing_flags.append(is_closing_order)
        self.events.append(('submit', trading_order.symbol))
        if trading_order.quantity in self.raise_quantities:
            raise self.raise_quantities[trading_order.quantity]
        if trading_order.quantity in self.db_reject_quantities:
            # The reason lands on the DATABASE row, not on this object.
            fresh = get_instance(TradingOrder, trading_order.id)
            reason = self.db_reject_quantities[trading_order.quantity]
            fresh.comment = f"{fresh.comment} | {reason}" if fresh.comment else reason
            update_instance(fresh)
            return None
        if trading_order.quantity in self.reject_quantities:
            trading_order.comment = f"{trading_order.comment or ''} | broker rejected"
            return None
        if trading_order.symbol in self.washtrade_symbols:
            trading_order.status = OrderStatus.WASHTRADE_LOCKED
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

    def close_transaction(self, transaction_id):
        self.events.append(('close', transaction_id))
        if transaction_id in self.close_raises:
            raise self.close_raises[transaction_id]
        if transaction_id in self.close_failures:
            return {'success': False, 'message': self.close_failures[transaction_id]}
        self.closed.append(transaction_id)
        return {'success': True, 'message': 'closed', 'canceled_count': 0,
                'deleted_count': 0, 'close_order_id': 999}


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
# submit_plan -- the ADJUST branch
# ---------------------------------------------------------------------------

def test_submit_plan_trim_on_a_held_symbol_adjusts_the_transaction_fifo(monkeypatch):
    account = FakeAccount(account_id=9)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    first = make_open_transaction(9, "AAPL", 20.0)
    second = make_open_transaction(9, "AAPL", 10.0)
    calls = []

    def fake_adjust(acct, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
        calls.append((transaction.id, qty_change))
        return {'success': True, 'message': 'ok', 'orders_created': [111], 'orders_canceled': []}

    monkeypatch.setattr(TransactionHelper, 'adjust_quantity_with_tpsl', staticmethod(fake_adjust))

    row = make_row("AAPL", OrderDirection.SELL, -25.0, 2650.0, 0.0, price=106.0)
    row.target_quantity = 5.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="22", allow_fractional=False)

    assert calls == [(first, -20.0), (second, -5.0)]
    assert outcomes[0].action == ACTION_ADJUST
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED


def test_submit_plan_adjust_reports_failed_when_one_leg_of_the_split_fails(monkeypatch):
    account = FakeAccount(account_id=55)
    first = make_open_transaction(55, "AAPL", 20.0)
    second = make_open_transaction(55, "AAPL", 10.0)

    def fake_adjust(acct, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
        if transaction.id == second:
            return {'success': False, 'message': 'held_for_orders',
                    'orders_created': [], 'orders_canceled': []}
        return {'success': True, 'message': 'ok', 'orders_created': [111], 'orders_canceled': []}

    monkeypatch.setattr(TransactionHelper, 'adjust_quantity_with_tpsl', staticmethod(fake_adjust))

    row = make_row("AAPL", OrderDirection.SELL, -25.0, 2650.0, 0.0, price=106.0)
    row.target_quantity = 5.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="56", allow_fractional=False)

    # HALF the trim went through. Reporting it as SUBMITTED would tell the user
    # the position is at target when it is not.
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "held_for_orders" in outcomes[0].message
    assert outcomes[0].order_ids == [111]


def test_submit_plan_adjust_with_no_remaining_transaction_quantity_is_skipped(monkeypatch):
    """Every open transaction is already at zero: there is nothing to trim, and
    sending a zero-quantity adjustment is an order the broker cancels."""
    account = FakeAccount(account_id=57)
    empty = make_open_transaction(57, "AAPL", 0.0)
    called = []
    monkeypatch.setattr(TransactionHelper, 'adjust_quantity_with_tpsl',
                        staticmethod(lambda *a, **k: called.append(a)))

    row = make_row("AAPL", OrderDirection.SELL, -5.0, 530.0, 0.0, price=106.0)
    row.target_quantity = 5.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10.0, price=106.0,
                                     transaction_ids=[empty])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="58", allow_fractional=False)

    assert called == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED


# ---------------------------------------------------------------------------
# submit_plan -- the CLOSE branch
# ---------------------------------------------------------------------------

def test_submit_plan_close_keeps_going_after_one_transaction_refuses():
    """Three transactions in one symbol: a refusal on the first must not leave the
    other two open with the run reporting a single failure and nothing else."""
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
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert outcomes[0].transaction_ids == [b, c]
    assert "held for another order" in outcomes[0].message


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
    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "broker timeout" in outcomes[0].message


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


def test_submit_plan_falls_back_to_whole_shares_when_the_adapter_refuses_locally():
    """L1: TastyTrade refuses a fractional priced order in the adapter, BEFORE any
    round trip, by raising a ValueError naming ``fractional_market_orders_only``.
    That has to drive the same fallback as a broker-side rejection, not reach the
    user as a traceback."""
    account = FakeAccount(account_id=67)
    account.positions = []
    account.raise_quantities = {2.5: ValueError(
        "TastyTrade will not accept the fractional quantity 2.5 of NVDA ... "
        "fractional_market_orders_only")}

    outcomes = svc.submit_plan(account, _fractional_plan(), {}, run_tag="68",
                               allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5, 2.0]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].path == "whole"


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
