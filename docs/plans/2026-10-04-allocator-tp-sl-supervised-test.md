# Allocator TP/SL: supervised live test (operator checklist)

Purpose: confirm on the real TastyTrade account the broker behaviours the code relies on
(`BROKER_ASSUMPTIONS` A1 to A10 in `tests/allocator_protection_fakes.py`) before the feature is used for real.
You place real orders in this test, with a tiny position. Take your time; nothing here is urgent.

## Before you start

- Instance: prod (port 8081). Time: regular market hours.
- One liquid symbol you hold, 10 to 20 whole shares. Do not buy or sell that symbol by any other means during the test.
- No allocator run is scheduled for the test window. Do not start one.
- Keep the TastyTrade site open on the Orders tab. Everything the app shows must match the site.

## Steps

1. **Version.** The sidebar footer shows APP 1226 or newer, and the database is at Alembic `b8d1f4a29c63`.
2. **Stop only (A8).** In the allocator page, open TP/SL for the symbol and save a stop about 5% below the price, no
   take-profit. On the site there is ONE GTC stop for the whole number of shares. Its External ID looks like
   `ba2prot:<id>:<n>:<8 hex characters>`. In the dialog the slice is LIVE, the GTC column shows a date WITHOUT a `*`,
   and the order ids match the site.
3. **History probe (A10).** Run the read-only helper and keep its output:
   `.venv\Scripts\python.exe tools/tt_protection_probe.py --account-id <id> --tag <the External ID from step 2>`.
   Required: the tag search finds the order and does not fail with an HTTP 400; the plain history is reported
   NEWEST FIRST and shows 0 rows older than the start time; you note whether the complex history is newest or oldest
   first; once step 4 exists, run it again and check the OCO legs show a `complex_order_id`.
4. **Take-profit plus runner (A3, A2).** Save a take-profit about 60% of the shares at a price well above the market,
   the rest as a runner, stop 5% below. The OCO dry run passes. Its quantity is counted ONCE: the total reserved on the
   site equals the shares held. Then, on the site, try to enter a SELL limit for 1 share about 50% above the market:
   it must be REFUSED because every share is reserved. If it is accepted, cancel it at once and stop here (see abort).
5. **Resize (A5).** Press Resize protection. The old orders show Cancel Requested and then Cancelled within the
   timeout, no CANCEL_UNCONFIRMED alert appears, and there are no duplicate working orders afterwards.
6. **After the close (A9, A6).** After 16:00 the statuses stay live in the dialog. The next morning there is no
   RECONCILE_FETCH_FAILED alert.
7. **A take-profit fill (A7, A4).** Change the take-profit so it sits 1 to 2 ticks above the bid, on a small slice
   (about 2 shares). When it fills: the partner stop of that OCO is Cancelled; a FILL note appears with the weight
   change and any pinned symbols; the sale is booked (a synthetic sell in the order list); NO protection is added
   for 5 minutes (settle wait). If it fills only partly: the partner stays live for the remainder and the slice shows
   the part taken.
8. **Cancel on the site.** Cancel ONE protective order on the TastyTrade site. The app shows a LOST_CANCELLED alarm and
   re-places NOTHING over the next two refreshes. Resize protection clears it.
9. **Manual partial close.** Close part of the position from Live Trades. The log shows CANCELLED_FOR_SALE, the sale
   fills, and protection is re-placed at the new size after the settle wait.
10. **Disable.** Switch protection off. Every protective order is cancelled and the cancel is confirmed on the site.

## Abort criteria

Stop the test immediately, press Switch off, and if the cancel is not confirmed cancel the orders on the site by hand
and then press Switch off again. NEVER move a stop above the market to force an exit. Abort on any of:

- a short position, or a protective sell larger than the shares held;
- duplicate working orders for one slice;
- quantity or price on the site that differs from the dialog;
- a CANCEL_UNCONFIRMED, WEIGHT_FAILED or TXN_FAILED entry;
- an UNKNOWN_STATE alert open for more than 10 minutes;
- the step 3 search fails with a 400 or does not find an order that is on the site;
- the step 4 test sell is accepted and fills.

A GTC column that shows `*` (no date from the broker) is NOT an abort: write it down. Renewal will not run for that
order, so check its expiry by hand.

## What to record

The probe output (step 3), the External ID format, whether the complex history is newest or oldest first, the GTC date
shown, and any deviation from the text above. Send these back before general use.
