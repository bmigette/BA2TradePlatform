"""Stage-2 universe (2026-09-25): split-basis overrides measured on the 753-name universe.

Every ratio is the put-call-parity spot of the nearest expiry (ThetaData, as traded) over the
FMP close x the layer-1 factor, per session 2020-2025 -- the option basis guard's own measure
replayed on every session ("par/close" when no factor is applied). A plateau away from 1 that
ends at a dated event is a price adjustment FMP applies but its calendar lacks
(``add_price_adjustment``), a calendar row FMP's closes never took (``exclude_calendar_event``),
a row with the wrong ratio (both on one date: a replacement), or -- for a real one-day move near
a split in a freshly re-fetched file -- ``acknowledge_jump``.

Every add ratio follows the PLATEAU CONVENTION (median r over <= 60 sessions before / from the
event; ``tools/measure_basis_plateaus.py`` reproduces each), except fixed-ratio actions, which
take their nominal ratio.

Part 1 (files not re-fetched):
  TRP   South Bow spin-off, ex 2024-10-02: 1.1007 before / 1.0024 after = x1.098.
  MMM   Solventum spin-off, ex 2024-04-01: 1.1971 before / 1.0009 after = x1.1961.
  SKM   the ADR left FMP's basis on 2021-11-30 (calendar 0.607, kept) and came back on
        2021-12-27 (chain re-struck): 1.6494 before / 0.9963 after = x1.6555 on 2021-12-27.
  MTCH  the 2020-07-01 row 1751/500 is IAC's separation ratio: 0.2848 x 3.502 = 0.9974 before.

Part 2 (measured on the forced full re-fetch of 2026-09-25; interval medians of par/close):
  AMC   exclude the 2022-08-22 x2 APE row (0.0998 before = the 1:10 alone); acknowledge
        2023-08-14 x0.6445 and 2023-09-06 x0.6320 (real moves near the 1:10).
  FCEL  add the missing 1:30 of 2024-11-11 at its nominal 1/30; acknowledge 2019-06-05 x0.6667.
  GSK   exclude the 2022-07-22 4:5 row (1.0000 before, 1.0007 after: no step at all).
  QGEN  the 2024-01-30 x0.97 row IS in the prices (kept); the 2025-01-29 x0.9722 row is
        replaced by the measured x1.019; the 2026-01-08 x0.95 row stays excluded.
  LBRDA FMP carries the 2025-07-15 x1.067 row only from 2025-06-16: x0.9374 on 06-16.
  FTI   replace the 2021-02-16 x1.344 row by x1.2213.
  PHG   2022-05-13 x1.0769 and the 2023-05-11 row replaced by x0.966 (rows 2020-06-30 and
        2024-05-09 stay).
  FLEX  / CMCSA: FMP shows ex-distribution prices for the weeks before the spin's ex-date
        (1.3785 over 2023-12-04..2024-01-02; 1.0685 over 2025-12-08..2026-01-02): a pair of
        adds bracketing the stretch (FLEX x0.727 / x1.3748, CMCSA x0.9364 / x1.068).
  ARWR, ENLT, MARA, SMCI: acknowledged real moves near a split (layer 1 only; guard clean).

A flat series carries the real calendars and anchors; the local cache is read (read-only)
where present.
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


ADD, EXCL, ACK = (ovr.KIND_ADD_PRICE_ADJUSTMENT, ovr.KIND_EXCLUDE_CALENDAR_EVENT,
                  ovr.KIND_ACKNOWLEDGE_JUMP)
A2020 = date(2020, 1, 2)

#: symbol -> [(event_date, ratio, kind, anchors)]. Ratios: the PLATEAU CONVENTION of
#: split_basis_overrides (median r over <= 60 evaluable sessions before / from the event).
def _a(*extra):
    return lambda base: ((A2020, base),) + tuple(extra)


_1 = _a()
EXPECTED = {
    "TRP": [(date(2024, 10, 2), 1.098, ADD, _1)],
    "MMM": [(date(2024, 4, 1), 1.1961, ADD, _1)],
    "SKM": [(date(2021, 12, 27), 1.6555, ADD, _a((date(2021, 12, 15), 26.9)))],
    "MTCH": [(date(2020, 7, 1), 3.502, EXCL, _1)],
    "AMC": [(date(2022, 8, 22), 2.0, EXCL, _1), (date(2023, 8, 14), 0.644487, ACK, _1),
            (date(2023, 9, 6), 0.631965, ACK, _1)],
    "ARWR": [(date(2011, 11, 10), 1.401869, ACK, _1)],
    "ENLT": [(date(2023, 2, 10), 0.682692, ACK, lambda base: ((date(2023, 2, 10), 17.75),))],
    "FCEL": [(date(2019, 6, 5), 0.666667, ACK, _1), (date(2024, 11, 11), 1 / 30, ADD, _1)],
    "GSK": [(date(2022, 7, 22), 0.8, EXCL, _1)],
    "QGEN": [(date(2025, 1, 29), 0.9722, EXCL, _a((date(2024, 6, 3), 43.0))),
             (date(2025, 1, 29), 1.019, ADD, _a((date(2024, 6, 3), 43.0))),
             (date(2026, 1, 8), 0.95, EXCL, _1)],
    "LBRDA": [(date(2025, 6, 16), 0.9374, ADD, _a((date(2025, 6, 24), 89.03)))],
    "FTI": [(date(2021, 2, 16), 1.344, EXCL, _1), (date(2021, 2, 16), 1.2213, ADD, _1)],
    "PHG": [(date(2022, 5, 13), 1.0769, ADD, _a((date(2022, 9, 7), 17.43))),
            (date(2023, 5, 11), 1.045455, EXCL, _a((date(2022, 9, 7), 17.43))),
            (date(2023, 5, 11), 0.966, ADD, _a((date(2022, 9, 7), 17.43)))],
    "FLEX": [(date(2023, 12, 4), 0.727, ADD, _a((date(2023, 12, 11), 18.77))),
             (date(2024, 1, 3), 1.3748, ADD, _a((date(2023, 12, 11), 18.77)))],
    "CMCSA": [(date(2025, 12, 8), 0.9364, ADD, _a((date(2025, 12, 15), 26.43))),
              (date(2026, 1, 5), 1.068, ADD, _a((date(2025, 12, 15), 26.43)))],
    "MARA": [(date(2017, 11, 22), 1.535211, ACK, _1), (date(2017, 11, 24), 2.729358, ACK, _1),
             (date(2017, 11, 28), 0.591398, ACK, _1), (date(2019, 4, 10), 1.424354, ACK, _1)],
    "SMCI": [(date(2024, 10, 30), 0.673249, ACK, _1)],
}
#: The FMP close at 2020-01-02 each symbol's entries were measured on (ENLT: its own anchor).
ANCHORS = {"TRP": 48.0, "MMM": 150.49, "SKM": 22.5, "MTCH": 84.19, "AMC": 74.6, "ARWR": 63.12,
           "ENLT": 17.75, "FCEL": 60.3, "GSK": 46.92, "QGEN": 34.18,
           "LBRDA": 125.19, "FTI": 17.46, "PHG": 44.87, "FLEX": 12.78, "CMCSA": 45.37,
           "MARA": 1.05, "SMCI": 2.36}
#: A stretch override carries a second anchor inside its stretch (review 2026-09-26).
STRETCH = {"SKM": date(2021, 12, 15), "LBRDA": date(2025, 6, 24), "PHG": date(2022, 9, 7),
           "FLEX": date(2023, 12, 11), "CMCSA": date(2025, 12, 15), "QGEN": date(2024, 6, 3)}


def _anchors(sym):
    """Every distinct anchor of the symbol's shipped entries."""
    return sorted({a for _d, _r, _k, fa in EXPECTED[sym] for a in fa(ANCHORS[sym])})


def _cs(y, m, d, r):
    return CalendarSplit(date(y, m, d), r)


#: The real FMP calendars (fmp_history/mc_stock_split__<SYM>.json) and daily-file first bars.
CALENDARS = {
    "TRP": ([_cs(1984, 2, 13, 2.0)], date(2011, 6, 22)),
    "MMM": ([_cs(1972, 6, 16, 2.0), _cs(1987, 6, 16, 2.0), _cs(1994, 4, 11, 2.0),
             _cs(2003, 9, 30, 2.0)], date(2011, 6, 22)),
    "SKM": ([_cs(1996, 12, 27, 1.03), _cs(1997, 12, 29, 1.03), _cs(1998, 12, 29, 1.03),
             _cs(1999, 7, 13, 1.103), _cs(1999, 12, 29, 1.03), _cs(2021, 11, 30, 0.607)],
            date(2011, 6, 22)),
    "MTCH": ([_cs(1998, 3, 27, 2.0), _cs(2000, 2, 25, 2.0), _cs(2005, 8, 9, 0.9),
              _cs(2008, 8, 21, 1.146), _cs(2020, 7, 1, 1751 / 500)], date(2018, 1, 2)),
    "AMC": ([_cs(2022, 8, 22, 2.0), _cs(2023, 8, 24, 0.1)], date(2013, 12, 18)),
    "FCEL": ([_cs(1999, 11, 17, 1.5), _cs(2000, 9, 14, 2.0), _cs(2001, 6, 20, 2.0),
              _cs(2015, 12, 4, 1 / 12), _cs(2019, 5, 9, 1 / 12)], date(2011, 5, 17)),
    "GSK": ([_cs(1980, 2, 13, 2.0), _cs(1983, 2, 22, 2.0), _cs(1986, 1, 27, 2.0),
             _cs(1991, 11, 12, 2.0), _cs(2022, 7, 22, 0.8)], date(2011, 6, 22)),
    "QGEN": ([_cs(1999, 7, 16, 2.0), _cs(2000, 7, 14, 4.0), _cs(2017, 1, 25, 481 / 500),
              _cs(2024, 1, 30, 0.97), _cs(2025, 1, 29, 35 / 36), _cs(2026, 1, 8, 0.95)],
             date(2011, 9, 29)),
    "LBRDA": ([_cs(2014, 12, 11, 1.045), _cs(2025, 7, 15, 1.067)], date(2014, 11, 4)),
    "FTI": ([_cs(2007, 9, 4, 2.0), _cs(2011, 4, 1, 2.0), _cs(2017, 1, 17, 2.0),
             _cs(2021, 2, 16, 1.344)], date(2011, 6, 22)),
    "PHG": ([_cs(1984, 4, 27, 1.1), _cs(1999, 6, 1, 0.92), _cs(2000, 4, 18, 4.0),
             _cs(2000, 8, 1, 0.97), _cs(2020, 6, 30, 1.02), _cs(2023, 5, 11, 23 / 22),
             _cs(2024, 5, 9, 1.034)], date(2011, 6, 22)),
    "FLEX": ([_cs(1999, 1, 12, 2.0), _cs(1999, 12, 23, 2.0), _cs(2000, 10, 17, 2.0)],
             date(2011, 6, 22)),
    "CMCSA": ([_cs(2007, 2, 22, 1.5), _cs(2017, 2, 21, 2.0)], date(2011, 6, 20)),
}
#: The part-2 files carry the full-fetch marker the forced re-fetch writes.
REFETCHED = {"AMC", "FCEL", "GSK", "QGEN", "LBRDA", "FTI", "PHG", "FLEX", "CMCSA"}
LAST = date(2026, 9, 24)

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
    ("AMC", date(2020, 6, 1), 5.225, 53.100),
    ("AMC", date(2022, 6, 1), 12.845, 128.100),
    ("AMC", date(2023, 3, 1), 6.515, 65.700),
    ("AMC", date(2024, 6, 3), 4.840, 4.810),
    ("FCEL", date(2020, 6, 3), 2.400, 72.600),
    ("FCEL", date(2023, 6, 1), 2.170, 65.100),
    ("FCEL", date(2025, 3, 7), 6.525, 6.590),
    ("GSK", date(2020, 6, 1), 41.825, 41.850),
    ("GSK", date(2022, 6, 1), 43.050, 43.040),
    ("GSK", date(2023, 6, 1), 33.850, 33.800),
    ("QGEN", date(2020, 6, 1), 43.575, 43.990),
    ("QGEN", date(2024, 6, 3), 44.225, 43.000),     # between the kept 0.97 and the 2025 step
    ("QGEN", date(2025, 6, 2), 45.050, 44.900),
    ("LBRDA", date(2021, 6, 1), 160.800, 159.830),
    ("LBRDA", date(2025, 6, 24), 95.350, 89.030),   # the early ex-distribution stretch
    ("LBRDA", date(2025, 9, 2), 60.700, 60.450),
    ("FTI", date(2020, 6, 1), 7.875, 6.430),
    ("FTI", date(2021, 1, 4), 9.850, 8.060),
    ("FTI", date(2022, 6, 1), 8.500, 8.490),
    ("PHG", date(2021, 6, 1), 55.825, 52.370),      # dividend season: 1.3% low after the fix
    ("PHG", date(2022, 9, 7), 17.475, 17.430),
    ("PHG", date(2023, 9, 7), 21.500, 20.630),
    ("PHG", date(2024, 9, 3), 29.825, 29.770),
    ("FLEX", date(2020, 6, 1), 10.250, 10.220),
    ("FLEX", date(2023, 12, 11), 25.850, 18.770),   # the ex-distribution stretch
    ("FLEX", date(2024, 3, 1), 29.075, 29.000),
    ("CMCSA", date(2020, 6, 1), 40.195, 40.180),
    ("CMCSA", date(2025, 12, 15), 28.250, 26.430),  # the ex-distribution stretch
]
#: AMC: hard-to-borrow through the APE period, parity runs 1.5-4% under the spot.
TOLERANCE = {"AMC": 0.03}
#: A sweep session the unfixed basis puts beyond 5% (the sessions an override exists for).
OFF_WITHOUT = {("TRP", date(2020, 6, 1)), ("TRP", date(2024, 9, 3)), ("MMM", date(2022, 6, 1)),
               ("SKM", date(2020, 7, 1)), ("SKM", date(2021, 12, 15)), ("MTCH", date(2020, 6, 1)),
               ("AMC", date(2022, 6, 1)), ("FCEL", date(2023, 6, 1)), ("GSK", date(2020, 6, 1)),
               ("QGEN", date(2020, 6, 1)), ("LBRDA", date(2021, 6, 1)), ("FTI", date(2020, 6, 1)),
               ("PHG", date(2022, 9, 7)), ("FLEX", date(2023, 12, 11)),
               ("CMCSA", date(2025, 12, 15))}


def _no_ack(sym):
    """The symbol's non-acknowledge entries: a flat series has no jump to acknowledge."""
    return tuple(e for e in ovr.overrides_for(sym) if e.kind != ACK)


def _resolve(sym, overrides=None, scale=1.0):
    cal, first = CALENDARS[sym]
    days = np.arange(np.datetime64(first), np.datetime64(LAST) + 1, dtype="datetime64[D]")
    days = days[np.is_busday(days)]
    # Geometric ramp through the anchors (flat outside them): every anchor is hit exactly and no
    # one-day move is split-sized, so the mixed-basis scan sees an ordinary series.
    anchors = _anchors(sym)
    xs = np.array([np.datetime64(d).astype("datetime64[D]").astype(np.int64) for d, _c in anchors])
    ys = np.log([c * scale for _d, c in anchors])
    px = np.exp(np.interp(days.astype(np.int64), xs, ys))
    marker = {"fetched_on_utc": "2026-09-25"} if sym in REFETCHED else None
    return resolve_symbol_split_basis(sym, days, px, px, px, px, cal, marker=marker,
                                      overrides=_no_ack(sym) if overrides is None else overrides)


def test_the_module_under_test_is_the_worktree_copy():
    assert _in_this_checkout(ovr.__file__), ovr.__file__


def test_the_version_was_bumped_for_this_edit():
    assert ovr.OVERRIDES_VERSION >= "2026-09-26.2"


@pytest.mark.parametrize("sym", sorted(EXPECTED))
def test_the_shipped_entries_are_the_measured_ones(sym):
    got = sorted(((e.event_date, e.kind), (e.ratio, e.anchors)) for e in ovr.overrides_for(sym))
    want = sorted(((d, k), (r, fa(ANCHORS[sym]))) for d, r, k, fa in EXPECTED[sym])
    assert got == want
    assert all(e.evidence for e in ovr.overrides_for(sym))


@pytest.mark.parametrize("sym", sorted(STRETCH))
def test_a_stretch_override_is_anchored_inside_its_stretch(sym):
    inside = [e for e in ovr.overrides_for(sym) if e.kind == ADD
              and any(d == STRETCH[sym] for d, _c in e.anchors)]
    assert inside, sym
    for e in ovr.overrides_for(sym):
        if e.kind == ADD:
            assert len(e.anchors) >= 2, (sym, e.event_date)


def test_a_vendor_fix_of_only_the_stretch_refuses():
    """FLEX: FMP re-states 2023-12-04..2024-01-02 on the chain's basis while 2020 stays: the
    brackets would double count, and the stretch anchor refuses the symbol."""
    with pytest.raises(SplitBasisRefused, match="2023-12-11"):
        cal, first = CALENDARS["FLEX"]
        days = np.arange(np.datetime64(first), np.datetime64(LAST) + 1, dtype="datetime64[D]")
        days = days[np.is_busday(days)]
        px = np.full(len(days), 12.78)
        px[days == np.datetime64(date(2023, 12, 11))] = 18.77 * 1.3785
        resolve_symbol_split_basis("FLEX", days, px, px, px, px, cal, overrides=_no_ack("FLEX"))


@pytest.mark.parametrize("sym,day,par,fmp", SWEEP)
def test_overrides_bring_the_parity_ratio_back_to_one(sym, day, par, fmp):
    b = _resolve(sym)
    assert par / (fmp * b.factor(day)) == pytest.approx(1.0, abs=TOLERANCE.get(sym, 0.015))


@pytest.mark.parametrize("sym,day,par,fmp", [s for s in SWEEP if (s[0], s[1]) in OFF_WITHOUT])
def test_without_the_overrides_the_session_is_off(sym, day, par, fmp):
    b = _resolve(sym, overrides=())
    assert abs(par / (fmp * b.factor(day)) - 1.0) > 0.05


def test_skm_keeps_its_calendar_row_and_adds_the_return_leg():
    b = _resolve("SKM")
    assert b.factor(date(2021, 11, 29)) == pytest.approx(0.607 * 1.6555)
    assert b.factor(date(2021, 11, 30)) == pytest.approx(1.6555)
    assert b.factor(date(2021, 12, 27)) == 1.0


def test_mtch_excludes_only_the_separation_row():
    b = _resolve("MTCH")
    assert b.factor(date(2020, 6, 30)) == 1.0
    assert [s.date for s in b.splits] == []


def test_fti_and_phg_replace_the_vendor_ratio_on_its_own_date():
    fti = _resolve("FTI")
    assert fti.factor(date(2021, 2, 12)) == pytest.approx(1.2213)
    assert fti.factor(date(2021, 2, 16)) == 1.0
    phg = _resolve("PHG")
    assert phg.factor(date(2021, 6, 1)) == pytest.approx(1.0769 * 0.966 * 1.034)
    assert phg.factor(date(2022, 9, 7)) == pytest.approx(0.966 * 1.034)
    assert phg.factor(date(2023, 9, 7)) == pytest.approx(1.034)


def test_qgen_keeps_the_2024_row_and_replaces_the_2025_one():
    b = _resolve("QGEN")
    assert b.factor(date(2024, 1, 29)) == pytest.approx(0.97 * 1.019)
    assert b.factor(date(2024, 6, 3)) == pytest.approx(1.019)
    assert b.factor(date(2025, 6, 2)) == 1.0


def test_flex_and_cmcsa_bracket_the_ex_distribution_stretch():
    flex = _resolve("FLEX")
    assert flex.factor(date(2023, 12, 1)) == pytest.approx(0.727 * 1.3748)
    assert flex.factor(date(2023, 12, 4)) == pytest.approx(1.3748)
    assert flex.factor(date(2024, 1, 3)) == 1.0
    cmcsa = _resolve("CMCSA")
    assert cmcsa.factor(date(2025, 12, 5)) == pytest.approx(0.9364 * 1.068)
    assert cmcsa.factor(date(2025, 12, 31)) == pytest.approx(1.068)


def test_a_rebased_file_refuses():
    with pytest.raises(SplitBasisRefused, match="MMM"):
        _resolve("MMM", scale=1.0102)


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
    """The real file + calendar + marker resolve (acknowledged jumps included) and put every
    sweep session within tolerance: what the backtest's layer 1 will do."""
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
            assert par / (fmp * b.factor(day)) == pytest.approx(1.0, abs=TOLERANCE.get(sym, 0.015)), (sym, day)
