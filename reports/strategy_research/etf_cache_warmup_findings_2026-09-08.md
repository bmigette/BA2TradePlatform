# ETFTrend and cache warmup

ETFTrend ranks a fixed basket (SPY, IEF, TLT, GLD) by 126- or 252-trading-bar
price momentum at the last completed month-end. A fund qualifies only when its
momentum is positive and its price exceeds SMA200. The research grid selects
the top one or two qualifying funds. Unfilled slots remain cash. The expert
emits recommendations; ordinary rules and risk management handle orders and
stops. It uses no LLM, fundamentals or return forecast.

The [warmup script](../../tools/strategy_research/warm_etf_cache.py) is ready.
See the [runbook](../../docs/strategy_research/goal2020_followups.md) for commands.
The default 2020–2025 campaign requests daily and five-minute OHLCV from
2018-05-11 through 2025-12-31, including the engine's 600-day preload window.

## Existing cache check

A read-only check on September 8 examined every expected NYSE session, including
holiday and early-close rules. Daily history passes for all four symbols:
1,921 of 1,921 required sessions each. Five-minute history has the following
missing or short sessions:

| ETF | Sessions meeting the count check | Short/missing before 2020 | Short/missing during 2020–2025 |
|---|---:|---:|---:|
| SPY | 1,809 / 1,921 | 101 | 11 |
| IEF | 1,813 / 1,921 | 98 | 10 |
| TLT | 1,815 / 1,921 | 98 | 8 |
| GLD | 895 / 1,921 | 413 | 613 |

The [detailed evidence](etf_cache_coverage_2026-09-08.json) lists every affected
date. This refines the earlier nine-of-ten-family preflight result: that check
sampled broad date bounds and only required execution history from the backtest
start. It did not inspect every session or the full intraday preload window.
The ETF family should therefore receive warmup before research execution.

The new check requires at least the regular-session five-minute row count for
each date. It does not certify every expected timestamp: extended-hours rows
can hide missing regular-session bars, and missing provider rows may remain
unavailable after a fetch. A failed check is a reported data limitation, not
evidence that redownloading will necessarily recover every row.

## Script behavior and validation

- Default: offline plan. `--check`: read-only coverage report. `--run`: fetch
  missing/short sessions through the existing FMP provider.
- Daily requests are bounded to a year; intraday requests to three calendar
  days. The final requested date includes the full day.
- Valid chunks merge into the same native Parquet files used by the engine.
  Existing history outside the request is preserved. Restarting skips covered
  sessions; empty or incomplete responses fail visibly without fabricating bars.
- Invalid OHLCV prices, duplicate timestamps and conflicting interval-alias
  files fail explicitly. Atomic writes and per-symbol locks coordinate instances
  of this script. Avoid simultaneous independent cache writers for the same
  symbols; those tools do not share this script's process lock.
- FMP credentials come from the environment or a read-only settings DB query.
  A small optional-key constructor extension lets the shared FMP provider work
  without starting the application's DB engine. Existing callers retain their
  original settings lookup.

**36 targeted tests passed** (33 warmup/ETF tests and three existing provider
construction tests), covering actual native-cache writes, incomplete and empty
responses, internal one-session gaps, resumption, dates, holidays, invalid data,
read-only key access and the ETF expert's existing engine integration.
The two test trees run in separate processes because both define `tests.conftest`.

Only preview/check and hermetic tests were executed. **No real data downloads,
research runs or production settings changes were performed.** This ETF-specific
script does not address the separate small-cap EarningsDrift BID cache gap.
