# Production Senate latency — 2026-09-14

**The task was slow, not stuck.** Production expert 13, `goal2020-sen_S6_notional_top1`, completed analysis 5540 at **15:52:11 CEST**. Worker task `analysis_11` completed at 15:52:13 and was removed from the persisted queue at 15:52:14. Its recorded worker duration was **1,333.41 seconds (22m 13s)**.

The analysis gathered **249 symbols**, returned **8 actionable recommendations** and omitted 241 HOLD/SKIP results. Recommendation IDs are 1892–1899, for GS, INTC, MCD, MRK, MSFT, NVDA, TSCO and WAB. These are recommendations; this investigation does not equate them with filled orders.

## Where the time went

Times below are local CEST, from the production logs identified through desktop `ba2.bat`. The production database is `C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite`, port 8081. The process was PID 24044, started at 11:24:25.

| Interval | Observation |
|---|---|
| 15:30:00–15:30:07 | Worker starts and creates the basket analysis. |
| 15:30:07–15:35:26 | Full paginated Senate feed: 13,952 records, approximately 5m 18s. |
| 15:35:26–15:38:43 | Full paginated House feed: 25,250 records, approximately 3m 17s. |
| 15:38:44–15:40:16 | Histories for 40 trader-name strings: 40 Senate and 40 House requests, approximately 1m 32s. |
| 15:40:16–15:46:18 | Historical/skill preparation before the first observed current-price lookup. The code performs skill and holding calculations here; there is no per-stage timing log to subdivide these six minutes reliably. |
| 15:46:18–15:52:11 | Current-price and remaining per-symbol historical-price resolution, approximately 5m 53s. The production log records 251 sequential fresh-price fetches in this interval. This duration also includes interleaved historical work, so it is not all removable quote latency. |
| 15:52:11–15:52:14 | Recommendations/state stored, worker completes, persisted task removed. |

The log continued advancing across different symbols during the investigation. The database then transitioned from `RUNNING` to `COMPLETED`; this was not merely a stale UI row disappearing.

## Causes

1. **Sequential account prices.** `_gather_all` called `_get_current_price(symbol)` inside the basket loop. The existing account API accepts a list, but this caller was using its scalar path. Alpaca's implementation requests last trades and quotes, so a scalar expert call can mean two broker requests. Many observed calls spent approximately 0.7–1 second in broker resolution, with additional time between them. The existing 60-second account cache does not help much when each symbol is new.
2. **Full-feed downloads and shared throttling.** Production logged **199 FMP HTTP 429 responses between 15:30:32 and 15:38:59**, across all experts, not Senate alone. Breakdown: historical-price-full 151; insider-trading 11; senate-trades 9; price-target 8; price-target-consensus 9; quote 3; house-trades 6; house-trades-by-name 2. The shared retry gate makes one caller's cooldown delay other FMP callers too. These are logged attempts, not 199 unique datasets or a measured sum of waiting time.
3. **Live Congress history bypasses the backtest disk cache.** `_all_trades_index_cached(is_live=True)` fetches both full feeds again. `fmp_history_disk_cached` calls the fetcher directly when the freeze flag is off, including trader histories and cold price-history requests. Senate does keep a process-level daily-open map and scoring caches; it is inaccurate to say it has no caching at all. Those caches do not remove full-feed/trader downloads every live analysis, and the price map starts cold after a restart.
4. **Limited progress visibility.** The UI keeps the basket as one running task. There were no Senate-specific progress lines between the last trader-history response and final recommendation creation, although the shared log showed the price requests. Its 15-minute OPEN_POSITIONS safety timer fired at 15:45 because entry analysis had not reached order processing. That is not a deadlock or analysis timeout; the released positions pass found no Senate positions to manage.

The House feed returned HTTP 400 at page 101. Existing pagination handling kept the 25,250 rows already retrieved and continued. This is a separate feed-depth limitation, not the reason the task remained running afterwards. The deployed analysis uses 90-day disclosure and 60-day execution windows, with trader skill enabled; changing those rules simply to speed up the run would change the strategy.

## Implemented improvement

The live weighted-Senate basket now prepares historical inputs first, then calls the existing **account batch-price API once** for all surviving symbols. It reads the returned map directly throughout basket assembly, rather than preloading the 60-second cache and calling the scalar helper again.

- Same account, broker price-selection behavior, cache and provider observation path.
- Batch prices are obtained after historical preparation so that preparation cannot age the new snapshot.
- Prices are matched by symbol, independent of response ordering.
- Missing/`None` prices skip only the affected symbols, as before. An invalid non-map response raises instead of silently declaring an empty opportunity set.
- Empty baskets make no quote request. Symbols whose historical preparation raises a recognized cache miss are excluded before requesting quotes.
- Backtests retain their per-symbol `providers.price_at_date(symbol, as_of)` calls. No strategy settings, signals, risk limits, sizing formulas or stored backtest results were changed.
- New log messages report requested/priced symbol counts and unavailable symbols.

Files: [Senate basket](../../packages/experts/ba2_experts/FMPSenateTraderWeight.py), [existing account-price bridge typing](../../packages/common/ba2_common/core/interfaces/MarketExpertInterface.py), [regression tests](../../packages/experts/tests/test_senate_gather_process.py).

**Validation: 103 tests passed.** The Senate suite passed 89 tests, including batch-to-account routing, delayed quote acquisition, symbol mapping, live/backtest recommendation equality, missing-price isolation, malformed-response failure, historical-cache-miss isolation and empty-basket handling. Golden parity plus backtest basket-dispatch integration passed another 14 tests. Tests used isolated fixtures and no production/market requests.

This verifies identical decisions for identical supplied prices. A batch naturally obtains a tighter-in-time live snapshot than a six-minute sequence; actual moving-market prices need not equal those the old loop would have fetched later. No production timing improvement has yet been measured.

## Application status and follow-up

The change is in the working tree, uncommitted and unpushed. **Production was not restarted**; the completed 15:30 analysis used the original implementation. The app runs with `reload=False`, so a subsequent controlled restart is needed to load the change.

Batching addresses the current-price part. Full-history refresh, FMP rate contention and cold historical preparation remain. Their next optimization should preserve the same data and strategy: persist/refresh history with explicit freshness boundaries, prepare required history before the market-open burst, and add stage timings. Simply shortening the history windows or disabling skill calculations would invalidate the comparison to the backtest and is not part of this change.

Evidence: production `logs/FMPSenateTraderWeight-exp13.log`, `logs/all.debug.log`, `logs/app.log`; read-only database row `marketanalysis.id=5540`. API credentials are deliberately omitted. On Windows, directory-reported file size/mtime remained stale while these log handles were open; reading the files revealed their current contents.

## Why Senate then had only $54.85 available

At 15:52:14, the recorded account equity was **$2,041.83**. The configured account margin factor is **1.8**, so its allowed gross exposure was **$3,675.294**. The logged remaining headroom was **$54.854**; therefore the exposure counted by that snapshot was **$3,620.44**:

`$2,041.83 × 1.8 − $3,620.44 = $54.854`

The exposure term is broker long market value plus absolute short market value, plus remaining working entry orders. It covers the whole account, including other experts/manual holdings; it is not Senate's own used balance. The $3,620.44 is reconstructed from the exact logged ceiling/headroom rather than a later moving-market broker snapshot. A subsequent read-only local order check found only closing/protective SELL orders for BUY transactions among nonterminal orders (16 HELD, 20 NEW), which the pending-entry calculation excludes.

Senate's 50% virtual allocation was **$1,837.647**, with $0 used on its own books. This is an expert budget, not segregated cash held aside while other experts trade. Its spendable amount was clamped by both broker availability (**$1,006.97**) and the account-wide exposure headroom (**$54.854**). The lower number wins.

The configured minimum available-equity threshold is **10% of Senate's virtual allocation**, or **$183.7647**. Only $54.85 remained (2.985% of the virtual allocation), so GS, MRK and INTC were skipped by the entry gate. This is consistent with the current limits; the recommendation did not fail because the analysis was still running.

The generic message suggesting “increase virtual equity percentage” is misleading for this case. That cannot increase the account-wide $54.85 headroom and would raise the percentage-based minimum threshold. The binding constraint is total account exposure, not Senate's allocation percentage. No margin limit or allocation was changed by this investigation.
