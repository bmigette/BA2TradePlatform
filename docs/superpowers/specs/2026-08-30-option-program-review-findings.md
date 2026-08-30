# Option Program Review — Findings (2026-08-30)

Four independent fresh-eyes reviews of the ENTIRE option strategy/grid program, run on a
different model (Fable) than the one that built it, one per facet: strategy & economics,
grid/GA search design, backtest engine correctness, risk/lifecycle/selection stack. Every
finding below was labelled VERIFIED (checked against code/probes) or SUSPECT by its
reviewer; that labelling is preserved. Scope: all five option design docs, three plans,
the running stage-1 grid, the main-tree engine/rails, and the `option-selection-modes`
branch (Tasks 1-6).

**Method note.** The reviewers were explicitly briefed to hunt the "confidently repeated,
never verified" claim class after the naked-short-put error survived three documents. They
found more of the same class — and several places where the *prose is doing work the code
cannot do*.

---

## Verdict in one paragraph

The per-structure code is in materially better shape than the documents describing it:
reserves are broker-empirical where it matters, unknown-never-reads-as-zero is genuinely
enforced, the selection stack survives adversarial probing, and the fill path is guarded
against junk quotes. The defects concentrate at the JOINTS: stage-1→stage-2 seeding does
not exist, the capital analysis measured the wrong gate, the fitness metric's incentive
gradient points somewhere its docs deny, live entry rails and the circuit breaker are dead
code, and every NON-fill touchpoint (marks, expiry settlement, forced buybacks) consumes
the quoteless data raw with no analogue of the arb guard that protects fills. Net: stage-1
results are usable as *relative rankings within a payoff family*, not as absolute
economics, and cross-family comparisons are biased in known directions. Live deployment
beyond a small, defined-risk, single-expert book would be trading on the design's promises
rather than its code.

---

## P0 — results-invalidating or money-wrong. Fix before trusting any grid number.

**F1. Long-ITM expiry settles at the raw bar close — no intrinsic floor, no arb guard.**
`backtest_account.py:3603-3605`. The arb guard exists because this cache carries junk
prints (its own docstring: a $0.01 call against $54 intrinsic); it protects FILLS only.
The expiry path realises the junk directly: a deep-ITM long call can be "sold" for $0.01
against $50 intrinsic. Shorts are immune (exact intrinsic via strike), so this is an
ASYMMETRIC loss generator against every debit arm — long calls/puts, verticals' long legs
at per-leg settlement, JL/ratio wings. One-line fix: clamp settlement premium to
[intrinsic, spot/strike] — the bounds the arb guard already encodes. Same bound needed at
`_liquidate_option_lot:1290` and the mark at `:537`. (Engine, VERIFIED mechanism.)

**F2. A dead book can measure dd < 100% and escape the wipeout sentinel.**
`backtest_account.py:546-547`: a NON-defined-risk lot with no premium bar this tick is
marked at its ENTRY premium. Defined-risk legs got the intrinsic fallback; the dangerous
shapes (CSP, strangle, straddle, JL, ratio — exactly what the clamp excludes) did not. A
deep-ITM naked short stops printing daily candles precisely when it matters, so a
-$5,000/contract liability can be marked at its $150 entry credit for weeks: equity
overstated, drawdown understated, margin breaches masked, and runs ending with such
positions book the overstatement into final equity and the trade rows (`:2854-2873`).
This is the ONLY found path around `dd>=100 -> WIPED_OUT_SENTINEL` (db56fb17). Fix:
extend the intrinsic fallback (floor for shorts) to all option lots. (Engine, VERIFIED
path / SUSPECT frequency — measure bar-missing rate for held contracts, see F19.)

**F3. Stage 2 cannot run as designed — three independent grounds, all verified.**
(a) Seeding: `warmStartFromOptimizationId` is a single int; no multi-source merge exists;
`genetic.py:472` fills absent genes with the gene's min, so a seeded winner arrives with
every member `enabled` toggle = 0 — OFF. (b) Three of 18 members do not fit the group
machinery: O_CC/O_PP/O_WHEEL have no `_OPTION_STRATS` row (KeyError in
`_build_strategy_option_group`), and a Strategy carries ONE `entry_action` flag — mixed
option-entry/equity-entry groups are architecturally unsupported, not just unimplemented.
(c) Per-member exit genes are UNPREFIXED (`cond:tp:value`), so 18 winners carry 18
conflicting values for the same stage-2 key; the "~280 of ~300 genes arrive near-optimal"
premise is false in both directions. Also unspecified: which EXPERT stage 2 runs under
(stage 1 produces winners under two). (Grid, VERIFIED.) Decide the substrate BEFORE
spending more stage-1 CPU-months whose only non-recorded output is seeds.

**F4. The running stage-1 is misconfigured on four axes, at a 4-12 MONTH timescale.**
Measured from the run report's own timestamps: gen-3 throughput is 12-14 trials/hr and
falling; at the design's ~180k trials stage 1 is months, not the "weekend or a fortnight"
framing. Meanwhile: (a) `stage1_run.sh` omits the design's own universe caps — 5 of 36
jobs (O_CSP/O_JL/O_RS uncapped; O_SSTD/O_SSTG no $300 cap) burn budget §6 explicitly
priced as waste, and their verdicts confound structure quality with affordability;
(b) O_CC/O_PP jobs optimize `sharpe_ratio`, not `option_car` — their "winners" will be
the low-trade artifacts the metric family is documented to produce, seeded into a
composition ranked on option_car; (c) elitism is 0.1% -> exactly 1 elite (the launcher
passes elitismPercent 0.1 at :3943/:4149 — the ENGINE honors the parameter, the
LAUNCHER's value is the bug; the two facet reports disagree only because they read
different layers); (d) job 1's best fitness was found in GENERATION 1 and is unchanged
through gen 3 — if that persists to early-stop, the "winner" seeded onward is a
random-init sample, not a search result. Additionally the 20:55 restart did NOT resume
from checkpoint despite one existing — undiagnosed, and material at months-scale.
(Grid, VERIFIED. Recommendation at the end.)

**F5. Live entry rails and the circuit breaker are dead code; no aggregate max-loss cap
exists anywhere.** `option_book.check_rails`/`admit` have ZERO production callers —
`max_deployment_pct`, `max_notional_leverage`, `undefined_risk_max_pct`, max-concurrent,
one-per-underlying are enforced nowhere. The breaker flattens once (edge signal) and then
gates nothing: the next entry cycle re-opens the book at the bottom of the drawdown, the
exact churn `option_book.py:139-144` says it exists to stop. The module's header claim
("the live pass and the backtest engine run ONE implementation of each rail") is false on
both sides — live runs none; backtest PremiumSeller still runs the old `_within_rails`
stopgap carrying defects the pure modules document as fixed. The only aggregate constraint
is the reserve pool: ~20% Reg-T for naked strangles, $0 for every debit structure. §8.2's
`book_left`/`instrument_left`/`structure_cap` remain design-only. For the GRID this means
concentrated books are a fitness artifact to discount; for LIVE it means the documented
risk system substantially does not exist. (Risk, VERIFIED.)

**F6. The grid's capital analysis measured the wrong gate.** The assignment-capacity gate
(short-put strike notional vs CASH, account-wide, cumulative, wholesale-refuse — not
downsize) is the actual binding constraint at $20k, absent from §6's table: one 10%-OTM
short put above spot ≈ $222 can never open; an IC on a $100 name only trades at sizing
≲5% (most of the 5-30% gene band is a zero-trade region); one 2-contract IC at strike 90
pledges $18k of $20k, blocking essentially every other put-side credit entry. Stage-1
verdicts for the entire put-side credit family will substantially measure this gate, not
the market. "Iron condor: any spot" is also false independently — wing width is % of
spot, so defined-risk reserves scale with spot. Fix: either make put-side credit builders
DOWNSIZE to assignment capacity instead of refusing wholesale, or re-scope sub-universes
(the spot cap applies to EVERY short-put structure), and rewrite §6. (Strategy, VERIFIED.)

---

## P1 — wrong conclusions likely. Fix before stage-1 restart / before Tasks 7-10 land.

**F7. Exit fills are a filter, not a cost.** Closes quote the as-of MID with no
concession (`TradeActions._close_limit_price:3961-3976`); the fill rule then needs the
fill-day mid to move half-spread in the position's favor, else the DAY sweep kills the
order and the manage pass re-quotes tomorrow. Entries got the `option_entry_cross` gene
for exactly this; closes got nothing. TP/SL/DTE exits systematically slip days and
migrate to the SPREAD-FREE expiry-settlement path — every arm's exit costs are flattered,
and it changes WHICH trades exist. Fix: apply the concession (or forced crossing for
DTE/SL) to closes. (Engine, VERIFIED.)

**F8. F1-sibling: assignment share-leg closes rely on a transaction roll gated on an
unrelated fill signal.** `daily_engine.run:670-673` rolls transactions only `if filled`;
steps 4a-pre/4a/4a-bis run after the gate and write synthetic FILLED orders. Expiry and
margin liquidation close their transactions directly — safe — but the assignment paths
that close an offsetting EQUITY lot rely on the roll, so after shares are called away on
a quiet bar the equity transaction stays OPENED until any later fill anywhere, and
`_has_open_or_waiting_position` locks the symbol out of re-entry for an arbitrary time.
O_CC/O_WHEEL arms exposed. Fix: run `refresh_transactions()` whenever 4a-pre/4a/4a-bis
reported activity. (Engine, VERIFIED.)

**F9. `option_car` fitness: two verified holes.** (a) Ordering: the wipeout check sits
AFTER the `base <= 0` early return and the trade gate — a genome with dd >= 100% and
NEGATIVE return scores an ordinary small negative, ranking far above both sentinels; a
wiped genome under 12 trades/yr returns LOW_TRADE (-1e8), outranking ZERO_TRADE (-1e9):
"a 3-trade blow-up outranks never trading", inverting the stated invariant. Move the
dd>=100 disqualification ahead of both. (b) Incentive gradient: with size scaling both
return and dd, score is strictly DECREASING in size above the 5% dd floor — the GA is
told to shrink every positive-edge genome to dd≈5% and collect a 16x multiplier, guarded
by an "unreachable" claim that is unverified and likely false (trade count is
size-independent). The gen-1 champion (+760%/-46.3% dd) confirms the other end: at high
base the squared penalty loses to return. Test the 5% claim empirically; it is the same
genre as the naked-short-put claim and guards a 16x. (Grid, VERIFIED arithmetic.)

**F10. Short strangle reserves only the put leg.** `TradeActions.py:3443` — entry model
disagrees with the sim's own maintenance model (which loops BOTH short lots) by ~2x, so
O_SSTG opens what it immediately breaches, then force-unwinds at unguarded marks
(compounding F1/F2's mark problem). (Engine, VERIFIED.)

**F11. The 5% spread model was never calibrated.** All quotes in the cache are
bid==ask==close; realism rests entirely on `--option-spread-pct` (5% of premium) + the
entry-cross gene. Real OTM single-name spreads run 10-40% of premium; a 4-leg condor
round-trip at realistic widths could cost 30-80% of its credit — much of a marginal
credit-arm edge may be spread-model optimism. One day of TastyTrade quote sampling
converts the guess to data; do it before restarting stage 1. (Strategy+Engine, VERIFIED
premise / SUSPECT magnitude.)

**F12. Permission refusals are laundered into quote refusals.** Naked shorts refused for
lack of PERMISSION (`allow_undefined_risk=False`) report `MAX_LOSS_UNMEASURABLE_REFUSAL`
— operator sent to re-pull quotes when the remedy is a setting. `UNDEFINED_RISK_REFUSAL`
is emitted by nothing repo-wide. Also: `ceiling=0.0` (the exhausted-budget value) reports
BUDGET_CEILING ("widen the box") instead of exhausted. Fix in `_chargeable_max_loss` /
`_ceiling_reason`: return charge+cause. (Risk, VERIFIED. Small; slot into Task 7 or a 6b.)

**F13. The naked-short-put-UNBOUNDED error still stands in FOUR places** despite the code
being correct and the 2a99e92f doc fix: `2026-08-29...design.md` §4 table row, §7:217
("on profit only"), §6's `opt_sl_ml` emission prose (which contradicts the mechanism it
specifies — under MEASURED-keying the rule WOULD be emitted for short puts), and a
shipped test docstring `packages/common/tests/test_option_payoff.py:149-150`. Task 9
builds `opt_sl_ml` FROM §6 — purge these before Task 9. Same class: jade lizard "no
upside risk by construction" is false (needs credit >= wing width; enforced nowhere,
rarely true at defaults; the max-loss formula survives because the put side dominates).
Also: the retracted "sizing refuses at contracts=0" claim, deleted from the module
docstring in 3d676204, survives verbatim in the features-test docstring (~:845).
(Risk+Strategy, VERIFIED.)

**F14. PremiumSeller's backtest dispatch skips exits and the breaker on entry bars.**
`_bypass_run_kind` returns "entry" whenever entry_ok, and only `rebalance` runs on entry
bars; `manage_open` is the only place the peak ratchets, the trip tests, and exits run. A
daily-entry config never manages at all. Scoped: PremiumSeller is backtest-only and
slated for deletion — but its stage-0 numbers carry this. (Risk, VERIFIED mechanism.)

---

## P2 — decide-before-building, and hygiene.

**F15. `w_rr`/`w_profit` are near-redundant with `premium` on real chains.** Two
independent measurements: Spearman(rr, premium) = 0.98-1.0 across synthetic BS chains,
and rho = 1.000 on a realistic descending-premium ladder EVEN ON the synthetic-denominator
path. Within one chain, rr/profit/premium are all near-monotone in moneyness — extra
moneyness dials duplicating what the strike box controls, at ~5ms/pick. And our
`test_rr_is_not_a_rescaling_of_profit` manufactures its ordering flip with a 100-strike
call quoted RICHER than the 90-strike on the same expiry — a premium ladder that cannot
occur in a real chain: mathematically distinct, market-redundant. DECISION NEEDED before
Task 10 wires the genes: drop `w_rr`, or re-denominate against something not monotone in
moneyness (e.g. rr at fixed |delta|). The `w_premium` sign-fix rationale has the same
soft spot ("a buyer wants cheap premium" within one chain means "prefer further OTM").
(Strategy+Risk, VERIFIED measurements.)

**F16. ~8-11 equity-RM genes ride every stage-1 option genome, probably inert.** Real
genomes are 40-51 genes (not the design's ~22 structure-only): `atr_risk_budget_pct`,
`use_atr_stop`, `min_stop_loss_pct` etc. on strategies with no equity leg. If inert,
20-25% of every job's genome is dead search dimensions — the exact pathology this program
hunts. One pin test settles it (toggle `use_atr_stop`, assert byte-identical option
backtest). (Grid, VERIFIED count / SUSPECT deadness.)

**F17. Selection-stack visibility is itself dead code.** `inapplicable_features` — the
mechanism that replaced §7's raise-on-absent-column promise (itself falsified: an
all-absent column silently flattens, no raise) — has zero production callers. Wire the
applicability report into `_resolve` when Task 10 lands, or the dead-gene diagnosis
tooling is decoration. Also record: NONE of the 7 weights are GA-wired today (P5
unbuilt), so the running stage 1 is unaffected by any of the selection-stack findings.
(Risk+Grid, VERIFIED.)

**F18. Wheel comparisons are regime-censored in the wheel's favor.** Option data starts
2024-02; the window is bull-heavy; `hold_assigned_stock` shares that ride to end-of-run
are unmanaged long-equity beta that GAINS in this window, and the regime that punishes
the wheel is absent from the data entirely. Additionally `cc_guard` halts the ruleset
while a covered call is open, so calls ride to expiry binary — understating
live-achievable premium capture. Read O_WHEEL stage-1 results as "long large-caps in a
bull market plus premium". (Strategy, VERIFIED.)

**F19. Measure the mark-data hole before trusting absolute numbers.** Nobody has measured
how often daily bars are missing or junk for HELD contracts — every such event flows
straight into equity/drawdown/realised P&L through the paths in F1/F2. A one-day probe
(parquet store vs held-contract histories from a finished stage-1 job) quantifies the
largest single source of unmeasured error. (Engine, the verdict's headline ask.)

**Hygiene (verified, small):** debit/credit partition guard is tautological
(complement-defined, can never fire — launcher:2986); "15 structures" vs "18 members" in
adjacent grid-doc paragraphs, ~240 arithmetic never recomputed; short-straddle reserve
uses max(legs) vs Reg-T greater+other-premium (small systematic under-reserve);
`structure_metrics` classifies a covered call's short leg as naked (latent);
`option_expiry_outcome` pins dead code in tests; single-leg fills skip `slippage_bps`
while multi-leg pays it (moot at slippage 0); the bracket-exit path is silently inert for
options (unreachable today, no log); the 2026-08-24 "DECIDED: defined-risk only" was
reversed by the grid doc without any doc recording the reversal; `inapplicable_features`
docstring overstates the mixed-set case; the stage-1 report lacks the top-1/top-5
concentration check the house discipline requires before trusting any backtest.

## What held up under attack (for fairness, and so nobody "fixes" it)

The `_OFF_SCALE` three-state machinery (leaks TypeError loudly; no undocumented demotion
path constructible); `_unbounded_risk_leg`'s narrow attribution (every adversarial case
correctly None); the breaker's latch hysteresis math; cover/close paths (closes never
cover-gated, wheel included; the pledged-share lock is what actually closes the same-bar
naked-call hazard — the 4a-pre docstring's narrative is half-obsolete but the code is
right); the assignment-capacity gate (so effective it is F6's problem); iv_rank/iv_rv
operator assignment (all 15 members hand-checked correct); the `_build_daily_trial_config`
whitelist (spot-checked end-to-end, NO dropped option knobs — the account_settings
wholesale-forward pattern holds); defined-risk MTM clamping (composition-aware, cannot
hide a wipeout); B10 partial-close netting; ARC gate arithmetic; the empirically-pinned
JL/ratio reserves.

## Recommended sequencing

1. **Now, engine (small, high-leverage):** F1 intrinsic clamp (+ margin/mark twins), F2
   fallback extension, F8 roll condition, F7 close concession. All are backtest-only
   files — but they ARE imported by the option stage-1 job, so land them together with
   the stage-1 restart below, not underneath the running job.
2. **Pause the option stage-1 matrix at the next job boundary** (matrix3/equity grids are
   unaffected): apply #1, add the universe caps, set option_car for O_CC/O_PP, fix
   elitism to the intended value, diagnose the non-resume, THEN restart. Continuing to
   burn months on the current configuration produces numbers the engine fixes will
   invalidate anyway.
3. **Before Tasks 7-10 resume:** purge F13's four stale claims (blocks Task 9); decide
   F15 (drop vs re-denominate rr — blocks Task 10); fold F12 into Task 7 or a small 6b.
4. **Stage-2 substrate (F3) before any more stage-1 spend than the restart requires.**
5. **Before ANY live option deployment:** F5 — wire the rails and breaker entry-gate
   into the SHARED decision path per the operator decision recorded 2026-08-30 in the
   risk-manager design §4 (one `ba2_common` implementation invoked from
   `TradeActions`/`_resolve()`, arming live and backtest in the same commit, PremiumSeller
   stopgap deleted, parity gate extended to option decisions); add a total
   committed-max-loss cap; F10 strangle reserve.
