# Options Grid 2 + Convex Grid + FMPEarningsEvent — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to execute this plan task-by-task (fresh implementer per task, two-stage review
> after each: spec compliance, then adversarial quality with mutation re-runs).

**Goal:** Build everything the two 2026-08-31 designs specify: the multi-strategy
Options Grid 2 (`O_LEAPC O_LEAPP O_PMCC O_ERN O_CBS O_PBS`, phase-2 `O_CAL`), the
separate convex-harvest grid with its `option_convex` fitness, and the
`FMPEarningsEvent` expert that ranks earnings events for `O_ERN`.

**Architecture:** Everything stacks on the option-selection stack (Task-8
max-loss stamps, delta-viable bar greeks, the covered-call stock-leg fix, the
shared option RM). New shared code goes in `packages/*`; grid/launcher/fitness
code in `testplatform/`; nothing in-tree-shim. One new two-expiry lifecycle
generalises PMCC and (later) calendars.

**Tech stack:** Python 3.12 venv at `BA2TradePlatform\.venv`, pytest, DEAP GA,
pyarrow parquet options cache, FMP disk cache (`fmp_history`).

**Design sources (read FIRST, from the branch you build on):**
- `docs/superpowers/specs/2026-08-31-leaps-grid-design.md` (Grid 2 + expert §9)
- `docs/superpowers/specs/2026-08-31-convex-harvest-grid-design.md`
- `docs/superpowers/specs/2026-08-29-option-selection-modes-and-max-loss-design.md`
  (the stamps/policy machinery this consumes)

---

## Ground rules (apply to EVERY task)

**Where.** A dedicated worktree, branch `options-grid2`, created from the TIP of
`option-selection-modes` after its final review closes (the controller states the
base SHA at dispatch). NEVER edit or run tests in the main tree
`C:\Users\basti\Documents\dev\BA2TradePlatform` — live grids import from it;
docs there are read-only reference. No push, no `version.py` (owed by the merger,
in the merge commit). File-copy restores, never `git checkout --`.

**Method traps (each cost a prior agent real time):**
- `packages/common` tests: run FROM `packages/common` in the worktree — its
  conftest defeats PYTHONPATH shadowing (a shadowed run silently tests the MAIN
  tree and reports false greens).
- Backend: `pytest tests/` (bare `pytest` dies on `tests_scripts/` collection).
  PYTHONPATH = worktree `packages\common;packages\providers;packages\experts;testplatform`
  (Windows-style, `;`-joined; the `testplatform` entry is REQUIRED or
  `ba2test_launcher` resolves to the MAIN tree). Verify `ba2_common.__file__`
  AND `ba2test_launcher.__file__` resolve inside the worktree before trusting
  any count. Load the launcher by absolute path (`_LAUNCHER_PATH` pattern).
- Root `tests/`: run from the worktree root IN THE BACKGROUND (~9 min; a
  foreground run hits the 10-min Bash cap and the faulthandler dump imitates a
  deadlock). Known-bad list: `test_portfolio_allocation_page`,
  `test_option_intent_migration`, `test_tastytrade_account`,
  `test_broker_sdk_pins`, plus the packages/common float-dust wizard test and
  the 5 backend `curve_uneven` frozen-baseline failures.
- `tests/test_no_zero_coercion.py` is line-number-pinned against
  `ui/pages/settings.py` and count-pinned per module — any edit there moves it;
  update baselines with arithmetic shown in the commit, never adjust-until-green.
- Config access: explicit dict access, no `.get()` defaults. Confidence 1–100.
  `format_type` contract for any provider. No fallback prices ever.

**Per-task definition of done:** failing test first → implement → suites at
baseline + your new tests → EVERY guard mutation-killed by a NAMED test (apply
the mutation, run, restore by file copy, list kills in the report) → one commit,
explicit paths, message ends with the standard co-author/session trailer.

**Numbers discipline:** any count/coverage claim in code comments must be
re-measured by the task that writes it, with seed and method stated
(the w_spread lesson; see `option_selector._publishes_spread` for the pattern).

**Model routing** (controller sets `model` at dispatch):
- **opus**: Tasks 5, 6, 7, 9, 12 (lifecycle, fitness, expert core).
- **sonnet**: Tasks 1, 2, 3, 4, 8, 10, 11, 13, 14 (mechanical/mirrored work).

---

## Phase A — shared machinery

### Task 1 (sonnet): `option_strike_method: "delta"`
**Files:** `packages/common/ba2_common/core/option_selector.py` (selection),
`packages/common/ba2_common/core/TradeActions.py` (method plumbing + settings
definitions), tests in `packages/common/tests/test_option_delta_strike_method.py`.
A third strike method beside `percent_otm`/`atm`: pick the candidate whose BAR
delta is nearest the target (absolute value for puts). Fail-closed: if NO
candidate in the chain carries a delta, refuse with a cause naming the method —
never fall back to another method silently. Eligibility/spread/policy machinery
unchanged (the method only changes the target function). Tests: nearest-delta
pick for calls and puts; all-null-delta chain refuses; existing methods
byte-identical (pin one golden pick per method). Mutations: (a) fallback to
percent_otm on missing delta → named refusal test fails; (b) sign error on puts
→ named put test fails.

### Task 2 (sonnet): chain-depth preflight probe tool
**Files:** create `tools/probe_option_chain_depth.py` + backend test
`testplatform/backend/tests/test_probe_option_chain_depth.py` (loaded by
absolute path, `_LAUNCHER_PATH` pattern).
Productionise the 2026-08-31 in-session measurement: for each symbol of an
input list, does the parquet tree hold an expiry with bars at `--min-dte N`
inside `--start/--end`? Output: kept list to a file + a printed kept/dropped
summary (the grid-2 §5 per-strategy thresholds: 365 LEAPS keys, 180
backspread/calendar, 7 O_ERN). Stdlib+pyarrow only, read-only, seeded sampling
OFF by default (full scan; `--sample N --seed S` optional and printed). Tests
run against a tmp parquet tree fixture (build 3 symbols: deep, shallow, empty).
Mutations: threshold off-by-one; dropped symbols silently omitted from summary.

### Task 3 (sonnet): Black-Scholes mark fallback
**Files:** create `packages/common/ba2_common/core/option_bs.py` (pure:
`bs_price(spot, strike, dte_days, iv, right, r=0.0) -> float`), wire in
`testplatform/backend/app/services/backtest/backtest_account.py` mark-fallback
chain; tests `packages/common/tests/test_option_bs.py` +
`testplatform/backend/tests/backtest/test_bs_mark_fallback.py`.
Hierarchy becomes: bar close → **BS(bar iv)** → the existing
`max(intrinsic, entry)` — each stage only when the prior is unavailable, and
EVERY stage still clamped by `_clamp_premium_to_no_arb`. BS requires spot, iv,
dte all present and finite, else falls through. **BS never touches a risk
number**: reserve/max-loss/margin paths must be provably BS-free. Tests:
hand-derived BS values (put-call parity check in-test); fallback ordering;
clamp still applied. Mutations: (a) BS reachable from `option_reserve_required`
or `short_pair_margin_per_contract` call graph (assert via import/callers test)
→ named test; (b) skip clamp after BS → named test; (c) missing iv silently
priced at 0 → named test.

### Task 4 (sonnet): take-profit-multiple exit for long options
**Files:** `packages/common/ba2_common/core/TradeConditions.py` (new field
`profit_multiple_of_premium`, riding `_get_pnl_for_condition` beside
`LossPctOfMaxLossCondition`), registry closure (types.py, CONDITION_MAP,
rule_builders.FIELD_EVENT, docs, export/import — the closure suite enforces);
tests `packages/common/tests/test_profit_multiple_condition.py`.
Value = current structure value ÷ entry premium (net debit); fires on `>=`
threshold; NEVER fires when entry premium ≤ 0 (credit structures) or
unresolvable — unknown never reads as firing (Task-8 discipline). Scale-free
across contracts. Mutations: credit structure reads as firing; absence reads
as 0-but-firing on `<`.

## Phase B — structures

### Task 5 (opus): backspread builders `O_CBS` / `O_PBS`
**Files:** `packages/common/ba2_common/core/TradeActions.py` (two builders,
1×2: SELL 1 nearer-delta leg, BUY 2 further legs, both delta-selected via
Task 1), reserve/stamp integration; tests
`packages/common/tests/test_backspread_builders.py`.
Payoff machinery already handles ratios (`O_RS` is the mirror — read it first).
Requirements: max loss MEASURED (worst case pins between strikes — hand-derive
in tests for both call and put versions); Task-8 stamp present; net
credit-or-debit both admissible; the short leg is COVERED by the longs (never
charged naked — pin against `short_pair_margin_per_contract`/naked branches);
`_size_by_cost` uses max loss, not premium, for these (the known
premium-vs-max-loss gap — close it for these builders and say so). Mutations:
ratio flipped (2 short 1 long = unbounded — must REFUSE, kill by test); wrong
leg delta ordering.

### Task 6 (opus): two-expiry lifecycle builder (PMCC first)
**Files:** `packages/common/ba2_common/core/TradeActions.py` +
`packages/common/ba2_common/core/option_lifecycle.py` (the shared lifecycle
promoted by the RM work — extend, don't fork); tests
`packages/common/tests/test_pmcc_lifecycle.py` + backend e2e
`testplatform/backend/tests/backtest/test_pmcc_engine.py`.
The wheel pattern with a long-CALL cover (design §3–4): entry = LEAPS
(delta-selected, DTE≥365) + immediate short call, admission requires short
strike > LEAPS strike; roll loop at short expiry/buyback trigger (% of credit
decayed — searched knob plumbed later by Task 10); structure exits (long DTE
floor, `opt_sl_ml`, delta floor searched-on/off) close BOTH legs. Risk basis:
intrinsic floor — max loss = LEAPS debit − net credits, restamped as credits
accrue; charged covered, never naked. Marks per Task 3 hierarchy. THE
INVARIANT: the engine can never hold the short without the long — pinned by a
test that tries every exit path and asserts leg-pair atomicity. Extends the
covered-call stock-leg fix (same seam, cover kind = long option). This is the
largest task; STOP-and-report rather than invent if the lifecycle service's
shape fights the design.

## Phase C — the expert

### Task 7 (opus): `FMPEarningsEvent` core (features from the disk cache)
**Files:** create `packages/experts/ba2_experts/FMPEarningsEvent.py` +
`packages/experts/tests/test_fmp_earnings_event.py`.
MarketExpert with `get_settings_definitions`: `earnings_days_look` (int, 10),
`min_hist_events` (int, 4), `min_analysts` (int, 3), `allow_unconfirmed_dates`
(bool, False), weights `w_hist_move`/`w_surprise_vol`/`w_vol_cheapness`
(float, 1.0/1.0/1.0). Per run: symbols with an event inside the look window;
features from the fundamentals providers (`past_earnings_quarterly` shapes —
dates, eps, epsEstimated): hist_move = avg |earnings-day move| over past
events (dates × OHLCV provider), surprise_vol = std of past surprise %.
Composite rank → confidence 1–100; recommendation data carries
`days_to_earnings`, feature values, and the event date. Below
`min_hist_events` → no recommendation (never a padded rank). `w_dispersion`/
`w_revision` DO NOT EXIST (design §9 withheld them — a comment records why and
what unlocks them). Tests use fixture payloads mirroring the measured cache
shapes, incl. the SBET-class no-coverage case. Mutations: min_hist_events
bypass; unconfirmed date admitted when disallowed; a weight that never moves
the rank (dead-gene check at expert level).

### Task 8 (sonnet): `w_vol_cheapness` (implied leg)
**Files:** same expert + tests.
hist_move ÷ implied move, implied = ATM straddle price ÷ spot from the options
store at decision date (backtest: parquet reader; live: broker chain via the
account interface — duck-typed seam, fail-to-absent). When implied is
unavailable the FEATURE is absent and its weight inert for that symbol
(applicability-first: absent never demotes — the `_OFF_SCALE` discipline from
the selection stack). Mutation: absent implied read as 0 (would demote) →
named test.

### Task 9 (opus): `O_ERN` entry gating
**Files:** `packages/common/ba2_common/core/TradeConditions.py` (new numeric
field `days_to_earnings` read from the RECOMMENDATION data, registry closure),
launcher-side strategy def next task; tests in packages/common + backend.
The strategy owns timing (design §9): entry rule leaf
`days_to_earnings <= X` (X searched 1–5) + exit `days_after_event >= Y`
(Y searched 0–2; implement as a time-based exit keyed off the stamped event
date). Absent stamp → never fires. iv-rank entry gate: REUSE `_IV_RANK_GATE`
machinery (launcher side, Task 10). Mutations: absent event date fires;
timezone/date-boundary off-by-one (event date vs bar date compare — pin the
convention explicitly).

## Phase D — grids

### Task 10 (sonnet): Grid-2 launcher wiring + matrix script
**Files:** `testplatform/ba2test_launcher.py` (`_OPTION_STRATS` entries
O_LEAPC/O_LEAPP/O_PMCC/O_ERN/O_CBS/O_PBS with the design-§2 gene tables;
NO group key in round one), `tools/run_options2_matrix.py` (phase-1 job list,
probe-tool preflight with per-strategy thresholds, `--experts` incl.
FMPEarningsEvent for O_ERN), the commented lower trade floor for long-dated
keys ONLY; tests: extend `test_option_grid_foundations.py` gene-budget/cap
pins (hand-derived arithmetic), new `test_options2_matrix_script.py`
(bash -n + job-list golden). O_SSTG/O_SSTD exclusion discipline carries over
(one unbounded-set definition). Every new gene passes the dead-gene guard
pattern (genome → trial config → different submitted structure) — one named
test per gene family. Mutations: a gene dropped by the settings whitelist;
trade floor applied to O_ERN (must NOT be).

### Task 11 (sonnet): expert genes for FMPEarningsEvent
**Files:** launcher `_EXPERT_OPT` wiring for the three weights +
min_analysts + allow_unconfirmed_dates (only for jobs whose expert is
FMPEarningsEvent), threading through `_build_daily_trial_config`'s expert
settings (THE WHITELIST TRAP — prove the value reaches the expert with a
recorded-chain test per gene); mutation: whitelist drop.

### Task 12 (opus): `option_convex` fitness
**Files:** `testplatform/backend/app/services/strategy_fitness.py` (or the
module the registry lives in — find `_OPTION_CAR_STRATEGIES` routing and
mirror it), config for the breadth floor + dd threshold; tests: frozen-
baseline suite in the equity-fitness pattern
(`test_strategy_fitness_convex_frozen.py`) + property tests.
Order (design §3, F9a discipline — LITERAL order): wipeout sentinel FIRST;
then total end-window return net of costs; dd penalty zero below 50%, linear
50→90, sentinel ≥100; breadth floor (≥30 tickets/yr AND ≥20 underlyings) →
LOW_TRADE-style sentinel; telemetry (hit rate, top1/top5 share) recorded in
the result payload, NEVER in the score. Constants are commented config, not
genes. Mutations: dd penalty below threshold; breadth floor OR instead of
AND; sentinel ordering swapped (wipeout after return) — each kills a named
frozen/property test.

### Task 13 (sonnet): `O_CONVEX` key + convex matrix
**Files:** launcher `O_CONVEX` def (design §2 genes: delta 0.10–0.35, DTE
180–540, per-ticket premium sizing 0.5–2.0%, tp multiple 3–10x|expiry via
Task 4's condition, sl_ml default OFF, 1 ticket/underlying),
`tools/run_convex_matrix.py` (fitness `option_convex`, DTE≥270 probe
threshold, jobs O_CONVEX × experts). The tail-hedge put arm = a
`kind` toggle gene (call|put|both) — cheap per design. Pins: fitness routing
(an O_CONVEX job must REFUSE `option_car` and vice versa — never silently
cross-score); sl_ml default genuinely off in the emitted ruleset.

### Task 14 (sonnet): docs + STATE + phase-2 stub
Record `O_CAL` as phase-gated (stub entry refusing loudly with the design
reference, like the naked exclusion); update EXPERTS.md for FMPEarningsEvent;
write the plan's completion STATE note; verify ALL suites one last time and
tabulate the final baselines for the merger (who owes the version bump).

---

## Review cadence
After each task: spec-compliance review, then adversarial quality review with
at least one mutation re-run and independent re-derivation of any number the
task wrote into comments. After Task 14: one consolidated final review of the
branch. The controller dispatches reviews with the same worktree/method-trap
preamble as implementation briefs.
