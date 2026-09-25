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
    AssetClass, OrderDirection, OrderRecommendation, OrderStatus, TransactionStatus,
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
        self.position_reads = 0

    def get_positions(self):
        self.position_reads += 1
        return self._positions if self._positions is None else list(self._positions)

    def close_transaction(self, transaction_id):
        self.closed.append(transaction_id)
        return {"success": True, "message": f"closed {transaction_id}", "close_order_id": 900 + transaction_id}

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
    def __init__(self, enable_sell):
        self._settings = {} if enable_sell is None else {"enable_sell": enable_sell}

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

    def configure(enable_sell):
        instance_resolver.set_instance_resolver(_Resolver(_Expert(enable_sell)))

    state.configure = configure
    configure(False)
    try:
        with ts.inmem_trades():
            yield state
    finally:
        instance_resolver.set_instance_resolver(previous)


def _rec(action=OrderRecommendation.SELL):
    return SimpleNamespace(instance_id=EXPERT_ID, id=7, recommended_action=action)


def _hold(side, qty, status=TransactionStatus.OPENED, asset_class=AssetClass.EQUITY):
    txn = Transaction(symbol=SYMBOL, quantity=qty, side=side, status=status,
                      expert_id=EXPERT_ID, asset_class=asset_class)
    return add_instance(txn)


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
        assert "enable_sell is off" in result["message"]

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

    def test_enable_sell_off_keeps_the_pre_change_pending_sell(self, world):
        """UNCHANGED: with enable_sell off (every live expert) a long still stages the PENDING
        qty-0 sell it always did. The risk manager's permission filter then drops it, as before;
        whether that inert `sell` exit should close the long is a separate decision."""
        world.configure(False)
        _hold(OrderDirection.BUY, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": 10.0}])
        result = _sell(account).execute()

        assert result["success"] is True
        assert "opens_short" not in result["data"]
        order = get_instance(TradingOrder, result["data"]["order_id"])
        assert (order.side, order.quantity, order.status) == (
            OrderDirection.SELL, 0.0, OrderStatus.PENDING)
        assert account.closed == []


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

    def test_enable_sell_off_short_book_keeps_the_legacy_refusal(self, world):
        world.configure(False)
        result = _sell(_StubAccount([{"symbol": SYMBOL, "qty": -10.0}])).execute()
        assert result["success"] is False
        assert result["message"] == f"No long position to sell for {SYMBOL}"


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
    @pytest.mark.parametrize("enable_sell", [True, False])
    def test_buy_while_short_covers_and_never_flips(self, world, enable_sell):
        world.configure(enable_sell)
        txn_id = _hold(OrderDirection.SELL, 10.0)
        account = _StubAccount([{"symbol": SYMBOL, "qty": -10.0}])
        result = _buy(account).execute()

        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id]
        assert result["data"]["closing"] is True and "order_id" not in result["data"]
        assert _pending_orders() == [], "a pending BUY would be sized as a fresh long entry"

    def test_an_unfilled_short_entry_is_cancelled_not_refused(self, world):
        txn_id = _hold(OrderDirection.SELL, 10.0, TransactionStatus.WAITING)
        account = _StubAccount([])
        result = _buy(account).execute()
        assert result["success"] is True, result["message"]
        assert account.closed == [txn_id] and _pending_orders() == []

    def test_buy_while_short_refuses_when_the_broker_is_less_short(self, world):
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
