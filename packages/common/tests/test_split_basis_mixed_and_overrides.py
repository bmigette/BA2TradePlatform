"""Plan Part G1 (BT/live option parity): data-repair support in the split basis.

G1a -- ``check_split_basis`` flags a MIXED-BASIS file: a split-sized one-day close move near a
calendar split but not on its ex-date. The regression case is the real CRWD cache (4-for-1,
ex-date 2026-07-02; FMP's file is as traded through 2026-06-15 and /4 from 2026-06-16, so the
ex-date bar alone looks "consistent"). Real large moves far from any split (APP, ARM earnings,
NFLX 2022-04-20) must not be flagged.

G1b -- ``split_basis_overrides``: the spin-offs / stock dividends FMP applies to its closes but
omits from its split calendar (HON, NVS, SCCO). The expected ratios come from the 2026-09-23
parity sweep (``sessions_ratio_all.csv``): put-call-parity spot of the nearest expiry vs the
FMP close of the same session.

Fixture ``fixtures/fmp_daily_mixed_basis_slices.csv``: real OHLC slices of the local FMP daily
cache (CRWD 2026-05-26..07-31, APP 2024-10-24..11-21, ARM 2024-01-25..02-22, NFLX
2022-04-06..05-04).
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import split_basis_overrides as ovr
from ba2_common.core.split_basis import (
    REFETCH_VERDICTS, VERDICT_MIXED_BASIS, CalendarSplit, SplitBasisRefused, check_split_basis,
    needs_full_refetch, resolve_symbol_split_basis,
)


def _in_this_checkout(module_file) -> bool:
    """The module under test is THIS checkout's copy, not a stale editable install elsewhere
    (the venv's editable installs point at the main checkout; a worktree must not test that)."""
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False

FIXTURE = Path(__file__).parent / "fixtures" / "fmp_daily_mixed_basis_slices.csv"


def test_the_modules_under_test_are_the_worktree_copies():
    import ba2_common.core.split_basis as m
    assert _in_this_checkout(m.__file__) and _in_this_checkout(ovr.__file__), (m.__file__, ovr.__file__)


def _slice(sym, through=None):
    df = pd.read_csv(FIXTURE)
    df = df[df.symbol == sym]
    if through is not None:
        df = df[df.Date <= through.isoformat()]
    days = pd.to_datetime(df.Date).to_numpy(dtype="datetime64[ns]")
    return days, df.Open.to_numpy(), df.High.to_numpy(), df.Low.to_numpy(), df.Close.to_numpy()


def _check(sym, splits, through=None, marker=None):
    return check_split_basis(*_slice(sym, through), splits, symbol=sym, marker=marker)


CRWD_CAL = [CalendarSplit(date(2026, 7, 2), 4.0)]


# ---- G1a: mixed-basis files ----------------------------------------------------------------------
def test_mixed_basis_is_a_refetch_verdict():
    assert VERDICT_MIXED_BASIS in REFETCH_VERDICTS


def test_crwd_adjusted_twelve_sessions_early_is_mixed_basis():
    """The ex-date bar (2026-07-02) is on the adjusted basis -- the ratio rule alone said
    'consistent' -- but 2026-06-16 is a x0.2447 step: every bar before it is 4x too high."""
    checks = _check("CRWD", CRWD_CAL)
    assert [c.verdict for c in checks] == [VERDICT_MIXED_BASIS]
    assert checks[0].checked_bar == date(2026, 6, 16)
    assert "2026-06-16 x0.2447" in checks[0].reason
    assert needs_full_refetch(checks)


def test_the_ratio_rule_alone_passes_crwd_which_is_why_the_scan_exists(monkeypatch):
    import ba2_common.core.split_basis as sb
    monkeypatch.setattr(sb, "_mixed_basis_check", lambda *a, **k: None)
    assert [c.verdict for c in _check("CRWD", CRWD_CAL)] == ["consistent"]


def test_a_marker_does_not_exempt_a_mixed_basis_file():
    checks = _check("CRWD", CRWD_CAL, marker={"fetched_on_utc": "2026-09-01"})
    assert [c.verdict for c in checks] == [VERDICT_MIXED_BASIS]


def test_a_split_after_the_last_bar_with_the_early_step_already_in_the_file_is_mixed():
    """Cached through 2026-06-30: the split is 'future', but the step it caused is already in."""
    checks = _check("CRWD", CRWD_CAL, through=date(2026, 6, 30))
    assert [c.verdict for c in checks] == [VERDICT_MIXED_BASIS]


def test_crwd_resolve_refuses_naming_the_mixed_basis():
    with pytest.raises(SplitBasisRefused, match="mixed_basis"):
        resolve_symbol_split_basis("CRWD", *_slice("CRWD"), CRWD_CAL)


@pytest.mark.parametrize("sym,calendar,day,ratio", [
    ("APP", [], date(2024, 11, 7), 1.4627),
    ("ARM", [], date(2024, 2, 8), 1.4789),
    # the real NFLX calendar: every split is years away from 2022-04-20
    ("NFLX", [CalendarSplit(date(2004, 2, 12), 2.0), CalendarSplit(date(2015, 7, 15), 7.0),
              CalendarSplit(date(2025, 11, 17), 10.0)], date(2022, 4, 20), 0.6489),
])
def test_real_earnings_moves_far_from_any_split_are_not_flagged(sym, calendar, day, ratio):
    days, o, h, l, c = _slice(sym)
    i = int(np.flatnonzero(days == np.datetime64(day))[0])
    assert c[i] / c[i - 1] == pytest.approx(ratio, abs=1e-4)          # the move IS beyond x1.4
    checks = check_split_basis(days, o, h, l, c, calendar, symbol=sym)
    assert not any(ch.verdict == VERDICT_MIXED_BASIS for ch in checks)


def test_the_same_nflx_move_is_flagged_only_inside_the_window_and_never_on_the_ex_date():
    days, o, h, l, c = _slice("NFLX")
    near = check_split_basis(days, o, h, l, c, [CalendarSplit(date(2022, 5, 2), 2.0)])
    assert [ch.verdict for ch in near] == [VERDICT_MIXED_BASIS]      # 12 days later
    far = check_split_basis(days, o, h, l, c, [CalendarSplit(date(2022, 5, 21), 2.0)])
    assert [ch.verdict for ch in far] == ["future"]                    # 31 days later
    # A split ON that day is the ratio rule's business (a 1.5-sized step there is "drift" or
    # "consistent", never "mixed_basis").
    on = check_split_basis(days, o, h, l, c, [CalendarSplit(date(2022, 4, 20), 1.5)])
    assert [ch.verdict for ch in on] != [VERDICT_MIXED_BASIS]


# ---- G1b: overrides ----------------------------------------------------------------------------------
def _flat_series(first, last, anchors):
    """A flat business-day series; ``anchors`` {date: close} are placed exactly."""
    days = np.arange(np.datetime64(first), np.datetime64(last) + 1, dtype="datetime64[D]")
    days = days[np.is_busday(days)]
    base = next(iter(anchors.values()))
    px = np.full(len(days), float(base))
    for d, v in anchors.items():
        px[days == np.datetime64(d)] = v
    return days, px


# The real FMP calendars (fmp_history/mc_stock_split__<SYM>.json) and file ranges.
CALENDARS = {
    "HON": [CalendarSplit(date(2026, 6, 28), 1907 / 2000), CalendarSplit(date(2018, 10, 28), 1.011),
            CalendarSplit(date(1997, 9, 16), 2.0)],
    "NVS": [CalendarSplit(date(2019, 4, 9), 279 / 250), CalendarSplit(date(2000, 5, 11), 2.0)],
    "SCCO": [CalendarSplit(date(2026, 8, 11), 253 / 250), CalendarSplit(date(2024, 5, 7), 1263 / 1250),
             CalendarSplit(date(2012, 2, 13), 1.0107), CalendarSplit(date(2008, 7, 10), 3.0)],
}
ANCHORS = {"HON": 178.71, "NVS": 89.85, "SCCO": 40.18}
MARKER = {"fetched_on_utc": "2026-09-16"}   # the real files' full-fetch marker date
FIRST, LAST = date(2011, 9, 20), date(2026, 9, 16)

#: (symbol, session, put-call-parity spot, FMP close) from the 2026-09-23 sweep.
SWEEP = [
    ("HON", date(2020, 6, 1), 146.19, 144.49),
    ("HON", date(2023, 9, 1), 188.525, 186.22),
    ("HON", date(2025, 6, 2), 225.475, 222.92),
    ("HON", date(2025, 11, 3), 198.2, 207.41),       # after the spin-off
    ("NVS", date(2020, 6, 1), 86.725, 82.02),
    ("NVS", date(2022, 6, 1), 90.075, 84.82),
    ("NVS", date(2024, 9, 3), 118.725, 118.5),       # after the spin-off
    ("SCCO", date(2020, 6, 1), 36.85, 34.66),
    ("SCCO", date(2024, 9, 3), 96.675, 91.92),
    ("SCCO", date(2025, 3, 3), 86.85, 83.99),
    ("SCCO", date(2025, 6, 2), 92.625, 90.44),
    ("SCCO", date(2025, 9, 2), 97.45, 96.1),         # before the 2025-11-12 one only
]


def _resolve(sym, overrides=None, anchor=None):
    days, px = _flat_series(FIRST, LAST, {date(2020, 1, 2): ANCHORS[sym] if anchor is None else anchor})
    return resolve_symbol_split_basis(sym, days, px, px, px, px, CALENDARS[sym], marker=MARKER,
                                      overrides=overrides)


@pytest.mark.parametrize("sym,day,par,fmp", SWEEP)
def test_overrides_bring_the_parity_ratio_back_to_one(sym, day, par, fmp):
    b = _resolve(sym)
    assert par / (fmp * b.factor(day)) == pytest.approx(1.0, abs=0.013)


def test_without_the_overrides_the_pre_event_sessions_are_off():
    for sym, day, par, fmp in SWEEP[:3] + SWEEP[4:6] + SWEEP[7:9]:
        b = _resolve(sym, overrides=())
        assert abs(par / (fmp * b.factor(day)) - 1.0) > 0.02, (sym, day)


def test_the_shipped_entries_carry_the_measured_anchors():
    """Constants vs constants: the shipped table is the one these tests exercise."""
    for sym in ("HON", "NVS", "SCCO"):
        for e in ovr.overrides_for(sym):
            assert e.anchors == ((date(2020, 1, 2), ANCHORS[sym]),)
    assert len(ovr.overrides_for("SCCO")) == 6


def _local_fmp_path(sym):
    import ba2_common.config as cfg
    import os
    return os.path.join(cfg.CACHE_FOLDER, "FMPOHLCVProvider", f"{sym}_1d.parquet")


@pytest.mark.parametrize("sym", ["HON", "NVS", "SCCO"])
def test_the_anchors_match_the_local_fmp_cache_when_present(sym):
    """Reads the machine's real FMP daily file (read-only); skipped where there is none. A
    failure here means the local file was re-based since the entry was measured -- exactly
    what makes the option path refuse the symbol."""
    import os
    path = _local_fmp_path(sym)
    if not os.path.exists(path):
        pytest.skip(f"no local FMP cache for {sym}")
    df = pd.read_parquet(path, columns=["Date", "Close"])
    by_day = dict(zip(pd.to_datetime(df["Date"]).dt.date, df["Close"].astype(float)))
    for e in ovr.overrides_for(sym):
        for day, close in e.anchors:
            assert by_day[day] == pytest.approx(close, rel=ovr.ANCHOR_TOLERANCE), (sym, day)


def test_a_changed_anchor_refuses_naming_symbol_anchor_expected_and_actual():
    with pytest.raises(SplitBasisRefused) as e:
        _resolve("NVS", anchor=89.85 * 1.0575)          # a re-fetch that re-based the history
    msg = str(e.value)
    assert "NVS" in msg and "2020-01-02" in msg and "89.85" in msg and "95.0164" in msg


def test_an_anchor_inside_the_tolerance_passes():
    b = _resolve("NVS", anchor=89.85 * 1.0009)
    assert b.factor(date(2023, 10, 3)) == pytest.approx(1.0575)


def test_add_price_adjustment_is_not_classified_undetectable_without_a_marker():
    """A 5.75% spin-off is below MIN_DETECTABLE_FACTOR; as a calendar split it would need a
    marker. As an override it needs none (only NVS's own calendar row would)."""
    days, px = _flat_series(date(2020, 1, 2), LAST, {date(2020, 1, 2): 89.85})
    b = resolve_symbol_split_basis("NVS", days, px, px, px, px, CALENDARS["NVS"])
    assert b.factor(date(2023, 10, 3)) == pytest.approx(1.0575)
    assert b.factor(date(2023, 10, 4)) == 1.0


def _synthetic(kind, event, ratio=0.5):
    return (ovr.BasisOverride("XYZ", event, ratio, kind, ((date(2024, 1, 2), 100.0),), "test"),)


def test_exclude_calendar_event_drops_the_row_and_its_verdict():
    """A calendar row the operator knows the prices never took (a step-free file reads as
    'adjusted for it', so without the override the factor would divide it out)."""
    cal = [CalendarSplit(date(2024, 6, 3), 2.0)]
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    base = resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal, overrides=())
    assert base.factor(date(2024, 3, 1)) == 2.0
    ex = resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal,
                                    overrides=_synthetic(ovr.KIND_EXCLUDE_CALENDAR_EVENT, date(2024, 6, 3)))
    assert ex.factor(date(2024, 3, 1)) == 1.0 and ex.splits == ()
    assert ex.overrides_version is not None


def test_exclude_calendar_event_rescues_a_refusing_row():
    cal = [CalendarSplit(date(2024, 6, 3), 1.25)]       # undetectable without a marker
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    with pytest.raises(SplitBasisRefused, match="undetectable"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal, overrides=())
    b = resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal,
                                   overrides=_synthetic(ovr.KIND_EXCLUDE_CALENDAR_EVENT, date(2024, 6, 3)))
    assert b.factor(date(2024, 3, 1)) == 1.0


def test_an_exclusion_of_a_row_the_calendar_lacks_refuses():
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    with pytest.raises(SplitBasisRefused, match="does not hold"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, [],
                                   overrides=_synthetic(ovr.KIND_EXCLUDE_CALENDAR_EVENT, date(2024, 6, 3)))


def test_an_added_adjustment_the_calendar_now_carries_refuses_rather_than_doubling():
    cal = [CalendarSplit(date(2024, 6, 3), 1.05)]
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    with pytest.raises(SplitBasisRefused, match="double count"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal, marker={"fetched_on_utc": "2025-01-02"},
                                   overrides=_synthetic(ovr.KIND_ADD_PRICE_ADJUSTMENT, date(2024, 6, 3), 1.05))


def test_a_symbol_without_overrides_is_unchanged():
    days, px = _flat_series(date(2024, 1, 2), date(2026, 3, 2), {date(2024, 1, 2): 56.0})
    cal = [CalendarSplit(date(2025, 11, 17), 10.0)]
    with_default = resolve_symbol_split_basis("NFLX", days, px, px, px, px, cal)
    with_none = resolve_symbol_split_basis("NFLX", days, px, px, px, px, cal, overrides=())
    assert with_default == with_none
    assert with_default.overrides_version is None
    assert with_default.identity()[-1] is None


def test_identity_changes_when_the_overrides_version_changes(monkeypatch):
    b1 = _resolve("HON")
    assert b1.overrides_version.startswith(ovr.OVERRIDES_VERSION + ":")
    monkeypatch.setattr(ovr, "OVERRIDES_VERSION", ovr.OVERRIDES_VERSION + "-next")
    b2 = _resolve("HON")
    assert b2.identity() != b1.identity()
    assert b2.factor(date(2020, 6, 1)) == b1.factor(date(2020, 6, 1))


def test_identity_changes_when_an_entry_changes():
    edited = tuple(ovr.BasisOverride(o.symbol, o.event_date, o.ratio + 0.001 if o.symbol == "HON" else o.ratio,
                                     o.kind, o.anchors, o.evidence) for o in ovr.BASIS_OVERRIDES)
    assert _resolve("HON", overrides=edited).identity() != _resolve("HON").identity()


# ---- acknowledge_jump: a real move near a split --------------------------------------------------
def _ack_series():
    """Flat 100, one-basis file with a 2-for-1 at 2024-06-03 (adjusted: no step), and a REAL
    +50% earnings gap on 2024-05-10, 24 days (16 sessions) before it -- outside the ratio rule's own 10-bar neighbourhood -> the scan flags mixed_basis."""
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    px = px.copy()
    px[days >= np.datetime64("2024-05-10")] = 150.0
    return days, px


ACK_CAL = [CalendarSplit(date(2024, 6, 3), 2.0)]


def _ack(day=date(2024, 5, 10), ratio=1.5):
    return (ovr.BasisOverride("XYZ", day, ratio, ovr.KIND_ACKNOWLEDGE_JUMP,
                              ((date(2024, 1, 2), 100.0),), "earnings gap"),)


def test_a_real_move_near_a_split_is_mixed_basis_until_acknowledged():
    days, px = _ack_series()
    assert [c.verdict for c in check_split_basis(days, px, px, px, px, ACK_CAL, symbol="XYZ")]         == [VERDICT_MIXED_BASIS]
    with pytest.raises(SplitBasisRefused, match="mixed_basis"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, ACK_CAL, overrides=())
    checks = check_split_basis(days, px, px, px, px, ACK_CAL, symbol="XYZ",
                               acknowledged_jumps={date(2024, 5, 10): 1.5})
    assert [c.verdict for c in checks] == ["consistent"]
    b = resolve_symbol_split_basis("XYZ", days, px, px, px, px, ACK_CAL, overrides=_ack())
    assert b.factor(date(2024, 3, 1)) == 2.0                 # the split stays in the factor
    assert b.overrides_version is not None


def test_an_acknowledgement_removes_only_its_own_jump():
    days, px = _ack_series()
    px = px.copy()
    px[days >= np.datetime64("2024-06-28")] = 60.0            # a second, unacknowledged move
    with pytest.raises(SplitBasisRefused, match="2024-06-28"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, ACK_CAL, overrides=_ack())


def test_an_acknowledged_jump_that_no_longer_matches_the_file_refuses():
    days, px = _ack_series()
    with pytest.raises(SplitBasisRefused, match="no longer matches the file.*x1.5000"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, ACK_CAL, overrides=_ack(ratio=1.6))
    with pytest.raises(SplitBasisRefused, match="no longer matches the file"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, ACK_CAL,
                                   overrides=_ack(day=date(2024, 5, 13)))
    # and a non-matching acknowledgement suppresses nothing in the plain check either
    checks = check_split_basis(days, px, px, px, px, ACK_CAL, symbol="XYZ",
                               acknowledged_jumps={date(2024, 5, 10): 1.6})
    assert [c.verdict for c in checks] == [VERDICT_MIXED_BASIS]


def test_the_default_check_honours_the_shipped_acknowledgements(monkeypatch):
    """The warmup preflight and the live drift report call check_split_basis without
    overrides: the shipped table is consulted by symbol."""
    days, px = _ack_series()
    monkeypatch.setattr(ovr, "BASIS_OVERRIDES", ovr.BASIS_OVERRIDES + _ack())
    assert [c.verdict for c in check_split_basis(days, px, px, px, px, ACK_CAL, symbol="XYZ")]         == ["consistent"]
    assert [c.verdict for c in check_split_basis(days, px, px, px, px, ACK_CAL, symbol="OTHER")]         == [VERDICT_MIXED_BASIS]


def test_a_ratio_less_calendar_row_inside_the_file_refuses_unless_excluded():
    cal = [CalendarSplit(date(2024, 6, 3), float("nan"))]
    days, px = _flat_series(date(2024, 1, 2), date(2024, 12, 31), {date(2024, 1, 2): 100.0})
    with pytest.raises(SplitBasisRefused, match="no usable ratio"):
        resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal, overrides=())
    b = resolve_symbol_split_basis("XYZ", days, px, px, px, px, cal,
                                   overrides=_synthetic(ovr.KIND_EXCLUDE_CALENDAR_EVENT, date(2024, 6, 3)))
    assert b.splits == () and b.factor(date(2024, 3, 1)) == 1.0
