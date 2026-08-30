# Option Selection Modes, Budget-Aware Picking, and Max-Loss Exits — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan
> task-by-task.

**Goal:** add profit/risk-reward contract selection, a budget ceiling the picker respects, and a
stop expressed as a percentage of max loss — then GA-wire all seven selection weights.

**Architecture:** everything derives from one new pure unit, `option_payoff.max_profit()`, the
mirror of the existing `max_loss()`. Two new `SelectionPolicy` weights consume it; a
`max_loss_ceiling` filter keeps the picker inside budget; a new exit condition reads the max loss
already persisted at submit. Phases match the parent design's P1/P3/P5 and each leaves the tree
green.

**Tech stack:** Python 3.11/3.12, pytest, SQLModel. Pure units in
`packages/common/ba2_common/core/`; grid wiring in `testplatform/ba2test_launcher.py`.

**Design:** `docs/superpowers/specs/2026-08-29-option-selection-modes-and-max-loss-design.md`,
which amends `docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`.

---

## READ THIS FIRST — a gap the design did not resolve

`SelectionPolicy.pick()` chooses **one contract from a chain**. `max_profit()` / `max_loss()` are
properties of a **whole structure**. For a single-leg structure (long call, cash-secured put) the
contract *is* the structure and the features are directly computable. For a vertical spread it is
not: the policy picks one leg and the builder derives the wing with `select_wing`, so neither max
profit nor max loss exists at the moment `pick()` runs.

**Resolution: `PolicyContext` gains an optional `structure_fn`.**

```python
structure_fn: Optional[Callable[[OptionContract], Optional[Sequence[PayoffLeg]]]] = None
```

The builder — which is the only thing that knows how to complete its own structure — supplies a
closure turning a candidate contract into the full leg list. The policy stays ignorant of
structure shapes; the builder stays ignorant of scoring. That is the same division of labour the
module docstring already claims ("the rule owns the strategy's shape; the policy owns which
contract best expresses it").

**When `structure_fn is None`, `w_profit` and `w_rr` are INAPPLICABLE, not zero.** They are
reported inert exactly as an `UNBOUNDED` payoff is (design §4). This is what lets the feature ship
before every one of the 17 builders is taught to supply a closure: an untaught builder loses the
feature visibly rather than scoring every candidate identically and letting a GA-tuned weight
silently do nothing.

Cost is O(n) payoff evaluations per pick. `payoff_at` is piecewise-linear over a handful of
critical points, and `score_all` already skips features whose weight is zero, so the default
policy pays nothing.

---

## READ THIS SECOND — an unmeasurable max PROFIT must never narrow the search

**Operator constraint, 2026-08-29:** a structure whose max profit cannot be measured must still be
explored. `UNBOUNDED` profit is the defining property of a long call, not a defect in it, and the
grid exists partly to find out whether long premium pays. Any behaviour that quietly demotes those
structures would answer that question by construction rather than by measurement.

Three rules follow, and every one of them is asserted by a test in this plan:

1. **Never exclude on profit.** `max_loss_ceiling` is the *only* payoff-derived filter. There is
   no max-profit filter, and Task 5 must not grow one.
2. **Inapplicable means inert, never worst.** When the whole column is unrankable the weight
   contributes uniformly, so ranking is untouched. What must never happen is a structure scoring
   `_WORST` on profit *while its peers score real values* — that is demotion wearing the costume
   of fail-closed. This is why `inapplicable_features` asks whether the **column** is unrankable
   rather than whether a candidate is.
3. **Never emit the gene where it cannot apply** (Task 10). A weight the GA can turn up against a
   structure it cannot describe is worse than no weight at all.

**Fail-closed applies to LOSS, not to PROFIT, and the asymmetry is deliberate.** An unmeasurable
loss admitted to a budget can bankrupt the sleeve; an unmeasurable profit costs nothing but a
ranking signal. Symmetry here would be a bug in the safe direction only by accident.

---

## Phase P1 — pure units (behaviour-neutral)

### Task 1: `max_profit()` — the mirror of `max_loss()`

**Files:**
- Modify: `packages/common/ba2_common/core/option_payoff.py`
- Test: `packages/common/tests/test_option_payoff.py`

**Step 1: Write the failing tests.** Append to `test_option_payoff.py`. These four shapes are the
ones that pin the guard order — do not reduce them.

```python
from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, UNMEASURABLE, PayoffLeg, max_profit,
)
from ba2_common.core.types import OrderDirection


def _call(side, premium, strike, ratio=1):
    return PayoffLeg(kind="call", side=side, premium=premium, strike=strike, ratio=ratio)


def _put(side, premium, strike, ratio=1):
    return PayoffLeg(kind="put", side=side, premium=premium, strike=strike, ratio=ratio)


def test_a_long_call_has_unbounded_profit_and_is_not_called_unprofitable():
    """THE GUARD ORDER. A long call's payoff is NON-POSITIVE across [0, K_max] -- it is
    the debit, everywhere below the strike. A 'cannot profit anywhere' test running first
    would report every ordinary long call as UNMEASURABLE, the exact mirror of the bug
    max_loss's own ordering comment describes."""
    result = max_profit([_call(OrderDirection.BUY, 2.50, 100.0)])
    assert result.state == UNBOUNDED
    assert result.reason is None


def test_a_credit_vertical_profits_at_most_its_credit():
    """Short 100c @ 3.00, long 105c @ 1.00 -> net credit 2.00/share = $200/unit."""
    legs = [_call(OrderDirection.SELL, 3.00, 100.0),
            _call(OrderDirection.BUY, 1.00, 105.0)]
    result = max_profit(legs)
    assert result.state == MEASURED
    assert result.amount == pytest.approx(200.0)


def test_a_naked_short_put_profits_at_most_its_credit():
    """Bounded ABOVE (the credit) while unbounded BELOW -- so max_profit is MEASURED on
    the very structure whose max_loss is UNBOUNDED. The two answers are independent."""
    result = max_profit([_put(OrderDirection.SELL, 4.00, 90.0)])
    assert result.state == MEASURED
    assert result.amount == pytest.approx(400.0)


def test_a_debit_spread_bought_above_its_width_cannot_profit():
    """Long 100c @ 6.00, short 105c @ 1.00 = 5.00 debit for a 5.00-wide spread. Best
    outcome is exactly break-even, which is a crossed or stale quote rather than a trade."""
    legs = [_call(OrderDirection.BUY, 6.00, 100.0),
            _call(OrderDirection.SELL, 1.00, 105.0)]
    result = max_profit(legs)
    assert result.state == UNMEASURABLE
    assert "profit" in result.reason.lower()
```

**Step 2: Run and verify they fail.**

```
cd packages/common && python -m pytest tests/test_option_payoff.py -k max_profit -v
```
Expected: `ImportError: cannot import name 'max_profit'`.

**Step 3: Implement.** Add after `max_loss` in `option_payoff.py`:

```python
#: Mirror of MIN_MEASURABLE_LOSS. A structure whose best outcome is under a cent is not a
#: trade with a thin edge; it is a stale or crossed quote. Same magnitude for the same
#: reason -- one cent is far below any real structure's per-unit profit and far above the
#: floating-point dust a credit-equals-width subtraction leaves behind.
MIN_MEASURABLE_PROFIT = 0.01


@dataclass(frozen=True)
class MaxProfitResult:
    """A max-profit answer, in the same three explicitly named states as MaxLossResult.

    A SEPARATE TYPE rather than reusing MaxLossResult, whose docstring promises ``amount``
    is "POSITIVE dollars of LOSS". One dataclass carrying both meanings is how a caller
    ends up sizing a budget off a profit.

    ``amount`` is set iff ``state == MEASURED`` and is POSITIVE dollars of profit.
    """

    state: str
    amount: Optional[float] = None
    reason: Optional[str] = None


def max_profit(legs: Sequence[PayoffLeg]) -> MaxProfitResult:
    """The best-case profit of ONE structure unit at expiry, as POSITIVE dollars.

    The mirror of ``max_loss``: same critical-points scan, ``max`` where that takes ``min``,
    and the SAME NON-NEGOTIABLE GUARD ORDER for the mirrored reason. A long call is
    non-positive across the whole of ``[0, K_max]`` -- it is simply the debit -- so running
    the "cannot profit" test before the slope test would report every ordinary long call as
    UNMEASURABLE, exactly as running the arbitrage test first reports every naked short call
    that way in ``max_loss``.
    """
    problem = validate_legs(legs)
    if problem is not None:
        return MaxProfitResult(UNMEASURABLE, reason=problem)

    if upside_slope(legs) > _SLOPE_EPSILON:
        return MaxProfitResult(UNBOUNDED)

    best = max(payoff_at(legs, s) for s in critical_points(legs))

    if best <= MIN_MEASURABLE_PROFIT:
        return MaxProfitResult(
            UNMEASURABLE,
            reason=(f"structure shows no meaningful profitable outcome (best payoff "
                    f"{best:.4f} at expiry, within {MIN_MEASURABLE_PROFIT} of break-even); "
                    f"a structure that cannot profit is a stale or crossed quote rather "
                    f"than a trade"))

    return MaxProfitResult(MEASURED, amount=best)
```

**Step 4: Run and verify pass.** Then the whole file:
```
cd packages/common && python -m pytest tests/test_option_payoff.py -q
```
Expected: all pass, no existing test touched.

**Step 5: Commit.**
```bash
git add packages/common/ba2_common/core/option_payoff.py packages/common/tests/test_option_payoff.py
git commit -m "feat(options): max_profit() -- the mirror of max_loss, same tri-state and guard order"
```

---

### Task 2: `structure_fn` on `PolicyContext`

**Files:**
- Modify: `packages/common/ba2_common/core/option_selection_policy.py`
- Test: `packages/common/tests/test_option_selection_policy_noop.py`

**Step 1: Write the failing test.**

```python
def test_structure_fn_defaults_to_none_and_changes_no_existing_pick():
    """Adding the field must not perturb any existing selection."""
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30)
    assert ctx.structure_fn is None
```

**Step 2: Run, verify it fails** (`AttributeError`).

**Step 3: Implement.** Add the import and the field to `PolicyContext`:

```python
from typing import Callable, Sequence  # extend the existing typing import
from ba2_common.core.option_payoff import PayoffLeg
```
```python
    #: Turns a candidate contract into the FULL leg list of the structure it would become.
    #: Supplied by the builder, which is the only thing that knows its own shape -- the
    #: policy must not learn structure shapes and the builder must not learn scoring.
    #:
    #: None means the profit/rr features are INAPPLICABLE for this pick, not that they score
    #: zero. See ``feature_matrix``. That is what lets these features ship before all 17
    #: builders supply a closure: an untaught builder loses the feature VISIBLY.
    structure_fn: Optional[Callable[[OptionContract], Optional[Sequence[PayoffLeg]]]] = None
```

**Step 4: Run the whole no-op suite.**
```
cd packages/common && python -m pytest tests/test_option_selection_policy_noop.py -q
```
Expected: all pass — the field is inert.

**Step 5: Commit.**
```bash
git commit -am "feat(options): PolicyContext.structure_fn -- the builder supplies its own shape"
```

---

### Task 3: the `profit` and `rr` features

**Files:**
- Modify: `packages/common/ba2_common/core/option_selection_policy.py`
- Test: `packages/common/tests/test_option_selection_policy_features.py` (new)

**Step 1: Write the failing tests.**

```python
"""The two payoff-derived selection features, and the applicability rule that keeps them
from becoming dead genes."""
import pytest
from datetime import date

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_selection_policy import (
    FEATURE_NAMES, PolicyContext, feature_matrix, inapplicable_features,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderDirection


def _c(strike, bid, ask, delta):
    return OptionContract(symbol=f"X{strike:g}", underlying="X", option_type=OptionRight.CALL,
                          strike=strike, expiry=date(2026, 2, 20), bid=bid, ask=ask,
                          delta=delta)


def _credit_vertical(width):
    """A closure completing each candidate into a credit vertical `width` points wide."""
    def _fn(c):
        return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=c.mid, strike=c.strike),
                PayoffLeg(kind="call", side=OrderDirection.BUY, premium=0.10,
                          strike=c.strike + width)]
    return _fn


def test_profit_and_rr_are_named_features():
    assert "profit" in FEATURE_NAMES and "rr" in FEATURE_NAMES


def test_a_richer_credit_scores_higher_on_profit():
    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, 0.90, 1.10, 0.15)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_credit_vertical(5.0))
    m = feature_matrix(cands, ctx, only=["profit"])
    assert m["profit"][0] > m["profit"][1]


def test_without_a_structure_fn_profit_and_rr_are_inapplicable_not_zero():
    """THE DEAD-GENE GUARD. A weight that silently scores every candidate the same is a
    gene the GA burns budget on and can never move."""
    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, 0.90, 1.10, 0.15)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30)
    assert set(inapplicable_features(cands, ctx)) >= {"profit", "rr"}


def test_an_unbounded_payoff_makes_the_feature_inapplicable():
    """A long call: every candidate reports UNBOUNDED profit, so the column cannot rank."""
    def _long_call(c):
        return [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=c.mid, strike=c.strike)]

    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, 0.90, 1.10, 0.15)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_long_call)
    assert "profit" in inapplicable_features(cands, ctx)
    assert "rr" in inapplicable_features(cands, ctx)


def test_one_unmeasurable_candidate_fails_closed_rather_than_disabling_the_column():
    """Inapplicable is a property of the STRUCTURE SHAPE, not of one bad quote. A single
    candidate the evaluator cannot price scores worst; the feature stays live."""
    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, None, None, 0.15)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_credit_vertical(5.0))
    assert "profit" not in inapplicable_features(cands, ctx)
    m = feature_matrix(cands, ctx, only=["profit"])
    assert m["profit"][1] == 0.0
```

**Step 2: Run, verify failure** (`ImportError` on `inapplicable_features`).

**Step 3: Implement.** Extend `FEATURE_NAMES`, add the two raw extractors and the applicability
helper, and register the builders.

```python
FEATURE_NAMES = ("box_center", "premium", "iv", "rvol", "spread", "profit", "rr")

#: Features whose value is a property of the STRUCTURE, so they need ``structure_fn``.
PAYOFF_FEATURES = ("profit", "rr")


def _payoff_pair(c, ctx):
    """``(max_profit, max_loss)`` in dollars for the structure this candidate becomes.

    Either element is None when its side is not MEASURED -- UNBOUNDED and UNMEASURABLE
    collapse together here because neither yields a number to rank on. The DIFFERENCE
    between them is captured by ``inapplicable_features``, which asks whether the whole
    column is unrankable rather than whether one candidate is.
    """
    from ba2_common.core.option_payoff import MEASURED, max_loss, max_profit
    if ctx.structure_fn is None:
        return None, None
    legs = ctx.structure_fn(c)
    if not legs:
        return None, None
    p, l = max_profit(legs), max_loss(legs)
    return (p.amount if p.state == MEASURED else None,
            l.amount if l.state == MEASURED else None)


def _max_profit_of(c, ctx):
    return _payoff_pair(c, ctx)[0]


def _reward_to_risk(c, ctx):
    """max_profit / max_loss. None unless BOTH sides are MEASURED -- a ratio with an
    unbounded denominator is not a large number, it is not a number."""
    profit, loss = _payoff_pair(c, ctx)
    if profit is None or loss is None or loss <= 0:
        return None
    return profit / loss


def inapplicable_features(candidates, ctx):
    """Features that cannot rank THIS candidate set at all, so their weights are inert.

    Distinct from a missing value on one candidate, which fails closed and scores worst
    (``_maximise``). A feature lands here when the payoff SHAPE denies it a number for
    every candidate -- a long call has unbounded profit whatever strike you choose -- or
    when no ``structure_fn`` was supplied.

    Reported rather than raised, and rather than silently contributing zero. Raising would
    crash a perfectly valid long-call arm over a weight it should ignore; silence would
    make that weight a dead gene. Mirrors ``option_book.RailVerdict.evaluated``, which
    records that ``undefined_risk_max_pct`` is genuinely dead for a debit arm.
    """
    out = []
    for name in PAYOFF_FEATURES:
        raw = ([_max_profit_of(c, ctx) for c in candidates] if name == "profit"
               else [_reward_to_risk(c, ctx) for c in candidates])
        if all(v is None for v in raw):
            out.append(name)
    return tuple(out)
```

Register in `feature_matrix`'s `builders` dict:
```python
        "profit": lambda: _maximise([_max_profit_of(c, ctx) for c in candidates]),
        "rr": lambda: _maximise([_reward_to_risk(c, ctx) for c in candidates]),
```

**Step 4: Run.**
```
cd packages/common && python -m pytest tests/test_option_selection_policy_features.py tests/test_option_selection_policy_noop.py -q
```
Expected: all pass.

**Step 5: Commit.**
```bash
git add packages/common/ba2_common/core/option_selection_policy.py packages/common/tests/test_option_selection_policy_features.py
git commit -m "feat(options): profit and risk-reward selection features, with inapplicability reported not raised"
```

---

### Task 4: the two weights, and the preserved no-op

**Files:**
- Modify: `packages/common/ba2_common/core/option_selection_policy.py`
- Test: `packages/common/tests/test_option_selection_policy_noop.py`

**Step 1: Write the failing tests.**

```python
def test_the_two_new_weights_default_to_zero_and_keep_is_default_true():
    assert SelectionPolicy().w_profit == 0.0
    assert SelectionPolicy().w_rr == 0.0
    assert SelectionPolicy().is_default


def test_a_policy_with_only_w_profit_set_is_not_default():
    assert not SelectionPolicy(w_profit=1.0).is_default
```

**Step 2: Run, verify failure.**

**Step 3: Implement.** Add to `SelectionPolicy` (after `w_spread`):
```python
    w_profit: float = 0.0
    w_rr: float = 0.0
```
Extend `is_default`:
```python
        return (self.w_box_center == 1.0 and self.w_premium == 0.0 and self.w_iv == 0.0
                and self.w_rvol == 0.0 and self.w_spread == 0.0
                and self.w_profit == 0.0 and self.w_rr == 0.0)
```
Extend `score_all`'s weights dict:
```python
               "spread": policy.w_spread, "profit": policy.w_profit, "rr": policy.w_rr}
```

**Step 4: Run the FULL common suite** — this is the no-op proof and it must be complete, not
scoped:
```
cd packages/common && python -m pytest tests/ -q
```
Expected: identical pass count to before Task 1. `score_all` already skips zero-weight features,
so the default policy computes neither new column.

**Step 5: Commit.**
```bash
git commit -am "feat(options): w_profit and w_rr weights; default policy still a provable no-op"
```

---

## Phase P3 — the budget ceiling (gated on `classic_options`)

### Task 5: `max_loss_ceiling` filters candidates before scoring

**Files:**
- Modify: `packages/common/ba2_common/core/option_selection_policy.py`
- Test: `packages/common/tests/test_option_selection_policy_features.py`

**Step 1: Write the failing tests.**

```python
def test_a_candidate_above_the_ceiling_is_never_picked():
    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, 0.90, 1.10, 0.15)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_credit_vertical(5.0), max_loss_ceiling=250.0)
    # the 100 strike's vertical risks 5.00 - 3.00 = 2.00/share = $200; the 105's risks
    # 5.00 - 1.00 = 4.00/share = $400 and must be excluded.
    picked = pick(cands, ctx, SelectionPolicy())
    assert picked.strike == 100


def test_a_ceiling_of_none_is_a_byte_identical_no_op():
    cands = [_c(100, 2.90, 3.10, 0.30), _c(105, 0.90, 1.10, 0.15)]
    base = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30)
    with_fn = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                            structure_fn=_credit_vertical(5.0), max_loss_ceiling=None)
    assert pick(cands, base, SelectionPolicy()) == pick(cands, with_fn, SelectionPolicy())


def test_an_unmeasurable_max_loss_fails_CLOSED_against_a_ceiling():
    """Cannot prove it fits, so it does not get in. An unknown must never be admitted to a
    budget it might exceed."""
    cands = [_c(100, None, None, 0.30)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_credit_vertical(5.0), max_loss_ceiling=250.0)
    assert pick(cands, ctx, SelectionPolicy()) is None


def test_an_UNBOUNDED_max_loss_is_charged_notional_not_excluded():
    """§8.3 CONFORMANCE. When undefined risk is permitted the design charges a notional --
    rather than refusing. Excluding it here would make the ceiling silently override
    allow_undefined_risk_options, so a permitted naked short could never be selected at
    all and the setting would read as working while doing nothing."""
    cands = [_c(100, 2.90, 3.10, 0.30)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_naked_short_call, spot=100.0,
                        undefined_risk_notional=10_000.0,
                        max_loss_ceiling=12_000.0)
    assert pick(cands, ctx, SelectionPolicy()) is not None

    ctx_tight = replace(ctx, max_loss_ceiling=5_000.0)
    assert pick(cands, ctx_tight, SelectionPolicy()) is None


def test_an_UNBOUNDED_max_loss_is_excluded_when_undefined_risk_is_NOT_permitted():
    """undefined_risk_notional=None is the default and means 'not permitted here', which
    is §8.3's default refusal -- not an oversight to be papered over with a guess."""
    cands = [_c(100, 2.90, 3.10, 0.30)]
    ctx = PolicyContext(strike_method="delta", today=date(2026, 1, 1), target=0.30,
                        structure_fn=_naked_short_call, spot=100.0,
                        max_loss_ceiling=1_000_000.0)
    assert pick(cands, ctx, SelectionPolicy()) is None
```

> **USE A SHORT CALL, NOT A SHORT PUT, TO REACH THE UNBOUNDED BRANCH.** A naked short PUT's
> loss is BOUNDED — the underlying cannot go below zero, so the worst case is
> `(strike − credit) × 100` and `max_loss` returns `MEASURED`. `upside_slope`, the only route
> to `UNBOUNDED`, sums calls and stock only; a short put contributes nothing to it. An earlier
> draft of this plan used `_naked_short_put` here, which would have exercised the MEASURED path
> while claiming to test the unbounded one — a test that passes for the wrong reason and leaves
> the branch it names unpinned. Define the helper as:
>
> ```python
> def _naked_short_call(c):
>     return [PayoffLeg(kind="call", side=OrderDirection.SELL, premium=c.mid,
>                       strike=c.strike)]
> ```

> **This pair of tests is a CORRECTION to the first draft of this plan, not an addition.**
> The draft filtered on `_payoff_pair(c, ctx)[1] or float("inf")`, which collapses `UNBOUNDED`
> and `UNMEASURABLE` into "excluded". That silently overrides
> `allow_undefined_risk_options`: with the setting ON, design §8.3 charges `spot × 100` and
> proceeds, but the draft filter would drop the candidate anyway — a permitted structure that
> can never be picked, and a setting that reads as working while doing nothing. `UNBOUNDED`
> (a known shape, priced by rule) and `UNMEASURABLE` (a broken quote) must not share a branch.

**Step 2: Run, verify failure.**

**Step 3: Implement.** Add the field to `PolicyContext`:
```python
    #: Dollars of max loss ONE contract may risk. None disables the filter entirely and is
    #: a provable no-op.
    #:
    #: This is min(instrument_left, structure_cap) -- NOT the full budget. ``book_left``
    #: depends on which structures this bar's greedy triage admits first, so it does not
    #: exist yet at selection time; threading a number that does not exist would be a
    #: fiction. Triage still sizes and refuses against the full budget afterwards.
    max_loss_ceiling: Optional[float] = None

    #: Dollars to charge ONE contract of an UNBOUNDED-loss structure, per design §8.3 --
    #: ``spot x 100``, the cash-secured-put treatment. None means undefined risk is NOT
    #: permitted here, which is §8.3's default.
    #:
    #: This exists so the ceiling cannot silently override
    #: ``allow_undefined_risk_options``. Collapsing UNBOUNDED into "excluded" would make a
    #: permitted naked short unselectable while the setting still read as ON.
    undefined_risk_notional: Optional[float] = None
```

Add a chargeable-loss helper and the filter at the END of `eligible`, after the box filter:
```python
def _chargeable_max_loss(c, ctx):
    """Dollars this candidate's max loss should be CHARGED against the ceiling.

    THREE STATES, THREE ANSWERS -- collapsing any two of them is a bug:
      * MEASURED     -> the measured amount;
      * UNBOUNDED    -> ``undefined_risk_notional`` when permitted (design §8.3), else
                        infinity, i.e. refused for lack of permission rather than for
                        lack of a number;
      * UNMEASURABLE -> infinity. A broken quote is never priced by rule.
    """
    from ba2_common.core.option_payoff import MEASURED, UNBOUNDED, max_loss
    if ctx.structure_fn is None:
        return float("inf")
    legs = ctx.structure_fn(c)
    if not legs:
        return float("inf")
    result = max_loss(legs)
    if result.state == MEASURED:
        return result.amount
    if result.state == UNBOUNDED and ctx.undefined_risk_notional is not None:
        return ctx.undefined_risk_notional
    return float("inf")
```
```python
    boxed = [c for c in out if _in_box(c, ctx)]
    if ctx.max_loss_ceiling is None:
        return boxed
    # FAILS CLOSED on LOSS ONLY. There is deliberately no max-PROFIT filter: an
    # unmeasurable profit costs a ranking signal, an unmeasurable loss can bankrupt the
    # sleeve. See "an unmeasurable max PROFIT must never narrow the search".
    return [c for c in boxed
            if _chargeable_max_loss(c, ctx) <= ctx.max_loss_ceiling]
```

**Step 4: Run.**
```
cd packages/common && python -m pytest tests/ -q
```

**Step 5: Commit.**
```bash
git commit -am "feat(options): max_loss_ceiling -- the picker cannot choose what the budget cannot afford"
```

---

### Task 6: `BUDGET_CEILING_REFUSAL`

**Files:**
- Modify: `packages/common/ba2_common/core/option_request.py`
- Test: `packages/common/tests/test_option_request.py`

Follow the existing refusal-phrase pattern in that module (it already registers the phrases
builders emit — see commit `7a263a74`). The refusal must name **the cheapest candidate's max loss
and the ceiling it exceeded**, so "this sleeve stopped trading" is diagnosable rather than
mysterious. A silent zero here is the failure mode design §9 exists to prevent.

Commit: `feat(options): BUDGET_CEILING_REFUSAL names the cheapest candidate and the ceiling`

---

## Phase P5 — grid wiring

### Task 7: `w_premium` sign fix

**Files:** `docs/superpowers/specs/2026-08-27-option-risk-manager-design.md` §9.5 is already
amended by the new design doc §8; this task is the *gene domain* when Task 9 writes it.

Domain becomes `-2.0 .. 2.0`. **Do not skip this or fold it silently into Task 9** — it is a
behaviour change to a documented gene and deserves its own commit and message. A seller wants rich
premium and a buyer wants cheap; unsigned, the gene is half dead across the entire debit half.

---

### Task 8: `loss_pct_of_max_loss` condition field

**Files:**
- Modify: the condition evaluator in `packages/common/ba2_common/core/TradeConditions.py`
- Test: alongside the existing option condition tests

Reads `max_loss_per_contract` **back off the parent order's `data`**, where design §8.2 has the RM
persist it at submit (mirroring `option_reserve`, `TradeActions.py:2326`). No leg reconstruction,
no OCC parsing — that is what makes this cheap.

The condition **cannot fire** when the persisted value is absent or was not `MEASURED`. That is
"contracts that support it" enforced by the data rather than by a special case.

Commit: `feat(options): loss_pct_of_max_loss -- a stop scaled to defined risk, not to credit`

---

### Task 9: `opt_sl_ml` exit rule, defined-risk members only

**Files:**
- Modify: `testplatform/ba2test_launcher.py` — `_option_exit_rules`, currently at line ~2989
- Test: `testplatform/backend/tests/test_option_grid_foundations.py`

**The test that matters most** — assert over **all** members, not a spot check:

```python
def test_opt_sl_ml_is_never_emitted_for_a_member_whose_max_loss_is_unbounded():
    for kind in _OPTION_STRATS:
        ids = {r["id"] for r in _option_exit_rules(kind)}
        if kind in _UNDEFINED_RISK_MEMBERS:
            assert "opt_sl_ml" not in ids, f"{kind} has no max loss to take a fraction of"
        else:
            assert "opt_sl_ml" in ids
```

Rule body is in design §6. Both stops may be live at once — first match wins, which the
OPEN_POSITIONS ruleset already does.

Commit: `feat(grid): opt_sl_ml -- stop at a % of max loss, defined-risk members only`

---

### Task 10: GA-wire all seven weights, with the sharing tier

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (param space)
- Test: `testplatform/backend/tests/test_option_param_reachability.py`

Domains are design §7's table. Sharing:
- the five general weights **share per debit/credit half**, reusing the existing
  `_DEBIT_OPTION_MEMBERS` / `_CREDIT_OPTION_MEMBERS` partition — 10 genes, not 90;
- `w_profit` / `w_rr` are emitted **only where the payoff is bounded on the side they read**,
  which does *not* follow the debit/credit split.

**The dead-gene guard test is the point of this task**, not an extra:

```python
def test_every_emitted_weight_can_actually_move_the_pick():
    """A weight that cannot change the contract selected on a recorded chain is a gene the
    GA burns budget on and can never move -- the failure this codebase has already paid for
    twice (the dead roll gene; the trial-config whitelist dropping new knobs)."""
```

> **APPLICABILITY IS PER-CHAIN, NOT PER-STRUCTURE — this changes how the guard test must be
> written.** (Discovered building Tasks 2-4, 2026-08-30.)
>
> `inapplicable_features` is sensitive to a SINGLE candidate's payoff shape: if *any* candidate
> in the set is off-scale, the whole column goes inert (that is the fix for the mixed-shape
> demotion hole — see the design's applicability section). So `w_profit`/`w_rr` can flip between
> live and inert for the same rule as the chain changes from bar to bar.
>
> Two consequences:
>
> 1. **The guard test must use a chain on which the feature is genuinely applicable**, or it
>    fails for a reason that has nothing to do with the gene being dead. Assert applicability
>    first (`inapplicable_features(...) == ()`), then assert the weight moves the pick. A guard
>    test that cannot tell "dead gene" from "inert on this particular chain" is worse than none.
> 2. **"Emit only where the payoff is bounded on the side they read" is a statement about the
>    BUILDER's shape, not about any one chain.** A single-shape builder (every credit vertical
>    completes to a credit vertical) has a stable answer and is what the emission rule keys on.
>    Only a builder whose `structure_fn` returns different shapes per candidate has a varying
>    one — no builder does that today, and if one is ever added, its genes need re-examining
>    rather than the emission rule being loosened.
>
> The variance is correct behaviour — the alternative is demoting long calls — but it means the
> two genes' effective search pressure is a function of chain composition, which is worth
> knowing before reading GA results that use them.

Before wiring, **re-read `reference-trial-config-whitelist-drops-new-knobs`**:
`_build_daily_trial_config` rebuilds the config key by key, so a knob missing there is inert while
every log claims it works. Add the seven weights there in the same commit.

Commit: `feat(grid): GA-wire all seven selection weights; w_premium becomes signed`

---

## Verification before calling this done

Use `@superpowers:verification-before-completion`. Evidence, not assertions:

1. `cd packages/common && python -m pytest tests/ -q` — full suite, pass count recorded.
2. `cd testplatform/backend && python -m pytest tests/ -q` — **from this directory**, matching CI's
   `working-directory`. A path-relative test that passes from the repo root and fails in CI is a
   defect this repo shipped once already (`dcd12237`).
3. The recorded-chain no-op test passes **unchanged**.
4. The dead-gene guard passes for every emitted weight on every member.
5. **Bump `testplatform/version.py`** (`TEST_APP_VERSION`) — Tasks 8-10 touch `packages/` and
   `testplatform/`. Do NOT bump mid-grid-run without checking: a bump re-syncs every distributed
   worker, and `reference-distributed-optimize-traps` records that breaking the version match
   mid-run kills the run.

---

## Out of scope

* Re-picking against `book_left` (design §11).
* Teaching all 17 builders to supply `structure_fn` — untaught builders lose the two features
  visibly, which is the designed fallback. Do them as they are needed, not up front.
* Share-capacity for short calls.
