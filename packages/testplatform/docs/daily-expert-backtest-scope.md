# Daily Expert Backtest Engine - Scope & Caveats (Phase 2)

This note documents the scope, guardrails, and known caveats of the Phase-2 daily
multi-asset expert backtest engine that ships in `backend/app/services/backtest/`.
It complements the Backtesting UI and the `Backtest` model (`backend/app/models/backtest.py`).

## What this engine is

The daily expert engine reuses the **real packaged ba2trade decision/order path** (from
the `ba2_common` / `ba2_providers` / `ba2_experts` editable packages) against a simulated
`BacktestAccount`, with NO live broker. Per bar (one calendar day) it:

1. runs each enabled expert's `analyze_as_of(as_of, context)` -> `Recommendation`;
2. maps each recommendation to a persisted `ExpertRecommendation` row in the separate
   backtest trading DB;
3. drives `TradeActionEvaluator` (the enter/exit `TradeConditions` ruleset) to create
   PENDING orders, then `TradeRiskManagement.review_and_prioritize_pending_orders` to
   size (classic notional / risk-based ATR) and submit them to the `BacktestAccount`;
4. fills next-bar with fees + slippage and per-bar TP/SL/stop + OCO handling.

Results are converted into the shared `Backtest` results row (metric columns + JSON
`equity_curve` / `drawdown_curve` / `trades` blobs) consumed by the existing UI.

## Two engines, one table (`engine_type` discriminator)

The `backtests` table carries both engines. Migration
`backend/db_migrate/013_backtest_model_id_nullable_engine_type.py`:

- makes `model_id` **nullable** (daily expert runs are NOT model-driven, so they store
  `model_id = NULL`; the legacy ML path always sets a real `model_id`); and
- adds `engine_type VARCHAR DEFAULT 'ml'` with values:
  - `ml` - legacy model-driven `backtesting.py` runs (the original engine), and
  - `daily_expert` - the Phase-2 daily multi-asset expert engine.

The `POST /api/backtests/daily` route stamps `engine_type='daily_expert'`; everything else
defaults to `'ml'`. The UI (`frontend/src/pages/Backtesting.tsx`) and the dashboard
`recentActivity` branch on `engineType` to label which engine produced each run. `to_dict()`
emits it as `engineType`.

## Scope (v1)

- **Equities only.** The account implements `AccountInterface` (the same surface
  `AlpacaAccount` implements), NOT `OptionsAccountInterface`. No options/futures/FX.
- **Daily cadence only.** One bar = one calendar day; intrabar order interactions are
  resolved conservatively (e.g. when a single bar's range straddles both the TP and SL of
  an OCO leg, the stop/loss side fills).
- **Classic risk management only.** Sizing is classic notional or risk-based ATR via the
  packaged `TradeRiskManagement` + `position_sizing`; no advanced/portfolio RM.
- **Separate backtest trading DB.** The expert/order/transaction rows live in the
  `ba2_common` `configure_db()` SQLite DB; the `Backtest` *results* row lives in the host
  `SessionLocal` DB. Two distinct databases.
- **Validated clean experts.** First runs target the no-LLM experts `FMPEarningsDrift` and
  `FMPInsiderClusterBuy`.

## Known caveats

### Consensus-endpoint lookahead (any later FMPRating run)

`FMPRating` (and any expert that reads FMP's *consensus*/analyst-estimate endpoints) can
exhibit **lookahead bias** in a historical backtest: those endpoints return the *current*
consensus, not the consensus as it stood on the `as_of` date. FMP does not expose a
point-in-time consensus snapshot, so a backtest of `FMPRating` over past dates may see
consensus values that did not yet exist on those dates. This is documented in the Phase-1
expert refactor (FMPRating treats SKIP as first-class and the consensus lookahead is called
out there). **Do not treat `FMPRating` backtest results as leak-free** until a
point-in-time consensus source is wired. The clean v1 experts (`FMPEarningsDrift`,
`FMPInsiderClusterBuy`) read point-in-time-safe endpoints (earnings/insider transactions
keyed by report/transaction date).

### Multi-asset buy & hold benchmark

`buy_hold_return` is `0.0` for daily expert runs in v1 - a proper multi-asset B&H benchmark
needs the full universe reconstruction that is deferred to a later phase. Single-asset ML
runs keep their existing benchmark behaviour.

### Per-fill vs round-trip P&L

`BacktestAccount.get_filled_trades` currently returns per-FILL rows; a true entry->exit
round-trip P&L join (so `win_rate` / `profit_factor` reflect realized round trips) is a
later refinement. `total_return` is computed from net liquidating value (including
mark-to-market of open positions), so it is correct even before that join lands.
