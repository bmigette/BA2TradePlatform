# Backtest Platform — Phase 3 (Screener History + Survivorship-Free Universe + Grouped/Labeled Cache) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the stock screener **one provider class** that honours the `as_of` contract (design §4): `as_of=None` ⇒ the live FMP `/stock-screener` endpoint (unchanged behaviour); `as_of=<date>` ⇒ a **reconstructed historical screen** that bypasses the live endpoint, builds a **survivorship-free** universe for that date, reconstructs each symbol's as-of metrics (market-cap = shares × as-of close, volume, price, float, fundamentals, Weinstein Stage-2, price-drop) from the Phase-2 `as_of` providers, applies the **same** post-fetch filter functions in memory, and emits the **identical `_normalise_result` dict shape** so the downstream `StockScreener` pipeline is byte-identical to live. Per scan date the screener returns a **grouped, labeled** survivor set keyed `(symbol, scan_date, screen_config_hash)` and persisted to a new screener-history cache, so a backtest sweep over scan dates becomes a cheap cache replay and the Phase-4 engine's per-rebalance-bar universe is "that date's set". This enables FactorRanker and every screener-driven expert (incl. the Weinstein Stage-2 filter) to run in the daily engine.

**Architecture:** The screener has **one filter LOGIC** that never forks. The only fork is the **fetch source**, isolated to a single seam: `StockScreener.screen()` line 144 `candidates = screener.screen_stocks(filters)`. Phase 3 threads an optional `as_of: Optional[datetime] = None` into `ScreenerProviderInterface.screen_stocks` (default `None` = today = zero breakage), re-anchors the two `datetime.now()`-based windows inside `StockScreener._fetch_history_bulk` / `_fetch_quotes_chunked` (lines 352–354) so `to_date=as_of`, and sources Stage-2 enrichment from as-of provider data. The historical branch is implemented as a **new `FMPHistoricalScreenerProvider`** registered under `SCREENER_PROVIDERS["fmp_historical"]`, so `FMPScreenerProvider` (the live path) stays untouched and the golden-test regression for `as_of=None` is trivially preserved. All of `_build_provider_filters`, `_enrich_with_rvol`, `_filter_by_weinstein_stage2`, `_filter_by_price_drop`, `_rank`, `weinstein.classify_weinstein_stage` are reused unchanged — only their **data inputs** swap from live to as-of.

**Tech Stack:** Python ≥3.11, the extracted `ba2_providers` package (Phase 0) + its Phase-2 `as_of` `get()` providers + effective-date native cache, `ba2_common` (`weinstein`, `config`, `logger`, screener interface), SQLite (the new screener-history index table, generalizing the Phase-2 `provider_cache` pattern), pandas/requests, pytest. The work lands in **`BA2TestPlatform`** (the backtest/ML host that consumes the packages) plus edits to the **`ba2_providers`** package; **BA2TradePlatform stays untouched** (its migration is Phase 6).

---

## Source of truth & repo locations

- Packages (consumed, edited where noted): `BA2TradeProviders/ba2_providers/` (screener lives here after Phase 0: `ba2_providers/StockScreener.py`, `ba2_providers/screener/FMPScreenerProvider.py`, `ba2_providers/screener/__init__.py`, `ba2_providers/__init__.py` registry; `ba2_providers/weinstein.py`? — **no**: `weinstein` is in `ba2_common.core.weinstein`, imported by `StockScreener` as `from ba2_common.core.weinstein import classify_weinstein_stage`). `ScreenerProviderInterface` is `ba2_common/core/interfaces/ScreenerProviderInterface.py`.
- Host (new modules + tests + UI wiring): `BA2TestPlatform/` (siblings under `…/dev/BA2/`: `BA2TestPlatform`, `BA2TradeCommon`, `BA2TradeProviders`, `BA2TradeExperts`, `BA2TradePlatform`).
- Live source tree (read-only reference for current screener behaviour): `BA2TradePlatform/ba2_trade_platform/core/StockScreener.py`, `…/modules/dataproviders/screener/FMPScreenerProvider.py`, `…/core/interfaces/ScreenerProviderInterface.py`, `…/core/weinstein.py`.
- Derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§4, §6 Phase 3) and `docs/FMP_BACKTEST_FEASIBILITY.md` (survivorship/point-in-time warnings; endpoint probe results — all FMP universe endpoints confirmed available on our key).

> **Re-plan checkpoint (Phase-0 output):** Confirm the exact post-Phase-0 location of the screener modules before editing. Phase 0's package layout (phase0-plan §"Package layout") flattens `modules/dataproviders/*` → `ba2_providers/*` and copies `core/StockScreener.py` → `ba2_providers/StockScreener.py` (Task 8 Step 1). If Phase 0 instead kept `StockScreener` in `ba2_common.core`, retarget every `ba2_providers/StockScreener.py` path below to `ba2_common/core/StockScreener.py` and adjust the screener provider import accordingly. The codemod made `StockScreener`'s `from .weinstein import classify_weinstein_stage` into `from ba2_common.core.weinstein import classify_weinstein_stage` and its `from ..modules.dataproviders import get_provider` into `from ba2_providers import get_provider`.

> **Re-plan checkpoint (Phase-2 output):** Confirm the **exact `as_of` `get()` signatures and native-cache read path** produced by Phase 2 before wiring the historical provider. This plan calls the Phase-2 providers through `get_provider(category, name)` + the uniform `get(symbol, as_of=..., lookback=..., format_type='dict')` wrapper (design §3, SHARED CONTRACTS `provider_asof`). Pin: (a) the OHLCV provider's `get(symbol, as_of=<date>, lookback_days=..., format_type='dict')` return shape (the close-at-as_of source), (b) the fundamentals-details provider's `as_of`-aware shares-outstanding access (`weightedAverageShsOut` via `get_income_statement(..., end_date=as_of)` per Phase-2's statements lookahead fix on `fillingDate`/`acceptedDate`), and (c) whether Phase 2 exposes `price_at_date`. The standardized as-of price source for Phase 3 is **OHLCV close on (or last trading day ≤) as_of** (resolves open-question #3 below toward "close").

## Decisions taken (confirm before execution)

These resolve forks the design + shared-contract open-questions surfaced. Override any at approval time.

1. **New `FMPHistoricalScreenerProvider`, not a branch inside `FMPScreenerProvider` (Model A).** Keeps the live class byte-identical (zero golden-test risk for `as_of=None`), gives the historical branch its own file/tests, and registers cleanly under `SCREENER_PROVIDERS["fmp_historical"]`. `StockScreener` selects it when `as_of` is set (or via a new `screener_provider="fmp_historical"` resolution) — see Task 3. *Alternative:* one provider with an `if as_of:` fork (less code, but couples the two paths and risks live drift).
2. **As-of price source = OHLCV close on (or last trading day ≤) `as_of`** for ALL screener metrics, via the Phase-2 OHLCV `get()` (resolves open-question #3). Do NOT reuse FMPSenateTraderWeight's open-price `/historical-price-full?from=&to=` helper — one source so all experts agree, and close is the natural daily-bar anchor.
3. **Market-cap reconstruction = shares-outstanding(as_of) × close(as_of).** Prefer FMP's dated `/api/v3/historical-market-capitalization` when it covers the date; else `shares-from-latest-report ≤ as_of` (`weightedAverageShsOut` from the Phase-2 as-of income statement, same as-of-report discipline as `get_financial_ratios`) × close. Record which path was used in the cache row (`market_cap_source`) for auditability.
4. **Float is an approximation in backtest (resolves open-question #8).** FMP serves only the *current* `floatShares`; use it as a static proxy for `float_min`/`float_max` historical filters (documented limitation, flagged per-row `float_approx=True`). Do NOT attempt `/api/v4/shares_float` history unless a probe in Task 2 confirms our key returns dated rows; if it does, prefer it and set `float_approx=False`.
5. **Two universe modes, both survivorship-free, both terminating in the identical `_normalise_result` dict** (design §4): `broad` = `available-traded/list` ∪ `delisted-companies` with `ipoDate ≤ D ≤ (delistedDate or +∞)`; `sp500`/`nasdaq` = dated constituents replayed from the historical change log. `universe_mode` is a new screener setting (default `broad`).
6. **`screen_config_hash` = sha256 of canonical-JSON of the effective `_settings` subset that affects results** (the 14 `screener_*` threshold keys + `screener_weinstein_stage2_only` + `screener_sort_metric` + `universe_mode`), computed from `StockScreener._settings` **after** type coercion (lines 86–108) so defaults are baked in. `group/expert` is a human label (e.g. `FactorRanker`, `PennyMomentumTrader`, `Weinstein-S2`) passed by the caller; the **machine identity is the hash** — different criteria never mix, same criteria replay.
7. **Cache lives in `BA2TestPlatform`, generalizing the Phase-2 `provider_cache` SQLite pattern** (a new `screener_history` table). One row per surviving `(symbol, scan_date, screen_config_hash)`. The Phase-2 native-cache infra (per-key locks, atomic writes, range-coverage check) is reused, not reinvented.

## The fetch/process seam (the one line that forks)

```
StockScreener.screen()  → candidates = screener.screen_stocks(filters [, as_of])   # line 144
                          ▲ as_of=None  → FMPScreenerProvider (live FMP /stock-screener)        [UNCHANGED]
                          ▲ as_of=<date>→ FMPHistoricalScreenerProvider (reconstructed universe)  [NEW]
                          │
            then IDENTICAL post-fetch pipeline for BOTH:
              _enrich_with_rvol → _filter_by_weinstein_stage2 → _rank → _filter_by_price_drop
                          (only DATA INPUTS swap live↔as-of; LOGIC never forks)
```

`_fetch_quotes_chunked` / `_fetch_history_bulk` (the two `datetime.now()` windows, lines 352–354) are re-anchored to `to_date=as_of` so the enrichment/Weinstein/price-drop stages read bars truncated to `as_of` — already point-in-time-safe (closes ≤ as_of feed `classify_weinstein_stage` and the peak/drop math).

## Acceptance gate for Phase 3

A historical screen at a past date reproduces the live filter logic, surfaces delisted names, and caches once. Concretely (verified by Task 8):

1. **Live-equivalence (golden regression):** `StockScreener(settings, as_of=None).screen()` produces output **byte-equal** to the pre-Phase-3 live FMP screener for a fixed `settings` (the `_normalise_result` dict shape and pipeline are unchanged; `FMPScreenerProvider` untouched).
2. **Historical filter-logic equivalence:** for a fixed `(scan_date, settings)` with mocked as-of providers, `FMPHistoricalScreenerProvider.screen_stocks(filters, as_of=scan_date)` returns dicts of the **same shape and keys** as the live provider, and the full `StockScreener` pipeline applies the **same** `_enrich_with_rvol`/`_filter_by_weinstein_stage2`/`_rank`/`_filter_by_price_drop` functions over them (the survivor set is reproducible).
3. **Survivorship test:** a symbol that delisted **before** `today` but **traded on `scan_date`** (`ipoDate ≤ scan_date ≤ delistedDate`) **appears** in the `scan_date` universe and **disappears** from a universe after its `delistedDate`. A fixed-current-universe run (live screener replayed over the past) would omit it — the delta proves survivorship-bias removal.
4. **Cache-once:** the first `screen()` at `(scan_date, screen_config_hash)` runs the as-of pipeline and writes survivor rows; the second call **replays from cache** with no provider fetches (assert fetch-count == 0 on the second pass) and returns an identical survivor list; a config change yields a **distinct hash namespace** (different rows, no mixing).
5. **Phase-1 golden re-verify (regression):** the Phase-1 golden test (`run_analysis` == `analyze_as_of(now)` for every backtestable expert) still passes — Phase 3 added an optional `as_of` param defaulting to `None`, changing no live decision logic.

---

## Task 1: Extend `ScreenerProviderInterface` with the optional `as_of` seam

**Files:**
- Edit `BA2TradeProviders/ba2_providers/` interface import target → actually `BA2TradeCommon/ba2_common/core/interfaces/ScreenerProviderInterface.py` (the interface lives in `ba2_common` after Phase 0).
- Edit `BA2TradeProviders/ba2_providers/screener/FMPScreenerProvider.py` (accept + ignore `as_of`, documented live-only).
- Test: `BA2TestPlatform/tests/test_screener_interface_asof.py`

> **Re-plan checkpoint:** confirm the interface file path. Per phase0-plan §"Package layout", `ScreenerProviderInterface.py` is copied into `ba2_common/core/interfaces/`. If your Phase-0 run left it elsewhere, retarget Step 1.

- [ ] **Step 1: Add the optional `as_of` param to `screen_stocks`**

Edit `BA2TradeCommon/ba2_common/core/interfaces/ScreenerProviderInterface.py`. Change the abstract signature (current line 31) and document the contract:

```python
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, List, Optional


class ScreenerProviderInterface(ABC):

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return the provider name (e.g., 'fmp', 'fmp_historical')."""
        ...

    @abstractmethod
    def screen_stocks(
        self,
        filters: Dict[str, Any],
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Screen stocks matching filters.

        as_of:
            None  -> live screen (today's listings) — the existing behaviour.
            <date> -> point-in-time reconstructed screen: build a survivorship-free
                      universe for `as_of`, reconstruct as-of metrics, apply the SAME
                      numeric/exchange thresholds in memory, and return the IDENTICAL
                      normalised dict shape so the downstream pipeline is unchanged.

        Returns: list of dicts with keys: symbol, company_name, price, volume,
            market_cap, sector, industry, exchange, beta, is_actively_trading,
            country, float_shares.
        """
        ...

    @abstractmethod
    def validate_config(self) -> bool:
        ...
```

> The default `as_of=None` means every existing caller is source-compatible (zero breakage) — this is the no-regression lever for gate item 5.

- [ ] **Step 2: Make `FMPScreenerProvider` accept-and-ignore `as_of` (documented live-only)**

Edit `BA2TradeProviders/ba2_providers/screener/FMPScreenerProvider.py`. Widen the signature to match the interface; keep behaviour identical for `as_of=None`, and **raise** (loud, not silent) if `as_of` is set — the live screener has no temporal param (SHARED CONTRACTS `per_category_mapping.Screener`: "live-only, as_of-ignored"):

```python
from datetime import datetime
from typing import Optional

    def screen_stocks(
        self,
        filters: Dict[str, Any],
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        if as_of is not None:
            raise ValueError(
                "FMPScreenerProvider is live-only (no temporal param). "
                "Use the 'fmp_historical' provider for as_of reconstruction."
            )
        # ... unchanged body (build params, fmp_http_get, normalise) ...
```

Everything below the guard (the `_build_params`/`fmp_http_get`/`_normalise_result` body) stays byte-identical.

- [ ] **Step 3: Write the interface/live-provider as_of tests**

`BA2TestPlatform/tests/test_screener_interface_asof.py`:

```python
import pytest
from datetime import datetime, timezone


def test_interface_screen_stocks_accepts_as_of_kw():
    import inspect
    from ba2_common.core.interfaces.ScreenerProviderInterface import ScreenerProviderInterface
    sig = inspect.signature(ScreenerProviderInterface.screen_stocks)
    assert "as_of" in sig.parameters
    assert sig.parameters["as_of"].default is None


def test_live_provider_rejects_as_of():
    from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider
    p = FMPScreenerProvider()
    with pytest.raises(ValueError):
        p.screen_stocks({}, as_of=datetime(2022, 1, 3, tzinfo=timezone.utc))
```

- [ ] **Step 4: Run the tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest tests/test_screener_interface_asof.py -v
```
Expected: PASS (no network — `FMPScreenerProvider()` construction only reads the API key from settings; the `as_of` branch raises before any HTTP). (Per `backend/CLAUDE.md`, use the backend venv — never system python; cwd stays at the `BA2TestPlatform` root so the `backend.app.services` imports resolve.)

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): add optional as_of to ScreenerProviderInterface.screen_stocks"
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "feat(providers): FMPScreenerProvider widens screen_stocks(as_of) signature (live-only guard)"
```

---

## Task 2: Survivorship-free universe builder

**Files:**
- Create `BA2TradeProviders/ba2_providers/screener/universe.py` (the universe module: broad + index-scoped, both survivorship-free)
- Test: `BA2TradeProviders/tests/test_universe.py`

The universe builder answers one question: **"which symbols traded on date `D`?"** It is the input the historical screener reconstructs metrics for. All FMP endpoints below were probed live against our key (FMP feasibility doc: "every endpoint returned data — none premium-gated").

- [ ] **Step 1: Probe the FMP universe endpoints (confirm coverage on our key)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform
python - <<'PY'
import requests
from ba2_common.config import get_app_setting   # confirm import path post-Phase-0
k = get_app_setting("FMP_API_KEY")
base = "https://financialmodelingprep.com/api/v3"
for name, url in [
    ("available-traded", f"{base}/available-traded/list"),
    ("delisted (p0)",    f"{base}/delisted-companies?page=0"),
    ("sp500",            f"{base}/sp500_constituent"),
    ("sp500-hist",       f"{base}/historical/sp500_constituent"),
    ("nasdaq",           f"{base}/nasdaq_constituent"),
    ("nasdaq-hist",      f"{base}/historical/nasdaq_constituent"),
    ("hist-mktcap AAPL", f"{base}/historical-market-capitalization/AAPL?limit=5"),
    ("shares-float AAPL","https://financialmodelingprep.com/api/v4/shares_float?symbol=AAPL"),
]:
    try:
        r = requests.get(url, params={"apikey": k}, timeout=20)
        j = r.json()
        n = len(j) if isinstance(j, list) else "obj"
        keys = list(j[0].keys())[:8] if isinstance(j, list) and j and isinstance(j[0], dict) else j
        print(f"{name:18s} status={r.status_code} rows={n} keys={keys}")
    except Exception as e:
        print(f"{name:18s} ERROR {e}")
PY
```
Expected: each prints a non-empty row count and the field names. **Record the actual `ipoDate`/`delistedDate` field names** from `available-traded`/`delisted-companies` and the `date`/`symbol`/`addedSecurity`/`removedTicker` (or equivalents) from the historical-constituent change logs — Steps 2–4 assume these; reconcile if FMP's field names differ. **Confirm whether `shares_float` returns dated history** (decides Decision 4 / open-question #8): if it returns a single current row, keep float as a static proxy.

- [ ] **Step 2: Write `universe.py` — broad survivorship-free set**

`BA2TradeProviders/ba2_providers/screener/universe.py`:

```python
"""Survivorship-free historical universe construction for the as-of screener.

Two modes, both terminating in a plain list[str] of symbols tradable on a date:
  - broad: available-traded/list UNION delisted-companies, filtered per date by
           the symbol's [ipoDate, delistedDate] lifecycle window.
  - index-scoped (sp500 / nasdaq): dated constituents replayed from the historical
           add/remove change log walked backward from today to the as-of date.

All fetches go through fmp_http_get (retry + FMP 200-error-dict handling). The full
lists are bounded and small (~10-12k symbols, ~tens of change events) so we fetch
once and slice in memory — the universe is computed once per backtest range.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from ba2_common.config import get_app_setting
from ba2_common.logger import logger
from ..fmp_common import fmp_http_get, FMPError   # post-Phase-0 path: ba2_providers/fmp_common.py

_BASE = "https://financialmodelingprep.com/api/v3"


def _api_key() -> str:
    k = get_app_setting("FMP_API_KEY")
    if not k:
        raise ValueError("FMP_API_KEY not configured — required for universe reconstruction")
    return k


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_lifecycle_map() -> Dict[str, Tuple[Optional[datetime], Optional[datetime]]]:
    """Return {symbol: (ipo_date, delisted_date)} merging active + delisted lists.

    Active symbols have delisted_date=None (treated as +inf). Field names per the
    Task-2 Step-1 probe: active list 'symbol'/'ipoDate' (ipoDate may be absent ->
    fall back to first historical bar in the caller); delisted list
    'symbol'/'ipoDate'/'delistedDate'.
    """
    key = _api_key()
    lifecycle: Dict[str, Tuple[Optional[datetime], Optional[datetime]]] = {}

    # Active (currently traded)
    resp = fmp_http_get(f"{_BASE}/available-traded/list",
                        params={"apikey": key}, endpoint="available-traded", timeout=30)
    for row in (resp.json() or []):
        if isinstance(row, dict) and row.get("symbol"):
            lifecycle[row["symbol"].upper()] = (_parse_date(row.get("ipoDate")), None)

    # Delisted (paginated)
    page = 0
    while True:
        r = fmp_http_get(f"{_BASE}/delisted-companies",
                         params={"apikey": key, "page": page}, endpoint="delisted-companies", timeout=30)
        rows = r.json() or []
        if not rows or not isinstance(rows, list):
            break
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                lifecycle[row["symbol"].upper()] = (
                    _parse_date(row.get("ipoDate")),
                    _parse_date(row.get("delistedDate")),
                )
        if len(rows) < 100:   # FMP page size; last page reached
            break
        page += 1

    logger.info(f"universe: lifecycle map built for {len(lifecycle)} symbols")
    return lifecycle


def broad_universe(as_of: datetime,
                   lifecycle: Optional[Dict[str, Tuple]] = None) -> List[str]:
    """Symbols tradable on `as_of`: ipoDate <= as_of <= (delistedDate or +inf)."""
    lifecycle = lifecycle if lifecycle is not None else fetch_lifecycle_map()
    out: List[str] = []
    for sym, (ipo, delisted) in lifecycle.items():
        if ipo is not None and ipo > as_of:
            continue                          # not yet public on as_of
        if delisted is not None and delisted < as_of:
            continue                          # already delisted before as_of
        out.append(sym)
    logger.info(f"universe(broad): {len(out)} symbols tradable on {as_of.date()}")
    return out


def index_universe(index: str, as_of: datetime) -> List[str]:
    """Reconstruct dated index membership by replaying the historical change log.

    index: 'sp500' or 'nasdaq'. Start from the CURRENT constituent set, then walk
    the dated add/remove events backward from today to `as_of`, inverting each:
      - an 'addedSecurity' event after as_of  -> the symbol was NOT a member on as_of -> remove
      - a 'removedTicker' event after as_of   -> the symbol WAS a member on as_of   -> add back
    Field names per the Task-2 Step-1 probe; reconcile if FMP differs.
    """
    key = _api_key()
    cur_url = f"{_BASE}/{index}_constituent"
    hist_url = f"{_BASE}/historical/{index}_constituent"

    current = {row["symbol"].upper()
               for row in (fmp_http_get(cur_url, params={"apikey": key},
                                        endpoint=f"{index}_constituent", timeout=30).json() or [])
               if isinstance(row, dict) and row.get("symbol")}

    members: Set[str] = set(current)
    changes = fmp_http_get(hist_url, params={"apikey": key},
                           endpoint=f"{index}_historical", timeout=30).json() or []
    for ev in changes:                         # FMP returns newest-first
        ev_date = _parse_date(ev.get("date") or ev.get("dateAdded"))
        if ev_date is None or ev_date <= as_of:
            continue                           # only invert events AFTER as_of
        added = (ev.get("symbol") or ev.get("addedSecurity") or "").upper()
        removed = (ev.get("removedTicker") or "").upper()
        if added:
            members.discard(added)             # was added after as_of -> not a member then
        if removed:
            members.add(removed)               # was removed after as_of -> a member then
    logger.info(f"universe({index}): {len(members)} members on {as_of.date()}")
    return sorted(members)
```

- [ ] **Step 3: Write the universe tests (logic, mocked fetches)**

`BA2TradeProviders/tests/test_universe.py`:

```python
from datetime import datetime, timezone
import ba2_providers.screener.universe as U

D = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def test_broad_universe_lifecycle_window():
    lifecycle = {
        "ALIVE":   (D("2010-01-01"), None),          # active, IPO'd long ago
        "DEAD":    (D("2015-01-01"), D("2021-06-30")),  # delisted before today
        "NEWBIE":  (D("2024-01-01"), None),          # IPO'd after the scan date
    }
    on = U.broad_universe(D("2020-06-30"), lifecycle=lifecycle)
    assert "ALIVE" in on
    assert "DEAD" in on          # survivorship: traded on 2020-06-30, delisted 2021 -> present
    assert "NEWBIE" not in on    # not yet public


def test_broad_universe_excludes_delisted_after_death():
    lifecycle = {"DEAD": (D("2015-01-01"), D("2021-06-30"))}
    after = U.broad_universe(D("2022-01-03"), lifecycle=lifecycle)
    assert "DEAD" not in after   # gone after its delistedDate


def test_index_universe_replays_changelog(monkeypatch):
    # current members = {AAA, BBB}; after as_of, CCC was added and DDD was removed.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        class R:
            def __init__(self, j): self._j = j
            def json(self): return self._j
        if url.endswith("_constituent") and "historical" not in url:
            return R([{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}])
        return R([
            {"date": "2023-03-01", "symbol": "CCC", "removedTicker": ""},    # CCC added 2023 (after as_of)
            {"date": "2023-03-01", "symbol": "", "removedTicker": "DDD"},    # DDD removed 2023
        ])
    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("sp500", D("2022-01-03"))
    assert "CCC" not in members  # added after as_of -> not a member then
    assert "DDD" in members      # removed after as_of -> a member then
    assert "AAA" in members and "BBB" in members
```

- [ ] **Step 4: Run universe tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && python -m pytest tests/test_universe.py -v
```
Expected: PASS (pure list logic; fetches mocked).

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "feat(providers): survivorship-free universe builder (broad + dated index constituents)"
```

---

## Task 3: `FMPHistoricalScreenerProvider` (reconstruct as-of metrics, reuse the filter shape)

**Files:**
- Create `BA2TradeProviders/ba2_providers/screener/FMPHistoricalScreenerProvider.py`
- Edit `BA2TradeProviders/ba2_providers/screener/__init__.py` (export the new class)
- Edit `BA2TradeProviders/ba2_providers/__init__.py` (register `SCREENER_PROVIDERS["fmp_historical"]`)
- Test: `BA2TradeProviders/tests/test_historical_screener.py`

The provider's job: given `filters` + `as_of`, build the universe (Task 2), reconstruct per-symbol as-of metrics from the **Phase-2 `as_of` providers**, apply the **same numeric/exchange thresholds** the live provider applies, and return the **identical `_normalise_result` dict list**. The heavy client-side filters (RVOL, Weinstein, price-drop) stay in `StockScreener` — this provider does the Stage-1 equivalent (the part the live FMP endpoint does server-side) over a historical universe.

> **Re-plan checkpoint:** the metric reconstruction calls Phase-2 providers via `get_provider(...).get(...)`. Confirm the exact OHLCV and fundamentals-details `as_of` `get()` signatures from the Phase-2 deliverable and adjust the `_close_at` / `_shares_at` helpers below. The shapes here follow SHARED CONTRACTS `provider_asof` (OHLCV `as_of→end_date`, `lookback→lookback_days`; fundamentals statements `as_of→end_date`, `lookback→lookback_periods`, effective_date on `fillingDate`/`acceptedDate`).

- [ ] **Step 1: Write the historical provider**

`BA2TradeProviders/ba2_providers/screener/FMPHistoricalScreenerProvider.py`:

```python
"""Point-in-time stock screener: reconstruct a historical screen as-of a past date.

as_of=None is NOT supported here (use FMPScreenerProvider for live). This class
implements ScreenerProviderInterface.screen_stocks(filters, as_of=<date>) by:
  1. building a survivorship-free universe for as_of (universe.py),
  2. reconstructing each symbol's as-of price/market-cap/volume/sector/float,
  3. applying the SAME numeric/exchange thresholds the live FMP /stock-screener
     applies server-side (price/volume/market_cap/float/exchange),
  4. emitting the IDENTICAL normalised dict so StockScreener's downstream pipeline
     (RVOL enrich, Weinstein, rank, price-drop) runs unchanged.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces.ScreenerProviderInterface import ScreenerProviderInterface
from ba2_common.config import get_app_setting
from ba2_common.logger import logger
from .universe import broad_universe, index_universe, fetch_lifecycle_map


class FMPHistoricalScreenerProvider(ScreenerProviderInterface):
    """Reconstructed historical screener (survivorship-free)."""

    def __init__(self, universe_mode: str = "broad"):
        self.api_key = get_app_setting("FMP_API_KEY")
        self.universe_mode = universe_mode   # 'broad' | 'sp500' | 'nasdaq'

    def get_provider_name(self) -> str:
        return "fmp_historical"

    def validate_config(self) -> bool:
        return bool(self.api_key)

    # -- as-of metric helpers (route through Phase-2 as_of providers) --

    def _ohlcv(self):
        from ba2_providers import get_provider
        return get_provider("ohlcv", "fmp")   # confirm provider key in Phase-2 registry

    def _details(self):
        from ba2_providers import get_provider
        return get_provider("fundamentals_details", "fmp")

    def _close_at(self, symbol: str, as_of: datetime) -> Optional[float]:
        """Close on (or last trading day <=) as_of via the Phase-2 OHLCV get()."""
        try:
            res = self._ohlcv().get(symbol, as_of=as_of, lookback_days=10, format_type="dict")
            bars = (res or {}).get("bars") or res or []   # reconcile shape with Phase 2
            if not bars:
                return None
            last = bars[-1]
            return float(last.get("close") if isinstance(last, dict) else last[4])
        except Exception as e:
            logger.debug(f"hist-screener: no close for {symbol}@{as_of.date()}: {e}")
            return None

    def _avg_volume_at(self, symbol: str, as_of: datetime, window: int = 20) -> float:
        try:
            res = self._ohlcv().get(symbol, as_of=as_of, lookback_days=window + 5, format_type="dict")
            bars = (res or {}).get("bars") or res or []
            vols = [float(b.get("volume", 0)) for b in bars[-window:]] if bars else []
            return round(sum(vols) / len(vols), 2) if vols else 0.0
        except Exception:
            return 0.0

    def _shares_at(self, symbol: str, as_of: datetime) -> Optional[float]:
        """Shares outstanding from the latest income statement with report date <= as_of
        (weightedAverageShsOut), per the as-of-report discipline of get_financial_ratios."""
        try:
            stmt = self._details().get_income_statement(
                symbol, end_date=as_of, lookback_periods=1, format_type="dict")
            rows = (stmt or {}).get("statements") or stmt or []
            if not rows:
                return None
            return float(rows[0].get("weighted_average_shares_outstanding") or 0) or None
        except Exception as e:
            logger.debug(f"hist-screener: no shares for {symbol}@{as_of.date()}: {e}")
            return None

    def _market_cap_at(self, symbol: str, as_of: datetime, close: Optional[float]) -> Optional[float]:
        # Prefer FMP dated historical-market-capitalization; else shares x close.
        try:
            from ..fmp_common import fmp_http_get
            url = f"https://financialmodelingprep.com/api/v3/historical-market-capitalization/{symbol}"
            to = as_of.strftime("%Y-%m-%d")
            r = fmp_http_get(url, params={"apikey": self.api_key, "from": to, "to": to, "limit": 5},
                             endpoint="historical-market-cap", timeout=20)
            rows = r.json() or []
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                mc = rows[0].get("marketCap")
                if mc:
                    return float(mc)
        except Exception:
            pass
        shares = self._shares_at(symbol, as_of)
        if shares and close:
            return shares * close
        return None

    def screen_stocks(self, filters: Dict[str, Any],
                      as_of: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if as_of is None:
            raise ValueError("FMPHistoricalScreenerProvider requires as_of (use 'fmp' for live)")
        if not self.validate_config():
            logger.error("FMP API key not configured for historical screener")
            return []

        if self.universe_mode in ("sp500", "nasdaq"):
            symbols = index_universe(self.universe_mode, as_of)
        else:
            symbols = broad_universe(as_of, lifecycle=fetch_lifecycle_map())

        price_min = filters.get("price_min"); price_max = filters.get("price_max")
        vol_min = filters.get("volume_min")
        mcap_min = filters.get("market_cap_min"); mcap_max = filters.get("market_cap_max")
        float_max = filters.get("float_max")
        exchanges = set(filters.get("exchanges") or [])
        limit = filters.get("limit") or 10_000

        out: List[Dict[str, Any]] = []
        for sym in symbols:
            close = self._close_at(sym, as_of)
            if close is None or close <= 0:
                continue
            if price_min and close < price_min:   continue
            if price_max and close > price_max:    continue
            mcap = self._market_cap_at(sym, as_of, close)
            if mcap_min and (mcap or 0) < mcap_min: continue
            if mcap_max and mcap and mcap > mcap_max: continue
            avg_vol = self._avg_volume_at(sym, as_of)
            if vol_min and avg_vol < vol_min:      continue
            # float: current floatShares proxy (documented approximation, Decision 4)
            float_shares = None   # filled by RVOL enrichment downstream if available
            out.append(self._normalise(sym, close, avg_vol, mcap, float_shares))
            if len(out) >= limit:
                break
        logger.info(f"hist-screener: {len(out)} candidates @ {as_of.date()} "
                    f"(universe={self.universe_mode}, scanned {len(symbols)})")
        return out

    @staticmethod
    def _normalise(symbol, price, volume, market_cap, float_shares) -> Dict[str, Any]:
        """SAME shape/keys as FMPScreenerProvider._normalise_result so the pipeline
        is unchanged. sector/industry/exchange/beta/country are best-effort as-of
        (filled by enrichment); exchange restriction is enforced via the live quote
        enrichment that runs downstream, OR add a dated profile lookup here if needed."""
        return {
            "symbol": symbol,
            "company_name": None,
            "price": price,
            "volume": volume,
            "market_cap": market_cap,
            "sector": None,
            "industry": None,
            "exchange": None,
            "beta": None,
            "is_actively_trading": None,
            "country": None,
            "float_shares": float_shares,
        }
```

> **Note on exchange filtering:** the live provider restricts to NASDAQ/NYSE/AMEX server-side. For the historical path, exchange is reconstructed from a dated `/profile` lookup if available, else enforced downstream by the RVOL quote enrichment (US-only quotes). Confirm during execution which is cheaper; if exchange is unavailable as-of, document it as a minor approximation (the broad universe is already US-listed via `available-traded`).

- [ ] **Step 2: Register the provider**

Edit `BA2TradeProviders/ba2_providers/screener/__init__.py`:

```python
from .FMPScreenerProvider import FMPScreenerProvider
from .FMPHistoricalScreenerProvider import FMPHistoricalScreenerProvider

__all__ = ["FMPScreenerProvider", "FMPHistoricalScreenerProvider"]
```

Edit `BA2TradeProviders/ba2_providers/__init__.py` — the screener import + registry:

```python
from .screener import FMPScreenerProvider, FMPHistoricalScreenerProvider
# ...
SCREENER_PROVIDERS: Dict[str, Type[ScreenerProviderInterface]] = {
    "fmp": FMPScreenerProvider,
    "fmp_historical": FMPHistoricalScreenerProvider,
}
```

> `FMPHistoricalScreenerProvider.__init__` takes `universe_mode`; `get_provider("screener", "fmp_historical", universe_mode="sp500")` flows through the registry's `**kwargs` (the `get_provider` factory already forwards `**kwargs` to the constructor — confirmed in the source registry).

- [ ] **Step 3: Write the historical-provider tests (mocked as-of providers)**

`BA2TradeProviders/tests/test_historical_screener.py`:

```python
from datetime import datetime, timezone
import pytest
import ba2_providers.screener.FMPHistoricalScreenerProvider as H

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)


def _patch(monkeypatch, prov, *, universe, closes, mcaps, vols):
    monkeypatch.setattr(prov, "_close_at", lambda s, a: closes.get(s))
    monkeypatch.setattr(prov, "_market_cap_at", lambda s, a, c: mcaps.get(s))
    monkeypatch.setattr(prov, "_avg_volume_at", lambda s, a, window=20: vols.get(s, 0.0))
    monkeypatch.setattr(H, "fetch_lifecycle_map", lambda: {})
    monkeypatch.setattr(H, "broad_universe", lambda a, lifecycle=None: universe)


def test_historical_screen_applies_thresholds(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(monkeypatch, p,
           universe=["KEEP", "CHEAP", "SMALL", "DEAD"],
           closes={"KEEP": 50.0, "CHEAP": 5.0, "SMALL": 60.0, "DEAD": 40.0},
           mcaps={"KEEP": 5e9, "CHEAP": 9e9, "SMALL": 1e8, "DEAD": 3e9},
           vols={"KEEP": 1_000_000, "CHEAP": 2_000_000, "SMALL": 800_000, "DEAD": 700_000})
    filters = {"price_min": 20.0, "market_cap_min": 1_000_000_000,
               "volume_min": 500_000, "exchanges": ["NASDAQ", "NYSE", "AMEX"], "limit": 10_000}
    res = p.screen_stocks(filters, as_of=AS_OF)
    syms = {r["symbol"] for r in res}
    assert "KEEP" in syms          # passes all
    assert "DEAD" in syms          # survivorship: present on as_of (universe contains it)
    assert "CHEAP" not in syms     # price 5 < 20
    assert "SMALL" not in syms     # mcap 1e8 < 1e9


def test_historical_normalised_shape_matches_live():
    from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider
    live_keys = set(FMPScreenerProvider._normalise_result({}).keys())
    hist_keys = set(H.FMPHistoricalScreenerProvider._normalise(
        "X", 1.0, 1.0, 1.0, None).keys())
    assert hist_keys == live_keys   # identical dict shape -> unchanged downstream pipeline


def test_historical_rejects_none_as_of():
    p = H.FMPHistoricalScreenerProvider()
    p.api_key = "x"
    with pytest.raises(ValueError):
        p.screen_stocks({}, as_of=None)
```

- [ ] **Step 4: Run the historical-provider tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && python -m pytest tests/test_historical_screener.py -v
```
Expected: PASS. The `test_historical_normalised_shape_matches_live` is the **gate item 2** anchor — it proves identical dict shape.

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "feat(providers): FMPHistoricalScreenerProvider (as-of metric reconstruction, identical dict shape)"
```

---

## Task 4: Re-anchor `StockScreener` for `as_of` (the seam line + the two now() windows)

**Files:**
- Edit `BA2TradeProviders/ba2_providers/StockScreener.py` (add `as_of` ctor/`screen()` param; re-anchor `_fetch_history_bulk`/`_fetch_quotes_chunked`; thread `as_of` into the provider call + Weinstein/price-drop)
- Test: `BA2TradeProviders/tests/test_stockscreener_asof.py`

`StockScreener`'s LOGIC must not fork. The edits: (i) accept `as_of`, (ii) select `fmp_historical` when `as_of` is set, (iii) pass `as_of` into `screen_stocks`, (iv) re-anchor the two `now()` windows so enrichment/Weinstein/price-drop read bars truncated to `as_of`.

- [ ] **Step 1: Add `as_of` to the constructor + `screen()` provider selection**

Edit `BA2TradeProviders/ba2_providers/StockScreener.py`. Add `as_of` to `__init__` and add a `universe_mode` default to `_DEFAULTS`:

```python
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

class StockScreener:
    _DEFAULTS: Dict[str, Any] = {
        # ... existing keys unchanged ...
        "screener_weinstein_stage2_only": 0,
        "universe_mode": "broad",           # NEW: 'broad' | 'sp500' | 'nasdaq' (historical only)
    }

    def __init__(self, settings: Dict[str, Any], progress_callback=None,
                 as_of: Optional[datetime] = None):
        self._progress_callback = progress_callback
        self._as_of = as_of                 # None => live; <date> => reconstructed
        # ... existing settings-coercion loop unchanged ...
```

In `screen()`, replace the provider-selection + Stage-1 call (current lines 131–144):

```python
        from ba2_providers import get_provider   # post-Phase-0 import (was ..modules.dataproviders)

        stats: Dict[str, int] = {}
        if self._as_of is None:
            provider_name = self._settings["screener_provider"]
            screener = get_provider("screener", provider_name)
        else:
            provider_name = "fmp_historical"
            screener = get_provider("screener", "fmp_historical",
                                    universe_mode=self._settings["universe_mode"])

        filters = self._build_provider_filters()
        logger.info(f"StockScreener: stage 1 — screening via '{provider_name}' "
                    f"(as_of={self._as_of}) with filters: {filters}")
        self._report_progress("Fetching candidates from screener...", 0.05)
        candidates = screener.screen_stocks(filters, as_of=self._as_of)
        stats["screener_candidates"] = len(candidates)
```

Everything after line 145 (the `if not candidates` guard, Stage 2/2.5/3/4) is **unchanged** — same functions, same logic.

- [ ] **Step 2: Re-anchor the two `now()` windows to `as_of`**

In `_fetch_history_bulk` (currently `@staticmethod`, lines 326–407) and `_fetch_quotes_chunked` (254–324): the `now()` anchor (lines 352–354) must become `as_of` when reconstructing. Convert `_fetch_history_bulk` to an **instance method** (drop `@staticmethod`) so it can read `self._as_of`, and re-anchor:

```python
    def _fetch_history_bulk(self, symbols, lookback_days, chunk_size=5, max_workers=8):
        # ... unchanged setup (api_key, threading imports) ...
        anchor = self._as_of or datetime.now(timezone.utc)
        from_date = (anchor - timedelta(days=lookback_days + 5)).strftime("%Y-%m-%d")
        to_date = anchor.strftime("%Y-%m-%d")
        params_base = {"apikey": api_key, "from": from_date, "to": to_date}
        # ... rest unchanged (chunked fetch, reverse to oldest-first) ...
```

Update the two callers (`_filter_by_price_drop` line 571, `_filter_by_weinstein_stage2` line 639) from `self._fetch_history_bulk(...)` — they already call it on `self` via `self._fetch_history_bulk`? **Confirm:** the source calls `self._fetch_history_bulk(all_symbols, lookback_days)` (it's invoked as `self.`-bound even though decorated `@staticmethod` — Python allows that). After dropping `@staticmethod`, the `self.` calls work unchanged. For `_fetch_quotes_chunked` (RVOL enrichment, live quotes): in the historical path, quote-based RVOL has no clean as-of equivalent — re-anchor RVOL to **as-of average volume from bars** instead. Add a guard at its call site in `_enrich_with_rvol`:

```python
        if self._as_of is not None:
            # historical: derive RVOL from as-of bars, not live quotes
            quotes_map = {}   # no live quotes; rvol computed from bar volumes below
        else:
            quotes_map = self._fetch_quotes_chunked(all_symbols)
```

> **Re-plan checkpoint:** the RVOL reconstruction in the historical path needs current-vs-average bar volume from `_fetch_history_bulk(as_of)`. Confirm at execution whether `_enrich_with_rvol` should compute `rvol = today_bar_volume / trailing_avg_volume` from the as-of bars (preferred, fully point-in-time) when `self._as_of` is set, replacing the live-quote `volume/avgVolume`. Keep the LIVE branch (`quotes_map = self._fetch_quotes_chunked(...)`) byte-identical so `as_of=None` is unchanged.

- [ ] **Step 3: Write the `StockScreener` as_of tests (mock the provider + fetches)**

`BA2TradeProviders/tests/test_stockscreener_asof.py`:

```python
from datetime import datetime, timezone
import ba2_providers.StockScreener as S

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)


def test_screener_selects_historical_provider_when_as_of_set(monkeypatch):
    captured = {}
    class FakeProv:
        def screen_stocks(self, filters, as_of=None):
            captured["as_of"] = as_of
            captured["filters"] = filters
            return []   # empty -> early return, skips enrichment
    def fake_get_provider(cat, name, **kw):
        captured["name"] = name
        captured["kw"] = kw
        return FakeProv()
    import ba2_providers
    monkeypatch.setattr(ba2_providers, "get_provider", fake_get_provider)

    sc = S.StockScreener({"universe_mode": "sp500"}, as_of=AS_OF)
    out = sc.screen()
    assert captured["name"] == "fmp_historical"
    assert captured["kw"]["universe_mode"] == "sp500"
    assert captured["as_of"] == AS_OF
    assert out["results"] == []


def test_screener_live_path_unchanged(monkeypatch):
    captured = {}
    class FakeProv:
        def screen_stocks(self, filters, as_of=None):
            captured["as_of"] = as_of
            return []
    import ba2_providers
    monkeypatch.setattr(ba2_providers, "get_provider", lambda c, n, **k: FakeProv())
    sc = S.StockScreener({"screener_provider": "fmp"})   # no as_of
    sc.screen()
    assert captured["as_of"] is None   # live path: as_of stays None


def test_fetch_history_bulk_anchors_on_as_of(monkeypatch):
    sc = S.StockScreener({}, as_of=AS_OF)
    captured = {}
    # intercept the HTTP layer to assert the to_date window
    import ba2_providers.StockScreener as mod
    def fake_http(url, params=None, endpoint=None, timeout=None):
        captured["to"] = params.get("to")
        class R:
            def json(self): return {"historicalStockList": []}
        return R()
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_http, raising=False)
    monkeypatch.setattr(mod, "get_app_setting", lambda k: "key", raising=False)
    sc._fetch_history_bulk(["AAA"], lookback_days=5)
    assert captured["to"] == "2020-06-30"   # anchored on as_of, not today
```

- [ ] **Step 4: Run the StockScreener as_of tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && python -m pytest tests/test_stockscreener_asof.py -v
```
Expected: PASS. If `test_fetch_history_bulk_anchors_on_as_of` errors on the `fmp_http_get` patch target, reconcile the import path (`from ..fmp_common import fmp_http_get` inside the method → patch `ba2_providers.fmp_common.fmp_http_get`).

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "feat(providers): StockScreener as_of (historical provider selection + now()-window re-anchor)"
```

---

## Task 5: Grouped/labeled screener-history cache (the `screener_history` table + replay)

**Files:**
- Create `BA2TestPlatform/backend/app/services/screener_history_cache.py` (the cache service: hash, write survivors, replay)
- Create the `screener_history` table (via the Phase-2 `provider_cache` SQLite store, or a migration in `BA2TestPlatform`)
- Test: `BA2TestPlatform/tests/test_screener_history_cache.py`

The cache makes a backtest sweep over scan dates a cheap scan and guarantees "cache-once" (gate item 4). One row per surviving `(symbol, scan_date, screen_config_hash)`. The machine identity is `screen_config_hash`; `group/expert` is a human label.

> **Re-plan checkpoint (Phase-2 cache infra):** confirm the Phase-2 SQLite cache (the generic `provider_cache` table + per-key locks + atomic writes under `<CACHE_FOLDER>/datasets/cache`). Reuse its connection/locking helpers for `screener_history` rather than opening a second sqlite. If Phase 2 exposed a reusable `CachedProviderMixin` or cache-DB handle, build `screener_history` on it; otherwise create a sibling table in the same backtest DB.

- [ ] **Step 1: Define the `screen_config_hash` + the cache schema**

`BA2TestPlatform/backend/app/services/screener_history_cache.py`:

```python
"""Grouped/labeled screener-history cache.

Row identity: (symbol, scan_date, screen_config_hash). screen_config_hash is the
machine identity (sha256 of the result-affecting settings AFTER coercion). group/expert
is a human label. A config change yields a distinct hash namespace so different criteria
never mix; replaying a (scan_date, hash) returns the exact survivor set with no fetches.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ba2_common.logger import logger

# The 14 screener_* threshold keys + stage2 flag + sort_metric + universe_mode.
# These (and ONLY these) affect which symbols survive -> they define the hash.
_HASH_KEYS = [
    "screener_provider",
    "screener_market_cap_min", "screener_market_cap_max",
    "screener_volume_min", "screener_volume_max",
    "screener_float_min", "screener_float_max",
    "screener_price_min", "screener_price_max",
    "screener_relative_volume_min",
    "screener_price_drop_pct", "screener_price_drop_days",
    "screener_max_stocks", "screener_sort_metric",
    "screener_weinstein_stage2_only",
    "universe_mode",
]


def screen_config_hash(coerced_settings: Dict[str, Any]) -> str:
    """sha256 of canonical-JSON of the result-affecting settings subset (post-coercion)."""
    subset = {k: coerced_settings.get(k) for k in _HASH_KEYS}
    canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_DDL = """
CREATE TABLE IF NOT EXISTS screener_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    group_label TEXT NOT NULL,
    screen_config_hash TEXT NOT NULL,
    rank INTEGER,
    sort_metric_value REAL,
    market_cap_as_of REAL,
    price_as_of REAL,
    relative_volume REAL,
    weinstein_stage INTEGER,
    price_drop_pct REAL,
    universe_mode TEXT,
    market_cap_source TEXT,
    float_approx INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, scan_date, screen_config_hash)
);
CREATE INDEX IF NOT EXISTS ix_scan_hash ON screener_history(scan_date, screen_config_hash);
"""


class ScreenerHistoryCache:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    def has(self, scan_date: datetime, cfg_hash: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM screener_history WHERE scan_date=? AND screen_config_hash=? LIMIT 1",
                (scan_date.strftime("%Y-%m-%d"), cfg_hash)).fetchone()
            return row is not None

    def replay(self, scan_date: datetime, cfg_hash: str) -> List[Dict[str, Any]]:
        """Return cached survivors (rank-ordered) for (scan_date, hash) — NO fetches."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM screener_history WHERE scan_date=? AND screen_config_hash=? "
                "ORDER BY rank ASC", (scan_date.strftime("%Y-%m-%d"), cfg_hash)).fetchall()
        return [dict(r) for r in rows]

    def write(self, scan_date: datetime, cfg_hash: str, group_label: str,
              survivors: List[Dict[str, Any]], universe_mode: str) -> int:
        """Persist the survivor list (idempotent via the UNIQUE key)."""
        now = datetime.utcnow().isoformat()
        sd = scan_date.strftime("%Y-%m-%d")
        written = 0
        with self._lock, self._connect() as con:
            for rank, s in enumerate(survivors):
                con.execute(
                    "INSERT OR REPLACE INTO screener_history "
                    "(symbol, scan_date, group_label, screen_config_hash, rank, "
                    " sort_metric_value, market_cap_as_of, price_as_of, relative_volume, "
                    " weinstein_stage, price_drop_pct, universe_mode, market_cap_source, "
                    " float_approx, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s.get("symbol"), sd, group_label, cfg_hash, rank,
                     s.get("sort_metric_value"), s.get("market_cap"), s.get("price"),
                     s.get("relative_volume"), s.get("weinstein_stage"),
                     s.get("price_drop_pct"), universe_mode,
                     s.get("market_cap_source"), 1 if s.get("float_approx", True) else 0, now))
                written += 1
            con.commit()
        logger.info(f"screener-history: wrote {written} survivors for {sd} ({group_label}, {cfg_hash[:8]})")
        return written
```

- [ ] **Step 2: Wire replay-or-compute into a thin runner**

Add to `screener_history_cache.py` a function that ties `StockScreener` + the cache together — this is what the Phase-4 engine calls per rebalance bar:

```python
def screened_universe_for_bar(settings: Dict[str, Any], scan_date: datetime,
                              group_label: str, cache: "ScreenerHistoryCache",
                              progress_callback=None) -> List[Dict[str, Any]]:
    """Replay from cache if present for (scan_date, hash); else run the as-of pipeline
    and persist labeled survivors. Returns the survivor dict list (the bar's universe)."""
    from ba2_providers.StockScreener import StockScreener
    sc = StockScreener(settings, progress_callback=progress_callback, as_of=scan_date)
    cfg_hash = screen_config_hash(sc._settings)   # post-coercion settings
    if cache.has(scan_date, cfg_hash):
        logger.info(f"screener-history: replay {scan_date.date()} {cfg_hash[:8]} (no fetch)")
        return cache.replay(scan_date, cfg_hash)
    result = sc.screen()
    survivors = result["results"]
    # annotate sort_metric_value for cache ranking
    metric = sc._settings["screener_sort_metric"]
    for s in survivors:
        s["sort_metric_value"] = s.get(metric) or s.get("market_cap")
        s.setdefault("market_cap_source", "shares_x_close")
        s.setdefault("float_approx", True)
    cache.write(scan_date, cfg_hash, group_label, survivors, sc._settings["universe_mode"])
    return survivors
```

- [ ] **Step 3: Write the cache tests**

`BA2TestPlatform/tests/test_screener_history_cache.py`:

```python
from datetime import datetime, timezone
import ba2_test_platform_screener as _  # placeholder; import path reconciled below
from backend.app.services.screener_history_cache import (
    ScreenerHistoryCache, screen_config_hash, screened_universe_for_bar)

SD = datetime(2020, 6, 30, tzinfo=timezone.utc)
BASE = {"screener_market_cap_min": 1_000_000_000, "screener_sort_metric": "market_cap",
        "universe_mode": "broad"}


def test_hash_stable_and_config_sensitive():
    h1 = screen_config_hash({**BASE})
    h2 = screen_config_hash({**BASE})
    h3 = screen_config_hash({**BASE, "screener_market_cap_min": 2_000_000_000})
    assert h1 == h2          # deterministic
    assert h1 != h3          # a threshold change -> distinct namespace


def test_write_then_replay(tmp_path):
    cache = ScreenerHistoryCache(str(tmp_path / "sh.sqlite"))
    survivors = [{"symbol": "AAA", "market_cap": 5e9, "price": 50.0},
                 {"symbol": "BBB", "market_cap": 3e9, "price": 40.0}]
    cache.write(SD, "hashX", "FactorRanker", survivors, "broad")
    assert cache.has(SD, "hashX")
    rows = cache.replay(SD, "hashX")
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]   # rank-ordered


def test_cache_once_no_second_fetch(tmp_path, monkeypatch):
    cache = ScreenerHistoryCache(str(tmp_path / "sh2.sqlite"))
    calls = {"n": 0}
    import backend.app.services.screener_history_cache as M

    class FakeSC:
        def __init__(self, settings, progress_callback=None, as_of=None):
            self._settings = {**BASE, "screener_provider": "fmp_historical",
                              "screener_market_cap_max": 0, "screener_volume_min": 0,
                              "screener_volume_max": 0, "screener_float_min": 0,
                              "screener_float_max": 0, "screener_price_min": 0,
                              "screener_price_max": 0, "screener_relative_volume_min": 0,
                              "screener_price_drop_pct": 0, "screener_price_drop_days": 1,
                              "screener_max_stocks": 10, "screener_weinstein_stage2_only": 0}
        def screen(self):
            calls["n"] += 1
            return {"results": [{"symbol": "AAA", "market_cap": 5e9, "price": 50.0}], "stats": {}}
    monkeypatch.setattr(M, "StockScreener", FakeSC, raising=False)
    # also patch the lazy import inside the function
    import ba2_providers.StockScreener as SCmod
    monkeypatch.setattr(SCmod, "StockScreener", FakeSC, raising=False)

    u1 = screened_universe_for_bar(BASE, SD, "FactorRanker", cache)
    u2 = screened_universe_for_bar(BASE, SD, "FactorRanker", cache)
    assert calls["n"] == 1                      # second call replayed from cache, no screen()
    assert [r["symbol"] for r in u1] == [r["symbol"] for r in u2]
```

> **Re-plan checkpoint (import paths):** the test's `from backend.app.services...` and the `StockScreener` patch target depend on `BA2TestPlatform`'s package layout + how `screened_universe_for_bar` imports `StockScreener` (lazy `from ba2_providers.StockScreener import StockScreener`). Reconcile the patch target to wherever the name is looked up. Drop the placeholder `import ba2_test_platform_screener` line — it is a reminder to set `PYTHONPATH`/conftest so `backend.app.services` resolves.

- [ ] **Step 4: Run the cache tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest tests/test_screener_history_cache.py -v
```
Expected: PASS — especially `test_cache_once_no_second_fetch` (gate item 4: `screen()` called exactly once).

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): grouped/labeled screener-history cache (config-hash namespacing + cache-once replay)"
```

---

## Task 6: Weinstein Stage-2 over reconstructed history (the screener-expert enabler)

**Files:**
- Edit `BA2TradeProviders/ba2_providers/StockScreener.py` (`_filter_by_weinstein_stage2` reads as-of bars — already covered by Task 4 Step 2's `_fetch_history_bulk` re-anchor; this task verifies it end-to-end)
- Test: `BA2TradeProviders/tests/test_weinstein_asof.py`

`_filter_by_weinstein_stage2` already feeds `classify_weinstein_stage(closes)` (pure, in `ba2_common.core.weinstein`) closes truncated to the fetch window. After Task 4's re-anchor, those closes are ≤ `as_of` (point-in-time-safe by construction). This task pins that with a deterministic test.

- [ ] **Step 1: Confirm `_filter_by_weinstein_stage2` is as-of-safe**

`grep -n "_fetch_history_bulk\|classify_weinstein_stage\|self._as_of" BA2TradeProviders/ba2_providers/StockScreener.py`. Confirm `_filter_by_weinstein_stage2` (source lines 627–668) calls `self._fetch_history_bulk(all_symbols, 250)` (now `as_of`-anchored) and `classify_weinstein_stage(closes)`. No code change should be needed beyond Task 4; if `_fetch_history_bulk` is still `@staticmethod`-bound, the `self.` call already routes through the instance after Task 4 dropped the decorator.

- [ ] **Step 2: Write the as-of Weinstein test (deterministic bars, no network)**

`BA2TradeProviders/tests/test_weinstein_asof.py`:

```python
from datetime import datetime, timezone
import ba2_providers.StockScreener as S

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)


def test_weinstein_stage2_over_asof_bars(monkeypatch):
    sc = S.StockScreener({"screener_weinstein_stage2_only": 1}, as_of=AS_OF)

    # UP: clean uptrend above a rising 150-SMA -> Stage 2. FLAT: no trend -> not Stage 2.
    up = [{"close": 10 + i * 0.5} for i in range(200)]
    flat = [{"close": 50.0} for _ in range(200)]
    monkeypatch.setattr(sc, "_fetch_history_bulk",
                        lambda symbols, lookback_days, **k: {"UP": up, "FLAT": flat})

    candidates = [{"symbol": "UP"}, {"symbol": "FLAT"}]
    passed, stats = sc._filter_by_weinstein_stage2(candidates)
    syms = {c["symbol"] for c in passed}
    assert "UP" in syms
    assert "FLAT" not in syms
    assert passed[0]["weinstein_stage"] == 2
    assert stats["weinstein_stage2"] == 1


def test_weinstein_pure_classifier_imported_from_common():
    # the filter delegates to the SAME pure function used live
    from ba2_common.core.weinstein import classify_weinstein_stage
    closes = [10 + i * 0.5 for i in range(200)]
    assert classify_weinstein_stage(closes)["stage"] == 2
```

- [ ] **Step 3: Run the Weinstein as-of tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && python -m pytest tests/test_weinstein_asof.py -v
```
Expected: PASS — proves the Stage-2 filter (the FactorRanker/screener-expert enabler) runs unchanged over reconstructed bars.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "test(providers): Weinstein Stage-2 filter verified over as-of reconstructed bars" || true
```

---

## Task 7: Survivorship integration test (delisted names in past universes)

**Files:**
- Test: `BA2TestPlatform/tests/test_survivorship_integration.py`

This is the **gate item 3** anchor: a delisted symbol appears on dates it traded and vanishes after `delistedDate`, and a fixed-current-universe run would have omitted it.

- [ ] **Step 1: Write the survivorship integration test (mock the lifecycle + metrics)**

`BA2TestPlatform/tests/test_survivorship_integration.py`:

```python
from datetime import datetime, timezone
import ba2_providers.screener.FMPHistoricalScreenerProvider as H
import ba2_providers.screener.universe as U

D = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

# A loser that delisted in 2021 — absent from today's universe, present in 2020.
LIFECYCLE = {
    "WINNER": (D("2010-01-01"), None),
    "LOSER":  (D("2014-01-01"), D("2021-03-15")),
}


def _build(monkeypatch, mode="broad"):
    p = H.FMPHistoricalScreenerProvider(universe_mode=mode)
    p.api_key = "x"
    monkeypatch.setattr(H, "fetch_lifecycle_map", lambda: LIFECYCLE)
    monkeypatch.setattr(H, "broad_universe",
                        lambda a, lifecycle=None: U.broad_universe(a, lifecycle=LIFECYCLE))
    monkeypatch.setattr(p, "_close_at", lambda s, a: 50.0)
    monkeypatch.setattr(p, "_market_cap_at", lambda s, a, c: 5e9)
    monkeypatch.setattr(p, "_avg_volume_at", lambda s, a, window=20: 1_000_000)
    return p


def test_delisted_symbol_present_on_traded_date(monkeypatch):
    p = _build(monkeypatch)
    filters = {"price_min": 20, "market_cap_min": 1_000_000_000, "volume_min": 500_000,
               "limit": 100}
    on_2020 = {r["symbol"] for r in p.screen_stocks(filters, as_of=D("2020-06-30"))}
    assert "LOSER" in on_2020          # survivorship-free: traded in 2020 -> present
    assert "WINNER" in on_2020


def test_delisted_symbol_absent_after_death(monkeypatch):
    p = _build(monkeypatch)
    filters = {"price_min": 20, "market_cap_min": 1_000_000_000, "volume_min": 500_000,
               "limit": 100}
    on_2022 = {r["symbol"] for r in p.screen_stocks(filters, as_of=D("2022-01-03"))}
    assert "LOSER" not in on_2022      # delisted 2021-03-15 -> gone in 2022
    assert "WINNER" in on_2022


def test_fixed_current_universe_would_omit_loser(monkeypatch):
    # The live screener returns today's listings only; LOSER is delisted now -> omitted.
    # The historical path INCLUDES it on 2020 -> the delta is exactly the survivorship fix.
    p = _build(monkeypatch)
    filters = {"price_min": 20, "market_cap_min": 1_000_000_000, "volume_min": 500_000,
               "limit": 100}
    hist_2020 = {r["symbol"] for r in p.screen_stocks(filters, as_of=D("2020-06-30"))}
    current_listings = {s for s, (ipo, dl) in LIFECYCLE.items() if dl is None}  # today
    assert "LOSER" in hist_2020
    assert "LOSER" not in current_listings   # the bias the historical universe removes
```

- [ ] **Step 2: Run the survivorship test**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest tests/test_survivorship_integration.py -v
```
Expected: PASS — all three assertions encode the survivorship-bias-removal contract.

- [ ] **Step 3: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "test(backtest): survivorship integration — delisted names present on traded dates only"
```

---

## Task 8: Phase-3 acceptance gate + regression re-verify

- [ ] **Step 1: Full Phase-3 test run (providers + backtest host)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && python -m pytest tests/test_universe.py tests/test_historical_screener.py tests/test_stockscreener_asof.py tests/test_weinstein_asof.py -q
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest tests/test_screener_interface_asof.py tests/test_screener_history_cache.py tests/test_survivorship_integration.py -q
```
Expected: all PASS. These cover gate items 1–4 (live-equivalence via the untouched `FMPScreenerProvider` + `as_of=None` path; historical filter-logic equivalence via the identical-dict-shape test; survivorship; cache-once).

- [ ] **Step 2: Live-equivalence golden regression (`as_of=None` byte-equal)**

`BA2TestPlatform/tests/test_screener_live_equivalence.py` (network-gated; runs only when `FMP_API_KEY` is set):

```python
import os
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("FMP_API_KEY"),
                                reason="needs live FMP key for golden equivalence")


def test_live_screen_unchanged_shape_and_keys():
    """as_of=None must hit the live FMP screener and return the canonical dict shape."""
    from ba2_providers.StockScreener import StockScreener
    settings = {"screener_market_cap_min": 50_000_000_000, "screener_volume_min": 1_000_000,
                "screener_price_min": 20.0, "screener_relative_volume_min": 0,
                "screener_price_drop_pct": 0, "screener_max_stocks": 5,
                "screener_sort_metric": "market_cap"}
    out = StockScreener(settings).screen()   # no as_of -> live
    assert isinstance(out["results"], list)
    if out["results"]:
        keys = set(out["results"][0].keys())
        assert {"symbol", "price", "market_cap", "volume"} <= keys
```

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest tests/test_screener_live_equivalence.py -v
```
Expected: PASS (or skipped if no key). This is gate item 1 — the live path is unchanged because `FMPScreenerProvider` is untouched and `as_of` defaults to `None`.

- [ ] **Step 3: Phase-1 golden re-verify (regression — gate item 5)**

Re-run the Phase-1 golden test suite (live `run_analysis` == `analyze_as_of(now)` for every backtestable expert). Phase 3 only added an optional `as_of` param defaulting to `None`, so no live decision logic changed.

> **Re-plan checkpoint:** the Phase-1 golden test location/runner is a Phase-1 deliverable. Confirm its path and command (likely `BA2TestPlatform/tests/test_golden_*.py` or in the experts repo). Run it and confirm green.

```bash
# Re-plan checkpoint: substitute the actual Phase-1 golden test path/command.
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform && ./backend/venv/bin/python -m pytest -k golden -q
```
Expected: same green baseline as end of Phase 1.

- [ ] **Step 4: Confirm BA2TradePlatform is untouched**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform status --short
```
Expected: only this plan doc (and pre-existing untracked files); **no** changes under `ba2_trade_platform/` (the live host migrates in Phase 6).

- [ ] **Step 5: Push the package + host branches (only after approval to publish)**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders push -u origin phase3-screener-history
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon  push -u origin phase3-screener-history
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform push -u origin phase3-screener-history
```
> Pushing is outward-facing — do this only when the user confirms.

---

## Self-Review

**Spec coverage (design §4, §6 Phase 3 + SHARED CONTRACTS `screener_history`):**
- "ONE provider class, two data-source modes behind an optional `as_of`; filter LOGIC never forks" → Task 1 (interface seam) + Task 3 (`fmp_historical`) + Task 4 (`StockScreener` selects by `as_of`, reuses all post-fetch filters). ✓
- "fetch-vs-filter seam is the single line `candidates = screener.screen_stocks(filters)`" → Task 4 Step 1 (line 144 forks on `as_of` only). ✓
- "broad = available-traded ∪ delisted with ipo/delisted lifecycle; index-scoped = dated sp500/nasdaq constituents" → Task 2 (`broad_universe` + `index_universe`). ✓
- "market-cap = shares × as-of price; price/volume/float reconstruction" → Task 3 (`_close_at`/`_market_cap_at`/`_avg_volume_at`/`_shares_at`; dated `historical-market-capitalization` preferred). ✓
- "reuse `_build_provider_filters`/`_enrich_with_rvol`/`_filter_by_weinstein_stage2`/`_filter_by_price_drop`/`_rank`/`classify_weinstein_stage` unchanged; only DATA INPUTS swap" → Tasks 4 & 6 (no logic edits; identical-dict-shape test). ✓
- "re-anchor the two now()-based windows so to_date=as_of" → Task 4 Step 2 (`_fetch_history_bulk` instance method + RVOL guard). ✓
- "grouped/labeled cache keyed (symbol, scan_date, screen_config_hash); group/expert human label; hash = sha256 of coerced result-affecting settings" → Task 5 (`screen_config_hash` + `screener_history` table + replay). ✓
- "replay before screening; config change = distinct hash namespace" → Task 5 Step 2 (`screened_universe_for_bar`) + tests. ✓
- GATE: "historical screen reproduces live filter logic + survivorship + cache-once" → Task 8 (gate items 1–4) + Task 7 (survivorship) + Task 5 (cache-once). ✓
- Locked decisions respected: equities-first (universe is US-listed equities); daily cadence (scan_date granularity); extract-by-copy (edits land in `ba2_providers`/`BA2TestPlatform`, **BA2TradePlatform untouched** until Phase 6); reuses `weinstein`/`fmp_common`/`StockScreener` seams, no new screen logic. ✓

**Placeholder scan:** `universe.py`, `FMPHistoricalScreenerProvider.py`, `screener_history_cache.py`, the interface/StockScreener edits, and all tests contain full code. The "confirm field name / signature in source" notes and the four `> Re-plan checkpoint:` blocks are deliberate guards against (a) Phase-0/1/2 outputs not yet built and (b) FMP field-name drift — they describe exactly what to confirm at execution time instead of fabricating it. No "TBD"/"add later".

**Type/name consistency:** `as_of: Optional[datetime] = None` is consistent across `ScreenerProviderInterface.screen_stocks`, both providers, and `StockScreener.__init__`. `screen_config_hash(coerced_settings)` is computed from `StockScreener._settings` (post-coercion) in both Task 5 and the cache runner. `_normalise`/`_normalise_result` key sets are asserted equal (Task 3 Step 3). `universe_mode` flows `_DEFAULTS` → `get_provider(..., universe_mode=...)` → `FMPHistoricalScreenerProvider.__init__` → `universe.index_universe`/`broad_universe`. `get_provider`'s `**kwargs` forwarding to the constructor is confirmed from the live registry source.

**Known reconciliation points (verify against source/earlier-phase output during execution, do not assume):** post-Phase-0 location of `StockScreener.py` / `ScreenerProviderInterface.py`; exact Phase-2 `get()` signatures + return shapes (OHLCV `bars`, income-statement `weighted_average_shares_outstanding`); FMP field names for `ipoDate`/`delistedDate` and the index change-log (`addedSecurity`/`removedTicker`/`date`); whether `/api/v4/shares_float` returns dated history (float approximation vs real); the Phase-1 golden test path/command; the `BA2TestPlatform` package import roots for the cache service tests; whether exchange can be reconstructed as-of or is enforced by downstream US-only quote enrichment.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase3-screener-history-plan.md`. Phase 3 **depends on Phases 0–2** (package layout + `as_of` providers + native cache) and is **consumed by Phase 4** (the engine's per-rebalance-bar universe). Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`). Tasks 1→2→3→4 are sequential (each builds the next); Tasks 5/6/7 can parallelize after Task 4; Task 8 is the gate.
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

All edits land on a `phase3-screener-history` branch in `BA2TradeProviders` (+ small `BA2TradeCommon` interface change) and `BA2TestPlatform`; **`BA2TradePlatform` stays read-only** (migrates in Phase 6). Before starting, re-read the four `> Re-plan checkpoint:` blocks and confirm the Phase-0/1/2 outputs they depend on.
