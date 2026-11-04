"""The host-shared ATM-IV table (2026-11-04): identical answers, built once, never waited on.

``get_iv_rank`` asks ``get_atm_iv`` for ~252 trailing days per symbol per bar, and on the
748-name stage-2 pilot that scan was ~75% of every trial. The table caches the scan's answer
per (store, spot, rate) on the host. These tests pin that it can only ever change WHERE the
answer comes from, never WHAT it is.

Run:
    ./venv/bin/python -m pytest tests/backtest/test_parquet_atm_iv_table.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import app.services.backtest.parquet_options_provider as pq
from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider,
    reset_run_overlays,
)
from tests.backtest.test_parquet_options_provider import (  # noqa: F401  (fixtures)
    _RATE,
    _SPOT,
    _UNDER,
    _spot_source,
    shared_arrays_enabled,
    store_root,
)

_DAYS = [date(2022, 12, 20) + timedelta(days=k) for k in range(70)]


def _provider(root, spot=_spot_source, rate=_RATE, scope="test"):
    return ParquetOptionsProvider(root, spot_source=spot, risk_free_rate=rate, spot_scope=scope)


def _scan(root, as_of, spot=_spot_source, rate=_RATE):
    """The scan alone, on a fresh overlay: what get_atm_iv answered before the table."""
    pq.clear_worker_parquet_options_cache()
    p = _provider(root, spot, rate)
    return pq._atm_iv_scan(p._u(_UNDER), as_of.toordinal(), spot)


def _count_builds(monkeypatch):
    calls = []
    real = pq._atm_iv_table_arrays

    def counting(u, spot_source):
        calls.append(u.underlying)
        return real(u, spot_source)

    monkeypatch.setattr(pq, "_atm_iv_table_arrays", counting)
    return calls


def test_the_table_answers_exactly_what_the_scan_answers(store_root):
    expected = {d: _scan(store_root, d) for d in _DAYS}
    assert any(v is not None for v in expected.values()), "the fixture must have an ATM day"
    pq.clear_worker_parquet_options_cache()
    p = _provider(store_root)
    got = {d: p.get_atm_iv(_UNDER, d) for d in _DAYS}
    assert got == expected
    assert isinstance(pq._WORKER_ATM_TABLES[(store_root, _UNDER, p.risk_free_rate_source.identity,
                                             "test")], pq._AtmIvTable)


def test_a_table_cell_is_served_without_scanning(monkeypatch, store_root):
    p = _provider(store_root)
    first = p.get_atm_iv(_UNDER, date(2023, 1, 10))

    def boom(*a, **k):
        raise AssertionError("a day the table holds was scanned")

    monkeypatch.setattr(pq, "_atm_iv_scan", boom)
    pq._WORKER_ATM_IV_CACHE.clear()
    assert p.get_atm_iv(_UNDER, date(2023, 1, 10)) == first


def test_days_outside_the_table_take_the_scan(store_root):
    """The table spans first..last bar date; before it and on a weekend the scan answers."""
    p = _provider(store_root)
    for d in (date(2022, 12, 30), date(2023, 1, 7), date(2023, 2, 1)):   # before / Sat / after
        assert p.get_atm_iv(_UNDER, d) == _scan(store_root, d)


def test_built_once_per_host_then_opened(monkeypatch, store_root):
    builds = _count_builds(monkeypatch)
    _provider(store_root).get_atm_iv(_UNDER, date(2023, 1, 10))
    pq.clear_worker_parquet_options_cache()          # a fresh worker process on the same host
    _provider(store_root).get_atm_iv(_UNDER, date(2023, 1, 10))
    assert builds == [_UNDER]


def test_a_different_spot_is_a_different_table(monkeypatch, store_root):
    builds = _count_builds(monkeypatch)

    def other_spot(underlying, on):
        v = _spot_source(underlying, on)
        return None if v is None else v * 1.07

    a = _provider(store_root).get_atm_iv(_UNDER, date(2023, 1, 10))
    b = _provider(store_root, spot=other_spot, scope="other").get_atm_iv(_UNDER, date(2023, 1, 10))
    assert len(builds) == 2
    assert b == _scan(store_root, date(2023, 1, 10), spot=other_spot)
    assert a != b


def test_a_held_build_lock_is_never_waited_on(monkeypatch, store_root):
    """Every worker walks the same names in the same order: a loser answers from the scan this
    trial and asks again next trial, instead of queueing behind the builder."""
    from ba2_common.core import shared_arrays as sa

    # The ATM-IV key's lock is held by another process (the raws' own build_or_open is not).
    monkeypatch.setattr(sa.DerivedArrayStore, "open_or_build_nowait",
                        lambda self, key, sources, build_fn: None)
    p = _provider(store_root)
    d = date(2023, 1, 10)
    assert p.get_atm_iv(_UNDER, d) == _scan(store_root, d)
    p = _provider(store_root)
    p.get_atm_iv(_UNDER, d)
    key = (store_root, _UNDER, p.risk_free_rate_source.identity, "test")
    assert pq._WORKER_ATM_TABLES[key] is pq._PENDING

    monkeypatch.undo()
    reset_run_overlays()
    assert key not in pq._WORKER_ATM_TABLES
    p.get_atm_iv(_UNDER, d)
    assert isinstance(pq._WORKER_ATM_TABLES[key], pq._AtmIvTable)


def test_a_day_the_rate_cannot_state_is_left_to_the_scan(store_root):
    """A FRED series starting 2023-01-11 cannot price the 01-10 row the 01-10 scan reads. The
    build leaves every day whose scan raised ABSENT, so asking for one raises exactly as
    before -- never a value -- while a day whose scan reads no row (01-09) is still served."""
    from ba2_providers.macro.risk_free_rate import RiskFreeRate, RiskFreeRateUnavailable

    rate = RiskFreeRate(source="fred-test", ords=[date(2023, 1, 11).toordinal()], values=[0.045])
    p = _provider(store_root, rate=rate)
    assert p.get_atm_iv(_UNDER, date(2023, 1, 9)) is None
    table = pq._WORKER_ATM_TABLES[(store_root, _UNDER, rate.identity, "test")]
    assert table.lookup(date(2023, 1, 9).toordinal()) is None
    assert table.lookup(date(2023, 1, 10).toordinal()) is pq._MISSING
    with pytest.raises(RiskFreeRateUnavailable):
        p.get_atm_iv(_UNDER, date(2023, 1, 10))


def test_the_escape_hatch_builds_no_table(monkeypatch, store_root):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    builds = _count_builds(monkeypatch)
    p = _provider(store_root)
    assert p.get_atm_iv(_UNDER, date(2023, 1, 10)) == _scan(store_root, date(2023, 1, 10))
    assert builds == []


def test_the_spot_digest_covers_every_bar_date(store_root):
    """Moving the close of ANY bar date moves the key (here: the first one)."""
    p = _provider(store_root)
    u = p._u(_UNDER)
    first = min(_SPOT)

    def moved(underlying, on):
        v = _spot_source(underlying, on)
        return v + 1.0 if on == first else v

    assert pq._spot_rate_digest(u, _spot_source) != pq._spot_rate_digest(u, moved)
    assert pq._spot_rate_digest(u, _spot_source) == pq._spot_rate_digest(u, _spot_source)
