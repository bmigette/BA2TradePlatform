"""Tests for scripts/migrate_ohlcv_fetch_cache.py — relocate legacy fetch-cache OHLCV into native."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

# Load the one-off script (lives in scripts/, not a package) by path.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ohlcv_fetch_cache.py"
_spec = importlib.util.spec_from_file_location("migrate_ohlcv_fetch_cache", _SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def _src_bars(closes, start="2024-01-02"):
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({
        "Date": dates, "Open": 1.0, "High": 2.0, "Low": 0.5,
        "Close": list(closes), "Volume": 100.0,
    })


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Isolate native_cache at tmp_path and return (cache_root, legacy_fmp_dir, native_fmp_dir)."""
    from ba2_common.core import native_cache
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    legacy = tmp_path / "ohlcv" / "fmp"
    legacy.mkdir(parents=True)
    native = tmp_path / "FMPOHLCVProvider"
    return tmp_path, legacy, native


def test_migrates_new_symbol_with_effective_date(cache):
    root, legacy, native = cache
    _src_bars([10, 11, 12]).to_parquet(legacy / "AAA_5min.parquet", index=False)

    rc = migrate.main(["--cache-folder", str(root), "--apply"])
    assert rc == 0

    out_path = native / "AAA_5min.parquet"
    assert out_path.exists(), "migrated file must land in the native FMPOHLCVProvider dir"
    out = pd.read_parquet(out_path)
    assert "effective_date" in out.columns
    assert (pd.to_datetime(out["effective_date"]) == pd.to_datetime(out["Date"])).all()
    assert len(out) == 3


def test_dry_run_writes_nothing(cache):
    root, legacy, native = cache
    _src_bars([10, 11]).to_parquet(legacy / "AAA_5min.parquet", index=False)

    rc = migrate.main(["--cache-folder", str(root)])  # dry-run default
    assert rc == 0
    assert not (native / "AAA_5min.parquet").exists()
    assert (legacy / "AAA_5min.parquet").exists()  # source untouched


def test_merge_prefers_native_on_overlap(cache):
    root, legacy, native = cache
    native.mkdir()
    # Native already has D1,D2 with Close 100/200 (+effective_date, as the real cache would).
    nat = _src_bars([100, 200])
    nat["effective_date"] = nat["Date"]
    nat.to_parquet(native / "AAA_5min.parquet", index=False)
    # Source overlaps on D2 (Close 999, must be discarded) and adds D3 (Close 300).
    _src_bars([999, 300], start="2024-01-03").to_parquet(legacy / "AAA_5min.parquet", index=False)

    rc = migrate.main(["--cache-folder", str(root), "--apply"])
    assert rc == 0

    out = pd.read_parquet(native / "AAA_5min.parquet").sort_values("Date").reset_index(drop=True)
    assert len(out) == 3  # D1, D2, D3 (deduped)
    by_date = dict(zip(pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d"), out["Close"]))
    assert by_date["2024-01-02"] == 100   # native D1
    assert by_date["2024-01-03"] == 200   # OVERLAP -> native wins (not 999)
    assert by_date["2024-01-04"] == 300   # source-only D3


def test_delete_source_removes_legacy_after_verify(cache):
    root, legacy, native = cache
    _src_bars([10, 11]).to_parquet(legacy / "AAA_5min.parquet", index=False)

    rc = migrate.main(["--cache-folder", str(root), "--apply", "--delete-source"])
    assert rc == 0
    assert (native / "AAA_5min.parquet").exists()
    assert not legacy.exists()                      # ohlcv/fmp/ removed
    assert not (root / "ohlcv").exists()            # empty ohlcv/ root removed
