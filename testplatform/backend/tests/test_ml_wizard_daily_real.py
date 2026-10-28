"""The daily wizard source on the REAL ``build_features`` (synthetic cache): FRED is only ever read through the
store's own hermetic reader (docs/plans, Phase 7 / Task 7.1)."""
import inspect
from datetime import datetime

import pandas as pd
import pytest

from ba2_providers import fmp_common
from ba2_providers.macro import fred_series as fs

from app.services import ml_dataset_source as S
from app.services import ml_features as F
from app.services import ml_wizard_targets as W
from tests import test_ml_features as TF
from tests import test_ml_features_macro as TM
from tests.test_ml_universe import write_metric_store


@pytest.fixture(scope="module")
def wiz_cache(tmp_path_factory):
    root = tmp_path_factory.mktemp("wizcache")
    s = TF._sessions()
    for i, sym in enumerate(("AAA", "BBB") + TF.CTX):
        TF._write(root, sym, TF._bars(s, 100 + i))
    write_metric_store(root / "screener" / "metric_store_alpaca_20261022", TF._store_rows())
    TM._synthetic_fred(root)
    return root


@pytest.fixture(autouse=True)
def _env(monkeypatch, wiz_cache):
    monkeypatch.setattr(F, "FRED_PINS", {})                      # the synthetic FRED cache is not the pinned real one
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 2)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.0)
    TM.point_fred_at(monkeypatch, wiz_cache)
    monkeypatch.setattr("ba2_common.config.CACHE_FOLDER", str(wiz_cache))
    S.clear_daily_memo()
    yield
    S.clear_daily_memo()
    fs.reset_cache()


def _fetch(groups, ticker="AAA", min_share=0.7):
    return S.fetch_ohlcv_frame("alpaca_hist", ticker, "1d", datetime(2022, 6, 1), datetime(2024, 3, 28), [], None,
                               {"daily_groups": groups, "daily_min_share": min_share,
                                "daily_universe": ["AAA", "BBB"]})


def test_mac_is_read_only_inside_the_hermetic_context(monkeypatch):
    seen = {"reads": 0}
    inner = fs.get_series_as_of

    def guarded(series_id, as_of):
        seen["reads"] += 1
        assert fmp_common._is_hermetic_fmp_history(), "a FRED read outside hermetic_fmp_history"
        return inner(series_id, as_of)

    def boom(*a, **k):
        raise AssertionError("the FRED reader went to the network")
    monkeypatch.setattr(fs, "get_series_as_of", guarded)
    monkeypatch.setattr(fs, "_fill_cache_on_the_live_path", boom)
    monkeypatch.setattr(fs, "refresh_series", boom)
    df = _fetch(["t", "mac"])
    assert seen["reads"] > 0
    assert any(c.startswith("mac_") for c in df.columns) and any(c.startswith("t_") for c in df.columns)
    assert df["Date"].iloc[0] >= pd.Timestamp("2022-06-01") and df["filled"].dtype == bool
    assert not df[["Open", "High", "Low", "Close"]].isna().any().any()


def test_without_mac_the_fred_reader_is_never_touched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("FRED was read although the mac group is off")
    monkeypatch.setattr(fs, "get_series_as_of", boom)
    monkeypatch.setattr(fs, "fred_series", boom, raising=False)
    df = _fetch(["t", "mtf", "m"])
    assert not any(c.startswith("mac_") for c in df.columns) and any(c.startswith("m_") for c in df.columns)


def test_the_wizard_modules_never_call_the_fred_readers_themselves():
    for mod in (S, W):
        src = inspect.getsource(mod)
        assert "fred_series" not in src and "get_series_as_of" not in src
