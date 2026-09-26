"""The option risk-free rate: as-of FRED DGS3MO from the disk cache, refusing loudly.

Pins: cache-only read (no network), refusal on a missing file / a window the series does not
cover / a gap inside it, no lookahead (``rate_on(d)`` never sees an observation dated after
``d``, and agrees with ``fred_series.get_series_as_of``), forward fill, and the explicit
constant that a run records as ``explicit``.
"""
import json
from datetime import date, datetime, timedelta

import pytest

from ba2_providers.macro import fred_series
from ba2_providers.macro import risk_free_rate as rfr


def _write(path, rows):
    path.write_text(json.dumps({"series_id": "DGS3MO", "fetched_at": "2026-01-01T00:00:00",
                                "observations": [{"date": d, "value": v} for d, v in rows]}))
    return str(path)


def _weekdays(start, end, value_of):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append((d.isoformat(), value_of(d)))
        d += timedelta(days=1)
    return out


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(fred_series, "_MEM", {}, raising=False)
    (tmp_path / "fred").mkdir()
    return tmp_path / "fred" / "DGS3MO.json"


# ------------------------------------------------------------------------------ refusals
def test_a_missing_cache_file_refuses(cache):
    with pytest.raises(rfr.RiskFreeRateUnavailable, match="not in the cache"):
        rfr.fred_dgs3mo_rate(date(2024, 1, 1), date(2024, 6, 30))


def test_the_read_never_touches_the_network(cache, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network")

    monkeypatch.setattr(fred_series, "refresh_series", _boom)
    monkeypatch.setattr(fred_series.requests, "get", _boom)
    with pytest.raises(rfr.RiskFreeRateUnavailable):
        rfr.fred_dgs3mo_rate(date(2024, 1, 1), date(2024, 6, 30))


def test_a_series_ending_before_the_window_refuses(cache):
    _write(cache, _weekdays(date(2023, 1, 2), date(2024, 3, 29), lambda d: "5.0"))
    with pytest.raises(rfr.RiskFreeRateUnavailable, match="before the window end"):
        rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))


def test_a_series_starting_after_the_window_refuses(cache):
    _write(cache, _weekdays(date(2024, 3, 1), date(2024, 12, 31), lambda d: "5.0"))
    with pytest.raises(rfr.RiskFreeRateUnavailable, match="does not cover the start"):
        rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))


def test_a_gap_inside_the_window_refuses(cache):
    rows = [r for r in _weekdays(date(2023, 12, 1), date(2024, 7, 31), lambda d: "5.0")
            if not ("2024-03-01" <= r[0] <= "2024-03-20")]
    _write(cache, rows)
    with pytest.raises(rfr.RiskFreeRateUnavailable, match="gap"):
        rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))


def test_a_lookup_outside_the_covered_history_refuses(cache):
    _write(cache, _weekdays(date(2024, 1, 2), date(2024, 6, 28), lambda d: "5.0"))
    rate = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))
    with pytest.raises(rfr.RiskFreeRateUnavailable):
        rate.rate_on(date(2023, 12, 1))      # nothing on or before it
    with pytest.raises(rfr.RiskFreeRateUnavailable):
        rate.rate_on(date(2024, 8, 30))      # last observation is 2 months stale


def test_a_malformed_row_refuses(cache):
    _write(cache, [("2024-01-02", "5.0"), ("2024-01-03", "five")])
    with pytest.raises(rfr.RiskFreeRateUnavailable, match="malformed"):
        rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 1, 3))


# --------------------------------------------------------------------------- semantics
def test_as_of_lookup_has_no_lookahead_and_forward_fills(cache):
    rows = _weekdays(date(2023, 12, 1), date(2024, 7, 31),
                     lambda d: f"{d.toordinal() % 97 / 10:.2f}")
    rows = [r for r in rows if r[0] != "2024-03-05"]      # a holiday: no observation
    _write(cache, rows)
    rate = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))
    by_day = {date.fromisoformat(d): float(v) / 100 for d, v in rows}

    # Weekday: that day's own print. Weekend / holiday: the LAST print before it.
    assert rate.rate_on(date(2024, 3, 4)) == by_day[date(2024, 3, 4)]
    assert rate.rate_on(date(2024, 3, 5)) == by_day[date(2024, 3, 4)]
    assert rate.rate_on(date(2024, 3, 9)) == by_day[date(2024, 3, 8)]
    assert rate.rate_on(datetime(2024, 3, 10, 15, 30)) == by_day[date(2024, 3, 8)]

    # Never a later print: equal to the platform's point-in-time reader on every day.
    d = date(2024, 1, 2)
    while d <= date(2024, 6, 28):
        expected = fred_series.get_series_as_of("DGS3MO", datetime(d.year, d.month, d.day))
        assert rate.rate_on(d) == pytest.approx(expected.iloc[-1] / 100.0), d
        latest = max(k for k in by_day if k <= d)
        assert rate.rate_on(d) == by_day[latest]
        d += timedelta(days=1)


def test_a_literal_dot_row_is_no_observation_and_is_forward_filled(cache):
    """FRED writes "." for a day with no print (a holiday the series still lists). It is not
    a zero and not a parse error: the day takes the previous print."""
    rows = _weekdays(date(2023, 12, 1), date(2024, 6, 28), lambda d: "5.25")
    rows = [(d, "." if d == "2024-03-05" else ("5.30" if d == "2024-03-04" else v))
            for d, v in rows]
    _write(cache, rows)
    rate = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28))
    assert rate.rate_on(date(2024, 3, 5)) == pytest.approx(0.0530)
    assert rate.rate_on(date(2024, 3, 6)) == pytest.approx(0.0525)


def test_the_check_rate_window_tool_is_cache_only_and_exits_1_when_short(cache, capsys):
    import importlib.util
    import pathlib

    tool = pathlib.Path(__file__).resolve().parents[3] / "tools" / "refresh_fred_cache.py"
    spec = importlib.util.spec_from_file_location("_refresh_fred_cache", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def run(*argv):
        import sys
        old = sys.argv
        sys.argv = ["refresh_fred_cache.py", *argv]
        try:
            mod.main()
            return 0
        except SystemExit as e:
            return e.code
        finally:
            sys.argv = old

    _write(cache, _weekdays(date(2023, 1, 2), date(2024, 12, 31), lambda d: "5.0"))
    assert run("--check-rate-window", "2024-01-02", "2024-12-31", "--lead-days", "30") in (0, None)
    assert "DGS3MO covers 2023-12-03..2024-12-31" in capsys.readouterr().out
    # The warmup lead reaches before the file's first print -> refused, exit status non-zero.
    code = run("--check-rate-window", "2024-01-02", "2024-12-31", "--lead-days", "730")
    assert code not in (0, None) and "NOT available" in str(code)


def test_data_after_the_window_changes_neither_rates_nor_identity(cache, tmp_path):
    base = _weekdays(date(2023, 12, 1), date(2024, 6, 28), lambda d: "5.25")
    a = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28),
                             path=_write(tmp_path / "a.json", base))
    b = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28),
                             path=_write(tmp_path / "b.json",
                                         base + [("2024-07-01", "9.99")]))
    assert a.identity == b.identity
    assert b.rate_on(date(2024, 7, 3)) == pytest.approx(0.0525)    # 9.99 is never seen


def test_the_identity_moves_with_the_data(tmp_path):
    base = _weekdays(date(2023, 12, 1), date(2024, 6, 28), lambda d: "5.25")
    other = [(d, "5.26" if d == "2024-02-01" else v) for d, v in base]
    a = rfr.fred_dgs3mo_rate("2024-01-02", "2024-06-28", path=_write(tmp_path / "a.json", base))
    b = rfr.fred_dgs3mo_rate("2024-01-02", "2024-06-28", path=_write(tmp_path / "b.json", other))
    assert a.identity != b.identity


def test_the_record_says_where_the_rate_came_from(cache):
    _write(cache, _weekdays(date(2023, 12, 1), date(2024, 6, 28), lambda d: "5.25"))
    rec = rfr.fred_dgs3mo_rate(date(2024, 1, 2), date(2024, 6, 28)).describe()
    assert rec["source"] == "fred-dgs3mo" and rec["series"] == "DGS3MO"
    assert rec["window"] == ["2024-01-02", "2024-06-28"]
    assert rec["min"] == rec["max"] == pytest.approx(0.0525)


def test_an_explicit_rate_is_a_recorded_constant():
    rate = rfr.explicit_rate("0.03", origin="env:BACKTEST_OPTIONS_RISK_FREE_RATE")
    assert rate.is_explicit and rate.rate_on(date(2020, 1, 1)) == 0.03
    assert rate.describe() == {"source": "explicit", "identity": "explicit:0.03", "rate": 0.03,
                               "origin": "env:BACKTEST_OPTIONS_RISK_FREE_RATE"}
    with pytest.raises(ValueError):
        rfr.explicit_rate("4.5", origin="x")        # percent, not a decimal
    with pytest.raises(ValueError):
        rfr.as_risk_free_rate(None, origin="x")    # there is no default rate
