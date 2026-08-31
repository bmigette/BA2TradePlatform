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
