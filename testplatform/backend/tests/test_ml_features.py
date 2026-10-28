"""Point-in-time DAILY feature store (app/services/ml_features.py).

The decision clock is the session close: a row for session S may only use data public at S's close. The CUT TEST is
the proof for every group: build from a cache truncated after day D and every common row must be identical."""
import shutil
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
from app.services import ml_panel as P

FIRST_BAR, LAST_BAR = date(2021, 1, 4), date(2024, 3, 28)     # what the synthetic cache holds
FIRST, LAST = date(2022, 6, 1), date(2024, 3, 28)             # what the builds ask for (>420 days of warm-up cached)
CTX = ("SPY", "QQQ", "IWM", "TLT", "GLD", "XLK", "XLF")
SECTOR_OF = {"AAA": "Technology", "BBB": "Financial Services", "CCC": "Technology"}


def _sessions(first=FIRST_BAR, last=LAST_BAR):
    return pd.DatetimeIndex(pd.to_datetime(P.regular_sessions(first, last)["session"]))


def _bars(sessions, seed, drift=0.0003):
    rng = np.random.default_rng(seed)
    n = len(sessions)
    r = rng.normal(drift, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(r))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = rng.integers(500_000, 5_000_000, n).astype("int64")
    df = pd.DataFrame({"Date": sessions, "Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol})
    df["effective_date"] = df["Date"]
    return df


def _write(root, sym, df):
    d = root / "AlpacaHistoryOHLCVProvider"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{sym}_1d.parquet", index=False)


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    root = tmp_path_factory.mktemp("cache1d")
    s = _sessions()
    for i, sym in enumerate(("AAA", "BBB", "CCC") + CTX):
        _write(root, sym, _bars(s, 100 + i))
    return root


def _truncate(cache, tmp_path, day):
    """A copy of ``cache`` with every daily file cut after ``day`` (the later bars are really gone)."""
    trunc = tmp_path / "cache"
    shutil.copytree(cache, trunc)
    for p in (trunc / "AlpacaHistoryOHLCVProvider").glob("*_1d.parquet"):
        df = pd.read_parquet(p)
        kept = df[df["Date"] <= pd.Timestamp(day)]
        assert len(kept) < len(df)
        kept.to_parquet(p, index=False)
    return trunc


# ---------------------------------------------------------------------------------------------- Task 1.1
def test_load_daily_aligns_to_nyse_sessions(cache):
    daily, meta = F.load_daily(["AAA", "SPY"], date(2023, 1, 3), date(2023, 3, 31), cache)
    exp = _sessions(date(2023, 1, 3), date(2023, 3, 31))
    for sym in ("AAA", "SPY"):
        d = daily[sym]
        assert d.index.equals(exp) and d.index.name == "session"
        assert list(d.columns) == ["open", "high", "low", "close", "volume", "listed", "filled"]
        assert d["listed"].all() and not d["filled"].any() and not d[["open", "close"]].isna().any().any()
    assert meta["filled"] == {} and meta["first_bar"]["AAA"] == "2021-01-04"
    raw = pd.read_parquet(cache / "AlpacaHistoryOHLCVProvider" / "AAA_1d.parquet").set_index("Date")
    assert daily["AAA"]["close"].iloc[0] == raw.loc[pd.Timestamp("2023-01-03"), "Close"]


def test_load_daily_missing_file_refuses(cache):
    with pytest.raises(P.PanelDataError, match="NOPE"):
        F.load_daily(["AAA", "NOPE"], date(2023, 1, 3), date(2023, 3, 31), cache)


def test_load_daily_late_listing_is_nan_and_not_listed(cache, tmp_path):
    root = tmp_path / "c"
    s = _sessions()
    late = s[s >= pd.Timestamp("2023-02-01")]
    _write(root, "LATE", _bars(late, 5))
    daily, meta = F.load_daily(["LATE"], date(2023, 1, 3), date(2023, 3, 31), root)
    d = daily["LATE"]
    pre = d.index < pd.Timestamp("2023-02-01")
    assert pre.sum() > 10
    assert not d.loc[pre, "listed"].any() and d.loc[pre, ["open", "high", "low", "close", "volume"]].isna().all().all()
    assert d.loc[~pre, "listed"].all() and not d.loc[~pre, ["open", "close", "volume"]].isna().any().any()
    assert meta["first_bar"]["LATE"] == "2023-02-01" and meta["filled"] == {}


def test_load_daily_cache_older_than_range_is_listed_from_the_first_session(cache):
    daily, _ = F.load_daily(["AAA"], date(2023, 1, 3), date(2023, 1, 31), cache)
    assert daily["AAA"]["listed"].all()


def test_load_daily_small_gap_is_filled_flat_and_listed(tmp_path):
    root = tmp_path / "c"
    s = _sessions(date(2020, 1, 2), date(2024, 3, 28))                 # > 1000 sessions: 1 gap <= 0.1%
    assert len(s) > 1000
    df = _bars(s, 7)
    gap = df["Date"].iloc[500]
    _write(root, "GAP", df[df["Date"] != gap])
    daily, meta = F.load_daily(["GAP"], date(2020, 1, 2), date(2024, 3, 28), root)
    d = daily["GAP"]
    assert meta["filled"] == {"GAP": [str(gap.date())]}
    row, prev = d.loc[gap], d.iloc[499]
    assert d["filled"].sum() == 1 and row["listed"] and row["volume"] == 0.0
    assert row["open"] == row["high"] == row["low"] == row["close"] == prev["close"]


def test_load_daily_too_many_gaps_refuses(tmp_path):
    root = tmp_path / "c"
    s = _sessions(date(2020, 1, 2), date(2024, 3, 28))
    df = _bars(s, 7)
    _write(root, "GAPS", df.drop(index=[500, 800]))                    # 2 of > 1000 = > 0.1%
    with pytest.raises(P.PanelDataError, match="GAPS"):
        F.load_daily(["GAPS"], date(2020, 1, 2), date(2024, 3, 28), root)


def test_load_daily_gap_share_counts_from_the_listing(tmp_path):
    """Sessions before the symbol existed are not 'missing': 2 gaps in a short listed life are still refused, and
    no gap at all is fine however late the listing."""
    root = tmp_path / "c"
    s = _sessions(date(2022, 1, 3), date(2024, 3, 28))
    df = _bars(s[s >= pd.Timestamp("2023-06-01")], 3)
    _write(root, "OK", df)
    F.load_daily(["OK"], date(2022, 1, 3), date(2024, 3, 28), root)
    _write(root, "BAD", df.drop(index=[40, 80]))
    with pytest.raises(P.PanelDataError):
        F.load_daily(["BAD"], date(2022, 1, 3), date(2024, 3, 28), root)


def test_load_daily_bar_off_the_calendar_refuses(tmp_path):
    root = tmp_path / "c"
    df = _bars(_sessions(date(2023, 1, 3), date(2023, 3, 31)), 1)
    extra = df.iloc[[0]].copy()
    extra["Date"] = pd.Timestamp("2023-01-07")                         # a Saturday
    _write(root, "OFF", pd.concat([df, extra]).sort_values("Date"))
    with pytest.raises(P.PanelDataError, match="calendar"):
        F.load_daily(["OFF"], date(2023, 1, 3), date(2023, 3, 31), root)


def test_load_daily_refuses_nan_duplicate_and_tz_aware(tmp_path):
    root = tmp_path / "c"
    df = _bars(_sessions(date(2023, 1, 3), date(2023, 3, 31)), 1)
    nan = df.copy()
    nan.loc[10, "Close"] = np.nan
    _write(root, "NAN", nan)
    _write(root, "DUP", pd.concat([df, df.iloc[[5]]]).sort_values("Date"))
    tz = df.copy()
    tz["Date"] = tz["Date"].dt.tz_localize("UTC")
    _write(root, "TZ", tz)
    for sym in ("NAN", "DUP", "TZ"):
        with pytest.raises(P.PanelDataError, match=sym):
            F.load_daily([sym], date(2023, 1, 3), date(2023, 3, 31), root)
