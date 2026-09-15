"""ohlcv-v1 profile registry (design 2026-09-15 §3 table, amendment 2)."""
import dataclasses

import pytest

from ba2_common.core import market_conditions as mc
from ba2_common.core.market_conditions import (
    FIELDS, OHLCV_V1, PROFILES, FieldSpec, ProfileSpec, field_spec, profile_for_field, register_profile,
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
    assert (s.kind, s.short, s.searched_v1) == ("numeric", short, True)
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


def _cat(name="t_structure_state", codes=None):
    return FieldSpec(name, "categorical", "struct", True, codes=codes or {"bull": 1, "bear": 2})


def test_register_profile_rejects_duplicates(monkeypatch):
    monkeypatch.setattr(mc, "PROFILES", dict(mc.PROFILES))
    with pytest.raises(ValueError):
        register_profile(ProfileSpec(name="ohlcv-v1", calc_version="x", fields=(_cat(),)))
    with pytest.raises(ValueError):   # field already registered by ohlcv-v1
        register_profile(ProfileSpec(name="other", calc_version="x", fields=(
            FieldSpec("underlying_adx_14", "numeric", "adx", True, 10.0, 40.0, 5.0, "<", 25.0),)))
    with pytest.raises(ValueError):   # duplicate field inside one profile
        register_profile(ProfileSpec(name="dup", calc_version="x", fields=(_cat(), _cat())))
    with pytest.raises(ValueError):   # short id already used by ohlcv-v1 (launcher ids collide)
        register_profile(ProfileSpec(name="short", calc_version="x", fields=(
            FieldSpec("t_other", "categorical", "adx", True, codes={"a": 1}),)))
    with pytest.raises(ValueError):   # empty profile
        register_profile(ProfileSpec(name="empty", calc_version="x", fields=()))
    spec = ProfileSpec(name="t-v1", calc_version="t/calc-1", fields=(_cat(),))
    register_profile(spec)
    assert mc.PROFILES["t-v1"] is spec
    assert profile_for_field("t_structure_state") is spec
    assert field_spec("t_structure_state").codes == {"bull": 1, "bear": 2}
    assert "t-v1" not in PROFILES   # the monkeypatched copy is the one mutated


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
    dict(kind="numeric", value_min=1.0, value_max=2.0, value_step=0.5, anchor_op="<=", anchor_value=1.0),
    dict(kind="numeric", value_min=1.0, value_max=2.0, value_step=0.5, anchor_op="<", anchor_value=1.0,
         codes={"a": 1}),
    dict(kind="numeric", value_min=2.0, value_max=1.0, value_step=0.5, anchor_op="<", anchor_value=1.0),
    dict(kind="numeric", value_min=1.0, value_max=2.0, value_step=0.0, anchor_op="<", anchor_value=1.0),
    dict(kind="ordinal", value_min=1.0, value_max=2.0, value_step=0.5, anchor_op="<", anchor_value=1.0),
])
def test_field_spec_consistency_is_validated(kwargs):
    with pytest.raises(ValueError):
        FieldSpec(name="x", short="x", searched_v1=True, **kwargs)
