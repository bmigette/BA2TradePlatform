# Option GA Grid — Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the six independent pieces the option grid needs, none of which depend on seeding, universe tooling or the driver script.

**Architecture:** All six are edits to `testplatform/ba2test_launcher.py` plus their tests. They are ordered so the genome-shrinking change lands first (every later measurement is taken against the smaller genome), then the new structure, then the knobs.

**Tech Stack:** Python 3.11+, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-option-ga-grid-design.md`, sections 4, 6.1, 7, 7.1.

---

## Orientation

**Repo:** `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform`. Branch off `dev`.

**The venv is `venv/`, NOT `.venv/`.** The launcher needs a path prefix for anything that imports it:

```bash
PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python ...
```

**Baselines** — record before you start, they must not go down:

```bash
./venv/bin/python -m pytest packages/common/tests/ -q                     # 2402
./venv/bin/python -m pytest tests/ -q                                     # 4410
PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python \
  -m pytest testplatform/backend -q --ignore=testplatform/backend/tests_scripts \
  --ignore=testplatform/backend/scripts                                   # 3202 passed, 1 known Windows-only failure
```

`tests/` and `packages/common/tests/` cannot share one pytest invocation — both `conftest.py` files import as `tests.conftest`.

**Measuring a genome** — you will do this repeatedly:

```python
PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("lch", "testplatform/ba2test_launcher.py")
m = importlib.util.module_from_spec(spec); sys.modules["lch"] = m
try: spec.loader.exec_module(m)
except SystemExit: pass
from app.services.strategy_param_space import collect_param_space
for key in ("O_LC", "O_IC", "OS1"):
    print(key, len(collect_param_space(m._build_strategy(key, f"g-{key}", "FMPRating"))))
PY
```

**Four tests will fail across this plan, and two assert the OPPOSITE of an intended change.** That is expected and named per task. Rewriting them is part of the task — but only those four, and only as each task names them. If any other test breaks, you have made a mistake.

**Do not place real orders.** Nothing here touches a broker.

---

## File Structure

| file | responsibility |
|---|---|
| `testplatform/ba2test_launcher.py` | all six changes |
| `testplatform/backend/tests/test_launcher_volume_vol_genes.py` | **rewrite** — asserts per-member `rel_volume` |
| `testplatform/backend/tests/test_launcher_option_sizing_gene.py` | **rewrite** — asserts `spec["max"] <= 40.0` |
| `testplatform/backend/tests/test_option_strategy_builders.py` | **extend** — add `O_WHEEL` |
| `testplatform/backend/tests/test_option_grid_foundations.py` | **new** — the genome and gate assertions |

---

### Task 1: Replace the four `price_*` gates with one expected-profit gate

The largest win in the plan: it removes the only FMPRating-specific coupling in the entry rule and shrinks every genome by ~21%.

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (`_price_target_gates` ~2973, `_option_entry_rule` ~3087)
- Test: `testplatform/backend/tests/test_option_grid_foundations.py` (new)

- [ ] **Step 1: Write the failing test**

Create `testplatform/backend/tests/test_option_grid_foundations.py`:

```python
"""The option entry rule must gate on a signal EVERY expert produces.

WHY THIS CHANGED. The four price_* gates read price_vs_target_low_percent /
price_vs_target_high_percent, and PriceVsTargetLowCondition is hard-keyed to
expert_recommendation.data["FMPRating"]["target_low"]. Only FMPRating writes target_low, so
under any other expert all four gates fail CLOSED -- 8 of ~28 genes per structure, and any
genome enabling one trades nothing. That is the pathology the launcher already records for the
confidence gate in opt 333 (enabled 0.80 in dead genomes vs 0.14 in trading ones).

expected_profit_percent is NON-NULLABLE on ExpertRecommendation, so every expert produces it,
and N_EXPECTED_PROFIT_TARGET_PERCENT already exists as a condition. target_price is nullable and
DERIVES from expected_profit_percent when absent -- the model's own field description says so --
so the two are the same signal and one gate replaces four.
"""
import importlib.util
import sys

import pytest


def _launcher():
    spec = importlib.util.spec_from_file_location("lch", "testplatform/ba2test_launcher.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _space(m, key, expert="FMPRating"):
    from app.services.strategy_param_space import collect_param_space
    return collect_param_space(m._build_strategy(key, f"g-{key}", expert))


PURE = ["O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS", "O_BEARCS", "O_BULLPS",
        "O_CSP", "O_IC", "O_JL", "O_RS", "O_SSTD", "O_SSTG", "O_STRD", "O_STRG"]


@pytest.mark.parametrize("key", PURE)
def test_no_structure_gates_on_the_analyst_target_range(key):
    """price_vs_target_* is FMPRating-only data. Nothing may depend on it."""
    genes = _space(_launcher(), key)
    offenders = [g for g in genes if "price_low" in g or "price_high" in g]
    assert not offenders, f"{key} still carries FMPRating-only price gates: {offenders}"


@pytest.mark.parametrize("key", PURE)
def test_every_structure_gates_on_expected_profit(key):
    genes = _space(_launcher(), key)
    assert any("exp_profit" in g for g in genes), (
        f"{key} has no expected-profit gate; the entry has no universal signal gate at all")


@pytest.mark.parametrize("key", PURE)
def test_the_expected_profit_gate_is_searchable_and_toggleable(key):
    genes = _space(_launcher(), key)
    assert any(g.endswith("-exp_profit:value") for g in genes)
    assert any(g.endswith("-exp_profit:enabled") for g in genes)


def test_the_swap_shrinks_every_structure_genome():
    """The point is not only correctness: 8 genes out, 2 in, on every structure."""
    m = _launcher()
    for key in PURE:
        assert len(_space(m, key)) <= 26, (
            f"{key} genome is {len(_space(m, key))}; the price-gate swap should put every "
            f"structure at or under 26 genes")


def test_the_group_genome_shrinks_too():
    m = _launcher()
    assert len(_space(m, "OS1")) <= 95, "OS1 should fall from 120 to ~90 after the swap"


def test_no_value_offset_from_survives_in_an_option_entry_rule():
    """The four price gates chained via value_offset_from, and those offsets resolve their base
    against the GLOBAL gene map -- so any future shared condition id would have silently coupled
    members across a family. Removing the gates removes the trap; keep it removed."""
    m = _launcher()
    strat = m._build_strategy("OS1", "g-OS1", "FMPRating")

    def walk(node, out):
        if isinstance(node, list):
            for n in node:
                walk(n, out)
        elif isinstance(node, dict):
            if "value_offset_from" in node:
                out.append(node.get("id"))
            for v in node.values():
                walk(v, out)
        return out

    assert walk(strat.entry_rules, []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python -m pytest testplatform/backend/tests/test_option_grid_foundations.py -q`
Expected: the `price_low`/`price_high` and `exp_profit` tests fail; the genome tests fail at 28/30/120.

- [ ] **Step 3: Write the implementation**

In `testplatform/ba2test_launcher.py`, add beside the other gate constants (near `_RELATIVE_VOLUME_GATE`, ~3062):

```python
# EXPECTED PROFIT — the entry's only signal-strength gate, and the ONLY one every expert can
# answer. `ExpertRecommendation.expected_profit_percent` is non-nullable, so an expert cannot
# omit it; `target_price` is nullable and DERIVES from it when absent (see the field's own
# description), so the two are the same signal and this gate covers both.
#
# It REPLACES the four price_vs_target_* gates. Those read
# `expert_recommendation.data["FMPRating"]["target_low"]` via a hard-keyed condition, and only
# FMPRating writes that key — so under DeterministicScorer (or any future expert) all four
# failed CLOSED, taking 8 of ~28 genes per structure with them and making any genome that
# enabled one trade nothing.
#
# Range is AUTHORED, not measured: 2-20% brackets "any positive edge" through "a call the expert
# is loud about", and the grid searches the threshold. Re-centre it on the realised
# expected_profit distribution once a grid has run.
_EXPECTED_PROFIT_GATE = {"value": 5.0, "value_min": 2.0, "value_max": 20.0, "value_step": 2.0}


def _expected_profit_gate(m: str) -> dict:
    """The expected-profit entry gate leaf for member prefix ``m``."""
    return {"id": f"{m}-exp_profit", "field": "expected_profit_target_percent", "op": ">",
            "optimize": True, "toggle_optimize": True, **_EXPECTED_PROFIT_GATE}
```

Then in `_option_entry_rule` (~3112), replace the `price_target_conditions` line and its use:

```python
    m = member.lower()
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
            _iv_rank_gate(m, member),
            _relative_volume_gate(m),
            _iv_rv_gate(m, member),
            _expected_profit_gate(m),
        ]},
        "actions": [_option_entry_action_for(member)],
        "continue_processing": False,
    }
```

Delete `_price_target_gates` and the four band constants `_PRICE_GATE_LOW_FLOOR`,
`_PRICE_GATE_HIGH_FLOOR`, `_PRICE_GATE_WIDTH` **only if nothing else references them** — check
first with `grep -n "_price_target_gates\|_PRICE_GATE_" testplatform/ba2test_launcher.py`. If an
equity strategy uses them, leave them and only stop calling them from the option rule.

Update the `_option_entry_rule` docstring: it currently says the rule carries "four
price-vs-analyst-target-range gates", which becomes false.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python -m pytest testplatform/backend/tests/test_option_grid_foundations.py -q`
Expected: all pass.

Run the launcher's own option tests:
`PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python -m pytest testplatform/backend/tests -q -k "launcher or option"`
Expected: no NEW failures. Any test asserting a price gate is a genuine casualty of this change — report it, do not silently delete it.

- [ ] **Step 5: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/test_option_grid_foundations.py
git commit -m "feat(grid): gate option entries on expected profit, not the analyst target range"
```

---

### Task 2: Share the two expert-independent condition ids

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (`_relative_volume_gate` ~3073, the confidence leaf in `_option_entry_rule` ~3121)
- Rewrite: `testplatform/backend/tests/test_launcher_volume_vol_genes.py`
- Test: `testplatform/backend/tests/test_option_grid_foundations.py` (extend)

**Apply the shared id in BOTH job shapes, not only in groups.** A single-structure job has one
member, so sharing changes no gene count there — but it makes the gene KEY identical between the
single and group shapes, which is exactly what the later seeding work needs. Doing it only for
groups would leave stage-1's `cond:o_lc-rel_volume:*` unknown in the stage-2 space, and
`encode_params` drops unknown keys silently.

- [ ] **Step 1: Write the failing test**

Append to `testplatform/backend/tests/test_option_grid_foundations.py`:

```python
SHARED = ["rel_volume", "gate_confidence"]


@pytest.mark.parametrize("gate", SHARED)
def test_the_expert_independent_gates_are_shared_across_group_members(gate):
    """One gene for the whole family, not one per member.

    These two are shared because their SEMANTICS do not vary by structure -- the launcher's own
    comment on rel_volume says "the searched threshold is the only per-half difference, and there
    is none". iv_rank and iv_to_realized_vol are NOT shared and must not be: their operator flips
    between debit (`<`, buy cheap vol) and credit (`>`, sell rich vol) members, and the GA never
    searches an operator, so a shared gate there is not expressible at all.
    """
    genes = [g for g in _space(_launcher(), "OS1") if gate in g]
    assert genes, f"{gate} produced no genes at all"
    assert all(g.startswith(f"cond:shared-{gate}") for g in genes), (
        f"{gate} is still per-member: {sorted(genes)}")
    assert len(genes) == 2, f"expected exactly value+enabled, got {sorted(genes)}"


@pytest.mark.parametrize("gate", ["iv_rank", "iv_rv"])
def test_the_direction_dependent_gates_stay_per_member(gate):
    genes = [g for g in _space(_launcher(), "OS1") if gate in g]
    assert not any("shared-" in g for g in genes), (
        f"{gate}'s operator flips debit/credit; sharing it is not expressible")
    assert len(genes) == 10, f"OS1 has 5 members, so {gate} should emit 10 genes"


@pytest.mark.parametrize("gate", SHARED)
def test_single_and_group_jobs_use_the_SAME_key_for_a_shared_gate(gate):
    """The seeding requirement. A stage-1 single-structure winner is later encoded into the
    stage-2 group space; a key present in one and absent from the other is dropped silently by
    encode_params, so the shared gates must key identically in both shapes."""
    m = _launcher()
    single = {g for g in _space(m, "O_LC") if gate in g}
    group = {g for g in _space(m, "OS1") if gate in g}
    assert single == group, f"{gate} keys differ: single={sorted(single)} group={sorted(group)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python -m pytest testplatform/backend/tests/test_option_grid_foundations.py -q -k shared`
Expected: fails — the ids are still `o_lc-rel_volume` etc.

- [ ] **Step 3: Write the implementation**

Change `_relative_volume_gate` (~3073) to take no member prefix:

```python
def _relative_volume_gate() -> dict:
    """The relative-volume entry gate leaf. SHARED across every member of a group.

    Shared because its semantics genuinely do not vary by structure: real participation behind
    the signal confirms a trade whether you are buying or selling premium, and this gate has no
    per-half difference at all -- it was replicated per member as pure duplication.

    Contrast `_iv_rank_gate` / `_iv_rv_gate`, which must stay per-member: their operator flips
    between debit and credit halves and the GA never searches an operator, so one shared node
    cannot express both.

    The id is deliberately the same in a SINGLE-structure job and in a group, so a stage-1
    winner's gene keys survive being encoded into the stage-2 group space.
    """
    return {"id": "shared-rel_volume", "field": "relative_volume", "op": ">",
            "optimize": True, "toggle_optimize": True, **_RELATIVE_VOLUME_GATE}
```

In `_option_entry_rule`, change the confidence leaf's id and the `_relative_volume_gate` call:

```python
            {"id": "shared-gate_confidence", "field": "confidence", "op": ">", "value": 50,
             "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5,
             "toggle_optimize": True},
            _iv_rank_gate(m, member),
            _relative_volume_gate(),
```

Amend the `_option_entry_rule` docstring line that says ids "are prefixed with the member key so
a GROUP of these rules yields uniquely-keyed genes per member" — true for all leaves except these
two, and the exception is the point.

- [ ] **Step 4: Rewrite the test that asserts the opposite**

`testplatform/backend/tests/test_launcher_volume_vol_genes.py` has
`test_every_group_member_searches_it_independently`, parametrized over `_GATES`, asserting a
PER-MEMBER `rel_volume` gene. That assertion is now wrong for `rel_volume` and still right for
`iv_rank`/`iv_rv`.

Do NOT delete it. Split it: keep the per-member assertion for the direction-dependent gates, and
add a shared assertion for `rel_volume`, with a comment recording why the two differ (the
operator flip). Read the file and adapt to its actual structure.

- [ ] **Step 5: Run and commit**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python -m pytest testplatform/backend/tests -q -k "launcher or option"`
Expected: all pass.

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/
git commit -m "feat(grid): share the two expert-independent condition ids across group members"
```

---

### Task 3: `O_WHEEL`

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (near `_build_strategy_covered_call` ~3228, `_STRATEGY_BUILDERS` ~3295, `_OPTION_STRATEGY_KEYS` ~2350)
- Test: `testplatform/backend/tests/test_option_grid_foundations.py` (extend)

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_wheel_is_a_registered_strategy():
    m = _launcher()
    assert "O_WHEEL" in m._STRATEGY_BUILDERS
    assert "O_WHEEL" in m._OPTION_STRATEGY_KEYS


def test_wheel_enters_by_selling_a_put():
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    actions = [a.get("action_type") for r in s.entry_rules for a in (r.get("actions") or [])]
    assert "sell_cash_secured_put" in actions, f"wheel entry actions were {actions}"


def test_wheel_writes_calls_ONLY_against_assigned_shares():
    """The distinction that makes it a wheel.

    Gating on has_position would write calls against any stock the expert holds;
    has_assigned_shares writes them only against shares the wheel's own put put you into. The
    condition exists and is tested as a rule trigger in tests/test_wheel_assignment_order.py.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    fields = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if node.get("field"):
                fields.append(node["field"])
            for v in node.values():
                walk(v)

    walk(s.exit_rules)
    assert "has_assigned_shares" in fields, f"wheel overlay gates on {sorted(set(fields))}"
    cc_rules = [r for r in s.exit_rules
                if any(a.get("action_type") == "sell_covered_call" for a in (r.get("actions") or []))]
    assert cc_rules, "wheel has no covered-call overlay rule"


def test_wheel_overlay_is_reachable_not_appended():
    """The bug that made every historical O_CC number a mislabelled equity run.

    An overlay appended AFTER S2's floor stop can never fire -- that rule is conditioned only on
    has_position, matches every managed position, and declares no toggle gene so the GA cannot
    route around it. O_CC and O_PP, two OPPOSITE strategies, produced byte-identical top-5
    results with zero trades carrying a contract symbol because of it. The overlay must be
    SPLICED before the first stop-adjusting rule.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    ids = [r.get("id") for r in s.exit_rules]
    assert "cc_sell" in ids, f"no overlay rule in {ids}"
    assert ids.index("cc_sell") < len(ids) - 1, (
        f"the overlay is LAST in the exit list, which is the appended-and-unreachable shape: {ids}")


def test_wheel_guards_against_stacking_a_second_call():
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    guards = [r for r in s.exit_rules
              if any(a.get("action_type") == "stop_processing" for a in (r.get("actions") or []))]
    assert guards, "no stop_processing guard; the overlay will re-fire every manage cycle"
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `KeyError: 'O_WHEEL'` / assertion failures on the registry tests.

- [ ] **Step 3: Write the implementation**

Add after `_build_strategy_covered_call`:

```python
def _build_strategy_wheel(kind: str):
    """O_WHEEL — sell a cash-secured put; when it is ASSIGNED, write calls against the shares.

    A composition of two existing builders, not new machinery: `O_CSP`'s pure-option entry rule
    plus `O_CC`'s guard/overlay pair, with ONE deliberate change — the overlay is gated on
    ``has_assigned_shares`` rather than ``has_position``.

    That gate IS the wheel. ``has_position`` would write calls against any stock the expert
    happens to hold, including shares bought outright by some other rule; ``has_assigned_shares``
    writes them only against shares this strategy's own put delivered. The condition exists and
    is covered as a rule trigger by tests/test_wheel_assignment_order.py.

    SPLICED, never appended (OPT-B1). An overlay appended after S2's floor stop can never fire:
    that rule matches every managed position and declares no toggle gene, so the GA cannot route
    around it either. That defect is why O_CC and O_PP — opposite strategies — once produced
    byte-identical top-5 results with zero trades carrying a contract symbol.

    NOT round-lot constrained, unlike O_CC/O_PP: the shares arrive from assignment in exact
    100-share lots by construction, so there is no odd-lot entry to floor.
    """
    s = _build_strategy_option("O_CSP")
    s.exit_rules = _insert_option_overlay(
        s.exit_rules,
        {"id": "cc_guard",
         "conditions": {"type": "AND", "conditions": [
             {"id": "cc_guard_has_cc", "field": "has_covered_call"}]},
         "actions": [{"action_type": "stop_processing"}],
         "continue_processing": False},
        {"id": "cc_sell",
         "conditions": {"type": "AND", "conditions": [
             {"id": "cc_assigned", "field": "has_assigned_shares"}]},
         "actions": [_option_overlay_action(
             "sell_covered_call", strike_param=5.0,
             strike_min=2.0, strike_max=12.0, strike_step=2.0)],
         "continue_processing": True})
    return s
```

Register it in `_STRATEGY_BUILDERS` beside the other option rows:

```python
    "O_WHEEL": _build_strategy_wheel,
```

and add `"O_WHEEL"` to the `_OPTION_STRATEGY_KEYS` set beside `O_CC`/`O_PP`/`O_STK`:

```python
_OPTION_STRATEGY_KEYS = _PURE_OPTION_STRATEGIES | {"O_CC", "O_PP", "O_STK", "O_WHEEL"}
```

**Check `_build_strategy_option`'s signature before using it** — Task 1's survey saw
`_build_strategy_option(kind)` at ~2915, but verify, and verify `_option_overlay_action`'s
signature at ~2762 rather than assuming the kwargs above.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend ./venv/bin/python -m pytest testplatform/backend/tests/test_option_grid_foundations.py -q`
Expected: all pass.

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python -m pytest testplatform/backend/tests/test_option_strategy_builders.py -q`
Expected: pass. If it enumerates the strategy set exhaustively it will need `O_WHEEL` added — that is a mechanical extension, not a weakening.

- [ ] **Step 5: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/
git commit -m "feat(grid): add O_WHEEL -- sell puts, write calls against assigned shares"
```

---

### Task 4: A gates-off flag for the smoke stage

Stage 0a must run with every optional condition gate disabled, so that "traded nothing" can only
mean data or wiring. No such flag exists.

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (`_option_entry_rule`, and the `optimize` argparse block ~4436)
- Test: `testplatform/backend/tests/test_option_grid_foundations.py` (extend)

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_gates_off_disables_every_optional_entry_gate():
    """Stage 0a's purpose: separate 'the plumbing is broken' from 'the strategy is bad'.

    iv_rank and iv_to_realized_vol fail CLOSED when IV is unmeasurable, and an options cache
    without greeks makes every gated individual trade nothing and score the zero-trade sentinel.
    With the gates off, 'traded nothing' can only mean data or wiring.
    """
    m = _launcher()
    rule = m._option_entry_rule("O_LC", gates_off=True)
    leaves = rule["conditions"]["conditions"]
    optional = [c for c in leaves if c.get("toggle_optimize")]
    assert optional, "no toggleable gates found; the test is measuring the wrong thing"
    assert all(c.get("enabled") is False for c in optional), (
        f"these gates are still on: {[c['id'] for c in optional if c.get('enabled') is not False]}")


def test_gates_off_leaves_the_structural_conditions_ALONE():
    """`has_no_position` is not a strategy gate, it is a correctness guard. Disabling it would
    let the smoke run stack duplicate positions and mask the very plumbing it is testing."""
    m = _launcher()
    rule = m._option_entry_rule("O_LC", gates_off=True)
    flat = [c for c in rule["conditions"]["conditions"] if c["id"].endswith("-flat")]
    assert flat and flat[0].get("enabled") is not False


def test_gates_off_defaults_to_false():
    m = _launcher()
    normal = m._option_entry_rule("O_LC")
    assert not any(c.get("enabled") is False for c in normal["conditions"]["conditions"])
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `TypeError: _option_entry_rule() got an unexpected keyword argument 'gates_off'`.

- [ ] **Step 3: Write the implementation**

Give `_option_entry_rule` a `gates_off` keyword and apply it after the leaf list is built:

```python
def _option_entry_rule(member: str, *, toggleable: bool = False,
                       gates_off: bool = False) -> dict:
```

and immediately before `return rule`:

```python
    if gates_off:
        # REMOVE the leaves, do not mark them. `ConditionLeaf.to_canonical_dict` rebuilds leaves
        # from declared fields, so `normalize_trade_rules` DROPS an `enabled` key and the marker
        # never reaches the engine — `_apply_to_tree` disables a gate by deleting the node.
        # SMOKE MODE. Turn every OPTIONAL gate off so the run exercises the pipeline rather than
        # the strategy. `toggle_optimize` is precisely the marker for "the GA may switch this
        # off", which makes it the right discriminator: a leaf carrying it is a strategy opinion,
        # a leaf without it is a correctness guard (`has_no_position`) that must stay on — with
        # it off, a smoke run would stack duplicate positions and mask the plumbing it is testing.
        rule["conditions"]["conditions"] = [
            leaf for leaf in rule["conditions"]["conditions"]
            if not leaf.get("toggle_optimize")]
```

Thread it from the CLI. Add to the `optimize` parser (~4436):

```python
    op.add_argument("--gates-off", action="store_true",
                    help="Disable every OPTIONAL option-entry condition gate (smoke runs). "
                         "iv_rank and iv_to_realized_vol fail CLOSED when IV is unmeasurable, so "
                         "on a cache without greeks a gated individual trades nothing and scores "
                         "the zero-trade sentinel; with the gates off, 'traded nothing' can only "
                         "mean data or wiring. Correctness guards stay on.")
```

Then thread `gates_off` from `args` through `_build_strategy` to `_option_entry_rule`. **Read the
call chain before editing** — `_build_strategy(kind, name, expert)` dispatches through
`_STRATEGY_BUILDERS[kind](kind)` for option kinds, so the flag has to reach the builders. The
least invasive route is a module-level toggle set at command entry, matching how
`_OPTION_MIN_VOLUME_DEFAULT` is already handled (see `_apply_option_min_volume`). Follow that
existing pattern rather than changing every builder's signature.

- [ ] **Step 4: Run and commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/
git commit -m "feat(grid): --gates-off for smoke runs, disabling optional entry gates only"
```

---

### Task 5: Raise the per-instrument cap and sizing band for option jobs only

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (`_RM_OPT` ~1352, its two spread sites ~3635 and ~3830, `_OPTION_SIZING_BANDS` ~2670)
- Rewrite: `testplatform/backend/tests/test_launcher_option_sizing_gene.py`
- Test: `testplatform/backend/tests/test_option_grid_foundations.py` (extend)

A cash-secured put at spot \$100 reserves exactly \$10,000 — **50%** of a \$20k account. Budget is
`equity × min(option_sizing%, per_instrument_cap%)`, so **both** ranges must reach 50%; raising
one alone does nothing.

`_RM_OPT` is a single global dict spread inline at two sites and shared with every equity
strategy, so it must be overridden per job rather than edited.

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_option_jobs_can_size_a_full_notional_structure_at_spot_100():
    m = _launcher()
    cap = m._rm_opt_for("O_CSP")["max_virtual_equity_per_instrument_percent"]
    assert cap["max"] >= 50.0, (
        f"per-instrument cap tops out at {cap['max']}%; a cash-secured put at spot 100 reserves "
        f"$10,000, i.e. 50% of a $20k account, so it can never open")


def test_equity_jobs_keep_the_original_cap():
    """The same setting is read by the equity risk manager. Raising it globally would move every
    equity grid and make new results incomparable to old ones."""
    m = _launcher()
    assert m._rm_opt_for("S2")["max_virtual_equity_per_instrument_percent"]["max"] == 30.0


def test_the_full_notional_sizing_band_reaches_fifty_percent():
    m = _launcher()
    lo, hi, step = m._OPTION_SIZING_BANDS[20.0]
    assert hi >= 50.0, f"full-notional option_sizing band tops out at {hi}%"


def test_raising_one_range_alone_would_not_help():
    """Documents WHY both move: the budget is the MIN of the two."""
    m = _launcher()
    cap = m._rm_opt_for("O_CSP")["max_virtual_equity_per_instrument_percent"]["max"]
    sizing = m._OPTION_SIZING_BANDS[20.0][1]
    assert min(cap, sizing) >= 50.0
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `AttributeError: module has no attribute '_rm_opt_for'`.

- [ ] **Step 3: Write the implementation**

Add beside `_RM_OPT`:

```python
#: Option jobs need a higher per-instrument ceiling than equity ones, and the setting is shared.
#:
#: A cash-secured put at spot $100 reserves strike*100 = $10,000, exactly 50% of the grid's $20k
#: account. The sizing budget is `equity * min(option_sizing%, max_virtual_equity_per_instrument
#: _percent%)`, so BOTH ranges must reach 50% — raising either alone changes nothing. At the old
#: 30% ceiling the full-notional structures topped out at spot $60 and could not open on most of
#: a large-cap universe.
#:
#: SCOPED, not global: the classic equity risk manager reads the same setting, so editing
#: `_RM_OPT` in place would move every equity grid and make new results incomparable to old.
_OPTION_RM_OVERRIDE = {
    "max_virtual_equity_per_instrument_percent": {
        "optimize": True, "min": 5.0, "max": 50.0, "step": 5.0, "type": "float"},
}


def _rm_opt_for(kind: str) -> dict:
    """The classic-RM gene block for a strategy kind: `_RM_OPT`, plus the option override.

    EXCLUDE `O_STK`. It is inside `_OPTION_STRATEGY_KEYS` but it is
    `_build_strategy_stock` -> `_build_strategy_S2`, i.e. the plain-equity BASELINE the option
    strategies are measured against; widening its cap destroys the control arm. `O_CC`/`O_PP`
    DO get the raise — a covered call funds 100 shares, $10,000 at spot $100, the same
    constraint as a CSP. Gating on `_PURE_OPTION_STRATEGIES` looks right and is wrong for that
    reason.
    """
    if kind in _OPTION_STRATEGY_KEYS and kind != "O_STK":
        return {**_RM_OPT, **_OPTION_RM_OVERRIDE}
    return dict(_RM_OPT)
```

Replace `**_RM_OPT` with `**_rm_opt_for(strat_kind)` at BOTH spread sites (~3635, ~3830). **Check
what the kind variable is called at each site** — it may be `args.strategy`, `strat_kind` or a
loop variable; use whatever is in scope.

Then widen the full-notional sizing band in `_OPTION_SIZING_BANDS` (~2670):

```python
    20.0: (5.0, 50.0, 5.0),    # neutral credit + full-notional
```

- [ ] **Step 4: Rewrite the test that pins the old ceiling**

`testplatform/backend/tests/test_launcher_option_sizing_gene.py:87` asserts `spec["max"] <= 40.0`.
Update it to 50.0 and add a comment recording why the ceiling moved (the \$10,000 CSP at spot
100). Do not weaken it to an inequality with no upper bound — the point of the assertion is that
the band is bounded.

- [ ] **Step 5: Run and commit**

Run: `PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python -m pytest testplatform/backend/tests -q -k "launcher or option or sizing"`

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/
git commit -m "feat(grid): raise the per-instrument cap and full-notional sizing band to 50% for option jobs"
```

---

### Task 6: Fix the stale `optimize-batch --fitness` help

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (~4645)

`_resolve_fitness` correctly returns `option_consistent_annual_return` for pure-option kinds, but
the `optimize-batch` help still says `consistent_annual_return`. A one-line docs bug that would
mislead whoever reads `--help` before launching a multi-day grid.

- [ ] **Step 1: Confirm the code is right and the help is wrong**

Run: `grep -n "option_consistent_annual_return" testplatform/ba2test_launcher.py | head -3`
Expected: `_resolve_fitness` returns it for `_PURE_OPTION_STRATEGIES`.

- [ ] **Step 2: Fix the help text**

Change the `ob.add_argument("--fitness", ...)` help so it reads
`'option_consistent_annual_return' for pure-option kinds (OS1-OS4/O_*), 'calmar_ratio' for stock
kinds` — matching `_resolve_fitness`.

- [ ] **Step 3: Commit**

```bash
git add testplatform/ba2test_launcher.py
git commit -m "docs(grid): optimize-batch --fitness help said the wrong metric for option kinds"
```

---

### Task 7: Verify and bump

- [ ] **Step 1: Measure the genomes and record them**

Run the genome snippet from the Orientation section for `O_LC`, `O_IC`, `O_CSP`, `O_WHEEL`, `OS1`.
Expected, after Tasks 1 and 2: single structures at or under 26, `OS1` at or under 95. Record the
actual numbers in the commit message — the grid spec's population figures are derived from them.

- [ ] **Step 2: Run all suites**

```bash
./venv/bin/python -m pytest packages/common/tests/ -q                     # >= 2402
./venv/bin/python -m pytest tests/ -q                                     # 4410
PYTHONPATH=packages/common:packages/providers:packages/experts ./venv/bin/python \
  -m pytest testplatform/backend -q --ignore=testplatform/backend/tests_scripts \
  --ignore=testplatform/backend/scripts
```

Expected: the backend suite up by the new tests, with only the known Windows-only
`test_worker_server.py::test_logs_rejects_path_traversal` failing.

- [ ] **Step 3: Bump the version**

`testplatform/` changed, so bump `TEST_APP_VERSION` by 1. **Read the current value first** —
other machines push to this file and it moves between sessions.

- [ ] **Step 4: Commit and push**

```bash
git add testplatform/version.py
git commit -m "chore: bump TEST_APP_VERSION for the option grid foundations"
git push
```

---

## What this plan does NOT do

- **Cross-job seeding.** The largest remaining item, and the naive version is broken: `genetic.py:377` fills any gene absent from a seed with `config['min']`, and an `enabled` gene's min is 0, so seeding a group job from single-structure winners sets every member toggle OFF and the seed trades nothing. Its own plan.
- **`OS_ALL`.** Needs the exit-band decision first: `_option_exit_rules` branches on the group key and `OS_ALL` is not in `_DEBIT_OPTION_KINDS`, so it would hand credit exit bands to its debit members. Registering it also fails a live test asserting the full-notional three belong to no group.
- **Universe tooling.** No tool builds a price-capped universe list, `--screener-cap-band` is unreachable for pure-option jobs, and ETFs are excluded from the metric store at build time.
- **The driver script.** `optimize-batch` cannot express the grid and polls forever without `serve` running.
- **Expert-settings seeding.** Deferred until the running equity GA jobs finish — there is nothing to seed from.

## Self-review notes

- Spec §4 (condition tiers) → Task 2. §6.1 (signal experts) → Task 1, which removes the FMPRating coupling that blocked DeterministicScorer. §6 (capital) → Task 5. §7 items 1, 2, 6 → Tasks 3, 2, 6. §7.1's gates-off gap → Task 4.
- Every code step carries real code. Three steps deliberately say "read the call chain / check the signature before editing" rather than inventing one — Task 3's `_build_strategy_option` and `_option_overlay_action`, Task 4's flag threading, and Task 5's two spread sites. Those are places the survey gave a location but not a verified signature, and guessing would be worse than looking.
- Types and helpers used in later tasks (`_rm_opt_for`, `_expected_profit_gate`, `_build_strategy_wheel`, `gates_off`) are all defined in the task that introduces them.
