"""``tools/option_universe_preflight.py`` -- the committed stage-2 preflight (2026-09-26).

A throwaway cache root (FMP daily files, split calendars, a ThetaData tree, fmp_history DS files,
a screener metric store) exercises every section cache-only; the guard is exercised through its
wiring (the tool's acceptance run on real data is the 753-name stage-2 universe, in the
commit that added it). No network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from ba2_common.core.interfaces import OptionEodBar
from ba2_providers.options import thetadata as T
from ba2_providers.options.parquet_store import OptionHistoryParquetStore
from ba2_providers.options.tastytrade import occ_symbol

_REPO = Path(__file__).resolve().parents[3]
START, END = date(2024, 1, 2), date(2024, 12, 31)


def _tool():
    spec = importlib.util.spec_from_file_location("option_universe_preflight",
                                                  str(_REPO / "tools" / "option_universe_preflight.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["option_universe_preflight"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def tool():
    return _tool()


def _daily(root, sym, first=date(2022, 1, 3), last=END):
    days = pd.bdate_range(first, last)
    p = Path(root) / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Date": days, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                  "Volume": 1e6}).to_parquet(p, index=False)


def _split_calendar(root, sym):
    p = Path(root) / "fmp_history" / f"mc_stock_split__{sym}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"symbol": sym, "historical": []}), encoding="utf-8")


def _partition(store, sym, expiry, first_bar):
    occ = occ_symbol(sym, expiry, "C", 100.0)
    bars = [OptionEodBar(occ_symbol=occ, bar_date=first_bar + timedelta(days=i), open=1, high=1,
                         low=1, close=1, volume=1, open_interest=1, iv=0.3) for i in range(3)]
    store.write_partition(sym, expiry, bars, date(2020, 1, 1), expiry)


@pytest.fixture
def root(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    r = tmp_path / "cache"
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(r))
    for sym in ("AAA", "XYZ", "NOCAL"):
        _daily(r, sym)
    for sym in ("AAA", "XYZ"):
        _split_calendar(r, sym)
    theta = str(r / "ThetaDataOptionsProvider")
    hooked = OptionHistoryParquetStore(root=theta, **T.parquet_store_hooks())
    _partition(hooked, "AAA", date(2024, 1, 19), date(2024, 1, 2))
    _partition(hooked, "AAA", date(2024, 12, 20), date(2024, 12, 2))
    os.makedirs(os.path.join(theta, "AAA", "exp=2024-02-16"))            # unfinished: no manifest
    legacy = OptionHistoryParquetStore(
        root=theta, clock=lambda: T.ROOT_HISTORY_LEGACY_FETCHED["XYZ"] - timedelta(days=9))
    _partition(legacy, "XYZ", date(2024, 1, 19), date(2024, 1, 2))      # stale under XYZ <- SQ
    # DS: AAA has every namespace, XYZ misses one.
    hist = r / "fmp_history"
    rows = {"income_statement_annual": [{"date": "2023-12-31", "fillingDate": "2024-02-01"}],
            "balance_sheet_annual": [{"date": "2023-12-31", "fillingDate": "2024-02-01"}],
            "cashflow_statement_annual": [{"date": "2023-12-31", "fillingDate": "2024-02-01"}],
            "grades_historical": [{"date": "2024-06-01"}],
            "past_earnings_quarterly": [{"date": "2024-05-01", "eps": 1.0}],
            "price_target": []}
    for sym in ("AAA", "XYZ", "NOCAL"):
        for ns, v in rows.items():
            if sym == "XYZ" and ns == "price_target":
                continue
            (hist / f"{ns}__{sym}.json").write_text(json.dumps(v), encoding="utf-8")
    # Gate store: AAA and XYZ present, NOCAL absent.
    ms = r / "screener" / "metric_store" / "ym=2024-03"
    ms.mkdir(parents=True)
    pd.DataFrame({"date": ["2024-03-02", "2024-03-09", "2024-03-02"], "symbol": ["AAA", "AAA", "XYZ"],
                  "market_cap": [1e9, None, 2e9]}).to_parquet(ms / "part-00001.parquet", index=False)
    return str(r)


def test_the_ds_namespaces_are_the_ones_the_prewarm_warms(tool):
    from app.services.prewarm_fetchers import PrewarmFetchers
    seen = []
    fake = type("F", (), {"_warm": lambda self, ns, sym, lookback_days=None: seen.append(ns)})()
    PrewarmFetchers.do_deterministic_scorer(fake, "AAA")
    assert sorted(seen) == sorted(tool.DS_NAMESPACES)


def test_every_section_reports_and_the_blocking_ones_fail_the_run(tool, root, tmp_path):
    out = str(tmp_path / "pf.json")
    rep = tool.run(["AAA", "XYZ", "NOCAL", "NOTREE"], START, END, ["coverage", "split", "ds", "gate"],
                   out_path=out, guard_starts=[START], guard_phases=1, procs=1, cache_root=root,
                   gate_store=os.path.join(root, "screener", "metric_store"), log=lambda m: None)
    assert json.loads(Path(out).read_text())["summary"] == rep["summary"]
    rows = {r["symbol"]: r for r in rep["symbols"]}
    aaa = rows["AAA"]["coverage"]
    assert aaa["unfinished"] == ["2024-02-16"] and aaa["root_history_stale"] == []
    assert aaa["first_bar"] == "2024-01-02" and aaa["last_bar"] == "2024-12-04"
    assert rows["XYZ"]["coverage"]["root_history_stale"] == ["2024-01-19"]
    assert rows["NOTREE"]["coverage"]["theta"] is None
    assert rows["AAA"]["split"] == {"ok": True}
    assert not rows["NOCAL"]["split"]["ok"] and "split calendar" in rows["NOCAL"]["split"]["refused"]
    assert rep["ds"]["missing_files"] == ["price_target/XYZ"] + \
        [f"{ns}/NOTREE" for ns in tool.DS_NAMESPACES]
    assert rep["gate"]["absent"] == ["NOCAL", "NOTREE"]
    assert rep["gate"]["per_symbol"]["AAA"] == {"rows": 2, "mcap_missing": 1, "first": "2024-03-02",
                                                "last": "2024-03-09"}

    blocking = {(f["section"], f["symbol"]) for f in rep["findings"] if f["severity"] == "blocking"}
    assert blocking == {("coverage", "AAA"), ("coverage", "XYZ"), ("coverage", "NOTREE"), ("coverage", "NOCAL"),
                        ("split", "NOCAL"), ("split", "NOTREE")}
    review = {(f["section"], f["symbol"]) for f in rep["findings"] if f["severity"] == "review"}
    assert ("gate", "NOCAL") in review and ("ds", "XYZ") in review
    assert rep["summary"]["split_refused"] == ["NOCAL", "NOTREE"]


def test_a_guard_refusal_is_a_blocking_finding(tool, root, tmp_path, monkeypatch):
    calls = []

    def fake_guard(sym, cfg, fmp, basis):
        calls.append((sym, tuple(cfg["guard_starts"]), cfg["guard_phases"]))
        return {"runs": 2, "checks": 10, "carry_passed": 0, "refusals": [
            {"start": "2024-01-02", "phase": 1, "session": "2024-03-01", "why": "ratio 0.80"}]}

    monkeypatch.setattr(tool, "_guard", fake_guard)
    rep = tool.run(["AAA"], START, END, ["guard"], out_path=None, guard_starts=[START], guard_phases=2,
                   procs=1, cache_root=root, log=lambda m: None)
    assert calls == [("AAA", (START,), 2)]
    assert rep["summary"]["guard_refused"] == ["AAA"]
    (f,) = [f for f in rep["findings"] if f["severity"] == "blocking"]
    assert f["section"] == "guard" and "2024-03-01" in f["what"]


def test_the_cli_exit_codes(tool, root, tmp_path, monkeypatch, capsys):
    u = tmp_path / "u.txt"
    u.write_text("AAA\n", encoding="utf-8")
    out = str(tmp_path / "pf.json")
    gate = os.path.join(root, "screener", "metric_store")
    assert tool.main(["--universe-file", str(u), "--start", "2024-01-02", "--end", "2024-12-31",
                      "--out", out, "--sections", "ds,gate", "--procs", "1", "--gate-store", gate]) == 0
    assert tool.main(["--universe-file", str(u), "--start", "2024-01-02", "--end", "2024-12-31",
                      "--out", out, "--sections", "coverage", "--procs", "1"]) == 1   # unfinished
    assert tool.main(["--universe-file", str(u), "--start", "2024-01-02", "--end", "2024-12-31",
                      "--out", out, "--sections", "bogus"]) == 2
    assert tool.main(["--universe-file", str(tmp_path / "none.txt"), "--start", "2024-01-02",
                      "--end", "2024-12-31", "--out", out]) == 2


def test_a_late_option_start_is_flagged_only_against_the_stock_s_own_history(tool):
    """STLA-like (stock history from 2011, options from 2021-01-19: a chain under an earlier
    ticker, or a vendor gap) is a review finding; an IPO whose options follow its listing is not."""
    def row(sym, fmp_first, first_bar):
        return {"symbol": sym, "coverage": {"theta": True, "unfinished": [], "root_history_stale": [],
                                            "fmp_first": fmp_first, "first_bar": first_bar}}
    cfg = {"start": date(2020, 1, 1), "end": date(2025, 12, 31)}
    f = tool._findings([row("RENAMED", "2011-06-22", "2021-01-19"), row("IPO", "2020-12-10", "2020-12-16"),
                        row("EARLY", "2011-06-22", "2020-01-02")], None, None, cfg)
    assert [(x["symbol"], x["severity"]) for x in f] == [("RENAMED", "review")]
    assert "ROOT_HISTORY" in f[0]["what"]
