"""Walk-forward geometry (ml_walkforward): blocks, windows, purge, validation split, regimes."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_walkforward as W


def test_quarter_and_half_block_counts_for_the_exploration_range():
    q = W.blocks("2019-01-01", "2026-10-12", "Q")
    h = W.blocks("2019-01-01", "2026-10-12", "H")
    assert len(q) == 31 and len(h) == 16
    assert q[0].name == "2019Q1" and q[0].start == date(2019, 1, 1) and q[0].end == date(2019, 3, 31)
    assert h[0].name == "2019H1" and h[0].end == date(2019, 6, 30)


def test_blocks_are_contiguous_and_cover_the_range_exactly():
    for freq in ("Q", "H"):
        bl = W.blocks("2019-01-01", "2026-10-12", freq)
        assert bl[0].start == date(2019, 1, 1) and bl[-1].end == date(2026, 10, 12)
        for a, b in zip(bl, bl[1:]):
            assert b.start == a.end_exclusive


def test_short_tail_merges_into_the_previous_block():
    q = W.blocks("2019-01-01", "2026-10-12", "Q")
    assert q[-1].name == "2026Q3" and q[-1].end == date(2026, 10, 12)            # Oct 1..12 joined Q3
    # a tail of exactly half a period or more stays
    q2 = W.blocks("2019-01-01", "2019-05-20", "Q")                                # Apr 1..May 20 = 50 of 91 days
    assert [b.name for b in q2] == ["2019Q1", "2019Q2"] and q2[-1].end == date(2019, 5, 20)
    q3 = W.blocks("2019-01-01", "2019-05-10", "Q")                                # Apr 1..May 10 = 40 days < 45.5
    assert [b.name for b in q3] == ["2019Q1"] and q3[0].end == date(2019, 5, 10)
    # the half-year tail 2026-07-01..10-12 (104 of 184 days) is its own block
    h = W.blocks("2019-01-01", "2026-10-12", "H")
    assert h[-1].name == "2026H2" and h[-1].start == date(2026, 7, 1)


def test_full_final_period_is_not_merged_and_single_block_is_kept():
    q = W.blocks("2019-01-01", "2019-12-31", "Q")
    assert len(q) == 4 and q[-1].end == date(2019, 12, 31)
    one = W.blocks("2019-01-01", "2019-01-10", "Q")                                # nothing to merge into
    assert len(one) == 1 and one[0].end == date(2019, 1, 10)


def test_blocks_refuse_bad_input():
    with pytest.raises(ValueError):
        W.blocks("2019-01-01", "2018-01-01", "Q")
    with pytest.raises(ValueError):
        W.blocks("2019-01-01", "2020-01-01", "M")


def test_fixed_windows_end_at_the_block_start():
    b = W.blocks("2019-01-01", "2026-10-12", "Q")[5]                              # 2020Q2
    assert b.start == date(2020, 4, 1)
    assert W.train_window(b, 12) == (date(2019, 4, 1), date(2020, 4, 1))
    assert W.train_window(b, 24) == (date(2018, 4, 1), date(2020, 4, 1))
    assert W.train_window(b, 36) == (date(2017, 4, 1), date(2020, 4, 1))


def test_expanding_window_starts_at_data_start_and_fixed_is_clipped_to_it():
    first = W.blocks("2019-01-01", "2026-10-12", "H")[0]
    assert W.train_window(first, "expanding", "2016-01-04") == (date(2016, 1, 4), date(2019, 1, 1))
    assert W.train_window(first, 36, "2016-01-04") == (date(2016, 1, 4), date(2019, 1, 1))      # clipped
    assert W.train_window(first, 12, "2016-01-04")[0] == date(2018, 1, 1)
    with pytest.raises(ValueError):
        W.train_window(first, "expanding")
    with pytest.raises(ValueError):
        W.train_window(first, 18)
    with pytest.raises(ValueError):
        W.train_window(first, True)


def test_purge_drops_rows_whose_target_ends_at_or_after_the_block_start():
    sess = pd.to_datetime(["2019-12-20", "2019-12-26", "2019-12-30", "2019-12-31", "2019-12-27"])
    tgt = pd.to_datetime(["2019-12-26", "2020-01-02", "2019-12-31", "2020-01-01", None])
    keep = W.purge(sess, tgt, "2020-01-01")
    #   ends 12-26 keep | ends 01-02 drop | ends 12-31 keep | ends exactly 01-01 drop | no target drop
    assert keep.tolist() == [True, False, True, False, False]


def test_purge_matches_a_horizon_gap_on_a_toy_table():
    sessions = pd.bdate_range("2019-11-01", "2019-12-31")
    H = 5
    tgt = pd.Series(sessions).shift(-H)                                           # target ends H sessions later
    keep = W.purge(sessions, tgt, "2020-01-01")
    last_kept = sessions[keep][-1]
    assert last_kept == sessions[len(sessions) - 1 - H]                           # H rows before the end are dropped
    assert keep.sum() == len(sessions) - H


def test_purge_refuses_a_target_before_its_session_and_length_mismatch():
    s = pd.to_datetime(["2019-12-20"])
    with pytest.raises(ValueError, match="before its own session"):
        W.purge(s, pd.to_datetime(["2019-12-19"]), "2020-01-01")
    with pytest.raises(ValueError):
        W.purge(s, pd.to_datetime(["2019-12-21", "2019-12-22"]), "2020-01-01")


def _toy(n_sess=100, per=3, H=5):
    sessions = pd.bdate_range("2019-01-01", periods=n_sess)
    s = np.repeat(sessions.values, per)
    t = pd.Series(sessions).shift(-H).reindex(range(n_sess)).values
    tgt = np.repeat(t, per)
    return s, tgt, sessions


def test_validation_split_takes_the_last_share_of_sessions_and_purges_the_fit_rows():
    s, tgt, sessions = _toy(100, 3, 5)
    train = W.purge(s, tgt, sessions[90])                       # block starts at session 90: last 5 sessions drop
    fit, val = W.validation_split(train, s, tgt, share=0.15)
    train_sessions = np.unique(s[train])
    n_val = len(train_sessions) - int(len(train_sessions) * 0.85)
    assert len(np.unique(s[val])) == n_val
    vstart = s[val].min()
    assert vstart == train_sessions[int(len(train_sessions) * 0.85)]
    assert not (fit & val).any() and (fit | val).sum() < train.sum()
    assert (s[fit] < vstart).all()
    assert (tgt[fit] < vstart).all()                            # purged against the validation block
    # the sessions between the last fit target and the validation start are the dropped gap (H sessions)
    assert len(np.unique(s[train & ~fit & ~val])) == 5


def test_validation_split_refuses_degenerate_inputs():
    s, tgt, _ = _toy(10, 1, 2)
    with pytest.raises(ValueError):
        W.validation_split(np.zeros(len(s), bool), s, tgt)
    with pytest.raises(ValueError):
        W.validation_split(np.ones(len(s), bool), s, tgt, share=1.0)
    with pytest.raises(ValueError, match="same length"):
        W.validation_split(np.ones(3, bool), s, tgt)


@pytest.mark.parametrize("d,name", [
    ("2018-12-31", "pre2019"), ("2016-01-04", "pre2019"),
    ("2019-01-01", "2019"), ("2019-12-31", "2019"), ("2020-01-02", "2019"), ("2020-02-18", "2019"),
    ("2020-02-19", "2020crash"), ("2020-06-30", "2020crash"),
    ("2020-07-01", "2021"), ("2021-12-31", "2021"),
    ("2022-01-03", "2022bear"), ("2022-12-30", "2022bear"),
    ("2023-01-03", "2023-24"), ("2024-12-31", "2023-24"),
    ("2025-01-02", "2025-26"), ("2026-10-12", "2025-26"),
])
def test_regime_boundaries(d, name):
    assert W.regime(d) == name
    assert W.regime(pd.Timestamp(d)) == name
    assert W.regime(date.fromisoformat(d)) == name


def test_vectorised_regimes_agree_with_the_scalar_function():
    days = pd.bdate_range("2016-01-04", "2026-10-12")
    v = W.regimes(days)
    assert list(v) == [W.regime(d) for d in days]
    assert set(v) == set(W.REGIMES)
    assert list(W.regimes(pd.Series(days[:3]))) == ["pre2019"] * 3
