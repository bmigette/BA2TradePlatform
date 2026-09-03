# options-grid2 — consolidated final review (2026-09-02/03)

Branch `options-grid2`, reviewed at `74be78a1` (branch range `56c3f8c2..74be78a1`, ~178
commits), plus this review's own commits. Worktree
`C:\Users\basti\Documents\dev\BA2-options-grid2`. Plan:
`docs/superpowers/plans/2026-08-31-options-grid2-convex-earnings-impl.md`.

---

## VERDICT: **MERGE-READY** (re-verified independently, 2026-09-03)

> **ORIGINAL VERDICT (2026-09-02): FIX-NEEDED** — nothing structural; 5 stale tests blocked a
> clean merge. Everything below this box is the original review, unrewritten.
>
> **RE-VERIFICATION (2026-09-03), by the reviewer, on the 5 fix commits.** Every claim checked
> by execution, not by reading the commit messages:
>
> * **Backend `pytest tests/`: 4532 passed / 158 skipped / 7 failed** (was 4518/158/12). The 5
>   `O_PMCC` staleness failures are gone; the 7 remaining are EXACTLY the 2 pre-existing
>   `test_seam_wiring.py` (F3, still open) + the 5 pre-existing `curve_uneven` frozen-baseline
>   cases. No new failure anywhere.
> * `packages/common` **3133 passed / 1** (the known float-dust). Goldens **3 + 5 = 8, both
>   fingerprints unmoved**. `test_deploy_round_trip_parity.py` **16**,
>   `test_options2_matrix_script.py` **17**, `test_pmcc_lifecycle.py` **106**, the two launcher
>   gene files **284**.
> * **Mutations executed by the reviewer** (restored by file copy, tree clean after each):
>   dropping the `allow_automated_trade_modification` row from `BACKTEST_FORCED_SETTINGS` →
>   `test_every_forced_setting_reaches_the_deployed_instance` FAILS ×2 (the explicit membership
>   assertion at `test_deploy_round_trip_parity.py:372-377` is what makes a deletion
>   non-vacuous); restoring the importer's universe-block drop
>   (`import_deploy_payload.py:129`) → `test_the_import_tool_still_consumes_the_universe_block`
>   FAILS.
> * **The refreeze of the two labelled goldens is justified by `718f7cf4` alone.** Diffed: the
>   only expectations that moved are `_MIN_DTE` gaining `"O_PMCC": 365`, `groups[365]` gaining
>   one member (still three distinct thresholds), the job list gaining one row in second
>   position (the order `_jobs` yields), and `"open_pmcc"` joining the `long_premium` set in
>   both independent re-derivations. No other frozen value changed — this is a refreeze, not
>   adjust-until-green.
>
> **Table totality (§2a V3):** the handler has exactly **two** `save_settings` sites — the only
> two in the whole backtest package. The non-bypass one is 100% table-driven
> (`forced_expert_settings(_run_facts(config))`, `daily_backtest_handler.py:1187`), and all
> three trees — handler, export API (`backtests.py:1258`/`:1270`) and
> `tools/import_deploy_payload.py` — read the same table, pinned by
> `test_the_handler_and_the_exporter_read_THE_SAME_TABLE`. **One write is not a table row:**
> the BYPASS path's `enabled_instruments` (`daily_backtest_handler.py:1163-1168`). It is
> explicitly justified — in `live_settings_from_universe`'s docstring, which argues a static
> universe maps to no setting and that overwriting a live instance's universe is a separate,
> bigger decision — but the justification lives in prose rather than as a row, and no test pins
> that the bypass path writes nothing else. **One judgement call:** `equity_cap` is left out as
> market-simulation, alongside commission/slippage/spread/fill_model; unlike those it is a
> capital constraint rather than an exchange parameter, so it is the one omission worth a
> second look. `risk_manager_mode` is not forced at all — `assert_backtestable_risk_mode`
> refuses `smart` instead.
>
> **Remaining, none blocking:** F3 (the stale `get_provider` closure — still the reason the
> backend suite is order-dependent), F0 (the 52nd root-suite known-bad), the §2b live-only
> lifecycle gaps (recorded in `6b28e185`, not implemented), and V5/V4 which are `backtest_only`
> rows by design — carried so a deploy is explicitly different rather than silently different,
> and closable only by a live broker/engine change. The merger checklist in §8 still applies in
> full, above all the ONE `TEST_APP_VERSION` bump at a matrix3 job boundary.
>
> **Section 9 records what the fix commits did; nothing above it was rewritten.**

The branch's *code* stands up. The two things the operator was most worried about both came
back clean:

* **The open question is answered, and the answer is "the code is fine."** A TradeRule-shaped
  ruleset driven through `run_daily_backtest` — the GA's own `_trial_worker` entry point —
  **does** produce fills. The earlier 0-orders harness result was signal absence, not a
  handler defect. Now pinned by a test (§3).
* **Task 6's roll machinery is reached by BOTH runtimes**, verified hop by hop, and the
  `_entry_order` oldest-by-id fix is correct (§2c).

What blocks the merge is narrower and entirely mechanical: **Task 6 (`O_PMCC`, `718f7cf4`)
landed AFTER the branch's own final verification pass and broke 5 backend tests that were
never re-run.** All 5 are stale test-side expectations, not production defects. They are
listed with their exact fixes in §7 (F1, F2). They were left for the operator rather than
patched here because two of them are explicitly-labelled goldens, and the ground rules forbid
adjust-until-green.

Beyond that, the review found one **pre-existing** parity gap class that is a genuine
launch-readiness input (§2a, V3/V5/V2 in particular) and one pre-existing test-isolation
defect (§7 F3). Neither is caused by this branch.

---

## 1. What this review changed

| Commit | What |
|---|---|
| (merge) | `origin/dev` merged — clean, no conflicts. dev's side touched only `thetadata.py`, its provider test, and `testplatform/version.py`; this branch touched none of them since the merge base. `version.py` taken from dev verbatim (`TEST_APP_VERSION = "2026.09.0010"`). |
| `67141bbe` | NEW TEST: a TradeRule ruleset through `run_daily_backtest`, asserting fills (§3). |
| `9a126d13` | NEW TEST + golden: an option results-identity fingerprint for `O_LEAP` (§4). |

**CWD fix (mandate item 4) was already landed** — `test_option_grid_foundations.py:28-29`
already resolves `_LAUNCHER_PATH` off `__file__` (comment cites `dcd12237`). Verified passing
from both directories: 215 passed from `testplatform/backend`, 215 passed from the worktree
root. No commit needed.

No production code was changed by this review. Every mutation was restored by file copy and
`git status --short` was verified clean after each.

---

## 2. Parity audit

### 2a. Launcher-only behaviour (the operator's top concern)

The rule: a GA gene must land ONLY in an emitted ruleset parameter or an expert setting.
GA-level knobs (fitness, population, trade/breadth floors, schedule, universe, dates, warmup,
seed, caching) and market-simulation parameters (commission, slippage, spread, fill model —
the account simulating the exchange) are legitimately launcher-side.

**Everything the GA *searches* is clean.** All ~30 gene families — `model:*` (expert params +
the classic-RM sizing block + the regime overlay), `cond:<id>:value`/`:enabled`,
`entry|exit:<rid>:enabled`, `exit:<rid>:a<i>:action_value`, and the whole option-selection set
(`option_strike_param`, `option_strike_method`, `option_strike_delta[_long]`,
`option_structure`, `option_dte`, `option_wing_width`, `option_sizing`, `option_min_arc`,
`option_entry_cross`, `optsel:<half>:w_*`) — land in a ruleset action parameter or an expert
setting. `test_gene_to_artefact_audit.py` pins that direction across 31 case combinations with
per-gene sentinels, and the trial-config **whitelist drops nothing**: all five `decode_params`
outputs are consumed by `_build_daily_trial_config`. **No inert GA dimension was found.** The
rule-level `enabled: False` convention is fail-closed on both seeding paths — an authored-off
rule is *removed*, never emitted carrying a stale flag.

**What is NOT clean is the other direction** — behaviour the trial config carries that the
artefact does not. The audit test never asks that question, which is why none of this was
caught. Five findings, all **pre-existing** (none introduced by this branch's tasks), verified
against the code by a second pass:

| # | Finding | Where | Severity for a live deploy |
|---|---|---|---|
| **V3** | The handler **forces four trading permissions** onto every trial's expert, after the decoded overrides: `allow_automated_trade_opening=True`, `enable_buy=True`, `allow_automated_trade_modification=True`, `enable_sell=bool(enable_short)`. The interface defaults `allow_automated_trade_opening` and `allow_automated_trade_modification` to **False**. `TradeManager.py:2611` gates every open-positions action on `allow_automated_trade_modification` (`evaluator.execute(submit_to_broker=...)`). The launcher puts no permission key into expert settings, and `import_deploy_payload.py:120-122` sets only `allow_automated_trade_opening`, only for freshly created instances. | `daily_backtest_handler.py:1147-1156`; defaults `MarketExpertInterface.py:77-96`; gate `ba2_trade_platform/core/TradeManager.py:2611` | **HIGHEST.** A deployed genome evaluates its exits and creates them *pending, never submitted*. Every deploy runs live with no automated exits until someone ticks the box by hand. |
| **V5** | `hold_assigned_stock` is a per-strategy-key **launcher** decision. Only `O_WHEEL` gets `True`; for every other option key the backtest sells stock delivered by assignment (clamped to the unpledged excess, remainder re-queued). Live never does this — `AlpacaAccount.reconcile_option_assignments` opens the equity long and stops. | `ba2test_launcher.py:4440-4463`, written at `:5018`/`:5347`; consumed at `backtest_account.py:3823` (`daily_backtest_handler.py:497` is only the API passthrough) | **HIGH.** Backtested P&L for `O_CSP`/`O_JL`/`O_RS` and the wheel family assumes a de-risking action live will not perform. The handler's own comment says flipping it "changes WHICH STRATEGY the run represents". |
| **V2** | A hard-coded **$100 underlying-price cap** (`_MAX_STOCK_PRICE_DEFAULT = 100.0`), plus per-row caps ($300 for `O_SSTG`/`O_SSTD`, $100 for `O_IC`/`O_BF`/`O_LP`), enforced in the screener gate at `metric_store.py:1102`/`:1230`. It *is* exported (`backtests.py:1184-1195`, inside `universe.screener_settings`) and a live setting exists (`MarketExpertInterface.py:567`, `screener_price_max`) — but `tools/import_deploy_payload.py:119` writes only `settings.settings.expert_params` and **silently drops the `universe` block**. | `ba2test_launcher.py:3240`, `:3785-3798`, `:5143-5145`, `:6262`; rows `:2304`, `:2313`, `:2336`, `:2363`, `:2421` | **MEDIUM-HIGH.** The genome was selected on cheap names only; live it structures the same trades on $200-$400 underlyings where the per-contract reserve exceeds the sleeve. |
| **V4** | `entry_action` is a testplatform-only config key carrying an **undecoded template**; `daily_engine.py:411-417` derives a run-global `_entry_is_option` from it and `:1011-1015` submits option entries directly instead of staging an RM candidate. Live has no such key and stages *every* enter-market recommendation as an RM candidate (`TradeManager.py:1906-1934`). A second, sharper bug rides the same line: if the GA prunes **every** member rule of a group, `daily_backtest_handler.py:1026`'s guard (`entry_rules == [] and not buy_tree and not entry_action`) fails *because `entry_action` is truthy*, so `:1028-1030` re-arms the run with `members[0]`'s option action on a bare permissive gate — the exact silent re-arm that comment was written to prevent. | `ba2test_launcher.py:4169`, `:4569`; `strategy_optimization_handler.py:1851`, `:2032`; `daily_engine.py:411-417`, `:1011-1015`; `daily_backtest_handler.py:1026-1030` | **MEDIUM.** The RM-funding asymmetry is real. The empty-group re-arm corrupts both the GA fitness and the persisted top-N for that genome. (The `members[0]` concern *per se* is inert — the derived value is a boolean and every member is an option action.) |
| **V1** | The six `screener:*` genes reach a **per-bar entry universe filter** that lives only in the trial config (`screener_runtime`). | `ba2test_launcher.py:5137` + `:5185-5187` → `strategy_param_space.py:728` → `strategy_optimization_handler.py:1917-1936`, `:2048` → `daily_engine.py:601-606`, `:705` | **LOW for the option grids** — no options driver passes `--screener` (only `run_screener_capband_matrix.py:394` does), so this bites the equity cap-band grid. The live settings already exist under the exact same six names (`MarketExpertInterface.py:519-601`); as with V2 the break is in `import_deploy_payload.py`. |

**Common root for V1, V2 and half of V3:** `tools/import_deploy_payload.py` consumes only
`settings.settings.expert_params` and discards the `universe`, `execution` and
`execution_interval` blocks that `_derive_export_payload` (`backtests.py:1216-1255`) already
builds. One block in the importer closes most of it.

**Sub-claim explicitly REFUTED** (it would have been the worst finding, so it is recorded):
`enable_short` is indeed never set by either launcher command, but that flag is what *seeds*
the symmetric SELL entry rule (`daily_backtest_handler.py:1029` → `default_rulesets.py:283-284`).
With it off, no SELL-side candidate is ever created, so `_filter_orders_by_permissions` never
fires. The equity legs of `O_CC`/`O_PP`/`O_STK` are BUY orders and pass on `enable_buy=True`;
`O_WHEEL`'s entry is `O_CSP`'s short put, a pure-option entry. **No grid key is silently
crippled.**

**Secondary:** `test_gene_to_artefact_audit.py`'s `test_the_allowlist_is_exactly_this`
(`:662-668`) asserts the only exemption is `schedule:`, while `_check_gene`'s `screener`
branch (`:417-430`) accepts `trial["screener_runtime"]["settings"]` as a valid destination — a
second exemption that is *documented* (comment at `:96-99`) but **un-asserted**, so that test
cannot catch it. Also `strategy_optimization_handler.py:1990`
`.get("execution_interval", "1d")` — no shipped caller omits it, but a hand-written config
silently gets a daily fill clock instead of 5 min (latent trap, low severity). And
`strategy_param_space.py:544-551` swallows a malformed authored DTE window into `hw = 7` —
unreachable with shipped data (every authored window is int/int).

### 2b. The live-only option lifecycle pass — STANDING FINDING, not a blocker

`option_lifecycle_service.py` → `option_lifecycle.decide()` runs **live only**, by design.
Documented at `EXPERTS.md:164`:

> - **The exit/servicing pass is still LIVE ONLY**, by design: profit capture, tested-delta,
> roll-DTE and the stops run in `option_lifecycle_service`, while a backtest expresses the
> same exits as the strategy's own `close_option` rules, which the GA searches. Do not read a
> backtest as evidence about profit-capture/roll-DTE/tested-delta behaviour.

The one-line summary: **live = the shared rules path *plus* `decide()`; backtest = the shared
rules path only.** Live is a strict superset for exits, so a grid result systematically
*understates* how aggressively a live sleeve exits.

| Live behaviour | Live code | Backtest equivalent | Reproduced? |
|---|---|---|---|
| Circuit-breaker FLATTEN of the book | `option_lifecycle.py:1058-1062`; `option_lifecycle_service.py:358-368`, closed `:415-418` | transition IS shared (`daily_engine.py:1544-1564` → `OptionRiskManagement.update_sleeve_breaker:1161`); entry decline shared (`option_book.py:582-585`) — **the flatten is not** | **PARTIAL** — backtest trips and refuses new entries but never closes the book; it rides the drawdown live liquidates |
| Covered-call cover-lost close | `option_lifecycle.py:938-989`, `:1064-1069`; cover measured `option_lifecycle_service.py:522-710` | NONE (`grep cover_lost` over `testplatform/` = 0 hits) | **NO** |
| Profit capture (`profit_capture_pct`) | `option_lifecycle.py:1071-1079`; P&L `_pnl_pct:495-533` | `opt_tp` rule `ba2test_launcher.py:4077-4080` → `ProfitLossPercentCondition` `TradeConditions.py:1814` | **PARTIAL** — same arithmetic, **two implementations** (`_pnl_pct` says "mirrors"), different quote sources (chain map vs per-leg `get_option_quote`) |
| Strangle-specific capture (`strangle_capture_pct`) | `option_lifecycle.py:1072-1073` | NONE as a distinct knob — all credit kinds share one `opt_tp` band | **PARTIAL** |
| Credit-multiple stop (`dr_stop_*`/`ur_stop_*`) | `option_lifecycle.py:1081-1097` | `opt_sl` `:4117-4121`; `opt_sl_ml` `:4140-4144` → `LossPctOfMaxLossCondition` | **DIFFERENT** — live partitions by declared strategy tag (and carries a deliberate quirk: with `ur_stop` off, a naked structure has NO stop even when `dr_stop` is on); the backtest partitions differently and can have both live, with different denominators |
| **Tested-delta management** | `option_lifecycle.py:536-568`, `:1099-1105` | **NONE** — the only delta condition is `LongLegDeltaCondition` (`TradeConditions.py:3679`); no short-leg delta field exists in the registry | **NO — the hardest gap.** No ruleset can express it and the GA cannot search it |
| DTE roll/close, single-expiry | `option_lifecycle.py:1120-1122`, `roll_window_dte:571-606` (SHORT leg) | `opt_dte` `:4085-4089` → `DaysToExpiryCondition:3216` (**LONG** leg) | **PARTIAL** — identical on single-expiry; on a declared multi-expiry structure the two readers *deliberately* disagree (documented both sides) |
| PMCC overlay roll | `option_lifecycle.py:1113-1119`, `pmcc_roll_due:868-892` | `pmcc_roll_dte` `:3992-3996` → `ShortLegDaysToExpiryCondition:3560` → `RollPMCCShortAction:4875` | **YES via the rule** — and the live `decide()` branch is a **dead end**: `LIFECYCLE_ROLL_SHORT` is not in `LIFECYCLE_CLOSING_REASONS` and `option_lifecycle_service.py:397-418` handles only `UNKNOWN`/`COVER_LOST`/`should_close`, so live computes "roll" and silently discards it |
| PMCC buyback (`pmcc_buyback_pct`) | `option_lifecycle.py:218-228`, `credit_decay_pct:647-676` | `pmcc_roll_buyback` `:3997-4001` → `CreditDecayedPctCondition:3619` | **YES** — genuinely one implementation |
| Unknown / unmeasurable alarm | `option_lifecycle.py:1124-1130`; `LifecyclePassResult.unknown:196` | per-condition only (`_TwoExpiryLegCondition._unevaluable:3544-3548`) | **PARTIAL** — backtest has no aggregate; an unevaluable condition is a `False`, indistinguishable from "threshold not met". A trial cannot report how many bars it was blind |
| Close execution mechanics | `option_lifecycle_service._close:770-824` — **MARKET** order | `CloseOptionAction:5236-5409` — **LIMIT** + backtest-only spread concession `:5301-5347` | **DIFFERENT by design**; residual optimism recorded at `:5264-5269` |
| Wheel steps | NONE in the live pass (assignment reconciled in `AlpacaAccount.py:6489-6593`) | `_build_strategy_wheel` `:4703-4770`, conditions `TradeConditions.py:3768`/`:3836` | **N/A** — rules-only in both; runs live through the same evaluator |
| Expiry / assignment settlement | NONE in the live pass — OCC + broker | `daily_engine._apply_option_expiry:1455-1542` | **DIFFERENT** — backtest never exercises ITM longs, always assigns short ITM, models no early American assignment |

**Reverse direction — what the backtest does that live does not:** margin-call liquidation
(`daily_engine.py:1430-1433`, `:756-757`); `hold_assigned_stock` (V5 above); the exit-quote
concession; **per-bar cadence** (backtest walks OPEN_POSITIONS every bar, live on the
JobManager schedule); and **rule ordering effects** — `_insert_option_overlay(anchor="front")`
plus `continue_processing` make first-match order load-bearing in the backtest, while live's
`decide()` has its own fixed precedence ladder, so on a bar where profit-capture and roll-DTE
both hold the two runtimes record different reasons.

**Recommended follow-up, cheapest first:**
1. **Add a `short_leg_delta` condition.** The only live rule with *no* expressible backtest
   counterpart. The shape already exists (`_TwoExpiryLegCondition` + `option_lifecycle._tested`'s
   short-leg selection); follows the `credit_decayed_pct` precedent exactly. Biggest win per
   unit of work.
2. **Decide what `LIFECYCLE_ROLL_SHORT` is for.** Today live computes it and drops it. Prefer
   *deleting* the branch and letting the rule own the roll in both runtimes — that matches
   `RollPMCCShortAction`'s own "IT IS A RULE, NOT AN ENGINE HOOK" argument.
3. **Give the breaker trip an exit in the backtest** at the `update_sleeve_breaker` call site
   (`daily_engine.py:1550`), mirroring `option_lifecycle_service.py:415-418`. Without it,
   `EXPERTS.md:161`'s "one implementation, two callers" is true of the latch but not of its
   consequence.
4. **Unify the two P&L implementations** (`option_lifecycle._pnl_pct` and
   `TradeConditions._get_spread_pnl_via_transaction`).
5. Longer term: drive a shared `decide()` per bar from `daily_engine`, as the breaker
   transition already is.

Also worth recording: **no shipped grid spec sets `risk_manager_mode: classic_options`**
(`ba2test_launcher.py:1073-1083`, pinned by
`test_no_shipped_expert_spec_selects_a_risk_manager_mode`), so in current grid jobs the entry
rails and the breaker are inert too. That changes how to read every "both runtimes" claim in
`EXPERTS.md:158-163`.

### 2c. Task 6's roll — reached by BOTH runtimes (re-confirmed)

All five items live in `packages/common/ba2_common/` and are reached through the same two
shared spines: the ruleset walk (`TradeActionEvaluator` → `create_action` → `action.execute()`)
and the fill observation (`ReadOnlyAccountInterface.refresh_transactions`).

| # | Item | Live entry | Backtest entry | Verdict |
|---|---|---|---|---|
| 1 | `RollPMCCShortAction` (`TradeActions.py:4875`) | `WorkerQueue.py:1578` → `TradeManager.py:2358` → evaluator `:2531`/`:2554` → `:2611` → `TradeActionEvaluator.py:378` → `action.execute()` | `daily_engine.py:463` → `_manage_open_positions` `:712` (def `:1154`) → evaluator `:1230`/`:1233` → `:1242` → same `TradeActionEvaluator.execute` | **BOTH** |
| 2 | `CloseOptionAction` (`TradeActions.py:5236`) | identical chain; `forced_exit` set at `TradeActionEvaluator.py:1161` | identical chain | **BOTH** |
| 3 | Submit guard (`OptionsAccountInterface.submit_option_order:238`; multi-expiry invariant `:322-333`, cover `:343`) | Alpaca/Tasty **do not override** `submit_option_order` — only the broker hook `_submit_option_order_impl` (`AlpacaAccount.py:6122`) | `BacktestAccount.submit_option_order` (`backtest_account.py:2085`) is a 3-line `super()` passthrough; hook at `:3617` | **BOTH** |
| 4 | Fill-derived max-loss: `_restamp_declared_multi_expiry_max_loss:925` called from `refresh_transactions` at `ReadOnlyAccountInterface.py:1057` → `OptionRiskManagement.max_loss_from_fills:525` → `option_lifecycle.intrinsic_floor_per_contract:700` | `JobManager.py:724` → `TradeManager.py:222` → `account.refresh_transactions()` at `TradeManager.py:276`; no live account overrides it | **the ENGINE calls it**: `daily_engine.py:1375` (after `refresh_orders()`) and `:1451` (post-settlement) → `BacktestAccount.refresh_transactions` (`backtest_account.py:2950`), whose **first statement** is `super().refresh_transactions()` (`:2968`) | **BOTH** |
| 5 | `_entry_order` oldest-by-id (`TradeActions.py:4942-4954`) | via #1 | via #1; sibling `ReadOnlyAccountInterface.py:952-954` via #4 | **BOTH, correct** |

**`74be78a1` verified.** It removes `RollPMCCShortAction._restamp_max_loss` entirely, replaces
the submit-time write with a comment (`TradeActions.py:5134-5141`), and renames the result key
to `projected_max_loss_per_contract` (`:5149`) so **nothing is persisted at submit**. The
persisted value is written only in `_restamp_declared_multi_expiry_max_loss`, from
`entry.filled_qty or entry.quantity` and executed leg rows; `max_loss_from_fills` returns
`None` (leave the stamp alone) for an unreadable structure count, no executed leg, or an
executed leg with `open_price is None`. Unfilled tickets contribute nothing and the derivation
is idempotent. Equity does structurally zero work — one attribute read and one `is None` test
at `ReadOnlyAccountInterface.py:1055-1057`, pinned by
`test_an_equity_transaction_never_reaches_the_option_restamp`.

**`_entry_order` — correct.** Both copies use the identical key:

```python
return min(rows, key=lambda o: (o.id is None, o.id or 0)) if rows else None
```

A genuine `min` by id; the tuple key sorts `id is None` last rather than aliasing it to 0; the
added `not contract_symbol` filter stops a single-leg option order being read as a multi-leg
parent. Both copies agreeing matters because that row carries both `ORDER_PMCC_OVERLAY_KEY`
and `max_loss_per_contract` (the `loss_pct_of_max_loss` denominator).

**One gap worth a follow-up test:** the `ReadOnlyAccountInterface` copy *is* pinned (via
`_entry_stamp(parent)` re-fetching the original parent by id after a roll —
`test_pmcc_lifecycle.py:1370`, `:1375`, `:1485`, `:1518`). **`RollPMCCShortAction._entry_order`
has no test naming the ordering.** The nearest coverage
(`test_an_UNFILLED_roll_does_not_block_the_next_roll_FOREVER`, `:1447`) does exercise it with
two parentless rows, but `trade_store.orders_where` returns insertion/rowid order, so the
pre-fix `rows[0]` would have passed it too. A test that reverses or shuffles the returned rows
before selection would close it. (Listed as F5 below.)

---

## 3. The open question — RESOLVED, in favour of the code

**Question:** a standalone harness produced 0 orders driving a TradeRule-shaped ruleset
through `run_daily_backtest` (the GA's `_trial_worker` entry point), while the golden run uses
a lower-level path. Handler defect, or signal absence?

**Answer: signal absence. The handler is fine.** Three runs over the same hermetic fixture,
same expert (`FMPEarningsDrift`), same window, differing only in ruleset shape:

| Run | Ruleset shape | `total_trades` |
|---|---|---|
| A (control) | no `entry_rules` — the legacy default seeding path | **1** |
| B | TradeRule rows from the shared converter `trade_rules_from_legacy` | **1** |
| C | the launcher's own `S1` rules via `_build_strategy` → `decode_params` | **4** |

Committed as `testplatform/backend/tests/backtest/test_traderule_ruleset_through_run_daily_backtest.py`
(`67141bbe`). No existing test covered this arrangement: `test_grid2_engine_paths.py` drives
TradeRule rulesets but constructs `DailyBacktestEngine` by hand; `test_daily_engine_e2e.py`
goes through the handler but on the legacy path; `test_deploy_round_trip_parity.py` compares
decoded artefacts without running anything.

**The fills alone pin nothing — a mutation proved it.** Forcing `_is_trade_rules` to return
`False` left all three trade counts **unchanged**, because `_seed_enter`'s legacy arm also
accepts `entry_rules` (as `seed_ruleset_from_tree(entry_actions=...)`) and still fills. The
test therefore also spies on which of the four enter-seeding functions ran and with what
argument. Re-run under the same mutation: **tests B and C now FAIL**; restored by file copy,
tree clean, 3 passed.

---

## 4. The new option results baseline

No golden-style fingerprint existed for any option key. Added:
`testplatform/backend/tests/backtest/test_option_golden_run.py` +
`tests/backtest/golden/option_leap_golden_run.json` (`9a126d13`).

It runs the `O_LEAP` chain — the **launcher's own** emitted entry/exit rules via `_leap_rules`,
over a fixture chain at the ~50% LEAPS bar density — through the real
`DailyBacktestEngine.run()`, and pins every round-trip at full float precision *including* the
option-only columns (`contract_symbol`, `underlying_symbol`, `multiplier`) that the equity
fingerprint deliberately drops, **plus the whole 149-point equity curve**.

```
sha256        a28a414be4d1e0c9e5cd9b5ce9b393ce987c9004cb1bd39eb3d28382839e73e5
n_trades      1   (entry 2024-01-02 @ 21.25, exit 2024-04-29 @ 24.7115, pnl 692.30, 84 bars,
                   contract GOLDX250222C00080000)
final_equity  100692.3
curve         149 points, 53 distinct held-option equity values over the 84 held bars
```

**THIS IS A NEW BASELINE, NOT A PROOF OF IDENTITY WITH PRE-BRANCH BEHAVIOUR.** The equity
golden can claim identity because its harness was replayed against a pre-options reference
tree and compared. Nothing comparable is claimed here — the branch's own results-comparability
note already records a **baseline split on the Black-Scholes mark fallback**, so an option run
on this branch is *not* expected to reproduce a pre-branch number. This pin says only: from
here on, these numbers do not move without someone saying so.

Two fixture defects were found and fixed while building it, each now guarded by its own test:

* A period-14 premium wave against the ~42-bar hold put the exit on the wave's own zero phase,
  so entry and exit filled at the identical premium and **every P&L field was `'0.0'`** — a
  fingerprint that hashes stably while pinning nothing. Period is now 13.
  `test_the_golden_carries_a_NON_ZERO_round_trip` guards it.
* A flat underlying left the BS fallback returning one constant.
  `test_the_curve_actually_MOVES_while_the_option_is_held` guards it.

**Mutation-verified.** `option_bs.bs_price(...) * 1.000001` — a 1-part-per-million perturbation
— leaves the trade rows untouched and is caught **only** by the pinned curve. That is the
evidence that the BS fallback is genuinely reached and genuinely pinned. Restored by file
copy; 15 passed across the two goldens + `test_grid2_engine_paths.py` afterwards.

---

## 5. Suites

All run one at a time, on the merged tree, with the worktree-pinning `PYTHONPATH` verified
(`ba2_common`, `ba2_providers`, `ba2_experts` and `ba2test_launcher` all resolve inside
`BA2-options-grid2`). Free RAM 26-27 GB throughout.

| Suite | Result | vs the STATE note's baseline |
|---|---|---|
| backend `pytest tests/` (from `testplatform/backend`) | **4518 passed / 158 skipped / 12 failed**, 8m05s | was 4446/158/5 — **7 NEW failures**, all pre-existing on the branch (see below) |
| `packages/common` (from its own dir) | **3131 passed / 1 failed** (the known `test_portfolio_allocation_wizard` float-dust) | at baseline |
| `packages/experts` | **885 passed / 0 failed** | at baseline |
| `packages/providers` | **450 passed / 0 failed** | 447 + 3 from dev's thetadata test |
| root `tests/` (from the worktree root) | **4494 passed / 52 failed**, 6m05s | the known-bad set (`test_portfolio_allocation_page` 31, `test_option_intent_migration` 16, `test_tastytrade_account` 2, `test_broker_sdk_pins` 2 = 51) **+ 1** |
| `tests/backtest/test_equity_golden_run.py` | **3 passed** — fingerprint unmoved | at baseline |
| `tests/backtest/test_option_golden_run.py` (NEW) | **5 passed** | new |
| `tests/test_strategy_fitness_equity_frozen.py` | **809 passed / 5 failed** (the known `curve_uneven` cases) | at baseline |
| `tests/test_strategy_fitness_option_car.py` | **58 passed / 0 failed** | at baseline |
| `tests/test_strategy_fitness_convex_frozen.py` | **167 passed / 0 failed** | at baseline |
| `test_option_grid_foundations.py` from `testplatform/backend` / from the worktree root | **215 passed / 215 passed** | CWD fix confirmed both ways |

### The 7 new backend failures — all pre-existing, none caused by this review

**5 of them are Task 6 staleness** (`test_options2_matrix_script.py` ×3,
`test_launcher_iv_rank_gene.py` ×1, `test_launcher_volume_vol_genes.py` ×1). They fail in
isolation, without any of this review's files, and the cause is diagnosed statically: `O_PMCC`
was added to `_DEFAULT_STRATEGIES`/`_MIN_DTE`/`_OPTION_STRATS` and four dependent tests were
not updated. **F1/F2 in §7.** The STATE note's "no new failures" verification predates Task 6.

**2 of them are an order-dependent seam-wiring capture** (`test_seam_wiring.py` ×2). Controlled
comparison over `tests/backtest/`: **with** this review's two new files 2 failed / 1032 passed;
**without** them 2 failed / 1024 passed — the identical two. **F3 in §7.**

### The 1 extra root-suite failure

`tests/test_accounts/test_account_interface.py::TestTheExitGuardNetsSalesAlreadyInFlight::test_a_transactions_OWN_working_close_is_not_counted_against_itself`
— `sqlite3.OperationalError: no such table: accountdefinition`, a test-DB setup failure in
`tests/conftest.py:62`'s `reset_test_db`. **Reproduces in isolation** (1 failed / 110 passed),
so it is not a load flake. Neither the test file, `conftest.py`, nor `ba2_common/core/db.py`
was touched by this branch (`git log 56c3f8c2..HEAD --` on those paths is empty) — it arrived
with the `option-selection-modes` work already on `dev`. **Recommend adding it to the known-bad
list (making it 52) or fixing the fixture.**

---

## 6. Mutation spot-checks of earlier tasks' pins

Three earlier tasks' guards, re-verified by executing one mutation each. Every restore was by
**file copy**, and `git status --short` was clean after each (only this untracked report file).

| Task | Guard | Mutation applied | Named test | Result |
|---|---|---|---|---|
| 10 | fixed-delta strike-method set | dropped `"O_PMCC"` from `_FIXED_DELTA_METHOD_STRATEGIES` (`ba2test_launcher.py:2707`) | `test_the_pmcc_joins_the_debit_half_and_the_fixed_delta_method_set` (`test_option_grid_foundations.py:1827`) | **KILLED** — AssertionError at `:1831` |
| 12 | cap-binding return term | clamped the return term at the cap in `_option_convex` (`strategy_fitness.py:1417`), simulating a rank on the capped (deployed) equity series — the exact masking the pin exists to prevent | `test_a_capped_run_ranks_on_uncapped_pnl_not_on_the_capped_equity_series` (`test_strategy_fitness_convex_frozen.py:386`) | **KILLED** — AssertionError at `:397` (`big > 100.0`) |
| 13 | rule-level `enabled` guard | made an authored-off rule SURVIVE instead of being removed (`strategy_param_space.py:644-647` → `pass`) | `test_the_default_genome_carries_no_active_sl_ml_exit` (`test_convex_grid_foundations.py:316`) | **KILLED** |

After all three restores: `test_option_grid_foundations.py` +
`test_strategy_fitness_convex_frozen.py` + `test_convex_grid_foundations.py` = **430 passed**.

A **fourth** mutation, on this review's own new test, is recorded in §3: forcing
`_is_trade_rules` to `False` initially **SURVIVED**, which is what forced that test to be
strengthened with a seeder spy; it then killed tests B and C.

A **fifth**, on the new option golden (§4): `option_bs.bs_price(...) * 1.000001` — caught only
by the pinned equity curve, which is why the curve is in the fingerprint.

---

## 7. FIX-NEEDED list for the operator

Nothing here was changed by this review — every item needs either a production-code change or a
deliberate golden edit, both outside this review's mandate.

### BLOCKING the merge (5 failing backend tests, all test-side staleness)

**Root cause, shared by F1 and F2:** Task 6 (`O_PMCC`) landed as `718f7cf4` **after** the
branch's own final-verification pass (STATE note item 7) recorded "4446 passed / 5 failed, no
new failures". Adding `O_PMCC` to `_OPTION_STRATS` and to the matrix driver's
`_DEFAULT_STRATEGIES` invalidated four dependent tests, and the suites were never re-run. The
launcher's behaviour is **correct** in every case — `test_option_grid_foundations.py:1827`
(`test_the_pmcc_joins_the_debit_half_and_the_fixed_delta_method_set`) passes, confirming the
production classification. Only the tests are stale.

**F1 — `tests/test_options2_matrix_script.py`, 3 tests.** `_DEFAULT_STRATEGIES` is now
`["O_LEAP", "O_PMCC", "O_ERN", "O_CBS", "O_PBS"]` (`tools/run_options2_matrix.py:84`) and
`_MIN_DTE` now carries `"O_PMCC": 365` (`:89-100`, with the rationale: the PMCC's LONG leg is a
LEAPS, so it needs the same January-cycle depth).

* `test_the_chain_depth_thresholds_are_the_designs` (`:92-95`) — add `"O_PMCC": 365` to the
  expected dict.
* `test_the_preflight_runs_once_per_DISTINCT_threshold` (`:104-112`) — `sorted(groups[365])`
  becomes `["O_LEAP", "O_PMCC"]`. The docstring's "Three probes for four keys" becomes "for
  five keys" (still three distinct thresholds — the point of the test is unchanged).
* `test_the_job_list_is_exactly_this` (`:134-144`) — **this one is labelled "THE GOLDEN"**;
  insert `("opt2-FMPRating-O_PMCC", "FMPRating", "O_PMCC")` as the SECOND row, matching the
  strategy-major order of `_DEFAULT_STRATEGIES`. Confirm the ordering intent before editing.

**F2 — `tests/test_launcher_iv_rank_gene.py` + `tests/test_launcher_volume_vol_genes.py`, 2
tests.** Both hold an INDEPENDENT re-derivation of the debit/credit partition (deliberately not
read back from `_DEBIT_OPTION_MEMBERS`, so a mis-assignment cannot be self-consistent).
`O_PMCC`'s `action_type` is `"open_pmcc"` (`ba2test_launcher.py:2503`) and is absent from both
lists, so each test derives `">"` while the launcher correctly emits `"<"`.

* `test_the_two_halves_are_both_non_empty_and_disjoint_in_direction`
  (`test_launcher_iv_rank_gene.py:126`, list at `:136-145`)
* `test_the_two_halves_use_opposite_iv_rv_directions`
  (`test_launcher_volume_vol_genes.py:165`, list at `:168-175`)

Add `"open_pmcc"` to the `long_premium` set in BOTH, with the reasoning stated in the
re-derivation's own idiom: **a PMCC is a net-DEBIT diagonal — the LEAPS long dominates the
30-45-DTE short, so it wants the buyer's direction on both the IV-rank and the IV/RV gate.**
(Both files already carry exactly this note for the backspreads; follow that pattern.)

### NON-BLOCKING (pre-existing; none caused by this branch)

**F0 — one extra root-suite failure**, `test_account_interface.py::...::test_a_transactions_OWN_working_close_is_not_counted_against_itself`
(`sqlite3.OperationalError: no such table: accountdefinition`, from `tests/conftest.py:62`
`reset_test_db`). Reproduces in isolation; arrived with the `option-selection-modes` work
already on `dev`. Either fix the fixture or record it as the 52nd known-bad.

**F3 — the seam-wiring resolver captures a stale `get_provider` (test-isolation).** Two
`tests/backtest/test_seam_wiring.py` tests fail in a full `tests/backtest/` run and pass when
the file is run alone. **Reproduces in two files:**
`pytest tests/backtest/test_daily_engine_e2e.py tests/backtest/test_seam_wiring.py` →
`assert <FixtureOHLCVProvider> is <FMPOHLCVProvider>`.

**Not caused by this review's new tests** — controlled comparison: `tests/backtest/` with my
two files = 2 failed / 1032 passed; without them = 2 failed / 1024 passed, the identical two.
And none of `seam_wiring.py`, `test_seam_wiring.py`, `e2e_support.py`, `hermetic_providers.py`
or `test_daily_engine_e2e.py` was touched by this branch (`git log 56c3f8c2..HEAD --` on those
paths is empty).

**Mechanism, exactly:** `_wire_provider_resolver` (`seam_wiring.py:182-199`) does
`from ba2_providers import get_provider` and its `_resolve` closure **binds that function
object**. `wire_backtest_seams` is idempotent (`if _resolver is None`, `:152-156`), so if the
FIRST wire happens while `e2e_support.hermetic_providers` has `ba2_providers.get_provider`
patched to the fixture, the closure captures the FIXTURE permanently and is never rebuilt for
the rest of the process. `hermetic_providers` restores the module attribute correctly — the
closure is what is stale.

**Fix (one line, production):** resolve through the module attribute at call time —
`import ba2_providers` and `return ba2_providers.get_provider(category, name, **kwargs)` —
instead of the bound name. Benign in production (each GA worker wires once against the real
provider), but it makes the whole backend suite order-dependent, which is how the 5 stale tests
above stayed invisible for a day.

**F4 — the parity violations of §2a.** V3 (forced trading permissions, no artefact) is the one
to fix before any live deploy off this branch; V5, V2, V4, V1 follow. The common repair for
V1/V2/half of V3 is one block in `tools/import_deploy_payload.py:119-122`, carrying
`universe.screener_settings` + `instrument_selection_method` + the permission gates into
`expert_params` — the export side (`backtests.py:1184-1255`) already builds them and the live
settings already exist under the same names.

**F5 — `RollPMCCShortAction._entry_order` has no test naming its ordering** (§2c). The fix is
correct; it is simply unwitnessed on the action side, because `trade_store.orders_where`
returns insertion/rowid order and the pre-fix `rows[0]` would have passed the nearest existing
test. A test that reverses or shuffles the returned rows before selection closes it.

**F6 — `test_gene_to_artefact_audit.py` audits one direction only** (§2a secondary). It asks
"does every gene reach the artefact?" and never "does the trial config carry behaviour the
artefact does not?" — which is why the whole V1-V5 class was invisible. Its
`test_the_allowlist_is_exactly_this` also cannot catch the `screener` exemption its own
`_check_gene` grants.

---

## 8. MERGER CHECKLIST

1. **Land F1 + F2 first.** 5 stale tests, test-only edits, exact changes in §7. The branch is
   not clean-green until they are done.
2. **ONE `TEST_APP_VERSION` bump, at a matrix3 job boundary — NEVER mid-run.** Workers compare
   `TEST_APP_VERSION` alone to decide whether to self-update
   (`worker_client.py:ensure_synced`), so a mid-run bump fragments a running grid onto
   different code and silently breaks trial reproducibility. This branch touches only
   `packages/` and `testplatform/`, so it is `testplatform/version.py` that bumps, **not**
   `ba2_trade_platform/version.py`.
3. **`origin/dev` merge state: DONE and clean.** Merged during this review; dev's side touched
   only `packages/providers/ba2_providers/options/thetadata.py` (expiry-capped query window),
   its provider test, and `version.py`. No overlap with this branch since the merge base, no
   conflicts. `version.py` taken from dev verbatim at `TEST_APP_VERSION = "2026.09.0010"` — the
   merger's own bump goes on top of that number, so re-check dev has not moved again first.
4. **Option grids must be RETARGETED to start 2020-01-01 on ThetaData before any launch**, with
   provider parity pins and the option-cache optimisation in place. Memory note from the
   measured cold/warm work: cold load is **~3.6 s / 22 MB per symbol** on the 2024+ store and
   roughly **3x that at 2020**. Mitigations to have in hand before launching: raise
   `BT_MAX_TASKS_PER_CHILD` for option jobs (`strategy_optimization_handler.py:406`, default
   **8**) so the cold tax amortises over more individuals, and/or move to **one parquet per
   symbol**. The STATE note's amortised table stands: ~9.8 s/trial (~6%) on the local pool at
   the default recycle interval, ~1.3 s/trial (~0.8%) on the distributed path — real, but not
   a launch blocker by itself. The genuinely open perf question is the STATE note's own: does
   the parquet store hold RAW frames that get RE-FILTERED per bar, versus a pre-processed
   structure like the equity path's OHLCV preload + worker memo. Launch readiness on
   performance waits on that probe.
5. **`BT_BAR_CACHE_TRIALS=0` is the default** (`price_source.py:171`) and means *flush the whole
   bar cache every individual*. That costs equity grids hours of repeated preloads. Evaluate
   raising it for the next launch — it is one env var.
6. **Do not read a backtest as evidence about profit-capture / roll-DTE / tested-delta**
   (`EXPERTS.md:164`, §2b). If any option key is deployed live off this branch, the live
   lifecycle pass will exit more aggressively than the grid that selected it.
7. **Before any live deploy: fix V3** (§2a/F4). Otherwise the deployed instance evaluates its
   exits and never submits them.
8. **Every LIVE `O_CC` / `O_WHEEL` instance must be RE-EXPORTED and RE-IMPORTED** (added
   2026-09-03, §10). Those two keys gained a rule — `cc_dte`, which buys the written call back
   at a DTE floor — and `option_lifecycle.decide` simultaneously STOPPED closing a
   single-expiry structure at its expiry. A live instance whose stored ruleset predates this
   therefore has neither mechanism: the pass no longer closes its call and its ruleset has no
   rule that does, so the call runs to expiry or assignment unmanaged. This is a live-money
   step, not housekeeping. Run `tools/export_deploy_payload.py` for the backtest that
   currently backs each instance and `tools/import_deploy_payload.py` to push the regenerated
   ruleset onto it, then `POST /api/reload` to drop the instance and settings caches. Any
   PMCC-shaped instance also needs a `roll_pmcc_short` rule present — from 2026-09-03 the pass
   RAISES `UnownedRollError` when a two-expiry structure is due to roll and the ruleset has no
   rule to roll it (that is the intended loud refusal, not a regression).

### Parked operator items (carried forward, unresolved)

* **`OS2` = `[O_IC]` alone.** `O_CSP` (cash-secured put, also full-notional/neutral-ish) is
  excluded by the same naked-vol/full-notional filter that dropped `O_SSTG`/`O_SSTD`. Whether
  it should ever join `OS2` or stay standalone is an open design question —
  `ba2test_launcher.py` ~line 3705 carries the detail.
* **`classic_options` risk-manager rails have no UI path** — the settings dialog renders none
  of the sleeve rails. Compounding this: **no shipped grid spec sets
  `risk_manager_mode: classic_options`** at all, so in current grid jobs the entry rails and
  the sleeve breaker are inert.
* **The `option-selection-modes` stack (through `56c3f8c2`, this branch's base) is MERGED into
  `dev` but not DEPLOYED to any live `ExpertInstance`.** A separate, pending operator action,
  unrelated to this branch's merge status.
* **The live-only lifecycle pass** (§2b) — standing follow-up, five ranked steps, cheapest
  first being a `short_leg_delta` condition.
* **`run_screener_capband_matrix.py` default fitness** is `calmar_ratio`, not
  `consistent_annual_return`. Check any NEW bare invocation before comparing its numbers with
  the recorded matrix3/goal2020 jobs, which pass `--fitness` explicitly.

---

# 9. POST-FIX (2026-09-03) — the FIX-NEEDED items, implemented

Appended, not edited: everything above is the reviewer's findings as written, and stands. This
section records what was done about them. Same worktree, same `PYTHONPATH` pinning, one suite at
a time. Free RAM 22-27 GB throughout.

| Commit | Item | What |
|---|---|---|
| `f73d14bd` | §7 F1 + F2 | the 5 stale `O_PMCC` expectations — a JUSTIFIED refreeze, naming `718f7cf4` |
| `d1a604ef` | §7 F5 | `RollPMCCShortAction._entry_order` — the oldest-by-id rule, named |
| `dcc91137` | §7 F4 / §2a V1-V5 | ONE forced-settings table read by the handler AND the exporter; the importer stops dropping the universe block; the empty-group re-arm closed |
| `6b28e185` | §2b + §7 F3 | the lifecycle and seam-wiring follow-ups, recorded in the STATE note |

## 9a. What each item became

**F1 + F2 (`f73d14bd`).** Test-only, exactly the edits §7 specified. `_MIN_DTE` gains
`"O_PMCC": 365`; `sorted(groups[365])` becomes `["O_LEAP", "O_PMCC"]` (still THREE distinct
thresholds for five keys — the sharing IS the saving that test states); the labelled GOLDEN job
list becomes 5 rows = the 4 frozen before + `O_PMCC` in SECOND position, because `_jobs` yields
in `_DEFAULT_STRATEGIES` order and `O_PMCC` is not the event key. `"open_pmcc"` joins the
`long_premium` set in BOTH independent re-derivations, reasoned from the structure (a net-DEBIT
diagonal whose LEAPS long dominates the short financing it) rather than read back from
`_DEBIT_OPTION_MEMBERS` — which is the whole point of those lists. Both goldens carry a comment
naming `718f7cf4` as the commit that legitimately moved them.

**F5 (`d1a604ef`).** Two tests in `packages/common/tests/test_pmcc_lifecycle.py` that wrap
`trade_store.orders_where` to return its rows REVERSED — a return order it is entirely entitled
to produce, since it promises none. One states the ambiguity (two parentless option rows after
one roll, only the older carrying `ORDER_PMCC_OVERLAY_KEY`) and pins the selection under both
orders; the other pins the consequence, that a SECOND roll leaves the same contract on the book
either way. The base fixture stops after one roll cycle — and one roll is exactly the case where
`rows[0]` is still right — so a local `TwoCycleAccount` adds a third expiry tier.

**F4 / V1-V5 (`dcc91137`).** The repair is ONE table,
`packages/common/ba2_common/core/deploy_parity.py`, in `ba2_common` because three trees read it:
the testplatform handler, the testplatform export API, and `tools/import_deploy_payload.py`
(which runs against the LIVE checkout and has only the packages on its path).

| row | live setting | value | disposition |
|---|---|---|---|
| `allow_automated_trade_opening` | same name | `True` | carried + applied |
| `enable_buy` | same name | `True` | carried + applied |
| `allow_automated_trade_modification` | same name | `True` | carried + applied — **V3, the severe one** |
| `enable_sell` | same name | `bool(enable_short)` | carried + applied |
| `hold_assigned_stock` | **none** | the run's flag | carried under `backtest_only`, RECORDED not applied (V5) |
| `entry_action` | **none** | the template | carried under `backtest_only`, RECORDED not applied (V4) |

Each `live_setting is None` row carries a `why_no_live_analogue` string the round-trip test
quotes, so accepting the gap stays a deliberate act rather than an omission.
`hold_assigned_stock` cannot be closed without changing
`AlpacaAccount.reconcile_option_assignments` — a live BROKER class, out of mandate and an
operator decision. `entry_action` has no live key at all (`TradeManager` stages EVERY
enter-market recommendation as an RM candidate), so carrying it as an APPLIED setting would be a
lie.

V1 + V2 were the same root §7 F4 diagnosed: `live_settings_from_universe` maps
`universe.screener_settings` onto the identically-named live settings plus
`instrument_selection_method="screener"`, and the importer now consumes the block it used to
drop. A static universe maps to nothing, deliberately — its symbols are a candidate list, not a
setting, and overwriting a live instance's `enabled_instruments` from a backtest is a much bigger
decision than carrying the screener config.

V4's sharper half is fixed in production: `_seed_enter`'s guard read
`entry_rules == [] and not buy_tree and not entry_action`, and `entry_action` is always truthy on
an option run, so a genome that pruned every entry rule was re-armed with the run's option action
on a bare permissive gate. The empty list is the genome's decision; it now wins, and the run says
so at ERROR level.

`test_deploy_round_trip_parity.py` went 8 -> 16 passed. Its new pin calls
`daily_backtest_handler._build_experts` FOR REAL against a temp backtest trading DB and reads the
persisted `ExpertSetting` rows back, so "what the backtest engine received" is OBSERVED, not
re-derived. It also asserts the table's MEMBERSHIP explicitly — without that, a test iterating
the table it is checking would pass vacuously when a row is deleted, which is the very defect.

**No live-account or broker class was touched.**

## 9b. Mutations executed for the post-fix work

| # | Mutation | Named test it killed | Result |
|---|---|---|---|
| 1 | `RollPMCCShortAction._entry_order` -> `rows[0] if rows else None` | both new ordering tests | **KILLED both**; all 104 pre-existing tests in that file still PASSED — §2c's "the pre-fix `rows[0]` would have passed it too" reproduced exactly |
| 2 | drop the `allow_automated_trade_modification` row from the table | `test_every_forced_setting_reaches_the_deployed_instance` | **KILLED**, both parametrized cases |
| 3 | restore the importer's universe-block drop | `test_the_import_tool_still_consumes_the_universe_block` | **KILLED** |
| 4 | restore `and not entry_action` in `_seed_enter`'s guard | `test_config_entry_rules_explicitly_empty_is_NOT_re_armed_by_a_run_level_entry_action` | **KILLED** |

Every restore was by **file copy**, with `git status --short` verified clean afterwards.

## 9c. Suites — post-fix (the §5 table, re-run)

| Suite | Post-fix | §5 |
|---|---|---|
| backend `pytest tests/` (from `testplatform/backend`) | **4532 passed / 158 skipped / 7 failed**, 7m50s | 4518 / 158 / 12 |
| `packages/common` (from its own dir) | **3133 passed / 1 failed** (the known `test_portfolio_allocation_wizard` float-dust) | 3131 / 1 |
| `tests/backtest/test_equity_golden_run.py` + `test_option_golden_run.py` | **8 passed** — BOTH fingerprints unmoved | 3 + 5 |
| `tests/backtest/test_deploy_round_trip_parity.py` | **16 passed** | 8 |
| `packages/common/tests/test_pmcc_lifecycle.py` | **106 passed** | 104 |
| `tests/test_options2_matrix_script.py` | **17 passed** | 14 passed / 3 failed |
| `tests/test_launcher_iv_rank_gene.py` + `test_launcher_volume_vol_genes.py` | **284 passed** | 282 passed / 2 failed |

**The 7 remaining backend failures, itemised.** The 5 stale `O_PMCC` failures are GONE. Left:

* **2 × `tests/backtest/test_seam_wiring.py`** — §7 F3, **pre-existing**, unchanged. The
  production one-line fix (resolve `get_provider` through the module attribute at call time) was
  deliberately NOT taken: production change, outside the FIX-NEEDED mandate. Now recorded as
  follow-up **D** in the STATE note. Expect these two in any full backend run.
* **5 × `tests/test_strategy_fitness_equity_frozen.py::…[curve_uneven|*]`** — the STATE note's
  own pre-existing baseline, listed in §5 as "809 passed / 5 failed (the known `curve_uneven`
  cases) — at baseline". They surface in the full run because it includes that file.

§7 F0 (`test_account_interface.py::…OWN_working_close…`) is a ROOT-suite failure, untouched by
this work; it still wants either a fixture fix or a place on the known-bad list.

## 9d. What was NOT done, and why

* **§2b / §1b lifecycle follow-ups** — recorded, not implemented, by mandate. They are now a
  ranked operator task list in
  `docs/superpowers/plans/2026-08-30-option-program-review-STATE.md`. Read **A1** first: live
  computes `LIFECYCLE_ROLL_SHORT` and silently discards it. That is a live-money behaviour
  decision, not a tooling change.
* **§7 F3** — the seam-wiring fix: production, out of mandate, recorded as follow-up **D**.
* **§7 F0** — the root-suite `accountdefinition` fixture failure: pre-existing, out of scope.
* **§7 F6** — `test_gene_to_artefact_audit.py` still audits one direction only. The *other*
  direction is now pinned for the DEPLOY path specifically, by the forced-settings table and its
  round-trip test — which is the path that made V1-V5 invisible. Broadening the audit test itself
  remains open.
* **No `version.py` bump.** Merger checklist item 2 still owns it — and note `dcc91137` touches
  `packages/`, so it is `testplatform/version.py` that bumps, at a matrix3 job boundary and never
  mid-run.

---

## 10. POST-MERGE-READY ADDENDUM (2026-09-03) — the §1b/A1 follow-up, implemented

**Nothing above this section was rewritten.** The reviewer's findings stand as written; this
records what was built on top of them, under a new operator rule: *no silent failure anywhere
— a computed decision must be acted on or refused loudly, never dropped.*

### 10a. A1's premise was narrower than recorded, and the fix is not the one recommended

`LIFECYCLE_ROLL_SHORT` is emitted **only** for a transaction tagged `pmcc`:
`is_multi_expiry_strategy` is membership in `frozenset({PMCC_STRATEGY})`
(`option_expiry.py:90-114`), fail-closed for `None`/`""`/anything unrecognised. A stock-covered
call is `covered_call` — single-expiry — so it took the `elif` and produced
`LIFECYCLE_ROLL_DTE`, a CLOSING reason the pass acted on. **No covered call, wheel or other
non-PMCC structure could reach the discard.** The recommendation in §2b (delete the branch) was
therefore not enough on its own: deleting it leaves a PMCC deployed without a roll rule
silently unrolled — the same failure class, moved one layer out.

### 10b. What shipped

| Commit | What |
|---|---|
| `bfd58fcc` | DESIGN NOTE: the nine-row `decide()` reason table, the per-key backtest counterparts, three candidate fix shapes. |
| `95a0ed36` | **The dispatch is TOTAL.** `LIFECYCLE_DISPOSITIONS` names what happens to every reason (close / report / rule-owned / no action); the loop is driven off it and a reason with no entry RAISES. The `if not decision.should_close: continue` fall-through is gone. `ROLL_SHORT` is recorded (`roll_due`) and never rolled here; `UnownedRollError` is raised — after everything else is acted on — when a two-expiry structure is due to roll and the ruleset carries no `roll_pmcc_short` rule. Shared, not re-derived: `rule_builders.action_type_of`/`rule_carries_action` and `db.ruleset_event_actions` (the ordered eager load `TradeActionEvaluator` already needed; it now calls it too). |
| `07514a05` | **`covered_call_days_to_expiry` + `close_option(close_target='covered_call')`**, both resolving through `trade_repository.held_covered_calls` — one lookup, netted over executed rows, `covered_call`-tagged only. A parameter rather than a second action, so there stays ONE close implementation. |
| `4e33a620` | **`O_CC`/`O_WHEEL` emit `cc_dte`** at the front of their exit list, plus the engine e2e that is its pin. |
| `5427aca3` | **`decide()` stops emitting `LIFECYCLE_ROLL_DTE`** — constant, `LIFECYCLE_CLOSING_REASONS` membership and branch deleted. The `opt_dte` rule owns that exit in both runtimes. Plus the parity test. |

### 10c. The finding this work produced, and it generalises

**An option exit condition anchored on the evaluated TRANSACTION is inert for a stock-anchored
overlay key.** `daily_engine._manage_open_positions:1230` and the live
`TradeManager.process_open_positions_recommendations` it mirrors both evaluate the
OPEN_POSITIONS ruleset once per SYMBOL with `existing_order = _oldest_entry_order(txns)`. On
`O_CC` (and on `O_WHEEL` after assignment) that is the STOCK, while `SellCoveredCallAction`
writes the call on its own transaction — so `DaysToExpiryCondition._resolve_expiry` finds no
option legs, is unevaluable, and **never fires in either direction while carrying a searched
gene**.

Measured, not argued: the first version of `tests/backtest/test_covered_call_engine.py` emitted
a plain `opt_dte` rule on `O_CC`, and the run was identical to the run with the rule DELETED —
`exit_time 2024-02-06, exit_price 0.0` both times, the call expired worthless. **Overlay keys
must use repository-resolved conditions.** Recorded in the STATE note as standing.

### 10d. Mutations executed

| Mutation | Result |
|---|---|
| Drop `LIFECYCLE_ROLL_SHORT` from `LIFECYCLE_DISPOSITIONS` | **5 FAIL**, led by the reflection pin `test_every_lifecycle_reason_has_a_disposition` |
| Re-add the silent `if not decision.should_close: continue` | **5 FAIL** (the roll is dropped again; the no-disposition raise stops firing) |
| Remove `cc_dte` from `O_CC`'s emitted ruleset (in-test, `_run(drop_dte=True)`) | The written call **rides to expiry at price 0.0** — pre-branch behaviour, restored |

Each file mutation restored by file copy; `git status --short` clean after each. Both decider
mutations were re-run on the FINAL table (after the roll-DTE deletion), not only on the interim
one.

### 10e. Suites, post-merge with `origin/dev` (`bfb19508`, `TEST_APP_VERSION 2026.09.0013`, no bump)

* backend `pytest tests/`: **4558 passed / 158 skipped / 7 failed** — the 7 are EXACTLY the
  known pre-existing set (2 `test_seam_wiring` F3 + 5 `curve_uneven` frozen-baseline).
  A second full run of the SAME tree reported 8, the extra being
  `test_dashboard_hot_path.py::test_dashboard_stats_route_never_touches_the_blobs` — a test
  `origin/dev` brought in (`b46a2433`), which PASSES in isolation, passed in the first full
  run of this identical tree, and passes when run alongside this branch's own new backend
  tests. It is a new instance of the suite's known ORDER DEPENDENCE (F3), from dev's side, not
  a regression of this work. Worth adding to the known-bad list or fixing with F3.
* `packages/common`: **3161+ passed / 1** (the known float-dust flake).
* Goldens **18, both fingerprints unmoved** — and no refreeze was needed. That is arithmetic:
  the equity golden runs an equity key, the option golden (`9a126d13`) is `O_LEAP`, and
  `cc_dte` is emitted only for `_COVERED_CALL_OVERLAY_KINDS = {O_CC, O_WHEEL}`, neither of which
  has a golden. `O_CSP`/`O_PP`/`O_PMCC` emit byte-identical rule lists (checked by execution).
* `test_option_grid_foundations.py` **215 from the worktree root** as well as from `backend/`.

### 10f. What this does NOT close

B1 (short-leg delta), C1 (breaker flatten), C3/C4/C5, C7-C10 and D are untouched and still
ranked in the STATE note. `EXPERTS.md:164`'s warning narrows but does not disappear: roll-DTE is
now genuinely shared, and profit-capture / tested-delta / the credit stops remain live-only.
