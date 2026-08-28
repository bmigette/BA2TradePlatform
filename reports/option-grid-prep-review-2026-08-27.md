# Option Grid Prep — Code & Plan Review

**Date:** 2026-08-27
**Scope:** all option-grid preparation work merged on `dev` in the last 7 days (pulled at review time; HEAD `30de779a`), reviewed against its plan/spec docs. One small incoming commit was expected after this review and is NOT covered here.
> **H1/H2 SUPERSEDED 2026-08-27 (after this review).** Both were true at the reviewed commit
> `30de779a` and are left UNAMENDED on purpose: this report is the audit record that motivated
> the fix, and rewriting a dated finding destroys the evidence that it was found. The backtest
> now holds assigned stock behind an opt-in `hold_assigned_stock` switch (default OFF) and
> O_WHEEL is runnable — see the option-ga-grid design spec and lifecycle plan Task 10.

**Method:** full git history walk (~50 commits in the option cluster), spec+plan read, three parallel deep-review passes (grid foundations, option-RM P1/P2a, model-lifecycle + live safety seams), independent re-verification of every high finding by direct code inspection and executed probes, and all four test suites run locally.

---

## Verdict

The six-piece **option grid foundations** work is **sound and grid-ready**: every task landed as designed, the two bugs found during review were fixed with tests before merge, and all measurable plan claims reproduce exactly (genome sizes, shared-gene parity, gates-off semantics, the 50% cap scoping).

**One show-stopper for exactly ONE structure:** `O_WHEEL` is registered and launchable, but the backtest engine provably cannot run it — it would create **naked short calls by construction** (H1/H2 below). The builder's own docstring says *"Do not launch an O_WHEEL grid until that is fixed"*, but nothing enforces it, and the grid spec still counts the wheel among stage 1's 18 jobs. Everything else — the 15 pure structures plus O_CC/O_PP/O_STK — is clear to run.

Test suites: the grid/option work itself is **fully green** (details in §7). The failures observed are all environmental (venv drift / Python-version float differences), none traceable to this work.

---

## Findings

### H1 — HIGH / BLOCKER for O_WHEEL: backtest liquidates assigned stock; plan Task 10 never landed

- **Where:** `testplatform/backend/app/services/backtest/backtest_account.py:3206` (`process_pending_assignment_sells`… `process_pending_assignment_liquidations`), `daily_engine.py:735–744` (step 4a-pre), scheduling at `backtest_account.py:3070` (`_book_assignment_share_leg`).
- **What:** `docs/superpowers/plans/2026-08-24-option-model-and-lifecycle.md` Task 10 is explicit: *"The backtest must stop liquidating assigned stock… **The wheel cannot be backtested at all.**"* with a switch and `tests/backtest/test_wheel_assignment.py`. Neither exists; all checkboxes unchecked; no commit implements it. Instead, the "no-orphaned-stock" policy was hardened in the OPPOSITE direction (commits `39b84b4f`, `91794c90`): every ITM short-option assignment schedules a FULL liquidation of the resulting shares at the next bar's open — and `test_option_orphan_stock_and_arb_guards.py::test_short_put_assignment_stock_liquidated_next_bar_open` now **pins that behaviour as intent**.
- **Why it matters:** the wheel's second leg (write calls against assigned shares) requires holding the assigned stock across bars. The current engine cannot represent that.

### H2 — HIGH / BLOCKER for O_WHEEL: registered strategy + engine order = naked short call

- **Where:** `testplatform/ba2test_launcher.py:3372` (`_build_strategy_wheel`, commit `313c820e`), engine order in `daily_engine.py` (step 3 manage at :718, step 4a-pre liquidation at :742, expiry at :759).
- **The mechanism (verified, not inferred):**
  1. Bar N: short put expires ITM → physically assigned, `_book_assignment_share_leg` schedules liquidation (`_pending_assignment_sells`).
  2. Bar N+1, **step 3**: `_manage_open_positions` runs. `cc_sell` sees `has_assigned_shares` = true and writes a covered call against the 100 shares.
  3. Bar N+1, **step 4a-pre**: `process_pending_assignment_liquidations` sells all 100 shares — out from under the call it just wrote. The call is now **naked short**.
  4. Worse, it compounds: `cc_guard` (stop when `has_covered_call`) is false while no call is open, so every manage cycle re-writes another call against already-liquidated shares.
- **State of the guard:** prose only. The docstring warns *"Do not launch an O_WHEEL grid until that is fixed"*, but O_WHEEL is in `_STRATEGY_BUILDERS` / `_PURE_OPTION_STRATEGIES`, buildable by any `optimize --strategy O_WHEEL` or `--strategies` list containing it. It is absent from both `--strategy/--strategies` help texts (limits accidental launch, does not prevent it). The grid spec §5 Stage 1 lists it among the 18 jobs — the spec and the builder contradict each other.
- **Also contradicts grid spec §2**, which claims *"a complete option backtest engine… called-away lot splitting"*. Lot splitting exists in the LIVE account (`AlpacaAccount._settle_called_away`); in backtest, assigned lots die the next bar.
- **Recommended:** fix Task 10 (hold assigned stock, explicit switch so historical runs don't move) BEFORE any wheel job; until then either refuse O_WHEEL at build time (`sys.exit` in the builder when launched from a grid command) or drop it from the stage-1 list and record the 17-job reality in the spec.

### H3 — HIGH (latent): `validate_legs` applies the put-only bound to CALL legs

- **Where:** `packages/common/ba2_common/core/option_payoff.py:105–118` (commit `55071192`).
- **What:** the "premium > strike is a bad quote" check sits inside `if leg.kind in _OPTION_KINDS:` — both calls and puts — while the comment two lines above states *"There is deliberately NO equivalent bound for a call: its value is unbounded above."*
- **Executed proof:** `PayoffLeg(kind="call", side=BUY, premium=102.0, strike=50.0)` — a perfectly valid deep-ITM call — is refused with *"premium 102.0 exceeds strike 50.0; a put can never be worth more than its strike"*, and `max_loss` → `UNMEASURABLE`.
- **Blast radius today:** zero — no production code imports these modules yet (grep-verified; only tests). **Latent for Phase 3**, where the spec makes `max_loss` the sizing budget: deep-ITM call legs (ITM bull-call-spread long legs, LEAPS, high-priced underlyings) would be refused; a deep-ITM naked short call would report UNMEASURABLE instead of UNBOUNDED (wrong refusal phrase, wrong triage path).
- **Fix is one line:** restrict the check to `leg.kind == "put"`. No test pins the behaviour either way today — add one with the fix.

### H4 — HIGH: lifecycle plan Tasks 9–12 are unfinished but downstream docs assume them

- **Task 11 (live/backtest parity) missing:** `tests/test_option_lifecycle_parity.py` does not exist; `daily_engine.py` has zero imports of `ba2_common.core.option_lifecycle`. The plan's structural guard ("both engines import the decision function, neither defines its own") would fail. Spec §5's "parity is a design constraint" is half-met (live calls the pure function; backtest manages options solely via exit rules).
- **Task 9 (startup readiness reporting) missing:** nothing flags (a) an expert holding options with no `open_positions` schedule, or (b) a broker without `reconcile_option_assignments` (only Alpaca implements it — IBKR/TastyTrade don't). JobManager has only `_report_iv_rank_readiness` (`JobManager.py:687`). These were "the two silent failure modes this design exists to remove".
- **Task 12 (delete PremiumSeller/OptionPortfolioManager) not done:** `packages/experts/ba2_experts/PremiumSeller/` intact; `daily_engine.py` still carries the bypass machinery (:415–438, :1280–1291). The 2026-08-25 spec's out-of-scope notes assume it was deleted.
- Not grid-blocking, but the plans' "done" checkboxes and the specs that lean on them overstate completion.

### M1 — MEDIUM: grid spec §6.1 overstates DeterministicScorer's price-target model

- The spec says DeterministicScorer *"now carries the shared `analyst_target_model`"* and the grid runs both signal experts so no finding rests on one signal. Verified: the model IS in DeterministicScorer (`DeterministicScorer/combine.py:235`, opt-in `use_model_target`, **default OFF**) — but its launcher gene block (`ba2test_launcher.py:1183–1206`) exposes **no gene for it**. Commits `a30794ac`/`40f3f4f2` wired the model gene only into FMPEarningsDrift/FMPInsiderClusterBuy (`expected_profit_mode` gains `model`). Under DeterministicScorer the GA therefore always measures the ATR target, not the fundamentals model — so "both experts" compares different target mechanisms, and the spec's §8.1 deferral premise is looser than it reads.

### M2 — MEDIUM: plan doc Task 4 snippet is syntactically broken

- `docs/superpowers/plans/2026-08-27-option-grid-foundations.md:612–615`: commit `49dd5736` amended the snippet from marking `enabled=False` to removing leaves, but the edit left a dangling `for leaf in rule["conditions"]["conditions"]:` line with no body in front of the list comprehension — the snippet is not valid Python. The IMPLEMENTATION is correct (`ba2test_launcher.py:3216–3217`); anyone re-executing the plan will hit a SyntaxError. One-line doc fix.

### M3 — MEDIUM: Seam 3's OCO sibling branch unguarded (residual)

- `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:1271–1293`: the `every_option_contract_is_flat` guard was added to the `filled_closing_orders` arm (:1309–1312) but not to the `oco_leg_filled and status == OPENED` branch, which still closes the whole transaction wholesale. Currently unreachable for option structures (nothing writes OCO legs onto them) — same defect class the seam claims to close, so worth closing while the seam is fresh.

### L1 — LOW: `optimize-batch --fitness` help less precise than its `optimize` twin

- `ba2test_launcher.py:4884–4888` says `'option_consistent_annual_return' for pure-option kinds (OS1-OS4/O_*)` without noting the equity-entry exclusion (`O_CC`/`O_PP`/`O_STK`) that `_resolve_fitness` (:2584–2587) applies and that the `optimize` help states explicitly. Correct, but the multi-day-grid reader meets the batch help first. (The Task 6 commit went beyond plan and also fixed the `optimize` help plus added the drift test — good.)

### L2 — LOW: stale docstrings

- `tests/test_launcher_iv_rank_gene.py:11–30` historical gene dump still shows pre-Task-2 ids (`o_lc-gate_confidence`) and the eight price-gate genes deleted in Task 1.
- `ba2test_launcher.py:2971–2977` (`_build_strategy_option` docstring) omits four of the entry's seven gates (pre-existing).

---

## What verified clean (per task)

**Task 1 — expected-profit gate ✅** all four `price_*` gates gone; `_price_target_gates`/`_PRICE_GATE_*` deleted with **0 references repo-wide**; new gate exactly per plan (field `expected_profit_target_percent`, 5.0/2.0/20.0/2.0, toggleable); tombstone comments replace the stale docstring claims; the deleted test's `value_offset_from` coverage was extracted into `test_strategy_param_space_value_offset_from.py` with provenance documented.

**Task 2 — shared ids ✅** `shared-rel_volume` / `shared-gate_confidence` used in BOTH single and group shapes (the seeding-critical property), pinned by `test_single_and_group_jobs_use_the_SAME_key_for_a_shared_gate`; executed probe confirms identical gene keys; `iv_rank`/`iv_rv` correctly stay per-member (operator flip); `_relative_volume_gate()` has exactly one call site, no member prefix.

**Task 3 — O_WHEEL build ✅ (launchability ✗, see H2)** composes O_CSP entry + overlay gated on `has_assigned_shares` (not `has_position`), spliced with `anchor="front"` (plan's `anchor="adjust"` would have raised — documented deviation), `cc_guard` present, registered; ids reuse O_CSP's entry keys verbatim (tested). Strike params match the plan; no copy-paste errors.

**Task 4 — --gates-off ✅ (after the fix)** commit `30de779a` closed the real hole (flag parsed but never applied on `optimize-batch`); verified both `_cmd_optimize` (:3580) and `_cmd_optimize_batch` (:3896) set `_OPTION_GATES_OFF` before building strategies; gates-off REMOVES toggleable leaves (marking would be dropped by `ConditionLeaf.to_canonical_dict` — the implementer caught this and the plan was amended); `has_no_position` survives (executed: OFF leaves `['o_lc-flat']` only); `test_gates_off_survives_normalisation_and_reaches_the_ENGINE` pins it end-to-end.

**Task 5 — 50% cap ✅ (after the fix)** commit `49dd5736` fixed the O_STK control-arm leak; `_rm_opt_for` (:1385–1404) excludes exactly `O_STK`, keeps O_CC/O_PP/O_WHEEL at 50%; both spread sites converted (`optimize` :3811, `optimize-batch` :4012); repo-wide grep finds **no stray `**_RM_OPT` spread** (`_BYPASS_RM_OPT` for FactorRanker correctly untouched); `_OPTION_SIZING_BANDS[20.0]` hi = 50.0; both directions pinned by tests (S2/O_STK at 30, option kinds at 50).

**Task 6 — fitness help ✅** matches `_resolve_fitness`; drift test added.

**Option RM Phase 1 ✅** payoff math executed against hand-computed examples: long call K100/P2 @105 = +300 ✓, short put @95 = −300 ✓, bull put spread max_loss = (width − credit)×100 = 420 ✓, naked short put MEASURED 9800 ✓, naked short call & 1×2 call ratio UNBOUNDED ✓, covered call 8800 (basis−credit, not the wrong strike-capped form) ✓. OptionTerm windows contiguous 0–1095, no gaps/overlaps (e1df8368 landed). The no-op-at-default-weights proof (904b3f20) is a genuine differential test against legacy `_pick_by` across 7 chains × 3 methods incl. ties — not a tautology; policy intentionally not yet wired into `select_single`.

**Option RM Phase 2a ✅** exactly 7 builders converted to `_resolve()` (plan's count verified); the unified-sizer parity test (ade1b7de) recomputes the pre-split formulas inline — diffed against the actual pre-split source, a real parity proof; refusal messages byte-identical to pre-split; `test_new_option_actions.py` was strengthened (dual-shape AST scan + coverage floor), no test weakened.

**Live safety seams (2026-08-25) ✅** all ten seams verified: guards exist, tests pin them, and every one RAISES or REFUSES — no silent money fallbacks found (seams 1a–1d close seams, 2 pledge tri-state + entry/exit/monitor cover guards, 3 multi-leg settlement guard). Independents OPT-S1/S2/S4/S5, OPT-L4/L5/L6/L7 all landed with tests. Zero-coercion lint passing.

**Grid-spec §2 claims ✅** 15 `_OPTION_STRATS` keys, `has_assigned_shares` condition + 14 wheel tests, early-assignment-not-modelled at `daily_engine.py:1408` verbatim, `warmStartFromOptimizationId` single-int, `genetic.py:377` fills missing genes with `min`, `OS_ALL` absent — all true.

---

## Genome measurements (executed, plan claims hold)

| shape | genes | plan bound |
|---|---|---|
| singles (15 pure + O_STK) | 19–25 | ≤ 26 ✓ |
| O_CC / O_PP | 24 | — |
| O_WHEEL | 29 | — |
| OS1 | **74** | ≤ 95 ✓ (spec estimated ~90) |
| OS2 / OS3 / OS4 | 48 / 40 / 31 | — |

Pinned as upper bounds by `test_option_grid_foundations.py:63–74`. Single↔group shared-gene key parity verified for both shared gates.

---

## Test suite results (this machine, 2026-08-27)

| suite | interpreter | result | verdict |
|---|---|---|---|
| `test_option_grid_foundations.py` | .venv 3.11.8 | **83 passed** | ✅ |
| backend `-k "launcher or option"` | .venv | **1066 passed, 1 skipped** | ✅ |
| `packages/common/tests -k option` | .venv | **1031 passed** | ✅ |
| option RM new suites (9 files) | .venv | **233 passed** | ✅ |
| full backend | .venv (3.11.8) | 5 failed / 3285 passed | env — 5× `test_strategy_fitness_equity_frozen` `curve_uneven` differ by 2 ULP in the last digit (`…187`→`…189`); **pass on 3.12.10**. Float-summation sensitivity, not this work |
| full backend | ba2-venvs/test (3.12.10) | 7 failed / 3439 passed | env/flaky — iv-rank calendar (3), chronos inference (1), `test_optimization` GA (3); different failures each run, unrelated files |
| live `tests/` | .venv | 51 failed / 4361 passed | **venv drift, all pre-existing:** 31× portfolio-allocation pages (`TypeError: object function can't be used in 'await'` — nicegui 3.13 installed vs async-listener shape the tests assume), 16× option-intent migration (subprocess `FileNotFoundError` — spawns the wrong python), 2× TastyTrade TIF "Ext Overnight"/"GTC Ext Overnight" not in `_TT_TIF_MAP`, 2× broker SDK pins (tastytrade 12.4.1 installed vs pinned 12.0.2, alpaca-py 0.43.4 vs pinned 0.43.2). None touch option-grid code |

Note: the plan's "known Windows-only failure" (`test_worker_server.py::test_logs_rejects_path_traversal`) did not appear in either backend run; the failures above replaced it.

---

## Bottom line for the grid

1. **Stages 0a/0b and stage 1 can start** for the 15 pure structures + O_CC/O_PP/O_STK on both signal experts. The foundations are correct, tested, and the two bugs found mid-review were fixed with pins before merge.
2. **Exclude O_WHEEL until H1/H2 are fixed** — or add a code-level refusal. Running it today doesn't produce bad numbers; it produces a naked-call simulation labelled as a wheel. Amend the grid spec's "18 jobs" accordingly (or fix the engine first).
3. **Fix the one-line call-leg guard (H3) before Phase 3 wires `max_loss` into sizing** — cheap now, a wrong-refusal debugging session later.
4. Housekeeping: plan Task 4 snippet syntax (M2), batch fitness help precision (L1), the two stale docstrings (L2), and deciding the fate of lifecycle plan Tasks 9/11/12 (H4) so the docs stop implying they're done.

---

## Addendum — storage-parity check of the grid's conditions (2026-08-27 evening)

The 2026-07-30 inert-gate incident (`DaysSinceLastClose*` reading an EMPTY in-memory store while SQLite saw the rows — 30 of 153 optimizations scored dead genes) raised the question whether the option grid's own conditions are storage-safe. Audited and probed:

- **Every storage-touching grid condition goes through a storage-aware path** — the `trade_repository` seam (`HasAssignedSharesCondition`, `HasCoveredCallCondition`, `HasProtectivePutCondition`, `HasOptionPositionCondition`, `DaysOpenedCondition`'s open-date lookup) or the dual-path `trade_store` helpers (`transactions_where`, `get_or_none`, `orders_where` behind `has_no_position`, `days_to_expiry`). The one remaining raw `get_db()` in `TradeConditions.py:158` queries `ExpertRecommendation`, not an in-mem trade model — not this defect class.
- **Executed parity probe** (`test_files/probe_option_grid_condition_parity.py`): the same book seeded into SQLite and the RAM backtest store; all six storage-touching conditions answer identically under both. The discrimination arm proves the answers are real, not "always True": bought-outright shares do NOT fire `has_assigned_shares`; a 3-day-old position correctly fails `days_opened > 4`.
- **Pinned permanently:** 4 new tests in `packages/common/tests/test_trade_conditions_storage_agnostic.py` (29 → 33 passing), including a dual-backend parity test for the wheel gates. Full `packages/common/tests` suite re-run green.

**Verdict:** no inert-gate risk found in the grid's condition set. (This does NOT clear H1/H2 — those are engine bar-ordering, a different layer.)
