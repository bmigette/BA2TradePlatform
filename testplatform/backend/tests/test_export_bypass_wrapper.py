"""End-to-end tests of the test app's export wrapper (app.api.backtests._derive_export_payload).

The derivation lives in ba2_common.export.backtest_export and is pure: the wrapper supplies the
DB-bound inputs. These tests pin the wiring the golden fixtures cannot see:

  * the BYPASS path: a FactorRanker run from a screener optimization with
    apply_to_expert_settings exports universe_source=screener plus the effective screener
    settings, which needs the wrapper's real bypass check (_is_bypass_expert_class) and the
    optimization block resolved from the DB;
  * an unsupported kind still maps to the same HTTP 400 and message the endpoint always raised.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.backtests import _derive_export_payload
from app.models.backtest import Backtest
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization


def _backtest(**overrides):
    defaults = dict(
        name="TOP1-bypass", expert_name="FMPRating", engine_type="daily_expert",
        strategy_params={"model:profit_ratio": 1.2}, start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=None,
    )
    defaults.update(overrides)
    return Backtest(**defaults)


def test_factor_ranker_screener_run_exports_the_bypass_overlay(db):
    # The bypass detection swallows import failures and returns False, which would make this
    # test fail for the wrong reason in a broken environment: skip loudly instead.
    pytest.importorskip("ba2_experts.FactorRanker")

    strat = Strategy(name="export-bypass-strat", entry_rules=[], exit_rules=[])
    db.add(strat)
    db.flush()
    opt = StrategyOptimization(
        strategy_id=strat.id, name="export-bypass-opt",
        fitness_metric="sharpe", optimization_type="genetic",
        optimization_config={
            "backtest": {
                "experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
                "account_settings": {},
                "screener_opt": {
                    "store": "sp500", "base_settings": {"min_mcap": 1}, "cadence_days": 7,
                    "apply_to_expert_settings": True,
                },
            }
        },
        all_results=[], best_params={}, best_fitness=1.0, status="completed",
    )
    db.add(opt)
    db.flush()
    bt = _backtest(expert_name="FactorRanker", optimization_id=opt.id,
                   strategy_params={"screener:min_mcap": 5})
    db.add(bt)
    db.flush()

    payload = _derive_export_payload(bt, "expert_settings", db)

    params = payload["settings"]["expert_params"]
    assert params["universe_source"] == "screener"
    assert params["screener_store"] == "sp500"
    assert params["min_mcap"] == 5
    assert payload["universe"]["mode"] == "screener"


def test_unsupported_kind_is_the_same_http_400():
    with pytest.raises(HTTPException) as exc:
        _derive_export_payload(_backtest(), "bogus", db=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Unsupported export kind: 'bogus'. Use 'expert_settings' or 'ruleset'."
