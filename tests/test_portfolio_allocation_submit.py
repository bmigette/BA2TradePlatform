"""Live-side Portfolio Allocation service tests.

Uses tests/conftest.py's in-memory SQLite (autouse `patch_db_engine`) and a
duck-typed FakeAccount -- no broker, no NiceGUI.
"""
import pytest

from ba2_trade_platform.core.account_types import MarginInfo, OrderImpact
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, PositionFetchFailed, PositionState,
)
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
        self.cash_transfers = []     # list[CashTransfer]
        self.submitted = []          # [(symbol, side, quantity, comment)]
        self.previewed = []          # [(symbol, quantity, is_closing_order)]
        self.preview_raises = False
        self.margin_raises = False
        self.closed = []             # [transaction_id]
        self.reject_quantities = set()
        self.washtrade_symbols = set()

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
        return list(self.cash_transfers)

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        self.submitted.append((trading_order.symbol, trading_order.side,
                               trading_order.quantity, trading_order.comment))
        if trading_order.quantity in self.reject_quantities:
            trading_order.comment = f"{trading_order.comment or ''} | broker rejected"
            return None
        if trading_order.symbol in self.washtrade_symbols:
            trading_order.status = OrderStatus.WASHTRADE_LOCKED
            return trading_order
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        return trading_order

    def close_transaction(self, transaction_id):
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
