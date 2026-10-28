"""Stage-1 grid runner (app/services/ml_grid.py) on tiny synthetic stores with a planted (or no) signal."""
import json
import os
import re
from datetime import date
from pathlib import Path

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


def payload(world, cells=None, **over):
    """A complete ``ml_grid`` payload: the handler takes no defaults, every key is explicit."""
    p = {"out_dir": "out", "cpu_slots": 1, "gpu_slots": 0, "config_path": None,
         "cells": cells if cells is not None else [cell(world[0])], "limit_cells": None,
         "cache_root": str(world[2]), "settings": None, "cores": 2, "rerun_mismatched": False}
    p.update(over)
    return p


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
        G.handle_ml_grid("t", payload(signal_world, cells=[bad], out_dir=str(tmp_path)))
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
    res = G.handle_ml_grid("cli", payload(signal_world, cells=cells, out_dir=str(tmp_path / "pool"), cpu_slots=2,
                                          settings={**_settings().to_dict(), "cores": 2}))
    assert res["done"] == 2 and res["errors"] == 0
    a = {(r["cell_key"], r["seed"]): r["primary"]["value"] for r in G.read_results(tmp_path / "inline" / "results.jsonl")}
    b = {(r["cell_key"], r["seed"]): r["primary"]["value"] for r in G.read_results(tmp_path / "pool" / "results.jsonl")}
    assert a == b and len(a) == 2


def test_handler_payload_validation(signal_world, tmp_path):
    c = cell(signal_world[0])
    full = payload(signal_world, out_dir=str(tmp_path / "o"))
    for key in full:
        with pytest.raises(ValueError, match=f"missing {key!r}"):                  # every key is required, none defaulted
            G.handle_ml_grid("t", {k: v for k, v in full.items() if k != key})
    with pytest.raises(ValueError, match="exactly one"):
        G.handle_ml_grid("t", {**full, "cells": None})
    with pytest.raises(ValueError, match="exactly one"):
        G.handle_ml_grid("t", {**full, "cells": [c], "config_path": "x.json"})
    with pytest.raises(ValueError, match="unknown grid settings"):
        G.handle_ml_grid("t", {**full, "settings": {"nope": 1}})
    for bad in ({"cpu_slots": 0}, {"gpu_slots": -1}, {"limit_cells": 0}, {"cores": None}, {"cores": 0},
                {"rerun_mismatched": "yes"}):
        with pytest.raises(ValueError):
            G.handle_ml_grid("t", {**full, **bad})


def test_unusable_groups_are_refused_before_anything_starts(signal_world, tmp_path, recording_pool):
    out = tmp_path / "out"
    bad = cell(signal_world[0], groups=["x"])                       # the synthetic store has no x_ columns
    with pytest.raises(ValueError, match="feature group 'x'") as ei:
        run([cell(signal_world[0]), bad], out, signal_world, inline=False, cpu_slots=2)
    assert "no column" in str(ei.value) and bad["groups"] == ["x"]
    assert not recording_pool.instances and not (out / "results.jsonl").exists()           # no pool, no work, no lock
    assert not (out / ".ml_grid.lock").exists()


def test_gpu_pool_hook(signal_world, tmp_path, monkeypatch):
    """A cell routed to the gpu pool needs gpu slots (the real GPU families are tested in test_ml_grid_seq)."""
    c = cell(signal_world[0], blocks="H")
    monkeypatch.setitem(G.FAMILY_POOL, "lgbm", "gpu")
    with pytest.raises(ValueError, match="gpu pool"):
        run([c], tmp_path / "a", signal_world)
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
    pq = pq.astype({c: "float64" for c in pq.columns if pq[c].dtype == "float32"})
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
    with pytest.raises(ValueError, match="feature group 'x'"):
        run([bad], tmp_path / "bad", world)
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
         status="ok", name=None, fitted=31, blocks=31):
    return {"cell_key": key, "seed": seed, "track": track, "status": status, "n_blocks": blocks,
            "n_blocks_fitted": fitted, "primary": {"name": name or G.PRIMARY[track], "value": value},
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
    assert c.loc["nv", "std"] == 0.0 and c.loc["nv", "n_seeds"] == 1            # run once: std is 0 by construction
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
    assert ident["daily_fingerprint"] == G.daily_fingerprint(signal_world[2], tuple(sorted(SYMS)), FIRST, LAST)


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


def _own_cache(signal_world, tmp_path):
    import shutil
    cache = tmp_path / "cache"
    shutil.copytree(signal_world[2], cache)
    return (signal_world[0], signal_world[1], cache), cache / "AlpacaHistoryOHLCVProvider" / f"{SYMS[0]}_1d.parquet"


def test_a_touch_or_an_appended_bar_does_not_force_a_rerun_but_a_changed_bar_does(signal_world, tmp_path):
    out = tmp_path / "o"
    world, f = _own_cache(signal_world, tmp_path)
    c = cell(signal_world[0])
    cfg = dict(settings=_settings(control_seeds=(1,)))
    run([c], out, world, **cfg)
    os.utime(f, (1_900_000_000, 1_900_000_000))                                          # a touch
    assert run([c], out, world, **cfg)["tasks"] == 0
    df = pd.read_parquet(f)                                                              # an append-only refresh
    extra = df.iloc[[-1]].copy()
    extra["Date"] = extra["Date"] + pd.Timedelta(days=1)
    extra["effective_date"] = extra["Date"]
    pd.concat([df, extra]).to_parquet(f, index=False)
    assert run([c], out, world, **cfg)["tasks"] == 0
    changed = pd.read_parquet(f)                                                         # a bar INSIDE the window changes
    changed.loc[changed["Date"] == pd.Timestamp("2019-03-14"), "Close"] *= 1.0001
    changed.to_parquet(f, index=False)
    with pytest.raises(G.ResumeMismatchError, match="daily_fingerprint"):
        run([c], out, world, **cfg)


def test_the_fingerprint_is_taken_from_the_bars_the_panel_reads(signal_world, tmp_path):
    panel = G.get_panel(str(signal_world[0]), tuple(sorted(SYMS)), "selection", str(signal_world[2]))
    assert panel.daily_fingerprint == G.daily_fingerprint(signal_world[2], tuple(sorted(SYMS)), FIRST, LAST)
    assert G.daily_fingerprint(signal_world[2], SYMS[:3], FIRST, LAST) != panel.daily_fingerprint


def test_a_row_whose_predictions_file_is_missing_is_not_reused(signal_world, tmp_path):
    out = tmp_path / "o"
    c = cell(signal_world[0])
    run([c], out, signal_world, settings=_settings(control_seeds=(1,)))
    pred = next((out / "predictions").glob("*.parquet"))
    pred.unlink()
    res = run([c], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert res["tasks"] == 1 and res["done"] == 1 and pred.exists()
    assert len(G.read_results(out / "results.jsonl")) == 1 and len(G.read_results(out / "results.stale.jsonl")) == 1


def test_a_no_predictions_row_needs_no_file(signal_world, tmp_path):
    out = tmp_path / "o"
    c = cell(signal_world[0], target={"kind": "terciles", "horizon": 5}, track="timing")
    run([c], out, signal_world, settings=_settings(min_count=10 ** 6, control_seeds=(1,)))

    res = run([c], out, signal_world, settings=_settings(min_count=10 ** 6, control_seeds=(1,)))
    assert res["tasks"] == 0                                              # the status says why there is no file


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


# ================================================================== pool lifecycle and operations
def die_once_task(cfg, seed, expected_key):
    """A task function for a spawned worker: the FIRST call anywhere kills its process (os._exit), the rest run."""
    import os
    try:
        fd = os.open(os.environ["GRID_TEST_DIE_MARKER"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return G._run_task(cfg, seed, expected_key)
    os.close(fd)
    os._exit(3)


def die_on_seed2_task(cfg, seed, expected_key):
    import os
    if seed == 2:
        os._exit(3)
    return G._run_task(cfg, seed, expected_key)


class RecordingPool(G.ProcessPoolExecutor):
    """Records submissions, the peak number of open futures and every shutdown call."""
    instances = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.futures, self.shutdowns, self.max_open = [], [], 0
        RecordingPool.instances.append(self)

    def submit(self, fn, *a, **k):
        fu = super().submit(fn, *a, **k)
        self.futures.append(fu)
        self.max_open = max(self.max_open, sum(1 for f in self.futures if not f.done()))
        return fu

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdowns.append((wait, cancel_futures))
        return super().shutdown(wait=wait, cancel_futures=cancel_futures)


@pytest.fixture
def recording_pool(monkeypatch):
    RecordingPool.instances = []
    monkeypatch.setattr(G, "ProcessPoolExecutor", RecordingPool)
    return RecordingPool


def test_a_worker_that_dies_mid_task_is_replaced_and_the_run_completes(signal_world, tmp_path, monkeypatch,
                                                                       recording_pool):
    monkeypatch.setenv("GRID_TEST_DIE_MARKER", str(tmp_path / "died"))
    out = tmp_path / "o"
    res = run([cell(signal_world[0], seeds=[1, 2, 3])], out, signal_world, inline=False, cpu_slots=2,
              task_fn=die_once_task, settings=_settings(control_seeds=(1,)))
    assert res["done"] == 3 and res["errors"] == 0 and res["pool_rebuilds"] >= 1
    assert sorted(r["seed"] for r in G.read_results(out / "results.jsonl")) == [1, 2, 3]
    assert len(recording_pool.instances) == 1 + res["pool_rebuilds"]
    assert all(p.shutdowns[-1] == (False, True) for p in recording_pool.instances)


def test_a_task_that_always_kills_its_worker_ends_as_an_error_after_three_retries(signal_world, tmp_path,
                                                                                   recording_pool):
    out = tmp_path / "o"
    res = run([cell(signal_world[0], seeds=[1, 2, 3])], out, signal_world, inline=False, cpu_slots=1,
              task_fn=die_on_seed2_task, settings=_settings(control_seeds=(1,)))
    assert res["done"] == 2 and res["errors"] == 1
    assert sorted(r["seed"] for r in G.read_results(out / "results.jsonl")) == [1, 3]      # the neighbours survive
    err = G.read_results(out / "errors.jsonl")
    assert len(err) == 1 and err[0]["seed"] == 2 and "worker process died" in err[0]["error"]
    assert "3 retries" in err[0]["error"] and res["pool_rebuilds"] >= 4


def test_a_validate_cell_failure_mid_run_is_an_error_record_not_an_abort(signal_world, tmp_path, monkeypatch):
    good = cell(signal_world[0])
    bad = cell(signal_world[0], family="xgb", params={}, target={"kind": "regression", "horizon": 5})
    cells = [G.validate_cell(good), G.validate_cell(bad)]
    real = G.validate_cell

    def flaky(raw):
        if raw["family"] == "xgb":
            raise RuntimeError("store vanished")
        return real(raw)
    monkeypatch.setattr(G, "validate_cell", flaky)
    out = tmp_path / "o"
    res = G.run_grid(cells, out, 1, cache_root=signal_world[2], settings=_settings(control_seeds=(1,)), inline=True)
    assert res["done"] == 1 and res["errors"] == 1
    assert "store vanished" in G.read_results(out / "errors.jsonl")[0]["error"]


def test_the_worker_refuses_a_cell_whose_key_changed_since_planning(signal_world, tmp_path):
    c = G.validate_cell(cell(signal_world[0]))
    G._init_worker(_settings().to_dict(), str(signal_world[2]), str(tmp_path), 1)
    r = G._run_task(c.config(with_seeds=True), 1, "0123456789ab")
    assert "key" in r["error"] and r["cell_key"] == "0123456789ab"


def test_a_master_exception_leaves_no_queued_futures_and_uses_spawn(signal_world, tmp_path, recording_pool):
    def progress(msg, pct=None):
        if msg.startswith("ml_grid [2/"):
            raise RuntimeError("master boom")
    with pytest.raises(RuntimeError, match="master boom"):
        run([cell(signal_world[0], seeds=[1, 2, 3, 4, 5, 6])], tmp_path / "o", signal_world, inline=False,
            cpu_slots=1, progress=progress, settings=_settings(control_seeds=(1,)))
    pool = recording_pool.instances[-1]
    assert pool.shutdowns[-1] == (False, True)
    assert not [f for f in pool.futures if not (f.done() or f.running() or f.cancelled())]
    assert pool._mp_context.get_start_method() == "spawn"
    assert len(pool.futures) < 6                                    # the rest was never even submitted


def test_submission_is_bounded_to_twice_the_cpu_slots(signal_world, tmp_path, recording_pool):
    res = run([cell(signal_world[0], seeds=[1, 2, 3, 4, 5, 6])], tmp_path / "o", signal_world, inline=False,
              cpu_slots=1, settings=_settings(control_seeds=(1,)))
    assert res["done"] == 6 and recording_pool.instances[0].max_open <= 2


def test_a_cancel_check_stops_the_run_cooperatively(signal_world, tmp_path):
    out = tmp_path / "o"
    stop = lambda: len(G.read_results(out / "results.jsonl")) >= 1                # noqa: E731
    res = run([cell(signal_world[0], seeds=[1, 2, 3, 4])], out, signal_world, cancel_check=stop,
              settings=_settings(control_seeds=(1,)))
    assert res["cancelled"] is True and res["done"] == 1
    again = run([cell(signal_world[0], seeds=[1, 2, 3, 4])], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert again["tasks"] == 3 and again["cancelled"] is False             # a cancelled run resumes where it stopped


def test_a_cancel_check_stops_the_pool_run_too(signal_world, tmp_path, recording_pool):
    out = tmp_path / "o"
    stop = lambda: len(G.read_results(out / "results.jsonl")) >= 1                # noqa: E731
    res = run([cell(signal_world[0], seeds=[1, 2, 3, 4, 5, 6])], out, signal_world, cancel_check=stop, inline=False,
              cpu_slots=1, settings=_settings(control_seeds=(1,)))
    assert res["cancelled"] is True and 1 <= res["done"] < 6
    assert recording_pool.instances[-1].shutdowns[-1] == (False, True)


def test_the_queue_cancel_check_reads_the_task_status(monkeypatch):
    import app.services.task_queue as tq

    class Q:
        status = "running"

        def get_task_status(self, task_id):
            assert task_id == "t-1"
            return {"status": self.status}
    q = Q()
    monkeypatch.setattr(tq, "get_task_queue", lambda: q)
    check = G.make_cancel_check("t-1", interval=0.0)
    assert check() is False
    q.status = "cancelled"
    assert check() is True
    monkeypatch.setattr(tq, "get_task_queue", lambda: (_ for _ in ()).throw(RuntimeError("no queue")))
    assert G.make_cancel_check("cli", interval=0.0)() is False              # the CLI has no queue row


def test_the_cli_turns_ctrl_c_into_a_clean_exit(monkeypatch, capsys):
    import ba2test_launcher as L
    import types
    import app.services.ml_grid as grid

    def interrupted(task_id, payload):
        raise KeyboardInterrupt
    monkeypatch.setattr(grid, "handle_ml_grid", interrupted)
    args = types.SimpleNamespace(config="c.json", out="o", cpu_slots=1, gpu_slots=0, limit_cells=None,
                                 cache_root=None, cores=2, rerun_mismatched=False, report_only=None)
    assert L._cmd_ml_grid(args) == 130
    assert "interrupted" in capsys.readouterr().out


# ------------------------------------------------------------------ lock file
def test_a_second_run_in_the_same_out_dir_is_refused_and_a_stale_lock_is_taken_over(signal_world, tmp_path):
    import socket
    import subprocess
    import sys
    out = tmp_path / "o"
    out.mkdir()
    lock = out / ".ml_grid.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": "now"}), encoding="utf-8")
    with pytest.raises(G.GridLockedError, match="already running"):
        run([cell(signal_world[0])], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert lock.exists()                                             # the refused run did not touch the holder's lock
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert not G.pid_alive(dead.pid) and G.pid_alive(os.getpid())
    lock.write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname(), "started": "then"}), encoding="utf-8")
    res = run([cell(signal_world[0])], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert res["done"] == 1 and not lock.exists()                    # taken over, then released


def test_the_lock_is_released_after_an_exception(signal_world, tmp_path):
    def progress(msg, pct=None):
        if msg.startswith("ml_grid ["):
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        run([cell(signal_world[0])], tmp_path / "o", signal_world, progress=progress,
            settings=_settings(control_seeds=(1,)))
    assert not (tmp_path / "o" / ".ml_grid.lock").exists()


# ------------------------------------------------------------------ worker memory, ordering, threads, warnings
def test_the_initializer_loads_nothing_and_a_worker_keeps_at_most_two_panels(signal_world, tmp_path):
    G._PANELS.clear()
    G._init_worker(_settings().to_dict(), str(signal_world[2]), str(tmp_path), 1)
    assert len(G._PANELS) == 0                                       # panels are loaded lazily, by the task that needs them
    keys = []
    for n in (6, 7, 8):
        G.get_panel(str(signal_world[0]), tuple(sorted(SYMS[:n])), "selection", str(signal_world[2]))
        keys.append(next(reversed(G._PANELS)))
    assert G.PANEL_CACHE_SIZE == 2 and list(G._PANELS) == keys[1:]    # the oldest was evicted, with its cached targets
    G.get_panel(str(signal_world[0]), tuple(sorted(SYMS[:7])), "selection", str(signal_world[2]))
    assert list(G._PANELS) == [keys[2], keys[1]]                     # a hit becomes the most recent


def test_tasks_are_grouped_by_panel_then_slowest_family_first(signal_world):
    s = signal_world[0]
    mk = lambda syms, **o: G.validate_cell(cell(s, symbols=syms, params={} if o.get("family") != "lgbm" else TINY,  # noqa: E731
                                               **o))
    a, b = SYMS[:6], SYMS[:7]
    cells = [mk(a, family="linear", target={"kind": "up", "horizon": 5}), mk(b, family="lgbm"),
             mk(a, family="xgb", target={"kind": "regression", "horizon": 5}), mk(a, family="lgbm"),
             mk(b, family="xgb", target={"kind": "regression", "horizon": 5})]
    plan = G.plan_tasks(cells)
    assert [(tuple(sorted(c.symbols)) == tuple(sorted(a)), c.family) for c, _ in plan] == \
        [(True, "xgb"), (True, "lgbm"), (True, "linear"), (False, "xgb"), (False, "lgbm")]


def test_deterministic_families_run_once_and_say_so(signal_world, tmp_path):
    out = tmp_path / "o"
    c = cell(signal_world[0], family="linear", params={}, seeds=[3, 1, 2], target={"kind": "up", "horizon": 5})
    res = run([c], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert res["tasks"] == 1 and res["done"] == 1
    r = G.read_results(out / "results.jsonl")[0]
    assert r["seed"] == 1 and r["deterministic"] is True and r["seeds_run"] == 1
    assert run([c], out, signal_world, settings=_settings(control_seeds=(1,)))["tasks"] == 0
    stoch = cell(signal_world[0], seeds=[1, 2])
    run([stoch], out, signal_world, settings=_settings(control_seeds=(1,)))
    assert all(r["deterministic"] is False and r["seeds_run"] == 2
               for r in G.read_results(out / "results.jsonl") if r["config"]["family"] == "lgbm")


def test_a_worker_limits_its_blas_and_openmp_threads(tmp_path):
    from threadpoolctl import threadpool_info
    G._init_worker(_settings().to_dict(), str(tmp_path), str(tmp_path), 2)
    try:
        assert os.environ["OMP_NUM_THREADS"] == "2" and os.environ["OPENBLAS_NUM_THREADS"] == "2"
        assert all(p["num_threads"] <= 2 for p in threadpool_info())
    finally:
        G._WORKER["limits"].restore_original_limits()


def test_the_worker_does_not_silence_all_warnings(tmp_path):
    import warnings
    before = list(warnings.filters)
    G._init_worker(_settings().to_dict(), str(tmp_path), str(tmp_path), 1)
    added = [f for f in warnings.filters if f not in before]
    G._WORKER["limits"].restore_original_limits()
    assert not [f for f in added if f[0] == "ignore" and f[1] is None]             # no blanket ignore
    assert all(f[1] is not None for f in added)                                      # each filter names a message


# ------------------------------------------------------------------ store identity, normalisation, audit, route
def test_a_store_without_inputs_in_its_meta_says_to_rebuild_it(signal_world, tmp_path):
    moved = _copy_tree(signal_world[0], tmp_path / "old")
    meta = moved / "stocks" / "features.parquet.meta.json"
    m = json.loads(meta.read_text(encoding="utf-8"))
    del m["ensure"]["inputs"]
    meta.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="store meta lacks ensure.inputs: rebuild the store"):
        G.validate_cell(cell(moved))


def test_train_months_spellings_give_one_canonical_cell(signal_world):
    s = signal_world[0]
    a = G.validate_cell(cell(s, train_months=36))
    for spelling in ("36", 36.0, " 36 "):
        b = G.validate_cell(cell(s, train_months=spelling))
        assert b.train_months == 36 and isinstance(b.train_months, int) and b.key == a.key
    for spelling in ("expanding", "Expanding", " EXPANDING "):
        assert G.validate_cell(cell(s, train_months=spelling)).train_months == "expanding"
    for bad in (True, 18, "18", 36.5, None, "", [36]):
        with pytest.raises(ValueError, match="train_months"):
            G.validate_cell(cell(s, train_months=bad))


def test_rows_record_library_versions_and_the_code_hash(signal_world, tmp_path):
    run([cell(signal_world[0])], tmp_path, signal_world, settings=_settings(control_seeds=(1,)))
    v = G.read_results(tmp_path / "results.jsonl")[0]["versions"]
    import numpy
    import pandas
    assert v["numpy"] == numpy.__version__ and v["pandas"] == pandas.__version__ and v["lightgbm"]
    assert re.fullmatch(r"[0-9a-f]{12}", v["code"]) and "python" in v
    assert v["catboost"] is None or isinstance(v["catboost"], str)


def test_the_store_owner_helper_is_shared():
    avail = [("stocks", Path("a"), ["AAPL", "X"]), ("etf", Path("b"), ["SPY", "X"])]
    assert G.store_owners(avail) == {"AAPL": "stocks", "X": "stocks", "SPY": "etf"}
    assert G.stores_used(avail, ["SPY", "AAPL"]) == [("stocks", Path("a")), ("etf", Path("b"))]
    with pytest.raises(ValueError, match="in no store"):
        G.stores_used(avail, ["NOPE"], where="here")


def test_the_route_checks_limits_and_the_resume_identity_before_queueing(signal_world, tmp_path, monkeypatch):
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
    out = tmp_path / "o"
    body = {"out_dir": str(out), "cpu_slots": 2, "cells": [cell(signal_world[0])], "cache_root": str(signal_world[2]),
            "settings": _settings(control_seeds=(1,)).to_dict()}
    for bad in ({"cpu_slots": 0}, {"limit_cells": 0}, {"gpu_slots": -1}, {"cores": 0}):
        r = cl.post("/api/ml-search/grid", json={**body, **bad})
        assert r.status_code == 400, (bad, r.text)
    assert not queued
    run([cell(signal_world[0])], out, signal_world, settings=_settings(control_seeds=(1,)))
    mismatch = {**body, "settings": _settings(control_seeds=(1,), cost_bps=11.0).to_dict()}
    r = cl.post("/api/ml-search/grid", json=mismatch)
    assert r.status_code == 400 and "cost_bps" in r.json()["detail"] and not queued          # refused BEFORE queueing
    assert cl.post("/api/ml-search/grid", json={**mismatch, "rerun_mismatched": True}).status_code == 200
    ok = cl.post("/api/ml-search/grid", json={**body, "out_dir": str(tmp_path / "fresh")})
    assert ok.status_code == 200 and queued[-1]["payload"]["cores"] >= 1                    # cores made explicit
    bad_groups = {**body, "out_dir": str(tmp_path / "g"), "cells": [cell(signal_world[0], groups=["x"])]}
    assert cl.post("/api/ml-search/grid", json=bad_groups).status_code == 400
    import socket
    lock = tmp_path / "locked"
    lock.mkdir()
    (lock / ".ml_grid.lock").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": "x"}))
    r = cl.post("/api/ml-search/grid", json={**body, "out_dir": str(lock)})
    assert r.status_code == 400 and "already running" in r.json()["detail"]


# ------------------------------------------------------------------ factor_effects: coverage, filters, dedupe, median p
def test_factor_effects_drops_cells_with_low_block_coverage_and_says_why():
    rows = [_row("ok", s, 0.1 + 0.01 * s) for s in (1, 2, 3)] + \
           [_row("thin", s, 0.9, fitted=20) for s in (1, 2, 3)] + \
           [_row("mixed", 1, 0.5), _row("mixed", 2, 0.5, fitted=10)]
    res = G.factor_effects(rows, "timing")
    assert list(res["cells"]["cell_key"]) == ["ok"] and res["cells"]["coverage"].iloc[0] == 1.0
    ex = res["excluded"].set_index("cell_key")
    assert set(ex.index) == {"thin", "mixed"} and "coverage" in ex.loc["thin", "reason"]
    assert ex.loc["thin", "coverage"] == pytest.approx(20 / 31) and ex.loc["mixed", "coverage"] == pytest.approx(10 / 31)
    loose = G.factor_effects(rows, "timing", min_coverage=0.5)
    assert set(loose["cells"]["cell_key"]) == {"ok", "thin"}               # 'mixed' has a seed under 0.5
    assert G.factor_effects(rows, "timing", min_coverage=0.0)["excluded"].empty
    with pytest.raises(ValueError, match="min_coverage"):
        G.factor_effects(rows, "timing", min_coverage=1.5)


def test_factor_effects_max_low_iter_share_filter_is_optional():
    rows = [_row("flat", 1, 0.3), _row("flat", 2, 0.3), _row("fit", 1, 0.1), _row("fit", 2, 0.1), _row("lin", 1, 0.2,
                                                                                                      family="linear")]
    for r in rows:
        r["low_iter_share"] = {"flat": 0.9, "fit": 0.2, "lin": None}[r["cell_key"]]
    assert set(G.factor_effects(rows, "timing")["cells"]["cell_key"]) == {"flat", "fit", "lin"}
    res = G.factor_effects(rows, "timing", max_low_iter_share=0.5)
    assert set(res["cells"]["cell_key"]) == {"fit", "lin"}                  # a non-boosting family has no share: kept
    assert "low_iter_share" in res["excluded"].set_index("cell_key").loc["flat", "reason"]


def test_factor_effects_dedupes_on_cell_and_seed_keeping_the_last_row():
    rows = [_row("a", 1, 0.9), _row("a", 2, 0.2), _row("a", 1, 0.1)]          # seed 1 was rerun: the last row counts
    c = G.factor_effects(rows, "timing")["cells"].iloc[0]
    assert c["n_seeds"] == 2 and c["mean"] == pytest.approx(0.15)


def test_factor_effects_uses_the_median_p_value_and_keeps_the_per_seed_values():
    rows = [_row("a", 1, 0.1, p=0.01), _row("a", 2, 0.1, p=0.04), _row("a", 3, 0.1, p=0.90)]
    c = G.factor_effects(rows, "timing")["cells"].iloc[0]
    assert c["p_value"] == pytest.approx(0.04)                              # the mean would be 0.317
    assert list(c["p_values"]) == [0.01, 0.04, 0.90]


# ------------------------------------------------------------------ disk
def test_predictions_are_float32_zstd_and_the_metrics_see_exactly_those_values(signal_world, tmp_path):
    import pyarrow.parquet as pq
    out = tmp_path / "o"
    run([cell(signal_world[0], track="timing", target={"kind": "up", "horizon": 5})], out, signal_world,
        settings=_settings(control_seeds=(1,)))
    f = next((out / "predictions").glob("*.parquet"))
    pf = pq.ParquetFile(f)
    names = pf.schema_arrow.names
    types = {n: str(pf.schema_arrow.field(n).type) for n in names}
    assert all(types[n] == "float" for n in ("score", "ret", "vol"))        # pyarrow's name for float32
    assert pf.metadata.row_group(0).column(names.index("score")).compression == "ZSTD"
    df = pd.read_parquet(f)
    assert df["score"].dtype == np.float32 and df["ret"].dtype == np.float32


def test_the_disk_estimate_scales_and_is_in_the_right_range(signal_world, tmp_path):
    st = _settings()
    small = G.validate_cell(cell(signal_world[0], symbols=SYMS[:5], seeds=[1]))
    big = G.validate_cell(cell(signal_world[0], symbols=SYMS, seeds=[1, 2, 3]))
    e_small, e_big = G.estimate_disk([(small, 1)], st), G.estimate_disk([(big, s) for s in (1, 2, 3)], st)
    assert e_small["predictions"] > 0 and e_big["predictions"] == pytest.approx(e_small["predictions"] * 2 * 3, rel=0.01)
    assert e_big["total"] == e_big["predictions"] + e_big["results"] and e_big["rows_per_task"] > 0
    out = tmp_path / "o"
    run([cell(signal_world[0], seeds=[1, 2])], out, signal_world, settings=_settings(control_seeds=(1,)))
    actual = sum(f.stat().st_size for f in (out / "predictions").glob("*.parquet"))
    est = G.estimate_disk([(G.validate_cell(cell(signal_world[0])), s) for s in (1, 2)], st)["predictions"]
    assert 0.25 * est <= actual <= 4 * est, (actual, est)


def test_the_run_logs_the_disk_estimate_at_the_start(signal_world, tmp_path):
    msgs = []
    run([cell(signal_world[0])], tmp_path / "o", signal_world, progress=lambda m, pct=None: msgs.append(m),
        settings=_settings(control_seeds=(1,)))
    assert any(m.startswith("ml_grid: estimated disk") and "free" in m for m in msgs)


# ================================================================== follow-ups of the quality review
class FakePool:
    """A scripted stand-in for ProcessPoolExecutor. Seed ``POISON`` kills the pool: it fails itself and (with
    FAIL_NEIGHBOURS) every other pending future, and a broken pool refuses further submissions, as the real one does.
    Other tasks finish ``DELAY`` seconds after they are submitted."""
    instances = []
    POISON = 3
    FAIL_NEIGHBOURS = True
    DELAY = 0.05
    MANUAL = False              # True: pending futures complete exactly when the driver calls wait() (deterministic)

    def __init__(self, *a, **k):
        self.results = {}
        self.kw = k
        self.id = len(FakePool.instances)
        FakePool.instances.append(self)
        self.pending, self.submitted, self.broken, self.shutdowns = [], [], False, []

    def submit(self, fn, cfg, seed, key):
        import threading
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool
        if self.broken:
            raise BrokenProcessPool("the pool is broken")
        self.pending = [f for f in self.pending if not f.done()]
        self.submitted.append((seed, len(self.pending)))
        fu = Future()
        if seed == self.POISON:
            self.broken = True
            if self.FAIL_NEIGHBOURS:
                for f in self.pending:
                    f.set_exception(BrokenProcessPool("a worker died"))
            fu.set_exception(BrokenProcessPool("a worker died"))
        else:
            self.pending.append(fu)
            self.results[fu] = {"ok": seed}
            if not self.MANUAL:
                def done():
                    if not fu.done():
                        fu.set_result({"ok": seed})
                threading.Timer(self.DELAY, done).start()
        return fu

    def complete_pending(self):
        for f in self.pending:
            if not f.done():
                f.set_result(self.results[f])

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdowns.append((wait, cancel_futures))


@pytest.fixture
def fake_pool(monkeypatch):
    FakePool.instances = []
    FakePool.POISON, FakePool.FAIL_NEIGHBOURS, FakePool.DELAY, FakePool.MANUAL = 3, True, 0.05, False
    monkeypatch.setattr(G, "ProcessPoolExecutor", FakePool)
    return FakePool


@pytest.fixture
def manual_pool(fake_pool, monkeypatch):
    """The fake pool whose pending futures complete exactly when the driver waits: no timing races."""
    fake_pool.MANUAL = True
    real_wait = G.wait

    def wait(fs, timeout=None, return_when=None):
        fake_pool.instances[-1].complete_pending()
        return real_wait(fs, timeout=timeout, return_when=return_when)
    monkeypatch.setattr(G, "wait", wait)
    return fake_pool


def _fake_tasks(world, seeds):
    c = G.validate_cell(cell(world[0], seeds=list(seeds)))
    return [(c, s) for s in seeds]


def _drive(world, seeds, cpu_slots, finish, **over):
    summary = {}
    G._drive_pool(_fake_tasks(world, seeds), cpu_slots, (), lambda *a: None, finish, lambda: False, summary)
    return summary


def test_a_finish_failure_such_as_a_full_disk_propagates_and_is_not_read_as_a_broken_pool(signal_world, fake_pool):
    fake_pool.POISON, fake_pool.FAIL_NEIGHBOURS, fake_pool.DELAY = 1, False, 0.2

    def finish(r):
        if "ok" in r:
            raise OSError(28, "No space left on device")
    # seed 2 is still running when seed 1 breaks the pool: it is collected on the break path, its result is valid and
    # writing it fails -- that OSError must reach the caller, not turn the task into a "broken" retry
    with pytest.raises(OSError, match="No space left"):
        _drive(signal_world, [2, 1], 2, finish)
    assert fake_pool.instances[-1].shutdowns[-1] == (False, True)


def test_a_finish_failure_on_the_normal_path_propagates_too(signal_world, fake_pool):
    fake_pool.POISON = 99

    def finish(r):
        raise OSError(28, "No space left on device")
    with pytest.raises(OSError, match="No space left"):
        _drive(signal_world, [1, 2], 2, finish)


def test_probation_uses_a_small_window_and_only_tasks_charged_twice_run_alone(signal_world, manual_pool):
    fake_pool = manual_pool
    rows = []
    summary = _drive(signal_world, list(range(1, 9)), 8, rows.append)            # window 16, probation window 8 // 4 = 2
    ok = sorted(r["ok"] for r in rows if "ok" in r)
    err = [r for r in rows if "error" in r]
    assert ok == [1, 2, 4, 5, 6, 7, 8] and len(err) == 1 and err[0]["seed"] == 3
    assert summary["pool_rebuilds"] == 4 and len(fake_pool.instances) == 5
    first, probation, alone1, alone2, rest = fake_pool.instances
    assert [s for s, _ in first.submitted] == [1, 2, 3]                            # the whole window broke together
    assert max(p for _, p in probation.submitted) + 1 <= 2                         # never more than 2 in flight ...
    assert any(p == 1 for _, p in probation.submitted)                             # ... and really 2 at once (not 1)
    assert alone1.submitted == [(3, 0)] and alone2.submitted == [(3, 0)]           # charged twice: a singleton
    assert sorted(s for s, _ in rest.submitted) == [4, 5, 6, 7, 8]                 # back to the full window afterwards
    assert len({id(p.kw["initargs"][-1]) for p in fake_pool.instances}) == 5      # each pool has its own stagger counter


def test_a_submit_on_a_pool_that_just_broke_is_retried_not_fatal(signal_world, manual_pool):
    fake_pool = manual_pool
    rows = []
    _drive(signal_world, [1, 3, 2], 4, rows.append)                                # seed 2 is submitted after the break
    assert sorted(r["ok"] for r in rows if "ok" in r) == [1, 2]
    assert 2 not in [s for s, _ in fake_pool.instances[0].submitted]               # refused by the broken pool, put back


# ------------------------------------------------------------------ lock: atomic creation, patient reads
def test_the_lock_is_created_atomically_from_a_temp_file(tmp_path, monkeypatch):
    calls = []
    real = os.link

    def spy(src, dst, *a, **k):
        calls.append((Path(src).name, Path(dst).name, Path(src).read_text(encoding="utf-8")))   # complete when linked
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "link", spy)
    out = tmp_path / "o"
    out.mkdir()
    lock = G.acquire_lock(out)
    assert len(calls) == 1 and calls[0][1] == G.LOCK_NAME and json.loads(calls[0][2])["pid"] == os.getpid()
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert [p.name for p in out.iterdir()] == [G.LOCK_NAME]                          # the temp file is gone
    with pytest.raises(G.GridLockedError):
        G.acquire_lock(out)
    assert [p.name for p in out.iterdir()] == [G.LOCK_NAME]                          # the loser leaves no temp file either
    G.release_lock(lock)
    assert not lock.exists()


def test_the_lock_falls_back_when_the_filesystem_has_no_hard_links(tmp_path, monkeypatch):
    def nolink(*a, **k):
        raise OSError(1, "not supported")
    monkeypatch.setattr(os, "link", nolink)
    out = tmp_path / "o"
    out.mkdir()
    lock = G.acquire_lock(out)
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    G.release_lock(lock)


def test_an_unreadable_lock_is_given_a_moment_before_it_is_called_stale(tmp_path, monkeypatch):
    import socket
    import threading
    import time
    out = tmp_path / "o"
    out.mkdir()
    lock = out / G.LOCK_NAME
    lock.write_text("", encoding="utf-8")                                            # a holder that is mid-write
    monkeypatch.setattr(G, "LOCK_READ_TRIES", 8)
    monkeypatch.setattr(G, "LOCK_READ_WAIT", 0.05)
    live = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": "now"})
    threading.Timer(0.12, lambda: lock.write_text(live, encoding="utf-8")).start()
    with pytest.raises(G.GridLockedError, match="already running"):
        G.acquire_lock(out)
    time.sleep(0.05)
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()      # it was NOT taken over
    lock.write_text("", encoding="utf-8")                                            # an empty lock that never fills
    got = G.acquire_lock(out)
    assert json.loads(got.read_text(encoding="utf-8"))["pid"] == os.getpid()      # stale after the retries: taken over
    G.release_lock(got)


# ------------------------------------------------------------------ factor_effects: no_predictions is coverage 0
def _np_row(key, seed, **kw):
    r = _row(key, seed, None, fitted=0, status="no_predictions", **kw)
    r["primary"] = {"name": None, "value": None}
    return r


def test_a_no_predictions_seed_counts_as_coverage_zero():
    rows = [_row("half", 1, 0.2), _row("half", 2, 0.2), _np_row("half", 3),             # 2 of 3 seeds produced anything
            _row("ok", 1, 0.1), _row("ok", 2, 0.1),
            _np_row("none", 1), _np_row("none", 2)]                                     # no seed produced anything
    res = G.factor_effects(rows, "timing")
    assert list(res["cells"]["cell_key"]) == ["ok"]
    ex = res["excluded"].set_index("cell_key")
    assert ex.loc["half", "coverage"] == 0.0 and "coverage" in ex.loc["half", "reason"]
    assert ex.loc["none", "coverage"] == 0.0
    loose = G.factor_effects(rows, "timing", min_coverage=0.0)
    assert set(loose["cells"]["cell_key"]) == {"ok", "half"}                            # kept, mean over its ok seeds
    assert loose["cells"].set_index("cell_key").loc["half", "n_seeds"] == 2
    assert list(loose["excluded"]["cell_key"]) == ["none"]
    assert "no seed produced predictions" in loose["excluded"]["reason"].iloc[0]
    other = [_np_row("sel", 1, track="selection")]
    assert G.factor_effects(other, "timing")["excluded"].empty                          # another track is not this table's


# ------------------------------------------------------------------ resume check: per-symbol bar hashes are cached
def test_daily_bar_hashes_are_cached_per_symbol(signal_world, monkeypatch):
    G._bar_hash.cache_clear()
    reads = []
    real = G.F._read_daily
    monkeypatch.setattr(G.F, "_read_daily", lambda sym, root: (reads.append(sym), real(sym, root))[1])
    cache = signal_world[2]
    a = G.daily_fingerprint(cache, SYMS[:4], FIRST, LAST)
    assert sorted(reads) == sorted(SYMS[:4])
    b = G.daily_fingerprint(cache, SYMS[2:6], FIRST, LAST)                              # overlapping set: 2 new symbols only
    assert sorted(reads) == sorted(SYMS[:6]) and a != b
    n = len(reads)
    assert G.daily_fingerprint(cache, SYMS[:4], FIRST, LAST) == a and len(reads) == n   # repeated: no reads at all
    f = cache / "AlpacaHistoryOHLCVProvider" / f"{SYMS[0]}_1d.parquet"
    os.utime(f, (1_900_000_000, 1_900_000_000))                                         # a touched file is re-read ...
    assert G.daily_fingerprint(cache, SYMS[:4], FIRST, LAST) == a and len(reads) == n + 1   # ... and hashes the same
    G.daily_fingerprint(cache, SYMS[:4], FIRST, date(2019, 12, 31))                      # another window: a different key
    assert len(reads) == n + 1 + 4


def test_repeated_prechecks_do_not_reload_bars(signal_world, tmp_path, monkeypatch):
    out = tmp_path / "o"
    run([cell(signal_world[0])], out, signal_world, settings=_settings(control_seeds=(1,)))
    body = payload(signal_world, out_dir=str(out), settings=_settings(control_seeds=(1,)).to_dict())
    G.precheck_grid(body)                                                               # warms the hashes
    reads = []
    real = G.F._read_daily
    monkeypatch.setattr(G.F, "_read_daily", lambda sym, root: (reads.append(sym), real(sym, root))[1])
    assert G.precheck_grid(body) == 1 and G.precheck_grid(body) == 1
    assert reads == []


# ------------------------------------------------------------------ staggered first panel load
def test_workers_stagger_their_first_panel_load_by_their_index(signal_world, tmp_path, monkeypatch):
    import multiprocessing
    slept = []
    monkeypatch.setattr(G.time, "sleep", lambda s: slept.append(s))
    counter = multiprocessing.get_context("spawn").Value("i", 0)
    c1 = G.validate_cell(cell(signal_world[0], symbols=SYMS[:6]))
    c2 = G.validate_cell(cell(signal_world[0], symbols=SYMS[:7]))
    try:
        for i in range(3):
            G._WORKER.clear()
            G._PANELS.clear()
            G._init_worker(_settings().to_dict(), str(signal_world[2]), str(tmp_path), 1, counter)
            assert G._WORKER["index"] == i and slept == []                       # the initializer itself waits for nothing
            G._panel_for(c1, str(signal_world[2]))
            G._panel_for(c2, str(signal_world[2]))                               # only the FIRST load of a worker waits
            assert slept == ([] if i == 0 else [i * G.PANEL_STAGGER_SECONDS])
            slept.clear()
    finally:
        G._WORKER["limits"].restore_original_limits()
    G._WORKER.clear()
    G._init_worker(_settings().to_dict(), str(signal_world[2]), str(tmp_path), 1)        # inline / no counter: no wait
    G._PANELS.clear()
    G._panel_for(c1, str(signal_world[2]))
    assert slept == [] and G._WORKER["index"] == 0
    G._WORKER["limits"].restore_original_limits()


# ------------------------------------------------------------------ gate / report / dashboard hooks
def fake_task(cfg, seed, key):
    """A module-level per-task function that fabricates a finished row (no fitting)."""
    name = G.PRIMARY[cfg["track"]]
    c = G.validate_cell(cfg)                           # a resumable row: the real identity and a predictions file
    syms = tuple(sorted(G.resolve_symbols(c.symbols)))
    first, last, _ = G.store_span(c.store, syms)
    ident = G.identity_of(c, G._WORKER["settings"], G.daily_fingerprint(G._WORKER["cache_root"], syms, first, last))
    pred = Path(G._WORKER["out_dir"]) / "predictions" / f"{key}_{seed}.parquet"
    pred.parent.mkdir(parents=True, exist_ok=True)
    pred.write_bytes(b"")
    return {"cell_key": key, "seed": seed, "track": cfg["track"], "label": G._label_of(cfg), "status": "ok",
            "n_rows": 10, "skipped_blocks": [], "timings": {"total": 0.1}, "config": {k: v for k, v in cfg.items()
                                                                                       if k != "seeds"},
            "n_blocks": 31, "n_blocks_fitted": 31, "seeds_run": 1, "low_iter_share": 0.0,
            "primary": {"name": name, "value": 0.01 * seed},
            "regimes": {name: {"all": {"mean": 0.01, "t": 1.0, "n": 10}}},
            "controls": {"p_value": 0.5, "primary": "shuffle_symbols", "shuffle_symbols": {"mean": 0.0}},
            "identity": ident, "versions": {"code": "c0de"}}


def _horizon_cells(world, n):
    return [cell(world[0], target={"kind": "rel_return", "horizon": h}) for h in (1, 3, 5, 10, 20)[:n]]


@pytest.mark.parametrize("n_cells", [5, 4])
def test_the_dashboard_hook_runs_at_the_start_after_every_finished_row_and_at_the_end(signal_world, tmp_path,
                                                                                     monkeypatch, n_cells):
    calls = []
    monkeypatch.setattr(G, "_refresh_outputs", lambda out, summary, final, finished: calls.append((finished, final)))
    run(_horizon_cells(signal_world, n_cells), tmp_path / "o", signal_world, task_fn=fake_task)
    # the throttle lives in ml_grid_report.refresh (an O(1) check), so the runner asks after EVERY row; the last row
    # is covered by the final rebuild
    assert calls == [(i, False) for i in range(n_cells)] + [(n_cells, True)]


def test_errors_count_as_finished_rows_for_the_hook(signal_world, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(G, "_refresh_outputs", lambda out, summary, final, finished: calls.append((finished, final)))

    def sometimes_error(cfg, seed, key):
        if cfg["target"]["horizon"] in (3, 10):
            return G._error_record(key, seed, "boom", "l")
        return fake_task(cfg, seed, key)
    run(_horizon_cells(signal_world, 4), tmp_path / "o", signal_world, task_fn=sometimes_error)
    assert calls == [(0, False), (1, False), (2, False), (3, False), (4, True)]


def test_only_the_start_and_the_end_force_a_build(signal_world, tmp_path, monkeypatch):
    import app.services.ml_grid_report as RP
    seen = []
    monkeypatch.setattr(RP, "refresh", lambda out, summary, final, force=False: seen.append((final, force)))
    run(_horizon_cells(signal_world, 3), tmp_path / "o", signal_world, task_fn=fake_task)
    assert seen == [(False, True), (False, False), (False, False), (True, True)]


def test_live_builds_during_a_run_are_throttled_by_the_report_module(signal_world, tmp_path, monkeypatch):
    import app.services.ml_grid_report as RP
    built = []
    real = RP.render_dashboard
    monkeypatch.setattr(RP, "render_dashboard", lambda st: (built.append(1), real(st))[1])
    monkeypatch.setattr(RP, "_now", lambda: 1000.0)                          # the clock never advances
    run(_horizon_cells(signal_world, 5), tmp_path / "o", signal_world, task_fn=fake_task)
    assert len(built) == 2                                                   # the start and the end, nothing between


def test_a_finished_run_leaves_report_dashboard_and_run_summary(signal_world, tmp_path):
    out = tmp_path / "o"
    res = run(_horizon_cells(signal_world, 3), out, signal_world, task_fn=fake_task)
    assert res["done"] == 3
    assert (out / "report.md").exists() and (out / "dashboard.html").exists()
    saved = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert saved["done"] == 3 and saved["errors"] == 0 and saved["pool_rebuilds"] == 0 and saved["planned_tasks"] == 3
    md = (out / "report.md").read_text(encoding="utf-8")
    assert re.search(r"rows done\D+3 of 3", md) and "pool_rebuilds" in md
    assert not list(out.glob("*.tmp"))


def test_a_resumed_run_with_nothing_left_still_rebuilds_the_outputs(signal_world, tmp_path):
    out = tmp_path / "o"
    run(_horizon_cells(signal_world, 2), out, signal_world, task_fn=fake_task)
    (out / "report.md").unlink()
    (out / "dashboard.html").unlink()
    res = run(_horizon_cells(signal_world, 2), out, signal_world, task_fn=fake_task)
    assert res["done"] == 0 and (out / "report.md").exists() and (out / "dashboard.html").exists()


def test_a_failing_dashboard_rebuild_never_stops_the_grid(signal_world, tmp_path, monkeypatch):
    import app.services.ml_grid_report as RP

    def boom(*a, **k):
        raise OSError("the dashboard file is open in another program")
    monkeypatch.setattr(RP, "refresh", boom)
    res = run(_horizon_cells(signal_world, 2), tmp_path / "o", signal_world, task_fn=fake_task)
    assert res["done"] == 2 and len(G.read_results(tmp_path / "o" / "results.jsonl")) == 2


def test_a_cancelled_run_still_writes_the_final_outputs(signal_world, tmp_path):
    out = tmp_path / "o"
    n = {"i": 0}

    def cancel_after_one():
        n["i"] += 1
        return n["i"] > 1
    res = run(_horizon_cells(signal_world, 3), out, signal_world, task_fn=fake_task, cancel_check=cancel_after_one)
    assert res["cancelled"] and res["done"] == 1
    assert (out / "report.md").exists() and (out / "dashboard.html").exists()
    assert json.loads((out / "run_summary.json").read_text(encoding="utf-8"))["cancelled"] is True


def test_report_only_rebuilds_from_an_existing_dir_and_runs_nothing(signal_world, tmp_path, monkeypatch, capsys):
    import ba2test_launcher as L
    import app.services.ml_grid as grid
    out = tmp_path / "o"
    run(_horizon_cells(signal_world, 2), out, signal_world, task_fn=fake_task)
    for name in ("report.md", "dashboard.html"):
        (out / name).unlink()
    monkeypatch.setattr(grid, "handle_ml_grid", lambda *a, **k: pytest.fail("report-only must not run the grid"))
    monkeypatch.setattr(grid, "run_grid", lambda *a, **k: pytest.fail("report-only must not run the grid"))
    assert L.main(["ml-grid", "--report-only", str(out)]) == 0
    assert (out / "report.md").exists() and (out / "dashboard.html").exists()
    printed = capsys.readouterr().out
    assert "dashboard.html" in printed and "report.md" in printed


def test_report_only_refuses_a_missing_dir_and_the_other_flags_stay_required_without_it(tmp_path, capsys):
    import ba2test_launcher as L
    assert L.main(["ml-grid", "--report-only", str(tmp_path / "nope")]) != 0
    assert "results.jsonl" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        L.main(["ml-grid", "--out", str(tmp_path)])                      # no --report-only, no --config / --cpu-slots


# ------------------------------------------------------------------ per-regime controls in the rows
def _by_regime_checks(row, track):
    name = G.PRIMARY[track]
    ctl = row["controls"]
    assert ctl["primary"] in ctl["p_values"]
    br = ctl["by_regime"]                                       # the stricter control, regime by regime
    seen = [r for r in G.W.REGIMES if r in row["regimes"][name]]
    assert set(br) == set(seen) and seen
    for reg, e in br.items():
        assert e["control"] in ctl["p_values"]
        model = row["regimes"][name][reg]["mean"]
        assert e["model"] == (pytest.approx(model) if model is not None else None)       # same number as the row's
        for k in ("null_mean", "null_std", "p_value", "rank"):
            assert k in e
        if e["null_mean"] is not None:
            rows_ = ctl[e["control"]]["by_regime"][reg]
            assert e["null_mean"] == pytest.approx(rows_["mean"]) and e["p_value"] == rows_["p_value"]
    for n in ctl["p_values"]:                                   # every gating control keeps its per-regime summary
        assert set(ctl[n]["by_regime"]) == set(seen)
        for e in ctl[n]["by_regime"].values():
            assert set(e) >= {"mean", "std", "p_value", "rank", "model"}


def test_rows_hold_per_regime_control_nulls_that_agree_with_the_rows_regime_values(signal_world, tmp_path):
    out = tmp_path / "o"
    st = _settings(control_seeds=(1, 2, 3))
    cells = [cell(signal_world[0], seeds=[1]),
             cell(signal_world[0], track="timing", target={"kind": "up", "horizon": 5}, seeds=[1])]
    run(cells, out, signal_world, settings=st)
    rows = {r["track"]: r for r in G.read_results(out / "results.jsonl")}
    _by_regime_checks(rows["selection"], "selection")
    _by_regime_checks(rows["timing"], "timing")
    assert set(rows["timing"]["controls"]["p_values"]) == {"shuffle_time", "shuffle_time_within_block"}


def test_the_per_regime_entry_keeps_every_controls_null_and_shows_the_larger_p_control():
    ctl = {"shuffle_time": {"mean": -0.01, "by_regime": {
               "2019": {"mean": -0.02, "std": 0.01, "p_value": 0.30, "rank": 7, "model": 0.01},
               "2021": {"mean": -0.02, "std": 0.01, "p_value": 0.05, "rank": 1, "model": 0.01}}},
           "shuffle_time_within_block": {"mean": -0.04, "by_regime": {
               "2019": {"mean": -0.05, "std": 0.02, "p_value": 0.10, "rank": 2, "model": 0.01},
               "2021": {"mean": 0.00, "std": 0.02, "p_value": 0.20, "rank": 4, "model": 0.01}}}}
    names = ("shuffle_time", "shuffle_time_within_block")
    strict = G._strict_by_regime(ctl, names)
    # the displayed p / std / rank are the larger-p control's; the gate takes the highest of null_means itself
    assert strict["2019"]["control"] == "shuffle_time" and strict["2019"]["null_mean"] == -0.02
    assert strict["2019"]["null_means"] == {"shuffle_time": -0.02, "shuffle_time_within_block": -0.05}
    assert strict["2021"]["control"] == "shuffle_time_within_block" and strict["2021"]["p_value"] == 0.20
    assert strict["2021"]["null_means"] == {"shuffle_time": -0.02, "shuffle_time_within_block": 0.00}
    assert strict["2021"]["null_std"] == 0.02 and strict["2021"]["model"] == 0.01
    ctl["shuffle_time"]["by_regime"]["2019"]["p_value"] = None                   # a non-finite null: no p, last control
    again = G._strict_by_regime(ctl, names)
    assert again["2019"]["control"] == "shuffle_time_within_block" and again["2019"]["p_value"] is None


def test_rows_store_the_per_control_null_means_per_regime(signal_world, tmp_path):
    out = tmp_path / "o"
    run([cell(signal_world[0], track="timing", target={"kind": "up", "horizon": 5}, seeds=[1])], out, signal_world,
        settings=_settings(control_seeds=(1, 2, 3)))
    row = G.read_results(out / "results.jsonl")[0]
    for reg, e in row["controls"]["by_regime"].items():
        assert set(e["null_means"]) == {"shuffle_time", "shuffle_time_within_block"}
        for n, v in e["null_means"].items():
            assert v == row["controls"][n]["by_regime"][reg]["mean"]


# ------------------------------------------------------------------ review fixes: finally, rate limit, summary
def test_the_final_outputs_are_written_even_when_the_run_dies(signal_world, tmp_path):
    out = tmp_path / "o"

    def dies_on_third(cfg, seed, key):
        if cfg["target"]["horizon"] == 5:
            raise RuntimeError("the master hit something unexpected")
        return fake_task(cfg, seed, key)
    with pytest.raises(RuntimeError, match="unexpected"):
        run(_horizon_cells(signal_world, 3), out, signal_world, task_fn=dies_on_third)
    assert (out / "report.md").exists() and (out / "dashboard.html").exists()
    assert len(G.read_results(out / "results.jsonl")) == 2


def test_a_failing_rebuild_is_logged_in_full_once_and_then_only_counted(signal_world, tmp_path, monkeypatch, caplog):
    import app.services.ml_grid_report as RP

    def boom(*a, **k):
        raise OSError("the dashboard file is open in another program")
    monkeypatch.setattr(RP, "refresh", boom)
    with caplog.at_level("WARNING"):
        res = run(_horizon_cells(signal_world, 4), tmp_path / "o", signal_world, task_fn=fake_task)
    assert res["done"] == 4
    full = [r for r in caplog.records if "could not rebuild" in r.getMessage()]
    assert len(full) == 1 and full[0].exc_info                                  # the first failure, with its traceback
    counted = [r for r in caplog.records if "rebuilds failed" in r.getMessage()]
    assert len(counted) == 1 and "5 " in counted[0].getMessage()                 # start + 3 cadence + final


def test_the_run_summary_records_the_planned_tasks_and_cumulative_pool_rebuilds(signal_world, tmp_path):
    out = tmp_path / "o"
    run(_horizon_cells(signal_world, 3), out, signal_world, task_fn=fake_task)
    saved = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    cells = [G.validate_cell(c) for c in _horizon_cells(signal_world, 3)]
    keys = sorted(f"{c.key}:1" for c in cells)
    assert saved["planned_tasks"] == 3 and saved["planned_count"] == 3 and "planned_keys" not in saved
    assert saved["planned_hash"] == __import__("hashlib").sha1("\n".join(keys).encode("utf-8")).hexdigest()
    assert (out / "planned_keys.txt").read_text(encoding="utf-8").split() == keys
    assert saved["pool_rebuilds"] == 0 and saved["pool_rebuilds_total"] == 0
    saved["pool_rebuilds_total"] = 4                                          # an earlier run had rebuilt the pool
    (out / "run_summary.json").write_text(json.dumps(saved), encoding="utf-8")
    run(_horizon_cells(signal_world, 3), out, signal_world, task_fn=fake_task)
    again = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert again["pool_rebuilds"] == 0 and again["pool_rebuilds_total"] == 4


def test_report_only_returns_before_the_grid_runner_is_imported():
    import inspect
    import ba2test_launcher as L
    src = inspect.getsource(L._cmd_ml_grid)
    assert src.index("report_only") < src.index("from app.services.ml_grid import")


# ------------------------------------------------------------------ --eval-start / --eval-end
def _cli_payload(monkeypatch, tmp_path, *extra):
    import ba2test_launcher as L
    import app.services.ml_grid as grid
    seen = {}

    def fake(task_id, payload):
        seen.update(payload)
        return {"done": 0, "errors": 0, "skipped_existing": 0, "cancelled": False, "out_dir": str(tmp_path)}
    monkeypatch.setattr(grid, "handle_ml_grid", fake)
    code = L.main(["ml-grid", "--config", "c.json", "--out", str(tmp_path), "--cpu-slots", "1", *extra])
    return code, seen


def test_the_cli_passes_the_eval_window_as_settings(monkeypatch, tmp_path):
    _, seen = _cli_payload(monkeypatch, tmp_path, "--eval-start", "2025-01-01", "--eval-end", "2025-10-12")
    assert seen["settings"] == {"eval_start": "2025-01-01", "eval_end": "2025-10-12"}
    G.GridSettings.from_dict({**seen["settings"], "cores": 2})                 # and the runner accepts it
    _, seen = _cli_payload(monkeypatch, tmp_path, "--eval-start", "2024-01-01")
    assert seen["settings"] == {"eval_start": "2024-01-01"}
    _, seen = _cli_payload(monkeypatch, tmp_path, "--eval-end", "2025-12-31")
    assert seen["settings"] == {"eval_end": "2025-12-31"}
    _, seen = _cli_payload(monkeypatch, tmp_path)
    assert seen["settings"] is None


@pytest.mark.parametrize("args, msg", [
    (("--eval-start", "2025/01/01"), "eval-start"),
    (("--eval-start", "not-a-date"), "eval-start"),
    (("--eval-end", "2025-13-40"), "eval-end"),
    (("--eval-start", "2015-12-31"), "outside"),
    (("--eval-start", str(G.DATA_START)), "outside"),                          # the window must start after the data does
    (("--eval-end", "2026-10-13"), "outside"),
    (("--eval-end", "2015-01-01"), "outside"),
    (("--eval-start", "2025-06-01", "--eval-end", "2025-06-01"), "before"),
    (("--eval-start", "2025-06-01", "--eval-end", "2025-01-01"), "before"),
    (("--eval-start", "2026-10-12"), "before"),                                 # the default end is 2026-10-12
])
def test_the_cli_refuses_a_bad_eval_window(monkeypatch, tmp_path, capsys, args, msg):
    import ba2test_launcher as L
    import app.services.ml_grid as grid
    monkeypatch.setattr(grid, "handle_ml_grid", lambda *a, **k: pytest.fail("must refuse before running"))
    with pytest.raises(SystemExit) as e:
        L.main(["ml-grid", "--config", "c.json", "--out", str(tmp_path), "--cpu-slots", "1", *args])
    assert e.value.code != 0
    text = str(e.value.code) + capsys.readouterr().err
    assert msg in text


def test_the_eval_bounds_are_inclusive_at_the_limits(monkeypatch, tmp_path):
    _, seen = _cli_payload(monkeypatch, tmp_path, "--eval-start", "2016-01-05", "--eval-end", str(G.EVAL_END))
    assert seen["settings"] == {"eval_start": "2016-01-05", "eval_end": str(G.EVAL_END)}
