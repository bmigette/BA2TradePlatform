# Backtest Platform — Phase 2 (Daily Engine + `BacktestAccount`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is **Phase 2** of the expert-backtest program (design §6 "Phase 2 — Engine + `BacktestAccount(AccountInterface)`"), consuming Phase 0's extracted packages and Phase 1's `_gather`/`_process`/`analyze_as_of` seam.

**Goal:** In **BA2TestPlatform** (the backtest host), build (a) a `BacktestAccount(AccountInterface)` — a simulated broker that implements the full live `AccountInterface` surface (next-bar fills + fees/slippage, multi-symbol cash/positions/equity ledger, per-bar TP/SL/stop evaluation) against a **separate backtest DB** — and (b) a **custom daily multi-asset engine** that drives the **real ba2trade decision path** (universe → `expert.analyze_as_of(as_of, context)` → enter/exit `TradeConditions`/ruleset → classic `TradeRiskManagement` → `position_sizing` → `account.submit_order()` → fill → record). Wire the `ba2_common` seams in this host (`set_instance_resolver`, `set_llm_service`, `TradeConditions.set_provider_resolver`, `configure_db`, ATR provider injection). Run the **first daily backtests on `FMPEarningsDrift` + `FMPInsiderClusterBuy`** (the two "clean" experts from the FMP feasibility survey).

**Architecture:** The simulator is a **custom daily portfolio simulator, NOT a third-party engine** (NautilusTrader/vectorbt/backtrader all invert control and would force rewriting the experts — design §5). The whole point is to reuse the live decision/sizing/order code unchanged. `BacktestAccount` inherits **all** of `AccountInterface`'s concrete orchestration (`submit_order`, `refresh_transactions`, `close_transaction*`, the `_validate_*`/transaction helpers, wash-trade locks, the price cache) and only implements the **19 equity abstracts** (12 from `ReadOnlyAccountInterface` + 6 from `AccountInterface` + `get_settings_definitions`). We do **NOT** inherit `OptionsAccountInterface` in v1 (equities-first; `BacktestOptionsAccount(BacktestAccount, OptionsAccountInterface)` is deferred). We **reuse** BA2TestPlatform's `Backtest` SQLAlchemy model + metrics conversion + `TaskQueueService` job queue + `strategy_executor` condition-eval helpers + `Backtesting.tsx` UI. The existing `backtesting.py`/`MLStrategy` single-asset engine is **retained UNCHANGED** as the ML expert's engine (two engines by expert type, one results model, one UI).

**Tech Stack:** Python ≥3.11; the three Phase-0 packages (`ba2trade-common`, `ba2trade-providers`, `ba2trade-experts`) installed editable into BA2TestPlatform's `backend/venv`; BA2TestPlatform's existing FastAPI + SQLAlchemy declarative `Base` + `TaskQueueService` (DB-backed daemon-thread queue, NOT Celery) + React/recharts frontend; pytest.

---

## Source of truth & repo locations

- **Backtest host (where ALL new code lands):** `BA2TestPlatform/backend/` (FastAPI app under `backend/app/`). Python env: `BA2TestPlatform/backend/venv/bin/python` (per `backend/CLAUDE.md` — never system python).
- **Contract source (read-only this phase):** `BA2TradePlatform/ba2_trade_platform/core/interfaces/{ReadOnlyAccountInterface,AccountInterface,OptionsAccountInterface}.py`, `core/models.py`, `core/types.py`, `core/TradeManager.py`, `core/TradeRiskManagement.py`, `core/position_sizing.py`. These define the `AccountInterface` surface `BacktestAccount` must satisfy and the live order/transaction lifecycle it reuses.
- **Phase-0 packages:** `BA2TradeCommon/ba2_common`, `BA2TradeProviders/ba2_providers`, `BA2TradeExperts/ba2_experts` (siblings under `…/dev/BA2/`). `BacktestAccount` subclasses `ba2_common.core.interfaces.AccountInterface`; experts come from `ba2_experts`.
- **Reuse targets in BA2TestPlatform:** `backend/app/models/backtest.py` (`Backtest` table), `backend/app/services/task_queue.py` (`TaskQueueService.queue_task`/`register_handler`/`update_progress`/`is_task_paused`), `backend/app/services/strategy_executor.py` (`evaluate_condition_tree`/`evaluate_condition`/`evaluate_comparison`/`ConfirmationTracker`/`build_context`/`StrategyExecutor`), `backend/app/services/backtest_handler.py` (`_convert_bt_results`/`_safe_float`/`_safe_duration_days`/`_empty_results` metric shapes), `backend/app/main.py` (handler registration block @246-257; router includes @330-345), `backend/app/api/backtests.py`, `frontend/src/pages/Backtesting.tsx` (camelCase `results.equityCurve`/`drawdownCurve`/`trades` contract @126-127, metric cards `totalReturn`/`sharpeRatio`/`profitFactor` @149-153).
- This plan is derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§2, §5, §6 Phase 2), `docs/FMP_BACKTEST_FEASIBILITY.md`, the SHARED CONTRACTS (`backtest_account`, `engine_loop`, `golden_test`), and a file-by-file recon of both trees.

> **Re-plan checkpoint (Phase-1 dependency):** This phase consumes Phase 1's outputs that did not exist at authoring time: `BacktestInterface.analyze_as_of(as_of, context)` on each expert, the `Recommendation` value object, the `BacktestContext` shape (must carry `providers`, `settings`, `account`, `as_of`, `subtype`), and `_gather`/`_process`. **Before Task 4, confirm:** (1) the import path + fields of `Recommendation` (contract: `signal: SignalType, confidence, expected_profit_percent, current_price, details, raw_outputs, skip, skip_reason`); (2) the exact `BacktestContext` constructor/attributes Phase 1 shipped; (3) that `FMPEarningsDrift`/`FMPInsiderClusterBuy` expose `analyze_as_of`. If Phase 1 named these differently, adopt Phase-1's names verbatim — do NOT invent.

## Decisions taken (confirm before execution)

These resolve forks the recon + open-questions surfaced. Override any at approval time.

1. **`BacktestAccount(AccountInterface)` only — NOT `OptionsAccountInterface` (equities-first v1).** `supports_trading=True`, `supports_options` unset/False. Options (`BacktestOptionsAccount(BacktestAccount, OptionsAccountInterface)`, +6 options abstracts) is a later phase gated on historical options data (FMP lacks cheap chains/greeks — design §5). *(SHARED CONTRACTS `backtest_account.class`.)*
2. **Separate backtest DB via `ba2_common.core.db.configure_db`.** The inherited `AccountInterface`/`refresh_transactions`/`submit_order` logic reads/writes `TradingOrder`/`Transaction`/`AccountDefinition` rows; the backtest runs against a throwaway sqlite file so it never touches the live DB. **Per-run fresh DB** (one sqlite file per backtest run id), schema = `SQLModel.metadata.create_all` from `ba2_common.core.models` (identical to live so inherited DB logic works unchanged). *(Open-question "Separate backtest DB" resolved: identical schema, per-run fresh.)*
3. **Reuse BA2TestPlatform's `Backtest` SQLAlchemy table AS-IS** (`backend/app/models/backtest.py`, plain declarative `Base`, NOT SQLModel). It already carries the config fields, status, JSON result blobs (`results`/`trades`/`equity_curve`/`drawdown_curve`), and every metric column the daily engine needs. For multi-asset we add a `universe`/aggregate via the JSON blobs (no schema change v1). `model_id` is `nullable=False` in the live model — **Decision 3a:** for expert (non-ML) runs, point `model_id` at a sentinel/expert-run row OR make `model_id` nullable via a one-line migration; this plan uses a **nullable `model_id` migration** (Task 7, `db_migrate`) so expert runs need no fake model. *(Open-question "Backtest model SQLAlchemy vs SQLModel" resolved: keep the BA2TestPlatform SQLAlchemy table; the backtest *trading* DB is the separate SQLModel `configure_db` one — two distinct DBs, see Decision 2.)*
4. **One `daily_backtest` job-queue handler**, registered alongside the existing `dataset_regeneration`/`training_job`/`backtest` handlers in `main.py`. `TaskQueueService` is `max_workers=1` (sequential) — daily runs serialize behind training; we accept that for v1 (a priority lane is noted as future work). *(SHARED CONTRACTS `engine_loop.reused_ba2testplatform.job_queue`.)*
5. **`current_price` = OHLCV close-at-`as_of`.** Standardize ALL experts + the engine on the as-of bar **close** as the single price source (the `Recommendation.current_price` Phase 1 already threads). Fills use the **next bar** (open ± slippage) per the fill model; the *decision* price is the as-of close. *(Open-question "current_price source" resolved: close, one source.)*
6. **Universe v1 = static `enabled_instruments` list** passed in the run payload. Phase-3 historical-screener reconstruction is NOT a dependency of this phase (FMPEarningsDrift/InsiderClusterBuy run on an explicit symbol list). The engine's universe-resolution hook is built now but reads the static list; Phase 3 swaps the hook body. *(design §6: Phase 3 enables FactorRanker/screener; Phase 2 ships clean experts on a static universe.)*
7. **Diversification RM (`per_instrument_cap_pct`, `max_concurrent_positions`) IS in scope for the new multi-asset engine** (it is multi-symbol, unlike the legacy single-asset `MLStrategy`). The classic-RM params are *enforced* by the engine + `position_sizing`; their *optimization* (joint GA) is Phase 5. *(Open-question "multi-asset diversification RM" resolved: enforce in the new engine now.)*
8. **`get_orders`/`refresh_orders`/`modify_order` keep AlpacaAccount's widened signatures** (`get_orders(status=..., fetch_all=...)`, `refresh_orders(heuristic_mapping=..., fetch_all=...)`, `modify_order(order_id, trading_order=None)`) so the inherited callers and any shared call sites work unchanged. *(SHARED CONTRACTS `backtest_account.must_implement_abstracts`.)*

## The reuse map (what we inherit vs implement)

`BacktestAccount` **inherits unchanged** from `AccountInterface`/`ReadOnlyAccountInterface` (the biggest free win — do NOT override):
- `submit_order` (validation/persistence/auto-transaction/wash-trade lock/TP-SL fan-out/qty recalc) — `AccountInterface.py:60`.
- `refresh_transactions` (broker-agnostic `WAITING→OPENED→CLOSED`, never-opened cleanup, OCO-close detection, open/close price derivation) — `ReadOnlyAccountInterface.py:411`.
- `close_transaction`/`close_transaction_async`/`submit_close_order_for_transaction` — `AccountInterface.py:1061,1111,1201`.
- All `_validate_*`/`_handle_transaction_requirements`/`_create_transaction_for_order`/`_recalculate_transaction_quantity`, wash-trade helpers, `get_instrument_current_price` (cached), `filter_supported_symbols`.

`BacktestAccount` **implements** the 19 equity abstracts (signatures pinned to the contract; see Task 2–3):

| # | Abstract | Source decl | Role |
|---|---|---|---|
| 1 | `get_settings_definitions()` | (static, like AlpacaAccount:132) | starting_cash, commission_per_trade, slippage_bps, fill_model |
| 2 | `get_balance()` | ROAI:71 | simulated cash ledger |
| 3 | `get_account_info()` | ROAI:81 | MUST expose `.equity` (read by `_validate_position_size_limits`) |
| 4 | `get_positions()` | ROAI:95 | ledger position objects |
| 5 | `get_orders(status=None, fetch_all=False)` | ROAI:110 | query `TradingOrder` by account_id from backtest DB |
| 6 | `get_order(order_id)` | ROAI:132 | lookup by broker_order_id/id |
| 7 | `symbols_exist(symbols)` | ROAI:152 | True iff dataset has price history |
| 8 | `_get_instrument_current_price_impl(syms, price_type)` | ROAI:200 | **the time machine** (as-of bar price) |
| 9 | `refresh_positions()` | ROAI:389 | no-op True (+ optional MTM recompute) |
| 10 | `refresh_orders(heuristic_mapping=False, fetch_all=True)` | ROAI:400 | **the fill engine** |
| 11 | `get_dividends(...)` | ROAI:726 | `[]` v1 |
| 12 | `get_filled_trades(...)` | ROAI:750 | from filled `TradingOrder` rows |
| 13 | `get_balance_history(...)` | ROAI:770 | **the equity curve** |
| 14 | `_submit_order_impl(order, tp, sl, is_closing)` | AccountInterface:30 | assign broker_order_id; stage fill |
| 15 | `cancel_order(order_id)` | AccountInterface:899 | CANCELED + release reservations |
| 16 | `modify_order(order_id, trading_order=None)` | AccountInterface:912 | in-place pre-fill edit |
| 17 | `adjust_tp(txn, price, source)` | AccountInterface:978 | TP leg (SELL_LIMIT long / BUY_LIMIT short) |
| 18 | `adjust_sl(txn, price, source)` | AccountInterface:983 | SL leg (SELL_STOP long / BUY_STOP short) |
| 19 | `adjust_tp_sl(txn, tp, sl, source)` | AccountInterface:988 | paired OCO (may call adjust_tp+adjust_sl) |

## The critical gotcha (must handle, or every test silently passes on stale prices)

`get_instrument_current_price` (inherited, `ReadOnlyAccountInterface.py:233`) caches via the **class-level** `_GLOBAL_PRICE_CACHE: Dict[int, Dict[str, ...]]` keyed `symbol:price_type` with `PRICE_CACHE_TIME` TTL using **real wall-clock** (`ROAI.py:46,274-368`). The virtual backtest clock moves faster than wall time ⇒ a price cached at virtual day N is still "fresh" at virtual day N+5, leaking lookahead/stale prices across simulated bars. **Mitigation (chosen):** `BacktestAccount` **overrides `get_instrument_current_price` to bypass caching entirely** (call `_get_instrument_current_price_impl` directly), AND the engine **busts** `BacktestAccount._GLOBAL_PRICE_CACHE.pop(self.id, None)` on every clock advance as a belt-and-braces guard. A regression test (Task 6) asserts the price changes across bars.

## Acceptance GATE for Phase 2

1. **`BacktestAccount` is concrete & complete:** `BacktestAccount(account_def_id)` instantiates with **no** `TypeError: Can't instantiate abstract class …` (all 19 abstracts implemented), against the separate backtest DB. Verified by `tests/test_backtest_account_contract.py`.
2. **Fill engine correctness:** unit tests prove MARKET→next-bar open±slippage, LIMIT fills only when bar crosses limit, STOP triggers at stop±slippage, TP/SL legs evaluate per bar, OCO cancels the sibling, cash/positions/equity ledger update with commission. (`tests/test_backtest_account_fills.py`.)
3. **End-to-end deterministic run:** a `daily_backtest` of a **clean expert** (`FMPEarningsDrift`, then `FMPInsiderClusterBuy`) over a fixed date range against a **fixed provider cache** produces a stored `Backtest` row with `status="completed"`, sane metrics (`total_return`/`sharpe_ratio`/`max_drawdown`/`win_rate`/`profit_factor` all non-NaN, finite), and non-empty `equity_curve`. (`tests/test_daily_engine_e2e.py`.)
4. **Reproducibility:** running the SAME backtest twice (same cache + same params + same seed) yields a **byte-identical** `equity_curve` and identical metrics. (`tests/test_daily_engine_reproducible.py`.) This is the design's "same cache + same params ⇒ identical result" for the engine layer.
5. **Phase-1 golden regression inside the loop:** for a fixed `as_of`, `expert.analyze_as_of(as_of, context)` called from inside the engine equals the Phase-1 golden `Recommendation` for that `as_of` (re-verify the BACKTEST CONTRACT — the engine did not perturb decision logic). (`tests/test_engine_golden_regression.py`.)
6. **Price-cache busting:** the time-machine returns a *different* price on consecutive virtual bars for the same symbol (no stale-cache leak). (Part of `tests/test_backtest_account_fills.py`.)
7. **BA2TradePlatform untouched:** `git -C BA2TradePlatform status` clean (this phase changes only BA2TestPlatform + the 3 packages already produced in Phase 0). The legacy `backtesting.py`/`MLStrategy` path and existing `handle_backtest` still pass their existing tests.

---

## Task 1: Wire the Phase-0 packages + seams into BA2TestPlatform; backtest-DB bootstrap

**Files (create/edit in `BA2TestPlatform/backend/`):**
- Edit `backend/requirements.txt` (or `pyproject`) — add the 3 editable packages.
- Create `backend/app/services/backtest/__init__.py` (new subpackage for all Phase-2 engine code).
- Create `backend/app/services/backtest/seam_wiring.py` — host-side wiring of the 4 `ba2_common`/`ba2_experts` seams.
- Create `backend/app/services/backtest/backtest_db.py` — per-run backtest-DB lifecycle via `configure_db`.
- Test: `backend/tests/backtest/test_seam_wiring.py`.

- [ ] **Step 1: Install the three packages editable into the backtest venv**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/pip install -e /Users/bmigette/Documents/dev/BA2/BA2TradeCommon
./venv/bin/pip install -e /Users/bmigette/Documents/dev/BA2/BA2TradeProviders
./venv/bin/pip install -e /Users/bmigette/Documents/dev/BA2/BA2TradeExperts
./venv/bin/python -c "import ba2_common, ba2_providers, ba2_experts; print('chain ok', ba2_common.__version__)"
```
Expected: prints `chain ok 0.1.0`. Add the same three `-e` lines to `backend/requirements.txt` (with absolute or `../../BA2Trade*` relative paths) so a fresh `pip install -r` reproduces it.

> **Re-plan checkpoint:** If Phase 0 has been *pushed* and BA2TestPlatform should consume the git versions instead of local editable clones, replace the `-e <path>` installs with `pip install "git+ssh://git@github.com/bmigette/BA2TradeCommon.git"` (chain order common→providers→experts). Confirm at execution which mode the user wants.

- [ ] **Step 2: Implement the host `InstanceResolver` for BA2TestPlatform**

`backend/app/services/backtest/seam_wiring.py`:

```python
"""Host-side wiring of the ba2_common/ba2_experts seams for the backtest engine.

Phase 0 defined the seams but left them unconfigured; the live BA2TradePlatform
wires them in Phase 6. BA2TestPlatform wires its OWN (backtest-flavoured) versions
here so the inherited AccountInterface/expert code can resolve instances, an LLM
service (unused by the clean experts), and providers, all against the backtest cache."""
from __future__ import annotations
from typing import Any, Dict

from ba2_common.core.instance_resolver import set_instance_resolver, InstanceResolver
from ba2_common.core.interfaces.LLMServiceInterface import (
    set_llm_service, LLMServiceInterface, LLMServiceNotConfigured,
)


class BacktestInstanceResolver:
    """Resolves expert/account ids to live instances for the backtest run.

    The backtest engine constructs the BacktestAccount + expert instances and
    registers them here so the inherited AccountInterface code (which calls
    get_instance_resolver().get_account_instance(...) etc.) finds them."""
    def __init__(self) -> None:
        self._accounts: Dict[int, Any] = {}
        self._experts: Dict[int, Any] = {}

    def register_account(self, account_id: int, instance: Any) -> None:
        self._accounts[account_id] = instance

    def register_expert(self, expert_id: int, instance: Any) -> None:
        self._experts[expert_id] = instance

    def get_account_instance(self, account_id: int) -> Any:
        return self._accounts[account_id]

    def get_expert_instance(self, expert_id: int) -> Any:
        return self._experts[expert_id]

    def get_account_instance_from_transaction(self, transaction: Any) -> Any:
        return self._accounts[transaction.account_id]


class _NoLLMService(LLMServiceInterface):
    """The clean experts (EarningsDrift/InsiderClusterBuy) never call an LLM.
    Configure a loud-failing service so any accidental LLM call is caught, not silent."""
    def create_llm(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured("Backtest engine does not provide an LLM service "
                                      "(clean experts must not call LLMs).")
    def do_llm_call_with_websearch(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured("Backtest engine does not provide an LLM service.")


_resolver: BacktestInstanceResolver | None = None


def get_backtest_resolver() -> BacktestInstanceResolver:
    if _resolver is None:
        raise RuntimeError("seam wiring not initialised; call wire_backtest_seams() first")
    return _resolver


def wire_backtest_seams() -> BacktestInstanceResolver:
    """Idempotent: install the resolver + LLM service + provider resolver once per process."""
    global _resolver
    if _resolver is None:
        _resolver = BacktestInstanceResolver()
        set_instance_resolver(_resolver)              # ba2_common seam
        set_llm_service(_NoLLMService())              # ba2_common seam
        _wire_provider_resolver()                     # TradeConditions seam (Step 3)
    return _resolver
```

> Confirm `set_instance_resolver`/`set_llm_service` import paths against the **actual** Phase-0 `ba2_common` (Phase 0 plan Task 4 placed them at `ba2_common.core.instance_resolver` and `ba2_common.core.interfaces.LLMServiceInterface`). The `InstanceResolver` Protocol method names (`get_account_instance`/`get_expert_instance`/`get_account_instance_from_transaction`) match Phase-0 Task 4.

- [ ] **Step 3: Wire the `TradeConditions` provider resolver + the `position_sizing` ATR injection**

Add to `seam_wiring.py`:

```python
def _wire_provider_resolver() -> None:
    """TradeConditions data access is injected (Phase 0 severed the ba2_common->ba2_providers
    edge). Route condition data fetches to the as_of providers pointing at the backtest cache."""
    from ba2_common.core import TradeConditions
    from ba2_providers import get_provider  # noqa: F401  (ba2_providers is allowed here; this is the host)

    def _resolver(category: str, name: str, **kw):
        # Phase 1/2 providers are as_of-aware; the engine sets the as_of clock on context.
        return get_provider(category, name, **kw)

    TradeConditions.set_provider_resolver(_resolver)


def make_indicator_provider():
    """Return the indicator provider injected into position_sizing.get_latest_atr.
    Phase 0 made get_latest_atr(symbol, indicator_provider, ...) take an injected provider
    so ba2_common never imports ba2_providers."""
    from ba2_providers import get_provider
    return get_provider("indicators", "pandas")   # confirm registry key name in ba2_providers
```

> **Re-plan checkpoint:** Phase 0 Task 6 defined `TradeConditions.set_provider_resolver(fn)` with signature `fn(category, name, **kw)` and `position_sizing.get_latest_atr(symbol, indicator_provider, ...)`. Confirm both exist with those exact names in the installed `ba2_common`; confirm the `ba2_providers` indicator registry key (`"pandas"` per the design's `PandasIndicatorCalc`). If the names differ, adopt the package's actual names.

- [ ] **Step 4: Backtest-DB lifecycle (separate sqlite, per run)**

`backend/app/services/backtest/backtest_db.py`:

```python
"""Per-run backtest trading DB. Distinct from (a) BA2TestPlatform's own results DB
(the Backtest table lives there) and (b) the live BA2TradePlatform DB.

The inherited AccountInterface/refresh_transactions/submit_order logic reads & writes
TradingOrder/Transaction/AccountDefinition rows via ba2_common.core.db; we point that
db at a throwaway sqlite file so a run is hermetic and reproducible."""
from __future__ import annotations
import os, tempfile, pathlib
from contextlib import contextmanager

from ba2_common.core import db as common_db


@contextmanager
def backtest_trading_db(run_id: int | str):
    """Configure ba2_common.core.db at a fresh sqlite for this run, create the schema,
    yield the file path, and (optionally) clean up. Schema is IDENTICAL to live so the
    inherited DB logic works unchanged."""
    root = pathlib.Path(tempfile.gettempdir()) / "ba2_backtest_dbs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"run_{run_id}.sqlite"
    if path.exists():
        path.unlink()
    common_db.configure_db(str(path))   # Phase-0 DB seam
    common_db.init_db()                 # SQLModel.metadata.create_all(get_engine())
    try:
        yield str(path)
    finally:
        # Keep the file for post-mortem debugging; engine is reset on next configure_db.
        pass


def seed_account_definition(account_id: int, settings: dict) -> int:
    """Insert an AccountDefinition row for the BacktestAccount into the backtest DB so the
    inherited code (which loads AccountDefinition by id) finds it. Returns the row id."""
    from ba2_common.core.models import AccountDefinition
    from ba2_common.core.db import add_instance
    # Field names per ba2_common.core.models.AccountDefinition (confirm before run).
    row = AccountDefinition(id=account_id, name=f"backtest-{account_id}",
                            account_type="backtest", enabled=True)
    return add_instance(row)
```

> **Re-plan checkpoint:** Confirm `AccountDefinition`'s required fields/column names in `ba2_common.core.models` (the recon shows it at `core/models.py:52`). Adjust `seed_account_definition` to the real required columns (e.g. `account_type` enum value, settings linkage) — fail-early if a required field is missing rather than defaulting.

- [ ] **Step 5: Write + run the seam-wiring test**

`backend/tests/backtest/test_seam_wiring.py`:

```python
def test_wire_seams_idempotent_and_resolves():
    from app.services.backtest.seam_wiring import wire_backtest_seams, get_backtest_resolver
    r1 = wire_backtest_seams()
    r2 = wire_backtest_seams()
    assert r1 is r2                                  # idempotent
    r1.register_account(99, "the-account")
    from ba2_common.core.instance_resolver import get_instance_resolver
    assert get_instance_resolver().get_account_instance(99) == "the-account"

def test_backtest_db_isolates(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db
    from ba2_common.core import db
    with backtest_trading_db("seamtest") as path:
        assert path.endswith("run_seamtest.sqlite")
        assert str(path) in str(db.get_engine().url)
```

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
mkdir -p tests/backtest && touch tests/backtest/__init__.py
./venv/bin/python -m pytest tests/backtest/test_seam_wiring.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): install ba2 packages + wire seams + per-run backtest DB (Phase 2 Task 1)"
```

---

## Task 2: `BacktestAccount` — ledger + read-only abstracts + the time machine

**Files:**
- Create `backend/app/services/backtest/backtest_account.py` — `BacktestAccount(AccountInterface)` (this task: the 12 `ReadOnlyAccountInterface` abstracts + `get_settings_definitions` + the price-cache override + the in-memory ledger).
- Create `backend/app/services/backtest/price_source.py` — the as-of OHLCV price source (the time-machine backing store).
- Test: `backend/tests/backtest/test_backtest_account_contract.py`.

- [ ] **Step 1: The as-of price source (time-machine backing store)**

`backend/app/services/backtest/price_source.py`:

```python
"""As-of OHLCV price source for the backtest. Backed by the Phase-2 provider cache
(ba2_providers OHLCV with as_of), wrapped in a virtual-clock that the engine advances.

This is the ONLY place that knows 'the current backtest bar'. _get_instrument_current_price_impl
delegates here; the engine calls set_clock() each bar."""
from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional


class AsOfPriceSource:
    def __init__(self, ohlcv_provider, interval: str = "1d"):
        self._ohlcv = ohlcv_provider          # ba2_providers OHLCV provider (as_of-aware)
        self._interval = interval
        self._clock: Optional[datetime] = None
        # Per-symbol pre-loaded bar frames keyed by date for O(1) bar lookup.
        self._bars: Dict[str, Dict] = {}      # symbol -> {date -> bar dict}
        self._dates: List[datetime] = []      # the trading-day clock

    def set_clock(self, as_of: datetime) -> None:
        self._clock = as_of

    def now(self) -> datetime:
        if self._clock is None:
            raise RuntimeError("AsOfPriceSource clock not set; engine must call set_clock() per bar")
        return self._clock

    def preload(self, symbols: List[str], start: datetime, end: datetime, warmup_days: int) -> None:
        """Pull each symbol's bounded history once (native-cache contract: one fetch, served sliced)."""
        from datetime import timedelta
        fetch_start = start - timedelta(days=warmup_days)
        for sym in symbols:
            res = self._ohlcv.get(sym, as_of=end, lookback=(end - fetch_start).days,
                                  interval=self._interval, format_type="dict")
            # Normalise to {date -> {open,high,low,close,volume}}. Confirm provider dict shape.
            self._bars[sym] = {_bar_date(b): b for b in _rows(res)}

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self._bars and len(self._bars[symbol]) > 0

    def close_at(self, symbol: str, as_of: Optional[datetime] = None) -> Optional[float]:
        d = _norm(as_of or self.now())
        bar = self._bars.get(symbol, {}).get(d)
        return float(bar["close"]) if bar else None

    def bar_at(self, symbol: str, as_of: Optional[datetime] = None) -> Optional[dict]:
        d = _norm(as_of or self.now())
        return self._bars.get(symbol, {}).get(d)

    def next_bar(self, symbol: str, after: datetime) -> Optional[dict]:
        """The NEXT trading bar strictly after `after` (for next-bar fills)."""
        cand = [d for d in self._bars.get(symbol, {}) if d > _norm(after)]
        return self._bars[symbol][min(cand)] if cand else None
```
Add small private helpers `_rows(res)`, `_bar_date(b)`, `_norm(dt)` (extract the list of bar dicts from the provider result, parse the bar's date, normalise to a date key).

> **Re-plan checkpoint:** The exact dict shape of `ohlcv_provider.get(..., format_type="dict")` is a Phase-2-provider output. Confirm the keys (`Date`/`date`, `open`/`Open`, …) from the installed `ba2_providers` OHLCV provider and write `_rows`/`_bar_date` to match. Per SHARED CONTRACTS, OHLCV `as_of→end_date`, `lookback→lookback_days`, anchor = bar Date; the existing `end_date` mask is already correct.

- [ ] **Step 2: `BacktestAccount` skeleton + the ledger + `get_settings_definitions`**

`backend/app/services/backtest/backtest_account.py` (start):

```python
"""BacktestAccount — a simulated broker implementing the live AccountInterface so the
real expert -> Recommendation -> TradeConditions -> classic RM -> position_sizing ->
submit_order path runs unchanged. Equities-only v1 (does NOT inherit OptionsAccountInterface)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder, Transaction, AccountDefinition
from ba2_common.core.types import OrderStatus, OrderType
from ba2_common.core.db import get_db, get_instance, add_instance, update_instance

from .price_source import AsOfPriceSource


@dataclass
class _Position:
    symbol: str
    qty: float = 0.0           # signed: +long / -short
    avg_price: float = 0.0
    realized_pl: float = 0.0


class BacktestAccount(AccountInterface):
    supports_trading = True
    supports_options = False

    def __init__(self, id: int, price_source: AsOfPriceSource, settings: Dict[str, Any]):
        super().__init__(id)                 # ROAI.__init__ registers self.id in _GLOBAL_PRICE_CACHE
        self._price = price_source
        self._cfg = settings                 # resolved dict: starting_cash, commission_per_trade, slippage_bps, fill_model
        self._cash: float = float(settings["starting_cash"])
        self._positions: Dict[str, _Position] = {}
        self._equity_snapshots: List[Dict[str, Any]] = []   # the equity curve
        self._broker_seq = 0

    # ---- settings ----------------------------------------------------------
    @staticmethod
    def get_settings_definitions() -> Dict[str, Any]:
        return {
            "starting_cash": {"type": "float", "required": True, "description": "Initial simulated cash"},
            "commission_per_trade": {"type": "float", "required": True, "description": "Flat $ commission per fill (or pct if fill_model says so)"},
            "slippage_bps": {"type": "float", "required": True, "description": "Slippage in basis points applied to market/stop fills"},
            "fill_model": {"type": "str", "required": True, "description": "next_bar_open | same_bar_close"},
        }
```

> No defaults in config access (`backend/CLAUDE.md`): every key read via `settings["…"]`, validated fail-early by the engine before the run (Task 5 Step 1).

- [ ] **Step 3: The 12 read-only abstracts**

Append to `BacktestAccount`:

```python
    # ---- ledger reads ------------------------------------------------------
    def get_balance(self) -> Optional[float]:
        return self._cash

    def get_account_info(self) -> Dict[str, Any]:
        equity = self._cash + self._open_positions_mtm()
        info = {"cash": self._cash, "equity": equity, "buying_power": max(self._cash, 0.0)}
        # _validate_position_size_limits reads float(account_info.equity); expose as attr too.
        return _AttrDict(info)

    def get_positions(self) -> Any:
        out = []
        for p in self._positions.values():
            if p.qty == 0:
                continue
            cur = self._price.close_at(p.symbol)
            out.append(_AttrDict({
                "symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price,
                "current_price": cur,
                "unrealized_pl": (None if cur is None else (cur - p.avg_price) * p.qty),
            }))
        return out

    def get_orders(self, status: Optional[Any] = None, fetch_all: bool = False) -> Any:
        with get_db() as s:
            from sqlmodel import select
            q = select(TradingOrder).where(TradingOrder.account_id == self.id)
            if status is not None and status != OrderStatus.ALL:
                q = q.where(TradingOrder.status == status)
            return list(s.exec(q))

    def get_order(self, order_id: str) -> Any:
        with get_db() as s:
            from sqlmodel import select
            row = s.exec(select(TradingOrder).where(TradingOrder.broker_order_id == str(order_id))).first()
            if row is None and str(order_id).isdigit():
                row = s.get(TradingOrder, int(order_id))
            return row

    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        return {s: self._price.has_symbol(s) for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type: str = "bid"):
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: self._price.close_at(s) for s in symbol_or_symbols}
        px = self._price.close_at(symbol_or_symbols)
        if px is None:
            raise ValueError(f"No backtest price for {symbol_or_symbols} at {self._price.now()}")
        return px

    def refresh_positions(self) -> bool:
        return True                         # ledger is local; nothing to sync

    def get_dividends(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        return []                           # v1: synthesize from corporate actions later

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        rows = self.get_orders(fetch_all=True)
        return [self._order_to_trade(o) for o in rows
                if o.status in OrderStatus.get_executed_statuses() and o.filled_qty]

    def get_balance_history(self, start_date=None, end_date=None) -> List[Dict]:
        return list(self._equity_snapshots)
```
Add the helpers: `_AttrDict` (dict that also exposes keys as attributes — needed because `_validate_position_size_limits` reads `account_info.equity`), `_open_positions_mtm()` (Σ qty×close), `_order_to_trade(o)` (derive a filled-trade dict), and a per-bar `snapshot_equity(as_of)` the engine calls (appends `{date, net_liquidating_value, cash_balance, equity_value}`).

- [ ] **Step 4: Override `get_instrument_current_price` to defeat the wall-clock cache**

```python
    def get_instrument_current_price(self, symbol_or_symbols, price_type: str = "bid"):
        """OVERRIDE: bypass the inherited _GLOBAL_PRICE_CACHE (wall-clock TTL) — the virtual
        clock moves faster than wall time, so caching leaks stale prices across bars."""
        return self._get_instrument_current_price_impl(symbol_or_symbols, price_type)
```
Confirm the inherited method name/signature in `ReadOnlyAccountInterface.py:233` matches exactly (it does per recon). The engine additionally pops `BacktestAccount._GLOBAL_PRICE_CACHE.pop(self.id, None)` each bar (Task 5).

- [ ] **Step 5: Contract test — `BacktestAccount` is concrete & read-only methods work**

`backend/tests/backtest/test_backtest_account_contract.py`:

```python
from datetime import datetime
import pytest

CFG = {"starting_cash": 100_000.0, "commission_per_trade": 1.0,
       "slippage_bps": 5.0, "fill_model": "next_bar_open"}

def _acct(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    wire_backtest_seams()
    ctx = backtest_trading_db("contract"); path = ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _FakePriceSource()                       # in-memory bars, no network
    acct = BacktestAccount(1, ps, CFG)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx

def test_backtest_account_is_concrete(tmp_path):
    acct, ctx = _acct(tmp_path)
    try:
        assert acct.get_balance() == 100_000.0
        assert acct.get_account_info().equity == 100_000.0     # _AttrDict attribute access
        assert acct.get_positions() == []
        assert acct.symbols_exist(["AAPL", "ZZZZ"]) == {"AAPL": True, "ZZZZ": False}
    finally:
        ctx.__exit__(None, None, None)

def test_no_abstractmethod_left():
    from app.services.backtest.backtest_account import BacktestAccount
    assert getattr(BacktestAccount, "__abstractmethods__", frozenset()) == frozenset()
```
Provide `_FakePriceSource` in the test (subclass `AsOfPriceSource` or a stub exposing `close_at`/`has_symbol`/`bar_at`/`next_bar`/`now`/`set_clock` over a hand-coded 5-bar AAPL series). `test_no_abstractmethod_left` is the gate-1 check (item 1 of the GATE) — it fails loudly if any of the 19 abstracts is unimplemented.

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/backtest/test_backtest_account_contract.py -v
```
Expected: PASS. (After Task 3 the `__abstractmethods__` set becomes empty.)

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): BacktestAccount ledger + read-only abstracts + time-machine price source (Phase 2 Task 2)"
```

---

## Task 3: `BacktestAccount` — the fill engine + order/TP/SL/OCO abstracts

**Files:**
- Edit `backend/app/services/backtest/backtest_account.py` (add the 6 `AccountInterface` abstracts + `refresh_orders` fill engine).
- Test: `backend/tests/backtest/test_backtest_account_fills.py`.

- [ ] **Step 1: `_submit_order_impl` — stage the order, assign a synthetic broker id**

```python
    def _next_broker_id(self) -> str:
        self._broker_seq += 1
        return f"BT-{self.id}-{self._broker_seq}"

    def _submit_order_impl(self, trading_order: TradingOrder, tp_price=None, sl_price=None,
                           is_closing_order: bool = False) -> Any:
        """Called by the INHERITED submit_order after validation/persistence. We do NOT
        reimplement submit_order; we only assign a broker id and set the working state.
        MARKET -> staged to fill next bar (or same-bar close per fill_model).
        LIMIT/STOP -> SUBMITTED/working; refresh_orders evaluates each bar."""
        trading_order.broker_order_id = self._next_broker_id()
        if trading_order.order_type == OrderType.MARKET:
            trading_order.status = OrderStatus.SUBMITTED   # filled by refresh_orders on the chosen bar
        else:
            trading_order.status = OrderStatus.SUBMITTED
        update_instance(trading_order)
        return trading_order
```

- [ ] **Step 2: `refresh_orders` — THE fill engine (per working order vs the current bar)**

```python
    def refresh_orders(self, heuristic_mapping: bool = False, fetch_all: bool = True) -> bool:
        """Evaluate every working order against the CURRENT bar; transition to FILLED /
        PARTIALLY_FILLED; update cash + position ledger; then trigger dependent TP/SL/OCO legs."""
        as_of = self._price.now()
        working = [o for o in self.get_orders(fetch_all=True)
                   if o.status in OrderStatus.get_open_order_statuses()]   # confirm helper name
        for o in working:
            bar = self._bar_for_fill(o, as_of)        # next bar (default) or same-bar close per fill_model
            if bar is None:
                continue
            fill_px = self._evaluate_fill(o, bar)     # None if not triggered this bar
            if fill_px is None:
                continue
            self._apply_fill(o, fill_px, as_of)
            self._trigger_dependents(o)               # OCO sibling cancel + TP/SL activation
        return True
```
Implement the helpers, each pinned to the contract's `fill_model`:

```python
    def _bar_for_fill(self, order, as_of):
        if self._cfg["fill_model"] == "same_bar_close":
            return self._price.bar_at(order.symbol, as_of)
        return self._price.next_bar(order.symbol, as_of)   # default next_bar_open

    def _slip(self, px, side_is_buy):
        bps = self._cfg["slippage_bps"] / 10_000.0
        return px * (1 + bps) if side_is_buy else px * (1 - bps)   # worsening direction

    def _evaluate_fill(self, o, bar):
        ot = o.order_type
        is_buy = ot in (OrderType.MARKET, OrderType.BUY_LIMIT, OrderType.BUY_STOP)
        if ot == OrderType.MARKET:
            ref = bar["open"] if self._cfg["fill_model"] == "next_bar_open" else bar["close"]
            return self._slip(ref, is_buy)
        if ot in (OrderType.BUY_LIMIT, OrderType.SELL_LIMIT):
            lim = o.limit_price
            if ot == OrderType.BUY_LIMIT and bar["low"] <= lim:   return lim
            if ot == OrderType.SELL_LIMIT and bar["high"] >= lim: return lim
            return None
        if ot in (OrderType.BUY_STOP, OrderType.SELL_STOP):
            stop = o.stop_price                                    # confirm field name in models
            if ot == OrderType.BUY_STOP and bar["high"] >= stop:  return self._slip(stop, True)
            if ot == OrderType.SELL_STOP and bar["low"] <= stop:  return self._slip(stop, False)
            return None
        return None

    def _apply_fill(self, o, fill_px, as_of):
        qty = o.quantity if o.quantity is not None else 0.0
        signed = qty if o.order_direction == _BUY else -qty       # confirm direction enum/field
        commission = self._cfg["commission_per_trade"]
        self._cash -= signed * fill_px + commission
        self._update_position(o.symbol, signed, fill_px)
        o.filled_qty = qty
        o.open_price = fill_px
        o.status = OrderStatus.FILLED
        update_instance(o)
```
Add `_update_position` (weighted-avg on increase; realized-PnL + cash on reduce/close), and confirm `OrderStatus.get_open_order_statuses()` vs the recon's `get_open_order_statuses`/`get_executed_statuses` (types.py shows both families exist; verify the exact method names before running).

> **Re-plan checkpoint:** Field/enum names to confirm against `ba2_common.core.models.TradingOrder` & `core.types` before running: `limit_price`, `stop_price` (the recon shows `limit_price`/`open_price`; confirm the stop field), `order_direction`/the BUY enum value, and the open-order status-set helper. The `OrderType` enum members (`MARKET`, `BUY_LIMIT`, `SELL_LIMIT`, `BUY_STOP`, `SELL_STOP`, `OCO`) are confirmed present (`types.py:250-259`).

- [ ] **Step 3: TP/SL leg + OCO abstracts (`adjust_tp`/`adjust_sl`/`adjust_tp_sl`)**

```python
    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """Create/replace a WAITING_TRIGGER TP leg: SELL_LIMIT for a long, BUY_LIMIT for a short."""
        is_long = self._txn_is_long(transaction)
        self._replace_leg(transaction, OrderType.SELL_LIMIT if is_long else OrderType.BUY_LIMIT,
                          price=new_tp_price, leg="TP", source=source)
        return True

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        is_long = self._txn_is_long(transaction)
        self._replace_leg(transaction, OrderType.SELL_STOP if is_long else OrderType.BUY_STOP,
                          price=new_sl_price, leg="SL", source=source)
        return True

    def adjust_tp_sl(self, transaction: Transaction, new_tp_price=None, new_sl_price=None,
                     source: str = "") -> bool:
        """Paired TP+SL as OCO (tag OrderType.OCO / 'OCO-' comment so the fill engine cancels
        the sibling and refresh_transactions recognises the close)."""
        ok = True
        if new_tp_price is not None: ok &= self.adjust_tp(transaction, new_tp_price, source)
        if new_sl_price is not None: ok &= self.adjust_sl(transaction, new_sl_price, source)
        self._tag_oco_pair(transaction)          # link the two legs so _trigger_dependents cancels the sibling
        return ok
```
Implement `_replace_leg` (cancel any existing same-leg WAITING/SUBMITTED order for the transaction, create a new `TradingOrder` with `depends_on_order`/`depends_order_status_trigger` set so it activates when the parent opens — fields confirmed at `models.py:260,369`), `_txn_is_long`, `_tag_oco_pair`, and `_trigger_dependents` (when one OCO leg fills, set its sibling to `CANCELED`).

- [ ] **Step 4: `cancel_order` + `modify_order`**

```python
    def cancel_order(self, order_id: str) -> Any:
        o = self.get_order(order_id)
        if o is None:
            return None
        o.status = OrderStatus.CANCELED
        update_instance(o)            # reserved cash/position is notional-only in this sim; nothing to release
        return o

    def modify_order(self, order_id: str, trading_order: TradingOrder = None) -> Any:
        o = self.get_order(order_id)
        if o is None or o.status in OrderStatus.get_terminal_statuses():
            return None
        if trading_order is not None:
            o.limit_price = trading_order.limit_price
            o.stop_price = getattr(trading_order, "stop_price", o.stop_price)
            o.quantity = trading_order.quantity
        update_instance(o)
        return o
```

- [ ] **Step 5: Fill-engine + cache-bust tests**

`backend/tests/backtest/test_backtest_account_fills.py`:

```python
CFG = {"starting_cash": 100_000.0, "commission_per_trade": 1.0,
       "slippage_bps": 0.0, "fill_model": "next_bar_open"}

def test_market_order_fills_next_bar_open():
    acct, ps = _acct_with_bars([(D1, o=100, h=101, l=99, c=100),
                                (D2, o=102, h=103, l=101, c=102)])
    ps.set_clock(D1)
    order = _make_market_buy("AAPL", qty=10)        # routed via inherited submit_order
    acct.submit_order(order)
    acct.refresh_orders()                            # fills at D2 open = 102
    o = acct.get_order(order.broker_order_id)
    assert o.status == OrderStatus.FILLED
    assert o.open_price == 102.0
    assert acct.get_balance() == 100_000.0 - 10*102.0 - 1.0

def test_limit_only_fills_when_bar_crosses():
    acct, ps = _acct_with_bars([(D1, 100,101,99,100), (D2, 102,103,101,102)])
    ps.set_clock(D1)
    order = _make_buy_limit("AAPL", qty=10, limit=98.0)
    acct.submit_order(order); acct.refresh_orders()
    assert acct.get_order(order.broker_order_id).status != OrderStatus.FILLED   # low 101 > 98

def test_slippage_worsens_market_fill():
    cfg = {**CFG, "slippage_bps": 50.0}              # 0.5%
    acct, ps = _acct_with_bars([(D1,100,101,99,100),(D2,102,103,101,102)], cfg=cfg)
    ps.set_clock(D1); o=_make_market_buy("AAPL",10); acct.submit_order(o); acct.refresh_orders()
    assert acct.get_order(o.broker_order_id).open_price == 102.0 * 1.005

def test_oco_sibling_cancelled_on_fill():
    # open a long, attach OCO TP+SL, drive a bar that hits TP -> SL cancelled.
    ...

def test_price_cache_busted_across_bars():
    acct, ps = _acct_with_bars([(D1,100,101,99,100),(D2,200,201,199,200)])
    ps.set_clock(D1); assert acct.get_instrument_current_price("AAPL") == 100.0
    ps.set_clock(D2); assert acct.get_instrument_current_price("AAPL") == 200.0   # not stale 100
```
Provide the `_acct_with_bars`/`_make_*` helpers (build `TradingOrder` rows with the real `OrderType`/direction enums, route through the **inherited** `submit_order` so validation/persistence runs). Fill in `test_oco_sibling_cancelled_on_fill`.

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/backtest/test_backtest_account_fills.py -v
```
Expected: PASS (this is GATE items 2 + 6).

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): BacktestAccount fill engine + TP/SL/OCO legs + cancel/modify (Phase 2 Task 3)"
```

---

## Task 4: The daily engine loop (universe → analyze_as_of → real ba2trade path → record)

**Files:**
- Create `backend/app/services/backtest/daily_engine.py` — the custom daily multi-asset simulator.
- Create `backend/app/services/backtest/context.py` — `BacktestContext` adapter (if Phase 1 did not export one usable here).
- Test: `backend/tests/backtest/test_daily_engine_unit.py`, `backend/tests/backtest/test_engine_golden_regression.py`.

- [ ] **Step 1: Confirm/adapt `BacktestContext` and `Recommendation` from Phase 1**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python - <<'PY'
import inspect
# Confirm Phase-1 contract names actually shipped:
from ba2_experts.FMPEarningsDrift import FMPEarningsDrift           # confirm import path
assert hasattr(FMPEarningsDrift, "analyze_as_of"), "Phase 1 analyze_as_of missing"
try:
    from ba2_common.core.interfaces.MarketExpertInterface import BacktestInterface  # confirm location
    print("BacktestInterface ok")
except Exception as e:
    print("CHECK BacktestInterface location:", e)
PY
```
> **Re-plan checkpoint:** Phase 1's SHARED CONTRACT defines `analyze_as_of(self, as_of, context)` calling `self._gather(context.providers, as_of)` + `self._process(bundle, settings, as_of)`, and `context` carrying `providers`, `settings`, `account`, `as_of`, `subtype`. If Phase 1 shipped a concrete `BacktestContext` class, **import and use it**; only create `context.py` below if Phase 1 left `context` as a duck-typed object the engine must construct.

`backend/app/services/backtest/context.py` (only if needed):

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

@dataclass
class BacktestContext:
    providers: Any           # as_of-aware ProviderBundle pointing at the backtest cache + backtest DB
    settings: Dict[str, Any] # resolved + optimizer-overridden per trial (Phase 5)
    account: Any             # BacktestAccount
    as_of: datetime          # the virtual clock (engine updates each bar)
    subtype: Optional[str] = None   # AnalysisUseCase ENTER_MARKET vs OPEN_POSITIONS
```

- [ ] **Step 2: The daily clock + universe hook**

`backend/app/services/backtest/daily_engine.py` (start):

```python
"""Custom daily multi-asset backtest engine. Drives the REAL ba2trade decision path:
universe -> expert.analyze_as_of(as_of) -> TradeConditions/ruleset -> classic RM ->
position_sizing -> BacktestAccount.submit_order -> fills -> record. Deterministic."""
from __future__ import annotations
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np

from app.services.backtest.seam_wiring import wire_backtest_seams, make_indicator_provider


def trading_days(start: datetime, end: datetime, price_source) -> List[datetime]:
    """The daily clock = the union of dataset trading days in [start, end]. Use the
    price source's own bar dates so the clock matches available data (no phantom bars)."""
    days = sorted({d for sym in price_source._bars for d in price_source._bars[sym]
                   if start <= d <= end})
    return days


def resolve_universe(as_of: datetime, config: Dict[str, Any], price_source) -> List[str]:
    """v1: static enabled_instruments from config, filtered to symbols with a bar on as_of.
    Phase 3 replaces the body with the historical screener reconstruction."""
    universe = config["enabled_instruments"]
    return [s for s in universe if price_source.bar_at(s, as_of) is not None]
```

- [ ] **Step 3: The per-bar core loop (the design §5 steps 1-7)**

```python
class DailyBacktestEngine:
    def __init__(self, *, account, experts, price_source, config, progress_cb=None):
        self.account = account
        self.experts = experts                 # list of (expert_instance, expert_settings)
        self.price = price_source
        self.config = config
        self.progress_cb = progress_cb or (lambda pct, msg: None)
        self.seed = config["seed"]

    def run(self) -> Dict[str, Any]:
        random.seed(self.seed); np.random.seed(self.seed)     # determinism_rule (1)
        from app.services.backtest.context import BacktestContext
        from ba2_common.core.TradeManager import get_trade_manager      # confirm export
        tm = get_trade_manager()
        days = trading_days(self.config["start_date"], self.config["end_date"], self.price)
        for i, as_of in enumerate(days):
            # 7-prep: advance the clock + BUST the per-account price cache (the gotcha).
            self.price.set_clock(as_of)
            type(self.account)._GLOBAL_PRICE_CACHE.pop(self.account.id, None)

            # 2. universe for the bar
            universe = resolve_universe(as_of, self.config, self.price)

            # 3. each expert/symbol: rec = analyze_as_of (= _gather + _process, the SAME live logic)
            for expert, settings in self.experts:
                ctx = BacktestContext(providers=expert_providers(expert, as_of, self.price),
                                      settings=settings, account=self.account, as_of=as_of,
                                      subtype=self.config.get("subtype"))
                for symbol in universe:
                    rec = expert.analyze_as_of(as_of, _with_symbol(ctx, symbol))
                    if rec.skip:
                        continue
                    # 4. REAL ba2trade path: Recommendation -> persisted ExpertRecommendation
                    #    -> TradeManager/RM -> position_sizing -> account.submit_order.
                    er = _recommendation_to_expert_recommendation(rec, expert, symbol, as_of)
                    tm.process_recommendation(er)          # creates the pending TradingOrder
                # classic RM sizes + prioritises the pending orders, then submits them.
                _run_risk_manager_and_submit(expert, self.account)

            # 5. fills on THIS bar's working orders; roll order state into transactions.
            self.account.refresh_orders()
            self.account.refresh_transactions()

            # 6. record per-bar equity / drawdown.
            self.account.snapshot_equity(as_of)
            self.progress_cb((i + 1) / max(len(days), 1) * 100.0, f"bar {as_of:%Y-%m-%d}")

        # 8. convert to the Backtest results dict (Task 5).
        from app.services.backtest.results import build_results
        return build_results(self.account, self.config)
```
Implement the small adapters: `expert_providers(expert, as_of, price_source)` (the as-of `ProviderBundle` the expert's `_gather` needs, backed by the backtest cache — confirm the Phase-1 bundle shape), `_with_symbol(ctx, symbol)`, `_recommendation_to_expert_recommendation(rec, …)` (map the Phase-1 `Recommendation` value object to a persisted `ExpertRecommendation` row in the **backtest** DB, mirroring live `run_analysis` step 6 — `core/models.py:88`), and `_run_risk_manager_and_submit` which calls `TradeRiskManagement.review_and_prioritize_pending_orders(expert_instance_id)` then `account.submit_order(order)` for each prioritised order (the **exact** live path — `TradeManager.py:1170`/`TradeRiskManagement.py:105`).

> **Re-plan checkpoint (the BACKTEST CONTRACT pivot):** SHARED CONTRACTS say the engine "routes rec through the REAL ba2trade path … `TradeConditions`/ruleset → classic Risk Manager → `position_sizing` → `BacktestAccount.submit_order`." The recon confirms the live route is `TradeManager.process_recommendation` → `TradeRiskManagement.review_and_prioritize_pending_orders` (which calls `compute_risk_based_quantity`/`get_latest_atr`) → `account.submit_order`. **Confirm at execution** whether Phase 1 expects the engine to (a) persist an `ExpertRecommendation` and reuse `TradeManager`/`TradeRiskManagement` verbatim (this plan's choice — maximum reuse, exercises the real RM), or (b) hand the `Recommendation` straight to a thinner enter/exit evaluator. Choice (a) is preferred because it is literally the live path; only fall back to (b) if `TradeManager` has live-only coupling that cannot be satisfied against the backtest DB (in which case extract the RM-sizing call `review_and_prioritize_pending_orders` and drive it directly). **FactorRanker is the documented exception:** it has NO `ExpertRecommendation` seam — its `_process` returns target weights handed directly to `submit_order` (out of scope this phase; EarningsDrift/InsiderClusterBuy both use the `ExpertRecommendation` path).

- [ ] **Step 4: Inject ATR into the RM sizing path**

`TradeRiskManagement` calls `position_sizing.get_latest_atr` for `atr_stop_mult` sizing. Phase 0 made `get_latest_atr(symbol, indicator_provider, …)` take an injected provider. Ensure `_run_risk_manager_and_submit` (or the RM construction) passes `make_indicator_provider()` (Task 1 Step 3) so ATR resolves against the backtest indicator cache.

> **Re-plan checkpoint:** Confirm how Phase 0 threaded `indicator_provider` into `TradeRiskManagement` (Phase 0 Task 6 Step 2 noted it threads an optional `indicator_provider=None` through the RM sizing call). Wire the engine to pass the backtest indicator provider at that exact seam.

- [ ] **Step 5: Unit test the loop on a fixed fake expert (no providers)**

`backend/tests/backtest/test_daily_engine_unit.py`: build a stub expert whose `analyze_as_of` returns a deterministic BUY `Recommendation` on day 1 for "AAPL" and HOLD after; drive a 5-bar series; assert (a) exactly one position opens, (b) `get_balance_history()` has 5 snapshots, (c) the final equity reflects the AAPL move, (d) the price cache was busted each bar (price source `now()` advanced). This isolates the loop mechanics from real experts/providers.

- [ ] **Step 6: Golden regression — analyze_as_of inside the loop == Phase-1 golden**

`backend/tests/backtest/test_engine_golden_regression.py`: pin `as_of`, mock providers to the Phase-1 fixture, and assert `expert.analyze_as_of(as_of, ctx)` called via the engine's context equals the Phase-1 golden `Recommendation` (signal/confidence/expected_profit_percent/details/skip), float-tolerant. This is GATE item 5 (the engine did not perturb decision logic).

> **Re-plan checkpoint:** Reuse Phase 1's golden fixtures/harness directly if exported; otherwise replicate the fixed-fixture + pinned-`current_price` approach from the Phase-1 golden_test contract.

- [ ] **Step 7: Run + commit**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/backtest/test_daily_engine_unit.py tests/backtest/test_engine_golden_regression.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): daily multi-asset engine driving the real ba2trade path (Phase 2 Task 4)"
```
Expected: PASS.

---

## Task 5: Results conversion + `daily_backtest` job handler + `Backtest` persistence

**Files:**
- Create `backend/app/services/backtest/results.py` — equity/drawdown/trades + metrics → the `Backtest` columns (reuse `_safe_float`/`_safe_duration_days`/profit_factor cap from `backtest_handler.py`).
- Create `backend/app/services/backtest/daily_backtest_handler.py` — `handle_daily_backtest(task_id, payload)`.
- Edit `backend/app/main.py` — register the handler (after line 257) + (optional) include a router.
- Edit `backend/app/api/backtests.py` — add a `POST /api/backtests/daily` route that queues the task.
- Test: `backend/tests/backtest/test_results_metrics.py`.

- [ ] **Step 1: Build the results dict (reuse the existing metric shape)**

`backend/app/services/backtest/results.py`:

```python
"""Convert a finished BacktestAccount run into the Backtest results dict + metric columns.
Mirrors backtest_handler._convert_bt_results() output shape so the SAME UI + Backtest
columns are populated; reuses _safe_float / _safe_duration_days / profit_factor cap."""
from __future__ import annotations
import math
from typing import Any, Dict, List

from app.services.backtest_handler import _safe_float, _safe_duration_days   # reuse existing guards


def build_results(account, config: Dict[str, Any]) -> Dict[str, Any]:
    snaps = account.get_balance_history()                  # [{date, net_liquidating_value, cash_balance, equity_value}]
    equity_curve = [{"date": s["date"], "equity": _safe_float(s["net_liquidating_value"])} for s in snaps]
    drawdown_curve = _drawdown_curve(equity_curve)
    trades = [_trade_row(t) for t in account.get_filled_trades()]
    initial = float(config["initial_capital"])
    final = equity_curve[-1]["equity"] if equity_curve else initial

    metrics = _compute_metrics(equity_curve, drawdown_curve, trades, initial, final)
    metrics["equity_curve"] = equity_curve
    metrics["drawdown_curve"] = drawdown_curve
    metrics["trades"] = trades
    return metrics
```
Implement `_drawdown_curve` (running peak → drawdown %), `_trade_row` (entry/exit/pnl/pnl_pct/bars_held/exit_reason — matching the `Backtest._transform_trades_for_frontend` field names so the UI maps cleanly), and `_compute_metrics` populating EVERY reused column: `total_return, sharpe_ratio, max_drawdown, win_rate, profit_factor (cap 999.99), total_trades, winning/losing_trades, avg_trade_duration, final_equity, best/worst_trade, exposure_time, buy_hold_return, annualized_return, volatility, sortino_ratio, calmar_ratio, sqn, expectancy, avg_drawdown, max_drawdown_duration, avg_trade, equity_peak`. Guard NaN/Inf via `_safe_float` (the existing helper).

- [ ] **Step 2: The job handler (contract: `handler(task_id, payload) -> result dict`)**

`backend/app/services/backtest/daily_backtest_handler.py`:

```python
"""'daily_backtest' task handler. Contract matches the existing handlers:
handler(task_id, payload) -> result dict; calls update_progress per simulated day;
polls is_task_paused for pause; persists to the Backtest table on completion."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict

from app.services.task_queue import get_task_queue
from app.models.backtest import Backtest
from app.models.database import SessionLocal     # confirm session factory name


REQUIRED_KEYS = ["name", "enabled_instruments", "experts", "start_date", "end_date",
                 "initial_capital", "commission", "slippage", "fill_model",
                 "fitness_metric", "seed"]


def handle_daily_backtest(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # No-defaults rule (backend/CLAUDE.md): validate fail-early.
    for k in REQUIRED_KEYS:
        if payload.get(k) is None:
            return {"status": "failed", "error": f"payload.{k} is required"}

    backtest_id = payload["backtest_id"]
    tq = get_task_queue()
    db = SessionLocal()
    try:
        bt = db.get(Backtest, backtest_id)
        bt.status = "running"; bt.started_at = datetime.utcnow(); db.commit()

        from app.services.backtest.seam_wiring import wire_backtest_seams
        from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
        from app.services.backtest.backtest_account import BacktestAccount
        from app.services.backtest.price_source import AsOfPriceSource
        from app.services.backtest.daily_engine import DailyBacktestEngine
        from ba2_providers import get_provider

        resolver = wire_backtest_seams()
        config = _build_config(payload)
        with backtest_trading_db(backtest_id):
            seed_account_definition(account_id=1, settings=config["account_settings"])
            ps = AsOfPriceSource(get_provider("ohlcv", "fmp"))            # confirm registry key
            ps.preload(config["enabled_instruments"], config["start_date"],
                       config["end_date"], warmup_days=config["warmup_days"])
            account = BacktestAccount(1, ps, config["account_settings"])
            resolver.register_account(1, account)
            experts = _build_experts(payload, resolver, ps)              # construct + register expert instances

            def progress(pct, msg):
                if tq.is_task_paused(task_id):
                    raise RuntimeError("paused")        # surface as paused per task_queue contract
                tq.update_progress(task_id, pct, msg)

            engine = DailyBacktestEngine(account=account, experts=experts, price_source=ps,
                                         config=config, progress_cb=progress)
            results = engine.run()

        _persist_results(db, bt, results)               # map results dict -> Backtest columns + JSON blobs
        bt.status = "completed"; bt.completed_at = datetime.utcnow(); db.commit()
        return {"status": "completed", "backtest_id": backtest_id}
    except Exception as e:
        bt = db.get(Backtest, backtest_id)
        if bt:
            bt.status = "failed"; bt.error_message = str(e)[:1000]; db.commit()
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
```
Implement `_build_config` (parse dates, assemble `account_settings={starting_cash=initial_capital, commission_per_trade=commission, slippage_bps=slippage, fill_model}`, default `warmup_days` from the longest indicator lookback), `_build_experts` (instantiate `FMPEarningsDrift`/`FMPInsiderClusterBuy` from `ba2_experts`, resolve their settings to plain dicts per the Phase-1 settings-dict contract, register in the resolver), and `_persist_results` (assign each metric column + `bt.results`/`bt.trades`/`bt.equity_curve`/`bt.drawdown_curve`).

> **Re-plan checkpoint:** Confirm BA2TestPlatform's SQLAlchemy session factory name (`SessionLocal` vs `get_db`) in `backend/app/models/database.py`, and the `ba2_providers` OHLCV registry key (`"fmp"`). Confirm expert constructor signature (`ExpertInstance` id-based per `MarketExpertInterface.__init__(self, id)`) — the backtest must seed an `ExpertInstance` row in the backtest DB or construct the expert with a synthetic id; mirror how Phase 1 instantiates experts for its golden test.

- [ ] **Step 3: Register the handler in `main.py`**

In `backend/app/main.py`, after the existing `task_queue.register_handler('backtest', handle_backtest)` (line 257), add:

```python
    from app.services.backtest.daily_backtest_handler import handle_daily_backtest
    task_queue.register_handler('daily_backtest', handle_daily_backtest)
```
(Import alongside the other handler imports in the startup block.)

- [ ] **Step 4: `POST /api/backtests/daily` route**

In `backend/app/api/backtests.py`, add a route that (1) creates a `Backtest` row (`status="pending"`, config fields from the request body; `model_id=None` per the Task-7 migration), (2) queues the task via `get_task_queue().queue_task(task_type="daily_backtest", name=..., payload={..., "backtest_id": bt.id})`, (3) returns the task id + backtest id. Mirror the existing `POST` in this file for shape/validation (no-defaults).

- [ ] **Step 5: Results-metrics unit test**

`backend/tests/backtest/test_results_metrics.py`: feed `build_results` a hand-made account stub with a known equity curve (e.g. 100k→110k→105k) and 3 trades; assert `total_return == 5.0`, `max_drawdown` ≈ the 110→105 dip, `profit_factor` finite & capped, `win_rate` correct, and `equity_curve`/`drawdown_curve`/`trades` present with the camelCase-ready field names the `Backtest.to_dict()` consumes.

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/backtest/test_results_metrics.py -v
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): results conversion + daily_backtest handler + API route (Phase 2 Task 5)"
```
Expected: PASS.

---

## Task 6: End-to-end clean-expert backtests + reproducibility (the GATE)

**Files:**
- Test: `backend/tests/backtest/test_daily_engine_e2e.py`, `backend/tests/backtest/test_daily_engine_reproducible.py`.
- Fixture: `backend/tests/backtest/fixtures/` — a small fixed OHLCV + signal fixture for `FMPEarningsDrift`/`FMPInsiderClusterBuy` so the run is hermetic (no network).

- [ ] **Step 1: Build a hermetic provider fixture**

Create a fixed cache fixture (a handful of symbols, ~60 daily bars, a planted earnings-surprise row and an insider-cluster window) under `tests/backtest/fixtures/`. Point the providers at it (either pre-seed the Phase-2 provider cache directory or inject a fixture provider via the resolver). This makes "same cache" concrete for reproducibility.

> **Re-plan checkpoint:** Use the Phase-2 native-cache layout (parquet for OHLCV, SQLite `provider_cache` for events) if Phase 2-providers landed before this phase; otherwise seed an in-memory fixture provider registered through `TradeConditions.set_provider_resolver`/the expert's `_gather` bundle. Confirm which provider plumbing Phase 1/2 expects.

- [ ] **Step 2: End-to-end FMPEarningsDrift run → stored `Backtest` row**

`backend/tests/backtest/test_daily_engine_e2e.py`:

```python
def test_earnings_drift_e2e_produces_completed_backtest(tmp_path):
    payload = _earnings_drift_payload(initial_capital=100_000, seed=42)
    result = handle_daily_backtest("e2e-1", payload)
    assert result["status"] == "completed"
    bt = _load_backtest(result["backtest_id"])
    assert bt.status == "completed"
    for m in (bt.total_return, bt.sharpe_ratio, bt.max_drawdown, bt.win_rate, bt.profit_factor):
        assert m is not None and math.isfinite(m)        # sane metrics (GATE item 3)
    assert bt.equity_curve and len(bt.equity_curve) >= 1
    assert bt.profit_factor <= 999.99                     # cap honoured

def test_insider_cluster_e2e(tmp_path):
    result = handle_daily_backtest("e2e-2", _insider_cluster_payload(seed=42))
    assert result["status"] == "completed"
```

- [ ] **Step 3: Reproducibility test (same cache + params + seed ⇒ identical)**

`backend/tests/backtest/test_daily_engine_reproducible.py`:

```python
def test_same_inputs_identical_equity_curve():
    p = _earnings_drift_payload(initial_capital=100_000, seed=7)
    r1 = handle_daily_backtest("rep-a", {**p, "backtest_id": _new_bt()})
    r2 = handle_daily_backtest("rep-b", {**p, "backtest_id": _new_bt()})
    b1, b2 = _load_backtest(r1["backtest_id"]), _load_backtest(r2["backtest_id"])
    assert b1.equity_curve == b2.equity_curve            # byte-identical (GATE item 4)
    assert b1.total_return == b2.total_return
    assert b1.sharpe_ratio == b2.sharpe_ratio
```
This is the design's "same cache + same params ⇒ identical result" at the engine layer and the determinism prerequisite Phase 5 (optimizer) depends on.

- [ ] **Step 4: Confirm BA2TradePlatform untouched + legacy ML path intact**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform status --short    # expect: only docs/plans/*phase2* (this file)
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/ -q -k "backtest or strategy or handle_backtest"   # legacy MLStrategy tests still green
```
Expected: BA2TradePlatform clean; legacy `MLStrategy`/`handle_backtest` tests pass (we added a NEW handler, changed nothing in the old one).

- [ ] **Step 5: Run the full Phase-2 suite**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m pytest tests/backtest/ -v
```
Expected: all green — this is GATE items 1-6.

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "test(backtest): e2e clean-expert backtests + reproducibility gate (Phase 2 Task 6)"
```

---

## Task 7: `Backtest`-model migration + UI surfacing + docs

**Files:**
- Create a migration under `backend/db_migrate/` making `Backtest.model_id` nullable (Decision 3a) and (optional) adding a `universe`/`engine_type` column.
- Edit `frontend/src/pages/Backtesting.tsx` — surface daily (expert) backtests (multi-asset selector/aggregate view) reusing the existing metric cards + tabs.
- Edit `backend/app/api/dashboard.py` — `recentActivity` already has a `'backtest'` type; ensure daily runs appear.
- Create `docs/` note documenting the equities-only/daily-only scope + the consensus-endpoint lookahead caveat for any later FMPRating run.

- [ ] **Step 1: Migration — `model_id` nullable + `engine_type` discriminator**

Add a migration (mirror the existing `backend/db_migrate/` style) altering `backtests.model_id` to `nullable=True` and adding `engine_type VARCHAR DEFAULT 'ml'` (values: `ml` | `daily_expert`) so the UI/queries can distinguish the two engines while sharing one table.

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
ls db_migrate/ | tail            # confirm the migration tooling/style
```
Update `backend/app/models/backtest.py`: `model_id = Column(Integer, ForeignKey("trained_models.id"), nullable=True)` and add the `engine_type` column + include it in `to_dict()` as `engineType`.

> **Re-plan checkpoint:** Confirm BA2TestPlatform's migration mechanism (`db_migrate/` scripts vs alembic). The recon shows a `backend/db_migrate/` dir — follow its existing pattern. Set `engine_type='daily_expert'` in the `POST /api/backtests/daily` route (Task 5 Step 4).

- [ ] **Step 2: Frontend — surface daily expert backtests**

In `frontend/src/pages/Backtesting.tsx`, reuse the existing metric cards (Total Return/Sharpe/Max DD/Win Rate/Profit Factor — fields `totalReturn`/`sharpeRatio`/`maxDrawdown`/`winRate`/`profitFactor`, lines ~149-153) and the tabbed equity/drawdown/trades recharts `AreaChart` (the `results.equityCurve`/`drawdownCurve` contract, lines ~126-127). Add a multi-asset affordance: a symbol/universe selector or an aggregate (portfolio-level) view, since the existing page assumes a single asset. Branch on `engineType === 'daily_expert'` to show the universe instead of a single model/dataset. (Recommend extracting the metric-cards+tabs into a shared component, but do not block on it.)

- [ ] **Step 3: Manual UI smoke**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
./venv/bin/python -m uvicorn app.main:app --reload   # then POST /api/backtests/daily and open the page
```
Expected: a daily-expert backtest appears in the list with populated metric cards + equity/drawdown charts.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(backtest): Backtest model migration (nullable model_id + engine_type) + daily UI surfacing (Phase 2 Task 7)"
```

---

## Self-Review

**Spec coverage (design §6 Phase 2 + §2 + §5 + SHARED CONTRACTS `backtest_account`/`engine_loop`/`golden_test`):**
- "Build `BacktestAccount(AccountInterface)` … next-bar fills + fees/slippage; multi-symbol cash/positions/equity; per-bar TP/SL/stop" → Tasks 2-3 (19 abstracts; `refresh_orders` fill engine; OCO legs; ledger). ✓
- "Daily engine loop: universe → `analyze_as_of` → REAL ba2trade path (TradeConditions/RM/position_sizing → submit_order) → fill → record" → Task 4 (drives `TradeManager.process_recommendation` → `TradeRiskManagement.review_and_prioritize_pending_orders` → `account.submit_order`, the confirmed live route). ✓
- "REUSE `Backtest` model + metrics + job-queue + UI + condition-eval helpers (custom simulator)" → Backtest table reused (Tasks 5/7), metrics via `_safe_float`/`_convert_bt_results` shape (Task 5), `TaskQueueService.queue_task`/`register_handler` (Tasks 5), `Backtesting.tsx` camelCase contract (Task 7), `strategy_executor` helpers available for ruleset eval (Task 4). NautilusTrader/vectorbt explicitly NOT used. ✓
- "Wire the ba2_common seams (set_instance_resolver, set_llm_service, TradeConditions.set_provider_resolver, configure_db, inject ATR)" → Task 1 (all 4 + ATR provider). ✓
- "Bust the per-account price cache each bar" → Task 4 Step 3 + `BacktestAccount.get_instrument_current_price` override (Task 2 Step 4); regression test (Task 3 Step 5). ✓
- "First daily backtests on FMPEarningsDrift + FMPInsiderClusterBuy" → Tasks 5-6. ✓
- "GATE: deterministic e2e producing a stored Backtest row with sane metrics + reproducibility (same cache+params ⇒ identical equity curve)" → GATE items 3-4, Task 6. ✓
- Equities-only v1 (NOT `OptionsAccountInterface`), daily cadence, classic RM only, separate backtest DB, ML path retained → Decisions 1-2/7, Tasks 2-4. ✓ (locked guardrails honoured)
- Phase-1 golden regression re-verified inside the loop → GATE item 5, Task 4 Step 6. ✓

**Placeholder scan:** every code step ships real code (settings defs, ledger, fill model with explicit MARKET/LIMIT/STOP/OCO branches, slippage math, the loop, results metrics, the handler, the migration). The deliberate `> Re-plan checkpoint:` notes mark genuine dependencies on not-yet-built Phase-1/Phase-2-provider outputs (Recommendation/BacktestContext shape, provider dict shape, model field names) — these are correctness guards for an autonomous run, NOT "TBD" filler. No defaults are introduced (per both CLAUDE.md no-defaults rules — config read via `[...]`, validated fail-early in the handler).

**Type/name consistency:** seam APIs are used consistently — `wire_backtest_seams`/`get_backtest_resolver` (Tasks 1,2,5); `backtest_trading_db`/`seed_account_definition` (Tasks 1,2,5); `AsOfPriceSource.set_clock`/`close_at`/`next_bar`/`bar_at`/`has_symbol`/`preload` (Tasks 2,3,4,5); `BacktestAccount` 19 abstracts pinned to the `ReadOnlyAccountInterface`/`AccountInterface` source line numbers; `DailyBacktestEngine.run` → `build_results` → `handle_daily_backtest` → `Backtest` columns is one consistent chain. The `daily_backtest` task type is consistent across handler/registration/route.

**Known reconciliation points (verify against source during execution, do not assume):** Phase-1 `Recommendation`/`BacktestContext`/`analyze_as_of` names & import paths; `ba2_providers` registry keys (`ohlcv`→`"fmp"`, `indicators`→`"pandas"`); `ba2_common.core.models` field names (`TradingOrder.stop_price`/`limit_price`/`order_direction`, `AccountDefinition` required cols, `ExpertInstance` construction); `OrderStatus` set-helper names (`get_open_order_statuses`/`get_executed_statuses`/`get_terminal_statuses`); `TradeManager.get_trade_manager`/`TradeRiskManagement.review_and_prioritize_pending_orders` exports & the ATR-injection seam Phase 0 left; BA2TestPlatform `SessionLocal`/`db_migrate` tooling; the provider `format_type="dict"` bar-dict shape.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase2-engine-backtestaccount-plan.md`. All code lands in **BA2TestPlatform/backend** (the backtest host) + a small migration; **BA2TradePlatform stays untouched** (its migration onto the packages is the separate Phase-6 plan) and the legacy `MLStrategy` engine is unchanged. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`). Task order is strict (1→2→3→4→5→6→7): Task 1 wires seams the rest need; Tasks 2-3 build the account the engine drives; Task 4 needs the account; Task 5 needs the engine; Task 6 is the GATE; Task 7 is UI/migration polish.
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

**Before starting:** resolve the Phase-1 `Re-plan checkpoint` in Task 4 Step 1 (confirm `Recommendation`/`BacktestContext`/`analyze_as_of` shipped and their exact shapes) — it gates Tasks 4-6. **GATE verification command** (run after Task 6): `./venv/bin/python -m pytest tests/backtest/ -v` must be fully green, with `test_no_abstractmethod_left`, `test_*_fills`, `test_price_cache_busted_across_bars`, `test_*_e2e_produces_completed_backtest`, `test_same_inputs_identical_equity_curve`, and `test_engine_golden_regression` all passing.
