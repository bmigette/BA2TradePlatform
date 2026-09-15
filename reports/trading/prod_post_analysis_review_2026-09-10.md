# Production 8081 review — September 10, 2026

Scheduled check started at 15:40 Paris time. Database evidence captured at
15:47:32; production had completed its analysis/submission batches by 15:36:12
and subsequently refreshed broker orders. **Eight new trades filled, but this
is not an all-clear: three take-profits disappeared, two funded entries were
refused, and yesterday's four incorrect RAT stops remain unchanged.**

`C:/Users/basti/Desktop/ba2.bat` confirms port 8081 uses
`C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite` and the adjacent
`logs/` directory. All production access was read-only (`mode=ro`,
`PRAGMA query_only=ON`). No orders, production settings, services, or strategy
code were changed. This is evidence from the platform's broker-synchronized
database and logs, not an independent broker query.

## Findings requiring attention

### 1. Confirmed: Insider entries lost their take-profit orders

| Transaction / entry | Symbol | Ruleset TP created | Canceled TP order | Active stop order |
|---|---|---:|---|---|
| 203 / 607 | CELH | $31.2832752 | 608 | 609, 3 shares, $23.8571 |
| 204 / 610 | CAVA | $57.88332 | 611 | 612, 1 share, $48.1867 |
| 205 / 613 | NCLH | $15.041026574 | 614 | 615, 6 shares, $12.8148 |

Expert 8's entry ruleset 22 buys and sets a TP. The logs show the pending limit
exit being created, followed by `initial_setup` replacing it with a stop-only
exit. The database confirms all three TP orders are `CANCELED` without a broker
ID; their replacement stops are `NEW` with broker IDs. Transaction `take_profit`
is NULL, while `meta_data.TradeConditionsData.current_target_price` still holds
the intended target. These positions have stop protection but no working TP.

The root cause is a stale transaction write, not a leverage calculation:

1. `TradeActions._AdjustPriceLevelAction.execute()` loads a transaction, calls
   `account.adjust_tp()`, then passes that original object to its post-hook
   (`packages/common/ba2_common/core/TradeActions.py:1053`).
2. Alpaca saves the TP through a **different** transaction instance in its own
   session (`ba2_trade_platform/modules/accounts/AlpacaAccount.py:3889`, `4002`).
3. `AdjustTakeProfitAction._post_broker_hook()` saves metadata through
   `update_instance(transaction)` (`TradeActions.py:1256`). That helper copies
   every loaded field, including the original NULL TP, back into the database
   (`packages/common/ba2_common/core/db.py:840`).
4. The later safeguard attachment correctly reads that now-NULL TP and builds
   a stop-only exit, canceling the intended TP leg.

**Reproduced in an isolated in-memory SQLite database:** after simulating the
broker's separate-session TP write, the actual post-hook and DB helper changed
TP `31.2832752 -> NULL`, while successfully retaining metadata target `31.28`.
No broker call was involved. The SQL-less backtest store holds the same object
by identity, so this detached-object persistence failure is not equivalent to
the normal backtest path.

The September 9 test covered combined TP+SL bracket setup and tighter-stop
selection; it did not exercise this TP-only action metadata hook. The new stop
reconciliation helper is not the source of the stale write. Expert 8's daily
open-position rules adjust SL or close; they do not recreate the missing TP.

### 2. Two funded mid-ED entries were rejected; stale pending fills are the likely cause

Risk-manager run 7 funded NAVN 22, TTAN 8, SAIL 27, AVAV 3 and CHWY 3, using
$1,904.05 available capital. Only NAVN, TTAN and CHWY were submitted successfully.

At 15:32:34.098, SAIL's $466.02 order was refused against **$29.02** available;
AVAV's $437.86 order received the same refusal at 15:32:34.298. Neither reached
the broker. Orders 594/596 and their exits were canceled; never-opened
transactions 199/200 were subsequently deleted. There is no stranded active
entry for either symbol.

The fill/pending timing is problematic:

- The broker account snapshot refreshes during submission, while local orders
  588/590/592 still have pending status and NULL filled quantity.
- Only **after both refusals**, at 15:32:34.575–.585, order synchronization records
  their fills of 3, 8 and 22 shares.
- `_stock_exposure_breakdown()` combines broker marked exposure with local
  `_pending_stock_entry_notional()`. The latter counts the entire quantity when
  local `filled_qty` is NULL (`ReadOnlyAccountInterface.py:794`). Thus a fill
  already in the broker snapshot can also remain in local pending exposure.
- The next logged sizing snapshot, at 15:34:31, has no pending entries and
  **$942.69 headroom**, despite the additional completed fills and slightly lower
  equity. SAIL plus AVAV required $903.88 at their sizing quotes.

This strongly supports false refusals caused by counting filled exposure twice.
The exact gross/pending breakdown at the rejection instant was not logged, so
its individual terms cannot be independently reconstructed to the cent. Do not
describe these as a proven lack of actual buying power or fix them by increasing
the account limit. The data sources need coherent fill accounting first.

Run 7 is recorded as `COMPLETED`, `symbols_funded=5`, with SAIL/AVAV marked
`FUNDED`: that records the sizing decision, not successful execution. Operational
reviews must join it to final order outcomes.

### 3. The four older RAT stops are still approximately 10%, not 4%

| Transaction | Symbol | Shares | Fill | Current recorded SL | 4% from fill, rounded |
|---|---|---:|---:|---:|---:|
| 127 | RARE | 10 | $15.12 | $13.6080 | $14.52 |
| 128 | ANAB | 2 | $55.48 | $49.9320 | $53.26 |
| 194 | ENOV | 14 | $19.9753 | $17.9818 | $19.18 |
| 195 | CBIO | 14 | $20.6215 | $18.5584 | $19.80 |

All remain `OPENED`, with active OCO exits and linked broker stop-limit children.
The final column is the strategy reference, not a broker modification made by
this audit. Existing positions were not repaired by the prospective entry fix.
The separate ED ANAB transaction 192 holds **3 shares**, with a $53.072024 stop;
it must not be confused with RAT ANAB transaction 128.

### 4. Margin warnings overstate what the evidence establishes

The log repeatedly says stock exposure is past the ceiling and that something
outside the experts consumed it. At 15:32:23, that warning appears alongside
an explicit capital mapping of **$1,703.40 gross exposure against a $3,607.45
ceiling, with no pending entries**. Exposure is demonstrably below that ceiling.

The warning uses remaining broker buying power versus
`equity * (broker_multiplier - margin_factor)` instead of the actual exposure
breakdown (`ReadOnlyAccountInterface.py:603`). It is not reliable evidence of
either a ceiling breach or manual/allocator activity. The actual entry gate is
separate. Today's `app.log` contained 1,116 such warnings, 353 headroom-clamp
messages, and 82 Alpaca configuration-read warnings by capture time. The latter
recover fractional-trading information via a raw response; they did not prevent
the eight submissions. Six ERROR lines describe the two refused entries at
three logging layers, not six separate rejected trades.

## What worked and how the trades were sized

The account has margin enabled, **factor 1.8**, broker multiplier **4**, and all
six enabled experts have **60% virtual equity**. Capital is multiplied by 1.8
once, then allocated at 60%; there is no extra 2x factor in this configuration.
The experts' allocations overlap and compete for the same account headroom.

| Sizing pass | Raw equity | Account capital at 1.8x | Expert capital at 60% | Gross / pending | Available after clamps | Per-symbol cap |
|---|---:|---:|---:|---:|---:|---:|
| Mid ED 11, 15:32:23 | $2,004.14 | $3,607.45 | $2,164.47 | $1,703.40 / $0 | $1,904.05 | 25% = $476.01 |
| RAT 12, 15:34:31 | $1,995.93 | $3,592.67 | $2,155.60 | $2,649.98 / $0 | $942.69 | 15% = $141.40 |
| Insider 8, 15:35:58 | $2,006.97 | $3,612.55 | $2,167.53 | $2,937.82 / $0 | $674.73 | 15% = $101.21 |

Broker buying power at these passes was $3,944.31, $2,391.00 and $1,751.87,
respectively. Account exposure headroom was the tighter constraint. Classic
per-symbol caps still use available funds, preserving the existing calculation.

Mid ED uses `risk_atr`: its 8% ATR risk budget is approximately $173.16 from
$2,164.47 expert capital. NAVN and TTAN were limited by the $476.01 notional cap;
CHWY was limited by the batch's remaining balance. RAT and Insider use notional
sizing, taking whole shares within their available-funds caps.

| Expert | Symbol / txn | Shares | Sizing quote | Fill | Filled notional | SL after fill | TP after fill |
|---|---|---:|---:|---:|---:|---:|---:|
| Mid ED | NAVN / 196 | 22 | $21.40 | $20.2050 | $444.51 | $18.9965 (~6%) | $21.90076 |
| Mid ED | TTAN / 197 | 8 | $56.1320 | $56.1594 | $449.28 | $52.7858 (~6%) | $57.445489 |
| Mid ED | CHWY / 198 | 3 | $20.3399 | $20.4115 | $61.23 | $19.1873 (~6%) | $20.81973 |
| RAT | SANA / 201 | 44 | $3.19 | $3.0598 | $134.63 | $2.9374 (~4%) | $7.08468 |
| RAT | EVER / 202 | 6 | $23.26 | $23.6552 | $141.93 | $22.7090 (~4%) | $24.128304 |
| Insider | CELH / 203 | 3 | $27.64 | $27.3786 | $82.14 | $23.8571 (~13%) | **Missing** |
| Insider | CAVA / 204 | 1 | $55.98 | $55.3900 | $55.39 | $48.1867 (~13%) | **Missing** |
| Insider | NCLH / 205 | 6 | $14.59 | $14.7498 | $88.50 | $12.8148 (~13%) | **Missing** |

Total new filled notional: **$1,457.61**. All eight entry rows are `FILLED` and
all eight have active exit orders covering their full quantities. The five
additional `HELD` stop-limit rows are children of the OCOs, not duplicate entries.
No duplicate entry submissions were found in the examined records. Fill prices
can differ from sizing quotes; caps were calculated at those quotes, not against
the eventual fill notional. Quote freshness was not independently verified.

SANA (confidence 100) and EVER (81.9) selected RAT's >=80 branch. Their stops
remain 4% from actual fills instead of being overwritten to 10%. This is direct
production evidence of the corrected stop-selection behavior. The checkout is
APP 2026.09.1147 and contains commit `5cecf06d`; the examined logs have no explicit
startup version stamp, so the precise loaded revision is not independently
attested. Mid ED's 6% safeguard correctly beats its 16% ruleset stop.

Small ED's existing ANAB stop tightened to 6%. NX requested $20.8022 (6% from
fill), then the shared 3% minimum distance from the current price moved it to
$20.640436; the log explicitly records that enforcement. This differs from an
unqualified 6%-from-entry description, but is separate from margin multiplication.
Recorded TP values also include the platform's existing minimum-profit/fill
validation; they are not multiplied by account leverage.

## Analysis completion and exclusions

All scheduled work completed; there are no failed/in-progress analysis rows
for today in the snapshot, and the persisted queue is empty.

| Expert | Entry analyses | Open-position analyses | Outcome |
|---|---|---|---|
| Large DS 7 | Not scheduled Thursday | None held | Correctly idle; Monday entry schedule |
| Insider 8 | 40 completed: 4 BUY, 36 HOLD | None held at open | 3 fills; DY cannot fit one share in $101.21 cap |
| Small ED 9 | 19 completed, all HOLD | 2 completed | No new entries; existing exits adjusted |
| Mid DS 10 | Not scheduled Thursday | 4 completed | No new entries; Monday/Tuesday entry schedule |
| Mid ED 11 | 14 completed: 6 BUY, 8 HOLD | None held at open | 3 fills, 2 submit refusals; AGX unaffordable in initial batch |
| RAT 12 | 9 completed, 11 skipped | 4 completed | 2 fills; SB's 15 confidence fails both entry thresholds |

The 11 RAT `SKIPPED` rows are distinct from order rejections; their individual
skip reasons were not exhaustively audited. The final risk-manager batch ended
at 15:36:12.527. Account refresh at 15:36:34 promoted all eight transactions to
`OPENED`, and another refresh completed around 15:41 without additional fills.

## Recommended next work, preserving backtests

1. Fix the TP metadata write so it updates only the intended metadata and
   preserves fresh TP/SL state. Add a real SQL-backed TP-only action -> funded
   submission -> fill regression for these three Insider cases. Keep the
   existing backtest decision semantics and frozen results unchanged.
2. Reconcile broker positions and local fill reservations coherently under the
   existing account submission lock. Test a fill visible in exposure before
   local order refresh, plus partial fills, cancellation and genuine outstanding
   entries. Preserve the account ceiling; do not simply drop pending reservations.
3. Correct the three missing Insider TPs and four old RAT stops through the
   normal protected order-adjustment path after checking current broker state.
   This audit has not performed those modifications.
4. Base ceiling warnings on actual exposure, and record each submission's
   capital/gross/pending terms and final outcome so false refusals are diagnosable.

This audit changes only report/evidence files, so it does not invalidate any
backtest result. It confirms the new-entry stop fix and capital multiplier, but
does **not** establish complete live/backtest parity. The already documented
cash-versus-equity and classic cap differences remain separate, deferred issues.

Evidence: [selected production snapshot and log excerpts](prod_post_analysis_evidence_2026-09-10.json).
Prior context: [September 9 entry-stop review](live_entry_stop_review_2026-09-09.md)
and [margin release review](../margin/pre_push_review_2026-09-09.md).
