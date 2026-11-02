# Worthy bearish signals for put / sell structures: findings (2026-09-28)

Can the option grid find profitable bearish structures (long puts, bear call spreads) on the stage-1
universe, and what should happen to the remaining bearish job, O_BEARCS?

**Short answer.**

- **Why O_LP (opt 18) produced nothing:** the funnel dies at the GA-sampled market-condition gates,
  then at the fitness robustness screen. It does not die at the DeterministicScorer (DS) signal.
- **Do profitable puts exist?** Yes, in two windows: February-March 2020 and 2022. Unconditional
  puts bought in those months paid +73% to +282% per month. Outside them, puts lost money in every
  year.
- **Can an oracle-free signal find them?** None of the more than 60 signal variants measured (DS,
  price rules, technical features, factors, earnings, analysts, insiders, index regime) turned put
  buying profitable on these 97 names. The best edge is a weak underperformance signal: earnings
  misses, about −1.0% to −1.4% excess over 20 days. The stocks lag the market; they do not fall.
- **O_BEARCS:** skip it. It has the same entry funnel as O_LP. Its structure also loses in every
  year except 2022, both unconditionally and on a bearish price rule.

Everything below is measured unless marked **Assumption** or **Estimate**. Scripts and raw outputs
are listed in §8.

## 1. Method and conventions

- **Universe:** `tools/options_universe_top100.txt` (97 names).
- **Window:** signals dated 2020-01-01 to 2025-12-31.
- **Code:** dev `9ed2c553`. The grid host runs `6ed40d5a`, an ancestor. Between them nothing changed
  in DeterministicScorer or strategy_fitness; only small changes in TradeActions and
  backtest_account.
- **Grid data:** `strategy_optimizations` rows 14-19 of the stage-1 matrix DB on remote227, read with
  one read-only query. Opt 18 = O_LP: 807 unique genomes, `option_car_target_soft30`, robust
  fitness on. Opt 17 = O_LC: 3,142 genomes.
- **DS signal supply:** recomputed on 2026-09-28 through the real `analyze_as_of` path (141,106 scored
  bars, 96 symbols). It includes the FRED publication-lag fix `86c2bb0f`, which landed after the
  2026-09-24 probe. Genomes are re-scored with a vectorised copy of `combine.final_score`, which
  matched the real function to under 1e-12 on 3,000 bars × 4 settings. `macro_short_side=mirror` as
  in the grid.
- **Forward returns** (event studies):
  - Entry is the close of t+1, one bar after the signal. `fwdN = close[t+1+N] / close[t+1] − 1`.
    **hit** = fwd < 0.
  - **xs** is the forward return minus the equal-weight mean of the 97 names over the same dates.
    It removes the market move, so a signal that only fires in bear markets shows a good hit rate
    and roughly zero xs.
  - Signals on the same symbol within 20 trading days merge into one **episode**.
  - Unconditional baseline: 20d hit 42.8%, mean +1.83%; 60d hit 38.0%, mean +5.84%. 2022 is the only
    year with a negative mean: 20d hit 53.0%, mean −0.39%.
- **Put P&L:**
  - Source: the local ThetaData cache.
  - Entry: one put about 5% OTM with about 45 days to expiry (DTE), bought at the t+1 close ASK.
  - Exit: at the BID after 20 (or 40) trading days, or on the last bar before expiry.
  - The strike is chosen from the spot price implied by put-call parity, which avoids split-basis
    errors. Windows that cross a split are dropped.
  - **Book** = sum of exit bids / sum of entry asks − 1, i.e. one contract per signal. "Mid" repeats
    this at mid prices.
- **Full backtests:** `test_files/probe_olp_funnel_20260924.py`, copied with new paths, running the
  real `run_daily_backtest`. Every rerun reproduced its GA record: p_top_below and p_best_off_66
  exactly; p_most_trades_below at 144 trades and −90.05% against the GA's 141 and −90.18%.
- **Probe deviations from the grid:**
  - The probe's intraday-drawdown hook predates the `peak_at` keyword. Refinement failed in the
    reruns, so their max drawdown is the daily-curve figure. The trades and returns are unaffected.
  - The locally cached split calendar lists a 2:1 APH split on 2026-09-03. The option-basis guard
    therefore refused APH 2020 chains (parity/as-traded = 0.50), and the later reruns exclude APH.
    **Side risk:** a grid host whose split calendar refreshes without an OHLCV refresh would abort
    any job that touches APH.

## 2. Q1: why O_LP produced nothing (the measured funnel)

### 2.1 Grid outcome (opt 18, early-stopped at generation 5)

| trades | genomes | median return | positive return | fitness_raw > 0 | final fitness > 0 |
|---|---|---|---|---|---|
| 0 | 285 | 0 | 0 | 0 | 0 |
| 1-9 | 338 | −0.5% | 146 | 103 | 0 |
| 10-29 | 96 | −19.1% | 20 | 7 (all 10+ rows combined) | 0 |
| 30-99 | 59 | −53.5% | 8 | (included above) | 0 |
| 100+ | 29 | −99.0% | 1 | (included above) | 0 |

The **O_LC comparison** (opt 17): 1,781 of its 2,094 genomes with 100+ trades scored fitness > 0.

**Signal-mode split.** The `cond:o_lp-signal:mode` gene was still searchable:

| mode | genomes | meaning |
|---|---|---|
| below | 320 | puts on SELL, the intended arm |
| off | 293 | puts on BUY or SELL |
| above | 194 | puts on BUY only |

So 60% of O_LP genomes were not bearish-gated at all.

**The top-return genome** (+1,163.9%, 66 trades) is a mode-off genome.
- 101 of its 109 submitted entries came from BUY recommendations.
- Two 2025 puts made the book: WMT in March (+$99.9k on 290 contracts) and ETN in January (+$82.4k).
- The top five trades are 101% of net profit, so the concentration factor is 0 and the fitness is 0.

### 2.2 Stage by stage (all 807 genomes)

| stage | measured | where it dies |
|---|---|---|
| **1. DS signal + confidence gate**, on the genome's schedule days | median supply per genome: **3,908** decision points (mode below), 9,518 (off), 10,964 (above). Zero supply: 7 genomes, all weekend-only schedules (the weekend genes are still in the space). | Not here. |
| **2. The genome's market-condition gates**, emulated from the ta-structure-v1 and ohlcv-v1 store (unknown never passes) | Median survivors: **8** (mode below), 0.25% of supply. 73 of 320 below-genomes keep **0**. 10 genomes carry a gate that can never fire (`dist-resistance below 0.0` ×7, `dist-support below 0.0` ×3). | **Main killer.** Spearman correlation with actual trades: 0.82 for survivors, 0.17 for raw supply. |
| **3. Residual**: the has_no_position guard, the IV rank / relative volume / IV-RV / expected-profit gates, sizing, fills | trades / survivors, median 0.10 | A 10× cut. |
| **Result** | median **1 trade**; 285 genomes with 0 | 162 of the 285 zero-trade genomes already had 0 survivors at stage 2. |

**Why stage 2 is so lethal.**
- Each of the 8 market-gate mode genes is off/below/above, so a random genome switches about 2/3 of
  them on. Measured: 5.43 gates on average per genome; 184 genomes carry 7-8.
- Their directions and thresholds are random. A sample of 3,000 random genomes over the gate
  ranges gave:
  - median firing rate 0.049% of symbol-days;
  - 27% never fire at all;
  - 85 gate pairs are individually common (≥ 5%) but jointly below 0.5%, for example
    `chan-pos < 0.1 AND vs-prior-high > −0.25` at 0.003%.
- This is measured before the gates are ANDed with the SELL signal.
- **Consequence:** the GA's initial population is almost all non-trading. Generations 1-5 found no
  positive final fitness, and early stopping (patience 5, compared against 0.0) ended the job.

### 2.3 DS SELL supply (mirror, current code)

SELL symbol-days per year. Confidence gate 40 means |final| > 0.40, which overrides any `theta_sell`
at 0.4 or below: 395 of the 807 genomes had the gate on, so their `theta_sell` gene was inert.

| weights | macro | gate | total | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | symbols |
|---|---|---|---|---|---|---|---|---|---|---|
| default .5/.3 | multiply+mirror | 40 | 1,561 | 648 | 0 | 724 | 83 | 0 | 106 | 36 |
| default .5/.3 | multiply+mirror | none (θ .2) | 9,577 | 3,848 | 111 | 3,383 | 1,185 | 221 | 829 | 80 |
| default .5/.3 | off / gate | 40 | 7,795 | 3,043 | 497 | 1,818 | 1,339 | 407 | 691 | 59 |
| tech .8 | off / gate | 40 | 12,884 | 2,985 | 374 | 5,441 | 2,056 | 620 | 1,408 | 94 |
| fund-heavy .1/.7/.2/.3 | multiply+mirror | 40 | 2,074 | 998 | 3 | 780 | 127 | 1 | 165 | 51 |

**The mirror works as designed.**
- 2022 SELL bars at gate 40, default weights: 724 with mirror, against 142 with "same".
- In the 2020-02-19..03-23 crash window: 118 with mirror, against 20 with "same".
- It makes supply bear-regime-only: 2021 and 2024 have none.

**Supply is concentrated.** Under mirror at gate 40:
- 60 of 96 symbols never SELL.
- The top ten names hold 63% of SELLs: GE 177, CRWD 171, MRVL 148, ORCL 84, WDC 80.
- **The signal is still not the bottleneck:** mode-below genomes see a median of 3,908 SELL decision
  points.

### 2.4 Control backtests (every optional gate off, confidence > 40, template exits, Mon-Fri)

Same engine as the grid (post-`3b3465c8` fixes).

| control | SELL recs | → after the confidence and flat gates | refused (budget / no liquid put / empty chain) | orders → filled | trades | total | 2020 | 2022 | Feb-Mar 2020 entries | 2022 bear entries (Jan to mid-Oct) |
|---|---|---|---|---|---|---|---|---|---|---|
| default, multiply+mirror | 9,557 | 662 | 154 / 134 / 50 | 231 → 120 | 120 | **−55.5%** | −20.0% | −9.3% | 12 entries, +$5,078 | 36 entries, +$56 on $17.8k premium |
| default, macro off | 20,231 | ≈2,370 | 2,411 / 626 / 294 | 695 → 405 | 405 | **−94.2%** | −66.7% | −15.1% | 16, +$6,535 | 56, −$536 on $8.6k |
| tech .8, macro off | 26,974 | ≈3,940 | 6,195 / 914 / 424 | 869 → 509 | 509 | **−95.6%** | −55.3% | −55.0% | 17, +$3,315 | 136, −$2,732 on $21.2k |

**Two post-gate leaks:**
- 42-48% of entry DAY orders expired unfilled.
- "Insufficient budget" refusals grow as the account shrinks.

Neither is the reason these books lose: the entries they do fill lose money.

### 2.5 Fitness floor (why the 110 positive genomes scored 0)

110 genomes had positive raw fitness: 87 with ≤ 5 trades, 16 with 6-9, 7 with ≥ 10. All 110 were
zeroed by the robust factor. The two reruns show it: p_top_below has top5 = 100% and conc_factor 0;
p_best_off_66 has top5 = 101% and conc_factor 0.

A book of ≤ 5 structures is 100% concentrated by definition. Measured here, 6-9-trade winners are too.

**The best SELL-gated genome is one event.** p_top_below: 3 trades, +334%. All three puts were entered
on 2020-02-28 and expired 2020-03-20 (BUD, CVX and WFC), +$66.9k in total. It is the crash, not a
strategy.

**Verdict for Q1:**
1. **The gates.** GA-sampled market-condition gates kill 99.75% of the supply.
2. **The economics.** With the gates removed, puts on DS SELL lose money: −55% to −96% over
   six years. Only the February-March 2020 entries pay, and 2022 is flat to negative.
3. **The fitness screen.** It zeroes the few surviving books, which are crash-lottery tickets.

Sizing and fills are real but secondary leaks.

## 3. Q2: do profitable bearish trades exist?

**Unconditional puts.**
- Setup: every name, first trading day of each month, 6,984 events, 6,520 priced.
- Result at hold 20, 5% OTM: book **−27.6%** (mid −19.2%), win rate 21.6%.

| year | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|
| hold 20, 5% OTM | −42% | −48% | **+46%** | −38% | −40% | −39% |
| hold 40 | −38% | −65% | **+78%** | −56% | −50% | −40% |
| hold 20, ATM | −34% | −38% | +42% | −27% | −32% | −28% |
| hold 20, 10% OTM | −52% | −58% | +42% | −54% | −51% | −50% |

**Positive months** (hold 20): 17 of 71.
- 2020: 2020-02 +73%, 2020-03 +105%.
- 2022: 01 +76%, 02 +62%, 04 +282%, 06 +111%, 09 +18%, 12 +55%.
- Other years: 2023-09 +124%, 2024-07 +65%, 2025-02 +50%.

2020 as a whole is still −42%: the April-August 2020 entries lost 64-92% each month.

**So the expectation holds.** Puts paid in the 2020 crash and through 2022, and on single-name
drawdowns such as META in January 2022: +508% ask to bid on one contract.

The problem is timing. A put book has to be on only in those windows, and no oracle-free rule below
switches on in time.

**Oracle-free price rules.**

| rule | episodes | 20d hit | 20d xs | 60d xs | put book h20 | put book h40 | 2022 (h20) | ex-top-5 (h20) |
|---|---|---|---|---|---|---|---|---|
| R1 close < SMA200 & 20d-low break | 1,581 | 43.3% | +0.04% | −0.06% | −33.9% | −55.0% | −18% | −48.1% |
| R2 R1 & SMA50 < SMA200 & SMA50 falling | 703 | 42.5% | +0.29% | +0.88% | −37.8% | −59.7% | −38% | −49.5% |
| R3 first cross below SMA200 | 1,296 | 43.8% | +0.01% | −0.82% | −17.3% | −19.9% | +48% | −39.4% |
| R4 relative strength 6m, bottom decile (monthly) | 602 | 46.3% | −0.32% | −0.06% | −17.3% | −11.3% | +46% | −33.3% |
| R5 relative strength 12-1, bottom decile (monthly) | 594 | 48.8% | −0.21% | +0.78% | −19.8% | −26.3% | +39% | −34.2% |
| R6 R4 & < SMA200 | 558 | 47.1% | −0.46% | −0.35% | −12.2% | **−2.3%** (mid +7.6%) | +46% | −30.1% |
| R7 20d-low break | 2,533 | 42.6% | +0.03% | +0.03% | −35.7% | −51.5% | −20% | −47.2% |
| R8 SPY < SMA200 & R1 | 756 | 47.2% | +0.18% | +0.29% | −19.9% | −59.2% | −24% | −42.1% |
| R9 SPY 20d-low break & SPY < SMA200 → puts on all 97 | 1,164 | 48.6% | 0.00% | 0.00% | −25.8% | −70.4% | −61% (2020: +99%) | −36.0% |
| R10 drawdown > 25% & 20d-low break | 781 | 39.7% | +0.50% | +1.53% | −50.1% | −60.3% | −45% | −58.8% |

**Verdict for Q2:**
- **Breakdown rules are worse than random.** They arrive after the move, and these large caps
  mean-revert: R10's 60d excess is +1.53%.
- **Relative-strength-bottom rules come closest** (R6 at h40, −2.3%). But without their top five
  trades they are at −29%, and they are profitable only in 2020 at h40 and in 2022.
- **Assumption / caveat:** the universe is today's large caps. Survivorship tilts every bearish test
  toward names that recovered.

## 4. Q3: candidate bearish signal sources

Same method throughout. "Put" is the h20 book unless marked h40. Full per-variant and per-year tables
are in the result files (§8).

| # | source | best variant (definition) | episodes | 20d hit | 20d xs | 60d xs | put book | verdict |
|---|---|---|---|---|---|---|---|---|
| a | DS lower threshold | SELL final < −0.2, tech .8, macro off | 1,758 | 43.3% | −0.18% | −0.40% | −19.1% (2022 +5%) | no edge |
| a | DS confidence > 40 | default, macro off | 520 | 43.5% | −0.28% | −0.21% | −27.2% | no edge |
| a | DS confidence > 40, mirror | default, multiply+mirror | 144 | 41.0% | −0.33% | **−1.98%** | −15.8% (mid +2.2%); ex-top-5 −56% | too few; carried by 2020 |
| a | DS not-BUY & downtrend | final < 0.3 & close < SMA200 & SMA50 falling | 2,219 | 41.6% | −0.02% | −0.36% | −30.9% | no edge |
| a | DS cross-sectional bottom decile (monthly) | default, off/gate | 598 | 47.0% | **−0.86%** | **−0.89%** | not measured | weak relative underperformance |
| b | ta-structure-v1 / ohlcv-v1 | slope50 < −0.20 ATR | 126 | 50.8% | −1.58% | −0.22% | +1.5% (99 priced; ex-top-3 −33.6%) | too rare, concentrated |
| b | market structure | enters "bear" & slope < 0 | 1,604 | 42.6% | +0.01% | −0.35% | −40.1% | no edge |
| b | support broken / fresh break | — | 715-751 | 42% | +0.3 to +0.5% | +0.9 to +1.0% | — | contrarian-bullish |
| c | FactorRanker (its own scoring code, point in time) | composite bottom decile | 592 | 43.1% | **+0.87%** | +3.77% | −33.0% | inverted: bottom beats universe |
| c | FactorRanker | momentum bottom decile & < SMA200 | 424 | 50.0% | +0.05% | +0.66% | −9.6% / −1.1% (h40); ex-top-3 −25% | no edge |
| d | earnings miss (FMPEarningsDrift surprise formula) | EPS miss & revenue miss | 184 | 43.5% | **−1.36% (t −2.8)** | −1.60% (t −2.4) | −20.9% / −6.6% (h40) | best edge, but relative only |
| d | earnings miss | any EPS miss | 359 | 46.0% | **−1.03% (t −2.3)** | −1.09% | −13.8% / −0.7% (h40); ex-top-5 −37.8% | best edge, but relative only |
| e | analyst downgrades | ≥ 2 firms within 20d | 246 | 44.7% | +0.16% | −0.57% | −30.3% / −55.4% | no edge |
| e | price-target cuts | cut ≥ 20% (same firm) | 175 | 41.7% | +1.49% | +3.07% | — | contrarian-bullish; no 2020 data |
| f | insider selling clusters | ≥ 2 sellers, 30d, ≥ $200k (sales only) | 1,493 | 43.3% | −0.13% | +0.09% | −19.3% / −25.4% | no edge |
| g | index regime | SPY < SMA200, first cross (9 episodes) | 9 | SPY 55.6% | SPY 20d −1.54% | SPY 60d +4.04% | no index chains | too rare, mean-reverting |
| h | HOLD as put (contrarian) | DS \|final\| ≤ 0.2 | 3,948 | 43.2% | −0.23% | **−0.81%** | not measured | mild relative underperformance |
| h | BUY as put (contrarian) | DS final > 0.3 | 2,638 | 44.7% | +0.07% | +0.22% | opt 18 "above" arm: 194 genomes, median −15.3%, 21 positive | no edge |
| — | **control: unconditional puts** | monthly, all names | 6,984 | — | — | — | **−27.6%** (2022 +46%) | the benchmark every row must beat |

Notes per source:

- **(a) DS:**
  - No DS threshold, mode or composite produces a negative raw 20d mean. Every variant has a
    positive mean forward return (+0.95% to +3.57%).
  - Lowering `theta_sell` adds supply, not edge.
  - The cross-sectional bottom decile (−0.9% xs) is the only DS form with a persistent sign, and it
    is a relative signal.
- **(b) Technical / market-condition features:** they describe the past 20-60 days and carry about
  zero excess. Support breaks are followed by bounces.
- **Market-gate firing rates (b):**
  - `dist-support < 0` and `dist-resistance < 0` fire on exactly 0% of symbol-days: the distances are
    always ≥ 0, and a broken support is stored as *unknown*.
  - 22 other gate settings fire on more than 90% of days, so they are effectively off.
- **(c) FactorRanker:**
  - Its bottom decile is dominated by MCD (58 of 72 months), PM, DELL and BA. MCD and HD have
    negative equity, so their ROE term ranks them last on quality.
  - The ranking carries no downside information on these names: the top decile also beats the
    universe (+0.70%).
- **(d) Earnings:**
  - Coverage is 97 of 97 names.
  - This is the only source with a statistically real negative drift. But the raw mean stays positive
    (+0.6% to +0.8%): the names lag, they do not fall.
  - Adding a negative day-1 reaction makes the 40d put book worse (−54% to −57%).
- **(e) Analysts:**
  - Price-target data starts in 2021-04, so there is no 2020 test.
  - Big cuts and downgrades to sell are followed by outperformance of +1% to +3% over 60 days.
- **(f) Insiders:**
  - 8 names have no data (ASML, NVO, NVS, RIO, RY, SAP, SHEL, UBS).
  - 10b5-1 plans are not identifiable in the data.
  - Large-cap selling carries no information.
- **(g) Index level:**
  - The ThetaData cache holds 856 underlyings and **no SPY, QQQ or IWM chains**. The old
    `options_history.sqlite` has SPY, QQQ and IWM daily option bars only for 2024-02-01..2026-07-06,
    with no quotes. **Index puts cannot be tested in 2020 or 2022 with local data.**
  - Index regime triggers are too rare: 9-25 episodes in 6 years. SPY is higher 60 days later
    after every trigger measured: mean +4.0% to +5.0%.
- **(h) HOLD / BUY as a put trigger:** no edge. The HOLD bucket's −0.8% 60d xs mirrors BUY
  outperformance; it is not a crash signal.

## 5. Q4: structural fixes to the grid

### 5.1 Does `option_car_target_soft30` disfavour low-frequency bearish strategies?

Yes, in four independent ways. Each was read from the code and confirmed on opt 18:

1. **Concentration.** A book of ≤ 5 structures has top5 = 100% by definition, so conc_factor = 0
   (`robustness_metrics`, option-structures branch). 6-9-trade winners in opt 18 also scored 0.
2. **Trade ramp.** `trade_gate = min(structures / 30, 1)` over the whole window. A 6-trade crash
   hedge keeps 20% of its score before any other factor.
3. **Consistency.** `worst_year / mean_year` is floored at 0.25. A hedge by construction loses in
   most years.
4. **Negative CAR.** A negative CAR is returned unfactored, so a hedge that costs money on its own can
   never score above 0, however much it would cut the drawdown of a combined book.

**Standalone, the metric cannot recognise a hedge.** The unconditional put overlay measured in §3
earns about −4.5% to −5.4% of equity per year in bull years and +3.8% in 2022. That uses 1% of equity
of premium per month and a simple, non-compounded sum; **Estimate**. On its own it is a negative-CAR
strategy that soft30 scores below 0 by design. Whether it is worth having depends on the combined
equity curve, which this metric never sees.

As a hedge, single-name puts are also weak: +3.8% in 2022 against SPY −19.5%. Index puts would hedge
better, but they cannot be priced for 2020-2022 without new data (§4g).

### 5.2 The grid mechanics that made O_LP untestable, independent of the metric

| issue | measured effect | fix |
|---|---|---|
| Signal mode off/below/above on a bearish arm | 60% of O_LP genomes not SELL-gated; the top genome buys puts on BUY | Pin `mode=below` for O_LP, O_BEARCS, O_LEAPP, O_PBS and O_CONVEXP. Use a separate, explicit contrarian job if wanted. |
| 8 market gates with 2/3 on-probability | 5.43 gates on per genome; 0.25% survival; 27% of random gate genomes never fire | Default each gate off with a low on-probability, or cap the number switched on (e.g. ≤ 2). Or seed the initial population gates-off. |
| Dead gate values | `dist-support/-resistance below 0.0` fire 0% (10 genomes); 22 settings fire > 90% | Start the dist-* "below" range at 0.5. Trim the range ends that fire on < 1% or > 90% of days. |
| Weekend schedule genes | 7 genomes with 0 decision points | Remove saturday/sunday (already recommended on 2026-09-24; still in the space). |
| Confidence gate makes `theta_sell` inert | 395 of 807 genomes | Make the confidence gate relative to `theta_sell` (e.g. margin above it), or drop one of the two genes. |
| Early stop against 0.0 | stopped at generation 5 with all fitness ≤ 0 | Do not early-stop a job whose best fitness is still ≤ 0. Flag it as untestable instead. |

### 5.3 Is a combined bullish/bearish style (stage 2) the right place for puts?

Yes, with conditions:

- **Score on the combined book.** A put arm earns its place only if it raises combined CAR/DD, so
  score the combined equity curve (soft30 on the total book is fine). The concentration screen must
  run per book, not per arm.
- **Measure the edge before paying for a grid.** The only measured single-name edges are relative
  (earnings misses, DS bottom decile, relative-strength bottom decile). Relative edges fit as a
  **filter on the bullish book** (do not buy calls on those names) or as a paired short leg, not as
  outright puts.
- **Get index chains.** A regime put overlay needs SPY/QQQ ThetaData chains for 2020-2025 before it
  can be tested at all.

## 6. Ranked recommendation (cheapest credible path first)

1. **Stop spending grid time on standalone bearish single-name structures under DS on this
   universe.** Skip O_BEARCS now, and hold O_LEAPP, O_PBS and O_CONVEXP. Cost: nothing. Every
   measured signal family loses money on puts, and the metric zeroes the rest.
2. **Use the measured relative signals as a filter in the bullish book (stage 2).** This needs no new
   structure.
   - Candidates: earnings miss (EPS and revenue) −1.4% 20d xs; DS cross-sectional bottom decile
     −0.9%; relative-strength-6m bottom decile & < SMA200 −0.5%.
   - Test: add "no entry within N days after an EPS miss" or "not in the bottom decile" to O_LC, then
     compare against the O_LC TOP-N. **Assumption:** removing the laggards helps a call book; to be
     measured.
3. **Backfill SPY/QQQ(/IWM) ThetaData chains for 2020-2025, then test a regime-gated index put
   overlay inside stage 2.** It is the only hedge shape not yet measured, because the data is
   missing. **Estimate** of the stakes: single-name put overlay +3.8% in 2022 against about −5% per
   year in bull years.
4. **Only if bearish arms are rerun, fix the gene space first** (§5.2): pin `mode=below`, sparse
   gates, no dead values, no weekend genes, no early stop at 0. Score them in the combined stage-2
   book, not with standalone soft30.
5. **Do not pursue** these as put triggers: analyst downgrades or target cuts, insider-selling
   clusters, FactorRanker bottom ranks, market-structure breakdowns, or a lower DS `theta_sell`. All
   are measured at no edge or a contrarian edge (§4).

## 7. O_BEARCS (stage-1 job 5 of 15): skip

**Recommendation: skip it.** Do not run it as is, and do not run it with changed settings in stage 1.

- **Same funnel.** O_BEARCS uses the same bearish entry tree as O_LP: signal-mode gene (off/below/
  above), shared confidence gate, the same 8 market-condition gates. Expect the same result: most
  genomes trade 0-9 times, 60% are not SELL-gated, and robust soft30 zeroes the thin books.
- **Negative economics.**
  - Setup: a bear call credit spread, short leg about 8% OTM and long leg one strike higher, held to
    the last bar before expiry.
  - Unconditional monthly result, return on risk: **−14.9%** ask/bid and **−7.1% at mid**
    (4,906 spreads).
  - Per year at mid: 2020 −9.8%, 2021 −4.7%, **2022 +4.5%**, 2023 −11.9%, 2024 −8.8%, 2025 −12.6%.
  - At 4% OTM: −20.3% (ask/bid); at 12% OTM: −12.7%. Even 2022 is negative at ask/bid (−0.8% to −6.2%).
  - Gated on the bearish price rule R1: **−24.9%**, worse than random, and negative in every year.
  - Adjacent strikes give no credit at all on 14-35% of attempts (the action refuses those).
  - **Assumption:** the engine's `pow-2026-09-22` spread model fills somewhere between the mid and
    ask/bid figures, and both are negative.
- **Grid time.** O_LP took 9.2 h on the same host (2026-09-27 20:12 → 09-28 05:26). Skipping saves
  about that much for the neutral/credit jobs behind it (O_BF, O_IC, O_JL, ...). **Estimate.**
- **How to skip.** `run_options_matrix.py` stops the whole campaign on a failed job unless the job
  wrote a no-measurement marker, so a mid-job cancel stops the matrix. Restart the driver **at a job
  boundary** (after O_BULLPS completes, before O_BEARCS starts) with O_BEARCS removed from
  `--strategies`. Completed jobs are skipped idempotently. Do not bump `version.py` while the
  campaign runs.

## 8. Reproduction

All scripts and raw outputs are in the session scratchpad,
`C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad\bearish_0928\`:

| file | contents |
|---|---|
| `bearlib.py` | panel, event study, put simulator |
| `probe_ds_sell_now.py` + `ds_sell_now/rows` | DS section scores |
| `ds_analysis.py` → `ds_sell_named_mirror.csv`, `olp18_genome_supply.csv` | DS supply |
| `gate_emul.py` → `olp18_gate_funnel.csv` | per-genome gate funnel |
| `probe_funnel_now.py`, `probe_funnel_noaph.py` → `funnel/`, `funnel_noaph/` | backtest reruns and controls |
| `q2_events.py` → `q2_events.json`, `q2_puts.py` → `q2_puts.json` | rules, DS variants, index |
| `bcs.py` → `bcs_results.json`, `bcs_mid.py` → `bcs_mid.json` | bear call spreads |
| `agentA/results.json` | (d)(e)(f) + unconditional put control |
| `agentB/results.json` | (b)(c) + gate firing rates |
| `remote_dump.json` | opt 14-19 rows from remote227 |

The DS probe and the funnel probe are copies of `test_files/probe_ds_sell_rate_20260924.py` and
`test_files/probe_olp_funnel_20260924.py`, changed only in their output and genome paths.
