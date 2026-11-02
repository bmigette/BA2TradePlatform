"""The pure verdicts of ``ba2_common.core.ohlcv_topup_guard.verify_topup`` (APH, 2026-09-28).

The provider-level behaviour (full re-fetch, refusal, byte-identical append) is pinned in
``packages/providers/tests/test_ohlcv_topup_across_split.py``.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import ohlcv_topup_guard as g
from ba2_common.core.split_basis import CalendarSplit


def _bars(start: str, n: int, close0: float = 100.0, seed: int = 3) -> pd.DataFrame:
    days = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(seed)
    c = close0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Date": days, "Open": o, "High": np.maximum(o, c) * 1.01,
                         "Low": np.minimum(o, c) * 0.99, "Close": c,
                         "Volume": rng.integers(1_000_000, 2_000_000, n).astype(float)})


def _scaled(df: pd.DataFrame, k: float, before=None) -> pd.DataFrame:
    out = df.copy()
    rows = slice(None) if before is None else out["Date"] < pd.Timestamp(before)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[rows, col] = out.loc[rows, col] * k
    out.loc[rows, "Volume"] = out.loc[rows, "Volume"] / k
    return out


TRUTH = _bars("2026-01-02", 120)
LAST = 80                                   # the cache holds TRUTH[:LAST]
SPLIT = TRUTH["Date"].iloc[100].date()      # a 2:1 split among the new bars
CAL = [CalendarSplit(SPLIT, 2.0)]


def _vendor(frame: pd.DataFrame) -> pd.DataFrame:
    """The vendor's answer from the first of the last TOPUP_OVERLAP_BARS cached days on."""
    return frame.iloc[LAST - g.TOPUP_OVERLAP_BARS:].reset_index(drop=True)


def test_plain_topup_agrees():
    v = g.verify_topup(TRUTH.iloc[:LAST], _vendor(TRUTH.iloc[:LAST + 10]), [])
    assert v.verdict == g.VERDICT_AGREE and v.appendable and v.shared == g.TOPUP_OVERLAP_BARS
    assert v.provisional_days == ()


def test_tz_aware_vendor_dates_match_naive_cache():
    vendor = _vendor(TRUTH.iloc[:LAST + 3]).copy()
    vendor["Date"] = vendor["Date"].dt.tz_localize("UTC")
    assert g.verify_topup(TRUTH.iloc[:LAST], vendor, []).verdict == g.VERDICT_AGREE


def test_aph_shape_vendor_rebased_is_a_basis_change_confirmed_by_the_calendar():
    """Cached bars as traded before the split (x2), vendor bars adjusted: every overlap bar is the
    cached one x0.5 -> replace, never append."""
    cached = _scaled(TRUTH.iloc[:LAST], 2.0)
    v = g.verify_topup(cached, _vendor(TRUTH), CAL, symbol="APH")
    assert v.verdict == g.VERDICT_BASIS_CHANGE and v.needs_full_refetch and not v.appendable
    assert v.factor == pytest.approx(0.5, rel=1e-6)
    assert "calendar split(s)" in v.reason and SPLIT.isoformat() in v.reason


def test_a_split_like_rescale_without_a_calendar_event_is_still_a_basis_change():
    """CRWD shape: the vendor applied a 4:1 early; nothing on the calendar in the window yet."""
    cached = _scaled(TRUTH.iloc[:LAST], 4.0)
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]), [])
    assert v.verdict == g.VERDICT_BASIS_CHANGE and v.factor == pytest.approx(0.25)
    assert "split-like ratio 4:1" in v.reason


def test_a_cache_that_already_straddles_a_split_is_a_basis_change():
    """Older overlap bars stale (x2), newer ones agree: appended across a split earlier."""
    split_in_overlap = TRUTH["Date"].iloc[LAST - 2].date()
    cached = _scaled(TRUTH.iloc[:LAST], 2.0, before=split_in_overlap)
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]),
                       [CalendarSplit(split_in_overlap, 2.0)])
    assert v.verdict == g.VERDICT_BASIS_CHANGE and v.factor == pytest.approx(0.5)


def test_a_uniform_rescale_that_is_no_split_is_refused():
    cached = _scaled(TRUTH.iloc[:LAST], 1.07)
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]), [])
    assert v.verdict == g.VERDICT_UNEXPLAINED and not v.appendable and not v.needs_full_refetch


def test_a_non_uniform_mismatch_is_refused():
    cached = TRUTH.iloc[:LAST].copy()
    i = LAST - 3
    cached.loc[i, ["Open", "High", "Low", "Close"]] = cached.loc[i, ["Open", "High", "Low", "Close"]] * 0.5
    cached.loc[LAST - 1, "Close"] = cached.loc[LAST - 1, "Close"] * 1.2
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]), [])
    assert v.verdict == g.VERDICT_UNEXPLAINED
    assert TRUTH["Date"].iloc[i].date().isoformat() in v.reason


def test_a_provisional_snapshot_is_not_a_disagreement():
    """AAOI 2026-09-21 shape: the last cached bar was cached mid-session."""
    cached = TRUTH.iloc[:LAST].copy()
    last = cached.index[-1]
    o = cached.loc[last, "Open"]
    cached.loc[last, ["High", "Low", "Close"]] = [o, o, o]
    cached.loc[last, "Volume"] = cached.loc[last, "Volume"] * 0.05
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]), [])
    assert v.verdict == g.VERDICT_AGREE
    assert v.provisional_days == (TRUTH["Date"].iloc[LAST - 1].date(),)


def test_a_calendar_split_the_vendor_has_not_adjusted_yet_is_refused():
    """Vendor lag: pre-split bars still as traded (= the cache), then the as-traded x0.5 step."""
    vendor_view = _scaled(TRUTH, 2.0, before=SPLIT)
    cached = vendor_view.iloc[:LAST]
    v = g.verify_topup(cached, _vendor(vendor_view), CAL)
    assert v.verdict == g.VERDICT_VENDOR_UNADJUSTED and not v.appendable
    assert not v.needs_full_refetch and "has not adjusted" in v.reason


def test_a_small_calendar_split_crossed_with_an_agreeing_overlap_is_refused():
    """A 5% stock dividend cannot be told from an ordinary day, so an agreeing overlap proves the
    vendor did not adjust for it."""
    vendor_view = _scaled(TRUTH, 1.05, before=SPLIT)
    v = g.verify_topup(vendor_view.iloc[:LAST], _vendor(vendor_view), [CalendarSplit(SPLIT, 1.05)])
    assert v.verdict == g.VERDICT_VENDOR_UNADJUSTED and "too small" in v.reason


def test_a_calendar_split_crossed_without_a_step_is_a_full_refetch():
    """The vendor's bars are continuous across the ex-date and agree with the cache: the vendor is
    on one basis, and only a full re-fetch proves the older cached bars are too."""
    v = g.verify_topup(TRUTH.iloc[:LAST], _vendor(TRUTH), CAL)
    assert v.verdict == g.VERDICT_CALENDAR_SPLIT and v.needs_full_refetch


def test_a_split_sized_step_with_the_calendar_unreadable_is_refused():
    vendor_view = _scaled(TRUTH, 2.0, before=SPLIT)
    v = g.verify_topup(vendor_view.iloc[:LAST], _vendor(vendor_view), None, calendar_failed=True)
    assert v.verdict == g.VERDICT_UNVERIFIABLE_STEP and not v.appendable
    # ... but a provider WITHOUT a calendar (None, not failed) keeps the overlap-only verdict.
    assert g.verify_topup(vendor_view.iloc[:LAST], _vendor(vendor_view), None).verdict == g.VERDICT_AGREE


def test_no_shared_session_is_refused():
    v = g.verify_topup(TRUTH.iloc[:LAST], TRUTH.iloc[LAST:LAST + 5], [])
    assert v.verdict == g.VERDICT_NO_OVERLAP and not v.appendable


def test_verify_replacement():
    probe = _vendor(TRUTH.iloc[:LAST + 5])
    days = [d.date() for d in TRUTH["Date"].iloc[LAST - g.TOPUP_OVERLAP_BARS:LAST]]
    g.verify_replacement(probe, TRUTH, days)                      # reproduces it: fine
    with pytest.raises(g.OHLCVTopUpRefused, match="lacks the session"):
        g.verify_replacement(probe, TRUTH.drop(index=LAST - 2), days)
    with pytest.raises(g.OHLCVTopUpRefused, match="disagrees"):
        g.verify_replacement(probe, _scaled(TRUTH, 1.3), days)


def test_the_refusal_is_never_absorbed():
    from ba2_common.core.failure_modes import is_never_absorbed
    assert is_never_absorbed(g.OHLCVTopUpRefused("x"))


def test_split_like_ratio():
    assert g._split_like_ratio(0.5, g.SPLIT_RATIO_TOL) == "2:1"
    assert g._split_like_ratio(2 / 3, g.SPLIT_RATIO_TOL) == "3:2"
    assert g._split_like_ratio(1.5, g.SPLIT_RATIO_TOL) == "2:3"
    assert g._split_like_ratio(10.0, g.SPLIT_RATIO_TOL) == "1:10"
    assert g._split_like_ratio(1.07, g.SPLIT_RATIO_TOL) is None


def _snapshot_with_open_off(rel: float):
    """The last cached bar: a flat partial-day snapshot whose open is ``rel`` away from the vendor's."""
    cached = TRUTH.iloc[:LAST].copy()
    last = cached.index[-1]
    vendor_open = TRUTH.loc[last, "Open"]
    o = vendor_open * (1 + rel)
    lo, hi = TRUTH.loc[last, "Low"], TRUTH.loc[last, "High"]
    assert lo <= o <= hi, "fixture: the snapshot open must sit inside the vendor's range"
    cached.loc[last, ["Open", "High", "Low", "Close"]] = [o, o, o, o]
    cached.loc[last, "Volume"] = cached.loc[last, "Volume"] * 0.05
    return cached


@pytest.mark.parametrize("rel", [0.006, 0.007, 0.0097, -0.006, -0.0097])
def test_a_provisional_snapshot_whose_open_is_a_first_print_is_replaced_not_refused(rel):
    """MXL/VSXY/GKOS/SIMO 2026-10-01: the snapshot's open was 0.6-1.0% off the vendor's official open."""
    cached = _snapshot_with_open_off(rel)
    v = g.verify_topup(cached, _vendor(TRUTH.iloc[:LAST + 5]), [])
    assert v.verdict == g.VERDICT_AGREE
    assert v.provisional_days == (TRUTH["Date"].iloc[LAST - 1].date(),)


def test_an_open_beyond_the_provisional_tolerance_is_still_refused():
    last = TRUTH.iloc[LAST - 1]
    rel = 0.05
    cached = TRUTH.iloc[:LAST].copy()
    i = cached.index[-1]
    o = float(last.Open) * (1 + rel)
    cached.loc[i, ["Open", "High", "Low", "Close"]] = [o, o, o, o]
    vendor = TRUTH.iloc[:LAST + 5].copy()
    # widen the vendor's day range so the open is inside it: only the 5% open gap can refuse it
    vendor.loc[vendor.index[LAST - 1], "High"] = o * 1.02
    v = g.verify_topup(cached, _vendor(vendor), [])
    assert v.verdict == g.VERDICT_UNEXPLAINED


def test_a_relaxed_open_never_hides_a_split():
    """A 2:1 rebase of the last bars is still a basis change, not a provisional snapshot."""
    cached = TRUTH.iloc[:LAST].copy()
    v = g.verify_topup(cached, _vendor(_scaled(TRUTH.iloc[:LAST + 5], 0.5, before=SPLIT)), CAL)
    assert v.verdict != g.VERDICT_AGREE
