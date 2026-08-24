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
  - **DECIDED: the grid searches DEFINED-RISK structures only.** No position whose loss is
    unbounded. Of the 16 entry structures that splits as:

    | | |
    |---|---|
    | **In** | `buy_call`, `buy_put`, `bull_call_spread`, `bear_put_spread`, `bear_call_spread`, `buy_protective_put`, `open_call_butterfly`, `open_iron_condor`, `open_straddle` and `open_strangle` (LONG — a paid debit, loss capped at the premium), `sell_covered_call` (covered by shares), `sell_cash_secured_put` (bounded at strike x 100, and cash-reserved) |
    | **Out** | `open_short_straddle`, `open_short_strangle` — naked on both sides; `open_put_ratio_spread` — the extra short put is naked below the long strike |
    | **Judgement** | `open_jade_lizard` — no upside risk by construction, downside equals a cash-secured put. Defensible as "in", but excluded unless deliberately re-admitted |

    Note the LONG straddle and strangle are defined-risk and stay in; it is their SHORT forms that
    go. The codebase already has this concept: `PremiumSeller` defaults `enable_short_put` and
    `enable_short_strangle` to `False` and carries an `undefined_risk_max_pct` rail, which the
    lifecycle pass inherits (plan Task 7).

  - **DECIDED: direction is a third grid axis — BUY-ONLY (net debit) vs SELL-ONLY (net credit).**
    Premium *buying* (pay a debit, make money when the contract is worth more) and premium
    *selling* (collect a credit, make money on decay) are different businesses with mirrored
    skew, and the grid tests them as separate arms. With the two axes already decided that gives
    8 cells: {0DTE, 30–45 DTE} × {index ETF, large-cap stock} × {buy, sell}.

    **The debit/credit discriminator already exists in the code** and must be reused rather than
    re-derived: exactly the 7 short-premium builders pass `option_reserve=` to their submit call
    (`TradeActions.py:2546` CSP, `:2631` bear call, `:2815` short straddle, `:2883` short strangle,
    `:2955` IC, `:3027` jade lizard, `:3163` put ratio); debit builders pass none. Do **not** use
    the sign of `limit_price` — covered call and CSP submit a *positive* limit at `contract.bid`
    (`:2445`, `:2541`) while bear call spread and IC submit negative (`:2629`, `:2954`), so a sign
    test misfiles two of the In-list structures.

    **Expressed as arms, not as a gene.** The "sweep group" object already exists — `_OPTION_GROUPS`
    (`ba2test_launcher.py:2276-2296`) is one optimize job searching a family, each member becoming
    a toggleable `entry:<member>-entry:enabled` 0/1 gene. A `structure_mode` categorical is not
    expressible anyway (only `model:*` keys take categoricals; everything else routes through
    `_range_entry`, int/float only), and more decisively the split governs *run-level* things a
    per-individual gene cannot vary: the exit-rule template (`_option_exit_rules:2451-2484`
    branches on `_DEBIT_OPTION_KINDS`), the row-level `fitness_metric`, the per-invocation fitness
    knobs, and the hand-set `option_sizing` constants (5–8% debit vs 15–20% credit).

    | arm | today | note |
    |---|---|---|
    | **BUY** | `--strategies OS1,OS4` — works with **zero new code** | `OS1` = `O_LC`, `O_LP`, `O_VERT`, `O_BF`, `O_BULLCS` ("directional DEBIT"); `OS4` = `O_STRD`, `O_STRG` ("volatility DEBIT") |
    | **SELL** | needs a new group key | `OS2`/`OS3` as shipped are mostly unusable — see below |

    **The sell arm is the problem, not the buy arm.** After the spec's own defined-risk filter
    removes `open_short_straddle`/`open_short_strangle`, and after `_FULL_NOTIONAL_OPTION_KINDS`
    (`:2270`) strips `O_CSP`/`O_JL`/`O_RS` because a CSP reserves ~$28,800 at spot 320 against the
    `--initial-capital` default of **10,000** (`:3842`), the searched credit residue is just
    **`O_IC` and `O_BEARCS`** — one neutral, one bearish. Against the buy arm's 7. **The sell arm
    has no bullish defined-risk credit expression at all.**

    **`bull_put_spread` is confirmed absent** — zero hits repo-wide across `.py`/`.md`/`.ts`. The
    put credit spread is the canonical defined-risk income structure and its absence is the single
    largest gap in the axis. A `build_put_credit_spread` does exist at
    `packages/experts/ba2_experts/PremiumSeller/structures.py:87`, but it bypasses `TradeActions`
    entirely and is **not** in `BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES`
    (`backtest_account.py:433`), so it gets no MTM clamp and no unit combo-expiry settlement. It is
    not a drop-in. Either build `open_bull_put_spread` end to end (enum member + `_OptionEntryAction`
    + `action_map` + `_OPTION_STRATS` key + the tag added to `DEFINED_RISK_SHORT_STRATEGIES` +
    registration in `_DEBIT_OPTION_KINDS`'s complement), or raise `--initial-capital` to ~$100k to
    re-admit `O_CSP`. Registering any new group key is **three** edits — `_OPTION_GROUPS_ALL`
    (`:2280`), `_STRATEGY_BUILDERS` (`:2687`), `_DEBIT_OPTION_KINDS` (`:2447`) — and skipping the
    third silently hands a debit group the credit exit profile (tight TP plus a stop that can
    never fire).

  - **BLOCKER — the adjusted-fitness profit cap is broken for options, and it is not a skew
    argument, it is a units bug.** `results.py:448` computes
    `cost = (t["entry_price"] or 0.0) * (t["size"] or 0.0)`. For an option leg `entry_price` is
    premium **per share** and `size` is **contracts**, so the ×100 contract multiplier is missing —
    even though the P&L eleven lines earlier in the same function *does* apply it
    (`gross = (exit_px - entry_px) * size * direction * mult`, `backtest_account.py:2258`). `cost`
    is therefore 1/100th of the capital actually deployed while `pnl` is full size, so the
    default-on `--profit-cap-pct 2000` caps a trade's gain at **20% of deployed capital, not 20×**.

    This is not a buy-arm inconvenience; it invalidates option grid results already produced:
    - A long call that triples has its gain truncated to 0.2× the premium paid. The buy arm's
      entire right tail is deleted.
    - Trades are paired **per leg** (`backtest_account.py:2134-2156`, group key
      `(transaction_id, contract_symbol)`), so a spread's *winning* leg is capped and its *losing*
      leg is not. **An iron condor at maximum profit scores a negative adjusted return.**
    - `_consistent_annual_return` — the default fitness for every pure-option kind
      (`_resolve_fitness:2304-2316`) — ranks on `adjusted_annualized_return` whenever either cap
      key is set (`strategy_fitness.py:669-673`). The default path *is* the broken path.

    And it cannot currently be switched off from the driver: `tools/run_options_matrix.py:204-207`
    forwards each flag only `if args.X and args.X > 0`, so passing `0` makes it falsy, **omits** the
    flag, and lets the launcher re-apply 2000.0/25.0. The help text "Pass 0 to disable" (`:132-138`)
    is wrong. The same guard bug is in `run_senate_matrix.py:142-143` and
    `run_screener_capband_matrix.py:409-410`. Until fixed, disable by invoking `ba2-test optimize`
    directly with **both** `--profit-cap-pct 0 --profit-share-cap-pct 0` (line 669 ORs the two).

    Fixing the multiplier also forces a real design decision that must be made explicitly rather
    than inherited: **should the cap apply per leg or per structure?** Per-leg capping is what
    produces the negative-scoring iron condor, so a multiplier fix alone does not make spread
    scoring correct.

  - **Other measurement biases against the buy arm**, each to be settled before the arms are
    compared:
    - `trade_gate` counts **legs, not structures** (`results.py:383`, `:502`). One iron condor entry
      is 4 trades, one long call is 1. The gate hard-disqualifies below 12/yr with a `-1e8`
      sentinel and ramps to full credit at 30/yr, so the sell arm clears both thresholds on roughly
      half the structure count. The same mechanism scrambles `win_rate`: an iron condor **at max
      profit** books 2 winning and 2 losing legs — a 50% win rate.
    - `dd_guard = min(20/max(dd,1), 2.0)` rewards the smooth premium seller, capped at 2×.
    - `--fitness-win-rate-factor` multiplies by `2 × win_rate` (~0.70× for a 35%-win buy arm vs
      ~1.60× for an 80%-win seller). `--robust-fitness`'s concentration screen returns **exactly
      0.0** when the top 5 trades reach 100% of net P&L — which *is* the definition of a
      tail-carried buy book, leaving the GA no gradient whatsoever. Both are opt-in; decide
      explicitly rather than inheriting a default.
    - **Cross-arm comparison must use a yardstick computed outside `compute_fitness`.** The bias is
      metric-specific rather than uniform — on synthetic equal-P&L books `total_return` is exactly
      neutral while `sharpe`/`sqn`/`car` favour the seller ~1.7–1.8× and `sortino` inverts to an 8×
      buy-side advantage. Use dollar expectancy per unit of defined risk, or plain `total_return`.
      Per-arm `--fitness` already works (`--fitness` short-circuits `_resolve_fitness` at `:2314`),
      so the two arms can be *scored* differently and *compared* on a neutral third measure.

  - **Backtest fidelity, buy-arm specifics.** One feared distortion is absent and one is real:
    - **Long ITM expiry IS monetised** — `settle_single_leg_expiry` (`backtest_account.py:2736-2766`)
      sells to close at the expiry bar's premium, falling back to intrinsic. The buy arm's rare big
      win is paid. Good.
    - **Spread crossing is charged only on multi-leg children.** `_option_fill_price:1529-1544`
      applies the modelled spread only when there is no `limit_price`; every single-leg option order
      carries one, multi-leg children do not. So `O_LC`/`O_LP` pay **zero** spread while `O_IC` pays
      it 4× — the cost cliff runs *inside* the buy arm, and an unconstrained GA will discover "use
      the single-leg structure" as a cost artefact rather than as economics. There is no derived
      spread to charge anyway: `_pit_quotes` sets bid = ask = close, and **zero** of 4,328,587
      quoted chain rows have `ask > bid`.
    - **`straddle` and `strangle` — the whole of OS4 — are missing from
      `DEFINED_RISK_LONG_STRATEGIES`** (`backtest_account.py:430-432`), so they get no group MTM
      clamp and a single outlier premium print can distort their drawdown, which feeds `dd_guard`.
    - **Pin the interval and fill model.** `tools/run_options_matrix.py` defaults `--interval 1d`;
      `ba2-test optimize` defaults `5min`, at which `_apply_option_expiry` settles at ~09:35 on
      expiry day and deletes the final session's move. Worse, at `1d` with the default
      `--fill-model next_bar_open` a **0DTE contract has no D+1 bar and never fills at all**
      (`get_bar` is an exact-date lookup with no forward-fill). The 0DTE arm needs
      `same_bar_close` plus a last-bar-of-session settlement gate. **Verify empirically** — this is
      deduced from the code, not observed in a run.

  - **Exit rules that misbehave on debit structures.** Nothing crashes, but meanings flip:
    `profit_loss_percent` divides by `abs(entry net premium)` (`TradeConditions.py:1643`), which is
    max *profit* for a credit and max *loss* for a debit. **No stop-loss gene exists for the buy arm
    at all** — `_option_exit_rules` appends `opt_sl` only `if not debit` (`:2478`). PremiumSeller's
    stop is a range problem rather than a missing gene: `dr_stop_credit_mult` ranged 1.5–3.0 gives
    −150%..−300%, unreachable below a debit's −100% floor, but `mult = 0.5` is exactly a "−50% of
    debit paid" stop, so extending the range below 1.0 and renaming suffices. `_tested` does
    `if n >= 0: continue` — a permanent no-op on all-long structures, and on a debit vertical it
    would fire against the short leg *at maximum profit*, so it must not be promoted verbatim into
    the shared lifecycle pass (plan Task 6).

    Finally the rails. All three are blind to a long-only book — `_txn_metrics` returns
    `(True, 0.0, 0.0)` when a transaction has no executed SELL leg, so a pure-debit arm reports
    zero deployment and nothing engages. **But they must not all be fixed the same way**, and an
    earlier draft of this spec was wrong to say they should all move to a premium-outlay basis.
    Collapsing three distinct risks onto one number would make the same setting name mean
    different things in the two arms:

    | rail | for a debit arm |
    |---|---|
    | `max_deployment_pct` | **Reimplement on premium outlay.** Capital at risk is capital at risk, and for a debit the outlay *is* the max loss. This one shares cleanly. |
    | `max_notional_leverage` | **Leave short-side.** It caps assignment exposure, and a long option cannot be assigned against you. Outlay is not notional; a long-strike number here would not be comparable to the credit arm's under the same setting. |
    | `undefined_risk_max_pct` | **Leave inert — that is correct.** A debit structure has no undefined risk. A premium-outlay naked cap would be meaningless. |

    Inertness must be *visible* rather than silent: the promoted rail check reports which rails
    actually ran, so "this rail did not apply" is distinguishable from "this rail passed". A
    candidate that declares itself undefined-risk hits the naked cap whatever its strategy is
    called, so a naked structure under a novel name cannot skip the rail — the existing hardcoded
    `("short_put", "short_strangle")` tuple fails open otherwise.

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

---

# Sub-project H — TastyTrade historical option data (NEW, follow-up)

Added 2026-08-24 after probing the live DXLink feed. **This removes the need for ThetaData.**

## What was measured

`test_files/probe_tastytrade_option_history.py`, run against the real production account.
`DXLinkStreamer.subscribe_candle(symbols, interval, start_time)` accepts an arbitrary
`start_time`, and each `Candle` carries `open/high/low/close`, `volume`, `vwap`,
`bid_volume`, `ask_volume`, **`imp_volatility`** and **`open_interest`**.

**Contracts that have already EXPIRED are served.** This was the make-or-break question and the
answer is yes — a 2024 expiry returns its full life and stops on its expiry date.

Coverage, by expiry, measured on AAPL:

| expiry | bars | IV | OI |
|---|---|---|---|
| 2019-01-18 | 101 | 0% | 0% |
| 2021-01-15 | 334 | 0% | 35% |
| 2022-01-21 | 507 | 0% | 79% |
| 2023-01-20 | 591 | 13% | 100% |
| 2024-01-19 | 587 | 55% | 100% |
| 2024-09-20 | 236 | 99% | 99% |
| 2026-02-20 | 173 | 94% | 99% |

IV is not a per-contract cutoff but a **wall-clock floor**: counting IV-bearing bars back from each
expiry lands on roughly **October 2022** every time. So ~8 years of bars, ~5 years of open
interest, ~4 years of IV.

**Limits found:**
- **No intraday for expired contracts.** An hourly request on a dead contract returned one junk
  sentinel bar. Daily only for history — a 0DTE arm can be backtested open-to-close but not
  managed intraday.
- **No bid/ask prices.** `Candle` carries `bid_volume`/`ask_volume` but not the quotes, so the
  spread-cost calibration gap is NOT closed by this source. Needs its own probe.
- **The WebSocket needs an explicit certifi SSL context.** REST works because `httpx` uses certifi
  while `websockets` picks up a self-signed corporate root from the system store. Pass
  `DXLinkStreamer(session, ssl_context=ssl.create_default_context(cafile=certifi.where()))`.

## Scope

**H1 — `TastyTradeOptionsProvider`.** A historical options provider alongside the existing
`ThetaDataOptionsProvider`, which is already written, registered in `OPTIONS_PROVIDERS` and
unit-tested but has **zero production callers**. That seam already exists; use it rather than
inventing a second one. Streaming, not REST, so the shape is a paced crawl over contracts with
backpressure — not a drop-in for `fetch_options.py`'s request loop.

**H2 — the cache moves to PARQUET ON DISK, not SQLite.** DECIDED.

The current cache is a single 10 GB SQLite file
(`~/Documents/ba2/common/cache/options/options_history.sqlite`) and it is the **outlier** in this
platform: the cache tier holds **33,425 parquet files against 2 sqlite ones**. OHLCV
(`ohlcv_cache_provider.py`), the screener metric store, the news store and `native_cache.py` are
all parquet via `pyarrow` (24.0.0, already installed). Options should match rather than introduce a
third convention.

Beyond consistency, it fits the workload better: columnar compression on 63M+ mostly-numeric bars,
cheap date-range scans via row groups, natural partitioning by underlying (1,917 of them), and —
the operational one — **no write-lock contention when several GA workers read the cache
concurrently**, which a single SQLite file across processes handles badly.

It also makes the schema problem vanish rather than needing a fix. The recorded blocker was that
`option_bar` has no `iv`/`delta`/`gamma`/`theta`/`vega` columns, `_BAR_COLS` names them in the
INSERT, and because the DDL is `CREATE TABLE IF NOT EXISTS` a rebuild **crashes on the first bar
write** (reproduced). Writing a fresh parquet store sidesteps the `ALTER TABLE` entirely — the new
store simply has the columns, including `open_interest`.

Follow the existing OHLCV cache layout for partitioning and naming so one mental model covers both.
The old SQLite file is not migrated: the warm-up rebuilds 2023-01-01 onward from TastyTrade, which
is a superset of what it holds for that window and carries the IV and OI it never had.

**H3 — the warm-up script.** Build the grid's cache over **2023-01-01 → today**. Chosen because it
sits inside the IV floor, so every bar carries IV rather than starting ragged. It must be
resumable (63M+ bars in the existing cache; a crawl that cannot resume is unusable), report
coverage honestly per symbol, and never silently write a bar with a NULL IV where one was expected.

## Why this matters beyond convenience

IV and greeks are currently *computed* — `fetch_options.py` would derive IV by Black-Scholes
inversion of each bar's close. That is a model output, adequate for strike selection and flagged as
such. dxfeed's `imp_volatility` is exchange-derived, and **`open_interest` cannot be computed at
all**. Today OI is NULL across all 6,757,055 chain rows, which is why `min_open_interest` rejects
the entire chain in backtest and why delta-based selection yields zero trades.

## Depends on

Nothing in A+B. Can be specced and built in parallel. It **blocks D** (the grid runner), because a
delta- or IV-gated arm cannot be optimised against a cache that has neither.
