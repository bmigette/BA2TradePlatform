# Cross-Model Code Review — BA2 Trade Platform (2026-06-30)

**Reviewers:** OpenAI **gpt-5.5** + Moonshot **kimi-k2.7-code**, run via **aider 0.86.2** in
read-only *ask/audit* mode (no edits), high reasoning/thinking effort.
**Scope:** highest-risk core of all three areas — **trade** (live), **test** (backtest/opt),
**common** (shared experts/providers/core). 5 batches × 2 models = 10 audit runs.
Raw per-model outputs are in `./raw/`.

Each finding below is tagged after **verification against the actual code**:
**✅Confirmed · ⏳Latent (not exercised today) · ❌False-positive**, plus a **Grid** column:
does fixing it change the running grid's fitness (`calmar`, which is **100% equity-curve-derived**)?

---

## TL;DR

- **Grid restart: NOT needed.** No finding changes the equities-5min-screener equity curve
  (→ calmar) for the configs the grid is exploring. All real findings sit in trade-list display
  metrics, the live-trade path, options (not backtested), caches/build, or robustness for inputs
  that don't occur in the grid. **Let the grid keep running.**
- **The models are productive but ~30% false-positive** on HIGH items — verification matters.
- **Most valuable real findings** are in the **live trade** path (options assignment/exercise,
  sentinel TP/SL defaults) and **build/cache robustness** — worth fixing, none urgent for the grid.

---

## Grid-impact verdict (the restart question)

| Finding class | Touches equity curve / calmar? | Grid |
|---|---|---|
| Trade-list metrics (open-at-end, commission×2, Sortino) | No — display only | ✅ valid |
| Live-trade path (Alpaca, TradeManager, JobManager, WorkerQueue) | No — not backtested | ✅ valid |
| Options (cash guard, x100 multiplier, assignment) | No — equities-only BT | ✅ valid |
| Caches/build (metric_store, persist_sentinel, TTLCache, `_WORKER_BAR_CACHE`) | No — already built / memory only | ✅ valid |
| position_sizing NaN / wrong-side-stop | No — grid inputs clean (real prices, %-stops) | ✅ valid |
| genetic genes-exceed-range | No — search-space fidelity, not result-corruption | ✅ valid |
| **Stop gap-fill** | Yes (fill price) | ⚠️ marginal on 5min |
| Empty-AND all-children-off → vacuous-True entry | Yes (entry set) | ⚠️ narrow subset of trials |

**Conclusion:** the only two equity-curve-adjacent items are marginal (5-min intraday gaps are
tiny; the empty-AND edge hits only trials that disable *every* entry leaf, and is debatably
intended). Neither materially moves calmar. **No restart.**

> Verified the two paths that *could* have changed this: (1) FactorRanker's rebalance submits
> orders **directly** via `account.submit_order` (`portfolio.py:327/341`), NOT through the
> `Increase/DecreaseInstrumentShareAction` flagged by Kimi — so those action bugs are **live-only**.
> (2) The grid strategies open→bracket→close (no scale-in action). So the common-core `TradeActions`
> findings touch **no grid backtest path**.

---

## Confirmed — worth fixing (none block the grid)

### Live trade (real-money) — highest priority
- ✅ **Option assignment/exercise effects not applied** — `AlpacaAccount._apply_option_activity`:
  `OPEXC` closes the option with `close_reason="exercised"` but doesn't apply the exercise's
  equity/cash (share delivery) effects; short-call assignment reconciles by `100*contracts`
  imprecisely. *(HIGH, live)*
- ✅ **Dangerous sentinel TP/SL defaults** — `DEFAULT_TP_PRICE=9999.0`, `DEFAULT_SL_PRICE=0.01`
  used as live order defaults; a missing/0 level can submit a real order at an absurd price.
  *(HIGH, live)* — fix: directional defaults or refuse to create the bracket leg.
- ✅ **`submit_to_broker=False` ignored for TP/SL adjust** — `TradeActionEvaluator` (~301) and
  `_AdjustPriceLevelAction.execute` (`TradeActions` ~720) call the broker even in manual-review
  mode. *(HIGH, live)* — backtest unaffected (simulated account).
- ✅ Alpaca order pagination uses `until_date` (can miss/duplicate at the boundary); option qty
  not `abs()`-normalized; `account_id` missing on a regenerated TP order; OCO legs added to the
  "safe set" before broker confirmation; TP `limit_price` not rebased when the dependent leg is a
  TP. *(MED, live)*
- ✅ **JobManager full-schedule refresh silently drops schedules** (~180/380). *(HIGH, live)*

### Build / cache robustness (no result impact)
- ✅ **`metric_store.existing_months` treats `ym=` dir-exists as complete** — `build_store`
  flushes partial files; a crash mid-month → next build skips it → silently incomplete store.
  Fix: atomic completion marker. *(HIGH, build)*
- ✅ **`_WORKER_BAR_CACHE` (price_source) unbounded, no eviction** → multi-GB/OOM on long-lived
  workers (same family as the FactorRanker OOM, different cache). Fix: evict per-optimization like
  `_FULL_SERIES_MEMO`. *(HIGH, stability — ⚠️ watch the grid's later mid/small jobs)*
- ✅ `persist_empty_sentinel` is a **global flag, not thread-local** (unlike `frozen`/`hermetic`);
  `TTLCache.get_or_call` releases its lock before `fn()` → **no single-flight** (FMP bursts);
  `write_partitions` `.tmp` name not unique per process. *(MED, concurrency)*

### Backtest correctness (confirmed, but not grid-invalidating)
- ✅ **Stop orders fill at `stop_price`, ignoring gap risk** — if a bar opens beyond the stop the
  realistic fill is the unfavorable open. *(real; marginal on 5-min, bigger on daily)*
- ✅ `daily_engine` swallows all non-cache-miss per-symbol expert exceptions → a real expert bug is
  hidden and the symbol silently skipped. *(MED — fix: let unexpected errors propagate)*
- ✅ Trade-list-only (no calmar impact): **open-at-end positions counted as closed trades**;
  `commission*2` regardless of fill count on scaled in/out; **Sortino** divides Σr² by the
  *downside* count (non-standard denominator).
- ✅ `genetic`: decoded genes can exceed the declared max by a step; empty `choices` → `max=-1`
  invalid range; `strategy_param_space` empty AND/OR after toggling all children → vacuous True.
  *(edge cases)*
- ✅ FactorRanker metric_store path drops `float_min/max` + `price_drop_days` filters → different
  universe than configured. *(MED)*

### Common-core robustness
- ✅ `position_sizing` lacks `math.isfinite` validation and accepts a wrong-side stop
  (`abs(price-stop)` with no long/short check). *(MED — harmless on the grid; matters live)*

---

## Verified FALSE POSITIVES (do not chase)

- ❌ **"FMP 200-status error dicts aren't detected"** — `fmp_common` HAS `_FMP_ERROR_KEYS` +
  `FMPError` and **raises** on error dicts (retry→raise). The fetchers' `else []` never sees one.
- ❌ **"FactorRanker `min_price` uses live price in backtest"** — `BacktestAccount.
  get_instrument_current_price` binds the provider `as_of` to the simulated bar (per-bar
  `set_clock`); it's point-in-time, not live.
- ❌ **"`_size_and_submit` not called for management orders"** — `_manage_open_positions` submits
  with `submit_to_broker=True` (direct); only entries use `False` + `_size_and_submit`.
- ❌ **"`TrialBroker` shared across threads without sync"** — `trial_broker.py` guards *every*
  method with a `threading.Condition`. (Model couldn't see that file.)
- ⏳ **Options x100 multiplier missing in the profit cap / option buys not cash-secured** — real in
  the code but **latent**: options backtest isn't supported (equities-only), so no current effect.

---

## Methodology notes
- gpt-5.5 answered in French despite the English instruction (content unaffected); kimi-k2.7-code
  needed `temperature=1` and UTF-8 I/O on Windows. kimi emits a full chain-of-thought.
- Files audited: backtest_account, daily_engine, results, strategy_param_space, distributed_eval,
  FMPRating, FactorRanker, metric_store, fmp_common, AlpacaAccount, TradeManager, TradeActions,
  option_selector, position_sizing, TradeActionEvaluator, JobManager, WorkerQueue,
  strategy_optimization_handler, genetic, worker_client, price_source.
- NOT covered (candidates for a follow-up batch): IBKR/TastyTrade accounts, SmartRiskManager graph,
  TradingAgents, the UI, the API layer.
