"""DeterministicScorer TECHNICAL: the series forms must be the scalars, exactly.

WHY THIS FILE EXISTS
--------------------
``technical_score`` used to rebuild every indicator from the as_of SLICE on
every bar: 6.77 ms and ~41 pandas Series constructions per call, ~145k calls per
GA trial (97 symbols x ~1500 bars), which was essentially the whole trial. The
indicators are causal recursions and trailing windows, so each one is now
computed ONCE per (symbol, frame, parameters) as a full SERIES and indexed by
bar position.

That is only safe because of two properties, and this file pins both:

RISK 1 -- NO LOOKAHEAD. The series is computed over a frame that CONTAINS ROWS
AFTER the decision date. ``test_no_lookahead_*`` asserts, per indicator and per
date, that the value read out of the full-frame series is EXACTLY (``==``, not
approx) the value the original per-slice scalar produces from a frame TRUNCATED
at that date. A centered window, a bfill, a reverse scan, a whole-series
mean/std or an alignment-shifting dropna would break this instantly.

RISK 2 -- THE LIVE PATH. ``_process`` is the shared funnel for the backtest
(``analyze_as_of``) and for live (``run_analysis`` -> ``_gather_and_process``);
there is no branch on which one is running. The memo is keyed on a fingerprint
of the underlying frame, so a live re-run after a new bar cannot be served
yesterday's indicators -- ``test_changed_frame_invalidates_memo`` pins that, and
``test_view_path_matches_slice_path_*`` pins that the two paths agree.

The ``_ref_*`` functions below are VERBATIM copies of the scalar implementations
as they stood at commit d66a5a75, before the series rewrite. They are the
reference, deliberately duplicated so a future edit to technical.py cannot move
both sides of the comparison at once.
"""
from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd
import pytest

from ba2_experts.DeterministicScorer import data as D
from ba2_experts.DeterministicScorer import technical as T


# --------------------------------------------------------------------------
# Frozen pre-rewrite scalars (commit d66a5a75) -- the reference side
# --------------------------------------------------------------------------
def _ref_momentum_12_1(closes, lookback=252, skip=21):
    if closes is None or lookback <= 0 or skip < 0 or skip >= lookback:
        return None
    if len(closes) < lookback:
        return None
    p_recent = float(closes.iloc[-1 - skip]) if skip > 0 else float(closes.iloc[-1])
    p_base = float(closes.iloc[-lookback])
    if p_base <= 0 or not np.isfinite(p_base) or not np.isfinite(p_recent):
        return None
    return p_recent / p_base - 1.0


def _ref_realized_vol_daily(closes, window=63):
    if closes is None or len(closes) < max(10, window // 2):
        return None
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < 10:
        return None
    sigma = float(rets.std(ddof=1))
    return sigma if np.isfinite(sigma) and sigma > 0 else None


def _ref_vol_adjusted_momentum(closes, lookback=252, skip=21, vol_window=63):
    mom = _ref_momentum_12_1(closes, lookback, skip)
    sigma = _ref_realized_vol_daily(closes, vol_window)
    if mom is None or sigma is None:
        return None
    horizon = max(int(lookback) - int(skip), 1)
    sigma_h = sigma * math.sqrt(horizon)
    if sigma_h <= 0 or not np.isfinite(sigma_h):
        return None
    return mom / sigma_h


def _ref_distance_to_sma(closes, period=200):
    if closes is None or len(closes) < period:
        return None
    sma = float(closes.iloc[-period:].mean())
    last = float(closes.iloc[-1])
    if sma <= 0 or not np.isfinite(sma):
        return None
    return last / sma - 1.0


def _ref_rsi_wilder(closes, period=14):
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = float(gain.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    avg_loss = float(loss.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _ref_wilder_smooth(series, period):
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def _ref_adx_wilder(highs, lows, closes, period=14):
    n = min(len(highs), len(lows), len(closes))
    if n < 2 * period:
        return None
    highs, lows, closes = highs.iloc[-n:], lows.iloc[-n:], closes.iloc[-n:]
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = _ref_wilder_smooth(tr, period)
    plus_di = 100.0 * _ref_wilder_smooth(plus_dm, period) / atr.replace(0.0, np.nan)
    minus_di = 100.0 * _ref_wilder_smooth(minus_dm, period) / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = _ref_wilder_smooth(dx, period)
    val = float(adx.iloc[-1])
    return val if np.isfinite(val) else None


def _ref_atr_wilder(highs, lows, closes, period=14):
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    highs, lows, closes = highs.iloc[-n:], lows.iloc[-n:], closes.iloc[-n:]
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = _ref_wilder_smooth(tr, period)
    val = float(atr.iloc[-1])
    return val if np.isfinite(val) and val > 0 else None


def _ref_donchian_state(highs, lows, closes, period=20):
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    h_window = highs.iloc[-(period + 1):-1]
    l_window = lows.iloc[-(period + 1):-1]
    close = float(closes.iloc[-1])
    upper = float(h_window.max())
    lower = float(l_window.min())
    if close > upper:
        return 1.0
    if close < lower:
        return -1.0
    if upper <= lower:
        return 0.0
    return 2.0 * (close - lower) / (upper - lower) - 1.0


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
def _frame(close: np.ndarray, high=None, low=None, start="2019-01-02") -> pd.DataFrame:
    n = len(close)
    return pd.DataFrame({
        "Date": pd.date_range(start, periods=n, freq="B"),
        "Open": close,
        "High": close * 1.008 if high is None else high,
        "Low": close * 0.992 if low is None else low,
        "Close": close,
        "Volume": np.full(n, 1_000_000.0),
    })


def _synthetic_frames():
    rng = np.random.default_rng(20260919)
    n = 900
    walk = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.014, n)))
    yield "randomwalk", _frame(
        walk, walk * (1 + np.abs(rng.normal(0, 0.008, n))),
        walk * (1 - np.abs(rng.normal(0, 0.008, n))))
    flat = np.full(600, 42.0)                      # zero deltas: RSI 100, ATR 0
    yield "flat", _frame(flat, flat, flat)
    vee = np.concatenate([np.linspace(10, 200, 400), np.linspace(200, 15, 260)])
    yield "vshape", _frame(vee)
    rng2 = np.random.default_rng(3)                # ties in the Donchian channel
    dupes = np.repeat(50 + np.cumsum(rng2.normal(0, 0.2, 320)), 2)
    yield "dupes", _frame(dupes)


_REAL_CACHE = os.path.join(
    os.path.expanduser("~"), "Documents", "ba2", "common", "cache", "FMPOHLCVProvider")
_REAL_SYMBOLS = ("AAPL", "MSFT", "NVDA", "XOM", "KO")


def _real_frames():
    for sym in _REAL_SYMBOLS:
        path = os.path.join(_REAL_CACHE, f"{sym}_1d.parquet")
        if not os.path.exists(path):
            continue
        df = pd.read_parquet(path)
        cols = {c.lower(): c for c in df.columns}
        need = ("date", "close", "high", "low")
        if not all(k in cols for k in need):
            continue
        out = pd.DataFrame({
            "Date": pd.to_datetime(df[cols["date"]]),
            "Close": df[cols["close"]].astype(float),
            "High": df[cols["high"]].astype(float),
            "Low": df[cols["low"]].astype(float),
        }).sort_values("Date").reset_index(drop=True)
        yield sym, out.tail(1600).reset_index(drop=True)


# Three parameter sets: the defaults, plus two the GA can actually produce.
PARAM_SETS = (
    dict(mom_lookback_days=252, mom_skip_days=21, vol_window=63,
         sma_trend_period=200, rsi_period=14, donchian_period=20, adx_period=14),
    dict(mom_lookback_days=126, mom_skip_days=5, vol_window=20,
         sma_trend_period=50, rsi_period=7, donchian_period=10, adx_period=10),
    dict(mom_lookback_days=60, mom_skip_days=0, vol_window=252,
         sma_trend_period=300, rsi_period=21, donchian_period=55, adx_period=28),
)


def _indicator_cases(df: pd.DataFrame, p: dict):
    """(name, full-frame series, reference scalar over a truncated frame)."""
    C, H, L = df["Close"], df["High"], df["Low"]
    return (
        ("momentum_12_1",
         T.momentum_12_1_series(C, p["mom_lookback_days"], p["mom_skip_days"]),
         lambda s: _ref_momentum_12_1(s["Close"], p["mom_lookback_days"], p["mom_skip_days"])),
        ("realized_vol_daily",
         T.realized_vol_daily_series(C, p["vol_window"]),
         lambda s: _ref_realized_vol_daily(s["Close"], p["vol_window"])),
        ("vol_adjusted_momentum",
         T.vol_adjusted_momentum_series(C, p["mom_lookback_days"], p["mom_skip_days"],
                                        p["vol_window"]),
         lambda s: _ref_vol_adjusted_momentum(s["Close"], p["mom_lookback_days"],
                                              p["mom_skip_days"], p["vol_window"])),
        ("distance_to_sma",
         T.distance_to_sma_series(C, p["sma_trend_period"]),
         lambda s: _ref_distance_to_sma(s["Close"], p["sma_trend_period"])),
        ("rsi_wilder",
         T.rsi_wilder_series(C, p["rsi_period"]),
         lambda s: _ref_rsi_wilder(s["Close"], p["rsi_period"])),
        ("donchian_state",
         T.donchian_state_series(H, L, C, p["donchian_period"]),
         lambda s: _ref_donchian_state(s["High"], s["Low"], s["Close"], p["donchian_period"])),
        ("adx_wilder",
         T.adx_wilder_series(H, L, C, p["adx_period"]),
         lambda s: _ref_adx_wilder(s["High"], s["Low"], s["Close"], p["adx_period"])),
        ("atr_wilder",
         T.atr_wilder_series(H, L, C, p["adx_period"]),
         lambda s: _ref_atr_wilder(s["High"], s["Low"], s["Close"], p["adx_period"])),
    )


def _scalar_of(series_value):
    """The series' NaN encoding -> the scalar contract's None."""
    return None if not np.isfinite(series_value) else float(series_value)


def _assert_exact(name, expected, got, t, label):
    if expected is None:
        assert got is None, (
            f"{label} {name} @t={t}: series says {got!r}, truncated scalar says None")
        return
    assert got is not None, (
        f"{label} {name} @t={t}: series says None, truncated scalar says {expected!r}")
    # EXACT. Not approx: a rounding difference here means the series is not the
    # same float operation sequence the per-slice scalar performs, and every
    # stored backtest result becomes unreproducible.
    assert got == expected, (
        f"{label} {name} @t={t}: {got!r} != {expected!r} (delta {got - expected:.3e})")


# --------------------------------------------------------------------------
# RISK 1: no lookahead
# --------------------------------------------------------------------------
@pytest.mark.parametrize("params", PARAM_SETS, ids=("defaults", "fast", "slow"))
def test_no_lookahead_synthetic(params):
    """series_from_full_frame[t] == scalar(frame truncated at t), exactly."""
    checked = 0
    for fname, df in _synthetic_frames():
        n = len(df)
        for name, series, ref in _indicator_cases(df, params):
            assert len(series) == n
            for t in range(0, n, 11):
                _assert_exact(name, ref(df.iloc[:t + 1]), _scalar_of(series[t]), t, fname)
                checked += 1
    assert checked > 1500, f"only {checked} (indicator, date) pairs checked"


@pytest.mark.skipif(not any(True for _ in _real_frames()),
                    reason="no local FMP OHLCV parquet cache")
@pytest.mark.parametrize("params", PARAM_SETS, ids=("defaults", "fast", "slow"))
def test_no_lookahead_real_symbols(params):
    """Same audit on real cached daily bars for several symbols."""
    symbols = 0
    for sym, df in _real_frames():
        symbols += 1
        n = len(df)
        for name, series, ref in _indicator_cases(df, params):
            for t in range(max(0, n - 400), n, 3):
                _assert_exact(name, ref(df.iloc[:t + 1]), _scalar_of(series[t]), t, sym)
    assert symbols >= 2


def test_scalar_wrappers_match_the_frozen_reference():
    """The public scalars are wrappers now; they must not have drifted."""
    for fname, df in _synthetic_frames():
        n = len(df)
        for t in (0, 1, 13, 199, 200, 201, 400, n - 1):
            if t >= n:
                continue
            s = df.iloc[:t + 1]
            C, H, L = s["Close"], s["High"], s["Low"]
            pairs = (
                ("momentum_12_1", T.momentum_12_1(C), _ref_momentum_12_1(C)),
                ("realized_vol_daily", T.realized_vol_daily(C), _ref_realized_vol_daily(C)),
                ("vol_adjusted_momentum", T.vol_adjusted_momentum(C),
                 _ref_vol_adjusted_momentum(C)),
                ("distance_to_sma", T.distance_to_sma(C), _ref_distance_to_sma(C)),
                ("rsi_wilder", T.rsi_wilder(C), _ref_rsi_wilder(C)),
                ("donchian_state", T.donchian_state(H, L, C), _ref_donchian_state(H, L, C)),
                ("adx_wilder", T.adx_wilder(H, L, C), _ref_adx_wilder(H, L, C)),
                ("atr_wilder", T.atr_wilder(H, L, C), _ref_atr_wilder(H, L, C)),
            )
            for name, got, expected in pairs:
                _assert_exact(name, expected, got, t, fname)


def test_future_rows_cannot_move_a_past_value():
    """Appending future bars must not change any earlier element of the series.

    The direct statement of "no lookahead", independent of the scalar reference:
    if an indicator ever read forward, extending the frame would move values
    behind the extension point.
    """
    rng = np.random.default_rng(5)
    base = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 700)))
    tail = base[-1] * np.exp(np.cumsum(rng.normal(0.01, 0.05, 250)))   # violent future
    short = _frame(base)
    long = _frame(np.concatenate([base, tail]))
    for params in PARAM_SETS:
        for (name, s_series, _), (_, l_series, _) in zip(
                _indicator_cases(short, params), _indicator_cases(long, params)):
            a, b = np.asarray(s_series), np.asarray(l_series)[:len(short)]
            same = (a == b) | (np.isnan(a) & np.isnan(b))
            assert same.all(), (
                f"{name}: {int((~same).sum())} of {len(a)} values moved when "
                f"future bars were appended -- the indicator reads forward")


# --------------------------------------------------------------------------
# RISK 2: the view path is the slice path, and it cannot go stale
# --------------------------------------------------------------------------
def _settings(params: dict) -> dict:
    s = dict(params)
    s.update({"adx_gate": 25.0, "adx_rsi_boost": 2.0, "atr_period": params["adx_period"]})
    return s


@pytest.mark.parametrize("params", PARAM_SETS, ids=("defaults", "fast", "slow"))
def test_view_path_matches_slice_path_exactly(params):
    """technical_score(slice, view=...) == technical_score(slice) for every bar.

    This is the backtest-vs-live invariant in miniature: ONE code path, and the
    memoised full-frame read must return what the per-slice arithmetic returns.
    """
    settings = _settings(params)
    for fname, df in _synthetic_frames():
        view = D.OhlcvView(fname, df)
        n = len(df)
        for t in range(300, n, 17):
            sl = df.iloc[:t + 1].reset_index(drop=True)
            assert view.position_of_prefix(sl) == t
            fast = T.technical_score(sl, settings, view=view)
            slow = T.technical_score(sl, settings)
            assert fast == slow, f"{fname} @t={t}: {fast} != {slow}"


def test_view_path_engages_and_memoises():
    """The fast path must actually be taken -- one build per indicator, then hits."""
    D.reset_caches()
    _, df = next(iter(_synthetic_frames()))
    view = D.OhlcvView("MEMO", df)
    settings = _settings(PARAM_SETS[0])
    for t in range(400, 460):
        T.technical_score(df.iloc[:t + 1].reset_index(drop=True), settings, view=view)
    stats = T.series_memo_stats()
    # Six indicator arrays built once, then read out of the memo every bar.
    assert stats["miss"] == 6, stats
    assert stats["hit"] == 60 * 6 - 6, stats
    assert stats["evicted"] == 0, stats


def test_changed_frame_invalidates_memo():
    """A new bar makes a NEW view whose fingerprint cannot hit the old entries.

    The live hazard: ``run_analysis`` re-runs as bars arrive. A memo keyed on the
    symbol alone would serve yesterday's indicators into today's trading
    decision.
    """
    D.reset_caches()
    _, df = next(iter(_synthetic_frames()))
    settings = _settings(PARAM_SETS[0])
    first = D.OhlcvView("LIVE", df.iloc[:-1].reset_index(drop=True))
    before = T.technical_score(df.iloc[:-1].reset_index(drop=True), settings, view=first)
    n_after_first = T.series_memo_stats()["miss"]

    moved = df.copy()
    moved.loc[moved.index[-1], ["Close", "High", "Low"]] = [
        float(moved["Close"].iloc[-2]) * 1.35] * 3
    second = D.OhlcvView("LIVE", moved)
    assert second.fingerprint != first.fingerprint

    after = T.technical_score(moved, settings, view=second)
    assert T.series_memo_stats()["miss"] == n_after_first + 6, "stale entries were reused"
    assert after != before
    # ...and it is the same answer the per-slice path gives for the new frame.
    assert after == T.technical_score(moved, settings)


def test_reset_caches_drops_the_series_memo():
    D.reset_caches()
    _, df = next(iter(_synthetic_frames()))
    view = D.OhlcvView("RESET", df)
    T.technical_score(df, _settings(PARAM_SETS[0]), view=view)
    assert T.series_memo_stats()["miss"] == 6
    D.reset_caches()
    assert T.series_memo_stats() == {"hit": 0, "miss": 0, "evicted": 0}


def test_foreign_or_non_prefix_frame_is_refused():
    """A slice that is not a prefix of the view must fall back, not misindex."""
    _, df = next(iter(_synthetic_frames()))
    view = D.OhlcvView("A", df)
    other = df.copy()
    other["Close"] = other["Close"] * 1.01
    assert view.position_of_prefix(other.iloc[:300].reset_index(drop=True)) is None
    assert view.position_of_prefix(df.iloc[5:305].reset_index(drop=True)) is None
    assert view.position_of_prefix(pd.concat([df, df]).reset_index(drop=True)) is None
    assert view.position_of_prefix(df.iloc[:300].reset_index(drop=True)) == 299
    # ...and technical_score still answers, via the slice path.
    T._reset_view_warning()
    settings = _settings(PARAM_SETS[0])
    sl = other.iloc[:400].reset_index(drop=True)
    assert T.technical_score(sl, settings, view=view) == T.technical_score(sl, settings)


def test_non_monotonic_frame_gets_no_fast_path():
    """Out-of-order dates break the prefix argument; the view must refuse."""
    _, df = next(iter(_synthetic_frames()))
    shuffled = df.copy()
    d = shuffled["Date"].to_numpy()
    d[10], d[11] = d[11], d[10]
    shuffled["Date"] = d
    view = D.OhlcvView("SHUF", shuffled)
    assert view.ascending is False
    assert view.position_of_prefix(shuffled.iloc[:300].reset_index(drop=True)) is None


def test_memo_is_bounded():
    """A long-lived GA worker walks many parameter sets; the memo must not grow."""
    D.reset_caches()
    _, df = next(iter(_synthetic_frames()))
    view = D.OhlcvView("BOUND", df)
    for period in range(20, 20 + T._SERIES_MEMO_MAX + 50):
        T._memoised((view.symbol, view.fingerprint, "d200", (period,)),
                    lambda p=period: T.distance_to_sma_series(view.close_s, p))
    assert len(T._SERIES_MEMO) <= T._SERIES_MEMO_MAX
    assert T.series_memo_stats()["evicted"] > 0


# --------------------------------------------------------------------------
# The hoisted date parse and the index-close memo
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tz", (None, "UTC", "America/New_York"))
def test_searchsorted_slice_matches_the_mask(tz):
    """_slice_to_as_of with a view == the boolean-mask version, row for row."""
    _, df = next(iter(_synthetic_frames()))
    if tz is not None:
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize("UTC").dt.tz_convert(tz)
    view = D.OhlcvView("TZ", df)
    dates = pd.to_datetime(df["Date"])
    for i in (0, 1, 50, 300, len(df) - 1):
        for as_of in (dates.iloc[i].to_pydatetime(),
                      (dates.iloc[i] + pd.Timedelta(hours=13)).to_pydatetime()):
            fast = D._slice_to_as_of(df, as_of, view)
            slow = D._slice_to_as_of(df, as_of, None)
            pd.testing.assert_frame_equal(fast.reset_index(drop=True),
                                          slow.reset_index(drop=True))


def test_index_closes_memo_is_keyed_on_the_frame(monkeypatch):
    """The SPY slice is shared across symbols on a bar, but never across frames."""
    import datetime as _dt
    D.reset_caches()
    n = 400
    close = np.linspace(300.0, 420.0, n)
    frames = {"v1": _frame(close), "v2": _frame(close * 1.1)}
    state = {"which": "v1", "calls": 0}

    class _FakeOhlcv:
        def get_ohlcv_data(self, symbol, start_date, end_date, interval):
            state["calls"] += 1
            return frames[state["which"]].copy()

    class _Bundle:
        def ohlcv(self):
            return _FakeOhlcv()

    providers = _Bundle()
    as_of = _dt.datetime(2020, 6, 1)
    first = D.fetch_index_closes(providers, as_of, "SPY")
    assert state["calls"] == 1
    for _ in range(20):                       # every symbol on the same bar
        again = D.fetch_index_closes(providers, as_of, "SPY")
        assert again is first                 # memo hit: no rebuild
    assert state["calls"] == 1

    # A new frame (reset + a different payload) must NOT serve the memo entry.
    D.reset_caches()
    state["which"] = "v2"
    fresh = D.fetch_index_closes(providers, as_of, "SPY")
    assert state["calls"] == 2
    assert float(fresh.iloc[-1]) != float(first.iloc[-1])


# --------------------------------------------------------------------------
# RISK 2, end to end: the LIVE gather/process pair (as_of=None)
# --------------------------------------------------------------------------
def _live_expert_and_providers(frame: pd.DataFrame, monkeypatch):
    """A DeterministicScorer wired to a fake bundle, on the LIVE branch.

    ``as_of=None`` throughout -- the same pair ``_gather_and_process`` drives
    from ``run_analysis``. Statements/macro are pinned so the test measures the
    OHLCV/technical path and nothing else.
    """
    from ba2_common.core.backtest_context import LiveProviderBundle
    from ba2_experts.DeterministicScorer import DeterministicScorer

    class _FakeOhlcv:
        def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d"):
            return frame.copy()

    monkeypatch.setattr(D, "fetch_statements", lambda *a, **k: {
        "income": [], "balance": [], "cashflow": []})
    monkeypatch.setattr(D, "fetch_macro_series", lambda *a, **k: {
        "vix": 16.0, "unrate_series": None,
        "spread_10y3m_series": None, "oas_series": None})

    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_w_analyst = 0.0
    e._gather_w_earnings = 0.0
    e._gather_index_symbol = "SPY"
    e._gather_use_model_target = False
    return e, LiveProviderBundle(lambda cat, name, **kw: _FakeOhlcv() if cat == "ohlcv" else None)


def _live_calc(e, providers, settings):
    bundle = e._gather(providers, as_of=None)
    rec = e._process(bundle, settings, as_of=None)
    return rec.raw_outputs["calc"], rec


def test_live_path_identical_with_and_without_the_view(monkeypatch):
    """Live (as_of=None) must produce the same recommendation either way.

    Run 1 is the real live flow: ``_gather`` caches the frame, builds a view,
    and ``_process`` reads memoised series. Run 2 drops the views so the very
    same bundle goes down the per-slice path. Identical, field for field.
    """
    _, frame = next(iter(_synthetic_frames()))
    e, providers = _live_expert_and_providers(frame, monkeypatch)
    settings = _settings(PARAM_SETS[0])
    settings.update({"min_history_days": 260, "w_analyst": 0.0, "w_earnings": 0.0})

    D.reset_caches()
    with_view, rec_a = _live_calc(e, providers, settings)
    assert T.series_memo_stats()["miss"] == 6, "the live path did not use the view"

    D.reset_caches()
    monkeypatch.setattr(D, "frame_view", lambda *a, **k: None)
    without_view, rec_b = _live_calc(e, providers, settings)
    assert T.series_memo_stats()["miss"] == 0, "the slice path still touched the memo"

    assert with_view == without_view
    for field in ("signal", "confidence", "current_price", "details",
                  "expected_profit_percent", "target_price"):
        assert getattr(rec_a, field) == getattr(rec_b, field), field


def test_live_rerun_after_a_new_bar_is_not_served_stale(monkeypatch):
    """The live staleness hazard, stated directly.

    Live re-runs as bars arrive. A memo keyed on the symbol alone would answer
    the second run with yesterday's indicators; keyed on the frame fingerprint,
    the new payload misses and is recomputed.
    """
    _, base = next(iter(_synthetic_frames()))
    yesterday = base.iloc[:-1].reset_index(drop=True)
    settings = _settings(PARAM_SETS[0])
    settings.update({"min_history_days": 260, "w_analyst": 0.0, "w_earnings": 0.0})

    D.reset_caches()
    e, providers = _live_expert_and_providers(yesterday, monkeypatch)
    first, rec_first = _live_calc(e, providers, settings)

    # A new bar arrives, well away from yesterday's close.
    today = base.copy()
    today.loc[today.index[-1], ["Close", "High", "Low"]] = [
        float(base["Close"].iloc[-2]) * 1.4] * 3
    D.reset_caches()                      # what /api/reload does between runs
    e2, providers2 = _live_expert_and_providers(today, monkeypatch)
    second, rec_second = _live_calc(e2, providers2, settings)

    assert rec_second.current_price != rec_first.current_price
    assert second["technical"] != first["technical"], "served a stale technical score"
