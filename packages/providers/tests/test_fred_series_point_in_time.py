"""Point-in-time correctness for the FRED series cache.

The bug these guard against: a backtest standing on 2024-01-31 seeing January's
unemployment rate, which was not published until 2024-02-02. Filtering on the
observation date leaks a month of hindsight into every macro regime call.

No network: each test writes a synthetic cache file and reads it back.
"""
import json
import os

import pytest

from ba2_providers.macro import fred_series as fs


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "CACHE_FOLDER", str(tmp_path))
    fs.reset_cache()
    yield
    fs.reset_cache()


def _write(series_id: str, vintage: bool, observations: list) -> None:
    os.makedirs(os.path.join(fs.CACHE_FOLDER, "fred"), exist_ok=True)
    with open(fs.cache_path(series_id), "w", encoding="utf-8") as fh:
        json.dump({"series_id": series_id, "vintage": vintage,
                   "observations": observations}, fh)


def test_vintage_series_hides_observations_not_yet_published():
    """UNRATE for January is dated 2024-01-01 but first published 2024-02-02."""
    _write("UNRATE", True, [
        {"date": "2023-12-01", "value": "3.7", "realtime_start": "2024-01-05"},
        {"date": "2024-01-01", "value": "3.9", "realtime_start": "2024-02-02"},
    ])

    before = fs.get_series_as_of("UNRATE", "2024-02-01")
    assert len(before) == 1
    assert str(before.index[-1].date()) == "2023-12-01", (
        "January's reading leaked before its publication date")

    on_release = fs.get_series_as_of("UNRATE", "2024-02-02")
    assert len(on_release) == 2
    assert str(on_release.index[-1].date()) == "2024-01-01"
    assert on_release.iloc[-1] == pytest.approx(3.9)


def test_unrevised_series_cuts_on_observation_date():
    """Daily series are published same-day, so the observation date is the cut."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
        {"date": "2024-03-18", "value": "14.33"},
    ])

    s = fs.get_series_as_of("VIXCLS", "2024-03-15")
    assert len(s) == 2
    assert s.iloc[-1] == pytest.approx(14.41)


def test_as_of_none_returns_everything():
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
    ])
    assert len(fs.get_series_as_of("VIXCLS", None)) == 2


def test_missing_values_are_skipped_not_zeroed():
    """FRED marks gaps with '.'; coercing those to 0.0 would poison a z-score."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "."},
    ])
    s = fs.get_series_as_of("VIXCLS", None)
    assert len(s) == 1
    assert s.iloc[-1] == pytest.approx(14.40)


def test_unknown_series_raises_rather_than_guessing_vintage_mode():
    with pytest.raises(ValueError, match="Add it to SERIES_SPEC"):
        fs.get_series_as_of("NOTASERIES", None)


def test_missing_cache_file_raises_pointing_at_prewarm():
    """A backtest must fail loudly, not silently fetch or return an empty series."""
    with pytest.raises(FileNotFoundError, match="prewarm"):
        fs.get_series_as_of("UNRATE", None)


def test_napm_is_not_in_the_spec():
    """ISM pulled FRED licensing; NAPM 404s. It must not reappear by copy-paste."""
    assert "NAPM" not in fs.SERIES_SPEC


def test_ice_bofa_hy_oas_is_not_in_the_spec():
    """FRED serves ICE indices on a rolling ~3y licence -- unusable pre-2023."""
    assert "BAMLH0A0HYM2" not in fs.SERIES_SPEC
    assert "BAA10Y" in fs.SERIES_SPEC
