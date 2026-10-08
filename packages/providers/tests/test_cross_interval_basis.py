"""Cross-interval price-level check (``ba2_providers.ohlcv.cross_interval_basis``): the pure
classification on synthetic bars, and the per-file-identity memo.

MEASURED TOLERANCES (the real cache, 2020-01-01..2026-10-07, 7 048 symbols with a 5-minute file; final scan of
2026-10-08 20:29-20:44 CEST, FMPOHLCVProvider, while another task was repairing files in that cache):
6 585 ok, 203 mismatched (5 constant_factor + 185 factor_changes + 13 noisy), 255 insufficient, 5 no_intraday.
Healthy symbols: median |ln close ratio| <= 0.0091 for every ok symbol (99th percentile 0.0010), MAD 0.0005
(median) / 0.0072 (99th percentile) / 0.028 (worst); 15-25% of the sessions of a thin name sit outside 2%
with a median of exactly 1 (so scatter is not a verdict).  All 7 hand-found defects (DD 0.333, SIRI 0.10,
SAFE 6.25, PROSY 2.18, ESEA 1.23, IVR 1.05, CRESY 1.0165) were flagged by the first full scan (17:36-17:48
CEST); by the final scan DD had been repaired by the other task and reads ok, the other six are still flagged.
FALSE POSITIVES: of the 219 symbols flagged by the pre-final scan, 218 are corroborated by the INDEPENDENT
session-HIGH and session-LOW ratios sitting at the same level as the close ratio (within 2%), i.e. the daily
and the 5-minute series really are on different price levels; the 219th (BLNE) has one stray high.  Estimated
false-positive rate of the mismatch classes: <= 1 in 219 flagged = 0.5% of the flagged, 0.015% of the 6 800
judged symbols.  The small levels are the known spin-offs (BDX 1.025 until the 2022-04 Embecta spin, ZBH
1.031 until 2022-03, IBM 1.046 until 2021-11 Kyndryl, MRK 1.049 until 2021-06 Organon, ILMN 1.029 until the
2024-06 GRAIL spin, FNF 1.040 until 2022-12, O 1.033 until 2021-11, WPC 1.021 until 2023-11).

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_cross_interval_basis.py -q -p no:cacheprovider
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_providers.ohlcv import cross_interval_basis as cib

FIRST, LAST = date(2023, 1, 3), date(2023, 12, 29)
BAR_MIN = 5


def _session_list(a, b):
    out = []
    for _o, c in nyse_regular_sessions(a, b):
        local = c.astimezone(NY_TZ)
        out.append((local.date(), local.hour * 60 + local.minute))
    return out


def make_pair(first=FIRST, last=LAST, seed=7, jitter=0.0004, drop_after=None):
    """A consistent (daily, intraday 5-minute) pair over the NYSE sessions of [first, last]: ratio 1.

    The daily close is the last 5-minute close times (1 + closing-auction jitter); daily high/low are the
    session extrema. ``drop_after={date: minute}`` truncates that session's bars (an incomplete session)."""
    rng = np.random.default_rng(seed)
    price = 50.0
    drows, irows = [], []
    for day, close_min in _session_list(first, last):
        n_bars = (close_min - (9 * 60 + 30)) // BAR_MIN
        r = rng.normal(0.0001, 0.0006, n_bars)
        path = price * np.exp(np.cumsum(r))
        price = float(path[-1])
        stamps = [pd.Timestamp(day) + pd.Timedelta(minutes=9 * 60 + 30 + BAR_MIN * k) for k in range(n_bars)]
        hi = path * (1 + np.abs(rng.normal(0, 0.0003, n_bars)))
        lo = path * (1 - np.abs(rng.normal(0, 0.0003, n_bars)))
        bars = pd.DataFrame({"Date": stamps, "Open": path, "High": hi, "Low": lo, "Close": path, "Volume": 1000.0})
        drows.append({"Date": pd.Timestamp(day), "Open": float(path[0]), "High": float(hi.max()),
                      "Low": float(lo.min()), "Close": float(path[-1] * (1 + rng.normal(0, jitter))),
                      "Volume": 1e6})
        if drop_after and day in drop_after:
            cut = pd.Timestamp(day) + pd.Timedelta(minutes=drop_after[day])
            bars = bars[bars["Date"] < cut]
        irows.append(bars)
    return pd.DataFrame(drows), pd.concat(irows, ignore_index=True)


def scale(intraday, factor, before=None, after=None, only=None):
    """Multiply the intraday prices by ``factor`` on the sessions selected (before/after a date, or a
    (lo, hi) range)."""
    out = intraday.copy()
    day = out["Date"].dt.normalize()
    m = pd.Series(True, index=out.index)
    if before is not None:
        m &= day < pd.Timestamp(before)
    if after is not None:
        m &= day >= pd.Timestamp(after)
    if only is not None:
        m &= (day >= pd.Timestamp(only[0])) & (day <= pd.Timestamp(only[1]))
    for c in ("Open", "High", "Low", "Close"):
        out.loc[m, c] = out.loc[m, c] * factor
    return out


def judge(daily, intraday, symbol="SYN", interval="5min"):
    t = cib.reduce_sessions(daily, intraday, interval)
    return cib.classify(t, symbol, FIRST, LAST), t


@pytest.fixture(scope="module")
def pair():
    return make_pair()


# --------------------------------------------------------------------------- the pure classification
def test_consistent_bars_are_ok(pair):
    res, t = judge(*pair)
    assert res.klass == cib.KLASS_OK, res.reason
    assert res.common_sessions == len(t) and res.no_intraday_sessions == 0
    assert abs(res.median_close_ratio - 1.0) < 1e-3
    assert abs(res.median_high_ratio - 1.0) < 2e-3 and abs(res.median_low_ratio - 1.0) < 2e-3
    assert res.segments == [] and not res.mismatched and not res.unjudged


@pytest.mark.parametrize("name,factor", [("DD", 1 / 3), ("SIRI", 0.10), ("SAFE", 6.25), ("PROSY", 2.18),
                                         ("ESEA", 1.23), ("IVR", 1.05), ("CRESY", 1.0165)])
def test_the_seven_hand_found_symbols_are_constant_factors(pair, name, factor):
    daily, intra = pair
    res, _ = judge(daily, scale(intra, factor), name)
    assert res.klass == cib.KLASS_CONSTANT_FACTOR, (name, res.klass, res.reason)
    assert res.factor == pytest.approx(factor, rel=3e-3)
    assert res.mismatched
    assert name in res.describe() and "constant_factor" in res.describe()


def test_a_factor_change_reports_each_segment_and_the_boundary_date(pair):
    daily, intra = pair
    boundary = date(2023, 6, 15)
    res, _ = judge(daily, scale(intra, 2.0, before=boundary))
    assert res.klass == cib.KLASS_FACTOR_CHANGES
    assert [round(s.factor, 2) for s in res.segments] == [2.0, 1.0]
    got = date.fromisoformat(res.boundaries[0])
    assert abs((got - boundary).days) <= 3, got           # the re-adjustment boundary, to the session
    assert res.segments[0].first_day == "2023-01-03" and res.segments[-1].last_day == "2023-12-29"
    assert res.factor == pytest.approx(1.0, abs=2e-3)      # the LATEST level
    assert "factor_changes" in res.describe() and "x2" in res.describe()


def test_garbage_levels_are_noisy(pair):
    daily, intra = pair
    rng = np.random.default_rng(3)
    bad = intra.copy()
    days = bad["Date"].dt.normalize()
    per_day = {d: float(np.exp(rng.uniform(-1.6, 1.6))) for d in days.unique()}
    mult = days.map(per_day)
    for c in ("Open", "High", "Low", "Close"):
        bad[c] = bad[c] * mult
    res, _ = judge(daily, bad)
    assert res.klass == cib.KLASS_NOISY and res.mismatched


def test_sessions_with_a_daily_bar_but_no_intraday_are_counted_not_hidden(pair):
    daily, intra = pair
    gap = (intra["Date"] >= "2023-04-03") & (intra["Date"] < "2023-05-15")
    res, t = judge(daily, intra[~gap])
    assert res.klass == cib.KLASS_OK
    missing = int(((daily["Date"] >= "2023-04-03") & (daily["Date"] < "2023-05-15")).sum())
    assert res.no_intraday_sessions == missing > 20
    assert res.common_sessions == len(daily) - missing


def test_half_day_is_complete_at_its_early_close_and_a_truncated_session_is_not():
    # 2023-11-24 is a 13:00 half day; 2023-12-05 is cut at 14:00 (a vendor hole)
    daily, intra = make_pair(drop_after={date(2023, 12, 5): 14 * 60})
    half = intra[intra["Date"].dt.normalize() == "2023-11-24"]
    assert half["Date"].dt.hour.max() == 12 and half["Date"].dt.minute.max() == 55   # the last bar is 12:55
    t = cib.reduce_sessions(daily, intra, "5min")
    i_half = int(np.flatnonzero(t.days == np.datetime64("2023-11-24"))[0])
    i_cut = int(np.flatnonzero(t.days == np.datetime64("2023-12-05"))[0])
    assert t.complete[i_half] and t.has_intraday[i_half]
    assert t.has_intraday[i_cut] and not t.complete[i_cut]
    res = cib.classify(t, "SYN", FIRST, LAST)
    assert res.klass == cib.KLASS_OK and res.incomplete_sessions == 1
    assert res.common_sessions == len(t) - 1


def test_too_few_sessions_cannot_be_judged_and_are_never_ok():
    daily, intra = make_pair(date(2023, 3, 1), date(2023, 3, 9))       # 7 sessions
    res = cib.classify(cib.reduce_sessions(daily, intra, "5min"), "SYN", date(2023, 3, 1), date(2023, 3, 9))
    assert res.klass == cib.KLASS_INSUFFICIENT and res.unjudged and not res.mismatched
    assert res.klass != cib.KLASS_OK and "comparable sessions" in res.reason
    # ... even when the 7 sessions are on a wrong basis: reported as unjudged, not as ok
    res2 = cib.classify(cib.reduce_sessions(daily, scale(intra, 3.0), "5min"), "SYN",
                        date(2023, 3, 1), date(2023, 3, 9))
    assert res2.klass == cib.KLASS_INSUFFICIENT


def test_no_intraday_and_no_daily_are_their_own_classes(pair):
    daily, intra = pair
    t = cib.reduce_sessions(daily, intra.iloc[0:0], "5min")
    assert cib.classify(t, "SYN", FIRST, LAST).klass == cib.KLASS_NO_INTRADAY
    t = cib.reduce_sessions(daily.iloc[0:0], intra, "5min")
    assert cib.classify(t, "SYN", FIRST, LAST).klass == cib.KLASS_NO_DAILY
    t = cib.reduce_sessions(daily, intra, "5min")
    assert cib.classify(t, "SYN", date(2031, 1, 2), date(2031, 2, 2)).klass == cib.KLASS_NO_DAILY


def test_a_tz_aware_intraday_is_read_in_new_york_wall_clock(pair):
    daily, intra = pair
    aware = intra.copy()
    aware["Date"] = aware["Date"].dt.tz_localize(NY_TZ).dt.tz_convert("UTC")
    res_aware, _ = judge(daily, aware)
    res_naive, _ = judge(daily, intra)
    assert res_aware.klass == res_naive.klass == cib.KLASS_OK
    assert res_aware.common_sessions == res_naive.common_sessions


def test_other_intraday_intervals_work():
    daily, intra5 = make_pair(first=date(2023, 3, 1), last=date(2023, 9, 29))
    # 30-minute bars: last bar of the session starts 15:30
    d30 = intra5.copy()
    d30["Date"] = d30["Date"].dt.floor("30min") - pd.Timedelta(minutes=0)
    agg = d30.groupby("Date").agg(Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
                                  Close=("Close", "last")).reset_index()
    t = cib.reduce_sessions(daily, agg, "30min")
    assert cib.classify(t, "SYN", date(2023, 3, 1), date(2023, 9, 29)).klass == cib.KLASS_OK
    assert cib.interval_minutes("1h") == 60 and cib.interval_minutes("5m") == 5
    with pytest.raises(ValueError):
        cib.interval_minutes("7min")


# --------------------------------------------------------------------------- tolerances: what is NOT a defect
def test_closing_auction_scatter_and_a_few_bad_prints_are_ok(pair):
    daily, intra = pair
    rng = np.random.default_rng(11)
    d2 = daily.copy()
    d2["Close"] = d2["Close"] * np.exp(rng.normal(0, 0.012, len(d2)))        # a thin name's auction scatter
    wild = rng.choice(len(d2), size=12, replace=False)                         # a few 10% bad prints
    d2.loc[wild, "Close"] = d2.loc[wild, "Close"] * 1.10
    res, _ = judge(d2, intra)
    assert res.klass == cib.KLASS_OK, res.reason
    assert res.out_share > 0.05                       # plenty of sessions are outside 2%: not a verdict


def test_a_short_small_stretch_is_not_a_basis_but_a_long_one_is(pair):
    daily, intra = pair
    short = scale(intra, 1.03, only=("2023-05-01", "2023-05-19"))             # 15 sessions at +3%
    assert judge(daily, short)[0].klass == cib.KLASS_OK
    long_small = scale(intra, 1.03, only=("2023-03-01", "2023-08-31"))        # ~125 sessions at +3%
    r = judge(daily, long_small)[0]
    assert r.klass == cib.KLASS_FACTOR_CHANGES and 1.02 < r.segments[1].factor < 1.04
    tiny_short = scale(intra, 1.015, only=("2023-03-01", "2023-08-31"))       # 1.5% for half a year
    assert judge(daily, tiny_short)[0].klass == cib.KLASS_OK
    # CRESY: 1.65% for the WHOLE year is a defect (250 sessions)
    assert judge(daily, scale(intra, 1.0165))[0].klass == cib.KLASS_CONSTANT_FACTOR


def test_a_short_big_stretch_is_a_factor_change(pair):
    daily, intra = pair
    res, _ = judge(daily, scale(intra, 0.1, only=("2023-07-03", "2023-07-21")))   # 15 sessions at 10x lower
    assert res.klass == cib.KLASS_FACTOR_CHANGES
    assert any(abs(s.factor - 0.1) < 0.01 and s.sessions >= 12 for s in res.segments)


def test_the_write_time_tail_check_catches_a_short_append_across_a_split(tmp_path, monkeypatch):
    daily, intra = make_pair()
    folder = tmp_path / "FMPOHLCVProvider"
    folder.mkdir()
    daily.assign(effective_date=daily["Date"]).to_parquet(folder / "TAIL_1d.parquet", index=False)
    store = cib.BasisStore("FMPOHLCVProvider", memo=None, folder=str(folder))
    # 3 sessions on the other basis appended after a consistent history: too short for the whole-history
    # verdict, but the TAIL is x2 off the daily level
    frame = scale(intra, 2.0, after="2023-12-22")
    res = store.check_frame("TAIL", "5min", frame)
    assert res.mismatched and res.factor == pytest.approx(2.0, rel=5e-3)
    assert store.check_frame("TAIL", "5min", intra).klass == cib.KLASS_OK


# --------------------------------------------------------------------------- files, identity, memo
@pytest.fixture
def cache_folder(tmp_path):
    folder = tmp_path / "FMPOHLCVProvider"
    folder.mkdir()
    return folder


def _write(folder, symbol, daily, intra):
    daily.assign(effective_date=daily["Date"]).to_parquet(folder / f"{symbol}_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(folder / f"{symbol}_5min.parquet", index=False)


def test_the_memo_means_no_rescan_until_a_file_changes(cache_folder, tmp_path, pair):
    daily, intra = pair
    _write(cache_folder, "AAA", daily, intra)
    memo = cib.MemoDir(str(tmp_path / "memo"))
    s1 = cib.BasisStore("FMPOHLCVProvider", memo=memo, folder=str(cache_folder))
    assert s1.check("AAA", "5min", FIRST, LAST).klass == cib.KLASS_OK
    assert s1.reads == 1
    for _ in range(5):                                  # a GA's trials
        s1.check("AAA", "5min", FIRST, LAST)
    assert s1.reads == 1 and s1.mem_hits == 5           # in-process memo: no parquet read

    s2 = cib.BasisStore("FMPOHLCVProvider", memo=memo, folder=str(cache_folder))   # another process / job
    assert s2.check("AAA", "5min", FIRST, LAST).klass == cib.KLASS_OK
    assert s2.reads == 0 and s2.memo_hits == 1          # warm result across jobs: the .npz memo

    # a different window over the same files needs no read either (the table is per file, not per window)
    assert s2.check("AAA", "5min", date(2023, 6, 1), LAST).klass == cib.KLASS_OK
    assert s2.reads == 0

    _write(cache_folder, "AAA", daily, scale(intra, 2.0))      # the file is rewritten on another basis
    s3 = cib.BasisStore("FMPOHLCVProvider", memo=memo, folder=str(cache_folder))
    assert s3.check("AAA", "5min", FIRST, LAST).klass == cib.KLASS_CONSTANT_FACTOR
    assert s3.reads == 1 and s3.memo_hits == 0          # identity changed: rescanned, never a stale verdict


def test_identity_is_size_and_mtime(cache_folder, pair):
    daily, intra = pair
    _write(cache_folder, "BBB", daily, intra)
    path = str(cache_folder / "BBB_5min.parquet")
    a = cib._identity(path)
    os.utime(path, ns=(a[1] + 10_000_000_000, a[1] + 10_000_000_000))
    b = cib._identity(path)
    assert a != b and a[0] == b[0]


def test_missing_files_are_classes_not_errors(cache_folder, pair):
    daily, intra = pair
    daily.assign(effective_date=daily["Date"]).to_parquet(cache_folder / "ONLYD_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(cache_folder / "ONLYI_5min.parquet", index=False)
    st = cib.BasisStore("FMPOHLCVProvider", memo=None, folder=str(cache_folder))
    assert st.check("ONLYD", "5min", FIRST, LAST).klass == cib.KLASS_NO_INTRADAY
    assert st.check("ONLYI", "5min", FIRST, LAST).klass == cib.KLASS_NO_DAILY
    assert st.check("NOSUCH", "5min", FIRST, LAST).klass == cib.KLASS_NO_DAILY


def test_check_many_keeps_input_order(cache_folder, pair):
    daily, intra = pair
    _write(cache_folder, "OKS", daily, intra)
    _write(cache_folder, "BAD", daily, scale(intra, 0.5))
    st = cib.BasisStore("FMPOHLCVProvider", memo=None, folder=str(cache_folder))
    out = cib.check_many(["BAD", "OKS", "GONE"], "5min", FIRST, LAST, store=st)
    assert [r.symbol for r in out] == ["BAD", "OKS", "GONE"]
    assert [r.klass for r in out] == [cib.KLASS_CONSTANT_FACTOR, cib.KLASS_OK, cib.KLASS_NO_DAILY]


# --------------------------------------------------------------------------- review round: bursts, windows, calendar, aliases
def _scale_days(intra, factor, days, fields=("Open", "High", "Low", "Close")):
    out = intra.copy()
    m = out["Date"].dt.normalize().isin([pd.Timestamp(d) for d in days])
    for c in fields:
        out.loc[m, c] = out.loc[m, c] * factor
    return out


def test_a_short_basis_burst_is_flagged_with_its_dates(pair):
    """TRVG (2023-11-07..15, 7 sessions at x0.20): fewer than the 10 sessions a level needs, but unmistakable: the
    WHOLE session (high and low too) is on another basis."""
    daily, intra = pair
    days = [d for d, _ in _session_list(date(2023, 6, 5), date(2023, 6, 13))]
    res, _ = judge(daily, _scale_days(intra, 0.2, days))
    assert res.klass == cib.KLASS_FACTOR_CHANGES, res.reason
    assert [s.kind for s in res.segments] == ["burst_basis"]
    assert res.segments[0].first_day == "2023-06-05" and res.segments[0].sessions == 7
    assert res.segments[0].factor == pytest.approx(0.2, rel=0.02)
    assert "another basis" in res.reason


def test_a_burst_of_single_bad_bars_is_flagged_but_marked_not_a_basis(pair):
    """FGN / TCPA / the preferreds of 2026-03: the session LOW and the last close are at x0.3 while the session
    HIGH is at the daily level: bad bars, not a basis (the rebase tool refuses it)."""
    daily, intra = pair
    out = intra.copy()
    for d in [d for d, _ in _session_list(date(2023, 6, 5), date(2023, 6, 13))]:
        m = out["Date"].dt.normalize() == pd.Timestamp(d)
        last = out[m].index[-1]
        for c in ("Open", "Low", "Close"):
            out.loc[last, c] = out.loc[last, c] * 0.3
    res, _ = judge(daily, out)
    assert res.klass == cib.KLASS_FACTOR_CHANGES
    assert [s.kind for s in res.segments] == ["burst_prints"] and "single bad bars" in res.reason


def test_alternating_burst_sessions_still_form_a_burst(pair):
    daily, intra = pair
    sess = [d for d, _ in _session_list(date(2023, 6, 5), date(2023, 6, 20))]
    res, _ = judge(daily, _scale_days(intra, 0.26, sess[0::2][:6]))          # every other session for 11 sessions
    assert res.klass == cib.KLASS_FACTOR_CHANGES and res.segments[0].sessions >= 9


def test_two_wrong_sessions_or_a_one_off_print_are_not_a_burst(pair):
    daily, intra = pair
    sess = [d for d, _ in _session_list(date(2023, 6, 5), date(2023, 6, 30))]
    assert judge(daily, _scale_days(intra, 0.2, sess[:2]))[0].klass == cib.KLASS_OK       # < 3 sessions
    assert judge(daily, _scale_days(intra, 0.2, [sess[0], sess[8], sess[14]]))[0].klass == cib.KLASS_OK   # too far apart
    assert judge(daily, _scale_days(intra, 1.1, sess[:8]))[0].klass == cib.KLASS_OK       # 10% is not a burst level


def test_the_judged_window_covers_warmup_and_the_read_guard_window():
    lo, hi = cib.judged_window("2023-06-01", "2023-06-30", 30)
    assert lo == pd.Timestamp("2023-06-30") - pd.Timedelta(days=cib.MIN_JUDGED_DAYS)      # 90d beats 30d warmup
    lo, hi = cib.judged_window("2023-06-01", "2023-12-30", 365)
    assert lo == pd.Timestamp("2022-06-01")                                                # warmup beats 90d
    from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
    assert MarketDataProviderInterface.INTRADAY_READ_GUARD_DAYS == cib.MIN_JUDGED_DAYS    # one number, two homes


def test_a_session_past_the_calendar_table_fails_loudly(pair, monkeypatch):
    daily, intra = pair
    monkeypatch.setattr(cib, "_CAL", None)
    monkeypatch.setattr(cib, "_CAL_LAST", date(2023, 6, 30))
    with pytest.raises(cib.CalendarRangeExceeded, match="outside the NYSE calendar table"):
        cib.reduce_sessions(daily, intra, "5min")
    monkeypatch.setattr(cib, "_CAL", None)                      # leave the real table for the next test


def test_a_shadowing_alias_stub_is_named_in_the_reason(cache_folder, tmp_path, pair):
    """DGNX / MODD / MTEK: ``<SYM>_5m.parquet`` (a June-2026 stub) shadows the real ``<SYM>_5min.parquet``; readers
    open the first spelling only, so the check reports what the ENGINE would read, and says why."""
    daily, intra = pair
    _write(cache_folder, "SHD", daily, intra)
    stub = intra[intra["Date"] >= "2023-12-20"]
    stub.assign(effective_date=stub["Date"]).to_parquet(cache_folder / "SHD_5m.parquet", index=False)
    st = cib.BasisStore("FMPOHLCVProvider", memo=None, folder=str(cache_folder))
    res = st.check("SHD", "5min", FIRST, LAST)
    assert res.klass == cib.KLASS_INSUFFICIENT and "SHD_5m.parquet shadows SHD_5min.parquet" in res.reason
