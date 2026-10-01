"""``gap_fill`` on the window-computing readers (market_condition_readers.py): the UNPINNED
(research-mode) live/BT compute path, which is the one place outside a pinned manifest where a
fill policy must be applied explicitly. When a manifest IS pinned, the reader never consults its
own ``gap_fill`` at all -- the manifest already carries whatever policy built it, which is the
project's one-shared-function BT/live parity rule applied to this feature.
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
from ba2_common.core.market_conditions import STATUS_MISSING_SESSION, STATUS_VALID, WINDOW

SESSION = date(2025, 6, 30)


def _frame(sessions, seed=7):
    rng = np.random.default_rng(seed)
    n = len(sessions)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * 1.004
    l = np.minimum(o, c) * 0.996
    v = rng.integers(1_000_000, 2_000_000, n).astype(float)
    return pd.DataFrame({"Date": pd.to_datetime(sessions), "Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": v})


def _write(root, symbol, df):
    folder = os.path.join(root, "FMPOHLCVProvider")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{symbol}_1d.parquet")
    tmp = path + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return path


def test_default_gap_fill_is_unchanged_research_mode_behaviour(tmp_path):
    days = regular_sessions_ending_at(SESSION, WINDOW + 10)
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    df = _frame(days)
    df = df[df["Date"] != pd.Timestamp(hole)]
    _write(tmp_path, "AAA", df)

    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path))
    row = reader.observe("AAA", SESSION)
    assert row.by_field()["underlying_adx_14"].status == STATUS_MISSING_SESSION
    assert reader.computed == 1


def test_gap_fill_previous_serves_a_valid_row_over_the_same_hole(tmp_path):
    days = regular_sessions_ending_at(SESSION, WINDOW + 10)
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    df = _frame(days)
    df = df[df["Date"] != pd.Timestamp(hole)]
    _write(tmp_path, "AAA", df)

    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path), gap_fill="previous")
    row = reader.observe("AAA", SESSION)
    assert all(o.status == STATUS_VALID for o in row.by_field().values())


def test_gap_fill_is_cached_per_symbol_not_recomputed_per_session(tmp_path):
    """Filling is O(the symbol's whole history); a reader walking many sessions of one symbol
    (a backtest) must pay that cost once, not once per session."""
    days = regular_sessions_ending_at(SESSION, WINDOW + 30)
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    df = _frame(days)
    df = df[df["Date"] != pd.Timestamp(hole)]
    _write(tmp_path, "AAA", df)

    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path), gap_fill="previous")
    sessions = regular_sessions_ending_at(SESSION, 5)
    for s in sessions:
        reader.observe("AAA", s)
    assert len(reader._fill_cache) == 1


def test_gap_fill_cache_invalidates_when_the_file_changes(tmp_path):
    days = regular_sessions_ending_at(SESSION, WINDOW + 10)
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    df = _frame(days)
    df_missing = df[df["Date"] != pd.Timestamp(hole)]
    _write(tmp_path, "AAA", df_missing)

    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path), gap_fill="previous")
    row1 = reader.observe("AAA", SESSION)
    assert all(o.status == STATUS_VALID for o in row1.by_field().values())

    _write(tmp_path, "AAA", df)  # the hole is repaired at the source
    row2 = reader.observe("AAA", SESSION)
    assert all(o.status == STATUS_VALID for o in row2.by_field().values())
    # Both rows are valid, but they must not be the SAME (stale) memo entry: the file changed.
    assert reader.computed == 2


def test_an_unknown_gap_fill_policy_is_refused_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown gap_fill policy"):
        FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path), gap_fill="next")


def test_a_reader_without_a_manifest_tolerates_a_missing_cache_file(tmp_path):
    reader = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path), gap_fill="previous")
    assert reader.observe("NOPE", SESSION) is None
