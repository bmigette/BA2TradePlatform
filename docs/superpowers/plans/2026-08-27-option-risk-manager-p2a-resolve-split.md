# Option Risk Manager — Phase 2a (Resolve Split, Premium-Sized Builders) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `_build_and_submit()` into a `_resolve()` that produces a priced structure and a shared tail that sizes and submits it — for the 7 premium-sized builders — with **zero behaviour change**.

**Architecture:** `_resolve()` stays lexically inside each concrete action class (several tests AST-scan those class bodies). It returns a `ResolvedStructure` carrying everything except quantity. A new shared `_size_and_submit()` on `_OptionEntryAction` performs the identical sizing, the identical `quantity < 1` refusal and the identical submit. `execute()` orchestrates the two. No risk manager exists yet; nothing moves out of the action in this phase.

**Tech Stack:** Python 3.11+, stdlib dataclasses, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-option-risk-manager-design.md` §4.2, §4.5.

---

## Why this plan is scoped differently from what the spec's P2 row says

The spec said P2 was "`_build_and_submit` → `_resolve` split across the builders — the RM calls resolve then submits". A 12-agent survey of the actual code (2026-08-27) found four things that make that framing wrong, and this plan is the corrected version. **Read this section before Task 1; it is the difference between a safe refactor and a broken one.**

1. **There are 17 builders, not 16.** Verified: 7 size via `_size`, 8 via `_size_by_reserve`, 2 (covered call, protective put) size off held shares and use neither. This plan covers **only the 7 premium-sized ones**. The other two families get their own plans, because their tails genuinely differ.

2. **`ResolvedStructure` cannot be filled by `_resolve()`.** As shipped in Phase 1 it requires `max_loss_per_contract`, `payoff_at_target` and `score`. `payoff_at_target` needs the recommendation's target and `score` needs the whole bar's context — neither exists inside a single action. Task 1 splits the value object accordingly.

3. **The refactor must not move code out of the concrete classes.** Four test files AST-scan each action class's own source: `test_strike_method_registry.py` (21 tests) looks for `method=self.strike_method` inside each class, `test_option_economics.py` scans each credit builder for the ARC gate, and `test_new_option_actions.py` / `test_option_assignment_capacity_wiring.py` scan for `_submit_option_order` call sites. Moving selection into a shared base or a strategy table breaks them — and they are drift guards for real risk classes, so the right response is to respect the constraint, not to delete the tests.

4. **`self._result(...)` is not pure — it persists a `TradeActionResult` row** via `create_and_save_action_result` (`TradeActions.py:346+`). Every guard currently writes one, and the UI reads them. `_resolve()` must therefore keep returning `self._result(False, msg)` dicts for its refusals rather than a new sentinel type. `StructureRefusal` is for the *risk manager* in Phase 3; it does not appear in this phase at all.

There is a fifth finding that this plan deliberately does **not** act on, recorded so it is not lost: option entry actions are reached from **four** code paths — the `enter_market` ruleset, the open-positions overlay ruleset, the unified `TradeRule` entry path, and the `PremiumSeller` bypass expert (which builds option positions with no `TradeAction` at all). Because this phase changes no behaviour, all four keep working untouched. Phase 3 must state which of them the risk manager governs.

---

## Orientation for the implementer

**Repo:** `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform`. Work on a branch off `dev`.

**The venv is `venv/`, NOT `.venv/`.** Run tests as `./venv/bin/python -m pytest <paths> -q`.

**You cannot run `tests/` and `packages/common/tests/` in one pytest invocation** — both `conftest.py` files import as `tests.conftest` and collide. Run them separately.

**Shared code lives in `packages/common/ba2_common/`.** Files under `ba2_trade_platform/core/` are re-export shims; never put real code there.

**Baselines before you start** (run each and record the number; they must not go down):

```bash
./venv/bin/python -m pytest packages/common/tests/ -q          # 2352 passed
./venv/bin/python -m pytest tests/ -q                          # 4410 passed
```

**The one rule that matters most in this plan:** every step is behaviour-neutral. If a test that passed before your change fails after it, you have made a mistake — do not "fix" the test. The only tests that may legitimately change are the two in `test_held_equity_shares_arithmetic.py` that call `_build_and_submit()` by name (Task 4), and those are a rename, not a semantic change.

**Do not place real orders.** Nothing in this plan touches a broker.

---

## File Structure

| file | responsibility |
|---|---|
| `packages/common/ba2_common/core/option_request.py` | **Modified.** Split `ResolvedStructure` into resolve-time and score-time halves; add `cost_per_contract` and `sizing_basis`; register the refusal phrases the builders actually emit. |
| `packages/common/ba2_common/core/TradeActions.py` | **Modified.** Add `_resolve()`/`_size_and_submit()` to `_OptionEntryAction`; rewrite `execute()`; convert 7 concrete builders. |
| `packages/common/tests/test_option_resolve_split.py` | **New.** Proves the split is behaviour-neutral and that `_resolve()` is independently callable. |

---

### Task 1: Split the value object and register the real refusal phrases

**Files:**
- Modify: `packages/common/ba2_common/core/option_request.py`
- Test: `packages/common/tests/test_option_request.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_option_request.py`:

```python
# --- added for Phase 2a -----------------------------------------------------------------

from ba2_common.core.option_request import (
    EMPTY_CHAIN_REFUSAL, MISSING_QUOTE_REFUSAL, NON_POSITIVE_NET_REFUSAL,
    NO_LIQUID_CONTRACT_REFUSAL, SELECTION_CONFIG_REFUSAL, ResolvedStructure, ScoredStructure)


def test_the_phrases_the_builders_actually_emit_are_registered():
    """Phase 1 registered eight phrases invented from the design doc. A survey of the 17
    builders found five refusal KINDS they really emit that had no phrase at all -- so a
    StructureRefusal could not have been constructed for any of them without raising."""
    for phrase in (EMPTY_CHAIN_REFUSAL, MISSING_QUOTE_REFUSAL, NON_POSITIVE_NET_REFUSAL,
                   NO_LIQUID_CONTRACT_REFUSAL, SELECTION_CONFIG_REFUSAL):
        assert phrase in REFUSAL_PHRASES
    assert len(set(REFUSAL_PHRASES)) == len(REFUSAL_PHRASES)


def test_resolved_structure_carries_no_score_and_no_max_loss():
    """`_resolve()` runs inside ONE action and cannot know the bar's other candidates, so it
    cannot produce a score; and payoff-at-target needs the recommendation's target, which is a
    risk-manager input. Keeping those fields on ResolvedStructure would force every builder to
    invent them."""
    fields = set(ResolvedStructure.__dataclass_fields__)
    assert "score" not in fields
    assert "payoff_at_target" not in fields
    assert "max_loss_per_contract" not in fields
    assert {"legs", "payoff_legs", "limit_price", "option_strategy", "dte",
            "reserve_kwargs", "reserve_per_contract", "cost_per_contract",
            "sizing_basis"} <= fields


def test_scored_structure_wraps_a_resolved_one_and_adds_the_risk_manager_numbers():
    fields = set(ScoredStructure.__dataclass_fields__)
    assert {"resolved", "max_loss_per_contract", "payoff_at_target", "score"} <= fields


def test_cost_per_contract_is_what_the_sizing_budget_is_divided_by():
    """The whole point of the field: `_size` divides by `premium * 100` and `_size_by_reserve`
    divides by the reserve. Expressing both as "dollars one contract consumes" collapses two
    sizers into one and is what lets the shared tail be shared at all."""
    req = a_request()
    r = ResolvedStructure(request=req, legs=[], payoff_legs=[], limit_price=1.25,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=125.0,
                          sizing_basis="premium")
    assert r.cost_per_contract == 125.0
    assert r.sizing_basis == "premium"


@pytest.mark.parametrize("basis", ["premium", "reserve", "held_shares"])
def test_sizing_basis_accepts_the_three_real_families(basis):
    req = a_request()
    r = ResolvedStructure(request=req, legs=[], payoff_legs=[], limit_price=1.0,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=100.0,
                          sizing_basis=basis)
    assert r.sizing_basis == basis


def test_an_unknown_sizing_basis_is_refused():
    with pytest.raises(ValueError):
        ResolvedStructure(request=a_request(), legs=[], payoff_legs=[], limit_price=1.0,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=100.0,
                          sizing_basis="vibes")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_request.py -q`
Expected: `ImportError: cannot import name 'EMPTY_CHAIN_REFUSAL' from 'ba2_common.core.option_request'`

- [ ] **Step 3: Write the implementation**

In `packages/common/ba2_common/core/option_request.py`, add these five constants immediately after `EMPTY_BOX_REFUSAL`:

```python
# The five above were derived from the design document. These five were derived from the CODE:
# a survey of all 17 entry builders (2026-08-27) catalogued every `_result(False, ...)` they
# emit, and these kinds had no registered phrase -- so `StructureRefusal` would have RAISED on
# any of them, which is the opposite of the "a reason, never a silent drop" contract.
EMPTY_CHAIN_REFUSAL = "the option chain came back empty"
NO_LIQUID_CONTRACT_REFUSAL = "no contract survived the liquidity gates"
MISSING_QUOTE_REFUSAL = "the selected contract carries no usable quote"
NON_POSITIVE_NET_REFUSAL = "the structure prices to a non-positive net"
SELECTION_CONFIG_REFUSAL = "a selection parameter can never select anything"
```

Extend `REFUSAL_PHRASES` to include all five, keeping the existing eight.

Then replace `ResolvedStructure` with the split pair:

```python
#: The three ways a structure's size is decided today. `_size` divides the budget by
#: `premium * 100`; `_size_by_reserve` divides it by the collateral; the two overlays divide
#: held shares by 100 and ignore the budget entirely. Naming the family on the resolution is
#: what lets ONE shared tail size all three without a chain of isinstance checks.
SIZING_BASES = ("premium", "reserve", "held_shares")


@dataclass(frozen=True)
class ResolvedStructure:
    """A concrete, priced structure — everything a SINGLE action can know.

    Deliberately carries no ``score``, no ``payoff_at_target`` and no ``max_loss_per_contract``.
    A score depends on the other candidates on the bar and a payoff-at-target depends on the
    recommendation's target price; an action resolving one structure for one symbol can produce
    neither, and giving it fields it cannot fill would mean every builder inventing a number.
    Those three live on ``ScoredStructure``, which the risk manager builds in Phase 3.

    ``cost_per_contract`` is the dollars ONE contract consumes of the sizing budget. It is the
    common denominator of the two existing sizers -- ``premium * 100`` for ``_size`` and the
    collateral for ``_size_by_reserve`` -- so expressing it once here is what allows a single
    shared sizing tail.
    """

    request: OptionStructureRequest
    legs: List[OptionLeg]                       # what the broker is asked for
    payoff_legs: List[PayoffLeg]                # what the payoff evaluator measures
    limit_price: float                          # the net, signed as _submit_option_order wants
    option_strategy: str                        # reserve-table strategy name
    dte: int
    reserve_per_contract: float
    cost_per_contract: float
    sizing_basis: str
    reserve_kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.sizing_basis not in SIZING_BASES:
            raise ValueError(
                f"Unknown sizing_basis {self.sizing_basis!r}; expected one of "
                f"{list(SIZING_BASES)}. The shared sizing tail dispatches on this, so an "
                f"unrecognised value would silently size nothing.")


@dataclass(frozen=True)
class ScoredStructure:
    """A resolved structure plus the numbers only the risk manager can compute.

    Separate from ``ResolvedStructure`` because these three need inputs an action does not
    have: the recommendation's target price, and the rest of the bar's candidates.
    """

    resolved: ResolvedStructure
    max_loss_per_contract: float
    payoff_at_target: float
    score: float
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_request.py -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_request.py packages/common/tests/test_option_request.py
git commit -m "refactor(options): split ResolvedStructure from ScoredStructure; register the phrases builders emit"
```

---

### Task 2: The shared resolve/size/submit scaffolding on the base

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py` (class `_OptionEntryAction`, around lines 2370-2400)
- Test: `packages/common/tests/test_option_resolve_split.py` (new)

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_resolve_split.py`:

```python
"""The resolve/submit split must be BEHAVIOUR-NEUTRAL and independently callable.

Two properties, and they pull in opposite directions, which is why both are pinned:

  * `execute()` must do exactly what it did before -- same order, same quantity, same limit,
    same refusals, same persisted TradeActionResult rows. ~20 existing tests depend on it.
  * `_resolve()` must be callable ON ITS OWN and reach a priced structure without submitting
    anything, because that is the whole point: in Phase 3 a risk manager calls it.
"""
from datetime import date, timedelta

import pytest

from ba2_common.core import TradeActions
from ba2_common.core.option_request import ResolvedStructure
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderRecommendation


class _Acct:
    """Minimal options account double: serves a chain, a price, a balance, records submits."""

    def __init__(self, chain):
        self._chain = chain
        self.submitted = []

    def get_option_chain(self, symbol, expiry_min, expiry_max, option_type):
        return [c for c in self._chain if c.option_type == option_type]

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_balance(self):
        return 100_000.0

    def submit_option_order(self, **kw):
        self.submitted.append(kw)
        return type("O", (), {"id": len(self.submitted)})()


def _contract(strike, right, *, bid=1.00, ask=1.10, delta=0.30, dte=30):
    return OptionContract(
        symbol=f"X{int(strike*1000):08d}", underlying="X", option_type=right,
        strike=float(strike), expiry=date.today() + timedelta(days=dte),
        bid=bid, ask=ask, last=None, implied_volatility=0.25, delta=delta, volume=500)


CHAIN = ([_contract(s, OptionRight.CALL) for s in (95, 100, 105, 110)]
         + [_contract(s, OptionRight.PUT) for s in (90, 95, 100, 105)])


def _action(cls, **kw):
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    acct = _Acct(CHAIN)
    acct.__class__ = type("_A", (_Acct, OptionsAccountInterface), {})
    params = dict(strike_method="delta", strike_param=0.30, dte_min=1, dte_max=60,
                  sizing=10.0)
    params.update(kw)
    a = cls("X", acct, OrderRecommendation.BUY, **params)
    return a, acct


def test_resolve_returns_a_structure_and_submits_nothing():
    a, acct = _action(TradeActions.BuyCallAction)
    resolved = a._resolve()
    assert isinstance(resolved, ResolvedStructure)
    assert acct.submitted == []          # THE point of the split
    assert resolved.option_strategy == "long_call"
    assert resolved.sizing_basis == "premium"
    assert len(resolved.legs) == 1
    assert len(resolved.payoff_legs) == 1


def test_resolve_prices_cost_per_contract_as_the_premium_times_one_hundred():
    a, _ = _action(TradeActions.BuyCallAction)
    resolved = a._resolve()
    assert resolved.cost_per_contract == pytest.approx(resolved.limit_price * 100.0)


def test_resolve_computes_dte_which_no_builder_used_to_compute():
    a, _ = _action(TradeActions.BuyCallAction)
    assert a._resolve().dte == 30


def test_execute_still_submits_and_matches_the_pre_split_arithmetic():
    a, acct = _action(TradeActions.BuyCallAction)
    a.execute()
    assert len(acct.submitted) == 1
    sub = acct.submitted[0]
    # 100_000 * 10% = 10_000 budget; ask 1.10 -> 110/contract -> floor(10000/110) = 90
    assert sub["quantity"] == 90
    assert sub["limit_price"] == pytest.approx(1.10)
    assert sub["option_strategy"] == "long_call"


def test_a_refusal_from_resolve_short_circuits_execute_without_submitting():
    a, acct = _action(TradeActions.BuyCallAction, strike_method="delta", strike_param=0.30)
    a.account._chain = []                       # empty chain
    result = a.execute()
    assert acct.submitted == []
    assert result.success is False


def test_submit_to_broker_false_still_reaches_the_informational_result():
    a, acct = _action(TradeActions.BuyCallAction)
    a.submit_to_broker = False
    result = a.execute()
    assert acct.submitted == []
    assert result.success is True
    assert "not submitted" in result.message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_resolve_split.py -q`
Expected: `AttributeError: 'BuyCallAction' object has no attribute '_resolve'`

- [ ] **Step 3: Write the implementation**

In `packages/common/ba2_common/core/TradeActions.py`, replace the `_build_and_submit` stub and `execute()` on `_OptionEntryAction` (currently lines ~2370-2398) with:

```python
    def _resolve(self):
        """Select contracts, build legs, price the structure. Return a ``ResolvedStructure``
        — or, for a refusal, the ``self._result(False, ...)`` dict the builder already returns.

        REFUSALS STAY AS ``_result`` DICTS, not ``StructureRefusal``. ``_result`` PERSISTS a
        ``TradeActionResult`` row (``create_and_save_action_result``), and the UI reads those
        rows; returning a pure value object here would silently stop writing them. The typed
        refusal is a Phase 3 concern, on the risk-manager side.

        NO QUANTITY, AND NO ACCOUNT STATE THAT DEPENDS ON ONE. Everything a single action can
        know about one structure belongs here; everything that needs the size belongs in
        ``_size_and_submit``.
        """
        raise NotImplementedError

    def _dte_for(self, expiry) -> int:
        """Days to expiry against the action's clock (simulated in a backtest, wall in live).

        Broken out because NO builder computed this before — it existed only transiently inside
        ``_refuse_if_arc_below_floor``, and only when an ARC floor was configured.
        ``ResolvedStructure`` needs it unconditionally.
        """
        return (expiry - self._today()).days

    def _size_and_submit(self, resolved) -> Dict[str, Any]:
        """Size ``resolved`` and submit it. The former tail of every ``_build_and_submit``.

        BYTE-IDENTICAL ARITHMETIC TO WHAT IT REPLACES. ``_size(premium, pct)`` computed
        ``floor(budget / (premium * 100))`` and ``_size_by_reserve(reserve, pct)`` computed
        ``floor(budget / reserve)``. Both are ``floor(budget / cost_per_contract)``, which is
        why ``ResolvedStructure`` carries that single number instead of the two inputs.
        """
        quantity = self._size_by_cost(resolved.cost_per_contract, self.sizing)
        if quantity < 1:
            return self._result(
                False,
                f"Insufficient budget to size {resolved.option_strategy} for "
                f"{self.instrument_name} (premium={resolved.limit_price})")
        return self._submit_option_order(
            resolved.legs, quantity, resolved.limit_price, resolved.option_strategy)

    def _size_by_cost(self, cost_per_contract: Optional[float],
                      sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / cost_per_contract), capped as before.

        The single sizer ``_size`` and ``_size_by_reserve`` both reduce to. They remain on the
        class (tests and the classic-RM path reference them) and now delegate here, so there is
        one definition of the cap interaction rather than two copies that can drift.
        """
        if not cost_per_contract or cost_per_contract <= 0:
            return 0
        if not sizing_pct or sizing_pct <= 0:
            return 0
        equity = self._virtual_equity()
        if equity is None or equity <= 0:
            return 0
        budget = equity * (sizing_pct / 100.0)
        cap = self._max_equity_per_instrument_cap(equity)
        if cap is not None:
            budget = min(budget, cap)
        return int(math.floor(budget / cost_per_contract))

    def execute(self) -> "TradeActionResult":
        try:
            if not self._supports_options():
                return self._result(False, f"Account does not support options for {self.instrument_name}")
            resolved = self._resolve()
            if not isinstance(resolved, ResolvedStructure):
                return resolved          # a refusal dict from _result(False, ...)
            return self._size_and_submit(resolved)
        except OptionLiquidityDataMissingToday as e:
            # NOT a misconfiguration: this source HAS published the field before, so today's
            # chain simply came back without it (Alpaca types open_interest Optional). The
            # entry is still refused — a gate is never applied to a chain that cannot answer
            # it — but a transient broker gap must not shout ERROR once per symbol per day,
            # nor tell the user to clear a gate they will want back tomorrow.
            logger.warning(f"{self._action_type_value()} for {self.instrument_name}: {e}")
            return self._result(False, str(e))
        except OptionSelectionConfigError as e:
            # A parameter that can never select anything (a liquidity gate the data source
            # cannot answer, an inverted DTE window). NOT a runtime failure and not a
            # market condition — surface the exact knob instead of "No liquid <structure>",
            # which reads as "the chain is thin" and sends the user hunting the wrong thing.
            logger.error(f"{self._action_type_value()} for {self.instrument_name} is "
                         f"misconfigured: {e}")
            return self._result(False, str(e))
        except Exception as e:
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Error executing {self._action_type_value()} for {self.instrument_name}: {e}",
                         exc_info=True)
            return self._result(False, f"Error executing option action: {str(e)}")
```

Then rewrite the two existing sizers to delegate, preserving their signatures because tests and the classic-RM path call them by name:

```python
    def _size(self, premium: float, sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / (premium * 100)); 0 if not sizeable.

        Now a thin adapter over ``_size_by_cost``: a premium is a per-SHARE price, so one
        contract costs ``premium * 100``. Kept as a named method because tests and the
        equity-side RM reference it.
        """
        if premium is None or premium <= 0:
            return 0
        return self._size_by_cost(premium * 100.0, sizing_pct)

    def _size_by_reserve(self, reserve_per_contract: float,
                         sizing_pct: Optional[float]) -> int:
        """floor(virtual_equity * sizing% / reserve_per_contract). For credit/naked structures
        where net premium is negative (can't size off premium). Adapter over ``_size_by_cost``:
        the collateral IS the per-contract cost."""
        return self._size_by_cost(reserve_per_contract, sizing_pct)
```

Add the import at the top of `TradeActions.py`, beside the other `ba2_common.core` imports:

```python
from ba2_common.core.option_request import ResolvedStructure
```

- [ ] **Step 4: Run tests to verify the scaffolding works and nothing regressed**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_resolve_split.py -q`
Expected: FAIL on the `_resolve` tests (no builder implements it yet), PASS on `test_execute_still_submits...` only after Task 3. This is expected: Task 2 lands the scaffolding, Task 3 lands the first user.

Run: `./venv/bin/python -m pytest packages/common/tests/ -q`
Expected: `2352 passed` — unchanged. `_size` and `_size_by_reserve` now delegate, so if this number moves you have changed the arithmetic.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_option_resolve_split.py
git commit -m "refactor(options): add _resolve/_size_and_submit scaffolding; unify the two sizers"
```

---

### Task 3: Convert the 7 premium-sized builders

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py`
- Test: `packages/common/tests/test_option_resolve_split.py` (extend)

All seven tails are **identical in shape** — verified by extraction, not assumed:

```
    <premium> = <expression>                       # stays in _resolve
    if <premium> <= 0: return self._result(...)    # stays in _resolve  (5 of 7 have this)
    quantity = self._size(<premium>, self.sizing)  # DELETE — the shared tail does it
    if quantity < 1: return self._result(...)      # DELETE — the shared tail does it
    return self._submit_option_order(legs, quantity, <premium>, "<strategy>")   # DELETE
```

replaced by:

```
    return ResolvedStructure(
        request=None, legs=<legs>, payoff_legs=<payoff legs>, limit_price=<premium>,
        option_strategy="<strategy>", dte=self._dte_for(<expiry>),
        reserve_per_contract=0.0, cost_per_contract=<premium> * 100.0,
        sizing_basis="premium", reserve_kwargs={})
```

`request=None` is correct for this phase: `OptionStructureRequest` is produced by the rule engine in Phase 3, and nothing reads the field yet. `reserve_per_contract=0.0` is correct because all seven are debit structures in `ZERO_RESERVE_STRATEGIES`, which is why none of them passes `option_reserve=` today.

**The exact per-builder values** (extracted from the current source, not inferred):

| class | rename `_build_and_submit` → `_resolve`, then | premium expression | strategy | expiry for `dte` | payoff legs |
|---|---|---|---|---|---|
| `BuyCallAction` | 1 leg | `contract.ask` | `long_call` | `contract.expiry` | 1 × BUY call @ `contract.ask` |
| `BuyPutAction` | 1 leg | `contract.ask` | `long_put` | `contract.expiry` | 1 × BUY put @ `contract.ask` |
| `OpenBullCallSpreadAction` | 2 legs | `round(long_c.ask - short_c.bid, 4)` | `bull_call_spread` | `long_c.expiry` | BUY call @ `long_c.ask`, SELL call @ `short_c.bid` |
| `OpenBearPutSpreadAction` | 2 legs | `round(long_c.ask - short_c.bid, 4)` | `bear_put_spread` | `long_c.expiry` | BUY put @ `long_c.ask`, SELL put @ `short_c.bid` |
| `OpenStraddleAction` | 2 legs | `round(call_c.ask + put_c.ask, 4)` | `straddle` | `call_c.expiry` | BUY call @ `call_c.ask`, BUY put @ `put_c.ask` |
| `OpenStrangleAction` | 2 legs | `round(call_c.ask + put_c.ask, 4)` | `strangle` | `call_c.expiry` | BUY call @ `call_c.ask`, BUY put @ `put_c.ask` |
| `OpenCallButterflyAction` | 3 legs | `round(lower.ask + upper.ask - 2 * body.bid, 4)` | `call_butterfly` | `body.expiry` | BUY call @ `lower.ask` r1, SELL call @ `body.bid` **r2**, BUY call @ `upper.ask` r1 |

Every premium above is a per-share value the builder already computes, so **no builder needs a new price lookup**. The butterfly's body leg is the only one with `ratio=2`.

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_option_resolve_split.py`:

```python
PREMIUM_SIZED = [
    ("BuyCallAction", "long_call", 1),
    ("BuyPutAction", "long_put", 1),
    ("OpenBullCallSpreadAction", "bull_call_spread", 2),
    ("OpenBearPutSpreadAction", "bear_put_spread", 2),
    ("OpenStraddleAction", "straddle", 2),
    ("OpenStrangleAction", "strangle", 2),
    ("OpenCallButterflyAction", "call_butterfly", 3),
]


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_every_premium_sized_builder_resolves_without_submitting(cls_name, strategy, n_legs):
    a, acct = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip(f"{cls_name} refused on this synthetic chain: {resolved.message}")
    assert acct.submitted == []
    assert resolved.option_strategy == strategy
    assert len(resolved.legs) == n_legs
    assert len(resolved.payoff_legs) == n_legs
    assert resolved.sizing_basis == "premium"
    assert resolved.reserve_per_contract == 0.0
    assert resolved.cost_per_contract == pytest.approx(resolved.limit_price * 100.0)
    assert resolved.dte == 30


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_payoff_legs_reprice_to_the_same_net_the_limit_price_carries(cls_name, strategy, n_legs):
    """The payoff legs and the limit price are two views of one structure. If they disagree,
    the risk manager's max-loss and the broker's fill price describe different trades."""
    from ba2_common.core.option_payoff import payoff_at, validate_legs
    a, _ = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip("refused on this synthetic chain")
    assert validate_legs(resolved.payoff_legs) is None
    # At an underlying of 0 every call is worthless and every put is worth its strike, so the
    # payoff there is a pure function of the premiums -- which is what we are cross-checking.
    net_paid = sum((1 if leg.side.value == "buy" else -1) * leg.premium * leg.ratio
                   for leg in resolved.payoff_legs)
    assert net_paid == pytest.approx(resolved.limit_price, abs=1e-4)


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_execute_still_submits_for_every_premium_sized_builder(cls_name, strategy, n_legs):
    a, acct = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    result = a.execute()
    if not result.success:
        pytest.skip(f"{cls_name} refused on this synthetic chain: {result.message}")
    assert len(acct.submitted) == 1
    assert acct.submitted[0]["option_strategy"] == strategy
    assert acct.submitted[0]["quantity"] >= 1


def test_the_butterfly_body_leg_carries_ratio_two():
    a, _ = _action(TradeActions.OpenCallButterflyAction, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip("refused on this synthetic chain")
    ratios = sorted(leg.ratio for leg in resolved.payoff_legs)
    assert ratios == [1, 1, 2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_resolve_split.py -q`
Expected: failures with `NotImplementedError` from the base `_resolve`.

- [ ] **Step 3: Convert each of the seven builders**

For each class in the table above, apply exactly this transformation and nothing else:

1. Rename the method `_build_and_submit` → `_resolve`. **Do not move it out of the class** — four test files AST-scan these class bodies for `method=self.strike_method` and for `_submit_option_order` call sites.
2. Leave every line up to and including the premium computation and its `<= 0` guard untouched.
3. Delete the three tail lines (`quantity = self._size(...)`, the `if quantity < 1` refusal, and the `return self._submit_option_order(...)`).
4. Return the `ResolvedStructure` shown above, filled from the table's row for that class.
5. Build `payoff_legs` from the table's last column using `PayoffLeg(kind=..., side=OrderDirection.BUY|SELL, premium=<the leg's own quote side>, strike=<contract>.strike, ratio=<1 or 2>)`. Leave `multiplier` at its default.

Add to the imports at the top of `TradeActions.py`:

```python
from ba2_common.core.option_payoff import PayoffLeg
```

Worked example — `BuyCallAction`, the whole method after conversion:

```python
    def _resolve(self):
        chain = self._chain(self.OPTION_TYPE)
        if not chain:
            return self._result(False, f"Empty option chain for {self.instrument_name}")
        liq = self._liq(chain)
        spot = self._spot()
        contract = select_single(
            chain, method=self.strike_method, strike_param=self.strike_param, spot=spot,
            option_type=self.OPTION_TYPE, dte_min=self.dte_min, dte_max=self.dte_max,
            today=self._today(), target_price=self._consensus_target(), **liq)
        if contract is None:
            return self._result(False, f"No liquid call contract for {self.instrument_name}")
        if contract.ask is None or contract.ask <= 0:
            return self._result(False, f"No ask price for {contract.symbol}")
        limit_price = contract.ask                          # buy at ASK
        leg = OptionLeg(contract_symbol=contract.symbol, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=self.OPTION_TYPE,
                        strike=contract.strike, expiry=contract.expiry,
                        underlying=contract.underlying)
        return ResolvedStructure(
            request=None, legs=[leg],
            payoff_legs=[PayoffLeg(kind="call", side=OrderDirection.BUY,
                                   premium=contract.ask, strike=contract.strike)],
            limit_price=limit_price, option_strategy="long_call",
            dte=self._dte_for(contract.expiry), reserve_per_contract=0.0,
            cost_per_contract=limit_price * 100.0, sizing_basis="premium",
            reserve_kwargs={})
```

- [ ] **Step 4: Run the full package suite**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_resolve_split.py -q`
Expected: all pass.

Run: `./venv/bin/python -m pytest packages/common/tests/ -q`
Expected: `2352` **plus** the new tests, with **zero previously-passing tests failing**. In particular these five files must be untouched-green, because they are the behaviour-neutrality proof:

```bash
./venv/bin/python -m pytest \
  packages/common/tests/test_option_reserve_lockstep.py \
  packages/common/tests/test_strike_method_registry.py \
  packages/common/tests/test_option_leg_liquidity_symmetry.py \
  packages/common/tests/test_option_strike_method_honoured.py \
  packages/common/tests/test_new_option_actions.py -q
```
Expected: `19 + 21 + 17 + 18 + 52 = 127 passed`.

Run: `./venv/bin/python -m pytest tests/ -q`
Expected: `4410 passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_option_resolve_split.py
git commit -m "refactor(options): convert the 7 premium-sized builders to _resolve()"
```

---

### Task 4: Update the two tests that call `_build_and_submit` by name

**Files:**
- Modify: `packages/common/tests/test_held_equity_shares_arithmetic.py:146,161`

These are the only two tests in the repo that call the old method directly. Both belong to the covered-call/protective-put family, which this plan does **not** convert — so after Task 3 they still call a method that exists. Verify that, and only rename if it has genuinely gone.

- [ ] **Step 1: Check whether anything still references the old name**

Run: `grep -rn "_build_and_submit" packages/ tests/ testplatform/ --include=*.py`

Expected after Task 3: hits remain for the 10 unconverted builders (8 reserve-sized + 2 overlays) and for the two tests. If a hit names one of the 7 converted classes, you missed a call site.

- [ ] **Step 2: Run the file**

Run: `./venv/bin/python -m pytest packages/common/tests/test_held_equity_shares_arithmetic.py -q`
Expected: `11 passed` — unchanged, because the overlay builders are untouched by this plan.

- [ ] **Step 3: Commit only if something changed**

If Step 2 passed with no edit, there is nothing to commit; say so and move on.

---

### Task 5: Prove the split changed no arithmetic

**Files:**
- Test: `packages/common/tests/test_option_resolve_split.py` (extend)

- [ ] **Step 1: Write the equivalence test**

Append:

```python
def test_the_unified_sizer_reproduces_both_old_sizers_exactly():
    """`_size` and `_size_by_reserve` now both delegate to `_size_by_cost`. This pins that the
    delegation is arithmetic-preserving, over the whole grid of inputs that used to hit two
    separate implementations -- including the zero and negative cases each guarded differently.
    """
    import math
    a, _ = _action(TradeActions.BuyCallAction)
    equity = a._virtual_equity()
    for premium in (0.01, 0.10, 1.00, 1.10, 7.35, 250.0):
        for pct in (0.5, 1.0, 10.0, 100.0):
            budget = equity * (pct / 100.0)
            cap = a._max_equity_per_instrument_cap(equity)
            if cap is not None:
                budget = min(budget, cap)
            assert a._size(premium, pct) == int(math.floor(budget / (premium * 100.0)))
            assert a._size_by_reserve(premium * 100.0, pct) == a._size(premium, pct)


@pytest.mark.parametrize("bad", [0, -1.0, None])
def test_both_sizers_still_refuse_the_unsizeable_inputs_they_always_refused(bad):
    a, _ = _action(TradeActions.BuyCallAction)
    assert a._size(bad, 10.0) == 0
    assert a._size(1.0, bad) == 0
    assert a._size_by_reserve(bad, 10.0) == 0
    assert a._size_by_reserve(100.0, bad) == 0
```

- [ ] **Step 2: Run it**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_resolve_split.py -q`
Expected: all pass.

- [ ] **Step 3: Mutation-check the equivalence**

The whole plan claims behaviour-neutrality, so prove the tests would notice if it were not. Use a Python harness that VERIFIES each substitution landed — a shell `sed`/`perl` one-liner silently no-ops on unescaped regex metacharacters, and a run where every mutant "survives" with output identical to the control means the mutants never applied.

Mutate `packages/common/ba2_common/core/TradeActions.py`, one at a time, restoring from a file copy (NOT `git checkout`, which would destroy uncommitted work), with `PYTHONDONTWRITEBYTECODE=1` and `__pycache__` purged either side:

1. `_size_by_cost`: `math.floor` → `math.ceil`
2. `_size`: `premium * 100.0` → `premium * 10.0`
3. `_size_and_submit`: `if quantity < 1` → `if quantity < 0`
4. `_size_by_cost`: drop the `cap` clamp
5. `BuyCallAction._resolve`: `cost_per_contract=limit_price * 100.0` → `limit_price`

Expected: all five KILLED. Verify the source is restored byte-identically (`md5`) and that the control run is green both before and after.

- [ ] **Step 4: Commit**

```bash
git add packages/common/tests/test_option_resolve_split.py
git commit -m "test(options): pin that the unified sizer reproduces both old sizers exactly"
```

---

### Task 6: Version bump and push

- [ ] **Step 1: Run all three suites**

```bash
./venv/bin/python -m pytest packages/common/tests/ -q
./venv/bin/python -m pytest tests/ -q
PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python -m pytest \
  testplatform/backend -q --ignore=testplatform/backend/tests_scripts --ignore=testplatform/backend/scripts
```

Expected: `packages/common` up by the new tests only; `tests/` **4410**; backend **3202 passed** with the single known Windows-only `test_worker_server.py::test_logs_rejects_path_traversal` failure. Any other failure is real.

- [ ] **Step 2: Bump `TEST_APP_VERSION`**

`packages/` changed, so per `CLAUDE.md` bump `testplatform/version.py` (not `APP_VERSION`) by 1. Read the current value first — other machines push to this file and it moves between sessions.

- [ ] **Step 3: Commit and push**

```bash
git add testplatform/version.py
git commit -m "chore: bump TEST_APP_VERSION for the option resolve split (phase 2a)"
git push
```

---

## What this plan does NOT do

- **The 8 reserve-sized builders** (`SellCashSecuredPut`, `OpenBearCallSpread`, `OpenBullPutSpread`, `OpenShortStraddle`, `OpenShortStrangle`, `OpenIronCondor`, `OpenJadeLizard`, `OpenPutRatioSpread`) — Phase 2b. Their tails differ: each computes a reserve, several call `option_reserve_required` twice with different quantities, and all eight run a buying-power gate that reads live state after sizing.
- **The 2 overlay builders** (`SellCoveredCall`, `BuyProtectivePut`) — Phase 2c, and they are the hard ones. They refuse on held shares *before* fetching a chain, so making `_resolve()` run first would have them fetch chains they do not fetch today. They are also the only two whose `payoff_legs` need a **stock leg**, whose per-share basis no builder computes.
- **Any risk manager.** Nothing moves out of the action in this phase. `_resolve()` merely becomes independently callable.
- **The four call sites.** `_entry_is_option`, the overlay ruleset, the unified `TradeRule` path and the `PremiumSeller` bypass are all untouched, because behaviour is unchanged.

## Self-review notes

- Spec §4.2 (the submit seam is a single point all builders end at) → confirmed by extraction; Task 3 relies on it.
- Spec §4.5 (`ResolvedStructure` shape) → **amended** by Task 1, with the reason recorded in the file. The spec's field list is what Phase 3's `ScoredStructure` carries.
- Every code step shows real code. The one place this plan uses a table rather than seven repeated task bodies is Task 3, because the seven transformations are provably the same edit — the table carries each builder's actual values, so nothing is left for the implementer to infer.
- Types used in later tasks (`ResolvedStructure`, `PayoffLeg`, `_size_by_cost`, `_dte_for`, `sizing_basis`) are all defined in Tasks 1 and 2.
