# Options data and visualization audit — observations from 22 September 2026

Report completed 27 September. Code audited on 22 September: `43bc3546`. The API checks and tests below are dated evidence, not a claim about deployments or entitlements after that date. Audit only: no trading logic, account settings, or production database was changed.

## Assessment

We collect enough contract information for the existing option selectors, but **not a complete, durable dataset for explaining and comparing live and backtest trades**. IV, delta, gamma, theta and vega are available on both paths; their source, observation time and calculation differ. Expiry is recorded, so DTE is derivable. The largest gaps are entry/exit metric snapshots, liquidity information discarded in live, and contract details unavailable in the backtest popup for Parquet stores.

The live API is capable of providing more than the application retains. This was verified against the credentials configured in both the production 8081 and options-paper 8082 databases, using bounded read-only requests for two SPY calls. No orders were sent.

## What Alpaca actually supplied

Evidence: [redacted API observations](../review_evidence/options-data-2026-09-22/alpaca-capabilities.json). Probe time: **2026-09-22 11:57 UTC**, before the US session opened.

| Check | Production 8081 | Options paper 8082 |
|---|---|---|
| Configured option feed | `indicative` | `indicative` |
| Contract metadata | HTTP 200 | HTTP 200 |
| Indicative snapshots | HTTP 200 | HTTP 200 |
| Explicit OPRA snapshots | HTTP 403 | HTTP 403 |
| Historical option daily bars, bounded recent interval | HTTP 403 | HTTP 403 |
| Locally recorded ATM-IV history rows | 0 | 0 |

Both snapshot responses contained IV and **delta, gamma, theta, vega and rho**. They also contained bid/ask prices and sizes, trade prices and sizes, and separate quote/trade timestamps. Contract metadata included expiry, strike, exercise style, multiplier, open interest **and its observation date**.

An additional useful discovery: the **raw JSON snapshots contained `dailyBar` and `prevDailyBar`**, including volume, VWAP and trade count. The two contracts' latest daily volumes were 971 and 1,845 for 21 September. The installed `alpaca-py` `OptionsSnapshot` model dropped both bar objects. This is observed availability for these requests, not a guarantee that every contract/feed always supplies them. The separate historical-bars endpoint was forbidden with both tested credentials; do not assume an unrestricted historical warmup is available.

Quotes were dated 21 September, which is expected before the next session opens. The point is to display their timestamps, not to classify overnight data as an outage. Open interest was dated **18 September**, whereas the latest price bar was 21 September: OI must not be presented as a contemporaneous intraday value.

Alpaca documents indicative data as delayed trades and modified quotes. It is not interchangeable with an OPRA quote. The application explicitly selects the configured feed. [Alpaca snapshots documentation](https://docs.alpaca.markets/us/reference/optionsnapshots), [chain documentation](https://docs.alpaca.markets/us/reference/optionchain).

## Coverage matrix

“Available” does not mean populated for every contract. Null fields must remain unknown.

| Metric | Alpaca availability | Live collection / retention | Backtest collection / retention | UI coverage |
|---|---|---|---|---|
| Underlying, call/put, strike, expiry, multiplier | Contract API; confirmed | Recorded contract/order terms; rejects nonstandard deliverables | Recorded on trades and source rows | Both display terms |
| DTE | Derive from expiry and an explicit observation date | Computed for selection and current live display | Computed for selection; recoverable at entry/exit | Live shows current DTE; test detail does not expose entry/exit DTE |
| Bid, ask, last, midpoint, spread | Snapshots; confirmed | Available transiently; not a full immutable trade snapshot | Theta Parquet can retain real bid/ask; other caches can use a zero-spread close-price proxy | Live shows bid/ask/mid/last; historical execution spread provenance is incomplete |
| IV, delta, gamma, theta, vega | Snapshots; confirmed | Four Greeks and IV mapped into DTOs; current detail only | Daily model-derived IV/Greeks; SQLite stores columns, Parquet computes on demand | Live current values; test supports SQLite contract detail only |
| Rho | Snapshot; confirmed | Discarded | Not returned by option-history Greek wrapper | Neither |
| Quote/trade timestamps and sizes | Snapshots; confirmed | Single quote retains quote timestamp; chain drops timestamps; sizes and trade time dropped | Daily history is not a time-of-entry quote | Live hides retained quote timestamp; test labels daily/prior-session references |
| Session volume, VWAP, trade count | Raw snapshot bars observed; historical bars API exists but tested access forbidden | SDK drops snapshot bars; chain volume stays `None` | Daily volume retained; not full intraday liquidity | Test API includes volume but contract-detail table drops it; live detail omits it |
| Open interest and observation date | Contract API; confirmed | Chain retains OI count, drops date; quote/detail path omits OI | Source-dependent; Parquet can carry dated-row OI; SQLite daily bar schema has no OI | Test has an OI column but often unavailable; live detail lacks it |
| ATM-IV history / percentile | Must archive observations or derive from historical prices | Daily recorder exists; both inspected databases empty | Reconstructed from historical provider | Live percentile calculation differs from the rule |
| ITM/ATM/OTM, intrinsic/extrinsic value | Derive from same-time underlying, terms and premium | Inputs mostly available; no complete stored observation | Inputs available with daily-time limitations | Test shows moneyness; live lacks equivalent badges; extrinsic/intrinsic breakdown absent |
| Breakeven, max profit/loss, expiration payoff | Derive from legs and premiums | Shared payoff calculation | Shared payoff semantics | Test has cards and overlay; live has overlay but fewer controls/cards |
| Net delta/gamma/theta/vega, portfolio exposure | Derive using signed contract quantities and multiplier | No complete aggregated option-risk view found | Not persisted as trade/portfolio risk history | Neither offers a full risk dashboard |

Alpaca's documented models expose the five Greeks and dated contract metadata. Historical option bars are a separate resource; this audit did not find a documented historical broker-IV/Greek snapshot endpoint suitable for replaying old observations. [Data models](https://alpaca.markets/sdks/python/api_reference/data/models.html), [contract models](https://alpaca.markets/sdks/python/api_reference/trading/models.html), [historical option API](https://alpaca.markets/sdks/python/api_reference/data/option/historical.html), [historical bars](https://docs.alpaca.markets/us/reference/optionbars).

## Findings, ordered by practical impact

### 1. High — live volume gates cannot currently use the volume Alpaca supplies

`AlpacaAccount.get_option_chain` never populates `OptionContract.volume`. The shared selector explicitly raises `OptionLiquidityDataUnavailable` if a nonempty chain publishes no volume and a volume gate is enabled. This is a capability gap versus backtests, whose selectors receive daily bar volume. It is conditional on the rule enabling that gate; this audit does not establish that an active production rule currently does so.

The raw response proves that a useful source exists even on the tested indicative feed, but the SDK/application mapping discards it. Capture volume together with its **session date and completeness**. Prior-session full-day volume and today's partial volume are different inputs; do not change the selection rule's meaning merely by filling the missing field.

Sources: `ba2_trade_platform/modules/accounts/AlpacaAccount.py:5792`; `packages/common/ba2_common/core/option_selector.py:193`; `packages/common/ba2_common/core/option_types.py:8`. [Offline reproduction](../review_evidence/options-data-2026-09-22/offline-probes.json).

### 2. High for audit/replay — no complete entry and exit option-metric records

Live transactions/orders retain terms, quantities, premiums and lifecycle information. `OptionIVSnapshot` is only an underlying-level ATM-IV history table. Neither supplies a per-leg immutable record of the IV, Greeks, quote, liquidity and underlying observation used to make a trade decision.

Backtest trade serialization retains trade terms and results, but strips the opportunity to retain those analytical snapshots. Its popup reconstructs contract details from the cache later. That is useful context, but not proof of the exact values the engine used at entry.

Consequences: after a trade closes, a current live quote cannot explain its original delta/IV; expiry can make even that current lookup impossible; cache changes can alter reconstructed historical context. Exact live/backtest replay remains incomplete.

Sources: `packages/common/ba2_common/core/models.py:900`; `testplatform/backend/app/services/backtest/results.py:385`; `ba2_trade_platform/ui/pages/live_trades.py:2180`.

### 3. High for analysis — the backtest popup does not read Theta/Tasty Parquet contract detail

`build_trade_chart_context` only creates an `OptionsHistoryCache` reader when recorded provenance identifies SQLite. A run recorded against another store gets an explicit unsupported-store explanation and unavailable contract details, even though the backtest engine can compute Greeks from that store.

This is honest missingness, not fabricated data, but it prevents the current Parquet workflow from exposing its IV/Greeks beside trades. Add a read-only reader for the **run's recorded store and model parameters**, preserving prior-session/as-of rules. Do not silently read another cache.

Source: `testplatform/backend/app/services/backtest_trade_chart.py:537`.

### 4. Medium — the live popup's IV percentile can disagree with the trading rule

The popup counts samples **at or below** current IV and reads all stored dates. The rule counts **strictly below**, excludes today, uses a 365-calendar-day default window, and validates plausible IV values. The popup also fixes its minimum sample count at 20 rather than describing the active rule's parameters.

Reproduction through the actual methods: with 20 historical samples of 0.30 and current IV 0.30, **popup = 100%; shared rule = 0%**. Existing UI tests accept the popup's independent formula, so passing tests do not establish parity here. Both inspected databases currently contain no samples, which masks the visible discrepancy for now.

Use the shared calculation and expose window/sample count/as-of date. Also label the statistic **IV percentile**, or explain it: the implemented fraction-below statistic is not the range-based `(current − minimum) / (maximum − minimum)` definition commonly called IV rank.

Sources: `ba2_trade_platform/ui/pages/live_trades.py:2233`; `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:943`, `:1000`, `:1091`. [Reproduction](../review_evidence/options-data-2026-09-22/offline-probes.json).

### 5. Medium — live “RIGHT NOW” hides feed and observation age

The popup retains the quote timestamp during collection but omits it from rendering. It also omits `indicative` versus OPRA, bid/ask sizes and OI age. The chain DTO drops quote/trade timestamps altogether. The observed premarket response is a concrete example of “fetched now” not meaning “observed now.”

Display feed, quote time, trade time, OI date and fetch time separately. Missing or stale data should be explicit; use session-aware freshness, not an overnight wall-clock timeout. Rho is a lower-priority omission than these fields.

Sources: `AlpacaAccount.py:5920`; `ba2_trade_platform/ui/pages/live_trades.py:2266`.

### 6. Medium — live and historical IV/Greek inputs are not identical

Live consumes Alpaca's supplied IV/Greeks. Historical readers use European Black–Scholes inversion of daily option and underlying prices; this approximates American equity options and depends on rate/dividend/time assumptions. The Parquet reader preserves vendor IV separately as `vendor_iv`, but selection uses its computed IV.

ATM selection differs too: live picks the nearest strike to current spot over 20–45 DTE, without restricting to calls; both historical readers select the call nearest 0.50 delta over that range, then break ties by expiry and strike. These are different definitions and can affect IV-dependent decisions. This audit establishes a methodological difference, not its profitability impact or a measured rate of mismatched trades.

Old SQLite rows can also fall back to a chain-level Greek snapshot when a daily bar lacks computed IV. That fallback should carry its own source/date and not be represented as a fresh entry observation.

Sources: `AlpacaAccount.py:5964`; `testplatform/backend/app/services/backtest/parquet_options_provider.py:1215`; `testplatform/backend/app/services/backtest/options_provider.py:220`; `testplatform/backend/app/services/backtest/option_greeks.py:1`.

Do not alter these definitions as a UI fix: changing selector inputs could change historical results. First collect paired observations and quantify differences, then version any strategy-facing correction separately.

### 7. Medium/low — visualization is useful but incomplete

**Backtest strengths:** a single underlying chart with strike lines, trade markers and green/red expiration-payoff regions; whole-structure/individual-leg selection; payoff and breakeven cards; neutral ITM/OTM badges; explicit daily/prior-session data quality and source explanations. Recorded P&L is distinguished from hypothetical payoff and end-of-run marks.

**Backtest gaps:** Parquet detail above; IV displayed as raw `0.300` without a percent unit while live shows `30.0%`; API volume omitted from the table; no entry/exit DTE; no aggregate structure Greeks, risk-history chart or Greek-driven P&L attribution. Missing per-leg P&L makes the displayed total explicitly partial, which is good.

**Live strengths:** a dedicated options tab, contract/expiry/DTE/premium/P&L columns, transaction detail and the requested candle/payoff overlay with markers. Current contract Greeks and IV are separate from expiration payoff.

**Live gaps:** no corresponding entry/exit metric comparison; no visible quote freshness or OI/volume/sizes; fewer payoff summary cards and no rendered overlay/leg toggles despite builder support; no net structure/portfolio Greeks or equivalent moneyness badges. Current collection calls the broker per leg; the snapshot endpoint supports batching, so a shared batch fetch would improve consistency and latency.

Sources: `testplatform/frontend/src/components/OptionTradeDetails.tsx`; `testplatform/frontend/src/lib/contractDetail.ts:58`; `testplatform/frontend/src/components/TradeChartModal.tsx`; `ba2_trade_platform/ui/pages/option_trades.py`; `ba2_trade_platform/ui/components/option_structure_chart.py`.

## Suggested implementation sequence

1. **Preserve observations without changing trade decisions.** Introduce a versioned per-leg snapshot at decision time, fill/reconciliation and close: terms; observed/fetched times; feed/provider; underlying price/time; bid/ask/sizes; last/time; IV and five Greeks with units; volume/session/completeness; OI/date. Keep missing fields null with reasons. Do not pretend a post-fill lookup was captured at the fill instant.
2. **Reuse that schema in backtests.** Persist exactly what the selector and valuation used, plus model/rate/dividend/price-source metadata and cache identity. Record decision and fill observations separately. Older trades remain explicitly reconstructed/partial.
3. **Fix UI-only gaps.** Shared percentile helper, consistent IV percent units, DTE at entry/exit/current, freshness/feed badges, volume/OI dates, Parquet reader and live payoff controls. These should not change historical trade outcomes.
4. **Add useful derived analytics.** Signed, quantity- and multiplier-adjusted net Greeks; spread dollars/percent; intrinsic/extrinsic value; moneyness distance; payoff limits/breakevens; liquidity and coverage summaries. Label model units before aggregating Greeks.
5. **Warm and cache deliberately.** Archive a defined option universe daily, batch contract snapshots, retain source timestamps, and share immutable history through the central cache. Existing ATM-IV recording only targets enabled IV-percentile-gated experts; zero rows alone does not prove it is broken. A historical warmup cannot recover unrecorded broker Greeks from today's snapshot, and the tested accounts cannot currently use Alpaca's historical-bars endpoint.

Skew, term structure, IV percentile and IV-versus-realized-volatility are feasible **derived** studies after suitable history is retained. Exact probability of profit, dealer positioning/GEX, intraday OI change and actual investor trade direction are not fields verified in the tested API. Do not present estimated values as broker facts. Current chains also do not reconstruct the historical set of rejected candidates; exact selector replay requires decision-time universe snapshots, not just snapshots of the selected legs.

## Validation and limits

- **182 live tests passed**, including the offline reproductions of dropped fields, the enabled volume-gate failure, and UI/rule percentile disagreement.
- **144 backend tests passed** for historical providers, Greek cache columns and chart context.
- **104 frontend tests passed** for option chart, view state, trade mapping and contract detail.
- Outputs: [live](../review_evidence/options-data-2026-09-22/live-tests.txt), [backend](../review_evidence/options-data-2026-09-22/backend-tests.txt), [frontend](../review_evidence/options-data-2026-09-22/frontend-tests.txt).
- Probe scripts: `test_files/options_data_audit_20260922.py` and `test_files/options_data_audit_20260922_offline.py`. The API probe is opt-in with `--api`, reads SQLite in read-only mode, and writes only allowlisted non-secret response metadata.
- Visualization assessment was source/component-test based, not a fresh end-to-end browser inspection. No full historical-store coverage scan, intraday market-hours entitlement test, trade execution or backtest rerun was performed. Schema availability does not establish complete history for every symbol.
