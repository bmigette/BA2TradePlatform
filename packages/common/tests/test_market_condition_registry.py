"""ohlcv-v1 profile registry (design 2026-09-15 §3 table, amendment 2)."""
import dataclasses

import pytest

from ba2_common.core import market_conditions as mc
from ba2_common.core.market_conditions import (
    FIELDS, OHLCV_V1, PROFILES, FieldSpec, ProfileSpec, field_codes, field_spec, profile_for_field,
    register_profile, registered_profile,
)


def test_ohlcv_v1_fields_match_calculator_fields_in_order():
    assert tuple(f.name for f in PROFILES["ohlcv-v1"].fields) == FIELDS
    assert PROFILES["ohlcv-v1"] is OHLCV_V1
    assert OHLCV_V1.calc_version == mc.CALC_VERSION


@pytest.mark.parametrize("name,short,lo,hi,step,op,anchor", [
    ("underlying_trend_slope_50_atr14", "slope", -0.30, 0.30, 0.05, ">", 0.0),
    ("underlying_adx_14", "adx", 10.0, 40.0, 5.0, "<", 25.0),
    ("underlying_realized_vol_ratio_5_20", "rv", 0.50, 2.00, 0.25, "<", 1.0),
])
def test_design_table_ranges_and_anchors(name, short, lo, hi, step, op, anchor):
    s = field_spec(name)
    assert (s.kind, s.short, s.searched) == ("numeric", short, True)
    assert (s.value_min, s.value_max, s.value_step) == (lo, hi, step)
    assert (s.anchor_op, s.anchor_value) == (op, anchor)
    assert s.codes is None and s.ui_name


def test_field_spec_lookup():
    assert field_spec("underlying_adx_14").short == "adx"
    with pytest.raises(KeyError) as ei:
        field_spec("nope")
    msg = str(ei.value)
    assert "nope" in msg
    for f in FIELDS:
        assert f in msg


def test_profile_for_field():
    assert profile_for_field("underlying_realized_vol_ratio_5_20") is OHLCV_V1
    with pytest.raises(KeyError):
        profile_for_field("nope")


def test_specs_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        OHLCV_V1.fields[0].short = "x"


def _cat(name="t_structure_state", short="struct", codes=None):
    return FieldSpec(name=name, kind="categorical", short=short, searched=True,
                     codes=codes or {"bull": 1, "bear": 2})


def test_categorical_spec_is_hashable_and_its_codes_are_immutable():
    raw = {"bull": 1, "bear": 2}
    spec = _cat(codes=raw)
    assert isinstance(hash(spec), int)
    assert hash(OHLCV_V1) == hash(OHLCV_V1)
    with pytest.raises(TypeError):
        spec.codes["none"] = 0
    raw["range"] = 3                      # the caller's dict is copied, not aliased
    assert dict(spec.codes) == {"bull": 1, "bear": 2}
    assert spec == _cat()


def test_categorical_field_spec_pickles_and_deep_copies_equal():
    import copy
    import pickle

    spec = _cat(codes={"bear": 2, "bull": 1})
    prof = ProfileSpec(name="p", calc_version="p/calc-1", fields=(spec,))
    for clone in (pickle.loads(pickle.dumps(spec)), copy.deepcopy(spec), copy.copy(spec)):
        assert clone == spec and hash(clone) == hash(spec)
        assert dict(clone.codes) == {"bull": 1, "bear": 2}
        with pytest.raises(TypeError):
            clone.codes["none"] = 0
    assert pickle.loads(pickle.dumps(prof)) == prof and copy.deepcopy(prof) == prof
    assert pickle.loads(pickle.dumps(OHLCV_V1)) == OHLCV_V1
    # codes participate in equality and hashing, independent of the input dict's order
    assert _cat(codes={"bull": 1, "bear": 2}) == _cat(codes={"bear": 2, "bull": 1})
    assert hash(_cat(codes={"bull": 1, "bear": 2})) == hash(_cat(codes={"bear": 2, "bull": 1}))
    assert _cat(codes={"bull": 1, "bear": 2}) != _cat(codes={"bull": 1, "bear": 3})


def test_codes_iterate_in_code_order_not_name_order():
    # Task 8 builds mode_choices=["off", *codes] from this order; the choice-gene index follows it.
    spec = _cat(codes={"bear": 2, "bull": 1})
    assert list(spec.codes) == ["bull", "bear"]
    assert list(_cat(codes={"a": 3, "z": 1, "m": 2}).codes) == ["z", "m", "a"]


@pytest.mark.parametrize("spec", [
    FieldSpec(name="t_num", kind="numeric", short="tn", searched=False, value_min=-1.0, value_max=1.0,
              value_step=0.5, anchor_op=">", anchor_value=0.0, ui_name="Num"),
    FieldSpec(name="t_cat", kind="categorical", short="tc", searched=True, codes={"bear": 2, "bull": 1},
              ui_name="Cat"),
], ids=["numeric", "categorical"])
def test_field_spec_to_dict_round_trips(spec):
    d = spec.to_dict()
    assert "_code_pairs" not in d
    assert list(d) == ["name", "kind", "short", "searched", "value_min", "value_max", "value_step",
                       "anchor_op", "anchor_value", "codes", "ui_name"]
    assert d["codes"] is None or type(d["codes"]) is dict
    assert FieldSpec(**d) == spec
    if spec.kind == "categorical":
        assert list(d["codes"].items()) == [("bull", 1), ("bear", 2)]


def test_dataclasses_replace_keeps_codes_and_needs_codes_none_to_turn_numeric():
    spec = _cat()
    assert dataclasses.replace(spec, ui_name="x").codes == spec.codes
    num = dict(kind="numeric", value_min=0.0, value_max=1.0, value_step=0.5, anchor_op="<", anchor_value=0.5)
    with pytest.raises(ValueError):
        dataclasses.replace(spec, **num)
    assert dataclasses.replace(spec, codes=None, **num).codes is None


def test_profile_spec_coerces_fields_to_a_tuple_and_requires_names():
    prof = ProfileSpec(name="p", calc_version="p/calc-1", fields=[_cat()])
    assert isinstance(prof.fields, tuple) and prof.fields == (_cat(),)
    with pytest.raises(ValueError):
        ProfileSpec(name="", calc_version="x", fields=(_cat(),))
    with pytest.raises(ValueError):
        ProfileSpec(name="p", calc_version="", fields=(_cat(),))


def test_register_profile_rejects_duplicates():
    with pytest.raises(ValueError):
        register_profile(ProfileSpec(name="ohlcv-v1", calc_version="x", fields=(_cat(),)))
    with pytest.raises(ValueError):   # field already registered by ohlcv-v1
        register_profile(ProfileSpec(name="other", calc_version="x", fields=(
            FieldSpec(name="underlying_adx_14", kind="numeric", short="adx2", searched=True, value_min=10.0,
                      value_max=40.0, value_step=5.0, anchor_op="<", anchor_value=25.0),)))
    with pytest.raises(ValueError):   # duplicate field inside one profile
        register_profile(ProfileSpec(name="dup", calc_version="x", fields=(_cat(), _cat())))
    with pytest.raises(ValueError):   # short id already used by ohlcv-v1 (launcher ids collide)
        register_profile(ProfileSpec(name="short", calc_version="x", fields=(_cat("t_other", short="adx"),)))
    with pytest.raises(ValueError):   # empty profile
        register_profile(ProfileSpec(name="empty", calc_version="x", fields=()))
    assert set(PROFILES) == {"ohlcv-v1", "ta-structure-v1"}   # nothing above was registered


def test_registered_profile_hook_registers_then_restores():
    before = dict(PROFILES)
    spec = ProfileSpec(name="t-v1", calc_version="t/calc-1", fields=(_cat(),))
    with registered_profile(spec) as got:
        assert got is spec
        assert mc.PROFILES["t-v1"] is spec
        assert profile_for_field("t_structure_state") is spec
        assert field_spec("t_structure_state").codes == {"bull": 1, "bear": 2}
        with pytest.raises(ValueError):   # re-registering inside the context is still refused
            register_profile(spec)
    assert "t-v1" not in PROFILES and PROFILES == before   # original dict unchanged
    assert mc.PROFILES is PROFILES
    with pytest.raises(KeyError):
        field_spec("t_structure_state")


def test_field_codes_memo_returns_the_same_read_only_mapping():
    with registered_profile(ProfileSpec(name="t-memo", calc_version="x", fields=(_cat(),))):
        first = field_codes("t_structure_state")
        assert first is field_codes("t_structure_state")
        assert dict(first) == {"bull": 1, "bear": 2}
        with pytest.raises(TypeError):
            first["sideways"] = 3


def test_field_codes_memo_is_invalidated_where_the_registry_changes():
    with registered_profile(ProfileSpec(name="t-memo1", calc_version="x", fields=(_cat(),))):
        assert dict(field_codes("t_structure_state")) == {"bull": 1, "bear": 2}
    with pytest.raises(KeyError):
        field_codes("t_structure_state")
    other = _cat(codes={"bull": 1, "bear": 2, "chop": 3})
    with registered_profile(ProfileSpec(name="t-memo2", calc_version="x", fields=(other,))):
        assert dict(field_codes("t_structure_state")) == {"bull": 1, "bear": 2, "chop": 3}


def test_field_codes_memo_is_cleared_by_register_profile():
    # A KeyError is not memoised, but a stale mapping would be: register_profile must clear.
    saved = dict(PROFILES)
    try:
        register_profile(ProfileSpec(name="t-memo3", calc_version="x", fields=(_cat(),)))
        assert dict(field_codes("t_structure_state")) == {"bull": 1, "bear": 2}
        PROFILES.clear()
        PROFILES.update(saved)
        register_profile(ProfileSpec(name="t-memo4", calc_version="x",
                                     fields=(_cat(codes={"up": 5}),)))
        assert dict(field_codes("t_structure_state")) == {"up": 5}
    finally:
        PROFILES.clear()
        PROFILES.update(saved)
        field_codes.cache_clear()


def test_field_codes_on_a_numeric_field_raises_value_error_naming_it():
    with pytest.raises(ValueError, match="underlying_adx_14"):
        field_codes("underlying_adx_14")


def test_registered_profile_restores_after_an_exception_inside():
    before = dict(PROFILES)
    with pytest.raises(RuntimeError):
        with registered_profile(ProfileSpec(name="t-v2", calc_version="x", fields=(_cat(),))):
            raise RuntimeError("boom")
    assert PROFILES == before


_NUM = dict(value_min=1.0, value_max=2.0, value_step=0.5, anchor_op="<", anchor_value=1.0)


@pytest.mark.parametrize("kwargs", [
    dict(kind="categorical", codes={"bull": 1, "none": 0}),
    dict(kind="categorical", codes={"bull": 1, "none": 2}),
    dict(kind="categorical", codes={}),
    dict(kind="categorical", codes=None),
    dict(kind="categorical", codes={"bull": 1, "bear": 1}),
    dict(kind="categorical", codes={"bull": 0, "bear": 1}),
    dict(kind="categorical", codes={"bull": True, "bear": 2}),
    dict(kind="categorical", codes={"bull": 1, "bear": 2}, value_min=1.0),
    dict(kind="numeric"),
    dict(kind="numeric", value_min=1.0, value_max=2.0),
    dict(kind="numeric", **{**_NUM, "anchor_op": "<="}),
    dict(kind="numeric", **_NUM, codes={"a": 1}),
    dict(kind="numeric", **{**_NUM, "value_min": 2.0, "value_max": 1.0}),
    dict(kind="numeric", **{**_NUM, "value_step": 0.0}),
    dict(kind="numeric", **{**_NUM, "anchor_value": 0.5}),
    dict(kind="numeric", **{**_NUM, "anchor_value": 2.5}),
    dict(kind="ordinal", **_NUM),
], ids=["none-code-0", "none-code", "empty-codes", "no-codes", "dup-codes", "zero-code", "bool-code",
        "cat-with-range", "num-no-range", "num-no-step", "bad-anchor-op", "num-with-codes",
        "min-above-max", "zero-step", "anchor-below-min", "anchor-above-max", "bad-kind"])
def test_field_spec_consistency_is_validated(kwargs):
    with pytest.raises(ValueError):
        FieldSpec(name="x", short="x", searched=True, **kwargs)


def test_anchor_on_range_bounds_is_allowed():
    FieldSpec(name="x", short="x", searched=True, kind="numeric", **{**_NUM, "anchor_value": 1.0})
    FieldSpec(name="x", short="x", searched=True, kind="numeric", **{**_NUM, "anchor_value": 2.0})
