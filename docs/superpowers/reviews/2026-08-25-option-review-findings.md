# Option lifecycle & structures — review findings

**Status:** living document. Reviews run 2026-08-25. **Track A (live safety) fixed 2026-08-26** —
see the fix status below. Everything else remains open.

This tracks the findings from a set of read-only reviews of the option stack. It exists so the
findings survive past the conversation that produced them. Each finding has a stable ID; use the
ID in commit messages when one is fixed, and update the fix status below.

| Review | Scope | Result |
|---|---|---|
| **R1 — covered call & wheel** | share coverage, stock acquisition, assignment/exercise/expiry, the 100× multiplier, backtest-vs-live parity | 18 findings raised, **13 survived** refutation, 5 refuted |
| **R2 — all other structures** | combo-vs-legged margin, strike/expiry validity invariants, per-structure collateral, long-option-as-collateral, entry gating, exit completeness | 18 raised, **17 survived**, 1 refuted |
| **R3 — condition optimizability** | domain bounds vs GA gene ranges, cross-symbol coherence, dead/unregistered genes, data quality, missing normalized conditions, grid & fitness | 33 conditions inventoried, 18 raised, **15 survived**, 3 refuted |
| **R4 — option fitness design panel** | 4 independent fitness designs, judged on gameability / implementability / statistical soundness | judging failed (dossier truncated); **13 substrate findings** produced instead |

Method for all four: independent finders per lens, then **every** finding handed to a separate
agent instructed to *refute* it by default and to verify the cited code actually exists. Only
survivors are recorded below. Refuted claims are listed at the end so they are not resurrected.

---

## Fix status — updated 2026-08-26

**Track A (live safety, seams 1–3) is complete.** 24 commits, every task spec-reviewed *and*
quality-reviewed, 67 mutations in the final hardening pass with the no-op control surviving in all
seven batches.

### Closed

| ID | Closed by | Note |
|---|---|---|
| `OPT-L1` | `42998b31` `02ca35af` `6cc1079d` `828e65a9` `0e4ee7a7` `66365227` `893fac29` | entry, exit and continuous monitor. The exit half took four commits: the first covered one lot, the second netted closes already in flight, the third stopped the trim seam excluding its own prior staged sales |
| `OPT-L2` | `e6edb266` `2b484131` | the filter, plus making the silence it introduced loud |
| `OPT-L3` | `df718f27` `17d0a243` `ebcde94c` `9da007e1` | all three equity paths refuse an option |
| `OPT-S3` | **`a2c6ff28`** | **not** `de4f0f0f`, which claimed it — see below |
| `OPT-S8` | `de4f0f0f` | the shared close-decision branch |

### Still open — the eight independents from the Seam 1 spec

`OPT-S5` · `OPT-S1` · `OPT-L5`/`OPT-S6` · `OPT-L6` · `OPT-S2` · `OPT-L4` · `OPT-S4` · `OPT-L7`.
Always scoped as a separate plan; untouched by Track A.

### Still open — Tracks B, C and D

Every `OPT-B*`, the remaining `OPT-S*`, all `OPT-C*` and all `F*`.

### A correction worth keeping

`de4f0f0f` was committed as "(OPT-S3, OPT-S8)". Review established only OPT-S8 was fixed: the live
door is `AlpacaAccount._apply_option_activity` → `_close_txn`, which sets `Transaction.status =
CLOSED` directly and never passes through `refresh_transactions`, where that guard lives. The
guarded arm was in fact **unreachable** in the live engine. `5b876e8b` corrected the claim;
`a2c6ff28` then actually fixed it.

This is the defect class no test can catch — the code did exactly what it said. The claim about
*which* bug it fixed was false.

### Found during Track A, absent from the original reviews

**Fixed:**

- A partially filled sell-to-open pledged only its **filled** part: 3 contracts with 1 filled
  reported 100 shares of cover instead of 300. The same defect in `short_put_assignment_exposure`
  was worse — **the first fill made the book read $45,000 *less* exposed** (`aeea3b33`).
- Allocation could **never converge** on a symbol carrying an option transaction, because
  `split_delta_fifo` consumed the option's *contract count* as if it were shares (`e6edb266`).
- Two lots of one ticker each passed a cover check the other invalidated — deterministically on the
  deferred branch, where nothing reaches the broker to decrement the holding (`828e65a9`).
- The entry guard could validate a 30-share position and create a 300-share one, and a test asserted
  that admission was correct (`3092cb8b`).
- Nothing recorded a settled option leg at all — no order row, no metadata — so the per-contract
  balance OPT-S3's fix needs had to be created first (`a2c6ff28`).
- `_submit_row`'s action dispatch *ended* in `return _open_symbol(...)`, so an unrecognised action
  placed a MARKET BUY (`2b484131`).

**Open, recorded, not fixed:**

- **`qty` vs `qty_available`.** Shares committed to a resting SELL_STOP still count as full cover, at
  **both** the entry and exit seams. Counting resting legs was tried and reverted: `close_transaction`
  cancels legs at the broker without terminalising the DB rows, so every later exit then refused on
  phantom orders. `qty_available` is the field that would answer it, and `IBKRAccount.get_positions`
  never populates it — the adapters come first. Pinned by two named tests.
- **A trim cancels the previous trim's staged sale.** `is_tpsl_order` treats any order carrying
  `depends_on_order` as a protective leg, so the transaction is written down for shares that then
  never sell. Live position drift, independent of the cover guard.
- **`cancel_order` has two incompatible signatures.** IBKR takes a `TradingOrder`; the interface,
  Alpaca and TastyTrade take an id. On IBKR a partial close can never work, and the increase path
  grows a position while leaving its old TP/SL legs live.
- **A broker position with no tracked transactions at all** still reports "nothing to do" on a sell —
  the same silence as `OPT-L2`'s, from a different cause.

### One behaviour change to know about

A covered-call sleeve and a credit-spread sleeve **on the same ticker will not both write**.
`shares_pledged_to_short_calls` counts the spread's short leg as consuming 100 shares, so it refuses
a genuinely covered call beside it. Fail-safe and deliberate — a short 160C really can call away 100
shares between 160 and 170 — and pinned by `test_an_open_credit_spread_consumes_cover_on_the_same_ticker`.

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

---

# R3 — are option conditions optimizable by the GA?

27 agents, 33 conditions inventoried, 18 findings raised, **15 survived**, 3 refuted.

## The answer, and why it is the opposite of what was expected

The worry going in was that conditions on raw option values — bid, ask, premium in dollars —
would be unbounded and symbol-scaled, and therefore un-optimizable. **Those conditions are not in
any grid, correctly.** `profit_loss_amount` exists and is deliberately never given a range.

The real problem is the inverse:

> **`iv_rank` — the one genuinely bounded, symbol-comparable option quantity the platform owns —
> has NO GA range anywhere in the rules-engine grid.**

No `"field": "iv_rank"` leaf exists in any built strategy. The only literal hits repo-wide are the
enum, `FIELD_EVENT`, the abbreviation map, the audit module and tests. It is fully implemented,
correctly registered, and has a proper fail-closed unknown path
(`TradeConditions.py:2382`, `_iv_rank_from_series` at `OptionsAccountInterface.py:436`) — and the
GA never touches it.

**Every option gene currently searched is a position-MANAGEMENT knob:** %OTM, DTE-window centre,
wing width, %-of-premium TP/SL, days held, days-to-expiry. There is essentially no *entry
selection* dimension in the search.

And the single most heavily searched option gene is the symbol-scaled one:

> **`percent_otm` is the only strike-selection gene in all 16 option grids, and it is
> volatility-blind.** The normalized alternative — delta — is implemented, backtest-supported, and
> the *live default*, yet never searched.

Compounding this, the GA gene is literally **named `option_delta`** while carrying percent-OTM
values in every range (`ba2test_launcher.py:2103-2255`). Two quantities, one name — the house bug
pattern, in the gene space itself.

`days_to_earnings` is likewise searchable nowhere, despite the documented straddle/strangle recipe
`iv_rank <= 30 and days_to_earnings <= 5` (`rules_documentation.py:510, :521`) depending on both.

## R3 findings

| ID | Sev | Summary | Status |
|---|---|---|---|
| [OPT-C1](#opt-c1) | high | **No premium-richness criterion exists** — credit structures accepted on `net_credit > 0` alone | open |
| [OPT-C2](#opt-c2) | high | The option cache's expiry horizon is per-symbol and frozen by `resume=True` — the universe decays symbol-by-symbol across the window | open |
| [OPT-C3](#opt-c3) | medium | `percent_otm` is the only strike gene and is volatility-blind; delta is implemented but never searched | open |
| [OPT-C4](#opt-c4) | medium | The buy/sell split is **structural only** — both halves search an identical, volatility-free condition set | open |
| [OPT-C5](#opt-c5) | medium | ~32–38 % of the price-gate gene space is logically unsatisfiable, and the waste is centred on the authored default | open |
| [OPT-C6](#opt-c6) | medium | The chain relabels a contract's LAST-TRADED bar as the as-of bar — stale quote/IV/delta with no age bound | open |
| [OPT-C7](#opt-c7) | medium | Option TP/SL genes are unevaluable on non-printing bars while time exits always evaluate — the exit search is biased toward time exits | open |
| [OPT-C8](#opt-c8) | medium | `get_atm_iv` falls back to a frozen start-date row whose IV can be **lookahead** — corrupting the one comparable statistic | open |
| [OPT-C9](#opt-c9) | medium | Every vertical is exactly one strike wide, so the TP gene's live domain is set by a *different* gene | open |
| [OPT-C10](#opt-c10) | low | `iv_rank` has no range, and the auto-range generator puts a fifth of its levels above the field's own ceiling | open |
| [OPT-C11](#opt-c11) | low | `option_wing_width` collapses to 2–4 distinct structures out of 6 values, differently per symbol | open |
| [OPT-C12](#opt-c12) | low | `price_vs_target_high_percent` sits at −31 % median — 77 % of the universe is outside the searched ±20 % window | open |
| [OPT-C13](#opt-c13) | low | In GROUP jobs a dead member scores **neutral**, not catastrophic | open |
| OPT-C14 | low | PremiumSeller `target_dte` declares 21..45 but decodes to {24,30,36,42,48} — floor unreachable, ceiling exceeded by 3 | dies with Task 12 |
| OPT-C15 | high | PremiumSeller `spread_width` demands an exact listed strike, so each value silently deletes a different subset of the universe | dies with Task 12 |

### OPT-C1 — No premium-richness entry criterion exists anywhere
`packages/common/ba2_common/core/TradeActions.py:2642`

Credit structures are admitted on **`net_credit > 0` alone**. There is no minimum credit, no
credit-as-a-fraction-of-width, no return-on-collateral floor, no annualised-yield gate.

Selling far-OTM options for near-zero credit expires worthless roughly 97 % of the time. So on any
win-rate- or Sharpe-flavoured fitness the GA is **actively rewarded** for doing exactly that. This
is the pennies-in-front-of-a-steamroller mechanism, and it is not merely unpenalised — the
profitability criterion is absent from the search space entirely.

**This is the single most important cross-link to R4.** No fitness change can fully compensate for
an entry criterion that does not exist.

### OPT-C2 — The option universe decays symbol-by-symbol across the window
`testplatform/backend/app/services/backtest/fetch_options.py:454`

The cache's expiry horizon is per-symbol and frozen by `resume=True`. Every option genome is
therefore scored on a universe that shrinks on a fixed calendar schedule having nothing to do with
the genome.

Consequence: **fitness is dominated by *when* a genome trades.** A gene combination that happens to
fire early scores against a full universe; one that fires later scores against a decayed one. This
is the symbol-mix failure mode with a time axis, and it directly undermines the equity cap's
premise that a stable setting should score the same regardless of start date.

### OPT-C3 — The most-searched gene is the symbol-scaled one
`testplatform/ba2test_launcher.py:2105`

`percent_otm` maps to a wildly different assignment probability and premium per symbol — 5 % OTM on
a 15-vol utility and on a 90-vol biotech are not comparable propositions. The $0.10 premium floor
then silently removes the low-vol / low-price half of the universe, so the gene doubles as a
universe filter.

Delta is implemented, backtest-supported and the live default — and is never searched. Note the
R1/cache finding that the legacy chain cache has **no IV and no greeks at all**, which may be *why*
`percent_otm` was hard-coded; if so, wiring delta requires the parquet store to exist first.

### OPT-C4 — The buy/sell split is structural only
`testplatform/ba2test_launcher.py:2591`

Debit and credit halves search an **identical, volatility-free condition set**. A premium seller
wants IV rank *high*; a premium buyer wants it *low*. Neither can express its thesis, because the
quantity that would express it is not a gene. The split the user asked for exists in the structures
but not in the search.

### OPT-C5 — A third of the entry-gate space is logically dead
`testplatform/ba2test_launcher.py:2577-2586`

The four price-vs-target gates are 8 genes per structure that collapse to a single interval on spot
price. **31.6–38 % of the sampled sub-space is an empty conjunction** that guarantees zero trades,
all scoring the identical `-1e9` sentinel, so selection gets no gradient from them.

Worse, the waste is concentrated **exactly at the authored default (all-zero)** — which is where
warm-start and every hand-seeded individual begins.

### OPT-C6 — Stale bars relabelled as the as-of bar
`testplatform/backend/app/services/backtest/options_provider.py:282`

The chain relabels a contract's **last-traded** bar as the as-of bar, with no age bound. Volume,
quote, IV and delta can all be arbitrarily stale.

This partially defeats the `min_volume` tradability floor that the launcher deliberately made
non-searchable (`ba2test_launcher.py:2341-2352`, default 25) — the GA's trade population ends up
shaped by data *age* rather than by liquidity.

### OPT-C7 — The exit search is structurally biased toward time exits
`testplatform/backend/app/services/backtest/options_provider.py:287`

Option TP/SL genes are **unevaluable on any bar the contract did not print**; the time-based exit
genes always evaluate. Two of the four exit genes are silently data-gated and two are not.

So the GA chooses between exit *mechanisms* on the basis of which one can be evaluated rather than
which one is profitable. Credit structures are hit hardest — they carry the most legs and the
cheapest, thinnest wings.

### OPT-C8 — Lookahead in the one comparable statistic
`testplatform/backend/app/services/backtest/options_provider.py:371`

`get_atm_iv` falls back to the cache build's **start-date** chain row, whose IV can itself have been
inverted from a price dated *after* the evaluation bar. The `iv_rank` series therefore mixes
point-in-time IV with frozen — and sometimes lookahead — constants.

`PremiumSeller.iv_rank_min` (`ba2test_launcher.py:1264`, 20..60 step 10) is the **only** place the
GA searches an IV-rank threshold anywhere, so if the underlying statistic is corrupted on a
meaningful fraction of bars, that one search is unreliable too.

### OPT-C9 — The TP gene's domain is set by a different gene
`testplatform/ba2test_launcher.py:2507`

Every vertical is built exactly **one strike wide**, so the `profit_loss_percent` TP/SL basis is
fixed by the strike ladder and by `option_strike_param`. The debit TP band's top half (125–200 %) is
unreachable at the ATM end of the grid — roughly **19 % of the joint (strike_param, TP) grid is a
dead TP** for O_VERT/O_BULLCS, and crossover routinely recombines the two into that dead region.

### OPT-C10 — `iv_rank`'s only possible range is wrong
`packages/common/ba2_common/core/rules_convert.py:70, :83`

The sole path that would ever give `iv_rank` a range is the generic **±50 %-of-current-value**
fallback used when a *live* ruleset is imported (`ruleset_meta.py:164, :279`). That generator puts
about a fifth of its levels **above 100** — outside the field's own ceiling.

### OPT-C11 — `option_wing_width` collapses to a handful of phenotypes
`packages/common/ba2_common/core/option_selector.py:340`

Six values collapse to 2–4 distinct structures, and *which* values collapse differs per symbol.
`_mutate_individual` (`genetic.py:233-266`) uses σ = (8−3)/6 = 0.83, so a typical mutation moves one
bucket — which on AAPL/MSFT/GOOGL is a no-op four times out of five. The GA burns evaluations
re-testing an identical phenotype and reads the flat fitness as a plateau.

### OPT-C12 — Two of the four price gates are inert on 77 % of the universe
`testplatform/ba2test_launcher.py:2581`

All four analyst-target gates share one ±20 % window, but `price_vs_target_high_percent` has a
**−31 % median**. On ~77 % of symbols the gene cannot change any evaluation; on the other 23 % it
acts as a symbol filter rather than a signal.

### OPT-C13 — A dead member scores neutral in GROUP jobs
`testplatform/ba2test_launcher.py:2577`

In GROUP jobs a member that cannot trade scores **neutral**, not catastrophic. Contradictory or
unsatisfiable genomes therefore survive selection as "not bad" rather than being driven out — the
dangerous variant of the zero-trade case.

## Other inventory facts worth keeping

- **`option_sizing` is bounded, symbol-comparable, and NOT a gene.** `_collect_action_genes`
  (`strategy_param_space.py:147-177`) emits genes only for `action_value`, the per-action toggle,
  `option_delta`, `option_dte` and `option_wing_width`. There is no `option_sizing_optimize`
  anywhere, so position size is a per-structure constant (5–20 %) the GA cannot touch.
- **`days_to_expiry` is emitted as a FLOAT gene** — `strategy_param_space.py:134-137` hardcodes
  `is_int=False` for every `cond:*:value`, so an integer day-count is sampled with
  `random.uniform` and made integral only by step rounding.
- **`min_volume` is deliberately not a gene** and the code says why
  (`ba2test_launcher.py:2341-2352`): it is a tradability floor, not a strategy parameter. Correct.
- **`has_covered_call` / `has_protective_put` guards carry no `toggle_optimize`**, so they are
  fixed, unsearchable parts of every O_CC / O_PP genome.
- **`percent_below_recent_high`'s 20-bar window is a hardcoded class constant** — even where the
  threshold were a gene, the window could never be tuned.
- **O_SSTD and O_STRD have no strike gene at all** (always ATM).
- The `price_vs_target_*` registration gap is the same defect as **OPT-S12** — found independently
  from a different angle, which is corroboration.

## What to add — the user asked for RVOL

The user asked for **relative volume** and **IV ÷ realised volatility** as genes. Both are the right
shape (bounded, symbol-comparable). Sequencing note: they are additions on top of a **missing
foundation** — `iv_rank` is already implemented and simply unwired, so wiring the gene that exists
comes first and costs almost nothing.

Design constraints for an RVOL gene, from the house rules:
- **Underlying RVOL, not contract RVOL.** Most individual contracts trade zero on most days, so a
  contract-level ratio is undefined far more often than informative. Volume ÷ open interest is the
  better contract-level unusual-activity signal.
- **The trailing window must exclude the current bar**, or the average absorbs the spike it is meant
  to detect — and it must not reach forward (the lookahead shape already found in
  `percent_below_recent_high` and again in OPT-C8).
- **Insufficient history is unknown, not 1.0.** Defaulting to "normal" makes the gene silently
  free-passing on every newly-listed symbol, and 1.0 looks plausible enough that nobody would notice.

Also missing and worth considering, all bounded and symbol-comparable: premium as a percent of
strike; credit as a percent of spread width (the classic "collect a third of the width" rule);
annualised return on collateral; bid-ask spread as a percent of mid; distance to strike in standard
deviations (the properly normalized "how far OTM", which is what OPT-C3 actually needs).

---

---

# R4 — option-specific fitness

**Ran partially.** The grounding and all four designs completed; the three judges received only
Design 1 because the dossier was truncated when the four designs were concatenated into their
prompt (a defect in the orchestration script, not in the agents). All three judges reported the
truncation and refused to invent scores for designs they never saw — the right call.

**The comparison is therefore missing, but the judges spent their effort auditing the substrate
instead, and that turned out to be worth more than the comparison.** Several findings below
invalidate entire design families outright, which makes re-running the original comparison
pointless — the design problem itself has changed.

## F1 — CAR has NO term that decreases when a genome takes more risk

This is arithmetic, not opinion, and it is the headline.

Under the equity cap every period return is `period_pnl / cap` (`equity_cap.py:96-104`) and
drawdown is `(pnl - peak_pnl) / cap` (`equity_cap.py:110-135`). So **doubling contract count
doubles `base` and doubles `|max_drawdown|` exactly.**

`consistent_annual_return` is `base × dd_guard × consistency × trade_gate`
(`strategy_fitness.py:753`), where `dd_guard = min(20 / max(dd, 1), 2.0)` (`:724-725`), and
`consistency` and `trade_gate` are size-invariant ratios. Therefore:

| realised drawdown at size *s* | CAR(2s) / CAR(s) |
|---|---|
| dd ≥ 10 % | **1.000 — exactly indifferent to size** |
| dd = 7 % | 1.43 |
| dd ≤ 5 % | **2.00 — doubling size doubles the score** |

**CAR strictly rewards leverage up to a 10 %-of-cap drawdown, and is exactly flat above it.**

## F2 — A missing drawdown earns the MAXIMUM risk reward

`strategy_fitness.py:724`:

```python
dd = abs(float(results.get("max_drawdown") or 0.0))
dd_guard = min(_CAR_DD_REFERENCE / max(dd, _CAR_DD_FLOOR), _CAR_DD_GUARD_MAX)
```

`or 0.0` means an **unmeasurable** drawdown becomes `dd = 0` → `dd_guard = 2.0`, the largest
multiplier the function can produce. The house unknown-as-zero bug, sitting in the fitness
function itself, handing its best score to a genome whose risk could not be measured.

## F3 — `avg_trades_per_year` counts LEGS, not structures

`get_round_trip_trades` (`backtest_account.py:2211-2232`) keys round trips on
`(transaction_id, contract_symbol)` — **one row per leg**. An iron condor is 4 trades; a strangle
is 2.

The `trade_gate` hard floor is 12 trades/yr (below it, `LOW_TRADE_SENTINEL`, disqualified) with a
linear ramp to full credit at 30/yr (`strategy_fitness.py:679-694`). So:

- **3 iron condors a year clears the hard floor.**
- **7.5 condors a year earns full credit.**

Every count-based quantity inherits this: `total_trades`, `win_rate`, `expectancy`, `sqn`,
`profit_factor` and the Monte-Carlo bootstrap all treat a 4-leg condor as four independent
observations. The inflation is 2–4× and it is **concentrated on exactly the negatively-skewed
multi-leg credit structures whose means are least estimable**, while the single-leg long-premium
arm faces a 4× stricter cadence gate.

**Fixing the denominator to count `transaction_id`s is a one-line change that would improve every
candidate design more than the metric each of them proposes.**

## F4 — There is no out-of-sample split anywhere

`grep -rn "walk_forward|out_of_sample|holdout|in_sample"` over `backend/app/services` and
`ba2test_launcher.py` returns only an unrelated ML forecasting hit.

The GA selects the maximum of roughly population × generations ≈ 10³ noisy in-sample scores on a
single window. The expected optimistic bias of that maximum is about `σ·√(2 ln N)` ≈ **3.7 σ**, and
**no choice of fitness reduces it — only a holdout does.**

Worse, the relationship runs backwards: a more expressive fitness gives the GA more distinct ways
to fit the window's idiosyncrasies, so a *better-designed* fitness can **increase** the selection
bias it was built to reduce. One walk-forward fold buys more out-of-sample validity than any metric
change on this list.

## F5 — Collateral is a run constant by construction, so ROC designs degenerate

`_size_by_reserve` (`packages/common/ba2_common/core/TradeActions.py:2077-2093`) is
`floor(equity × sizing_pct/100 / reserve_per_contract)`, and for defined-risk credit structures
`reserve_per_contract` **is** the max loss (`:2649`, `:2755`; CSP at `:2568`).

So `contracts × max_loss ≡ option_sizing % of equity`, **by construction** — and `option_sizing` is
a hard-coded per-structure constant (15.0 for O_BEARCS/O_BULLPS, 20.0 for O_IC/O_SSTG/O_SSTD) that
is **not a gene** (see OPT-C-inventory: there is no `option_sizing_optimize` anywhere).

Consequences:
- Any fitness denominated in collateral-at-risk, max-loss, buying-power reduction or
  return-on-collateral divides by a near-constant within each structure family and **degenerates to
  plain return.** The entire capital-efficiency design family is non-viable as things stand.
- That constant also sets the **buy-arm-vs-sell-arm exchange rate**: halve `option_sizing` and every
  seller's score doubles relative to every buyer's.

Either `option_sizing` becomes a searched gene, or any design must state explicitly that its risk
denominator is a run constant.

## F6 — There is no per-position or per-bar collateral series, anywhere

`snapshot_equity` (`backtest_account.py:1087-1126`) records exactly
`{date, net_liquidating_value, cash_balance, equity_value}`, and `equity_value` is
`_open_positions_mtm()` — a **mark**, not a reserve.

`maintenance_margin_requirement()` (`:706-773`) can compute a number at any instant but is never
recorded, and is not collateral-at-risk anyway: it explicitly skips defined-risk combo legs and
covered calls, so **an iron condor reads as consuming zero collateral.**

The live stack's real per-structure reserve (`order.data['option_reserve']`) **does not exist in the
test platform at all** — `grep -rn option_reserve testplatform/` finds one launcher comment and
design docs, zero code.

So a return-on-collateral fitness is not a scoring-layer change: it needs a new field recorded per
bar by the engine and threaded through `build_results` and four plumbing sites. **Weeks, not days.**

## F7 — Trade rows and the equity curve describe DIFFERENT books after an assignment

Assigned shares are created by `_update_position` with **no order**
(`backtest_account.py:2820-2823`); the cleanup order gets `transaction_id=None`
(`_record_stock_liquidation_close`, `:1052-1064`); and `get_round_trip_trades` drops every order
with no transaction (`:2222-2223`).

**So the assignment's stock P&L is in the curve and absent from the rows.** `total_trades`,
`win_rate`, `profit_factor`, `expectancy`, `best/worst_trade`, the Monte-Carlo bootstrap,
`stressed_results` and the profit cap's `excess` all inherit the gap — and
`adj_final = final - excess` (`results.py:678`) **subtracts a row-derived quantity from a
curve-derived one.**

Every candidate fitness is built out of trade rows. For the short-premium structures this whole
exercise is about, **the rows are missing the realised tail.** The fix is small — link the
liquidation order to the option transaction, or emit a row for the assigned lot — and it should
precede any fitness work. (Closely related to OPT-B2 in R1.)

## F8 — Nothing records that a book changed shape mid-life

`_exit_reason` (`backtest_account.py:2399-2410`) only ever returns `take_profit` / `stop_loss` /
`exit`, so an **assignment is indistinguishable from a normal exit**. Rolls, partial closes and
staggered leg exits are equally invisible.

Any ex-ante design that treats a `transaction_id` group as one fixed payoff over
`[min(entry_time), max(exit_time)]` therefore mis-prices exactly the wheel — the headline
premium-income strategy — and mis-prices it in an unpredictable direction, which is the direction a
GA searches.

## F9 — No risk term ever touches a losing genome

`strategy_fitness.py:696-697`:

```python
if base <= 0:
    return base   # unfactored: penalty factors on a negative would flip its sign
```

Every candidate design is multiplicative on top of CAR, so all of them inherit this. **Risk is only
priced along the profitable ridge.** Defensible for ranking, but it means no design in this family
can claim to "charge" for risk in general — only to reorder the winners.

## F10 — The equity cap embeds a hidden volatility penalty that handicaps the buy arm

`scoring_curve` compounds **per recorded point** (`equity_cap.py:96-104`) and the curve is one point
per daily bar (`snapshot_equity`, `daily_engine.py:788`), so annualised log growth is
`252 × (μ − σ²/2)` on a fixed denominator.

Measured drag: at $100/day P&L σ on a $20,000 cap, 0.32 %/yr; at **$400/day, 5.04 %/yr**.

A lumpy long-premium book therefore surrenders roughly **4.7 percentage points of `base` per year**
relative to a smooth short-premium book of identical total P&L — a structural buy/sell handicap of
20–40 % in relative terms, **built into the return term itself**, invisible to any risk term bolted
on afterwards. The buy/sell fairness argument is partly lost before a single fitness term runs.

## F11 — Trading is essentially free, so no design can lean on costs to brake churn

`--commission` defaults to **$0.10 per fill** (`ba2test_launcher.py:3959`) and `--slippage` to
**0.0** (`:3967`), and `tools/run_options_matrix.py` forwards neither. An 8-fill condor round trip
costs **$0.80** on a $20,000 account. Any fitness term that rewards turnover or trade count is
exploitable at will.

## F12 — The optimizer is scalar at six independent layers

`creator.create('FitnessMax', base.Fitness, weights=(1.0,))` (`genetic.py:177`);
`selTournament` (`:217`); `rebuild_population`'s hard-coded `ind.fitness.values = (float(fit),)`
(`:452`, which is also the **resume** path); `_trial_worker` returning `{'fitness': float(fit)}`
(`strategy_optimization_handler.py:241-243`); the remote HTTP contract in `distributed_eval.py`;
and `ga_fitness = Column(Float)` (`backend/app/models/backtest.py:139`).

Any Pareto/multi-objective design pays all six plus a migration, **and breaks mid-generation
checkpoint/resume.**

## F13 — A pricer exists, but not where a fitness function can reach it

Good news that cuts both ways. `backtest/option_greeks.py:38 bs_price` is pure Black-Scholes with
no I/O; `:57 implied_volatility` inverts by bisection; and `fetch_options.py:133/171` already calls
`compute_iv_and_greeks` at **cache-build** time, so the options sqlite carries historical
per-contract IV and delta. Re-pricing a book under a shock is cheap CPU.

The constraint is the seam: the chain cache is reachable only via `account._options.cache`, which
exists inside `build_results` (see `_delta_at_entry`, `results.py:249-265`) and **does not exist
inside `compute_fitness`** — which also runs on the master when re-scoring stored rows
(`ba2test_launcher.py:3470`) and inside `_maybe_robust`.

**Any shock overlay must be computed at results time and echoed as a key, never computed at fitness
time.** A design that puts re-pricing inside `compute_fitness` works in-process and silently fails
or raises on the top-N re-score path.

---

## Current fitness, transcribed (for reference)

`compute_fitness` (`strategy_fitness.py:255-342`). Sentinels, ordered so wiped-out < no-trade <
too-few-trades < any real score: `ZERO_TRADE_SENTINEL = -1e9`, `LOW_TRADE_SENTINEL = -1e8`,
`WIPED_OUT_SENTINEL = -2e9`.

`_consistent_annual_return` (`:632-753`) = `base × dd_guard × consistency × trade_gate`:

- **base** — `adjusted_annualized_return` whenever a profit cap is set. `--profit-cap-pct` defaults
  to **2000** and `--profit-share-cap-pct` to **25**, both DEFAULT-ON since 2026-07-31
  (`ba2test_launcher.py:3906, :3910`), so on the options grid base is **always** the adjusted
  figure: per-structure gains clipped at 20× cost basis and at 25 % of net profit.
- **trade_gate** — `< 12` trades/yr disqualified outright; 12–30 linear ramp 0.4×–1.0×; ≥ 30 → 1.0.
- **dd_guard** — `min(20 / max(dd, 1), 2.0)`. 1.0 at 20 % dd, capped 2.0 at ≤ 10 %, 0.667 at 30 %.
- **consistency** — `clamp(worst_year / mean_year, 0.25, 1.0)`; 1.0 if fewer than 2 years. Guarded
  by a **loud** `raise` if `equity_curve` is missing, naming the ~4× inflation it would cause.
- **sign guard** — `if base <= 0: return base` unfactored (F9).

Optional and off by default: win-rate factor (`val × 2 × win_rate/100`, silent no-op when win rate
is missing), trade scale (a structural no-op for CAR, which returns earlier), spread stress
(re-prices finished trades at a wider spread, takes the min), and robustness (multiplicative
concentration × Monte-Carlo × spread factors). `sharpe_ratio`, `sortino_ratio` and `win_rate` have
**no adjusted variant**, so they use raw values even under a profit cap.

---

## Recommended sequence — revised in light of the substrate findings

The design question is now secondary. In dependency order:

1. **`avg_trades_per_year` counts structures, not legs** (F3). One line. Improves every metric
   downstream and un-breaks the statistical-significance gate on exactly the structures that need it.
2. **Link the assignment's stock P&L back into the trade rows** (F7). Small, and every row-derived
   metric is wrong for the wheel until it lands. Pairs with OPT-B2.
3. **Add one walk-forward fold** (F4). Buys more out-of-sample validity than any metric change here,
   and nothing else on this list can substitute for it.
4. **Fix `max_drawdown or 0.0`** (F2). One line; currently hands the maximum risk reward to a genome
   whose risk is unmeasurable.
5. **Add a premium-richness entry gate** (OPT-C1) and **wire `iv_rank` as a gene** (R3). The GA
   cannot select for premium quality with neither the criterion nor the volatility gene available.
6. **Decide `option_sizing`: gene or declared constant** (F5). Until then no risk-denominated
   fitness means what it claims.
7. **Only then** design the fitness term itself — and note that F1 means the minimum viable change
   is *some* term that decreases with size, which CAR currently lacks entirely.

Two design families are already ruled out by the substrate: **return-on-collateral** (F5, F6) and
**multi-objective/Pareto** (F12). A **stress overlay** remains viable but must be computed at
results time (F13).

## Cross-link

**OPT-C1** — there is no premium-richness entry criterion at all, so the GA is rewarded for selling
near-worthless premium regardless of the fitness. No fitness change fully compensates for a missing
entry gate; F1 and OPT-C1 must be fixed together.
