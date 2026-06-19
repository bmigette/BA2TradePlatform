# Options (Put/Call) Backtesting — Design Spec

**Date:** 2026-06-15
**Status:** Approved (brainstorming) — ready for implementation plan.

## Goal

Run the **existing live option `TradeAction`s** inside the daily backtest engine with trustworthy, real-priced fills, driven by the strategy's automation rules — achieving **parity with the live options path**. The same strategy/ruleset that trades options live should backtest identically (within data limits).

## Architecture (one line)

A cache-backed `HistoricalOptionsProvider` (Alpaca-sourced, offline) feeds an **options-capable `BacktestAccount`** (implements `OptionsAccountInterface`); the existing `TradeAction` classes and ruleset path are reused unchanged; the fill engine is extended to price option legs off cached premium bars and to resolve expiry/exercise/assignment per bar.

## Tech stack

Python (FastAPI backend, `ba2_common`/`ba2_providers` packages), `alpaca-py` SDK for the offline options fetch, sqlite/parquet for the cache, existing DEAP optimizer + React/TS frontend for the action picker.

---

## Context — what already exists (audit, 2026-06-15)

The platform already models options **end-to-end for live trading**; only the backtest account is equities-only. Evidence (file:line):

- **Enums / actions** — `BA2TradeCommon/ba2_common/core/types.py`: `AssetClass{EQUITY,OPTION}` (240–242), `OptionRight{CALL,PUT}` (245–247), option `ExpertActionType`s (381–391: `BUY_CALL`, `BUY_PUT`, `SELL_COVERED_CALL`, `SELL_CASH_SECURED_PUT`, `BUY_PROTECTIVE_PUT`, `OPEN_BULL_CALL_SPREAD`, `OPEN_BEAR_PUT_SPREAD`, `OPEN_BEAR_CALL_SPREAD`, `OPEN_STRADDLE`, `OPEN_STRANGLE`, `CLOSE_OPTION`), `get_option_action_values()`/`is_option_action()` (489–523), option `ExpertEventType` flags (`F_HAS_COVERED_CALL`, `N_IV_RANK`, …, 341–368).
- **TradeAction classes** — `BA2TradeCommon/ba2_common/core/TradeActions.py`: `_OptionEntryAction` (1505) calls `self.account.get_option_chain(...)` (1565) and `self.account.submit_option_order(...)` (1681); concrete `BuyCallAction` (1713), `OpenBullCallSpreadAction` (1750), `BuyPutAction` (1805), `OpenBearPutSpreadAction` (1842), `SellCoveredCallAction` (1898), `BuyProtectivePutAction` (1937), `SellCashSecuredPutAction` (1976), `OpenBearCallSpreadAction` (2036), `CloseOptionAction`.
- **Account interface** — `BA2TradeCommon/ba2_common/core/interfaces/OptionsAccountInterface.py`: `get_option_chain` (25), `get_option_quote` (38), `get_atm_implied_volatility` (43), `get_option_positions` (49), `submit_option_order` (61–161), `close_option_position` (164), `get_iv_rank` (195–220).
- **Value objects** — `BA2TradeCommon/ba2_common/core/option_types.py`: `OptionContract` (10–42), `OptionQuote` (45–62), `OptionLeg` (66–76), `OptionPosition` (79–92).
- **Models** — `BA2TradeCommon/ba2_common/core/models.py`: `TradingOrder` option fields (484–493: `asset_class`, `contract_symbol`, `option_type`, `strike`, `expiry`, `underlying_symbol`, `multiplier`, `position_intent`, `option_strategy`); `Transaction.multiplier` (187) with multiplier-aware valuation (`get_current_open_equity` 314–316, `get_pending_open_equity` 393–397).
- **Live implementation** — `BA2TradePlatform/ba2_trade_platform/modules/accounts/AlpacaAccount.py`: dual inheritance `(AccountInterface, OptionsAccountInterface)` (66), `get_option_chain` (4537–4642), `get_option_quote` (4645–4687), `get_atm_implied_volatility` (4689–4711), multi-leg submit + close.

**The blocker** — `BA2TestPlatform/backend/app/services/backtest/backtest_account.py`: class is *"Equities-only v1"* (11), `class BacktestAccount(AccountInterface)` (95, NOT `OptionsAccountInterface`), `supports_options = False` (100). No options data provider exists in `BA2TradeProviders` (only OHLCV/fundamentals/news/indicators/screener).

## Data source decision

**Alpaca** (via `alpaca-py`) — already integrated live. Historical option **bars** (1Min–1Month) + **chains with IV/greeks** via `OptionHistoricalDataClient`; contract discovery via `GetOptionContractsRequest`; OCC symbol format `AAPL250117C00150000`. Constraints: **history starts Feb 2024** (hard floor); free "Basic" tier = *indicative* feed (modified quotes, latest delayed 15 min — irrelevant for historical bars); OPRA feed (~$99/mo) only needed for true bid/ask precision. **FMP is not used** (its options are snapshot/recent, not deep historical). Deep pre-2024 history would require a dedicated vendor (ORATS/Polygon/CBOE) — out of scope for v1.

## Locked decisions

| Decision | Choice |
|---|---|
| v1 strategy scope | **Parity with live** — all option actions incl. multi-leg (spreads, straddle/strangle) |
| Driver | **Strategy rules (ruleset actions)** — no new expert |
| Fill model | **Bar-based** off cached per-contract premium bars (`fill_model`: next_bar_open / same_bar_close), × multiplier, ± slippage |
| Expiry/assignment | **Realistic at-expiry exercise/assignment** → share positions; OTM worthless. **Early American deferred** |
| Architecture | **A — cache-backed, real Alpaca bars** |
| Data source / window | Alpaca, **Feb-2024+** |

---

## Components

### 1. Options cache + `HistoricalOptionsProvider`
Mirror the screener-history cache pattern (`BA2TestPlatform/backend/app/services/backtest/universe_resolver.py` + `ba2-test fetch-screener`): build offline, read-only at backtest time, **fail-fast on miss** (`OptionsCacheMiss`), never live-fetch mid-bar.
- **CLI** `ba2-test fetch-options --underlyings <list|@file> --start <d> --end <d> [--interval 1d] [--feed indicative|opra]` — pulls, per underlying: chain snapshots (per trading day or per the run cadence) with IV/greeks/quote, and per-contract premium bars over each contract's life within the window.
- **Storage** under the cache dir, keyed `(underlying, date)` → chain, `(occ_symbol, date)` → bar. parquet or sqlite (match the screener cache's choice).
- **Provider** (new, `ba2_providers` or test-platform `dataproviders/`): `get_chain(underlying, as_of, filters)`, `get_quote(occ_symbol, as_of)`, `get_bar(occ_symbol, as_of)`, `get_atm_iv(underlying, as_of)`, `iv_history(underlying)`. **As-of clamped** (returns nothing dated after `as_of`), analogous to `AsOfClampedOHLCVProvider`. Injected into the account via a seam.

### 2. Options-capable `BacktestAccount`
`class BacktestAccount(AccountInterface, OptionsAccountInterface)`, `supports_options = True`. Implement all `OptionsAccountInterface` methods against the provider:
- `get_option_chain` → provider chain filtered (the live signature: expiry/strike/type filters); returns `OptionContract`s (with IV/greeks for selection).
- `get_option_quote`, `get_atm_implied_volatility`, `get_iv_rank` → provider.
- `get_option_positions` → option `Transaction`s for the expert.
- `_submit_option_order_impl(trading_order, legs)` → create option `TradingOrder` legs + parent and the option `Transaction`(s) using the existing fields (`contract_symbol`/`strike`/`expiry`/`option_type`/`multiplier=100`/`position_intent`/`option_strategy`); reuse the linking logic from `OptionsAccountInterface.submit_option_order` (61–161) where possible.
- `close_option_position` → opposite-intent closing order.

### 3. Fill engine extension (`refresh_orders` / fill helpers, `backtest_account.py` 726+)
- **Leg fill** = the option's cached premium bar resolved per `fill_model` (next_bar_open/same_bar_close), price × `multiplier` (100), ± slippage; commission per contract per leg.
- **Multi-leg** = one parent + N leg orders, filled **together** (all-or-none on the same bar) at the net debit/credit; reuse the OCO/dependent-leg machinery conceptually.
- **Marking** = open option positions valued at the contract's premium **close** × multiplier each bar (`snapshot_equity` uses this; valuation already multiplier-aware in `Transaction`).

### 4. Expiry / exercise / assignment (per-bar lifecycle)
Add a per-bar pass (near `_apply_initial_brackets`, `daily_engine.py` ~503, and the new bypass-stop pass): for each open option `Transaction` with `expiry <= as_of`:
- **OTM** → expire worthless (premium → 0, realize P&L, close transaction).
- **ITM long** → exercise: call → buy 100×qty shares @ strike (cash out); put → sell 100×qty shares @ strike. Create/adjust an **equity** `Transaction`.
- **ITM short (assigned)** → covered call: 100×qty shares called away (sell @ strike); CSP: buy 100×qty shares @ strike.
- Resulting share positions flow through the existing equity ledger. Settlement price = strike; mark uses the equity OHLCV thereafter.
- **Early American assignment: NOT modeled in v1** (documented limitation).

### 5. Strategy/ruleset action wiring + UI
- The backtest already seeds rulesets from the enter/exit condition trees (`rules_tree_json` / `seed_ruleset_from_tree`). Ensure **option actions** are available as rule actions and execute via the options-capable account (the `TradeAction` classes already exist).
- **Frontend** (`Backtesting.tsx` Strategy section): the exit/RM **action picker** gains the option actions + their selection params (target delta, DTE, %OTM, spread width). These params join the **optimize** param-space (numeric, Opt-toggleable) like other rule operands.

### 6. Trust / error handling
- `OptionsCacheMiss` fail-fast (no silent skip, no live fetch) — prevents the lookahead/survivorship traps from the trust audit.
- All chain/quote/bar reads **as-of clamped** to the engine clock.
- Validate the backtest date range is **≥ 2024-02-01** for option strategies; clear error otherwise.
- Round-trip P&L is **multiplier-aware** and pairs via the side-based logic (the 2026-06-15 pairing fix).

### 7. Data flow (per bar)
clock=as_of → universe → expert equity signal (unchanged) → ruleset eval → an option action fires → `TradeAction` calls `account.get_option_chain(as_of)` [cache, clamped] → selects contract(s) by delta/DTE/%OTM → `submit_option_order` → leg orders staged → next bar fills off cached premium bars → positions marked each bar off premium close → at expiry, exercise/assignment → equity ledger. Lookahead-free throughout.

### 8. Testing
- **Pure units**: contract selection; leg fill pricing (×multiplier, slippage, multi-leg net debit/credit); expiry/exercise/assignment payoff math (ITM/OTM × call/put × long/short → resulting share position + cash); `get_iv_rank`.
- **Engine e2e**: a small fixture options cache (1–2 underlyings, a few contracts) + a strategy rule that buys a call and one that sells a covered call → assert fills, per-bar marking, expiry outcome, and the equity ledger after assignment.
- **Real-data smoke** (a Feb-2024+ underlying) gated on Alpaca API keys (skipped without them).

---

## Phasing (drives the implementation plan)

1. **Data layer** — options cache schema + `HistoricalOptionsProvider` (as-of clamped, fail-fast) + `ba2-test fetch-options` CLI. Unit-tested against a fixture cache.
2. **Account (single-leg)** — `BacktestAccount` implements `OptionsAccountInterface`; single-leg `submit_option_order`, bar-based leg fills, multiplier-aware marking.
3. **Lifecycle + multi-leg** — per-bar expiry/exercise/assignment → equity ledger; multi-leg (spreads, straddle/strangle) parent+legs fills.
4. **Wiring + UI** — option actions available in the ruleset/exit-action path; frontend action picker + selection params; optimize param-space integration.
5. **Trust hardening + e2e** — as-of clamp + `OptionsCacheMiss` + date-window validation + multiplier-aware round-trips; full e2e tests.

## Out of scope / future

- Early American assignment (model at-expiry only in v1).
- Pre-2024 history / deep options surface (would need ORATS/Polygon/CBOE).
- OPRA bid/ask quote-based fills (v1 uses bar-based; OPRA optional later for spread realism).
- A dedicated options-emitting expert (options are rule-driven in v1).
