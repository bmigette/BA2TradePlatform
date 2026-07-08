# Options backtest/optimization readiness — data-gap analysis

Date: 2026-07-08 (updated same day — see "Build vs buy" section, which
supersedes the vendor-purchase recommendation below)

## Update: we can compute historical IV/greeks ourselves — no vendor needed

IV is not an independent observed quantity — it is **derived from the
option's own traded price** (the volatility that makes Black-Scholes
reproduce that price). Everything needed to compute it historically is
already in this codebase for free:

| Black-Scholes input | Source (already have it) |
|---|---|
| Option price | `fetch_options.py` → `get_option_bars` daily close (already fetched) |
| Strike / expiry / type | chain metadata (already fetched) |
| Underlying price | cached equity OHLCV (already fetched) |
| Risk-free rate | `ba2_providers.macro.FREDMacroProvider` — `DGS1MO`/`DGS3MO` Treasury yields (already integrated) |
| Dividend yield | minor input; default 0 or pull from FMP fundamentals |

Method: invert Black-Scholes numerically (`scipy.optimize.brentq` —
scipy is already a dependency) against the option's close price to solve
for IV, then read delta/gamma/theta/vega/rho **analytically** off the same
BS formula once IV is known. No new data source, no per-symbol cost.

Caveats (manageable, not blocking):
- Derived from trade **close**, not NBBO mid — noisier than a true quote-
  based IV, but no worse than the fill-price assumption the engine already
  makes.
- Equity options are American-style; pure Black-Scholes has a known bias
  (worse for puts). Barone-Adesi-Whaley is the standard fix and not much
  harder to implement than plain BS.
- No trade that day → no bar → no IV that day (same sparsity we already
  accept for fills).
- Needs bounds/sanity-checks at extreme moneyness / very short DTE where
  the root-find gets numerically unstable.

**This changes the recommendation**: building a small IV-solver module
(reusing option bars we already cache) unlocks delta-based strike selection
and `IVRankCondition` backtesting for free, and should be tried BEFORE
spending money on EODHD/Polygon/ORATS. The vendor price comparison below
is kept for reference in case the self-computed IV proves too noisy in
practice (e.g. compare our computed IV against a vendor's on a sample and
decide from there) — but it's no longer the default next step.

## TL;DR (original — see update above)

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

## Price comparison (2026-07-08, from vendor sites/search — verify before buying)

Goal: historical, point-in-time greeks/IV for a broad screener universe
(hundreds of symbols, multi-year window) — the workload `fetch-options` runs
today, just with real historical greeks instead of None.

| Vendor | Entry price | Historical depth | Greeks/IV historical? | Fits our bulk-universe workload? |
|---|---|---|---|---|
| **Alpaca** (current) | Free / $99/mo (Algo Trader Plus) | since 2024-02-01 (bars only) | **No** — snapshot-only at any tier | Bars yes, greeks no — can't be fixed by upgrading |
| **Polygon.io / Massive** | Free (EOD, 2yr) → $29/mo Starter (2yr) → **$79/mo Developer (4yr)** → $199/mo Advanced (full history, ~2014+, realtime) | 2–10 yrs by tier | Yes, part of the options product line (tier-gating on greeks specifically not confirmed — verify before buying) | Good — REST + flat-file bulk downloads, reputable data quality |
| **EODHD** (Unicorn Bay marketplace add-on) | **$29.99/mo promo → $39.99/mo** regular | 2 yrs | Yes — delta/gamma/theta/vega + IV listed in the field set | Best headline price; unclear if it needs a separate EODHD base plan on top — verify |
| **ORATS** | $99–$299/mo (direct API) | full history since 2007 | Yes — this is their core product | Good, but pricier than EODHD/Polygon for similar recency |
| **IVolatility** | Pay-per-use: $0.20 (underlying)/$0.40 (option price)/$0.60 (IV) **per ticker-day**, no subscription | back to 2005 | Yes | **Bad fit for bulk building.** Example: 300 symbols × ~630 trading days (2.5 yrs) × $0.40 ≈ **$75,600** one-time to backfill option prices alone, before the $0.60 IV dataset. Fine for one-off single-symbol research, not for a screener-wide cache. |

**Read**: for the actual workload (`fetch-options`-style bulk historical build
across a screener universe), **EODHD's ~$30–40/mo add-on and Polygon/Massive's
$79/mo Developer tier** are the realistic options — both roughly Alpaca
Algo-Trader-Plus money, but they actually deliver historical greeks/IV, which
Alpaca structurally cannot at any price. ORATS is solid but costs more for
similar depth. IVolatility's per-ticker-day pricing only works for narrow,
targeted research, not a universe-wide cache.

None of the pricing above was verified by hitting a live account/checkout —
treat it as a starting point and confirm current numbers + whether historical
greeks are actually tier-gated before committing spend.

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

## Recommended next steps (revised after the build-vs-buy finding above)

1. Run `ba2-test fetch-options` for the screener universe to populate the
   cache — nothing else matters until this exists.
2. Build a small Black-Scholes IV-solver (`scipy.optimize.brentq` + FRED
   risk-free rate) that backfills `iv`/`delta`/`gamma`/`theta`/`vega` on the
   option_chain rows from the bar closes we already fetch. Try this BEFORE
   paying for vendor data — it's free and uses only already-integrated
   pieces (scipy, `FREDMacroProvider`, existing option bars).
3. Once computed IV/greeks exist, `delta` strike selection and
   `IVRankCondition` become backtestable — validate them against a small
   sample (e.g. spot-check computed IV against a live Alpaca snapshot on a
   recent date) before trusting them at scale.
4. Only if the self-computed IV proves too noisy in practice, fall back to
   a vendor: EODHD (~$30–40/mo) and Polygon/Massive Developer ($79/mo) are
   the best-value fits for a screener-wide historical build; see the price
   comparison above.
5. Add a configurable synthetic spread cost on option fills (e.g. bps of
   premium) so combo/short-premium backtest P&L isn't fills-at-mid fantasy —
   independent of the IV question, still needed either way.
