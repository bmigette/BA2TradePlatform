# Margin Trading (Leverage) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a live account deploy more than its balance, capped by a per-account `margin_factor`, with the broker's real buying power as the truth, and show balance vs buying power in the UI.

**Architecture:** Two builtin account settings (`margin_enabled`, `margin_factor`) on `ReadOnlyAccountInterface`; four new account accessors (stock/option margin multiplier, broker buying power, stock/option *tradable balance*) built on the existing `AccountSnapshot`; every expert-side sizing path switches its base from `get_balance()` to `get_tradable_balance()`. The portfolio allocator and the backtest account are untouched. Design: `docs/plans/2026-09-08-margin-trading-design.md`.

**Tech Stack:** Python 3.12, SQLModel, NiceGUI, pytest. Shared code lives in `packages/common/ba2_common` (source of truth); the in-tree `ba2_trade_platform/core/...` modules are re-export shims and are NOT edited.

**Worktree:** `C:\Users\basti\Documents\dev\BA2-margin`, branch `feat/margin-trading`. Python is the main checkout's venv: `../BA2TradePlatform/.venv/Scripts/python.exe`. `pytest.ini` prepends this worktree's `packages/*` so `pytest` runs the worktree's package code. Always run tests from the worktree root.

**Test command prefix (use everywhere below):**
```
cd C:\Users\basti\Documents\dev\BA2-margin && ../BA2TradePlatform/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -o addopts="" 
```

**Baseline (pre-existing failures, NOT ours, leave alone):** `tests/test_no_zero_coercion.py::test_no_new_unknown_reads_as_zero`, `tests/test_option_actions.py::test_sell_covered_call_short_of_cover_returns_a_refusal_and_never_raises`, `tests/test_tastytrade_account.py::test_time_in_force_survives_a_broker_round_trip[Ext Overnight]` and `[GTC Ext Overnight]`.

**Rules that apply to every task:**
- No `.get(key, default)` on config; no fabricated numbers for money (raise or `None`-means-unknown as the existing code around you does).
- Logging via `from ba2_common.logger import logger` in packages, `from ba2_trade_platform.logger import logger` in-tree. `exc_info=True` only inside `except`.
- Commit after every task with a message in the repo's style (`feat(margin): ...`, `test(margin): ...`). End each commit body with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01AutNfBsyEKDTZAw7qcNTZJ
  ```

---

### Task 1: `AccountSnapshot.option_buying_power`

**Files:**
- Modify: `packages/common/ba2_common/core/account_types.py:82-86` (the `AccountSnapshot` dataclass fields)
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:196-207` (base tolerant probe)
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py:2028-2042` (the `AccountSnapshot(...)` construction)
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:462-493` (the `AccountSnapshot(...)` construction)
- Test: `packages/common/tests/test_margin_snapshot.py` (create)
- Test: `tests/test_alpaca_account_snapshot.py` (append one test)

**Step 1: Write the failing tests**

`packages/common/tests/test_margin_snapshot.py`:
```python
"""AccountSnapshot.option_buying_power: the broker's option (derivative) buying power.

None means the broker did not publish one -- never 0.0. The base tolerant probe
reads it from `options_buying_power` (Alpaca) or `derivative_buying_power`
(TastyTrade); nothing else is guessed at.
"""
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Probe(ReadOnlyAccountInterface):
    """Only what the base get_account_snapshot() needs."""

    def __init__(self, info):
        self.id = 1
        self._info = info

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_balance(self):
        return None

    def get_account_info(self):
        return self._info

    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


def test_default_is_none():
    assert AccountSnapshot().option_buying_power is None


def test_probe_reads_alpaca_name():
    snap = _Probe({"options_buying_power": "1234.5"}).get_account_snapshot()
    assert snap.option_buying_power == 1234.5


def test_probe_reads_tastytrade_name():
    snap = _Probe({"derivative_buying_power": 99.0}).get_account_snapshot()
    assert snap.option_buying_power == 99.0


def test_probe_leaves_none_when_broker_publishes_neither():
    snap = _Probe({"buying_power": 10.0}).get_account_snapshot()
    assert snap.option_buying_power is None
```

Append to `tests/test_alpaca_account_snapshot.py` (look at how that file builds its fake `TradeAccount` / patches `get_account_info`, and reuse that helper; the assertion is the only new thing):
```python
def test_alpaca_snapshot_carries_options_buying_power(<same fixtures the neighbouring tests use>):
    # build the account exactly as the test above it does, with the fake TradeAccount
    # carrying options_buying_power="4321.0"
    snap = account.get_account_snapshot()
    assert snap.option_buying_power == 4321.0
```

**Step 2: Run tests to verify they fail**

Run: `<prefix> packages/common/tests/test_margin_snapshot.py tests/test_alpaca_account_snapshot.py -k "option" `
Expected: FAIL with `AttributeError: 'AccountSnapshot' object has no attribute 'option_buying_power'` (dataclass rejects unknown attribute / assertion on None).

**Step 3: Implement**

`account_types.py`, after `non_marginable_buying_power`:
```python
    buying_power: Optional[float] = None
    non_marginable_buying_power: Optional[float] = None
    #: The broker's OPTION (derivative) buying power. Alpaca `options_buying_power`,
    #: TastyTrade `derivative_buying_power`. None = not published, never zero.
    option_buying_power: Optional[float] = None
```
Also add one line to the class docstring: "``option_buying_power`` is the broker's derivative buying power; ``None`` when the broker publishes none."

`ReadOnlyAccountInterface.py` base probe, in the `return AccountSnapshot(` block after `non_marginable_buying_power=...`:
```python
            option_buying_power=_first("options_buying_power", "derivative_buying_power"),
```

`AlpacaAccount.py` snapshot construction, after `non_marginable_buying_power=_f('non_marginable_buying_power'),`:
```python
            option_buying_power=_f('options_buying_power'),
```

`TastyTradeAccount.py` snapshot construction, after `non_marginable_buying_power=_num("cash_available_to_withdraw"),`:
```python
            option_buying_power=_num("derivative_buying_power"),
```

**Step 4: Run tests**

Run: `<prefix> packages/common/tests/test_margin_snapshot.py tests/test_alpaca_account_snapshot.py tests/test_tastytrade_account.py`
Expected: new tests PASS; only the 2 baseline Tasty `time_in_force` failures remain.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/account_types.py packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py ba2_trade_platform/modules/accounts/AlpacaAccount.py ba2_trade_platform/modules/accounts/TastyTradeAccount.py packages/common/tests/test_margin_snapshot.py tests/test_alpaca_account_snapshot.py
git commit -m "feat(margin): AccountSnapshot carries the broker's option buying power"
```

---

### Task 2: Builtin settings `margin_enabled` / `margin_factor` + pure validator

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:38-56` (`_ensure_builtin_settings`) and module level
- Test: `packages/common/tests/test_margin_settings.py` (create)

**Step 1: Write the failing tests**

```python
"""margin_enabled / margin_factor: the per-account leverage switch and ceiling.

Declared ONCE on ReadOnlyAccountInterface so every broker inherits them and the
generic account settings dialog renders/saves them with no UI code (same pattern
as manual_trading_enabled). Read through get_setting_with_interface_default,
never settings.get(key, default) -- see test_manual_trading_setting.py.
"""
import pytest

from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
    ReadOnlyAccountInterface, MARGIN_FACTOR_MIN, margin_factor_error,
)


def _defs():
    return ReadOnlyAccountInterface.get_merged_settings_definitions()


def test_margin_enabled_is_declared_as_bool_defaulting_false():
    d = _defs()["margin_enabled"]
    assert d["type"] == "bool" and d["default"] is False and d["required"] is False


def test_margin_factor_is_declared_as_float_defaulting_1_8():
    d = _defs()["margin_factor"]
    assert d["type"] == "float" and d["default"] == 1.8 and d["required"] is False


def test_both_carry_a_tooltip():
    for key in ("margin_enabled", "margin_factor"):
        assert _defs()[key]["tooltip"]


@pytest.mark.parametrize("bad", [0.0, 0.99, -1.0, None, "abc"])
def test_margin_factor_error_names_the_problem(bad):
    msg = margin_factor_error(bad)
    assert msg and "margin_factor" in msg


@pytest.mark.parametrize("ok", [1.0, 1.8, 4.0, "2"])
def test_margin_factor_error_is_none_for_a_valid_factor(ok):
    assert margin_factor_error(ok) is None


def test_min_is_one():
    assert MARGIN_FACTOR_MIN == 1.0
```

**Step 2: Run to verify failure**

Run: `<prefix> packages/common/tests/test_margin_settings.py`
Expected: FAIL, `ImportError: cannot import name 'MARGIN_FACTOR_MIN'`.

**Step 3: Implement**

At module level in `ReadOnlyAccountInterface.py` (after the imports, before the class):
```python
#: A margin_factor below 1.0 would let an account deploy LESS than its balance,
#: which is virtual_equity_pct's job, not this setting's. 1.0 == margin off.
MARGIN_FACTOR_MIN = 1.0


def margin_factor_error(value: Any) -> Optional[str]:
    """Why ``value`` is not an acceptable ``margin_factor``; ``None`` when it is. Pure.

    Used by the account settings dialog at save time and by the account itself at
    read time, so the two can never disagree about what is valid.
    """
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return f"margin_factor must be a number, got {value!r}"
    if factor < MARGIN_FACTOR_MIN:
        return f"margin_factor must be >= {MARGIN_FACTOR_MIN} (1.0 means no leverage), got {factor}"
    return None
```

In `_ensure_builtin_settings`, add after `manual_trading_enabled`:
```python
                "margin_enabled": {
                    "type": "bool",
                    "required": False,
                    "default": False,
                    "description": "Margin trading enabled",
                    "tooltip": "Let this account's experts deploy more than the account balance, up to balance x margin factor, never more than the broker's own buying power. Off = experts size against the plain balance.",
                },
                "margin_factor": {
                    "type": "float",
                    "required": False,
                    "default": 1.8,
                    "description": "Margin factor (max exposure / balance)",
                    "tooltip": "Ceiling on total exposure as a multiple of balance: 1.8 means a $10k account never holds more than $18k of positions. Capped by the broker's multiplier. Ignored when margin trading is off. Minimum 1.0.",
                },
```

**Step 4: Run tests**

Run: `<prefix> packages/common/tests/test_margin_settings.py packages/common/tests/test_manual_trading_setting.py tests/test_settings.py`
Expected: PASS.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py packages/common/tests/test_margin_settings.py
git commit -m "feat(margin): margin_enabled / margin_factor builtin account settings"
```

---

### Task 3: Multipliers and buying power accessors

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` (add methods after `true_equity`, ~line 240)
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py` (override, near `get_account_info` ~line 1855)
- Test: `packages/common/tests/test_margin_accessors.py` (create)

**Step 1: Write the failing tests**

```python
"""get_stock_margin_multiplier / get_option_margin_multiplier / get_buying_power /
get_option_buying_power -- broker facts, read off the AccountSnapshot.

A missing stock multiplier or buying power RAISES: these feed position sizing and
a fabricated number there is a fabricated order. The option figures default
conservatively (multiplier 1.0 = cash-settled; option BP None = unknown, the
caller skips its check and says so).
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Stub(ReadOnlyAccountInterface):
    def __init__(self, snapshot):
        self.id = 7
        self._snap = snapshot

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        return self._snap

    def get_balance(self):
        return self._snap.equity

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


def test_stock_multiplier_is_the_snapshot_multiplier():
    assert _Stub(AccountSnapshot(margin_multiplier=2.0)).get_stock_margin_multiplier() == 2.0


def test_stock_multiplier_raises_when_broker_publishes_none():
    with pytest.raises(ValueError, match="account 7"):
        _Stub(AccountSnapshot()).get_stock_margin_multiplier()


def test_option_multiplier_defaults_to_one():
    assert _Stub(AccountSnapshot(margin_multiplier=4.0)).get_option_margin_multiplier() == 1.0


def test_buying_power_is_the_snapshot_buying_power():
    assert _Stub(AccountSnapshot(buying_power=1500.0)).get_buying_power() == 1500.0


def test_buying_power_raises_when_broker_publishes_none():
    with pytest.raises(ValueError, match="buying power"):
        _Stub(AccountSnapshot(equity=10.0)).get_buying_power()


def test_option_buying_power_may_be_none():
    assert _Stub(AccountSnapshot()).get_option_buying_power() is None
    assert _Stub(AccountSnapshot(option_buying_power=3.0)).get_option_buying_power() == 3.0
```

**Step 2: Run to verify failure**

Run: `<prefix> packages/common/tests/test_margin_accessors.py`
Expected: FAIL, `AttributeError: '_Stub' object has no attribute 'get_stock_margin_multiplier'`.

**Step 3: Implement** (in `ReadOnlyAccountInterface`, right after `true_equity`)

```python
    # ------------------------------------------------------------------
    # MARGIN / LEVERAGE.  Design: docs/plans/2026-09-08-margin-trading-design.md
    # ------------------------------------------------------------------
    def get_stock_margin_multiplier(self) -> float:
        """The broker's stock leverage: dollars of buying power per dollar of equity.

        Alpaca publishes it as ``TradeAccount.multiplier``; TastyTrade derives 2.0/1.0
        from ``margin_or_cash``; the backtest account overrides to 1.0. RAISES when the
        broker published none (IBKR): this number scales position sizes, and a guessed
        multiplier is a guessed order.
        """
        multiplier = self.get_account_snapshot().margin_multiplier
        if multiplier is None:
            raise ValueError(
                f"account {self.id} ({type(self).__name__}) published no stock margin "
                f"multiplier; cannot size with margin")
        return float(multiplier)

    def get_option_margin_multiplier(self) -> float:
        """The broker's OPTION leverage. Base default 1.0: long options are cash-settled
        at every supported broker. An adapter overrides only with a real broker figure."""
        return 1.0

    def get_buying_power(self) -> float:
        """The broker's REMAINING stock buying power. RAISES when unpublished."""
        bp = self.get_account_snapshot().buying_power
        if bp is None:
            raise ValueError(
                f"account {self.id} ({type(self).__name__}) published no buying power")
        return float(bp)

    def get_option_buying_power(self) -> Optional[float]:
        """The broker's remaining OPTION buying power, or ``None`` when unpublished.

        ``None`` is allowed here (unlike ``get_buying_power``) because its only
        consumer is the over-exposure WARNING, which skips itself and says so.
        """
        return self.get_account_snapshot().option_buying_power
```

`backtest_account.py`, next to `get_account_info`:
```python
    def get_stock_margin_multiplier(self) -> float:
        """The simulator runs unlevered. Backtests that need leverage are run with a
        larger starting balance (operator decision, 2026-09-08 design)."""
        return 1.0
```

**Step 4: Run tests**

Run: `<prefix> packages/common/tests/test_margin_accessors.py tests/test_accounts/test_account_interface.py`
Expected: PASS.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py testplatform/backend/app/services/backtest/backtest_account.py packages/common/tests/test_margin_accessors.py
git commit -m "feat(margin): stock/option multiplier and buying-power accessors on the account"
```

---

### Task 4: Pure margin math + `get_tradable_balance` / `get_option_tradable_balance`

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` (module-level pure functions + two methods after Task 3's block)
- Test: `packages/common/tests/test_margin_tradable_balance.py` (create)

**Step 1: Write the failing tests**

```python
"""Tradable balance = balance x min(margin_factor, broker multiplier) with margin on;
balance with it off. Plus the over-exposure warning threshold.

Worked example the operator gave: balance 10k, broker multiplier 2 (20k gross
capacity), factor 1.8 (platform deploys at most 18k). Once the broker's REMAINING
buying power drops under 20k - 18k = 2k, gross exposure has passed the platform's
own ceiling -- something outside the experts (allocator, manual trade) consumed
it -- and that is the WARNING.
"""
import logging

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
    ReadOnlyAccountInterface, tradable_balance_for, over_exposure_threshold,
)


# ----- pure math -----------------------------------------------------------

def test_margin_off_is_the_balance():
    assert tradable_balance_for(10_000.0, margin_enabled=False, factor=1.8, multiplier=2.0) == 10_000.0


def test_margin_on_is_balance_times_factor():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=2.0) == 18_000.0


def test_broker_multiplier_below_factor_wins():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=1.5) == 15_000.0


def test_non_marginable_account_is_the_balance():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=1.0) == 10_000.0


def test_threshold_is_balance_times_multiplier_minus_factor():
    assert over_exposure_threshold(10_000.0, multiplier=2.0, factor=1.8) == pytest.approx(2_000.0)


def test_threshold_is_negative_when_factor_exceeds_multiplier():
    # then no remaining-BP figure can ever be below it: the warning cannot fire
    assert over_exposure_threshold(10_000.0, multiplier=1.5, factor=1.8) < 0


# ----- the account methods --------------------------------------------------

class _Stub(ReadOnlyAccountInterface):
    def __init__(self, *, balance, snapshot, settings):
        self.id = 3
        self._balance = balance
        self._snap = snapshot
        self._stored = settings

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


ON = {"margin_enabled": True, "margin_factor": 1.8}
OFF = {"margin_enabled": False, "margin_factor": 1.8}
UNSET = {"margin_enabled": None, "margin_factor": None}   # never saved: defaults apply


def test_off_returns_balance_and_never_touches_the_snapshot():
    class _NoSnap(_Stub):
        def get_account_snapshot(self):
            raise AssertionError("must not be read with margin off")
    acct = _NoSnap(balance=10_000.0, snapshot=None, settings=OFF)
    assert acct.get_tradable_balance() == 10_000.0
    assert acct.get_option_tradable_balance() == 10_000.0


def test_unset_settings_read_as_off():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0), settings=UNSET)
    assert acct.get_tradable_balance() == 10_000.0


def test_on_returns_balance_times_factor():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0), settings=ON)
    assert acct.get_tradable_balance() == 18_000.0


def test_on_option_side_uses_the_option_multiplier_default_one():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0), settings=ON)
    assert acct.get_option_tradable_balance() == 10_000.0


def test_string_true_from_the_settings_table_reads_as_on():
    # the deploy-parity trap: bool settings can come back as "1"/"true"
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0),
                 settings={"margin_enabled": "true", "margin_factor": "1.8"})
    assert acct.get_tradable_balance() == 18_000.0


def test_on_with_cash_account_returns_balance_and_warns(caplog):
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=1.0, buying_power=4_000.0), settings=ON)
    with caplog.at_level(logging.WARNING):
        assert acct.get_tradable_balance() == 10_000.0
    assert any("non-marginable" in r.message and "account 3" in r.message for r in caplog.records)


def test_on_raises_when_balance_unknown():
    acct = _Stub(balance=None, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=1.0), settings=ON)
    with pytest.raises(ValueError, match="balance"):
        acct.get_tradable_balance()


def test_on_raises_when_multiplier_unknown():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(buying_power=1.0), settings=ON)
    with pytest.raises(ValueError, match="multiplier"):
        acct.get_tradable_balance()


def test_on_raises_when_buying_power_unknown():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0), settings=ON)
    with pytest.raises(ValueError, match="buying power"):
        acct.get_tradable_balance()


def test_on_raises_on_a_bad_factor():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=1.0),
                 settings={"margin_enabled": True, "margin_factor": 0.5})
    with pytest.raises(ValueError, match="margin_factor"):
        acct.get_tradable_balance()


def test_over_exposure_warns_below_threshold_and_not_at_it(caplog):
    snap_at = AccountSnapshot(margin_multiplier=2.0, buying_power=2_000.0)     # exactly 20k-18k
    snap_below = AccountSnapshot(margin_multiplier=2.0, buying_power=1_999.0)
    with caplog.at_level(logging.WARNING):
        _Stub(balance=10_000.0, snapshot=snap_at, settings=ON).get_tradable_balance()
    assert not any("past the margin ceiling" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _Stub(balance=10_000.0, snapshot=snap_below, settings=ON).get_tradable_balance()
    hits = [r for r in caplog.records if "past the margin ceiling" in r.message]
    assert len(hits) == 1 and "1,999.00" in hits[0].message and "2,000.00" in hits[0].message


def test_option_over_exposure_is_skipped_with_a_debug_line_when_option_bp_unknown(caplog):
    class _Levered(_Stub):
        def get_option_margin_multiplier(self):
            return 2.0
    acct = _Levered(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0), settings=ON)
    with caplog.at_level(logging.DEBUG):
        assert acct.get_option_tradable_balance() == 18_000.0
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    assert any("option buying power" in r.message and r.levelno == logging.DEBUG for r in caplog.records)
```

**Step 2: Run to verify failure**

Run: `<prefix> packages/common/tests/test_margin_tradable_balance.py`
Expected: FAIL, `ImportError: cannot import name 'tradable_balance_for'`.

**Step 3: Implement**

Module level (next to `margin_factor_error`):
```python
def tradable_balance_for(balance: float, *, margin_enabled: bool, factor: float,
                         multiplier: float) -> float:
    """balance x min(factor, multiplier) with margin on; balance with it off. Pure.

    ``multiplier <= 1.0`` (a cash account) therefore yields the plain balance even
    with margin on -- the broker will not lend, so the factor has nothing to scale.
    """
    if not margin_enabled:
        return float(balance)
    return float(balance) * min(float(factor), max(float(multiplier), 1.0))


def over_exposure_threshold(balance: float, *, multiplier: float, factor: float) -> float:
    """The remaining-buying-power level below which gross exposure has passed the
    platform's own ceiling. Pure.

    Gross capacity is balance x multiplier; the platform intends to use
    balance x factor; what should still be left is the difference. A factor above
    the multiplier gives a negative threshold, which no remaining BP can be under.
    """
    return float(balance) * (float(multiplier) - float(factor))
```

Methods, after `get_option_buying_power` (import `coerce_bool` from `ExtendableSettingsInterface` at the top of the file):
```python
    def _margin_enabled(self) -> bool:
        return coerce_bool(self.get_setting_with_interface_default("margin_enabled", log_warning=False))

    def _margin_factor(self) -> float:
        raw = self.get_setting_with_interface_default("margin_factor", log_warning=False)
        err = margin_factor_error(raw)
        if err:
            raise ValueError(f"account {self.id}: {err}")
        return float(raw)

    def _tradable_balance(self, *, asset: str, multiplier: float,
                          remaining_bp: Optional[float], bp_known: bool) -> float:
        balance = self.get_balance()
        if balance is None:
            raise ValueError(f"account {self.id} ({type(self).__name__}): balance unavailable")
        if not self._margin_enabled():
            return float(balance)
        factor = self._margin_factor()
        if multiplier <= 1.0:
            logger.warning(
                f"[Account {self.id}] margin_enabled but the broker reports a non-marginable "
                f"{asset} account (multiplier {multiplier:g}); tradable {asset} balance stays at "
                f"the balance ${balance:,.2f}")
            return float(balance)
        if not bp_known:
            logger.debug(f"[Account {self.id}] no {asset} buying power published; "
                         f"over-exposure check skipped")
        else:
            threshold = over_exposure_threshold(balance, multiplier=multiplier, factor=factor)
            if remaining_bp < threshold:
                logger.warning(
                    f"[Account {self.id}] {asset} exposure is past the margin ceiling: remaining "
                    f"broker buying power ${remaining_bp:,.2f} < ${threshold:,.2f} "
                    f"(balance ${balance:,.2f} x (multiplier {multiplier:g} - factor {factor:g})). "
                    f"Something outside the experts (allocator, manual trades) consumed it.")
        return tradable_balance_for(balance, margin_enabled=True, factor=factor,
                                    multiplier=multiplier)

    def get_tradable_balance(self) -> float:
        """What this account's experts may deploy in STOCK, in dollars.

        margin off: ``get_balance()``. margin on: balance x min(margin_factor, broker
        stock multiplier). Remaining broker buying power is NOT subtracted here -- the
        expert's own ``get_available_balance`` subtracts its positions and clamps to
        broker BP -- it is only read for the over-exposure WARNING.

        RAISES (never returns a guess) when balance, multiplier or buying power is
        unknown with margin on. With margin off only the balance is read.
        """
        if not self._margin_enabled():
            return self._tradable_balance(asset="stock", multiplier=1.0, remaining_bp=None, bp_known=False)
        return self._tradable_balance(
            asset="stock", multiplier=self.get_stock_margin_multiplier(),
            remaining_bp=self.get_buying_power(), bp_known=True)

    def get_option_tradable_balance(self) -> float:
        """Same as ``get_tradable_balance`` for OPTIONS, with the option multiplier.
        Option buying power may be unpublished: then the warning is skipped (DEBUG)."""
        if not self._margin_enabled():
            return self._tradable_balance(asset="option", multiplier=1.0, remaining_bp=None, bp_known=False)
        option_bp = self.get_option_buying_power()
        return self._tradable_balance(
            asset="option", multiplier=self.get_option_margin_multiplier(),
            remaining_bp=option_bp, bp_known=option_bp is not None)
```
Note the "off" path calls `_tradable_balance` with dummy multiplier only to share the balance-None check; it returns before reading anything else. Keep it that way so `test_off_returns_balance_and_never_touches_the_snapshot` holds.

**Step 4: Run tests**

Run: `<prefix> packages/common/tests/test_margin_tradable_balance.py packages/common/tests/test_margin_accessors.py packages/common/tests/test_margin_settings.py`
Expected: PASS.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py packages/common/tests/test_margin_tradable_balance.py
git commit -m "feat(margin): get_tradable_balance / get_option_tradable_balance with the over-exposure warning"
```

---

### Task 5: Expert virtual balance reads the tradable balance (+ test fakes)

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:848-853` (in `get_virtual_balance`)
- Modify: `tests/test_available_balance_clamp.py:17-32` (`_FakeAccount`)
- Modify: `tests/test_option_lifecycle_service.py` and `tests/test_overview_account_scoped_widgets.py` (their duck-typed fakes that define `get_balance` but not `get_tradable_balance`; add the one-liner below to each)
- Test: `tests/test_margin_expert_sizing.py` (create)

**Step 1: Write the failing test**

```python
"""With margin on, every expert-side figure starts from the account's TRADABLE
balance, not its balance -- the whole point is to trade above what was invested.
"""
import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from tests import factories


class _Account:
    """Only what get_virtual_balance / get_available_balance read."""

    def __init__(self, id_val, *, balance, tradable, buying_power):
        self.id = id_val
        self._balance, self._tradable, self._bp = balance, tradable, buying_power
        self.tradable_calls = 0

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        self.tradable_calls += 1
        return self._tradable

    def get_account_info(self):
        return {"buying_power": self._bp}

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        return {} if isinstance(symbol_or_list, (list, tuple, set)) else None


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "margin sizing test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _with_account(account, fn):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _R:
        def get_account_instance(self, account_id):
            return account
    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return fn()
    finally:
        set_instance_resolver(prev)


@pytest.mark.usefixtures("reset_test_db")
def test_virtual_balance_is_tradable_times_pct():
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_Expert", virtual_equity_pct=50.0)
    account = _Account(acct_def.id, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0)
    assert _with_account(account, _Expert(inst.id).get_virtual_balance) == 9_000.0
    assert account.tradable_calls == 1


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_still_clamps_to_broker_buying_power():
    """The factor widens the base; the broker's remaining BP still caps the result."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    account = _Account(acct_def.id, balance=10_000.0, tradable=18_000.0, buying_power=5_000.0)
    assert _with_account(account, _Expert(inst.id).get_available_balance) == 5_000.0


@pytest.mark.usefixtures("reset_test_db")
def test_a_tradable_balance_error_yields_none_not_a_number():
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)

    class _Broken(_Account):
        def get_tradable_balance(self):
            raise ValueError("account published no buying power")
    account = _Broken(acct_def.id, balance=10_000.0, tradable=None, buying_power=None)
    assert _with_account(account, _Expert(inst.id).get_virtual_balance) is None
```

**Step 2: Run to verify failure**

Run: `<prefix> tests/test_margin_expert_sizing.py`
Expected: the first test FAILS with `assert 5000.0 == 9000.0` (balance x 50%).

**Step 3: Implement**

`MarketExpertInterface.get_virtual_balance`, replace the "Get account balance" block:
```python
            # The TRADABLE balance, not the balance: with margin on this is
            # balance x min(margin_factor, broker multiplier), so every expert-side
            # figure downstream (available balance, risk sizing, per-instrument cap)
            # scales with the account's leverage setting. Raises when the broker
            # published nothing usable; the except below turns that into None,
            # which every caller already treats as "cannot size".
            account_balance = account.get_tradable_balance()
```
Update the docstring's first line to say "tradable balance" and the log line to `Account tradable balance=`.

Then the duck-typed fakes. In `tests/test_available_balance_clamp.py::_FakeAccount` add:
```python
    def get_tradable_balance(self):
        return self._balance   # margin off in these tests
```
Do the same in the fakes of `tests/test_option_lifecycle_service.py` and `tests/test_overview_account_scoped_widgets.py` IF they reach `get_virtual_balance` (run the files; add the one-liner only where a test fails with `AttributeError: ... get_tradable_balance`). `tests/conftest.py`'s fake inherits `AccountInterface`, so it gets the base method and needs nothing.

**Step 4: Run tests**

Run: `<prefix> tests/test_margin_expert_sizing.py tests/test_available_balance_clamp.py tests/test_virtual_equity_zero_pct.py tests/test_option_lifecycle_service.py tests/test_overview_account_scoped_widgets.py tests/test_smart_rm_atr_sizing_seam.py tests/test_smart_rm_portfolio_equity_seam.py tests/test_live_enter_path.py tests/test_funded_entry_loop.py`
Expected: PASS.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/interfaces/MarketExpertInterface.py tests/test_margin_expert_sizing.py tests/test_available_balance_clamp.py tests/test_option_lifecycle_service.py tests/test_overview_account_scoped_widgets.py
git commit -m "feat(margin): expert virtual balance starts from the account's tradable balance"
```

---

### Task 6: The other expert-side readers: `_virtual_equity`, position-size limits, balance-usage chart

The classic RM (`TradeRiskManagement._risk_atr_quantity`, line 1262) and the Smart RM (`SmartRiskManagerToolkit._auto_size_by_risk`, line 1886) already take `equity = expert.get_virtual_balance()`, so Task 5 covered them. Three readers still start from `get_balance()`/`snapshot.equity`:

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py:2317-2320` (`_virtual_equity`)
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py:1288-1310` (`_validate_position_size_limits`)
- Modify: `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py:82-85`
- Test: `tests/test_margin_expert_sizing.py` (append)

**Step 1: Write the failing tests** (append to `tests/test_margin_expert_sizing.py`)

```python
def test_trade_action_virtual_equity_agrees_with_the_expert(monkeypatch):
    """PARITY PIN: TradeActions._virtual_equity and MarketExpertInterface.get_virtual_balance
    must be the same number for the same account -- one base (tradable), two callers."""
    from ba2_common.core.TradeActions import TradeAction  # the base class that owns _virtual_equity

    account = _Account(1, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0)
    action = TradeAction.__new__(TradeAction)
    action.account = account
    action.expert_recommendation = None          # -> pct defaults to 100
    assert action._virtual_equity() == 18_000.0


def test_position_size_limit_is_a_percent_of_tradable_balance():
    """max_virtual_equity_per_instrument_percent is applied to the TRADABLE balance."""
    # Look at tests/test_accounts/test_account_interface.py for how
    # _validate_position_size_limits is exercised (fake account + expert instance +
    # order). Reuse that harness. Set margin so get_tradable_balance() returns
    # 2x snapshot.equity, and assert an order sized at 1.5x the unlevered cap PASSES.
    ...
```
Read `tests/test_accounts/test_account_interface.py` first and copy its `_validate_position_size_limits` harness verbatim for the second test; the only new element is a fake whose `get_tradable_balance()` returns double its `get_account_snapshot().equity`.

**Step 2: Run to verify failure**

Run: `<prefix> tests/test_margin_expert_sizing.py -k "virtual_equity or position_size"`
Expected: FAIL (`10000 == 18000`; the order is rejected).

**Step 3: Implement**

`TradeActions._virtual_equity`:
```python
    def _virtual_equity(self) -> Optional[float]:
        """tradable balance * virtual_equity_pct/100 (defaults to the whole tradable
        balance when unknown). SAME base as MarketExpertInterface.get_virtual_balance:
        with margin on both scale by the account's factor, so a share-increase action
        and the expert's own sizing never disagree about the sleeve's size."""
        try:
            balance = self.account.get_tradable_balance()
        except Exception as e:
            logger.error(f"_virtual_equity: tradable balance unavailable for account "
                         f"{self.account.id}: {e}", exc_info=True)
            return None
```
(then the existing pct logic unchanged.)

`AccountInterface._validate_position_size_limits`: KEEP the `snapshot.equity` read and its None-refusal exactly as they are (the backtest account's `get_balance()` is spendable CASH by design while its snapshot equity is deployed equity; swapping the denominator to `get_tradable_balance()` would silently change every backtest's per-instrument cap, breaking BT/live byte-identity). Instead SCALE the denominator by the account's effective factor, added in Task 4's fix round (`effective_margin_factor()`: 1.0 with margin off and no snapshot read, else `min(margin_factor, broker multiplier)`):
```python
            account_equity = float(account_equity)
            try:
                account_equity *= self.effective_margin_factor()
            except Exception as e:
                logger.error(
                    f"POSITION SIZE VALIDATION CANNOT RUN for {trading_order.symbol}: "
                    f"account {self.id} margin factor unavailable ({e}). Rejecting the "
                    f"order rather than treating an unrun risk check as passed.", exc_info=True)
                errors.append(
                    f"Cannot validate position size limits: margin factor is unavailable "
                    f"from {self.__class__.__name__} ({e}). Refusing the order rather than "
                    f"skipping the check.")
                return errors
```
placed right after the existing `account_equity = float(account_equity)` line. Add a WHY comment: with margin on the cap is a percent of the TRADABLE balance (design 2026-09-08); with it off this multiplies by 1.0 and reads nothing, so backtests are unchanged.

`BalanceUsagePerExpertChart`, the balance fetch:
```python
                if acc_id not in balance_by_account:
                    acct = _get_account(acc_id)
                    try:
                        balance_by_account[acc_id] = acct.get_tradable_balance() if acct else None
                    except Exception as e:
                        logger.error(f"Tradable balance unavailable for account {acc_id}: {e}", exc_info=True)
                        balance_by_account[acc_id] = None
```
and fix the comment: "Virtual balance = account TRADABLE balance * virtual_equity_pct (same base as get_virtual_balance, so a margin account is not painted as over-allocated)".

**Step 4: Run tests**

Run: `<prefix> tests/test_margin_expert_sizing.py tests/test_accounts/test_account_interface.py tests/test_balance_usage_oversubscription.py tests/test_option_actions.py tests/test_no_zero_coercion.py`
Expected: PASS except the 2 baseline failures (`test_no_new_unknown_reads_as_zero`, `test_sell_covered_call_short_of_cover...`). If `test_no_new_unknown_reads_as_zero` reports a NEW file/line, read its message: it greps for `or 0`-style coercions and the new code must not add one.

**Step 5: Commit**
```
git add packages/common/ba2_common/core/TradeActions.py packages/common/ba2_common/core/interfaces/AccountInterface.py ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py tests/test_margin_expert_sizing.py
git commit -m "feat(margin): share-increase, position-size cap and balance-usage chart read the tradable balance"
```

---

### Task 7: Options: `available_option_buying_power` on the option tradable balance

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:2541-2555`
- Test: `tests/test_option_reserve.py` (append one test; read the file's existing fixture for `available_option_buying_power` first and reuse it)

**Step 1: Write the failing test**

```python
def test_available_option_buying_power_uses_the_option_tradable_balance(<existing fixture>):
    """Base is get_option_tradable_balance(), not get_balance(): a levered option sleeve
    gets the levered figure; with margin off the two are equal and nothing changes."""
    account.<make get_option_tradable_balance return 2x get_balance, e.g. monkeypatch>
    # with an empty reserve pool:
    assert account.available_option_buying_power() == 2 * account.get_balance()
```

**Step 2: Run to verify failure**

Run: `<prefix> tests/test_option_reserve.py -k option_tradable`
Expected: FAIL (`== balance`, not 2x).

**Step 3: Implement**

```python
        pool = self.reserved_option_buying_power_detail()
        if not pool.is_measurable:
            return None
        try:
            bal = self.get_option_tradable_balance()
        except Exception as e:
            logger.error(f"[Account {self.id}] option tradable balance unavailable: {e}", exc_info=True)
            return None
        return bal - pool.total
```
Docstring: "Option TRADABLE balance minus reserves". Keep the `None`-not-`0.0` paragraph.

**Step 4: Run tests**

Run: `<prefix> tests/test_option_reserve.py tests/test_option_assignment_capacity_account.py tests/test_option_actions.py tests/test_option_lifecycle_service.py`
Expected: PASS (minus the 1 baseline failure in test_option_actions).

**Step 5: Commit**
```
git add packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py tests/test_option_reserve.py
git commit -m "feat(margin): option buying power reserve base is the option tradable balance"
```

---

### Task 8: Header badge `Balance / BP` with breakdown

**Files:**
- Modify: `ba2_trade_platform/ui/layout.py` (`_BalanceEntry`, `_read_account_value`, `_refresh_one_balance`, `header_balance_from_cache`, `header_balance_breakdown`, `_render_account_balance`, `_paint`, `_paint_breakdown`)
- Test: `tests/test_header_account_balance.py` (append a section)

Design: the cache entry keeps ONE dated read per account that now carries four figures. The `HeaderBalance` decision logic is reused unchanged per figure by selecting a field. The badge text is `"{balance.text} / BP {tradable.text}"`; the breakdown menu lists per account: value, stock tradable, option tradable, broker BP.

**Step 1: Write the failing tests** (append to `tests/test_header_account_balance.py`; use the file's existing `_Broker`, `_use_brokers`, `Clock` helpers)

```python
# ---------------------------------------------------------------------------
# Margin: the badge shows balance AND buying power
# ---------------------------------------------------------------------------

class _MarginBroker(_Broker):
    def __init__(self, *, net_liquidation, tradable=None, option_tradable=None,
                 broker_bp=None, tradable_raises=None, **kw):
        super().__init__(net_liquidation=net_liquidation, **kw)
        self.snapshot.buying_power = broker_bp
        self._tradable, self._option_tradable, self._tradable_raises = tradable, option_tradable, tradable_raises

    def get_tradable_balance(self):
        if self._tradable_raises:
            raise self._tradable_raises
        return self._tradable

    def get_option_tradable_balance(self):
        return self._option_tradable


def test_the_badge_reads_balance_slash_bp(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _MarginBroker(net_liquidation=10_000.0, tradable=18_000.0,
                                                option_tradable=10_000.0, broker_bp=20_000.0)})
    layout.refresh_header_balance_cache([1], utcnow=clock)
    view = layout.header_badge_from_cache([(1, 'A')], utcnow=clock)
    assert view.text == '$10,000.00 / BP $18,000.00'


def test_a_failed_tradable_read_marks_bp_unknown_but_keeps_the_balance(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _MarginBroker(net_liquidation=10_000.0, broker_bp=1.0,
                                                tradable_raises=ValueError("no multiplier"))})
    layout.refresh_header_balance_cache([1], utcnow=clock)
    view = layout.header_badge_from_cache([(1, 'A')], utcnow=clock)
    assert view.text == '$10,000.00 / BP —'


def test_the_breakdown_lists_the_four_figures_per_account(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _MarginBroker(net_liquidation=10_000.0, tradable=18_000.0,
                                                option_tradable=10_000.0, broker_bp=20_000.0)})
    layout.refresh_header_balance_cache([1], utcnow=clock)
    bd = layout.header_balance_breakdown([(1, 'A')], utcnow=clock)
    (label, figures), = bd.lines
    assert label == 'A'
    assert [f.text for f in (figures.value, figures.tradable, figures.option_tradable, figures.broker_bp)] == \
        ['$10,000.00', '$18,000.00', '$10,000.00', '$20,000.00']


def test_bp_totals_across_accounts_and_marks_partial_like_the_balance(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _MarginBroker(net_liquidation=1.0, tradable=2.0, broker_bp=1.0),
                               2: _MarginBroker(net_liquidation=1.0, tradable=None, broker_bp=1.0)})
    layout.refresh_header_balance_cache([1, 2], utcnow=clock)
    view = layout.header_badge_from_cache([(1, 'A'), (2, 'B')], utcnow=clock)
    assert view.text == '$2.00 / BP $2.00 (partial)'
```
Note: `header_balance_from_cache` (existing, 61 tests) keeps its exact signature and behaviour for the `value` field. The new `header_badge_from_cache` composes the badge.

**Step 2: Run to verify failure**

Run: `<prefix> tests/test_header_account_balance.py -k "badge or breakdown_lists or bp_totals"`
Expected: FAIL, `AttributeError: module ... has no attribute 'header_badge_from_cache'`.

**Step 3: Implement** (in `layout.py`)

1. Replace `_BalanceEntry`:
```python
@dataclass(frozen=True)
class AccountFigures:
    """One dated broker read of an account: its value plus the three margin figures.
    Any of them may be None = could not be read; never zero for unknown."""
    value: Optional[float] = None
    tradable: Optional[float] = None
    option_tradable: Optional[float] = None
    broker_bp: Optional[float] = None


@dataclass
class _BalanceEntry:
    """(docstring as before; ``figures`` replaces ``value``)"""
    figures: AccountFigures = field(default_factory=AccountFigures)
    as_of: Optional[datetime] = None
    attempted_at: Optional[datetime] = None

    @property
    def value(self) -> Optional[float]:   # kept: the 61 existing tests and the balance path read it
        return self.figures.value
```
(`field` from dataclasses; add to the import.)

2. `_read_account_value` becomes `_read_account_figures(account_id) -> AccountFigures`:
```python
    account = get_account_instance_from_id(account_id)
    if account is None:
        return AccountFigures()
    snapshot = account.get_account_snapshot()
    value = account_value_from_snapshot(snapshot)

    def _try(name):
        # A margin figure that cannot be read leaves ONLY that figure unknown. The
        # balance is the headline; a broker with no multiplier must not blank it.
        try:
            return float(getattr(account, name)())
        except Exception as e:
            logger.warning(f"Header balance: {name} unreadable for account {account_id}: {e}")
            return None
    return AccountFigures(value=value,
                          tradable=_try('get_tradable_balance'),
                          option_tradable=_try('get_option_tradable_balance'),
                          broker_bp=snapshot.buying_power)
```
Keep a thin `_read_account_value(account_id)` returning `_read_account_figures(account_id).value` ONLY if an existing test monkeypatches it (grep the test file; if none does, delete it).

3. `_refresh_one_balance`: read `figures = _read_account_figures(account_id)`; treat `figures.value is None` exactly as the old `value is None` (failed read, keep previous); `changed = entry.figures != figures`; `entry.figures = figures`.

4. Generalise the cache readers with a field selector:
```python
def _figure_of(entry: Optional[_BalanceEntry], which: str) -> Optional[float]:
    return getattr(entry.figures, which) if entry is not None else None


def header_figure_from_cache(accounts, *, which: str = 'value', utcnow=_utcnow) -> HeaderBalance:
    reads = []
    oldest = None
    for account_id, label in accounts:
        entry = _BALANCE_CACHE.get(account_id)
        value = _figure_of(entry, which)
        reads.append((label, value))
        if value is not None and entry is not None and entry.as_of is not None:
            oldest = entry.as_of if oldest is None else min(oldest, entry.as_of)
    total, unreadable = combine_account_values(reads)
    return header_balance(value=total, as_of=oldest, now=utcnow(), unreadable=unreadable)


def header_balance_from_cache(accounts, *, utcnow=_utcnow) -> HeaderBalance:
    """(existing docstring) -- the account VALUE figure."""
    return header_figure_from_cache(accounts, which='value', utcnow=utcnow)


HEADER_BP_PREFIX = ' / BP '


def header_badge_from_cache(accounts, *, utcnow=_utcnow) -> HeaderBalance:
    """The badge: balance, then the stock tradable balance as 'BP'. The colour/glyph
    state is the BALANCE's; BP's own stale/partial markers ride in its text."""
    bal = header_figure_from_cache(accounts, which='value', utcnow=utcnow)
    bp = header_figure_from_cache(accounts, which='tradable', utcnow=utcnow)
    return HeaderBalance(text=bal.text + HEADER_BP_PREFIX + bp.text,
                         detail=bal.detail + HEADER_BP_DETAIL_FMT.format(detail=bp.detail),
                         available=bal.available, stale=bal.stale, partial=bal.partial)
```
with `HEADER_BP_DETAIL_FMT = '\nBuying power (tradable): {detail}'` next to the other `HEADER_BALANCE_*` constants.

5. Breakdown: `HeaderBalanceBreakdown.lines` becomes `Tuple[Tuple[str, AccountFigureViews], ...]` where
```python
@dataclass(frozen=True)
class AccountFigureViews:
    value: HeaderBalance
    tradable: HeaderBalance
    option_tradable: HeaderBalance
    broker_bp: HeaderBalance
```
built by calling `header_balance(...)` once per figure with the same `as_of`/`now` and `unreadable=() if v is not None else (label,)`. `total` stays the VALUE total (`header_balance_from_cache`). Check the existing breakdown tests: they read `line.text` per `(label, line)`; change those assertions to `line.value.text` (mechanical; list every test you touch in the commit body).

6. `_render_account_balance`: `_view_or_unknown` calls `header_badge_from_cache`. `_paint_breakdown`: each account row shows four small labels in order with headers `Balance`, `BP`, `Opt BP`, `Broker BP` (one header row above the account rows; classes `text-xs text-secondary-custom`).

**Step 4: Run tests**

Run: `<prefix> tests/test_header_account_balance.py`
Expected: all PASS (61 old + 4 new).

**Step 5: Commit**
```
git add ba2_trade_platform/ui/layout.py tests/test_header_account_balance.py
git commit -m "feat(margin): header badge shows Balance / BP with a four-figure breakdown"
```

---

### Task 9: Floating P/L per account card gains a BP cell

**Files:**
- Modify: `ba2_trade_platform/ui/components/FloatingPLPerAccountWidget.py` (`PLRow`, both balance fetch blocks at ~300 and ~619, `_draw`, `_balance_text`)
- Test: `tests/test_overview_account_scoped_widgets.py` or wherever `FloatingPLPerAccountWidget` rows are currently tested (grep `PLRow(` in tests; append there)

**Step 1: Write the failing test**

```python
def test_plrow_carries_tradable_and_broker_bp_and_the_bp_cell_formats_them():
    from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import PLRow, _bp_text
    row = PLRow(name='A', pl=1.0, balance=10_000.0, tradable=18_000.0, broker_bp=20_000.0)
    assert _bp_text(row.tradable) == 'BP: $18,000.00'
    assert _bp_text(None) == 'BP: unknown'
```
Plus one test through the existing row-building harness of that file asserting that a fake account whose `get_tradable_balance()` returns 18k yields `row.tradable == 18_000.0`, and one whose `get_tradable_balance()` raises yields `row.tradable is None` while `row.balance` is still set.

**Step 2: Run to verify failure**

Expected: `TypeError: PLRow.__init__() got an unexpected keyword argument 'tradable'`.

**Step 3: Implement**

`PLRow`: add `tradable: Optional[float] = None` and `broker_bp: Optional[float] = None` (docstring: same None-is-unknown contract).

Factor the duplicated balance fetch (lines ~300-313 and ~619-630) into one helper on the base class and call it from both places:
```python
    def _read_money(self, account, account_id) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """(balance, tradable, broker_bp), each None when unreadable, never zero."""
        if not self._show_balance:
            return None, None, None
        balance = tradable = broker_bp = None
        try:
            bal = account.get_balance()
            balance = float(bal) if bal is not None else None
            if bal is None:
                logger.warning(f"Balance unavailable for account {account_id}; showing it as unknown rather than as zero")
        except Exception as e:
            logger.error(f"Could not fetch balance for account {account_id}: {e}", exc_info=True)
        try:
            tradable = float(account.get_tradable_balance())
        except Exception as e:
            logger.warning(f"Tradable balance unavailable for account {account_id}: {e}")
        try:
            broker_bp = account.get_account_snapshot().buying_power
        except Exception as e:
            logger.warning(f"Broker buying power unavailable for account {account_id}: {e}")
        return balance, tradable, broker_bp
```
Thread `tradable=..., broker_bp=...` into every `PLRow(...)` construction in both `_rows_for_account` paths.

`_draw`: next to the balance label add
```python
                    if self._show_balance:
                        ui.label(_balance_text(row.balance)).classes('text-xs text-gray-500')
                        ui.label(_bp_text(row.tradable)).classes('text-xs text-gray-500') \
                            .tooltip(BROKER_BP_TOOLTIP_FMT.format(bp=_money_or_dash(row.broker_bp)))
```
and in the total row a `_bp_text(bp_total, partial=bool(bp_missing))` computed with `combine_measurements([(r.name, r.tradable) for r in rows])`. Add:
```python
UNKNOWN_BP_TEXT = 'BP: unknown'
BROKER_BP_TOOLTIP_FMT = 'Broker buying power: {bp}'


def _money_or_dash(value: Optional[float]) -> str:
    return '—' if value is None else f'${value:,.2f}'


def _bp_text(value: Optional[float], *, partial: bool = False) -> str:
    """The 'BP:' cell: the account's TRADABLE balance. None is unknown."""
    if value is None:
        return UNKNOWN_BP_TEXT
    return f'BP: ${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')
```

**Step 4: Run tests**

Run: `<prefix> tests/test_overview_account_scoped_widgets.py tests/test_no_zero_coercion.py` (+ the file you appended to)
Expected: PASS (baseline failure aside).

**Step 5: Commit**
```
git commit -am "feat(margin): Floating P/L per account shows BP (tradable) with broker BP on hover"
```

---

### Task 10: Live trades column `Value / CapReq`

**Files:**
- Modify: `ba2_trade_platform/ui/components/LiveTradesTable.py:77`
- Modify: `ba2_trade_platform/ui/pages/live_trades.py:392-400` (account map) and `:515-524` (value cell)
- Create: pure helper in `ba2_trade_platform/ui/utils/margin_view.py`
- Test: `tests/test_live_trades_capreq.py` (create)

**Step 1: Write the failing test**

```python
"""Live trades 'Value / CapReq': capital requirement = value / margin_factor when the
account has margin on, else the value itself."""
from ba2_trade_platform.ui.utils.margin_view import capital_requirement, value_capreq_text


def test_capreq_equals_value_with_margin_off():
    assert capital_requirement(1800.0, margin_enabled=False, margin_factor=1.8) == 1800.0


def test_capreq_is_value_over_factor_with_margin_on():
    assert capital_requirement(1800.0, margin_enabled=True, margin_factor=1.8) == 1000.0


def test_cell_text():
    assert value_capreq_text(1800.0, 1000.0) == '$1,800.00 / $1,000.00'
    assert value_capreq_text(None, None) == ''
```

**Step 2: Run to verify failure**

Expected: `ModuleNotFoundError: ba2_trade_platform.ui.utils.margin_view`.

**Step 3: Implement**

`ba2_trade_platform/ui/utils/margin_view.py`:
```python
"""Pure formatting for the margin figures shown on the live trades page."""
from typing import Optional


def capital_requirement(value: float, *, margin_enabled: bool, margin_factor: float) -> float:
    """Dollars of the account's own balance this position consumes.

    value / margin_factor with margin on (a $18k position on a 1.8x account ties up
    $10k of balance); the value itself with margin off.
    """
    if not margin_enabled:
        return float(value)
    return float(value) / float(margin_factor)


def value_capreq_text(value: Optional[float], capreq: Optional[float]) -> str:
    if value is None or capreq is None:
        return ''
    return f"${value:,.2f} / ${capreq:,.2f}"
```

`LiveTradesTable.py:77`: `label='Value / CapReq'` (field stays `value`; sorting stays on `value_numeric` if the table has one, else leave `sortable=True` on the text as today).

`live_trades.py`: where `account_names` is built, ALSO build `factor_by_account: Dict[int, float]` from the account's EFFECTIVE factor (added in Task 4's fix round; 1.0 with margin off, `min(margin_factor, broker multiplier)` with it on, so the cell agrees with the sizing that actually happened rather than with the raw setting):
```python
        from ba2_trade_platform.core.utils import get_account_instance_from_id
        factor_by_account = {}
        for acc_id in unique_account_ids:
            try:
                acct = get_account_instance_from_id(acc_id, session=session)
                factor_by_account[acc_id] = acct.effective_margin_factor()
            except Exception as e:
                logger.error(f"Effective margin factor unavailable for account {acc_id}: {e}", exc_info=True)
                # unknown, not "1.0": the cell shows the value alone, see below
```
and `capital_requirement(value, *, effective_factor)` becomes simply `value / effective_factor` (drop the `margin_enabled` parameter; the factor already encodes "off" as 1.0). Adjust the tests in step 1 accordingly (`capital_requirement(1800.0, effective_factor=1.0) == 1800.0`, `capital_requirement(1800.0, effective_factor=1.8) == 1000.0`).
and the value cell:
```python
            value_str = ''
            if txn.quantity and current_price_str:
                try:
                    current_price = current_prices.get(txn.symbol)
                    if current_price:
                        value = txn.quantity * current_price
                        acc_id = txn_to_account.get(txn.id)
                        factor = factor_by_account.get(acc_id)
                        if factor is None:
                            value_str = f"${value:,.2f}"          # factor unreadable: value alone
                        else:
                            value_str = value_capreq_text(
                                value, capital_requirement(value, effective_factor=factor))
                except Exception as e:
                    logger.debug(f"Could not calculate value for {txn.symbol}: {e}")
```
(`get_account_instance_from_id` is the cached live factory, so this is one lookup per account per render, not per row.)

**Step 4: Run tests**

Run: `<prefix> tests/test_live_trades_capreq.py` plus any existing live_trades page test (`grep -l live_trades tests`).
Expected: PASS.

**Step 5: Commit**
```
git add ba2_trade_platform/ui/utils/margin_view.py ba2_trade_platform/ui/components/LiveTradesTable.py ba2_trade_platform/ui/pages/live_trades.py tests/test_live_trades_capreq.py
git commit -m "feat(margin): live trades column Value / CapReq"
```

---

### Task 11: Settings dialog refuses `margin_factor < 1.0`

**Files:**
- Modify: `ba2_trade_platform/ui/pages/settings.py:853-856` (`save_account`, right after `dynamic_settings` is collected)
- Test: `tests/test_settings.py` (append; look at how that file drives `save_account` or its helpers. If it does not drive the dialog, test the pure guard below instead.)

**Step 1: Write the failing test**

```python
def test_account_settings_reject_a_margin_factor_below_one():
    from ba2_trade_platform.ui.pages.settings import account_settings_error
    assert account_settings_error({"margin_factor": 0.5}) is not None
    assert account_settings_error({"margin_factor": 1.8}) is None
    assert account_settings_error({}) is None          # not in the form: nothing to check
```

**Step 2: Run to verify failure**

Expected: `ImportError: cannot import name 'account_settings_error'`.

**Step 3: Implement**

Module level in `settings.py`:
```python
def account_settings_error(dynamic_settings: Dict[str, Any]) -> Optional[str]:
    """Why this account settings form must not be saved; None when it may. Pure."""
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import margin_factor_error
    if "margin_factor" in dynamic_settings:
        return margin_factor_error(dynamic_settings["margin_factor"])
    return None
```
In `save_account`, right after the `dynamic_settings` dict is filled:
```python
            problem = account_settings_error(dynamic_settings)
            if problem:
                ui.notify(problem, type='negative')
                logger.warning(f"Refused to save account settings: {problem}")
                return
```

**Step 4: Run tests**

Run: `<prefix> tests/test_settings.py`
Expected: PASS.

**Step 5: Commit**
```
git add ba2_trade_platform/ui/pages/settings.py tests/test_settings.py
git commit -m "feat(margin): account settings dialog refuses a margin factor below 1.0"
```

---

### Task 12: Full regression, versions, design-doc status

**Files:**
- Modify: `ba2_trade_platform/version.py` (`APP_VERSION` 2026.09.1134 -> 2026.09.1135)
- Modify: `testplatform/version.py` (`TEST_APP_VERSION` 2026.09.0022 -> 2026.09.0023)
- Modify: `docs/plans/2026-09-08-margin-trading-design.md` (Status line -> "implemented on feat/margin-trading, <date>")

**Step 1: Run the whole root suite**

Run: `<prefix> tests -x --deselect tests/test_no_zero_coercion.py::test_no_new_unknown_reads_as_zero --deselect "tests/test_option_actions.py::test_sell_covered_call_short_of_cover_returns_a_refusal_and_never_raises" --deselect "tests/test_tastytrade_account.py::test_time_in_force_survives_a_broker_round_trip"`
Expected: PASS. (Runtime several minutes; run in the background and wait.)

**Step 2: Run the package suites**

Run: `<prefix> packages/common/tests`
Expected: PASS (one known float-dust failure per the 2026-09-03 baseline is acceptable; anything mentioning margin is ours).

**Step 3: Bump both version files** (packages/ AND ba2_trade_platform/ changed). Check nobody is mid-grid before pushing (memory: never bump mid-run for distributed workers; the bump is committed now, pushed only at a job boundary).

**Step 4: Commit**
```
git add ba2_trade_platform/version.py testplatform/version.py docs/plans/2026-09-08-margin-trading-design.md
git commit -m "chore(margin): version bumps + design status"
```

**Step 5: Report** to the operator: commits list, test counts, the four baseline failures, and the follow-ups from the design doc (allocator under the factor, separate option factor, backtest leverage). Do NOT merge or push: merge timing is the operator's call (grid job boundary).
