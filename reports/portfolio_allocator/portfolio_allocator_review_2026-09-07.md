# Portfolio allocator code review — 7 September 2026

**Result: five reproducible defects, including three high-priority issues.** The existing allocation arithmetic and service tests are extensive: **1,479 selected tests passed**. The additional reproductions below expose gaps between planning, broker submission and income reconciliation that those tests do not cover.

This was an audit. Application code, production settings and production orders were not changed. All reproduction orders went to a fake account and all reproduction database writes went to an isolated in-memory SQLite database.

## Scope and relevance to production 8081

Reviewed the shared calculation engine and store, live submission service, page/wizard flow, broker margin adapters and the associated tests. Source revision observed: `fa0929f5915754d9d7b91529ba1288bdf5361050`. The evidence file also records SHA-256 hashes of the five central source files, because the working repository can change during an audit.

The feature allocates **manually traded accounts by label and symbol**. It does not distribute capital among the six trading experts.

A read-only snapshot of the production database at approximately 11:25 UTC showed:

| Account | Allocator state |
|---|---|
| 2 — TastyTrade | Market valuation, fractional enabled, 10% reserve, 12 managed labels totaling 100% |
| 1 — Alcapa Live | Six enabled experts; the page gate should exclude it even though its manual flag is saved as true |

There were **no recorded allocation runs** in this database. TastyTrade also had **zero local TradingOrder rows and therefore no linked transactions**. I did not request its live broker holdings. If it already holds managed stocks at the broker, finding PA-05 is particularly relevant to its first rebalance. Nothing in this review establishes that any of these defects has already caused a production trade or loss.

## Findings

| ID | Priority | Finding | Reproduced consequence |
|---|---|---|---|
| PA-01 | P1 | Submission trusts stale positions and an already-used rebalance plan | A target of 10 shares becomes 20; a $1,000 reserve is spent |
| PA-02 | P1 | Rounding recovery discards the higher cost returned by broker precheck | A plan requiring $400 is reported as fitting $200 |
| PA-03 | P1 | Reconciliation can permanently finalize a run during submission | $1,000 of filled buys consumes $0 of income; later reconciliation cannot repair it |
| PA-04 | P2 | Manual-account and enabled-expert restrictions are checked only when entering the page | A stale dialog submits after the account becomes ineligible |
| PA-05 | P2 | Untracked holdings are counted as sale funding, then skipped with a success outcome | A planned $1,000 sale never happens, while its dependent buy is attempted |

### PA-01 — Recheck the account and make submission idempotent

The page passes the `current` positions and `base` retained from the dry run straight to the service. The service checks target totals, missing prices recorded on that base and market hours, but does not fetch current positions or buying power before sending the stored deltas. Its reconciliation pass updates the income ledger; it does not rebuild the plan. There is no account-level submission lock or durable identifier for an approved plan. The wizard's `_submitted` latch protects one dialog from a second click, but two dialogs have separate latches.

**Reproduction:** start with $2,000 cash and a 50% reserve. Two dialogs review the same target: buy 10 shares of BBB at $100 and retain $1,000. Submit the first, then the stale second. The service creates two run IDs and sends both 10-share buys, with **zero position or account-snapshot reads at submission**. The fake account ends with 20 shares and $0 of the intended reserve. Both buys fit the original cash balance, so a broker buying-power check alone would not prevent this example.

**Change:** serialize submission per account, re-read positions and working orders, and reject a stale plan for a fresh review when the intended deltas have changed. Bind an approved plan ID to at most one submission. Account for working orders before admitting a later rebalance. Do not silently replace the approved quantities with newly calculated quantities and submit those without review.

Evidence: [page submission boundary](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/ui/pages/portfolio_allocation.py:639), [service submission](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:1910), [per-dialog latch](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py:1903).

### PA-02 — Preserve broker-prechecked costs when restoring rounded orders

`apply_order_impacts` replaces `row.bp_cost` with the broker's answer but keeps the original `row.bp_factor`. When scaling rounds an order to zero, `_reclaim_rounding_slack` can restore it using `price × original bp_factor`, replacing the higher prechecked cost with the old estimate.

**Reproduction:** two stocks cost $100 per share; each target is one whole share and the available buying power is $200. The initial factor of 1 estimates $100 per order. Broker precheck says each order consumes $200. Scaling by 0.5 rounds both to zero; rounding recovery then restores both at a reported cost of $100 each. The final plan reports **$200 required and no budget error**, although the same one-share orders were prechecked at **$400 combined**.

This path matters for TastyTrade, whose adapter implements order preview. Alpaca currently has no equivalent order-preview implementation in this flow.

**Change:** carry the calibrated cost through every sizing pass, including rounding recovery. Re-preview changed final quantities where necessary; do not assume the whole broker impact scales linearly if it includes fixed fees or nonlinear margin effects. The final aggregate budget must use the final orders' costs.

Evidence: [precheck cost replacement](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation.py:2637), [rounding recovery uses the old factor](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation.py:2080).

### PA-03 — Distinguish an actively submitting run from an abandoned run

The run is inserted before its first order exists. `get_unconsumed_runs` selects every run without an income-consumption timestamp, including an active submission. A second page's income refresh can therefore reconcile that run while its order list is empty or incomplete. Empty fills are considered settled. Finalization stamps the run once, and a later finalization only restates totals without correcting the consumed income.

**Reproduction:** insert a $1,000 deposit. Interleave a reconciliation pass after `run_allocation` creates its run and saves the fractional preference, before `submit_plan` starts. The reconciliation stamps the empty run as settled. The original run then buys and fills $1,000. Its final result reports **$1,000 bought, $0 income consumed, $1,000 income still open**. The run has already left the reconciliation queue, so subsequent ordinary refreshes do not repair it.

`BEGIN IMMEDIATE` prevents two store writes from consuming the same ledger simultaneously. It does not distinguish a completed submission from one that has not yet created all its orders. There is also a related race: finalization replaces `order_ids` wholesale, so a stale reconciliation snapshot can overwrite IDs appended meanwhile.

**Change:** add an explicit submission lifecycle and completion marker. Reconcile only runs whose order set is complete, or abandoned runs claimed through a recovery lease. Protect finalization against an outdated order-set version. Preserve crash recovery for a process that dies before sending any order; permanently ignoring empty runs would create a different leak.

Evidence: [run creation before submission](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:1882), [reconcile loop](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:1707), [one-shot finalization and order-list replacement](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation_store.py:1020), [query includes active runs](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation_store.py:1086).

### PA-04 — Enforce account eligibility again at submission

`_load_gate` verifies the manual flag and absence of enabled experts when entering the page. Neither `_submit_plan` nor `run_allocation` enforces those restrictions again. Another page can enable an expert or turn off the manual flag while a dry run remains open.

**Reproduction:** prepare a valid plan, then turn the fake account's manual flag off and insert an enabled expert in the isolated database. The same pure page gate refuses this state. `run_allocation` nevertheless submits the buy and never reads the flag.

**Change:** perform the eligibility check at the service boundary before any run or order is written, using current database state. Apply it to both rebalance and label-investment modes. An account-level submission guard should coordinate this check with changes that enable experts.

Evidence: [page-only eligibility check](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/ui/pages/portfolio_allocation.py:219), [service gates omit eligibility](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:1845).

### PA-05 — Exclude known unactionable sales from planning and funding

A broker holding without a local equity transaction is valued and sized normally by the engine. Its sale contributes `bp_released`, and the resulting money can fund buys. At submission, `decide_symbol_action` returns `ACTION_SKIP` because there is no transaction to resize or close. The non-option case receives no explicit explanation that the platform cannot sell it. Skipped rows also do not prevent the overall run from being classified as successful.

Refusing to sell an untracked holding is an intentional conservative behavior. The defect is **predicting the sale and spending its estimated proceeds despite already knowing that it cannot be submitted**, then describing the skipped sale as an ordinary success-compatible outcome.

**Reproduction:** hold 10 AAA shares at $100 at the broker, with no local transaction and $0 free buying power. Target AAA at zero and BBB at 100%. The plan shows a $1,000 sale funding a $1,000 buy. Submission skips AAA with the message `target 0 - close position`, sends the BBB buy, and records success when the fake broker accepts it. AAA remains at 10 shares. A real broker may instead reject BBB; either result differs from the reviewed rebalance.

**Change:** resolve actionability before the dry run. Mark untracked holdings explicitly, give them no sale-funding credit, and identify the reconciliation/import work needed before they can be traded. Preserve the refusal instead of adding an untracked sell shortcut. Report a requested but impossible reduction distinctly from a zero-delta skip.

Evidence: [sales add buying-power release](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation.py:2384), [untracked sell becomes a skip](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation.py:3789), [skip message](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:784), [success classification](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:1968).

## Documented behavior worth reconsidering

These are separate from the five defects above because the current source explicitly describes these choices.

**Budget validation is advisory.** Removing a funding sell from the selection can leave $1,000 of buys against $0 available; the validator returns a warning, but Submit still sends the buys. The probe confirms this. The code deliberately relies on the broker to reject orders as capacity runs out. I recommend blocking an already-known budget violation and requiring a new reviewed selection. Likewise, submitting sells first does not guarantee that they have filled or that their buying power has been released before the first buy is attempted. [Budget warning](C:/Users/basti/Documents/dev/BA2TradePlatform/packages/common/ba2_common/core/portfolio_allocation.py:3001), [submit loop](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/core/portfolio_allocation_service.py:757).

**Refresh keeps the wizard's original base snapshot.** Its callback returns a new plan and market gate, while the page separately keeps a new base for eventual submission. The wizard's own `self.base` remains old. A recovered missing quote can leave Submit blocked until the dialog is reopened, and cash-after displays can use the old cash figure. This limitation is acknowledged in `_base_block`'s docstring. Returning the base with the refreshed plan would make those displays and gates consistent. [Acknowledged limitation](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py:1006), [refresh assignment](C:/Users/basti/Documents/dev/BA2TradePlatform/ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py:1836).

## Verification and reproduction

The existing suites passed:

- **575 shared-engine tests:** allocation, sell-released buying power, redistribution, bumped outcomes, validation and wizard arithmetic.
- **904 live-side tests:** submission, persistence, account scoping, view calculations, dry-run selection, validation/retry and simulated base. One dependency deprecation warning (`websockets.legacy`); no failures.

The five additional defect probes and the advisory-budget probe run against the real engine, service and store with an in-memory database and a fake broker. They assert the **observed faulty behavior**, so they are audit reproductions rather than future regression tests. The fake broker intentionally accepts submitted orders; this demonstrates what the application attempts, not which over-budget orders a real broker would accept.

Artifacts: [reproduction script](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/portfolio_allocator/portfolio_allocator_audit_probe.py), [machine-readable evidence and source hashes](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/portfolio_allocator/portfolio_allocator_audit_evidence_2026-09-07.json), [test execution record](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/portfolio_allocator/portfolio_allocator_test_record_2026-09-07.txt).

The installed trade venv launcher could not find its base Python. Verification used the bundled Python 3.12 interpreter with the existing trade environment's site-packages, without installing or upgrading dependencies. Re-run the probes from the repository root:

```powershell
& 'C:/Users/basti/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' reports/portfolio_allocator/portfolio_allocator_audit_probe.py --site-packages 'C:/Users/basti/ba2-venvs/trade/Lib/site-packages'
```

**Fix order:** PA-01/PA-02 before relying on automated submission, then PA-03's run lifecycle, followed by PA-04 and PA-05. PA-05 should be resolved before rebalancing any pre-existing TastyTrade holdings that lack local transactions. These are execution and accounting fixes; optimizing strategy rules cannot correct them.

The preceding strategy work is saved separately: [six deployed strategies](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/deployed_strategy_review_2026-09-07.md), [additional strategy ideas](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/expert_strategy_ideas_2026-09-07.md), and [deployment parity findings](C:/Users/basti/Documents/dev/BA2TradePlatform/reports/prod8081_settings_parity_review_2026-09-07.md).
