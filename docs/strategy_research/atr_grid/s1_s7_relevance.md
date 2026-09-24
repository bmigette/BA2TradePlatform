# S1–S7 relevance from the goal2020 grid

Read-only analysis, 2026-09-24. Source: `~/Documents/ba2/test/dl_forecasting.db`, opened `mode=ro`.
Scripts and outputs are in this folder (see §9).

## 0. Verdict

| Strategy | Recommendation for the ATR grid | Key evidence |
|---|---|---|
| **S1** | **KEEP** (every expert) | Top-3 by fitness in 17 of 22 cells, 1st in 10. Never last. It is in the pooled top 5 of 4 of 5 experts by fitness and 5 of 5 by CAR. Median top-row CAR is 16.7% with −13.3% drawdown. Caveat: it had 2.3× the GA budget of the others. |
| **S6** | **KEEP** (every expert) | Top-3 by fitness in 15 of 22 cells, 1st in 7. It wins 16 of 22 fitness head-to-heads against S2 and 19 of 22 against S3. It has the cleanest concentration: median top-5 share of P&L 30%, and 0% of P&L from positions still open at the end. |
| **S5** | **KEEP** (every expert), flagged | Top-3 by fitness in 13 of 22 cells. It beats S3 in 16 of 22 and S2 in 14 of 22. It is the most year-dependent strategy: in 6 of 22 top rows, one year carries more than half of the growth. |
| **S2** | **KEEP, except FMPInsiderClusterBuy** | Top-3 by fitness in 10 of 22 cells. In ICB it is last in 3 cells and 5th in the 4th, with fitness 0.10–0.67 and only 14–33% of trials succeeding. Its rule set contains S7's rules, with wider ranges. |
| **S3** | **DROP** | Never 1st in any cell, and top-3 by fitness in only 2 of 22. It is last in 10 of 22 cells on the all-GA-trials check. It loses to S5 in 16 of 22 cells by fitness and 15 of 22 by CAR. Its strongest pooled rows are concentrated: ICB-mid top-5 share is 95–97%. |
| **S7** | **DROP** (possible exception: FMPRating mid) | It is in the pooled top 5 of 0 of 5 experts by fitness and 0 of 5 by CAR. It ties S2 head to head (11–11). In FMPRating large, the cell it replicates, it ranks 3rd and 5th. |

- **The operator's literal rule drops nothing.** goal2020 ran six strategies, not seven: S4 was merged into S1 and the reborn S4 was never gridded. So "in the top 5" means "not last", and no strategy is last in every cell. The lowest count is S2, in the top 5 by fitness in 15 of 22 cells.
- **The pooled reading drops only S7.** Pooled per expert, S7 is the one strategy that never reaches any expert's top 5 by fitness or by CAR.
- **The recommendation above goes further than the rule.** It uses the head-to-head record and the concentration and year-dependence flags. That makes it a judgement, not a mechanical cut. §8 lists the places where the data is too thin.

## 1. What each strategy is (from `testplatform/ba2test_launcher.py`)

| Key | One line | Strategy genes |
|---|---|---|
| S1 (`_build_strategy_S1`, ~2093) | Three graded-conviction entry tiers, each a droppable entry rule, with an entry TP/SL bracket (TP anchored on the analyst target, SL from the fill). Exits: floor stop, bearish close, time exit. | 35 |
| S2 (`_build_strategy_row`, ~1642) | "Bracket + light exits": confidence and expected-profit gates, three cooldown gates, a fixed-% take-profit close, bearish/downgrade closes, breakeven lock, time exit and an always-on floor stop. | 20 |
| S3 (~1731) | Momentum/trailing: light gates, a 3-tier trailing-stop ladder, time exit and floor stop. No TP. | 16 |
| S5 (~1792) | S2/S3 hybrid: S2's signal exits and breakeven lock, S3's trailing ladder, and a wide 40–80% cap in place of the fixed TP. | 21 |
| S6 (~1866) | High-frequency quick cycle: entry TP 6–16% and SL −2..−10% bracket, signal exits, and an always-on short time exit (10–30 days). | 11 |
| S7 (~1915) | A replica of the archived FMPRating S2-large winner (#91): a profitable-close cooldown, toggleable gates, a +24..42% TP close, breakeven lock and a toggleable floor stop. | 15 |
| S4 | Not in goal2020. The old S4 was merged into S1, and the reborn structure-native S4 was never gridded. | — |

Gene counts are measured from each job's `parameter_ranges`, not computed.

## 2. Inventory

- **Rows.** There are 178 StrategyOptimization rows named `%goal2020%`: 135 `completed`, 30 `failed` and 13 `cancelled`.
- **Failed and cancelled rows.** Every one was superseded by a later completed row of the same cell. They were abandoned, 0-trial checkpoint, power-outage and inert-toggle relaunches.
- **No cell is missing.** The strategy cells are 22 cells × 6 strategies = 132 jobs, plus 3 FactorRanker jobs, which have no strategy and are excluded.
- **Cells:** 5 experts, with bands and 2 sizing modes.
  - FMPRating (FMPR): large, mid and small. Window 2022–2025, rows carry `-from2022`.
  - FMPEarningsDrift (ED): mid and small.
  - FMPInsiderClusterBuy (ICB): mid and small.
  - DeterministicScorer (DS, matrix 3, `-ds`): large, mid and small.
  - FMPSenateTraderWeight (SEN, `sen-S?-goal2020-*`): no band, post-lookahead-fix runs.

Opt ids per cell:

| expert | band | mode | S1 | S2 | S3 | S5 | S6 | S7 | window |
|---|---|---|---|---|---|---|---|---|---|
| DS | large | notional | 349 | 350 | 351 | 407 | 412 | 413 | 2020-2025 |
| DS | large | risk_atr | 334 | 335 | 336 | 371 | 375 | 378 | 2020-2025 |
| DS | mid | notional | 352 | 353 | 354 | 415 | 416 | 419 | 2020-2025 |
| DS | mid | risk_atr | 339 | 341 | 342 | 388 | 389 | 397 | 2020-2025 |
| DS | small | notional | 356 | 360 | 363 | 423 | 425 | 428 | 2020-2025 |
| DS | small | risk_atr | 346 | 347 | 348 | 448 | 439 | 440 | 2020-2025 |
| ED | mid | notional | 424 | 426 | 427 | 503 | 505 | 506 | 2020-2025 |
| ED | mid | risk_atr | 370 | 372 | 376 | 472 | 473 | 475 | 2020-2025 |
| ED | small | notional | 437 | 441 | 443 | 515 | 517 | 518 | 2020-2025 |
| ED | small | risk_atr | 386 | 395 | 411 | 486 | 488 | 490 | 2020-2025 |
| ICB | mid | notional | 430 | 431 | 432 | 507 | 509 | 510 | 2020-2025 |
| ICB | mid | risk_atr | 377 | 379 | 380 | 476 | 477 | 479 | 2020-2025 |
| ICB | small | notional | 447 | 454 | 455 | 519 | 520 | 521 | 2020-2025 |
| ICB | small | risk_atr | 414 | 417 | 418 | 492 | 494 | 495 | 2020-2025 |
| FMPR | large | notional | 390 | 396 | 398 | 497 | 498 | 499 | 2022-2025 |
| FMPR | large | risk_atr | 358 | 359 | 361 | 461 | 462 | 463 | 2022-2025 |
| FMPR | mid | notional | 399 | 400 | 402 | 500 | 501 | 502 | 2022-2025 |
| FMPR | mid | risk_atr | 364 | 365 | 366 | 465 | 466 | 471 | 2022-2025 |
| FMPR | small | notional | 433 | 435 | 436 | 511 | 513 | 514 | 2022-2025 |
| FMPR | small | risk_atr | 383 | 384 | 385 | 480 | 481 | 482 | 2022-2025 |
| SEN | all | notional | 493 | 496 | 504 | 508 | 512 | 516 | 2020-2025 |
| SEN | all | risk_atr | 460 | 470 | 474 | 487 | 489 | 491 | 2020-2025 |

Flags on the inventory:
- **11 of the 66 risk_atr/notional pairs are byte-identical** in best fitness and top row. They are DS S3 (mid, small), DS S5 and S6 (small), ED S3 small, ICB S1 and S3 small, FMPR S3 large, and FMPR S1, S2 and S3 mid. The per-instrument cap binds and collapses risk_atr onto notional. Those cells are one experiment counted twice.
- **Unequal GA budgets.** Outside Senate, S1 ran with population 140 and the others with 60, or 70 for FMPRating. Senate ran every strategy at 40. All jobs ran 8 generations with early stop 4. Outside Senate, S1 therefore recorded about 2.5× as many trials (~780 against ~300), which biases those comparisons toward S1. Senate S1 still ranked 1st in risk_atr at equal budget.
- **Low success rates for ICB with S2/S3/S5/S7 and for FMPR small with S2/S3/S7.** Only 5–35% of trials succeed. The rest score the −1e9 trapdoor, meaning no qualifying trades. This is structural, not bad luck, and it is why S2 and S3 sit last in ICB.
- **Thin `all_results` in resumed jobs.** The known limitation is that a resumed job's `all_results` restarts empty. Affected: ED small S5 risk_atr (11 recorded trials), sen-S5 risk_atr (16) and DS mid S5 risk_atr (52). `best_fitness` survives the resume. Only the trial pool is thin.
- **All 135 jobs ran with `robust_fitness=True`**, so fitness is comparable across the whole grid, and pooling by fitness is legitimate.

## 3. Which rows were used

- **Fitness:** `strategy_optimizations.best_fitness`, the GA's own number.
- **Economics:** the job's top persisted TOP-N row. TOP1 is persisted for all 135 jobs, and 108 jobs persisted all five ranks.
  - Profit is `final_equity − initial_capital`.
  - CAR is `results.annualized_return`.
  - DD is `max_drawdown`.
  - Per-year returns come from the equity curve.
  - Concentration (top-1 and top-5 trade P&L as a percentage of net P&L, and P&L from `open_at_end` positions) comes from the `trades` JSON.
  - Average capital in use is a Python port of `testplatform/frontend/src/lib/capitalUsage.ts`.
- **GA-genome match.** 410 rows carry `_atr_swap_migration` and/or `_inert_toggle_pin` blocks, so their stored genes differ from what the GA scored. After restoring each block's `from` values:
  - 642 of 647 persisted TOP-N rows reproduce the GA's own record of the same genome, with the same trade count and total return within 0.05 points.
  - 3 differ slightly: bt 1016 (284 vs 281 trades), bt 1017 (273 vs 272), and bt 1475 (sen S2 TOP1, 516 vs 518 trades, 75.1% vs 82.2% total return).
  - 2 have no matching trial in the recorded pool: FMPR small S3 and S7 notional, both in jobs with 43–44 successful trials.
  - The old "persisted TOP-N diverges from GA" problem is therefore absent from goal2020.
- **All-GA-trials cross-check.** Per cell, I took the best CAR among every successful GA trial with DD ≤ 25% and ≥ 60 trades (`ga_all_trials_bestcar.json`). This guards against the TOP-5 rows being unrepresentative.

## 4. Per-cell ranks

Each entry is rank by best GA fitness / rank by CAR of the job's top row. Lower is better; 6 strategies per cell.

| expert | band | mode | S1 | S2 | S3 | S5 | S6 | S7 |
|---|---|---|---|---|---|---|---|---|
| DS | large | notional | 4/1 | 3/3 | 5/5 | 2/2 | 1/4 | 6/6 |
| DS | large | risk_atr | 2/4 | 3/3 | 4/5 | 6/6 | 1/1 | 5/2 |
| DS | mid | notional | 4/2 | 2/3 | 6/5 | 3/4 | 1/1 | 5/6 |
| DS | mid | risk_atr | 4/3 | 2/4 | 5/5 | 3/2 | 1/1 | 6/6 |
| DS | small | notional | 2/3 | 1/2 | 6/6 | 3/5 | 5/4 | 4/1 |
| DS | small | risk_atr | 1/2 | 3/1 | 6/6 | 4/5 | 5/4 | 2/3 |
| ED | mid | notional | 1/1 | 6/6 | 5/5 | 3/3 | 4/2 | 2/4 |
| ED | mid | risk_atr | 1/1 | 3/2 | 6/5 | 2/3 | 5/6 | 4/4 |
| ED | small | notional | 3/5 | 1/2 | 5/4 | 2/3 | 6/6 | 4/1 |
| ED | small | risk_atr | 2/4 | 3/5 | 4/3 | 1/2 | 6/1 | 5/6 |
| ICB | mid | notional | 1/1 | 6/6 | 4/4 | 5/3 | 3/5 | 2/2 |
| ICB | mid | risk_atr | 1/1 | 5/2 | 6/6 | 2/5 | 3/3 | 4/4 |
| ICB | small | notional | 1/1 | 6/6 | 4/3 | 5/5 | 3/4 | 2/2 |
| ICB | small | risk_atr | 1/1 | 6/6 | 4/2 | 3/4 | 2/3 | 5/5 |
| FMPR | large | notional | 2/2 | 4/4 | 5/5 | 6/6 | 1/1 | 3/3 |
| FMPR | large | risk_atr | 4/1 | 3/3 | 2/4 | 6/5 | 1/2 | 5/6 |
| FMPR | mid | notional | 1/1 | 6/5 | 5/6 | 4/4 | 3/2 | 2/3 |
| FMPR | mid | risk_atr | 2/1 | 6/5 | 4/6 | 5/3 | 3/4 | 1/2 |
| FMPR | small | notional | 2/1 | 4/4 | 5/3 | 3/6 | 1/2 | 6/5 |
| FMPR | small | risk_atr | 1/1 | 5/2 | 6/5 | 4/6 | 3/4 | 2/3 |
| SEN | all | notional | 4/4 | 6/6 | 5/3 | 1/2 | 2/1 | 3/5 |
| SEN | all | risk_atr | 1/1 | 4/5 | 3/3 | 2/2 | 5/4 | 6/6 |

Best GA fitness per cell:

| expert | band | mode | S1 | S2 | S3 | S5 | S6 | S7 |
|---|---|---|---|---|---|---|---|---|
| DS | large | notional | 4.73 | 5.60 | 4.11 | 6.42 | 7.41 | 4.03 |
| DS | large | risk_atr | 5.74 | 4.84 | 4.21 | 3.80 | 7.80 | 3.99 |
| DS | mid | notional | 4.54 | 5.89 | 3.43 | 5.85 | 6.34 | 3.70 |
| DS | mid | risk_atr | 3.94 | 4.34 | 3.43 | 4.20 | 8.42 | 2.84 |
| DS | small | notional | 5.00 | 5.90 | 1.72 | 4.25 | 3.56 | 4.03 |
| DS | small | risk_atr | 6.67 | 4.47 | 1.72 | 4.25 | 3.56 | 5.84 |
| ED | mid | notional | 8.00 | 3.18 | 4.13 | 6.65 | 5.84 | 7.14 |
| ED | mid | risk_atr | 8.29 | 5.38 | 3.90 | 5.92 | 4.53 | 5.23 |
| ED | small | notional | 5.62 | 7.83 | 4.34 | 6.50 | 3.75 | 4.77 |
| ED | small | risk_atr | 5.85 | 5.00 | 4.34 | 6.14 | 3.77 | 3.86 |
| ICB | mid | notional | 7.48 | 0.10 | 0.35 | 0.28 | 1.78 | 2.26 |
| ICB | mid | risk_atr | 4.01 | 0.50 | 0.02 | 2.91 | 2.23 | 2.19 |
| ICB | small | notional | 13.16 | 0.19 | 1.98 | 1.34 | 2.85 | 4.78 |
| ICB | small | risk_atr | 13.16 | 0.67 | 1.98 | 2.14 | 5.21 | 1.88 |
| FMPR | large | notional | 8.00 | 5.97 | 5.94 | 4.44 | 8.53 | 7.08 |
| FMPR | large | risk_atr | 5.55 | 5.92 | 5.94 | 3.37 | 7.47 | 3.85 |
| FMPR | mid | notional | 6.05 | 1.79 | 2.78 | 3.12 | 3.45 | 3.67 |
| FMPR | mid | risk_atr | 6.05 | 1.79 | 2.78 | 1.97 | 3.70 | 7.96 |
| FMPR | small | notional | 6.77 | 1.63 | 0.00 | 3.10 | 13.30 | 0.00 |
| FMPR | small | risk_atr | 6.39 | 0.90 | 0.27 | 2.29 | 3.86 | 4.08 |
| SEN | all | notional | 4.75 | 3.09 | 4.39 | 5.83 | 5.46 | 5.42 |
| SEN | all | risk_atr | 6.17 | 5.19 | 5.44 | 5.57 | 4.23 | 3.95 |

Economics of each job's top persisted row, as CAR % / max DD % / trades:

| expert | band | mode | S1 | S2 | S3 | S5 | S6 | S7 |
|---|---|---|---|---|---|---|---|---|
| DS | large | notional | 17.6 / -18.6 / 340 | 15.3 / -13.6 / 636 | 9.2 / -11.2 / 638 | 15.8 / -10.9 / 238 | 15.1 / -9.9 / 188 | 8.1 / -9.9 / 316 |
| DS | large | risk_atr | 11.7 / -8.9 / 271 | 12.6 / -12.9 / 705 | 8.7 / -13.2 / 465 | 8.3 / -10.9 / 583 | 24.5 / -15.6 / 451 | 14.7 / -17.7 / 82 |
| DS | mid | notional | 16.8 / -10.6 / 774 | 16.2 / -13.5 / 327 | 10.7 / -12.3 / 1008 | 13.9 / -11.9 / 162 | 17.4 / -20.5 / 279 | 10.6 / -14.3 / 365 |
| DS | mid | risk_atr | 15.7 / -17.5 / 403 | 15.3 / -17.7 / 446 | 10.7 / -12.3 / 1008 | 15.8 / -13.6 / 106 | 20.1 / -11.8 / 227 | 8.3 / -14.2 / 520 |
| DS | small | notional | 12.5 / -14.7 / 329 | 12.9 / -9.2 / 335 | 7.0 / -8.9 / 975 | 9.9 / -9.6 / 216 | 10.0 / -11.1 / 290 | 14.2 / -12.7 / 307 |
| DS | small | risk_atr | 17.1 / -12.2 / 128 | 22.3 / -23.3 / 163 | 7.0 / -8.9 / 975 | 9.9 / -9.6 / 216 | 10.0 / -11.1 / 290 | 14.8 / -11.9 / 364 |
| ED | mid | notional | 24.9 / -16.4 / 472 | 9.4 / -13.0 / 407 | 13.4 / -18.5 / 508 | 16.4 / -17.7 / 568 | 17.0 / -8.0 / 728 | 15.6 / -10.9 / 406 |
| ED | mid | risk_atr | 28.4 / -16.3 / 562 | 12.0 / -10.4 / 261 | 10.9 / -13.4 / 600 | 11.8 / -8.9 / 362 | 10.3 / -7.0 / 558 | 11.3 / -10.7 / 207 |
| ED | small | notional | 12.6 / -16.3 / 776 | 15.4 / -10.8 / 471 | 12.8 / -14.6 / 221 | 13.8 / -10.6 / 616 | 6.8 / -12.7 / 236 | 17.3 / -17.9 / 570 |
| ED | small | risk_atr | 12.3 / -11.6 / 580 | 10.9 / -12.3 / 311 | 12.8 / -14.6 / 221 | 17.9 / -14.2 / 368 | 19.3 / -23.1 / 365 | 8.4 / -9.4 / 201 |
| ICB | mid | notional | 15.8 / -9.4 / 269 | 6.8 / -10.0 / 145 | 7.9 / -18.2 / 219 | 10.7 / -21.5 / 169 | 7.3 / -13.2 / 158 | 13.7 / -13.2 / 108 |
| ICB | mid | risk_atr | 17.9 / -13.2 / 181 | 11.2 / -20.4 / 152 | 4.9 / -16.4 / 206 | 5.8 / -9.7 / 305 | 7.7 / -11.2 / 290 | 6.7 / -14.9 / 191 |
| ICB | small | notional | 23.8 / -9.7 / 244 | 2.9 / -4.8 / 89 | 12.7 / -22.8 / 339 | 8.5 / -16.6 / 392 | 12.0 / -14.7 / 213 | 21.8 / -17.5 / 168 |
| ICB | small | risk_atr | 23.8 / -9.7 / 244 | 3.4 / -7.0 / 232 | 12.7 / -22.8 / 339 | 7.3 / -11.8 / 142 | 9.3 / -10.4 / 188 | 5.5 / -12.8 / 172 |
| FMPR | large | notional | 16.0 / -9.0 / 257 | 13.2 / -7.7 / 134 | 11.8 / -8.1 / 140 | 10.1 / -11.3 / 133 | 20.6 / -12.2 / 198 | 15.0 / -10.6 / 126 |
| FMPR | large | risk_atr | 16.2 / -14.6 / 297 | 15.1 / -9.1 / 140 | 11.8 / -8.1 / 140 | 10.0 / -15.2 / 373 | 15.5 / -9.6 / 229 | 8.0 / -10.3 / 277 |
| FMPR | mid | notional | 16.7 / -15.2 / 200 | 8.1 / -6.4 / 255 | 6.8 / -11.3 / 809 | 8.2 / -13.1 / 538 | 9.6 / -5.5 / 144 | 8.4 / -11.4 / 284 |
| FMPR | mid | risk_atr | 16.7 / -15.2 / 200 | 8.1 / -6.4 / 255 | 6.8 / -11.3 / 809 | 14.1 / -15.0 / 207 | 11.9 / -14.0 / 1322 | 14.9 / -11.0 / 144 |
| FMPR | small | notional | 28.7 / -13.5 / 387 | 12.8 / -15.6 / 144 | 13.0 / -22.1 / 162 | 7.4 / -11.0 / 484 | 17.7 / -12.1 / 94 | 8.0 / -34.8 / 51 |
| FMPR | small | risk_atr | 19.4 / -17.2 / 436 | 18.3 / -15.7 / 101 | 8.1 / -11.7 / 207 | 5.1 / -10.6 / 304 | 10.9 / -12.1 / 198 | 12.4 / -13.3 / 193 |
| SEN | all | notional | 11.3 / -11.4 / 296 | 6.6 / -7.4 / 1114 | 11.3 / -12.4 / 376 | 11.8 / -10.1 / 269 | 14.3 / -9.7 / 405 | 10.8 / -9.2 / 241 |
| SEN | all | risk_atr | 14.0 / -11.2 / 178 | 9.8 / -8.9 / 516 | 10.9 / -9.4 / 351 | 11.2 / -7.3 / 677 | 10.1 / -9.6 / 546 | 8.6 / -10.8 / 201 |

The full per-cell detail is in `s1_s7_cell_ranks.csv`: profit, calmar, win rate, capital in use, top-1/top-5, year shares, GA-match counts and the best CAR among all TOP-N rows.

## 5. Top-5 membership, both readings

### (a) Per cell: top 5 of the cell's 6 strategies

| strat | cells | top-5 fit | top-5 CAR | top-3 fit | top-3 CAR | 1st fit | 1st CAR | last fit | last CAR | mean rank fit | mean rank CAR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | 22 | 22 | 22 | 17 | 18 | 10 | 13 | 0 | 0 | 2.05 | 1.91 |
| S2 | 22 | 15 | 17 | 10 | 10 | 2 | 1 | 7 | 5 | 4.00 | 3.86 |
| S3 | 22 | 16 | 17 | 2 | 6 | 0 | 0 | 6 | 5 | 4.77 | 4.50 |
| S5 | 22 | 19 | 18 | 13 | 10 | 2 | 0 | 3 | 4 | 3.41 | 3.91 |
| S6 | 22 | 20 | 20 | 15 | 12 | 7 | 6 | 2 | 2 | 2.95 | 2.95 |
| S7 | 22 | 18 | 16 | 9 | 10 | 1 | 2 | 4 | 6 | 3.82 | 3.86 |

Cells where a strategy is **outside** the top 5 by fitness, meaning it came last:
- **S1:** none.
- **S2:** ED mid notional, ICB mid notional, ICB small (both modes), FMPR mid (both modes) and SEN notional.
- **S3:** DS mid notional, DS small (both modes), ED mid risk_atr, ICB mid risk_atr, and FMPR small risk_atr.
- **S5:** DS large risk_atr, and FMPR large (both modes).
- **S6:** ED small (both modes).
- **S7:** DS large notional, DS mid risk_atr, FMPR small notional, and SEN risk_atr.

**All-GA-trials check.** This ranks by the best CAR among all trials with DD ≤ 25% and ≥ 60 trades.
- Top-5 counts: S1 19, S2 20, S3 12, S5 20, S6 19, S7 20.
- Last place: **S3 10 of 22**, S6 3, S1 3, S2 2, S5 2, S7 2.
- It confirms S3 as the bottom strategy, independently of the persisted rows.

Head to head over the 22 cells: row beats column, by fitness | by CAR.

| | S1 | S2 | S3 | S5 | S6 | S7 |
|---|---|---|---|---|---|---|
| S1 | — | 16\|18 | 21\|19 | 16\|18 | 14\|16 | 20\|19 |
| S2 | 6\|4 | — | 13\|14 | 8\|11 | 6\|7 | 11\|11 |
| S3 | 1\|3 | 9\|8 | — | 6\|7 | 3\|6 | 7\|9 |
| S5 | 6\|4 | 14\|11 | 16\|15 | — | 9\|6 | 12\|10 |
| S6 | 8\|6 | 16\|15 | 19\|16 | 13\|16 | — | 11\|14 |
| S7 | 2\|3 | 11\|11 | 14\|13 | 10\|12 | 11\|8 | — |

Order: S1 > S6 > S5 > S2 ≈ S7 > S3.

### (b) Pooled: all goal2020 TOP-N rows per expert, and per expert × band

This counts how many pools include at least one of the strategy's TOP-N rows in the pool's overall top 5.

| strat | expert pools hit, fit / CAR / calmar (of 5) | expert × band pools hit, fit / CAR / calmar (of 11) |
|---|---|---|
| S1 | 4 / 5 / 3 | 8 / 8 / 7 |
| S2 | 1 / 3 / 1 | 2 / 5 / 4 |
| S3 | 1 / 1 / 0 | 1 / 2 / 0 |
| S5 | 1 / 1 / 1 | 3 / 1 / 3 |
| S6 | 3 / 2 / 4 | 5 / 5 / 8 |
| S7 | **0 / 0** / 1 | 2 / 3 / 2 |

Pooled top 5 per expert:
- **DS, 179 rows:**
  - Fitness: S6×5. All five come from the risk_atr mid/large jobs, 7.57–8.42.
  - CAR: S6 31.2%, S6 24.5%, S2 22.3%, S2 22.0%, S1 20.9%.
- **ED, 116 rows:**
  - Fitness: S1 8.29, S1 8.00, S2 7.83, S1 7.79, S1 7.68.
  - CAR: S1 28.4%, S5 27.8%, S1 24.9%, S2 22.8%, S3 22.6%.
- **ICB, 114 rows:** S1 takes all 10 places, by fitness (13.16 top) and by CAR (27.4% top).
- **FMPR, 170 rows:**
  - Fitness: S6 13.30, S6 11.81, S6 8.53, S6 8.26, S1 8.00.
  - CAR: S1 37.0%, S1 35.5%, S1 28.7%, S2 23.9%, S1 23.6%.
- **SEN, 53 rows:**
  - Fitness: S1 6.17, S5 5.83, S5 5.75, S6 5.46, S3 5.44.
  - CAR: S1 15.0%, then S6/S1 at 14.3–14.4%.

Where the weaker strategies did reach a pool, and why it does not rescue them:
- **S3.** It reaches the ED CAR pool through #1103, 22.6% CAR and −21.0% DD, a ForwardTestCandidate. It also reaches the ICB-mid CAR pool, but every such row has a **top-5 trade share of 95–97%** (#1140–1142). That is concentration, not an edge.
- **S7.** It reaches FMPR-mid, where it has the cell's best fitness (7.96 in risk_atr), and the DS-small and ICB-mid pools.
- **S5.** It reaches the Senate pools and ED-mid through #1598, whose CAR of 27.8% comes from a +90.8% year in 2020.
- **S2.** It reaches DS-small, ED-small and FMPR large/mid calmar. Its best CAR row, #1155 (FMPR small, 23.9%), has **top-1 58% / top-5 81%**.

## 6. Per strategy: best economics, concentration and year dependence

The median columns are over each job's top persisted row (22 rows per strategy).

| strat | best CAR (bt, cell) | its DD | its top1/top5 % | best calmar (bt) CAR/DD | median CAR / DD / trades | median top1% / top5% | median capital in use % | top5 > 50% | best year > 50% of growth | 2020 > 50% | 2022 > 50% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | 37.0% (#1331, FMPR/small/notional) | -23.3 | 29/65 | 4.12 (#1244) 23.4/-5.7 | 16.7 / -13.3 / 296 | 9 / 33 | 29 | 2/22 | 3/22 | 2/22 | 0/22 |
| S2 | 23.9% (#1155, FMPR/small/risk_atr) | -21.8 | 58/81 | 1.71 (#1050) 13.2/-7.7 | 12.3 / -10.6 / 258 | 11 / 33 | 28 | 7/22 | 1/22 | 1/22 | 0/22 |
| S3 | 22.6% (#1103, ED/mid/risk_atr) | -21.0 | 13/50 | 1.47 (#1055) 11.8/-8.1 | 10.8 / -12.4 / 364 | 11 / 39 | 32 | 4/22 | 4/22 | 2/22 | 0/22 |
| S5 | 27.8% (#1598, ED/mid/notional) | -25.4 | 6/27 | 1.54 (#1523) 11.2/-7.3 | 10.4 / -11.2 / 304 | 9 / 36 | 29 | 3/22 | 6/22 | 2/22 | 0/22 |
| S6 | 31.2% (#1109, DS/large/risk_atr) | -26.7 | 10/40 | 2.12 (#1602) 17.0/-8.0 | 11.9 / -11.5 / 258 | 8 / 30 | 25 | 1/22 | 1/22 | 1/22 | 0/22 |
| S7 | 21.8% (#1681, ICB/small/notional) | -17.5 | 10/42 | 1.79 (#1607) 14.5/-8.1 | 11.1 / -12.3 / 204 | 9 / 33 | 27 | 1/22 | 5/22 | 1/22 | 0/22 |

Median share of P&L from positions still `open_at_end` in the top rows:

| Strategy | Median | Max |
|---|---|---|
| S6 | 0% | 4% |
| S1 | 1.8% | 14% |
| S2 | 6.5% | 119% |
| S3 | 7.5% | 121% |
| S7 | 7.7% | 166% |
| S5 | 10.6% | 111% |

A value above 100% means never-exited winners carry more than the whole net P&L. S6's time exit makes it the only structurally clean strategy, which matches the sen5min3 finding.

Concentration cost one full pass over the 647 TOP-N `trades` and `equity_curve` blobs, about 75 s, so it was computed for every row.

### The regime-overlay question

**No goal2020 result was earned through `regime_overlay`.**
- Of the 416 pre-pin TOP rows, 190 carried `regime_overlay_enabled=1` as the GA searched it.
- Every one executed with the overlay off. The bool gene stored as `"1"` read as False, and `_inert_toggle_pin` now records that.
- `use_atr_stop` is the same: 183 rows searched 1 and executed 0.
- So "wins via the overlay" does not exist here. The real 2020/2022 exposure has to be read from the per-year returns.

Top rows where one year carries more than half of the cumulative log growth:
- **2020-shaped** (that year carries more than half):
  - ICB mid: S1 #1113 and #1298 (50–53%), S5 #1487 (87%, two negative years), S6 #1619 (61%) and S7 #1628 (74%).
  - ICB small: S3 #1256/#1410 (60%) and S5 #1548 (66%).
  - This is the whole ICB-mid cell, as the shortlist memory already noted: one big 2020, then about 10% a year.
- **Other single-year shapes:** in FMPR (2022–2025) the dominant year is 2023, 2024 or 2025, never 2022. S5 (#1568/#1579/#1448), S7 (#1443/#1588/#1642/#1513) and S3 (#1217/#1087) account for 9 of the 10 such FMPR rows.
- **Best-CAR rows that are really 2020 plus one other year:**
  - S6 #1109 (DS large): +97% in 2020 and +82% in 2024, 81% average capital in use, −26.7% DD.
  - S5 #1598 (ED mid): +91% in 2020.
  - Neither should be read as a steady edge.

Deployed and candidate rows are not affected by the drop list. #1103 (S3) and #1088 (S1) were candidates for ED mid; #1113/#1302 (S1) for ICB mid. Dropping a strategy from the new grid changes no live instance.

## 7. Recommendation for the ATR grid

- **KEEP S1, S6 and S5 on every expert. KEEP S2 on every expert except FMPInsiderClusterBuy. DROP S3 and S7.**
  - Why S3 goes: it is the only strategy that never places 1st, is top-3 in 2 of 22 cells, loses to S5 in about 70% of cells, and is last in 10 of 22 on the all-trials check. S5 contains S3's trailing ladder, so S3's one idea stays in the grid.
  - Why S7 goes: it is the only strategy absent from every expert's pooled top 5, and it ties S2 head to head. S2's rules are a superset of S7's (the same TP close, breakeven lock, signal closes and floor stop, plus time and cooldown gates, over wider ranges). The GA can rediscover S7 inside S2.
  - **Exception to consider:** S7 has FMPR-mid risk_atr's best fitness (7.96 against S1's 6.05). If the operator wants that cell covered, run S7 for FMPR mid only (+2 jobs with control).
  - Why S2 is dropped for ICB: it is last or 5th in all 4 ICB cells, with fitness 0.10–0.67, and only 14–33% of its trials trade. For ICB the grid keeps S1, S5 and S6.
- **Flags to carry into the ATR grid's reporting:**
  - S5 is the most year-dependent strategy. Report its per-year returns and top-1/top-5 before trusting it.
  - S2's best row is concentrated (58% / 81%).
  - The ICB-mid cell is 2020-shaped whatever the strategy.
- **Cost of the choice.** With the same budget rule and an all-off control per cell (see the design draft), the keep list is **84 jobs, about 61 days** sequential. All six strategies would be 128 jobs, about 93 days.

## 8. Where the data is too thin to decide

1. **One GA run per cell.** Each cell had one seed, 8 generations and early stop 4. There are no replicates, so seed noise is unmeasured. Many cells separate the 2nd–4th strategies by less than 15% of fitness. Those ranks are not significant, and S2 vs S5 vs S7 is inside that band.
2. **Outside Senate, S1 had about 2.5× the search budget** (population 140 against 60/70). Part of its dominance may be budget. The A4 rule removes that asymmetry, since every job gets population 120, and the ATR grid will show how much of S1's lead is real.
3. **Several risk_atr/notional pairs are duplicates.** 11 of the 66 are byte-identical, so the 22 cells are fewer independent experiments than they look. That affects DS small, FMPR mid and ICB small most.
4. **FMPRating has 4 years (2022–2025)** and no 2020 stress year. Its year-dependence figures are not comparable with the other experts'.
5. **ICB with S2/S3/S5/S7 and FMPR small with S2/S3/S7 mostly do not trade.** 5–35% of their trials succeed. Their low ranks say that the strategy's gates do not fit the expert's recommendations. They say nothing about the exits.
6. **Senate is one universe with 12 jobs.** Its S1–S7 ordering is flat (fitness 3.1–6.2), and its conclusions rest on the thinnest evidence.
7. **The 2026 holdout was never scored**, so every rank here is in-sample.

## 9. Reproduce

All scripts are read-only on the DB and write only to this folder.

1. `.venv/Scripts/python.exe extract_goal2020.py` writes `goal2020_extract.json` in about 75 s. It uses covering-index reads for the summary columns and one blob per row for `results`, `trades` and `equity_curve`.
2. `.venv/Scripts/python.exe analyze_s1_s7.py` writes:
   - `s1_s7_cell_ranks.csv`: one row per expert/band/mode/strategy;
   - `s1_s7_pooled.csv`: reading (b);
   - `s1_s7_strategy_summary.csv`;
   - `analysis_tables.md`.
3. `supplement.py` and `supplement2.py` cover the GA-match counts, identical pairs, the all-trials ranking (`ga_all_trials_bestcar.json`), the head-to-head table, `open_at_end` shares and per-trial timing.
4. `cost_model.py` produces the cost figures used in the design draft.
