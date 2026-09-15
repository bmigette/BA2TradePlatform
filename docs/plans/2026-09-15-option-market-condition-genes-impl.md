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

**Gene names (D§5):** `cond:<leaf-id>:mode` (choice gene over `["off","below","above"]`) and `cond:<leaf-id>:value` (range). Leaf ids: `<member>-market-slope`, `<member>-market-adx`, `<member>-market-rv` where `<member>` is the structure key lower-cased (`o_lc`, `o_ic`, …); O_CC → `o_cc-market-*`, O_PP → `o_pp-market-*`, O_WHEEL → `o_wheel-market-*` (D§5: wheel gets its own ids even though its entry is the CSP builder).

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

**Context.** D§5. `ConditionLeaf.to_canonical_dict` rebuilds from DECLARED fields only, so the three new fields MUST be declared (the file's own comment on `value_offset_from` says why). Reject `mode_optimize=True` together with `toggle_optimize=True` (two disable controls). Reject unknown `mode` values and unsupported choice lists (only `["off","below","above"]` in v1). A legacy leaf without these fields is unchanged (assert canonical output equality on an existing fixture).

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

**Context.** Categorical genes already exist at the ACTION level: `option_strike_method` emits `{"type": "choice", "choices": [...], "min": 0, "max": len-1, "step": 1}` (`strategy_param_space.py:232-256`) and `genetic.py` decodes `type=="choice"` as an index. Reuse that shape for `cond:<id>:mode`. Decode (`_apply_to_tree._recurse`): a leaf whose decoded `mode == "off"` is DROPPED from its parent's `conditions` (exactly like `enabled == 0`); `"below"` sets `node["op"] = node["comparison"] = "<"`; `"above"` sets both to `">"` (D§5: "decode must synchronize op and comparison"). `decode_params` already routes any `cond:<id>:<field>` into `cond_by_id` (line 731-733), so no change there. A leaf with `mode_optimize` AND `toggle_optimize` must raise at collection (belt and braces with Task 2).

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

Condition classes: `UnderlyingTrendSlopeCondition`, `UnderlyingAdxCondition`, `UnderlyingRealizedVolRatioCondition`, all `CompareCondition` subclasses with a shared mixin `_MarketConditionCompare` holding `FIELD` and:

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

BT adapter: `AsOfPriceSource.window_before(symbol, session, n)` returns the arrays for the `n` sessions ending at `session` from its private `_o/_h/_l/_c/_v` columns (no DataFrame), or `None` if coverage is short; the BT reader (a small class in the backend) wraps it and memoizes by `(symbol, session)` per process with a bounded LRU (2000 entries). The BT resolver builds the context from the engine's current simulated date (`account._as_of_date()` gives the date; the effective session label is that date and `prior_session = prior_regular_session(that date)`).

Live adapter: reads the FMP OHLCV provider's CACHE ONLY (`cached_only`-style path, never a network call inside a condition — find the provider's cache read API); `decision_time = replay_now()` read once in the coordinating thread of the scheduled analysis and stored on the resolver for the duration of that analysis (design: "different analyses cannot share a mutable global clock" → keep it per-analysis, e.g. a `contextvars.ContextVar` set by the analysis coordinator, not a module global).

**Commit** (may be two commits: sessions+certification, then adapters). `feat(market-conditions): prior_session_v1 calendar lookup, source certification, BT/live context adapters and replay capture`

---

### Task 6: Central feature store, manifests and the warmup service (plan / build / verify)

**Files:**
- Create: `packages/common/ba2_common/core/market_condition_store.py` (objects, manifests, identity, reader over parquet)
- Create: `packages/providers/ba2_providers/market_conditions/warmup.py` (plan/fetch/build/publish orchestration using existing OHLCV provider warmers)
- Create: `tools/warm_market_conditions.py` (CLI: `plan`, `build --cache-only|--fetch-missing`, `verify`, `prepare-host` — prepare-host lands in Task 7)
- Test: `packages/common/tests/test_market_condition_store.py`, `packages/providers/tests/test_market_condition_warmup.py`, `testplatform/backend/tests/test_warm_market_conditions_cli.py`

**Context.** D§4.2–4.4, D§4.6. Layout under `CACHE_FOLDER/market_conditions/ohlcv-v1/`: `objects/<sha256>.parquet` (feature shards, rows = symbol × session; columns: `symbol`, `session` (date32), the three float64 values, three status codes (int8 over the `STATUSES` order), `window_digest` (32 bytes), `raw_shard_ref` (string), `raw_row_lo`, `raw_row_hi`), `raw/<sha256>.parquet` (normalized 5-column float64 bar shards per symbol/month, deduplicated), `manifests/<sha256>.json`. Sharding: one feature object per (symbol, calendar month of the session). Manifest JSON pins: `source_profile`, `timing_policy`, `calc_version`, `calendar_version` (pandas_market_calendars version string), `schema_version`, `objects` (list of `{path, sha256, symbol, month, rows}`), `raw_objects`, `coverage` (`{symbol: {first_session, last_session, rows, exceptions: [...]}}`), `created_at`, `universe_digest`, `window_start`, `window_end`. Portable identity = sha256 of the canonical manifest JSON without `created_at`.

Store API: `MarketConditionStore(root)` with `write_manifest`, `read_manifest(digest)`, `iter_rows(manifest, symbol)`, `verify(manifest) -> report` (hashes every referenced object; size equality alone is not integrity), `retained_window(window_digest) -> (o,h,l,c,v)`.

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

**Context.** D§4.3, D§4.5, D§4.6. Mapped layout per manifest: key `mc_<manifest-digest[:16]>_v<LAYOUT_VERSION>` in `DerivedArrayStore` under `derived_root_for(CACHE_FOLDER)/market_conditions/`; arrays: `symbol_index` (int32 codes + a small JSON symbol table in the marker), `session` (int32 days since epoch, sorted per symbol), `values` (float64 [n,3]), `status` (int8 [n,3]), `offsets` (int64 per symbol). `observe(symbol, session)` = bisect on the symbol's session slice → `MarketConditionValues`; a symbol/session absent → `None`. Bounded handle cache; reopen after worker recycle (`BT_MAX_TASKS_PER_CHILD`). `BA2_SHARED_ARRAYS=0` loads the same central rows into private arrays (no recompute). Keep `ensure_fd_headroom()`; the reader holds ≤ 5 descriptors per manifest.

Tests: values from the mapped reader equal the parquet reader for every row; a second host given only the manifest+objects builds mappings without recomputing (counter = 0 computed rows); a corrupted object with the same size is rejected by verification; a worker whose local snapshot is missing reports unready and receives no trials; recycling a worker reopens the mapping and answers identically; descriptor count bounded.

**Commit.** `feat(market-conditions): host-shared mapped feature reader, prepare-host, worker readiness on the manifest digest, hash-verified sync`

---

### Task 8: Launcher profile, per-structure placement, driver flag and deployment export

**Files:**
- Modify: `testplatform/ba2test_launcher.py`: new module flag `_MARKET_CONDITION_PROFILE` (`none` default) set by CLI `--market-condition-profile {none,ohlcv-v1}`; `_market_condition_gates(m: str) -> list[dict]` (three leaves per D§5 JSON, ids `<m>-market-slope|adx|rv`, ranges per D§3 table, `mode_optimize=True`, `mode_choices=["off","below","above"]`, authored `op`/`value`: slope `> 0.0`, adx `< 25`, rv `< 1.0` — the template's explicit fixed interpretation); appended at the END of `_option_entry_rule`'s AND list (line ~4517-4523) when the profile is on; for O_CC/O_PP appended to the `_build_strategy_row` entry tree (line ~1638) with `m = kind.lower()` (`o_cc`/`o_pp`) — this tree has no prefix convention today, so the three new leaf ids are the first prefixed ones there; for O_WHEEL, `_build_strategy_wheel` (line ~4857) reuses O_CSP's rule: after building, RENAME the three market leaves' ids from `o_csp-market-*` to `o_wheel-market-*` (D§5 wheel-specific ids). Run config records `market_condition_profile`, `source_profile`, `timing_policy`, `calc_version`, `market_condition_manifest` (digest) and these flow into the configuration digest and every checkpoint/distributed payload.
- Modify: `tools/run_options_matrix.py` `build_cmd` (pass `--market-condition-profile` through; it folds into `discovery_name`'s digest automatically) and `--profile discovery` help text; `tools/stage1_run.sh` gets `MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-none}"` and, when not `none`, runs `tools/warm_market_conditions.py plan+build+verify+prepare-host` ONCE before dispatch and passes the manifest digest.
- Modify: deployment export/import (`tools/export_deploy_payload.py` / `import_deploy_payload.py` or the senate equivalents, whichever the option deploy uses): the exporter REJECTS a payload containing an unresolved `mode` gene or `mode_optimize` leaf (D§5); resolved leaves export as ordinary numeric conditions; import on a server whose `FIELD_EVENT` lacks the new fields must REJECT, not drop (add the check in `rule_builders.triggers_from_condition_tree`'s caller used by import: an unmapped field that is in a `STRICT_FIELDS` set raises instead of warning).
- Test: `testplatform/backend/tests/test_launcher_market_condition_profile.py`, `test_option_matrix_market_condition_flag.py`, `test_deploy_payload_market_conditions.py`; existing `test_launcher_option_entry_rule.py`, `test_option_grid_foundations.py`, `test_stage1_run_sh.py`, `test_convex_matrix_script.py` must remain green with profile `none`.

**Tests:** profile `none` → `_option_entry_rule("O_LC")` is byte-identical to dev HEAD's output (pin a literal); profile `ohlcv-v1` → each of the 16 permitted structures collects exactly six more genes named per the contract; O_CC/O_PP place them in the stock-entry tree and NOT in the overlay rules; O_WHEEL ids are `o_wheel-market-*`; every new leaf reaches the engine (`triggers_from_condition_tree` yields 3 more triggers per structure); the all-off control (three `mode=off`) decodes to a tree equal to profile `none`'s; `discovery_name` digest changes when the flag changes and is stable otherwise; `stage1_run.sh --dry-run` prints the warm step before the matrix when the profile is on; exporter rejects unresolved genes; importer rejects unknown strict fields.

**Commit.** `feat(launcher): --market-condition-profile ohlcv-v1 appends the three mode/threshold gates per structure; driver flag, warm-once preflight, strict deploy export/import`

---

### Task 9: Coverage/performance report, entry-state attribution and the all-off compatibility gate

**Files:**
- Create: `tools/report_market_conditions.py` (per-job summary per D§7: winning modes/thresholds, versions, eligible recommendations vs gate-rejected vs unknown-input counts by reason, submitted/filled structures, per-year profit/CAR/DD from the account engine, attribution of executed structures' net P&L and top-1/top-5 concentration to the recorded entry-state measurement bins with explicit bin edges; NEVER annualises a filtered trade subset as a funded account)
- Modify: the backtest results blob to record per-trade entry-state values (the three values + statuses at entry) when the profile is on; the daily engine's stats to count `eligible`, `gate_rejected`, `unknown_input_by_reason` for the new conditions only (D§6 "Report eligible recommendations separately from condition rejections")
- Test: `testplatform/backend/tests/backtest/test_market_condition_all_off_matches_baseline.py` (a frozen small option run — reuse `golden/option_leap_golden_run.json`'s fixture pattern — run with profile `none` and with profile `ohlcv-v1` + all three modes `off`: orders, trades and equity curve byte-identical; research metadata compared separately), `test_report_market_conditions.py`, plus the existing golden/parity suites (`test_engine_golden_regression.py`, `test_option_golden_run.py`, `test_parity_golden.py`) green.
- Benchmark script: `testplatform/backend/tests_scripts/bench_market_conditions.py` producing the D§4.6 counters on a fixed 20-symbol subset: cold build time, repeat warmup time, prepare-host time, P50/P95 `observe()` latency, worker RSS and descriptor count at the intended 30 workers. Write results to `reports/strategy_research/market_conditions_bench_<date>.md`.

**Commit.** `feat(market-conditions): per-job coverage/attribution report, entry-state capture, all-off compatibility gate and benchmark`

---

## Order, dependencies and what "done" means

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9. Tasks 2 and 3 can be reviewed in parallel with 4 but must be committed in order. After Task 9: run the full backend suite (`tests/backtest` and the rest as separate invocations), `packages/common/tests`, `packages/providers/tests`, and the root suite (two invocations per the worktree note); compare failures against the dev baseline recorded in memory (25 root failures in 7 files; backend fully green). Then the controller merges at a grid boundary and bumps `TEST_APP_VERSION` (packages + testplatform) and `APP_VERSION` (live seam wiring) once.

**Explicit decisions taken by this plan (deviations are not allowed without the controller):**
- Context reaches conditions through a resolver seam (`set_market_condition_context_resolver`), not new positional parameters on `create_condition`/`TradeActionEvaluator`.
- Live decision time comes from one `replay_now()` read per analysis, carried in a `ContextVar`, never a module global.
- The wheel's three leaves are renamed to `o_wheel-market-*` after reuse of the CSP rule.
- Authored template ops: slope `> 0.0`, ADX `< 25`, RV `< 1.0`; ranges exactly D§3.
- `mode` decoded as the token string on the leaf; `op` and `comparison` set together.
