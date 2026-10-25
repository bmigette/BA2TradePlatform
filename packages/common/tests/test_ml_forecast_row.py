import math
from datetime import date

import pytest

from ba2_common.core.ml_forecast import forecast_row as fr
from ba2_common.core.ml_forecast.forecast_row import (
    DECILE_LEVELS, SL_GRID_PCT, TP_GRID_PCT, ForecastRow, decile_col, prob_col,
)

H = (5, 10)


def deciles(center=0.01, step=0.01):
    return tuple(center + step * (i - 4) for i in range(9))


def make_row(**over):
    base = dict(symbol="AAPL", session=date(2024, 3, 1), deciles={5: deciles(), 10: deciles(0.02, 0.015)}, path_probs=None)
    base.update(over)
    return ForecastRow(**base)


def full_probs(p=0.5):
    return {(d, h, tp, sl): p for d in ("long", "short") for h in H for tp in TP_GRID_PCT for sl in SL_GRID_PCT}


def test_constants_and_names():
    assert DECILE_LEVELS == (10, 20, 30, 40, 50, 60, 70, 80, 90)
    assert decile_col(5, 50) == "h5_q50"
    assert prob_col("long", 10, 3, 2) == "p_long_h10_tp3_sl2"
    assert len(TP_GRID_PCT) >= 3 and len(SL_GRID_PCT) >= 3
    assert list(TP_GRID_PCT) == sorted(TP_GRID_PCT)


def test_valid_row_and_accessors():
    r = make_row()
    assert r.horizons == (5, 10)
    assert r.decile(5, 50) == pytest.approx(0.01)
    assert not r.has_paths


def test_symbol_uppercased_and_validated():
    assert make_row(symbol="aapl").symbol == "AAPL"
    with pytest.raises(ValueError):
        make_row(symbol="")


def test_session_must_be_plain_date():
    from datetime import datetime
    with pytest.raises(TypeError):
        make_row(session=datetime(2024, 3, 1, 10))
    with pytest.raises(TypeError):
        make_row(session="2024-03-01")


@pytest.mark.parametrize("bad", [
    {5: deciles()[:8]},                                    # wrong count
    {5: tuple(reversed(deciles()))},                       # not monotone
    {5: (float("nan"),) + deciles()[1:]},                  # nan
    {5: (-1.5,) + deciles()[1:]},                          # return below -100%
    {},                                                    # empty
    {0: deciles()},                                        # bad horizon
])
def test_invalid_deciles_raise(bad):
    with pytest.raises(ValueError):
        make_row(deciles=bad)


def test_flat_deciles_allowed():
    make_row(deciles={5: (0.0,) * 9, 10: (0.0,) * 9})


def test_probs_validated():
    r = make_row(path_probs=full_probs(0.4))
    assert r.has_paths and r.path_prob("long", 5, TP_GRID_PCT[0], SL_GRID_PCT[0]) == 0.4
    with pytest.raises(ValueError):
        make_row(path_probs={**full_probs(), ("long", 5, TP_GRID_PCT[0], SL_GRID_PCT[0]): 1.2})
    with pytest.raises(ValueError):
        make_row(path_probs={**full_probs(), ("long", 5, TP_GRID_PCT[0], SL_GRID_PCT[0]): float("nan")})
    incomplete = full_probs()
    incomplete.pop(("short", 10, TP_GRID_PCT[0], SL_GRID_PCT[0]))
    with pytest.raises(ValueError):
        make_row(path_probs=incomplete)
    with pytest.raises(ValueError):
        make_row(path_probs={**full_probs(), ("long", 5, 99, 1): 0.5})   # off-grid cell


def test_record_round_trip():
    for r in (make_row(), make_row(path_probs=full_probs(0.3))):
        rec = r.to_record()
        assert rec["session"] == r.session
        back = ForecastRow.from_record(r.symbol, rec, horizons=H, with_paths=r.has_paths)
        assert back == r


def test_from_record_missing_column_raises():
    rec = make_row().to_record()
    rec.pop("h5_q30")
    with pytest.raises(ValueError):
        ForecastRow.from_record("AAPL", rec, horizons=H, with_paths=False)


def test_column_list_matches_record():
    assert set(fr.record_columns(H, with_paths=True)) == set(make_row(path_probs=full_probs()).to_record())
    assert set(fr.record_columns(H, with_paths=False)) == set(make_row().to_record())
