"""GPU families of the stage-1 grid (Task 4.4b): ``tsai-<arch>`` on the intraday 1h panel and ``darts-<arch>`` on the
daily store frame, run in a second process pool; on tiny synthetic data, CPU torch (``device: "cpu"``)."""
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
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
    meta = {"symbols": list(GSYMS), "universe": list(GSYMS), "purpose": "dev", "membership": None,
            "first_day": str(FIRST_G), "last_day": str(LAST_G), "cache_end": str(LAST_G),
            "ensure": {"min_share": 0.7, "inputs": {"code": F.code_version(), "fingerprints": {
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


# ================================================================== review round 3: a multi-day GPU run
class _Exited(BaseException):
    def __init__(self, code):
        self.code = code


def _worker_task(world, tmp_path, cell_cfg=None, settings=None, seed=1):
    """Initialise this process as a grid worker and run one task through ``_run_task`` (as a pool worker would)."""
    st = settings or _gsettings()
    c = G.validate_cell(cell_cfg or tsai_cell(world))
    G._init_worker(st.to_dict(), str(world[2]), str(tmp_path), 1)
    G._WORKER["poisoned"] = False
    return c, (lambda: G._run_task(c.config(with_seeds=True), seed, c.key))


def _failing_fit(monkeypatch, exc):
    def boom(self, *a, **k):
        raise exc
    monkeypatch.setattr(MS.TsaiAdapter, "fit", boom)


# ---- 1. cleanup after a failure
def test_the_gpu_worker_cleans_up_after_the_exception_has_left_its_except_block(world, tmp_path, monkeypatch):
    import sys
    seen = []
    monkeypatch.setattr(S, "cleanup_gpu", lambda: seen.append(sys.exc_info()[0]))
    _failing_fit(monkeypatch, RuntimeError("boom"))
    c, run_task = _worker_task(world, tmp_path)
    rec = run_task()
    assert "error" in rec and "boom" in rec["error"]
    assert seen == [None]                                    # called once, with no exception (and so no traceback) alive
    seen.clear()
    cc, run_ok = _worker_task(world, tmp_path, lgbm_cell(world))
    assert "error" not in run_ok() and seen == []            # a tabular task does not touch the GPU


def test_cleanup_gpu_collects_and_empties_the_cuda_cache(monkeypatch):
    import torch
    calls = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: calls.append("empty"))
    import gc
    monkeypatch.setattr(gc, "collect", lambda *a: calls.append("gc") or 0)
    S.cleanup_gpu()
    assert calls == ["gc", "empty"]


def test_a_failing_release_never_masks_the_fit_error(world, tmp_path, monkeypatch):
    _failing_fit(monkeypatch, RuntimeError("fit failed"))
    monkeypatch.setattr(MS._SequenceAdapter, "release", lambda self: (_ for _ in ()).throw(RuntimeError("release failed")))
    c, run_task = _worker_task(world, tmp_path)
    rec = run_task()
    assert "fit failed" in rec["error"] and "release failed" not in rec["error"]


def test_a_failing_release_after_a_good_fit_costs_nothing(world, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(MS._SequenceAdapter, "release", lambda self: (_ for _ in ()).throw(RuntimeError("release failed")))
    out = tmp_path / "o"
    with caplog.at_level("WARNING"):
        res = grun([tsai_cell(world)], out, world)
    assert res["done"] == 1 and res["errors"] == 0
    assert any("releasing" in r.getMessage() and r.exc_info and "release failed" in str(r.exc_info[1])
               for r in caplog.records)


# ---- 2. sticky CUDA errors
def test_cuda_errors_are_classified_as_recoverable_or_sticky():
    import torch
    assert S.classify_gpu_error(torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 2 GiB")) == "oom"
    assert S.classify_gpu_error(RuntimeError("CUDA out of memory. Tried to allocate 2 GiB")) == "oom"
    for text in ("CUDA error: an illegal memory access was encountered", "cuDNN error: CUDNN_STATUS_EXECUTION_FAILED",
                 "CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate", "device-side assert triggered"):
        assert S.classify_gpu_error(RuntimeError(text)) == "sticky", text
    assert S.classify_gpu_error(ValueError("the validation loss was never finite")) is None
    assert S.classify_gpu_error(KeyError("x")) is None
    try:                                                      # a sticky error behind a wrapper is found in the chain
        try:
            raise RuntimeError("CUDA error: unspecified launch failure")
        except RuntimeError as inner:
            raise ValueError("fit failed") from inner
    except ValueError as outer:
        assert S.classify_gpu_error(outer) == "sticky"


def test_a_sticky_cuda_error_poisons_the_worker_and_its_next_task_exits(world, tmp_path, monkeypatch):
    _failing_fit(monkeypatch, RuntimeError("CUDA error: device-side assert triggered"))
    c, run_task = _worker_task(world, tmp_path)
    rec = run_task()
    assert rec["cuda_error"] == "sticky" and G._WORKER["poisoned"] is True
    monkeypatch.setattr(G.os, "_exit", lambda code: (_ for _ in ()).throw(_Exited(code)))
    ran = []
    monkeypatch.setattr(G, "run_cell_seed", lambda *a, **k: ran.append(1))
    with pytest.raises(_Exited) as e:
        run_task()
    assert e.value.code == 1 and not ran                       # it left before touching the task


def test_an_out_of_memory_error_is_recoverable(world, tmp_path, monkeypatch):
    import torch
    _failing_fit(monkeypatch, torch.cuda.OutOfMemoryError("CUDA out of memory"))
    c, run_task = _worker_task(world, tmp_path)
    rec = run_task()
    assert rec["cuda_error"] == "oom" and G._WORKER["poisoned"] is False


def test_an_ordinary_error_carries_no_cuda_flag(world, tmp_path, monkeypatch):
    _failing_fit(monkeypatch, ValueError("plain"))
    c, run_task = _worker_task(world, tmp_path)
    rec = run_task()
    assert "cuda_error" not in rec and G._WORKER["poisoned"] is False


def _err(key, seed, cuda=None):
    r = G._error_record(key, seed, "boom", "l")
    if cuda:
        r["cuda_error"] = cuda
    return r


def test_the_breaker_trips_after_ten_consecutive_cuda_errors_and_a_success_resets_it():
    b = G.GpuBreaker(10)
    assert not any(b.record(_err("k", i, "sticky")) for i in range(9))
    assert b.record({"cell_key": "k", "seed": 1, "primary": {}}) is False          # a good row resets the count
    assert not any(b.record(_err("k", i, "oom")) for i in range(9))
    assert b.record(_err("k", 99, "oom")) is True and b.tripped
    b3 = G.GpuBreaker(3)
    b3.record(_err("k", 1, "oom"))
    b3.record(_err("k", 2))                                    # an ordinary error neither counts nor resets
    b3.record(_err("k", 3, "oom"))
    assert b3.record(_err("k", 4, "oom")) is True


def _cells(world):
    return ([(G.validate_cell(lgbm_cell(world)), 1)], [(G.validate_cell(tsai_cell(world)), 1)])


def test_the_gpu_lane_is_aborted_loudly_after_ten_consecutive_cuda_errors_and_the_cpu_lane_goes_on(world, monkeypatch,
                                                                                                    caplog):
    cpu_t, gpu_t = _cells(world)
    fed = {"gpu": 0, "cpu": 0}

    def drive(tasks, slots, init_args, task_fn, finish, cancelled, summary, *a, **k):
        name = "gpu" if tasks[0][0].pool == "gpu" else "cpu"
        if name == "cpu":
            for i in range(3):
                finish({"cell_key": "c", "seed": i, "primary": {"name": "x", "value": 1.0}})
                fed["cpu"] += 1
            return
        while not cancelled() and fed["gpu"] < 50:
            finish(_err("g", fed["gpu"], "sticky"))
            fed["gpu"] += 1
    monkeypatch.setattr(G, "_drive_pool", drive)
    rows = []
    summary = {"pool_rebuilds": 0, "cancelled": False}
    with caplog.at_level("ERROR"):
        G._drive_lanes(cpu_t, gpu_t, 1, 1, (), None, rows.append, lambda: False, summary)
    assert fed["gpu"] == 10 and fed["cpu"] == 3                                  # stopped right at the tenth
    assert summary["gpu_lane_aborted"] is True and summary["cancelled"] is False
    assert any("GPU lane" in r.getMessage() and "10" in r.getMessage() for r in caplog.records)
    assert len(rows) == 13


# ---- 3. no silent CPU
def test_device_auto_without_cuda_in_a_gpu_worker_is_an_error_not_a_cpu_run(world, tmp_path, monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    p = {k: v for k, v in TINY_NET.items() if k != "device"}                    # device defaults to "auto"
    c, run_task = _worker_task(world, tmp_path, tsai_cell(world, params=p))
    rec = run_task()
    assert "error" in rec and "CUDA is not available" in rec["error"] and "device" in rec["error"]
    c2, run_task2 = _worker_task(world, tmp_path, darts_cell(world, params={k: v for k, v in TINY_NET.items()
                                                                           if k != "device"}))
    assert "CUDA is not available" in run_task2()["error"]


def test_an_explicit_cpu_device_still_runs_on_the_cpu(world, tmp_path):
    out = tmp_path / "o"
    grun([tsai_cell(world)], out, world)
    assert all(b["device"] == "cpu" for b in G.read_results(out / "results.jsonl")[0]["blocks"])


# ---- 4 / 5. the time budget
import time as _real_time


class FakeClock:
    """``time`` look-alike for ml_grid / ml_grid_seq: perf_counter = the real one + an offset the tests advance."""

    def __init__(self):
        self.offset = 0.0

    def perf_counter(self):
        return _real_time.perf_counter() + self.offset

    def __getattr__(self, name):
        return getattr(_real_time, name)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(G, "time", c)
    monkeypatch.setattr(S, "time", c)
    return c


def _slow_blocks(monkeypatch, clock, costs):
    """Make every fitted block "take" ``costs[k]`` seconds of the fake clock."""
    real = G.fit_block
    n = {"k": 0}

    def wrapped(*a, **k):
        res = real(*a, **k)
        clock.offset += costs[min(n["k"], len(costs) - 1)]
        n["k"] += 1
        return res
    monkeypatch.setattr(G, "fit_block", wrapped)


def _run_one(world, tmp_path, minutes, **cfg):
    out = tmp_path / "o"
    res = grun([tsai_cell(world, **cfg)], out, world, settings=_gsettings(gpu_budget_minutes=minutes))
    return res, out


def test_a_row_that_finished_over_budget_is_kept_and_marked(world, tmp_path, monkeypatch, clock):
    _slow_blocks(monkeypatch, clock, [40.0, 25.0])                    # 65 s of a 60 s budget, the last block ends it
    res, out = _run_one(world, tmp_path, 1.0)
    assert res["done"] == 1 and res["errors"] == 0
    row = G.read_results(out / "results.jsonl")[0]
    assert row["status"] == "ok" and row["over_budget"] is True


def test_a_row_inside_the_budget_is_not_marked(world, tmp_path, monkeypatch, clock):
    _slow_blocks(monkeypatch, clock, [10.0, 10.0])
    res, out = _run_one(world, tmp_path, 1.0)
    assert G.read_results(out / "results.jsonl")[0]["over_budget"] is False


def test_the_projection_needs_two_fitted_blocks_and_counts_only_fitted_time():
    assert G.budget_projection(50.0, [50.0], 5) is None                          # one block: no rate yet (cold start)
    assert G.budget_projection(0.0, [], 5) is None
    assert G.budget_projection(30.0, [10.0, 20.0], 2) == pytest.approx(30.0 + 15.0 * 2)
    assert G.budget_projection(30.0, [10.0, 20.0], 0) == pytest.approx(30.0)


def _six_blocks(monkeypatch, repeats):
    """The real blocks followed by ``repeats`` copies of the last one (the world has only 2-3 half-year blocks)."""
    real = G.W.blocks

    def blocks(*a, **k):
        b = list(real(*a, **k))
        return b + [b[-1]] * repeats
    monkeypatch.setattr(G.W, "blocks", blocks)


def test_the_projection_aborts_the_task_when_the_remaining_blocks_will_not_fit_1_5_times_the_budget(
        world, tmp_path, monkeypatch, clock):
    _six_blocks(monkeypatch, 4)                          # 6 blocks in all
    _slow_blocks(monkeypatch, clock, [20.0, 20.0])       # 2 fitted: 40 s so far, 20 s each, 4 to go: 120 s > 1.5 x 60 s
    res, out = _run_one(world, tmp_path, 1.0)
    assert res["done"] == 0 and res["errors"] == 1
    err = G.read_results(out / "errors.jsonl")[0]
    assert "time budget" in err["error"] and "projected" in err["error"] and err["timeout"] is True
    assert "after 2 of 6" in err["error"]


def test_skipped_blocks_do_not_enter_the_rate_or_the_remaining_count(world, tmp_path, monkeypatch, clock):
    _six_blocks(monkeypatch, 3)                          # 5 blocks: the first is skipped, then 2 fitted, 2 still to come
    real = G.fit_block
    n = {"k": 0}

    def wrapped(*a, **k):
        n["k"] += 1
        if n["k"] == 1:
            raise G.SkipBlock("no data")                 # costs nothing
        res = real(*a, **k)
        clock.offset += 20.0
        return res
    monkeypatch.setattr(G, "fit_block", wrapped)
    res, out = _run_one(world, tmp_path, 1.0)
    # 40 s of fitted work, 20 s a block, 2 blocks to come: 80 s <= 90 s -> it goes on (the old formula divided the
    # elapsed time by the 3 processed blocks and multiplied by all 5: 67 s -- and a mean over skipped blocks is noise)
    assert res["errors"] == 0 or "projected" not in G.read_results(out / "errors.jsonl")[0]["error"]


def test_a_slow_cold_start_does_not_abort_the_task(world, tmp_path, monkeypatch, clock):
    _slow_blocks(monkeypatch, clock, [50.0, 3.0, 3.0])   # the first block pays the CUDA warm-up; 3 blocks
    out = tmp_path / "o"
    res = grun([tsai_cell(world)], out, world, settings=_gsettings(gpu_budget_minutes=1.0, eval_start=date(2019, 7, 1)))
    assert res["done"] == 1 and res["errors"] == 0, G.read_results(out / "errors.jsonl")
    row = G.read_results(out / "results.jsonl")[0]
    assert row["n_blocks"] == 3 and row["n_blocks_fitted"] == 3                    # the old rule projected 150 s and aborted


def test_a_projection_under_1_5_times_the_budget_lets_the_task_finish(world, tmp_path, monkeypatch, clock):
    _slow_blocks(monkeypatch, clock, [40.0, 1.0])       # 40 s for 1 of 2 blocks: 80 s projected <= 90 s, 41 s in total
    res, out = _run_one(world, tmp_path, 1.0)
    assert res["done"] == 1 and G.read_results(out / "results.jsonl")[0]["over_budget"] is False


def test_the_overrun_before_a_block_starts_is_a_timeout_error(world, tmp_path, monkeypatch, clock):
    _slow_blocks(monkeypatch, clock, [70.0, 1.0])       # already over the budget when block 2 would start
    res, out = _run_one(world, tmp_path, 1.0)
    err = G.read_results(out / "errors.jsonl")[0]
    assert res["errors"] == 1 and err["timeout"] is True and "after 1 of 2 blocks" in err["error"]


def test_building_the_intraday_panel_does_not_count_against_the_budget(world, tmp_path, monkeypatch, clock):
    S._INTRADAY.clear()
    real_build = S.MP.build_panel

    def slow_build(*a, **k):
        clock.offset += 1000.0                                        # the panel build "takes" 1000 s
        return real_build(*a, **k)
    monkeypatch.setattr(S.MP, "build_panel", slow_build)
    res, out = _run_one(world, tmp_path, 1.0)
    assert res["done"] == 1 and res["errors"] == 0
    row = G.read_results(out / "results.jsonl")[0]
    assert row["over_budget"] is False
    assert row["blocks"][0]["intraday_build_seconds"] >= 1000.0 and row["blocks"][1]["intraday_build_seconds"] == 0.0
    assert row["timings"]["intraday_build"] >= 1000.0


def test_block_timings_separate_the_build_the_preparation_and_the_fit(world, tmp_path):
    S._INTRADAY.clear()
    out = tmp_path / "o"
    grun([tsai_cell(world)], out, world)
    row = G.read_results(out / "results.jsonl")[0]
    first, second = row["blocks"]
    for b in (first, second):
        assert b["fit_seconds"] > 0 and b["prep_seconds"] > 0 and b["intraday_build_seconds"] >= 0
    assert first["intraday_build_seconds"] > 0 and second["intraday_build_seconds"] == 0     # built once, then cached
    assert row["timings"]["intraday_build"] == pytest.approx(first["intraday_build_seconds"])
    assert row["timings"]["block_fit"] == pytest.approx(first["fit_seconds"] + second["fit_seconds"])
    assert row["timings"]["block_prep"] == pytest.approx(first["prep_seconds"] + second["prep_seconds"])


def test_a_tabular_row_has_no_budget_fields(world, tmp_path):
    out = tmp_path / "o"
    grun([lgbm_cell(world)], out, world)
    row = G.read_results(out / "results.jsonl")[0]
    assert "over_budget" not in row and "block_fit" not in row["timings"]


# ---- 5. timeouts are not retried by a restart; the master's hard deadline
def timeout_task(cfg, seed, key):
    if cfg["family"].startswith("tsai"):
        return {**G._error_record(key, seed, "BudgetExceeded: time budget of 1 minutes exceeded", "l"), "timeout": True}
    return G._run_task(cfg, seed, key)


def test_a_restart_skips_timed_out_tasks_unless_asked_to_retry_them(world, tmp_path):
    out = tmp_path / "o"
    cells = [lgbm_cell(world), tsai_cell(world)]
    first = grun(cells, out, world, task_fn=timeout_task)
    assert first["done"] == 1 and first["errors"] == 1
    again = grun(cells, out, world, task_fn=timeout_task)
    assert again["tasks"] == 0 and again["skipped_timeouts"] == 1 and again["errors"] == 0
    retry = grun(cells, out, world, task_fn=timeout_task, retry_timeouts=True)
    assert retry["tasks"] == 1 and retry["errors"] == 1 and "skipped_timeouts" not in retry
    ok = grun(cells, out, world, retry_timeouts=True)                          # the real task this time
    assert ok["done"] == 1 and len(G.read_results(out / "results.jsonl")) == 2
    assert grun(cells, out, world)["tasks"] == 0                              # finished: nothing to skip or retry


def test_an_ordinary_error_is_still_retried_by_a_restart(world, tmp_path):
    out = tmp_path / "o"

    def fail(cfg, seed, key):
        return G._error_record(key, seed, "ValueError: x", "l")
    # (module-level not needed: inline runs in this process)
    first = grun([tsai_cell(world)], out, world, task_fn=fail)
    assert first["errors"] == 1
    assert grun([tsai_cell(world)], out, world, task_fn=fail)["tasks"] == 1


def test_the_retry_timeouts_flag_reaches_the_cli_the_route_and_the_payload(monkeypatch, tmp_path):
    import types
    import ba2test_launcher as L
    import app.services.ml_grid as grid
    from app.api.ml_search import MlGridRequest
    assert MlGridRequest.model_fields["retry_timeouts"].default is False
    assert "retry_timeouts" in G.PAYLOAD_KEYS
    seen = {}
    monkeypatch.setattr(grid, "handle_ml_grid", lambda tid, payload: (seen.update(payload), {
        "done": 0, "errors": 0, "skipped_existing": 0, "cancelled": False, "out_dir": str(tmp_path)})[1])
    L.main(["ml-grid", "--config", "c.json", "--out", str(tmp_path), "--cpu-slots", "1"])
    assert seen["retry_timeouts"] is False
    L.main(["ml-grid", "--config", "c.json", "--out", str(tmp_path), "--cpu-slots", "1", "--retry-timeouts"])
    assert seen["retry_timeouts"] is True


def slow_or_quick_task(cfg, seed, key):
    """tsai-inception hangs (a stuck kernel); everything else returns at once."""
    if cfg["family"] == "tsai-inception":
        _real_time.sleep(300)
    return {"cell_key": key, "seed": seed, "track": cfg["track"], "label": G._label_of(cfg), "status": "ok", "n_rows": 1,
            "skipped_blocks": [], "timings": {"total": 0.1}, "primary": {"name": "quintile_spread_net", "value": 0.0},
            "controls": {}, "config": {k: v for k, v in cfg.items() if k != "seeds"}}


def test_a_task_stuck_twice_is_retried_once_and_then_recorded_as_a_timeout(world, tmp_path):
    out = tmp_path / "o"
    cells = [tsai_cell(world), tsai_cell(world, family="tsai-minirocket", params={"device": "cpu"})]
    t0 = _real_time.time()
    res = grun(cells, out, world, inline=False, cpu_slots=1, gpu_slots=2, task_fn=slow_or_quick_task,
               settings=_gsettings(gpu_budget_minutes=0.1))                  # hard deadline: 3 x 6 s = 18 s, twice
    assert _real_time.time() - t0 < 150                                       # not the 300 s of the stuck kernel
    assert res["done"] == 1 and res["errors"] == 1 and res["pool_rebuilds"] >= 2
    errs = G.read_results(out / "errors.jsonl")
    key = G.validate_cell(tsai_cell(world)).key
    assert len(errs) == 2 and all(e["cell_key"] == key and e["hard_deadline"] is True for e in errs)
    assert "timeout" not in errs[0] and errs[0]["retrying"] is True                # the first one: a retry follows
    assert errs[1]["timeout"] is True and "retrying" not in errs[1]                # the second: given up
    assert "hard deadline" in errs[1]["error"]
    done = G.read_results(out / "results.jsonl")
    assert [r["config"]["family"] for r in done] == ["tsai-minirocket"]       # the innocent task still finished
    again = grun([tsai_cell(world)], out, world, task_fn=slow_or_quick_task_ok)     # a restart skips the timeout
    assert again["tasks"] == 0 and again["skipped_timeouts"] == 1


def hang_once_task(cfg, seed, key):
    """The first run of tsai-inception hangs (a transient driver hang), the retry returns at once."""
    if cfg["family"] == "tsai-inception":
        marker = Path(os.environ["HANG_ONCE_MARKER"])
        if not marker.exists():
            marker.write_text("hung", encoding="utf-8")
            _real_time.sleep(300)
    return slow_or_quick_task({**cfg, "family": "x"}, seed, key)


def test_a_transient_hang_is_absorbed_by_the_one_automatic_retry(world, tmp_path, monkeypatch):
    monkeypatch.setenv("HANG_ONCE_MARKER", str(tmp_path / "hung"))
    out = tmp_path / "o"
    res = grun([tsai_cell(world)], out, world, inline=False, cpu_slots=1, gpu_slots=1, task_fn=hang_once_task,
               settings=_gsettings(gpu_budget_minutes=0.1))
    assert res["done"] == 1 and res["errors"] == 0 and res["pool_rebuilds"] >= 1
    errs = G.read_results(out / "errors.jsonl")
    assert len(errs) == 1 and errs[0]["hard_deadline"] is True and errs[0]["retrying"] is True
    assert "timeout" not in errs[0]
    assert len(G.read_results(out / "results.jsonl")) == 1


def test_a_run_that_dies_after_the_first_hard_deadline_retries_it_on_restart(world, tmp_path):
    out = tmp_path / "o"
    key = G.validate_cell(tsai_cell(world)).key
    out.mkdir()
    (out / "errors.jsonl").write_text(json.dumps({"cell_key": key, "seed": 1, "error": "hard deadline", "label": "l",
                                                  "hard_deadline": True, "retrying": True}) + "\n", encoding="utf-8")
    assert grun([tsai_cell(world)], out, world)["tasks"] == 1                  # not a timeout: it runs again
# ---- 6. cancel, Ctrl-C, lanes
class SnapshotPool(G.ProcessPoolExecutor):
    """A real pool that remembers its worker processes and the keyword arguments it was built with."""
    instances = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.kwargs = k
        self.procs = []
        SnapshotPool.instances.append(self)

    def submit(self, *a, **k):
        fu = super().submit(*a, **k)
        self.procs = list(self._processes.values())
        return fu


def sleeping_task(cfg, seed, key):
    _real_time.sleep(300)


def test_cancelling_terminates_the_workers_instead_of_leaving_a_stuck_task_running(world, tmp_path, monkeypatch):
    SnapshotPool.instances = []
    monkeypatch.setattr(G, "ProcessPoolExecutor", SnapshotPool)
    t0 = _real_time.time()
    out = tmp_path / "o"
    res = grun([lgbm_cell(world)], out, world, inline=False, cpu_slots=1, task_fn=sleeping_task,
               cancel_check=lambda: _real_time.time() - t0 > 6)
    assert res["cancelled"] and _real_time.time() - t0 < 90
    procs = [p for pool in SnapshotPool.instances for p in pool.procs]
    assert procs
    for p in procs:
        p.join(20)
        assert not p.is_alive()                                                # killed, not left sleeping for 300 s


def test_a_master_failure_terminates_the_workers_too(world, tmp_path, monkeypatch):
    SnapshotPool.instances = []
    monkeypatch.setattr(G, "ProcessPoolExecutor", SnapshotPool)

    def boom_finish(*a, **k):
        raise OSError("disk full")
    t0 = _real_time.time()
    cells = [lgbm_cell(world), lgbm_cell(world, train_months=24)]
    monkeypatch.setattr(G, "_append", lambda *a, **k: boom_finish())
    with pytest.raises(OSError):
        grun(cells, tmp_path / "o", world, inline=False, cpu_slots=2, task_fn=quick_task)
    for p in [p for pool in SnapshotPool.instances for p in pool.procs]:
        p.join(20)
        assert not p.is_alive()
    assert _real_time.time() - t0 < 90


def quick_task(cfg, seed, key):
    return slow_or_quick_task({**cfg, "family": "lgbm"}, seed, key)


class _Lane:
    """A fake ``_drive_pool``: calls ``finish`` for its rows and loops until it is told to stop."""

    def __init__(self, name, rows=0, fail_after=None, error=None):
        self.name, self.rows, self.fail_after, self.error = name, rows, fail_after, error
        self.exited = False
        self.saw_stop = False

    def __call__(self, tasks, slots, init_args, task_fn, finish, cancelled, summary, *a, **k):
        try:
            summary["pool_rebuilds"] = 1
            for i in range(self.rows):
                finish({"cell_key": self.name, "seed": i, "primary": {"name": "x", "value": 1.0}})
            if self.fail_after is not None:
                _real_time.sleep(self.fail_after)
                raise self.error
            while not cancelled():
                _real_time.sleep(0.01)
            self.saw_stop = True
        finally:
            _real_time.sleep(0.2)                                              # a lane takes a moment to wind down
            self.exited = True


def _lane_tasks(world):
    return _cells(world)


def test_finish_calls_from_both_lanes_never_overlap(world, monkeypatch):
    cpu_t, gpu_t = _lane_tasks(world)
    state = {"active": 0, "max": 0, "calls": 0}

    def finish(r):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        _real_time.sleep(0.0005)
        state["calls"] += 1
        state["active"] -= 1
    monkeypatch.setattr(G, "_drive_pool", lambda tasks, *a, **k: _finishing_lane(tasks, *a, **k))
    summary = {"pool_rebuilds": 0, "cancelled": False}
    G._drive_lanes(cpu_t, gpu_t, 1, 1, (), None, finish, lambda: False, summary)
    assert state["calls"] == 300 and state["max"] == 1 and summary["pool_rebuilds"] == 2


def _finishing_lane(tasks, slots, init_args, task_fn, finish, cancelled, summary, *a, **k):
    summary["pool_rebuilds"] = 1
    for i in range(150):
        finish({"cell_key": tasks[0][0].pool, "seed": i, "primary": {"name": "x", "value": 1.0}})


def test_ctrl_c_waits_for_the_lanes_to_wind_down_before_it_propagates(world, monkeypatch):
    cpu_t, gpu_t = _lane_tasks(world)
    lanes = {"cpu": _Lane("cpu"), "gpu": _Lane("gpu")}
    monkeypatch.setattr(G, "_drive_pool", lambda tasks, *a, **k: lanes[tasks[0][0].pool](tasks, *a, **k))
    real_join = G.threading.Thread.join
    n = {"calls": 0}

    def join(self, timeout=None):
        n["calls"] += 1
        if n["calls"] == 1:
            raise KeyboardInterrupt
        return real_join(self, timeout)
    monkeypatch.setattr(G.threading.Thread, "join", join)
    with pytest.raises(KeyboardInterrupt):
        G._drive_lanes(cpu_t, gpu_t, 1, 1, (), None, lambda r: None, lambda: False, {"pool_rebuilds": 0})
    assert all(l.exited and l.saw_stop for l in lanes.values())               # both lanes were stopped and joined


def test_a_lane_that_exits_mid_run_stops_the_other_keeps_its_rows_and_is_re_raised(world, monkeypatch, caplog):
    cpu_t, gpu_t = _lane_tasks(world)
    lanes = {"cpu": _Lane("cpu", rows=5), "gpu": _Lane("gpu", fail_after=0.3, error=OSError("disk full"))}
    monkeypatch.setattr(G, "_drive_pool", lambda tasks, *a, **k: lanes[tasks[0][0].pool](tasks, *a, **k))
    rows = []
    with caplog.at_level("ERROR"):
        with pytest.raises(OSError, match="disk full"):
            G._drive_lanes(cpu_t, gpu_t, 1, 1, (), None, rows.append, lambda: False, {"pool_rebuilds": 0})
    assert len(rows) == 5 and lanes["cpu"].saw_stop and lanes["cpu"].exited and lanes["gpu"].exited


def test_every_lane_error_is_kept_not_only_the_first(world, monkeypatch, caplog):
    cpu_t, gpu_t = _lane_tasks(world)
    lanes = {"cpu": _Lane("cpu", fail_after=0.2, error=ValueError("cpu lane broke")),
             "gpu": _Lane("gpu", fail_after=0.25, error=OSError("gpu lane broke"))}
    monkeypatch.setattr(G, "_drive_pool", lambda tasks, *a, **k: lanes[tasks[0][0].pool](tasks, *a, **k))
    with caplog.at_level("ERROR"):
        with pytest.raises(Exception) as e:
            G._drive_lanes(cpu_t, gpu_t, 1, 1, (), None, lambda r: None, lambda: False, {"pool_rebuilds": 0})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "cpu lane broke" in text and "gpu lane broke" in text                 # both are in the log
    notes = " ".join(getattr(e.value, "__notes__", []))
    assert ("gpu lane broke" in notes) != ("cpu lane broke" in notes) or len(e.value.__notes__) == 1


def test_prediction_files_are_written_through_a_temp_name_with_the_pid_and_never_left_behind(world, tmp_path,
                                                                                            monkeypatch):
    seen = []
    real = G.os.replace
    monkeypatch.setattr(G.os, "replace", lambda a, b: (seen.append(Path(a).name), real(a, b))[1])
    out = tmp_path / "o"
    grun([lgbm_cell(world)], out, world)
    assert any(str(os.getpid()) in n and n.endswith(".tmp") and ".parquet" in n for n in seen)
    assert not list((out / "predictions").glob("*.tmp"))
    out2 = tmp_path / "o2"
    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda self, *a, **k: (_ for _ in ()).throw(OSError("full")))
    res = grun([lgbm_cell(world)], out2, world, task_fn=lambda cfg, seed, key: G._run_task(cfg, seed, key))
    assert res["errors"] == 1 and not list((out2 / "predictions").glob("*.tmp"))


# ---- worker memory, pool settings
def test_a_gpu_worker_keeps_one_intraday_panel_and_does_not_pin_evicted_daily_panels(world):
    import gc
    import weakref
    assert S.INTRADAY_CACHE_SIZE == 1
    st = _gsettings()
    panel = G.load_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))
    ip = S.get_intraday(panel, st)
    ip.eod_rows(panel), ip.daily_of(panel)
    ref = weakref.ref(panel)
    del panel
    gc.collect()
    assert ref() is None                                                       # the intraday panel does not keep it alive


def test_the_gpu_pool_is_built_with_one_task_per_slot_retirement_and_a_hard_deadline(world, tmp_path, monkeypatch):
    SnapshotPool.instances = []
    monkeypatch.setattr(G, "ProcessPoolExecutor", SnapshotPool)
    seen = {}
    real = G._drive_pool

    def spy(tasks, slots, init_args, task_fn, finish, cancelled, summary, **k):
        seen[tasks[0][0].pool] = k
        return real(tasks, slots, init_args, task_fn, finish, cancelled, summary, **k)
    monkeypatch.setattr(G, "_drive_pool", spy)
    grun([lgbm_cell(world), tsai_cell(world)], tmp_path / "o", world, inline=False, cpu_slots=1, gpu_slots=1,
         task_fn=slow_or_quick_task_ok, settings=_gsettings(gpu_budget_minutes=2.0))
    assert seen["cpu"] == {"deadline": 3 * 60 * 60.0, "window_factor": 1}                # the default cpu budget: 60 minutes
    assert seen["gpu"] == {"deadline": 3 * 2.0 * 60, "window_factor": 1, "max_tasks_per_child": G.GPU_MAX_TASKS_PER_CHILD}
    assert G.GPU_MAX_TASKS_PER_CHILD == 40
    kws = [pool.kwargs for pool in SnapshotPool.instances]
    assert sorted("max_tasks_per_child" in k for k in kws) == [False, True]
    assert [k["max_tasks_per_child"] for k in kws if "max_tasks_per_child" in k] == [40]


def slow_or_quick_task_ok(cfg, seed, key):
    return slow_or_quick_task({**cfg, "family": "x"}, seed, key)


def test_only_the_first_generation_of_workers_is_staggered():
    import multiprocessing
    counter = multiprocessing.get_context("spawn").Value("i", 0)
    st = _gsettings()
    seen = []
    for _ in range(5):                                                         # 2 workers, then 3 replacements
        G._init_worker(st.to_dict(), "c", "o", 1, counter, 2)
        seen.append(G._WORKER["index"])
    assert seen == [0, 1, 0, 0, 0]
    counter2 = multiprocessing.get_context("spawn").Value("i", 0)
    seen = []
    for _ in range(3):                                                         # without the pool size: as before
        G._init_worker(st.to_dict(), "c", "o", 1, counter2)
        seen.append(G._WORKER["index"])
    assert seen == [0, 1, 2]


# ---- duplication
def test_the_intraday_span_reads_each_cache_file_once(world, monkeypatch):
    panel = G.get_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))
    S._file_span.cache_clear()
    calls = []
    real = pd.read_parquet
    monkeypatch.setattr(S.pd, "read_parquet", lambda *a, **k: (calls.append(k.get("columns")), real(*a, **k))[1])
    a = S.intraday_span(panel, _gsettings())
    n = len(calls)
    assert n == len(GSYMS) + 2                                                  # the symbols + SPY + QQQ, one read each
    assert S.intraday_span(panel, _gsettings()) == a and len(calls) == n


def test_the_robust_scaling_is_one_shared_helper(world):
    from app.services import ml_search
    col = np.array([1.0, 2.0, 2.5, np.nan, 40.0, 3.0])
    med, iqr = ml_search.robust_center_scale(col)
    assert med == pytest.approx(np.nanmedian(col))
    q75, q25 = np.nanpercentile(col, [75, 25])
    assert iqr == pytest.approx(q75 - q25)
    flat = ml_search.robust_center_scale(np.array([3.0, 3.0, 3.0]))
    assert flat[0] == 3.0 and flat[1] == pytest.approx(1e-12)                   # no spread: the std (0) + 1e-12
    nan = ml_search.robust_center_scale(np.array([np.nan, np.nan]))
    assert np.isnan(nan[0]) and np.isnan(nan[1])
    panel = _daily_panel(world)
    cols = panel.group_columns(("t",))
    fit = np.flatnonzero(panel.ok)[:600]
    data = S.daily_sequence_data(panel, cols, fit, 10, 5)
    X = panel.X[:, cols]
    for j in range(len(cols)):
        m, i = ml_search.robust_center_scale(X[fit, j].astype(np.float64))
        assert data.scaler["median"][j] == pytest.approx(m) and data.scaler["iqr"][j] == pytest.approx(i)
    frame = pd.DataFrame(X[:300], columns=[f"c{j}" for j in range(X.shape[1])])
    frame["symbol"] = "A"
    fd = ml_search.fold_data(frame, [f"c{j}" for j in range(X.shape[1])], np.arange(200), False)
    m0, i0 = ml_search.robust_center_scale(X[:200, 0].astype(np.float64))
    assert fd.scaler["median"][0] == pytest.approx(m0) and fd.scaler["iqr"][0] == pytest.approx(i0)


# ---- help texts
def test_the_help_texts_name_the_constants_and_the_two_pools(capsys):
    import ba2test_launcher as L
    from app.services import ml_grid_bounds as B
    assert G.DATA_START == B.DATA_START and G.EVAL_END == B.EVAL_END and G.EVAL_START == B.EVAL_START
    with pytest.raises(SystemExit):
        L.main(["ml-grid", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert str(B.EVAL_END) in text and str(B.EVAL_START) in text and str(B.DATA_START) in text
    assert "BOTH pools" in text and "total workers" in text


def test_building_the_cli_help_does_not_import_the_grid_runner():
    import subprocess
    import sys
    code = ("import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s'); import ba2test_launcher as L\n"
            "try:\n    L.main(['ml-grid', '--help'])\nexcept SystemExit:\n    pass\n"
            "print('LOADED' if 'app.services.ml_grid' in sys.modules else 'LIGHT')"
            % (str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert out.stdout.strip().endswith("LIGHT"), out.stdout[-500:] + out.stderr[-500:]


# ---- 7. the tsai identity: a content hash of the 5-minute bars
def _copy_world(world, root: Path):
    stores, _, cache = world
    shutil.copytree(cache, root / "cache")
    shutil.copytree(stores, root / "stores")
    return root / "stores", root / "stores" / "stocks", root / "cache"


def _bars_file(world, sym):
    return world[2] / "AlpacaHistoryOHLCVProvider" / f"{sym}_5min.parquet"


def _fp(world, **over):
    kw = dict(symbols=tuple(GSYMS), eval_end=LAST_G, store_last=LAST_G)
    kw.update(over)
    return S.intraday_fingerprint(str(world[2]), kw["symbols"], kw["eval_end"], kw["store_last"])


def test_the_intraday_fingerprint_is_a_content_hash_of_the_bars_in_the_window(world, tmp_path):
    w = _copy_world(world, tmp_path)
    base = _fp(w)
    assert len(base) == 16 and base == _fp(w)
    assert _fp(w, symbols=tuple(reversed(GSYMS))) == base                         # the order of the symbols does not matter
    f = _bars_file(w, GSYMS[0])
    os.utime(f, (1_900_000_000, 1_900_000_000))                                     # a touch
    assert _fp(w) == base
    df = pd.read_parquet(f)
    extra = df.iloc[[-1]].copy()
    extra["Date"] = extra["Date"] + pd.Timedelta(days=5)                            # a bar AFTER the store's last day
    pd.concat([df, extra]).to_parquet(f, index=False)
    assert _fp(w) == base
    df = pd.read_parquet(f)
    df.loc[df.index[len(df) // 2], "Close"] *= 1.0001                               # a bar INSIDE the window
    df.to_parquet(f, index=False)
    assert _fp(w) != base
    again = _fp(w)
    q = pd.read_parquet(_bars_file(w, "QQQ"))                                       # the context symbols count too
    q.loc[q.index[len(q) // 3], "Open"] *= 1.001
    q.to_parquet(_bars_file(w, "QQQ"), index=False)
    assert _fp(w) != again
    assert _fp(w, eval_end=date(2020, 6, 30)) != _fp(w)                              # the window ends at eval_end


def test_a_tsai_row_records_the_fingerprint_and_no_mtime(tsai_run, darts_run, world):
    ident = tsai_run[2][0]["identity"]
    assert len(ident["intraday_fingerprint"]) == 16
    assert "mtime" not in json.dumps(ident)
    assert "intraday_fingerprint" not in darts_run[2][0]["identity"]               # only tsai reads the 5-minute bars
    assert ident["intraday_fingerprint"] == _fp(world, symbols=tuple(sorted(GSYMS)))


def test_a_changed_five_minute_bar_makes_a_tsai_resume_refuse_but_a_touch_does_not(world, tmp_path):
    w = _copy_world(world, tmp_path / "w")
    out = tmp_path / "o"
    cells = [tsai_cell(w), lgbm_cell(w)]
    grun(cells, out, w)
    f = _bars_file(w, GSYMS[2])
    os.utime(f, (1_900_000_000, 1_900_000_000))
    assert grun(cells, out, w)["tasks"] == 0
    df = pd.read_parquet(f)
    df.loc[df.index[len(df) // 2], "Close"] *= 1.0001
    df.to_parquet(f, index=False)
    with pytest.raises(G.ResumeMismatchError, match="intraday_fingerprint"):
        grun(cells, out, w)
    assert grun([lgbm_cell(w)], out, w)["tasks"] == 0                              # a tabular row does not read them
    res = grun(cells, out, w, rerun_mismatched=True)
    assert res["done"] == 1


# ---- 8. the audit
def test_the_library_versions_cover_the_deep_learning_stack_and_the_code_hash_the_gpu_modules():
    v = G.library_versions()
    for key in ("torch", "darts", "pytorch_lightning", "tsai"):
        assert key in v and v[key], key
    for mod in ("ml_grid_seq", "ml_models_seq", "ml_panel", "ml_search"):
        assert mod in G._CODE_MODULES
    assert len(v["code"]) == 12


def test_the_gpu_versions_name_cuda_cudnn_and_the_device_without_waking_the_gpu(monkeypatch):
    import torch
    S.gpu_versions.cache_clear()
    monkeypatch.setattr(torch.cuda, "is_initialized", lambda: False)
    v = S.gpu_versions()
    assert set(v) == {"torch_cuda", "cudnn", "device"} and v["device"] is None            # CUDA was not touched
    S.gpu_versions.cache_clear()
    monkeypatch.setattr(torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda i=0: "Fake GPU 9000")
    assert S.gpu_versions()["device"] == "Fake GPU 9000"
    S.gpu_versions.cache_clear()


def test_a_gpu_row_carries_the_gpu_versions_a_tabular_row_does_not(tsai_run, darts_run, world, tmp_path):
    for _, _, rows in (tsai_run, darts_run):
        assert set(rows[0]["versions"]) >= {"torch", "torch_cuda", "cudnn", "device", "darts", "tsai", "code"}
    out = tmp_path / "o"
    grun([lgbm_cell(world)], out, world)
    assert "torch_cuda" not in G.read_results(out / "results.jsonl")[0]["versions"]


# ---- nondeterminism record
NONDET = ("upsample_linear1d_backward_out_cuda does not have a deterministic implementation, but you set "
          "'torch.use_deterministic_algorithms(True, warn_only=True)'. You can file an issue.")


def test_a_block_that_hits_a_nondeterministic_kernel_says_so(world, tmp_path, monkeypatch):
    import warnings
    real = MS.TsaiAdapter.fit

    def fit(self, *a, **k):
        warnings.warn(NONDET, UserWarning)
        return real(self, *a, **k)
    monkeypatch.setattr(MS.TsaiAdapter, "fit", fit)
    out = tmp_path / "o"
    grun([tsai_cell(world)], out, world)
    row = G.read_results(out / "results.jsonl")[0]
    assert row["gpu_nondeterministic"] is True and row["nondeterministic_ops"] == ["upsample_linear1d_backward_out_cuda"]
    for b in row["blocks"]:
        assert b["gpu_nondeterministic"] is True and b["nondeterministic_ops"] == ["upsample_linear1d_backward_out_cuda"]


def test_a_clean_block_is_not_flagged(tsai_run):
    row = tsai_run[2][0]
    assert row["gpu_nondeterministic"] is False and row["nondeterministic_ops"] == []
    assert all(b["gpu_nondeterministic"] is False for b in row["blocks"])


def test_other_warnings_of_a_block_are_not_swallowed(world, tmp_path, monkeypatch):
    import warnings
    real = MS.TsaiAdapter.fit

    def fit(self, *a, **k):
        warnings.warn("something unrelated", UserWarning)
        return real(self, *a, **k)
    monkeypatch.setattr(MS.TsaiAdapter, "fit", fit)
    with pytest.warns(UserWarning, match="something unrelated"):
        grun([tsai_cell(world)], tmp_path / "o", world)


def test_the_comments_do_not_promise_bitwise_repeatability():
    text = Path(S.__file__).read_text(encoding="utf-8")
    assert "bitwise repeatable" not in re.sub(r"not bitwise repeatable", "", text, flags=re.I)


# ---------------------------------------------------------------- 3. a sticky CUDA record restarts the pool at once
def sticky_or_slow_task(cfg, seed, key):
    """train_months 24 (B): hangs the first time (until the pool is restarted), then returns at once. train_months 12 (A):
    waits until B has started hanging (so the restart really interrupts it), then returns a sticky CUDA error record."""
    marker = Path(os.environ["STICKY_TEST_MARKER"])
    if cfg["train_months"] == 12:
        for _ in range(1200):
            if marker.exists():
                break
            _real_time.sleep(0.1)
        rec = G._error_record(key, seed, "RuntimeError: CUDA error: device-side assert triggered", "l")
        rec["cuda_error"] = "sticky"
        return rec
    if not marker.exists():
        marker.write_text("started", encoding="utf-8")
        _real_time.sleep(240)
    return slow_or_quick_task({**cfg, "family": "x"}, seed, key)


def test_a_sticky_cuda_record_terminates_the_pool_and_requeues_the_other_tasks_uncharged(world, tmp_path, monkeypatch):
    monkeypatch.setenv("STICKY_TEST_MARKER", str(tmp_path / "started"))
    SnapshotPool.instances = []
    monkeypatch.setattr(G, "ProcessPoolExecutor", SnapshotPool)
    a = G.validate_cell(tsai_cell(world))
    b = G.validate_cell(tsai_cell(world, train_months=24))
    rows, summary = [], {}
    t0 = _real_time.time()
    G._drive_pool([(a, 1), (b, 1)], 2, (_gsettings().to_dict(), str(world[2]), str(tmp_path), 1), sticky_or_slow_task,
                  rows.append, lambda: False, summary, window_factor=1)
    assert _real_time.time() - t0 < 150                                       # not the 240 s the slow task would have taken
    assert summary["pool_rebuilds"] >= 1 and len(SnapshotPool.instances) >= 2
    bad = [r for r in rows if "error" in r]
    assert len(bad) == 1 and bad[0]["cuda_error"] == "sticky"                  # the sticky one is recorded ...
    assert not any("worker process died" in r["error"] for r in bad)           # ... nobody else was charged
    assert [r["cell_key"] for r in rows if "error" not in r] == [b.key]       # the slow task ran again and finished


# ---------------------------------------------------------------- 4. the runtime panel end
def test_the_runtime_intraday_span_ends_where_the_master_and_the_resume_check_say(world):
    panel = G.load_panel(str(world[0]), tuple(sorted(GSYMS)), "selection", str(world[2]))
    store_last = G.store_span(str(world[0]), tuple(sorted(GSYMS)))[1]
    assert panel.store_last == store_last
    st = _gsettings()
    first, last, _ = S.intraday_span(panel, st)
    assert last == min(store_last, st.eval_end)
    panel.store_last = date(2020, 3, 2)                                        # whatever the store says is what counts
    assert S.intraday_span(panel, st)[1] == date(2020, 3, 2)
    assert S.intraday_fingerprint(str(world[2]), GSYMS, st.eval_end, date(2020, 3, 2)) == \
        S.intraday_fingerprint(str(world[2]), GSYMS, st.eval_end, panel.store_last)


def test_the_panel_uses_the_stores_last_day_not_the_last_bar_of_the_daily_panel(world, tmp_path):
    w = _copy_world(world, tmp_path / "w")
    mp = w[1] / "features.parquet.meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    meta["last_day"] = "2020-12-30"                                           # one session before the data ends
    mp.write_text(json.dumps(meta), encoding="utf-8")
    panel = G.load_panel(str(w[0]), tuple(sorted(GSYMS)), "selection", str(w[2]))
    assert panel.store_last == date(2020, 12, 30) == G.store_span(str(w[0]), tuple(sorted(GSYMS)))[1]
    assert S.intraday_span(panel, _gsettings())[1] == date(2020, 12, 30)


# ---------------------------------------------------------------- 5. housekeeping
def test_a_run_sweeps_leftover_prediction_temp_files_under_the_lock(world, tmp_path):
    out = tmp_path / "o"
    (out / "predictions").mkdir(parents=True)
    stale = out / "predictions" / "abc_1.parquet.4242.tmp"
    stale.write_bytes(b"half a file")
    keep = out / "predictions" / "other.parquet"
    keep.write_bytes(b"x")
    grun([lgbm_cell(world)], out, world)
    assert not stale.exists() and keep.exists() and not list((out / "predictions").glob("*.tmp"))


def test_stale_darts_temp_dirs_are_swept_but_a_fresh_one_is_left_alone(tmp_path):
    import os as _os
    old, fresh, other = tmp_path / "ba2_darts_old", tmp_path / "ba2_darts_fresh", tmp_path / "unrelated_dir"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "x.ckpt").write_bytes(b"x")
    long_ago = _real_time.time() - 24 * 3600
    _os.utime(old, (long_ago, long_ago))
    _os.utime(other, (long_ago, long_ago))
    n = S.sweep_stale_darts_dirs(tmp_path, max_age_hours=6)
    assert n == 1 and not old.exists() and fresh.exists() and other.exists()


def test_a_run_with_gpu_cells_sweeps_the_darts_temp_dirs(world, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "sweep_stale_darts_dirs", lambda *a, **k: calls.append(1) or 0)
    grun([tsai_cell(world)], tmp_path / "o", world)
    assert calls
    calls.clear()
    grun([lgbm_cell(world)], tmp_path / "o2", world)
    assert not calls                                                            # a tabular run has no darts dirs


def test_the_warning_collector_counts_per_op_and_keeps_a_bounded_set_of_others():
    import warnings
    c = S.WarningCollector(max_others=5)

    def warn(text):                                                              # one call site: one key per message
        warnings.warn(text, UserWarning)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with c:
            for _ in range(5000):
                warn(NONDET)
            for i in range(200):
                warn(f"unrelated {i}")
            for _ in range(10):
                warn("unrelated 0")
    assert c.ops == {"upsample_linear1d_backward_out_cuda": 5000}
    assert len(c.others) == 5 and c.dropped == 195                              # a bound, and the overflow is counted
    assert sum(c.others.values()) == 15                                         # 1 + 10 of "unrelated 0", 1 each of 1-4


def test_the_collector_reemits_what_it_kept_and_restores_the_warning_machinery():
    import warnings
    orig = warnings.showwarning
    with pytest.warns(UserWarning, match="unrelated 7") as rec:
        with S.WarningCollector():
            warnings.warn("unrelated 7", UserWarning)
    assert warnings.showwarning is orig


def test_the_counts_per_op_are_on_the_block_audit(world, tmp_path, monkeypatch):
    import warnings
    real = MS.TsaiAdapter.fit

    def fit(self, *a, **k):
        for _ in range(300):
            warnings.warn(NONDET, UserWarning)
        return real(self, *a, **k)
    monkeypatch.setattr(MS.TsaiAdapter, "fit", fit)
    out = tmp_path / "o"
    grun([tsai_cell(world)], out, world)
    row = G.read_results(out / "results.jsonl")[0]
    assert all(b["nondeterministic_counts"] == {"upsample_linear1d_backward_out_cuda": 300} for b in row["blocks"])
    assert row["nondeterministic_ops"] == ["upsample_linear1d_backward_out_cuda"]


def test_warnings_are_kept_when_the_fit_fails(world, tmp_path, monkeypatch):
    import warnings

    def fit(self, *a, **k):
        warnings.warn("a warning before the crash", UserWarning)
        raise RuntimeError("boom")
    monkeypatch.setattr(MS.TsaiAdapter, "fit", fit)
    with pytest.warns(UserWarning, match="a warning before the crash"):
        res = grun([tsai_cell(world)], tmp_path / "o", world)
    assert res["errors"] == 1


# ---- the CPU lane has a hard deadline too (cpu_budget_minutes, 3 x)
def test_cpu_budget_minutes_is_a_run_setting_that_is_no_part_of_any_identity(world):
    assert _gsettings().cpu_budget_minutes == 60.0
    for bad in (0, -1, float("nan"), True, "60", None):
        with pytest.raises(ValueError, match="cpu_budget_minutes"):
            _gsettings(cpu_budget_minutes=bad)
    st = _gsettings(cpu_budget_minutes=5)
    assert st.to_dict()["cpu_budget_minutes"] == 5 and G.GridSettings.from_dict(st.to_dict()) == st
    for cell in (lgbm_cell(world), tsai_cell(world)):
        assert "cpu_budget_minutes" not in G.identity_of(G.validate_cell(cell), st, "fp")["settings"]


def test_the_cpu_pool_is_built_with_a_hard_deadline_of_three_cpu_budgets(world, tmp_path, monkeypatch):
    seen = {}
    real = G._drive_pool

    def spy(tasks, slots, init_args, task_fn, finish, cancelled, summary, **k):
        seen[tasks[0][0].pool] = k
        return real(tasks, slots, init_args, task_fn, finish, cancelled, summary, **k)
    monkeypatch.setattr(G, "_drive_pool", spy)
    grun([lgbm_cell(world)], tmp_path / "o", world, inline=False, cpu_slots=1, gpu_slots=0,
         task_fn=slow_or_quick_task_ok, settings=_gsettings(cpu_budget_minutes=2.0))
    assert seen["cpu"] == {"deadline": 3 * 2.0 * 60, "window_factor": 1}          # one task per slot: the clock starts at once


def cpu_hang_task(cfg, seed, key):
    """lgbm hangs (a stuck fit); naive returns at once."""
    if cfg["family"] == "lgbm":
        _real_time.sleep(300)
    return slow_or_quick_task({**cfg, "family": "x"}, seed, key)


def test_a_cpu_task_stuck_twice_is_retried_once_and_then_recorded_as_a_timeout(world, tmp_path):
    out = tmp_path / "o"
    cells = [lgbm_cell(world), lgbm_cell(world, family="naive", params={})]
    t0 = _real_time.time()
    res = grun(cells, out, world, inline=False, cpu_slots=2, gpu_slots=0, task_fn=cpu_hang_task,
               settings=_gsettings(cpu_budget_minutes=0.2))                         # hard deadline: 3 x 12 s = 36 s, twice
    assert _real_time.time() - t0 < 200                                             # not the 300 s of the stuck fit
    assert res["done"] == 1 and res["errors"] == 1 and res["pool_rebuilds"] >= 2
    errs = G.read_results(out / "errors.jsonl")
    key = G.validate_cell(lgbm_cell(world)).key
    assert len(errs) == 2 and all(e["cell_key"] == key and e["hard_deadline"] is True for e in errs)
    assert "timeout" not in errs[0] and errs[0]["retrying"] is True                  # the first one: a retry follows
    assert errs[1]["timeout"] is True and "retrying" not in errs[1]                  # the second: given up
    naive_key = G.validate_cell(lgbm_cell(world, family="naive", params={})).key
    assert [r["cell_key"] for r in G.read_results(out / "results.jsonl")] == [naive_key]           # the innocent one finished
    again = grun([lgbm_cell(world)], out, world, task_fn=slow_or_quick_task_ok)    # a restart skips the timeout
    assert again["tasks"] == 0 and again["skipped_timeouts"] == 1


# ---- precheck_grid: GPU cells need GPU slots
def test_precheck_grid_requires_a_gpu_slot_when_there_are_gpu_cells(world, tmp_path):
    from tests.test_ml_grid import payload
    body = payload(world, cells=[tsai_cell(world)], out_dir=str(tmp_path / "o"), gpu_slots=0)
    with pytest.raises(ValueError, match="gpu_slots"):
        G.precheck_grid(body)                                                  # the API's 400, not a failure after queueing
    assert G.precheck_grid({**body, "gpu_slots": 1}) == 1
    only_cpu = payload(world, cells=[lgbm_cell(world)], out_dir=str(tmp_path / "o2"), gpu_slots=0)
    assert G.precheck_grid(only_cpu) == 1                                       # no GPU cell: no GPU slot needed
