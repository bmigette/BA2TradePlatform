# Option implied-volatility entry conditions: profile `iv-v1`

Date: 2026-11-15
Status: built on `feat/option-vol-conditions` for backtest and GA use. **Live refuses these gates.** See
"Live source" below for the decision that is still open.
Parent design: [2026-09-15-option-market-condition-genes-design.md](2026-09-15-option-market-condition-genes-design.md).
`iv-v1` is a third profile in the same store, registry, gene scheme and seams.

## 1. Why

A study of 821 large caps from 2020 to 2025 found that a high ratio of implied to realized
volatility, and an inverted term structure, both marked richer option premium in every year. The
effect is small. These conditions are therefore gates for premium-selling structures (iron condors,
credit spreads). They are not strategies. The GA searches each gate as off, below a threshold, or
above a threshold, for every structure.

## 2. Fields

| Field | Short | Searched | Range (step) | Anchor | Meaning |
|---|---|---|---|---|---|
| `underlying_iv_rv_ratio_30_20` | `ivrv` | yes | 0.6 to 2.0 (0.1) | `> 1.2` | ATM 30-day IV divided by 20-session realized vol |
| `underlying_iv_term_slope_30_60` | `ivterm` | yes | 0.85 to 1.20 (0.05) | `> 1.0` | ATM 30-day IV divided by ATM 60-day IV (above 1 means inverted) |
| `underlying_atm_iv_30` | `iv30` | no | 0.10 to 1.00 (0.05) | `> 0.30` | the 30-day component |
| `underlying_atm_iv_60` | `iv60` | no | 0.10 to 1.00 (0.05) | `> 0.30` | the 60-day component |
| `underlying_realized_vol_20` | `rv20` | no | 0.10 to 1.00 (0.05) | `> 0.30` | the realized component |

All five fields are stored. Each is an `ExpertEventType` member and a strict deployable name. The
launcher adds the two searched leaves to every structure's initial-entry tree as `<member>-market-ivrv`
and `<member>-market-ivterm`. That gives four genes per entry arm. The study's quantiles (IV/RV p10 0.73,
median 1.10, p90 1.58) set the ranges. They are starting bounds, not estimates.
`calc_version = iv-v1/calc-1`.

## 3. Implied volatility method (`ba2_common/core/implied_vol.py`)

The method is ported from the study script and checked against its output. On AAPL, all 102 Tue/Fri
rows of 2023 that the study bracketed match to 1e-14. The vendor `iv` column is never read: it is
keyed without the expiry.

1. The input is ONE session's end-of-day chain (expiry, call/put, strike, bid, ask), normalised.
   Exact duplicate rows collapse to one. A strike quoted twice with different prices is dropped. The
   chain is never resolved by taking the first row.
2. Candidate expiries are those 7 to 130 calendar days out.
3. Forward per expiry, from put-call parity on the as-traded strikes, undiscounted. Take the strike
   K* where the call and put mids are closest (ties go to the lowest strike); the forward is
   `F = K* + C_mid - P_mid`. Forward outliers (more than 10% from the median forward) are dropped.
4. Black-76 inversion of the mid. `T = dte/365`. Bisection on [0.01, 5] for 45 steps; a result pinned
   at either bound counts as no inversion. A quote is inverted only if it is two-sided and sane: bid > 0,
   ask >= bid, spread <= 50% of mid, 0.5F <= K <= 1.5F, and the mid strictly inside the
   no-arbitrage band.
5. ATM IV per expiry. A strike's IV is the mean of its call and put IVs. Take the valid strikes on
   either side of F; if they are no more than 10% of F apart, interpolate linearly in strike.
   Otherwise use the nearer one if it is within 2.5% of F; otherwise there is no value.
6. Constant maturity, with total variance linear in calendar days. IV30 is interpolated between the
   nearest usable expiry in [7, 30] and the nearest in (30, 75]. IV60 uses [21, 60] and (60, 130].
   Both sides are required: there is no extrapolation and no flat single-expiry fallback. The study's
   flagged "single" fallback was deliberately dropped.

Realized vol uses the conventions of `underlying_realized_vol_ratio_5_20`: the same validated
128-session OHLCV window from the same source, ending at the same session, and the same
`math.log` / `fsum` sample std with ddof 1. It takes the last 20 returns and annualises by
sqrt(252), because it is compared with an annualised IV.

## 4. Timing and no look-ahead

A row for session S holds S's end-of-day chain (`bar_date == S`, contracts expiring after S) and the
OHLCV window ending at S. The row is keyed by the data session, exactly like the other profiles.
The shared resolver applies `prior_session_v1`. A backtest bar D reads row D and fills on the next
bar, which is the live decision on N(D). On an intraday clock the resolver reads P(D). Nothing in the
profile changes the timing. The build hands the calculator only the chain rows whose `bar_date` is
the row's session.

## 5. Missing values

The existing policy is unchanged:

- An unknown value never passes an active gate.
- A refusal is counted per status in `market_unknown_input_by_reason`.
- Launch-time window coverage refuses an unexplained interior run of `missing_session` or
  `invalid_prices`. Leading runs (before listing) and trailing runs (after delisting) are exempt.

How `iv-v1` maps onto that policy:

| Case | Status |
|---|---|
| no chain rows at all for the session (a source hole) | `missing_session` on the four implied fields |
| a chain exists but cannot define the value (no forward, crossed or zero quotes, no strike near the money, no expiry on one side of 30 or 60 days) | `insufficient_history`. This is a legitimately undefined value, like ta-structure's "no confirmed pivot", so it does not refuse a launch. Monthly-only chains produce it periodically. |
| OHLCV window failure | `rv20` and `ivrv` carry the window's own status, exactly like ohlcv-v1's RV ratio |
| realized vol is exactly 0 | `ivrv` is `insufficient_history`, never infinity |
| a ratio with an unknown component | takes that component's status (IV30 first), and the reason is prefixed |

Because the profile has no compute-on-miss path, a reader without a pinned manifest refuses with
`ChainProfileNeedsManifest`.

## 6. Live source: NOT available. Live refuses (open decision)

Live would need the prior session's end-of-day chain with bid and ask. What the platform can read
today:

- **Alpaca**: the live chain (`AlpacaAccount.get_option_chain`) is the current intraday snapshot. The
  historical API has bars and trades, but no quotes. The existing live IV-rank series
  (`atm_iv_history.py`) inverts bar closes for exactly this reason.
- **TastyTrade / dxfeed**: historical candles only, with no bid or ask.
- **IBKR**: untested; no live account.
- **ThetaData**: the same vendor and endpoint as the backtest store (`option_history_eod`,
  `expiration="*"`), so it would be an identical source. But nothing live uses it.

The live resolver therefore serves `iv-v1` with `LiveUnavailableMarketConditionReader`:

- Every observation is `no_context`, with the reason.
- An ERROR is logged once per process, plus one per decision pass naming the expert.
- The expert's other profiles keep their readers.
- The deploy importer refuses a payload with an `iv-v1` leaf, because the strategy could never enter
  live. A genome whose iv genes all decoded to off still imports.

The options for the owner:

1. **Build a live ThetaData EOD adapter.** It would be a pre-analysis step that fetches the prior
   session's chain for the gated universe, through the same `chains()` interface
   (`option_chain_source.py`), and builds that session's rows. This gives exact parity. The cost is a
   live dependency on the ThetaData subscription and key (`AppSetting('thetadata_api_key')`).
   ThetaData allows one session per key, so a live process would invalidate concurrent backfill
   sessions; the backfills would need a second key, or to be scheduled around the live fetch.
2. **Capture the Alpaca chain after the close each day** (latest quotes at about 16:05 ET) and
   certify it against ThetaData EOD before use. This is a different source: it needs a certification
   study, and history starts only from the first capture.
3. **Keep `iv-v1` backtest-only** for now, and use it to learn whether the gates pay before paying
   for a live source.

## 7. Building the store

```text
python tools/warm_market_conditions.py plan --profile iv-v1 --universe-file <U.txt> \
    --start 2020-01-02 --end 2025-12-31 --ohlcv-history-source <fmp|alpaca> \
    [--option-root <CACHE>/ThetaDataOptionsProvider] --out iv_plan.json
python tools/warm_market_conditions.py build --plan iv_plan.json --cache-only --concurrency 1 \
    --ohlcv-history-source <fmp|alpaca> --print-digest [--allow-exclusions]
python tools/warm_market_conditions.py verify --manifest <digest> --ohlcv-history-source <...>
python tools/warm_market_conditions.py prepare-host --manifest <digest> --ohlcv-history-source <...>
```

- **OHLCV steps.** Snapshot, certification, split checks, windows and raw shards are the existing
  ones. `source_profile` is the OHLCV source, which the backtest seam compares with the job's history.
- **Option tree.** The tree is read read-only, one calendar year per symbol at a time. A symbol with
  no tree (`no_option_chain`), or a tree fetched only up to a date before the last row
  (`option_chain_stale_tail`), is a blocking item that the warmup cannot fetch. With
  `--allow-exclusions`, a symbol with no chain is left out of the snapshot entirely.
- **Row identity.** Each row is identified by its window digest together with its chain digest, so a
  rebuild reuses a row only when both its bars and its quotes are unchanged.
- **Coverage records.** Sessions with bars but no chain rows are recorded in coverage as
  `option_chain_missing`.
- **Manifest.** The manifest records `option_source = thetadata-eod-parquet-v1`. This is an optional
  key, so OHLCV manifests are byte-identical.
- **Not retained.** The chain itself is not retained in the store: the parquet tree is the evidence.
  `retained_window` does not serve chain-profile rows, and there is no live capture or replay of
  `iv-v1` windows.

**Smoke measurement** (5 real large caps over 2024, FMP bars, real ThetaData tree, one builder):

- Build: 9.6 s, which is two chain-years per symbol, since the first row is 2023-12-29. The chain
  read and IV computation take about 0.7 to 1 s per symbol-year.
- Feature objects: 0.72 MB, about 0.14 MB per symbol-year. Most of that is the hash strings for the
  raw-shard references.
- Rebuild: reused all 1,265 rows, recomputed none, and produced the same digest.

**Estimate for about 820 symbols, 2020 to 2025:** roughly 1.5 to 2 hours with one builder, and about
0.7 GB of feature objects, plus the OHLCV raw shards, which are shared with ohlcv-v1 when the content
is identical.

## 8. Launcher usage

```text
python tools/run_options_matrix.py ... --market-condition-profile iv-v1 \
    --market-condition-manifest <iv digest>
python tools/run_options_matrix.py ... --market-condition-profile ohlcv-v1,iv-v1 \
    --market-condition-manifest ohlcv-v1=<digest>,iv-v1=<digest>
```

Each profile needs its own manifest. The profiles combine through the existing composite reader.
Genes appear in registry order (ohlcv-v1, then ta-structure-v1, then iv-v1), whatever order the flag
lists them in. Profile `none` and the two existing profiles emit exactly what they did before.

`market:profile` (`--search-market-profile`) cannot select `iv-v1`: its choice list is a persisted
contract and was not extended. A job that uses the gene together with iv leaves is refused. Jobs
without iv leaves are unaffected, because the coverage guard now checks only the profiles a strategy
reads.
