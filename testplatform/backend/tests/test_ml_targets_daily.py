"""Daily-row targets for the ML exploration v2 (app/services/ml_targets.py, "daily rows" section)."""
from datetime import date

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.services import ml_targets as T
from app.services.ml_panel import true_range, wilder

N_SESS = 90
SYMS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
SESSIONS = pd.bdate_range("2024-01-02", periods=N_SESS)
HS = (1, 3, 5, 10, 20)


def make_bars(seed=0, n_sess=N_SESS):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n_sess)
    bars = {}
    for sym in SYMS:
        close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.015, n_sess)))
        open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.004, n_sess))
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n_sess)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n_sess)))
        bars[sym] = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1e6,
                                  "listed": True, "filled": False, "ended": False}, index=idx)
        bars[sym].index.name = "session"
    return bars


def at(df, sym, i, col):
    return df.loc[(sym, SESSIONS[i]), col]


@pytest.fixture(scope="module")
def bars():
    return make_bars()


@pytest.fixture(scope="module")
def tg(bars):
    return T.daily_targets(bars, HS)


# ------------------------------------------------------------------ returns
def test_index_and_columns(tg):
    assert tg.index.names == ["symbol", "session"]
    assert len(tg) == len(SYMS) * N_SESS
    assert list(tg.index.get_level_values("symbol").unique()) == SYMS
    for h in HS:
        for c in ("ret", "vol", "rel_ret", "quintile", "beats", "rank_rel", "realvol", "target_end", "up", "z"):
            assert f"{c}_{h}" in tg.columns
    assert "session_group" in tg.columns


def test_entry_is_next_open_and_exit_is_close_at_s_plus_h(bars, tg):
    d = bars["S2"]
    s = 30
    for h in HS:
        assert at(tg, "S2", s, f"ret_{h}") == pytest.approx(d["close"].iloc[s + h] / d["open"].iloc[s + 1] - 1.0)
    # not the old convention (close of S to close of S+H)
    assert at(tg, "S2", s, "ret_5") != pytest.approx(d["close"].iloc[s + 5] / d["close"].iloc[s] - 1.0)


def test_nan_past_the_data_end(tg):
    for h in HS:
        last_ok = N_SESS - 1 - h
        assert not np.isnan(at(tg, "S0", last_ok, f"ret_{h}"))
        assert np.isnan(at(tg, "S0", last_ok + 1, f"ret_{h}"))
        assert np.isnan(at(tg, "S0", N_SESS - 1, f"ret_{h}"))


def test_filled_entry_or_exit_session_is_nan():
    b = make_bars()
    b["S3"] = b["S3"].copy()
    s = 40
    b["S3"].iloc[s + 1, b["S3"].columns.get_loc("filled")] = True      # a flat, invented bar
    tg = T.daily_targets(b, HS)
    for h in HS:
        assert np.isnan(at(tg, "S3", s, f"ret_{h}")), h               # S+1 is the entry of every horizon
    assert np.isnan(at(tg, "S3", s + 1 - 3, "ret_3"))                  # ... and the exit of ret_3 three sessions earlier
    assert not np.isnan(at(tg, "S3", s + 1 - 4, "ret_3"))
    assert not np.isnan(at(tg, "S3", s + 1, "ret_3"))                  # a filled S itself is not the problem
    assert not np.isnan(at(tg, "S0", s, "ret_3"))                      # other symbols are untouched


@pytest.mark.parametrize("flag", ["ended", "unlisted"])
def test_ended_or_unlisted_tail_is_nan(flag):
    b = make_bars()
    d = b["S3"].copy()
    s = 40
    d.iloc[s + 5:, [d.columns.get_loc(c) for c in ("open", "high", "low", "close")]] = np.nan
    d.iloc[s + 5:, d.columns.get_loc("listed")] = False
    if flag == "ended":
        d.iloc[s + 5:, d.columns.get_loc("ended")] = True
    b["S3"] = d
    tg = T.daily_targets(b, HS)
    assert np.isnan(at(tg, "S3", s, "ret_5")) and np.isnan(at(tg, "S3", s, "ret_10"))     # S+5 is gone
    assert not np.isnan(at(tg, "S3", s, "ret_3"))                                          # S+3 still prints
    assert not np.isnan(at(tg, "S3", s + 3, "ret_1"))                                      # S+1 = s+4 prints
    assert np.isnan(at(tg, "S3", s + 4, "ret_1"))                                          # S+1 = s+5 is gone
    assert np.isnan(at(tg, "S3", s + 6, "vol_5"))                                          # no ATR on a dead row
    assert not np.isnan(at(tg, "S0", s + 6, "vol_5"))


def test_ended_row_s_plus_1_missing_but_ended_column_absent_is_allowed():
    b = make_bars()
    b = {k: v.drop(columns="ended") for k, v in b.items()}
    tg = T.daily_targets(b, (3,))
    assert not np.isnan(at(tg, "S0", 10, "ret_3"))


# ------------------------------------------------------------------ vol unit
def test_vol_is_atr_pct_through_s_times_sqrt_h(bars, tg):
    d = bars["S5"]
    atr = wilder(true_range(d["high"], d["low"], d["close"]), 14)
    s = 50
    for h in HS:
        assert at(tg, "S5", s, f"vol_{h}") == pytest.approx(atr.iloc[s] / d["close"].iloc[s] * np.sqrt(h))
    assert at(tg, "S5", s, "atr_pct") == pytest.approx(atr.iloc[s] / d["close"].iloc[s])
    assert np.isnan(at(tg, "S5", 5, "vol_5"))                        # ATR needs 14 sessions


def test_vol_ignores_the_future(bars):
    b = {k: v.copy() for k, v in bars.items()}
    ref = T.daily_targets(b, (5,))
    b["S5"].iloc[51:, [b["S5"].columns.get_loc(c) for c in ("open", "high", "low", "close")]] *= 3.0
    chg = T.daily_targets(b, (5,))
    assert at(chg, "S5", 50, "vol_5") == at(ref, "S5", 50, "vol_5")


def test_z_is_ret_over_vol_clipped(tg):
    for h in HS:
        z = tg[f"z_{h}"].dropna()
        assert z.abs().max() <= T.REGRESSION_CLIP + 1e-12
        raw = (tg[f"ret_{h}"] / tg[f"vol_{h}"]).dropna()
        ok = raw.abs() < T.REGRESSION_CLIP
        assert np.allclose(z[raw.index[ok]], raw[ok])
    big = (tg["ret_5"] / tg["vol_5"]).abs() > 5
    if big.any():
        assert (tg.loc[big, "z_5"].abs() == 5).all()


# ------------------------------------------------------------------ selection
def test_rel_ret_sums_to_zero_per_session(tg):
    for h in HS:
        s = tg[f"rel_ret_{h}"].groupby("session").sum(min_count=1).dropna()
        assert len(s) > 0
        assert np.allclose(s, 0.0, atol=1e-12)


def test_rel_ret_is_ret_minus_session_mean(tg):
    h = 5
    ret = tg[f"ret_{h}"].unstack("symbol")
    rel = tg[f"rel_ret_{h}"].unstack("symbol")
    i = 40
    assert np.allclose(rel.iloc[i], ret.iloc[i] - ret.iloc[i].mean())


def test_universe_membership_limits_the_mean_and_nans_non_members(bars):
    h = 3
    member = pd.DataFrame(True, index=SESSIONS, columns=SYMS)
    member.loc[SESSIONS[40], ["S0", "S1"]] = False              # 6 members left
    member.loc[SESSIONS[41], ["S0", "S1", "S2", "S3"]] = False  # 4 members: below the minimum
    tg = T.daily_targets(bars, (h,), universe=member)
    ret = tg[f"ret_{h}"].unstack("symbol")
    rel = tg[f"rel_ret_{h}"].unstack("symbol")
    members = ["S2", "S3", "S4", "S5", "S6", "S7"]
    assert np.allclose(rel.loc[SESSIONS[40], members], ret.loc[SESSIONS[40], members] - ret.loc[SESSIONS[40], members].mean())
    assert rel.loc[SESSIONS[40], ["S0", "S1"]].isna().all()
    assert not ret.loc[SESSIONS[40], ["S0", "S1"]].isna().any()   # timing targets are kept
    assert rel.loc[SESSIONS[41]].isna().all()                     # fewer than 5 members
    assert rel.loc[SESSIONS[42]].notna().all()


def test_symbols_not_decidable_at_s_do_not_shape_relative_targets():
    """A symbol unlisted at S (listing at S+1) or still in its ATR warm-up has no relative target and does not move
    the others' session mean or ranks: a future listing must not shape today's labels."""
    b = make_bars()
    d = b["S7"].copy()
    d.iloc[:30, [d.columns.get_loc(c) for c in ("open", "high", "low", "close")]] = np.nan
    d.iloc[:30, d.columns.get_loc("listed")] = False
    b["S7"] = d
    h = 3
    tg = T.daily_targets(b, (h,))
    ret = tg[f"ret_{h}"].unstack("symbol")
    rel = tg[f"rel_ret_{h}"].unstack("symbol")
    rk = tg[f"rank_rel_{h}"].unstack("symbol")
    others = SYMS[:-1]
    assert not np.isnan(ret.loc[SESSIONS[29], "S7"])                  # it lists at S+1 and has an outcome ...
    for i in (20, 29, 35, 42):                                        # ... but is undecidable at S (unlisted, then ATR warm-up)
        assert np.isnan(rel.loc[SESSIONS[i], "S7"]) and np.isnan(rk.loc[SESSIONS[i], "S7"]), i
        if i >= 29:
            assert np.allclose(rel.loc[SESSIONS[i], others], ret.loc[SESSIONS[i], others] - ret.loc[SESSIONS[i], others].mean())
    assert not np.isnan(rel.loc[SESSIONS[44], "S7"])                  # ATR14 is available from the 14th listed bar
    # the others' results equal a panel without S7 at all
    ref = T.daily_targets({k: v for k, v in b.items() if k != "S7"}, (h,))
    ref_rel = ref[f"rel_ret_{h}"].unstack("symbol")
    ref_rk = ref[f"rank_rel_{h}"].unstack("symbol")
    for i in (29, 35, 42):
        assert np.allclose(rel.loc[SESSIONS[i], others], ref_rel.loc[SESSIONS[i]])
        assert np.array_equal(rk.loc[SESSIONS[i], others], ref_rk.loc[SESSIONS[i]])


def test_filled_s_is_not_decidable_either():
    b = make_bars()
    b["S2"] = b["S2"].copy()
    b["S2"].iloc[40, b["S2"].columns.get_loc("filled")] = True
    tg = T.daily_targets(b, (3,))
    rel = tg["rel_ret_3"].unstack("symbol")
    assert np.isnan(rel.loc[SESSIONS[40], "S2"]) and rel.loc[SESSIONS[40]].drop("S2").notna().all()



def test_fewer_than_five_values_is_nan(bars):
    b = {k: v.copy() for k, v in bars.items()}
    for sym in ("S0", "S1", "S2", "S3"):                          # 4 of 8 symbols have no S+3 on session 30
        b[sym].iloc[33, b[sym].columns.get_loc("filled")] = True
    tg = T.daily_targets(b, (3,))
    assert tg["rel_ret_3"].unstack("symbol").loc[SESSIONS[30]].isna().all()
    assert tg["rank_rel_3"].unstack("symbol").loc[SESSIONS[30]].isna().all()
    assert tg["ret_3"].unstack("symbol").loc[SESSIONS[30]].notna().sum() == 4


def test_quintile_beats_consistency(tg):
    for h in HS:
        rel = tg[f"rel_ret_{h}"].unstack("symbol")
        q = tg[f"quintile_{h}"].unstack("symbol")
        beats = tg[f"beats_{h}"].unstack("symbol")
        assert ((beats == 1) == (rel > 0)).where(rel.notna(), True).all().all()
        assert beats.isna().equals(rel.isna())
        for i in range(len(rel)):
            r = rel.iloc[i].dropna()
            if not len(r):
                continue
            top, bottom = r[q.iloc[i] == 1], r[q.iloc[i] == 0]
            assert len(top) >= 1 and len(bottom) >= 1
            assert top.min() >= r.drop(top.index).max()
            assert bottom.max() <= r.drop(bottom.index).min()
            assert q.iloc[i][r.index].isin([0, 1]).sum() < len(r)    # the middle is NaN
    qs = tg["quintile_5"].dropna()
    assert set(qs.unique()) == {0.0, 1.0}


def test_rank_buckets_zero_to_four_within_the_session(tg):
    for h in HS:
        rel = tg[f"rel_ret_{h}"].unstack("symbol")
        rk = tg[f"rank_rel_{h}"].unstack("symbol")
        assert rk.isna().equals(rel.isna())
        assert set(rk.stack().unique()) == {0.0, 1.0, 2.0, 3.0, 4.0}
        for i in (30, 55):
            r, b = rel.iloc[i], rk.iloc[i]
            order = b[r.sort_values().index]
            assert (np.diff(order.to_numpy()) >= 0).all()           # monotone in rel_ret
            assert b.min() == 0 and b.max() == 4
    # the top bucket is the quintile target's positive class
    both = tg[["rank_rel_5", "quintile_5"]].dropna()
    assert (both.loc[both["rank_rel_5"] == 4, "quintile_5"] == 1).all()
    assert (both.loc[both["rank_rel_5"] == 0, "quintile_5"] == 0).all()


def test_rank_buckets_parameter(bars):
    tg = T.daily_targets(bars, (5,), n_buckets=3)
    assert set(tg["rank_rel_5"].dropna().unique()) == {0.0, 1.0, 2.0}
    with pytest.raises(ValueError):
        T.daily_targets(bars, (5,), n_buckets=1)


def test_session_group_and_rank_groups(tg):
    g = tg["session_group"].unstack("symbol")
    assert (g.nunique(axis=1) == 1).all()
    assert (g.iloc[:, 0].to_numpy() == np.arange(N_SESS)).all()
    order, rel, sizes = T.rank_groups(tg, 5)
    grp = tg["session_group"].to_numpy()[order]
    assert (np.diff(grp) >= 0).all()                               # contiguous, ascending groups
    assert sizes.sum() == len(order) == tg["rank_rel_5"].notna().sum()
    assert (np.repeat(np.unique(grp), sizes) == grp).all()
    assert rel.dtype.kind == "i" and rel.min() == 0 and rel.max() == 4


# ------------------------------------------------------------------ realised vol / purge
def test_realvol_is_the_std_of_log_returns_s_plus_1_to_s_plus_h(bars, tg):
    d = bars["S4"]
    lr = np.log(d["close"]).diff()
    s = 25
    for h in (3, 5, 10, 20):
        assert at(tg, "S4", s, f"realvol_{h}") == pytest.approx(lr.iloc[s + 1:s + h + 1].std(ddof=1))
    assert np.isnan(at(tg, "S4", s, "realvol_1"))                    # one return has no spread
    assert np.isnan(at(tg, "S4", N_SESS - 2, "realvol_3"))


def test_target_end_is_the_session_of_s_plus_h(tg):
    for h in HS:
        te = tg[f"target_end_{h}"].xs("S1", level="symbol")
        assert te.iloc[10] == SESSIONS[10 + h]
        assert te.iloc[N_SESS - 1 - h] == SESSIONS[N_SESS - 1]
        assert te.iloc[N_SESS - h:].isna().all()
        assert te.iloc[:N_SESS - h].notna().all()


# ------------------------------------------------------------------ timing targets
def test_up_is_ret_positive(tg):
    for h in HS:
        r, u = tg[f"ret_{h}"], tg[f"up_{h}"]
        assert u.isna().equals(r.isna())
        assert ((u == 1) == (r > 0))[r.notna()].all()


def test_multistep_matrix(tg):
    m = T.multistep_matrix(tg, HS)
    assert m.shape == (len(tg), len(HS))
    for j, h in enumerate(HS):
        assert np.array_equal(m[:, j], tg[f"up_{h}"].to_numpy(), equal_nan=True)
    assert T.multistep_matrix(tg, (5, 1)).shape == (len(tg), 2)
    with pytest.raises(KeyError):
        T.multistep_matrix(tg, (2,))


# ------------------------------------------------------------------ barriers
def excursions(bars, h):
    e = T.daily_excursions(bars, [h])[h]
    return e.up, e.dn


def barrier_bars():
    """A flat, quiet series so ATR is a known constant, with hand-made S+1.. bars after session 30."""
    idx = pd.bdate_range("2024-01-02", periods=60)
    d = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6,
                      "listed": True, "filled": False, "ended": False}, index=idx)
    d.index.name = "session"
    return d


def test_barrier_path_starts_at_the_next_open_and_spans_s_plus_1_to_s_plus_h():
    base = barrier_bars()
    b = {sym: base.copy() for sym in SYMS[:2]}
    s, h = 30, 3
    # S+1 opens at 110 (a gap up), its range is 109..112; S+2 reaches 118; S+3 drops to 101; S+4 spikes (outside)
    d = b["S0"]
    for i, (o, hi, lo, c) in {s + 1: (110, 112, 109, 111), s + 2: (111, 118, 110, 117), s + 3: (117, 117.5, 101, 102),
                              s + 4: (102, 500, 1, 102)}.items():
        d.iloc[i, :4] = [o, hi, lo, c]
    atr = wilder(true_range(d["high"], d["low"], d["close"]), 14).iloc[s]
    assert atr > 0
    up, dn = excursions(b, h)
    row = 0 * len(base) + s
    assert up.shape == (2 * len(base), h)
    np.testing.assert_allclose(up[row], [(112 - 110) / atr, (118 - 110) / atr, (118 - 110) / atr], rtol=1e-5)
    np.testing.assert_allclose(dn[row], [(110 - 109) / atr, (110 - 109) / atr, (110 - 101) / atr], rtol=1e-5)
    # S's close is not the entry
    assert not np.isclose(up[row][0], (112 - 100) / atr)
    # S+4 (outside the horizon) changes nothing; S+3 (the last path bar) does
    d2 = {k: v.copy() for k, v in b.items()}
    d2["S0"].iloc[s + 4, :4] = [102, 900, 0.5, 102]
    up2, _ = excursions(d2, h)
    np.testing.assert_array_equal(up2[row], up[row])
    d2["S0"].iloc[s + 3, d2["S0"].columns.get_loc("high")] = 130
    up3, _ = excursions(d2, h)
    assert up3[row][-1] > up[row][-1]
    # NaN where the horizon runs past the data end
    assert np.isnan(up[len(base) - 1]).all() and np.isnan(up[len(base) - h]).all()
    assert not np.isnan(up[len(base) - h - 1]).any()


def test_barrier_excursions_do_not_depend_on_the_close_of_s():
    base = barrier_bars()
    b = {sym: base.copy() for sym in SYMS[:2]}
    up, dn = excursions(b, 3)
    b["S0"].iloc[30, b["S0"].columns.get_loc("close")] = 100.4          # S's close moves, its range does not
    up2, dn2 = excursions(b, 3)
    np.testing.assert_array_equal(up[30], up2[30])
    b["S0"].iloc[31, b["S0"].columns.get_loc("open")] = 100.5           # the entry moves
    up3, _ = excursions(b, 3)
    assert not np.array_equal(up[30], up3[30])


def test_same_bar_touch_of_both_barriers_is_a_loss():
    base = barrier_bars()
    b = {sym: base.copy() for sym in SYMS[:2]}
    s = 30
    b["S0"].iloc[s + 1, :4] = [100, 120, 80, 100]       # one bar tags +20 and -20 around the open
    up, dn = excursions(b, 3)
    lab = T.triple_barrier_labels(up, dn, 1.0, 1.0)
    assert lab[s] == 0.0
    # a win needs the take-profit alone
    b["S1"].iloc[s + 1, :4] = [100, 120, 99.9, 100]
    up, dn = excursions(b, 3)
    lab = T.triple_barrier_labels(up, dn, 1.0, 5.0)
    assert lab[len(base) + s] == 2.0


def _fit_inputs(bars, h):
    df = T.daily_targets(bars, (h,))
    return df, T.daily_excursions(bars, [h])[h]


def test_fitted_barrier_k_gives_a_third_timeouts_on_train_rows(bars):
    h = 5
    df, exc = _fit_inputs(bars, h)
    sess = df.index.get_level_values("session")
    mask = np.asarray(sess < SESSIONS[60])
    for ratio in (1.0, 0.5):
        ft = T.fit_daily_target(df, T.TargetSpec("triple_barrier", h, ratio), mask, exc)
        assert ft.params["m"] == pytest.approx(ft.params["k"] * ratio)
        tl = ft.labels[mask]
        tl = tl[~np.isnan(tl)]
        assert abs((tl == 1).mean() - 1 / 3) < 0.1
        assert ft.class_returns.shape == (len(df), 3)
        bb = T.fit_daily_target(df, T.TargetSpec("binary_barrier", h, ratio), mask, exc)
        assert set(np.unique(bb.labels[~np.isnan(bb.labels)])) <= {0.0, 2.0}
        assert bb.params["k"] == ft.params["k"]
        assert np.array_equal(bb.labels == 2.0, ft.labels == 2.0)


@pytest.mark.parametrize("kind", ["direction", "binary_direction", "triple_barrier", "binary_barrier"])
def test_fitted_thresholds_use_train_rows_only(bars, kind):
    h = 5
    df, exc0 = _fit_inputs(bars, h)
    up, dn = exc0.up, exc0.dn
    mask = np.asarray(df.index.get_level_values("session") < SESSIONS[55])
    spec = T.TargetSpec(kind, h)
    ref = T.fit_daily_target(df, spec, mask, exc0)
    wild = df.copy()
    wild.loc[~mask, f"ret_{h}"] = wild.loc[~mask, f"ret_{h}"] * 7.0 + 0.3
    up2, dn2 = up.copy(), dn.copy()
    up2[~mask] = np.random.default_rng(1).uniform(0, 30, up2[~mask].shape)
    dn2[~mask] = np.random.default_rng(2).uniform(0, 30, dn2[~mask].shape)
    chg = T.fit_daily_target(wild, spec, mask, dataclasses.replace(exc0, up=up2, dn=dn2))
    assert chg.params == ref.params
    if kind == "direction":
        tr = df.loc[mask, f"ret_{h}"].dropna().to_numpy()
        lo, hi = np.quantile(tr, [1 / 3, 2 / 3])
        assert ref.params["lo"] == pytest.approx(lo) and ref.params["hi"] == pytest.approx(hi)
        lab = ref.labels[mask & df[f"ret_{h}"].notna().to_numpy()]
        assert 0.3 < (lab == 0).mean() < 0.37 and 0.3 < (lab == 2).mean() < 0.37
    # ... and changing TRAIN rows does change the fit
    wild2 = df.copy()
    wild2.loc[mask, f"ret_{h}"] = wild2.loc[mask, f"ret_{h}"] * 7.0 + 0.3
    up3, dn3 = up.copy(), dn.copy()
    up3[mask] = np.random.default_rng(3).uniform(0, 30, up3[mask].shape)
    chg2 = T.fit_daily_target(wild2, spec, mask, dataclasses.replace(exc0, up=up3, dn=dn3))
    if kind != "binary_direction":                      # its fit holds only class means, which are not parameters
        assert chg2.params != ref.params
    else:
        assert not np.allclose(chg2.class_returns[0], ref.class_returns[0])


def test_fit_daily_target_other_kinds_and_refusals(bars):
    h = 3
    df, exc = _fit_inputs(bars, h)
    mask = np.ones(len(df), dtype=bool)
    fr = T.fit_daily_target(df, T.TargetSpec("forward_return", h), mask)
    assert np.array_equal(fr.labels, df[f"ret_{h}"].to_numpy(), equal_nan=True)
    rg = T.fit_daily_target(df, T.TargetSpec("regression", h), mask)
    assert np.array_equal(rg.labels, df[f"z_{h}"].to_numpy(), equal_nan=True)
    bd = T.fit_daily_target(df, T.TargetSpec("binary_direction", h), mask)
    assert np.array_equal(bd.labels == 2, (df[f"ret_{h}"] > 0).to_numpy())
    with pytest.raises(ValueError, match="excursions"):
        T.fit_daily_target(df, T.TargetSpec("triple_barrier", h), mask)
    with pytest.raises(ValueError, match="do not match"):
        T.fit_daily_target(df, T.TargetSpec("triple_barrier", h), mask, dataclasses.replace(exc, up=exc.up[:10], dn=exc.dn[:10]))
    with pytest.raises(ValueError, match="train_mask"):
        T.fit_daily_target(df, T.TargetSpec("direction", h), np.flatnonzero(mask))
    with pytest.raises(KeyError):
        T.fit_daily_target(df, T.TargetSpec("direction", 7), mask)
    with pytest.raises(T.TargetBalanceError):
        T.fit_daily_target(df, T.TargetSpec("direction", h), np.zeros(len(df), dtype=bool))


# ------------------------------------------------------------------ folds / purge
def test_daily_fold_rows_purge(tg):
    h = 5
    fold = T.Fold("F", date(2024, 1, 2), date(2024, 3, 1), date(2024, 3, 29))
    train, pred = T.daily_fold_rows(tg, fold, h, f"ret_{h}")
    sess = tg.index.get_level_values("session")
    te = tg[f"target_end_{h}"]
    assert (te.iloc[train] < pd.Timestamp(fold.pred_start)).all()
    assert (sess[train] < pd.Timestamp(fold.pred_start)).all()
    # the last h training sessions before the block are purged
    last_train = sess[train].max()
    assert SESSIONS[SESSIONS < pd.Timestamp(fold.pred_start)][-h - 1] == last_train
    assert (sess[pred] >= pd.Timestamp(fold.pred_start)).all() and (sess[pred] <= pd.Timestamp(fold.pred_end)).all()


def test_daily_fold_rows_keep_only_rows_with_a_finite_target_and_atr(tg):
    h = 5
    fold = T.Fold("F", date(2024, 1, 2), date(2024, 3, 1), date(2024, 3, 29))
    for col in (f"ret_{h}", f"rel_ret_{h}", f"up_{h}"):
        train, pred = T.daily_fold_rows(tg, fold, h, col)
        for rows in (train, pred):
            assert np.isfinite(tg[col].to_numpy()[rows]).all(), col
            assert np.isfinite(tg["atr_pct"].to_numpy()[rows]).all(), col
    train, pred = T.daily_fold_rows(tg, fold, h, f"ret_{h}")
    sess = tg.index.get_level_values("session")
    in_block = (sess >= pd.Timestamp(fold.pred_start)) & (sess <= pd.Timestamp(fold.pred_end))
    assert len(pred) == len(SYMS) * (in_block.sum() // len(SYMS))      # the whole block has outcomes and an ATR
    # the first 13 sessions have no ATR: a fold that trains from the start loses them
    early = T.Fold("E", date(2024, 1, 2), date(2024, 3, 1), date(2024, 3, 29))
    t2, _ = T.daily_fold_rows(tg, early, h, f"ret_{h}")
    assert (sess[t2] >= SESSIONS[13]).all()
    with pytest.raises(KeyError):
        T.daily_fold_rows(tg, fold, h, "nope")


def test_daily_fold_rows_refuse_a_target_of_another_horizon(tg):
    fold = T.Fold("F", date(2024, 1, 2), date(2024, 3, 1), date(2024, 3, 29))
    with pytest.raises(ValueError, match="horizon"):
        T.daily_fold_rows(tg, fold, 10, "rel_ret_5")           # purged with 10 sessions but trained on a 5-session target
    with pytest.raises(ValueError, match="horizon"):
        T.daily_fold_rows(tg, fold, 5, "rel_ret_10")
    with pytest.raises(ValueError, match="horizon"):
        T.daily_fold_rows(tg, fold, 5, "atr_pct")              # not a horizon target at all
    with pytest.raises(ValueError, match="horizon"):
        T.daily_fold_rows(tg, fold, 1, "ret_11")               # a suffix match is not enough: "_1" vs "_11"
    T.daily_fold_rows(tg, fold, 10, "rel_ret_10")


# ------------------------------------------------------------------ class balance
def test_check_balance_min_count_mode():
    spec = T.TargetSpec("triple_barrier", 5)
    T.check_balance(spec, "F", mode="min_count", counts_by_part={"train": [50, 500, 5000]})
    with pytest.raises(T.TargetBalanceError, match="49 examples < 50"):
        T.check_balance(spec, "F", mode="min_count", counts_by_part={"train": [5000, 49, 5000]})
    with pytest.raises(T.TargetBalanceError, match="pred"):
        T.check_balance(spec, "F", mode="min_count", counts_by_part={"train": [60, 60, 60], "pred": [60, 60, 10]})
    # a 2% class is fine in count mode, refused in share mode
    shares = [0.02, 0.5, 0.48]
    T.check_balance(spec, "F", mode="min_count", counts_by_part={"train": [200, 5000, 4800]})
    with pytest.raises(T.TargetBalanceError):
        T.check_balance(spec, "F", {"train": shares})
    # a two-class target ignores class 1
    T.check_balance(T.TargetSpec("binary_barrier", 5), "F", mode="min_count", counts_by_part={"train": [60, 0, 60]})
    with pytest.raises(T.TargetBalanceError):
        T.check_balance(T.TargetSpec("binary_barrier", 5), "F", mode="min_count", counts_by_part={"train": [60, 0, 10]})
    # a custom threshold; regression is never refused
    with pytest.raises(T.TargetBalanceError):
        T.check_balance(spec, "F", mode="min_count", counts_by_part={"train": [99, 99, 99]}, min_count=100)
    T.check_balance(T.TargetSpec("regression", 5), "F", mode="min_count", counts_by_part={"train": [0, 0, 0]})


def test_check_balance_modes_are_explicit():
    spec = T.TargetSpec("direction", 5)
    with pytest.raises(ValueError, match="mode"):
        T.check_balance(spec, "F", {"train": [0.3, 0.3, 0.4]}, mode="count")
    with pytest.raises(ValueError, match="counts_by_part"):
        T.check_balance(spec, "F", mode="min_count")
    with pytest.raises(ValueError, match="shares_by_part"):
        T.check_balance(spec, "F", mode="share")
    with pytest.raises(ValueError, match="min_count"):
        T.check_balance(spec, "F", {"train": [0.3, 0.3, 0.4]}, mode="min_count", counts_by_part={"train": [60] * 3})
    # the share mode is unchanged
    T.check_balance(spec, "F", {"train": [0.3, 0.3, 0.4]})
    with pytest.raises(T.TargetBalanceError, match="14.0%"):
        T.check_balance(spec, "F", {"train": [0.14, 0.4, 0.46]})


def test_class_counts():
    lab = np.array([0, 1, 2, 2, np.nan, 2.0])
    assert T.class_counts(lab, np.arange(6)) == [1, 1, 3]
    assert T.class_counts(lab, np.array([0, 4])) == [1, 0, 0]


# ------------------------------------------------------------------ input refusals
def test_input_refusals(bars):
    with pytest.raises(ValueError, match="horizons"):
        T.daily_targets(bars, (0, 3))
    with pytest.raises(ValueError, match="horizons"):
        T.daily_targets(bars, (3, 3))
    with pytest.raises(ValueError, match="empty"):
        T.daily_targets({}, (3,))
    short = {k: v for k, v in bars.items()}
    short["S1"] = bars["S1"].iloc[:-1]
    with pytest.raises(ValueError, match="S1"):
        T.daily_targets(short, (3,))
    nolisted = {k: v.drop(columns="listed") for k, v in bars.items()}
    with pytest.raises(ValueError, match="listed"):
        T.daily_targets(nolisted, (3,))
    member = pd.DataFrame(True, index=SESSIONS[:-5], columns=SYMS)
    with pytest.raises(ValueError, match="universe has no row"):
        T.daily_targets(bars, (3,), universe=member)
    member = pd.DataFrame(True, index=SESSIONS, columns=SYMS[:-1])
    with pytest.raises(ValueError, match="no column"):
        T.daily_targets(bars, (3,), universe=member)


# ------------------------------------------------------------------ shared arrays, excursions, memory
def test_arrays_are_computed_once_and_shared(bars, monkeypatch):
    from app.services import ml_panel
    calls = []
    real = ml_panel.wilder
    monkeypatch.setattr(ml_panel, "wilder", lambda *a, **k: calls.append(1) or real(*a, **k))
    arrays = T.daily_arrays(bars)
    assert len(calls) == len(SYMS)
    shared = T.daily_targets(bars, HS, arrays=arrays)
    exc = T.daily_excursions(None, HS, arrays=arrays)
    assert len(calls) == len(SYMS)                                        # the ATR loop ran once
    pd.testing.assert_frame_equal(shared, T.daily_targets(bars, HS))
    assert exc[5].fingerprint == arrays.fingerprint


def test_excursions_are_built_once_for_the_largest_horizon_and_sliced(bars):
    both = T.daily_excursions(bars, HS)
    assert sorted(both) == sorted(HS)
    for h in HS:
        alone = T.daily_excursions(bars, [h])[h]
        assert both[h].up.shape == (len(SYMS) * N_SESS, h)
        np.testing.assert_array_equal(both[h].up, alone.up)
        np.testing.assert_array_equal(both[h].dn, alone.dn)
        assert both[h].horizon == h
    # a row valid for H=1 but past the end for H=20 keeps its short path only for the short horizon
    row = N_SESS - 2                                                      # S+1 = the last session
    assert not np.isnan(both[1].up[row]).any() and np.isnan(both[20].up[row]).all()
    # the validity mask is the horizon's own: rows valid for 3 but not 5
    row = N_SESS - 4
    assert not np.isnan(both[3].up[row]).any() and np.isnan(both[5].up[row]).all()


def test_excursions_are_matched_to_the_frame_by_fingerprint(bars):
    h = 5
    df = T.daily_targets(bars, (h,))
    exc = T.daily_excursions(bars, [h])[h]
    mask = np.ones(len(df), dtype=bool)
    spec = T.TargetSpec("triple_barrier", h)
    T.fit_daily_target(df, spec, mask, exc)                                # the matching layout passes
    renamed = {f"X{i}": v for i, (k, v) in enumerate(bars.items())}       # same shape, other symbols
    with pytest.raises(ValueError, match="different symbols or sessions"):
        T.fit_daily_target(df, spec, mask, T.daily_excursions(renamed, [h])[h])
    later = {k: v.set_axis(v.index + pd.Timedelta(days=7)) for k, v in bars.items()}      # same shape, other sessions
    with pytest.raises(ValueError, match="different symbols or sessions"):
        T.fit_daily_target(df, spec, mask, T.daily_excursions(later, [h])[h])
    with pytest.raises(ValueError, match="do not match"):
        T.fit_daily_target(df, spec, mask, T.daily_excursions(bars, [3])[3])
    with pytest.raises(ValueError, match="different symbols or sessions"):
        T.fit_daily_target(df.iloc[::-1], spec, mask, exc)                 # a reordered frame is another layout
    with pytest.raises(ValueError, match="symbol-major"):
        T.daily_fingerprint(df.iloc[5:])                                   # a sliced frame has no layout at all
    assert T.daily_fingerprint(df) == exc.fingerprint


def test_kinds_build_only_the_requested_families(bars, tg):
    part = T.daily_targets(bars, (3, 5), kinds=("up", "realvol"))
    assert sorted(part.columns) == sorted(["atr_pct", "up_3", "up_5", "realvol_3", "realvol_5"])
    for c in part.columns:
        pd.testing.assert_series_equal(part[c], tg[c], check_names=False)
    sel = T.daily_targets(bars, (5,), kinds=("rank_rel",))
    assert sorted(sel.columns) == ["atr_pct", "rank_rel_5", "session_group"]
    pd.testing.assert_series_equal(sel["rank_rel_5"], tg["rank_rel_5"])
    ret_only = T.daily_targets(bars, (5,), kinds=("ret",))
    assert sorted(ret_only.columns) == ["atr_pct", "ret_5"]
    with pytest.raises(ValueError, match="kinds"):
        T.daily_targets(bars, (5,), kinds=("ret", "nope"))
    with pytest.raises(ValueError, match="kinds"):
        T.daily_targets(bars, (5,), kinds=())
    with pytest.raises(KeyError, match="ret_5"):
        T.fit_daily_target(T.daily_targets(bars, (5,), kinds=("up",)), T.TargetSpec("direction", 5),
                           np.ones(len(bars) * N_SESS, dtype=bool))
    # regression needs only z
    z = T.daily_targets(bars, (5,), kinds=("z",))
    T.fit_daily_target(z, T.TargetSpec("regression", 5), np.ones(len(z), dtype=bool))


def test_flag_and_bucket_columns_are_float32(tg):
    for h in HS:
        for c in ("up", "quintile", "beats", "rank_rel"):
            assert tg[f"{c}_{h}"].dtype == np.float32, c
    assert tg["session_group"].dtype == np.int32


def test_one_bucket_pass_per_horizon_with_the_default_buckets(bars, monkeypatch):
    calls = []
    real = T._buckets
    monkeypatch.setattr(T, "_buckets", lambda rel, n: calls.append(n) or real(rel, n))
    T.daily_targets(bars, HS)
    assert calls == [5] * len(HS)
    calls.clear()
    T.daily_targets(bars, (3,), n_buckets=3)
    assert sorted(calls) == [3, 5]
    calls.clear()
    T.daily_targets(bars, (3,), kinds=("rank_rel",), n_buckets=3)
    assert calls == [3]


def test_rank_groups_accepts_row_positions(tg):
    fold = T.Fold("F", date(2024, 1, 2), date(2024, 3, 1), date(2024, 3, 29))
    train, _ = T.daily_fold_rows(tg, fold, 5, "rel_ret_5")
    order, rel, sizes = T.rank_groups(tg, 5, rows=train)
    assert set(order) <= set(train) and len(order) == len(train)           # every usable train row has a rank
    grp = tg["session_group"].to_numpy()[order]
    assert (np.diff(grp) >= 0).all() and sizes.sum() == len(order)
    shuffled = np.random.default_rng(0).permutation(train)
    order2, rel2, sizes2 = T.rank_groups(tg, 5, rows=shuffled)
    assert np.array_equal(sizes, sizes2) and np.array_equal(np.sort(order), np.sort(order2))
    assert np.array_equal(rel2, tg["rank_rel_5"].to_numpy()[order2].astype(int))


def test_bool_flag_columns_are_required_not_cast(bars):
    for col in ("listed", "filled", "ended"):
        bad = {k: v.copy() for k, v in bars.items()}
        bad["S1"][col] = bad["S1"][col].astype(int)
        with pytest.raises(TypeError, match=col):
            T.daily_targets(bad, (3,))
    obj = {k: v.copy() for k, v in bars.items()}
    obj["S1"]["listed"] = obj["S1"]["listed"].astype(object)
    with pytest.raises(TypeError, match="listed"):
        T.daily_targets(obj, (3,))
