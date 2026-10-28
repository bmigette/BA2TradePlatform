"""Point-in-time universe from the screener metric store (app/services/ml_universe.py)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_holdout as H
from app.services import ml_universe as U

# real NDQ30 names so the committed lock list decides: ADBE AMZN MSFT NVDA are LOCKED, AAPL AMD TSLA are dev
LOCKED = ("ADBE", "AMZN", "MSFT", "NVDA")
DEV = ("AAPL", "AMD", "TSLA")
SCANS = [d.strftime("%Y-%m-%d") for d in pd.date_range("2022-01-05", "2022-03-30", freq="7D")]      # Wednesdays


def write_metric_store(root, frame: pd.DataFrame) -> str:
    """Write ``frame`` (one row per (date, symbol)) as the month partitions a real store has; returns the dir."""
    root.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    frame["_ym"] = frame["date"].str[:7]
    for ym, part in frame.groupby("_ym"):
        d = root / f"ym={ym}"
        d.mkdir(exist_ok=True)
        part.drop(columns="_ym").to_parquet(d / "part-00001.parquet", index=False)
    return str(root)


def _row(scan, sym, cap, price=50.0, volume=2e6, sector="Technology", mom=0.1, stage=2.0, rv=1.0):
    return dict(date=scan, symbol=sym, sector=sector, close=price, price=price, market_cap=cap, volume=volume,
                relative_volume=rv, weinstein_stage=stage, momentum_12_1=mom)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    rows = []
    for k, scan in enumerate(SCANS):
        caps = {"AAPL": 3.0e12, "MSFT": 2.5e12, "AMZN": 1.8e12, "NVDA": 1.0e12, "TSLA": 5e11, "ADBE": 3e11,
                "AMD": 4e11 if scan < "2022-02-02" else 9e12}                    # AMD jumps to #1 on the 2022-02-02 scan
        for sym, cap in caps.items():
            rows.append(_row(scan, sym, cap))
        rows.append(_row(scan, "PENNY", 5e12, price=3.0))                          # biggest cap but price <= 5
        rows.append(_row(scan, "ILLIQ", 4e12, volume=100.0 if k != 3 else 5e9))    # thin, one huge print on one scan
    return write_metric_store(tmp_path_factory.mktemp("ms") / "metric_store_t", pd.DataFrame(rows))


def test_the_lock_list_is_what_the_fixture_assumes():
    assert all(H.is_locked_largecap(s) for s in LOCKED) and not any(H.is_locked_largecap(s) for s in DEV)


def test_newest_visible_scan_only_and_never_a_later_one(store):
    # Tuesday 2022-02-01: the scans of 2022-01-26 is the newest; the 2022-02-02 scan (AMD = #1) does not exist yet
    before = U.universe_as_of(date(2022, 2, 1), 3, store, "dev", min_dollar_volume=1e6)
    assert "AMD" not in before and before[0] == "AAPL"
    on = U.universe_as_of(date(2022, 2, 2), 3, store, "dev", min_dollar_volume=1e6)      # daily clock: S <= D
    assert on[0] == "AMD"
    # the visible-scan helper agrees
    assert U.visible_scan(store, date(2022, 2, 1)) == "2022-01-26" and U.visible_scan(store, date(2022, 2, 2)) == "2022-02-02"


def test_universe_holds_between_scans(store):
    ref = U.universe_as_of(date(2022, 2, 2), 4, store, "dev", min_dollar_volume=1e6)
    for day in (date(2022, 2, 3), date(2022, 2, 6), date(2022, 2, 8)):                   # incl. a Sunday
        assert U.universe_as_of(day, 4, store, "dev", min_dollar_volume=1e6) == ref
    assert U.visible_scan(store, date(2022, 2, 9)) == "2022-02-09"


def test_dev_excludes_locked_and_holdout_returns_only_locked(store):
    day = date(2022, 3, 10)
    dev = U.universe_as_of(day, 20, store, "dev", min_dollar_volume=1e6)
    hold = U.universe_as_of(day, 20, store, "holdout", min_dollar_volume=1e6)
    assert set(dev) >= set(DEV) and not set(dev) & set(LOCKED)
    assert set(hold) >= set(LOCKED) and not set(hold) & set(DEV)
    assert all(H.is_locked_largecap(s) for s in hold) and not any(H.is_locked_largecap(s) for s in dev)
    assert not set(dev) & set(hold)


def test_top_n_is_taken_before_the_split(store):
    """top_n names by market cap qualify, then the purpose keeps its share: dev + holdout partition them."""
    day = date(2022, 3, 10)
    full = U.universe_as_of(day, 5, store, "dev", min_dollar_volume=1e6) + U.universe_as_of(day, 5, store, "holdout", min_dollar_volume=1e6)
    assert len(full) <= 5 and len(set(full)) == len(full)
    top5 = ["AMD", "AAPL", "MSFT", "AMZN", "NVDA"]
    assert set(full) == set(top5)


def test_price_and_median_dollar_volume_gates(store):
    u = U.universe_as_of(date(2022, 3, 10), 50, store, "dev", min_dollar_volume=1e6)
    assert "PENNY" not in u                                    # price 3 <= 5
    assert "ILLIQ" not in u                                    # median dollar volume is tiny; one big print does not count
    spike = U.universe_as_of(date(2022, 1, 27), 50, store, "dev", min_dollar_volume=1e6)      # window holds the big print
    assert "ILLIQ" not in spike
    low = U.universe_as_of(date(2022, 3, 10), 50, store, "dev", min_dollar_volume=0.0)
    assert "ILLIQ" in low and "PENNY" not in low               # the volume gate is the only thing keeping ILLIQ out
    high = U.universe_as_of(date(2022, 3, 10), 50, store, "dev", min_dollar_volume=1e12)
    assert high == []


def test_before_the_first_scan_and_after_the_store_ends_refuse(store):
    with pytest.raises(U.UniverseUnavailable):
        U.universe_as_of(date(2022, 1, 4), 3, store, "dev", min_dollar_volume=1e6)
    from ba2_providers.screener.metric_store import MetricStoreStaleError
    with pytest.raises(MetricStoreStaleError):
        U.universe_as_of(date(2022, 6, 30), 3, store, "dev", min_dollar_volume=1e6)


def test_bad_purpose_refuses(store):
    with pytest.raises(ValueError, match="purpose"):
        U.universe_as_of(date(2022, 3, 10), 3, store, "all")


def test_the_store_is_loaded_once_per_process(store):
    assert U.scan_store(store) is U.scan_store(store)
    assert U.scan_store(store).dates == SCANS


def test_scan_values_follow_the_visible_scan(store):
    sessions = pd.DatetimeIndex(["2022-01-04", "2022-01-05", "2022-01-12", "2022-01-14", "2022-02-02"])
    vals = U.scan_values(["AAPL", "AMD", "GONE"], sessions, ["market_cap", "weinstein_stage"], store)
    cap = vals["market_cap"]
    assert cap.index.equals(sessions) and list(cap.columns) == ["AAPL", "AMD", "GONE"]
    assert cap.loc["2022-01-04"].isna().all()                            # before the first scan
    assert cap.loc["2022-01-05", "AAPL"] == 3.0e12 and cap.loc["2022-01-14", "AMD"] == 4e11
    assert cap.loc["2022-02-02", "AMD"] == 9e12                          # the scan dated that day (S <= D)
    assert cap["GONE"].isna().all()                                      # a symbol the store does not have


def test_sector_of_from_store_returns_known_sectors_only(store, tmp_path):
    assert U.sector_of_from_store(["AAPL", "AMD", "GONE"], store) == {"AAPL": "Technology", "AMD": "Technology"}
    rows = [_row(s, "NOSEC", 1e11, sector=None) for s in SCANS[:3]] + [_row(s, "TWO", 1e11, sector="Energy") for s in SCANS[:2]]
    rows += [_row(SCANS[2], "TWO", 1e11, sector="Utilities")]
    other = write_metric_store(tmp_path / "metric_store_s", pd.DataFrame(rows))
    assert U.sector_of_from_store(["NOSEC"], other) == {}
    with pytest.raises(U.UniverseUnavailable, match="TWO"):               # two different sectors: ambiguous, refuse
        U.sector_of_from_store(["TWO"], other)
