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


@pytest.fixture(autouse=True)
def _small_cross_sections(monkeypatch):
    """The synthetic universes have 2-3 symbols; the production floors (>= 5 members, >= 50% of them) are tested apart."""
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 2)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.0)


STORE_SECTORS = {"AAA": "Technology", "BBB": "Financial Services", "CCC": "Technology"}
STORE_SCANS = [d.strftime("%Y-%m-%d") for d in pd.date_range("2021-06-02", "2024-03-27", freq="7D")]      # Wednesdays


def _store_rows(seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for scan in STORE_SCANS:
        for i, (sym, sector) in enumerate(STORE_SECTORS.items()):
            rows.append(dict(date=scan, symbol=sym, sector=sector, close=100.0, price=100.0,
                             market_cap=1e11 * (1 + i) * float(rng.uniform(0.9, 1.1)), volume=2e6,
                             relative_volume=float(rng.uniform(0.5, 2.0)), weinstein_stage=float(rng.integers(1, 5)),
                             momentum_12_1=float(rng.normal(0.1, 0.3))))
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    root = tmp_path_factory.mktemp("cache1d")
    s = _sessions()
    for i, sym in enumerate(("AAA", "BBB", "CCC") + CTX):
        _write(root, sym, _bars(s, 100 + i))
    from tests.test_ml_universe import write_metric_store
    write_metric_store(root / "screener" / "metric_store_alpaca_20261022", _store_rows())
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
    store = trunc / "screener" / "metric_store_alpaca_20261022"
    if store.exists():                                   # the scans dated after ``day`` are gone too
        for p in store.glob("ym=*/*.parquet"):
            df = pd.read_parquet(p)
            df[df["date"] <= pd.Timestamp(day).strftime("%Y-%m-%d")].to_parquet(p, index=False)
        shutil.rmtree(trunc / "screener" / "_derived", ignore_errors=True)
    return trunc


# ---------------------------------------------------------------------------------------------- Task 1.1
def test_load_daily_aligns_to_nyse_sessions(cache):
    daily, meta = F.load_daily(["AAA", "SPY"], date(2023, 1, 3), date(2023, 3, 31), cache)
    exp = _sessions(date(2023, 1, 3), date(2023, 3, 31))
    for sym in ("AAA", "SPY"):
        d = daily[sym]
        assert d.index.equals(exp) and d.index.name == "session"
        assert list(d.columns) == ["open", "high", "low", "close", "volume", "listed", "filled", "ended"]
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


def test_load_daily_gap_runs_up_to_two_are_filled_flat_and_listed(tmp_path):
    root = tmp_path / "c"
    s = _sessions(date(2023, 1, 3), date(2023, 6, 30))                 # a SHORT window: the rule is not a share
    df = _bars(s, 7)
    one, two = df["Date"].iloc[20], df["Date"].iloc[60:62]
    _write(root, "GAP", df[~df["Date"].isin([one, *two])])
    daily, meta = F.load_daily(["GAP"], date(2023, 1, 3), date(2023, 6, 30), root)
    d = daily["GAP"]
    assert meta["filled"] == {"GAP": [str(one.date()), *[str(t.date()) for t in two]]}
    assert d["filled"].sum() == 3 and d.loc[d["filled"], "volume"].eq(0.0).all() and d["listed"].all()
    for t in (one, *two):
        i = d.index.get_loc(t)
        row = d.iloc[i]
        assert row["open"] == row["high"] == row["low"] == row["close"] == d["close"].iloc[20 - 1 if t == one else 59]


def test_load_daily_three_consecutive_missing_sessions_refuse(tmp_path):
    root = tmp_path / "c"
    s = _sessions(date(2020, 1, 2), date(2024, 3, 28))
    df = _bars(s, 7)
    _write(root, "RUN3", df.drop(index=[500, 501, 502]))
    with pytest.raises(P.PanelDataError, match="RUN3"):
        F.load_daily(["RUN3"], date(2020, 1, 2), date(2024, 3, 28), root)
    _write(root, "RUN2", df.drop(index=[500, 501, 800, 1000]))        # runs of 2, 1, 1 are fine whatever the length
    daily, _ = F.load_daily(["RUN2"], date(2020, 1, 2), date(2024, 3, 28), root)
    assert daily["RUN2"]["filled"].sum() == 4


def test_load_daily_result_does_not_depend_on_the_window_length(tmp_path):
    """Two separate gaps are fine in a short window and in a long one; a run of 3 straddling the range start is
    measured whole and refused even though only part of it lies inside the range."""
    root = tmp_path / "c"
    s = _sessions(date(2022, 1, 3), date(2024, 3, 28))
    df = _bars(s, 3)
    _write(root, "TWO", df.drop(index=[300, 340]))
    short, _ = F.load_daily(["TWO"], date(2023, 1, 3), date(2023, 12, 29), root)
    long, _ = F.load_daily(["TWO"], date(2022, 1, 3), date(2024, 3, 28), root)
    pd.testing.assert_frame_equal(short["TWO"], long["TWO"].loc[short["TWO"].index])
    _write(root, "EDGE", df.drop(index=[198, 199, 200]))
    start = df["Date"].iloc[200]
    with pytest.raises(P.PanelDataError, match="EDGE"):
        F.load_daily(["EDGE"], start.date(), date(2023, 12, 29), root)


def test_load_daily_stale_tail_refuses(cache, tmp_path):
    """Sessions AFTER the last cached bar are not 'gaps' to fill: a stale or delisted series is refused."""
    root = tmp_path / "c"
    df = pd.read_parquet(cache / "AlpacaHistoryOHLCVProvider" / "AAA_1d.parquet")
    _write(root, "STALE", df[df["Date"] <= pd.Timestamp("2023-02-15")])
    with pytest.raises(P.PanelDataError, match="STALE.*stale"):
        F.load_daily(["STALE"], date(2023, 1, 3), date(2023, 3, 31), root)
    daily, _ = F.load_daily(["STALE"], date(2023, 1, 3), date(2023, 2, 15), root)      # ending at the last bar is fine
    assert daily["STALE"]["listed"].all() and not daily["STALE"]["filled"].any()
    with pytest.raises(P.PanelDataError, match="stale"):                                # a file that ends before the range
        F.load_daily(["STALE"], date(2023, 3, 1), date(2023, 3, 31), root)


def test_load_daily_refuses_non_positive_close_and_inverted_range(tmp_path):
    root = tmp_path / "c"
    df = _bars(_sessions(date(2023, 1, 3), date(2023, 3, 31)), 1)
    bad_close = df.copy()
    bad_close.loc[10, "Close"] = 0.0
    inverted = df.copy()
    inverted.loc[12, ["High", "Low"]] = inverted.loc[12, ["Low", "High"]].to_numpy()
    _write(root, "ZERO", bad_close)
    _write(root, "INV", inverted)
    for sym in ("ZERO", "INV"):
        with pytest.raises(P.PanelDataError, match=sym):
            F.load_daily([sym], date(2023, 1, 3), date(2023, 3, 31), root)


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


def _build(cache, groups, symbols=SYMS, first=FIRST, last=LAST, ctx=CTX, sector_of=None, universe=None,
           membership=None):
    return F.build_features(list(symbols), first, last, list(groups), cache, list(ctx), sector_of=sector_of,
                            universe=universe, membership=membership)


def _assert_cut_identical(cache, tmp_path, groups, day=CUT_DAY, symbols=SYMS, sector_of=None, universe=None,
                          truncate_extra=None, membership=None, nan_ok=False):
    """THE point-in-time proof: a build over a cache cut after ``day`` equals the full build on every common row.
    ``label_bar_filled`` is the one deliberate look-forward column (the NEXT session's fill flag): it is compared
    separately."""
    full = _build(cache, groups, symbols, sector_of=sector_of, universe=universe, membership=membership)
    trunc = _truncate(cache, tmp_path, day)
    if truncate_extra is not None:                       # a group with its own data files cuts them too
        truncate_extra(trunc, day)
    cut = _build(trunc, groups, symbols, last=day, sector_of=sector_of, universe=universe, membership=membership)
    common = full[full.index.get_level_values("session") <= pd.Timestamp(day)]
    assert len(common) == len(cut) > 300 * len(symbols) // 2
    assert common.index.equals(cut.index) and list(common.columns) == list(cut.columns)
    pd.testing.assert_frame_equal(common.drop(columns="label_bar_filled"), cut.drop(columns="label_bar_filled"),
                                  check_exact=True)                    # every column, bit for bit
    last = cut.index.get_level_values("session") == pd.Timestamp(day)
    assert cut.loc[last, "label_bar_filled"].isna().all()               # tomorrow is unknown in the cut build
    pd.testing.assert_series_equal(common.loc[~last, "label_bar_filled"], cut.loc[~last, "label_bar_filled"])
    feats = F.feature_columns(cut)
    isna = cut[feats].isna()
    if "t_vol_z" in feats:
        isna.loc[cut["filled"], "t_vol_z"] = False                    # a filled bar has no volume: its own z is NaN
    if nan_ok:                                                        # non-members are NaN by design: most cells still carry values
        assert feats and (~isna.to_numpy()).mean() > 0.5
    else:
        assert feats and not isna.to_numpy().any()                    # the comparison is not NaN == NaN
    return full


def test_cut_test_t_and_mtf(cache, tmp_path):
    _assert_cut_identical(cache, tmp_path, ["t", "mtf"])


def test_cut_test_detects_a_look_ahead(cache, tmp_path, monkeypatch):
    """The cut test is real: a builder that reads tomorrow's close FAILS it."""
    def leaky(daily, ctx):
        frames = {s: pd.DataFrame({"l_next_ret": np.log(daily[s]["close"].shift(-1) / daily[s]["close"])})
                  for s in ctx.symbols}
        return pd.concat(frames, names=["symbol", "session"])
    monkeypatch.setitem(F.GROUPS, "l", F.GroupSpec(leaky, "l_"))
    with pytest.raises(AssertionError):
        _assert_cut_identical(cache, tmp_path, ["l"])


def test_build_features_shape_and_no_nan(cache):
    f = _build(cache, ["t", "mtf"])
    assert f.index.names == ["symbol", "session"] and list(f.index.get_level_values("symbol").unique()) == list(SYMS)
    sess = f.index.get_level_values("session")
    exp = _sessions(FIRST, LAST)
    assert sess.min() == exp[0] and sess.max() == exp[-1] and len(f) == len(SYMS) * len(exp)
    assert list(f.columns[:4]) == ["listed", "ended", "filled", "label_bar_filled"] and f["listed"].all()
    assert not f["filled"].any() and f["label_bar_filled"].dropna().eq(False).all()
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


# ---------------------------------------------------------------------------------------------- Task 1.3
M_ETFS = ("SPY", "QQQ", "IWM", "TLT", "GLD")


def _lr(cache, sym, n):
    c = np.log(_raw(cache, sym)["Close"])
    return c.diff(n)


def test_cut_test_m(cache, tmp_path):
    _assert_cut_identical(cache, tmp_path, ["m"], sector_of=SECTOR_OF)


def test_cut_test_all_three_groups(cache, tmp_path):
    full = _assert_cut_identical(cache, tmp_path, ["t", "mtf", "m"], sector_of=SECTOR_OF)
    assert F.feature_columns(full, ["t"]) == list(F.T_COLUMNS) and F.feature_columns(full, ["m"]) == list(F.M_COLUMNS)


def test_m_columns_and_etf_returns(cache):
    f = _build(cache, ["m"], sector_of=SECTOR_OF)
    assert F.feature_columns(f) == list(F.M_COLUMNS)
    assert len(F.M_COLUMNS) == 5 * 3 + 2 + 2 + 1
    assert f[list(F.M_COLUMNS)].notna().all().all()
    a = f.loc["AAA"]
    for sym in M_ETFS:
        for n in (1, 5, 20):
            np.testing.assert_allclose(a[f"m_{sym.lower()}_ret_{n}"], _lr(cache, sym, n).reindex(a.index), rtol=1e-12)
    # the market columns are the same series for every symbol
    np.testing.assert_array_equal(f.loc["AAA", "m_spy_ret_20"].to_numpy(), f.loc["BBB", "m_spy_ret_20"].to_numpy())


def test_relative_strength_vs_spy_is_own_minus_spy(cache):
    f = _build(cache, ["m"], sector_of=SECTOR_OF)
    for sym in SYMS:
        for n in (5, 20):
            exp = (_lr(cache, sym, n) - _lr(cache, "SPY", n)).reindex(f.loc[sym].index)
            np.testing.assert_allclose(f.loc[sym, f"m_rs_spy_{n}"], exp, rtol=1e-12, atol=1e-15)
    t = _build(cache, ["t", "m"], sector_of=SECTOR_OF)                 # = the T group's own 20-day return minus SPY's
    np.testing.assert_allclose(t["m_rs_spy_20"], t["t_ret_20"] - t["m_spy_ret_20"], rtol=1e-12, atol=1e-15)


def test_relative_strength_vs_sector_etf(cache):
    f = _build(cache, ["m"], sector_of=SECTOR_OF)
    for sym, etf in (("AAA", "XLK"), ("BBB", "XLF")):
        for n in (5, 20):
            exp = (_lr(cache, sym, n) - _lr(cache, etf, n)).reindex(f.loc[sym].index)
            np.testing.assert_allclose(f.loc[sym, f"m_rs_sector_{n}"], exp, rtol=1e-12, atol=1e-15)
    assert not np.allclose(f.loc["AAA", "m_rs_sector_20"], f.loc["AAA", "m_rs_spy_20"])


def test_breadth_is_the_share_of_the_build_set_above_its_sma50(cache):
    syms = ("AAA", "BBB", "CCC")
    f = _build(cache, ["m"], symbols=syms, sector_of=SECTOR_OF)
    above = pd.DataFrame({s: _raw(cache, s)["Close"] > _raw(cache, s)["Close"].rolling(50).mean() for s in syms})
    exp = above.mean(axis=1).reindex(f.loc["AAA"].index)
    for s in syms:
        np.testing.assert_allclose(f.loc[s, "m_breadth_sma50"], exp, rtol=0, atol=1e-15)   # same for every symbol
    assert set(np.round(exp.unique(), 6)) <= {0.0, 0.333333, 0.666667, 1.0} and exp.nunique() > 2
    solo = _build(cache, ["m"], symbols=("AAA",), sector_of=SECTOR_OF)                      # context ETFs are not counted
    assert set(solo["m_breadth_sma50"].unique()) <= {0.0, 1.0}


def test_breadth_ignores_symbols_not_yet_listed(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "LATE", _bars(s[s >= pd.Timestamp("2023-06-01")], 11))
    sector = dict(SECTOR_OF, LATE="Technology")
    f = _build(root, ["m"], symbols=("AAA", "LATE"), sector_of=sector)
    before = f.loc["AAA", "m_breadth_sma50"][f.loc["AAA"].index < pd.Timestamp("2023-06-01")]
    assert set(before.unique()) <= {0.0, 1.0}                  # only AAA is counted until LATE exists (and has 50 bars)
    late = f.loc["LATE"]
    assert late.loc[late.index < pd.Timestamp("2023-06-01"), list(F.M_COLUMNS)].isna().all().all()    # unlisted rows are NaN in every column


def test_unknown_or_missing_sector_raises(cache):
    with pytest.raises(P.PanelDataError, match="unknown sector"):
        _build(cache, ["m"], sector_of={"AAA": "Technology", "BBB": "Crypto Mining"})
    with pytest.raises(P.PanelDataError, match="BBB"):
        _build(cache, ["m"], sector_of={"AAA": "Technology"})
    with pytest.raises(P.PanelDataError, match="BBB"):
        _build(cache, ["m"], sector_of={"AAA": "Technology", "BBB": None})
    with pytest.raises(P.PanelDataError, match="sector_of"):
        _build(cache, ["m"])


def test_m_adds_its_own_context_and_refuses_a_missing_series(cache, tmp_path):
    """build_features loads the market ETFs and the symbols' sector ETFs itself; a series that is not cached refuses."""
    bare = _build(cache, ["m"], ctx=(), sector_of=SECTOR_OF)
    pd.testing.assert_frame_equal(bare, _build(cache, ["m"], sector_of=SECTOR_OF))
    assert set(bare.attrs["meta"]["required_context"]) == {"SPY", "QQQ", "IWM", "TLT", "GLD", "XLK", "XLF"}
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    with pytest.raises(P.PanelDataError, match="XLE"):                 # a sector ETF that is not cached
        _build(root, ["m"], sector_of={"AAA": "Energy", "BBB": "Technology"})
    (root / "AlpacaHistoryOHLCVProvider" / "GLD_1d.parquet").unlink()
    with pytest.raises(P.PanelDataError, match="GLD"):
        _build(root, ["m"], sector_of=SECTOR_OF)


def test_sector_etf_map_is_the_committed_one():
    assert F.SECTOR_ETF == {"Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
                            "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Industrials": "XLI",
                            "Basic Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
                            "Communication Services": "XLC"}
    assert F.MARKET_ETFS == M_ETFS


def test_sector_etf_listed_later_leaves_nan_only_before_its_listing(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "XLC", _bars(s[s >= pd.Timestamp("2022-09-01")], 12))
    f = _build(root, ["m"], symbols=("AAA",), ctx=CTX + ("XLC",), sector_of={"AAA": "Communication Services"}).loc["AAA"]
    pre = f.index < pd.Timestamp("2022-09-02")
    assert pre.sum() > 40 and f.loc[pre, ["m_rs_sector_5", "m_rs_sector_20"]].isna().all().all()
    assert f.loc[pre, "m_rs_spy_20"].notna().all()               # the symbol itself is listed: only the sector leg is missing
    assert f.loc[f.index > pd.Timestamp("2022-10-15"), ["m_rs_sector_5", "m_rs_sector_20"]].notna().all().all()


# ---------------------------------------------------------------------------------------------- review fixes
GAP_ONE, GAP_TWO = pd.Timestamp("2023-03-15"), (pd.Timestamp("2023-08-09"), pd.Timestamp("2023-08-10"))


@pytest.fixture(scope="module")
def gap_cache(cache, tmp_path_factory):
    """The synthetic cache with a missing bar (AAA) and a missing run of two (BBB) INSIDE the cut window."""
    root = tmp_path_factory.mktemp("gapcache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    for sym, days in (("AAA", [GAP_ONE]), ("BBB", list(GAP_TWO))):
        df = _raw(root, sym).reset_index()
        _write(root, sym, df[~df["Date"].isin(days)])
    return root


def test_cut_test_with_filled_bars_inside_the_window(gap_cache, tmp_path):
    full = _assert_cut_identical(gap_cache, tmp_path, ["t", "mtf", "m"], sector_of=SECTOR_OF)
    assert full["filled"].sum() == 3


def test_filled_flag_and_label_bar_filled(gap_cache):
    f = _build(gap_cache, ["t"])
    assert list(f.index[f["filled"]]) == [("AAA", GAP_ONE), ("BBB", GAP_TWO[0]), ("BBB", GAP_TWO[1])]
    lab = f["label_bar_filled"]
    assert lab.dtype.name == "boolean"
    for sym, day in (("AAA", GAP_ONE), ("BBB", GAP_TWO[0]), ("BBB", GAP_TWO[1])):
        sess = f.loc[sym].index
        prev = sess[sess.get_loc(day) - 1]
        assert lab[(sym, prev)]                                        # the decision BEFORE a filled bar fills on it
    assert int(lab.sum()) == 3 and lab.loc[:, pd.Timestamp(LAST)].isna().all()       # the last session has no known next session
    # the flag is the next session's `filled`, nothing else
    for sym in SYMS:
        np.testing.assert_array_equal(lab.loc[sym].iloc[:-1].to_numpy(dtype=bool), f.loc[sym, "filled"].iloc[1:].to_numpy())


def test_filled_bars_do_not_distort_the_volume_statistics(gap_cache):
    """A fabricated zero-volume bar is left out of t_vol_z / t_obv_slope instead of entering them as log1p(0)."""
    f = _build(gap_cache, ["t"]).loc["AAA"]
    r = _raw(gap_cache, "AAA")
    lv = np.log1p(r["Volume"].astype(float))
    at = f.index[f.index.get_loc(GAP_ONE) + 5]                          # a window that contains the filled session
    # reference: the 60-session window of the LOADED grid with the filled session's volume removed
    sess = f.index
    grid_lv = lv.reindex(sess)
    grid_lv.loc[GAP_ONE] = np.nan
    w = grid_lv.iloc[sess.get_loc(at) - 59: sess.get_loc(at) + 1]
    assert w.isna().sum() == 1
    assert f.loc[at, "t_vol_z"] == pytest.approx((grid_lv.loc[at] - w.mean()) / w.std())
    assert f["t_obv_slope"].notna().all() and f.loc[~f["filled"], "t_vol_z"].notna().all()
    assert np.isnan(f.loc[GAP_ONE, "t_vol_z"])                          # the fabricated bar itself has no volume z
    # with the zero kept the statistic would be clearly different
    with_zero = grid_lv.fillna(0.0).iloc[sess.get_loc(at) - 59: sess.get_loc(at) + 1]
    assert abs((grid_lv.loc[at] - with_zero.mean()) / with_zero.std() - f.loc[at, "t_vol_z"]) > 0.01


def test_features_are_the_same_whatever_first_day_is(cache):
    """Every symbol is loaded from its first cached bar, so a row does not depend on where the request starts:
    builds over [D1, L] and [D2, L] agree exactly on the common rows, weekly columns included."""
    a = _build(cache, ["t", "mtf", "m"], first=date(2021, 6, 1), sector_of=SECTOR_OF)
    b = _build(cache, ["t", "mtf", "m"], first=date(2023, 3, 1), sector_of=SECTOR_OF)
    common = a.loc[b.index]
    assert len(b) < len(a) and len(b) > 500
    pd.testing.assert_frame_equal(common, b, check_exact=True)
    c = _build(cache, ["t", "mtf"], first=date(2023, 3, 1), last=date(2023, 12, 29))     # also a shorter window
    cols = [x for x in c.columns if x != "label_bar_filled"]
    pd.testing.assert_frame_equal(b.loc[c.index, cols], c[cols], check_exact=True)


def test_requested_range_before_any_bar_gives_unlisted_rows(cache):
    f = _build(cache, ["t"], first=date(2020, 11, 2), last=date(2021, 2, 26), ctx=())
    assert f.index.get_level_values("session").min() == pd.Timestamp("2020-11-02")
    pre = f.index.get_level_values("session") < pd.Timestamp("2021-01-04")
    assert not f.loc[pre, "listed"].any() and f.loc[pre, F.feature_columns(f)].isna().all().all()


def test_registry_group_specs_and_feature_columns(cache):
    spec = F.GROUPS["m"]
    assert isinstance(spec, F.GroupSpec) and spec.prefix == "m_" and spec.needs_sector
    assert F.GROUPS["t"].prefix == "t_" and not F.GROUPS["t"].needs_sector and F.GROUPS["mtf"].prefix == "mtf_"
    assert F.GROUPS["t"].required_context(["AAA"], None) == set()
    assert F.GROUPS["m"].required_context(["AAA", "BBB"], SECTOR_OF) == set(F.MARKET_ETFS) | {"XLK", "XLF"}
    f = _build(cache, ["t"])
    with pytest.raises(ValueError, match="unknown"):
        F.feature_columns(f, ["t", "nope"])


def _spec(builder, prefix):
    return F.GroupSpec(builder, prefix)


def test_builder_output_contract_is_enforced(cache, monkeypatch):
    def full_index(ctx):
        return pd.MultiIndex.from_product([ctx.symbols, ctx.sessions], names=["symbol", "session"])

    def missing_symbol(daily, ctx):
        return pd.DataFrame({"x_a": 1.0}, index=pd.MultiIndex.from_product([ctx.symbols[:1], ctx.sessions]))

    def not_float(daily, ctx):
        return pd.DataFrame({"x_a": 1}, index=full_index(ctx))

    def boolean(daily, ctx):
        return pd.DataFrame({"x_a": True}, index=full_index(ctx))

    def wrong_prefix(daily, ctx):
        return pd.DataFrame({"y_a": 1.0}, index=full_index(ctx))

    def shuffled(daily, ctx):
        return pd.DataFrame({"x_a": 1.0}, index=full_index(ctx)[::-1])

    for name, fn in (("missing_symbol", missing_symbol), ("not_float", not_float), ("boolean", boolean),
                     ("wrong_prefix", wrong_prefix), ("shuffled", shuffled)):
        monkeypatch.setitem(F.GROUPS, "x", _spec(fn, "x_"))
        with pytest.raises(ValueError, match="group 'x'"):
            _build(cache, ["x"], ctx=())

    def good(daily, ctx):
        return pd.DataFrame({"x_a": 1.0}, index=full_index(ctx))
    monkeypatch.setitem(F.GROUPS, "x", _spec(good, "x_"))
    assert F.feature_columns(_build(cache, ["x"], ctx=())) == ["x_a"]


def test_infinite_feature_raises_with_symbol_and_session(cache, monkeypatch):
    """+-inf is never silently turned into NaN: the build names the symbol, session and column."""
    def infinite(daily, ctx):
        idx = pd.MultiIndex.from_product([ctx.symbols, ctx.sessions], names=["symbol", "session"])
        df = pd.DataFrame({"x_a": 1.0}, index=idx)
        df.loc[("BBB", pd.Timestamp("2023-05-10")), "x_a"] = np.inf
        return df
    monkeypatch.setitem(F.GROUPS, "x", _spec(infinite, "x_"))
    with pytest.raises(P.PanelDataError, match=r"BBB.*2023-05-10|2023-05-10.*BBB"):
        _build(cache, ["x"], ctx=())


def test_group_needing_sector_refuses_without_it(cache):
    with pytest.raises(P.PanelDataError, match="sector_of"):
        _build(cache, ["t", "m"])


def test_universe_defaults_to_symbols_and_must_be_a_superset(cache):
    f = _build(cache, ["m"], sector_of=SECTOR_OF)
    assert f.attrs["meta"]["universe"] == list(SYMS)
    with pytest.raises(ValueError, match="universe"):
        _build(cache, ["m"], sector_of=SECTOR_OF, universe=("AAA", "CCC"))        # BBB is built but not in the universe


def test_chunked_builds_with_the_same_universe_give_the_same_breadth(cache):
    universe = ("AAA", "BBB", "CCC")
    one = _build(cache, ["t", "m"], symbols=universe, sector_of=SECTOR_OF)
    parts = [_build(cache, ["t", "m"], symbols=chunk, sector_of=SECTOR_OF, universe=universe)
             for chunk in (("AAA", "BBB"), ("CCC",))]
    joined = pd.concat(parts).loc[one.index]
    pd.testing.assert_frame_equal(joined.drop(columns="label_bar_filled"), one.drop(columns="label_bar_filled"),
                                  check_exact=True)
    solo = _build(cache, ["m"], symbols=("AAA",), sector_of=SECTOR_OF)                 # universe = AAA alone differs
    assert not np.allclose(solo.loc["AAA", "m_breadth_sma50"], one.loc["AAA", "m_breadth_sma50"])
    assert parts[1].attrs["meta"]["universe"] == list(universe)


def test_cut_test_with_an_explicit_universe(cache, tmp_path):
    _assert_cut_identical(cache, tmp_path, ["m"], symbols=("AAA",), sector_of=SECTOR_OF, universe=("AAA", "BBB", "CCC"))


def test_save_features_writes_parquet_and_a_meta_sidecar(cache, tmp_path):
    import json
    f = _build(cache, ["t", "mtf"])
    path = tmp_path / "store" / "features.parquet"
    F.save_features(f, path)
    meta = json.loads((tmp_path / "store" / "features.parquet.meta.json").read_text(encoding="utf-8"))
    assert meta["groups"]["t"] == list(F.T_COLUMNS) and meta["symbols"] == list(SYMS) and meta["rows"] == len(f)
    assert meta["first_day"] == str(FIRST) and set(meta["source"]["daily"]) >= set(SYMS)
    back = pd.read_parquet(path)
    pd.testing.assert_frame_equal(back, f, check_exact=True, check_freq=False)
    with pytest.raises(ValueError, match="meta"):
        F.save_features(pd.DataFrame({"a": [1.0]}), tmp_path / "bare.parquet")


def test_ml_panel_helpers_are_public_and_aliased():
    assert P.ema is P._ema and P.wilder is P._wilder and P.true_range is P._true_range
    assert F.ema is P.ema and F.wilder is P.wilder and F.true_range is P.true_range


# ---------------------------------------------------------------------------------------------- Task 1.4
def _ended_root(tmp_path, last_bar_pos):
    root = tmp_path / "c"
    s = _sessions(date(2022, 6, 1), date(2023, 6, 30))
    df = _bars(s, 3)
    _write(root, "GONE", df.iloc[: last_bar_pos + 1])
    return root, s


def test_a_series_that_ended_long_before_the_cache_end_is_nan_and_ended(tmp_path):
    root, s = _ended_root(tmp_path, 150)
    last_bar, cache_end = s[150], s[170]                                   # 20 sessions short: ENDED
    daily, meta = F.load_daily(["GONE"], s[100].date(), s[200].date(), root, cache_end=cache_end.date())
    d = daily["GONE"]
    assert list(d.columns) == ["open", "high", "low", "close", "volume", "listed", "filled", "ended"]
    after = d.index > last_bar
    assert after.sum() == 50 and d.loc[after, "ended"].all() and not d.loc[after, "listed"].any()
    assert d.loc[after, ["open", "high", "low", "close", "volume"]].isna().all().all() and not d.loc[after, "filled"].any()
    assert d.loc[~after, "listed"].all() and not d.loc[~after, "ended"].any()
    assert meta["ended"] == {"GONE": str(last_bar.date())}


def test_one_to_ten_sessions_short_is_stale_eleven_is_ended(tmp_path):
    root, s = _ended_root(tmp_path, 150)
    for short in (1, 5, 10):
        with pytest.raises(P.PanelDataError, match="GONE.*stale"):
            F.load_daily(["GONE"], s[100].date(), s[200].date(), root, cache_end=s[150 + short].date())
    daily, meta = F.load_daily(["GONE"], s[100].date(), s[200].date(), root, cache_end=s[161].date())
    assert meta["ended"] == {"GONE": str(s[150].date())} and daily["GONE"]["ended"].sum() == 50
    # a series that reaches the cache end is neither stale nor ended; without a cache end every tail is stale
    daily, meta = F.load_daily(["GONE"], s[100].date(), s[150].date(), root, cache_end=s[150].date())
    assert not daily["GONE"]["ended"].any() and meta["ended"] == {}
    with pytest.raises(P.PanelDataError, match="stale"):
        F.load_daily(["GONE"], s[100].date(), s[200].date(), root)


def test_a_range_that_stops_before_the_end_has_no_tail(tmp_path):
    root, s = _ended_root(tmp_path, 150)
    daily, meta = F.load_daily(["GONE"], s[100].date(), s[140].date(), root, cache_end=s[250].date())
    assert not daily["GONE"]["ended"].any() and daily["GONE"]["listed"].all()


def test_build_features_marks_an_ended_symbol(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    gone = _bars(s[s <= pd.Timestamp("2023-06-30")], 55)
    _write(root, "GONE", gone)
    f = _build(root, ["t", "mtf"], symbols=("AAA", "GONE")).loc["GONE"]
    after = f.index > pd.Timestamp("2023-06-30")
    feats = F.feature_columns(f)
    assert after.sum() > 100 and f.loc[after, "ended"].all() and not f.loc[after, "listed"].any()
    assert f.loc[after, feats].isna().all().all() and not f.loc[~after, "ended"].any()
    last_real = f.index[~after][-1]
    assert f.loc[last_real, "label_bar_filled"]                         # there is no fill bar after the last one
    assert f.loc[~after, feats].notna().all().all()
    aaa = _build(root, ["t", "mtf"], symbols=("AAA", "GONE")).loc["AAA"]
    assert not aaa["ended"].any()


def test_build_features_cache_end_comes_from_spy(cache, tmp_path):
    """A symbol that stops a few sessions before SPY's last bar is stale (refused), not ended."""
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "SHORT", _bars(s[:-4], 56))
    with pytest.raises(P.PanelDataError, match="SHORT.*stale"):
        _build(root, ["t"], symbols=("AAA", "SHORT"))


# ---- group X
X_BASE = ("ret_5", "ret_20", "ret_60", "rvol_20", "dist_sma50", "rsi", "vol_z")
XU = ("AAA", "BBB", "CCC")


def test_x_columns(cache):
    f = _build(cache, ["x"], symbols=XU)
    assert F.feature_columns(f) == list(F.X_COLUMNS)
    assert len(F.X_COLUMNS) == 2 * len(X_BASE) + 5
    assert F.GROUPS["x"].prefix == "x_" and not F.GROUPS["x"].needs_sector
    assert f[list(F.X_COLUMNS)].notna().all().all()


def _closes(cache, syms=XU):
    return pd.DataFrame({s: _raw(cache, s)["Close"] for s in syms})


def test_ranks_and_z_use_only_the_universe_rows_that_day(cache):
    f = _build(cache, ["x"], symbols=("AAA",), universe=XU)
    ret20 = np.log(_closes(cache)).diff(20)
    n = ret20.notna().sum(axis=1)
    exp_rk = ((ret20.rank(axis=1) - 0.5).div(n, axis=0))["AAA"].reindex(f.loc["AAA"].index)
    exp_z = ret20.sub(ret20.mean(axis=1), axis=0).div(ret20.std(axis=1), axis=0)["AAA"].reindex(f.loc["AAA"].index)
    np.testing.assert_allclose(f.loc["AAA", "x_rk_ret_20"], exp_rk, rtol=1e-12)
    np.testing.assert_allclose(f.loc["AAA", "x_z_ret_20"], exp_z, rtol=1e-10)
    assert f["x_rk_ret_20"].between(0, 1).all() and np.isin(np.round(f["x_rk_ret_20"].unique(), 6), [0.166667, 0.5, 0.833333]).all()
    full = _build(cache, ["x"], symbols=XU)
    for col in ("x_z_ret_20", "x_z_vol_z"):                              # a cross-section is centred
        np.testing.assert_allclose(full[col].groupby(level="session").mean(), 0.0, atol=1e-12)


def test_cross_section_ignores_unlisted_and_ended_universe_members(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "LATE", _bars(s[s >= pd.Timestamp("2023-02-01")], 9))
    f = _build(root, ["x"], symbols=("AAA",), universe=XU + ("LATE",))
    base = _build(root, ["x"], symbols=("AAA",), universe=XU)
    early = f.index.get_level_values("session") < pd.Timestamp("2023-02-07")
    pd.testing.assert_frame_equal(f[early][list(F.X_COLUMNS)], base[early][list(F.X_COLUMNS)], check_exact=True)
    assert not f[~early][list(F.X_COLUMNS)].equals(base[~early][list(F.X_COLUMNS)])    # LATE joins once it has history


def test_chunked_builds_agree_for_x(cache):
    one = _build(cache, ["t", "x"], symbols=XU)
    parts = [_build(cache, ["t", "x"], symbols=c, universe=XU) for c in (("AAA",), ("BBB", "CCC"))]
    joined = pd.concat(parts).loc[one.index]
    pd.testing.assert_frame_equal(joined.drop(columns="label_bar_filled"), one.drop(columns="label_bar_filled"),
                                  check_exact=True)


def test_cut_test_x(cache, tmp_path):
    """The store is truncated too (scans dated after D are gone): a build that peeked at the newest scan fails."""
    _assert_cut_identical(cache, tmp_path, ["x"], symbols=XU)


def test_cut_test_all_groups_with_x(cache, tmp_path):
    _assert_cut_identical(cache, tmp_path, ["t", "mtf", "m", "x"], symbols=("AAA", "BBB"),
                          sector_of=SECTOR_OF, universe=XU)


def test_x_momentum_volume_and_stage_come_from_the_bars_for_every_year(cache, tmp_path):
    """x_mom_12_1 / x_vol_ratio_20_60 / x_weinstein are computed from OHLCV (the store's own definitions), so they exist before
    the store's first scan; only the sector comes from the store (static)."""
    from ba2_providers.screener import metric_store as ms
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    store = root / "screener" / "metric_store_alpaca_20261022"
    for p in store.glob("ym=*/*.parquet"):                              # the store now starts in 2023-06
        df = pd.read_parquet(p)
        df = df[df["date"] >= "2023-06-07"]
        if len(df):
            df.to_parquet(p, index=False)
        else:
            p.unlink()
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    f = _build(root, ["x"], symbols=XU).loc["AAA"]
    assert f.index[0] < pd.Timestamp("2023-06-01") and f[list(F.X_COLUMNS)].notna().all().all()
    raw = _raw(cache, "AAA")
    close, vol = raw["Close"], raw["Volume"].astype(float)
    np.testing.assert_allclose(f["x_mom_12_1"], (close.shift(21) / close.shift(251) - 1).reindex(f.index), rtol=1e-12)
    np.testing.assert_allclose(f["x_vol_ratio_20_60"], (vol.rolling(20).mean() / vol.rolling(60).mean()).reindex(f.index), rtol=1e-12)
    np.testing.assert_array_equal(f["x_weinstein"], ms.weinstein_stage_series(close).reindex(f.index))
    assert set(f["x_weinstein"].unique()) <= {1.0, 2.0, 3.0, 4.0}
    assert (f["x_sector"] == F.SECTOR_CODE["Technology"]).all()          # the sector is the store's, in every year
    moms = pd.DataFrame({s: _raw(cache, s)["Close"] for s in XU})
    moms = moms.shift(21) / moms.shift(251) - 1
    exp_rk = ((moms.rank(axis=1) - 0.5).div(moms.notna().sum(axis=1), axis=0))["AAA"].reindex(f.index)
    np.testing.assert_allclose(f["x_mom_12_1_rk"], exp_rk, rtol=1e-12)
    full_store = _build(cache, ["x"], symbols=XU).loc["AAA"]
    pd.testing.assert_frame_equal(f, full_store, check_exact=True)       # the store's early scans change nothing


def _membership(index, entering=None):
    """AAA and BBB are always members; CCC from ``entering`` on (never, if None)."""
    m = pd.DataFrame(False, index=index, columns=["AAA", "BBB", "CCC"])
    m["AAA"] = True
    m["BBB"] = True
    if entering is not None:
        m.loc[index >= pd.Timestamp(entering), "CCC"] = True
    return m


def test_a_symbol_entering_the_membership_is_not_ranked_before_and_does_not_move_the_others(cache):
    sess = _sessions(FIRST, LAST)
    d = pd.Timestamp("2023-03-01")
    mem = _membership(sess, entering=d)
    f = _build(cache, ["x"], symbols=XU, membership=mem)
    base = _build(cache, ["x"], symbols=("AAA", "BBB"))                  # the two permanent members alone
    before = f.index.get_level_values("session") < d
    rk = [c for c in F.X_COLUMNS if c.startswith(("x_rk_", "x_z_")) or c == "x_mom_12_1_rk"]
    ccc = f.loc["CCC"]
    assert ccc.loc[ccc.index < d, rk].isna().all().all()                 # not a member yet: not ranked ...
    assert ccc.loc[ccc.index >= d, rk].notna().all().all()               # ... from the day it enters
    for s in ("AAA", "BBB"):                                             # ... and the others ignore it until then
        pd.testing.assert_frame_equal(f.loc[s].loc[:d - pd.Timedelta(days=1), rk], base.loc[s].loc[:d - pd.Timedelta(days=1), rk],
                                      check_exact=True)
        assert not np.allclose(f.loc[s].loc[d:, "x_rk_ret_20"], base.loc[s].loc[d:, "x_rk_ret_20"])
    n = {pd.Timestamp("2023-02-28"): 2, d: 3}
    for day, k in n.items():
        assert set(np.round(f.xs(day, level="session")["x_rk_ret_20"].dropna(), 6)) <= {round((i + 0.5) / k, 6) for i in range(k)}


def test_membership_also_scopes_breadth(cache):
    sess = _sessions(FIRST, LAST)
    d = pd.Timestamp("2023-03-01")
    f = _build(cache, ["m"], symbols=XU, sector_of=SECTOR_OF, membership=_membership(sess, entering=d))
    sma = {s: _raw(cache, s)["Close"] > _raw(cache, s)["Close"].rolling(50).mean() for s in XU}
    two = pd.DataFrame({s: sma[s] for s in ("AAA", "BBB")}).mean(axis=1)
    three = pd.DataFrame(sma).mean(axis=1)
    b = f.loc["AAA", "m_breadth_sma50"]
    np.testing.assert_allclose(b[b.index < d], two.reindex(b.index)[b.index < d], atol=1e-15)
    np.testing.assert_allclose(b[b.index >= d], three.reindex(b.index)[b.index >= d], atol=1e-15)


def test_the_production_floors_need_five_members_and_half_of_them(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 5)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.5)
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    names = ("DDD", "EEE", "FFF")
    for i, sym in enumerate(names):
        late = s if i == 0 else s[s >= pd.Timestamp("2023-02-01")]      # EEE and FFF list late
        _write(root, sym, _bars(late, 70 + i))
    six = XU + names
    f = _build(root, ["x"], symbols=("AAA",), universe=six).loc["AAA"]
    early = f.index < pd.Timestamp("2023-02-01")
    assert f.loc[early, "x_rk_ret_20"].isna().all()                      # 4 listed members < 5, whatever the share
    assert f.loc[f.index >= pd.Timestamp("2023-05-15"), "x_rk_ret_20"].notna().all()      # all six have history
    mid = (f.index >= pd.Timestamp("2023-02-09")) & (f.index < pd.Timestamp("2023-03-01"))
    assert mid.sum() > 5 and f.loc[mid, "x_rk_ret_5"].notna().all() and f.loc[mid, "x_rk_vol_z"].isna().all()   # 6 members, vol_z needs 60 sessions
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 2)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.8)                           # now the share rule alone: 4 of 6 listed = 0.67
    g = _build(root, ["x"], symbols=("AAA",), universe=six).loc["AAA"]
    assert g.loc[early, "x_rk_ret_20"].isna().all() and g.loc[g.index >= pd.Timestamp("2023-05-15"), "x_rk_ret_20"].notna().all()


def test_an_ended_member_leaves_the_cross_section(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "GONE", _bars(s[s <= pd.Timestamp("2023-06-30")], 55))
    with_gone = _build(root, ["x"], symbols=("AAA",), universe=XU + ("GONE",))
    without = _build(root, ["x"], symbols=("AAA",), universe=XU)
    after = with_gone.index.get_level_values("session") > pd.Timestamp("2023-06-30")
    rk = [c for c in F.X_COLUMNS if c.startswith("x_rk_") or c.startswith("x_z_")]
    pd.testing.assert_frame_equal(with_gone[after][rk], without[after][rk], check_exact=True)      # GONE no longer ranked
    assert not with_gone[~after][rk].equals(without[~after][rk])                                    # but it was before


def test_an_unknown_sector_in_the_store_says_where_to_add_it(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    for p in (root / "screener" / "metric_store_alpaca_20261022").glob("ym=*/*.parquet"):
        df = pd.read_parquet(p)
        df.loc[df["symbol"] == "CCC", "sector"] = "Crypto Mining"
        df.to_parquet(p, index=False)
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    with pytest.raises(P.PanelDataError, match="add it to SECTOR_CODE"):
        _build(root, ["x"], symbols=XU)


def test_sector_codes_are_a_committed_map():
    assert F.SECTOR_CODE == {"Basic Materials": 1, "Communication Services": 2, "Consumer Cyclical": 3,
                             "Consumer Defensive": 4, "Energy": 5, "Financial Services": 6, "Healthcare": 7,
                             "Industrials": 8, "Real Estate": 9, "Technology": 10, "Utilities": 11}
    assert set(F.SECTOR_CODE) == set(F.SECTOR_ETF)


def test_sector_of_can_come_from_the_store(cache):
    from_store = _build(cache, ["m"], sector_of="store")
    pd.testing.assert_frame_equal(from_store, _build(cache, ["m"], sector_of=SECTOR_OF))
    assert from_store.attrs["meta"]["sector_source"] == "store"
    # a symbol the store has no sector for refuses unless the caller overrides it
    with pytest.raises(P.PanelDataError, match="no sector for ZZZ"):
        F.build_features(["AAA", "ZZZ"], FIRST, LAST, ["m"], cache, [], sector_of="store")


def test_sector_overrides_fill_what_the_store_lacks(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    shutil.copy(root / "AlpacaHistoryOHLCVProvider" / "AAA_1d.parquet", root / "AlpacaHistoryOHLCVProvider" / "ZZZ_1d.parquet")
    f = F.build_features(["AAA", "ZZZ"], FIRST, LAST, ["m"], root, [], sector_of="store",
                         sector_overrides={"ZZZ": "Technology"})
    assert f.attrs["meta"]["sector_overrides"] == {"ZZZ": "Technology"}
    np.testing.assert_array_equal(f.loc["ZZZ", "m_rs_sector_20"].to_numpy(), f.loc["AAA", "m_rs_sector_20"].to_numpy())


def test_x_needs_the_store_only_for_the_sector(cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    for p in (root / "screener" / "metric_store_alpaca_20261022").glob("ym=*/*.parquet"):
        df = pd.read_parquet(p)
        df[df["date"] <= "2023-12-27"].to_parquet(p, index=False)       # the store ends 3 months before LAST
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    f = _build(root, ["x"], symbols=XU)                                 # no staleness error: the scans are not read
    assert f[list(F.X_COLUMNS)].notna().all().all()

def test_cut_test_and_chunks_with_a_membership(cache, tmp_path):
    mem = _membership(_sessions(FIRST, LAST), entering="2023-03-01")
    _assert_cut_identical(cache, tmp_path, ["x", "m"], symbols=XU, sector_of=SECTOR_OF, membership=mem, nan_ok=True)
    one = _build(cache, ["x", "m"], symbols=XU, sector_of=SECTOR_OF, membership=mem)
    parts = [_build(cache, ["x", "m"], symbols=c, sector_of=SECTOR_OF, membership=mem) for c in (("AAA",), ("BBB", "CCC"))]
    pd.testing.assert_frame_equal(pd.concat(parts).loc[one.index].drop(columns="label_bar_filled"),
                                  one.drop(columns="label_bar_filled"), check_exact=True)
    assert one.attrs["meta"]["membership"]["first_member_session"] == str(_sessions(FIRST, LAST)[0].date())


def test_membership_must_be_boolean_and_cover_the_universe(cache):
    sess = _sessions(FIRST, LAST)
    with pytest.raises(ValueError, match="bool"):
        _build(cache, ["x"], symbols=XU, membership=_membership(sess).astype(float))
    with pytest.raises(ValueError, match="universe"):
        _build(cache, ["x"], symbols=("AAA",), universe=("AAA",), membership=_membership(sess))


# ---------------------------------------------------------------------------------------------- ended vs stale, cache
def _with_gone_in_the_store(cache, tmp_path, last_scan):
    """A cache where GONE's bars end 2023-06-30 and the store lists GONE on every scan up to ``last_scan``."""
    from tests.test_ml_universe import write_metric_store
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s = _sessions()
    _write(root, "GONE", _bars(s[s <= pd.Timestamp("2023-06-30")], 55))
    rows = _store_rows()
    gone = rows[rows["symbol"] == "AAA"].assign(symbol="GONE")
    gone = gone[gone["date"] <= last_scan]
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    write_metric_store(root / "screener" / "metric_store_alpaca_20261022", pd.concat([rows, gone]))
    return root


def test_a_store_scan_after_the_last_bar_makes_the_series_stale_not_ended(cache, tmp_path):
    root = _with_gone_in_the_store(cache, tmp_path, "2023-09-27")
    with pytest.raises(P.PanelDataError, match=r"GONE.*2023-09-27.*after its last cached bar 2023-06-30.*stale"):
        _build(root, ["t"], symbols=("AAA", "GONE"))
    ok_root = _with_gone_in_the_store(cache, tmp_path / "ok", "2023-06-28")
    f = _build(ok_root, ["t"], symbols=("AAA", "GONE"))
    assert f.attrs["meta"]["ended"] == {"GONE": "2023-06-30"}                  # each ended symbol's last bar, recorded
    assert f.attrs["meta"]["load"]["ended"] == {"GONE": "2023-06-30"}


def test_the_stale_message_says_it_is_too_short_to_count_as_ended(tmp_path):
    root = tmp_path / "c"
    s = _sessions(date(2022, 6, 1), date(2023, 6, 30))
    _write(root, "GONE", _bars(s, 3).iloc[:151])
    with pytest.raises(P.PanelDataError, match=r"5 sessions short.*too short to count as ended.*more than 10"):
        F.load_daily(["GONE"], s[100].date(), s[200].date(), root, cache_end=s[155].date())


def test_a_shared_build_cache_loads_and_computes_once_and_changes_nothing(cache, monkeypatch):
    loads, techs = [], []
    real_load, real_tech = F.load_daily, F._technical_one
    monkeypatch.setattr(F, "load_daily", lambda syms, *a, **k: (loads.append(list(syms)), real_load(syms, *a, **k))[1])
    monkeypatch.setattr(F, "_technical_one", lambda d: (techs.append(len(d)), real_tech(d))[1])
    chunks = (("AAA",), ("BBB", "CCC"))
    plain = [_build(cache, ["t", "x"], symbols=c, universe=XU) for c in chunks]
    n_loads_plain, n_techs_plain = len(loads), len(techs)
    assert n_loads_plain == 2 and n_techs_plain == (3 + 1) + (3 + 2)
    loads.clear(), techs.clear()
    shared = F.BuildCache()
    cached = [F.build_features(list(c), FIRST, LAST, ["t", "x"], cache, list(CTX), universe=list(XU), preloaded=shared)
              for c in chunks]
    assert len(loads) == 1 and len(techs) == 3                                  # loaded once, T computed once per symbol
    for a, b in zip(plain, cached):
        pd.testing.assert_frame_equal(a.drop(columns="label_bar_filled"), b.drop(columns="label_bar_filled"), check_exact=True)


def test_a_shared_build_cache_refuses_a_different_window(cache):
    shared = F.BuildCache()
    F.build_features(["AAA"], FIRST, LAST, ["t"], cache, list(CTX), preloaded=shared)
    with pytest.raises(ValueError, match="last_day"):
        F.build_features(["AAA"], FIRST, date(2024, 3, 27), ["t"], cache, list(CTX), preloaded=shared)


# ---------------------------------------------------------------------------------------------- re-review fixes
@pytest.mark.parametrize("days_after,refused", [(1, False), (7, False), (8, True)])
def test_a_store_scan_within_the_staleness_limit_after_the_last_bar_is_still_ended(cache, tmp_path, days_after, refused):
    """The store keeps a delisted name for up to STALENESS_MAX_DAYS (7) after its last bar: scans 1..7 days after are
    normal for an ended name; 8 days after means the cached series stopped early."""
    from ba2_providers.screener import metric_store as ms
    from tests.test_ml_universe import write_metric_store
    assert ms.STALENESS_MAX_DAYS == 7
    root = tmp_path / "c"
    shutil.copytree(cache, root)
    s_ = _sessions()
    _write(root, "GONE", _bars(s_[s_ <= pd.Timestamp("2023-06-30")], 55))
    rows = _store_rows()
    gone = rows[(rows["symbol"] == "AAA") & (rows["date"] <= "2023-06-28")].assign(symbol="GONE")
    extra = gone.iloc[[-1]].assign(date=str((pd.Timestamp("2023-06-30") + pd.Timedelta(days=days_after)).date()))
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    write_metric_store(root / "screener" / "metric_store_alpaca_20261022", pd.concat([rows, gone, extra]))
    if refused:
        with pytest.raises(P.PanelDataError, match=r"GONE.*stale"):
            _build(root, ["t"], symbols=("AAA", "GONE"))
    else:
        f = _build(root, ["t"], symbols=("AAA", "GONE"))
        assert f.attrs["meta"]["ended"] == {"GONE": "2023-06-30"}


def test_membership_provenance_records_a_digest_and_the_parameters(cache, tmp_path):
    from tests.test_ml_universe import write_metric_store
    sess = _sessions(FIRST, LAST)
    mem = _membership(sess, entering="2023-03-01")
    mem.attrs["membership"] = {"top_n": 300, "purpose": "dev", "min_dollar_volume": 2e7}
    f = _build(cache, ["x"], symbols=XU, membership=mem)
    rec = f.attrs["meta"]["membership"]
    assert rec["top_n"] == 300 and rec["purpose"] == "dev" and rec["min_dollar_volume"] == 2e7
    assert len(rec["digest"]) == 16 and rec["symbols"] == 3
    other = mem.copy()
    other.loc[other.index >= pd.Timestamp("2023-06-01"), "BBB"] = False
    assert _build(cache, ["x"], symbols=XU, membership=other).attrs["meta"]["membership"]["digest"] != rec["digest"]
    again = _membership(sess, entering="2023-03-01")
    assert _build(cache, ["x"], symbols=XU, membership=again).attrs["meta"]["membership"]["digest"] == rec["digest"]
    assert _build(cache, ["x"], symbols=XU, membership=again).attrs["meta"]["membership"]["top_n"] is None   # not from the helper


def test_membership_from_the_helper_carries_its_parameters(tmp_path):
    from app.services import ml_universe as U
    from tests.test_ml_universe import SCANS, _row, write_metric_store
    store = write_metric_store(tmp_path / "metric_store_m", pd.DataFrame([_row(s, "AAPL", 3e12) for s in SCANS]))
    m = U.membership_from_universe(pd.DatetimeIndex(pd.bdate_range("2022-01-24", "2022-02-04")), 5, "dev", store,
                                   min_dollar_volume=1e6)
    assert m.attrs["membership"] == {"top_n": 5, "purpose": "dev", "min_dollar_volume": 1e6, "store_dir": store}


def test_a_membership_that_does_not_cover_the_sessions_from_its_first_scan_on_refuses(cache):
    sess = _sessions(FIRST, LAST)
    full = _membership(sess)
    holed = full.drop(index=sess[[100, 101, 102]])                              # three sessions missing in the middle
    with pytest.raises(ValueError, match=r"membership does not cover.*3 sessions.*" + str(sess[100].date())):
        _build(cache, ["x"], symbols=XU, membership=holed)
    short = full.loc[:sess[-10]]                                                 # ends before the build's last session
    with pytest.raises(ValueError, match="membership does not cover"):
        _build(cache, ["x"], symbols=XU, membership=short)
    # sessions BEFORE the membership's first member session are not required (the universe does not exist yet)
    late = full.loc[sess[200]:]
    f = _build(cache, ["x"], symbols=XU, membership=late)
    assert f.loc["AAA"].index[0] < sess[200]
