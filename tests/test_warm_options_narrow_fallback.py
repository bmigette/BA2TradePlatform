"""A symbol whose WIDE request always dies must still be fetched, in slices.

MEASURED 2026-09-10/11. Sixteen underlyings -- COST, CVX, CRWD, DAL, CVS, VRT, VIRT and
friends -- gave up with their ENTIRE ladder unfetched (336, 342, 290 partitions), on the
first request, four attempts running:

    [VRT] attempt 4 failed: _MultiThreadedRendezvous: RPC terminated
    [VRT] GIVING UP after 4 attempt(s) - 342 expiry partition(s) left unfetched

Every one is a big, liquid name, and every failure is on the FIRST request before anything
is written. The `--wide` shape asks for every expiration 2020-2026 in one call
(``expiration="*"``), and for the largest chains that stream does not survive. The earlier
fix (treating the vendor's INTERNAL as transient rather than permanent, b0629626) was
correct and insufficient: retrying an over-large request just fails four times instead of
once. Making the request SMALLER is the only thing that changes the outcome.
"""
import importlib.util
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta

import pytest


_MODNAME = "warm_options_history_under_test"


def _mod():
    """Load the script as a module -- it is a tool, not a package member.

    ABSOLUTE path from ``__file__``: a relative one resolves against the CWD, and another
    suite's conftest moves that to ``testplatform/backend``, so this file passed alone and
    failed the moment it ran beside them.

    The module is registered in ``sys.modules`` BEFORE execution because ``@dataclass``
    resolves a class's module by name while the class body runs -- but a failed exec must
    not leave that half-built shell cached, or every later test reports a missing attribute
    instead of the real error.
    """
    if _MODNAME in sys.modules:
        return sys.modules[_MODNAME]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "tools", "warm_options_history.py")
    spec = importlib.util.spec_from_file_location(_MODNAME, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = m
    try:
        spec.loader.exec_module(m)
    except Exception:
        sys.modules.pop(_MODNAME, None)
        raise
    return m


class TestWindowSlices:
    """The splitter the fallback walks. Its contract is what keeps the flush sound."""

    def test_the_slices_cover_the_span_exactly(self):
        m = _mod()
        s = m.window_slices(date(2020, 1, 1), date(2026, 9, 10), 1.0)
        assert s[0][0] == date(2020, 1, 1)
        assert s[-1][1] == date(2026, 9, 10)

    def test_they_are_contiguous_and_oldest_first(self):
        """LOAD-BEARING, not cosmetic. The caller closes an expiry the moment a bar dated
        after it arrives, which is only sound while bar dates never go backwards. Walking
        the slices in order extends that guarantee across several requests."""
        m = _mod()
        s = m.window_slices(date(2020, 1, 1), date(2026, 9, 10), 0.5)
        assert len(s) > 1
        for a, b in zip(s, s[1:]):
            assert (b[0] - a[1]).days == 1, f"gap or overlap between {a} and {b}"
            assert a[0] < b[0]

    def test_zero_years_disables_the_split(self):
        """So the fallback is a no-op rather than a special case at the call site."""
        m = _mod()
        assert m.window_slices(date(2020, 1, 1), date(2026, 9, 10), 0) == \
               [(date(2020, 1, 1), date(2026, 9, 10))]

    def test_a_span_inside_one_slice_is_one_window(self):
        m = _mod()
        assert len(m.window_slices(date(2026, 1, 1), date(2026, 3, 1), 1.0)) == 1


@dataclass(frozen=True)
class _Bar:
    occ_symbol: str
    bar_date: date


class _Provider:
    """A provider whose WIDE call always dies and whose narrow calls work.

    That is the live shape exactly: the failure is a function of how much is asked for in
    one request, not of the symbol being unavailable.
    """

    def __init__(self, wide_span_days, bars):
        self.wide_span_days = wide_span_days
        self._bars = bars
        self.calls = []

    def fetch_underlying_eod_bars(self, symbol, *, start, end):
        self.calls.append((start, end))
        if (end - start).days >= self.wide_span_days:
            raise RuntimeError(
                "_MultiThreadedRendezvous: <_MultiThreadedRendezvous of RPC that "
                "terminated with:\n\tstatus = StatusCode.INTERNAL")
        for b in self._bars:
            if start <= b.bar_date <= end:
                yield b


class _Store:
    """Records what was written; nothing touches disk."""

    def __init__(self):
        self.written = {}

    def write_partition(self, underlying, expiry, bars, start, end, *, empty_contracts=()):
        self.written[(underlying, expiry)] = len(bars)
        return {"rows": len(bars), "status": "empty" if not bars else "written"}


def _run(monkeypatch, provider, *, expiries, narrow_years, retries=2):
    """Drive run_symbol_units for ONE symbol and return (stats, store)."""
    m = _mod()
    import argparse

    contracts = [type("C", (), {"expiry": e, "occ_symbol": f"X{i}"})()
                 for i, e in enumerate(expiries)]
    unit = m.SymbolUnit(underlying="COST", pending_expiries=list(expiries),
                        contracts=contracts)
    # The real parser resolves an OCC symbol to its expiry; the fake bars carry it directly.
    monkeypatch.setattr(m, "parse_occ_expiry",
                        lambda occ: {f"X{i}": e for i, e in enumerate(expiries)}.get(occ))
    # Every knob run_symbol_units reads; a missing one fails as an AttributeError deep in
    # the loop rather than at the call, so they are listed rather than minimised.
    ns = argparse.Namespace(max_retries=retries, backoff=0.0, rate_limit=0.0,
                            progress_every=0, narrow_fallback_years=narrow_years)
    store = _Store()
    logs = []
    stats = m.run_symbol_units([unit], provider, store,
                               date(2020, 1, 1), date(2026, 9, 10), ns,
                               clock=lambda: __import__("datetime").datetime(2026, 9, 11),
                               sleep=lambda _s: None, log=logs.append)
    return stats, store, logs


def test_a_symbol_the_wide_request_cannot_deliver_is_salvaged_in_slices(monkeypatch):
    """THE DEFECT. Before this, the symbol was abandoned with its whole ladder unfetched."""
    expiries = [date(2020, 6, 19), date(2022, 6, 17), date(2025, 6, 20)]
    bars = [_Bar(f"X{i}", e - timedelta(days=30)) for i, e in enumerate(expiries)]
    prov = _Provider(wide_span_days=365 * 3, bars=bars)

    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert stats.units_failed == 0, "the symbol must not be abandoned any more"
    assert len(store.written) == len(expiries), store.written
    assert any("retrying in" in l and "narrower window" in l for l in logs), logs
    assert any("recovered via narrow windows" in l for l in logs), logs


def test_the_wide_request_is_still_tried_first(monkeypatch):
    """The fallback is a fallback. The wide shape is ~70x faster per symbol and must stay
    the default path -- narrowing only where the vendor cannot deliver."""
    expiries = [date(2020, 6, 19)]
    prov = _Provider(wide_span_days=365 * 3,
                     bars=[_Bar("X0", date(2020, 5, 20))])
    _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0, retries=2)

    span = lambda c: (c[1] - c[0]).days
    wide = [c for c in prov.calls if span(c) > 365 * 3]
    narrow = [c for c in prov.calls if span(c) <= 366]
    assert span(prov.calls[0]) > 365 * 3, "the first call must be the WIDE one"
    # The wide shape is RETRIED to exhaustion before narrowing -- a transient vendor blip
    # must not cost the fast path.
    assert len(wide) == 2, f"expected max_retries wide attempts, got {len(wide)}"
    assert narrow and prov.calls.index(narrow[0]) == len(wide),         "narrowing must begin only after the wide retries are spent"


def test_disabling_the_fallback_restores_the_old_behaviour(monkeypatch):
    """0 years means the symbol is abandoned exactly as before -- an escape hatch, and the
    proof that the fallback is what changes the outcome rather than something else."""
    expiries = [date(2020, 6, 19), date(2022, 6, 17)]
    prov = _Provider(wide_span_days=365 * 3,
                     bars=[_Bar(f"X{i}", e - timedelta(days=30))
                           for i, e in enumerate(expiries)])

    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=0)

    assert stats.units_failed == len(expiries)
    assert store.written == {}
    assert any("GIVING UP" in l for l in logs)


def test_a_failed_window_leaves_the_symbol_owed_rather_than_truncated(monkeypatch):
    """The safe answer, and NOT the one I first wrote.

    Partial salvage looks attractive -- keep the windows that worked -- and is wrong here. A
    contract may be listed years before it expires (a LEAPS expiring 2025 trades from 2023),
    so an expiry in a LATER window can own bars in an EARLIER one. Writing its partition
    after that earlier window failed would produce a file whose manifest says COMPLETE while
    silently missing those bars, and nothing ever looks at it again. Owing the symbol one
    more run is far cheaper than a truncated partition nobody can detect.
    """
    expiries = [date(2020, 6, 19), date(2025, 6, 20)]
    bars = [_Bar("X0", date(2020, 5, 20)), _Bar("X1", date(2025, 5, 21))]

    class _Patchy(_Provider):
        def fetch_underlying_eod_bars(self, symbol, *, start, end):
            self.calls.append((start, end))
            if (end - start).days >= 365 * 3:
                raise RuntimeError("StatusCode.INTERNAL")      # the wide call
            if start.year == 2020:
                raise RuntimeError("StatusCode.INTERNAL")      # one bad window
            for b in self._bars:
                if start <= b.bar_date <= end:
                    yield b

    stats, store, logs = _run(monkeypatch, _Patchy(365 * 3, bars),
                              expiries=expiries, narrow_years=1.0)

    assert store.written == {},         "a failed window must not leave a partition claiming to be complete"
    assert stats.units_failed == len(expiries), "the whole symbol stays owed"
    assert any("GIVING UP" in l for l in logs)


def test_a_failed_window_stops_the_walk_instead_of_writing_past_it(monkeypatch):
    """MEASURED LIVE 2026-09-11, and the first version of this fix got it wrong.

    CNC's 2020 window died; the walk carried on and the 2021+ windows flushed 355 partitions.
    A contract expiring 2021-05-28 trades from 2020, so its partition was written without its
    2020 bars, under a manifest saying COMPLETE -- undetectable, and never revisited.

    The rule the code now keeps: stop at the FIRST failed window. What closed before it is
    provably whole (an expiry is flushed only once a later-dated bar arrives, so it closed
    inside an earlier window, and windows are walked oldest-first); what comes after is not
    fetched at all rather than fetched incompletely.
    """
    # 2020 expiry closes inside window 1; 2021 fails; the 2023 LEAPS would be flushed by
    # window 4 while owning bars in the window that died.
    expiries = [date(2020, 6, 19), date(2023, 6, 16)]

    class _BadSecondWindow(_Provider):
        def fetch_underlying_eod_bars(self, symbol, *, start, end):
            self.calls.append((start, end))
            if (end - start).days >= 365 * 3:
                raise RuntimeError("StatusCode.INTERNAL")          # the wide call
            if start.year == 2021:
                raise RuntimeError("StatusCode.INTERNAL")          # the window that dies
            for b in self._bars:
                if start <= b.bar_date <= end:
                    yield b

    bars = [_Bar("X0", date(2020, 5, 20)),       # the 2020 expiry's own bars
            _Bar("X1", date(2020, 6, 30)),       # a later bar in window 1: closes the 2020 expiry
            _Bar("X1", date(2021, 3, 1)),        # the LEAPS' bars in the window that dies
            _Bar("X1", date(2023, 5, 15))]
    prov = _BadSecondWindow(365 * 3, bars)

    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert (date(2023, 6, 16)) not in store.written,         "a partition whose history spans the failed window must NOT be written"
    assert (date(2020, 6, 19)) in [e for _u, e in store.written],         "what closed before the failure is whole and must be kept"
    # And the proof it is a stop, not a filter: nothing after the failed window is requested.
    assert not [c for c in prov.calls if c[0].year > 2021],         f"the walk must stop at the failed window, got {prov.calls}"


class _SilentProvider:
    """A provider whose stream closes cleanly having yielded nothing.

    MEASURED 2026-09-11 against ThetaData: twelve underlyings -- COP, COST, COTY, CPB, CRWD,
    CSX, CTSH, CVE, CVNA, CVS, CVX, DAL -- each answered in about a second with an empty
    stream and no error, on the same afternoon the vendor was failing their neighbours with
    an INTERNAL ArrayIndexOutOfBounds. It is a vendor fault wearing the shape of an answer.
    """

    def __init__(self):
        self.calls = []

    def fetch_underlying_eod_bars(self, symbol, *, start, end):
        self.calls.append((start, end))
        return iter(())


def test_an_empty_stream_is_a_failure_not_an_empty_market(monkeypatch):
    """THE POISONING. Before this, a silent vendor wrote every partition as EMPTY.

    4,763 manifests were written in one run asserting that ConocoPhillips and Costco had no
    listed options for six years. A partition marked empty is never re-read, so the vendor's
    silence became a permanent fact about the market -- the worst failure shape there is,
    because the store afterwards looks complete.
    """
    expiries = [date(2020, 6, 19), date(2022, 6, 17), date(2025, 6, 20)]
    prov = _SilentProvider()

    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert store.written == {},         f"silence must write NOTHING, got {store.written}"
    assert stats.units_failed == len(expiries), "the symbol stays owed"
    assert any("EMPTY stream" in l for l in logs), logs


def test_the_empty_stream_is_retried_like_any_other_failure(monkeypatch):
    """It is a failed attempt, so it gets the same retries -- a one-off blip costs nothing."""
    prov = _SilentProvider()
    _run(monkeypatch, prov, expiries=[date(2020, 6, 19)], narrow_years=0, retries=3)
    assert len(prov.calls) == 3, f"expected 3 wide attempts, got {prov.calls}"


def test_a_symbol_with_nothing_left_to_fetch_is_not_called_a_failure(monkeypatch):
    """The guard keys on PENDING work, not on the bar count: a symbol whose partitions were
    all flushed mid-stream ends with an empty tail and must still count as done."""
    expiries = [date(2020, 6, 19), date(2021, 6, 18)]
    # A later-dated bar closes both expiries mid-stream, so `pending` empties before the end.
    bars = [_Bar("X0", date(2020, 5, 20)), _Bar("X1", date(2021, 5, 20)),
            _Bar("X1", date(2021, 7, 1))]
    prov = _Provider(wide_span_days=10 ** 6, bars=bars)   # the wide call succeeds

    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert stats.units_failed == 0, logs
    assert len(store.written) == len(expiries), store.written


def test_a_trickle_in_one_window_does_not_certify_the_silent_ones(monkeypatch):
    """MEASURED LIVE 2026-09-11, and the reason the symbol-level guard was not enough.

    CNC's walk delivered bars in ONE window and nothing in the other six. The symbol-level
    check ("did we see any bar at all?") passed, the walk reported "recovered via narrow
    windows", and the tail flush wrote 333 partitions as EMPTY -- the identical false record,
    reached by a narrower path. Centene has listed options in every one of those expiries.
    """
    expiries = [date(2020, 6, 19), date(2021, 6, 18), date(2026, 6, 19)]

    class _OnlyLastWindowSpeaks(_Provider):
        def fetch_underlying_eod_bars(self, symbol, *, start, end):
            self.calls.append((start, end))
            if (end - start).days >= 365 * 3:
                raise RuntimeError("StatusCode.INTERNAL")     # the wide call
            if end.year < 2026:
                return                                        # silent, and no error
            for b in self._bars:
                if start <= b.bar_date <= end:
                    yield b

    prov = _OnlyLastWindowSpeaks(365 * 3, [_Bar("X2", date(2026, 3, 2))])
    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert store.written == {},         f"a silent window must not certify its expiries as empty, got {store.written}"
    assert not any("recovered via narrow" in l for l in logs), logs
    assert any("EMPTY stream" in l for l in logs), logs
    # It stops at the FIRST silent window that owed expiries, so the later ones are not asked.
    assert not [c for c in prov.calls if c[0].year >= 2022],         f"the walk must stop at the first silent window, got {prov.calls}"


def test_a_window_with_no_expiry_due_may_be_legitimately_empty(monkeypatch):
    """The other half of the rule, or a symbol first listed in 2023 could never be fetched.

    Nothing expires in 2020-2022 here, so those windows returning nothing is the market, not
    the vendor -- and the walk must carry on to the windows that do own expiries.
    """
    expiries = [date(2025, 6, 20)]

    class _QuietEarlyYears(_Provider):
        def fetch_underlying_eod_bars(self, symbol, *, start, end):
            self.calls.append((start, end))
            if (end - start).days >= 365 * 3:
                raise RuntimeError("StatusCode.INTERNAL")
            for b in self._bars:
                if start <= b.bar_date <= end:
                    yield b

    prov = _QuietEarlyYears(365 * 3, [_Bar("X0", date(2025, 5, 21))])
    stats, store, logs = _run(monkeypatch, prov, expiries=expiries, narrow_years=1.0)

    assert stats.units_failed == 0, logs
    assert len(store.written) == 1, store.written
    assert any("recovered via narrow windows" in l for l in logs), logs
