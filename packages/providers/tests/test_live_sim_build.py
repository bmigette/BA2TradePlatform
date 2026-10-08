"""End to end on a tiny synthetic cache: the panel build (fingerprint-named directory, coverage lists, split-aware raw shares,
resumability) and the prewarm/refusal contract."""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import pytest

from ba2_providers.screener import live_sim as ls
from ba2_providers.screener import live_sim_build as lb

SESSIONS = [d.date() for d in pd.bdate_range("2023-10-02", "2024-03-28")]


def _bars(path, last_day=None, close=100.0):
    days = [d for d in SESSIONS if last_day is None or d <= last_day]
    df = pd.DataFrame({"Date": pd.to_datetime(days), "Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1_000_000})
    df.to_parquet(path, index=False)


@pytest.fixture()
def cache(tmp_path):
    c = tmp_path / "cache"
    (c / "FMPOHLCVProvider").mkdir(parents=True)
    fund = c / "screener_fundamentals"
    for k in ("market_cap", "shares", "splits"):
        (fund / k).mkdir(parents=True)
    # A: normal, has the vendor share history; B: a 10:1 split on 2024-02-15 (cache adjusted), implied-only shares;
    # C: LISTED but stale bars; D: not listed, stale bars (delisted); E: no share source at all
    _bars(c / "FMPOHLCVProvider" / "A_1d.parquet")
    _bars(c / "FMPOHLCVProvider" / "B_1d.parquet", close=10.0)
    _bars(c / "FMPOHLCVProvider" / "C_1d.parquet", last_day=SESSIONS[20])
    _bars(c / "FMPOHLCVProvider" / "D_1d.parquet", last_day=SESSIONS[20])
    _bars(c / "FMPOHLCVProvider" / "E_1d.parquet")
    pd.DataFrame({"date": ["2023-06-01"], "outstanding": [2e7]}).to_parquet(fund / "shares" / "A.parquet", index=False)
    pd.DataFrame({"date": ["2023-06-01"], "outstanding": [2e7]}).to_parquet(fund / "shares" / "C.parquet", index=False)
    pd.DataFrame({"date": ["2023-06-01"], "outstanding": [2e7]}).to_parquet(fund / "shares" / "D.parquet", index=False)
    # B: as-traded cap 1e9 before the split (raw price 100 x 1e7 shares), 1e9 after (raw 10 x 1e8)
    caps = [(d.isoformat(), 1.0e9) for d in SESSIONS]
    pd.DataFrame({"date": [x[0] for x in caps], "market_cap": [x[1] for x in caps]}).to_parquet(fund / "market_cap" / "B.parquet", index=False)
    for s in ("A", "C", "D"):
        pd.DataFrame({"date": [x[0] for x in caps], "market_cap": [2e9] * len(caps)}).to_parquet(fund / "market_cap" / f"{s}.parquet", index=False)
    for s, sp in (("A", []), ("B", [{"date": "2024-02-15", "numerator": 10, "denominator": 1}]), ("C", []), ("D", []), ("E", [])):
        (fund / "splits" / f"{s}.json").write_text(json.dumps({"symbol": s, "historical": sp}))
    snap_dir = c / "screener" / "vendor_shares"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2024-04-01.json").write_text(json.dumps({
        "fetched_at_ny": "2024-04-01T20:00:00", "rows": {"A": 2e7, "B": 1e8, "C": 2e7},
        "caps": {"A": [2e9, 100.0], "B": [1e9, 10.0], "C": [2e9, 100.0], "E": [2e9, 100.0]}}))
    return c


def test_build_writes_a_fingerprint_directory_with_explicit_coverage_lists(cache):
    man = lb.build_daily_panel(str(cache), ["A", "B", "C", "D", "E"], "2023-10-02", "2024-03-28", workers=2, log=lambda m: None)
    path = man["path"]
    assert os.path.basename(path) == man["panel_fingerprint"] and os.path.dirname(path) == ls.panel_root(str(cache))
    assert not any(n.endswith((".tmp", ".building")) for n in os.listdir(path))
    assert man["stale_listed_symbols"] == ["C"]                       # in the vendor's current listing, bars stale
    assert man["delisted_symbols"] == ["D"] and man["delisted_symbols_count"] == 1
    assert man["shares_missing_listed"] == ["E"]                      # listed, no share source
    assert man["splits_unknown"] == [] and man["split_report"]["symbols_with_split_in_window"] == 1
    probs = ls.panel_problems(path, "2024-02-01", "2024-03-20", warmup_days=20)
    assert any("CURRENT listing" in p and "C" in p for p in probs) and any("no share-count source" in p for p in probs)
    # rebuilt with the stale symbol acknowledged and the share-less one dropped from the universe: clean
    man2 = lb.build_daily_panel(str(cache), ["A", "B", "C", "D"], "2023-10-02", "2024-03-28", workers=2,
                                log=lambda m: None, acknowledged_stale=["C"])
    assert ls.panel_problems(man2["path"], "2024-02-01", "2024-03-20", warmup_days=20) == []
    assert man2["panel_fingerprint"] != man["panel_fingerprint"]       # another universe -> another directory
    assert {d for d, _, _ in ls.list_panels(str(cache))} == {man["path"], man2["path"]}


def test_split_name_keeps_one_as_traded_cap_across_the_split(cache):
    man = lb.build_daily_panel(str(cache), ["A", "B"], "2023-10-02", "2024-03-28", workers=2, log=lambda m: None)
    pan = ls.load_panel(man["path"])
    i = pan.sym_index["B"]
    for day in ("2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16", "2024-03-20"):
        p = pan.pos(day)
        cap = float(pan.arrays["lc"][p, i]) * float(pan.arrays["shares"][p, i]) * float(pan.arrays["fac"][p, i])
        assert cap == pytest.approx(1.0e9, rel=1e-6), (day, cap)       # no 10x step at the split


def test_an_up_to_date_panel_is_a_no_op_and_force_rebuilds(cache):
    logs = []
    m1 = lb.build_daily_panel(str(cache), ["A", "B"], "2023-10-02", "2024-03-28", workers=2, log=logs.append)
    m2 = lb.build_daily_panel(str(cache), ["A", "B"], "2023-10-02", "2024-03-28", workers=2, log=logs.append)
    assert m1["status"] == "built" and m2["status"] == "up_to_date" and m1["path"] == m2["path"]


def test_a_vendor_listing_without_a_snapshot_refuses(tmp_path):
    with pytest.raises(ls.SimulationRefusal, match="no vendor share table"):
        lb.build_daily_panel(str(tmp_path), ["A"], "2023-10-02", "2024-03-28", log=lambda m: None)
