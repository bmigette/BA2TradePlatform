# Equity Short Selling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Rules can open equity shorts (sell from flat, gated on the expert's `enable_sell`), in
live trading and backtests, with a netting rule replacing `allow_hedging`. No current rule, expert
or stored backtest changes behaviour.

**Design:** [2026-09-24-equity-short-selling-design.md](2026-09-24-equity-short-selling-design.md).
Read it first.

**Architecture:** The only blocker is `SellAction.execute`; sizing, stops, Alpaca/TastyTrade exits
and the backtest account are already short-capable. Changes are small and gated. Every stored
backtest is long-only, so byte-identity is testable.

---

## Hard constraints (every task)

1. **No behaviour change for current rules, experts or stored backtests.**
   - A long-only run is byte-identical.
   - An expert with `enable_sell=false` (all live experts) behaves as today.
2. **Do not touch the live platform.**
   - Work only in the worktree `C:\Users\basti\Documents\dev\BA2-pullback`, branch
     `feat/short-selling`.
   - Never edit `C:\Users\basti\Documents\dev\BA2TradePlatform`; prod, dev and 8082 run from that
     clone.
   - Never call 8080/8081/8082, never write to real DBs, never set BA2_TEST_KEEP_DB.
3. **Python:** `C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe`.
   - `packages/common/tests`, root `tests/`, backend `tests/` (without tests/backtest) and backend
     `tests/backtest` are separate invocations, never concurrent.
   - Root runs use `--ignore-glob='tests/test_portfolio_allocation*.py'`, then those files alone.
   - Backend runs from `testplatform/backend`.
   - **Known pollution:** a tests/backtest test leaks the in-memory trade-store flag. SQLite-mode
     engine tests must force it off and assert the mode.
4. **Baselines** (failures that are not yours):
   - root: 2 × test_no_zero_coercion;
   - common: up to 5 × test_bool_setting_round_trip;
   - backend non-backtest: 17, the known list (replay/test_historical 3, convex_grid_foundations 1,
     launcher_holdout_rail 1, option_selection_weight_genes 6, options2_matrix_script 1,
     strategy_fitness_equity_frozen 5);
   - providers: test_split_basis_refetch 1, plus the flaky test_market_condition_warmup coalesce test;
   - backtest: 0.
   - Classify any other failure against a run on origin/dev before claiming it is not yours.
5. **The `test_no_zero_coercion` line allowlist** must stay green (`test_no_allowlist_entry_is_stale`).
   Re-point line numbers in edited allowlisted files.
6. **Commit per task** with the session trailers. No push, no version bump.

---

### Task S1: sell opens a short from flat; opposite orders never flip

**Files:**
- `packages/common/ba2_common/core/TradeActions.py`: `SellAction` (~442-510), `BuyAction`, and the
  quantity/close helpers they use.
- Whatever risk-management step sets the quantity of a PENDING sell or buy: trace
  `TradeRiskManagement` for SELL/BUY entries vs closes (`_filter_orders_by_permissions` ~220/634,
  sizing).

**Behaviour:**
- **`sell`, long held:** unchanged meaning (reduce or close), but the final quantity is capped at the
  held quantity. Check today's behaviour first; if it already caps, pin it with a test.
- **`sell`, flat:**
  - if the expert's `enable_sell` (read via `get_setting_with_interface_default` + `coerce_bool`) is
    true, create an ENTRY sell order that risk management sizes like a buy entry, with a safeguard
    stop above;
  - otherwise refuse with "No position to sell and selling is disabled for this expert (enable_sell
    is off)".
  - A position-fetch failure still refuses, as today.
- **`buy`, short held:** covers or reduces the short, capped at the short size (never flips to long).
- **`buy`, flat or long:** unchanged.
- **Direction-aware protective legs:** follow the existing `is_long`/side plumbing, and verify the
  entry bracket (TP/SL adjust actions after a sell entry) computes a short's SL above and TP below.
  A3 established that stop percents are direction-relative (−8 = adverse).

**Tests** (packages/common/tests/test_short_selling_actions.py, plus a backend engine test):
- every row of the design's section 1 table;
- the refusal message;
- a position-fetch failure refuses;
- **engine:** a real backtest with `enable_short` gives a short entry from flat; the stop is above
  the entry and the TP below; covered by the stop or by the reverse signal;
- **no-impact:** a long-only real-engine run is byte-identical before and after. Patch the old
  SellAction back in for the comparison, or compare against committed golden output.

### Task S2: the netting rule replaces `allow_hedging`

**Remove:**
- the `allow_hedging` interface setting (packages/common/.../MarketExpertInterface.py ~213);
- the UI checkbox and its load/save code (ba2_trade_platform/ui/pages/settings.py ~2070, 2871-2904,
  4781-4783);
- the Smart Risk Manager prompt lines (SmartRiskManagerGraph.py ~710-757, ~2339-2351);
- the toolkit branch (SmartRiskManagerToolkit.py ~2132).

**Replace them with the netting rule:**
- The toolkit's `open_*_position` on a symbol with an opposite open position refuses to open.
  Point the LLM to close/reduce tools instead, or make it reduce: check what the toolkit's existing
  non-hedging branch does, and keep that behaviour, which is what `allow_hedging=false` gave.
- Prompt text: "an order opposite to an open position only reduces or closes it".

**Checks:**
- Deploy parity and export/import stop carrying the key; stored values are ignored. Verify
  `expert_batch_export_import`, settings export and `deploy_parity` don't choke on a stored stale
  key.
- grep the repo (including tests) for `allow_hedging` and update or remove references.

**Tests:**
- no `allow_hedging` remains in the settings definitions;
- the toolkit refuses an opposite open;
- a stale stored value is ignored;
- the batch export/import round-trip still works.

### Task S3: Alpaca shortability check

**File:** `ba2_trade_platform/modules/accounts/AlpacaAccount.py`, at the point `submit_order`
handles a SELL that opens a position.
- Before submitting an order that OPENS a short, fetch the asset. There may be an existing asset or
  marginability helper around lines ~2438-2544; reuse any asset cache.
- Refuse, with a clear error surfaced like other submit refusals, unless `shortable` and
  `easy_to_borrow` are both true.
- Closing sells (reducing a long) skip the check.

**Tests:** fake the Alpaca client/asset. Cover:
- a shortable, easy-to-borrow asset → submitted;
- not shortable → refused, with no broker order;
- a closing sell → no asset check.

### Task S4: backtest borrow cost

**Files:** `testplatform/backend/app/services/backtest/backtest_account.py` (a daily accrual on
open short market value), the trial-config whitelist (`strategy_optimization_handler._build_daily_trial_config`),
`results.build_results` (echo the config plus a separate `short_borrow_cost` line), and the
launcher/driver passthrough if needed.
- **Setting:** `short_borrow_rate_pa`, default 0.005. It must survive the whitelist; see memory
  "trial-config whitelist drops new knobs", and add a test at each hop.
- **Accrual:** once per session, charged to cash or equity the same way spread/commission costs are
  booked; read how the account books them.
- **No shorts, no change:** zero charge and byte-identical results.

**Tests:**
- a held short accrues rate/252 (or the calendar convention already used for interest) per session;
- the config reaches the account;
- long-only results are unchanged.

### Task S5: parity, the pullback_rsi unlock, and live-path tests

**Live path:** `TradeManager` enter-market pass plus a fake Alpaca account, with a sell rule and
`enable_sell` on and the position flat. Check:
- an entry SELL order with a safeguard stop above;
- the OCO/exit spec built with BUY_STOP above and the TP below;
- the netting rows.

**Parity:** the same bars and recommendation in the backtest engine and the live path give the same
entry side, stop and TP.

**pullback_rsi:**
- remove the short refusal (`runtime.opens_equity_short` / `refuse_unrunnable` and preflight) in
  tools/strategy_research/exploration/;
- the strict xfail `test_the_short_job_opens_a_short` must now PASS: convert it to a normal test;
- the default exploration fingerprints stay unchanged (`798c87…`, pullback_rsi `b9524f…`); verify
  them.

### Task S6: docs

- `packages/common/ba2_common/core/rules_documentation.py`: SELL text (broker semantics, the gate,
  netting).
- The settings help for `enable_sell`.
- `docs/strategy_research/exploration/pullback_and_market_exits.md`: the short jobs now run; remove
  the "equity shorts can't open" gap.
- The design doc status: implemented.

### Final: full verification + branch review

- Run all 7 suites (common, experts, providers, root, root_pa, backend, backtest) and compare the
  failure lists against the baselines.
- A final opus review of live-path safety.
