### 1. HIGH — Unrelated or earlier-expiring calls count as protection for a naked short

**File:** `packages/common/ba2_common/core/option_lifecycle.py`, lines **804–816**

`uncovered_short_calls()` pools every long call into one count. It never checks `underlying` or whether the long survives through the short’s expiry.

**Scenario:** Supply one long AAPL call and one short TSLA call, each one contract. The function returns `()`, declaring no uncovered shorts, although the TSLA call has unlimited loss. Likewise, a long call expiring January 17 is counted as covering a same-underlying short expiring February 21, despite leaving the short naked after January 17.

### 2. HIGH — Invalid risk-manager configuration disables the risk manager

**File:** `packages/common/ba2_common/core/OptionRiskManagement.py`, lines **236–249**

`option_risk_manager_enabled()` catches the configuration error raised by `normalise_risk_manager_mode()` and returns `False`. Logging does not make this fail closed: the error becomes a decision not to engage the option rails.

**Scenario:** Settings contain `risk_manager_mode="classic_option"` instead of `"classic_options"`, with deployment and concurrency limits otherwise configured. The function warns and returns `False`; dispatch bypasses those option limits instead of refusing the malformed configuration.

### 3. HIGH — Configured max-loss ceiling passes everything when its measurement function is missing

**File:** `packages/common/ba2_common/core/option_selection_policy.py`, lines **1184–1185**

The early return treats a missing `structure_fn` exactly like an explicitly disabled ceiling.

**Scenario:** Call `eligible()` with an otherwise eligible contract, `max_loss_ceiling=0`, and `structure_fn=None`. It returns the contract with no refusal. A configured zero-dollar risk allowance has therefore admitted a contract without measuring its loss.

### 4. HIGH — Missing quote side is replaced with a fabricated zero-spread market

**File:** `testplatform/backend/app/services/backtest/parquet_options_provider.py`, lines **494–504**, **685–688**  
**Related:** `packages/common/ba2_common/core/option_selector.py`, lines **255–259**; `option_selection_policy.py`, lines **598–600**, **615**, **667**

When either quote side is absent, the provider overwrites **both** sides with the trade close—even when the other side is a real quote. The selector accepts the resulting zero spread. The chain-wide availability probe does not prevent this when another contract has a positive spread.

**Scenario:** A row has `bid=1`, `ask=None`, `close=2`. Another contract publishes a real spread. With `max_spread_pct=0.10`, the first row becomes `bid=ask=2`, passes the spread gate, and presents a fabricated executable bid and ask.

This also identifies the “zero ranks best” code: `_minimise()` at policy line **615**, used for spreads at **667**, turns a normalized zero into **1.0**. An all-zero spread column receives 1.0 everywhere; in a mixed real/proxy chain, the fabricated zero spread ranks above positive real spreads.

### 5. HIGH — Lifecycle decisions price a short buyback at a stale last trade when the ask is missing

**File:** `packages/common/ba2_common/core/option_lifecycle.py`, lines **497–503**, **702–703**

Both `_exit_mark()` and `pmcc_credit_decay()` substitute `last` for a missing ask. A historical print does not establish a currently executable buyback price.

**Scenario:** One short was sold for $2/share. Its row now has `bid=3`, `ask=None`, `last=0.50`. `_pnl_pct()` reports **+75%**, triggering a 50% profit capture; `pmcc_credit_decay()` also reports 75% decay. Yet even the published bid is $3, so a normal uncrossed ask cannot support that $0.50 buyback. The missing ask should make the measurement unknown, not turn a losing short into a captured-profit decision.

### 6. HIGH — File-backed runs retain and share breaker state across runs

**File:** `packages/common/ba2_common/core/OptionRiskManagement.py`, lines **333–337**, **899–902**, **935–937**

State isolation depends solely on `inmem_trades_active()`. When false, state uses `(None, expert_id)`, but `reset_thread_state()` removes only keys whose first element is the current thread ID.

**Scenario:** A file-backed run for expert 7 stores a halted breaker. The run ends and calls `reset_thread_state()`; `(None, 7)` survives. A subsequent file-backed run for expert 7 starts and `get_breaker_state(7)` returns the previous run’s halted state rather than a fresh breaker. A later-date run can therefore contaminate an earlier-date replay; the same key also collides with live state for that expert.

### 7. MEDIUM — Wing selection can return the center or an inward strike

**File:** `packages/common/ba2_common/core/option_selector.py`, lines **543–553**

`select_wing()` chooses the nearest strike to the calculated target without requiring a call wing to be above the center or a put wing to be below it.

**Scenario:** For calls, `center_strike=100`, `width_pct=5`, and eligible strikes `[95, 100]`, the function returns strike **100**. If only strike 95 survives liquidity filtering, it returns **95**. Neither is the promised farther-OTM protective wing; the first can collapse the intended spread to the center contract.

### 8. MEDIUM — Missing premium passes the unconditional minimum-premium gate

**File:** `packages/common/ba2_common/core/option_selector.py`, lines **236–238**, **450–459**

The premium floor is applied only when `mark is not None`. Neither `_candidates()` nor the legacy picker rejects an otherwise selectable unpriced contract.

**Scenario:** A contract has `bid=ask=last=None`, valid expiry, and `delta=0.30`. With liquidity gates disabled and `method="delta", strike_param=0.30`, `passes_liquidity()` returns `True` and `select_single()` returns that contract. The unconditional $0.10 floor has passed an input it cannot measure.

This establishes a selection defect; downstream order submission was not supplied and is not assumed to succeed.

### 9. MEDIUM — Negative selection weights turn missing measurements into best-scoring measurements

**File:** `packages/common/ba2_common/core/option_selection_policy.py`, lines **603–605**, **663–664**, **1235–1238**

Missing premium/IV receives `_WORST=0` before multiplication by a signed weight. For negative weights, zero is the **best**, not worst, weighted contribution.

**Scenario:** Three contracts have identical target delta and expiry:

| Strike | IV |
|---|---:|
| 90 | `None` |
| 100 | 0.20 |
| 110 | 0.40 |

With `w_iv=-2`, IV contributions become `[0, 0, -2]`. Box-center contributions are identical, so the missing-IV strike 90 wins the strike tie-break. Missing IV is treated as equally desirable as the cheapest measured volatility instead of receiving the worst contribution for the configured direction. The same inversion applies to negative `w_premium`.

### 10. MEDIUM — Non-finite lifecycle inputs produce a healthy HOLD instead of UNKNOWN

**File:** `packages/common/ba2_common/core/option_lifecycle.py`, lines **533–544**, **570–579**, **1087–1104**, **1145–1152**

The P&L and tested-delta paths check for `None`, but not finiteness. NaN survives as a computed value; subsequent comparisons all evaluate false, without setting a blind reason.

**Scenario:** A single-expiry short has valid entry premium, quantity and expiry, but its current ask and delta are both `float("nan")`. Profit capture, credit stop, and tested-delta checks are enabled. `_pnl_pct()` returns NaN with an empty error string; `_tested()` returns `False` with no error. With no other trigger, `decide()` returns **HOLD**, reporting `P&L nan%`, rather than UNKNOWN.

### 11. MEDIUM — Structure risk metrics ignore the supplied contract multiplier

**File:** `packages/common/ba2_common/core/option_lifecycle.py`, lines **423–424**, **438**, **441**

`structure_metrics()` hardcodes 100 despite `OptionStructure.multiplier` being an input. Both candidate and existing-book calculations can therefore measure a different contract size from assignment calculations.

**Scenario:** An `OptionStructure(multiplier=10)` holds one short put at strike 100. The function reports notional and naked committed capital of **$10,000**, rather than **$1,000**. A 5-point vertical similarly reports $500 of width exposure instead of $50. Conversely, a supplied multiplier above 100 is understated.

### 12. MEDIUM — Intraday entry-delta lookup uses the entry day’s future close

**File:** `testplatform/backend/app/services/backtest/parquet_options_provider.py`, lines **594–603**, **744–754**, **429–435**

`delta_at_entry()` accepts a datetime but discards its time, then includes that date’s daily bar. Its returned delta is inverted from the daily option close and underlying close.

**Scenario:** Call `delta_at_entry(..., when=datetime(2025, 6, 2, 10, 0))` with a June 2 daily bar available in the historical store. It selects that bar and returns a delta calculated using June 2’s closing prices—information unavailable at the 10:00 entry. This violates point-in-time lookup at the supplied entry instant.
