# DeterministicScorer and bearish option structures: findings (2026-09-24)

Why does the stage-1 O_LP job (DeterministicScorer, long put) score fitness 0 with one-trade top genomes?
**Status: diagnosed.** Everything below is measured. Section 5.1 contains four ENGINE and METRIC bugs
that affect every option job, not only O_LP. Nothing below has been changed in code: the stage-1 grid is
running, and changing the scorer or the gene space mid-run would change genomes under it.

Evidence:

- **Probe:** `test_files/probe_ds_sell_rate_20260924.py`.
- **Probe coverage:** the real as-of backtest path, on every bar (96 symbols, 2020-01-01..2025-12-31, 141,106 bars).
- **Probe outputs:** the session scratchpad `ds_sell/`.
- **Grid data:** job 2 of the stage-1 relaunch (`strategy_optimizations.id = 16`, remote227). 581 genomes had been evaluated by generation 5.

## 1. SELL supply is not the problem

DeterministicScorer emits plenty of SELLs on this universe. The confidence gate below is the entry
rule's `confidence >` gene.

| weights | macro_mode | θ_sell 0.1 | θ_sell 0.2 | θ_sell 0.3 | θ_sell 0.4 | SELL bars with confidence > 40 (6 y) |
|---|---|---|---|---|---|---|
| default (tech 0.5 / fund 0.3) | multiply | 17.3% | 8.7% | 4.2% | 1.8% | 2,502 (37 symbols) |
| default | off / gate | 21.4% | 14.4% | 8.9% | 5.5% | 7,795 (59 symbols) |
| technical 0.8 | off / gate | 24.7% | 19.2% | 14.1% | 9.1% | 12,884 |

Every one of the 1,799 weight combinations the GA can reach, under every macro mode, yields strong
SELLs. The worst case is analyst-only under multiply: about 107 bars/yr with confidence > 40.

No code path blocks a SELL from reaching an option entry:

- the backtest keeps SELL recommendations (`daily_engine.py:325` drops only HOLD/SKIP/ERROR);
- option entries size themselves (`daily_engine.py:1067`);
- `enable_sell` only filters equity orders in `TradeRiskManagement.py`.

## 2. `macro_mode=multiply` suppresses bearish signals in bear regimes

**Where:** `combine.py:154`, `macro.py:159` (`exposure_multiplier`).

**What it does:** multiply mode scales the final score by `m_floor + (1-m_floor)*(regime+1)/2`, and
by 0 in a hard risk-off. The multiplier was designed to cut LONG exposure in a bad regime, but it
shrinks negative scores too. SELLs are therefore damped exactly when the market falls.

Share of bars scoring below −0.4 (a SELL that passes a confidence-40 gate):

| weights | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|
| default, before macro | 13.1 | 2.1 | 7.7 | 5.7 | 1.7 | 2.9 |
| default, multiply | 3.7 | 1.3 | **0.6** | 2.7 | 1.0 | 1.4 |
| technical only, before macro | 12.9 | 1.6 | **23.1** | 8.7 | 2.6 | 5.9 |
| technical only, multiply | 3.4 | 1.2 | **0.9** | 3.5 | 1.3 | 3.3 |

The 2022 regime averaged −0.23, so the mean multiplier was 0.54. The hard risk-off never fired: the
regime never went below −0.75.

**Proposed fix:** make the multiplier direction-aware. Scale the long side by `m(regime)` and the
short side by `m(-regime)`, so a bearish regime amplifies SELL conviction instead of muting it.
Alternatively, restrict bearish structures (O_LP, O_BEARCS, O_LEAPP, O_PBS) to `macro_mode ∈ {gate, off}`.
Either way it changes behaviour, so it needs a fitness re-baseline.

## 3. Confidence ceiling constant is stale

**Where:** `_EXPERT_CONFIDENCE_CEILING["DeterministicScorer"] = 50` (`ba2test_launcher.py:5768`).

The ceiling rests on a 2023-only measurement (max |final| 0.562). Over 2020-2025 the measured max is
0.79 (default weights, macro off) and 0.91 (fundamental-heavy). The cap is still safe, since gates of
40-50 are all reachable, but its justifying comment and the combine.py header should be updated.
Consider raising the cap to 70.

## 4. Altman-Z veto misfires on financials

**Where:** `combine.py:110` (`apply_vetoes`) and the fundamental section's distress veto.

The veto caps the score at 0. It fires on 28% of bars overall, and on more than half the bars of 25
symbols: mostly banks, insurers and other financials, where Altman Z is not a valid distress model.
Examples: JPM, BAC, GS, C, WFC, MS, HSBC, AXP, T, VZ, BA, GE.

**Effect:** those names can NEVER score BUY under DeterministicScorer. This hurts O_LC and every
bullish job, not O_LP (a veto keeps negative scores).

**Proposed fix:** skip Altman Z for financial-sector symbols (sector from company profile), or
replace it with a sector-appropriate distress check.

## 5. Why O_LP barely trades: diagnosis in progress

Grid state at generation 4-5 (479 genomes):

| trades | genomes | median return | best | profitable |
|---|---|---|---|---|
| 0 | 255 (53%) | 0 | n/a | n/a |
| 1-9 | 146 | −2.9% | +268% | 28 |
| 10-29 | 48 | −15.3% | +153% | 7 |
| 30-99 | 14 | −33.7% | +174% | 4 |
| 100+ | 16 | −87.8% | +69% | 2 |

Observations (causes still being verified):

- **Stacked gates.** Each O_LP genome carries about 12 optional entry gates on top of the signal and
  confidence gates: IV rank, relative volume, IV/RV, expected profit, and 8 market-condition gates.
  A random early population ANDs many of them, and that is the leading suspect for the zero- and
  one-trade genomes.
- **Maximum drawdown −100% with a positive total return.** `best_ret_30_99` (+174%, 42 trades) and
  `dd100_pos_ret` (+69%, 287 trades) both show this. An account cannot recover from exactly 0, so
  either the drawdown metric is wrong for option runs (mark-to-market, cash vs. equity) or the equity
  curve passes through a bogus zero. Such genomes get the bust sentinel (−2e9) and drop out of the
  search. That would be a GA-selection bug, not just a display bug.
- **Expected economics.** Long puts should make money in the February-March 2020 crash and in 2022.
  A no-gates control run (macro off, θ_sell 0.2, confidence 40) will say whether they do.

### 5.1 Diagnosis results

The diagnosis re-ran 6 GA genomes and one control locally. Probes: `test_files/probe_olp_funnel_20260924.py`
and `test_files/probe_olp_report_20260924.py`; outputs in the scratchpad `olp_diag/`. Every genome
reproduces its GA record exactly: trades, total return, max DD and fitness all match.

**Engine / metric bugs (they affect ALL option jobs, not only O_LP):**

1. **An option held across a stock split is settled and marked wrongly.**
   - **Mechanism:** the contract keeps its pre-split strike, while spot is converted with the
     settlement day's split factor (`daily_engine.py:1664`). Intrinsic value is computed at
     `backtest_account.py:4572/4618`, and daily marks come from `_option_spot_asof` at `:3913`.
     Puts show huge fake gains; calls show fake losses.
   - **O_LP: every "best return" genome is a split artifact.** Five puts that really expired
     worthless were booked as gains:

     | Contract | Split | Fake P&L |
     |---|---|---|
     | AAPL, 3 puts | 4:1, 2020-08-31 | +$27.7k, +$57.2k, +$27.4k |
     | ANET | 4:1, 2024-12-04 | +$51.0k |
     | PANW | 2:1, 2024-12-16 | +$30.3k |

     Corrected returns: +268% → about −20%, +153% → about +13%, +174% → about −83%, and +69%
     → account wiped out.
   - **O_LC** job-1 TOP-N: 2-3 split-crossing calls per run (NVDA 2021, PANW 2024, APH 2024), all
     fake LOSSES of −$3.5k to −$5.2k against $250-320k of P&L. So O_LC is not inflated by it.
   - **Fix:** adjust the contract at the split (strike ÷ k, multiplier × k), or mark it against the
     contract's own pre-split basis.
2. **The max-drawdown "intraday refinement" is wrong for option runs.**
   - **Mechanism:** `results.py:631` → `intraday_drawdown.py:158/164` adds (worst intraday P&L −
     realised P&L) to the run's maximum drawdown. For a WINNING trade, the realised gain is counted
     as extra drawdown.
   - **Effect:** −100% on positive-return runs, which hits the bust sentinel (−2e9) at
     `strategy_fitness.py:1778`. Every refined DD is inflated; for example, best_ret_10_29 reports
     −20.15% where the equity curve shows −9.74%.
   - **Consequence:** the GA's drawdown penalty and wipe-out sentinel act on a wrong number in every
     option job, and the O_LC TOP-N max DDs (about −30 to −37%) are overstated too.
   - **Fix:** measure the dip against entry equity and the running peak. Until then, rank on the
     daily equity-curve drawdown.
3. **Held options do not count as "activity"** (`daily_engine.py:818-823`, the skip at `:806`).
   - **Mechanism:** `_has_activity` checks `get_positions()`, which excludes option positions. A
     run holding only options therefore jumps from entry day to entry day.
   - **Effect:** exits are not evaluated on the days in between. The equity curve is sampled only on
     entry days, and expiry settles late (AAPL 8/28 settled 8/31, after the split).
   - **BT/live PARITY BREAK:** the deployed 8082 genome enters Mon/Tue/Fri, but live evaluates exits
     Mon-Fri, so its backtest never evaluated a Wednesday or Thursday exit.
   - **Fix:** count option positions in `_has_activity`, and re-check every option run with an entry
     schedule that is not every weekday.
4. **Low-trade fitness loophole** (`strategy_fitness.py:1043`).
   - **Mechanism:** with fewer than 2 trades, the concentration and Monte Carlo factors stay at 1.0,
     while 2-5-trade genomes get concentration 0.
   - **Effect:** that is why every top O_LP genome has exactly 1 trade.
   - **Fix:** apply the low-trade sentinel below about 10 completed trades.
5. **Weekend schedule genes are dead** (`strategy_param_space.py:945-949` only repairs "all days
   off"). A Saturday-only genome gets 0 decision points.

**Why O_LP barely trades: the entry funnel.** The main blockers, in order:

1. **Stacked optional gates.**
   - `shared-rel_volume` (`ba2test_launcher.py:4399`, range 0.5-3.0): SELL bars have a median
     relative volume of 0.88, so a genome with rel_volume > 2 loses 97% of the remainder.
   - `iv_rv < 0.8` drops 79% where enabled.
   - `iv_rank` fails closed when IV rank is unavailable (`TradeConditions.py:2768`), mostly in
     2020-2021.
   - The GA can set contradictory market gates, e.g. slope < −0.3 AND ADX < 15.
   - Signal mode "above" makes O_LP buy puts on BUY signals.
2. **Sizing refusal** (`TradeActions.py:3526`, `_size_by_cost` at `:2712`). The budget is 1-10% of
   equity divided by (premium × 100), so a put costing $6 or more rounds to 0 contracts on names like
   CRWD, BA, AMD, UNH, NFLX and META.
3. **Unfilled day orders.**
   - Fills are capped at 10% of the bar's volume (`backtest_account.py:313/2907`), while the selector
     only requires volume ≥ 25 (`option_selector.py:249`). That means at most 2.5 contracts per
     order, against a sizing of up to 180.
   - Day-only limits expire (`:2561`). With next-open fills, a buy limit fills only when the put got
     cheaper overnight, which selects against the days the thesis works.
   - Exits suffer the same way, so positions ride to expiry.
4. **Empty option chains.** SHEL, TTE, SAN, SMFG, MUFG and UBS have empty or illiquid chains (mostly
   2020).

**Control run (were puts profitable in 2020 / 2022?).** Setup: all optional gates off,
macro_mode off, default weights, θ_sell 0.2, confidence > 40, template exits, Mon-Fri entries.

| 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | total |
|---|---|---|---|---|---|---|
| −53.0% | −17.2% | −8.1% | −64.0% | −4.2% | −12.2% | **−89.2%** (408 trades) |

- **Only the crash window paid.** Entries from 2020-02-19 to 03-23 made +$5.9k (+69% on premium),
  but there were only 12 of them because the SELL signals lag.
- **The 2020 rebound entries lost −$17.2k.** The 2022 bear (Jan to mid-Oct) was flat: +$0.6k on
  $17.7k of premium.
- **Exits:** 253 of 408 exits were the days-to-expiry ≤ 21 rule (median hold 8 bars), because the
  template enters at 25-45 DTE and exits at 21. The DTE exit should be relative to the entry window.

**Conclusion.** With this expert's SELL timing, long puts are not profitable even across the bear
phases; only the February-March 2020 crash paid. O_LP's apparent winners are split artifacts, and its
losers are partly metric artefacts. The engine bugs (1-4) distort GA selection in EVERY option job,
not just O_LP.

**Recommended gene and range changes (for the relaunch):**

- remove the weekend schedule genes;
- cap `rel_volume` at 1.5 or less;
- set the `iv_rv` lower bound to 1.0 or more;
- pin the bearish signal mode to "below";
- set `option_entry_cross` to 0.75-1.0;
- tie `min_volume` to about 10× the planned contracts, or cap the size at 10% of bar volume at
  sizing time;
- give `option_sizing` a floor of about 3%, or allow 1 contract up to the per-instrument cap;
- make the DTE exit relative to the entry window;
- drop SHEL, TTE, SAN, SMFG, MUFG and UBS from option universes.

## Decision rule (user, 2026-09-24)

Let O_LP run to about generation 10. If no viable genome appears, hold all put and bearish jobs until
the cause is understood and fixed.
