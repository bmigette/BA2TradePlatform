"""Plan Task 12: the as-traded split basis of an ``alpaca``-history option run comes from the VERIFIED split table.

Option stores hold strikes as traded; the alpaca cache is RAW Alpaca bars back-adjusted by our own table
(``alpaca_history.build.adjust``: prices / (split F x spin-off F), F(d) = product of the factors of events dated AFTER d).
So factor(D) = that same product and ``adjusted close x factor`` = the raw close.

Unit tests use a synthetic certified cache (one 10:1 split, one spin-off, one symbol with no events). The integration test
reads the REAL local cache and the REAL raw store, and skips when either is absent.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

from ba2_common.core.split_basis import SplitBasisRefused

RUN_ID = "20261021T000000Z-unit"
BUILT_AT = "2026-10-21T10:00:00+00:00"
CSVS = ("split_review.csv", "split_accepted.csv", "split_vs_fmp.csv", "fmp_daily_inputs.csv", "yahoo_inputs.csv")


def _write_cache(root: Path, *, splits, spinoffs, manifest_events, symbols_1d_first="2016-01-04", certify=True,
                 manifest_sha_override=None) -> str:
    """A certified alpaca cache with the symbols SPL (events as given), FLAT (none) and a table that may disagree."""
    from ba2_providers.ohlcv.alpaca_history import verify as ver

    cache = root / "AlpacaHistoryOHLCVProvider"
    meta = cache / "_meta"
    meta.mkdir(parents=True)
    table = {"run_id": RUN_ID, "asof": "2026-10-12", "inputs_sha256": "i", "overrides_sha256": "o",
             "fmp_daily_files": {}, "yahoo_files": {}, "breaks": {}, "spinoffs": spinoffs,
             "absorbed_spinoffs": {}, "splits": splits}
    raw = json.dumps(table).encode()
    (meta / "split_table.json").write_bytes(raw)
    for name in CSVS:
        (meta / name).write_text(f"symbol,run_id\nSPL,{RUN_ID}\n", encoding="utf-8")
    sym = {s: {"1d": {"first": symbols_1d_first, "last": "2026-10-12", "bars": 10},
               "events": manifest_events.get(s, {"splits": [], "spinoffs": []})} for s in ("SPL", "FLAT")}
    sym["NOBARS"] = {"skipped": "no bars"}
    manifest = {"split_table": {"run_id": RUN_ID, "sha256": manifest_sha_override or hashlib.sha256(raw).hexdigest()},
                "built_at": BUILT_AT, "subset": False, "symbols": sym}
    (meta / "build_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (meta / "suspensions.json").write_text(json.dumps(
        {"build": f"{RUN_ID} built {BUILT_AT}", "rule": {}, "symbols": {}, "undecided": {}}), encoding="utf-8")
    if certify:
        ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})
    return str(cache)


_SPLIT = {"SPL": [["2024-06-10", 10.0]]}
_EVENTS = {"SPL": {"splits": [["2024-06-10", 10.0]], "spinoffs": []}}


@pytest.fixture
def cache(tmp_path):
    from app.services.backtest import option_split_basis as osb
    osb.clear_split_basis_memo()
    osb._ALPACA_MEMO.clear()
    yield _write_cache(tmp_path, splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS)
    osb._ALPACA_MEMO.clear()


def _basis(cache_dir, symbols, **kw):
    from app.services.backtest.option_split_basis import build_run_split_basis
    return build_run_split_basis(symbols, None, history_source="alpaca", cache_dir=cache_dir, **kw)


def test_factor_is_ten_before_the_split_and_one_on_and_after_it(cache):
    b = _basis(cache, ["SPL"])
    assert b.factor("SPL", date(2024, 6, 7)) == 10.0
    assert b.factor("SPL", date(2024, 6, 9)) == 10.0
    assert b.factor("SPL", date(2024, 6, 10)) == 1.0       # a bar ON the ex-date is already post-split
    assert b.factor("SPL", date(2026, 1, 2)) == 1.0


def test_a_symbol_with_zero_applied_events_has_a_proven_factor_of_one(cache):
    b = _basis(cache, ["FLAT"])
    assert b.factor("FLAT", date(2020, 1, 2)) == 1.0
    assert b.basis_of("FLAT").splits == ()


def test_spinoffs_scale_prices_like_splits_and_compose(tmp_path):
    root = _write_cache(tmp_path, splits=_SPLIT, spinoffs={"SPL": [["2025-10-30", 1.05]]},
                        manifest_events={"SPL": {"splits": [["2024-06-10", 10.0]], "spinoffs": [["2025-10-30", 1.05]]}})
    b = _basis(root, ["SPL"])
    assert b.factor("SPL", date(2024, 6, 7)) == pytest.approx(10.5)
    assert b.factor("SPL", date(2025, 1, 2)) == pytest.approx(1.05)
    assert b.factor("SPL", date(2025, 10, 30)) == 1.0


def test_dot_spelling_maps_to_the_cache_dash_spelling():
    from app.services.backtest.option_split_basis import _alpaca_symbol
    assert _alpaca_symbol("brk.b") == "BRK-B"


def test_a_symbol_missing_from_the_manifest_refuses_naming_it_never_one(cache):
    with pytest.raises(SplitBasisRefused) as e:
        _basis(cache, ["SPL", "GHOST", "PHANTOM"])
    assert "GHOST" in str(e.value) and "PHANTOM" in str(e.value) and "SPL:" not in str(e.value)


def test_a_symbol_with_no_daily_file_refuses(cache):
    with pytest.raises(SplitBasisRefused, match="NOBARS: the alpaca build has no daily file"):
        _basis(cache, ["NOBARS"])


def test_an_uncertified_cache_refuses(tmp_path):
    root = _write_cache(tmp_path, splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS, certify=False)
    with pytest.raises(SplitBasisRefused, match="not certified"):
        _basis(root, ["SPL"])


def test_a_missing_cache_or_table_refuses(tmp_path):
    with pytest.raises(SplitBasisRefused):
        _basis(str(tmp_path / "AlpacaHistoryOHLCVProvider"), ["SPL"])


def test_a_table_that_is_not_the_one_the_cache_was_built_with_refuses(tmp_path):
    root = _write_cache(tmp_path, splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS, manifest_sha_override="0" * 64)
    with pytest.raises(SplitBasisRefused, match="not the one the cache was built with"):
        _basis(root, ["SPL"])


def test_events_that_differ_from_what_the_build_recorded_refuse(tmp_path):
    root = _write_cache(tmp_path, splits=_SPLIT, spinoffs={}, manifest_events={"SPL": {"splits": [], "spinoffs": []}})
    with pytest.raises(SplitBasisRefused, match="differ from those the build applied"):
        _basis(root, ["SPL"])


def test_an_event_on_or_before_the_first_bar_is_not_applied_and_agrees_with_the_build(tmp_path):
    root = _write_cache(tmp_path, splits={"SPL": [["2015-06-01", 2.0], ["2024-06-10", 10.0]]}, spinoffs={},
                        manifest_events=_EVENTS)
    b = _basis(root, ["SPL"])
    assert b.factor("SPL", date(2016, 1, 4)) == 10.0


def test_non_daily_clock_refuses(cache):
    with pytest.raises(SplitBasisRefused, match="execution_interval"):
        _basis(cache, ["SPL"], interval="1h")


def test_an_unknown_source_refuses(cache):
    from app.services.backtest.option_split_basis import build_run_split_basis
    with pytest.raises(SplitBasisRefused, match="no as-traded split basis is defined"):
        build_run_split_basis(["SPL"], None, history_source="yahoo", cache_dir=cache)


def test_alpaca_without_a_cache_dir_refuses():
    from app.services.backtest.option_split_basis import build_run_split_basis
    with pytest.raises(SplitBasisRefused, match="cache_dir"):
        build_run_split_basis(["SPL"], None, history_source="alpaca")


def test_digest_changes_when_the_table_changes(tmp_path):
    a = _basis(_write_cache(tmp_path / "a", splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS), ["SPL"])
    same = _basis(_write_cache(tmp_path / "b", splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS), ["SPL"])
    changed = _basis(_write_cache(tmp_path / "c", splits={"SPL": [["2024-06-10", 20.0]]}, spinoffs={},
                                  manifest_events={"SPL": {"splits": [["2024-06-10", 20.0]], "spinoffs": []}}), ["SPL"])
    assert a.digest() == same.digest()          # a pure function of the table's content
    assert a.digest() != changed.digest()


def test_digest_changes_when_only_the_table_run_id_changes(tmp_path):
    from app.services.backtest.option_split_basis import clear_split_basis_memo
    root = _write_cache(tmp_path, splits=_SPLIT, spinoffs={}, manifest_events=_EVENTS)
    d1 = _basis(root, ["FLAT"]).digest()
    # same symbols, same factors, but a rebuilt table (another sha / run) must be another cache identity
    from app.services.backtest import option_split_basis as osb
    meta = Path(root) / "_meta"
    t = json.loads((meta / "split_table.json").read_text())
    t["inputs_sha256"] = "other"
    raw = json.dumps(t).encode()
    (meta / "split_table.json").write_bytes(raw)
    m = json.loads((meta / "build_manifest.json").read_text())
    m["split_table"]["sha256"] = hashlib.sha256(raw).hexdigest()
    (meta / "build_manifest.json").write_text(json.dumps(m), encoding="utf-8")
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})
    osb._ALPACA_MEMO.clear()
    clear_split_basis_memo()
    assert _basis(root, ["FLAT"]).digest() != d1


def test_options_store_wires_the_alpaca_basis_from_the_job_source(cache, monkeypatch):
    """build_options_run picks the path from the job's history source and the FMP loader is never touched."""
    from app.services.backtest import option_split_basis as osb
    from app.services.backtest import options_store as ostore
    from ba2_common.core.ohlcv_history import OHLCV_HISTORY_SOURCE_KEY

    calls = {}

    def fake(symbols, ohlcv_provider, *, interval="1d", history_source="fmp", cache_dir=None):
        calls.update(source=history_source, cache_dir=cache_dir, interval=interval)
        return "BASIS"

    monkeypatch.setattr(osb, "build_run_split_basis", fake)
    monkeypatch.setattr(ostore, "build_options_provider", lambda *a, **k: "PROVIDER")
    base = {"options_cache_db": "x", "execution_interval": "1d", "enabled_instruments": ["SPL"]}
    assert ostore.build_options_run({**base, OHLCV_HISTORY_SOURCE_KEY: "alpaca"}, price_source=None,
                                    ohlcv_provider=object()) == ("PROVIDER", "BASIS")
    assert calls["source"] == "alpaca" and calls["cache_dir"].endswith("AlpacaHistoryOHLCVProvider")
    calls.clear()
    ostore.build_options_run(dict(base), price_source=None, ohlcv_provider=object())     # unstamped = legacy fmp
    assert calls["source"] == "fmp" and calls["cache_dir"] is None


# --------------------------------------------------------------------------------------------------------------------
# REAL local cache vs the REAL raw Alpaca store
# --------------------------------------------------------------------------------------------------------------------
def _real_cache_dir():
    import ba2_common.config as cfg
    return os.path.join(cfg.CACHE_FOLDER, "AlpacaHistoryOHLCVProvider")


def _raw_closes(symbol):
    import pandas as pd
    import ba2_common.config as cfg
    for store in ("alpaca_store_daily2016", "alpaca_store"):
        p = os.path.join(cfg.BA2_HOME, store, "bars", "1Day", f"symbol={symbol}", "all.parquet")
        if os.path.isfile(p):
            df = pd.read_parquet(p)
            if "n" in df.columns:
                df = df[~((df["v"] == 0) & (df["n"] == 0))]          # the build drops placeholder bars
            day = df["t"].dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
            return dict(zip(day.dt.date, df["c"]))
    return None


@pytest.mark.parametrize("symbol,split_day", [("NVDA", date(2024, 6, 10)), ("AAPL", date(2020, 8, 31))])
def test_real_cache_adjusted_close_times_factor_is_the_raw_close(symbol, split_day):
    import pandas as pd
    from app.services import backtest as _  # noqa: F401  (import path sanity)
    from app.services.backtest.option_split_basis import build_alpaca_run_split_basis, clear_split_basis_memo
    from app.services.backtest.history_source import AlpacaCacheNotCertified, verify_alpaca_cache_certified

    cache = _real_cache_dir()
    adj_path = os.path.join(cache, f"{symbol}_1d.parquet")
    raw = _raw_closes(symbol)
    if not os.path.isfile(adj_path) or raw is None:
        pytest.skip("real alpaca history cache / raw store not present on this machine")
    try:
        verify_alpaca_cache_certified(cache)
    except AlpacaCacheNotCertified:
        pytest.skip("local alpaca cache is not certified")
    clear_split_basis_memo()
    basis = build_alpaca_run_split_basis([symbol], cache)
    adj = pd.read_parquet(adj_path, columns=["Date", "Close"])
    adj_by_day = dict(zip(pd.to_datetime(adj["Date"]).dt.normalize().dt.date, adj["Close"]))
    days = sorted(d for d in adj_by_day if d in raw)
    around = [d for d in days if abs((d - split_day).days) <= 8]
    assert any(d < split_day for d in around) and any(d >= split_day for d in around), "window does not straddle the split"
    for d in around:
        got = adj_by_day[d] * basis.factor(symbol, d)
        assert got == pytest.approx(raw[d], rel=1e-4), (symbol, d, got, raw[d])
    # and the far past (several events deep for NVDA: 4:1 2021 and 10:1 2024)
    for d in days[:: max(1, len(days) // 40)]:
        assert adj_by_day[d] * basis.factor(symbol, d) == pytest.approx(raw[d], rel=1e-4), (symbol, d)
