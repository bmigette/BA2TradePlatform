# Code review — 2026-09-12 → 2026-09-22 (dev `88ecd31d^..8ca3d737`)

This review covers 270 commits, about 230 once the `docs(grid): progress` commits are left out. It ran as six read-only passes. I rechecked the main claims from each pass against the code before including them:

- **Verified:** read in the code or reproduced.
- **Suspected:** plausible, but not traced end to end.

The review changed nothing in the repo; the only new file is this report.

| Area | Share of commits | Main paths |
|---|---|---|
| Option market-condition genes (ohlcv-v1, ta-structure-v1) | ~35% | `packages/common/.../market_condition_*`, `packages/providers/.../market_conditions`, `split_basis.py` |
| Options engine, GA, grid, perf, shared arrays | ~25% | `testplatform/backend/app` (genetic, handler, backtest_account, fitness), `ba2test_launcher.py`, `tools/run_options_matrix.py` |
| Live platform (orders, UI, option chart, reports, experts) | ~20% | `AlpacaAccount.py`, `TradeManager.py`, `JobManager.py`, `ui/pages`, `ui/components` |
| Tools, CI, frontend, docs/plans | ~20% | `tools/`, `.github/`, `testplatform/frontend`, `docs/plans` |

---

## 1. Priority list (do these first)

| # | Sev | Finding | Where |
|---|---|---|---|
| P1 | **Critical (live parity)** | Every options-grid strategy carries `option_min_volume=25`, but the live Alpaca chain never sets `volume`. `check_liquidity_data_available` therefore raises on every live chain. A grid option strategy deployed as-is would **never open a trade live**. The refusal is logged, not silent. | `ba2test_launcher.py:3327,3783`; `AlpacaAccount.py:6174-6189`; `option_selector.py:192` |
| P2 | **Critical (data)** | Warmup re-checks splits only when `refetch_required` is set. A split dated after the cache end (`VERDICT_FUTURE`) is never re-checked after the top-up. Adjusted bars then get appended to unadjusted ones and published as "valid" market-condition rows. | `warmup.py:1166-1176`, `split_basis.py:~138` (370f0f2e, d50bf929) |
| P3 | Important (money) | The 24h PENDING_CANCEL last resort measures age from the order's `created_at`, not from when the cancel was sent. Take a cancelled OCO leg on a position held more than 24h whose cancel hasn't landed yet: it is immediately set back to the broker's working status, so the cancel intent is lost and the dependent replacement never fires. A single 404 on an old order is marked CANCELED on the first lookup. | `AlpacaAccount.py:2999,3025,3053` (8ca3d737) |
| P4 | Important (ops) | If one expert fails to load, `_execute_scheduled_group` raises before the slot is registered, so every expert at that fire time is skipped. Enter-market runs Mondays only, so that costs a week. `ExpertPriority` waits also have no timeout. | `JobManager.py` `_execute_scheduled_group` (72df3388) |
| P5 | Important (GA) | Resuming from an end-of-generation checkpoint inserts a **phantom generation**. It evaluates nothing, records G's best again and, since c9d83b4d, uses up one patience unit. Seen on the running remote227 job: the log says "RESUMING at generation 16", and the first evaluated individual is gen 17. It also breaks the promise that a resume reproduces the same offspring. | `genetic.py:535-537,724,788-797` |
| P6 | Important (GA) | The stall guard times the whole *batch* (5400s with no trial finishing), not each trial. It then memoises `STALLED_SENTINEL` for the rest of the run. Healthy slow genomes at the tail (trials up to 16,572s were measured) are written off permanently, so the result depends on machine load rather than the seed. | `strategy_optimization_handler.py:1694,1708-1745,1896-1910` (96cb472f/441771e8) |
| P7 | Important (live) | An expert with both profiles plus live replay capture crashes. `CapturingMarketConditionReader(self.reader)` receives a `CompositeMarketConditionReader`, which has no `_retain_windows`. The resulting `AttributeError` in `begin_decision` aborts the whole entry pass. | `market_condition_live.py:502-507`, `market_condition_readers.py:359,547` |
| P8 | Important (live) | A pinned live manifest goes stale silently. The coverage check looks at which symbols are covered, not at the session range. Once the manifest is stale, every gate reads `missing_session`, and that is logged at DEBUG only. Gated experts stop entering with no ERROR anywhere. The composite reader also memoises "missing" for the resolver's lifetime. | `market_condition_live.py` `refresh_coverage`; `TradeConditions.py:~4250`; `market_condition_readers.py:640` |
| P9 | Important (UI money) | The Floating P/L widgets don't handle options. They check no `asset_class`, apply no ×100 multiplier, and look up the underlying symbol in a price map keyed by OCC symbols. Spread legs that net out show `0.0`, which looks like a measured zero. The TastyTrade broker figure covers equities only but is used as the account total. | `FloatingPLPerAccountWidget.py:457-537`; `TastyTradeAccount.get_broker_floating_pl` (125721f8) |
| P10 | Important (CI) | The PARITY GATE step writes no JUnit XML. The new failure annotator (86c21033/352336e2/955dfa4f) therefore prints nothing for the gate that matters most. | `.github/workflows/parity-and-coverage.yml:56-68` |

---

## 2. Findings by area

### 2.1 Market-condition genes (vs `docs/plans/2026-09-15-option-market-condition-genes-{design,impl}.md`)
**Verified clean:**
- No lookahead: a decision on day D reads only D's previous regular session, including over weekends, holidays and half-days.
- Pivots are used only once confirmed.
- The batch computation matches the 128-bar reference.
- Naive timestamps are refused.
- Gene ranges match the design.
- Profiles and manifests reach `_build_daily_trial_config`.
- Gates sit only on AND entry trees.

**Findings:**
- **P2, P7, P8** above.
- **Important: gated matrix runs reuse the ungated job name** outside `--profile discovery` (`run_options_matrix.py:225`, 4bc97cb1). The run is either skipped as already DONE or resumes the ungated checkpoint with a wider genome.
- **Important: batch expert import skips the market-condition checks** `assert_no_market_fields`/`assert_fields_served` (`expert_batch_export_import.py:269-337`, 54e0f25e). The same file uses `general.get("account_id") or 1` and `virtual_equity_pct ... or 100.0`, which break CLAUDE.md's no-default rule.
- **Important: `import_deploy_payload.py` writes before it can refuse.** Line 207 commits an enabled instance before the refusal at line 228, so the comment "Nothing is written" is false.
- **Suspected Important: the FMP API key may end up in manifests.** Fetch errors are stored as `f"{type(e).__name__}: {e}"` in the manifest coverage. A requests error includes the URL with `?apikey=`, and the manifest carries a digest and is shipped to workers.
- **Minor: two functions build the decision context,** one for backtest (`market_condition_bt.py:100-124`) and one for live (`market_condition_live.py:336-352`). On a holiday, live evaluates against Friday while the backtest refuses with `no_context`. There is no parity test (design §8.7), and it breaks the one-shared-function rule.
- **Minor, smaller items:**
  - A genome missing its `cond:<id>:mode` gene keeps the gate ON at its anchor value.
  - Break-of-structure and change-of-character walk back using *today's* structure, and the test oracle shares the bug.
  - `_append_market_condition_gates` doesn't check that the root group is AND.
  - Symbol case differs between `missing_coverage` and `observe()`.
  - `test_two_concurrent_builders_coalesce_on_one_manifest` failed once in 20 runs.

**Design gaps:**
- §4.5: live never rolls forward to a fresh manifest.
- §4.6: there is no garbage collection of the central store.
- Deploy payloads don't carry the manifest digest or calc version.
- The <1% trial-cost gate was measured for ohlcv-v1 only.
- `run_options2_matrix.py` and `run_convex_matrix.py` lack support, and no deferral is recorded.
- The design header still says "not implemented".
- The impl plan's leaf ids differ from the registry ids, and those ids are persisted gene keys.

### 2.2 Options engine / GA / grid / perf
**Verified clean:**
- Every trial path resets the overlay in a `finally` block.
- Running out of shared-array file descriptors is fatal and loud.
- No new knob is dropped by `_build_daily_trial_config`.
- The direction gene uses the same code in backtest and live.
- f5933fd6's structure labels are correct.

**Findings:**
- **P5, P6** above.
- **Important (suspected, methodology): a DeterministicScorer genome with no signal can trade.** Since d66a5a75 every weight can reach 0. The score is then 0, giving HOLD at the confidence floor of 5.0, which passes `confidence <= X` on O_STRD/O_STRG/O_IC. That genome enters every symbol on every scheduled day.
- **Minor: the new audits never run.** No conftest enables `BT_MTM_AUDIT` or `BT_TRADE_STORE_AUDIT`, although the comment says the suite turns them on (faf553a6).
- **Minor: soft30 counts 30 structures over the whole window,** which is 5 per year on 2020-25, not 30 per year (`strategy_fitness.py:1797-1808`).
- **Minor, smaller items:**
  - `as_utc_key` treats wall-clock `created_at` and simulated `open_date` as the same clock (`utils.py:1399`).
  - `_elite_slice` has no `is_measured_result` filter.
  - `_resumed_no_improvement` is never reset.

**What could inflate the stage-1 O_LC +2042% / -37.7% DD best:**
1. It is leveraged beta on ~97 large caps in a mostly bull 2020-25 window, with sizing up to 10% of equity per ticket, compounding.
2. There are no real quotes. The model uses a flat spread of 5% of premium plus a 0.02 tick (doubled under 100 contracts/day). Cheap OTM calls typically trade 10-30% wide.
3. Entry limits keep retrying on later bars until the premium comes back to the limit, which picks favourable fills.
4. CAR has no cap above the targets, which pushes sizing to the maximum.
5. Marks without a bar fall back to Black-Scholes at the last IV, which smooths the recorded drawdown.

Before trusting this result:
- Compare it with a 2× QQQ hold.
- Report 2022 on its own, and the top-5 trades' share of P&L (the concentration memo).
- Re-score it at 2× spread.

### 2.3 Live platform
- **P3, P4, P9** above. The 97 targeted tests (wash-trade lock, option tab, floating P/L) pass.
- **Important: the buying-power clamp falls back to figures it shouldn't use.** When the snapshot's `buying_power` is None it uses Alpaca's *effective* buying power, which b913de9b deliberately rejected. It swallows the snapshot exception without logging, and its last fallback is `get_balance()`, which returns equity.
- **Minor: a universe-only settings import still wipes settings,** including `allow_automated_trade_opening` (`settings.py:4013-4019` vs `:4156`). A failed `save_setting` only logs a warning.
- **Minor: `option_closed_pnl` has its own realised-P&L formula.** It doesn't normalise a signed credit `open_price`, so the sign may flip, and it duplicates `calculate_option_pnl`. Separately, `calculate_transaction_pnl` defaults the multiplier to 1 while the display refuses to price without one.
- **Minor: Options tab load.** Every 30s refresh runs one order query per transaction and one broker quote per contract. A load failure shows an empty table, and `_quote_snapshot` is reset from a worker thread, which can race.
- **Minor: a missing fill price is recorded as `0`** in `get_filled_trades` (`AlpacaAccount.py:5909`), which breaks the no-fallback rule.
- **Minor: `fred_series.refresh_series` writes a fixed `{path}.tmp` with no lock.** Concurrent refreshes can collide on Windows `os.replace`, leaving the stale file in use.

### 2.4 Tools / CI / frontend / docs
- **P10** above.
- **Minor: annotation injection.** The annotator strips newlines from the failure body but not from `classname::name`, so a crafted test id could forge an annotation.
- **Clean:** no blanket-kill patterns, the `backup_dbs.py` fix is correct, settings export/import opens the right DB, and the frontend `hiddenTradeIds` fix is a real fix.
- **Stranded test:** `test_files/options_data_audit_20260922_offline.py` has real asserts pinning an IV-rank mismatch between UI and rule, but pytest never collects it. Port it to `tests/`.

**Versioning compliance (CLAUDE.md):**

| Commit(s) | Changed | Bump | OK |
|---|---|---|---|
| 72df3388 | packages/common | APP only | No |
| 631009f4 | testplatform/backend | none | No (next push) |
| 10136037..96cb472f | testplatform/backend | none | No (next push) |
| 02d99d8c | testplatform, tools | none | No |
| ce0eb688→7f2f58a5 | — | TEST 0075→**0073**→0074→0075 (went backwards, then reused numbers) | No (stale base) |

`ensure_synced` compares version strings for inequality, so no worker desync is proven. Two sessions each computing "+1" from a stale checkout could still collide, though. Two suggestions:
- A CI or pre-push check that any change to `testplatform/` or `packages/` also changes `testplatform/version.py`.
- Compute the next number from `git show origin/dev:<file>`.

---

## 3. Options data capture: live vs backtest

**Short answer: no.** Neither path saves the market state of an option position.
- Greeks, IV, bid/ask and OI are all fetched into `OptionContract`, then **dropped when it becomes an `OptionLeg`** (`option_types.py:66-75`), before any order row is written.
- `TradingOrder` and `Transaction` have no greek, IV, spot or quote columns.
- The backtest keeps only the `trades` JSON.
- The prod and dev DBs currently hold 0 option orders, 0 option transactions and 0 `option_iv_snapshot` rows.

| Metric | Live stored | Backtest stored | Alpaca provides | Parity |
|---|---|---|---|---|
| Delta/gamma/theta/vega | fetched for strike selection, not stored | computed with Black-Scholes, not stored | yes (snapshot `greeks`) | missing in both; sources differ (broker vs BS from close) |
| Rho | not mapped | no | yes | missing in both |
| Per-leg IV | fetched, not stored | fetched, not stored | yes (`impliedVolatility`) | missing in both |
| Underlying IV rank | `OptionIVSnapshot` table, 0 rows, not per entry | computed per bar, not stored | no history (build from own snapshots) | missing in both |
| DTE entry/exit | derivable | derivable | derivable | OK |
| Strike/right/expiry/multiplier | order columns | trades JSON | yes | OK |
| Spot + moneyness at entry/exit | no | no | spot yes, moneyness derivable | missing in both |
| Bid/ask/mid/spread at fill | no | no (bid = ask = close in the store) | yes | missing in both |
| Fill vs mid (slippage) | fill + limit only | no | derivable if mid is saved | missing |
| Open interest / volume | OI fetched, not stored; **volume never mapped (P1)** | neither | OI daily (contracts), volume (`dailyBar.v`) | missing; volume is a live bug |
| Structure type | `option_strategy` on Transaction | **not in trades JSON** | n/a | parity break |
| Width, net debit/credit, max P/L, breakeven | limit + `max_loss_per_contract` | no | derivable | partly, live only |
| Exit reason | `close_reason`: expired/assigned/exercised, otherwise generic words | **guessed, wrongly** (see note) | activities OPASN/OPEXP/OPEXC | broken |
| Mark-to-market snapshots over the trade's life | none (lifecycle fetches chains, keeps nothing) | account equity only | yes | missing in both |
| Expert confidence / market-condition values at entry | via `expert_recommendation_id` | not in trades JSON | n/a | parity break |

How the backtest guesses the exit reason:
- Every single-leg limit close is labelled `take_profit`.
- Expiry, assignment and multi-leg exits are labelled `exit`.
- Both expiry and assignment close the transaction as `option_expiry`.

**Recommended fix: one shared code path in `packages/common`, called by both live and backtest.**
1. **Do first (P1):** map `volume` (`snapshot.daily_bar.volume`) and `rho` in `AlpacaAccount.get_option_chain`/`get_option_quote`. Add a parity test that a grid-built option config passes the liquidity check on a live-shaped chain.
2. **Entry snapshot:** carry the chosen `OptionContract` quote on `OptionLeg`. In `_submit_option_order`, write per leg: spot, moneyness, DTE, bid/ask/mid/spread%, IV, the five greeks, OI, volume and the greeks source. Add the structure facts too: strategy, width, net debit/credit, max P/L, breakeven. Use the existing `extra_entry_facts` route.
3. **Real exit reason:** record the actual trigger on the close order in `CloseOptionAction.execute` (rule id, TP, SL, time, roll, expiry, assignment or forced), plus an exit snapshot. Delete the price-proximity `_exit_reason` guess and use one close-reason vocabulary in both paths.
4. **Trades JSON:** pass the snapshots through, together with `option_strategy`, the recommendation's confidence and the market-condition values at entry.
5. **Daily mark snapshots per leg** (mark, delta, IV, spot): one shared table, written by the live lifecycle pass and by the backtest's per-bar valuation.

---

## 4. Data visualization

### Live (NiceGUI): only Alpaca-backed or derivable items
**What exists:**
- **Options tab:** strategy, expiry with DTE, legs, net and current premium, TP/SL, P/L with the multiplier. Unpriced rows are counted separately.
- **Popup:** candles, strike lines, entry/exit markers, the expiration payoff, and per contract bid/ask/mid/last, IV, greeks, ATM IV and IV rank.

**Bugs:**
- **P9:** Floating P/L (above).
- **Wrong capital figure:** "Value/CapReq" shows the credit for a credit spread, not width minus credit, and the totals add credits and debits together as "Cost".
- **DTE clock:** the table uses the server's local date and the popup uses UTC; neither uses New York time.
- **Stale quotes hidden:** the quote timestamp is collected but not shown, and the trade timestamp isn't fetched, so a stale `last` goes unnoticed.
- **Greeks per contract:** they are not multiplied by signed quantity × 100.
- **Markers a day late:** they use naive UTC `created_at`, so orders after 20:00 ET land on the next candle.
- **Colours:** the candles reuse the payoff chart's P/L greens and reds.

**Spec gaps** (`2026-09-20-option-trade-chart-spec.md`):
- §3B: breakevens and max P/L are computed but only used to size the axis.
- §2c: the overlay/marker toggles aren't wired.
- "Current" is empty for spreads, and "Value" shows the entry cost.
- Quotes take one call per contract instead of one batched `OptionSnapshotRequest`.
- The payoff logic exists twice, in Python and TypeScript, with a copied fixture table. One shared JSON fixture would keep them in step.

| # | Improvement | Effort | Value |
|---|---|---|---|
| L1 | Make Floating P/L option-aware (reuse `option_transaction_pnl` or the broker's `unrealized_pl`) | S | High |
| L2 | Show breakevens and max profit/loss under the chart | S | High |
| L3 | CapReq = max loss; separate debit and credit totals | S | High |
| L4 | "Quote as of HH:MM ET" with a staleness flag; DTE in New York time | S | Med |
| L5 | Position greeks (× qty × 100), summed per structure, expert and account | M | High |
| L6 | One batched snapshot request per refresh | S | Med |
| L7 | Premium-over-time line from Alpaca option bars; moneyness per leg; exit reason | M | Med |
| L8 | P/L by `option_strategy` and by expert; a broker-vs-platform reconciliation column | M | Med |

Leave these out of live: historical greeks, IV history from before our own snapshots, and probability of profit. Alpaca provides none of them.

### Test platform (React)
**What exists:**
- Equity curve (downsampled to 2,000 points), a drawdown tab and capital usage.
- GA best/average/minimum per generation.
- Monte-Carlo p5-p95 bars.
- A per-trade option modal: candles, strikes, markers, payoff, legs, moneyness, and IV/greeks/OI at entry and exit from the cache.

**Bugs and performance:**
- The full trades array is serialised **twice** (`models/backtest.py:344`, "backwards compat").
- `/backtests/{id}` has no pagination, and the list has no virtualisation.
- `TradingChart.tsx` hardcodes dark colours.
- `BacktestChart.tsx` is dead code.
- P/L is shown by colour alone in places.

| # | Improvement | Effort | Value |
|---|---|---|---|
| T1 | Drop the duplicate `trades` key; paginate or lazy-load trades | S | High |
| T2 | Log/linear toggle; drawdown under the equity chart with a shared x-axis | S | High |
| T3 | SPY (or QQQ) buy-and-hold overlay from the OHLCV cache | M | High |
| T4 | Grid comparison page (return vs max DD scatter per cell or structure), using the unused `POST /backtests/compare` | M | High |
| T5 | Concentration chart: top-1/top-5 share of P&L, cumulative P&L by trade rank | S | High |
| T6 | Per-trade DTE at entry and exit; exit-reason counts per structure (after §3 fix 3) | S | Med |
| T7 | Monte-Carlo path fan | M | Med |

Suggested order: P1 → L1 → T1 → L2/L3 → T2 → T5 → L4 → T4 → L5 → T3.

---

## 5. Suggested fix batches
1. **Before any live option deploy:** P1 (volume/rho mapping plus parity test), L1/P9, L3, and §3 fixes 2-4 (entry/exit snapshots, real exit reason). All of these touch `packages/`, so bump TEST_APP_VERSION at a grid job boundary.
2. **Live safety (APP bump, prod restart):** P3 (a `pending_cancel_since` timestamp in `meta_data`), P4 (isolate each expert, add a timeout to priority waits), the buying-power clamp fallback, P7, P8.
3. **Next grid-boundary deploy (TEST bump):**
   - P5: checkpoint the bred offspring, or skip evaluation on resume, and add a test that a resume through `optimize()` matches an uninterrupted run.
   - P6: time each trial on its own and retry a stall once instead of memoising it.
   - Put the profile digest into gated job names.
   - P2: re-check splits on the snapshot the build actually reads.
4. **Hygiene:** P10 plus annotator sanitisation, the versioning CI check, turn on the audits in the conftests, port the stranded `test_files` audit, and fix stale design-doc headers and ids.
