# Backtest Equity Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An optional fixed dollar ceiling on the equity a backtest may deploy, so a strategy can be tested for stability across years independent of its start date.

**Architecture:** One private helper on `BacktestAccount` computes `min(cap, equity())`; the three money accessors consult it, so every downstream reader (sizer, buying power, margin, option rails) inherits the cap without knowing it exists. The recorded equity curve stays **real**; `results.build_results` converts it to a fixed-denominator synthetic curve at scoring time.

**Tech Stack:** Python 3.12, pytest, SQLModel. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-25-backtest-equity-cap-design.md`

---

## Before you start

**Read the spec first.** In particular the "Two quantities, deliberately separated" section — the
central risk in this feature is one leaking into the other, and every task below exists to keep
them apart.

**Environment.** venv is `venv/` (NOT `.venv/`). Backend tests need an explicit `PYTHONPATH` and
the backend's own `pytest.ini`; from the repo root you otherwise get ~57 collection errors because
root `pytest.ini` sets `testpaths = tests`:

```bash
W=/Users/bmigette/Documents/dev/BA2/BA2TradePlatform
export PYTHONPATH=$W/packages/common:$W/packages/providers:$W/packages/experts:$W
$W/venv/bin/python -m pytest -c $W/testplatform/backend/pytest.ini \
  --rootdir=$W/testplatform/backend $W/testplatform/backend/tests/
```

One backend failure is pre-existing and expected: `test_worker_server.py::test_logs_rejects_path_traversal`
is Windows-only. **Never mix `packages/*/tests/` with root `tests/` in one pytest invocation** —
each package has its own `conftest.py` with the same module name and pytest errors on the clash.

**House rules that this feature will trip over if ignored:**

- **Never use `caplog`** — use `_capture_errors(monkeypatch)`.
- **Freeze time, never to today.** Use a literal date.
- **Unknown is not zero.** This codebase has fixed 25 separate instances of a missing value
  becoming `0.0` and reading as measured. A period P&L of exactly `0.0` is a measured flat period;
  an unmeasurable one is not.
- **No `or 0.0` on a money field.** Use an explicit `is None` test.
- Never write to `~/Documents/ba2/trade/db.sqlite` or `~/Documents/ba2_trade_platform/db.sqlite`
  (real money data — read-only `mode=ro&immutable=1` or not at all). Never `~/Downloads/`.
- `git add <explicit paths>` only. Never `git add -A`.

## Key facts established by reading the code

Do not re-derive these; they are load-bearing for the tasks below.

| fact | where |
|---|---|
| `BacktestAccount.equity()` is `self._cash + self._open_positions_mtm()` | `backtest_account.py:681-683` |
| `get_balance()` returns **`self._cash`** — the cash ledger, NOT equity | `backtest_account.py:1155-1157` |
| `get_account_info()` returns `balance`/`cash`/`equity`/`buying_power`; `buying_power` is `max(self._cash, 0.0)` | `backtest_account.py:1159-1173` |
| `get_account_snapshot` is **not** overridden on `BacktestAccount`; it inherits the base | — |
| `snapshot_equity` records `net_liquidating_value`, clamps `<= 0` to `0.0` and sets `_wiped_out`, and **raises** on non-finite | `backtest_account.py:1072-1110` |
| `build_results` builds `equity_curve` from `s["net_liquidating_value"]` | `results.py:131-133` |
| `_drawdown_curve(equity_curve)` produces the drawdown series | `results.py:295-302` |
| `results.py` reads account settings as `config["account_settings"][...]` | `results.py:150` |
| the config is built at `daily_backtest_handler._build_config`, `account_settings` at `:388` | `daily_backtest_handler.py:387-436` |
| the launcher's `--initial-capital` is `op.add_argument` at `:3893`, consumed at `:2881`/`:3187` | `ba2test_launcher.py` |

---

## File structure

| file | responsibility |
|---|---|
| `testplatform/backend/app/services/backtest/equity_cap.py` | **NEW.** Pure functions: validation, deployed-equity, and the scoring conversion. No I/O, no engine imports. All the arithmetic lives here so it is unit-testable without a backtest. |
| `testplatform/backend/app/services/backtest/backtest_account.py` | Consults `deployed_equity()` from the three money accessors. Nothing else changes. |
| `testplatform/backend/app/services/backtest/results.py` | Calls the scoring conversion when a cap is set. |
| `testplatform/backend/app/services/backtest/daily_backtest_handler.py` | Carries `equity_cap` into `account_settings`, validated. |
| `testplatform/ba2test_launcher.py` | `--equity-cap` as a run-level parameter. |
| `testplatform/backend/tests/backtest/test_equity_cap.py` | **NEW.** Unit tests for the pure module. |
| `testplatform/backend/tests/backtest/test_equity_cap_e2e.py` | **NEW.** The end-to-end runs. |

Putting every calculation in one pure module is deliberate: the two quantities must be visibly
separate, and a reviewer should be able to read both conversions side by side without an engine in
the way.

---

## Task 1: The pure module — validation

**Files:**
- Create: `testplatform/backend/app/services/backtest/equity_cap.py`
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The optional fixed-notional equity cap. Pure arithmetic, no engine, no clock."""
import math
import pytest

from app.services.backtest.equity_cap import (
    EquityCapError, validate_equity_cap,
)


def test_none_means_the_feature_is_off():
    assert validate_equity_cap(None) is None


def test_a_positive_cap_is_returned_as_a_float():
    assert validate_equity_cap(20_000) == 20_000.0
    assert isinstance(validate_equity_cap(20_000), float)


@pytest.mark.parametrize("bad", [0, 0.0, -1, -20_000.0])
def test_a_non_positive_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be greater than zero"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", ["20000", "", [], {}, object()])
def test_a_non_numeric_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be finite"):
        validate_equity_cap(bad)


def test_a_bool_is_not_a_number():
    """True == 1 in Python. A boolean reaching a money field is a caller bug, not a $1 cap."""
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(True)


def test_a_cap_above_the_initial_capital_is_allowed_and_says_so(caplog_free_logger):
    """It cannot bind YET, but the account may grow into it. Not an error."""
    msgs = []
    assert validate_equity_cap(50_000, initial_capital=20_000, log=msgs.append) == 50_000.0
    assert any("cannot bind" in m and "50,000" in m and "20,000" in m for m in msgs), msgs


def test_a_cap_at_or_below_the_initial_capital_logs_nothing():
    msgs = []
    validate_equity_cap(20_000, initial_capital=20_000, log=msgs.append)
    validate_equity_cap(5_000, initial_capital=20_000, log=msgs.append)
    assert msgs == []
```

Add this fixture at the top of the test file (the house rule forbids `caplog`):

```python
@pytest.fixture
def caplog_free_logger():
    """Named only to make the no-caplog rule visible at the call site."""
    return None
```

- [ ] **Step 2: Run the tests, watch them fail**

Run:
```bash
W=/Users/bmigette/Documents/dev/BA2/BA2TradePlatform
export PYTHONPATH=$W/packages/common:$W/packages/providers:$W/packages/experts:$W
$W/venv/bin/python -m pytest -c $W/testplatform/backend/pytest.ini \
  --rootdir=$W/testplatform/backend \
  $W/testplatform/backend/tests/backtest/test_equity_cap.py -v
```
Expected: collection error — `ModuleNotFoundError: No module named 'app.services.backtest.equity_cap'`.

- [ ] **Step 3: Write the module**

```python
"""The optional fixed-notional equity cap for a backtest.

A backtest compounds, so a strategy that did well in year one deploys larger positions ever
after and its later results are carried by its earlier luck. This holds the capital still, so a
result is about the strategy rather than about when it started.

TWO QUANTITIES LIVE HERE AND MUST NOT BE CONFLATED:

  deployed_equity()  -- what the SIZER, buying power, margin and the option rails may see.
                        Capped. Never reaches the recorded equity curve.
  scoring_curve()    -- what the METRICS see. Built from the REAL recorded equity, with every
                        period's return divided by the FIXED cap so a steady strategy reads the
                        same percentage every year.

Feeding the capped figure into scoring would report zero P&L for every period spent above the
cap -- the strategy would appear to stop earning the moment it succeeded.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence


class EquityCapError(ValueError):
    """A configured equity cap that cannot be honoured. Raised at config time, never mid-run."""


def validate_equity_cap(
    raw: Any,
    *,
    initial_capital: Optional[float] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[float]:
    """Normalise a configured cap. ``None`` means the feature is off.

    A cap ABOVE the initial capital is allowed -- it cannot bind yet, but the account may grow
    into it. That is a fact worth logging, not an error.
    """
    if raw is None:
        return None
    # bool is an int subclass; a boolean in a money field is a caller bug, not a $1 cap.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise EquityCapError(
            f"equity_cap must be a number or None, got {type(raw).__name__}: {raw!r}")
    cap = float(raw)
    if not math.isfinite(cap):
        raise EquityCapError(f"equity_cap must be finite, got {cap!r}")
    if cap <= 0:
        raise EquityCapError(
            f"equity_cap must be greater than zero, got {cap:,.2f}. A cap of zero would make "
            f"every position unaffordable; omit the setting to disable the feature.")
    if initial_capital is not None and cap > float(initial_capital):
        if log is not None:
            log(f"equity_cap {cap:,.2f} is above the initial capital "
                f"{float(initial_capital):,.2f}, so it cannot bind until the account grows to it.")
    return cap
```

- [ ] **Step 4: Run the tests, watch them pass**

Same command as Step 2. Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/equity_cap.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): equity cap config validation"
```

---

## Task 2: The pure module — deployed equity

**Files:**
- Modify: `testplatform/backend/app/services/backtest/equity_cap.py`
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.services.backtest.equity_cap import deployed_equity


def test_with_no_cap_the_real_equity_passes_through():
    assert deployed_equity(37_412.55, cap=None) == 37_412.55


def test_above_the_cap_only_the_cap_is_deployed():
    assert deployed_equity(40_000.0, cap=20_000.0) == 20_000.0


def test_below_the_cap_the_real_equity_is_deployed():
    """'except if account value goes below' -- a drawdown genuinely shrinks what can be deployed."""
    assert deployed_equity(15_000.0, cap=20_000.0) == 15_000.0


def test_exactly_at_the_cap():
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0


def test_recovery_climbs_back_to_the_cap_and_stops():
    assert deployed_equity(18_000.0, cap=20_000.0) == 18_000.0
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0
    assert deployed_equity(25_000.0, cap=20_000.0) == 20_000.0


def test_a_wiped_out_account_deploys_nothing_not_the_cap():
    assert deployed_equity(0.0, cap=20_000.0) == 0.0


def test_negative_equity_is_not_raised_to_zero_here():
    """The caller decides what a negative account means; this function does not invent a floor."""
    assert deployed_equity(-500.0, cap=20_000.0) == -500.0


def test_unmeasurable_equity_is_unmeasurable_not_zero():
    """None in means None out. A broker/engine that cannot state equity has not stated zero."""
    assert deployed_equity(None, cap=20_000.0) is None
    assert deployed_equity(None, cap=None) is None
```

- [ ] **Step 2: Run them, watch them fail**

Run the same command as Task 1 Step 2.
Expected: `ImportError: cannot import name 'deployed_equity'`.

- [ ] **Step 3: Implement**

Append to `equity_cap.py`:

```python
def deployed_equity(real_equity: Optional[float],
                    *, cap: Optional[float]) -> Optional[float]:
    """The equity the SIZER may see: ``min(cap, real_equity)``.

    ``real_equity`` includes unrealised marks, deliberately: an open position that is down
    genuinely leaves less to deploy, and one that is up is exactly the excess the cap withholds.

    ``None`` in, ``None`` out -- an engine that cannot state its equity has not stated zero.
    """
    if real_equity is None:
        return None
    if cap is None:
        return real_equity
    return min(float(cap), float(real_equity))
```

- [ ] **Step 4: Run them, watch them pass**

Expected: `16 passed`.

- [ ] **Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/equity_cap.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): deployed_equity - min(cap, real), None-preserving"
```

---

## Task 3: The pure module — the scoring conversion

This is the task the whole feature turns on. Read the spec's "Scoring series" section before
starting.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/equity_cap.py`
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime

from app.services.backtest.equity_cap import scoring_curve, capped_drawdown_curve


def _pt(y, equity):
    return {"date": datetime(y, 1, 1), "equity": float(equity)}


def test_five_k_a_year_on_twenty_k_reads_twenty_five_percent_every_year():
    """THE headline case. The naive `cap + cumulative P&L` curve would read
    25 / 20 / 16.7 / 14.3 for this identical strategy; that decline is the compounding
    effect this feature exists to remove."""
    real = [_pt(2020, 20_000), _pt(2021, 25_000), _pt(2022, 30_000),
            _pt(2023, 35_000), _pt(2024, 40_000)]
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 25_000.0, 31_250.0, 39_062.5, 48_828.125])


def test_the_curve_keeps_its_dates():
    real = [_pt(2020, 20_000), _pt(2021, 25_000)]
    assert [p["date"] for p in scoring_curve(real, cap=20_000.0)] == \
           [datetime(2020, 1, 1), datetime(2021, 1, 1)]


def test_with_no_cap_the_curve_is_returned_untouched():
    real = [_pt(2020, 20_000), _pt(2021, 25_000)]
    assert scoring_curve(real, cap=None) == real


def test_a_flat_period_is_a_measured_zero_not_a_missing_one():
    real = [_pt(2020, 20_000), _pt(2021, 20_000), _pt(2022, 25_000)]
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 20_000.0, 25_000.0])


def test_a_loss_period_compounds_downward_on_the_fixed_denominator():
    real = [_pt(2020, 20_000), _pt(2021, 18_000)]     # -2,000 = -10% of the 20k cap
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 18_000.0])


def test_the_denominator_is_the_cap_not_the_running_equity():
    """A +2,000 period reads +10% whether it happens first or last."""
    early = scoring_curve([_pt(2020, 20_000), _pt(2021, 22_000)], cap=20_000.0)
    late = scoring_curve([_pt(2020, 20_000), _pt(2021, 20_000), _pt(2022, 22_000)],
                         cap=20_000.0)
    first_step = early[1]["equity"] / early[0]["equity"] - 1.0
    last_step = late[2]["equity"] / late[1]["equity"] - 1.0
    assert first_step == pytest.approx(0.10)
    assert last_step == pytest.approx(0.10)


def test_a_single_point_curve_has_no_return_to_compute():
    assert [p["equity"] for p in scoring_curve([_pt(2020, 20_000)], cap=20_000.0)] == [20_000.0]


def test_an_empty_curve_stays_empty():
    assert scoring_curve([], cap=20_000.0) == []


def test_a_two_thousand_drawdown_is_ten_percent_whenever_it_happens():
    """Risk is denominated in the cap too, or dd_guard rewards a late-run strategy by
    arithmetic alone (dd_guard = min(20/max(dd,1), 2.0))."""
    early = [_pt(2020, 20_000), _pt(2021, 18_000), _pt(2022, 20_000)]
    late = [_pt(2020, 20_000), _pt(2021, 40_000), _pt(2022, 38_000)]
    assert min(p["drawdown"] for p in capped_drawdown_curve(early, cap=20_000.0)) \
        == pytest.approx(-10.0)
    assert min(p["drawdown"] for p in capped_drawdown_curve(late, cap=20_000.0)) \
        == pytest.approx(-10.0)


def test_the_capped_drawdown_curve_keeps_its_dates_and_starts_flat():
    pts = capped_drawdown_curve([_pt(2020, 20_000), _pt(2021, 18_000)], cap=20_000.0)
    assert [p["date"] for p in pts] == [datetime(2020, 1, 1), datetime(2021, 1, 1)]
    assert pts[0]["drawdown"] == pytest.approx(0.0)
```

- [ ] **Step 2: Run them, watch them fail**

Expected: `ImportError: cannot import name 'scoring_curve'`.

- [ ] **Step 3: Implement**

Append to `equity_cap.py`:

```python
def scoring_curve(equity_curve: Sequence[Dict[str, Any]],
                  *, cap: Optional[float]) -> List[Dict[str, Any]]:
    """Restate a REAL equity curve on a fixed denominator, for the metrics.

    Each period's return is ``period_pnl / cap`` -- the CAP, never the running equity -- and the
    returns are compounded. $5,000 a year on a $20,000 cap therefore reads 25% every year rather
    than 25 / 20 / 16.7 / 14.3, and a steady strategy scores the same whatever year it started.

    ``equity_curve`` must be the REAL recorded series. Differencing the capped figure would report
    zero P&L for every period spent above the cap.

    A "period" is one point of the recorded curve -- whatever granularity ``snapshot_equity``
    wrote. No resampling: a second time base would let this curve and the trade ledger disagree
    about when a return happened.
    """
    if cap is None:
        return list(equity_curve)
    pts = list(equity_curve)
    if not pts:
        return []
    out = [{**pts[0], "equity": float(cap)}]
    level = float(cap)
    for prev, cur in zip(pts, pts[1:]):
        period_pnl = float(cur["equity"]) - float(prev["equity"])
        level *= (1.0 + period_pnl / float(cap))
        out.append({**cur, "equity": level})
    return out


def capped_drawdown_curve(equity_curve: Sequence[Dict[str, Any]],
                          *, cap: Optional[float]) -> List[Dict[str, Any]]:
    """Peak-to-trough on cumulative P&L, divided by the CAP.

    A $2,000 trough is 10% of a $20,000 cap whenever it happens. On the compounded scoring curve
    it would read -10% in year one and -5% in year four -- risk on a moving denominator while
    returns sit on a fixed one, which hands a late-run strategy a better ``dd_guard`` multiplier
    for no reason but arithmetic.
    """
    pts = list(equity_curve)
    if not pts:
        return []
    if cap is None:
        raise EquityCapError(
            "capped_drawdown_curve called with cap=None; use results._drawdown_curve instead")
    base = float(pts[0]["equity"])
    peak_pnl = 0.0
    out: List[Dict[str, Any]] = []
    for pt in pts:
        pnl = float(pt["equity"]) - base
        peak_pnl = max(peak_pnl, pnl)
        out.append({"date": pt["date"], "drawdown": (pnl - peak_pnl) / float(cap) * 100.0})
    return out
```

- [ ] **Step 4: Run them, watch them pass**

Expected: `26 passed`.

- [ ] **Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/equity_cap.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): fixed-denominator scoring curve and cap-denominated drawdown"
```

---

## Task 4: Cap the account's money surface

**Files:**
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py` (`__init__`, `get_balance` at `:1155`, `get_account_info` at `:1159`)
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

Append to the test file. Use the same `BacktestAccount` construction the existing tests in
`testplatform/backend/tests/backtest/test_option_fills.py` use — read one and copy its fixture
rather than inventing a new way to build an account.

```python
def test_with_no_cap_the_account_reports_its_real_money(capped_account):
    acct = capped_account(cash=40_000.0, cap=None)
    assert acct.get_balance() == 40_000.0
    assert acct.get_account_info()["equity"] == 40_000.0
    assert acct.get_account_info()["buying_power"] == 40_000.0


def test_above_the_cap_equity_and_buying_power_are_capped(capped_account):
    acct = capped_account(cash=40_000.0, cap=20_000.0)
    assert acct.get_account_info()["equity"] == 20_000.0
    assert acct.get_account_info()["buying_power"] == 20_000.0
    assert acct.get_balance() == 20_000.0


def test_below_the_cap_the_real_figures_are_reported(capped_account):
    acct = capped_account(cash=15_000.0, cap=20_000.0)
    assert acct.get_account_info()["equity"] == 15_000.0
    assert acct.get_balance() == 15_000.0


def test_cash_is_never_reported_above_what_is_actually_held(capped_account):
    """Cap 20k, equity 40k, but only 5k in cash because the rest is invested. You cannot spend
    money you do not have, so the cap must not RAISE the cash figure."""
    acct = capped_account(cash=5_000.0, cap=20_000.0, mtm=35_000.0)
    assert acct.get_balance() == 5_000.0


def test_buying_power_never_goes_negative(capped_account):
    acct = capped_account(cash=-500.0, cap=20_000.0)
    assert acct.get_account_info()["buying_power"] == 0.0


def test_the_recorded_equity_curve_is_NEVER_capped(capped_account):
    """The cap must not reach snapshot_equity or the run's own history becomes
    unreconstructable -- and the scoring curve would then report zero P&L for every period
    spent above the cap."""
    acct = capped_account(cash=40_000.0, cap=20_000.0)
    snap = acct.snapshot_equity(datetime(2024, 3, 15, 16, 0))
    assert snap["net_liquidating_value"] == 40_000.0
    assert snap["cash_balance"] == 40_000.0
```

Add the fixture:

```python
@pytest.fixture
def capped_account(monkeypatch):
    """A BacktestAccount with a known cash/MTM and an optional cap."""
    from app.services.backtest.backtest_account import BacktestAccount

    def _make(*, cash, cap, mtm=0.0):
        acct = BacktestAccount.__new__(BacktestAccount)   # bypass the engine-heavy __init__
        acct._cash = float(cash)
        acct._equity_cap = cap
        acct._positions = {}
        acct._equity_snapshots = []
        acct._snapshot_dates = []
        acct._wiped_out = False
        monkeypatch.setattr(acct, "_open_positions_mtm", lambda: float(mtm))
        return acct
    return _make
```

- [ ] **Step 2: Run them, watch them fail**

Expected: `AttributeError: 'BacktestAccount' object has no attribute '_equity_cap'` on the
no-cap test, then wrong values on the capped ones.

- [ ] **Step 3: Implement**

In `BacktestAccount.__init__`, beside the other config reads, add:

```python
        # Optional fixed-notional cap. None = off; every path is then byte-identical to before.
        # Validated at config time (daily_backtest_handler), so a bad value never reaches here.
        self._equity_cap: Optional[float] = cfg.get("equity_cap")
```

Add the helper next to `equity()` (`backtest_account.py:681`):

```python
    def deployed_equity(self) -> float:
        """Equity the SIZER may see: ``min(cap, equity())``. Uncapped when no cap is set.

        Every money accessor routes through here so the cap is enforced at ONE seam. Capping
        inside the risk manager instead would leave buying power and margin reading the real
        balance, letting a margin account deploy twice the cap while appearing capped.
        """
        from app.services.backtest.equity_cap import deployed_equity as _deployed
        return _deployed(self.equity(), cap=self._equity_cap)
```

Replace `get_balance` (`:1155-1157`) with:

```python
    def get_balance(self) -> Optional[float]:
        """Spendable cash, never more than the deployed equity allows.

        ``min`` in both directions matters: the cap must not RAISE cash above what is actually
        held (money in open positions is not spendable), and cash must not exceed the cap when
        the account is sitting flat above it.
        """
        if self._equity_cap is None:
            return self._cash
        return min(self._cash, self.deployed_equity())
```

Replace the `get_account_info` body (`:1159-1173`) with:

```python
    def get_account_info(self) -> Dict[str, Any]:
        """Account info dict; exposes ``.equity`` (read by _validate_position_size_limits)."""
        eq = self.deployed_equity()
        cash = self.get_balance()
        return _AttrDict(
            {
                "balance": cash,
                "cash": cash,
                "equity": eq,
                "buying_power": max(cash, 0.0),
            }
        )
```

**Do not touch `snapshot_equity`.** It must keep recording real equity.

- [ ] **Step 4: Run them, watch them pass**

Expected: `32 passed`.

- [ ] **Step 5: Run the full backend suite — nothing may move**

Run the full backend command from "Before you start".
Expected: the same counts as before this task, plus your new tests, with only the known
Windows-only failure.

- [ ] **Step 6: Commit**

```bash
git add testplatform/backend/app/services/backtest/backtest_account.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): cap the account money surface at one seam"
```

---

## Task 5: Wire the scoring conversion into results

**Files:**
- Modify: `testplatform/backend/app/services/backtest/results.py:131-152`
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_results_scores_a_capped_run_on_the_fixed_denominator(monkeypatch):
    """$5k a year on a $20k cap: 25% a year, CAGR 25%, total return 144%."""
    from app.services.backtest import results as R

    account = _fake_account_with_snapshots([
        (datetime(2020, 1, 1), 20_000.0), (datetime(2021, 1, 1), 25_000.0),
        (datetime(2022, 1, 1), 30_000.0), (datetime(2023, 1, 1), 35_000.0),
        (datetime(2024, 1, 1), 40_000.0),
    ])
    cfg = _base_config(initial_capital=20_000.0, equity_cap=20_000.0)
    out = R.build_results(account, cfg)
    assert out["equity_curve"][-1]["equity"] == pytest.approx(48_828.125)
    assert out["annualized_return"] == pytest.approx(25.0, abs=0.05)
    assert out["total_return"] == pytest.approx(144.14, abs=0.05)


def test_the_same_run_uncapped_scores_on_the_real_curve(monkeypatch):
    from app.services.backtest import results as R
    account = _fake_account_with_snapshots([
        (datetime(2020, 1, 1), 20_000.0), (datetime(2024, 1, 1), 40_000.0),
    ])
    out = R.build_results(account, _base_config(initial_capital=20_000.0, equity_cap=None))
    assert out["equity_curve"][-1]["equity"] == pytest.approx(40_000.0)
    assert out["total_return"] == pytest.approx(100.0, abs=0.05)


def test_a_capped_runs_drawdown_is_denominated_in_the_cap():
    from app.services.backtest import results as R
    account = _fake_account_with_snapshots([
        (datetime(2020, 1, 1), 20_000.0), (datetime(2021, 1, 1), 40_000.0),
        (datetime(2022, 1, 1), 38_000.0),
    ])
    out = R.build_results(account, _base_config(initial_capital=20_000.0, equity_cap=20_000.0))
    assert out["max_drawdown"] == pytest.approx(-10.0, abs=0.01)
```

Write `_fake_account_with_snapshots` and `_base_config` as real helpers in the test file —
read `testplatform/backend/tests/backtest/test_results_metrics.py` and copy its config shape so
the keys match what `build_results` actually requires (it reads
`config["account_settings"]["commission_per_trade"]` among others).

- [ ] **Step 2: Run them, watch them fail**

Expected: the capped test reports `40_000.0` and `total_return == 100.0` — the real curve,
because nothing converts it yet.

- [ ] **Step 3: Implement**

In `results.build_results`, immediately after `equity_curve` is built (`results.py:131-133`) and
**before** `drawdown_curve` is computed (`:136`):

```python
    # Fixed-notional runs are scored on a FIXED denominator: each period's return is
    # period P&L / cap, compounded. See equity_cap.scoring_curve. The recorded series stays
    # real; only the scored one is restated.
    _cap = validate_equity_cap(config["account_settings"].get("equity_cap"))
    if _cap is not None:
        drawdown_curve = capped_drawdown_curve(equity_curve, cap=_cap)
        equity_curve = scoring_curve(equity_curve, cap=_cap)
    else:
        drawdown_curve = _drawdown_curve(equity_curve)
```

and delete the now-duplicated `drawdown_curve = _drawdown_curve(equity_curve)` at `:136`.

Add the import at the top of `results.py`:

```python
from app.services.backtest.equity_cap import (
    capped_drawdown_curve, scoring_curve, validate_equity_cap,
)
```

**Order matters and is easy to get wrong:** `capped_drawdown_curve` takes the **real** curve, so
it must be computed *before* `equity_curve` is reassigned. Reversing those two lines silently
measures drawdown on the synthetic curve — which is exactly the defect the cap-denominated
drawdown exists to prevent, and it produces a plausible smaller number.

- [ ] **Step 4: Run them, watch them pass**

Expected: `35 passed`.

- [ ] **Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/results.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): score a capped run on the fixed denominator"
```

---

## Task 6: Config plumbing and validation

**Files:**
- Modify: `testplatform/backend/app/services/backtest/daily_backtest_handler.py:169`, `:387-436`
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_config_carries_the_cap_into_account_settings():
    from app.services.backtest.daily_backtest_handler import _build_config
    cfg = _build_config(_minimal_payload(initial_capital=20_000.0, equity_cap=20_000.0))
    assert cfg["account_settings"]["equity_cap"] == 20_000.0


def test_an_absent_cap_is_None_not_zero():
    from app.services.backtest.daily_backtest_handler import _build_config
    cfg = _build_config(_minimal_payload(initial_capital=20_000.0))
    assert cfg["account_settings"]["equity_cap"] is None


def test_a_bad_cap_is_refused_at_CONFIG_time_not_mid_run():
    from app.services.backtest.daily_backtest_handler import _build_config
    from app.services.backtest.equity_cap import EquityCapError
    with pytest.raises(EquityCapError, match="greater than zero"):
        _build_config(_minimal_payload(initial_capital=20_000.0, equity_cap=0))
```

Write `_minimal_payload` by reading what `_build_config` requires at
`daily_backtest_handler.py:387-436` — do not guess the keys.

- [ ] **Step 2: Run them, watch them fail**

Expected: `KeyError: 'equity_cap'`.

- [ ] **Step 3: Implement**

Add `"equity_cap"` to the `_ACCOUNT_SETTING_KEYS` tuple at `daily_backtest_handler.py:169`, and
in `_build_config` beside `initial_capital = float(payload["initial_capital"])` (`:387`):

```python
    equity_cap = validate_equity_cap(
        payload.get("equity_cap"),
        initial_capital=initial_capital,
        log=logger.info,
    )
```

and add `"equity_cap": equity_cap,` to the `account_settings` dict at `:388`.

Import at the top: `from app.services.backtest.equity_cap import validate_equity_cap`.

- [ ] **Step 4: Run them, watch them pass**

- [ ] **Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/daily_backtest_handler.py \
        testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "feat(backtest): equity_cap config plumbing, validated at config time"
```

---

## Task 7: GA run-level parameter

**Files:**
- Modify: `testplatform/ba2test_launcher.py:3893` (argparse), `:2881`, `:3187` (the two config builders)
- Test: `testplatform/backend/tests/test_equity_cap_launcher.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
def test_the_launcher_accepts_an_equity_cap():
    import ba2test_launcher as L
    ns = L.build_parser().parse_args(["optimize", "--equity-cap", "20000"])
    assert ns.equity_cap == 20_000.0


def test_the_cap_defaults_to_off():
    import ba2test_launcher as L
    assert L.build_parser().parse_args(["optimize"]).equity_cap is None


def test_the_cap_reaches_account_settings():
    import ba2test_launcher as L
    cfg = L._build_optimization_config(_ns(equity_cap=20_000.0))
    assert cfg["backtest"]["account_settings"]["equity_cap"] == 20_000.0


def test_the_cap_is_NOT_a_gene():
    """A gene would optimise the CAPITAL rather than the strategy -- the opposite of the point --
    and every individual would then be scored against a different denominator."""
    from app.services.strategy_param_space import collect_param_space
    space = collect_param_space(_strategy_with_cap(20_000.0))
    assert not any("equity_cap" in k for k in space), \
        [k for k in space if "equity_cap" in k]
```

Read `ba2test_launcher.py` around `:2881` and `:3187` to find the real function names for the two
config builders and write `_ns` / `_strategy_with_cap` against them; the names above are
placeholders **you must replace with the real ones**.

- [ ] **Step 2: Run them, watch them fail**

- [ ] **Step 3: Implement**

Beside `op.add_argument("--initial-capital", type=float, default=10000.0)` at `:3893`:

```python
    op.add_argument("--equity-cap", type=float, default=None,
                    help="Optional FIXED equity the risk manager sizes against, in dollars "
                         "(default: off, i.e. the account compounds). With a cap set, profits "
                         "above it are not deployed and losses below it are real, so every year "
                         "faces the same capital -- use this to test whether a setting is stable "
                         "across years independent of its start date. Run-level, never a gene.")
```

At both `:2881` and `:3187`, add to the `account_settings` dict:

```python
                    "equity_cap": args.equity_cap,
```

- [ ] **Step 4: Run them, watch them pass**

- [ ] **Step 5: Commit**

```bash
git add testplatform/ba2test_launcher.py \
        testplatform/backend/tests/test_equity_cap_launcher.py
git commit -m "feat(ga): --equity-cap as a run-level parameter, never a gene"
```

---

## Task 8: Live safety

**Files:**
- Test: `testplatform/backend/tests/backtest/test_equity_cap.py`

- [ ] **Step 1: Write the failing test**

```python
def test_no_LIVE_account_class_can_reach_the_equity_cap():
    """This is a backtest analysis tool. A live account must have no code path to it --
    asserted rather than assumed, because 'we only call it from the backtest' is exactly the
    kind of claim that stops being true."""
    from ba2_common.core.interfaces.AccountInterface import AccountInterface
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    for cls in (AccountInterface, ReadOnlyAccountInterface, AlpacaAccount, TastyTradeAccount):
        names = dir(cls)
        assert "deployed_equity" not in names, f"{cls.__name__} exposes deployed_equity"
        assert "_equity_cap" not in names, f"{cls.__name__} exposes _equity_cap"
```

- [ ] **Step 2: Run it — it should PASS immediately**

This is a guard, not a red-green test: nothing has been added to those classes. Prove it bites by
temporarily adding `deployed_equity = None` to `AccountInterface`, re-running (expect FAIL),
then removing it. **Do that, and record the failure output in your commit message** — an
assertion you never watched fail is not a guard.

- [ ] **Step 3: Commit**

```bash
git add testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "test(backtest): assert no live account class can reach the equity cap"
```

---

## Task 9: End-to-end

The spec makes this mandatory, not optional. Unit tests on the two conversions are necessary and
not sufficient: the feature is a number flowing through sizer, buying-power gate, margin check,
rails, equity recorder and metric builder, and every defect of this shape in this codebase has
been a seam that each side tested correctly in isolation.

**Files:**
- Create: `testplatform/backend/tests/backtest/test_equity_cap_e2e.py`
- Extend: `testplatform/backend/tests/backtest/test_engine_golden_regression.py`

Read `testplatform/backend/tests/backtest/test_daily_engine_e2e.py` first and follow its harness
exactly — do not build a new way to run the engine.

- [ ] **Step 1: The no-regression golden run**

```python
def test_a_run_with_no_cap_is_byte_identical_to_before():
    """Off by default must mean NOTHING moved -- proven by comparing full results, not by the
    suite staying green. A green suite proves nothing broke that a test already covered."""
    baseline = run_golden_backtest(equity_cap=None)
    assert baseline == GOLDEN_EXPECTED      # the existing golden fixture, unchanged
```

- [ ] **Step 2: Deployed equity never exceeds the cap, sampled every bar**

```python
def test_deployed_equity_never_exceeds_the_cap_across_a_whole_run():
    seen = []
    account = run_engine_capturing_account(equity_cap=20_000.0,
                                           on_bar=lambda a: seen.append(a.deployed_equity()))
    assert seen, "the run produced no bars"
    assert max(seen) <= 20_000.0 + 1e-9, f"deployed {max(seen):,.2f} exceeded the cap"
    assert account.equity() > 20_000.0, \
        "this run never grew past the cap, so it does not test anything"
```

The second assertion matters: without it the test passes trivially on a losing strategy.

- [ ] **Step 3: THE feature test — start date must not matter**

```python
def test_the_same_strategy_scores_the_same_started_in_year_three_as_in_year_one():
    """This IS the feature. It can only be shown end to end."""
    from_2020 = run_engine(start="2020-01-01", end="2024-01-01", equity_cap=20_000.0)
    from_2022 = run_engine(start="2022-01-01", end="2024-01-01", equity_cap=20_000.0)
    # Compare the OVERLAPPING window's annualised return, not the whole run.
    assert from_2020["annualized_return_2022_onward"] == \
        pytest.approx(from_2022["annualized_return"], abs=0.5)
```

You will need to derive `annualized_return_2022_onward` from the returned equity curve — write
that helper in the test file. If the harness cannot express it, say so in your report rather than
weakening the assertion; this is the test the feature exists for.

- [ ] **Step 4: The cap reached the SIZER, not just the metrics**

```python
def test_a_capped_run_opens_visibly_smaller_positions_than_an_uncapped_one():
    uncapped = run_engine(equity_cap=None, capture_orders=True)
    capped = run_engine(equity_cap=5_000.0, capture_orders=True)
    assert capped["orders"], "the capped run placed no orders at all"
    assert max(o["notional"] for o in capped["orders"]) < \
           max(o["notional"] for o in uncapped["orders"])
```

- [ ] **Step 5: The cap reached buying power and margin, not just the risk manager**

```python
def test_the_buying_power_gate_refuses_what_the_real_balance_would_have_allowed():
    """If the cap stopped at the risk manager, a margin account could deploy 2x it."""
    acct = build_engine_account(cash=40_000.0, equity_cap=20_000.0)
    assert acct.get_account_info()["buying_power"] == 20_000.0
    assert acct.check_option_buying_power(30_000.0) is False
```

- [ ] **Step 6: The GA path**

```python
def test_a_ga_run_completes_with_the_cap_and_scores_every_individual_the_same_way():
    result = run_small_ga(equity_cap=20_000.0, generations=2, population=4)
    assert result["completed"]
    assert all(t["account_settings"]["equity_cap"] == 20_000.0 for t in result["trials"])
    assert not any("equity_cap" in g for g in result["gene_space"])
```

- [ ] **Step 7: Run the whole backend suite**

Expected: previous counts plus your new tests, only the known Windows-only failure.

- [ ] **Step 8: Commit**

```bash
git add testplatform/backend/tests/backtest/test_equity_cap_e2e.py \
        testplatform/backend/tests/backtest/test_engine_golden_regression.py
git commit -m "test(backtest): end-to-end equity cap, incl. start-date invariance"
```

---

## Task 10: Mutation-test the whole feature

Every prescribed mutation list on this project has proven to be a floor; recent agents ran 108,
191 and 258 mutations and each still found real defects past their brief. Treat this as a
starting point.

- [ ] **Step 1: Set the harness up correctly**

`PYTHONDONTWRITEBYTECODE=1` and purge `__pycache__` before every run. CPython validates a cached
`.pyc` on source mtime in whole **seconds** plus size, so a same-size mutation written in the same
wall second as a restore silently runs stale bytecode and reports a **FALSE survivor**. This has
already cost one agent on this project 23 phantom survivors. Verify each restore with
`git hash-object` and make sure the harness's restore can always finish — a command timeout killed
one mid-run and left a mutated line behind.

- [ ] **Step 2: Run at minimum these, each must fail a NAMED test**

| # | mutation | why it matters |
|---|---|---|
| 1 | `deployed_equity` returns `max` instead of `min` | the cap stops binding entirely |
| 2 | `deployed_equity(None)` returns `0.0` | unknown reads as a wiped-out account |
| 3 | the cap reaches `snapshot_equity` | the recorded history becomes unreconstructable |
| 4 | `scoring_curve` divides by the running equity, not the cap | the declining-CAGR bug the feature exists to remove |
| 5 | `scoring_curve` differences the CAPPED series | zero P&L for every period above the cap |
| 6 | `capped_drawdown_curve` runs on the synthetic curve | risk on a moving denominator |
| 7 | the two lines in Task 5 Step 3 are swapped | same as 6, and silently plausible |
| 8 | `get_balance` returns the cap rather than `min(cash, deployed)` | invents cash the account does not hold |
| 9 | `validate_equity_cap` accepts `0` | every position unaffordable, silently |
| 10 | a cap above initial capital raises instead of logging | refuses a legitimate config |
| 11 | `equity_cap` becomes a gene | every individual scored on a different denominator |
| 12 | the cap defaults to a number instead of `None` | the feature is on by default |
| 13 | `buying_power` uncapped while equity is capped | a margin account deploys 2x |

- [ ] **Step 3: Report survivors and the tests you added to kill them**

- [ ] **Step 4: Commit**

```bash
git add testplatform/backend/tests/backtest/test_equity_cap.py
git commit -m "test(backtest): close the gaps the equity-cap mutation run found"
```

---

## Task 11: Version bump

**Files:**
- Modify: `testplatform/version.py`

- [ ] **Step 1: Bump `TEST_APP_VERSION` by one**

This change is confined to `testplatform/`, so only `TEST_APP_VERSION` moves. Per `CLAUDE.md` the
distributed GA workers decide whether to self-update by comparing that string alone — shipping
without bumping leaves workers running different code from the master, which silently breaks trial
reproducibility.

- [ ] **Step 2: Commit**

```bash
git add testplatform/version.py
git commit -m "chore: bump TEST_APP_VERSION for the backtest equity cap"
```

---

## Self-review notes

Checked against the spec:

- Two quantities separated — Tasks 2, 3, 5; the leak is mutation 5.
- Deployed includes unrealised — Task 2 (`equity()` is cash + MTM), Task 4.
- 25%-every-year — Task 3 Step 1, first test.
- Cap-denominated drawdown — Task 3, Task 5.
- One seam — Task 4; mutation 13 proves buying power inherits it.
- `snapshot_equity` untouched — Task 4 Step 1 last test, mutation 3.
- Run-level not a gene — Task 7, mutation 11.
- Live safety — Task 8.
- No regression — Task 9 Step 1, mutation 12.
- Edge cases (recovery, unaffordable, single period, flat period) — Tasks 2 and 3.
- E2E mandatory, three named harnesses — Task 9.

**Known gap, deliberately left to the implementer:** Tasks 5, 6, 7 and 9 contain helper names
(`_fake_account_with_snapshots`, `_minimal_payload`, `_ns`, `_build_optimization_config`,
`run_engine`) that must be replaced with the real ones found by reading the existing tests and
launcher. Each is flagged in place. Inventing signatures for code I have not read would be worse
than saying so.
