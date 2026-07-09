"""Verify the rebuilt S7 (faithful replica) reproduces the archived 186% winner THROUGH THE
UNIFIED RULE MODEL pipeline (trade_rules_from_legacy -> seed_ruleset_from_rules 1:1).

Takes the byte-identical winner reproduction (backtest #240, opt 49: 186.53%, 149 trades,
2023-2026 large-cap band), converts its persisted legacy ruleset (buy tree + single-action
exit rows) into TradeRule lists exactly like _strategy_from_parts does, threads the winner's
model:* gene values as expert overrides, and runs ONE backtest with opt 49's own run config.
A result near 186%/149 proves the new pipeline carries the winner's economics; a large gap
means the unified seeding changed semantics somewhere.

Run (test venv):
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe test_files/verify_s7_replica.py
"""
import json
import os
import sqlite3
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
sys.path.insert(0, _BACKEND)

# Bootstrap exactly like ba2test_launcher: .env, then point ba2_common at the test DB and
# mirror FMP_API_KEY from the app-settings DB into the env (the providers read the ENV).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND, ".env"))
    load_dotenv(os.path.join(_REPO, "testplatform", ".env"))
except Exception:  # noqa: BLE001 — dotenv optional
    pass

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"

from ba2_common.core import db as _ba2_db  # noqa: E402
_ba2_db.configure_db(DB)
if not os.getenv("FMP_API_KEY"):
    from ba2_common.config import get_app_setting
    _k = get_app_setting("FMP_API_KEY")
    if _k:
        os.environ["FMP_API_KEY"] = _k


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    bt = conn.execute("SELECT * FROM backtests WHERE id=240").fetchone()
    opt = conn.execute("SELECT optimization_config FROM strategy_optimizations WHERE id=?",
                       (bt["optimization_id"],)).fetchone()
    conn.close()

    sp = json.loads(bt["strategy_params"])
    cfg = json.loads(opt["optimization_config"])
    bt_block = cfg["backtest"]

    from ba2_common.core.rule_models import trade_rules_from_legacy
    converted = trade_rules_from_legacy(
        buy_tree=sp.get("buyEntryConditions"),
        entry_actions=sp.get("entryActions"),
        exit_conditions=sp.get("exitConditions"),
    )
    print(f"entry rules: {[r['id'] for r in converted['entry_rules']]}")
    print(f"exit rules:  {[r['id'] for r in converted['exit_rules']]}")

    # The winner's expert settings (model:* genes persisted on the row).
    overrides = {k[len("model:"):]: v for k, v in sp.items() if k.startswith("model:")}
    schedule = {k[len("schedule:"):]: bool(v) for k, v in sp.items() if k.startswith("schedule:")}
    print(f"model overrides: {overrides}")

    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config, _build_hoisted_state,
    )
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    hoisted = _build_hoisted_state(bt_block)
    decoded = {
        "expert_overrides": overrides,
        "screener_overrides": {k[len("screener:"):]: v for k, v in sp.items()
                               if k.startswith("screener:")},
        "schedule_days": schedule or None,
        "entry_rules": converted["entry_rules"],
        "exit_rules": converted["exit_rules"],
    }
    trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
    trial_cfg["name"] = "verify-s7-replica"

    results = run_daily_backtest(trial_cfg)
    print("\n=== unified-pipeline replay of the archived winner ===")
    print(f"total_return:  {results.get('total_return')}%   (archived: 186.53%)")
    print(f"total_trades:  {results.get('total_trades')}     (archived: 149)")
    print(f"max_drawdown:  {results.get('max_drawdown')}%  (archived: -11.52%)")
    print(f"calmar:        {results.get('calmar_ratio')}    (archived: 3.66)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
