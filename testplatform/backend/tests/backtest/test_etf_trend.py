"""ETF selection is causal, monthly, cash-capable and uses the normal expert contract."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.types import OrderRecommendation
from ba2_experts.ETFTrend import ETFTrend, monthly_selection


@pytest.fixture
def settings():
    return {"universe_symbols": ["AAA", "BBB", "CCC"], "momentum_bars": 126,
            "trend_bars": 200, "top_n": 2}


@pytest.fixture
def histories():
    dates = pd.bdate_range("2018-01-01", "2020-03-31", tz="UTC")
    return {s: pd.DataFrame({"Date": dates, "Close": np.linspace(start, end, len(dates))})
            for s, start, end in [("AAA", 50, 100), ("BBB", 100, 50), ("CCC", 100, 125)]}


def test_positive_filter_selects_only_eligible_top_n(settings, histories):
    out = monthly_selection(histories, datetime(2020, 2, 3, 9, 30), settings)
    assert out["selected"] == ["AAA", "CCC"]
    assert set(out["anchor_dates"].values()) == {"2020-01-31"}
    settings["top_n"] = 1
    assert monthly_selection(histories, datetime(2020, 2, 3), settings)["selected"] == ["AAA"]


def test_future_and_entry_day_closes_cannot_change_ranking(settings, histories):
    at = datetime(2020, 2, 3, 9, 30)
    before = monthly_selection(histories, at, settings)
    changed = deepcopy(histories)
    for frame in changed.values():
        frame.loc[frame.Date >= "2020-02-03", "Close"] = 1000000
    assert monthly_selection(changed, at, settings) == before


def test_membership_stays_fixed_during_month_but_current_price_advances(settings, histories):
    before = monthly_selection(histories, datetime(2020, 2, 3), settings)
    changed = deepcopy(histories)
    changed["BBB"].loc[changed["BBB"].Date >= "2020-02-01", "Close"] = 1000
    after = monthly_selection(changed, datetime(2020, 2, 20), settings)
    assert before["selected"] == after["selected"]
    assert before["scores"] == after["scores"]
    assert after["prices"]["BBB"] == 1000
    march = monthly_selection(changed, datetime(2020, 3, 2), settings)
    assert march["selected"][0] == "BBB"


def test_all_negative_means_cash_and_sell_signals(settings, histories):
    histories = {s: histories["BBB"].copy() for s in settings["universe_symbols"]}
    assert monthly_selection(histories, datetime(2020, 2, 3), settings)["selected"] == []
    expert = object.__new__(ETFTrend)
    provider = SimpleNamespace(get_ohlcv_data=lambda symbol, **kw: histories[symbol])
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    for symbol in settings["universe_symbols"]:
        rec = expert.analyze_as_of(datetime(2020, 2, 3), BacktestContext(
            providers=bundle, settings=settings, extra={"symbol": symbol}))
        assert rec.signal == OrderRecommendation.SELL
        assert rec.expected_profit_percent == 0.0
        assert rec.current_price > 0


def test_fewer_eligible_than_slots_does_not_fill_with_bad_trend(settings, histories):
    histories["CCC"] = histories["BBB"].copy()
    assert monthly_selection(histories, datetime(2020, 2, 3), settings)["selected"] == ["AAA"]


@pytest.mark.parametrize("defect", ["missing", "zero", "nan", "duplicate", "short", "stale"])
def test_bad_data_fails_instead_of_inventing_a_rank(settings, histories, defect):
    frame = histories["AAA"]
    if defect == "missing":
        histories["AAA"] = None
    elif defect in ("zero", "nan"):
        frame.loc[0, "Close"] = 0 if defect == "zero" else float("nan")
    elif defect == "duplicate":
        histories["AAA"] = pd.concat([frame, frame.iloc[:1]])
    elif defect == "short":
        histories["AAA"] = frame[frame.Date >= "2020-01-01"]
    elif defect == "stale":
        histories["AAA"] = frame[frame.Date < "2019-12-01"]
    with pytest.raises(ValueError):
        monthly_selection(histories, datetime(2020, 2, 3), settings)


def test_ties_and_timezone_boundary_are_deterministic(settings, histories):
    histories["CCC"] = histories["AAA"].copy()
    settings["universe_symbols"] = ["CCC", "BBB", "AAA"]
    settings["top_n"] = 1
    result = monthly_selection(histories, datetime(2020, 2, 1, 0, 30, tzinfo=timezone.utc), settings)
    assert result["selected"] == ["AAA"]
    assert result["selection_month"] == "2020-01-01"  # still January in New York


def test_live_and_backtest_use_the_same_signal(monkeypatch, settings, histories):
    module = importlib.import_module("ba2_experts.ETFTrend")
    at = datetime(2020, 2, 3, 14, 30, tzinfo=timezone.utc)
    class Clock:
        @staticmethod
        def now(tz):
            return at
    monkeypatch.setattr(module, "datetime", Clock)
    captured = []
    monkeypatch.setattr(module, "add_instance", lambda row: captured.append(row))
    monkeypatch.setattr(module, "update_instance", lambda row: None)
    expert = object.__new__(ETFTrend)
    expert.id = 1
    expert.logger = SimpleNamespace(error=lambda *a, **kw: pytest.fail(str(a)))
    provider = SimpleNamespace(get_ohlcv_data=lambda symbol, **kw: histories[symbol])
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    expert._live_providers = lambda: bundle
    expert._resolve_settings = lambda keys: settings
    analysis = SimpleNamespace(id=10, subtype=None, status=None, state={})
    expert.run_analysis("AAA", analysis)
    rec = expert.analyze_as_of(at, BacktestContext(providers=bundle, settings=settings, extra={"symbol": "AAA"}))
    assert captured[0].recommended_action == rec.signal
    assert captured[0].price_at_date == rec.current_price
    assert captured[0].data["ETFTrend"] == rec.raw_outputs


def test_real_engine_opens_selected_fund_through_shared_rules():
    """The expert must actually reach a fill with zero expected-profit estimate."""
    import logging
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params
    from tools.strategy_research.profiles import build_manifest
    from tests.backtest.fixtures.e2e_support import hermetic_providers
    job = build_manifest(families=["etf_trend"])["jobs"][0]
    bt = job["optimization_config"]["backtest"]
    bt.update(backtest_id="etf-rules-fill", start_date="2024-02-01", end_date="2024-02-16",
              warmup_days=30, enabled_instruments=["AAPL", "MSFT"], execution_interval="1d",
              run_schedule_override=None, manage_schedule_override=None)
    bt["experts"][0]["settings"].update(universe_symbols=["AAPL", "MSFT"], momentum_bars=5,
                                         trend_bars=5, top_n=1)
    decoded = decode_params(SimpleNamespace(**job["strategy"]), {})
    config = _build_daily_trial_config(bt, decoded)
    before = logging.root.manager.disable
    try:
        logging.disable(logging.INFO)
        with hermetic_providers():
            result = run_daily_backtest(config)
    finally:
        logging.disable(before)
    assert result["total_trades"] >= 1
    assert {t["symbol"] for t in result["trades"]} == {"AAPL"}
    assert result["final_equity"] > 10000
