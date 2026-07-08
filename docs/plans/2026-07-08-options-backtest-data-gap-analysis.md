# Options backtest/optimization readiness — data-gap analysis

Date: 2026-07-08

## TL;DR

The options **engine** (strategy actions, chain selection, fills, expiry/assignment
settlement, margin, GA gene space) is complete and production-quality. The
**data** underneath it is the blocker for trustworthy "maximum profit" results:

1. The local options cache is currently empty — nothing can run until
   `ba2-test fetch-options` is run.
2. Neither Alpaca nor FMP provides **historical point-in-time greeks/IV**. Both
   only expose greeks/IV as a *live snapshot* of today. This is an external
   data-source limitation, not a gap in our code.
3. Neither provides historical bid/ask **quote** history either — only
   historical daily **bars** (OHLCV trade prices). So our zero-spread fill
   assumption (bid=ask=last=close) is the best available with these two
   sources, not a shortcut we chose over a better option.
4. Alpaca's options history starts **2024-02-01** — about 2.5 years of mostly
   low-vol bull-market data as of this writing.

None of this blocks running backtests. It does mean: strike selection must use
`percent_otm` or `consensus_target` (not `delta`), IV-rank/vol-timing entries
can't be backtested faithfully, and combo/short-premium P&L is optimistic
(no realistic spread cost, no early assignment).

## What was checked

### Alpaca (`alpaca-py`, already integrated — `fetch_options.py`)

Methods on `OptionHistoricalDataClient` / option chain endpoints:

| Method | Returns | Historical (past date)? |
|---|---|---|
| `get_option_bars` | daily/intraday OHLCV bars | **Yes** — since 2024-02-01 |
| `get_option_trades` | trade prints | recent only |
| `get_option_latest_quote` | current bid/ask | No — live only |
| `get_option_latest_trade` | current trade | No — live only |
| `get_option_snapshot` / `get_option_chain` | latest trade+quote+**IV+greeks** | No — live snapshot only |
| `get_option_exchange_codes` | metadata | n/a |

Confirmed: **no Alpaca endpoint returns IV/greeks or bid/ask for a past date.**
The chain/snapshot endpoints are always "as of now." This matches what
`fetch_options.py` already assumes (its docstring calls this out) — the code
is not leaving data on the table.

Subscription tiers: Free/Basic gets the `indicative` options feed (delayed,
modified quotes); `Algo Trader Plus` ($99/mo) unlocks the real-time `OPRA`
feed. Tier only affects the **live/current** feed quality — it does not
unlock historical greeks/IV or historical quotes, so upgrading Alpaca's plan
would not fix the backtest data gap.

Sources:
- https://alpaca.markets/sdks/python/api_reference/data/option/historical.html
- https://docs.alpaca.markets/us/docs/historical-option-data
- https://docs.alpaca.markets/reference/optionchain
- https://docs.alpaca.markets/us/docs/about-market-data-api

### FMP (Financial Modeling Prep, already integrated for equities/fundamentals/news)

No options-chain, options-quote, or options-greeks endpoint found in FMP's
API surface, and no FMP options provider exists anywhere in this repo
(`packages/providers/ba2_providers` has FMP providers for OHLCV, fundamentals,
news, insider, screener — nothing for options). FMP is fundamentals/equity-
data focused; it is not an options data vendor.

**Conclusion: FMP is not an option for options data at all** (pun intended) —
it doesn't have the endpoint, integrated or not.

### Third-party options-specific vendors (not integrated, for future reference)

If historical IV/greeks become a priority (to backtest vol-selling /
IV-rank-gated strategies), the vendors that actually offer point-in-time
historical greeks are dedicated options-data shops, not Alpaca/FMP:

- **IVolatility** — RAW IV datasets, per-strike/expiry, US history back to 2005
- **Polygon.io** — options chains with greeks; better realtime/historical
  granularity than Alpaca for options specifically
- **EODHD** — options + greeks + IV bundled with broader historical coverage
- **ORATS** — options-focused, deep historical greeks/IV, used by many quant
  shops

None of these are integrated today; adding one would be a new provider
(`packages/providers`) plus a `fetch-options`-equivalent cache builder.

## Implication for current work

- `option_selector.select_*` methods that take `method="delta"` will return
  nothing against our cache (no historical delta) — already documented in
  `option_selector.py` and `fetch_options.py`. Use `percent_otm` or
  `consensus_target` instead.
- `IVRankCondition` exists in `TradeConditions.py` but has no historical IV
  feed to backtest against with the current data sources.
- Combo/spread/short-premium backtests are systematically optimistic: zero
  bid/ask spread cost, no early American assignment. Treat their backtested
  edge as an upper bound, not an expected return.
- The empty local cache (`~/Documents/ba2/common/options/` had zero files
  as of this check) must be populated via `ba2-test fetch-options` before any
  options backtest can run at all.

## Recommended next steps (unchanged priority from the verbal review)

1. Run `ba2-test fetch-options` for the screener universe to populate the
   cache — nothing else matters until this exists.
2. Restrict initial option strategies to `percent_otm`/`consensus_target`
   selection (delta selection is a dead end with this data).
3. If vol-timing/premium-selling strategies matter, budget for a dedicated
   options-data vendor (IVolatility/Polygon/EODHD) — Alpaca/FMP cannot supply
   historical greeks/IV no matter the subscription tier.
4. Add a configurable synthetic spread cost on option fills (e.g. bps of
   premium) so combo/short-premium backtest P&L isn't fills-at-mid fantasy.
