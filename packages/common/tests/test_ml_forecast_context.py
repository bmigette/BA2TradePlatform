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
