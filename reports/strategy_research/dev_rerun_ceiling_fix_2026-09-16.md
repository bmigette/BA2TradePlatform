# Dev-deployed backtests re-run under the corrected per-instrument ceiling

Generated 2026-09-16T16:32:33.692432+00:00

The stored numbers were produced when the classic RM's per-instrument ceiling was
`available x ratio`; the new ones use `virtual x ratio` (finding 4, 2026-09-16).
No stored row was modified.

| bt | strategy | trades old→new | total% old→new | CAR% old→new | maxDD% old→new |
|---|---|---|---|---|---|
| 1146 | TOP4-scr-mid-FactorRanker-goal2020-riskatr | 191 → 35 | +43.29 → +34.06 | +6.19 → +5.01 | -12.85 → -12.34 |
