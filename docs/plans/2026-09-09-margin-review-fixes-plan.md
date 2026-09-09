# Margin review fixes (2026-09-09) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Resolve the actionable findings of `reports/margin/margin_tp_sl_review_2026-09-09.md` (findings 1, 3, 5, plus the exposure consequence of finding 2) and implement the user-confirmed plan `docs/plans/2026-09-09-margin-live-backtest-parity.md` (steps 1-7) WITHOUT changing any unlevered backtest output, pinning the deliberately-deferred findings (6 and 4) as strict-xfail parity fixtures.

**Architecture:** Leverage stays live-only. Every change is either (a) reachable only when `margin_enabled` is True on the account (backtests always run with it off, so they are byte-identical by construction, not by luck), or (b) a pure refactor whose old numerical behaviour is pinned by a test before the refactor. One shared function, two callers — never a live fork. The equity golden run (`testplatform/backend/tests/backtest/test_equity_golden_run.py`) is the compatibility gate: its fingerprint must be unchanged at the end.

**Tech Stack:** Python 3.11 (`C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe`), pytest, SQLModel. Shared code lives in `packages/common/ba2_common` (source of truth; in-tree `ba2_trade_platform/core/*.py` files of the same name are re-export shims). Live-only code (Smart RM, brokers) lives in `ba2_trade_platform/`.

**Worktree:** `C:\Users\basti\Documents\dev\BA2-margin`, branch `fix/margin-review-2026-09-09` (from `dev` @ 51234531).

---

## Non-negotiable project rules (read before every task)

1. **No `.get(key, default)` on config/settings** — explicit access; missing config must surface. (`get_setting_with_interface_default(key, log_warning=False)` is the sanctioned reader.)
2. **No fallback values for prices, balances, quantities, multipliers.** `None`/NaN means unknown → raise `ValueError` or return an explicit refusal; never substitute a number.
3. **No silent failure.** Every branch of a decision maps to an action or a loud refusal (ERROR log + error string / raise).
4. **Backtest outputs must not change.** `margin_enabled` is False in every backtest. Anything new that could alter sizing must be unreachable with margin off. Prove it with a call-counter test where the plan says so.
5. **Logging:** `from ba2_common.logger import logger` in packages, `from ba2_trade_platform.logger import logger` in-tree. `exc_info=True` only inside `except`. `ba2_common.logger` sets `propagate=False`: pytest `caplog` does NOT see records — assert logs by monkeypatching the module's `logger.warning/error/info` (pattern: `packages/common/tests/test_account_seams.py`).
6. **Running tests** (from the worktree root unless stated):
   - `PY=C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe`
   - packages: `$PY -m pytest packages/common/tests/<file> -q -p no:cacheprovider`
   - root: `$PY -m pytest tests/<file> -q -p no:cacheprovider`
   - backend: `cd testplatform/backend && $PY -m pytest tests/backtest/<file> -q -p no:cacheprovider`
   - `packages/common/tests` and `tests/` CANNOT share one invocation (conftest clash). NEVER run two pytest invocations concurrently in the same worktree.
7. **Commit after each task** on the branch. Commit messages end with:
   ```
   Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
   Claude-Session: https://claude.ai/code/session_019YvcXA7kBWFVimxiNSSnNX
   ```
8. Do not edit `reports/margin/*` or `docs/plans/2026-09-09-margin-live-backtest-parity.md` except where Task 6 says so.

---

## Verified code map (line numbers at 51234531)

| What | Where |
|---|---|
| Pure margin helpers `margin_factor_error`, `effective_factor_for`, `tradable_balance_for`, `over_exposure_threshold` | `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:24-80` |
| `_stock_multiplier_from` (accepts NaN — finding 5), `_buying_power_from`, `_plain_balance`, `_effective_factor`, `_tradable_balance`, `effective_margin_factor[_from]`, `get_tradable_balance`, `get_option_tradable_balance` | same file `:344-560` |
| `AccountSnapshot` (fields `cash, equity, net_liquidation, buying_power, option_buying_power, margin_multiplier, long_market_value, short_market_value (NEGATIVE while short), ...`) | `packages/common/ba2_common/core/account_types.py:47` |
| `get_account_snapshot()` default builder from `get_account_info()` (`_first("long_market_value")`, short forced negative) | `ReadOnlyAccountInterface.py:200-290` |
| Expert `get_virtual_balance` (tradable × pct) | `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:850-880` |
| Expert `get_available_balance` (virtual − used, clamp to broker BP via `_get_actual_available_balance`) | same `:900-960`, clamp helper `:963-995` |
| Expert `_calculate_used_balance` (profitable at cost, losing at cost+loss — finding 2) | same `:1040-1140` |
| Expert setting defs `sizing_mode`, `risk_per_trade_pct`, `atr_risk_budget_pct` | same `:140-175` |
| `AccountInterface.submit_order` (validate → transaction → impl → TP/SL legs) | `packages/common/ba2_common/core/interfaces/AccountInterface.py:212-…` |
| `_validate_trading_order` (calls the validators, returns `{'is_valid', 'errors'}`) | same `:853` |
| expert available-balance validator (new vs adding-to-position discrimination via `_get_transaction_entry_order`) | same `:1140-1234` |
| `_validate_position_size_limits` (equity × effective factor × pct) | same `:1236-1340` |
| `BacktestAccount.submit_order` → `super().submit_order(...)` (so every shared validator RUNS in backtests) | `testplatform/backend/app/services/backtest/backtest_account.py:2085` |
| `BacktestAccount.get_balance()` = CASH (finding 6); `get_account_info()` publishes `multiplier 1.0`, `buying_power=max(cash,0)`, no market values | same `:1821-1833`, `:1870-1880` |
| Classic RM `_size_prioritized_orders` (per-instrument cap = available × ratio — finding 4) | `packages/common/ba2_common/core/TradeRiskManagement.py:330-370` |
| Classic RM `_risk_atr_quantity` (budget resolution lines 1330-1333) | same `:1310-1365` |
| Smart RM `_auto_size_by_risk` (reads `risk_per_trade_pct` — finding 1) | `ba2_trade_platform/core/SmartRiskManagerToolkit.py:1870-1935` |
| Smart RM explicit-qty stop synthesis (`derive_stop_for_quantity` with `risk_per_trade_pct`) | same `:2005-2030` |
| `compute_risk_based_quantity`, `derive_stop_for_quantity`, `synthesize_safeguard_stop`, `get_latest_atr` | `packages/common/ba2_common/core/position_sizing.py` (in-tree shim: `ba2_trade_platform/core/position_sizing.py`) |
| Store helpers `orders_where(account_id=…, …)`, `transactions_where(...)` | `packages/common/ba2_common/core/trade_store.py:208, 270` |
| Existing test patterns: live-shaped fake account + real expert | `tests/test_margin_expert_sizing.py`, `tests/test_available_balance_clamp.py` |
| Smart RM toolkit built bare with `object.__new__` | `tests/test_smart_rm_atr_sizing_seam.py` |
| Real BacktestAccount harness (`_acct()`, `backtest_trading_db`, `wire_backtest_seams`, `AsOfPriceSource.load_bars`) | `testplatform/backend/tests/backtest/test_backtest_account_contract.py` |
| Equity golden fingerprint (results-identity gate) | `testplatform/backend/tests/backtest/test_equity_golden_run.py`, `golden/equity_golden_run.json` |
| CI parity gate | `.github/workflows/parity-and-coverage.yml` |
| Review probe (records PRE-fix behaviour, asserts the defects) | `reports/margin/reproduce_margin_review.py` |

Baseline freeze (plan step 1) was run BEFORE any change: `reports/margin/baseline_goldens_2026-09-09.txt` (golden fingerprints + targeted suites). The final gate re-runs it and diffs.

---

### Task 1: Reject non-finite broker figures (finding 5)

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` (`_stock_multiplier_from`, `_buying_power_from`, `_plain_balance`; and the option buying-power reader used by `get_option_tradable_balance` if it has its own `_from` helper)
- Test: `packages/common/tests/test_margin_finite_inputs.py` (new)

**Step 1: Write the failing tests** — build the account bare (`object.__new__(ReadOnlyAccountInterface)` is abstract; use the same `_Account` trick the existing `packages/common/tests/test_margin_tradable_balance.py` uses — read it first and reuse its fixture style). Cases:
- `margin_multiplier=float("nan")` → `_stock_multiplier_from` raises `ValueError` mentioning "finite".
- `margin_multiplier=float("inf")` → raises.
- `buying_power=float("nan")` → `_buying_power_from` raises.
- `get_balance()` returning `float("nan")` → `_plain_balance` raises; also with `margin_enabled=False` (`get_tradable_balance` must refuse, not return NaN).
- Regression: `get_tradable_balance()` with snapshot `margin_multiplier=nan`, factor 1.8, balance 10 000 raises (the review's $18 000 reproduction must no longer be possible).
- Legit zeros stay legit: `buying_power=0.0` → returns 0.0; `get_balance() == 0.0` → 0.0 (a measured zero is not unknown).

**Step 2: Run** `packages/common/tests/test_margin_finite_inputs.py` → FAIL (NaN passes today).

**Step 3: Implement** — in each reader, after `float(x)`, `if not math.isfinite(v): raise ValueError(f"account {self.id} ({type(self).__name__}) published a non-finite <figure> ({x!r}); cannot size with it")`. Keep existing None/<=0 messages. `math` is already imported in the module.

**Step 4: Run** the new file + `packages/common/tests/test_margin_accessors.py test_margin_snapshot.py test_margin_tradable_balance.py` → PASS.

**Step 5: Commit** `fix(margin): refuse non-finite broker multiplier / buying power / balance (review finding 5)`.

---

### Task 2: One sizing-budget resolver for classic and Smart RM (finding 1)

**Files:**
- Modify: `packages/common/ba2_common/core/position_sizing.py` — add `resolve_sizing_risk_budget_pct(get_setting) -> float`
- Modify: `packages/common/ba2_common/core/TradeRiskManagement.py:1330-1333` — call the resolver
- Modify: `ba2_trade_platform/core/SmartRiskManagerToolkit.py:1885` and `:2012` — call the resolver
- Check: `ba2_trade_platform/core/position_sizing.py` shim re-exports the new name (if it enumerates names, add it)
- Test: `packages/common/tests/test_position_sizing.py` (append), `tests/test_smart_rm_sizing_budget.py` (new)

**Step 1: Pin the classic behaviour FIRST** (before touching TradeRiskManagement). Resolver contract — must reproduce `TradeRiskManagement._risk_atr_quantity` lines 1330-1333 exactly:
```python
def resolve_sizing_risk_budget_pct(get_setting) -> float:
    """The %-of-equity DOLLAR-RISK budget for risk-based sizing, for BOTH risk managers.

    get_setting(key) -> the expert's setting value (typically
    ``functools.partial(expert.get_setting_with_interface_default, log_warning=False)``).
    Prefers ``atr_risk_budget_pct``; when that is None falls back to ``risk_per_trade_pct``
    (which ALSO sets the stop DISTANCE in the classic RM -- one gene doing two jobs was the
    2026-08-16 defect). ``float(x or 1.0)`` semantics are kept on purpose: it is the classic
    RM's historical behaviour and every backtest result depends on it.
    """
    budget = get_setting("atr_risk_budget_pct")
    if budget is None:
        budget = get_setting("risk_per_trade_pct")
    return float(budget or 1.0)
```
Tests (append to `packages/common/tests/test_position_sizing.py`): budget set → budget; budget None → risk_per_trade; both None → 1.0; budget 0 → 1.0 (documented quirk, pinned); budget "0.5" string → 0.5. Then a classic pin: build the same mock expert/account as `reports/margin/reproduce_margin_review.py` (virtual 18 000, price 100, SL 95, `atr_risk_budget_pct=0.5`, `risk_per_trade_pct=5`) and assert `TradeRiskManagement()._risk_atr_quantity(...) == 18` BEFORE the refactor (must pass at HEAD), and stays 18 after.

**Step 2: Run** → resolver tests FAIL (not defined), classic pin PASSES.

**Step 3: Implement** the resolver; replace the three lines in `_risk_atr_quantity` with `risk_pct = resolve_sizing_risk_budget_pct(functools.partial(expert.get_setting_with_interface_default, log_warning=False))` keeping the surrounding comment (trim it to point at the resolver). Run the classic pin + `packages/common/tests/test_position_sizing.py` → PASS.

**Step 4: Smart RM failing test** `tests/test_smart_rm_sizing_budget.py`: reuse `_make_toolkit` shape from `tests/test_smart_rm_atr_sizing_seam.py` with settings `atr_risk_budget_pct=0.5, risk_per_trade_pct=5.0, use_atr_stop=False, max_virtual_equity_per_instrument_percent=100`, virtual 18 000, available 18 000, price 100, `sl_price=95` → assert `_auto_size_by_risk(...)["quantity"] == 18` (today 180). Second test: budget unset → 180 (fallback unchanged). Third test for the explicit-quantity path: call `_open_position_internal`-equivalent stop synthesis — easiest is to test `derive_stop_for_quantity` is invoked with the RESOLVED budget: monkeypatch `ba2_trade_platform.core.SmartRiskManagerToolkit.derive_stop_for_quantity`? It is imported inside the function from `.position_sizing`; monkeypatch `ba2_trade_platform.core.position_sizing.derive_stop_for_quantity` to capture `risk_pct` and assert 0.5, not 5.0. If wiring the whole `_open_position_internal` is impractical (DB), extract the small block into a helper method `_synthesize_stop_for_explicit_quantity(symbol, quantity, order_direction)` and unit-test that.

**Step 5: Implement** in SmartRiskManagerToolkit both sites; run `tests/test_smart_rm_sizing_budget.py tests/test_smart_rm_atr_sizing_seam.py` → PASS.

**Step 6: Commit** `fix(smart-rm): size and synthesize stops from the shared sizing-budget resolver (review finding 1)`.

Note for the reviewer: the classic RM also scales `risk_pct` by `_regime_scale(expert, 'regime_risk_scale')`; Smart RM does not. That is out of scope here — record it in the task's commit body as a known remaining divergence, do not fix silently.

---

### Task 3: Account stock-exposure headroom: clamp + entry gate + per-account lock (findings 3 and 2)

Scope rule: **everything in this task is reachable ONLY when `margin_enabled` is True.** With margin off `get_stock_exposure_headroom()` returns `None` without reading the snapshot, the expert clamp is skipped, the gate is skipped. A call-counter test proves it.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` — pure `stock_exposure_headroom(ceiling, gross_exposure, pending_entries)` next to the other pure helpers; method `get_stock_exposure_headroom(exclude_order_id=None) -> Optional[float]`; helper `_pending_stock_entry_notional(exclude_order_id)`.
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:936-951` — clamp available to headroom (after the BP clamp).
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py` — `_validate_account_exposure(trading_order)` called from `_validate_trading_order` for non-closing orders; per-account `threading.RLock` held in `submit_order` across validation + `_submit_order_impl`.
- Test: `packages/common/tests/test_stock_exposure_gate.py` (new), `tests/test_margin_exposure_clamp.py` (new, uses the `tests/test_margin_expert_sizing.py` fixtures/factories).

**Semantics (write these into docstrings):**
- ceiling = `_plain_balance() × _effective_factor(...)` from ONE snapshot (reuse the `get_tradable_balance` shape: balance first, then one `get_account_snapshot()`). ceiling is literally `get_tradable_balance()` — compute both from the same snapshot in one private method to avoid a second TastyTrade round trip.
- gross exposure = `snapshot.long_market_value + abs(snapshot.short_market_value)`. Either `None` → raise `ValueError("account … published no long/short market value; cannot measure exposure")`. Non-finite → raise (Task 1 style).
- pending entries = Σ over this account's BROKER-WORKING stock ENTRY orders (excluding `exclude_order_id`) of `remaining_qty × (limit_price or current price)`. Definition of "broker-working entry": `orders_where(account_id=self.id)` rows with `broker_order_id` set, status in the non-terminal working set (NEW, PENDING_NEW, ACCEPTED, OPEN, PARTIALLY_FILLED, ACCEPTED_FOR_BIDDING, PENDING_REVIEW, CALCULATED — read `OrderStatus` in `types.py:64` and choose deliberately; write the set as a module constant with a comment), `depends_on_order is None` (protective legs are not entries), not an option order (`multiplier` in (None, 1) / no option fields — check how `_calculate_used_balance` discriminates options and reuse), and whose side equals its transaction's side (a reducing order has the opposite side). remaining_qty = `quantity - (filled_quantity or 0)` if the model has a filled-quantity field, else `quantity`. Price for a market order: `get_instrument_current_price(symbol)`; `None`/`<= 0` → raise (no fabricated notional).
- `stock_exposure_headroom = ceiling - gross - pending` (may be negative; never clamped here).
- Expert clamp (`get_available_balance`): after the BP clamp, `headroom = account.get_stock_exposure_headroom(exclude_order_id=None)`; `None` → nothing; else if `headroom < available_balance` → `available_balance = headroom` with an INFO log naming ceiling/gross/pending. A `ValueError` from the headroom read propagates to the existing `except` (which returns `None` = "cannot size") — that is the loud refusal, keep it.
- Gate (`_validate_account_exposure`): only for orders that OPEN or ADD (same discrimination as the expert balance validator: skip when `is_closing_order`, skip when the order's side is opposite to an existing position/transaction side, skip option orders). `headroom = self.get_stock_exposure_headroom(exclude_order_id=trading_order.id)`; `None` → no error. `notional = quantity × (limit_price or current price)`; if `notional > headroom` → append error `"Account {id} stock exposure ceiling: order ${notional} exceeds remaining headroom ${headroom} (ceiling ${ceiling} = balance × factor {f}; gross ${gross}; pending ${pending}). Reduce exposure or raise margin_factor."` and `logger.error`. A `ValueError` from the read → error string "Cannot validate account exposure … refusing" (same pattern as the equity branch of `_validate_position_size_limits`).
- Lock: `_submit_locks: Dict[int, threading.RLock]` class-level with a guard lock, `self._submit_lock()` returns this account id's RLock (getattr-style, no `__init__` dependency — see the `_non_marginable_warned` comment). `submit_order` wraps from `_validate_trading_order` through `_submit_order_impl` in `with self._submit_lock():`. RLock because leg submission re-enters.

**Step 1: Failing tests** `packages/common/tests/test_stock_exposure_gate.py` (bare fake account subclass of `AccountInterface` with stubbed abstracts, fixed snapshot, `orders_where` monkeypatched or the in-mem store via `trade_store.inmem_trades()`):
1. pure fn: `stock_exposure_headroom(18000, 19800, 0) == -1800`, with pending.
2. margin off → `get_stock_exposure_headroom()` is `None` and `get_account_snapshot` was NOT called (counter).
3. review finding 2 numbers: equity 11 800, factor 1.8, multiplier 2, long MV 19 800, no pending → headroom 1 440.
4. review finding 3 numbers: equity 10 000, long MV 18 000, broker BP 2 000 → headroom 0; a BUY 10 @ 100 entry is refused with the ceiling error; a SELL that reduces an existing long is NOT refused; `is_closing_order=True` never refused.
5. pending counted: one working entry order (broker_order_id set, NEW) for 50 @ 100 → headroom drops by 5 000; the order under validation itself is excluded via `exclude_order_id`; a protective leg (`depends_on_order` set) is not counted; a FILLED order is not counted.
6. `long_market_value=None` → `get_stock_exposure_headroom` raises ValueError; the gate turns it into a "cannot validate … refusing" error (order NOT submitted).
7. concurrency: two threads submit BUY 100 @ 100 on an account with headroom 12 000; fake `_submit_order_impl` sleeps 50 ms and then adds the notional to the fake snapshot's long MV; exactly one succeeds, the other is refused by the ceiling error.
8. lock is re-entrant: `_submit_order_impl` calling `self.submit_order(...)` for a leg does not deadlock.

`tests/test_margin_exposure_clamp.py` (real `MarketExpertInterface` + factories, like `tests/test_margin_expert_sizing.py`): finding-2 scenario → `get_available_balance() == 1440.0` (was 3 240); margin off → account's headroom method not consulted (counter) and the value is unchanged from today's arithmetic.

**Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** new files + `packages/common/tests/test_margin_tradable_balance.py test_account_seams.py` + `tests/test_margin_expert_sizing.py tests/test_available_balance_clamp.py tests/test_tp_sl_validation.py` → PASS. Then from `testplatform/backend`: `tests/backtest/test_equity_golden_run.py tests/backtest/test_backtest_account_contract.py` → PASS, fingerprint unchanged.

**Step 5: Commit** `feat(margin): account stock-exposure ceiling is enforced — expert clamp + entry gate + per-account submit lock (review findings 2/3)`.

---

### Task 4: Capital mapping on every live sizing decision (plan step 5)

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` — `describe_capital() -> Dict[str, Any]`
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py` — `describe_capital_mapping() -> Optional[Dict[str, Any]]`
- Modify: `packages/common/ba2_common/core/TradeRiskManagement.py:_size_prioritized_orders` and `ba2_trade_platform/core/SmartRiskManagerToolkit.py:_auto_size_by_risk` — one log line
- Test: `tests/test_margin_capital_mapping.py` (new)

**Spec:** `describe_capital()` returns `{"balance", "margin_enabled", "margin_factor", "broker_multiplier", "effective_factor", "tradable_balance", "broker_buying_power", "gross_exposure", "pending_entries", "headroom"}` from ONE snapshot when margin is on; with margin off it returns `{"balance", "margin_enabled": False, "effective_factor": 1.0, "tradable_balance": balance}` and the rest `None`, and does NOT read the snapshot. `describe_capital_mapping()` adds `{"expert_id", "virtual_equity_pct", "virtual_balance", "used_balance", "available_balance", "equivalent_unlevered_balance": tradable_balance}` — the "equivalent unlevered account" column of the review table. Never raises: a `ValueError` from the account becomes `{"error": str(e)}` plus whatever was computed, and the caller logs it at ERROR.

Log line: in `_size_prioritized_orders` right after the existing "Virtual balance:" INFO, `logger.info(f"Capital mapping: {mapping}")` when `mapping["effective_factor"] != 1.0`, else `logger.debug(...)` (backtests: margin off → DEBUG, no snapshot read). Same in `_auto_size_by_risk`.

Tests: $2 000 equity / multiplier 2 / factor 2 / 100 % → `equivalent_unlevered_balance == 4000`, `virtual_balance == 4000`; $1 900 → 3 800; margin off → snapshot counter 0 and log at DEBUG not INFO (monkeypatch `logger`).

**Commit** `feat(margin): every sizing decision logs the raw-equity → effective-capital mapping (plan step 5)`.

---

### Task 5: Three-way live/backtest sizing parity tests + pinned blockers (plan steps 2 and 6)

**Files:**
- Create: `testplatform/backend/tests/backtest/test_margin_live_backtest_parity.py`
- (read-only inputs) `test_backtest_account_contract.py::_acct`, `tests/test_margin_expert_sizing.py`, `reports/margin/reproduce_margin_review.py`

**Harness:** three accounts registered on the backtest seam registry (`wire_backtest_seams().register_account(id, acct)`), three `ExpertInstance` rows (one per account, same settings, `virtual_equity_pct` parametrised) seeded with `backtest_db` helpers:
- **BT**: real `BacktestAccount`, `starting_cash=4000`, margin off (never changed).
- **L1**: live-shaped `AccountInterface` subclass: `get_balance()==4000`, snapshot `equity=4000, margin_multiplier=1.0, buying_power=4000, long_market_value=0, short_market_value=0`, margin off.
- **L2**: same shape, `get_balance()==2000`, snapshot `equity=2000, margin_multiplier=2.0, buying_power=4000`, settings `margin_enabled=True, margin_factor=2.0`.
Settings for the expert: `commission_per_trade=0`, `max_virtual_equity_per_instrument_percent=100`, price 100 everywhere (`get_instrument_current_price` on the fakes; the BT account through `AsOfPriceSource.load_bars`).

**Assertions (flat state, must PASS):** for pct in (25, 50, 100) and `sizing_mode` in (`notional`, `risk_atr` with SL 95): `get_virtual_balance`, `get_available_balance`, `TradeRiskManagement().size_candidate_orders(...)` quantities, `AdjustTakeProfitAction/AdjustStopLossAction.compute_price` (+10 % / −5 %, long and short → 110/95 and 90/105), and `_validate_position_size_limits` verdict are IDENTICAL across BT, L1, L2. Also: L2 with `margin_factor=3.0` (multiplier 2 binds) equals L2 at 2.0; L2 with margin off equals a $2 000 unlevered account (half of L1's quantities); L2 with `margin_multiplier=nan` → `get_virtual_balance()` is `None` (refusal, Task 1); $1 900×2 vs L1 at 3 800 and $2 100×2 vs 4 200 agree (build L1 variants).

**Post-entry state:** open one position of 10 @ 100 (BT: real `submit_order` + clock step + `refresh_orders` so the ledger fills — copy the fill dance from the contract test; L1/L2: snapshot equity unchanged, `long_market_value=1000`, BP reduced by 1 000, plus a `Transaction` row for the expert). Assert L1 == L2 for virtual/available/next quantity (PASS). Assert BT == L1 → **`@pytest.mark.xfail(strict=True, reason="finding 6: BacktestAccount.get_balance() is cash, live is equity; shared expert math charges the position twice in the backtest ($3,000/$2,000 vs $4,000/$3,000). Deliberately NOT fixed in the leverage feature: changing it moves every backtest result and is a separately versioned correction. See docs/plans/2026-09-09-margin-live-backtest-parity.md §6.")`**, with the exact numbers asserted inside so the xfail flips to XPASS (and fails strict) the day the contract is corrected.

**Finding 4 pin:** on L1 with a 9 000 position out of 18 000 and a 10 % cap, assert `_size_prioritized_orders` returns `max_equity_per_instrument == 1800` → `xfail(strict=True, reason="finding 4: classic per-instrument ceiling is available × ratio (900), not virtual × ratio (1800); changing it changes historical sizing — deferred, see plan §6")`.

Run from `testplatform/backend`. Also run `tests/backtest/test_equity_golden_run.py` after — fingerprint unchanged.

**Commit** `test(margin): three-way BT/$4k-1x/$2k-2x sizing parity gate; findings 4 and 6 pinned as strict xfail (plan steps 2, 6)`.

---

### Task 6: Release gate, docs, versions (plan step 7)

**Files:**
- Modify: `.github/workflows/parity-and-coverage.yml` — add `tests/backtest/test_margin_live_backtest_parity.py` to the blocking PARITY GATE step command (same line as `test_parity_golden.py`).
- Modify: `docs/plans/2026-09-09-margin-live-backtest-parity.md` — replace the "Status: plan only" sentence with a status table: step → done/deferred, naming the test file and commit per step; §6 lists findings 6 and 4 as strict-xfail pins (blockers, not fixed) and finding 2 as "ceiling consequence closed by the Task 3 clamp/gate; expert-side cost accounting unchanged (result-neutral)".
- Modify: `docs/plans/2026-09-08-margin-trading-design.md` "Follow-ups" section — tick "allocator under the factor" as *enforced as a ceiling gate when margin is on* and add the two pinned blockers.
- Modify: `reports/margin/reproduce_margin_review.py` — add a module docstring line: "Records behaviour at 51234531 (pre-fix). Findings 1, 3 and 5 changed on branch fix/margin-review-2026-09-09; the durable expectations live in the tests named in docs/plans/2026-09-09-margin-review-fixes-plan.md. Do not run this as a regression test." Do NOT change its assertions.
- Modify: `ba2_trade_platform/version.py` `APP_VERSION 2026.09.1143 → 2026.09.1144`; `testplatform/version.py` `TEST_APP_VERSION 2026.09.0023 → 2026.09.0024` (packages/ changed).

**Commit** `chore(margin): parity CI gate + plan status + APP 1144 / TEST 0024`.

---

## Final gate (controller runs, not a task agent)

1. `testplatform/backend`: `test_equity_golden_run.py test_option_golden_run.py test_equity_cap_e2e.py test_parity_golden.py test_margin_live_backtest_parity.py` → all pass (xfails xfailed, none XPASS); fingerprints identical to `reports/margin/baseline_goldens_2026-09-09.txt`.
2. `testplatform/backend`: `tests/backtest -q` full.
3. `packages/common/tests -q` full (separately).
4. `tests -q --ignore=tests/test_portfolio_allocation_page.py` then that file alone; compare against the dev baseline of 24 failures / 7 files (memory `worktree-test-running-quirks`); any NEW failure is ours.
5. Write the resolution section into the final report to the user (what was fixed, what is pinned, what remains a decision).

---

## Status (2026-09-09)

All six tasks are implemented on branch `fix/margin-review-2026-09-09`. The
equity golden fingerprint recorded in `reports/margin/baseline_goldens_2026-09-09.txt`
(step 1) is unchanged after every task.

| Task | Commit(s) |
|---|---|
| 0 Baseline freeze and docs | 681a20cb |
| 1 Non-finite inputs refuse loudly (finding 5) | 51b9d1da, e99b36fd |
| 2 Smart RM uses the shared budget resolver (finding 1) | 2036d39a, 7f5bd5b5, 6a906172 |
| 3 Account stock-exposure ceiling (findings 2/3) | d6c1028f, b01a475e |
| 4 Capital mapping is logged per decision | 5be52d7d, fb4c9edd |
| 5 Three-way parity gate, blockers pinned | af479f07, 1cfc042a |
| 6 CI parity gate, plan status, versions | this commit |

Test files per task: T1 `packages/common/tests/test_margin_finite_inputs.py`,
`tests/test_margin_finite_inputs_expert.py`; T2
`packages/common/tests/test_position_sizing.py`,
`tests/test_smart_rm_sizing_budget.py`,
`packages/common/tests/test_atr_risk_budget_decoupling.py`; T3
`packages/common/tests/test_stock_exposure_gate.py`,
`tests/test_margin_exposure_clamp.py`, `tests/test_tastytrade_account.py`; T4
`tests/test_margin_capital_mapping.py`; T5
`testplatform/backend/tests/backtest/test_margin_live_backtest_parity.py`
(18 pass plus 2 strict xfail).

### Deferred / follow-up

Recorded, deliberately not fixed here.

- Findings 6 and 4: NOT fixed by design; pinned as strict xfails in the parity
  file. Finding 6: `BacktestAccount.get_balance()` is cash, live is equity, so
  shared expert math charges a position twice in the backtest, $3,000/$2,000
  against $4,000/$3,000. Finding 4: the classic per-instrument ceiling is
  available capital times the ratio (900), not virtual capital times the ratio
  (1800). Both change historical backtest numbers; a separately versioned
  correction is the operator's decision.
- Finding 2: the ceiling consequence is closed by the Task 3 clamp and gate
  (margin on); the expert's own cost-based used-balance accounting is unchanged
  (result-neutral for backtests).
- Live margin-on round-trip cost: one `submit_order` now takes about 3 snapshots
  plus 2 pending-order queries under the per-account lock (position-size
  validator, expert headroom clamp, exposure gate), and `describe_capital()`
  adds one snapshot plus one order scan per sizing decision. TastyTrade's
  snapshot is an uncached REST call. Follow-up: compute the StockExposure
  breakdown once per submit and thread it through.
- `tradingorder.account_id` and `depends_on_order` are not indexed; the
  pending-entry query runs per sizing decision against the full orders table
  (live, margin on).
- The per-account submit RLock is the one Task 3 piece reachable with margin off
  (serialises only; results unchanged; GA trial threads share the backtest
  account id's lock).
- `_validate_account_exposure` catches only `ValueError`; broker exceptions from
  `get_instrument_current_price` propagate (house style under
  `BA2_ERROR_MODE=enforce`).
- IBKR plus `margin_enabled` remains the documented unsupported path (no stock
  multiplier, so it refuses at the multiplier step).
- Classic RM scales the risk budget by `_regime_scale(expert, 'regime_risk_scale')`;
  Smart RM does not (known divergence).
- `SmartRiskManagerToolkit`: `min_stop_loss_pct or 7.0` (explicit-quantity path)
  and `or 0.0` (auto-size path). A configured 0.0 becomes 7.0 in one place: one
  setting, two floors. Deferred, pinned in `tests/test_smart_rm_sizing_budget.py`.
- LIVE behaviour change on the first deploy of a genome with
  `atr_risk_budget_pct` set (never set on prod or dev as of 2026-09-06 per
  `tools/migrate_atr_budget_swap.py`): Smart RM explicit-quantity orders are
  reduced when the budget-implied stop is tighter than `min_stop_loss_pct`.
- Pre-existing dead branch: `ba2_trade_platform/core/SmartRiskManagerQueue.py:101-109`
  tests `if account_equity is None` on `get_tradable_balance()`, which raises
  rather than returning None.
- `effective_factor_for` (pure) has no finiteness guard; the invariant lives at
  the call sites.
- Test-stub duplication: the bare account `_Stub` is copied across
  `test_margin_finite_inputs`, `test_margin_tradable_balance`,
  `test_margin_accessors` and `test_stock_exposure_gate`. Extract a shared stub
  module later.
- `get_virtual_balance`, `_available_balance_breakdown` and
  `describe_capital_mapping` repeat the resolve-instance-and-account dance (3
  copies). A helper is a pure refactor and needs a golden check.
- The expert seam `describe_capital_mapping` is called bare while the account
  seams are getattr-guarded; three Smart RM test doubles grew the method.
- Not covered by any test yet: cancel/retry reserve-release of a pending entry;
  several experts entering concurrently on one account.
- Latent, dead today: `BacktestInstanceResolver.get_account_instance_from_transaction`
  reads `transaction.account_id`, a column `Transaction` does not have (all
  callers use the live registry).
