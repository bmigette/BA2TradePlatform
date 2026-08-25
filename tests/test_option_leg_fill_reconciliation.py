"""Task 4 — a multi-leg option order's CHILD rows must carry the real per-leg economics.

Measured on the live DB (transaction 660, an ACN bull call spread): the parent order 2147
filled — ``status=FILLED, filled_qty=1, open_price=2.3`` (the net debit) — and recorded
``legs_broker_ids=["be463ce6…", "b346df92…"]``. Its two children 2148/2149 each hold the
matching ``broker_order_id`` and the contract identity (``ACN260821C00130000`` /
``ACN260821C00135000``)… and are still sitting at ``ACCEPTED / filled_qty=0 /
open_price=NULL``, because ``refresh_orders`` only ever reconciled OCO legs.

Two consequences, both measured, not hypothetical:
  * per-leg attribution is impossible — "which leg killed this trade?" has no answer when
    every leg row says it never executed; and
  * the executed position has to be inferred from rows the database itself says never
    executed.

``refresh_orders`` already receives everything needed: ``_fetch_raw_alpaca_orders`` sets
``filter.nested = True``, so an MLEG parent comes back with a ``legs`` array of full order
objects, each with its own ``id``, ``status``, ``filled_qty`` and ``filled_avg_price``.
``parent.legs_broker_ids`` ↔ ``child.broker_order_id`` is the link.

THE RULE THESE TESTS EXIST TO PIN: a leg the broker does not return must stay UNTOUCHED —
not be marked filled. Silence is not "this leg didn't fill", exactly as ``get_positions()``
returning ``None`` is not ``[]``. That conflation has caused five separate incidents in
this codebase.

No network: the Alpaca SDK client is a fake, and every broker order is built by feeding a
RAW payload dict to ``alpaca.trading.models.Order`` — its ``__init__`` normalises the ``""``
that real MLEG responses put in ``type``/``side``/``symbol`` into ``None``, and python
kwargs on a hand-rolled stub would skip that coercion entirely.
"""
from datetime import datetime, timezone

import pytest
from alpaca.trading.models import Order as AlpacaOrder

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType as CoreOrderType,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

# Frozen, and deliberately NOT today: a mutation once survived because the frozen date
# happened to equal the wall clock.
FROZEN_NOW = datetime(2026, 8, 14, 14, 30, tzinfo=timezone.utc)

PARENT_BROKER_ID = "1bb1b126-5aa5-4fc1-a210-7fca0d2fc848"
LEG_LONG_BROKER_ID = "be463ce6-2161-472d-8422-50c139193427"
LEG_SHORT_BROKER_ID = "b346df92-bc8c-495a-a9ba-b55a6f037f6e"
LONG_OCC = "ACN260821C00130000"
SHORT_OCC = "ACN260821C00135000"


# ---------------------------------------------------------------------------
# Raw broker payloads -> real alpaca-py models
# ---------------------------------------------------------------------------
def _raw_leg(broker_id, symbol, side, status, filled_qty, filled_avg_price,
             qty="1", ratio_qty="1"):
    """One leg of a nested MLEG response, shaped like the real JSON.

    Note ``"type": ""`` / ``"order_type": ""`` / ``"asset_class": "us_option"`` — real MLEG
    legs omit the type by sending an empty string, which ``Order.__init__`` turns into
    ``None``. Building these from a raw dict keeps that coercion in the test.
    """
    return {
        "id": broker_id,
        "client_order_id": f"leg-{broker_id[:8]}",
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
        "submitted_at": FROZEN_NOW,
        "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "symbol": symbol,
        "asset_class": "us_option",
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "order_class": "mleg",
        "order_type": "",
        "type": "",
        "side": side,
        "time_in_force": "day",
        "status": status,
        "extended_hours": False,
        "ratio_qty": ratio_qty,
    }


def _raw_mleg_parent(db_order_id, status, filled_qty, filled_avg_price, legs,
                     qty="1", limit_price="2.30"):
    """The MLEG parent as ``get_orders(nested=True)`` returns it: no top-level symbol or
    side (both ``""`` in the wire format), and the legs nested underneath."""
    return {
        "id": PARENT_BROKER_ID,
        "client_order_id": str(db_order_id),
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
        "submitted_at": FROZEN_NOW,
        "asset_id": "",
        "symbol": "",
        "asset_class": "",
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "order_class": "mleg",
        "order_type": "limit",
        "type": "limit",
        "side": "",
        "time_in_force": "day",
        "limit_price": limit_price,
        "status": status,
        "extended_hours": False,
        "legs": legs,
    }


def _broker_order(raw):
    return AlpacaOrder(**raw)


# ---------------------------------------------------------------------------
# Account + DB fixtures
# ---------------------------------------------------------------------------
def _make_alpaca(account_id, raw_orders):
    """An AlpacaAccount whose only broker contact is a canned ``get_orders``."""
    acct = AlpacaAccount.__new__(AlpacaAccount)          # bypass __init__/DB/network
    acct.id = account_id
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get_orders(self, filter):
            self.calls += 1
            assert filter.nested is True, "leg economics only arrive when nested=True"
            return [_broker_order(r) for r in raw_orders]

    acct.client = FakeClient()
    return acct


def _spread(account_id, *, parent_status=OrderStatus.ACCEPTED, parent_filled=0.0,
            parent_price=None, legs=((LONG_OCC, OrderDirection.BUY, LEG_LONG_BROKER_ID),
                                     (SHORT_OCC, OrderDirection.SELL, LEG_SHORT_BROKER_ID))):
    """Persist a multi-leg option parent + its leg children, exactly as
    ``submit_option_order`` leaves them: children ACCEPTED, filled_qty 0, open_price NULL."""
    parent = TradingOrder(
        account_id=account_id, symbol="ACN", underlying_symbol="ACN", quantity=1,
        side=OrderDirection.BUY, order_type=CoreOrderType.BUY_LIMIT, status=parent_status,
        filled_qty=parent_filled, open_price=parent_price, limit_price=2.30,
        asset_class=AssetClass.OPTION, multiplier=100, option_strategy="bull_call_spread",
        broker_order_id=PARENT_BROKER_ID,
        legs_broker_ids=[bid for _, _, bid in legs],
    )
    parent_id = add_instance(parent, expunge_after_flush=True)

    child_ids = []
    for occ, side, broker_id in legs:
        child = TradingOrder(
            account_id=account_id, symbol=occ, underlying_symbol="ACN", quantity=1,
            side=side,
            order_type=(CoreOrderType.BUY_LIMIT if side == OrderDirection.BUY
                        else CoreOrderType.SELL_LIMIT),
            status=OrderStatus.ACCEPTED, filled_qty=0.0, open_price=None,
            asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=occ,
            parent_order_id=parent_id, broker_order_id=broker_id,
        )
        child_ids.append(add_instance(child, expunge_after_flush=True))
    return parent_id, child_ids


def _snapshot(order_id):
    o = get_instance(TradingOrder, order_id)
    return (o.status, None if o.filled_qty is None else float(o.filled_qty), o.open_price)


# ---------------------------------------------------------------------------
# 1. Both legs filled, at DIFFERENT prices
# ---------------------------------------------------------------------------
def test_both_legs_of_a_filled_spread_record_their_own_price(mock_account_def):
    """The two legs of a vertical NEVER fill at the same price — that difference IS the
    spread. Asserting distinct values is what stops one shared number (the parent's net
    debit, or the first leg's price copied across) from passing."""
    parent_id, (long_id, short_id) = _spread(mock_account_def.id)

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "2.30",
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35"),
              _raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "filled", "1", "2.05")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])

    assert acct.refresh_orders(fetch_all=False) is True

    long_leg = get_instance(TradingOrder, long_id)
    short_leg = get_instance(TradingOrder, short_id)

    assert long_leg.status == OrderStatus.FILLED
    assert float(long_leg.filled_qty) == 1.0
    assert float(long_leg.open_price) == 4.35

    assert short_leg.status == OrderStatus.FILLED
    assert float(short_leg.filled_qty) == 1.0
    assert float(short_leg.open_price) == 2.05

    # Each leg its OWN economics: one shared value cannot satisfy this.
    assert float(long_leg.open_price) != float(short_leg.open_price)
    # ...and neither is the parent's net debit.
    parent = get_instance(TradingOrder, parent_id)
    assert float(parent.open_price) == 2.30
    assert float(long_leg.open_price) != float(parent.open_price)
    assert float(short_leg.open_price) != float(parent.open_price)


def test_the_legs_are_matched_by_broker_id_not_by_position(mock_account_def):
    """The broker is under no obligation to return the legs in submission order. Matching
    by list index would silently swap the long leg's price onto the short leg — the two
    rows would still look plausible, and the structure's P&L would be exactly wrong."""
    parent_id, (long_id, short_id) = _spread(mock_account_def.id)

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "2.30",
        # REVERSED relative to legs_broker_ids / the child rows.
        legs=[_raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "filled", "1", "2.05"),
              _raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    assert float(get_instance(TradingOrder, long_id).open_price) == 4.35
    assert float(get_instance(TradingOrder, short_id).open_price) == 2.05


# ---------------------------------------------------------------------------
# 2. Four-leg condor
# ---------------------------------------------------------------------------
# Broker ids are UUIDs — alpaca-py's Order model parses `id` as one, so anything else
# would never survive a real response.
CONDOR = [
    ("ACN260821P00115000", OrderDirection.BUY, "0c0d0a01-1111-4a11-8a11-000000000001", "buy", "0.42"),
    ("ACN260821P00120000", OrderDirection.SELL, "0c0d0a02-2222-4a22-8a22-000000000002", "sell", "1.18"),
    ("ACN260821C00140000", OrderDirection.SELL, "0c0d0a03-3333-4a33-8a33-000000000003", "sell", "1.31"),
    ("ACN260821C00145000", OrderDirection.BUY, "0c0d0a04-4444-4a44-8a44-000000000004", "buy", "0.55"),
]


def test_a_four_leg_condor_records_four_distinct_leg_fills(mock_account_def):
    """Four legs, four contracts, four prices. The parent carries one net credit and no
    contract at all, so the condor's per-leg economics exist only on these rows."""
    parent_id, child_ids = _spread(
        mock_account_def.id,
        legs=[(occ, side, bid) for occ, side, bid, _, _ in CONDOR],
    )

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "-1.52",
        legs=[_raw_leg(bid, occ, wire_side, "filled", "1", price)
              for occ, _, bid, wire_side, price in CONDOR],
        limit_price="-1.52",
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    prices = []
    for child_id, (occ, _, _, _, price) in zip(child_ids, CONDOR):
        leg = get_instance(TradingOrder, child_id)
        assert leg.contract_symbol == occ
        assert leg.status == OrderStatus.FILLED
        assert float(leg.filled_qty) == 1.0
        assert float(leg.open_price) == float(price)
        prices.append(float(leg.open_price))

    assert len(set(prices)) == 4, f"four legs must keep four prices, got {prices}"


# ---------------------------------------------------------------------------
# 3. Partial fill — one leg filled, one still working
# ---------------------------------------------------------------------------
def test_a_partial_fill_updates_only_the_leg_that_filled(mock_account_def):
    """One leg done, one still working. The working leg must not inherit the fill, and the
    parent must not be recorded as complete — a half-executed spread is not a spread, and
    calling it FILLED would hand the book a position that does not exist."""
    parent_id, (long_id, short_id) = _spread(mock_account_def.id)

    raw = _raw_mleg_parent(
        parent_id, "partially_filled", "0", None,
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35"),
              _raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "accepted", "0", None)],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    long_leg = get_instance(TradingOrder, long_id)
    assert long_leg.status == OrderStatus.FILLED
    assert float(long_leg.filled_qty) == 1.0
    assert float(long_leg.open_price) == 4.35

    short_leg = get_instance(TradingOrder, short_id)
    assert short_leg.status == OrderStatus.ACCEPTED
    assert float(short_leg.filled_qty) == 0.0
    assert short_leg.open_price is None, "an unfilled leg has NO price, not a zero one"

    parent = get_instance(TradingOrder, parent_id)
    assert parent.status == OrderStatus.PARTIALLY_FILLED
    assert parent.status != OrderStatus.FILLED
    assert float(parent.filled_qty) == 0.0


# ---------------------------------------------------------------------------
# 4. A leg the broker did not mention
# ---------------------------------------------------------------------------
def test_a_leg_absent_from_the_broker_response_is_left_exactly_as_it_was(mock_account_def):
    """Silence is not a fill. The broker returning one of two legs says nothing whatsoever
    about the other, so that row must come out byte-for-byte as it went in — same status,
    same (zero) filled_qty, same NULL price. `None` is not `[]`; this is the same
    conflation that made ``get_positions()`` report a fetch failure as a flat book."""
    parent_id, (long_id, short_id) = _spread(mock_account_def.id)
    before = _snapshot(short_id)

    raw = _raw_mleg_parent(
        parent_id, "partially_filled", "0", None,
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    assert get_instance(TradingOrder, long_id).status == OrderStatus.FILLED
    assert _snapshot(short_id) == before
    assert before == (OrderStatus.ACCEPTED, 0.0, None)


def test_a_response_with_no_legs_at_all_touches_no_child(mock_account_def):
    """``legs=None`` (a non-nested response, or a shape we don't recognise) is the same
    silence — and it must not be read as "the whole structure failed to fill" either."""
    parent_id, child_ids = _spread(mock_account_def.id)
    before = [_snapshot(cid) for cid in child_ids]

    raw = _raw_mleg_parent(parent_id, "filled", "1", "2.30", legs=None)
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    assert get_instance(TradingOrder, parent_id).status == OrderStatus.FILLED
    assert [_snapshot(cid) for cid in child_ids] == before


# ---------------------------------------------------------------------------
# 5. Idempotence
# ---------------------------------------------------------------------------
def test_reconciling_twice_changes_nothing_the_second_time(mock_account_def):
    """``refresh_orders`` runs on a schedule; reconciliation is a fixed point, not an
    accumulator."""
    parent_id, child_ids = _spread(mock_account_def.id)

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "2.30",
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35"),
              _raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "filled", "1", "2.05")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])

    assert acct.refresh_orders(fetch_all=False) is True
    after_first = [_snapshot(cid) for cid in child_ids] + [_snapshot(parent_id)]

    assert acct.refresh_orders(fetch_all=False) is True
    after_second = [_snapshot(cid) for cid in child_ids] + [_snapshot(parent_id)]

    assert after_second == after_first
    assert after_first[0][0] == OrderStatus.FILLED     # and it really had reconciled


# ---------------------------------------------------------------------------
# 6. The parent is not collateral damage
# ---------------------------------------------------------------------------
def test_the_parents_own_fill_survives_the_child_updates(mock_account_def):
    """The parent's ``open_price`` is the NET debit of the structure, not any leg's price.
    Reconciling the children must leave it alone."""
    parent_id, _ = _spread(mock_account_def.id)

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "2.30",
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35"),
              _raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "filled", "1", "2.05")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    parent = get_instance(TradingOrder, parent_id)
    assert parent.status == OrderStatus.FILLED
    assert float(parent.filled_qty) == 1.0
    assert float(parent.open_price) == 2.30
    assert parent.contract_symbol is None, "a 2-leg parent still has no single contract"
    assert parent.legs_broker_ids == [LEG_LONG_BROKER_ID, LEG_SHORT_BROKER_ID]


# ---------------------------------------------------------------------------
# 7. A child belonging to a DIFFERENT parent is not swept up
# ---------------------------------------------------------------------------
def test_only_this_parents_children_are_reconciled(mock_account_def):
    """Transaction 660 holds two structures — the opening spread and its closing pair —
    whose children share a transaction_id. Reconciliation is keyed on the parent."""
    parent_id, (long_id, short_id) = _spread(mock_account_def.id)

    other_parent = TradingOrder(
        account_id=mock_account_def.id, symbol="ACN", quantity=1, side=OrderDirection.SELL,
        order_type=CoreOrderType.SELL_LIMIT, status=OrderStatus.ACCEPTED,
        asset_class=AssetClass.OPTION, multiplier=100, option_strategy="close",
        broker_order_id="2e61427f-b535-4820-8d5d-cb1c500136d7",
        legs_broker_ids=["bec7b499", "6313e7c2"],
    )
    other_parent_id = add_instance(other_parent, expunge_after_flush=True)
    stranger = TradingOrder(
        account_id=mock_account_def.id, symbol=LONG_OCC, quantity=1,
        side=OrderDirection.SELL, order_type=CoreOrderType.SELL_LIMIT,
        status=OrderStatus.ACCEPTED, filled_qty=0.0, open_price=None,
        asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=LONG_OCC,
        parent_order_id=other_parent_id, broker_order_id="bec7b499",
    )
    stranger_id = add_instance(stranger, expunge_after_flush=True)
    before = _snapshot(stranger_id)

    raw = _raw_mleg_parent(
        parent_id, "filled", "1", "2.30",
        legs=[_raw_leg(LEG_LONG_BROKER_ID, LONG_OCC, "buy", "filled", "1", "4.35"),
              _raw_leg(LEG_SHORT_BROKER_ID, SHORT_OCC, "sell", "filled", "1", "2.05")],
    )
    acct = _make_alpaca(mock_account_def.id, [raw])
    assert acct.refresh_orders(fetch_all=False) is True

    assert get_instance(TradingOrder, long_id).status == OrderStatus.FILLED
    assert _snapshot(stranger_id) == before
