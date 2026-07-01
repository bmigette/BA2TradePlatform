# Code Audit — 2026-07-01 (bugs, strategy flaws, live-vs-test parity)

Scope: order lifecycle (live `TradeManager`/`AccountInterface`/brokers vs backtest
`daily_engine`/`BacktestAccount`), Phase-6 shim/package integrity, risk-sizing plumbing
(incl. the new `use_atr_stop`), fill semantics, strategy/GA definitions in
`ba2test_launcher.py`.

## A. Live-platform bugs (actionable)

### A1. IBKRAccount.submit_order breaks the whole submit contract — HIGH (if IBKR used for trading)
`ba2_trade_platform/modules/accounts/IBKRAccount.py:290` defines
`submit_order(self, order)` — it **overrides the base template method** instead of
implementing `_submit_order_impl(...)`, and doesn't accept the interface kwargs
(`tp_price`, `sl_price`, `is_closing_order` — see
`packages/common/ba2_common/core/interfaces/AccountInterface.py:60`).
Consequences:
- `TradeManager.py:1205` (`submit_order(order, sl_price=...)`) and `TradeManager.py:307`
  (`submit_order(order, is_closing_order=...)`) **raise TypeError** on an IBKR account —
  caught by the surrounding try/except, so the order silently sticks in PENDING/ERROR.
- Even plain calls bypass ALL base-class logic: validation, transaction creation,
  wash-trade lock, and TP/SL bracket creation (`adjust_tp_sl`) never run for IBKR.

Fix: rename the body to `_submit_order_impl(self, trading_order, tp_price=None,
sl_price=None, is_closing_order=False)` (letting the base `submit_order` template run),
and either implement `adjust_tp/adjust_sl/adjust_tp_sl` or leave the base
NotImplementedError warnings to surface.

### A2. WASHTRADE_LOCKED re-submit loses the safeguard SL — MEDIUM-HIGH
When an entry order is wash-trade locked, `AccountInterface.submit_order` returns early
(`AccountInterface.py:151-162`) **before** `_submit_order_impl` and before the
`adjust_tp_sl/adjust_sl` bracket block — so no protective leg exists yet.
The unlock path (`TradeManager.py:307`) re-submits with
`submit_order(order, is_closing_order=is_closing)` — **without**
`sl_price=order.stop_price` — so a previously-locked entry fills with **no protective
stop**, even though the RM computed one (it's still sitting in `order.stop_price`).

Fix: in `_check_washtrade_locked_orders`, pass `sl_price=order.stop_price or None` for
non-closing entry orders (mirroring `TradeManager.py:1205`).

### A3. Live manage pass consumes recommendations of ANY subtype — MEDIUM (parity + correctness)
`TradeManager.process_open_positions_recommendations` (`TradeManager.py:1599-1619`)
selects the latest `ExpertRecommendation` per symbol in the lookback window **without
filtering `subtype`** (the model has `subtype: AnalysisUseCase`, models.py:120). A fresh
ENTER_MARKET rec created after the open-positions analysis will shadow it, so exit rules
are evaluated against an entry-thesis rec. The backtest always evaluates a **fresh
OPEN_POSITIONS** rec (`daily_engine._manage_open_positions`) — a live-vs-test divergence
AND a live correctness smell.

Fix: add `ExpertRecommendation.subtype == AnalysisUseCase.OPEN_POSITIONS` to the query
(or prefer OPEN_POSITIONS-subtype recs and fall back only when none exist).

### A4. UI `_place_order` path never passes tp/sl — LOW / possibly by design
`TradeManager._place_order` (`TradeManager.py:899`, called from
`ui/pages/marketanalysis.py:4842`) submits without `sl_price`, so manually-placed orders
carry no safeguard stop. If manual orders are meant to be fully manual, fine — otherwise
thread `order.stop_price` through like the auto-submit path.

## B. Live-vs-backtest divergences

### B1. Wash-trade lock is a half-modeled no-op in the backtest — LOW impact, confusing state
`BacktestAccount` inherits the lock check (an opposing working order marks the new order
WASHTRADE_LOCKED), but the fill engine's working set uses `get_active_statuses()` which
**includes** WASHTRADE_LOCKED (`backtest_account.py:647-654` — only WAITING_TRIGGER is
excluded), so a "locked" order **fills anyway** next bar. Net: BT behaves as if the lock
doesn't exist (≈ live-after-unlock, usually fine), but the status flag semantics are
violated mid-flight and nothing models live's delay/starvation.

Suggestion: override `_is_washtrade_lock_candidate -> False` in `BacktestAccount` with a
comment ("live-only broker friction, not modeled"), making the divergence explicit and
the state consistent.

### B2. Deliberate, documented divergences (OK, no action)
- BT manages OPENED transactions only vs live WAITING+OPENED
  (`daily_engine._held_transactions` — justified by the next-bar fill model).
- BT OCO same-bar ambiguity resolves SL-first (conservative); live lets the broker decide.
- Live manual TP/SL override locks (`tp_manual_override`/`sl_manual_override` in
  `AlpacaAccount._adjust_tpsl_internal`) aren't modeled in BT — no manual actor exists.
- Live TP-preservation on `adjust_sl` (re-issue OCO) is present in BOTH implementations
  (BT `backtest_account.py:1496-1522`, live `_adjust_tpsl_internal`) — parity confirmed.

### B3. Package integrity — confirmed good
- All in-tree `core/*` shims are clean 11-line `sys.modules` aliases (no drift possible).
- BOTH venvs (`trade`, `test`) resolve `ba2_common`/`ba2_providers` editable from this
  repo — single source of truth. Corollary: **uncommitted package edits are already
  "deployed"** to the running grid and to live on next restart (the `use_atr_stop` work
  is currently in this state — commit it).

## C. Strategy-design flaws (shared by both platforms — the GA can exploit them)

### C1. `adjust_stop_loss` has no ratchet guard — HIGH (strategy level)
`AdjustStopLossAction` (`TradeActions.py:1053`) and both account `adjust_sl`
implementations **replace** the stop with whatever the rule computes; the only guard is
minimum-distance (too-close), never "don't loosen". Two exploits/flaws follow:

1. **Safeguard override / risk-sizing mismatch:** the RM sizes the position off the
   safeguard stop distance (min(ATR×mult, risk%), floored at `min_stop_loss_pct`) and
   places that stop at entry. On the FIRST manage bar, the strategy's `exit_stoploss`
   rule (condition `has_position` — true every bar while holding) replaces it with the
   gene value (−3%…−20%). Sized for a 10% stop but stopped at −20% ⇒ realized loss at
   stop = **2× `risk_per_trade_pct`**. The GA is free to discover and exploit this
   (likely a contributor to outlier drawdowns, e.g. scr-mid-S1-aggr's −18.2% dd).
2. **Trailing stops un-trail:** the S2/S3 profit-lock tiers only hold while their
   `profit_loss_percent > X` condition is true. If price retreats below the tier, the
   tier stops firing but `exit_stoploss` still fires ⇒ the stop drops BACK to entry−X%.
   A trailing stop should never retreat; here it does, on both platforms identically.

Fix (single change, shared code): add ratchet-only semantics — for a long, never move
`transaction.stop_loss` DOWN once set (short: never up) — either always-on in
`_AdjustPriceLevelAction`/`adjust_sl`, or as an `only_tighten` flag the strategy rules
set. Alternative/complement: have RM sizing use the strategy's `exit_stoploss` distance
when that rule is enabled, so sizing and effective stop agree.

### C2. Once-per-day dedup can starve the entry pass — LATENT
`daily_engine` (~line 576-583): the first bar of a day where EITHER gate (entry/manage)
fires claims `(expert_id, date)` in `analyzed_days`; if entry and manage schedules pin
DIFFERENT `times`, the later pass never runs that day. Benign today (grid pins both at
09:30) — but a one-line trap for future schedule changes.

Fix: separate dedup sets per pass (`analyzed_entry_days` / `analyzed_manage_days`).

## D. Verified-clean areas (for the record)
- `use_atr_stop` plumbing: GA int 0/1 and live bool-JSON storage both coerce correctly
  through `get_setting_with_interface_default`; SmartRM + classic RM both gated; the
  FactorRanker/bypass/whatif `risk_per_trade_pct` reuses are ATR-free (no toggle needed).
- Live RM singleton is seeded with an indicator provider at startup
  (`seam_wiring.py:127`), and `TradeManager` uses that singleton — ATR sizing parity holds.
- BT entry orders carrying `stop_price` (safeguard) fill strictly by `order_type`
  (MARKET), never misread as stop orders (`_evaluate_fill`/`_trigger_thresholds`).
- Alpaca MARKET requests ignore `stop_price` — no accidental broker-side stop.
- ATR metric-store columns (7/14/21/28) exactly cover the `atr_period` gene range.
- `pnl_pct` is account-equity-relative by documented design (`backtest_account.py:1063`).
