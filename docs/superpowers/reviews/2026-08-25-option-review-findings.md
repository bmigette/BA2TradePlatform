# Option lifecycle & structures — review findings

**Status:** living document. Started 2026-08-25. Nothing here is fixed yet.

This tracks the findings from a set of read-only reviews of the option stack. It exists so the
findings survive past the conversation that produced them. Each finding has a stable ID; use the
ID in commit messages when one is fixed, and update the Status column here.

| Review | Scope | State |
|---|---|---|
| **R1 — covered call & wheel** | share coverage, stock acquisition, assignment/exercise/expiry, the 100× multiplier, backtest-vs-live parity | **complete** — 27 agents, 18 findings raised, 13 survived adversarial refutation, 5 refuted |
| **R2 — all other structures** | combo-vs-legged margin, strike/expiry validity invariants, per-structure collateral, long-option-as-collateral, entry gating, exit completeness | running |
| **R3 — condition optimizability** | domain bounds vs GA gene ranges, cross-symbol coherence, dead/unregistered genes, data quality, missing normalized conditions, grid & fitness | running |
| **R4 — option fitness design panel** | 4 independent fitness designs, judged on gameability / implementability / statistical soundness | running |

Method for all four: independent finders per lens, then **every** finding handed to a separate
agent instructed to *refute* it by default and to verify the cited code actually exists. Only
survivors are recorded below. Refuted claims are listed at the end so they are not resurrected.

---

## The headline answer

**Nothing buys the stock for a covered call, and coverage is verified exactly once — at the moment
the call is written — and never again.**

`SellCoveredCallAction._build_and_submit` (`packages/common/ba2_common/core/TradeActions.py:2462-2469`)
assumes the shares already exist and refuses if they do not:

```python
held = self._held_equity_shares()
quantity = int(math.floor(held / 100.0)) if held > 0 else 0
if quantity < 1:
    return self._result(False, ...)
```

There is no buy-write, no combo order, no acquire-then-overlay sequence. Three unrelated
mechanisms put the shares there:

1. **Live, ordinary path** — a plain earlier equity `BuyAction` (`TradeActions.py:489`) fired by an
   ENTER_MARKET rule. A different order, a different Transaction, possibly days earlier. Nothing
   links it to the later covered call except that both belong to the same expert and symbol. If the
   equity entry is sized below 100 shares, the covered call refuses **forever** and the strategy is
   silently plain long equity.
2. **Live, wheel path** — short-put assignment mints the shares. `AlpacaAccount._apply_option_activity:6363-6394`
   creates the equity Transaction and `_record_assignment_equity_order:6612` mints a synthetic
   FILLED equity BUY order. That order row is what makes the shares visible to `_held_equity_shares`,
   which reads **order** rows, not `Transaction.quantity`. A CSP is what creates the shares; there is
   no "buy the stock" step anywhere in the wheel.
3. **Backtest** — `_build_strategy_covered_call` (`testplatform/ba2test_launcher.py:2657`) stamps
   `lot_size = 100` on every entry via `_with_round_lot_entry:2631`. Without it the overlay silently
   no-opped and produced byte-identical results to plain equity.

Three structural problems compound it:

- **The check is not at the seam.** `_held_equity_shares()` has exactly two callers —
  `SellCoveredCallAction:2463` and `BuyProtectivePutAction:2503`. The actual broker seam,
  `OptionsAccountInterface.submit_option_order:139-195`, validates only non-empty legs, ≤4 legs, and
  a single expiry. It never looks at the share position. Any future caller of the seam writes a short
  call with no coverage test at all.
- **The check reads our own DB order ledger, not the broker.** It sums `filled_qty` over the
  *expert's* OPENED transactions and never calls `account.get_positions()`. Shares removed by anything
  outside that expert's order rows — a broker-side stop fill, another expert, a margin call, a
  portfolio rebalance — are invisible to it.
- **The call is booked as its own Transaction with no link to the shares.** `_submit_option_order`
  (`TradeActions.py:2215-2217`) calls the seam with no `transaction_id`, so a second Transaction is
  created keyed on the **underlying** symbol. Nothing joins the two and no close path is option-aware.

On the broker side: **Alpaca is the only options-capable live adapter** (`AlpacaAccount.py:196`;
IBKR and TastyTrade do not implement `OptionsAccountInterface`). The platform models no concept of
option approval level or share collateral anywhere. If Alpaca rejects an uncovering equity sale or a
naked `sell_to_open`, that is luck, not a safeguard.

---

## Findings

Severity is money-at-risk, not crash risk. `Reach` is whether it fires with shipped code and
ordinary configuration today.

| ID | Sev | Affects | Reach | Summary | Status |
|---|---|---|---|---|---|
| [OPT-L1](#opt-l1) | high | live + backtest | today | No coverage re-check after entry — any equity exit strands a naked short call, and the assignment loss never reaches the books | open |
| [OPT-L2](#opt-l2) | high | live | today | Portfolio allocation treats an option Transaction as equity of the underlying and sells the covering shares | open |
| [OPT-L3](#opt-l3) | high | live + backtest | config | Generic close path builds an EQUITY order for an OPTION transaction | open |
| [OPT-L4](#opt-l4) | high | live | today | The one coverage check over-counts on the SELL side — a canceled partial fill is skipped | open |
| [OPT-L5](#opt-l5) | high | live | today | Assignment-capacity and option buying-power gates read EQUITY and call it CASH | open |
| [OPT-L6](#opt-l6) | medium | live + backtest | config | `call_butterfly` is in `RESERVING_STRATEGIES` but persists no reserve — makes option BP unmeasurable account-wide | open |
| [OPT-L7](#opt-l7) | low | live | config | No deliverable-size check — an adjusted (non-100) contract is under-collateralised | open |
| [OPT-B1](#opt-b1) | high | backtest | today | The shipped O_CC / O_PP overlay **can never fire** — every covered-call backtest is mislabelled plain equity | open |
| [OPT-B2](#opt-b2) | high | backtest | today | Backtest covered-call assignment orphans the equity Transaction → phantom shares forever | open |
| [OPT-B3](#opt-b3) | high | backtest | today | Backtest debit-combo cash guard cannot tell an open from a close | open |
| [OPT-B4](#opt-b4) | medium | backtest | today | Backtest option limit orders never expire; live forces `TimeInForce.DAY` | open |

### OPT-L1 — No coverage re-check after entry
`packages/common/ba2_common/core/TradeActions.py:2215` (no `transaction_id`) +
`packages/common/ba2_common/core/interfaces/AccountInterface.py:1611-1619`

Two independent lenses converged on this from opposite directions.

The covered call is a separate Transaction with no link to the shares. `CloseAction`
(`TradeActions.py:592-611`) closes only the transaction behind `existing_order`, which both engines
resolve to the **oldest** transaction for the symbol (`TradeManager.py:2515-2516`;
`daily_engine.py:1262,1330`). So an equity exit sells the shares and leaves the short call open.

The exit does not even need to come from a rule. A broker-side risk-manager stop
(`TradeRiskManagement.py:926`, submitted as an OCO leg) can fill at 3am: the shares are gone, no
platform code runs, and the call is bare for the rest of its DTE. `option_lifecycle.py:79-86`
contains no `cover_lost` exit reason, and `covered_call` sits in `ZERO_RESERVE_STRATEGIES`
(`OptionsAccountInterface.py:672-676`) precisely on the assumption the shares are still there.

**The ledger damage is worse than the position damage.** When the now-uncovered call is assigned,
`AlpacaAccount._apply_option_activity:6402-6409` finds no equity long, emits only a
`logger.warning`, closes the option Transaction with `close_reason="assigned"`, and records **no
equity effect at all**. The delivery loss never lands in the platform's books.

*Unverified:* whether Alpaca permits the uncovering equity sale at all — brokers typically hold the
shares as collateral for a Level-1 covered call. Not determinable from this repo. The platform
relies on it either way, which is the actual problem.

### OPT-L2 — Portfolio allocation treats an option position as equity
`ba2_trade_platform/core/portfolio_allocation_service.py:51-55`

`_open_transaction_ids` has no `Transaction.asset_class` filter. An option Transaction's `symbol` is
deliberately the **underlying** (`OptionsAccountInterface.py:208-210, 307-310`), so it is handed to
the allocation engine as an equity transaction of that ticker and routed through the equity
close/adjust path with no guard at any layer.

A "set AAPL to 0%" run loops `state.transaction_ids` and calls `close_transaction` on both: the
equity one genuinely sells the 100 covering shares; the option one submits a **1-share equity BUY**
and reports `OUTCOME_SUBMITTED`. The short call survives both.

`option_lifecycle_service._open_option_transactions:390-408` filters on exactly this column — the
seam exists and allocation is the one caller that skipped it.

Refinement from the verifier: the naked outcome additionally depends on the broker (Alpaca encumbers
covered-call shares via `qty_available`, so the equity sell may be rejected — the platform neither
checks nor relies on this). It does **not** cascade into a restacked second call: the option
transaction is left CLOSING, not CLOSED, and `open_option_orders_book_wide` counts CLOSING, so
`has_covered_call` still holds. The `_adjust_symbol` trim variant was **refuted** — FIFO is ascending
transaction id and the equity transaction is necessarily older, so a trim is absorbed by it. The
reachable adjust variant is an **ADD**, which `split_delta_fifo` routes entirely to the oldest
transaction — the option one, whenever an option was opened on the symbol first
(`portfolio_allocation.py:3290-3291`).

### OPT-L3 — Generic close path builds an EQUITY order for an OPTION transaction
`packages/common/ba2_common/core/interfaces/AccountInterface.py:1611-1619`

Same root as OPT-L2 but reachable from any `close` rule. `submit_close_order_for_transaction` builds
the order from `transaction.symbol` alone, so an option transaction yields an equity MARKET order on
the underlying — contract count read as a share count, `asset_class` left at its `EQUITY` default
(`models.py:490`). The 1-share fill then makes `position_balanced` True and the transaction is
marked CLOSED, hiding the still-live short call from every management pass.

Not reachable from any *shipped* ruleset today: the live reference uses `CLOSE_OPTION`, and the
backtest O_CC path is shadowed (OPT-B1). One config edit from firing.

### OPT-L4 — The coverage check over-counts on the SELL side
`packages/common/ba2_common/core/TradeActions.py:2115-2118`

```python
if o.status not in get_executed_statuses(): continue
qty = o.filled_qty
if not qty: continue
```

A cancel-and-replace that races a fill leaves a SELL `CANCELED` with `filled_qty=100`.
`reconcile_canceled_partial_fill` repairs `Transaction.quantity` but writes no compensating order
row, so this loop skips it and reports 200 shares against 100 held → 2 contracts written, 1 naked.

The codebase's own transaction recalculation already compensates for exactly this case
(`ReadOnlyAccountInterface.py:1006-1008`: `if order.status in executed_statuses or filled_qty > 0`).
The coverage sizer is the one place that does not.

**Live-only.** The backtest cannot produce the state: every FILLED path sets `filled_qty` in the same
breath as the status, and every CANCELED path either zeroes the quantity or cancels an untouched
resting order. Both callers of `reconcile_canceled_partial_fill` are in `AlpacaAccount.py` (:2957,
:3096). A live-money defect with no backtest signal — which is why no grid run would surface it.

The secondary case (`filled_qty is None` on an executed order) is a genuine asymmetric fail-open with
a concrete write path (`AlpacaAccount.py:3069-3070`) but has not been observed in production.

### OPT-L5 — Option gates read EQUITY and call it CASH
`packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:1128, 1147`

```python
cash = self.get_balance()
...
ok = exposure.cost + additional_cost <= cash   # refusal text says "against N of cash"
```

`AlpacaAccount.get_balance` (`:1884-1889`) returns `TradeAccount.equity`. The real cash **is fetched
and discarded** (`cash=_f('cash')`, `AlpacaAccount.py:2015`).

A $100k-equity / $3k-cash account is admitted to a $20k CSP delivery obligation. On a margin account
this is unintended leverage rather than an unfunded position — but it defeats the assignment-capacity
rail built for exactly this purpose.

### OPT-L6 — `call_butterfly` reserves nothing but is listed as reserving
`packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:685` vs
`packages/common/ba2_common/core/TradeActions.py:3283`

`OpenCallButterflyAction` is the only one of the 17 entry actions that submits a reserving-named
strategy without `option_reserve=`. Its open parent order permanently trips the blind branch
(`:914-922`), so `available_option_buying_power()` returns `None` and `check_option_buying_power(>0)`
returns False **for all 8 credit builders, account-wide, across all experts**. Reproduced in memory.

Fails **closed** — no capital risk, and `logger.error` fires on every gate evaluation with
`(available=None)`. The defect in the message is that it instructs the operator to repair a reserve
that should never have existed. The fix is to move it to `ZERO_RESERVE_STRATEGIES`; it is a debit
structure and the design spec already classes it as one
(`docs/superpowers/specs/2026-08-24-option-model-and-lifecycle-design.md:228`).

Scope correction from the verifier: no standard GA run mixes a butterfly with a credit builder
(O_BF appears only in the all-debit OS1 group, and `optimize-batch --strategies` dispatches each
strategy as a separate job), so backtest reach is via user-authored rulesets only.

### OPT-L7 — No deliverable-size check on adjusted contracts
`ba2_trade_platform/modules/accounts/AlpacaAccount.py:5768-5782`

`get_option_chain` drops `meta.size` and `meta.root_symbol`; `OptionContract`
(`option_types.py:10-27`) has no field to hold them; every money site hardcodes 100.

Both *historical* providers already filter non-standard deliverables
(`ba2_providers/options/alpaca.py:37`, `tastytrade.py:478-486`) and the pure layer is already
multiplier-aware and unit-tested. The live chain is the single un-plugged hole.

Cleanest consequence is on the covered-call side: `quantity = floor(held/100.0)`
(`TradeActions.py:2464`) writes 1 contract against 100 held shares even when the contract obliges
150 — a 50-share naked short no gate can see.

The CSP side is smaller than first claimed: the local `strike*100` reserve is a deliberately
conservative proxy that already exceeds Reg-T on the real 150-share notional, so the account is not
over-admitted; the shortfall only bites on actual assignment.

### OPT-B1 — The shipped backtest covered-call overlay can never fire
`testplatform/ba2test_launcher.py:2666-2680`

`cc_guard`/`cc_sell` are appended **after** S2's exit list, whose last rule is `exit_stoploss`
(`:1489-1492`) — conditioned only on `has_position`, deliberately placed last by
`_first_match_order:1523`, with `continue_processing` defaulting False (`rule_models.py:379`) — and
the evaluator breaks on first match (`TradeActionEvaluator.py:199-203`).

The GA **cannot** disable the shadowing rule: `collect_param_space` emits no
`exit:exit_stoploss:enabled` and no `exit:exit_stoploss:a0:enabled` gene
(`strategy_param_space.py:157,185` both require `toggle_optimize`, which `exit_stoploss` does not
declare). The shadow is unconditional in every genome, not merely likely.

**Consequence: every O_CC / O_PP number ever produced is a mislabelled plain-equity run** — and a
handicapped one, because `_with_round_lot_entry` forces `lot_size=100`, so at $20k the risk manager
sizes 3–27 shares on a $180–320 name and most entries are rejected as unfunded. The directly
checkable signature is that **O_CC and O_PP should now be byte-identical to each other**.

This is the same symptom `_with_round_lot_entry`'s docstring (`:2638-2648`) claims to have fixed — a
second, distinct cause.

### OPT-B2 — Backtest covered-call assignment orphans the equity Transaction
`testplatform/backend/app/services/backtest/backtest_account.py:2814-2823`

```python
signed = float(shares) if share_side == OrderDirection.BUY else -float(shares)
self._cash -= signed * float(share_price)
self._update_position(position.underlying, signed, float(share_price))
```

`_update_position` (`:1128`) mutates only the in-memory `self._positions` dict. **No Transaction and
no TradingOrder is written.** `process_pending_assignment_liquidations:2942-2944` short-circuits on
`held <= 0`, so `_record_stock_liquidation_close` never runs.

The equity Transaction therefore stays OPENED with a FILLED BUY and no SELL →
`_held_equity_shares()` reports 100 phantom shares forever → the overlay writes another, naked call
each cycle; and a later equity exit sells 100 shares that do not exist, opening a real short from
`qty=0` (`_apply_fill:3542` clamps buys only). The phantom sell has two triggers, not one: the
rule-driven `CloseAction` path **and** the lean TP/SL bracket path, which sizes the close as
`held = abs(net)` over FILLED order rows (`:2014-2019, 2041`) with no ledger clamp.

Live handles the mirror correctly (`AlpacaAccount._settle_called_away:6440`, which splits an
oversized lot rather than erasing it).

**Currently masked by OPT-B1.** See the sequencing note below.

### OPT-B3 — Backtest debit-combo cash guard cannot tell an open from a close
`testplatform/backend/app/services/backtest/backtest_account.py:1917-1955`

`_fill_multi_leg_parent` never reads `position_intent` or `option_strategy`. The single-leg sibling
at `:1818-1820` explicitly does (`if intent and "open" not in intent: return True`).

A multi-leg **close** of a credit structure can be cancelled outright ("entry NOT opened") or
silently rescaled, with `_sync_transaction_quantity` overwriting `Transaction.quantity` with the
number of structures *closed*. Nothing repairs it (`ReadOnlyAccountInterface.py:1053` deliberately
skips multi-leg), and that value is the divisor for `spread_pnl_percent` take-profits
(`TradeConditions.py:1731-1733`).

Realistic triggers are the one-bar post-assignment cash trough (`daily_engine.py:730` runs
`refresh_orders` before `:740 process_pending_assignment_liquidations`, while `settle_option_expiry`
debits cash at `:2822`) and undefined-risk short strangles/straddles whose entry reserve is Reg-T
naked margin only. Defined-risk verticals and condors already reserve their full width at entry, so
their close debit is pre-secured — the iron-condor framing is the *least* likely instantiation.

The partial branch is worse than first reported: after a 2-of-3 cap the close parent is FILLED,
`has_pending_closing_order` stops blocking, and the next close attempt reads the **entry** parent's
`filled_qty=3` (`TradeActions.py:3567`) with `build_closing_legs` rounding ratio to 1 (`:3396`),
submitting 3 contracts per leg against 1 held — over-closing and flipping the position by 2.

### OPT-B4 — Backtest option limit orders never expire
`ba2_trade_platform/modules/accounts/AlpacaAccount.py:5967` (live forces `TimeInForce.DAY`) vs no TIF
or age handling anywhere in `backtest_account.py` / `daily_engine.py`.

All 17 option entries are LIMIT (`TradeActions.py:2216`). A missed entry rests for the contract's
whole life at a stale price, keeping its reserve charged (`OptionsAccountInterface.py:848-859`) and,
for pure-option families, locking the symbol out of the run via the WAITING dup gate
(`daily_engine.py:1019`).

This lets the GA quote aggressively and never pay for the misses — it changes *which trades exist*,
which is the worst distortion class for a fitness function.

---

## Sequencing constraint — OPT-B1 masks OPT-B2

**OPT-B1 and OPT-B2 must land in the same change.** Fixing the rule ordering alone makes the
simulator *actively wrong* rather than merely inert: it immediately starts writing covered calls,
assigning them, orphaning the equity Transaction, re-writing naked calls against phantom shares, and
opening fabricated short stock positions.

---

## Verified correct — checked, not assumed

Recording these matters as much as the defects; it bounds what still needs attention.

- **The cash-secured-put arm is well built.** `option_reserve_required`
  (`OptionsAccountInterface.py:689`) **raises** on an unknown strategy and on a missing sizing input
  rather than returning 0.0. `naked_margin_per_contract` raises on a missing strike.
  `available_option_buying_power` returns `None` (not 0.0) on an unreadable balance and every gate
  treats None as refusal. A missing `option_reserve` is recorded UNMEASURABLE, not free. The
  unknown-read-as-zero pattern is deliberately and repeatedly defused here.
  *(This supersedes an earlier concern about "six `return 0.0` guards" inside
  `option_reserve_required` — that shape was not found on the current code.)*
- **Entry sequencing is fail-closed.** `_held_equity_shares` counts only `{FILLED, PARTIALLY_FILLED}`
  and sums `filled_qty`, not `quantity`. An unfilled stock buy contributes zero; a 60-of-100 partial
  gives `floor(0.6) = 0` → refusal, not a naked 40. A missing expert instance returns a hard `0.0`.
  Option orders are explicitly skipped (`:2113-2114`) so a call cannot count itself as its own cover.
- **No off-by-100 anywhere.** ~a dozen sites checked across sizing, reserves, margin, MTM, expiry
  settlement, P&L and commissions — the multiplier is applied exactly once everywhere. The two
  historically broken sites (`results._deployed_capital`, `monte_carlo.apply_spread_cost`) are fixed
  and now read the multiplier off the trade row.
- **`get_positions()` tri-state is honoured in all three adapters** (`AlpacaAccount.py:1800`,
  `IBKRAccount.py:270`, `TastyTradeAccount.py:499`) — `None` on failure, never `[]`.
- **The backtest margin engine is better than the live entry gate.** `_covered_short_call_contracts`
  (`backtest_account.py:836-863`) allocates real simulated shares greedily, largest lot first, so the
  same 100 shares can never exempt two lots, and it is recomputed every bar — a call that loses its
  cover is correctly re-charged Reg-T margin.
- **Live assignment bookkeeping is solid.** Detection is activity-driven (OPASN/OPEXC/OPEXP/OPCSH,
  7-day lookback, deduped via OptionActivity rows) so **early American assignment is caught**. Cost
  basis is the strike. An OPASN with `qty is None` is **refused** rather than coerced to 0
  (`AlpacaAccount.py:6355-6362`). `_settle_called_away` splits an oversized lot rather than erasing it.
- **`sell_to_open` vs `sell_to_close` is correct everywhere.** Multi-leg is a genuine Alpaca MLEG
  combo with the 4-leg ceiling enforced at the seam. Rejected orders do not leak phantom reserves
  (ERROR is terminal). The equity reconciler has an explicit options guard
  (`ReadOnlyAccountInterface.py:1433-1438`).
- **`submit_close_order_for_transaction` sizes off measured, not ordered, quantity**
  (`AccountInterface.py:1585-1608`) and refuses to fall back on a net of zero. Its *instrument* is
  still wrong for options — that is OPT-L3.
- **Backtest fill plausibility is guarded**: 10%-of-volume participation cap, no-arb premium guard,
  $0.10 minimum tradeable premium, and `_publishes_spread` fails **closed** on a `bid == ask` cache.
  The single-leg-pays-zero-spread defect is genuinely fixed in the engine.

---

## Recommended order of work

### Tier 0 — before any option strategy is trusted with live money

1. **Enforce coverage at the seam, not the call site.** `OptionsAccountInterface.submit_option_order`
   should refuse a `covered_call` (and any `sell_to_open` call leg claiming cover) whose underlying
   share count it cannot verify. Closes OPT-L1's entry half, closes the `PremiumSeller`-shaped
   bypass, and survives future callers. Same commit: **link the option Transaction to the equity
   Transaction it covers** (or store the covering `transaction_id` in the parent order's `data`) so a
   close can find its counterpart.
2. **Make the close seam asset-class aware** (OPT-L3, and OPT-L2's mechanism).
   `submit_close_order_for_transaction` (`AccountInterface.py:1611`) must refuse — or route to
   `close_option_position` — when `transaction.asset_class == AssetClass.OPTION`. Independently, add
   the `asset_class` filter to `portfolio_allocation_service._open_transaction_ids:51`, plus a
   refusal to sell shares collateralising an open short call.
3. **Add a post-entry coverage monitor.** Step 1 does not fix OPT-L1's *exit* half: the shares can
   leave via a broker-side stop with no platform code running. Natural home is
   `option_lifecycle_service.run_option_lifecycle_pass` — it already runs before OPEN_POSITIONS
   (`JobManager.py:1466`) and already builds structures. It needs a `cover_lost` decision reason
   alongside `profit_capture` / `credit_stop`. This is effectively the missing half of plan Task 8,
   which wired only premium-based exits.
4. **Fix the coverage arithmetic** (OPT-L4): count CANCELED-with-`filled_qty>0`, mirroring
   `ReadOnlyAccountInterface.py:1006-1008`; treat `filled_qty is None` on an executed order as
   unmeasurable (refuse) rather than `continue`, mirroring `models.py:266-275`.

### Tier 1 — correctness of the gates, before scaling size

5. **OPT-L5**: give the option gates a real cash accessor rather than `get_balance()`, or rename the
   rail and its refusal text honestly. Today the code promises cash-securing and implements
   equity-securing.
6. **OPT-L6**: move `call_butterfly` into `ZERO_RESERVE_STRATEGIES`, and add a lockstep test of *list
   membership vs what the builders actually persist* — the existing test
   (`packages/common/tests/test_new_option_actions.py:461`) only checks list-vs-branch.
7. **OPT-L7**: port `is_standard_occ` (`fetch_options.py:74`) into `AlpacaAccount.get_option_chain`,
   or carry `size`/`root_symbol` onto `OptionContract` and let the multiplier-aware pure layer work.
   Three lines; both historical providers already have them.

### Tier 2 — restore backtest trust (blocks strategy promotion, not live safety)

8. **OPT-B1 + OPT-B2 together** (see the sequencing constraint). Reorder the overlay ahead of the
   always-matching floor stop (or give `exit_stoploss` `continue_processing=True`), **and** make
   `settle_option_expiry` write the closing equity order for called-away shares — the backtest mirror
   of `_settle_called_away:6440`. This is plan **Task 10** (plan `:657`), still unimplemented.
9. **OPT-B3**: read `position_intent` in `_fill_multi_leg_parent` and exempt closes, exactly as
   `_cap_single_leg_option_entry:1818-1820` already does.
10. **OPT-B4**: terminalise unfilled option limits at end of bar (live semantics), or age them out.
11. **Plan Task 11 — the live/backtest option parity test.** `parity_harness.py` is equity entry-side
    only. Every one of OPT-B1–B4 and the live/backtest split in OPT-L4 survived review because
    nothing runs one option decision through both simulators and diffs it. **This is the control that
    would have caught most of Tier 2.**

### Deferred

- **Plan Task 7's portfolio rails are dead in production.** `option_book.check_rails` / `admit` are
  called from nothing but tests and docstrings — the plan never scheduled a wiring step.
  `LifecyclePassResult.breaker` is likewise discarded by its only caller (`JobManager.py:1466-1467`),
  so the sleeve circuit breaker flattens the book on trip but suppresses no subsequent entry. Not a
  regression, but neither should be described as "we have portfolio rails".
- **Plan Task 12 (delete `PremiumSeller`)** is low-risk cleanup — it bypasses every entry gate but
  cannot reach a live broker (`run_analysis` raises).

---

## Latent, flagged but not counted as findings

- **`PremiumSeller._txn_metrics` width** (`packages/experts/ba2_experts/PremiumSeller/portfolio.py:98`,
  `min(short strikes) - max(long strikes)`) books a bear call spread as naked at ~131× its true risk.
  Latent only: PremiumSeller builds no such structure and is backtest-only, and the promoted
  replacement (`option_lifecycle.structure_metrics:224`) fixes it explicitly. Dies with Task 12.
- **`AlpacaAccount.get_positions()` does not filter option rows**, unlike `TastyTradeAccount.py:511-514`,
  so the comment at `ReadOnlyAccountInterface.py:1433` ("get_positions() reports EQUITY positions
  only") is false for Alpaca. All consumers key on symbol and an option's symbol is its OCC string,
  so no concrete harm was substantiated. A latent premise violation.

---

## Refuted — do not resurrect without new evidence

Five claims were raised by a finder and killed by an independent verifier:

1. Strike-vs-cost-basis selection is broken.
2. "The wheel is unbacktestable" — mechanically true, but the severity was wrong.
3. The option spread model is 0.0 on API-launched backtests.
4. "No broker permission check anywhere" — true, but materially overstated.
5. Naked short straddles appear in the default GA grid.

---

## Gaps in R1 — named, not papered over

- **Broker behaviour is not determinable from this repo.** Whether Alpaca encumbers covered-call
  shares against an equity sale, rejects a `sell_to_open` when the shares are pledged to an open RM
  stop, or accepts an opening order in an adjusted contract — all unverified. All three would blunt
  (not remove) OPT-L1, OPT-L2 and OPT-L7. **The platform relies on none of them, which is the actual
  problem.**
- **Nothing was executed.** No tests were run and no broker was touched. One read-only Python import
  of `ba2test_launcher` printed rule ordering (no DB, no network).
- **No backtest test covers the covered-call assignment case.**
  `testplatform/backend/tests/backtest/test_option_orphan_stock_and_arb_guards.py:383` tests only the
  naked short-call case.

---

---

# R2 — all other structures

27 agents, 18 findings raised, **17 survived** adversarial refutation, 1 refuted.

## The lead hypothesis was WRONG — there is no legging-in

I predicted the headline finding would be that the platform legs into spreads, so the broker
margins the short leg as naked. **It does not.** Recording this because a refuted hypothesis is
worth as much as a confirmed one, and because it removes a whole class of worry.

Every structure — every `TradeActions` builder, PremiumSeller, the lifecycle service and both close
paths — calls **one seam** with the full leg list:
`OptionsAccountInterface.submit_option_order(legs, quantity, ...)` (`:139`). It persists one parent
`TradingOrder` plus one child per leg (`:261-281`) and then makes exactly **one** broker call
(`:284`). Legs are capped at 4 (`:164`) and must share one expiry (`:186-193`).

Alpaca builds a native `OrderClass.MLEG` request (`AlpacaAccount.py:5986-6005`) submitted once at
`:6020`. Positive limit = net debit, negative = net credit (`:5955-5956`). Broker leg ids are
written back onto the children at `:6049-6078`. Grepping for a per-leg submission loop finds none.
**Spreads are margined by Alpaca as defined-risk spreads; the short leg of a vertical is never
submitted naked.**

**There is also only one options adapter, so there is no adapter divergence.**
`IBKRAccount` (`:26`) does not inherit `OptionsAccountInterface`. `TastyTradeAccount` (`:67`) does
not either, and says so explicitly in its docstring (`:85`): *"Out of scope (explicitly unsupported
below, never silently half-working) … OptionsAccountInterface."* Both refuse gracefully at
`_OptionEntryAction._supports_options` (`TradeActions.py:1912-1913`).

## Structures implemented — 17 entry builders plus a close

All in `packages/common/ba2_common/core/TradeActions.py`:

| Structure | Class | Line |
|---|---|---|
| long_call | `BuyCallAction` | 2265 |
| long_put | `BuyPutAction` | 2359 |
| bull_call_spread (debit vertical) | `OpenBullCallSpreadAction` | 2303 |
| bear_put_spread (debit vertical) | `OpenBearPutSpreadAction` | 2397 |
| covered_call | `SellCoveredCallAction` | 2454 |
| protective_put | `BuyProtectivePutAction` | 2494 |
| cash_secured_put | `SellCashSecuredPutAction` | 2534 |
| bear_call_spread (credit vertical) | `OpenBearCallSpreadAction` | 2596 |
| bull_put_spread (credit vertical) | `OpenBullPutSpreadAction` | 2681 |
| straddle (long) | `OpenStraddleAction` | 2795 |
| strangle (long) | `OpenStrangleAction` | 2857 |
| short_straddle | `OpenShortStraddleAction` | 2916 |
| short_strangle | `OpenShortStrangleAction` | 2986 |
| iron_condor | `OpenIronCondorAction` | 3060 |
| jade_lizard | `OpenJadeLizardAction` | 3141 |
| call_butterfly (1-2-1) | `OpenCallButterflyAction` | 3219 |
| put_ratio_spread (1×2) | `OpenPutRatioSpreadAction` | 3289 |
| close (any structure) | `build_closing_legs` / `CloseOptionAction._close_multi_leg` | 3362 / 3540 |

PremiumSeller's `put_credit_spread` and `short_put` are **defined but unreachable**
(`packages/experts/ba2_experts/PremiumSeller/`). Plan Task 12 deletes them.

## R2 findings

| ID | Sev | Affects | Reach | Summary | Status |
|---|---|---|---|---|---|
| [OPT-S1](#opt-s1) | high | live | today | A rejected combo strands its leg children PENDING **forever**, creating permanent phantom short-put exposure that blocks 7 structures | open |
| [OPT-S2](#opt-s2) | high | live | today | `strike_method` is silently ignored by 8 of 17 builders — the UI **defaults it to `delta`** so "0.30" becomes "0.30 % OTM" | open |
| [OPT-S3](#opt-s3) | high | live | today | Early assignment on ONE leg closes the WHOLE structure's Transaction, orphaning the surviving legs at the broker | open |
| [OPT-S4](#opt-s4) | high | live | today | **No live reconciliation of the option book against the broker at all** — `get_option_positions()` has zero production callers | open |
| [OPT-S5](#opt-s5) | high | live | today | Live single-leg option close drops the transaction link — the position never closes and the exit re-submits forever | open |
| [OPT-S6](#opt-s6) | high | live + backtest | today | `get_balance()` means EQUITY live and CASH in backtest — the same call sizes every structure and answers "can we take delivery?" | open |
| [OPT-S7](#opt-s7) | high | backtest | today | The backtest never enforces a combo's **net limit price** — all 12 multi-leg structures | open |
| [OPT-S8](#opt-s8) | high | backtest | today | Margin-liquidating one naked leg marks the whole transaction CLOSED, orphaning the survivor while still charging its margin | open |
| [OPT-S9](#opt-s9) | medium | live | today | Nearest-strike selection has no minimum-distance or OTM-side constraint — **7.34 % of short strangles collapse into straddles**, measured | open |
| [OPT-S10](#opt-s10) | medium | live | today | No debit structure consults buying power — 9 of 18 structures ungated, sizing never reduced by deployed capital | open |
| [OPT-S11](#opt-s11) | medium | live | today | `bear_put_spread` is the one short-put-bearing structure that skips the assignment-capacity gate (7 of 8 call sites enforce it) | open |
| [OPT-S12](#opt-s12) | medium | live | today | A **third** condition registry, `get_numeric_event_values()`, is missing 4 numeric events — the live editor saves them with no operator or value | open |
| [OPT-S13](#opt-s13) | medium | backtest | today | Butterfly defined-risk bound is `min(gaps)`, below a broken-wing fly's true payoff — and the clamp **moves real simulated cash** | open |
| OPT-L6 (dup) | high | live | today | `call_butterfly` reserve poisoning — independently re-found by two R2 lenses, see R1 above | open |
| OPT-L3 (dup) | high | live | today | The close seam has no option branch — R2 widened it to **every** structure, N contracts → N shares | open |

### OPT-S1 — A rejected combo strands its leg children forever
`packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:283-291`

When `_submit_option_order_impl` raises — an Alpaca `APIError` on rejection (approval tier,
insufficient BP on an ungated debit combo) or any transient network error, since `alpaca_api_retry`
re-raises non-429 APIErrors (`AlpacaAccount.py:182-184`) — the except block sets **only the parent**
to ERROR. The N leg children persisted at `:259-282` keep `status=PENDING` and
`broker_order_id=None`, and the Transaction stays WAITING.

**Nothing self-heals.** `refresh_orders`' sweep requires a broker id (`AlpacaAccount.py:3022`).
`sync_transaction_orders` cannot fire because it classifies leg children as non-terminal
`market_entry_orders` — `ReadOnlyAccountInterface.py:954` keys on `depends_on_order` but children
carry `parent_order_id` (`models.py:437` vs `:472`), so `all_orders_terminal`,
`all_entry_orders_terminal` and `never_opened` are all False. `_fail_unsent_entry` is equity-only.
`clean_pending_orders` is a manual UI button.

A stranded **short put** child then permanently poisons `_refuse_if_cannot_take_delivery`, blocking
bear put spread, bull put spread, CSP, short straddle, short strangle, iron condor, jade lizard and
put ratio spread. Short calls are deliberately excluded (`:1035`), so the poisoning set is narrower
than a naive reading suggests — but it is permanent and requires manual intervention.

### OPT-S2 — `strike_method` is silently ignored by 8 of 17 builders
`packages/common/ba2_common/core/TradeActions.py:3079-3084` (and 14 sibling sites)

```python
otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
sc = select_single(call_chain, method="percent_otm", strike_param=otm, spot=spot, ...)
```

`method=self.strike_method` appears at exactly **9** sites (`:2280, 2319, 2374, 2413, 2476, 2516,
2555, 2629, 2721`). Hard-coded `method="percent_otm"` appears at exactly **15** sites (`:2816, 2826,
2880, 2887, 2934, 2942, 3007, 3011, 3020, 3081, 3084, 3162, 3165, 3239, 3309`), covering
`straddle`, `strangle`, `short_straddle`, `short_strangle`, `iron_condor`, `jade_lizard`,
`call_butterfly`, `put_ratio_spread`. For those 8 classes `self.strike_method` is set on the shared
base (`:1894`) and then **never read** — a dead attribute.

**What makes this a live trap rather than a curiosity:** the rule editor renders the Strike Method
select for *every* non-close option action, **defaults it to `delta`** (`ui/pages/settings.py:5127-5132`),
placeholders Strike Param as `0.30 or {"long":0.45,"short":0.25}` (`:5136`), and persists
`strike_method` unconditionally (`:5296`). No validator anywhere rejects a `strike_method` the
chosen structure cannot honour. A user configuring an iron condor sees "delta", types `0.30`
expecting a 30-delta short, and gets a strike **0.30 % out of the money** — effectively at the money.

Mitigating: leaving Strike Param blank means `settings.py:5295 if sp and sp.value:` never fires and
each action falls back to its `DEFAULT_OTM_PCT` — the safe path. It is the UI's `delta` default plus
the `0.30` placeholder that makes the unsafe path the natural one.

### OPT-S3 — Early assignment on one leg closes the whole structure
`ba2_trade_platform/modules/accounts/AlpacaAccount.py:6392` (put branch), `:6401` (call branch)

An OPASN/OPEXC/OPEXP on **one** leg of a multi-leg structure closes the **entire** structure's
Transaction. No code path in the live platform can then see, manage or close the surviving legs
again — including the protective long.

Affects every structure whose short put can be assigned (bull put spread, iron condor, jade lizard,
put ratio spread, short straddle, short strangle, bear put spread) and, via the call branch, iron
condor / bear call spread / jade lizard / short straddle / short strangle.

Rated high rather than critical because the loss is bounded by the structure's defined risk (Alpaca
is spread-level). It is nonetheless a silent, unrecoverable ledger-vs-broker divergence with real
money at stake and **zero detection anywhere**.

### OPT-S4 — No live reconciliation of the option book against the broker
`packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:1322`, guard at `:1437`

`reconcile_externally_closed_transactions` **skips every transaction carrying an option order** and
delegates to `TradeManager._reconcile_account_option_activities` (`:325`), which reads only
OPASN/OPEXC/OPEXP/OPCSH over a 7-day window and is the sole caller.

`AlpacaAccount.get_option_positions()` (`:5895`) has **no live production caller** — only backtest
`daily_engine.py:1414`, an ad-hoc script, and tests.

Four candidate seams were checked and none catch it: `refresh_orders` only updates orders already in
the local DB; `refresh_transactions` derives state purely from local orders; `refresh_positions`
only logs a count; and `option_lifecycle_service` builds its book from local OPENED transactions
while its only broker call (`_broker_can_answer:349`) is a liveness check on **equity** positions.

So a broker-side close — manual intervention, a broker risk action, anything outside our activity
feed — leaks both the ledger position **and** its buying-power reserve permanently, because FILLED
is not in `get_terminal_statuses()` (`types.py:111-119`).

### OPT-S5 — Live single-leg option close drops the transaction link
`ba2_trade_platform/modules/accounts/AlpacaAccount.py:6092-6093`

```python
self.submit_option_order([leg], int(position.quantity), order_type, limit_price,
                         option_strategy="close")   # transaction_id= omitted
```

Called positionally, omitting the seam's 7th parameter. The seam then **mints a fresh Transaction**
(`OptionsAccountInterface.py:245-248` → `AccountInterface._create_transaction_for_order:585-627`,
which unconditionally constructs a new Transaction with no lookup of an existing open one).

Every sibling close path passes the id: `CloseOptionAction._close_multi_leg`
(`TradeActions.py:3627-3629`), `option_lifecycle_service._close` (`:624-626`), and the backtest's own
`close_option_position` (`backtest_account.py:2757-2764`, with the rationale spelled out at
`:2734-2738`). **The live single-leg path is the sole omission.**

Consequence: the original position never reaches CLOSED, so the exit condition stays true and
re-submits forever; and the buying-power reserve plus short-put assignment exposure are never
released, since `open_option_orders_book_wide` filters on `not_statuses=(CLOSED, FAILED)`
(`:854-859`).

Affects long_call, long_put, cash_secured_put, covered_call, protective_put, and any spread leg
reaching the single-leg branch.

### OPT-S6 — `get_balance()` means EQUITY live and CASH in backtest
`packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:1128`

The same call is the single denominator for **three** different jobs: option sizing
(`_virtual_equity`, `TradeActions.py:2012`), the reserve gate (`available_option_buying_power`,
`:957`), and the assignment-capacity gate (`assignment_capacity`, `:1128`).

It returns total **equity** in live (`AlpacaAccount.py:1886-1887 float(account.equity)`) and
spendable **cash** in the backtest (`backtest_account.py:1170-1182 return self._cash`, with
`equity()` deliberately not used). Neither adapter overrides the three option methods.

The assignment-capacity gate is designed, documented ("of cash", `:1145`) and unit-tested as a
cash-on-hand gate. In live it is fed stock-inclusive equity, so a "cash-secured" put can be admitted
with a fraction of the cash and is in fact **margin-secured** — silent undisclosed leverage on the
platform's most explicitly unlevered structure. It is also a live/backtest parity gap in both
delivery admission and entry sizing, since backtest cash falls with each debit while live equity
does not.

**The correct figure is already fetched and discarded:** `AccountSnapshot.cash` /
`non_marginable_buying_power` at `AlpacaAccount.py:2015` and `:2019`.

*(This is the same root as OPT-L5, found independently by a different lens with the parity dimension
added.)*

### OPT-S7 — The backtest never enforces a combo's net limit price
`testplatform/backend/app/services/backtest/backtest_account.py:1597-1602`, `:1873-1971`

`submit_option_order` puts the net limit on the **parent** (`OptionsAccountInterface.py:207`) and
builds children with **no** `limit_price` (`:261-281`). So `_option_fill_price`'s two limit branches
cannot fire for a child and it falls through to the market-style `_option_slip`. The code comments
this itself at `:1597-1602`.

`_fill_multi_leg_parent` then prices every leg off its own bar, applies only the debit cash cap, and
writes `parent.open_price = net` (`:1966`) with **no comparison to `parent.limit_price`** — a grep
confirms that field is never read in the multi-leg path.

Single-leg option orders **do** enforce their limit (fixed in `f2034456`, pinned by
`tests/backtest/test_option_limit_spread_cost.py:150-163`), so the gap is specific to the 12
multi-leg structures the GA searches.

Direction, corrected by the verifier: the backtest-only fills are the ones **worse** than the limit,
so mean credit is *understated*; what is inflated is the **trade count**. That is the more damaging
distortion for a fitness function — it changes which trades exist.

Related, same area: each leg is charged the modelled half-spread independently (`_option_slip`,
`:1663`), so an N-leg combo pays N half-spreads rather than one net-combo cross — the opposite bias,
overstating condor costs.

### OPT-S8 — Margin-liquidating one naked leg closes the whole transaction
`packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:1187`

A one-leg margin-call liquidation records its buy-back through `_record_option_expiry_close`, which
stamps `depends_on_order=(entry.id ...)` (`backtest_account.py:3137`). That makes it a *dependent
FILLED* order, so the next `refresh_transactions()` takes the
`elif filled_closing_orders and transaction.status == OPENED:` branch and closes the **entire**
transaction with `close_reason="tp_sl_filled"` — pre-empting the per-contract `position_balanced`
logic at `:1037-1053`, which is only consulted in the last elif at `:1211`.

The surviving leg then disappears from `get_option_positions()` and `_option_transaction_for_contract`,
so it can never be managed or settled — **while its `_OptionLot` stays in the ledger and keeps being
charged in `maintenance_margin_requirement()`.**

Reproduced by execution on a short strangle: after liquidating the call and one
`refresh_transactions()`, the transaction is CLOSED, `held after refresh: []`, the put lot is still
in the ledger at qty −1.0, and `_apply_option_expiry` leaves it there with maintenance still 2000.

Also fires for defined-risk combos whenever one leg's expiry settlement fails and a sibling's
succeeds (`daily_engine.py:1453-1470` logs and continues per leg).

### OPT-S9 — Strike selection has no minimum-distance or OTM-side constraint
`packages/common/ba2_common/core/option_selector.py:279, 283`

`_pick_by` is an unconstrained argmin: no maximum distance from the %OTM/delta target, and no
requirement that the pick be on the OTM side of spot. `OpenShortStrangleAction` (`:2998-3033`) never
checks `call_c.strike != put_c.strike`.

**Measured by replaying the real selectors over the real option cache at the GA's own settings**
(strike_param 6–20 %, min_volume 25):

- **7.34 % of fully submittable short-strangle selections (166 / 2263) return both short legs on the
  SAME strike** — a short straddle, persisted, reserved and labelled `short_strangle`. Example:
  ARQT 2024-06-14, spot 8.63, ladder [2.5, 5, 7.5, 10, 12.5, 15] → callK = putK = 10.0 for *every*
  strike_param from 6 % to 20 %, with the put already ITM.
- **11.6 % of short calls and 20.7 % of short puts are selected ITM.**

The iron condor half of the original claim was **refuted**: 0 collapses in 5,083 admitted condors,
because the existing wing guard (`TradeActions.py:3098`) refuses whenever the ladder is coarse
enough — `wing_width_pct` (3–8 %) is never larger than `strike_param` (8–20 %).

The impact analysis was also corrected: a collapsed strangle sizes off
`naked_margin_per_contract(put strike)`, not (width − credit), so it sizes *smaller*, not larger.

### OPT-S10 — No debit structure consults buying power
`packages/common/ba2_common/core/TradeActions.py:2061-2075`

`_size` returns `int(math.floor(budget / (premium * 100.0)))` where budget comes from
`_virtual_equity()` (`:2010-2026`, `balance * pct/100`). The only ceiling,
`_max_equity_per_instrument_cap` (`:2028-2059`), is `equity * pct/100` **with no subtraction of the
instrument's current allocation**. `check_option_buying_power` appears only at the 8 credit/naked
sites.

So nine of eighteen structures — long_call, long_put, bull_call_spread, bear_put_spread, straddle,
strangle, call_butterfly, protective_put, covered_call — size off virtual equity, are never gated,
and contribute $0 to the reserve pool.

No seam compensates: `submit_option_order` validates leg count and expiry only, and
`option_book.check_rails`/`admit` have **zero production importers**.

The contrast with the equity path is decisive — `MarketExpertInterface.get_available_balance:794`
computes `virtual_balance - used_balance` and then clamps to real broker buying power (`:805-809`),
a documented 2026-07-21 fix for exactly this overstatement.

### OPT-S11 — `bear_put_spread` skips the assignment-capacity gate
`packages/common/ba2_common/core/TradeActions.py:2435-2438`

It sells a put and reaches `_submit_option_order(...)` with **no capital gate of any kind**: no
`_refuse_if_cannot_take_delivery` (the 7 enforcing call sites are `:2581, 2777, 2969, 3043, 3120,
3200, 3343`) and no `check_option_buying_power`, because `bear_put_spread` is in
`ZERO_RESERVE_STRATEGIES` (`OptionsAccountInterface.py:672-675`).

Strike ordering confirmed at `option_selector.py:319` — `return (hi, lo)  # put debit spread: buy
higher strike, sell lower` — so the short leg genuinely is the lower-strike put.

The house pattern exactly: the rule lives at call sites, not at the seam.

**Not novel** — the repo documents it as a deliberate deferral at
`packages/common/tests/test_option_assignment_capacity_wiring.py:719-724` ("KNOWN AND REPORTED,
deliberately out of this commit's scope"). Recording it here so it is tracked rather than forgotten.

### OPT-S12 — A third condition registry is missing four numeric events
`packages/common/ba2_common/core/types.py:534-555`

`get_numeric_event_values()` enumerates 17 of the 21 `N_*` members, omitting `N_NEW_TARGET_PERCENT`
(`:395`) and the three `N_PRICE_VS_TARGET_*` (`:403-405`).

All four have working `CompareCondition` classes and correct `rule_builders.FIELD_EVENT` entries, so
**the GA/backtest path is unaffected**. The **live** rule editor classifies triggers with
`is_numeric_event` (`types.py:597`), so for these four fields `settings.py:4973` never renders the
operator/value widgets and `:5227-5236` persists `{'event_type': ...}` with no operator and no value.

Two consequences: a rule authored in Settings on any of these four fields is unusable from birth;
and **opening a GA-imported option rule that uses them and clicking Save silently strips its
threshold.** That matters because the four `price_vs_target_*` gates are the tunable entry gates on
*every* GA option entry rule (`ba2test_launcher._option_entry_rule:2576-2591`).

Blast radius correction: under the default `BA2_ERROR_MODE="enforce"` the `ValueError` from
`create_condition:3070` is **not** absorbed — it re-raises through `TradeActionEvaluator.py:827, 778,
223`, so `evaluate()` throws and `TradeManager.py:1890` catches it per-recommendation with
`continue`. The whole ruleset evaluation for that symbol is lost, not just the one condition.

**This is the third registry a condition must appear in.** It is the same shape as the earlier
`FIELD_EVENT` incident that killed 52 % of a genome.

### OPT-S13 — Butterfly defined-risk bound is `min(gaps)`
`testplatform/backend/app/services/backtest/backtest_account.py:589-590`

`_defined_risk_width_per_structure` returns `min(gaps)` for `call_butterfly`, but a long 1-2-1 fly's
maximum expiry payoff is the **lower** gap `k2 − k1`, not the smaller of the two. Whenever the upper
wing is narrower, the bound is strictly below the attainable payoff.

**It moves real simulated cash**, not just the display: `settle_defined_risk_combo_expiry` applies
`net_payoff = max(-bound, min(net_payoff, bound))` (`:3030`) immediately before `self._cash +=
net_payoff` (`:3033`). The same width caps the mid-life mark at `:539`.

Broken wings are produced **deterministically**: the lower-wing picker tie-breaks to the *farther*
strike (`TradeActions.py:3260`) while `select_wing` tie-breaks to the *nearer* one
(`option_selector.py:268-269`). On a $5 grid with a $100 body, the GA's `wing_width=7.5%` yields
90/100/105 — bound $500 against a true max payoff of $1,000, a **50 % truncation**; 12.5 % yields
85/100/110, a 33 % truncation. Both widths are in the searched grid (`ba2test_launcher.py:2157-2159`).

Every butterfly test in the suite uses equal wings, and the one "broken wing" assertion
(`test_options_review_fixes.py:130`, `[170,180,205]`) is the mirror orientation where `min(gaps)` is
coincidentally correct — so nothing catches it.

The same `_defined_risk_width_per_structure` backs the mid-life MTM clamp for all four verticals and
the iron condor.

---

## Pending — R3, R4

- **R3 (condition optimizability):** per-condition table of unit / domain / symbol-comparability /
  registration / GA range vs real range. Includes the **RVOL** question — both *relative volume* and
  *IV ÷ realised volatility* are wanted as genes. Note OPT-S12 above already surfaces a registry gap
  from a different angle; R3 should be cross-checked against it.
- **R4 (fitness):** recommended option-specific fitness. The premise under test is that a
  consistency-rewarding fitness is precisely what a short-premium strategy games, and that options
  uniquely permit an **ex-ante** risk denominator because a defined-risk structure's max loss is
  arithmetic rather than sampled.
