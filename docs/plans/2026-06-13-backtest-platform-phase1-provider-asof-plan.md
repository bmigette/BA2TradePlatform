# Backtest Platform — Phase 1 (Provider `as_of` + native cache + expert `_gather`/`_process` split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is independently testable; **Task 0 → Task 12 is a hard order** (the golden harness in Task 12 is the acceptance gate).

**Goal:** Make every backtestable expert run the **same decision logic** live and in backtest by (1) adding a uniform provider `get(symbol, as_of=None, lookback=…)` contract whose `as_of=None` path is byte-identical to today's live fetch, (2) building an effective-date native cache (parquet time-series + a generic SQLite `provider_cache` index, generalizing the proven BA2TestPlatform `NewsCache`), (3) fixing the two confirmed lookahead bugs (insider `transactionDate`→`filingDate`; statements `fiscalDateEnding`→`fillingDate`/`acceptedDate`), and (4) refactoring each expert into a pure-ish `_gather(providers, as_of) → data_bundle` + a **pure** `_process(data_bundle, settings[, as_of]) → Recommendation`, with live `run_analysis` reduced to a thin orchestrator and `BacktestInterface.analyze_as_of(as_of, context)` calling the **same** `_gather`+`_process`.

**Architecture:** This phase lands in the three packages produced by Phase 0 (`ba2_common ← ba2_providers ← ba2_experts`), **not** in the live `BA2TradePlatform/ba2_trade_platform/` tree (its migration is Phase 6). The `Recommendation` value object, `ProviderBundle`/`BacktestContext` accessors, `BacktestInterface.analyze_as_of` seam, and the `CachedProviderMixin` all live in `ba2_common`; the cache implementation and per-category `get()` wrappers land in `ba2_providers`; the per-expert `_gather`/`_process` split lands in `ba2_experts`. The **BACKTEST CONTRACT**: `_gather(live, as_of=None)+_process == _gather(providers, as_of=now)+_process`, proven by the golden test.

**Locked decisions honored:** classic Risk Manager only; daily cadence v1; equities-first / options-ready (this phase touches no account code); extract-by-copy (Phase 0 already copied; Phase 1 edits the **copies**, never the live tree); the `ba2_common` seams (`instance_resolver`, `LLMServiceInterface`, `db.configure_db`, `position_sizing.get_latest_atr(indicator_provider)`, `TradeConditions.set_provider_resolver`) are consumed, not redefined.

**Tech Stack:** Python ≥3.11, SQLModel/SQLAlchemy (the new `provider_cache` model is a `ba2_common` SQLModel table written to the configured DB), `pyarrow`/`pandas` (parquet time-series store), pytest. New runtime dep added to `ba2_providers`: `pyarrow>=16.0`.

---

## Source of truth & repo locations

- **Phase-0 packages (edited here, on a new branch each):** `BA2TradeCommon` (`ba2_common`), `BA2TradeProviders` (`ba2_providers`), `BA2TradeExperts` (`ba2_experts`), siblings under `/Users/bmigette/Documents/dev/BA2/`. Branch each `phase1-asof` off `phase0-extraction`.
- **Live reference tree (READ-ONLY, never edited in Phase 1):** `BA2TradePlatform/ba2_trade_platform/` at commit `72eefee`. Used only to read original signatures / behaviour for the byte-equality assertions.
- Derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§2 the backtest contract, §3 providers as-of + native cache, §6 Phase 1) and `docs/FMP_BACKTEST_FEASIBILITY.md`, plus a file-by-file recon of the `72eefee` expert/provider code, and the SHARED CONTRACTS (`gather_process`, `provider_asof`, `recommendation_object`, `data_bundle_shape`, `golden_test`).

> Re-plan checkpoint: this plan assumes Phase 0 has landed and the three packages install/import cleanly (`ba2_common.core.interfaces`, `ba2_providers.get_provider`, `ba2_experts.get_expert_class` all importable; `lint-imports` green in each). **Before starting, confirm `git -C BA2TradeCommon branch --show-current` resolves and `python -c "import ba2_experts"` succeeds in a chain venv.** If Phase 0 mapped a file to a different module path than this plan names (e.g. `ba2_experts.FMPEarningsDrift` vs `ba2_experts.experts.FMPEarningsDrift`), use the **actual** Phase-0 path and note the delta in the task commit.

## Decisions taken (confirm before execution)

These resolve forks the recon + open-questions surfaced. Override at approval time.

1. **`current_price` source = OHLCV close at `as_of` (one source for all experts).** Resolves open-question 3. `_gather` resolves `current_price` via the OHLCV provider's `as_of` close (NOT the live broker quote, NOT FMPSenateTraderWeight's open-price helper), stores it in every `data_bundle`, so `_process` is pure. Live `run_analysis` with `as_of=None` uses the same OHLCV `as_of=None` close so live==backtest is logic-only (the golden harness additionally pins `current_price` to a fixture in both paths so a price-source difference can never mask logic drift).
2. **`Recommendation` is a frozen-ish dataclass in `ba2_common.core.types`** (not a new module), next to the enums it references, so both `ba2_common` interfaces and `ba2_experts` import it without a layering edge.
3. **`SignalType` reuses the existing `OrderRecommendation` enum** (`BUY/SELL/HOLD/OVERWEIGHT/UNDERWEIGHT/ERROR`, `types.py:272`). SKIP is modeled as `Recommendation.skip=True` (+`skip_reason`), **not** a new enum member, because the live code already returns HOLD/BUY and uses `MarketAnalysisStatus.SKIPPED` (`types.py:391`) for the skip lifecycle.
4. **FMPRating stays in scope but documented-lookahead.** The two consensus endpoints have no per-row date (feasibility doc + open-question 2); Phase 1 splits FMPRating into `_gather`/`_process` faithfully and the golden test runs it **with `as_of=None` only** plus a documented caveat that `as_of=<past>` carries consensus lookahead (true historical reconstruction via `grades-historical` is deferred to a later "FMPRating last" phase). Done LAST.
5. **Cache writes go to the DB configured by `ba2_common.core.db.configure_db`** (Phase-0 seam) for the `provider_cache` index table; parquet/JSON blobs go under `config.CACHE_FOLDER/datasets/cache/`. Tests point both at temp dirs via the existing `conftest` `configure_db` fixture + a `CACHE_FOLDER` monkeypatch.
6. **Lookahead-bug fixes are guarded by `as_of`.** With `as_of=None` (live) the corrected providers must return byte-equal results to the pre-refactor fetch (the live windows already used `transactionDate`/`fiscalDateEnding` only for the date *range*, not for no-lookahead). The `filingDate`/`fillingDate` anchor is enforced **only** when `as_of` is set, so live behaviour is unchanged and the byte-equality gate (Task 11) holds.
7. **Insider `filingDate` fallback** = `transactionDate + reporting_lag_days` (default 2 business days) when `filingDate` is missing/empty (open-question 6). **Statement effective_date** = `fillingDate` if present else `acceptedDate` else `fiscalDateEnding + 75 days` reporting-lag fallback (open-question 7). Both fallbacks are constants in `provider_utils`, documented as approximations.

## The Phase-1 seams (added to `ba2_common`, implemented in `ba2_providers`/`ba2_experts`)

- **`Recommendation`** (`ba2_common/core/types.py`): the value object `_process` returns (fields per SHARED CONTRACT `recommendation_object`). Live maps it → `ExpertRecommendation` row + `AnalysisOutput`; backtest maps it → engine signal.
- **`BacktestInterface` + `analyze_as_of`** (`ba2_common/core/interfaces/MarketExpertInterface.py`): a `Protocol`/mixin method `analyze_as_of(as_of, context) -> Recommendation` = `_gather(context.providers, as_of)` + `_process(bundle, context.settings, as_of)`.
- **`ProviderBundle` + `BacktestContext`** (`ba2_common/core/types.py`): typed accessors exposing the provider set an expert needs (`ohlcv`, `fundamentals_details`, `fundamentals_overview`, `insider`, `news`, `indicators`, `congress`/FMP-http, `price_at_date`) and the per-trial `settings`/`account`/`as_of`/`subtype`. Phase 1 ships a `LiveProviderBundle` (wraps `ba2_providers.get_provider`); the backtest-cache-backed bundle is Phase 4 — Phase 1 only needs `analyze_as_of(now)` to work through the live bundle.
- **`CachedProviderMixin` + uniform `get(...)`** (`ba2_common/core/interfaces/` mixin, implemented per category in `ba2_providers`): the rename/alias + no-lookahead-enforcement layer over each provider's existing native params (per SHARED CONTRACT `provider_asof.per_category_mapping`).

## Acceptance gate for Phase 1 (the golden test)

For every backtestable expert, `rec_live = _process(_gather(live_providers, as_of=None), settings)` and `rec_asof = expert.analyze_as_of(as_of=now, context)` must be **equal** on `(signal, confidence, expected_profit_percent, details, skip, skip_reason)`, float-tolerant. PLUS: live `run_analysis` still drives the existing lifecycle (`RUNNING→COMPLETED/SKIPPED/FAILED`, `ExpertRecommendation`+`AnalysisOutput` persisted, `WorkerQueue.py:1011` caller signature unchanged). PLUS: provider `as_of=None` returns byte-equal results to the pre-refactor direct fetch, and a fixed `(symbol, as_of)` replays deterministically with `effective_date <= as_of` (no-lookahead), with cache-hit-count assertions proving the native cache serves slices. Verified by `tests/test_golden_live_vs_asof.py` (Task 12), `tests/test_provider_asof.py` (Task 11), and per-expert tests.

---

## Task 0: Branch the three packages + add the `pyarrow` dep

**Files:** branch only; edit `BA2TradeProviders/pyproject.toml`.

- [ ] **Step 1: Branch each package off the Phase-0 branch**

```bash
cd /Users/bmigette/Documents/dev/BA2
for r in BA2TradeCommon BA2TradeProviders BA2TradeExperts; do
  git -C "$r" checkout phase0-extraction && git -C "$r" checkout -b phase1-asof
done
```

> Re-plan checkpoint: if Phase 0 was merged to `main`, branch off `main` instead. Confirm `import ba2_experts` works in a fresh chain venv before editing (the byte-equality gate compares against the live tree, not against a broken Phase-0 import).

- [ ] **Step 2: Add `pyarrow` to `ba2_providers` runtime deps**

Edit `BA2TradeProviders/pyproject.toml`, in the `[project].dependencies` list add (after `"numpy>=2.0.0",`):

```toml
    "pyarrow>=16.0.0",
```

- [ ] **Step 3: Rebuild the chain venv (editable) so the new dep resolves**

```bash
cd /Users/bmigette/Documents/dev/BA2
rm -rf /tmp/v_p1 && python -m venv /tmp/v_p1
/tmp/v_p1/bin/pip install -q -e BA2TradeCommon -e BA2TradeProviders -e "BA2TradeExperts[dev]"
/tmp/v_p1/bin/python -c "import pyarrow, ba2_common, ba2_providers, ba2_experts; print('chain ok')"
```
Expected: `chain ok`.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -am "chore(phase1): branch + add pyarrow for native parquet cache"
```

---

## Task 1: `Recommendation`, `ProviderBundle`, `BacktestContext` value objects in `ba2_common`

**Files:**
- Edit `BA2TradeCommon/ba2_common/core/types.py` (append `Recommendation`)
- Create `BA2TradeCommon/ba2_common/core/backtest_context.py` (`ProviderBundle`, `LiveProviderBundle`, `BacktestContext`)
- Test: `BA2TradeCommon/tests/test_recommendation.py`

- [ ] **Step 1: Append `Recommendation` to `types.py`**

At the end of `ba2_common/core/types.py` (after the existing enums; `OrderRecommendation` is already defined at line 272):

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Recommendation:
    """Value object returned by every expert's pure ``_process``.

    NOT the SQLModel ``ExpertRecommendation`` row — live ``run_analysis`` maps this
    to an ``ExpertRecommendation`` + ``AnalysisOutput`` rows; the backtest engine maps
    it to an enter/exit/hold/skip signal with no DB persistence. SKIP is first-class
    (FMPRating no-coverage / FactorRanker empty-universe).
    """
    signal: OrderRecommendation          # BUY/SELL/HOLD/OVERWEIGHT/UNDERWEIGHT/ERROR
    confidence: float                    # 1-100 scale (platform convention)
    current_price: float                 # the as_of close, resolved in _gather
    details: str = ""
    expected_profit_percent: Optional[float] = None
    raw_outputs: Dict[str, Any] = field(default_factory=dict)   # -> AnalysisOutput rows
    skip: bool = False
    skip_reason: Optional[str] = None

    def almost_equals(self, other: "Recommendation", tol: float = 1e-6) -> bool:
        """Golden-test equality: identical signal/skip + float-tolerant numerics + details."""
        if not isinstance(other, Recommendation):
            return False
        if self.signal != other.signal or self.skip != other.skip:
            return False
        if (self.skip_reason or "") != (other.skip_reason or ""):
            return False
        if self.details != other.details:
            return False
        def _close(a, b):
            if a is None or b is None:
                return a is None and b is None
            return abs(float(a) - float(b)) <= tol
        return _close(self.confidence, other.confidence) and \
               _close(self.expected_profit_percent, other.expected_profit_percent)
```

> Re-plan checkpoint: confirm `OrderRecommendation` is importable in the same module scope (it is, at `types.py:272`). If Phase 0 split `types.py`, import `OrderRecommendation` at the top of wherever `Recommendation` lands.

- [ ] **Step 2: Write `backtest_context.py`**

`BA2TradeCommon/ba2_common/core/backtest_context.py`:

```python
"""ProviderBundle + BacktestContext — the injected accessors experts use in _gather.

Phase 1 ships only LiveProviderBundle (wraps the live get_provider registry) so
analyze_as_of(now) works through the real providers. The backtest-cache-backed
bundle (pointing at the parquet/SQLite as_of cache + a separate backtest DB) is
built in Phase 4; this module defines the protocol it must satisfy.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class ProviderBundle(Protocol):
    """Typed accessor over the provider set an expert needs. Methods return the
    SAME provider objects the live registry returns, so _gather is provider-agnostic."""
    def ohlcv(self) -> Any: ...
    def fundamentals_details(self) -> Any: ...
    def fundamentals_overview(self) -> Any: ...
    def insider(self) -> Any: ...
    def news(self) -> Any: ...
    def indicators(self) -> Any: ...
    def congress(self) -> Any: ...                      # senate/house FMP-http provider
    def price_at_date(self, symbol: str, as_of: Optional[datetime]) -> Optional[float]: ...


class LiveProviderBundle:
    """Live bundle: resolves providers via an injected get_provider callable.

    The host (or the test harness) passes get_provider so ba2_common keeps no
    edge to ba2_providers. price_at_date resolves the as_of close via the ohlcv
    provider's get()/get_ohlcv_data (Decision 1: one price source for all experts)."""

    def __init__(self, get_provider: Callable[..., Any]):
        self._get = get_provider

    def ohlcv(self): return self._get("ohlcv", "fmp")
    def fundamentals_details(self): return self._get("fundamentals_details", "fmp")
    def fundamentals_overview(self): return self._get("fundamentals_overview", "fmp")
    def insider(self): return self._get("insider", "fmp")
    def news(self): return self._get("news", "fmp")
    def indicators(self): return self._get("indicators", "pandas")
    def congress(self): return self._get("congress", "fmp")

    def price_at_date(self, symbol: str, as_of: Optional[datetime]) -> Optional[float]:
        prov = self.ohlcv()
        df = prov.get_ohlcv_data(symbol, end_date=as_of, lookback_days=7, interval="1d")
        if df is None or getattr(df, "empty", True):
            return None
        return float(df["Close"].iloc[-1])


@dataclass
class BacktestContext:
    """Carries everything analyze_as_of needs, set from OUTSIDE the expert."""
    providers: ProviderBundle
    settings: Dict[str, Any]                    # resolved + optimizer-overridden per trial
    as_of: Optional[datetime] = None
    account: Any = None                         # BacktestAccount (Phase 4); None in golden test
    subtype: Any = None                         # AnalysisUseCase for subtype-aware experts
    extra: Dict[str, Any] = field(default_factory=dict)
```

> Re-plan checkpoint: the provider registry **category keys** (`"ohlcv"`, `"fundamentals_details"`, `"congress"`, `"indicators"`/`"pandas"`) must match the Phase-0 `ba2_providers.get_provider` registry. Confirm the actual keys with `grep -n "PROVIDERS = \|def get_provider" ba2_providers/__init__.py` and adjust `LiveProviderBundle` to the real keys before running Task 12.

- [ ] **Step 3: Write the value-object test**

`BA2TradeCommon/tests/test_recommendation.py`:

```python
from ba2_common.core.types import Recommendation, OrderRecommendation


def test_recommendation_skip_is_first_class():
    r = Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0,
                       current_price=10.0, skip=True, skip_reason="no coverage")
    assert r.skip is True and r.skip_reason == "no coverage"


def test_almost_equals_float_tolerant():
    a = Recommendation(OrderRecommendation.BUY, 78.10000001, 100.0, "x", 8.0)
    b = Recommendation(OrderRecommendation.BUY, 78.1, 100.0, "x", 8.0)
    assert a.almost_equals(b)


def test_almost_equals_detects_signal_drift():
    a = Recommendation(OrderRecommendation.BUY, 78.1, 100.0, "x", 8.0)
    b = Recommendation(OrderRecommendation.HOLD, 78.1, 100.0, "x", 8.0)
    assert not a.almost_equals(b)


def test_live_provider_bundle_price_at_date(monkeypatch):
    import pandas as pd
    from ba2_common.core.backtest_context import LiveProviderBundle

    class FakeOHLCV:
        def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
            return pd.DataFrame({"Close": [9.0, 10.5]})
    bundle = LiveProviderBundle(lambda cat, name, **kw: FakeOHLCV())
    assert bundle.price_at_date("AAPL", None) == 10.5
```

- [ ] **Step 4: Run + commit**

```bash
/tmp/v_p1/bin/pip install -q -e BA2TradeCommon
/tmp/v_p1/bin/python -m pytest BA2TradeCommon/tests/test_recommendation.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -am "feat(common): Recommendation value object + ProviderBundle/BacktestContext"
```
Expected: PASS.

---

## Task 2: `BacktestInterface` + `analyze_as_of` seam on `MarketExpertInterface`

**Files:**
- Edit `BA2TradeCommon/ba2_common/core/interfaces/MarketExpertInterface.py` (add the seam; do NOT remove `run_analysis`)
- Edit `BA2TradeCommon/ba2_common/core/interfaces/__init__.py` (export `BacktestInterface`)
- Test: `BA2TradeCommon/tests/test_backtest_interface.py`

- [ ] **Step 1: Add the `BacktestInterface` protocol + default `analyze_as_of`/`_gather`/`_process` hooks**

In `ba2_common/core/interfaces/MarketExpertInterface.py`, add imports near the top:

```python
from ..types import Recommendation
from ..backtest_context import BacktestContext, ProviderBundle
```

Then add to the `MarketExpertInterface` class body (these are the seam every expert overrides; the bases raise so an un-refactored expert fails loudly rather than silently diverging):

```python
    # ---- Backtest contract (Phase 1) ---------------------------------
    def _gather(self, providers: "ProviderBundle", as_of: "Optional[datetime]") -> Dict[str, Any]:
        """Pull every datum this expert needs via providers (never raw HTTP/DB).
        as_of=None => latest (live). Returns the data_bundle, which ALWAYS carries
        current_price (the as_of close). Subclasses MUST override."""
        raise NotImplementedError(f"{type(self).__name__} has not implemented _gather (Phase 1)")

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: "Optional[datetime]" = None) -> "Recommendation":
        """PURE decision logic. settings is a fully-resolved plain dict (no self.* config
        reads). as_of is used only where the logic needs 'now' for date math. Subclasses
        MUST override."""
        raise NotImplementedError(f"{type(self).__name__} has not implemented _process (Phase 1)")

    def analyze_as_of(self, as_of: "datetime", context: "BacktestContext") -> "Recommendation":
        """The single BacktestInterface method. Runs the SAME _gather+_process as live."""
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    def _resolve_settings(self, keys) -> Dict[str, Any]:
        """Resolve the given setting keys to a plain dict via the live default-resolver.
        Used by live run_analysis to build the settings dict _process consumes, so
        _process never touches self for config (matches optimizer-override flow)."""
        return {k: self.get_setting_with_interface_default(k) for k in keys}
```

Add `from datetime import datetime` and `from typing import ... Dict, Any` to the existing imports if not present.

- [ ] **Step 2: Define the `BacktestInterface` protocol object for export/typing**

Append at module end of `MarketExpertInterface.py`:

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class BacktestInterface(Protocol):
    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation: ...
```

- [ ] **Step 3: Export from `interfaces/__init__.py`**

Add to `ba2_common/core/interfaces/__init__.py`:

```python
from .MarketExpertInterface import MarketExpertInterface, BacktestInterface
```
(Keep the existing `MarketExpertInterface` export if already there; ensure `BacktestInterface` is added to `__all__` if `__all__` is defined.)

- [ ] **Step 4: Write the seam test**

`BA2TradeCommon/tests/test_backtest_interface.py`:

```python
import pytest
from datetime import datetime, timezone
from ba2_common.core.interfaces import MarketExpertInterface, BacktestInterface
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import Recommendation, OrderRecommendation


class _StubExpert(MarketExpertInterface):
    SETTING_MODEL = None  # not used in this stub path
    def __init__(self): pass
    @classmethod
    def description(cls): return "stub"
    def render_market_analysis(self, ma): return ""
    def _gather(self, providers, as_of):
        return {"current_price": 100.0, "as_of": as_of}
    def _process(self, bundle, settings, as_of=None):
        return Recommendation(OrderRecommendation.BUY, 70.0, bundle["current_price"],
                              "stub-details", settings.get("expected_profit_percent"))


def test_analyze_as_of_runs_gather_then_process():
    ctx = BacktestContext(providers=LiveProviderBundle(lambda *a, **k: None),
                          settings={"expected_profit_percent": 5.0},
                          as_of=datetime(2026, 6, 13, tzinfo=timezone.utc))
    rec = _StubExpert().analyze_as_of(ctx.as_of, ctx)
    assert isinstance(rec, BacktestInterface) is False  # rec is a value object, not the iface
    assert rec.signal == OrderRecommendation.BUY and rec.expected_profit_percent == 5.0


def test_unrefactored_expert_fails_loud():
    class Bad(MarketExpertInterface):
        def __init__(self): pass
        @classmethod
        def description(cls): return "bad"
        def render_market_analysis(self, ma): return ""
    with pytest.raises(NotImplementedError):
        Bad()._gather(None, None)
```

- [ ] **Step 5: Run + commit**

```bash
/tmp/v_p1/bin/pip install -q -e BA2TradeCommon
/tmp/v_p1/bin/python -m pytest BA2TradeCommon/tests/test_backtest_interface.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -am "feat(common): BacktestInterface + analyze_as_of/_gather/_process seam on MarketExpertInterface"
```
Expected: PASS.

> Re-plan checkpoint: `_StubExpert` bypasses `MarketExpertInterface.__init__` (which reads the DB for builtin settings). If the real `__init__` is required for the stub (e.g. abstract-method enforcement changed in Phase 0), construct it with a configured temp DB via the `conftest` fixture instead. Confirm the abstract-method set (`description`, `render_market_analysis`) matches the Phase-0 interface.

---

## Task 3: Native cache substrate — `provider_cache` model + `CachedProviderMixin`

**Files:**
- Create `BA2TradeCommon/ba2_common/core/provider_cache_model.py` (SQLModel `ProviderCache` index table)
- Create `BA2TradeProviders/ba2_providers/cache/__init__.py`, `BA2TradeProviders/ba2_providers/cache/native_cache.py` (parquet time-series store + SQLite event store + read/write path)
- Edit `BA2TradeCommon/ba2_common/core/provider_utils.py` (add `effective_date` helpers + lookahead-lag constants)
- Test: `BA2TradeProviders/tests/test_native_cache.py`

- [ ] **Step 1: Define the `ProviderCache` SQLModel index table**

`BA2TradeCommon/ba2_common/core/provider_cache_model.py` (one generic table per SHARED CONTRACT `native_cache.storage`; lives in `ba2_common` so it registers in the configured DB alongside the other models):

```python
"""Generic event/record cache index — generalizes BA2TestPlatform NewsCache.

Time-series data (ohlcv, indicators) lives in parquet (see ba2_providers cache);
event/record data (insider, fundamentals, news, estimates) is indexed here with
large payloads spilled to disk JSON. EVERY row carries effective_date (when the
datum became PUBLIC) distinct from value_date (what the datum is ABOUT)."""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Index


class ProviderCache(SQLModel, table=True):
    __tablename__ = "provider_cache"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)            # e.g. FMPInsiderProvider
    data_type: str = Field(index=True)           # insider_txn|balance_sheet|income_stmt|...
    symbol: str = Field(index=True)
    frequency: Optional[str] = Field(default=None)   # quarterly/annual/None
    value_date: datetime                          # fiscalDateEnding / transactionDate / event time
    effective_date: datetime                      # fillingDate / filingDate / publishedDate / report date
    payload_hash: str = Field(index=True)         # sha256 of canonical payload (dedupe key)
    content_file_path: Optional[str] = Field(default=None)   # spill for large payloads
    raw_json: Optional[str] = Field(default=None)            # inline for small payloads
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        Index("ix_provcache_lookup", "provider", "data_type", "symbol", "effective_date"),
        Index("ix_provcache_value", "data_type", "symbol", "value_date"),
        Index("ix_provcache_dedupe", "provider", "data_type", "symbol", "payload_hash", unique=True),
    )
```

Then ensure it is imported by `db.init_db()` table registration — add `from . import provider_cache_model  # noqa` to the lazy `from . import models` block in `db.py` `init_db`, OR import it inside `models.py` so `SQLModel.metadata` sees it. Confirm against the Phase-0 `init_db` body.

- [ ] **Step 2: Add effective-date helpers + lag constants to `provider_utils.py`**

In `ba2_common/core/provider_utils.py` add:

```python
from datetime import datetime, timedelta, timezone

# Reporting-lag fallbacks (Decision 7) — documented approximations.
INSIDER_FILING_LAG_DAYS = 2        # Form-4 publication lag when filingDate is missing
STATEMENT_REPORTING_LAG_DAYS = 75  # 10-Q/10-K filing lag when fillingDate/acceptedDate missing


def parse_provider_date(value, default=None):
    """Robustly parse an FMP date string (YYYY-MM-DD[THH:MM:SS]) to tz-aware UTC."""
    if not value:
        return default
    try:
        dt = datetime.fromisoformat(str(value).split("T")[0])
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, AttributeError):
        return default


def insider_effective_date(txn: dict):
    """filingDate (Form-4 publication) <= as_of; fallback transactionDate + lag."""
    eff = parse_provider_date(txn.get("filingDate"))
    if eff is not None:
        return eff
    txn_date = parse_provider_date(txn.get("transactionDate"))
    return txn_date + timedelta(days=INSIDER_FILING_LAG_DAYS) if txn_date else None


def statement_effective_date(row: dict):
    """SEC fillingDate else acceptedDate else fiscalDateEnding + reporting-lag."""
    for key in ("fillingDate", "filingDate", "acceptedDate"):
        eff = parse_provider_date(row.get(key))
        if eff is not None:
            return eff
    fde = parse_provider_date(row.get("fiscalDateEnding") or row.get("date"))
    return fde + timedelta(days=STATEMENT_REPORTING_LAG_DAYS) if fde else None
```

> Re-plan checkpoint: confirm `provider_utils.py` already imports `datetime`/`timedelta` (it has `validate_date_range`/`calculate_date_range`). FMP field-name casing is `fillingDate` (FMP's known typo) on statements and `filingDate` on insider — both are checked above. **Verify against a live FMP probe (`test_files/` ad-hoc script) that statement rows actually carry `fillingDate`** before relying on it; if not present universally, the `STATEMENT_REPORTING_LAG_DAYS` fallback covers the gap (documented).

- [ ] **Step 3: Implement the native cache (parquet + SQLite read/write path)**

`BA2TradeProviders/ba2_providers/cache/native_cache.py`:

```python
"""Native as_of cache: parquet for time-series, ProviderCache(SQLite) for events.

read_path (mirrors BA2TestPlatform base.py range-coverage, but on effective_date):
  validate dates -> lookup rows effective_date<=as_of within value_date window ->
  on miss/coverage-gap (cached max effective_date < as_of) call the injected
  fetch_impl, upsert by payload_hash (dedupe), then filter+format.
Historical as_of (< today - settle_lag): immutable, no TTL.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ba2_common.config import CACHE_FOLDER
from ba2_common.core.db import get_db
from ba2_common.core.provider_cache_model import ProviderCache
from ba2_common.logger import logger

_CACHE_ROOT = os.path.join(CACHE_FOLDER, "datasets", "cache")
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _spill_path(data_type: str, provider: str, h: str) -> str:
    d = os.path.join(_CACHE_ROOT, data_type, provider)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{h}.json")


def upsert_event_rows(provider: str, data_type: str, symbol: str,
                      rows: List[dict], value_date_fn: Callable[[dict], Optional[datetime]],
                      effective_date_fn: Callable[[dict], Optional[datetime]],
                      frequency: Optional[str] = None, spill_threshold: int = 4000) -> int:
    """Dedupe-upsert event rows into ProviderCache. Returns count written."""
    written = 0
    with _lock_for(f"{provider}:{data_type}:{symbol}"):
        session = get_db()
        try:
            for row in rows:
                vd, ed = value_date_fn(row), effective_date_fn(row)
                if vd is None or ed is None:
                    continue
                h = _payload_hash(row)
                exists = session.query(ProviderCache).filter_by(
                    provider=provider, data_type=data_type, symbol=symbol, payload_hash=h
                ).first() if hasattr(session, "query") else None
                if exists:
                    continue
                raw = json.dumps(row, default=str)
                cfp = None
                if len(raw) > spill_threshold:
                    cfp = _spill_path(data_type, provider, h)
                    with open(cfp, "w") as f:
                        f.write(raw)
                    raw = None
                session.add(ProviderCache(
                    provider=provider, data_type=data_type, symbol=symbol, frequency=frequency,
                    value_date=vd, effective_date=ed, payload_hash=h,
                    content_file_path=cfp, raw_json=raw, fetched_at=datetime.now(timezone.utc)))
                written += 1
            session.commit()
        finally:
            session.close()
    return written


def read_event_rows(provider: str, data_type: str, symbol: str,
                    as_of: Optional[datetime], value_from: Optional[datetime] = None) -> List[dict]:
    """Return cached rows with effective_date<=as_of (no-lookahead) within value window,
    newest value_date first. as_of=None => no effective_date ceiling (live latest)."""
    session = get_db()
    try:
        from sqlmodel import select
        stmt = select(ProviderCache).where(
            ProviderCache.provider == provider,
            ProviderCache.data_type == data_type,
            ProviderCache.symbol == symbol)
        if as_of is not None:
            stmt = stmt.where(ProviderCache.effective_date <= as_of)
        if value_from is not None:
            stmt = stmt.where(ProviderCache.value_date >= value_from)
        stmt = stmt.order_by(ProviderCache.value_date.desc())
        rows = session.exec(stmt).all()
    finally:
        session.close()
    out = []
    for r in rows:
        raw = r.raw_json
        if raw is None and r.content_file_path and os.path.exists(r.content_file_path):
            with open(r.content_file_path) as f:
                raw = f.read()
        if raw:
            out.append(json.loads(raw))
    return out


# ---- parquet time-series (ohlcv, indicators) -------------------------
def timeseries_path(provider: str, symbol: str, interval: str) -> str:
    d = os.path.join(CACHE_FOLDER, provider)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol.upper()}_{interval}.parquet")


def read_timeseries(provider: str, symbol: str, interval: str,
                    as_of: Optional[datetime]):
    """Read a parquet time-series sliced to effective_date<=as_of. None on miss."""
    import pandas as pd
    path = timeseries_path(provider, symbol, interval)
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if as_of is not None and "effective_date" in df.columns:
        df = df[pd.to_datetime(df["effective_date"]) <= as_of]
    return df


def write_timeseries(provider: str, symbol: str, interval: str, df) -> None:
    """Atomic temp+rename parquet write. df MUST carry an effective_date column
    (for OHLCV effective_date == bar Date)."""
    import pandas as pd
    path = timeseries_path(provider, symbol, interval)
    with _lock_for(path):
        tmp = path + ".tmp"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
```

- [ ] **Step 4: Add cache-hit counters for the test assertions**

In `native_cache.py` add a module-level counter the read paths bump (used by Task 11's cache-hit assertions):

```python
class _Stats:
    hits = 0
    misses = 0
    fetches = 0

STATS = _Stats()

def reset_stats():
    STATS.hits = STATS.misses = STATS.fetches = 0
```
Bump `STATS.hits` when `read_event_rows`/`read_timeseries` return cached data and `STATS.misses`/`STATS.fetches` when they fall through to a fetch (wire these in the per-category `get()` in Task 4, where the fetch_impl is actually called).

- [ ] **Step 5: Write the native-cache test**

`BA2TradeProviders/tests/test_native_cache.py`:

```python
from datetime import datetime, timezone
import pandas as pd
import pytest

from ba2_providers.cache import native_cache as nc
from ba2_common.core.provider_utils import insider_effective_date, parse_provider_date


def test_event_upsert_and_no_lookahead_read():
    rows = [
        {"insider_name": "A", "transactionDate": "2026-01-05", "filingDate": "2026-01-08", "v": 1},
        {"insider_name": "B", "transactionDate": "2026-02-01", "filingDate": "2026-02-20", "v": 2},
    ]
    nc.upsert_event_rows(
        "FMPInsiderProvider", "insider_txn", "AAPL", rows,
        value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
        effective_date_fn=insider_effective_date)
    # as_of before B's filingDate => only A is knowable
    as_of = datetime(2026, 2, 10, tzinfo=timezone.utc)
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "AAPL", as_of)
    names = {r["insider_name"] for r in got}
    assert names == {"A"}, f"lookahead leak: {names}"


def test_event_upsert_dedupes_by_payload_hash():
    row = {"insider_name": "C", "transactionDate": "2026-03-01", "filingDate": "2026-03-02", "v": 9}
    nc.upsert_event_rows("FMPInsiderProvider", "insider_txn", "TSLA", [row],
                         value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
                         effective_date_fn=insider_effective_date)
    n2 = nc.upsert_event_rows("FMPInsiderProvider", "insider_txn", "TSLA", [row],
                              value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
                              effective_date_fn=insider_effective_date)
    assert n2 == 0  # second write is a no-op (dedupe)


def test_timeseries_asof_slice(tmp_path, monkeypatch):
    df = pd.DataFrame({"Date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
                       "Close": [10, 11, 12]})
    df["effective_date"] = df["Date"]
    nc.write_timeseries("FMPOHLCVProvider", "AAPL", "1d", df)
    sliced = nc.read_timeseries("FMPOHLCVProvider", "AAPL", "1d",
                                datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert list(sliced["Close"]) == [10, 11]
```

The `conftest.py` (from Phase 0) already calls `db.configure_db(temp)`; add a `CACHE_FOLDER` redirect to it (see Step 6).

- [ ] **Step 6: Redirect `CACHE_FOLDER` to a temp dir in the providers conftest**

In `BA2TradeProviders/tests/conftest.py`, extend the session fixture so cache writes never touch the real `~/Documents/.../cache`:

```python
import os, tempfile
@pytest.fixture(scope="session", autouse=True)
def _isolated_cache():
    tmp = tempfile.mkdtemp()
    import ba2_common.config as cfg
    cfg.CACHE_FOLDER = tmp
    # native_cache binds CACHE_FOLDER at import; rebind its module constant too
    import ba2_providers.cache.native_cache as nc
    nc._CACHE_ROOT = os.path.join(tmp, "datasets", "cache")
    yield
```
(Merge with the existing `configure_db` fixture body rather than duplicating it.)

- [ ] **Step 7: Run + commit**

```bash
/tmp/v_p1/bin/pip install -q -e BA2TradeCommon -e "BA2TradeProviders[dev]"
/tmp/v_p1/bin/python -m pytest BA2TradeProviders/tests/test_native_cache.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -am "feat(common): ProviderCache index model + effective-date helpers"
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -am "feat(providers): native as_of cache (parquet time-series + SQLite event store)"
```
Expected: PASS — especially `test_event_upsert_and_no_lookahead_read` (the no-lookahead invariant).

---

## Task 4: Uniform `get(as_of)` wrapper per category + fix the two lookahead bugs

**Files (edit the copied providers in `ba2_providers`):**
- `BA2TradeProviders/ba2_providers/insider/FMPInsiderProvider.py` (bug fix #1)
- `BA2TradeProviders/ba2_providers/fundamentals/details/FMPCompanyDetailsProvider.py` (bug fix #2)
- `BA2TradeProviders/ba2_providers/ohlcv/FMPOHLCVProvider.py`, `news/FMPNewsProvider.py`, `indicators/*` (cache-back `get()`)
- Create `BA2TradeProviders/ba2_providers/cache/cached_get.py` (the shared `get()` alias layer)
- Test: `BA2TradeProviders/tests/test_provider_asof.py` (extended in Task 11)

- [ ] **Step 1: Fix insider lookahead bug (`transactionDate` → `filingDate`)**

In `ba2_providers/insider/FMPInsiderProvider.py` `get_insider_transactions`, the date filter currently keys on `transactionDate` (live ref `FMPInsiderProvider.py:133,146`). Add an `as_of` param and switch the no-lookahead filter to `effective_date` (filingDate) **only when `as_of` is set**, preserving the live `transactionDate`-range behaviour when `as_of=None`:

```python
from ba2_common.core.provider_utils import insider_effective_date, parse_provider_date

def get_insider_transactions(self, symbol, end_date, start_date=None,
                             lookback_days=None, as_of=None, format_type="dict"):
    ...  # existing arg validation + range calc unchanged
    for transaction in insider_data:
        trans_date = parse_provider_date(transaction.get("transactionDate"))
        if trans_date is None:
            continue
        # No-lookahead anchor: when as_of is set, the trade is only knowable once
        # its Form-4 filingDate <= as_of (LOOKAHEAD BUG FIX). transactionDate stays
        # the value/display date and the lower-bound of the requested window.
        if as_of is not None:
            eff = insider_effective_date(transaction)
            if eff is None or eff > as_of:
                continue
        if actual_start_date <= trans_date <= end_date:
            filtered_transactions.append(transaction)
            ...  # value accumulation unchanged
```

> Re-plan checkpoint: live ref `FMPInsiderProvider.py:117` calls `fmpsdk.insider_trading(symbol=...)` with **no date params** (it fetches all, filters client-side). Confirm that is still true post-Phase-0 codemod; the `as_of` filter is purely a client-side addition, so `as_of=None` is byte-identical to today.

- [ ] **Step 2: Fix statements lookahead bug (`fiscalDateEnding` → `fillingDate`/`acceptedDate`)**

In `ba2_providers/fundamentals/details/FMPCompanyDetailsProvider.py`, the statement filters (`get_balance_sheet`/`get_income_statement`/`get_cashflow_statement`, live refs around lines 79-322) select "most recent statement as of end_date" on `fiscalDateEnding`. Add an `as_of` param threaded to the shared filter helper, and when `as_of` is set, require `statement_effective_date(row) <= as_of`:

```python
from ba2_common.core.provider_utils import statement_effective_date

# inside the shared filter (the function called at lines ~123/223/315 with
# (data, end_date, start_date, lookback_periods)): add as_of and apply:
if as_of is not None:
    data = [r for r in data
            if (eff := statement_effective_date(r)) is not None and eff <= as_of]
# then keep the existing fiscalDateEnding<=end_date + lookback_periods slicing.
```
`get_past_earnings` already anchors on the report/announcement date (per SHARED CONTRACT) — add the `as_of` param and pass it through, but its existing report-date filter is already point-in-time-safe; just plumb `as_of` so callers can pass it.

> Re-plan checkpoint: identify the **actual** shared filter helper name in the post-Phase-0 file (the recon shows the three public methods call a common slicer at lines ~123/223/315). Add `as_of` to that helper's signature and all three call sites. Confirm `fillingDate` presence on the live FMP statement rows with a probe; rely on the `STATEMENT_REPORTING_LAG_DAYS` fallback otherwise.

- [ ] **Step 3: Add the shared `get()` alias layer**

`BA2TradeProviders/ba2_providers/cache/cached_get.py` — the rename/alias + no-lookahead-enforcement wrapper (SHARED CONTRACT `provider_asof.uniform_contract`; it is an ALIAS, not a rewrite — it normalizes `as_of`/`lookback` to each category's native param and routes through `native_cache`):

```python
"""Uniform get(symbol, as_of=None, lookback=..., field=None, format_type='dict')
across provider categories. Maps to each category's existing native param:
  OHLCV/Indicators/News/Insider: as_of->end_date, lookback->lookback_days
  Fundamentals statements:       as_of->end_date, lookback->lookback_periods
as_of=None => latest (live, UNCHANGED). Screener is EXCLUDED (no temporal param).
"""
from datetime import datetime, timezone
from typing import Any, Optional


def ohlcv_get(provider, symbol, as_of=None, lookback=400, interval="1d", format_type="dict"):
    end = as_of or datetime.now(timezone.utc)
    return provider.get_ohlcv_data(symbol, end_date=end, lookback_days=lookback, interval=interval)


def insider_get(provider, symbol, as_of=None, lookback=30, format_type="dict"):
    end = as_of or datetime.now(timezone.utc)
    return provider.get_insider_transactions(symbol, end_date=end, lookback_days=lookback,
                                             as_of=as_of, format_type=format_type)


def statement_get(provider, symbol, statement, as_of=None, frequency="annual",
                  lookback_periods=1, format_type="dict"):
    end = as_of or datetime.now(timezone.utc)
    fn = getattr(provider, f"get_{statement}")   # balance_sheet|income_statement|cashflow_statement
    return fn(symbol, frequency, end, lookback_periods=lookback_periods,
              as_of=as_of, format_type=format_type)


def past_earnings_get(provider, symbol, as_of=None, frequency="quarterly",
                      lookback_periods=1, format_type="dict"):
    end = as_of or datetime.now(timezone.utc)
    return provider.get_past_earnings(symbol, frequency=frequency, end_date=end,
                                      lookback_periods=lookback_periods, format_type=format_type)
```

> Re-plan checkpoint: the design's ideal end-state is a single `get()` method ON `CachedProviderMixin`. Phase 1 ships the alias functions (above) because they require zero changes to the ~80%-correct existing signatures and keep `as_of=None` byte-identical. Promoting them to a mixin method is a mechanical follow-up; do NOT block Phase 1 on it. Confirm `get_indicator`/news signatures and add `indicator_get`/`news_get` mirroring the above if the experts in Tasks 5-10 need them (EarningsDrift/Insider/Senate/Rating do not; FactorRanker uses ohlcv/statements/overview/past_earnings/estimates).

- [ ] **Step 4: Document the as_of-ignored / lookahead-limited providers**

Add a module docstring note (or `# AS_OF LIMITATION:` comments) in:
- `screener/FMPScreenerProvider.py`: "live-only, as_of ignored — historical screening is Phase 3."
- `fundamentals/overview/FMPCompanyOverviewProvider.py` and any financial-ratios path: "as_of_date-native but live TTM 'now' snapshot; true point-in-time requires the effective-date cache (documented limitation)."
- FMPRating's consensus endpoints (handled in Task 10): "no per-row date; latest snapshot regardless of as_of — documented backtest lookahead."

- [ ] **Step 5: Write the as_of byte-equality + lookahead-fix test**

`BA2TradeProviders/tests/test_provider_asof.py`:

```python
from datetime import datetime, timezone
from unittest.mock import patch
from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider

FAKE = [
    {"insider_name": "A", "transactionType": "P-Purchase", "transactionDate": "2026-01-05",
     "filingDate": "2026-01-08", "securitiesTransacted": "1000", "price": "10"},
    {"insider_name": "B", "transactionType": "P-Purchase", "transactionDate": "2026-01-06",
     "filingDate": "2026-03-01", "securitiesTransacted": "1000", "price": "10"},  # filed late
]

def _prov():
    p = FMPInsiderProvider.__new__(FMPInsiderProvider)
    p.api_key = "x"
    return p

def test_asof_none_matches_pre_refactor_transactiondate_filter():
    """as_of=None keeps the live transactionDate-range behaviour (byte-equal): both A,B in Jan."""
    with patch("ba2_providers.insider.FMPInsiderProvider.fmpsdk.insider_trading", return_value=FAKE):
        out = _prov().get_insider_transactions("AAPL", end_date=datetime(2026, 1, 31, tzinfo=timezone.utc),
                                               lookback_days=60, as_of=None, format_type="dict")
    assert out["transaction_count"] == 2  # B counts: its transactionDate is in range

def test_asof_enforces_filingdate_no_lookahead():
    """as_of mid-Feb: B's filingDate (Mar 1) is AFTER as_of => excluded (bug fix)."""
    with patch("ba2_providers.insider.FMPInsiderProvider.fmpsdk.insider_trading", return_value=FAKE):
        out = _prov().get_insider_transactions("AAPL", end_date=datetime(2026, 2, 15, tzinfo=timezone.utc),
                                               lookback_days=60, as_of=datetime(2026, 2, 15, tzinfo=timezone.utc),
                                               format_type="dict")
    names = {t["insider_name"] for t in out["transactions"]}
    assert names == {"A"}, f"filingDate lookahead leak: {names}"
```

- [ ] **Step 6: Run + commit**

```bash
/tmp/v_p1/bin/pip install -q -e "BA2TradeProviders[dev]"
/tmp/v_p1/bin/python -m pytest BA2TradeProviders/tests/test_provider_asof.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -am "feat(providers): uniform get(as_of) alias + fix insider/statement lookahead bugs (as_of-gated)"
```
Expected: PASS — `test_asof_none_matches_pre_refactor` proves no live-behaviour change; `test_asof_enforces_filingdate_no_lookahead` proves the corrected anchor.

> Re-plan checkpoint: the byte-equality assertion here is an in-test mock. The **authoritative** byte-equality gate (real FMP fetch vs cache) is a separate `test_files/`-style probe in Task 11 Step 3 — run it against a live key once, snapshot the result, and assert equality. Keep it out of the unit suite (network).

---

## Task 5: Refactor FMPEarningsDrift (`_gather`/`_process`) — clean expert #1

**Files:**
- `BA2TradeExperts/ba2_experts/FMPEarningsDrift.py`
- Test: `BA2TradeExperts/tests/test_earnings_drift_gather_process.py`

This is the template all other experts follow. `evaluate_earnings_drift` is already pure (live ref `FMPEarningsDrift.py:26-95`) — keep it verbatim. Split the orchestration.

- [ ] **Step 1: Add `_gather`**

In `ba2_experts/FMPEarningsDrift.py`, add (replacing the live-only `_fetch_latest_earnings` which hardcoded `datetime.now`):

```python
from ba2_common.core.types import Recommendation, OrderRecommendation
from ba2_common.core.backtest_context import ProviderBundle
from ba2_providers.cache.cached_get import past_earnings_get

    _SETTING_KEYS = ("surprise_min_pct", "max_days_since_report", "expected_profit_percent")

    def _gather(self, providers: ProviderBundle, as_of):
        symbol = self._gather_symbol            # set by _gather caller (see Step 3)
        details_provider = providers.fundamentals_details()
        data = past_earnings_get(details_provider, symbol, as_of=as_of,
                                 frequency="quarterly", lookback_periods=1, format_type="dict")
        latest = None
        if isinstance(data, dict):
            earnings = data.get("earnings") or []
            latest = earnings[0] if earnings else None
        current_price = providers.price_at_date(symbol, as_of)
        return {"latest_earnings": latest, "current_price": current_price, "symbol": symbol}
```

- [ ] **Step 2: Add `_process` (pure)**

```python
    def _process(self, data_bundle, settings, as_of=None) -> Recommendation:
        from datetime import datetime, timezone
        now = as_of or datetime.now(timezone.utc)
        surprise_min = float(settings["surprise_min_pct"])
        max_days = int(settings["max_days_since_report"])
        expected_profit = float(settings["expected_profit_percent"])
        result = evaluate_earnings_drift(data_bundle["latest_earnings"], now, surprise_min, max_days)

        if result["is_signal"]:
            signal, confidence, expected = OrderRecommendation.BUY, result["confidence"], expected_profit
        else:
            signal, confidence, expected = OrderRecommendation.HOLD, 10.0, 0.0

        current_price = data_bundle["current_price"]
        symbol = data_bundle["symbol"]
        details = f"""Post-Earnings-Drift Analysis for {symbol}

Latest report: {result['report_date'] or 'N/A'} ({result['days_since_report']} days ago)
Reported EPS: {result['reported_eps']}  vs estimate {result['estimated_eps']}
EPS surprise: {result['surprise_pct']}% (threshold {surprise_min}%, freshness window {max_days}d)
Verdict: {result['reason']}

Recommendation: {signal.value}
Confidence: {confidence:.1f}%
"""
        return Recommendation(
            signal=signal, confidence=round(confidence, 1), current_price=current_price,
            details=details, expected_profit_percent=expected,
            raw_outputs={"name": "Earnings Drift Analysis", "type": "earnings_drift_analysis",
                         "text": details, "evaluation": {k: result[k] for k in (
                             "is_signal", "surprise_pct", "days_since_report", "report_date",
                             "reported_eps", "estimated_eps", "reason")}})
```

> Detail-string parity is load-bearing for the golden test: keep the f-string **byte-identical** to the live `run_analysis` `details` block (live ref `FMPEarningsDrift.py:170-179`) so `rec_live.details == rec_asof.details`.

- [ ] **Step 3: Rewrite live `run_analysis` as the thin orchestrator**

Replace the body of `run_analysis` (live ref lines 147-240) with the orchestrator from SHARED CONTRACT `live_run_analysis_refactor`. It keeps the signature `(self, symbol, market_analysis)` (WorkerQueue caller unchanged) and the full lifecycle:

```python
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        from datetime import datetime, timezone
        from ba2_common.core.backtest_context import LiveProviderBundle
        self.logger.info(f"Starting earnings-drift analysis for {symbol} (Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            settings = self._resolve_settings(self._SETTING_KEYS)
            self._gather_symbol = symbol
            providers = self._live_providers()       # see Step 4
            bundle = self._gather(providers, as_of=None)
            if not bundle.get("current_price"):
                raise ValueError(f"Unable to get current price for {symbol}")
            rec = self._process(bundle, settings, as_of=None)

            recommendation_id = add_instance(ExpertRecommendation(
                instance_id=self.id, symbol=symbol, recommended_action=rec.signal,
                expected_profit_percent=rec.expected_profit_percent, price_at_date=rec.current_price,
                details=rec.details, confidence=round(rec.confidence, 1),
                risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM,
                market_analysis_id=market_analysis.id, created_at=datetime.now(timezone.utc)))

            session = get_db()
            try:
                session.add(AnalysisOutput(
                    market_analysis_id=market_analysis.id, name=rec.raw_outputs["name"],
                    type=rec.raw_outputs["type"], text=rec.raw_outputs["text"]))
                session.commit()
            finally:
                session.close()

            market_analysis.state = {"earnings_drift": {
                "recommendation": {"signal": rec.signal.value, "confidence": rec.confidence,
                                   "expected_profit_percent": rec.expected_profit_percent},
                "evaluation": rec.raw_outputs["evaluation"],
                "settings": {"surprise_min_pct": settings["surprise_min_pct"],
                             "max_days_since_report": settings["max_days_since_report"]},
                "expert_recommendation_id": recommendation_id,
                "current_price": rec.current_price,
                "analysis_timestamp": datetime.now(timezone.utc).isoformat()}}
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
        except Exception as e:
            self.logger.error(f"Earnings-drift analysis failed for {symbol}: {e}", exc_info=True)
            market_analysis.state = {"error": str(e),
                                     "error_timestamp": datetime.now(timezone.utc).isoformat(),
                                     "analysis_failed": True}
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            raise
```

> Note: EarningsDrift has no SKIP outcome; the `if rec.skip:` branch from the SHARED CONTRACT is a no-op here (added in FMPRating/FactorRanker, Tasks 9/10).

- [ ] **Step 4: Add a `_live_providers()` helper on the expert (or base)**

Add to `MarketExpertInterface` (so every expert reuses it; lives in `ba2_common`, takes the injected `get_provider` via the TradeConditions/host resolver to avoid a `ba2_common→ba2_providers` edge):

```python
    def _live_providers(self):
        """Build a LiveProviderBundle from the host-injected provider resolver.
        ba2_common never imports ba2_providers; the host wires the resolver in Phase 6.
        For the Phase-1 golden harness, the test injects get_provider directly."""
        from ..backtest_context import LiveProviderBundle
        from ..TradeConditions import _get_provider  # the Phase-0 injected resolver
        return LiveProviderBundle(lambda cat, name, **kw: _get_provider(cat, name, **kw))
```

> Re-plan checkpoint: Phase 0 added `TradeConditions.set_provider_resolver`/`_get_provider`. Confirm its signature is `(category, name, **kw)`; if Phase 0 used a different shape, adapt `_live_providers`. In Phase 1's golden test the resolver is set by the harness, NOT the live host (host wiring is Phase 6).

- [ ] **Step 5: Write the expert gather/process + golden-parity test**

`BA2TradeExperts/tests/test_earnings_drift_gather_process.py`:

```python
from datetime import datetime, timezone
import pandas as pd
from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)
SETTINGS = {"surprise_min_pct": 5.0, "max_days_since_report": 30, "expected_profit_percent": 8.0}

class FakeDetails:
    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
        return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2,
                              "estimated_eps": 1.0, "surprise_percent": 20.0}]}

class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})

def _get_provider(cat, name, **kw):
    return {"fundamentals_details": FakeDetails(), "ohlcv": FakeOHLCV()}[cat]

def _expert():
    e = FMPEarningsDrift.__new__(FMPEarningsDrift)
    e.id = 1
    e._gather_symbol = "AAPL"
    return e

def test_process_buy_on_fresh_beat():
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    assert 55.0 <= rec.confidence <= 100.0
    assert rec.current_price == 100.0
    assert rec.expected_profit_percent == 8.0

def test_gather_threads_as_of_into_provider(monkeypatch):
    captured = {}
    class Spy(FakeDetails):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            captured["end_date"] = end_date
            return super().get_past_earnings(symbol, frequency, end_date, lookback_periods, format_type)
    e = _expert()
    e._gather(LiveProviderBundle(lambda c, n, **k: Spy() if c == "fundamentals_details" else FakeOHLCV()), as_of=NOW)
    assert captured["end_date"] == NOW  # as_of threaded, not datetime.now()
```

- [ ] **Step 6: Run + commit**

```bash
/tmp/v_p1/bin/pip install -q -e BA2TradeCommon -e BA2TradeProviders -e "BA2TradeExperts[dev]"
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_earnings_drift_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FMPEarningsDrift into _gather/_process + analyze_as_of"
```
Expected: PASS.

---

## Task 6: Refactor FMPInsiderClusterBuy (`_gather`/`_process`) — clean expert #2

**Files:**
- `BA2TradeExperts/ba2_experts/FMPInsiderClusterBuy.py`
- Test: `BA2TradeExperts/tests/test_insider_cluster_gather_process.py`

`detect_insider_cluster` is already pure (live ref `FMPInsiderClusterBuy.py:25-75`) — keep verbatim. `_calculate_recommendation` (lines 125-164) is pure-ish (builds details) — keep but call it from `_process`.

- [ ] **Step 1: Add `_gather`** (uses the as_of-gated insider provider from Task 4)

```python
from ba2_common.core.types import Recommendation, OrderRecommendation
from ba2_providers.cache.cached_get import insider_get

    _SETTING_KEYS = ("lookback_days", "min_insiders", "min_total_value", "expected_profit_percent")

    def _gather(self, providers, as_of):
        symbol = self._gather_symbol
        lookback_days = int(self._gather_lookback_days)   # resolved by caller from settings
        insider_provider = providers.insider()
        insider_data = insider_get(insider_provider, symbol, as_of=as_of,
                                   lookback=lookback_days, format_type="dict")
        if not isinstance(insider_data, dict):
            insider_data = {"transactions": [], "start_date": "", "end_date": ""}
        current_price = providers.price_at_date(symbol, as_of)
        return {"insider_data": insider_data, "current_price": current_price, "symbol": symbol}
```

> `lookback_days` is a setting needed at gather time (it bounds the fetch window). Resolve it in the caller and stash it on `self._gather_lookback_days` (live) / pass via `context.settings` (backtest — `analyze_as_of` sets `self._gather_lookback_days = context.settings["lookback_days"]` before `_gather`). Document this two-value handoff in a comment.

- [ ] **Step 2: Add `_process`** (calls the existing `_calculate_recommendation`)

```python
    def _process(self, data_bundle, settings, as_of=None) -> Recommendation:
        rec = self._calculate_recommendation(
            data_bundle["insider_data"], int(settings["min_insiders"]),
            float(settings["min_total_value"]), float(settings["expected_profit_percent"]))
        return Recommendation(
            signal=rec["signal"], confidence=round(rec["confidence"], 1),
            current_price=data_bundle["current_price"], details=rec["details"],
            expected_profit_percent=rec["expected_profit_percent"],
            raw_outputs={"name": "Insider Cluster Analysis", "type": "insider_cluster_analysis",
                         "text": rec["details"], "cluster": rec["cluster"]})
```

- [ ] **Step 3: Rewrite live `run_analysis` as orchestrator** (mirror Task 5 Step 3: resolve settings, set `self._gather_symbol`/`self._gather_lookback_days`, `_gather(None)`, price guard, `_process`, persist `ExpertRecommendation`+`AnalysisOutput`, build `market_analysis.state["insider_cluster"]` byte-identical to live ref lines 216-239, `TimeHorizon.MEDIUM_TERM`). Keep the `state` dict shape exactly as the live version.

- [ ] **Step 4: Override `analyze_as_of` to set the gather-time lookback** (since lookback is needed before `_gather`):

```python
    def analyze_as_of(self, as_of, context):
        self._gather_symbol = context.extra.get("symbol", self._gather_symbol)
        self._gather_lookback_days = int(context.settings["lookback_days"])
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)
```

> Re-plan checkpoint: `context.extra["symbol"]` is how the engine (Phase 4) tells a per-symbol expert which symbol to analyze. In the Phase-1 golden test the harness sets `self._gather_symbol` directly. Confirm with Phase 4 how the symbol flows; if a dedicated `context.symbol` field is added later, prefer it.

- [ ] **Step 5: Test** `BA2TradeExperts/tests/test_insider_cluster_gather_process.py` (mirror Task 5 Step 5: fake insider provider returning 3 P-Purchase txns → assert BUY + buyer_count + confidence; fake returning 2 → HOLD; assert `_gather` threads `as_of`→`end_date`/`filingDate` filter; assert current_price from bundle). Reuse the `detect_insider_cluster` fixtures from the Phase-0 `test_clean_expert_calculators.py` for value parity.

- [ ] **Step 6: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_insider_cluster_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FMPInsiderClusterBuy into _gather/_process + analyze_as_of"
```
Expected: PASS.

---

## Task 7: Refactor FMPSenateTraderCopy + FMPSenateTraderWeight

**Files:**
- `BA2TradeExperts/ba2_experts/FMPSenateTraderCopy.py`, `FMPSenateTraderWeight.py`
- Test: `BA2TradeExperts/tests/test_senate_gather_process.py`

These are the two-stage gather cases (SHARED CONTRACT `data_bundle_shape.FMPSenateTraderCopy`/`FMPSenateTraderWeight`). The decision functions currently interleave fetches; `_gather` must pre-resolve the maps so `_process` is pure.

- [ ] **Step 1: FMPSenateTraderCopy `_gather`** — bundle `{senate_trades, house_trades, current_price_map, supported_symbols}`:

```python
    def _gather(self, providers, as_of):
        congress = providers.congress()
        senate = self._fetch_senate_trades(symbol=None) or []     # existing fetchers, now via congress provider
        house = self._fetch_house_trades(symbol=None) or []
        # supported_symbols + current_price_map are live concerns resolved here
        # (backtest: dataset-availability + as_of close). For golden test as_of=None == live.
        all_syms = {t.get("symbol") for t in (senate + house) if t.get("symbol")}
        current_price_map = {s: providers.price_at_date(s, as_of) for s in all_syms}
        supported = {s for s, p in current_price_map.items() if p is not None}
        return {"senate_trades": senate, "house_trades": house,
                "current_price_map": current_price_map, "supported_symbols": supported}
```

- [ ] **Step 2: FMPSenateTraderCopy `_process(bundle, settings, as_of)`** — subtype-aware (SHARED CONTRACT): `_filter_trades_by_age_multi(all_trades, now=as_of)` AND **drop trades with `disclosureDate`/`transactionDate > as_of`** (no-lookahead), `_find_copy_trades`, group by symbol, per-symbol `_generate_recommendations(..., subtype=context.subtype or AnalysisUseCase.ENTER_MARKET)`. Pass `now=as_of` into the existing `_filter_trades_by_age_multi` (live ref line 187 — change its hardcoded `datetime.now` to a `now` param defaulting to `now or datetime.now(...)`). Return a `Recommendation` (or list per symbol — see checkpoint).

> Re-plan checkpoint: FMPSenateTraderCopy emits per-symbol recommendations (it analyzes a basket, not one symbol). Confirm whether `_process` returns a single `Recommendation` (one symbol per call, engine loops) or a `List[Recommendation]`. The live `run_analysis` (lines 731+) writes multiple `ExpertRecommendation` rows. **Decision needed at execution:** keep `_process` returning `List[Recommendation]` for the multi-symbol experts (Senate*, FactorRanker) and have the golden harness compare the list element-wise; single-symbol experts return one. Update `analyze_as_of`'s return type note accordingly. This is open-question 1's sibling for Senate.

- [ ] **Step 3: FMPSenateTraderWeight `_gather`** — the two-stage pre-resolve (SHARED CONTRACT): bundle `{all_trades, current_price, exec_price_by_trade, trader_history_by_name}`. Pre-resolve `exec_price_by_trade` via the already-date-aware `_get_price_at_date` (live ref line 209) for each trade's exec_date, and `trader_history_by_name` via `_fetch_trader_history` (line 122) filtered to `<= as_of`. Move these fetches OUT of `_filter_trades`/`_calculate_recommendation` and INTO `_gather` so `_process` is pure:

```python
    def _gather(self, providers, as_of):
        symbol = self._gather_symbol
        senate = self._fetch_senate_trades(symbol) or []
        house = self._fetch_house_trades(symbol) or []
        all_trades = senate + house
        exec_price_by_trade = {self._trade_key(t): self._get_price_at_date(t.get("symbol"),
                               self._exec_date(t)) for t in all_trades}
        traders = {t.get("representative") or t.get("senator") for t in all_trades}
        trader_history_by_name = {name: [h for h in (self._fetch_trader_history(name) or [])
                                  if self._disclosure_date(h) is None or self._disclosure_date(h) <= (as_of or datetime.now(timezone.utc))]
                                  for name in traders if name}
        current_price = providers.price_at_date(symbol, as_of)
        return {"all_trades": all_trades, "current_price": current_price,
                "exec_price_by_trade": exec_price_by_trade,
                "trader_history_by_name": trader_history_by_name, "symbol": symbol}
```

- [ ] **Step 4: FMPSenateTraderWeight `_process(bundle, settings, as_of)`** — `_filter_trades(now=as_of, using exec_price_by_trade)` + `_calculate_recommendation(using trader_history_by_name)`. Thread `now=as_of` into `_filter_trades` (live ref line 265) replacing its internal `datetime.now`, and have `_filter_trades`/`_calculate_recommendation` read prices/history from the pre-resolved maps instead of calling `_get_price_at_date`/`_fetch_trader_history` directly. Return `Recommendation`.

> Re-plan checkpoint: `_trade_key`/`_exec_date`/`_disclosure_date` are helper names this plan introduces for the maps — confirm the actual field names FMP uses (`transactionDate`, `disclosureDate`/`dateRecieved`, `representative`/`senator`/`office`) in the live `FMPSenateTraderWeight.py` and implement the key/date extractors to match. The two-stage refactor is the riskiest part of Phase 1 (fetches are interleaved in the decision functions); do it incrementally and lean on the golden test.

- [ ] **Step 5: Rewrite both live `run_analysis` as orchestrators** (resolve settings incl. `target`/age thresholds, set `self._gather_symbol`, `_gather(None)`, `_process(None)`, persist; keep the `market_analysis.subtype` read for Copy's ENTER_MARKET vs OPEN_POSITIONS branch — live ref lines 748-860 — by passing `subtype` into `_process` via `settings["_subtype"]` or a `_gather_subtype` attr).

- [ ] **Step 6: Test** `test_senate_gather_process.py`: fake congress provider with dated senate/house trades straddling `as_of`; assert (a) trades with `disclosureDate > as_of` are dropped, (b) `exec_price_by_trade`/`trader_history_by_name` are populated in the bundle (proving the pre-resolve), (c) Copy honors both subtypes, (d) Weight's confidence uses pre-resolved history. Assert `as_of=None` path produces the same recs as a direct live-style call.

- [ ] **Step 7: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_senate_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FMPSenateTraderCopy/Weight into _gather(two-stage)/_process"
```
Expected: PASS.

---

## Task 8: Refactor FinnHubRating

**Files:**
- `BA2TradeExperts/ba2_experts/FinnHubRating.py`
- Test: `BA2TradeExperts/tests/test_finnhub_rating_gather_process.py`

`consensus_from_counts` is already pure (live ref `FinnHubRating.py:23`). The one lookahead fix here is **period selection**: `_calculate_recommendation` uses `trends_data[0]` (line 210) — must instead pick the most-recent period with `period_date <= as_of`.

- [ ] **Step 1: `_gather`** — bundle `{trends_data: list, current_price}`. Fetch via `_fetch_recommendation_trends` (line 130) through the providers bundle (Finnhub is a separate API — resolve via `providers` with a `"rating"`/`"finnhub"` category or keep the existing direct Finnhub client but route the price via `providers.price_at_date`). Store the **full** `trends_data` list (not just `[0]`) so `_process` can select by date.

- [ ] **Step 2: `_process(bundle, settings, as_of)`** — select the most-recent period:

```python
    def _process(self, data_bundle, settings, as_of=None):
        from datetime import datetime, timezone
        from ba2_common.core.provider_utils import parse_provider_date
        now = as_of or datetime.now(timezone.utc)
        trends = data_bundle["trends_data"] or []
        # NOT trends[0]: pick the latest period whose period date <= as_of (no-lookahead)
        eligible = [t for t in trends if (pd_ := parse_provider_date(t.get("period"))) and pd_ <= now]
        latest = max(eligible, key=lambda t: parse_provider_date(t.get("period"))) if eligible else None
        if latest is None:
            return Recommendation(OrderRecommendation.HOLD, 10.0, data_bundle["current_price"],
                                  "No recommendation-trend period on/before as_of", 0.0,
                                  skip=False)
        counts = {k: latest.get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        result = consensus_from_counts(counts, settings.get("thresholds"))
        ...  # map result -> signal/confidence/details exactly as live _calculate_recommendation
```

> Detail-string + thresholds parity: pass FinnHub thresholds via `settings` (SHARED CONTRACT — `_process` never reads `self` for config). Keep the details string byte-identical to live ref (the block around line 214+).

- [ ] **Step 3: Orchestrator `run_analysis`** (mirror Task 5 Step 3; thresholds + any rating settings into `_resolve_settings`).

- [ ] **Step 4: Test** — fake `trends_data` with periods `["2026-05-01","2026-06-01","2026-07-01"]`; assert `_process` with `as_of=2026-06-15` picks the `2026-06-01` period (NOT `[0]` if `[0]` is the future `2026-07-01`); assert byte-equal details vs a live-style direct call with `as_of=None`.

- [ ] **Step 5: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_finnhub_rating_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FinnHubRating into _gather/_process (as_of period selection, not trends[0])"
```
Expected: PASS.

---

## Task 9: Refactor FactorRanker

**Files:**
- `BA2TradeExperts/ba2_experts/FactorRanker/{__init__.py,data.py}`
- Test: `BA2TradeExperts/tests/test_factor_ranker_gather_process.py`

FactorRanker's factor data layer is already mostly `as_of`-aware (`fetch_value_inputs`/`fetch_quality_inputs`/`fetch_pead_inputs` take `as_of`, live ref `data.py:178/228/275`). Two gaps: (1) `fetch_close_prices` (line 163) has no `end_date`/`as_of`; (2) `_compute_factor` (`__init__.py:263`) doesn't thread `as_of`. FactorRanker has **no `ExpertRecommendation` seam** — it executes via `FactorPortfolioManager.rebalance` (open-question 1).

- [ ] **Step 1: Add `as_of` to `fetch_close_prices`**

```python
def fetch_close_prices(symbols, lookback_days: int = 400, as_of=None) -> Dict[str, pd.Series]:
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    from datetime import datetime, timezone
    provider = FMPOHLCVProvider()
    end = as_of or datetime.now(timezone.utc)
    out = {}
    for sym in symbols:
        try:
            df = provider.get_ohlcv_data(sym, end_date=end, lookback_days=lookback_days, interval="1d")
            if df is not None and not df.empty and "Close" in df:
                out[sym] = df["Close"].reset_index(drop=True)
        except Exception as e:
            logger.warning(f"FactorRanker: price fetch failed for {sym}: {e}")
    return out
```

- [ ] **Step 2: `_gather`** — bundle `{universe, factors, holdings, prices, current_price_map}`. Resolve the universe (Phase-1 scope: `static`/`enabled_instruments` only; the `screener` source is Phase 3), then loop `_FACTOR_PIPELINE` fetchers passing `as_of` (momentum→`fetch_close_prices(..., as_of=as_of)`; value/quality/pead already take `as_of`). Read `holdings` via `FactorPortfolioManager.get_holdings()` (live concern — resolved in `_gather`, in backtest comes from the account). `_compute_factor` (line 263) must accept + thread `as_of` to its fetcher.

```python
    def _gather(self, providers, as_of):
        universe = self._resolve_universe()       # static only in Phase 1
        weights = self._gather_weights            # resolved by caller from settings
        factors = {}
        for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
            if float(weights.get(name, 0.0)) == 0.0:
                continue
            factors[name] = self._compute_factor(name, fetch_name, calc, universe, as_of=as_of)
        holdings = self._gather_holdings()        # pm.get_holdings()[0] live; account in backtest
        prices = data.fetch_close_prices(universe, as_of=as_of)
        return {"universe": universe, "factors": factors, "holdings": holdings,
                "prices": prices, "current_price": None}   # FactorRanker is basket-level
```

- [ ] **Step 3: `_process(bundle, settings, as_of)`** — pure: `composite_score(factors, weights, winsorize_pct)` + `rank_symbols` + `long_only_top_n` → target weights. Return a `Recommendation` whose `raw_outputs` carries the **target weights dict** + the ranked book (SHARED CONTRACT: FactorRanker hands target weights directly to the engine, no ExpertRecommendation seam). Use a sentinel signal (`OrderRecommendation.OVERWEIGHT`) and `skip=True, skip_reason="empty universe"` when the universe/factors are empty (preserves the live `_mark_skipped` outcomes at `__init__.py:148/162`).

```python
    def _process(self, data_bundle, settings, as_of=None):
        if not data_bundle["universe"]:
            return Recommendation(OrderRecommendation.HOLD, 0.0, 0.0,
                                  "No candidate instruments configured", skip=True,
                                  skip_reason="empty universe")
        if not data_bundle["factors"]:
            return Recommendation(OrderRecommendation.HOLD, 0.0, 0.0,
                                  "No factors enabled", skip=True, skip_reason="no factors")
        weights = settings["_factor_weights"]
        comp = composite_score(data_bundle["factors"], weights, float(settings["winsorize_pct"] or 0.0))
        ranked = rank_symbols(comp)
        targets = long_only_top_n(ranked, comp, top_n=int(settings["top_n"]),
                                  weighting=settings["weighting"],
                                  max_weight_per_name=float(settings["max_weight_per_name"]),
                                  gross_exposure=float(settings["gross_exposure"]))
        book = self._build_book(ranked, comp, data_bundle["factors"], targets, weights,
                                float(settings["winsorize_pct"] or 0.0), set(data_bundle["holdings"]))
        return Recommendation(OrderRecommendation.OVERWEIGHT, 0.0, 0.0,
                              f"Ranked {len(ranked)} names, holding {len(targets)}",
                              raw_outputs={"targets": targets, "book": book,
                                           "name": "Ranked book", "type": "factor_ranking"})
```

- [ ] **Step 4: Orchestrator `run_analysis`** — resolve settings (weights/winsorize/top_n/weighting/max_weight_per_name/gross_exposure) into a dict (set `self._gather_weights`/`self._gather_holdings`), `_gather(None)`, `_process(None)`; if `rec.skip` → `_mark_skipped` (preserve live behaviour, `MarketAnalysisStatus.SKIPPED`); else `FactorPortfolioManager(self.id).rebalance(rec.raw_outputs["targets"])` + write `market_analysis.state["factor_ranker"] = rec.raw_outputs["book"]` + `_write_output`. **The rebalance is live-only**; in backtest (Phase 4) `analyze_as_of` returns the targets and the engine routes them to `submit_order`.

> Re-plan checkpoint (open-question 1): confirm with Phase 4 that the engine accepts `rec.raw_outputs["targets"]` directly (weight-based) and whether the classic RM/position_sizing applies to weight targets or FactorRanker bypasses the shared enter/exit ruleset in backtest. Phase 1 only needs `_process` to PRODUCE the targets purely; the routing decision is Phase 4's. Document the chosen contract in the commit.

- [ ] **Step 5: Test** `test_factor_ranker_gather_process.py`: fake `data.fetch_*` returning small fixtures for a 4-symbol universe; assert `_process` returns target weights summing to ~`gross_exposure`, `top_n` honored, ranking stable; assert empty universe → `skip=True`; assert `fetch_close_prices` receives `end_date==as_of` (spy). Reuse FactorRanker factor-math fixtures from Phase-0 tests for value parity.

- [ ] **Step 6: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_factor_ranker_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FactorRanker into _gather(as_of)/_process(target weights) + fetch_close_prices end_date"
```
Expected: PASS.

---

## Task 10: Refactor FMPRating (LAST — consensus lookahead documented)

**Files:**
- `BA2TradeExperts/ba2_experts/FMPRating.py`
- Test: `BA2TradeExperts/tests/test_fmp_rating_gather_process.py`

FMPRating is last because the two consensus endpoints have no per-row date (Decision 4 + feasibility doc). Phase 1 splits it faithfully; `as_of` reconstruction is deferred.

- [ ] **Step 1: `_gather`** — bundle `{consensus_data, upgrade_data, current_price}`. Fetch via `_fetch_price_target_consensus` (live ref `FMPRating.py:78`) + `_fetch_upgrade_downgrade` (line 122) through the providers bundle; price via `providers.price_at_date`. Add the `# AS_OF LIMITATION` docstring: these endpoints return the latest snapshot regardless of `as_of` (documented backtest lookahead bias).

- [ ] **Step 2: `_process(bundle, settings, as_of)`** — SKIP is first-class here:

```python
    def _process(self, data_bundle, settings, as_of=None):
        consensus = data_bundle["consensus_data"]
        if not consensus:
            return Recommendation(OrderRecommendation.HOLD, 0.0, data_bundle["current_price"],
                                  "No analyst coverage", skip=True, skip_reason="no consensus data")
        analyst_count = self._count_analysts(consensus)   # existing helper
        if analyst_count < int(settings["min_analysts"]):
            return Recommendation(OrderRecommendation.HOLD, 0.0, data_bundle["current_price"],
                                  f"Insufficient analysts ({analyst_count} < {settings['min_analysts']})",
                                  skip=True, skip_reason="insufficient analysts")
        rec = self._calculate_recommendation(
            consensus, data_bundle["upgrade_data"], data_bundle["current_price"],
            float(settings["profit_ratio"]), int(settings["min_analysts"]),
            settings["target_price_type"])     # target_price_type pushed in via settings
        return Recommendation(rec["signal"], round(rec["confidence"], 1),
                              data_bundle["current_price"], rec["details"],
                              rec["expected_profit_percent"],
                              raw_outputs={"name": "Analyst Rating Analysis",
                                           "type": "analyst_rating_analysis", "text": rec["details"]})
```

> `_calculate_recommendation` (live ref line 153) currently reads `self.settings`/`target_price_type` internally — refactor it to take `target_price_type` as a parameter (SHARED CONTRACT: `FMPRating.target_price_type` passed via settings, `_process` never touches `self` for config). Keep its math + details string byte-identical.

- [ ] **Step 3: Orchestrator `run_analysis`** — resolve settings incl. `target_price_type`/`min_analysts`/`profit_ratio`; `_gather(None)`; `_process(None)`; **honor `rec.skip`**: set `market_analysis.status = MarketAnalysisStatus.SKIPPED` + state + `update_instance`, return (preserve the live FMPRating no-coverage/insufficient-analysts skip outcomes); else persist `ExpertRecommendation`+`AnalysisOutput` as usual.

- [ ] **Step 4: Test** `test_fmp_rating_gather_process.py`: fake consensus with N analysts → BUY/HOLD per math; fake `None` consensus → `skip=True, skip_reason="no consensus data"`; fake below-`min_analysts` → `skip=True, skip_reason="insufficient analysts"`; assert `target_price_type` flows from settings (spy on `_calculate_recommendation`). Add a test asserting the `# AS_OF LIMITATION` docstring exists (regression guard so the caveat isn't silently dropped).

- [ ] **Step 5: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeExperts/tests/test_fmp_rating_gather_process.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "feat(experts): split FMPRating into _gather/_process (SKIP first-class; consensus lookahead documented)"
```
Expected: PASS.

---

## Task 11: Provider as_of regression gate (byte-equality + cache-hit + no-lookahead)

**Files:**
- `BA2TradeProviders/tests/test_provider_asof.py` (extend), `BA2TradeProviders/tests/test_cache_hits.py`
- `BA2TradeProviders/test_files/probe_asof_byte_equality.py` (network probe, NOT in pytest suite)

- [ ] **Step 1: Cache-hit-count assertion** — `test_cache_hits.py`: prime the cache via one `read_event_rows` miss (fetch path), then assert a second identical read is a `STATS.hits` hit and issues **zero** new fetches. Also assert a 50-symbol × 500-bar simulated slice loop issues ~50 fetches not 25 000 (the design's caching claim) using a fetch-counting fake.

```python
from ba2_providers.cache import native_cache as nc

def test_second_read_is_cache_hit():
    nc.reset_stats()
    # ... upsert rows, then two reads; first miss/fetch, second hit
    # assert nc.STATS.fetches == 1 and nc.STATS.hits >= 1
```

- [ ] **Step 2: No-lookahead determinism** — assert a fixed `(symbol, as_of)` read returns the SAME rows across repeated calls and that ALL returned rows have `effective_date <= as_of` (extend `test_provider_asof.py`).

- [ ] **Step 3: Byte-equality probe (live key, run once, snapshot)** — `test_files/probe_asof_byte_equality.py`: for a fixed `(symbol, as_of=None)`, call the pre-refactor live tree fetch (import from `BA2TradePlatform/ba2_trade_platform`) and the new `ba2_providers` `as_of=None` `get()`, assert the normalized dicts are equal. This is the authoritative "no live behaviour change" proof. Document the snapshot in the commit; keep it out of CI (network + cross-tree import).

> Re-plan checkpoint: importing the live tree alongside the package in one process may collide on module names. If so, run the two fetches in separate subprocesses and compare their JSON output files. The live tree stays READ-ONLY.

- [ ] **Step 4: Run + commit**

```bash
/tmp/v_p1/bin/python -m pytest BA2TradeProviders/tests/test_cache_hits.py BA2TradeProviders/tests/test_provider_asof.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -am "test(providers): as_of byte-equality + cache-hit + no-lookahead regression gate"
```
Expected: PASS.

---

## Task 12: THE GOLDEN TEST — live decision == analyze_as_of(now), all experts

**Files:**
- `BA2TradeExperts/tests/test_golden_live_vs_asof.py`
- Test harness fixtures: `BA2TradeExperts/tests/golden_fixtures.py`

This is the Phase-1 acceptance gate (SHARED CONTRACT `golden_test`). For every backtestable expert, `_process(_gather(live, None), settings)` must equal `analyze_as_of(now, context)` on `(signal, confidence, expected_profit_percent, details, skip, skip_reason)`, with `current_price` pinned identically in both paths.

- [ ] **Step 1: Build the fixture providers** (`golden_fixtures.py`) — fixed, deterministic fakes for each provider category (earnings row, insider txns, senate/house trades, finnhub trends, factor inputs, rating consensus) plus a `FakeOHLCV` returning a constant close so `current_price` is pinned. Provide a `make_get_provider(fixture)` returning the `(category, name, **kw) -> provider` callable, and `inject_resolver(get_provider)` that calls `TradeConditions.set_provider_resolver(get_provider)` (so `_live_providers()` resolves to the fixtures).

- [ ] **Step 2: Parametrized golden test**

```python
import pytest
from datetime import datetime, timezone
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import AnalysisUseCase  # if subtype needed
from ba2_experts.tests.golden_fixtures import make_get_provider, FIXTURES

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

CASES = [
    ("FMPEarningsDrift", {}),
    ("FMPInsiderClusterBuy", {}),
    ("FinnHubRating", {}),
    ("FMPSenateTraderCopy", {"subtype": AnalysisUseCase.ENTER_MARKET}),
    ("FMPSenateTraderCopy", {"subtype": AnalysisUseCase.OPEN_POSITIONS}),
    ("FMPSenateTraderWeight", {}),
    ("FactorRanker", {}),
    ("FMPRating", {}),   # as_of=None only (consensus lookahead documented)
]

@pytest.mark.parametrize("expert_name,opts", CASES)
def test_live_equals_analyze_as_of_now(expert_name, opts, golden_expert_factory):
    expert, settings = golden_expert_factory(expert_name)   # constructs + sets _gather_* attrs
    get_provider = make_get_provider(FIXTURES[expert_name])

    # Live path: _gather(live, None) + _process
    bundle_live = expert._gather(LiveProviderBundle(get_provider), as_of=None)
    rec_live = expert._process(bundle_live, settings, as_of=None)

    # Backtest path: analyze_as_of(now)
    ctx = BacktestContext(providers=LiveProviderBundle(get_provider), settings=settings,
                          as_of=NOW, subtype=opts.get("subtype"))
    rec_asof = expert.analyze_as_of(NOW, ctx)

    # Pin current_price identically so price-source diff can't mask logic drift
    if isinstance(rec_live, list):
        assert len(rec_live) == len(rec_asof)
        for a, b in zip(rec_live, rec_asof):
            b.current_price = a.current_price
            assert a.almost_equals(b), f"{expert_name} drift: {a} != {b}"
    else:
        rec_asof.current_price = rec_live.current_price
        assert rec_live.almost_equals(rec_asof), f"{expert_name} drift"
```

(The `golden_expert_factory` conftest fixture constructs each expert against the temp DB, resolves its `_SETTING_KEYS` to defaults, and sets the gather-time attrs the multi-stage experts need.)

> Note: for `as_of=NOW` vs `as_of=None`, the fixtures are time-invariant (a fixed earnings row, fixed trends) so the only difference exercised is the `as_of` plumbing, exactly what the gate must prove. The FMPRating case asserts equality at `as_of=None==NOW` with the documented caveat that `as_of=<past>` is NOT covered (consensus snapshot).

- [ ] **Step 3: Run the full Phase-1 suite across all three packages**

```bash
cd /Users/bmigette/Documents/dev/BA2
/tmp/v_p1/bin/python -m pytest BA2TradeCommon/tests BA2TradeProviders/tests BA2TradeExperts/tests -v
```
Expected: ALL PASS — the golden test green for all 8 cases is the Phase-1 GATE.

- [ ] **Step 4: import-linter still green (no new layering edges)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeCommon && /tmp/v_p1/bin/lint-imports
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders && /tmp/v_p1/bin/lint-imports
cd /Users/bmigette/Documents/dev/BA2/BA2TradeExperts && /tmp/v_p1/bin/lint-imports
```
Expected: `Contracts: 1 kept, 0 broken.` in each. (Critical: `ba2_common` still must NOT import `ba2_providers` — the `_live_providers` helper routes through the injected `TradeConditions` resolver, not a direct import.)

- [ ] **Step 5: Verify BA2TradePlatform untouched**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform status --short
```
Expected: only the Phase-1 plan doc; **no** changes under `ba2_trade_platform/` (Phase 1 edits packages only).

- [ ] **Step 6: Commit + (after approval) push the three `phase1-asof` branches**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -am "test(experts): golden test — live decision == analyze_as_of(now) for all experts (PHASE-1 GATE)"
for r in BA2TradeCommon BA2TradeProviders BA2TradeExperts; do
  git -C /Users/bmigette/Documents/dev/BA2/$r push -u origin phase1-asof   # only after approval
done
```

---

## Self-Review

**Spec coverage (design §2/§3/§6 Phase 1 + SHARED CONTRACTS):**
- "Add the contract (live = `as_of=None`, unchanged)" → Task 4 uniform `get()` alias + Decision 6 (as_of-gated bug fixes) + Task 11 byte-equality gate. ✓
- "native cache (effective vs value date; parquet/SQLite; reuse NewsCache pattern)" → Task 3 `ProviderCache` model + `native_cache.py` (parquet + SQLite event store). ✓
- "Fix the two lookahead bugs (statements fiscalDateEnding→fillingDate/acceptedDate; insider transactionDate→filingDate)" → Task 4 Steps 1-2 + `provider_utils` helpers (Task 3 Step 2). ✓
- "Refactor experts to split `_gather`/`_process`" + "`BacktestInterface.analyze_as_of`" → Tasks 2 (seam) + 5-10 (all 7/8 experts, clean ones first, FMPRating last per the design + feasibility doc ordering). ✓
- "current_price as an as_of close, settings as plain dict, pre-resolve hard cases (Senate Weight exec_price/trader_history; FactorRanker as_of through _compute_factor + fetch_close_prices end_date)" → Decision 1 + Tasks 7 Step 3-4 + Task 9 Steps 1-2. ✓
- "Keep all persistence in run_analysis only" → Tasks 5-10 orchestrator steps; `_process` returns a value object, never touches DB. ✓
- **GOLDEN TEST gate** (live==as_of(now) per expert + cache-hit + no-lookahead) → Task 12 + Task 11. ✓
- Locked decisions: classic RM only (no RM code touched in Phase 1); equities/options-ready (no account code touched); extract-by-copy (packages edited, live tree read-only — Task 12 Step 5 verifies); ba2_common seams consumed not redefined (Task 5 Step 4 routes via `TradeConditions._get_provider`). ✓

**Placeholder scan:** every code step carries real code grounded in live refs (file:line) — `evaluate_earnings_drift`/`detect_insider_cluster`/`consensus_from_counts` kept verbatim; `ProviderCache` mirrors `NewsCache`; `_gather`/`_process`/`run_analysis` bodies are full. No "TBD"/"add logic here". The `> Re-plan checkpoint:` notes are deliberate execution-time guards (real signatures in 90KB+ files, FMP field-name casing, Phase-0 module paths, Phase-4 contracts) — they describe exactly what to confirm, not fabricated detail.

**Type/name consistency:** `Recommendation` fields (signal/confidence/current_price/details/expected_profit_percent/raw_outputs/skip/skip_reason) consistent across Tasks 1,5-10,12 and match SHARED CONTRACT `recommendation_object`. `_gather(providers, as_of)`/`_process(bundle, settings, as_of)`/`analyze_as_of(as_of, context)` signatures match SHARED CONTRACT `gather_signature`/`process_signature`/`analyze_as_of_signature`. `ProviderBundle` accessors (ohlcv/fundamentals_details/insider/news/indicators/congress/price_at_date) consistent Task 1↔5-10. `effective_date`/`value_date` pair + `insider_effective_date`/`statement_effective_date` consistent Task 3↔4. Cache `STATS` consistent Task 3↔11.

**Known reconciliation points (verify against source during execution, do not assume):** Phase-0 actual module paths for experts/providers; `ba2_providers.get_provider` registry category keys (Task 1 checkpoint); `TradeConditions._get_provider` signature (Task 5 Step 4); FMP field casing `fillingDate` (statements) vs `filingDate` (insider) and presence (Task 3/4 probes); the shared statement-filter helper name in `FMPCompanyDetailsProvider` (Task 4 Step 2); Senate trade key/date field names `disclosureDate`/`representative`/`senator` (Task 7 checkpoint); whether multi-symbol experts return `Recommendation` vs `List[Recommendation]` (Task 7 Step 2 decision); FactorRanker engine target-weight contract (Task 9 checkpoint, open-question 1); FMPRating `_count_analysts`/`_calculate_recommendation` helper names (Task 10).

**Open questions surfaced to the user** (carried from SHARED CONTRACTS, resolved provisionally by Decisions 1-7; confirm at approval): current_price source (Decision 1: OHLCV as_of close); FMPRating consensus lookahead handling (Decision 4: in-scope, as_of=None tested, reconstruction deferred); multi-symbol `_process` return shape (Task 7); FactorRanker no-ExpertRecommendation-seam engine contract (Task 9, open-question 1); insider filingDate fallback + statement effective_date anchor (Decision 7).

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase1-provider-asof-plan.md`. Prerequisite: Phase 0 must be landed (the three packages install/import cleanly). Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks; Tasks 5-10 (per-expert splits) are independent and can be parallelized after Tasks 1-4 land (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`).
2. **Inline Execution** — execute Tasks 0→12 in order with checkpoints; Task 12 (golden test) is the GATE — do not declare Phase 1 done until all 8 golden cases are green AND `lint-imports` is clean AND `BA2TradePlatform` is byte-unchanged (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

All work runs on branches `phase1-asof` across the three package repos; `BA2TradePlatform/ba2_trade_platform/` stays read-only (its migration onto these refactored experts/providers is Phase 6).
