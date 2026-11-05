# Stage-1 option grid: what the GA converged on, per strategy

Source: `dl_forecasting.db` (read-only), final stage-1 optimizations 117,119-122,124-133 (BULLPS = old assignment rule opt 120; re-run 134 ignored). Rows used: TOP1..TOP5 (GA top-N re-runs) and DTOP* (distinct top-N); 5-12 rows per strategy (O_STRD has TOP1/2/5 + DTOP6/9/10; O_STRG, O_CC, O_PP, O_LP, O_VERT have only TOP1-5).

## How to read the tables

- Columns T1..T5 = TOP1..TOP5 values; `Â·` = gene off / inactive (gate disabled, market-gate mode `off`, strike gene not used by the chosen strike method). `DTOP` = summary of the distinct-top rows; `range` = searched [min,max]; `verdict` is computed over ALL rows of that strategy.
- Gate rows show the effective comparison `(>)`/`(<)` taken from the stored entry/exit rules; market gates show direction+threshold (`>0.05` = field above 0.05). Market-condition fields: slope = 50-day trend slope in ATR units (`underlying_trend_slope_50_atr14`); adx = ADX14; rv = 5d/20d realised-vol ratio (>1 = vol expanding); dist-support / dist-resistance = distance to nearest swing support/resistance in ATRs; chan-pos = position in the 20d channel (0 bottom, 1 top); vs-prior-high = close minus prior 20d high in ATRs (negative = below); structure = swing structure state (bull/bear).
- Verdict words: ROBUST (span <= 30% of range), moderate (<= 60%), SCATTERED (> 60%, GA indifferent or two basins). `**AT MIN/MAX BOUND**` = >= half of the rows sit on a range edge (range possibly too narrow). Gate/mode verdicts: ON (>= 80% of rows), OFF (<= 20%), SCATTERED in between.
- `m.*` rows are DeterministicScorer genes. Dropped from the tables as inferred-dead for options (see cross-strategy section): k_stop, k_target, atr_*, min_stop_loss_pct, regime_*, risk_per_trade_pct.
- Weekdays: Sat/Sun are shown excluded (no bars). `sl_ml` = stop at % of max loss; `td` = time exit (days held); `dte` = DTE exit.

## Important caveat on "convergence"

The top-N rows of one GA run are near-clones (same basin, a mutation or two apart), so identical values across TOP1-5 show only that the final population collapsed, not that the gene was independently validated. DTOP (distinct) rows and the cases where TOP5 sits in a different basin (IC, JL, CSP) are the real evidence of sensitivity. Strategies with low trade counts or low fitness (IC, JL, LP, VERT, BULLPS) have weak signal: scattered values there mean "nothing to find", not "robustly indifferent".

## Overview (TOP1 economics)

| strategy | CAR % | maxDD % | trades | fitness | note |
|---|---|---|---|---|---|
| O_LC | 108.8 | -41.5 | 405 | 25.5 | TOP1-5 CAR 99-111 |
| O_BULLCS | 66.2 | -33.6 | 434 | 16.5 | |
| O_STRG | 65.9 | -39.2 | 674 | 21.8 | 5 rows only |
| O_BF | 53.1 | -28.6 | 381 | 12.1 | |
| O_STRD | 42.0 | -25.4 | 348 | 17.4 | TOP5 has 726 trades |
| O_WHEEL | 32.4 | -25.2 | 238 | 7.5 | |
| O_CC | 29.1 | -23.6 | 333 | 14.6 | stock + CC |
| O_PP | 22.1 | -13.6 | 460 | 10.5 | stock + PP |
| O_CSP | 12.5 | -12.0 | 122 | 2.8 | |
| O_BULLPS | 8.6 | -6.4 | 128 | 1.6 | old rule |
| O_RS | 7.7 | -6.1 | 294 | 1.2 | |
| O_JL | 6.7 | -9.6 | 189 | 0.11 | |
| O_LP | 6.0 | -14.2 | 9 | 0.00 | found nothing |
| O_VERT | 5.5 | -5.6 | 32 | 0.09 | tiny sample |
| O_IC | 4.3 | -10.5 | 132 | 0.01 | |


## O_LC long call (opt 117)

**TOP1**: CAR 108.8%, maxDD -41.5%, 405 trades, fitness 25.52. TOP1-5 CAR 99..111%, DD -42..-33%, trades 291..407; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-gate_confidence (>) | 50 | 50 | 50 | 50 | 50 | 5/5 on [50..50] | [40,50] | ON (10/10); value med 50 span 0% **AT MAX BOUND** |
| shared-rel_volume (>) | 0.5 | 0.75 | 0.75 | 0.75 | 0.5 | 5/5 on [0.5..0.75] | [0.5,1.5] | ON (10/10); value med 0.75 span 25% |
| iv_rv (<) | · | 1.4 | · | 1.4 | · | 4/5 on [1.4..1.4] | [1,1.6] | SCATTERED (6/10 on); value med 1.4 span 0% |
| exit.opt_tp (>) | 75 | 75 | 75 | 75 | 75 | 5/5 on [75..100] | [25,200] | ON (10/10); value med 75 span 14% |
| signal | above | above | above | above | above | abovex5 |  | ON (10/10) dir abovex10 |
| market-slope | >0.05 | >0.05 | >0.05 | >0.05 | >0.05 | 5/5 on mostly >0.05 | [-0.3,0.3] | ON (10/10) dir >x10; value med 0.05 span 0% |
| market-rv | <1.5 | <1.5 | <1.25 | <1.5 | <1.5 | 5/5 on mostly <1.5 | [0.5,2] | ON (10/10) dir <x10; value med 1.5 span 17% |
| market-vs-prior-high | <1 | <1 | <1 | · | <1 | 5/5 on mostly <1 | [-3,2] | ON (9/10) dir <x9; value med 1 span 30% |
| m.w_technical | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | [0.5..0.5] med 0.5 | [0,0.8] | ROBUST med 0.5 span 0% |
| m.w_fundamental | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 | [0.4..0.4] med 0.4 | [0,0.7] | ROBUST med 0.4 span 0% |
| m.w_analyst | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 0% |
| m.w_earnings | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 0% |
| m.macro_mode | off | off | off | off | off | offx5 |  | mode offx10 |
| m.theta_buy | 0.35 | 0.35 | 0.35 | 0.35 | 0.35 | [0.35..0.35] med 0.35 | [0.15,0.45] | ROBUST med 0.35 span 0% |
| m.theta_sell | 0.3 | 0.3 | 0.3 | 0.3 | 0.3 | [0.3..0.3] med 0.3 | [0.1,0.4] | ROBUST med 0.3 span 0% |
| m.max_virtual_equity_per_instrument_percent | 45 | 45 | 45 | 45 | 45 | [45..50] med 45 | [5,50] | ROBUST med 45 span 11% |
| entry.strike_method | delta | delta | delta | delta | delta | deltax5 |  | mode deltax10 |
| entry.strike_delta | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 | [0.4..0.4] med 0.4 | [0.05,0.5] | ROBUST med 0.4 span 0% |
| entry.dte | 50 | 50 | 50 | 50 | 50 | [50..50] med 50 | [20,60] | ROBUST med 50 span 0% |
| entry.sizing | 3 | 3 | 3 | 3 | 3 | [3..3] med 3 | [1,10] | ROBUST med 3 span 0% |
| entry.entry_cross | 0.8 | 0.8 | 0.8 | 0.8 | 0.8 | [0.8..0.8] med 0.8 | [0.75,1] | ROBUST med 0.8 span 0% |
| optsel:debit:w_premium | -1.5 | -2 | -2 | -2 | -2 | [-2..-1.5] med -2 | [-2,2] | ROBUST med -2 span 12% **AT MIN BOUND** |
| optsel:debit:w_iv | 0 | -1 | -1 | -1 | -1 | [-1..0] med -1 | [-2,2] | ROBUST med -1 span 25% |
| optsel:debit:w_rvol | 1 | 1 | 1.5 | 1 | 1 | [1..1] med 1 | [0,2] | ROBUST med 1 span 25% |
| entry weekdays | mon tue | mon tue | mon tue | mon tue | mon tue | mon tuex5 | | mon tuex10 |

Always inactive across all rows: iv_rank, exp_profit, exit.opt_time, exit.opt_dte, exit.opt_sl_ml, market-adx, market-dist-support, market-dist-resistance, market-chan-pos, market-structure, entry.strike_param


**Convergence verdict**
- ROBUST (all 10 rows identical): DTE 50 (interior of 20-60), delta strike 0.40, sizing 3, entry-cross 0.8, take-profit 75, DS weights 0.5/0.4/0.2/0.2, theta_buy 0.35, macro off, weekdays Mon+Tue, signal = DS direction above (bullish).
- AT / NEAR BOUND: confidence gate `>50` at the MAX of 40-50 (widen: 55-65); optsel w_premium -2 at MIN (cheapest premium wins); per-instrument cap 45-50 (range max 50).
- SCATTERED / insensitive: iv_rv gate (on in 6/10, always 1.4), rel_volume value (0.5-0.75). Dead everywhere: iv_rank, exp_profit, time/DTE/sl_ml exits, adx, dist-support, dist-resistance, chan-pos, structure.

**Enters when (plain words)**: Buys a ~0.40-delta call with ~50 DTE on Mon/Tue when DS is bullish (confidence > 50), relative volume >= 0.5-0.75, the 50-day trend slope is above +0.05 (not falling), short-term realised vol is not spiking (5d/20d ratio < 1.5) and the close is less than ~1 ATR above the prior 20-day high (not over-extended). Exits only by take-profit at +75% (otherwise runs to expiry/stop).


## O_BULLCS bull call spread (opt 119)

**TOP1**: CAR 66.2%, maxDD -33.6%, 434 trades, fitness 16.54. TOP1-5 CAR 61..66%, DD -40..-33%, trades 358..442; 6 DTOP rows (11 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-gate_confidence (>) | 40 | · | · | 40 | · | 2/6 on [40..40] | [40,50] | SCATTERED (4/11 on); value med 40 span 0% **AT MIN BOUND** |
| iv_rank (<) | 30 | 15 | 15 | 30 | 15 | 6/6 on [15..15] | [10,60] | ON (11/11); value med 15 span 30% |
| exp_profit (>) | · | · | 6 | · | 6 | 1/6 on [6..6] | [2,20] | SCATTERED (3/11 on); value med 6 span 0% |
| exit.opt_tp (>) | 100 | 125 | 125 | 100 | 125 | 6/6 on [100..125] | [25,200] | ON (11/11); value med 125 span 14% |
| market-slope | >-0.1 | >-0.1 | >-0.2 | >-0.1 | >-0.2 | 6/6 on mostly >-0.1 | [-0.3,0.3] | ON (11/11) dir >x11; value med -0.1 span 17% |
| market-dist-support | <5 | · | · | <5 | · | 0/6 on | [0,5] | OFF (2/11 on) dir <x2 |
| market-dist-resistance | <4 | <1 | <1.5 | <4 | <1.5 | 6/6 on mostly <1 | [0,5] | ON (11/11) dir <x11; value med 1.5 span 60% |
| market-chan-pos | >0 | >0.4 | >0.4 | >0 | >0.4 | 6/6 on mostly >0.4 | [0,1] | ON (11/11) dir >x11; value med 0.4 span 40% |
| market-vs-prior-high | · | · | · | · | · | 1/6 on mostly <-0.25 | [-3,2] | OFF (1/11 on) dir <x1 |
| m.w_technical | 0.3 | 0.4 | 0.4 | 0.3 | 0.3 | [0.3..0.4] med 0.4 | [0,0.8] | ROBUST med 0.4 span 12% |
| m.w_fundamental | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 | [0.4..0.4] med 0.4 | [0,0.7] | ROBUST med 0.4 span 0% |
| m.w_analyst | 0.3 | 0.2 | 0.2 | 0.3 | 0.3 | [0.1..0.3] med 0.2 | [0,0.4] | moderate med 0.2 span 50% |
| m.w_earnings | 0.4 | 0.2 | 0.2 | 0.4 | 0.4 | [0.2..0.4] med 0.2 | [0,0.4] | moderate med 0.2 span 50% |
| m.macro_mode | off | gate | gate | off | off | gatex5/offx1 |  | mode gatex7/offx4 |
| m.theta_buy | 0.4 | 0.4 | 0.4 | 0.4 | 0.35 | [0.35..0.4] med 0.4 | [0.15,0.45] | ROBUST med 0.4 span 17% |
| m.theta_sell | 0.4 | 0.2 | 0.2 | 0.4 | 0.2 | [0.2..0.3] med 0.2 | [0.1,0.4] | SCATTERED med 0.2 span 67% |
| m.max_virtual_equity_per_instrument_percent | 50 | 50 | 50 | 50 | 35 | [35..50] med 50 | [5,50] | moderate med 50 span 33% **AT MAX BOUND** |
| entry.strike_method | delta | delta | delta | delta | delta | deltax6 |  | mode deltax11 |
| entry.strike_delta | 0.25 | 0.3 | 0.3 | 0.3 | 0.3 | [0.3..0.3] med 0.3 | [0.05,0.5] | ROBUST med 0.3 span 11% |
| entry.dte | 60 | 60 | 60 | 60 | 60 | [60..60] med 60 | [20,60] | ROBUST med 60 span 0% **AT MAX BOUND** |
| entry.sizing | 5 | 6 | 6 | 6 | 6 | [6..6] med 6 | [1,10] | ROBUST med 6 span 11% |
| entry.entry_cross | 0.9 | 0.95 | 0.95 | 0.9 | 0.9 | [0.9..0.95] med 0.95 | [0.75,1] | ROBUST med 0.95 span 20% |
| optsel:debit:w_premium | 1.5 | 0.5 | 0.5 | 1 | 1 | [0.5..1] med 0.5 | [-2,2] | ROBUST med 0.5 span 25% |
| optsel:debit:w_iv | -1 | -1 | -1 | -2 | -2 | [-2..-1] med -1 | [-2,2] | ROBUST med -1 span 25% |
| optsel:debit:w_rvol | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | [0.5..1] med 0.5 | [0,2] | ROBUST med 0.5 span 25% |
| entry weekdays | mon | mon tue | mon tue | mon | mon | mon tuex4/monx2 | | mon tuex6/monx5 |

Always inactive across all rows: shared-rel_volume, iv_rv, exit.opt_time, exit.opt_dte, exit.opt_sl_ml, signal, market-adx, market-rv, market-structure, entry.strike_param


**Convergence verdict**
- ROBUST: DTE 60 (AT MAX bound of 20-60: widen), delta 0.3 (T1 0.25), sizing 5-6, cross 0.9-0.95, take-profit 100-125% (range 25-200), iv_rank gate ON 11/11 (`<` 15-30, mostly 15), slope `>` -0.1/-0.2, dist-resistance `<` 1-4 (always on, value loose), chan-pos `>` 0.1-0.4, weekdays Mon (+Tue).
- AT BOUND: DTE max; cap 35-50 (mode 50 = max); confidence gate 40 = MIN when on (4/11 only).
- SCATTERED: macro_mode (gate 7 / off 4), theta_sell, w_analyst/w_earnings, exp_profit (3/11), dist-support (2/11). DS signal never required (signal off 11/11) - entry is driven by market regime + IV, not by the DS view.
- Dead: rel_volume, iv_rv, time/DTE/sl_ml exits, adx, rv, structure.

**Enters when (plain words)**: Buys a long-dated (60 DTE) ~0.30-delta call debit spread on Mon/Tue when IV rank is LOW (< 15-30, cheap premium), the 50d trend is not falling (slope > -0.1), price sits in the upper part of its 20d channel (> 0.1-0.4) and within ~1-1.5 ATR of resistance (about to test highs). No DS direction needed. Takes profit at +100-125% of debit.


## O_BULLPS bull put spread (opt 120, OLD assignment rule, superseded by 134)

**TOP1**: CAR 8.6%, maxDD -6.4%, 128 trades, fitness 1.64. TOP1-5 CAR 5..9%, DD -7..-4%, trades 106..158; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-rel_volume (>) | 0.75 | 0.75 | 0.75 | 0.75 | 0.75 | 5/5 on [0.75..1] | [0.5,1.5] | ON (10/10); value med 0.75 span 25% |
| iv_rv (>) | 0.9 | 1 | 1 | 0.9 | 0.9 | 5/5 on [0.9..1.1] | [0.8,1.6] | ON (10/10); value med 0.9 span 25% |
| exp_profit (>) | 8 | 4 | 4 | 8 | 8 | 5/5 on [4..8] | [2,20] | ON (10/10); value med 8 span 22% |
| market-slope | <0.3 | <0.3 | <0.3 | <0.3 | <0.3 | 5/5 on mostly <0.3 | [-0.3,0.3] | ON (10/10) dir <x10; value med 0.3 span 17% **AT MAX BOUND** |
| market-rv | <1.25 | <1.25 | <1.25 | · | <1.25 | 4/5 on mostly <1.25 | [0.5,2] | ON (8/10) dir <x8; value med 1.25 span 0% |
| market-dist-support | <3.5 | <3.5 | <3.5 | <3.5 | <3.5 | 4/5 on mostly <3.5 | [0,5] | ON (9/10) dir <x9; value med 3.5 span 10% |
| market-dist-resistance | · | <2.5 | · | <2 | <2 | 2/5 on mostly <3.5 | [0,5] | SCATTERED (5/10 on) dir <x5; value med 2.5 span 30% |
| market-chan-pos | · | · | · | · | >0.2 | 0/5 on | [0,1] | OFF (1/10 on) dir >x1 |
| market-vs-prior-high | · | · | · | · | <-0.25 | 0/5 on | [-3,2] | OFF (1/10 on) dir <x1 |
| market-structure | bear | bear | bear | bear | bear | bearx5 |  | ON (10/10) dir bearx10 |
| m.w_technical | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.8] | ROBUST med 0.2 span 0% |
| m.w_fundamental | 0.5 | 0.5 | 0.5 | 0.5 | 0.7 | [0.5..0.7] med 0.5 | [0,0.7] | ROBUST med 0.5 span 29% |
| m.w_analyst | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 0% |
| m.w_earnings | 0 | 0 | 0 | 0 | 0 | [0..0] med 0 | [0,0.4] | ROBUST med 0 span 0% **AT MIN BOUND** |
| m.macro_mode | multiply | multiply | gate | multiply | gate | multiplyx2/gatex2/offx1 |  | mode multiplyx5/gatex4/offx1 |
| m.theta_buy | 0.25 | 0.25 | 0.25 | 0.25 | 0.25 | [0.15..0.25] med 0.25 | [0.15,0.45] | moderate med 0.25 span 33% |
| m.theta_sell | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0.1,0.4] | ROBUST med 0.2 span 0% |
| m.max_virtual_equity_per_instrument_percent | 20 | 20 | 20 | 20 | 20 | [20..20] med 20 | [5,50] | ROBUST med 20 span 0% |
| entry.strike_param | 10 | 10 | 10 | 10 | · | [8..12] med 10 | [4,16] | moderate med 10 span 33% |
| entry.strike_method | percent_otm | percent_otm | percent_otm | percent_otm | delta | percent_otmx5 |  | mode percent_otmx9/deltax1 |
| entry.strike_delta | · | · | · | · | 0.3 | · | [0.05,0.5] | mostly inactive (1/10) |
| entry.dte | 45 | 45 | 45 | 45 | 45 | [45..45] med 45 | [20,60] | ROBUST med 45 span 0% |
| entry.sizing | 30 | 30 | 30 | 30 | 30 | [27.5..30] med 30 | [5,30] | ROBUST med 30 span 10% **AT MAX BOUND** |
| entry.min_arc | 2.5 | 2.5 | 2.5 | 2.5 | 2.5 | [2.5..2.5] med 2.5 | [0,3] | ROBUST med 2.5 span 0% |
| entry.entry_cross | 0.85 | 0.85 | 0.85 | 0.85 | 0.85 | [0.85..0.85] med 0.85 | [0.75,1] | ROBUST med 0.85 span 0% |
| optsel:credit:w_premium | -1.5 | -1.5 | -1.5 | -1.5 | -1.5 | [-1.5..-0.5] med -1.5 | [-2,2] | ROBUST med -1.5 span 25% |
| optsel:credit:w_iv | 0 | 0 | 0 | 0 | 0 | [0..0.5] med 0 | [-2,2] | ROBUST med 0 span 12% |
| optsel:credit:w_rvol | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | [0.5..1] med 0.5 | [0,2] | ROBUST med 0.5 span 25% |
| entry weekdays | mon wed thu fri | mon wed thu fri | mon wed thu fri | mon wed thu fri | mon wed thu fri | mon wed thu frix5 | | mon wed thu frix10 |

Always inactive across all rows: shared-gate_confidence, iv_rank, exit.opt_tp, exit.opt_time, exit.opt_dte, exit.opt_sl, exit.opt_sl_ml, signal, market-adx


**Convergence verdict**
- NOTE: old assignment rule; results are superseded by re-run 134. Treat as qualitative only (CAR 5-9%, DD -4..-7%).
- ROBUST (identical in all 10): NO exit rule at all (tp/time/dte/sl/sl_ml all off 10/10, hold to expiry), DTE 45, strike 10% OTM (8-12; one row delta 0.3), sizing 30 (AT MAX of 5-30), min_arc 2.5 (range 0-3, near max), cross 0.85, cap 20, DS w_earnings 0 (MIN), w_technical 0.2, structure = bear 10/10, dist-support `<` 3.5 (9/10), rv `<` 1.25 (8/10), iv_rv `>` 0.9-1.1, exp_profit `>` 4-8, rel_volume `>` 0.75.
- AT BOUND: sizing max, slope `<` 0.3 = max of range (effectively non-binding). dist-resistance 5/10, chan-pos/vs-prior-high 1/10 (scattered/off).
- Weekdays Mon/Wed/Thu/Fri (Tue always off). macro multiply/gate mix; theta_buy 0.15-0.25.

**Enters when (plain words)**: Sells a 45-DTE put spread ~10% OTM, any weekday but Tue, when the swing structure is BEAR (a washed-out name), price is within ~3.5 ATR above support, realised-vol ratio < 1.25 (no panic spike), IV/RV > 0.9 (premium not cheap) and some positive expected profit. No DS direction gate. Holds to expiry (no take-profit/stop).


## O_WHEEL (opt 121)

**TOP1**: CAR 32.4%, maxDD -25.2%, 238 trades, fitness 7.48. TOP1-5 CAR 31..43%, DD -28..-22%, trades 174..255; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| exp_profit (>) | 16 | 16 | 18 | 16 | 16 | 3/5 on [16..16] | [2,20] | ON (8/10); value med 16 span 11% |
| exit.opt_tp (>) | 55 | · | · | · | · | 1/5 on [55..55] | [25,75] | OFF (2/10 on) |
| exit.opt_time (>) | 30 | · | · | · | 30 | 2/5 on [30..30] | [10,35] | SCATTERED (4/10 on); value med 30 span 0% |
| exit.opt_sl (<) | · | -100 | · | -100 | -100 | 3/5 on [-100..-75] | [-200,-50] | SCATTERED (6/10 on); value med -100 span 17% |
| exit.opt_sl_ml (>) | 40 | 40 | 40 | 40 | 40 | 5/5 on [30..40] | [25,75] | ON (10/10); value med 40 span 20% |
| market-slope | <0.2 | · | · | · | · | 2/5 on mostly <0.2 | [-0.3,0.3] | SCATTERED (3/10 on) dir <x3; value med 0.2 span 0% |
| market-rv | · | <2 | · | <2 | <2 | 2/5 on mostly <2 | [0.5,2] | SCATTERED (5/10 on) dir <x5; value med 2 span 0% **AT MAX BOUND** |
| market-vs-prior-high | · | <1 | · | <1 | <1 | 2/5 on mostly <1 | [-3,2] | SCATTERED (5/10 on) dir <x5; value med 1 span 10% |
| m.w_technical | 0.3 | 0.7 | 0.7 | 0.7 | 0.7 | [0.3..0.7] med 0.4 | [0,0.8] | moderate med 0.7 span 50% |
| m.w_fundamental | 0.1 | 0.4 | 0.7 | 0.4 | 0.4 | [0.1..0.7] med 0.4 | [0,0.7] | SCATTERED med 0.4 span 86% |
| m.w_analyst | 0.1 | 0.2 | 0.2 | 0.2 | 0.2 | [0.1..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 25% |
| m.w_earnings | 0.2 | 0.3 | 0.3 | 0.3 | 0.3 | [0.2..0.3] med 0.3 | [0,0.4] | ROBUST med 0.3 span 25% |
| m.macro_mode | gate | off | off | off | off | offx3/gatex2 |  | mode offx7/gatex3 |
| m.theta_buy | 0.15 | 0.15 | 0.15 | 0.15 | 0.15 | [0.15..0.25] med 0.15 | [0.15,0.45] | moderate med 0.15 span 33% **AT MIN BOUND** |
| m.theta_sell | 0.1 | 0.3 | 0.3 | 0.3 | 0.3 | [0.1..0.3] med 0.3 | [0.1,0.4] | SCATTERED med 0.3 span 67% |
| m.max_virtual_equity_per_instrument_percent | 50 | 40 | 45 | 40 | 40 | [40..50] med 40 | [5,50] | ROBUST med 40 span 22% |
| entry.strike_method | delta | delta | delta | delta | delta | deltax5 |  | mode deltax10 |
| entry.strike_delta | 0.2 | 0.3 | 0.35 | 0.3 | 0.3 | [0.2..0.35] med 0.3 | [0.05,0.5] | moderate med 0.3 span 33% |
| entry.dte | 20 | 20 | 25 | 20 | 20 | [20..30] med 25 | [20,50] | moderate med 20 span 33% **AT MIN BOUND** |
| entry.sizing | 30 | 30 | 30 | 30 | 30 | [30..30] med 30 | [5,50] | ROBUST med 30 span 0% |
| entry.min_arc | 0.05 | 0.05 | 0.05 | 0.05 | 0.05 | [0.05..0.15] med 0.05 | [0,0.3] | moderate med 0.05 span 33% |
| entry.entry_cross | 0.95 | 0.95 | 0.95 | 1 | 1 | [0.95..0.95] med 0.95 | [0.75,1] | ROBUST med 0.95 span 20% |
| optsel:credit:w_premium | -0.5 | -0.5 | -0.5 | -0.5 | -0.5 | [-0.5..0] med -0.5 | [-2,2] | ROBUST med -0.5 span 12% |
| optsel:credit:w_iv | -1.5 | -2 | -1.5 | -2 | -2 | [-2..-1.5] med -2 | [-2,2] | ROBUST med -2 span 12% **AT MIN BOUND** |
| optsel:credit:w_rvol | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | [0.5..0.5] med 0.5 | [0,2] | ROBUST med 0.5 span 0% |
| cc_sell.strike_method | delta | delta | delta | delta | delta | deltax5 |  | mode deltax10 |
| cc_sell.strike_delta | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | [0.05..0.1] med 0.1 | [0.05,0.5] | ROBUST med 0.1 span 11% |
| cc_sell.dte | 45 | 45 | 45 | 60 | 45 | [35..60] med 45 | [20,60] | SCATTERED med 45 span 62% |
| cc_sell.entry_cross | 0.95 | 0.9 | 0.9 | 0.95 | 0.9 | [0.9..0.95] med 0.95 | [0.75,1] | ROBUST med 0.925 span 20% |
| entry weekdays | mon wed | mon tue thu fri | mon tue thu fri | mon tue thu fri | mon tue thu fri | mon tue thu frix2/mon tue frix1/mon wedx1/mon tue wedx1 | | mon tue thu frix6/mon wedx2/mon tue frix1/mon tue wedx1 |

Always inactive across all rows: shared-gate_confidence, iv_rank, shared-rel_volume, iv_rv, exit.cc_dte, exit.opt_dte, signal, market-adx, market-dist-support, market-dist-resistance, market-chan-pos, market-structure, entry.strike_param, cc_sell.strike_param


**Convergence verdict**
- ROBUST: put leg delta 0.2-0.35 (mode 0.3), sizing 30, entry-cross 0.95-1, covered-call leg delta 0.10 (0.05-0.1), exp_profit `>` 16 (8/10), sl_ml exit 40 ON 10/10 (30-40), cap 40-50, optsel w_premium -0.5, w_rvol 0.5.
- AT BOUND: put DTE 20 (MIN of 20-50: widen downward / test shorter), theta_buy 0.15 (MIN), optsel w_iv -2 (MIN, 5/10 rows).
- SCATTERED (fitness-insensitive): ALL market-condition gates (slope 3/10, rv 5/10 at max-bound 2 = non-binding, vs-prior-high 5/10), TP 55 (2/10), time exit 30 (4/10), sl -100 (6/10), CC DTE 35-60, DS w_technical/w_fundamental, theta_sell, macro_mode, extra weekdays (Mon always).
- Dead: confidence, iv_rank, rel_volume, iv_rv, adx, dist-*, chan-pos, structure, cc_dte exit, dte exit, signal.

**Enters when (plain words)**: No real market-regime filter: sells a ~0.30-delta short-dated (20-25 DTE) put on Mondays (plus a varying second day) when expected profit > 16%, with DS entry threshold very permissive (theta_buy 0.15). If assigned, sells ~0.10-delta 45-DTE calls. Risk exit when loss reaches 40% of max loss. Market gates, where on, are non-binding (rv < 2, slope < 0.2).


## O_BF call butterfly (opt 122)

**TOP1**: CAR 53.1%, maxDD -28.6%, 381 trades, fitness 12.09. TOP1-5 CAR 53..58%, DD -33..-29%, trades 360..390; 7 DTOP rows (12 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-gate_confidence (>) | 40 | 40 | 40 | 40 | 40 | 7/7 on [40..40] | [40,50] | ON (12/12); value med 40 span 0% **AT MIN BOUND** |
| exp_profit (>) | 2 | 6 | 4 | 6 | 2 | 7/7 on [2..6] | [2,20] | ON (12/12); value med 4 span 22% |
| exit.opt_tp (>) | 100 | 100 | 100 | 100 | 100 | 6/7 on [100..100] | [25,200] | ON (11/12); value med 100 span 0% |
| market-rv | >1 | >1 | >1 | >1 | >1 | 7/7 on mostly >1 | [0.5,2] | ON (12/12) dir >x12; value med 1 span 17% |
| market-dist-support | >3 | >3 | >3 | >3 | >3 | 7/7 on mostly >3 | [0,5] | ON (12/12) dir >x12; value med 3 span 20% |
| market-vs-prior-high | >-2.25 | >-2.25 | >-2.25 | >-2.25 | >-2.25 | 7/7 on mostly >-2.25 | [-3,2] | ON (12/12) dir >x12; value med -2.25 span 10% |
| market-structure | bull | bull | bull | bull | bull | bullx7 |  | ON (12/12) dir bullx12 |
| m.w_technical | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.8] | ROBUST med 0.2 span 0% |
| m.w_fundamental | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | [0.5..0.5] med 0.5 | [0,0.7] | ROBUST med 0.5 span 0% |
| m.w_analyst | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 0% |
| m.w_earnings | 0 | 0 | 0 | 0 | 0 | [0..0] med 0 | [0,0.4] | ROBUST med 0 span 0% **AT MIN BOUND** |
| m.macro_mode | off | gate | off | off | off | offx4/gatex3 |  | mode offx8/gatex4 |
| m.theta_buy | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0.15,0.45] | ROBUST med 0.2 span 0% |
| m.theta_sell | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | [0.1..0.1] med 0.1 | [0.1,0.4] | ROBUST med 0.1 span 0% **AT MIN BOUND** |
| m.max_virtual_equity_per_instrument_percent | 35 | 35 | 35 | 35 | 35 | [35..35] med 35 | [5,50] | ROBUST med 35 span 0% |
| entry.strike_param | 0 | 0 | 0 | 0 | 0 | [0..0] med 0 | [0,6] | ROBUST med 0 span 0% **AT MIN BOUND** |
| entry.dte | 25 | 25 | 25 | 25 | 25 | [25..25] med 25 | [20,60] | ROBUST med 25 span 0% |
| entry.wing_width | 10 | 10 | 10 | 10 | 10 | [10..10] med 10 | [5,15] | ROBUST med 10 span 0% |
| entry.sizing | 10 | 12 | 12 | 10 | 12 | [10..12] med 10 | [2,16] | ROBUST med 10 span 14% |
| entry.entry_cross | 0.95 | 1 | 1 | 1 | 1 | [0.9..1] med 0.9 | [0.75,1] | moderate med 0.975 span 40% **AT MAX BOUND** |
| optsel:debit:w_premium | -0.5 | -0.5 | -0.5 | -0.5 | -0.5 | [-1..-0.5] med -0.5 | [-2,2] | ROBUST med -0.5 span 12% |
| optsel:debit:w_iv | -1.5 | -2 | -1.5 | -1.5 | -2 | [-2..-1.5] med -1.5 | [-2,2] | ROBUST med -1.5 span 12% |
| optsel:debit:w_rvol | 1 | 1 | 1 | 1 | 1 | [1..1] med 1 | [0,2] | ROBUST med 1 span 0% |
| entry weekdays | thu fri | thu fri | thu fri | thu fri | thu fri | thu frix6/thux1 | | thu frix11/thux1 |

Always inactive across all rows: iv_rank, shared-rel_volume, iv_rv, exit.opt_time, exit.opt_dte, exit.opt_sl_ml, signal, market-slope, market-adx, market-dist-resistance, market-chan-pos


**Convergence verdict**
- ROBUST (very tight, 12 rows): DTE 25, strike offset 0 = ATM (AT MIN bound), wing width 10 (5-15), sizing 10-12, cross 0.95-1 (AT MAX), take-profit 100 (11/12), confidence `>` 40 (AT MIN, 12/12), exp_profit `>` 2-6 (near min 2), rv `>` 1 (12/12), dist-support `>` 3 (12/12), vs-prior-high `>` -2.25 (12/12, loose: range -3..2), structure = bull (12/12), cap 35, DS w_fundamental 0.5, w_earnings 0 (MIN), theta_buy 0.2, theta_sell 0.1 (MIN), weekdays Thu/Fri.
- SCATTERED: macro_mode (off 8/gate 4), optsel w_iv (-1.5..-2).
- Dead: slope, adx, dist-resistance, chan-pos, iv_rank, rel_volume, iv_rv, time/DTE/sl_ml exits, DS signal.

**Enters when (plain words)**: Opens an ATM 25-DTE call butterfly (wings ~10%) on Thu/Fri when the swing structure is BULL, short-term realised vol is RISING (5d/20d ratio > 1), price is FAR from support (> 3 ATR above it) and not extremely below its prior 20d high, with a permissive DS confidence (> 40). Takes profit at +100% of debit; no other exit.


## O_IC iron condor (opt 124)

**TOP1**: CAR 4.3%, maxDD -10.5%, 132 trades, fitness 0.01. TOP1-5 CAR 3..7%, DD -17..-6%, trades 32..148; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| arm.o_ic-entry-hold | 1 | 1 | 1 | 1 | 1 | 5/5 on | [0,1] | ON (10/10) |
| iv_rank (>) | · | · | · | · | 45 | 0/5 on | [20,70] | OFF (1/10 on) |
| shared-rel_volume (>) | · | · | · | · | 0.75 | 3/5 on [0.75..0.75] | [0.5,1.5] | SCATTERED (4/10 on); value med 0.75 span 0% |
| iv_rv (>) | 1.1 | 0.9 | 0.9 | 0.9 | 1.2 | 4/5 on [1.1..1.2] | [0.8,1.6] | ON (9/10); value med 1.1 span 38% |
| exp_profit [INERT: id absent from rule set] | 10 | 14 | 10 | 10 | 16 | 3/5 on [10..14] | [2,20] | INERT (gene toggled but condition absent from rules) ON (8/10); value med 12 span 33% |
| exit.opt_time (>) | 15 | 30 | · | · | · | 4/5 on [10..30] | [10,35] | SCATTERED (6/10 on); value med 22.5 span 80% |
| exit.opt_dte (<=) | 15 | 9 | 15 | 15 | · | 2/5 on [3..9] | [0,21] | SCATTERED (6/10 on); value med 12 span 57% |
| exit.opt_sl (<) | -125 | · | -125 | -150 | -175 | 1/5 on [-125..-125] | [-200,-50] | SCATTERED (5/10 on); value med -125 span 33% |
| exit.opt_sl_ml (>) | 35 | 60 | 35 | 40 | · | 5/5 on [35..75] | [25,75] | ON (9/10); value med 40 span 80% |
| market-slope | >-0.15 | >-0.1 | >-0.1 | >-0.1 | >-0.15 | 5/5 on mostly >-0.15 | [-0.3,0.3] | ON (10/10) dir >x10; value med -0.15 span 8% |
| market-adx | <15 | <15 | <15 | <15 | <15 | 5/5 on mostly <15 | [10,40] | ON (10/10) dir <x10; value med 15 span 0% |
| market-rv | · | · | · | · | · | 1/5 on mostly <1.75 | [0.5,2] | OFF (1/10 on) dir <x1 |
| market-dist-support | <4.5 | <4.5 | <4.5 | <4.5 | <4.5 | 4/5 on mostly <4.5 | [0,5] | ON (9/10) dir <x9; value med 4.5 span 20% |
| market-dist-resistance | <1.5 | <1.5 | <1.5 | <2 | · | 4/5 on mostly <1.5 | [0,5] | ON (8/10) dir <x8; value med 1.5 span 20% |
| market-chan-pos | <0.4 | <0.4 | >0.4 | >0.4 | <0 | 5/5 on mostly <0.4 | [0,1] | ON (10/10) dir <x8/>x2; value med 0.4 span 40% |
| market-vs-prior-high | <0.75 | <0.75 | <0.75 | <0.75 | >-3 | 5/5 on mostly <0.75 | [-3,2] | ON (10/10) dir <x9/>x1; value med 0.75 span 75% |
| market-structure | bull | bull | bull | bull | · | bullx5 |  | ON (9/10) dir bullx9 |
| m.w_technical | 0.7 | 0.7 | 0.7 | 0.7 | 0.1 | [0.7..0.8] med 0.7 | [0,0.8] | SCATTERED med 0.7 span 88% |
| m.w_fundamental | 0 | 0.6 | 0.1 | 0.1 | 0.3 | [0..0.6] med 0 | [0,0.7] | SCATTERED med 0.05 span 86% **AT MIN BOUND** |
| m.w_analyst | 0.2 | 0.3 | 0.2 | 0.2 | 0.3 | [0.1..0.3] med 0.2 | [0,0.4] | moderate med 0.2 span 50% |
| m.w_earnings | 0.1 | 0 | 0.1 | 0.1 | 0.1 | [0..0.2] med 0.1 | [0,0.4] | moderate med 0.1 span 50% |
| m.macro_mode | off | multiply | off | off | gate | offx4/multiplyx1 |  | mode offx7/multiplyx2/gatex1 |
| m.theta_buy | 0.15 | 0.35 | 0.15 | 0.15 | 0.45 | [0.15..0.35] med 0.15 | [0.15,0.45] | SCATTERED med 0.15 span 100% **AT MIN BOUND** |
| m.theta_sell | 0.3 | 0.2 | 0.3 | 0.3 | 0.2 | [0.2..0.3] med 0.3 | [0.1,0.4] | moderate med 0.3 span 33% |
| m.max_virtual_equity_per_instrument_percent | 20 | 20 | 20 | 20 | 20 | [20..20] med 20 | [5,50] | ROBUST med 20 span 0% |
| entry-hold.strike_param | 20 | 20 | 20 | 20 | 10 | [20..20] med 20 | [8,20] | SCATTERED med 20 span 83% **AT MAX BOUND** |
| entry-hold.dte | 35 | 35 | 35 | 35 | 45 | [35..35] med 35 | [20,50] | moderate med 35 span 33% |
| entry-hold.wing_width | 6 | 6 | 6 | 6 | 6 | [5..6] med 6 | [3,8] | ROBUST med 6 span 20% |
| entry-hold.sizing | 40 | 40 | 40 | 40 | 15 | [40..40] med 40 | [5,50] | moderate med 40 span 56% |
| entry-hold.min_arc | 0 | 0 | 0 | 0 | 0.5 | [0..0.5] med 0 | [0,3] | ROBUST med 0 span 17% **AT MIN BOUND** |
| entry-hold.entry_cross | 0.95 | 0.95 | 0.95 | 0.95 | 0.75 | [0.95..0.95] med 0.95 | [0.75,1] | SCATTERED med 0.95 span 80% |
| optsel:credit:w_premium | 0 | 0 | 0 | 0 | -1.5 | [0..0.5] med 0 | [-2,2] | moderate med 0 span 50% |
| optsel:credit:w_iv | -1 | -1 | -1 | -1 | -1.5 | [-2..-0.5] med -1 | [-2,2] | moderate med -1 span 38% |
| optsel:credit:w_rvol | 0 | 0 | 0 | 0 | 1 | [0..0] med 0 | [0,2] | moderate med 0 span 50% **AT MIN BOUND** |
| entry-low_confidence.strike_param | 14 | 14 | 16 | 16 | 14 | [14..14] med 14 | [8,20] | ROBUST med 14 span 17% |
| entry-low_confidence.dte | 35 | 35 | 35 | 35 | 20 | [30..35] med 35 | [20,50] | moderate med 35 span 50% |
| entry-low_confidence.wing_width | 5 | 5 | 6 | 6 | 7 | [5..7] med 5 | [3,8] | moderate med 5.5 span 40% |
| entry-low_confidence.sizing | 40 | 40 | 40 | 40 | 10 | [25..40] med 40 | [5,50] | SCATTERED med 40 span 67% |
| entry-low_confidence.min_arc | 1 | 0 | 1 | 1 | 2 | [0..2.5] med 0.5 | [0,3] | SCATTERED med 1 span 83% |
| entry-low_confidence.entry_cross | 0.75 | 0.75 | 0.75 | 0.75 | 0.8 | [0.75..0.95] med 0.75 | [0.75,1] | SCATTERED med 0.75 span 80% **AT MIN BOUND** |
| low_confidence:value | 35 | 30 | 30 | 30 | 25 | [30..40] med 35 | [10,50] | moderate med 30 span 38% |
| entry weekdays | mon tue wed fri | wed | mon wed fri | mon fri | mon thu | mon tue wed frix3/tue wed frix1/wedx1 | | mon tue wed frix4/wedx2/mon wed frix1/mon frix1 |

Always inactive across all rows: arm.o_ic-entry-low_confidence, exit.opt_tp


**Convergence verdict**
- Weak strategy (CAR 3-7%, fitness ~0.01) and top-N are NOT one basin: TOP1-4 agree, TOP5 is different (w_technical 0.1 vs 0.7, strike 10 vs 20, sizing 15 vs 40, cross 0.75 vs 0.95).
- Neutral-rating (`hold`) arm ON 10/10; the low_confidence arm OFF 10/10, so all `entry-low_confidence.*` genes and `low_confidence:value` are INERT (their values are drift). exp_profit gene is INERT too (condition absent from the hold-arm rule set).
- ROBUST: ADX `<` 15 (10/10; near MIN of 10-40), slope `>` -0.1/-0.15 (10/10), dist-support `<` 4.5 (9/10), dist-resistance `<` 1.5-2 (8/10), vs-prior-high `<` 0.75 (10/10), structure = bull (9/10), chan-pos `<` 0.4 (8/10; 2 rows flip to `>`), iv_rv `>` 1.1 (9/10, 0.9-1.2), wing 6, cap 20 (all rows), strike 20% OTM (AT MAX of 8-20), DTE 35, min_arc 0 (MIN), cross 0.95.
- SCATTERED: exits (time 22/DTE 12/sl -125 each on in ~half; only sl_ml 40 is on 9/10 but value 35-75), DS weights and theta_buy 0.15-0.45, optsel weights, rel_volume.

**Enters when (plain words)**: Sells a wide (strikes ~20% OTM, wings 6%) 35-DTE iron condor on NEUTRAL-rated names, early in the week, when there is no trend (ADX < 15), 50d slope not falling (> -0.15), price compressed between support (< 4.5 ATR) and resistance (< 1.5 ATR), lower part of the channel (< 0.4), below prior 20d high, swing structure bull, and IV/RV > 1.1 (rich premium). Exits scattered: stop at ~40% of max loss.


## O_JL jade lizard (opt 125)

**TOP1**: CAR 6.7%, maxDD -9.6%, 189 trades, fitness 0.11. TOP1-5 CAR 3..7%, DD -10..-2%, trades 105..189; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-gate_confidence (>) | 45 | 45 | 45 | 45 | · | 1/5 on [45..45] | [40,50] | SCATTERED (5/10 on); value med 45 span 0% |
| iv_rank (>) | · | · | · | · | 50 | 4/5 on [45..50] | [20,70] | SCATTERED (5/10 on); value med 50 span 10% |
| shared-rel_volume (>) | 0.5 | 0.5 | 0.75 | 0.5 | · | 1/5 on [0.5..0.5] | [0.5,1.5] | SCATTERED (5/10 on); value med 0.5 span 25% **AT MIN BOUND** |
| exp_profit (>) | · | · | · | · | 18 | 4/5 on [16..18] | [2,20] | SCATTERED (5/10 on); value med 18 span 11% |
| exit.opt_tp (>) | 30 | 30 | 30 | 30 | · | 1/5 on [30..30] | [25,75] | SCATTERED (5/10 on); value med 30 span 0% |
| exit.opt_dte (<=) | · | · | · | · | 3 | 4/5 on [3..3] | [0,21] | SCATTERED (5/10 on); value med 3 span 0% |
| exit.opt_sl (<) | -175 | -175 | -175 | -175 | · | 1/5 on [-150..-150] | [-200,-50] | SCATTERED (5/10 on); value med -175 span 17% |
| exit.opt_sl_ml (>) | 45 | · | 45 | 45 | · | 0/5 on | [25,75] | SCATTERED (3/10 on); value med 45 span 0% |
| market-slope | <0 | <0 | <0 | <0 | · | 1/5 on mostly <0 | [-0.3,0.3] | SCATTERED (5/10 on) dir <x5; value med 0 span 0% |
| market-adx | <30 | <25 | <30 | <30 | · | 1/5 on mostly <40 | [10,40] | SCATTERED (5/10 on) dir <x5; value med 30 span 50% |
| market-rv | <1 | <1 | <1 | <1 | · | 1/5 on mostly <1 | [0.5,2] | SCATTERED (5/10 on) dir <x5; value med 1 span 0% |
| market-dist-support | <3 | <3 | <3.5 | <3 | <3.5 | 5/5 on mostly <3.5 | [0,5] | ON (10/10) dir <x10; value med 3.5 span 10% |
| market-dist-resistance | <2.5 | <2.5 | <2.5 | <2.5 | <5 | 4/5 on mostly <5 | [0,5] | ON (9/10) dir <x9; value med 5 span 50% **AT MAX BOUND** |
| market-chan-pos | <0.9 | <0.8 | <0.9 | <0.9 | >0.1 | 5/5 on mostly >0.1 | [0,1] | ON (10/10) dir <x5/>x5; value med 0.45 span 80% |
| market-vs-prior-high | <0.75 | <0.75 | <0.75 | <0.75 | <-1.75 | 5/5 on mostly <-1.75 | [-3,2] | ON (10/10) dir <x10; value med 0.625 span 65% |
| market-structure | · | · | · | · | bear | bearx4/·x1 |  | SCATTERED (5/10 on) dir bearx5 |
| m.w_technical | 0.8 | 0.8 | 0.8 | 0.6 | 0.6 | [0.5..0.6] med 0.6 | [0,0.8] | moderate med 0.6 span 38% |
| m.w_fundamental | 0.3 | 0.3 | 0.5 | 0.3 | 0.1 | [0..0.3] med 0 | [0,0.7] | SCATTERED med 0.2 span 71% |
| m.w_analyst | 0.1 | 0 | 0.1 | 0.2 | 0 | [0..0.1] med 0 | [0,0.4] | moderate med 0 span 50% **AT MIN BOUND** |
| m.w_earnings | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.1..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 25% |
| m.macro_mode | gate | gate | gate | gate | multiply | multiplyx3/gatex1/offx1 |  | mode gatex5/multiplyx4/offx1 |
| m.theta_buy | 0.35 | 0.35 | 0.35 | 0.3 | 0.35 | [0.25..0.35] med 0.35 | [0.15,0.45] | moderate med 0.35 span 33% |
| m.theta_sell | 0.2 | 0.2 | 0.2 | 0.1 | 0.2 | [0.2..0.3] med 0.2 | [0.1,0.4] | SCATTERED med 0.2 span 67% |
| m.max_virtual_equity_per_instrument_percent | 50 | 35 | 50 | 50 | 20 | [20..50] med 20 | [5,50] | SCATTERED med 32.5 span 67% |
| entry.strike_param | 8 | 8 | 8 | 8 | 10 | [8..12] med 12 | [6,16] | moderate med 9 span 40% |
| entry.dte | 40 | 40 | 40 | 40 | 45 | [40..50] med 45 | [20,50] | moderate med 42.5 span 33% |
| entry.wing_width | 5 | 5 | 5 | 5 | 5 | [3..5] med 5 | [3,8] | moderate med 5 span 40% |
| entry.sizing | 50 | 50 | 50 | 50 | 35 | [15..50] med 35 | [5,50] | SCATTERED med 42.5 span 78% **AT MAX BOUND** |
| entry.min_arc | 0.2 | 0.2 | 0.15 | 0.2 | 0.2 | [0.2..0.2] med 0.2 | [0,0.3] | ROBUST med 0.2 span 17% |
| entry.entry_cross | 0.8 | 0.8 | 0.8 | 0.8 | 0.95 | [0.8..1] med 0.95 | [0.75,1] | SCATTERED med 0.875 span 80% |
| optsel:credit:w_premium | 1.5 | 1 | 1.5 | 1.5 | -1 | [-1..1.5] med -1 | [-2,2] | SCATTERED med 0 span 62% |
| optsel:credit:w_iv | -2 | -2 | -2 | -2 | 1.5 | [-2..1.5] med 1.5 | [-2,2] | SCATTERED med -0.5 span 88% **AT MIN BOUND** |
| optsel:credit:w_rvol | 0.5 | 0.5 | 0.5 | 0.5 | 1.5 | [0.5..2] med 1.5 | [0,2] | SCATTERED med 1 span 75% |
| entry weekdays | mon wed thu fri | mon wed thu fri | wed thu fri | mon wed thu fri | tue thu | tue thux3/mon wed thu frix1/mon tue thux1 | | mon wed thu frix4/tue thux4/wed thu frix1/mon tue thux1 |

Always inactive across all rows: iv_rv, exit.opt_time, signal


**Convergence verdict**
- Weak (CAR 3-7%). Two basins: A = TOP1-4 (confidence `>` 45, rel_volume `>` 0.5, slope `<` 0, ADX `<` 30, rv `<` 1, TP 30, sl -175, sl_ml 45, w_iv -2, cap 35-50); B = TOP5 + DTOP (iv_rank `>` 45-50, exp_profit `>` 16-18, DTE exit 3, structure bear, w_iv +1.5, cap 20). Each gate is therefore ON in ~5/10 rows = SCATTERED.
- ROBUST across both: dist-support `<` 3-3.5 (10/10), dist-resistance `<` 2.5-5 (9/10; B hits MAX 5), vs-prior-high `<` 0.75 (10/10; B -1.75), chan-pos on 10/10 but direction splits (`<` 0.9 vs `>` 0.1), weekdays Wed/Thu, strike 8-12% OTM, DTE 40-50 (max bound 50), wing 5 (3-5), min_arc 0.2, w_earnings 0.2.
- AT BOUND: sizing 50 (MAX; 4/5 of TOP), DTE 50, dist-resistance 5.
- SCATTERED: sizing, cap (20-50), cross (0.8-1), all optsel weights, macro_mode.

**Enters when (plain words)**: Sells a jade lizard (put side ~8% OTM, call spread wing 5) with 40-50 DTE on Wed/Thu (+Mon/Fri) when price is near support (< 3.5 ATR) and below prior-high+0.75 ATR. Basin A: also trend slope < 0, ADX < 30, quiet vol (ratio < 1), confidence > 45; basin B: instead high IV rank (> 45), big expected profit (> 16) and bear swing structure. Exits: TP 30% / stop -175% / 45% of max loss in A; DTE exit at 3 in B.


## O_RS ratio spread (opt 126)

**TOP1**: CAR 7.7%, maxDD -6.1%, 294 trades, fitness 1.19. TOP1-5 CAR 7..8%, DD -8..-5%, trades 220..294; 7 DTOP rows (12 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| exit.opt_dte (<=) | 3 | 3 | 3 | · | 3 | 6/7 on [0..3] | [0,21] | ON (10/12); value med 3 span 14% |
| exit.opt_sl_ml (>) | 75 | 75 | 75 | · | · | 4/7 on [75..75] | [25,75] | SCATTERED (7/12 on); value med 75 span 0% **AT MAX BOUND** |
| market-rv | <1.25 | <1.25 | <1.25 | <1.25 | <1.25 | 7/7 on mostly <1.25 | [0.5,2] | ON (12/12) dir <x12; value med 1.25 span 0% |
| market-dist-resistance | <5 | <4.5 | <4.5 | <4.5 | · | 7/7 on mostly <4.5 | [0,5] | ON (11/12) dir <x11; value med 4.5 span 20% |
| market-vs-prior-high | · | · | · | · | <0.5 | 0/7 on | [-3,2] | OFF (1/12 on) dir <x1 |
| m.w_technical | 0.2 | 0.2 | 0.2 | 0.1 | 0.1 | [0.1..0.2] med 0.2 | [0,0.8] | ROBUST med 0.2 span 12% |
| m.w_fundamental | 0.6 | 0.6 | 0.6 | 0.6 | 0.6 | [0.4..0.6] med 0.6 | [0,0.7] | ROBUST med 0.6 span 29% |
| m.w_analyst | 0.3 | 0.3 | 0.3 | 0.3 | 0.3 | [0.3..0.3] med 0.3 | [0,0.4] | ROBUST med 0.3 span 0% |
| m.w_earnings | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | [0.1..0.2] med 0.1 | [0,0.4] | ROBUST med 0.1 span 25% |
| m.macro_mode | multiply | multiply | multiply | multiply | multiply | multiplyx6/gatex1 |  | mode multiplyx11/gatex1 |
| m.theta_buy | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | [0.15..0.2] med 0.2 | [0.15,0.45] | ROBUST med 0.2 span 17% |
| m.theta_sell | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | [0.1..0.2] med 0.1 | [0.1,0.4] | moderate med 0.1 span 33% **AT MIN BOUND** |
| m.max_virtual_equity_per_instrument_percent | 40 | 40 | 50 | 50 | 50 | [40..50] med 40 | [5,50] | ROBUST med 40 span 22% |
| entry.strike_param | 8 | 8 | 8 | 8 | 10 | [8..8] med 8 | [2,10] | ROBUST med 8 span 25% |
| entry.dte | 35 | 35 | 35 | 35 | 35 | [30..35] med 35 | [20,50] | ROBUST med 35 span 17% |
| entry.wing_width | 5 | 5 | 5 | 5 | 5 | [5..5] med 5 | [3,8] | ROBUST med 5 span 0% |
| entry.sizing | 17.5 | 17.5 | 17.5 | 15 | 15 | [10..17.5] med 17.5 | [5,30] | ROBUST med 17.5 span 30% |
| entry.min_arc | 0.05 | 0.05 | 0.05 | 0.1 | 0.1 | [0.05..0.1] med 0.1 | [0,0.3] | ROBUST med 0.1 span 17% |
| entry.entry_cross | 0.9 | 0.9 | 0.9 | 0.9 | 0.95 | [0.85..0.9] med 0.9 | [0.75,1] | moderate med 0.9 span 40% |
| optsel:credit:w_premium | -1 | -1 | -1 | -1 | -1 | [-1..-1] med -1 | [-2,2] | ROBUST med -1 span 0% |
| optsel:credit:w_iv | -2 | -2 | -2 | -2 | -2 | [-2..-2] med -2 | [-2,2] | ROBUST med -2 span 0% **AT MIN BOUND** |
| optsel:credit:w_rvol | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | [1.5..1.5] med 1.5 | [0,2] | ROBUST med 1.5 span 0% |
| entry weekdays | mon wed | mon wed | mon wed | mon wed | mon wed | mon wedx7 | | mon wedx12 |

Always inactive across all rows: shared-gate_confidence, iv_rank, shared-rel_volume, iv_rv, exp_profit, exit.opt_tp, exit.opt_time, exit.opt_sl, signal, market-slope, market-adx, market-dist-support, market-chan-pos, market-structure


**Convergence verdict**
- VERY TIGHT (12 rows): strike offset 8% (2-10), DTE 35, wing 5, sizing 15-17.5, min_arc 0.05-0.1, cross 0.9, optsel w_premium -1 / w_iv -2 (MIN) / w_rvol 1.5, DTE exit 3 (10/12), sl_ml 75 (AT MAX of 25-75; 7/12), rv `<` 1.25 (12/12), dist-resistance `<` 4.5-5 (11/12; near MAX 5 = nearly non-binding), DS fundamental 0.6/analyst 0.3, macro multiply 11/12, theta_buy 0.2, theta_sell 0.1 (MIN), cap 40-50, weekdays Mon+Wed (12/12).
- Dead: every other gate (confidence, iv_rank, rel_volume, iv_rv, exp_profit, slope, adx, dist-support, chan-pos, structure), TP, time exit, hard SL, signal.

**Enters when (plain words)**: Practically unconditional: every Mon/Wed opens a 35-DTE ratio spread (short strikes ~8% OTM, wing 5) whenever the 5d/20d realised-vol ratio is below 1.25 (calm) and price is not far under resistance. Held until 3 DTE, loss cap at 75% of max loss. The edge (CAR 7-8%, DD -5..-8%) is a steady low-vol carry, not a market-timing signal.


## O_CSP cash-secured put (opt 127)

**TOP1**: CAR 12.5%, maxDD -12.0%, 122 trades, fitness 2.80. TOP1-5 CAR 10..17%, DD -20..-8%, trades 101..294; 5 DTOP rows (10 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| iv_rv (>) | 0.8 | · | · | · | · | 0/5 on | [0.8,1.6] | OFF (1/10 on) |
| exp_profit (>) | · | 16 | 16 | 14 | 16 | 5/5 on [16..16] | [2,20] | ON (9/10); value med 16 span 11% |
| exit.opt_tp (>) | · | · | 45 | 45 | 45 | 3/5 on [45..45] | [25,75] | SCATTERED (6/10 on); value med 45 span 0% |
| exit.opt_time (>) | 30 | 30 | 30 | · | · | 1/5 on [30..30] | [10,35] | SCATTERED (4/10 on); value med 30 span 0% |
| exit.opt_sl (<) | · | · | · | · | · | 2/5 on [-100..-100] | [-200,-50] | OFF (2/10 on) |
| exit.opt_sl_ml (>) | 45 | 50 | 50 | · | 50 | 5/5 on [50..50] | [25,75] | ON (9/10); value med 50 span 10% |
| signal | · | · | · | · | · | ·x3/belowx2 |  | OFF (2/10 on) dir belowx2 |
| market-slope | >-0.25 | · | · | · | <0.2 | 2/5 on mostly <0.2 | [-0.3,0.3] | SCATTERED (4/10 on) dir <x3/>x1; value med 0.2 span 75% |
| market-rv | · | · | · | <1.75 | · | 0/5 on | [0.5,2] | OFF (1/10 on) dir <x1 |
| market-chan-pos | >0.3 | >0.3 | >0.3 | · | · | 0/5 on | [0,1] | SCATTERED (3/10 on) dir >x3; value med 0.3 span 0% |
| m.w_technical | 0.6 | 0.2 | 0.5 | 0.6 | 0.6 | [0.5..0.6] med 0.6 | [0,0.8] | moderate med 0.6 span 50% |
| m.w_fundamental | 0.5 | 0.6 | 0.5 | 0.5 | 0.5 | [0.5..0.5] med 0.5 | [0,0.7] | ROBUST med 0.5 span 14% |
| m.w_analyst | 0.1 | 0.2 | 0.1 | 0.1 | 0.1 | [0.1..0.2] med 0.1 | [0,0.4] | ROBUST med 0.1 span 25% |
| m.w_earnings | 0.4 | 0.3 | 0.4 | 0.3 | 0.4 | [0.3..0.4] med 0.4 | [0,0.4] | ROBUST med 0.4 span 25% **AT MAX BOUND** |
| m.macro_mode | gate | off | off | gate | gate | gatex4/offx1 |  | mode gatex7/offx3 |
| m.theta_buy | 0.15 | 0.2 | 0.2 | 0.15 | 0.15 | [0.15..0.2] med 0.15 | [0.15,0.45] | ROBUST med 0.15 span 17% **AT MIN BOUND** |
| m.theta_sell | 0.3 | 0.2 | 0.2 | 0.3 | 0.3 | [0.2..0.3] med 0.3 | [0.1,0.4] | moderate med 0.3 span 33% |
| m.max_virtual_equity_per_instrument_percent | 35 | 40 | 40 | 35 | 35 | [35..40] med 35 | [5,50] | ROBUST med 35 span 11% |
| entry.strike_param | 10 | 20 | 20 | · | 20 | [10..20] med 17.5 | [5,20] | SCATTERED med 20 span 67% **AT MAX BOUND** |
| entry.strike_method | percent_otm | percent_otm | percent_otm | delta | percent_otm | percent_otmx3/deltax2 |  | mode percent_otmx7/deltax3 |
| entry.strike_delta | · | · | · | 0.35 | · | [0.3..0.3] med 0.3 | [0.05,0.5] | mostly inactive (3/10) |
| entry.dte | 45 | 45 | 45 | 20 | 45 | [45..45] med 45 | [20,50] | SCATTERED med 45 span 83% |
| entry.sizing | 45 | 45 | 45 | 15 | 45 | [45..45] med 45 | [5,50] | SCATTERED med 45 span 67% |
| entry.min_arc | 0.3 | 0.3 | 0.3 | 0.15 | 0.3 | [0.15..0.3] med 0.3 | [0,0.3] | moderate med 0.3 span 50% **AT MAX BOUND** |
| entry.entry_cross | 0.9 | 0.95 | 0.9 | 0.95 | 0.9 | [0.9..1] med 0.9 | [0.75,1] | moderate med 0.9 span 40% |
| optsel:credit:w_premium | 1 | 0.5 | 1 | 0.5 | 1 | [-0.5..1] med 1 | [-2,2] | moderate med 1 span 38% |
| optsel:credit:w_iv | -0.5 | -0.5 | -0.5 | 1 | -0.5 | [-0.5..1] med -0.5 | [-2,2] | moderate med -0.5 span 38% |
| optsel:credit:w_rvol | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | [1.5..1.5] med 1.5 | [0,2] | ROBUST med 1.5 span 0% |
| entry weekdays | mon fri | mon wed | mon fri | mon fri | fri | mon frix3/frix2 | | mon frix6/frix3/mon wedx1 |

Always inactive across all rows: shared-gate_confidence, iv_rank, shared-rel_volume, exit.opt_dte, market-adx, market-dist-support, market-dist-resistance, market-vs-prior-high, market-structure


**Convergence verdict**
- Moderate. Two sub-basins: TOP1-3,5 (strike 20% OTM / DTE 45 / sizing 45 / cross 0.9-0.95 / min_arc 0.3) vs TOP4 (delta 0.35, DTE 20, sizing 15, min_arc 0.15).
- ROBUST: exp_profit `>` 14-16 (9/10), sl_ml 45-50 (9/10), w_rvol 1.5, cap 35-40, weekdays Fri (+Mon), theta_buy 0.15-0.2 (MIN).
- AT BOUND: strike 20% OTM (MAX of 5-20), min_arc 0.3 (MAX of 0-0.3), DS w_earnings 0.4 (MAX), theta_buy 0.15 (MIN).
- SCATTERED: TP 45 (6/10), time exit 30 (4/10), sl -100 (2/10), all market gates (slope 4/10, chan-pos 3/10, rv 1/10), signal below (2/10), macro (gate 7/off 3), optsel w_premium/w_iv.
- Dead: confidence, iv_rank, rel_volume, DTE exit, adx, dist-*, vs-prior-high, structure.

**Enters when (plain words)**: Sells far-OTM (~20%) 45-DTE puts on Fridays/Mondays whenever expected profit > ~16% and the (permissive) DS threshold is met. Market regime is irrelevant (gates scattered/off). Exits: stop at ~50% of max loss, optional take-profit at 45% / 30-day time stop.


## O_STRD long straddle (opt 128)

**TOP1**: CAR 42.0%, maxDD -25.4%, 348 trades, fitness 17.41. TOP1-5 CAR 42..50%, DD -42..-25%, trades 348..726; 3 DTOP rows (6 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| arm.o_strd-entry-hold | 1 | 1 | 1 | 3/3 on | [0,1] | ON (6/6) |
| iv_rank (<) | · | 45 | · | 0/3 on | [10,60] | OFF (1/6 on) |
| shared-rel_volume (>) | 1 | · | 1 | 3/3 on [1..1] | [0.5,1.5] | ON (5/6); value med 1 span 0% |
| iv_rv (<) | 1.3 | · | 1.3 | 1/3 on [1.3..1.3] | [1,1.6] | SCATTERED (3/6 on); value med 1.3 span 0% |
| arm.o_strd-entry-low_confidence | 1 | 1 | 1 | 3/3 on | [0,1] | ON (6/6) |
| exp_profit (>) | 16 | 16 | 16 | 3/3 on [16..16] | [2,20] | ON (6/6); value med 16 span 0% |
| exit.opt_tp (>) | 75 | 75 | 75 | 3/3 on [75..100] | [25,200] | ON (6/6); value med 75 span 14% |
| market-slope | <0.15 | >-0.15 | <0.15 | 1/3 on mostly <0.3 | [-0.3,0.3] | SCATTERED (4/6 on) dir <x3/>x1; value med 0.15 span 75% |
| market-adx | >30 | · | >30 | 3/3 on mostly >30 | [10,40] | ON (5/6) dir >x5; value med 30 span 0% |
| market-rv | <1.5 | <1.5 | <1.5 | 3/3 on mostly <1.5 | [0.5,2] | ON (6/6) dir <x6; value med 1.5 span 0% |
| market-dist-resistance | <2.5 | · | <2.5 | 3/3 on mostly <2.5 | [0,5] | ON (5/6) dir <x5; value med 2.5 span 0% |
| market-chan-pos | · | <0.8 | · | 1/3 on mostly <0.9 | [0,1] | SCATTERED (2/6 on) dir <x2 |
| m.w_technical | 0.2 | 0.2 | 0.7 | [0.2..0.3] med 0.3 | [0,0.8] | SCATTERED med 0.25 span 62% |
| m.w_fundamental | 0.5 | 0.5 | 0.2 | [0.5..0.7] med 0.5 | [0,0.7] | SCATTERED med 0.5 span 71% |
| m.w_analyst | 0.2 | 0.1 | 0.2 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 25% |
| m.w_earnings | 0.2 | 0.2 | 0.1 | [0.2..0.2] med 0.2 | [0,0.4] | ROBUST med 0.2 span 25% |
| m.macro_mode | multiply | multiply | multiply | multiplyx3 |  | mode multiplyx6 |
| m.theta_buy | 0.4 | 0.4 | 0.4 | [0.4..0.4] med 0.4 | [0.15,0.45] | ROBUST med 0.4 span 0% |
| m.theta_sell | 0.3 | 0.3 | 0.3 | [0.3..0.3] med 0.3 | [0.1,0.4] | ROBUST med 0.3 span 0% |
| m.max_virtual_equity_per_instrument_percent | 40 | 40 | 15 | [40..40] med 40 | [5,50] | moderate med 40 span 56% |
| entry-hold.dte | 55 | 55 | 15 | [55..55] med 55 | [10,55] | SCATTERED med 55 span 89% **AT MAX BOUND** |
| entry-hold.sizing | 9 | 9 | 7 | [8..9] med 9 | [1,10] | ROBUST med 9 span 22% |
| entry-hold.entry_cross | 0.8 | 0.8 | 0.9 | [0.75..0.9] med 0.8 | [0.75,1] | SCATTERED med 0.8 span 60% |
| optsel:debit:w_premium | 0.5 | 1 | -0.5 | [-0.5..0.5] med 0.5 | [-2,2] | moderate med 0.5 span 38% |
| optsel:debit:w_iv | 0 | -1 | 2 | [0..2] med 0 | [-2,2] | SCATTERED med 0 span 75% |
| optsel:debit:w_rvol | 0 | 0 | 1 | [0..1] med 0 | [0,2] | moderate med 0 span 50% **AT MIN BOUND** |
| entry-low_confidence.dte | 55 | 55 | 55 | [50..55] med 55 | [10,55] | ROBUST med 55 span 11% **AT MAX BOUND** |
| entry-low_confidence.sizing | 8 | 8 | 10 | [8..8] med 8 | [1,10] | ROBUST med 8 span 22% |
| entry-low_confidence.entry_cross | 0.8 | 0.8 | 0.8 | [0.8..0.8] med 0.8 | [0.75,1] | ROBUST med 0.8 span 0% |
| low_confidence:value | 25 | 15 | 20 | [25..30] med 25 | [10,50] | moderate med 25 span 38% |
| entry weekdays | fri | fri | tue fri | mon tue thu | fri | frix2/mon tue thux1 | | frix4/tue frix1/mon tue thux1 |

Always inactive across all rows: exit.opt_time, exit.opt_dte, exit.opt_sl_ml, market-dist-support, market-vs-prior-high, market-structure


**Convergence verdict**
- Only TOP1, TOP2, TOP5 and DTOP6/9/10 were persisted (6 rows); table columns T1-T3 = TOP1, TOP2, TOP5.
- ROBUST: both arms ON (neutral-rated `hold` arm AND low-confidence arm with confidence `<=` 20-30), exp_profit `>` 16 (6/6; in the low-conf arm), TP 75 (6/6; DTOP up to 100), no other exit, rv `<` 1.5 (6/6), ADX `>` 30 (5/6), dist-resistance `<` 2.5 (5/6), rel_volume `>` 1 (5/6), DS macro multiply (6/6), theta_buy 0.4 / theta_sell 0.3, sizing 8-9, cross 0.8, weekday Fri.
- AT BOUND: DTE 55 = MAX of 10-55 (5/6 rows; one at 15) - widen to 90+.
- SCATTERED: slope (4/6, direction flips), iv_rv `<` 1.3 (3/6), iv_rank (1/6), chan-pos (2/6), DS tech/fundamental weights, optsel weights (w_iv -> +2 in one row), cap 15-40.

**Enters when (plain words)**: Buys an ATM ~55-DTE straddle on Fridays on neutral- or low-confidence-rated names when a trend is already strong (ADX > 30), the 5d/20d vol ratio is still < 1.5 (vol not yet blown out), relative volume > 1 and price is within 2.5 ATR under resistance (breakout set-up), expected profit > 16%. Exit at +75% (up to 100%) of premium, otherwise held.


## O_STRG long strangle (opt 129)

**TOP1**: CAR 65.9%, maxDD -39.2%, 674 trades, fitness 21.82. TOP1-5 CAR 66..73%, DD -45..-39%, trades 644..688; 0 DTOP rows (5 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| arm.o_strg-entry-hold | 1 | 1 | 1 | 1 | 1 | 0/0 on | [0,1] | ON (5/5) |
| market-slope | >-0.2 | >-0.2 | >-0.2 | >-0.05 | >-0.05 | 0/0 on | [-0.3,0.3] | ON (5/5) dir >x5; value med -0.2 span 25% |
| market-rv | >1.5 | >1.5 | >1.5 | >1.5 | >1.5 | 0/0 on | [0.5,2] | ON (5/5) dir >x5; value med 1.5 span 0% |
| market-dist-resistance | <4 | <4 | <3.5 | <3.5 | <4 | 0/0 on | [0,5] | ON (5/5) dir <x5; value med 4 span 10% |
| market-chan-pos | >0 | >0 | >0 | >0 | >0 | 0/0 on | [0,1] | ON (5/5) dir >x5; value med 0 span 0% **AT MIN BOUND** |
| market-vs-prior-high | >-1.5 | >-1.5 | >-1.5 | >-1.5 | >-1.5 | 0/0 on | [-3,2] | ON (5/5) dir >x5; value med -1.5 span 0% |
| m.w_technical | 0.2 | 0.1 | 0.2 | 0.2 | 0.2 | · | [0,0.8] | ROBUST med 0.2 span 12% |
| m.w_fundamental | 0.3 | 0.1 | 0.2 | 0.2 | 0.3 | · | [0,0.7] | ROBUST med 0.2 span 29% |
| m.w_analyst | 0.2 | 0.1 | 0.1 | 0.1 | 0.2 | · | [0,0.4] | ROBUST med 0.1 span 25% |
| m.w_earnings | 0.3 | 0.3 | 0.3 | 0.3 | 0.3 | · | [0,0.4] | ROBUST med 0.3 span 0% |
| m.macro_mode | multiply | multiply | multiply | multiply | multiply |  |  | mode multiplyx5 |
| m.theta_buy | 0.4 | 0.4 | 0.45 | 0.45 | 0.4 | · | [0.15,0.45] | ROBUST med 0.4 span 17% |
| m.theta_sell | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | · | [0.1,0.4] | ROBUST med 0.2 span 0% |
| m.max_virtual_equity_per_instrument_percent | 40 | 40 | 40 | 40 | 40 | · | [5,50] | ROBUST med 40 span 0% |
| entry-hold.strike_method | delta | delta | delta | delta | delta |  |  | mode deltax5 |
| entry-hold.strike_delta | 0.45 | 0.45 | 0.45 | 0.45 | 0.45 | · | [0.05,0.5] | ROBUST med 0.45 span 0% |
| entry-hold.dte | 40 | 40 | 40 | 40 | 40 | · | [10,55] | ROBUST med 40 span 0% |
| entry-hold.sizing | 8 | 8 | 8 | 8 | 8 | · | [1,10] | ROBUST med 8 span 0% |
| entry-hold.entry_cross | 0.8 | 0.75 | 0.8 | 0.8 | 0.8 | · | [0.75,1] | ROBUST med 0.8 span 20% |
| optsel:debit:w_premium | 2 | 2 | 2 | 2 | 2 | · | [-2,2] | ROBUST med 2 span 0% **AT MAX BOUND** |
| optsel:debit:w_iv | 1 | 1 | 1 | 1 | 1 | · | [-2,2] | ROBUST med 1 span 0% |
| optsel:debit:w_rvol | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | · | [0,2] | ROBUST med 1.5 span 0% |
| entry-low_confidence.strike_method | delta | delta | delta | delta | delta |  |  | mode deltax5 |
| entry-low_confidence.strike_delta | 0.45 | 0.3 | 0.5 | 0.5 | 0.45 | · | [0.05,0.5] | moderate med 0.45 span 44% |
| entry-low_confidence.dte | 30 | 40 | 30 | 30 | 30 | · | [10,55] | ROBUST med 30 span 22% |
| entry-low_confidence.sizing | 7 | 8 | 7 | 7 | 7 | · | [1,10] | ROBUST med 7 span 11% |
| entry-low_confidence.entry_cross | 0.8 | 0.8 | 0.75 | 0.75 | 0.8 | · | [0.75,1] | ROBUST med 0.8 span 20% |
| low_confidence:value | 25 | 20 | 20 | 20 | 25 | · | [10,50] | ROBUST med 20 span 12% |
| entry weekdays | mon wed fri | mon wed fri | mon wed fri | mon wed fri | mon wed fri |  | | mon wed frix5 |

Always inactive across all rows: iv_rank, shared-rel_volume, iv_rv, arm.o_strg-entry-low_confidence, exp_profit, exit.opt_tp, exit.opt_time, exit.opt_dte, exit.opt_sl_ml, market-adx, market-dist-support, market-structure, entry-hold.strike_param, entry-low_confidence.strike_param


**Convergence verdict**
- Only 5 rows (no DTOP). Rows are near-identical, so convergence is real but the basin is not independently tested.
- ROBUST: strangle delta 0.45 (near MAX 0.5, i.e. almost a straddle), DTE 40, sizing 8, cross 0.75-0.8, optsel w_premium +2 (AT MAX), w_iv 1, w_rvol 1.5, rv `>` 1.5 (5/5), slope `>` -0.2/-0.05, dist-resistance `<` 3.5-4, vs-prior-high `>` -1.5, chan-pos `>` 0 (MIN bound: non-binding), NO exits (hold to expiry), weekdays Mon/Wed/Fri, macro multiply, theta_buy 0.4-0.45, cap 40.
- Low-confidence arm OFF 5/5 (its genes inert). Dead: all IV gates, rel_volume, exp_profit, adx, dist-support, structure.

**Enters when (plain words)**: Buys a ~0.45-delta 40-DTE strangle Mon/Wed/Fri on neutral-rated names exactly when short-term realised vol is EXPANDING (5d/20d ratio > 1.5), the 50d slope is not collapsing (> -0.2), price is within ~4 ATR of resistance and not more than 1.5 ATR below its prior 20d high. Holds to expiry. Opposite vol condition to STRD (< 1.5) and RS.


## O_CC covered call overlay on stock (opt 130)

**TOP1**: CAR 29.1%, maxDD -23.6%, 333 trades, fitness 14.56. TOP1-5 CAR 26..29%, DD -27..-16%, trades 291..333; 0 DTOP rows (5 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| gate_expected_profit (>) | · | 5 | 5 | 5 | · | 0/0 on | [0,15] | SCATTERED (3/5 on); value med 5 span 0% |
| gate_days_since_close (>) | · | · | · | · | 15 | 0/0 on | [0,30] | OFF (1/5 on) |
| exit.exit_bearish | · | · | 1 | · | · | 0/0 on | [0,1] | OFF (1/5 on) |
| exit.exit_takeprofit (>) | 24 | 24 | 24 | 24 | 24 | 0/0 on | [8,60] | ON (5/5); value med 24 span 0% |
| exit.exit_belock (>) | · | 4 | · | · | 4 | 0/0 on | [3,20] | SCATTERED (2/5 on) |
| market-slope | <0.25 | · | <0.2 | <0.2 | · | 0/0 on | [-0.3,0.3] | SCATTERED (3/5 on) dir <x3; value med 0.2 span 8% |
| market-adx | · | >20 | · | · | >20 | 0/0 on | [10,40] | SCATTERED (2/5 on) dir >x2 |
| market-rv | · | <0.75 | · | · | <0.75 | 0/0 on | [0.5,2] | SCATTERED (2/5 on) dir <x2 |
| market-dist-support | <3.5 | · | <3.5 | <3.5 | · | 0/0 on | [0,5] | SCATTERED (3/5 on) dir <x3; value med 3.5 span 0% |
| market-chan-pos | >0.3 | >0 | >0.3 | >0.3 | >0 | 0/0 on | [0,1] | ON (5/5) dir >x5; value med 0.3 span 30% |
| m.w_technical | 0.5 | 0.5 | 0.5 | 0.6 | 0.5 | · | [0,0.8] | ROBUST med 0.5 span 13% |
| m.w_fundamental | 0.1 | 0 | 0 | 0 | 0 | · | [0,0.7] | ROBUST med 0 span 14% **AT MIN BOUND** |
| m.w_analyst | 0.1 | 0.3 | 0.1 | 0.1 | 0.3 | · | [0,0.4] | moderate med 0.1 span 50% |
| m.w_earnings | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | · | [0,0.4] | ROBUST med 0.1 span 0% |
| m.macro_mode | off | gate | off | off | gate |  |  | mode offx3/gatex2 |
| m.theta_buy | 0.2 | 0.3 | 0.2 | 0.2 | 0.3 | · | [0.15,0.45] | moderate med 0.2 span 33% |
| m.theta_sell | 0.3 | 0.3 | 0.3 | 0.3 | 0.3 | · | [0.1,0.4] | ROBUST med 0.3 span 0% |
| m.max_virtual_equity_per_instrument_percent | 50 | 50 | 50 | 50 | 45 | · | [5,50] | ROBUST med 50 span 11% **AT MAX BOUND** |
| cc_sell.strike_method | delta | delta | delta | delta | delta |  |  | mode deltax5 |
| cc_sell.strike_delta | 0.15 | 0.1 | 0.15 | 0.15 | 0.1 | · | [0.05,0.5] | ROBUST med 0.15 span 11% |
| cc_sell.dte | 40 | 35 | 40 | 40 | 35 | · | [20,60] | ROBUST med 40 span 12% |
| cc_sell.entry_cross | 0.9 | 0.85 | 0.9 | 0.9 | 0.85 | · | [0.75,1] | ROBUST med 0.9 span 20% |
| exit_belock.action_value | 4 | 8 | 6 | 6 | 8 | · | [-2,8] | moderate med 6 span 40% |
| exit_stoploss.action_value | -12 | -4 | -12 | -12 | -4 | · | [-20,-3] | moderate med -12 span 47% |
| entry weekdays | mon fri | wed fri | mon fri | mon fri | wed fri |  | | mon frix3/wed frix2 |

Always inactive across all rows: gate_confidence, gate_days_since_profit, gate_days_since_loss, exit.cc_dte, exit.exit_downgrade, exit.exit_time, market-dist-resistance, market-vs-prior-high, market-structure, cc_sell.strike_param


**Convergence verdict**
- Stock-entry overlay (the entry is a stock buy on a bullish DS signal; the option is the covered-call exit leg). Only 5 rows.
- ROBUST: stock take-profit 24% (5/5), CC leg delta 0.10-0.15, CC DTE 35-40, CC cross 0.85-0.9, chan-pos `>` 0-0.3 (5/5, near MIN = non-binding), cap 45-50 (AT MAX), DS w_technical 0.5, w_fundamental 0 (MIN), w_earnings 0.1, theta_sell 0.3.
- SCATTERED: slope `<` 0.2-0.25 (3/5), dist-support `<` 3.5 (3/5), ADX `>` 20 + rv `<` 0.75 (2/5, same two rows), expected-profit gate 5 (3/5), belock 4/8 (2/5), stop-loss -12 vs -4 (two clusters), macro (off 3/gate 2), theta_buy 0.2/0.3.
- Dead: confidence gate, days-since-profit/loss, cc_dte, bearish/downgrade/time exits, dist-resistance, vs-prior-high, structure.

**Enters when (plain words)**: Buys the stock on a bullish DS signal (Mon/Fri or Wed/Fri) when it is not stretched above trend (slope < 0.2-0.25), within ~3.5 ATR of support and not in the bottom 30% of its channel. Immediately sells a ~0.10-0.15 delta, 35-40 DTE call; closes the stock at +24%, stop -12% (or -4%).


## O_PP protective put overlay on stock (opt 131)

**TOP1**: CAR 22.1%, maxDD -13.6%, 460 trades, fitness 10.48. TOP1-5 CAR 21..26%, DD -24..-13%, trades 448..655; 0 DTOP rows (5 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| gate_days_since_close (>) | 10 | · | · | 10 | · | 0/0 on | [0,30] | SCATTERED (2/5 on) |
| exit.exit_takeprofit (>) | 16 | 12 | 12 | 16 | 12 | 0/0 on | [8,60] | ON (5/5); value med 12 span 8% |
| exit.exit_belock (>) | 20 | 20 | 20 | 20 | 20 | 0/0 on | [3,20] | ON (5/5); value med 20 span 0% **AT MAX BOUND** |
| market-slope | >0 | · | · | >0 | · | 0/0 on | [-0.3,0.3] | SCATTERED (2/5 on) dir >x2 |
| m.w_technical | 0.3 | 0.3 | 0.2 | 0.2 | 0.2 | · | [0,0.8] | ROBUST med 0.2 span 13% |
| m.w_fundamental | 0.7 | 0.7 | 0.5 | 0.5 | 0.5 | · | [0,0.7] | ROBUST med 0.5 span 29% |
| m.w_analyst | 0.4 | 0.2 | 0.2 | 0.2 | 0.2 | · | [0,0.4] | moderate med 0.2 span 50% |
| m.w_earnings | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | · | [0,0.4] | ROBUST med 0.1 span 0% |
| m.macro_mode | off | off | off | off | off |  |  | mode offx5 |
| m.theta_buy | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | · | [0.15,0.45] | ROBUST med 0.2 span 0% |
| m.theta_sell | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | · | [0.1,0.4] | ROBUST med 0.1 span 0% **AT MIN BOUND** |
| m.max_virtual_equity_per_instrument_percent | 30 | 30 | 30 | 30 | 30 | · | [5,50] | ROBUST med 30 span 0% |
| pp_buy.strike_param | 12 | 15 | 9 | 12 | 15 | · | [3,15] | moderate med 12 span 50% |
| pp_buy.strike_method | percent_otm | percent_otm | percent_otm | percent_otm | percent_otm |  |  | mode percent_otmx5 |
| pp_buy.dte | 25 | 25 | 25 | 25 | 25 | · | [20,60] | ROBUST med 25 span 0% |
| pp_buy.entry_cross | 0.85 | 0.85 | 0.85 | 0.85 | 0.85 | · | [0.75,1] | ROBUST med 0.85 span 0% |
| exit_belock.action_value | 8 | 8 | 8 | 8 | 8 | · | [-2,8] | ROBUST med 8 span 0% **AT MAX BOUND** |
| exit_stoploss.action_value | -16 | -16 | -16 | -16 | -16 | · | [-20,-3] | ROBUST med -16 span 0% |
| entry weekdays | wed thu | thu fri | wed thu | wed thu | wed thu |  | | wed thux4/thu frix1 |

Always inactive across all rows: gate_confidence, gate_expected_profit, gate_days_since_profit, gate_days_since_loss, exit.exit_bearish, exit.exit_downgrade, exit.exit_time, market-adx, market-rv, market-dist-support, market-dist-resistance, market-chan-pos, market-vs-prior-high, market-structure, pp_buy.strike_delta


**Convergence verdict**
- Stock-entry overlay with a protective put. Only 5 rows.
- ROBUST: put 25 DTE (5/5), cross 0.85, strike 9-15% OTM (3-15; at the wide end), stock TP 12-16%, break-even lock trigger 20% (AT MAX of 3-20) lifting the stop to +8% (AT MAX of -2..8), stop-loss -16, macro off 5/5, theta_buy 0.2, theta_sell 0.1 (MIN), cap 30, weekdays Wed/Thu.
- SCATTERED: days-since-close > 10 (2/5), slope > 0 (2/5). Dead: every other market gate, confidence, exp-profit.

**Enters when (plain words)**: Essentially unconditional: buys the stock on a bullish DS signal on Wed/Thu, hedges with a 25-DTE put 9-15% OTM, locks in +8% once the trade is +20%, takes profit at +12-16%. No market-regime filter was selected.


## O_LP long put (opt 132) - found nothing

**TOP1**: CAR 6.0%, maxDD -14.2%, 9 trades, fitness 0.00. TOP1-5 CAR -0..6%, DD -14..-0%, trades 1..9; 0 DTOP rows (5 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| shared-gate_confidence (>) | 45 | · | 45 | · | 45 | 0/0 on | [40,50] | SCATTERED (3/5 on); value med 45 span 0% |
| iv_rank (<) | · | 25 | 35 | · | · | 0/0 on | [10,60] | SCATTERED (2/5 on) |
| shared-rel_volume (>) | · | 1.5 | · | 1 | · | 0/0 on | [0.5,1.5] | SCATTERED (2/5 on) |
| iv_rv (<) | 1.4 | 1.3 | 1.3 | · | 1.3 | 0/0 on | [1,1.6] | ON (4/5); value med 1.3 span 17% |
| exit.opt_tp (>) | 125 | 25 | 50 | · | · | 0/0 on | [25,200] | SCATTERED (3/5 on); value med 50 span 57% |
| exit.opt_time (>) | · | · | · | · | 20 | 0/0 on | [10,45] | OFF (1/5 on) |
| exit.opt_dte (<=) | · | · | · | 18 | 3 | 0/0 on | [0,21] | SCATTERED (2/5 on) |
| exit.opt_sl_ml (>) | 40 | 70 | 35 | 65 | 70 | 0/0 on | [25,75] | ON (5/5); value med 65 span 70% |
| signal | · | · | below | below | · |  |  | SCATTERED (2/5 on) dir belowx2 |
| market-slope | · | · | <0.05 | <0.1 | · | 0/0 on | [-0.3,0.3] | SCATTERED (2/5 on) dir <x2 |
| market-adx | <20 | >30 | >20 | >35 | <10 | 0/0 on | [10,40] | ON (5/5) dir >x3/<x2; value med 20 span 83% |
| market-rv | >1 | <0.75 | <1.5 | <0.5 | · | 0/0 on | [0.5,2] | ON (4/5) dir <x3/>x1; value med 0.875 span 67% |
| market-dist-support | <2 | >0.5 | >3 | >0.5 | <4.5 | 0/0 on | [0,5] | ON (5/5) dir >x3/<x2; value med 2 span 80% |
| market-dist-resistance | <2 | <3 | · | <0.5 | >1.5 | 0/0 on | [0,5] | ON (4/5) dir <x3/>x1; value med 1.75 span 50% |
| market-chan-pos | >0.8 | <0.5 | · | >0.3 | · | 0/0 on | [0,1] | SCATTERED (3/5 on) dir >x2/<x1; value med 0.5 span 50% |
| market-vs-prior-high | · | · | · | <-1.5 | · | 0/0 on | [-3,2] | OFF (1/5 on) dir <x1 |
| market-structure | bull | · | bear | · | · |  |  | SCATTERED (2/5 on) dir bullx1/bearx1 |
| m.w_technical | 0.1 | 0.5 | 0.8 | 0.8 | 0.2 | · | [0,0.8] | SCATTERED med 0.5 span 88% |
| m.w_fundamental | 0.7 | 0.4 | 0.6 | 0.1 | 0.4 | · | [0,0.7] | SCATTERED med 0.4 span 86% |
| m.w_analyst | 0.2 | 0.4 | 0.3 | 0.3 | 0.1 | · | [0,0.4] | SCATTERED med 0.3 span 75% |
| m.w_earnings | 0.3 | 0.2 | 0.3 | 0.2 | 0.2 | · | [0,0.4] | ROBUST med 0.2 span 25% |
| m.macro_mode | multiply | gate | off | gate | off |  |  | mode gatex2/offx2/multiplyx1 |
| m.theta_buy | 0.2 | 0.25 | 0.2 | 0.3 | 0.15 | · | [0.15,0.45] | moderate med 0.2 span 50% |
| m.theta_sell | 0.2 | 0.3 | 0.2 | 0.1 | 0.2 | · | [0.1,0.4] | SCATTERED med 0.2 span 67% |
| m.max_virtual_equity_per_instrument_percent | 40 | 30 | 15 | 30 | 25 | · | [5,50] | moderate med 30 span 56% |
| entry.strike_param | 6 | 0 | · | 2 | 4 | · | [0,8] | SCATTERED med 3 span 75% |
| entry.strike_method | percent_otm | percent_otm | delta | percent_otm | percent_otm |  |  | mode percent_otmx4/deltax1 |
| entry.strike_delta | · | · | 0.45 | · | · | · | [0.05,0.5] | mostly inactive (1/5) |
| entry.dte | 30 | 40 | 50 | 45 | 50 | · | [20,60] | moderate med 45 span 50% |
| entry.sizing | 7 | 2 | 8 | 4 | 5 | · | [1,10] | SCATTERED med 5 span 67% |
| entry.entry_cross | 0.8 | 0.85 | 0.8 | 0.75 | 1 | · | [0.75,1] | SCATTERED med 0.8 span 100% |
| optsel:debit:w_premium | -1 | 1 | 0 | 0 | -1 | · | [-2,2] | moderate med 0 span 50% |
| optsel:debit:w_iv | -1 | 0.5 | 2 | -1 | 1.5 | · | [-2,2] | SCATTERED med 0.5 span 75% |
| optsel:debit:w_rvol | 0.5 | 0.5 | 1 | 2 | 0.5 | · | [0,2] | SCATTERED med 0.5 span 75% |
| entry weekdays | mon wed thu fri | none | mon tue fri | tue fri | wed |  | | mon wed thu frix1/nonex1/mon tue frix1/tue frix1 |

Always inactive across all rows: exp_profit


**Convergence verdict**
- NO convergence: only 1-9 trades per row, CAR ~0-6%, fitness ~0. Every gene is scattered (strike %, delta, DTE 30-50, sizing 2-8, the market gates flip direction between rows, DS weights span the whole range). Do not mine this row for settings; it reflects 'nothing works', not a robust optimum.
- Only mild recurrences: ADX gate present in 5/5 (but `<`20, `>`30, `>`20, `>`35, `<`10), rv `<` 0.75-1.5 in 4/5, sl_ml exit on 5/5 (35-70).

**Enters when (plain words)**: No coherent regime. The few trades are idiosyncratic (a put needs a down move the DS-based universe rarely delivers); treat the market-condition output as noise.


## O_VERT vertical (opt 133)

**TOP1**: CAR 5.5%, maxDD -5.6%, 32 trades, fitness 0.09. TOP1-5 CAR 5..6%, DD -6..-4%, trades 26..32; 0 DTOP rows (5 rows total in verdicts; `·` = off/inactive).

| gene | T1 | T2 | T3 | T4 | T5 | DTOP | range | verdict (all rows) |
|---|---|---|---|---|---|---|---|---|
| exit.opt_tp (>) | 175 | 175 | 175 | · | 175 | 0/0 on | [25,200] | ON (4/5); value med 175 span 0% |
| exit.opt_time (>) | 35 | 35 | 35 | 35 | 35 | 0/0 on | [10,45] | ON (5/5); value med 35 span 0% |
| exit.opt_sl_ml (>) | · | 50 | · | · | 50 | 0/0 on | [25,75] | SCATTERED (2/5 on) |
| market-slope | <-0.1 | <-0.1 | <-0.1 | <-0.1 | <-0.1 | 0/0 on | [-0.3,0.3] | ON (5/5) dir <x5; value med -0.1 span 0% |
| market-adx | >15 | >15 | >15 | >15 | >15 | 0/0 on | [10,40] | ON (5/5) dir >x5; value med 15 span 0% |
| market-rv | <0.75 | <0.75 | <0.75 | <0.75 | <0.75 | 0/0 on | [0.5,2] | ON (5/5) dir <x5; value med 0.75 span 0% |
| market-dist-support | <1.5 | <1.5 | <1.5 | <1.5 | <1.5 | 0/0 on | [0,5] | ON (5/5) dir <x5; value med 1.5 span 0% |
| market-vs-prior-high | <1 | <1 | <1 | · | <1.25 | 0/0 on | [-3,2] | ON (4/5) dir <x4; value med 1 span 5% |
| market-structure | bull | bull | bull | bull | bull |  |  | ON (5/5) dir bullx5 |
| m.w_technical | 0.5 | 0.5 | 0.5 | 0.5 | 0.4 | · | [0,0.8] | ROBUST med 0.5 span 12% |
| m.w_fundamental | 0.2 | 0.2 | 0.2 | 0.2 | 0.1 | · | [0,0.7] | ROBUST med 0.2 span 14% |
| m.w_analyst | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | · | [0,0.4] | ROBUST med 0.2 span 0% |
| m.w_earnings | 0.2 | 0.2 | 0.3 | 0.3 | 0.2 | · | [0,0.4] | ROBUST med 0.2 span 25% |
| m.macro_mode | gate | multiply | gate | gate | multiply |  |  | mode gatex3/multiplyx2 |
| m.theta_buy | 0.25 | 0.2 | 0.25 | 0.25 | 0.25 | · | [0.15,0.45] | ROBUST med 0.25 span 17% |
| m.theta_sell | 0.1 | 0.1 | 0.1 | 0.1 | 0.1 | · | [0.1,0.4] | ROBUST med 0.1 span 0% **AT MIN BOUND** |
| m.max_virtual_equity_per_instrument_percent | 25 | 25 | 25 | 25 | 25 | · | [5,50] | ROBUST med 25 span 0% |
| entry.strike_method | delta | delta | delta | delta | delta |  |  | mode deltax5 |
| entry.strike_delta | 0.45 | 0.45 | 0.45 | 0.5 | 0.45 | · | [0.05,0.5] | ROBUST med 0.45 span 11% |
| entry.dte | 25 | 25 | 25 | 25 | 25 | · | [20,60] | ROBUST med 25 span 0% |
| entry.sizing | 3 | 3 | 3 | 3 | 3 | · | [1,10] | ROBUST med 3 span 0% |
| entry.entry_cross | 0.75 | 0.75 | 0.75 | 0.75 | 0.75 | · | [0.75,1] | ROBUST med 0.75 span 0% **AT MIN BOUND** |
| optsel:debit:w_premium | 2 | 2 | 2 | 2 | 2 | · | [-2,2] | ROBUST med 2 span 0% **AT MAX BOUND** |
| optsel:debit:w_iv | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | · | [-2,2] | ROBUST med 1.5 span 0% |
| optsel:debit:w_rvol | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | · | [0,2] | ROBUST med 1.5 span 0% |
| entry weekdays | tue wed fri | tue wed fri | tue wed fri | tue wed fri | tue wed fri |  | | tue wed frix5 |

Always inactive across all rows: shared-gate_confidence, iv_rank, shared-rel_volume, iv_rv, exp_profit, exit.opt_dte, signal, market-dist-resistance, market-chan-pos, entry.strike_param


**Convergence verdict**
- Only 5 rows, tiny sample (26-32 trades, CAR 5-6%), but the 5 rows are near-identical: delta 0.45-0.5, DTE 25, sizing 3, cross 0.75 (AT MIN), optsel w_premium +2 (AT MAX), w_iv 1.5, w_rvol 1.5, TP 175 (near max 200, 4/5), time exit 35 (5/5), slope `<` -0.1, ADX `>` 15, rv `<` 0.75, dist-support `<` 1.5, vs-prior-high `<` 1-1.25, structure bull, cap 25, weekdays Tue/Wed/Fri, DS tech 0.5.
- SCATTERED: macro (gate 3/multiply 2), sl_ml (2/5). Dead: confidence, iv, rel_volume, exp_profit, dist-resistance, chan-pos, DTE exit, signal.
- Caveat: 32 trades over 6 years - the gate stack is so restrictive that convergence may reflect 'few trades available', not edge.

**Enters when (plain words)**: Very selective (about 5 trades per year): a 25-DTE ~0.45-delta vertical when the 50d trend is DOWN (slope < -0.1) but the swing structure is bull, ADX > 15, vol is quiet (ratio < 0.75) and price is sitting within ~1.5 ATR of support (a bounce set-up), below its prior 20d high. Large TP (175% of premium) or a 35-day time exit.


---

# Cross-strategy synthesis (for stage-2 design)

## 1. Market-condition gates by strategy
Cell = direction + median threshold among rows where the gate is on (rows on / rows total). Empty = off in all rows. Only gates on in >= 80% of rows are "selected"; partial counts are noise or two basins. (O_LP is noise; O_BULLPS is the old rule.)

| strategy | slope | adx | rv | dist-support | dist-resistance | chan-pos | vs-prior-high | structure |
|---|---|---|---|---|---|---|---|---|
| O_LC | >0.05 (10/10) |  | <1.5 (10/10) |  |  |  | <1 (9/10) |  |
| O_BULLCS | >-0.1 (11/11) |  |  | <5 (2/11) | <1.5 (11/11) | >0.4 (11/11) | <-0.25 (1/11) |  |
| O_BULLPS | <0.3 (10/10) |  | <1.25 (8/10) | <3.5 (9/10) | <2.5 (5/10) | >0.2 (1/10) | <-0.25 (1/10) | bear (10/10) |
| O_WHEEL | <0.2 (3/10) |  | <2 (5/10) |  |  |  | <1 (5/10) |  |
| O_BF |  |  | >1 (12/12) | >3 (12/12) |  |  | >-2.25 (12/12) | bull (12/12) |
| O_IC | >-0.15 (10/10) | <15 (10/10) | <1.75 (1/10) | <4.5 (9/10) | <1.5 (8/10) | <0.4 (10/10) | <0.75 (10/10) | bull (9/10) |
| O_JL | <0 (5/10) | <30 (5/10) | <1 (5/10) | <3.5 (10/10) | <5 (9/10) | <0.9 (10/10) | <0.625 (10/10) | bear (5/10) |
| O_RS |  |  | <1.25 (12/12) |  | <4.5 (11/12) |  | <0.5 (1/12) |  |
| O_CSP | <0.2 (4/10) |  | <1.75 (1/10) |  |  | >0.3 (3/10) |  |  |
| O_STRD | <0.15 (4/6) | >30 (5/6) | <1.5 (6/6) |  | <2.5 (5/6) | <0.85 (2/6) |  |  |
| O_STRG | >-0.2 (5/5) |  | >1.5 (5/5) |  | <4 (5/5) | >0 (5/5) | >-1.5 (5/5) |  |
| O_CC | <0.2 (3/5) | >20 (2/5) | <0.75 (2/5) | <3.5 (3/5) |  | >0.3 (5/5) |  |  |
| O_PP | >0 (2/5) |  |  |  |  |  |  |  |
| O_LP | <0.075 (2/5) | >30 (5/5) | <0.75 (4/5) | >0.5 (5/5) | <2 (4/5) | >0.55 (3/5) | <-1.5 (1/5) | bull (2/5) |
| O_VERT | <-0.1 (5/5) | >15 (5/5) | <0.75 (5/5) | <1.5 (5/5) |  |  | <1 (4/5) | bull (5/5) |


Reading the matrix:
- **rv (5d/20d realised-vol ratio)** is the most widely used gate (LC, BULLPS, BF, RS, STRD, STRG, VERT; weaker in WHEEL/IC/CSP). Direction splits by structure: `<` 1.25-1.5 (calm) for carry and directional debit structures (LC, BULLPS, RS, STRD), `<` 0.75 for VERT; `>` 1-1.5 (vol expanding) for the vol-buyers that want a move: BF (`>1`) and STRG (`>1.5`). STRD is the exception among vol buyers (`<1.5`, plus ADX `>30`).
- **dist-support / dist-resistance (ATR distance)**: near support (`<` 3.5-4.5) for BULLPS, IC, JL, VERT, CC (sell-into-weakness / bounce structures); near resistance (`<` 1.5-4.5) for BULLCS, IC, RS, STRD, STRG (about to test a level). BF is the opposite (`dist-support > 3`, away from support).
- **slope (50d trend)**: lower-bounded `>` -0.2..+0.05 for the long-delta structures (LC, BULLCS, IC, STRG): "not in a downtrend". Upper-bounded `<` 0..0.3 for credit/range structures where selected (BULLPS at max bound = non-binding, JL basin A `<0`). VERT is the only one wanting a down-slope (`<-0.1`).
- **vs-prior-high**: upper-bounded `<` 0.75-1 ATR (not over-extended above the 20d high) in LC, IC, JL, VERT; BF and STRG use a loose lower bound (`>` -2.25 / -1.5, near non-binding).
- **chan-pos** (BULLCS `>0.4`, CC `>0.3`, STRG `>0`, IC `<0.4`, JL `<0.9`/`>0.1`) is either non-binding (value at a range end) or direction-flipping: weak signal.
- **structure (bull/bear swing state)** is selected for BF=bull, IC=bull, VERT=bull, BULLPS=**bear** (10/10, sells puts after weakness), JL=bear in basin B. Not used by LC, BULLCS, RS, CSP, WHEEL, STRD, STRG.
- **ADX**: IC `<15` (range), STRD `>30` (trend), VERT `>15`; unused elsewhere.
- Not selected anywhere as a stable gate: iv_rank (only BULLCS `<15-30` low IV; JL basin B `>45`), rel_volume (weak: LC, BULLPS, STRD), confidence (LC `>50` at max bound; BF/BULLCS/JL `>40-45` near the min = essentially a floor), exp_profit (BULLPS `>4-8`, CSP/WHEEL `>16`, BF `>2-6`).

## 2. The DS signal is mostly irrelevant for options
- The direction-signal gene (`signal` mode above/below) is ON only in LC (`above`); off in every other strategy (CSP/LP use `below` in 2 rows). IC/STRD/STRG have a fixed neutral-rating (`hold`) entry arm, STRD also a low-confidence arm; CC/PP use the bullish flag for the stock entry.
- So BULLCS, BULLPS, BF, RS, JL, WHEEL, VERT enter on market regime + IV/expected-profit only; their DS weights, theta_buy and macro_mode are scattered or arbitrary (macro_mode mixes off/gate/multiply except RS/STRD/STRG `multiply`, PP `off`). A stage-2 expert can pick the structure from market regime alone and use DS at most as a direction/confidence input for LC-like entries.

## 3. DTE per structure (searched ranges end at 50-60)
| bucket | strategies (typical DTE) |
|---|---|
| short <= 21d | none selected; WHEEL put leg sits at the range MIN (20, mostly 20-25) |
| mid-short 22-30 | BF 25, VERT 25, PP put 25, WHEEL put 20-25 |
| mid 31-60 | RS 35, IC 35, BULLPS 45, CSP 45 (one row 20), LC 50, JL 40-50, STRG 40, CC call 40, WHEEL call 45, BULLCS 60 (MAX), STRD 55 (MAX) |
| long > 90d | unreachable (ranges end at 50-60). BULLCS (60), STRD (55) and JL (50) are PINNED at the max, so the range is probably too narrow for the long-premium structures |

Debit structures that need time (BULLCS, STRD, LC) push to the top of the range; credit structures sit mid (35-45); only the pin-payoff BF and the short-vol overlays go to 20-25.

## 4. Exits that recur
| exit | strategies | typical value |
|---|---|---|
| take-profit (% of premium) | LC 75, BULLCS 100-125, BF 100, STRD 75, VERT 175 (debit, ON in all rows); JL 30, CSP 45, WHEEL 55 (credit, scattered) | debit structures want +75..+175% (range 25-200; VERT near max) |
| stop at % of max loss (`sl_ml`) | WHEEL 40, IC 40, CSP 50, JL 45, RS 75 (MAX), LP 65 | 40-50% for credit structures; the most recurrent exit |
| DTE exit | RS 3, JL 3 (basin B), IC ~12 (scattered) | close ~3 DTE (before pin/assignment) |
| time exit (days held) | VERT 35, WHEEL 30, CSP 30, IC ~22 | scattered except VERT |
| hard stop `sl` (% of premium) | WHEEL -100, IC -125, JL -175 | scattered |
| no exit rule (hold to expiry) | BULLPS (all off 10/10), STRG (all off 5/5) | |
| stock overlays | CC stock TP 24, PP stock TP 12-16, belock trigger 4-20 (PP at MAX), stop -12/-16 | |

## 5. Genes at a range bound (candidates to WIDEN in stage 2)
- DTE: BULLCS 60 (max), STRD 55 (max), JL 50 (max), WHEEL put 20 (min).
- Sizing: BULLPS 30 (max), JL 50 (max); STRD/BF/LC interior.
- Strike: IC and CSP 20% OTM (max), BF offset 0 (min, ATM), STRG delta 0.45 and VERT delta 0.45-0.5 (near max 0.5).
- Per-instrument cap: 50 (max) in LC, BULLCS, WHEEL, CC, RS, JL(4/5 TOP); 20 pinned in BULLPS/IC; 25-40 elsewhere.
- Gates: LC confidence `>50` (max); BF/BULLCS/JL confidence 40-45 (min); BULLPS slope `<0.3` (max); STRG chan-pos `>0` (min); RS sl_ml 75 (max); JL dist-resistance 5 (max).
- Option-selection weights: w_premium -2 (LC, min) or +2 (STRG, VERT, max); w_iv -2 (RS, WHEEL, JL basin A; min). The sign is robust within a family (debit vs credit), so widen to +/-3.
- min_arc: CSP 0.3 (max of 0-0.3), BULLPS 2.5 (near max 3), IC 0 (min).
- DS: theta_buy 0.15 (min) in WHEEL, CSP, IC; w_earnings 0 (min) in BULLPS, BF; w_earnings 0.4 (max) in CSP.
- entry-cross: BF 0.95-1 (max), VERT 0.75 (min).

## 6. Dead / irrelevant genes (everywhere or nearly)
- Inferred dead for options: `model:k_stop`, `k_target`, `atr_period`, `atr_multiplier`, `min_stop_loss_pct`, `regime_risk/stop/tp_scale`, `risk_per_trade_pct` (fixed settings have `use_atr_stop=false`, `regime_overlay_enabled=false`; values differ arbitrarily between strategies, e.g. regime_risk_scale ends at 1.75 in LC, 1.0 in IC, 0.75 in CSP). This is an inference from the settings and the lack of pattern, not an ablation. `atr_risk_budget_pct` also varies without pattern.
- Weekend `schedule:*` genes (no bars). The weekday choice is real but weakly consistent: LC Mon/Tue, BULLCS Mon(/Tue), BF Thu/Fri, STRD Fri, CSP Fri/Mon, RS Mon/Wed, PP Wed/Thu, STRG Mon/Wed/Fri; it mostly throttles trade frequency.
- Inert by construction: IC low-confidence-arm genes and `exp_profit` (condition absent from the hold-arm rule), STRG low-confidence-arm genes (arm off), strike_param when method=delta and vice versa.
- iv_rank, rel_volume, iv_rv, ADX, chan-pos and most market gates are OFF in the majority of strategies (see matrix). The time exit and DTE exit are off in most rows.
- `macro_mode` and DS weights: scattered in IC, JL, WHEEL, LP, STRD (fitness-insensitive).

## 7. Stage-2 implications (short)
1. Most reproducible regime filters: vol ratio (rv) with structure-dependent direction, ATR distance to support/resistance, 50d slope floor, vs-prior-high ceiling, and the swing-structure flag for BF/IC/VERT/BULLPS.
2. Regime-to-structure map suggested by the winners: calm + uptrend + not extended -> LC (50d); low IV rank + near resistance + not falling -> BULLCS (60d); vol expanding + bull structure + far from support -> BF (25d, Thu/Fri); vol expanding + near resistance -> STRG (40d); strong trend (ADX>30) + vol not yet spiked -> STRD (55d); no trend (ADX<15) + compressed range + IV>RV -> IC (35d); bear structure + near support -> BULLPS (45d); calm vol, anywhere -> RS/CSP/WHEEL carry.
3. Widen DTE upward (90-120) for BULLCS/STRD/JL and the cap/sizing/strike/optsel ranges in section 5; drop the dead model genes from the genome; stop searching LP (no edge) and treat IC/JL/VERT results as low-confidence.
