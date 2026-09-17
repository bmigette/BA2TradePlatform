# Dev-deployed backtests re-run under the corrected per-instrument ceiling

The 26 backtests behind the enabled dev ExpertInstances, re-run on remote227 at `TEST_APP_VERSION 2026.09.0043` / commit `187ccbdb`.

**No stored row was modified.** The stored numbers stay the record of what was selected; everything below is a comparison, not a replacement.

## What changed and why

The classic risk manager took the per-instrument ceiling from `available x ratio` -- what was LEFT in the sleeve -- so the cap shrank as the sleeve filled. It now takes `virtual x ratio`, a fixed share of the sleeve. Later entries in a cycle are therefore sized larger, which is why almost every row trades FEWER names for MORE return and a slightly deeper drawdown.

**19 improved, 4 degraded, 3 bit-identical.** Median CAR change +1.00 pts, mean +1.01, range -2.06 to +3.57.

The 3 bit-identical rows are FactorRanker strategies, which bypass the classic RM altogether -- they are the control, and they confirm the harness reproduces a stored run exactly when the fix does not apply to it.

## Rows marked * carry a hand-written pin

Twelve rows were rewritten by hand in September (`_atr_swap_migration`, `_inert_toggle_pin`) so `strategy_params` shows what ACTUALLY executed after dd1f912e was found to have wired `atr_risk_budget_pct` and `risk_per_trade_pct` to each other's jobs. Rebuilding from the raw GA genome does not reproduce those runs; the harness re-applies each pin before submitting. A first pass without this showed bt1107 at CAR 6.37 -- an artefact of the harness sizing on a 0.5% risk budget where the row executed on 7%, not a platform regression.

| bt | instance alias | trades | total % | CAR % | maxDD % | |
|---|---|---|---|---|---|---|
| 1000 | ft3-sm-DetScorer-S1-riskatr-bt1000 | 261 → 133 | +136.97 → +112.71 | +15.48 → +13.42 | -16.14 → -21.79 | * |
| 1146 | ft1-mid-FactorRanker-riskatr-bt1146 | 191 → 35 | +43.29 → +34.06 | +6.19 → +5.01 | -12.85 → -12.34 |  |
| 1528 | ft2-sm-EarnDrift-S6-riskatr-bt1528 | 365 → 331 | +187.70 → +173.41 | +19.28 → +18.27 | -23.12 → -23.65 |  |
| 1070 | ft1-mid-Rating-S1-riskatr-bt1070 | 200 → 184 | +85.11 → +84.12 | +16.69 → +16.53 | -15.24 → -15.43 | * |
| 1064 | ft1-lg-FactorRanker-riskatr-bt1064 | 1151 → 1151 | +98.39 → +98.39 | +12.11 → +12.11 | -11.67 → -11.67 |  |
| 1065 | ft1-lg-FactorRanker-riskatr-bt1065 | 1279 → 1279 | +104.43 → +104.43 | +12.67 → +12.67 | -12.04 → -12.04 |  |
| 1143 | ft1-mid-FactorRanker-riskatr-bt1143 | 236 → 236 | +29.66 → +29.66 | +4.43 → +4.43 | -7.31 → -7.31 |  |
| 1619 | ft2-mid-Insider-S6-notional-bt1619 | 158 → 153 | +52.45 → +55.21 | +7.29 → +7.61 | -13.22 → -13.38 |  |
| 1434 | ft1-lg-Rating-S6-riskatr-bt1434 | 382 → 336 | +99.89 → +102.51 | +18.96 → +19.35 | -13.24 → -13.41 |  |
| 1439 | ft3-sen-Senate-S1-riskatr-bt1439 | 178 → 160 | +119.53 → +124.04 | +14.02 → +14.41 | -11.25 → -12.39 | * |
| 1173 | ft3-mid-DetScorer-S6-riskatr-bt1173 | 231 → 214 | +208.17 → +215.02 | +20.66 → +21.10 | -12.78 → -15.35 | * |
| 1508 | ft1-sm-Rating-S6-riskatr-bt1508 | 198 → 194 | +51.14 → +53.73 | +10.91 → +11.38 | -12.05 → -11.98 |  |
| 1386 | ft2-sm-Insider-S1-notional-bt1386 | 299 → 296 | +327.09 → +338.66 | +27.41 → +27.98 | -12.51 → -13.99 | * |
| 1607 | ft2-mid-EarnDrift-S7-notional-bt1607 | 315 → 307 | +124.68 → +133.75 | +14.46 → +15.22 | -8.06 → -8.04 |  |
| 1649 | ft3-sen-Senate-S6-notional-bt1649 | 468 → 451 | +124.17 → +136.26 | +14.42 → +15.42 | -9.41 → -10.79 |  |
| 1681 | ft2-sm-Insider-S7-notional-bt1681 | 168 → 161 | +226.73 → +243.59 | +21.84 → +22.87 | -17.52 → -18.95 |  |
| 1358 | ft3-sm-DetScorer-S7-riskatr-bt1358 | 488 → 357 | +128.80 → +142.84 | +14.81 → +15.95 | -12.36 → -18.40 | * |
| 1088 | ft2-mid-EarnDrift-S1-riskatr-bt1088 | 562 → 531 | +346.98 → +375.05 | +28.38 → +29.69 | -16.28 → -17.18 | * |
| 1298 | ft2-mid-Insider-S1-notional-bt1298 | 269 → 258 | +140.82 → +158.40 | +15.79 → +17.16 | -9.38 → -10.52 | * |
| 1367 | ft2-sm-EarnDrift-S2-notional-bt1367 | 237 → 157 | +242.37 → +267.08 | +22.79 → +24.23 | -19.15 → -21.74 | * |
| 1583 | ft1-mid-Rating-S6-notional-bt1583 | 284 → 238 | +52.42 → +60.82 | +11.14 → +12.65 | -15.17 → -17.27 |  |
| 1182 | ft1-lg-Rating-S1-notional-bt1182 | 256 → 240 | +90.25 → +107.49 | +17.49 → +20.08 | -11.71 → -11.54 | * |
| 1030 | ft3-mid-DetScorer-S2-notional-bt1030 | 327 → 283 | +146.59 → +184.58 | +16.25 → +19.06 | -13.50 → -14.50 | * |
| 1107 | ft3-lg-DetScorer-S6-riskatr-bt1107 | 451 → 344 | +271.68 → +332.43 | +24.49 → +27.67 | -15.59 → -16.88 | * |
| 1633 | ft1-sm-Rating-S5-notional-bt1633 | 524 → 360 | +30.98 → +48.22 | +7.00 → +10.37 | -10.66 → -17.22 |  |
| 1017 | ft3-lg-DetScorer-S2-notional-bt1017 | 273 → 242 | +161.98 → +213.42 | +17.43 → +21.00 | -17.24 → -17.88 | * |

## Worth a second look

* **bt1000** (`ft3-sm-DetScorer-S1-riskatr`) is the only material loser: CAR 15.48 -> 13.42 AND drawdown -16.14 -> -21.79, on half the trades (261 -> 133). Larger positions concentrated it into a worse book.
* **bt1633** keeps the biggest DD deterioration among the winners: -10.66 -> -17.22 for CAR 7.00 -> 10.37. Judge it on whether that trade is wanted, not on the CAR alone.
* **bt1358** and **bt1367** also buy their gain with drawdown (-12.36 -> -18.40 and -19.15 -> -21.74).
* **bt1146** (FactorRanker, `ft1-mid-FactorRanker-riskatr`) moved 191 -> 35 trades and CAR 6.19 -> 5.01 -- and the ceiling CANNOT be the cause: `FactorRanker.bypasses_classic_rm` is True, so the changed code never runs for it, and its three sibling FactorRanker rows reproduce bit-identically. Checked: the rebuilt genome matches the stored row key-for-key (not a config mismatch); three re-runs returned identical results (not nondeterminism); and bt1143 came from the SAME optimization 34 minutes later and does reproduce (not general code drift). **Unresolved** -- something between 2026-08-29 and today changes this run and it is not the ceiling. Until it is found, read this table as 'the ceiling AND whatever this is', noting that for the 3 rows where the ceiling provably does nothing, whatever-this-is contributed exactly zero.

## The selection question this raises

Every deployed instance was chosen by ranking backtests produced under the OLD ceiling. The ranking is not preserved: the change is worth up to 3.6 CAR points and is not uniform (it depends on how many names a cycle funds and in what order), so a strategy that lost a shortlist place by a fraction of a point may not have lost it under the corrected sizing. Re-ranking the shortlist is a separate decision and has not been done here.