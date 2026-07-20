# Test-account spread/cache audit — 2026-07-16

Working memo for deciding what to update. Covers: current dev-account deployments, the
per-deployment spread-robustness resim, the mid-band FMPEarningsDrift warm-start-with-spread
results, and the cache/data-quality findings that came up along the way.

## Process rule: tag every deployed backtest's source with `ForwardTest`

**Whenever a `Backtest` row (test-platform DB) is deployed to a live `ExpertInstance`
(either account, dev or prod), add `"ForwardTest"` to that backtest's `labels` field.**
This marks it as "the specific run whose settings are live" so it's filterable in
`ba2-test runs`/the History UI, distinct from every other trial the GA produced for the
same job. Added retroactively 2026-07-19 to all 15 backtests currently backing a live
instance (§1); apply going forward on every new deploy (e.g. right after the
`add_instance(ExpertInstance(...))` step in a deploy script).

## 1. Currently deployed instances (dev trade platform: `ba2\trade\db.sqlite`)

3 accounts (`BA2-Test1`/`2`/`3`, all Alpaca). 25 `ExpertInstance` rows total as of 2026-07-20
(24 goal6-grid instances at target + 1 out-of-scope PennyMomentumTrader); none disabled (see
§2/§3). Settings below are the fields most relevant to spread/TP-SL behavior — full settings
dumps are in the DB if needed.

### BA2-Test1 (account_id=1)

| id | expert | alias | enabled | band | cap_min | max_stocks | drop_pct | rel_vol_min | ATR mult/period | use_atr_stop | min_SL% | risk/trade% | TP ref |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | FMPRating | goal6-large_S1 | 1 | large | $140B | 30 | 10.0 | 1.1 | 5.5/21 | 1 | 7.0 | 5.0 | low_consensus_avg |
| 6 | FMPRating | goal6-large_S4 | 1 | large | $70B | 50 | 11.0 | 1.2 | 4.0/21 | 0 | 14.0 | 8.0 | median |
| 7 | FMPRating | goal6-large_S6 | 1 | large | $90B | 40 | 0.0 | 1.0 | 5.0/14 | 0 | 11.0 | 8.5 | high |
| 9 | FMPRating | goal6-mid_S3 | 1 | mid | $4B | 10 | 25.0 | 2.5 | 4.5/28 | 1 | 11.0 | 10.0 | median |
| 10 | FMPRating | goal6-mid_S4 | 1 | mid | $2B | 10 | 15.0 | 2.1 | 4.5/21 | 1 | 8.0 | 3.0 | low_consensus_avg |
| 11 | FMPRating | goal6-mid_S1top4 | 1 | mid | $5B | 40 | 11.0 | 2.1 | 4.0/21 | 0 | 8.0 | 6.0 | low_consensus_avg |
| 12 | FMPRating | goal6-small_S7 | 1 (reseeded 2026-07-19) | small | see note | — | — | — | — | — | — | — | — |
| 13 | FMPRating | goal6-small_S5 | 1 | small | $200M | 20 | 23.0 | 1.2 | 4.5/21 | 0 | 4.0 | 10.0 | high |
| 14 | FactorRanker | goal6-large_FRtop1 | 1 | large | $50B | 10 | 6.0 | 1.9 | — | — | — | — | (expert-driven) |
| 15 | FactorRanker | goal6-large_FRtop4 | 1 | large | $50B | 20 | 6.0 | 1.9 | — | — | — | — | (expert-driven) |
| 26 | FactorRanker | goal6-mid_FRtop1 | 1 | mid | (screener, mid) | — | — | — | — | — | — | — | (expert-driven) |
| 27 | FactorRanker | goal6-mid_FRtop4 | 1 | mid | (screener, mid) | — | — | — | — | — | — | — | (expert-driven) |
| 30 | FactorRanker | goal6-small_FRtop2 | 1 | small | (screener, small) | — | — | — | — | — | — | — | (expert-driven) |
| 31 | FactorRanker | goal6-small_FRtop3 | 1 | small | (screener, small) | — | — | — | — | — | — | — | (expert-driven) |

Notes (2026-07-19): ids 26/27 deployed from the `-goal6c` re-run (fixed genes: `hard_stop_pct`
removed, `risk_per_trade_pct` GA-tunable, screener volume floor 1.0→0.0), TOP1 (bt726,
20.9%/-8.96%DD/70.0%WR) + TOP4 (bt724, 50.7%/-9.42%DD/76.5%WR) — same rank pair as the existing
large-band deployment. TOP1 wins the `consistent_annual_return` fitness despite lower raw
return because it's the only mid individual with all-3-years positive (no `consistency`-floor
penalty); TOP4 is the best raw-return individual either mid run has found, included for
diversification. Both `ForwardTest`-tagged.

Notes (2026-07-20): ids 30/31 deployed from opt199 (`-goal6c` small-band re-run, same fixed
genes as the mid re-run above). opt190 (pre-fix genes) was uniformly weak/negative (-0.17% to
-0.66% return, TOP1-4) — opt199's top-3 are all positive: TOP2 (bt728, 24.71%/70.0%WR),
TOP3 (bt729, 20.35%/61.11%WR), TOP1 (bt731, 13.52%/64.71%WR, best `consistent_annual_return`
fitness but lower return/WR). User-directed pick 2026-07-20: TOP2+TOP3 over TOP1, for
diversification and to lead on return/WR (same rationale as the existing large/mid pairs) —
trade frequency is low (10-19 trades over 3.5yr, ~3-5/yr) so the fitness score itself is modest
(`best_fitness`=0.76, heavily penalized by the `trade_gate` term vs the 30/yr target) despite
solidly positive returns. Both `ForwardTest`-tagged. Closes the FactorRanker gap: 4 → 6.

### BA2-Test2 (account_id=2)

| id | expert | alias | enabled | cap_min | max_stocks | drop_pct | rel_vol_min | ATR mult/period | use_atr_stop | min_SL% | risk/trade% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 16 | FMPEarningsDrift | goal6-mid_ED_S1top1 | 1 | $6B | 30 | 1.0 | 1.9 | 3.5/21 | 0 | 12.0 | 5.0 |
| 20 | FMPEarningsDrift | goal6-mid_ED_S2top1 | 1 | $2B | 50 | 3.0 | 2.7 | 3.5/28 | 0 | 8.0 | 1.0 |
| 19 | FMPInsiderClusterBuy | goal6-mid_ICB_S2top3 | 1 | $5B | 30 | 2.0 | 1.6 | 4.0/21 | 0 | 9.0 | 9.0 |
| 21 | FMPInsiderClusterBuy | goal6-small_ICB_S1top3 | 1 | $200M | 20 | — | — | — | — | — | — |
| 22 | FMPEarningsDrift | goal6-small_ED_S1top1 | 1 | $100M | 40 | 7.0 | 2.6 | 6.0/28 | 1 | 14.0 | 1.5 |
| 23 | FMPEarningsDrift | goal6-small_ED_S7top2 | 1 | $1.1B | 20 | 4.0 | 1.5 | 6.0/28 | 0 | 12.0 | 3.0 |
| 24 | FMPInsiderClusterBuy | goal6-small_ICB_S7top1 | 1 | (screener, small) | — | — | — | — | — | — | — |
| 25 | FMPInsiderClusterBuy | goal6-mid_ICB_S7top2 | 1 | $6B | 40 | 0.0 | 1.2 | 6.0/7 | 1 | 12.0 | 5.0 |

Notes: id 17 (`goal6-mid_ED_S2top1`, disabled for spread fragility, see §3) was replaced by id
20, reseeded from `TOP1-scr-mid-FMPEarningsDrift-S2-goal6-spread` (backtest 660, the warm-start-
with-spread job flagged as in-progress in §8) — held up better under spread resim. ids 21/22/23
are net-new small-band deployments (2026-07-19), not reseeds — 21 closes the InsiderClusterBuy
small-band gap, 22/23 close the EarningsDrift small-band gap (§2). id24 (bt717, S7-TOP1,
70.49%/-11.64%DD/48.15%WR) closes the remaining InsiderClusterBuy small-band slot. id18
(`goal6-mid_ICB_S7top1`, bt551, TOP1) **replaced by id25** (bt549, TOP2) — TOP1 had been ranked
#1 by the `consistent_annual_return` fitness metric purely on `trade_gate` (avg_trades_per_year
25.4 vs TOP2's 17.0, closer to the 30/yr target), but TOP2 beats it on total return (97.63% vs
68.87%), every single calendar year (2023/24/25), and has a *better* YoY consistency ratio
(worst/mean 0.876 vs 0.860) at nearly identical drawdown — user-directed swap 2026-07-19.
`ForwardTest` label moved bt551 -> bt549 accordingly. id18 had zero open transactions (never
traded before being superseded), so it was **deleted outright** (not just disabled) once id25
was confirmed live — user-directed cleanup 2026-07-19.

### BA2-Test3 (account_id=3)

| id | expert | alias | enabled | notes |
|---|---|---|---|---|
| 4 | PennyMomentumTrader | TestPenny | 1 | Not part of the GA/screener pipeline — no matching `StrategyOptimization`/`Backtest` row exists. sizing_mode=risk_atr, risk_manager_mode=classic, min_SL%=7.0, risk/trade%=2.0. Out of scope for this audit. |
| 28 | FMPSenateTraderWeight | goal6-senate_S3top3 | 1 | bt739 (`TOP3-sen-S3-goal6`, opt201): 92.12% return / 66.17% WR / -14.32% DD / 133 trades. `instrument_selection_method=expert` (basket dispatch — Senate scans its own candidate list every cycle via `_gather_all`, not the backtest's static 498-symbol scanning universe). risk/trade%=5.0. `ForwardTest`-tagged. |
| 29 | FMPSenateTraderWeight | goal6-senate_S5top2 | 1 | bt743 (`TOP2-sen-S5-goal6`, opt202): 179.13% return / 35.29% WR / -14.47% DD / 119 trades. Same basket-dispatch wiring as id28. risk/trade%=5.0. `ForwardTest`-tagged. |

All non-PennyMomentumTrader instances trace back to a specific `Backtest` row via the
`TOP<rank>-scr-<band>-<expert>-<strategy>-goal6` naming convention (mapping table in §3), except
the FMPSenateTraderWeight pair (ids 28/29, `TOP<rank>-sen-<strategy>-goal6`, not band-split —
Senate's universe spans all bands, deployed here since BA2-Test3 had the most spare capacity).

## 2. Final deployment goal & gap analysis

Target state: **8 FMPRating, 6 FactorRanker, 4 FMPEarningsDrift, 4 FMPInsiderClusterBuy,
2 FMPSenateTraderWeight** (24 instances total, excluding `PennyMomentumTrader` which is
out of scope for this GA/screener goal). General rule: **2 deployments per cap-band per
expert**, with the number of bands an expert covers set by where it actually has edge/data
(per existing project knowledge: `FMPEarningsDrift` is small/mid-cap only; `FMPInsiderClusterBuy`
covers **mid + small** — no reliable large-cap insider data — so neither should run on
large-cap).

| expert | target | current | gap | band split (2/band) |
|---|---|---|---|---|
| FMPRating | 8 | 8 | **0 — at target count** | large/mid/small currently 3/3/2, not an even 2/2/2/2 — see note below. The FRAGILE-flagged small instance (id12) is now reseeded in place (§3) — zero disabled/fragile FMPRating instances remain. |
| FactorRanker | 6 | 6 | **0 — closed 2026-07-20** | large (2, ids 14/15) + mid (2, ids 26/27, deployed 2026-07-19) + small (2, ids 30/31, deployed 2026-07-20) |
| FMPEarningsDrift | 4 | 4 | **0 — closed 2026-07-19** | mid (S1top1 id16, reseeded S2top1 id20) + small (S1top1 id22, S7top2 id23) — 2/2 |
| FMPInsiderClusterBuy | 4 | 4 | **0 — closed 2026-07-19** | mid (S2top3 id19, S7top2 id25) + small (S1top3 id21, S7top1 id24) — 2/2 |
| FMPSenateTraderWeight | 2 | 2 | **0 — closed 2026-07-20** | not band-split (Senate's universe spans all bands — single blended assumption per `tools/run_senate_matrix.py`); deployed S3top3 (id28, bt739) + S5top2 (id29, bt743) to BA2-Test3, best 2 by `consistent_annual_return` fitness across the full S2/S3/S5/S6 matrix |
| **Total** | **24** | **24** | **at target** | |

**2026-07-19 update — EarningsDrift small-band gap closed**: the small-band grid (opt166=S1,
opt167=S2, plus S7 from an earlier phase, opt195) finished. S2's full top-5 (opt167:
54.93%/60.11%/24.11%/50.73%/79.32% return) was checked against S1 and S7 per the process rule
below and confirmed **not competitive** — every S2 individual landed below both S1's TOP1
(111.44%, bt709) and S7's best (TOP2, 89.77%, bt704). Deployed bt709 (S1 TOP1) as id22 and
bt704 (S7 TOP2, chosen over S7's own TOP1 at 79.9%) as id23, both tagged `ForwardTest` on their
source `Backtest.labels`.

Notes:
- **FMPRating is at the target count (8) but not evenly split 2-per-band** — currently
  large:3 (S1, S4, S6), mid:3 (S3, S4, S1top4), small:2 (S7, S5). If "2 per band" is meant
  literally (and FMPRating spans large/mid/small = 3 bands → 6, not 8), either the target
  implies a 4th band split for FMPRating specifically (e.g. mega-cap $140B+ vs large $70-90B,
  which the current `goal6-large_S1`/`S4`/`S6` cap-min values of $140B/$70B/$90B hint at), or
  the count-8-vs-even-split rule isn't meant to reconcile exactly and a rebalance to 2/2/2/2
  (dropping to 6) is intended. **Needs a decision**, not assumed either way here.
- **FactorRanker gap (+4)**: need mid-band and small-band FactorRanker optimization jobs run
  and their top-2 deployed — none exist yet in either band (grid only ran large-band
  FactorRanker: opt 117, backtests 480/481).
- **FMPEarningsDrift gap (+2)**: need small-band deployed. Small-band EarningsDrift work
  exists (the "old 204%" backtest 562, `TOP1-scr-small-FMPEarningsDrift-S1-goal6`, opt 131 —
  investigated earlier this session and found to not reliably reproduce even at spread=0 due
  to a screener-universe reconstruction discrepancy predating the `7bf6dc4` fix window — do
  not deploy 562 as-is). The currently-running grid Phase 1b (`scr-small-FMPEarningsDrift-*`,
  job 148 = S1 retry) is building fresh small-band candidates now.
- **FMPInsiderClusterBuy gap (+2)**: covers mid + small (not large, no reliable large-cap
  insider data). Mid is already deployed (ICB_S7top1, ICB_S2top3, ids 18/19). Need small-band
  optimization run and top-2 deployed — grid Phase 2 (`scr-small-FMPInsiderClusterBuy-*`,
  spread=40bps) covers this but hasn't run yet (queued after Phase 1b).
- **FMPSenateTraderWeight gap — closed 2026-07-20**: Phase 3 (`tools/run_senate_matrix.py`,
  strategies S2/S3/S5/S6, spread=20bps, opt200/201/202/206) finished. Top candidates per
  strategy: S2 TOP2 (bt732, 74.4%/17.29%ann), S3 TOP3 (bt739, 92.12%/20.59%ann/66.17%WR), S5
  TOP2 (bt743, 179.13%/34.22%ann), S6 TOP2 (bt747, 47.83%/11.86%ann) — S3 TOP3 and S5 TOP2 won
  on `consistent_annual_return` (approximate recompute: annualized_return × win_rate_factor,
  with `dd_guard`/`trade_gate` both ≈1.0 for every leading candidate — all under the 20% DD
  guard threshold and well above the 30 trades/yr gate). Deployed both to BA2-Test3 (ids
  28/29, the only account with spare capacity) as basket-dispatch instances
  (`instrument_selection_method=expert`), `ForwardTest`-tagged on bt739/bt743.

## 3. Currently disabled (pending reseed) — UPDATE: both now reseeded, none disabled

- **id 12 — `goal6-small_S7`** (FMPRating, small-cap, BA2-Test1). Resim: -20.9pp return delta
  from spread=0 to spread=80bps → flagged **FRAGILE**. **Reseeded IN PLACE 2026-07-19** from
  bt653 (`TOP4-scr-small-FMPRating-S7-goal6-spread`, opt182's warm-start-with-spread batch):
  117.26% return / -14.35% DD / 45.16% WR — the best of that batch on return, drawdown, AND
  YoY consistency simultaneously (years 48.0%/30.9%/12.7%, worst/mean=0.415 vs TOP1's 0.197).
  Updated the SAME `ExpertInstance` row's `enter_market_ruleset_id`/`open_positions_ruleset_id`
  + settings in place (did NOT create a new instance or delete id12) because it had 3 live
  OPEN transactions (IONS/ALNY/ORCL, opened 2026-07-14) — swapping the ruleset in place lets
  the normal TradeActionEvaluator/RM machinery adjust TP/SL and close those positions under
  the new rules, rather than orphaning them under a dead/deleted instance. Re-enabled.
- **id 17 — `goal6-mid_ED_S2top1`** (FMPEarningsDrift, mid-cap, BA2-Test2). Resim: -100.8pp
  delta from spread=0 to spread=50bps (132.28% → 31.46%, a 4x collapse between 15bps and
  30bps) → flagged **FRAGILE**. **Replaced 2026-07-18/19** by a new instance, id 20, reseeded
  from `TOP1-scr-mid-FMPEarningsDrift-S2-goal6-spread` (bt660) — held up better under spread
  resim. id17 itself had zero open transactions and was deleted outright rather than left
  disabled (see §1 note).

Both disabled via `UPDATE expertinstance SET enabled=0` + `POST /api/reload` originally
(confirmed live, no restart needed); both are now reseeded and live again as of 2026-07-19.

## 4. Spread-robustness resim — full results (all 14 GA-derived deployments)

Methodology: reconstruct each deployment's source `Backtest` via
`rebuild_config_for_backtest()` (same mechanism as the `/rerun` endpoint — applies the actual
per-day screener-hoisted universe the GA scored the individual with, not a static snapshot),
then re-run at increasing `spread_bps` (real fill-engine modeling: MARKET/STOP fills get
extra half-spread price degradation, LIMIT/TP fills get their trigger threshold widened by
half-spread). Per-band spread levels: large=[0,3,10,20], mid=[0,15,30,50], small=[0,40,60,80].

| deployment | id | band | source bt | stored | resim@0bps | resim@max | Δ (spread only) | flag |
|---|---|---|---|---|---|---|---|---|
| goal6-large_S1 | 5 | large | 369 | 159.52% | 90.73% | 84.50% (20bps) | -6.2pp | ok |
| goal6-large_S4 | 6 | large | 386 | 103.47% | 100.75% | 101.75% (20bps) | +1.0pp | ok (=prod live config) |
| goal6-large_S6 | 7 | large | 397 | 89.31% | 91.94% | 90.79% (20bps) | -1.2pp | ok |
| goal6-mid_S3 | 9 | mid | 414 | 73.23% | 70.20% | 68.40–69.18% (50bps) | -1.0 to -1.8pp | ok |
| goal6-mid_S4 | 10 | mid | 421 | 88.28% | 88.28% | 117.77–119.22% (30–50bps) | **+31pp (anomaly)** | investigate — return *increases* with spread, likely a trade-mix shift, not a real "spread helps" effect |
| goal6-mid_S1top4 | 11 | mid | 407 | 69.78% | 29.90% | 24.02% (50bps) | -5.9pp | watch — large stored-vs-resim gap is data-drift (see §4), spread sensitivity itself is mild |
| goal6-small_S7 | 12 | small | 473 | 154.80% | 109.34% | 88.47% (80bps) | **-20.9pp** | **FRAGILE — disabled** |
| goal6-small_S5 | 13 | small | 460 | — | 58.00% | 38.25% (80bps) | -19.8pp | watch |
| goal6-large_FRtop1 | 14 | large | 480 | 49.55% | 49.55% (exact match) | 41.27% (20bps) | -8.3pp | watch |
| goal6-large_FRtop4 | 15 | large | 481 | 44.43% | 44.43% (exact match) | 35.85% (20bps) | -8.6pp | watch |
| goal6-mid_ED_S1top1 | 16 | mid | 494 | 65.37% | 22.38% | 16.17% (50bps) | -6.2pp | watch — large stored-vs-resim gap is data-drift, spread sensitivity mild |
| goal6-mid_ED_S2top1 | 17 | mid | 502 | 58.02% | 132.28% | 31.46% (50bps) | **-100.8pp** | **FRAGILE — disabled** |
| goal6-mid_ICB_S7top1 | 18 | mid | 551 | 68.87% | 38.00% | 37.42% (50bps) | -0.6pp | ok (data-drift on stored, but genuinely spread-insensitive) |
| goal6-mid_ICB_S2top3 | 19 | mid | 531 | 54.11% | 53.92% | 45.07% (50bps) | -8.9pp | watch |

**Read this table as two separate signals, don't conflate them:**
- **`stored` vs `resim@0bps`** = data/code drift since the backtest was originally persisted
  (see §4) — NOT a spread effect.
- **`resim@0bps` vs `resim@max`** = the actual spread-cost signal — this is what "FRAGILE"/
  "watch"/"ok" is based on.

## 5. Why `stored` and `resim@0bps` disagree for some rows

Two distinct, unrelated root causes identified this session — both fixed in code, but only
forward-looking (old `Backtest` rows keep their historical numbers as originally computed):

1. **Universe-reconstruction correction** (commit `7bf6dc4`, 2026-06-26): the CLI's original
   top-N persist path (`_persist_top_backtests`) built the config *without* the "hoisted"
   per-day screener state, so persisted `TOP*` backtests silently ran as a **static-universe**
   snapshot instead of the actual optimized day-by-day screener run the GA scored. Fixed for
   both the persist path and the `/rerun` path. All 14 deployments here postdate this fix
   (created 07-11 through 07-15), so it's **not** the explanation for any of the drift above.

2. **Empty entry_rules silently fell back to a default ruleset** (commit `da0b281`,
   2026-07-13 15:00:52): `decode_params()` couldn't distinguish "genome pruned every entry
   branch to nothing" from "no unified-model template configured at all" — both produced `[]`,
   and `[]` is falsy, so a genuinely "never enter" genome silently ran the generic
   bullish+flat default ruleset instead. Confirmed in production data on this exact family
   (`scr-mid-FMPRating-S1-goal6`'s TOP1, backtest 406 — not one of our deployments, but same
   bug). **Any backtest created before 2026-07-13 15:00 predates this fix**: `bt369` (large_S1,
   created 07-11), `bt386`, `bt397`, `bt407`, `bt414`, `bt421`, `bt460`, `bt473` all predate it.
   `bt480`/`481`/`494`/`502`/`531`/`551` postdate it (created after 07-13 15:00) — and indeed
   `bt480` is the one deployment whose resim@0bps matched `stored` **exactly**, confirming the
   theory.

**Practical rule going forward: don't trust the `stored` column for anything persisted before
2026-07-13 15:00 as a picture of current behavior — trust `resim@0bps` instead.**

## 6. Mid-band FMPEarningsDrift warm-start-with-spread pass

Took each of the 6 non-spread `-goal6b` optimization jobs (S1/S2/S3/S5/S6/S7, ids 133–140,
8 generations, spread=0) and warm-started a new 3-generation job from each one's final
population, this time with `spread_bps=15`. (Separate from and not directly mapped to the
live-deployed `-goal6` family in §1/§3 — this was a research batch on the newer `-goal6b`
naming, sharing strategy+expert+band but not backtest lineage with ids 16/17.)

| strategy | no-spread (orig) | spread=15 (warm) | verdict |
|---|---|---|---|
| S1 | 63.86% / 145 trades | **-6.55% / 513 trades** (only TOP5 persisted, no TOP1-4) | worse — degraded into a losing but still-active strategy |
| S2 | 53.91% / 86 trades | 35.53% / 81 trades | worse but reasonable — real spread cost, not degenerate |
| S3 | 77.64% / 118 trades | 75.20% / 117 trades | ~flat — held up well |
| S5 | 45.71% / 287 trades | **63.01% / 246 trades** | better — genuine improvement |
| S6 | 34.68% / 167 trades | **4.21% / only 2 trades** | collapsed — degenerate, see below |
| S7 | 110.41% / 91 trades | **-0.21% / only 2 trades** | collapsed — degenerate, see below |

**S6/S7 collapse investigated in detail — not a pipeline bug.** All 5 persisted top individuals
for BOTH jobs show only 1-2 total trades (not just the reported "best" one), so this is a
population-wide collapse, not one unlucky individual. Genome diff vs the pre-spread original:
S6 lost half its active trading-schedule days (4→2), `risk_per_trade_pct` jumped 3.5%→8.0%
(bet bigger, far less often), `expected_profit_mode` switched dynamic→static (narrower TP,
more exposed to the spread-widened trigger threshold). S7's genome barely changed but lost one
schedule day and a gating condition. Best-guess explanation: warm-starting from an
already-converged (low-diversity) 8-generation population, then hitting a suddenly-harsher
fitness landscape (spread=15), with only 3 generations to adapt — not enough runway to find a
genuinely different robust strategy shape, so the population retreated toward "trade almost
never" as the least-bad locally-available option. **Do not use S6/S7 warm results as reseed
candidates.** Would need either more generations or a fresh (non-warm-started) run under spread.

**Neither of these 6 results is a direct reseed candidate for the 2 disabled instances** (§2) —
`goal6-small_S7` (id 12) traces to opt 116 (FMPRating, not EarningsDrift), and
`goal6-mid_ED_S2top1` (id 17) traces to opt 119, not opt 134/137/140. Dedicated warm-start
runs from opt 116 and opt 119 specifically are still needed before reseeding either.

## 7. Cache / data-quality findings (background, informs confidence in all of the above)

- **Confirmed real bug, shared infrastructure**: `e48cc76` (2026-07-15) — `ba2-test prewarm`'s
  TTL-freeze flag never propagated into `ThreadPoolExecutor` worker threads (thread-local
  doesn't cross pool boundaries), so every prewarm fetch took the "live: never persist"
  branch — burned FMP rate-limit budget on every expert's prewarm (FMPRating, EarningsDrift,
  InsiderClusterBuy, FinnHubRating, SenateTraderWeight — not just senate, per the fix's own
  commit message), writing zero new cache files. Fixed via
  `ThreadPoolExecutor(initializer=set_ttl_frozen, initargs=(True,))`.
- **Confirmed real bug, found while auditing the above**: `1973705` (2026-07-15, 1hr after
  the fix above) — two backtest code paths (`OPEN_POSITIONS` position management, and the
  `FactorRanker`-style bypass-expert path) silently swallowed hermetic cache-miss errors
  instead of aborting loudly, degrading results invisibly on a missing pre-warm instead of
  failing the run. Fixed to match the already-correct `ENTER_MARKET` handling. All 14
  deployments here predate this fix (created before 07-15 16:30), so a silent degradation on
  either code path can't be ruled out for any of them without re-checking original run logs
  (not retained — GA trials run in-memory).
- **OHLCV cache staleness, quantified**: 1d cache's panel-typical last bar was stuck at
  `2026-06-18` (vs the required 2026-06-30) for the bulk of the ~11k-symbol universe — matches
  the prewarm bug above. 5min cache (what the grid actually trades on) was in much better
  shape (panel-typical = 2026-06-30 exactly, 78% full June coverage vs 1d's 5%).
- **62 symbols with near-total-void gaps fixed**: found via `>180d` internal-gap scan scoped
  to the actual screener universe (not the raw ~11k/~7k file counts, which include irrelevant
  ETFs/funds) — 52 in the first pass, 10 more (`BREZ,HCIC,HCICU,HLMVX,NUCL,ONTX,PTN,SLVR,
  SSEYX,TMS`) in a second pass. Refetched 1d+5min, 2022-01-01→2026-07-15, all succeeded.
- **`metric_store` fully rebuilt**: was previously `ym=2022-06`→`ym=2026-06` (partial 2022);
  rebuilt to `ym=2022-01`→`ym=2026-06` (54 months, 4,738 symbols, `--market-cap-min 5e7`
  matching the small-band floor — the documented "loosest bound" the store needs to support
  all 3 cap bands). Old store backed up at
  `C:\Users\basti\Documents\ba2\common\cache\screener\metric_store.bak-20260716` (not deleted).
  Confirmed via code trace that `build-screener-metrics` reads OHLCV exclusively from 1d bars
  (`ohlcv_get` defaults to `interval="1d"`, hardcoded at the call site) — 5min bars never touch
  `price_drop_pct`/`relative_volume`/`weinstein_stage`/`momentum`/`atr_*`.
- **Full-universe 2022 coverage gap, in progress**: rebuilding the store surfaced that even
  outside the 62 "void" symbols, ~73% of the universe reaches back to 2022 at 1d (81% at 5min)
  — i.e. ~19-27% didn't. Scoped to the *current* 4,718-symbol screener universe specifically:
  1,331 symbols missing 2022 coverage (1,030 at 1d, 1,315 at 5min). Fetch launched
  (2022-01-01→2026-07-15, both intervals) — **status as of this memo: in progress**, hitting
  normal FMP rate-limit backoff.
- **`sync-cache --worker remote150` run once already** (after the 62-symbol fix + first
  metric_store rebuild): 1,473 files pushed (~545MB), 49 stale files pruned. **Needs re-running
  after the 1,331-symbol 2022 batch finishes** to push that additional data to the remote
  worker too.
- **Grid sequence** (`run_grid_sequence_v2.sh`, resumable) was killed once for severe RAM
  pressure (98-99.6% used, small-band job with population=140 thrashing), then restarted after
  the metric_store rebuild — it resumes cleanly (skips completed jobs, retries the one marked
  `failed`).

## 8. Open items / what needs deciding

**Toward the deployment goal (§2):**
- [ ] Decide FMPRating's band split: keep 3/3/2 (large/mid/small) at 8 total, or rebalance to
      an even 2/2/2/2 across 4 bands (would need defining a mega-cap tier — hinted at by the
      existing $140B vs $70B vs $90B cap-min spread across the 3 current "large" instances).
- [x] **Done 2026-07-19/20** — mid-band (ids 26/27, bt726/bt724) and small-band (ids 30/31,
      bt728/bt729) FactorRanker deployed. FactorRanker gap closed: 2 → 6.
- [x] **Done 2026-07-19** — deployed top small-band FMPEarningsDrift picks: S1 TOP1 (bt709,
      id22) and S7 TOP2 (bt704, id23). Did NOT reuse backtest 562 (doesn't reliably reproduce,
      see §5). S2 (opt167) top-5 fully checked and confirmed not competitive with S1/S7.
      EarningsDrift gap closed: 2 → 4.
- [x] **Done 2026-07-19** — small-band FMPInsiderClusterBuy: deployed both needed slots
      (`goal6-small_ICB_S1top3`, bt626, id21; `goal6-small_ICB_S7top1`, bt717, id24).
      InsiderClusterBuy gap closed: 2 → 4. Also swapped the mid `S7` deployment (id18 → id25,
      TOP1 → TOP2) after a user-requested fitness review — see §1 notes.
- [x] **Done 2026-07-20** — Phase 3 of the grid (`tools/run_senate_matrix.py`, S2/S3/S5/S6,
      opt200/201/202/206) finished; deployed top-2 by fitness (S3 TOP3 bt739 id28, S5 TOP2
      bt743 id29) to BA2-Test3. FMPSenateTraderWeight gap closed: 0 → 2.

**Data/infra follow-through:**
- [ ] 2022-coverage fetch (1,331 symbols) — wait for completion, then **rebuild `metric_store`
      again** (the 07-16 rebuild predates this fetch, so it doesn't include this data), then
      `sync-cache --worker remote150` again.
- [ ] Investigate the `goal6-mid_S4` (id 10) return-increases-with-spread anomaly (+31pp) —
      likely a trade-mix artifact, not validated yet.
- [x] **Started 2026-07-18 13:47** — dedicated warm-start-with-spread jobs from opt 116
      (small_S7, FMPRating) and opt 119 (mid_ED_S2top1, FMPEarningsDrift), sequential (opt116
      first), 3 generations each, `--warm-start-from` the parent job's final population.
      Spread level picked as each band's low non-zero slot (small=40bps, mid=15bps — mirrors
      the §6 mid-band EarningsDrift precedent, not the max stress-test level from §4's resim
      table). Launch script: `run_warmstart_disabled_instances.sh` (scratchpad); log:
      `warmstart_disabled_instances.log`. New jobs will be named
      `scr-small-FMPRating-S7-goal6-spread` and `scr-mid-FMPEarningsDrift-S2-goal6-spread`.
      Once complete, evaluate top individuals the same way §6 did (watch for the S6/S7-style
      population collapse into a near-zero-trade "safe" strategy) before reseeding ids 12/17.
- [ ] "watch"-tier deployments (large_FRtop1/4, mid_S1top4, small_S5, mid_ED_S1top1,
      mid_ICB_S2top3) — no action taken yet, worth a second look once the fetch/rebuild
      settles.
- [ ] `goal6-mid_ICB_S7top1` (id 18) and `goal6-large_S4`/`S6` (ids 6/7) are the cleanest
      results (flat/positive under spread, no major data-drift) — reasonable to leave as-is.
