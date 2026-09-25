"""TP/SL direction comes from the POSITION, not from the bar's recommendation.

``_AdjustPriceLevelAction`` used to read the recommendation FIRST and fall back to the order side
only on a HOLD. A LONG held through a SELL/UNDERWEIGHT bar was then priced as a short:

  * SL-only (``execute``): the stop landed ABOVE the market -> the min-distance floor, 3% under
    the market as the ratchet saw a "tighter" long stop;
  * TP-only (``execute``): the target dropped to the minimum-profit floor, entry +2%;
  * merged (``compute_price`` + ``ruleset_stop_policy``, TradeActionEvaluator Phase 2): the
    stop above the market and the target below it.

The mirror hit a SHORT on a BUY bar. Every path now reads the side of the position being adjusted
(the order, else its transaction); the recommendation is only a fallback when there is no order.
When the recommendation AGREES with the side -- every logged live case (81 prod, 46 dev, all BUY
on a long) -- nothing changes.
"""
from types import SimpleNamespace

import pytest

import ba2_common.config as cfg
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeActions import (
    AdjustStopLossAction, AdjustTakeProfitAction, ruleset_stop_policy, stop_is_long_position,
)
from ba2_common.core.types import (
    OrderDirection, OrderRecommendation as R, OrderStatus, OrderType, ReferenceValue,
    TransactionStatus,
)

OPEN = 100.0
SL_PCT, TP_PCT = -8.0, 10.0


class _Account(AccountInterface):
    def __init__(self, price):
        self.id = 1
        self._settings_cache = None
        self._price = price
        self.sent = []

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return self._price

    def adjust_sl(self, transaction, price, source=""):
        self.sent.append(("SL", round(price, 4)))
        transaction.stop_loss = price
        return True

    def adjust_tp(self, transaction, price, source=""):
        self.sent.append(("TP", round(price, 4)))
        transaction.take_profit = price
        return True

    def get_account_info(self): raise AssertionError("not read")
    def _get_instrument_current_price_impl(self, *a, **k): return self._price
    def _submit_order_impl(self, *a, **k): raise AssertionError("nothing is submitted")
    def adjust_tp_sl(self, *a, **k): return True
    def cancel_order(self, *a, **k): return None
    def get_balance(self): return 100_000.0
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def get_positions(self): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setattr(cfg, "get_min_tp_sl_percent", lambda: 3.0)
    with ts.inmem_trades():
        yield


def _position(side, stop_loss=None):
    """An OPENED position entered at OPEN; returns its FILLED entry order."""
    txn_id = add_instance(Transaction(symbol="XYZ", quantity=10.0, side=side,
                                      status=TransactionStatus.OPENED, expert_id=42,
                                      open_price=OPEN, stop_loss=stop_loss))
    order_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", quantity=10.0, side=side, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=10.0, open_price=OPEN, transaction_id=txn_id))
    return get_instance(TradingOrder, order_id)


def _rec():
    return SimpleNamespace(instance_id=42, id=7)


def _sl(account, rec, order):
    return AdjustStopLossAction("XYZ", account, rec, order, expert_recommendation=_rec(),
                                reference_value=ReferenceValue.ORDER_OPEN_PRICE.value,
                                percent=SL_PCT)


def _tp(account, rec, order):
    return AdjustTakeProfitAction("XYZ", account, rec, order, expert_recommendation=_rec(),
                                  reference_value=ReferenceValue.ORDER_OPEN_PRICE.value,
                                  percent=TP_PCT)


# (position side, market price, bar recommendation, expected SL, expected TP)
LONG = (OrderDirection.BUY, 105.0, 92.0, 110.0)      # SL below, TP above
SHORT = (OrderDirection.SELL, 95.0, 108.0, 90.0)     # SL above, TP below

CASES = [
    pytest.param(*LONG[:2], R.SELL, *LONG[2:], id="long-on-SELL"),
    pytest.param(*LONG[:2], R.UNDERWEIGHT, *LONG[2:], id="long-on-UNDERWEIGHT"),
    pytest.param(*SHORT[:2], R.BUY, *SHORT[2:], id="short-on-BUY"),
    pytest.param(*SHORT[:2], R.OVERWEIGHT, *SHORT[2:], id="short-on-OVERWEIGHT"),
    # The recommendation agrees with the side (or is HOLD): unchanged.
    pytest.param(*LONG[:2], R.BUY, *LONG[2:], id="long-on-BUY-unchanged"),
    pytest.param(*LONG[:2], R.HOLD, *LONG[2:], id="long-on-HOLD-unchanged"),
    pytest.param(*SHORT[:2], R.SELL, *SHORT[2:], id="short-on-SELL-unchanged"),
]


@pytest.mark.parametrize("side, market, rec, want_sl, want_tp", CASES)
def test_merged_path(side, market, rec, want_sl, want_tp):
    order = _position(side)
    account = _Account(market)
    sl, tp = _sl(account, rec, order), _tp(account, rec, order)
    sl_px, tp_px = sl.compute_price(order), tp.compute_price(order)
    assert sl_px == pytest.approx(want_sl)
    assert tp_px == pytest.approx(want_tp)
    txn = get_instance(Transaction, order.transaction_id)
    applied, _reason = ruleset_stop_policy(txn, sl_px, stop_is_long_position(txn, order),
                                           lambda: None, price_getter=lambda: market,
                                           rule_price=sl.rule_price)
    assert applied == pytest.approx(want_sl)
    # the protective side of the market
    assert (applied < market) if side == OrderDirection.BUY else (applied > market)
    assert (tp_px > market) if side == OrderDirection.BUY else (tp_px < market)


@pytest.mark.parametrize("side, market, rec, want_sl, want_tp", CASES)
def test_stop_only_execute(side, market, rec, want_sl, want_tp):
    order = _position(side)
    account = _Account(market)
    result = _sl(account, rec, order).execute()
    assert result["success"] is True, result["message"]
    assert account.sent == [("SL", pytest.approx(want_sl))]


@pytest.mark.parametrize("side, market, rec, want_sl, want_tp", CASES)
def test_take_profit_only_execute(side, market, rec, want_sl, want_tp):
    order = _position(side)
    account = _Account(market)
    result = _tp(account, rec, order).execute()
    assert result["success"] is True, result["message"]
    assert account.sent == [("TP", pytest.approx(want_tp))]


def test_no_order_falls_back_to_the_recommendation():
    """A pure computation with no position to read keeps the old reading of the recommendation."""
    account = _Account(100.0)
    sl = AdjustStopLossAction("XYZ", account, R.SELL, None, expert_recommendation=_rec(),
                              reference_value=ReferenceValue.CURRENT_PRICE.value, percent=SL_PCT)
    assert sl._position_is_long() == (False, "recommendation SELL (no order)")
