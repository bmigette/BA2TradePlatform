"""session_volume: the one shared rule for "volume traded in the data session" (plan step B1)."""
from datetime import date, datetime

import numpy as np
import pytest

from ba2_common.core.option_session import session_volume

D = date(2025, 6, 9)
PREV = date(2025, 6, 6)
NEXT = date(2025, 6, 10)


def test_exact_match_returns_its_volume():
    assert session_volume([(D, 120)], D) == 120


def test_only_older_bar_is_zero():
    assert session_volume([(PREV, 500)], D) == 0


def test_only_newer_bar_is_zero_never_lookahead():
    assert session_volume([(NEXT, 500)], D) == 0


def test_picks_the_bar_dated_data_session():
    assert session_volume([(PREV, 7), (D, 42)], D) == 42


def test_zero_volume_bar_is_zero():
    assert session_volume([(D, 0)], D) == 0


def test_matching_bar_without_volume_is_refused():
    with pytest.raises(ValueError):
        session_volume([(D, None)], D)


def test_empty_is_zero():
    assert session_volume([], D) == 0


def test_duplicate_date_conflict_is_refused():
    with pytest.raises(ValueError):
        session_volume([(D, 1), (D, 2)], D)


def test_duplicate_date_same_volume_is_accepted():
    assert session_volume([(D, 5), (D, 5)], D) == 5


def test_datetime_data_session_is_refused():
    with pytest.raises(TypeError):
        session_volume([(D, 1)], datetime(2025, 6, 9, 16, 0))


def test_datetime_bar_date_is_refused():
    with pytest.raises(TypeError):
        session_volume([(datetime(2025, 6, 9, 16, 0), 1)], D)


def test_nan_volume_on_matching_bar_is_refused():
    with pytest.raises(ValueError):
        session_volume([(D, float("nan"))], D)


def test_pandas_na_volume_on_matching_bar_is_refused():
    pd = pytest.importorskip("pandas")
    with pytest.raises(ValueError):
        session_volume([(D, pd.NA)], D)


def test_negative_volume_is_refused():
    with pytest.raises(ValueError):
        session_volume([(D, -1)], D)


def test_non_integral_volume_is_refused():
    with pytest.raises(ValueError):
        session_volume([(D, 2.5)], D)


def test_integral_float_volume_is_accepted_as_int():
    v = session_volume([(D, 7.0)], D)
    assert v == 7 and type(v) is int


def test_numpy_int64_returns_plain_int():
    v = session_volume([(D, np.int64(42))], D)
    assert v == 42 and type(v) is int


def test_duplicate_compared_after_normalisation():
    assert session_volume([(D, np.int64(5)), (D, 5.0)], D) == 5


def test_nan_on_a_non_matching_bar_is_ignored():
    assert session_volume([(PREV, float("nan")), (D, 3)], D) == 3
