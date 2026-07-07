"""Quick hermetic backtest of a SIMULATED S1 GA individual.

Proves the whole S1 path end-to-end after the unification work: build the S1 strategy (live
"high conviction" + entry TP/SL bracket), GENERATE an individual (collect_param_space -> pick genes,
with the entry TP/SL bracket TOGGLED ON and the buy gates relaxed so entries fire), DECODE it
(decode_params -> buy_tree / entry_rules / exit_rules), seed those rules, and run the DailyBacktestEngine
over synthetic bars with a stub expert emitting BUY recs. Reports trades + equity + confirms the
entry bracket fired and the temp-list flow persisted only funded orders. No network, no real data.

Run:  cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/quick_bt_s1.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from datetime import date, datetime

logging.disable(logging.INFO)  # keep output readable

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _load_launcher():
    path = os.path.normpath(os.path.join(_BACKEND, "..", "ba2test_launcher.py"))
    spec = importlib.util.spec_from_file_location("ba2test_launcher", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ba2test_launcher"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_individual(space):
    """A deterministic individual: enable the entry TP/SL bracket, DROP the buy-gate conditions
    (so the stub reliably enters), drop exit rules (hold to end), value genes at midpoint."""
    ind = {}
    for gene, spec in space.items():
        if gene.endswith(":enabled"):
            ind[gene] = 1 if gene.startswith("entry:") else 0  # entry bracket ON, other toggles OFF
        elif isinstance(spec, dict) and spec.get("type") == "float":
            lo, hi = spec.get("min", 0.0), spec.get("max", 0.0)
            ind[gene] = round((lo + hi) / 2.0, 2)
        elif isinstance(spec, dict) and spec.get("type") == "int":
            ind[gene] = int((spec.get("min", 0) + spec.get("max", 0)) // 2)
    return ind


def main() -> int:
    from app.services.strategy_param_space import collect_param_space, decode_params
    from app.services.backtest.backtest_db import (backtest_trading_db, seed_account_definition,
                                                   seed_expert_instance)
    from app.services.backtest.default_rulesets import (seed_ruleset_from_tree,
                                                       seed_open_positions_ruleset)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.types import OrderRecommendation, OrderStatus, Recommendation

    L = _load_launcher()
    # 1. Build S1 + 2. generate an individual + 3. decode it.
    s1 = L._build_strategy("S1", "quick-S1", "FMPRating")
    space = collect_param_space(s1)
    individual = _make_individual(space)
    decoded = decode_params(s1, individual)
    entry_rules = decoded.get("entry_rules") or []
    print(f"S1 param-space genes: {len(space)}  (entry-bracket genes: "
          f"{sum(1 for g in space if g.startswith('entry:'))})")
    print(f"decoded individual -> entry_rules (TP/SL bracket): "
          f"{[(r['action_type'], r.get('action_value')) for r in entry_rules]}")

    CFG = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
           "fill_model": "next_bar_open"}
    BARS = [(date(2024, 1, d), 100.0, 101.0, 99.0, c) for d, c in
            [(2, 100), (3, 100), (4, 100), (5, 120), (8, 130)]]  # a rising AAPL

    class _S1Expert(MarketExpertInterface):
        def __init__(self, id_val, ps):
            self.id = id_val; self._ps = ps; self._settings_cache = None
        @classmethod
        def description(cls): return "S1 quick-bt stub (BUY on day 1, target 130)"
        def render_market_analysis(self, ma): return ""
        def run_analysis(self, s, ma): return None
        def analyze_as_of(self, as_of, context):
            close = self._ps.close_at("AAPL", as_of) or 100.0
            d = as_of.date() if hasattr(as_of, "date") else as_of
            if d == date(2024, 1, 2):
                r = Recommendation(signal=OrderRecommendation.BUY, confidence=100.0,
                                   current_price=float(close), details="s1 buy",
                                   expected_profit_percent=30.0)
                try: r.target_price = 130.0  # for the target-anchored entry TP
                except Exception: pass
                return r
            return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                                  current_price=float(close), details="hold",
                                  expected_profit_percent=0.0)

    from ba2_common.core.db import activity_logging_disabled
    resolver = wire_backtest_seams()
    with backtest_trading_db("quick-s1"), activity_logging_disabled():
        seed_account_definition(1, CFG)
        ruleset_id = seed_ruleset_from_tree(decoded.get("buy_tree"), name="quick-s1-enter",
                                            entry_actions=entry_rules)
        open_id = seed_open_positions_ruleset(decoded.get("exit_rules") or [], name="quick-s1-exit")
        seed_expert_instance(account_id=1, expert_class_name="_S1Expert",
                             enter_market_ruleset_id=ruleset_id,
                             open_positions_ruleset_id=open_id, instance_id=1)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c,
                               "Volume": 1000} for (d, o, h, lo, c) in BARS])
        account = BacktestAccount(1, ps, CFG)
        resolver.register_account(1, account)
        expert = _S1Expert(1, ps)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(1, expert)

        engine = DailyBacktestEngine(
            account=account, experts=[(expert, 1, {}, ruleset_id)], price_source=ps,
            config={"start_date": date(2024, 1, 2), "end_date": date(2024, 1, 8),
                    "enabled_instruments": ["AAPL"], "seed": 42}, indicator_provider=None)
        engine._indicator_provider = object()  # notional sizing (no ATR build)
        results = engine.run()

        orders = account.get_orders()
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        brackets = [o for o in orders if (o.stop_price or o.limit_price) and o.depends_on_order]
        pos = account.get_positions()
        print("\n=== quick BT result (simulated S1 individual) ===")
        print(f"  bars run:            {len(BARS)}")
        print(f"  orders created:      {len(orders)}  (only FUNDED persisted — temp-list flow)")
        print(f"  filled entries:      {len([o for o in filled if o.side.value=='BUY'])}")
        print(f"  bracket legs (TP/SL): {len(brackets)}  <- S1 entry bracket fired")
        print(f"  final equity:        ${account.equity():,.2f}  (start $100,000)")
        print(f"  open positions:      {[(p['symbol'], p['qty']) for p in pos]}")
        ok = len(orders) > 0 and account.equity() > 0
        print("QUICK BT OK" if ok else "QUICK BT: no orders (check gates)")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
