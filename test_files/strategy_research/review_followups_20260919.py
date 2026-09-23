"""Hermetic review evidence for 4ce1e6e8 and the preceding option-direction work.

Run with the test venv from the repository root. Uses temporary databases/caches,
fixture option prices and stub recommendations; never calls a broker or provider.
This is an investigation probe, not a regression test that blesses existing bugs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "testplatform", ROOT / "testplatform/backend"):
    sys.path.insert(0, str(path))


def run():
    # Readers keep mappings/SQLite handles until process exit on Windows.
    with tempfile.TemporaryDirectory(prefix="ba2-review-20260919-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        os.environ["BA2_HOME"] = str(root)
        os.environ["DATABASE_URL"] = "sqlite:///" + (root / "host.sqlite").as_posix()
        os.environ["BA2_FILE_LOGGING"] = "0"
        os.environ["BA2_STDOUT_LOGGING"] = "0"
        logging.disable(logging.CRITICAL)

        import ba2test_launcher as launcher
        from ba2_common.core.types import OrderRecommendation, Recommendation
        from ba2_common.core.market_condition_reader import MappedMarketConditionReader
        from app.services.backtest.default_rulesets import seed_entry_ruleset_from_rules
        from app.services.backtest.seam_wiring import check_market_condition_window
        from app.services.backtest.daily_engine import DailyBacktestEngine
        from app.services.strategy_param_space import decode_params, collect_param_space
        from app.services.strategy_optimization_handler import _build_daily_trial_config
        from tests.backtest.test_option_entry_path import _build_engine_with_option_entry
        from tests.test_research10_market_conditions import cache_job
        from tools.strategy_research import market_conditions as MC, profiles as P

        option_runs = []
        for kind, mode, signal, confidence in (
            ("O_LC", "above", OrderRecommendation.BUY, 90),
            ("O_LC", "above", OrderRecommendation.SELL, 90),
            ("O_LC", "below", OrderRecommendation.SELL, 90),
            ("O_LC", "below", OrderRecommendation.BUY, 90),
            ("O_LC", "off", OrderRecommendation.HOLD, 90),
            ("O_STRD", None, OrderRecommendation.HOLD, 20),
            ("O_STRD", "off", OrderRecommendation.BUY, 20),
            ("O_STRD", "off", OrderRecommendation.SELL, 20),
            ("O_STRD", "off", OrderRecommendation.BUY, 50),
            ("O_STRD", None, OrderRecommendation.BUY, 20),
            ("O_STRD", None, OrderRecommendation.SELL, 20),
        ):
            engine, account, expert, context, expert_id, action = _build_engine_with_option_entry(
                action_type="buy_call", strike_method="percent_otm", strike_param=2.0,
                dte_min=20, dte_max=45, sizing=5.0)
            try:
                # Isolate the signal gate. For O_STRD this deliberately still uses a buy-call
                # action: no straddle chain assumptions can hide an unreachable HOLD gate.
                leaf = launcher._option_signal_gate("review", kind)
                rules = [{"id": "review-entry", "conditions": {"type": "AND", "conditions": [
                    leaf, {"id": "review-flat", "field": "has_no_position", "field_type": "flag"}]},
                    "actions": [action], "continue_processing": False}]
                if kind == "O_STRD":
                    rules[0]["conditions"]["conditions"].append(launcher._low_confidence_gate("review"))
                    flat = {} if mode is None else {"cond:review-signal:enabled": 0}
                else:
                    flat = {} if mode is None else {"cond:review-signal:mode": mode}
                decoded = decode_params(SimpleNamespace(entry_rules=rules, exit_rules=[]), flat)
                ruleset_id = seed_entry_ruleset_from_rules(decoded["entry_rules"], name="review-signal")
                engine.experts = [(expert, expert_id, expert.settings, ruleset_id)]

                def analyze(as_of, context):
                    return Recommendation(signal=signal, confidence=confidence,
                        current_price=context.account.get_instrument_current_price("AAPL"),
                        expected_profit_percent=10.0, details="isolated review signal", raw_outputs={})

                expert.analyze_as_of = analyze
                engine.run()
                option_runs.append({"template_gate": kind, "mode": mode, "signal": signal.value,
                    "confidence": confidence,
                    "option_positions": len(account.get_option_positions()),
                    "cash": account.get_balance()})
            finally:
                context.__exit__(None, None, None)

        # Actual neutral entry path exits before touching self/account/ruleset/provider.
        empty_engine = object.__new__(DailyBacktestEngine)
        hold_staged = empty_engine._stage_recommendation_candidate(
            SimpleNamespace(signal=OrderRecommendation.HOLD, skip=False),
            expert=None, expert_id=1, symbol="AAA", ruleset_id=1,
            as_of=datetime(2024, 3, 28, tzinfo=timezone.utc), equity_candidates=[])

        # The committed driver fixture covers all required feature rows, but its declared
        # decision window ends a day early. Compare driver readiness with actual trial guard.
        job, cache_root, _, _ = cache_job(root)
        bt = job["optimization_config"]["backtest"]
        driver_report = MC.preflight(bt, cache_root)
        bt["backtest_id"] = "review-preflight"
        trial = _build_daily_trial_config(bt, decode_params(SimpleNamespace(**job["strategy"]), {}), option_trade_records=False)
        mapped = MappedMarketConditionReader(cache_root, bt["market_condition_manifests"]["ohlcv-v1"], "ohlcv-v1")
        try:
            check_market_condition_window(trial, SimpleNamespace(mapped_reader=mapped))
            trial_error = None
        except ValueError as exc:
            trial_error = str(exc)

        # Show the existing recipe genes still present in an advertised all-off comparison.
        comparison = P.build_manifest(families=["quality_momentum"], search="genetic",
            market_condition_profile="ohlcv-v1", market_condition_manifest="a" * 64,
            market_condition_mode="all-off")["jobs"][0]
        recipe_genes = list(collect_param_space(SimpleNamespace(**comparison["strategy"]),
            comparison["optimization_config"]["expert_params"]))

        assert [r["option_positions"] > 0 for r in option_runs] == [
            True, False, True, False, False, False, True, True, False, False, False], option_runs
        assert hold_staged is False and driver_report and trial_error is not None
        return {"reviewed": ["4ce1e6e8", "f359ab9a"], "option_runs": option_runs,
            "neutral_hold_staged": hold_staged, "preflight_passed": True,
            "trial_guard_error": trial_error, "all_off_still_searches": recipe_genes}


if __name__ == "__main__":
    evidence = run()
    output = ROOT / "reports/strategy_research/commit_review_2026-09-19_evidence.json"
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
