# Option Risk Manager (`classic_options`) — Design

**Date:** 2026-08-27
**Status:** Approved design, not yet implemented
**Supersedes nothing.** Complements `2026-08-25-option-live-safety-seams-design.md` (which made the
live option path *safe*); this one makes it *decided*.

---

## 1. Problem

For equities, order origination is a two-stage pipeline: the rule engine decides *whether*, the risk
manager decides *how much*. For options there is no second stage at all.

Verified against the code as of `f154de11`:

1. **The risk manager never sees an option.** `_OptionEntryAction` (`TradeActions.py:1870`) fetches
   the chain, selects contracts, sizes the order and submits it, all inside the rule action. The
   backtest states the bypass explicitly — `daily_engine.py:1045`, `if self._entry_is_option:` skips
   `size_candidate_orders` entirely.
2. **The option path is confidence-blind.** `grep confidence TradeActions.py` returns nothing. A
   95%-confidence and a 51%-confidence recommendation open the identical structure at the identical
   size.
3. **There is no triage.** Every option rule that fires submits independently. Two structures on the
   same symbol do not compete for a budget, do not dedupe, and cannot know about each other.
4. **Term is a raw day window.** `dte_min`/`dte_max` are two correlated integers that can express an
   inverted window; there is no vocabulary in which "one month" is a strategy concept.
5. **Selection policy is frozen per rule.** One `strike_method` plus one `strike_param`, chosen by
   the GA and then applied unchanged to every symbol and every state of the chain that rule ever
   sees.

A correction to the premise this work started from: **the classic RM does not size by confidence
either.** Confidence only orders the queue (`compute_order_priority_score`,
`TradeRiskManagement.py:23`); quantity comes from the per-instrument cap, the instrument weight
config, `diversification_factor`, the regime scale and then notional or `risk_atr` sizing. The
option RM specified here *does* size by confidence — that is a deliberate divergence, decided in
§8.2, not an inherited behaviour.

## 2. Goal

A second stage for options: the rule engine proposes structures within stated boundaries; a new
`OptionRiskManagement` resolves each proposal into a concrete priced structure, ranks them, sizes
them against the expert's allocated capital, and submits the winners. Every policy knob it uses is
a gene, so the GA can search the policy rather than inherit a guess at it.

## 3. Decisions

Settled during design; recorded here because each one closes a fork that would otherwise be
re-litigated during implementation.

| # | Decision |
|---|---|
| D1 | The budget unit is **maximum loss**, accurate for every structure — not the broker reserve. |
| D2 | **Max loss and buying power are separate constraints and both must hold.** Max loss is the budget (what competes for allocation); the reserve is a feasibility gate (without it the broker rejects the order). |
| D3 | Structures with unbounded loss are refused unless a new expert setting `allow_undefined_risk_options` is on. **Default off.** When on they are charged on underlying notional (`spot × 100`). Excluded from the option grid for now. |
| D4 | Ranking score is `payoff_at_target / max_loss × (365 / DTE) × (confidence / 100)`. |
| D5 | Term is a **finite enum** mapping to a **hard DTE window with no widening**. |
| D6 | No capital sleeves. An expert is either a stock expert or an options expert; the option RM's budget is the expert's allocated virtual equity, with `max_virtual_equity_per_instrument_percent` applying per instrument as today. |
| D7 | Contract selection inside the rule's box is **weighted candidate scoring**; the weights are genes. |
| D8 | "Parallel short and long term" means **independent structures** the RM may take one or both of. Calendars/diagonals are deferred (§12). |
| D9 | A covered call with no shares held **acquires the stock first**, writing the call via the existing `WAITING_TRIGGER` / `depends_on_order` mechanism. |
| D10 | Confidence scales **both** rank and size. |

### 3.1 Why the reserve table cannot serve as max loss

`OptionsAccountInterface.option_reserve_required` is broker margin, and it diverges from true max
loss in both directions:

| structure | reserve today | true max loss |
|---|---|---|
| cash-secured put | `strike × 100` | `(strike − credit) × 100` |
| jade lizard | `(put_strike + call_width − credit) × 100` | `(put_strike − credit) × 100` — **overstates** by the call width |
| credit vertical, iron condor | `(width − credit) × 100` | same |
| put ratio spread | `(strike − credit) × 100` | bounded at zero underlying |
| short straddle / strangle | Reg-T ≈ 20% notional | unbounded above |
| long call/put, debit spread, butterfly | `0` | premium paid |
| covered call, protective put | `0` | entangled with the stock leg |

Both remain in use, for the two different jobs D2 names.

### 3.2 Why "expected value from IV" was rejected as the score

Under the risk-neutral distribution implied by a contract's own IV, every structure prices to
approximately zero expected value minus the spread. A score built on it ranks noise. Real edge can
only enter through the recommendation's own view, which is why D4's numerator is the payoff at the
**recommendation's target price**, not an integral over an implied distribution.

## 4. Architecture

### 4.1 Approach

**Request → resolve → triage.** The option actions stop submitting and become *resolvers*. A new
`OptionRiskManagement`, sibling to `TradeRiskManagement`, collects the bar's requests, resolves,
scores, triages, sizes and submits. This mirrors the equity temp-order-list flow already documented
in `trade_cycle.py`.

Two alternatives were considered and rejected:

- **Extend `TradeRiskManagement`.** It is 1244 lines and operates on `TradingOrder` rows; an option
  candidate is a *structure*, not an order. Existing equity sizing must stay bit-identical for every
  current backtest, and the perturbation risk is the whole objection.
- **An advisor the action consults before submitting.** An action cannot see the other actions on
  the bar, so triage is impossible. It fails the core requirement.

### 4.2 What makes this a bounded change

The submit path is already a single seam: `_submit_option_order(legs, quantity, limit_price,
option_strategy, option_reserve)` at `TradeActions.py:2326`. All 17 builders end by calling it. The
refactor splits each `_build_and_submit()` into a `_resolve()` that returns everything **except**
quantity, and lets the RM supply quantity and call the existing submit seam.

Each structure's knowledge of its own leg shape stays where it is and stays tested. Only *which
contract* and *how many* move out.

### 4.3 Components

| file | status | responsibility |
|---|---|---|
| `ba2_common/core/option_terms.py` | new, pure | `OptionTerm` enum ↔ DTE window |
| `ba2_common/core/option_payoff.py` | new, pure | payoff at expiry from a leg set → payoff-at-price and max loss |
| `ba2_common/core/option_selection_policy.py` | new, pure | weighted candidate scoring inside the box |
| `ba2_common/core/option_request.py` | new, pure | `OptionStructureRequest`, `ResolvedStructure`, `StructureRefusal` |
| `ba2_common/core/OptionRiskManagement.py` | new | resolve → score → triage → size → submit |
| `ba2_common/core/option_selector.py` | modified | selectors accept a `policy` instead of hardcoding `min(distance)` |
| `ba2_common/core/TradeActions.py` | modified | `_build_and_submit` → `_resolve`; emit request instead of submitting |
| `testplatform/.../daily_engine.py`, `ba2_trade_platform/core/TradeManager.py` | modified | route option requests to the option RM instead of bypassing it |

All new shared code goes in `packages/common` per the Phase 6 rule; the in-tree modules are
re-export shims.

### 4.4 Data flow, per bar per expert

```
each passing recommendation
  → enter ruleset → option action.execute()
  → OptionStructureRequest             (staged; NOTHING submitted)

OptionRiskManagement.process(expert_id, requests) →  (submitted, refusals)
  1 RESOLVE   chain fetch (cached per symbol/day) → filter to the box
              → policy.pick() → legs → price (buy@ask / sell@bid)
              → per-contract max_loss, reserve, payoff@target, DTE
              refusals are RECORDED, not raised
  2 SCORE     payoff_at_target / max_loss × 365/DTE × confidence/100
  3 TRIAGE    sort desc, greedy allocate, per-instrument cap, BP feasibility
  4 SUBMIT    combo via _submit_option_order
              buy-write → equity BUY, call staged WAITING_TRIGGER on depends_on_order
```

### 4.5 Data shapes

```python
@dataclass(frozen=True)
class OptionStructureRequest:
    """What a rule action proposes. Carries boundaries, never a decision."""
    structure: str                      # ExpertActionType value
    symbol: str
    expert_recommendation_id: int
    term: OptionTerm | None             # wins over dte_min/dte_max when set
    dte_min: int | None                 # legacy window, used when term is None
    dte_max: int | None
    strike_method: str                  # delta | percent_otm | consensus_target
    box_min: float | None               # e.g. delta 0.10; None with box_max set = open-ended
    box_max: float | None               # e.g. delta 0.25
    wing_width_pct: float | None
    min_open_interest: int | None
    max_spread_pct: float | None
    min_volume: int | None
    min_arc: float | None
    sizing_pct: float | None
    resolver: "_OptionEntryAction"      # the action instance, called to resolve


@dataclass(frozen=True)
class ResolvedStructure:
    """A concrete, priced structure — everything except how many."""
    request: OptionStructureRequest
    legs: list[OptionLeg]               # for _submit_option_order
    payoff_legs: list[PayoffLeg]        # for the payoff evaluator
    limit_price: float
    option_strategy: str                # reserve-table name
    reserve_kwargs: dict                # forwarded to option_reserve_required
    dte: int
    max_loss_per_contract: float        # or the UNBOUNDED sentinel
    reserve_per_contract: float
    payoff_at_target: float
    score: float


@dataclass(frozen=True)
class StructureRefusal:
    request: OptionStructureRequest
    phrase: str                         # one of the stable phrases in §9
    detail: str
```

Carrying the action instance as `resolver` is deliberate: it already holds the account, the
recommendation and the gates, so the RM can call `_resolve()` without reconstructing any of it.

`OptionRiskManagement.process()` returns `(submitted, refusals)` so both the log and the UI can show
what was declined and why — a refusal that is never surfaced is the same as a silent zero.

## 5. Max loss — derived from the payoff curve, not tabulated

A per-structure max-loss table would drift against the builders, exactly as the reserve table has
already drifted from true max loss (§3.1). Instead one pure evaluator computes it from the legs.

```python
@dataclass(frozen=True)
class PayoffLeg:
    kind: Literal["call", "put", "stock"]
    strike: float | None          # None for stock
    side: OrderDirection          # BUY = long, SELL = short
    ratio: int = 1                # legs per one structure unit
    premium: float                # per share, ALWAYS POSITIVE: paid if BUY, received if SELL
    multiplier: float = 100.0     # stock legs keep this default: one stock leg IS the
                                  # 100 shares backing one contract
```

Payoff of one structure unit at underlying `S`:

```
for each leg:
    intrinsic = max(S - K, 0)  if call
                max(K - S, 0)  if put
                S              if stock
    sign       = +1 if BUY else -1
    entry_cash = -sign * premium          # a buy pays, a sell receives
    leg_pnl    = (sign * intrinsic + entry_cash) * ratio * multiplier
payoff(S) = Σ leg_pnl
```

**Max loss** is `−min(payoff(S))` over the critical points `{0} ∪ {every strike}`. The payoff is
piecewise linear with kinks only at strikes, so the minimum over a bounded region is always attained
at a critical point — this is exact, not a sample.

**Boundedness.** The downside is always bounded: below every strike each put contributes at most its
strike, so `payoff(0)` is finite. Only the upside can run away, and only through net short calls.
The far-right slope is `Σ_calls sign · ratio · multiplier + Σ_stock sign · ratio · multiplier`; if it
is negative the evaluator returns `UNBOUNDED` rather than a number, and §8.3 decides what happens
next.

**A non-positive computed max loss is treated as UNMEASURABLE, not as free money.** A structure that
cannot lose at any underlying price is an arbitrage; in practice it means a stale or crossed quote.
It refuses with `MAX_LOSS_UNMEASURABLE_REFUSAL` and logs loudly.

Covered call, protective put and buy-write pass their stock leg into the same evaluator, so the
entanglement needs no special case. Note what the evaluator then says about a covered call: its max
loss is `(basis − credit) × 100`, the stock going to zero — **not** `(basis − strike − credit)`, which
is the intuitive-but-wrong answer a hand-written table invites. The strike caps the *upside*, not the
downside. This is the argument for deriving rather than tabulating, in miniature.

Only `payoff_at(legs, spot)` and `max_loss(legs)` are built. Max profit and breakevens are
computable from the same curve but have no caller, so they are not built (YAGNI).

## 6. Terms

```python
class OptionTerm(str, Enum):
    ZERO_DTE      = "0dte"
    ONE_WEEK      = "1w"
    TWO_WEEKS     = "2w"
    ONE_MONTH     = "1m"
    TWO_MONTHS    = "2m"
    THREE_MONTHS  = "3m"
    SIX_MONTHS    = "6m"
    LEAPS         = "leaps"
```

| term | DTE window |
|---|---|
| `ZERO_DTE` | 0–0 |
| `ONE_WEEK` | 1–9 |
| `TWO_WEEKS` | 10–18 |
| `ONE_MONTH` | 21–45 ← today's grid default |
| `TWO_MONTHS` | 46–75 |
| `THREE_MONTHS` | 76–115 |
| `SIX_MONTHS` | 150–210 |
| `LEAPS` | 300–450 |

The window is **hard**. If nothing selectable falls inside it the request is refused with the reason;
the RM never widens. Silent widening would make the term gene partly meaningless — the GA could not
distinguish "ONE_MONTH worked" from "ONE_MONTH became TWO_MONTHS half the time".

As a gene this is one categorical choice replacing two correlated integers that could express an
inverted window.

## 7. Selection policy

The rule states a **box** ("a put, delta 0.10–0.25, ONE_MONTH"). The RM chooses inside it.

```python
@dataclass(frozen=True)
class SelectionPolicy:
    w_box_center: float = 1.0     # PINNED reference weight, not a gene
    w_premium:    float = 0.0
    w_iv:         float = 0.0
    w_rvol:       float = 0.0
    w_spread:     float = 0.0
```

`score(candidate) = Σ wᵢ · fᵢ(candidate)`, maximised. Features, each **min-max normalised to [0,1]
across the candidate set**:

| feature | definition | higher is |
|---|---|---|
| `box_center` | `1 −` normalised distance from the box centre (delta or %OTM depending on `strike_method`) | closer to centre |
| `premium` | `(mid / strike) × 365/DTE` — per-contract premium richness | richer |
| `iv` | the contract's IV percentile **within the candidate set** (i.e. its position on the skew) | more expensive vol |
| `rvol` | contract volume ÷ median volume of the candidate set | more actively traded |
| `spread` | `1 −` normalised `spread_pct` | tighter |

**Normalising within the candidate set is what makes this work across symbols.** This is the direct
answer to the original R3 concern that option prices, bids and asks have no standard range per
symbol: an absolute threshold on premium is meaningless across a \$15 stock and a \$900 stock, but a
*rank within the chain in front of you* is scale-free. `iv` and `rvol` are deliberately
chain-relative for the same reason — symbol-level IV rank and relative volume already exist as rule
*conditions*, and duplicating them here would answer a different question badly.

`w_box_center` is **pinned at 1.0 rather than being a gene**: scaling all five weights together
changes no ranking, so leaving it free would give the GA a degenerate direction to wander.

**Missing values fail closed.** A candidate lacking a feature the policy weights non-zero scores
worst on that feature — the same discipline as `passes_liquidity`. If *no* candidate publishes it,
that is the `check_liquidity_data_available` situation and it raises a configuration error naming the
weight, rather than silently ranking every candidate identically.

**Ties** resolve by `(strike, expiry)`, exactly as `option_selector._tie` does today.

**The default policy is a provable no-op.** With only `w_box_center = 1.0`, the normalised distance
is a monotone transform of the raw distance, so the argmax equals today's argmin — including the
degenerate case where every distance is equal, where all candidates score 0 and the existing
`(strike, expiry)` tie-break decides, which is also what happens today. This is verified by a test
against a recorded chain, not asserted.

## 8. Triage and sizing

### 8.1 Three lanes

Not every option order is an opportunity competing for capital, and treating them uniformly would
mis-score two of the three kinds.

**Lane 1 — Opportunity.** Directional and premium-selling structures, plus the buy-write. New
capital at risk. Ranked by the D4 score, greedy-allocated against the budget.

**Lane 2 — Overlay.** A covered call or protective put written against shares **already held**. The
capital is already committed; the option changes the risk of an existing position rather than opening
a new one. These are **not triaged against lane 1**:

- *Covered call*: budget charge **0** — writing a call against shares you hold consumes no new
  capital and reduces risk. Constrained by **cover capacity** (`check_cover_for_covered_call`), not
  by capital, and admitted on the existing `min_arc` floor with collateral = the shares' market value.
- *Protective put*: budget charge = the **debit**, since it does cost money. Admitted when the rule
  fires and there is a position to protect. It is a hedge, so scoring it by payoff-at-target would
  refuse exactly the insurance a bullish view wants (a put scores negative against an upward target);
  it is therefore admitted, not ranked.

**Lane 3 — Servicing.** The equity purchase inside a buy-write. Not independently budgeted; its cost
is already inside the buy-write's max loss.

### 8.2 Lane 1 arithmetic

Inputs, all from existing accessors:

```
virtual_equity     = balance × virtual_equity_pct / 100                  # _virtual_equity()
per_instrument_cap = virtual_equity × max_virtual_equity_per_instrument_percent / 100
available_bp       = account buying power less reserved_option_buying_power()
committed[symbol]  = Σ max_loss of that symbol's open option structures under this expert,
                     plus anything taken earlier in THIS bar's triage
committed_total    = Σ committed[·]
```

Per candidate, in rank order:

```
instrument_left = per_instrument_cap − committed[symbol]
book_left       = virtual_equity    − committed_total
structure_cap   = virtual_equity × option_sizing_pct / 100
raw_budget      = min(instrument_left, book_left, structure_cap)
conf_budget     = raw_budget × confidence / 100
contracts       = floor(conf_budget / max_loss_per_contract)
```

then the feasibility gate, which can only reduce:

```
reserve = option_reserve_required(strategy, contracts, …)
if reserve > available_bp:  contracts = floor(available_bp / reserve_per_contract)
if contracts <= 0:          refuse (BUYING_POWER_REFUSAL)
```

then commit `contracts × max_loss_per_contract` to `committed[symbol]` and `committed_total`, and
decrement `available_bp` by the reserve.

**`option_sizing_pct` survives with a new job.** It stops being *the* sizing knob and becomes the
**per-structure** cap, while `max_virtual_equity_per_instrument_percent` is the **per-symbol** cap.
This preserves the existing gene and is what stops the first of two structures on one symbol from
consuming the whole instrument allowance.

**Confidence is applied twice** — once in the rank, once in the size. This is deliberate and
recorded here so it is not later mistaken for a bug.

**Committed exposure is read from persisted data, not reconstructed.** At submit time the RM writes
`max_loss_per_contract` onto the parent order's `data`, exactly as `option_reserve` is already
persisted (`TradeActions.py:2326`). `committed[symbol]` is then a sum over open option orders, with
no leg reconstruction and no OCC parsing.

### 8.3 Undefined risk

When the payoff evaluator returns `UNBOUNDED`:

- `allow_undefined_risk_options` off (**the default**) → refuse with `UNDEFINED_RISK_REFUSAL`.
- on → `max_loss_per_contract = spot × 100`, the same conservative full-notional treatment a
  cash-secured put receives.

The setting is fixed off in the option grid for now.

## 9. Refusals — a reason, never a zero

Every refusal is recorded on the `StructureRefusal` and carries a stable, greppable phrase, joining
the existing `ARC_FLOOR_REFUSAL` and `ASSIGNMENT_CAPACITY_REFUSAL`:

| phrase | meaning | severity |
|---|---|---|
| `UNDEFINED_RISK_REFUSAL` | unbounded loss, setting off | INFO |
| `MAX_LOSS_UNMEASURABLE_REFUSAL` | a leg has no price, or a non-positive computed max loss | ERROR |
| `CONFIDENCE_UNMEASURABLE_REFUSAL` | `recommendation.confidence is None` | ERROR |
| `TARGET_UNMEASURABLE_REFUSAL` | no target price, so payoff-at-target cannot be evaluated | ERROR |
| `NEGATIVE_EXPECTANCY_REFUSAL` | payoff at target is negative — the structure disagrees with its own recommendation | INFO |
| `BUYING_POWER_REFUSAL` | reserve exceeds available BP even at one contract | WARNING |
| `BUDGET_EXHAUSTED_REFUSAL` | normal triage outcome | INFO |
| `EMPTY_BOX_REFUSAL` | no selectable contract; names *which* gate emptied the set | WARNING |

`CONFIDENCE_UNMEASURABLE_REFUSAL` is the sharp one. `ExpertRecommendation.confidence` is
`float | None`. Under a confidence-scaling policy an absent confidence cannot read as 0 (a silent
no-trade, indistinguishable from "no signal") and must not read as 100 (full size on unmeasured
conviction). It refuses — the same discipline as `_held_equity_shares` returning `None`.

`TARGET_UNMEASURABLE_REFUSAL` deserves the same care. The target comes from the existing
`_consensus_target()` (FMP `target_consensus`, else `price × (1 ± expected_profit_percent/100)`).
Falling back to *spot* was considered and rejected: at spot an OTM credit structure sits at maximum
profit while every debit structure sits at a loss, so the fallback would systematically bias the
search toward premium selling. Refusing is honest.

**If either of these two fires broadly in practice, revisit the decision rather than the
threshold** — a refusal that fires on most bars is a design error, not a safety feature. This should
be measured in the first `classic_options` backtest.

## 9.5 Genes

| gene | domain | replaces / status |
|---|---|---|
| `option_term` | categorical over the 8 terms | replaces `option_dte_min` + `option_dte_max` — one choice instead of two correlated integers that could express an inverted window |
| `delta_box_min` | 0.05–0.50 | replaces the single `option_strike_param` point |
| `delta_box_max` | 0.05–0.50, `≥ box_min` | new; a degenerate box (`min == max`) reproduces today's point selection |
| `w_premium` | 0.0–2.0 | new policy weight |
| `w_iv` | −2.0–2.0 | new; signed, so the GA can prefer *cheap* vol as well as rich |
| `w_rvol` | 0.0–2.0 | new — this is the rvol gene |
| `w_spread` | 0.0–2.0 | new |
| `w_box_center` | **pinned 1.0** | reference weight, deliberately not a gene (§7) |
| `option_sizing_pct` | unchanged | retained, re-purposed as the per-structure cap (§8.2) |
| `min_arc` | unchanged, per collateral family | retained |
| `option_min_volume` | unchanged | retained |
| `strike_method` | unchanged | retained |
| `allow_undefined_risk_options` | **fixed off** | not searched in the grid (D3) |

`w_iv` is the one signed weight. Premium richness, relative volume and tightness have an unambiguous
good direction; implied volatility does not — selling structures want rich vol and buying structures
want cheap vol, and which is right is exactly the sort of question the GA should settle rather than
inherit.

The bands above are **derived from the arithmetic, not measured** — see §12.

## 10. Share acquisition (D9)

`SELL_COVERED_CALL` keeps its current meaning: write against shares already held, refuse otherwise.
Acquisition is the RM's job, using the existing dependency mechanism:

1. The RM sizes N contracts; N × 100 shares are needed.
2. Submit the equity BUY.
3. Create the short call with `status = WAITING_TRIGGER`, `depends_on_order = <equity order id>`,
   `depends_order_status_trigger = OrderStatus.FILLED`.
4. Live, `TradeManager._check_all_waiting_trigger_orders()` activates it; in the backtest,
   `backtest_account.refresh_orders()` step 1 does. Both paths are intact — the recent lean-simulator
   change replaced the TP/SL bracket legs only, not the dependency machinery.

**Partial fills are the part that must not be got wrong.** On activation the contract count is
**re-derived from the parent's `filled_qty`**, floored to whole lots: 250 shares filled writes 2
contracts, not 3. This is the same `floor(held / 100)` discipline `_held_equity_shares` enforces. If
`filled_qty` is `None` the call is **not** written and the leg is cancelled — an unknown fill is not
a full fill, and writing against it is a naked short call. A parent that ends `CANCELED` with zero
filled cancels the waiting call.

Max loss for the buy-write is evaluated by §5 with the stock leg at its expected fill price, so the
full stock cost is charged to the budget. There is no double counting: an options expert runs no
equity RM.

## 11. Scope of `classic_options` mode

A new value on the existing `risk_manager_mode` setting
(`MarketExpertInterface.py:330`). **The new path is opt-in per expert** — nothing existing changes
until an expert is switched over, which is what makes the bit-identical requirement easy to hold
rather than something to hope for.

Within a `classic_options` expert:

- Option **entry** actions route through the option RM.
- Equity **entry** actions are refused and logged ERROR once — per D6 an expert is either a stock
  expert or an options expert, and a stray equity entry is a misconfiguration worth surfacing.
- Equity **close** actions remain allowed: liquidating assigned shares and unwinding a buy-write both
  need them.
- Exit and adjustment actions (`close_option`, TP/SL adjustment) are unchanged.

### Backward compatibility

- The 14 live option entry actions carry `dte_min`/`dte_max`. Both fields stay; `term` wins when set,
  otherwise the raw window is used. No live rule breaks.
- A bare `strike_param` degenerates the box to a single point, giving selection identical to today.
- Experts in `classic` mode keep the current path in full.

## 12. Deferred, with reasons

- **Calendar and diagonal spreads.** Genuinely different economics (long vega, profit from
  differential decay) that two independent legs cannot replicate. Deferred because their payoff at
  the front expiry depends on the back leg's *remaining time value*, so §5's expiry evaluator is not
  sufficient — they need a pricing model.
- **Re-centring the gene bands.** The ARC bands and the new policy-weight ranges are derived from
  arithmetic and plausible values, not measured. Re-centre after the first real option grid.
- **The option grid itself remains blocked.** Wiring the TastyTrade parquet store into
  `HistoricalOptionsProvider` is the precondition — the backtest's only option reader is the
  Alpaca-served `OptionsHistoryCache`. Phases 1–4 below are fully testable without it; only phase 5
  depends on it.

## 13. Testing

**Pure units.** Table-driven payoff tests per structure, including the exotics, cross-checked against
closed-form max loss where one exists (credit vertical, CSP, iron condor, jade lizard, butterfly).
Property tests: max loss is attained at a critical point; the payoff is continuous at every strike;
`UNBOUNDED` is returned if and only if the far-right slope is negative.

**Policy no-op.** Default weights reproduce current selection on a recorded chain, contract-for-
contract, including the degenerate all-equal-distance case.

**Triage.** In-memory tests with a fake account: budget exhaustion, per-instrument cap binding, two
structures on one symbol, undefined-risk refusal both ways, confidence-missing refusal, BP-driven
reduction, and the three lanes not contaminating each other's budget.

**Buy-write sequencing.** Full fill, partial fill flooring to lots, `filled_qty is None` cancelling
the leg, parent cancelled with zero filled.

**Backtest determinism.** An existing option grid run reproduces bit-identically in `classic` mode
with all of the new code present and unused.

## 14. Phasing

| phase | content | behaviour change |
|---|---|---|
| P1 | the four pure units (`option_terms`, `option_payoff`, `option_selection_policy`, `option_request`) | none |
| P2 | `_build_and_submit` → `_resolve` split across the 17 builders (7 size via `_size`, 8 via `_size_by_reserve`, 2 off held shares) | none — `execute()` calls resolve then sizes and submits exactly as before |
| P3 | `OptionRiskManagement`: three lanes, triage, confidence sizing | gated on `classic_options` |
| P4 | buy-write and `WAITING_TRIGGER` share servicing | new structure |
| P5 | genes and grid wiring | grid only; blocked on §12 |

Each phase leaves the tree green and shippable.
