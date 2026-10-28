"""Shuffle controls (ml_controls): multisets preserved; a cross-sectional signal dies under the symbol shuffle and
survives the time shuffle, a timing signal the reverse."""
import numpy as np
import pandas as pd
import pytest

from app.services import ml_controls as C
from app.services import ml_eval as E

SESSIONS = pd.bdate_range("2021-01-04", periods=150)
N_SYM = 30


def _frame(kind: str, seed=0):
    rng = np.random.default_rng(seed)
    alpha = rng.normal(size=N_SYM)                     # persistent name-level effect
    market = rng.normal(size=len(SESSIONS))            # day-level (all names) effect
    rows = []
    for t, s in enumerate(SESSIONS):
        for i in range(N_SYM):
            if kind == "cross":
                score = alpha[i] + rng.normal(0, 0.05)
                ret = 0.01 * alpha[i] + rng.normal(0, 0.002)
            else:
                score = market[t] + rng.normal(0, 0.01)
                ret = 0.01 * market[t] + rng.normal(0, 0.002)
            rows.append((f"S{i:02d}", s, score, ret))
    df = pd.DataFrame(rows, columns=["symbol", "session", "score", "ret"])
    df["vol"] = 0.02
    df["rel_ret"] = df["ret"] - df.groupby("session")["ret"].transform("mean")
    return df


def test_shuffles_preserve_the_score_multisets_and_everything_else():
    df = _frame("cross")
    a = C.shuffle_symbols(df, 1)
    b = C.shuffle_time(df, 1)
    f = E.flat(df)
    keep = ["symbol", "session", "ret", "vol", "rel_ret"]
    for out in (a, b):
        assert list(out.columns) == list(df.columns) and len(out) == len(df)
        assert out[keep].equals(f[keep])
    for _, g in f.groupby("session"):
        ga = a.loc[g.index]
        assert np.array_equal(np.sort(ga["score"].to_numpy()), np.sort(g["score"].to_numpy()))
    for _, g in f.groupby("symbol"):
        gb = b.loc[g.index]
        assert np.array_equal(np.sort(gb["score"].to_numpy()), np.sort(g["score"].to_numpy()))
    assert not np.array_equal(a["score"], f["score"]) and not np.array_equal(b["score"], f["score"])


def test_shuffles_move_scores_only_along_their_own_axis():
    df = E.flat(_frame("cross"))
    a = C.shuffle_symbols(df, 2)
    b = C.shuffle_time(df, 2)
    # symbol shuffle: each session keeps its own mean (same multiset); time shuffle: each symbol keeps its own mean
    assert np.allclose(a.groupby("session")["score"].mean(), df.groupby("session")["score"].mean())
    assert np.allclose(b.groupby("symbol")["score"].mean(), df.groupby("symbol")["score"].mean())


def test_shuffles_are_deterministic_per_seed_and_independent_of_input_order():
    df = _frame("cross")
    assert C.shuffle_symbols(df, 3).equals(C.shuffle_symbols(df, 3))
    assert C.shuffle_time(df, 3).equals(C.shuffle_time(df.sample(frac=1.0, random_state=1), 3))
    assert not C.shuffle_time(df, 3).equals(C.shuffle_time(df, 4))
    before = df.copy()
    C.shuffle_symbols(df, 1)
    assert df.equals(before)                                            # input not modified


def test_cross_sectional_signal_survives_time_shuffle_and_dies_under_symbol_shuffle():
    df = _frame("cross")
    ic = E.rank_ic_metric()
    real = ic(df)
    assert real > 0.9
    assert ic(C.shuffle_time(df, 1)) > 0.8                              # a persistent name effect is time-invariant
    assert abs(ic(C.shuffle_symbols(df, 1))) < 0.1
    sp = E.spread_metric(0.0)
    assert sp(df) > 10 * abs(sp(C.shuffle_symbols(df, 1)))


def test_timing_signal_survives_symbol_shuffle_and_dies_under_time_shuffle():
    df = _frame("timing")
    tm = E.timing_metric(0.0, 0.0)
    real = tm(df)
    assert real > 0.3
    assert tm(C.shuffle_symbols(df, 1)) > 0.9 * real                    # day-level score is the same for every name
    assert abs(tm(C.shuffle_time(df, 1))) < 0.15 * real


def test_controls_report_layout_and_values():
    df = _frame("cross")
    outcomes = df[["symbol", "session", "ret", "rel_ret", "vol"]].set_index(["symbol", "session"])
    preds = df[["symbol", "session", "score"]]
    naive = preds.assign(score=-df["score"])                              # a deliberately bad baseline
    lin = preds.assign(score=df["score"] * 2)
    ic = E.rank_ic_metric()
    rep = C.controls_report(preds, outcomes, ic, seeds=(1, 2, 3), naive=naive, linear=lin)
    assert rep["model"] == pytest.approx(ic(df))
    assert rep["naive"] == pytest.approx(-rep["model"]) and rep["linear"] == pytest.approx(rep["model"])
    assert rep["seeds"] == [1, 2, 3]
    ss, st = rep["shuffle_symbols"], rep["shuffle_time"]
    assert len(ss["values"]) == 3 and ss["mean"] == pytest.approx(np.mean(ss["values"]))
    assert ss["std"] == pytest.approx(np.std(ss["values"], ddof=1))
    assert abs(ss["mean"]) < 0.1 < st["mean"]
    assert ss["values"][0] == pytest.approx(ic(C.shuffle_symbols(df, 1)))
    plain = C.controls_report(preds, outcomes, ic)
    assert "naive" not in plain and "linear" not in plain and plain["seeds"] == [1, 2, 3]


def test_controls_report_uses_the_same_rows_for_model_and_controls_and_refuses_empties():
    df = _frame("cross")
    outcomes = df[["symbol", "session", "ret", "rel_ret", "vol"]].copy()
    outcomes.loc[outcomes["session"] > SESSIONS[100], ["ret", "rel_ret"]] = np.nan      # no outcome at the tail
    seen = []

    def metric(d):
        seen.append(len(d))
        return 0.0
    C.controls_report(df[["symbol", "session", "score"]], outcomes, metric, seeds=(1,))
    assert len(set(seen)) == 1 and seen[0] == 101 * N_SYM
    with pytest.raises(ValueError, match="no row"):
        C.controls_report(df[["symbol", "session", "score"]], outcomes.assign(ret=np.nan, rel_ret=np.nan), metric)
    with pytest.raises(ValueError):
        C.controls_report(df[["symbol", "session", "score"]], outcomes, metric, seeds=())
    with pytest.raises(ValueError, match="none of"):
        C.controls_report(df[["symbol", "session", "score"]], df[["symbol", "session"]], metric)
