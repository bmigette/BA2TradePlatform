# Backtest Platform — Phase 6 (Migrate BA2TradePlatform onto the packages) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the live **BA2TradePlatform** from its in-tree `ba2_trade_platform/{core, modules/dataproviders, modules/experts}` to *consuming* the three extracted packages (`ba2_common`, `ba2_providers`, `ba2_experts`), and **wire every Phase-0 seam at startup** so the live app keeps behaving identically. This kills the live/test divergence: after this phase there is one copy of the interfaces, providers, and clean experts (the packages), and BA2TradePlatform owns only the genuinely live-only pieces (concrete brokers, Smart RM, TradingAgents, the 3 AI providers, the LLM stack, the UI, the live runtime services).

**Strategy:** **Consume-by-shim** (default) — replace the in-tree extracted modules with thin re-export shims that import from the packages, so the ~hundreds of existing `from ba2_trade_platform.core.X import …` / `from ba2_trade_platform.modules.dataproviders import get_provider` call sites across the live code keep working unchanged, while the *implementation* now lives in the packages. The live-only modules that were intentionally NOT extracted (Decisions 4/5 of the Phase 0 plan: `ModelFactory`, `ChatKimiThinking`, `prompt_caching`, `LLMUsageTracker`, `AlpacaAccount`/`IBKRAccount`/`TastyTradeAccount`, the `modules/accounts` registry, `AINewsProvider`/`AICompanyOverviewProvider`/`AISocialMediaSentiment`, `TradingAgents`/`TradingAgentsUI`, Smart RM, the whole `ui/` tree, `ExpertInstanceCache`/`AccountInstanceCache`, `InstrumentAutoAdder`) **stay in-tree unchanged**. The seams are wired in a single new `core/seam_wiring.py` called once from `main.initialize_system()` before anything resolves an instance, an LLM, a provider, or a DB session.

**Tech Stack:** Python ≥3.11; the live platform’s existing `requirements.txt` gains the three `ba2trade-*` git deps (installed via `BA2TradeCommon/install.sh`). No change to SQLModel/NiceGUI/langchain/TradingAgents. The packages already exist and pass their own gates (Phase 0). This phase changes only BA2TradePlatform.

---

## Source of truth & repo locations

- **Live host (the only tree this phase edits):** `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform/ba2_trade_platform/` at branch `dev`, commit `72eefee` (verify with `git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform rev-parse --short HEAD` == `72eefee` before starting).
- **Packages (consumed read-only here):** `BA2TradeCommon` (`ba2_common`), `BA2TradeProviders` (`ba2_providers`), `BA2TradeExperts` (`ba2_experts`), siblings under `…/dev/BA2/`. Produced by the Phase 0 plan `docs/plans/2026-06-13-backtest-platform-phase0-plan.md` (esp. **Task 11**, which is the seed for this phase) and consumed by Phases 1–5.
- This plan is derived from the design doc `docs/plans/2026-06-13-backtest-platform-design.md` §6 (Phase 6), the Phase 0 plan (the three seams, the package layout, Task 11), and a file-by-file recon of the live `72eefee` tree.

> **Re-plan checkpoint (package surface):** This phase consumes the *outputs* of Phases 0–5. Several details below name functions/seams that Phases 0–5 *create* in the packages (`ba2_common.core.instance_resolver.set_instance_resolver`, `ba2_common.core.interfaces.LLMServiceInterface.set_llm_service`, `ba2_common.core.db.configure_db`, `ba2_common.core.TradeConditions.set_provider_resolver`, `ba2_common.core.position_sizing.get_latest_atr(symbol, indicator_provider, …)`, `ba2_experts.set_instrument_auto_adder_hook`, the experts’ `_gather`/`_process`/`analyze_as_of`). Before executing each task, confirm the **actual** exported names in the installed packages (`python -c "import ba2_common.core.instance_resolver as m; print(dir(m))"` etc.); if a name drifted during Phase 0–5 execution, use the real name. Do not invent a name the package does not export.

## Decisions taken (confirm before execution)

These resolve forks the recon surfaced. Override any at approval time.

1. **Consume-by-shim, not delete-and-rewrite (Model A).** Each extracted in-tree module becomes a one-line re-export of its package twin (`from ba2_common.core.types import *  # noqa` etc.). This keeps the live import surface (`ba2_trade_platform.core.*`, `ba2_trade_platform.modules.dataproviders.*`, `ba2_trade_platform.modules.experts.*`) stable so no live caller, UI page, test, or Alembic migration needs editing. *Alternative:* delete the in-tree dirs and rewrite every import to the package roots (bigger diff, higher blast radius on a live trading app). Shim-first is reversible (delete a shim → revert to in-tree copy) and lets the migration land incrementally.
2. **One wiring point, called first.** All seam injection happens in a new `ba2_trade_platform/core/seam_wiring.py::wire_all_seams()` invoked from `main.initialize_system()` **before** `init_db()` and before any provider/expert/account resolution. Idempotent (guards re-entry) so tests can call it too.
3. **DB seam uses the live path by default.** `wire_all_seams()` calls `ba2_common.core.db.configure_db(config.DB_FILE)` so the package DB engine targets the same `~/Documents/ba2_trade_platform/db.sqlite` the live app uses today. `--db-file` override still flows through (it sets `config.DB_FILE` before `initialize_system`).
4. **Live keeps its own `config.py`/`logger.py`.** The live `ba2_trade_platform/config.py` (CalVer version, live paths, the live `.env` keys) stays the source of truth for the live app; `ba2_common.config`/`ba2_common.logger` get *configured from* it at wiring time (paths) rather than replaced. The two co-exist; the package modules read the values the host pushes in.
5. **The 3 instance-factory functions move to a live `InstanceResolver` impl but stay callable under their old names.** `core/utils.py` keeps `get_expert_instance_from_id` / `get_account_instance_from_id` / `get_account_instance_from_transaction` (the Phase 0 plan deliberately did NOT copy these to `ba2_common`); they now delegate to / are wrapped by the new `core/instance_registry.py::LiveInstanceResolver`, which is what `set_instance_resolver()` receives. The `ExpertInstanceCache`/`AccountInstanceCache` singletons stay live and back the resolver.
6. **Golden-test reuse, not reinvention.** The Phase-1 golden test (`run_analysis`-equivalent decision == `analyze_as_of(now)`) is the acceptance gate; this phase re-runs it *through the wired live host* (same harness Phase 1 built, now importing the experts from `ba2_experts` and resolving providers/LLM/DB through the live wiring). The clean experts in scope: FMPEarningsDrift, FMPInsiderClusterBuy, FinnHubRating, FMPRating (documented consensus-lookahead caveat), FMPSenateTraderCopy (both subtypes), FMPSenateTraderWeight, FactorRanker.
7. **No behaviour change to live trading.** This is a *plumbing* migration. Any observable change to a live decision, order, sizing, or fill is a bug, caught by (a) the full live pytest suite staying green and (b) the golden test.

## The wiring map (what gets injected, by whom, when)

`wire_all_seams()` (new `core/seam_wiring.py`), called once at the **top** of `main.initialize_system()`:

| Seam (package side) | Live injection | Replaces in-tree back-edge |
|---|---|---|
| `ba2_common.core.db.configure_db(config.DB_FILE)` | live DB path | the package’s default DB path |
| `ba2_common.core.instance_resolver.set_instance_resolver(LiveInstanceResolver())` | `core/instance_registry.py` wrapping the 3 factory funcs + the two `InstanceCache`s | interface lazy `..utils.get_*_instance_from_id` |
| `ba2_common.core.interfaces.LLMServiceInterface.set_llm_service(ModelFactoryLLMService())` | `core/llm_service.py` adapting `ModelFactory.create_llm` / `do_llm_call_with_websearch` | Penny mixins’ direct `ModelFactory` import |
| `ba2_common.core.TradeConditions.set_provider_resolver(get_provider)` | the live `modules.dataproviders.get_provider` | TradeConditions lazy `from ..modules.dataproviders import get_provider` + `import fmpsdk` |
| `ba2_experts.set_instrument_auto_adder_hook(_auto_add_hook)` | wraps live `InstrumentAutoAdder.get_instrument_auto_adder().queue_symbols(...)` | Penny screening’s direct `InstrumentAutoAdder` import |
| ATR provider injection: live host passes a `ba2_providers` indicator provider into `position_sizing.get_latest_atr(symbol, indicator_provider, …)` | a live default-indicator-provider accessor used by the classic RM call site | `position_sizing` lazy `from ..modules.dataproviders.indicators.PandasIndicatorCalc` |

## Acceptance gate for Phase 6

1. **App boots end-to-end on the packages.** `python main.py --db-file <tmp> --port <free>` starts, `wire_all_seams()` runs first, the UI route module imports, no `InstanceResolverNotConfigured`/`LLMServiceNotConfigured`/“provider resolver not configured” at runtime. (Boot-smoke harness in Task 8.)
2. **Full live pytest suite green** at the same baseline as before Phase 6 (`pytest -q`), proving the shimmed imports + wiring did not regress anything.
3. **Golden test passes through the wired live host** for the clean experts (Decision 6): for each, `run_analysis`-equivalent decision `== analyze_as_of(now)` on `(signal, confidence, expected_profit_percent, details, skip/skip_reason)`, tolerance-equal on floats, with the experts imported from `ba2_experts` and all data/LLM/DB/instances resolved through the live wiring.
4. **No live-only module was extracted/broken:** `AlpacaAccount` order path, Smart RM, TradingAgents, the 3 AI providers, and the UI still import and function (covered by their existing tests in the suite).
5. **The in-tree extracted modules are now shims** (or deleted+rewritten if the alternative was chosen): `grep` shows the implementation bodies live in the packages, not duplicated in-tree.

---

## Task 1: Install the package chain into the live venv + dependency pinning

**Files (create/edit):** `BA2TradePlatform/requirements.txt` (append the 3 git deps), no code yet.

- [ ] **Step 1: Confirm baseline + record the green pytest baseline**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform rev-parse --short HEAD   # MUST print 72eefee
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
python -m pytest -q 2>&1 | tail -20    # record pass/fail/error counts as the regression baseline
```
Capture the summary line (e.g. `N passed, M skipped`). The Phase 6 gate (acceptance #2) compares against this exact baseline.

- [ ] **Step 2: Install the three packages editable into the live venv**

```bash
cd /Users/bmigette/Documents/dev/BA2
PYTHON="$(cd BA2TradePlatform && python -c 'import sys; print(sys.executable)')"
PYTHON="$PYTHON" bash BA2TradeCommon/install.sh --editable --ui
```
Expected: ends with `ok 0.1.0`. The `--ui` extra installs `nicegui` for `ba2_experts` (FactorRanker/Penny `ui.py`); the live app already has nicegui so this is a no-op upgrade-check.

> **Re-plan checkpoint:** `install.sh --editable` resolves sibling clones under `…/dev/BA2/`. If Phases 0–5 were executed in a worktree or the package branches are not the ones checked out, point `install.sh` at the correct clones or `pip install -e <path>` each package explicitly. Confirm `pip show ba2trade-common ba2trade-providers ba2trade-experts` reports the versions Phases 1–5 produced.

- [ ] **Step 3: Verify the packages import in the live interpreter and expose the seams**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python - <<'PY'
import ba2_common, ba2_providers, ba2_experts
from ba2_common.core import db, instance_resolver
from ba2_common.core.interfaces import LLMServiceInterface as L
from ba2_common.core import TradeConditions as TC
from ba2_common.core import position_sizing as PS
print("configure_db:", hasattr(db, "configure_db"))
print("set_instance_resolver:", hasattr(instance_resolver, "set_instance_resolver"))
print("set_llm_service:", hasattr(L, "set_llm_service"))
print("set_provider_resolver:", hasattr(TC, "set_provider_resolver"))
print("get_latest_atr:", hasattr(PS, "get_latest_atr"))
print("set_instrument_auto_adder_hook:", hasattr(ba2_experts, "set_instrument_auto_adder_hook"))
from ba2_experts import get_expert_class
print("FMPEarningsDrift class:", get_expert_class("FMPEarningsDrift") is not None)
PY
```
Expected: every line `True` and the expert class non-None. **No placeholders** — if any is `False`, the package surface drifted from this plan; reconcile the real name (Re-plan checkpoint above) before continuing.

- [ ] **Step 4: Pin the git deps in `requirements.txt`**

Append to `BA2TradePlatform/requirements.txt` (use the exact key’d names; keep the SSH form consistent with `install.sh`):

```
# --- Extracted BA2 packages (Phase 6 migration) ---
ba2trade-common @ git+ssh://git@github.com/bmigette/BA2TradeCommon.git@main
ba2trade-providers @ git+ssh://git@github.com/bmigette/BA2TradeProviders.git@main
ba2trade-experts[ui] @ git+ssh://git@github.com/bmigette/BA2TradeExperts.git@main
```

- [ ] **Step 5: Branch the live repo for the migration (never on `dev` directly)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
git checkout -b phase6-migrate-onto-packages
git add requirements.txt && git commit -m "chore(phase6): add ba2trade-{common,providers,experts} git deps"
```

---

## Task 2: The live `InstanceResolver` (`core/instance_registry.py`)

**Files:** create `ba2_trade_platform/core/instance_registry.py`; edit `ba2_trade_platform/core/utils.py` (delegate the 3 funcs); test `tests/test_instance_resolver_seam.py`.

The Phase 0 plan defined `ba2_common.core.instance_resolver.InstanceResolver` (Protocol: `get_expert_instance(id)`, `get_account_instance(id)`, `get_account_instance_from_transaction(txn)`) with a default `_UnconfiguredResolver` that raises `InstanceResolverNotConfigured`. The live host provides the concrete impl backed by the existing factory functions + caches (recon: `core/utils.py:98` `get_expert_instance_from_id`, `:317` `get_account_instance_from_id`, `:706` `get_account_instance_from_transaction`; caches `core/ExpertInstanceCache.py`, `core/AccountInstanceCache.py`).

- [ ] **Step 1: Write `core/instance_registry.py`**

Create `ba2_trade_platform/core/instance_registry.py`:

```python
"""Live InstanceResolver implementation for the seam defined in
ba2_common.core.instance_resolver. Wraps the existing factory functions +
singleton instance caches so package interface code can turn an expert/account
id (or a transaction) into a live instance without ba2_common importing the
live registries."""
from __future__ import annotations
from typing import Any

from ..logger import logger


class LiveInstanceResolver:
    """Concrete ba2_common InstanceResolver backed by the live caches + registries."""

    def get_expert_instance(self, expert_id: int) -> Any:
        # Import lazily to avoid import-time cycles during startup wiring.
        from .utils import get_expert_instance_from_id
        return get_expert_instance_from_id(expert_id)

    def get_account_instance(self, account_id: int) -> Any:
        from .utils import get_account_instance_from_id
        return get_account_instance_from_id(account_id)

    def get_account_instance_from_transaction(self, transaction: Any) -> Any:
        from .utils import get_account_instance_from_transaction
        # ba2_common passes either a Transaction row or its id; the live helper
        # is keyed by transaction_id. Accept both.
        txn_id = getattr(transaction, "id", transaction)
        return get_account_instance_from_transaction(txn_id)
```

> **Re-plan checkpoint:** Confirm the package `InstanceResolver` protocol method *names/signatures* against the installed `ba2_common.core.instance_resolver` (Phase 0 named them `get_expert_instance`/`get_account_instance`/`get_account_instance_from_transaction`). If Phase 0/1 settled on passing a `transaction_id` vs a `Transaction` object at the call sites in `AccountInterface`/`MarketExpertInterface`, align the `get_account_instance_from_transaction` argument handling (the `getattr(transaction, "id", transaction)` shim already covers both).

- [ ] **Step 2: Keep the 3 live factory funcs (they stay in `utils.py`)**

Do **not** delete `get_expert_instance_from_id` / `get_account_instance_from_id` / `get_account_instance_from_transaction` from `core/utils.py` — live code (e.g. `WorkerQueue`, `TradeManager`, UI pages) still calls them by name, and the resolver delegates to them. Leave `core/utils.py` lines 11–12 (`from ..modules.experts import get_expert_class` / `from ..modules.accounts import get_account_class`) in place — these are live-only registry imports and `core/utils.py` is a **live** module (the *package* twin `ba2_common.core.utils` is the pure subset; the live one keeps the registry funcs). The shimming of `core/utils.py` is handled carefully in Task 5 (it is a *split* shim, not a full re-export).

- [ ] **Step 3: Write the resolver seam test**

`tests/test_instance_resolver_seam.py`:

```python
def test_live_resolver_satisfies_protocol():
    from ba2_common.core.instance_resolver import InstanceResolver
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver
    r = LiveInstanceResolver()
    assert isinstance(r, InstanceResolver)  # runtime_checkable Protocol

def test_resolver_resolves_expert_from_db(expert_instance_factory):
    # expert_instance_factory is a conftest factory creating an ExpertInstance row
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver
    inst = expert_instance_factory(expert="FMPEarningsDrift")
    r = LiveInstanceResolver()
    obj = r.get_expert_instance(inst.id)
    assert obj is not None
    assert obj.id == inst.id
```

> **Re-plan checkpoint:** `expert_instance_factory` is assumed from the existing `tests/factories.py` / `tests/conftest.py` (recon confirmed `tests/factories.py` exists and conftest builds DB records). Confirm the real factory name/signature in `tests/factories.py`; if the factory creates an `ExpertInstance` differently, adapt the call. If no such factory exists for this expert type, build the row directly via `add_instance(ExpertInstance(expert="FMPEarningsDrift", ...))` using the real model fields.

- [ ] **Step 4: Run the resolver tests** (after Task 7 wiring is in, this passes; before wiring, the protocol check passes and the DB-resolution test needs the wired resolver — run it as part of Task 8)

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest tests/test_instance_resolver_seam.py -v
```
Expected: `test_live_resolver_satisfies_protocol` PASS now; the DB-resolution test PASS once `wire_all_seams()` (Task 7) has configured the DB.

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): live LiveInstanceResolver backed by factory funcs + instance caches"
```

---

## Task 3: The live `LLMServiceInterface` impl (`core/llm_service.py`)

**Files:** create `ba2_trade_platform/core/llm_service.py`; test `tests/test_llm_service_seam.py`.

Phase 0 defined `ba2_common.core.interfaces.LLMServiceInterface` (abstract `create_llm(...)` + `do_llm_call_with_websearch(...)`, returning `Any`) plus module-level `set_llm_service`/`get_llm_service`. The live `ModelFactory` provides both (recon: `core/ModelFactory.py:135` `create_llm`, `:893` `do_llm_call_with_websearch`; both are classmethods). The adapter forwards verbatim.

- [ ] **Step 1: Write `core/llm_service.py`**

```python
"""Live LLMServiceInterface implementation adapting ModelFactory, injected into
ba2_common so package expert code (PennyMomentumTrader's mixins) gets an LLM
without importing langchain/ModelFactory directly."""
from __future__ import annotations
from typing import Any, Optional, List, Dict

from ba2_common.core.interfaces.LLMServiceInterface import LLMServiceInterface
from .ModelFactory import ModelFactory


class ModelFactoryLLMService(LLMServiceInterface):
    def create_llm(
        self,
        model_selection: str,
        temperature: float = 0.0,
        streaming: Optional[bool] = None,
        callbacks: Optional[List[Any]] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        track_usage: bool = True,
        use_case: str = "LangChain LLM Call",
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None,
        **extra_kwargs: Any,
    ) -> Any:
        return ModelFactory.create_llm(
            model_selection,
            temperature=temperature,
            streaming=streaming,
            callbacks=callbacks,
            model_kwargs=model_kwargs,
            track_usage=track_usage,
            use_case=use_case,
            expert_instance_id=expert_instance_id,
            account_id=account_id,
            symbol=symbol,
            market_analysis_id=market_analysis_id,
            smart_risk_manager_job_id=smart_risk_manager_job_id,
            **extra_kwargs,
        )

    def do_llm_call_with_websearch(
        self,
        model_selection: str,
        prompt: str,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> str:
        return ModelFactory.do_llm_call_with_websearch(
            model_selection, prompt, max_tokens=max_tokens, temperature=temperature
        )
```

> The `create_llm`/`do_llm_call_with_websearch` parameter lists mirror `ModelFactory` exactly (recon `ModelFactory.py:135-151`, `:893-899`) and the Phase 0 `LLMServiceInterface` abstract signature exactly — no kwargs dropped, so usage-tracking (`expert_instance_id`/`account_id`/`symbol`/`market_analysis_id`) keeps flowing.

- [ ] **Step 2: Write the LLM-service seam test**

`tests/test_llm_service_seam.py`:

```python
def test_modelfactory_service_is_llmserviceinterface():
    from ba2_common.core.interfaces.LLMServiceInterface import LLMServiceInterface
    from ba2_trade_platform.core.llm_service import ModelFactoryLLMService
    assert isinstance(ModelFactoryLLMService(), LLMServiceInterface)

def test_create_llm_forwards_kwargs(monkeypatch):
    captured = {}
    from ba2_trade_platform.core import llm_service as ls
    def fake_create_llm(model_selection, **kw):
        captured["model"] = model_selection; captured["kw"] = kw; return ("llm", model_selection)
    monkeypatch.setattr(ls.ModelFactory, "create_llm", staticmethod(fake_create_llm))
    out = ls.ModelFactoryLLMService().create_llm(
        "openai/gpt5", temperature=0.0, use_case="UnitTest", expert_instance_id=7)
    assert out == ("llm", "openai/gpt5")
    assert captured["kw"]["expert_instance_id"] == 7
    assert captured["kw"]["use_case"] == "UnitTest"
```

- [ ] **Step 3: Run the LLM-service tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest tests/test_llm_service_seam.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): ModelFactory-backed LLMServiceInterface impl"
```

---

## Task 4: The instrument-auto-adder hook + the ATR indicator provider injection

**Files:** create `ba2_trade_platform/core/seam_helpers.py` (the auto-adder hook + the default indicator-provider accessor); these are *referenced* by `seam_wiring.py` (Task 7).

- [ ] **Step 1: Write the auto-adder hook + ATR provider accessor**

Penny’s `screening.py` (recon line 261/265) directly imported `InstrumentAutoAdder`. Phase 0/Task 9 converted that to `ba2_experts.get_instrument_auto_adder_hook()` (default no-op). The live host supplies the real hook. Create `ba2_trade_platform/core/seam_helpers.py`:

```python
"""Host-provided hooks/accessors injected into the packages at wiring time:
- the instrument-auto-adder hook (Penny screening uses it via ba2_experts),
- the default indicator provider for position_sizing.get_latest_atr injection."""
from __future__ import annotations
from typing import List

from ..logger import logger


def auto_add_instruments_hook(symbols: List[str]) -> None:
    """Live hook for ba2_experts.set_instrument_auto_adder_hook. Queues symbols
    into the live InstrumentAutoAdder service."""
    try:
        from .InstrumentAutoAdder import get_instrument_auto_adder
        adder = get_instrument_auto_adder()
        # Re-plan checkpoint: confirm the live queue method name on the auto-adder
        # service (recon: PennyMomentumTrader/screening.py:265-267 called
        # get_instrument_auto_adder() then queued symbols). Use that exact method.
        adder.queue_symbols(symbols)
    except Exception as e:
        logger.warning(f"auto_add_instruments_hook failed for {symbols}: {e}")


def get_default_indicator_provider():
    """Return a ba2_providers indicator provider for ATR fetches. Used by the
    classic RM / position_sizing call site so ba2_common never imports providers."""
    from ba2_providers import get_provider
    # Re-plan checkpoint: confirm the indicator provider category/name registered
    # in ba2_providers (Phase 0/2). The live tree used PandasIndicatorCalc under
    # the "indicators" category; mirror whatever ba2_providers registers.
    return get_provider("indicators", "pandas")
```

> **Re-plan checkpoints (two):** (1) The exact method to queue symbols on `InstrumentAutoAdder` — read `core/InstrumentAutoAdder.py` for the public method Penny used (recon shows `get_instrument_auto_adder()` then a queue call; confirm the method name, e.g. `queue_symbols`/`add_symbols`). (2) The indicator-provider registry key in `ba2_providers` (`get_provider("indicators", "<name>")`) — Phase 0 copied `PandasIndicatorCalc` into `ba2_providers/indicators/`; confirm the registered name (recon live key was used via `PandasIndicatorCalc` directly, so the registry key may be e.g. `"pandas"` or the class’s registered name).

- [ ] **Step 2: Confirm the `get_latest_atr` call site that needs the provider**

In the live tree, `position_sizing.get_latest_atr` (recon `core/position_sizing.py:206`) lazily imported `PandasIndicatorCalc`. The package twin (Phase 0/Task 6, Step 2) changed the signature to `get_latest_atr(symbol, indicator_provider, period=14, interval="1d")`. The live caller is `TradeRiskManagement` (the classic RM). Identify the live call site so the wiring can thread the provider:

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform/ba2_trade_platform
grep -rn "get_latest_atr" core/ modules/
```
Record each call site. The classic RM lives in `ba2_common.core.TradeRiskManagement` now (extracted) — so the *package* RM must receive the provider. Two acceptable patterns (pick the one Phase 0/Task 6 Step 2 actually implemented):
- (a) the package RM accepts an optional `indicator_provider=None` threaded from the host call; the live host passes `get_default_indicator_provider()` when invoking RM sizing, or
- (b) `position_sizing` exposes a module-level injectable default provider the host sets once.

> **Re-plan checkpoint:** Read the installed `ba2_common.core.position_sizing.get_latest_atr` and `ba2_common.core.TradeRiskManagement` to see which pattern Phase 0 shipped. If (a), wire by passing `get_default_indicator_provider()` at the RM invocation in the live `TradeManager`/order path; if (b), add `position_sizing.set_default_indicator_provider(get_default_indicator_provider())` to `wire_all_seams()`. Implement only the one that matches the package.

- [ ] **Step 3: Commit the helpers**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): seam helpers (auto-adder hook + default indicator provider)"
```

---

## Task 5: Shim the in-tree extracted modules to the packages

**Files (edit/replace with shims):** the in-tree modules that were copied into the packages in Phase 0 — under `ba2_trade_platform/core/`, `ba2_trade_platform/modules/dataproviders/`, `ba2_trade_platform/modules/experts/`. This is the surgical heart of the migration.

The **shim rule**: a module that was extracted **whole** becomes `from <package.module> import *  # noqa: F401,F403` **plus** an explicit re-export of any names `*` misses (private names, names not in `__all__`). A module that was **split** (only part extracted) keeps its live-only part and re-exports the extracted part. Modules that were **NOT extracted** (live-only per Phase 0 Decisions 4/5) are left **untouched**.

- [ ] **Step 1: Enumerate the extracted vs live-only modules (authoritative list)**

From the Phase 0 plan’s package layout + import-rewrite table, the extracted set is:
- **`ba2_common.core` ⇐ in-tree `core/`:** `types`, `option_types`, `option_selector`, `date_utils`, `text_utils`, `provider_utils`, `weinstein`, `models_registry`, `models`, `db`, `position_sizing`, `news_enrichment`, `TransactionHelper`, `TradeConditions`, `TradeActions`, `TradeActionEvaluator`, `TradeRiskManagement` (classic), `rules_documentation`, `rules_export_import` (exporter/importer only), and `core/interfaces/*` (the 17 base interface files). Plus `config`/`logger` (Decision 4: live keeps its own — **do not shim** `config.py`/`logger.py`).
- **`ba2_providers` ⇐ in-tree `modules/dataproviders/`:** the whole tree **minus** the 3 AI providers, plus `StockScreener` (which came from `core/`).
- **`ba2_experts` ⇐ in-tree `modules/experts/`:** the whole tree **minus** `TradingAgents`/`TradingAgentsUI`.

Live-only (untouched): `core/ModelFactory.py`, `core/ChatKimiThinking.py` (if present), `core/prompt_caching.py`, `core/LLMUsageTracker.py`, `core/ExpertInstanceCache.py`, `core/AccountInstanceCache.py`, `core/InstrumentAutoAdder.py`, `core/WorkerQueue.py`, `core/JobManager.py`, `core/TradeManager.py`, `core/SmartRiskManager*`, `core/SmartRiskManagerQueue.py`, `modules/accounts/*`, `modules/dataproviders/{news/AINewsProvider, fundamentals/overview/AICompanyOverviewProvider, socialmedia/AISocialMediaSentiment}.py`, `modules/experts/{TradingAgents,TradingAgentsUI}.py`, the live `RulesExportImportUI` (re-add it as a live UI module — see Step 5), `ui/*`, `config.py`, `logger.py`, `version.py`, `core/utils.py` (split — Step 4).

Print the live `core/` to cross-check nothing is missed:

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform/ba2_trade_platform
ls -1 core/*.py core/interfaces/*.py
```

> **Re-plan checkpoint:** This enumeration is from the Phase 0 plan’s layout. Before shimming, reconcile against what Phase 0 *actually* extracted (some optional modules like `ChatKimiThinking` may not exist at `72eefee`). For each in-tree `core/*.py`, decide: extracted-whole → shim; live-only → leave; split → Step 4. The single source of truth is “does `ba2_common.core.<name>` import successfully?” If yes and it was extracted, shim it; if `ImportError`, it stays live.

- [ ] **Step 2: Write the shim generator (verifies the package twin exists before shimming)**

Create `ba2_trade_platform/tools/make_shims.py` (live-repo dev tool):

```python
"""Replace an in-tree extracted module with a re-export shim of its package twin.
Refuses to shim if the package module does not import (prevents silent breakage).

Usage:
    python tools/make_shims.py core/types.py            ba2_common.core.types
    python tools/make_shims.py modules/experts/__init__.py ba2_experts
Writes a shim ONLY after confirming `import <pkg_module>` succeeds.
"""
import importlib, pathlib, sys

def main():
    rel_path = pathlib.Path("ba2_trade_platform") / sys.argv[1]
    pkg_module = sys.argv[2]
    try:
        importlib.import_module(pkg_module)
    except Exception as e:
        sys.exit(f"REFUSING to shim {rel_path}: package module {pkg_module} failed to import: {e}")
    shim = (
        f'"""Re-export shim: implementation lives in {pkg_module} (Phase 6 migration).\n'
        f'Kept so existing `from ba2_trade_platform...` imports resolve unchanged."""\n'
        f"from {pkg_module} import *  # noqa: F401,F403\n"
    )
    rel_path.write_text(shim, encoding="utf-8")
    print(f"shimmed {rel_path} -> {pkg_module}")

if __name__ == "__main__":
    main()
```

> `from X import *` only re-exports names in `X.__all__` (or public names if no `__all__`). For modules whose live callers import a **private** or non-`__all__` name (e.g. `from ba2_trade_platform.core.types import _SomeHelper`), the shim must additionally re-export it explicitly. Step 3 catches these via the import-smoke + full test run; fix by appending `from X import _SomeHelper` to that shim.

- [ ] **Step 3: Shim the `ba2_common.core` leaf + engine modules**

Run the generator for each extracted `core/*.py` (skip `config.py`/`logger.py`/`utils.py`/live-only):

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
for m in types option_types option_selector date_utils text_utils provider_utils weinstein \
         models_registry models db position_sizing news_enrichment TransactionHelper \
         TradeConditions TradeActions TradeActionEvaluator TradeRiskManagement \
         rules_documentation; do
  python tools/make_shims.py "core/$m.py" "ba2_common.core.$m"
done
```
For `rules_export_import.py` (split — package has exporter/importer only, the UI class stayed live): shim the exporter/importer and re-add the UI class in Step 5, so do **not** run the generic generator on it; handle it in Step 5.

Then shim each interface:

```bash
for i in DataProviderInterface MarketDataProviderInterface MarketIndicatorsInterface \
         CompanyFundamentalsOverviewInterface CompanyFundamentalsDetailsInterface \
         CompanyInsiderInterface MacroEconomicsInterface MarketNewsInterface \
         SocialMediaDataProviderInterface ScreenerProviderInterface \
         ReadOnlyAccountInterface AccountInterface OptionsAccountInterface \
         ExtendableSettingsInterface MarketExpertInterface LiveExpertInterface \
         SmartRiskExpertInterface; do
  python tools/make_shims.py "core/interfaces/$i.py" "ba2_common.core.interfaces.$i"
done
```
And the interfaces package `__init__`:
```bash
python tools/make_shims.py "core/interfaces/__init__.py" "ba2_common.core.interfaces"
```

> **DB shim caveat:** the in-tree `core/db.py` becomes `from ba2_common.core.db import *`. But `wire_all_seams()` (Task 7) must call `ba2_common.core.db.configure_db(config.DB_FILE)` **before** any code touches `get_engine()`. The shim re-exports `configure_db`/`get_engine`/`get_db`/`init_db`/`add_instance`/etc., so live callers of `from ba2_trade_platform.core.db import get_db, add_instance` keep working — they hit the package engine the host configured.

- [ ] **Step 4: Split-shim `core/utils.py` (the keystone)**

`core/utils.py` is **split**: the pure subset went to `ba2_common.core.utils`, but the live one keeps the 3 instance-factory funcs + the two top-level registry imports (`from ..modules.experts import get_expert_class`, `from ..modules.accounts import get_account_class`). Replace the **bodies** of the pure functions with a re-export of the package, while keeping the live-only funcs in place. Concretely, rewrite `core/utils.py` to:

```python
"""Live utils. The pure helpers are re-exported from ba2_common.core.utils
(single source of truth, Phase 6). The instance-factory functions + registry
glue stay live here (they need the live registries + instance caches)."""
# Pure helpers now live in the package:
from ba2_common.core.utils import *  # noqa: F401,F403
from ba2_common.core.utils import (  # explicit, in case __all__ is partial
    parse_fmp_amount_range, calculate_fmp_trade_metrics, calculate_transaction_pnl,
    close_transaction_with_logging, expert_uses_risk_manager,
    expert_schedules_open_positions, get_risk_manager_mode,
    # ... (the full pure list from the Phase 0 Task 5 split)
)

# Live-only registry + instance resolution (NOT extracted):
from .models import ExpertInstance, AccountDefinition  # etc., as the funcs need
from .db import get_instance, get_db
from ..modules.experts import get_expert_class
from ..modules.accounts import get_account_class
# ... keep get_expert_instance_from_id / get_account_instance_from_id /
#     get_account_instance_from_transaction bodies verbatim (Task 2 Step 2).
```

> **Re-plan checkpoint:** The precise pure-vs-live split is the Phase 0 Task 5 list (24 pure functions named there). Copy that exact list into the explicit re-export. The risk is a name the package’s `__all__` omits → `from … import *` silently drops it → a live caller breaks. The full test run (Step 7) + boot smoke (Task 8) catch this; for each `ImportError`/`AttributeError`, add the missing name to the explicit re-export. Do **not** duplicate the function body in the live file — re-export it.

- [ ] **Step 5: Re-add the live `RulesExportImportUI`**

Phase 0 dropped `RulesExportImportUI` (the nicegui class) from the package’s `rules_export_import.py`. The live UI needs it. Make `core/rules_export_import.py` a split shim: re-export `RulesExporter`/`RulesImporter` from the package and keep the live `RulesExportImportUI` class in-tree:

```python
"""Live rules export/import. Exporter/importer come from the package; the
nicegui UI class stays live."""
from ba2_common.core.rules_export_import import RulesExporter, RulesImporter  # noqa: F401

from nicegui import ui  # live-only UI dependency
# ... (paste the original RulesExportImportUI class body, lines ~376-end of the
#      72eefee source, unchanged — it only used RulesExporter/RulesImporter + ui).
```

> **Re-plan checkpoint:** Confirm `RulesExportImportUI` was the *only* live consumer broken by the package split (Phase 0 Task 6 Step 5 noted lines ~376-end). If other UI modules imported `RulesExportImportUI` from `core.rules_export_import`, they keep working (it’s still defined here).

- [ ] **Step 6: Shim the providers + experts trees**

Providers: the whole `modules/dataproviders/` tree was extracted minus the 3 AI providers. Shim the registry `__init__` and each sub-package `__init__` to the package, **but** the 3 AI providers + their registry entries stay live. The cleanest split: shim every extracted provider module to its `ba2_providers` twin, and keep the AI provider files + a live registry overlay. Replace `modules/dataproviders/__init__.py` with a **merge shim**:

```python
"""Live provider registry: ba2_providers registry + the 3 live AI providers."""
from ba2_providers import get_provider as _pkg_get_provider, *  # noqa: F401,F403

# Live-only AI providers (stayed in BA2TradePlatform, need ModelFactory):
from .news.AINewsProvider import AINewsProvider
from .fundamentals.overview.AICompanyOverviewProvider import AICompanyOverviewProvider
from .socialmedia.AISocialMediaSentiment import AISocialMediaSentiment

_LIVE_AI = {
    ("news", "ai"): AINewsProvider,
    ("fundamentals_overview", "ai"): AICompanyOverviewProvider,
    ("socialmedia", "ai"): AISocialMediaSentiment,
}

def get_provider(category, provider_name, **kwargs):
    cls = _LIVE_AI.get((category, provider_name))
    if cls is not None:
        return cls(**kwargs)
    return _pkg_get_provider(category, provider_name, **kwargs)
```

Then shim every **non-AI** provider module to its package twin (so `from ba2_trade_platform.modules.dataproviders.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider` still resolves):

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
# Example pattern; iterate over each extracted provider module file:
for f in $(find ba2_trade_platform/modules/dataproviders -name "*.py" \
          ! -name "AINewsProvider.py" ! -name "AICompanyOverviewProvider.py" \
          ! -name "AISocialMediaSentiment.py" ! -name "__init__.py"); do
  rel="${f#ba2_trade_platform/}"
  mod="ba2_providers.${rel#modules/dataproviders/}"; mod="${mod%.py}"; mod="${mod//\//.}"
  python tools/make_shims.py "$rel" "$mod"
done
```
Keep the AI provider files and the sub-package `__init__.py`s that re-export AI providers as **live merge shims** (re-export the package’s non-AI names + the live AI name). The generic generator refuses to shim a module whose package twin doesn’t exist, so AI provider files are auto-skipped (their `ba2_providers.*` twin was never created) — good.

Experts: shim each extracted expert + the `__init__` registry to `ba2_experts`, keep `TradingAgents`/`TradingAgentsUI` live. Replace `modules/experts/__init__.py` with a merge shim:

```python
"""Live expert registry: ba2_experts registry + live-only TradingAgents."""
from ba2_experts import get_expert_class as _pkg_get_expert_class, *  # noqa: F401,F403
from .TradingAgents import TradingAgents

def get_expert_class(expert_type):
    if expert_type == "TradingAgents":
        return TradingAgents
    return _pkg_get_expert_class(expert_type)
```
Then shim each non-TradingAgents expert module to its `ba2_experts` twin:

```bash
for f in $(find ba2_trade_platform/modules/experts -maxdepth 1 -name "*.py" \
          ! -name "TradingAgents.py" ! -name "TradingAgentsUI.py" ! -name "__init__.py"); do
  rel="${f#ba2_trade_platform/}"
  mod="ba2_experts.$(basename "${f%.py}")"
  python tools/make_shims.py "$rel" "$mod"
done
# FactorRanker/ and PennyMomentumTrader/ packages: shim their __init__ + submodules to ba2_experts.<pkg>.<sub>
```

> **Re-plan checkpoint (two):** (1) Whether `from pkg import *` is legal syntax combined with a named import on the same line — it is **not** in Python (`from x import *, name` is a SyntaxError). Split into two lines: `from ba2_providers import *` then `from ba2_providers import get_provider as _pkg_get_provider`. Fix the snippet shape during execution. (2) Confirm the live AI provider files still import (`ModelFactory` available) — they’re untouched, so they should.

- [ ] **Step 7: Import-smoke the shimmed live tree**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python - <<'PY'
import importlib
# wire DB first so package modules that touch the engine at import are safe:
import ba2_trade_platform.config as cfg
from ba2_common.core import db; db.configure_db(cfg.DB_FILE)
for m in ["ba2_trade_platform.core.types", "ba2_trade_platform.core.models",
          "ba2_trade_platform.core.db", "ba2_trade_platform.core.utils",
          "ba2_trade_platform.core.TradeConditions",
          "ba2_trade_platform.core.TradeRiskManagement",
          "ba2_trade_platform.core.interfaces",
          "ba2_trade_platform.modules.dataproviders",
          "ba2_trade_platform.modules.experts"]:
    importlib.import_module(m)
from ba2_trade_platform.modules.experts import get_expert_class
from ba2_trade_platform.modules.dataproviders import get_provider
assert get_expert_class("TradingAgents") is not None         # live
assert get_expert_class("FMPEarningsDrift") is not None      # package
print("SHIM IMPORTS OK")
PY
```
Expected: `SHIM IMPORTS OK`. Any `ImportError`/`AttributeError` here names a missing re-export — add it to the offending shim (Step 4/6 caveats).

- [ ] **Step 8: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): shim in-tree extracted modules to ba2_common/providers/experts"
```

---

## Task 6: Convert the in-tree back-edges that shims can’t paper over

Two live modules had back-edges the *package* twins removed via injection (Phase 0 Task 6 Steps 2–3): `TradeConditions` (lazy `get_provider` + `import fmpsdk`) and `position_sizing.get_latest_atr` (lazy `PandasIndicatorCalc`). Because we shim the in-tree `TradeConditions`/`position_sizing` to the **package** versions (which now use injected resolvers), the live behaviour is restored only once the resolvers are **wired** (Task 7). This task verifies the live call sites that *invoke* those package functions pass/inject what the package expects.

- [ ] **Step 1: TradeConditions provider resolver — confirm the wiring covers the 3 sites**

The package `TradeConditions` (Phase 0) replaced the 2 `get_provider("ohlcv","yfinance")` sites (recon `core/TradeConditions.py:1576,1634`) and the `import fmpsdk` / `historical_earning_calendar` site (`:1744,1752`) with `_get_provider(...)` routed through `set_provider_resolver`. `wire_all_seams()` (Task 7) calls `TradeConditions.set_provider_resolver(get_provider)`. Verify the live `get_provider` signature matches what the package resolver expects:

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform/ba2_trade_platform
grep -n "def get_provider" modules/dataproviders/__init__.py   # recon: (category, provider_name, **kwargs)
```
The package resolver was defined as `fn(category, name, **kw)` (Phase 0 Task 6 Step 3) — matches the live `get_provider(category, provider_name, **kwargs)`. The fmpsdk site: the package routes the earnings-calendar fetch through a provider method instead of raw `fmpsdk` — confirm the package’s `_get_provider("fundamentals_details","fmp")` (or whichever category Phase 0 chose) returns a provider exposing the past-earnings call the condition needs, and that the live `get_provider` registers that provider. No live edit needed if the registry already has it.

> **Re-plan checkpoint:** Read the installed `ba2_common.core.TradeConditions` to see the EXACT resolver signature and the EXACT provider category/method the fmpsdk site was rewritten to. The live `get_provider` must serve that `(category, name)`; recon confirms FMP fundamentals-details + ohlcv yfinance are both registered live. If Phase 0 chose a category the live registry lacks, register/alias it in the live `modules/dataproviders/__init__.py` merge shim.

- [ ] **Step 2: ATR provider injection — confirm the classic-RM call path**

From Task 4 Step 2, you identified the live `get_latest_atr` call sites and which injection pattern Phase 0 shipped. Apply the matching wiring:
- If pattern (a) (RM takes `indicator_provider` arg): at the live RM-sizing invocation (in `TradeManager`/the order path that calls the classic RM), pass `get_default_indicator_provider()`.
- If pattern (b) (module-level default): handled in `wire_all_seams()` (Task 7).

```bash
grep -rn "get_latest_atr\|TradeRiskManagement(" core/TradeManager.py core/ | head
```
Edit the identified call site(s) minimally to thread the provider. Keep the change tiny and local.

> **Re-plan checkpoint:** Only edit live call sites if pattern (a). The classic RM may already degrade gracefully when no ATR is available (Phase 0 Task 6 Step 2 said the pure `compute_risk_based_quantity` handles “no usable ATR” by returning a reasoned zero). Confirm whether the live app actually exercises ATR-based sizing for the in-scope clean experts; if those experts don’t use ATR sizing, this path is inert for the golden test (note it, don’t over-engineer).

- [ ] **Step 3: Commit (if any live call-site edits were needed)**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): thread provider/ATR injection into live TradeConditions/RM call sites" || true
```

---

## Task 7: `core/seam_wiring.py` + hook it into `main.initialize_system()`

**Files:** create `ba2_trade_platform/core/seam_wiring.py`; edit `main.py` (call it first); test `tests/test_seam_wiring.py`.

- [ ] **Step 1: Write `core/seam_wiring.py`**

```python
"""Single seam-wiring entry point. Injects the live implementations into the
ba2_common/ba2_providers/ba2_experts seams. Call ONCE at startup, before any
DB/provider/expert/LLM/instance resolution. Idempotent."""
from __future__ import annotations
import threading

from ..logger import logger

_wired = False
_lock = threading.Lock()


def wire_all_seams() -> None:
    global _wired
    with _lock:
        if _wired:
            return

        import ba2_trade_platform.config as config

        # 1) DB seam: point the package engine at the live sqlite path.
        from ba2_common.core import db
        db.configure_db(config.DB_FILE)

        # 2) Instance resolver.
        from ba2_common.core.instance_resolver import set_instance_resolver
        from .instance_registry import LiveInstanceResolver
        set_instance_resolver(LiveInstanceResolver())

        # 3) LLM service.
        from ba2_common.core.interfaces.LLMServiceInterface import set_llm_service
        from .llm_service import ModelFactoryLLMService
        set_llm_service(ModelFactoryLLMService())

        # 4) TradeConditions provider resolver.
        from ba2_common.core import TradeConditions
        from .seam_helpers import auto_add_instruments_hook
        # get_provider comes from the live merge-shim registry (AI providers + package).
        from ..modules.dataproviders import get_provider
        TradeConditions.set_provider_resolver(get_provider)

        # 5) Instrument auto-adder hook (Penny screening uses it via ba2_experts).
        import ba2_experts
        ba2_experts.set_instrument_auto_adder_hook(auto_add_instruments_hook)

        # 6) ATR provider injection (only if the package uses pattern (b);
        #    pattern (a) is wired at the RM call site in Task 6).
        try:
            from ba2_common.core import position_sizing
            if hasattr(position_sizing, "set_default_indicator_provider"):
                from .seam_helpers import get_default_indicator_provider
                position_sizing.set_default_indicator_provider(get_default_indicator_provider())
        except Exception as e:
            logger.warning(f"ATR provider injection skipped: {e}")

        _wired = True
        logger.info("All ba2_common/providers/experts seams wired to live implementations")
```

> **Re-plan checkpoint:** Steps 1–5 are stable (the seam APIs are fixed by Phase 0). Step 6’s `set_default_indicator_provider` is *conditional* on Phase 0 shipping pattern (b); the `hasattr` guard makes it safe either way. Confirm the exact `set_*` names against the installed packages (Task 1 Step 3 already prints them).

- [ ] **Step 2: Call `wire_all_seams()` first in `main.initialize_system()`**

Edit `main.py` `initialize_system()` (recon: it currently loads db/worker-queue modules then `init_db()` at line 82). Insert the wiring **before** `init_db()` — ideally before the first package import that could touch a seam. Add right after `logger.info("Initializing BA2 Trade Platform...")` (recon line 63) and before the folder-creation / `init_db()` block:

```python
    # Wire the extracted-package seams to live implementations BEFORE any
    # DB/provider/expert/LLM/instance resolution happens (Phase 6 migration).
    logger.info("Wiring ba2_common/providers/experts seams...")
    from ba2_trade_platform.core.seam_wiring import wire_all_seams
    wire_all_seams()
```

The existing `init_db()` (recon line 82) now hits the package engine the wiring configured. Because `core/db.py` is a shim to `ba2_common.core.db`, the `from ba2_trade_platform.core.db import init_db, get_db` at the top of `initialize_system` (recon line 52) resolves to the package `init_db`/`get_db` — which use the engine `configure_db` set. Order is correct as long as `wire_all_seams()` precedes `init_db()`.

> **Re-plan checkpoint:** `main.initialize_system()` imports `core.db.init_db` at recon line 52 (module import, no engine I/O — Phase 0 made the engine lazy). Verify the package `db.init_db` does not build the engine at import; it builds lazily via `get_engine()` inside `init_db()`. So importing the shim at line 52 is safe; only the *call* to `init_db()` at line 82 builds the engine, after wiring. If any other top-of-`initialize_system` import (worker queue, smart RM queue, job manager — recon lines 54–61) resolves an instance/provider/LLM at *import* time, move `wire_all_seams()` above those imports too. Boot smoke (Task 8) is the backstop.

- [ ] **Step 3: Write the wiring test**

`tests/test_seam_wiring.py`:

```python
def test_wire_all_seams_is_idempotent_and_configures(tmp_path, monkeypatch):
    import ba2_trade_platform.config as config
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "wired.sqlite"))
    from ba2_trade_platform.core.seam_wiring import wire_all_seams
    wire_all_seams()
    wire_all_seams()  # second call is a no-op (idempotent)

    from ba2_common.core import db
    assert str(tmp_path / "wired.sqlite") in str(db.get_engine().url)

    from ba2_common.core.instance_resolver import get_instance_resolver
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver
    assert isinstance(get_instance_resolver(), LiveInstanceResolver)

    from ba2_common.core.interfaces.LLMServiceInterface import get_llm_service
    from ba2_trade_platform.core.llm_service import ModelFactoryLLMService
    assert isinstance(get_llm_service(), ModelFactoryLLMService)
```

> **Re-plan checkpoint:** `wire_all_seams()` memoizes via `_wired`. If a *prior* test already wired with a different DB path, the idempotent guard returns early and the `get_engine().url` assertion would see the earlier path. For test isolation, either reset `seam_wiring._wired = False` in this test before calling, or assert the resolver/LLM-service types (which are path-independent) and check DB config in a dedicated process. Prefer resetting `_wired` in the test to make the DB assertion deterministic.

- [ ] **Step 4: Run the wiring + (now-unblocked) resolver test**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest \
  tests/test_seam_wiring.py tests/test_instance_resolver_seam.py \
  tests/test_llm_service_seam.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "feat(phase6): wire_all_seams() injected at startup in main.initialize_system"
```

---

## Task 8: Boot smoke + full live regression + golden test (the GATE)

**Files:** create `tests/test_boot_smoke.py`, `tests/test_phase6_golden.py`.

- [ ] **Step 1: Boot-smoke harness (no UI, no network) — acceptance #1**

`tests/test_boot_smoke.py`:

```python
def test_app_boots_through_wiring(tmp_path, monkeypatch):
    """Exercise the startup path up to (and including) init_db on the package
    engine, proving wire_all_seams runs first and nothing raises *NotConfigured."""
    import ba2_trade_platform.config as config
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "boot.sqlite"))

    from ba2_trade_platform.core import seam_wiring
    seam_wiring._wired = False
    seam_wiring.wire_all_seams()

    from ba2_trade_platform.core.db import init_db, get_db, add_instance, get_instance
    init_db()

    # Resolve an expert + an account through the wired seams (no *NotConfigured).
    from ba2_common.core.instance_resolver import get_instance_resolver
    r = get_instance_resolver()
    assert r is not None
    # A provider resolves through the live merge-shim registry:
    from ba2_trade_platform.modules.dataproviders import get_provider
    assert get_provider("ohlcv", "yfinance") is not None
```

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest tests/test_boot_smoke.py -v
```
Expected: PASS.

- [ ] **Step 2: Full process boot against a throwaway DB (real `main.py`)** — acceptance #1

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
PORT=$(python -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
timeout 45 python main.py --db-file /tmp/ba2_phase6_boot.sqlite --port "$PORT" > /tmp/ba2_boot.log 2>&1 &
BOOT_PID=$!
sleep 30
grep -q "All ba2_common/providers/experts seams wired" /tmp/ba2_boot.log && \
  grep -q "initialization complete" /tmp/ba2_boot.log && echo "BOOT OK" || \
  (echo "BOOT FAIL"; tail -40 /tmp/ba2_boot.log)
kill "$BOOT_PID" 2>/dev/null || true
```
Expected: `BOOT OK`. The log must show the wiring line BEFORE the DB-init line and contain no `InstanceResolverNotConfigured`/`LLMServiceNotConfigured`/`provider resolver not configured`.

> **Re-plan checkpoint:** Full boot also starts JobManager/WorkerQueue/SmartRiskManager/InstrumentAutoAdder (recon `main.py:91-113`) which may attempt live broker/network calls. If boot hangs on a live integration, the in-process boot-smoke (Step 1) is the authoritative acceptance for the *wiring*; treat full-process boot as best-effort and capture any live-integration failure as out-of-scope-for-Phase-6 (it predates the migration). Confirm the failure is NOT a seam error before excusing it.

- [ ] **Step 3: Full live regression — acceptance #2**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest -q 2>&1 | tail -25
```
Expected: the **same** pass/fail/skip baseline recorded in Task 1 Step 1. Any *new* failure is a migration regression — triage with `superpowers:systematic-debugging` (most likely a shim missing a non-`__all__` re-export; add it to the offending shim from Task 5).

- [ ] **Step 4: Golden test through the wired live host — acceptance #3 (the GATE)**

This re-runs the Phase-1 golden test, now with experts imported from `ba2_experts` and all I/O resolved through the live wiring. Reuse the Phase-1 harness if it exists; otherwise create `tests/test_phase6_golden.py`:

```python
"""Phase 6 acceptance: for each clean expert, the live decision (run_analysis-
equivalent _process(_gather(live, None), settings)) must equal analyze_as_of(now)
through the wired live host. Pins current_price identically in both paths so the
live-quote vs as_of-close difference cannot mask logic drift; mocks providers to a
fixed fixture."""
import math
from datetime import datetime, timezone
import pytest

from ba2_trade_platform.core.seam_wiring import wire_all_seams

CLEAN_EXPERTS = [
    "FMPEarningsDrift", "FMPInsiderClusterBuy", "FinnHubRating",
    "FMPRating",                      # documented consensus-lookahead caveat
    "FMPSenateTraderCopy",            # both subtypes (parametrized below)
    "FMPSenateTraderWeight", "FactorRanker",
]

def _floats_equal(a, b, tol=1e-6):
    if a is None or b is None:
        return a == b
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)

@pytest.fixture(scope="module", autouse=True)
def _wired(tmp_path_factory, monkeypatch_session):
    import ba2_trade_platform.config as config
    db = tmp_path_factory.mktemp("golden") / "golden.sqlite"
    monkeypatch_session.setattr(config, "DB_FILE", str(db))
    from ba2_trade_platform.core import seam_wiring
    seam_wiring._wired = False
    wire_all_seams()
    from ba2_trade_platform.core.db import init_db
    init_db()

@pytest.mark.parametrize("expert_name", CLEAN_EXPERTS)
def test_live_equals_analyze_as_of_now(expert_name, golden_fixture_for):
    """golden_fixture_for(expert_name) -> (expert, context, settings, fixed_price).
    Re-plan checkpoint: this fixture is the Phase-1 golden harness. Reuse it; it
    must (a) mock the expert's providers to a fixed bundle, (b) pin current_price
    in both paths, (c) supply the resolved settings dict."""
    expert, context, settings, fixed_price = golden_fixture_for(expert_name)
    now = datetime.now(timezone.utc)

    bundle_live = expert._gather(context.providers, as_of=None)
    bundle_live["current_price"] = fixed_price
    rec_live = expert._process(bundle_live, settings, as_of=None)

    rec_asof = expert.analyze_as_of(now, context)   # _gather(now)+_process

    assert rec_live.signal == rec_asof.signal
    assert _floats_equal(rec_live.confidence, rec_asof.confidence)
    assert _floats_equal(rec_live.expected_profit_percent, rec_asof.expected_profit_percent)
    assert rec_live.details == rec_asof.details
    assert rec_live.skip == rec_asof.skip
    assert rec_live.skip_reason == rec_asof.skip_reason
```

> **Re-plan checkpoint (critical):** The golden harness (`golden_fixture_for`, the provider mocking, the `Recommendation` field names, the `_gather`/`_process`/`analyze_as_of` signatures, and the FMPSenateTraderCopy subtype parametrization) are **produced by Phase 1**. Do NOT reinvent them — import/reuse the Phase-1 fixtures (likely under `BA2TradeExperts/tests/` or a shared golden module). If Phase 1 placed the harness in the package repo, run that harness here but with the seams wired by the live host (so it exercises live instance/LLM/DB/provider resolution). Confirm the actual `Recommendation` field set against `ba2_common` (Phase 1 contract: `signal, confidence, expected_profit_percent, current_price, details, raw_outputs, skip, skip_reason`). The `monkeypatch_session` fixture is a session-scoped monkeypatch (add to conftest if absent: `@pytest.fixture(scope="session") def monkeypatch_session(): mp = pytest.MonkeyPatch(); yield mp; mp.undo()`).

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest tests/test_phase6_golden.py -v
```
Expected: all parametrized cases PASS (the migration preserved live decision behaviour through the packages).

- [ ] **Step 5: Verify the in-tree extracted modules are shims, not duplicates — acceptance #5**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform
# Extracted modules should be ~1-3 lines of re-export, not full implementations:
for f in core/types.py core/TradeConditions.py core/position_sizing.py \
         core/interfaces/AccountInterface.py; do
  echo "== $f =="; wc -l "ba2_trade_platform/$f"; grep -c "import" "ba2_trade_platform/$f"
done
# Confirm no langchain leaked into the package-side imports (live keeps langchain):
grep -rn "ba2_trade_platform.modules\|ba2_trade_platform.core.ModelFactory" \
     /tmp 2>/dev/null || true
```
Expected: shimmed files are tiny (a few lines). The split shims (`utils.py`, `rules_export_import.py`, the two registry `__init__`s) are larger but contain only re-exports + the explicitly-retained live-only names.

- [ ] **Step 6: Commit the gate**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "test(phase6): boot smoke + golden test through wired live host (acceptance gate)"
```

---

## Task 9: Cleanup, docs, and finalize

- [ ] **Step 1: Update `CLAUDE.md` import guidance**

The live `CLAUDE.md` documents `from ba2_trade_platform.core.db import …` and `core/utils.py` helpers. Those imports still work (shims), but add a short note that the *implementation* now lives in `ba2_common`/`ba2_providers`/`ba2_experts` and that new shared code should be contributed to the packages (not the in-tree shims). Edit the “Avoid Code Duplication” + “Core Directory Structure” sections to point at the packages as the source of truth.

- [ ] **Step 2: Bump the live app version**

Per `CLAUDE.md` (“Before every git push, increment the build number NNNNN by 1”), bump `ba2_trade_platform/version.py` `APP_VERSION`.

- [ ] **Step 3: Final full regression + golden re-run**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest -q 2>&1 | tail -10
python -m pytest tests/test_phase6_golden.py tests/test_boot_smoke.py tests/test_seam_wiring.py -q
```
Expected: baseline-equal regression + all Phase-6 gate tests green.

- [ ] **Step 4: Commit + finish the branch**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform commit -m "docs(phase6): point CLAUDE.md at packages; bump version"
```
Use `superpowers:finishing-a-development-branch` to merge/PR `phase6-migrate-onto-packages` only after the user confirms (this touches the live trading app). Pushing is outward-facing — do it only on explicit approval.

---

## Self-Review

**Spec coverage (design §6 Phase 6 + Phase 0 Task 11 + shared contract `per_phase_scope.phase_6`):**
- “replace in-tree `ba2_trade_platform/{core,modules/dataproviders,modules/experts}` with consumption of the packages (shims or direct import + deletion)” → Task 5 (shim-by-default, Decision 1). ✓
- “create the live `instance_registry` (the 3 instance-factory funcs + caches) implementing `InstanceResolver`” → Task 2 (`LiveInstanceResolver` delegating to the retained factory funcs + `Expert/AccountInstanceCache`). ✓
- “provide a `ModelFactory`-backed `LLMServiceInterface`” → Task 3. ✓
- “WIRE all seams at startup (`set_instance_resolver`, `set_llm_service`, `TradeConditions.set_provider_resolver(get_provider)`, `configure_db`, `set_instrument_auto_adder_hook`, inject ATR provider)” → Task 7 `wire_all_seams()` (all six), with the auto-adder hook + ATR accessor in Task 4 and the provider/ATR call-site threading in Task 6. ✓
- “keep live-only pieces (AlpacaAccount/IBKR/TastyTrade, Smart RM, TradingAgents, the 3 AI providers, UI, LLM stack)” → Task 5 Step 1 enumerates them as untouched; merge-shims preserve the AI providers + TradingAgents in the registries (Task 5 Step 6). ✓
- “can run parallel/last” → branch `phase6-migrate-onto-packages`, BA2TradePlatform untouched until this phase; all prior phases already landed in the packages. ✓
- GATE: “full live test suite green + the golden test (run_analysis == analyze_as_of(now)) for the clean experts + app boots and run_analysis works end-to-end” → Task 8 (boot smoke #1, full regression #2, golden #3). ✓

**Locked-decision/seam compliance:** Classic RM only (ATR injection via `get_latest_atr(symbol, indicator_provider)`, no smart RM touched — Smart RM stays live, Task 5). Equities-first/options-ready (no options work here; `OptionsAccountInterface` rides along in the shimmed interfaces). Extract-by-copy honored — this phase *consumes* the copies; it does not re-extract. The six `ba2_common` seams are all wired (Task 7). BA2TradePlatform is only modified in *this* phase (Decision 7: plumbing only, no live-behaviour change), satisfying “keep BA2TradePlatform safe except where the phase explicitly migrates it.”

**Placeholder scan:** All new live modules (`instance_registry.py`, `llm_service.py`, `seam_helpers.py`, `seam_wiring.py`, `tools/make_shims.py`) and all tests contain full code. Shim bodies are exact re-export lines. The `> Re-plan checkpoint:` notes are deliberate guards for details that genuinely depend on Phases 0–5 outputs (the real exported seam names, the golden harness location, the InstrumentAutoAdder queue method, the indicator-provider registry key, which ATR injection pattern shipped) — they instruct the executor to confirm-then-use the real value, never to fabricate. No “TBD”/“add error handling”.

**Type/name consistency:** Seam APIs match across tasks and the Phase 0 plan — `configure_db` (Task 7), `set_instance_resolver`/`get_instance_resolver` + `LiveInstanceResolver.get_expert_instance/get_account_instance/get_account_instance_from_transaction` (Tasks 2,7,8), `set_llm_service`/`get_llm_service` + `ModelFactoryLLMService.create_llm/do_llm_call_with_websearch` (Tasks 3,7,8), `TradeConditions.set_provider_resolver(get_provider)` (Tasks 6,7), `ba2_experts.set_instrument_auto_adder_hook` (Tasks 4,7), `position_sizing.get_latest_atr(symbol, indicator_provider, …)` (Tasks 4,6,7). `ModelFactoryLLMService.create_llm` mirrors `ModelFactory.create_llm` (recon `ModelFactory.py:135-151`) param-for-param. `wire_all_seams()` is called before `init_db()` (recon `main.py:82`) and is idempotent.

**Known reconciliation points (verify against the installed packages + Phase-1 harness during execution, do not assume):** the exact exported seam names in `ba2_common`/`ba2_experts` (Task 1 Step 3 prints them); the InstanceResolver `from_transaction` arg shape (id vs row); the `InstrumentAutoAdder` queue method name; the `ba2_providers` indicator-provider registry key; which `get_latest_atr` injection pattern (a/b) Phase 0 shipped; whether any extracted shim must explicitly re-export a non-`__all__` name (caught by Task 5 Step 7 + Task 8 Step 3); the Phase-1 golden harness location + `Recommendation` field names + FMPSenateTraderCopy subtype parametrization; the legality of combined `from x import *, name` lines (split into two — Task 5 Step 6 caveat).

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase6-migrate-live-plan.md`. This phase edits ONLY `BA2TradePlatform` (on branch `phase6-migrate-onto-packages`); the three packages are consumed read-only and must already be installed (Phases 0–5 landed). Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`). Pause after Task 5 (the shim surgery) and Task 8 (the gate) for human review, since this touches a live trading app.
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

Prerequisites before starting: Phases 0–5 complete and the three `ba2trade-*` packages installable (`install.sh --editable`). The acceptance gate is Task 8 (boot smoke + full live regression at baseline + golden test through the wired host). Do not merge/push without explicit user approval — this is the live trading platform.
