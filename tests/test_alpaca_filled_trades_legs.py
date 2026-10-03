"""AlpacaAccount.get_filled_trades must include filled bracket/OCO legs.

Orders are fetched with nested=True, so an exit leg only exists inside its
parent's ``legs`` (the parent then shows filled_qty 0). No live API call.
"""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _o(id, side="sell", qty=0, price=0, symbol="AAA", legs=None, day=1):
    t = datetime(2026, 9, day, 15, 0, tzinfo=timezone.utc)
    return NS(id=id, side=f"OrderSide.{side.upper()}", filled_qty=qty, filled_avg_price=price,
              symbol=symbol, filled_at=t if qty else None, created_at=t, legs=legs)


def _acct(raw):
    a = object.__new__(AlpacaAccount)
    a.id = 1
    a._check_authentication = lambda: True
    a._fetch_raw_alpaca_orders = lambda status=None, fetch_all=False: raw
    return a


def test_filled_stop_leg_of_unfilled_parent_is_returned_as_sell():
    stop = _o("leg", qty=3, price=9.5, day=5)
    parent = _o("p", side="buy", qty=0, legs=[stop])
    trades = _acct([parent]).get_filled_trades()
    assert trades == [{"symbol": "AAA", "qty": 3.0, "side": "SELL", "date": stop.filled_at, "price": 9.5}]


def test_filled_parent_and_filled_leg_both_returned():
    leg = _o("leg", qty=2, price=11, day=6)
    parent = _o("p", side="buy", qty=2, price=10, legs=[leg])
    trades = _acct([parent]).get_filled_trades()
    assert [(t["side"], t["price"]) for t in trades] == [("BUY", 10.0), ("SELL", 11.0)]


def test_duplicate_ids_not_doubled():
    leg = _o("leg", qty=2, price=11)
    parent = _o("p", side="buy", qty=2, price=10, legs=[leg])
    trades = _acct([parent, leg, parent]).get_filled_trades()
    assert len(trades) == 2


def test_unfilled_leg_ignored():
    parent = _o("p", side="buy", qty=2, price=10, legs=[_o("tp", qty=0), _o("sl", qty=0)])
    assert len(_acct([parent]).get_filled_trades()) == 1


def test_nested_legs_of_legs():
    inner = _o("inner", qty=1, price=7)
    mid = _o("mid", qty=0, legs=[inner])
    parent = _o("p", side="buy", qty=0, legs=[mid])
    trades = _acct([parent]).get_filled_trades()
    assert [t["price"] for t in trades] == [7.0]


def test_symbol_filter_applies_to_legs():
    leg = _o("leg", qty=1, price=7, symbol="BBB")
    parent = _o("p", side="buy", qty=0, symbol="BBB", legs=[leg])
    assert _acct([parent]).get_filled_trades(symbol="AAA") == []


# ---- pagination: Alpaca's limit counts legs ---------------------------------

def _paged_acct(pages):
    a = object.__new__(AlpacaAccount)
    a.id = 1
    a._check_authentication = lambda: True
    a.client = MagicMock()
    a.client.get_orders.side_effect = list(pages) + [[]]
    return a


def test_fetch_all_continues_when_legs_fill_the_limit():
    # 400 top-level orders carrying 100 legs = 500 rows = the limit: more pages exist.
    page1 = [_o(f"a{i}", day=10, legs=[_o(f"l{i}")] if i < 100 else None) for i in range(400)]
    for i, o in enumerate(page1):
        o.created_at = datetime(2026, 9, 10, 12, i // 60, i % 60, tzinfo=timezone.utc)
    page2 = [_o("old", day=1)]
    a = _paged_acct([page1, page2])
    out = AlpacaAccount._fetch_raw_alpaca_orders.__wrapped__(a, fetch_all=True) \
        if hasattr(AlpacaAccount._fetch_raw_alpaca_orders, "__wrapped__") \
        else a._fetch_raw_alpaca_orders(fetch_all=True)
    assert {o.id for o in out} >= {"old", "a0"}
    assert a.client.get_orders.call_count == 2
    # next page starts exactly at the oldest order (no 1-day gap)
    assert a.client.get_orders.call_args_list[1][0][0].until == min(o.created_at for o in page1)
