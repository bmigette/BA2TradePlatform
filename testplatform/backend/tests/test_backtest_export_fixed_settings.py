"""Regression: _derive_export_payload's "expert_settings" kind must recover the expert's FIXED
(non-GA-tunable) settings (e.g. sizing_mode=risk_atr) from Backtest.strategy_params["
expertFixedSettings"] -- the durable copy _persist_top_backtests now stashes there (see
test_persist_top_backtests.py) -- REGARDLESS of whether the source StrategyOptimization row
still resolves (it can be pruned by `server db-cleanup` long after the "starred" Backtest is
kept). Before this fix, a pruned optimization silently dropped sizing_mode from the export,
and the live deploy ended up with zero safeguard stop-loss protection (the PKE/CALX incident)."""
from datetime import datetime

import pytest

from app.api.backtests import _derive_export_payload
from app.models.backtest import Backtest
from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


def _backtest(**overrides):
    defaults = dict(
        name="TOP1-test", expert_name="FMPRating", engine_type="daily_expert",
        strategy_params={"model:profit_ratio": 1.2}, start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 1), initial_capital=10_000.0,
        optimization_id=None,
    )
    defaults.update(overrides)
    return Backtest(**defaults)


def test_fallback_path_recovers_fixed_settings_when_no_optimization_link():
    """No optimization_id at all (e.g. a manual/standalone run, or the common real-world case
    of a pruned StrategyOptimization) -- _opt_backtest_block returns (None, None), so the
    fallback branch fires. expertFixedSettings must still surface in the export."""
    bt = _backtest(strategy_params={
        "model:profit_ratio": 1.2,
        "expertFixedSettings": {"sizing_mode": "risk_atr"},
    })
    payload = _derive_export_payload(bt, "expert_settings", db=None)
    assert payload["settings"]["expert_params"] == {
        "sizing_mode": "risk_atr", "profit_ratio": 1.2,
    }


def test_fallback_path_with_no_fixed_settings_is_unaffected():
    """No expertFixedSettings key at all -> falls back to model_overrides only, exactly like
    before this fix (no regression for runs that never had fixed settings)."""
    bt = _backtest(strategy_params={"model:profit_ratio": 1.2})
    payload = _derive_export_payload(bt, "expert_settings", db=None)
    assert payload["settings"]["expert_params"] == {"profit_ratio": 1.2}


def test_happy_path_layers_persisted_fixed_settings_under_live_optimization_settings(monkeypatch):
    """When the StrategyOptimization row DOES still resolve, its OWN base_settings win over the
    persisted expertFixedSettings floor (live source of truth takes precedence), but the floor
    still fills in anything the live row's spec doesn't cover."""
    db = SessionLocal()
    try:
        strat = Strategy(name="export-fixed-strat", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="export-fixed-opt",
            fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={
                "backtest": {
                    "backtest_id": 1, "enabled_instruments": ["AAPL"],
                    # Live row's spec sets sizing_mode itself -- must win over the persisted floor.
                    "experts": [{"class": "FMPRating", "settings": {"sizing_mode": "notional"}}],
                    "account_settings": {}, "warmup_days": 30, "seed": 42,
                }
            },
            all_results=[], best_params={}, best_fitness=1.0, status="completed",
        )
        db.add(opt); db.commit(); db.refresh(opt)

        bt = _backtest(
            optimization_id=opt.id,
            strategy_params={
                "model:profit_ratio": 1.2,
                # Floor layer: min_trader_hold_roundtrips isn't in the live spec above, so it
                # must come through from the persisted floor even though the live row resolves.
                "expertFixedSettings": {"sizing_mode": "risk_atr", "min_trader_hold_roundtrips": 3},
            },
        )
        payload = _derive_export_payload(bt, "expert_settings", db=db)
        params = payload["settings"]["expert_params"]
        assert params["sizing_mode"] == "notional"  # live row wins
        assert params["min_trader_hold_roundtrips"] == 3  # floor fills the gap
        assert params["profit_ratio"] == 1.2
    finally:
        db.close()
