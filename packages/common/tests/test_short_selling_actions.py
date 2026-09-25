"""Equity short selling, Task S1: `sell` is sell and `buy` is buy, as at the broker.

Design section 1 (docs/plans/2026-09-24-equity-short-selling-design.md), one test per row:

| Order  | Position held | Result                                                                |
|--------|---------------|-----------------------------------------------------------------------|
| sell   | long          | reduces/closes the long, capped at the position, never flips          |
| sell   | flat          | opens a short only with the expert's enable_sell on, else refused     |
| buy    | short         | covers the short, never flips to long                                 |
| buy    | flat or long  | unchanged (a PENDING buy for the risk manager)                        |
| close  | short         | already buys to cover (close_transaction)                             |

plus: the enable_sell-off refusal message, a position-fetch failure refusing in every branch, the
enable_sell-OFF long path staying exactly the pre-change PENDING sell, and the direction plumbing a
short entry relies on (entry-bracket TP/SL sides, the stop policy's "tighter" for a short).

Every position read here goes through its genuine code path against the in-memory trade store: the
account is a stub only at the broker edge (get_positions / close_transaction / prices).
"""
from types import SimpleNamespace

import pytest

from ba2_common.core import instance_resolver
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeActions import (
    AdjustStopLossAction, AdjustTakeProfitAction, BuyAction, CloseAction, SellAction,
    ruleset_stop_policy, stop_is_long_position,
)
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
)

EXPERT_ID = 42
SYMBOL = "AAPL"


class _StubAccount(AccountInterface):
    """A broker edge: a configurable position book and a recording close_transaction."""

    def __init__(self, positions=(), price=100.0):
        self.id = 1
        self._settings_cache = None
        self._positions = positions
        self._price = price
        self.closed = []
        self.reduced = []
        self.position_reads = 0

    def get_positions(self):
        self.position_reads += 1
        return self._positions if self._positions is None else list(self._positions)

    def close_transaction(self, transaction_id):
        self.closed.append(transaction_id)
        return {"success": True, "message": f"closed {transaction_id}", "close_order_id": 900 + transaction_id}

    def reduce_transaction(self, transaction_id, quantity):
        self.reduced.append((transaction_id, quantity))
        return {"success": True, "message": f"reduced {transaction_id} by {quantity:g}",
                "close_order_ids": [800 + transaction_id]}

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return self._price

    def get_account_info(self): raise AssertionError("no balance is read by these actions")
    def _get_instrument_current_price_impl(self, *a, **k): return self._price
    def _submit_order_impl(self, *a, **k): raise AssertionError("nothing here may reach the broker")
    def adjust_sl(self, *a, **k): return None
    def adjust_tp(self, *a, **k): return None
    def adjust_tp_sl(self, *a, **k): return None
    def cancel_order(self, *a, **k): return None
    def get_balance(self): return 100_000.0
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


class _Expert:
    """``enable_sell`` / ``enable_buy`` as stored; unset reads as the INTERFACE default
    (enable_buy True, enable_sell False), as a real expert's get_setting_with_interface_default."""

    def __init__(self, enable_sell, enable_buy=True):
        self._settings = {"enable_buy": enable_buy}
        if enable_sell is not None:
            self._settings["enable_sell"] = enable_sell

    def get_setting_with_interface_default(self, key, log_warning=False):
        return self._settings.get(key)


class _Resolver:
    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        assert expert_id == EXPERT_ID
        return self._expert

    def get_account_instance(self, account_id): return None
    def get_account_instance_from_transaction(self, transaction): return None


@pytest.fixture
def world():
    """In-memory trade store + a resolver whose expert carries ``enable_sell``."""
    previous = instance_resolver.get_instance_resolver()
    state = SimpleNamespace()

    def configure(enable_sell, enable_buy=True):
        instance_resolver.set_instance_resolver(_Resolver(_Expert(enable_sell, enable_buy)))

    state.configure = configure
    configure(False)
    try:
        with ts.inmem_trades():
            yield state
    finally:
        instance_resolver.set_instance_resolver(previous)


def _rec(action=OrderRecommendation.SELL):
    return SimpleNamespace(instance_id=EXPERT_ID, id=7, recommended_action=action)


def _hold(side, qty, status=TransactionStatus.OPENED, asset_class=AssetClass.EQUITY, filled=None):
    """An open transaction of this expert. An OPENED one gets its FILLED entry order
    (``filled`` shares, default all of ``qty``): the netting guard measures what a close will
    actually trade (``get_current_open_qty``), not the ordered quantity."""
    txn = Transaction(symbol=SYMBOL, quantity=qty, side=side, status=status,
                      expert_id=EXPERT_ID, asset_class=asset_class)
    txn_id = add_instance(txn)
    if status == TransactionStatus.OPENED and asset_class == AssetClass.EQUITY:
        add_instance(TradingOrder(
            account_id=1, symbol=SYMBOL, quantity=qty, side=side, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=qty if filled is None else filled,
            transaction_id=txn_id))
    return txn_id


def _sell(account, action=OrderRecommendation.SELL):
    a = SellAction(SYMBOL, account, action, expert_recommendation=_rec(action))
    a.submit_to_broker = True
    return a


def _buy(account, action=OrderRecommendation.BUY):
    a = BuyAction(SYMBOL, account, action, expert_recommendation=_rec(action))
    a.submit_to_broker = True
    return a


def _pending_orders():
    return [o for o in ts.orders_where(statuses=[OrderStatus.PENDING]) if o.symbol == SYMBOL]


# --------------------------------------------------------------------------- #
# sell, flat
# --------------------------------------------------------------------------- #

class TestSellFromFlat:
    def test_enable_sell_on_stages_an_entry_sell_for_the_risk_manager(self, world):
        world.configure(True)
        result = _sell(_StubAccount([])).execute()

        assert result["success"] is True, result["message"]
        assert result["data"]["opens_short"] is True
        order = get_instance(TradingOrder, result["data"]["order_id"])
        # The same PENDING qty-0 shape the risk manager sizes for a buy entry.
        assert order.side == OrderDirection.SELL
        assert order.quantity == 0.0
        assert order.status == OrderStatus.PENDING
        assert order.transaction_id is None, "a SELL-side Transaction is opened at submit, not here"

    def test_a_zero_quantity_broker_row_is_flat_too(self, world):
        world.configure(True)
        result = _sell(_StubAccount([{"symbol": SYMBOL, "qty": 0.0}])).execute()
        assert result["success"] is True and result["data"]["opens_short"] is True

    @pytest.mark.parametrize("enable_sell", [False, None, 0, "0", "false"],
                             ids=["false", "unset", "int-0", "str-0", "str-false"])
    def test_enable_sell_off_refuses_with_the_reason(self, world, enable_sell):
        world.configure(enable_sell)
        result = _sell(_StubAccount([])).execute()

        assert result["success"] is False
        assert result["message"] == (
            "No position to sell and selling is disabled for this expert (enable_sell is off)")
        assert _pending_orders() == []

    @pytest.mark.parametrize("enable_sell", [True, 1, "1", "true"])
    def test_enable_sell_is_read_through_coerce_bool(self, world, enable_sell):
        world.configure(enable_sell)
        assert _sell(_StubAccount([])).execute()["success"] is True

    def test_a_garbled_enable_sell_is_not_guessed(self, world):
        """coerce_bool raises on a spelling it cannot mean: surfaced (the evaluator logs the
        failed action), never read as on or off."""
        world.configure("maybe")
        with pytest.raises(ValueError, match="maybe"):
            _sell(_StubAccount([])).execute()
        assert _pending_orders() == []

    @pytest.mark.parametrize("action", [OrderRecommendation.BUY, OrderRecommendation.HOLD])
    def test_a_short_entry_needs_a_bearish_recommendation(self, world, action):
        """The enter pass sizes the candidate on the RECOMMENDATION's side and the entry bracket
        reads its direction from it: a sell entry on a bullish/hold rec would be sized and
        protected as a long."""
        world.configure(True)
        result = _sell(_StubAccount([]), action=action).execute()
        assert result["success"] is False
        assert "SELL/UNDERWEIGHT" in result["message"]
        assert _pending_orders() == []

    def test_underweight_opens_a_short_too(self, world):
        world.configure(True)
        assert _sell(_StubAccount([]), action=OrderRecommendation.UNDERWEIGHT).execute()["success"]

    def test_an_expertless_sell_never_opens_a_short(self, world):
        world.configure(True)
        # A recommendation with no expert instance (a manual order): enable_sell cannot be read.
        rec = SimpleNamespace(instance_id=None, id=7, recommended_action=OrderRecommendation.SELL)
        action = SellAction(SYMBOL, _StubAccount([]), OrderRecommendation.SELL,
                            expert_recommendation=rec)
        result = action.execute()
        assert result["success"] is False
        assert "no expert" in result["message"]

    def test_the_broker_holding_another_experts_long_is_refused(self, world):
        """This expert is flat, the broker is long (another expert on the account): a sell would
        reduce that long at the broker, not open a short."""
        world.configure(True)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 30.0}])
        result = _sell(account).execute()
        assert result["success"] is False
        assert "does not own" in result["message"]
        assert _pending_orders() == [] and account.closed == []

    def test_the_broker_holding_another_experts_short_still_allows_a_short(self, world):
        """Same direction nets additively at the broker, exactly like two experts both long."""
        world.configure(True)
        result = _sell(_StubAccount([{"symbol": SYMBOL, "qty": -30.0}])).execute()
        assert result["success"] is True and result["data"]["opens_short"] is True


# --------------------------------------------------------------------------- #
# sell, long held
# --------------------------------------------------------------------------- #

class TestSellWhileLong:
    def test_enable_sell_on_closes_the_long_and_never_stages_an_entry(self, world):
        world.configure(True)
        txn_id = _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        result = _sell(account).execute()

        assert result["success"] is True, result["message"]
        # close_transaction closes exactly the transaction's quantity against the SAME
        # transaction (is_closing_order=True): capped at what is held, no new Transaction, no
        # RM sizing, no safeguard stop.
        assert account.closed == [txn_id]
        assert result["data"]["closing"] is True
        assert "order_id" not in result["data"], "the evaluator would bracket a close as an entry"
        assert _pending_orders() == []

    def test_every_long_lot_of_the_expert_is_closed(self, world):
        world.configure(True)
        a, b = _hold(OrderDirection.BUY, 4.0), _hold(OrderDirection.BUY, 6.0, TransactionStatus.WAITING)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        assert _sell(account).execute()["success"] is True
        assert sorted(account.closed) == sorted([a, b])

    def test_option_transactions_on_the_underlying_are_not_shares(self, world):
        """An option Transaction's symbol is the UNDERLYING: a short call must not read as a
        short stock position (nor be closed by an equity sell)."""
        world.configure(True)
        _hold(OrderDirection.SELL, 2.0, asset_class=AssetClass.OPTION)
        result = _sell(_StubAccount([])).execute()
        assert result["success"] is True and result["data"]["opens_short"] is True

    def test_an_unfilled_long_entry_is_cancelled_not_refused(self, world):
        """A WAITING entry has not reached the broker: close_transaction cancels it, so an empty
        broker book is no reason to refuse (and nothing is sold)."""
        world.configure(True)
        txn_id = _hold(OrderDirection.BUY, 10.0, TransactionStatus.WAITING)
        account = _StubAccount([])
        result = _sell(account).execute()
        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id] and _pending_orders() == []

    def test_a_partially_filled_entry_closes_what_filled(self, world):
        """100 ordered, 60 filled, the broker holds 60: close_transaction sells the measured 60,
        so the sell is not refused (the ordered 100 would have read as 'broker holds less')."""
        world.configure(True)
        txn_id = _hold(OrderDirection.BUY, 100.0, filled=60.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 60.0}])
        result = _sell(account).execute()
        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id]

    def test_a_zero_quantity_waiting_long_still_counts_as_long(self, world):
        """Classified by SIDE: a WAITING BUY at quantity 0 is a long entry in flight, so a sell
        closes (cancels) it -- it must not read as flat and open a short beside it."""
        world.configure(True)
        txn_id = _hold(OrderDirection.BUY, 0.0, TransactionStatus.WAITING)
        account = _StubAccount([])
        result = _sell(account).execute()
        assert result["success"] is True and result["data"]["closing"] is True
        assert account.closed == [txn_id] and _pending_orders() == []

    def test_a_broker_holding_less_than_the_expert_refuses_rather_than_flip(self, world):
        world.configure(True)
        _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 4.0}])
        result = _sell(account).execute()
        assert result["success"] is False
        assert "flip" in result["message"]
        assert account.closed == [] and _pending_orders() == []

    def test_deferred_without_automated_modification(self, world):
        world.configure(True)
        _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        action = _sell(account)
        action.submit_to_broker = False
        result = action.execute()
        assert result["success"] is True and result["data"]["status"] == "PENDING"
        assert account.closed == []

    def test_enable_sell_off_still_closes_the_long(self, world):
        """Operator 2026-09-25: closing a long is gated by enable_BUY (the permission that opened
        it). The old inert "stage a PENDING sell the RM drops" path is gone."""
        world.configure(False, enable_buy=True)
        txn_id = _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        result = _sell(account).execute()
        assert result["success"] is True and result["data"]["closing"] is True
        assert account.closed == [txn_id] and _pending_orders() == []


# --------------------------------------------------------------------------- #
# sell, short held
# --------------------------------------------------------------------------- #

class TestSellWhileShort:
    def test_a_sell_never_adds_to_an_open_short(self, world):
        world.configure(True)
        _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -10.0}])
        result = _sell(account).execute()
        assert result["success"] is False
        assert "already short" in result["message"]
        assert _pending_orders() == [] and account.closed == []

    def test_another_experts_short_with_enable_sell_off_is_the_disabled_refusal(self, world):
        """This expert is flat (the broker short belongs to someone else): the sell would OPEN
        a short, which enable_sell forbids."""
        world.configure(False)
        result = _sell(_StubAccount([{"symbol": SYMBOL, "qty": -10.0}])).execute()
        assert result["success"] is False
        assert result["message"] == SellAction.SELLING_DISABLED_MESSAGE


# --------------------------------------------------------------------------- #
# position-fetch failure
# --------------------------------------------------------------------------- #

class TestFetchFailureRefuses:
    @pytest.mark.parametrize("enable_sell", [True, False])
    def test_sell_refuses_on_an_unverified_book(self, world, enable_sell):
        world.configure(enable_sell)
        _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount(None)            # get_positions() -> None: fetch FAILED
        result = _sell(account).execute()
        assert result["success"] is False
        assert result["data"]["position_fetch_failed"] is True
        assert _pending_orders() == [] and account.closed == []

    def test_a_cover_refuses_on_an_unverified_book(self, world):
        world.configure(True)
        _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount(None)
        result = _buy(account).execute()
        assert result["success"] is False
        assert result["data"]["position_fetch_failed"] is True
        assert _pending_orders() == [] and account.closed == []


# --------------------------------------------------------------------------- #
# buy
# --------------------------------------------------------------------------- #

class TestBuy:
    @pytest.mark.parametrize("enable_buy", [True, False])
    def test_buy_while_short_covers_and_never_flips(self, world, enable_buy):
        world.configure(True, enable_buy=enable_buy)     # covering needs enable_SELL only
        txn_id = _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -10.0}])
        result = _buy(account).execute()

        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id]
        assert result["data"]["closing"] is True and "order_id" not in result["data"]
        assert _pending_orders() == [], "a pending BUY would be sized as a fresh long entry"

    def test_a_zero_quantity_waiting_short_still_counts_as_short(self, world):
        world.configure(True)
        txn_id = _hold(OrderDirection.SELL, 0.0, TransactionStatus.WAITING)
        account = _StubAccount([])
        result = _buy(account).execute()
        assert result["success"] is True and result["data"]["closing"] is True
        assert account.closed == [txn_id] and _pending_orders() == []

    def test_a_partially_filled_short_covers_what_filled(self, world):
        world.configure(True)
        txn_id = _hold(OrderDirection.SELL, 100.0, filled=60.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -60.0}])
        result = _buy(account).execute()
        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id]

    def test_an_unfilled_short_entry_is_cancelled_not_refused(self, world):
        world.configure(True)
        txn_id = _hold(OrderDirection.SELL, 10.0, TransactionStatus.WAITING)
        account = _StubAccount([])
        result = _buy(account).execute()
        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id] and _pending_orders() == []

    def test_buy_while_short_refuses_when_the_broker_is_less_short(self, world):
        world.configure(True)
        _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -3.0}])
        result = _buy(account).execute()
        assert result["success"] is False and "flip" in result["message"]
        assert account.closed == [] and _pending_orders() == []

    @pytest.mark.parametrize("held", [None, OrderDirection.BUY], ids=["flat", "long"])
    def test_buy_flat_or_long_is_unchanged(self, world, held):
        if held is not None:
            _hold(held, 10.0)
        account = _StubAccount([])
        result = _buy(account).execute()

        assert result["success"] is True
        order = get_instance(TradingOrder, result["data"]["order_id"])
        assert (order.side, order.quantity, order.status) == (
            OrderDirection.BUY, 0.0, OrderStatus.PENDING)
        # Without a short there is nothing to net: the broker book is not even read.
        assert account.position_reads == 0 and account.closed == []


# --------------------------------------------------------------------------- #
# close, short
# --------------------------------------------------------------------------- #

def test_close_on_a_short_delegates_to_close_transaction(world):
    txn_id = _hold(OrderDirection.SELL, 10.0)
    entry = SimpleNamespace(transaction_id=txn_id)
    account = _StubAccount([{"symbol": SYMBOL, "qty": -10.0}])
    action = CloseAction(SYMBOL, account, OrderRecommendation.BUY, existing_order=entry,
                         expert_recommendation=_rec(OrderRecommendation.BUY))
    action.submit_to_broker = True
    assert action.execute()["success"] is True
    assert account.closed == [txn_id]


# --------------------------------------------------------------------------- #
# the direction plumbing a short entry relies on
# --------------------------------------------------------------------------- #

class TestShortEntryBracketDirection:
    """The entry rule's `adjust_stop_loss -8` / `adjust_take_profit +x` on a SELL entry: the
    percent is direction-relative, so the stop lands ABOVE and the target BELOW."""

    def _order(self):
        return SimpleNamespace(side=OrderDirection.SELL, limit_price=None, open_price=100.0,
                               expert_recommendation_id=None)

    def test_stop_is_above_the_short_entry(self, world):
        sl = AdjustStopLossAction(SYMBOL, _StubAccount([], price=100.0), OrderRecommendation.SELL,
                                  reference_value="order_open_price", percent=-8.0)
        assert sl.compute_price(self._order()) == pytest.approx(108.0)

    def test_target_is_below_the_short_entry(self, world):
        tp = AdjustTakeProfitAction(SYMBOL, _StubAccount([], price=100.0), OrderRecommendation.SELL,
                                    reference_value="order_open_price", percent=10.0)
        assert tp.compute_price(self._order()) == pytest.approx(90.0)

    def test_a_short_transaction_reads_as_short_for_the_stop_policy(self):
        assert stop_is_long_position(SimpleNamespace(side=OrderDirection.SELL, id=1)) is False

    @pytest.mark.parametrize("requested, applied, reason", [
        (105.0, 105.0, "tighter_or_equal"),     # LOWER is tighter for a short
        (112.0, 108.0, "ratchet"),              # higher would loosen: refused by default
    ])
    def test_the_ratchet_points_down_for_a_short(self, requested, applied, reason):
        txn = SimpleNamespace(id=1, side=OrderDirection.SELL, stop_loss=108.0, meta_data=None)
        price, why = ruleset_stop_policy(txn, requested, False, lambda: None)
        assert (price, why) == (applied, reason)


# --------------------------------------------------------------------------- #
# a legacy hedge (both sides held) is refused loudly, never netted by a sum
# --------------------------------------------------------------------------- #

class TestLegacyHedge:
    """Classification is by SIDE: a long AND a short at once (only the retired hedging setting,
    now replaced by the netting rule, could make one) must not be summed into a net and acted on."""

    def _both(self):
        return _hold(OrderDirection.BUY, 10.0), _hold(OrderDirection.SELL, 4.0)

    def test_a_sell_refuses(self, world):
        world.configure(True)
        self._both()
        account = _StubAccount([{"symbol": SYMBOL, "qty": 6.0}])
        result = _sell(account).execute()
        assert result["success"] is False and "legacy hedge" in result["message"]
        assert account.closed == [] and _pending_orders() == []

    def test_a_buy_refuses(self, world):
        world.configure(True)
        self._both()
        account = _StubAccount([{"symbol": SYMBOL, "qty": 6.0}])
        result = _buy(account).execute()
        assert result["success"] is False and "legacy hedge" in result["message"]
        assert account.closed == [] and _pending_orders() == []


# --------------------------------------------------------------------------- #
# get_expert_position: equity shares only, the expert found like resolve_expert
# --------------------------------------------------------------------------- #

class TestExpertPosition:
    def test_a_covered_call_is_not_a_short_share(self, world):
        _hold(OrderDirection.BUY, 100.0)
        _hold(OrderDirection.SELL, 1.0, asset_class=AssetClass.OPTION)
        assert _buy(_StubAccount([])).get_expert_position() == 100.0

    def test_a_pure_option_position_holds_no_shares(self, world):
        _hold(OrderDirection.BUY, 2.0, asset_class=AssetClass.OPTION)
        assert _buy(_StubAccount([])).get_expert_position() == 0.0

    def test_long_only_equity_is_unchanged(self, world):
        _hold(OrderDirection.BUY, 30.0)
        _hold(OrderDirection.BUY, 5.0, TransactionStatus.WAITING)
        assert _buy(_StubAccount([])).get_expert_position() == 35.0

    def test_no_expert_is_none(self, world):
        action = SellAction(SYMBOL, _StubAccount([]), OrderRecommendation.SELL)
        assert action.get_expert_position() is None

    def test_the_existing_orders_expert_is_the_fallback(self, world):
        """No recommendation (a TP/SL-style action): the expert comes from the existing order,
        exactly as resolve_expert finds it."""
        _hold(OrderDirection.BUY, 12.0)
        order = SimpleNamespace(get_expert_id=lambda: EXPERT_ID)
        action = SellAction(SYMBOL, _StubAccount([]), OrderRecommendation.SELL, existing_order=order)
        assert action.get_expert_position() == 12.0
        assert action.resolve_expert() is not None


# --------------------------------------------------------------------------- #
# the permission matrix (operator decision 2026-09-25)
# --------------------------------------------------------------------------- #

PERMISSIONS = [(True, True), (True, False), (False, True), (False, False)]
PERM_IDS = ["buy+sell", "buy-only", "sell-only", "none"]


@pytest.mark.parametrize("enable_buy, enable_sell", PERMISSIONS, ids=PERM_IDS)
class TestPermissionMatrix:
    """Open: enable_sell opens a short, enable_buy a long. Close: gated by the permission that
    OPENED the position -- a sell reducing a long needs enable_buy, a buy covering a short needs
    enable_sell -- whatever the other one says."""

    def test_flat_sell_opens_a_short_only_with_enable_sell(self, world, enable_buy, enable_sell):
        world.configure(enable_sell, enable_buy=enable_buy)
        result = _sell(_StubAccount([])).execute()
        if enable_sell:
            assert result["success"] is True and result["data"]["opens_short"] is True
        else:
            assert result["success"] is False
            assert result["message"] == SellAction.SELLING_DISABLED_MESSAGE
            assert result["data"]["missing_setting"] == "enable_sell"
            assert _pending_orders() == []

    @pytest.mark.parametrize("percent, closed, reduced", [
        (None, True, None), (100, True, None), (50, False, 5.0)], ids=["full", "100pct", "50pct"])
    def test_long_sell_closes_only_with_enable_buy(self, world, enable_buy, enable_sell,
                                                   percent, closed, reduced):
        world.configure(enable_sell, enable_buy=enable_buy)
        txn_id = _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        action = _sell(account)
        action.close_percent = percent
        result = action.execute()
        if enable_buy:
            assert result["success"] is True, result["message"]
            assert account.closed == ([txn_id] if closed else [])
            assert account.reduced == ([] if closed else [(txn_id, reduced)])
        else:
            assert result["success"] is False
            assert "enable_buy" in result["message"]
            assert result["data"]["missing_setting"] == "enable_buy"
            assert account.closed == [] and account.reduced == []
        assert _pending_orders() == []

    def test_flat_buy_is_the_unchanged_entry(self, world, enable_buy, enable_sell):
        """The action always stages the PENDING buy; the risk manager's permission filter is
        what gates the long entry on enable_buy (unchanged)."""
        from ba2_common.core.TradeRiskManagement import TradeRiskManagement
        world.configure(enable_sell, enable_buy=enable_buy)
        result = _buy(_StubAccount([])).execute()
        assert result["success"] is True
        order = get_instance(TradingOrder, result["data"]["order_id"])
        kept = TradeRiskManagement()._filter_orders_by_permissions([order], enable_buy, enable_sell)
        assert (kept == [order]) is enable_buy

    @pytest.mark.parametrize("percent, closed, reduced", [
        (None, True, None), (100, True, None), (50, False, 5.0)], ids=["full", "100pct", "50pct"])
    def test_short_buy_covers_only_with_enable_sell(self, world, enable_buy, enable_sell,
                                                    percent, closed, reduced):
        world.configure(enable_sell, enable_buy=enable_buy)
        txn_id = _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -10.0}])
        action = _buy(account)
        action.close_percent = percent
        result = action.execute()
        if enable_sell:
            assert result["success"] is True, result["message"]
            assert account.closed == ([txn_id] if closed else [])
            assert account.reduced == ([] if closed else [(txn_id, reduced)])
        else:
            assert result["success"] is False
            assert "enable_sell" in result["message"]
            assert result["data"]["missing_setting"] == "enable_sell"
            assert account.closed == [] and account.reduced == []
        assert _pending_orders() == []


# --------------------------------------------------------------------------- #
# the close percent
# --------------------------------------------------------------------------- #

class TestClosePercent:
    def _long(self, world, *lots, broker=None):
        world.configure(True)
        ids = [_hold(OrderDirection.BUY, q) for q in lots]
        return ids, _StubAccount([{"symbol": SYMBOL, "qty": sum(lots) if broker is None else broker}])

    def test_half_of_100_sells_50(self, world):
        ids, account = self._long(world, 100.0)
        action = _sell(account)
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is True
        assert account.reduced == [(ids[0], 50.0)] and account.closed == []
        assert result["data"]["quantity"] == 50.0 and result["data"]["percent"] == 50.0

    def test_rounds_down_to_whole_shares(self, world):
        ids, account = self._long(world, 10.0)
        action = _sell(account)
        action.close_percent = 55
        action.execute()
        assert account.reduced == [(ids[0], 5.0)]

    def test_a_percent_that_rounds_to_zero_is_refused(self, world):
        ids, account = self._long(world, 10.0)
        action = _sell(account)
        action.close_percent = 1
        result = action.execute()
        assert result["success"] is False and "rounds to 0" in result["message"]
        assert account.closed == [] and account.reduced == []

    @pytest.mark.parametrize("bad", [0, -5, 100.5, 150])
    def test_a_percent_out_of_range_is_refused(self, world, bad):
        ids, account = self._long(world, 10.0)
        action = _sell(account)
        action.close_percent = bad
        result = action.execute()
        assert result["success"] is False and "1..100" in result["message"]
        assert account.closed == [] and account.reduced == []

    def test_a_partial_close_is_taken_fifo_across_lots(self, world):
        """4 + 6 held, 50% = 5: the oldest lot (4) is closed whole, the next reduced by 1."""
        ids, account = self._long(world, 4.0, 6.0)
        action = _sell(account)
        action.close_percent = 50
        assert action.execute()["success"] is True
        assert account.closed == [ids[0]]
        assert account.reduced == [(ids[1], 1.0)]

    def test_a_percent_never_flips(self, world):
        """The broker holds less than the slice: refused, nothing sent."""
        ids, account = self._long(world, 100.0, broker=30.0)
        action = _sell(account)
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is False and "flip" in result["message"]
        assert account.closed == [] and account.reduced == []

    def test_a_buy_covering_a_short_takes_the_same_percent(self, world):
        world.configure(True)
        txn_id = _hold(OrderDirection.SELL, 100.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -100.0}])
        action = _buy(account)
        action.close_percent = 25
        assert action.execute()["success"] is True
        assert account.reduced == [(txn_id, 25.0)]

    def test_an_entry_ignores_the_percent(self, world):
        world.configure(True)
        action = _sell(_StubAccount([]))
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is True and result["data"]["opens_short"] is True

    def test_the_evaluator_passes_the_rule_value_as_the_percent(self):
        from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
        from ba2_common.core.types import ExpertActionType
        ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
        ev.account = _StubAccount([])
        for at, cls in ((ExpertActionType.SELL, SellAction), (ExpertActionType.BUY, BuyAction)):
            action = ev._create_trade_action(at, {"action_type": at.value, "value": 40.0}, SYMBOL,
                                             OrderRecommendation.SELL, None, _rec())
            assert isinstance(action, cls) and action.close_percent == 40.0
            plain = ev._create_trade_action(at, {"action_type": at.value}, SYMBOL,
                                            OrderRecommendation.SELL, None, _rec())
            assert plain.close_percent is None


# --------------------------------------------------------------------------- #
# review fixes of be1a0aab: what a partial close refuses, and how it stops
# --------------------------------------------------------------------------- #

class TestPartialCloseRefusals:
    def _long(self, world, qty=100.0, filled=None, broker=None):
        world.configure(True)
        txn_id = _hold(OrderDirection.BUY, qty, filled=filled)
        held = qty if filled is None else filled
        return txn_id, _StubAccount([{"symbol": SYMBOL, "qty": held if broker is None else broker}])

    @pytest.mark.parametrize("bad", ["half", "", [50], {"p": 50}])
    def test_a_non_numeric_percent_is_refused_not_raised(self, world, bad):
        txn_id, account = self._long(world)
        action = _sell(account)
        action.close_percent = bad
        result = action.execute()
        assert result["success"] is False and "must be a number" in result["message"]
        assert account.closed == [] and account.reduced == []

    def test_nan_is_refused(self, world):
        txn_id, account = self._long(world)
        action = _sell(account)
        action.close_percent = float("nan")
        assert action.execute()["success"] is False
        assert account.closed == [] and account.reduced == []

    def test_a_partly_filled_lot_refuses_a_partial_close(self, world):
        """100 ordered, 60 filled: the live trim sizes from the ORDERED quantity, so a partial
        close is refused (TransactionHelper is shared with Smart RM and left unchanged)."""
        txn_id, account = self._long(world, 100.0, filled=60.0)
        action = _sell(account)
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is False and "partly filled" in result["message"]
        assert result["data"]["partly_filled_transaction_ids"] == [txn_id]
        assert account.closed == [] and account.reduced == []

    def test_a_partly_filled_lot_still_closes_in_full(self, world):
        txn_id, account = self._long(world, 100.0, filled=60.0)
        assert _sell(account).execute()["success"] is True
        assert account.closed == [txn_id]

    def test_a_fractional_position_refuses_a_partial_close(self, world):
        txn_id, account = self._long(world, 10.5)
        action = _sell(account)
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is False and "fractional" in result["message"]
        assert account.closed == [] and account.reduced == []

    def test_a_fractional_position_still_closes_in_full(self, world):
        txn_id, account = self._long(world, 10.5)
        assert _sell(account).execute()["success"] is True
        assert account.closed == [txn_id]

    def test_one_fractional_lot_refuses_even_when_the_total_is_whole(self, world):
        world.configure(True)
        _hold(OrderDirection.BUY, 4.5)
        _hold(OrderDirection.BUY, 5.5)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        action = _sell(account)
        action.close_percent = 50
        result = action.execute()
        assert result["success"] is False and "fractional" in result["message"]

    def test_the_float_floor_edge(self, world):
        """375 x 18.4 / 100 is 68.99999999999999 in binary: 18.4% of 375 must sell 69, not 68."""
        txn_id, account = self._long(world, 375.0)
        action = _sell(account)
        action.close_percent = 18.4
        assert action.execute()["success"] is True
        assert account.reduced == [(txn_id, 69.0)]


class TestFifoStopsAtTheFirstFailedLeg:
    def test_a_failed_close_stops_the_remaining_legs(self, world):
        world.configure(True)
        a, b = _hold(OrderDirection.BUY, 4.0), _hold(OrderDirection.BUY, 6.0)

        class _FailingFirst(_StubAccount):
            def close_transaction(self, transaction_id):
                self.closed.append(transaction_id)
                return {"success": False, "message": "broker said no", "close_order_id": None}

        account = _FailingFirst([{"symbol": SYMBOL, "qty": 10.0}])
        action = _sell(account)
        action.close_percent = 50               # 5 = lot a (4) whole + 1 of lot b
        result = action.execute()
        assert result["success"] is False
        assert account.closed == [a] and account.reduced == [], "must stop at the failed leg"
        assert [leg["transaction_id"] for leg in result["data"]["legs"]] == [a]
        assert result["data"]["legs"][0]["success"] is False
        assert result["data"]["closed_transaction_ids"] == [a]
        assert "stopped after 1 of 2 legs" in result["message"]


class TestLiveReduceTransactionDefault:
    """I1: the live AccountInterface.reduce_transaction delegates to the shared partial-close
    facility with a NEGATIVE quantity change and maps its orders_created."""

    def test_delegation_sign_and_mapping(self, world, monkeypatch):
        from ba2_common.core.TransactionHelper import TransactionHelper
        txn_id = _hold(OrderDirection.BUY, 100.0)
        account = _StubAccount([])
        calls = []

        def fake(acct, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
            calls.append((acct, transaction.id, qty_change, expert_id))
            return {"success": True, "message": "trimmed", "orders_created": [11, 12],
                    "orders_canceled": [3]}

        monkeypatch.setattr(TransactionHelper, "adjust_quantity_with_tpsl", staticmethod(fake))
        result = AccountInterface.reduce_transaction(account, txn_id, 30.0)
        assert calls == [(account, txn_id, -30.0, EXPERT_ID)]
        assert result == {"success": True, "message": "trimmed", "close_order_ids": [11, 12]}

    @pytest.mark.parametrize("qty, filled, want", [
        (100.0, None, "not below"),       # a full close is close_transaction's job
        (0, None, "positive quantity"),
        (30.0, 60.0, "partly filled"),    # I2 at the account seam too
    ])
    def test_refusals_never_reach_the_facility(self, world, monkeypatch, qty, filled, want):
        from ba2_common.core.TransactionHelper import TransactionHelper
        txn_id = _hold(OrderDirection.BUY, 100.0, filled=filled)
        monkeypatch.setattr(TransactionHelper, "adjust_quantity_with_tpsl",
                            staticmethod(lambda *a, **k: pytest.fail("reached the facility")))
        result = AccountInterface.reduce_transaction(_StubAccount([]), txn_id, qty)
        assert result["success"] is False and want in result["message"]
        assert result["close_order_ids"] == []

    def test_an_option_transaction_is_refused(self, world, monkeypatch):
        from ba2_common.core.TransactionHelper import TransactionHelper
        txn_id = add_instance(Transaction(symbol=SYMBOL, quantity=2.0, side=OrderDirection.BUY,
                                          status=TransactionStatus.OPENED, expert_id=EXPERT_ID,
                                          asset_class=AssetClass.OPTION))
        monkeypatch.setattr(TransactionHelper, "adjust_quantity_with_tpsl",
                            staticmethod(lambda *a, **k: pytest.fail("reached the facility")))
        result = AccountInterface.reduce_transaction(_StubAccount([]), txn_id, 1.0)
        assert result["success"] is False and "OPTION" in result["message"]


def test_position_direction_survives_a_missing_transaction_row(world):
    """_position_is_long's transaction fallback: get_instance RAISES on a missing row; the
    lookup must fall through to the recommendation instead of failing the adjustment."""
    order = SimpleNamespace(id=1, side=None, transaction_id=987654)
    action = AdjustStopLossAction(SYMBOL, _StubAccount([]), OrderRecommendation.SELL, order,
                                  expert_recommendation=_rec(), reference_value="current_price",
                                  percent=-5.0)
    assert action._position_is_long(order) == (False, "recommendation SELL (no order)")


# --------------------------------------------------------------------------- #
# S2 (S1 review note): an ENTRY that failed before creating an order closed nothing
# --------------------------------------------------------------------------- #

class TestAFailedEntrySaysItClosedNothing:
    """BUY/SELL are closing-type actions (a buy covers a short, a sell closes a long), so a
    result with neither ``closed_transaction_ids`` nor an ``order_id`` reads as "unknown what it
    closed" and blocks every TP/SL adjustment of the pass. The three early failures of an ENTRY
    closed nothing, and now say so."""

    @staticmethod
    def _assert_closed_nothing(result, message):
        from ba2_common.core.TradeActionEvaluator import _closed_ids_or_unknown
        from ba2_common.core.types import ExpertActionType

        assert result["success"] is False
        assert result["message"] == message
        assert result["data"]["closed_transaction_ids"] == []
        for action_type in (ExpertActionType.BUY, ExpertActionType.SELL):
            assert _closed_ids_or_unknown(action_type, result["data"], set()) is False

    def test_a_buy_without_a_price(self, world):
        result = _buy(_StubAccount([], price=None)).execute()
        self._assert_closed_nothing(result, f"Cannot get current price for {SYMBOL}")

    def test_a_buy_whose_order_record_failed(self, world, monkeypatch):
        action = _buy(_StubAccount([]))
        monkeypatch.setattr(action, "create_order_record", lambda **k: None)
        self._assert_closed_nothing(action.execute(), "Failed to create order record")

    def test_a_short_entry_whose_order_record_failed(self, world, monkeypatch):
        world.configure(True)
        action = _sell(_StubAccount([]))
        monkeypatch.setattr(action, "create_order_record", lambda **k: None)
        self._assert_closed_nothing(action.execute(), "Failed to create order record")
