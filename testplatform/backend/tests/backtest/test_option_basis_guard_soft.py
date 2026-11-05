"""The E4 guard's SOFT vs HARD refusal (owner decision 2026-11-05).

A would-be refusal whose window median lies in [SOFT_BAND_LOW, 1 - TOLERANCE), on a symbol WITH
a dividend calendar, where the carry separation failed only for want of proof (veto /
unmeasurable), does not abort the GA job: NEW ENTRIES on that underlying are withheld (check()
returns False -> the provider's get_chain is empty) and the skip is recorded per symbol in the
trial's ``option_basis_guard`` stats. Everything else is still ``OptionSpotBasisMismatch``.
"""
from __future__ import annotations

import logging
from datetime import date

import pytest

from tests.backtest.test_option_basis_guard_carry import (
    STLA_APRIL, STLA_DIVS, _guard, _monthly)


@pytest.fixture(autouse=True)
def _logging_on():
    """Other tests (the preflight tool's in-process scan) leave ``logging.disable`` set."""
    prev = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    yield
    logging.disable(prev)


def _flat(ratio_terms, n=40, start=date(2024, 1, 2)):
    """n weekdays, spot 100, every session the same (dte, parity/spot) terms."""
    return {d: (100.0, list(ratio_terms)) for d in _monthly(start, n=n)}


def _run_all(g, u, guard, days):
    return [guard.check(u, "X", d) for d in days]


def test_stla_like_unverifiable_dividend_is_soft_not_fatal(monkeypatch, caplog):
    g, u, guard = _guard(monkeypatch, STLA_APRIL, dividends=lambda s, d: STLA_DIVS)
    days = sorted(STLA_APRIL)
    with caplog.at_level(logging.WARNING, logger=g.logger.name):
        allowed = _run_all(g, u, guard, days)           # must not raise
    assert False in allowed                              # entries were withheld
    soft = guard.stats()["soft_skips"]["X"]
    assert soft["sessions_skipped"] == allowed.count(False) >= 1
    assert soft["first_skipped"] <= soft["last_skipped"]
    assert soft["sessions_seen"] == len(days) and 0 < soft["skipped_share"] <= 1
    assert "soft band" in soft["reason"] and soft["evaluations"] >= 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NEW ENTRIES on X skipped on" in m for m in msgs)       # per (symbol, session)
    assert any("NEW ENTRIES were skipped on 1 symbol" in m for m in msgs)   # end-of-run summary


def test_the_warning_is_once_per_symbol_session(monkeypatch, caplog):
    table = _flat([(10, 0.93), (40, 0.93)])
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    d = sorted(table)[0]
    with caplog.at_level(logging.WARNING, logger=g.logger.name):
        guard.check(u, "X", d)
        guard.check(u, "X", d)
    assert sum("skipped on" in r.getMessage() for r in caplog.records) == 1
    assert guard.stats()["soft_skips"]["X"]["sessions_skipped"] == 1   # a session counts once


@pytest.mark.parametrize("ratio", [1.08, 1.5, 2.0, 0.5, 0.25, 0.80, 0.849])
def test_outside_the_soft_band_is_still_fatal(monkeypatch, ratio):
    table = _flat([(10, ratio), (40, ratio)])
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch):
        _run_all(g, u, guard, sorted(table))


def test_soft_band_edges():
    import app.services.backtest.option_basis_guard as g
    assert g.SOFT_BAND_LOW == 0.85 and g.SOFT_BAND_LOW < 1.0 - g.TOLERANCE


def test_no_dividend_calendar_is_fatal_even_inside_the_band(monkeypatch):
    table = _flat([(10, 0.93), (40, 0.93)])
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: None)
    with pytest.raises(g.OptionSpotBasisMismatch, match="no dividend calendar"):
        _run_all(g, u, guard, sorted(table))


def test_a_measured_carry_free_multiple_in_the_band_stays_fatal(monkeypatch):
    """Three collinear expiries at 0.93: the carry-free spot is MEASURED and still 0.93 -- the
    multiple survives the carry removal (a missed small spin-off), so this is HARD."""
    table = _flat([(10, 0.93), (17, 0.93), (24, 0.93)])
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="outside"):
        _run_all(g, u, guard, sorted(table))


def test_unmeasurable_carry_in_the_band_is_soft(monkeypatch):
    table = _flat([(10, 0.93), (40, 0.93)])           # non-payer, no third expiry to confirm
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    assert _run_all(g, u, guard, sorted(table))[0] is False
    assert guard.stats()["soft_skips"]["X"]["sessions_skipped"] >= 1


def test_entries_resume_when_the_window_clears(monkeypatch):
    days = _monthly(date(2024, 1, 2), n=60)
    table = {d: (100.0, [(10, 0.93), (40, 0.93)] if i < 15 else [(10, 1.0), (40, 1.0)])
             for i, d in enumerate(days)}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    allowed = _run_all(g, u, guard, days)
    assert allowed[0] is False
    assert allowed[-1] is True                         # cleared by later sampled checks
    first_ok = allowed.index(True)
    assert all(allowed[first_ok:])                     # and stays open
    rec = guard.stats()["soft_skips"]["X"]
    assert rec["sessions_skipped"] == allowed.count(False)
    assert rec["last_skipped"] < days[-1].isoformat()


def test_a_soft_episode_longer_than_soft_max_days_becomes_fatal(monkeypatch):
    """A carry is transient, a basis error persists (SMMT 2022-12..2023-06, 121 sessions): an
    episode past SOFT_MAX_DAYS is the job-fatal refusal, naming the episode."""
    days = _monthly(date(2024, 1, 2), n=80)            # ~112 calendar days of 0.93
    table = {d: (100.0, [(10, 0.93), (40, 0.93)]) for d in days}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    allowed = []
    with pytest.raises(g.OptionSpotBasisMismatch, match="SOFT_MAX_DAYS"):
        for d in days:
            allowed.append(guard.check(u, "X", d))
    assert allowed and allowed[0] is False             # soft first, fatal only once it persisted
    assert (days[len(allowed)] - days[0]).days > g.SOFT_MAX_DAYS


def test_a_cleared_episode_restarts_the_soft_clock(monkeypatch):
    """Two dividend-shaped episodes separated by a clean stretch never add up to SOFT_MAX_DAYS."""
    days = _monthly(date(2024, 1, 2), n=90)
    table = {d: (100.0, [(10, 0.93), (40, 0.93)] if (i < 30 or i >= 60) else [(10, 1.0), (40, 1.0)])
             for i, d in enumerate(days)}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    allowed = _run_all(g, u, guard, days)              # must not raise
    assert allowed[0] is False and allowed[45] is True and allowed[-1] is False
    import app.services.backtest.option_basis_guard as gm
    assert (days[29] - days[0]).days <= gm.SOFT_MAX_DAYS and (days[-1] - days[60]).days <= gm.SOFT_MAX_DAYS


def test_a_clean_symbol_has_an_empty_soft_record(monkeypatch):
    table = _flat([(10, 1.0), (40, 1.0)])
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    assert all(_run_all(g, u, guard, sorted(table)))
    assert guard.stats()["soft_skips"] == {}


def test_marks_and_exits_never_consult_the_guard_and_entries_do():
    """Open positions are marked/closed from get_quote / get_bar (no guard); the chain read that
    picks contracts for an entry is the only guarded one, and a soft skip empties it."""
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider as P

    class _Guard:
        def __init__(self, ok):
            self.ok, self.calls = ok, 0

        def check(self, u, underlying, as_of):
            self.calls += 1
            return self.ok

    class _U:
        n_rows = 5

    p = object.__new__(P)
    p._u = lambda underlying: _U()
    p._basis_guard = _Guard(False)
    d = date(2024, 5, 1)
    assert p.get_chain("X", d, expiry_min=d, expiry_max=d, data_session=d) == []
    assert p._basis_guard.calls == 1
    import inspect
    for fn in (P.get_quote, P.get_bar, P.get_atm_iv):
        assert "_basis_guard" not in inspect.getsource(fn)


def test_results_carry_soft_skips_in_the_guard_stats(monkeypatch):
    g, u, guard = _guard(monkeypatch, STLA_APRIL, dividends=lambda s, d: STLA_DIVS)
    _run_all(g, u, guard, sorted(STLA_APRIL))
    st = guard.stats()
    assert set(st["soft_skips"]["X"]) >= {"sessions_skipped", "sessions_seen", "skipped_share",
                                           "first_skipped", "last_skipped", "reason",
                                           "evaluations"}
