# Key findings (read this first; tables follow, reproducible with load.py + an.py in this folder)

1. **Horizon: nothing is long.** Every winner holds <= ~2 months (max p90 = 67d, none >90d; DTE at entry 16-59). Short (<=3w median): O_VERT (17d, DTE 16), O_WHEEL options (19.5d, but stock legs median 168d), O_IC/O_PP/O_BF borderline (23/25/29d). Mid (3w-3mo): everything else, clustering at 35-55d (O_LC 46, BULLCS 53, STRD 56). A genuine "long horizon" (LEAPS, >3 months) bucket is EMPTY in stage 1 and needs the stage 1.5 / LEAPS grid; stage-2 "long" must be filled from there.
2. **Return engine = directional/convex long-premium names:** O_LC (CAR 109%), O_STRG (66%), O_BULLCS (66%), O_BF (53%), O_STRD (42%). All have 44-57% win rates with avg win 1.4-2.4x avg loss, and 35-46% of P&L from the top 5 trades. Premium sellers (BULLPS, IC, JL, RS, CSP, VERT) earn only 4-12% CAR, 83-96% win rates, tiny size: they are carry/low-DD sleeves, not return sources. Win rates are high but each realised loss is large relative to gains (WHEEL: 99.5% option win, one -$3.2k avg loss ... stock legs hold the risk).
3. **Regimes (SPY, 6 daily regimes, no look-ahead):** O_LC earns almost ALL of its return in UP trend (low/mid vol) and ~0 in DOWN / BEAR-RALLY (Sharpe -0.03); lose in 2022 (-28%). O_BF similar (UP; negative in BEAR-RALLY, Sharpe -2.7). O_BULLCS best in PULLBACK/UP-mid-vol (Sharpe 3.2 / 2.2). Long-vol structures O_STRG/O_STRD earn in DOWN and PULLBACK (STRG Sharpe 1.3 / 4.1) and lose in UP-highV (STRG -0.6): STRG +129% in the Feb-Mar 2020 crash alone (single-event dependence), STRD +9.5%, and both +31-38% in 2022 while LC -28%. Premium sellers (CSP, RS, JL, BULLPS, IC) are positive in DOWN and BEAR-RALLY (Sharpe 1.3-3.9) and weak in calm UP-lowV (CSP Sharpe 0.2, JL 0.0, BULLPS -0.15) - they earn when IV is elevated, so a vol-conditioned switch (sell premium when vol is high/trend weak, buy convexity in calm uptrend or pullback) is supported by the data. O_WHEEL/O_CC/O_PP are long-beta (daily corr to SPY 0.80/0.75/0.36) and earn in every regime incl. DOWN (WHEEL 45%/yr, CC 58%/yr) - they are stock-beta wrappers, not option-alpha.
4. **Correlation: no redundant pairs (max monthly corr 0.57: WHEEL/CC). Highest clusters: {WHEEL, CC, CSP, JL, RS, PP} 0.4-0.57 (premium-selling + stock beta) and LC/BULLCS 0.47.** Long-vol (STRD/STRG) and VERT/IC are ~uncorrelated or negative to everything. Best blends (equal weight, daily rebalance): BF+STRG+CC Sharpe 2.3, CAR 55%, DD -13.6%; BULLCS+BF+STRG+CC Sharpe 2.5, CAR 60%, DD -12.6%; LC+WHEEL+BF+STRG Sharpe 2.4, CAR 73%, DD -13.7%; LC+BULLCS+BF+STRG best CAR/DD 5.6 (CAR 84%, DD -15%). O_BF + O_STRG recur everywhere = best diversifier pair (corr 0.17).
5. **Red flags.**
   - **Compounding/back-loading:** position size grows with equity (LC median 3 contracts in 2020-21 -> 13-21 in 2024-25); 2024+2025 = ~88% of O_LC dollar P&L, ~64% of BULLCS, ~60% of STRG. The +8,160% (LC) and +1,999-1,983% (BULLCS/STRG) totals are mostly a 2024-25 size effect; best single LC trade = 40 cheap LRCX calls ($3.75) -> +$151k, entries of 40-61 contracts on OTM options: market-impact/liquidity are NOT modelled (OI is often tens-hundreds). Treat dollar returns as upper bounds; compare per-regime % returns instead.
   - **Single-event:** O_STRG +129% in 02-19..03-31 2020 (and 2020-03 best month +60%); O_IC (14.5% of its P&L in the crash window, DELL 2020-01 trade = 23%) and O_JL (April 2025 = 24% of its dollar P&L, 2020-07 trade = 30%) and O_VERT (2022-05 = 33%, 2022 = 38% of P&L) hinge on 1-2 events. O_IC top5 trades = 76% of P&L, O_VERT 66%, only 33/16 structures: statistically thin. O_IC/O_JL/O_VERT TOP1 numbers (CAR 4-7%) are not worth stage-2 weight.
   - **Underlying concentration:** top-5 underlyings = 41-75% of option P&L everywhere (BULLCS 61%: NVDA/APP/AMZN/INTC/TSM; BF 59%: TSM/ANET/AMZN/NVDA/NFLX). Mega-cap tech/AI hindsight winners dominate; universe is 97 survivors.
   - **Assignment losses on stock legs:** for O_JL, O_RS, O_CSP, stock legs from assignment are liquidated after 3 days at a loss (JL -23.3k, RS -7.9k, CSP -8.1k) eating 70% / 41% / 28% of the option credit (JL option P&L +32.8k -> net +9.5k). The BULLPS old assignment rule also costs: 20% assigned. Premium-selling returns are overstated if you look only at option rows.
   - **O_PP:** option legs lose (-12k, 5.6% win rate, pure insurance cost) but the stock legs carry it (+58k); exit mix 41% stop-loss on stock. It is stock beta with a hedge, not an option strategy.
   - **Data oddities:** O_BF min pnl_pct -130% (more than the debit; check net_price sign for multi-leg rows with a short ratio leg), O_BULLCS -80%, O_VERT -62%/+64% on 2 legs; tiny-price fills (<= $0.05 entry) only 1 in IC and 1 in JL - fine. Some BF/BULLCS legs show 'exercised' at 28-60d on short legs (assigned 14-31%): early exercise/assignment behaviour is modelled but affects 20-30% of BF/BULLCS structures; the stated "exit_reason" for multi-leg rows is a per-leg mode so reasons are noisy.
   - **Robustness TOP1 vs TOP2/3:** consistent for LC/BULLCS/BULLPS/WHEEL/BF/RS/STRG/CC (CAR within +-10%); less stable: CSP (12.5 vs 17.1), STRD (42 vs 50, DD -25 vs -41), PP (22 vs 25), JL (6.7 vs 5.3). Only STRD has no TOP3.
6. **Design implication for stage 2 (regime x horizon expert):** regime gate on SPY trend x vol; (a) UP & low/mid vol: LC / BULLCS / BF (mid horizon 30-55d); (b) PULLBACK: BULLCS, STRG/STRD (long vol); (c) DOWN / high vol: STRG/STRD long-vol plus CSP/RS/JL credit sleeves and CC/PP wrappers, NOT LC/BF; (d) BEAR-RALLY: premium-selling (BULLPS, CSP, RS, WHEEL), avoid LC/BF/STRG. Short horizon needs VERT (thin data, 16 trades) or IC, and the long bucket (LEAPS) must come from stage 1.5. Size-vs-liquidity caps are needed before trusting LC/BULLCS dollar numbers.

Caveats: regimes are daily, SPY-based (FMPOHLCVProvider/SPY_1d.parquet), vol terciles use 2020-2025 only; equity-based figures (sections 1b, 2, 3) use TOP1 equity curves on a business-day grid with ffill; structure P&L = sum of leg pnl per transaction_id; "stock legs" rows are separate transactions. 2a sums of daily returns are additive approximations, dominated by the late, large-equity period only in dollar terms (2e), not in 2a-2d.

---
# Stage-1 option grid winners: trades, regimes, correlation (TOP1)

Source: dl_forecasting.db (read-only), backtests TOP1 ids 10=O_LC, 18=O_BULLCS, 39=O_BULLPS, 48=O_WHEEL, 61=O_BF, 72=O_IC, 82=O_JL, 93=O_RS, 105=O_CSP, 111=O_STRD, 119=O_STRG, 126=O_CC, 128=O_PP, 141=O_VERT. Window 2020-01-02..2025-12-31, $20k start. Trades are one row per LEG; aggregated per transaction_id into structures (multi-leg structures: 2-4 rows share a transaction_id - verified). For O_CC / O_PP / O_WHEEL the stock legs carry their OWN transaction_ids (rows with no option_type), so those tables split 'opt' (option structures) from '(stock legs)'. Hold = calendar days entry->exit; OTM% = signed moneyness of the primary (long/first) leg, positive = out of the money.

## 1. Per-strategy trade profile (structure level)

| strat | n | days med | p25 | p75 | dte med | dte rng | OTM% med | win% | avg win$ | avg loss$ | sum pnl$ | top5 trades % of own pnl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| O_LC | 405 | 46.0 | 17.0 | 53.0 | 52.0 | 44-59 | 5.1 | 46.4 | 16,682.4 | -6,914.5 | 1,635,837.0 | 34.6 |
| O_BULLCS | 217 | 53.0 | 46.0 | 60.0 | 59.0 | 52-66 | 2.6 | 57.1 | 5,266.2 | -2,665.1 | 405,147.9 | 37.3 |
| O_BULLPS | 64 | 39.0 | 37.0 | 44.0 | 38.0 | 35-53 | 9.3 | 82.8 | 278.0 | -184.0 | 12,710.8 | 34.6 |
| O_WHEEL | 214 | 19.5 | 10.0 | 39.0 | 24.0 | 10-53 | 8.3 | 99.5 | 176.1 | -3,162.2 | 34,349.6 | 13.3 |
| O_WHEEL (stock legs) | 24 | 168.0 | 98.0 | 267.8 |  |  |  | 79.2 | 3,010.1 | -2,022.0 | 47,081.4 | 57.5 |
| O_BF | 127 | 29.0 | 22.0 | 35.0 | 28.0 | 18-35 | -6.1 | 66.9 | 4,007.4 | -2,461.9 | 237,227.1 | 42.9 |
| O_IC | 33 | 23.0 | 16.0 | 32.0 | 36.0 | 25-45 | 10.1 | 72.7 | 341.3 | -251.4 | 5,928.8 | 75.9 |
| O_JL | 55 | 34.0 | 21.5 | 38.0 | 39.0 | 31-50 | 5.2 | 94.5 | 657.7 | -455.0 | 32,832.9 | 29.8 |
| O_JL (stock legs) | 24 | 3.0 | 3.0 | 3.0 |  |  |  | 8.3 | 98.2 | -1,069.4 | -23,331.3 |  |
| O_RS | 144 | 35.0 | 28.0 | 40.0 | 36.0 | 29-45 | 6.3 | 95.8 | 145.4 | -171.7 | 19,029.5 | 31.9 |
| O_RS (stock legs) | 6 | 3.0 | 3.0 | 3.0 |  |  |  | 0.0 |  | -1,314.2 | -7,885.1 |  |
| O_CSP | 113 | 32.0 | 31.0 | 36.0 | 39.0 | 38-53 | 0.3 | 83.2 | 375.5 | -351.1 | 28,626.8 | 18.6 |
| O_CSP (stock legs) | 9 | 3.0 | 3.0 | 3.0 |  |  |  | 0.0 |  | -903.2 | -8,128.6 |  |
| O_STRD | 174 | 56.0 | 49.0 | 63.0 | 53.0 | 45-60 | -0.7 | 55.7 | 3,575.2 | -2,640.0 | 143,518.8 | 44.0 |
| O_STRG | 337 | 39.0 | 35.0 | 46.0 | 38.0 | 31-50 | -0.7 | 44.2 | 6,041.4 | -2,667.2 | 398,734.8 | 46.4 |
| O_CC | 232 | 39.0 | 32.0 | 46.0 | 38.0 | 30-50 | 11.1 | 98.7 | 98.7 | -48.4 | 22,459.3 | 19.0 |
| O_CC (stock legs) | 101 | 77.0 | 53.0 | 143.0 |  |  |  | 65.3 | 1,199.8 | -828.6 | 50,181.6 | 43.2 |
| O_PP | 337 | 25.0 | 22.0 | 31.0 | 24.0 | 15-35 | 9.4 | 5.6 | 396.4 | -61.6 | -12,043.0 |  |
| O_PP (stock legs) | 123 | 37.0 | 20.0 | 92.5 |  |  |  | 57.7 | 1,349.9 | -722.9 | 58,248.1 | 22.6 |
| O_VERT | 16 | 17.0 | 17.0 | 21.0 | 16.0 | 15-30 | 3.0 | 81.2 | 624.6 | -171.5 | 7,605.3 | 65.7 |

| strat | n und | max conc | reasons |
|---|---|---|---|
| O_LC | 66 | 19 | expired_otm 48%, take_profit 39%, exercised 11%, open_at_end 2% |
| O_BULLCS | 69 | 16 | expired_otm 33%, take_profit 29%, assigned 19%, exercised 14% |
| O_BULLPS | 37 | 4 | expired_otm 78%, assigned 20%, open_at_end 2% |
| O_WHEEL | 33 | 5 | take_profit 39%, expired_otm 37%, assigned 22%, open_at_end 1% |
| O_WHEEL (stock legs) | 17 | 6 | exit 96%, open_at_end 4% |
| O_BF | 59 | 7 | expired_otm 56%, assigned 31%, exercised 8%, take_profit 5% |
| O_IC | 25 | 3 | time_exit 39%, expired_otm 36%, stop_loss 12%, dte_exit 6% |
| O_JL | 29 | 3 | take_profit 51%, expired_otm 35%, assigned 9%, stop_loss 5% |
| O_JL (stock legs) | 19 | 1 | exit 100% |
| O_RS | 31 | 7 | dte_exit 65%, expired_otm 28%, assigned 4%, exercised 2% |
| O_RS (stock legs) | 5 | 1 | exit 100% |
| O_CSP | 38 | 4 | time_exit 84%, assigned 8%, expired_otm 6%, open_at_end 2% |
| O_CSP (stock legs) | 7 | 1 | exit 100% |
| O_STRD | 71 | 11 | exercised 83%, take_profit 16%, open_at_end 1% |
| O_STRG | 89 | 15 | exercised 91%, expired_otm 6%, open_at_end 3% |
| O_CC | 31 | 7 | expired_otm 72%, assigned 26%, open_at_end 2% |
| O_CC (stock legs) | 31 | 7 | exit 66%, stop_loss 28%, open_at_end 6% |
| O_PP | 32 | 9 | expired_otm 91%, exercised 8%, open_at_end 1% |
| O_PP (stock legs) | 33 | 7 | exit 54%, stop_loss 41%, open_at_end 5% |
| O_VERT | 13 | 3 | assigned 62%, exercised 31%, expired_otm 6% |

Top-5 underlyings by P&L (option rows):

- O_LC: GOOGL 175,385, LRCX 157,394, TSM 151,464, IBM 149,691, AVGO 128,307  (top-5 = 47% of option P&L)
- O_BULLCS: NVDA 71,627, APP 45,881, AMZN 43,873, INTC 43,187, TSM 41,728  (top-5 = 61% of option P&L)
- O_BULLPS: MU 1,720, AMD 1,417, GE 1,403, MRVL 996, BA 873  (top-5 = 50% of option P&L)
- O_WHEEL: ANET 6,924, MRVL 4,775, AMD 3,242, APP 3,171, PLTR 2,767  (top-5 = 61% of option P&L)
- O_BF: TSM 39,165, ANET 29,897, AMZN 25,308, NVDA 24,055, NFLX 21,824  (top-5 = 59% of option P&L)
- O_IC: DELL 1,356, JNJ 1,149, MS 980, MU 550, WDC 396  (top-5 = 75% of option P&L)
- O_JL: INTC 6,317, PLTR 2,936, C 2,819, T 2,423, CSCO 2,230  (top-5 = 51% of option P&L)
- O_RS: MRVL 4,868, PLTR 2,138, WDC 1,552, CVX 1,527, TSM 1,262  (top-5 = 60% of option P&L)
- O_CSP: MRVL 3,762, GE 2,486, PLTR 2,386, APP 2,227, T 2,178  (top-5 = 46% of option P&L)
- O_STRD: LLY 19,664, NFLX 15,221, AAPL 14,753, PLTR 13,910, ABBV 13,219  (top-5 = 53% of option P&L)
- O_STRG: LLY 51,415, CRWD 49,595, GOOGL 49,047, AVGO 42,035, HON 35,600  (top-5 = 57% of option P&L)
- O_CC: APP 2,566, MRVL 2,134, SCCO 1,561, NVDA 1,526, STX 1,380  (top-5 = 41% of option P&L)
- O_PP: AMAT 1,065, MRK 725, NVO 468, RIO 310, TSM 149  (top-5 = -23% of option P&L)
- O_VERT: DELL 1,480, C 1,375, SCHW 964, DIS 876, WFC 672  (top-5 = 71% of option P&L)

## 1b. Equity-curve stats

| strat | final$ | total ret% | CAR% | maxDD% | Sharpe | worst month | best month | longest underwater (d) | % months + |
|---|---|---|---|---|---|---|---|---|---|
| O_LC | 1,652,153.0 | 8,160.8 | 108.8 | -41.5 | 1.5 | 2024-04 -29.0% | 2020-08 105.5% | 532 | 59.7 |
| O_BULLCS | 419,812.5 | 1,999.1 | 66.1 | -33.6 | 1.4 | 2021-09 -25.6% | 2021-10 40.7% | 283 | 59.7 |
| O_BULLPS | 32,723.4 | 63.6 | 8.6 | -6.4 | 1.3 | 2025-07 -1.5% | 2024-09 5.1% | 406 | 69.4 |
| O_WHEEL | 107,421.7 | 437.1 | 32.4 | -25.2 | 1.3 | 2022-04 -12.2% | 2023-01 16.0% | 290 | 72.2 |
| O_BF | 257,227.1 | 1,186.1 | 53.1 | -28.6 | 1.5 | 2021-01 -16.5% | 2023-06 37.6% | 459 | 58.3 |
| O_IC | 25,698.8 | 28.5 | 4.3 | -9.7 | 0.6 | 2021-10 -6.2% | 2021-12 8.3% | 302 | 31.9 |
| O_JL | 29,501.6 | 47.5 | 6.7 | -9.6 | 0.6 | 2022-04 -5.6% | 2023-03 9.1% | 272 | 44.4 |
| O_RS | 31,144.4 | 55.7 | 7.7 | -6.1 | 1.1 | 2020-09 -3.5% | 2023-01 5.6% | 189 | 77.8 |
| O_CSP | 40,496.2 | 102.5 | 12.5 | -12.0 | 1.1 | 2020-03 -5.4% | 2020-11 9.1% | 241 | 66.7 |
| O_STRD | 164,033.0 | 720.2 | 42.0 | -25.4 | 1.1 | 2022-12 -12.7% | 2022-04 35.7% | 418 | 56.9 |
| O_STRG | 416,636.0 | 1,983.2 | 65.9 | -39.2 | 1.2 | 2020-09 -25.3% | 2020-03 60.1% | 326 | 56.9 |
| O_CC | 92,602.9 | 363.0 | 29.1 | -23.6 | 1.3 | 2021-09 -7.5% | 2022-10 15.6% | 237 | 73.6 |
| O_PP | 66,159.2 | 230.8 | 22.1 | -13.6 | 1.3 | 2022-06 -5.4% | 2022-11 17.0% | 206 | 69.4 |
| O_VERT | 27,605.3 | 38.0 | 5.5 | -5.6 | 0.8 | 2020-04 -3.1% | 2022-05 9.7% | 366 | 22.2 |

## 1c. Robustness: TOP1 vs TOP2/TOP3 of the same optimization

| strat | final$ | CAR% | maxDD% | Sharpe | n struct | top5 trade share% |
|---|---|---|---|---|---|---|
| O_LC TOP1 | 1,652,153.0 | 108.8 | -41.5 | 1.6 | 405.0 | 34.6 |
| O_LC TOP2 | 1,749,716.7 | 110.8 | -33.4 | 1.7 | 291.0 | 42.8 |
| O_LC TOP3 | 1,226,736.5 | 98.7 | -33.6 | 1.7 | 342.0 | 39.8 |
| O_BULLCS TOP1 | 419,812.5 | 66.1 | -33.6 | 1.6 | 217.0 | 37.3 |
| O_BULLCS TOP2 | 400,893.1 | 64.9 | -34.5 | 1.4 | 196.0 | 39.8 |
| O_BULLCS TOP3 | 411,766.9 | 65.6 | -33.8 | 1.4 | 221.0 | 42.3 |
| O_BULLPS TOP1 | 32,723.4 | 8.6 | -6.4 | 1.4 | 64.0 | 34.6 |
| O_BULLPS TOP2 | 31,372.7 | 7.8 | -3.7 | 1.5 | 53.0 | 34.7 |
| O_BULLPS TOP3 | 30,610.2 | 7.4 | -6.9 | 1.2 | 79.0 | 38.7 |
| O_WHEEL TOP1 | 107,421.7 | 32.4 | -25.2 | 1.3 | 238.0 | 33.2 |
| O_WHEEL TOP3 | 103,199.9 | 31.5 | -23.2 | 1.3 | 174.0 | 30.6 |
| O_WHEEL TOP2 | 106,636.9 | 32.2 | -27.9 | 1.3 | 238.0 | 30.7 |
| O_BF TOP1 | 257,227.1 | 53.1 | -28.6 | 1.7 | 127.0 | 42.9 |
| O_BF TOP2 | 256,418.0 | 53.0 | -30.8 | 1.5 | 120.0 | 42.9 |
| O_BF TOP3 | 309,054.2 | 57.9 | -33.1 | 1.6 | 130.0 | 46.5 |
| O_IC TOP1 | 25,698.8 | 4.3 | -9.7 | 0.7 | 33.0 | 75.9 |
| O_IC TOP2 | 25,022.1 | 3.8 | -7.0 | 0.9 | 32.0 | 69.0 |
| O_IC TOP3 | 24,647.1 | 3.5 | -9.7 | 0.6 | 37.0 | 75.1 |
| O_JL TOP1 | 29,501.6 | 6.7 | -9.6 | 0.7 | 79.0 | 103.0 |
| O_JL TOP3 | 27,556.3 | 5.5 | -9.1 | 0.7 | 66.0 | 124.1 |
| O_JL TOP2 | 27,186.9 | 5.3 | -7.4 | 0.8 | 57.0 | 117.9 |
| O_RS TOP1 | 31,144.4 | 7.7 | -6.1 | 1.2 | 150.0 | 54.4 |
| O_RS TOP3 | 30,753.3 | 7.4 | -6.3 | 1.2 | 146.0 | 56.2 |
| O_RS TOP2 | 31,023.3 | 7.6 | -6.1 | 1.2 | 149.0 | 55.0 |
| O_CSP TOP1 | 40,496.2 | 12.5 | -12.0 | 1.2 | 122.0 | 26.0 |
| O_CSP TOP3 | 51,481.8 | 17.1 | -17.3 | 1.3 | 122.0 | 35.6 |
| O_CSP TOP2 | 43,927.2 | 14.0 | -19.9 | 1.0 | 126.0 | 35.6 |
| O_STRD TOP1 | 164,033.0 | 42.0 | -25.4 | 1.2 | 174.0 | 44.0 |
| O_STRD TOP2 | 228,192.3 | 50.1 | -41.0 | 1.0 | 363.0 | 51.7 |
| O_STRG TOP1 | 416,636.0 | 65.9 | -39.2 | 1.3 | 337.0 | 46.4 |
| O_STRG TOP2 | 529,526.3 | 72.7 | -39.4 | 1.3 | 337.0 | 46.9 |
| O_STRG TOP3 | 461,022.1 | 68.8 | -40.9 | 1.3 | 344.0 | 47.9 |
| O_CC TOP1 | 92,602.9 | 29.1 | -23.6 | 1.3 | 333.0 | 29.9 |
| O_CC TOP2 | 78,444.6 | 25.6 | -18.7 | 1.3 | 291.0 | 38.0 |
| O_CC TOP3 | 86,819.3 | 27.7 | -27.1 | 1.2 | 324.0 | 33.8 |
| O_PP TOP1 | 66,159.2 | 22.1 | -13.6 | 1.3 | 460.0 | 28.5 |
| O_PP TOP2 | 62,204.5 | 20.8 | -21.2 | 1.1 | 618.0 | 27.5 |
| O_PP TOP3 | 78,179.3 | 25.5 | -21.1 | 1.2 | 654.0 | 36.3 |
| O_VERT TOP1 | 27,605.3 | 5.5 | -5.6 | 1.0 | 16.0 | 65.7 |
| O_VERT TOP2 | 26,831.9 | 5.0 | -3.6 | 1.1 | 15.0 | 67.2 |
| O_VERT TOP3 | 26,615.1 | 4.9 | -3.6 | 1.1 | 15.0 | 66.7 |

## 2. Market regimes (SPY daily, FMPOHLCVProvider/SPY_1d.parquet)

Trend: UP = close>SMA200 and SMA50 rising (10d diff>0); PULLBACK = above SMA200, SMA50 falling; BEAR-RALLY = below SMA200, SMA50 rising; DOWN = below and falling. Vol = SPY 20d realised vol (ann.) terciles over 2020-2025: low < 11.8%, mid < 17.3%, high above. Six regimes = UP x 3 vol buckets + PULLBACK + BEAR-RALLY + DOWN. Regime for day t is the one known at close t-1 (no look-ahead). Daily classification, not monthly.

Days per regime: UP-lowV 516, UP-midV 438, DOWN 260, UP-highV 161, BEAR-RALLY 96, PULLBACK 94

### 2a. Sum of daily equity returns (%-points) per regime

| reg6 | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BEAR-RALLY | -0.2 | 10.2 | 6.3 | 13.9 | -8.8 | 7.6 | 7.5 | 7.5 | 11.4 | 1.4 | -5.7 | -1.2 | 17.0 | -0.0 |
| DOWN | -0.4 | 16.3 | 13.5 | 46.7 | 16.7 | 0.0 | 25.5 | 16.8 | 21.3 | 66.2 | 96.7 | 60.1 | 26.4 | 23.5 |
| PULLBACK | 8.3 | 38.6 | 2.8 | 0.6 | 2.6 | -6.1 | 5.2 | -2.6 | 4.4 | 26.3 | 56.6 | 5.7 | -0.8 | 4.8 |
| UP-highV | 11.3 | 15.6 | 5.6 | 24.6 | 68.5 | 2.7 | -0.1 | 4.0 | 30.2 | 17.1 | -19.9 | 20.8 | 18.5 | -1.8 |
| UP-lowV | 273.9 | 107.2 | -1.4 | 54.2 | 73.2 | 13.4 | 0.4 | 5.4 | 3.6 | 89.3 | 167.9 | 27.8 | 26.9 | 1.6 |
| UP-midV | 237.8 | 163.8 | 23.6 | 45.1 | 133.5 | 9.1 | 4.1 | 14.7 | 2.9 | 51.1 | 78.4 | 53.7 | 37.0 | 5.6 |
| TOTAL | 530.8 | 351.7 | 50.4 | 185.1 | 285.6 | 26.6 | 42.5 | 45.7 | 73.9 | 251.4 | 374.1 | 167.0 | 125.1 | 33.7 |

### 2b. Annualised mean daily return (%) within regime

| reg6 | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BEAR-RALLY | -0.4 | 26.9 | 16.4 | 36.5 | -23.2 | 19.8 | 19.7 | 19.6 | 30.0 | 3.7 | -14.8 | -3.0 | 44.7 | -0.1 |
| DOWN | -0.4 | 15.8 | 13.0 | 45.2 | 16.2 | 0.0 | 24.7 | 16.3 | 20.7 | 64.2 | 93.7 | 58.3 | 25.6 | 22.8 |
| PULLBACK | 22.2 | 103.4 | 7.6 | 1.7 | 7.1 | -16.5 | 13.9 | -7.1 | 11.8 | 70.5 | 151.8 | 15.3 | -2.1 | 12.8 |
| UP-highV | 17.7 | 24.5 | 8.7 | 38.5 | 107.3 | 4.2 | -0.1 | 6.2 | 47.2 | 26.8 | -31.1 | 32.6 | 28.9 | -2.8 |
| UP-lowV | 133.8 | 52.3 | -0.7 | 26.5 | 35.7 | 6.5 | 0.2 | 2.6 | 1.8 | 43.6 | 82.0 | 13.6 | 13.1 | 0.8 |
| UP-midV | 136.8 | 94.2 | 13.6 | 25.9 | 76.8 | 5.2 | 2.4 | 8.5 | 1.7 | 29.4 | 45.1 | 30.9 | 21.3 | 3.2 |

### 2c. Annualised Sharpe within regime

| reg6 | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BEAR-RALLY | -0.03 | 0.96 | 3.94 | 1.81 | -2.69 | 2.70 | 1.41 | 1.80 | 3.44 | 0.09 | -0.30 | -0.18 | 2.57 | -0.03 |
| DOWN | -0.03 | 1.02 | 1.40 | 1.07 | 0.82 | 0.02 | 1.32 | 1.44 | 1.26 | 1.16 | 1.32 | 1.75 | 1.20 | 1.50 |
| PULLBACK | 0.81 | 3.15 | 1.52 | 0.08 | 0.37 | -2.08 | 1.28 | -1.21 | 1.25 | 2.55 | 4.08 | 0.80 | -0.15 | 2.09 |
| UP-highV | 0.57 | 0.66 | 1.75 | 2.16 | 3.38 | 0.80 | -0.01 | 0.89 | 4.40 | 1.11 | -0.62 | 1.54 | 1.98 | -0.93 |
| UP-lowV | 1.88 | 1.13 | -0.15 | 1.71 | 1.00 | 0.86 | 0.03 | 0.79 | 0.22 | 1.46 | 2.11 | 0.90 | 1.04 | 0.24 |
| UP-midV | 2.05 | 2.15 | 2.25 | 1.53 | 2.14 | 0.62 | 0.32 | 2.06 | 0.20 | 0.84 | 1.02 | 1.68 | 1.51 | 1.55 |

### 2d. Compounded return (%) in event windows / years

|  | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2020 crash 02-19..03-31 | -14.9 | -9.3 | 1.0 | -11.7 | 1.9 | 4.0 | -3.8 | -0.2 | -8.1 | 9.5 | 128.7 | -10.4 | 0.8 | 0.0 |
| 2020 rebound 04-01..12-31 | 126.4 | 8.3 | 8.4 | 69.4 | 43.7 | 0.9 | 15.2 | 8.3 | 20.5 | 39.3 | -5.7 | 40.6 | 10.4 | 6.6 |
| 2021 | 159.5 | 172.7 | 7.1 | 17.2 | 73.6 | 0.5 | 4.4 | 5.5 | 11.2 | 20.0 | 136.6 | 19.4 | 18.9 | 2.5 |
| 2022 bear (full yr) | -28.4 | 3.0 | 12.9 | -1.9 | -7.2 | 5.5 | -1.0 | 5.9 | 17.7 | 38.0 | 31.2 | 17.7 | 24.3 | 13.4 |
| 2023 | 103.5 | 113.2 | 7.7 | 53.9 | 87.0 | 8.4 | 11.1 | 12.0 | 14.7 | 30.6 | 52.9 | 27.1 | 25.4 | 3.9 |
| 2024 | 275.4 | 44.8 | 7.5 | 30.9 | 67.9 | 1.4 | 3.5 | 7.3 | 7.9 | 83.4 | 27.4 | 33.9 | 27.3 | 3.9 |
| 2025 Jan-Mar | 11.1 | 2.3 | 0.2 | 2.8 | 7.0 | 0.4 | 1.3 | 2.5 | 5.0 | 2.5 | 4.1 | -1.2 | -2.8 | 1.5 |
| 2025-04 tariff shock | 2.1 | -1.2 | -1.2 | -1.8 | 0.0 | 0.0 | 8.7 | 0.2 | 0.2 | 2.3 | -0.1 | 3.0 | 2.0 | 3.2 |
| 2025 May-Dec | 91.0 | 72.8 | 7.8 | 49.9 | 46.3 | 2.2 | 1.8 | 3.8 | 5.3 | 30.4 | 82.5 | 42.8 | 19.2 | -1.5 |

### 2e. Share of total dollar P&L earned in each window (%) (dollar P&L is back-loaded because equity compounds)

|  | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2020 crash 02-19..03-31 | -0.3 | -0.7 | 1.6 | -2.7 | 0.2 | 14.5 | -8.0 | -0.4 | -8.0 | 1.3 | 5.5 | -3.0 | 0.4 | 0.0 |
| 2020 rebound 04-01..12-31 | 1.8 | 0.5 | 13.3 | 14.4 | 4.2 | 3.2 | 30.7 | 15.0 | 18.8 | 5.9 | -0.6 | 10.6 | 4.9 | 17.5 |
| 2021 | 5.2 | 12.0 | 12.2 | 6.0 | 10.1 | 1.8 | 10.2 | 10.7 | 12.3 | 4.2 | 12.5 | 7.1 | 9.8 | 7.0 |
| 2022 bear (full yr) | -2.4 | 0.6 | 23.8 | -0.8 | -1.7 | 20.9 | -2.3 | 12.2 | 21.7 | 9.6 | 6.8 | 7.8 | 15.0 | 38.4 |
| 2023 | 6.3 | 22.0 | 16.0 | 21.7 | 19.2 | 33.6 | 26.7 | 26.2 | 21.2 | 10.6 | 15.0 | 14.0 | 19.4 | 12.7 |
| 2024 | 34.3 | 18.6 | 16.9 | 19.2 | 28.0 | 6.0 | 9.3 | 17.9 | 13.0 | 37.9 | 11.9 | 22.2 | 26.2 | 13.2 |
| 2025 Jan-Mar | 5.2 | 1.4 | 0.5 | 2.3 | 4.9 | 1.8 | 3.5 | 6.6 | 8.9 | 2.1 | 2.3 | -1.0 | -3.4 | 5.4 |
| 2025-04 tariff shock | 1.1 | -0.7 | -2.9 | -1.5 | 0.0 | 0.0 | 24.3 | 0.4 | 0.5 | 2.0 | -0.0 | 2.6 | 2.4 | 11.4 |
| 2025 May-Dec | 48.2 | 44.3 | 18.6 | 40.9 | 34.3 | 9.5 | 5.5 | 10.3 | 9.9 | 26.5 | 47.5 | 38.2 | 23.2 | -5.6 |

### 2f. By ENTRY-date regime: n trades / win% / mean structure P&L as % of equity at entry

| reg | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BEAR-RALLY | 8 / 38% / 0.26 | 14 / 36% / 0.18 | 7 / 86% / 0.65 | 15 / 100% / 0.25 | 6 / 50% / -0.74 | 2 / 100% / 1.63 | 4 / 100% / 3.74 | 11 / 100% / 1.29 | 10 / 70% / 0.61 | 10 / 60% / 0.85 | 16 / 31% / 0.44 | 11 / 100% / 0.21 | 25 / 8% / 0.15 | 1 / 100% / 1.98 |
| DOWN | 20 / 50% / 1.36 | 9 / 44% / 1.8 | 13 / 92% / 1.21 | 26 / 100% / 0.37 | 4 / 50% / -0.15 | 3 / 67% / 0.09 | 11 / 100% / 2.93 | 32 / 94% / 0.56 | 24 / 96% / 1.46 | 40 / 52% / 1.31 | 62 / 52% / 0.93 | 31 / 100% / 0.25 | 45 / 7% / -0.16 | 5 / 100% / 3.93 |
| PULLBACK | 13 / 31% / 0.81 | 16 / 69% / 3.25 | 3 / 67% / 0.41 | 10 / 100% / 0.52 | 4 / 100% / 7.49 | 2 / 100% / 1.06 | 3 / 100% / 1.13 | 10 / 100% / 1.09 | 5 / 60% / 0.79 | 7 / 43% / -0.66 | 21 / 48% / 1.91 | 19 / 100% / 0.22 | 23 / 17% / -0.11 | 2 / 100% / 2.7 |
| UP-highV | 35 / 34% / -0.27 | 14 / 43% / 0.44 | 6 / 83% / 0.93 | 22 / 100% / 0.43 | 9 / 56% / 1.33 | 4 / 75% / 0.73 | 7 / 86% / 4.25 | 15 / 100% / 0.23 | 13 / 92% / 0.85 | 10 / 70% / 1.31 | 20 / 60% / 1.86 | 17 / 100% / 0.23 | 36 / 3% / -0.1 | 2 / 0% / -0.19 |
| UP-lowV | 204 / 50% / 1.28 | 106 / 56% / 1.72 | 12 / 83% / 0.66 | 71 / 99% / 0.25 | 67 / 70% / 2.62 | 14 / 79% / 1.18 | 17 / 94% / 1.89 | 37 / 92% / 0.44 | 28 / 82% / 0.79 | 58 / 57% / 1.86 | 125 / 45% / 1.71 | 87 / 98% / 0.21 | 115 / 4% / -0.1 | 5 / 80% / 1.19 |
| UP-midV | 125 / 46% / 2.04 | 58 / 67% / 1.46 | 23 / 78% / 0.65 | 70 / 100% / 0.34 | 37 / 65% / 1.9 | 8 / 50% / 0.21 | 13 / 92% / 2.07 | 39 / 97% / 0.41 | 33 / 79% / 0.77 | 49 / 55% / 1.18 | 93 / 37% / 0.34 | 67 / 99% / 0.23 | 93 / 4% / -0.06 | 1 / 100% / 0.59 |

## 3. Correlation of monthly returns (TOP1 equity curves)

|  | O_LC | O_BULLCS | O_BULLPS | O_WHEEL | O_BF | O_IC | O_JL | O_RS | O_CSP | O_STRD | O_STRG | O_CC | O_PP | O_VERT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| O_LC | 1.00 | 0.47 | 0.08 | 0.21 | 0.22 | -0.01 | 0.10 | 0.10 | 0.14 | 0.09 | 0.15 | 0.25 | 0.15 | -0.11 |
| O_BULLCS | 0.47 | 1.00 | 0.33 | 0.31 | 0.12 | -0.27 | 0.09 | 0.16 | 0.16 | 0.09 | 0.27 | 0.18 | 0.30 | -0.17 |
| O_BULLPS | 0.08 | 0.33 | 1.00 | 0.25 | -0.02 | -0.25 | 0.12 | 0.10 | 0.19 | 0.13 | 0.19 | 0.22 | 0.29 | -0.08 |
| O_WHEEL | 0.21 | 0.31 | 0.25 | 1.00 | 0.23 | -0.12 | 0.44 | 0.48 | 0.46 | 0.20 | 0.18 | 0.57 | 0.47 | -0.26 |
| O_BF | 0.22 | 0.12 | -0.02 | 0.23 | 1.00 | 0.27 | 0.16 | 0.01 | 0.18 | -0.08 | 0.17 | 0.18 | 0.22 | -0.10 |
| O_IC | -0.01 | -0.27 | -0.25 | -0.12 | 0.27 | 1.00 | -0.12 | 0.00 | -0.03 | 0.12 | -0.08 | -0.02 | 0.05 | -0.11 |
| O_JL | 0.10 | 0.09 | 0.12 | 0.44 | 0.16 | -0.12 | 1.00 | 0.45 | 0.45 | 0.07 | 0.09 | 0.42 | 0.31 | 0.04 |
| O_RS | 0.10 | 0.16 | 0.10 | 0.48 | 0.01 | 0.00 | 0.45 | 1.00 | 0.27 | -0.06 | 0.04 | 0.16 | 0.39 | -0.19 |
| O_CSP | 0.14 | 0.16 | 0.19 | 0.46 | 0.18 | -0.03 | 0.45 | 0.27 | 1.00 | 0.19 | -0.13 | 0.50 | 0.29 | 0.07 |
| O_STRD | 0.09 | 0.09 | 0.13 | 0.20 | -0.08 | 0.12 | 0.07 | -0.06 | 0.19 | 1.00 | 0.20 | 0.28 | 0.13 | -0.09 |
| O_STRG | 0.15 | 0.27 | 0.19 | 0.18 | 0.17 | -0.08 | 0.09 | 0.04 | -0.13 | 0.20 | 1.00 | 0.27 | 0.11 | -0.15 |
| O_CC | 0.25 | 0.18 | 0.22 | 0.57 | 0.18 | -0.02 | 0.42 | 0.16 | 0.50 | 0.28 | 0.27 | 1.00 | 0.40 | -0.07 |
| O_PP | 0.15 | 0.30 | 0.29 | 0.47 | 0.22 | 0.05 | 0.31 | 0.39 | 0.29 | 0.13 | 0.11 | 0.40 | 1.00 | -0.14 |
| O_VERT | -0.11 | -0.17 | -0.08 | -0.26 | -0.10 | -0.11 | 0.04 | -0.19 | 0.07 | -0.09 | -0.15 | -0.07 | -0.14 | 1.00 |

Redundant pairs (corr > 0.8): none

Pairs 0.6-0.8: 

Daily-return correlation with SPY: O_LC 0.27, O_BULLCS 0.34, O_BULLPS 0.46, O_WHEEL 0.80, O_BF 0.10, O_IC 0.02, O_JL 0.50, O_RS 0.34, O_CSP 0.60, O_STRD -0.07, O_STRG -0.26, O_CC 0.75, O_PP 0.36, O_VERT -0.22

## 3b. Best equal-weight blends (daily-rebalanced mean of daily returns)

Singles for reference:

|  | Sharpe | CAR% | maxDD% | CAR/DD |
|---|---|---|---|---|
| O_LC | 1.5 | 108.8 | -41.5 | 2.6 |
| O_BULLCS | 1.4 | 66.1 | -33.6 | 2.0 |
| O_BULLPS | 1.3 | 8.6 | -6.4 | 1.3 |
| O_WHEEL | 1.3 | 32.4 | -25.2 | 1.3 |
| O_BF | 1.5 | 53.1 | -28.6 | 1.9 |
| O_IC | 0.6 | 4.3 | -9.7 | 0.4 |
| O_JL | 0.6 | 6.7 | -9.6 | 0.7 |
| O_RS | 1.1 | 7.7 | -6.1 | 1.2 |
| O_CSP | 1.1 | 12.5 | -12.0 | 1.0 |
| O_STRD | 1.1 | 42.0 | -25.4 | 1.7 |
| O_STRG | 1.2 | 65.9 | -39.2 | 1.7 |
| O_CC | 1.3 | 29.1 | -23.6 | 1.2 |
| O_PP | 1.3 | 21.7 | -13.6 | 1.6 |
| O_VERT | 0.8 | 5.5 | -5.6 | 1.0 |

### top 6 by Sharpe, k=2

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_BULLCS+O_BF | 2.0 | 64.2 | -18.7 | 3.4 | 0.1 |
| O_LC+O_BF | 1.9 | 86.5 | -27.2 | 3.2 | 0.2 |
| O_LC+O_STRG | 1.9 | 97.3 | -25.9 | 3.8 | 0.1 |
| O_BF+O_CC | 1.9 | 43.0 | -15.6 | 2.8 | 0.2 |
| O_WHEEL+O_BF | 1.8 | 44.9 | -19.8 | 2.3 | 0.2 |
| O_BULLCS+O_STRG | 1.8 | 74.0 | -21.6 | 3.4 | 0.3 |

### top 6 by CAR/maxDD, k=2

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_LC+O_BULLCS | 1.8 | 92.9 | -23.9 | 3.9 | 0.5 |
| O_LC+O_STRG | 1.9 | 97.3 | -25.9 | 3.8 | 0.1 |
| O_LC+O_STRD | 1.8 | 80.5 | -22.0 | 3.7 | 0.1 |
| O_BULLCS+O_BF | 2.0 | 64.2 | -18.7 | 3.4 | 0.1 |
| O_BULLCS+O_STRG | 1.8 | 74.0 | -21.6 | 3.4 | 0.3 |
| O_LC+O_BF | 1.9 | 86.5 | -27.2 | 3.2 | 0.2 |

### top 6 by Sharpe, k=3

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_BF+O_STRG+O_CC | 2.3 | 55.2 | -13.6 | 4.0 | 0.3 |
| O_WHEEL+O_BF+O_STRG | 2.2 | 56.6 | -12.3 | 4.6 | 0.2 |
| O_BULLCS+O_BF+O_STRG | 2.2 | 70.2 | -15.3 | 4.6 | 0.3 |
| O_LC+O_BF+O_STRG | 2.2 | 85.8 | -18.9 | 4.5 | 0.2 |
| O_WHEEL+O_BF+O_STRD | 2.2 | 47.0 | -13.2 | 3.6 | 0.2 |
| O_BULLCS+O_BF+O_STRD | 2.2 | 59.9 | -13.2 | 4.5 | 0.1 |

### top 6 by CAR/maxDD, k=3

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_LC+O_BULLCS+O_STRG | 2.1 | 90.8 | -18.7 | 4.9 | 0.5 |
| O_LC+O_BULLCS+O_STRD | 2.0 | 79.2 | -16.7 | 4.7 | 0.5 |
| O_WHEEL+O_BF+O_STRG | 2.2 | 56.6 | -12.3 | 4.6 | 0.2 |
| O_BULLCS+O_BF+O_STRG | 2.2 | 70.2 | -15.3 | 4.6 | 0.3 |
| O_BULLCS+O_BF+O_STRD | 2.2 | 59.9 | -13.2 | 4.5 | 0.1 |
| O_LC+O_BF+O_STRG | 2.2 | 85.8 | -18.9 | 4.5 | 0.2 |

### top 6 by Sharpe, k=4

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_BULLCS+O_BF+O_STRG+O_CC | 2.5 | 60.4 | -12.6 | 4.8 | 0.3 |
| O_BULLCS+O_WHEEL+O_BF+O_STRG | 2.5 | 61.5 | -12.7 | 4.9 | 0.3 |
| O_WHEEL+O_BF+O_STRG+O_CC | 2.4 | 50.3 | -12.0 | 4.2 | 0.6 |
| O_LC+O_WHEEL+O_BF+O_STRG | 2.4 | 72.8 | -13.7 | 5.3 | 0.2 |
| O_LC+O_BF+O_STRG+O_CC | 2.4 | 71.5 | -15.2 | 4.7 | 0.3 |
| O_BF+O_CSP+O_STRG+O_CC | 2.4 | 43.9 | -9.1 | 4.8 | 0.5 |

### top 6 by CAR/maxDD, k=4

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_LC+O_BULLCS+O_BF+O_STRG | 2.4 | 83.6 | -15.0 | 5.6 | 0.5 |
| O_LC+O_WHEEL+O_BF+O_STRG | 2.4 | 72.8 | -13.7 | 5.3 | 0.2 |
| O_LC+O_BULLCS+O_BF+O_STRD | 2.3 | 75.1 | -14.6 | 5.1 | 0.5 |
| O_LC+O_BULLCS+O_WHEEL+O_STRG | 2.3 | 76.4 | -14.9 | 5.1 | 0.5 |
| O_LC+O_BULLCS+O_STRG+O_CC | 2.3 | 75.1 | -14.7 | 5.1 | 0.5 |
| O_LC+O_BULLCS+O_STRD+O_PP | 2.1 | 64.0 | -12.9 | 5.0 | 0.5 |

### top 6 by Sharpe, k=3, excluding O_LC

| combo | Sharpe | CAR% | maxDD% | CAR/DD | max pair corr |
|---|---|---|---|---|---|
| O_BF+O_STRG+O_CC | 2.3 | 55.2 | -13.6 | 4.0 | 0.3 |
| O_WHEEL+O_BF+O_STRG | 2.2 | 56.6 | -12.3 | 4.6 | 0.2 |
| O_BULLCS+O_BF+O_STRG | 2.2 | 70.2 | -15.3 | 4.6 | 0.3 |
| O_WHEEL+O_BF+O_STRD | 2.2 | 47.0 | -13.2 | 3.6 | 0.2 |
| O_BULLCS+O_BF+O_STRD | 2.2 | 59.9 | -13.2 | 4.5 | 0.1 |
| O_BULLCS+O_STRG+O_CC | 2.2 | 60.1 | -16.1 | 3.7 | 0.3 |

## 4. Horizon classification (calendar days, option structures)

| strat | median hold d | p75 | p90 | held >21d % | held >90d % | median DTE at entry | class |
|---|---|---|---|---|---|---|---|
| O_LC | 46.0 | 53.0 | 60.0 | 70.1 | 0.0 | 52.0 | mid (22-90d) |
| O_BULLCS | 53.0 | 60.0 | 67.0 | 94.0 | 0.0 | 59.0 | mid (22-90d) |
| O_BULLPS | 39.0 | 44.0 | 51.0 | 98.4 | 0.0 | 38.0 | mid (22-90d) |
| O_WHEEL | 19.5 | 39.0 | 51.0 | 49.1 | 0.0 | 24.0 | short (<=21d) |
| O_BF | 29.0 | 35.0 | 36.0 | 80.3 | 0.0 | 28.0 | mid (22-90d) |
| O_IC | 23.0 | 32.0 | 36.6 | 51.5 | 0.0 | 36.0 | mid (22-90d) |
| O_JL | 34.0 | 38.0 | 46.0 | 74.5 | 0.0 | 39.0 | mid (22-90d) |
| O_RS | 35.0 | 40.0 | 44.0 | 99.3 | 0.0 | 36.0 | mid (22-90d) |
| O_CSP | 32.0 | 36.0 | 42.0 | 99.1 | 0.0 | 39.0 | mid (22-90d) |
| O_STRD | 56.0 | 63.0 | 63.0 | 96.0 | 0.0 | 53.0 | mid (22-90d) |
| O_STRG | 39.0 | 46.0 | 49.0 | 98.8 | 0.0 | 38.0 | mid (22-90d) |
| O_CC | 39.0 | 46.0 | 51.0 | 98.3 | 0.0 | 38.0 | mid (22-90d) |
| O_PP | 25.0 | 31.0 | 32.0 | 78.3 | 0.0 | 24.0 | mid (22-90d) |
| O_VERT | 17.0 | 21.0 | 25.5 | 12.5 | 0.0 | 16.0 | short (<=21d) |

## 5. Red-flag data

| strat | total pnl$ | best trade$ | best trade share% | best trade | best exit-month share% | best month | 2020-exit share% | legs entry px<=0.05 | rows |pnl_pct|>500 | max pnl_pct | min pnl_pct |
|---|---|---|---|---|---|---|---|---|---|---|---|
| O_LC | 1,635,837.0 | 151,059.8 | 9.2 | LRCX 2025-09-02 | 17.8 | 2025-10 | 2.3 | 0 | 0 | 96.9 | -8.0 |
| O_BULLCS | 405,147.9 | 40,607.6 | 10.0 | INTC 2025-09-15 | 13.0 | 2025-08 | 2.4 | 0 | 0 | 91.6 | -79.7 |
| O_BULLPS | 12,710.8 | 1,607.6 | 12.6 | MU 2024-09-11 | 12.6 | 2024-10 | 15.3 | 0 | 0 | 12.9 | -14.3 |
| O_WHEEL | 81,430.9 | 7,055.7 | 8.7 | SCCO 2020-03-20 | 11.7 | 2025-08 | 6.6 | 0 | 0 | 44.0 | -10.9 |
| O_BF | 237,227.1 | 23,911.7 | 10.1 | TSM 2025-06-27 | 13.4 | 2025-12 | 4.6 | 0 | 0 | 75.7 | -130.5 |
| O_IC | 5,928.8 | 1,355.6 | 22.9 | DELL 2020-01-07 | 22.9 | 2020-02 | 25.4 | 1 | 0 | 10.1 | -11.2 |
| O_JL | 9,501.6 | 2,818.7 | 29.7 | C 2020-07-06 | 19.3 | 2025-04 | 22.7 | 1 | 0 | 11.6 | -13.9 |
| O_RS | 11,144.4 | 1,992.8 | 17.9 | MRVL 2022-04-06 | 14.2 | 2022-09 | 13.6 | 0 | 0 | 6.6 | -12.9 |
| O_CSP | 20,498.2 | 1,295.9 | 6.3 | T 2023-09-25 | 7.9 | 2022-08 | 13.8 | 0 | 0 | 4.3 | -6.8 |
| O_STRD | 143,518.8 | 14,049.8 | 9.8 | TSM 2024-04-19 | 20.8 | 2025-07 | 7.3 | 0 | 0 | 35.2 | -10.4 |
| O_STRG | 398,734.8 | 56,109.8 | 14.1 | LLY 2025-10-01 | 24.1 | 2025-05 | 4.4 | 0 | 0 | 66.6 | -20.0 |
| O_CC | 72,640.9 | 5,591.9 | 7.7 | PLTR 2024-12-27 | 11.7 | 2024-03 | 8.9 | 0 | 0 | 8.6 | -6.9 |
| O_PP | 46,205.1 | 3,066.8 | 6.6 | NVDA 2025-09-24 | 13.1 | 2025-10 | 4.5 | 0 | 0 | 6.3 | -3.3 |
| O_VERT | 7,605.3 | 1,479.8 | 19.5 | DELL 2022-04-20 | 32.7 | 2022-05 | 17.5 | 0 | 0 | 64.5 | -62.5 |

