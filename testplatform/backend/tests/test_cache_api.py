"""Tests for the cache-management API (/api/cache).

Task 1 covers the usage scanner + drill-down endpoints. Deletion-endpoint
tests are added in a later task.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def seeded_cache(tmp_path, monkeypatch):
    """Point every cache root at a throwaway tree and seed a few files.

    INCIDENT (see cache_manager._is_safe_delete_root's docstring): ``monkeypatch.setenv(
    "CACHE_FOLDER", ...)`` + ``importlib.reload(cache_manager)`` alone ONLY re-binds
    ``cache_manager``'s own module-level ``CACHE_FOLDER`` (used for the "datasets"/"models"
    types). The "ohlcv"/"fmp_history"/"screener" types resolve via ``ba2_common.config.
    CACHE_FOLDER`` — a module-level constant frozen at ``ba2_common.config``'s first import
    (already imported by countless other test files by the time this fixture runs) — so the env
    var alone never reaches it, and those types silently kept pointing at the REAL cache. This
    wiped ~15,000 real files when the "clear" tests below ran. Fix: ALSO monkeypatch the
    ``ba2_common.config.CACHE_FOLDER`` ATTRIBUTE directly (not just the env var) — this is the
    contract ``ba2_providers.fmp_common._fmp_history_cache_dir`` already documents ("tests that
    rebind CACHE_FOLDER win") and ``cache_manager``'s per-call ``from ba2_common.config import
    CACHE_FOLDER`` re-imports already honour once the attribute is actually rebound. A hard
    safety guard (_is_safe_delete_root) now ALSO refuses any real-path delete under pytest
    regardless of this fixture's correctness, as defense in depth.
    """
    monkeypatch.setenv("CACHE_FOLDER", str(tmp_path / "cache"))
    import ba2_common.config as _ba2_cfg
    monkeypatch.setattr(_ba2_cfg, "CACHE_FOLDER", str(tmp_path / "cache"))

    # Seed ohlcv (provider subfolder + SYMBOL_interval file) BEFORE the reload below:
    # cache_manager._ohlcv_roots() takes a ONE-TIME snapshot (base.iterdir(), matched into the
    # module-level _OHLCV_ROOTS constant) at RELOAD time — a provider dir created AFTER the
    # reload is never picked up, silently leaving "ohlcv" empty (not wrong-pathed, just blind).
    ohlcv = tmp_path / "cache" / "FMPOHLCVProvider"
    ohlcv.mkdir(parents=True)
    (ohlcv / "AAPL_1d.csv").write_text("Date,Open\n2020-01-01,1\n")

    from app.services import cache_manager
    importlib.reload(cache_manager)  # re-read CACHE_FOLDER + rebuild CACHE_TYPES (scans the seed above)

    # seed datasets (destructive type) — override its root onto the temp tree
    cache_manager.CACHE_TYPES["datasets"]["roots"] = [tmp_path / "datasets"]
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "ds1.csv").write_text("x\n1\n")

    # seed models (destructive type) — override its root onto the temp tree so
    # clean-all's "leaves trained_models intact" guarantee is asserted on real
    # files, not just an empty backend trained_models/ dir.
    cache_manager.CACHE_TYPES["models"]["roots"] = [tmp_path / "trained_models"]
    (tmp_path / "trained_models" / "job123").mkdir(parents=True)
    (tmp_path / "trained_models" / "job123" / "model.pt").write_text("weights")

    # "jobs"/"news"/"exports" all resolve via app.paths.* constants — frozen at app.paths' own
    # first import, the SAME isolation gap as CACHE_FOLDER/ba2_common.config. Override every one
    # onto the temp tree, else clean_all would try to clear the REAL test-platform caches (now
    # refused outright by the hard safety guard in cache_manager._is_safe_delete_root rather than
    # silently deleted, but each still needs a proper isolated root for clean_all to exercise
    # correctly end-to-end).
    for _type, _dirname in (("jobs", "jobs"), ("news", "news"), ("exports", "exports")):
        d = tmp_path / _dirname
        d.mkdir()
        cache_manager.CACHE_TYPES[_type]["roots"] = [d]

    return cache_manager


def test_usage_reports_per_type(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/cache/usage")
    assert r.status_code == 200
    types = r.json()["types"]
    assert types["ohlcv"]["files"] >= 1
    assert types["ohlcv"]["bytes"] > 0
    assert types["datasets"]["destructive"] is True
    # all expected types present
    for name in ("ohlcv", "jobs", "news", "datasets", "models", "exports", "asof"):
        assert name in types


def test_usage_reports_mtime_and_ttl(seeded_cache):
    from app.main import app
    client = TestClient(app)
    types = client.get("/api/cache/usage").json()["types"]
    # ohlcv has a 24h TTL and a populated newest mtime
    assert types["ohlcv"]["ttl_hours"] == 24
    assert types["ohlcv"]["newest"] is not None
    # non-destructive flag on ohlcv
    assert types["ohlcv"]["destructive"] is False


def test_drill_down_ohlcv(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/cache/usage/ohlcv")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(
        it["symbol"] == "AAPL" and it["interval"] == "1d" and it["provider"] == "FMPOHLCVProvider"
        for it in items
    )


def test_drill_down_datasets_flat_listing(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/cache/usage/datasets")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["name"] == "ds1.csv" and it["bytes"] > 0 for it in items)


def test_drill_down_unknown_type_404(seeded_cache):
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/cache/usage/bogus").status_code == 404


# --------------------------------------------------------------------------
# Deletion endpoints (Task 2): clean-all / by-type / by-date, .tmp-aware,
# destructive guard excluding datasets + trained_models from clean-all.
# --------------------------------------------------------------------------


def test_clear_by_type_removes_only_that_type(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.delete("/api/cache/ohlcv")
    assert r.status_code == 200
    assert r.json()["files_removed"] >= 1
    # ohlcv now empty, datasets untouched
    usage = client.get("/api/cache/usage").json()["types"]
    assert usage["ohlcv"]["files"] == 0
    assert usage["datasets"]["files"] >= 1


def test_clean_all_skips_destructive(seeded_cache, tmp_path):
    """clean-all clears non-destructive types but leaves dataset CSVs +
    trained_models INTACT on disk; both clear only via an explicit type delete."""
    from app.main import app
    client = TestClient(app)
    r = client.delete("/api/cache")
    assert r.status_code == 200
    body = r.json()
    assert "skipped" in body["datasets"]
    assert "skipped" in body["models"]
    # ohlcv (non-destructive) was cleaned by clean-all
    assert body["ohlcv"]["files_removed"] >= 1
    # datasets + trained_models files survive clean-all (response AND on disk)
    usage = client.get("/api/cache/usage").json()["types"]
    assert usage["datasets"]["files"] >= 1
    assert usage["models"]["files"] >= 1
    assert (tmp_path / "datasets" / "ds1.csv").exists()
    assert (tmp_path / "trained_models" / "job123" / "model.pt").exists()
    # explicit datasets delete IS allowed and removes it
    r2 = client.delete("/api/cache/datasets")
    assert r2.status_code == 200
    assert r2.json()["files_removed"] >= 1
    usage2 = client.get("/api/cache/usage").json()["types"]
    assert usage2["datasets"]["files"] == 0
    # ...and trained_models is still untouched (only its own explicit delete clears it)
    assert (tmp_path / "trained_models" / "job123" / "model.pt").exists()
    # explicit models delete IS allowed and removes it
    r3 = client.delete("/api/cache/models")
    assert r3.status_code == 200
    assert r3.json()["files_removed"] >= 1
    assert not (tmp_path / "trained_models" / "job123" / "model.pt").exists()


def test_clear_by_date_only_old_files(seeded_cache, tmp_path):
    import os
    import time
    from app.main import app
    # backdate the seeded ohlcv file 100 days; add a fresh one that must survive
    provider_dir = tmp_path / "cache" / "FMPOHLCVProvider"
    (provider_dir / "MSFT_1d.csv").write_text("Date,Open\n2020-01-01,2\n")
    old_file = provider_dir / "AAPL_1d.csv"
    old = time.time() - 100 * 86400
    os.utime(old_file, (old, old))
    client = TestClient(app)
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10 * 86400))
    r = client.delete(f"/api/cache/ohlcv?before={cutoff}")
    assert r.status_code == 200
    assert r.json()["files_removed"] == 1
    # the fresh MSFT file survives
    assert (provider_dir / "MSFT_1d.csv").exists()
    assert not old_file.exists()


def test_clear_by_date_rejects_bad_format(seeded_cache):
    from app.main import app
    client = TestClient(app)
    assert client.delete("/api/cache/ohlcv?before=13-06-2026").status_code == 400


def test_clear_unknown_type_404(seeded_cache):
    from app.main import app
    client = TestClient(app)
    assert client.delete("/api/cache/bogus").status_code == 404


def test_clear_skips_tmp_staging_files(seeded_cache, tmp_path):
    """A concurrent atomic-write .tmp staging file must never be deleted."""
    from app.main import app
    provider_dir = tmp_path / "cache" / "FMPOHLCVProvider"
    tmp_file = provider_dir / "AAPL_1d.csv.tmp"
    tmp_file.write_text("partial")
    client = TestClient(app)
    r = client.delete("/api/cache/ohlcv")
    assert r.status_code == 200
    # the real csv is gone, the .tmp staging file survives
    assert not (provider_dir / "AAPL_1d.csv").exists()
    assert tmp_file.exists()


def test_clear_ohlcv_symbol_filter(seeded_cache, tmp_path):
    """symbol/interval filter removes only the matching OHLCV file."""
    from app.main import app
    provider_dir = tmp_path / "cache" / "FMPOHLCVProvider"
    (provider_dir / "MSFT_1d.csv").write_text("Date,Open\n2020-01-01,2\n")
    client = TestClient(app)
    r = client.delete("/api/cache/ohlcv?symbol=AAPL&interval=1d")
    assert r.status_code == 200
    assert r.json()["files_removed"] == 1
    assert not (provider_dir / "AAPL_1d.csv").exists()
    assert (provider_dir / "MSFT_1d.csv").exists()
