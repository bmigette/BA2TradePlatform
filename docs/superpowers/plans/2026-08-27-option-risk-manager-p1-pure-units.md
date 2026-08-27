# Option Risk Manager — Phase 1 (Pure Units) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four pure, dependency-free units the option risk manager will stand on — term vocabulary, payoff/max-loss evaluator, contract selection policy, and the request/resolved value objects — with no change to any existing behaviour.

**Architecture:** Four new modules under `packages/common/ba2_common/core/`, each with one responsibility and no DB, network or broker access. Nothing imports them yet; Phase 2 wires them in. One small supporting change promotes `option_selector._target_strike` to public so the policy can reuse it instead of duplicating it.

**Tech Stack:** Python 3.11+, stdlib dataclasses and enums, pytest. No new third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-option-risk-manager-design.md` (sections 4.5, 5, 6, 7).

---

## Orientation for the implementer

You almost certainly have no context for this codebase. Read this section before Task 1.

**What an option structure is here.** A "structure" is one trade made of one or more option
contracts (legs) — a long call is one leg, an iron condor is four. Sixteen of them are implemented
as rule actions in `packages/common/ba2_common/core/TradeActions.py` (classes `BuyCallAction`
through `OpenPutRatioSpreadAction`, lines 2401–3600). You are not touching those in this phase.

**Where shared code lives.** This repo did a "Phase 6" split: the real implementation of shared code
lives in `packages/common/ba2_common/`, and the files under `ba2_trade_platform/core/` are thin
re-export shims. **Always add shared code to `packages/common`.** Editing a shim is pointless — it
only re-exports.

**Two house rules that this plan leans on heavily.**

1. *Unknown is never zero.* The recurring defect class in this codebase is a missing measurement
   silently reading as `0`, which then looks like a real, safe answer. Every function you write here
   that could fail to measure something must say so distinctly — never by returning `0`, `[]` or
   `False`. Read the docstring of `packages/common/ba2_common/core/option_economics.py` for the
   canonical statement of this.
2. *A gate nobody can satisfy is a configuration error, not a verdict.* See
   `option_selector.OptionLiquidityDataUnavailable`.

**Running tests.** The venv is `venv/`, **not** `.venv/`:

```bash
./venv/bin/python -m pytest packages/common/tests/ -q
```

You cannot run `tests/` and `packages/common/tests/` in one pytest invocation — both have a
`conftest.py` that imports as `tests.conftest` and they collide. Run them separately.

**Baseline before you start:** `packages/common` is 2169 passing. Nothing in this phase should
change that number except by adding to it.

**Do not place real orders.** Nothing in this phase touches a broker, but be aware of the rule.

---

## File Structure

| file | responsibility |
|---|---|
| `packages/common/ba2_common/core/option_terms.py` | The finite `OptionTerm` enum and its DTE windows. Nothing else. |
| `packages/common/ba2_common/core/option_payoff.py` | `PayoffLeg`, `payoff_at()`, `max_loss()`. Pure arithmetic on a leg set; knows nothing about strategies by name. |
| `packages/common/ba2_common/core/option_selection_policy.py` | `SelectionPolicy` (the weights), `PolicyContext` (the box), `pick()`. Chooses one contract from a candidate list. |
| `packages/common/ba2_common/core/option_request.py` | `OptionStructureRequest`, `ResolvedStructure`, `StructureRefusal`, and the refusal phrase constants. Value objects only, no logic. |
| `packages/common/ba2_common/core/option_selector.py` | **Modified**: rename private `_target_strike` to public `target_strike` so the policy can reuse it. |

Four new files rather than one because they have genuinely different reasons to change: the term
windows are a product decision, the payoff maths is arithmetic, the policy is a search-space
definition, and the value objects are a wire format.

---

### Task 1: Option term vocabulary

**Files:**
- Create: `packages/common/ba2_common/core/option_terms.py`
- Test: `packages/common/tests/test_option_terms.py`

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_terms.py`:

```python
"""The term vocabulary must be total, ordered, and loud about unknown input.

WHY THESE PROPERTIES. ``option_term`` replaces the ``dte_min``/``dte_max`` pair as a GA gene,
and a categorical gene is only well-behaved if every value it can take resolves to a distinct,
usable window. A missing window would crash mid-backtest; an inverted one would silently select
nothing (the exact failure ``TradeActions._expiry_window`` already has to raise for); two
overlapping windows would let two distinct gene values produce identical behaviour, which wastes
GA budget on a difference that isn't one.
"""
import pytest

from ba2_common.core.option_terms import OptionTerm, dte_window


def test_every_term_has_a_window():
    for term in OptionTerm:
        lo, hi = dte_window(term)
        assert isinstance(lo, int) and isinstance(hi, int)


def test_no_window_is_inverted():
    for term in OptionTerm:
        lo, hi = dte_window(term)
        assert lo <= hi, f"{term} window [{lo}, {hi}] is inverted"


def test_windows_are_ordered_and_non_overlapping():
    windows = [dte_window(t) for t in OptionTerm]
    for (lo_a, hi_a), (lo_b, hi_b) in zip(windows, windows[1:]):
        assert hi_a < lo_b, f"[{lo_a},{hi_a}] overlaps or precedes [{lo_b},{hi_b}]"


def test_one_month_matches_the_existing_grid_default():
    # ba2test_launcher's option strategies all default to option_dte_min=25/max=45.
    # ONE_MONTH must contain that window or migrating the grid changes what it trades.
    lo, hi = dte_window(OptionTerm.ONE_MONTH)
    assert lo <= 25 and hi >= 45


def test_string_value_resolves():
    assert dte_window("1m") == dte_window(OptionTerm.ONE_MONTH)


def test_unknown_string_raises_and_names_the_valid_values():
    with pytest.raises(ValueError) as exc:
        dte_window("1 month")
    assert "1m" in str(exc.value)


def test_none_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        dte_window(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_terms.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'ba2_common.core.option_terms'`

- [ ] **Step 3: Write the implementation**

Create `packages/common/ba2_common/core/option_terms.py`:

```python
"""The finite option TERM vocabulary and its days-to-expiry windows.

A term is what a rule REQUESTS ("one month"); the window is what the selector filters expiries
with. Keeping the vocabulary finite and central is what turns term into a single categorical
gene. The ``dte_min``/``dte_max`` pair it replaces is two correlated integers that can express an
inverted, unsatisfiable window — ``TradeActions._expiry_window`` exists largely to raise for
exactly that case.

THE WINDOWS ARE HARD. Nothing here or downstream widens one: a term whose window contains no
selectable expiry is a refusal with a reason, never a silent substitution. Widening would make
the gene partly meaningless — a GA result could not distinguish "ONE_MONTH worked" from
"ONE_MONTH quietly became TWO_MONTHS half the time".
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Tuple


class OptionTerm(str, Enum):
    """A requested holding term. The VALUES are the wire format in rule-action JSON.

    NB for anyone later persisting this on a model: this codebase stores str-enums by NAME,
    not value, so a migration would have to backfill ``"ONE_MONTH"``, not ``"1m"``.
    """

    ZERO_DTE = "0dte"
    ONE_WEEK = "1w"
    TWO_WEEKS = "2w"
    ONE_MONTH = "1m"
    TWO_MONTHS = "2m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    LEAPS = "leaps"


#: term -> (dte_min, dte_max), both INCLUSIVE.
#:
#: The gaps between windows (19-20, 116-149, 211-299) are DELIBERATE. Two adjacent terms that
#: shared a boundary could select the same expiry on a chain with sparse expiries, collapsing two
#: distinct gene values into one behaviour — the GA would then spend budget distinguishing
#: options that are not different. ONE_MONTH is 21-45 because that contains the 25-45 window
#: every strategy in ``ba2test_launcher`` currently defaults to.
_WINDOWS: Dict["OptionTerm", Tuple[int, int]] = {
    OptionTerm.ZERO_DTE: (0, 0),
    OptionTerm.ONE_WEEK: (1, 9),
    OptionTerm.TWO_WEEKS: (10, 18),
    OptionTerm.ONE_MONTH: (21, 45),
    OptionTerm.TWO_MONTHS: (46, 75),
    OptionTerm.THREE_MONTHS: (76, 115),
    OptionTerm.SIX_MONTHS: (150, 210),
    OptionTerm.LEAPS: (300, 450),
}


def dte_window(term) -> Tuple[int, int]:
    """The INCLUSIVE ``(dte_min, dte_max)`` window for ``term``.

    Accepts an ``OptionTerm`` or its string value (rule-action JSON carries the string).

    Raises ``ValueError`` for anything else. Returning a default window instead would silently
    trade a term nobody asked for, which is the worst available outcome: the backtest would
    report results for a strategy that was never configured.
    """
    valid = [t.value for t in OptionTerm]
    # OptionTerm is a str subclass, so plain isinstance(term, str) is True for members too.
    if isinstance(term, str) and not isinstance(term, OptionTerm):
        try:
            term = OptionTerm(term)
        except ValueError:
            raise ValueError(
                f"Unknown option term {term!r}; expected one of {valid}") from None
    try:
        return _WINDOWS[term]
    except (KeyError, TypeError):
        raise ValueError(
            f"Unknown option term {term!r}; expected one of {valid}") from None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_terms.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_terms.py packages/common/tests/test_option_terms.py
git commit -m "feat(options): finite OptionTerm vocabulary with hard DTE windows"
```

---

### Task 2: Payoff leg and payoff-at-price

**Files:**
- Create: `packages/common/ba2_common/core/option_payoff.py`
- Test: `packages/common/tests/test_option_payoff.py`

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_payoff.py`:

```python
"""Payoff at expiry, checked against hand-computed values for real structures.

WHY HAND-COMPUTED AND NOT PROPERTY-ONLY. The whole point of deriving max loss from the payoff
curve is that a hand-written per-structure table drifts. That argument only holds if the curve
itself is right, so the curve is pinned to arithmetic a reader can verify in their head.
"""
import pytest

from ba2_common.core.option_payoff import PayoffLeg, payoff_at, validate_legs
from ba2_common.core.types import OrderDirection


def long_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_stock(entry):
    return PayoffLeg(kind="stock", side=OrderDirection.BUY, premium=entry, strike=None)


def test_long_call_below_strike_loses_exactly_the_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 90.0) == pytest.approx(-500.0)


def test_long_call_above_breakeven_is_intrinsic_less_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 110.0) == pytest.approx(500.0)


def test_short_put_at_zero_loses_strike_less_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 0.0) == pytest.approx(-9700.0)


def test_short_put_expiring_worthless_keeps_the_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 120.0) == pytest.approx(300.0)


def test_covered_call_is_capped_above_the_strike():
    # 100 shares bought at 100, short 105 call for 2. Above 105 the payoff is flat at
    # (105 - 100 + 2) * 100 = 700.
    legs = [long_stock(100.0), short_call(105, 2.0)]
    assert payoff_at(legs, 105.0) == pytest.approx(700.0)
    assert payoff_at(legs, 130.0) == pytest.approx(700.0)


def test_stock_leg_defaults_to_the_hundred_shares_backing_one_contract():
    legs = [long_stock(50.0)]
    assert payoff_at(legs, 51.0) == pytest.approx(100.0)


def test_ratio_multiplies_the_leg():
    one = payoff_at([short_put(100, 3.0)], 0.0)
    two = payoff_at([short_put(100, 3.0, ratio=2)], 0.0)
    assert two == pytest.approx(2 * one)


@pytest.mark.parametrize("legs, fragment", [
    ([], "no legs"),
    ([PayoffLeg(kind="future", side=OrderDirection.BUY, premium=1.0, strike=100)],
     "unknown leg kind"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=-1.0, strike=100)],
     "not a usable price"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=None)],
     "not a usable strike"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=100, ratio=0)],
     "must be positive"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=float("nan"), strike=100)],
     "not a usable price"),
])
def test_validate_legs_names_the_problem(legs, fragment):
    problem = validate_legs(legs)
    assert problem is not None and fragment in problem


def test_validate_legs_accepts_a_good_structure():
    assert validate_legs([long_call(100, 5.0), short_call(110, 2.0)]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_payoff.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'ba2_common.core.option_payoff'`

- [ ] **Step 3: Write the implementation**

Create `packages/common/ba2_common/core/option_payoff.py`:

```python
"""Payoff at expiry for an arbitrary option/stock leg set. Pure: no DB, no network, no broker.

WHY THIS EXISTS RATHER THAN A PER-STRUCTURE MAX-LOSS TABLE. The platform already has one
per-structure risk table — ``OptionsAccountInterface.option_reserve_required`` — and it is BROKER
MARGIN, which is not maximum loss and diverges from it in both directions. A cash-secured put
reserves ``strike * 100`` but can only lose ``(strike - credit) * 100``. A jade lizard reserves
the put strike PLUS the call wing, though its loss is bounded by the put side alone. Both remain
correct as margin; neither is max loss.

A second hand-maintained table would be a second thing to keep correct against seventeen builders,
and it drifts easily: the intuitive max loss for a covered call is "basis minus strike minus
credit", which is WRONG — the strike caps the upside, not the downside, and the real answer is
"basis minus credit" (the stock going to zero). Derived from the legs, it cannot be got wrong
structure-by-structure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ba2_common.core.types import OrderDirection

#: Leg kinds that carry a strike. ``stock`` is the third valid kind and carries none.
_OPTION_KINDS = ("call", "put")
_ALL_KINDS = ("call", "put", "stock")


@dataclass(frozen=True)
class PayoffLeg:
    """One leg of a structure, as the payoff evaluator sees it.

    ``premium`` is ALWAYS POSITIVE — what was paid for a BUY, what was received for a SELL. The
    direction lives in ``side`` alone, so a caller cannot express "a short leg with a negative
    credit" and silently get a sign flip. Every sign in this module derives from ``side``.

    A STOCK LEG uses ``kind="stock"``, ``strike=None``, ``premium=`` the per-share entry price,
    and the DEFAULT ``multiplier=100.0`` — i.e. one stock leg is the 100 shares that back one
    contract. That keeps a covered call's two legs on the same scale with no arithmetic at the
    call site.

    ``ratio`` is legs per ONE structure unit: a 1x2 put ratio spread has a short leg with
    ``ratio=2``.
    """

    kind: str                        # "call" | "put" | "stock"
    side: OrderDirection             # BUY = long, SELL = short
    premium: float                   # per share, always positive
    strike: Optional[float] = None   # required for call/put, None for stock
    ratio: int = 1
    multiplier: float = 100.0


def validate_legs(legs: Sequence[PayoffLeg]) -> Optional[str]:
    """``None`` when every leg can be priced; otherwise a human-readable reason it cannot.

    RETURNED, NOT RAISED. The caller turns this into a recorded refusal on ONE structure. A
    raise here would escape into the middle of a bar's evaluation and take every other
    structure's decision down with it — the same reasoning as
    ``option_economics.collateral_per_contract``.
    """
    if not legs:
        return "structure has no legs"
    for i, leg in enumerate(legs):
        where = f"leg {i} ({leg.kind})"
        if leg.kind not in _ALL_KINDS:
            return f"{where}: unknown leg kind {leg.kind!r}, expected one of {list(_ALL_KINDS)}"
        if leg.side not in (OrderDirection.BUY, OrderDirection.SELL):
            return f"{where}: side {leg.side!r} is neither BUY nor SELL"
        if (leg.premium is None or isinstance(leg.premium, bool)
                or not math.isfinite(leg.premium) or leg.premium < 0):
            return (f"{where}: premium {leg.premium!r} is not a usable price "
                    f"(must be a finite, non-negative number; the sign lives in `side`)")
        if leg.ratio is None or isinstance(leg.ratio, bool) or leg.ratio <= 0:
            return f"{where}: ratio {leg.ratio!r} must be positive"
        if (leg.multiplier is None or not math.isfinite(leg.multiplier)
                or leg.multiplier <= 0):
            return f"{where}: multiplier {leg.multiplier!r} must be positive"
        if leg.kind in _OPTION_KINDS:
            if (leg.strike is None or isinstance(leg.strike, bool)
                    or not math.isfinite(leg.strike) or leg.strike <= 0):
                return f"{where}: strike {leg.strike!r} is not a usable strike"
    return None


def _sign(side: OrderDirection) -> float:
    """+1 for a long leg, -1 for a short one. The ONLY place direction becomes arithmetic."""
    return 1.0 if side == OrderDirection.BUY else -1.0


def payoff_at(legs: Sequence[PayoffLeg], spot: float) -> float:
    """Total P&L in DOLLARS of ONE structure unit if the underlying expires at ``spot``.

    Assumes ``validate_legs(legs) is None`` — call it first. Passing unvalidated legs will
    raise a ``TypeError`` on the bad leg rather than returning a wrong number, which is the
    intended failure mode.
    """
    total = 0.0
    for leg in legs:
        if leg.kind == "call":
            intrinsic = max(spot - leg.strike, 0.0)
        elif leg.kind == "put":
            intrinsic = max(leg.strike - spot, 0.0)
        else:  # stock
            intrinsic = spot
        s = _sign(leg.side)
        total += (s * intrinsic - s * leg.premium) * leg.ratio * leg.multiplier
    return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_payoff.py -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_payoff.py packages/common/tests/test_option_payoff.py
git commit -m "feat(options): PayoffLeg and payoff-at-expiry evaluator"
```

---

### Task 3: Max loss, as a three-state result

**Files:**
- Modify: `packages/common/ba2_common/core/option_payoff.py` (append)
- Test: `packages/common/tests/test_option_max_loss.py`

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_max_loss.py`:

```python
"""Max loss for every structure family, cross-checked against closed-form arithmetic.

WHY EACH CASE. These are the structures whose reserve (broker margin) and max loss DIFFER, so
they are precisely the ones a reader might assume the reserve table already answers:

  * cash-secured put   reserve strike*100          max loss (strike-credit)*100
  * jade lizard        reserve (K_p+width-c)*100   max loss (K_p-c)*100  <- reserve OVERSTATES
  * covered call       reserve 0                   max loss (basis-credit)*100
  * short straddle     reserve Reg-T               max loss UNBOUNDED
"""
import pytest

from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, UNMEASURABLE, PayoffLeg, max_loss)
from ba2_common.core.types import OrderDirection


def leg(kind, side, premium, strike=None, ratio=1):
    return PayoffLeg(kind=kind, side=side, premium=premium, strike=strike, ratio=ratio)


LONG, SHORT = OrderDirection.BUY, OrderDirection.SELL


def test_long_call_max_loss_is_the_debit():
    r = max_loss([leg("call", LONG, 5.0, 100)])
    assert r.state == MEASURED and r.amount == pytest.approx(500.0)


def test_credit_vertical_max_loss_is_width_less_credit():
    # Short 100 call for 3.0, long 105 call for 1.0 -> credit 2.0, width 5.
    legs = [leg("call", SHORT, 3.0, 100), leg("call", LONG, 1.0, 105)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(300.0)


def test_cash_secured_put_max_loss_is_strike_less_credit_not_the_full_strike():
    r = max_loss([leg("put", SHORT, 3.0, 90)])
    assert r.state == MEASURED and r.amount == pytest.approx(8700.0)


def test_iron_condor_with_unequal_wings_uses_the_WIDER_wing():
    # Put side 90/85 (width 5), call side 110/118 (width 8), total credit 3.0.
    legs = [leg("put", SHORT, 2.0, 90), leg("put", LONG, 1.0, 85),
            leg("call", SHORT, 2.5, 110), leg("call", LONG, 0.5, 118)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(500.0)  # (8 - 3) * 100


def test_jade_lizard_max_loss_is_the_put_side_only():
    # Short 90 put 2.0, short 105 call 1.5, long 110 call 0.5 -> credit 3.0, call width 5.
    # Reserve would be (90 + 5 - 3) * 100 = 9200. True max loss is (90 - 3) * 100 = 8700.
    legs = [leg("put", SHORT, 2.0, 90),
            leg("call", SHORT, 1.5, 105), leg("call", LONG, 0.5, 110)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(8700.0)


def test_covered_call_max_loss_is_basis_less_credit_NOT_basis_less_strike_less_credit():
    # 100 shares at 100, short 105 call for 2. The strike caps the UPSIDE; the downside runs
    # to zero. Intuition says (100 - 105 - 2); arithmetic says (100 - 2) * 100.
    legs = [PayoffLeg(kind="stock", side=LONG, premium=100.0),
            leg("call", SHORT, 2.0, 105)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(9800.0)


def test_protective_put_max_loss_is_basis_less_strike_plus_debit():
    legs = [PayoffLeg(kind="stock", side=LONG, premium=100.0),
            leg("put", LONG, 3.0, 95)]
    r = max_loss(legs)
    assert r.state == MEASURED and r.amount == pytest.approx(800.0)


def test_short_straddle_is_unbounded_not_a_large_number():
    legs = [leg("call", SHORT, 4.0, 100), leg("put", SHORT, 3.5, 100)]
    r = max_loss(legs)
    assert r.state == UNBOUNDED
    assert r.amount is None


def test_short_strangle_is_unbounded():
    legs = [leg("call", SHORT, 2.0, 110), leg("put", SHORT, 2.0, 90)]
    assert max_loss(legs).state == UNBOUNDED


def test_put_ratio_spread_is_bounded_because_the_underlying_stops_at_zero():
    # Long one 100 put, short two 90 puts. Net short one put below 90 -> bounded at S=0.
    legs = [leg("put", LONG, 6.0, 100), leg("put", SHORT, 2.5, 90, ratio=2)]
    r = max_loss(legs)
    assert r.state == MEASURED


def test_bad_leg_is_unmeasurable_and_says_why():
    r = max_loss([leg("call", LONG, 5.0, None)])
    assert r.state == UNMEASURABLE and "strike" in r.reason


def test_a_structure_that_cannot_lose_is_unmeasurable_not_free_money():
    # Long 100 call for 1.0 AND short 100 call for 4.0 -> a 3.0 credit for zero risk.
    # That is an arbitrage, i.e. a stale or crossed quote.
    legs = [leg("call", LONG, 1.0, 100), leg("call", SHORT, 4.0, 100)]
    r = max_loss(legs)
    assert r.state == UNMEASURABLE
    assert "arbitrage" in r.reason


def test_measured_amount_is_always_positive():
    r = max_loss([leg("call", LONG, 5.0, 100)])
    assert r.amount > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_max_loss.py -q`
Expected: `ImportError: cannot import name 'MEASURED' from 'ba2_common.core.option_payoff'`

- [ ] **Step 3: Write the implementation**

Append to `packages/common/ba2_common/core/option_payoff.py`:

```python
#: The three states of a max-loss answer. Strings rather than an Enum because they are compared,
#: logged and asserted on far more often than they are iterated.
MEASURED = "MEASURED"
UNBOUNDED = "UNBOUNDED"
UNMEASURABLE = "UNMEASURABLE"


@dataclass(frozen=True)
class MaxLossResult:
    """A max-loss answer, in three explicitly named states.

    DELIBERATELY NOT AN ``Optional[float]``. This codebase's recurring defect class is "unknown
    reads as zero", and here that would be doubly bad: a max loss of ``0.0`` makes a structure
    look free to open, and an unbounded structure collapsed to ``0.0`` makes the single most
    dangerous position on the board look like the cheapest. Three states, each named, so a
    caller cannot handle one by accident.

    ``amount`` is set iff ``state == MEASURED`` and is POSITIVE dollars of loss.
    ``reason`` is set iff ``state == UNMEASURABLE``.
    """

    state: str
    amount: Optional[float] = None
    reason: Optional[str] = None


def critical_points(legs: Sequence[PayoffLeg]) -> List[float]:
    """The underlying prices at which the payoff slope can change: zero and every strike.

    The payoff is piecewise linear with kinks ONLY at strikes, so the minimum over the bounded
    region ``[0, highest strike]`` is always attained at one of these points. This makes the
    max-loss search EXACT rather than a sample of the curve.
    """
    points = {0.0}
    for leg in legs:
        if leg.kind in _OPTION_KINDS:
            points.add(float(leg.strike))
    return sorted(points)


def upside_slope(legs: Sequence[PayoffLeg]) -> float:
    """d(payoff)/d(spot) ABOVE every strike, in dollars per dollar of underlying.

    Only calls and stock have intrinsic value up there; every put is worthless. A NEGATIVE slope
    means the payoff falls without limit as the underlying rises — the one and only way an
    option structure's loss can be unbounded.

    The downside needs no equivalent test: below every strike, each short put loses at most its
    own strike, so ``payoff_at(legs, 0)`` is always finite. Losses are unbounded above, never
    below.
    """
    slope = 0.0
    for leg in legs:
        if leg.kind in ("call", "stock"):
            slope += _sign(leg.side) * leg.ratio * leg.multiplier
    return slope


def max_loss(legs: Sequence[PayoffLeg]) -> MaxLossResult:
    """The worst-case loss of ONE structure unit at expiry, as POSITIVE dollars.

    See ``MaxLossResult`` for why this is not a float.
    """
    problem = validate_legs(legs)
    if problem is not None:
        return MaxLossResult(UNMEASURABLE, reason=problem)

    if upside_slope(legs) < 0:
        return MaxLossResult(UNBOUNDED)

    worst = min(payoff_at(legs, s) for s in critical_points(legs))

    # A structure that cannot lose at ANY underlying price is an arbitrage. In practice that
    # never means free money — it means a stale, crossed or mis-signed quote. Reporting it as a
    # max loss of 0 would make it the cheapest thing on the board and the triage would take it
    # every time, at whatever size the budget allows.
    if worst >= 0:
        return MaxLossResult(
            UNMEASURABLE,
            reason=(f"structure shows no losing outcome (worst payoff {worst:.2f} at expiry); "
                    f"a risk-free structure is an arbitrage, so this is a stale or crossed "
                    f"quote rather than free money"))

    return MaxLossResult(MEASURED, amount=-worst)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_max_loss.py packages/common/tests/test_option_payoff.py -q`
Expected: `27 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_payoff.py packages/common/tests/test_option_max_loss.py
git commit -m "feat(options): three-state max_loss derived from the payoff curve"
```

---

### Task 4: Promote `_target_strike` to public

**Files:**
- Modify: `packages/common/ba2_common/core/option_selector.py:243-251` and its one call site at `:280`
- Test: `packages/common/tests/test_option_target_strike_public.py`

This is a prerequisite for Task 6: the selection policy must compute the same target strike the
existing selector does, and duplicating the arithmetic is how the two come to disagree.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_target_strike_public.py`:

```python
"""``target_strike`` is public so the selection policy reuses it instead of duplicating it.

WHY IT MATTERS: the policy's "distance from the box centre" feature must measure distance to the
SAME strike the existing selector aims at. Two copies of this arithmetic would drift, and the
symptom would be the new policy picking a different contract at DEFAULT weights — silently
breaking the no-op guarantee that lets us ship this without changing any backtest.
"""
from ba2_common.core.option_selector import target_strike
from ba2_common.core.types import OptionRight


def test_percent_otm_call_is_above_spot():
    assert target_strike("percent_otm", 10.0, 100.0, None, OptionRight.CALL) == 110.0


def test_percent_otm_put_is_below_spot():
    assert target_strike("percent_otm", 10.0, 100.0, None, OptionRight.PUT) == 90.0


def test_consensus_target_returns_the_target_price():
    assert target_strike("consensus_target", None, 100.0, 123.0, OptionRight.CALL) == 123.0


def test_delta_method_has_no_target_strike():
    assert target_strike("delta", 0.3, 100.0, None, OptionRight.CALL) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_target_strike_public.py -q`
Expected: `ImportError: cannot import name 'target_strike'`

- [ ] **Step 3: Rename the function and update its call site**

In `packages/common/ba2_common/core/option_selector.py`, change the definition at line 243 from
`def _target_strike(` to:

```python
def target_strike(method, strike_param, spot, target_price, option_type) -> Optional[float]:
    """The strike a non-delta method aims at, or None for the delta method.

    PUBLIC because ``option_selection_policy`` needs the identical number to measure a
    candidate's distance from the box centre. A second copy of this arithmetic would drift, and
    the first symptom would be the new policy picking a different contract at DEFAULT weights —
    breaking the no-op guarantee that lets the policy ship without changing any backtest.
    """
    if method == "percent_otm":
        if option_type == OptionRight.CALL:
            return spot * (1 + strike_param / 100.0)
        return spot * (1 - strike_param / 100.0)
    if method == "consensus_target":
        # TODO(P2 Task 5): optionally prefer strike <= target for calls / >= target for puts
        # (currently nearest-absolute).
        return target_price
    return None
```

Then in `_pick_by` (line ~280) change:

```python
    ts = _target_strike(method, strike_param, spot, target_price, option_type)
```

to:

```python
    ts = target_strike(method, strike_param, spot, target_price, option_type)
```

- [ ] **Step 4: Run tests to verify nothing regressed**

Run: `./venv/bin/python -m pytest packages/common/tests/ -q -k "option"`
Expected: all pass, including the four new ones. No test should reference `_target_strike`; if
one does, the grep in the next step will find it.

Run: `grep -rn "_target_strike" packages/ ba2_trade_platform/ testplatform/ --include=*.py`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_selector.py packages/common/tests/test_option_target_strike_public.py
git commit -m "refactor(options): make target_strike public for reuse by the selection policy"
```

---

### Task 5: Selection policy — weights, context and features

**Files:**
- Create: `packages/common/ba2_common/core/option_selection_policy.py`
- Test: `packages/common/tests/test_option_selection_features.py`

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_selection_features.py`:

```python
"""Feature normalisation is what makes contract selection work ACROSS symbols.

THE PROBLEM IT SOLVES. An absolute threshold on premium, volume or spread is meaningless across a
$15 stock and a $900 stock — the same "$2.00 of premium" is rich on one and negligible on the
other. Every feature here is therefore min-max normalised WITHIN the candidate set, i.e. it
measures a contract's RANK among its own peers on the chain in front of you, which is scale-free.

FAIL-CLOSED. A candidate missing a feature scores worst on it (0.0), never best — the same
direction as ``option_selector.passes_liquidity``, which refuses a contract whose liquidity is
unknown while its peers report theirs.
"""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, feature_matrix
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
EXPIRY = date(2024, 4, 5)


def c(strike, *, delta=0.30, bid=1.0, ask=1.2, iv=0.25, volume=100, expiry=EXPIRY):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}", underlying="X", option_type=OptionRight.CALL,
        strike=float(strike), expiry=expiry, bid=bid, ask=ask, last=None,
        implied_volatility=iv, delta=delta, volume=volume)


def ctx(**kw):
    base = dict(strike_method="delta", target=0.30, spot=100.0, option_type=OptionRight.CALL,
                today=TODAY)
    base.update(kw)
    return PolicyContext(**base)


def test_box_center_feature_is_highest_for_the_closest_candidate():
    cands = [c(100, delta=0.30), c(105, delta=0.40), c(110, delta=0.50)]
    m = feature_matrix(cands, ctx())
    assert m["box_center"][0] > m["box_center"][1] > m["box_center"][2]


def test_rvol_feature_is_highest_for_the_busiest_contract():
    cands = [c(100, volume=10), c(105, volume=500)]
    m = feature_matrix(cands, ctx())
    assert m["rvol"][1] > m["rvol"][0]


def test_spread_feature_is_highest_for_the_tightest_quote():
    tight = c(100, bid=1.00, ask=1.02)
    wide = c(105, bid=1.00, ask=2.00)
    m = feature_matrix([tight, wide], ctx())
    assert m["spread"][0] > m["spread"][1]


def test_iv_feature_is_highest_for_the_most_expensive_vol():
    m = feature_matrix([c(100, iv=0.20), c(105, iv=0.60)], ctx())
    assert m["iv"][1] > m["iv"][0]


def test_missing_value_scores_worst_not_best():
    # Three candidates, not two: with only one PRESENT value the range is degenerate and every
    # feature legitimately flattens to 0.0, which would let this test pass for the wrong reason.
    m = feature_matrix([c(100, volume=None), c(105, volume=10), c(110, volume=500)], ctx())
    assert m["rvol"][0] == 0.0     # missing -> worst
    assert m["rvol"][1] == 0.0     # lowest present -> also 0.0, but by measurement
    assert m["rvol"][2] == 1.0     # highest present -> best


def test_all_equal_values_contribute_nothing_rather_than_dividing_by_zero():
    m = feature_matrix([c(100, volume=7), c(105, volume=7)], ctx())
    assert m["rvol"] == [0.0, 0.0]


def test_single_candidate_does_not_crash():
    m = feature_matrix([c(100)], ctx())
    assert set(m) == {"box_center", "premium", "iv", "rvol", "spread"}
    assert all(len(v) == 1 for v in m.values())


def test_default_policy_weights_only_the_box_center():
    p = SelectionPolicy()
    assert p.w_box_center == 1.0
    assert (p.w_premium, p.w_iv, p.w_rvol, p.w_spread) == (0.0, 0.0, 0.0, 0.0)
    assert p.is_default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_selection_features.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'ba2_common.core.option_selection_policy'`

- [ ] **Step 3: Write the implementation**

Create `packages/common/ba2_common/core/option_selection_policy.py`:

```python
"""Choosing ONE contract from the candidates that fall inside a rule's box. Pure.

THE DIVISION OF LABOUR. A rule states a BOX — "a put, delta 0.10 to 0.25, one month". This module
chooses inside it. The rule owns the strategy's shape; the policy owns which contract best
expresses it today, and the policy's weights are GENES, so the GA searches the choosing rather
than inheriting somebody's guess at it.

WHY EVERY FEATURE IS NORMALISED WITHIN THE CANDIDATE SET. Option prices, spreads and volumes have
no standard range across symbols: "$2.00 of premium" is rich on a $15 stock and negligible on a
$900 one, so an absolute threshold optimised on one universe is meaningless on another. A
contract's RANK among the peers on its own chain is scale-free, and that is what these features
measure.

THE DEFAULT IS A PROVABLE NO-OP. With only ``w_box_center`` at its pinned 1.0, ``pick`` selects
exactly the contract ``option_selector._pick_by`` selects, tie-breaks included. That is what lets
this ship without moving a single existing backtest — see
``tests/test_option_selection_policy_noop.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

from ba2_common.core.option_selector import target_strike
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

#: The score a candidate gets on a feature it cannot answer. Features are MAXIMISED, so 0.0 is
#: the worst possible value: unknown never beats known. Same direction as
#: ``option_selector.passes_liquidity``, which fails closed on a missing liquidity field.
_WORST = 0.0

#: Calendar days per year, matching ``option_economics.DAYS_PER_YEAR``.
_DAYS_PER_YEAR = 365.0

FEATURE_NAMES = ("box_center", "premium", "iv", "rvol", "spread")


@dataclass(frozen=True)
class SelectionPolicy:
    """The weights that decide which contract in the box wins. Each non-pinned weight is a gene.

    ``w_box_center`` IS PINNED AT 1.0 AND IS NOT A GENE. Scaling all five weights by the same
    factor changes no ranking, so leaving it free would hand the GA a degenerate direction to
    wander in — budget spent exploring a difference that is not one.

    ``w_iv`` IS THE ONE SIGNED WEIGHT. Premium richness, relative volume and quote tightness have
    an unambiguous good direction. Implied volatility does not: premium SELLERS want rich vol and
    BUYERS want cheap vol, and which is right for a given strategy is exactly the sort of
    question the search should settle rather than inherit.
    """

    w_box_center: float = 1.0
    w_premium: float = 0.0
    w_iv: float = 0.0
    w_rvol: float = 0.0
    w_spread: float = 0.0

    @property
    def is_default(self) -> bool:
        """True when this policy reproduces the pre-policy selector exactly."""
        return (self.w_box_center == 1.0 and self.w_premium == 0.0 and self.w_iv == 0.0
                and self.w_rvol == 0.0 and self.w_spread == 0.0)


@dataclass(frozen=True)
class PolicyContext:
    """Everything about the request that is not the candidate list.

    ``target`` is the box CENTRE in the strike method's own units — a delta for ``delta``, a
    percentage for ``percent_otm``, unused for ``consensus_target``.

    THE BOX FILTER APPLIES ONLY WHEN ``box_min < box_max``. A degenerate or absent box means
    "aim at ``target``, filter nothing", which is what preserves compatibility with the existing
    single-``strike_param`` rules: filtering a chain down to contracts whose delta is exactly
    0.30 would leave nothing at all.
    """

    strike_method: str                      # "delta" | "percent_otm" | "consensus_target"
    target: Optional[float] = None
    box_min: Optional[float] = None
    box_max: Optional[float] = None
    spot: Optional[float] = None
    target_price: Optional[float] = None    # for consensus_target
    option_type: Optional[OptionRight] = None
    today: Optional[date] = None            # for the premium feature's annualisation


def _mark(c: OptionContract) -> Optional[float]:
    """The contract's price: mid when both sides quote, else last. None when neither exists."""
    return c.mid if c.mid is not None else c.last


def distance_from_target(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """How far this contract is from the box centre, in the strike method's own units.

    None when the contract cannot be measured at all (a delta method against a contract with no
    delta). ``pick`` excludes those candidates outright rather than scoring them worst — see its
    docstring for why that exactness matters.
    """
    if ctx.strike_method == "delta":
        if c.delta is None or ctx.target is None:
            return None
        return abs(abs(c.delta) - abs(ctx.target))
    ts = target_strike(ctx.strike_method, ctx.target, ctx.spot, ctx.target_price,
                       ctx.option_type)
    if ts is None:
        return None
    return abs(c.strike - ts)


def box_value(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """The quantity the box's bounds are expressed in, for this contract.

    ``delta`` -> absolute delta. ``percent_otm`` -> how far out of the money, in percent.
    ``consensus_target`` has no parameter, so it has no box and this is never consulted.
    """
    if ctx.strike_method == "delta":
        return None if c.delta is None else abs(c.delta)
    if ctx.strike_method == "percent_otm":
        if not ctx.spot:
            return None
        if ctx.option_type == OptionRight.PUT:
            return (1.0 - c.strike / ctx.spot) * 100.0
        return (c.strike / ctx.spot - 1.0) * 100.0
    return None


def _premium_richness(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """Annualised premium as a fraction of the strike: ``(mark / strike) * 365/dte``.

    A per-contract ratio, so it is comparable between a $15 and a $900 underlying before
    normalisation even begins. None when it cannot be computed — notably a same-day expiry,
    which is not a division opportunity (``365/0`` is infinite and infinity beats every peer).
    """
    mark = _mark(c)
    if mark is None or not c.strike or ctx.today is None:
        return None
    dte = (c.expiry - ctx.today).days
    if dte <= 0:
        return None
    return (mark / c.strike) * (_DAYS_PER_YEAR / dte)


def _normalise(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Min-max each value onto [0, 1]. ``None`` stays ``None`` for the caller to fail closed.

    A DEGENERATE RANGE (every present value equal, including the single-candidate case) maps
    everything to 0.0 rather than dividing by zero. That is not a cop-out: a feature that cannot
    distinguish the candidates must not contribute to the ranking, and an equal contribution to
    all of them is exactly no contribution.
    """
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    if hi - lo <= 0:
        return [None if v is None else 0.0 for v in values]
    return [None if v is None else (v - lo) / (hi - lo) for v in values]


def _maximise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a higher-is-better raw feature, failing closed on missing values."""
    return [_WORST if v is None else v for v in _normalise(values)]


def _minimise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a lower-is-better raw feature (distance, spread) and invert it.

    A missing value lands on ``_WORST`` AFTER inversion, not before — otherwise "unknown" would
    invert into the best possible score, which is the fail-OPEN this codebase keeps having to
    remove.
    """
    return [_WORST if v is None else 1.0 - v for v in _normalise(values)]


def feature_matrix(candidates: Sequence[OptionContract],
                   ctx: PolicyContext) -> Dict[str, List[float]]:
    """Every feature for every candidate, each normalised to [0, 1] and oriented so that
    HIGHER IS BETTER. Keys are ``FEATURE_NAMES``; each value is a list parallel to
    ``candidates``."""
    return {
        "box_center": _minimise([distance_from_target(c, ctx) for c in candidates]),
        "premium": _maximise([_premium_richness(c, ctx) for c in candidates]),
        "iv": _maximise([c.implied_volatility for c in candidates]),
        "rvol": _maximise([None if c.volume is None else float(c.volume)
                           for c in candidates]),
        "spread": _minimise([c.spread_pct for c in candidates]),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_selection_features.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_selection_policy.py packages/common/tests/test_option_selection_features.py
git commit -m "feat(options): selection policy weights and chain-relative feature matrix"
```

---

### Task 6: Selection policy — `pick()` and the no-op proof

**Files:**
- Modify: `packages/common/ba2_common/core/option_selection_policy.py` (append)
- Test: `packages/common/tests/test_option_selection_pick.py`
- Test: `packages/common/tests/test_option_selection_policy_noop.py`

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_option_selection_pick.py`:

```python
"""``pick`` applies the box filter, the weights and the tie-break, in that order."""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, pick
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
NEAR = date(2024, 4, 5)
FAR = date(2024, 4, 12)


def c(strike, *, delta=0.30, bid=1.0, ask=1.2, iv=0.25, volume=100, expiry=NEAR):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{expiry:%y%m%d}", underlying="X",
        option_type=OptionRight.CALL, strike=float(strike), expiry=expiry, bid=bid, ask=ask,
        last=None, implied_volatility=iv, delta=delta, volume=volume)


def ctx(**kw):
    base = dict(strike_method="delta", target=0.30, spot=100.0, option_type=OptionRight.CALL,
                today=TODAY)
    base.update(kw)
    return PolicyContext(**base)


def test_empty_candidates_returns_none():
    assert pick([], ctx(), SelectionPolicy()) is None


def test_default_policy_picks_the_delta_closest_to_target():
    cands = [c(105, delta=0.45), c(100, delta=0.31), c(110, delta=0.60)]
    assert pick(cands, ctx(), SelectionPolicy()).strike == 100.0


def test_box_filter_excludes_candidates_outside_the_band():
    cands = [c(100, delta=0.55), c(105, delta=0.20)]
    chosen = pick(cands, ctx(target=0.175, box_min=0.10, box_max=0.25), SelectionPolicy())
    assert chosen.strike == 105.0


def test_degenerate_box_filters_nothing():
    # box_min == box_max is a point target, not a filter. Filtering to delta == 0.30 exactly
    # would leave nothing and the rule would silently stop trading.
    cands = [c(100, delta=0.31), c(105, delta=0.45)]
    chosen = pick(cands, ctx(target=0.30, box_min=0.30, box_max=0.30), SelectionPolicy())
    assert chosen.strike == 100.0


def test_delta_method_excludes_contracts_with_no_delta():
    cands = [c(100, delta=None), c(105, delta=0.90)]
    assert pick(cands, ctx(), SelectionPolicy()).strike == 105.0


def test_delta_method_returns_none_when_no_candidate_has_a_delta():
    assert pick([c(100, delta=None)], ctx(), SelectionPolicy()) is None


def test_delta_method_with_no_target_selects_nothing_rather_than_guessing():
    # option_selector's docstring says a None strike_param under the delta method is a
    # misconfigured ruleset and _pick_by raises on it. Scoring instead would make every
    # distance unmeasurable, tie every candidate, and quietly hand back the lowest strike --
    # a real contract chosen for no reason. An empty result is a refusal the caller can report.
    assert pick([c(100, delta=0.30)], ctx(target=None), SelectionPolicy()) is None


def test_a_weight_can_override_the_box_center_preference():
    # 100 is nearer the 0.30 target; 105 pays far more premium. With premium weighted heavily
    # the policy must prefer 105 — this is the whole point of the mechanism.
    cands = [c(100, delta=0.30, bid=0.10, ask=0.12), c(105, delta=0.40, bid=5.0, ask=5.2)]
    chosen = pick(cands, ctx(), SelectionPolicy(w_premium=5.0))
    assert chosen.strike == 105.0


def test_ties_break_to_the_lowest_strike_then_the_earliest_expiry():
    # Identical on every feature; only strike and expiry differ.
    cands = [c(110, expiry=FAR), c(100, expiry=FAR), c(100, expiry=NEAR)]
    chosen = pick(cands, ctx(strike_method="percent_otm", target=0.0), SelectionPolicy())
    assert (chosen.strike, chosen.expiry) == (100.0, NEAR)
```

Create `packages/common/tests/test_option_selection_policy_noop.py`:

```python
"""THE NO-OP GUARANTEE: at default weights, ``pick`` selects what ``_pick_by`` selects.

WHY THIS IS THE MOST IMPORTANT TEST IN PHASE 1. The selection policy is being introduced into a
path that fourteen live rules and every option backtest already run through. It ships safely only
if turning it on WITHOUT configuring any weight changes nothing at all — not "changes little",
not "changes only in ties". This test is the evidence for that claim, and it must cover the
awkward cases specifically: exact ties, duplicate strikes across two expiries, and contracts with
no delta (which ``_pick_by`` filters out rather than ranking last).
"""
from datetime import date

import pytest

from ba2_common.core.option_selection_policy import PolicyContext, SelectionPolicy, pick
from ba2_common.core.option_selector import _pick_by
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 3, 4)
NEAR = date(2024, 4, 5)
FAR = date(2024, 4, 12)


def c(strike, *, delta=0.30, expiry=NEAR, right=OptionRight.CALL, bid=1.0, ask=1.2):
    return OptionContract(
        symbol=f"X{int(strike * 1000):08d}{expiry:%y%m%d}", underlying="X",
        option_type=right, strike=float(strike), expiry=expiry, bid=bid, ask=ask, last=None,
        implied_volatility=0.25, delta=delta, volume=100)


CHAINS = {
    "spread_of_deltas": [c(95, delta=0.62), c(100, delta=0.48), c(105, delta=0.31),
                         c(110, delta=0.19)],
    "exact_tie_on_distance": [c(95, delta=0.20), c(105, delta=0.40)],
    "duplicate_strike_two_expiries": [c(100, delta=0.30, expiry=FAR),
                                      c(100, delta=0.30, expiry=NEAR)],
    "all_identical": [c(100, delta=0.30), c(105, delta=0.30), c(110, delta=0.30)],
    "some_missing_delta": [c(100, delta=None), c(105, delta=0.33), c(110, delta=None)],
    "single_candidate": [c(100, delta=0.30)],
}


@pytest.mark.parametrize("name", sorted(CHAINS))
@pytest.mark.parametrize("target", [0.15, 0.30, 0.50])
def test_delta_method_matches_pick_by_at_default_weights(name, target):
    cands = CHAINS[name]
    legacy = _pick_by("delta", cands, target, 100.0, None, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="delta", target=target, spot=100.0,
                                option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


@pytest.mark.parametrize("name", sorted(CHAINS))
@pytest.mark.parametrize("param", [0.0, 5.0, 12.0])
def test_percent_otm_matches_pick_by_at_default_weights(name, param):
    cands = CHAINS[name]
    legacy = _pick_by("percent_otm", cands, param, 100.0, None, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="percent_otm", target=param, spot=100.0,
                                option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_consensus_target_matches_pick_by_at_default_weights(name):
    cands = CHAINS[name]
    legacy = _pick_by("consensus_target", cands, None, 100.0, 107.0, OptionRight.CALL)
    policy = pick(cands,
                  PolicyContext(strike_method="consensus_target", target=None, spot=100.0,
                                target_price=107.0, option_type=OptionRight.CALL, today=TODAY),
                  SelectionPolicy())
    assert policy is legacy


def test_chain_order_does_not_change_the_default_pick():
    cands = CHAINS["duplicate_strike_two_expiries"]
    ctx = PolicyContext(strike_method="delta", target=0.30, spot=100.0,
                        option_type=OptionRight.CALL, today=TODAY)
    forward = pick(list(cands), ctx, SelectionPolicy())
    backward = pick(list(reversed(cands)), ctx, SelectionPolicy())
    assert forward.expiry == backward.expiry == NEAR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_selection_pick.py packages/common/tests/test_option_selection_policy_noop.py -q`
Expected: `ImportError: cannot import name 'pick' from 'ba2_common.core.option_selection_policy'`

- [ ] **Step 3: Write the implementation**

Append to `packages/common/ba2_common/core/option_selection_policy.py`:

```python
def _in_box(c: OptionContract, ctx: PolicyContext) -> bool:
    """Is this contract inside the rule's box?

    A box exists only when ``box_min < box_max``. Absent or degenerate bounds mean "aim at the
    target, filter nothing" — see ``PolicyContext``. A contract whose box quantity cannot be
    measured fails CLOSED, because "I don't know where this contract sits" is not a reason to
    admit it to a band the rule deliberately narrowed.
    """
    if ctx.box_min is None or ctx.box_max is None or ctx.box_min >= ctx.box_max:
        return True
    v = box_value(c, ctx)
    if v is None:
        return False
    return ctx.box_min <= v <= ctx.box_max


def eligible(candidates: Sequence[OptionContract],
             ctx: PolicyContext) -> List[OptionContract]:
    """The candidates the policy is allowed to choose between.

    Two filters, and the ORDER OF THE FIRST ONE IS LOAD-BEARING. Under the ``delta`` method a
    contract with no delta is EXCLUDED, not scored worst. ``option_selector._pick_by`` does
    exactly that (``usable = [c for c in cands if c.delta is not None]``, returning None if
    none remain), and scoring them worst instead would differ from it whenever every candidate
    lacks a delta: ``_pick_by`` returns None, a worst-score policy would return an arbitrary
    contract with no measurable delta at all. That is a live selection change, so it is not
    allowed.
    """
    out = list(candidates)
    if ctx.strike_method == "delta":
        # A delta method with no target is a misconfigured ruleset, and ``_pick_by`` raises on
        # it (``abs(None)``). Scoring it instead would make every distance unmeasurable, tie
        # every candidate, and hand back the lowest strike — a real contract chosen for no
        # reason. Selecting nothing is a refusal the caller can report; a wrong pick is not.
        if ctx.target is None:
            return []
        out = [c for c in out if c.delta is not None]
    return [c for c in out if _in_box(c, ctx)]


def score_all(candidates: Sequence[OptionContract], ctx: PolicyContext,
              policy: SelectionPolicy) -> List[float]:
    """The weighted score of each candidate. Higher wins."""
    m = feature_matrix(candidates, ctx)
    weights = {"box_center": policy.w_box_center, "premium": policy.w_premium,
               "iv": policy.w_iv, "rvol": policy.w_rvol, "spread": policy.w_spread}
    return [sum(weights[name] * m[name][i] for name in FEATURE_NAMES)
            for i in range(len(candidates))]


def pick(candidates: Sequence[OptionContract], ctx: PolicyContext,
         policy: SelectionPolicy) -> Optional[OptionContract]:
    """The single best contract in the box, or None when the box is empty.

    THE TIE-BREAK IS THE EXISTING ONE. Ties resolve to the LOWEST STRIKE and then the EARLIEST
    EXPIRY, matching ``option_selector._tie``. That ordering is not cosmetic: the historical
    cache lists the same strike under more than one in-window expiry, so candidates routinely
    tie on the distance metric, and before the expiry term existed ``min()`` resolved them by
    input-list order — reversing the chain changed which contract every structure pinned itself
    to.

    Implemented as ``min`` over ``(-score, strike, expiry)`` rather than ``max`` over score,
    because that makes the two tie-break terms read in their natural ascending direction and
    keeps them identical to the legacy key.
    """
    cands = eligible(candidates, ctx)
    if not cands:
        return None
    scores = score_all(cands, ctx, policy)
    best = min(range(len(cands)),
               key=lambda i: (-scores[i], cands[i].strike, cands[i].expiry))
    return cands[best]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_selection_pick.py packages/common/tests/test_option_selection_policy_noop.py -q`
Expected: `9 passed` in the first file and `43 passed` in the second (6 chains × 3 delta targets = 18, 6 × 3 percent_otm params = 18, 6 consensus_target, plus the chain-order test).

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_selection_policy.py packages/common/tests/test_option_selection_pick.py packages/common/tests/test_option_selection_policy_noop.py
git commit -m "feat(options): policy pick() with a proven no-op at default weights"
```

---

### Task 7: Request and resolved-structure value objects

**Files:**
- Create: `packages/common/ba2_common/core/option_request.py`
- Test: `packages/common/tests/test_option_request.py`

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_option_request.py`:

```python
"""The value objects that carry a proposal from a rule action to the option risk manager.

WHY TEST DATA CLASSES AT ALL. Two things here are load-bearing rather than incidental: the
refusal PHRASES are a stable API (logs, the UI and future tests grep for them, so a typo or a
rename is a silent break), and the request must be FROZEN so that resolving one cannot mutate the
proposal another candidate is still being compared against.
"""
import dataclasses

import pytest

from ba2_common.core.option_request import (
    BUDGET_EXHAUSTED_REFUSAL, BUYING_POWER_REFUSAL, CONFIDENCE_UNMEASURABLE_REFUSAL,
    EMPTY_BOX_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL, NEGATIVE_EXPECTANCY_REFUSAL,
    REFUSAL_PHRASES, TARGET_UNMEASURABLE_REFUSAL, UNDEFINED_RISK_REFUSAL,
    OptionStructureRequest, StructureRefusal)


def a_request(**kw):
    base = dict(structure="buy_call", symbol="AAPL", expert_recommendation_id=1)
    base.update(kw)
    return OptionStructureRequest(**base)


def test_request_is_frozen():
    req = a_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.symbol = "MSFT"


def test_request_defaults_leave_every_optional_boundary_unset():
    req = a_request()
    assert req.term is None and req.dte_min is None and req.dte_max is None
    assert req.box_min is None and req.box_max is None
    assert req.min_arc is None and req.sizing_pct is None


def test_all_refusal_phrases_are_registered_and_distinct():
    phrases = [UNDEFINED_RISK_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL,
               CONFIDENCE_UNMEASURABLE_REFUSAL, TARGET_UNMEASURABLE_REFUSAL,
               NEGATIVE_EXPECTANCY_REFUSAL, BUYING_POWER_REFUSAL,
               BUDGET_EXHAUSTED_REFUSAL, EMPTY_BOX_REFUSAL]
    assert len(set(phrases)) == len(phrases)
    assert set(phrases) == set(REFUSAL_PHRASES)


def test_refusal_carries_the_request_the_phrase_and_a_detail():
    req = a_request()
    r = StructureRefusal(request=req, phrase=EMPTY_BOX_REFUSAL,
                         detail="min_volume=100 rejected all 42 candidates")
    assert r.request is req
    assert r.phrase == EMPTY_BOX_REFUSAL
    assert "min_volume" in r.detail


def test_refusal_rejects_an_unregistered_phrase():
    # A free-text phrase defeats the point: callers grep for these.
    with pytest.raises(ValueError):
        StructureRefusal(request=a_request(), phrase="it didn't work", detail="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_request.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'ba2_common.core.option_request'`

- [ ] **Step 3: Write the implementation**

Create `packages/common/ba2_common/core/option_request.py`:

```python
"""Value objects carrying an option proposal from a rule action to the option risk manager.

THE SHAPE OF THE HANDOFF. A rule action produces an ``OptionStructureRequest``: boundaries, never
a decision. The risk manager turns each one into either a ``ResolvedStructure`` (a concrete,
priced structure, everything except how many) or a ``StructureRefusal`` (a reason, never a silent
drop). Both outcomes are returned to the caller, because a refusal nobody can see is
indistinguishable from a structure that was never proposed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_terms import OptionTerm
from ba2_common.core.option_types import OptionLeg

# --- refusal phrases ------------------------------------------------------------------------
#
# STABLE, GREPPABLE STRINGS. Logs, the UI and tests all key on these, so they are constants and
# not inline literals — the same discipline as ``option_economics.ARC_FLOOR_REFUSAL`` and
# ``OptionsAccountInterface.ASSIGNMENT_CAPACITY_REFUSAL``, which exist so that three refusals
# with three different remedies can be told apart by a caller that only sees the message.

UNDEFINED_RISK_REFUSAL = "structure carries unbounded loss and undefined risk is not allowed"
MAX_LOSS_UNMEASURABLE_REFUSAL = "maximum loss is unmeasurable"
CONFIDENCE_UNMEASURABLE_REFUSAL = "recommendation carries no confidence"
TARGET_UNMEASURABLE_REFUSAL = "no target price, so payoff at target cannot be evaluated"
NEGATIVE_EXPECTANCY_REFUSAL = "payoff at the recommendation's own target is negative"
BUYING_POWER_REFUSAL = "reserve exceeds available buying power"
BUDGET_EXHAUSTED_REFUSAL = "instrument or book budget exhausted"
EMPTY_BOX_REFUSAL = "no selectable contract in the requested box"

#: Every phrase above. ``StructureRefusal`` validates against this so a free-text reason cannot
#: creep in — the phrases are only useful if they are exhaustive and stable.
REFUSAL_PHRASES = (
    UNDEFINED_RISK_REFUSAL,
    MAX_LOSS_UNMEASURABLE_REFUSAL,
    CONFIDENCE_UNMEASURABLE_REFUSAL,
    TARGET_UNMEASURABLE_REFUSAL,
    NEGATIVE_EXPECTANCY_REFUSAL,
    BUYING_POWER_REFUSAL,
    BUDGET_EXHAUSTED_REFUSAL,
    EMPTY_BOX_REFUSAL,
)


@dataclass(frozen=True)
class OptionStructureRequest:
    """What a rule action PROPOSES. Boundaries only — it never decides a contract or a size.

    Frozen on purpose: the risk manager holds several of these at once while triaging, and
    resolving one must not be able to mutate a proposal another candidate is still measured
    against.

    ``term`` wins over ``dte_min``/``dte_max`` when set. Both survive because fourteen live rules
    still carry the raw window and must keep working unchanged.

    ``resolver`` is the ``_OptionEntryAction`` instance that produced this request. Typed ``Any``
    to keep this module free of a ``TradeActions`` import (which would be circular). Carrying the
    instance is deliberate: it already holds the account, the recommendation and the gates, so
    the risk manager can resolve without reconstructing any of it.
    """

    structure: str                              # ExpertActionType value
    symbol: str
    expert_recommendation_id: int
    term: Optional[OptionTerm] = None
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None
    strike_method: Optional[str] = None         # delta | percent_otm | consensus_target
    box_min: Optional[float] = None
    box_max: Optional[float] = None
    wing_width_pct: Optional[float] = None
    min_open_interest: Optional[int] = None
    max_spread_pct: Optional[float] = None
    min_volume: Optional[int] = None
    min_arc: Optional[float] = None
    sizing_pct: Optional[float] = None
    resolver: Any = None


@dataclass(frozen=True)
class ResolvedStructure:
    """A concrete, priced structure — everything except HOW MANY.

    Quantity is deliberately absent. It is the risk manager's decision and depends on the other
    candidates on the bar, so a resolved structure that already carried one would be making a
    portfolio decision from inside a single symbol's evaluation.
    """

    request: OptionStructureRequest
    legs: List[OptionLeg]                       # what the broker is asked for
    payoff_legs: List[PayoffLeg]                # what the payoff evaluator measures
    limit_price: float
    option_strategy: str                        # reserve-table strategy name
    dte: int
    max_loss_per_contract: float
    reserve_per_contract: float
    payoff_at_target: float
    score: float
    reserve_kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructureRefusal:
    """Why one proposal produced no order. Always returned, never swallowed."""

    request: OptionStructureRequest
    phrase: str
    detail: str

    def __post_init__(self):
        if self.phrase not in REFUSAL_PHRASES:
            raise ValueError(
                f"Unregistered refusal phrase {self.phrase!r}. Callers grep for these, so a "
                f"free-text reason is invisible to them; add it to REFUSAL_PHRASES.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest packages/common/tests/test_option_request.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/option_request.py packages/common/tests/test_option_request.py
git commit -m "feat(options): structure request, resolved structure and refusal value objects"
```

---

### Task 8: Full-suite verification and version bump

**Files:**
- Modify: `testplatform/version.py`

- [ ] **Step 1: Run the whole `packages/common` suite**

Run: `./venv/bin/python -m pytest packages/common/tests/ -q`

Expected: `2272 passed` — the 2169 baseline plus the 103 this plan adds:

| file | tests |
|---|---|
| `test_option_terms.py` | 7 |
| `test_option_payoff.py` | 14 |
| `test_option_max_loss.py` | 13 |
| `test_option_target_strike_public.py` | 4 |
| `test_option_selection_features.py` | 8 |
| `test_option_selection_pick.py` | 9 |
| `test_option_selection_policy_noop.py` | 43 |
| `test_option_request.py` | 5 |

If the baseline differs from 2169, what matters is that it went UP by 103 and that nothing
previously passing now fails.

- [ ] **Step 2: Run the platform suite to confirm the `target_strike` rename broke nothing**

Run: `./venv/bin/python -m pytest tests/ -q`
Expected: `4406 passed`. Task 4 renamed a function; this is the check that no caller outside
`packages/common` referenced it.

Note: this suite is known to fail non-deterministically when run as one big invocation due to
session leakage between test files. If you see failures, re-run the failing files individually
before treating them as real.

- [ ] **Step 3: Run the testplatform backend suite**

Run: `./venv/bin/python -m pytest testplatform/backend -q`
Expected: `3194 passed, 1 failed`. The single failure is the known Windows-only
`test_worker_server.py::test_logs_rejects_path_traversal`. Any OTHER failure is real.

- [ ] **Step 4: Bump the test-platform version**

This phase changed `packages/`, which per `CLAUDE.md` requires bumping `TEST_APP_VERSION` (not
`APP_VERSION`) — the distributed GA workers decide whether to self-update by comparing that string
alone, so a `packages/` change that skips the bump leaves workers running different `ba2_common`
code from the master.

In `testplatform/version.py`, increment the build number by 1 (e.g. `2026.08.0024` → `2026.08.0025`;
read the current value first rather than assuming).

- [ ] **Step 5: Commit and push**

```bash
git add testplatform/version.py
git commit -m "chore: bump TEST_APP_VERSION for option risk manager phase 1"
git push
```

---

## What this phase does NOT do

Stated explicitly so a reviewer does not report them as gaps:

- **Nothing imports these modules.** They are unused until Phase 2 splits
  `_build_and_submit` into `_resolve`. That is intentional — the whole phase is designed to be
  provably behaviour-neutral.
- **No shims under `ba2_trade_platform/core/`.** Shims exist so in-tree code can keep its old
  import paths; no in-tree code imports these yet. Phase 5 adds one for `OptionTerm` when the
  settings UI needs the dropdown.
- **No `max_profit` or `breakevens`.** Computable from the same curve, but no caller needs them
  (YAGNI). Add them when something does.
- **No genes and no grid wiring.** Phase 5, and blocked on wiring the TastyTrade parquet store
  into `HistoricalOptionsProvider`.

## Self-review notes

Checked against the spec:

- §4.5 (data shapes) → Task 7. `ResolvedStructure` omits the spec's inline comment ordering but
  carries every field.
- §5 (max loss from the payoff curve) → Tasks 2 and 3, including the `UNBOUNDED` and
  `UNMEASURABLE` states and the covered-call correction.
- §6 (terms) → Task 1, including the hard-window property.
- §7 (selection policy) → Tasks 4, 5 and 6, including the pinned `w_box_center`, the signed
  `w_iv`, chain-relative normalisation, fail-closed missing values, the existing tie-break and the
  no-op proof.
- §9 (refusal phrases) → Task 7. The phrases are defined here; the severities in the spec's table
  are a Phase 3 concern (they describe how the risk manager logs them, and it does not exist yet).
- §8, §10, §11 → Phases 2 through 5. Not in scope.
