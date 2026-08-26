# Option Live Safety — Seam Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three seams so that an option position can never be treated as equity, can never be written or left without verified cover, and can never have a whole multi-leg structure closed by one leg settling.

**Architecture:** Three independent guards at shared seams rather than thirteen point fixes. Seam 1 refuses option transactions in the equity close/adjust paths. Seam 2 adds one derived, tri-state `shares_pledged_to_short_calls()` consumed at entry, at exit and by a continuous monitor. Seam 3 makes the multi-leg close decision per-contract instead of wholesale. No schema change, no migration.

**Tech Stack:** Python 3.12, SQLModel, pytest. Source of truth for shared code is `packages/common/ba2_common/`; `ba2_trade_platform/core/*` are re-export shims and must not be edited.

**Spec:** `docs/superpowers/specs/2026-08-25-option-live-safety-seams-design.md`
**Findings:** `docs/superpowers/reviews/2026-08-25-option-review-findings.md`

**Scope:** Seams 1–3 only. The eight independent defects (`OPT-S5`, `OPT-S1`, `OPT-L5`/`S6`, `OPT-L6`, `OPT-S2`, `OPT-L4`, `OPT-S4`, `OPT-L7`) are a separate plan.

---

## Rules that apply to every task

**Never place, open, close, modify or cancel a real trade.** Every test uses doubles. No test may reach a broker.

**Branch from `origin/dev`.** `pf_allocation` is stale.

**Stage by explicit path.** Never `git add -A`, `git add .`, or `git commit -a`.

**Baselines before you start** (run these; if they differ, note it and continue):
```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/ -q        # 4081 passed
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest packages/common/tests -q   # 1752 passed
```

**Every guard needs a caller-obeys test.** Asserting that a function raises is not enough — a gate can raise into a caller that swallows it. For each seam, also assert the *order is not submitted*.

**Unknown is never zero.** Any function added here that can fail to measure must return `None`, never `0`. A measured zero and an unmeasurable value are different facts and both must be tested.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `packages/common/ba2_common/core/interfaces/AccountInterface.py` | Seam 1 guards on the two close entry points; Seam 2 exit guard | 1, 2, 7 |
| `packages/common/ba2_common/core/TransactionHelper.py` | Seam 1 guard on the adjust path | 3 |
| `ba2_trade_platform/core/portfolio_allocation_service.py` | Seam 1 filter — options never enter the equity plan | 4 |
| `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py` | Seam 2 `shares_pledged_to_short_calls`; Seam 2 entry guard | 5, 6 |
| `packages/common/ba2_common/core/option_lifecycle.py` | Seam 2 `cover_lost` exit reason (pure) | 8 |
| `ba2_trade_platform/core/option_lifecycle_service.py` | Seam 2 monitor wiring (live only) | 9 |
| `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` | Seam 3 per-contract close decision | 10 |
| `tests/test_accounts/test_account_interface.py` | Seam 1 + Seam 2 exit tests | 1, 2, 7 |
| `tests/test_option_close_seam.py` (new) | Seam 1 adjust + allocation tests | 3, 4 |
| `packages/common/tests/test_option_collateral_pledge.py` (new) | Seam 2 pure tests | 5, 6 |
| `packages/common/tests/test_option_lifecycle.py` | Seam 2 `cover_lost` | 8 |
| `tests/test_option_cover_monitor.py` (new) | Seam 2 monitor wiring | 9 |
| `tests/test_multileg_close_decision.py` (new) | Seam 3 | 10 |

---

# SEAM 1 — asset-class blindness

### Task 1: `submit_close_order_for_transaction` refuses an option transaction

An option `Transaction.symbol` is the **underlying** ticker and `get_current_open_qty()` returns a **contract** count, so this function currently submits N *shares* for N *contracts*.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py:1562` (top of the function body, before any write)
- Test: `tests/test_accounts/test_account_interface.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_accounts/test_account_interface.py`:

```python
class TestTheEquityCloseSeamRefusesOptions:
    """An option Transaction's symbol is the UNDERLYING and its quantity is CONTRACTS.
    Building an equity order from those two fields submits N shares for N contracts."""

    def test_an_option_transaction_is_refused(self):
        from ba2_trade_platform.core.types import AssetClass
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        with pytest.raises(ValueError) as exc:
            account.submit_close_order_for_transaction(txn)
        assert "close_option" in str(exc.value)

    def test_an_equity_transaction_is_still_closed(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = account.submit_close_order_for_transaction(txn)
        assert result["success"] is True

    def test_NO_ORDER_IS_WRITTEN_when_an_option_is_refused(self):
        """The caller-obeys half: refusing must not leave a half-created equity order."""
        from ba2_trade_platform.core.types import AssetClass
        from ba2_common.core.trade_store import orders_where
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        before = len(orders_where(account_id=acct_def.id))
        with pytest.raises(ValueError):
            account.submit_close_order_for_transaction(txn)
        assert len(orders_where(account_id=acct_def.id)) == before
```

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_accounts/test_account_interface.py::TestTheEquityCloseSeamRefusesOptions -q
```
Expected: `2 failed, 1 passed` — the two option tests fail because no refusal exists (the equity one already passes).

- [ ] **Step 3: Implement the guard**

In `AccountInterface.submit_close_order_for_transaction`, immediately after the docstring and before the existing `from ba2_common.core.db import add_instance` line:

```python
        from ba2_common.core.types import AssetClass

        # SEAM 1 — an OPTION transaction must never be closed through the EQUITY path.
        #
        # An option Transaction's `symbol` is deliberately the UNDERLYING ticker
        # (OptionsAccountInterface._record_option_intent_on_transaction), and
        # get_current_open_qty() returns a CONTRACT count. Building a TradingOrder from
        # those two fields with asset_class at its EQUITY default therefore submits N
        # SHARES of the underlying for N CONTRACTS -- a different instrument, off by the
        # contract multiplier, and it never flattens the option leg.
        #
        # We REFUSE rather than route to close_option_position: routing would make the
        # generic equity path quietly become an option path, hiding the asset class one
        # layer further up, which is the defect being fixed.
        if transaction.asset_class == AssetClass.OPTION:
            raise ValueError(
                f"Transaction {transaction.id} is an OPTION position (underlying "
                f"{transaction.symbol}). submit_close_order_for_transaction builds an "
                f"EQUITY order from transaction.symbol and a CONTRACT count, which would "
                f"submit shares of {transaction.symbol} and leave the option open. "
                f"Use close_option_position() / the close_option action instead."
            )
```

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_accounts/test_account_interface.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/AccountInterface.py tests/test_accounts/test_account_interface.py
git commit -m "fix(options): the equity close seam refuses an option transaction (OPT-L3)"
```

---

### Task 2: `close_transaction` refuses an option transaction

`close_transaction` is the public entry point `CloseAction` calls. It must refuse before it cancels anything.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py:1678`
- Test: `tests/test_accounts/test_account_interface.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestCloseTransactionRefusesOptions:
    def test_close_transaction_refuses_an_option(self):
        from ba2_trade_platform.core.types import AssetClass
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=1.0,
                                 asset_class=AssetClass.OPTION)
        result = account.close_transaction(txn.id)
        assert result["success"] is False
        assert "close_option" in result["message"]

    def test_close_transaction_still_closes_an_equity(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = account.close_transaction(txn.id)
        assert result["success"] is True

    def test_an_option_close_CANCELS_NOTHING(self):
        """Caller-obeys: the refusal happens before any order is canceled or deleted."""
        from ba2_trade_platform.core.types import AssetClass
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=1.0,
                                 asset_class=AssetClass.OPTION)
        order = create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=1.0,
                                     transaction_id=txn.id, status=OrderStatus.NEW,
                                     asset_class=AssetClass.OPTION,
                                     contract_symbol="AAPL260116C00250000")
        result = account.close_transaction(txn.id)
        assert result["success"] is False
        assert result.get("canceled_count", 0) == 0
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder as TO
        assert get_instance(TO, order.id).status == OrderStatus.NEW
```

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_accounts/test_account_interface.py::TestCloseTransactionRefusesOptions -q
```
Expected: `2 failed, 1 passed`.

- [ ] **Step 3: Implement the guard**

At the top of `close_transaction`'s body, immediately after the docstring, before it loads orders:

```python
        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction as _Txn
        from ba2_common.core.types import AssetClass

        # SEAM 1 — refuse an OPTION transaction before ANYTHING is canceled or deleted.
        # See submit_close_order_for_transaction for the full reasoning. This returns a
        # dict rather than raising because close_transaction's contract is a result dict
        # and its callers branch on ["success"]; raising here would change that contract.
        _txn = get_instance(_Txn, transaction_id)
        if _txn is not None and _txn.asset_class == AssetClass.OPTION:
            msg = (
                f"Transaction {transaction_id} is an OPTION position (underlying "
                f"{_txn.symbol}) and cannot be closed through the equity path — that "
                f"would submit shares of {_txn.symbol} and leave the option open. "
                f"Use close_option_position() / the close_option action instead."
            )
            logger.error(f"close_transaction: {msg}")
            return {
                "success": False,
                "message": msg,
                "canceled_count": 0,
                "deleted_count": 0,
                "close_order_id": None,
            }
```

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_accounts/ -q
```

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/AccountInterface.py tests/test_accounts/test_account_interface.py
git commit -m "fix(options): close_transaction refuses an option before canceling anything (OPT-L3)"
```

---

### Task 3: `adjust_quantity_with_tpsl` refuses an option transaction

**Files:**
- Modify: `packages/common/ba2_common/core/TransactionHelper.py:739`
- Test: `tests/test_option_close_seam.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_option_close_seam.py`:

```python
"""Seam 1 — the equity adjust path and the allocation planner must not see options."""
import pytest
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction
from ba2_trade_platform.core.types import AssetClass


class TestAdjustQuantityRefusesOptions:
    def test_adjusting_an_option_transaction_is_refused(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION)
        with pytest.raises(ValueError) as exc:
            TransactionHelper.adjust_quantity_with_tpsl(
                transaction=txn, new_quantity=1.0, account=account)
        assert "OPTION" in str(exc.value)

    def test_adjusting_an_equity_transaction_is_unaffected(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        # Must not raise the option refusal. Any other outcome is this test's business.
        try:
            TransactionHelper.adjust_quantity_with_tpsl(
                transaction=txn, new_quantity=5.0, account=account)
        except ValueError as e:
            assert "OPTION" not in str(e)
```

> **Note for the implementer:** `adjust_quantity_with_tpsl`'s real signature is at
> `packages/common/ba2_common/core/TransactionHelper.py:739`. Read it and adjust the two
> call sites above to match its actual parameter names before running. Do not change the
> assertions.

- [ ] **Step 2: Run to verify it fails**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_option_close_seam.py -q
```
Expected: `test_adjusting_an_option_transaction_is_refused` FAILS (`DID NOT RAISE`).

- [ ] **Step 3: Implement the guard**

At the top of `adjust_quantity_with_tpsl`, before the order is built at `:790`:

```python
        from ba2_common.core.types import AssetClass

        # SEAM 1 — same defect as AccountInterface.submit_close_order_for_transaction:
        # this builds an equity order from `transaction.symbol`, which for an option is
        # the UNDERLYING, sized in CONTRACTS. Refuse.
        if transaction.asset_class == AssetClass.OPTION:
            raise ValueError(
                f"Transaction {transaction.id} is an OPTION position (underlying "
                f"{transaction.symbol}); adjust_quantity_with_tpsl builds an EQUITY "
                f"order and cannot resize an option structure."
            )
```

- [ ] **Step 4: Run to verify it passes**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_option_close_seam.py -q
```

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/TransactionHelper.py tests/test_option_close_seam.py
git commit -m "fix(options): the equity adjust path refuses an option transaction (OPT-L3)"
```

---

### Task 4: Option transactions never enter the allocation plan

**Files:**
- Modify: `ba2_trade_platform/core/portfolio_allocation_service.py:51-55`
- Test: `tests/test_option_close_seam.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_option_close_seam.py`:

```python
class TestAllocationExcludesOptions:
    def test_an_option_transaction_is_not_in_the_allocation_plan(self):
        """An option txn's symbol is the UNDERLYING, so without an asset_class filter the
        allocator treats a covered call as a holding of the stock and can sell the cover."""
        from ba2_trade_platform.core.portfolio_allocation_service import _open_transaction_ids
        acct_def = create_account_definition()
        equity = create_transaction(symbol="AAPL", quantity=100.0)
        option = create_transaction(symbol="AAPL", quantity=1.0,
                                    asset_class=AssetClass.OPTION)
        ids = _open_transaction_ids(acct_def.id)
        assert equity.id in ids
        assert option.id not in ids
```

> **Note for the implementer:** confirm `_open_transaction_ids`' real signature at
> `ba2_trade_platform/core/portfolio_allocation_service.py:51` and match the call above.

- [ ] **Step 2: Run to verify it fails**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_option_close_seam.py::TestAllocationExcludesOptions -q
```
Expected: FAIL — `option.id` is present.

- [ ] **Step 3: Implement the filter**

In `_open_transaction_ids`, filter the result to equity transactions only. The existing body
returns a set of ids from a `transactions_where(...)` call; wrap that call's result:

```python
    from ba2_common.core.types import AssetClass

    # An option Transaction's `symbol` is the UNDERLYING ticker, so without this filter the
    # allocation planner reads a covered call as a holding of the stock -- and a "set AAPL
    # to 0%" run then submits a wrong-instrument order for it AND sells the shares that
    # collateralise it. option_lifecycle_service._open_option_transactions already filters
    # on exactly this column; allocation was the caller that skipped it.
    return {t.id for t in txns if t.asset_class == AssetClass.EQUITY}
```

where `txns` is whatever the existing body already fetched. Do not change the fetch itself —
narrowing the returned set keeps the change to one line and cannot alter which transactions
are loaded.

- [ ] **Step 4: Run to verify it passes**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_option_close_seam.py tests/test_portfolio_allocation_page.py -q
```

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/portfolio_allocation_service.py tests/test_option_close_seam.py
git commit -m "fix(options): allocation excludes option transactions from the equity plan (OPT-L2)"
```

---

# SEAM 2 — the collateral invariant

### Task 5: `shares_pledged_to_short_calls` — derived and tri-state

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py` (add after `open_option_orders_book_wide`, which ends at `:860`)
- Test: `packages/common/tests/test_option_collateral_pledge.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_option_collateral_pledge.py`:

```python
"""Seam 2 — how many shares are pledged as cover to open short calls.

Tri-state and it matters: None means UNMEASURABLE (refuse the sell), 0 means MEASURED
that nothing is pledged (allow it). Collapsing those is the bug this seam exists to stop.
"""
import pytest


class TestSharesPledgedToShortCalls:
    def test_no_options_at_all_is_a_measured_zero(self, options_account):
        assert options_account.shares_pledged_to_short_calls("AAPL") == 0

    def test_one_short_call_pledges_one_hundred_shares(self, options_account_with_short_call):
        assert options_account_with_short_call.shares_pledged_to_short_calls("AAPL") == 100

    def test_three_contracts_pledge_three_hundred(self, options_account_with_short_calls_3):
        assert options_account_with_short_calls_3.shares_pledged_to_short_calls("AAPL") == 300

    def test_a_LONG_call_pledges_nothing(self, options_account_with_long_call):
        assert options_account_with_long_call.shares_pledged_to_short_calls("AAPL") == 0

    def test_a_short_PUT_pledges_no_shares(self, options_account_with_short_put):
        """A short put obliges CASH, not shares. It belongs to the assignment-capacity
        gate, not to this one."""
        assert options_account_with_short_put.shares_pledged_to_short_calls("AAPL") == 0

    def test_a_different_underlying_is_not_counted(self, options_account_with_short_call):
        assert options_account_with_short_call.shares_pledged_to_short_calls("MSFT") == 0

    def test_an_unreadable_book_is_UNKNOWN_not_zero(self, options_account_book_raises):
        assert options_account_book_raises.shares_pledged_to_short_calls("AAPL") is None

    def test_a_contract_with_no_multiplier_is_UNKNOWN_not_one_hundred(
            self, options_account_short_call_no_multiplier):
        """Do not silently assume 100 -- an adjusted contract may deliver something else
        (OPT-L7). Unmeasurable multiplier means unmeasurable pledge."""
        assert options_account_short_call_no_multiplier.shares_pledged_to_short_calls("AAPL") is None
```

> **Note for the implementer:** write the eight fixtures at the top of this file using the
> existing `packages/common/tests/` conventions. Each builds an account double whose
> `open_option_orders_book_wide()` returns the described list of `TradingOrder` rows
> (`asset_class=AssetClass.OPTION`, `contract_symbol` an OCC string, `side`, `quantity`,
> `multiplier`). `options_account_book_raises` must have `open_option_orders_book_wide`
> raise. Do not mock `shares_pledged_to_short_calls` itself.

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  packages/common/tests/test_option_collateral_pledge.py -q
```
Expected: 8 failures, `AttributeError: 'ExampleAccount' object has no attribute 'shares_pledged_to_short_calls'`.

- [ ] **Step 3: Implement**

Add to `OptionsAccountInterface`, after `open_option_orders_book_wide`:

```python
    def shares_pledged_to_short_calls(self, underlying: str) -> Optional[int]:
        """Shares of ``underlying`` currently acting as cover for open SHORT CALLS.

        TRI-STATE, and the distinction is the whole point:
          * an int (including 0) -- MEASURED. 0 means "nothing is pledged", and selling
            the shares is fine.
          * None -- UNMEASURABLE. The caller must REFUSE the sell, not permit it.

        Returning 0 on an unreadable book would let a covered call be stripped of its
        cover during a broker outage: the single most expensive instance of this
        codebase's recurring unknown-read-as-zero defect.

        Short PUTS are deliberately excluded -- a short put obliges CASH, and that is the
        assignment-capacity gate's question, not this one.

        The multiplier is read PER CONTRACT and never assumed to be 100: an adjusted
        contract may deliver a different number of shares (OPT-L7), and guessing here
        would under-report the pledge on exactly the contract where it matters most.
        """
        from ba2_common.core.types import AssetClass, OrderDirection, OptionRight

        try:
            open_orders = self.open_option_orders_book_wide()
        except Exception as e:
            logger.error(
                f"shares_pledged_to_short_calls({underlying}): could not read the option "
                f"book: {e}. Returning None (UNMEASURABLE) -- callers must refuse, not "
                f"assume the shares are free.", exc_info=True)
            return None
        if open_orders is None:
            logger.error(
                f"shares_pledged_to_short_calls({underlying}): the option book is "
                f"unreadable (None). Returning None (UNMEASURABLE).")
            return None

        total = 0
        for o in open_orders:
            if getattr(o, "asset_class", None) != AssetClass.OPTION:
                continue
            contract = getattr(o, "contract_symbol", None)
            if not contract:
                continue                      # net-only parent row, not a leg
            if o.side != OrderDirection.SELL:
                continue                      # long options pledge nothing
            parsed = self.parse_option_symbol(contract)
            if parsed is None or parsed.option_type != OptionRight.CALL:
                if parsed is None:
                    logger.error(
                        f"shares_pledged_to_short_calls({underlying}): could not parse "
                        f"contract {contract!r}. Returning None (UNMEASURABLE).")
                    return None
                continue                      # a short PUT: not this gate's question
            if parsed.underlying != underlying:
                continue
            multiplier = getattr(o, "multiplier", None)
            if not multiplier:
                logger.error(
                    f"shares_pledged_to_short_calls({underlying}): contract {contract!r} "
                    f"has no multiplier. Returning None (UNMEASURABLE) rather than "
                    f"assuming 100 -- an adjusted contract may not deliver 100 shares.")
                return None
            qty = o.filled_qty if o.filled_qty else o.quantity
            if qty is None:
                logger.error(
                    f"shares_pledged_to_short_calls({underlying}): contract {contract!r} "
                    f"has no measurable quantity. Returning None (UNMEASURABLE).")
                return None
            total += int(abs(float(qty)) * int(multiplier))
        return total
```

> **Note for the implementer:** `parse_option_symbol` is the existing OCC parser on this
> interface — confirm its real name and return shape before use (`AlpacaAccount._parse_occ_symbol`
> is the live one). If the interface has no shared parser, add the call through whatever
> the interface already exposes; do not write a second OCC parser.

Add the second accessor in the same commit — Tasks 6, 7 and 9 all depend on it:

```python
    def held_shares_for_cover(self, underlying: str) -> Optional[int]:
        """Shares of ``underlying`` this ACCOUNT holds, for cover arithmetic.

        Same tri-state contract as shares_pledged_to_short_calls: an int (including 0) is
        MEASURED, None is UNMEASURABLE and every caller must refuse rather than assume.

        Deliberately NOT TradeActions._held_equity_shares: that one is scoped to a single
        expert (see its docstring -- "coverage and eligibility are different questions"),
        and cover is an account-wide fact. Shares bought by a different expert still cover
        the call, and the broker does not care which expert bought them.
        """
        from ba2_common.core.types import AssetClass

        positions = self.get_positions()
        if positions is None:
            logger.error(
                f"held_shares_for_cover({underlying}): get_positions() returned None "
                f"(the fetch FAILED -- not 'flat'). Returning None (UNMEASURABLE).")
            return None
        for p in positions:
            if getattr(p, "asset_class", AssetClass.EQUITY) == AssetClass.OPTION:
                continue
            if getattr(p, "symbol", None) != underlying:
                continue
            qty = getattr(p, "quantity", None)
            if qty is None:
                logger.error(
                    f"held_shares_for_cover({underlying}): the position row carries no "
                    f"quantity. Returning None (UNMEASURABLE).")
                return None
            return int(float(qty))
        return 0        # MEASURED: the broker answered, and holds none of this symbol
```

Add these two tests for it to the same file:

```python
class TestHeldSharesForCover:
    def test_a_failed_position_fetch_is_UNKNOWN_not_flat(self, options_account_positions_none):
        assert options_account_positions_none.held_shares_for_cover("AAPL") is None

    def test_an_empty_position_list_is_a_MEASURED_zero(self, options_account):
        assert options_account.held_shares_for_cover("AAPL") == 0
```

This is the `get_positions()` tri-state that has been conflated seven times in this repo:
`None` means the fetch failed, `[]` means the account genuinely holds nothing.

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  packages/common/tests/test_option_collateral_pledge.py -q
```
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py packages/common/tests/test_option_collateral_pledge.py
git commit -m "feat(options): shares_pledged_to_short_calls -- derived, tri-state cover accounting"
```

---

### Task 6: The entry guard lives at the broker seam

`SellCoveredCallAction` already checks cover, and stays. The point is that it is the *only* caller doing so — a second caller of the seam writes an uncovered short call with no test at all.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:164` (with the other pre-write validations, after the 4-leg check and before the single-expiry check)
- Test: `packages/common/tests/test_option_collateral_pledge.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestTheSeamRefusesAnUncoveredCoveredCall:
    def test_a_covered_call_with_no_shares_is_refused_AT_THE_SEAM(
            self, options_account, short_call_leg):
        with pytest.raises(ValueError) as exc:
            options_account.submit_option_order(
                legs=[short_call_leg], quantity=1, option_strategy="covered_call")
        assert "cover" in str(exc.value).lower()

    def test_a_covered_call_WITH_shares_is_admitted(
            self, options_account_holding_100_aapl, short_call_leg):
        result = options_account_holding_100_aapl.submit_option_order(
            legs=[short_call_leg], quantity=1, option_strategy="covered_call")
        assert result is not None

    def test_an_UNMEASURABLE_cover_is_refused_not_assumed(
            self, options_account_book_raises, short_call_leg):
        """None must refuse. Permitting on unknown is the failure this seam prevents."""
        with pytest.raises(ValueError) as exc:
            options_account_book_raises.submit_option_order(
                legs=[short_call_leg], quantity=1, option_strategy="covered_call")
        assert "could not" in str(exc.value).lower() or "unmeasurable" in str(exc.value).lower()

    def test_a_NON_covered_call_strategy_is_unaffected(
            self, options_account, short_put_leg):
        result = options_account.submit_option_order(
            legs=[short_put_leg], quantity=1, option_strategy="cash_secured_put")
        assert result is not None

    def test_NOTHING_IS_PERSISTED_when_the_seam_refuses(
            self, options_account, short_call_leg):
        """Caller-obeys: the refusal precedes the parent order, the legs and the txn."""
        from ba2_common.core.trade_store import orders_where
        before = len(orders_where(account_id=options_account.id))
        with pytest.raises(ValueError):
            options_account.submit_option_order(
                legs=[short_call_leg], quantity=1, option_strategy="covered_call")
        assert len(orders_where(account_id=options_account.id)) == before
```

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  packages/common/tests/test_option_collateral_pledge.py::TestTheSeamRefusesAnUncoveredCoveredCall -q
```
Expected: 3 failed (the two "is admitted / unaffected" tests already pass).

- [ ] **Step 3: Implement**

In `submit_option_order`, directly after `if len(legs) > 4: raise ValueError(...)`:

```python
        # SEAM 2 (entry) — a covered call must have verified cover, checked HERE and not
        # only in SellCoveredCallAction.
        #
        # The action's own check (TradeActions.py:2462) is correct and stays. The problem
        # is that it is the ONLY caller performing it: PremiumSeller.rebalance already
        # calls this seam directly, and any future caller writes an uncovered short call
        # with no test at all. Placed before the parent order, the leg children and the
        # Transaction are written, so a refusal leaves nothing half-recorded.
        if option_strategy == "covered_call":
            _underlyings = {lg.underlying for lg in legs if lg.underlying}
            if len(_underlyings) != 1:
                raise ValueError(
                    f"covered_call requires exactly one underlying to verify cover "
                    f"against; got {sorted(_underlyings) or 'none'}.")
            _underlying = next(iter(_underlyings))
            _need = int(quantity) * 100
            _held = self.held_shares_for_cover(_underlying)
            if _held is None:
                raise ValueError(
                    f"Refusing a covered call on {_underlying}: the share position could "
                    f"not be measured, so cover is UNMEASURABLE. Refusing rather than "
                    f"assuming the shares are there — an uncovered short call has "
                    f"unbounded risk and needs an option approval tier this account may "
                    f"not hold. No order has been created."
                )
            if _held < _need:
                raise ValueError(
                    f"Refusing a covered call on {_underlying}: {quantity} contract(s) "
                    f"need {_need} shares of cover but the account holds {_held}. "
                    f"No order has been created."
                )
```

> **Note for the implementer:** `held_shares_for_cover(underlying)` does not exist yet. Add
> it to `OptionsAccountInterface` alongside `shares_pledged_to_short_calls`, tri-state on
> the same rules (`None` = unmeasurable, `0` = measured flat), reading the account's own
> equity position for that underlying. Do **not** reuse `TradeActions._held_equity_shares`
> — that one is scoped to a single expert (deliberately; see its docstring), and the seam
> must ask an account-wide question.

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest packages/common/tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py packages/common/tests/test_option_collateral_pledge.py
git commit -m "feat(options): the broker seam refuses an uncovered covered call (OPT-L1 entry half)"
```

---

### Task 7: The exit guard — never sell pledged cover

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py` (in `submit_close_order_for_transaction`, after the Task 1 guard and before the order is built)
- Test: `tests/test_accounts/test_account_interface.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestSellingPledgedCoverIsRefused:
    def test_selling_the_shares_behind_a_short_call_is_refused(self, monkeypatch):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        monkeypatch.setattr(type(account), "shares_pledged_to_short_calls",
                            lambda self, sym: 100, raising=False)
        txn = create_transaction(symbol="AAPL", quantity=100.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=100.0)
        result = account.submit_close_order_for_transaction(txn)
        assert result["success"] is False
        assert "pledged" in result["message"].lower()
        assert result["close_order_id"] is None

    def test_selling_UNPLEDGED_shares_is_allowed(self, monkeypatch):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        monkeypatch.setattr(type(account), "shares_pledged_to_short_calls",
                            lambda self, sym: 0, raising=False)
        txn = create_transaction(symbol="AAPL", quantity=100.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=100.0)
        assert account.submit_close_order_for_transaction(txn)["success"] is True

    def test_an_UNMEASURABLE_pledge_refuses_the_sell(self, monkeypatch):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        monkeypatch.setattr(type(account), "shares_pledged_to_short_calls",
                            lambda self, sym: None, raising=False)
        txn = create_transaction(symbol="AAPL", quantity=100.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=100.0)
        result = account.submit_close_order_for_transaction(txn)
        assert result["success"] is False
        assert "could not" in result["message"].lower()

    def test_an_account_that_cannot_hold_options_is_unaffected(self):
        """MockAccount is not an OptionsAccountInterface: nothing can be pledged."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=100.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=100.0)
        assert account.submit_close_order_for_transaction(txn)["success"] is True
```

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  tests/test_accounts/test_account_interface.py::TestSellingPledgedCoverIsRefused -q
```
Expected: `2 failed, 2 passed`.

- [ ] **Step 3: Implement**

In `submit_close_order_for_transaction`, after `current_qty = net`:

```python
        # SEAM 2 (exit) — never take an equity position below the shares pledged as cover
        # to an open short call.
        #
        # Entry checks cannot cover this: the shares can leave through a path that never
        # consults the option book. An account that cannot hold options has nothing
        # pledged, so the question is only asked of an OptionsAccountInterface.
        from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
        if isinstance(self, OptionsAccountInterface) and close_side == OrderDirection.SELL:
            pledged = self.shares_pledged_to_short_calls(transaction.symbol)
            if pledged is None:
                msg = (
                    f"Refusing to sell {current_qty} {transaction.symbol}: could not "
                    f"measure how many shares are pledged as cover to open short calls. "
                    f"Refusing rather than assuming none are — selling cover out from "
                    f"under a short call converts it to a naked call."
                )
                logger.error(f"submit_close_order_for_transaction: {msg}")
                return {"success": False, "message": msg, "close_order_id": None}
            if pledged > 0:
                held = self.held_shares_for_cover(transaction.symbol)
                if held is None or (held - current_qty) < pledged:
                    msg = (
                        f"Refusing to sell {current_qty} {transaction.symbol}: "
                        f"{pledged} share(s) are pledged as cover to open short calls "
                        f"and the account holds {held}. Close the short call first."
                    )
                    logger.error(f"submit_close_order_for_transaction: {msg}")
                    return {"success": False, "message": msg, "close_order_id": None}
```

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_accounts/ -q
```

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/AccountInterface.py tests/test_accounts/test_account_interface.py
git commit -m "fix(options): refuse to sell shares pledged as cover to an open short call (OPT-L1 exit half)"
```

---

### Task 8: A `cover_lost` exit reason

**Files:**
- Modify: `packages/common/ba2_common/core/option_lifecycle.py:79-86`
- Test: `packages/common/tests/test_option_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
class TestCoverLost:
    def test_a_covered_call_whose_cover_is_gone_is_told_to_close(self):
        from ba2_common.core.option_lifecycle import decide
        decision = decide(structure="covered_call", cover_shares_held=0,
                          cover_shares_required=100)
        assert decision.should_close is True
        assert decision.reason == "cover_lost"

    def test_a_covered_call_with_intact_cover_is_left_alone(self):
        from ba2_common.core.option_lifecycle import decide
        decision = decide(structure="covered_call", cover_shares_held=100,
                          cover_shares_required=100)
        assert decision.reason != "cover_lost"

    def test_UNMEASURABLE_cover_does_not_trigger_a_close(self):
        """Unknown must not be read as 'the cover is gone' -- that would liquidate a
        healthy position during a data outage. It alarms; it does not act."""
        from ba2_common.core.option_lifecycle import decide
        decision = decide(structure="covered_call", cover_shares_held=None,
                          cover_shares_required=100)
        assert decision.reason != "cover_lost"
        assert decision.unknown is True
```

> **Note for the implementer:** `decide()`'s real signature is in
> `packages/common/ba2_common/core/option_lifecycle.py`. Read it and add
> `cover_shares_held: Optional[int]` and `cover_shares_required: Optional[int]` as
> keyword-only parameters defaulting to `None`, so every existing caller is unaffected.
> Match the module's existing `LIFECYCLE_UNKNOWN` / `LifecycleDecision` conventions rather
> than inventing new ones.

- [ ] **Step 2: Run to verify it fails**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest \
  packages/common/tests/test_option_lifecycle.py::TestCoverLost -q
```

- [ ] **Step 3: Implement**

Add two keyword-only parameters to `decide()` — `cover_shares_held: Optional[int] = None` and
`cover_shares_required: Optional[int] = None` — so every existing caller is unaffected. Then add
this branch **before** the premium-based reasons, because a structure that has lost its cover
should close regardless of whether it is currently in profit:

```python
    # COVER LOST — the shares behind a covered call left without the option being closed.
    # Reached when a broker-side RM stop, another expert, a margin call or a rebalance
    # sold them while no platform code was running, so neither the entry check nor the
    # exit guard could see it.
    if structure == "covered_call" and cover_shares_required:
        if cover_shares_held is None:
            # UNMEASURABLE is not "the cover is gone". Liquidating a healthy position
            # because a quote feed hiccuped would be a self-inflicted loss. Alarm, do not act.
            return LifecycleDecision(should_close=False, reason=LIFECYCLE_UNKNOWN,
                                     unknown=True,
                                     detail="cover could not be measured")
        if cover_shares_held < cover_shares_required:
            return LifecycleDecision(
                should_close=True, reason="cover_lost",
                detail=(f"{cover_shares_held} share(s) held against "
                        f"{cover_shares_required} required — this call is no longer covered"))
```

Match the module's actual `LifecycleDecision` field names and its `LIFECYCLE_UNKNOWN` marker;
the shape above is the intent, not necessarily the exact constructor.

- [ ] **Step 4: Run to verify it passes**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest packages/common/tests/test_option_lifecycle.py -q
```

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_lifecycle.py packages/common/tests/test_option_lifecycle.py
git commit -m "feat(options): cover_lost lifecycle exit reason (OPT-L1)"
```

---

### Task 9: Wire the monitor into the live lifecycle pass

**Files:**
- Modify: `ba2_trade_platform/core/option_lifecycle_service.py`
- Test: `tests/test_option_cover_monitor.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_option_cover_monitor.py`:

```python
"""Seam 2 (continuous) — a covered call whose shares left overnight must be detected.

Entry and exit guards cannot see this case: a broker-side RM stop can fill while no
platform code is running.
"""
import pytest


class TestCoverLostMonitor:
    def test_a_covered_call_with_no_remaining_shares_is_flagged(self, live_service_with_naked_cc):
        result = live_service_with_naked_cc.run_option_lifecycle_pass()
        reasons = [d.reason for d in result.decisions]
        assert "cover_lost" in reasons

    def test_an_intact_covered_call_is_not_flagged(self, live_service_with_covered_cc):
        result = live_service_with_covered_cc.run_option_lifecycle_pass()
        assert "cover_lost" not in [d.reason for d in result.decisions]

    def test_an_unreadable_share_position_does_NOT_liquidate(self, live_service_cover_unreadable):
        """Loud, but it must not act on an unknown."""
        result = live_service_cover_unreadable.run_option_lifecycle_pass()
        assert "cover_lost" not in [d.reason for d in result.decisions]
```

> **Note for the implementer:** build the three fixtures against the real service with a
> doubled account. `run_option_lifecycle_pass`'s real name and return shape are in
> `ba2_trade_platform/core/option_lifecycle_service.py` — read them and match.

- [ ] **Step 2: Run to verify it fails**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_option_cover_monitor.py -q
```

- [ ] **Step 3: Implement**

In the loop over open option transactions inside `run_option_lifecycle_pass`, compute the two
cover figures and pass them into `decide()`:

```python
        # SEAM 2 (continuous) — the only place that can catch cover leaving between passes.
        cover_held = None
        cover_required = None
        if structure == "covered_call":
            cover_required = int(contracts) * int(multiplier)
            cover_held = account.held_shares_for_cover(underlying)
            if cover_held is None:
                logger.error(
                    f"Option lifecycle: could not measure the share position behind the "
                    f"covered call on {underlying} (transaction {txn.id}). Reporting "
                    f"UNKNOWN — NOT closing, because an unmeasurable cover is not a lost "
                    f"cover and liquidating on a failed read would be self-inflicted.")

        decision = decide(
            ...,                                   # existing arguments unchanged
            cover_shares_held=cover_held,
            cover_shares_required=cover_required,
        )
```

`contracts` and `multiplier` come from the transaction's option orders — reuse whatever the
pass already reads to build the book rather than re-querying. The pass already runs before
OPEN_POSITIONS (`JobManager.py:1466`), so a `cover_lost` decision is acted on before any new
entry is considered that cycle.

**Stand-down semantics:** `cover_lost` must suppress **entries, not exits** — the same rule
Task 8 of the original option plan established for the breaker. A book that has lost cover
must still be able to close.

- [ ] **Step 4: Run to verify it passes**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_option_cover_monitor.py -q
```

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/option_lifecycle_service.py tests/test_option_cover_monitor.py
git commit -m "feat(options): continuous cover-lost monitor in the lifecycle pass (OPT-L1)"
```

---

# SEAM 3 — per-leg close decisions

### Task 10: One leg settling must not close a multi-leg transaction

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:1187`
- Test: `tests/test_multileg_close_decision.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_multileg_close_decision.py`:

```python
"""Seam 3 — a multi-leg structure closes when EVERY contract is balanced, not when the
first leg's closing order fills.

One assigned/liquidated/expired leg currently closes the whole transaction, orphaning the
surviving legs -- including the protective long -- with nothing able to see them again.
"""
import pytest
from ba2_trade_platform.core.types import TransactionStatus


class TestOneLegDoesNotCloseTheStructure:
    def test_one_leg_of_a_strangle_settling_leaves_the_transaction_OPEN(
            self, account_with_short_strangle_one_leg_bought_back):
        account = account_with_short_strangle_one_leg_bought_back
        account.refresh_transactions()
        txn = account.reload_strangle_transaction()
        assert txn.status == TransactionStatus.OPENED

    def test_the_surviving_leg_is_still_visible(
            self, account_with_short_strangle_one_leg_bought_back):
        account = account_with_short_strangle_one_leg_bought_back
        account.refresh_transactions()
        assert len(account.get_option_positions()) == 1

    def test_the_transaction_closes_when_BOTH_legs_are_balanced(
            self, account_with_short_strangle_both_legs_bought_back):
        account = account_with_short_strangle_both_legs_bought_back
        account.refresh_transactions()
        txn = account.reload_strangle_transaction()
        assert txn.status == TransactionStatus.CLOSED

    def test_a_SINGLE_leg_option_still_closes_on_its_closing_fill(
            self, account_with_single_long_call_closed):
        """The fix must not strand single-leg positions, which have no sibling to wait on."""
        account = account_with_single_long_call_closed
        account.refresh_transactions()
        assert account.reload_call_transaction().status == TransactionStatus.CLOSED

    def test_an_EQUITY_transaction_is_completely_unaffected(
            self, account_with_equity_tp_filled):
        account = account_with_equity_tp_filled
        account.refresh_transactions()
        assert account.reload_equity_transaction().status == TransactionStatus.CLOSED
```

> **Note for the implementer:** the fixtures build real `Transaction` + `TradingOrder`
> rows through `tests/factories.py`. A short strangle is one parent (`option_strategy`
> set, no `contract_symbol`) plus two leg children with `parent_order_id` set and distinct
> `contract_symbol`s. "One leg bought back" means one BUY closing order FILLED against one
> of those contracts.

- [ ] **Step 2: Run to verify they fail**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_multileg_close_decision.py -q
```
Expected: the first two FAIL — the transaction is CLOSED with `close_reason="tp_sl_filled"` and the surviving leg has vanished. The last three pass.

- [ ] **Step 3: Implement**

`multi_leg_parent_ids`, `contract_net` and `position_balanced` are already computed above at `:1037-1053`. Before the `if/elif` chain that begins around `:1160`, add:

```python
                    # SEAM 3 — a multi-leg option structure closes when EVERY contract is
                    # balanced, not when the FIRST leg's closing order fills.
                    #
                    # An assignment, an expiry settlement or a one-leg margin liquidation
                    # produces a filled closing order for ONE contract. The
                    # `filled_closing_orders` branch below then closed the entire
                    # transaction, and the surviving legs -- including the protective long
                    # of a spread -- disappeared from get_option_positions() and
                    # _option_transaction_for_contract with nothing able to see or close
                    # them again, while the backtest kept charging their maintenance margin.
                    one_leg_of_many_settled = bool(multi_leg_parent_ids) and not position_balanced
```

Then change the branch at `:1187` from:

```python
                    elif filled_closing_orders and transaction.status == TransactionStatus.OPENED:
```

to:

```python
                    elif (filled_closing_orders
                          and transaction.status == TransactionStatus.OPENED
                          and not one_leg_of_many_settled):
```

- [ ] **Step 4: Run to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/test_multileg_close_decision.py -q
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest tests/ -q
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest packages/common/tests -q
```
Expected: all green, root suite at or above 4081.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py tests/test_multileg_close_decision.py
git commit -m "fix(options): one leg settling no longer closes a multi-leg structure (OPT-S3, OPT-S8)"
```

---

## Task 11: Mutation-harden the three seams

**Files:** no source changes expected; tests only if a mutant survives.

- [ ] **Step 1: Build the harness**

`PYTHONDONTWRITEBYTECODE=1`; purge `__pycache__` before and after every run; verify each restore with `git hash-object` against the pre-mutation hash; treat any run whose collected count differs from baseline as **INVALID**, never as killed. **Include a no-op comment control mutation in every batch — it MUST survive.** A killed control means the harness is broken; stop and fix it before trusting any verdict.

- [ ] **Step 2: Run these mutations. Every one must die.**

| # | Mutation | Guards |
|---|---|---|
| 1 | Delete the Task 1 `asset_class` guard | OPT-L3 |
| 2 | Invert it to `!= AssetClass.OPTION` | the equity path still works |
| 3 | Delete the Task 2 guard | OPT-L3 |
| 4 | Delete the Task 4 allocation filter | OPT-L2 |
| 5 | `shares_pledged_to_short_calls` returns `0` instead of `None` on an unreadable book | unknown ≠ zero |
| 6 | It returns `None` when the book is genuinely empty | zero ≠ unknown |
| 7 | It assumes `multiplier = 100` when absent | OPT-L7 leaking in |
| 8 | It counts short PUTS as pledging shares | wrong gate |
| 9 | It counts LONG calls | longs pledge nothing |
| 10 | The Task 6 entry guard permits when `held is None` | fail-open at entry |
| 11 | The Task 6 guard uses `<=` instead of `<` | boundary |
| 12 | The Task 7 exit guard permits when `pledged is None` | fail-open at exit |
| 13 | The Task 7 guard compares `held` instead of `held - current_qty` | off-by-a-position |
| 14 | `cover_lost` fires when `cover_shares_held is None` | must not liquidate on unknown |
| 15 | `one_leg_of_many_settled` forced to `False` | Seam 3 reverted |
| 16 | `one_leg_of_many_settled` forced to `True` | must not strand single legs |

- [ ] **Step 3:** For each survivor, decide equivalent vs real gap. A real gap gets a test, then re-run to kill. Record every survivor and your verdict.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test(options): mutation-harden the three option safety seams"
```

---

## Definition of done

- `tests/` ≥ 4081 passing, 0 failing; `packages/common` 1752; providers 424; experts 812.
- All 16 mutations killed, controls survived in every batch.
- No source file under `ba2_trade_platform/core/*.py` that is a re-export shim was edited.
- No broker was contacted by any test.
- Version files **not** bumped and nothing pushed — the coordinator handles both.
