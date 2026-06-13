# FMP Backtest Feasibility — Expert/Strategy Survey (2026-06-13)

Investigation of which BA2 experts can be backtested with the **current FMP API
key** (no premium upgrade). TradingAgents is excluded (LLM, not replayable from
historical data). Every endpoint below was probed live against our key and
returned data — **none were premium-gated**.

## TL;DR

| Expert | Signal endpoint(s) | Historical? | Backtestable | Effort |
|---|---|---|---|---|
| **FMPEarningsDrift** | `earnings-surprises` (dated EPS actual vs est) | ✅ full, dated | **Yes — clean** | Low |
| **FMPInsiderClusterBuy** | `insider-trading` (v4, dated Form-4 `filingDate`) | ✅ full, dated | **Yes — clean** | Low |
| **FMPSenateTraderCopy/Weight** | `senate-trades` / `house-trades` (dated disclosures) | ✅ full, dated | **Yes** (mind disclosure lag) | Low-Med |
| **FactorRanker** | `historical-price-full` + fundamentals (income/ratios) | ✅ prices; fundamentals dated | **Yes** | Medium |
| **Weinstein Stage-2 filter** | `historical-price-full` (SMA computable) | ✅ | **Yes** | Low |
| **FMPRating** | `price-target-consensus` + `upgrades-downgrades-consensus` | ⚠️ both **point-in-time only** (n=1) | **Yes, with reconstruction** | Medium-High |
| **FinnHubRating** | Finnhub recommendation-trends | ✅ (Finnhub, monthly history) | **Yes** (separate API) | Low-Med |

## Endpoint probe results (key works on all)

| Endpoint | Rows (AAPL) | Nature |
|---|---|---|
| `v3/historical-rating/{sym}` | 6235 | FMP daily letter rating (A-F) + component scores (DCF/ROE/…). Ready-made daily signal. |
| `stable/grades-historical?symbol=` | dated | **StrongBuy/Buy/Hold/Sell/StrongSell counts, dated** — the exact inputs FMPRating uses, but over time. |
| `v4/price-target` (historical) | 245 | Individual analyst targets with `publishedDate`, `priceTarget`, `priceWhenPosted`. |
| `v4/price-target-consensus` | 1 | **Current only — no history.** |
| `v4/upgrades-downgrades-consensus` | 1 | **Current only — no history.** |
| `v3/earnings-surprises/{sym}` | 109 | Dated actual vs estimated EPS. |
| `v3/historical/earning_calendar/{sym}` | 164 | Dated earnings w/ estimates. |
| `v4/insider-trading?symbol=` | 100/page | Dated Form-4: `filingDate`, `transactionDate`, type, name. |
| `stable/senate-trades?symbol=` | 100 | Dated congressional disclosures. |
| `v3/historical-price-full/{sym}` | daily | OHLCV — free, drives entries/exits + factors + Weinstein. |

## Per-expert notes

### Clean to backtest (single dated signal → clear entry/exit)
- **FMPEarningsDrift**: replay `earnings-surprises` by report date; enter when
  surprise ≥ threshold and report is fresh; exit after the drift window. Entry/exit
  prices from `historical-price-full`. No reconstruction needed.
- **FMPInsiderClusterBuy**: replay `insider-trading` using **`filingDate`** (the date
  the trade became public — NOT `transactionDate`, which would be lookahead). Cluster
  detection logic runs unchanged on the historical window.

### Backtestable with care
- **Senate/House traders**: disclosures are dated; the existing code already accounts
  for the reporting lag — backtest must use the disclosure/filing date as the as-of
  point, never the transaction date.
- **FactorRanker / Weinstein**: prices are fully historical; factor values and the
  30-week SMA are computable as-of any date. Caveat: **fundamentals restatement
  lookahead** (FMP serves as-reported figures; use the period's report date as the
  as-of) and **survivorship/universe bias** — the screener returns *today's*
  constituents, so a historical run over a fixed current universe overstates results
  (delisted losers are missing).

### FMPRating — the one gap (reconstruction required)
FMP exposes only the **current** consensus price target and the **current**
buy/sell/hold consensus (`n=1`, no history). To backtest FMPRating faithfully we must
**reconstruct** the inputs as-of each past date:
- Buy/Hold/Sell counts → from **`grades-historical`** (dated StrongBuy…StrongSell).
- Consensus price target → roll an average of **`v4/price-target`** entries whose
  `publishedDate` ≤ the as-of date (e.g. trailing 90 days).
This reproduces FMPRating's `_calculate_recommendation` math historically, but it is
an approximation of the live consensus and is the most involved of the set.

## Survivorship / point-in-time warnings (apply to all)
1. **Universe**: the FMP screener is point-in-time (current listings). Backtests need a
   historical universe or accept survivorship bias.
2. **As-of discipline**: always key signals to the date they became *public*
   (`filingDate`, `publishedDate`, earnings `date`), never an earlier event date.
3. **Fees/slippage**: model commissions + spread (the strategy doc shows friction
   dominates small accounts) so results aren't overstated.

## Recommendation
Build a **lightweight event-replay backtester** (not a full tick engine) keyed on the
dated signal endpoints + `historical-price-full` for fills. Start with the two clean
cases — **FMPEarningsDrift** and **FMPInsiderClusterBuy** — since each is a single
dated signal with an unambiguous entry/exit and no reconstruction. Reuse the experts'
existing pure calculators (`evaluate_earnings_drift`, `detect_insider_cluster`) so the
backtest exercises the real decision logic. Add FactorRanker/Weinstein next (prices
only), and FMPRating last (needs consensus reconstruction).
