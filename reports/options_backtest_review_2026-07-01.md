# Deep review — options-backtest commits `b715f3e..ffc89df` (2026-07-01)

Scope: the 12 incoming commits (unit settlement, cash-secured guards, maintenance
margin + forced liquidation, defined-risk MTM clamp, assignment/netting fixes,
round-trip pairing, snapshot floor) + the validation report. Reviewed for
correctness, lookahead, live-vs-BT parity, and fill/expiry/assignment semantics.
All 234 `tests/backtest/` tests pass post-merge on this machine.

## Verdict

The architecture is sound and the layered fixes address real, reproduced blow-ups
(−256%/−473%/−8974% cases each have a targeted test). No lookahead found. Equity-only
runs are provably untouched (hard `self._options is None` short-circuits). But several
of the new bounds are **looser than the risk they claim to bound**, and two paths
mis-account edge cases. Ranked findings below.

## Findings (ranked)

### F1 — Iron-condor clamp width uses the BODY gap, not the wing (bound ~2× too loose) — HIGH
`_option_group_bounds._width` = `max(adjacent strike gaps)` and
`settle_defined_risk_combo_expiry` uses the same widest-gap bound. For an iron condor
with strikes `k1<k2<k3<k4` (put wing / body / call wing), the widest adjacent gap is
usually the **body** `k3−k2`, but the structure's true defined risk is
`max(k2−k1, k4−k3)` (the wider wing). Example 90/95/105/110: widest gap = 10 (body),
true max loss = 5/share. Both the mid-life MTM clamp and the expiry safety clamp
therefore permit ~2× the real defined risk — precisely the class of outlier this work
exists to bound.
**Fix:** make width strategy-aware: `iron_condor` → `max(k2−k1, k4−k3)`;
verticals (2 strikes) → the gap (current behavior correct); butterfly → wing gap
(`min` of the two gaps for a broken-wing fly, equal-wing unchanged).

### F2 — Covered-call short legs are charged NAKED margin — HIGH (for small accounts)
`maintenance_margin_requirement` exempts only defined-risk COMBO legs
(`_defined_risk_contracts`). A `covered_call` short leg — covered by 100 shares of the
underlying per contract, classic margin requirement ≈ 0 — is charged the full naked
~20%-of-notional AND sits in the `maybe_margin_call_liquidation` candidate set. Under
equity stress the engine can (a) overstate the requirement enough to trigger a false
breach and (b) buy back a fully covered call to "fix" it.
**Fix:** in both the requirement and the liquidation candidate filter, exempt a short
CALL whose underlying long share position ≥ `100 × contracts` (and shrink the shares'
own availability accordingly if you want to be strict). `cash_secured_put` at naked
margin is acceptable (Reg-T-ish), just document it.

### F3 — Butterfly expiry bound scales by the BODY leg count + hardcoded 100 — MEDIUM
`settle_defined_risk_combo_expiry` sets
`contracts_per_structure = max(leg quantities)` — for a 1-2-1 butterfly the body leg
carries `2×structures`, so the bound is 2× loose; it also hardcodes `* 100.0` instead
of the legs' multiplier. Inconsistent with `_option_group_bounds._width`, which
correctly uses the PARENT's structure quantity and its multiplier.
**Fix:** derive structures from the parent order's quantity (same as `_width`) and use
the leg multiplier.

### F4 — Forced STOCK liquidation leaves no order/trade record — MEDIUM
`_liquidate_stock_position` moves cash + updates the ledger but records no
TradingOrder — equity jumps with no visible trade in `get_round_trip_trades`/reports.
(The option-lot path DOES record a synthetic close via `_record_option_expiry_close` —
asymmetric.)
**Fix:** persist a synthetic FILLED closing order (comment `margin_call_liquidation`)
like the option path, so the round-trip report explains the equity move.

### F5 — Option-lot liquidation fallback price = ENTRY premium — MEDIUM
`_liquidate_option_lot`: when the sparse cache has no premium bar at the liquidation
bar, the short is bought back at `lot.avg_price` — i.e. break-even — at exactly the
moment a margin breach implies the premium exploded against the position. This
understates the blow-up the margin path is modeling.
**Fix:** fall back to INTRINSIC via `_lot_strike_and_spot` (max(0, spot−strike) /
(strike−spot)), floored at the entry premium, before giving up.

### F6 — Per-bar O(orders × lots) scans on options runs — MEDIUM (perf)
For options runs, every bar's `snapshot_equity` → `_option_positions_mtm` →
`_option_group_bounds` does 2 full `get_orders()` passes plus ONE MORE full pass per
held lot (owner lookup), and the every-bar margin breach check calls `equity()` +
`maintenance_margin_requirement()` (each re-running those scans); the liquidation
loops recompute both per iteration. Daily-clock options runs tolerate it; a 5-min
options run won't.
**Fix:** memoize `(contract_group, group_bounds)` keyed on the order-cache generation
(the account already has `invalidate_order_cache` hooks to bust it), and compute
`equity()` once per check.

### F7 — Cash-secured caps leave `Transaction.quantity` stale — LOW
`_cap_single_leg_option_entry` and the debit-combo rescale mutate `order.quantity` /
leg quantities at FILL time but never touch the shared Transaction row → the
transactions table over-reports position size. Cosmetic in the BT (lots/round-trips
are order-derived), but worth a one-line `txn.quantity = capped` for consistency.

### F8 — Exercise-created shares = unmodeled free leverage — DESIGN QUESTION
Pre-existing but now more visible: a deep-ITM single-leg exercise converts to shares
at strike (e.g. −$32.5k cash on a $20k account) and LONG stock carries zero
maintenance in the new margin model, so deeply negative cash persists as free
leverage until some rule sells the shares.
**Options:** (a) cash-settle single-leg ITM expiries (intrinsic to cash, like the
combo unit settlement — simplest and consistent), (b) auto-sell exercised shares at
the next bar's open, or (c) add long-side maintenance + a negative-cash financing
cost. (a) changes covered-call assignment semantics the least if share delivery is
kept only where a share leg actually exists.

### F9 — Minor
- `DEFINED_RISK_*_STRATEGIES` contain `"debit_spread"`/`"credit_spread"` which no
  action ever emits (dead entries implying coverage that doesn't exist — drop or map).
- `maintenance_margin_requirement` silently `continue`s when a lot's strike can't be
  resolved — understates margin with no log line.
- `settle_defined_risk_combo_expiry` sets `txn.close_price = 0.0` (net payoff lost to
  the transaction row; round-trips carry it — cosmetic).

## Verified good
- **No lookahead:** option fills price at next-bar open by default (`_option_fill_price`
  mirrors the equity `_bar_for_fill`); expiry settles on the expiry bar's close.
- **Clamp is display-only:** MTM clamp never moves cash; realized P&L at expiry
  unaffected. Unit-settlement net payoff is bounded by construction (modulo F1/F3).
- **The netting fix** (`_multi_leg_positions`) correctly kills the repeated
  re-assignment blow-up, and synthetic closes link `depends_on_order` to protect
  entry-order resolution on shared transactions.
- **Defined-risk combos are never broken apart** by the liquidator.
- **Equity-only parity:** margin/clamp paths short-circuit on `self._options is None`;
  the full equity backtest suite is green post-merge.
- **Test coverage:** each historical blow-up has a dedicated regression test (7 new files).
