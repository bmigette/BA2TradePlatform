# Senate Expert Basket Dispatch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `FMPSenateTraderWeight` (and fix the already-half-built but broken
`FMPSenateTraderCopy`) call its analysis **once per scheduled bar** instead of once per
symbol in a static universe — internally scanning all congressional trades in the lookback
window, filtering by configured criteria plus a new "tradable stock only" filter, and
emitting **one scored recommendation per qualifying symbol** that still flows through the
full classic pipeline (`TradeActionEvaluator`, `TradeRiskManagement` sizing, one
`ExpertRecommendation` row per symbol) — in both backtest and live.

**Architecture:** Add a new "basket expert" dispatch mode to the daily backtest engine,
parallel to (but architecturally distinct from) the existing `bypasses_classic_rm` mode:
a basket expert's `analyze_as_of` is called **once per bar** and returns
`List[Recommendation]` (one per qualifying symbol) instead of being called once per symbol
and returning a single `Recommendation`. Each item in that list is then fed through the
**exact same per-symbol machinery** `_run_expert_bar` already uses today (persist
`ExpertRecommendation`, run `TradeActionEvaluator`, batch-size via `TradeRiskManagement`) —
nothing about position sizing or ruleset evaluation changes, only *how many symbols worth
of `Recommendation` come out of one `analyze_as_of` call*. Live gets the parallel change via
the **already-existing and already-proven** `should_expand_instrument_jobs=False` +
`"EXPERT"`-symbol `JobManager` dispatch (currently used only by `FMPSenateTraderCopy`) —
`FMPSenateTraderWeight` opts into the same mechanism.

**Tech Stack:** Python, SQLModel, the `ba2_common`/`ba2_experts`/`ba2_providers` shared
packages, `testplatform/backend` FastAPI backtest engine, pytest.

---

## Context you need before starting

Read these fully before touching code — this plan assumes you already understand them:

1. `packages/experts/ba2_experts/FMPSenateTraderWeight.py` — read `_gather` (line ~294-450),
   `_process` (search `def _process`), `analyze_as_of` (line ~552-560), and
   `_calculate_recommendation` (line ~1599 onward). This is the symbol-scoped expert you are
   converting to basket mode.
2. `packages/experts/ba2_experts/FMPSenateTraderCopy.py` — this is your **template**. Read
   `get_expert_properties` (line ~62-72), `run_analysis`/`_run_enter_market_analysis`
   (line ~854-1013), and `analyze_as_of` (line ~225-232). It already does almost everything
   this plan asks for `FMPSenateTraderWeight` to do — for BOTH live and (attempted) backtest.
3. `packages/experts/ba2_experts/expert_mixins.py` — `FMPCongressTradingMixin._fetch_congress_trades`
   (line ~126-190+). **Critical fact discovered this session:** this method ALREADY supports
   `symbol=None` → fetches the unscoped `{chamber}-latest` paginated feed (all disclosures,
   not one symbol). `FMPSenateTraderWeight` currently never calls it with `symbol=None`; Copy
   already does. You do not need to write a new fetch path — reuse this one.
4. `testplatform/backend/app/services/backtest/daily_engine.py` — read `_run_expert_bar`
   (line ~772-onward, the classic per-symbol loop), `_run_bypass_expert_bar` (line ~1044-1097,
   the FactorRanker-only target-weight path — **do not copy this pattern**, it skips
   `TradeActionEvaluator`/`TradeRiskManagement` entirely, which basket Senate must NOT do),
   and the dispatch branch at line ~612 (`if getattr(expert, "bypasses_classic_rm", False):`).
5. `testplatform/backend/app/services/backtest/daily_engine.py:263`
   `_recommendation_to_expert_recommendation` — the function that turns one `Recommendation`
   into one `ExpertRecommendation` row. You will call this once per item in the basket list
   instead of once per symbol-loop-iteration.
6. `ba2_trade_platform/core/JobManager.py` lines ~316-325, ~771-777, ~936-945, ~1115-1131 —
   the live `"EXPERT"`-symbol dispatch. Also read `docs/EXPERT_SYMBOL_EXECUTION_GUIDE.md` in
   full (short doc, already explains the two-phase Job Creation / Job Execution model).
7. `tools/build_senate_universe.py` lines ~51-64 — `_FUND_TICKER_RE` /
   `_is_junk_ticker(sym)`. This is the existing "drop non-equity ticker" heuristic (regex:
   4-5 uppercase letters ending in `X` = mutual fund). Reuse/extract this for the new
   "tradable stock only" filter rather than inventing a new classification.
8. `testplatform/backend/tests/backtest/test_daily_engine_bypass.py` — full file. This is
   your template for testing the new basket-dispatch engine path (stub expert +
   `DailyBacktestEngine`, no network).
9. `packages/experts/tests/test_senate_gather_process.py` — the 39-test suite covering
   `FMPSenateTraderWeight._gather`/`_process` today. **Must stay green throughout** — the
   per-symbol single-`Recommendation` code path is NOT being deleted (still needed as a
   fallback / for any caller that still passes a real symbol), only a new basket path is
   being added alongside it.

## Critical finding this plan must verify and fix

`FMPSenateTraderCopy.analyze_as_of` (line ~225) already returns `List[Recommendation]`, with
a docstring claiming parity with live. But `daily_engine.py:_run_expert_bar` has **no
handling for a list return** — it calls `rec = expert.analyze_as_of(as_of, ctx)` inside a
`for symbol in universe:` loop and immediately does `_recommendation_to_expert_recommendation(rec, ...)`,
which does `getattr(rec, "skip", False)` then `rec.signal`. Fed a `list`, `rec.signal` raises
`AttributeError`, silently caught by the per-symbol `except Exception` at line ~815, logged as
`"analyze_as_of failed for {symbol}"`. **Net effect: `FMPSenateTraderCopy` produces zero
recommendations in any testplatform backtest today.** Task 1 below confirms this with a real
run before any code changes, so you have a failing-test baseline; Task 3 fixes it as a
natural side effect of building the new engine dispatch mode.

---

## Task 1: Confirm FMPSenateTraderCopy's backtest breakage (root cause, no fix yet)

**Files:**
- Test: `testplatform/backend/tests/backtest/test_daily_engine_basket_dispatch.py` (new file)

**Step 1: Write a failing-by-design smoke test proving the breakage**

```python
"""Root-cause confirmation (Task 1 of the senate-basket-dispatch plan): FMPSenateTraderCopy's
analyze_as_of already returns List[Recommendation], but daily_engine.py has no code path that
expects a list -- it crashes with AttributeError inside the per-symbol try/except, which
swallows the error and produces zero recommendations. This test proves that today, BEFORE any
engine change, so Task 3's fix has a real before/after."""
from __future__ import annotations

from datetime import date, datetime

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


class _StubListReturningExpert(MarketExpertInterface):
    """Mimics FMPSenateTraderCopy's CURRENT (broken-in-backtest) shape: analyze_as_of
    returns a list, but carries none of the special markers this plan is about to add."""

    def __init__(self, id: int):
        super().__init__(id)
        self.call_count = 0

    @classmethod
    def description(cls) -> str:
        return "Stub list-returning expert (pre-fix shape)."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        self.call_count += 1
        return [
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
                details="stub basket rec",
            )
        ]


def test_list_returning_expert_produces_zero_recommendations_today():
    """Documents the CURRENT broken behavior. When Task 3 lands, an equivalent expert
    marked as a basket expert must produce a real ExpertRecommendation instead."""
    # Build the same minimal DailyBacktestEngine harness test_daily_engine_bypass.py uses
    # (bars, account, ExpertInstance/AccountDefinition seeded via backtest_db), substituting
    # _StubListReturningExpert for the bypass stub. Assert:
    #   - expert.call_count == number of universe symbols (it's STILL being called per-symbol,
    #     not once) -- proves the loop, not the expert, is the problem.
    #   - zero ExpertRecommendation rows exist in the run DB afterward.
    #   - the run log contains "analyze_as_of failed for" (the swallowed AttributeError).
    ...
```

Fill in the harness by copying `test_daily_engine_bypass.py`'s setup (imports, `_bar_rows`,
`BARS`/`START`/`END`, engine construction, `backtest_trading_db`/`seed_account_definition`/
`seed_expert_instance` calls) — use a 2+ symbol universe (e.g. `["AAPL", "MSFT"]`) so the
"called N times, not once" assertion is meaningful.

**Step 2: Run it**

```
./venv/bin/python -m pytest tests/backtest/test_daily_engine_basket_dispatch.py -v
```

Expected: PASS (this test documents current *broken* behavior, so a "pass" here proves the
bug exists exactly as described — do not skip this step even though it feels backwards).

**Step 3: Commit**

```bash
git add testplatform/backend/tests/backtest/test_daily_engine_basket_dispatch.py
git commit -m "test(backtest): document FMPSenateTraderCopy's list-return backtest breakage"
```

---

## Task 2: Engine support for basket-dispatch experts

**Files:**
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py`
- Test: `testplatform/backend/tests/backtest/test_daily_engine_basket_dispatch.py` (extend)

**Step 1: Write the failing test for the new dispatch path**

Add to the same test file: a `_StubBasketExpert` (copy `_StubBypassExpert`'s shape from
`test_daily_engine_bypass.py` but instead of `bypasses_classic_rm = True` and
`raw_outputs["targets"]`, set a new marker `analyzes_as_basket = True` and have
`analyze_as_of` return `List[Recommendation]`, one entry per symbol, each a normal
BUY/SELL/HOLD `Recommendation` with `current_price` and `raw_outputs["symbol"]` set (the
list items must self-identify their symbol — the engine no longer has a `for symbol in
universe` loop to pin it from).

```python
class _StubBasketExpert(MarketExpertInterface):
    """Basket expert: ONE analyze_as_of call per bar returns recommendations for
    MULTIPLE symbols. Each Recommendation carries its own symbol in raw_outputs['symbol']
    (the engine has no per-symbol loop to infer it from anymore)."""

    analyzes_as_basket = True

    def __init__(self, id: int):
        super().__init__(id)
        self.call_count = 0

    @classmethod
    def description(cls) -> str:
        return "Stub basket expert (one call, many symbols)."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        self.call_count += 1
        return [
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
                details="stub basket rec", raw_outputs={"symbol": "AAPL"},
            ),
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=60.0, current_price=50.0,
                details="stub basket rec", raw_outputs={"symbol": "MSFT"},
            ),
        ]


def test_basket_expert_analyzed_once_per_bar_not_per_symbol():
    # Same harness as Task 1's test, universe=["AAPL", "MSFT"], but using _StubBasketExpert.
    # Run the engine across N bars. Assert:
    #   - expert.call_count == N (once per bar, NOT N * len(universe))
    #   - TWO ExpertRecommendation rows exist per analysed bar (one per basket item)
    #   - each row's symbol matches raw_outputs['symbol'] from the corresponding list item
    ...


def test_basket_expert_still_uses_classic_rm_and_ruleset():
    # Same setup. Assert (unlike the bypass path):
    #   - TradeActionEvaluator IS constructed/invoked (monkeypatch or spy)
    #   - TradeRiskManagement sizing IS invoked (a funded TradingOrder appears, sized by the
    #     classic per-instrument-cap logic, not a target-weight rebalance)
    ...
```

**Step 2: Run to verify failure**

```
./venv/bin/python -m pytest tests/backtest/test_daily_engine_basket_dispatch.py -v
```

Expected: the two new tests FAIL (no `analyzes_as_basket` handling exists yet); the Task 1
test still passes (unchanged behavior so far).

**Step 3: Implement the engine dispatch change**

In `daily_engine.py`, add a new branch parallel to the `bypasses_classic_rm` check at line
~612 (inside the same `for expert, expert_id, settings, ruleset_id in self.experts:` loop
that currently reads):

```python
                if getattr(expert, "bypasses_classic_rm", False):
                    if entry_ok:
                        self._run_bypass_expert_bar(expert, expert_id, settings, as_of_dt)
                    continue
                if getattr(expert, "analyzes_as_basket", False):
                    if entry_ok:
                        self._run_basket_expert_bar(
                            expert, expert_id, settings, ruleset_id, as_of_dt
                        )
                    continue
                if entry_ok:
                    self._run_expert_bar(
                        expert, expert_id, settings, ruleset_id, entry_universe, as_of_dt
                    )
```

Add `_run_basket_expert_bar` as a near-duplicate of `_run_expert_bar` (line ~772), with
these differences:
- No `for symbol in universe:` loop and no `expert._gather_symbol = symbol` pinning.
- Call `expert.analyze_as_of(as_of, ctx)` **once** (build `ctx` once, same as
  `_run_bypass_expert_bar` does).
- The single call returns `recs: List[Recommendation]`. Loop over `recs`; for each `rec`,
  read its symbol from `rec.raw_outputs["symbol"]` (require it — raise/log+skip if absent,
  do not guess), then run the **exact same body** `_run_expert_bar`'s per-symbol loop runs
  today from `_recommendation_to_expert_recommendation(rec, ...)` onward (persist row,
  re-read via `get_instance`, `TradeActionEvaluator`, append to `equity_candidates`, and the
  same end-of-loop `_size_and_submit_candidates` call). The cleanest way to do this without
  duplicating ~60 lines: extract the per-recommendation body of `_run_expert_bar` (everything
  from `rec_id = _recommendation_to_expert_recommendation(...)` through the
  `equity_candidates.append(...)` line) into a small private helper
  `_stage_recommendation_candidate(self, rec, symbol, expert, expert_id, ...)` that both
  `_run_expert_bar` (per-symbol loop body) and `_run_basket_expert_bar` (per-list-item loop
  body) call. Match the exact same exception handling (`BacktestCacheMiss`/
  `FMPHistoryCacheMiss` re-raise, everything else logged+skipped) but note: since there is
  now only ONE `analyze_as_of` call for the whole bar (not one per symbol), a raised
  exception from `analyze_as_of` itself aborts the WHOLE bar for this expert (there's no
  "skip this one symbol, try the next" for the gather step anymore — only per-recommendation
  processing after the list comes back can skip individual items).

**Step 4: Run to verify tests pass**

```
./venv/bin/python -m pytest tests/backtest/test_daily_engine_basket_dispatch.py -v
```

Expected: all tests PASS, including Task 1's (still documents the OLD per-symbol path is
unaffected for experts that don't set `analyzes_as_basket`).

**Step 5: Run the full daily-engine regression suite**

```
./venv/bin/python -m pytest tests/backtest -q
```

Expected: no new failures (compare against the pre-existing 10 unrelated failures already
known from this session's earlier full-suite run — chronos/migration_022/news_batch/
optimization/trial_broker/worker_server — none of which touch this code).

**Step 6: Commit**

```bash
git add testplatform/backend/app/services/backtest/daily_engine.py testplatform/backend/tests/backtest/test_daily_engine_basket_dispatch.py
git commit -m "feat(backtest): add analyzes_as_basket engine dispatch (one call, many recommendations)"
```

---

## Task 3: Wire FMPSenateTraderCopy onto the new dispatch mode (fixes the dead backtest path)

**Files:**
- Modify: `packages/experts/ba2_experts/FMPSenateTraderCopy.py`
- Test: `packages/experts/tests/test_senate_copy_gather_process.py` if it exists (check
  first), else extend whatever test file already exercises `FMPSenateTraderCopy.analyze_as_of`
  (search `test_tools/test_fmp_senate_copy.py` — note this is under `test_tools/`, NOT
  pytest-collected per this repo's `tests/` vs `test_files/`/`test_tools/` convention in
  CLAUDE.md; if there's no real pytest test for Copy's `analyze_as_of`, add one to
  `packages/experts/tests/`).

**Step 1: Add the marker**

```python
class FMPSenateTraderCopy(AnalysisStatusRenderMixin, FMPCongressTradingMixin, MarketExpertInterface):
    ...
    analyzes_as_basket = True
```

**Step 2: Confirm `analyze_as_of`'s existing return shape matches what Task 2's engine expects**

Re-read `analyze_as_of` (line ~225-232) — it returns `self._process(bundle, merged, as_of)`
directly. Confirm `_process` already returns `List[Recommendation]` with each item's
`raw_outputs` containing a `"symbol"` key (Task 2's `_run_basket_expert_bar` requires this).
If `_process`'s returned `Recommendation` objects don't already set `raw_outputs["symbol"]`,
add it there (search where `_process` builds each `Recommendation` — likely near where
`_run_enter_market_analysis`, line ~934, calls `self._process(...)` — the LIVE path recovers
the symbol via `rec.raw_outputs["symbol"]` already at line ~954, so this key should already
exist; just confirm and cite the line, don't blindly re-add it).

**Step 3: Write a real backtest smoke test**

```python
def test_fmp_senate_trader_copy_produces_recommendations_in_backtest():
    """Was broken before Task 2/3 (zero recommendations, silently swallowed AttributeError).
    Now: one analyze_as_of call per bar, ExpertRecommendation rows appear for symbols with
    qualifying copy-trades."""
    # Reuse the Task 1/2 harness pattern, but construct a REAL FMPSenateTraderCopy instance
    # (not a stub) against fixture/hermetic trade data (check test_tools/test_fmp_senate_copy.py
    # for how it fabricates FMP trade fixtures without network -- likely monkeypatches
    # _fetch_congress_trades or seeds the disk cache the hermetic backtest reads from).
    ...
```

**Step 4: Run**

```
./venv/bin/python -m pytest packages/experts/tests/ -k senate_copy -v
```

Expected: PASS, with at least one `ExpertRecommendation` row produced (proving the
previously-dead path now works).

**Step 5: Commit**

```bash
git add packages/experts/ba2_experts/FMPSenateTraderCopy.py packages/experts/tests/
git commit -m "fix(senate-copy): wire analyzes_as_basket marker, fix dead backtest path"
```

---

## Task 4: Tradable-symbol filter (extract from build_senate_universe.py)

**Files:**
- Modify: `packages/common/ba2_common/core/utils.py` (shared home for pure helpers per
  CLAUDE.md's Phase 6 convention — this is a pure classification function, no DB/network)
- Modify: `tools/build_senate_universe.py` (switch to import the extracted function instead
  of defining its own copy)
- Test: `packages/common/tests/test_utils_pure.py` (existing file — extend)

**Step 1: Write the failing test**

```python
def test_is_tradable_stock_ticker():
    from ba2_common.core.utils import is_tradable_stock_ticker
    assert is_tradable_stock_ticker("AAPL") is True
    assert is_tradable_stock_ticker("MSFT") is True
    # Mutual-fund pattern: 4-5 uppercase letters ending in X
    assert is_tradable_stock_ticker("FTGCX") is False
    assert is_tradable_stock_ticker("VFIAX") is False
    assert is_tradable_stock_ticker("") is False
    assert is_tradable_stock_ticker(None) is False
```

**Step 2: Run to verify failure**

```
./venv/bin/python -m pytest packages/common/tests/test_utils_pure.py::test_is_tradable_stock_ticker -v
```

Expected: FAIL (`ImportError: cannot import name 'is_tradable_stock_ticker'`).

**Step 3: Extract the function**

Move `_FUND_TICKER_RE`/`_is_junk_ticker` logic from `tools/build_senate_universe.py`
(line ~51-64) into `packages/common/ba2_common/core/utils.py` as a public
`is_tradable_stock_ticker(sym: Optional[str]) -> bool` (inverted sense — the tool's
`_is_junk_ticker` returns True for junk; the new shared function should return True for
"keep it", matching how a filter predicate reads at call sites: `[s for s in symbols if
is_tradable_stock_ticker(s)]`). Keep the exact same regex
(`^[A-Z]{4,5}X$`) — this is a proven heuristic, don't "improve" it as part of this task.
Update `tools/build_senate_universe.py` to import and use it (via `not is_tradable_stock_ticker(sym)`
wherever `_is_junk_ticker(sym)` was called), deleting its local copy.

**Step 4: Run to verify pass**

```
./venv/bin/python -m pytest packages/common/tests/test_utils_pure.py::test_is_tradable_stock_ticker -v
./venv/bin/python -m pytest packages/common/tests -q
```

Expected: all PASS (including the pre-existing `build_senate_universe.py` behavior — if there's
an existing test for that script, re-run it too).

**Step 5: Commit**

```bash
git add packages/common/ba2_common/core/utils.py tools/build_senate_universe.py packages/common/tests/test_utils_pure.py
git commit -m "refactor(senate): extract is_tradable_stock_ticker as a shared pure helper"
```

---

## Task 5: FMPSenateTraderWeight basket gather/process (backtest side)

**This is the largest task.** Do not attempt it in one sitting — it's a genuine rewrite of
the expert's `_gather`/`_process` entry points, reusing internals that already exist.

**Files:**
- Modify: `packages/experts/ba2_experts/FMPSenateTraderWeight.py`
- Test: `packages/experts/tests/test_senate_gather_process.py` (extend — this is the 39-test
  suite; DO NOT let it regress, the existing single-symbol path must keep working since live
  parity (Task 6) and any direct-symbol callers still use it)

**Step 1: Design the basket gather (write this as a design comment/docstring first, then code)**

Add a new method `_gather_all(self, providers, as_of)` (parallel to the existing per-symbol
`_gather`, do not modify `_gather` itself) that:
1. Calls `self._fetch_senate_trades(symbol=None)` / `self._fetch_house_trades(symbol=None)`
   (both already support this via `FMPCongressTradingMixin._fetch_congress_trades` — confirm
   the Weight-side wrappers at line ~562-568 pass `symbol` through unchanged so `symbol=None`
   reaches the mixin; if they currently require a non-None symbol, relax that).
2. Applies `max_disclose_date_days`/`max_trade_exec_days`/`max_trade_price_delta_pct`
   filtering — reuse `_filter_trades`/`_disclosure_date_ok` (already generic, not
   symbol-specific in their date logic — confirm by reading them, they operate per-trade).
3. **NEW**: filters out non-tradable symbols using Task 4's `is_tradable_stock_ticker`.
4. Groups the surviving trades by symbol (`Dict[str, List[trade]]`).
5. For each surviving symbol, resolves `current_price` (same `providers.price_at_date`/
   `_get_current_price` call the per-symbol `_gather` already makes, just looped now) and
   builds the SAME per-trader history/skill/confidence pre-resolve maps `_gather` already
   builds (Stages 2-4 in the existing `_gather`, line ~422-474) — these are already
   symbol-INDEPENDENT except for the final per-symbol trade list, so this work happens ONCE
   for the whole bar and is shared across all qualifying symbols (this is the actual
   performance win: today it's redone per symbol via the per-symbol `_gather` call chain;
   the basket path does it once, explicitly, instead of relying on the day-scoped memo cache
   fix from earlier this session to de-duplicate it after the fact).
6. Returns a bundle keyed by symbol:
   `{symbol: {"filtered_trades": [...], "current_price": float, "trader_history_by_name": {...}, ...}}`
   — same per-symbol shape `_process` already consumes, just N of them instead of 1.

**Step 2: Design the basket process**

Add `_process_all(self, bundle_by_symbol, settings, as_of) -> List[Recommendation]` that
loops the bundle from `_gather_all` and calls the EXISTING `_calculate_recommendation` once
per symbol (unchanged signature/logic — this is where distance/signal/trader-score scoring
already lives, see line ~1599 onward), wrapping each result into a `Recommendation` with
`raw_outputs["symbol"] = symbol` set (matching Task 3's finding for Copy), and appending to
the returned list. Symbols whose recommendation is HOLD/SKIP/ERROR are still included in the
list (the engine's `_recommendation_to_expert_recommendation`, called per basket item by
Task 2's `_run_basket_expert_bar`, already knows how to turn a HOLD/SKIP into "not staged" —
don't pre-filter them out here, that logic must not be duplicated).

**Step 3: Add the new `analyze_as_of` override... wait, don't — add a NEW method, keep the existing one**

`FMPSenateTraderWeight.analyze_as_of` (line ~552) currently returns a single `Recommendation`
and is required by any caller still doing per-symbol analysis (there may not be any left
after Task 6, but don't delete the single-symbol path in this task — Task 6 decides whether
it's still reachable). Instead:
- Set `analyzes_as_basket = True` as a class attribute — but this changes what a SINGLE
  `analyze_as_of` method must return (it can't return both a `Recommendation` AND a
  `List[Recommendation]` depending on caller). Resolve this by making `analyze_as_of` itself
  branch: if `context.extra.get("symbol")` is set (the old per-symbol call convention), keep
  today's single-`Recommendation` behavior calling `_gather`/`_process`; if no symbol is
  pinned (the new basket call convention Task 2's `_run_basket_expert_bar` uses — it never
  sets `context.extra["symbol"]`), call `_gather_all`/`_process_all` and return the list.
  Write this as an explicit `if`/`else` with a comment explaining why both branches exist —
  do NOT silently guess.

**Step 4: Write the failing tests**

Add to `packages/experts/tests/test_senate_gather_process.py`:

```python
def test_gather_all_matches_per_symbol_gather_for_each_surviving_symbol():
    """Differential test: for every symbol that survives _gather_all's window+tradability
    filter, the per-symbol bundle it produces must be equivalent to calling the EXISTING
    per-symbol _gather(symbol=that_symbol, as_of) directly -- proves the basket path isn't
    silently dropping or corrupting data relative to the already-trusted per-symbol path."""
    ...

def test_process_all_matches_calculate_recommendation_per_symbol():
    """Differential test: _process_all's per-symbol Recommendation must match calling
    _calculate_recommendation directly on that symbol's filtered_trades -- same signal,
    confidence, expected_profit_percent."""
    ...

def test_gather_all_excludes_non_tradable_symbols():
    """A disclosed trade in a mutual-fund-pattern ticker (e.g. 'FTGCX') must never appear
    in _gather_all's output, even if it otherwise passes all date/amount criteria."""
    ...

def test_analyze_as_of_basket_mode_returns_list_when_no_symbol_pinned():
    """context.extra without 'symbol' -> analyze_as_of returns List[Recommendation]."""
    ...

def test_analyze_as_of_single_symbol_mode_unchanged_when_symbol_pinned():
    """context.extra['symbol'] set -> analyze_as_of returns a single Recommendation,
    byte-identical to today's behavior (regression guard)."""
    ...
```

**Step 5: Run to verify failures, then implement, then run to verify passes**

```
./venv/bin/python -m pytest packages/experts/tests/test_senate_gather_process.py -v
```

Expected: new tests FAIL first, then PASS after implementation; all 39 pre-existing tests
stay PASS throughout (run the whole file after every change, not just the new tests).

**Step 6: Performance sanity check**

Adapt the `senate_profile_fulllen.py` script pattern from this session (cProfile +
`frozen_ttl_cache`, full 42-month range, opt 178's stored config) to run the BASKET path
instead of the per-symbol path (call `analyze_as_of` with no symbol pinned, once per bar, for
the whole run) and confirm wall time is lower than the per-symbol baseline measured this
session (~543s cProfile'd for the full-length per-symbol run, post the two disk-cache/
in-memory-model fixes already landed). Record the before/after in the plan's companion PR
description — don't skip this, it's the entire point of the plan.

**Step 7: Commit**

```bash
git add packages/experts/ba2_experts/FMPSenateTraderWeight.py packages/experts/tests/test_senate_gather_process.py
git commit -m "feat(senate): basket _gather_all/_process_all + analyze_as_of dual-mode dispatch"
```

---

## Task 6: Live wiring (JobManager parity)

**Files:**
- Modify: `packages/experts/ba2_experts/FMPSenateTraderWeight.py`
- Test: `packages/experts/tests/` (new or extended — model on however Copy's live
  `run_analysis`/`get_expert_properties` are tested, if at all; if there's no existing live
  test for Copy, this is the first one — keep it narrow: assert the property dict shape and
  that `run_analysis("EXPERT", ...)` creates N `ExpertRecommendation` rows against a fixture,
  mirroring Copy's own test if one exists, else `test_tools/test_fmp_senate_copy.py`'s
  approach even though that file isn't pytest-collected — copy its FIXTURE technique, not its
  location)

**Step 1: Add `get_expert_properties`**

```python
@classmethod
def get_expert_properties(cls) -> Dict[str, Any]:
    return {
        "can_recommend_instruments": True,
        "should_expand_instrument_jobs": False,
        "required_instrument_selection_method": "expert",
    }
```

**Step 2: Add `run_analysis` handling the `"EXPERT"` symbol**

Mirror `FMPSenateTraderCopy.run_analysis`/`_run_enter_market_analysis` (line ~854-1013)
closely: route on `market_analysis.subtype`, call `self._gather_all(self._live_providers(),
as_of=None)` then `self._process_all(bundle, settings, as_of=None)` (Task 5's new methods),
then loop the returned list creating one `ExpertRecommendation` per symbol via
`self._create_expert_recommendation(...)` (check whether `FMPSenateTraderWeight` already has
an equivalent helper — search the file for `ExpertRecommendation(` constructor calls in the
existing single-symbol `run_analysis`, likely present already for the per-symbol live path;
reuse/factor it out rather than duplicating Copy's version verbatim).

**Step 3: Write the failing test, run, implement, run again — standard TDD cycle per Steps
1-4 of every prior task.**

**Step 4: Manual live-safety check (do NOT skip)**

Before committing, grep the whole repo for any code that assumes `FMPSenateTraderWeight`
always dispatches per-symbol (e.g. UI pages rendering per-instrument analysis history,
anything keying off `enabled_instruments` specifically for this expert class). The
`instrument_selection_method` setting itself is a per-`ExpertInstance` DB setting, not a
class default — so existing LIVE `FMPSenateTraderWeight` instances (if any exist in the dev
account) will keep their current `static`/`enabled_instruments` configuration and be
UNAFFECTED until an operator explicitly changes their `instrument_selection_method` setting
to `expert` — confirm this is true by reading `_get_enabled_instruments` (line ~750-800 in
`JobManager.py`, already read this session) once more with this specific question in mind.

**Step 5: Commit**

```bash
git add packages/experts/ba2_experts/FMPSenateTraderWeight.py packages/experts/tests/
git commit -m "feat(senate): live EXPERT-symbol dispatch parity (should_expand_instrument_jobs=False)"
```

---

## Task 7: Docs + version bump

**Files:**
- Modify: `ba2_trade_platform/version.py`
- Modify: `docs/plans/2026-07-15-senate-weight-fast-optimization.md` (add a note pointing at
  this plan — the universe-discovery work that plan did is now partially superseded: a
  basket-dispatch Senate expert derives its own working symbol set per bar from live
  disclosures + the tradability filter, rather than depending on a periodically-regenerated
  static `tools/senate_universe.txt`. Don't delete that plan or the tool — the static
  universe file is still useful for OHLCV/FMP-history PREWARM scoping, just no longer the
  expert's runtime dispatch mechanism.)
- Modify: `EXPERTS.md` and/or `docs/EXPERT_SYMBOL_EXECUTION_GUIDE.md` if they list which
  experts use which dispatch mode — add `FMPSenateTraderWeight` alongside `FMPSenateTraderCopy`.

**Step 1: Bump version**

Increment the build number per CLAUDE.md's "increment before every push" rule.

**Step 2: Commit**

```bash
git add ba2_trade_platform/version.py docs/ EXPERTS.md
git commit -m "docs(senate): basket-dispatch plan follow-through + version bump"
```

---

## Final verification

Run the full relevant suites one more time before considering this plan done:

```bash
./venv/bin/python -m pytest packages/common/tests -q
./venv/bin/python -m pytest packages/experts/tests -q
cd testplatform/backend && ./venv/bin/python -m pytest tests -q -k "backtest or daily or senate"
```

Then relaunch `tools/run_senate_matrix.py` (the S2/S3/S5/S6 grid this session was trying to
get through) with `FMPSenateTraderWeight` now in basket mode and confirm real wall-clock
`gen X/Y ind Z` progress ticks land well under the per-symbol baseline.
