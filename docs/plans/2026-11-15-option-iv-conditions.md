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

| `option_source` (trained on) | Store (read-only) | Live |
|---|---|---|
| `alphavantage` | `<CACHE>/AlphaVantageOptionsProvider/` (`tools/download_av_option_chains.py`): the ThetaData partition layout plus not-yet-compacted `_days/` and a `_ledger.jsonl` | **served** by the configured live IV chain source (section 6.6): `alphavantage` (`HISTORICAL_OPTIONS&date=<prior session>`) or `tastytrade` (captured snapshot) |
| `thetadata` | `<CACHE>/ThetaDataOptionsProvider/` | refused, by name |

The source is always named explicitly: `--option-source` is required for chain profiles. It is
recorded in the manifest (`option_source`, named like the backtest `options_store`) and on iv jobs
as the expert setting `market_condition_option_source`. That setting is optional, undeclared and
absent everywhere else.

- The launcher refuses an option job whose options store differs from the manifest's source.
- The backtest seam refuses a setting that contradicts the pinned manifest.
- Live serves only gates trained on `alphavantage`, from `alphavantage` or `tastytrade` (section 6.6).
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

**When a chain is fetched.**

- The reader is built only for an expert whose setting names `iv-v1` with source `alphavantage`,
  and the deploy importer drops `iv-v1` from a setting no leaf reads.
- A chain is fetched only when an **iv field** is read. The composite reader resolves a chain
  profile's fields lazily (`_LazyChainRow`), so an OHLCV leaf never causes an AV call. A composite
  without a chain reader still returns the plain `FeatureRow` with the same memo key.
- The capture recorder records only evidence already held (`peek_evidence`); it never fetches.
- Computed rows are memoised per reader. A refusal is memoised only for the current decision pass:
  a second leaf on the same symbol costs nothing, and the next pass retries. The composite's own
  memo holds the lazy row, so a transient failure is never served from it.

**Cache.** One file per chain:
`<CACHE>/market_conditions_live/alphavantage/<SYM>/<session>.parquet`.

- It holds only the six columns the computation reads, written atomically.
- A file that fails to read is deleted and refetched.
- `maintain_cache` removes `*.part` files older than an hour and keeps the newest 10 sessions per
  symbol. The prefetch runs it, never the decision path.

**Rate budget and the numbers.** These are settings-free constants in `av_live_chain.py`. The key
allows 75 calls/minute, and the option download takes 60 of them.

| Constant | Value | Meaning |
|---|---|---|
| `ON_DEMAND_PER_MINUTE` | 10 | decision-path share of the shared `_av_rate_budget.json` (owner `iv_live:<pid>`) |
| `PREFETCH_PER_MINUTE` | 12 | prefetch share |
| `HTTP_TIMEOUT_S` | 12 | HTTP timeout per call |
| `REQUEST_DEADLINE_S` | 15 | total time one request may take, response included |
| `BUDGET_WAIT_S` | 5 | longest wait for a budget slot on the decision path |
| `CIRCUIT_AFTER` | 3 | consecutive HTTP failures in a pass before further requests are refused |
| `KEEP_SESSIONS` | 10 | cached sessions kept per symbol; a symbol whose newest chain is older is removed |

On the decision path, nothing can stall a pass:

- The first budget refusal waits at most 5 s, then latches "budget exhausted" until the budget
  window frees a slot. Every further uncached symbol in the pass is refused immediately.
- Each request has a total deadline of 15 s.
- After 3 consecutive HTTP failures in a pass, the pass refuses further requests (circuit open).
- After waiting for a budget slot, the cache is checked again, so app instances prefetching in
  lockstep fetch each chain only once. The prefetch also adds a small random delay.

**Budget oversubscription while the download runs.** The nominal shares add up to more than the
key allows: 60 (download) + 12 x instances (prefetch) + 10 x instances (decision path). With three
app instances that is 126 against 75. The shared `_av_rate_budget.json` still enforces the key's 75
across every owner, so AV is never over-called. The cost is that the live owners get fewer slots
while the download takes its 60. Operator guidance while the download runs:

- Run the download with `--calls-per-minute 40` between 06:00 and 10:30 ET, or pause it over that
  window.
- Otherwise expect a slower prefetch, and more "chain not prefetched; rate budget exhausted"
  refusals at the decision.

**Prefetch** (off the decision path). The live `JobManager` runs it Mon–Fri at 06:30 ET, with a
09:45 ET deadline. It fetches the prior session's chain for each gated expert's candidate symbols,
waits for budget slots instead of refusing, resumes from the disk cache, and leaves what it could
not fetch to the decision path. `tools/prefetch_iv_chains.py` runs the same prefetch by hand.

- **When it is scheduled.** Only while an enabled expert has an active iv gate trained on
  alphavantage; otherwise nothing is scheduled or logged. This is re-evaluated at startup, on a full
  refresh, and on a single expert's refresh (an expert added, enabled or disabled, or its ruleset or
  profile changed).
- **Catch-up.** An app started inside the window runs it once immediately.
- **Stopping.** It stops at its next check (each symbol, inside every budget wait, while waiting on
  a request) when the JobManager stops or shuts down, and at interpreter exit (`atexit`). A restart
  at 07:00 is therefore never held until the deadline.
- **Failures.** A symbol that fails (bad data, I/O) is recorded and the run continues.
  `os.replace` is retried briefly on a Windows sharing violation, since app instances share the
  cache folder.

- **Candidates.** For an expert with static enabled instruments, those instruments. For an expert
  whose universe is chosen at analysis time (a screener), the symbols with a cached chain for one of
  the last 5 sessions, judged by the file's session date and never by its age on disk.
- **Screener limit.** The live screener runs at the decision on live FMP quotes. Running it at
  06:30 would screen on pre-market quotes (a different set), cost a full screen of FMP calls, and
  trigger its capture side effects, so the prefetch does not run it. A screener name that is new
  today is fetched on demand at up to 10/min; beyond that it is refused for the pass with the
  explicit reason "chain not prefetched; rate budget exhausted", summarised once per pass.
- **Typical size.** A stage-1 option expert runs on the 97-symbol top-100 universe, which takes
  about 8 minutes at 12/min. The 820-symbol large-cap universe, if fully static, takes about
  68 minutes and finishes around 07:40 ET. Both finish well before a 10:00 decision.
- **Remainder.** Symbols the prefetch could not cover are fetched on demand at the decision, at up
  to 10/min; beyond that they are refused for the pass and retried the next pass. I did not raise
  the live share: the prefetch covers every knowable universe before the decision, and the key is
  shared with the download.

**Failure policy.** Every failure produces the profile's fields at `no_context` with the reason. The
gate never passes, the evaluator counts the unknown, and nothing is cached. The decision scope logs
**one summary WARNING per reason per pass**, with the count and the first symbols. There is no
fallback source and no approximation.

- **Expected faults** (I/O, parse, the data contract) become this unknown.
- **A programming error propagates** (enforce-mode convention, `absorb_if_benign`). The live entry
  pass evaluates each recommendation in its own try/except, so it fails only that symbol's
  evaluation.
- **Startup.** If the reader module cannot be imported, startup continues with one WARNING, and iv
  gates refuse with that reason.

**One acceptance rule for the store and live** (the owner's backtest/live identity rule). Both paths
accept and map a raw AV payload with the same function, `alphavantage.map_chain`. It drops rows
with an unparsable OCC symbol, a foreign (adjusted) root, another date, or no price at all, and it
takes an undated row as the requested day. A chain it empties is cached live as an empty frame, so
it reads MISSING, exactly as the store records it. `test_store_and_live_agree_on_every_raw_payload_including_rejected_chains`
pins that live equals the store for payloads with other-dated rows, undated rows, an adjusted root,
priceless rows, and a fully rejected chain.

The rule was chosen from data:

- **Download ledgers:** 570,947 symbol-days and 328.9M raw rows; `map_chain` dropped 117,821 rows
  (0.036%), on 1.08% of symbol-days.
- **Worst days, classified with raw calls:** HST 2025-12-31, FUTU 2026-04-16 and BE 2024-11-14 were
  every one priceless rows (no quote, no trade), all correctly dated.
- **Seven raw chains, 11,910 rows** (these three, XSP, and the earlier MSFT and KO probes): no
  other-dated and no undated row.

The earlier stricter live check, which refused the whole chain, was removed. It could have refused
live a symbol-day that the backtest traded.

| Failure | Reason recorded |
|---|---|
| no key | `alpha_vantage_api_key is not set ...`. Only an active gate ever asks for the key, so a blank key breaks nothing else. |
| AV throttle or premium notice | `rate limit (Alpha Vantage): ...` |
| no budget slot within 5 s, or the latch is set | `chain not prefetched; rate budget exhausted ...` |
| HTTP or transport error, or the 15 s deadline | `Alpha Vantage request failed: ...`, with the key redacted |
| circuit open (3 consecutive HTTP failures this pass) | `Alpha Vantage failed 3 times in a row this pass ...` |
| empty response (not published yet, or no listed options) | `Alpha Vantage serves no chain ... yet` |
| every row rejected by `map_chain` | not a refusal: the row reads MISSING, as in the store |
| chain exists but cannot define the value | the calculator's `insufficient_history`, exactly as in the backtest |

**Refused sources.** For a `thetadata`-trained gate, or a gate with no recorded source, live serves a
`LiveUnavailableMarketConditionReader` naming the cause, with one ERROR per decision pass. The deploy
importer refuses such leaves and accepts leaves trained on `alphavantage`.

**Capture and replay.** Capture records each served row's values, statuses and reasons together with
its evidence: the chain arrays and the 128x5 window bytes, each with a digest
(schema `market_condition_chain_window/v1`). The chain is trimmed to the narrowest strike band
around each expiry's forward (±15%, then ±30%, then ±50%) whose recomputed row is identical to the
live row, falling back to the full chain, and it is then zlib-compressed. The trim runs only for
a row not recorded yet. The payload also carries `chain_served_digest`, the digest of the chain as
served and cached, which ties the replay evidence to what AV delivered. In the test, the stored
chain is under 40% of the raw bytes, which keeps a 100-symbol decision in the low single-digit MB.
Uncompressed payloads still replay. `ReplayChainReader` re-verifies both digests,
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

1. Choose the live IV chain source (Settings > Data Provider API Keys > "Live IV chain source", app
   setting `iv_live_chain_source`). **Unset, every active iv gate refuses**, naming the setting.
   - `tastytrade` (recommended; no Alpha Vantage subscription needed in live): enter the TastyTrade
     MARKET-DATA credentials under Broker API Keys (`tastytrade_market_client_secret`,
     `tastytrade_market_refresh_token`; a separate read-only OAuth grant is enough). With them blank,
     the gates refuse naming both settings. Experts with an iv gate need a BOUNDED universe (fixed
     instruments, or a non-empty `screener_symbol_universe`).
   - `alphavantage`: set `alpha_vantage_api_key`; needs an ongoing plan that includes
     `HISTORICAL_OPTIONS`. The live fetch shares that key's 75 calls/minute with the option download
     through `_av_rate_budget.json`.
2. Train on the AV store: `--options-store alphavantage --option-history-source alphavantage` and an
   iv manifest built with `--option-source alphavantage`.
3. Deploy as usual. The payload carries `market_condition_profile` and
   `market_condition_option_source = alphavantage`, and the importer accepts the iv leaves.
4. Reload (`/api/reload`) after changing the source or the credentials.
5. Watch the log for `iv-v1 live (<source>)` WARNINGs, the capture summaries
   (`iv-v1 tastytrade chain capture <session>: ...`) and the per-pass ERROR of a refused source.
6. While the Alpha Vantage store (or an AV key) still covers recent sessions, run
   `tools/certify_broker_iv_chain.py` daily after the close and keep `reports/iv_broker_certification.jsonl`.

### 6.6 Live IV chain source: `tastytrade` (broker snapshot, captured)

**Owner decision 2026-11-15.** The Alpha Vantage subscription is not kept for live. The backtest store
stays Alpha Vantage. Live reads the prior session's chain from TastyTrade market data, with
APP-LEVEL market-data credentials (no account definition, no order or account endpoint).

**Why TastyTrade, and not Alpaca.** Both measured on Sunday 2026-11-15 for session 2026-11-13, same
eight underlyings (MSFT KO AAPL JPM XOM NVDA ISRG EMN), same mapping and `compute_iv_v1`:

| | Alpaca (our keys) | TastyTrade |
|---|---|---|
| Feed | indicative only (`feed=opra` -> 403 "OPRA agreement is not signed") | real exchange quotes (dxFeed) |
| Bid/ask identical to Alpha Vantage, near the money | 0% (median mid differs 0.5-0.8%) | **100%** (3,478 contracts) |
| iv-v1 fields | IV30 differs up to 1.3%; term slope flips up to 1.7% of symbol-sessions | **identical** |
| Prior-session quotes after the close | yes (latest quote 15:59:59 ET Friday) | yes (`summary-date` 2026-11-13 on every contract) |

A broker feed is only ever eligible when its quotes are real OPRA quotes; an "indicative" feed is not.

**Pairing (certified-equivalent serving).** `market_conditions.LIVE_SERVING_PAIRINGS`: gates trained on
`alphavantage` may be served by `alphavantage` or `tastytrade`; anything else is refused by name. The
pairing rests on ONE session so far (2026-11-13, 8 underlyings, 3,478 contracts, 100% identical quotes,
identical fields; `PAIRING_EVIDENCE`). `tools/certify_broker_iv_chain.py` extends the record: for a
symbol list and a session it compares the TastyTrade snapshot with the Alpha Vantage chain -- from the
LOCAL AV store (no subscription needed) or the AV API -- and appends per-contract equality, the four
fields, their differences and the GA-threshold flips to `reports/iv_broker_certification.jsonl`.

**The market-data client** (`ba2_providers/options/tastytrade_quotes.py`, `TastyTradeMarketDataClient`):
OAuth refresh-token exchange (15-minute access token; TastyTrade does not rotate refresh tokens, so
nothing is written back), then only `GET /option-chains/{sym}/nested` and `GET /market-data/by-type`
(<= 100 contracts per call); any other path raises before a request. Credentials never appear in a
message, log or repr. Per underlying: 1 chain + 1 underlying + ceil(contracts/100) market-data calls --
about 3 s per symbol with a 0.2 s pause; ~5 min for 97 names, ~45 min for 820.

**What is fetched.** Expiries 7..130 days after S (the iv-v1 window), strikes within
`FETCH_STRIKE_BAND` = 20% of the underlying's close of S. Measured: a 15% band reproduces the full-chain
row exactly on all eight underlyings; tests pin 15% and 20% on the recorded fixtures.

**Validity by DATA (owner: the broker must not have flushed its snapshot when we download).**
`option_chain_quotes.validate_prior_session_snapshot(snapshot, S)`:

1. **Refused** when ANY contract is filed under a later session (`summary-date > S`) or was updated
   at/after the next session's open: today's session has started quoting.
2. The near-money contracts (7..130-day expiries, strike within 5% of the close) must number at least 4,
   and at least 90% of them must be quotes OF S: `summary-date == S`, `updated-at` between S's close
   (NYSE calendar, early closes included) and the next open, bid and ask present. Otherwise **refused**
   as "snapshot not the prior-session close" (flushed, nulled, stale).
3. The underlying's close must be filed under S.

`prev-close-date` is not used (it reads 1970-01-01 or an old date on many contracts). An accepted
snapshot keeps only the contracts that are quotes of S (another contract counts as a missing quote);
zero bids far from the money are normal and go through the shared acceptance rule unchanged
(`alphavantage.map_chain`, as AV-shaped rows). A refused snapshot is **never cached** and is retried at
the next attempt. The cached chain carries the facts (summary-date counts, min/max `updated-at`,
near-money counts, source) in its parquet metadata, and the capture evidence records them with the
serving source and the served-chain digest.

**Capture window** (`iv_chain_prefetch`, constants documented here; conservative until the weekday
behaviour is observed with `tools/probe_broker_iv_snapshot.py`): the ONE scheduler entry (parameterised
by the source) fires at :15 and :45 of every hour; an attempt runs only from **20:15 ET** on S's date
(`updated-at` values ran until ~20:00 ET) until **10 minutes before the next open** (09:20 ET), and once
at app start inside that window. Every attempt is validated on the data, so an early or late attempt can
only refuse, never store a wrong chain. The decision pass never fetches for this source.

**What is captured.** Exactly the union of the BOUNDED universes of the enabled experts with an active
iv gate: fixed instruments, or a screener's `screener_symbol_universe`. An expert with an unbounded
universe (a screener with an empty `screener_symbol_universe`, a dynamic/expert selection) is refused
loudly when the job is scheduled and at its gates, naming the setting.

**Refusals at the decision** (each counted per kind in the pass summary):

| Situation | Reason |
|---|---|
| source unset / malformed | `app setting iv_live_chain_source is not set ...` / `... malformed` |
| `tastytrade` with blank credentials | `... market-data credentials are not set (app settings tastytrade_market_client_secret and tastytrade_market_refresh_token)` |
| no capture ran for S (app down from close to open) | `no broker chain capture ran for session S ...; a broker snapshot cannot be backfilled` |
| a screener pick outside the captured universe | `SYM is not in the captured universe for S ...` |
| the capture refused / failed for the symbol | `the S chain of SYM was not captured: refused: snapshot not the prior-session close ...` |
| unbounded expert universe | `... screener_symbol_universe is empty ...` |

**No backfill.** TastyTrade (like Alpaca: `/v1beta1/options/quotes` -> 404) offers no historical option
quotes, so a missed capture is a refused session.

**IBKR** (not built): would need an OPRA options market-data subscription; the adapter's "frozen" market
data should return the last values after the close, but whether they equal the closing NBBO and how slow
a chain capture is with ~100 market-data lines are unknown, and the adapter has never run live.

### 6.5 Backward compatibility

For anything that does not use `iv-v1`, decisions, settings, stored configs, manifests, digests and
INFO-and-above logs are unchanged:

- **No mandatory setting and no migration.** `market_condition_option_source` is written only onto
  iv jobs, and absent means "no chain profile". The three new app settings (`iv_live_chain_source`,
  `tastytrade_market_client_secret`, `tastytrade_market_refresh_token`) are optional, have no default,
  and are read only for experts with an iv gate; blank credentials break nothing.
- **The per-instance resolver** reads the source setting, and adds it to its cache key, only for
  experts that name a chain profile (`test_an_ohlcv_expert_never_reads_or_keys_on_an_option_source`).
- **The live host registration** is silent and inert (`tests/test_iv_live_seam_registration.py`).
  An expert without a chain profile never reaches the chain seam.
- **OHLCV manifests** never carry `option_source`, and an ohlcv-v1 job's expert settings never carry
  the new key (`test_an_iv_job_records_its_option_source_and_an_ohlcv_job_does_not`).
- **The ohlcv-v1 and ta-structure-v1 live readers** are untouched. The all-off and profile-none
  goldens and every existing market-condition test pass.

For a running prod instance (8081) with no iv gate, pulling this code changes no decision,
setting or INFO-and-above log line. At startup there is one silent registration. Whenever the
scheduler (re)builds its system jobs, there is one read-only query of the `market_condition_profile`
setting rows, which finds no iv-v1 and stops there: no job is scheduled and no expert is
instantiated for it. No AV call is ever made.

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
- **AV store.** The build also reads not-yet-compacted staged days and merges them exactly as
  `AvOptionStore.compact` does: a staged row replaces the same contract's row of that day, and
  contracts the staged day lacks keep their rows. A store built mid-download therefore equals one
  built after compaction (tested, including the digest). `fetched_through` comes from the download
  ledger, and a torn last line is ignored.
- **Gap fill.** The build refuses a `--gap-fill` policy for iv-v1. The live reader reads the unfilled
  window, and the store must equal live.
- **Old manifests.** Manifests built before the rename record `thetadata-eod-parquet-v1`; that name is
  read as `thetadata` (`manifest_option_source`) and never rewritten or refused.
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
