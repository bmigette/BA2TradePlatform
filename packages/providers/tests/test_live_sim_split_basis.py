"""Split basis of the screener simulation: the OHLCV cache is split-adjusted as of its fetch, the vendor's share counts
are RAW as of each day (measured 2026-10-08: NVDA's series jumps 10x ON 2024-06-10, GOOGL's 20x around 2022-07-18).
So the market cap and the price thresholds use the AS-TRADED figures: ``split_basis.as_traded_factor`` per session."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from ba2_common.core.split_basis import CalendarSplit, as_traded_factor
from ba2_providers.screener import live_sim as ls
from ba2_providers.screener import live_sim_build as lb


def _cols(n, **over):
    base = dict(symbols=np.array([f"T{i}" for i in range(n)]), shares=np.full(n, 1e6), last_close=np.full(n, 100.0),
                rvol=np.full(n, 2.0), last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool),
                fl=np.full(n, np.nan), peak=None)
    base.update(over)
    return base


def test_market_cap_and_price_thresholds_use_the_as_traded_figures():
    syms = np.array(["SPL", "PLAIN", "UNK"])
    cols = _cols(3, symbols=syms, shares=np.array([1e7, 1e8, 1e8]), last_close=np.array([100.0, 100.0, 100.0]),
                 fac=np.array([10.0, 1.0, np.nan]))
    now = np.array([100.0, 100.0, 100.0])
    st = {"market_cap_min": 5e9, "market_cap_max": 1e10, "max_stocks": 9}
    got = ls.select_from_columns(**cols, now=now, settings=st, beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got] == ["PLAIN", "SPL"]       # SPL: 100 x 10 x 1e7 = 1e10; UNK: unknown calendar
    nofac = {k: v for k, v in cols.items() if k != "fac"}
    got0 = ls.select_from_columns(**nofac, now=now, settings=st, beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got0] == ["PLAIN", "UNK"]      # the pre-fix behaviour: SPL fell out of the band
    got2 = ls.select_from_columns(**cols, now=now, settings={"market_cap_min": 5e9, "price_max": 500.0, "max_stocks": 9},
                                  beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got2] == ["PLAIN"]             # SPL traded at 1000 raw


def test_factor_matrix_equals_the_repo_s_as_traded_factor(tmp_path):
    cache = str(tmp_path)
    (tmp_path / "screener_fundamentals" / "splits").mkdir(parents=True)
    cal = {"AAA": [("2024-06-10", 10, 1), ("2022-01-05", 3, 2)], "BBB": [("2023-03-01", 1, 8)], "CCC": []}
    for sym, sp in cal.items():
        (tmp_path / "screener_fundamentals" / "splits" / f"{sym}.json").write_text(json.dumps(
            {"symbol": sym, "historical": [{"date": d, "numerator": n, "denominator": de} for d, n, de in sp]}))
    sessions = [d.date().isoformat() for d in pd.bdate_range("2021-12-01", "2024-07-30")]
    basis = date(2024, 7, 30).toordinal()
    fac, rep = lb.build_factor_matrix(cache, ["AAA", "BBB", "CCC", "NOCAL"], sessions, basis)
    assert np.isnan(fac[3]).all() and rep["splits_unknown"] == ["NOCAL"] and rep["symbols_with_split_in_window"] == 2
    for s_, sym in enumerate(["AAA", "BBB", "CCC"]):
        splits = [CalendarSplit(date.fromisoformat(d), n / de) for d, n, de in cal[sym]]
        for t, day in enumerate(sessions):
            want = as_traded_factor(splits, date.fromisoformat(day), basis_date=date(2024, 7, 30))
            assert abs(float(fac[s_, t]) / want - 1) < 1e-6, (sym, day, fac[s_, t], want)
    assert abs(float(fac[0, sessions.index("2024-06-07")]) - 10.0) < 1e-5
    assert abs(float(fac[0, sessions.index("2024-06-10")]) - 1.0) < 1e-6      # the ex-date morning is post-split: raw == adjusted


def test_vendor_share_history_is_used_raw_as_of_the_day(tmp_path):
    """The shares matrix takes the vendor's dated series as of each session (a row is usable from its own date)."""
    cache = str(tmp_path)
    sh = tmp_path / "screener_fundamentals" / "shares"
    sh.mkdir(parents=True)
    pd.DataFrame({"date": ["2024-05-30", "2024-06-10"], "outstanding": [2.45983e9, 2.439413e10]}).to_parquet(sh / "NVDA.parquet", index=False)
    (tmp_path / "screener" / "vendor_shares").mkdir(parents=True)
    snap = tmp_path / "screener" / "vendor_shares" / "2026-10-08.json"
    snap.write_text(json.dumps({"fetched_at_ny": "2026-10-08T02:50:00", "rows": {"NVDA": 2.43e10},
                                "caps": {"NVDA": [3.0e12, 3.0e12 / 2.43e10]}}))
    sessions = [d.date().isoformat() for d in pd.bdate_range("2024-05-28", "2024-06-14")]
    idx = np.arange(len(sessions))
    c = np.full(len(sessions), 100.0)
    bars = {"NVDA": (idx, c, c, c, c, c)}
    bar_ord = {"NVDA": np.array([date.fromisoformat(s).toordinal() for s in sessions])}
    out, rep = lb.build_shares_matrix(cache, ["NVDA"], sessions, bars, str(snap), 45, bar_ord)
    assert rep["sources"]["vendor_history"] + rep["sources"]["vendor_history_plus_fmp_pre_history"] == 1
    assert out[0, sessions.index("2024-06-07")] == 2.45983e9 * 1.0
    assert out[0, sessions.index("2024-06-10")] == 2.439413e10                # raw, from its own date
