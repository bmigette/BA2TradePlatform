"""Track metrics (ml_eval) on hand-made frames whose answers are known."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services import ml_eval as E

SESSIONS = pd.bdate_range("2021-01-04", periods=2100)


def _panel(n_names=10, n_sess=120, seed=0, vol=0.02):
    rng = np.random.default_rng(seed)
    rows = []
    for s in SESSIONS[:n_sess]:
        for i in range(n_names):
            rows.append((f"S{i:02d}", s, rng.normal()))
    df = pd.DataFrame(rows, columns=["symbol", "session", "latent"])
    df["vol"] = vol
    return df


def _with_ret(df, ret):
    out = df.copy()
    out["ret"] = ret
    out["rel_ret"] = out["ret"] - out.groupby("session")["ret"].transform("mean")
    return out


# ------------------------------------------------------------------ rank IC
def test_rank_ic_perfect_reversed_and_random():
    df = _panel()
    df = _with_ret(df, df["latent"] * 0.01)
    assert E.rank_ic(df.assign(score=df["rel_ret"]))[0] == pytest.approx(1.0)
    assert E.rank_ic(df.assign(score=-df["rel_ret"]))[0] == pytest.approx(-1.0)
    rnd = df.assign(score=np.random.default_rng(101).normal(size=len(df)))
    m, s = E.rank_ic(rnd)
    assert abs(m) < 0.09 and len(s) == 120 and s.index.is_monotonic_increasing


def test_rank_ic_matches_scipy_spearman_per_session():
    from scipy.stats import spearmanr
    df = _panel(seed=1)
    df = _with_ret(df, df["latent"] * 0.01 + np.random.default_rng(2).normal(0, 0.01, len(df)))
    df["score"] = df["latent"] + np.random.default_rng(3).normal(0, 1, len(df))
    _, s = E.rank_ic(df)
    for sess in SESSIONS[:5]:
        g = df[df["session"] == sess]
        assert s[sess] == pytest.approx(spearmanr(g["score"], g["rel_ret"]).correlation)


def test_rank_ic_skips_small_constant_and_nan_sessions_and_accepts_index_levels():
    df = _panel(n_names=10, n_sess=4)
    df = _with_ret(df, df["latent"] * 0.01).assign(score=lambda d: d["rel_ret"])
    df = df[~((df["session"] == SESSIONS[0]) & (df["symbol"] > "S02"))]            # 3 names left on day 0
    df.loc[df["session"] == SESSIONS[1], "score"] = 1.0                            # constant score on day 1
    df.loc[(df["session"] == SESSIONS[2]) & (df["symbol"] < "S05"), "score"] = np.nan   # 5 left on day 2
    m, s = E.rank_ic(df.set_index(["symbol", "session"]))
    assert list(s.index) == [SESSIONS[2], SESSIONS[3]] and m == pytest.approx(1.0)
    m2, s2 = E.rank_ic(df.iloc[:0])
    assert math.isnan(m2) and s2.empty


def test_flat_refuses_duplicates_and_missing_columns():
    df = _panel(n_sess=2).assign(score=1.0, rel_ret=0.0)
    with pytest.raises(ValueError, match="duplicated"):
        E.rank_ic(pd.concat([df, df.iloc[:1]]))
    with pytest.raises(ValueError, match="lacks"):
        E.rank_ic(df.drop(columns="rel_ret"))


# ------------------------------------------------------------------ quintile spread
def _known_day():
    # one session, 10 names, score = rank, rel_ret hand-set
    ret = np.array([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5], float) * 0.01
    df = pd.DataFrame({"symbol": [f"S{i}" for i in range(10)], "session": SESSIONS[0], "score": np.arange(10.0),
                       "ret": ret, "vol": 0.02})
    df["rel_ret"] = df["ret"] - df["ret"].mean()
    return df


def test_perfect_score_gives_the_maximum_spread_hand_computed():
    df = _known_day()
    sp = E.quintile_spread(df)
    # top 2 names: rel 0.04, 0.05 -> mean 0.045 ; bottom 2: -0.05, -0.04 -> -0.045 ; /0.02 each
    assert sp.mean == pytest.approx((0.045 - (-0.045)) / 0.02)
    # long-only: top mean minus the universe mean (0 since rel_ret is demeaned)
    assert sp.long_mean == pytest.approx(0.045 / 0.02)
    # every other assignment of the scores is no better
    rng = np.random.default_rng(0)
    for _ in range(50):
        other = df.assign(score=rng.permutation(df["score"].to_numpy()))
        assert E.quintile_spread(other).mean <= sp.mean + 1e-12


def test_cost_lowers_the_spread_by_exactly_two_costs_over_vol_and_long_only_by_one():
    df = _known_day()
    s0, s1 = E.quintile_spread(df, 0.0), E.quintile_spread(df, 25.0)
    assert s0.mean - s1.mean == pytest.approx(2 * 0.0025 / 0.02)
    assert s0.long_mean - s1.long_mean == pytest.approx(0.0025 / 0.02)
    big = _panel()
    big = _with_ret(big, big["latent"] * 0.01).assign(score=lambda d: d["latent"])
    diff = E.quintile_spread(big, 0.0).series - E.quintile_spread(big, 10.0).series
    assert np.allclose(diff, 2 * 0.001 / 0.02)


def test_random_score_spread_is_about_zero_and_perfect_is_large():
    df = _panel(n_names=20)
    df = _with_ret(df, df["latent"] * 0.01)
    rnd = df.assign(score=np.random.default_rng(1).normal(size=len(df)))
    sp = E.quintile_spread(rnd)
    se = sp.series.std() / math.sqrt(len(sp.series))
    assert abs(sp.mean) < 4 * se
    assert E.quintile_spread(df.assign(score=df["latent"])).mean > 10 * abs(sp.mean)


def test_spread_skips_small_sessions_and_unpacks_as_mean_series():
    df = _known_day()
    small = df.iloc[:4]
    assert E.quintile_spread(small).series.empty
    m, s = E.quintile_spread(df)
    assert m == pytest.approx(s.iloc[0]) and len(s) == 1
    with pytest.raises(ValueError):
        E.quintile_spread(df, cost_bps=-1)
    with pytest.raises(ValueError, match="vol"):
        E.quintile_spread(df.assign(vol=0.0))


def test_spread_is_order_and_tie_deterministic():
    df = _known_day().assign(score=1.0)                           # all tied
    a = E.quintile_spread(df).mean
    b = E.quintile_spread(df.sample(frac=1.0, random_state=3)).mean
    assert a == b


# ------------------------------------------------------------------ timing
def _single(n=100, seed=0, signal=True):
    rng = np.random.default_rng(seed)
    score = rng.normal(size=n)
    ret = (0.01 * np.sign(score) if signal else 0.0) + rng.normal(0, 0.005, n) + 0.001
    return pd.DataFrame({"symbol": "X", "session": SESSIONS[:n], "score": score, "ret": ret, "vol": 0.1})


def test_timing_excess_hand_computed_with_cost():
    sc = np.tile([0.0, 1.0], 10)
    df = pd.DataFrame({"symbol": "X", "session": SESSIONS[:20], "score": sc,
                       "ret": np.where(sc == 1.0, 0.01, -0.01), "vol": 0.1})
    t = E.timing_excess(df, 0.5)
    assert t.excess == pytest.approx(0.1) and t.n_in == 10 and t.n == 20 and t.in_share == 0.5
    t2 = E.timing_excess(df, 0.5, cost_bps=10)
    assert t2.excess == pytest.approx((0.01 - 0.001) / 0.1)       # one round trip per in row, in vol units
    assert t2.series.mean() == pytest.approx(0.5 * (0.01 - 0.001) / 0.1)


def test_timing_excess_positive_for_signal_zero_for_noise():
    assert E.timing_excess(_single(300, signal=True), 0.0).excess > 0.05
    noise = E.timing_excess(_single(2000, signal=False), 0.0)
    assert abs(noise.excess) < 0.05
    assert math.isnan(E.timing_excess(_single(10), 99.0).excess)         # never in


def test_timing_sharpe_matches_a_hand_computation():
    df = _single(200, seed=4)
    thr = 0.3
    inn = df["score"].to_numpy() >= thr
    r = np.where(inn, df["ret"].to_numpy() - 0.0005, 0.0)
    expect = r.mean() / r.std(ddof=1) * math.sqrt(252 / 5)
    assert E.timing_sharpe(df, thr, cost_bps=5, horizon=5) == pytest.approx(expect)
    assert E.timing_sharpe(df, thr, horizon=5) > E.timing_sharpe(df, thr, cost_bps=300, horizon=5)
    assert math.isnan(E.timing_sharpe(df, 99.0))
    with pytest.raises(ValueError):
        E.timing_sharpe(df, thr, horizon=0)


def test_timing_per_row_threshold_follows_the_callers_row_order():
    df = _panel(n_names=3, n_sess=30).assign(score=lambda d: d["latent"], ret=lambda d: d["latent"] * 0.01)
    cut = np.random.default_rng(0).normal(size=len(df))
    shuffled_idx = np.random.default_rng(1).permutation(len(df))
    a = E.timing_excess(df, cut, 5).excess
    b = E.timing_excess(df.iloc[shuffled_idx], cut[shuffled_idx], 5).excess
    assert a == pytest.approx(b)
    with pytest.raises(ValueError):
        E.timing_excess(df, cut[:-1])


def test_expanding_cutoff_uses_only_earlier_rows():
    s = pd.Series(np.random.default_rng(0).normal(size=100))
    c = E.expanding_cutoff(s, 0.6, min_obs=20)
    assert c.iloc[:20].isna().all() and c.iloc[20:].notna().all()
    assert c.iloc[50] == pytest.approx(np.quantile(s.iloc[:50], 0.6))
    s2 = s.copy()
    s2.iloc[60:] += 100.0                                               # change the future only
    c2 = E.expanding_cutoff(s2, 0.6, min_obs=20)
    assert c.iloc[:61].equals(c2.iloc[:61])                             # row 60's cutoff cannot see row 60
    with pytest.raises(ValueError):
        E.expanding_cutoff(s, 1.0)


def test_timing_excludes_rows_without_a_cutoff():
    df = _single(60)
    cut = E.expanding_cutoff(df["score"], 0.6, min_obs=20)
    t = E.timing_excess(df, cut.to_numpy())
    assert t.n == 40


def test_timing_by_symbol_splits_symbols():
    a, b = _single(100, 1), _single(100, 2)
    b["symbol"] = "Y"
    out = E.timing_by_symbol(pd.concat([a, b]), 0.0, 0)
    assert list(out.index) == ["X", "Y"]
    assert out.loc["X", "excess"] == pytest.approx(E.timing_excess(a, 0.0).excess)


# ------------------------------------------------------------------ Newey-West
def test_newey_west_matches_a_hand_computed_value():
    x = [1.0, 2.0, 4.0, 3.0, 5.0]       # mean 3, deviations -2 -1 1 0 2: g0 = 2, g1 = 0.2, g2 = 0
    assert E.newey_west_se(x, 0) == pytest.approx(math.sqrt(2.0 / 5))
    lrv1 = 2.0 + 2 * 0.5 * 0.2
    assert E.newey_west_se(x, 1) == pytest.approx(math.sqrt(lrv1 / 5))
    assert E.newey_west_t(x, 1) == pytest.approx(3.0 / math.sqrt(lrv1 / 5))
    lrv2 = 2.0 + 2 * ((2 / 3) * 0.2 + (1 / 3) * 0.0)
    assert E.newey_west_t(x, 2) == pytest.approx(3.0 / math.sqrt(lrv2 / 5))


def test_newey_west_se_exceeds_the_naive_se_on_ar1_and_matches_it_on_iid():
    rng = np.random.default_rng(7)
    n, phi = 4000, 0.7
    e = rng.normal(size=n)
    x = np.empty(n)
    x[0] = e[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    naive = x.std(ddof=0) / math.sqrt(n)
    nw = E.newey_west_se(x, 20)
    assert nw > 1.6 * naive                                             # theory: sqrt((1+phi)/(1-phi)) = 2.38
    assert abs(E.newey_west_t(x, 20)) < abs(x.mean() / naive)
    iid = rng.normal(size=n)
    assert 0.85 < E.newey_west_se(iid, 5) / (iid.std(ddof=0) / math.sqrt(n)) < 1.2


def test_newey_west_drops_nan_and_refuses_short_or_bad_input():
    assert E.newey_west_t([1, 2, np.nan, 4, 3, 5], 1) == pytest.approx(E.newey_west_t([1, 2, 4, 3, 5], 1))
    with pytest.raises(ValueError, match="too short"):
        E.newey_west_t([1.0, 2.0], 1)
    with pytest.raises(ValueError):
        E.newey_west_t([1.0, 2.0, 3.0], -1)
    assert math.isnan(E.newey_west_t([2.0] * 10, 2))


# ------------------------------------------------------------------ regimes
def test_by_regime_on_a_series_and_a_frame():
    days = pd.to_datetime(["2018-06-01", "2019-06-03", "2020-03-02", "2020-08-03", "2022-03-01", "2023-03-01",
                           "2025-03-03", "2025-04-01"])
    s = pd.Series([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0], index=days)
    out = E.by_regime(s, lambda x: float(x.mean()))
    assert list(out) == ["all", "pre2019", "2019", "2020crash", "2021", "2022bear", "2023-24", "2025-26"]
    assert out["all"] == pytest.approx(s.mean()) and out["2025-26"] == pytest.approx(7.0) and out["2021"] == 3.0
    df = pd.DataFrame({"symbol": "X", "session": days, "v": s.values})
    out2 = E.by_regime(df, lambda d: float(d["v"].sum()))
    assert out2["2025-26"] == 14.0
    out3 = E.by_regime(df.set_index(["symbol", "session"]), lambda d: float(d["v"].sum()))
    assert out3 == out2
    only = E.by_regime(s[s.index > "2023-01-01"], lambda x: float(x.mean()))
    assert list(only) == ["all", "2023-24", "2025-26"]
    with pytest.raises(ValueError):
        E.by_regime(pd.DataFrame({"v": [1]}), len)


def test_by_regime_with_a_metric_fn_over_predictions():
    df = _panel(n_names=10, n_sess=120)
    df = _with_ret(df, df["latent"] * 0.01).assign(score=lambda d: d["rel_ret"])
    out = E.by_regime(df, E.rank_ic_metric())
    assert out["all"] == pytest.approx(1.0) and out["2021"] == pytest.approx(1.0)
