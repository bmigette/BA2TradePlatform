"""Economic-FINGERPRINT gate for the sample backtest (byte-identical economics guard).

Runs the SAME deterministic, hermetic sample BT as ``tools/perf_sample_bt.py`` (20 syms x 250
bars, stub BUY expert, enter ruleset + TP/stop exit ruleset) but instead of timing it, prints the
run's ECONOMIC fingerprint:

    final equity | round-trips | exit-reason breakdown | filled/total orders

This is the gate used to prove a refactor (e.g. the in-memory "dict trades" store) is
economically byte-identical: the numbers MUST match the locked values across the change.

Locked reference (pre-dict-trades): equity 9,901,584.03 | round-trips 320 |
exit_reasons {exit: 172, stop_loss: 148}.

Run:  cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/econ_gate_sample_bt.py
Env:  PERF_SYMS (default 20)  PERF_BARS (default 250)  BT_INMEM_TRADES=1 to force the store on.
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta

logging.disable(logging.CRITICAL)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

NSYMS = int(os.environ.get("PERF_SYMS", "20"))
NBARS = int(os.environ.get("PERF_BARS", "250"))


def _bars(seed):
    d0 = date(2024, 1, 1)
    rows = []
    for k in range(NBARS):
        dd = d0 + timedelta(days=k)
        p = 100.0 + (9 if (k + seed) % 7 < 3 else -7) * (((seed % 5) + 1) / 5.0) + (k % 13)
        rows.append({"Date": datetime(dd.year, dd.month, dd.day), "Open": p, "High": p * 1.03,
                     "Low": p * 0.97, "Close": p, "Volume": 1000})
    return rows


def _run():
    from app.services.backtest.backtest_db import (backtest_trading_db, seed_account_definition,
                                                   seed_expert_instance)
    from app.services.backtest.default_rulesets import (seed_enter_long_ruleset,
                                                       seed_open_positions_ruleset)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.db import activity_logging_disabled
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.types import OrderRecommendation, OrderStatus, Recommendation

    syms = [f"S{i}" for i in range(NSYMS)]
    d0 = date(2024, 1, 1)
    days = [d0 + timedelta(days=k) for k in range(NBARS)]
    cfg = {"starting_cash": 5_000_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
           "fill_model": "next_bar_open"}

    class E(MarketExpertInterface):
        def __init__(self, i, ps):
            self.id = i; self._ps = ps; self._settings_cache = None
        @classmethod
        def description(cls):
            return "econ-gate stub"
        def render_market_analysis(self, m):
            return ""
        def run_analysis(self, s, m):
            return None
        def analyze_as_of(self, as_of, ctx):
            sym = getattr(self, "_gather_symbol", "S0")
            close = self._ps.close_at(sym, as_of) or 100.0
            sig = OrderRecommendation.BUY if (as_of.day % 6 == 2) else OrderRecommendation.HOLD
            return Recommendation(signal=sig, confidence=90.0, current_price=float(close),
                                  details="x", expected_profit_percent=20.0)

    exit_rules = [
        {"id": "tp", "action_type": "close",
         "conditions": {"type": "AND", "conditions": [
             {"id": "p", "field": "profit_loss_percent", "op": ">", "value": 5}]}},
        {"id": "sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -5.0,
         "conditions": {"type": "AND", "conditions": [{"id": "h", "field": "has_position"}]}},
    ]

    wire = wire_backtest_seams()
    with backtest_trading_db("econ-gate"), activity_logging_disabled():
        seed_account_definition(1, cfg)
        rid = seed_enter_long_ruleset(name="econ-enter")
        oid = seed_open_positions_ruleset(exit_rules, name="econ-exit")
        seed_expert_instance(account_id=1, expert_class_name="E", enter_market_ruleset_id=rid,
                             open_positions_ruleset_id=oid, instance_id=1)
        ps = AsOfPriceSource(ohlcv_provider=None)
        for i, sym in enumerate(syms):
            ps.load_bars(sym, _bars(i))
        acct = BacktestAccount(1, ps, cfg)
        wire.register_account(1, acct)
        exp = E(1, ps)
        exp.save_settings({"allow_automated_trade_opening": (True, "bool"),
                           "enable_buy": (True, "bool")})
        wire.register_expert(1, exp)
        eng = DailyBacktestEngine(
            account=acct, experts=[(exp, 1, {}, rid)], price_source=ps,
            config={"start_date": days[0], "end_date": days[-1], "enabled_instruments": syms,
                    "seed": 42}, indicator_provider=None)
        eng._indicator_provider = object()
        eng.run()

        rts = acct.get_round_trip_trades()
        orders = acct.get_orders()
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        reasons = Counter(t.get("exit_reason", "unknown") for t in rts)
        return {
            "equity": round(acct.equity(), 2),
            "round_trips": len(rts),
            "exit_reasons": dict(sorted(reasons.items())),
            "orders": len(orders),
            "filled": len(filled),
        }


def main() -> int:
    fp = _run()
    print("=== sample-BT economic fingerprint ===")
    print(f"  final equity:   {fp['equity']:,.2f}")
    print(f"  round-trips:    {fp['round_trips']}")
    print(f"  exit_reasons:   {fp['exit_reasons']}")
    print(f"  orders (total): {fp['orders']}   filled: {fp['filled']}")
    print(f"FINGERPRINT equity={fp['equity']} rts={fp['round_trips']} "
          f"reasons={fp['exit_reasons']} orders={fp['orders']} filled={fp['filled']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
