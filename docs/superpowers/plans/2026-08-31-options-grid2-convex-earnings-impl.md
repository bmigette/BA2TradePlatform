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

#### DESIGN, 2026-09-02 (written BEFORE any code, per the controller)

**1. Where per-leg expiries live: they ALREADY live in the leg rows. NO column,
NO migration.**

The brief allowed for either answer and asked for evidence. The evidence says the
storage exists and has since revision `08de6c7b6eed`:

- `TradingOrder.expiry: date | None` — `packages/common/ba2_common/core/models.py:508`,
  in the per-leg option block (`contract_symbol`/`option_type`/`strike`/`expiry`/
  `position_intent`). Added to the physical table by
  `alembic/versions/08de6c7b6eed_add_option_fields_to_tradingorder.py:30`
  (`op.add_column("tradingorder", sa.Column("expiry", sa.Date(), nullable=True))`).
- A multi-leg structure persists **one `TradingOrder` child row per leg**, linked
  by `parent_order_id` (`models.py:481`), and
  `OptionsAccountInterface.submit_option_order` already writes each leg's own date:
  `expiry=leg.expiry` at `OptionsAccountInterface.py:421`. There is no leg JSON
  blob and no `OptionLeg` table — the child row IS the leg.
- Both in-memory leg value objects already carry it too: `OptionLeg.expiry`
  (`option_types.py:72`) and `LifecycleLeg.expiry` (`option_lifecycle.py:201`).
- Both DTE readers already consult per-leg dates today — `_dte`
  (`option_lifecycle.py:528`) and `DaysToExpiryCondition._held_legs`
  (`TradeConditions.py:3314`, feeding `_resolve_expiry` at `:3365`).

> Line references in this section were re-verified after the implementation
> commits, so they point at the post-task file (the numbers moved as the readers
> grew their rule docstrings). Accessor suite size, re-measured at the same time
> with `--collect-only`: **44** in `test_option_expiry_accessor.py`, and the split
> is **38 + 6** — the 38 that landed with req 1, plus the 6 `declared_expiries`
> cases req 3 added when the parameter went plural (3 new test functions, each
> parametrized over both named rules; `-k structure_level` collects exactly 6).
> The req-1 commit message's "38" was correct when written and is left as the
> historical record rather than rewritten.
>
> (The review note that prompted this refresh gave the split as "35 + 9". That
> arithmetic does not reproduce: `git show 8875abb9:` on the test file has 22
> `def test_` functions collecting 38 cases, and the plural-parameter additions
> collect 6. Recorded rather than copied, per the plan's numbers-discipline rule.)

So `Transaction.expiry` was never the *storage*; it is a denormalised
**structure-level summary** of a set that is already recorded per leg. The task is
therefore not "add per-leg storage" but "stop treating disagreement among the legs
as necessarily corrupt". `Transaction.expiry` keeps its exact single-value meaning:
it is filled only when the structure genuinely has ONE expiry, and for a declared
multi-expiry structure it stays NULL — the honest value — with the leg rows as the
record. Every existing builder is byte-identical (`len(expiries) <= 1` on every one
of the 16 supported structures, so every expression below evaluates as it does today).

**Migration id: NONE.** Alembic head stays `b7f3d21c98ae`
(`b7f3d21c98ae_add_risk_manager_run_table.py`). Requirement 5 becomes the
"no migration needed" proof it allows for: a test asserting the head is unchanged
and single, that the `transaction` table gains no column (`compare_metadata` drift
check, the `test_option_intent_migration.py` pattern), and that existing
single-expiry rows in a fixture DB read identically through the new accessor.

**2. The accessor** — new shared module
`packages/common/ba2_common/core/option_expiry.py` (source of truth; nothing in the
in-tree shim):

```python
MULTI_EXPIRY_OPTION_STRATEGIES: frozenset[str]        # the declaration (see 4)
def is_multi_expiry_strategy(strategy: str | None) -> bool

EXPIRY_RULE_ROLL_WINDOW  = "roll_window"      # -> the SHORT leg
EXPIRY_RULE_STRUCTURE_EXIT = "structure_exit" # -> the LONG leg

@dataclass(frozen=True)
class ExpiryLeg:            # one HELD leg, reduced to what an expiry question needs
    expiry: date | None
    net_qty: float          # signed, BUY +, SELL -; short is net_qty < 0

@dataclass(frozen=True)
class ExpiryResolution:
    expiry: date | None         # the answer, or None
    conflict: tuple[date, ...]  # the distinct candidates when unresolved, else ()
    missing: bool               # no candidate at all
    rule_applied: str | None    # which named rule picked it; None = single-expiry

def resolve_structure_expiry(legs, *, strategy, rule,
                             declared_expiries: Iterable[date | None] = ()
                             ) -> ExpiryResolution
```

It returns a RESULT, not a message. Each reader renders its own wording, so both
readers' existing "unknown because…" strings — which their suites assert on
verbatim — stay byte-identical. What is shared is the part that is actually risky:
the *selection*. `declared_expiries` holds the structure-level candidates, and it
is PLURAL because `DaysToExpiryCondition` reads two independent sources —
`Transaction.expiry` and the parent order's `expiry` — which can contradict each
other with no legs involved at all; collapsing them to one scalar would lose that
contradiction. `None` entries are ignored.

Resolution order: candidates = held legs' known expiries ∪ `declared_expiries`.
Zero → `missing`. Exactly one → that date, `rule_applied=None` (**a per-leg question
on a single-expiry structure returns the single expiry — no behaviour change**).
More than one → if the strategy is NOT declared multi-expiry, `conflict` (today's
refusal, unchanged); if it IS, apply the named rule and pick the nearest leg on the
requested side, `conflict` if that side has no leg (fail-closed: never fall back to
the other side).

**3. The named DTE rules.** Ambiguity is what the guard existed to prevent, so each
reader now states its side in code and in its docstring:

| reader | question it answers | rule | leg |
|---|---|---|---|
| `option_lifecycle._dte` | the roll window (`roll_dte`, "time to roll the overlay") | `EXPIRY_RULE_ROLL_WINDOW` | **SHORT** |
| `DaysToExpiryCondition` (the `opt_dte` exit) | the roll floor / structure exit ("is there still life to roll into") | `EXPIRY_RULE_STRUCTURE_EXIT` | **LONG** |
| the `opt_time` exit | elapsed days since open | — | **none** |

This matches design §4 exactly: "Roll loop: at short expiry"; "Structure exit:
long-leg DTE floor". `opt_time` is listed because requirement 3 names it: it reads
`days_opened` (`ba2test_launcher.py:3951-3954`), never an expiry, so it reads NO
leg — stated so nobody looks for a leg rule there. `opt_dte` reaches
`DaysToExpiryCondition` via `field: "days_to_expiry"` → `rule_builders.py:48` →
`TradeConditions.py:3656`.

**4. The multi-expiry declaration.** A module constant in shared code,
`MULTI_EXPIRY_OPTION_STRATEGIES = frozenset({"pmcc"})`, consulted by the guard AND
by both readers, so one list governs all three sites. Fail-closed by construction:
membership is opt-in, `None`/unknown/`""` is not a member, and an undeclared
two-expiry submit still raises the unchanged `ValueError`.

`"calendar_spread"` is deliberately NOT a member — `O_CAL` is phase-2 in
`_PHASE_GATED_OPTION_STRATEGIES` (`ba2test_launcher.py:2685`), and the
existing guard test submits two expiries tagged `calendar_spread` and requires a
refusal. Adding `"calendar_spread"` is the one-line change phase 2 makes.
`"pmcc"` has no builder yet (that is Task 6): this task opens the door, Task 6
walks through it, and `O_PMCC` stays phase-gated meanwhile.

**5. Both runtimes, one code path.** This is structural, not a convention. Verified
2026-09-02 with `grep -rn "def submit_option_order" --include=*.py . | grep -v
/tests/` — exactly TWO production definitions:

- `OptionsAccountInterface.submit_option_order` (`OptionsAccountInterface.py:237`)
  — the guard, the parent row, the per-leg child rows, the intent stamp;
- `BacktestAccount.submit_option_order` (`backtest_account.py:2085-2093`), a thin
  passthrough: `result = super().submit_option_order(*args, **kwargs)` followed by
  `self.invalidate_order_cache()`, so the fill engine's next read sees the new
  rows. It contains no expiry logic of its own.

`AlpacaAccount` does not override it at all. Both runtimes supply only the broker
hook `_submit_option_order_impl` (`backtest_account.py:3617`,
`AlpacaAccount.py:6122`). On the READ side,
`OptionRiskManagement.build_structure` was promoted precisely so "the live exit
pass and the shared entry gate build one book from one definition", and both DTE
readers reach `option_expiry.resolve_structure_expiry`.

The parity test therefore pins the live path by IDENTITY (`AlpacaAccount
.submit_option_order is OptionsAccountInterface.submit_option_order`) and the
backtest path BEHAVIOURALLY: `BacktestAccount`'s override is borrowed as a
function onto an account double (the technique `test_option_breaker_parity.py`
already uses) and must produce byte-identical per-leg rows and identical
accessor answers to the plain interface path, for both a declared PMCC and an
undeclared refusal.

> METHOD NOTE (2026-09-02, during implementation). A first "verification" of this
> paragraph ran the same grep piped through `head -10` and saw only
> `packages/common` test doubles — `testplatform` sorts after `packages`, so the
> one real override was cut off. That briefly produced a confident, wrong
> correction claiming no override existed. Recorded because the plan's
> numbers-discipline rule applies to design claims too: a truncated grep is not a
> measurement, and the fix (drop `head`, exclude `/tests/`) is what the line
> numbers above now rest on.

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

#### DESIGN, 2026-09-02 (written BEFORE any code, per the controller)

Measurements below were re-read on this branch at `571cd35c`; every line
reference is from that tree.

**0. The shape question the brief told me to STOP on, answered: the lifecycle
service does NOT fight the design, because the ROLL IS A RULESET RULE, not an
engine hook.**

`option_lifecycle.decide` has exactly one production caller —
`ba2_trade_platform/core/option_lifecycle_service.run_option_lifecycle_pass`,
the LIVE pass on the `JobManager` schedule. The BACKTEST engine never calls it
(`daily_engine.py:750` calls only `update_sleeve_breaker`), and the grid's
option exits are ruleset rules evaluated through `TradeActionEvaluator` by BOTH
runtimes (`daily_engine._manage_open_positions` at `:1230-1242`; the live
`TradeManager` through the same evaluator). So there are two ways to make the
roll happen and only one of them keeps every standing principle:

* an engine hook — a new per-bar step in `daily_engine` — would put the roll's
  knobs somewhere that is neither a ruleset param nor an expert setting, would
  need a second implementation for live, and would add per-bar work an equity
  trial has to be scoped out of by hand;
* **a rule** — `{condition on the SHORT leg} -> action roll_pmcc_short` in the
  OPEN_POSITIONS ruleset — puts the knobs in ruleset params (the audit test's
  own definition of a legal gene destination), runs identically in both
  runtimes because both walk the same ruleset through the same evaluator, and
  costs an equity trial exactly nothing because an equity ruleset has no such
  rule.

The precedent is `O_WHEEL`: `_build_strategy_wheel` already puts an option
ENTRY action (`sell_covered_call`) into the OPEN_POSITIONS ruleset, spliced at
the FRONT with `continue_processing: True` (`ba2test_launcher._insert_option_overlay`,
`anchor="front"`). The PMCC roll is the same shape.

`decide()` still gets the roll — see §6 — so the live pass does not close a
PMCC the moment its overlay approaches expiry. Both paths call the SAME pure
functions in `option_lifecycle`; that is the parity, and it is tested.

**1. The builder: `OpenPMCCAction`, action value `open_pmcc`, strategy tag
`"pmcc"` (already the sole member of `MULTI_EXPIRY_OPTION_STRATEGIES`).**

ONE `submit_option_order` call, two legs, two expiries, one parent + two per-leg
child rows (Task 6-PRE's storage, `OptionsAccountInterface.py:404-427`):

| leg | side | intent | selection |
|---|---|---|---|
| LEAPS call | BUY | `buy_to_open` | `select_single`, method `delta`, target = `_spread_params()[0]` (the LONG param), DTE window = `dte_min/dte_max` (365+) |
| overlay call | SELL | `sell_to_open` | `select_single`, method `delta`, target = `_spread_params()[1]` (the SHORT param), DTE window = `short_dte_min/short_dte_max` (fixed 30-45) |

Two chain fetches because the two DTE windows do not overlap; each is validated
by `_liq` on its own (the 2026-08-23 per-chain rule). `_build_and_submit`, not
`_resolve`, for the same reason the backspreads use it: the builder needs to
own its submit call (it stamps overlay facts, §4).

**ADMISSION: `short.strike > leaps.strike`, refused otherwise** — a
`self._result(False, ...)` verdict, not a raise (the operational-refusal
channel: the structure on offer is not a PMCC today). A short at or below the
long strike is a bear call spread wearing a diagonal's name, and its worst case
is not the LEAPS debit.

**Sizing** is premium sizing on the net debit: `cost_per_contract =
net_debit * 100`, `net_debit = leaps.ask - overlay.bid`. A non-positive net
(the overlay pays for the LEAPS outright) is refused: it is not the structure.

**No `option_reserve`.** A diagonal whose long expires AFTER the short is
margined as covered; `RESERVING_STRATEGIES` deliberately does not gain `"pmcc"`.

**2. Max loss = LEAPS debit − net credit, and the EXISTING stamp already
computes it.** `_measured_max_loss_per_contract(legs, net_debit)` builds
`PayoffLeg`s carrying the net on one carrier leg (`_entry_payoff_legs`,
`TradeActions.py:1885-1934`) and asks `option_payoff.max_loss`. For BUY call
K1 / SELL call K2>K1 at net debit `d` the upside slope is 0 (not UNBOUNDED),
the critical points are `{0, K1, K2}`, and the worst is `-d*100` at spot 0 —
i.e. **MEASURED, `d*100` per contract**, which is exactly design §3's intrinsic
floor ("at the short's expiry the long is worth >= intrinsic; max loss = LEAPS
debit − net credits"). Worked by hand in the test: LEAPS 20.00 ask, overlay
3.00 bid -> net 17.00 -> stamp 1700.00.

The payoff evaluator reads both legs as expiring together. That is not a bug
here, it is the conservative reading the design asked for, and it is stated in
the builder's docstring.

**CHARGED COVERED, NEVER NAKED, and no `stock_cover_price` is passed.**
`structure_metrics` pairs the short call with a long of the SAME right, so a
PMCC reports `is_defined_risk=True` and `committed = width * 100` with
`naked_committed` untouched (`option_book._is_undefined_risk`,
`option_book.py:486-514`). The cover here is an OPTION, inside the order's own
legs — which is why the covered-call seam's `stock_cover_price` argument is
NOT reused: that argument exists for cover the order's legs cannot see, and
passing it on a debit call spread would ADD the full stock value to the
measured loss (`1700 + 100*C`). **This is what "extends the covered-call
stock-leg fix, cover kind = long option" means in practice: the same seam
(`_measured_max_loss_per_contract` -> `admit_option_entry`), the same
covered-vs-naked question, answered from the legs instead of from an argument.**

**3. The roll, as two rules and one action.**

Nested OR groups are NOT evaluated: `rule_builders.tree_leaves` flattens every
leaf of a rule into one ANDed trigger dict (`rule_builders.py:168-217`,
`TradeActionEvaluator._evaluate_conditions:906`), and `rules_convert._or_branches`
exists precisely to split a top-level OR into N rules. "Roll at short expiry OR
at the buyback trigger" is therefore TWO rules, which is also the launcher's own
stated idiom ("Its OWN rule, not another leaf on opt_time").

```
pmcc_roll_dte      short_leg_days_to_expiry <= N   ->  roll_pmcc_short   (not toggleable)
pmcc_roll_buyback  credit_decayed_pct       >= P   ->  roll_pmcc_short   (toggleable)
```

Both are spliced at the FRONT of `_option_exit_rules("O_PMCC")` with
`continue_processing: True`, so a bar that rolls still reaches the close rules
behind it. `pmcc_roll_dte` is NOT toggleable for the same reason `opt_event` is
not on `O_ERN`: with the roll off, a PMCC is not a PMCC, it is a diagonal
waiting to be assigned.

**What the roll order IS: one multi-leg order on the SAME transaction**, tagged
`option_strategy="pmcc_roll"`, legs in this order:

1. `buy_to_close` the currently-held short (its own expiry),
2. `sell_to_open` the newly selected short (the next expiry).

One order, so the two legs cannot half-happen at the account level; the ORDER
of the legs is the fail-closed statement and is checked by a pure function
before submit (§7). `"pmcc_roll"` joins
`OptionsAccountInterface.NON_INTENT_STRATEGIES` so a roll can never re-stamp
the transaction's intent.

**Structure identity survives the roll** because nothing about it moves: same
`Transaction`, same entry parent `TradingOrder` (which is what
`existing_order` resolves to on every later bar —
`daily_engine._oldest_entry_order` -> `_entry_order_for_transaction`), same
`transaction_id` on every roll row. The rolled short is a NEW child order under
the ROLL parent, not under the entry parent — which is exactly why the close
path has to change (§5).

**The new short's selection parameters are NOT new genes.** They are read back
off the entry order's persisted `data` (§4). One gene, one thesis: a separate
`option_strike_delta` on the roll action would let the GA enter at a 0.15 delta
and roll to a 0.30, and would double the overlay's gene budget at pop 40.

**4. Entry facts: the overlay spec rides the entry order row.** Same idiom as
`earnings_event_date` and `entry_cross` — the row is the only thing that travels
with the position. `_submit_option_order` grows ONE argument,
`extra_entry_facts: Optional[Dict]`, merged into `data` AND into the persisted
`entry_facts` unconditionally: the whitelist trap documented at
`TradeActions.py:2970-2974` is real, and an explicit argument is how a
builder-specific fact closes it instead of tripping over it.

Stamped under `option_lifecycle.ORDER_PMCC_OVERLAY_KEY = "pmcc_overlay"`:
`strike_method`, `strike_param` (the SHORT target), `dte_min`, `dte_max`,
`min_open_interest`, `max_spread_pct`, `min_volume`. The roll action REFUSES
when the key is absent — that position was not opened by `open_pmcc`, and
guessing the overlay spec is exactly the fabricated-input this codebase refuses.
`entry_cross` is read from its own already-persisted key, so the roll's buyback
concedes the same fraction the entry did (the F7 rule for a DISCRETIONARY close;
a scheduled roll is not a risk exit).

**The restamp.** After a roll the accrued credit changes the intrinsic floor:

```
max_loss_new = max_loss_old + roll_net * 100        # roll_net signed: +debit / -credit
```

A roll that nets a credit `c` lowers the stamp by `c*100`; a roll that costs
more to buy back than the new sale brings in RAISES it. Clamped at `0.0`, and
`0.0` means the accrued credits have paid for the LEAPS — `loss_pct_of_max_loss`
then self-disarms (`per_contract <= 0 -> None`), which is the right answer: a
structure with no defined loss left has no loss percentage. Written by
merge-not-replace onto the ENTRY parent order's `data` (the row
`LossPctOfMaxLossCondition._defined_risk_dollars` reads, `TradeConditions.py:1882-1884`).

**5. The exit paths, and how each closes BOTH legs.**

| exit | rule / field | leg it reads | action |
|---|---|---|---|
| long DTE floor | `opt_dte` / `days_to_expiry` | LONG (6-PRE `EXPIRY_RULE_STRUCTURE_EXIT`) | `close_option` |
| max-loss stop | `opt_sl_ml` / `loss_pct_of_max_loss` | — (the restamped denominator) | `close_option` |
| delta floor | `pmcc_delta_floor` / `long_leg_delta` | LONG | `close_option` |
| take profit | `opt_tp` / `profit_loss_percent` | structure | `close_option` |
| elapsed time | `opt_time` / `days_opened` | — | `close_option` |
| manual / breaker | `CloseOptionAction`, `LIFECYCLE_BREAKER` | — | `close_option` |

Every one of them is the SAME action, `CloseOptionAction._close_multi_leg`,
which submits ONE multi-leg order flattening the structure. Atomicity is
therefore a property of the order, not of a sequence — but it only holds if
that order actually names the CURRENTLY-held legs, and today it does not:
`_close_multi_leg` enumerates `children` of the ENTRY parent
(`TradeActions.py:4901-4913`). After a roll the live short is a child of the
ROLL parent, so the existing code would close the LEAPS and leave the rolled
short NAKED. **That is the invariant's real failure mode and the change that
fixes it:** the closing set is the transaction's HELD contracts (the `held_qty`
netting already computed at `:4929-4956`), with the entry children first (so
every existing structure's leg list is byte-identical) and any held contract
they do not cover appended from its own order row.

**6. `decide()` — the live pass gets the roll too.** For a structure whose
strategy is declared in `MULTI_EXPIRY_OPTION_STRATEGIES`, the `roll_dte` branch
yields the NEW, NON-closing reason `LIFECYCLE_ROLL_SHORT` instead of
`LIFECYCLE_ROLL_DTE`. `_dte` already reads the SHORT leg there (6-PRE), so
without this change the live pass would CLOSE a PMCC every time its overlay
came within `roll_dte` — a LEAPS with a year left, thrown away on schedule.
The buyback trigger is read from an OPTIONAL setting
(`pmcc_buyback_pct`, present/absent by the `SETTING_BREAKER_TRIPPED` idiom), so
`option_lifecycle_service.REQUIRED_SETTINGS` and every live expert's settings
are untouched. `LIFECYCLE_ROLL_SHORT` is deliberately NOT in
`LIFECYCLE_CLOSING_REASONS`.

**7. THE INVARIANT, as three pure functions in `option_lifecycle` that both
runtimes call.**

```python
def uncovered_short_calls(legs) -> Tuple[str, ...]
    # net-short call contracts with no net-long call cover, after netting
def close_legs_are_fail_closed(legs) -> Optional[str]
    # every buy_to_close of a short must precede any sell_to_close of a long
def roll_legs_are_fail_closed(legs) -> Optional[str]
    # the buy_to_close of the old short must precede the sell_to_open of the new
```

Fail-closed means: the long is released only after the short's buyback is on
the same ticket ahead of it, and a roll whose new short cannot be selected
leaves the long ALONE (covered by nothing — allowed; a naked short is not).
The two ordering functions return a REASON string, and both the roll action and
the PMCC close path refuse on a non-`None` answer. The ordering rule is applied
ONLY to a declared multi-expiry structure: for a single-expiry combo every leg
settles at one expiry and the order inside the ticket carries no risk meaning,
so applying it there would move existing structures' leg lists for nothing.

**8. Marks (Task 3 hierarchy), per leg.** Nothing new is needed and nothing is
added: `_option_positions_mtm` already marks EVERY lot through
`bar close (no-arb clamped) -> BS(last_iv <= 5 days) -> intrinsic-floored entry
premium` (`backtest_account.py:513-647`). `"pmcc"` is deliberately NOT added to
`DEFINED_RISK_LONG_STRATEGIES`/`DEFINED_RISK_SHORT_STRATEGIES`, and that single
omission carries two consequences the design needs:

* the two legs are marked INDEPENDENTLY (the non-defined-risk branch), so
  structure value = long mark − short mark with no group clamp to a "width"
  that a two-expiry structure does not have;
* `defined_risk_combo_strategy` returns None, so `_apply_option_expiry` settles
  the overlay PER LEG at ITS OWN expiry and the LEAPS keeps living. A
  unit-settled combo would close the LEAPS on the overlay's expiry day — the
  exact opposite of the design.

Structure P&L for the exit conditions is already correct through
`_get_spread_pnl_via_transaction`: it nets EVERY executed option row on the
transaction (so each roll's realised credit folds into `cash_collected`) and
marks the still-held legs long-at-bid / short-at-ask.

STATED LIMITATION, not fixed here: `get_option_quote` is exact-date-or-None
(`options_provider.get_quote`), so on a bar with no option bar the equity curve
is BS-marked while every exit CONDITION declines to evaluate. That asymmetry is
pre-existing (Task 3 scoped itself to the mark path) and applies to `O_LEAP`
identically; it is recorded here because a PMCC at LEAPS-range sparsity meets it
on ~half its bars.

**9. Two-expiry submits that are not entries: the guard learns to ask the
TRANSACTION.** The close of a PMCC carries two expiries under
`option_strategy="close"`, and the roll carries two under `"pmcc_roll"` —
neither is a member of `MULTI_EXPIRY_OPTION_STRATEGIES`, so today's guard
(`OptionsAccountInterface.py:308-318`) would refuse both and a PMCC could be
opened and never closed. The declaration is therefore resolved as: the
`option_strategy` argument, OR — when a `transaction_id` is supplied — the
TRANSACTION's own recorded `option_strategy`. Fail-closed and unchanged
everywhere else: no `transaction_id` behaves exactly as today, and a
transaction whose own strategy is undeclared still refuses. One declaration,
consulted at every site.

**10. Genes (design §2), and where each lands.**

| gene | range / step | destination |
|---|---|---|
| `option_strike_delta` (overlay) | 0.15–0.30 step 0.05 (4) | action `strike_param[1]` |
| `option_strike_delta_long` (LEAPS) | 0.75–0.85 step 0.05 (3) | action `strike_param[0]` |
| `option_dte` (LEAPS window centre) | 410–500 step 15 (7) → windows [365,455]..[455,545] | action `dte_min`/`dte_max` |
| `option_sizing` | the shared option band | action `sizing` |
| `cond:roll_dte:value` | 1–7 step 1 (7) | leaf `short_leg_days_to_expiry` |
| `cond:roll_buyback:value` (+ `exit:pmcc_roll_buyback:enabled`) | 50–90 step 10 (5) | leaf `credit_decayed_pct` |
| `cond:delta_floor:value` (+ `exit:pmcc_delta_floor:enabled`) | 0.40–0.60 step 0.05 (5) | leaf `long_leg_delta` |
| `cond:dte:value` (`opt_dte`, the LEAPS roll floor) | 90–240 step 30 (6) | leaf `days_to_expiry` |
| plus the shared entry gates, `opt_tp`, `opt_time`, `opt_sl_ml` | as every grid-2 key | |

**FIXED, deliberately not genes:** the strike METHOD (`delta`, like every
grid-2 key — `_FIXED_DELTA_METHOD_STRATEGIES`), and the OVERLAY DTE WINDOW
(30–45, design §2's own numbers). The overlay window is one narrow band the
design states as a constant rather than a range, and at pop 40 / gen 6 the gene
budget belongs to the two deltas and the roll trigger. `opt_sl_ml` is AUTHORED
OFF by the removal idiom (`_OPTION_SL_ML_AUTHORED_OFF`) and still searched.

**11. Registry surface (each one a place a new action/field is silently
dropped if missed).** Actions `open_pmcc`, `roll_pmcc_short`:
`ExpertActionType`, `get_option_action_values`, `get_strike_method_action_values`,
`create_action`'s map, `TradeActionEvaluator._get_action_type_from_action` +
`priority_map`, `rules_documentation.get_action_type_documentation`. Fields
`short_leg_days_to_expiry`, `credit_decayed_pct`, `long_leg_delta`:
`ExpertEventType`, `get_numeric_event_values`, `TradeConditions.CONDITION_MAP`,
`rule_builders.FIELD_EVENT`, `rules_documentation.get_event_type_documentation`,
`rules_export_import._FIELD_ABBR` (checked for abbreviation collisions).
`long_leg_delta` joins `_FORCED_EXIT_EVENT_TYPES` (a broken-thesis structure
exit pays up, like the DTE floor); `short_leg_days_to_expiry` does NOT — no
emitted rule closes on it, and classifying a field nothing reads is the decoy
that table's docstring warns about.

`HistoricalOptionsProvider.get_quote` (and its parquet twin) start populating
`OptionQuote.delta`/`implied_volatility` from the bar row they already read —
`long_leg_delta` has no other point-in-time source, `OptionQuote` already
declares the fields, and no existing consumer reads them.

**12. Count pins this moves** (each updated with the arithmetic shown, never
adjusted-until-green): `test_gene_to_artefact_audit.py`'s `len(OPTION_KEYS)`,
`test_option_strike_method_honoured.py`'s two counts,
`test_option_ui_param_reachability.py`'s option-action count, and
`test_option_grid_foundations.py`'s grid-2 genome-size table. No new import is
added ABOVE `TradeActions.py:1548` (`tests/test_no_zero_coercion.py` pins that
exact line) — the new code uses the file's existing function-local import idiom.

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
| **ratio** | **5.3x-8.1x across 6 repeated runs** | | |

The six logged cold ratios: 5.31, 5.58, 6.32, 6.54, 6.80, 8.13 (a prior draft
of this note said "5.3x-11.5x" — the 11.5x was wrong, produced by
cross-pairing the cProfile-instrumented run's OPTION time against an
unpaired, different run's EQUITY minimum instead of that same run's own
EQUITY leg). Reviewer's independent reproduction (2026-09-02): 6.37x.

**Result — WARM (second+ trial on the SAME symbol in the SAME process),
STATED PRECISELY:** OPTION 0.169 s vs EQUITY's own 0.143-0.179 s. This
number is real but **does NOT establish "a full trial is comparable warm"**:
every timing run in this measurement — cold and warm alike — ended with
`open_option_positions=0` (see the profile below; no order ever filled), so
NONE of these numbers include the cost of PER-BAR POSITION MARKING on a held
option (the BS-fallback/greeks work item 2's design references as the other
half of a real trial's cost). The 0.169s warm number is the amortized
CANDIDATE-SELECTION cost only, not a full held-position trial. See the
STATE note's Open-items entry below for the amortized-cost table and the
open question this leaves for launch readiness.

**VERDICT: criterion 2 FAILS on the cold path; the warm path's
candidate-selection cost is comparable, but no measurement here covers a
full, position-holding trial.** The plan's acceptance criterion ("option
trial runtime stays comparable to equity trial runtime") was written without
this cold/warm distinction and is not met as a blanket claim. **Not fixed
here** (profiling only, per the controller's decision) — flagged as the
input an optimization task needs, not a task this branch completes.

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

1. **Task-3 Black-Scholes mark fallback (`cc337f9e`) is a new option-results
   baseline (2026-09 grid-2 vs the 2026.08.0058 stage-1 deploy).** Before
   Task 3,
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

**Rule-level `enabled: False` convention (never survives into an EMITTED
ruleset; shared guard).** CORRECTED (2026-09-02 review): the launcher DOES
stamp `enabled: False` onto its own AUTHORED rule TEMPLATE
(`ba2test_launcher.py` ~:3952/:4027, e.g. `_OPTION_SL_ML_AUTHORED_OFF`) —
"never emitted" overstated that. What is true, and verified present:
`strategy_param_space._decode_rule_list` (~:616-635) REMOVES an
authored-off rule at DECODE time (a default/unsearched genome drops it
outright; the GA's own gene can still turn it back on), and
`packages/common/ba2_common/core/rules_convert.py`'s
`live_actions_from_trade_rule` docstring states the shared fail-closed
choke point plainly — `rule.get("enabled") is False` converts to `None` (no
action) UNCONDITIONALLY, before anything else runs, on BOTH the backtest
seeder path (`default_rulesets.seed_ruleset_from_rules`) and the live
export path (`strategy_to_live_export`/`trade_rules_to_live_export`). So the
accurate claim is: a rule authored `enabled: False` never SURVIVES into an
emitted ruleset (the one actually seeded/exported), even though the flag
itself is real on the pre-decode template. This is the "shared guard"
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

---

## STATE — completion note (Task 14a item 6, 2026-09-02)

### What landed (SHAs, Tasks 1-14a)

Tasks 1-9 landed before this docs/closeout pass (`56c3f8c2..746d59fd` range,
see `git log --oneline 56c3f8c2..HEAD` for the full trail). From Task 10
onward, the SHA each task's own "LANDED AS" note above cites:

| Task | Landed as (primary SHAs) |
|---|---|
| 10 — launcher wiring + matrix script | `746d59fd`, `9dd116e8` |
| 11 — FMPEarningsEvent gene table | `4e8791ab` |
| 12 — `option_convex` fitness | `79087da8`, `8708786a` |
| 13 — `O_CONVEX` key + convex matrix | `967db2aa`, `64981161` |
| 14b (code-side) | 10 commits: `4f3cb7a6` + the 9 commits in `4f3cb7a6..4568a71f` (includes `8b705c58`, the O_LEAPC/O_LEAPP -> O_LEAP merge cited by Task 10's own "LANDED AS" note above — kept out of Task 10's row here so the two rows stay disjoint) |
| 14a item 1 — merge origin/dev | `5246bc49` |
| 14a item 2 — PRE-LAUNCH PERF | `db6924b6` |
| 14a item 3 — results-comparability note | `d1b269c3` |
| 14a item 4 — design/plan doc updates | `a04148f5` |
| 14a item 5 — ruleset editor (help text + coverage test) | `88eaf945` |
| 14a item 6 — this STATE note + EXPERTS.md | `7e73e85a` |
| 14a item 7 — final verification | (this commit) |

Also verified ALREADY LANDED, needing no new work this task: the `O_CAL`
phase-gated stub (refuses loudly with the design reference, like the naked
exclusion — `_PHASE_GATED_OPTION_STRATEGIES` in `ba2test_launcher.py`, wired
as part of Task 10's `746d59fd` launcher build). CORRECTED (2026-09-02
review): verified by its OWN tests,
`test_a_phase_gated_key_refuses_with_the_plan_reference` /
`test_a_phase_gated_key_is_a_KNOWN_key_that_refuses` in
`test_option_grid_foundations.py` — NOT "re-verified structurally by
`abe3686e`/`4568a71f`" as an earlier draft of this note claimed; those two
Task 14b commits only EXCLUDE the already-gated keys from their own
(different) structural audits, they do not independently verify the O_CAL
stub itself. The `days_after_event`/`days_opened` DELIBERATE
classification and the rule-level `enabled: False` removal convention (both
documented at their own call sites — see item 4 above).

### Baselines — FINAL (Task 14a item 7, re-measured end-to-end 2026-09-02,
after every item 1-6 commit on this branch)

| Suite | Result |
|---|---|
| backend `pytest tests/` (from `testplatform/backend`) | 4446 passed / 158 skipped / 5 failed (all 5 the known `curve_uneven` float-dust frozen-baseline cases — no memory-flake failures this run, unlike item 1's measurement under concurrent grid load) |
| `packages/common` (run from its own dir) | 2937 passed / 1 known (`test_portfolio_allocation_wizard.py` float-dust; +4 vs item 1's count from this task's own `test_rules_documentation_coverage.py`) |
| `packages/experts` | 885 passed / 0 failed |
| `packages/providers` | 447 passed / 0 failed |
| `tests/backtest/test_equity_golden_run.py` | 3 passed (fingerprint unmoved) |
| `tests/test_strategy_fitness_equity_frozen.py` | 809 passed / 5 failed (known `curve_uneven` cases, same as the full backend run) |
| `tests/test_strategy_fitness_option_car.py` | 58 passed / 0 failed |
| `tests/test_strategy_fitness_convex_frozen.py` | 167 passed / 0 failed |
| Root `tests/` (from the worktree root, foreground, ~5m15s) | 4483 passed / 51 failed — the exact known-bad set (`test_portfolio_allocation_page.py`, `test_option_intent_migration.py`, `test_tastytrade_account.py`, `test_broker_sdk_pins.py`), nothing new |

**No new failures anywhere. Every failing test is on the ground rules' own
known-bad list or a previously-recorded frozen-baseline float-dust case.**
The item-1 backend run's 3 extra `test_worker_server.py` failures (real,
momentary low system memory under concurrent grid load — 1.53GB/63.71GB
free at the time, matching the tests' own captured "1.4% memory free"
message) did not reproduce on this final run; they were a real environment
condition, not a code defect, exactly as recorded in item 1's commit.

### Open items

- **Item 2's perf verdict (criterion 2 FAILS cold) is a launch-readiness
  input, not resolved here — read item 2 above in full before deploying
  anything off this branch.** Amortised-cost table (reviewer analysis,
  2026-09-02, from item 2's own cold/warm numbers):

  | scenario | recycle interval | amortised cold tax | as % of a ~159s option trial |
  |---|---|---|---|
  | cold cost per symbol | — | ~0.80 s (0.973 − 0.169 measured) | — |
  | local pool | every 8 individuals | 78.4 s / 8 = ~9.8 s per trial | ~6% |
  | distributed path | once per GENERATION | ~1.3 s per trial | ~0.8% |

  So the cold tax is real but single-digit-percent once amortised across a
  real population/generation, and is NOT by itself a launch blocker at
  either recycle cadence. **The warm "~1.0x-1.2x" claim in item 2 does NOT
  establish that a FULL trial is comparable**: `open_option_positions=0` in
  EVERY timing run (cold and warm) means no run ever held a position long
  enough to price per-bar marking — the 0.169s warm number is the
  candidate-selection cost alone, not a full trial's cost. **Operator's open
  question, now under test by a short GA probe** (per-reviewer instruction,
  2026-09-02): whether the parquet options store, once loaded, holds RAW
  frames that get RE-FILTERED per bar (real pandas cost repeated every
  candidate evaluation) versus a PRE-PROCESSED, directly-usable structure —
  the same shape the equity path already has via its OHLCV preload +
  worker-level memo. **Launch readiness on performance waits for that
  probe's answer**, not on this task's own measurement alone.
- **`origin/dev` has advanced to `TEST_APP_VERSION = "2026.09.0010"`** since
  this branch's merge commit (`5246bc49`, which landed `dev`'s then-current
  `"2026.09.0009"`) — re-check before the merger's own version bump so the
  bumped number is not stale relative to `dev` at merge time.
- **Task 6-PRE and Task 6 HAVE NOW LANDED** (2026-09-02, on the
  `options-grid2` worktree branch). Per-leg expiries persist through the leg
  rows with no migration (6-PRE); the PMCC builder, the roll loop, the
  leg-pair invariant and the `O_PMCC` launcher key are Task 6. **`O_PMCC` is
  un-gated and launchable; `O_CAL` is still phase-gated**, by design §2's own
  decision to hold the calendar behind PMCC proving this machinery in a real
  run — and it is one line away (`"calendar_spread"` into
  `option_expiry.MULTI_EXPIRY_OPTION_STRATEGIES`, plus an `_OPTION_STRATS`
  row). The launchable option-key count moved 28 -> 29 accordingly
  (`test_gene_to_artefact_audit.py`).
- **Parked operator items** (carried forward, not resolved by this task):
  - `OS2` (the neutral-credit screener group) resolves to `[O_IC]` alone —
    `O_CSP` (cash-secured put, also full-notional/neutral-ish) is excluded
    from the group by the same naked-vol/full-notional filter that dropped
    `O_SSTG`/`O_SSTD`. Whether `O_CSP` should ever join `OS2`, or stay a
    standalone key, is an open design question (`ba2test_launcher.py`
    ~line 3705's own comment carries the detail) — not decided here.
  - `classic_options` risk-manager rails have no UI path (settings dialog
    renders none of the sleeve rails) — see item 5 above.
  - The `option-selection-modes` branch's work (through `56c3f8c2`, the
    base this branch was cut from) has been MERGED into `dev` but is not
    yet DEPLOYED to any live `ExpertInstance` — a live deploy of that stack
    remains a separate, pending operator action, unrelated to this branch's
    own merge/deploy status.
- **The 0-orders open question from item 2** (TradeRule-shaped rulesets
  through `run_daily_backtest` with real FMPRating on AXP) is unresolved —
  see item 2's note above for the evidence gathered and why "signal
  absence" is the more likely explanation.

### Who owes the `TEST_APP_VERSION` bump

**The merger**, per `CLAUDE.md`'s versioning table (`packages/` changes bump
`testplatform/version.py`'s `TEST_APP_VERSION`, not
`ba2_trade_platform/version.py`) — this branch touches only
`packages/`/`testplatform/`. **ONE bump, at a matrix3 job boundary, never
mid-run** (per the standing distributed-worker convention: workers compare
`TEST_APP_VERSION` alone to decide whether to self-update, so a mid-run bump
would fragment a running grid's workers onto different code). This branch's
own merge commit (`5246bc49`) took `dev`'s already-bumped
`TEST_APP_VERSION = "2026.09.0009"` verbatim and did not bump it further —
consistent with the ground rules ("No push, no `version.py` edits — the
merger owes the bump").

**`run_screener_capband_matrix.py` default-fitness note** (carried from
item 3): a bare invocation of that script (no `--fitness` flag) defaults to
`calmar_ratio`, not `consistent_annual_return` — the merger/operator should
check any NEW invocation (as opposed to the already-recorded matrix3/
goal2020 commands, which pass `--fitness consistent_annual_return`
explicitly) for whether it relies on the default before comparing its
numbers with either running grid's baseline.
