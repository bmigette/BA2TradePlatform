"""Point-in-time panel and targets for the custom ML models (app/services/ml_panel.py, ml_targets.py)."""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services import ml_panel as P
from app.services import ml_targets as T

FIRST, LAST = date(2023, 1, 3), date(2024, 3, 28)


def _write_5min(root, sym, sessions, seed):
    rng = np.random.default_rng(seed)
    rows = []
    price = 100.0
    for _, s in sessions.iterrows():
        t = s["open_ts"]
        while t + pd.Timedelta(minutes=5) <= s["close_ts"] + pd.Timedelta(hours=2):   # also prints after the close
            o = price
            price = max(1.0, price * float(np.exp(rng.normal(0, 0.002))))
            hi, lo = max(o, price) * 1.001, min(o, price) * 0.999
            rows.append((t, o, hi, lo, price, float(rng.integers(1000, 5000))))
            t += pd.Timedelta(minutes=5)
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["effective_date"] = df["Date"]
    d = root / "AlpacaHistoryOHLCVProvider"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{sym}_5min.parquet", index=False)


def _write_fundamentals(root, sym):
    d = root / "fmp_history"
    d.mkdir(parents=True, exist_ok=True)
    earn = []
    q = date(2022, 1, 27)
    for i in range(12):
        earn.append({"date": str(q + timedelta(days=91 * i)), "symbol": sym, "eps": 1.0 + 0.1 * i, "epsEstimated": 1.0,
                     "time": "amc" if i % 2 else "bmo", "revenue": 1e9 * (1 + 0.05 * i), "revenueEstimated": 1e9})
    earn.append({"date": "2030-01-30", "symbol": sym, "eps": None, "epsEstimated": 2.0, "time": "amc"})
    (d / f"past_earnings_quarterly__{sym}.json").write_text(json.dumps(earn))
    ann = [{"date": f"{y}-12-31", "acceptedDate": f"{y + 1}-02-02 16:30:00", "revenue": 4e9 * (1 + 0.1 * (y - 2020)),
            "grossProfit": 2e9, "operatingIncome": 1e9} for y in range(2020, 2024)]
    (d / f"income_statement_annual__{sym}.json").write_text(json.dumps(ann))


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    root = tmp_path_factory.mktemp("cache")
    sessions = P.regular_sessions(FIRST, LAST)
    for i, sym in enumerate(("SPY", "QQQ", "AAA", "BBB")):
        _write_5min(root, sym, sessions, i)
        _write_fundamentals(root, sym)
    return root


def _panel(cache, last=LAST, tf="1h"):
    return P.build_panel(P.PanelSpec(tf, FIRST, last, ("AAA", "BBB")), cache)


def test_bars_are_regular_session_and_stamped_at_end(cache):
    panel, meta = _panel(cache)
    s = P.regular_sessions(FIRST, LAST).set_index("session")
    ends = panel.groupby("session")["end"].max()
    assert (ends.values == s.loc[ends.index, "close_ts"].values).all()        # half days end at the early close
    first = panel[panel["bar"] == 0].set_index("session")["end"]
    assert (first.values == (s.loc[first.index, "open_ts"] + pd.Timedelta(hours=1)).values).all()
    assert meta["bars_per_session"] == 7 and panel.groupby(["symbol", "session"]).size().max() == 7


def test_features_are_point_in_time(cache):
    """A panel cut at an earlier day has identical features on every common row: no feature reads a later bar."""
    full, meta = _panel(cache)
    cut_day = date(2024, 2, 15)
    cut, _ = _panel(cache, last=cut_day)
    feats = meta["features"]
    a = full[pd.to_datetime(full["session"]) <= pd.Timestamp(cut_day)].reset_index(drop=True)
    b = cut.reset_index(drop=True)
    assert len(a) == len(b)
    pd.testing.assert_frame_equal(a[feats], b[feats], check_exact=False, rtol=1e-10)


@pytest.mark.parametrize("cut", ["2024-02-15 11:30", "2024-02-15 10:30", "2024-02-16 15:30"])
def test_features_are_point_in_time_at_an_intraday_cut(cache, tmp_path, cut):
    """The cache truncated at an INTRADAY bar end: every common row (bar end <= cut) is identical, features AND the
    barrier ATR. A daily-context or market feature that read today's close / high / low (the old same-session
    look-ahead) would differ on the bars of the cut day."""
    import shutil
    cut_ts = pd.Timestamp(cut)
    trunc = tmp_path / "cache"
    shutil.copytree(cache, trunc)
    for p in (trunc / "AlpacaHistoryOHLCVProvider").glob("*_5min.parquet"):
        df = pd.read_parquet(p)
        df[df["Date"] + pd.Timedelta(minutes=5) <= cut_ts].to_parquet(p, index=False)
    full, meta = _panel(cache)
    part, _ = P.build_panel(P.PanelSpec("1h", FIRST, cut_ts.date(), ("AAA", "BBB")), trunc)
    cols = meta["features"] + ["atr_d"]
    a = full[full["end"] <= cut_ts].reset_index(drop=True)
    b = part.reset_index(drop=True)
    assert len(a) == len(b) and (a["end"].values == b["end"].values).all()
    assert (b["end"].dt.date == cut_ts.date()).sum() > 0             # the cut day is really in the comparison
    pd.testing.assert_frame_equal(a[cols], b[cols], check_exact=False, rtol=1e-10)


def test_no_current_fundamentals_or_target_in_features(cache):
    panel, meta = _panel(cache)
    feats = meta["features"]
    assert not [c for c in feats if "live_snapshot" in c or c.startswith(("fwd_ret", "tgt_idx"))]
    assert {"atr_d", "close", "high"}.isdisjoint(feats)


def test_fundamentals_visibility():
    sess = [date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31)]
    assert P.fundamentals_visible_from("earnings", {"date": "2024-01-30", "time": "bmo"}, sess) == date(2024, 1, 30)
    assert P.fundamentals_visible_from("earnings", {"date": "2024-01-30", "time": "amc"}, sess) == date(2024, 1, 31)
    assert P.fundamentals_visible_from("earnings", {"date": "2024-01-30", "time": ""}, sess) == date(2024, 1, 31)
    assert P.fundamentals_visible_from("annual", {"acceptedDate": "2024-01-29 06:00:00"}, sess) == date(2024, 1, 30)
    assert P.fundamentals_visible_from("annual", {"acceptedDate": "2024-01-31 06:00:00"}, sess) is None


def test_market_features_use_same_bar(cache):
    panel, _ = _panel(cache)
    spy = P.load_bars("SPY", "1h", P.regular_sessions(FIRST, LAST), cache)
    lr = np.log(spy["close"]).diff()
    a = panel[panel["symbol"] == "AAA"].reset_index(drop=True)
    np.testing.assert_allclose(a["m_spy_ret_1"].values[1:], lr.values[1:], rtol=1e-12)
    np.testing.assert_allclose(a["m_rs_qqq_1"], a["t_ret_1"] - a["m_qqq_ret_1"], rtol=1e-9, atol=1e-15)


def test_forward_return_and_barrier(cache):
    panel, _ = _panel(cache)
    a = panel[panel["symbol"] == "AAA"]
    i = a.index[100]
    j = a.loc[i, "tgt_idx_3"]
    assert a.iloc[j]["session"] > a.loc[i, "session"] and a.iloc[j]["bar"] == a.loc[i, "bar"]
    assert a.loc[i, "fwd_ret_3"] == pytest.approx(a.iloc[j]["close"] / a.loc[i, "close"] - 1)
    assert np.isnan(a["fwd_ret_3"].iloc[-1]) and a["tgt_idx_3"].iloc[-1] == -1


def test_triple_barrier_labels_first_touch_and_ties():
    up = np.array([[0.5, 1.2, 1.2], [0.2, 0.2, 0.2], [1.1, 1.1, 1.1], [0.1, 0.3, 0.4], [np.nan] * 3])
    dn = np.array([[0.1, 0.1, 1.5], [0.5, 1.1, 1.1], [1.0, 1.0, 1.0], [0.1, 0.2, 0.3], [np.nan] * 3])
    lab = T.triple_barrier_labels(up, dn, 1.0, 1.0)
    assert lab[:4].tolist() == [2.0, 0.0, 0.0, 1.0]          # win, loss, same-bar tie = loss, timeout
    assert np.isnan(lab[4])


def test_folds_and_purge(cache):
    folds = T.quarterly_folds()
    assert [f.name for f in folds] == ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    assert folds[-1].pred_end == date(2026, 10, 12) and folds[0].train_start == date(2024, 1, 1)
    panel, _ = _panel(cache)
    f = T.Fold("t", date(2023, 6, 1), date(2024, 1, 2), date(2024, 3, 28))
    tr, pr = T.fold_rows(panel, f, 5)
    tgt_sess = pd.to_datetime(panel["session"]).dt.date.values[panel["tgt_idx_5"].values[tr]]
    # tgt_idx is a row index WITHIN the symbol: map through the symbol's rows
    for sym, g in panel.iloc[tr].groupby("symbol"):
        rows = panel[panel["symbol"] == sym].reset_index(drop=True)
        assert (pd.to_datetime(rows.loc[g["tgt_idx_5"].values, "session"]).dt.date < f.pred_start).all()
    assert (pd.to_datetime(panel["session"].iloc[pr]).dt.date >= f.pred_start).all()


def test_balance_refusal():
    spec = T.TargetSpec("direction", 3)
    with pytest.raises(T.TargetBalanceError):
        T.check_balance(spec, "x", {"pred": [0.1, 0.45, 0.45]})
    T.check_balance(spec, "x", {"pred": [0.2, 0.4, 0.4]})


def test_direction_thresholds_fitted_on_training_rows_only(cache):
    panel, _ = _panel(cache)
    spec = T.TargetSpec("direction", 1)
    f = T.Fold("t", date(2023, 6, 1), date(2024, 1, 2), date(2024, 3, 28))
    tr, pr = T.fold_rows(panel, f, 1)
    a = T.fit_target(panel, spec, tr)
    shifted = panel.copy()
    shifted.loc[pr, "fwd_ret_1"] += 0.5                      # changing prediction-block outcomes...
    b = T.fit_target(shifted, spec, tr)
    assert a.params == b.params                               # ...never moves the thresholds


def test_ticker_frame_and_labels(cache):
    df = P.ticker_frame("AAA", "1h", date(2024, 1, 2), LAST, P.FEATURE_GROUPS, cache)
    assert df.index.name == "Date" and {"Open", "Close"} <= set(df.columns)
    assert not [c for c in df.columns if c.startswith(("fwd_ret", "tgt_idx"))]
    mask = np.zeros(len(df), bool)
    mask[: len(df) // 2] = True
    lab, params, look = T.labels_for_frame(df, T.TargetSpec("triple_barrier", 2, 1.0), mask)
    assert set(np.unique(lab.dropna())) <= {0.0, 1.0, 2.0} and params["k"] > 0 and look == 14


def test_binary_and_regression_targets(cache):  # noqa: F811
    panel, _ = _panel(cache)
    f = T.Fold("t", date(2023, 6, 1), date(2024, 1, 2), date(2024, 3, 28))
    tr, pr = T.fold_rows(panel, f, 3)
    b = T.fit_target(panel, T.TargetSpec("binary_direction", 3), tr)
    fwd = panel["fwd_ret_3"].values
    ok = np.isfinite(fwd)
    assert set(np.unique(b.labels[ok])) <= {0.0, 2.0} and (b.labels[ok] == 2.0).sum() == (fwd[ok] > 0).sum()
    T.check_balance(T.TargetSpec("binary_direction", 3), "t", {"train": T.class_shares(b.labels, tr)})
    with pytest.raises(T.TargetBalanceError):
        T.check_balance(T.TargetSpec("binary_direction", 3), "t", {"pred": [0.9, 0.0, 0.1]})
    bb = T.fit_target(panel, T.TargetSpec("binary_barrier", 3, 1.0), tr)
    tb = T.fit_target(panel, T.TargetSpec("triple_barrier", 3, 1.0), tr)
    both = ~np.isnan(tb.labels)
    assert ((bb.labels[both] == 2) == (tb.labels[both] == 2)).all() and bb.params["k"] == tb.params["k"]
    r = T.fit_target(panel, T.TargetSpec("regression", 3), tr)
    vol = (panel["atr_d"] / panel["close"]).values * np.sqrt(3)
    good = np.isfinite(r.labels)
    assert np.allclose(r.labels[good], np.clip(fwd[good] / vol[good], -5, 5)) and np.abs(r.labels[good]).max() <= 5
    assert r.class_returns is None and not T.TargetSpec("regression", 3).is_classification
