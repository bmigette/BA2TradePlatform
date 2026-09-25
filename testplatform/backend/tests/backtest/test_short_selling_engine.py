"""Equity short selling (plan 2026-09-24, Task S1) in the REAL backtest engine.

  * A short opens from flat: ``enable_short`` (-> the expert's ``enable_sell``) plus a bearish
    entry rule whose action is ``sell``. The entry bracket's ``adjust_stop_loss -8`` lands ABOVE
    the entry and ``adjust_take_profit +10`` BELOW it, the transaction is SELL-side, and its
    recorded max-loss stop is the RM safeguard above the entry.
  * It is covered by its stop (price rallies through it) or by the reverse-signal close rule
    (a bullish recommendation fires the open-positions ``close``).
  * With ``enable_sell`` off the same bearish rule opens nothing.
  * No-impact: a long-only run (buy entries, a ``close`` exit rule) gives byte-identical orders,
    trades and equity with SellAction/BuyAction as they are now and with the pre-short-selling
    ``execute`` bodies patched back in. It exercises buys and closes and NO ``sell`` action: no
    stored strategy has one, and a sell against a long now closes it (operator 2026-09-25).
  * A ``sell``/``buy`` exit with a close percent reduces the position in the simulator
    (``BacktestAccount.reduce_transaction``): the transaction stays OPENED with the remainder,
    still protected by its stop.

Both trade-store modes; the SQLite mode forces the leaked in-memory flag off and asserts it.

Run from the backend dir:
    python -m pytest tests/backtest/test_short_selling_engine.py -v
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.position_sizing import max_loss_stop_of
from ba2_common.core.types import OrderRecommendation, Recommendation

from tests.backtest.test_max_loss_stop_engine import CFG, _store_mode

BUY, SELL, HOLD = OrderRecommendation.BUY, OrderRecommendation.SELL, OrderRecommendation.HOLD


class _SignalStubExpert(MarketExpertInterface):
    """Emits ``signals[day]`` (HOLD on any other day) for both the enter and the open-positions
    analysis. No providers."""

    def __init__(self, id: int, price_source, signals):
        super().__init__(id)
        self._ps = price_source
        self._signals = signals

    @classmethod
    def description(cls) -> str:
        return "Stub expert for the short-selling engine tests."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        close = float(self._ps.close_at("AAPL", as_of))
        day = as_of.date() if hasattr(as_of, "date") else as_of
        signal = self._signals.get(day, HOLD)
        return Recommendation(
            signal=signal,
            confidence=50.0 if signal == HOLD else 80.0,
            current_price=close,
            details="stub",
            expected_profit_percent=0.0 if signal == HOLD else 10.0,
        )


def _adjust(action_type, value):
    return {"id": f"{action_type}-{value:g}", "action_type": action_type,
            "reference_value": "order_open_price", "action_value": value}


BRACKET = [_adjust("adjust_stop_loss", -8.0), _adjust("adjust_take_profit", 10.0)]


def _signal_close(field):
    return {"id": f"close-on-{field}", "name": f"close-on-{field}",
            "conditions": {"type": "AND", "conditions": [
                {"id": f"{field}-flag", "field": field, "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}


def _run(bars, signals, *, run_id, inmem, enable_short, entry_actions=BRACKET,
         exit_rules=None, extra_settings=None):
    """Full engine.run(); returns the outcome and every entry transaction's protective state,
    read BEFORE teardown."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_exit_ruleset_from_rules, seed_ruleset_from_tree)
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"short-selling-{run_id}")
    ctx.__enter__()
    try:
        from ba2_common.core import trade_store
        assert trade_store.inmem_trades_active() == (inmem == "1"), "store mode is not the one asked for"
        seed_account_definition(account_id, CFG)
        enter_id = seed_ruleset_from_tree(None, name=f"short-enter-{run_id}",
                                          enable_short=enable_short, entry_actions=entry_actions)
        open_id = (seed_exit_ruleset_from_rules(exit_rules, name=f"short-open-{run_id}")
                   if exit_rules else None)
        seed_expert_instance(account_id=account_id, expert_class_name="_SignalStubExpert",
                             enter_market_ruleset_id=enter_id, open_positions_ruleset_id=open_id,
                             instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in bars])
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = _SignalStubExpert(expert_id, ps, signals)
        settings = {
            "allow_automated_trade_opening": (True, "bool"),
            "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"),
            # What deploy_parity derives from the run's enable_short.
            "enable_sell": (bool(enable_short), "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
            **(extra_settings or {}),
        }
        expert.save_settings(settings)
        resolver.register_expert(expert_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, enter_id)], price_source=ps,
            config={"start_date": datetime.combine(bars[0][0], datetime.min.time()),
                    "end_date": datetime.combine(bars[-1][0], datetime.min.time()),
                    "enabled_instruments": ["AAPL"], "seed": 42, "enable_short": enable_short},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()

        entries = sorted((o for o in account.get_orders()
                          if o.symbol == "AAPL" and o.depends_on_order is None
                          and o.transaction_id is not None),
                         key=lambda o: o.id)
        txns = {}
        for o in entries:
            txn = get_instance(Transaction, o.transaction_id)
            txns.setdefault(txn.id, {
                "side": txn.side.value, "open_price": txn.open_price, "close_price": txn.close_price,
                "stop_loss": txn.stop_loss, "take_profit": txn.take_profit,
                "max_loss_stop": max_loss_stop_of(txn), "status": txn.status.value,
                "quantity": txn.quantity})
        orders = sorted(
            (o.side.value, o.order_type.value, o.status.value, o.quantity, o.filled_qty,
             o.open_price, o.stop_price, o.limit_price, o.depends_on_order is None)
            for o in account.get_orders())
        outcome = {
            "orders": orders,
            "trades": account.get_round_trip_trades(),
            "equity": list(account.get_balance_history()),
        }
        return outcome, list(txns.values())
    finally:
        ctx.__exit__(None, None, None)


# Bearish on 2024-01-02 (close 100): the short fills at the next open (100). The safeguard and the
# ruleset stop are both 8% ABOVE (108); the target 10% BELOW (90).
DAY1 = date(2024, 1, 2)

# The price rallies through the stop on the 3rd bar.
STOPPED = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 103, 99, 102),
    (date(2024, 1, 4), 102, 112, 101, 111),
    (date(2024, 1, 5), 111, 113, 110, 112),
]

# The price drifts down without reaching either leg; a bullish signal on 2024-01-05 fires the
# reverse-signal close, which covers at the next open (95).
DRIFT = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 101, 97, 98),
    (date(2024, 1, 4), 98, 99, 95, 96),
    (date(2024, 1, 5), 96, 97, 94, 95),
    (date(2024, 1, 8), 95, 96, 93, 94),
    (date(2024, 1, 9), 94, 95, 93, 94),
]


def _run_id(base, inmem):
    return base + (100 if inmem == "0" else 0)


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_short_opens_from_flat_and_is_stopped_out_above(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(STOPPED, {DAY1: SELL}, run_id=_run_id(810, inmem), inmem=inmem,
                         enable_short=True)

    assert len(txns) == 1, f"exactly one short entry expected, got {txns}"
    txn = txns[0]
    assert txn["side"] == "SELL", "the short must be recorded as a SELL-side Transaction"
    assert txn["open_price"] == pytest.approx(100.0)
    assert txn["stop_loss"] > txn["open_price"], "a short's stop must sit ABOVE the entry"
    assert txn["stop_loss"] == pytest.approx(108.0)
    assert txn["take_profit"] < txn["open_price"], "a short's target must sit BELOW the entry"
    assert txn["take_profit"] == pytest.approx(90.0)
    assert txn["max_loss_stop"] == pytest.approx(108.0), "the sized-on safeguard, above the entry"

    [trade] = outcome["trades"]
    assert trade["direction"] == "sell"
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(108.0)
    assert trade["pnl"] < 0
    # Sized by risk: 8% risk budget over an 8% stop distance -> a full-risk-budget position,
    # never flipped: the cover bought back exactly what was sold.
    assert trade["size"] == pytest.approx(txn["quantity"])
    # The protective legs are BUY orders (a buy-stop above, a buy-limit below).
    legs = [o for o in outcome["orders"] if not o[-1]]
    assert legs and all(side == "BUY" for side, *_ in legs)


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_short_is_covered_by_the_reverse_signal(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(DRIFT, {DAY1: SELL, date(2024, 1, 5): BUY},
                         run_id=_run_id(820, inmem), inmem=inmem, enable_short=True,
                         exit_rules=[_signal_close("bullish")])

    assert len(txns) == 1 and txns[0]["side"] == "SELL"
    assert txns[0]["stop_loss"] > 100.0 > txns[0]["take_profit"]
    [trade] = outcome["trades"]
    assert trade["direction"] == "sell"
    assert trade["exit_reason"] == "exit", "covered by the close rule, not by a protective leg"
    assert trade["entry_price"] == pytest.approx(100.0)
    assert trade["exit_price"] == pytest.approx(95.0)
    assert trade["pnl"] > 0
    assert txns[0]["status"] == "CLOSED"


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_enable_sell_off_opens_no_short(monkeypatch, inmem):
    """The same bearish rule with shorts off (the seed only adds the sell rule with
    enable_short, so the permission is switched off on the expert alone here)."""
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(STOPPED, {DAY1: SELL}, run_id=_run_id(830, inmem), inmem=inmem,
                         enable_short=True, extra_settings={"enable_sell": (False, "bool")})
    assert txns == [] and outcome["trades"] == []
    assert all(o[0] != "SELL" or o[2] != "FILLED" for o in outcome["orders"])


def _signal_rule(field, action):
    return {"id": f"{action}-on-{field}", "name": f"{action}-on-{field}",
            "conditions": {"type": "AND", "conditions": [
                {"id": f"{field}-flag", "field": field, "op": "is_true"}]},
            "actions": [{"action_type": action}], "continue_processing": False}


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_with_shorts_on_a_sell_against_a_long_closes_it_and_never_flips(monkeypatch, inmem):
    """A bearish recommendation on a held long fires a `sell` exit: with enable_sell on it closes
    the long (close_transaction, capped at the held quantity), it does not open a short."""
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(DRIFT, {DAY1: BUY, date(2024, 1, 4): SELL},
                         run_id=_run_id(850, inmem), inmem=inmem, enable_short=True,
                         exit_rules=[_signal_rule("bearish", "sell")])
    assert [t["side"] for t in txns] == ["BUY"], f"no SELL-side (short) transaction may open: {txns}"
    [trade] = outcome["trades"]
    assert trade["direction"] == "buy" and trade["exit_reason"] == "exit"
    assert trade["size"] == pytest.approx(txns[0]["quantity"]), "sold exactly the held quantity"
    assert txns[0]["status"] == "CLOSED"


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_buy_against_a_short_covers_it_and_never_flips(monkeypatch, inmem):
    """A bullish recommendation on a held short fires a `buy` exit: it covers the short
    (close_transaction), it does not open a long."""
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(DRIFT, {DAY1: SELL, date(2024, 1, 5): BUY},
                         run_id=_run_id(860, inmem), inmem=inmem, enable_short=True,
                         exit_rules=[_signal_rule("bullish", "buy")])
    assert [t["side"] for t in txns] == ["SELL"], f"no BUY-side (long) transaction may open: {txns}"
    [trade] = outcome["trades"]
    assert trade["direction"] == "sell" and trade["exit_reason"] == "exit"
    assert trade["exit_price"] == pytest.approx(95.0) and trade["pnl"] > 0
    assert trade["size"] == pytest.approx(txns[0]["quantity"]), "bought back exactly the short"
    assert txns[0]["status"] == "CLOSED"


def _signal_rule_pct(field, action, percent):
    rule = _signal_rule(field, action)
    rule["actions"][0]["action_value"] = percent
    return rule


def _position(outcome_orders):
    """Net filled shares from the (side, type, status, qty, filled_qty, ...) order tuples."""
    net = 0.0
    for side, _type, status, _qty, filled, *_ in outcome_orders:
        if status == "filled" and filled:
            net += filled if side == "BUY" else -filled
    return net


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_sell_with_a_percent_partially_closes_a_long(monkeypatch, inmem):
    """BUY on day 1 (risk_atr: 100 shares at 100); a bearish bar fires `sell` 50%: 50 are sold,
    the transaction stays OPENED with the other 50 and its stop."""
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(DRIFT, {DAY1: BUY, date(2024, 1, 4): SELL},
                         run_id=_run_id(880, inmem), inmem=inmem, enable_short=False,
                         exit_rules=[_signal_rule_pct("bearish", "sell", 50)])
    assert [t["side"] for t in txns] == ["BUY"]
    assert txns[0]["status"] == "OPENED", "a partial close must leave the transaction open"
    assert txns[0]["stop_loss"] is not None and txns[0]["stop_loss"] < 100.0
    sells = [o for o in outcome["orders"] if o[0] == "SELL" and o[1] == "market" and o[2] == "filled"]
    assert len(sells) == 1 and sells[0][4] == pytest.approx(50.0), sells
    assert _position(outcome["orders"]) == pytest.approx(50.0), "never flips, never over-sells"


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_buy_with_a_percent_partially_covers_a_short(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(DRIFT, {DAY1: SELL, date(2024, 1, 5): BUY},
                         run_id=_run_id(890, inmem), inmem=inmem, enable_short=True,
                         exit_rules=[_signal_rule_pct("bullish", "buy", 50)])
    assert [t["side"] for t in txns] == ["SELL"]
    assert txns[0]["status"] == "OPENED"
    covers = [o for o in outcome["orders"] if o[0] == "BUY" and o[1] == "market" and o[2] == "filled"]
    assert len(covers) == 1 and covers[0][4] == pytest.approx(50.0), covers
    assert _position(outcome["orders"]) == pytest.approx(-50.0)


# --------------------------------------------------------------------------- #
# No impact on a long-only run
# --------------------------------------------------------------------------- #

def _legacy_sell_execute(self):
    """``SellAction.execute`` as it was before equity shorts (HEAD 8ed2801c), verbatim."""
    from ba2_common.core.TradeActions import ExpertActionType, logger, absorb_if_benign, InstanceNotFound
    from ba2_common.core.portfolio_allocation import PositionFetchFailed

    try:
        try:
            current_position = self.get_current_position()
        except PositionFetchFailed as e:
            logger.error(f"SellAction refusing {self.instrument_name}: {e}")
            return self.create_and_save_action_result(
                action_type=ExpertActionType.SELL.value,
                success=False,
                message=(f"Position book unverified for {self.instrument_name} "
                         f"(broker position fetch failed) - refusing to sell"),
                data={"position_fetch_failed": True}
            )
        if current_position is None or current_position <= 0:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.SELL.value,
                success=False,
                message=f"No long position to sell for {self.instrument_name}",
                data={}
            )
        order_id = self.create_order_record(side="sell", quantity=0.0, order_type="market")
        if not order_id:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.SELL.value, success=False,
                message="Failed to create order record", data={})
        return self.create_and_save_action_result(
            action_type=ExpertActionType.SELL.value,
            success=True,
            message=f"Sell order created for {self.instrument_name} (pending risk management review)",
            data={"order_id": order_id, "status": "PENDING"}
        )
    except Exception as e:
        absorb_if_benign(e, InstanceNotFound)
        return self.create_and_save_action_result(
            action_type=ExpertActionType.SELL.value, success=False,
            message=f"Error creating sell order: {str(e)}", data={})


def _legacy_buy_execute(self, quantity: Optional[float] = None):
    """``BuyAction.execute`` as it was before equity shorts (HEAD 8ed2801c), verbatim."""
    from ba2_common.core.TradeActions import ExpertActionType, absorb_if_benign, InstanceNotFound

    try:
        if quantity is None:
            quantity = 0.0
        current_price = self.get_current_price()
        if current_price is None:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.BUY.value, success=False,
                message=f"Cannot get current price for {self.instrument_name}", data={})
        order_id = self.create_order_record(
            side="buy", quantity=quantity, order_type="market",
            extra_data={"lot_size": self._equity_lot_size()} if self.lot_size else None)
        if not order_id:
            return self.create_and_save_action_result(
                action_type=ExpertActionType.BUY.value, success=False,
                message="Failed to create order record", data={})
        return self.create_and_save_action_result(
            action_type=ExpertActionType.BUY.value, success=True,
            message=f"Buy order created for {self.instrument_name} (pending risk management review)",
            data={"order_id": order_id, "status": "PENDING"})
    except Exception as e:
        absorb_if_benign(e, InstanceNotFound)
        return self.create_and_save_action_result(
            action_type=ExpertActionType.BUY.value, success=False,
            message=f"Error creating buy order: {str(e)}", data={})


# Two long round trips: the first closed by the profit `close` rule, the second stopped out.
LONG_ONLY = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 104, 99, 103),
    (date(2024, 1, 4), 103, 107, 102, 106),
    (date(2024, 1, 5), 106, 107, 104, 105),
    (date(2024, 1, 8), 105, 106, 103, 104),
    (date(2024, 1, 9), 104, 105, 90, 92),
    (date(2024, 1, 10), 92, 94, 91, 93),
    (date(2024, 1, 11), 93, 95, 92, 94),
]

_ALWAYS = {"type": "AND", "conditions": [
    {"id": "c", "field": "profit_loss_percent", "op": ">=", "value": -1000}]}
_IN_PROFIT = {"type": "AND", "conditions": [
    {"id": "p", "field": "profit_loss_percent", "op": ">=", "value": 5}]}
LONG_ONLY_EXITS = [
    {"id": "close-in-profit", "name": "close-in-profit", "conditions": _IN_PROFIT,
     "actions": [{"action_type": "close"}], "continue_processing": False},
]


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_long_only_run_is_byte_identical(monkeypatch, inmem):
    TA = __import__("ba2_common.core.TradeActions", fromlist=["x"])
    _store_mode(monkeypatch, inmem)
    signals = {d: BUY for (d, *_rest) in LONG_ONLY}

    calls = {"buy": 0, "sell": 0, "close": 0}
    real_buy, real_sell, real_close = TA.BuyAction.execute, TA.SellAction.execute, TA.CloseAction.execute

    def counting_buy(self, *a, **k):
        calls["buy"] += 1
        return real_buy(self, *a, **k)

    def counting_sell(self, *a, **k):
        calls["sell"] += 1
        return real_sell(self, *a, **k)

    def counting_close(self, *a, **k):
        result = real_close(self, *a, **k)
        calls["close"] += bool(result["success"])
        return result

    monkeypatch.setattr(TA.BuyAction, "execute", counting_buy)
    monkeypatch.setattr(TA.SellAction, "execute", counting_sell)
    monkeypatch.setattr(TA.CloseAction, "execute", counting_close)
    now, txns_now = _run(LONG_ONLY, signals, run_id=_run_id(840, inmem), inmem=inmem,
                         enable_short=False, exit_rules=LONG_ONLY_EXITS)

    assert calls["buy"] >= 2, calls
    assert calls["sell"] == 0, f"a long-only fixture must not run a sell action: {calls}"
    assert calls["close"] >= 1, f"the fixture never closed a position: {calls}"
    assert len(now["trades"]) >= 2 and all(t["direction"] == "buy" for t in now["trades"])
    assert {t["exit_reason"] for t in now["trades"]} >= {"exit", "stop_loss"}, now["trades"]
    assert all(t["side"] == "BUY" for t in txns_now)

    monkeypatch.setattr(TA.BuyAction, "execute", _legacy_buy_execute)
    monkeypatch.setattr(TA.SellAction, "execute", _legacy_sell_execute)
    monkeypatch.setattr(TA.CloseAction, "execute", real_close)
    before, txns_before = _run(LONG_ONLY, signals, run_id=_run_id(841, inmem), inmem=inmem,
                               enable_short=False, exit_rules=LONG_ONLY_EXITS)

    assert now["orders"] == before["orders"]
    assert now["trades"] == before["trades"]
    assert now["equity"] == before["equity"]
    assert txns_now == txns_before
