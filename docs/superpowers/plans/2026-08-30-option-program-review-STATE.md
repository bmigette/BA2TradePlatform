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

## Next steps, in order

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
