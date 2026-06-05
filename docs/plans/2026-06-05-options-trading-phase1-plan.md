# Options Trading — Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the options *infrastructure* layer — a broker-agnostic `OptionsAccountInterface`, its `AlpacaAccount` implementation (chains/quotes/Greeks/IV, single + multi-leg submit, close, positions), option fields on `TradingOrder`, multiplier-aware position math, an IV-history primitive, and a fully-canned `MockAccount` options double — so later phases can add rule conditions/actions on top.

**Architecture:** `OptionsAccountInterface` is a **capability mixin ABC** (sibling to `AccountInterface`, *not* a subclass). Option-capable brokers inherit **both**: `class AlpacaAccount(AccountInterface, OptionsAccountInterface)`. Option holdings reuse `TradingOrder`/`Transaction` (nullable option columns; multi-leg = a parent option order with leg children via the existing `parent_order_id`), inheriting the existing lifecycle. Greeks + IV come straight from Alpaca's option snapshot; IV-rank is self-computed from a stored trailing ATM-IV series (Alpaca exposes no IV history).

**Tech Stack:** Python 3.12, SQLModel + Alembic (SQLite), `alpaca-py==0.43.2` (`TradingClient`, `OptionHistoricalDataClient`), pytest. Pure value objects as `@dataclass`es in `core/option_types.py`.

---

## ⚠️ Environment notes (read before running anything)

- **This Mac's venv is `venv/`, NOT `.venv/`.** CLAUDE.md's `.venv\Scripts\python.exe` is the Windows path. Here, **all commands use `venv/bin/python`.**
- **Run the suite in two groups** (a pre-existing Windows native access-violation can crash the all-at-once run around the penny tests — not our code):
  ```bash
  venv/bin/python -m pytest --ignore=tests/test_penny_entry.py --ignore=tests/test_penny_momentum_trader.py -q
  venv/bin/python -m pytest tests/test_penny_entry.py tests/test_penny_momentum_trader.py -q
  ```
  (Confirm the exact penny filenames with `ls tests | grep -i penny` before relying on them.)
- **Alembic single head is `a1f7e9c4b023`** (`add_manual_override_locks.py`). New migrations set `down_revision = 'a1f7e9c4b023'`. Re-verify with the head-finder snippet if other branches landed first.
- **Bump `ba2_trade_platform/version.py` build number before every push.** End commit messages with the `Co-Authored-By: Claude ...` line.
- **Work on a feature branch / worktree off `dev`, never directly on `dev`** (Task 0).

---

## Resolved open questions (decisions locked by this plan)

These were the gating questions; here is how Phase 1 answers them (evidence verified against installed `alpaca-py==0.43.2` and the live code).

1. **IV-rank source → self-compute.** Alpaca does **not** expose IV rank or any IV *history*. `get_option_snapshot` / `get_option_chain` return point-in-time `implied_volatility` + Greeks (`delta/gamma/rho/theta/vega`); `get_option_bars` returns price OHLCV only. **Decision:** Phase 1 ships the *primitive* — `get_atm_implied_volatility(underlying)` (current ATM IV from the chain) + an `OptionIVSnapshot` table + `record_atm_iv()` + `get_iv_rank()` (percentile over the stored window, returns `None` if fewer than `min_samples`). The scheduled daily recording job and the `iv_rank` rule **condition** are Phase 2 (they consume this primitive).

2. **Position model → extend `TradingOrder` (the locked default), confirmed viable.** Add nullable option columns; equity rows leave them null/`equity`. Multi-leg spreads = one **parent** option `TradingOrder` (`option_strategy` set, no `contract_symbol`) + **leg children** linked by the existing `parent_order_id`, each carrying its own contract. `legs_broker_ids` (already on the model) stores broker leg ids. No new position tables. The one real gap — the equity/quantity math assumes a 1× multiplier — is fixed in Task 3.

3. **Assignment & expiry → mechanism chosen, implementation deferred to advanced Phase C.** `alpaca-py==0.43.2` `TradingClient` has **no** `get_account_activities` (broker-only). Reconciliation must call `GET /v2/account/activities` directly and parse `ActivityType` codes `OPASN` (assignment), `OPEXC` (exercise), `OPEXP` (expiry), `OPCSH` (cash settle), paired with `OPTRD` underlying legs. **Paper syncs these NTAs next-day.** Attribution back to expert/transaction: option `TradingOrder.transaction_id → Transaction.expert_id`, matched on `underlying_symbol`/`contract_symbol`. Phase 1 only maps live option **positions** (`get_option_positions`) and refreshes option orders; full assignment reconciliation is Phase C (it subsumes the base plan's covered-call assignment question).

4. **Multi-leg order & P&L accounting.** Submit via `OrderClass.MLEG` with 2–4 `OptionLegRequest` legs (unique symbols, `qty` required, top-level `symbol` omitted); limit price sign convention: **positive = debit, negative = credit**. Stored as parent + children (above). Per-leg fills map onto the children; unit P&L (net debit/credit vs current combined mark) is computed from the children — a Phase 1 helper, refined in later phases.

5. **Wash-trade lock.** Option orders use distinct OCC symbols, so they fall outside the equity wash-trade gate by symbol. Phase 1 explicitly **excludes** option orders from the wash-lock candidate check (Task 6 routes option submission through a dedicated path, not equity `submit_order`).

---

## Component & file inventory (what Phase 1 creates/touches)

| Area | File | Action |
|---|---|---|
| Enums | `ba2_trade_platform/core/types.py` | Add `AssetClass`, `OptionRight` |
| Value objects | `ba2_trade_platform/core/option_types.py` | **Create** `OptionContract`, `OptionQuote`, `OptionLeg`, `OptionPosition` |
| Interface | `ba2_trade_platform/core/interfaces/OptionsAccountInterface.py` | **Create** the capability ABC |
| Interface export | `ba2_trade_platform/core/interfaces/__init__.py` | Export the new interface |
| Model fields | `ba2_trade_platform/core/models.py` | Add option columns to `TradingOrder`; add `OptionIVSnapshot` |
| Migration | `alembic/versions/<rev>_add_option_fields.py` | **Create** (down_revision `a1f7e9c4b023`) |
| Position math | `ba2_trade_platform/core/models.py` | Make `Transaction` equity math multiplier-aware |
| Alpaca impl | `ba2_trade_platform/modules/accounts/AlpacaAccount.py` | Implement `OptionsAccountInterface` |
| Test double | `tests/conftest.py` | Extend `MockAccount` to implement `OptionsAccountInterface` |
| Tests | `tests/test_option_types.py`, `tests/test_options_account_interface.py`, `tests/test_options_trading_order.py`, `tests/test_alpaca_options.py`, `tests/test_option_iv_history.py` | **Create** |
| Paper validation | `test_files/validate_options_paper.py` | **Create** (manual, not pytest) |

---

## Task 0: Branch/worktree setup

**Step 1:** From `dev`, create an isolated branch (or worktree). Do **not** commit to `dev`.
```bash
git checkout dev && git pull origin dev
git checkout -b feature/options-trading-phase1
```
(Or, if using worktrees per repo convention: create a worktree off `dev` and work there.)

**Step 2:** Confirm baseline green before changing anything:
```bash
ls tests | grep -i penny    # confirm penny filenames
venv/bin/python -m pytest --ignore=tests/test_penny_entry.py --ignore=tests/test_penny_momentum_trader.py -q
```
Expected: PASS (record the count as your baseline).

---

## Task 1: Option value objects (`core/option_types.py`)

Pure, broker-agnostic dataclasses + the two enums. No DB, no Alpaca imports — trivially testable.

**Files:**
- Modify: `ba2_trade_platform/core/types.py` (add enums)
- Create: `ba2_trade_platform/core/option_types.py`
- Test: `tests/test_option_types.py`

**Step 1: Write the failing test** — `tests/test_option_types.py`:
```python
from datetime import date
from ba2_trade_platform.core.types import AssetClass, OptionRight
from ba2_trade_platform.core.option_types import (
    OptionContract, OptionQuote, OptionLeg, OptionPosition,
)
from ba2_trade_platform.core.types import OrderDirection


def test_asset_class_and_right_values():
    assert AssetClass.EQUITY.value == "equity"
    assert AssetClass.OPTION.value == "option"
    assert OptionRight.CALL.value == "call"
    assert OptionRight.PUT.value == "put"


def _contract(**kw):
    base = dict(
        symbol="AAPL260116C00150000", underlying="AAPL",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        bid=5.0, ask=5.4, last=5.2, implied_volatility=0.32,
        delta=0.55, gamma=0.02, theta=-0.04, vega=0.10,
        open_interest=1200, volume=300,
    )
    base.update(kw)
    return OptionContract(**base)


def test_contract_mid_and_spread_pct():
    c = _contract()
    assert c.mid == 5.2
    # spread_pct = (ask-bid)/mid*100 = 0.4/5.2*100
    assert round(c.spread_pct, 4) == round(0.4 / 5.2 * 100, 4)


def test_contract_mid_none_when_quote_missing():
    c = _contract(bid=None, ask=None)
    assert c.mid is None
    assert c.spread_pct is None


def test_leg_defaults_ratio_one():
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY)
    assert leg.ratio_qty == 1
    assert leg.position_intent is None


def test_option_position_fields():
    p = OptionPosition(
        contract_symbol="AAPL260116C00150000", underlying="AAPL",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        side=OrderDirection.BUY, quantity=2, avg_entry_price=5.2,
        current_price=6.0, market_value=1200.0, unrealized_pl=160.0,
    )
    assert p.multiplier == 100
    assert p.quantity == 2
```

**Step 2: Run — expect failure** (`ModuleNotFoundError` / `ImportError`):
```bash
venv/bin/python -m pytest tests/test_option_types.py -q
```

**Step 3: Implement.** Append to `ba2_trade_platform/core/types.py` (near `InstrumentType`, ~line 221):
```python
class AssetClass(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"
```
Create `ba2_trade_platform/core/option_types.py`:
```python
"""Broker-agnostic option value objects (pure dataclasses, no DB/SDK deps)."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .types import OptionRight, OrderDirection


@dataclass
class OptionContract:
    """One row of an option chain (quote + Greeks + liquidity)."""
    symbol: str                       # OCC contract symbol
    underlying: str
    option_type: OptionRight
    strike: float
    expiry: date
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return None  # never proxy mid from last trade (stale on illiquid options)

    @property
    def spread_pct(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        m = self.mid
        if not m:
            return None
        return (self.ask - self.bid) / m * 100


@dataclass
class OptionQuote:
    """Latest quote + Greeks for a single contract."""
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    timestamp: Optional[datetime] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return None  # never proxy mid from last trade (stale on illiquid options)


@dataclass
class OptionLeg:
    """One leg of an option order. ratio_qty multiplies the order quantity."""
    contract_symbol: str
    side: OrderDirection
    ratio_qty: int = 1
    position_intent: Optional[str] = None     # buy_to_open / sell_to_open / ...
    option_type: Optional[OptionRight] = None
    strike: Optional[float] = None
    expiry: Optional[date] = None
    underlying: Optional[str] = None


@dataclass
class OptionPosition:
    """A held option position (broker-agnostic)."""
    contract_symbol: str
    underlying: str
    option_type: OptionRight
    strike: float
    expiry: date
    side: OrderDirection                       # BUY = long, SELL = short
    quantity: float                            # number of contracts (positive)
    avg_entry_price: float                     # premium per share
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pl: Optional[float] = None
    multiplier: int = 100
```
Ensure `Enum` is imported in `types.py` (it already uses `from enum import Enum`).

**Step 4: Run — expect PASS:**
```bash
venv/bin/python -m pytest tests/test_option_types.py -q
```

**Step 5: Commit:**
```bash
git add ba2_trade_platform/core/types.py ba2_trade_platform/core/option_types.py tests/test_option_types.py
git commit -m "feat(options): add AssetClass/OptionRight enums and option value objects"
```

---

## Task 2: Option fields on `TradingOrder` + migration

**Files:**
- Modify: `ba2_trade_platform/core/models.py` (`TradingOrder`, ~after line 465; imports line 5–6)
- Create: `alembic/versions/<rev>_add_option_fields_to_tradingorder.py`
- Test: `tests/test_options_trading_order.py`

**Step 1: Write the failing test** — `tests/test_options_trading_order.py`:
```python
from datetime import date
from ba2_trade_platform.core.db import get_instance, add_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderType, OrderStatus,
)


def test_equity_order_defaults_to_equity_asset_class(mock_account_def):
    oid = add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=10,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
    ))
    o = get_instance(TradingOrder, oid)
    assert o.asset_class == AssetClass.EQUITY
    assert o.contract_symbol is None
    assert o.multiplier is None


def test_option_order_persists_contract_metadata(mock_account_def):
    oid = add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.PENDING, limit_price=5.2,
        asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00150000",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        underlying_symbol="AAPL", multiplier=100,
        position_intent="buy_to_open", option_strategy="long_call",
    ))
    o = get_instance(TradingOrder, oid)
    assert o.asset_class == AssetClass.OPTION
    assert o.contract_symbol == "AAPL260116C00150000"
    assert o.option_type == OptionRight.CALL
    assert o.strike == 150.0
    assert o.expiry == date(2026, 1, 16)
    assert o.underlying_symbol == "AAPL"
    assert o.multiplier == 100
    assert o.position_intent == "buy_to_open"
    assert o.option_strategy == "long_call"
```

**Step 2: Run — expect failure** (unexpected keyword args):
```bash
venv/bin/python -m pytest tests/test_options_trading_order.py -q
```

**Step 3: Implement.**
- In `models.py` line 5, add `AssetClass, OptionRight` to the `from .types import ...` list.
- In `models.py` line 6, change to also import `date`:
  ```python
  from datetime import datetime as DateTime, timezone, date
  ```
- In `TradingOrder`, insert after the OCO block (after line 465, before `def as_string`):
  ```python
      # --- Options fields (nullable; equity orders leave these unset) ---
      asset_class: AssetClass = Field(default=AssetClass.EQUITY, index=True, description="equity | option")
      contract_symbol: str | None = Field(default=None, index=True, description="OCC option contract symbol (single-leg)")
      option_type: OptionRight | None = Field(default=None, description="call | put for option legs")
      strike: float | None = Field(default=None, description="Option strike price")
      expiry: date | None = Field(default=None, description="Option expiration date")
      underlying_symbol: str | None = Field(default=None, index=True, description="Underlying equity symbol for options")
      multiplier: int | None = Field(default=None, description="Contract multiplier (100 for standard equity options)")
      position_intent: str | None = Field(default=None, description="Alpaca position intent: buy_to_open/sell_to_open/buy_to_close/sell_to_close")
      option_strategy: str | None = Field(default=None, description="Strategy tag on the parent order: long_call/bull_call_spread/covered_call/...")
  ```

**Step 4: Run model test (tests use `create_all`, so they pass without the migration) — expect PASS:**
```bash
venv/bin/python -m pytest tests/test_options_trading_order.py -q
```

**Step 5: Create the migration** — `alembic/versions/<rev>_add_option_fields_to_tradingorder.py`.
Generate via autogenerate **or** hand-write (preferred here for SQLite safety). Hand-written body:
```python
"""add option fields to tradingorder

Revision ID: <rev>
Revises: a1f7e9c4b023
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "<rev>"            # use the generated hash if autogenerated
down_revision: Union[str, Sequence[str], None] = "a1f7e9c4b023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tradingorder", sa.Column("asset_class", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("contract_symbol", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("option_type", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("strike", sa.Float(), nullable=True))
    op.add_column("tradingorder", sa.Column("expiry", sa.Date(), nullable=True))
    op.add_column("tradingorder", sa.Column("underlying_symbol", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("multiplier", sa.Integer(), nullable=True))
    op.add_column("tradingorder", sa.Column("position_intent", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("option_strategy", sa.String(), nullable=True))
    # Backfill existing equity rows
    op.execute("UPDATE tradingorder SET asset_class = 'equity' WHERE asset_class IS NULL")
    op.create_index("ix_tradingorder_contract_symbol", "tradingorder", ["contract_symbol"])
    op.create_index("ix_tradingorder_underlying_symbol", "tradingorder", ["underlying_symbol"])
    op.create_index("ix_tradingorder_asset_class", "tradingorder", ["asset_class"])


def downgrade():
    op.drop_index("ix_tradingorder_asset_class", table_name="tradingorder")
    op.drop_index("ix_tradingorder_underlying_symbol", table_name="tradingorder")
    op.drop_index("ix_tradingorder_contract_symbol", table_name="tradingorder")
    for col in ("option_strategy", "position_intent", "multiplier", "underlying_symbol",
                "expiry", "strike", "option_type", "contract_symbol", "asset_class"):
        op.drop_column("tradingorder", col)
```
Prefer `python migrate.py create "add option fields to tradingorder"` then edit the generated file to match the columns above (autogenerate may miss `server_default`/index nuances). Verify it applies against a **copy** of the dev DB:
```bash
cp ~/Documents/ba2_trade_platform/db.sqlite /tmp/db_test.sqlite
# point a throwaway run at the copy, or:
venv/bin/python migrate.py upgrade   # against dev DB only when confident; back it up first
venv/bin/python migrate.py current
```

**Step 6: Commit:**
```bash
git add ba2_trade_platform/core/models.py alembic/versions/*add_option_fields* tests/test_options_trading_order.py
git commit -m "feat(options): add option metadata fields to TradingOrder + migration"
```

---

## Task 3: Multiplier-aware position equity math

Option premium risk = `premium × multiplier(100) × contracts`, and pending option equity must use the **option premium** (limit price), not the underlying price. Fix both `Transaction` equity helpers.

**Files:**
- Modify: `ba2_trade_platform/core/models.py` (`get_current_open_equity` ~line 308; `get_pending_open_equity` ~line 360–382)
- Test: add to `tests/test_options_trading_order.py`

**Step 1: Write the failing test** (append to `tests/test_options_trading_order.py`):
```python
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import TransactionStatus


def test_current_open_equity_applies_option_multiplier(mock_account_def):
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=5.2,
    ))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=2, open_price=5.2,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, multiplier=100,
    ))
    txn = get_instance(Transaction, txn_id)
    # 2 contracts * $5.2 premium * 100 multiplier = $1040
    assert txn.get_current_open_equity() == 1040.0


def test_current_open_equity_equity_order_unchanged(mock_account_def):
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=10, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=150.0,
    ))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=10,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=10, open_price=150.0,
        transaction_id=txn_id,  # asset_class defaults to equity, multiplier None
    ))
    txn = get_instance(Transaction, txn_id)
    assert txn.get_current_open_equity() == 1500.0
```

**Step 2: Run — expect failure** (option case returns 10.4, not 1040):
```bash
venv/bin/python -m pytest tests/test_options_trading_order.py -k equity -q
```

**Step 3: Implement.** In `get_current_open_equity` (line 308) change:
```python
                        equity = abs(order.filled_qty) * price
```
to:
```python
                        equity = abs(order.filled_qty) * price * (order.multiplier or 1)
```
In `get_pending_open_equity`, replace the per-order body (lines ~360–383) so option orders use their own premium (`limit_price`) × multiplier and skip the underlying-price path:
```python
            for order in orders:
                if order.status in OrderStatus.get_unfilled_statuses():
                    if order.depends_on_order is not None:
                        continue
                    from .types import OrderType, AssetClass
                    if order.order_type in [OrderType.SELL_LIMIT, OrderType.BUY_LIMIT, OrderType.OCO, OrderType.SELL_STOP, OrderType.BUY_STOP] and order.asset_class != AssetClass.OPTION:
                        # equity exit orders (TP/SL) don't use buying power; option limits DO open positions
                        continue
                    remaining_qty = order.quantity
                    if order.filled_qty:
                        remaining_qty -= order.filled_qty
                    if remaining_qty > 0:
                        if order.asset_class == AssetClass.OPTION:
                            premium = order.limit_price or order.open_price
                            if premium:
                                total_equity += abs(remaining_qty) * premium * (order.multiplier or 100)
                        else:
                            total_equity += abs(remaining_qty) * market_price
```
> Note: `get_pending_open_equity` returns 0 early when `market_price` is falsy (line 347–349). Since option pending equity does **not** depend on the underlying market price, move the option branch to not require `market_price` — if all pending orders are options, compute from premium regardless. Adjust the early-return guard: only `return 0.0` if `market_price` is None **and** there are no option orders. Implement defensively; cover with the pending-equity test below.

**Step 3b:** Add a pending-equity option test asserting `get_pending_open_equity` returns `contracts × limit_price × 100` for a pending option order even with no underlying price.

**Step 4: Run — expect PASS** (run the full file):
```bash
venv/bin/python -m pytest tests/test_options_trading_order.py -q
```

**Step 5: Commit:**
```bash
git add ba2_trade_platform/core/models.py tests/test_options_trading_order.py
git commit -m "feat(options): make Transaction equity math option-multiplier aware"
```

---

## Task 4: `OptionsAccountInterface` capability ABC

A sibling capability interface. Defines the surface + a concrete `submit_option_order` (persistence) that delegates to an abstract `_submit_option_order_impl` (broker call) — mirroring `AccountInterface.submit_order` / `_submit_order_impl`. Intended to be mixed into an `AccountInterface` subclass (so `self._create_transaction_for_order`, `self.id`, `add_instance` etc. are available at runtime).

**Files:**
- Create: `ba2_trade_platform/core/interfaces/OptionsAccountInterface.py`
- Modify: `ba2_trade_platform/core/interfaces/__init__.py`
- Test: `tests/test_options_account_interface.py` (interface-shape tests; behavior tested via Mock in Task 5–6)

**Step 1: Write the failing test** — `tests/test_options_account_interface.py`:
```python
import inspect
import pytest
from ba2_trade_platform.core.interfaces import OptionsAccountInterface


def test_is_abstract_capability_interface():
    assert inspect.isabstract(OptionsAccountInterface)
    assert OptionsAccountInterface.supports_options is True
    with pytest.raises(TypeError):
        OptionsAccountInterface()  # abstract, cannot instantiate


def test_declares_expected_surface():
    for name in (
        "get_option_chain", "get_option_quote", "get_atm_implied_volatility",
        "get_option_positions", "submit_option_order", "_submit_option_order_impl",
        "close_option_position", "get_iv_rank",
    ):
        assert hasattr(OptionsAccountInterface, name), name
```

**Step 2: Run — expect failure** (ImportError):
```bash
venv/bin/python -m pytest tests/test_options_account_interface.py -q
```

**Step 3: Implement** `ba2_trade_platform/core/interfaces/OptionsAccountInterface.py`:
```python
"""Options capability interface — a sibling mixin to AccountInterface.

Brokers that support options inherit BOTH, e.g.:
    class AlpacaAccount(AccountInterface, OptionsAccountInterface): ...

Capability detection elsewhere should use isinstance(account, OptionsAccountInterface).
The concrete submit_option_order() owns TradingOrder/Transaction persistence and
delegates the broker call to the abstract _submit_option_order_impl().
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Optional

from ..option_types import OptionContract, OptionQuote, OptionLeg, OptionPosition
from ..types import OptionRight, OrderType


class OptionsAccountInterface(ABC):
    """Mixin granting an AccountInterface subclass option-trading capability."""

    supports_options: bool = True

    # --- Market data -------------------------------------------------------
    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        expiry_min: date,
        expiry_max: date,
        option_type: Optional[OptionRight] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
    ) -> List[OptionContract]:
        """Return chain rows (quote + Greeks + liquidity) within the filters."""
        ...

    @abstractmethod
    def get_option_quote(self, contract_symbol: str) -> Optional[OptionQuote]:
        """Latest quote + Greeks for one OCC contract."""
        ...

    @abstractmethod
    def get_atm_implied_volatility(self, underlying: str) -> Optional[float]:
        """Current near-ATM implied volatility for the underlying (0–1)."""
        ...

    # --- Positions ---------------------------------------------------------
    @abstractmethod
    def get_option_positions(self) -> List[OptionPosition]:
        """All currently-held option positions."""
        ...

    # --- Orders ------------------------------------------------------------
    @abstractmethod
    def _submit_option_order_impl(self, trading_order, legs: List[OptionLeg],
                                  leg_orders: Optional[List[Any]] = None) -> Any:
        """Broker-specific submit. Receives the persisted parent TradingOrder and
        the legs; must set broker ids/status and return the parent order."""
        ...

    def submit_option_order(
        self,
        legs: List[OptionLeg],
        quantity: int,
        order_type: str = "limit",            # "market" | "limit"
        limit_price: Optional[float] = None,   # premium; +debit / -credit for spreads
        option_strategy: Optional[str] = None,
        expert_recommendation_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
    ) -> Any:
        """Build & persist option TradingOrder(s), then submit to the broker.

        single leg -> one option TradingOrder (contract_symbol set)
        2–4 legs   -> a parent option order (option_strategy set, no contract_symbol)
                      + leg children linked via parent_order_id.
        """
        from ..db import add_instance, get_instance
        from ..models import TradingOrder
        from ..types import AssetClass, OrderDirection, OrderType as CoreOrderType, OrderStatus
        from ...logger import logger

        if not legs:
            raise ValueError("submit_option_order requires at least one leg")
        if len(legs) > 4:
            raise ValueError("Alpaca supports a maximum of 4 option legs")

        is_multi = len(legs) > 1
        # Map order_type + net direction to the directional CoreOrderType
        net_side = OrderDirection.BUY if (limit_price is None or limit_price >= 0) else OrderDirection.SELL
        if order_type == "market":
            core_type = CoreOrderType.MARKET
        else:
            core_type = CoreOrderType.BUY_LIMIT if net_side == OrderDirection.BUY else CoreOrderType.SELL_LIMIT

        first = legs[0]
        parent = TradingOrder(
            account_id=self.id,
            symbol=(first.underlying or first.contract_symbol),
            underlying_symbol=first.underlying,
            quantity=quantity,
            side=(first.side if not is_multi else net_side),
            order_type=core_type,
            status=OrderStatus.PENDING,
            limit_price=limit_price,
            asset_class=AssetClass.OPTION,
            multiplier=100,
            option_strategy=option_strategy or ("spread" if is_multi else "single"),
            position_intent=(first.position_intent if not is_multi else None),
            contract_symbol=(first.contract_symbol if not is_multi else None),
            option_type=(first.option_type if not is_multi else None),
            strike=(first.strike if not is_multi else None),
            expiry=(first.expiry if not is_multi else None),
            expert_recommendation_id=expert_recommendation_id,
            transaction_id=transaction_id,
        )
        parent_id = add_instance(parent, expunge_after_flush=True)
        parent = get_instance(TradingOrder, parent_id)

        # Create/link a Transaction so OPEN_POSITIONS rules can manage the position.
        if parent.transaction_id is None and hasattr(self, "_create_transaction_for_order"):
            self._create_transaction_for_order(parent)

        leg_orders = []
        if is_multi:
            for leg in legs:
                child = TradingOrder(
                    account_id=self.id,
                    symbol=leg.contract_symbol,
                    underlying_symbol=leg.underlying,
                    quantity=quantity * leg.ratio_qty,
                    side=leg.side,
                    order_type=CoreOrderType.MARKET if order_type == "market" else (
                        CoreOrderType.BUY_LIMIT if leg.side == OrderDirection.BUY else CoreOrderType.SELL_LIMIT),
                    status=OrderStatus.PENDING,
                    asset_class=AssetClass.OPTION,
                    multiplier=100,
                    contract_symbol=leg.contract_symbol,
                    option_type=leg.option_type,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    position_intent=leg.position_intent,
                    parent_order_id=parent.id,
                    transaction_id=parent.transaction_id,
                )
                leg_orders.append(get_instance(TradingOrder, add_instance(child, expunge_after_flush=True)))

        try:
            return self._submit_option_order_impl(parent, legs, leg_orders or None)
        except Exception as e:
            logger.error(f"Option order submission failed for {parent.symbol}: {e}", exc_info=True)
            parent.status = OrderStatus.ERROR
            parent.comment = f"{(parent.comment or '')} | option submit error: {str(e)[:200]}"
            from ..db import update_instance
            update_instance(parent)
            return None

    @abstractmethod
    def close_option_position(self, position: OptionPosition,
                              order_type: str = "limit",
                              limit_price: Optional[float] = None) -> Any:
        """Submit a closing order for a held option position (opposite intent)."""
        ...

    # --- IV rank (self-computed from stored ATM-IV history) ----------------
    @abstractmethod
    def get_iv_rank(self, underlying: str, lookback_days: int = 252,
                    min_samples: int = 20) -> Optional[float]:
        """IV percentile (0–100) over the stored trailing window, or None if
        insufficient history."""
        ...
```
Add to `ba2_trade_platform/core/interfaces/__init__.py`:
```python
from .OptionsAccountInterface import OptionsAccountInterface
```
…and add `"OptionsAccountInterface"` to `__all__`.

**Step 4: Run — expect PASS:**
```bash
venv/bin/python -m pytest tests/test_options_account_interface.py -q
```

**Step 5: Commit:**
```bash
git add ba2_trade_platform/core/interfaces/OptionsAccountInterface.py ba2_trade_platform/core/interfaces/__init__.py tests/test_options_account_interface.py
git commit -m "feat(options): add OptionsAccountInterface capability mixin"
```

---

## Task 5: Extend `MockAccount` with canned option data

Make `MockAccount` implement `OptionsAccountInterface` so the rest of Phase 1 (and Phases 2–4) can test option flows without a broker.

**Files:**
- Modify: `tests/conftest.py` (`class MockAccount`)
- Test: add to `tests/test_options_account_interface.py`

**Step 1: Write the failing test** (append):
```python
from datetime import date
from ba2_trade_platform.core.interfaces import OptionsAccountInterface
from ba2_trade_platform.core.types import OptionRight, OrderDirection


def test_mock_account_is_option_capable(mock_account):
    assert isinstance(mock_account, OptionsAccountInterface)
    assert mock_account.supports_options is True


def test_mock_chain_and_quote(mock_account):
    chain = mock_account.get_option_chain(
        "AAPL", date(2026, 1, 1), date(2026, 3, 1), OptionRight.CALL)
    assert len(chain) > 0
    c = chain[0]
    assert c.underlying == "AAPL"
    assert c.option_type == OptionRight.CALL
    assert c.delta is not None and c.implied_volatility is not None
    q = mock_account.get_option_quote(c.symbol)
    assert q is not None and q.symbol == c.symbol


def test_mock_atm_iv(mock_account):
    iv = mock_account.get_atm_implied_volatility("AAPL")
    assert 0 < iv < 2
```

**Step 2: Run — expect failure** (MockAccount not an OptionsAccountInterface):
```bash
venv/bin/python -m pytest tests/test_options_account_interface.py -k mock -q
```

**Step 3: Implement.** In `tests/conftest.py`:
- Change the class declaration:
  ```python
  from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
  class MockAccount(AccountInterface, OptionsAccountInterface):
  ```
- In `__init__`, add canned option state:
  ```python
          self._option_positions = []          # list[OptionPosition]
          self._submitted_option_orders = []   # capture for assertions
          self._atm_iv = {"AAPL": 0.30, "MSFT": 0.28, "GOOGL": 0.33}
  ```
- Add methods (canned, deterministic):
  ```python
      def _mk_contract(self, underlying, right, strike, expiry):
          from ba2_trade_platform.core.option_types import OptionContract
          spot = self._prices.get(underlying, 100.0)
          intrinsic = max(0.0, (spot - strike) if right.value == "call" else (strike - spot))
          mid = round(intrinsic + 2.0, 2)
          occ = f"{underlying}{expiry:%y%m%d}{'C' if right.value=='call' else 'P'}{int(strike*1000):08d}"
          return OptionContract(
              symbol=occ, underlying=underlying, option_type=right, strike=strike, expiry=expiry,
              bid=mid - 0.1, ask=mid + 0.1, last=mid, implied_volatility=0.30,
              delta=0.5, gamma=0.02, theta=-0.03, vega=0.1, open_interest=1000, volume=250)

      def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                           strike_min=None, strike_max=None):
          from ba2_trade_platform.core.types import OptionRight
          expiry = expiry_max
          spot = self._prices.get(underlying, 100.0)
          rights = [option_type] if option_type else [OptionRight.CALL, OptionRight.PUT]
          out = []
          for r in rights:
              for k in (round(spot * 0.95), round(spot), round(spot * 1.05)):
                  if strike_min and k < strike_min:  # honor filters
                      continue
                  if strike_max and k > strike_max:
                      continue
                  out.append(self._mk_contract(underlying, r, float(k), expiry))
          return out

      def get_option_quote(self, contract_symbol):
          from ba2_trade_platform.core.option_types import OptionQuote
          return OptionQuote(symbol=contract_symbol, bid=2.0, ask=2.2, last=2.1,
                             implied_volatility=0.30, delta=0.5, gamma=0.02, theta=-0.03, vega=0.1)

      def get_atm_implied_volatility(self, underlying):
          return self._atm_iv.get(underlying, 0.30)

      def get_option_positions(self):
          return self._option_positions

      def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
          from ba2_trade_platform.core.types import OrderStatus
          trading_order.status = OrderStatus.FILLED
          trading_order.filled_qty = trading_order.quantity
          trading_order.broker_order_id = f"mock-opt-{trading_order.id}"
          if leg_orders:
              for i, lo in enumerate(leg_orders):
                  lo.status = OrderStatus.FILLED
                  lo.filled_qty = lo.quantity
                  lo.broker_order_id = f"mock-opt-{trading_order.id}-leg{i}"
          self._submitted_option_orders.append(trading_order)
          return trading_order

      def close_option_position(self, position, order_type="limit", limit_price=None):
          from ba2_trade_platform.core.option_types import OptionLeg
          from ba2_trade_platform.core.types import OrderDirection
          close_side = OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
          intent = "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
          leg = OptionLeg(contract_symbol=position.contract_symbol, side=close_side,
                          position_intent=intent, option_type=position.option_type,
                          strike=position.strike, expiry=position.expiry, underlying=position.underlying)
          return self.submit_option_order([leg], int(position.quantity), order_type, limit_price,
                                          option_strategy="close")

      def get_iv_rank(self, underlying, lookback_days=252, min_samples=20):
          return 50.0  # deterministic stub for tests
  ```

**Step 4: Run — expect PASS** (and re-run the whole interface test file):
```bash
venv/bin/python -m pytest tests/test_options_account_interface.py -q
```

**Step 5: Commit:**
```bash
git add tests/conftest.py tests/test_options_account_interface.py
git commit -m "test(options): make MockAccount implement OptionsAccountInterface with canned data"
```

---

## Task 6: `submit_option_order` persistence behavior (via Mock)

Verify the concrete persistence logic (single-leg → one order; multi-leg → parent + children; Transaction created) using the Mock's broker impl.

**Files:**
- Test: `tests/test_options_account_interface.py` (append)

**Step 1: Write the failing test** (it will pass only once Task 4+5 are correct; treat any failure as a real defect to fix in Task 4's `submit_option_order`):
```python
from datetime import date
from sqlmodel import select
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.option_types import OptionLeg
from ba2_trade_platform.core.types import OrderDirection, OptionRight, OrderStatus, AssetClass


def test_single_leg_long_call_creates_one_order_and_txn(mock_account):
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                    position_intent="buy_to_open", option_type=OptionRight.CALL,
                    strike=150.0, expiry=date(2026, 1, 16), underlying="AAPL")
    result = mock_account.submit_option_order([leg], quantity=2, order_type="limit",
                                              limit_price=5.2, option_strategy="long_call")
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert result.asset_class == AssetClass.OPTION
    assert result.contract_symbol == "AAPL260116C00150000"
    assert result.transaction_id is not None
    with get_db() as s:
        txn = s.get(Transaction, result.transaction_id)
        assert txn is not None


def test_bull_call_spread_creates_parent_and_two_children(mock_account):
    long_leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                         position_intent="buy_to_open", option_type=OptionRight.CALL,
                         strike=150.0, expiry=date(2026, 1, 16), underlying="AAPL")
    short_leg = OptionLeg(contract_symbol="AAPL260116C00160000", side=OrderDirection.SELL,
                          position_intent="sell_to_open", option_type=OptionRight.CALL,
                          strike=160.0, expiry=date(2026, 1, 16), underlying="AAPL")
    parent = mock_account.submit_option_order([long_leg, short_leg], quantity=1,
                                              order_type="limit", limit_price=4.0,
                                              option_strategy="bull_call_spread")
    assert parent.contract_symbol is None
    assert parent.option_strategy == "bull_call_spread"
    with get_db() as s:
        children = s.exec(select(TradingOrder).where(
            TradingOrder.parent_order_id == parent.id)).all()
        assert len(children) == 2
        assert {c.contract_symbol for c in children} == {
            "AAPL260116C00150000", "AAPL260116C00160000"}
        assert all(c.transaction_id == parent.transaction_id for c in children)
```

**Step 2: Run:**
```bash
venv/bin/python -m pytest tests/test_options_account_interface.py -k "single_leg or spread" -q
```
Expected: PASS (if not, fix `submit_option_order` in Task 4 until green — that's the point of this task).

**Step 3:** No new impl if Task 4 is correct; otherwise fix and re-run.

**Step 4: Commit:**
```bash
git add tests/test_options_account_interface.py
git commit -m "test(options): cover single + multi-leg option order persistence"
```

---

## Task 7: AlpacaAccount — chain / quote / ATM-IV

Implement the market-data side using `OptionHistoricalDataClient`. Greeks + IV come directly from `OptionsSnapshot`.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (class decl line 65; imports; new methods)
- Test: `tests/test_alpaca_options.py`

**Step 1: Write the failing test** — `tests/test_alpaca_options.py`. Build an AlpacaAccount with settings stubbed and the option data client monkeypatched to return canned snapshots:
```python
from datetime import date
from types import SimpleNamespace
import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.interfaces import OptionsAccountInterface
from ba2_trade_platform.core.types import OptionRight


def _make_alpaca(monkeypatch):
    acct = AlpacaAccount.__new__(AlpacaAccount)        # bypass __init__/DB
    acct.id = 999
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    return acct


def test_alpaca_is_option_capable():
    assert issubclass(AlpacaAccount, OptionsAccountInterface)


def test_get_option_chain_maps_snapshot(monkeypatch):
    acct = _make_alpaca(monkeypatch)
    greeks = SimpleNamespace(delta=0.55, gamma=0.02, theta=-0.04, vega=0.1, rho=0.01)
    quote = SimpleNamespace(bid_price=5.0, ask_price=5.4, timestamp=None)
    trade = SimpleNamespace(price=5.2)
    snap = SimpleNamespace(latest_quote=quote, latest_trade=trade,
                           implied_volatility=0.32, greeks=greeks)
    contracts = {"AAPL260116C00150000": snap}

    class FakeOptClient:
        def __init__(self, *a, **k): pass
        def get_option_chain(self, req): return contracts

    # contract metadata comes from the trading client master list
    occ = SimpleNamespace(symbol="AAPL260116C00150000", underlying_symbol="AAPL",
                          type=SimpleNamespace(value="call"), strike_price=150.0,
                          expiration_date=date(2026, 1, 16), open_interest="1200")
    monkeypatch.setattr(acct, "_option_data_client", FakeOptClient(), raising=False)
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {"AAPL260116C00150000": occ}, raising=False)

    chain = acct.get_option_chain("AAPL", date(2026, 1, 1), date(2026, 3, 1), OptionRight.CALL)
    assert len(chain) == 1
    c = chain[0]
    assert c.symbol == "AAPL260116C00150000"
    assert c.delta == 0.55 and c.implied_volatility == 0.32
    assert c.bid == 5.0 and c.ask == 5.4
    assert c.open_interest == 1200
```
> Note: the exact attribute names on Alpaca's `Quote`/`Trade`/`OptionsSnapshot` are `bid_price`/`ask_price`/`price`/`implied_volatility`/`greeks.delta`. Confirm against the installed models (`alpaca/data/models/snapshots.py`, `quotes.py`, `trades.py`) and adjust the mapper + test together.

**Step 2: Run — expect failure** (`AttributeError`/missing methods):
```bash
venv/bin/python -m pytest tests/test_alpaca_options.py -q
```

**Step 3: Implement** in `AlpacaAccount.py`:
- Class declaration (line 65):
  ```python
  from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
  class AlpacaAccount(AccountInterface, OptionsAccountInterface):
  ```
- Add a lazily-created cached option data client:
  ```python
  def _get_option_data_client(self):
      client = getattr(self, "_option_data_client", None)
      if client is None:
          from alpaca.data.historical.option import OptionHistoricalDataClient
          client = OptionHistoricalDataClient(api_key=self.settings["api_key"],
                                              secret_key=self.settings["api_secret"])
          self._option_data_client = client
      return client
  ```
- `_get_option_contracts_meta(underlying, expiry_min, expiry_max, option_type, strike_min, strike_max)`: call `self.client.get_option_contracts(GetOptionContractsRequest(...))` (paginate via `next_page_token`), return `{occ_symbol: OptionContract_meta}`. Map `OptionRight` → `ContractType`; strike filters are **str** on the trading-side request.
- `get_option_chain(...)`: build `OptionChainRequest(underlying_symbol=..., type=..., strike_price_gte=..., strike_price_lte=..., expiration_date_gte=..., expiration_date_lte=..., feed=<OptionsFeed>)`, call `self._get_option_data_client().get_option_chain(req)` → dict of `{occ: OptionsSnapshot}`; join with `_get_option_contracts_meta` for `strike`/`expiry`/`type`/`open_interest`; map each into `OptionContract`. Guard all Greeks/IV/quote fields for `None`. Apply the expiry/strike/type filters defensively.
- `get_option_quote(contract_symbol)`: `get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=contract_symbol, feed=...))` → `OptionQuote`.
- `get_atm_implied_volatility(underlying)`: fetch spot via `self.get_instrument_current_price(underlying, 'mid')`, pull a near-dated chain (e.g. 20–45 DTE), pick the contract whose strike is nearest spot, return its `implied_volatility`. Return `None` if unavailable.
- Feed selection: use `OptionsFeed.INDICATIVE` unless an OPRA subscription setting is enabled; make it a small helper so it's overridable.

**Step 4: Run — expect PASS:**
```bash
venv/bin/python -m pytest tests/test_alpaca_options.py -q
```

**Step 5: Commit:**
```bash
git add ba2_trade_platform/modules/accounts/AlpacaAccount.py tests/test_alpaca_options.py
git commit -m "feat(options): AlpacaAccount option chain/quote/ATM-IV via OptionHistoricalDataClient"
```

---

## Task 8: AlpacaAccount — `get_option_positions`

Map broker option positions (Alpaca `asset_class == "us_option"`) into `OptionPosition`, parsing the OCC symbol for strike/expiry/right.

**Files:**
- Modify: `AlpacaAccount.py`
- Test: `tests/test_alpaca_options.py` (append)

**Step 1: Write the failing test** — feed a fake `self.client.get_all_positions()` returning one equity + one `us_option` position; assert only the option is mapped, with parsed strike/expiry/right and `side`/`quantity`/`avg_entry_price`.

**Step 2: Run — expect failure.**

**Step 3: Implement** a pure `_parse_occ_symbol(occ) -> (underlying, expiry: date, right: OptionRight, strike: float)` helper (OCC format: root + `YYMMDD` + `C`/`P` + strike×1000 as 8 digits) and `get_option_positions()` that filters `getattr(pos, "asset_class", "")` containing `option`, maps qty sign → `OrderDirection`, and builds `OptionPosition`. Unit-test `_parse_occ_symbol` directly too.

**Step 4: Run — expect PASS.**

**Step 5: Commit:**
```bash
git commit -am "feat(options): AlpacaAccount get_option_positions + OCC symbol parsing"
```

---

## Task 9: AlpacaAccount — `_submit_option_order_impl` (single + multi-leg)

**Files:**
- Modify: `AlpacaAccount.py`
- Test: `tests/test_alpaca_options.py` (append)

**Step 1: Write the failing test.** Monkeypatch `acct.client.submit_order` to capture the request object. Two cases:
- **single leg, limit BUY**: assert a `LimitOrderRequest` with `symbol == OCC`, `side == OrderSide.BUY`, `qty == 2`, `limit_price == 5.2`, `time_in_force == TimeInForce.DAY`, no `order_class`/`legs`.
- **bull call spread, limit**: assert an `OrderClass.MLEG` request with `qty == 1`, `legs` of length 2 (each an `OptionLegRequest` with `symbol`, `ratio_qty`, `position_intent`), top-level `symbol` absent/None, and `limit_price == 4.0` (positive = debit). Assert the parent + child TradingOrders get `broker_order_id`/`FILLED` written back.

**Step 2: Run — expect failure.**

**Step 3: Implement** `_submit_option_order_impl(self, trading_order, legs, leg_orders=None)`:
- Map `legs` → alpaca: single leg → `MarketOrderRequest`/`LimitOrderRequest(symbol=occ, qty=trading_order.quantity, side=OrderSide(...), time_in_force=TimeInForce.DAY, limit_price=..., client_order_id=str(trading_order.id))`.
- Multi-leg → build `OptionLegRequest(symbol=leg.contract_symbol, ratio_qty=leg.ratio_qty, position_intent=PositionIntent(leg.position_intent) or side=OrderSide(...))`; `LimitOrderRequest(qty=trading_order.quantity, order_class=OrderClass.MLEG, legs=[...], time_in_force=TimeInForce.DAY, limit_price=trading_order.limit_price, client_order_id=str(trading_order.id))` (omit top-level `symbol`). TIF for options must be `DAY`.
- `alpaca_order = self.client.submit_order(req)`; write `broker_order_id`, status (reuse the existing status-mapping helper), and `legs_broker_ids` (from `alpaca_order.legs`) onto the parent; map each returned leg to its child `TradingOrder` (`update_instance`). Wrap in `@alpaca_api_retry` + try/except with `logger.error(..., exc_info=True)`, mirroring `_submit_order_impl`.

**Step 4: Run — expect PASS.**

**Step 5: Commit:**
```bash
git commit -am "feat(options): AlpacaAccount single + multi-leg option order submission"
```

---

## Task 10: AlpacaAccount — `close_option_position` + `get_iv_rank`/`get_atm` wiring

**Files:**
- Modify: `AlpacaAccount.py`
- Test: `tests/test_alpaca_options.py` (append)

**Step 1: Write the failing test.** `close_option_position(OptionPosition long call, qty 2)` → builds a single-leg closing order with `side == SELL`, `position_intent == "sell_to_close"`, routed through `_submit_option_order_impl` (capture request). Short position → `BUY` / `buy_to_close`.

**Step 2: Run — expect failure.**

**Step 3: Implement** `close_option_position(...)` building an `OptionLeg` with the opposite side + `*_to_close` intent and calling `self.submit_option_order([leg], int(position.quantity), order_type, limit_price, option_strategy="close")`. Implement `get_iv_rank` to delegate to Task 11's history reader (placeholder returning `None` until Task 11 lands, then wire through).

**Step 4: Run — expect PASS.**

**Step 5: Commit:**
```bash
git commit -am "feat(options): AlpacaAccount close_option_position"
```

---

## Task 11: IV-history primitive (`OptionIVSnapshot` + `record_atm_iv` + `get_iv_rank`)

Resolves the IV-rank open question: store our own trailing ATM-IV series; compute percentile from it.

**Files:**
- Modify: `ba2_trade_platform/core/models.py` (new `OptionIVSnapshot`)
- Modify: `ba2_trade_platform/core/interfaces/OptionsAccountInterface.py` (add concrete `record_atm_iv` + a shared `_iv_rank_from_series` static helper)
- Modify: `AlpacaAccount.py` (`get_iv_rank` reads the table)
- Migration: new revision (down_revision = previous option migration)
- Test: `tests/test_option_iv_history.py`

**Step 1: Write the failing test** — pure percentile math + record/read:
```python
from datetime import datetime, timezone
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


def test_iv_rank_percentile_math():
    series = [0.10, 0.20, 0.30, 0.40, 0.50]
    # current 0.30 -> 2 of 5 below -> 40th percentile
    assert OptionsAccountInterface._iv_rank_from_series(series, current=0.30) == 40.0
    assert OptionsAccountInterface._iv_rank_from_series([0.2], current=0.2, min_samples=20) is None


def test_record_and_rank_roundtrip(mock_account):
    for iv in (0.10, 0.20, 0.30, 0.40, 0.50):
        mock_account.record_atm_iv("AAPL", iv)
    rank = mock_account.get_iv_rank("AAPL", min_samples=3)  # mock currently stubs 50.0
    assert rank is not None
```
> The mock stubs `get_iv_rank`; for the roundtrip, either un-stub the mock to use the real reader, or test the reader on `AlpacaAccount` with a monkeypatched current IV. Keep the **percentile math** test broker-independent (it tests the static helper).

**Step 2: Run — expect failure.**

**Step 3: Implement.**
- `OptionIVSnapshot` model in `models.py`:
  ```python
  class OptionIVSnapshot(SQLModel, table=True):
      __tablename__ = "option_iv_snapshot"
      id: int | None = Field(default=None, primary_key=True)
      account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
      underlying: str = Field(index=True)
      atm_iv: float
      recorded_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)
  ```
- Register it in `tests/conftest.py`'s model import list so `create_all` builds it.
- Static helper + concrete recorder on `OptionsAccountInterface`:
  ```python
  @staticmethod
  def _iv_rank_from_series(series, current, min_samples=20):
      vals = [v for v in series if v is not None]
      if current is None or len(vals) < min_samples:
          return None
      below = sum(1 for v in vals if v < current)
      return round(below / len(vals) * 100, 2)

  def record_atm_iv(self, underlying, iv=None):
      from ..db import add_instance
      from ..models import OptionIVSnapshot
      if iv is None:
          iv = self.get_atm_implied_volatility(underlying)
      if iv is None:
          return None
      return add_instance(OptionIVSnapshot(account_id=self.id, underlying=underlying, atm_iv=iv))
  ```
- `AlpacaAccount.get_iv_rank(underlying, lookback_days=252, min_samples=20)`: query `OptionIVSnapshot` for this account+underlying within `lookback_days`, current = `self.get_atm_implied_volatility(underlying)`, return `self._iv_rank_from_series([s.atm_iv ...], current, min_samples)`. Make the **Mock** use the same reader (drop the stub) so the roundtrip test exercises real logic.
- Migration: `op.create_table("option_iv_snapshot", ...)`; `down_revision` = the Task 2 revision.

**Step 4: Run — expect PASS:**
```bash
venv/bin/python -m pytest tests/test_option_iv_history.py -q
```

**Step 5: Commit:**
```bash
git add ba2_trade_platform/core/models.py ba2_trade_platform/core/interfaces/OptionsAccountInterface.py ba2_trade_platform/modules/accounts/AlpacaAccount.py alembic/versions/*iv_snapshot* tests/test_option_iv_history.py tests/conftest.py
git commit -m "feat(options): IV history primitive (OptionIVSnapshot, record_atm_iv, get_iv_rank)"
```

---

## Task 12: Full suite, paper validation, version bump

**Step 1: Run the full suite in two groups** (must be green):
```bash
venv/bin/python -m pytest --ignore=tests/test_penny_entry.py --ignore=tests/test_penny_momentum_trader.py -q
venv/bin/python -m pytest tests/test_penny_entry.py tests/test_penny_momentum_trader.py -q
```
Fix any regressions before proceeding. Do not claim done until both groups pass (the second may hit the known native crash — note it explicitly if so, don't paper over it).

**Step 2: Paper validation script** — `test_files/validate_options_paper.py` (manual, not collected by pytest). Using a real **paper** Alpaca account from the dev DB: print `get_account().options_trading_level`; fetch a chain for a liquid large-cap/ETF (e.g. `SPY`/`AAPL`), pick a near-ATM ~30–45 DTE liquid call (OI ≥ min, spread ≤ max), `submit_option_order` 1 contract at the **ask** (never mid), poll `get_option_positions()`, then `close_option_position` at the **bid**. Log every step. Run it manually:
```bash
venv/bin/python test_files/validate_options_paper.py
```
Capture the output in the PR description. Gate strategy availability on the runtime `options_trading_level` (don't hard-code Level 3).

**Step 3: Bump version** in `ba2_trade_platform/version.py` (increment build `NNNNN`).

**Step 4: Commit + push the branch:**
```bash
git add ba2_trade_platform/version.py test_files/validate_options_paper.py
git commit -m "chore(options): paper validation script + version bump for Phase 1"
git push -u origin feature/options-trading-phase1
```

---

## Definition of done (Phase 1)

- `OptionsAccountInterface` exists; `AlpacaAccount` and `MockAccount` both pass `isinstance(acct, OptionsAccountInterface)`.
- AlpacaAccount can fetch a chain (with Greeks+IV), a quote, ATM IV, and option positions, and can submit single-leg and 2–4-leg orders + close a position — all unit-tested with mocked Alpaca clients.
- `TradingOrder` carries option metadata; multi-leg spreads persist as parent + children; equity math is multiplier-aware. Migration applies cleanly on a copy of the dev DB.
- IV-rank primitive stores/reads an ATM-IV series and computes a percentile.
- Both test groups green; paper validation script run on paper and output captured.
- Version bumped; branch pushed; **not** merged to `dev` without review.

## Carried into later phases (explicitly NOT in Phase 1)

- Rule **conditions** (`dip`, `iv_rank`, `has_option_position`, DTE, moneyness) and **actions** (`buy_call`, `open_bull_call_spread`, `sell_covered_call`, `close_option`) + `OptionContractSelector` → **base Phase 2–4** (the registration map per condition/action is already documented: `types.py` enum → `TradeConditions.py`/`TradeActions.py` class + map → `TradeActionEvaluator.py` (`_create_trade_action` + `_get_action_type_from_action` + `execute()` categorization + `_sort_actions_by_priority`) → `rules_documentation.py`).
- Scheduled daily `record_atm_iv` job (JobManager) → Phase 2 (consumes the Task 11 primitive).
- **Assignment/expiry reconciliation** via raw `GET /v2/account/activities` (`OPASN`/`OPEXC`/`OPEXP`/`OPCSH`), cash/BP reserve for short premium → **advanced Phase C**.
- Bearish/put/straddle structures → **advanced Phases A–D**.
