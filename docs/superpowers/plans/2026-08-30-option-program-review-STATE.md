# Option program review — RESUME STATE (2026-08-30)

Session checkpoint so this work survives a rate-limit or session loss. If resuming cold:
read this file, then collect the four reviewer results, then write the findings doc.

## Where everything lives

* **Feature branch**: `option-selection-modes` in worktree `C:\Users\basti\Documents\dev\BA2-option-selection`.
  Tasks 1-6 of `docs/superpowers/plans/2026-08-29-option-selection-modes-and-max-loss.md`
  are DONE and committed (HEAD = `bc7986a8`, all local, deliberately unpushed — a
  `packages/` push implies a TEST_APP_VERSION bump, forbidden while a grid runs).
  Commits: daecc158, 0deb3668, 3486ad82, 01cb3187, 478da70d, ec310e65, e7eae1cd,
  717f32fa, 3d676204, bc7986a8. Suite state: packages/common 2512 passed / 1 failed
  (the pre-existing test_portfolio_allocation_wizard float-dust flake — unrelated).
* **Tasks 7-10 are ON HOLD** by operator instruction until (a) the program review below is
  written up and (b) operator reviews. Task 10 additionally requires a version bump and
  must wait for a gap between grid runs.
* Grid matrix3 is running from the MAIN tree — never edit main-tree code while it runs.

## In flight: 4 Fable-model program reviewers (full option strategy/grid scope)

Dispatched in parallel, read-only, one per facet. Results arrive as task notifications;
if those are lost, each agent's final result is the last `result` entry in its output
file (extract with a script — do NOT cat the whole JSONL into context):

| facet | output file (session temp, persists on disk) |
|---|---|
| Strategy & economics | `...\7ffbe7f6-...\tasks\adc74a66b11126d48.output` |
| Grid / GA search design | `...\7ffbe7f6-...\tasks\a4f524691d0d9c22e.output` |
| Engine correctness | `...\7ffbe7f6-...\tasks\a29e3b7082d4a3f28.output` |
| Risk / lifecycle / selection stack | `...\7ffbe7f6-...\tasks\af077a559ee494792.output` |

Full prefix: `C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-BA2TradePlatform\7ffbe7f6-3867-40dd-b426-f561d95cf869\tasks\`

Agents survive session restarts and can be resumed/queried by SendMessage if incomplete
(proven earlier this session with the Task-5 reviewer after a session exit).

## STATUS UPDATE: all four reviews COMPLETE, findings written and pushed
See docs/superpowers/specs/2026-08-30-option-program-review-findings.md (19 findings,
P0-P2). Tasks 7-10 remain ON HOLD for operator review of that document.

## Next steps, in order (original, now superseded by the findings doc's sequencing)

1. Collect the four facet reports; dedupe against ALREADY-KNOWN findings (below).
2. Synthesize into `docs/superpowers/specs/2026-08-30-option-program-review-findings.md`,
   severity-ranked, verified-vs-suspect preserved. Commit + push (docs-only, no bump).
3. STOP — operator reviews findings before Tasks 7-10 resume.

## Already known — do not re-report as new

* Naked short PUT loss is BOUNDED (corrected 2a99e92f); short call/stock are the unbounded ones.
* w_premium sign fix (0..2 -> -2..2) is designed, lands in Task 7.
* Risk-manager design §8 triage (book_left/instrument_left/structure_cap) DOES NOT EXIST
  in code; `_size_by_cost` sizes on premium/reserve, never max loss. Recorded in 3d676204.
* Selection-policy payoff features cost ~5039us/pick (200-row chain); zero-weight skip is
  load-bearing; payoff_columns sharing rule recorded in plan Task-10 notes.
* Per-chain applicability: w_profit/w_rr pressure varies with chain composition (any
  off-scale candidate -> column inert). Dead-gene guard must assert applicability first.
* _chargeable_max_loss collapses 3 refusal causes into inf (crossed quote / declining
  builder / unpermitted undefined risk) — Task-6 report, open.
* Stale test docstring: test_without_a_structure_fn_the_ceiling_is_inapplicable_rather_
  than_total still quotes the retracted "sizing refuses at contracts=0" claim — open, trivial.
* stage1_gen_report sha + strip pinned (c086c16f); wipeout sentinel in option_car (db56fb17).
* parquet_store staleness fix `min(end, expiry)` (404f792c); warmup Startup .bat removed
  (backup at tools/_removed_ba2_option_warmup_resume.bat.bak); cache verified UNDAMAGED
  against the 2026-08-28 export (400/400 empties pre-existing, 220/220 completes intact).


---

## CLOSE-OUT (2026-08-31 05:45) — implementation of the restart-blocking findings COMPLETE

All units implemented, reviewed (Opus/Fable), mutation-verified, and approved. Nothing
pushed from the fix branches; no version bump taken (deliberate — see deploy plan).

| branch | commits | scope | state |
|---|---|---|---|
| `option-review-fixes` (worktree BA2-review-fixes) | 665a2f30, 3841c49a, 2f9dc1b1, 03f799eb, 7c979e45, 994046e3 | F1+F2 clamps/marks, F10 both-leg reserve, F8 settlement roll, F7 close spread, F6 capacity downsize, fast-follows (put-close guard pin, long-side cap, doc trails) | APPROVED. tests/backtest 926+1skip; packages/common 2464+1known |
| `option-review-config` (worktree BA2-review-cfg) | 5e3835ce, 364186f2, a191025b | F9a wipeout ordering (literal invariant), F4 universe caps (incl. O_WHEEL inherit), elitism 10%, O_CC/O_PP -> option_car, POP/GEN overrides | APPROVED. option_car 58/58; equity fn byte-identical to base (verified twice) |
| `option-selection-modes` (worktree BA2-option-selection) | daecc158..bc7986a8 (Tasks 1-6) + 6199f477, 36be07e0, 546c57a8, 80fc5ca8 | selection stack + F12 exact-cause refusal ladder, F13 docstrings, F19 sparsity probe | Tasks 1-6 approved earlier; F12/F19 unit approved. packages/common 2521+1known |
| `dev` (pushed) | 0e361f52, 5ac4b62c, 2bd2a20a, a54cec62, + findings/plan docs | program-review findings (19), RM-sharing decision, F13 purges, F15 w_rr decision | pushed |

### Deploy plan (operator executes at chosen window)
1. Wait for a matrix3 job boundary (or accept losing the in-flight generation — jobs
   resume from checkpoints).
2. Merge `option-review-fixes` AND `option-review-config` together into dev — the review
   requires they deploy as a pair (F2 closes the wipeout-sentinel BYPASS, F9a fixes the
   sentinel ORDERING; each alone leaves the other's hole open). Selection branch merges
   whenever convenient (its code has no production caller until Task 10 wiring).
3. ONE `TEST_APP_VERSION` bump in the merge commit; push; let workers re-sync at their
   next job boundary.
4. Restart option stage-1 with `tools/stage1_run.sh` (now carries caps/fitness/elitism;
   24 workers per operator). ALL POST-FIX NUMBERS ARE A NEW BASELINE — never compare
   fitness/CAR against pre-fix runs (F1/F2 move marks+drawdowns, F7+put-guard change
   which exits fill, F6 converts zero-trade regions into entries, F10 halves strangle
   sizes).

### First-post-fix-job watch list (from the branch review)
(a) DOWNSIZED log rate up, put-side zero-trade band gone (F6 working);
(b) exits via expiry vs filled closes, split forced/discretionary (residual F7 optimism);
(c) strangle/straddle margin-liquidation ~zero at open (F10; any instant-breach left is
    a NEW bug);
(d) naked-arm drawdown distributions widen honestly (F2); any dd>=100 genome scoring
    above the sentinel would be an F9a regression.

### Open operator decisions (none block deploy)
* F10 reserve model: sim charges sum-of-both-legs (self-consistent, conservative); real
  Reg-T is greater-leg + other premium (~2x more capacity). Decide before INTERPRETING
  strangle/straddle grid results; changing it means touching entry+maintenance together.
* Stage-2 substrate (F3): per operator, built AFTER stage-1 research digestion.
* Tasks 7-10 of the selection plan: still on hold pending operator review of the findings
  doc; Task 9 unblocked by the F13 purges, Task 10 shaped by the F15 w_rr decision.

---

## FOLLOW-UP TASK LIST for the operator (recorded 2026-09-03, from the options-grid2 final review §1b/§2b)

Recorded, NOT implemented. Each item is a judgement call the operator owns; the branch's
FIX-NEEDED items were landed separately (the 5 stale tests, deploy-payload parity, the
`_entry_order` ordering pin). Ranked cheapest-first within each group.

### A. LIVE BEHAVIOUR — read this one first

**A1. `LIFECYCLE_ROLL_SHORT` is computed and then silently discarded. LIVE MONEY.**
`option_lifecycle.decide()` computes the PMCC overlay roll (`option_lifecycle.py:1113-1119`,
`pmcc_roll_due:868-892`) and hands back `LIFECYCLE_ROLL_SHORT`. That reason is **not in
`LIFECYCLE_CLOSING_REASONS`**, and `option_lifecycle_service.py:397-418` handles only
`UNKNOWN` / `COVER_LOST` / `should_close` — so the live pass reaches a roll decision and drops
it on the floor. Today the roll happens anyway, via the RULE (`pmcc_roll_dte` ->
`ShortLegDaysToExpiryCondition` -> `RollPMCCShortAction`), which is why nothing is visibly
broken. But a live PMCC sleeve is currently protected by exactly one of the two mechanisms
that appear to exist, and nothing in the code says which.

The review's recommendation is to **DELETE the dead branch** and let the rule own the roll in
both runtimes — that is `RollPMCCShortAction`'s own "IT IS A RULE, NOT AN ENGINE HOOK"
argument, and it is the only option that leaves one mechanism rather than one and a half. The
alternative (wiring the reason through the service) creates a second roller the backtest does
not have, i.e. a NEW parity gap. **This is a live-behaviour decision on real money. It is not
a tooling change, and it was deliberately left for the operator to judge.**

### A1 — RESOLVED 2026-09-03. Read this before acting on the list below.

`LIFECYCLE_ROLL_SHORT` is no longer discarded, and A1's premise turned out to be narrower
than recorded here: the reason is emitted ONLY for a transaction tagged `pmcc`
(`is_multi_expiry_strategy` is membership in `frozenset({"pmcc"})`, fail-closed), so no
covered call, wheel or other single-expiry structure could ever reach the discard. What
shipped instead of the recommended deletion:

* **The service's dispatch is TOTAL.** `LIFECYCLE_DISPOSITIONS` names what happens to every
  reason `decide` can return (close / report / rule-owned / no action), the loop is driven
  off it, and a reason with no entry RAISES. The `if not decision.should_close: continue`
  fall-through — a correct reading of HOLD and a wrong one of everything else — is gone.
* **The roll is still not performed by the pass.** `roll_pmcc_short` is a RULE walked by both
  runtimes; a roller here would race the searched trigger on an unsearched threshold. The
  decision is RECORDED (`LifecyclePassResult.roll_due`) and the one thing the rule cannot
  report about itself is raised: `UnownedRollError` when a two-expiry structure is due to
  roll and the sleeve's ruleset carries no `roll_pmcc_short` rule. Raised AFTER every other
  decision is acted on, and contained by `JobManager`'s existing guard.
* **C6 (the DTE roll/close) is closed too, in the same direction.** `decide` no longer emits
  `LIFECYCLE_ROLL_DTE` at all: the `opt_dte` rule the GA already searches owns that exit in
  both runtimes, so live stopped carrying a second copy of it on an unsearched threshold.
* **C2 (covered-call exits) is closed for the DTE case.** A NEW standing rule came out of it,
  and it generalises beyond this task: **an option exit condition anchored on the evaluated
  TRANSACTION is inert for a stock-anchored overlay key.** Both runtimes evaluate an
  OPEN_POSITIONS ruleset once per SYMBOL against the OLDEST entry order — the stock on
  `O_CC`/`O_WHEEL` — while the call is written on its own transaction, so `days_to_expiry`
  there never fires in either direction while carrying a searched gene. Overlay keys must use
  REPOSITORY-resolved conditions (`covered_call_days_to_expiry` +
  `close_option(close_target='covered_call')`, the shape `has_covered_call` already used).
  `O_CC` and `O_WHEEL` now emit `cc_dte`; **both keys are a new results baseline** (plan
  comparability entry 4) and **every live instance of them needs re-export/re-import**
  (final-review merger-checklist item 8).

**MERGER NOTE — the version bump.** `packages/` changed AFTER `origin/dev`'s
`TEST_APP_VERSION = "2026.09.0013"` bump (`bfb19508`), so the follow-up merge takes it to
**0014**. The operator does that bump at merge time, at a matrix3 job boundary and never
mid-run: distributed workers compare `TEST_APP_VERSION` alone to decide whether to
self-update, and shared `packages/` code that reaches them without a bump leaves workers
running different `ba2_common` from the master. No bump was taken on the branch.

Still open on this list: B1 (short-leg delta), C1 (breaker flatten), C3/C4/C5, C7-C10, and D.

### B. The tested-delta gap — the only live rule with NO backtest counterpart

**B1. Add a `ShortLegDeltaCondition`.** Tested-delta management (`option_lifecycle.py:536-568`,
`:1099-1105`) is the one live exit no ruleset can express: the only delta condition in the
registry is `LongLegDeltaCondition` (`TradeConditions.py:3679`), and **no short-leg delta field
exists**. So the GA cannot search it, and no grid result is evidence about it. The shape
already exists — `_TwoExpiryLegCondition` plus `option_lifecycle._tested`'s short-leg selection
— and the precedent to copy is exactly `credit_decayed_pct` -> `CreditDecayedPctCondition`
(`TradeConditions.py:3619`), which the review confirmed is genuinely ONE implementation reached
by both runtimes. Biggest parity win per unit of work on the whole list.

### C. The remaining `decide()`-only behaviours (review §2b table)

Live = the shared rules path **plus** `decide()`; backtest = the shared rules path only. Live
is a strict superset for exits, so **a grid result systematically understates how aggressively
a live sleeve exits.** Pinned in `EXPERTS.md:164`: do not read a backtest as evidence about
profit-capture / roll-DTE / tested-delta behaviour.

| # | Behaviour | Status | Cheapest close |
|---|---|---|---|
| C1 | **Circuit-breaker FLATTEN** | PARTIAL — the breaker TRANSITION is shared (`daily_engine.py:1544-1564` -> `update_sleeve_breaker:1161`) and the entry decline is shared, but the backtest never CLOSES the book: it rides a drawdown live liquidates | mirror `option_lifecycle_service.py:415-418` at the `update_sleeve_breaker` call site (`daily_engine.py:1550`). Without it `EXPERTS.md:161`'s "one implementation, two callers" is true of the latch but not of its consequence |
| C2 | **Covered-call cover-lost close** | NO — `grep cover_lost` over `testplatform/` = 0 hits | a condition, or accept that O_CC backtests never model it |
| C3 | **Profit capture** (`profit_capture_pct`) | PARTIAL — same arithmetic, **two implementations** (`option_lifecycle._pnl_pct:495-533` vs `TradeConditions._get_spread_pnl_via_transaction`), fed by different quote sources (chain map vs per-leg `get_option_quote`) | unify the two P&L implementations |
| C4 | **Strangle-specific capture** (`strangle_capture_pct`) | PARTIAL — no distinct backtest knob; every credit kind shares one `opt_tp` band | a per-kind band, if the distinction is real |
| C5 | **Credit-multiple stops** (`dr_stop_*`/`ur_stop_*`) | DIFFERENT — live partitions by declared strategy tag and carries a deliberate quirk (with `ur_stop` off, a naked structure has NO stop even when `dr_stop` is on); the backtest partitions differently, can have both live, and uses different denominators | decide which partition is correct, then make it one |
| C6 | **DTE roll/close** | PARTIAL — identical on single-expiry; on a declared multi-expiry structure live reads the SHORT leg and `DaysToExpiryCondition` the LONG. Deliberate, documented both sides | none needed; know it when reading a PMCC/diagonal result |
| C7 | **Unknown/unmeasurable alarm** | PARTIAL — live has `LifecyclePassResult.unknown`; the backtest has per-condition `_unevaluable` only, so an unevaluable condition is a `False`, indistinguishable from "threshold not met". **A trial cannot report how many bars it was blind** | an aggregate blind counter on the trial |
| C8 | **Close mechanics** | DIFFERENT BY DESIGN — live MARKET (`_close:770-824`), backtest LIMIT plus a backtest-only spread concession (`CloseOptionAction:5301-5347`); residual optimism recorded at `:5264-5269` | accepted |
| C9 | **Expiry / assignment settlement** | DIFFERENT — live is OCC plus the broker; the backtest never exercises ITM longs, always assigns short ITM, and models no early American assignment | known limitation |
| C10 | **Reverse direction** — what the BACKTEST does that live does not | margin-call liquidation; `hold_assigned_stock`; the exit-quote concession; **per-bar cadence** (the backtest walks OPEN_POSITIONS every bar, live runs on the JobManager schedule); and **rule ordering** — `_insert_option_overlay(anchor="front")` plus `continue_processing` make first-match order load-bearing in the backtest, while live's `decide()` has a fixed precedence ladder, so on a bar where profit-capture and roll-DTE both hold the two runtimes record different REASONS | — |

Also standing: **no shipped grid spec sets `risk_manager_mode: classic_options`**
(`ba2test_launcher.py:1073-1083`, pinned by
`test_no_shipped_expert_spec_selects_a_risk_manager_mode`), so in current grid jobs the entry
rails and the sleeve breaker are inert too. That changes how to read every "both runtimes"
claim in `EXPERTS.md:158-163`.

### D. Pre-existing: the seam-wiring resolver captures a stale `get_provider`

`_wire_provider_resolver` (`seam_wiring.py:182-199`) does `from ba2_providers import
get_provider` and its `_resolve` closure **binds that function object**. `wire_backtest_seams`
is idempotent (`if _resolver is None`, `:152-156`), so if the FIRST wire in a process happens
while `e2e_support.hermetic_providers` has `ba2_providers.get_provider` patched to a fixture,
the closure captures the FIXTURE permanently and is never rebuilt. `hermetic_providers`
restores the module attribute correctly — the closure is what is stale.

Reproduces in two files:
`pytest tests/backtest/test_daily_engine_e2e.py tests/backtest/test_seam_wiring.py` ->
`assert <FixtureOHLCVProvider> is <FMPOHLCVProvider>`. Not caused by the options-grid2 branch
(`git log 56c3f8c2..HEAD --` on `seam_wiring.py`, `test_seam_wiring.py`, `e2e_support.py`,
`hermetic_providers.py` and `test_daily_engine_e2e.py` is empty).

**Fix is one line, in production:** resolve through the module attribute at call time —
`import ba2_providers` then `return ba2_providers.get_provider(category, name, **kwargs)` —
instead of the bound name. Benign in production (each GA worker wires once, against the real
provider), but it makes the whole backend suite ORDER-DEPENDENT, which is how the 5 stale
`O_PMCC` tests stayed invisible for a day. **Left unfixed here as a production change outside
the FIX-NEEDED mandate**; its 2 failures are expected in a full backend run.
