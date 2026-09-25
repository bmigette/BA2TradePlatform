"""Equity short selling: the backtest engine and the LIVE path agree (plan 2026-09-24, Task S5).

Both arms are driven end to end, from the SAME seeded rulesets (``seed_ruleset_from_tree`` /
``seed_exit_ruleset_from_rules``), the SAME expert settings and the SAME recommendation:

* BACKTEST: the real ``DailyBacktestEngine`` over a ``BacktestAccount`` (the harness of
  test_short_selling_engine.py).
* LIVE: the real ``TradeManager`` passes (``process_expert_recommendations_after_analysis`` /
  ``process_open_positions_recommendations``) over a real ``AlpacaAccount`` whose Alpaca client
  is a MagicMock (nothing reaches a broker), in its own throwaway SQLite trading DB. The
  recommendation row is written by the engine's own ``_recommendation_to_expert_recommendation``
  from the same ``Recommendation`` value the backtest expert emits.

Compared:

* the ENTRY: side, RM quantity, the safeguard stop the entry was sized on (= the recorded
  max-loss stop), and the ruleset stop and target -- a short's stop ABOVE, its target BELOW;
* the NETTING decision, recorded at the account seam both paths share: a sell while long calls
  ``close_transaction`` on the long, a 50% sell calls ``reduce_transaction`` for half of it, a
  buy while short calls ``close_transaction`` on the short. Each account then does its own
  mechanics (the simulator fills; Alpaca stages a deferred close / the trim helper);
* a sell from flat with ``enable_sell`` off opens nothing in either (live: nothing sent, no
  transaction, the asset never fetched).

The fixture fills at the decision price (next open == signal close), so the entry prices are
comparable at all: live re-bases a pending stop to the actual fill (``rebase_price_to_fill``),
the backtest keeps the pre-fill reference -- a known seam difference for longs and shorts alike,
not exercised here.

Run from the backend dir:
    python -m pytest tests/backtest/test_short_selling_bt_live_parity.py -q
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ba2_common.core.types import OrderRecommendation, Recommendation

from tests.backtest.test_max_loss_stop_engine import CFG, _store_mode
from tests.backtest.test_short_selling_engine import (
    BRACKET, DAY1, DRIFT, STOPPED, _SignalStubExpert, _run, _signal_rule, _signal_rule_pct)

BUY, SELL = OrderRecommendation.BUY, OrderRecommendation.SELL
SYMBOL = "AAPL"
PRICE = 100.0
SETTINGS = {
    "allow_automated_trade_opening": (True, "bool"),
    "allow_automated_trade_modification": (True, "bool"),
    "enable_buy": (True, "bool"),
    "enable_sell": (True, "bool"),
    "sizing_mode": ("risk_atr", "str"),
    "risk_per_trade_pct": (8.0, "float"),
    "min_stop_loss_pct": (8.0, "float"),
    "use_atr_stop": (False, "bool"),
}


# --------------------------------------------------------------------------------------- #
# The live broker double: a real AlpacaAccount over a MagicMock client
# --------------------------------------------------------------------------------------- #
def _alpaca_cls():
    from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus, OrderSide
    from alpaca.trading.models import Asset
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    class _FakeAlpaca(AlpacaAccount):
        """Everything that decides what is SENT is the real code; only the read side
        (price, balance, snapshot, positions, order refresh) is canned."""

        def __init__(self, account_id, *, positions=()):
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
            self._balance = CFG["starting_cash"]
            self._positions = list(positions)
            self.client.get_asset.return_value = Asset(
                id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
                symbol=SYMBOL, status=AssetStatus.ACTIVE, tradable=True, marginable=True,
                shortable=True, easy_to_borrow=True, fractionable=True,
                min_order_size=0.001, min_trade_increment=0.001,
                maintenance_margin_requirement=30.0)
            self.client.get_all_positions.return_value = []
            self.client.submit_order.side_effect = lambda request: SimpleNamespace(
                id=f"brk-{self.client.submit_order.call_count}", symbol=request.symbol,
                qty=str(request.qty), side="buy" if request.side == OrderSide.BUY else "sell",
                type="market", status="new", time_in_force="gtc", order_class=None, legs=None,
                filled_qty="0", filled_avg_price=None, created_at=None, limit_price=None,
                stop_price=None)

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

    return _FakeAlpaca


class _LiveExpert(_SignalStubExpert):
    """The backtest stub expert class; live it is only read for its settings."""

    def __init__(self, id: int):
        super().__init__(id, price_source=None, signals={})


@contextmanager
def _live_world(monkeypatch, run_id, *, enter_id=None, open_id=None, positions=(),
                settings=None):
    """A throwaway SQLite trading DB (sqlite store mode: the live passes query rows directly)
    holding the account + expert, with every live instance lookup pointed at them."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core import trade_store
    from ba2_trade_platform.core.TradeManager import TradeManager

    _store_mode(monkeypatch, "0")
    resolver = wire_backtest_seams()
    with backtest_trading_db(f"short-parity-live-{run_id}"):
        assert trade_store.inmem_trades_active() is False
        seed_account_definition(run_id, CFG, provider="Alpaca")
        ids = {"enter": enter_id() if enter_id else None, "open": open_id() if open_id else None}
        seed_expert_instance(account_id=run_id, expert_class_name="_SignalStubExpert",
                             enter_market_ruleset_id=ids["enter"] or 0,
                             open_positions_ruleset_id=ids["open"], instance_id=run_id)
        account = _alpaca_cls()(run_id, positions=positions)
        expert = _LiveExpert(run_id)
        expert.save_settings({**SETTINGS, **(settings or {})})
        resolver.register_account(run_id, account)
        resolver.register_expert(run_id, expert)
        with patch("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                   return_value=expert), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=(lambda _id: account)), \
             patch.object(TradeManager, "_has_pending_analysis_jobs", return_value=False):
            yield SimpleNamespace(tm=TradeManager(), account=account, expert=expert, id=run_id)


def _write_recommendation(expert_id, signal, subtype):
    """The row the engine would write for this bar (its own helper), stamped now."""
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    rec = Recommendation(signal=signal, confidence=80.0, current_price=PRICE, details="stub",
                         expected_profit_percent=10.0)
    return _recommendation_to_expert_recommendation(
        rec, expert_instance_id=expert_id, symbol=SYMBOL, as_of=datetime.now(timezone.utc),
        subtype=subtype)


# --------------------------------------------------------------------------------------- #
# The entry
# --------------------------------------------------------------------------------------- #
def _live_short_entry(monkeypatch, run_id, settings=None):
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.position_sizing import max_loss_stop_of
    from ba2_common.core.types import AnalysisUseCase
    from sqlmodel import select
    from ba2_common.core.db import get_db

    enter = lambda: seed_ruleset_from_tree(None, name=f"short-enter-live-{run_id}",  # noqa: E731
                                           enable_short=True, entry_actions=BRACKET)
    with _live_world(monkeypatch, run_id, enter_id=enter, settings=settings) as w:
        _write_recommendation(w.id, SELL, AnalysisUseCase.ENTER_MARKET)
        w.tm.process_expert_recommendations_after_analysis(w.id, lookback_days=1)
        if settings:  # a refused entry: report what (if anything) reached the broker
            return {"sent": w.account.client.submit_order.call_count,
                    "transactions": len(get_db_rows(Transaction)),
                    "asset_checked": w.account.client.get_asset.call_count}
        with get_db() as session:
            orders = session.exec(select(TradingOrder).where(TradingOrder.symbol == SYMBOL)).all()
            for o in orders:
                session.expunge(o)
        [entry] = [o for o in orders if o.depends_on_order is None]
        [exit_leg] = [o for o in orders if o.depends_on_order == entry.id]
        txn = get_instance(Transaction, entry.transaction_id)
        [request] = [c.args[0] for c in w.account.client.submit_order.call_args_list]
        w.account.client.get_asset.assert_called_once_with(SYMBOL)
        return {
            "side": txn.side.value, "entry_side": entry.side.value,
            "quantity": float(request.qty), "safeguard": entry.stop_price,
            "stop_loss": txn.stop_loss, "take_profit": txn.take_profit,
            "max_loss_stop": max_loss_stop_of(txn),
            "exit_leg": (exit_leg.side, exit_leg.stop_price, exit_leg.limit_price),
            "broker_side": request.side.value,
        }


def get_db_rows(model):
    from sqlmodel import select
    from ba2_common.core.db import get_db
    with get_db() as session:
        return session.exec(select(model)).all()


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["bt-inmem-store", "bt-sqlite"])
def test_a_short_entry_is_the_same_short_in_the_backtest_and_live(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    run_id = 910 + (100 if inmem == "0" else 0)
    _outcome, [bt] = _run(STOPPED, {DAY1: SELL}, run_id=run_id, inmem=inmem, enable_short=True)
    live = _live_short_entry(monkeypatch, run_id + 1)

    # Same side: a SELL-side transaction opened by a SELL, sent to the broker as a sell.
    assert bt["side"] == live["side"] == "SELL"
    assert live["entry_side"] == "SELL" and live["broker_side"] == "sell"
    # Same size: the RM's risk_atr sizing (8% of $100k over an 8% stop distance = 1,000 shares)
    # capped at the default 10% per-instrument ceiling ($10k at $100).
    assert bt["quantity"] == live["quantity"] == pytest.approx(100.0)
    # Same protection at entry: stop 8% ABOVE, target 10% BELOW, and the safeguard the entry
    # was sized on recorded as its max-loss stop.
    assert bt["stop_loss"] == pytest.approx(live["stop_loss"]) == pytest.approx(108.0)
    assert bt["take_profit"] == pytest.approx(live["take_profit"]) == pytest.approx(90.0)
    assert bt["max_loss_stop"] == pytest.approx(live["max_loss_stop"]) == pytest.approx(108.0)
    assert live["safeguard"] == pytest.approx(108.0)
    # Live stages ONE resting BUY exit carrying exactly the backtest's two levels.
    leg_side, leg_stop, leg_limit = live["exit_leg"]
    assert leg_side.value == "BUY"
    assert (leg_stop, leg_limit) == (pytest.approx(bt["stop_loss"]), pytest.approx(bt["take_profit"]))


def test_a_sell_from_flat_with_enable_sell_off_opens_nothing_in_both(monkeypatch):
    off = {"enable_sell": (False, "bool")}
    _store_mode(monkeypatch, "0")
    outcome, txns = _run(STOPPED, {DAY1: SELL}, run_id=960, inmem="0", enable_short=True,
                         extra_settings=off)
    assert txns == [] and outcome["trades"] == []
    assert _live_short_entry(monkeypatch, 961, settings=off) == {
        "sent": 0, "transactions": 0, "asset_checked": 0}


# --------------------------------------------------------------------------------------- #
# The netting rows
# --------------------------------------------------------------------------------------- #
def _spy_account_seam(monkeypatch, cls, calls):
    """Record every close_transaction / reduce_transaction call on ``cls`` (then run it)."""
    real_close, real_reduce = cls.close_transaction, cls.reduce_transaction

    def close(self, transaction_id):
        calls.append(("close_transaction", transaction_id, None))
        return real_close(self, transaction_id)

    def reduce(self, transaction_id, quantity):
        calls.append(("reduce_transaction", transaction_id, float(quantity)))
        return real_reduce(self, transaction_id, quantity)

    monkeypatch.setattr(cls, "close_transaction", close)
    monkeypatch.setattr(cls, "reduce_transaction", reduce)


def _bt_netting(monkeypatch, signals, exit_rule, run_id):
    from app.services.backtest.backtest_account import BacktestAccount
    calls = []
    _spy_account_seam(monkeypatch, BacktestAccount, calls)
    _store_mode(monkeypatch, "0")
    outcome, txns = _run(DRIFT, signals, run_id=run_id, inmem="0", enable_short=True,
                         exit_rules=[exit_rule])
    return calls, outcome, txns


def _live_netting(monkeypatch, run_id, *, side, held, signal, exit_rule, trim_stub=None):
    """The live open-positions pass over one OPENED position of ``held`` shares on ``side``."""
    from app.services.backtest.default_rulesets import seed_exit_ruleset_from_rules
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.types import (AnalysisUseCase, OrderDirection, OrderStatus, OrderType,
                                       TransactionStatus)

    signed = held if side == OrderDirection.BUY else -held
    calls = []
    with _live_world(monkeypatch, run_id,
                     open_id=lambda: seed_exit_ruleset_from_rules(
                         [exit_rule], name=f"short-open-live-{run_id}"),
                     positions=[SimpleNamespace(symbol=SYMBOL, qty=signed)]) as w:
        _spy_account_seam(monkeypatch, type(w.account), calls)
        if trim_stub is not None:
            from ba2_common.core.TransactionHelper import TransactionHelper
            monkeypatch.setattr(TransactionHelper, "adjust_quantity_with_tpsl",
                                staticmethod(trim_stub))
        now = datetime.now(timezone.utc)
        txn_id = add_instance(Transaction(
            symbol=SYMBOL, quantity=held, side=side, status=TransactionStatus.OPENED,
            open_price=PRICE, open_date=now, expert_id=w.id, created_at=now))
        add_instance(TradingOrder(
            account_id=w.id, symbol=SYMBOL, quantity=held, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
            filled_qty=held, open_price=PRICE, broker_order_id="brk-entry", created_at=now))
        _write_recommendation(w.id, signal, AnalysisUseCase.OPEN_POSITIONS)
        w.tm.process_open_positions_recommendations(w.id, lookback_days=1)
        return calls, txn_id


def _as_decision(calls, txn_id):
    """The netting decision, independent of each path's row ids."""
    return [(name, "the position" if tid == txn_id else tid, qty) for name, tid, qty in calls]


def test_a_sell_while_long_closes_it_in_both(monkeypatch):
    from ba2_common.core.types import OrderDirection
    rule = _signal_rule("bearish", "sell")
    bt_calls, _outcome, [bt] = _bt_netting(
        monkeypatch, {DAY1: BUY, date(2024, 1, 4): SELL}, rule, 930)
    assert bt["side"] == "BUY" and bt["status"] == "CLOSED"
    bt_txn = _bt_txn_id(bt_calls)
    live_calls, live_txn = _live_netting(monkeypatch, 931, side=OrderDirection.BUY,
                                         held=bt["quantity"], signal=SELL, exit_rule=rule)
    assert _as_decision(bt_calls, bt_txn) == _as_decision(live_calls, live_txn) == [
        ("close_transaction", "the position", None)]


def test_a_50pct_sell_while_long_reduces_it_by_half_in_both(monkeypatch):
    from ba2_common.core.types import OrderDirection
    rule = _signal_rule_pct("bearish", "sell", 50)
    bt_calls, outcome, [bt] = _bt_netting(
        monkeypatch, {DAY1: BUY, date(2024, 1, 4): SELL}, rule, 940)
    assert bt["side"] == "BUY" and bt["status"] == "OPENED"
    bt_txn = _bt_txn_id(bt_calls)
    # What the long held before the trim: its entry fill (the transaction now shows the rest).
    [held] = [filled for side, kind, status, _q, filled, *_r, top in outcome["orders"]
              if side == "BUY" and kind == "market" and status == "filled" and top]
    trims = []

    def trim(account, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
        trims.append(qty_change)
        return {"success": True, "message": "trimmed", "orders_created": [1],
                "orders_canceled": []}

    live_calls, live_txn = _live_netting(monkeypatch, 941, side=OrderDirection.BUY,
                                         held=held, signal=SELL, exit_rule=rule,
                                         trim_stub=trim)
    half = held / 2
    assert bt["quantity"] == pytest.approx(held - half)
    assert _as_decision(bt_calls, bt_txn) == _as_decision(live_calls, live_txn) == [
        ("reduce_transaction", "the position", pytest.approx(half))]
    assert trims == [pytest.approx(-half)], "live hands the trim helper the same half"


def test_a_buy_while_short_covers_it_in_both(monkeypatch):
    from ba2_common.core.types import OrderDirection
    rule = _signal_rule("bullish", "buy")
    bt_calls, _outcome, [bt] = _bt_netting(
        monkeypatch, {DAY1: SELL, date(2024, 1, 5): BUY}, rule, 950)
    assert bt["side"] == "SELL" and bt["status"] == "CLOSED"
    bt_txn = _bt_txn_id(bt_calls)
    live_calls, live_txn = _live_netting(monkeypatch, 951, side=OrderDirection.SELL,
                                         held=bt["quantity"], signal=BUY, exit_rule=rule)
    assert _as_decision(bt_calls, bt_txn) == _as_decision(live_calls, live_txn) == [
        ("close_transaction", "the position", None)]


def _bt_txn_id(calls):
    """The backtest's only position is the one its netting call acted on."""
    ids = {tid for _name, tid, _qty in calls}
    assert len(ids) == 1, calls
    return ids.pop()
