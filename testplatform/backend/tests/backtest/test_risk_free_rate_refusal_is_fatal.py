"""A missing / short FRED DGS3MO cache ABORTS an option GA; it never scores 0.

Every trial of an options run resolves the same risk-free rate from the same cache file, so a
refusal there is deterministic: scored as an ordinary failed trial it would become 0 fitness on
every genome and the GA would "finish" on nothing. This drives the REAL refusal through the
REAL ``run_daily_backtest`` (called by ``_trial_worker`` exactly as a pool worker calls it) and
asserts the trial comes back ``fatal``.
"""
from __future__ import annotations

from datetime import date

import pytest

from tests.backtest.test_option_split_basis import _NFLX_CALENDAR, _write_fmp_daily


@pytest.fixture
def no_rate_cache(tmp_path, monkeypatch):
    """A cache holding NFLX's FMP daily bars and split calendar, and NO FRED series."""
    import json

    import ba2_common.config as cfg
    from ba2_common.core import native_cache
    from ba2_providers.macro import fred_series

    from app.services.backtest.option_split_basis import clear_split_basis_memo

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.delenv("BACKTEST_OPTIONS_RISK_FREE_RATE", raising=False)
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    _write_fmp_daily(tmp_path / "FMPOHLCVProvider" / "NFLX_1d.parquet")
    calendar = tmp_path / "fmp_history" / "mc_stock_split__NFLX.json"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text(json.dumps(_NFLX_CALENDAR))
    clear_split_basis_memo()
    yield tmp_path
    clear_split_basis_memo()


def _config(tmp_path):
    return {"backtest_id": 1, "start_date": "2024-05-01", "end_date": "2024-05-31",
            "enabled_instruments": ["NFLX"], "experts": [], "initial_capital": 20000.0,
            "account_settings": {"starting_cash": 20000.0}, "warmup_days": 0, "seed": 1,
            "execution_interval": "1d", "option_trade_records": False,
            "options_cache_db": str(tmp_path / "opt.sqlite")}


def test_run_daily_backtest_refuses_without_the_rate_cache(no_rate_cache):
    from ba2_providers.macro.risk_free_rate import RiskFreeRateUnavailable

    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    with pytest.raises(RiskFreeRateUnavailable, match="DGS3MO is not in the cache"):
        run_daily_backtest(_config(no_rate_cache))


def test_the_trial_wrapper_classifies_it_fatal(no_rate_cache):
    import app.services.strategy_optimization_handler as H

    out = H._trial_worker(_config(no_rate_cache), "calmar")
    assert out["ok"] is False and out["fatal"] is True, out
    assert "DGS3MO is not in the cache" in out["error"]
