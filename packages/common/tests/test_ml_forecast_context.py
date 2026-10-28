"""The ONE context-slicing function shared by the store builder and (later) the live expert."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.ml_forecast import context as cx


def bars(n, start="2022-01-03"):
    d = pd.bdate_range(start, periods=n)
    return d.to_numpy().astype("datetime64[D]"), 100.0 + np.arange(n, dtype=float)


def test_context_ends_at_session_and_never_reads_later_bars():
    d, c = bars(700)
    s = date(2024, 1, 5)
    ctx, why = cx.context_for(d, c, s, 512)
    assert why is None and len(ctx) == 512
    i = int(np.searchsorted(d, np.datetime64(s, "D")))
    assert ctx[-1] == c[i]
    c2 = c.copy()
    c2[i + 1:] = 1e9
    np.testing.assert_array_equal(cx.context_for(d, c2, s, 512)[0], ctx)


def test_reasons():
    d, c = bars(300)
    assert cx.context_for(d, c, date(2022, 1, 8), 10) == (None, "no_bar")          # a Saturday
    assert cx.context_for(d, c, date(2022, 6, 1), 512) == (None, "short")
    assert cx.context_for(d, c, date(2030, 1, 1), 10) == (None, "no_bar")


def test_implausible_calendar_gap_is_its_own_reason():
    d, c = bars(700)
    keep = np.ones(len(d), bool)
    keep[400:430] = False                 # 30 missing sessions inside the window of a late session
    d2, c2 = d[keep], c[keep]
    s = pd.Timestamp(d2[600]).date()
    assert cx.context_for(d2, c2, s, 512) == (None, "gap")
    keep = np.ones(len(d), bool)
    keep[400:403] = False                 # a few missing sessions (holiday-like) are fine
    assert cx.context_for(d[keep], c[keep], pd.Timestamp(d[keep][600]).date(), 512)[1] is None


def test_loader_rejects_tz_aware_dates_and_duplicates(tmp_path):
    p = tmp_path / cx.SOURCE_DIRS["alpaca_build"]
    p.mkdir()
    dates = pd.bdate_range("2024-01-01", periods=5, tz="UTC")
    pd.DataFrame({"Date": dates, "Close": np.arange(5.0) + 1}).to_parquet(p / "AAA_1d.parquet")
    with pytest.raises(ValueError, match="tz-naive"):
        cx.load_daily_series(tmp_path, "AAA")
    pd.DataFrame({"Date": pd.to_datetime(["2024-01-02"] * 2), "Close": [1.0, 2.0]}).to_parquet(p / "BBB_1d.parquet")
    with pytest.raises(ValueError, match="duplicate"):
        cx.load_daily_series(tmp_path, "BBB")
    with pytest.raises(ValueError, match="unknown history source"):
        cx.load_daily_series(tmp_path, "AAA", source="fmp")


def test_provenance_records_source_manifest_hash_and_version(tmp_path):
    (tmp_path / cx.SOURCE_DIRS["alpaca_build"] / "_meta").mkdir(parents=True)
    mf = tmp_path / cx.SOURCE_DIRS["alpaca_build"] / "_meta" / "build_manifest.json"
    mf.write_text(json.dumps({"built_at": "x"}))
    pv = cx.history_provenance(tmp_path)
    import hashlib
    assert pv == {"history_source": "alpaca_build", "build_manifest_sha256": hashlib.sha256(mf.read_bytes()).hexdigest(),
                  "context_function_version": cx.CONTEXT_FUNCTION_VERSION}
    mf.unlink()
    with pytest.raises(FileNotFoundError):
        cx.history_provenance(tmp_path)


# ---------------------------------------------------------------- OHLCV (Kronos)
def _write_ohlcv(root, sym, n=30, start="2024-01-02", extra=True):
    p = root / cx.SOURCE_DIRS["alpaca_build"]
    p.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(start, periods=n)
    c = 100.0 + np.arange(n, dtype=float)
    df = pd.DataFrame({"Date": dates, "Open": c - 0.5, "High": c + 1, "Low": c - 1, "Close": c,
                       "Volume": np.arange(n) + 1000})
    if extra:
        df["effective_date"] = dates
    df.to_parquet(p / f"{sym}_1d.parquet")
    return dates


def test_ohlcv_loader_shape_order_and_checks(tmp_path):
    dates = _write_ohlcv(tmp_path, "AAA")
    d, v = cx.load_daily_ohlcv_series(tmp_path, "AAA")
    assert v.shape == (30, 5) and v.dtype == np.float64 and cx.OHLCV_COLUMNS == ("Open", "High", "Low", "Close", "Volume")
    assert d[0] == np.datetime64("2024-01-02") and np.all(np.diff(d.astype("int64")) > 0)
    np.testing.assert_array_equal(v[:, 3], 100.0 + np.arange(30))          # Close is column 3
    np.testing.assert_array_equal(v[:, 4], 1000.0 + np.arange(30))
    # the closes loader agrees with the Close column
    np.testing.assert_array_equal(cx.load_daily_series(tmp_path, "AAA")[1], v[:, 3])
    p = tmp_path / cx.SOURCE_DIRS["alpaca_build"]
    pd.DataFrame({"Date": pd.bdate_range("2024-01-01", periods=3, tz="UTC"), "Open": 1.0, "High": 1.0, "Low": 1.0,
                  "Close": 1.0, "Volume": 1.0}).to_parquet(p / "TZ_1d.parquet")
    with pytest.raises(ValueError, match="tz-naive"):
        cx.load_daily_ohlcv_series(tmp_path, "TZ")
    pd.DataFrame({"Date": pd.to_datetime(["2024-01-02"] * 2), "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
                  "Volume": 1.0}).to_parquet(p / "DUP_1d.parquet")
    with pytest.raises(ValueError, match="duplicate"):
        cx.load_daily_ohlcv_series(tmp_path, "DUP")
    with pytest.raises(ValueError, match="unknown history source"):
        cx.load_daily_ohlcv_series(tmp_path, "AAA", source="fmp")


def test_ohlcv_window_is_the_closes_window_with_its_dates():
    d, c = bars(700)
    ohlcv = np.column_stack([c - 0.5, c + 1, c - 1, c, np.full(len(c), 5.0)])
    s = date(2024, 1, 5)
    wd, win, why = cx.ohlcv_context_for(d, ohlcv, s, 512)
    closes, _ = cx.context_for(d, c, s, 512)
    assert why is None and win.shape == (512, 5) and len(wd) == 512
    np.testing.assert_array_equal(win[:, 3], closes)
    assert wd[-1] == np.datetime64(s, "D") and np.all(np.diff(wd.astype("int64")) > 0)
    i = int(np.searchsorted(d, np.datetime64(s, "D")))
    np.testing.assert_array_equal(wd, d[i - 511:i + 1])


def test_ohlcv_window_never_reads_later_bars():
    d, c = bars(700)
    ohlcv = np.column_stack([c, c, c, c, c])
    s = date(2024, 1, 5)
    i = int(np.searchsorted(d, np.datetime64(s, "D")))
    poisoned = ohlcv.copy()
    poisoned[i + 1:] = 1e9
    wd1, w1, _ = cx.ohlcv_context_for(d, ohlcv, s, 512)
    wd2, w2, _ = cx.ohlcv_context_for(d, poisoned, s, 512)
    np.testing.assert_array_equal(w1, w2)
    np.testing.assert_array_equal(wd1, wd2)


def test_ohlcv_reasons_and_shape_check():
    d, c = bars(300)
    ohlcv = np.column_stack([c, c, c, c, c])
    assert cx.ohlcv_context_for(d, ohlcv, date(2022, 1, 8), 10) == (None, None, "no_bar")
    assert cx.ohlcv_context_for(d, ohlcv, date(2022, 6, 1), 512) == (None, None, "short")
    with pytest.raises(ValueError, match="ohlcv must be"):
        cx.ohlcv_context_for(d, c, date(2022, 6, 1), 10)
    with pytest.raises(ValueError, match="ohlcv must be"):
        cx.ohlcv_context_for(d[:-1], ohlcv, date(2022, 6, 1), 10)
