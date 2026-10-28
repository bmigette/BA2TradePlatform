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


# ---------------------------------------------------------------------------------------------- Task 1.2
SYMS = ("AAA", "BBB")
CUT_DAY = date(2023, 11, 15)                       # a Wednesday: the truncated build ends MID-WEEK


def _build(cache, groups, symbols=SYMS, first=FIRST, last=LAST, ctx=CTX, sector_of=None):
    return F.build_features(list(symbols), first, last, list(groups), cache, list(ctx), sector_of=sector_of)


def _assert_cut_identical(cache, tmp_path, groups, day=CUT_DAY, symbols=SYMS, sector_of=None):
    """THE point-in-time proof: a build over a cache cut after ``day`` equals the full build on every common row."""
    full = _build(cache, groups, symbols, sector_of=sector_of)
    cut = _build(_truncate(cache, tmp_path, day), groups, symbols, last=day, sector_of=sector_of)
    common = full[full.index.get_level_values("session") <= pd.Timestamp(day)]
    assert len(common) == len(cut) > 300 * len(symbols) // 2
    assert common.index.equals(cut.index) and list(common.columns) == list(cut.columns)
    pd.testing.assert_frame_equal(common, cut, check_exact=True)      # every column, bit for bit
    feats = F.feature_columns(cut)
    assert feats and cut[feats].notna().all().all()                   # the comparison is not NaN == NaN
    return full


def test_cut_test_t_and_mtf(cache, tmp_path):
    _assert_cut_identical(cache, tmp_path, ["t", "mtf"])


def test_cut_test_detects_a_look_ahead(cache, tmp_path, monkeypatch):
    """The cut test is real: a builder that reads tomorrow's close FAILS it."""
    def leaky(daily, ctx):
        frames = {s: pd.DataFrame({"l_next_ret": np.log(daily[s]["close"].shift(-1) / daily[s]["close"])})
                  for s in ctx.symbols}
        return pd.concat(frames, names=["symbol", "session"])
    monkeypatch.setitem(F.GROUPS, "l", leaky)
    with pytest.raises(AssertionError):
        _assert_cut_identical(cache, tmp_path, ["l"])


def test_build_features_shape_and_no_nan(cache):
    f = _build(cache, ["t", "mtf"])
    assert f.index.names == ["symbol", "session"] and list(f.index.get_level_values("symbol").unique()) == list(SYMS)
    sess = f.index.get_level_values("session")
    exp = _sessions(FIRST, LAST)
    assert sess.min() == exp[0] and sess.max() == exp[-1] and len(f) == len(SYMS) * len(exp)
    assert list(f.columns[:1]) == ["listed"] and f["listed"].all()
    feats = F.feature_columns(f)
    assert feats == F.feature_columns(f, ["t", "mtf"])
    assert all(c.startswith(("t_", "mtf_")) for c in feats)
    assert f[feats].notna().all().all() and np.isfinite(f[feats].to_numpy()).all()
    assert set(F.feature_columns(_build(cache, ["mtf"]))) == {c for c in feats if c.startswith("mtf_")}


def test_t_columns_are_exactly_the_documented_set(cache):
    f = _build(cache, ["t"])
    assert F.feature_columns(f) == list(F.T_COLUMNS)
    assert F.feature_columns(_build(cache, ["mtf"])) == list(F.MTF_COLUMNS)


def test_features_are_scale_free(cache, tmp_path):
    """Multiplying every price by a constant leaves every feature unchanged: no column is a price level."""
    root = tmp_path / "scaled"
    shutil.copytree(cache, root)
    for p in (root / "AlpacaHistoryOHLCVProvider").glob("*_1d.parquet"):
        df = pd.read_parquet(p)
        df[["Open", "High", "Low", "Close"]] *= 7.3
        df.to_parquet(p, index=False)
    a, b = _build(cache, ["t", "mtf"]), _build(root, ["t", "mtf"])
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-8, atol=1e-10)


def _raw(cache, sym):
    return pd.read_parquet(cache / "AlpacaHistoryOHLCVProvider" / f"{sym}_1d.parquet").set_index("Date")


def test_t_values_against_independent_formulas(cache):
    f = _build(cache, ["t"]).loc["AAA"]
    r = _raw(cache, "AAA")
    c, h, l, v = r["Close"], r["High"], r["Low"], r["Volume"].astype(float)
    lr = np.log(c)
    at = f.index[200]
    i = r.index.get_loc(at)
    assert f.loc[at, "t_ret_1"] == pytest.approx(lr.iloc[i] - lr.iloc[i - 1])
    assert f.loc[at, "t_ret_60"] == pytest.approx(lr.iloc[i] - lr.iloc[i - 60])
    d1 = lr.diff()
    assert f.loc[at, "t_rvol_20"] == pytest.approx(d1.iloc[i - 19:i + 1].std())
    assert f.loc[at, "t_rvol_60"] == pytest.approx(d1.iloc[i - 59:i + 1].std())
    atr = P._wilder(P._true_range(h, l, c), 14)
    assert f.loc[at, "t_atr_pct"] == pytest.approx(atr.iloc[i] / c.iloc[i])
    for n in (20, 50, 200):
        assert f.loc[at, f"t_dist_sma{n}"] == pytest.approx((c.iloc[i] - c.iloc[i - n + 1:i + 1].mean()) / atr.iloc[i])
    w = c.iloc[i - 19:i + 1]
    assert f.loc[at, "t_bb_pctb"] == pytest.approx((c.iloc[i] - (w.mean() - 2 * w.std())) / (4 * w.std()) - 0.5)
    assert f.loc[at, "t_bb_width"] == pytest.approx(4 * w.std() / w.mean())
    for n in (20, 55):
        hi, lo = h.iloc[i - n + 1:i + 1].max(), l.iloc[i - n + 1:i + 1].min()
        assert f.loc[at, f"t_donch_{n}"] == pytest.approx((c.iloc[i] - lo) / (hi - lo) - 0.5)
    pivot_prev = (h.iloc[i - 1] + l.iloc[i - 1] + c.iloc[i - 1]) / 3
    assert f.loc[at, "t_pivot_dist"] == pytest.approx((c.iloc[i] - pivot_prev) / atr.iloc[i])
    lv = np.log1p(v)
    assert f.loc[at, "t_vol_z"] == pytest.approx((lv.iloc[i] - lv.iloc[i - 59:i + 1].mean()) / lv.iloc[i - 59:i + 1].std())
    macd = P._ema(c, 12) - P._ema(c, 26)
    assert f.loc[at, "t_macd_hist"] == pytest.approx((macd.iloc[i] - P._ema(macd, 9).iloc[i]) / atr.iloc[i])
    # OBV slope: least-squares slope of OBV over 20 days / 20-day mean volume
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    slope = np.polyfit(np.arange(20), obv.iloc[i - 19:i + 1].to_numpy(), 1)[0]
    assert f.loc[at, "t_obv_slope"] == pytest.approx(slope / v.iloc[i - 19:i + 1].mean())
    # bounded oscillators
    assert f["t_rsi"].between(-0.5, 0.5).all() and f["t_bb_pctb"].between(-1.5, 1.5).all()
    assert f["t_donch_20"].between(-0.5, 0.5).all() and f["t_donch_55"].between(-0.5, 0.5).all()
    assert f["t_adx"].between(0, 1).all()


def test_rsi_is_wilder_and_defined_on_a_one_way_run():
    c = pd.Series(np.arange(1.0, 60.0))                       # only gains: RSI is 100, not NaN
    assert F._rsi_scaled(c).iloc[-1] == pytest.approx(0.5)
    flat = pd.Series(np.ones(60))
    assert F._rsi_scaled(flat).iloc[-1] == pytest.approx(0.0)  # no movement: neutral 50
    rng = np.random.default_rng(0)
    x = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    np.testing.assert_allclose(F._rsi_scaled(x).dropna(), P._rsi(x).dropna() / 100 - 0.5, rtol=1e-9)


def _raw_frame(n):
    b = _bars(_sessions(date(2021, 1, 4), date(2023, 12, 29))[:n], 42).set_index("Date")
    return b.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]


def test_parabolic_sar_matches_the_indicator_library():
    from app.indicators import TechnicalIndicators
    r = _raw_frame(400)
    ref = TechnicalIndicators.calculate_sar(r.rename(columns=str.title), 0.02, 0.2).to_numpy()
    np.testing.assert_allclose(F._parabolic_sar(r["high"].to_numpy(), r["low"].to_numpy()), ref, rtol=1e-12)


def test_parabolic_sar_textbook_cases():
    # a steady rise: SAR trails below the lows and accelerates towards price
    high = np.array([10, 11, 12, 13, 14, 15, 16.0])
    low = high - 1
    sar = F._parabolic_sar(high, low)
    assert (sar[1:] < low[1:]).all() and (np.diff(sar[1:]) >= 0).all() and sar[-1] > sar[1]
    # a collapse flips it above the bars: SAR jumps to the prior extreme point
    high2 = np.r_[high, 8.0, 7.0]
    low2 = np.r_[low, 6.5, 5.5]
    sar2 = F._parabolic_sar(high2, low2)
    assert sar2[7] == pytest.approx(16.0) and sar2[8] > high2[8]


def test_adx_is_high_in_a_trend_low_in_chop():
    n = 200
    up = pd.DataFrame({"high": np.linspace(100, 200, n) + 1, "low": np.linspace(100, 200, n) - 1,
                       "close": np.linspace(100, 200, n)})
    chop = pd.DataFrame({"high": 101 + (np.arange(n) % 2) * 0.5, "low": 99 + (np.arange(n) % 2) * 0.5,
                         "close": 100 + (np.arange(n) % 2) * 0.5})
    assert F._adx(up["high"], up["low"], up["close"]).iloc[-1] > 80
    assert F._adx(chop["high"], chop["low"], chop["close"]).iloc[-1] < 20


def test_mtf_weekly_values_attach_only_after_the_week_ends(cache):
    f = _build(cache, ["mtf"]).loc["AAA"]
    r = _raw(cache, "AAA")
    wc = r["Close"].groupby(r.index - pd.to_timedelta(r.index.weekday, unit="D")).last()    # weekly closes (week = Mon..)
    weeks = list(wc.index)
    wk = pd.Timestamp("2023-06-12")                       # an ordinary Mon..Fri week
    k = weeks.index(wk)
    sess = f.index[(f.index >= wk) & (f.index <= wk + pd.Timedelta(days=4))]
    assert len(sess) == 5
    prev_week_ret4 = np.log(wc.iloc[k - 1] / wc.iloc[k - 5])
    this_week_ret4 = np.log(wc.iloc[k] / wc.iloc[k - 4])
    for s in sess:                                         # incl. the week's LAST session (Friday)
        assert f.loc[s, "mtf_w_ret_4"] == pytest.approx(prev_week_ret4)
    assert sess[-1].weekday() == 4 and f.loc[sess[-1], "mtf_w_ret_4"] != pytest.approx(this_week_ret4)
    nxt = f.index[f.index > sess[-1]][0]                   # the next week's first session sees the finished week
    assert f.loc[nxt, "mtf_w_ret_4"] == pytest.approx(this_week_ret4)
    assert f.loc[sess[0], "mtf_w_ret_13"] == pytest.approx(np.log(wc.iloc[k - 1] / wc.iloc[k - 14]))


def test_mtf_week_ends_at_its_last_session_when_friday_is_a_holiday(cache):
    f = _build(cache, ["mtf"]).loc["AAA"]
    # Good Friday 2023-04-07: the week of 2023-04-03 ends Thursday 04-06; Monday 04-10 sees that week
    thu, mon = pd.Timestamp("2023-04-06"), pd.Timestamp("2023-04-10")
    assert pd.Timestamp("2023-04-07") not in f.index and thu in f.index
    assert f.loc[thu, "mtf_w_ret_4"] != pytest.approx(f.loc[mon, "mtf_w_ret_4"])
    r = _raw(cache, "AAA")
    wc = r["Close"].groupby(r.index - pd.to_timedelta(r.index.weekday, unit="D")).last()
    k = list(wc.index).index(pd.Timestamp("2023-04-03"))
    assert f.loc[mon, "mtf_w_ret_4"] == pytest.approx(np.log(wc.iloc[k] / wc.iloc[k - 4]))


def test_late_listed_symbol_is_nan_and_unlisted_before_its_first_bar(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    late = s[s >= pd.Timestamp("2023-02-01")]
    _write(root, "LATE", _bars(late, 9))
    f = _build(root, ["t", "mtf"], symbols=("LATE", "AAA")).loc["LATE"]
    feats = F.feature_columns(f)
    pre = f.index < pd.Timestamp("2023-02-01")
    assert pre.sum() > 100 and not f.loc[pre, "listed"].any() and f.loc[pre, feats].isna().all().all()
    # warm-up NaN right after listing, then the long windows fill in: listed rows are NaN-free only once warm
    assert f.loc[~pre, "listed"].all() and f.loc[~pre, feats].isna().any().any()
    assert f.loc[f.index >= pd.Timestamp("2024-02-01"), feats].notna().all().all()


def test_unknown_group_and_missing_symbol_refuse(cache):
    with pytest.raises(ValueError, match="unknown"):
        _build(cache, ["t", "nope"])
    with pytest.raises(P.PanelDataError, match="ZZZ"):
        _build(cache, ["t"], symbols=("AAA", "ZZZ"))
    with pytest.raises(ValueError):
        _build(cache, [])
