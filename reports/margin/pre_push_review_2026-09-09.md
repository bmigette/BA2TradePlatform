# Margin release review — September 9, 2026

Reviewed `fix/margin-review-2026-09-09` at `78d8ccdf`, against original dev
`51234531`. The branch was fast-forwarded into dev. The user explicitly requested
pushing dev for the live system. Release versions: trade `2026.09.1145`, test
`2026.09.0025` (the shared package changes require updating the worker version).

## Findings fixed before push

### P1 — Non-finite prices and pending quantities could defeat the exposure limit

The new account gate checked `price <= 0` but did not reject NaN. A NaN current
quote made the proposed notional NaN, and `notional > headroom` evaluated false:
the entry reached submission. A working entry with a NaN limit price, quantity,
or filled quantity also poisoned the pending total and headroom, allowing later
valid orders through. Infinite filled quantities could make a reservation disappear.

Fixed in shared `AccountInterface._validate_account_exposure` and
`ReadOnlyAccountInterface._pending_stock_entry_notional`: reject non-finite
prices, notionals and quantities. The headroom calculation also rejects
non-finite terms or results. An unreadable reservation refuses new entries.
The margin-off backtest path does not execute this exposure calculation.

Evidence: 12 new parameterized cases in `test_stock_exposure_gate.py`; ten
failed before the correction, including entries reaching the fake broker.
All twelve pass after it. No real broker or market-data requests were used.

### P1 — The submit lock did not prevent duplicate submissions from stale objects

Two workers can hold different detached objects for the same persisted order.
Serializing them does not refresh the second object's `broker_order_id`.
The adapter guard therefore still sees an unsent order. If there is room, it
can send again; if capacity is exhausted, validation rejects an already accepted
order after charging its own reservation.

With margin enabled, submission now re-reads the persisted order inside the
account lock and returns an already accepted order before validation or broker
submission. It checks account ownership before returning it. Retrying an entry
is not a way to modify or reattach protective legs; those have their own adjustment
methods. The unlevered/backtest submission path remains unchanged.

Evidence: two SQL-backed stale-copy tests in
`tests/test_margin_submit_idempotency.py`, with available and exhausted capacity.
Both fail when using the branch's original `submit_order` method and pass with
the correction. The fake broker is called exactly once.

This protects workers within one application process after acceptance has been
persisted. It does not establish cross-process exactly-once submission or recover
a broker acceptance that was never saved locally.

### P1/P2 — Close-order comments could misclassify both entries and reductions

`'close' in 'closing'` is false. The final branch fix recognized “Partial close
order” but removed recognition of “Closing …” without an opposite-side transaction
to identify the reduction. Such a retry could follow the entry/transaction-creation
path. The live `TradeManager` now recognizes both spellings, with a regression case
for each. The “Closing …” case failed before this correction and now passes.

The heuristic could also mark a known same-side entry as closing merely because
its comment contained “close” (for example, “Entry close to moving average”),
bypassing entry risk checks. Transaction side now takes precedence over comments;
the additional regression case failed before this fix and now passes.

## What this release establishes

- Smart RM uses the shared sizing risk budget, fixing the demonstrated 180-versus-18
  share discrepancy. Percentage TP/SL remains a price offset; leverage scales the
  sizing capital once, not the bracket distance.
- Broker limits and the configured account exposure ceiling constrain new stock
  entries. Genuine reductions retain the branch's bypass of entry budgets.
- The three-way sizing fixtures compare an ordinary backtest/live reference with
  a smaller live account at the same effective capital. They also cover the
  user's current-equity rule: $1,900 at 2× sizes from $3,800.
- All three frozen backtest fingerprints are unchanged. No leveraged backtest
  account implementation was added.

## Remaining limits and follow-up plan

**Full live/backtest equivalence is still not established.** The two strict xfails
are deliberate, visible exceptions, not successful parity tests:

1. Backtest balance is cash while live balance is equity. After buying $1,000 from
   $4,000, shared accounting reports backtest virtual/available $3,000/$2,000 versus
   live $4,000/$3,000. Correct this only as a separately versioned backtest change,
   rerun the historical baselines, and reassess deployed configurations.
2. The classic per-instrument cap uses remaining available funds, not total virtual
   capital. Preserve the historical calculation for this release; specify and
   validate its replacement alongside the separately versioned sizing correction.

The margin-only marked-exposure clamp can also produce different available funds
from an unlevered reference holding profitable positions. Preserving old backtests
and enforcing real live capacity does not guarantee identical trades in every state.

Next live-only work: compute one exposure/capital breakdown per submission and
reuse it throughout validation and logging. The branch review estimates about
eight TastyTrade REST calls per submission under the account lock; this review
did not measure production latency. Add deterministic multi-expert concurrency,
cancel/retry reservation-release and profitable-position scenarios. Review mixed
stock/options capacity separately: option market value contributes to gross
exposure while option orders themselves are exempt from the stock-entry gate.

The fresh full backtest run also exposed an existing test-isolation gap:
`test_covered_call_engine` supplies no indicator provider, so safeguard-stop ATR
calculation reaches `FMPOHLCVProvider` and stalls in FMP's rate-limit wait. The
diagnostic traceback confirms that path. Stop relying on external market-data
availability in these fixtures; inject deterministic historical indicators.
For this release's full-suite rerun, a test-harness guard raises `OSError` before
any FMP wait/request (after preserving the normal hermetic-contract check).
This exercises the existing unavailable-data handling; it does not validate a
real ATR feed and is not a production code change.

## Validation and release scope

See [pre-push validation](pre_push_validation_2026-09-09.txt) for commands, results,
known baseline failures and frozen-file hashes. All tests use temporary databases
and fake brokers. The wider baseline is recorded in
[the previous gate](final_gate_2026-09-09.txt).

Fresh results: 99 focused common tests, 50 focused live tests, and 42 final retry
tests passed (overlapping suites). The golden/parity replay passed 45 tests with
the two documented strict xfails. Broader live paths passed 602 tests with the
two recorded TastyTrade overnight-mapping failures. The offline full backtest
suite passed 1,146 tests, with two skips, two xfails and one what-if failure
(`final_equity` 100,000 versus expected 100,100). That failure reproduces with
the original `whatif.py` from pre-margin dev `51234531`; the file is unchanged.
The full suite is therefore not reported as all green. ETF trend's 15 tests pass
separately in this runtime, unlike the earlier handoff's recorded ETF failure.

This work merges and pushes code. It does not enable account margin settings,
submit a production trade, restart the live process, or prove which revision the
live process has loaded. The original untracked audit documents were preserved in
a scoped Git stash before merging their updated branch copies. Unrelated local
files were left alone.
