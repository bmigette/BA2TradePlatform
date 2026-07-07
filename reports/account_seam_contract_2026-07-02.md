# Account-seam contract (Phase 2 — live↔backtest engine unification)

Plan: `docs/plans/2026-07-02-live-backtest-engine-unification.md` (gaps #6, #12)
Status: 2026-07-07

## Purpose

The shared decision + order-flow code (`TradeActionEvaluator`, `TradeRiskManagement`,
`size_candidate_orders`, `TradeActions`) drives orders through an **AccountSeam** — the abstract
`AccountInterface` / `OptionsAccountInterface`. Two implementations satisfy it: the live broker
accounts (`AlpacaAccount`, `TastyTradeAccount`) and `BacktestAccount`. This document is the formal
behavioral contract both must honor, and — crucially — which clauses are **exact parity** vs
**documented approximations** (real broker friction the backtest deliberately models simply).

A parity bug is a clause where BacktestAccount and a live account *should* behave identically but
don't. An approximation is a clause where they intentionally differ and that difference is accepted.

## Exact-parity clauses (both sides must match)

| # | Clause | Verified (backtest) | Verified (live) |
|---|---|---|---|
| C1 | `submit_order` creates/attaches a WAITING transaction with the per-expert quantity (not the broker aggregate) | `test_backtest_account_contract.py`, `test_round_trip_trades.py` | `test_refresh_transactions_partial_fill.py`, `tests/test_accounts/*` |
| C2 | A MARKET entry fills and updates the cash/position ledger (weighted-avg cost, realized P&L on close) | `test_backtest_account_contract.py::test_position_ledger_weighted_average_and_realized_pl`, `test_market_order_fills_and_updates_ledger` | broker-owned; `test_refresh_transactions_partial_fill.py` |
| C3 | A protective SL/TP leg is created as a dependent WAITING_TRIGGER order off the entry, activated when the entry fills | `test_entry_bracket_engine.py`, `test_entry_bracket_seeding.py` | `test_trade_manager_triggers.py`, `test_tpsl_rebase_to_fill.py` |
| C4 | Dependent-leg price rebases to the actual fill price (TP/SL computed off fill, not the pre-fill estimate) | `test_daily_engine_stop.py` | `test_tpsl_rebase_to_fill.py` (`rebase_price_to_fill`) |
| C5 | CLOSE uses the transaction quantity + cleans up sibling WAITING_TRIGGER legs (no orphan OCO leg) | `test_round_trip_trades.py`, `test_option_close_multileg`-style | `test_option_close_multileg.py`, `test_reconcile_externally_closed.py` |
| C6 | Per-expert position/quantity accounting (a symbol held by two experts is tracked per-expert, not merged) | `BacktestAccount._positions` snapshot; `test_round_trip_trades.py` | `test_replacement_qty_gate.py` (`replacement_blocked_by_qty`) |
| C7 | Tighter-wins protective stop reconciliation applied identically where used | `test_reconcile_protective_stop.py` + `daily_engine` submit sites | live does NOT apply tighter-wins (documented — clause A5 below) |
| C8 | Sizing reads the same balance the account reports (`get_balance`) and prices (`get_instrument_current_price`) | `test_size_candidate_orders_parity.py` | `test_live_enter_path.py` |
| C9 | Only funded orders are persisted/submitted (temp-list flow: no qty=0 churn, no unfunded deletes) | `test_size_candidate_orders_parity.py`, `test_entry_equity_gate.py` | `test_live_enter_path.py` |

## Documented approximations (intentional, NOT unified away)

| # | Approximation | Rationale |
|---|---|---|
| A1 | **Fill timing**: BT fills at the NEXT bar open; live fills same-cycle at the broker | Time can't fast-forward live; the backtest's clock advances discretely. Modeled, documented. |
| A2 | **OCO precedence**: BT resolves SL before TP within a bar when both are touched | Deterministic worst-case; a real broker's intrabar order is unknowable from bar OHLC. |
| A3 | **Partial/limit fills**: BT fills MARKET fully; wash-trade delay disabled in BT | Real broker friction the backtest doesn't simulate; live handles via `refresh_orders`. |
| A4 | **WAITING_TRIGGER activation location**: live activates in `TradeManager._check_all_waiting_trigger_orders`; BT activates inside `BacktestAccount.refresh_orders` | Same *effect* (dependent leg goes live on fill), different owner — a contract test must drive each side's own activation path, not assume a shared `refresh_orders`. |
| A5 | **Tighter-wins stop**: applied in BT (C7), NOT in live | The live-policy change is intentionally out of scope until separately approved + paper-validated (see P1b commit). |
| A6 | **Early American assignment / expiry / margin call**: modeled by BacktestAccount, broker-owned live | No live analog to intercept; BT approximates. `test_option_assignment.py`. |
| A7 | **Eval-audit rows** (`TradeActionResult`): persisted live, skipped on the BT hot path | Observability only; no equity effect. Perf-sensitive. |

## Live-validation gap (honest)

The backtest suite + the mock-account live tests (`test_live_enter_path.py`) **cannot** exercise a
real broker's OCO cancel, partial fills, session/detachment, or the
`_check_all_waiting_trigger_orders` cascade against live infrastructure. The live-side clauses above
are covered by the live unit suite with mock accounts; the **temp-list enter-path rewrite (P1e
step 3) still requires a PAPER-ACCOUNT DRY-RUN** confirming brackets attach and no double-submission
before enabling on a funded live account.

## Follow-ups (not yet done)

- A single `account_contract_cases.py` parametrized over `(MockLiveAccount, BacktestAccount)` running
  C1–C9 as one suite (the plan's "both pass the same contract"). Today the clauses are verified by
  *separate* BT and live tests mapped above — equivalent coverage, not yet one parametrized module.
- C5/A4 asymmetry (activation owner) is the highest-value case to unify under P3 (the driver lift).
