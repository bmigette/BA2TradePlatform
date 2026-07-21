# Platform-Wide Bug Review — Live Trade App + Test Platform

**Date:** 2026-07-18
**Reviewer:** AI-assisted deep review (read-only; no fixes applied)
**Scope:** whole-platform bug-hunt beyond the senate work reviewed in
`reports/senate_basket_dispatch_review_2026-07-18.md` — both the live app (`ba2_trade_platform/`)
and the test app (`testplatform/`), plus the shared `packages/` libraries.

**Method (and its limits):** parallel review subagents were unavailable (provider quota), so this
pass is manual: pattern sweeps for the known bug classes (money-value fallbacks, raw SQL
bypassing the in-mem store, lookahead in expert as-of paths, unbounded per-instance memos in
live singletons) plus targeted deep reads of the flagged sites. Coverage details and the areas
NOT deep-reviewed are listed at the end — treat this as a risk-prioritized sweep, not a full audit.

---

## New findings

### P1 — MED: options buying-power reserve check is silently disabled in backtests — FIXED 2026-07-21

`OptionsAccountInterface.reserved_option_buying_power()`
(`packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:273-291`) sums pending
option reserves with a **raw** `select(TradingOrder).where(account_id, asset_class==OPTION)`.
During in-memory backtests, `TradingOrder` rows live in the thread-local in-mem store, so this
query silently returns `[]` → reserve = 0 → `available_option_buying_power()` (`:293-295`)
returns the FULL balance.

Reachability is real: `BacktestAccount(AccountInterface, OptionsAccountInterface)`
(`backtest_account.py:150`), and seven option-entry actions call
`self.account.check_option_buying_power(reserve)`
(`TradeActions.py:2182, :2267, :2450, :2519, :2588, :2655, :2776`).

**Effect:** in any options backtest, every `check_option_buying_power(reserve)` passes as if no
other pending option orders held reserves — the run can over-commit buying power and open
option entries that live (and the intended backtest semantics) would block. Silent backtest↔live
divergence that overstates strategy capacity. Same bug class as the senate report's M2 list;
add `:282` to that inventory. Pre-existing (TradingOrder has been in-mem since before the senate
changeset).

**Fix (2026-07-21):** routed `reserved_option_buying_power()` through `orders_where()` (the
dual-path in-mem/SQL helper already used by the rest of the account layer) instead of a raw
`select(TradingOrder)`. Regression test `tests/test_option_reserve.py::
test_reserved_option_buying_power_sees_inmem_orders` — confirmed RED (`0.0` instead of the
seeded `30000.0` reserve) against the pre-fix code, GREEN after. Motivated by the first
real options-optimization grid run (2026-07-20/21), so this class of over-commitment no longer
taints those results going forward.

### P2 — MED: FactorRanker bypass rebalances stamp backtest transactions with wall-clock dates

`FactorPortfolioManager._submit_buy` pre-creates the expert-attributed transaction with
`open_date=datetime.now(timezone.utc)` (`packages/experts/ba2_experts/FactorRanker/portfolio.py:323`;
`created_at` also defaults to real now). The backtest bypass path drives this exact code:
`daily_engine.py:1294` → `rebalance(targets)` → `_submit_buy`.

**Effect:** in a backtest, every FactorRanker transaction carries the REAL run date instead of
the simulated bar date. Anything comparing transaction dates to simulated time is poisoned:
`_oldest_entry_order`'s `min(txns, key=open_date...)` (`daily_engine.py:1240`), DaysOpened-style
conditions, date-ordered transaction queries, and end-of-run trade dates. FactorRanker's own
target-diff rebalance doesn't consume dates, which is likely why it went unnoticed — the blast
radius is the shared date-consuming machinery and reporting around it.

### P3 — LOW: FMP past-earnings fabricates `eps=0` for missing values

`FMPCompanyDetailsProvider.get_past_earnings`
(`packages/providers/ba2_providers/fundamentals/details/FMPCompanyDetailsProvider.py:649-656`):
`earning.get("eps", 0)` + `float(x) if x else 0` maps a MISSING actual/estimated EPS to `0.0`
instead of `None`/dropping the row. FactorRanker's SUE (`FactorRanker/factors.py:45`) then
computes `(0 - estimate)/std` — a real-looking signal from fabricated data; EarningsDrift's
surprise math consumes the same rows. Violates the repo's no-data-fallbacks rule. Rare (FMP
does omit eps fields on some rows), but silent.

### P4 — LOW: a configured `risk_per_trade_pct = 0` is silently rewritten to 1.0

`TradeRiskManagement.py:871`: `float(expert.get_setting_with_interface_default('risk_per_trade_pct', ...) or 1.0)`.
`0.0 or 1.0` → `1.0`, so an explicit zero (a plausible "no new risk / disable entries" value)
becomes 1%. The `or 0.0` for `min_stop_loss_pct` at `:874` is benign by comparison.

### P5 — LOW: `get_balance() or 0.0` money fallback

`OptionsAccountInterface.py:294`. A `None` balance (broker hiccup) becomes `0.0`, making
`check_option_buying_power` fail closed (blocks buys) — the safe direction, hence LOW, but
still the convention's fabricated-money pattern.

### P6 — LOW: partial-fill reconciliation books stop/limit price as the fill price

`TransactionHelper.py:134`: `fill_price = db_order.open_price or db_order.stop_price or db_order.limit_price`.
`open_price` is the real average fill price; when it's unset (partial fill tracked only via
`filled_qty`), the order's stop/limit is booked as the transaction's close price → wrong
realized P&L on the reconciled transaction. Live-only caller today
(`AlpacaAccount.reconcile_canceled_partial_fill` paths); rare race (cancel after partial fill).
Same method also does raw `session.get(TradingOrder/Transaction)` and a raw `select` at
`:115/:124/:161` — add to the latent in-mem-bypass inventory (no backtest caller today).

### P7 — Note (latent): FactorRanker SUE keeps negative `days_since`

`FactorRanker/factors.py:42` filters `days > drift_window_days` but not `days < 0`. Currently
unreachable because `get_past_earnings` excludes reports after `end_date`
(`FMPCompanyDetailsProvider.py:645`) — flagging so the guard isn't forgotten if another input
path ever feeds `earnings_surprise`.

---

## Verified clean in this pass

- **FMPEarningsDrift** — no lookahead: `datetime.now()` appears only in the explicitly
  live-only calendar-shortcut branch (`FMPEarningsDrift.py:222-245`); the backtest path goes
  point-in-time via the provider's `as_of`, and the freshness check has the future bound
  (`days_since < 0`, `:138`).
- **FMPRating** — backtest consensus/upgrades are reconstructed no-lookahead from dated
  history (`:321-374`); `datetime.now()` at `:361` is live-branch only; both recency filters
  bound above by `ref_date` (`:290`, `:314`).
- **FactorRanker data inputs** — `as_of` threaded through quality/PEAD inputs; the
  `_ohlcv_cache` at `data.py:219` is function-local (per call), not module state — no
  cross-trial leak.
- **TradeManager.py** — money-fallback pattern greps (`.get(k, <number>)`, `or 0.0/1.0` on
  price/qty/balance) came back clean.
- **GA/optimization surface (spot-check only)** — failed trials use the documented
  worst-case `fitness=-1e9` convention; input/prediction NaN guards exist in
  `backtest_handler.py:452-459, :541-546`. No confirmed bugs — but see coverage limits below.

## Inventory update (in-mem-bypass bug class)

Combined with the senate report, the full known list of raw SQL sites that silently see an
empty table when the backtest in-mem flag is on:

- Reachable in backtests today: `TradeRiskManagement.py:950`, `:1032`, `:1044`;
  `TradeActions.py:135`, `:1771-1782`, `:2923`, `:2957`; `AccountInterface.py:469`, `:750`;
  **`OptionsAccountInterface.py:282` (this pass, P1)**.
- Latent (no backtest caller today): `models.py:507` (`get_expert_id`);
  **`TransactionHelper.py:115/:124/:161` (this pass)**.

## Coverage limits (candid)

Deep-read this pass: non-senate experts (FMPRating, FMPEarningsDrift, FactorRanker core,
FinnHubRating surface), account-layer interfaces, targeted money-path greps across
`packages/common` and `TradeManager.py`, the options buying-power chain.
**Not deep-reviewed** (the subagent assignments that failed on quota): `TradeManager.py` /
`JobManager.py` / `WorkerQueue.py` full logic, `TradeActions.py` / `TradeConditions.py` line by
line, broker integrations (`AlpacaAccount`/`IBKRAccount`/`TastyTradeAccount`), backtest fill
engine internals (`backtest_account.py` fills/brackets/expiry), GA/distributed internals
(`genetic.py`, `distributed_eval.py`, `worker_server.py`), `PennyMomentumTrader` (live intraday
trader — heavy `datetime.now()` usage throughout, but its backtest reachability was not
established), the SmartRiskManager/TradingAgents LLM stack, all UI, and the 31K-line frontend.
A follow-up pass on those is worthwhile.

## Suggested priority

1. **P1** — route `reserved_option_buying_power` through `orders_where` (dual-path), like the
   other account-layer queries: silent wrong economics in every options backtest.
2. **P2** — stamp bypass-created transactions with the simulated `as_of` (needs a threaded
   date or account-level clock, not `datetime.now()`).
3. **P3/P4** — one-line convention fixes.
4. The latent inventory — worth one dedicated dual-path sweep closing out the class.
