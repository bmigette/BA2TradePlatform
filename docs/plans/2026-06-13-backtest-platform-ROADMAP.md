# Backtest Platform — Master ROADMAP

> Orchestration map + autonomous-execution strategy for the 7-phase expert-backtest program.
> Read this first; each phase has its own detailed implementation plan (linked below).
> Derived from `docs/plans/2026-06-13-backtest-platform-design.md`, `docs/FMP_BACKTEST_FEASIBILITY.md`, the SHARED CONTRACTS, and a file-by-file recon of the live tree at commit `72eefee`.

**Program goal:** Extract the BA2TradePlatform engine into three installable packages, make every backtestable expert run the *same* decision logic live and in backtest, build a deterministic daily backtest engine + simulated account, add a joint genetic optimizer, re-source ML datasets through the shared cache + add a cache UI, and finally migrate the live trading platform onto the packages — all while the live `BA2TradePlatform` keeps working untouched until the very last phase.

**Three repos / hosts in play:**
- **Packages** (`BA2TradeCommon`/`ba2_common`, `BA2TradeProviders`/`ba2_providers`, `BA2TradeExperts`/`ba2_experts`) — siblings under `…/dev/BA2/`. Built in Phase 0; edited in Phases 1–3.
- **Backtest host** (`BA2TestPlatform/backend/`) — the ML/backtest app that *consumes* the packages. The engine, optimizer, screener cache, and cache UI land here (Phases 2–5). Always use `BA2TestPlatform/backend/venv/bin/python` (per `backend/CLAUDE.md` — never system python).
- **Live host** (`BA2TradePlatform/ba2_trade_platform/`) — the running trading platform. **Read-only until Phase 6**, which migrates it onto the packages.

---

## ⚠️ Numbering note (read before cross-referencing the SHARED CONTRACTS)

The **plan-file numbering is authoritative**:

| Plan file | Scope |
|---|---|
| Phase 0 | Package extraction (extract-by-copy) |
| Phase 1 | Provider `as_of` + native cache + expert `_gather`/`_process` split (the golden test) |
| Phase 2 | Daily engine + `BacktestAccount` |
| Phase 3 | Screener history + survivorship-free universe + grouped cache |
| Phase 4 | Joint genetic optimizer |
| Phase 5 | Cache-management UI + re-source ML datasets through providers |
| Phase 6 | Migrate the live host onto the packages |

The SHARED CONTRACTS' `per_phase_scope`/`phase_dependencies` blocks use a **different (design-doc) numbering** where `phase_1`=expert split, `phase_2`=providers, `phase_3`=screener, `phase_4`=engine, `phase_5`=optimizer/ML, `phase_6`=migrate. The plan files **merged the contract's phase_1+phase_2 into plan-Phase-1** and **renumbered the rest down by one** (contract phase_4 engine → plan Phase 2, contract phase_5 optimizer → plan Phase 4, etc.). The Phase 4 plan documents this explicitly ("phase numbers are off-by-the-design-doc; in the design §6 the engine is Phase 2 and the optimizer is Phase 4"). **When a plan cites a "SHARED CONTRACT phase_N" scope, map it through this table.** This is a documentation-numbering mismatch, not a dependency or content error — every plan's dependencies are internally correct.

---

## 0. Cross-cutting requirements (added 2026-06-13 — apply to EVERY phase)

These were added after the per-phase plans were authored; they override the plans where they conflict and every phase's execution must honor them. (Full detail in the Phase 0 plan, Amendments A1–A5.)

1. **Execution env:** PyPI direct TLS fails on this machine — all `pip install` use `--trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org`. Package tests run on the live venv interpreter (`BA2TradePlatform/venv/bin/python`, which has the heavy deps) via `PYTHONPATH`, except BA2TestPlatform host code which uses `BA2TestPlatform/backend/venv/bin/python`. **Leak/isolation gates assert via `sys.modules` (a module is not *pulled* on import), never via "not installed"** — forbidden deps are present in these venvs.
2. **`install.{sh,ps1}` create a venv** (Phase 0 Amendment A2) and install the chain into it with the trusted-host flags; `--editable` installs the local sibling clones, default installs from `git+ssh`.
3. **Expert settings import/export lives in `ba2_experts/settings_io.py`** (extracted from `BA2TradePlatform/settings_export_import.py`'s `export_experts`/`import_experts`) and is consumed by **both** the live platform (Phase 6 wires its UI/CLI) and the backtest platform (load/export expert configs + optimizer-found params). Built in Phase 0 (Amendment A3).
4. **DB schema changes ship migration scripts via each repo's EXISTING migrator** (Amendment A4): `BA2TradePlatform` → `python migrate.py create/upgrade` (Alembic); `BA2TestPlatform` → `backend/scripts/migrate_db.py` + `backend/db_migrate/`. Affects Phase 1 (`ProviderCache`), Phase 2 (`Backtest.model_id` nullable), Phase 4 (RM columns on `Strategy`), Phase 5. Gate per schema task: applies on a fresh DB **and** upgrades an existing populated DB.
5. **Push policy:** commit + push are allowed for `BA2TradeCommon`/`BA2TradeProviders`/`BA2TradeExperts`/`BA2TestPlatform` (feature branches). **Never push `BA2TradePlatform` `dev`/`main`** until the whole program is reviewed; Phase 6 work stays on a local feature branch.

---

## 1. Phase dependency graph

```
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  PACKAGES: ba2_common ← ba2_providers ← ba2_experts                │
                 └──────────────────────────────────────────────────────────────────┘
   Phase 0  ──▶  Phase 1  ──▶  Phase 2  ──▶  Phase 3  ──▶  Phase 4  ──▶  Phase 5
  (extract)   (gather/      (engine +     (screener     (joint        (cache UI +
              process +     Backtest-     history +     genetic        re-source
              as_of cache   Account)      universe)     optimizer)     ML datasets)
              + GOLDEN)        │              │             │               │
                              └──────────────┴─────────────┴───────────────┘
                                                                            │
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  Phase 6 (migrate live host) — authored in parallel, LANDS LAST    │
                 │  consumes ALL prior phases; re-runs the Phase-1 golden test        │
                 └──────────────────────────────────────────────────────────────────┘
```

**What each phase consumes from its predecessors:**

| Phase | Consumes | From |
|---|---|---|
| **0** | the live tree at `72eefee` (read-only) | — |
| **1** | Phase 0's package layout + the 3 seam definitions (`instance_resolver`, `LLMServiceInterface`, `db.configure_db`, `position_sizing.get_latest_atr(indicator_provider)`, `TradeConditions.set_provider_resolver`) | Phase 0 |
| **2** | Phase 1's `_gather`/`_process`/`analyze_as_of` seam, the `Recommendation` value object, `BacktestContext`; Phase 0's `position_sizing`/`TradeConditions`/`db` seams; Phase 1's `as_of` providers + native cache for the price time-machine | Phases 0, 1 |
| **3** | Phase 1's `as_of` `get()` providers + effective-date native cache (for as-of metric reconstruction); Phase 0's screener package (`ba2_providers/StockScreener.py`, `ScreenerProviderInterface`) | Phases 0, 1 |
| **4** | Phase 2's **deterministic in-process daily-engine runner** (the per-trial evaluator); Phase 1/2's identical-cache determinism guarantee; reuses BA2TestPlatform's `GeneticOptimizer`, `Backtest`/`StrategyOptimization` models, task queue | Phase 2 (+ a working ML-adapter so Tasks 1–4/6–7 are testable before Phase 2 lands) |
| **5** | Phase 1's uniform `get(as_of)` + native cache paths (for the OHLCV adapter + the `asof` cache-UI type); the ML engine (`backtesting.py MLStrategy`) stays unchanged | Phase 1 (engine path untouched) |
| **6** | ALL prior phases — installs the 3 packages into the live venv, shims the in-tree modules, wires all 6 seams, re-runs the Phase-1 golden test through the wired host | Phases 0–5 |

> **Dependency note on Phase 4 ↔ Phase 2:** Phase 4's fitness function calls Phase 2's synchronous in-process daily runner (NOT an enqueued sub-task — `TaskQueueService` is `max_workers=1` and would deadlock). Phase 4 ships a typed `_run_trial_backtest` seam with a working `backtest_handler.run_backtest` ML adapter so Phase-4 Tasks 1–4 and 6–7 are implementable/testable **before** Phase 2 lands; only Task 5's daily-engine wiring is gated on the confirmed Phase-2 runner name/signature (`run_daily_backtest(config, hoisted, decoded) -> results_dict`).

---

## 2. Per-phase scope + acceptance gate + plan link

### Phase 0 — Package extraction (extract-by-copy)
- **Scope:** Copy the live engine into 3 pip-installable packages with strict one-way deps (`ba2_common ← ba2_providers ← ba2_experts`); define the 3 seams (`InstanceResolver`, `LLMServiceInterface`, provider/ATR injection) without wiring them; leave the live tree byte-unchanged.
- **GATE:** Fresh-venv imports of each package succeed with NO `langchain`/`fmpsdk`/`nicegui`/cross-package leakage; `lint-imports` green in all 3 repos; pure-calculator unit tests pass; `git -C BA2TradePlatform status` clean.
- **Plan:** [`2026-06-13-backtest-platform-phase0-plan.md`](./2026-06-13-backtest-platform-phase0-plan.md)

### Phase 1 — Provider `as_of` + native cache + expert `_gather`/`_process` split
- **Scope:** Add the uniform `get(symbol, as_of=None, lookback=…)` contract (as_of=None byte-identical to live); build the effective-date native cache (parquet time-series + SQLite `provider_cache`); fix the 2 lookahead bugs (insider `transactionDate`→`filingDate`; statements `fiscalDateEnding`→`fillingDate`/`acceptedDate`); refactor every backtestable expert into `_gather`+pure `_process`, with `run_analysis` as a thin orchestrator and `BacktestInterface.analyze_as_of(as_of, context)`.
- **GATE (THE GOLDEN TEST — defined here, once):** for every expert, `_process(_gather(live, None), settings) == analyze_as_of(now, context)` on `(signal, confidence, expected_profit_percent, details, skip, skip_reason)`, float-tolerant, `current_price` pinned identically in both paths (8 parametrized cases). PLUS live `run_analysis` lifecycle unchanged; PLUS provider `as_of=None` byte-equal to pre-refactor fetch + a fixed `(symbol, as_of)` replays deterministically with `effective_date ≤ as_of` + cache-hit-count assertions. `lint-imports` green; `BA2TradePlatform` byte-unchanged.
- **Plan:** [`2026-06-13-backtest-platform-phase1-provider-asof-plan.md`](./2026-06-13-backtest-platform-phase1-provider-asof-plan.md)

### Phase 2 — Daily engine + `BacktestAccount`
- **Scope:** Build `BacktestAccount(AccountInterface)` (19 equity abstracts: ledger + as-of price time-machine + next-bar fill engine in `refresh_orders` + TP/SL/OCO legs) on a separate backtest DB, and a custom daily multi-asset engine driving the REAL ba2trade path (universe → `analyze_as_of` → `TradeManager`/classic RM/`position_sizing` → `submit_order` → fills → record). Wire the seams in this host. First runs: `FMPEarningsDrift` + `FMPInsiderClusterBuy`.
- **GATE:** deterministic end-to-end daily backtest of a clean expert produces a stored `Backtest` row (status=completed) with sane finite metrics + non-empty equity curve; the SAME cache+params+seed re-run yields a **byte-identical** equity_curve + identical metrics; `BacktestAccount` has zero abstractmethods left; fill engine correct (next-bar MARKET±slippage, LIMIT/STOP crossing, TP/SL/OCO, ledger+commission); per-bar price-cache bust verified; `analyze_as_of` inside the loop == Phase-1 golden `Recommendation` for a fixed `as_of`. Verified by `./venv/bin/python -m pytest tests/backtest/ -v`; `BA2TradePlatform` clean.
- **Plan:** [`2026-06-13-backtest-platform-phase2-engine-backtestaccount-plan.md`](./2026-06-13-backtest-platform-phase2-engine-backtestaccount-plan.md)

### Phase 3 — Screener history + survivorship-free universe + grouped/labeled cache
- **Scope:** One screener class, two data-source modes behind an optional `as_of` (filter LOGIC never forks). `as_of=None` = live FMP screener (unchanged); `as_of=<date>` = reconstructed historical screen over a survivorship-free universe (broad = available-traded ∪ delisted by lifecycle window; index-scoped = dated constituent change-log replay), reconstructing as-of metrics and emitting the identical `_normalise_result` dict. Grouped/labeled cache keyed `(symbol, scan_date, screen_config_hash)`.
- **GATE:** `as_of=None` byte-equal to the live FMP screener; historical scan returns identical dict shape + reuses all post-fetch filters; delisted symbols appear on traded dates only; cache-once (second `screen()` at same `(scan_date, hash)` → zero fetches); Phase-1 golden test re-passes (only an optional `as_of=None` param added).
- **Plan:** [`2026-06-13-backtest-platform-phase3-screener-history-plan.md`](./2026-06-13-backtest-platform-phase3-screener-history-plan.md)

### Phase 4 — Joint genetic optimizer
- **Scope:** Expand the existing DEAP `GeneticOptimizer` into ONE joint search over `[expert + classic-RM + ruleset/condition params]`. New `strategy_param_space` (collect/flatten/decode the joint dict, deep-copy tree substitution by id), classic-RM optimize columns on `Strategy`, `strategy_optimization_handler` (validate → seed RNG → hoist param-independent pass → fitness via Phase-2 runner → memo). Reuse the optimizer, `Backtest`/`StrategyOptimization` models, task queue, UI.
- **GATE:** a seeded GA run over a clean expert is reproducible — same `optimization_config.seed` + same cache/decoded params ⇒ **byte-equal** `best_params` and `best_fitness`; fitness map correct (`max_drawdown` negated, 0-trade sentinel distinct from the 0.0 exception fallback); decode-by-id deep-copy leaves source `Strategy` unmutated; memo-hit determinism self-check; no regression to the ML `run_backtest`/`handle_backtest` path. Verified by `./venv/bin/python -m pytest` in `BA2TestPlatform/backend`.
- **Plan:** [`2026-06-13-backtest-platform-phase4-joint-optimizer-plan.md`](./2026-06-13-backtest-platform-phase4-joint-optimizer-plan.md)

### Phase 5 — Cache-management UI + re-source ML datasets through providers
- **Scope:** (1) A brand-new `/api/cache` router + scanner: per-type disk usage with clean-all / by-type / by-date over every cache (ohlcv, jobs, news, dataset CSVs, trained_models, news exports, + the new `as_of` cache); "Clean All" excludes dataset CSVs + trained_models. (2) Re-source the ML dataset OHLCV builder through `ba2_providers`' as_of cache behind `OHLCV_SOURCE=ba2_providers|legacy` (default legacy), keeping `backtesting.py MLStrategy` unchanged.
- **GATE:** dataset build with `OHLCV_SOURCE=ba2_providers` reproduces the legacy training matrix byte-equal (or a documented/justified delta); cache-UI usage/delete operations work on a seeded cache with the destructive guard honored; ML training still runs end-to-end through the provider-sourced dataset; `BA2TradePlatform` byte-unchanged.
- **Plan:** [`2026-06-13-backtest-platform-phase5-cache-ui-ml-datasets-plan.md`](./2026-06-13-backtest-platform-phase5-cache-ui-ml-datasets-plan.md)

### Phase 6 — Migrate the live host onto the packages (LAST)
- **Scope:** Switch the live `BA2TradePlatform` from its in-tree `core`/`modules` to *consuming* the 3 packages via consume-by-shim (each extracted in-tree module becomes a re-export of its package twin so the ~hundreds of `from ba2_trade_platform…` call sites keep working). Build the live `LiveInstanceResolver` + `ModelFactoryLLMService` + seam helpers; wire all 6 seams in `core/seam_wiring.py::wire_all_seams()` called first in `main.initialize_system()`. Live-only pieces (concrete brokers, Smart RM, TradingAgents, the 3 AI providers, LLM stack, UI, instance caches, InstrumentAutoAdder) stay untouched.
- **GATE:** app boots end-to-end on the packages with `wire_all_seams()` running first (no `*NotConfigured` errors); the full live pytest suite stays green at the pre-migration baseline; the Phase-1 golden test passes through the wired live host for all clean experts. Verified by `tests/test_boot_smoke.py`, a real `python main.py --db-file <tmp>` boot, `pytest -q`, and `tests/test_phase6_golden.py`.
- **Plan:** [`2026-06-13-backtest-platform-phase6-migrate-live-plan.md`](./2026-06-13-backtest-platform-phase6-migrate-live-plan.md)

---

## 3. Autonomous execution strategy

How an agent runs the whole program end-to-end with minimal human intervention while staying safe.

### 3.1 Per-phase execution — subagent-driven development
For each phase, use **`superpowers:subagent-driven-development`**:
- Dispatch a **fresh subagent per task** (each plan numbers its tasks; task order within a phase is the dependency order stated in that plan's Execution Handoff).
- **Review between tasks**: the orchestrator reviews the subagent's diff + test output before dispatching the next task. A task is "done" only when its step checkboxes are checked AND its stated `pytest`/`lint-imports` command is green (evidence before assertion — `superpowers:verification-before-completion`).
- Within a phase, tasks marked independent in the plan (e.g. Phase 1 Tasks 5–10 per-expert splits after Tasks 1–4; Phase 5's two clusters; Phase 3 Tasks 5/6/7 after Task 4) may be **parallelized** via `superpowers:dispatching-parallel-agents` — but only after their shared prerequisite tasks have passed.
- Commit after each task (the plans embed the commit commands). Push is outward-facing → see STOP conditions.

### 3.2 Re-plan checkpoint pass — at the START of each phase
Before dispatching a phase's first task, run a **Re-plan checkpoint pass**:
1. **Re-confirm the prior phase's concrete outputs.** Every plan from Phase 1 onward carries explicit `> Re-plan checkpoint:` notes naming what it assumes from earlier phases (exact exported seam names, the `Recommendation` field set, `BacktestContext` shape, provider `get()` signature + OHLCV dict shape, the registry category keys, the Phase-2 daily-runner name, model/enum field names, the migration mechanism, etc.). Resolve each by **reading the actual installed package / merged code** (e.g. `python -c "import ba2_common.core.instance_resolver as m; print(dir(m))"`), not by assuming.
2. **Patch the plan in place** where reality differs from the plan's provisional name/path (these are *minor* drifts the plans explicitly invite: "if Phase 0 named it differently, use the real name"). Record the delta in the task's commit message.
3. If a checkpoint reveals a **structural** difference (a missing deliverable, a contract the prior phase did not actually ship, an ambiguous fork the plan flagged as an open question), this is a re-plan trigger → STOP for human (see 3.4).

This pass is what makes the autonomous run **correct rather than fabricated**: the plans deliberately flag every cross-phase dependency instead of hard-coding a guessed value.

### 3.3 HARD GATES between phases
A phase is **not complete** — and the next phase **must not start** — until that phase's **named acceptance gate (§2) passes**, proven by running the gate's exact verification command and observing green output. Specifically:
- **The Phase-1 golden test is the program's keystone gate.** It is *defined once* in Phase 1 (Task 12) and *re-run as a regression* by Phases 2, 3, and 6 (each says: reuse the Phase-1 harness, do not reinvent it). Treat any golden-test failure in a later phase as a logic-drift regression, not a new test to relax.
- **The golden test specifically gates the Phase-6 package refactor:** the live host migration is accepted only when the golden test passes *through the wired live host* (experts imported from `ba2_experts`, all I/O resolved through the live wiring) AND the full live suite is at its pre-migration baseline. Do not merge Phase 6 otherwise.
- Phase 2's gate additionally requires **byte-identical reproducibility** (same cache+params+seed) — this is the determinism prerequisite Phase 4's optimizer depends on; do not start Phase 4 until it holds.
- Each phase's gate also asserts **`BA2TradePlatform` is byte-unchanged** (Phases 0–5) — a non-empty `git -C BA2TradePlatform status` (outside the plan docs) is a hard failure until Phase 6.

### 3.4 Branch / worktree strategy per repo
- **One branch per repo per phase**, branched off the prior phase's branch (or `main` if the prior phase was merged):
  - Packages (`BA2TradeCommon`/`Providers`/`Experts`): `phase0-extraction` → `phase1-asof` → `phase3-screener-history` (Phases 2/4/5 touch the *backtest host*, not the packages).
  - Backtest host (`BA2TestPlatform`): a `phaseN-*` branch on `BA2TestPlatform` for Phases 2/3/4/5.
  - Live host (`BA2TradePlatform`): `phase6-migrate-onto-packages` (Phase 6 only).
- **Worktrees:** when a phase will run long or in parallel with review of a prior phase, isolate it in a git worktree (`superpowers:using-git-worktrees`) so the workspace stays clean and the live tree is never accidentally dirtied. Use a worktree only when explicitly requested or when the plan's task set is large enough to benefit from isolation.
- **Finish a branch** with `superpowers:finishing-a-development-branch` only after the phase gate is green and (for outward-facing repos) the user approves.

### 3.5 STOP conditions — pause for human
The autonomous run **must pause and surface to a human** when:
1. **Gate failure** — a phase's named acceptance gate (or the golden-test regression) does not go green after a bounded debugging effort (use `superpowers:systematic-debugging` first; if the root cause is a genuine plan/contract gap, stop).
2. **Ambiguous re-plan** — a start-of-phase Re-plan checkpoint reveals a *structural* mismatch (a missing prior-phase deliverable, a contract the prior phase did not ship, or one of the plans' explicit open questions that needs a product/scope decision — see §4). Minor name/path drift is auto-patched (3.2); structural ambiguity stops.
3. **Destructive or outward-facing step** — any `git push`, PR/merge, publishing a package branch, a schema migration that drops/recreates a populated table, a "Clean All"/destructive cache delete against a real cache, or **any** edit to the live `BA2TradePlatform/ba2_trade_platform/` tree before Phase 6 (and the Phase-6 merge itself, which touches the live trading app). These require explicit user confirmation.

When stopping, report: the phase/task, the exact failing command + output (or the ambiguous checkpoint), and the proposed resolution.

---

## 4. Consolidated open questions

Carried from the SHARED CONTRACTS and each phase's author results. Most are **provisionally resolved by a plan Decision** (noted) but should be confirmed at approval or at the relevant phase's Re-plan checkpoint. Items still genuinely open are marked **OPEN**.

**Resolved-by-decision (confirm at approval):**
1. **`current_price` source** — standardized on **OHLCV close at `as_of`** for ALL experts (Phase 1 Decision 1, Phase 2 Decision 5, Phase 3 Decision 2). Not the live broker quote, not FMPSenateTraderWeight's open-price helper. *Confirm open-vs-close and one-source-for-all is acceptable.*
2. **FMPRating consensus lookahead** — the two consensus endpoints have no per-row date ⇒ unavoidable backtest lookahead. Kept **in-scope** with `as_of=None` tested + a documented caveat; historical reconstruction (grades-historical) deferred to a later "FMPRating last" effort (Phase 1 Decision 4). *Confirm accept-as-documented vs reconstruct-now.*
3. **Insider `filingDate` fallback** = `transactionDate + 2 business days`; **statement effective_date** = `fillingDate` else `acceptedDate` else `fiscalDateEnding + 75d` (Phase 1 Decision 7). *Confirm the lag constants + verify FMP field-name casing/coverage with a live probe (`fillingDate` is FMP's known typo on statements; `filingDate` on insider).*
4. **Multi-asset diversification RM** (`per_instrument_cap_pct`, `max_concurrent_positions`) — enforced **only in the new multi-asset daily engine** (Phase 2 Decision 7); degrades to a re-entry cap under the legacy single-asset `MLStrategy` (Phase 4 Decision 5). *Confirm scoping.*
5. **Backtest DB** — separate per-run sqlite via `ba2_common.core.db.configure_db`, schema identical to live so inherited `AccountInterface` DB logic works unchanged (Phase 2 Decision 2). Two distinct DBs coexist: BA2TestPlatform's results DB (the `Backtest` table) + the per-run trading DB. *Confirm per-run-fresh lifecycle + the results-DB session-factory name (`SessionLocal`).*
6. **`Backtest.model_id`** — `nullable=False` in the BA2TestPlatform table; Phase 2 chooses a nullable migration + `engine_type` discriminator for expert runs. *Confirm the migration tooling (`db_migrate` vs alembic — recon found no alembic under `backend/`) and that nullable `model_id` is acceptable.*
7. **Optimizer expert-params scope** — Phase 4 assumes `model:<p>` ranges are searched jointly but the trained ML model is **frozen** (only decision thresholds), and ba2-expert numeric settings are fully searchable (Phase 4 Decision 3). *Confirm vs "expert entirely fixed, search only RM+ruleset".*
8. **ML-dataset re-source breadth** — OHLCV cut over first (byte-equality tractable); sentiment/fundamentals/macro wired in shape but kept default-legacy with per-block equivalence **deferred** (Phase 5 Decision 4). *Confirm defer-vs-verify-now.*
9. **Float as-of** — only the *current* `floatShares` is available; used as a static proxy for historical float filters (documented approximation, Phase 3 Decision 4). *Confirm acceptable vs disable float filters for historical scans (or use `/api/v4/shares_float` if the key tier returns dated rows — Phase 3 Task 2 probe decides).*

**OPEN (need a decision before or during the relevant phase):**
10. **FactorRanker engine contract** — FactorRanker has **no `ExpertRecommendation` seam** (it executes via `FactorPortfolioManager.rebalance`/`submit_order` directly). Phase 1 makes `_process` *produce* target weights in `raw_outputs['targets']` purely; Phase 2 explicitly scopes FactorRanker **out** (EarningsDrift/InsiderClusterBuy use the `ExpertRecommendation` path). **OPEN for Phase 4 engine wiring:** confirm the engine accepts weight-based targets directly and whether classic RM/position_sizing applies to weight targets or FactorRanker bypasses the shared enter/exit ruleset in backtest.
11. **Multi-symbol `_process` return shape** — does `_process` return one `Recommendation` per call (engine loops symbols) or a `List[Recommendation]` for basket experts (FMPSenateTraderCopy/Weight, FactorRanker)? Phase 1 provisionally supports `List` with element-wise golden comparison (Phase 1 Task 7) — **confirm the engine contract** with Phase 2/4.
12. **BACKTEST CONTRACT routing depth** — Phase 2 drives the FULL live path (`persist ExpertRecommendation → TradeManager.process_recommendation → TradeRiskManagement.review_and_prioritize_pending_orders → account.submit_order`) for maximum reuse. **Confirm** Phase 1 expects this (vs a thinner enter/exit evaluator) and that `TradeManager` has no live-only coupling that breaks against the backtest DB.
13. **Phase-2 daily-runner API for the optimizer** — Phase 4's fitness needs Phase 2's **synchronous in-process** runner (`run_daily_backtest(config, hoisted, decoded) -> results_dict`, same keys as `_convert_bt_results`), NOT the async `handle_daily_backtest` (max_workers=1 deadlock). **Confirm/extract** the synchronous runner when Phase 2 lands.
14. **ATR injection pattern** — which pattern Phase 0 shipped for `get_latest_atr`: (a) RM accepts an `indicator_provider` arg threaded at the call site, or (b) `position_sizing` exposes a module-level `set_default_indicator_provider`. Phase 2/6 guard both; **read the installed package to pick one.**
15. **Consume-by-shim vs delete-and-rewrite (Phase 6)** — shim is the default (reversible, zero live-caller edits). **Confirm at Phase-6 approval** whether a full delete+rewrite of the in-tree dirs is preferred instead.
16. **Phase-0 module paths** — exact post-extraction paths (`ba2_experts.FMPEarningsDrift` vs `ba2_experts.experts.*`; `StockScreener` in `ba2_providers` vs `ba2_common.core`) and the `ba2_providers.get_provider` registry category keys + `TradeConditions._get_provider` signature. Provisionally fixed by Phase 0's layout; **confirm at each consuming phase's Re-plan checkpoint** (Phases 1/2/3/6 all flag this).

---

## 5. Cross-phase consistency status (this pass)

A cross-phase consistency review was run over all seven plan docs. Findings:

- **Seam APIs are consistent** across every phase: `_gather(providers, as_of)` / pure `_process(bundle, settings, as_of)` / `analyze_as_of(as_of, context)`; the `Recommendation` value object fields (`signal, confidence, current_price, details, expected_profit_percent, raw_outputs, skip, skip_reason`); `set_instance_resolver`/`get_instance_resolver` + `get_expert_instance`/`get_account_instance`/`get_account_instance_from_transaction`; `set_llm_service`/`get_llm_service` + `create_llm`/`do_llm_call_with_websearch`; `db.configure_db`/`get_engine`; `TradeConditions.set_provider_resolver(fn(category, name, **kw))`; `position_sizing.get_latest_atr(symbol, indicator_provider, …)`; `set_instrument_auto_adder_hook`.
- **The golden test is defined once** (Phase 1 Task 12) and **referenced as a regression gate** by Phases 2, 3, and 6 (each instructs reuse of the Phase-1 harness, not reinvention).
- **Phase dependencies are correct** (0→1→2→3→4→5; 6 last, consuming all). No phase consumes an output its predecessor does not produce; every cross-phase assumption is flagged with an explicit `> Re-plan checkpoint:`.
- **No contradictory file targets:** packages (Phases 0/1/3) vs `BA2TestPlatform/backend/` host (Phases 2/4/5) vs the live tree (Phase 6 only) are cleanly partitioned; the live tree is read-only until Phase 6 in every plan's gate.
- **`current_price` (OHLCV close at as_of, one source)** and **FactorRanker out-of-scope-until-Phase-4** are handled consistently across phases.

**Inconsistencies found:**
- **(Minor, FIXED in-place)** Phase 3 placed its BA2TestPlatform tests and pytest invocations under bare `python -m pytest` from the `BA2TestPlatform` root, diverging from the locked `backend/CLAUDE.md` convention used by Phases 2/4/5 (the `BA2TestPlatform/backend/venv/bin/python` interpreter). Six pytest commands in Phase 3 were corrected to `./backend/venv/bin/python -m pytest` (cwd kept at the `BA2TestPlatform` root so the test's `from backend.app.services…` imports still resolve). The Phase-3 host code paths (`backend/app/services/screener_history_cache.py`) were already correct.
- **(Larger, REPORTED not auto-fixed)** The SHARED CONTRACTS' `per_phase_scope`/`phase_dependencies` use a **different phase numbering** than the authoritative plan files (the contract's phase_1+phase_2 are merged into plan-Phase-1, shifting the rest down by one). This is a documentation-numbering mismatch only — every plan's dependencies and content are internally correct, and the Phase-4 plan already documents the off-by-one. It is **not** auto-rewritten because the plan files are authoritative and self-consistent; §"Numbering note" at the top of this roadmap is the canonical mapping. No content change is warranted.
