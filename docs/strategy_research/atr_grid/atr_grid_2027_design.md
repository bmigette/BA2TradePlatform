# The 2027 risk-ATR grid: design draft

Status: **DRAFT, 2026-09-24. Not approved, nothing implemented.** It builds on
`s1_s7_relevance.md` (same folder), the goal2020 runbook (`docs/RUNBOOK-goal2020-grid.md`), the
market-condition design (`docs/plans/2026-09-15-option-market-condition-genes-design.md` §6.0) and
the market-exit work on `feat/pullback-market-exits` (worktree `BA2-pullback`).

Working name: **`goal2027atr`**. Job suffix `-atr27-<mode>`; the FMPRating suffix stays `-from2022`.

## 1. Goal and baseline

**Goal.**
- Re-optimize the classic experts with the ATR stop genuinely on and searchable.
- Add market-condition entry gates and market-condition exit/stop/TP rules to the equity strategies.
- Use one window, 2020–2026, so every year from the COVID crash to the newest full year is scored.
- Produce deploy candidates whose live settings (`use_atr_stop`, `atr_multiplier`, `atr_period`,
  `allow_ruleset_sl_loosen`, `market_condition_profile`) are values that were actually exercised,
  rather than carried as inert numbers.

**This is a NEW BASELINE.** It is not a continuation of goal2020, and its fitnesses must never be ranked against the goal2020 archive:
- **ATR changes behaviour.** Every run on record executed with `use_atr_stop` off (the "1"-string defect, then `INERT_RM_TOGGLES`), so `atr_multiplier` and `atr_period` have never acted.
- **The window changes.** It adds 2026 (7 calendar buckets instead of 6; FMPRating 5 instead of 4). Because `consistent_annual_return` buckets by calendar year, this changes the consistency factor.
- **The gene space changes shape.** Market genes, the ATR gene and `allow_ruleset_sl_loosen` are added; the regime scales and weekend schedule genes are dropped.
- **The budget rule changes.** Population 120 and up to 30 generations, against goal2020's 40–140 × 8.

**Comparison with goal2020 happens only on economics** (CAR, drawdown, per-year, concentration), cell against cell, and never by merging rows. The per-year returns of the overlapping years, 2020–2025, are the like-for-like part.

## 2. Window and data prerequisites

- **Window:** 2020-01-01 → 2026-12-31, launched in 2027 once the December data has settled
  (mid-January at the earliest).
- **FMPRating keeps its 2022-01-01 floor.** `_EXPERT_MIN_START` in `tools/run_screener_capband_matrix.py` stays as is, and the names keep `-from2022`.
- **Holdout:** see open decision D1. The operator asked for 2020–2026 data, so this draft fits on all of it and asks for a 2027-H1 forward confirmation before any deploy.

Preflight list, all to be run on the master before job 1; the driver should refuse if any fail.

| # | Check | Today (2026-09-24) | Needed |
|---|---|---|---|
| P1 | Screener metric store partitions | `ym=2020-01` … `ym=2026-06` (78 partitions) | extend to `ym=2026-12`. The goal2020 preflight checks only the START (`first > "2020-01"`); add an END check (`last >= "2026-12"`). |
| P2 | 5min OHLCV cache for each band's screened union (loosest-gene union, as `grid_goal2020.sh` derives it) | goal2020 downloads ran 2020-01-02 → 2026-06-30 | backfill to 2026-12-31. **`tools/check_window_coverage.py` only takes `--start`**, so a cache that stops mid-2026 passes today. It needs an `--end` probe, or 2026-H2 silently trades on no bars. |
| P3 | 1d OHLCV for the same unions, plus ATR warm-up (≥ `max(atr_period × ATR_LOOKBACK_MULTIPLE, ATR_LOOKBACK_MIN_DAYS)` sessions before 2020-01-01, from `ba2_common/core/replay/dependencies.py`) | covered to mid-2026 | extend to 2026-12-31, and confirm `warmup_days` ≥ the ATR lookback at `atr_period` = 28 |
| P4 | FMPRating: price targets and grades | cache frozen at download | refresh through 2026-12-31; keep the 2022 floor |
| P5 | FMPEarningsDrift: earnings calendar and surprises | — | through 2026-12-31 (plus the next report date for events straddling year end) |
| P6 | FMPInsiderClusterBuy: Form-4 | — | through 2026-12-31 (filings are public at filing date; no lag issue) |
| P7 | FMPSenateTraderWeight: disclosures | — | through 2026-12-31. Tradeable at **disclosure** date (the `6d08a34e` fix), so late-2026 trades disclosed in 2027 correctly do not appear. Rebuild the sharded scoring JSONL for the new window (`reference-senate-scoring-caches-operational`). |
| P8 | DeterministicScorer sections: analyst, earnings, fundamentals (`screener_fundamentals`), macro (`fred`), technical | — | all through 2026-12-31 |
| P9 | Market-condition snapshots (`ohlcv-v1`, `ta-structure-v1`) for the **equity** universe | only the 97-symbol option universe, 2020-01-02..2025-12-31 | `plan` → `build` → `verify` → `prepare-host` over the union of the band universes, decision dates 2020-01-02..2026-12-31, on the master and **every** worker (digests are per host). See §5.3 for the scale risk. |
| P10 | Split-basis drift | 13 of 98 option symbols needed a full refetch | expect about 13% of the equity universe to need `--fetch-missing`; size the provider bill with `plan` first |
| P11 | Spread assumptions | 3 / 9 / 17 bps plus a 1.5× stress, measured on 2022 and 2024–25 | optionally add a 2026 quote sample; otherwise keep the values |
| P12 | Versions and pushes | — | push before launch. Never bump either `version.py` mid-run. Master and workers on the same `TEST_APP_VERSION`. |
| P13 | Checkout isolation | — | run from a checkout that nobody merges into during the grid (runbook checklist item 1) |

## 3. ATR: unpin `use_atr_stop` and search it, for this grid only

### 3.1 What happens today (cited)

The toggle is pinned in two places, deliberately mirrored:
- `testplatform/ba2test_launcher.py:1040`: `_INERT_RM_TOGGLES = {"use_atr_stop": False, "regime_overlay_enabled": False}`, applied by `_expert_run_settings` (`:1071`) before any caller `overrides`.
- `testplatform/backend/app/services/strategy_param_space.py:1019`: `INERT_RM_TOGGLES`, the same dict. `_build_daily_trial_config` (`strategy_optimization_handler.py:2458`) applies it **last, above the decoded genes** (`:2578`, `:2583`), so even a stored genome saying `model:use_atr_stop: 1` runs with it off.

The rest of the path:
- **Search space.** `_RM_OPT` (`ba2test_launcher.py:1454`) declares `use_atr_stop` with `optimize: False`, but still searches `atr_multiplier` (3.0–6.0) and `atr_period` (7–28). Those two are inert.
- **Default.** `use_atr_stop` declares `default: True` (`MarketExpertInterface.py:344`). Leaving it unpinned is therefore not "off": absence turns it ON.
- **Effect.** `TradeRiskManagement._ensure_safeguard_stop` (`TradeRiskManagement.py:1647`, read at `:1680`) fetches ATR only when the toggle is on. The safeguard stop is the tighter of `atr_multiplier×ATR` and `risk_per_trade_pct%`, floored at `min_stop_loss_pct%`.
  - The stop is placed in **every** sizing mode (`replay/dependencies.py` ~508). The interface tooltip saying "risk_atr only" is wrong on this point.
  - Under `risk_atr`, the same stop also sets the position size (`_risk_atr_quantity` calls the safeguard first).
- **Tests pinning today's behaviour:**
  - `testplatform/backend/tests/backtest/test_inert_rm_toggles_stay_off.py`. It includes `test_a_decoded_gene_saying_ON_does_not_win`, and the two constants are pinned equal.
  - `tests/test_inert_rm_toggles_are_measured.py`.

**So today there is no way to run a GA with ATR on**: even a run-level `overrides={"use_atr_stop": True}` is re-pinned per trial.

### 3.2 Change, scoped to this grid

The rule: every run without the new policy stays byte-identical. Current deploys, saved backtests, re-runs and the goal2020 archive do not move.

1. **A run-level policy, persisted with the run.**
   - A new launcher flag (e.g. `--rm-toggle-policy atr-searched`) writes `backtest_block["rm_toggles_unpinned"] = ["use_atr_stop"]` into `optimization_config.backtest`.
   - It lives in the run config, never in an env var: every path that rebuilds a trial from a persisted config must see it. Those paths are `_persist_top_backtests`, the re-run handler, the robustness variants, `tools/recover_missing_topn.py` and `tools/rerun_dev_deployed_on_worker.py`.
   - Otherwise a TOP-N re-run would re-pin ATR off and persist a different strategy from the one the GA scored, which is the persisted-TOP-N bug class again.
2. **Search space.** `_rm_opt_for(kind, sizing_mode, atr_searched=True)` returns `_RM_OPT` with:
   - `use_atr_stop` → `optimize: True`, int 0–1. It is searched, not dropped, because its default is True.
   - the three `regime_*_scale` genes removed. The overlay stays pinned off (D3), and they are dead weight.
   - `atr_multiplier` / `atr_period` ranges per D4.
3. **Pinning.**
   - `_expert_run_settings` and `_build_daily_trial_config` apply `INERT_RM_TOGGLES` minus the keys in `rm_toggles_unpinned`.
   - `regime_overlay_enabled` can never be unpinned by this policy. Refuse any other key.
   - The constant itself does not change.
4. **Identity.** Include the policy in the GA checkpoint fingerprint and in a new driver config digest (Task 11 already calls for one). Refuse the policy on a job whose name lacks `-atr27`.
5. **Export parity.**
   - `_executed_toggle` (`testplatform/backend/app/api/backtests.py:1330`) already reads `model:use_atr_stop` off the row, so a searched gene exports correctly with no change.
   - The trap is the **absent** case: absent means False. If any arm pins `use_atr_stop=True` instead of searching it, the row carries no gene, and the export would declare False for a run that used True.
   - Fix: the exporter reads the run's policy from `optimization_config.backtest`. It raises when the policy says unpinned and the gene is missing. It never defaults to False on a policy run.
   - The handler side (`daily_backtest_handler._run_facts`, `:1093`/`:1117`) already reads the resolved setting and needs no change.
6. **Import.**
   - Today `tools/import_deploy_payload.py:_apply_rm_toggles` (`:106`) **overwrites** the payload's toggle with the CLI flag (`--use-atr`, `_PINNED_RM_TOGGLES` at `:100`).
   - For a payload whose `use_atr_stop` is True, a missing `--use-atr` must **refuse**, naming the row, rather than silently deploying False. Today's payloads all say False and are unaffected.
   - The loud "ENABLED BY --use-atr / backtest did NOT exercise it" banner must be reworded for policy rows, whose backtest did exercise it.
7. **Bool read on both paths.**
   - `TradeRiskManagement.py:1680` reads `bool(...)`: `bool("0")` is True, the exact historical defect.
   - Add a parity test that the value reaching it is a real bool for a GA int gene (backtest) and for a stored setting (live, via `coerce_bool`).
8. **Tests.**
   - `test_inert_rm_toggles_stay_off.py` stays green **unmodified**; it is the control.
   - New tests:
     - A policy run decodes gene 1 → True and gene 0 → False.
     - A policy run cannot unpin the overlay.
     - A persisted policy config re-runs to the same trades.
     - The exporter carries the gene and refuses the absent case.
     - The importer refuses a True payload without the flag.
     - Replay's dependency list requests ATR for policy rows (`dependencies.py:522`).
9. **Live readiness.**
   - The live classic RM and the Smart Risk Manager (`SmartRiskManagerToolkit.py:1927`) both read ATR through `get_latest_atr`. The live capture/prewarm must fetch 1d ATR for the deployed universe.
   - Add one BT/live parity test: same symbol and session, same ATR value and same safeguard stop.
   - The ATR tz fix (2026.07.989) is the precedent for this failing silently.

### 3.3 What the ATR gene actually controls (read the results against this)

- **The safeguard stop is the loosest stop a trade can have.**
  - The ruleset stop policy is ratchet-only by default (`TradeActions.ruleset_stop_policy`, BA2-pullback `:1448`).
  - With `allow_ruleset_sl_loosen` on, a rule can loosen back only as far as the trade's **max-loss stop**, the stop the position was sized on.
  - That sized stop *is* the safeguard, so turning ATR on moves every trade's worst-case exit.
- **Strategy floor stops looser than the safeguard never bind.** S2's `exit_stoploss` (−20..−3%) is a tighten-only request, so it binds only when it is tighter than the safeguard. This was true in goal2020 as well.
- **The GA can switch ATR off without the gene.** A `min_stop_loss_pct` above `atr_multiplier×ATR` makes ATR inert. At the median daily ATR of 2.29%, a 3.0× floor already gives 6.9%.
- **Report the ATR-bound share per winner.** That is the fraction of entries whose safeguard stop came from the ATR candidate. A winner with `use_atr_stop=1` and an ATR-bound share near 0 did not use ATR.
  - **This needs a small addition.** `synthesize_safeguard_stop` (`position_sizing.py:256`) returns only the price, so nothing records which candidate won: ATR, risk % or the `min_stop` floor.
  - Add that as a trace note next to the existing `binding` notes in `_risk_atr_quantity`, or on the order.

## 4. Strategy set

From `s1_s7_relevance.md`:

| Strategy | Experts | Why |
|---|---|---|
| **S1** | all | dominant in goal2020 (1st by fitness in 10 of 22 cells, never last) |
| **S6** | all | 2nd overall; cleanest concentration (0% of P&L from never-exited positions) |
| **S5** | all | 3rd; keeps S3's trailing ladder in the grid. Flagged for year dependence. |
| **S2** | all except FMPInsiderClusterBuy | contains S7's rules; last or 5th in 4 of 4 ICB cells |
| S3, S7 | — | dropped (S7 possibly kept for FMPRating mid only; decision D10) |

- **Experts and bands:**
  - FMPRating: large, mid and small.
  - FMPEarningsDrift: mid and small.
  - FMPInsiderClusterBuy: mid and small.
  - DeterministicScorer: large, mid and small.
  - FMPSenateTraderWeight: one universe.
  - That makes 11 expert-bands.
- **FactorRanker is out.** It bypasses the classic RM, never reads ATR, and its mid/small results were empty.
- **Job count:** 11 expert-bands × 4 strategies − 2 (ICB S2) = **42 treatment jobs**. See §6 for controls.

## 5. Market conditions

### 5.1 Entry gates (§6.0 placement)

- **Placement.** Append the `ohlcv-v1` and `ta-structure-v1` leaves to the **initial-entry AND tree** of each kept strategy, never to exit or open-position rules. Use ids `<s>-market-<short>` and the same `_market_condition_gates` helper the option builders use.
- **Genes.** 6 + 9 = 15 per entry tree.
- **The equity path is refused today.** `ba2test_launcher.py:5031` exits with "--market-condition-profile is options-only in this delivery". Implementation plan Task 11 (`docs/plans/2026-09-15-option-market-condition-genes-impl.md:561`) holds the wiring:
  - builder placement;
  - `run_screener_capband_matrix.py` flag and config digest;
  - a byte-identity test for profile `none`;
  - a grid script with a frozen all-off control and matched seeds.
- **S1 has three entry rules** (one per conviction tier, `_build_strategy_S1` ~2093). Task 11's "exactly 15 additional genes" only holds if the three tiers **share** one gate set. Per-tier gates would add 45. This draft assumes shared (decision D5).

### 5.2 Market exits, stops and TPs, plus `allow_ruleset_sl_loosen`

This comes from `feat/pullback-market-exits`: 24 commits ahead of dev, not merged.

- **The rule contract** (B2, `assert_market_rule_actions`):
  - market leaves are allowed in exit rules whose actions are `close`/`reduce`/`adjust_stop_loss`/`adjust_take_profit`;
  - lifecycle/roll actions are still refused;
  - live wraps `process_open_positions_recommendations` in `market_condition_decision_scope`;
  - unknown means "does not fire".
- **The templates** (B5, `market_exit_rules(prefix, profiles, direction)`): `mkt-exit` (close), `mkt-stop` (stop to breakeven, or tighten) and `mkt-tp` (widen or pull in the TP). Each rule is off by default behind a rule-level toggle and appended **after** the existing exits. Adjustments get `continue_processing=True`, and no leaf can resolve to `off`. That is about **9 genes**.
- **`allow_ruleset_sl_loosen`** (`MarketExpertInterface`, BA2-pullback ~407) is an expert setting, default False.
  - It is deliberately neither a forced setting nor an inert toggle, so a genome may set it and it travels with export.
  - This draft searches it as **1 gene in both arms**. It is not a market-condition gene, so the control must carry it too, or the comparison would measure two things at once.
- **Prerequisites.**
  - Merge B1–B7, including the exit parity test B7 and the TP churn guard.
  - Add an equity-driver flag equivalent to B6's `--market-exit exit,stop,tp`.
  - Open question 3 of the pullback design (a live kill-switch AppSetting) should be settled before any deploy from this grid.

### 5.3 Scale risk of the equity feature snapshot

- **The equity union is about 20× the option universe.** The goal2020 memory sizing notes about 105 large, about 580 mid and about 1,320 small screened symbols, so roughly 2,000, against 97.
- **Measured on the options build:**
  - 7,154 feature objects for 98 symbols;
  - 13 split refetches;
  - **one descriptor per mapped array**: 98 × 18 = 1,764 already exceeded the Linux default of 1,024 (runbook, remote227 traps).
- **Scaled to the equity universe, that is about 36,000 descriptors per process**, before multiplying by pool children.
- **Feasibility must be measured before committing to the design.** Options:
  - a `plan` run for each band;
  - a `prepare-host` timing;
  - an fd count;
  - or scope the market arms to large and mid first (decision D7).

### 5.4 Lifting the S1–S7 deferral: an explicit operator decision

The operator's 2026-09-15 instruction was: "do not change strategy s1 s7, only option with these now". Task 11 was deferred on that basis.

- **This grid is where that deferral would be lifted.** It must be the operator's explicit call, not an implementation side effect.
- **The builders change only behind the new flags.** With profile `none` and no `--market-exit`, every S1–S7 builder must emit a strategy byte-identical to today's. A test pins it, per Task 11.
- **goal2020 rows, labels and the 26 forward-test deploys stay untouched.**

## 6. Arms, controls, sizing mode

- **Treatment arm:** ATR searched, market entry + exit genes on, `allow_ruleset_sl_loosen` searched.
- **Matched control arm, per cell:** identical except that every market gene is frozen **off**. Same:
  - seed;
  - population and generations;
  - window, costs, fitness and robust setting;
  - ATR policy.
  - The all-off control must be byte-identical to "no profile". The entry profile already passes that gate, and B5 pins it for exits.
  - The control measures what the market conditions add, cell by cell.
- **Optional legacy-equivalent arm** (ATR pinned off, market off), for S1 and S6 only, as decision D9. It is the only way to see what the new window alone did, separately from ATR. Without it, "ATR helped" can only be read from the gene's selection plus the ATR-bound share.
- **Sizing mode: `risk_atr` only** (decision D2). It is the mode where ATR acts twice, as stop and as size.
  - goal2020 split roughly evenly between the modes: notional won S5 and S7 8–2 and 8–3, and S1/S6 slightly preferred risk_atr.
  - 11 of 66 pairs were byte-identical.
  - A notional matrix doubles the cost.

## 7. GA budget: the gene-scaled rule and its cost

**Rule** (A4 revision, operator-approved 2026-09-24, `docs/plans/2026-09-24-pullback-and-market-exits.md` in BA2-pullback):
- **Generations:** 25, or 30 when a job has more than 20 genes.
- **Early stop:** 8.
- **Population:** clamp(4 × genes, 24, 120).
- **Status:** A4 is specified for the exploration driver and not implemented yet. The equity driver needs the same resolution logic, with the gene count recorded in `optimization_config`.

### 7.1 Gene counts

The blocks are measured from goal2020 `parameter_ranges`.

| Block | Genes | Notes |
|---|---|---|
| strategy | S1 35, S2 20, S5 21, S6 11 | |
| expert `model:*` | FMPR 6, ED 6, ICB 4, DS 9, SEN 19 | |
| screener | 6 (Senate 0) | |
| RM | 7 | `risk_per_trade_pct`, `atr_risk_budget_pct`, `atr_multiplier`, `atr_period`, `min_stop_loss_pct`, **`use_atr_stop`** (new), `max_virtual_equity_per_instrument_percent`. The 3 `regime_*_scale` genes are dropped (D3). |
| `allow_ruleset_sl_loosen` | 1 | |
| schedule | 5 | weekdays only (D8). The 2 weekend genes are noise on a daily clock (`reference-ga-schedule-genes`). |
| market entry | 15 | shared across S1's tiers (D5) |
| market exit | 9 | |

Genes per job, market arm / all-off control:

| Expert | S1 | S2 | S5 | S6 |
|---|---|---|---|---|
| FMPRating | 84 / 60 | 69 / 45 | 70 / 46 | 60 / 36 |
| FMPEarningsDrift | 84 / 60 | 69 / 45 | 70 / 46 | 60 / 36 |
| FMPInsiderClusterBuy | 82 / 58 | — | 68 / 44 | 58 / 34 |
| DeterministicScorer | 87 / 63 | 72 / 48 | 73 / 49 | 63 / 39 |
| FMPSenateTraderWeight | 91 / 67 | 76 / 52 | 77 / 53 | 67 / 43 |

- **Every job has more than 30 genes**, so every job gets **population 120, 30 generations, early stop 8**.
- **The 120 cap binds everywhere.** 4 × genes would be 136–364, so population per gene falls to 1.3–3.5, against the rule's intended 4. The rule was written for exploration families of 1–40 genes. Whether to raise the cap for this grid is decision D11; cost scales linearly with it.
- With per-tier S1 gates, S1 would carry 112–121 genes.

### 7.2 Wall-clock estimate (arithmetic)

Reproducible with `cost_model.py`; the output is in `cost_model.out` and `cost_breakdown.out`.

1. **Anchor** (RUNBOOK §1): FMPRating/S1/large, 2022–2025, population 140, 31.8 min per generation on 4 local + 6 remote = 10 slots.
   - goal2020 recorded 0.71 × pop × gens trials, so about 29% of each generation is memo duplicates. That gives 0.71 × 140 = 99.4 evaluations per generation.
   - **3.20 slot-minutes per evaluation**, from 31.8 × 10 / 99.4.
2. **Relative cost per expert/band**: measured goal2020 wall-minutes per recorded trial (jobs with ≥ 150 trials), normalised to FMPRating = 1.
   - FMPR 1.0 / 1.1 / 1.0; ICB mid 1.0, small 2.3; ED mid 3.2, small 3.9; DS large 3.0, mid 4.9, small 5.3; **Senate 8.6**.
   - DS ran on matrix 3's smaller fleet, so its weights are an upper bound.
3. **Window factor:** 7/6 for 2020–2026 against 2020–2025; 5/4 for FMPRating.
   - **Overhead** on the market arm: ×1.06. The gates measured +0.4% per trial; +5% is assumed for exit-rule evaluation.
4. **Trials per job:**
   - with early stop around generation 20: 0.71 × 120 × 20 = **1,704**;
   - with no early stop: 0.71 × 120 × 30 = **2,556**.
5. **Fleet:** remote227 at 28 slots (the current option grid runs there at `PARALLEL=28`) plus 4 local.
   - **Large and mid:** 32 slots.
   - **Small band:** 17. The peak child is 7.4–13 GB, so about 15 fit remote plus 2 local.
   - **Senate:** 16, at about 12.3 GB per child, none local.
6. **Worked examples:**
   - FMPR large S1 market arm: 1,704 × 3.20 × 1.0 × 1.25 × 1.06 / 32 slots / 60 = **3.7 h**.
   - Senate S1 market arm: 1,704 × 3.20 × 8.56 × 1.167 × 1.06 / 16 / 60 = **60 h**.

Totals, running one job at a time as goal2020 did:

| Scenario | Jobs | Hours | Days |
|---|---|---|---|
| Keep list, treatment + all-off control, early stop about gen 20 | 84 | 1,473 | **≈ 61** |
| Same, all 30 generations | 84 | 2,209 | ≈ 92 |
| Keep list, control only for S1 and S6 | 64 | 1,124 | ≈ 47 |
| Keep list, no control | 42 | 758 | ≈ 32 |
| All six strategies, with control (for comparison) | 128 | 2,228 | ≈ 93 |

- **By expert, 20-generation case:**
  - DeterministicScorer 491 h;
  - Senate 467 h (8 jobs, **32% of the grid**);
  - FMPEarningsDrift 288 h;
  - FMPRating 116 h;
  - FMPInsiderClusterBuy 111 h.
- **Cross-check:** goal2020's 135 jobs summed 786 job-hours for 47,081 recorded trials. The new grid has about 3× the trials, a 1.17× window and about 2× the slots: 786 × 3 × 1.17 / 2 ≈ 1,380 h, which is consistent.
- **Levers:**
  - run Senate on its own lane in parallel, as goal2020 ran matrix 3 alongside, which roughly halves the wall-clock;
  - controls only for S1/S6;
  - the population cap;
  - market arms for large and mid first.

## 8. Comparison and selection

Every finished job reports, with fitness only as the ranking column (`feedback-report-economics-not-just-fitness`):
- total profit, CAR and max drawdown, via `tools/report_grid_results.py --like %atr27%`;
- trades, win rate and calmar.

Additional per-candidate requirements before anything is called a winner:
1. **Per-year returns for every calendar year, 2020…2026, with 2026 as its own column.** It is the newest year and the only one no previous grid saw.
   - Flag a candidate when one year carries more than 50% of the log growth. goal2020's ICB-mid cell and the S5 winners were 2020-shaped.
   - FMPRating rows are 2022–2026; never mix them into a cross-expert per-year table.
2. **Concentration** (`reference-concentration-check-before-deploy`): top-1 and top-5 trade share of net P&L, plus the P&L share from positions still `open_at_end`, from the persisted `trades` JSON.
   - This is a soft penalty, per the operator's calibration, and already part of `robust_fitness`.
   - Extraction over 647 rows took about 75 s this time (`extract_goal2020.py`), so this is cheap; run it for every TOP-N row.
3. **Capital in use**: mean %, peak and idle days, from the `capitalUsage.ts` logic. Rank stackable, capital-light candidates on it (`feedback-stackable-capital-light-strategies`).
4. **Matched control per cell.** Report treatment minus control on CAR, maximum drawdown, the worst year and concentration, at the same seed and budget.
   - A market arm that wins only on fitness, not on the worst year or on drawdown, is not evidence for the gates.
   - Report gate counters (eligible recommendations against rejections, unknown by reason) and exit counters (market exits fired, stops tightened, TP changes per trade for churn), using `tools/report_market_conditions.py`.
5. **ATR evidence:** the winner's `use_atr_stop`, `atr_multiplier`, `atr_period`, and the ATR-bound share of entries (§3.3).
6. **Parity gates on the deploy path.** Before any ATR-grid row is deployed:
   - the export round-trip shows `use_atr_stop` equal to the row's gene;
   - the import refuses a mismatch;
   - the schedule genes export with `weekdays_only=True`.
7. **Tag** `ForwardTest` on any deployed source row, as usual.
8. **Forward confirmation.** Re-score the shortlisted genomes on 2027-H1 before deploy (D1).

## 9. Open decisions for the operator

| # | Decision | Draft's recommendation |
|---|---|---|
| D1 | Holdout: fit on 2020–2026 (as asked), fit on 2020–2025 and score 2026 out of sample, or fit on all and confirm on 2027-H1 | fit on 2020–2026, then **2027-H1 forward confirmation** before deploy. The 60–90-gene spaces raise overfitting risk. |
| D2 | Sizing modes: `risk_atr` only, or both | `risk_atr` only (halves the cost) |
| D3 | Regime overlay: stays pinned off, and its 3 scale genes are dropped from the space | yes; the overlay is a separate experiment |
| D4 | ATR ranges | `atr_multiplier` 1.5–6.0 step 0.5 (10 values), `atr_period` 7–28 step 7. **The 3.0 floor was raised on 2026-07-01 from whipsaw evidence gathered while ATR was dead** (tz bug fixed later, in 2026.07.989), so it has no basis. |
| D5 | S1 market gates: shared across the three tiers (15 genes) or per tier (45) | shared |
| D6 | `allow_ruleset_sl_loosen`: searched in both arms, pinned off, or pinned on | searched, in both arms |
| D7 | Market arms on all bands, or large/mid first (equity snapshot scale, §5.3) | measure the `plan` first; if the fd, disk or warm-time budget fails, large/mid first |
| D8 | Schedule genes: drop weekends (5 instead of 7) | drop |
| D9 | Legacy-equivalent arm (ATR off, market off) for S1/S6 | yes if the budget allows (+22 jobs, about 15 days); otherwise skip |
| D10 | Strategy list: S1/S2/S5/S6 (S2 not for ICB); keep S7 for FMPRating mid? | as listed; S7 for FMPR mid is optional (+2 jobs) |
| D11 | Population cap: keep 120 (the A4 rule) or raise for these 34–91-gene jobs | keep 120, and run a 1-cell pilot at 240 to see whether the cap costs fitness |
| D12 | **Lift the Task 11 deferral**, so S1–S7 builders gain market gates and exits behind flags | the operator's call (§5.4) |
| D13 | Merge `feat/pullback-market-exits` (B1–B7) and implement Task 11 + A4 + the §3.2 policy before 2027 | required for the design as drafted |
| D14 | Senate in the grid, at 32% of the cost, or on its own lane or later | its own parallel lane |
| D15 | Controls for every cell, or only S1/S6 | every cell if the lane plan fits; otherwise S1/S6 |
