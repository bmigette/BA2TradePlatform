# Option Market-Condition Genes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development from the controlling session) to implement this plan task-by-task.

**Goal:** Give every option structure three searchable entry gates on the underlying's prior-session market state (trend slope, ADX14, RV5/RV20) with a `mode` gene (off/below/above) and a `value` gene each, fed from a central, warmed, host-shared feature store, behind an opt-in launcher profile that leaves every existing run byte-identical.

**Architecture:** Pure calculators and a portable feature store live in `ba2_common`; provider-fetch orchestration in `ba2_providers`; backtest/live adapters supply an immutable `MarketConditionContext` through a resolver seam (the same pattern as `TradeConditions.set_provider_resolver`) so old conditions are untouched; the GA learns a categorical `cond:<id>:mode` gene using the existing `"choice"` gene shape; the launcher appends three leaves per structure only when `--market-condition-profile ohlcv-v1` is set. Feature rows are computed once per (source profile, symbol, session, window digest, calculator version), published as immutable parquet objects plus a manifest under `CACHE_FOLDER/market_conditions/ohlcv-v1/`, mirrored by the existing cache sync, and mapped per host through `DerivedArrayStore`.

**Tech Stack:** Python 3.12, numpy, pandas, pyarrow (parquet), pydantic v2 (`rule_models`), pytest; TypeScript (`testplatform/frontend`, type declarations only); existing `ba2_common.core.shared_arrays.DerivedArrayStore`, `market_calendar.nyse_regular_sessions`, `replay.clock.replay_now`.

**Design:** `docs/plans/2026-09-15-option-market-condition-genes-design.md` (sections cited as D§n). Every task's implementer gets the relevant design sections pasted verbatim.

**Worktree / commands:** all work in `C:/Users/basti/Documents/dev/BA2-mktcond` (branch `feat/option-market-conditions`, based on `origin/dev` cb584a60). Python: `C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe` (call it `$PY`). Test invocations (NEVER concurrently, one at a time):

| Suite | Run from | Command |
|---|---|---|
| shared package | `packages/common` | `$PY -m pytest tests/<file>.py -q -p no:cacheprovider` |
| providers | `packages/providers` | same |
| backend | `testplatform/backend` | `$PY -m pytest tests/<file>.py -q -p no:cacheprovider` (pytest.ini `pythonpath` maps `app`, `ba2test_launcher` and the three packages to THIS worktree) |
| root | repo root | `$PY -m pytest tests/<file>.py -q -p no:cacheprovider` |

Verified: from `packages/common`, `import ba2_common` resolves to the worktree. Backend `tests/backtest` and `tests/replay` must be separate invocations (see memory `worktree-test-running-quirks`). Commits: conventional prefix, end with the session attribution trailer the controller supplies. **Never bump `testplatform/version.py` or `ba2_trade_platform/version.py` in this branch** (done once at merge).

**Non-goals (D§1, D§9):** no change to DeterministicScorer's own ADX/EMA helpers; no change to existing conditions' provider calls; no fitness/sizing/TP/SL change; profile `none` emits exactly today's rules and genes; nothing runs on prod or the remote grids from this branch.

---

## Cross-cutting contracts (read before any task)

**Canonical field names (D§3):** `underlying_trend_slope_50_atr14`, `underlying_adx_14`, `underlying_realized_vol_ratio_5_20`. These are the `field` strings in condition leaves, the `ExpertEventType` enum VALUES, the `FIELD_EVENT` keys, and the feature-store column names. One spelling everywhere.

**Observation status enum (D§7):** `valid`, `insufficient_history`, `missing_session`, `invalid_prices`, `no_context`, `missing_replay_object`. An observation is `(value: float | None, status: str, reason: str)`. Only `valid` carries a value. **Unknown never passes an active comparison** (D§5).

**Window (D§3):** exactly 128 consecutive regular sessions ending at the prior session (D§4 `prior_session_v1`). Index 0..127. A calculator receives float64 arrays `o,h,l,c,v` of length 128 and returns the three observations; it never reads more bars than 128 (window invariance, D§8.2).

**Gene names (D§5):** `cond:<leaf-id>:mode` (choice gene over the leaf's `mode_choices`) and `cond:<leaf-id>:value` (range). **Two leaf kinds (D§5 amendment 2):** a NUMERIC leaf declares `mode_choices == ["off","below","above"]` plus `value/value_min/value_max/value_step`; a CATEGORICAL leaf (v1: `structure_state`) declares `mode_choices == ["off", <allowed values...>]` (e.g. `["off","bull","bear"]`), NO threshold fields, and decodes a non-off mode to an equality leaf `op "=="` whose `value` is the field's integer CODE for that choice (codes live in the field registry, see the profile registry below). Rules enforced at template load: first choice is always `"off"`; `"none"` is never a choice; a categorical leaf with any `value*` field, or a numeric leaf whose choices are not exactly off/below/above, is rejected; a `mode` token must be one of the leaf's choices.

**Profile registry (D§3, D§3.2, amendment 2):** `market_conditions.py` owns a registry `PROFILES: Dict[str, ProfileSpec]` where `ProfileSpec(name, calc_version, fields: Tuple[FieldSpec,...])` and `FieldSpec(name, kind: "numeric"|"categorical", short, searched: bool, value_min, value_max, value_step, anchor_op, anchor_value, codes: Optional[Mapping[str,int]] (read-only, hashable), ui_name)`; tests register throwaway profiles through the `registered_profile(spec)` context manager (restores `PROFILES` on exit). **Where a leaf's `mode_choices` is checked against the registry codes:** `rule_models` deliberately does not import the registry, so Task 3's collector (`_walk_condition_nodes`) and Task 8's launcher builder both assert `list(leaf["mode_choices"]) == ["off", *field_spec(field).codes]` for categorical leaves and raise otherwise; a deployed/exported categorical leaf may carry `mode` without `mode_choices` and is accepted by the model as long as the token is not `none`/`below`/`above`. `ohlcv-v1` (Task 1's three fields) is registered in Task 2; `ta-structure-v1` (twelve fields, D§3.2 table) in Task 10. Store, reader, conditions, launcher and report iterate `PROFILES[...].fields`, never a hard-coded trio, so the second profile is data, not code paths. Storage is float64 for every field (D§3.2 says "float32 per field per row" as a cost estimate; D§3.1/D§4.3 require unrounded float64 comparisons and this plan keeps float64 — flagged to the operator, not silently decided). Categorical fields are stored as their integer code in a float64 column; status columns are int8. Leaf ids: `<member>-market-slope`, `<member>-market-adx`, `<member>-market-rv` where `<member>` is the structure key lower-cased (`o_lc`, `o_ic`, …); O_CC → `o_cc-market-*`, O_PP → `o_pp-market-*`, O_WHEEL → `o_wheel-market-*` (D§5: wheel gets its own ids even though its entry is the CSP builder).

**Version strings:** `CALC_VERSION = "ohlcv-v1/calc-1"`, `SOURCE_PROFILE = "fmp-daily-split-adjusted-v1"` (Task 5 certifies the FMP cache columns; if certification fails the profile name is not used and preflight fails), `TIMING_POLICY = "prior_session_v1"`, `LAYOUT_VERSION = 1` (local mapped-array layout).

---

### Task 1: Pure calculators and observation types

**Files:**
- Create: `packages/common/ba2_common/core/market_conditions.py`
- Test: `packages/common/tests/test_market_conditions_calculators.py`

**Context.** D§3 and D§3.1 are the numerical contract. This module is pure (numpy only, no I/O, no provider, no logger side effects). Everything downstream (store, conditions, report) imports these names; do not put store or context code here.

**Step 1: Write the failing tests.** Cover, with hand-derived expected numbers (compute them in the test with an independent straightforward loop, not by calling the module under test):

```python
# packages/common/tests/test_market_conditions_calculators.py
import math
import numpy as np
import pytest
from ba2_common.core.market_conditions import (
    WINDOW, CALC_VERSION, Observation, MarketConditionValues,
    compute_market_conditions, ema_wilder_seeded, atr14_wilder, adx14_wilder,
    STATUS_VALID, STATUS_INSUFFICIENT_HISTORY, STATUS_INVALID_PRICES,
    FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO,
)

def _bars(closes, spread=0.5):
    c = np.asarray(closes, dtype=np.float64)
    h = c + spread; l = c - spread; o = c.copy(); v = np.full(len(c), 1e6)
    return o, h, l, c, v

def test_window_constant_is_128_and_field_names_are_canonical():
    assert WINDOW == 128
    assert FIELD_TREND_SLOPE == "underlying_trend_slope_50_atr14"
    assert FIELD_ADX == "underlying_adx_14"
    assert FIELD_RV_RATIO == "underlying_realized_vol_ratio_5_20"

def test_rising_path_has_positive_slope_and_falling_negative():
    up = _bars(np.linspace(100, 160, WINDOW))
    down = _bars(np.linspace(160, 100, WINDOW))
    r_up = compute_market_conditions(*up); r_dn = compute_market_conditions(*down)
    assert r_up.trend_slope.status == STATUS_VALID and r_up.trend_slope.value > 0
    assert r_dn.trend_slope.status == STATUS_VALID and r_dn.trend_slope.value < 0

def test_flat_path_with_valid_atr_is_a_real_zero_not_unknown():
    o, h, l, c, v = _bars(np.full(WINDOW, 100.0), spread=1.0)   # TR = 2 every bar, ATR = 2 > 0
    r = compute_market_conditions(o, h, l, c, v)
    assert r.trend_slope.status == STATUS_VALID and r.trend_slope.value == 0.0

def test_trend_slope_matches_independent_reference():
    rng = np.random.default_rng(7)
    c = 100 + np.cumsum(rng.normal(0, 1, WINDOW)); o, h, l, c, v = _bars(c, spread=0.8)
    # independent reference, D§3.1 verbatim
    ema = [None]*WINDOW; ema[49] = float(np.mean(c[:50]))
    for j in range(50, WINDOW): ema[j] = (2/51)*c[j] + (49/51)*ema[j-1]
    tr = [None] + [max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(1, WINDOW)]
    atr = [None]*WINDOW; atr[14] = float(np.mean(tr[1:15]))
    for j in range(15, WINDOW): atr[j] = (13*atr[j-1] + tr[j])/14
    expected = (ema[127]-ema[122])/(5*atr[127])
    r = compute_market_conditions(o, h, l, c, v)
    assert r.trend_slope.value == pytest.approx(expected, rel=0, abs=1e-12)

def test_adx_intermediates_are_pinned_on_a_reference_path():
    # pin +DI/-DI/DX at index 14 and 27 and ADX at 127 against the same-loop reference
    ...  # implementer: write the loop per D§3.1 (Wilder seed at 14, ADX seed at 27), assert to 1e-12

def test_adx_with_atr_positive_and_both_dm_zero_is_zero_not_unknown():
    # constant highs/lows with nonzero range: +DM = -DM = 0 every bar, ATR > 0 -> DX = 0 -> ADX = 0
    o, h, l, c, v = _bars(np.full(WINDOW, 100.0), spread=1.0)
    r = compute_market_conditions(o, h, l, c, v)
    assert r.adx.status == STATUS_VALID and r.adx.value == 0.0

def test_rv_ratio_uses_ddof1_and_no_annualization():
    rng = np.random.default_rng(3)
    c = 100*np.exp(np.cumsum(rng.normal(0, 0.01, WINDOW))); o, h, l, c, v = _bars(c)
    r = np.diff(np.log(c))
    expected = np.std(r[-5:], ddof=1) / np.std(r[-20:], ddof=1)
    res = compute_market_conditions(o, h, l, c, v)
    assert res.rv_ratio.value == pytest.approx(expected, abs=1e-12)

def test_rv_ratio_zero_numerator_is_valid_zero_and_zero_denominator_is_unknown():
    c = np.r_[100 + np.arange(108.0), np.full(20, 207.0)]  # last 20 closes flat -> both stds 0 -> unknown
    res = compute_market_conditions(*_bars(c))
    assert res.rv_ratio.status == STATUS_INVALID_PRICES and res.rv_ratio.value is None
    c2 = np.r_[100 + np.arange(123.0), np.full(5, 222.0)]  # last 5 flat, last 20 not -> valid 0
    res2 = compute_market_conditions(*_bars(c2))
    assert res2.rv_ratio.status == STATUS_VALID and res2.rv_ratio.value == 0.0

def test_short_window_is_insufficient_history_for_all_three():
    o, h, l, c, v = _bars(np.linspace(100, 110, 100))
    res = compute_market_conditions(o, h, l, c, v)
    assert {res.trend_slope.status, res.adx.status, res.rv_ratio.status} == {STATUS_INSUFFICIENT_HISTORY}

def test_nonfinite_or_nonpositive_price_is_invalid_prices_never_substituted():
    o, h, l, c, v = _bars(np.linspace(100, 110, WINDOW)); c[60] = np.nan
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.value is None and res.trend_slope.status == STATUS_INVALID_PRICES
    o, h, l, c, v = _bars(np.linspace(100, 110, WINDOW)); c[5] = 0.0
    assert compute_market_conditions(o, h, l, c, v).rv_ratio.status == STATUS_INVALID_PRICES

def test_ohlc_ordering_violation_is_invalid_prices():
    o, h, l, c, v = _bars(np.linspace(100, 110, WINDOW)); h[30] = l[30] - 1
    assert compute_market_conditions(o, h, l, c, v).adx.status == STATUS_INVALID_PRICES

def test_window_invariance_extra_history_does_not_change_values():
    rng = np.random.default_rng(11); c = 100 + np.cumsum(rng.normal(0, 1, 300))
    full = _bars(c); tail = tuple(a[-WINDOW:] for a in full)
    a = compute_market_conditions(*tail)
    b = compute_market_conditions(*tuple(x[-WINDOW:] for x in full))   # callers must pre-slice: passing 300 bars RAISES
    assert a == b
    with pytest.raises(ValueError):
        compute_market_conditions(*full)

def test_split_adjustment_case_documented_as_caller_responsibility():
    # a 2:1 split inside the window (unadjusted) produces a TR spike; the calculator does not
    # try to detect it -- it is the SOURCE contract's job (D§4). Pin that the value is simply
    # what the arithmetic says, so the certification test in Task 5 is the only guard.
    ...
```

Also a `MarketConditionValues.as_row()` test: returns a dict with the three canonical field names → value-or-None and `<field>_status` → status, plus `calc_version`.

**Step 2: Run to verify failure.** From `packages/common`: `$PY -m pytest tests/test_market_conditions_calculators.py -q -p no:cacheprovider` → ImportError.

**Step 3: Implement.**

```python
# packages/common/ba2_common/core/market_conditions.py
"""Pure market-condition calculators (D§3, D§3.1). No I/O. Fixed 128-session window."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
import math
import numpy as np

WINDOW = 128
CALC_VERSION = "ohlcv-v1/calc-1"
FIELD_TREND_SLOPE = "underlying_trend_slope_50_atr14"
FIELD_ADX = "underlying_adx_14"
FIELD_RV_RATIO = "underlying_realized_vol_ratio_5_20"
FIELDS = (FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO)

STATUS_VALID = "valid"
STATUS_INSUFFICIENT_HISTORY = "insufficient_history"
STATUS_MISSING_SESSION = "missing_session"
STATUS_INVALID_PRICES = "invalid_prices"
STATUS_NO_CONTEXT = "no_context"
STATUS_MISSING_REPLAY_OBJECT = "missing_replay_object"
STATUSES = (STATUS_VALID, STATUS_INSUFFICIENT_HISTORY, STATUS_MISSING_SESSION,
            STATUS_INVALID_PRICES, STATUS_NO_CONTEXT, STATUS_MISSING_REPLAY_OBJECT)

@dataclass(frozen=True)
class Observation:
    value: Optional[float]
    status: str
    reason: str = ""
    def __post_init__(self):
        if self.status not in STATUSES: raise ValueError(f"unknown status {self.status!r}")
        if (self.status == STATUS_VALID) != (self.value is not None):
            raise ValueError("a valid observation carries a value; an invalid one carries none")

@dataclass(frozen=True)
class MarketConditionValues:
    trend_slope: Observation
    adx: Observation
    rv_ratio: Observation
    calc_version: str = CALC_VERSION
    def as_row(self) -> Dict[str, Any]: ...
    def by_field(self) -> Dict[str, Observation]:
        return {FIELD_TREND_SLOPE: self.trend_slope, FIELD_ADX: self.adx, FIELD_RV_RATIO: self.rv_ratio}

def _validate(o, h, l, c, v) -> Optional[str]:
    """Return None when the window is usable, else the invalidity reason. Exactly WINDOW bars
    (fewer -> insufficient history is decided by the caller of compute_*; more -> ValueError:
    the caller must pre-slice so window invariance is structural, not incidental)."""
    ...

def ema_wilder_seeded(c: np.ndarray, period: int = 50) -> np.ndarray: ...   # seed at index period-1 with mean(c[:period])
def true_range(h, l, c) -> np.ndarray: ...                                  # index 0 is nan
def atr14_wilder(h, l, c) -> np.ndarray: ...                                # seed at 14 = mean(TR[1..14]); Wilder after
def adx14_wilder(h, l, c) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:  # +DI, -DI, DX, ADX arrays (nan before defined)
def realized_vol_ratio(c) -> Observation: ...
def compute_market_conditions(o, h, l, c, v) -> MarketConditionValues: ...
```

Rules the implementation must honour (they are the tests): arrays must be length exactly `WINDOW` (shorter → all three `insufficient_history`; longer → `ValueError`); any non-finite or non-positive price, or `h < max(o,c)`/`l > min(o,c)`/`h < l` → all three `invalid_prices` with the offending index in `reason`; `ATR[127] <= 0` → slope and ADX `invalid_prices` ("atr<=0"); DX with ATR>0 and both DI exactly 0 → 0; RV: zero denominator → `invalid_prices`, zero numerator with positive denominator → `0.0`.

**Step 4: Run tests → all pass.** Also run `tests/test_condition_registry_coverage.py` to be sure nothing imported broke (it should be unaffected).

**Step 5: Commit.** `feat(market-conditions): pure trend-slope/ADX14/RV-ratio calculators with a fixed 128-session window`

---

### Task 2: `ConditionLeaf` mode-gene metadata (Python + TypeScript types)

**Files:**
- Modify: `packages/common/ba2_common/core/rule_models.py:88-164` (ConditionLeaf)
- Modify: `testplatform/frontend/src/components/ConditionBuilder.tsx:7-24` (`ConditionNode` interface) and every place that copies a `ConditionNode`'s optimizer fields on import/edit/export (grep `toggleOptimize` in `testplatform/frontend/src` — mirror each site).
- Test: `packages/common/tests/test_rule_models_mode_genes.py`

**Context.** D§5 including amendment 2. `ConditionLeaf.to_canonical_dict` rebuilds from DECLARED fields only, so the three new fields MUST be declared (the file's own comment on `value_offset_from` says why). Reject `mode_optimize=True` together with `toggle_optimize=True` (two disable controls). Validation of `mode_choices` (module-level constants `MODE_OFF = "off"`, `NUMERIC_MODE_CHOICES = ("off","below","above")`, `FORBIDDEN_MODE_CHOICES = ("none",)`): the list must be non-empty, start with `"off"`, contain no duplicates and never contain `"none"`; a leaf that carries any threshold field (`value`, `value_min`, `value_max`, `value_step`, `value_offset_from`) is NUMERIC and its choices must equal `NUMERIC_MODE_CHOICES` exactly; a leaf with `mode_optimize` and none of those threshold fields is CATEGORICAL and its choices must contain at least one value besides `"off"` and must not contain `"below"`/`"above"`. A `mode` token must be `"off"` or one of the declared choices when choices are declared, else one of `NUMERIC_MODE_CHOICES`. Add a helper `leaf_mode_kind(leaf_dict) -> "numeric" | "categorical" | None` in `rule_models.py` (used by Task 3 and Task 8) that applies the same rule to a plain dict. This task also registers the `ohlcv-v1` profile in `market_conditions.py` (`ProfileSpec`/`FieldSpec`/`PROFILES` per the cross-cutting contract, with the D§3 ranges) — a data-only addition next to Task 1's constants, with a test that the three FieldSpecs match `FIELDS` and the D§3 ranges. A legacy leaf without these fields is unchanged (assert canonical output equality on an existing fixture).

**Step 1: Failing tests.**

```python
from ba2_common.core.rule_models import ConditionLeaf, normalize_trade_rules
import pytest

def test_mode_metadata_round_trips_through_canonical_dict_in_both_spellings():
    leaf = ConditionLeaf(id="o_ic-market-adx", field="underlying_adx_14", op="<", value=25,
                         optimize=True, value_min=10, value_max=40, value_step=5,
                         mode_optimize=True, mode_choices=["off", "below", "above"])
    out = leaf.to_canonical_dict()
    assert out["mode_optimize"] is True and out["modeOptimize"] is True
    assert out["mode_choices"] == ["off", "below", "above"] and out["modeChoices"] == out["mode_choices"]
    again = ConditionLeaf(**out).to_canonical_dict()
    assert again == out

def test_mode_and_toggle_optimize_together_is_rejected():
    with pytest.raises(ValueError, match="mode_optimize.*toggle_optimize"):
        ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=25,
                      mode_optimize=True, mode_choices=["off","below","above"], toggle_optimize=True)

def test_unknown_mode_value_and_unsupported_choice_list_are_rejected():
    with pytest.raises(ValueError): ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=1, mode="sideways")
    with pytest.raises(ValueError): ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=1, mode_optimize=True, mode_choices=["off","above"])

def test_categorical_leaf_declares_off_plus_values_and_no_threshold():
    ok = ConditionLeaf(id="s1-structure-state", field="structure_state", op="==",
                       mode_optimize=True, mode_choices=["off", "bull", "bear"])
    assert ok.to_canonical_dict()["mode_choices"] == ["off", "bull", "bear"]
    with pytest.raises(ValueError):   # categorical with a threshold
        ConditionLeaf(id="x", field="structure_state", op="==", value=1, mode_optimize=True, mode_choices=["off","bull","bear"])
    with pytest.raises(ValueError):   # "none" is never a choice
        ConditionLeaf(id="x", field="structure_state", op="==", mode_optimize=True, mode_choices=["off","bull","bear","none"])
    with pytest.raises(ValueError):   # first choice must be off
        ConditionLeaf(id="x", field="structure_state", op="==", mode_optimize=True, mode_choices=["bull","off","bear"])
    with pytest.raises(ValueError):   # numeric leaf with categorical choices
        ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=1, mode_optimize=True, mode_choices=["off","bull","bear"])

def test_leaf_mode_kind_helper():
    from ba2_common.core.rule_models import leaf_mode_kind
    assert leaf_mode_kind({"mode_optimize": True, "mode_choices": ["off","below","above"], "value": 1}) == "numeric"
    assert leaf_mode_kind({"mode_optimize": True, "mode_choices": ["off","bull","bear"]}) == "categorical"
    assert leaf_mode_kind({"field": "confidence", "value": 50}) is None

def test_resolved_mode_token_is_preserved_but_never_invented():
    leaf = ConditionLeaf(id="x", field="underlying_adx_14", op="<", value=1, mode="below")
    assert leaf.to_canonical_dict()["mode"] == "below"
    assert "mode" not in ConditionLeaf(id="x", field="confidence", op=">", value=1).to_canonical_dict()

def test_legacy_leaf_canonical_output_is_unchanged():
    legacy = {"id": "shared-gate_confidence", "field": "confidence", "op": ">", "value": 50,
              "optimize": True, "value_min": 40, "value_max": 75, "value_step": 5, "toggle_optimize": True}
    before = {...}  # paste the exact dict to_canonical_dict produces on dev HEAD (compute once, pin literally)
    assert ConditionLeaf(**legacy).to_canonical_dict() == before
```

**Step 3: Implement.** Add to `ConditionLeaf`:

```python
    #: MARKET-CONDITION MODE GENE (design 2026-09-15 §5). ``mode`` is the RESOLVED token a decoded
    #: genome carries ("off" removes the leaf; "below"/"above" set the operator); ``mode_optimize``
    #: + ``mode_choices`` are the optimizer template metadata. DECLARED for the same reason as
    #: value_offset_from: to_canonical_dict would otherwise drop them.
    mode: Optional[str] = None
    mode_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("mode_optimize", "modeOptimize"))
    mode_choices: Optional[List[str]] = Field(default=None, validation_alias=AliasChoices("mode_choices", "modeChoices"))

    @model_validator(mode="after")
    def _validate_mode_metadata(self):
        if self.mode is not None and self.mode not in MODE_CHOICES: raise ValueError(...)
        if self.mode_optimize:
            if self.toggle_optimize: raise ValueError("mode_optimize and toggle_optimize are two independent disable controls; declare one")
            if list(self.mode_choices or []) != list(MODE_CHOICES): raise ValueError(...)
        return self
```

with `MODE_CHOICES = ("off", "below", "above")` at module level, and emit `mode`/`modeOptimize`+`mode_optimize`/`modeChoices`+`mode_choices` in `to_canonical_dict` when not None. TypeScript: add `mode?: 'off'|'below'|'above'; modeOptimize?: boolean; modeChoices?: string[]` to `ConditionNode` and carry them through the same import/export/edit copy sites that carry `toggleOptimize` (no UI control needed in v1; the fields must survive a round trip). Run `npm run build` (or `npx tsc --noEmit`) in `testplatform/frontend` if the toolchain is installed; otherwise state that it was not run.

**Step 5: Commit.** `feat(rules): ConditionLeaf carries mode / mode_optimize / mode_choices; TS ConditionNode mirrors them`

---

### Task 3: Parameter space: collect and decode the `mode` gene

**Files:**
- Modify: `testplatform/backend/app/services/strategy_param_space.py:148-171` (`_walk_condition_nodes`), `:455-536` (`_apply_to_tree`)
- Test: `testplatform/backend/tests/test_strategy_param_space_mode_gene.py`

**Context.** Categorical genes already exist at the ACTION level: `option_strike_method` emits `{"type": "choice", "choices": [...], "min": 0, "max": len-1, "step": 1}` (`strategy_param_space.py:232-256`) and `genetic.py` decodes `type=="choice"` as an index. Reuse that shape for `cond:<id>:mode` with the leaf's OWN `mode_choices` (numeric: off/below/above; categorical: off + values). Decode (`_apply_to_tree._recurse`): a leaf whose decoded `mode == "off"` is DROPPED from its parent's `conditions` (exactly like `enabled == 0`); numeric `"below"` sets `node["op"] = node["comparison"] = "<"`, `"above"` sets both to `">"` (D§5: "decode must synchronize op and comparison"); a CATEGORICAL choice sets `node["op"] = node["comparison"] = "=="` and `node["value"] = float(code)` where `code = PROFILE_FIELD_CODES[field][choice]` looked up through `ba2_common.core.market_conditions.field_spec(field).codes` (the registry from Task 2), and keeps `node["mode"] = choice`. A categorical leaf emits NO `cond:<id>:value` gene even though it has no `optimize` flag to suppress (it has no `value_min`, so nothing is emitted today — pin that with a test). `decode_params` already routes any `cond:<id>:<field>` into `cond_by_id` (line 731-733), so no change there. A leaf with `mode_optimize` AND `toggle_optimize` must raise at collection (belt and braces with Task 2); use `rule_models.leaf_mode_kind` to classify.

**Step 1: Failing tests.**

```python
from app.services.strategy_param_space import collect_param_space_from_tree_for_test  # implementer: use the real public entry points used by test_strategy_param_space_collect.py / _decode.py

def _leaf(mode=None):
    d = {"id": "o_ic-market-adx", "field": "underlying_adx_14", "op": "<", "value": 25, "optimize": True,
         "value_min": 10, "value_max": 40, "value_step": 5, "mode_optimize": True, "mode_choices": ["off","below","above"]}
    if mode: d["mode"] = mode
    return d

def test_mode_optimize_emits_a_choice_gene_next_to_the_value_gene(): ...
    # keys: "cond:o_ic-market-adx:mode" == {"type":"choice","choices":["off","below","above"],"min":0,"max":2,"step":1}
    #       "cond:o_ic-market-adx:value" == _range_entry(10,40,5)
def test_three_leaves_produce_six_genes(): ...
def test_decode_off_drops_the_leaf_from_the_tree(): ...          # by_id={"o_ic-market-adx":{"mode":"off","value":20}} -> leaf absent
def test_decode_below_and_above_set_both_op_and_comparison(): ...
def test_decode_leaves_a_legacy_leaf_without_mode_unchanged(): ...
def test_mode_and_toggle_together_raise_at_collection(): ...
def test_all_off_control_decodes_every_new_leaf_out(): ...        # explicit "off" for each of 3 leaves -> tree equals the profile-none tree
def test_encode_decode_roundtrip_keeps_mode_index_and_value(): ... # through genetic.py's choice decoding, if a helper exists (see test_param_space_roundtrip.py)
def test_categorical_leaf_emits_only_a_mode_gene_and_decodes_to_an_equality_code(): ...
    # leaf {"id":"s1-structure-state","field":"structure_state","op":"==","mode_optimize":True,"mode_choices":["off","bull","bear"]}
    # -> genes: exactly {"cond:s1-structure-state:mode": choice over ["off","bull","bear"]}; no :value gene
    # -> decode "bull": node op/comparison "==", value == float(code for "bull"), mode "bull"; decode "off": leaf dropped
    # (register a throwaway categorical FieldSpec in the test via the registry's test hook, or use the real
    #  structure_state spec if Task 10 has landed; do not hard-code the code value in the test -- read it from the registry)
```

**Step 3: Implement.** In `_walk_condition_nodes` after the `optimize` block:

```python
    if cond.get("mode_optimize"):
        if cond.get("toggle_optimize"):
            raise ValueError(f"condition {cid!r}: mode_optimize and toggle_optimize are two independent disable controls")
        choices = list(cond.get("mode_choices") or [])
        if choices != ["off", "below", "above"]:
            raise ValueError(f"condition {cid!r}: unsupported mode_choices {choices}")
        out[f"cond:{cid}:mode"] = {"type": "choice", "choices": choices, "min": 0, "max": len(choices) - 1, "step": 1}
```

In `_apply_to_tree._recurse`, in the child loop add `if ccid and by_id.get(ccid, {}).get("mode") == "off": continue`; in the per-node block add:

```python
            if "mode" in sub and sub["mode"] in ("below", "above"):
                op = "<" if sub["mode"] == "below" else ">"
                node["op"] = op; node["comparison"] = op; node["mode"] = sub["mode"]
```

(the decoded choice may arrive as the token string or as an index, depending on `genetic.py`'s path — check `test_launcher_option_strike_method_gene.py` for which, and normalise once at the top of `_recurse` via the leaf's `mode_choices`).

**Step 5: Commit.** `feat(param-space): categorical cond:<id>:mode gene (off/below/above) collected and decoded onto the leaf`

---

### Task 4: Event types, condition classes, registry mapping and the context resolver seam

**Files:**
- Modify: `packages/common/ba2_common/core/types.py` (ExpertEventType, next to `N_IV_TO_REALIZED_VOL` at ~432, and the numeric-set list at ~649)
- Modify: `packages/common/ba2_common/core/TradeConditions.py` (three classes after `RelativeVolumeCondition` ~2730; `CONDITION_MAP` ~4055; a resolver seam next to `set_provider_resolver` ~80)
- Modify: `packages/common/ba2_common/core/rule_builders.py:79-81` (`FIELD_EVENT`)
- Create: `packages/common/ba2_common/core/market_condition_context.py`
- Test: `packages/common/tests/test_market_condition_conditions.py`; existing `tests/test_condition_registry_coverage.py` must stay green.

**Context.** D§4.1, D§5, D§7. There is NO evaluation-context seam today: `create_condition(event_type, account, instrument_name, expert_recommendation, existing_order, operator_str, value)` and `TradeActionEvaluator` thread only `account`. Decision for this plan (matches the design's "resolve lazily only when a surviving new condition is evaluated"): a module-level resolver seam in `TradeConditions`, mirroring `set_provider_resolver`:

```python
_market_condition_context_resolver = None
def set_market_condition_context_resolver(fn):   # fn(account, instrument_name, expert_recommendation) -> MarketConditionContext | None
def resolve_market_condition_context(account, instrument_name, expert_recommendation):
```

Backtest and live wiring install it in Task 5. With no resolver installed, the three conditions evaluate to `calculated_value=None`, return False, and record status `no_context` with reason "market-condition profile not wired for this process" (D§4.1: "must not call the legacy end-of-day helper as a fallback").

`MarketConditionContext` (frozen dataclass) fields: `decision_time: datetime` (tz-aware), `session_label: date` (the EFFECTIVE trading session the decision belongs to), `prior_session: date`, `source_profile: str`, `timing_policy: str`, `calc_version: str`, `reader: MarketConditionReader` (protocol with `observe(symbol: str, session: date) -> MarketConditionValues | None` — None = no row/coverage; the BT/live readers come in Tasks 5/7), `recorder: Optional[Callable[[str, date, MarketConditionValues], None]]`. Conditions call `ctx.reader.observe(instrument, ctx.prior_session)`; a `None` row → status `missing_session`.

Condition classes: `UnderlyingTrendSlopeCondition`, `UnderlyingAdxCondition`, `UnderlyingRealizedVolRatioCondition`, all `CompareCondition` subclasses with a shared mixin `_MarketConditionCompare` holding `FIELD` and the body below. Build the mixin so that Task 10 can add the `ta-structure-v1` classes by declaring `FIELD` only (one class per searched field, generated from the registry with a small factory `market_condition_condition_class(field) -> type` and registered into `CONDITION_MAP`/`FIELD_EVENT` by iterating `PROFILES` — the registry coverage test then guards every profile field automatically). Categorical fields compare the stored integer code with `==` (`CompareCondition` already maps `'=='` to `operator.eq`; the leaf's decoded `value` is the code, see Task 3). Event-type enum members: one per field across both profiles, VALUE == canonical field name.

```python
    def evaluate(self) -> bool:
        ctx = resolve_market_condition_context(self.account, self.instrument_name, self.expert_recommendation)
        if ctx is None:
            self.calculated_value = None; self.last_status = STATUS_NO_CONTEXT; self.last_reason = "no market-condition context wired"; return False
        values = ctx.reader.observe(self.instrument_name, ctx.prior_session)
        if values is None:
            self.calculated_value = None; self.last_status = STATUS_MISSING_SESSION; ...; return False
        obs = values.by_field()[self.FIELD]
        self.last_status, self.last_reason = obs.status, obs.reason
        if obs.status != STATUS_VALID:
            self.calculated_value = None; return False
        self.calculated_value = obs.value
        if ctx.recorder: ctx.recorder(self.instrument_name, ctx.prior_session, values)
        return self._compare(obs.value)   # strict < / > only (D§5: equality passes neither)
```

Check how `CompareCondition` performs the comparison today (`_compare`/operator dispatch) and reuse it; if it supports `>=`/`<=`, the leaf op is always `<`/`>` here so nothing changes, but add a test that `==` never passes.

Event types: `N_UNDERLYING_TREND_SLOPE = "underlying_trend_slope_50_atr14"`, `N_UNDERLYING_ADX = "underlying_adx_14"`, `N_UNDERLYING_RV_RATIO = "underlying_realized_vol_ratio_5_20"`, added to the numeric-event list at ~649 and to any `numeric fields` UI list the registry coverage test checks. `FIELD_EVENT` gets the three keys.

**Tests** (mock account, mock reader): no resolver → False + `no_context`; resolver returns ctx whose reader returns None → `missing_session`; invalid observation → False and `calculated_value is None`; valid 30 with `< 25` → False, `< 35` → True, equality → False for both operators; the recorder is called once per evaluation with the values; registry coverage test green; `triggers_from_condition_tree` on a tree with the three leaves yields three triggers (no WARNING "unmapped field").

**Commit.** `feat(conditions): three underlying market-condition numeric conditions behind a lazily-resolved MarketConditionContext seam`

---

### Task 5: Prior-session lookup, source certification, backtest and live context adapters, replay capture

**Files:**
- Modify: `packages/common/ba2_common/core/market_calendar.py` (add `prior_regular_session`, `regular_sessions_ending_at`)
- Create: `packages/common/ba2_common/core/market_condition_source.py` (window assembly + source certification)
- Modify: `testplatform/backend/app/services/backtest/price_source.py` (`AsOfPriceSource.window_before(symbol, session, n)`)
- Modify: `testplatform/backend/app/services/backtest/seam_wiring.py` (install the BT resolver when the run config carries `market_condition_profile == "ohlcv-v1"`)
- Modify: `ba2_trade_platform/core/seam_wiring.py` (live resolver; reads `replay_now()` ONCE per analysis on the coordinating thread — find where the analysis loop starts in `JobManager`; pass the value down, never call `replay_now()` in a worker)
- Modify: `packages/common/ba2_common/core/replay/` (capture a `market_condition_window` observation: normalized window digest, the three values, statuses — reuse the existing observation recording API; a hash without retained bytes is not replayable (D§4.2), so retain the normalized 128x5 float64 window bytes in the store, deduplicated by digest)
- Test: `packages/common/tests/test_market_condition_sessions.py`, `testplatform/backend/tests/backtest/test_market_condition_bt_context.py`, `tests/test_market_condition_live_context.py`, `packages/providers/tests/test_fmp_ohlcv_split_certification.py`

**Context.** D§4 and D§4.1. `market_calendar` offers only `nyse_regular_sessions(first_day, last_day)`; build:

```python
def prior_regular_session(decision: datetime | date) -> date:
    """The last regular session STRICTLY BEFORE the decision's exchange-local date (D§4 prior_session_v1).
    A tz-aware datetime is converted to America/New_York first; a naive datetime RAISES."""
def regular_sessions_ending_at(session: date, n: int) -> list[date]:
    """The n regular-session dates ending at ``session`` inclusive (raises if ``session`` is not a session)."""
```

Session tests: Monday 09:30 NY and Monday 15:45 NY → previous Friday (or Thursday over a holiday weekend, e.g. 2025-07-04 → 2025-07-03, and Presidents' Day); half day 2025-11-28 handled by data; DST transition dates; naive datetime raises; a daily BT session label `2025-03-10` → `2025-03-07`; both adapters resolve the same cutoff for the same session.

Source certification (`market_condition_source.py`): `assemble_window(bars_df, session) -> (o,h,l,c,v) | raise` enforcing exactly 128 unique consecutive sessions ending at `session`, monotonically increasing dates matching `regular_sessions_ending_at`, finite positive prices, OHLC ordering; `certify_source_columns(provider_name)`: reads the FMP OHLCV cache for a symbol with a known split inside a fixture window (AAPL 2020-08-31 4:1 and NVDA 2024-06-10 10:1) and asserts the cached Close/High/Low are on ONE consistent basis (no 4x/10x discontinuity across the split date in the ratio High/Close). Implementer: the Explore report found no `adjClose` handling in `FMPOHLCVProvider.py`; determine from the cached parquet whether the endpoint delivers split-adjusted OHLC, pin the finding in the test with the real cached data if available locally (`CACHE_FOLDER/FMPOHLCVProvider/AAPL_1d.parquet`), else `pytest.skip` with the exact reason and make `certify_source_columns` FAIL preflight (Task 6) rather than guess.

Facts from Task 4 the adapters must honour: the resolver is invoked once PER CONDITION EVALUATION (an entry with three market leaves resolves three times per symbol per bar) — the BT and live resolvers therefore build the context ONCE per (decision session, analysis) and return the same frozen object on repeat calls (a per-analysis cache keyed by session label; the BT adapter invalidates when the simulated date advances); the recorder is called once per valid leaf read — the replay recorder dedupes on (symbol, session, window digest); the condition does not check calc versions — the adapter's reader must serve rows whose `calc_version` equals the profile's, and the reader raises (not skips) on a mismatch. Also: `MarketConditionValues.by_field()` is hard-coded to the v1 trio; the store/reader row type used from Task 6 onward is a field-generic `FeatureRow(values: Mapping[str, Observation], calc_versions: Mapping[str, str])` exposing the same `by_field()` shape, and `MarketConditionValues` gains a `to_feature_row()` — Task 10's chart-structure values use the same generic row so the conditions need no change.

BT adapter: `AsOfPriceSource.window_before(symbol, session, n)` returns the arrays for the `n` sessions ending at `session` from its private `_o/_h/_l/_c/_v` columns (no DataFrame), or `None` if coverage is short; the BT reader (a small class in the backend) wraps it and memoizes by `(symbol, session)` per process with a bounded LRU (2000 entries). The BT resolver builds the context from the engine's current simulated date (`account._as_of_date()` gives the date; the effective session label is that date and `prior_session = prior_regular_session(that date)`).

Live adapter: reads the FMP OHLCV provider's CACHE ONLY (`cached_only`-style path, never a network call inside a condition — find the provider's cache read API); `decision_time = replay_now()` read once in the coordinating thread of the scheduled analysis and stored on the resolver for the duration of that analysis (design: "different analyses cannot share a mutable global clock" → keep it per-analysis, e.g. a `contextvars.ContextVar` set by the analysis coordinator, not a module global). **A ContextVar does NOT propagate into `ThreadPoolExecutor` / WorkerQueue threads on its own** — the coordinator must run worker callables through `contextvars.copy_context().run(...)` (or pass the frozen context object explicitly into the fan-out); pin this with a test that evaluates a market leaf from a pool thread and sees the coordinator's context. Both adapters return the SAME frozen `MarketConditionContext` object on repeat calls within one analysis/bar (three resolver calls per symbol per bar; `__post_init__` validation is not free) and the readers return memoised rows per (symbol, session) whose `by_field()` returns the stored mapping, not a fresh dict.

**Commit** (may be two commits: sessions+certification, then adapters). `feat(market-conditions): prior_session_v1 calendar lookup, source certification, BT/live context adapters and replay capture`

---

### Task 6: Central feature store, manifests and the warmup service (plan / build / verify)

**Files:**
- Create: `packages/common/ba2_common/core/market_condition_store.py` (objects, manifests, identity, reader over parquet)
- Create: `packages/providers/ba2_providers/market_conditions/warmup.py` (plan/fetch/build/publish orchestration using existing OHLCV provider warmers)
- Create: `tools/warm_market_conditions.py` (CLI: `plan`, `build --cache-only|--fetch-missing`, `verify`, `prepare-host` — prepare-host lands in Task 7)
- Test: `packages/common/tests/test_market_condition_store.py`, `packages/providers/tests/test_market_condition_warmup.py`, `testplatform/backend/tests/test_warm_market_conditions_cli.py`

**Context.** D§4.2–4.4, D§4.6, amendment 2 ("a second profile in the same store, not a second store"). Layout under `CACHE_FOLDER/market_conditions/<profile>/` for each profile in `PROFILES`: `objects/<sha256>.parquet` (feature shards, rows = symbol × session; columns: `symbol`, `session` (date32), one float64 column per `FieldSpec.name` in registry order (categorical fields hold their integer code as float64), one int8 status column per field (`<field>_status`, over the `STATUSES` order), `window_digest` (32 bytes), `raw_shard_ref` (string), `raw_row_lo`, `raw_row_hi`), `raw/<sha256>.parquet` (normalized 5-column float64 bar shards per symbol/month, deduplicated, SHARED across profiles — the raw bucket lives at `market_conditions/raw/`, not under a profile), `manifests/<sha256>.json`. The store, warmup and CLI take `--profile` and iterate the registry; the parquet schema is declared explicitly from the registry (never inferred from pandas dtypes, so an all-invalid column stays float64). The warmup computes every registered field of the requested profile(s) from one window read per (symbol, session), calling `compute_market_conditions` for `ohlcv-v1` and `compute_chart_structure` (Task 10) for `ta-structure-v1`; until Task 10 lands, only `ohlcv-v1` is registered and the CLI rejects an unknown profile name. Sharding: one feature object per (symbol, calendar month of the session). Manifest JSON pins: `source_profile`, `timing_policy`, `calc_version`, `calendar_version` (pandas_market_calendars version string), `schema_version`, `objects` (list of `{path, sha256, symbol, month, rows}`), `raw_objects`, `coverage` (`{symbol: {first_session, last_session, rows, exceptions: [...]}}`), `created_at`, `universe_digest`, `window_start`, `window_end`. Portable identity = sha256 of the canonical manifest JSON without `created_at`.

Store API: `MarketConditionStore(root)` with `write_manifest`, `read_manifest(digest)`, `iter_rows(manifest, symbol)`, `verify(manifest) -> report` (hashes every referenced object; size equality alone is not integrity), `retained_window(window_digest) -> (o,h,l,c,v)`. Window identity reuses Task 5's `market_condition_source.window_digest` (`sha256:<hex>` over the `<f8` (bars, 5) matrix) so a store row, a capture observation and a live read agree on the same key. **Preflight:** the warmup `plan` phase calls `certify_source_columns(cache_root)` (Task 5) and FAILS with the report when any symbol is not `consistent` — never builds features from an uncertified basis (D§4). **Split-basis drift (Task 5 review):** the two-fixture certification proves only that files fetched after their split are adjusted; the FMP cache top-up appends bars after the last cached one, so a symbol that SPLITS AFTER its first fetch ends up on a mixed basis (pre-split bars unadjusted) and would feed a fake 2x/4x/10x move into the calculators with status `valid`. The plan therefore requires, per universe symbol: read FMP's stock-split calendar (find the provider method or endpoint; cache it like other FMP data), and for every split dated after the cached file's first bar, verify the cached closes around the split date are on one basis (ratio check as in `certify_source_columns`, applied at THAT date); any symbol failing is reported and either force-refetched in full (the `--fetch-missing` phase) or excluded from the plan with an explicit coverage exception — never silently kept. A price-only detector is NOT sufficient (a reviewer scan found 59 lasting 1/k steps since 2020 across the cache, many real crashes), so the split calendar is the authority. **The LIVE path has the same exposure** (the live reader serves the cache directly and the live top-up appends bars): the FMP cache refresh (`MarketDataProviderInterface._refresh_parquet_if_stale`) must force a FULL re-fetch of a symbol whose split calendar shows a split after the file's first cached bar — implement it in Task 6 next to the preflight, with a test on a fabricated pre-split file. `retained_window` reuses Task 5's `window_from_bytes` / `window_digest_of_bytes` — no second decoder.

Warmup phases as D§4.4: `plan(profile, universe, start, end, source_profile, cache_root) -> WarmPlan` (union of symbols; per symbol the required sessions = every regular session in [start, end] as DECISION sessions → prior sessions → each needs 128 sessions back; earliest raw bar = 127 sessions before the first row's end session; lists missing raw bars vs the provider cache, missing feature rows vs existing manifests); `build(plan, fetch_missing: bool, concurrency)` reads each raw shard once, computes all three fields per session, publishes only missing/invalidated rows, `.part` + `os.replace`, coalesces concurrent builders with the `DerivedArrayStore`-style claim file, persists progress at shard boundaries, and refuses to publish a mixture if a source file changes mid-read (retry the snapshot). `verify(manifest)` re-hashes. Counters returned and printed: provider calls, provider bytes, rows computed, rows reused, objects written, objects reused, elapsed per phase.

**Acceptance tests (D§4.6, D§8.10):** second identical warmup → provider calls 0, rows computed 0, opens the same manifest digest; extending `end` by one session → exactly `len(universe)` new rows, old objects reused by hash; injecting a changed historical bar for one symbol → at most 128 ending sessions of that symbol recomputed and a NEW manifest, the old manifest still readable and unchanged; interrupted publication (kill after some objects) → resume trusts only hash-verified objects; two concurrent builders on the same plan → one builds, one waits, identical manifest; negative (unavailable) entries invalidated when the raw shard gains the bar; `--cache-only` with missing raw → nonzero exit with the missing inventory, no fetch.

**Commit.** `feat(market-conditions): central immutable feature store, manifests and the plan/build/verify warmup service + CLI`

---

### Task 7: Host-shared mapped reader, worker preparation and synced-object verification

**Files:**
- Create: `packages/common/ba2_common/core/market_condition_reader.py` (`MappedMarketConditionReader` over `DerivedArrayStore`)
- Modify: `tools/warm_market_conditions.py` (`prepare-host --manifest`), `tools/build_shared_arrays.py` (optional `--market-conditions <manifest>` hook so the existing prewarm step can prepare mappings in the same pass)
- Modify: `testplatform/backend/app/services/cache_sync.py` (post-arrival sha256 verification for `market_conditions/**` objects listed by a manifest; snapshot-scoped push that never prunes objects referenced by another pinned manifest), `worker_client.py` / `worker_server.py` (a worker acknowledges `market_condition_manifest` digest + local verification before it receives trials; missing/incompatible → unready, never a zero-trade result), `strategy_optimization_handler.py` (`_WORKER_ENV_KEYS`/config carry the manifest digest; the BT resolver from Task 5 receives the mapped reader)
- Test: `packages/common/tests/test_market_condition_reader.py`, `testplatform/backend/tests/test_market_condition_worker_readiness.py`, `testplatform/backend/tests/test_cache_sync_market_conditions.py`

**Context.** D§4.3, D§4.5, D§4.6. Task 5's readers (`market_condition_bt.py`, `market_condition_live.py`) compute on a miss and share a base whose subclasses implement only `_bars` / `_memo_key`, with the calc-version check in `_check_row`; the mapped reader of this task replaces the COMPUTE path with a store lookup, returns the same `FeatureRow`, raises the same `MarketConditionVersionMismatch`, and must still be able to supply the window bytes to the capture recorder (from the raw shard) — the BT/live readers become thin wrappers over it when a manifest is pinned, and fall back to compute-on-miss ONLY when no manifest is configured (research/dev), never silently in a grid job. Mapped layout per manifest: key `mc_<manifest-digest[:16]>_v<LAYOUT_VERSION>` in `DerivedArrayStore` under `derived_root_for(CACHE_FOLDER)/market_conditions/`; arrays: `symbol_index` (int32 codes + a small JSON symbol table in the marker), `session` (int32 days since epoch, sorted per symbol), `values` (float64 [n,3]), `status` (int8 [n,3]), `offsets` (int64 per symbol). `observe(symbol, session)` = bisect on the symbol's session slice → `MarketConditionValues`; a symbol/session absent → `None`. Bounded handle cache; reopen after worker recycle (`BT_MAX_TASKS_PER_CHILD`). `BA2_SHARED_ARRAYS=0` loads the same central rows into private arrays (no recompute). Keep `ensure_fd_headroom()`; the reader holds ≤ 5 descriptors per manifest.

Tests: values from the mapped reader equal the parquet reader for every row; a second host given only the manifest+objects builds mappings without recomputing (counter = 0 computed rows); a corrupted object with the same size is rejected by verification; a worker whose local snapshot is missing reports unready and receives no trials; recycling a worker reopens the mapping and answers identically; descriptor count bounded.

**Commit.** `feat(market-conditions): host-shared mapped feature reader, prepare-host, worker readiness on the manifest digest, hash-verified sync`

---

### Task 8: Launcher profile, per-structure placement, driver flag and deployment export

**Files:**
- Modify: `testplatform/ba2test_launcher.py`: new module setting `_MARKET_CONDITION_PROFILES: tuple[str, ...]` (empty = `none`, default) set by CLI `--market-condition-profile none|ohlcv-v1|ta-structure-v1|ohlcv-v1,ta-structure-v1` (comma list, validated against `PROFILES`; `ta-structure-v1` is accepted only once Task 10 has registered it); `_market_condition_gates(m: str) -> list[dict]` builds ONE leaf per `FieldSpec` with `searched=True` across the selected profiles, in registry order, ids `<m>-market-<short>` where `short` comes from the FieldSpec (`slope`, `adx`, `rv`, and for Task 10 `dist-support`, `dist-resistance`, `channel-pos`, `close-vs-high`, `structure-state`), numeric leaves with `mode_optimize=True`, `mode_choices=["off","below","above"]`, ranges from the FieldSpec, authored `op`/`value` = the FieldSpec's declared anchor (ohlcv-v1: slope `> 0.0`, adx `< 25`, rv `< 1.0`), categorical leaves with `mode_choices=["off", *field_spec.codes]` (iteration order = ascending CODE, e.g. bull, bear; the choice-gene index follows it, so pin it with a test) and `op "=="`, no threshold; every authored operator is taken from the generated condition class's `ALLOWED_OPERATORS` (Task 4: `{"<", ">"}` numeric, `{"=="}` categorical) so launcher and engine cannot drift — assert it in a test; appended at the END of `_option_entry_rule`'s AND list (line ~4517-4523) when any profile is on; any digest, manifest or payload that records a FieldSpec/ProfileSpec uses `FieldSpec.to_dict()` (never `dataclasses.asdict`, which exposes the private `_code_pairs`); for O_CC/O_PP appended to the `_build_strategy_row` entry tree (line ~1638) with `m = kind.lower()` (`o_cc`/`o_pp`) — this tree has no prefix convention today, so the three new leaf ids are the first prefixed ones there; for O_WHEEL, `_build_strategy_wheel` (line ~4857) reuses O_CSP's rule: after building, RENAME the three market leaves' ids from `o_csp-market-*` to `o_wheel-market-*` (D§5 wheel-specific ids). Run config records `market_condition_profile`, `source_profile`, `timing_policy`, `calc_version`, `market_condition_manifest` (digest) and these flow into the configuration digest and every checkpoint/distributed payload.
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py` `_build_daily_trial_config` (rebuilds the trial config key by key — the known whitelist trap: a knob missing there is inert while every log claims it works): pass `market_condition_profile` and the manifest digest through, with a test that a GA trial's config carries them (found in Task 5: today they would be dropped). Task 5's guard turns a dropped key into a per-trial `ValueError` from `install_backtest_market_conditions`; the GA handler must treat that as a FAILED JOB (abort, not a zero-fitness individual) — pin with a test. Same file (~1255 and ~1354, where `trial_key({..., "params": decoded_flat})` memoises trials on the RAW decoded genome): add the D§5 anchor canonicalisation for THIS profile only — before hashing, for every mode leaf decoded to `off`, replace the flat genome's `cond:<id>:value` with the leaf's authored anchor value so two genomes differing only in an inactive threshold dedupe to one phenotype; the raw genome is persisted untouched as provenance; guard so runs with no mode leaves produce exactly today's key (pin with a test on an existing fixture). Task 3 established (and pinned) that `strategy_param_space` holds no canonicalisation. Also: mode leaves are always appended INSIDE the entry AND group, never as a root leaf (Task 3 makes an `off` root leaf raise).
- Facts from Task 7 the launcher/driver must honour: the trial passthrough (`_build_daily_trial_config` emits `market_condition_profile`, `market_condition_manifest`, `_ga_trial`), `_WORKER_ENV_KEYS`, the evaluator plumbing and the GA refusal (a `_ga_trial` config with a profile but no manifest RAISES) are in place — the launcher sets `backtest_cfg["market_condition_manifest"]` (digest) and `["market_condition_profile"]` from the CLI, and the PERSISTED run config (the stored `backtest_cfg` block of the optimization row) must keep the digest so optimizer re-runs, robustness variants, top-N persist AND every `_build_daily_trial_config` consumer (`tools/backtest_parity.py`, `run_genome_once.py`, `recover_missing_topn.py`, `genome_concentration_check.py`) of a gated genome are not refused (Task 7 pins the round-trip; Task 8 pins that the launcher writes it); the master runs `prepare_host` itself before dispatch (Task 7) so the runbook warm step is an optimisation, not a correctness requirement; the launcher/driver REFUSES to dispatch when the pinned manifest does not cover the run's universe (Task 7 also enforces it at the seam for `_ga_trial` configs; the 2026-09-16 production snapshot covers 85 of 98 option symbols — the 13 `refetch_required` symbols must be re-fetched or the universe trimmed BEFORE the first gated grid, and which was chosen is recorded next to the measurement block); `stage1_run.sh`'s warm step = `warm_market_conditions plan/build/verify` then `prepare-host` on the MASTER, per-worker prepare is done by `distributed_eval._preflight_worker` (calls `/market-conditions/prepare` after `push_cache`; failure excludes the worker); live reads `BA2_MARKET_CONDITION_MANIFEST` next to `BA2_MARKET_CONDITION_PROFILE`; a `LAYOUT_VERSION` bump moves every mapped key and needs a `build_shared_arrays.py --sweep` pass (document in the runbook section).
- Modify: `tools/run_options_matrix.py` `build_cmd` (pass `--market-condition-profile` through; it folds into `discovery_name`'s digest automatically) and `--profile discovery` help text; **population, generation ceiling and patience are NOT changed by the profile** (operator 2026-09-15: "add genes but keep pop size as is, is already big") — the run configuration records the gene count of the widened space next to the unchanged POP=200/GEN=60/patience 8, nothing scales them; `tools/stage1_run.sh` gets `MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-none}"` and, when not `none`, runs `tools/warm_market_conditions.py plan+build+verify+prepare-host` ONCE before dispatch and passes the manifest digest.
- Modify: `testplatform/frontend/src/lib/tradeRules.ts` and `lib/geneCount.ts`: count a `cond:<id>:mode` gene for every leaf with `modeOptimize` (and no `:value` gene for a categorical leaf) so the UI gene count matches `strategy_param_space` (found in Task 2: the counters ignore mode genes today); add a vitest case next to the existing `geneCount.test.ts` ones. Also in `ConditionBuilder.tsx` (~line 563) disable the `toggleOptimize` checkbox when `condition.modeOptimize` is set, so the UI cannot save the combination the backend rejects.
- Modify: ruleset save/import validation (the canonical `normalize_trade_rules` caller used by the API save path and by deploy import): REFUSE a market-condition leaf (any registered field) inside an open-positions/exit ruleset — the design forbids gates on exits and Task 5 showed such a leaf would silently never pass live (`no_context` outside a decision scope); test it.
- Modify: deployment export/import (`tools/export_deploy_payload.py` / `import_deploy_payload.py` or the senate equivalents, whichever the option deploy uses): the exporter REJECTS a payload containing an unresolved `mode` gene or `mode_optimize` leaf (D§5); resolved leaves export as ordinary numeric conditions; import on a server whose `FIELD_EVENT` lacks the new fields must REJECT, not drop (add the check in `rule_builders.triggers_from_condition_tree`'s caller used by import: an unmapped field that is in a `STRICT_FIELDS` set raises instead of warning).
- Test: `testplatform/backend/tests/test_launcher_market_condition_profile.py`, `test_option_matrix_market_condition_flag.py`, `test_deploy_payload_market_conditions.py`; existing `test_launcher_option_entry_rule.py`, `test_option_grid_foundations.py`, `test_stage1_run_sh.py`, `test_convex_matrix_script.py` must remain green with profile `none`.

**Tests:** profile `none` → `_option_entry_rule("O_LC")` is byte-identical to dev HEAD's output (pin a literal); profile `ohlcv-v1` → each of the 16 permitted structures collects exactly six more genes named per the contract; O_CC/O_PP place them in the stock-entry tree and NOT in the overlay rules; O_WHEEL ids are `o_wheel-market-*`; every new leaf reaches the engine (`triggers_from_condition_tree` yields 3 more triggers per structure); the all-off control (three `mode=off`) decodes to a tree equal to profile `none`'s; `discovery_name` digest changes when the flag changes and is stable otherwise; `stage1_run.sh --dry-run` prints the warm step before the matrix when the profile is on; exporter rejects unresolved genes; importer rejects unknown strict fields.

**Commit.** `feat(launcher): --market-condition-profile ohlcv-v1 appends the three mode/threshold gates per structure; driver flag, warm-once preflight, strict deploy export/import`

---

### Task 9: Coverage/performance report, entry-state attribution and the all-off compatibility gate

**Files:**
- Facts from Task 8: the persisted optimization row carries a `market_condition` block (profiles, digest, source_profile, timing_policy, calendar_version, calc_versions, `FieldSpec.to_dict()` list, gene names + count) — the report reads versions and gene counts from it, never re-derives them; `--gates-off` strips mode leaves, so a smoke run cannot measure the gates (the trial-cost gate below uses a real genome with modes forced on); the all-off control equals profile none's tree by construction (pinned in `test_launcher_market_condition_profile.py`).
- Create: `tools/report_market_conditions.py` (per-job summary per D§7: winning modes/thresholds, versions, eligible recommendations vs gate-rejected vs unknown-input counts by reason, submitted/filled structures, per-year profit/CAR/DD from the account engine, attribution of executed structures' net P&L and top-1/top-5 concentration to the recorded entry-state measurement bins with explicit bin edges; NEVER annualises a filtered trade subset as a funded account)
- Modify: the live resolver (`market_condition_live.py`): when a manifest is pinned, run `check_market_condition_coverage` against the LIVE universe (the union of the enabled instruments of every ExpertInstance whose rulesets carry a market-condition leaf — derive it from the instance settings the way `JobManager` builds analysis jobs) at install and at each decision-scope open when the universe changed; missing symbols → one ERROR per symbol per process naming the digest, and those symbols' gates report `no_context` with that reason (the backtest seam already refuses a `_ga_trial` on the same condition; live must not gate them off silently). Test with a stubbed universe.
- Modify: `TradeManager.process_expert_recommendations_after_analysis` wrapper (Task 5): open/bind the live capture scope BEFORE `market_condition_decision_scope` so a gated live decision is replayable (today capture is inert live and a replay of a gated bundle raises `ReplayMiss` in `begin_decision`); test the recording round-trip through the wrapper. The capture path uses the mapped reader's `window_result_for` (arrays + calendar window dates) — `CapturingMarketConditionReader` asserts `inner._retain_windows`, which the BT reader sets False on purpose, so only the live wrapper is wired for capture.
- Modify: the backtest results blob to record per-trade entry-state values (the three values + statuses at entry) when the profile is on; the daily engine's stats to count `eligible`, `gate_rejected`, `unknown_input_by_reason` for the new conditions only (D§6 "Report eligible recommendations separately from condition rejections")
- Test: `testplatform/backend/tests/backtest/test_market_condition_all_off_matches_baseline.py` (a frozen small option run — reuse `golden/option_leap_golden_run.json`'s fixture pattern — run with profile `none` and with profile `ohlcv-v1` + all three modes `off`: orders, trades and equity curve byte-identical; research metadata compared separately), `test_report_market_conditions.py`, plus the existing golden/parity suites (`test_engine_golden_regression.py`, `test_option_golden_run.py`, `test_parity_golden.py`) green.
- Operations notes owed from Task 6 (record in `docs/RUNBOOK-goal2020-grid.md` under a "Market-condition feature store" section as part of this task): (a) the first live run after this ships forces a FULL FMP re-fetch, synchronously inside the cache refresh, for every symbol whose split calendar shows a post-first-bar split that is mixed or `undetectable` (factor < 1.5) — size it beforehand with `warm_market_conditions plan` over the live universe; (b) no GC/compaction exists yet: a future collector must walk OBJECTS, not only manifests, because whole-object reuse preserves old `raw_shard_ref` values verbatim (a raw shard is pinned by any manifest that transitively references it); (c) progress records under `_derived/market_conditions_build` expire after 24 h.
- Benchmark script: `testplatform/backend/tests_scripts/bench_market_conditions.py` producing the D§4.6 counters on a fixed 20-symbol subset: cold build time, repeat warmup time, prepare-host time, P50/P95 `observe()` latency, worker RSS and descriptor count at the intended 30 workers. Write results to `reports/strategy_research/market_conditions_bench_<date>.md`.
- **No-impact and trial-cost gate (operator 2026-09-15: "ensure no impact on existing bt and test perf of these new conditions"):** (a) with profile `none`, a known backtest re-run through `tools/backtest_parity.py` (the tool built for the shared-arrays acceptance: `--bt <id>` re-runs a persisted genome and compares results/trades/equity/drawdown byte-for-byte) is IDENTICAL to its persisted row for one equity reference (bt 1681) and one option reference (bt 1688), and a counter proves the market-condition resolver was never called; (b) with the profile on and all modes off, the same rows are identical too; (c) trial cost: time 30 evaluations of a fixed option genome on a fixed 20-symbol window with the profile off, then on with all three gates active — report median per-trial seconds and the per-evaluation `observe()`+compare cost; the acceptance bar is that the gates add < 1% to trial time (their work is one indexed lookup and one comparison per evaluated leaf; anything larger means a DataFrame, a calculator or a cache-tree scan leaked into the decision path and is a defect to fix, not a number to report). Record all three in the bench report.

**Commit.** `feat(market-conditions): per-job coverage/attribution report, entry-state capture, all-off compatibility gate and benchmark`

---

### Task 10: `ta-structure-v1` chart-structure calculators, batch form, condition classes

**Files:**
- Modify: `packages/common/ba2_common/core/market_conditions.py` (add `compute_chart_structure(o,h,l,c,v, atr=None) -> ChartStructureValues`, the pivot/level/channel/breakout/swing helpers, `ChartStructureValues.as_row()/by_field()`, and register the `ta-structure-v1` `ProfileSpec` with the twelve `FieldSpec`s from the D§3.2 table — five with `searched=True`: `structure_dist_support_atr`, `structure_dist_resistance_atr`, `channel_pos_20`, `close_vs_prior_high_20_atr`, `structure_state` (categorical, codes `{"bull": 1, "bear": 2}`; `none` is stored as code 0 but is NOT a choice); calc version `"ta-structure-v1/calc-1"`; conventions `PIVOT_K = 3`, `CHANNEL_LOOKBACK = 20`, `LEVEL_TOL_ATR = 0.25`)
- Create: `packages/common/ba2_common/core/market_conditions_batch.py` (full-history batch implementation per D§3.3 "Precomputation, batch form": pivots by K-shifted comparisons, confirmation index `p + K`, nearest-level queries against the sorted CONFIRMED levels restricted to each session's own 128-session window, regression by cumulative sums over the rolling 20, prior range by rolling max/min shifted by one; returns one row per session whose 128-session window is valid)
- Facts from Task 8: the backtest seam is SINGLE-profile today (`install_backtest_market_conditions` reads `config["market_condition_profile"]` as one registered name and builds one reader; `_build_daily_trial_config` and `BacktestMarketConditionReader` likewise), so the launcher REFUSES a comma list with a message naming the seam — Task 10 widens the seam to a profile LIST (one manifest per profile pinned as `market_condition_manifests: {profile: digest}`, one mapped reader per profile behind a composite reader whose `observe` merges rows by field; `_ga_trial` refusals and coverage checks apply per profile) and then lifts the launcher refusal; `STRICT_FIELD_NAMES` in `market_condition_rules.py` already lists the five searched ta-structure names (registry-independent so an old server can refuse them) — keep it in sync with the registry (`test_every_registered_field_is_a_strict_name` enforces the direction); flip `test_a_payload_from_a_newer_server_is_refused_rather_than_deployed_ungated` into a real categorical export round-trip once the enum members exist.
- Facts from Task 9: `tools/backtest_parity.py` cannot yet parity-check a GATED genome — its row comparison walks `results` byte-for-byte and a profile-on run legitimately carries the `market_condition` block and per-trade `entry_state`; per design §8.8 ("research metadata is compared separately") add both to the tool's identity exclusions and compare them in a separate section, then re-run the D(b) gate (profile on + all modes off on a persisted gated genome) that Task 9 could only pin through the frozen four-arm test. `MarketConditionRunRecord`, the report and the bench iterate `PROFILES[...].fields`, so the second profile needs no change there; unlisted fields get bins derived from the persisted FieldSpec range. `note_conditions` reads the evaluator's flat `condition_evaluations` (safe while the launcher puts market gates on ONE entry rule per structure). `ENTRY_STATE_MAX_GAP_DAYS = 7` bounds the date-based trade↔state binding; bind on identity if the trade blob ever carries a recommendation/transaction id.
- Modify: `packages/common/ba2_common/core/types.py` — add one `ExpertEventType` member per chart-structure field (VALUE == field name; the twelve stored fields, so the unsearched ones can be searched later without an enum change) and extend the numeric list; then call `TradeConditions.register_market_condition_conditions()` and `rule_builders.register_market_condition_field_events()` after `register_profile` (Task 4 made both idempotent and re-callable; they skip and WARN on a field without an event type, and `test_every_registered_field_has_an_expert_event_type_with_the_same_value` fails until the members exist — that is the intended tripwire). The categorical `structure_state` field's stored observation value is its float code.
- Test: `packages/common/tests/test_chart_structure_calculators.py`, `packages/common/tests/test_chart_structure_batch_equals_reference.py`

**Context.** D§3.2 and D§3.3 are the contract, verbatim: confirmed pivots (strict `>` over K = 3 on both sides, ties are not pivots, a pivot at p exists only at indices >= p + K); R = smallest confirmed pivot-high price strictly above C[127], S = largest confirmed pivot-low price strictly below; distances divided by ATR[127] (Task 1's `atr14_wilder`, same series — pass `atr=` through); no qualifying pivot → UNKNOWN (never 0, never the window extreme); touches = confirmed pivots within `level ± 0.25 × ATR[127]` including the defining one (min 1 when the level exists); OLS over indices 108..127 with x = 0..19, residual σ with `ddof=2`, `channel_slope_20_atr = b/ATR`, `channel_width_20_atr = 4σ/ATR`, `channel_pos_20 = (C[127] − (a + 19b − 2σ))/(4σ)` UNCLAMPED, σ = 0 → width/pos unknown, slope valid 0; `close_vs_prior_high_20_atr = (C[127] − max(H[107..126]))/ATR`, low mirrors with min(L[107..126]); swing structure by alternating-swing reduction of the confirmed pivots, `bull`/`bear`/`none` from the last two swing highs and lows (equality is neither; fewer than two of either → `none` and both `bars_since` unknown); BOS/CHoCH walked backwards from 127 using only pivots confirmed by each session, `127 − index` of the most recent break, none in window → unknown (not 0, not 128). ATR[127] <= 0 → every field unknown. Reductions follow Task 1's bit-exact contract (`math.fsum`, `d*d`, explicit loops) — the OLS via cumulative sums in the batch must reproduce the reference's fsum-based fit exactly; if it cannot, the batch computes the 20-point fit per session with the same fsum reductions (correctness beats the cumulative-sum shortcut; D§3.3's "agree exactly" is the requirement).

**Tests (D§8 items 14–16):** a pivot absent from the row K−1 sessions after the extreme and present K sessions after; equal-price ties not pivots; a window with no pivot above the close → unknown; touch counting at the tolerance boundary (exactly on `± 0.25 ATR` counts — pin the inclusive rule); σ = 0 channel; a close outside the channel (pos outside 0..1, unclamped); breakout measured against the range that excludes session 127; alternating-swing reduction with two highs between two lows; each of bull/bear/none; BOS/CHoCH walked with pivots as confirmed at each session; intermediate pivot lists and swing sequences pinned, not only final fields; `test_batch_equals_reference_for_every_session_and_field` on three synthetic 600-bar histories and, if the local FMP cache has it, one real symbol (`CACHE_FOLDER/FMPOHLCVProvider/AAPL_1d.parquet`) — exact `==`; `test_adding_a_future_bar_changes_no_earlier_row` (the sharpest test: a batch using future-confirmed pivots fails it); golden `float.hex()` rows for two synthetic windows; registry test that the twelve FieldSpecs match the D§3.2 table (names, kinds, searched flags, ranges) and that `structure_state` codes exclude `none`. Measure and record cold build time per symbol and `observe()` lookup cost in the test log (D§8.15).

**Commit.** `feat(market-conditions): ta-structure-v1 chart-structure profile -- confirmed-pivot levels, 20-session channel, prior-range breakout, swing structure; batch equals reference`

---

### Task 11 (DEFERRED — operator 2026-09-15: "do not change strategy s1 s7, only option with these now"): Equity strategies S1–S7 placement, equity driver flag and the new-name equity grid definition

Not part of this delivery. Nothing in Tasks 1–10 may touch `_build_strategy_S1..S7`, the equity drivers or `grid_goal2020.sh`; Task 8's `_market_condition_gates` is wired into the OPTION builders only. Kept here so the placement rules are not lost.

**Files:**
- Modify: `testplatform/ba2test_launcher.py`: `_build_strategy_S1..S7` (line ~1632 `_build_strategy_row` = S2, S3 ~1721, S5 ~1782, S6 ~1856, S7 ~1905, S1 ~2083, S4 ~2199 — each has its own initial-entry AND tree; the market leaves are appended to THAT tree only, never to open-positions/exit rules) with ids `<s>-market-<short>` (`s1-market-dist-support` … per D§6.0), through the same `_market_condition_gates(m)` helper and the same `--market-condition-profile` CLI flag as Task 8; run config/deploy metadata identical to Task 8's.
- Modify: `tools/run_screener_capband_matrix.py` `build_cmd` (~line 444, next to `--robust-fitness`) and `tools/grid_goal2020.sh` (a `MARKET_CONDITION_PROFILE` env → flag, default `none`), plus a config digest for the equity driver if it has none (the Explore report: only `run_options_matrix.py` digests its argv; add the same `discovery_name`-style sha256 over the resolved argv minus `--name/--parallel/--workers` to the equity driver so a profile change is a new job identity).
- Create: `tools/grid_equity_mktcond.sh` — the new-name equity grid definition (D§6.0): same cells as goal2020 (expert × band × strategy), profile `ohlcv-v1,ta-structure-v1`, frozen all-off control and matched seeds, name suffix `-mc1`, results compared against the goal2020 cell of the same expert/band/strategy (a `tools/report_grid_results.py` option `--compare-to <name-pattern>`), never merged; a `--dry-run` that prints every resolved command; the union warmup (Task 6/7 CLI) for the EQUITY universe runs once before dispatch (D§6.0: the equity universe is the superset).
- Test: `testplatform/backend/tests/test_launcher_equity_market_condition_placement.py` (each of S1–S7 with profile `none` produces a byte-identical strategy dict to dev HEAD — pin literals from `git show cb584a60:testplatform/ba2test_launcher.py` output; with both profiles on, exactly 15 additional genes per strategy, all on the initial entry tree; exits untouched), `test_equity_driver_market_condition_flag.py` (flag reaches the command, digest changes with the flag), `test_grid_equity_mktcond_sh.py` (dry-run parity with `test_stage1_run_sh.py`'s pattern).

**Compatibility gate (D§8.17):** an equity job and an option job over the same universe and dates resolve the same manifest digest and read identical feature rows (test with the store from Task 6 on a tiny universe).

**Commit.** `feat(equity): market-condition profiles on S1-S7 initial-entry trees, equity driver flag + digest, new-name equity grid definition`

---

## Order, dependencies and what "done" means

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 (Task 11 deferred; options only in this delivery). Tasks 2–8 are written profile-generic from the start (registry-driven) so Task 10 registers a second profile without touching them; if a Task 2–8 implementer finds a hard-coded trio anywhere, that is a defect to fix in that task, not in Task 10. Tasks 2 and 3 can be reviewed in parallel with 4 but must be committed in order. After Task 9: run the full backend suite (`tests/backtest` and the rest as separate invocations), `packages/common/tests`, `packages/providers/tests`, and the root suite (two invocations per the worktree note); compare failures against the dev baseline recorded in memory (25 root failures in 7 files; backend fully green). Then the controller merges at a grid boundary and bumps `TEST_APP_VERSION` (packages + testplatform) and `APP_VERSION` (live seam wiring) once.

**Explicit decisions taken by this plan (deviations are not allowed without the controller):**
- Context reaches conditions through a resolver seam (`set_market_condition_context_resolver`), not new positional parameters on `create_condition`/`TradeActionEvaluator`.
- Live decision time comes from one `replay_now()` read per analysis, carried in a `ContextVar`, never a module global.
- The wheel's three leaves are renamed to `o_wheel-market-*` after reuse of the CSP rule.
- Authored template ops: slope `> 0.0`, ADX `< 25`, RV `< 1.0`; ranges exactly D§3.
- `mode` decoded as the token string on the leaf; `op` and `comparison` set together.
