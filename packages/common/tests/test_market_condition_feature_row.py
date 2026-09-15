"""The field-generic ``FeatureRow`` and the per-profile compute registry."""
from __future__ import annotations

import numpy as np
import pytest

from ba2_common.core.market_condition_context import FeatureRowLike
from ba2_common.core.market_conditions import (
    CALC_VERSION,
    COMPUTE_BY_PROFILE,
    FIELDS,
    OHLCV_V1,
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_VALID,
    WINDOW,
    FeatureRow,
    Observation,
    compute_market_conditions,
)


def _window(seed=3):
    rng = np.random.default_rng(seed)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, WINDOW)))
    o = c * (1 + rng.normal(0, 0.002, WINDOW))
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * 0.99
    v = np.full(WINDOW, 1e6)
    return o, h, l, c, v


def test_to_feature_row_carries_every_field_and_version():
    values = compute_market_conditions(*_window())
    row = values.to_feature_row()
    assert isinstance(row, FeatureRowLike)
    assert dict(row.by_field()) == values.by_field()
    assert dict(row.calc_versions) == {f: CALC_VERSION for f in FIELDS}


def test_by_field_returns_the_stored_mapping_read_only():
    row = compute_market_conditions(*_window()).to_feature_row()
    assert row.by_field() is row.by_field()
    with pytest.raises(TypeError):
        row.by_field()["x"] = Observation(None, STATUS_INSUFFICIENT_HISTORY)  # type: ignore[index]


def test_construction_copies_the_callers_mapping():
    values = {"f": Observation(1.0, STATUS_VALID)}
    versions = {"f": "p/calc-1"}
    row = FeatureRow(values=values, calc_versions=versions)
    values["g"] = Observation(2.0, STATUS_VALID)
    versions["f"] = "changed"
    assert list(row.by_field()) == ["f"]
    assert row.calc_versions["f"] == "p/calc-1"


@pytest.mark.parametrize("values,versions", [
    ({}, {}),
    ({"f": 1.0}, {"f": "v"}),
    ({"f": Observation(1.0, STATUS_VALID)}, {}),
    ({"f": Observation(1.0, STATUS_VALID)}, {"f": ""}),
])
def test_invalid_rows_are_refused(values, versions):
    with pytest.raises(ValueError):
        FeatureRow(values=values, calc_versions=versions)


def test_uniform_row_covers_the_profile():
    row = FeatureRow.uniform(OHLCV_V1, STATUS_INSUFFICIENT_HISTORY, "short")
    assert list(row.by_field()) == [f.name for f in OHLCV_V1.fields]
    assert all(o.status == STATUS_INSUFFICIENT_HISTORY and o.value is None for o in row.by_field().values())
    with pytest.raises(ValueError):
        FeatureRow.uniform(OHLCV_V1, STATUS_VALID, "")


def test_compute_registry_covers_ohlcv_v1():
    assert set(COMPUTE_BY_PROFILE) <= set(PROFILES)
    row = COMPUTE_BY_PROFILE["ohlcv-v1"](*_window())
    assert row == compute_market_conditions(*_window()).to_feature_row()
