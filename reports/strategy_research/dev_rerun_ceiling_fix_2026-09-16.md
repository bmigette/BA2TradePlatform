# Dev-deployed backtests re-run under the corrected per-instrument ceiling

Generated 2026-09-16T15:34:56.902018+00:00

The stored numbers were produced when the classic RM's per-instrument ceiling was
`available x ratio`; the new ones use `virtual x ratio` (finding 4, 2026-09-16).
No stored row was modified.

| bt | strategy | trades old→new | total% old→new | CAR% old→new | maxDD% old→new |
|---|---|---|---|---|---|
| 1000 | TOP4-scr-small-DeterministicScorer-S1-goal20 | 261 → 163 | +136.97 → +150.48 | +15.48 → +16.55 | -16.14 → -21.32 |
| 1017 | TOP3-scr-large-DeterministicScorer-S2-goal20 | 273 → 242 | +161.98 → +213.42 | +17.43 → +21.00 | -17.24 → -17.88 |
| 1030 | TOP1-scr-mid-DeterministicScorer-S2-goal2020 | 327 → 283 | +146.59 → +184.58 | +16.25 → +19.06 | -13.50 → -14.50 |
| 1064 | TOP2-scr-large-FactorRanker-goal2020-riskatr | 1151 → 1151 | +98.39 → +98.39 | +12.11 → +12.11 | -11.67 → -11.67 |
| 1065 | TOP1-scr-large-FactorRanker-goal2020-riskatr | 1279 → 1279 | +104.43 → +104.43 | +12.67 → +12.67 | -12.04 → -12.04 |
| 1070 | TOP1-scr-mid-FMPRating-S1-goal2020-riskatr-f | 200 → 187 | +85.11 → +82.23 | +16.69 → +16.23 | -15.24 → -15.25 |
| 1088 | TOP1-scr-mid-FMPEarningsDrift-S1-goal2020-ri | 562 → 502 | +346.98 → +349.08 | +28.38 → +28.48 | -16.28 → -19.57 |
| 1107 | TOP1-scr-large-DeterministicScorer-S6-goal20 | 451 → 651 | +271.68 → +44.77 | +24.49 → +6.37 | -15.59 → -6.01 |
| 1143 | TOP1-scr-mid-FactorRanker-goal2020-riskatr | 236 → 236 | +29.66 → +29.66 | +4.43 → +4.43 | -7.31 → -7.31 |
| 1146 | TOP4-scr-mid-FactorRanker-goal2020-riskatr | 191 → 35 | +43.29 → +34.06 | +6.19 → +5.01 | -12.85 → -12.34 |
| 1173 | TOP3-scr-mid-DeterministicScorer-S6-goal2020 | 231 → 213 | +208.17 → +208.55 | +20.66 → +20.68 | -12.78 → -15.35 |
| 1182 | TOP3-scr-large-FMPRating-S1-goal2020-notiona | 256 → 240 | +90.25 → +107.49 | +17.49 → +20.08 | -11.71 → -11.54 |
| 1298 | TOP1-scr-mid-FMPInsiderClusterBuy-S1-goal202 | 269 → 258 | +140.82 → +158.40 | +15.79 → +17.16 | -9.38 → -10.52 |
| 1358 | TOP2-scr-small-DeterministicScorer-S7-goal20 | 488 → 462 | +128.80 → +110.88 | +14.81 → +13.26 | -12.36 → -12.67 |
| 1367 | TOP3-scr-small-FMPEarningsDrift-S2-goal2020- | 237 → 157 | +242.37 → +267.08 | +22.79 → +24.23 | -19.15 → -21.74 |
| 1386 | TOP2-scr-small-FMPInsiderClusterBuy-S1-goal2 | 299 → 296 | +327.09 → +338.66 | +27.41 → +27.98 | -12.51 → -13.99 |
| 1434 | TOP2-scr-large-FMPRating-S6-goal2020-riskatr | 382 → 336 | +99.89 → +102.51 | +18.96 → +19.35 | -13.24 → -13.41 |
| 1439 | TOP1-sen-S1-goal2020-risk_atr | 178 → 160 | +119.53 → +124.04 | +14.02 → +14.41 | -11.25 → -12.39 |
| 1508 | TOP1-scr-small-FMPRating-S6-goal2020-riskatr | 198 → 194 | +51.14 → +53.73 | +10.91 → +11.38 | -12.05 → -11.98 |
| 1528 | TOP1-scr-small-FMPEarningsDrift-S6-goal2020- | 365 → 331 | +187.70 → +173.41 | +19.28 → +18.27 | -23.12 → -23.65 |
| 1583 | TOP5-scr-mid-FMPRating-S6-goal2020-notional- | 284 → 238 | +52.42 → +60.82 | +11.14 → +12.65 | -15.17 → -17.27 |
| 1607 | TOP2-scr-mid-FMPEarningsDrift-S7-goal2020-no | 315 → 307 | +124.68 → +133.75 | +14.46 → +15.22 | -8.06 → -8.04 |
| 1619 | TOP1-scr-mid-FMPInsiderClusterBuy-S6-goal202 | 158 → 153 | +52.45 → +55.21 | +7.29 → +7.61 | -13.22 → -13.38 |
| 1633 | TOP2-scr-small-FMPRating-S5-goal2020-notiona | 524 → 360 | +30.98 → +48.22 | +7.00 → +10.37 | -10.66 → -17.22 |
| 1649 | TOP2-sen-S6-goal2020-notional | 468 → 451 | +124.17 → +136.26 | +14.42 → +15.42 | -9.41 → -10.79 |
| 1681 | TOP1-scr-small-FMPInsiderClusterBuy-S7-goal2 | 168 → 161 | +226.73 → +243.59 | +21.84 → +22.87 | -17.52 → -18.95 |
