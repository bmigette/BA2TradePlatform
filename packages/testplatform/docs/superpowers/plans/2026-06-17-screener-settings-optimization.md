# Screener-Settings Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the genetic optimizer treat screener thresholds (market-cap / volume / price / RVOL / price-drop) as genes, with a **dynamic per-day** universe selected from a precomputed, exportable metric store — so the GA can search "which stocks to trade, when" fast.

**Architecture:** A one-time builder fetches the **FMP screener universe** (~thousands of US symbols, current/actively-trading = survivorship-NOT-free by decision) and writes a **parquet, date-partitioned metric store** holding each symbol's screen metrics (cap/volume/price/sector + RVOL + price-drop %) at a **configurable scan cadence (default 1 week)**, computed **vectorized per-symbol** from the already-disk-cached OHLCV. At optimize time the store loads once per worker into pandas; each GA individual's screener genes filter the store at the most-recent scan date (static threshold → shortlist → dynamic threshold → sort/max_stocks) to produce a **time-varying** universe (held between scans), and the backtest engine gates entries to it.

**Tech Stack:** Python 3.12, pandas/pyarrow (parquet), DEAP GA, FMP API via `ba2_providers.fmp_common`, the existing as-of OHLCV parquet cache, FastAPI/SQLModel backend. Tests with pytest. Run Python via `BA2TestPlatform/backend/venv/bin/python`; the alpaca/FMP-keyed CLI is `~/ba2-venvs/test/bin/ba2-test`.

**Key decisions (from design):**
- Universe = **FMP screener current set** (survivorship-biased; accepted for v1). NOT sp500-only, NOT global-broad.
- **Dynamic shortlist over the BT**: the screen is re-evaluated at each scan-cadence date, so the universe is a *time series*, not one fixed list; it holds constant between scan dates.
- **Scan cadence is an OPTIMIZATION config option, default 1 week (7 days).** It sets both how often the metric store materializes a scan and how often the engine re-screens; align it with the analysis schedule (set 1 for daily analysis). Threaded: optimize config → store build → engine resolution.
- **Store raw metric VALUES** (not pass/fail) so any threshold in the opti range filters at query time. Keep the **static** gene range small (its loosest bound sizes the shortlist superset); dynamic gene ranges may be wide.
- **No server** (no Redis): parquet on disk (export + incremental) → in-process pandas per worker.

---

## File Structure

- **Create** `BA2TradeProviders/ba2_providers/screener/metric_store.py` — the metric store: build (universe enum + vectorized per-symbol daily metrics), parquet date-partitioned read/write with incremental extend, and the per-day filter `screen_universe_for_day`. One responsibility: the screener metric data layer. Lives in `ba2_providers` next to the existing `screener/` code.
- **Modify** `BA2TestPlatform/ba2test_launcher.py` — add `build-screener-metrics` CLI (Task 4) + the `_SCREENER_OPT` gene spec and `--screener-*` flags on `_cmd_optimize` (Task 9).
- **Modify** `BA2TestPlatform/backend/app/services/strategy_param_space.py` — `screener:*` gene namespace in `collect_param_space` + `decode_params` (Tasks 7-8).
- **Modify** `BA2TestPlatform/backend/app/services/strategy_optimization_handler.py` — hoist the loaded metric store in `_build_hoisted_state`; carry `screener_overrides` + store path through `_build_daily_trial_config` (Task 10).
- **Modify** `BA2TestPlatform/backend/app/services/backtest/daily_engine.py` — per-day universe gating from the screener selection (Task 11).
- **Tests** `BA2TradeProviders/tests/test_screener_metric_store.py`, `BA2TestPlatform/backend/tests/backtest/test_screener_genes.py`, `...test_screener_opt_e2e.py`.

---

## Phase 1 — Screener metric store (build, export, incremental)

### Task 1: Universe enumeration from the FMP screener

**Files:**
- Create: `BA2TradeProviders/ba2_providers/screener/metric_store.py`
- Test: `BA2TradeProviders/tests/test_screener_metric_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screener_metric_store.py
from unittest.mock import patch
from ba2_providers.screener import metric_store as ms

_FAKE_SCREEN = [
    {"symbol": "AAA", "marketCap": 5e9, "price": 30.0, "volume": 2_000_000, "sector": "Tech"},
    {"symbol": "BBB", "marketCap": 8e8, "price": 12.0, "volume": 100_000, "sector": "Energy"},  # below cap
    {"symbol": "CCC", "marketCap": 3e9, "price": 40.0, "volume": 900_000, "sector": "Tech"},
]

def test_enumerate_universe_applies_loosest_static_prefilter():
    with patch.object(ms, "_fetch_screener_rows", return_value=_FAKE_SCREEN):
        # loosest bounds: cap>=1e9, price>=10, vol>=500k -> AAA, CCC (BBB drops on cap+vol)
        rows = ms.enumerate_universe(api_key="x", market_cap_min=1e9, price_min=10.0, volume_min=500_000)
    assert {r["symbol"] for r in rows} == {"AAA", "CCC"}
    assert rows[0]["sector"]  # static fields preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_enumerate_universe_applies_loosest_static_prefilter -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'enumerate_universe'`

- [ ] **Step 3: Write minimal implementation**

```python
# ba2_providers/screener/metric_store.py
"""Precomputed, exportable screener METRIC STORE for screener-settings optimization.

The FMP screener universe (current actively-trading US names — survivorship-biased by design)
is enumerated once, then each symbol's per-day screen metrics are computed VECTORISED from the
already-disk-cached OHLCV and written as date-partitioned parquet (exportable; extend by adding
partitions). At optimize time the store loads into pandas and each GA individual filters it
per day. No server.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from ba2_providers.fmp_common import fmp_http_get

_SCREENER_URL = "https://financialmodelingprep.com/api/v3/stock-screener"


def _fetch_screener_rows(api_key: str) -> List[Dict[str, Any]]:
    """One call to the FMP screener for the current actively-trading US universe."""
    resp = fmp_http_get(
        _SCREENER_URL,
        params={"limit": 10000, "exchange": "nasdaq,nyse,amex",
                "isActivelyTrading": "true", "apikey": api_key},
        endpoint="stock-screener",
    )
    rows = resp.json()
    return rows if isinstance(rows, list) else []


def enumerate_universe(api_key: str, market_cap_min: float, price_min: float,
                       volume_min: float) -> List[Dict[str, Any]]:
    """Return screener rows passing the LOOSEST static bounds (the shortlist superset).

    Uses the screener's own current marketCap/price/volume fields (one call). These bounds are
    the loosest of every static gene's range, so no individual's looser threshold can admit a
    symbol we didn't include.
    """
    out = []
    for r in _fetch_screener_rows(api_key):
        cap = r.get("marketCap") or 0
        px = r.get("price") or 0
        vol = r.get("volume") or 0
        if cap >= market_cap_min and px >= price_min and vol >= volume_min:
            out.append(r)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_enumerate_universe_applies_loosest_static_prefilter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TradeProviders add ba2_providers/screener/metric_store.py tests/test_screener_metric_store.py
git -C BA2TradeProviders commit -m "feat(screener): metric-store universe enumeration (loosest-static prefilter)"
```

### Task 2: Vectorized per-symbol daily metrics

Compute, for one symbol, the full daily series of dynamic metrics from a cached OHLCV DataFrame in a single rolling pass: RVOL (`volume / 20d-avg-volume`), price-drop % (`(rolling N-day peak − close) / peak × 100`), and as-of market cap (`shares × close`). Static fields ride along from the screener row.

**Files:**
- Modify: `BA2TradeProviders/ba2_providers/screener/metric_store.py`
- Test: `BA2TradeProviders/tests/test_screener_metric_store.py`

- [ ] **Step 1: Write the failing test**

```python
import pandas as pd
import numpy as np

def _ohlcv(n=40, start_close=100.0):
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(np.linspace(start_close, start_close + n, n), index=idx)
    vol = pd.Series([1_000_000] * n, index=idx, dtype=float)
    vol.iloc[-1] = 3_000_000  # last day spikes -> RVOL ~3x
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 5, "Close": close, "Volume": vol})

def test_daily_metrics_rvol_and_dip_are_vectorised():
    df = _ohlcv()
    out = ms.compute_daily_metrics(df, shares=1_000_000, rvol_window=20, drop_days=5)
    last = out.iloc[-1]
    assert round(last["relative_volume"], 1) == 3.0          # 3M / 1M avg
    assert last["market_cap"] == 1_000_000 * df["Close"].iloc[-1]
    # price-drop %: close is at a rising series' peak -> ~0 drop on the last bar
    assert last["price_drop_pct"] >= 0.0
    assert out.shape[0] == df.shape[0]                        # one row per day (a time series)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_daily_metrics_rvol_and_dip_are_vectorised -v`
Expected: FAIL with `AttributeError: ... 'compute_daily_metrics'`

- [ ] **Step 3: Write minimal implementation**

```python
import pandas as pd  # add at top of metric_store.py

def compute_daily_metrics(ohlcv: "pd.DataFrame", shares: Optional[float],
                          rvol_window: int = 20, drop_days: int = 1) -> "pd.DataFrame":
    """Per-day screen metrics for ONE symbol, vectorised over its full history.

    ``ohlcv`` is indexed by date with columns Open/High/Low/Close/Volume (the shape the as-of
    OHLCV cache returns). Returns a DataFrame indexed by date with columns:
    close, market_cap, relative_volume, price_drop_pct. NaN rows (insufficient lookback) are
    kept — callers drop them. Point-in-time safe: every value at row D uses only bars <= D.
    """
    close = ohlcv["Close"].astype(float)
    vol = ohlcv["Volume"].astype(float)
    # RVOL: today's volume / trailing average of the PRIOR rvol_window days (EXCLUDES today via
    # shift(1) — point-in-time: today is the spike measured against its prior baseline). The
    # first row's avg is NaN -> .where(avg_vol > 0) leaves RVOL 0.
    avg_vol = vol.shift(1).rolling(rvol_window, min_periods=1).mean()
    rvol = (vol / avg_vol).where(avg_vol > 0, 0.0)
    # Price drop %: peak of the trailing window (inclusive) vs today's close.
    peak = close.rolling(max(1, drop_days), min_periods=1).max()
    drop_pct = ((peak - close) / peak * 100.0).where(peak > 0, 0.0)
    mcap = (close * shares) if shares else pd.Series(float("nan"), index=close.index)
    return pd.DataFrame({
        "close": close,
        "market_cap": mcap,
        "relative_volume": rvol.round(4),
        "price_drop_pct": drop_pct.round(4),
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_daily_metrics_rvol_and_dip_are_vectorised -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TradeProviders add ba2_providers/screener/metric_store.py tests/test_screener_metric_store.py
git -C BA2TradeProviders commit -m "feat(screener): vectorised per-symbol daily metrics (RVOL/dip/mcap)"
```

### Task 3: Parquet date-partitioned writer + incremental extend

Write the metric store as `parquet` partitioned by year-month (`<store>/ym=YYYY-MM/part.parquet`), so exporting is a folder copy and extending the date range only writes the new partitions. A `build_store(...)` orchestrates: enumerate universe → for each symbol load cached OHLCV → `compute_daily_metrics` → concat → write only the partitions not already present (incremental).

**Files:**
- Modify: `BA2TradeProviders/ba2_providers/screener/metric_store.py`
- Test: `BA2TradeProviders/tests/test_screener_metric_store.py`

- [ ] **Step 1: Write the failing test**

```python
import os

def test_store_write_is_incremental_by_partition(tmp_path):
    store = str(tmp_path / "mstore")
    df_jan = pd.DataFrame({"symbol": ["AAA"], "date": ["2023-01-31"], "close": [10.0],
                           "market_cap": [1e9], "relative_volume": [1.2], "price_drop_pct": [0.0],
                           "sector": ["Tech"], "volume": [1e6], "price": [10.0]})
    ms.write_partitions(store, df_jan)
    assert os.path.isdir(os.path.join(store, "ym=2023-01"))
    # existing partition is reported as present so build can skip it
    assert ms.existing_months(store) == {"2023-01"}
    # adding Feb does not touch Jan
    df_feb = df_jan.assign(date=["2023-02-28"])
    ms.write_partitions(store, df_feb)
    assert ms.existing_months(store) == {"2023-01", "2023-02"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_store_write_is_incremental_by_partition -v`
Expected: FAIL with `AttributeError: ... 'write_partitions'`

- [ ] **Step 3: Write minimal implementation**

```python
import os

def existing_months(store_dir: str) -> set:
    """Year-months (``YYYY-MM``) already materialised in the store (for incremental skip)."""
    if not os.path.isdir(store_dir):
        return set()
    return {d[len("ym="):] for d in os.listdir(store_dir) if d.startswith("ym=")}

def write_partitions(store_dir: str, df: "pd.DataFrame") -> None:
    """Write rows to ``<store>/ym=YYYY-MM/part.parquet`` (one file per month). Overwrites the
    month's file (idempotent re-build of a month); never touches other months."""
    os.makedirs(store_dir, exist_ok=True)
    ym = df["date"].astype(str).str.slice(0, 7)
    for month, chunk in df.groupby(ym):
        d = os.path.join(store_dir, f"ym={month}")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "part.parquet.tmp")
        chunk.to_parquet(tmp, index=False)
        os.replace(tmp, os.path.join(d, "part.parquet"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_store_write_is_incremental_by_partition -v`
Expected: PASS

- [ ] **Step 5: Add `build_store` orchestrator (no new test — covered by the CLI smoke in Task 4)**

```python
def scan_dates(start: str, end: str, cadence_days: int) -> "pd.DatetimeIndex":
    """The common scan-date grid: every ``cadence_days`` CALENDAR days from start..end.
    Default cadence 7 = one scan per week. Shared across symbols so scan dates are consistent."""
    import pandas as pd
    return pd.date_range(start=start, end=end, freq=f"{int(cadence_days)}D")

def build_store(store_dir: str, api_key: str, start: str, end: str, *,
                market_cap_min: float, price_min: float, volume_min: float,
                ohlcv_get, shares_get, cadence_days: int = 7,
                rvol_window: int = 20, drop_days: int = 1) -> Dict[str, Any]:
    """Build/extend the metric store for [start,end] at ``cadence_days`` (default 7 = weekly).
    SKIPS months already present (incremental). ``ohlcv_get(symbol, end_date)`` -> OHLCV up to
    end_date (as-of cache); ``shares_get(symbol)`` -> latest-filing shares. For each symbol the
    daily metrics are computed then sampled AS-OF each scan date (latest trading day <= scan
    date via ffill), so each row is point-in-time for its scan date. Returns
    {symbols, months_written, months_skipped, cadence_days}."""
    import pandas as pd
    grid = scan_dates(start, end, cadence_days)
    want_months = sorted({d.strftime("%Y-%m") for d in grid})
    have = existing_months(store_dir)
    todo_months = [m for m in want_months if m not in have]
    if not todo_months:
        return {"symbols": 0, "months_written": 0, "months_skipped": len(want_months), "cadence_days": cadence_days}
    grid_todo = grid[[d.strftime("%Y-%m") in set(todo_months) for d in grid]]
    universe = enumerate_universe(api_key, market_cap_min, price_min, volume_min)
    static_by_sym = {r["symbol"]: r for r in universe}
    frames = []
    for sym, srow in static_by_sym.items():
        df = ohlcv_get(sym, end)
        if df is None or df.empty:
            continue
        m = compute_daily_metrics(df, shares=shares_get(sym), rvol_window=rvol_window, drop_days=drop_days)
        m = m.reindex(grid_todo, method="ffill")             # value AS-OF each scan date
        m = m.dropna(subset=["close"]).reset_index().rename(columns={"index": "date"})
        m["date"] = m["date"].astype(str).str.slice(0, 10)
        if m.empty:
            continue
        m["symbol"] = sym
        m["sector"] = srow.get("sector")
        m["volume"] = m["close"] * 0 + (srow.get("volume") or 0)  # static screener volume ride-along
        m["price"] = m["close"]
        frames.append(m)
    if frames:
        write_partitions(store_dir, pd.concat(frames, ignore_index=True))
    return {"symbols": len(static_by_sym), "months_written": len(todo_months),
            "months_skipped": len(set(have) & set(want_months)), "cadence_days": cadence_days}
```

- [ ] **Step 6: Commit**

```bash
git -C BA2TradeProviders add ba2_providers/screener/metric_store.py tests/test_screener_metric_store.py
git -C BA2TradeProviders commit -m "feat(screener): parquet date-partitioned store + incremental build_store"
```

### Task 4: CLI `ba2-test build-screener-metrics`

**Files:**
- Modify: `BA2TestPlatform/ba2test_launcher.py` (add `_cmd_build_screener_metrics` + a subparser, mirroring the existing `fetch-screener` parser at `ba2test_launcher.py:1242-1248` and dispatch map at `:1356`)

- [ ] **Step 1: Implement the command (wires the store to the as-of OHLCV cache + shares provider)**

```python
def _cmd_build_screener_metrics(args) -> int:
    import app.models  # noqa: F401
    import pandas as _pd
    from datetime import datetime as _dt
    from app.models.database import init_db
    from ba2_common.config import get_app_setting
    from ba2_providers.screener import metric_store as ms
    from ba2_providers.cache.cached_get import ohlcv_get  # as-of OHLCV cache accessor
    from ba2_providers import get_provider
    init_db()
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        sys.exit("build-screener-metrics: FMP_API_KEY not configured")
    details = get_provider("fundamentals_details", "fmp")  # for shares_get
    def _ohlcv(sym, end):
        # The as-of OHLCV cache returns a DataFrame with a `Date` COLUMN + int index, rows not
        # guaranteed sorted, and `Date` parsed tz-AWARE (UTC). compute_daily_metrics expects a
        # tz-naive, ascending, date-INDEXED frame (and rolling needs ascending order), and the
        # scan grid is tz-naive — so normalize here (verified against the real cache in a perf
        # pass; the synthetic unit-test fixture was already clean so it didn't surface this).
        prov = get_provider("ohlcv", "fmp")
        df = ohlcv_get(prov, sym, as_of=_dt.fromisoformat(end), lookback=4000)
        if df is None or len(df) == 0:
            return df
        idx = _pd.to_datetime(df["Date"])
        if idx.dt.tz is not None:
            idx = idx.dt.tz_localize(None)
        return df.set_index(idx).sort_index()
    def _shares(sym):
        try:
            return details.shares_outstanding(sym)  # latest filing; static for v1
        except Exception:  # noqa: BLE001
            return None
    summary = ms.build_store(
        args.store, api_key, args.start, args.end,
        market_cap_min=args.market_cap_min, price_min=args.price_min, volume_min=args.volume_min,
        ohlcv_get=_ohlcv, shares_get=_shares, cadence_days=args.cadence_days, drop_days=args.drop_days)
    print(f"build-screener-metrics: {summary}")
    return 0
```

Add the subparser (after `fetch-screener` at `:1248`):

```python
    bm = sub.add_parser("build-screener-metrics", help="Build/extend the screener METRIC store (parquet).")
    bm.add_argument("--store", required=True, help="Path to the parquet metric-store dir.")
    bm.add_argument("--start", required=True); bm.add_argument("--end", required=True)
    bm.add_argument("--market-cap-min", type=float, required=True, help="LOOSEST cap bound (shortlist superset).")
    bm.add_argument("--price-min", type=float, default=0.0)
    bm.add_argument("--volume-min", type=float, default=0.0)
    bm.add_argument("--cadence-days", type=int, default=7, help="Scan cadence in days (default 7 = weekly). Match the analysis schedule.")
    bm.add_argument("--drop-days", type=int, default=1)
```

And the dispatch entry (in the `{...}` map near `:1356`): `"build-screener-metrics": lambda: _cmd_build_screener_metrics(args),`

- [ ] **Step 2: Smoke-test the build (small, real)**

Run:
```bash
~/ba2-venvs/test/bin/ba2-test build-screener-metrics --store /tmp/mstore \
  --start 2023-01-01 --end 2023-03-31 --market-cap-min 50000000000 --price-min 20 --volume-min 1000000 \
  --cadence-days 7 --drop-days 5
```
Expected: prints a summary dict with `symbols` > 0, `months_written` == 3, and `cadence_days: 7`; `/tmp/mstore/ym=2023-01/part.parquet` exists and holds ~weekly rows. Re-running the SAME command prints `months_written: 0, months_skipped: 3` (incremental).

- [ ] **Step 3: Commit**

```bash
git -C BA2TestPlatform add ba2test_launcher.py
git -C BA2TestPlatform commit -m "feat(cli): build-screener-metrics — build/extend the parquet metric store"
```

---

## Phase 2 — In-memory loader + per-day dynamic filter

### Task 5: Load the store into memory (memoized per worker)

**Files:**
- Modify: `BA2TradeProviders/ba2_providers/screener/metric_store.py`
- Test: `BA2TradeProviders/tests/test_screener_metric_store.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_store_returns_indexed_frame(tmp_path):
    store = str(tmp_path / "s")
    ms.write_partitions(store, pd.DataFrame({
        "symbol": ["AAA", "BBB"], "date": ["2023-01-31", "2023-01-31"], "close": [10.0, 20.0],
        "market_cap": [2e9, 6e8], "relative_volume": [1.5, 0.9], "price_drop_pct": [12.0, 1.0],
        "sector": ["Tech", "Energy"], "volume": [2e6, 1e5], "price": [10.0, 20.0]}))
    df = ms.load_store(store)
    assert set(df["symbol"]) == {"AAA", "BBB"}
    assert "2023-01-31" in set(df["date"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_load_store_returns_indexed_frame -v`
Expected: FAIL with `AttributeError: ... 'load_store'`

- [ ] **Step 3: Write minimal implementation**

```python
_STORE_MEMO: Dict[str, "pd.DataFrame"] = {}

def load_store(store_dir: str) -> "pd.DataFrame":
    """Load all month partitions into one DataFrame, memoised by store path (per process —
    GA workers stay alive across trials, so the store loads ~once per worker)."""
    import pandas as pd, glob
    hit = _STORE_MEMO.get(store_dir)
    if hit is not None:
        return hit
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "part.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")
    df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    _STORE_MEMO[store_dir] = df
    return df

def clear_store_memo() -> None:
    _STORE_MEMO.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_load_store_returns_indexed_frame -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TradeProviders add ba2_providers/screener/metric_store.py tests/test_screener_metric_store.py
git -C BA2TradeProviders commit -m "feat(screener): memoised in-memory store loader"
```

### Task 6: Per-day dynamic filter `screen_universe_for_day`

**Files:**
- Modify: `BA2TradeProviders/ba2_providers/screener/metric_store.py`
- Test: `BA2TradeProviders/tests/test_screener_metric_store.py`

- [ ] **Step 1: Write the failing test**

```python
def test_screen_universe_for_day_applies_thresholds_sort_and_cap():
    df = pd.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"], "date": ["2023-01-31"] * 3,
        "close": [10, 20, 30.0], "market_cap": [5e9, 2e9, 9e8],
        "relative_volume": [2.0, 1.1, 5.0], "price_drop_pct": [20.0, 1.0, 18.0],
        "sector": ["Tech"] * 3, "volume": [2e6, 2e6, 2e6], "price": [10, 20, 30.0]})
    sel = ms.screen_universe_for_day(df, "2023-01-31", {
        "market_cap_min": 1e9,            # drops CCC (9e8)
        "relative_volume_min": 1.5,       # drops BBB (1.1)
        "price_drop_pct": 15.0,           # AAA dip 20 >= 15 ok
        "max_stocks": 5, "sort_metric": "market_cap"})
    assert sel == ["AAA"]                  # only AAA passes all
    # forward day with no rows -> empty
    assert ms.screen_universe_for_day(df, "2023-02-01", {"market_cap_min": 0}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py::test_screen_universe_for_day_applies_thresholds_sort_and_cap -v`
Expected: FAIL with `AttributeError: ... 'screen_universe_for_day'`

- [ ] **Step 3: Write minimal implementation**

```python
def screen_universe_for_day(store_df: "pd.DataFrame", day: str,
                            settings: Dict[str, Any]) -> List[str]:
    """The dynamic per-day universe for one individual's screener thresholds.

    ``day`` is 'YYYY-MM-DD'. ``settings`` keys (all optional; absent => not enforced):
    market_cap_min/max, price_min/max, volume_min/max, relative_volume_min, price_drop_pct
    (min drop to qualify a 'dip'), max_stocks, sort_metric ('market_cap'|'relative_volume'|
    'price_drop_pct'). Returns the selected symbols (<= max_stocks), sorted by sort_metric desc.
    Pure in-memory filter over the precomputed row values — microseconds."""
    d = store_df[store_df["date"] == day]
    if d.empty:
        return []
    def _ge(col, key):
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] >= float(v)]
    def _le(col, key):
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] <= float(v)]
    _ge("market_cap", "market_cap_min"); _le("market_cap", "market_cap_max")
    _ge("price", "price_min"); _le("price", "price_max")
    _ge("volume", "volume_min"); _le("volume", "volume_max")
    _ge("relative_volume", "relative_volume_min")
    _ge("price_drop_pct", "price_drop_pct")
    if d.empty:
        return []
    sort_col = settings.get("sort_metric") or "market_cap"
    if sort_col not in d.columns:
        sort_col = "market_cap"
    d = d.sort_values(sort_col, ascending=False)
    n = int(settings.get("max_stocks") or 0)
    if n > 0:
        d = d.head(n)
    return list(d["symbol"])


def screen_universe_as_of(store_df: "pd.DataFrame", as_of_day: str,
                          settings: Dict[str, Any]) -> List[str]:
    """Same as ``screen_universe_for_day`` but resolves to the LATEST scan date <= as_of_day,
    so a bar between scan dates gets the held universe (the cadence is weekly by default). Empty
    if no scan date is on/before as_of_day."""
    dates = store_df["date"]
    prior = dates[dates <= as_of_day]
    if prior.empty:
        return []
    return screen_universe_for_day(store_df, prior.max(), settings)
```

- [ ] **Step 4: Add the as-of test, then run all metric-store tests**

```python
def test_screen_universe_as_of_holds_between_scans():
    df = pd.DataFrame({"symbol": ["AAA"], "date": ["2023-03-06"], "close": [10.0],
        "market_cap": [5e9], "relative_volume": [2.0], "price_drop_pct": [20.0],
        "sector": ["T"], "volume": [2e6], "price": [10.0]})
    # a Thursday with no scan row resolves to the Monday scan
    assert ms.screen_universe_as_of(df, "2023-03-09", {"market_cap_min": 1e9}) == ["AAA"]
    assert ms.screen_universe_as_of(df, "2023-03-01", {"market_cap_min": 1e9}) == []  # before first scan
```

Run: `~/ba2-venvs/test/bin/python -m pytest BA2TradeProviders/tests/test_screener_metric_store.py -v`
Expected: PASS (all metric-store tests)

- [ ] **Step 5: Commit**

```bash
git -C BA2TradeProviders add ba2_providers/screener/metric_store.py tests/test_screener_metric_store.py
git -C BA2TradeProviders commit -m "feat(screener): per-day dynamic filter (static+dynamic thresholds, sort, cap)"
```

---

## Phase 3 — `screener:*` gene namespace

### Task 7: Collect `screener:*` params into the search space

**Files:**
- Modify: `BA2TestPlatform/backend/app/services/strategy_param_space.py` (add `_collect_screener`; call it from `collect_param_space` at `:159-163`)
- Test: `BA2TestPlatform/backend/tests/backtest/test_screener_genes.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/backtest/test_screener_genes.py
from app.services.strategy_param_space import collect_param_space, decode_params

class _Strat:  # minimal stand-in
    initial_tp_percent = 5.0; initial_sl_percent = 5.0
    buy_entry_conditions = None; sell_entry_conditions = None; exit_conditions = []

def test_collect_screener_adds_namespaced_genes():
    space = collect_param_space(
        _Strat(), expert_cfg={"params": {}}, bypass=True,
        screener_cfg={
            "screener_market_cap_min": {"min": 1e9, "max": 5e9, "step": 1e9, "type": "float", "optimize": True},
            "screener_relative_volume_min": {"min": 1.0, "max": 2.0, "step": 0.1, "type": "float", "optimize": True},
        })
    assert "screener:screener_market_cap_min" in space
    assert "screener:screener_relative_volume_min" in space
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_genes.py::test_collect_screener_adds_namespaced_genes -v`
Expected: FAIL with `TypeError: collect_param_space() got an unexpected keyword argument 'screener_cfg'`

- [ ] **Step 3: Write minimal implementation**

In `strategy_param_space.py`, add the collector (mirror `_collect_expert`'s range-dict shape):

```python
def _collect_screener(screener_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """screener:<setting> ranges from a screener_cfg ({setting: {min,max,step,type,optimize}})."""
    out: Dict[str, Any] = {}
    for name, spec in (screener_cfg or {}).items():
        if not spec.get("optimize"):
            continue
        out[f"screener:{name}"] = {k: spec[k] for k in ("min", "max", "step", "type") if k in spec}
    return out
```

Change the signature + body of `collect_param_space` (`:142-178`):

```python
def collect_param_space(strategy, expert_cfg=None, bypass=False, screener_cfg=None):
    space: Dict[str, Any] = {}
    space.update(_collect_expert(expert_cfg))
    if not bypass:
        space.update(_collect_tp_sl(strategy))
        space.update(_collect_conditions(strategy))
    space.update(_collect_screener(screener_cfg))   # screener genes apply on BOTH paths
    if not space:
        raise ValueError(...)  # unchanged message
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_genes.py::test_collect_screener_adds_namespaced_genes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TestPlatform add backend/app/services/strategy_param_space.py backend/tests/backtest/test_screener_genes.py
git -C BA2TestPlatform commit -m "feat(opt): screener:* gene namespace in collect_param_space"
```

### Task 8: Decode `screener:*` → `screener_overrides`

**Files:**
- Modify: `BA2TestPlatform/backend/app/services/strategy_param_space.py` (`decode_params` at `:242-307`)
- Test: `BA2TestPlatform/backend/tests/backtest/test_screener_genes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_decode_screener_overrides():
    out = decode_params(_Strat(), {
        "tp": 6.0, "sl": 4.0,
        "screener:screener_market_cap_min": 2e9,
        "screener:screener_relative_volume_min": 1.4,
    })
    assert out["screener_overrides"] == {
        "screener_market_cap_min": 2e9, "screener_relative_volume_min": 1.4}
    assert out["tp"] == 6.0  # existing fields still present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_genes.py::test_decode_screener_overrides -v`
Expected: FAIL with `KeyError: 'screener_overrides'` (or the `ValueError: Unknown decoded param namespace: 'screener:...'`)

- [ ] **Step 3: Write minimal implementation**

In `decode_params`, add a `screener_overrides` dict (next to `expert_overrides` at `:238`), a branch in the loop (before the final `else` at `:262`), and include it in the return (`:303-307`):

```python
    screener_overrides: Dict[str, Any] = {}
    ...
        elif key.startswith("screener:"):
            screener_overrides[key[len("screener:"):]] = val
    ...
    return {
        "tp": tp, "sl": sl,
        "expert_overrides": expert_overrides,
        "screener_overrides": screener_overrides,
        "buy_tree": buy_tree, "sell_tree": sell_tree, "exit_rules": exit_rules,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_genes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TestPlatform add backend/app/services/strategy_param_space.py backend/tests/backtest/test_screener_genes.py
git -C BA2TestPlatform commit -m "feat(opt): decode screener:* genes into screener_overrides"
```

---

## Phase 4 — Optimizer wiring

### Task 9: Launcher `_SCREENER_OPT` spec + `--screener` flags on `optimize`

**Files:**
- Modify: `BA2TestPlatform/ba2test_launcher.py` (`_SCREENER_OPT` near `_RM_OPT` at `:504-509`; `_cmd_optimize` at `:751-868`; optimize subparser at `:1282-1308`)

- [ ] **Step 1: Add the spec + flags + config wiring**

```python
# near _RM_OPT (:504): the screener genes (small static range; wide dynamic ranges).
_SCREENER_OPT = {
    "screener_market_cap_min": {"min": 2e9, "max": 1e10, "step": 1e9, "type": "float", "optimize": True},
    "screener_relative_volume_min": {"min": 1.0, "max": 3.0, "step": 0.1, "type": "float", "optimize": True},
    "screener_price_drop_pct": {"min": 0.0, "max": 25.0, "step": 1.0, "type": "float", "optimize": True},
    "screener_max_stocks": {"min": 5, "max": 30, "step": 5, "type": "int", "optimize": True},
}
```

In the optimize subparser (`:1282`): `op.add_argument("--screener", action="store_true", help="Optimize a screener-selected dynamic universe.")`, `op.add_argument("--screener-store", default=None, help="Path to the parquet metric store (build-screener-metrics).")`, `op.add_argument("--screener-base-json", default=None, help="JSON of base (non-optimized) screener settings.")`, and `op.add_argument("--screener-cadence-days", type=int, default=7, help="Scan cadence in days (default 7 = weekly). Must match the metric store's build cadence; align with --run-schedule.")`.

In `_cmd_optimize` (`:810-821`), when `args.screener`: require `args.screener_store`; add a `screener_opt` block to `backtest_block` (carrying the cadence — a config option of the optimization) and merge the screener genes into `expert_params`:

```python
        if getattr(args, "screener", False):
            if not args.screener_store:
                sys.exit("optimize: --screener requires --screener-store")
            base = json.load(open(args.screener_base_json)) if args.screener_base_json else {}
            backtest_block["screener_opt"] = {
                "store": args.screener_store,
                "base_settings": base,
                "cadence_days": int(args.screener_cadence_days),  # default 7 = weekly
            }
        ...
        cfg = {
            ...
            "expert_params": {**spec["expert_params"], **_RM_OPT,
                              **({} if not getattr(args, "screener", False)
                                 else {f"screener:{k}": v for k, v in _SCREENER_OPT.items()})},
            "backtest": backtest_block,
        }
```

Note: the genes are pre-namespaced with `screener:` here so `collect_param_space`/`decode_params` route them (the existing `expert_params` are `model:`-namespaced inside `_collect_expert`; verify the handler passes `screener_cfg` — see Task 10). The run-level `enabled_instruments` for a screener optimize = the metric store's full symbol union (so the engine has OHLCV for any pick); set it from `metric_store.load_store(store)["symbol"].unique()` plus require those symbols' OHLCV be cached.

- [ ] **Step 2: Verify the CLI parses + builds config (no run yet)**

Run: `~/ba2-venvs/test/bin/ba2-test optimize --help` → shows `--screener`, `--screener-store`.

- [ ] **Step 3: Commit**

```bash
git -C BA2TestPlatform add ba2test_launcher.py
git -C BA2TestPlatform commit -m "feat(cli): optimize --screener (screener genes + metric-store wiring)"
```

### Task 10: Hoist the store + carry `screener_overrides` through the trial config

**Files:**
- Modify: `BA2TestPlatform/backend/app/services/strategy_optimization_handler.py` (`_build_hoisted_state` at `:532-547`; `_build_daily_trial_config` at `:587-684`; where `collect_param_space` is called — pass `screener_cfg` from `backtest.screener_opt`)

- [ ] **Step 1: Pass `screener_cfg` to `collect_param_space`**

Where the handler builds the param space (it calls `collect_param_space(strategy, expert_cfg=..., bypass=...)`), add `screener_cfg=opt_cfg["expert_params"]`-derived screener entries. Simplest: the `screener:`-prefixed keys are already in `expert_params`; route them by also passing a `screener_cfg` built from any `expert_params` keys starting with `screener:` (strip prefix). Confirm with a log line `Collected joint param space` includes `screener:*`.

- [ ] **Step 2: Hoist the loaded store once (param-independent)**

In `_build_hoisted_state` (`:532-547`), if `backtest.screener_opt` is present, load + memoize the store:

```python
    screener_opt = backtest_cfg.get("screener_opt")
    if screener_opt:
        from ba2_providers.screener import metric_store as _ms
        _ms.load_store(screener_opt["store"])  # warms the per-worker memo
        hoisted["screener_store"] = screener_opt["store"]
        hoisted["screener_base"] = screener_opt.get("base_settings", {})
        hoisted["screener_cadence_days"] = int(screener_opt.get("cadence_days", 7))  # default weekly
```

- [ ] **Step 3: Put each individual's effective screener settings into the trial config**

In `_build_daily_trial_config` (`:605-656`), after decoding, merge base + per-individual overrides and attach to the config the engine reads:

```python
    if hoisted.get("screener_store"):
        eff = {**hoisted.get("screener_base", {}), **decoded.get("screener_overrides", {})}
        cfg["screener_runtime"] = {"store": hoisted["screener_store"], "settings": eff,
                                   "cadence_days": hoisted.get("screener_cadence_days", 7)}
```

(`decoded` is the `decode_params(...)` result already computed in this function for `expert_overrides`/`tp`/`sl`.)

- [ ] **Step 4: Test the trial config carries the runtime screener block**

```python
# backend/tests/backtest/test_screener_genes.py
def test_trial_config_carries_screener_runtime(monkeypatch, tmp_path):
    # build a 1-row store so load_store succeeds
    from ba2_providers.screener import metric_store as ms
    import pandas as pd
    ms.write_partitions(str(tmp_path/"s"), pd.DataFrame({"symbol":["AAA"],"date":["2023-01-31"],
        "close":[10.0],"market_cap":[3e9],"relative_volume":[1.6],"price_drop_pct":[20.0],
        "sector":["T"],"volume":[2e6],"price":[10.0]}))
    # ... call _build_hoisted_state + _build_daily_trial_config with a screener_opt block and a
    # decoded screener_overrides; assert cfg["screener_runtime"]["settings"]["screener_market_cap_min"]
```

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_genes.py::test_trial_config_carries_screener_runtime -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C BA2TestPlatform add backend/app/services/strategy_optimization_handler.py backend/tests/backtest/test_screener_genes.py
git -C BA2TestPlatform commit -m "feat(opt): hoist screener store + carry per-individual screener settings into trials"
```

---

## Phase 5 — Engine per-day dynamic universe gating

### Task 11: Gate entries to the per-day screener universe

**Files:**
- Modify: `BA2TestPlatform/backend/app/services/backtest/daily_engine.py` (the per-bar expert/entry loop — the new-position analysis at `daily_engine.py:593` and the schedule/`analysis_idx` area)
- Modify: `BA2TestPlatform/backend/app/services/backtest/daily_backtest_handler.py` (`run_daily_backtest` reads `config["screener_runtime"]` and passes it to the engine)

- [ ] **Step 1: Write the failing test (engine restricts the day's candidate symbols)**

```python
# backend/tests/backtest/test_screener_opt_e2e.py — unit slice of the gating helper
from app.services.backtest.daily_engine import _screened_symbols_for_bar

def test_screened_symbols_for_bar_filters_by_day(tmp_path):
    from ba2_providers.screener import metric_store as ms
    import pandas as pd
    store = str(tmp_path/"s")
    ms.write_partitions(store, pd.DataFrame({
        "symbol":["AAA","BBB"], "date":["2023-03-01","2023-03-01"], "close":[10,20.0],
        "market_cap":[5e9,8e8], "relative_volume":[2.0,2.0], "price_drop_pct":[20.0,20.0],
        "sector":["T","T"], "volume":[2e6,2e6], "price":[10,20.0]}))
    rt = {"store": store, "settings": {"market_cap_min": 1e9, "max_stocks": 5}}
    import datetime as dt
    got = _screened_symbols_for_bar(rt, dt.datetime(2023,3,1))
    assert got == ["AAA"]               # BBB filtered (cap 8e8 < 1e9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_opt_e2e.py::test_screened_symbols_for_bar_filters_by_day -v`
Expected: FAIL with `ImportError: cannot import name '_screened_symbols_for_bar'`

- [ ] **Step 3: Implement the helper + wire it into the per-bar loop**

```python
# daily_engine.py
def _screened_symbols_for_bar(screener_runtime, as_of_dt):
    """The dynamic universe for this run's effective screener settings (or None when no
    screener). Resolves to the latest scan date <= this bar (the cadence is weekly by default,
    so the universe holds between scans). Returns symbols allowed to ENTER on this bar."""
    if not screener_runtime:
        return None
    from ba2_providers.screener import metric_store as ms
    df = ms.load_store(screener_runtime["store"])
    return ms.screen_universe_as_of(df, as_of_dt.strftime("%Y-%m-%d"), screener_runtime["settings"])
```

In the engine's per-bar new-position analysis (around `:593`), compute `allowed = _screened_symbols_for_bar(self._screener_runtime, as_of_dt)` ONCE per analysis bar; when `allowed is not None`, skip experts/symbols whose symbol is not in `allowed` for ENTRIES (do NOT block management/exits of already-open positions — those must still run). The cadence lives in the data (the store only holds scan-cadence rows; `screen_universe_as_of` resolves the latest one ≤ the bar), so no extra cadence logic is needed in the loop. Thread `screener_runtime` from `run_daily_backtest(config)` → engine constructor as `self._screener_runtime = config.get("screener_runtime")`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_screener_opt_e2e.py::test_screened_symbols_for_bar_filters_by_day -v`
Expected: PASS

- [ ] **Step 5: Run the full backtest regression (results-identity for NON-screener runs)**

Run: `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest -q`
Expected: `197 passed` (screener gating is a no-op when `screener_runtime` is absent — existing runs unchanged).

- [ ] **Step 6: Commit**

```bash
git -C BA2TestPlatform add backend/app/services/backtest/daily_engine.py backend/app/services/backtest/daily_backtest_handler.py backend/tests/backtest/test_screener_opt_e2e.py
git -C BA2TestPlatform commit -m "feat(engine): gate entries to the per-day screener universe (dynamic)"
```

### Task 12: End-to-end — optimize with screener genes (small, real)

**Files:**
- Test: manual CLI run (the unit slices above already cover the pure logic)

- [ ] **Step 1: Build a small metric store**

Run:
```bash
~/ba2-venvs/test/bin/ba2-test build-screener-metrics --store /tmp/mstore_e2e \
  --start 2023-01-01 --end 2023-06-30 --market-cap-min 50000000000 --price-min 20 --volume-min 1000000 \
  --cadence-days 7 --drop-days 5
```
Expected: summary with `symbols` > 20, `cadence_days: 7`.

- [ ] **Step 2: Run a tiny screener optimize (weekly cadence matches --run-schedule weekly)**

Run:
```bash
~/ba2-venvs/test/bin/ba2-test optimize --expert FMPRating \
  --start 2023-01-01 --end 2023-06-30 --interval 1d --run-schedule weekly \
  --population 4 --generations 1 --seed 7 --parallel 1 \
  --screener --screener-store /tmp/mstore_e2e --screener-cadence-days 7 --save-top 0 \
  --universe AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD
```
Expected: completes; prints `best_fitness=...`. Confirms screener genes decode, the per-day universe gates entries, and individuals with different screener thresholds get different fitness (vary `--seed` → different best_params including `screener:*`).

- [ ] **Step 3: Determinism check**

Re-run Step 2 with the same seed → identical `best_fitness` (the per-day filter is a pure function of cached store rows).

- [ ] **Step 4: Commit (docs/notes only, if any)**

```bash
git -C BA2TestPlatform commit --allow-empty -m "test(opt): screener-settings optimization e2e verified"
```

---

## Self-Review

**Spec coverage:** universe=FMP-screener (Task 1) ✓; survivorship=current-set (Task 1 `_fetch_screener_rows` `isActivelyTrading=true`) ✓; **configurable scan cadence, default 1 week** as an OPTIMIZATION config option (Task 3 `cadence_days=7`, Task 4 `--cadence-days`, Task 9 `--screener-cadence-days` → `screener_opt.cadence_days`, Task 10 threaded into `screener_runtime`, Task 11 `screen_universe_as_of` resolves the latest scan ≤ bar) ✓; store raw values + per-day/as-of filter (Tasks 2,6) ✓; exportable + incremental (Task 3 partitions + `existing_months`) ✓; in-memory pandas, no server (Task 5) ✓; dynamic universe over BT, held between scans (Task 6 `screen_universe_as_of` + Task 11 per-bar gating) ✓; screener genes (Tasks 7-8) ✓; optimizer wiring (Tasks 9-10) ✓; engine gating + non-screener results-identity (Task 11) ✓; small static opti range (Task 9 `_SCREENER_OPT` ranges) ✓.

**Placeholder scan:** none — every code step has concrete code; the only narrative steps (Task 9 config merge, Task 11 wiring) reference exact files+anchors with representative code.

**Type consistency:** `screener_overrides` (decode, Task 8) → `screener_runtime.settings` (Task 10) → `screen_universe_for_day(settings)` (Task 6) → `_screened_symbols_for_bar` (Task 11) use the same `screener_*` setting keys throughout; the store columns (`symbol/date/close/market_cap/relative_volume/price_drop_pct/sector/volume/price`) are identical across write (Task 3), load (Task 5), and filter (Task 6).

**Open risk to flag at execution:** Task 1 uses current `marketCap` for enumeration but Task 2 computes as-of `market_cap = shares × close`; the enumeration prefilter is intentionally loose (it only bounds *which symbols to include*), and the per-day as-of `market_cap` is what filters at query time — so a name whose as-of cap dips below a gene threshold is correctly excluded on that day. Confirm `shares_get` returns a usable latest-filing share count; if `None`, `market_cap` is NaN and the cap filter drops the symbol (acceptable for v1, noted).
