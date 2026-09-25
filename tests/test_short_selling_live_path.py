"""Equity short selling on the LIVE path (plan 2026-09-24, Task S5).

The REAL ``TradeManager`` passes run against the in-memory test DB with the real
``TradeActionEvaluator``, the real risk manager and the real ``AccountInterface.submit_order``.
The broker is a real ``AlpacaAccount`` whose Alpaca ``client`` is a ``MagicMock`` (the pattern of
tests/test_alpaca_short_availability.py): ``client.submit_order`` records the request that WOULD
have gone out and ``client.get_asset`` answers the shortability question. Only the account's
read-side network calls (price, balance, snapshot, positions, order refresh) are canned. Nothing
reaches Alpaca.

Pinned:

* ENTER pass, sell rule + ``enable_sell`` on, flat book: ONE market SELL goes to the broker, on a
  SELL-side Transaction; the RM safeguard stop the entry was sized on sits ABOVE the entry and is
  what ``record_max_loss_stop`` recorded; the resting exit the account staged is a BUY OCO with
  the stop ABOVE and the take-profit BELOW (``_target_exit_spec``; stop-only is a BUY_STOP above);
  the shortability gate was consulted (``get_asset``) for the entry and passed.
* the same pass on a NOT shortable asset sends nothing: the entry is marked ERROR with the
  reason, its staged exit is cancelled, and its WAITING transaction reads "dead" to the
  stranded-entry sweep that releases it.
* the same pass with ``enable_sell`` off sends nothing.
* OPEN-POSITIONS pass (the netting rows): a sell while long closes it through
  ``close_transaction``; a 50% sell reduces it through ``reduce_transaction`` ->
  ``TransactionHelper.adjust_quantity_with_tpsl`` (the remainder re-armed at the transaction's own
  TP/SL); a buy while short covers it through ``close_transaction``; a sell while short is
  refused. None of them opens a position or fetches the asset. A full close follows the real
  deferred-close flow: the resting OCO is cancelled at the broker, and the closing order is
  released by the trigger sweep once the cancel is confirmed.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus, OrderSide
from alpaca.trading.models import Asset

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import (AnalysisUseCase, ExpertActionType, OrderDirection,
                                   OrderRecommendation, OrderStatus, OrderType,
                                   TransactionStatus)

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from tests import factories

SYMBOL = "AAPL"
PRICE = 100.0


# --------------------------------------------------------------------------------------- #
# The broker double: a real AlpacaAccount over a MagicMock client
# --------------------------------------------------------------------------------------- #
def _asset(shortable=True, easy_to_borrow=True):
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=SYMBOL, status=AssetStatus.ACTIVE, tradable=True, marginable=True,
        shortable=shortable, easy_to_borrow=easy_to_borrow, fractionable=True,
        min_order_size=0.001, min_trade_increment=0.001,
        maintenance_margin_requirement=30.0)


class _FakeAlpaca(AlpacaAccount):
    """``AlpacaAccount`` minus the network: the trading client is a MagicMock and the read
    side (price, balance, snapshot, positions, order refresh) answers from canned values.
    Everything that decides what is SENT -- ``submit_order``, the shortability gate in
    ``_submit_order_impl``, ``adjust_tp_sl`` and its exit spec -- is the real code."""

    def __init__(self, account_id, *, asset=None, positions=(), balance=100_000.0):
        # Not super().__init__: that builds a real TradingClient from credential settings.
        self.id = account_id
        self._settings_cache = None
        self.client = MagicMock()
        self._authentication_error = None
        self._asset_cache = {}
        self._margin_info_cache = {}
        self._account_snapshot_cache = None
        self._balance_cache = None
        self._balance_cache_time = 0.0
        self._balance_cache_lock = threading.Lock()
        self._BALANCE_CACHE_TTL = 5.0
        self._balance = balance
        self._positions = list(positions)
        self.client.get_asset.return_value = asset if asset is not None else _asset()
        self.client.get_all_positions.return_value = []
        # The broker-side AVAILABLE quantity a deferred close checks before it is released:
        # everything held, once the resting exit's cancel is through.
        held = {p.symbol: p.qty for p in self._positions}
        self.client.get_open_position.side_effect = (
            lambda symbol: SimpleNamespace(qty=str(held[symbol]),
                                           qty_available=str(held[symbol])))
        self.client.submit_order.side_effect = self._accept
        self._broker_seq = 0

    def _accept(self, request):
        self._broker_seq += 1
        side = "buy" if request.side == OrderSide.BUY else "sell"
        return SimpleNamespace(
            id=f"brk-{self._broker_seq}", symbol=request.symbol, qty=str(request.qty),
            side=side, type="market", status="new", time_in_force="gtc", order_class=None,
            legs=None, filled_qty="0", filled_avg_price=None, created_at=None,
            limit_price=None, stop_price=None)

    # -- canned read side ---------------------------------------------------------------- #
    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {"balance": self._balance, "cash": self._balance, "equity": self._balance}

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance, buying_power=self._balance,
                               margin_multiplier=1.0, long_market_value=0.0,
                               short_market_value=0.0)

    def get_positions(self):
        return list(self._positions)

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        if isinstance(symbol_or_list, (list, tuple, set)):
            return {s: PRICE for s in symbol_or_list}
        return PRICE

    def refresh_orders(self, heuristic_mapping=False, fetch_all=True):
        return True

    # -- what went out ------------------------------------------------------------------- #
    def sent(self):
        """The requests handed to ``client.submit_order``, as (side, qty) pairs."""
        return [(c.args[0].side, float(c.args[0].qty))
                for c in self.client.submit_order.call_args_list]


class _ShortExpert(MarketExpertInterface):
    """A minimal classic-RM expert (the base RM/trading setting definitions)."""

    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "live short-selling path expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


# --------------------------------------------------------------------------------------- #
# Scenario helpers
# --------------------------------------------------------------------------------------- #
def _adjust(action_type, value):
    return {"action_type": action_type.value, "value": value,
            "reference_value": "order_open_price"}


def _seed_ruleset(name, subtype, triggers, actions):
    rs = factories.create_ruleset(name=name, subtype=subtype)
    ea = factories.create_event_action(
        name=f"{name}-rule", subtype=subtype, triggers=triggers,
        actions={f"action_{i}": a for i, a in enumerate(actions)})
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    return rs.id


def _seed_short_entry_ruleset():
    """bearish & confidence>=60 -> sell, stop -8% (adverse), target +10% (favourable)."""
    return _seed_ruleset(
        "short-enter", AnalysisUseCase.ENTER_MARKET,
        {"trigger_0": {"event_type": "bearish"},
         "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 60.0}},
        [{"action_type": ExpertActionType.SELL.value},
         _adjust(ExpertActionType.ADJUST_STOP_LOSS, -8.0),
         _adjust(ExpertActionType.ADJUST_TAKE_PROFIT, 10.0)])


def _expert_settings(*, enable_buy=True, enable_sell=True):
    return {
        "allow_automated_trade_opening": (True, "bool"),
        "allow_automated_trade_modification": (True, "bool"),
        "enable_buy": (enable_buy, "bool"),
        "enable_sell": (enable_sell, "bool"),
        "sizing_mode": ("risk_atr", "str"),
        "risk_per_trade_pct": (8.0, "float"),
        "min_stop_loss_pct": (8.0, "float"),
        "use_atr_stop": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (100.0, "float"),
    }


def _world(*, enable_buy=True, enable_sell=True, enter_ruleset_id=None,
           open_ruleset_id=None):
    acct_def = factories.create_account_definition(provider="Alpaca")
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_ShortExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=enter_ruleset_id, open_positions_ruleset_id=open_ruleset_id)
    expert = _ShortExpert(inst.id)
    expert.save_settings(_expert_settings(enable_buy=enable_buy, enable_sell=enable_sell))
    return acct_def, inst, expert


def _resolver_for(expert, account):
    class _R:
        def get_expert_instance(self, expert_id):
            return expert

        def get_account_instance(self, account_id):
            return account

        def get_account_instance_from_transaction(self, transaction):
            return account
    return _R()


@contextmanager
def _wired(expert, account):
    """Point every instance lookup of the live passes at ``expert`` / ``account``."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver
    from ba2_trade_platform.core.TradeManager import TradeManager

    try:
        prev = get_instance_resolver()
    except Exception:  # noqa: BLE001 -- nothing wired yet
        prev = None
    set_instance_resolver(_resolver_for(expert, account))
    try:
        with patch("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                   return_value=expert), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=(lambda _id: account)), \
             patch.object(TradeManager, "_has_pending_analysis_jobs", return_value=False):
            yield TradeManager()
    finally:
        if prev is not None:
            set_instance_resolver(prev)


def _bearish_rec(inst_id, **kw):
    kw.setdefault("subtype", AnalysisUseCase.ENTER_MARKET)
    return factories.create_recommendation(
        instance_id=inst_id, symbol=SYMBOL, recommended_action=OrderRecommendation.SELL,
        expected_profit_percent=10.0, price_at_date=PRICE, confidence=90.0,
        created_at=datetime.now(timezone.utc), **kw)


def _rows(model, **where):
    from sqlmodel import select
    from ba2_common.core.db import get_db
    with get_db() as session:
        stmt = select(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        rows = session.exec(stmt).all()
        for r in rows:
            session.expunge(r)
        return rows


# --------------------------------------------------------------------------------------- #
# ENTER pass: a short from flat
# --------------------------------------------------------------------------------------- #
def _run_short_entry(*, asset=None, enable_sell=True, recorded=None, monkeypatch=None):
    from ba2_common.core import trade_cycle

    if recorded is not None:
        real_record = trade_cycle.record_max_loss_stop

        def _spy(order, sl):
            recorded.append((order.id, sl))
            return real_record(order, sl)

        monkeypatch.setattr(trade_cycle, "record_max_loss_stop", _spy)

    acct_def, inst, expert = _world(enable_sell=enable_sell,
                                    enter_ruleset_id=_seed_short_entry_ruleset())
    account = _FakeAlpaca(acct_def.id, asset=asset)
    _bearish_rec(inst.id)
    with _wired(expert, account) as tm:
        created = tm.process_expert_recommendations_after_analysis(inst.id, lookback_days=1)
    return account, inst, created


def test_enter_pass_opens_a_short_with_its_stop_above_and_target_below(monkeypatch):
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.position_sizing import max_loss_stop_of

    recorded = []
    account, inst, created = _run_short_entry(recorded=recorded, monkeypatch=monkeypatch)

    # ONE market SELL reached the broker, sized by the risk manager.
    sent = account.sent()
    assert len(sent) == 1, f"exactly one broker order expected, got {sent}"
    side, qty = sent[0]
    assert side == OrderSide.SELL
    # risk_atr: an 8% risk budget over an 8% stop distance -> the whole $100k book at $100.
    assert qty == pytest.approx(1000.0)

    # The shortability gate was consulted for THIS entry, and passed.
    account.client.get_asset.assert_called_once_with(SYMBOL)

    [entry] = [o for o in _rows(TradingOrder, symbol=SYMBOL) if o.depends_on_order is None]
    assert entry.side == OrderDirection.SELL and entry.order_type == OrderType.MARKET
    assert entry.broker_order_id == "brk-1"
    assert [o.id for o in created] == [entry.id]

    txn = _rows(Transaction, id=entry.transaction_id)[0]
    assert txn.side == OrderDirection.SELL, "the short is a SELL-side Transaction"
    assert txn.expert_id == inst.id

    # The safeguard the entry was SIZED on sits ABOVE the entry and is the recorded max-loss stop.
    assert entry.stop_price == pytest.approx(108.0) and entry.stop_price > PRICE
    assert recorded == [(entry.id, pytest.approx(108.0))]
    assert max_loss_stop_of(txn) == pytest.approx(108.0)

    # The ruleset bracket: stop 8% ABOVE, target 10% BELOW.
    assert txn.stop_loss == pytest.approx(108.0) and txn.stop_loss > PRICE
    assert txn.take_profit == pytest.approx(90.0) and txn.take_profit < PRICE

    # The exit the account staged: ONE resting BUY OCO, stop above, limit (TP) below, waiting
    # for the entry to fill.
    exits = [o for o in _rows(TradingOrder, transaction_id=txn.id)
             if o.depends_on_order is not None and o.status not in OrderStatus.get_terminal_statuses()]
    assert len(exits) == 1, exits
    [oco] = exits
    assert oco.order_type == OrderType.OCO and oco.side == OrderDirection.BUY
    assert oco.stop_price == pytest.approx(108.0) and oco.limit_price == pytest.approx(90.0)
    assert oco.status == OrderStatus.WAITING_TRIGGER and oco.depends_on_order == entry.id

    # The exit spec itself (what the OCO is built from, no network): stop above, TP below; and a
    # stop-only short protects with a BUY_STOP above.
    spec = account._target_exit_spec(txn, entry)
    assert spec[0] == OrderType.OCO and spec[1] == pytest.approx(90.0) and spec[2] == pytest.approx(108.0)
    txn.take_profit = None
    stop_only = account._target_exit_spec(txn, entry)
    assert stop_only[0] == OrderType.BUY_STOP and stop_only[2] == pytest.approx(108.0)
    leg = account._build_exit_order(txn, entry, stop_only, quantity=qty, depends_on=entry.id,
                                    trigger_status=OrderStatus.FILLED)
    assert leg.side == OrderDirection.BUY and leg.stop_price > PRICE


def test_a_non_shortable_asset_refuses_the_short_before_the_broker():
    from ba2_common.core.models import TradingOrder, Transaction

    account, _inst, created = _run_short_entry(asset=_asset(shortable=False))

    account.client.get_asset.assert_called_once_with(SYMBOL)
    account.client.submit_order.assert_not_called()
    assert created == []
    [entry] = [o for o in _rows(TradingOrder, symbol=SYMBOL) if o.depends_on_order is None]
    assert entry.side == OrderDirection.SELL and not entry.broker_order_id
    assert entry.status == OrderStatus.ERROR
    assert "shortable=False" in (entry.comment or "")
    # Every exit staged for it is cancelled with it (the trigger sweep syncs an ERROR parent).
    legs = [o for o in _rows(TradingOrder, symbol=SYMBOL) if o.depends_on_order == entry.id]
    assert legs and all(o.status == OrderStatus.CANCELED for o in legs), legs
    # An ERROR entry is a broker-layer refusal, which the funded loop deliberately does not
    # compensate in-pass (_fail_unsent_entry only touches unsent statuses). Its WAITING
    # transaction is released by the stranded-entry sweep, which reads it as "dead" -- the same
    # path as any other submit the broker layer refused.
    from ba2_trade_platform.core.TradeManager import classify_waiting_entry
    txn = _rows(Transaction, id=entry.transaction_id)[0]
    assert txn.side == OrderDirection.SELL and txn.status == TransactionStatus.WAITING
    assert classify_waiting_entry([entry]) == "dead"


def test_a_sell_from_flat_with_enable_sell_off_is_refused():
    from ba2_common.core.models import TradingOrder, Transaction

    account, _inst, created = _run_short_entry(enable_sell=False)

    account.client.submit_order.assert_not_called()
    account.client.get_asset.assert_not_called()
    assert created == []
    assert _rows(Transaction, symbol=SYMBOL) == []
    assert all(o.side != OrderDirection.SELL or not o.broker_order_id
               for o in _rows(TradingOrder, symbol=SYMBOL))


# --------------------------------------------------------------------------------------- #
# OPEN-POSITIONS pass: the netting rows
# --------------------------------------------------------------------------------------- #
HELD = 100.0


def _seed_exit_ruleset(field, action, percent=None):
    """``<field>`` -> ``<action>`` (optionally a close percent), as an open_positions rule."""
    config = {"action_type": action.value}
    if percent is not None:
        config["value"] = percent
    return _seed_ruleset(f"{action.value}-on-{field}", AnalysisUseCase.OPEN_POSITIONS,
                         {"trigger_0": {"event_type": field}}, [config])


def _open_position(inst_id, account_id, side):
    """An OPENED transaction of HELD shares on ``side``, its FILLED entry, and its resting
    OCO exit (stop 8% adverse, target 10% favourable)."""
    is_long = side == OrderDirection.BUY
    sl, tp = (92.0, 110.0) if is_long else (108.0, 90.0)
    txn = factories.create_transaction(
        symbol=SYMBOL, quantity=HELD, side=side, status=TransactionStatus.OPENED,
        open_price=PRICE, expert_id=inst_id, stop_loss=sl, take_profit=tp)
    entry = factories.create_trading_order(
        account_id, symbol=SYMBOL, quantity=HELD, side=side, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn.id, filled_qty=HELD, open_price=PRICE,
        broker_order_id="brk-entry", created_at=datetime.now(timezone.utc))
    exit_side = OrderDirection.SELL if is_long else OrderDirection.BUY
    oco = factories.create_trading_order(
        account_id, symbol=SYMBOL, quantity=HELD, side=exit_side, order_type=OrderType.OCO,
        status=OrderStatus.NEW, transaction_id=txn.id, limit_price=tp, stop_price=sl,
        depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED,
        broker_order_id="brk-oco", created_at=datetime.now(timezone.utc))
    return txn, entry, oco


def _run_exit(*, side, field, action, percent=None, enable_buy=True, enable_sell=True):
    rec_action = OrderRecommendation.SELL if field == "bearish" else OrderRecommendation.BUY
    acct_def, inst, expert = _world(
        enable_buy=enable_buy, enable_sell=enable_sell,
        open_ruleset_id=_seed_exit_ruleset(field, action, percent))
    signed = HELD if side == OrderDirection.BUY else -HELD
    account = _FakeAlpaca(acct_def.id,
                          positions=[SimpleNamespace(symbol=SYMBOL, qty=signed)])
    txn, entry, oco = _open_position(inst.id, acct_def.id, side)
    factories.create_recommendation(
        instance_id=inst.id, symbol=SYMBOL, recommended_action=rec_action,
        expected_profit_percent=5.0, price_at_date=PRICE, confidence=90.0,
        subtype=AnalysisUseCase.OPEN_POSITIONS, created_at=datetime.now(timezone.utc))

    calls = {"close_transaction": [], "reduce_transaction": []}
    real_close, real_reduce = account.close_transaction, account.reduce_transaction

    def _close(transaction_id):
        calls["close_transaction"].append(transaction_id)
        return real_close(transaction_id)

    def _reduce(transaction_id, quantity):
        calls["reduce_transaction"].append((transaction_id, quantity))
        return real_reduce(transaction_id, quantity)

    account.close_transaction, account.reduce_transaction = _close, _reduce
    with _wired(expert, account) as tm:
        results = tm.process_open_positions_recommendations(inst.id, lookback_days=1)
    return account, expert, txn, entry, oco, calls, results


def _confirm_cancel_and_release(expert, account, oco):
    """What the next broker refresh does: the OCO's cancel is confirmed, and the trigger sweep
    submits the deferred close that was waiting on it."""
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import TradingOrder
    row = get_instance(TradingOrder, oco.id)
    row.status = OrderStatus.CANCELED
    update_instance(row)
    with _wired(expert, account) as tm:
        tm._check_all_waiting_trigger_orders()


def _deferred_close(txn, oco, side):
    """The close order ``close_transaction`` staged: a MARKET order on the SAME transaction,
    for exactly what is held, waiting for the resting OCO's cancel (a broker holds the shares
    under it)."""
    from ba2_common.core.models import TradingOrder
    [close] = [o for o in _rows(TradingOrder, transaction_id=txn.id)
               if o.order_type == OrderType.MARKET and o.id not in (oco.id,)
               and o.status != OrderStatus.FILLED]
    assert close.side == side and close.quantity == pytest.approx(HELD)
    assert close.depends_on_order == oco.id
    assert close.depends_order_status_trigger == OrderStatus.CANCELED
    return close


def _no_new_position(txn):
    from ba2_common.core.models import Transaction
    rows = _rows(Transaction, symbol=SYMBOL)
    assert [t.id for t in rows] == [txn.id], f"a new position was opened: {rows}"


def test_a_sell_while_long_closes_it_through_close_transaction():
    account, expert, txn, _entry, oco, calls, results = _run_exit(
        side=OrderDirection.BUY, field="bearish", action=ExpertActionType.SELL)

    assert calls == {"close_transaction": [txn.id], "reduce_transaction": []}
    [sell] = [r for r in results if r["action_type"] == ExpertActionType.SELL]
    assert sell["success"] is True and sell["data"]["closed_transaction_ids"] == [txn.id]
    # close_transaction cancelled the resting OCO at the broker and staged the close behind it.
    account.client.cancel_order_by_id.assert_called_once_with("brk-oco")
    _deferred_close(txn, oco, OrderDirection.SELL)
    account.client.submit_order.assert_not_called()

    _confirm_cancel_and_release(expert, account, oco)
    # The close is a SELL of exactly what is held, sent as a CLOSING order: the shortability
    # gate never looks at it, and nothing flips.
    assert account.sent() == [(OrderSide.SELL, HELD)]
    account.client.get_asset.assert_not_called()
    _no_new_position(txn)


def test_a_50pct_sell_while_long_reduces_it_through_the_trim_helper(monkeypatch):
    from ba2_common.core.TransactionHelper import TransactionHelper

    trims = []

    def _trim(account, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
        trims.append({"account": account, "transaction_id": transaction.id,
                      "held": transaction.quantity, "qty_change": qty_change,
                      "tp_price": tp_price, "sl_price": sl_price, "expert_id": expert_id,
                      "take_profit": transaction.take_profit,
                      "stop_loss": transaction.stop_loss})
        return {"success": True, "message": "trimmed", "orders_created": [901, 902, 903],
                "orders_canceled": [transaction.id]}

    monkeypatch.setattr(TransactionHelper, "adjust_quantity_with_tpsl", staticmethod(_trim))
    account, _expert, txn, _entry, _oco, calls, results = _run_exit(
        side=OrderDirection.BUY, field="bearish", action=ExpertActionType.SELL, percent=50)

    assert calls == {"close_transaction": [], "reduce_transaction": [(txn.id, 50.0)]}
    [trim] = trims
    assert trim["account"] is account and trim["transaction_id"] == txn.id
    assert trim["qty_change"] == pytest.approx(-50.0), "sells half of the 100 held"
    assert trim["expert_id"] == txn.expert_id
    # The helper contract: tp_price/sl_price None -> the remaining 50 are re-armed at the
    # transaction's OWN take-profit and stop (a long: target 110 above, stop 92 below).
    assert trim["tp_price"] is None and trim["sl_price"] is None
    assert trim["held"] + trim["qty_change"] == pytest.approx(50.0)
    assert (trim["take_profit"], trim["stop_loss"]) == (pytest.approx(110.0), pytest.approx(92.0))
    [sell] = [r for r in results if r["action_type"] == ExpertActionType.SELL]
    assert sell["success"] is True and sell["data"]["quantity"] == pytest.approx(50.0)
    assert sell["data"]["close_order_ids"] == [901, 902, 903]
    # Nothing was sent directly: the trim helper owns the sequencing.
    account.client.submit_order.assert_not_called()
    account.client.get_asset.assert_not_called()
    _no_new_position(txn)


def test_a_buy_while_short_covers_it_through_close_transaction():
    account, expert, txn, _entry, oco, calls, results = _run_exit(
        side=OrderDirection.SELL, field="bullish", action=ExpertActionType.BUY)

    assert calls == {"close_transaction": [txn.id], "reduce_transaction": []}
    [buy] = [r for r in results if r["action_type"] == ExpertActionType.BUY]
    assert buy["success"] is True and buy["data"]["closed_transaction_ids"] == [txn.id]
    account.client.cancel_order_by_id.assert_called_once_with("brk-oco")
    _deferred_close(txn, oco, OrderDirection.BUY)

    _confirm_cancel_and_release(expert, account, oco)
    assert account.sent() == [(OrderSide.BUY, HELD)], "bought back exactly the short, no flip"
    account.client.get_asset.assert_not_called()
    _no_new_position(txn)


def test_a_sell_while_short_is_refused_and_sends_nothing():
    """The netting rule's other half: a sell never adds to this expert's short."""
    account, _expert, txn, _entry, _oco, calls, results = _run_exit(
        side=OrderDirection.SELL, field="bearish", action=ExpertActionType.SELL)

    assert calls == {"close_transaction": [], "reduce_transaction": []}
    [sell] = [r for r in results if r["action_type"] == ExpertActionType.SELL]
    assert sell["success"] is False and "already short" in sell["message"]
    account.client.submit_order.assert_not_called()
    _no_new_position(txn)
