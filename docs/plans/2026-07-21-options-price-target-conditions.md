# Analyst Price-Target-Range Entry Conditions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the options GA (OS1/OS2/OS3 grid) a way to gate entries on WHERE the current
price sits relative to FMPRating's analyst price-target range (low/high), decoupled from the
expert's BUY/SELL/HOLD rating — so e.g. a put can fire because price is already above even the
high analyst estimate, regardless of the rating still saying "buy", and neutral/credit
structures (iron condor, strangles) can gate on price sitting *inside* the target range.

**Architecture:** FMPRating already computes and persists `target_low`/`target_high`/
`target_consensus` on every `ExpertRecommendation.data["FMPRating"]` — no new data plumbing
needed. Add two new `CompareCondition` subclasses (`price_vs_target_low_percent`,
`price_vs_target_high_percent`, each = `(current_price - target) / target * 100`) following the
exact pattern of the existing `PercentBelowRecentHighCondition`. Register them in
`TradeConditions.create_condition`'s field-to-class map — the GA's gene-space collector
(`strategy_param_space._walk_condition_nodes`) is completely field-agnostic (it only looks at
`id`/`optimize`/`value_min`/`value_max`/`toggle_optimize` keys), so once a condition dict using
these new `field` values appears in a rule, it becomes GA-optimizable automatically — zero
changes needed to the GA/gene-space code itself.

Each OS1/OS2/OS3 member's entry rule (`_option_entry_rule` in `testplatform/ba2test_launcher.py`)
is evaluated independently, first-match-wins (`continue_processing: False`), so "open a
DIFFERENT structure based on where price sits" is already the existing architecture — this plan
just gives the GA two new toggleable, optimizable dimensions per member to encode that decision
boundary, on top of making the existing bullish/bearish rating gate itself toggleable (so the
GA can rely on price-positioning alone, or combine it with the rating, per member).

**Tech Stack:** Python, SQLModel, DEAP-style genetic algorithm (`strategy_optimization_handler.py`
/ `strategy_param_space.py`), pytest.

---

## Design reference: four gates from two underlying conditions

- `price_vs_target_low_percent = (current_price - target_low) / target_low * 100`
  Positive = price already above the LOW (most conservative) analyst estimate.
- `price_vs_target_high_percent = (current_price - target_high) / target_high * 100`
  Positive = price already above the HIGH (most bullish) analyst estimate — i.e. overextended
  even by the most optimistic analyst's number.

**Important constraint that shapes this design:** the GA's gene-space collector
(`strategy_param_space._walk_condition_nodes`) only ever generates genes for a condition's
`value` (via `value_min`/`value_max`/`value_step`) and its `enabled` flag (via
`toggle_optimize`) — it never touches `op`. So a condition dict's comparison operator is FIXED
for the life of that dict; the GA can only slide the threshold and flip it on/off, never flip
its direction. That means a single `price_vs_target_low_percent` condition wired with a fixed
`op=">"` can express "price is above X" for any threshold X, but can NEVER express "price is
below X" — the two are different rule *shapes*, not different values of the same shape. Wiring
each field ONCE (an earlier draft of this plan wired `price_low: op=">"` +
`price_high: op="<"`) can therefore only ever reach ONE of the three non-trivial patterns below
(the "inside the range" one) — the other two, including the plan's own headline motivating
example ("put fires because price already cleared the high estimate, regardless of rating"),
would be structurally unreachable. The fix is to wire each field TWICE, once per direction, so
the GA can independently enable whichever shape(s) it needs per member:

| Gate id | Field | Fixed op | Meaning when enabled (threshold near 0) |
|---|---|---|---|
| `{m}-price_low_below` | `price_vs_target_low_percent` | `<` | Price still below the low estimate — room to run |
| `{m}-price_high_above` | `price_vs_target_high_percent` | `>` | Price already above the high estimate — overextended |
| `{m}-price_low_above` | `price_vs_target_low_percent` | `>` | Price has cleared the low estimate (paired with the next row for "inside the range") |
| `{m}-price_high_below` | `price_vs_target_high_percent` | `<` | Price hasn't reached the high estimate (paired with the row above) |

How the GA reaches each of the user's target patterns by toggling these four gates
independently (no per-member hardcoding of direction needed — only the shared default
threshold/range, searched per gate):

| Pattern | Gates enabled |
|---|---|
| "Still room to run" (favor `O_LC`) | `{m}-price_low_below` only |
| "Overextended, put regardless of rating" (favor `O_LP`/`O_VERT`) | `{m}-price_high_above` only |
| "Inside the analyst range" (favor `O_SSTG`/`O_SSTD`/`O_IC` — neutral/credit) | `{m}-price_low_above` AND `{m}-price_high_below` together |
| "Ignore price-target entirely, use rating only" | all four OFF (today's behavior, unchanged) |

This roughly doubles the gene-space growth from the original 2-gate design (4 new
toggleable/optimizable conditions per member instead of 2, on top of the now-toggleable rating
gate) — factor this into the "Relaunch note" population-size guidance at the end of this plan.

`price_vs_target_consensus_percent` (vs. the median/consensus target) is also added as a general
primitive (Task 2) for completeness and potential future use (e.g. `O_BF`'s "pinned near
consensus" framing), but Task 3 only wires `_low`/`_high` (as the four gates above) into the
OS1/OS2/OS3 templates — `consensus` can be wired into a specific member's rule by hand later the
same way if it proves useful.

---

### Task 1: New `ExpertEventType` enum members

**Files:**
- Modify: `packages/common/ba2_common/core/types.py`

**Step 1: Locate the numeric-condition block and add the new members**

Find this block (search for `N_PROFIT_LOSS_PERCENT`):

```python
    N_PERCENT_TO_NEW_TARGET = "percent_to_new_target"          # Distance from current price to new expert target
    N_NEW_TARGET_PERCENT = "new_target_percent"                # Percent change from current TP to new target (positive if higher, negative if lower)
    N_PROFIT_LOSS_AMOUNT = "profit_loss_amount"
    N_PROFIT_LOSS_PERCENT = "profit_loss_percent"
    N_DAYS_OPENED = "days_opened"
```

Add three new members immediately after `N_PROFIT_LOSS_PERCENT` (before `N_DAYS_OPENED`):

```python
    N_PROFIT_LOSS_AMOUNT = "profit_loss_amount"
    N_PROFIT_LOSS_PERCENT = "profit_loss_percent"
    # Current price vs. FMPRating's analyst price-target lines (target_low/target_high/
    # target_consensus, already persisted on ExpertRecommendation.data["FMPRating"] by
    # FMPRating.run_analysis) - lets an entry rule gate on WHERE price sits relative to the
    # analyst range, decoupled from the expert's BUY/SELL/HOLD rating. Positive % = price is
    # ABOVE that target line.
    N_PRICE_VS_TARGET_LOW_PERCENT = "price_vs_target_low_percent"
    N_PRICE_VS_TARGET_HIGH_PERCENT = "price_vs_target_high_percent"
    N_PRICE_VS_TARGET_CONSENSUS_PERCENT = "price_vs_target_consensus_percent"
    N_DAYS_OPENED = "days_opened"
```

**Step 2: No test needed for this step alone** — it's covered by Task 2's tests (an unregistered
enum member is inert; the condition classes + registration are what's actually exercised).

**Step 3: Commit**

```bash
git add packages/common/ba2_common/core/types.py
git commit -m "feat(conditions): add price-vs-analyst-target ExpertEventType members"
```

---

### Task 2: New condition classes + registration

**Files:**
- Modify: `packages/common/ba2_common/core/TradeConditions.py`
- Test: `tests/test_trade_conditions.py`

**Step 1: Write the failing tests**

Add to `tests/test_trade_conditions.py` (imports already include `create_condition,
CompareCondition` — add the three new class names to the existing import block at the top, and
append this new test class anywhere after `TestConfidenceCondition`):

```python
from ba2_trade_platform.core.TradeConditions import (
    # ...(existing imports)...
    PriceVsTargetLowCondition, PriceVsTargetHighCondition, PriceVsTargetConsensusCondition,
)
```

```python
def _make_recommendation_with_targets(target_low=None, target_high=None, target_consensus=None,
                                       price_at_date=150.0, **kwargs):
    rec = _make_recommendation(price_at_date=price_at_date, **kwargs)
    rec.data = {"FMPRating": {
        "target_low": target_low, "target_high": target_high, "target_consensus": target_consensus,
    }}
    return rec


class TestPriceVsTargetConditions:
    def test_price_above_low_target_is_positive(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 120.0
        rec = _make_recommendation_with_targets(target_low=100.0)
        cond = PriceVsTargetLowCondition(account, "AAPL", rec, ">", 0.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == pytest.approx(20.0)

    def test_price_below_low_target_is_negative(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 80.0
        rec = _make_recommendation_with_targets(target_low=100.0)
        cond = PriceVsTargetLowCondition(account, "AAPL", rec, "<", 0.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == pytest.approx(-20.0)

    def test_price_above_high_target_is_positive(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 250.0
        rec = _make_recommendation_with_targets(target_high=200.0)
        cond = PriceVsTargetHighCondition(account, "AAPL", rec, ">", 0.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == pytest.approx(25.0)

    def test_price_below_high_target(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 150.0
        rec = _make_recommendation_with_targets(target_high=200.0)
        cond = PriceVsTargetHighCondition(account, "AAPL", rec, "<", 0.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == pytest.approx(-25.0)

    def test_consensus_condition(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 110.0
        rec = _make_recommendation_with_targets(target_consensus=100.0)
        cond = PriceVsTargetConsensusCondition(account, "AAPL", rec, ">", 0.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == pytest.approx(10.0)

    def test_missing_target_data_returns_false(self):
        account = _make_mock_account()
        rec = _make_recommendation_with_targets()  # no targets set
        cond = PriceVsTargetLowCondition(account, "AAPL", rec, ">", 0.0)
        assert cond.evaluate() is False
        assert cond.calculated_value is None

    def test_missing_price_returns_false(self):
        account = _make_mock_account()
        account._prices.pop("AAPL", None)
        rec = _make_recommendation_with_targets(target_low=100.0)
        cond = PriceVsTargetLowCondition(account, "AAPL", rec, ">", 0.0)
        assert cond.evaluate() is False

    def test_create_condition_factory_wires_field(self):
        account = _make_mock_account()
        account._prices["AAPL"] = 120.0
        rec = _make_recommendation_with_targets(target_low=100.0)
        cond = create_condition(ExpertEventType.N_PRICE_VS_TARGET_LOW_PERCENT, account, "AAPL", rec,
                                operator_str=">", value=0.0)
        assert isinstance(cond, PriceVsTargetLowCondition)
        assert cond.evaluate() is True
```

**Step 2: Run tests to verify they fail**

```
.venv\Scripts\python.exe -m pytest tests/test_trade_conditions.py::TestPriceVsTargetConditions -v
```
Expected: FAIL with `ImportError: cannot import name 'PriceVsTargetLowCondition'`.

**Step 3: Implement the condition classes**

In `packages/common/ba2_common/core/TradeConditions.py`, add immediately after
`PercentAboveRecentLowCondition` (i.e. right before the `IVRankCondition` class — search for
`class IVRankCondition`):

```python
class PriceVsTargetLowCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst LOW price target.

    Calculates: (current_price - target_low) / target_low * 100. Positive means price is
    ABOVE the low (most conservative) analyst estimate. Reads target_low from
    expert_recommendation.data["FMPRating"] (persisted by FMPRating.run_analysis for every
    recommendation) - decoupled from the expert's BUY/SELL/HOLD rating so an option entry can
    gate on price positioning independent of the directional signal.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_low = fmp_data.get("target_low") if fmp_data else None
            if target_low is None or target_low <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_low) / target_low * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            logger.error(f"Error evaluating price vs target low condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst LOW target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PriceVsTargetHighCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst HIGH price target.

    Calculates: (current_price - target_high) / target_high * 100. Positive means price is
    ABOVE the high (most bullish) analyst estimate - i.e. overextended even by the most
    optimistic analyst's number. This is the condition that lets an entry fire a bearish
    structure (e.g. a long put) purely on price positioning, regardless of whether the
    expert's own rating still says BUY.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_high = fmp_data.get("target_high") if fmp_data else None
            if target_high is None or target_high <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_high) / target_high * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            logger.error(f"Error evaluating price vs target high condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst HIGH target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PriceVsTargetConsensusCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst CONSENSUS (median) price target.

    Calculates: (current_price - target_consensus) / target_consensus * 100.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_consensus = fmp_data.get("target_consensus") if fmp_data else None
            if target_consensus is None or target_consensus <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_consensus) / target_consensus * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            logger.error(f"Error evaluating price vs target consensus condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst CONSENSUS target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"
```

**Step 4: Register the three classes in `create_condition`'s `condition_map`**

In the same file, find `create_condition`'s `condition_map` dict (search for
`ExpertEventType.N_CONFIDENCE: ConfidenceCondition`) and add three lines right after it:

```python
        ExpertEventType.N_CONFIDENCE: ConfidenceCondition,
        ExpertEventType.N_PRICE_VS_TARGET_LOW_PERCENT: PriceVsTargetLowCondition,
        ExpertEventType.N_PRICE_VS_TARGET_HIGH_PERCENT: PriceVsTargetHighCondition,
        ExpertEventType.N_PRICE_VS_TARGET_CONSENSUS_PERCENT: PriceVsTargetConsensusCondition,
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE: InstrumentAccountShareCondition,
```

**Step 5: Run tests to verify they pass**

```
.venv\Scripts\python.exe -m pytest tests/test_trade_conditions.py::TestPriceVsTargetConditions -v
```
Expected: 8 passed.

**Step 6: Run the full trade-conditions suite to check for regressions**

```
.venv\Scripts\python.exe -m pytest tests/test_trade_conditions.py -v
```
Expected: all pass (no change to any existing condition).

**Step 7: Commit**

```bash
git add packages/common/ba2_common/core/TradeConditions.py tests/test_trade_conditions.py
git commit -m "feat(conditions): add price-vs-analyst-target-range conditions"
```

---

### Task 3: Wire into OS1/OS2/OS3 option entry rules

**Files:**
- Modify: `testplatform/ba2test_launcher.py:2007-2029` (`_option_entry_rule`)
- Test: `testplatform/backend/tests/test_launcher_option_entry_rule.py` (new file)

**Step 1: Write the failing test**

Create `testplatform/backend/tests/test_launcher_option_entry_rule.py`, following the exact
import idiom already used by `test_launcher_parse_symbols.py` (loads `ba2test_launcher.py` by
file path since it lives one directory above `backend/`):

```python
"""``_option_entry_rule`` must expose the price-vs-analyst-target conditions as toggleable,
optimizable gates (see docs/plans/2026-07-21-options-price-target-conditions.md), and the
existing bullish/bearish rating gate must itself become toggleable so the GA can rely on price
positioning alone.
"""
import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _find_cond(rule, cond_id):
    for c in rule["conditions"]["conditions"]:
        if c["id"] == cond_id:
            return c
    raise AssertionError(f"condition {cond_id} not found in rule {rule['id']}")


def test_signal_gate_is_toggleable():
    rule = mod._option_entry_rule("O_LC")
    signal = _find_cond(rule, "o_lc-signal")
    assert signal["field"] == "bullish"
    assert signal["toggle_optimize"] is True


def test_price_target_gates_present_and_optimizable_with_correct_directions():
    """Four gates, each independently toggleable, so the GA can reach all three non-trivial
    price-positioning patterns (below-low-only, above-high-only, within-range) - not just one
    of them. See the plan's "Design reference" section for why a single op-per-field wiring
    (an earlier, buggy draft of this plan) can't do this: op is never part of the gene space,
    only value and enabled are, so a fixed op only ever reaches ONE direction."""
    rule = mod._option_entry_rule("O_LC")

    low_below = _find_cond(rule, "o_lc-price_low_below")
    assert low_below["field"] == "price_vs_target_low_percent"
    assert low_below["op"] == "<"
    assert low_below["toggle_optimize"] is True
    assert low_below["optimize"] is True
    assert low_below["value_min"] < 0 < low_below["value_max"]

    high_above = _find_cond(rule, "o_lc-price_high_above")
    assert high_above["field"] == "price_vs_target_high_percent"
    assert high_above["op"] == ">"
    assert high_above["toggle_optimize"] is True

    low_above = _find_cond(rule, "o_lc-price_low_above")
    assert low_above["field"] == "price_vs_target_low_percent"
    assert low_above["op"] == ">"
    assert low_above["toggle_optimize"] is True

    high_below = _find_cond(rule, "o_lc-price_high_below")
    assert high_below["field"] == "price_vs_target_high_percent"
    assert high_below["op"] == "<"
    assert high_below["toggle_optimize"] is True


def test_bearish_member_gets_bearish_signal_field():
    rule = mod._option_entry_rule("O_LP")
    signal = _find_cond(rule, "o_lp-signal")
    assert signal["field"] == "bearish"


def test_every_pure_option_member_gets_all_four_price_target_gates():
    for member in mod._OPTION_STRATS:
        rule = mod._option_entry_rule(member)
        m = member.lower()
        _find_cond(rule, f"{m}-price_low_below")
        _find_cond(rule, f"{m}-price_high_above")
        _find_cond(rule, f"{m}-price_low_above")
        _find_cond(rule, f"{m}-price_high_below")
```

**Step 2: Run tests to verify they fail**

```
.venv\Scripts\python.exe -m pytest testplatform/backend/tests/test_launcher_option_entry_rule.py -v
```
Expected: FAIL — `test_signal_gate_is_toggleable` fails on `assert signal["toggle_optimize"] is True`
(KeyError, since the key doesn't exist yet), and the four-gate tests fail with the
`_find_cond` `AssertionError` (conditions not present yet).

**Step 3: Implement — modify `_option_entry_rule`**

In `testplatform/ba2test_launcher.py`, replace the current `_option_entry_rule` function body
(the one starting `def _option_entry_rule(member: str, *, toggleable: bool = False) -> dict:`)
with:

```python
def _option_entry_rule(member: str, *, toggleable: bool = False) -> dict:
    """The entry TradeRule dict for one pure-option strategy key: directional signal gate
    (bullish for every original key, bearish for O_LP — see _OPTION_ENTRY_GATE) + flat +
    optimizable confidence gate + four price-vs-analyst-target-range gates, action = the
    member's option action config. Rule/condition ids are prefixed with the member key so a
    GROUP of these rules yields uniquely-keyed genes per member. ``toggleable`` adds the
    rule-level enabled gene (group members only — a single-strategy job keeps its one entry
    always-on).

    The signal (bullish/bearish) gate and all four price-vs-target gates are independently
    toggle_optimize=True. Op is fixed per gate (the GA's gene space only ever searches a
    condition's threshold value and enabled flag, never its operator - see
    docs/plans/2026-07-21-options-price-target-conditions.md's "Design reference"), so each
    directional pattern needs its OWN gate rather than one gate whose op could flip:
    price_low_below (< , "still below the low estimate") + price_high_above (> , "already
    above the high estimate") + price_low_above (>) + price_high_below (< , the last two
    paired together = "inside the analyst range"). The GA can search: rating-only (today's
    behavior, all four price gates OFF), price-only (signal gate OFF, e.g. "put even though
    the rating still says buy" via price_high_above alone), any combination of the four price
    gates together, or every gate off entirely.
    """
    m = member.lower()
    price_target_conditions = [
        {"id": f"{m}-price_low_below", "field": "price_vs_target_low_percent", "op": "<",
         "value": 0.0, "optimize": True, "value_min": -20.0, "value_max": 20.0,
         "value_step": 5.0, "toggle_optimize": True},
        {"id": f"{m}-price_high_above", "field": "price_vs_target_high_percent", "op": ">",
         "value": 0.0, "optimize": True, "value_min": -20.0, "value_max": 20.0,
         "value_step": 5.0, "toggle_optimize": True},
        {"id": f"{m}-price_low_above", "field": "price_vs_target_low_percent", "op": ">",
         "value": 0.0, "optimize": True, "value_min": -20.0, "value_max": 20.0,
         "value_step": 5.0, "toggle_optimize": True},
        {"id": f"{m}-price_high_below", "field": "price_vs_target_high_percent", "op": "<",
         "value": 0.0, "optimize": True, "value_min": -20.0, "value_max": 20.0,
         "value_step": 5.0, "toggle_optimize": True},
    ]
    rule = {
        "id": f"{m}-entry",
        "name": f"{member}-entry",
        "conditions": {"id": f"{m}-root", "type": "AND", "conditions": [
            {"id": f"{m}-signal", "field": _OPTION_ENTRY_GATE[member], "field_type": "flag",
             "toggle_optimize": True},
            {"id": f"{m}-flat", "field": "has_no_position", "field_type": "flag"},
            {"id": f"{m}-gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5,
             "toggle_optimize": True},
            *price_target_conditions,
        ]},
        "actions": [_option_entry_action_for(member)],
        "continue_processing": False,
    }
    if toggleable:
        rule["toggle_optimize"] = True
    return rule
```

(Everything changed vs. the current version: `{m}-signal` gained `"toggle_optimize": True`; four
new condition dicts — `{m}-price_low_below`, `{m}-price_high_above`, `{m}-price_low_above`,
`{m}-price_high_below` — were appended before the closing `]}`, built via the
`price_target_conditions` list for readability given there are now four near-identical dicts.)

**Step 4: Run tests to verify they pass**

```
.venv\Scripts\python.exe -m pytest testplatform/backend/tests/test_launcher_option_entry_rule.py -v
```
Expected: 4 passed (5 with the loop test counting as 1).

**Step 5: Run the full existing launcher/backtest suite to check for regressions**

```
cd testplatform/backend
../../.venv/Scripts/python.exe -m pytest tests/ -q
../../.venv/Scripts/python.exe -m pytest ../../tests/test_trade_conditions.py -q
```
Expected: all pass. Pay particular attention to any test asserting the exact shape/length of
`_option_entry_rule`'s condition list (e.g. an existing golden-config or gene-count test) — if
one exists and fails only because it now sees 2 more conditions per member, update its expected
count; do NOT loosen the assertion to `>=` without checking why it was exact in the first place.

**Step 6: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/test_launcher_option_entry_rule.py
git commit -m "feat(options): wire price-vs-analyst-target gates into OS1/OS2/OS3 entry rules"
```

---

### Task 4: Docs + version bump

**Files:**
- Modify: `testplatform/docs/grid-and-fitness-guide.md` (or nearest relevant doc — check for an
  existing "condition catalog" or "OS1/OS2/OS3" section and add a short paragraph)
- Modify: `ba2_trade_platform/version.py`

**Step 1:** Add a short paragraph to the options-grid doc describing the two new conditions
(`price_vs_target_low_percent` / `price_vs_target_high_percent`), the sign convention, and that they're
toggleable/optimizable per OS1/OS2/OS3 member.

**Step 2:** Bump `APP_VERSION` in `ba2_trade_platform/version.py` (patch the trailing build
number per the repo's existing convention — check the current value first, don't guess).

**Step 3: Commit**

```bash
git add testplatform/docs/grid-and-fitness-guide.md ba2_trade_platform/version.py
git commit -m "docs: document price-vs-analyst-target entry conditions; version bump"
```

---

## Relaunch note (not a code task)

The added conditions roughly double the per-member gene count for OS1/OS2/OS3 (signal-gate
toggle + 2 new compare conditions, each with their own `enabled` + `value` genes). Per the
session discussion, plan to increase `--population` (and possibly `--generations`) above the
current defaults (40 / 8) when relaunching the options matrix after this lands, so the GA has
enough individuals to actually explore the larger search space rather than converging
prematurely on whatever the initial random population happened to sample. No specific number is
prescribed here — check GA convergence (population diversity / fitness plateauing) on the first
relaunch and adjust.
