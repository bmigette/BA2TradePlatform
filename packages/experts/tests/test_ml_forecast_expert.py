"""MLForecast expert: gene validation, store-alias resolution, entry mapping and the held-position flip, against a
temporary forecast store and fake providers (no DB, no torch)."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.ml_forecast import store
from ba2_common.core.ml_forecast.forecast_row import ForecastRow
from ba2_common.core.ml_forecast.model_card import ModelCard, model_key
from ba2_common.core.types import AnalysisUseCase, OrderRecommendation
import importlib

from ba2_experts.MLForecast import MLForecast, mapping_genes, resolve_model_key

mlf_mod = importlib.import_module("ba2_experts.MLForecast")   # the module (the package re-exports the class)

SETTINGS = {"model_key": "timesfm3-ctx512", "horizon": 10, "min_edge_pct": 1.0, "min_prob": 60.0,
            "max_uncertainty": 6.0, "tp_quantile": 70.0, "sl_quantile": 30.0, "exit_flip_prob": 60.0,
            "enable_sell": False}
CARD = ModelCard(model_id="google/timesfm-3.0-pytorch", family="timesfm", version="3.0.2", input_fields=("Close",),
                 timeframe="1d", context_length=512, past_only_covariates=(), past_future_covariates=(),
                 horizons=(5, 10, 20), scaler=None, output_type="deciles", pretraining_cutoff=None,
                 licence="timesfm-non-commercial-license-v1.0", licence_commercial_ok=False)
BULL = (-0.01, 0.0, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05)     # q50 +2%, P(up) ~0.8
BEAR = tuple(-x for x in reversed(BULL))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    mlf_mod._alias_memo.clear()
    store.reset_cache()
    key = model_key(CARD, ())
    rows = [ForecastRow("AAPL", date(2025, 3, 3), {h: BULL for h in (5, 10, 20)}),
            ForecastRow("AAPL", date(2025, 3, 4), {h: BEAR for h in (5, 10, 20)})]
    store.write_rows(key, CARD, {"card_id": "timesfm3-ctx512"}, rows, (), tmp_path)
    yield key
    store.reset_cache()


class _Ohlcv:
    def get_ohlcv_data(self, symbol, start_date, end_date, lookback_days, interval):
        d = pd.bdate_range("2025-01-02", "2025-03-04")
        c = np.linspace(100, 110, len(d))
        return pd.DataFrame({"Date": d.tz_localize("UTC"), "Open": c, "High": c + 1.0, "Low": c - 1.0, "Close": c,
                             "Volume": 1e6})


class _Providers:
    def ohlcv(self):
        return _Ohlcv()

    def price_at_date(self, symbol, as_of):
        return 110.0


def _expert():
    e = MLForecast.__new__(MLForecast)
    e._skips_logged = None
    return e


def _at(d):
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def test_alias_resolves_from_store_meta(cache):
    assert resolve_model_key("timesfm3-ctx512") == cache
    assert resolve_model_key(cache) == cache
    with pytest.raises(ValueError, match="matches 0 stores"):
        resolve_model_key("timesfm3-ctx2048")


def test_genes_validate_without_defaults():
    assert mapping_genes(SETTINGS).horizon == 10
    with pytest.raises(ValueError, match="horizon"):
        mapping_genes({**SETTINGS, "horizon": 7})
    bad = dict(SETTINGS)
    del bad["min_prob"]
    with pytest.raises(ValueError, match="Missing setting 'min_prob'"):
        mapping_genes(bad)


def test_entry_buy_carries_tp_distance_as_expected_profit(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 3)), "daily_expert")
    assert rec.signal == OrderRecommendation.BUY
    assert rec.raw_outputs["forecast_session"] == "2025-03-03"
    # tp_quantile 70 -> +3% of the decision price 110
    assert rec.target_price == pytest.approx(110.0 * 1.03, rel=1e-6)
    assert rec.expected_profit_percent == pytest.approx(3.0, rel=1e-5)
    assert 1 <= rec.confidence <= 95


def test_row_of_the_decision_day_is_visible_on_the_daily_clock_only_from_that_day(cache):
    # 2025-03-04 (bearish row) decides on the bearish row, not on the 03-03 bullish one
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 4)), "daily_expert")
    assert rec.signal == OrderRecommendation.HOLD and "sell" in rec.details.lower()


def test_held_position_flips_on_the_bearish_row(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 4)), AnalysisUseCase.OPEN_POSITIONS)
    assert rec.signal == OrderRecommendation.SELL
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 3, 3)), AnalysisUseCase.OPEN_POSITIONS)
    assert rec.signal == OrderRecommendation.BUY      # bullish p >= flip: a held long just stays (no exit)


def test_missing_row_is_a_skip_never_a_default(cache):
    rec = _expert()._analyze("AAPL", _Providers(), SETTINGS, _at(date(2025, 2, 1)), "daily_expert")
    assert rec.skip and rec.skip_reason == "no_forecast"
