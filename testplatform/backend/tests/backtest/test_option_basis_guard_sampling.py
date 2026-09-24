"""The E4 basis guard is SAMPLED (perf gate 2026-09-23): first evaluable session, each split
ex-date + the session after, then at most once every SAMPLE_EVERY sessions per symbol; the
median runs over the last up-to-WINDOW CHECKED sessions."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest


def _sessions(n, start=date(2024, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _guard(monkeypatch, par_by_day, spot=100.0, split_dates=None):
    import app.services.backtest.option_basis_guard as g

    days = sorted(par_by_day)

    class _Raw:
        underlying, n_rows, c_occ = "X", len(days), []
        date_of_ord = {}
        bar_ord = np.array([d.toordinal() for d in days])
        c_expiry_ord = np.array([], dtype=int)

    u = type("U", (), {"raw": _Raw()})()
    table = {d.toordinal(): p for d, p in par_by_day.items()}
    asked = []

    def fake(u_, o):
        asked.append(o)
        return table.get(o)

    monkeypatch.setattr(g, "parity_spot", fake)
    g.clear_basis_guard_cache()
    return g, u, g.BasisGuard(lambda s, d: spot, split_dates=split_dates), asked


def test_the_sampling_constants():
    import app.services.backtest.option_basis_guard as g
    assert g.SAMPLE_EVERY == 5 and g.WINDOW == 5 and g.TOLERANCE == 0.05


def test_a_clean_symbol_is_checked_on_its_first_session_then_every_fifth(monkeypatch):
    days = _sessions(20)
    g, u, guard, _ = _guard(monkeypatch, {d: 100.0 for d in days})
    checked = []
    for d in days:
        before = guard.checks
        guard.check(u, "X", d)
        if guard.checks > before:
            checked.append(days.index(d))
    assert checked == [0, 5, 10, 15]
    assert guard.stats()["sampled_out"] == 16


def test_until_the_first_evaluable_session_every_session_is_checked(monkeypatch):
    days = _sessions(12)
    par = {d: 100.0 for d in days}
    for d in days[:3]:
        par[d] = None                       # unevaluable
    g, u, guard, _ = _guard(monkeypatch, par)
    checked = []
    for i, d in enumerate(days):
        before = guard.checks
        guard.check(u, "X", d)
        if guard.checks > before:
            checked.append(i)
    assert checked == [0, 1, 2, 3, 8]


def test_an_unevaluable_sampled_session_is_retried_on_the_next(monkeypatch):
    days = _sessions(12)
    par = {d: 100.0 for d in days}
    par[days[5]] = None
    g, u, guard, _ = _guard(monkeypatch, par)
    checked = []
    for i, d in enumerate(days):
        before = guard.checks
        guard.check(u, "X", d)
        if guard.checks > before:
            checked.append(i)
    assert checked == [0, 5, 6, 11]


def test_a_split_ex_date_forces_that_session_and_the_next(monkeypatch):
    days = _sessions(15)
    ex = days[7] - timedelta(days=0)
    g, u, guard, _ = _guard(monkeypatch, {d: 100.0 for d in days}, split_dates=lambda s: (ex,))
    checked = []
    for i, d in enumerate(days):
        before = guard.checks
        guard.check(u, "X", d)
        if guard.checks > before:
            checked.append(i)
    assert checked == [0, 5, 7, 8, 13]


def test_an_ex_date_on_a_non_session_forces_the_first_session_after_it(monkeypatch):
    days = _sessions(15)                    # 2024-01-02 ..; 2024-01-13 is a Saturday
    sat = date(2024, 1, 13)
    g, u, guard, _ = _guard(monkeypatch, {d: 100.0 for d in days}, split_dates=lambda s: (sat,))
    checked = []
    for d in days:
        before = guard.checks
        guard.check(u, "X", d)
        if guard.checks > before:
            checked.append(d)
    assert date(2024, 1, 15) in checked and date(2024, 1, 16) in checked


def test_a_persistent_error_from_mid_run_is_refused_by_the_third_sampled_check(monkeypatch):
    days = _sessions(30)
    par = {d: (100.0 if i < 10 else 106.0) for i, d in enumerate(days)}
    g, u, guard, _ = _guard(monkeypatch, par)
    refused = None
    for i, d in enumerate(days):
        try:
            guard.check(u, "X", d)
        except g.OptionSpotBasisMismatch as e:
            refused = (i, str(e))
            break
    # checks at 0, 5, 10 (bad, passes: 3 good), 15 (bad, 2 good), 20 (bad: median bad)
    assert refused is not None and refused[0] == 20
    assert refused[1].count("2024-") >= 5   # the five CHECKED sessions are named


def test_an_isolated_bad_print_on_a_sampled_session_passes(monkeypatch):
    days = _sessions(30)
    par = {d: 100.0 for d in days}
    par[days[15]] = 120.0
    g, u, guard, _ = _guard(monkeypatch, par)
    for d in days:
        guard.check(u, "X", d)
    assert guard.outliers_passed == 1


def test_the_median_uses_the_checked_sessions_not_the_skipped_ones(monkeypatch):
    """Sessions between two checks are never read once the window is full of checked ones."""
    days = _sessions(30)
    par = {d: 100.0 for d in days}
    par[days[25]] = 120.0
    g, u, guard, asked = _guard(monkeypatch, par)
    for d in days:
        guard.check(u, "X", d)
    skipped = {days[i].toordinal() for i in range(30) if i % 5}
    assert not (skipped & set(asked))


def test_the_run_wires_the_split_dates_of_its_basis():
    from ba2_common.core.split_basis import CalendarSplit, SymbolSplitBasis
    from app.services.backtest.option_split_basis import RunSplitBasis
    from app.services.backtest.options_store import _split_dates_of

    b = RunSplitBasis({"NFLX": SymbolSplitBasis("NFLX", (CalendarSplit(date(2025, 11, 17), 10.0),),
                                                date(2026, 1, 30))})
    f = _split_dates_of(b)
    assert f("NFLX") == (date(2025, 11, 17),) and f("AAPL") == ()
