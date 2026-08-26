# Option live safety — seam enforcement (Track A)

**Status:** approved 2026-08-25. Live trading only. Backtest untouched.

**Source:** `docs/superpowers/reviews/2026-08-25-option-review-findings.md`. This spec covers
**Track A** of four. Tracks B (backtest fidelity), C (search quality) and D (fitness) get their own
specs. Finding IDs below refer to that document.

**Branch from `origin/dev`.** `pf_allocation` is stale.

## The problem

Thirteen live findings, and most of them are one failure repeated: **a rule enforced at a call site
rather than at the seam**, so a second caller walks past it. That is the defect pattern this
codebase has hit repeatedly — the silently-dropped `FIELD_EVENT` conditions and the
assignment-capacity gate were both this shape.

The response is therefore not thirteen point fixes. It is **three seams**, after which the class
cannot recur, followed by eight genuinely independent defects.

## What is NOT in scope, and why

**`OPT-L1`'s naked-call severity is reduced.** The original finding said an equity exit strands a
naked short call. On review the user observed that a broker holds the shares as collateral for a
covered call, which is almost certainly true at Alpaca for a Level-1 account — and R1 itself listed
this as its own largest unverified gap.

That changes the failure rather than removing it, and both remaining failures are in scope:

1. The uncovering equity sell is **rejected**, but the platform neither checks `qty_available` nor
   distinguishes a rejection from a transient error — so a portfolio-allocation run reports
   `OUTCOME_SUBMITTED` and silently does nothing.
2. On a margin account with higher option approval the short call can simply be naked-margined and
   the shares stay sellable, so the original failure is still reachable, just not universally.

We do not build against an assumption about broker behaviour we cannot verify from this repo. The
guard is cheap and correct either way.

Also out of scope: everything in Tracks B/C/D, and the legacy `PremiumSeller` expert (plan Task 12
deletes it; it cannot reach a live broker because `run_analysis` raises).

---

## Seam 1 — asset-class blindness in the close and adjust paths

### The defect

An option `Transaction`'s `symbol` is deliberately the **underlying** ticker
(`OptionsAccountInterface.py:208-210`, `:307-310`). Three functions build a closing order from
`transaction.symbol` alone, with `asset_class` left at its `EQUITY` default (`models.py:490`):

| function | file:line |
|---|---|
| `submit_close_order_for_transaction` | `packages/common/ba2_common/core/interfaces/AccountInterface.py:1611-1619` |
| `close_transaction` | `packages/common/ba2_common/core/interfaces/AccountInterface.py:1678` |
| `TransactionHelper.adjust_quantity_with_tpsl` | `packages/common/ba2_common/core/TransactionHelper.py:739`, order built `:790-802` |

So an option transaction routed through any of them submits an **equity** market order on the
underlying, sized off `get_current_open_qty()` (`models.py:239-282`) — which returns a **contract**
count. **N contracts become N shares.**

It then corrupts the ledger. `OrderStatus.FILLED` is not terminal (`types.py:111-119`), so the
transaction lands in `CLOSING`, not `CLOSED`. `CLOSING` is excluded from
`option_lifecycle_service._open_option_transactions` (`:405-407`, which selects `OPENED` only), so
the still-live option becomes invisible to every management pass while remaining open at the broker.

A fourth site feeds these: `portfolio_allocation_service._open_transaction_ids`
(`ba2_trade_platform/core/portfolio_allocation_service.py:51-55`) has no `asset_class` filter, so
option transactions enter the equity allocation plan as if they were holdings of the underlying.

Closes `OPT-L2`, `OPT-L3`.

### The decision: refuse, do not route

Each of the three functions **raises** when `transaction.asset_class == AssetClass.OPTION`, with a
message naming `close_option` as the correct path.

Routing to `close_option_position` was considered and rejected. It would make the generic equity
close path quietly become an option close path — hiding the asset class one layer further up, which
is the defect being fixed. A ruleset whose exit is a generic `close` acting on an option should
fail visibly and be told to use `close_option`, not be silently rescued.

`option_lifecycle_service._open_option_transactions:390-408` already filters on exactly this column,
so the seam being added is one the codebase already knows how to express; allocation is simply the
caller that skipped it.

### Allocation, additionally

`_open_transaction_ids` gains the `asset_class` filter so option transactions never enter the plan.
Independently, the allocation close path must refuse to sell shares that Seam 2 reports as pledged.

---

## Seam 2 — the collateral invariant

### Why there is no new column

The obvious model — a `collateral_transaction_id` FK on the option transaction — is wrong.
`_held_equity_shares()` (`TradeActions.py:2095-2124`) sums across **multiple** equity transactions,
so the relationship is many-to-many **by quantity**, not one-to-one by id. A single FK would have to
pick one arbitrarily and would go stale the moment a lot is split or partially closed.

The question the close path actually needs to ask is a quantity question. It is derivable from data
that already exists, needs no migration, and cannot drift out of sync.

### The function

```
shares_pledged_to_short_calls(account, symbol) -> int | None
```

Sums `contracts x multiplier` over open short-call option transactions on that underlying for that
account, using `open_option_orders_book_wide` (`OptionsAccountInterface.py:834-852`), which already
counts `CLOSING` as well as `OPENED`.

**Tri-state, and this is the load-bearing part.** It returns `None` — never `0` — when the pledge
cannot be determined. An unmeasurable pledge must refuse the sell, not permit it. `0` means
*measured: nothing is pledged*. Conflating the two is the failure this whole track exists to remove.

The multiplier is read per contract, not hardcoded to 100, so `OPT-L7` does not silently reintroduce
itself here.

### Enforced in three places

**Entry — at the broker seam.** `OptionsAccountInterface.submit_option_order` (`:139-195`) currently
validates only non-empty legs, a 4-leg ceiling and a single expiry. It gains a refusal for a
`covered_call` (and any `sell_to_open` call leg claiming cover) whose underlying share count it
cannot verify.

This is deliberately at the seam and not in `SellCoveredCallAction`. The action's own check
(`TradeActions.py:2462-2469`) is correct and stays; the point is that it is the *only* caller
performing it, so any future caller of the seam writes an uncovered short call with no test at all.

**Exit — the close and adjust guard.** The Seam 1 sites, plus the allocation close path, refuse to
take an equity position below its pledged quantity. Refusal names the symbol, the held quantity and
the pledged quantity, so the operator can act on it.

**Continuous — a cover-lost monitor.** Entry and exit checks cannot cover the case the finding
actually describes: shares leaving via a broker-side risk-manager stop
(`TradeRiskManagement.py:926`, submitted as an OCO leg) filling overnight with no platform code
running.

`option_lifecycle_service.run_option_lifecycle_pass` is the natural home. It already runs before
OPEN_POSITIONS (`JobManager.py:1466`) and already builds the option book. It gains a **`cover_lost`**
decision reason alongside the existing `profit_capture` and `credit_stop`
(`option_lifecycle.py:79-86`). This is the missing half of plan Task 8, which wired only
premium-based exits.

Closes `OPT-L1` (both halves), and the coverage half of `OPT-L2`.

### One consequence to accept deliberately

`covered_call` sits in `ZERO_RESERVE_STRATEGIES` (`OptionsAccountInterface.py:672-676`) precisely
because the shares are the collateral. That stays true and correct. The monitor is what makes the
assumption safe rather than merely asserted.

---

## Seam 3 — per-leg close decisions

### The defect

`ReadOnlyAccountInterface.py:1187`:

```python
elif filled_closing_orders and transaction.status == TransactionStatus.OPENED:
```

One leg's filled closing order — from an assignment, an expiry settlement, or a margin liquidation
— closes the **entire** multi-leg transaction with `close_reason="tp_sl_filled"`. It pre-empts the
per-contract `position_balanced` logic that already exists at `:1037-1053`, which is only consulted
in the last `elif` at `:1211`.

The surviving legs, **including the protective long**, then disappear from `get_option_positions()`
and `_option_transaction_for_contract`, so nothing can see, manage or close them again — while in
the backtest their `_OptionLot` stays in the ledger and keeps being charged maintenance margin.

### The fix

For a multi-leg option transaction, the branch must consult the per-contract balance rather than
closing wholesale. A transaction closes when **every** contract is balanced, not when the first one
is.

This is one branch serving both engines: it closes `OPT-S3` live and `OPT-S8` in the backtest, whose
trigger is `_record_option_expiry_close` stamping `depends_on_order`
(`testplatform/.../backtest_account.py:3137`) and thereby making the liquidation look like a
dependent fill. Track B does not need to revisit it.

**No new linkage is required.** Legs are already linked: `parent_order_id`
(`OptionsAccountInterface.py:277`) locally, and Alpaca MLEG leg ids written back onto each child
(`AlpacaAccount.py:6049-6078`).

While here: `parent_order_id`'s description reads *"ID of parent OCO order if this is a leg"*
(`models.py:472`) and now also carries option structures. Update the description. Do not rename the
field.

---

## The independents

Each ships alone, in any order, after the seams.

| ID | Fix | Where |
|---|---|---|
| `OPT-S5` | Pass `transaction_id=` — currently omitted, so the seam mints a **new** transaction, the original never closes, the exit re-submits forever and the reserve is never released. Every sibling close path passes it. | `AlpacaAccount.py:6092-6093` (7th param of `OptionsAccountInterface.py:139-147`) |
| `OPT-S1` | The `except` block sets only the **parent** to ERROR; N leg children stay `PENDING` with `broker_order_id=None` forever, and a stranded short-put child permanently blocks seven structures. Terminalise the children too. | `OptionsAccountInterface.py:283-291`, children at `:259-282` |
| `OPT-L5` / `OPT-S6` | The assignment-capacity and buying-power gates read `get_balance()`, which is **equity** live (`AlpacaAccount.py:1884-1889`) while the refusal text says "cash". Use a real cash accessor — the value is already fetched and discarded at `AlpacaAccount.py:2015`. | `OptionsAccountInterface.py:1128`, `:1147` |
| `OPT-L6` | `call_butterfly` is a debit structure listed in `RESERVING_STRATEGIES` whose builder persists no reserve, making account-wide option buying power unmeasurable and refusing all 8 credit structures. Move it to `ZERO_RESERVE_STRATEGIES`, plus a lockstep test of **list membership against what builders actually persist** (the existing test only checks list-vs-branch). | `OptionsAccountInterface.py:685` vs `TradeActions.py:3283` |
| `OPT-S2` | 8 of 17 builders hard-code `method="percent_otm"` and ignore `strike_method`, while the rule editor renders that select for every option action and **defaults it to `delta`** with a `0.30` placeholder. Ask for a 30-delta short, get one 0.30% OTM. **Refuse at config time** when a structure cannot honour the chosen method: the rule editor must not offer, and must not persist, a `strike_method` the selected action ignores. Refusal is the fix, not a fallback — silently reinterpreting the number is precisely the defect. Honouring delta in those 8 builders is a *separate* enhancement, deferred to Track C, because whether delta is even computable depends on chain data this spec does not touch. | 15 sites incl. `TradeActions.py:3081`; UI at `ui/pages/settings.py:5127-5132` |
| `OPT-L4` | The coverage sizer skips a `CANCELED` order carrying `filled_qty > 0`, over-counting shares that genuinely left. Mirror the compensation the codebase already applies at `ReadOnlyAccountInterface.py:1006-1008`. Treat `filled_qty is None` on an executed order as unmeasurable and refuse, per `models.py:266-275`. | `TradeActions.py:2115-2118` |
| `OPT-S4` | No reconciliation of the option book against the broker exists: the equity reconciler skips option transactions and `get_option_positions()` has **zero production callers**. A broker-side close leaks the ledger position and its reserve permanently. | `ReadOnlyAccountInterface.py:1322`, guard `:1437`; `AlpacaAccount.py:5895` |
| `OPT-L7` | The live chain drops `meta.size` and `meta.root_symbol`, so an adjusted (non-100-deliverable) contract is under-collateralised. Port `is_standard_occ` (`fetch_options.py:74`) into the chain, or carry the fields onto `OptionContract` and let the already-multiplier-aware pure layer work. Both historical providers already filter these. | `AlpacaAccount.py:5768-5782`; `option_types.py:10-27` |

---

## Testing

**Every guard needs a test that the caller OBEYS it**, not merely that the function returns an
error. A gate can append a refusal that nothing acts on — a mutation run earlier in this project
found exactly that shape, where three risk-gate errors were appended and the caller ignored all
three. For each seam: assert the order is **not submitted**, not just that an error was produced.

**Tri-state tests in both directions** on `shares_pledged_to_short_calls` and on the coverage sizer:
unmeasurable must not read as zero, and a measured zero must not read as unmeasurable. Both
directions have produced real defects in this codebase within the last day.

**Mutation-test the guards**, with the established harness discipline: `PYTHONDONTWRITEBYTECODE=1`,
`__pycache__` purged either side of every run, restores verified with `git hash-object`, a no-op
control mutation in every batch that **must survive**, and any run whose collected count differs
from baseline treated as INVALID rather than killed.

Mutations that must die: the `asset_class` guard removed; the guard inverted; the pledge read as `0`
when unmeasurable; the pledge ignored by the close path; `cover_lost` never fired; the multi-leg
close closing wholesale again; the entry coverage refusal removed from the seam while the
`SellCoveredCallAction` check remains (proving the seam is what is tested, not the call site).

**No live broker calls.** Every test uses doubles. Nothing in this spec may place, modify or cancel
a real order.

**Do not run the full root suite while other agents hold files.** Baseline at time of writing:
`tests/` 4081, `packages/common` 1752, `packages/providers` 424, `packages/experts` 812.

---

## Sequencing

1. **Seam 1** — self-contained, no dependencies, immediately removes the wrong-instrument order.
2. **Seam 2** — depends on nothing, but the exit guard is most useful once Seam 1 stops options
   entering the equity close path at all.
3. **Seam 3** — independent of both.
4. **Independents** — any order. `OPT-S5` and `OPT-S1` are the two runaways (forever-resubmit and
   permanent block respectively) and should come first.

Seams 1–3 are the shippable unit that closes the class. The independents can follow in a second
pass without blocking anything.
