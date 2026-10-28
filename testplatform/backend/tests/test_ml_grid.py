"""Stage-1 grid runner (app/services/ml_grid.py) on tiny synthetic stores with a planted (or no) signal."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_eval as E
from app.services import ml_grid as G
from app.services import ml_holdout as HO
from app.services import ml_panel as P
from app.services import ml_targets as T

FIRST, LAST = date(2017, 1, 3), date(2020, 6, 30)
TINY = {"n_estimators": 60, "early_stopping_rounds": 10, "min_child_samples": 20, "num_leaves": 7}


def _unlocked(n, prefix="SYN"):
    out, i = [], 0
    while len(out) < n:
        s = f"{prefix}{i}"
        if not HO.is_locked_largecap(s):
            out.append(s)
        i += 1
    return out


SYMS = _unlocked(10)


def _settings(**kw):
    base = dict(eval_start=date(2019, 1, 1), eval_end=date(2020, 6, 30), data_start=FIRST, min_count=5,
                control_seeds=tuple(range(1, 6)), cores=2, min_obs=20, regime_min_rows=10)
    base.update(kw)
    return G.GridSettings(**base)


def build_store(root, name, symbols, cache, beta, seed, extra_cols=(), canary=False):
    """Write bars into ``cache`` and a store ``root/name`` whose t_signal is the decision-time observable: the next
    day's log return is ``beta * t_signal + noise`` (beta 0 = pure noise)."""
    rng = np.random.default_rng(seed)
    sess = pd.DatetimeIndex(pd.to_datetime(P.regular_sessions(FIRST, LAST)["session"]))
    n = len(sess)
    market = rng.normal(0, 0.006, n)
    cdir = cache / "AlpacaHistoryOHLCVProvider"
    cdir.mkdir(parents=True, exist_ok=True)
    feats = []
    for k, sym in enumerate(symbols):
        sig = rng.normal(0, 1, n)
        noise = rng.normal(0, 1, n)
        r = np.r_[0.0, beta * sig[:-1]] + rng.normal(0, 0.012, n) + market
        close = 100.0 * np.exp(np.cumsum(r))
        open_ = np.r_[100.0, close[:-1]]
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
        bars = pd.DataFrame({"Date": sess, "Open": open_, "High": high, "Low": low, "Close": close,
                             "Volume": rng.integers(1_000_000, 2_000_000, n).astype("int64")})
        bars["effective_date"] = bars["Date"]
        bars.to_parquet(cdir / f"{sym}_1d.parquet", index=False)
        f = pd.DataFrame({"t_signal": sig, "t_noise": noise, "t_vol": np.abs(r)}, index=pd.MultiIndex.from_product(
            [[sym], sess], names=["symbol", "session"]))
        for c in extra_cols:
            f[c] = rng.normal(0, 1, n)
        if canary:                                  # the realised 5-session return, as a "feature": a pure leak
            ahead = np.full(n, np.nan)
            ahead[:-5] = close[5:] / close[:-5] - 1.0
            f["x_canary"] = ahead
        f["listed"], f["filled"], f["ended"] = True, False, False
        f["label_bar_filled"] = pd.array(np.zeros(n, dtype=bool), dtype="boolean")
        feats.append(f)
    df = pd.concat(feats)
    d = root / name
    d.mkdir(parents=True)
    df.to_parquet(d / "features.parquet")
    meta = {"symbols": list(symbols), "first_day": str(FIRST), "last_day": str(LAST), "cache_end": str(LAST),
            "ensure": {"min_share": 0.7, "inputs": {"code": "synthetic-1", "fingerprints": {
                "daily": {s: {"mtime_ns": 1, "size": 1} for s in symbols}}}}}
    (d / "features.parquet.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    years = sorted(set(sess.year))
    rep = pd.DataFrame({"rows": 1, "complete": 1, "share": 1.0},
                       index=pd.MultiIndex.from_tuples([("t", y) for y in years], names=["group", "year"]))
    rep.to_parquet(d / "coverage_report.parquet")
    return d


@pytest.fixture(scope="module")
def signal_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("signal")
    return root / "stores", build_store(root / "stores", "stocks", SYMS, root / "cache", 0.006, 1), root / "cache"


@pytest.fixture(scope="module")
def canary_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("canary")
    return root / "stores", build_store(root / "stores", "stocks", SYMS, root / "cache", 0.0, 6, canary=True), root / "cache"


@pytest.fixture(scope="module")
def noise_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("noise")
    return root / "stores", build_store(root / "stores", "stocks", SYMS, root / "cache", 0.0, 2), root / "cache"


def cell(store, **over):
    c = {"track": "selection", "store": str(store), "symbols": SYMS, "groups": ["t"],
         "target": {"kind": "rel_return", "horizon": 5}, "family": "lgbm", "params": TINY, "train_months": 12,
         "blocks": "Q", "seeds": [1]}
    c.update(over)
    return c


def run(cells, out, world, **kw):
    kw.setdefault("settings", _settings())
    kw.setdefault("inline", True)
    return G.run_grid([G.validate_cell(c) for c in cells], out, kw.pop("cpu_slots", 1), cache_root=world[2], **kw)


# ------------------------------------------------------------------ config validation
def test_validate_cell_is_strict_and_canonical(signal_world):
    base = cell(signal_world[0])
    c = G.validate_cell(base)
    assert c.groups == ("t",) and c.kind == "rel_return" and len(c.key) == 12
    for bad, msg in [({"extra": 1}, "unknown cell keys"), ({"track": "x"}, "track"), ({"groups": ["zz"]}, "unknown feature"),
                     ({"target": {"kind": "nope", "horizon": 5}}, "target kind"),
                     ({"target": {"kind": "rel_return", "horizon": 7}}, "horizon"),
                     ({"target": {"kind": "rel_return", "horizon": 5, "ratio": 2.0}}, "ratio"),
                     ({"target": {"kind": "up", "horizon": 5, "foo": 1}}, "target needs"),
                     ({"family": "xgb", "target": {"kind": "rank", "horizon": 5}}, "cannot do"),
                     ({"params": {"nope": 1}}, "unknown params"), ({"train_months": 18}, "train_months"),
                     ({"blocks": "M"}, "blocks"), ({"seeds": [1, 1]}, "seeds"), ({"seeds": []}, "seeds"),
                     ({"symbols": "timing"}, "not a selection universe"),
                     ({"target": {"kind": "volatility", "horizon": 1}}, "horizon >= 2")]:
        with pytest.raises(ValueError, match=msg):
            G.validate_cell({**base, **bad})
    with pytest.raises(ValueError, match="missing keys"):
        G.validate_cell({k: v for k, v in base.items() if k != "family"})
    with pytest.raises(ValueError, match="cross-sectional"):
        G.validate_cell({**base, "track": "timing"})                       # rel_return is selection-only
    assert G.validate_cell({k: v for k, v in base.items() if k != "params"}).params == ()


def test_cell_key_ignores_seeds_and_orderings_but_not_content(signal_world):
    a = G.validate_cell(cell(signal_world[0], groups=["t", "m"], seeds=[1, 2, 3]))
    b = G.validate_cell(cell(signal_world[0], groups=["m", "t"], seeds=[9], symbols=list(reversed(SYMS))))
    assert a.key == b.key
    assert a.key != G.validate_cell(cell(signal_world[0], groups=["t", "m"],
                                         target={"kind": "rel_return", "horizon": 10})).key
    assert a.key != G.validate_cell(cell(signal_world[0], groups=["t", "m"], family="xgb", params={})).key


def test_a_locked_symbol_raises_everywhere(signal_world, tmp_path):
    bad = cell(signal_world[0], symbols=SYMS[:4] + ["MSFT"])
    with pytest.raises(HO.LockedSymbolError, match="MSFT"):
        G.validate_cell(bad)
    with pytest.raises(HO.LockedSymbolError):
        G.load_cells([cell(signal_world[0]), bad])
    with pytest.raises(HO.LockedSymbolError):
        G.handle_ml_grid("t", {"out_dir": str(tmp_path), "cpu_slots": 1, "gpu_slots": 0, "cells": [bad]})
    assert set(G.TIMING_ETFS + G.TIMING_STOCKS).isdisjoint({"MSFT", "NVDA", "AMZN", "META"})
    HO.assert_unlocked(G.resolve_symbols("timing"), "test")           # the ETFs are exempt in ml_holdout itself
    HO.assert_unlocked(G.resolve_symbols("ndq30_dev"), "test")
    with pytest.raises(HO.LockedSymbolError, match="NVDA"):
        HO.assert_unlocked(["SPY", "NVDA"], "test")
    with pytest.raises(ValueError, match="no ETF"):                  # a selection universe holds no ETF
        G.validate_cell(cell(signal_world[0], symbols=SYMS[:4] + ["SPY"]))
    mixed = build_store(tmp_path / "stores", "stocks", SYMS[:4] + ["SPY"], tmp_path / "cache", 0.0, 9)
    G.validate_cell(cell(mixed.parent, track="timing", target={"kind": "up", "horizon": 5}, symbols=SYMS[:4] + ["SPY"]))
    assert len(G.resolve_symbols("timing")) == 19 and len(G.resolve_symbols("ndq30_dev")) == 20


def test_load_cells_refuses_duplicates_and_bad_shapes(signal_world, tmp_path):
    c = cell(signal_world[0])
    with pytest.raises(ValueError, match="same configuration"):
        G.load_cells([c, {**c, "seeds": [5]}])
    with pytest.raises(ValueError, match="non-empty list"):
        G.load_cells([])
    with pytest.raises(ValueError, match="exactly the key"):
        G.load_cells({"cells": [c], "other": 1})
    p = tmp_path / "cells.json"
    p.write_text(json.dumps({"cells": [c]}), encoding="utf-8")
    assert len(G.load_cells(p)) == 1 and len(G.load_cells(str(p))) == 1


# ------------------------------------------------------------------ end to end
def test_two_cells_two_seeds_end_to_end_resume_and_files(signal_world, tmp_path):
    out = tmp_path / "out"
    cells = [cell(signal_world[0], seeds=[1, 2]),
             cell(signal_world[0], track="timing", target={"kind": "up", "horizon": 5}, seeds=[1, 2])]
    res = run(cells, out, signal_world)
    assert res["tasks"] == 4 and res["done"] == 4 and res["errors"] == 0
    rows = G.read_results(out / "results.jsonl")
    assert sorted((r["track"], r["seed"]) for r in rows) == [("selection", 1), ("selection", 2), ("timing", 1),
                                                             ("timing", 2)]
    for r in rows:
        assert {"cell_key", "seed", "config", "metrics", "regimes", "controls", "skipped_blocks", "n_rows",
                "timings", "primary", "status", "n_blocks", "n_blocks_fitted", "clipped_blocks"} <= set(r)
        assert r["status"] == "ok" and r["n_blocks"] == 6 and r["n_blocks_fitted"] == 6 and r["n_rows"] > 3000
        assert r["primary"]["name"] == G.PRIMARY[r["track"]] and np.isfinite(r["primary"]["value"])
        assert set(r["timings"]) >= {"fit", "metrics", "controls", "total"}
        if r["track"] == "selection":
            assert r["controls"]["primary"] == "shuffle_symbols" and set(r["controls"]["p_values"]) == {"shuffle_symbols"}
        else:                                       # timing: the stricter of the two time nulls
            c = r["controls"]
            assert set(c["p_values"]) == {"shuffle_time", "shuffle_time_within_block"}
            assert c["p_value"] == max(c["p_values"].values()) == c["p_values"][c["primary"]]
            assert len(c["shuffle_time_within_block"]["values"]) == 5
        assert 0 < r["controls"]["p_value"] <= 1 and len(r["controls"]["shuffle_symbols"]["values"]) == 5
        assert "all" in next(iter(r["regimes"].values())) and "2019" in next(iter(r["regimes"].values()))
        pq = pd.read_parquet(out / "predictions" / f"{r['cell_key']}_{r['seed']}.parquet")
        assert len(pq) == r["n_rows"] and {"symbol", "session", "block", "score", "ret", "vol"} <= set(pq.columns)
        assert pq["session"].min() >= pd.Timestamp("2019-01-01") and pq["block"].nunique() == 6
    sel = [r for r in rows if r["track"] == "selection"][0]
    assert set(sel["metrics"]) == {"rank_ic", "quintile_spread_net", "long_only_net", "quintile_spread_gross"}
    assert {"mean", "t", "n"} <= set(sel["metrics"]["rank_ic"])
    tim = [r for r in rows if r["track"] == "timing"][0]
    assert {"timing_excess_net", "timing_excess_gross", "timing_sharpe_net", "timing_excess_by_symbol"} \
        <= set(tim["metrics"])
    assert (out / "results.jsonl").read_text(encoding="utf-8").count("\n") == 4

    before = (out / "results.jsonl").read_text(encoding="utf-8")
    again = run(cells, out, signal_world)                                   # restart: everything is finished
    assert again["tasks"] == 0 and again["skipped_existing"] == 4
    assert (out / "results.jsonl").read_text(encoding="utf-8") == before

    lines = before.splitlines()                                             # drop one row: only that one is redone
    gone = json.loads(lines[1])
    (out / "results.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    third = run(cells, out, signal_world)
    assert third["tasks"] == 1 and third["done"] == 1
    redone = [r for r in G.read_results(out / "results.jsonl") if (r["cell_key"], r["seed"]) == (gone["cell_key"], gone["seed"])]
    assert len(redone) == 1 and redone[0]["primary"]["value"] == gone["primary"]["value"]        # deterministic


def test_resume_survives_a_truncated_last_line(signal_world, tmp_path):
    out = tmp_path / "out"
    c = cell(signal_world[0], seeds=[1, 2])
    run([c], out, signal_world)
    path = out / "results.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n" + lines[1][:40], encoding="utf-8")           # the 2nd write was cut short
    res = run([c], out, signal_world)
    assert res["tasks"] == 1 and res["done"] == 1
    assert len(G.read_results(path)) == 2


def test_process_pool_matches_inline_and_the_handler_runs(signal_world, tmp_path):
    cells = [cell(signal_world[0], seeds=[1, 2])]
    run(cells, tmp_path / "inline", signal_world)
    res = G.handle_ml_grid("cli", {"out_dir": str(tmp_path / "pool"), "cpu_slots": 2, "gpu_slots": 0, "cells": cells,
                                   "cache_root": str(signal_world[2]), "cores": 2,
                                   "settings": {**_settings().to_dict(), "cores": 2}})
    assert res["done"] == 2 and res["errors"] == 0
    a = {(r["cell_key"], r["seed"]): r["primary"]["value"] for r in G.read_results(tmp_path / "inline" / "results.jsonl")}
    b = {(r["cell_key"], r["seed"]): r["primary"]["value"] for r in G.read_results(tmp_path / "pool" / "results.jsonl")}
    assert a == b and len(a) == 2


def test_handler_payload_validation(signal_world, tmp_path):
    c = cell(signal_world[0])
    with pytest.raises(ValueError, match="missing"):
        G.handle_ml_grid("t", {"out_dir": str(tmp_path), "cpu_slots": 1})
    with pytest.raises(ValueError, match="exactly one"):
        G.handle_ml_grid("t", {"out_dir": str(tmp_path), "cpu_slots": 1, "gpu_slots": 0})
    with pytest.raises(ValueError, match="exactly one"):
        G.handle_ml_grid("t", {"out_dir": str(tmp_path), "cpu_slots": 1, "gpu_slots": 0, "cells": [c],
                               "config_path": "x.json"})
    with pytest.raises(ValueError, match="unknown grid settings"):
        G.handle_ml_grid("t", {"out_dir": str(tmp_path), "cpu_slots": 1, "gpu_slots": 0, "cells": [c],
                               "settings": {"nope": 1}})


def test_an_exception_is_an_error_record_not_a_finished_row(signal_world, tmp_path):
    out = tmp_path / "out"
    bad = cell(signal_world[0], groups=["x"])                       # the synthetic store has no x_ columns
    res = run([bad], out, signal_world)
    assert res["done"] == 0 and res["errors"] == 1
    err = json.loads((out / "errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "feature group 'x'" in err["error"] and not (out / "results.jsonl").exists()
    assert run([bad], out, signal_world)["tasks"] == 1               # not finished: a restart retries it


def test_gpu_pool_hook(signal_world, tmp_path, monkeypatch):
    c = cell(signal_world[0])
    monkeypatch.setitem(G.FAMILY_POOL, "lgbm", "gpu")
    with pytest.raises(ValueError, match="gpu pool"):
        run([c], tmp_path / "a", signal_world)
    with pytest.raises(NotImplementedError):
        run([c], tmp_path / "b", signal_world, gpu_slots=1)
    assert G.n_jobs_for(6, 24) == 4 and G.n_jobs_for(30, 24) == 1 and G.n_jobs_for(1, 24) == 24


# ------------------------------------------------------------------ every target kind runs
@pytest.mark.parametrize("kind,track,family", [
    ("rel_return", "selection", "lgbm"), ("quintile", "selection", "lgbm"), ("beats", "selection", "lgbm"),
    ("rank", "selection", "lgbm"), ("up", "selection", "lgbm"), ("terciles", "timing", "lgbm"),
    ("triple_barrier", "timing", "lgbm"), ("binary_barrier", "selection", "lgbm"), ("regression", "timing", "lgbm"),
    ("quantile", "timing", "lgbm"), ("volatility", "timing", "lgbm"), ("up", "timing", "linear"),
    ("regression", "selection", "xgb")])
def test_every_target_kind_runs(signal_world, tmp_path, kind, track, family):
    params = TINY if family == "lgbm" else ({"n_estimators": 30, "early_stopping_rounds": 5} if family == "xgb" else {})
    c = cell(signal_world[0], track=track, target={"kind": kind, "horizon": 5}, family=family, params=params,
             groups=["t"])
    res = run([c], tmp_path / "o", signal_world, settings=_settings(control_seeds=(1, 2, 3)))
    assert res["done"] == 1, res
    r = G.read_results(tmp_path / "o" / "results.jsonl")[0]
    assert r["status"] == "ok" and r["n_blocks_fitted"] == 6 and np.isfinite(r["primary"]["value"])
    if kind == "volatility":
        assert r["primary"]["name"] == "spearman" and "skipped" in r["controls"]
        assert set(r["metrics"]) == {"spearman", "spearman_within_symbol"}
    else:
        assert r["primary"]["name"] == G.PRIMARY[track] and "p_value" in r["controls"]


def test_volatility_target_reports_a_bounded_spearman(signal_world, tmp_path):
    # the synthetic returns have no volatility clustering, so this only checks the plumbing: a bounded Spearman and
    # a regime table whose counts add up to the overall count
    c = cell(signal_world[0], track="timing", target={"kind": "volatility", "horizon": 5}, groups=["t"])
    run([c], tmp_path / "o", signal_world)
    r = G.read_results(tmp_path / "o" / "results.jsonl")[0]
    assert -1 <= r["primary"]["value"] <= 1 and r["regimes"]["spearman"]["all"]["n"] == r["metrics"]["spearman"]["n"]


# ------------------------------------------------------------------ planted signal vs noise
def _one(world, tmp_path, **over):
    c = cell(world[0], **over)
    run([c], tmp_path, world, settings=_settings(control_seeds=G.DEFAULT_SEEDS))
    return G.read_results(tmp_path / "results.jsonl")[0]


def test_planted_signal_selection_is_positive_and_beats_the_shuffle(signal_world, noise_world, tmp_path):
    sig = _one(signal_world, tmp_path / "s", target={"kind": "rel_return", "horizon": 5})
    noi = _one(noise_world, tmp_path / "n", target={"kind": "rel_return", "horizon": 5})
    assert sig["primary"]["value"] > 0.1 and sig["metrics"]["rank_ic"]["mean"] > 0.05
    assert sig["metrics"]["quintile_spread_net"]["t"] > 3
    assert sig["controls"]["rank"] == 1 and sig["controls"]["p_value"] <= 1 / 21 + 1e-9
    assert abs(noi["primary"]["value"]) < 0.5 * sig["primary"]["value"]
    assert abs(noi["metrics"]["rank_ic"]["mean"]) < 0.03 and noi["controls"]["p_value"] > 0.1


def test_planted_signal_timing_is_positive_and_noise_is_about_zero(signal_world, noise_world, tmp_path):
    kw = dict(track="timing", target={"kind": "up", "horizon": 5})
    sig = _one(signal_world, tmp_path / "s", **kw)
    noi = _one(noise_world, tmp_path / "n", **kw)
    assert sig["primary"]["value"] > 0.05 and sig["controls"]["p_values"]["shuffle_time"] <= 1 / 21 + 1e-9
    assert sig["controls"]["p_value"] <= 0.1             # a day-level signal also beats the stricter within-block null
    assert abs(noi["primary"]["value"]) < 0.3 * sig["primary"]["value"] and noi["controls"]["p_value"] > 0.1
    assert np.isfinite(sig["metrics"]["timing_sharpe_net"]["mean"])      # its sign is noise-dominated at this size


# ------------------------------------------------------------------ skipped blocks
def test_blocks_failing_min_count_are_skipped_and_recorded(signal_world, tmp_path):
    c = cell(signal_world[0], target={"kind": "terciles", "horizon": 5}, track="timing")
    run([c], tmp_path / "all", signal_world, settings=_settings(min_count=10 ** 6))
    r = G.read_results(tmp_path / "all" / "results.jsonl")[0]
    assert r["status"] == "no_predictions" and r["n_rows"] == 0 and r["primary"]["value"] is None
    assert [s["block"] for s in r["skipped_blocks"]] == ["2019Q1", "2019Q2", "2019Q3", "2019Q4", "2020Q1", "2020Q2"]
    assert all("unbalanced target" in s["reason"] and "examples < 1000000" in s["reason"] for s in r["skipped_blocks"])
    assert not (tmp_path / "all" / "predictions").exists()


def test_one_unbalanced_block_is_skipped_the_rest_are_fitted(signal_world, tmp_path, monkeypatch):
    real = T.check_balance

    def flaky(spec, fold_name, *a, **k):
        if fold_name == "2019Q3":
            raise T.TargetBalanceError("class down has 3 examples < 50")
        return real(spec, fold_name, *a, **k)
    monkeypatch.setattr(T, "check_balance", flaky)
    c = cell(signal_world[0], target={"kind": "binary_barrier", "horizon": 5}, track="timing")
    run([c], tmp_path / "o", signal_world)
    r = G.read_results(tmp_path / "o" / "results.jsonl")[0]
    assert r["skipped_blocks"] == [{"block": "2019Q3", "reason": "unbalanced target: class down has 3 examples < 50"}]
    assert r["n_blocks_fitted"] == 5 and r["status"] == "ok"
    pq = pd.read_parquet(tmp_path / "o" / "predictions" / f"{r['cell_key']}_1.parquet")
    assert "2019Q3" not in set(pq["block"]) and not pq["session"].between("2019-07-01", "2019-09-30").any()


def test_a_clipped_training_window_is_recorded(signal_world, tmp_path):
    c = cell(signal_world[0], train_months=36)                   # 2019Q1 minus 36 months precedes data_start 2017-01-03
    run([c], tmp_path / "o", signal_world, settings=_settings())
    r = G.read_results(tmp_path / "o" / "results.jsonl")[0]
    assert [x["block"] for x in r["clipped_blocks"]][:2] == ["2019Q1", "2019Q2"] and r["status"] == "ok"


# ------------------------------------------------------------------ causal cutoffs
def _stitched(seed=0, n_days=120):
    rng = np.random.default_rng(seed)
    sess = pd.bdate_range("2019-01-02", periods=n_days)
    rows = []
    for sym in ("A", "B", "C"):
        for i, s in enumerate(sess):
            rows.append((sym, s, f"b{i * 4 // n_days}", float(rng.normal())))
    d = pd.DataFrame(rows, columns=["symbol", "session", "block", "score"])
    return d.sort_values(["session", "symbol"]).reset_index(drop=True)


def test_cutoff_history_uses_only_earlier_blocks():
    d = _stitched()
    cut = G.block_cutoff(d, 0.6, 10)
    single = E.panel_expanding_cutoff(d, 0.6, 10).to_numpy()            # one causal pass over the stitched frame
    np.testing.assert_allclose(cut, single, equal_nan=True)
    last = d["block"] == "b3"
    d2 = d.copy()
    d2.loc[last, "score"] = d2.loc[last, "score"] + 100.0               # a later block changes ...
    cut2 = G.block_cutoff(d2, 0.6, 10)
    np.testing.assert_allclose(cut[~last.to_numpy()], cut2[~last.to_numpy()], equal_nan=True)   # ... earlier cutoffs do not
    first = d["block"] == "b0"
    assert np.isnan(cut[first.to_numpy()][:3 * 10]).all() or np.isnan(cut[first.to_numpy()]).sum() == 3 * 10
    b1 = (d["block"] == "b1").to_numpy()
    assert np.isfinite(cut[b1]).all()                                    # warmed up by block b0's scores
    # the first row of block b1 uses exactly block b0's scores of its symbol
    r0 = d[b1].iloc[0]
    expect = np.quantile(d[(d["block"] == "b0") & (d["symbol"] == r0["symbol"])]["score"], 0.6)
    assert cut[d.index[b1][0]] == pytest.approx(expect)


def test_timing_metrics_use_the_block_cutoff(signal_world, tmp_path):
    c = cell(signal_world[0], track="timing", target={"kind": "up", "horizon": 5})
    run([c], tmp_path, signal_world)
    r = G.read_results(tmp_path / "results.jsonl")[0]
    pq = pd.read_parquet(tmp_path / "predictions" / f"{r['cell_key']}_1.parquet")
    st = _settings()
    d = pq.assign(cutoff=G.block_cutoff(pq, st.timing_q, st.min_obs))
    ex = E.timing_excess(d, st.cost_bps, cutoff_col="cutoff").excess
    assert r["primary"]["value"] == pytest.approx(ex, rel=1e-9)


# ------------------------------------------------------------------ stores
def test_timing_over_two_stores_uses_the_columns_in_both(tmp_path):
    cache, root = tmp_path / "cache", tmp_path / "stores"
    etfs, stocks = _unlocked(6, "ETF"), _unlocked(4, "STK")
    build_store(root, "stocks", stocks, cache, 0.006, 3, extra_cols=("x_rank", "x_z"))
    build_store(root, "etf", etfs, cache, 0.006, 4)
    syms = etfs + stocks
    world = (root, None, cache)
    discovered = G.discover_stores(root)
    assert [n for n, _, _ in discovered] == ["stocks", "etf"]
    c = cell(root, track="timing", target={"kind": "up", "horizon": 5}, symbols=syms, groups=["t"])
    run([c], tmp_path / "o", world, settings=_settings(control_seeds=(1, 2)))
    r = G.read_results(tmp_path / "o" / "results.jsonl")[0]
    assert r["status"] == "ok" and r["n_features"] == 4                 # t_signal t_noise t_vol t_missing
    assert sorted(r["symbols"]) == sorted(syms)
    # x_ exists in the stocks store only: a cell that needs it on the mixed set is refused, naming the group
    bad = cell(root, track="timing", target={"kind": "up", "horizon": 5}, symbols=syms, groups=["t", "x"])
    out = tmp_path / "bad"
    res = run([bad], out, world)
    assert res["errors"] == 1 and "feature group 'x'" in json.loads((out / "errors.jsonl").read_text().splitlines()[0])["error"]
    # ... but on the stocks alone it works
    ok = cell(root, symbols=stocks, groups=["t", "x"], target={"kind": "rel_return", "horizon": 5})
    assert run([ok], tmp_path / "ok", world, settings=_settings(control_seeds=(1, 2)))["done"] == 1
    assert G.read_results(tmp_path / "ok" / "results.jsonl")[0]["n_features"] == 4 + 2
    with pytest.raises(ValueError, match="in no store"):
        G.load_panel(root, ("NOPE",), "timing", cache)


def test_coverage_flags_are_features(signal_world, tmp_path):
    panel = G.get_panel(str(signal_world[0]), tuple(SYMS), "selection", str(signal_world[2]))
    assert "t_missing" in panel.columns and "t_signal" in panel.columns
    assert "listed" not in panel.columns and "label_bar_filled" not in panel.columns
    assert panel.ok.all()


def test_rows_with_label_bar_filled_or_filled_are_not_used(tmp_path):
    cache, root = tmp_path / "cache", tmp_path / "stores"
    d = build_store(root, "stocks", SYMS, cache, 0.006, 5)
    df = pd.read_parquet(d / "features.parquet")
    sess = df.index.get_level_values("session")
    df.loc[(SYMS[0], sess[sess.isin(pd.to_datetime(["2019-02-04"]))][0]), "label_bar_filled"] = True
    df.loc[(SYMS[1], pd.Timestamp("2019-02-05")), "filled"] = True
    df.loc[(SYMS[2], pd.Timestamp("2019-02-06")), "ended"] = True
    df.loc[(SYMS[3], pd.Timestamp("2019-02-07")), "listed"] = False
    df.to_parquet(d / "features.parquet")
    p = G.load_panel(root, tuple(SYMS), "selection", cache)
    bad = p.index[~p.ok]
    assert sorted(bad.tolist()) == sorted([(SYMS[0], pd.Timestamp("2019-02-04")), (SYMS[1], pd.Timestamp("2019-02-05")),
                                           (SYMS[2], pd.Timestamp("2019-02-06")), (SYMS[3], pd.Timestamp("2019-02-07"))])


# ------------------------------------------------------------------ aggregation
def _row(key, seed, value, kind="up", horizon=5, family="lgbm", months=12, groups=("t",), track="timing", p=0.04,
         status="ok", name=None):
    return {"cell_key": key, "seed": seed, "track": track, "status": status,
            "primary": {"name": name or G.PRIMARY[track], "value": value},
            "controls": {"p_value": p},
            "config": {"target": {"kind": kind, "horizon": horizon}, "family": family, "train_months": months,
                       "groups": list(groups)}}


def test_factor_effects_means_stds_and_ranking():
    rows = [_row("a", 1, 0.10), _row("a", 2, 0.12), _row("a", 3, 0.14),                  # mean .12, std .02 -> .10
            _row("b", 1, 0.30), _row("b", 2, 0.00), _row("b", 3, 0.30),                  # mean .20, std .1732 -> .0268
            _row("c", 1, 0.05, kind="terciles", family="xgb"), _row("c", 2, 0.07, kind="terciles", family="xgb"),
            _row("s", 1, 9.0, track="selection"),                                        # other track
            _row("e", 1, 5.0, status="no_predictions"), _row("v", 1, 0.9, name="spearman"),
            _row("n", 1, None)]
    # cell b differs from a only by horizon
    for r in rows:
        if r["cell_key"] == "b":
            r["config"]["target"]["horizon"] = 10
    res = G.factor_effects(rows, "timing")
    cells = res["cells"]
    assert list(cells["cell_key"]) == ["a", "c", "b"]                                    # .10, .05-.014=.036, .0268
    a = cells[cells["cell_key"] == "a"].iloc[0]
    assert a["mean"] == pytest.approx(0.12) and a["std"] == pytest.approx(0.02) and a["score"] == pytest.approx(0.10)
    assert a["n_seeds"] == 3 and a["p_value"] == pytest.approx(0.04)
    f = res["factors"]
    h5, h10 = f.loc[("horizon", "5")], f.loc[("horizon", "10")]
    assert h5["n_cells"] == 2 and h5["mean"] == pytest.approx((0.12 + 0.06) / 2)
    assert h5["std"] == pytest.approx(np.std([0.12, 0.06], ddof=1))
    assert h10["n_cells"] == 1 and h10["mean"] == pytest.approx(0.2) and np.isnan(h10["std"])
    assert f.loc[("family", "xgb"), "n_cells"] == 1 and f.loc[("target", "terciles"), "mean"] == pytest.approx(0.06)
    assert set(f.index.get_level_values("factor")) == {"target", "horizon", "family", "train_months", "groups"}
    assert f.loc[("groups", "t"), "share_positive"] == 1.0
    sel = G.factor_effects(rows, "selection")["cells"]
    assert list(sel["cell_key"]) == ["s"]
    assert G.factor_effects([], "timing")["cells"].empty
    with pytest.raises(ValueError):
        G.factor_effects(rows, "other")


def test_factor_effects_reads_a_results_file(signal_world, tmp_path):
    run([cell(signal_world[0], seeds=[1, 2])], tmp_path, signal_world)
    res = G.factor_effects(tmp_path / "results.jsonl", "selection")
    assert len(res["cells"]) == 1 and res["cells"]["n_seeds"].iloc[0] == 2
    assert ("target", "rel_return") in res["factors"].index


# ------------------------------------------------------------------ job wiring
def test_route_validates_and_enqueues(signal_world, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import ml_search as api
    queued = []

    class Q:
        def queue_task(self, **kw):
            queued.append(kw)
            return "task-1"
    monkeypatch.setattr(api, "get_task_queue", lambda: Q())
    app = FastAPI()
    app.include_router(api.router, prefix="/api/ml-search")
    cl = TestClient(app)
    ok = cl.post("/api/ml-search/grid", json={"out_dir": "o", "cpu_slots": 2, "cells": [cell(signal_world[0])]})
    assert ok.status_code == 200 and ok.json() == {"task_id": "task-1", "cells": 1}
    assert queued[0]["task_type"] == "ml_grid" and queued[0]["payload"]["cpu_slots"] == 2
    locked = cl.post("/api/ml-search/grid", json={"out_dir": "o", "cpu_slots": 2,
                                                  "cells": [cell(signal_world[0], symbols=SYMS[:4] + ["NVDA"])]})
    assert locked.status_code == 400 and "NVDA" in locked.json()["detail"]
    assert cl.post("/api/ml-search/grid", json={"out_dir": "o", "cpu_slots": 2}).status_code == 400
    assert cl.post("/api/ml-search/grid", json={"out_dir": "o", "cpu_slots": 2,
                                                "cells": [{**cell(signal_world[0]), "bogus": 1}]}).status_code == 400
    assert len(queued) == 1


def test_handler_is_registered_in_main_and_the_cli_exists():
    from pathlib import Path
    root = Path(G.__file__).resolve().parents[2]
    main_src = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "register_handler('ml_grid', handle_ml_grid)" in main_src
    launcher = (root.parent / "ba2test_launcher.py").read_text(encoding="utf-8")
    assert '"ml-grid": lambda: _cmd_ml_grid(args)' in launcher and 'sub.add_parser("ml-grid"' in launcher


# ------------------------------------------------------------------ audit: no look-ahead, no joined targets
_TARGET_NAMES = r"^(ret|vol|rel_ret|quintile|beats|rank_rel|up|z|realvol|target_end)_\d+$|^(atr_pct|session_group)$"


def _spy(monkeypatch):
    """Record every adapter fit (train / validation matrices and names) and every score call."""
    real = G.M.make_adapter
    fits, scores = [], []

    def make(family, n_jobs=1, params=None, **kw):
        ad = real(family, n_jobs, params, **kw)
        fit, score = ad.fit, ad.score

        def spy_fit(Xt, yt, Xv, yv, task, seed, groups_train=None, groups_val=None, feature_names=None):
            fits.append({"Xt": np.array(Xt), "Xv": np.array(Xv), "names": list(feature_names), "seed": seed,
                         "yt": np.array(yt)})
            return fit(Xt, yt, Xv, yv, task, seed, groups_train=groups_train, groups_val=groups_val,
                       feature_names=feature_names)

        def spy_score(X):
            scores.append(np.array(X))
            return score(X)
        ad.fit, ad.score = spy_fit, spy_score
        return ad
    monkeypatch.setattr(G.M, "make_adapter", make)
    return fits, scores


def _locate(world, X, names):
    """(symbol, session) of every row of ``X``, found by its (t_signal, t_noise) pair in the store as given."""
    df = pd.read_parquet(world[1] / "features.parquet")
    key = {(np.float32(a), np.float32(b)): ix for a, b, ix in zip(df["t_signal"], df["t_noise"], df.index)}
    i, j = names.index("t_signal"), names.index("t_noise")
    return [key[(X[r, i], X[r, j])] for r in range(len(X))], df


@pytest.mark.parametrize("track,kind", [("selection", "rel_return"), ("timing", "up")])
def test_each_block_trains_strictly_before_it_and_validates_inside_the_window(signal_world, tmp_path, monkeypatch,
                                                                              track, kind):
    fits, scores = _spy(monkeypatch)
    H = 5
    run([cell(signal_world[0], track=track, target={"kind": kind, "horizon": H})], tmp_path, signal_world,
        settings=_settings(control_seeds=(1,)))
    st = _settings()
    blocks = G.W.blocks(st.eval_start, st.eval_end, "Q")
    assert len(fits) == len(blocks) == len(scores) == 6
    sess = pd.DatetimeIndex(pd.to_datetime(P.regular_sessions(FIRST, LAST)["session"]))
    pos = {s: i for i, s in enumerate(sess)}
    for b, f, sc in zip(blocks, fits, scores):
        bstart = pd.Timestamp(b.start)
        win = G.W.train_window(b, 12, st.data_start)
        fit_ix, df = _locate(signal_world, f["Xt"], f["names"])
        val_ix, _ = _locate(signal_world, f["Xv"], f["names"])
        fit_s = np.array([s for _, s in fit_ix])
        val_s = np.array([s for _, s in val_ix])

        def tend(ss):                                   # the session on which each label ends
            return np.array([sess[pos[s] + H] for s in ss])
        assert len(fit_s) and len(val_s)
        assert fit_s.min() >= pd.Timestamp(win.start) and val_s.max() < bstart               # inside the window
        assert tend(np.r_[fit_s, val_s]).max() < bstart                                       # max(target_end) < block.start
        assert val_s.min() >= fit_s.max() and tend(fit_s).max() < val_s.min()                 # validation is later, purged
        pred_ix, _ = _locate(signal_world, sc, f["names"])
        pred_s = np.array([s for _, s in pred_ix])
        assert pred_s.min() >= bstart and pred_s.max() <= pd.Timestamp(b.end)                 # scores only inside the block


def test_different_seeds_change_the_scores_and_the_same_seed_does_not(signal_world, tmp_path):
    c = cell(signal_world[0], seeds=[1, 2])
    run([c], tmp_path / "a", signal_world, settings=_settings(control_seeds=(1,)))
    run([c], tmp_path / "b", signal_world, settings=_settings(control_seeds=(1,)))
    key = G.validate_cell(c).key

    def rd(d, s):
        return pd.read_parquet(d / "predictions" / f"{key}_{s}.parquet")["score"].to_numpy()
    assert not np.allclose(rd(tmp_path / "a", 1), rd(tmp_path / "a", 2))
    assert np.array_equal(rd(tmp_path / "a", 1), rd(tmp_path / "b", 1))


def test_the_grid_feeds_the_model_the_stores_columns_only_never_a_target(signal_world, tmp_path, monkeypatch):
    fits, scores = _spy(monkeypatch)
    run([cell(signal_world[0])], tmp_path, signal_world, settings=_settings(control_seeds=(1,)))
    import re
    store_cols = [c for c in pd.read_parquet(signal_world[1] / "features.parquet").columns if c.startswith("t_")]
    panel = G.get_panel(str(signal_world[0]), tuple(SYMS), "selection", str(signal_world[2]))
    assert not [c for c in panel.columns if re.match(_TARGET_NAMES, c)]
    assert set(panel.columns) == set(store_cols) | {"t_missing"}
    for f in fits:
        assert set(f["names"]) == set(panel.columns) and not [c for c in f["names"] if re.match(_TARGET_NAMES, c)]
        ix, df = _locate(signal_world, f["Xt"], f["names"])
        raw = df.loc[ix, [n for n in f["names"] if n != "t_missing"]].to_numpy(dtype=np.float32)
        got = f["Xt"][:, [i for i, n in enumerate(f["names"]) if n != "t_missing"]]
        assert np.array_equal(raw, got, equal_nan=True)                        # exactly the store's values, untouched
    tg = panel.targets(5)
    assert not set(tg.columns) & set(panel.columns)                            # targets and features never share a name


def test_leak_canary_a_future_return_column_is_used_only_when_its_group_is_selected(canary_world, tmp_path, monkeypatch):
    fits, _ = _spy(monkeypatch)
    safe = cell(canary_world[0], groups=["t"], target={"kind": "rel_return", "horizon": 5})
    leaky = cell(canary_world[0], groups=["t", "x"], target={"kind": "rel_return", "horizon": 5})
    run([safe], tmp_path / "s", canary_world, settings=_settings(control_seeds=(1,)))
    assert fits and all("x_canary" not in f["names"] for f in fits)
    rs = G.read_results(tmp_path / "s" / "results.jsonl")[0]
    assert abs(rs["metrics"]["rank_ic"]["mean"]) < 0.04                         # no signal without the canary
    fits.clear()
    run([leaky], tmp_path / "l", canary_world, settings=_settings(control_seeds=(1,)))
    assert all("x_canary" in f["names"] for f in fits)
    rl = G.read_results(tmp_path / "l" / "results.jsonl")[0]
    assert rl["metrics"]["rank_ic"]["mean"] > 0.5                               # the canary really leaks if it is selected


# ------------------------------------------------------------------ iterations, ties, deterministic families
def test_rows_record_best_iterations_and_the_low_iteration_share(noise_world, tmp_path):
    out = tmp_path / "o"
    run([cell(noise_world[0], seeds=[1]),
         cell(noise_world[0], family="linear", params={}, seeds=[1], target={"kind": "up", "horizon": 5})],
        out, noise_world)
    rows = {r["config"]["family"]: r for r in G.read_results(out / "results.jsonl")}
    lg = rows["lgbm"]
    its = [b["n_iter"] for b in lg["blocks"]]
    assert len(its) == 6 and all(isinstance(i, int) and i >= 1 for i in its)
    assert lg["low_iter_share"] == pytest.approx(np.mean([i <= G.LOW_ITER for i in its]))
    assert rows["linear"]["low_iter_share"] is None and all(b["n_iter"] is None for b in rows["linear"]["blocks"])
    assert G.LOW_ITER == 3


def test_quantile_best_iteration_is_the_median_level_and_levels_are_kept(signal_world, tmp_path):
    run([cell(signal_world[0], track="timing", target={"kind": "quantile", "horizon": 5})], tmp_path, signal_world,
        settings=_settings(control_seeds=(1,)))
    b = G.read_results(tmp_path / "results.jsonl")[0]["blocks"][0]
    assert len(b["n_iter_levels"]) == 9 and b["n_iter"] == b["n_iter_levels"][4]


def test_blocks_record_their_audit_fields(signal_world, tmp_path):
    run([cell(signal_world[0])], tmp_path, signal_world, settings=_settings(control_seeds=(1,)))
    for b in G.read_results(tmp_path / "results.jsonl")[0]["blocks"]:
        assert b["window_start"] <= b["fit_first"] <= b["fit_last"] <= b["val_first"] <= b["val_last"] < b["start"]
        assert b["train_target_end_max"] < b["start"] <= b["end"]


def test_deterministic_families_are_marked_in_factor_effects():
    rows = [_row("lin", s, 0.1, family="linear") for s in (1, 2, 3)] + \
           [_row("gb", 1, 0.05), _row("gb", 2, 0.15), _row("nv", 1, 0.0, family="naive")]
    for r in rows:
        r["low_iter_share"] = 0.5 if r["cell_key"] == "gb" else None
    res = G.factor_effects(rows, "timing")
    c = res["cells"].set_index("cell_key")
    assert c.loc["lin", "std"] == pytest.approx(0.0, abs=1e-12) and bool(c.loc["lin", "deterministic"]) and bool(c.loc["nv", "deterministic"])
    assert not bool(c.loc["gb", "deterministic"]) and c.loc["gb", "low_iter_share"] == pytest.approx(0.5)
    assert np.isnan(c.loc["lin", "low_iter_share"])
    f = res["factors"]
    assert f.loc[("family", "linear"), "n_deterministic"] == 1 and f.loc[("family", "lgbm"), "n_deterministic"] == 0
    assert f.loc[("family", "lgbm"), "low_iter_share"] == pytest.approx(0.5)
    assert G.DETERMINISTIC_FAMILIES == ("naive", "linear")


def test_the_cell_schema_documents_the_two_barrier_kinds():
    assert "triple_barrier" in G.__doc__ and "binary_barrier" in G.__doc__ and "Cell schema" in G.__doc__
    assert G.KINDS["triple_barrier"].task == "multiclass" and G.KINDS["binary_barrier"].task == "binary"


# ------------------------------------------------------------------ resume identity
def _copy_tree(src, dst):
    import shutil
    shutil.copytree(src, dst)
    return dst


def test_cell_key_follows_the_store_signature_not_its_path(signal_world, tmp_path):
    a = G.validate_cell(cell(signal_world[0]))
    moved = _copy_tree(signal_world[0], tmp_path / "elsewhere")
    b = G.validate_cell(cell(moved))
    assert a.key == b.key and a.signature == b.signature and a.config()["store"] != b.config()["store"]
    meta = moved / "stocks" / "features.parquet.meta.json"
    m = json.loads(meta.read_text(encoding="utf-8"))
    m["ensure"]["inputs"]["code"] = "synthetic-2"                       # the store was rebuilt from other inputs
    meta.write_text(json.dumps(m), encoding="utf-8")
    assert G.validate_cell(cell(moved)).key != a.key


def test_resume_works_after_the_store_moves(signal_world, tmp_path):
    out = tmp_path / "o"
    run([cell(signal_world[0])], out, signal_world, settings=_settings(control_seeds=(1,)))
    moved = _copy_tree(signal_world[0], tmp_path / "moved")
    res = run([cell(moved)], out, (moved, None, signal_world[2]), settings=_settings(control_seeds=(1,)))
    assert res["tasks"] == 0 and res["skipped_existing"] == 1


def test_rows_record_settings_and_store_and_daily_identity(signal_world, tmp_path):
    run([cell(signal_world[0])], tmp_path, signal_world)
    r = G.read_results(tmp_path / "results.jsonl")[0]
    ident = r["identity"]
    assert ident["settings"]["cost_bps"] == 7.0 and ident["settings"]["timing_q"] == 0.6
    assert ident["settings"]["eval_start"] == "2019-01-01" and ident["settings"]["control_seeds"] == [1, 2, 3, 4, 5]
    assert "cores" not in ident["settings"]                              # thread count does not change a result
    assert ident["store_signature"] == G.validate_cell(cell(signal_world[0])).signature
    assert ident["daily_fingerprint"] == G.daily_fingerprint(signal_world[2], tuple(sorted(SYMS)))


def test_resume_refuses_rows_made_under_other_settings_unless_told_to_rerun(signal_world, tmp_path):
    out = tmp_path / "o"
    c = cell(signal_world[0])
    run([c], out, signal_world, settings=_settings(control_seeds=(1, 2)))
    before = (out / "results.jsonl").read_text(encoding="utf-8")
    other = _settings(control_seeds=(1, 2), cost_bps=12.0)
    with pytest.raises(G.ResumeMismatchError, match="cost_bps") as ei:
        run([c], out, signal_world, settings=other)
    assert "rerun-mismatched" in str(ei.value) and (out / "results.jsonl").read_text(encoding="utf-8") == before
    same_but_threads = _settings(control_seeds=(1, 2), cores=7)
    assert run([c], out, signal_world, settings=same_but_threads)["tasks"] == 0           # cores is not identity
    res = run([c], out, signal_world, settings=other, rerun_mismatched=True)
    assert res["tasks"] == 1 and res["done"] == 1
    rows = G.read_results(out / "results.jsonl")
    assert len(rows) == 1 and rows[0]["identity"]["settings"]["cost_bps"] == 12.0
    stale = G.read_results(out / "results.stale.jsonl")
    assert len(stale) == 1 and stale[0]["identity"]["settings"]["cost_bps"] == 7.0


def test_resume_refuses_when_the_daily_cache_changed(signal_world, tmp_path):
    import os
    import shutil
    out = tmp_path / "o"
    cache = tmp_path / "cache"
    shutil.copytree(signal_world[2], cache)
    world = (signal_world[0], signal_world[1], cache)
    c = cell(signal_world[0])
    run([c], out, world, settings=_settings(control_seeds=(1,)))
    f = cache / "AlpacaHistoryOHLCVProvider" / f"{SYMS[0]}_1d.parquet"
    os.utime(f, (1_900_000_000, 1_900_000_000))
    with pytest.raises(G.ResumeMismatchError, match="daily_fingerprint"):
        run([c], out, world, settings=_settings(control_seeds=(1,)))


def test_a_row_without_identity_is_a_mismatch_and_other_cells_are_not_checked(signal_world, tmp_path):
    out = tmp_path / "o"
    c = cell(signal_world[0])
    run([c], out, signal_world, settings=_settings(control_seeds=(1,)))
    row = G.read_results(out / "results.jsonl")[0]
    other = {**row, "cell_key": "ffffffffffff"}                    # a row of a cell that is not in this run
    legacy = {k: v for k, v in row.items() if k != "identity"}
    (out / "results.jsonl").write_text(json.dumps(other) + "\n" + json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(G.ResumeMismatchError, match="no identity"):
        run([c], out, signal_world, settings=_settings(control_seeds=(1,)))
    ok = run([c], out, signal_world, settings=_settings(control_seeds=(1,)), rerun_mismatched=True)
    assert ok["done"] == 1
    keys = [r["cell_key"] for r in G.read_results(out / "results.jsonl")]
    assert sorted(keys) == sorted(["ffffffffffff", G.validate_cell(c).key])         # the foreign row stayed


def test_the_rerun_flag_is_wired_through_the_payload_and_the_cli():
    from pathlib import Path
    root = Path(G.__file__).resolve().parents[2]
    assert "--rerun-mismatched" in (root.parent / "ba2test_launcher.py").read_text(encoding="utf-8")
    assert "rerun_mismatched" in (root / "app" / "api" / "ml_search.py").read_text(encoding="utf-8")
