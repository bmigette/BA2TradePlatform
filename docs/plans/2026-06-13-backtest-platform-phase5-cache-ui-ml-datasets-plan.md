# Backtest Platform — Phase 5 (Cache-Management UI + Re-source ML Datasets Through Providers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two deliverables on `BA2TestPlatform` (design §6 Phase 5, §3): (1) a **cache-management UI** — disk usage per cache type with clean-all / clean-by-type / clean-by-date over the native provider cache and all sibling caches (OHLCV, jobs, news, dataset CSVs, trained_models, news exports, and the new `ba2_providers` as_of cache); and (2) **re-source the ML dataset builder through `ba2_providers`' as_of cache** so two consumers share one cache — experts read point-in-time slices (Phases 1–4), ML training reads a materialized feature/target matrix — while the existing `backtesting.py` `MLStrategy` single-asset path keeps working unchanged as the "ML expert" engine.

**Architecture:** Single re-source **seam** at `app/api/datasets.py::get_ohlcv_provider()` (and, behind a config flag, the sentiment/fundamentals/macro service factories): replace direct provider instantiation with a thin **adapter** to `ba2_providers`' uniform `get(symbol, as_of=..., lookback=...)` API that returns the SAME contract the builder already consumes (objects exposing `.timestamp/.open/.high/.low/.close/.volume`, i.e. today's `MarketDataPoint`). The DataFrame construction (`datasets.py:241-244`), `calculate_warmup_period`/warmup-row filtering, `use_cache`, `interval`, targets (`PredictionTargetService`), and normalization (`DataPreparationService`) all stay UNCHANGED. Migration is gated behind `OHLCV_SOURCE=ba2_providers|legacy` (default `legacy`) with **byte-equality verification** before cutover. The cache UI is a brand-new `/api/cache` router (none exists today — `providers.router` is commented out in `app/main.py:352`) reusing the proven disk-scan + clear-cache APIs.

**Tech Stack:** Python ≥3.11 (`BA2TestPlatform/backend/venv`), FastAPI, SQLAlchemy (plain declarative `Base`, NOT SQLModel), pandas, `ba2_providers` (installed via Phase-0 `install.sh --editable`), React + Vite + recharts (`BA2TestPlatform/frontend`). Provider `as_of` cache + `provider_cache` SQLite table land in **Phase 2** — Phase 5 consumes them.

---

## Source of truth & repo locations

- Target tree (this phase edits it): `BA2TestPlatform/backend/` + `BA2TestPlatform/frontend/`. Run all Python via `BA2TestPlatform/backend/venv/bin/python` (per `backend/CLAUDE.md`).
- Provider packages (consumed, not edited here): `ba2_providers` (`BA2TradeProviders`), `ba2_common` (`BA2TradeCommon`) — installed editable.
- This plan is derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§3 "ML datasets remain first-class", §6 Phase 5) and the SHARED CONTRACTS `ml_datasets.resourcing_through_providers` + `ml_datasets.cache_ui_scope`, plus a file-by-file recon of `BA2TestPlatform/backend`.

## Recon facts (verified in the `BA2TestPlatform` tree — use these exact names)

- **The ML dataset builder** is `app/api/datasets.py::_build_dataset_in_background(dataset_id, dataset_config)` (`datasets.py:174`) + its sibling `_regenerate_dataset_in_background` (`datasets.py:385`). Both fetch OHLCV via `get_ohlcv_provider(provider_name)` (`datasets.py:36`) → `provider.get_data(symbol, start_date, end_date, interval)` → build a DataFrame from `dp.timestamp/.open/.high/.low/.close/.volume` (`datasets.py:226-244`, `429-449`, `485-498`).
- **OHLCV provider factory:** `get_ohlcv_provider` maps `yfinance|yf|fmp` → `YFinanceDataProvider`/`FMPOHLCVProvider` (`datasets.py:38-44`). The `get_data` contract returns `List[MarketDataPoint]` (`dataproviders/interfaces/MarketDataProviderInterface.py:337`, `MarketDataPoint` defined in `dataproviders/base.py:18`).
- **Warmup:** `calculate_warmup_period(indicators, timeframe) -> (warmup_bars, warmup_days)` (`dataset_handler.py:57`); the builder fetches `fetch_start_date = start_date - timedelta(days=warmup_days)` then filters warmup rows back out (`datasets.py:218`, `345-355`). Source-agnostic — keep as-is.
- **News cache** (the proven pattern to generalize): `NewsCacheService` (`app/services/news_cache.py:23`) with `clear_cache(provider=None, ticker=None) -> int` (`news_cache.py:585`) and `get_cache_stats() -> Dict` (`news_cache.py:550`); `NewsCache` SQLAlchemy model (`app/models/news_cache.py`); content files under `datasets/cache/news` (`NewsCacheService.__init__` default `cache_dir`).
- **OHLCV cache clear:** `MarketDataProviderInterface.clear_cache(symbol=None, interval=None)` (`dataproviders/interfaces/MarketDataProviderInterface.py:566`); `CACHE_FOLDER = os.getenv("CACHE_FOLDER", <backend>/cache)` (`MarketDataProviderInterface.py:23`); per-provider subfolder `os.path.join(CACHE_FOLDER, self.__class__.__name__)` (`:50`); 24h TTL (`base.py:80`), atomic temp+rename + per-file `threading.Lock` (`MarketDataProviderInterface._get_cache_lock` `:55`).
- **Job/model cleanup helpers to reuse:** `cleanup_generation_models(task_id, generation)` (`job_handler.py:625`), `cleanup_non_elite_models(...)` (`:654`), `cleanup_job_models(task_id, keep_best=True)` (`:789`). Per-job model cache lives under `datasets/cache/jobs/{task_id}`.
- **News exports** dir `NEWS_EXPORTS_DIR = Path("news_exports")` (`app/api/tools.py:244`); the disk-size pattern `sum(f.stat().st_size for f in dir.rglob("*") if f.is_file())` is used at `tools.py:956`.
- **CWD path inconsistency (must normalize in the scanner):** `dataproviders/base.py:78` uses `Path("backend/datasets/cache")` while `datasets.py:738,1897` use `Path("datasets")` and `MarketDataProviderInterface.py:23` uses an absolute `CACHE_FOLDER`. The usage scanner resolves all of these against one config-driven root.
- **Handler registration / routers:** `app/main.py:255-257` registers `dataset_regeneration`/`training_job`/`backtest` handlers; routers included `app/main.py:332-345`; `providers.router` is commented out at `:352`. Frontend calls the API directly at `http://localhost:8000` (e.g. `Tools.tsx:151`) — there is no central API-base module.
- **Frontend conventions:** `frontend/src/pages/Tools.tsx:32` is a tabbed page (`news|fundamentals|macro|maintenance`) with a `MaintenancePanel` (`Tools.tsx:109`) — the cache UI mirrors this. `Backtesting.tsx` uses the camelCase results contract (`results.equityCurve`/`drawdownCurve`/`trades`, `Backtesting.tsx:126-127,1259-1296`) and metric cards (`:1165-1181`).

## Decisions taken (confirm before execution)

These resolve forks the recon surfaced. Override any at approval time.

1. **Two independent deliverables, two task clusters.** Tasks 1–4 = cache UI (backend `/api/cache` + scanner + frontend page); Tasks 5–8 = re-source ML datasets. They share only the cache they operate over and can be executed/merged independently. The GATE requires both green.
2. **Re-source is gated + reversible (`OHLCV_SOURCE` flag).** Default `legacy` (today's `YFinance/FMP` direct path). `ba2_providers` is selected only after byte-equality verification (Task 7). The legacy `dataproviders/base.py` path stays as fallback. *Alternative rejected:* hard cutover (no rollback if the provider cache diverges).
3. **Cache UI is read-then-act, with destructive types behind explicit selection.** "Clean All" excludes dataset CSVs and `trained_models` (destructive, irreplaceable) — those clear only via an explicit `type` request (contract `ml_datasets.cache_ui_scope.cross_cutting`). 24h-TTL OHLCV caches are purgeable anytime.
4. **OHLCV re-source first; sentiment/fundamentals/macro behind the same flag, last.** The contract names OHLCV as the primary seam (`get_ohlcv_provider`) and the service factories as secondary. v1 cutover targets OHLCV only (the byte-equality gate is tractable there); the service factories get the adapter shape but stay `legacy` until separately verified (Task 8 is documented as deferred-verification).
5. **`ba2_providers` adapter returns `MarketDataPoint`, not a new type.** The adapter wraps `ba2_providers.get(...)` rows into the existing `dataproviders.base.MarketDataPoint` so `_build_dataset_in_background`'s DataFrame line is untouched. No change to `MarketDataProviderInterface.get_data` callers.
6. **Cache UI surfaces the as_of cache as its own type.** Per contract, the new `ba2_providers` as_of cache (parquet time-series + `provider_cache` SQLite, both under `<CACHE_FOLDER>/datasets/cache`, defined in Phase 2) is a first-class type alongside `ohlcv|jobs|news|datasets|models|exports`.

## Phase dependencies (what Phase 5 consumes)

> **Re-plan checkpoint (Phase 2 outputs):** Phase 5's re-source adapter (Task 5) and the as_of cache UI type (Task 2) both depend on Phase 2 having shipped: (a) the uniform `ba2_providers` `get(symbol, as_of=None, lookback=..., format_type=...)` wrapper / `CachedProviderMixin`; (b) the native cache stores — parquet time-series at `<CACHE_FOLDER>/<Provider>/<SYMBOL>_<interval>.parquet` and the `provider_cache` SQLite index table under `<CACHE_FOLDER>/datasets/cache/`. **At execution time, confirm the exact `get()` signature, the OHLCV row object's attribute names, and the on-disk paths from the merged Phase-2 code before wiring the adapter and the scanner.** If Phase 2 has not landed, build the cache UI for the EXISTING cache types only (ohlcv/jobs/news/datasets/models/exports) and stub the as_of type behind a feature check, and hold Task 5 until the provider `get()` exists.

Per the contract `phase_dependencies`: Phase 5 also consumes Phase 4's deterministic backtest run (the ML engine stays `backtesting.py MLStrategy`) but does NOT modify it here.

## Acceptance gate for Phase 5 (verified)

1. **Dataset build via providers reproduces the prior matrix.** With `OHLCV_SOURCE=ba2_providers`, building a dataset for a fixed `(ticker, timeframe, start, end)` produces a CSV byte-equal to the `legacy` build (or a documented, justified equivalence delta) — verified by `tests/test_resource_ml_datasets.py::test_byte_equal_csv`.
2. **Cache-UI operations work on a seeded cache.** `GET /api/cache/usage` reports per-type bytes/count/mtime on a seeded cache; `DELETE /api/cache/{type}` and `DELETE /api/cache/{type}?before=YYYY-MM-DD` remove exactly the targeted entries; "Clean All" leaves dataset CSVs + `trained_models` intact — verified by `tests/test_cache_api.py`.
3. **ML training still runs.** A dataset built through the provider path trains end-to-end (`backtesting.py MLStrategy` / `job_handler` training job) with no schema/contract change — verified by the existing dataset-generation + a smoke training run.
4. **No regression on the live live-trading platform.** `BA2TradePlatform/ba2_trade_platform/` is byte-for-byte unchanged (`git -C BA2TradePlatform status` clean except plan docs).

---

## Task 1: Cache-usage scanner + `/api/cache` router (usage endpoint)

**Files (create/edit):**
- Create `BA2TestPlatform/backend/app/services/cache_manager.py` (the scanner — single source of truth for cache roots, per-type usage, drill-down)
- Create `BA2TestPlatform/backend/app/api/cache.py` (the `/api/cache` router)
- Edit `BA2TestPlatform/backend/app/main.py` (include the new router)
- Test: `BA2TestPlatform/backend/tests/test_cache_api.py`

- [ ] **Step 1: Define the cache-type registry + root resolution in `cache_manager.py`**

Create `BA2TestPlatform/backend/app/services/cache_manager.py`. This module is the ONE place that knows where each cache lives, normalizing the CWD path inconsistency (`base.py:78` `Path("backend/datasets/cache")` vs `datasets.py:738` `Path("datasets")` vs `MarketDataProviderInterface.py:23` absolute `CACHE_FOLDER`).

```python
"""Cache discovery + usage scanner for the cache-management UI.

Single source of truth for every cache root. Normalizes the CWD path
inconsistency across the codebase (base.py Path('backend/datasets/cache'),
datasets.py Path('datasets'), MarketDataProviderInterface CACHE_FOLDER) by
resolving every root against the backend dir, not the process CWD."""
from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

# backend/ root = parents[2] from app/services/cache_manager.py
BACKEND_DIR = Path(__file__).resolve().parents[2]

# CACHE_FOLDER as the provider layer sees it (MarketDataProviderInterface.py:23).
CACHE_FOLDER = Path(os.getenv("CACHE_FOLDER", str(BACKEND_DIR / "cache")))

def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (BACKEND_DIR / p)

# Cache-type -> on-disk root(s). Mirrors ml_datasets.cache_ui_scope.types_tracked.
# DESTRUCTIVE types (datasets, models) are excluded from "clean all" (see api/cache.py).
CACHE_TYPES: Dict[str, Dict[str, Any]] = {
    "ohlcv":    {"roots": [CACHE_FOLDER],                          "destructive": False, "ttl_hours": 24},
    "jobs":     {"roots": [_resolve("datasets/cache/jobs")],      "destructive": False, "ttl_hours": None},
    "news":     {"roots": [_resolve("datasets/cache/news")],      "destructive": False, "ttl_hours": None, "db_backed": True},
    "datasets": {"roots": [_resolve("datasets")],                 "destructive": True,  "ttl_hours": None},
    "models":   {"roots": [_resolve("trained_models")],           "destructive": True,  "ttl_hours": None},
    "exports":  {"roots": [_resolve("news_exports")],             "destructive": False, "ttl_hours": None},
    # NEW (Phase 2) as_of provider cache: parquet time-series + provider_cache SQLite.
    # > Re-plan checkpoint: confirm this path from the merged Phase-2 code before relying on it.
    "asof":     {"roots": [CACHE_FOLDER / "datasets" / "cache"],  "destructive": False, "ttl_hours": None},
}

def _scan_dir(root: Path) -> Dict[str, Any]:
    """Return total bytes, file count, oldest/newest mtime for a directory tree."""
    total = 0; count = 0; oldest: Optional[float] = None; newest: Optional[float] = None
    if not root.exists():
        return {"bytes": 0, "files": 0, "oldest": None, "newest": None, "exists": False}
    for f in root.rglob("*"):
        if f.is_file():
            try:
                st = f.stat()
            except OSError:
                continue
            total += st.st_size; count += 1
            m = st.st_mtime
            oldest = m if oldest is None else min(oldest, m)
            newest = m if newest is None else max(newest, m)
    return {
        "bytes": total, "files": count, "exists": True,
        "oldest": datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat() if oldest else None,
        "newest": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None,
    }

def get_usage() -> Dict[str, Any]:
    """Per-type disk usage for every tracked cache. News also reports DB row counts."""
    out: Dict[str, Any] = {}
    for name, cfg in CACHE_TYPES.items():
        agg = {"bytes": 0, "files": 0, "oldest": None, "newest": None, "exists": False,
               "destructive": cfg["destructive"], "ttl_hours": cfg["ttl_hours"]}
        for root in cfg["roots"]:
            s = _scan_dir(root)
            agg["bytes"] += s["bytes"]; agg["files"] += s["files"]
            agg["exists"] = agg["exists"] or s["exists"]
            for k in ("oldest", "newest"):
                if s[k] and (agg[k] is None or
                             (k == "oldest" and s[k] < agg[k]) or
                             (k == "newest" and s[k] > agg[k])):
                    agg[k] = s[k]
        if cfg.get("db_backed") and name == "news":
            try:
                from app.services.news_cache import NewsCacheService
                agg["db_stats"] = NewsCacheService().get_cache_stats()  # news_cache.py:550
            except Exception:
                agg["db_stats"] = None
        out[name] = agg
    return out
```

> No placeholders: every root maps to a real on-disk location verified in recon. `parents[2]` is exact (`app/services/cache_manager.py` → `app` → `backend`).

- [ ] **Step 2: Add per-type drill-down to `cache_manager.py`**

Append drill-down (per-ticker/interval for OHLCV via filename parse; per-provider/ticker for news via DB; per-task_id for jobs/models):

```python
def drill_down(cache_type: str) -> List[Dict[str, Any]]:
    """Per-item breakdown for one cache type (UI drill-down)."""
    cfg = CACHE_TYPES.get(cache_type)
    if not cfg:
        raise KeyError(cache_type)
    items: List[Dict[str, Any]] = []
    if cache_type == "ohlcv":
        # provider subfolders contain <SYMBOL>_<interval>.{csv,parquet} (base.py:129, MDPI:50)
        for root in cfg["roots"]:
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file() and f.suffix in (".csv", ".parquet"):
                    stem = f.stem  # SYMBOL_interval
                    sym, _, interval = stem.rpartition("_")
                    st = f.stat()
                    items.append({"provider": f.parent.name, "symbol": sym or stem,
                                  "interval": interval, "bytes": st.st_size,
                                  "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                                  "stale": (datetime.now().timestamp() - st.st_mtime) > 24*3600})
    elif cache_type == "news":
        from app.services.news_cache import NewsCacheService
        stats = NewsCacheService().get_cache_stats()  # by_provider counts
        for prov, n in (stats.get("by_provider") or {}).items():
            items.append({"provider": prov, "articles": n})
    elif cache_type in ("jobs", "models"):
        for root in cfg["roots"]:
            if not root.exists():
                continue
            for d in root.iterdir():
                if d.is_dir():
                    size = sum(x.stat().st_size for x in d.rglob("*") if x.is_file())
                    items.append({"task_id": d.name, "bytes": size})
    else:  # datasets, exports, asof — flat file listing
        for root in cfg["roots"]:
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file():
                    st = f.stat()
                    items.append({"name": str(f.relative_to(root)), "bytes": st.st_size,
                                  "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()})
    return items
```

- [ ] **Step 3: Create the `/api/cache` router with the usage endpoint**

Create `BA2TestPlatform/backend/app/api/cache.py`:

```python
"""Cache-management API. Brand-new router (no /api/cache exists today;
providers.router is commented out in main.py:352)."""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import logging

from app.services import cache_manager

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/usage")
async def cache_usage():
    """Per-type disk usage (bytes, file count, oldest/newest mtime, destructive flag, TTL)."""
    return {"types": cache_manager.get_usage()}

@router.get("/usage/{cache_type}")
async def cache_drill_down(cache_type: str):
    """Per-item breakdown for one cache type."""
    try:
        return {"type": cache_type, "items": cache_manager.drill_down(cache_type)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cache type: {cache_type}")
```

- [ ] **Step 4: Register the router in `main.py`**

In `BA2TestPlatform/backend/app/main.py`, add the import alongside the other router imports (near `:329`) and include it next to `tools.router` (`:333`):

```python
from app.api import cache as cache_api  # add to the router-import block
app.include_router(cache_api.router, prefix="/api/cache", tags=["cache"])
```

- [ ] **Step 5: Write the usage test**

`BA2TestPlatform/backend/tests/test_cache_api.py`:

```python
import os, json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def seeded_cache(tmp_path, monkeypatch):
    """Point every cache root at a throwaway tree and seed a few files."""
    monkeypatch.setenv("CACHE_FOLDER", str(tmp_path / "cache"))
    import importlib
    from app.services import cache_manager
    importlib.reload(cache_manager)             # re-read CACHE_FOLDER + roots
    # seed ohlcv (provider subfolder + SYMBOL_interval file)
    ohlcv = tmp_path / "cache" / "FMPOHLCVProvider"; ohlcv.mkdir(parents=True)
    (ohlcv / "AAPL_1d.csv").write_text("Date,Open\n2020-01-01,1\n")
    # seed datasets (destructive type)
    cache_manager.CACHE_TYPES["datasets"]["roots"] = [tmp_path / "datasets"]
    (tmp_path / "datasets").mkdir(); (tmp_path / "datasets" / "ds1.csv").write_text("x\n1\n")
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

def test_drill_down_ohlcv(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/cache/usage/ohlcv")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["symbol"] == "AAPL" and it["interval"] == "1d" for it in items)

def test_drill_down_unknown_type_404(seeded_cache):
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/cache/usage/bogus").status_code == 404
```

- [ ] **Step 6: Run the usage tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && \
  venv/bin/python -m pytest tests/test_cache_api.py -k usage -v
```
Expected: the usage + drill-down tests PASS (deletion tests added in Task 2 will be skipped/absent for now).

---

## Task 2: Cache deletion endpoints (clean-all / by-type / by-date) — thread-safe, lock-aware

**Files (edit):**
- Edit `BA2TestPlatform/backend/app/services/cache_manager.py` (add deletion helpers honoring per-file locks + `.tmp` atomic-write awareness + the destructive guard)
- Edit `BA2TestPlatform/backend/app/api/cache.py` (DELETE endpoints)
- Test: extend `BA2TestPlatform/backend/tests/test_cache_api.py`

- [ ] **Step 1: Add lock-aware deletion to `cache_manager.py`**

Reuse the existing clear APIs where they exist (`NewsCacheService.clear_cache` for news rows, `MarketDataProviderInterface.clear_cache` for OHLCV) and a direct-but-careful file delete otherwise. Append:

```python
import time

def _safe_unlink(f: Path) -> int:
    """Delete one file, skipping .tmp atomic-write staging files. Returns bytes freed."""
    if f.suffix == ".tmp" or f.name.endswith(".tmp"):
        return 0
    try:
        size = f.stat().st_size
        f.unlink()
        return size
    except OSError:
        return 0

def _delete_tree(root: Path, before: Optional[datetime] = None) -> Dict[str, int]:
    freed = 0; removed = 0
    if not root.exists():
        return {"bytes_freed": 0, "files_removed": 0}
    for f in list(root.rglob("*")):
        if not f.is_file():
            continue
        if before is not None:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime >= before:
                continue
        b = _safe_unlink(f)
        if b or not f.exists():
            freed += b; removed += 1
    return {"bytes_freed": freed, "files_removed": removed}

def clear_type(cache_type: str, before: Optional[datetime] = None,
               symbol: Optional[str] = None, interval: Optional[str] = None,
               provider: Optional[str] = None, ticker: Optional[str] = None,
               task_id: Optional[str] = None) -> Dict[str, Any]:
    """Clear one cache type, optionally filtered. Routes through the type's native
    clear API when one exists (news rows, OHLCV provider cache), else lock-aware
    file deletion. Honors .tmp atomic-write staging files (never deleted)."""
    cfg = CACHE_TYPES.get(cache_type)
    if not cfg:
        raise KeyError(cache_type)
    if cache_type == "news":
        from app.services.news_cache import NewsCacheService
        deleted = NewsCacheService().clear_cache(provider=provider, ticker=ticker)  # DB rows
        # also clear orphaned content files (before-date aware)
        file_res = {"bytes_freed": 0, "files_removed": 0}
        for root in cfg["roots"]:
            file_res = _delete_tree(root, before)
        return {"db_rows_deleted": deleted, **file_res}
    result = {"bytes_freed": 0, "files_removed": 0}
    for root in cfg["roots"]:
        target = root
        if cache_type in ("jobs", "models") and task_id:
            target = root / task_id
        r = _delete_tree(target, before)
        result["bytes_freed"] += r["bytes_freed"]; result["files_removed"] += r["files_removed"]
    return result

def clear_all(before: Optional[datetime] = None) -> Dict[str, Any]:
    """Clean every NON-destructive cache type. Excludes datasets + trained_models
    (cross_cutting rule: only an explicit type request may delete those)."""
    out: Dict[str, Any] = {}
    for name, cfg in CACHE_TYPES.items():
        if cfg["destructive"]:
            out[name] = {"skipped": "destructive — clear explicitly by type"}
            continue
        out[name] = clear_type(name, before=before)
    return out
```

> The `_get_cache_lock` registry (`MarketDataProviderInterface.py:55`) protects writers; deletion uses `_safe_unlink` + `.tmp` skipping so a concurrent atomic write (`temp + rename`) is never half-deleted. For OHLCV the contract allows the native `MarketDataProviderInterface.clear_cache(symbol, interval)` — prefer it when a `symbol`/`interval` filter is given; the file-tree path covers the no-filter "purge all OHLCV" case.

- [ ] **Step 2: Add the DELETE endpoints to `api/cache.py`**

```python
from datetime import datetime, timezone

def _parse_before(before: Optional[str]) -> Optional[datetime]:
    if not before:
        return None
    try:
        return datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="before must be YYYY-MM-DD")

@router.delete("")
async def clear_all_caches(before: Optional[str] = Query(None, description="Only delete entries older than YYYY-MM-DD")):
    """Clean all NON-destructive cache types. datasets + trained_models are excluded."""
    return cache_manager.clear_all(before=_parse_before(before))

@router.delete("/{cache_type}")
async def clear_cache_type(
    cache_type: str,
    before: Optional[str] = Query(None, description="Only delete entries older than YYYY-MM-DD"),
    symbol: Optional[str] = Query(None), interval: Optional[str] = Query(None),
    provider: Optional[str] = Query(None), ticker: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
):
    """Clean one cache type (incl. the destructive datasets/models when named explicitly),
    optionally filtered by date and granular keys."""
    try:
        return cache_manager.clear_type(
            cache_type, before=_parse_before(before),
            symbol=symbol, interval=interval, provider=provider, ticker=ticker, task_id=task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cache type: {cache_type}")
```

- [ ] **Step 3: Extend the test with deletion cases**

Append to `BA2TestPlatform/backend/tests/test_cache_api.py`:

```python
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

def test_clean_all_skips_destructive(seeded_cache):
    from app.main import app
    client = TestClient(app)
    r = client.delete("/api/cache")
    body = r.json()
    assert "skipped" in body["datasets"]
    assert "skipped" in body["models"]
    # explicit datasets delete IS allowed
    r2 = client.delete("/api/cache/datasets")
    assert r2.status_code == 200

def test_clear_by_date_only_old_files(seeded_cache, tmp_path):
    import os, time
    from app.main import app
    # backdate the ohlcv file 100 days
    ohlcv = next((tmp_path / "cache" / "FMPOHLCVProvider").glob("*.csv"))
    old = time.time() - 100*86400
    os.utime(ohlcv, (old, old))
    client = TestClient(app)
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10*86400))
    r = client.delete(f"/api/cache/ohlcv?before={cutoff}")
    assert r.json()["files_removed"] == 1

def test_clear_by_date_rejects_bad_format(seeded_cache):
    from app.main import app
    client = TestClient(app)
    assert client.delete("/api/cache/ohlcv?before=13-06-2026").status_code == 400
```

- [ ] **Step 4: Run the full cache-API test**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && \
  venv/bin/python -m pytest tests/test_cache_api.py -v
```
Expected: all usage + deletion tests PASS (this satisfies GATE item 2).

---

## Task 3: Cache-management frontend page

**Files (create/edit):**
- Create `BA2TestPlatform/frontend/src/pages/CacheManagement.tsx`
- Edit the frontend router + nav (`frontend/src/App.tsx` or the route table — confirm the actual router file)
- Reuse the `Tools.tsx` tabbed/card visual idiom and direct `http://localhost:8000` fetch convention.

- [ ] **Step 1: Confirm the router + nav wiring file**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/frontend
grep -rn "Route\|Routes\|path=\|Tools\b" src/App.tsx src/main.tsx src/components/ 2>/dev/null | head -20
```
> Re-plan checkpoint: use whatever router file the grep reveals (React Router `<Route path=...>` table). Add a `/cache` route + a sidebar/nav entry "Cache" next to the existing "Tools" entry, mirroring how `Tools` is registered.

- [ ] **Step 2: Build the page (cards per type + actions)**

Create `BA2TestPlatform/frontend/src/pages/CacheManagement.tsx`. One card per cache type with human-readable size, item count, last-modified; buttons Clean All / Clean by Type / Clean by Date (date picker) with confirm + post-action usage refresh:

```tsx
import React, { useState, useEffect } from 'react';

interface CacheTypeUsage {
  bytes: number; files: number; oldest: string | null; newest: string | null;
  exists: boolean; destructive: boolean; ttl_hours: number | null;
  db_stats?: { total_articles: number } | null;
}
type Usage = Record<string, CacheTypeUsage>;

const API = 'http://localhost:8000/api/cache';
const fmtBytes = (b: number) => {
  if (b < 1024) return `${b} B`;
  const u = ['KB', 'MB', 'GB', 'TB']; let i = -1; let n = b;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(1)} ${u[i]}`;
};

const CacheManagement: React.FC = () => {
  const [usage, setUsage] = useState<Usage>({});
  const [loading, setLoading] = useState(false);
  const [beforeDate, setBeforeDate] = useState('');
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true); setError(null);
    try {
      const r = await fetch(`${API}/usage`);
      if (!r.ok) throw new Error(`usage ${r.status}`);
      setUsage((await r.json()).types);
    } catch (e: any) { setError(String(e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, []);

  const cleanType = async (type: string, useDate: boolean) => {
    const t = usage[type];
    const msg = t?.destructive
      ? `"${type}" is DESTRUCTIVE (irreplaceable). Really delete it?`
      : `Clean cache "${type}"${useDate && beforeDate ? ` before ${beforeDate}` : ''}?`;
    if (!window.confirm(msg)) return;
    const qs = useDate && beforeDate ? `?before=${beforeDate}` : '';
    const r = await fetch(`${API}/${type}${qs}`, { method: 'DELETE' });
    if (!r.ok) { setError(`delete ${type} -> ${r.status}`); return; }
    await refresh();
  };

  const cleanAll = async () => {
    if (!window.confirm('Clean ALL non-destructive caches? (datasets + trained_models are kept)')) return;
    const qs = beforeDate ? `?before=${beforeDate}` : '';
    const r = await fetch(`${API}${qs}`, { method: 'DELETE' });
    if (!r.ok) { setError(`clean all -> ${r.status}`); return; }
    await refresh();
  };

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Cache Management</h1>
        <div className="flex items-center gap-2">
          <input type="date" value={beforeDate} onChange={e => setBeforeDate(e.target.value)}
                 className="border rounded px-2 py-1" title="Clean entries older than this date" />
          <button onClick={refresh} className="px-3 py-1 border rounded">Refresh</button>
          <button onClick={cleanAll} className="px-3 py-1 bg-red-600 text-white rounded">Clean All</button>
        </div>
      </div>
      {error && <div className="text-red-600">{error}</div>}
      {loading && <div>Loading…</div>}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {Object.entries(usage).map(([type, t]) => (
          <div key={type} className="border rounded-lg p-4 shadow-sm">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold capitalize">{type}</h3>
              {t.destructive && <span className="text-xs text-red-600">destructive</span>}
            </div>
            <p className="text-sm text-gray-500">Size: {fmtBytes(t.bytes)}</p>
            <p className="text-sm text-gray-500">Items: {t.files}{t.db_stats ? ` (+${t.db_stats.total_articles} rows)` : ''}</p>
            <p className="text-sm text-gray-500">Newest: {t.newest ? t.newest.slice(0, 10) : '—'}</p>
            {t.ttl_hours && <p className="text-xs text-gray-400">TTL {t.ttl_hours}h</p>}
            <div className="flex gap-2 mt-2">
              <button onClick={() => cleanType(type, false)} className="px-2 py-1 text-sm border rounded">Clean</button>
              <button onClick={() => cleanType(type, true)} className="px-2 py-1 text-sm border rounded"
                      disabled={!beforeDate}>Clean by Date</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default CacheManagement;
```

- [ ] **Step 3: Wire the route + nav**

Add `import CacheManagement from './pages/CacheManagement';` and a `<Route path="/cache" element={<CacheManagement />} />` (or the project's equivalent) to the router file found in Step 1, plus a "Cache" nav link next to "Tools".

- [ ] **Step 4: Build the frontend to confirm it compiles**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/frontend && npm run build
```
Expected: build succeeds with no TypeScript errors. (Use `recon:test-engine (UI)` to manually exercise the page against a seeded cache + running backend: load `/cache`, confirm cards render, click Clean on a non-destructive type, confirm usage refreshes.)

---

## Task 4: Cache-UI verification on a seeded cache (cache-UI half of the GATE)

- [ ] **Step 1: Seed a representative cache + run the backend**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
# seed a couple of OHLCV files + a fake job dir + a dataset CSV under a temp CACHE_FOLDER
export CACHE_FOLDER=$(mktemp -d)/cache
mkdir -p "$CACHE_FOLDER/FMPOHLCVProvider" datasets/cache/jobs/job123 datasets trained_models
printf 'Date,Open\n2020-01-01,1\n' > "$CACHE_FOLDER/FMPOHLCVProvider/AAPL_1d.csv"
printf 'x\n1\n' > datasets/seed_ds.csv
venv/bin/python -m uvicorn app.main:app --port 8000 &
sleep 4
```

- [ ] **Step 2: Exercise every endpoint and assert behavior**

```bash
echo "== usage ==" && curl -s localhost:8000/api/cache/usage | venv/bin/python -m json.tool | head -40
echo "== drill ohlcv ==" && curl -s localhost:8000/api/cache/usage/ohlcv | venv/bin/python -m json.tool
echo "== clean all (datasets must be skipped) ==" && curl -s -X DELETE localhost:8000/api/cache | venv/bin/python -m json.tool
echo "== datasets still present ==" && curl -s localhost:8000/api/cache/usage | venv/bin/python -c "import sys,json; d=json.load(sys.stdin)['types']; print('datasets files:', d['datasets']['files'])"
echo "== explicit datasets delete ==" && curl -s -X DELETE localhost:8000/api/cache/datasets | venv/bin/python -m json.tool
```
Expected: usage lists all types; `clean all` returns `skipped` for `datasets` + `models`; datasets file survives clean-all and is removed only by the explicit `DELETE /api/cache/datasets`.

- [ ] **Step 3: Stop the server**

```bash
kill %1 2>/dev/null || true
```

- [ ] **Step 4: Commit the cache-UI cluster**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/services/cache_manager.py backend/app/api/cache.py backend/app/main.py backend/tests/test_cache_api.py frontend/src/pages/CacheManagement.tsx frontend/src/App.tsx
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(cache-ui): /api/cache usage + clean-all/by-type/by-date + frontend page"
```
> Confirm the exact router-file path edited in Task 3 Step 3 and include it in the `git add` list.

---

## Task 5: `ba2_providers` OHLCV adapter behind the `OHLCV_SOURCE` seam

**Files (create/edit):**
- Create `BA2TestPlatform/backend/dataproviders/ba2providers_adapter.py` (adapter: `ba2_providers.get(...)` → `List[MarketDataPoint]`)
- Edit `BA2TestPlatform/backend/app/api/datasets.py` (`get_ohlcv_provider` chooses the adapter when `OHLCV_SOURCE=ba2_providers`)
- Test: `BA2TestPlatform/backend/tests/test_resource_ml_datasets.py`

- [ ] **Step 1: Confirm the `ba2_providers` `get()` signature + OHLCV provider key**

```bash
cd /Users/bmigette/Documents/dev/BA2
venv_test=BA2TestPlatform/backend/venv/bin/python
$venv_test -c "import ba2_providers; from ba2_providers import get_provider; print('ok', callable(get_provider))" || echo "ba2_providers NOT installed — run BA2TradeCommon/install.sh --editable into backend/venv first"
grep -rn "def get\b\|def get(\|as_of\|def _get_ohlcv_data_impl\|class .*OHLCV" BA2TradeProviders/ba2_providers/ohlcv/ 2>/dev/null | head
```
> Re-plan checkpoint (Phase-2 contract): the uniform wrapper is `get(symbol, as_of=None, lookback=..., field=None, format_type='dict', **kw)` (contract `provider_asof.uniform_contract`) with OHLCV mapping `as_of -> end_date (inclusive)`, `lookback -> lookback_days` (contract `per_category_mapping.OHLCV/MarketData`). **Confirm the exact method name, the row object's attribute names (`.timestamp/.open/.high/.low/.close/.volume` vs a dict), and the registry key (`get_provider("ohlcv", "fmp")` vs a direct class) against the merged Phase-2 code before writing the adapter.** If `ba2_providers` is not yet installed into `backend/venv`, install the chain editable (`PYTHON=BA2TestPlatform/backend/venv/bin/python bash BA2TradeCommon/install.sh --editable`) — that is a prerequisite, not part of byte-equality.

- [ ] **Step 2: Write the adapter**

Create `BA2TestPlatform/backend/dataproviders/ba2providers_adapter.py`. It exposes the SAME `get_data(symbol, start_date, end_date, interval, use_cache=True) -> List[MarketDataPoint]` shape `_build_dataset_in_background` calls, so the builder is untouched:

```python
"""Adapter: source OHLCV for the ML dataset builder THROUGH ba2_providers' as_of cache.

The dataset builder consumes provider.get_data(...) -> List[MarketDataPoint]
(datasets.py:226, base.py:18). This adapter satisfies that contract while routing
the fetch through ba2_providers' uniform get() + native as_of cache, so experts
(point-in-time slices) and ML training (materialized matrix) share ONE cache.

Selected only when OHLCV_SOURCE=ba2_providers (default legacy). as_of = end_date
gives the reproducible/leak-free snapshot known at the dataset's end (design §3)."""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from dataproviders.base import MarketDataPoint   # reuse the existing contract type
import logging

logger = logging.getLogger(__name__)


class BA2ProvidersOHLCVAdapter:
    def __init__(self, provider_name: str = "fmp"):
        self._provider_name = provider_name
        # lazy import so a legacy install without ba2_providers still loads datasets.py
        from ba2_providers import get_provider          # confirm export in Step 1
        self._provider = get_provider("ohlcv", provider_name)  # confirm key in Step 1

    def get_data(self, symbol: str, start_date: Optional[datetime] = None,
                 end_date: Optional[datetime] = None, interval: str = "1d",
                 use_cache: bool = True) -> List[MarketDataPoint]:
        # OHLCV mapping (Phase-2 contract): as_of -> end_date inclusive, lookback -> days.
        lookback_days = None
        if start_date and end_date:
            lookback_days = max((end_date - start_date).days, 1)
        # > Re-plan checkpoint: align kwarg names with the confirmed get() signature.
        rows = self._provider.get(
            symbol, as_of=end_date, lookback=lookback_days,
            interval=interval, format_type="dict",
        )
        return [self._to_point(symbol, r) for r in self._iter_rows(rows)]

    @staticmethod
    def _iter_rows(rows):
        # get(format_type="dict") may return a dict bundle or a list of bars.
        if isinstance(rows, dict):
            return rows.get("data") or rows.get("values") or rows.get("bars") or []
        return rows or []

    @staticmethod
    def _to_point(symbol: str, r) -> MarketDataPoint:
        if isinstance(r, dict):
            return MarketDataPoint(
                symbol=symbol,
                timestamp=r.get("timestamp") or r.get("Date") or r.get("date"),
                open=float(r["open"] if "open" in r else r["Open"]),
                high=float(r["high"] if "high" in r else r["High"]),
                low=float(r["low"] if "low" in r else r["Low"]),
                close=float(r["close"] if "close" in r else r["Close"]),
                volume=float(r["volume"] if "volume" in r else r["Volume"]),
            )
        # object form: already exposes .timestamp/.open/... — pass straight through
        return MarketDataPoint(symbol=symbol, timestamp=r.timestamp, open=float(r.open),
                               high=float(r.high), low=float(r.low), close=float(r.close),
                               volume=float(r.volume))
```

> Confirm `MarketDataPoint`'s constructor kwargs against `dataproviders/base.py:18-46` (recon shows `symbol, timestamp, open, high, low, close, volume`) and adjust `_to_point` if a positional/`extra` field exists.

- [ ] **Step 3: Make `get_ohlcv_provider` flag-aware in `datasets.py`**

In `BA2TestPlatform/backend/app/api/datasets.py`, replace the body of `get_ohlcv_provider` (`datasets.py:36-44`) so it consults `OHLCV_SOURCE` (env, default `legacy`):

```python
import os

def get_ohlcv_provider(provider_name: str = "yfinance"):
    """Get the OHLCV provider. OHLCV_SOURCE=ba2_providers routes through the
    ba2_providers as_of cache (shared with experts); default 'legacy' keeps the
    direct YFinance/FMP path. Verified byte-equal before cutover (Phase 5 gate)."""
    source = os.getenv("OHLCV_SOURCE", "legacy").lower()
    if source == "ba2_providers":
        try:
            from dataproviders.ba2providers_adapter import BA2ProvidersOHLCVAdapter
            return BA2ProvidersOHLCVAdapter(provider_name if provider_name.lower() == "fmp" else "fmp")
        except Exception as e:
            logger.error(f"ba2_providers OHLCV source failed ({e}); falling back to legacy", exc_info=True)
    provider_map = {
        "yfinance": YFinanceDataProvider,
        "yf": YFinanceDataProvider,
        "fmp": FMPOHLCVProvider,
    }
    provider_class = provider_map.get(provider_name.lower(), YFinanceDataProvider)
    return provider_class()
```

> Only the factory changes — `_build_dataset_in_background` / `_regenerate_dataset_in_background` keep calling `get_ohlcv_provider(...).get_data(...)` exactly as before. The 6 call sites (`datasets.py:223,424,482,1837,2075,2281`) are all routed through this one factory, so the seam is single-point.

- [ ] **Step 4: Write the adapter unit test (no network — monkeypatched provider)**

`BA2TestPlatform/backend/tests/test_resource_ml_datasets.py`:

```python
from datetime import datetime
import types

def test_adapter_maps_dict_rows_to_marketdatapoints(monkeypatch):
    import dataproviders.ba2providers_adapter as mod

    class FakeProvider:
        def get(self, symbol, as_of=None, lookback=None, interval="1d", format_type="dict"):
            return {"data": [
                {"Date": datetime(2020, 1, 2), "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5, "Volume": 100},
                {"Date": datetime(2020, 1, 3), "Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.0, "Volume": 200},
            ]}
    monkeypatch.setattr(mod, "get_provider", lambda cat, name: FakeProvider(), raising=False)
    # bypass __init__'s real import
    adapter = mod.BA2ProvidersOHLCVAdapter.__new__(mod.BA2ProvidersOHLCVAdapter)
    adapter._provider = FakeProvider()
    pts = adapter.get_data("AAPL", datetime(2020, 1, 1), datetime(2020, 1, 5), "1d")
    assert len(pts) == 2
    assert pts[0].close == 1.5 and pts[1].volume == 200
    assert pts[0].timestamp == datetime(2020, 1, 2)

def test_get_ohlcv_provider_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("OHLCV_SOURCE", raising=False)
    from app.api.datasets import get_ohlcv_provider
    from dataproviders.ohlcv.YFinanceDataProvider import YFinanceDataProvider
    assert isinstance(get_ohlcv_provider("yfinance"), YFinanceDataProvider)
```

- [ ] **Step 5: Run the adapter tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && \
  venv/bin/python -m pytest tests/test_resource_ml_datasets.py -k adapter -v
```
Expected: PASS. (The byte-equality test is added in Task 7.)

---

## Task 6: Adapter shape for sentiment / fundamentals / macro service factories (wired, default legacy)

**Files (edit):** `BA2TestPlatform/backend/app/services/{sentiment,fundamentals,macro}.py` factory/instantiation points only.

The contract names the OHLCV factory as the primary seam and the sentiment/fundamentals/macro service factories as the secondary re-source seam (`ml_datasets.resourcing_through_providers.seam`). Mirror the OHLCV pattern: gate behind `OHLCV_SOURCE` (or a parallel `FEATURES_SOURCE`) and pass `as_of=dataset.end_date` for reproducible builds, keeping the DataFrame-merge logic + `news_/bs_/is_/cf_/earn_/fundamental_/macro_` column prefixes UNCHANGED (`invariants_preserved`).

- [ ] **Step 1: Locate the provider-instantiation points in each service**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
grep -n "Provider(\|get_provider\|provider =\|def fetch_news_for_ticker\|def create_statement_features\|def integrate_macro" app/services/sentiment.py app/services/fundamentals.py app/services/macro.py | head -30
```
> Re-plan checkpoint: these services fetch via per-service clients today. Identify the single factory/instantiation line per service that the adapter replaces. The dataset builder calls `SentimentService().fetch_news_for_ticker(...)` (`datasets.py:293`), `FundamentalsService.create_statement_features_v2(...)` (`datasets.py:319`), `MacroService().integrate_macro_with_ohlc(...)` (`datasets.py:334`) — keep all three method signatures and their output columns intact.

- [ ] **Step 2: Add the gated `as_of` source per service (shape only, default legacy)**

For each service, add an internal `_provider_source()` helper that returns either the legacy client or a `ba2_providers`-backed adapter based on `os.getenv("FEATURES_SOURCE", "legacy")`, and thread `as_of=end_date` through the fetch when the provider source is `ba2_providers`. Because byte-equality for these multi-field feature blocks is materially harder than OHLCV, **keep the default `legacy`** and document the cutover as deferred to Task 8.

> Re-plan checkpoint: implement the adapter wiring to the confirmed Phase-2 provider categories (`news`, `fundamentals_details`, `fundamentals_overview`, `macro`). Do NOT flip the default to `ba2_providers` for these until Task 8's per-block equivalence is documented.

- [ ] **Step 3: Smoke-test that legacy default is unchanged**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && \
  venv/bin/python -c "
import os; os.environ.pop('FEATURES_SOURCE', None)
from app.services.sentiment import SentimentService
from app.services.fundamentals import FundamentalsService
from app.services.macro import MacroService
print('services import + default-legacy ok')
"
```
Expected: prints the ok line (default path untouched).

---

## Task 7: Byte-equality verification (re-source half of the GATE)

**Files (edit):** `BA2TestPlatform/backend/tests/test_resource_ml_datasets.py` + a one-off verification script.

- [ ] **Step 1: Build the SAME dataset twice (legacy vs ba2_providers) and diff the CSV**

Add the equivalence test. It builds for a fixed `(ticker, timeframe, start, end)` under each source and compares the resulting CSV. Network-bound, so mark it `@pytest.mark.integration` and provide a non-network variant that feeds both paths a shared fixed fixture.

```python
import os, hashlib
import pandas as pd
import pytest
from pathlib import Path

FIXED = dict(ticker="AAPL", timeframe="1d", start_date="2023-01-03", end_date="2023-03-31")

def _build_csv(tmp_path, source: str) -> Path:
    os.environ["OHLCV_SOURCE"] = source
    from app.api.datasets import _build_dataset_in_background
    out = tmp_path / f"ds_{source}.csv"
    # minimal Dataset row + config; reuse the real builder
    cfg = {**FIXED, "data_provider": "fmp", "technical_indicators": [],
           "sentiment_config": {}, "fundamentals_config": {}}
    # > Re-plan checkpoint: create a Dataset row with file_path=out via SessionLocal,
    #   then call _build_dataset_in_background(ds.id, cfg). Confirm the Dataset model's
    #   required columns (app/models/dataset.py) at execution time.
    ...
    return out

@pytest.mark.integration
def test_byte_equal_csv(tmp_path):
    legacy = _build_csv(tmp_path, "legacy")
    asof   = _build_csv(tmp_path, "ba2_providers")
    dl = pd.read_csv(legacy); da = pd.read_csv(asof)
    # Exact OHLCV equality on the overlapping date range (warmup already filtered).
    cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    pd.testing.assert_frame_equal(
        dl[cols].reset_index(drop=True), da[cols].reset_index(drop=True),
        check_dtype=False, atol=1e-6,
        obj="legacy vs ba2_providers OHLCV")
```

- [ ] **Step 2: Document any justified equivalence delta**

If the frames are not byte-equal, capture the cause (e.g. provider returns split/adjustment-corrected closes, or an extra trailing bar) and either (a) reconcile in the adapter, or (b) record a documented equivalence note in the test docstring + this plan's Self-Review. Per contract, "verify byte-equality of generated CSVs for a fixed (ticker,timeframe,as_of) before cutover" — a delta must be explained, not ignored.

- [ ] **Step 3: Run the equivalence test (network)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && \
  OHLCV_SOURCE=legacy venv/bin/python -m pytest tests/test_resource_ml_datasets.py -m integration -v
```
Expected: PASS (byte-equal) or a documented, justified delta. This satisfies GATE item 1.

- [ ] **Step 4: Confirm training still runs through the provider path (GATE item 3)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
# build a small dataset with OHLCV_SOURCE=ba2_providers, then run the existing
# dataset-generation + a minimal MLStrategy training smoke (job_handler / backtesting.py).
OHLCV_SOURCE=ba2_providers venv/bin/python scripts/test_dataset_generation.py
```
Expected: the provider-sourced dataset builds READY and a downstream training/backtest smoke completes — the `backtesting.py MLStrategy` "ML expert" engine is unchanged (design §5: two engines by expert type, one results model, one UI).

- [ ] **Step 5: Commit the re-source cluster**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/dataproviders/ba2providers_adapter.py backend/app/api/datasets.py backend/app/services/sentiment.py backend/app/services/fundamentals.py backend/app/services/macro.py backend/tests/test_resource_ml_datasets.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(ml-datasets): re-source OHLCV through ba2_providers as_of cache behind OHLCV_SOURCE flag (byte-equal verified)"
```

---

## Task 8: Cutover note + one-time cache invalidation + Phase-5 gate sign-off

**Files (edit):** `BA2TestPlatform/docs/` cutover note; no code default flips unless byte-equality is green.

- [ ] **Step 1: Decide the OHLCV default + record the cutover**

If Task 7's byte-equality is green, flip the runtime default to `OHLCV_SOURCE=ba2_providers` for new builds (set in the backend's runtime env / `app/main.py` startup log), keeping `legacy` available as a fallback. Sentiment/fundamentals/macro stay `legacy` (their equivalence is deferred — record this explicitly). Add a short cutover note to `BA2TestPlatform/docs/` documenting the flag, the verified delta (if any), and the rollback path.

- [ ] **Step 2: One-time cache invalidation/rebuild**

Per contract `migration_safety`: "One-time cache invalidation/rebuild expected (legacy datasets/cache vs new as_of cache)." Use the new cache UI to purge the legacy OHLCV cache once, so subsequent builds populate the shared as_of cache:

```bash
curl -s -X DELETE http://localhost:8000/api/cache/ohlcv | venv/bin/python -m json.tool
```

- [ ] **Step 3: Run the full Phase-5 gate**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
venv/bin/python -m pytest tests/test_cache_api.py tests/test_resource_ml_datasets.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform status --short   # MUST be clean (live untouched)
```
Expected: cache-API tests green (GATE 2), re-source tests green/documented (GATE 1), training smoke green (GATE 3), `BA2TradePlatform` clean (GATE 4).

- [ ] **Step 4: Commit the cutover docs**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add docs/ && \
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "docs(phase5): OHLCV re-source cutover note + one-time cache invalidation + gate sign-off"
```

---

## Self-Review

**Spec coverage (design §6 Phase 5, §3 + SHARED CONTRACTS `ml_datasets`):**
- "cache-management UI (disk usage per type, clean-all / by-type / by-date)" → Tasks 1–4. ✓ (`/api/cache/usage`, `DELETE /api/cache`, `DELETE /api/cache/{type}[?before=]`, frontend `CacheManagement.tsx`)
- "types_tracked: ohlcv, jobs, news, dataset CSVs, trained_models, news exports, + the NEW as_of cache" → `CACHE_TYPES` registry (Task 1 Step 1). ✓
- "reuse_clear_apis: NewsCacheService.clear_cache+get_cache_stats, MarketDataProviderInterface.clear_cache, job_handler cleanup_*" → reused in `cache_manager` (Tasks 1–2). ✓
- "cross_cutting: do NOT delete dataset CSVs/trained_models under generic clean; thread-safe + .tmp aware; normalize CWD path" → `clear_all` destructive guard, `_safe_unlink` `.tmp` skip, `_resolve()` root normalization. ✓
- "re-source ML datasets through ba2_providers as_of cache; two consumers of one cache" → adapter + flag-aware `get_ohlcv_provider` (Tasks 5–6). ✓
- "seam at get_ohlcv_provider() + the sentiment/fundamentals/macro factories" → Task 5 (OHLCV) + Task 6 (services). ✓
- "as_of=dataset.end_date for reproducible/leak-free builds" → adapter passes `as_of=end_date` (Task 5 Step 2). ✓
- "invariants_preserved: use_cache, interval, calculate_warmup_period + warmup filtering, targets, normalization UNCHANGED" → only the factory + an adapter added; `_build_dataset_in_background` body untouched. ✓
- "migration_safety: gate behind OHLCV_SOURCE; legacy fallback; byte-equality before cutover; one-time invalidation" → Tasks 5/7/8. ✓
- "keep backtesting.py MLStrategy single-asset path as the ML expert engine (two engines, one results model, one UI)" → not modified; Decision 1 + Task 7 Step 4. ✓
- "surface the as_of cache as its own type" → `"asof"` in `CACHE_TYPES`. ✓

**Placeholder scan:** the scanner, deletion helpers, both API routers, the adapter, the flag-aware factory, the frontend page, and the cache-API tests contain full code. The `...` ellipses appear ONLY inside the network-bound `_build_csv` helper (Task 7 Step 1) immediately under an explicit `> Re-plan checkpoint` instructing the executor to construct the `Dataset` row from `app/models/dataset.py` at execution time — deliberate, not a stub. No "TBD"/"add error handling later".

**Type/name consistency:** real names used throughout — `_build_dataset_in_background` (`datasets.py:174`), `get_ohlcv_provider` (`:36`), `MarketDataPoint` (`base.py:18`), `provider.get_data` (`MarketDataProviderInterface.py:337`), `calculate_warmup_period` (`dataset_handler.py:57`), `NewsCacheService.clear_cache/get_cache_stats` (`news_cache.py:585/550`), `MarketDataProviderInterface.clear_cache` (`:566`), `cleanup_job_models` (`job_handler.py:789`), `CACHE_FOLDER` (`MarketDataProviderInterface.py:23`), router registration pattern (`main.py:332-345`), camelCase results contract (`Backtesting.tsx:126`). Endpoint names are stable across backend + frontend (`/api/cache/usage`, `DELETE /api/cache`, `DELETE /api/cache/{type}`).

**Known reconciliation points (verify against source/Phase-2 during execution, do not assume):**
1. Phase-2 `ba2_providers.get()` exact signature + OHLCV row attribute names + registry key (`get_provider("ohlcv","fmp")`) — Task 5 Step 1.
2. Phase-2 on-disk as_of cache paths (parquet path + `provider_cache` SQLite location) for the `"asof"` cache type — Task 1 Step 1 checkpoint.
3. `MarketDataPoint` constructor kwargs (`base.py:18-46`) — Task 5 Step 2.
4. `Dataset` model required columns for the byte-equality build (`app/models/dataset.py`) — Task 7 Step 1.
5. Frontend router file + nav registration path — Task 3 Step 1.
6. Sentiment/fundamentals/macro single instantiation line per service — Task 6 Step 1.

**Safety:** all edits are confined to `BA2TestPlatform`. `BA2TradePlatform/ba2_trade_platform/` is never touched (GATE item 4), consistent with the locked decision that the live platform migrates only in Phase 6.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase5-cache-ui-ml-datasets-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`). The two clusters (Tasks 1–4 cache UI; Tasks 5–8 re-source) are independent and can be dispatched in parallel.
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

Prerequisite: the Phase-0 packages installed editable into `BA2TestPlatform/backend/venv` (`PYTHON=BA2TestPlatform/backend/venv/bin/python bash BA2TradeCommon/install.sh --editable`), and Phase 2's `ba2_providers` `get(as_of=...)` + native cache merged (see the Phase-dependencies re-plan checkpoint). Use `recon:ml-data` for the dataset-builder reconciliation points and `recon:test-engine (UI)` to exercise the cache page. All work lands on `BA2TestPlatform`; `BA2TradePlatform` stays read-only.
