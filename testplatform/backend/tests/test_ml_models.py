"""Model adapters (ml_models): every supported (family, task) learns a planted signal, is seeded, and the
unsupported pairs are refused."""
import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score

from app.services import ml_models as MM

FEATS = [f"f{i}" for i in range(6)] + ["t_ret_20"]
SESSION_SIZE = 40


def _signal(X: pd.DataFrame) -> np.ndarray:
    return X["f0"].to_numpy() + 0.6 * X["f1"].to_numpy() + 0.5 * X["t_ret_20"].to_numpy()


def _make(n: int, seed: int, nan_share: float = 0.05):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, len(FEATS))), columns=FEATS)
    sig = _signal(X)
    # NaN in two of the signal-bearing / noise columns (the signal itself was computed before)
    X.loc[rng.random(n) < nan_share, "f3"] = np.nan
    X.loc[rng.random(n) < nan_share, "f1"] = np.nan
    return X, sig, rng


def _data(task: str, seed: int = 0):
    """(Xtr, ytr, Xva, yva, Xte, yte_eval, gtr, gva) with a signal the model should find."""
    sizes = (2400, 800, 1200)
    rng = np.random.default_rng(100 + seed)
    parts = []
    for i, n in enumerate(sizes):
        X, sig, r = _make(n, 1000 * seed + i)
        noise = r.normal(size=n)
        latent = sig + noise
        if task == "binary":
            y = (latent > 0).astype(int)
        elif task == "multiclass":
            y = np.digitize(latent, np.quantile(latent, [1 / 3, 2 / 3]))
        elif task in ("regression", "quantile"):
            y = latent
        else:                                                          # rank: quintile of the latent in the session
            y = np.zeros(n, dtype=int)
            for s in range(0, n, SESSION_SIZE):
                sl = slice(s, s + SESSION_SIZE)
                y[sl] = np.digitize(latent[sl], np.quantile(latent[sl], [0.2, 0.4, 0.6, 0.8]))
        parts.append((X, y, latent))
    gtr = np.full(sizes[0] // SESSION_SIZE, SESSION_SIZE)
    gva = np.full(sizes[1] // SESSION_SIZE, SESSION_SIZE)
    return parts, gtr, gva


def _fit_predict(family, task, seed=1, **kw):
    parts, gtr, gva = _data(task)
    (Xtr, ytr, _), (Xva, yva, _), (Xte, yte, lat) = parts
    m = MM.make_adapter(family, n_jobs=2, **kw)
    m.fit(Xtr, ytr, Xva, yva, task, seed, groups_train=gtr, groups_val=gva)
    return m, m.predict(Xte), yte, lat


CASES = [(f, t) for f, ts in MM.SUPPORTED_TASKS.items() if f != "naive" for t in ts]


@pytest.mark.parametrize("family,task", CASES)
def test_family_task_beats_chance(family, task):
    m, p, y, lat = _fit_predict(family, task)
    if task == "binary":
        assert p.shape == (len(y), 2) and np.allclose(p.sum(1), 1)
        assert roc_auc_score(y, p[:, 1]) > 0.6
    elif task == "multiclass":
        assert p.shape == (len(y), 3) and np.allclose(p.sum(1), 1)
        majority = np.bincount(y).max() / len(y)
        assert (p.argmax(1) == y).mean() > majority + 0.03
    elif task == "quantile":
        assert p.shape == (len(y), 9)
        assert spearmanr(p[:, 4], lat).correlation > 0.2
    else:
        assert p.shape == (len(y),)
        assert spearmanr(p, lat).correlation > 0.2


@pytest.mark.parametrize("family,task", [c for c in CASES if c[0] != "linear"])
def test_same_seed_identical_different_seed_differs(family, task):
    _, a, _, _ = _fit_predict(family, task, seed=3)
    _, b, _, _ = _fit_predict(family, task, seed=3)
    _, c, _, _ = _fit_predict(family, task, seed=4)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_linear_is_seed_free_and_deterministic():
    _, a, _, _ = _fit_predict("linear", "regression", seed=1)
    _, b, _, _ = _fit_predict("linear", "regression", seed=2)
    assert np.array_equal(a, b)


def test_quantile_outputs_are_monotone_per_row_and_sorted_even_if_models_cross():
    m, p, y, _ = _fit_predict("lgbm", "quantile")
    assert (np.diff(p, axis=1) >= 0).all()
    # force the raw per-alpha models to cross: the output must still be sorted
    m._models = m._models[::-1]
    parts, _, _ = _data("quantile")
    q = m.predict(parts[2][0])
    assert (np.diff(q, axis=1) >= 0).all()


def test_quantile_levels_roughly_calibrated():
    m, p, y, _ = _fit_predict("lgbm", "quantile")
    assert abs((y <= p[:, 0]).mean() - 0.1) < 0.06
    assert abs((y <= p[:, 8]).mean() - 0.9) < 0.06


@pytest.mark.parametrize("family,task", [("xgb", "rank"), ("xgb", "quantile"), ("catboost", "rank"),
                                         ("catboost", "quantile"), ("linear", "rank"), ("linear", "quantile"),
                                         ("naive", "multiclass"), ("naive", "quantile")])
def test_unsupported_pairs_raise(family, task):
    parts, gtr, gva = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    with pytest.raises(MM.UnsupportedTaskError):
        MM.make_adapter(family).fit(Xtr, ytr, Xva, yva, task, 1, gtr, gva)


def test_unknown_task_family_and_params_raise():
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    with pytest.raises(MM.UnsupportedTaskError):
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "classify", 1)
    with pytest.raises(ValueError, match="unknown model family"):
        MM.make_adapter("forest")
    with pytest.raises(ValueError, match="unknown params"):
        MM.make_adapter("lgbm", params={"num_leafs": 8})


def test_params_override_defaults_and_defaults_are_declared():
    d = MM.DEFAULTS["lgbm"]
    assert d["n_estimators"] == 2000 and d["early_stopping_rounds"] == 50
    m = MM.make_adapter("lgbm", params={"learning_rate": 0.2, "num_leaves": 7})
    assert m.params["learning_rate"] == 0.2 and m.params["num_leaves"] == 7
    assert m.params["n_estimators"] == 2000


def test_lgbm_early_stops_well_before_the_cap_and_uses_n_jobs():
    m, _, _, _ = _fit_predict("lgbm", "regression")
    assert 0 < m.n_iter < 2000 and m.n_jobs == 2


def test_label_validation_refuses_loudly():
    parts, gtr, gva = _data("binary")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    with pytest.raises(ValueError, match="0 / 1"):
        MM.make_adapter("lgbm").fit(Xtr, ytr * 2, Xva, yva * 2, "binary", 1)
    with pytest.raises(ValueError, match="NaN"):
        bad = ytr.astype(float)
        bad[0] = np.nan
        MM.make_adapter("lgbm").fit(Xtr, bad, Xva, yva, "binary", 1)
    with pytest.raises(ValueError, match="infinite"):
        X2 = Xtr.copy()
        X2.iloc[0, 0] = np.inf
        MM.make_adapter("lgbm").fit(X2, ytr, Xva, yva, "binary", 1)
    with pytest.raises(ValueError, match="groups"):
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "rank", 1)
    with pytest.raises(ValueError, match="empty"):
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xva.iloc[:0], yva[:0], "binary", 1)


def test_rank_groups_must_sum_to_rows():
    parts, gtr, gva = _data("rank")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    with pytest.raises(ValueError, match="sum"):
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "rank", 1, gtr[:-1], gva)


def test_predict_checks_columns_and_order():
    m, _, _, _ = _fit_predict("lgbm", "regression")
    parts, _, _ = _data("regression")
    X = parts[2][0]
    with pytest.raises(ValueError, match="columns"):
        m.predict(X.iloc[:, :-1])
    with pytest.raises(ValueError, match="order"):
        m.predict(X[FEATS[::-1]])
    with pytest.raises(RuntimeError, match="before fit"):
        MM.make_adapter("lgbm").predict(X)


# ------------------------------------------------------------------ linear
def test_linear_scaler_is_fit_on_train_only_and_nan_becomes_zero():
    rng = np.random.default_rng(0)
    Xtr = pd.DataFrame({"a": rng.normal(10, 2, 500), "b": rng.normal(0, 1, 500)})
    ytr = Xtr["b"].to_numpy() * 2 + rng.normal(0, 0.1, 500)
    Xva = Xtr.iloc[:50]
    m = MM.make_adapter("linear")
    m.fit(Xtr, ytr, Xva, ytr[:50], "regression", 0)
    assert abs(m._center[0] - np.median(Xtr["a"])) < 1e-12
    # predicting with a wildly different block must not move the scaler
    c0 = m._center.copy()
    Xte = pd.DataFrame({"a": [1e6, np.nan], "b": [np.nan, 1.0]})
    p = m.predict(Xte)
    assert np.array_equal(m._center, c0) and np.isfinite(p).all()


def test_linear_missing_indicators_only_when_asked_and_only_for_train_nan_columns():
    rng = np.random.default_rng(1)
    n = 800
    x = rng.normal(size=n)
    miss = rng.random(n) < 0.3
    xa = np.where(miss, np.nan, x)
    y = np.where(miss, 5.0, 0.0) + rng.normal(0, 0.1, n)      # missingness itself carries the signal
    X = pd.DataFrame({"a": xa, "b": rng.normal(size=n)})
    plain = MM.make_adapter("linear").fit(X, y, X.iloc[:10], y[:10], "regression", 0)
    ind = MM.make_adapter("linear", missing_indicators=True).fit(X, y, X.iloc[:10], y[:10], "regression", 0)
    assert plain._design(X.to_numpy()).shape[1] == 2
    assert ind._design(X.to_numpy()).shape[1] == 3
    assert np.corrcoef(plain.predict(X), y)[0, 1] < 0.2
    assert np.corrcoef(ind.predict(X), y)[0, 1] > 0.95


def test_linear_constant_and_all_nan_columns_do_not_break():
    X = pd.DataFrame({"const": np.ones(100), "allnan": np.full(100, np.nan),
                      "x": np.random.default_rng(0).normal(size=100)})
    y = X["x"].to_numpy()
    m = MM.make_adapter("linear").fit(X, y, X.iloc[:5], y[:5], "regression", 0)
    assert np.isfinite(m.predict(X)).all()


# ------------------------------------------------------------------ naive
def test_naive_returns_the_named_column_and_needs_names():
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, _, _) = parts
    m = MM.make_adapter("naive").fit(Xtr, ytr, Xva, yva, "regression", 0)
    assert np.array_equal(m.predict(Xte), Xte["t_ret_20"].to_numpy())
    m2 = MM.make_adapter("naive", params={"column": "f2"}).fit(Xtr, ytr, Xva, yva, "regression", 0)
    assert np.array_equal(m2.predict(Xte.to_numpy()), Xte["f2"].to_numpy())
    # an ndarray works when the names are given at fit
    m3 = MM.make_adapter("naive").fit(Xtr.to_numpy(), ytr, Xva.to_numpy(), yva, "regression", 0, feature_names=FEATS)
    assert np.array_equal(m3.predict(Xte.to_numpy()), Xte["t_ret_20"].to_numpy())
    with pytest.raises(ValueError, match="feature names"):
        MM.make_adapter("naive").fit(Xtr.to_numpy(), ytr, Xva.to_numpy(), yva, "regression", 0)
    with pytest.raises(ValueError, match="not among"):
        MM.make_adapter("naive", params={"column": "nope"}).fit(Xtr, ytr, Xva, yva, "regression", 0)


def test_naive_keeps_nan_where_the_feature_is_missing_and_beats_chance():
    parts, _, _ = _data("binary")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, yte, _) = parts
    Xte = Xte.copy()
    Xte.loc[Xte.index[:3], "t_ret_20"] = np.nan
    m = MM.make_adapter("naive").fit(Xtr, ytr, Xva, yva, "binary", 0)
    p = m.predict(Xte)
    assert np.isnan(p[:3]).all()
    ok = ~np.isnan(p)
    assert roc_auc_score(yte[ok], p[ok]) > 0.55


# ------------------------------------------------------------------ review fixes
@pytest.mark.parametrize("family,task", CASES)
def test_score_is_one_dimensional_and_follows_the_documented_rule(family, task):
    m, p, y, _ = _fit_predict(family, task)
    parts, _, _ = _data(task)
    Xte = parts[2][0]
    s = m.score(Xte)
    assert s.shape == (len(y),)
    if task == "binary":
        assert np.array_equal(s, p[:, 1]) and m.score_kind == "probability"
    elif task == "multiclass":
        assert np.array_equal(s, p[:, 2] - p[:, 0]) and m.score_kind == "probability_spread"
        assert spearmanr(s, parts[2][2]).correlation > 0.2
    elif task == "quantile":
        assert np.array_equal(s, p[:, 4]) and m.score_kind == "median"
    else:
        assert np.array_equal(s, p) and m.score_kind == ("rank_score" if task == "rank" else "point_forecast")


def test_naive_score_is_its_column_and_has_kind_feature():
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, _, _) = parts
    m = MM.make_adapter("naive").fit(Xtr, ytr, Xva, yva, "regression", 0)
    assert np.array_equal(m.score(Xte), Xte["t_ret_20"].to_numpy()) and m.score_kind == "feature"


def test_naive_needs_no_labels_validation_block_or_groups():
    parts, _, _ = _data("regression")
    (Xtr, _, _), _, (Xte, _, _) = parts
    for task in ("regression", "binary", "rank"):
        m = MM.make_adapter("naive").fit(Xtr, None, Xtr.iloc[:0], None, task, 0)
        assert np.array_equal(m.predict(Xte), Xte["t_ret_20"].to_numpy())
    m = MM.make_adapter("naive").fit(Xtr, None, None, None, "regression", 0)
    assert m.n_features == len(FEATS)


def test_failed_refit_leaves_the_adapter_unfitted_and_a_refit_resets_state():
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, _, _) = parts
    m = MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "regression", 1)
    assert m.task == "regression" and m.n_iter is not None
    bad = ytr.copy()
    bad[0] = np.nan
    with pytest.raises(ValueError):
        m.fit(Xtr, bad, Xva, yva, "regression", 2)
    assert m.task is None and m.n_iter is None and m.feature_names is None
    with pytest.raises(RuntimeError, match="before fit"):
        m.predict(Xte)
    m.fit(Xtr, ytr, Xva, yva, "regression", 1)
    with pytest.raises(ValueError):
        m.fit(Xtr, ytr, Xva.iloc[:, :3], yva, "regression", 3)         # column mismatch
    assert m.task is None
    m2 = MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "regression", 1)
    m2.fit(Xtr, (ytr > 0).astype(int), Xva, (yva > 0).astype(int), "binary", 5)
    assert m2.task == "binary" and m2.seed == 5 and m2.n_classes == 2


def test_state_is_assigned_only_after_the_inner_fit_succeeds(monkeypatch):
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    m = MM.make_adapter("lgbm")

    def boom(*a, **k):
        raise RuntimeError("library failure")
    monkeypatch.setattr(m, "_fit", boom)
    with pytest.raises(RuntimeError, match="library failure"):
        m.fit(Xtr, ytr, Xva, yva, "regression", 1)
    assert m.task is None and m.seed is None and m.n_features is None


@pytest.mark.parametrize("val", ["empty_frame", "none"])
def test_linear_works_with_an_empty_or_missing_validation_set(val):
    parts, _, _ = _data("binary")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, yte, _) = parts
    Xv = Xtr.iloc[:0] if val == "empty_frame" else None
    yv = ytr[:0] if val == "empty_frame" else None
    m = MM.make_adapter("linear").fit(Xtr, ytr, Xv, yv, "binary", 0)
    assert roc_auc_score(yte, m.score(Xte)) > 0.6
    m2 = MM.make_adapter("linear").fit(Xtr, ytr, np.empty((0, Xtr.shape[1])), np.empty(0), "binary", 0)
    assert np.array_equal(m.predict(Xte), m2.predict(Xte))
    with pytest.raises(ValueError, match="empty"):                    # a tree still needs its validation block
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xtr.iloc[:0], ytr[:0], "binary", 0)


def test_linear_clips_scaled_features_at_five():
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"a": rng.normal(size=400), "b": rng.normal(size=400)})
    y = X["a"].to_numpy()
    m = MM.make_adapter("linear").fit(X, y, X.iloc[:5], y[:5], "regression", 0)
    wild = pd.DataFrame({"a": [1e9, -1e9, 0.0], "b": [np.nan, 3.0, -1e12]})
    D = m._design(wild.to_numpy())
    assert D.max() <= 5.0 and D.min() >= -5.0
    assert D[0, 0] == 5.0 and D[1, 0] == -5.0 and D[0, 1] == 0.0       # clipped, and NaN -> 0 still
    p = m.predict(wild)
    assert np.isfinite(p).all() and abs(p[0]) < 50


def test_float32_inputs_are_not_upcast_and_infinities_are_still_refused():
    X32 = np.random.default_rng(0).normal(size=(10, 3)).astype(np.float32)
    arr, _ = MM._as_matrix(X32, None, "X")
    assert arr.dtype == np.float32
    arr, _ = MM._as_matrix(pd.DataFrame(X32, columns=list("abc")), None, "X")
    assert arr.dtype == np.float32
    arr, _ = MM._as_matrix(X32.astype(np.float64), None, "X")
    assert arr.dtype == np.float64
    arr, _ = MM._as_matrix(np.arange(6).reshape(2, 3), None, "X")        # ints become float64
    assert arr.dtype == np.float64
    bad = X32.copy()
    bad[0, 0] = np.inf
    with pytest.raises(ValueError, match="infinite"):
        MM._as_matrix(bad, None, "X")
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), (Xte, _, _) = parts
    for fam in ("lgbm", "xgb", "catboost", "linear"):
        m = MM.make_adapter(fam).fit(Xtr.astype(np.float32), ytr, Xva.astype(np.float32), yva, "regression", 1)
        assert np.isfinite(m.predict(Xte.astype(np.float32))).all() and m.predict(Xte).dtype == np.float64


def test_quantile_builds_one_training_dataset_for_all_nine_models(monkeypatch):
    import lightgbm as lgb
    made = []
    real = lgb.Dataset

    class Counting(real):
        def __init__(self, *a, **k):
            made.append(1)
            super().__init__(*a, **k)
    monkeypatch.setattr(lgb, "Dataset", Counting)
    m, p, y, lat = _fit_predict("lgbm", "quantile")
    assert len(made) == 2 and len(m._models) == 9                        # train + validation, reused nine times
    assert spearmanr(p[:, 4], lat).correlation > 0.2


def test_no_per_fit_warning_filter_and_no_deprecation_noise():
    import inspect
    import warnings
    parts, _, _ = _data("regression")
    (Xtr, ytr, _), (Xva, yva, _), _ = parts
    with warnings.catch_warnings(record=True) as w:
        MM.make_adapter("lgbm").fit(Xtr, ytr, Xva, yva, "regression", 1)
    assert not [x for x in w if "eval_set" in str(x.message)]
    assert "catch_warnings" not in inspect.getsource(MM.LGBMAdapter)     # thread-safe: the filter is set once at import


@pytest.mark.parametrize("family,bad", [
    ("lgbm", {"n_estimators": 0}), ("lgbm", {"num_leaves": 2.5}), ("lgbm", {"learning_rate": -0.1}),
    ("lgbm", {"early_stopping_rounds": True}), ("lgbm", {"subsample": 0.0}), ("lgbm", {"colsample_bytree": 1.5}),
    ("lgbm", {"reg_lambda": -1.0}), ("lgbm", {"min_child_samples": 0}),
    ("xgb", {"max_depth": 0}), ("xgb", {"min_child_weight": -1.0}), ("catboost", {"depth": 0}),
    ("catboost", {"l2_leaf_reg": -1.0}), ("linear", {"C": 0.0}), ("linear", {"max_iter": 0}),
    ("linear", {"alpha": -1.0}), ("naive", {"column": ""}), ("lgbm", {"learning_rate": float("nan")}),
])
def test_param_values_are_validated(family, bad):
    with pytest.raises(ValueError, match="param"):
        MM.make_adapter(family, params=bad)


def test_valid_param_overrides_are_accepted():
    MM.make_adapter("lgbm", params={"n_estimators": 10, "num_leaves": 4, "learning_rate": 0.3, "subsample": 1.0})
    MM.make_adapter("xgb", params={"max_depth": 3})
    MM.make_adapter("linear", params={"C": 0.1, "alpha": 10.0})
    with pytest.raises(ValueError):
        MM.make_adapter("lgbm", n_jobs=0)
