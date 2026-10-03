"""AlpacaAccount.get_filled_trades must include filled bracket/OCO legs.

Orders are fetched with nested=True, so an exit leg only exists inside its
parent's ``legs`` (the parent then shows filled_qty 0). No live API call.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _o(id, side="sell", qty=0, price=0, symbol="AAA", legs=None, day=1):
    t = datetime(2026, 9, day, 15, 0, tzinfo=timezone.utc)
    return NS(id=id, side=f"OrderSide.{side.upper()}", filled_qty=qty, filled_avg_price=price,
              symbol=symbol, filled_at=t if qty else None, created_at=t, legs=legs)


def _acct(raw):
    a = object.__new__(AlpacaAccount)
    a.id = 1
    a._check_authentication = lambda: True
    a._fetch_raw_alpaca_orders = lambda status=None, fetch_all=False, full_history=False: raw
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


def _fetch(a, **kw):
    f = AlpacaAccount._fetch_raw_alpaca_orders
    return getattr(f, "__wrapped__", f)(a, **kw)


def _full_page(n_top=400, n_legs=100, base_min=0):
    """n_top top-level orders carrying n_legs legs = n_top + n_legs rows (=500)."""
    page = []
    for i in range(n_top):
        o = _o(f"a{i}", legs=[_o(f"l{i}")] if i < n_legs else None)
        o.created_at = datetime(2026, 9, 10, 12, base_min, 0, tzinfo=timezone.utc) + timedelta(seconds=i)
        page.append(o)
    return page


def test_full_history_continues_when_legs_fill_the_limit_and_overlaps_one_second():
    page1 = _full_page()
    a = _paged_acct([page1, [_o("old", day=1)]])
    out = _fetch(a, fetch_all=True, full_history=True)
    assert {o.id for o in out} >= {"old", "a0"}
    assert a.client.get_orders.call_count == 2
    assert a.client.get_orders.call_args_list[1][0][0].until ==         min(o.created_at for o in page1) + timedelta(seconds=1)


def test_legacy_mode_is_unchanged_one_page_for_a_full_page_containing_legs():
    """refresh_orders/get_orders path: old stop check (len < limit) and no pagination."""
    a = _paged_acct([_full_page(), [_o("old", day=1)]])
    out = _fetch(a, fetch_all=True)
    assert a.client.get_orders.call_count == 1
    assert len(out) == 400


def test_legacy_mode_pages_with_minus_one_day_when_page_is_len_full():
    page1 = [_o(f"x{i}") for i in range(500)]
    for i, o in enumerate(page1):
        o.created_at = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=i)
    a = _paged_acct([page1, [_o("old", day=1)]])
    _fetch(a, fetch_all=True)
    assert a.client.get_orders.call_args_list[1][0][0].until ==         min(o.created_at for o in page1) - timedelta(days=1)


def test_legacy_single_page_call_is_unchanged():
    a = _paged_acct([[_o("a")]])
    _fetch(a)
    assert a.client.get_orders.call_count == 1


def test_parent_and_leg_split_at_page_boundary_keeps_the_copy_with_more_legs():
    # Page 1 ends with the parent seen WITHOUT its leg; page 2 (overlap) has it WITH the leg.
    page1 = _full_page(n_top=500, n_legs=0)
    boundary = min(o.created_at for o in page1)
    bare = next(o for o in page1 if o.created_at == boundary)
    full = _o(bare.id, legs=[_o("leg-split", qty=1, price=5)])
    full.created_at = boundary
    a = _paged_acct([page1, [full, _o("older", day=1)]])
    out = _fetch(a, fetch_all=True, full_history=True)
    kept = next(o for o in out if o.id == bare.id)
    assert [l.id for l in kept.legs] == ["leg-split"]


def test_same_page_forever_terminates():
    page = _full_page()
    a = object.__new__(AlpacaAccount)
    a.id = 1
    a._check_authentication = lambda: True
    a.client = MagicMock()
    a.client.get_orders.side_effect = lambda f: list(page)
    out = _fetch(a, fetch_all=True, full_history=True)
    assert a.client.get_orders.call_count == 2
    assert len(out) == 400


def test_full_history_failed_page_raises_instead_of_returning_truncated_data():
    a = _paged_acct([_full_page()])
    a.client.get_orders.side_effect = [_full_page(), RuntimeError("boom")]
    with pytest.raises(RuntimeError):
        _fetch(a, fetch_all=True, full_history=True)


def test_legacy_failed_fetch_still_returns_empty():
    a = _paged_acct([])
    a.client.get_orders.side_effect = RuntimeError("boom")
    assert _fetch(a, fetch_all=True) == []


def test_get_filled_trades_requests_full_history_and_propagates_failure():
    a = object.__new__(AlpacaAccount)
    a.id = 1
    a._check_authentication = lambda: True
    seen = {}

    def fake(status=None, fetch_all=False, full_history=False):
        seen.update(fetch_all=fetch_all, full_history=full_history)
        raise RuntimeError("boom")

    a._fetch_raw_alpaca_orders = fake
    with pytest.raises(RuntimeError):
        a.get_filled_trades()
    assert seen == {"fetch_all": True, "full_history": True}
