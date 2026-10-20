"""``tools/measure_basis_plateaus.py`` -- the PLATEAU CONVENTION every stage-2 add ratio was
measured under (review round 2: commit the measurement so it reproduces). The pure part is
pinned here; the store-reading part is the option basis guard's own ``parity_spot``.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "measure_basis_plateaus.py"


def _tool():
    spec = importlib.util.spec_from_file_location("measure_basis_plateaus", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["measure_basis_plateaus"] = m
    spec.loader.exec_module(m)
    return m


def _days(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_a_lone_event_is_the_ratio_of_the_adjacent_medians():
    m = _tool()
    days = _days(date(2024, 1, 2), 200)
    cut = days[120]
    series = {d: (1.10 if d < cut else 1.0) for d in days}
    [r] = m.plateau_ratios(series, [cut])
    assert r["ratio"] == pytest.approx(1.10) and r["left_n"] == 60 and r["right_n"] == 60


def test_a_stretch_is_bracketed_and_windows_stop_at_the_next_cut():
    m = _tool()
    days = _days(date(2024, 1, 2), 200)
    a, b = days[100], days[120]
    series = {d: (1.3785 if a <= d < b else 1.0) for d in days}
    r1, r2 = m.plateau_ratios(series, [a, b])
    assert r1["right_n"] == 20 and r2["left_n"] == 20
    assert r1["ratio"] == pytest.approx(1 / 1.3785, abs=1e-4) and r2["ratio"] == pytest.approx(1.3785)


def test_small_ratios_keep_six_decimals():
    m = _tool()
    days = _days(date(2024, 1, 2), 100)
    series = {d: (0.0333 if i < 50 else 0.9949) for i, d in enumerate(days)}
    assert m.plateau_ratios(series, [days[50]])[0]["ratio"] == 0.033471
