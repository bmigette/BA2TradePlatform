# TradingAgents Memory Scope + Strategy Notes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the TradingAgents memory/reflection system from generalizing "broken chart" lessons from unrelated tickers to every new candidate, and give users a way to tell the AI analysts the strategy intent (e.g. "this screener intentionally selects falling-knife stocks for buy-the-dip").

**Architecture:**
- `FinancialSituationMemory` (SQL-backed memory shim in the TradingAgents thirdparty package) gains 3 config-driven knobs: `memory_injection_scope` (`none`/`same_symbol`/`all_symbols`), `memory_max_trades`, `memory_lookback_days`. These are read from the `config` dict already passed into its constructor (currently ignored).
- A new free-form `analysis_strategy_notes` expert setting is threaded through `TradingAgentsGraph` config → `GraphSetup.setup_graph` → the Research Manager, Trader, and Risk Manager node creators → their prompt templates, rendered as an optional "Strategy Context" block. The Bull/Bear Researcher debate prompts are deliberately left unchanged (see Task 4) so the debate stays an independent check rather than a rubber stamp.
- New settings are added to `TradingAgents.get_settings_definitions()` and wired into `_create_tradingagents_config()`.

**Tech Stack:** Python, SQLModel, pytest, existing `tests/conftest.py` in-memory SQLite fixtures + `tests/factories.py`.

**Background (do not re-derive — already investigated this session):**
- Prod expert instance 5 ("TA-Dynamic-GPT5") uses `screener_sort_metric=price_drop_pct`, `screener_price_drop_pct=15`, `screener_price_drop_days=7` — i.e. it intentionally analyzes the biggest 7-day droppers.
- The last run (2026-06-10) produced 12 SELL + 7 UNDERWEIGHT, 0 BUY/HOLD/OVERWEIGHT. 18/19 judge decisions explicitly cited "past mistakes/lessons" (e.g. "my recent losses in SYM, FLS, and TOST").
- Those cross-ticker losses came from `FinancialSituationMemory._fetch_cross_ticker_summary()` (last 30 days, all symbols, no scope control). Since every new candidate from this screener has a "broken chart" by construction, the retrieved lesson ("don't buy broken charts before confirmation") is wrongly generalized to every candidate, even ones with strongly improving fundamentals (e.g. AVEX: revenue +306.9% YoY, swung to profit, beat EPS by 56.2%, but still got SELL on technical/dilution grounds).
- The user's diagnosis: those past cross-ticker losses were likely caused by stop-losses set too tight on otherwise-correct mean-reversion theses, not because the BUY thesis was wrong — so the memory is teaching the wrong lesson account-wide.

---

### Task 1: Add new settings to `TradingAgents.get_settings_definitions()`

**Files:**
- Modify: `ba2_trade_platform/modules/experts/TradingAgents.py`
- Test: `tests/test_experts/test_tradingagents_settings.py` (new)

**Step 1: Write the failing test**

Create `tests/test_experts/test_tradingagents_settings.py`:

```python
"""Tests for new TradingAgents memory-scope and strategy-notes settings."""
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents


class TestNewSettingsDefinitions:
    def test_memory_injection_scope_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_injection_scope"]
        assert d["type"] == "str"
        assert d["default"] == "same_symbol"
        assert d["valid_values"] == ["none", "same_symbol", "all_symbols"]

    def test_memory_max_trades_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_max_trades"]
        assert d["type"] == "int"
        assert d["default"] == 2

    def test_memory_lookback_days_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["memory_lookback_days"]
        assert d["type"] == "int"
        assert d["default"] == 14

    def test_analysis_strategy_notes_definition(self):
        defs = TradingAgents.get_settings_definitions()
        d = defs["analysis_strategy_notes"]
        assert d["type"] == "str"
        assert d["required"] is False
        assert d["default"] == ""
```

**Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_experts/test_tradingagents_settings.py -v`
Expected: FAIL with `KeyError: 'memory_injection_scope'` (and similar for the others).

**Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/experts/TradingAgents.py`, inside `get_settings_definitions()`, add the new keys near the existing `"use_memory"` entry (around line 228-232):

```python
            "use_memory": {
                "type": "bool", "required": True, "default": True,
                "description": "Use memory system for context-aware analysis",
                "tooltip": "When enabled, agents retrieve past experiences and recommendations from memory to inform current decisions. Memories are always stored but only used when this is enabled. Disabling may be useful for fresh analysis without historical bias."
            },
            "memory_injection_scope": {
                "type": "str", "required": False, "default": "same_symbol",
                "description": "Memory/Reflection Injection Scope",
                "valid_values": ["none", "same_symbol", "all_symbols"],
                "help": "Controls which past trade history and reflections are injected into the bull/bear/trader/judge prompts. 'none' disables memory injection entirely. 'same_symbol' (default) only injects past analyses/outcomes for the same symbol. 'all_symbols' additionally includes a cross-ticker summary of recent wins/losses across the whole account.",
                "tooltip": "Scope of past-trade memory injected into AI prompts. 'same_symbol' avoids generalizing lessons from unrelated tickers (e.g. a tight-stop loss on ticker A) to every new candidate."
            },
            "memory_max_trades": {
                "type": "int", "required": False, "default": 2,
                "description": "Max Past Trades Injected per Symbol",
                "tooltip": "Maximum number of past completed analyses for the same symbol to inject into prompts as memory/reflection context."
            },
            "memory_lookback_days": {
                "type": "int", "required": False, "default": 14,
                "description": "Memory Lookback Window (days)",
                "tooltip": "Only past analyses/trades from within this many days are eligible for memory injection (applies to both same-symbol and cross-ticker history). Keeps reflections relevant to the current strategy/market regime."
            },
            "analysis_strategy_notes": {
                "type": "str", "required": False, "default": "",
                "description": "Trading Strategy Notes for AI Analysts",
                "help": "Free-form notes describing this instance's intended strategy, injected into the bull/bear researchers, investment judge, trader, and risk manager prompts. Use this to give the AI context that isn't obvious from the data alone — e.g. 'this screener intentionally selects stocks that just dropped >15% as buy-the-dip candidates; a broken short-term chart is the entry condition, not new bearish information.'",
                "tooltip": "High-level strategy context shared with all analysis/decision agents (separate from the Smart Risk Manager's user instructions, which apply to position management)."
            },
```

**Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_experts/test_tradingagents_settings.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add ba2_trade_platform/modules/experts/TradingAgents.py tests/test_experts/test_tradingagents_settings.py
git commit -m "feat(tradingagents): add memory scope and strategy-notes settings"
```

---

### Task 2: Wire the new settings into `_create_tradingagents_config`

**Files:**
- Modify: `ba2_trade_platform/modules/experts/TradingAgents.py:314-328` (`config.update({...})` block inside `_create_tradingagents_config`)
- Test: `tests/test_experts/test_tradingagents_settings.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_experts/test_tradingagents_settings.py`. This needs a real `TradingAgents` instance backed by the test DB — use the `tests/factories.py` helpers already used elsewhere.

```python
import pytest
from ba2_trade_platform.core.types import AnalysisUseCase
from tests.factories import create_account_definition, create_expert_instance


@pytest.fixture
def ta_expert(db_session):
    account = create_account_definition()
    instance = create_expert_instance(account.id, expert="TradingAgents")
    return TradingAgents(instance.id)


class TestCreateConfigMemorySettings:
    def test_defaults_propagate_to_config(self, ta_expert):
        config = ta_expert._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
        assert config["memory_injection_scope"] == "same_symbol"
        assert config["memory_max_trades"] == 2
        assert config["memory_lookback_days"] == 14
        assert config["analysis_strategy_notes"] == ""

    def test_overrides_propagate_to_config(self, ta_expert, db_session):
        from ba2_trade_platform.core.models import ExpertSetting
        from ba2_trade_platform.core.db import add_instance
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_injection_scope", value_json="all_symbols"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_max_trades", value_json="5"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="memory_lookback_days", value_json="30"))
        add_instance(ExpertSetting(instance_id=ta_expert.instance.id, key="analysis_strategy_notes", value_json="Buy the dip on broken charts."))

        # settings cache is populated lazily on first access; force a reload
        ta_expert2 = TradingAgents(ta_expert.instance.id)
        config = ta_expert2._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
        assert config["memory_injection_scope"] == "all_symbols"
        assert config["memory_max_trades"] == 5
        assert config["memory_lookback_days"] == 30
        assert config["analysis_strategy_notes"] == "Buy the dip on broken charts."
```

> Note: check how other `ExpertSetting` rows are stored in this codebase (`value_str` vs `value_json` vs `value_float` columns — see `expertsetting` table schema dumped earlier: `id, instance_id, key, value_str, value_json, value_float`). Match whatever the `settings` property in `ExtendableSettingsInterface` actually reads for `str`/`int` types — read `ba2_trade_platform/core/interfaces/ExtendableSettingsInterface.py` `settings` property (around line 284+) before writing this test, and adjust the `ExpertSetting(...)` construction accordingly (e.g. it may need `value_str=` for str-typed settings and `value_json` storing a JSON-encoded int for int-typed settings). Follow whatever pattern existing tests in `tests/test_experts/` or `tests/test_live_expert_interface.py` use for setting per-instance settings.

**Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_experts/test_tradingagents_settings.py -v -k Config`
Expected: FAIL with `KeyError: 'memory_injection_scope'` on the config dict.

**Step 3: Write minimal implementation**

In `_create_tradingagents_config` (`ba2_trade_platform/modules/experts/TradingAgents.py`), extend the `config.update({...})` block (currently ends around line 328) to add:

```python
            'memory_injection_scope': self.settings.get('memory_injection_scope') or settings_def['memory_injection_scope']['default'],
            'memory_max_trades': int(self.settings.get('memory_max_trades') or settings_def['memory_max_trades']['default']),
            'memory_lookback_days': int(self.settings.get('memory_lookback_days') or settings_def['memory_lookback_days']['default']),
            'analysis_strategy_notes': self.settings.get('analysis_strategy_notes') or settings_def['analysis_strategy_notes']['default'],
```

**Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_experts/test_tradingagents_settings.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add ba2_trade_platform/modules/experts/TradingAgents.py tests/test_experts/test_tradingagents_settings.py
git commit -m "feat(tradingagents): propagate memory scope and strategy notes into TA config"
```

---

### Task 3: Make `FinancialSituationMemory` config-driven (scope, max trades, lookback days)

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/memory.py`
- Test: `tests/test_tradingagents_memory.py` (new)

This is the core fix. Read the current file fully first (`memory.py`, ~349 lines) — it's already SQL-backed (no embeddings), so this is a logic change, not a rewrite.

**Step 1: Write the failing tests**

Create `tests/test_tradingagents_memory.py`:

```python
"""Tests for FinancialSituationMemory's configurable memory injection scope."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.memory import (
    FinancialSituationMemory,
)
from ba2_trade_platform.core.types import (
    OrderRecommendation, MarketAnalysisStatus, AnalysisUseCase,
    OrderDirection, TransactionStatus, OrderStatus, OrderType,
)
from tests.factories import (
    create_account_definition, create_expert_instance, create_market_analysis,
    create_recommendation, create_transaction, create_trading_order,
)


@pytest.fixture
def expert_instance(db_session):
    account = create_account_definition()
    return create_expert_instance(account.id, expert="TradingAgents")


def _make_past_analysis(expert_instance_id, symbol, days_ago, action=OrderRecommendation.SELL):
    ma = create_market_analysis(
        symbol=symbol,
        expert_instance_id=expert_instance_id,
        status=MarketAnalysisStatus.COMPLETED,
        subtype=AnalysisUseCase.ENTER_MARKET,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    create_recommendation(
        instance_id=expert_instance_id,
        market_analysis_id=ma.id,
        symbol=symbol,
        recommended_action=action,
    )
    return ma


class TestScopeNone:
    def test_none_scope_returns_empty(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "none", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        assert mem.get_memories("any situation") == []


class TestScopeSameSymbol:
    def test_returns_only_same_symbol_within_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=20)  # outside 14-day window
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        assert len(memories) == 1
        assert "AAPL" not in "" or True  # block content doesn't echo symbol; just check count

    def test_respects_max_trades_limit(self, expert_instance):
        for i in range(5):
            _make_past_analysis(expert_instance.id, "AAPL", days_ago=i + 1)
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        assert len(memories) == 2

    def test_does_not_include_cross_ticker_summary(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        # Closed losing trade on a DIFFERENT symbol, recent
        txn = create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "same_symbol", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" not in combined


class TestScopeAllSymbols:
    def test_includes_cross_ticker_summary_within_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        txn = create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        txn.close_date = datetime.now(timezone.utc) - timedelta(days=2)
        from ba2_trade_platform.core.db import update_instance
        update_instance(txn)

        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "all_symbols", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" in combined

    def test_excludes_cross_ticker_outside_lookback(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        txn = create_transaction(
            symbol="TOST", side=OrderDirection.BUY, status=TransactionStatus.CLOSED,
            open_price=100.0, close_price=80.0, quantity=10,
            expert_id=expert_instance.id,
        )
        txn.close_date = datetime.now(timezone.utc) - timedelta(days=20)  # outside 14-day window
        from ba2_trade_platform.core.db import update_instance
        update_instance(txn)

        mem = FinancialSituationMemory(
            "bull_memory",
            {"memory_injection_scope": "all_symbols", "memory_max_trades": 2, "memory_lookback_days": 14},
            symbol="AAPL",
            market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        memories = mem.get_memories("any situation")
        combined = "\n".join(m["recommendation"] for m in memories)
        assert "TOST" not in combined


class TestDefaults:
    def test_missing_config_keys_use_defaults(self, expert_instance):
        _make_past_analysis(expert_instance.id, "AAPL", days_ago=1)
        mem = FinancialSituationMemory(
            "bull_memory", {}, symbol="AAPL", market_analysis_id=None,
            expert_instance_id=expert_instance.id,
        )
        assert mem.scope == "same_symbol"
        assert mem.max_trades == 2
        assert mem.lookback_days == 14
```

Before finalizing this test file, double-check:
- `create_transaction` in `tests/factories.py` accepts `close_price`/`close_date` via `**kwargs` (it does — `Transaction(**kwargs)` passthrough), but `close_date` isn't in the explicit signature, so pass it via kwargs or set+`update_instance` after creation as shown above.
- `Transaction.open_price.is_not(None)` and `quantity > 0` filters in `_fetch_cross_ticker_summary` — make sure the factory's `open_price=100.0` and `quantity=10` satisfy these.

**Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_memory.py -v`
Expected: FAIL — `FinancialSituationMemory` currently ignores `config`, so `mem.scope`/`mem.max_trades`/`mem.lookback_days` don't exist (AttributeError), and same-symbol fetch has no lookback cutoff or scope gating.

**Step 3: Write minimal implementation**

In `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/memory.py`:

1. Update `__init__` (around line 38-49):

```python
    def __init__(
        self,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        expert_instance_id: Optional[int] = None,
    ):
        self.name = name  # e.g. "bull_memory", "trader_memory"
        self.symbol = symbol.upper() if symbol else None
        self.market_analysis_id = market_analysis_id
        self.expert_instance_id = expert_instance_id

        config = config or {}
        self.scope = config.get("memory_injection_scope", "same_symbol")
        self.max_trades = config.get("memory_max_trades", 2)
        self.lookback_days = config.get("memory_lookback_days", 14)
```

2. Update `get_memories` (around line 95-140) to gate on scope:

```python
    def get_memories(self, current_situation, n_matches=2, aggregate_chunks=False):
        if self.scope == "none":
            return []
        if self.expert_instance_id is None or self.symbol is None:
            return []

        try:
            past_blocks = self._fetch_same_symbol_blocks(self.max_trades)
            cross_block = self._fetch_cross_ticker_summary() if self.scope == "all_symbols" else ""
        except Exception as e:
            logger.warning(
                f"FinancialSituationMemory.get_memories failed for "
                f"expert={self.expert_instance_id} symbol={self.symbol}: {e}",
                exc_info=True,
            )
            return []

        if not past_blocks and not cross_block:
            return []

        if not past_blocks:
            return [{"recommendation": cross_block, "matched_situation": "", "similarity_score": 1.0}]

        if cross_block:
            past_blocks[0] = past_blocks[0] + "\n\n" + cross_block

        return [
            {"recommendation": block, "matched_situation": "", "similarity_score": 1.0}
            for block in past_blocks
        ]
```

(Remove the old docstring's "Args: n_matches / aggregate_chunks" wording or update it to note they're retained only for call-site compatibility and superseded by `self.max_trades`/`self.scope`.)

3. Update `_fetch_same_symbol_blocks` (around line 145-187) to add a lookback cutoff:

```python
    def _fetch_same_symbol_blocks(self, n_matches: int) -> List[str]:
        """Format the most recent N completed analyses for this expert+symbol,
        limited to the configured lookback window."""
        from sqlmodel import select, Session
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import (
            MarketAnalysis,
            ExpertRecommendation,
            Transaction,
        )
        from ba2_trade_platform.core.types import MarketAnalysisStatus

        blocks: List[str] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        with Session(get_db().bind) as session:
            stmt = (
                select(MarketAnalysis)
                .where(MarketAnalysis.expert_instance_id == self.expert_instance_id)
                .where(MarketAnalysis.symbol == self.symbol)
                .where(MarketAnalysis.status == MarketAnalysisStatus.COMPLETED)
                .where(MarketAnalysis.created_at >= cutoff)
            )
            if self.market_analysis_id is not None:
                stmt = stmt.where(MarketAnalysis.id != self.market_analysis_id)
            stmt = stmt.order_by(MarketAnalysis.created_at.desc()).limit(n_matches)

            past_mas = list(session.exec(stmt).all())

            for ma in past_mas:
                rec = session.exec(
                    select(ExpertRecommendation)
                    .where(ExpertRecommendation.market_analysis_id == ma.id)
                    .order_by(ExpertRecommendation.created_at.desc())
                    .limit(1)
                ).first()

                outcome = None
                if rec is not None:
                    outcome = self._lookup_realized_outcome(session, rec.id)

                blocks.append(self._format_past_analysis(ma, rec, outcome))

        return blocks
```

(Only the added `.where(MarketAnalysis.created_at >= cutoff)` line and the `cutoff = ...` line are new.)

4. Update `_fetch_cross_ticker_summary` (around line 189-236) to use `self.lookback_days` instead of the hardcoded 30:

```python
    def _fetch_cross_ticker_summary(self) -> str:
        """Recent closed trades from the same expert across other tickers,
        within the configured lookback window."""
        from sqlmodel import select, Session
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import Transaction
        from ba2_trade_platform.core.types import TransactionStatus, OrderDirection

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        with Session(get_db().bind) as session:
            stmt = (
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.symbol != self.symbol)
                .where(Transaction.status == TransactionStatus.CLOSED)
                .where(Transaction.close_date >= cutoff)
                .where(Transaction.open_price.is_not(None))
                .where(Transaction.close_price.is_not(None))
                .where(Transaction.quantity > 0)
                .order_by(Transaction.close_date.desc())
                .limit(20)
            )
            txns = list(session.exec(stmt).all())
        # ... rest unchanged (wins/losses formatting)
```

Update the docstring at the top of `_fetch_cross_ticker_summary` and the line `lines = ["=== Recent cross-ticker outcomes (this expert, last 30 days) ==="]` to use `self.lookback_days` in the label, e.g. `f"=== Recent cross-ticker outcomes (this expert, last {self.lookback_days} days) ==="`.

**Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_memory.py -v`
Expected: PASS (all tests)

Then run the full test suite to check for regressions:

Run: `.venv\Scripts\python.exe -m pytest -x`
Expected: PASS

**Step 5: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/memory.py tests/test_tradingagents_memory.py
git commit -m "fix(tradingagents): scope memory/reflection injection by symbol and lookback window"
```

---

### Task 4: Add `strategy_notes` placeholder + helper to `prompts.py`

> **Scope decision:** `strategy_notes` is injected only into the **synthesis + execution**
> agents — Research Manager (investment judge), Trader, and Risk Manager (final judge).
> The Bull/Bear Researcher debate prompts are intentionally left unchanged so the debate
> stays an independent check rather than a rubber stamp; the synthesis/execution layer is
> where "broken chart = expected entry, not new bad news" framing should be applied.

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py`
- Test: `tests/test_tradingagents_prompts.py` (new)

**Step 1: Write the failing tests**

Create `tests/test_tradingagents_prompts.py`:

```python
"""Tests for strategy-notes injection into TradingAgents synthesis/execution prompts."""
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
    format_research_manager_prompt,
    format_risk_manager_prompt,
    format_trader_system_prompt,
)


class TestStrategyNotesInjection:
    def test_research_manager_prompt_includes_notes(self):
        result = format_research_manager_prompt(
            strategy_notes="BUY THE DIP STRATEGY", past_memory_str="PM", history="H",
        )
        assert "BUY THE DIP STRATEGY" in result

    def test_research_manager_prompt_omits_notes_section_when_empty(self):
        result = format_research_manager_prompt(strategy_notes="", past_memory_str="PM", history="H")
        assert "Strategy Context" not in result

    def test_risk_manager_prompt_includes_notes(self):
        result = format_risk_manager_prompt(
            strategy_notes="BUY THE DIP STRATEGY", trader_plan="PLAN", past_memory_str="PM", history="H",
        )
        assert "BUY THE DIP STRATEGY" in result

    def test_trader_system_prompt_includes_notes(self):
        result = format_trader_system_prompt(past_memory_str="PM", strategy_notes="BUY THE DIP STRATEGY")
        assert "BUY THE DIP STRATEGY" in result

    def test_trader_system_prompt_defaults_to_no_notes(self):
        result = format_trader_system_prompt(past_memory_str="PM")
        assert "Strategy Context" not in result
```

**Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_prompts.py -v`
Expected: FAIL — `format_research_manager_prompt(strategy_notes=...)` and `format_risk_manager_prompt(strategy_notes=...)` raise `KeyError`/`TypeError` because `{strategy_notes}` isn't a placeholder yet, and `format_trader_system_prompt` doesn't accept `strategy_notes`.

**Step 3: Write minimal implementation**

In `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py`:

1. Add a small helper near the top of the "MANAGER PROMPTS" section (around line 118):

```python
def _format_strategy_notes_block(strategy_notes: str) -> str:
    """Render the optional user-provided strategy context block.

    Returns an empty string when no notes are configured, so the
    `{strategy_notes}` placeholder collapses to nothing in the prompt.
    """
    if not strategy_notes:
        return ""
    return f"\n**Strategy Context (from user — read carefully, it explains why these candidates look the way they do):**\n{strategy_notes}\n"
```

2. Add `{strategy_notes}` placeholders to the templates, right after the intro paragraph of each:

- `RESEARCH_MANAGER_PROMPT` (line ~168): after the first paragraph.
- `RISK_MANAGER_PROMPT` (line ~187): after the first paragraph, before "**Rating Scale**".
- `TRADER_SYSTEM_PROMPT` (line ~224): after the first paragraph, before "**Rating Scale:**".

Do NOT modify `BULL_RESEARCHER_PROMPT` or `BEAR_RESEARCHER_PROMPT`.

3. Update `format_research_manager_prompt` and `format_risk_manager_prompt` to render the block via the helper instead of passing the raw string through `.format()`:

```python
def format_research_manager_prompt(**kwargs) -> str:
    """Format research manager prompt with provided variables"""
    kwargs = dict(kwargs)
    kwargs["strategy_notes"] = _format_strategy_notes_block(kwargs.get("strategy_notes", ""))
    result = RESEARCH_MANAGER_PROMPT.format(**kwargs)
    logger.debug(f"\n------------------\nRESEARCH MANAGER PROMPT\n-----------------------\n{result}")
    return result
```

Apply the same `kwargs["strategy_notes"] = _format_strategy_notes_block(...)` pattern to `format_risk_manager_prompt`.

4. `format_trader_system_prompt` has an explicit signature — update it:

```python
def format_trader_system_prompt(past_memory_str: str, strategy_notes: str = "") -> str:
    """Format trader system prompt"""
    result = TRADER_SYSTEM_PROMPT.format(
        past_memory_str=past_memory_str,
        strategy_notes=_format_strategy_notes_block(strategy_notes),
    )
    logger.debug(f"\n------------------\nTRADER SYSTEM PROMPT\n-----------------------\n{result}")
    return result
```

**Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_prompts.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py tests/test_tradingagents_prompts.py
git commit -m "feat(tradingagents): support optional strategy-context block in research-manager/trader/risk-manager prompts"
```

---

### Task 5: Plumb `strategy_notes` from config through node creators and graph setup

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/managers/research_manager.py`
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/managers/risk_manager.py`
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/trader/trader.py`
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/setup.py`
- Test: `tests/test_tradingagents_prompts.py` (extend)

This task has no DB/LLM dependency — it's pure wiring. Keep each `create_*` function signature backward compatible by giving `strategy_notes` a default of `""`. `bull_researcher.py` and `bear_researcher.py` are NOT touched (per the Task 4 scope decision).

**Step 1: Write the failing test**

Add to `tests/test_tradingagents_prompts.py`:

```python
class TestNodeCreatorsAcceptStrategyNotes:
    def test_create_research_manager_accepts_strategy_notes(self):
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.managers.research_manager import create_research_manager
        node = create_research_manager(llm=None, memory=None, strategy_notes="NOTES")
        assert callable(node)

    def test_create_risk_manager_accepts_strategy_notes(self):
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.managers.risk_manager import create_risk_manager
        node = create_risk_manager(llm=None, memory=None, strategy_notes="NOTES")
        assert callable(node)

    def test_create_trader_accepts_strategy_notes(self):
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.trader.trader import create_trader
        node = create_trader(llm=None, memory=None, strategy_notes="NOTES")
        assert callable(node)
```

**Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_prompts.py -v -k StrategyNotes`
Expected: FAIL with `TypeError: create_research_manager() got an unexpected keyword argument 'strategy_notes'` (and similarly for the other 2).

**Step 3: Write minimal implementation**

For each of the 3 files, change the `create_*` signature to accept `strategy_notes: str = ""` and pass it into the corresponding `format_*_prompt(...)` call:

`research_manager.py`:
```python
def create_research_manager(llm, memory, strategy_notes: str = ""):
    def research_manager_node(state) -> dict:
        ...
        prompt = format_research_manager_prompt(
            past_memory_str=past_memory_str,
            history=history,
            strategy_notes=strategy_notes,
        )
        ...
    return research_manager_node
```

`risk_manager.py`:
```python
def create_risk_manager(llm, memory, strategy_notes: str = ""):
    def risk_manager_node(state) -> dict:
        ...
        prompt = format_risk_manager_prompt(
            trader_plan=trader_plan,
            past_memory_str=past_memory_str,
            history=history,
            strategy_notes=strategy_notes,
        )
        ...
    return risk_manager_node
```

`trader.py`:
```python
def create_trader(llm, memory, strategy_notes: str = ""):
    def trader_node(state, name):
        ...
        messages = [
            {
                "role": "system",
                "content": format_trader_system_prompt(past_memory_str=past_memory_str, strategy_notes=strategy_notes),
            },
            ...
        ]
        ...
    return functools.partial(trader_node, name="Trader")
```

Then in `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/setup.py`, inside `setup_graph` (around line 75, alongside the existing `parallel_tool_calls = self.config.get(...)` line), add:

```python
        strategy_notes = self.config.get("analysis_strategy_notes", "")
```

And update 3 of the 5 node-creation calls (around lines 134-153) — leave `bull_researcher_node`/`bear_researcher_node` unchanged:

```python
        bull_researcher_node = create_bull_researcher(
            self.quick_thinking_llm, self.bull_memory
        )
        bear_researcher_node = create_bear_researcher(
            self.quick_thinking_llm, self.bear_memory
        )
        research_manager_node = create_research_manager(
            self.deep_thinking_llm, self.invest_judge_memory, strategy_notes
        )
        trader_node = create_trader(self.quick_thinking_llm, self.trader_memory, strategy_notes)

        # Create risk analysis nodes
        risky_analyst = create_risky_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        safe_analyst = create_safe_debator(self.quick_thinking_llm)
        risk_manager_node = create_risk_manager(
            self.trade_recommendation_llm, self.risk_manager_memory, strategy_notes
        )
```

**Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_prompts.py -v`
Expected: PASS (8 tests total)

Then run the full suite:

Run: `.venv\Scripts\python.exe -m pytest -x`
Expected: PASS

**Step 5: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/managers/research_manager.py \
        ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/managers/risk_manager.py \
        ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/trader/trader.py \
        ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/setup.py
git commit -m "feat(tradingagents): thread strategy notes from config into research-manager/trader/risk-manager nodes"
```

---

### Task 6: Bump version, configure prod GPT5 instance (manual, no automated test)

**Files:**
- Modify: `ba2_trade_platform/version.py` (per CLAUDE.md: bump build number before push)
- Manual: prod DB settings for expert instance 5 ("TA-Dynamic-GPT5") via the Settings UI (`http://localhost:8081/settings`) — do **not** hand-edit prod `db.sqlite` directly.

**Step 1: Bump version**

Open `ba2_trade_platform/version.py`, increment the build number (NNNNN) by 1, updating year/month if changed. Confirm current value first:

Run: `.venv\Scripts\python.exe -c "from ba2_trade_platform.version import APP_VERSION; print(APP_VERSION)"`

**Step 2: Set `analysis_strategy_notes` for prod instance 5**

After deploying (commit + push from dev, `git pull` in prod per the dev/prod workflow — confirm with the user before doing this), go to the GPT5 instance's settings page and set **Trading Strategy Notes for AI Analysts** to something like:

```
This account intentionally screens for stocks down >=15% over the past 7 days
(screener_sort_metric=price_drop_pct), i.e. mean-reversion / "buy the dip"
candidates sorted by largest recent drop. A "technically broken chart" (price
below the 10/50 EMA/SMA, Bollinger midpoint, etc.) is therefore the EXPECTED,
BY-DESIGN entry condition for every candidate you'll see here -- do not treat
it, by itself, as new bearish information or as the deciding factor.

Focus the debate on whether FUNDAMENTALS, the catalyst/news backdrop, and
valuation justify a rebound (BUY/OVERWEIGHT) vs. continued decline
(SELL/UNDERWEIGHT) over the chosen horizon. A name with strongly improving
fundamentals (e.g. accelerating revenue, swing to profitability, EPS beats)
should not be defaulted to SELL purely because the chart hasn't "confirmed" yet
-- that lack of confirmation is the opportunity, not the risk.

Note on past losses: prior realized losses on this account were primarily
caused by stop-losses placed too tight on otherwise-correct mean-reversion
entries, not by the underlying BUY thesis being wrong. When reviewing past
trade outcomes, weigh "stopped out too early on a thesis that later played out"
differently from "thesis was simply wrong".
```

Leave **Memory/Reflection Injection Scope**, **Max Past Trades Injected per Symbol**, and **Memory Lookback Window (days)** at their defaults (`same_symbol` / `2` / `14`) — these now match the defaults proposed in this conversation, so no override is needed for instance 5 specifically. Only override them if the user wants instance-specific values different from the new global defaults.

**Step 3: Commit version bump**

```bash
git add ba2_trade_platform/version.py
git commit -m "chore: bump version"
```

(Settings changes for instance 5 are made via the running app's UI against the prod DB and are NOT part of this commit.)

---

### Task 7: Show "Data provided to this analyst" prompt expander on the Market Analysis tab

**Background:** `fundamentals_analyst.py`, `news_analyst.py`, `social_media_analyst.py`, and `macro_analyst.py` all return an `*_input` field (`f"{prompt_config['system']}\n\n===== DATA PROVIDED TO ANALYST =====\n\n{human}"`) that `TradingAgentsUI._render_input_expander()` displays as a "🔎 Data provided to this analyst (analyst prompt)" expander. `market_analyst.py` is the only analyst that doesn't return this, so its tab is missing the expander, even though the system prompt is fully known up front (the market analyst is agentic/tool-using, so there's no fixed "human" data message — but the system prompt alone is still useful to show, and tool call inputs/outputs are already visible in the "Tool Outputs" tab).

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py`
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_states.py:83-86`
- Modify: `ba2_trade_platform/modules/experts/TradingAgentsUI.py:159-161` and `:230-232`
- Test: `tests/test_tradingagents_market_analyst.py` (new), `tests/test_tradingagents_ui.py` (new)

**Step 1: Write the failing tests**

Create `tests/test_tradingagents_market_analyst.py`:

```python
"""Tests that the Market Analyst exposes its system prompt via market_input,
matching the pattern used by the other analysts (macro/news/social/fundamentals)."""
from unittest.mock import MagicMock, patch


def test_market_analyst_returns_market_input():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.market_analyst import (
        create_market_analyst,
    )

    llm = MagicMock()
    result_mock = MagicMock()
    result_mock.tool_calls = []
    result_mock.content = "Market report text"

    with patch(
        "ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.market_analyst.ChatPromptTemplate"
    ) as mock_template_cls:
        prompt_mock = MagicMock()
        mock_template_cls.from_messages.return_value = prompt_mock
        chain_mock = prompt_mock.__or__.return_value
        chain_mock.invoke.return_value = result_mock

        node = create_market_analyst(llm, toolkit=None, tools=[])
        state = {"trade_date": "2026-06-10", "company_of_interest": "AAPL", "messages": []}
        out = node(state)

    assert "market_input" in out
    assert out["market_input"].strip()
    assert "trading assistant" in out["market_input"].lower()  # from MARKET_ANALYST_SYSTEM_PROMPT
```

Create `tests/test_tradingagents_ui.py`:

```python
"""Tests for the TradingAgentsUI 'Data provided to this analyst' expander."""
from unittest.mock import MagicMock, patch

from ba2_trade_platform.modules.experts.TradingAgentsUI import TradingAgentsUI
from ba2_trade_platform.core.types import MarketAnalysisStatus


def _make_market_analysis(status):
    ma = MagicMock()
    ma.status = status
    ma.state = {"trading_agent_graph": {"market_report": "report", "market_input": "system prompt + data"}}
    return ma


def _patch_ui(ui_obj):
    return [
        patch("ba2_trade_platform.modules.experts.TradingAgentsUI.ui", MagicMock()),
        patch.object(ui_obj, "_render_content_panel"),
        patch.object(ui_obj, "_render_input_expander"),
        patch.object(ui_obj, "_render_data_visualization_panel"),
        patch.object(ui_obj, "_render_tool_outputs_panel"),
        patch.object(ui_obj, "_render_summary_panel"),
        patch.object(ui_obj, "_render_in_progress_summary"),
        patch.object(ui_obj, "_render_debate_panel"),
        patch.object(ui_obj, "_render_expert_recommendation"),
    ]


class TestMarketAnalysisPromptExpander:
    def test_completed_ui_shows_market_input_expander(self):
        ui_obj = TradingAgentsUI(_make_market_analysis(MarketAnalysisStatus.COMPLETED))
        patches = _patch_ui(ui_obj)
        with patches[0], patches[1], patches[2] as mock_expander, *patches[3:]:
            ui_obj._render_completed_ui()
        mock_expander.assert_any_call("market_input")

    def test_in_progress_ui_shows_market_input_expander(self):
        ui_obj = TradingAgentsUI(_make_market_analysis(MarketAnalysisStatus.RUNNING))
        patches = _patch_ui(ui_obj)
        with patches[0], patches[1], patches[2] as mock_expander, *patches[3:]:
            ui_obj._render_in_progress_ui()
        mock_expander.assert_any_call("market_input")
```

> Note: Python's `with a, b, *rest:` syntax for a dynamic list of context managers is invalid — use `contextlib.ExitStack()` instead. Rewrite the `with` blocks as:
> ```python
> from contextlib import ExitStack
> with ExitStack() as stack:
>     for p in patches:
>         cm = stack.enter_context(p)
>     mock_expander = stack.enter_context(patches[2])  # capture the right one
> ```
> Simplify to whatever's cleanest — the goal is just: patch `ui` + all sub-render methods to no-ops, capture `_render_input_expander` as a `Mock`, call the render method, assert it was called with `"market_input"`.

**Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_market_analyst.py tests/test_tradingagents_ui.py -v`
Expected: FAIL — `out["market_input"]` raises `KeyError` (market_analyst.py doesn't return it yet); `mock_expander.assert_any_call("market_input")` fails (not called).

**Step 3: Write minimal implementation**

1. In `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_states.py`, add alongside lines 83-86:

```python
    market_input: Annotated[str, "Input data/prompt given to the Market Analyst"]
```

2. In `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py`, return the system prompt as `market_input`:

```python
        return {
            "messages": [result],
            "market_report": report,
            "market_input": prompt_config["system"],
        }
```

3. In `ba2_trade_platform/modules/experts/TradingAgentsUI.py`, add the expander call after both `market_report` content panels:

Line ~159-161 (`_render_completed_ui`):
```python
            with ui.tab_panel(market_tab):
                self._render_content_panel('market_report', '📈 Market Analysis', 
                                         'Technical analysis and market indicators')
                self._render_input_expander('market_input')
```

Line ~230-232 (`_render_in_progress_ui`):
```python
            with ui.tab_panel(market_tab):
                self._render_content_panel('market_report', '📈 Market Analysis', 
                                         'Technical analysis and market indicators')
                self._render_input_expander('market_input')
```

**Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_tradingagents_market_analyst.py tests/test_tradingagents_ui.py -v`
Expected: PASS (3 tests)

Then run the full suite:

Run: `.venv\Scripts\python.exe -m pytest -x`
Expected: PASS

**Step 5: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py \
        ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_states.py \
        ba2_trade_platform/modules/experts/TradingAgentsUI.py \
        tests/test_tradingagents_market_analyst.py tests/test_tradingagents_ui.py
git commit -m "fix(tradingagents-ui): show analyst prompt expander on Market Analysis tab"
```

---

## Other recommendations surfaced during investigation (not part of this plan's code changes — discuss with user before acting)

1. **Smart Risk Manager alignment**: instance 5's `smart_risk_manager_user_instructions` is currently unset (falls back to the interface default), and that default already contains mean-reversion guidance ("for counter-trend / mean-reversion entries ... prefer widening SL to ~8%"). Once `analysis_strategy_notes` explains the buy-the-dip framing to the entry-analysis agents, consider also customizing `smart_risk_manager_user_instructions` for instance 5 to explicitly reference the same "this account trades broken-chart mean-reversion setups, expect choppy continuation before reversal" framing, so position management stays consistent with entry intent.

2. **Re-running with the new defaults is the cleanest "memory reset"**: rather than building logic to retroactively distinguish "stopped out too early" vs "thesis wrong" in `_fetch_cross_ticker_summary`, the `memory_lookback_days=14` default naturally ages out the SYM/FLS/TOST-era reflections once they're >14 days old. No data migration needed.

3. **Consider a follow-up (separate task, not now)**: `_fetch_cross_ticker_summary` currently has no way to distinguish "closed via stop-loss" from "closed via take-profit/manual exit" in the wins/losses summary it shows the LLM (it has `close_reason` available in `_lookup_realized_outcome` but doesn't surface it in the cross-ticker block). Surfacing `close_reason` per trade (e.g. `TOST -20.0% (BUY, stopped_out)`) would let the LLM itself differentiate "thesis wrong" vs "stopped out too early" without needing scope/lookback workarounds. Flagged for later — out of scope here.

---

## Final verification

Run: `.venv\Scripts\python.exe -m pytest -x`
Expected: full suite PASS, including the new `tests/test_tradingagents_memory.py`, `tests/test_tradingagents_prompts.py`, `tests/test_experts/test_tradingagents_settings.py`, `tests/test_tradingagents_market_analyst.py`, and `tests/test_tradingagents_ui.py`.
