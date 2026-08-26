"""OPT-L6 — RESERVING_STRATEGIES must match what the BUILDERS actually persist.

``call_butterfly`` — a DEBIT structure — sat in ``RESERVING_STRATEGIES`` while
``OpenCallButterflyAction`` submitted it with no ``option_reserve=``. It was the only one
of the 17 entry builders to do so, and the consequence was account-wide:
``reserved_option_buying_power_detail`` treats "listed as reserving, no reserve recorded"
as UNMEASURABLE, so one open butterfly made ``available_option_buying_power()`` return
``None`` and ``check_option_buying_power(>0)`` return False for ALL EIGHT credit builders,
for every expert on the account, until the order was closed by hand.

It fails CLOSED — no capital was ever at risk — but it disables the entire credit arm and
tells the operator to repair a reserve that should never have existed.

WHY THE EXISTING TEST DID NOT CATCH IT. ``test_the_two_strategy_lists_match_the_branches``
checks list-vs-BRANCH: every name in ``RESERVING_STRATEGIES`` must reach a pricing branch.
``call_butterfly`` does reach one, so that test passed. The relationship that was actually
broken is list-vs-BUILDER, and nothing tested it.

This file tests BOTH directions of that relationship, by RUNNING every entry action and
reading what it persisted rather than by reading the source:

  * a structure the list calls reserving must arrive with a reserve, and
  * a structure the list calls zero-reserve must not smuggle one in.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import ExpertActionType, OptionRight, get_option_action_values


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "reserve_lockstep.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    """Records ``(option_strategy, option_reserve)`` for whatever the builder submits.

    ``submit_option_order`` is overridden rather than stubbed at the broker: the reserve
    is written by ``_OptionEntryAction._submit_option_order`` onto the returned order's
    ``data``, so the double has to hand back an order object it can be read from.
    """

    def __init__(self, spot=100.0):
        self.id = 1
        self._spot = spot
        self.orders = []

    def _as_of_date(self):
        return date(2024, 6, 1)

    def get_balance(self):
        return 1_000_000.0

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=1_000_000.0, equity=1_000_000.0,
                               net_liquidation=1_000_000.0)

    def get_positions(self):
        # The broker's own view of the shares the covered-call / protective-put builders
        # need, so those two run to completion instead of refusing for want of cover.
        return [{"symbol": "AAPL", "qty": 200.0, "asset_class": "us_equity"}]

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(70, 131, 5):
            if option_type == OptionRight.CALL:
                otm = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
            else:
                otm = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, volume=500))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        # A REAL persisted row, because that is where the reserve actually lands:
        # ``_OptionEntryAction._submit_option_order`` writes it with
        # ``get_instance(TradingOrder, order_id)`` + ``update_instance``, so a double
        # returning a bare namespace would silently record nothing and this test would
        # be measuring its own stub instead of the builder.
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import AssetClass, OrderStatus, OrderType
        order_id = add_instance(TradingOrder(
            account_id=self.id, symbol="AAPL", underlying_symbol="AAPL",
            quantity=quantity, side=legs[0].side, order_type=OrderType.BUY_LIMIT,
            status=OrderStatus.PENDING, asset_class=AssetClass.OPTION, multiplier=100,
            option_strategy=option_strategy, limit_price=limit_price, data={}))
        self.orders.append(order_id)
        from ba2_common.core.db import get_instance
        return get_instance(TradingOrder, order_id)

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 1_000_000.0


ENTRY_ACTION_VALUES = sorted(set(get_option_action_values())
                             - {ExpertActionType.CLOSE_OPTION.value})

_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


def _run(action_type, monkeypatch):
    """Execute one entry builder and return ``((strategy, reserve_or_None), result)``.

    ``sizing`` is 2 % rather than the 20 % the sibling harnesses use: at 20 % of a $1m
    account the put-selling structures are (correctly) refused by the real
    assignment-capacity gate before they ever submit, and a builder that never submits
    cannot be checked for what it persists.
    """
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import TradingOrder

    monkeypatch.setattr(TA._OptionEntryAction, "_held_equity_shares", lambda self: 200.0)
    acct = _Acct()
    act = TA.create_action(
        ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(), None, _REC,
        strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40,
        sizing=2.0, min_open_interest=10, max_spread_pct=90.0, min_volume=25,
        wing_width_pct=10.0)
    act.submit_to_broker = True
    result = act.execute()
    if not acct.orders:
        return None, result
    order = get_instance(TradingOrder, acct.orders[-1])
    return (order.option_strategy, (order.data or {}).get("option_reserve")), result


@pytest.mark.parametrize("action_type", ENTRY_ACTION_VALUES)
def test_every_builder_persists_exactly_the_reserve_its_list_membership_promises(
        action_type, monkeypatch):
    """The lockstep the existing list-vs-branch test could not express."""
    submitted, result = _run(action_type, monkeypatch)
    assert submitted is not None, (
        f"{action_type} submitted nothing, so this lockstep cannot be checked: "
        f"{getattr(result, 'message', result)}")
    strategy, reserve = submitted

    reserving = strategy in OptionsAccountInterface.RESERVING_STRATEGIES
    zero = strategy in OptionsAccountInterface.ZERO_RESERVE_STRATEGIES
    assert reserving or zero, (
        f"{action_type} submits option_strategy={strategy!r}, which is in NEITHER "
        f"RESERVING_STRATEGIES nor ZERO_RESERVE_STRATEGIES — option_reserve_required "
        f"raises on it and every gate consulting it is dead")

    if reserving:
        assert reserve is not None and reserve > 0, (
            f"{action_type} submits {strategy!r}, which RESERVING_STRATEGIES says must "
            f"carry a reserve, but persisted option_reserve={reserve!r}. One such open "
            f"order makes available_option_buying_power() UNMEASURABLE account-wide and "
            f"refuses every credit structure for every expert until it is closed by hand.")
    else:
        assert reserve is None, (
            f"{action_type} submits {strategy!r}, listed as ZERO-reserve, yet persisted "
            f"option_reserve={reserve!r} — a debit structure charging the credit budget")


def test_the_call_butterfly_is_a_debit_structure_and_reserves_nothing():
    """Named explicitly: this is the one that was mis-listed, and it is a DEBIT fly."""
    assert "call_butterfly" in OptionsAccountInterface.ZERO_RESERVE_STRATEGIES
    assert "call_butterfly" not in OptionsAccountInterface.RESERVING_STRATEGIES
    assert OptionsAccountInterface.option_reserve_required(
        "call_butterfly", 3, spread_width=5.0, net_credit=1.0) == 0.0


def test_one_open_butterfly_no_longer_blinds_the_whole_account(monkeypatch):
    """The account-wide consequence, measured rather than reasoned about.

    An OPEN butterfly parent with no ``option_reserve`` in its ``data`` used to make the
    reserve pool unmeasurable, which turns every ``check_option_buying_power(>0)`` into a
    refusal for every expert on the account.
    """
    from ba2_common.core import trade_store as ts
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import (
        AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
    )

    with ts.inmem_trades():
        acct = _Acct()
        txn = add_instance(Transaction(symbol="AAPL", quantity=1, open_price=1.0,
                                       side=OrderDirection.BUY,
                                       status=TransactionStatus.OPENED))
        add_instance(TradingOrder(
            account_id=acct.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
            filled_qty=1, side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
            status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
            option_strategy="call_butterfly", transaction_id=txn, data={}))

        pool = acct.reserved_option_buying_power_detail()
        assert pool.is_measurable, (
            f"one open call_butterfly makes the reserve pool unmeasurable "
            f"({pool.unmeasurable}); every credit structure on this account is refused")
        assert acct.check_option_buying_power(1_000.0) is True
