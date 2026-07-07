# Live ↔ Backtest Engine Unification — Design & Plan (2026-07-02)

**Goal:** run the **same engine** in live and backtest — 100% *behavioral coverage*
(every decision the live engine makes is exercised identically in the backtest, and
vice versa) — **without wrecking backtest throughput** (hard budget: **≤ 20–30%**
perf hit vs today's grid).

**Scope (LOCKED 2026-07-02):** "same engine / 100% coverage" is officially bounded to
**classic-RM experts + bypass experts** (e.g. FactorRanker). **Smart-RM** and
**AI-driven / non-deterministic universe** experts are **out of parity scope** — they
have no faithful, deterministic backtest. The backtest must **fail loud** (clear
error, not a silent different-policy run) when asked to backtest/claim parity for a
Smart-RM or AI-universe expert. "100% coverage" = 100% of the *in-scope* engine.

**TL;DR (the thesis, confirmed by a 7-reader code audit):** the **decision core is
already shared**; the **orchestration/driver loop is reimplemented** in the
backtest, and that reimplementation is where every parity gap lives. The backtest's
speed comes entirely from **I/O seams** (in-memory order cache, flat-bar skip,
cadence dedup, columnar as-of price store, hermetic caches) — routing the backtest
through the live `TradeManager`/`JobManager` code would re-introduce a broker
round-trip + DB SELECT + ActivityLog write + scheduler tick **per simulated bar**,
reversing the 11.4× win. **So: unify the DECISION + ORCHESTRATION-LOGIC into shared,
seam-parameterized code that both drivers call; keep the DRIVER LOOP + I/O behind
seams.** Then prove parity with a golden live→backtest replay harness (which does
not exist today).

---

## 1. Current parity assessment

### Already shared (phase-6 `ba2_common`) — the DECISION core
Both live `TradeManager` and backtest `daily_engine` call the **same** classes:
- `TradeActionEvaluator.evaluate/execute` (enter_market AND open_positions) — `TradeManager.py:1075/1705` and `daily_engine.py:831/914`.
- `TradeConditions.*` (full condition catalog), `TradeActions.*` (Buy/Sell/Close/AdjustTP/AdjustSL/Increase/Decrease + 6 option actions).
- `TradeRiskManagement.review_and_prioritize_pending_orders` + `position_sizing.compute_risk_based_quantity / synthesize_safeguard_stop / derive_stop_for_quantity / get_latest_atr`.
- `rule_builders.action_from_rule / triggers_from_condition_tree`.
- The expert decision itself: `run_analysis` (live) and `analyze_as_of` (backtest) are different **entry points** that both funnel into the byte-identical `_gather` + `_process`.
- Entry TP/SL brackets are now **structurally unified** (migration 026: TP/SL are ordinary rules converted by the shared `action_from_rule`, executed by the shared evaluator — no separate entry-bracket codepath).

### Reimplemented (backtest `daily_engine`) — the ORCHESTRATION/driver
`daily_engine` deliberately does **not** import `TradeManager` (docstring), and
re-writes its bodies from the same packaged pieces:
- `TradeManager.process_expert_recommendations_after_analysis` → `_run_expert_bar` (763) + `_size_and_submit` (1144).
- `TradeManager.process_open_positions_recommendations` → `_manage_open_positions` (853).
- `run_analysis` step-6 (Recommendation→`ExpertRecommendation` row) → `_recommendation_to_expert_recommendation` (260).
- `JobManager._execute_*_analysis` scheduling/instrument-selection → `_entry_schedule`/`_manage_schedule`/`_schedule_allows_entry` (196) + `resolve_universe`/`_screened_symbols_for_bar` (95).
- Live broker fill/OCO/TP-SL → `BacktestAccount.refresh_orders` (the fill engine) + option expiry/assignment/margin-call (no live analog — broker-owned).

### Honest coverage estimate
- **Decision math: ~95–100% parity** for classic-RM experts (same code; residual is data-seam fidelity, §2).
- **Orchestration: ~80% behavioral parity** — better than it looks: capital-allocation *prioritization* is already the shared batched RM (verified §2 gap #1), and A2/A3/B1/C1/C2 + entry brackets are fixed. The remaining true gaps are structural: universe/instrument selection (#2), Smart-RM mode (#3), self-executing experts (#4), SELL-exit sizing (#7), account-seam OCO/CLOSE fidelity (#6), and the rec-set/window composition (#1 residual).
- **Whole-run parity is currently *asserted by code audit + shared-code reuse*, NOT measured** — there is **no** live→backtest comparison harness (the "sync" channel is worker DR replication, unrelated). This is the single biggest missing piece.

---

## 2. Parity-gap matrix (most-severe first)

| # | Behavior | Live (file:line) | Backtest | Gap | Sev | Perf-sens | Unify approach |
|---|---|---|---|---|---|---|---|
| 1 | **Cross-symbol capital allocation — rec-SET composition** | RM drains a **lookback-window** batch by priority (`process_expert_recommendations_after_analysis` → `review_and_prioritize_pending_orders`) | `_run_expert_bar` creates PENDING orders for ALL of the bar's symbols, then `_size_and_submit` (1160-1162) runs the **same** `review_and_prioritize_pending_orders` over the whole batch — so prioritization/capital-drain is already SHARED + batched per bar. *(Verified: the "universe order" claim was wrong.)* Residual: the batch is **one bar's** recs vs live's **lookback-window** drain (which may span multiple analysis cycles), so under a capital constraint the *funded set* can still differ when live's window aggregates recs the BT bar doesn't | high→**medium** | med | Align the batch boundary: model live's lookback-window drain semantics (or confirm the single-bar batch == the live per-drain batch given the shared cadence); assert via the golden harness. NOT a per-symbol-ordering fix — that's already correct |
| 2 | **Instrument/universe selection** | `JobManager._execute_dynamic/_expert_driven/_screener_analysis` (live per-scan screen; AI-driven) | offline screener-cache union; no LLM universe | Survivorship + universe drift; non-deterministic LLM universes unrepresented | **critical** | med | Scope parity claims to deterministic universes; snapshot live screen results per scan-date into the as-of cache; document AI-universe as out-of-parity |
| 3 | **Risk-manager mode (classic vs smart)** | `WorkerQueue._check_and_process...` branches `risk_manager_mode`; SmartRiskManager (live-only) | no Smart-RM emulation | A `smart` expert's live sizing/entry policy is **not** what the backtest models | **out-of-scope (locked)** | high | **SCOPED OUT.** Backtest **fails loud** on a Smart-RM expert (no silent different-policy run). Parity = classic-RM + bypass only |
| 4 | **Self-executing experts (`expert_uses_risk_manager` opt-out)** | classic path skipped for self-executing experts | backtest applies classic path | Entirely wrong trades if mismatched | high | high | Thread the opt-out through the shared cycle; backtest honors it |
| 5 | **OPEN_POSITIONS rec subtype selection** (audit A3) | latest rec in lookback, `MarketAnalysis.subtype` heuristic w/ ALL-rec fallback (`TradeManager.py:1608-1656`) | always a **fresh** OPEN_POSITIONS rec (`daily_engine.py:892`) | Live can evaluate exits against a stale ENTER rec | high→**fixed** | low | Audit A3 fixed; converge fully by adding `ExpertRecommendation.subtype` column + always-fresh manage rec in both |
| 6 | **TP/SL bracket mechanics + OCO fill precedence + CLOSE sibling-leg cleanup** | broker OCO/OTO, WAITING_TRIGGER dependent leg rebased to fill; `adjust_tp_sl` merges last-TP+last-SL | `BacktestAccount` models OCO SL-first, next-bar leg fills | Exit P&L drifts if leg lifecycle/precedence not matched | high | high | Keep as an **account-seam contract** with a documented, tested spec; assert via the golden harness |
| 7 | **Plain SELL exit sizing** | staged qty=0 SELL sized by BUY-oriented notional RM | same shared RM (BUY-oriented) | A SELL exit can be sized ≠ the held position | high | med | Add explicit exit-quantity handling in shared RM (size SELL to held qty, not notional cap) — fixes a real bug in both |
| 8 | **Protective-stop reconciliation at submit (tighter-wins)** | live attaches safeguard leg; tighter-wins **not re-confirmed** in live tail | `_size_and_submit` does explicit tighter-wins merge (1177) | BT may do *more* than live → realized per-trade risk differs | high | low | Move the tighter-wins reconciliation into shared `position_sizing`/RM so both apply it identically |
| 9 | **ATR source + clock (sizing seam)** | live network indicator provider @ now | offline MetricStore/AsOf ATR @ bar | Cache/live ATR mismatch silently changes size | high | high | Already injectable (`TradeRiskManagement.py:99`, `position_sizing.py:262`) — the Phase-0 seam; keep injectable, never hardcode the singleton; parity-test the ATR value |
| 10 | **Analysis cadence / scheduling** | APScheduler cron per expert/use-case | bar-cadence gating + once-per-day dedup | Per-day dedup can starve a sub-pass if entry/manage pin different times (audit C2, fixed); "no times" = every-bar live vs once/day BT | high→**fixed** | high | C2 fixed; keep the cadence as a **schedule seam** feeding the shared cycle |
| 11 | **Expert data seam (FMP consensus)** | current-snapshot FMP endpoints + real-time quote | reconstructed from dated-history + as-of close | Backtest is a **proxy** of the live decision, not a replay (FMP's proprietary consensus vs a rolling mean) | medium | low | Accept + document as an inherent as-of limitation; measure divergence via the harness; consider capturing live consensus snapshots dated for future replay |
| 12 | **Order lifecycle timing** | same-cycle fill; manages WAITING+OPENED; wash-trade delay | next-bar fill; OPENED-only; wash-trade disabled | Documented-OK modeling choices | doc-OK | med | Re-state as accepted; assert wash-trade/limit-entry are the only WAITING divergences |
| 13 | **Duplicate-position + equity gates** | dup/equity safety checks before entry | relies on `has_no_position` in the tree; BT force-trades | Custom trees can stack dupes / overtrade near drawdown floor | high | low | Move dup + equity-sufficiency gates into the shared cycle so both enforce them regardless of ruleset |
| 14 | **Automation permission gates** | `allow_automated_trade_opening/modification` gate real orders | BT forces gates ON | BT can't reproduce a "logged-only" live config | doc-OK | low | Accept (BT is trading-intent) but thread the flag so a golden replay of a logged-only period matches |
| 15 | **Recommendation→row fidelity** | full mapping incl. subtype, risk_level, market_analysis_id | hardcoded risk_level/time_horizon, null MA id, `raw_outputs` vs nested `FMPRating` payload | Rules/consumers keying off those fields differ (e.g. option `consensus_target` strike) | medium | low | Converge the mapping in a shared helper; add `subtype` column; nest the expert payload identically |
| 16 | **Eval-audit rows (`TradeActionResult`)** | live persists per-eval | BT skips | Observability only; no equity effect | doc-OK | high | Keep optional/off on the BT hot path |

**Fixed since the 2026-07-01 audit:** A2 (washtrade SL), A3 (subtype), B1 (washtrade
no-op), C1 (SL ratchet — shared), C2 (dedup starve), entry-bracket unification.
**Still open:** A1 (IBKR `submit_order` override — neutralized by disabling IBKR, not
reworked), A4 (UI `_place_order` no tp/sl).

---

## 3. Target architecture

**Principle:** extend phase-6. Phase-6 shared the *decision core*. This work shares
the *orchestration logic* — as **pure, seam-parameterized functions** — while the
*driver loop* and *I/O* stay platform-specific behind seams.

### Options considered
- **A — Shared "TradeCycle" orchestration core (RECOMMENDED).** Extract the
  logic of `TradeManager.process_expert_recommendations_after_analysis` and
  `process_open_positions_recommendations` into `ba2_common.core.trade_cycle` as
  pure functions parameterized by seams: `AccountSeam` (submit/refresh/positions —
  live broker vs `BacktestAccount`), `ClockSeam` (now vs bar `as_of`), `DataSeam`
  (providers — live vs hermetic as-of), `PersistenceSeam` (DB rows vs in-memory,
  and optional eval-audit rows), `ScheduleSeam` (cron vs bar-cadence). Both live
  `TradeManager` and `daily_engine` become **thin adapters** that build the seams
  and call the shared cycle. The perf seams (in-memory order cache, gating,
  caches) live *inside the backtest's seam implementations* — the shared logic is
  unchanged, so no perf regression from the logic move.
- **B — Backtest drives live `TradeManager` directly** with a simulated clock +
  account. **REJECTED**: the perf reader shows this re-introduces a broker
  round-trip + DB SELECT + ActivityLog write + scheduler tick **per bar** →
  reverses the 11.4× / flat-skip / cadence wins. Fails the perf budget outright.
- **C — Keep two loops; close each gap in shared code + add a golden harness.**
  The incremental fallback; it *is* Phase 1 of Option A (close the gaps in shared
  helpers first, then lift the driver skeleton).

### Recommendation: A, reached incrementally via C
Do C first (close the high-severity gaps by moving their logic into shared helpers
+ stand up the golden harness), then lift the remaining driver skeleton into
`trade_cycle` so `_run_expert_bar`/`_size_and_submit`/`_manage_open_positions` and
the live equivalents become adapters over one body.

### What stays legitimately divergent (model explicitly, not "fix")
Real broker friction — wash-trade delay/rejection, same-cycle vs next-bar fill,
partial/limit fills, early American assignment, manual TP/SL override locks,
concurrency skips. These are **account-seam** behaviors documented as approximations
and covered by the account-seam contract tests, not unified away.

---

## 4. Perf strategy (staying in the 20–30% budget)

The perf reader quantified today's wins and the cost of losing them:

| BT optimization | Saves | Must stay as… |
|---|---|---|
| In-memory order cache (`_active_orders`/`invalidate_order_cache`, book_dirty) | 62× fewer DB queries, 11.4× wall | AccountSeam impl (never a live broker read on the hot path) |
| Flat-bar skip (`_has_activity` + `analysis_idx` bisect) | ~59k idle 5min bars → a handful | Driver-loop-only (no live analog; time can't fast-forward live) |
| No-fill gating of the txn roll + bracket pass | ~½ per-bar cost | Driver gating around the shared roll |
| Cadence dedup (once per expert/day) | 78×/day → 1× analysis | ScheduleSeam |
| Columnar as-of price store + O(1) clock cursor | next_bar 320× | DataSeam/ClockSeam |
| OHLCV memo + hermetic FMP disk cache | ~370s / 206s→3.2s | DataSeam |
| `frozen_ttl_cache`, activity/file logging disabled | thousands of fetches / DB writes | run-scoped context managers (unchanged) |

**Why the logic move is cheap (<5%):** relocating orchestration *logic* into
`trade_cycle` moves the *same* computation; the expensive I/O stays behind the same
seams. The only new per-decision cost is a handful of lookups already present
(e.g. the tighter-wins `get_instance(Transaction)` at `_size_and_submit` 1177 —
negligible, entry-only).

**The one genuine cost is a *correctness* change, not a perf change:** fixing gap #1
(batch-prioritize recs before sizing) means the backtest collects a bar's recs
before the RM tail instead of sizing per-symbol. That is **already how live works**;
it adds one in-memory sort + a single batched RM pass per (expert, bar) — cheaper
than N per-symbol RM passes, if anything. Budget-safe.

**Dangerous moves to forbid (would blow the budget):** any per-bar broker call, per-
recommendation DB SELECT/write on the hot path, per-order ORM fill evaluation,
per-bar network fetch, per-bar wall-clock scheduler tick, always-on eval-audit rows,
coupling analysis cadence to the fill clock (~245× on daily→5min). The
`trade_cycle` API must make these *impossible* by construction (they live only in
the live seam impls).

**Perf gate:** every phase runs the existing perf guards + a wall-clock benchmark on
a fixed grid trial (e.g. 1yr × 8 syms × 5min); regression must stay < 20% cumulative
(target < 10%), else the phase is reworked.

---

## 5. Parity validation harness (the missing evidence)

Today parity is *asserted*, not *measured*. Build a **golden replay harness**:

1. **Capture** a live period's ground truth: the persisted `ExpertRecommendation`
   rows (decision + `price_at_date`/`expected_profit_percent`), the resulting
   `TradingOrder`/`Transaction` rows (sized qty, stop, TP/SL, fills), and the
   account balance timeline, from the live DB (read-only `BA2_LIVE_DB`).
2. **Replay** the *same recommendations* (not re-derived) through the shared
   `trade_cycle` with a `ReplayAccountSeam` (fills forced to the live fills) and a
   `ReplayClockSeam`, and assert **decision identity**: same orders created, same
   sized quantities, same stop/TP prices, same exit actions, same order of capital
   consumption. Divergences are parity bugs.
3. **Separately**, replay the same *as-of data* through `analyze_as_of` and measure
   the **decision proxy error** (gap #11) — the reconstruction fidelity of the
   data seam — as a distribution, not pass/fail.
4. **"100% coverage" definition:** every `trade_cycle` branch (each condition, each
   action type, each RM path, each gate) is exercised by at least one golden case
   AND produces the live-identical decision; tracked as branch coverage over
   `trade_cycle` + the account-seam contract. A coverage report is the artifact.

(Note: the existing "sync"/`sync_receiver` channel is **not** this — it replicates
the test platform's own rows to GA workers. The golden harness is new.)

---

## 6. Phased plan (each phase shippable + perf-gated)

**Phase 0 — Parity harness + baseline (evidence first).**
Build the golden replay harness (§5) against a captured live window; produce the
first parity report + the decision-proxy error distribution. Add a fixed-trial
wall-clock benchmark. *No behavior change* — this measures the starting point and
guards every later phase. Files: new `testplatform/backend/app/services/backtest/parity_harness.py` + `tests/backtest/test_parity_golden.py`.

**Phase 1 — Close the critical decision-parity gaps in shared code (Option C).**
- **1a. Rec-set/window composition (gap #1 residual).** Prioritization is ALREADY
  the shared batched RM — do NOT rebuild it. Instead reconcile the *batch boundary*:
  confirm (via the golden harness) that the BT single-bar batch produces the same
  funded set as live's lookback-window drain given the shared cadence, and align the
  window semantics if not. Cheap (in-memory).
- **1b. Explicit SELL-exit sizing (gap #7) + tighter-wins stop reconciliation
  (gap #8)** into shared `position_sizing`/RM so both platforms apply identically.
- **1c. Dup-position + equity-sufficiency gates (gap #13)** into the shared cycle.
- **1d. `ExpertRecommendation.subtype` column (gap #5/#15)** + converge the
  rec→row mapping + nested expert payload in a shared helper; removes the live
  subtype-join and the last manage-pass divergence. (migration 028)
Each sub-phase: shared extraction + golden-harness assertion turns from ❌→✅ + perf gate.

**Phase 2 — Account-seam contract (gap #6, #12).**
Write a formal `AccountSeam` contract spec + contract tests covering order lifecycle,
OCO leg creation/cancel/precedence, CLOSE sibling cleanup, WAITING_TRIGGER rebase,
per-expert quantity accounting. `BacktestAccount` and the broker accounts both pass
the contract; documented approximations (next-bar fill, SL-first OCO, no early
assignment) are explicit contract clauses.

**Phase 3 — Lift the driver skeleton into `ba2_common.core.trade_cycle` (Option A).**
Extract `process_expert_recommendations_after_analysis` /
`process_open_positions_recommendations` bodies into seam-parameterized functions;
make live `TradeManager` and `daily_engine` thin adapters. The backtest's perf
seams (order cache, gating, caches, cadence) become the injected impls — verified by
the perf gate that throughput is within budget. Golden harness must stay 100%.

**Phase 4 — Enforce the locked scope: fail-loud the out-of-scope (gap #2, #3, #4).**
Per the locked scope decision, make the backtest **refuse with a clear error** when a
Smart-RM or AI-driven-universe expert is submitted (no silent different-policy run) —
this is a definitive guard, not "unless a deterministic core exists". Snapshot live
screener scans into the as-of cache for *deterministic*-universe parity; AI-universe
stays explicitly out of scope. Thread `expert_uses_risk_manager` (self-executing)
through the shared cycle. Fold in the audit's remaining opens (A1 IBKR rework if IBKR
is ever re-enabled; A4).

**Phase 5 — Continuous parity gate.**
Wire the golden harness into CI (a small captured window) so any future change that
breaks live↔backtest decision identity fails the build. Publish the coverage report.

---

## 7. Risks & open questions

- **Smart-RM (gap #3) — RESOLVED (locked):** permanently scoped OUT of parity; the
  backtest fails loud on a Smart-RM expert. "100% coverage" means 100% of the
  in-scope (classic-RM + bypass) engine. (A deterministic Smart-RM core could be a
  separate future project, but is explicitly not part of this goal.)
- **Data-seam proxy error (gap #11)** is irreducible for FMP consensus (proprietary
  aggregation, no dated snapshots). Best mitigation: start capturing dated live
  consensus snapshots now for future faithful replay.
- **FMP historical revision/back-fill** (survivorship-lookahead the code can't
  detect) — needs an external data-integrity assumption stated explicitly.
- **Capital-allocation fix (gap #1) will change historical backtest results** (by
  design — it makes them *correct*). Communicate that saved/optimized results shift.
- **Effort vs payoff:** Phases 0–1 deliver most of the parity value at low risk;
  Phase 3 (driver lift) is the largest refactor — do it only after 0–2 prove the
  seams and the harness catches regressions.
- **A1 IBKR** stays a latent live bug behind the disabled broker; rework only when
  IBKR trading is re-enabled.
