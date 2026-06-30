# Options Strategy Coverage + Options Grid — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the options experts run all tastytrade structures except calendar
(short straddle/strangle, iron condor, jade lizard, butterfly, ratio spread + the
already-supported long call / covered call / vertical / stock), via a clean
entry-option path, then validate with an optimization job and ship an FMPRating ×
options grid script.

**Architecture:** New explicit `_OptionEntryAction` subclasses (one per strategy)
reusing the existing 2–4-leg `submit_option_order` + per-leg `ratio_qty`; a new
entry-option path so the enter_market ruleset fires an option action directly (no
equity leg); percent-OTM + DTE + wing-width strike selection (no greeks). Per-bar
fills reuse the existing in-memory order cache (no new DB churn).

**Tech Stack:** Python 3.12, SQLModel, pytest. Packages: `ba2_common`
(`packages/common`), backend (`testplatform/backend`). Test venv:
`~/ba2-venvs/test/bin/python`. CLI: `ba2-test`.

## Environment / commands

- Run ba2_common tests: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/ -q`
- Run backtest tests: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/backtest/ -q`
- All paths below are relative to `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform`.
- **Edit under `packages/`** (the editable source), NOT old sibling dirs.
- Commit after each task. Do NOT push (version bump + coordination needed; user pushes).

## File map

| File | Change |
|---|---|
| `packages/common/ba2_common/core/types.py` | +6 enum values; extend `get_option_action_values()` |
| `packages/common/ba2_common/core/option_selector.py` | +`select_wing()` |
| `packages/common/ba2_common/core/TradeActions.py` | +`wing_width_pct` ctor param; +6 action classes; register in `create_action` |
| `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py` | extend `option_reserve_required` |
| `packages/common/ba2_common/core/rule_builders.py` | +`wing_width_pct` in `_OPTION_ACTION_PARAM_KEYS` |
| `packages/common/tests/test_new_option_actions.py` | NEW unit tests |
| `testplatform/backend/app/services/backtest/default_rulesets.py` | entry-option seeding |
| `testplatform/backend/app/services/backtest/daily_engine.py` | option-entry direct submit |
| `testplatform/backend/app/services/backtest/daily_backtest_handler.py` | `strategy_uses_options` entry detection; thread `entry_action` |
| `testplatform/backend/app/services/strategy_param_space.py` | +`option_wing_width` gene |
| `testplatform/backend/app/api/strategies.py` | +`option_wing_width*` fields |
| `testplatform/ba2test_launcher.py` | option strategy builders + thread `entry_action` |
| `testplatform/scripts/run_options_grid.sh` | NEW grid script |
| `testplatform/backend/tests/backtest/test_option_entry_path.py` | NEW e2e |

---

## Phase A — New option action classes (`ba2_common`)

### Task A1: `select_wing` strike-selection helper

**Files:**
- Modify: `packages/common/ba2_common/core/option_selector.py`
- Test: `packages/common/tests/test_option_selector_wing.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# packages/common/tests/test_option_selector_wing.py
from datetime import date
from ba2_common.core.option_selector import select_wing
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight


def _c(strike, otype=OptionRight.CALL, oi=500, bid=1.0, ask=1.1):
    return OptionContract(
        symbol=f"X{int(strike)}{'C' if otype==OptionRight.CALL else 'P'}",
        underlying="X", option_type=otype, strike=float(strike),
        expiry=date(2024, 6, 21), bid=bid, ask=ask, last=bid, open_interest=oi,
        delta=None, implied_volatility=None)


def test_select_wing_call_picks_higher_strike():
    chain = [_c(s) for s in (100, 105, 110, 115, 120)]
    # center 100, +10% wing => target 110, nearest is 110
    w = select_wing(chain, center_strike=100.0, width_pct=10.0,
                    option_type=OptionRight.CALL, dte_min=None, dte_max=None,
                    today=date(2024, 6, 1))
    assert w is not None and w.strike == 110.0


def test_select_wing_put_picks_lower_strike():
    chain = [_c(s, OptionRight.PUT) for s in (80, 85, 90, 95, 100)]
    # center 100, 10% wing on PUT => target 90
    w = select_wing(chain, center_strike=100.0, width_pct=10.0,
                    option_type=OptionRight.PUT, dte_min=None, dte_max=None,
                    today=date(2024, 6, 1))
    assert w is not None and w.strike == 90.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_option_selector_wing.py -q`
Expected: FAIL with `ImportError: cannot import name 'select_wing'`

- [ ] **Step 3: Implement `select_wing`**

Append to `packages/common/ba2_common/core/option_selector.py`:

```python
def select_wing(chain, *, center_strike, width_pct, option_type,
                dte_min, dte_max, today, expiry=None,
                min_open_interest=None, max_spread_pct=None):
    """Pick the wing contract nearest ``center_strike`` moved ``width_pct`` percent
    farther OTM (calls: up; puts: down). When ``expiry`` is given, restrict to that
    expiry (wings must share the short leg's expiry)."""
    cands = _candidates(chain, option_type, dte_min, dte_max, today,
                        min_open_interest, max_spread_pct)
    if expiry is not None:
        cands = [c for c in cands if c.expiry == expiry]
    if not cands:
        return None
    if option_type == OptionRight.CALL:
        target = center_strike * (1 + width_pct / 100.0)
    else:
        target = center_strike * (1 - width_pct / 100.0)
    return min(cands, key=lambda c: (abs(c.strike - target), c.strike))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_option_selector_wing.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_selector.py packages/common/tests/test_option_selector_wing.py
git commit -m "feat(options): select_wing strike helper for multi-leg wings"
```

---

### Task A2: Enum values + `wing_width_pct` ctor param

**Files:**
- Modify: `packages/common/ba2_common/core/types.py` (enum ~382-403; `get_option_action_values` ~514)
- Modify: `packages/common/ba2_common/core/TradeActions.py` (`_OptionEntryAction.__init__` ~1516)
- Test: `packages/common/tests/test_new_option_action_enums.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# packages/common/tests/test_new_option_action_enums.py
from ba2_common.core.types import ExpertActionType, get_option_action_values, is_option_action

NEW = ["open_short_straddle", "open_short_strangle", "open_iron_condor",
       "open_jade_lizard", "open_call_butterfly", "open_put_ratio_spread"]


def test_new_enum_members_exist_and_detected():
    for v in NEW:
        assert ExpertActionType(v).value == v
        assert v in get_option_action_values()
        assert is_option_action(v)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_action_enums.py -q`
Expected: FAIL with `ValueError: 'open_short_straddle' is not a valid ExpertActionType`

- [ ] **Step 3: Add enum members + register**

In `types.py`, in `class ExpertActionType`, after `OPEN_STRANGLE = "open_strangle"`:

```python
    OPEN_SHORT_STRADDLE = "open_short_straddle"
    OPEN_SHORT_STRANGLE = "open_short_strangle"
    OPEN_IRON_CONDOR = "open_iron_condor"
    OPEN_JADE_LIZARD = "open_jade_lizard"
    OPEN_CALL_BUTTERFLY = "open_call_butterfly"
    OPEN_PUT_RATIO_SPREAD = "open_put_ratio_spread"
```

In `get_option_action_values()`, add before `CLOSE_OPTION` (keep CLOSE_OPTION last):

```python
        ExpertActionType.OPEN_SHORT_STRADDLE.value,
        ExpertActionType.OPEN_SHORT_STRANGLE.value,
        ExpertActionType.OPEN_IRON_CONDOR.value,
        ExpertActionType.OPEN_JADE_LIZARD.value,
        ExpertActionType.OPEN_CALL_BUTTERFLY.value,
        ExpertActionType.OPEN_PUT_RATIO_SPREAD.value,
```

- [ ] **Step 4: Add `wing_width_pct` to `_OptionEntryAction.__init__`**

In `TradeActions.py`, add a kwarg + store it. After the `max_spread_pct` param in the signature add `wing_width_pct: Optional[float] = None,` and after `self.max_spread_pct = max_spread_pct` add:

```python
        self.wing_width_pct = wing_width_pct
```

- [ ] **Step 5: Run test + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_action_enums.py -q`
Expected: PASS

```bash
git add packages/common/ba2_common/core/types.py packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_new_option_action_enums.py
git commit -m "feat(options): add 6 new option action enum values + wing_width_pct param"
```

---

### Task A3: Short straddle + short strangle actions

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py` (add classes after `OpenStrangleAction`, ~line 2258)
- Test: `packages/common/tests/test_new_option_actions.py` (create)

These reuse `_size_by_reserve` (added here) since credit premium is negative.

- [ ] **Step 1: Write the failing test**

```python
# packages/common/tests/test_new_option_actions.py
from datetime import date
from types import SimpleNamespace
import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


class FakeAccount(OptionsAccountInterface):
    """Minimal options account capturing submit_option_order calls."""
    def __init__(self, spot=100.0):
        self.id = 1
        self._spot = spot
        self.submitted = []
    # capability + clock
    def _as_of_date(self):
        return date(2024, 6, 1)
    def get_balance(self):
        return 100_000.0
    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot
    def get_current_price(self, symbol=None):
        return self._spot
    # chain: strikes around spot for both rights, 30 DTE
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type==OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=2.0, ask=2.2, last=2.1,
                open_interest=1000, delta=None, implied_volatility=None))
        return out
    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy))
        return order
    # unused abstract bits
    def get_option_quote(self, contract_symbol):
        return None
    def get_atm_implied_volatility(self, underlying):
        return 0.3
    def get_option_positions(self):
        return []
    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None
    def check_option_buying_power(self, required):
        return True
    def available_option_buying_power(self):
        return 100_000.0


def _mk(action_type, **kw):
    acct = FakeAccount()
    rec = SimpleNamespace(id=1, instance_id=None)
    act = create_action(ExpertActionType(action_type), "AAPL", acct,
                        SimpleNamespace(), None, rec, **kw)
    act.submit_to_broker = True
    return acct, act


def test_short_strangle_two_short_legs_credit():
    acct, act = _mk("open_short_strangle", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_strangle"
    assert len(sub["legs"]) == 2
    assert all(l.side == OrderDirection.SELL for l in sub["legs"])
    assert sub["limit_price"] < 0   # credit
    assert sub["quantity"] >= 1


def test_short_straddle_same_strike_both_short():
    acct, act = _mk("open_short_straddle", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=20.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_straddle"
    legs = sub["legs"]
    assert len(legs) == 2 and legs[0].strike == legs[1].strike
    assert all(l.side == OrderDirection.SELL for l in legs)
    assert sub["limit_price"] < 0
```

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: FAIL (`ValueError: Unknown action type` — not yet registered)

- [ ] **Step 3: Add a reserve-sizing helper on `_OptionEntryAction`**

In `TradeActions.py`, inside `_OptionEntryAction`, after `_size`:

```python
    def _size_by_reserve(self, reserve_per_contract: float,
                         sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / reserve_per_contract). For credit/naked
        structures where net premium is negative (can't size off premium)."""
        if not reserve_per_contract or reserve_per_contract <= 0:
            return 0
        if not sizing_pct or sizing_pct <= 0:
            return 0
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return 0
        return int(math.floor((equity * (sizing_pct / 100.0)) / reserve_per_contract))
```

- [ ] **Step 4: Add the two action classes**

After `OpenStrangleAction` (before `build_closing_legs`, ~line 2259):

```python
class OpenShortStraddleAction(_OptionEntryAction):
    """Short straddle: SELL an ATM call AND an ATM put at the SAME strike (credit).

    Short-volatility: collect both premiums (sold at BID). Net premium is a CREDIT
    (limit price negative). Naked on both sides; reserve a conservative strike*100
    per contract proxy and size off it."""

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRADDLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if call_c is None:
            return self._result(False, f"No liquid ATM call for short straddle on {self.instrument_name}")
        put_candidates = [c for c in put_chain
                          if c.strike == call_c.strike and c.expiry == call_c.expiry]
        put_c = select_single(
            put_candidates, method="percent_otm", strike_param=0, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if put_c is None:
            return self._result(False, f"No liquid ATM put for short straddle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short straddle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)        # sell both at BID
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short straddle")
        per_contract_reserve = call_c.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short straddle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_straddle", quantity, strike=call_c.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short straddle on {self.instrument_name}")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.SELL,
                             position_intent="sell_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.SELL,
                            position_intent="sell_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, -net_credit,
                                         "short_straddle", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open short straddle on {self.instrument_name}"


class OpenShortStrangleAction(_OptionEntryAction):
    """Short strangle: SELL an OTM call AND an OTM put at DIFFERENT strikes (credit).

    Both legs OTM by ``strike_param`` percent (default 10%), sold at BID. Net credit
    (limit negative). Naked both sides; reserve strike*100 of the SHORT PUT per
    contract proxy and size off it."""

    DEFAULT_OTM_PCT = 10.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_SHORT_STRANGLE.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        call_c = select_single(
            call_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        put_c = select_single(
            put_chain, method="percent_otm", strike_param=otm, spot=spot,
            option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), min_open_interest=self.min_open_interest,
            max_spread_pct=self.max_spread_pct)
        if call_c is None or put_c is None:
            return self._result(False, f"No liquid OTM legs for short strangle on {self.instrument_name}")
        # Pin both legs to the same expiry (use the call's expiry).
        if put_c.expiry != call_c.expiry:
            put_c = select_single(
                [c for c in put_chain if c.expiry == call_c.expiry],
                method="percent_otm", strike_param=otm, spot=spot,
                option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                today=self._today(), min_open_interest=self.min_open_interest,
                max_spread_pct=self.max_spread_pct)
            if put_c is None:
                return self._result(False, f"No same-expiry OTM put for short strangle on {self.instrument_name}")
        if call_c.bid is None or put_c.bid is None:
            return self._result(False, f"Missing bid for short strangle legs on {self.instrument_name}")
        net_credit = round(call_c.bid + put_c.bid, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} short strangle")
        per_contract_reserve = put_c.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size short strangle for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "short_strangle", quantity, strike=put_c.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for short strangle on {self.instrument_name}")
        call_leg = OptionLeg(contract_symbol=call_c.symbol, side=OrderDirection.SELL,
                             position_intent="sell_to_open", option_type=OptionRight.CALL,
                             strike=call_c.strike, expiry=call_c.expiry, underlying=call_c.underlying)
        put_leg = OptionLeg(contract_symbol=put_c.symbol, side=OrderDirection.SELL,
                            position_intent="sell_to_open", option_type=OptionRight.PUT,
                            strike=put_c.strike, expiry=put_c.expiry, underlying=put_c.underlying)
        return self._submit_option_order([call_leg, put_leg], quantity, -net_credit,
                                         "short_strangle", option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open short strangle on {self.instrument_name}"
```

- [ ] **Step 5: Register in `create_action` + extend reserve (do here so tests pass)**

In `create_action` `action_map` add:

```python
        ExpertActionType.OPEN_SHORT_STRADDLE: OpenShortStraddleAction,
        ExpertActionType.OPEN_SHORT_STRANGLE: OpenShortStrangleAction,
```

In `OptionsAccountInterface.option_reserve_required`, before `return 0.0`, add:

```python
        if strategy in ("short_straddle", "short_strangle", "naked_put", "put_ratio_spread"):
            if strike is None:
                return 0.0
            return strike * 100.0 * quantity
        if strategy in ("iron_condor", "jade_lizard", "call_butterfly", "debit_spread"):
            if spread_width is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            return max(0.0, (spread_width - credit)) * 100.0 * quantity
```

- [ ] **Step 6: Run test to verify pass**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py packages/common/tests/test_new_option_actions.py
git commit -m "feat(options): short straddle + short strangle actions (credit, reserve-sized)"
```

---

### Task A4: Iron condor action

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py`
- Test: append to `packages/common/tests/test_new_option_actions.py`

- [ ] **Step 1: Add failing test**

```python
def test_iron_condor_four_legs_credit_defined_risk():
    acct, act = _mk("open_iron_condor", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "iron_condor"
    legs = sub["legs"]
    assert len(legs) == 4
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(sells) == 2 and len(buys) == 2
    assert sub["limit_price"] < 0  # net credit
```

(`wing_width_pct` is passed via `create_action(**kw)` → `_OptionEntryAction.__init__`.)

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py::test_iron_condor_four_legs_credit_defined_risk -q`
Expected: FAIL (`Unknown action type`)

- [ ] **Step 3: Add the class (after OpenShortStrangleAction)**

```python
class OpenIronCondorAction(_OptionEntryAction):
    """Iron condor (4 legs, credit, defined risk): SELL OTM put + BUY farther-OTM put
    + SELL OTM call + BUY farther-OTM call. Short legs at ``strike_param`` %OTM; wings
    ``wing_width_pct`` farther OTM. Credit = short bids - long asks (limit negative).
    Max loss = (wing width - credit); reserved per contract."""

    DEFAULT_OTM_PCT = 10.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_IRON_CONDOR.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        sc = select_single(call_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), min_open_interest=self.min_open_interest,
                           max_spread_pct=self.max_spread_pct)
        sp = select_single(put_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), min_open_interest=self.min_open_interest,
                           max_spread_pct=self.max_spread_pct)
        if sc is None or sp is None:
            return self._result(False, f"No liquid short legs for iron condor on {self.instrument_name}")
        # Wings farther OTM, same expiry as the matching short leg.
        lc = select_wing(call_chain, center_strike=sc.strike, width_pct=wing,
                         option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sc.expiry,
                         min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        lp = select_wing(put_chain, center_strike=sp.strike, width_pct=wing,
                         option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sp.expiry,
                         min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if lc is None or lp is None or lc.strike <= sc.strike or lp.strike >= sp.strike:
            return self._result(False, f"No valid wings for iron condor on {self.instrument_name}")
        if None in (sc.bid, sp.bid, lc.ask, lp.ask):
            return self._result(False, f"Missing quotes for iron condor on {self.instrument_name}")
        net_credit = round(sc.bid + sp.bid - lc.ask - lp.ask, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} iron condor")
        width = max(lc.strike - sc.strike, sp.strike - lp.strike)
        max_loss = max(0.0, width - net_credit)
        per_contract_reserve = max_loss * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing) if per_contract_reserve > 0 else 0
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size iron condor for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "iron_condor", quantity, spread_width=width, net_credit=net_credit)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for iron condor on {self.instrument_name}")
        legs = [
            OptionLeg(contract_symbol=sp.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.PUT, strike=sp.strike, expiry=sp.expiry, underlying=sp.underlying),
            OptionLeg(contract_symbol=lp.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.PUT, strike=lp.strike, expiry=lp.expiry, underlying=lp.underlying),
            OptionLeg(contract_symbol=sc.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.CALL, strike=sc.strike, expiry=sc.expiry, underlying=sc.underlying),
            OptionLeg(contract_symbol=lc.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.CALL, strike=lc.strike, expiry=lc.expiry, underlying=lc.underlying),
        ]
        return self._submit_option_order(legs, quantity, -net_credit, "iron_condor",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open iron condor on {self.instrument_name}"
```

Register in `create_action`: `ExpertActionType.OPEN_IRON_CONDOR: OpenIronCondorAction,`

- [ ] **Step 4: Run test + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: PASS

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_new_option_actions.py
git commit -m "feat(options): iron condor action (4-leg credit, defined risk)"
```

---

### Task A5: Jade lizard action

**Files:** Modify `TradeActions.py`; test appended.

- [ ] **Step 1: Add failing test**

```python
def test_jade_lizard_three_legs_credit():
    acct, act = _mk("open_jade_lizard", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "jade_lizard"
    legs = sub["legs"]
    assert len(legs) == 3
    assert sum(1 for l in legs if l.side == OrderDirection.SELL) == 2
    assert sum(1 for l in legs if l.side == OrderDirection.BUY) == 1
    assert sub["limit_price"] < 0
```

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py::test_jade_lizard_three_legs_credit -q`
Expected: FAIL

- [ ] **Step 3: Add the class**

```python
class OpenJadeLizardAction(_OptionEntryAction):
    """Jade lizard (3 legs, credit): SELL OTM put + SELL OTM call + BUY farther-OTM
    call (caps call-side risk). Short legs at ``strike_param`` %OTM; call wing
    ``wing_width_pct`` farther OTM. Put side remains naked (reserve strike*100).
    Credit = sp.bid + sc.bid - lc.ask (limit negative)."""

    DEFAULT_OTM_PCT = 10.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_JADE_LIZARD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        call_chain = self._chain(OptionRight.CALL)
        put_chain = self._chain(OptionRight.PUT)
        if not call_chain or not put_chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        sc = select_single(call_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), min_open_interest=self.min_open_interest,
                           max_spread_pct=self.max_spread_pct)
        sp = select_single(put_chain, method="percent_otm", strike_param=otm, spot=spot,
                           option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                           today=self._today(), min_open_interest=self.min_open_interest,
                           max_spread_pct=self.max_spread_pct)
        if sc is None or sp is None:
            return self._result(False, f"No liquid short legs for jade lizard on {self.instrument_name}")
        lc = select_wing(call_chain, center_strike=sc.strike, width_pct=wing,
                         option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                         today=self._today(), expiry=sc.expiry,
                         min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if lc is None or lc.strike <= sc.strike:
            return self._result(False, f"No valid call wing for jade lizard on {self.instrument_name}")
        if None in (sc.bid, sp.bid, lc.ask):
            return self._result(False, f"Missing quotes for jade lizard on {self.instrument_name}")
        net_credit = round(sc.bid + sp.bid - lc.ask, 4)
        if net_credit <= 0:
            return self._result(False, f"Non-positive credit for {self.instrument_name} jade lizard")
        per_contract_reserve = sp.strike * 100.0       # put side naked
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size jade lizard for {self.instrument_name}")
        reserve = self.account.option_reserve_required("naked_put", quantity, strike=sp.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for jade lizard on {self.instrument_name}")
        legs = [
            OptionLeg(contract_symbol=sp.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.PUT, strike=sp.strike, expiry=sp.expiry, underlying=sp.underlying),
            OptionLeg(contract_symbol=sc.symbol, side=OrderDirection.SELL, position_intent="sell_to_open",
                      option_type=OptionRight.CALL, strike=sc.strike, expiry=sc.expiry, underlying=sc.underlying),
            OptionLeg(contract_symbol=lc.symbol, side=OrderDirection.BUY, position_intent="buy_to_open",
                      option_type=OptionRight.CALL, strike=lc.strike, expiry=lc.expiry, underlying=lc.underlying),
        ]
        return self._submit_option_order(legs, quantity, -net_credit, "jade_lizard",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open jade lizard on {self.instrument_name}"
```

Register: `ExpertActionType.OPEN_JADE_LIZARD: OpenJadeLizardAction,`

- [ ] **Step 4: Run test + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: PASS

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_new_option_actions.py
git commit -m "feat(options): jade lizard action (3-leg credit)"
```

---

### Task A6: Call butterfly action (1-2-1 debit)

**Files:** Modify `TradeActions.py`; test appended.

- [ ] **Step 1: Add failing test**

```python
def test_call_butterfly_three_strikes_ratio_121_debit():
    acct, act = _mk("open_call_butterfly", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=10.0,
                    wing_width_pct=10.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "call_butterfly"
    legs = sub["legs"]
    assert len(legs) == 3
    body = [l for l in legs if l.side == OrderDirection.SELL]
    wings = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(body) == 1 and body[0].ratio_qty == 2
    assert len(wings) == 2 and all(w.ratio_qty == 1 for w in wings)
    assert sub["limit_price"] > 0  # net debit
```

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py::test_call_butterfly_three_strikes_ratio_121_debit -q`
Expected: FAIL

- [ ] **Step 3: Add the class**

```python
class OpenCallButterflyAction(_OptionEntryAction):
    """Long call butterfly (debit, 1-2-1): BUY 1 lower call + SELL 2 body calls +
    BUY 1 upper call. Body at ``strike_param`` %OTM (~ATM at 0); wings
    ``wing_width_pct`` below/above the body. Net debit = lower.ask + upper.ask
    - 2*body.bid (limit positive). Size off the debit."""

    DEFAULT_BODY_PCT = 0.0
    DEFAULT_WING_PCT = 10.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_CALL_BUTTERFLY.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(OptionRight.CALL)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        body_otm = self.strike_param if self.strike_param is not None else self.DEFAULT_BODY_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        body = select_single(chain, method="percent_otm", strike_param=body_otm, spot=spot,
                             option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                             today=self._today(), min_open_interest=self.min_open_interest,
                             max_spread_pct=self.max_spread_pct)
        if body is None:
            return self._result(False, f"No liquid body call for butterfly on {self.instrument_name}")
        upper = select_wing(chain, center_strike=body.strike, width_pct=wing,
                            option_type=OptionRight.CALL, dte_min=self.dte_min, dte_max=self.dte_max,
                            today=self._today(), expiry=body.expiry,
                            min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        # Lower wing: a call BELOW the body. Reuse select_wing with a PUT-style downward
        # target by searching for strike nearest body*(1 - wing%).
        lower_target = body.strike * (1 - wing / 100.0)
        lower_cands = [c for c in chain if c.expiry == body.expiry and c.strike < body.strike
                       and passes_liquidity(c, self.min_open_interest, self.max_spread_pct)]
        lower = min(lower_cands, key=lambda c: abs(c.strike - lower_target)) if lower_cands else None
        if upper is None or lower is None or upper.strike <= body.strike or lower.strike >= body.strike:
            return self._result(False, f"No valid wings for butterfly on {self.instrument_name}")
        if None in (lower.ask, upper.ask, body.bid):
            return self._result(False, f"Missing quotes for butterfly on {self.instrument_name}")
        net_debit = round(lower.ask + upper.ask - 2 * body.bid, 4)
        if net_debit <= 0:
            return self._result(False, f"Non-positive debit for {self.instrument_name} butterfly")
        quantity = self._size(net_debit, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size butterfly for {self.instrument_name}")
        legs = [
            OptionLeg(contract_symbol=lower.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.CALL,
                      strike=lower.strike, expiry=lower.expiry, underlying=lower.underlying),
            OptionLeg(contract_symbol=body.symbol, side=OrderDirection.SELL, ratio_qty=2,
                      position_intent="sell_to_open", option_type=OptionRight.CALL,
                      strike=body.strike, expiry=body.expiry, underlying=body.underlying),
            OptionLeg(contract_symbol=upper.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.CALL,
                      strike=upper.strike, expiry=upper.expiry, underlying=upper.underlying),
        ]
        return self._submit_option_order(legs, quantity, net_debit, "call_butterfly")

    def get_description(self) -> str:
        return f"Open call butterfly on {self.instrument_name}"
```

Add `passes_liquidity` to the existing `option_selector` import at the top of
`TradeActions.py` (find the `from ba2_common.core.option_selector import ...`
line and add `passes_liquidity` and `select_wing`).

Register: `ExpertActionType.OPEN_CALL_BUTTERFLY: OpenCallButterflyAction,`

- [ ] **Step 4: Run test + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: PASS

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_new_option_actions.py
git commit -m "feat(options): long call butterfly action (1-2-1 debit)"
```

---

### Task A7: Put ratio spread action (1-2 credit)

**Files:** Modify `TradeActions.py`; test appended.

- [ ] **Step 1: Add failing test**

```python
def test_put_ratio_spread_buy1_sell2():
    acct, act = _mk("open_put_ratio_spread", strike_method="percent_otm",
                    strike_param=5.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res.success, res.message
    sub = acct.submitted[0]
    assert sub["strategy"] == "put_ratio_spread"
    legs = sub["legs"]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    assert len(buys) == 1 and buys[0].ratio_qty == 1
    assert len(sells) == 1 and sells[0].ratio_qty == 2
    # buy the higher (less OTM) put, sell the lower (further OTM) put
    assert buys[0].strike > sells[0].strike
```

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py::test_put_ratio_spread_buy1_sell2 -q`
Expected: FAIL

- [ ] **Step 3: Add the class**

```python
class OpenPutRatioSpreadAction(_OptionEntryAction):
    """Put front-ratio spread (1-2): BUY 1 put near ``strike_param`` %OTM + SELL 2
    puts ``wing_width_pct`` farther OTM. Typically a small credit/even with extra
    downside risk below the short strike. limit = long.ask - 2*short.bid (sign per
    result). The naked short put (1 net short) is reserved at short.strike*100."""

    DEFAULT_OTM_PCT = 5.0
    DEFAULT_WING_PCT = 5.0

    def _action_type_value(self) -> str:
        return ExpertActionType.OPEN_PUT_RATIO_SPREAD.value

    def _build_and_submit(self) -> Dict[str, Any]:
        chain = self._chain(OptionRight.PUT)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        spot = self._spot()
        otm = self.strike_param if self.strike_param is not None else self.DEFAULT_OTM_PCT
        wing = self.wing_width_pct if self.wing_width_pct is not None else self.DEFAULT_WING_PCT
        long_p = select_single(chain, method="percent_otm", strike_param=otm, spot=spot,
                               option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                               today=self._today(), min_open_interest=self.min_open_interest,
                               max_spread_pct=self.max_spread_pct)
        if long_p is None:
            return self._result(False, f"No liquid long put for ratio spread on {self.instrument_name}")
        short_p = select_wing(chain, center_strike=long_p.strike, width_pct=wing,
                              option_type=OptionRight.PUT, dte_min=self.dte_min, dte_max=self.dte_max,
                              today=self._today(), expiry=long_p.expiry,
                              min_open_interest=self.min_open_interest, max_spread_pct=self.max_spread_pct)
        if short_p is None or short_p.strike >= long_p.strike:
            return self._result(False, f"No valid short put wing for ratio spread on {self.instrument_name}")
        if long_p.ask is None or short_p.bid is None:
            return self._result(False, f"Missing quotes for ratio spread on {self.instrument_name}")
        net = round(long_p.ask - 2 * short_p.bid, 4)   # usually negative (credit)
        # Reserve the 1 net naked short put.
        per_contract_reserve = short_p.strike * 100.0
        quantity = self._size_by_reserve(per_contract_reserve, self.sizing)
        if quantity < 1:
            return self._result(False, f"Insufficient budget to size ratio spread for {self.instrument_name}")
        reserve = self.account.option_reserve_required(
            "put_ratio_spread", quantity, strike=short_p.strike)
        if not self.account.check_option_buying_power(reserve):
            return self._result(False, f"Insufficient BP for ratio spread on {self.instrument_name}")
        legs = [
            OptionLeg(contract_symbol=long_p.symbol, side=OrderDirection.BUY, ratio_qty=1,
                      position_intent="buy_to_open", option_type=OptionRight.PUT,
                      strike=long_p.strike, expiry=long_p.expiry, underlying=long_p.underlying),
            OptionLeg(contract_symbol=short_p.symbol, side=OrderDirection.SELL, ratio_qty=2,
                      position_intent="sell_to_open", option_type=OptionRight.PUT,
                      strike=short_p.strike, expiry=short_p.expiry, underlying=short_p.underlying),
        ]
        return self._submit_option_order(legs, quantity, net, "put_ratio_spread",
                                         option_reserve=reserve)

    def get_description(self) -> str:
        return f"Open put ratio spread on {self.instrument_name}"
```

Register: `ExpertActionType.OPEN_PUT_RATIO_SPREAD: OpenPutRatioSpreadAction,`

- [ ] **Step 4: Run full action test file + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_new_option_actions.py -q`
Expected: PASS (all)

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_new_option_actions.py
git commit -m "feat(options): put ratio spread action (1-2 credit)"
```

---

### Task A8: rule_builders forwards `wing_width_pct`

**Files:**
- Modify: `packages/common/ba2_common/core/rule_builders.py` (`_OPTION_ACTION_PARAM_KEYS` ~135)
- Modify: `packages/common/ba2_common/core/TradeActionEvaluator.py` (verify it passes `wing_width_pct` to `create_action` — it forwards the whole cfg as kwargs; confirm)
- Test: `packages/common/tests/test_rule_builders_wing.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# packages/common/tests/test_rule_builders_wing.py
from ba2_common.core.rule_builders import action_from_rule


def test_action_from_rule_forwards_wing_width():
    rule = {"action_type": "open_iron_condor", "option_strike_param": 10.0,
            "option_dte_min": 20, "option_dte_max": 40, "option_sizing": 20.0,
            "option_wing_width_pct": 5.0}
    out = action_from_rule(rule)
    cfg = out["act"]
    assert cfg["action_type"] == "open_iron_condor"
    assert cfg["wing_width_pct"] == 5.0
    assert cfg["strike_param"] == 10.0
```

- [ ] **Step 2: Run to verify failure**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_rule_builders_wing.py -q`
Expected: FAIL (`KeyError: 'wing_width_pct'`)

- [ ] **Step 3: Add the param key**

In `rule_builders.py`, in `_OPTION_ACTION_PARAM_KEYS`, add:

```python
    ("wing_width_pct", ("option_wing_width_pct", "option_wing_width")),
```

- [ ] **Step 4: Run + commit**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/test_rule_builders_wing.py -q`
Expected: PASS

```bash
git add packages/common/ba2_common/core/rule_builders.py packages/common/tests/test_rule_builders_wing.py
git commit -m "feat(options): forward option_wing_width_pct through rule_builders"
```

- [ ] **Step 5: Full ba2_common regression**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/ -q`
Expected: PASS (no regressions). If `create_action`/evaluator does NOT forward
`wing_width_pct`, fix by ensuring the option cfg dict is passed as `**kwargs` (it
already is for the other option params) and re-run.

---

## Phase B — Entry-option path (engine)

### Task B1: Entry-option seeding + direct submit + detection

**Files:**
- Modify: `testplatform/backend/app/services/backtest/default_rulesets.py` (`_entry_actions` ~166; `seed_ruleset_from_tree` ~180)
- Modify: `testplatform/backend/app/services/backtest/daily_backtest_handler.py` (`strategy_uses_options` ~60; thread `entry_action` into `_build_experts` config ~683)
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py` (`_run_expert_bar` ~724-742; mark option-entry experts)
- Test: `testplatform/backend/tests/backtest/test_option_entry_path.py` (create)

This is the de-risking task: prove a `buy_call` ENTRY opens an option from flat
(no equity) end-to-end, then the same path serves all pure-option strategies.

- [ ] **Step 1: Write the failing e2e test**

Model it on `tests/backtest/test_options_rule_e2e.py` (reuse its fixture chain
helpers / `seed_enter_long_ruleset` import), but seed the ENTER ruleset with an
option action and assert an option order FILLS with **no equity order**.

```python
# testplatform/backend/tests/backtest/test_option_entry_path.py
"""Entry-option path: an enter_market ruleset whose action is buy_call opens an
OPTION position from flat — no equity leg. Mirrors test_options_rule_e2e but the
option action is the ENTRY, exercising _run_expert_bar's direct-submit branch."""
import pytest
from datetime import date

# Reuse the e2e harness builders from the sibling test module.
from tests.backtest.test_options_rule_e2e import (  # type: ignore
    _build_engine_with_option_entry,  # NEW helper added in this test module below
)


def test_buy_call_entry_opens_option_no_equity():
    result = _build_engine_with_option_entry(action_type="buy_call",
                                             strike_method="percent_otm",
                                             strike_param=2.0, dte_min=20, dte_max=45,
                                             sizing=5.0)
    trades = result["filled_trades"]
    opts = [t for t in trades if getattr(t, "asset_class", None) == "option"
            or str(getattr(t, "asset_class", "")).endswith("OPTION")]
    assert opts, f"expected an option fill from the entry action; got {trades}"
    equities = [t for t in trades if t not in opts]
    assert not equities, f"entry-option path must not open equity; got {equities}"
```

Add `_build_engine_with_option_entry(...)` to **this** test file (not the sibling)
by adapting the sibling's `_build_engine(...)`: seed the enter ruleset via
`seed_ruleset_from_tree(buy_tree=None, entry_action={...})` (the new param), no
`open_positions` ruleset, an always-BUY stub expert, and run the engine over the
fixture option cache. (Copy the sibling's account/cache/expert setup verbatim;
only the ruleset seeding differs.)

- [ ] **Step 2: Run to verify failure**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_option_entry_path.py -q`
Expected: FAIL (`seed_ruleset_from_tree` has no `entry_action` param)

- [ ] **Step 3: Add `entry_action` to the seeder**

In `default_rulesets.py`, change `_entry_actions` to accept an optional option action:

```python
def _entry_actions(side: str, entry_action: dict | None = None) -> dict:
    """The open action for an entry rule. Equity BUY/SELL by default; when
    ``entry_action`` (an option action config from rule_builders.action_from_rule)
    is given, emit THAT as the entry action instead (pure-option entry, no equity)."""
    if entry_action:
        from ba2_common.core.rule_builders import action_from_rule
        built = action_from_rule(entry_action, key=side)
        if built:
            return built
    open_act = ExpertActionType.BUY.value if side == "buy" else ExpertActionType.SELL.value
    return {side: {"action_type": open_act}}
```

Thread `entry_action` through `seed_ruleset_from_tree`:

```python
def seed_ruleset_from_tree(buy_tree, name: str = "backtest-enter-tree",
                           enable_short: bool = False, entry_action: dict | None = None) -> int:
```

and pass it at the two `_entry_actions("buy"|"sell")` call sites:
`actions=_entry_actions("buy", entry_action)` /
`actions=_entry_actions("sell", entry_action)`. When `buy_tree` is None but
`entry_action` is set, fall back to a permissive bullish+flat gate (so the option
fires): if `buy_tree` is None, set `groups = [{}]` (single empty gate).

- [ ] **Step 4: Thread `entry_action` through the handler**

In `daily_backtest_handler.py` `_build_experts`, read it and pass to `_seed_enter`:

```python
    entry_action = config.get("entry_action")
    def _seed_enter(nm: str) -> int:
        if buy_tree or entry_action:
            return seed_ruleset_from_tree(buy_tree, name=nm, enable_short=enable_short,
                                          entry_action=entry_action)
        return (seed_enter_long_short_ruleset(name=nm) if enable_short
                else seed_enter_long_ruleset(name=nm))
```

And extend `strategy_uses_options` to detect entry option actions:

```python
def strategy_uses_options(cfg: Dict[str, Any]) -> bool:
    ea = cfg.get("entry_action")
    if isinstance(ea, dict):
        a = ea.get("option_strategy") or ea.get("action_type") or ea.get("action")
        if a and is_option_action(str(a)):
            return True
    for rule in (cfg.get("exit_rules") or cfg.get("exit_conditions") or []):
        if not isinstance(rule, dict):
            continue
        action = rule.get("option_strategy") or rule.get("action_type") or rule.get("action")
        if action and is_option_action(str(action)):
            return True
    return False
```

- [ ] **Step 5: Direct-submit option entry actions in the engine**

In `daily_engine.py` `_run_expert_bar`, the entry currently executes with
`submit_to_broker=False`. Detect an option entry action and submit directly so the
`_OptionEntryAction` sizes+submits itself. After `action_summaries = evaluator.evaluate(...)`
and the error check, replace the single `execute(submit_to_broker=False)` with:

```python
                # Option entry actions size + submit themselves (like the open-positions
                # path); equity BUY stays a PENDING qty=0 order the RM sizes next.
                from ba2_common.core.types import is_option_action
                opt_entry = any(
                    is_option_action(str((s or {}).get("action_type") or (s or {}).get("action")))
                    for s in action_summaries if isinstance(s, dict))
                results = evaluator.execute(submit_to_broker=bool(opt_entry))
                if any(r.get("success") and (r.get("data") or {}).get("order_id") for r in results):
                    created_any = True
```

If `action_summaries` items don't expose `action_type`, derive `opt_entry` instead
from the ruleset: pass a per-expert boolean computed once in `_build_experts`
(store on the `(expert, id, settings, ruleset_id)` tuple is awkward — simplest:
stash `self._option_entry_expert_ids: set[int]` set by the handler via
`config["entry_action"]` presence, and check `expert_id in self._option_entry_expert_ids`).
Prefer the set approach for robustness:
  - In `__init__`, `self._option_entry = bool(...)` is not per-expert; instead add
    `self._option_entry_ruleset = is_option_action(...)` global flag from
    `config.get("entry_action")`. Since the grid runs ONE strategy per job, a global
    `config["entry_action"]`-derived flag is sufficient:

```python
        self._entry_is_option = False
        ea = config.get("entry_action")
        if isinstance(ea, dict):
            from ba2_common.core.types import is_option_action
            a = ea.get("action_type") or ea.get("action")
            self._entry_is_option = bool(a and is_option_action(str(a)))
```

  then in `_run_expert_bar`: `results = evaluator.execute(submit_to_broker=self._entry_is_option)`.

Use the global-flag approach (one strategy per run) — simpler and unambiguous.

- [ ] **Step 6: Re-entry guard (option position counts as a position)**

Verify `F_HAS_NO_POSITION` is False when an option position is held. Search the
evaluator/TradeConditions for `F_HAS_NO_POSITION` handling; if it only checks
equity positions, include `account.get_option_positions()`. Add an assertion to
the e2e test that the entry fires only once (one option open over the run).

```python
    assert len(opts) == 1, f"entry should fire once while the option is held; got {len(opts)}"
```

- [ ] **Step 7: Run e2e to verify pass**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_option_entry_path.py -q`
Expected: PASS

- [ ] **Step 8: Regression + commit**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/backtest/ -q`
Expected: PASS (no regressions in existing option/equity tests)

```bash
git add testplatform/backend/app/services/backtest/default_rulesets.py testplatform/backend/app/services/backtest/daily_backtest_handler.py testplatform/backend/app/services/backtest/daily_engine.py testplatform/backend/tests/backtest/test_option_entry_path.py
git commit -m "feat(options): entry-option path — enter_market fires option action, no equity leg"
```

---

## Phase C — Optimizer `option_wing_width` gene

### Task C1: Add the wing-width gene (param-space + decode + API)

**Files:**
- Modify: `testplatform/backend/app/services/strategy_param_space.py` (collect ~144-154; decode ~280-285)
- Modify: `testplatform/backend/app/api/strategies.py` (~57-68)
- Test: `testplatform/backend/tests/test_param_space_wing.py` (create — follow existing param-space test style)

- [ ] **Step 1: Write the failing test**

```python
# testplatform/backend/tests/test_param_space_wing.py
from app.services.strategy_param_space import collect_param_space, decode_params


def test_wing_width_gene_collected_and_decoded():
    exit_rule = {"id": "e1", "action_type": "open_iron_condor",
                 "option_wing_width_optimize": True,
                 "option_wing_width_min": 3.0, "option_wing_width_max": 10.0,
                 "option_wing_width_step": 1.0}
    space = collect_param_space(buy_tree=None, exit_rules=[exit_rule],
                                expert_params={}, strategy=None)
    assert "exit:e1:option_wing_width" in space
    decoded = decode_params({"exit:e1:option_wing_width": 5.0}, exit_rules=[exit_rule])
    # decoded exit rule carries the chosen wing width
    er = decoded["exit_rules"][0]
    assert er.get("option_wing_width_pct") == 5.0 or er.get("wing_width_pct") == 5.0
```

Adapt the call signatures to the ACTUAL `collect_param_space` / `decode_params`
signatures in the file (read them first; the test asserts behavior, adjust the
call to match). The two assertions (gene key present; decode sets the wing on the
rule) are the contract.

- [ ] **Step 2: Run to verify failure**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/test_param_space_wing.py -q`
Expected: FAIL

- [ ] **Step 3: Add the gene to collect (mirror the option_dte block ~150-154)**

```python
        if eid and exit_rule.get("option_wing_width_optimize"):
            out[f"exit:{eid}:option_wing_width"] = _range_entry(
                exit_rule.get("option_wing_width_min"),
                exit_rule.get("option_wing_width_max"),
                exit_rule.get("option_wing_width_step"), is_int=False,
            )
```

- [ ] **Step 4: Add decode (mirror option_dte ~285)**

In the decode loop add a branch:

```python
            elif field == "option_wing_width":
                exit_option_wing_by_id[eid] = val
```

declare `exit_option_wing_by_id: Dict[str, Any] = {}` near the other
`exit_option_*_by_id` dicts (~260), and where decoded exit rules are rebuilt set
`rule["option_wing_width_pct"] = exit_option_wing_by_id[eid]` for matching ids
(mirror exactly how `option_dte`/`option_delta` are applied to the rebuilt rule).

- [ ] **Step 5: Add API fields (mirror option_dte_* in strategies.py ~65-68)**

```python
    option_wing_width: Optional[float] = None
    option_wing_width_optimize: bool = False
    option_wing_width_min: Optional[float] = None
    option_wing_width_max: Optional[float] = None
    option_wing_width_step: Optional[float] = None
```

- [ ] **Step 6: Run + regression + commit**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/test_param_space_wing.py tests/ -q -k "param_space or strategies"`
Expected: PASS

```bash
git add testplatform/backend/app/services/strategy_param_space.py testplatform/backend/app/api/strategies.py testplatform/backend/tests/test_param_space_wing.py
git commit -m "feat(options): option_wing_width optimizer gene + API fields"
```

---

## Phase D — Launcher strategy builders + grid script

### Task D1: Option strategy builders + thread `entry_action`

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (`_STRATEGY_BUILDERS` ~1114; the optimize/optimize-batch config build ~1190-1240 and ~1430-1440 — add `entry_action` to the per-trial/run config when the strategy is an option strategy)
- Test: `testplatform/backend/tests/test_option_strategy_builders.py` (create)

Design: an option strategy builder returns a `Strategy` carrying an
`entry_action` dict (the option action config) + an exit ruleset (close at +50%
premium profit + time exit). A module-level dict maps strategy key → option
action config with optimizable ranges. The launcher, when the chosen strategy is
an option strategy, sets `config["entry_action"]` (for pure-option) OR seeds the
covered-call/stock variants conventionally.

- [ ] **Step 1: Write the failing test**

```python
# testplatform/backend/tests/test_option_strategy_builders.py
import importlib.util, sys, os

# load the launcher module by path (it lives at testplatform/ba2test_launcher.py)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_option_strategy_keys_registered():
    for k in ["O_LC", "O_CC", "O_VERT", "O_STK", "O_SSTG", "O_SSTD",
              "O_IC", "O_JL", "O_BF", "O_RS"]:
        assert k in mod._STRATEGY_BUILDERS, f"{k} missing from _STRATEGY_BUILDERS"


def test_short_strangle_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_SSTG")
    assert entry["action_type"] == "open_short_strangle"
    assert "option_strike_param" in entry
```

- [ ] **Step 2: Run to verify failure**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/test_option_strategy_builders.py -q`
Expected: FAIL (`O_LC missing` / `_option_entry_action_for` undefined)

- [ ] **Step 3: Add option strategy definitions to the launcher**

Near `_STRATEGY_BUILDERS` add a config map + helpers. Each entry-action carries
optimizable ranges (strike_param / dte / wing) so the GA searches them:

```python
# Option strategy entry-action configs (pure-option entries). Ranges drive the
# optimizer genes (exit:<id>:option_delta/option_dte/option_wing_width). DTE/%OTM
# windows are tastytrade-ish (30-45 DTE, sell ~10-20% OTM, wings 3-7%).
_OPTION_STRATS = {
    "O_LC": {  # long call
        "action_type": "buy_call", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 8.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_VERT": {  # bear put vertical (debit)
        "action_type": "open_bear_put_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 2.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
        "option_strike_param_max": 6.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 60, "option_dte_step": 5},
    "O_SSTG": {  # short strangle (credit)
        "action_type": "open_short_strangle", "option_strike_method": "percent_otm",
        "option_strike_param": 12.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 6.0,
        "option_strike_param_max": 20.0, "option_strike_param_step": 2.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_SSTD": {  # short straddle (credit)
        "action_type": "open_short_straddle", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0,
        "option_dte_optimize": True, "option_dte_min_range": 20,
        "option_dte_max_range": 50, "option_dte_step": 5},
    "O_IC": {  # iron condor (credit, defined risk)
        "action_type": "open_iron_condor", "option_strike_method": "percent_otm",
        "option_strike_param": 12.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 8.0,
        "option_strike_param_max": 20.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
    "O_JL": {  # jade lizard (credit)
        "action_type": "open_jade_lizard", "option_strike_method": "percent_otm",
        "option_strike_param": 10.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 20.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 6.0,
        "option_strike_param_max": 16.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
    "O_BF": {  # long call butterfly (debit)
        "action_type": "open_call_butterfly", "option_strike_method": "percent_otm",
        "option_strike_param": 0.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 8.0, "option_wing_width_pct": 10.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 5.0,
        "option_wing_width_max": 15.0, "option_wing_width_step": 2.5},
    "O_RS": {  # put ratio spread (credit/even)
        "action_type": "open_put_ratio_spread", "option_strike_method": "percent_otm",
        "option_strike_param": 5.0, "option_dte_min": 25, "option_dte_max": 45,
        "option_sizing": 15.0, "option_wing_width_pct": 5.0,
        "option_strike_param_optimize": True, "option_strike_param_min": 2.0,
        "option_strike_param_max": 10.0, "option_strike_param_step": 2.0,
        "option_wing_width_optimize": True, "option_wing_width_min": 3.0,
        "option_wing_width_max": 8.0, "option_wing_width_step": 1.0},
}


def _option_entry_action_for(kind: str) -> dict:
    return dict(_OPTION_STRATS[kind])


def _option_exit_rules(kind: str):
    """Close the option at +50% premium profit, plus a time exit. (CLOSE on the
    held option position via close_option.)"""
    return [
        {"id": "opt_tp", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 50,
              "optimize": True, "value_min": 25, "value_max": 75, "value_step": 5}]}},
        {"id": "opt_time", "action_type": "close_option", "toggle_optimize": True,
         "conditions": {"type": "AND", "conditions": [
             {"id": "td", "field": "days_opened", "op": ">", "value": 21,
              "optimize": True, "value_min": 10, "value_max": 35, "value_step": 5}]}},
    ]


def _build_strategy_option(kind: str):
    """A pure-option Strategy: entry_action = the option action; exit = close at
    +50% / time. No equity TP/SL brackets."""
    from app.models.strategy import Strategy
    s = Strategy(
        name=kind,
        buy_entry_conditions={"id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5,
             "toggle_optimize": True}]},
        exit_conditions=_option_exit_rules(kind),
        initial_tp_percent=500.0, initial_tp_optimize=False,
        initial_sl_percent=500.0, initial_sl_optimize=False,
    )
    # Carry the entry option action so the handler config picks it up.
    s.entry_action = _option_entry_action_for(kind)  # type: ignore[attr-defined]
    return s


def _build_strategy_covered_call(kind: str):
    """O_CC — equity entry + a sell_covered_call OPEN_POSITIONS overlay rule."""
    from app.models.strategy import Strategy
    s = _build_strategy_S2("O_CC")  # reuse equity entry + base exits
    s.exit_conditions = list(s.exit_conditions) + [{
        "id": "cc_sell", "action_type": "sell_covered_call",
        "option_strike_method": "percent_otm", "option_strike_param": 5.0,
        "option_dte_min": 25, "option_dte_max": 45,
        "conditions": {"type": "AND", "conditions": [{"id": "h", "field": "has_position"}]}}]
    return s


def _build_strategy_stock(kind: str):
    """O_STK — plain equity long (the S2 baseline)."""
    return _build_strategy_S2("O_STK")
```

Register all keys in `_STRATEGY_BUILDERS`:

```python
    "O_LC": _build_strategy_option, "O_VERT": _build_strategy_option,
    "O_SSTG": _build_strategy_option, "O_SSTD": _build_strategy_option,
    "O_IC": _build_strategy_option, "O_JL": _build_strategy_option,
    "O_BF": _build_strategy_option, "O_RS": _build_strategy_option,
    "O_CC": _build_strategy_covered_call, "O_STK": _build_strategy_stock,
```

Update `_build_strategy(kind, name, expert)` dispatch: the option builders take
`(kind)` not `(name)`. Add a branch:

```python
    if kind in ("O_LC", "O_VERT", "O_SSTG", "O_SSTD", "O_IC", "O_JL", "O_BF", "O_RS"):
        return _build_strategy_option(kind)
    if kind == "O_CC":
        return _build_strategy_covered_call(kind)
    if kind == "O_STK":
        return _build_strategy_stock(kind)
```

- [ ] **Step 4: Thread `entry_action` into the run config**

Where the launcher assembles the per-trial / per-run backtest config (the
`optimize` path ~1190-1240 and `optimize-batch` ~1430-1440), after building the
strategy add:

```python
        entry_action = getattr(strat, "entry_action", None)
        if entry_action:
            base_config["entry_action"] = entry_action   # handler -> _build_experts
```

(Use the actual config-dict variable name at each site; grep for where
`buy_tree` / `exit_rules` are placed into the config and add `entry_action`
alongside.)

- [ ] **Step 5: Run builder test + verify**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/test_option_strategy_builders.py -q`
Expected: PASS

- [ ] **Step 6: Add option strategy keys to the CLI choices**

In the `optimize` argparse `--strategy` choices (~1975) and the `optimize-batch`
strategy parsing, allow the `O_*` keys (the batch `--strategies` is comma-split
and dispatched through `_build_strategy`, so just ensure no hardcoded
`choices=["S1".."S4"]` rejects them; widen/remove that restriction for batch).

- [ ] **Step 7: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/test_option_strategy_builders.py
git commit -m "feat(options): launcher option strategy builders (O_LC..O_RS, O_CC, O_STK) + entry_action wiring"
```

---

### Task D2: `run_options_grid.sh`

**Files:**
- Create: `testplatform/scripts/run_options_grid.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Options strategy grid: FMPRating x {the 10 option/equity strategies} over a 10
# mega-cap universe, on a DAILY analysis cadence + 1d fill clock (option cache bars
# are daily). Builds the offline options cache first (ba2-test fetch-options), then
# launches ONE optimize-batch grid. Mirrors run_phase1_grid.sh but for options.
#
# Usage:
#   scripts/run_options_grid.sh                       # most-recent ~3mo window
#   START=2024-03-01 END=2024-06-01 scripts/run_options_grid.sh
#
# Prereqs: venvs installed (ba2-test on PATH), keys in .env, an options-entitled
# Alpaca key for the cache build (account-3 in the live DB), serve backend up.
set -euo pipefail

START="${START:-}"          # empty => auto-detect most-recent ~3mo (set below)
END="${END:-}"
INTERVAL="${INTERVAL:-1d}"
POPULATION="${POPULATION:-12}"
GENERATIONS="${GENERATIONS:-4}"
PARALLEL="${PARALLEL:-4}"
FITNESS="${FITNESS:-total_return}"
EXPERTS="${EXPERTS:-FMPRating}"
STRATEGIES="${STRATEGIES:-O_LC,O_CC,O_VERT,O_STK,O_SSTG,O_SSTD,O_IC,O_JL,O_BF,O_RS}"
UNIVERSE="${UNIVERSE:-AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,AMD,NFLX}"
BA2_TEST="${BA2_TEST:-ba2-test}"
API="${API:-http://localhost:8000}"

# Auto-detect a recent ~3-month window ending ~last week (option daily bars settle
# with a short delay). Override with START/END.
if [[ -z "$END" ]]; then
  END="$(python -c "import datetime; print(datetime.date.today()-datetime.timedelta(days=7))")"
fi
if [[ -z "$START" ]]; then
  START="$(python -c "import datetime,sys; d=datetime.date.fromisoformat('$END'); print(d-datetime.timedelta(days=92))")"
fi

echo ">> options grid: experts=$EXPERTS strategies=$STRATEGIES window=$START..$END interval=$INTERVAL"
echo ">> universe: $(echo "$UNIVERSE" | tr ',' ' ' | wc -w) symbols"

if ! curl -fs --max-time 5 "$API/api/tasks?limit=1" >/dev/null 2>&1; then
  echo "!! serve backend not reachable at $API — start it: $BA2_TEST serve --mode back"; exit 1
fi

# 1. OHLCV cache (daily) for the underlier signals + 210d warmup for FMPRating.
CACHE_START="$(python -c "import datetime; d=datetime.date.fromisoformat('$START'); print(d-datetime.timedelta(days=210))")"
echo ">> pre-caching $INTERVAL OHLCV $CACHE_START..$END"
"$BA2_TEST" fetch-cache --symbols "$UNIVERSE" --timeframes "$INTERVAL" \
  --start "$CACHE_START" --end "$END" --provider fmp --workers 5

# 2. prewarm FMPRating history (ratings/targets) so GA workers read disk.
echo ">> pre-warming FMP history cache"
"$BA2_TEST" prewarm --symbols "$UNIVERSE" --experts "$EXPERTS" --end "$END" --workers 5 || \
  echo "!! prewarm failed (non-fatal); continuing"

# 3. build the offline OPTIONS cache (Alpaca; account-3 options-entitled key).
echo ">> building options cache $START..$END"
"$BA2_TEST" fetch-options --underlyings "$UNIVERSE" --start "$START" --end "$END"

# 4. launch ONE grid (daily cadence).
echo ">> launching optimize-batch options grid"
exec "$BA2_TEST" optimize-batch \
  --experts "$EXPERTS" --strategies "$STRATEGIES" --universe "$UNIVERSE" \
  --start "$START" --end "$END" --fitness "$FITNESS" --interval "$INTERVAL" \
  --run-schedule daily \
  --population "$POPULATION" --generations "$GENERATIONS" --parallel "$PARALLEL"
```

- [ ] **Step 2: Make executable + sanity-check (no run)**

Run: `chmod +x testplatform/scripts/run_options_grid.sh && bash -n testplatform/scripts/run_options_grid.sh && echo OK`
Expected: `OK` (syntax valid)

- [ ] **Step 3: Commit**

```bash
git add testplatform/scripts/run_options_grid.sh
git commit -m "feat(options): run_options_grid.sh — FMPRating x option strategies grid"
```

---

## Phase E — Cache build, validation run, perf check

### Task E1: Build the options cache (real data)

- [ ] **Step 1: Confirm options-entitled key is set**

The options-entitled Alpaca key lives in the live DB `accountsetting account_id=3`
(account "ba2New"). Confirm `ba2-test fetch-options` resolves it (it reads the live
DB read-only). If the dev `.env` key is used and 401s, point fetch-options at
account-3 (see prior `fetch_options` plumbing).

- [ ] **Step 2: Detect the latest available window + build**

Run (auto-window via the grid script's logic, or manually):

```bash
END=$(python -c "import datetime;print(datetime.date.today()-datetime.timedelta(days=7))")
START=$(python -c "import datetime,sys;d=datetime.date.fromisoformat('$END');print(d-datetime.timedelta(days=92))")
ba2-test fetch-options --underlyings AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,AMD,NFLX --start "$START" --end "$END"
```

Expected: a populated `options_cache.sqlite` (chains + per-contract daily bars).
If the recent window is sparse/empty (data-availability), fall back to a known-good
2024 window (e.g. `START=2024-03-01 END=2024-06-01`) and note the change.

- [ ] **Step 3: Verify the cache has chains for all 10 symbols**

```bash
ba2-test fetch-options --underlyings AAPL --start "$START" --end "$END" --dry-run 2>/dev/null || true
python - <<'PY'
import sqlite3, os
db = os.path.expanduser("~/Documents/ba2/common/cache/options/options_history.sqlite")
# adjust to the actual cache path printed by fetch-options
con = sqlite3.connect(db); cur = con.cursor()
for t in ("chains","contracts","bars"):
    try: print(t, cur.execute(f"select count(*) from {t}").fetchone()[0])
    except Exception as e: print(t, "n/a", e)
PY
```

Expected: non-zero chain/contract/bar counts. (No commit — this is data.)

---

### Task E2: Per-strategy validation opt jobs

- [ ] **Step 1: Start the serve backend (if not running)**

Run (user may need to run interactively): `ba2-test serve --mode back`

- [ ] **Step 2: Run each strategy's small GA and record total_return**

For each `K` in `O_LC O_VERT O_SSTG O_SSTD O_IC O_JL O_BF O_RS O_CC O_STK`:

```bash
ba2-test optimize --expert FMPRating --strategy "$K" \
  --universe AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,AMD,NFLX \
  --start "$START" --end "$END" --fitness total_return --interval 1d \
  --run-schedule daily --population 12 --generations 4 --parallel 4
```

(If `optimize` requires a single `--strategy` from a fixed choices list, widen it
in Task D1 Step 6 first.) Record each run's best-individual `total_return`.

- [ ] **Step 3: Assess profitability per strategy**

For each strategy confirm the best individual has `total_return > 0` AND that
option orders actually FILLED (non-zero trades — guard against a "profitable"
zero-trade run). Use `ba2-test report` / the individuals endpoint to read results.

- [ ] **Step 4: Retry / debug / alt-window policy**

For any strategy with `total_return <= 0` OR zero fills:
  1. Re-run 1–2 times (GA variance).
  2. If zero fills: debug (liquidity filters too tight? %OTM strike off the chain?
     DTE window empty for the cache's expiries?). Fix the strategy config / action,
     re-test the unit + e2e tests, re-run.
  3. If still negative after fills look correct: try the alt window
     (`START=2024-03-01 END=2024-06-01`).
  4. If still negative: record it in the results summary for revisit (do NOT force).

- [ ] **Step 5: Write a results summary**

Create `testplatform/reports/options_strategies_validation.md` with a table:
strategy | window | fills | best total_return | status (profitable / negative /
revisit) | notes. Commit it.

```bash
git add testplatform/reports/options_strategies_validation.md
git commit -m "docs(options): per-strategy validation results"
```

---

### Task E3: Perf verification (no per-bar DB churn)

**Files:**
- Test: `testplatform/backend/tests/backtest/test_option_run_perf.py` (create)

- [ ] **Step 1: Write a perf-guard test**

Assert an options entry run does NOT add per-bar DB queries vs. the equity
baseline pattern — e.g. patch/count `get_db`/session usage or assert the
in-memory order cache path is used (reuse the style of any existing perf/no-op
test; search `tests/` for `invalidate_order_cache` / `frozen_ttl_cache` /
`activity_logging_disabled` usage in tests). Minimum viable assertion: run a short
option backtest inside `frozen_ttl_cache()` + `activity_logging_disabled()` and
assert it completes and the account's order-cache `invalidate_order_cache` is
called only on event bars (mock + count), not every bar.

```python
# testplatform/backend/tests/backtest/test_option_run_perf.py
def test_option_run_uses_inmemory_order_cache(monkeypatch):
    # Build the same engine fixture as test_option_entry_path but count
    # account.invalidate_order_cache() calls; assert it is << number of bars
    # (only event bars), proving no per-bar cache reload was introduced.
    ...
```

Flesh out using the `test_option_entry_path` harness; the contract is: invalidate
calls ≈ number of bars with fills/new orders, NOT total bars.

- [ ] **Step 2: Run + commit**

Run: `cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_option_run_perf.py -q`
Expected: PASS

```bash
git add testplatform/backend/tests/backtest/test_option_run_perf.py
git commit -m "test(options): perf guard — option runs reuse in-memory order cache"
```

- [ ] **Step 3: Full regression**

Run: `~/ba2-venvs/test/bin/python -m pytest packages/common/tests/ -q && cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests/ -q`
Expected: PASS (all green)

---

## Self-review notes

- **Spec coverage:** 6 new strategies (A3–A7) ✅; entry-option path (B1) ✅;
  wing gene (C1) ✅; full-set grid incl. already-supported (D1: O_LC/O_CC/O_VERT/
  O_STK + 6 new) ✅; cache+run+grid (D2,E1,E2) ✅; perf constraint (E3 + B1
  direct-submit reuse) ✅; success criterion + retry policy (E2 step 4) ✅;
  calendar OUT OF SCOPE ✅.
- **Known integration risks to handle during execution (not placeholders —
  flagged):** (1) exact `collect_param_space`/`decode_params` signatures (C1 — read
  before writing the test call); (2) where `optimize`/`optimize-batch` assemble the
  config dict to attach `entry_action` (D1 step 4 — grep for `buy_tree`/`exit_rules`
  insertion site); (3) `F_HAS_NO_POSITION` option-awareness (B1 step 6 — verify,
  fix if equity-only); (4) `close_option` as an exit rule firing on a held option
  position (D1 `_option_exit_rules` — the e2e in B1 should be extended to cover an
  exit if not already). Each has a concrete verification step in its task.
