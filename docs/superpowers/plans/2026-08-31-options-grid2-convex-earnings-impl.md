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

**Performance acceptance criteria (operator, 2026-09-01) — apply to EVERY task:**
1. Option code must not impact stock-backtest performance: a pure-equity trial
   does ZERO additional per-bar/per-action work from option machinery. Every
   option path sits behind a mode/structure check equity trials never enter —
   pinned structurally (call-count zero in an equity trial), plus a wall-clock
   sanity against the pre-task baseline on one representative equity trial
   (>~5% regression = STOP-and-report).
2. Option trial runtime stays comparable to equity trial runtime. Any task
   adding per-bar or per-candidate work (BS fallback, two-expiry lifecycle,
   delta selection, earnings features) MEASURES its cost on one representative
   option trial and reports before/after — never assumed. A task that makes
   option trials a runtime class slower than equity trials is not done.

**Results-identity acceptance criterion (operator, 2026-09-01) — apply to EVERY
task:** non-option backtest results are BIT-IDENTICAL, proven by
`testplatform/backend/tests/backtest/test_equity_golden_run.py`'s pinned
fingerprint (`tests/backtest/golden/equity_golden_run.json`) — a real
`DailyBacktestEngine.run()` equity run over a synthetic fixture, every trade
pinned at full float precision through the whole order path (entries, sized
orders, next-bar fills, TP/SL bracket exits, per-bar equity). Any task that
moves that fingerprint is NOT done until the movement is explained and the
golden is DELIBERATELY regenerated with operator-visible justification —
never adjusted-until-green. This criterion is what makes criterion 1's
"equity trials do zero extra work" claim checkable at the RESULT level rather
than argued from scoping. Retrospective cross-check (2026-09-01): the same
harness run against `abcee41f^` = `8109dca3` (the parent of the first option
merge into dev) and against `a901e360` gave the identical fingerprint
`f6226a66e47a15f17c9c5a85e2dde91d07489dd1baffb974b938e90aa163f067` — VERDICT
EQUAL, so the 89 commits in between (all four option merges `abcee41f`,
`3176f4d5`, `06de5148`, `70603e3a` plus their fix rounds) left equity results
untouched. Scope caveat: the reference tree already carries the earlier option
engine (`options_provider.py`, `option_greeks.py`, `OptionsAccountInterface`),
so the pin covers the option program-review era onward, not the option engine's
original introduction.

**Model routing** (controller sets `model` at dispatch):
- **opus**: Tasks 5, 6, 7, 9, 12 (lifecycle, fitness, expert core).
- **sonnet**: Tasks 1, 2, 3, 4, 8, 10, 11, 13, 14 (mechanical/mirrored work).

---

## Phase A — shared machinery

### Task 1 (sonnet): missing-delta cause-naming for `option_strike_method: "delta"`
**CORRECTED 2026-09-01, twice.** First correction: the original text below
described `delta` strike selection as new work. It is not: `_pick_by`'s
`method == "delta"` branch (nearest BAR delta to the target, absolute value
for puts, ties broken by `option_selector._tie` — strike then expiry) and the
`SelectionPolicy` path's own named refusal (`_no_candidate_reason`) both
shipped with `option-selection-modes`, before this plan was written, and are
already covered by `test_option_selection_pick.py`,
`test_option_selection_policy_noop.py`, `test_option_selection_policy_features.py`
and `test_strike_method_registry.py`. An implementer who took the text below
at face value would either re-prove already-shipped behaviour or, worse, risk
destabilizing it.

Second correction (review of commit d02d6018): the first correction's own
text scoped the fix to "the LEGACY path (`policy=None`/default)". That claim
is FALSE and was caught in review — `_pick_refusal_message` fires from the
same `if contract/pair is None` branch regardless of which internal route
produced the `None`, so it names the missing-delta cause equally on the
legacy `_pick_by` route AND on an ACTIVE, non-default `SelectionPolicy`
route (`_policy_pick`/`eligible`), proved empirically with `w_profit=0.5` in
review. The behavior was always correct; only this doc's (and the original
commit message's) claim about it was wrong. Not scoping it down — the
message is factually accurate on both paths.

**What Task 1 actually is:** a `None` pick from `select_single`/
`select_vertical_spread` under `method="delta"` used to collapse into the
SAME generic "No liquid `<structure>`" message regardless of cause — an
empty/DTE-filtered/illiquid chain and a chain that carries no delta data at
all were indistinguishable, so a grid post-mortem on an `O_LEAPC`-style job
could not tell "skip this symbol" from "skip this method" apart. This is
true whether the pick took the legacy route or an active `SelectionPolicy`
route (that path already draws the distinction internally via
`_no_candidate_reason`, but the CALLER-SIDE message TradeActions actually
returns did not, on either route, until this task). **Files:**
`packages/common/ba2_common/core/option_selector.py` (new pure helper
`describe_pick_failure` — gated on `method == "delta"` FIRST, so nothing
pays for the extra chain re-filter on any other pick; policy-agnostic by
construction, since it re-derives the DTE/liquidity-filtered candidate set
directly rather than reading off whichever internal route produced the
`None`), `packages/common/ba2_common/core/TradeActions.py`
(`_pick_refusal_message`, one seam all nine builders call from inside their
existing `if contract/pair is None` branch instead of nine hand-written
cause checks — NOT gated on `self.selection_policy`), tests in
`packages/common/tests/test_option_delta_strike_method.py`. Eligibility/
spread/policy machinery and the `SelectionPolicy` seam's own no-op guarantee
are untouched for every existing method — this only adds a reason string to
one specific `None` case, on whichever route produced it. Tests
(deliberately NOT re-proving nearest-pick correctness, which the suites
named above already cover): the new cause-naming (all-null-delta chain names
the method on BOTH the legacy and an active-`SelectionPolicy` pick;
a merely-illiquid/DTE-filtered chain does not), partial-null skip (a null
delta is dropped, never scored as 0), the tie rule the legacy `_pick_by`
delta path uses (documented and pinned explicitly), and a byte-identical pin
of every non-delta refusal. Mutations: (a) the all-null refusal silently
falls back to the generic message → named cause-naming tests fail; (b) a
null delta scored as 0 → named partial-null-skip tests fail; (c) a non-delta
method's refusal message changes → the named byte-identical pin fails.

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

### Task 6-PRE (opus): per-leg expiries on the Transaction -- the PMCC/calendar prerequisite

**Runs AFTER Task 14. The riskiest task in the plan: a migration on `Transaction`,
a LIVE money record, consumed by both runtimes.** Requirements (each pinned):
1. **Additive, nullable**: per-leg expiry storage designed from how legs are
   already persisted (the parent order's `legs` list is the likely home; if the
   `Transaction` row needs a column it is nullable, with an alembic migration
   AND a downgrade). `Transaction.expiry` KEEPS its single-value meaning for
   every existing single-expiry structure -- byte-identical behaviour for all
   current builders (pin: golden equity fingerprint unmoved; every option suite
   at baseline).
2. **The single-expiry guard in `submit_option_order` stays the DEFAULT**,
   relaxed only for an explicitly declared multi-expiry strategy set
   (fail-closed: an undeclared two-expiry submit still refuses).
3. **The three DTE readers get a per-leg answer with NAMED rules**:
   `option_lifecycle._dte` and `DaysToExpiryCondition` -- structure DTE = the
   SHORT leg for roll-window questions, the LONG leg for roll-floor/structure-
   exit questions; `opt_time`/`opt_dte` state which leg they read. Ambiguity
   was the guard's whole reason to exist -- make the rule explicit, tested from
   both legs.
4. **Both runtimes**: live and backtest read/write the per-leg fields through
   ONE shared accessor; parity-style test.
5. Migration tested forward AND backward on a fixture DB with existing
   single-expiry rows; those rows read identically after upgrade.
Model: opus. Two-stage review, with the migration reviewed by a second agent.

### Task 6 (opus): two-expiry lifecycle builder (PMCC first)

> **RESEQUENCED 2026-09-01 (controller decision after a designed STOP).** The
> codebase refuses two-expiry structures at three independent, test-pinned
> sites (`submit_option_order`'s single-expiry guard, `option_lifecycle._dte`'s
> conflicting-expiries rule, `DaysToExpiryCondition`'s union check) because
> `Transaction.expiry` is a single value "valid ONLY because every supported
> structure is single-expiry". The guard's own comment prescribes the entry
> price: teach the Transaction per-leg expiries first. Therefore: Tasks 7-13
> run BEFORE this task; then a NEW prerequisite task (6-PRE: per-leg expiry
> semantics + migration + the three DTE readers' per-leg answers — structure
> DTE = short leg for the roll window, long leg for the roll floor) lands;
> then this task as written. `O_PMCC` is phase-gated beside `O_CAL` until
> then — grid-2 phase 1 searches {O_LEAPC, O_LEAPP, O_ERN, O_CBS, O_PBS}.
> The wheel-shaped workaround (linked transactions) was REJECTED: it
> re-litigates the M3 stamp ruling on a second structure and charges the
> PMCC short naked in the meantime.

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

> **LANDED AS** `746d59fd` (the five phase-1 option keys, gene tables, matrix
> driver) + `9dd116e8` (fix: lower trade floor scoped to the long-dated
> family only) + `8b705c58` (later operator amendment, 2026-09-02: O_LEAPC/
> O_LEAPP merged into the ONE signal-driven group key `O_LEAP` — see the
> leaps design's own "LANDED AS" note in §2). No other deviation from the
> brief below.

> **FRESH-EYES AMENDMENTS (controller review 2026-09-01, after Task 9):**
> 1. **Fixed method, delta-domain params -- the dead-gene trap.** The launcher's
>    existing strike-method machinery makes the METHOD a categorical gene sharing
>    ONE `strike_param` domain; a percent-OTM domain (0-8) read as a DELTA target
>    is nonsense and refuses every genome. For every grid-2 key the method is
>    FIXED to `"delta"` (never searched) and `option_strike_param`'s domain is the
>    key's DELTA band from design section 2 (O_LEAPC 0.70-0.90 step 0.05; the
>    backspread pair-shape per leg). Pin: no grid-2 key emits a strike-method
>    gene; every strike_param band lies in (0,1).
> 2. **O_ERN expiry-after-event constraint.** The straddle's expiry must land
>    AFTER the print. Today that is only implicit (dte_min >= 7 > days-before
>    <= 5). Pin it as an explicit gene-space constraint (dte_min floor > the
>    entry-days-before ceiling) with a test that a violating genome is refused
>    at decode, never emitted.
> 3. **Field name**: the entry leaf is `rec_days_to_earnings` (stamp-sourced),
>    NOT `days_to_earnings` (the calendar-fetching legacy condition).
> 4. **Registration**: add `FMPEarningsEvent` to `_SUPPORTED_EXPERTS` /
>    `_EXPERT_WARMUP_BARS` (the class declares `BACKTEST_WARMUP_BARS = 620`;
>    the table must agree -- pin equality).
> 5. **Debit/credit partition**: every new key joins the debit or credit set
>    explicitly by its iv-rank THESIS (long-vol keys = debit set; backspreads by
>    the same rule). The import-time partition assertion must stay total.
> 6. **End-to-end engine pins (deferred here by the Task 9 review):** (a) an
>    O_ERN run through `DailyBacktestEngine`: expert stamps -> entry gate fires at
>    rec_days_to_earnings <= X -> straddle submitted -> `days_after_event` exit
>    closes it (the whole chain the unit tests only proved synthetically); (b) an
>    O_LEAPC run at LEAPS-range bar sparsity (~50% bar density on a DTE>=365
>    expiry) proving the BS fallback marks the position and the DTE-floor exit
>    fires -- nothing tests that path today.
> 7. **Golden equity fingerprint unmoved** (results-identity criterion); the
>    lower trade floor is CONFIG naming the long-dated keys only (never O_ERN).
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

> **LANDED AS** `4e8791ab` (weights + `min_analysts` + `allow_unconfirmed_dates`
> gene table, threaded through `_build_daily_trial_config`'s expert
> settings). The `min_analysts` 0-5/default-1 amendment (below) landed with
> it — see the leaps design's own "LANDED AS" note under §9.

> **AMENDMENT (2026-09-01):** `min_analysts` gene range is **0-5 with 0 = gate
> OFF** (measured: default 3 refuses 66% of the universe at a 2023 as-of, 47%
> at 2025 -- decaying strictness that tilts the traded universe across the
> window). Lower the expert's DEFAULT to 1 (a data-quality floor, not a
> selection filter) and record the measurement beside it. Add
> `allow_unconfirmed_dates` as a boolean gene.
**Files:** launcher `_EXPERT_OPT` wiring for the three weights +
min_analysts + allow_unconfirmed_dates (only for jobs whose expert is
FMPEarningsEvent), threading through `_build_daily_trial_config`'s expert
settings (THE WHITELIST TRAP — prove the value reaches the expert with a
recorded-chain test per gene); mutation: whitelist drop.

### Task 12 (opus): `option_convex` fitness

> **LANDED AS** `79087da8` (the fitness itself — ranks on uncapped
> cumulative P&L / starting capital, `capped_drawdown_curve` for the
> drawdown term, per the equity-cap-masking amendment below) + `8708786a`
> (fix: telemetry counted per TICKET, not per structure; a dead exception
> swallow removed). No other deviation.

> **AMENDMENT (2026-09-01) -- the equity-cap masking lesson, second
> application.** Option grids run with `equity_cap`; capped equity reports ZERO
> P&L above the cap (`equity_cap.py`), so a 5x convex winner would read as
> nothing -- masking exactly the outcomes this fitness exists to find. The
> return term MUST be **uncapped cumulative P&L / starting capital**, and the
> drawdown term MUST use the `capped_drawdown_curve` definition (peak-to-trough
> on cumulative P&L / cap), never peak-to-trough on the capped equity series.
> Pin with a fixture where the cap binds: a run whose uncapped P&L is +400% must
> score above one at +40% (mutation: rank on capped equity -> the test fails).
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

> **LANDED AS** `967db2aa` (the `O_CONVEX` key + `tools/run_convex_matrix.py`)
> + `64981161` (review fix: `opt_sl_ml`'s `enabled=False` was inert at
> runtime — the removal-not-flag fix; convex-matrix hardening). **Deviation
> from the brief below**: no `kind` toggle gene — `O_CONVEX` is a GROUP key
> over two member arms (`O_CONVEXC`/`O_CONVEXP`), the same
> `_build_strategy_option_group` mechanism `O_LEAP` uses, per operator
> decision 2026-09-02 (see the convex design's own "LANDED AS" note in §2).
**Files:** launcher `O_CONVEX` def (design §2 genes: delta 0.10–0.35, DTE
180–540, per-ticket premium sizing 0.5–2.0%, tp multiple 3–10x|expiry via
Task 4's condition, sl_ml default OFF, 1 ticket/underlying),
`tools/run_convex_matrix.py` (fitness `option_convex`, DTE≥270 probe
threshold, jobs O_CONVEX × experts). The tail-hedge put arm = a
`kind` toggle gene (call|put|both) — cheap per design. Pins: fitness routing
(an O_CONVEX job must REFUSE `option_car` and vice versa — never silently
cross-score); sl_ml default genuinely off in the emitted ruleset.

### Task 14 (sonnet): docs + STATE + phase-2 stub

> **LANDED AS two halves.** Task 14b (code-side: `O_CAL` stub,
> `EXPERTS.md`, misc review fast-follows) is DONE — 10 commits
> `4f3cb7a6..4568a71f`. Task 14a (this docs/closeout half — the merge, the
> perf timing, the results-comparability note, and the completion STATE
> note below) is this section's own work; see the STATE note's per-item SHA
> table for its commits.

> **AMENDMENTS (2026-09-01):** (a) PRE-LAUNCH PERF: time ONE real-cache option
> trial (parquet store, a grid-2 key, a real universe symbol) against one real
> equity trial of comparable bars -- criterion 2 has only ever been measured on
> fixtures; record both numbers. (b) RESULTS-COMPARABILITY NOTE: the Task-3 BS
> mark fallback changes OPTION results vs the 0058 deploy; stage-1 numbers run
> before this branch merges and grid-2 numbers after are DIFFERENT BASELINES --
> record it the way the CAR-scale change was recorded; never compare across it.
> (c) carries: Task 2's 3 doc nits; the aliased-import limitation of getsource
> pins (codebase-wide); perf_sample_bt's orders/run=0 repair; option_greeks BS
> unification; chase dev's newer commits (worker memory reclaim + 2026.09.0001
> bump) in a cheap merge; the days_after_event(forced)-vs-days_opened
> (discretionary) classification recorded as DELIBERATE (event terminal date vs
> staleness exit -- controller recommendation: keep).
Record `O_CAL` as phase-gated (stub entry refusing loudly with the design
reference, like the naked exclusion); update EXPERTS.md for FMPEarningsEvent;
write the plan's completion STATE note; verify ALL suites one last time and
tabulate the final baselines for the merger (who owes the version bump).

#### Task 14a item 2 — PRE-LAUNCH PERF, measured 2026-09-02

**Method.** One real-cache option trial (parquet store, key `O_LEAPC`, real
universe symbol AXP, window 2024-01-02..2024-03-15 — AXP's real 2025-01-17
January-cycle LEAPS expiry sits ~381 DTE at the window start, inside
`O_LEAPC`'s authored 380-470 entry band) timed against one real equity trial
of comparable bars (same symbol/window/expert-shaped gene table, key `S1`).
Both driven through the low-level `DailyBacktestEngine` harness
`testplatform/backend/tests/backtest/test_grid2_engine_paths.py` already uses
(NOT an optimization job), foreground, `logging.disable(logging.WARNING)`.
Script kept out-of-tree (scratchpad, not committed):
`run_one_trial_perf2.py`.

**Result — COLD (first trial touching this symbol in a fresh process, i.e.
a worker's first trial on a new symbol):**

| leg | wall-clock | bars | orders |
|---|---|---|---|
| EQUITY (S1) | 0.143-0.179 s | 52 | 3 (1 filled round-trip) |
| OPTION (O_LEAPC, real parquet) | 0.972-1.905 s | 52 | 5 (none filled — see below) |
| **ratio** | **5.3x-11.5x across 6 repeated runs** | | |

**Result — WARM (second+ trial on the SAME symbol in the SAME process, the
steady state of a real GA generation once a worker has touched a symbol
once):** OPTION 0.169 s vs EQUITY's own 0.143-0.179 s — **ratio ~1.0x-1.2x,
comparable.**

**VERDICT: criterion 2 FAILS on the cold path, PASSES on the warm path.**
The plan's acceptance criterion ("option trial runtime stays comparable to
equity trial runtime") was written without this cold/warm distinction and is
not met as a blanket claim — a worker's first trial against any new symbol
pays a real, un-amortized tax; every trial after it on that symbol (the rest
of a GA generation, and every later generation) does not. **Not fixed here**
(profiling only, per the controller's decision) — flagged as the input an
optimization task needs, not a task this branch completes.

**Profile (cProfile, cold trial, sorted by cumulative time, top of a 25-row
table; full 25 rows in `docs/superpowers/plans/`-adjacent scratch, restated
here):**

```
ncalls  tottime  cumtime  file:function
     1    0.003    1.905  daily_engine.py:463(run)
    52    0.001    1.878  daily_engine.py:839(_run_expert_bar)
    52    0.001    1.861  daily_engine.py:907(_stage_recommendation_candidate)
    48    0.002    1.804  TradeActionEvaluator.py:366(execute)
    48    0.000    1.796  TradeActions.py:3050(execute)
    48    0.000    1.758  TradeActions.py:3089(_resolve)
    48    0.000    1.676  TradeActions.py:2229(_chain)
    48    0.000    1.675  backtest_account.py:3394(get_option_chain)
    48    0.021    1.675  parquet_options_provider.py:613(get_chain)
    58    0.000    1.566  parquet_options_provider.py:706(_u)
    58    0.000    1.565  parquet_options_provider.py:541(_underlying)
     1    0.015    1.562  parquet_options_provider.py:522(_raw_underlying)
     1    0.015    1.548  parquet_options_provider.py:498(_load_raw_underlying)
     1    0.016    0.984  parquet_store.py:404(read_underlying)
     1    0.000    0.950  pandas concat.py:157(concat)
   184    0.007    0.848  pandas parquet.py:500(read_parquet)
     1    0.024    0.548  parquet_options_provider.py:237(__init__)  [ParquetOptionsProvider]
   184    0.055    0.409  pyarrow pandas_compat.py:794(table_to_dataframe)
```

**Hot spot, named:** `_load_raw_underlying` reads **184 separate parquet
partition files** (one per expiry AXP has ever listed, 2023-01-03..2026-08-27
— every January/monthly cycle across the FULL 3.5-year store, not scoped to
the run's 2.5-month window) and concatenates them into one `_RawUnderlying`
via pandas `read_parquet` x184 -> `pd.concat`. This is a **per-symbol,
per-process ONE-TIME load**, not a per-bar or per-candidate cost — it is
cached afterward by `parquet_options_provider._WORKER_RAW_CACHE` (an LRU
keyed on `(root, underlying)`, "read at most once per worker" per its own
docstring), which is exactly why the warm-trial number collapses to ~equity
parity. The BS-fallback / per-candidate delta filtering that runs on EVERY
bar (`get_chain` x48, `execute`/`_resolve`/`_chain` chain) is cheap by
comparison (tottime, not cumtime, on those frames is near-zero) — **the
entire cold-path cost is the eager whole-history load, not per-bar work**.
Optimization-task input: scope `read_underlying` to (or lazily extend to)
the run's actual `[start-warmup, end]` window instead of reading every
partition the store has ever accumulated for that symbol.

**Real-world impact depends on how many DISTINCT symbols cycle through one
worker process per job** (paid once per symbol per worker, not once per
trial) — not measured here (out of scope for a profiling-only task); a
future optimization task should measure it before sizing any fix.

**Settling the 0-orders question (existing evidence, not new debugging).**
`app/services/strategy_optimization_handler.py:_trial_worker` — the GA's
own per-individual entry point every local/remote trial goes through — calls
`run_daily_backtest(config, ...)` directly (line ~267), the SAME entry point
`tools/run_genome_once.py` uses and the one this task first tried.
`tests/backtest/test_equity_golden_run.py` (the results-identity pin) does
**NOT** use that path: it drives `DailyBacktestEngine` directly (the
low-level harness, matching this task's `test_grid2_engine_paths.py`-shaped
approach) via the LEGACY `seed_ruleset_from_tree(buy_tree=..., ...)` path,
not the unified TradeRule-list (`entry_rules`) shape `S1`/`O_LEAPC` use. So
the golden run's proof that trades happen does not directly cover the
TradeRule-list-through-`run_daily_backtest` combination this task's first
attempt used. **OPEN QUESTION for the final reviewer:** a first attempt at
this task drove `run_daily_backtest` directly with real FMPRating +
TradeRule-shaped `S1`/`O_LEAPC` `entry_rules` on AXP/this window and got ZERO
orders on BOTH sides — even after independently confirming
`run_daily_backtest`'s own `_build_experts` forces
`allow_automated_trade_opening=True` and builds a real ATR
`indicator_provider` automatically (ruling out this task's first two
low-level-harness bugs, below, as the cause there). Real grid jobs
(matrix3/goal2020) demonstrably DO place trades through this exact
mechanism at scale, so **signal absence (FMPRating never cleared `S1`'s
tier gate for AXP in this narrow 2.5-month window) is the more likely
explanation** than a framework defect — but this was not independently
isolated further (out of scope: "do not debug"), and is recorded here as
unresolved rather than asserted.

**Three standalone-harness lessons** (next to the getsource/aliased-import
note below — same spirit: pitfalls the next agent building a bare
`DailyBacktestEngine` harness will hit and should not have to re-discover):
1. A bare `indicator_provider=object()` makes classic-RM ATR sizing raise
   `AttributeError` on every candidate, silently swallowed by the engine
   ("candidate risk manager failed ... 'object' object has no attribute
   'get_indicator'") — zero orders, no exception surfaced. Same defect
   `tools/perf_sample_bt.py`'s docstring already documents and fixes with a
   `_StubIndicatorProvider`; any new standalone harness needs the same stub.
2. `allow_automated_trade_opening` (interface default `False`) must be
   forced `True` via `expert.save_settings(...)` in any hand-built harness —
   `run_daily_backtest`'s own `_build_experts` does this automatically for a
   real trial, but a low-level harness (like
   `test_grid2_engine_paths.py`'s `_harness`, or `tests/backtest/
   test_equity_golden_run.py`) must do it explicitly.
3. A pure-option entry needs `config["entry_action"]` set (in addition to
   `entry_rules`) on the `DailyBacktestEngine` config or the engine submits
   with `submit_to_broker=False`, and every option order lands as
   `"(manual review, not submitted)"` — a `TradeActionResult`, never a real
   `TradingOrder` — instead of being priced/sized/filled.

#### Task 14a item 3 — results-comparability note

Recorded the way the 2026-08-04 CAR-scale change is recorded
(`docs/RUNBOOK-goal2020-grid.md` §7, "Three things make this grid's numbers
a new baseline, not a continuation"): each point below marks a NEW baseline
that must never be compared with numbers from before it, within a run or
across runs.

1. **Task-3 Black-Scholes mark fallback is a new option-results baseline
   (2026-09 grid-2 vs the 2026.08.0058 stage-1 deploy).** Before Task 3,
   an option position with no bar on a given day carried at
   `max(intrinsic, entry)` — a floor, not a market-implied mark. After Task
   3, the same barless day marks at `BS(bar iv)` first, falling through to
   the floor only when spot/iv/dte are unavailable. Since roughly a third to
   half of the trading days at LEAPS range have no bar for the held contract
   (design §1: 50-65% density; this task's own item-2 measurement corroborates
   sparsity at real symbols), this changes the DAILY equity curve — and
   therefore drawdown, Sharpe, and every fitness computed off it — for every
   option position that spans a barless day. **Stage-1 option numbers (any
   backtest/optimization run BEFORE this branch merges, tagged with
   `app_version` <= `2026.08.0058`) and grid-2 numbers (run AFTER this
   branch merges) are DIFFERENT BASELINES.** Never rank, average, or diff
   an option result across that line; re-run the comparison side you need
   on the new baseline instead.
2. **683c7379 restates `total_return`/`calmar_ratio` inside
   `stressed_results` — every fitness run with `--stress-spread-bps` set is
   a new baseline for those two metrics** (the stress was computed but
   silently discarded before this fix; a stressed run's `total_return`/
   `calmar_ratio` now differ from a pre-fix run of the identical genome on
   the identical data). Scope, checked directly against the tools this
   plan's grids use: `tools/run_screener_capband_matrix.py`'s goal2020/
   matrix3 invocations pass `--fitness consistent_annual_return` explicitly
   (`docs/RUNBOOK-goal2020-grid.md`'s own recorded command line) — NOT
   `total_return`/`calmar_ratio` — so **matrix3 and goal2020 are
   unaffected** by this change. A **bare** `run_screener_capband_matrix.py`
   invocation (no `--fitness` flag) DEFAULTS to `calmar_ratio`
   (`ap.add_argument("--fitness", default="calmar_ratio")`) and IS affected
   if it also carries `--stress-spread-bps` — a distinct trap from the
   metric-name one: the flag alone does not tell you which baseline a run's
   `total_return`/`calmar_ratio` belong to; check whether `--stress-spread-bps`
   was set and whether the run predates or postdates 683c7379.
3. **`perf_sample_bt.py`'s numbers before/after 4169ae47 are NOT
   COMPARABLE**, per that commit's own docstring: the harness's indicator
   provider was a bare `object()` for its whole prior life, so every
   BUY evaluated was NEVER SIZED OR SUBMITTED (`orders/run=0`) — the
   wall-clock measured rule evaluation with no order flow behind it, not
   the fill-engine/order-simulator cost the docstring claims to measure.
   Measured on the default 20x250 sample, best of 3: BEFORE 0.416s / 0
   orders, AFTER 4.796s / 649 orders — an 11x difference that is the fill
   engine finally running, not a regression. Any older recorded
   `perf_sample_bt` number is describing a different (and, per the harness's
   own purpose, broken) program.

#### Task 14a item 4 — design/plan doc updates: remaining pieces

**The aliased-import limitation of `getsource`-based pins (codebase-wide).**
Several suites (`test_strike_method_registry.py`,
`test_bs_mark_fallback.py`'s `test_risk_function_source_never_names_bs`, and
others) pin a call-graph invariant by reading a function/class's OWN source
text with `inspect.getsource` and asserting a literal name string is present
or absent in it (e.g. `assert "bs_price" not in src` on every reserve/margin
function, proving BS never reaches a risk number). This is a real, working
guard for the direct-call shape it was written against — but `getsource`
returns the SOURCE AS LITERALLY WRITTEN, not a resolved call graph: a
function that reaches the guarded target through an ALIASED IMPORT (`from
ba2_common.core.option_bs import bs_price as _compute_bs`, then calling
`_compute_bs(...)`) would leave the literal string `"bs_price"` entirely
absent from the guarded function's own source, and the pin would report
"clean" while the call still happens. None of today's call sites use this
form (verified for the BS/risk-function pin specifically as part of Task 3's
own review), so the guards are correct as they stand — this is a recorded
LIMITATION of the technique for the next agent adding a call site or a new
`getsource`-based pin, not a defect found in this branch's code.

**Task 2's 3 doc nits.** The plan's own amendment text (Task 14's
amendments block, item (c)) references "Task 2's 3 doc nits" as something to
carry forward, but no separate review artifact persisted in the repo (no
review-notes doc, no follow-up commit message enumerating them) — the trail
was searched (`docs/superpowers/`, the commit `2583e520` message in full,
and `tools/probe_option_chain_depth.py`'s own docstring) and none names three
specific items. Recorded here as NOT INDEPENDENTLY LOCATABLE rather than
invented or silently dropped; `tools/probe_option_chain_depth.py`'s
docstring and tests were re-read for clarity in the course of this task and
no defect or omission was found in them.

**`days_after_event` (forced) vs `days_opened` (discretionary), recorded as
DELIBERATE.** Already landed in code, verified present, no doc change
needed: `packages/common/ba2_common/core/TradeActionEvaluator.py`'s
`_FORCED_EXIT_EVENT_TYPES` carries a block comment on its
`N_DAYS_AFTER_EVENT` entry beginning "THIS IS CLASSIFIED OPPOSITE TO
`days_opened`, DELIBERATELY, AND THE DIFFERENCE IS NOT 'both count days'" —
`days_opened` is a STALENESS exit (nothing changed, the thesis hasn't paid,
so it stays discretionary), while `days_after_event` is the terminal date of
a binary event trade (the thesis is over once the print has passed and the
searched window has elapsed, so it pays up like `days_to_expiry`). This is
exactly the classification item 4 asks to have recorded — it already is, in
the classifier's own docstring, the most authoritative place for it.

**Rule-level `enabled: False` convention (never emitted; shared guard).**
Also already landed and verified present:
`packages/common/ba2_common/core/rules_convert.py`'s
`live_actions_from_trade_rule` docstring states the choke point plainly —
`rule.get("enabled") is False` converts to `None` (no action) UNCONDITIONALLY,
before anything else runs, on BOTH the backtest seeder path
(`default_rulesets.seed_ruleset_from_rules`) and the live export path
(`strategy_to_live_export`/`trade_rules_to_live_export`) — and the emit-time
half (`strategy_param_space._decode_rule_list` REMOVING an authored-off rule
rather than flagging it) is documented beside it. This is the "shared guard"
plan item 4's operator decision (e) refers to; already documented at its own
call site, no doc addition needed here.

#### Task 14a item 5 — ruleset editor

**Help text.** `packages/common/ba2_common/core/rules_documentation.py`'s
`get_action_type_documentation()` was missing entries for NINE
`ExpertActionType` members, not only the two backspreads the plan named:
`STOP_PROCESSING`, `OPEN_SHORT_STRADDLE`, `OPEN_SHORT_STRANGLE`,
`OPEN_IRON_CONDOR`, `OPEN_JADE_LIZARD`, `OPEN_CALL_BUTTERFLY`,
`OPEN_PUT_RATIO_SPREAD`, `OPEN_CALL_BACKSPREAD`, `OPEN_PUT_BACKSPREAD`. All
nine were backfilled (not only the two named) so the new coverage test
below is an unconditional ratchet rather than a partial guard carrying
pre-existing debt. The two backspreads carry the risk note the plan asked
for: max loss is bounded and the worst case pins at the **LONG** strike
(verified against the actual builder docstrings,
`TradeActions.py`'s `OpenCallBackspreadAction`/`OpenPutBackspreadAction` —
NOT the short strike, which a first draft of this entry got wrong and this
task caught before committing), and both are marked ARC-floor exempt
(`option_economics.ARC_FLOOR_EXEMPT_STRATEGIES` names `call_backspread`/
`put_backspread`).

**Test**: `packages/common/tests/test_rules_documentation_coverage.py` — every
`ExpertActionType` member has an entry (and the reverse: no stale entry
names a non-existent member); every entry has the expected shape (name/
description/use_cases/parameters/example, non-empty); the two backspreads'
entries are pinned to name the LONG strike and the ARC exemption
specifically.

**UI check — READ-ONLY** (per the brief's fallback: launching the worktree
app was not attempted, in favour of a faster, equally conclusive structural
check). `ba2_trade_platform/ui/pages/settings.py`'s ruleset editor is
ENUM-DRIVEN at both places that matter, verified by reading the source
directly:
- The per-row action-type dropdown builds its options as
  `{a.value: get_action_type_display_label(a.value) for a in
  ExpertActionType}` — a full enum iteration, not a hand-maintained list —
  so `OPEN_CALL_BACKSPREAD`/`OPEN_PUT_BACKSPREAD` were ALREADY selectable
  before this task; only their HELP TEXT was missing, which is exactly this
  item's scope.
- The row's foldable "Info" panel and the ruleset dialog's full
  "⚡ Available Action Types" reference section both call
  `get_action_type_documentation()` and render `doc['name']`/
  `doc['description']`/`doc['example']`/`doc['use_cases']` directly off
  whatever the dict returns — so the two new entries render with no
  settings.py code change needed.

**Parked item, noted (not addressed by this task): the `classic_options`
risk-manager rails have no UI path.** Already recorded in
`docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`: "the
settings dialog renders none of the sleeve rails, so no UI path configures a
live `classic_options` sleeve" — restated here because it is directly
adjacent to the ruleset-editor work this item touched, not because it is
new. Out of scope for Task 14a.

---

## Review cadence
After each task: spec-compliance review, then adversarial quality review with
at least one mutation re-run and independent re-derivation of any number the
task wrote into comments. After Task 14: one consolidated final review of the
branch. The controller dispatches reviews with the same worktree/method-trap
preamble as implementation briefs.
