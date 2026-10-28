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
    tm = E.timing_metric(q=0.5, cost_bps=0.0, min_obs=20)       # the cutoff is recomputed from each shuffled score
    real = tm(df)
    assert real > 0.3
    assert tm(C.shuffle_symbols(df, 1)) > 0.8 * real                    # day-level score is the same for every name
    assert abs(tm(C.shuffle_time(df, 1))) < 0.25 * real


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
    assert "naive" not in plain and "linear" not in plain and plain["seeds"] == list(range(1, 21))


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


def test_controls_report_defaults_to_20_seeds_and_reports_rank_and_p_value():
    df = _frame("cross")
    outcomes = df[["symbol", "session", "ret", "rel_ret", "vol"]]
    rep = C.controls_report(df[["symbol", "session", "score"]], outcomes, E.rank_ic_metric())
    assert rep["seeds"] == list(range(1, 21))
    ss, st = rep["shuffle_symbols"], rep["shuffle_time"]
    assert len(ss["values"]) == 20 and len(st["values"]) == 20
    # the model beats every symbol shuffle: rank 1, p = 1 / 21 ; the time shuffle keeps a persistent name effect
    assert ss["rank"] == 1 and ss["p_value"] == pytest.approx(1 / 21)
    assert st["rank"] > 1 and st["p_value"] > 0.5
    # a metric the shuffles tie or beat gets the worst rank and p = 1
    rep2 = C.controls_report(df[["symbol", "session", "score"]], outcomes, lambda d: -1.0, seeds=(1, 2, 3))
    assert rep2["shuffle_symbols"]["rank"] == 1 and rep2["shuffle_symbols"]["p_value"] == pytest.approx(1.0)
    assert C.empirical_rank(0.5, [0.1, 0.9, 0.5, float("nan")]) == (2, pytest.approx(3 / 4))
    assert C.empirical_rank(float("nan"), [0.1]) == (None, None)


def test_controls_report_with_a_timing_metric_recomputes_the_cutoff_after_each_shuffle():
    df = _frame("timing")
    outcomes = df[["symbol", "session", "ret", "rel_ret", "vol"]]
    preds = df[["symbol", "session", "score"]]
    tm = E.timing_metric(q=0.5, cost_bps=0.0, min_obs=20)
    rep = C.controls_report(preds, outcomes, tm, seeds=(1, 2, 3, 4))
    assert rep["model"] > 0.3
    assert rep["shuffle_symbols"]["mean"] > 0.8 * rep["model"]          # day-level timing survives the cross shuffle
    assert abs(rep["shuffle_time"]["mean"]) < 0.25 * rep["model"]      # and dies under the time shuffle
    # each control value is the metric of the shuffled frame with its OWN causal cutoff
    joined = E.flat(df)
    manual = tm(C.shuffle_time(joined, 1))
    assert rep["shuffle_time"]["values"][0] == pytest.approx(manual)
    # a metric over a frame carrying a stale cutoff column would NOT see the shuffle: that is what q= avoids
    stale = E.with_cutoff(joined, 0.5, 20)
    assert E.timing_metric(cutoff_col="cutoff")(C.shuffle_time(stale, 1)) != pytest.approx(manual)


# ------------------------------------------------------------------ shuffle_time_within_block
def _regime_frame(seed=0, n_blocks=12, block_len=40, n_sym=10):
    """Scores that only know the REGIME: every score is its block's state plus a little noise, and the block's drift
    follows the state. A model like that has an "in" share that tracks the regime, not the day."""
    rng = np.random.default_rng(seed)
    sess = pd.bdate_range("2020-01-02", periods=n_blocks * block_len)
    state = rng.normal(size=n_blocks)
    rows = []
    for t, s in enumerate(sess):
        b = t // block_len
        for i in range(n_sym):
            rows.append((f"S{i:02d}", s, f"B{b:02d}", state[b] + rng.normal(0, 0.05),
                         0.004 * state[b] + rng.normal(0, 0.01)))
    df = pd.DataFrame(rows, columns=["symbol", "session", "block", "score", "ret"])
    df["vol"] = 0.02
    df["rel_ret"] = df["ret"] - df.groupby("session")["ret"].transform("mean")
    return df


def test_shuffle_time_within_block_permutes_each_symbols_scores_inside_its_block():
    df = E.flat(_regime_frame())
    out = C.shuffle_time_within_block(df, 1)
    keep = ["symbol", "session", "block", "ret", "vol", "rel_ret"]
    assert list(out.columns) == list(df.columns) and out[keep].equals(df[keep])
    for (_, _), g in df.groupby(["symbol", "block"]):
        assert np.array_equal(np.sort(out.loc[g.index, "score"].to_numpy()), np.sort(g["score"].to_numpy()))
    assert not np.array_equal(out["score"], df["score"])
    # deterministic per seed, independent of row order, different across seeds, input untouched
    assert out.equals(C.shuffle_time_within_block(df.sample(frac=1.0, random_state=5), 1))
    assert not out.equals(C.shuffle_time_within_block(df, 2))
    before = df.copy()
    C.shuffle_time_within_block(df, 1)
    assert df.equals(before)
    with pytest.raises(ValueError, match="block"):
        C.shuffle_time_within_block(df.drop(columns="block"), 1)


def test_within_block_shuffle_keeps_a_block_level_signal_that_the_full_time_shuffle_destroys():
    df = _regime_frame()
    tm = E.timing_metric(q=0.6, cost_bps=0.0, min_obs=20)
    real = tm(df)
    assert real > 0
    full = np.mean([tm(C.shuffle_time(df, s)) for s in range(1, 6)])
    within = np.mean([tm(C.shuffle_time_within_block(df, s)) for s in range(1, 6)])
    assert abs(full) < 0.3 * real                       # the unrestricted null kills a regime-only score ...
    assert within > 0.5 * real                          # ... the within-block null keeps most of it


def test_controls_report_adds_the_within_block_null_only_when_asked_and_it_penalises_a_regime_model():
    df = _regime_frame()
    outcomes = df[["symbol", "session", "ret", "vol"]]
    preds = df[["symbol", "session", "score", "block"]]
    tm = E.timing_metric(q=0.6, cost_bps=0.0, min_obs=20)
    plain = C.controls_report(preds, outcomes, tm, seeds=tuple(range(1, 21)))
    assert "shuffle_time_within_block" not in plain
    rep = C.controls_report(preds, outcomes, tm, seeds=tuple(range(1, 21)), within_block=True)
    assert rep["shuffle_time"]["p_value"] == pytest.approx(1 / 21)                 # looks significant against the full null
    wb = rep["shuffle_time_within_block"]
    assert len(wb["values"]) == 20 and wb["p_value"] > 0.2 and wb["mean"] > 0.5 * rep["model"]
    manual = tm(C.shuffle_time_within_block(E.flat(df), 1))
    assert wb["values"][0] == pytest.approx(manual)
    with pytest.raises(ValueError, match="block"):
        C.controls_report(preds.drop(columns="block"), outcomes, tm, seeds=(1,), within_block=True)
