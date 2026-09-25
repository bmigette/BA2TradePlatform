"""Stage-2 universe (2026-09-25): split-basis overrides measured on the 753-name universe.

Every ratio below is the put-call-parity spot of the nearest expiry (ThetaData, as traded) over
the FMP close x the layer-1 factor, per session, 2020-2025 -- the guard's own measure
(``option_basis_guard``) replayed on every session. A plateau away from 1 that ends at a dated
event is a price adjustment FMP applies but its calendar lacks (``add_price_adjustment``), or a
calendar row FMP's closes never took (``exclude_calendar_event``).

  TRP   South Bow spin-off, ex 2024-10-02: 1.1007 over the 60 evaluable sessions before
        (p10-p90 1.0875-1.1028, the low tail being ex-dividend weeks), 1.0024 after.
  MMM   Solventum spin-off, ex 2024-04-01: 1.1971 (p10-p90 1.1961-1.1979) before, 1.0009 after.
  SKM   SK Telecom ADR: the as-traded ADR moved onto another basis on 2021-11-30 (the calendar's
        0.607 row) and back onto FMP's on 2021-12-27 (the chain re-struck: strikes 20.74,
        25.74, ... first listed that day). Parity/(FMP x factor): 1.6515 before 2021-11-30 (the
        0.607 row is in the factor: 1.6515 x 0.607 = 1.0025), 1.6517 over 2021-11-30..12-23,
        0.9965 from 2021-12-29. One added event of x1.6517 on 2021-12-27 puts both plateaus
        at 1.000.
  MTCH  the 2020-07-01 row (1751/500) is IAC's separation ratio; FMP's MTCH closes before it are
        the OLD Match Group's own price: 0.2848 x 3.502 = 0.9974 before, 1.0000 after.

A fake series carries the real calendars and anchors; the local cache is read (read-only) where
present.
"""
import json
import os
from datetime import date

import numpy as np
import pytest

from ba2_common.core import split_basis_overrides as ovr
from ba2_common.core.split_basis import CalendarSplit, SplitBasisRefused, resolve_symbol_split_basis


def _in_this_checkout(module_file) -> bool:
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False


ADD, EXCL = ovr.KIND_ADD_PRICE_ADJUSTMENT, ovr.KIND_EXCLUDE_CALENDAR_EVENT
A2020 = date(2020, 1, 2)

#: symbol -> [(event_date, ratio, kind)], and the FMP close at 2020-01-02 each was measured on.
EXPECTED = {
    "TRP": [(date(2024, 10, 2), 1.1007, ADD)],
    "MMM": [(date(2024, 4, 1), 1.1971, ADD)],
    "SKM": [(date(2021, 12, 27), 1.6517, ADD)],
    "MTCH": [(date(2020, 7, 1), 3.502, EXCL)],
}
ANCHORS = {"TRP": 48.0, "MMM": 150.49, "SKM": 22.5, "MTCH": 84.19}

#: The real FMP calendars (fmp_history/mc_stock_split__<SYM>.json) and daily-file ranges.
CALENDARS = {
    "TRP": [CalendarSplit(date(1984, 2, 13), 2.0)],
    "MMM": [CalendarSplit(date(1972, 6, 16), 2.0), CalendarSplit(date(1987, 6, 16), 2.0),
            CalendarSplit(date(1994, 4, 11), 2.0), CalendarSplit(date(2003, 9, 30), 2.0)],
    "SKM": [CalendarSplit(date(1996, 12, 27), 1.03), CalendarSplit(date(1997, 12, 29), 1.03),
            CalendarSplit(date(1998, 12, 29), 1.03), CalendarSplit(date(1999, 7, 13), 1.103),
            CalendarSplit(date(1999, 12, 29), 1.03), CalendarSplit(date(2021, 11, 30), 0.607)],
    "MTCH": [CalendarSplit(date(1998, 3, 27), 2.0), CalendarSplit(date(2000, 2, 25), 2.0),
             CalendarSplit(date(2005, 8, 9), 0.9), CalendarSplit(date(2008, 8, 21), 1.146),
             CalendarSplit(date(2020, 7, 1), 1751 / 500)],
}
FILE_RANGE = {"TRP": (date(2011, 6, 22), date(2026, 6, 30)),
              "MMM": (date(2011, 6, 22), date(2026, 6, 30)),
              "SKM": (date(2011, 6, 22), date(2026, 6, 30)),
              "MTCH": (date(2018, 1, 2), date(2026, 6, 30))}

#: (symbol, session, parity spot, FMP close) from the 2026-09-25 sweep.
SWEEP = [
    ("TRP", date(2020, 6, 1), 44.925, 40.910),
    ("TRP", date(2022, 6, 1), 57.550, 52.910),      # an ex-dividend week: 1.2% low after the fix
    ("TRP", date(2024, 9, 3), 46.350, 42.120),
    ("TRP", date(2024, 11, 1), 46.350, 46.300),     # after the spin-off
    ("MMM", date(2020, 6, 1), 155.445, 130.080),
    ("MMM", date(2022, 6, 1), 146.445, 122.420),
    ("MMM", date(2024, 3, 1), 91.970, 76.800),
    ("MMM", date(2024, 6, 3), 99.870, 99.760),      # after the spin-off
    ("SKM", date(2020, 7, 1), 20.325, 20.120),
    ("SKM", date(2021, 11, 1), 29.050, 29.150),
    ("SKM", date(2021, 12, 15), 44.425, 26.900),    # between the two basis changes
    ("SKM", date(2022, 3, 1), 24.790, 24.700),
    ("MTCH", date(2020, 2, 3), 80.350, 80.720),
    ("MTCH", date(2020, 6, 1), 91.550, 91.730),
    ("MTCH", date(2020, 9, 1), 113.190, 113.410),
]


def _series(sym, anchor=None):
    first, last = FILE_RANGE[sym]
    days = np.arange(np.datetime64(first), np.datetime64(last) + 1, dtype="datetime64[D]")
    days = days[np.is_busday(days)]
    px = np.full(len(days), float(ANCHORS[sym] if anchor is None else anchor))
    return days, px


def _resolve(sym, overrides=None, anchor=None):
    days, px = _series(sym, anchor)
    return resolve_symbol_split_basis(sym, days, px, px, px, px, CALENDARS[sym], overrides=overrides)


def test_the_module_under_test_is_the_worktree_copy():
    assert _in_this_checkout(ovr.__file__), ovr.__file__


def test_the_version_was_bumped_for_this_edit():
    assert ovr.OVERRIDES_VERSION >= "2026-09-25"


@pytest.mark.parametrize("sym", sorted(EXPECTED))
def test_the_shipped_entries_are_the_measured_ones(sym):
    got = [(e.event_date, e.ratio, e.kind) for e in ovr.overrides_for(sym)]
    assert got == EXPECTED[sym]
    for e in ovr.overrides_for(sym):
        assert e.anchors == ((A2020, ANCHORS[sym]),)
        assert e.evidence


@pytest.mark.parametrize("sym,day,par,fmp", SWEEP)
def test_overrides_bring_the_parity_ratio_back_to_one(sym, day, par, fmp):
    b = _resolve(sym)
    assert par / (fmp * b.factor(day)) == pytest.approx(1.0, abs=0.015)


@pytest.mark.parametrize("sym,day,par,fmp", [s for s in SWEEP if s[1] < {
    "TRP": date(2024, 10, 2), "MMM": date(2024, 4, 1), "SKM": date(2021, 12, 27),
    "MTCH": date(2020, 7, 1)}[s[0]]])
def test_without_the_overrides_the_pre_event_sessions_are_off(sym, day, par, fmp):
    b = _resolve(sym, overrides=())
    assert abs(par / (fmp * b.factor(day)) - 1.0) > 0.05


def test_skm_keeps_its_calendar_row_and_adds_the_return_leg():
    b = _resolve("SKM")
    assert b.factor(date(2021, 11, 29)) == pytest.approx(0.607 * 1.6517)
    assert b.factor(date(2021, 11, 30)) == pytest.approx(1.6517)
    assert b.factor(date(2021, 12, 27)) == 1.0


def test_mtch_excludes_only_the_separation_row():
    b = _resolve("MTCH")
    assert b.factor(date(2020, 6, 30)) == 1.0
    assert [s.date for s in b.splits] == []


def test_a_rebased_file_refuses():
    with pytest.raises(SplitBasisRefused, match="MMM"):
        _resolve("MMM", anchor=150.49 * 1.0102)


# ---- the machine's real cache (read-only; skipped where absent) ----------------------------
def _cache():
    import ba2_common.config as cfg
    return cfg.CACHE_FOLDER


def _local_calendar(sym):
    p = os.path.join(_cache(), "fmp_history", f"mc_stock_split__{sym}.json")
    if not os.path.exists(p):
        return None
    rows = json.load(open(p, encoding="utf-8")).get("historical") or []
    out = []
    for r in rows:
        num, den = r.get("numerator"), r.get("denominator")
        ratio = float(num) / float(den) if num and den else float("nan")
        out.append(CalendarSplit(date.fromisoformat(r["date"]), ratio))
    return out


@pytest.mark.parametrize("sym", sorted(EXPECTED))
def test_the_local_cache_resolves_with_the_shipped_overrides(sym):
    import pandas as pd
    from ba2_common.core.split_basis import read_full_fetch_marker
    path = os.path.join(_cache(), "FMPOHLCVProvider", f"{sym}_1d.parquet")
    cal = _local_calendar(sym)
    if not os.path.exists(path) or cal is None:
        pytest.skip(f"no local FMP cache / calendar for {sym}")
    df = pd.read_parquet(path, columns=["Date", "Open", "High", "Low", "Close"])
    d = pd.to_datetime(df["Date"])
    d = d.dt.tz_localize(None) if getattr(d.dt, "tz", None) is not None else d
    days = d.dt.normalize().to_numpy(dtype="datetime64[ns]")
    b = resolve_symbol_split_basis(sym, days, df.Open.to_numpy(), df.High.to_numpy(),
                                   df.Low.to_numpy(), df.Close.to_numpy(), cal,
                                   marker=read_full_fetch_marker(path))
    for s, day, par, fmp in SWEEP:
        if s == sym:
            assert par / (fmp * b.factor(day)) == pytest.approx(1.0, abs=0.015), (sym, day)
