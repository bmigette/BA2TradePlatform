# Option data model and lifecycle — design

**Date:** 2026-08-24
**Scope:** Sub-projects **A** (option-aware data model) and **B** (shared lifecycle pass), designed
together because B's shape determines what A must store.

**Goal:** Make an option position a first-class, fully-recorded thing that the platform manages
over its whole life — so the wheel, condors and every other structure work identically in live and
backtest, on one code path.

---

## 1. Why this exists

Today the platform can *open* an option position and cannot reliably *know what it opened* or
*manage it afterwards*. Three measured facts drive the whole design.

**Of 28 filled option orders, only 11 record which contract was traded.** `submit_option_order`
deliberately nulls the contract fields on a multi-leg parent
(`OptionsAccountInterface.py:113-116`), and the parent is the row the broker fills. The children
carry the contracts but are never reconciled, sitting at `ACCEPTED / filled_qty=0` forever.
23 of 82 option orders have no recoverable contract at any price.

**Management is inexpressible in the rules engine.** `TradeActionEvaluator.evaluate(instrument_name,
expert_recommendation, ruleset_id, existing_order)` is per-instrument-per-recommendation *by
signature*. Book rails, a sleeve circuit breaker, roll-at-DTE and tested-delta defence all need to
see more than one position, or need facts (expiry, delta) that no condition exposes.

**So a parallel path was built and then stranded.** `PremiumSeller` +
`OptionPortfolioManager` can do all of it, but `run_analysis` raises
`NotImplementedError("backtest-only in v1")`, it is absent from the live registry, and its own spec
§11 says *"No wheel / no holding assigned stock."* Its two audits found the shared path reached
**~3% capital utilisation** against a 40% target and used equity-shaped TP/SL that is the wrong
exit for theta — real findings, addressed by forking the architecture rather than fixing it.

---

## 2. The organising idea

The split that matters is **not** options-versus-stocks. It is:

| phase | question | driven by | cadence |
|---|---|---|---|
| **Entry** | "should I open something on AAPL?" | expert signal, per symbol | the expert's analysis schedule |
| **Management** | "what does my book need today?" | position state and the calendar | the same schedule, but no expert |

These are two phases of one pipeline. Building them as two disjoint *experts* is the error being
corrected.

**Decision: unify.** Entry stays rules-driven — it works, it is live, it is configurable, and it is
expert-agnostic, which is what supporting `FMPRating` and `DeterministicScorer` requires.
Management becomes a shared lifecycle pass that every option position goes through regardless of
which expert opened it. `PremiumSeller`'s logic is **promoted** into that pass;
`PremiumSeller`/`OptionPortfolioManager` are then **deleted** as a separate path.

---

## 3. Data model — Transaction is intent, orders are execution

**`Transaction` = the intent.** "A bull call spread on ACN." One row, keyed on the **underlying**.

`Transaction.symbol` stays the **underlying ticker** and must not hold an OCC contract string.
`JobManager._execute_open_positions_analysis` selects `distinct Transaction.symbol` and submits a
market analysis per value; an OCC string there would be analysed as a ticker, breaking the wheel's
second leg and every rating, price and screener condition. A four-leg condor has four contract
symbols and one intent, so the underlying is also the semantically correct value.

Added to `Transaction`:

| field | purpose |
|---|---|
| `asset_class` | today the only tell that a row is an option is `multiplier=100` |
| `option_strategy` | the intent: `bull_call_spread`, `iron_condor`, `covered_call`… |
| `expiry` | the structure's expiry — see the single-expiry constraint below |

`strike` is deliberately **not** added: it is meaningful for a single leg and misleading for four.

**A single `expiry` on the intent is only valid because every structure the platform supports is
single-expiry.** All 16 entry structures — the four singles, the four verticals, straddle/strangle
and their short forms, iron condor, jade lizard, call butterfly, put ratio spread — put every leg
on one expiry. There are no calendars or diagonals. If one is ever added, `Transaction.expiry`
becomes wrong rather than merely incomplete, and the field must move to the legs. That is the
condition under which this decision must be revisited, and it should be asserted in code so the
next person adding a structure cannot miss it.

**Orders = the execution.** `TradingOrder` already carries `contract_symbol`, `option_type`,
`strike`, `expiry`, `underlying_symbol`, `multiplier`, `position_intent`, `option_strategy`, and
the parent/child graph already models a structure correctly (one parent, N children sharing a
`transaction_id`). Two things are broken on top of it and both are fixed here:

1. **Leg fills are never reconciled.** Children must be updated from the broker via
   `legs_broker_ids` so they carry real `status`, `filled_qty` and `open_price`. Until they do,
   per-leg attribution is impossible and the executed position is inferred from rows the DB says
   never executed.
2. **The parent has no contract identity.** Stamp what is unambiguous at the structure level —
   `expiry`, and for a single leg the full contract — so a reader has something honest without
   walking children.

**Migration is forward-only.** 23 of the 82 existing option orders have unrecoverable contracts;
backfilling is not possible and not attempted. The historical option book stays partially
unreconstructible, and this is stated rather than papered over.

---

## 4. The shared lifecycle pass

**Trigger: the existing `open_positions` schedule, running BEFORE the expert analyses.**

No new cron. The schedule is one the user already configures per expert, and the symbol set is
already portfolio-driven — `_execute_open_positions_analysis` reads open transactions, not the
screener, which is why the wheel's second leg re-arms at all.

**It must not invoke an expert.** Maintenance is calendar and state driven: roll at 21 DTE, capture
at 50% of credit, defend a tested short, trip a circuit breaker. None needs an opinion about the
underlying — the ledger and the chain already answer it. Paying for an FMP call plus an LLM
analysis to discover a spread is at 21 DTE is precisely the cost behind the "options must be as
fast as stocks" requirement. The expert analysis still runs afterwards and still owns anything
genuinely needing a view, such as cutting assigned shares on a downgrade.

**Cadence is daily, and that is sufficient.** Roll-at-DTE is a calendar fact; 50%-of-credit on a
30–45 DTE spread takes weeks; assignment detection already happens on the 5-minute reconcile. Only
a tested short has an intraday argument, and defending next morning is a defensible policy.

**What the pass owns** — promoted verbatim from `OptionPortfolioManager`, and unavailable to any
rule:

- book rails: `max_deployment_pct`, `max_notional_leverage`, `undefined_risk_max_pct`
- circuit breaker on sleeve drawdown, with peak-equity tracking
- concurrent-structure and one-per-underlying caps
- exits: profit capture, credit-multiple stop, tested-delta defence, DTE roll

**Rails are per-expert sleeves**, inheriting the expert-scoped symbol set. This matches
`PremiumSeller`'s original semantics. An account-wide cap across several option experts is a
different feature and is out of scope.

**Two failure modes to surface, not hide:**

- An expert holding open option positions with **no `open_positions` schedule** never manages them.
  The startup readiness report must flag it. A silent never-managed position is the failure this
  whole design exists to remove.
- **Only `AlpacaAccount` implements `reconcile_option_assignments`.** IBKR and TastyTrade have no
  assignment detection at all, so the wheel is Alpaca-only until sub-project G lands. Say so at
  startup rather than letting a TastyTrade wheel silently stall.

---

## 5. Bugs this design must fix

These are measured, reproduced defects — not speculative hardening.

**Partial called-away erases real shares.** One assigned contract against a 300-share transaction
closes **all 300**; `_close_txn` has no partial close, so 200 shares vanish from the ledger while
sitting at the broker. Currently only logged as a warning. Fixing it requires a transaction split
and belongs here.

**Roll-at-DTE never fires for multi-leg.** `OptionPortfolioManager._should_close` reads
`parent.expiry`, which `submit_option_order` sets to `None` for every multi-leg. The design's
headline exit — "manage at 21 DTE to avoid end-of-life gamma" — is dead for `put_credit_spread`
and `short_strangle`, and every GA result for those families was produced with a dead roll gene.
Stamping `expiry` on the parent (§3) fixes it.

**The backtest cannot run the wheel at all.** `settle_single_leg_expiry` assigns a short ITM put and
then schedules a full liquidation of the resulting stock at the next bar's open. The shares are
gone before any manage pass can write a call. Backtest and live must agree here or the strategy
cannot be validated.

**Live/backtest parity is a design constraint, not a nicety.** The lifecycle logic lives in
`packages/` and both engines call it. Two implementations of one rule is exactly how the
short-sign divergence happened.

---

## 6. Error handling

- **Unknown is never a value.** A missing quote, a missing greek or an unavailable IV must remain
  distinct from zero. This codebase has been bitten five times by collapsing "we don't know" into a
  number.
- **Gates fail closed.** A liquidity or IV gate whose data is absent must refuse, and a filter whose
  data is *entirely* absent must be a loud configuration error rather than a silent zero-result.
- **The pass must be idempotent.** It runs on a schedule and may overlap a manual action;
  `has_pending_closing_order` is the existing primitive and must guard every close.
- **A broker that cannot answer stops the pass for that account**, rather than proceeding against a
  book it cannot see.

---

## 7. Testing

- **Unit, pure**: the lifecycle decision function is pure — positions and chain in, actions out —
  and is tested without a DB or a broker.
- **Integration**: the full wheel, CSP → assignment → covered call → called away, driven through
  the real reconciler with a faked broker at the account seam. Faking below that seam is what let
  the assignment gap ship.
- **Parity**: the same scenario through the live pass and the backtest engine must produce the same
  decisions.
- **Mutation testing on every money path**, which on this project has repeatedly found live bugs
  that a green suite did not.

---

## 8. Out of scope, captured so the design does not foreclose them

- **C — contract and structure selection.** Scoring over the joint expiry/strike/premium space
  rather than nearest-delta; condition-gated structure choice; short vs swing DTE regimes.
- **D — optimization**, including the **grid runner** for a 0DTE arm and a 30–45 DTE arm across
  `FMPRating` and `DeterministicScorer`. This gets its own spec and plan as the immediate follow-up
  to A+B. Constraints settled now, because they shape that work:
  - **DECIDED: the 0DTE arm covers BOTH ETFs and stocks, and the grid tests both.** They are not
    the same strategy and must be separate arms rather than one universe:

    | | expiry cadence | analysis day | universe |
    |---|---|---|---|
    | **Index ETFs** (SPY, QQQ, IWM…) | daily | any trading day | small, fixed, no screener needed |
    | **Large-cap stocks** | weeklies expiring Friday | Friday only | screener-driven |

    Treating them as one arm would either waste four days a week of ETF opportunity or generate
    stock entries on days with no listed same-day expiry. The grid runs both and they are compared,
    not merged.
  - **Analysis-day alignment is a grid parameter, not a setting**, and it is universe-dependent per
    the table above. 0DTE requires the analysis to run *on* the expiry day; 30–45 DTE requires entry
    days that land on a real listed expiry. The schedule genes must therefore be aware of which arm
    they belong to.
  - **An optionable-symbol screener filter needs a source.** FMP does not expose one; the options
    are deriving it from the broker contract list and caching, or a maintained universe file. It
    matters only for the stock arms — the ETF arm's universe is fixed and known-optionable.
- **E — backtest fidelity** beyond the wheel: greeks column migration, spread-cost calibration.
- **F — dedicated options UI page**, which depends on this spec landing first.
- **G — TastyTrade options support**, and Alpaca parity. Both brokers must reach the same
  capability. IV rank differs by necessity: TastyTrade publishes
  `implied_volatility_index_rank` directly; Alpaca does not and needs the computed series. Alpaca
  **does** publish `open_interest` (already read) and volume via the snapshot's `daily_bar` (not yet
  read), so live liquidity gates are an adapter fix, not a data purchase.
- **Account-wide rails** across multiple option experts.
