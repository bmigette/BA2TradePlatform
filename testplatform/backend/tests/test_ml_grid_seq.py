"""GPU families of the stage-1 grid (Task 4.4b): ``tsai-<arch>`` on the intraday 1h panel and ``darts-<arch>`` on the
daily store frame, run in a second process pool; on tiny synthetic data, CPU torch (``device: "cpu"``)."""
import json
import os
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import ml_grid as G
from app.services import ml_grid_seq as S
from app.services import ml_holdout as HO
from app.services import ml_models_seq as MS
from app.services import ml_panel as P

FIRST_G, LAST_G = date(2018, 7, 2), date(2020, 12, 31)
CUT = date(2020, 6, 30)                          # the end of the first test block


def _unlocked(n, prefix="GPU"):
    out, i = [], 0
    while len(out) < n:
        s = f"{prefix}{i}"
        if not HO.is_locked_largecap(s):
            out.append(s)
        i += 1
    return out


GSYMS = _unlocked(8)


def _gsettings(**kw):
    base = dict(eval_start=date(2020, 4, 1), eval_end=LAST_G, data_start=FIRST_G, min_count=5,
                control_seeds=(1, 2, 3), cores=2, min_obs=20, regime_min_rows=10, seq_lookback=14,
                seq_samples_per_session=2, darts_lookback=10, gpu_max_epochs=2)
    base.update(kw)
    return G.GridSettings(**base)


# ================================================================== the synthetic world
def build_gpu_world(root: Path, beta: float = 0.5, seed: int = 11):
    """5-minute and daily bars of SPY, QQQ and ``GSYMS`` (the daily bars are made FROM the 5-minute bars) plus a daily
    feature store of ``GSYMS``. A symbol's day return = market + noise + ``beta`` x the previous day's return, so the
    previous day's return (``t_signal`` of the store, ``t_ret_1d`` of the panel) carries signal."""
    rng = np.random.default_rng(seed)
    sess = P.regular_sessions(FIRST_G, LAST_G)
    starts, sess_of = [], []
    for k, (o, c) in enumerate(zip(sess["open_ts"], sess["close_ts"])):
        m = int((c - o) / pd.Timedelta(minutes=5))
        starts.append(o + pd.to_timedelta(np.arange(m) * 5, unit="m"))
        sess_of.append(np.full(m, k))
    starts = pd.DatetimeIndex(np.concatenate([s.to_numpy() for s in starts]))
    sidx = np.concatenate(sess_of)
    nb = len(starts)
    bounds = np.r_[0, np.flatnonzero(np.diff(sidx)) + 1, nb]
    market = rng.normal(0, 0.0006, nb)
    cdir = root / "cache" / "AlpacaHistoryOHLCVProvider"
    cdir.mkdir(parents=True, exist_ok=True)
    day_dates = pd.DatetimeIndex(pd.to_datetime(sess["session"]))
    daily_close = {}
    for sym in ("SPY", "QQQ") + tuple(GSYMS):
        mb = 1.0 if sym == "SPY" else 1.1 if sym == "QQQ" else 0.8
        idio = 0.0003 if sym in ("SPY", "QQQ") else 0.0009
        ret = np.empty(nb)
        prev = 0.0
        for a, b in zip(bounds[:-1], bounds[1:]):
            r = market[a:b] * mb + rng.normal(0, idio, b - a)
            if sym in GSYMS:
                r = r + beta * prev / (b - a)
            ret[a:b] = r
            prev = float(r.sum())
        close = 100.0 * np.exp(np.cumsum(ret))
        open_ = np.r_[100.0, close[:-1]]
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0002, nb)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0002, nb)))
        vol = rng.integers(10_000, 20_000, nb).astype("int64")
        pd.DataFrame({"Date": starts, "Open": open_, "High": high, "Low": low, "Close": close,
                      "Volume": vol}).to_parquet(cdir / f"{sym}_5min.parquet", index=False)
        g = pd.DataFrame({"s": sidx, "o": open_, "h": high, "l": low, "c": close, "v": vol}).groupby("s")
        d = pd.DataFrame({"Date": day_dates, "Open": g["o"].first().to_numpy(), "High": g["h"].max().to_numpy(),
                          "Low": g["l"].min().to_numpy(), "Close": g["c"].last().to_numpy(),
                          "Volume": g["v"].sum().to_numpy()})
        d["effective_date"] = d["Date"]
        d.to_parquet(cdir / f"{sym}_1d.parquet", index=False)
        daily_close[sym] = d["Close"].to_numpy()
    feats = []
    for sym in GSYMS:
        lr = np.r_[0.0, np.diff(np.log(daily_close[sym]))]
        f = pd.DataFrame({"t_signal": lr / lr.std(), "t_noise": rng.normal(0, 1, len(lr)), "t_vol": np.abs(lr)},
                         index=pd.MultiIndex.from_product([[sym], day_dates], names=["symbol", "session"]))
        f["listed"], f["filled"], f["ended"] = True, False, False
        f["label_bar_filled"] = pd.array(np.zeros(len(lr), dtype=bool), dtype="boolean")
        feats.append(f)
    d = root / "stores" / "stocks"
    d.mkdir(parents=True)
    pd.concat(feats).to_parquet(d / "features.parquet")
    meta = {"symbols": list(GSYMS), "first_day": str(FIRST_G), "last_day": str(LAST_G), "cache_end": str(LAST_G),
            "ensure": {"min_share": 0.7, "inputs": {"code": "synthetic-gpu-1", "fingerprints": {
                "daily": {s: {"mtime_ns": 1, "size": 1} for s in GSYMS}}}}}
    (d / "features.parquet.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    years = sorted(set(day_dates.year))
    pd.DataFrame({"rows": 1, "complete": 1, "share": 1.0},
                 index=pd.MultiIndex.from_tuples([("t", y) for y in years], names=["group", "year"])
                 ).to_parquet(d / "coverage_report.parquet")
    return root / "stores", d, root / "cache"


def truncate_world(world, cut: date, root: Path):
    """A copy of ``world`` whose daily bars, 5-minute bars and store end at ``cut`` (the future is gone)."""
    stores, _, cache = world
    new_cache, new_stores = root / "cache", root / "stores"
    shutil.copytree(cache, new_cache)
    shutil.copytree(stores, new_stores)
    end = pd.Timestamp(cut) + pd.Timedelta(days=1)
    for f in (new_cache / "AlpacaHistoryOHLCVProvider").glob("*.parquet"):
        df = pd.read_parquet(f)
        df[df["Date"] < end].to_parquet(f, index=False)
    sd = new_stores / "stocks"
    feats = pd.read_parquet(sd / "features.parquet")
    feats[feats.index.get_level_values("session") <= pd.Timestamp(cut)].to_parquet(sd / "features.parquet")
    mp = sd / "features.parquet.meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    meta["last_day"] = meta["cache_end"] = str(cut)
    mp.write_text(json.dumps(meta), encoding="utf-8")
    return new_stores, sd, new_cache


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_gpu_world(tmp_path_factory.mktemp("gpuworld"))


TINY_NET = {"width": 8, "epochs": 2, "batch_size": 128, "patience": 1, "min_epochs": 1, "device": "cpu",
            "dropout": 0.0}


def tsai_cell(world, **over):
    c = {"track": "selection", "store": str(world[0]), "symbols": GSYMS, "groups": ["panel_1h"],
         "target": {"kind": "rel_return", "horizon": 5}, "family": "tsai-inception", "params": dict(TINY_NET),
         "train_months": 12, "blocks": "H", "seeds": [1]}
    c.update(over)
    return c


def darts_cell(world, **over):
    p = {**TINY_NET, "num_samples": 20}
    c = {"track": "selection", "store": str(world[0]), "symbols": GSYMS, "groups": ["t"],
         "target": {"kind": "rel_return", "horizon": 5}, "family": "darts-nhits", "params": p,
         "train_months": 12, "blocks": "H", "seeds": [1]}
    c.update(over)
    return c


def lgbm_cell(world, **over):
    c = {"track": "selection", "store": str(world[0]), "symbols": GSYMS, "groups": ["t"],
         "target": {"kind": "rel_return", "horizon": 5}, "family": "lgbm",
         "params": {"n_estimators": 40, "early_stopping_rounds": 5, "min_child_samples": 20, "num_leaves": 7},
         "train_months": 12, "blocks": "H", "seeds": [1]}
    c.update(over)
    return c


def grun(cells, out, world, **kw):
    kw.setdefault("settings", _gsettings())
    kw.setdefault("inline", True)
    kw.setdefault("gpu_slots", 1)
    return G.run_grid([G.validate_cell(c) for c in cells], out, kw.pop("cpu_slots", 1), cache_root=world[2], **kw)


@pytest.fixture(scope="module")
def tsai_run(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("tsai_run")
    res = grun([tsai_cell(world)], out, world)
    return out, res, G.read_results(out / "results.jsonl")


@pytest.fixture(scope="module")
def darts_run(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("darts_run")
    res = grun([darts_cell(world)], out, world)
    return out, res, G.read_results(out / "results.jsonl")


# ================================================================== families and refusals
def test_every_tsai_architecture_and_nhits_nbeats_tcn_are_gpu_families():
    for arch in MS.TSAI_ARCHS:
        assert G.FAMILY_POOL[f"tsai-{arch}"] == "gpu"
    for arch in ("nhits", "nbeats", "tcn"):
        assert G.FAMILY_POOL[f"darts-{arch}"] == "gpu"
    assert "darts-tft" not in G.FAMILY_POOL and "tsai" not in G.FAMILY_POOL and "darts" not in G.FAMILY_POOL
    assert all(G.FAMILY_POOL[f] == "cpu" for f in ("lgbm", "xgb", "catboost", "linear", "naive"))
    assert set(G.GPU_FAMILIES) == {f for f, p in G.FAMILY_POOL.items() if p == "gpu"}
    for f in G.GPU_FAMILIES:
        assert f in G.FAMILY_COST                                         # the scheduler can order them


def test_a_gpu_cell_has_the_gpu_pool_and_its_key_covers_family_and_params(world):
    a = G.validate_cell(tsai_cell(world))
    assert a.pool == "gpu" and a.groups == ("panel_1h",) and a.blocks == "H"
    assert a.key != G.validate_cell(tsai_cell(world, family="tsai-minirocket", params={"device": "cpu"})).key
    assert a.key != G.validate_cell(tsai_cell(world, params={**TINY_NET, "width": 16})).key
    assert a.key != G.validate_cell(darts_cell(world)).key
    assert G.validate_cell(lgbm_cell(world)).pool == "cpu"
    assert G.plan_tasks([a, G.validate_cell(lgbm_cell(world))])           # ordering the pools' tasks works


@pytest.mark.parametrize("over, msg", [
    ({"groups": ["t"]}, "panel_1h"),
    ({"groups": ["panel_1h", "t"]}, "panel_1h"),
    ({"blocks": "Q"}, "half-year"),
    ({"family": "tsai-nope"}, "family"),
    ({"family": "tsai"}, "family"),
    ({"params": {**TINY_NET, "arch": "tst"}}, "arch"),
    ({"target": {"kind": "rank", "horizon": 5}}, "cannot do"),
    ({"params": {"nope": 1}}, "unknown params"),
])
def test_tsai_cells_refuse_what_they_cannot_run(world, over, msg):
    with pytest.raises(ValueError, match=msg):
        G.validate_cell(tsai_cell(world, **over))


@pytest.mark.parametrize("over, msg", [
    ({"family": "darts-tft"}, "family"),
    ({"family": "darts-nope"}, "family"),
    ({"blocks": "Q"}, "half-year"),
    ({"target": {"kind": "up", "horizon": 5}, "track": "timing"}, "darts"),
    ({"target": {"kind": "terciles", "horizon": 5}}, "darts"),
    ({"target": {"kind": "quintile", "horizon": 5}}, "darts"),
    ({"groups": ["panel_1h"]}, "unknown feature groups"),
    ({"params": {**TINY_NET, "arch": "tcn"}}, "arch"),
])
def test_darts_cells_refuse_what_they_cannot_run(world, over, msg):
    with pytest.raises(ValueError, match=msg):
        G.validate_cell(darts_cell(world, **over))


def test_darts_accepts_rel_return_and_the_quantile_kind(world):
    G.validate_cell(darts_cell(world))
    G.validate_cell(darts_cell(world, target={"kind": "quantile", "horizon": 5}))
    G.validate_cell(darts_cell(world, family="darts-tcn", params={**TINY_NET, "num_samples": 20}))


def test_check_groups_does_not_look_for_panel_1h_in_the_store(world):
    G.check_groups([G.validate_cell(tsai_cell(world)), G.validate_cell(darts_cell(world))])


def test_the_new_settings_are_validated_and_only_gpu_rows_carry_them(world):
    for bad in ({"seq_lookback": 0}, {"seq_samples_per_session": 0}, {"darts_lookback": 0}, {"gpu_max_epochs": 0},
                {"gpu_budget_minutes": 0}, {"gpu_budget_minutes": -1}):
        with pytest.raises(ValueError):
            _gsettings(**bad)
    st = _gsettings()
    tab = G.identity_of(G.validate_cell(lgbm_cell(world)), st, "fp")["settings"]
    gpu = G.identity_of(G.validate_cell(tsai_cell(world)), st, "fp")["settings"]
    assert "seq_lookback" not in tab and "gpu_max_epochs" not in tab        # old tabular rows keep their identity
    assert gpu["seq_lookback"] == 14 and gpu["seq_samples_per_session"] == 2 and gpu["gpu_max_epochs"] == 2
    assert "gpu_budget_minutes" not in tab and "gpu_budget_minutes" not in gpu and "cores" not in gpu
    assert G.GridSettings.from_dict(st.to_dict()) == st


def test_gpu_slots_default_to_three_in_the_cli_and_the_route(monkeypatch, tmp_path):
    import types
    import ba2test_launcher as L
    import app.services.ml_grid as grid
    from app.api.ml_search import MlGridRequest
    assert MlGridRequest.model_fields["gpu_slots"].default == 3
    seen = {}

    def fake(task_id, payload):
        seen.update(payload)
        return {"done": 0, "errors": 0, "skipped_existing": 0, "cancelled": False, "out_dir": str(tmp_path)}
    monkeypatch.setattr(grid, "handle_ml_grid", fake)
    L.main(["ml-grid", "--config", "c.json", "--out", str(tmp_path), "--cpu-slots", "2"])
    assert seen["gpu_slots"] == 3 and seen["cpu_slots"] == 2


# ================================================================== the tsai input
def test_the_intraday_panel_holds_the_cells_symbols_and_only_causal_features(world):
    st = _gsettings()
    panel = G.get_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))
    ip = S.get_intraday(panel, st)
    assert set(ip.df["symbol"]) == set(GSYMS)
    assert ip.df["is_eod"].sum() == ip.df.groupby(["symbol", "session"]).ngroups
    assert all(f.startswith(("t_", "d_", "m_")) for f in ip.feats) and not any("fwd" in f for f in ip.feats)
    assert ip.df.index.equals(pd.RangeIndex(len(ip.df)))
    d2e = ip.eod_rows(panel)                                                  # daily row -> its EOD intraday row
    ok = d2e >= 0
    assert ok.any() and ip.df["is_eod"].to_numpy()[d2e[ok]].all()
    sym = ip.df["symbol"].to_numpy()[d2e[ok]]
    assert (sym == panel.index.get_level_values("symbol").to_numpy()[ok]).all()


def test_a_tsai_cell_runs_through_the_grid_and_records_what_it_used(tsai_run):
    out, res, rows = tsai_run
    assert res["done"] == 1 and res["errors"] == 0 and len(rows) == 1
    r = rows[0]
    assert r["status"] == "ok" and r["config"]["groups"] == ["panel_1h"] and r["config"]["family"] == "tsai-inception"
    assert r["n_blocks"] == 2 and r["n_blocks_fitted"] == 2 and r["config"]["blocks"] == "H"
    assert r["low_iter_share"] is None                                        # epochs are not boosting rounds
    for b in r["blocks"]:
        assert b["fit_seconds"] > 0 and b["device"] == "cpu" and b["n_iter"] is None
        assert b["n_samples"] > b["n_fit_sessions"] > 0 and b["n_channels"] >= 10 and b["epochs_run"] >= 1
        assert b["lookback"] == 14 and b["samples_per_session"] == 2
        assert b["train_target_end_max"] < b["start"]                        # every training label is decided before
    assert r["identity"]["settings"]["seq_lookback"] == 14
    assert r["n_features"] == r["blocks"][0]["n_channels"]


def test_only_the_end_of_day_bar_of_each_test_session_is_predicted(world, tmp_path, monkeypatch):
    seen = {"predict": [], "fit": []}
    real_predict, real_fit = MS.TsaiAdapter.predict, MS.TsaiAdapter.fit

    def predict(self, X):
        seen["predict"].append(X.rows.copy())
        return real_predict(self, X)

    def fit(self, Xt, yt, Xv, yv, task, seed, **kw):
        seen["fit"].append((Xt.rows.copy(), np.asarray(yt).copy(), Xv.rows.copy()))
        return real_fit(self, Xt, yt, Xv, yv, task, seed, **kw)
    monkeypatch.setattr(MS.TsaiAdapter, "predict", predict)
    monkeypatch.setattr(MS.TsaiAdapter, "fit", fit)
    out = tmp_path / "o"
    grun([tsai_cell(world)], out, world)
    st = _gsettings()
    panel = G.get_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))
    ip = S.get_intraday(panel, st)
    eod = ip.df["is_eod"].to_numpy()
    assert len(seen["predict"]) == 2
    for rows in seen["predict"]:
        assert eod[rows].all()                                                # nothing but end-of-day bars
        assert len(set(zip(ip.df["symbol"].to_numpy()[rows], ip.df["session"].to_numpy()[rows]))) == len(rows)
    pred = pd.read_parquet(next((out / "predictions").glob("*.parquet")))
    assert not pred.duplicated(["symbol", "session"]).any()                   # one daily row per (symbol, session)
    assert len(pred) == sum(len(r) for r in seen["predict"])
    # training: sampled bars, every sampled (symbol, session) has its end-of-day bar, labels = the DAILY target
    tg = panel.targets(5)
    d2e = ip.eod_rows(panel)
    for rows, y, val_rows in seen["fit"]:
        assert eod[val_rows].all()                                            # validation = end-of-day bars too
        keys = pd.MultiIndex.from_arrays([ip.df["symbol"].to_numpy()[rows],
                                          pd.to_datetime(ip.df["session"].to_numpy()[rows])])
        drows = panel.index.get_indexer(keys)
        assert (drows >= 0).all()
        np.testing.assert_allclose(y, tg["rel_ret_5"].to_numpy()[drows], rtol=1e-6)      # label of the session
        per = pd.Series(eod[rows]).groupby(keys).sum()
        assert (per == 1).all()                                               # the EOD bar of every sampled session
        assert (~eod[rows]).any() and len(rows) <= 2 * len(per)               # plus at most one more bar (per_session 2)


def test_the_tsai_scores_are_scored_like_the_tabular_cells(tsai_run):
    out, _, rows = tsai_run
    r = rows[0]
    assert r["primary"]["name"] == "quintile_spread_net" and r["primary"]["value"] is not None
    assert set(r["controls"]) >= {"shuffle_symbols", "primary", "p_value", "by_regime"}
    pred = pd.read_parquet(next((out / "predictions").glob("*.parquet")))
    assert {"symbol", "session", "block", "score", "rel_ret"} <= set(pred.columns)
    assert r["n_rows"] == len(pred) > 100


# ================================================================== the darts input
def test_a_darts_cell_runs_through_the_grid_on_the_daily_store_frame(darts_run, world):
    out, res, rows = darts_run
    assert res["done"] == 1 and res["errors"] == 0
    r = rows[0]
    assert r["status"] == "ok" and r["config"]["family"] == "darts-nhits" and r["config"]["groups"] == ["t"]
    assert r["n_blocks_fitted"] == 2 and r["low_iter_share"] is None
    for b in r["blocks"]:
        assert b["fit_seconds"] > 0 and b["device"] == "cpu" and b["epochs_run"] >= 1
        assert b["lookback"] == 10 and b["horizon_steps"] == 5 and b["n_channels"] == r["n_features"] >= 3
        assert b["train_target_end_max"] < b["start"] and b["n_iter"] is None
    pred = pd.read_parquet(next((out / "predictions").glob("*.parquet")))
    assert not pred.duplicated(["symbol", "session"]).any() and len(pred) == r["n_rows"] > 100
    assert pred["score"].between(0.0, 1.0).all()                              # P(up) of the deciles


def test_darts_trains_on_series_that_end_before_validation_and_predicts_scorable_rows_only(world, tmp_path, monkeypatch):
    seen = []
    real = MS.DartsAdapter.fit

    def fit(self, Xt, yt, Xv, yv, task, seed, **kw):
        seen.append((Xt, Xv, task))
        return real(self, Xt, yt, Xv, yv, task, seed, **kw)
    monkeypatch.setattr(MS.DartsAdapter, "fit", fit)
    grun([darts_cell(world)], tmp_path / "o", world)
    assert len(seen) == 2
    for Xt, Xv, task in seen:
        assert task == "quantile" and Xt.horizon_steps == 5 and Xt.lookback == 10 and Xt.returns is not None
        assert np.isfinite(Xt.features).all() and np.isfinite(Xt.returns).all()
        assert len(np.unique(Xt.series_start[Xt.rows])) == len(GSYMS)         # one series per symbol


# ================================================================== blocks, budget, epochs, release
def test_a_gpu_cell_with_quarterly_blocks_is_refused_at_run_time_too(world, tmp_path):
    c = G.validate_cell(tsai_cell(world))
    object.__setattr__(c, "blocks", "Q")                                       # bypass validate_cell
    with pytest.raises(ValueError, match="half-year"):
        G.run_cell_seed(c, 1, G.get_panel(c.store, tuple(sorted(GSYMS)), "selection", str(world[2])), _gsettings(), 1)


def test_the_time_budget_aborts_the_task_with_an_error_row(world, tmp_path):
    out = tmp_path / "o"
    res = grun([tsai_cell(world)], out, world, settings=_gsettings(gpu_budget_minutes=1e-6))
    assert res["done"] == 0 and res["errors"] == 1
    err = G.read_results(out / "errors.jsonl")[0]
    assert "time budget" in err["error"] and "minutes" in err["error"]
    assert not G.read_results(out / "results.jsonl")


def test_the_epoch_cap_limits_a_cells_epochs(world, tmp_path):
    out = tmp_path / "o"
    p = {**TINY_NET, "epochs": 6, "patience": 5, "min_epochs": 5}
    grun([tsai_cell(world, params=p)], out, world, settings=_gsettings(gpu_max_epochs=1))
    r = G.read_results(out / "results.jsonl")[0]
    assert all(b["epochs_run"] == 1 for b in r["blocks"])
    out2 = tmp_path / "o2"
    grun([tsai_cell(world, params=p)], out2, world, settings=_gsettings(gpu_max_epochs=3))
    assert all(b["epochs_run"] == 3 for b in G.read_results(out2 / "results.jsonl")[0]["blocks"])


def test_every_block_releases_its_adapter(world, tmp_path, monkeypatch):
    calls = []
    real = MS._SequenceAdapter.release

    def release(self):
        calls.append(self.family)
        return real(self)
    monkeypatch.setattr(MS._SequenceAdapter, "release", release)
    grun([tsai_cell(world), darts_cell(world)], tmp_path / "o", world)
    assert calls.count("tsai") == 2 and calls.count("darts") == 2             # one per fitted block


def test_a_failing_fit_still_releases_the_adapter(world, tmp_path, monkeypatch):
    calls = []
    real = MS._SequenceAdapter.release
    monkeypatch.setattr(MS._SequenceAdapter, "release", lambda self: (calls.append(1), real(self))[1])

    def boom(self, *a, **k):
        raise RuntimeError("cuda out of memory")
    monkeypatch.setattr(MS.TsaiAdapter, "fit", boom)
    res = grun([tsai_cell(world)], tmp_path / "o", world)
    assert res["errors"] == 1 and calls


# ================================================================== determinism and the no-look-ahead cut
def _preds(out):
    return pd.read_parquet(next((out / "predictions").glob("*.parquet"))).set_index(["symbol", "session"]).sort_index()


@pytest.mark.parametrize("make, run_fixture", [(tsai_cell, "tsai_run"), (darts_cell, "darts_run")])
def test_the_same_seed_and_data_give_an_identical_row(world, tmp_path, request, make, run_fixture):
    first_out, _, first_rows = request.getfixturevalue(run_fixture)
    out = tmp_path / "again"
    grun([make(world)], out, world)
    again = G.read_results(out / "results.jsonl")[0]
    assert again["primary"]["value"] == pytest.approx(first_rows[0]["primary"]["value"], abs=1e-5)
    a, b = _preds(first_out), _preds(out)
    assert a.index.equals(b.index)
    np.testing.assert_allclose(a["score"].to_numpy(), b["score"].to_numpy(), atol=1e-5)
    assert [x["n_samples"] if "n_samples" in x else x["n_fit"] for x in again["blocks"]] == \
        [x["n_samples"] if "n_samples" in x else x["n_fit"] for x in first_rows[0]["blocks"]]


def test_a_different_seed_gives_a_different_model(world, tmp_path, tsai_run):
    out = tmp_path / "s2"
    grun([tsai_cell(world, seeds=[2])], out, world)
    a, b = _preds(tsai_run[0]), _preds(out)
    common = a.index.intersection(b.index)
    assert len(common) > 100 and not np.allclose(a.loc[common, "score"], b.loc[common, "score"], atol=1e-4)


@pytest.mark.parametrize("make, run_fixture", [(tsai_cell, "tsai_run"), (darts_cell, "darts_run")])
def test_nothing_after_a_test_blocks_end_changes_that_blocks_predictions(world, tmp_path, request, make, run_fixture):
    """Truncate the daily and the 5-minute data after the first test block's end and rerun: the block's scores of the
    sessions both runs score are the same (the model never saw a bar after the block's start; the features are causal)."""
    full_out = request.getfixturevalue(run_fixture)[0]
    cut_world = truncate_world(world, CUT, tmp_path / "cut")
    out = tmp_path / "cutrun"
    grun([make(cut_world)], out, cut_world, settings=_gsettings(eval_end=CUT))
    cut_rows = G.read_results(out / "results.jsonl")
    assert cut_rows and cut_rows[0]["status"] == "ok" and cut_rows[0]["n_blocks"] == 1
    a = _preds(full_out)
    a = a[a["block"] == cut_rows[0]["blocks"][0]["block"]]
    b = _preds(out)
    common = a.index.intersection(b.index)
    assert len(common) >= 0.6 * len(b) > 50                                    # only the last horizon's rows are missing
    np.testing.assert_allclose(a.loc[common, "score"].to_numpy(), b.loc[common, "score"].to_numpy(), atol=1e-5)


# ================================================================== resume
def test_a_finished_gpu_row_is_reused_and_a_changed_seq_setting_is_a_mismatch_for_gpu_rows_only(world, tmp_path):
    out = tmp_path / "o"
    cells = [tsai_cell(world), lgbm_cell(world)]
    first = grun(cells, out, world)
    assert first["done"] == 2
    assert grun(cells, out, world)["tasks"] == 0                              # nothing left
    with pytest.raises(G.ResumeMismatchError, match="seq_lookback"):
        grun(cells, out, world, settings=_gsettings(seq_lookback=21))
    res = grun([lgbm_cell(world)], out, world, settings=_gsettings(seq_lookback=21))   # a tabular row is not affected
    assert res["tasks"] == 0
    stale = grun(cells, out, world, settings=_gsettings(seq_lookback=21), rerun_mismatched=True)
    assert stale["done"] == 1                                                  # only the gpu row was recomputed


# ================================================================== the pools
def test_both_pools_run_concurrently_in_one_grid(world, tmp_path):
    out = tmp_path / "o"
    res = grun([lgbm_cell(world), tsai_cell(world)], out, world, inline=False, cpu_slots=1, gpu_slots=1)
    assert res["done"] == 2 and res["errors"] == 0 and res["pool_rebuilds"] == 0
    rows = {r["config"]["family"]: r for r in G.read_results(out / "results.jsonl")}
    assert set(rows) == {"lgbm", "tsai-inception"} and all(r["status"] == "ok" for r in rows.values())
    assert rows["tsai-inception"]["versions"]["python"]                        # a spawned worker wrote it


def test_gpu_tasks_need_gpu_slots(world, tmp_path):
    with pytest.raises(ValueError, match="gpu pool"):
        grun([tsai_cell(world)], tmp_path / "o", world, gpu_slots=0)


def die_on_gpu_task(cfg, seed, expected_key):
    if cfg["family"].startswith(("tsai", "darts")):
        os._exit(3)
    return G._run_task(cfg, seed, expected_key)


def test_a_worker_that_dies_on_the_gpu_pool_is_isolated_from_the_cpu_pool(world, tmp_path):
    out = tmp_path / "o"
    res = grun([lgbm_cell(world), tsai_cell(world)], out, world, inline=False, cpu_slots=1, gpu_slots=1,
               task_fn=die_on_gpu_task)
    assert res["done"] == 1 and res["errors"] == 1 and res["pool_rebuilds"] >= 4
    rows = G.read_results(out / "results.jsonl")
    assert [r["config"]["family"] for r in rows] == ["lgbm"]                   # the cpu cell was untouched
    err = G.read_results(out / "errors.jsonl")
    assert len(err) == 1 and "worker process died" in err[0]["error"] and "3 retries" in err[0]["error"]


def test_a_cancel_stops_both_pools(world, tmp_path):
    out = tmp_path / "o"
    res = grun([lgbm_cell(world), tsai_cell(world), darts_cell(world)], out, world, inline=False, cpu_slots=1,
               gpu_slots=1, cancel_check=lambda: True)
    assert res["cancelled"] and res["done"] == 0


def test_a_failure_in_one_lane_stops_the_other_and_propagates(world, tmp_path, monkeypatch):
    real = G._drive_pool

    def drive(tasks, slots, init_args, task_fn, finish, cancelled, summary, *a, **k):
        if tasks and tasks[0][0].pool == "gpu":
            raise OSError("disk full")
        return real(tasks, slots, init_args, task_fn, finish, cancelled, summary, *a, **k)
    monkeypatch.setattr(G, "_drive_pool", drive)
    with pytest.raises(OSError, match="disk full"):
        grun([lgbm_cell(world), tsai_cell(world)], tmp_path / "o", world, inline=False, cpu_slots=1, gpu_slots=1)


# ================================================================== the darts target series
def shock_world(world, root: Path, seed: int = 5):
    """A copy of ``world`` whose DAILY bars of every symbol carry the same market shock: each session's open / high /
    low / close of every symbol is multiplied by one common factor (a random walk's level). The store and the 5-minute
    bars are untouched."""
    stores, _, cache = world
    new_cache, new_stores = root / "cache", root / "stores"
    shutil.copytree(cache, new_cache)
    shutil.copytree(stores, new_stores)
    rng = np.random.default_rng(seed)
    level = None
    for f in sorted((new_cache / "AlpacaHistoryOHLCVProvider").glob("*_1d.parquet")):
        df = pd.read_parquet(f)
        if level is None:
            level = np.exp(np.cumsum(rng.normal(0, 0.02, len(df))))             # one factor per session, shared
        for c in ("Open", "High", "Low", "Close"):
            df[c] = df[c].to_numpy() * level
        df.to_parquet(f, index=False)
    return new_stores, new_stores / "stocks", new_cache


def _daily_panel(world):
    return G.get_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))


def _seq(panel, neutral):
    cols = panel.group_columns(("t",))
    fit = np.flatnonzero(panel.ok)[:600]
    return S.daily_sequence_data(panel, cols, fit, 10, 5, neutral=neutral)


def test_the_rel_return_series_is_the_log_return_minus_the_decidable_universes_mean(world):
    panel = _daily_panel(world)
    own, neutral = _seq(panel, False), _seq(panel, True)
    n_sym, n_s = len(panel.symbols), len(panel.arrays.sessions)
    r_own = own.returns.reshape(n_sym, n_s)
    r_neu = neutral.returns.reshape(n_sym, n_s)
    a = panel.arrays
    member = (a.tradable & np.isfinite(a.atr_pct)).T                              # the ml_targets membership at S
    assert member[:, 60:].all()                                                   # past the ATR warm-up every name counts
    t = 100
    np.testing.assert_allclose(r_neu[:, t], r_own[:, t] - r_own[member[:, t], t].mean(), atol=1e-12)
    assert abs(r_neu[:, 80:].mean(axis=0)).max() < 1e-12                           # the session mean is removed
    assert np.abs(r_neu - r_own).max() > 1e-4                                      # and it is not the own series
    assert neutral.series_start.tolist() == own.series_start.tolist() and neutral.horizon_steps == 5


def test_a_common_market_shock_leaves_the_rel_return_series_unchanged_and_moves_the_own_series(world, tmp_path):
    shocked = shock_world(world, tmp_path / "shock")
    a, b = _daily_panel(world), _daily_panel(shocked)
    np.testing.assert_allclose(_seq(a, True).returns, _seq(b, True).returns, atol=1e-9)
    assert np.abs(_seq(a, False).returns - _seq(b, False).returns).max() > 1e-3     # the timing series feels the shock


def test_a_common_market_shock_leaves_the_rel_return_scores_unchanged(world, tmp_path, darts_run):
    shocked = shock_world(world, tmp_path / "shock")
    out = tmp_path / "o"
    grun([darts_cell(shocked)], out, shocked)
    a, b = _preds(darts_run[0]), _preds(out)
    assert a.index.equals(b.index) and len(a) > 100
    np.testing.assert_allclose(a["score"].to_numpy(), b["score"].to_numpy(), atol=1e-4)
    assert G.read_results(out / "results.jsonl")[0]["blocks"][0]["target_series"] == "market_neutral"


def test_the_quantile_kind_keeps_the_own_return_series(world, tmp_path, monkeypatch):
    seen = []
    real = S.daily_sequence_data
    monkeypatch.setattr(S, "daily_sequence_data", lambda *a, **k: (seen.append(k["neutral"]), real(*a, **k))[1])
    out = tmp_path / "o"
    grun([darts_cell(world, target={"kind": "quantile", "horizon": 5})], out, world)
    assert seen and not any(seen)
    assert G.read_results(out / "results.jsonl")[0]["blocks"][0]["target_series"] == "own_return"


def test_the_rel_return_kind_asks_for_the_neutral_series(world, tmp_path, monkeypatch):
    seen = []
    real = S.daily_sequence_data
    monkeypatch.setattr(S, "daily_sequence_data", lambda *a, **k: (seen.append(k["neutral"]), real(*a, **k))[1])
    grun([darts_cell(world)], tmp_path / "o", world)
    assert seen and all(seen)


def test_a_gpu_block_runs_with_deterministic_cudnn(world, tmp_path):
    """On the GPU the same seed only repeats bit for bit with cudnn.deterministic and no autotuning (the adapters leave
    these process-wide flags to the caller: the grid's GPU blocks set them)."""
    import torch
    torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark = False, True
    grun([tsai_cell(world)], tmp_path / "o", world)
    assert torch.backends.cudnn.deterministic is True and torch.backends.cudnn.benchmark is False
    assert torch.are_deterministic_algorithms_enabled() and os.environ["CUBLAS_WORKSPACE_CONFIG"]
