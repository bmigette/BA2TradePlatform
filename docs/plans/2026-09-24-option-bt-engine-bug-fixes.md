# Option Backtest Engine Bug Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the four option-backtest engine and fitness bugs found on 2026-09-24, plus the dead weekend
schedule genes, so that stage 1 of the option grid can be relaunched from scratch on numbers that mean what
they say.

**Architecture:** every fix sits on the OPTION path of the testplatform backtest (`BacktestAccount`,
`DailyBacktestEngine`, `results.py` / `intraday_drawdown.py`) or on the option fitness. Equity runs must stay
byte-identical, and the existing equity golden runs prove that. Each bug gets a failing regression test
first (TDD), then the smallest fix. Finish with the full suites, the equity no-impact gate, a 1-year perf
gate on remote227, then the relaunch.

**Tech Stack:** Python 3.11 (local) / 3.13 (remote227), pytest, SQLite, the parquet/ThetaData option store.

**Source of truth for the bugs:** `docs/findings-2026-09-24-deterministicscorer-bearish-options.md` §5.1
(copied from the main checkout; the probes that reproduced them are
`test_files/probe_olp_funnel_20260924.py` and `test_files/probe_olp_report_20260924.py`).

**Worktree:** `C:\Users\basti\Documents\dev\BA2-optbugs`, branch `fix/option-bt-engine-bugs`, based on
origin/dev `bba4e0bf`.

---

## Ground rules (read before any task)

- **Test commands.** Run the three suites as separate invocations and never concurrently:
  - `packages/common/tests`: from the worktree, `..\BA2TradePlatform\.venv\Scripts\python.exe -m pytest packages/common/tests -q -p no:cacheprovider`
  - `testplatform/backend/tests`: run from `testplatform/backend`, because its `pytest.ini` sets `pythonpath`.
  - the root `tests/`.
- **Baseline.** Before changing anything, run the full unfiltered suites and record the baseline failures.
  Filtered runs have missed regressions before.
- **Equity no-impact is a hard gate.** `testplatform/backend/tests/backtest/test_equity_golden_run.py` and
  `test_engine_golden_regression.py` must stay byte-identical. Option goldens (`test_option_golden_run*.py`)
  MAY change, but only for a reason you state in the commit and the task report.
- **Standalone backtest scripts** must call `logging.disable(logging.INFO)`.
- **No broad `except` that swallows a computed decision.** A refused or unknown value must be loud. See the
  memory note "No silent failure anywhere".
- **Versioning.** Bump `testplatform/version.py` `TEST_APP_VERSION` by 1 in the final commit before the push
  (0088 → 0089). All changes are under `testplatform/` or `packages/`.
- **Commits.** One commit per task, message `fix(option-bt): …`, ending with the Co-Authored-By /
  Claude-Session trailer.

---

### Task 0: Baseline + investigate post-split option data

**Files:** none changed. Write the notes into the task report.

**Step 1.** Run the three full suites on the untouched worktree. Record the pass/fail counts and the failing
test ids (the known baseline is the two `test_no_zero_coercion.py` failures in the root suite).

**Step 2 — find out what the option store holds for a contract after its underlying splits.** Use ThetaData
through the store the grid uses (`BACKTEST_OPTIONS_STORE=thetadata`). Take:

- `AAPL200918P00400000` (AAPL split 4:1 on 2020-08-31);
- `ANET241220P00370000` (ANET split 4:1 on 2024-12-04).

For each, answer:

- (a) Does the OLD symbol still have daily bars after the split date, and are its prices pre-split
  equivalent (about 4× the post-split contract's)?
- (b) Is there an adjusted contract at strike/4 with bars?
- (c) What does `BacktestAccount._option_positions_mtm` use for a lot whose symbol has no bar that day?
  (BS fallback / entry premium — read `backtest_account.py:798-900`.)

The answers pick the Task 1 approach:

- **If the old symbol keeps pre-split-equivalent bars:** only the SPOT (intrinsic, no-arb bounds, expiry
  settlement) is wrong. Task 1 converts spot into the lot's own basis.
- **If the old symbol has no bars after the split:** the lot must be valued in its own basis everywhere
  (spot, BS fallback, intrinsic), and exits must still be able to fill. Say how a sell-to-close finds a price.

---

### Task 1: An option held across a split is valued in its own basis

**Problem.** A lot is valued against the spot in the basis of the CURRENT bar.

- **Where:** `backtest_account.py:3913` `_option_spot_asof` and `:3888` `option_basis_price`, plus expiry at
  `daily_engine.py:1664`, which converts with the settlement day's factor.
- **Mechanism:** after a split the factor is 1, so a pre-split strike is compared with a post-split spot.
- **Effect:** puts gain and calls lose by the split ratio. For example, `AAPL200828P410` was booked at
  +$27.7k but really expired worthless.

**Rule.** A contract's strike is in the basis of the day it was TRADED. Its spot is
`adjusted_close(t) × as_traded_factor(entry_day)`: the adjusted series expressed in the entry day's share
basis. For a lot that never crosses a split this equals today's conversion, so nothing changes for it.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py`:
  - `_OptionLot` (`:240`): add `basis_date: Optional[date]`, set when the lot OPENS and kept on adds.
  - `_option_spot_asof`, which gains an optional basis date.
  - Every intrinsic/no-arb/margin/expiry read that takes a lot's spot. Enumerate them with
    `grep -n "_option_spot_asof\|option_basis_price" backtest_account.py daily_engine.py`.
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py:1664` and the combo branch above it.
  Convert with the position's basis date, not the settlement day.
- Test: `testplatform/backend/tests/backtest/test_option_split_crossing.py` (new).

**Step 1: Write the failing tests.** Build them on the existing option-account fixtures; see
`test_option_expiry.py` and `test_option_unit_settlement.py` for how a `BacktestAccount` with a synthetic
option provider, price source and split basis is constructed. Cover:

1. **Long put, 4:1 split, really OTM.** Underlying $400 pre-split. Put strike 410 opened 5 days before the
   split, expiring 3 weeks after it. The adjusted spot is $110 post-split (= $440 pre-split basis, above
   410). At expiry: `close_reason` `expired_otm`, P&L = −premium paid.
2. **Long call, 4:1 split, really ITM.** Strike 300, pre-split spot $400 (post-split $100). The adjusted
   close at expiry is $110, i.e. $440 in the contract's basis. Settled at intrinsic (440 − 300) × 100 × qty
   when no expiry bar exists.
3. **Daily mark across the split.** The lot's MTM on the bar after the split equals the pre-split basis
   valuation, not the split-distorted one. This one depends on Task 0 (c): no jump in equity on the split
   bar beyond the real price move.
4. **Control.** A lot that does NOT cross a split produces exactly the same numbers as before (assert
   equality with the pre-fix path).

**Step 2:** run and verify tests 1-3 FAIL for the right reason (fake intrinsic), and test 4 passes.

**Step 3:** implement per the rule and Task 0's findings.

**Step 4:** run the new tests, the whole `testplatform/backend/tests/backtest/` folder, and the split-basis
suites (`packages/common/tests/test_split_basis*.py`, `test_option_resolve_split.py`). Expected: pass.

**Step 5:** commit.

---

### Task 2: A held option position counts as activity

**Problem.** `daily_engine.py:818` `_has_activity` checks `self.account.get_positions()`, which does not
include option lots.

- **Effect:** an option-only book with no working order jumps from entry day to entry day (`:806`). Exits
  are never evaluated on the days in between, the equity curve is sampled only on entry days, and expiry
  settles late.
- **Parity break:** live evaluates open positions Mon-Fri while the deployed 8082 genome enters
  Mon/Tue/Fri.

**Files:**
- Modify: `daily_engine.py` `_has_activity` (`:818-840`). Also return True when the account holds any open
  option lot (`self.account._option_positions`, or a public accessor if one exists; prefer adding
  `BacktestAccount.has_open_option_positions()` to reaching into a private dict).
- Test: `testplatform/backend/tests/backtest/test_option_activity_stepping.py` (new).

**Step 1: failing tests.**

1. **Option book with a Monday-only entry schedule.** A long call entered Monday with an exit rule
   `days_opened > 1`. It must close on WEDNESDAY's bar, not the next Monday. Assert the exit date, and that
   the equity curve has Tue/Wed/Thu points while the lot is open.
2. **Expiry on a non-entry day.** A contract expiring on a Friday under a Monday-only schedule settles ON
   that Friday, with that Friday's spot.
3. **Equity no-impact.** An equity-only run's visited-bar sequence is unchanged. Assert on a small
   synthetic run, or rely on the golden run in Step 4 and say so.

**Step 2:** verify tests 1-2 fail (exit on the next Monday / settlement late).

**Step 3:** implement.

**Step 4:** run the backtest folder plus `test_equity_golden_run.py` and `test_engine_golden_regression.py`.
They must be byte-identical. Option golden runs MAY change: re-baseline them only after confirming each
diff is a newly evaluated non-entry-day exit or settlement, and list the counts in the commit message.

**Step 5:** commit.

**Perf note:** option trials will now step daily while holding, like equity runs already do. The Task 6 perf
gate measures the cost. It is behaviour-driven, and the user accepted such costs before (2026-09-23).

---

### Task 3: The intraday drawdown refinement measures a dip, not a realised gain

**Problem.** `intraday_drawdown.py:158-164` sets `extra_loss = worst_pnl − realised_pnl` and adds
`extra_loss / equity_at_entry` to the RUN's max drawdown.

- **Effect:** for a winning trade the realised gain is counted as drawdown. The result is −100% on
  profitable runs (the bust sentinel at `strategy_fitness.py:1778`), and every refined option drawdown is
  inflated. The O_LC TOP1 shows −34.3% where the curve says −25.5%.

**Rule.** A trade's intraday dip is
`dip_dd = (equity_at(entry) + min(0, worst_pnl)) / peak_at(entry) − 1`, in percentage points. It uses the
running equity peak up to the entry, taken from the same equity curve. The refinement returns
`min(max_drawdown, min over trades of dip_dd)`. It still only ever makes the figure worse, and it stays
floored at −100%.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/intraday_drawdown.py` `refine_max_drawdown`. Add an
  injected `peak_at` callable next to `equity_at`.
- Modify: `testplatform/backend/app/services/backtest/results.py` `_build_refine_drawdown_fn` (`:242`).
  Build `peak_at` from the equity curve.
- Test: extend the existing intraday-drawdown tests (`grep -rln refine_max_drawdown testplatform/backend/tests`).

**Step 1: failing tests.**

1. **A winning trade.** Realised +$51,048, `worst_pnl` −$500, entry equity $11,156, peak $12,000. The
   refined figure is at most the dip `(11156 − 500)/12000 − 1 = −11.2%` combined with the daily max,
   NOT −100%.
2. **A losing trade whose intraday low was worse than its realised loss** still deepens the drawdown by the
   correct amount.
3. **No flagged trades** → returns the input unchanged.

**Step 2:** verify tests 1-2 fail on the current formula.

**Step 3:** implement.

**Step 4:** run the tests, plus a re-computation check. On the local copy of an O_LC run, the refined DD
must now be ≤ the daily DD and close to it (unless a genuine intraday dip exists). Report
TOP1 −25.49% (daily) vs the new refined value.

**Step 5:** commit.

---

### Task 4: A 1-trade genome no longer escapes the concentration screen

**Problem.** `strategy_fitness.py:1043` `robustness_metrics` returns all factors 1.0 when `len(pnl) < 2`.

- **Effect:** a single-trade run has 100% concentration by definition, yet it is scored as perfectly
  diversified, while 2-5-trade runs get concentration factor 0. Under `option_car_target_soft30` every top
  O_LP genome therefore had exactly 1 trade.
- **Decided design (user, 2026-09-23):** thin trading is PENALISED, not zeroed. So this is NOT a new hard
  trade floor.

**Rule.** With exactly 1 trade and positive net P&L: `top1_pct = top5_pct = 100` and the concentration
factor comes from the same formula as every other run (it gives 0 above `_CONC_DEAD_PCT`). With 0 trades or
net ≤ 0, keep the current early return.

**Equity no-impact check (corrected 2026-09-25):** the equity CAR metrics (`car`/`goal`/
`consistent_annual_return`) do return `LOW_TRADE_SENTINEL` below their trade floor (12/yr, 8/yr for
DeterministicScorer) before robustness is applied, and `robust_fitness` passes a sentinel through. But the
generic metrics (calmar, sharpe, total_return, sortino, profit_factor, sqn, win_rate) have NO trade floor,
and robust fitness is on by default since 2026-09-17, so a 1-trade equity run on one of them DOES reach the
screen (`test_strategy_fitness_equity_frozen` pins `single_trade` -> all factors 1.0). That is why the change
is gated to the option CAR-family metrics: `compute_fitness`'s three option branches pass
`option_structures=True` and nothing else does. `robustness_metrics` has one caller, `robust_fitness`, whose
one caller is `_maybe_robust` inside `compute_fitness`.

Under the same flag the screen reads per-STRUCTURE P&L (`_structure_pnls`), not per-row leg P&L: rows are
legs, so any run of <= 5 rows read top5 = 100%, and offsetting legs pushed top1 past 100%. The MC resample
draws structures too (legs of one bet are not independent draws). A share that is 100% by definition (one
structure, or top5 of <= 5 structures) is set to exactly 100.0, because float division gave
99.99999999999999 and a factor of ~3.6e-24 that outranked an exact 0.

**Share lots + overlays (decision 2026-09-25).** The order code books an O_CC / O_PP overlay as its own
transaction and assigned stock as a new equity transaction, so on the transaction partition one position
became two offsetting "bets" (O_PP, 12 cycles of shares +1000 / put −600: top5 = 104% → factor 0, where 12
combined bets are 41.7% → 0.959). The option metrics' partition (`_option_structure_groups`) therefore also
joins a share lot with an option structure on the same underlying when their holding windows overlap for a
positive length, or when the option was settled INTO the lot (assigned/exercised, closed on the bar the lot
opened, lot opened at its strike). Joins are transitive (union-find): one bet per share-holding window, so a
wheel (CSP → assignment → covered calls → called away) is one bet. The option CAR-family trade gates (soft30
ramp and the legacy per-year gates) count the same partition, so the ramp and the screen agree; the default
partition (equity metrics, `option_convex`) is unchanged. Known limitations:
- windows that merely TOUCH (close both legs and re-open both on the same bar) are deliberately not joined,
  or every cycle of a run would chain into one bet; only a settlement at the strike hands one position on;
- a share lot held across many overlay cycles is ONE bet, so a buy-and-hold covered-call book counts as few
  bets on the trade gate as well as on concentration;
- summed `pnl_pct` is approximate for rows that open later than their structure (an overlay written on a lot
  already held, a rolled PMCC short), since each row is relative to equity at its own entry;
- this is the FITNESS grouping only; `tools/genome_concentration_check.py` uses it for option-metric runs
  (`scores_option_structures`) so the deploy-time check agrees with the GA. Order/ledger code is unchanged.

**Files:**
- Modify: `testplatform/backend/app/services/strategy_fitness.py` `robustness_metrics`.
- Test: `testplatform/backend/tests/test_strategy_fitness_*` (add to the file covering robustness; find it
  with `grep -rln robustness_metrics testplatform/backend/tests`).

**Steps:**
1. A failing test: a 1-trade positive run has `conc_factor == 0.0` and `top1_pct == 100`.
2. A 1-trade run's soft30 fitness is ≤ the 2-trade run's.
3. Implement, run the tests, commit.

---

### Task 5: Weekend schedule genes can never fire

**Problem.** `strategy_param_space.py:76` `SCHEDULE_DAYS` includes saturday/sunday, and the repair at
`:945-949` only fires when ALL days are off. A weekend-only genome gets 0 decision points in a daily-bar
backtest (for example the 0-trade sample in job 2).

**Rule.** The repair fires when no WEEKDAY is on: it forces monday on and leaves the weekend flags as they
are. Removing the weekend genes outright is the cleaner fix, but check first whether anything else reads
them: the live schedule export and deploy (`tools/import_deploy_payload.py`, `schedule_days`). If removing
them touches the export or deploy path, keep the genes and only fix the repair.

**Files:** `strategy_param_space.py`. Test in the existing param-space test file
(`grep -rln "SCHEDULE_DAYS\|schedule_days" testplatform/backend/tests`).

**Steps:**
1. A failing test: `{saturday: True, rest False}` → decoded schedule has monday True.
2. Implement.
3. Run the param-space tests.
4. Commit.

---

### Task 6: Gates

1. **Full suites.** Run the three full unfiltered suites and compare with the Task 0 baseline. Every new
   failure is a regression to fix, not to explain away.
2. **Equity no-impact.** The equity golden runs are byte-identical. Also run one real equity backtest locally
   (any FMPRating S1 config over 2024) before and after on the same data; the results must be identical.
3. **Option re-check.**
   - Re-run the diagnosis probes (`test_files/probe_olp_report_20260924.py`) on the fixed code for
     best_ret_30_99 and dd100_pos_ret. The split-inflated P&L must be gone and max DD must equal the curve,
     give or take a genuine intraday dip.
   - Re-run the deployed O_LC TOP1 genome (`scratchpad/deploy/olc_top1_payload.json`, or the params in
     `scratchpad/deploy/local_stage1_olc_backtests_before_delete.json` id 1709) and report
     return/CAR/maxDD/trades against the old +1588% / −34.3% / 351.
4. **Perf gate on remote227, 1 year.**
   - Same recipe as 2026-09-23: `/home/debian/perfgate`, O_LC and O_IC, one year, before/after on the same
     box. Report the per-trial time delta.
   - Task 2's daily stepping is expected to cost time. Report it, do not hide it, and let the user accept or
     reject it.
   - Batch the SSH commands into one session per step: fail2ban bans this machine after bursts.
5. **Review.** A spec review, then a code-quality review, then a final review of the whole branch.

---

### Task 7: Merge, push, relaunch

1. Merge `fix/option-bt-engine-bugs` into dev with TEST_APP_VERSION 0088 → 0089, and push. The grid is
   stopped, so there is no job boundary to wait for.
2. **Update the grid clone** on remote227 (`/home/debian/ba2-grid/repo`, branch `stage1-2020`). Its
   `origin` is a stale local mirror, so fetch GitHub directly:
   `git fetch https://github.com/bmigette/BA2TradePlatform.git dev && git merge --ff-only FETCH_HEAD`.
3. **Archive** the old log `stage1_parity_20260923T115339Z.log`.
4. **Relaunch** with the same env as the 2026-09-23 launch (see memory
   `project-bt-live-option-parity-and-stage1-relaunch` and `tools/stage1_run.sh`): 28 slots, fitness
   `option_car_target_soft30`, DeterministicScorer, 16 structures, POP 200, GEN 60, early-stop 8.
   - Use a NEW suffix so no job name collides with the stopped run's rows (15 completed, 16 cancelled).
   - Apply any strategy-level decisions the user makes (below).
5. **Re-create the 2-hourly monitor cron** and the distinct TOP-N tracking.
6. **Update memory:** the relaunch record and the fixed-bug note.

---

## Revisions after Task 0 and the user's decisions (2026-09-24)

**Task 0 finding.** A pre-split option symbol has NO bars after the split. The adjusted contract trades
under a NEW symbol: strike ÷ k, quantity × k, premium ÷ k.

Some pre-split OCC strings are REUSED by unrelated contracts after the split. For PANW 2:1, 356 of 1,124
symbols are reused this way; for AAPL 4:1, 44 of 1,064. A held lot on such a symbol would be marked, filled
and settled against a different contract.

**Execution order:** 2 → 1a → 1b → 3 → 4 → 5 → 13 → 8 → 9 → 10 → 11 → 12 → 6 → 7.

**Backward-compatibility acceptance** (user, 2026-09-24). This is a Task 6 gate.

- Re-run at least TWO existing stored stock backtests from the local testplatform DB on the final branch,
  with their stored configs and the same data. Each result must be BYTE-IDENTICAL to the stored one:
  trades, equity curve and every metric.
- Pick ONE DeterministicScorer stock backtest, because Task 10 changes that expert's code, and ONE backtest
  from another expert, for example FMPRating or FactorRanker.
- Any difference fails the gate; it must be explained and fixed, not waived.
- Option backtests are exempt: bugs 1-4 change them on purpose.

**Task 10 is IN stage 1** (user, 2026-09-24).

- Stage 1 uses the new behaviour through its launcher setting.
- DeterministicScorer runs LIVE, so the flag's default MUST keep every existing expert, backtest and live
  result exactly as it is.
- Future grids can change the default choice.

- **Task 1a — lot basis + collision guard.**
  - Record `k_lot` (the as-traded factor on the fill day) when a lot opens.
  - Every held-lot spot is `adjusted_close × k_lot`: marks, BS fallback, no-arb bounds, margin,
    maintenance, liquidation, the run-end intrinsic floor, single-leg and combo expiry settlement, the
    assignment share leg, covered-call cover, pledged-cover conversions, and the per-trade factor in the
    `results.py` intraday refinement.
  - A bar read for a held lot on a day whose factor differs from `k_lot` counts as "no bar". That applies
    to marks, quotes, close fills, settlement and the recorder.
  - Expiry settlement reads the EXPIRY date's close.
  - Where the change touches shared live code, it must be a no-op when there is no split basis (live).
- **Task 1b — re-key onto the adjusted contract on the ex-date.**
  - Applies to integer forward splits, when the store has the adjusted symbol with bars from the ex-date.
  - The re-key: strike ÷ k (OCC 3-decimal strike string), qty × k, avg_price ÷ k, and the lot and order
    linkage moves with it. Exits then fill on real bars, which is what live does.
  - Non-integer and reverse splits stay on 1a and ride to expiry, LOUDLY logged.

**User decisions (2026-09-24):**

- **Run the bearish jobs** (O_LP, O_BEARCS) with the signal-mode gene free. All 16 jobs.
- **Task 8 — 1-contract sizing floor, behind a flag.** Option action param `min_one_contract` (default
  False, so every existing run reproduces). When the cost-based size rounds to 0 but ONE contract fits
  under the per-instrument cap (and the risk budget), buy 1. It lives in the shared `TradeActions` sizing
  (`_size_by_cost` ~:2712 / the refusal ~:3526), so live and backtest behave the same. The launcher sets
  it True for the stage-1 relaunch.
- **Task 9 — `option_entry_cross` gene range 0.75-1.0** in the launcher, for new grid runs only.
- **Task 10 — direction-aware DeterministicScorer macro, behind a setting.**
  - New expert setting `macro_short_side`: `"same"` is the default (today's behaviour: the multiplier
    scales negative scores too); `"mirror"` scales a negative score by `exposure_multiplier(-regime)`, so
    a bearish regime AMPLIFIES SELL conviction instead of muting it.
  - The default reproduces every existing result: stock and option backtests, and live experts. It must be
    registered in the expert's settings and in `_build_daily_trial_config`'s whitelist (see the memory note
    "trial-config whitelist drops new knobs").
  - The launcher sets `"mirror"` as a FIXED setting for the stage-1 relaunch (not a gene).
  - Proof of no impact: the DeterministicScorer equity goldens are byte-identical under the default.
- **Not in scope:** the Altman-Z exemption for financials.
- **Task 11 — size within fill volume (APPROVED 2026-09-24).**
  - The backtest fill engine caps an option fill at 10% of the contract's bar volume
    (`backtest_account.py:313/2907`), so an order larger than that expires unfilled. In the O_LP diagnosis
    that was about half of the expired entries.
  - At ORDER time, cap the backtest option order's contract count at the fillable amount:
    `floor(bar_volume × cap)` of the bar the fill will use, which in practice is the decision bar's volume.
    It must be the same number the fill engine will allow.
  - BACKTEST-ONLY: live Alpaca fills small orders against the quote whatever the day's volume, so live
    sizing is unchanged. It is behind a run-config flag, default off, so older option runs reproduce. The
    launcher sets it on for stage 1.
  - If the cap rounds to 0, the order is not placed and the reason is logged. Never a silent 0.
- **Task 13 — option ledger consistency check** (from the Task 2 code review; must land before the
  relaunch).
  - **Why:** the lot ledger (`_option_positions`) can drift from the transaction view. The known route is
    `_liquidate_option_lot` (a margin-call buy-back), which books the whole lot's close on ONE transaction
    when two OPENED transactions share a contract, found through `_option_transaction_for_contract`. An
    orphaned lot then corrupts equity:
    - its mark falls back to BS or the entry premium, floored at intrinsic;
    - it carries maintenance margin;
    - it takes covered-call cover;
    - a margin call can buy it back.
  - **Detect it:** at every expiry pass and at run end, compare each non-zero lot with
    `get_option_positions()` summed per contract, and log an ERROR on any mismatch.
  - **Fix the known route:** make the margin-call buy-back distribute the close across ALL OPENED
    transactions that hold the contract.
  - **Test it:** a regression test with two same-contract transactions plus a margin call.
  - **Reusable audit plugin:** the scratchpad `plug/orphan_plug.py`.
- **Logged, out of scope:** the test platform UI deploy/import
  (`testplatform/frontend/src/pages/Backtesting.tsx:1381-1384`) sets `execution_schedule_open_positions` to
  the ENTRY days. A UI re-run of an exported GA backtest therefore manages exits only on entry days, while GA
  trials and `tools/import_deploy_payload.py` manage every weekday.
- **Task 12 — narrow gate ranges (APPROVED 2026-09-24).** Launcher gene ranges only; new grid runs only.
  - `_RELATIVE_VOLUME_GATE` max 3.0 → 1.5 (`ba2test_launcher.py` ~:4399).
  - Debit `_IV_RV` range floor 0.8 → 1.0. The credit half keeps its range. If `_IV_RV_RANGE` is shared
    between the halves, split it so only the DEBIT floor moves.

## Decisions for the user (original list; see the revisions above for what was decided)

Diagnosis recommendations that change strategy economics for EVERY structure, or reverse a deliberate
design:

- **`option_entry_cross` range 0.75-1.0** (today about 0.25). Day-only limits at next-open fills expire
  unfilled, which selects against the days a thesis works.
- **`option_sizing` floor of about 3%**, or allow 1 contract up to the per-instrument cap: expensive
  premiums round to 0 contracts.
- **Fill-volume cap vs the selector's `min_volume`.** Fills are capped at 10% of bar volume while the
  selector accepts volume ≥ 25, i.e. 2.5 contracts. Either tie `min_volume` to the planned size or cap the
  size at sizing time.
- **Pin bearish structures' signal mode to "below".** Today the GA may choose "above" (contrarian). That was
  deliberate (`_option_signal_gate` docstring).
- **Narrow `rel_volume` to ≤1.5** and **raise the debit `iv_rv` floor to ≥1.0.** The GA can learn these,
  but they waste early generations.
- **Direction-aware DeterministicScorer macro multiplier.** A bearish regime should amplify SELL
  conviction, not mute it (findings §2).
- **Altman-Z veto skipped for financials** (findings §4). This affects every DeterministicScorer bullish
  job.
- **Hold the bearish jobs (O_LP, O_BEARCS)** until the above is settled. The control run lost 89% even with
  no gates.
