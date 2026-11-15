# Option implied-volatility entry conditions: profile `iv-v1`

Date: 2026-11-15
Status: built on `feat/option-vol-conditions` for backtest, GA and LIVE. Owner decisions
(2026-11-15): the option reruns read the **Alpha Vantage** option store, and live reads the same
vendor's prior-session chain. A gate trained on the ThetaData store stays backtest-only; live and
the deploy importer refuse it.
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
3. Forward per expiry, from put-call parity on the as-traded strikes, undiscounted. Only two-sided
   quotes count (bid > 0, ask >= bid); the study also admitted a zero bid. Take the strike K* where
   the call and put mids are closest (ties go to the lowest strike); the forward is
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
   flagged "single" fallback was deliberately dropped. The one exception is an expiry exactly on the
   target day, which is the constant-maturity value by itself.
7. **Discounting is ignored (D = 1), deliberately.** This biases the IV slightly low when rates are
   high. A true 0.30 vol recovers 0.2987 at 30 days and 0.2974 at 60 days at r = 5.3% (2023–24),
   and 0.3000 at r = 0.05% (2020–21). That is under 0.5% of the level; the gate steps are 5–8% of it.
   The term slope reads about +0.4% high in a high-rate regime. I measured the alternative,
   estimating D per expiry from the put-call-parity slope across strikes, on real chains: AAPL, MSFT,
   JPM, XOM, KO and AMD, March of 2021, 2023 and 2024, 20–75 day expiries, strikes within 10% of F,
   321 expiry fits. The D error ran from −0.02 to +0.04 (p10 to p90). The implied rate's median was
   −0.9% in 2021, and −8% and −12% in 2023 and 2024 against a true ~5.2%. That noise is far larger
   than the bias it would remove; American early exercise and dividends distort parity across
   strikes. This pure module has no rate source, so D = 1 stays.
   (`test_the_documented_discount_bias_of_d_equals_one` pins the bias.) After these changes the study
   parity still holds exactly: AAPL, KO and AMD, 2023, 102 bracketed rows each, max |diff| 7e-15.

Realized vol uses the conventions of `underlying_realized_vol_ratio_5_20`: the same validated
128-session OHLCV window from the same source, ending at the same session, and the same
`math.log` / `fsum` sample std with ddof 1. It takes the last 20 returns and annualises by
sqrt(252), because it is compared with an annualised IV.

## 4. Timing and no look-ahead

**Verified on real data: `bar_date` is the quote's own session.** I computed the parity forward of
the nearest expiry (7 days or more) for `bar_date` D−1, D and D+1 around large gap days. In every
case it tracks the close of its own `bar_date`, not the close of the day before or after:

- AAPL 2024-06-11, 2025-04-03, 2025-04-09
- META 2024-02-02, 2022-10-27
- AMZN 2022-02-04 (as-traded ×20)
- NFLX 2022-04-20 (×10)
- TSLA 2024-10-24
- MSFT 2023-04-26

For example, META 2024-02-02: F = 475.40 against a close of 474.99 on D and 394.78 on D−1. TSLA
2024-10-24: F = 260.82 against 260.48 on D and 213.65 on D−1. The rows are not one session late.

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

**Chain quality** (`market_condition_iv_quality.py`). `insufficient_history` and a leading
`missing_session` run are both exempt from window coverage. A broken chain can hide behind either:
a missing expiry partition, blank or truncated quotes, or a tree that starts years after the bars.
So for each symbol and for IV30 and IV60, the build measures the sessions where the stock has a
usable OHLCV window (RV20 valid) but the IV is unknown for any reason. It records the share of such
sessions and the longest contiguous run with its dates, plus the count of quotes dropped as
conflicting duplicates. These go into the symbol's coverage record (`iv_quality` and
`chain_quality` entries; chain manifests only), and the worst symbols are printed at the end of the
build.

- `verify` refuses a symbol whose longest run exceeds 20 sessions or whose share exceeds 40%. The
  overrides are `--iv-max-run`, `--iv-max-rate` and `--iv-accept-symbols`.
- The launcher applies the same thresholds, but over exactly the sessions the run reads (from the
  mapped statuses). Its overrides are `--iv-quality-max-run`, `--iv-quality-max-rate` and
  `--iv-quality-accept`; the matrix driver forwards them only when they are set. Accepted symbols
  are recorded on the run (`iv_quality_accepted`).

## 6. Sources and the live reader

### 6.1 Option sources

| `option_source` | Store (read-only) | Live |
|---|---|---|
| `alphavantage` | `<CACHE>/AlphaVantageOptionsProvider/` (`tools/download_av_option_chains.py`): the ThetaData partition layout plus not-yet-compacted `_days/` and a `_ledger.jsonl` | **served**: `HISTORICAL_OPTIONS&date=<prior session>` |
| `thetadata` | `<CACHE>/ThetaDataOptionsProvider/` | refused, by name |

The source is always named explicitly: `--option-source` is required for chain profiles. It is
recorded in the manifest (`option_source`, named like the backtest `options_store`) and on iv jobs
as the expert setting `market_condition_option_source`. That setting is optional, undeclared and
absent everywhere else.

- The launcher refuses an option job whose options store differs from the manifest's source.
- The backtest seam refuses a setting that contradicts the pinned manifest.
- Live serves only `alphavantage`.
- IV always comes from our function on bid/ask; AV's `implied_volatility` and greeks columns are
  never read.

**AV `bar_date` is the quote's own session**, checked the same way as ThetaData: the parity forward
for D-1, D and D+1 around gap days tracks the D close in every case. The days checked: AMZN
2022-02-04 and 2022-04-29 (as-traded x20), ADBE 2024-06-14 and 2024-03-15, DIS 2022-11-09, ABNB
2023-05-10, AMD 2024-10-30, GOOGL 2023-02-08. Example: ADBE 2024-06-14, forward 525.35 against a close
of 525.31 on D and 458.74 on D-1.

**AV against ThetaData, same symbol-sessions.** 12 symbols (AA, AAL, ABNB, ABT, AFL, AMD, AMZN, BAC,
C, DIS, EBAY, GOOGL), 2023-2024, 6,024 symbol-sessions:

- **Chains:** present in both on 100% of the sessions.
- **IV30:** defined in both on 100%. Identical to 1e-12 on 95.9% of sessions; differs by more than
  0.01 vol on 0.23%; maximum difference 0.020.
- **IV60:** defined in both on 93.6%, only in AV on 0.03%, in neither on 6.4%. Identical on 97.9%.
- **Differences, AV minus ThetaData:** the median and the 5th and 95th percentiles of the
  difference are exactly 0 for IV30, IV60, IV/RV and the term slope. A relative difference above 5%
  occurs on 0.13% (IV30), 0.04% (IV60), 0.13% (IV/RV) and 0.18% (term slope) of sessions.
- **Gate flips:**
  - IV/RV above 1.0 / 1.2 / 1.4: 0.03% / 0.07% / 0.03% of sessions.
  - Term slope above 0.95 / 1.0 / 1.05: 0.39% / 0.21% / 0.04%.
- **AV quote quality** (5 symbols, H1 2024, 380k quote rows; ThetaData in parentheses): zero or
  missing bid 16.07% (16.01%), missing ask 0.00%, crossed 0 (0), two-sided 83.9% (84.0%). Bid and
  ask identical to the previous session on 3.21% of contracts (3.25%).

The two vendors carry essentially the same end-of-day NBBO. The AV quotes are not broken, stale or
crossed beyond what ThetaData shows.

### 6.2 Live reader (`ba2_providers/market_conditions/av_live_chain.py`)

**Same function, same inputs.** For the decision's prior session, live fetches AV's chain through
the downloader's own `fetch_chain_payload`. It maps the chain with the store's `map_chain` and
filters it with the store reader's `chain_quotes_from_frame`. The realized-vol leg reads the window
the ohlcv-v1 live reader reads (FMP or Alpaca daily cache, `assemble_window`), and the row comes from
`compute_iv_v1`. `packages/providers/tests/test_av_live_iv_certification.py` pins the live rows
against the rows of a store built by the real downloader code from the same raw rows. **Tolerance:
none (exact equality)**, because both sides run the same code on the same numbers.

**On demand.** The reader is built only for an expert whose setting names `iv-v1` with source
`alphavantage`, and the deploy importer drops `iv-v1` from a setting no leaf reads. A chain is
fetched only when a market leaf of that expert is evaluated for the symbol. Every fetched chain is
cached on disk at `<CACHE>/market_conditions_live/alphavantage/<SYM>/<session>.parquet`, so repeat
passes, restarts and other experts make no further call. Rows are also memoised per reader.

**Rate budget.** Calls go through the shared `_av_rate_budget.json`
(owner `iv_live:<pid>`, 10/min of the key's 75), waiting at most 20 s for a slot.

**Failure policy.** Every failure produces the profile's fields at `no_context` with the reason, one
WARNING per (symbol, session, reason), and is never memoised or cached, so the next pass retries.
The gate never passes and the evaluator counts the unknown. There is no fallback source and no
approximation.

| Failure | Reason recorded |
|---|---|
| no key | `alpha_vantage_api_key is not set ...`. Only an active gate ever asks for the key, so a blank key breaks nothing else. |
| AV throttle or premium notice | `rate limit (Alpha Vantage): ...` |
| no budget slot within 20 s | `rate limit: no slot in the shared Alpha Vantage budget ...` |
| HTTP or transport error | `Alpha Vantage request failed: ...`, with the key redacted |
| empty chain (not published yet, or no listed options) | `Alpha Vantage serves no chain ... yet` |
| chain for another date, or no usable contract | named as such |
| chain exists but cannot define the value | the calculator's `insufficient_history`, exactly as in the backtest |

**Refused sources.** For a `thetadata`-trained gate, or a gate with no recorded source, live serves a
`LiveUnavailableMarketConditionReader` naming the cause, with one ERROR per decision pass. The deploy
importer refuses such leaves and accepts leaves trained on `alphavantage`.

**Capture and replay.** Capture records each served row's values, statuses and reasons together with
its evidence: the normalized chain arrays and the 128x5 window bytes, each with a digest
(schema `market_condition_chain_window/v1`). `ReplayChainReader` re-verifies both digests,
recomputes the row with the shared function, and refuses (`ReplayMiss`) evidence that is missing,
tampered with, or recomputes differently. A recorded refusal replays as the same refusal.

**Seam.** `ba2_common` may not import providers, so `ba2_trade_platform/core/seam_wiring.py`
registers the AV reader factory. The registration reads nothing, calls nothing and logs nothing.

### 6.3 Availability before the decision (evidence)

**The download ledgers.** The running download recorded 194 fetches of a prior session's chain that
returned rows, made the next trading morning before 10:00 ET. The earliest was at 00:02 ET and the
latest at 09:55 ET; for example, AA's 2026-11-09 chain (742 rows) was fetched at 2026-11-10 04:56 ET.
The 18 empty results in that window were all symbols with no listed options.

**Live calls** on Sunday 2026-11-15 at 04:07 ET (three calls through the shared budget):

| Request | Rows | Row dates | Two-sided quotes |
|---|---|---|---|
| MSFT, latest (no date) | 4,072 | 2026-11-13 | 3,743 |
| MSFT, date=2026-11-13 | 4,072 | 2026-11-13 | 3,743 |
| KO, date=2026-11-13 | 1,208 | 2026-11-13 | 1,009 |

So the prior session's chain is available well before a 10:00 ET decision. Not verified: whether AV
revises a chain after it first appears. The store keeps one fetch per day, so this could not be
compared.

### 6.4 Operator steps for live iv gates

1. Set `alpha_vantage_api_key` in the live app's settings. **Live iv gates need an ongoing Alpha
   Vantage subscription whose plan includes `HISTORICAL_OPTIONS`.** The live fetch shares that key's
   75 calls/minute with the option download through `_av_rate_budget.json`.
2. Train on the AV store: `--options-store alphavantage --option-history-source alphavantage` and an
   iv manifest built with `--option-source alphavantage`.
3. Deploy as usual. The payload carries `market_condition_profile` and
   `market_condition_option_source = alphavantage`, and the importer accepts the iv leaves.
4. Watch the log for `iv-v1 live (alphavantage)` WARNINGs and the per-pass ERROR of a refused source.

### 6.5 Backward compatibility

For anything that does not use `iv-v1`, decisions, settings, stored configs, manifests, digests and
INFO-and-above logs are unchanged:

- **No new declared or mandatory setting and no migration.** `market_condition_option_source` is
  written only onto iv jobs, and absent means "no chain profile".
- **The per-instance resolver** reads the source setting, and adds it to its cache key, only for
  experts that name a chain profile (`test_an_ohlcv_expert_never_reads_or_keys_on_an_option_source`).
- **The live host registration** is silent and inert (`tests/test_iv_live_seam_registration.py`).
  An expert without a chain profile never reaches the chain seam.
- **OHLCV manifests** never carry `option_source`, and an ohlcv-v1 job's expert settings never carry
  the new key (`test_an_iv_job_records_its_option_source_and_an_ohlcv_job_does_not`).
- **The ohlcv-v1 and ta-structure-v1 live readers** are untouched. The all-off and profile-none
  goldens and every existing market-condition test pass.

For a running prod instance (8081) with no iv gate, pulling this code changes nothing: one silent
dictionary registration at startup and no new reads, calls or log lines.

## 7. Building the store

```text
python tools/warm_market_conditions.py plan --profile iv-v1 --option-source alphavantage \
    --universe-file <U.txt> --start 2020-01-02 --end 2025-12-31 --ohlcv-history-source <fmp|alpaca> \
    [--option-root <CACHE>/AlphaVantageOptionsProvider] --out iv_plan.json
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
- **Manifest.** The manifest records `option_source` (`alphavantage` or `thetadata`). This is an
  optional key, so OHLCV manifests are byte-identical.
- **AV store.** The build also reads not-yet-compacted staged days; a staged day replaces that day's
  partition rows, exactly as compaction does. `fetched_through` comes from the download ledger, and a
  torn last line is ignored.
- **Not retained.** The store does not retain the chain itself: the parquet tree is the evidence, so
  `retained_window` does not serve chain-profile rows. Live decisions are captured with their chain
  evidence (section 6.2).

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

Each profile needs its own manifest. On an option job, the launcher refuses an iv manifest whose
`option_source` is not the job's options store: an `alphavantage` manifest requires
`--options-store alphavantage`, and a `thetadata` one requires `thetadata`. Equity jobs trade no chain and are not held to a store. The profiles combine through the existing composite reader.
Genes appear in registry order (ohlcv-v1, then ta-structure-v1, then iv-v1), whatever order the flag
lists them in. Profile `none` and the two existing profiles emit exactly what they did before.

`market:profile` (`--search-market-profile`) cannot select `iv-v1`: its choice list is a persisted
contract and was not extended. A job that uses the gene together with iv leaves is refused. Jobs
without iv leaves are unaffected, because the coverage guard now checks only the profiles a strategy
reads.
