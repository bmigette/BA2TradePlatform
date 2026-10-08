"""``tools/cache_health_check.py``: the cross-interval PRICE-LEVEL check (ON by default).

The tool's other checks (worker CRC sync, date gaps, per-month coverage, NaN rates) never compared the
price LEVELS of a symbol's daily and intraday files, so 7 of 250 random symbols (a 5-minute history 0.10x ..
6.25x the daily one) passed all of them. This check runs over the FULL symbol set (the validity check samples
25: a sample would have missed ~97% of the bad symbols), exits non-zero on any mismatch, and writes the full
table with ``--basis-csv``.
"""
import csv
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

import tools.cache_health_check as H
from ba2_common.core import native_cache
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_providers.ohlcv import cross_interval_basis as cib

PROV = "FMPOHLCVProvider"


def _pair(seed=5):
    rng = np.random.default_rng(seed)
    price = 40.0
    d, i = [], []
    for _o, c in nyse_regular_sessions(date(2023, 1, 3), date(2023, 12, 29)):
        local = c.astimezone(NY_TZ)
        n = (local.hour * 60 + local.minute - 570) // 5
        path = price * np.exp(np.cumsum(rng.normal(0.0001, 0.0006, n)))
        price = float(path[-1])
        stamps = [pd.Timestamp(local.date()) + pd.Timedelta(minutes=570 + 5 * k) for k in range(n)]
        i.append(pd.DataFrame({"Date": stamps, "Open": path, "High": path * 1.0003, "Low": path * 0.9997,
                               "Close": path, "Volume": 1000.0}))
        d.append({"Date": pd.Timestamp(local.date()), "Open": float(path[0]), "High": float(path.max() * 1.0003),
                  "Low": float(path.min() * 0.9997), "Close": float(path[-1]), "Volume": 1e6})
    return pd.DataFrame(d), pd.concat(i, ignore_index=True)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    (root / PROV).mkdir(parents=True)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(root))
    cib._STORES.clear()
    yield root / PROV
    cib._STORES.clear()


def _put(folder, symbol, factor=1.0):
    daily, intra = _pair()
    for c in ("Open", "High", "Low", "Close"):
        intra[c] = intra[c] * factor
    daily.assign(effective_date=daily["Date"]).to_parquet(folder / f"{symbol}_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(folder / f"{symbol}_5min.parquet", index=False)


def _run(monkeypatch, tmp_path, *extra):
    argv = ["cache_health_check.py", "--skip-workers", "--skip-gaps", "--skip-validity",
            "--start", "2023-01-01", "--end", "2023-12-31", "--basis-memo-dir", str(tmp_path / "memo"),
            "--basis-workers", "1", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    return H.main()


def test_a_healthy_cache_exits_zero(cache, tmp_path, monkeypatch, capsys):
    _put(cache, "AAA")
    _put(cache, "BBB")
    assert _run(monkeypatch, tmp_path) == 0
    out = capsys.readouterr().out
    assert "CROSS-INTERVAL PRICE-LEVEL BASIS" in out and "FULL set, not a sample" in out
    assert "ok=2" in out and "ALL CHECKS PASSED" in out


def test_a_mismatched_symbol_exits_nonzero_and_is_listed_with_its_factor(cache, tmp_path, monkeypatch, capsys):
    _put(cache, "AAA")
    _put(cache, "DD", 1 / 3)
    _put(cache, "SIRI", 0.1)
    assert _run(monkeypatch, tmp_path) == 1
    out = capsys.readouterr().out
    assert "constant_factor=2" in out and "ok=1" in out
    assert "MISMATCH DD: constant_factor x0.3333" in out and "MISMATCH SIRI: constant_factor x0.1" in out
    assert "ISSUES FOUND" in out


def test_the_check_is_on_by_default_and_skip_basis_opts_out(cache, tmp_path, monkeypatch, capsys):
    _put(cache, "DD", 1 / 3)
    assert _run(monkeypatch, tmp_path) == 1
    capsys.readouterr()
    assert _run(monkeypatch, tmp_path, "--skip-basis") == 0
    assert "CROSS-INTERVAL" not in capsys.readouterr().out


def test_csv_holds_the_full_table_with_boundaries(cache, tmp_path, monkeypatch):
    daily, intra = _pair()
    pre = intra["Date"] < "2023-07-03"
    for c in ("Open", "High", "Low", "Close"):
        intra.loc[pre, c] = intra.loc[pre, c] * 6.25
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "SAFE_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(cache / "SAFE_5min.parquet", index=False)
    _put(cache, "OK1")
    # a daily file with no 5-minute file is not in the set (the set is the symbols WITH an intraday file);
    # an intraday file with no daily file is reported, not dropped
    _pair()[1].assign(effective_date=lambda d: d["Date"]).to_parquet(cache / "NODAILY_5min.parquet", index=False)
    out_csv = tmp_path / "out" / "basis.csv"
    assert _run(monkeypatch, tmp_path, "--basis-csv", str(out_csv)) == 1
    rows = {r["symbol"]: r for r in csv.DictReader(open(out_csv, encoding="utf-8"))}
    assert set(rows) == {"SAFE", "OK1", "NODAILY"}
    assert rows["SAFE"]["class"] == "factor_changes"
    assert abs(date.fromisoformat(rows["SAFE"]["boundaries"]) - date(2023, 7, 3)).days <= 3
    assert "x6.25" in rows["SAFE"]["segments"]
    assert rows["OK1"]["class"] == "ok" and rows["NODAILY"]["class"] == "no_daily"
    for col in ("symbol", "class", "factor", "median_close_ratio", "boundaries", "segments", "by_year",
                "common_sessions", "no_intraday_sessions", "far_sessions"):
        assert col in rows["SAFE"]


def test_a_stale_marker_fails_the_check_even_when_prices_agree(cache, tmp_path, monkeypatch, capsys):
    from ba2_common.core import split_basis
    _put(cache, "AAA")
    split_basis.write_intraday_stale(str(cache / "AAA_5min.parquet"), reason="daily history replaced")
    assert _run(monkeypatch, tmp_path) == 1
    assert "STALE marker" in capsys.readouterr().out


def test_basis_symbols_restricts_the_set(cache, tmp_path, monkeypatch, capsys):
    _put(cache, "AAA")
    _put(cache, "BAD", 0.5)
    assert _run(monkeypatch, tmp_path, "--basis-symbols", "AAA") == 0
    assert _run(monkeypatch, tmp_path, "--basis-symbols", "AAA,BAD") == 1
