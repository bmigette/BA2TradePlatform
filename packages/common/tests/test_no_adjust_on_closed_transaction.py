"""C1: Phase 2 never adjusts TP/SL on a transaction a Phase-1 action in the same pass closed/reduced.

THE DEFECT (review of be1a0aab). A rule firing ``sell 50%`` + ``adjust_stop_loss`` on a held long:
Phase 1's ``reduce_transaction`` -> ``TransactionHelper.adjust_quantity_with_tpsl`` stages the
partial close as a WAITING_TRIGGER order with no broker id and writes ``transaction.quantity``
down. The closing sell returns no ``order_id``, so Phase 2 fell into the ``existing_transactions``
branch and called ``adjust_tp_sl`` on that transaction -- and live, AlpacaAccount's
``_handle_filled_entry_exit`` then CANCELED every non-terminal order without a broker id,
the pending partial close included: DB 50, broker 100, part unprotected. The older sibling: a
full close (closing sell or CloseAction) plus an adjust staged exit legs on a closing position.

These drive the REAL ``TradeActionEvaluator.execute`` with real actions; the account is a stub
only at the broker edge. Its TP/SL entry points (adjust_tp_sl / adjust_sl / adjust_tp) RECORD
every call and the tests assert none was made for the closed/reduced transaction (a positive
control proves an adjust alone does reach them); ``_handle_filled_entry_exit`` RAISES if reached.
"""
from types import SimpleNamespace

import pytest

import ba2_common.config as cfg
from ba2_common.core import instance_resolver
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.TradeActions import (
    AdjustStopLossAction, AdjustTakeProfitAction, BuyAction, CloseAction, SellAction,
)
from ba2_common.core.types import (
    OrderDirection, OrderRecommendation as R, OrderStatus, OrderType, ReferenceValue,
    TransactionStatus,
)

EXPERT_ID = 42
SYMBOL = "AAPL"


class _Account(AccountInterface):
    def __init__(self, positions):
        self.id = 1
        self._settings_cache = None
        self._positions = positions
        self.closed, self.reduced, self.adjusted = [], [], []

    def get_positions(self):
        return list(self._positions)

    def close_transaction(self, transaction_id):
        self.closed.append(transaction_id)
        return {"success": True, "message": "closed", "close_order_id": 900}

    def reduce_transaction(self, transaction_id, quantity):
        self.reduced.append((transaction_id, quantity))
        return {"success": True, "message": "reduced", "close_order_ids": [800]}

    # -- every TP/SL entry point records; the positive control needs them to work --
    def adjust_tp_sl(self, transaction, tp, sl, source=""):
        self.adjusted.append(("tp_sl", transaction.id))
        return True

    def adjust_sl(self, transaction, price, source=""):
        self.adjusted.append(("sl", transaction.id))
        return True

    def adjust_tp(self, transaction, price, source=""):
        self.adjusted.append(("tp", transaction.id))
        return True

    def _handle_filled_entry_exit(self, *a, **k):
        raise AssertionError("_handle_filled_entry_exit reached: it cancels a pending close")

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return 110.0

    def get_account_info(self): raise AssertionError("not read")
    def _get_instrument_current_price_impl(self, *a, **k): return 110.0
    def _submit_order_impl(self, *a, **k): raise AssertionError("nothing is submitted here")
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
    def get_setting_with_interface_default(self, key, log_warning=False):
        return {"enable_buy": True, "enable_sell": True}.get(key)


class _Resolver:
    def get_expert_instance(self, expert_id): return _Expert()
    def get_account_instance(self, account_id): return None
    def get_account_instance_from_transaction(self, transaction): return None


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setattr(cfg, "get_min_tp_sl_percent", lambda: 3.0)
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_Resolver())
    try:
        with ts.inmem_trades():
            yield
    finally:
        instance_resolver.set_instance_resolver(previous)


def _position(side, qty=100.0):
    txn_id = add_instance(Transaction(symbol=SYMBOL, quantity=qty, side=side,
                                      status=TransactionStatus.OPENED, expert_id=EXPERT_ID,
                                      open_price=100.0, stop_loss=92.0 if side == OrderDirection.BUY else 120.0))
    # Both stops are LOOSER than what the rule asks at 110 (104.50 long, 115.50 short), so an
    # adjust that is reached always calls the account -- the skip is what keeps it silent.
    add_instance(TradingOrder(account_id=1, symbol=SYMBOL, quantity=qty, side=side,
                              order_type=OrderType.MARKET, status=OrderStatus.FILLED,
                              filled_qty=qty, open_price=100.0, transaction_id=txn_id))
    return get_instance(Transaction, txn_id)


def _rec(action):
    return SimpleNamespace(instance_id=EXPERT_ID, id=7, recommended_action=action)


def _run(account, txn, *actions):
    ev = TradeActionEvaluator(account=account, instrument_name=SYMBOL, existing_transactions=[txn])
    ev.expert_recommendation = _rec(R.SELL)
    ev.trade_actions = list(actions)
    return ev.execute(submit_to_broker=True)


def _sl(action=R.SELL):
    return AdjustStopLossAction(SYMBOL, None, action, None, expert_recommendation=_rec(action),
                                reference_value=ReferenceValue.CURRENT_PRICE.value, percent=-5.0)


def _tp(action=R.SELL):
    return AdjustTakeProfitAction(SYMBOL, None, action, None, expert_recommendation=_rec(action),
                                  reference_value=ReferenceValue.CURRENT_PRICE.value, percent=10.0)


def _bind(account, *actions):
    for a in actions:
        a.account = account
    return actions


@pytest.mark.parametrize("side, qty, rec", [(OrderDirection.BUY, 100.0, R.SELL),
                                            (OrderDirection.SELL, -100.0, R.BUY)])
def test_positive_control_an_adjust_alone_does_reach_the_account(side, qty, rec):
    txn = _position(side)
    account = _Account([{"symbol": SYMBOL, "qty": qty}])
    _run(account, txn, *_bind(account, _sl(rec)))
    assert account.adjusted == [("sl", txn.id)]


@pytest.mark.parametrize("with_tp", [False, True], ids=["sl-only", "merged-tp-sl"])
def test_a_partial_sell_plus_an_adjust_never_touches_the_reduced_transaction(with_tp):
    txn = _position(OrderDirection.BUY)
    account = _Account([{"symbol": SYMBOL, "qty": 100.0}])
    sell = SellAction(SYMBOL, account, R.SELL, expert_recommendation=_rec(R.SELL), percent=50)
    extra = (_sl(), _tp()) if with_tp else (_sl(),)
    results = _run(account, txn, sell, *_bind(account, *extra))
    assert account.reduced == [(txn.id, 50.0)]
    assert account.adjusted == [], "Phase 2 re-armed exit legs on the transaction it just reduced"
    skipped = [r for r in results if (r.get("data") or {}).get("skipped") == "closed_or_reduced_this_pass"]
    assert skipped and skipped[0]["data"]["transaction_id"] == txn.id


def test_a_full_closing_sell_plus_an_adjust_never_touches_the_closed_transaction():
    txn = _position(OrderDirection.BUY)
    account = _Account([{"symbol": SYMBOL, "qty": 100.0}])
    sell = SellAction(SYMBOL, account, R.SELL, expert_recommendation=_rec(R.SELL))
    _run(account, txn, sell, *_bind(account, _sl()))
    assert account.closed == [txn.id] and account.adjusted == []


def test_a_partial_cover_plus_an_adjust_never_touches_the_reduced_short():
    txn = _position(OrderDirection.SELL)
    account = _Account([{"symbol": SYMBOL, "qty": -100.0}])
    buy = BuyAction(SYMBOL, account, R.BUY, expert_recommendation=_rec(R.BUY), percent=50)
    _run(account, txn, buy, *_bind(account, _sl(R.BUY)))
    assert account.reduced == [(txn.id, 50.0)] and account.adjusted == []


def test_close_action_plus_an_adjust_never_touches_the_closed_transaction():
    txn = _position(OrderDirection.BUY)
    account = _Account([{"symbol": SYMBOL, "qty": 100.0}])
    entry = get_instance(TradingOrder, [o for o in ts.orders_where(transaction_id=txn.id)][0].id)
    close = CloseAction(SYMBOL, account, R.SELL, existing_order=entry,
                        expert_recommendation=_rec(R.SELL))
    _run(account, txn, close, *_bind(account, _sl(), _tp()))
    assert account.closed == [txn.id] and account.adjusted == []


def test_every_closing_result_names_its_transactions():
    """The evaluator keys the skip on data.closed_transaction_ids: every closing/reducing
    result -- sell, buy-cover, CloseAction, partial trim, deferred -- must carry it."""
    txn = _position(OrderDirection.BUY)
    account = _Account([{"symbol": SYMBOL, "qty": 100.0}])
    for percent in (None, 50):
        sell = SellAction(SYMBOL, account, R.SELL, expert_recommendation=_rec(R.SELL),
                          percent=percent)
        for submit in (True, False):
            sell.submit_to_broker = submit
            assert sell.execute()["data"]["closed_transaction_ids"] == [txn.id]
