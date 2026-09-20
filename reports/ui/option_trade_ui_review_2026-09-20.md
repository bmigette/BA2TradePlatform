# Option trade UI implementation review

Date: 2026-09-20

Implementation reviewed: `778abf67` through `c41f8156` on `dev`. HEAD advanced to `16e02a06` during review; the intervening commits only add existing reports and an ignore rule.

Verdict: the feature is present, but **not ready to treat its live structure P&L and charts as reliable**. Two incorrect-money calculations and several presentation/data-wiring defects remain.

## Where options appear now

**Live Trades → Options** is implemented. **Stocks** is the first/default tab. There is no separate Options sidebar menu or route. The Options tab uses `Transaction.asset_class == OPTION`, lists one transaction per structure, and opens the shared transaction-details dialog through its details action.

The live dialog now contains order terms, a Plotly underlying chart with a rotated expiration-payoff overlay, and a current broker contract-detail block. The backtest dialog uses the existing lightweight-charts candle chart with an SVG payoff overlay and a leg/moneyness table.

The single-chart design is therefore implemented on both sides. Its data selection and interaction behavior need correction before the spec can be called complete.

## Findings

### R1 — P1: the live Options tab misprices a spread through the single-contract path

Location: [`option_trades.py`](../../ba2_trade_platform/ui/pages/option_trades.py), `_build_rows`, lines 304–310.

The representative order is always `option_orders[0]` whenever contract legs exist. A spread has contract legs, so `option_transaction_pnl` dispatches to `_get_option_pnl_via_transaction`, not the spread function. That function uses the chosen leg's quote against the **parent transaction's net entry premium and quantity**. It does not even calculate that leg's independent P&L correctly in this case.

Offline reproduction with the actual loader method and shared single-leg pricing function:

- Entry: long 95 call at 8, short 105 call at 2; parent debit 6, one structure, multiplier 100.
- Current executable prices: long bid 13.30, short ask 3.60.
- Correct combined mark: `(13.30 - 3.60 - 6) × 100 = +$370`.
- **Displayed by the Options loader: +$730**, using `(13.30 - 6) × 100`.

Fix: select the actual multi-leg opening parent and call the existing structure pricing seam. For a true single-contract transaction, keep the single-contract path. Add a loader-level regression with a parent plus its legs; the existing dispatch tests only test already-correct caller inputs.

### R2 — P1: live payoff construction treats closing/cancelled orders as new positions

Location: [`live_trades.py`](../../ba2_trade_platform/ui/pages/live_trades.py), `_payoff_for`, lines 2127–2147.

Every order with a contract symbol becomes a payoff leg. There is no check of executed status, opening/closing intent, `filled_qty`, or lifecycle. The requested quantity is used even when an order filled only partly.

The same spread's original expiry payoff is **-$600 below 95 / +$400 above 105**. Adding its closing sells/buys to the dialog input changes the plotted curve into **+$370 at every underlying price**. It describes the net cash of already closed fills rather than the entry structure's possible expiration outcomes. A cancelled pending order can also make the whole chart unavailable or add exposure that was never held.

Fix: define the displayed basis explicitly. Reconstruct the entry position from executed opening fills with actual filled quantities and weighted entry premiums. If offering a current-position view, build it separately from remaining signed quantities and keep realized cash explicit. Do not concatenate the entire order history into a portfolio. Test closed, partially closed, scaled, cancelled, and partially filled structures. Derive displayed leg counts from the same normalized contracts; current order counts also count exits as extra legs.

### R3 — P2: live markers use the latest close for every historical event

Location: [`option_structure_chart.py`](../../ba2_trade_platform/ui/components/option_structure_chart.py), `chart_inputs_from`, lines 122–149.

`spot = normalised[-1]['close']` supplies the Y value of every structure and order marker. A September 8 entry candle with range **99–103** was annotated at **130**, the September 11 close in the probe. With 20 days of chart padding, this may even be a price after the exit. These are not markers on the relevant bars.

Fix: anchor visual arrows to the event's own candle high/low, keeping that visual anchor separate from an exact/estimated spot quote. Preserve the honest **order placed** label where no per-leg fill timestamp exists. Do not imply a later close was the entry price. Test missing sessions and separate entry/exit markers on the same bar.

### R4 — P2: resizing the price axis detaches the backtest payoff from prices

Location: [`TradeChartModal.tsx`](../../testplatform/frontend/src/components/TradeChartModal.tsx), chart subscriptions at lines 226–240 and SVG projection at lines 245–317.

The SVG is recomputed for changes to the visible **time** range and window size. A vertical price-axis drag changes candle and strike coordinates without triggering either event.

An isolated Edge browser check confirmed: the canvas changed, but the SVG path was byte-identical. The curve's strike kinks and zero crossing then refer to the wrong underlying prices. This is a calculation presentation error, not just an appearance problem.

Fix: use a supported chart primitive that renders against the current price scale, or explicitly invalidate projection for every price-scale/size change. Keep the curve, fills, bands, zero line and future P&L ticks in that same render lifecycle. Add an actual browser regression that drags the price axis.

### R5 — P2: default marker sets overlap and are not chronologically ordered

Locations: [`optionChartView.ts`](../../testplatform/frontend/src/lib/optionChartView.ts), `legMarkers` and `allMarkers`, lines 293–330; [`TradeChartModal.tsx`](../../testplatform/frontend/src/components/TradeChartModal.tsx), line 223.

The supplied sequence is structure entry, structure exit, leg entry, leg exit: **Sep 8, Sep 11, Sep 8, Sep 11**. It is not sorted for the chart's chronological marker processing. Both the structure entry and leg entry are below the same bar without layout coordination; multiline text is painted as overlapping long canvas labels. The captured popup has an unreadable entry label. Leg exits also use the same below-bar/up-arrow shape as entries.

Fix: construct a sorted, event-aware marker layout. Keep entry and exit distinct, including same-day cases; group multiple legs into a short marker with details on hover or in a linked panel. `open_at_end` currently also becomes a **Structure exit** marker: label it a run-end valuation instead.

### R6 — P2: backtest Greeks/IV/OI are not wired through to the user

Locations: [`backtest_trade_chart.py`](../../testplatform/backend/app/services/backtest_trade_chart.py), `options_store_path`, lines 63–83 and reader creation at 409–417; [`btApi.ts`](../../testplatform/frontend/src/lib/btApi.ts), `TradeChartLeg`; [`OptionTradeDetails.tsx`](../../testplatform/frontend/src/components/OptionTradeDetails.tsx).

There are three independent gaps:

1. The lookup checks `options_cache_db`, `options_db_path`, `settings`, `opt_block`, and `config` attributes. The persisted `Backtest` model does not have those attributes/columns. It has `strategy_params`, `results`, and an `optimization_id`, among other fields. Unit fixtures add attributes that normal ORM rows do not carry. The normal saved-result path therefore cannot resolve the store this way.
2. It always instantiates `OptionsHistoryCache` (SQLite), ignoring the existing option-store selection between SQLite and parquet providers. An `options_cache_db` flag/path alone does not prove that SQLite supplied that run's option data.
3. The API schema accepts `entryContract` and `exitContract`, but the TypeScript leg type and component never consume/render them. The browser fixture supplied real-looking IV, delta, gamma, theta, vega, volume, and OI values: none was displayed. In addition, the service emits `open_interest` while the response model expects `openInterest`, so that value would be dropped even if supplied. The current SQLite daily-bar schema itself does not contain an OI column.

Fix: resolve/preserve the actual run's store identity through persisted provenance, use its existing read-only viewer reader, explicitly map supported fields, and render entry/exit contract detail with dates and missing-data reasons. Do not silently substitute the platform's current default store or fill missing OI from an unrelated snapshot. Older runs without provable provenance should say unavailable.

### R7 — P2: the new entry Greeks lookup discards the intraday time

Location: [`backtest_trade_chart.py`](../../testplatform/backend/app/services/backtest_trade_chart.py), `contract_detail`, line 104.

The new lookup uses `event.date()` and includes that day's option bar. A **13:30 UTC entry** therefore gets the same day's daily IV/delta, which can be based on a close not yet available at entry. The docstring calls this point-in-time clamping, but it only clamps the calendar date. The separate underlying-reference path correctly avoids the same-day close for a timestamped morning event, so the two panels can describe different observation times.

Fix: apply observation-availability semantics. For a morning entry with daily data, use the prior completed session and label it approximate/stale. A daily retrospective snapshot is also displayable if explicitly labeled as such, but must not be described as entry-time Greeks. This is a **new display-path defect**; this review does not propose changing the trading engine or existing historical results.

### R8 — P2: the supposedly read-only chart endpoint can create or migrate an option cache

Location: [`backtest_trade_chart.py`](../../testplatform/backend/app/services/backtest_trade_chart.py), line 415; constructor in [`options_cache.py`](../../testplatform/backend/app/services/backtest/options_cache.py), lines 43–53.

`OptionsHistoryCache` opens SQLite normally and runs CREATE/ALTER/index setup in its constructor. In the offline probe, a saved path pointing to a previously absent file caused opening chart context to **create a new database**. An older cache could also be migrated. The underlying-history path is cache-only; the newly added option-detail path is not read-only by construction.

Fix: use an existing-file, read-only connection/reader; return unavailable for a missing file, with no directories/files created and no schema work. Keep warming/migration outside a GET request. Test absence and read-only permissions as well as a fully populated cache.

### R9 — P2: Options totals change when paging or sorting

Location: [`option_trades.py`](../../ba2_trade_platform/ui/pages/option_trades.py), lines 235–260, 279–372 and `_refresh_totals`.

With normal sorting, SQL pagination happens before `_build_rows`, and the totals are accumulated from only that page. With a computed sort, `_build_rows` sees all matching transactions before the result is sliced. The label remains **TOTAL (open option structures)** in both cases. Merely changing sort mode can change the displayed cost and P&L for the same filter.

Fix: calculate totals for the same full filtered/account-scoped set independently of pagination, or explicitly make them page totals with identical behavior under every sort. Continue to distinguish unpriced positions and unknown cost; do not show a partial sum as a complete portfolio total.

### R10 — P2: option quote collection blocks the NiceGUI event loop

Location: [`option_trades.py`](../../ba2_trade_platform/ui/pages/option_trades.py), async `_data_loader` calling synchronous `_build_rows` at line 256, which calls broker pricing at lines 306–315.

Every refresh does sequential account/quote calls on the async loader's thread. The default is 20 visible rows with a 30-second refresh; a computed sort processes the whole result set. A slow broker makes this UI path block its event loop, rather than just showing a loading state for the table. Single-leg rows can fetch the same quote again for the Current column.

Fix: move blocking reads into a worker thread, batch/deduplicate by account and contract where supported, and bound refresh concurrency. Reuse one quote snapshot for both P&L and displayed current premium. Test a delayed broker fixture while another UI callback remains responsive.

## Remaining delivery gaps

These were also verified in code or the isolated popup, and should be included in the finishing pass:

- **Live moneyness and payoff figures:** the Python moneyness/payoff helpers exist, but the live renderer does not display ITM/ATM/OTM, numeric breakevens, or max profit/loss. The Plotly renderer receives toggle flags but the dialog supplies no marker/overlay toggle controls. Current broker spot/Greeks alone do not provide historical entry/exit moneyness.
- **Backtest quantitative overlay:** the SVG has a vertical zero line but no horizontal P&L scale/tick labels, despite the locked one-chart specification. The screenshot therefore cannot tell the reader how much P&L the horizontal distance represents.
- **Incomplete shading domain:** `zoneBands` stops at the strike/breakeven-based suggested domain. The 95/105 spread shades only roughly 94–106, so candles at 108–110 have no profit shading even though the position is profitable there at expiration. Clip mathematically complete sign intervals to the visible price range; flat zero-payoff intervals should remain neutral, not green.
- **Transaction selection:** a backtest structure parent still only expands/collapses; it does not open the chart. A leg opens the complete transaction, but the popup header still shows that leg's percent/reason. The fixture header reads **+5.30%** while the complete structure's stored result is **+$370**. Keep the scope and denominator explicit.
- **Missing snapshot context:** backtest moneyness shows a quality label but not the observation timestamp/age. The context's `resultDigest` is not checked by the modal. Price adjustment/deliverable compatibility is not represented in the delivered schema, although the spec requires it before combining historical spot and contract strikes.
- **Input and dialog polish:** missing P&L contributes zero to the recorded total; different/missing expiries need stricter compatibility handling; close-by-Escape/focus containment is not implemented in the React modal. The screenshot also shows cramped table headings and marker labels. These deserve fixture/UI coverage, rather than more pure arithmetic tests alone.
- **IV label:** live `_iv_rank` computes an empirical percentile over all stored samples, not a min/max-normalized rank over an explicit window. Its label/window should be made explicit and matched to the intended platform convention before users compare it with other IV-rank displays.

## Validation performed

| Check | Result |
|---|---|
| Frontend option payoff, overlay-helper, and existing option-trade tests | **82 passed** |
| Backend saved trade-chart context tests | **20 passed** |
| Live option P&L, option-detail, chart-figure, and tab tests | **48 passed** |
| Shared Python payoff-chart tests | **28 passed** |
| Additional offline probes against actual methods | All **5 defects reproduced**: wrong spread P&L, closing-fill payoff cancellation, marker Y error, same-day Greeks lookup, cache file creation. |
| Isolated Edge rendering of the actual React popup | No page errors; one mocked context request. Greeks absent, marker labels overlap, and price-axis drag changes candles without updating the SVG. |

Total: **178 existing tests passed**. The existing coverage validates useful arithmetic and helper behavior, but does not catch the caller/lifecycle/browser issues above. Initial combined Python collection hit duplicate `tests.conftest` module names; running the common suite separately resolved collection. Restricted-process launches also required the installed runtimes to run outside the sandbox. These were test-environment issues, not product failures.

Reproducible review assets:

- [Offline Python probes](../../test_files/review_option_ui_20260920.py). Assertions deliberately confirm the observed defects; these are investigation probes, not desired-behavior regression tests.
- [Isolated browser harness](../../test_files/option_ui_review_20260920/browser.cjs) and [React fixture](../../test_files/option_ui_review_20260920/harness.tsx). All HTTP requests are intercepted; no real account or API credentials are used.
- [Browser evidence](assets/option-ui-review-2026-09-20/evidence.json).
- [Before price-axis drag](assets/option-ui-review-2026-09-20/popup-before.png).
- [After price-axis drag](assets/option-ui-review-2026-09-20/popup-after-axis-drag.png).

This was an implementation review with synthetic fixtures, not a production broker trial or a complete end-to-end run of both deployed applications. I did not click live close/edit actions, change production data, submit orders, rerun backtests, or alter experts/rules/valuation logic. The option chart changes inspected add presentation/read-only access plus a new shared chart helper; no evidence here establishes changed historical backtest results.

## Fix order

1. Fix **R1/R2** and add transaction-loader/lifecycle regressions before relying on any live option dollar figures or risk curves.
2. Fix **R3–R5** and finish the one-chart scale, sign bands, and moneyness/limits in both UIs. Validate with actual rendered charts and axis interactions.
3. Complete contract-detail provenance/rendering and fix **R6–R8** with cache-only, missing-cache, parquet/SQLite, and intraday-availability tests.
4. Fix **R9/R10**, then finish structure-parent selection, labels, stale-result protection, missing-data and accessibility behavior.

Keep these corrections in UI adapters, data-access helpers, and tests. Preserve recorded backtest results and the trading engine. A successful finishing pass should update the spec's acceptance checklist with the actual browser and lifecycle evidence, not just its test counts.
