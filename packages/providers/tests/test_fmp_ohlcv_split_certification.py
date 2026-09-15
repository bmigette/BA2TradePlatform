"""Certification of the FMP daily OHLCV cache for the ``fmp-daily-split-adjusted-v1`` source
profile (design section 4: certify the provider's cache columns against split fixtures before
using them; never guess an adjustment).

The real-cache test reads the machine's actual cache READ-ONLY (``tests/conftest.py`` redirects
``CACHE_FOLDER`` to a temp dir for writes, so the real root is resolved from the environment the
same way ``ba2_common.config`` does). It checks the measured finding (2026-09-15) with ratio tolerances:

* AAPL 4:1 on 2020-08-31 -- Close[split]/Close[prev] = 129.04/124.81 = 1.0339; the pre-split
  close 124.81 is the raw 499.23 divided by 4 (so split-adjusted, NOT dividend-adjusted);
* NVDA 10:1 on 2024-06-10 -- Close ratio 121.79/120.89 = 1.0074;
* intrabar O/C, H/C, L/C ratios move by < 3% across either split;
=> both ``split-adjusted``, report ``consistent=True``.
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_source import (
    BASIS_MIXED,
    BASIS_SPLIT_ADJUSTED,
    BASIS_UNADJUSTED,
    BASIS_UNAVAILABLE,
    CERTIFICATION_SPLITS,
    SOURCE_PROFILE_FMP_DAILY,
    SplitFixture,
    certify_source_columns,
    certify_split,
)


def _real_cache_root() -> str:
    if os.environ.get("CACHE_FOLDER"):
        return os.environ["CACHE_FOLDER"]
    home = os.path.abspath(os.getenv("BA2_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ba2")))
    return os.path.join(home, "common", "cache")


def _has_real_cache(root: str) -> bool:
    return all(os.path.exists(os.path.join(root, "FMPOHLCVProvider", f"{fx.symbol}_1d.parquet"))
               for fx in CERTIFICATION_SPLITS)


def test_real_fmp_cache_is_split_adjusted():
    root = _real_cache_root()
    if not _has_real_cache(root):
        pytest.skip(f"FMP daily cache for {[fx.symbol for fx in CERTIFICATION_SPLITS]} not present under {root}")
    report = certify_source_columns(root)
    assert report.source_profile == SOURCE_PROFILE_FMP_DAILY
    by = report.by_symbol()

    from ba2_common.core.market_condition_source import read_fmp_daily_cache

    for fx in CERTIFICATION_SPLITS:
        cert = by[fx.symbol]
        assert cert.basis == BASIS_SPLIT_ADJUSTED and cert.consistent, cert
        # An ordinary trading day across the split, nowhere near 1/factor (0.25 / 0.10).
        assert 0.8 < cert.close_ratio < 1.25, cert
        assert all(0.8 < r < 1.25 for r in cert.column_ratios.values()), cert
        assert all(abs(j - 1.0) < 0.05 for j in cert.intrabar_jumps.values()), cert
        # Exact: the report's close ratio IS the file's Close[split] / Close[previous session].
        dates, _o, _h, _l, c, _v = read_fmp_daily_cache(
            os.path.join(root, "FMPOHLCVProvider", f"{fx.symbol}_1d.parquet"))
        i = int(np.flatnonzero(dates == np.datetime64(fx.split_date))[0])
        assert cert.close_ratio == c[i] / c[i - 1]
    assert report.consistent

    # Split-adjusted but NOT dividend-adjusted: AAPL's pre-split close times 4 is the raw
    # 2020-08-28 print (499.23) to the cent; a dividend-adjusted series sits ~3% lower.
    dates, _o, _h, _l, c, _v = read_fmp_daily_cache(os.path.join(root, "FMPOHLCVProvider", "AAPL_1d.parquet"))
    prev = c[int(np.flatnonzero(dates == np.datetime64("2020-08-28"))[0])]
    assert abs(prev * 4 - 499.23) < 0.03


def test_missing_cache_is_unavailable_and_inconsistent(tmp_path):
    report = certify_source_columns(str(tmp_path))
    assert not report.consistent
    assert {s.basis for s in report.symbols} == {BASIS_UNAVAILABLE}


FX = SplitFixture("SYN", date(2024, 6, 10), 10.0)


def _synthetic(adjust_close=True, adjust_hl=True, adjust_open=True, jump_near=False):
    days = regular_sessions_ending_at(date(2024, 6, 28), 60)
    rng = np.random.default_rng(7)
    c = 120.0 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    if jump_near:
        k = days.index(date(2024, 6, 5))
        c[k:] *= 0.1  # an unrelated split-sized move a few sessions earlier
    o = c * 1.001
    h = c * 1.01
    l = c * 0.99
    pre = np.array([d < FX.split_date for d in days])
    o, h, l, c = o.copy(), h.copy(), l.copy(), c.copy()
    if not adjust_open:
        o[pre] *= FX.factor
    if not adjust_hl:
        h[pre] *= FX.factor
        l[pre] *= FX.factor
    if not adjust_close:
        c[pre] *= FX.factor
    return np.array(days, dtype="datetime64[D]"), o, h, l, c


def test_detector_passes_a_consistent_series():
    cert = certify_split(*_synthetic(), FX)
    assert cert.basis == BASIS_SPLIT_ADJUSTED and cert.consistent


def test_detector_flags_a_fully_unadjusted_split():
    cert = certify_split(*_synthetic(adjust_close=False, adjust_hl=False, adjust_open=False), FX)
    assert cert.basis == BASIS_UNADJUSTED and not cert.consistent
    assert cert.close_ratio == pytest.approx(0.1, rel=0.1)


def test_detector_flags_mixed_bases():
    # Adjusted closes with unadjusted highs/lows: the H/C and L/C ratios jump by ~10x.
    cert = certify_split(*_synthetic(adjust_hl=False), FX)
    assert cert.basis == BASIS_MIXED and not cert.consistent
    assert cert.intrabar_jumps["High"] == pytest.approx(10.0, rel=0.05)


def test_detector_refuses_when_a_split_sized_move_is_nearby():
    cert = certify_split(*_synthetic(jump_near=True), FX)
    assert not cert.consistent


def test_detector_reports_a_missing_split_bar():
    d, o, h, l, c = _synthetic()
    keep = d != np.datetime64(FX.split_date)
    cert = certify_split(d[keep], o[keep], h[keep], l[keep], c[keep], FX)
    assert cert.basis == BASIS_UNAVAILABLE and not cert.consistent
