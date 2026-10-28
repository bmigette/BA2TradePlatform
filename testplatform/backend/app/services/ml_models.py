"""Model adapters for the ML exploration v2 grid (tabular families), one interface for every family.

The grid screens (target x model family x training length x feature group) configurations, three seeds each, on
walk-forward folds. Every cell needs the same four calls -- ``fit`` on a training block with a time-ordered validation
block for early stopping, ``predict`` on the next block -- so each family hides its library behind ``ModelAdapter``.

Tasks and what ``predict`` returns:

* ``binary``      labels {0, 1}; ``(n, 2)`` class probabilities (column 1 = P(class 1)). The ``ml_targets`` binary
  targets use labels 0 / 2: the caller maps them, an adapter never guesses.
* ``multiclass``  labels 0..k-1, every class present in the training block; ``(n, k)`` probabilities.
* ``regression``  float labels; ``(n,)`` point forecast.
* ``rank``        (lgbm only) non-negative integer relevance, LightGBM lambdarank; ``groups`` = rows per session,
  the rows being consecutive by session; ``(n,)`` score, higher = more relevant.
* ``quantile``    (lgbm only) nine models at alpha 0.1 .. 0.9; ``(n, 9)`` predictions, sorted per row so the quantiles
  never cross.

Families: ``lgbm``, ``xgb``, ``catboost``, ``linear`` (a robust-scaled logistic / ridge baseline) and ``naive`` (no
fit: one named feature column returned as the score; the benchmark every model has to beat).

Determinism: every stochastic family takes the seed and runs deterministically (LightGBM ``deterministic`` +
``force_row_wise``; XGBoost / CatBoost seeded, ``hist`` / CPU). Row and column subsampling are on by default
(``DEFAULTS``) so that different seeds really give different models. ``linear`` and ``naive`` have no randomness and
ignore the seed. Inputs with NaN are fine for the trees (native missing handling) and for ``linear`` (imputed after
scaling, see ``LinearAdapter``); infinities are always refused. Nothing here reads files or the clock.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TASKS: Tuple[str, ...] = ("binary", "multiclass", "regression", "rank", "quantile")
QUANTILE_LEVELS: Tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

#: tasks each family can do; anything else is refused at ``fit``
SUPPORTED_TASKS: Dict[str, Tuple[str, ...]] = {
    "lgbm": ("binary", "multiclass", "regression", "rank", "quantile"),
    "xgb": ("binary", "multiclass", "regression"),
    "catboost": ("binary", "multiclass", "regression"),
    "linear": ("binary", "multiclass", "regression"),
    # the naive column is a score, not a probability: it can stand in for a point forecast / ranking / binary score
    "naive": ("binary", "regression", "rank"),
}

#: explicit hyper-parameter defaults per family; ``params`` overrides a subset (unknown keys are refused). Declared
#: here, never defaulted in a ``.get``, so a grid cell's configuration is readable in one place.
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "lgbm": {"learning_rate": 0.05, "num_leaves": 31, "n_estimators": 2000, "early_stopping_rounds": 50,
             "min_child_samples": 100, "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8,
             "reg_lambda": 1.0, "rank_eval_at": 10},
    "xgb": {"learning_rate": 0.05, "max_depth": 6, "n_estimators": 2000, "early_stopping_rounds": 50,
            "min_child_weight": 20.0, "subsample": 0.8, "colsample_bytree": 0.8, "reg_lambda": 1.0},
    "catboost": {"learning_rate": 0.05, "depth": 6, "n_estimators": 2000, "early_stopping_rounds": 50,
                 "l2_leaf_reg": 3.0, "subsample": 0.8},
    "linear": {"C": 1.0, "max_iter": 2000, "alpha": 1.0},
    "naive": {"column": "t_ret_20"},
}


class UnsupportedTaskError(ValueError):
    """The (family, task) pair does not exist."""


# ------------------------------------------------------------------ input helpers
def _as_matrix(X, names: Optional[Sequence[str]], where: str) -> Tuple[np.ndarray, Optional[Tuple[str, ...]]]:
    """float64 matrix of ``X`` (DataFrame or array) and its column names (None for an unnamed array). Infinities are
    refused: a tree would silently bin them and a linear model would be wrecked."""
    if isinstance(X, pd.DataFrame):
        cols = tuple(str(c) for c in X.columns)
        if names is not None and tuple(names) != cols:
            raise ValueError(f"{where}: feature_names disagree with the DataFrame columns")
        arr = X.to_numpy(dtype=np.float64)
    else:
        arr = np.asarray(X, dtype=np.float64)
        cols = tuple(str(n) for n in names) if names is not None else None
    if arr.ndim != 2:
        raise ValueError(f"{where}: X must be 2-D, got shape {arr.shape}")
    if cols is not None and len(cols) != arr.shape[1]:
        raise ValueError(f"{where}: {len(cols)} feature names for {arr.shape[1]} columns")
    if np.isinf(arr).any():
        raise ValueError(f"{where}: X contains an infinite value (refused, clean the feature upstream)")
    return arr, cols


def _as_labels(y, task: str, n: int, where: str) -> np.ndarray:
    arr = np.asarray(y, dtype=np.float64).ravel()
    if len(arr) != n:
        raise ValueError(f"{where}: {len(arr)} labels for {n} rows")
    if not np.isfinite(arr).all():
        raise ValueError(f"{where}: labels contain NaN / inf (drop unlabelled rows before fitting)")
    if task in ("binary", "multiclass", "rank"):
        if not np.array_equal(arr, np.round(arr)):
            raise ValueError(f"{where}: {task} labels must be integers")
        if arr.min() < 0:
            raise ValueError(f"{where}: {task} labels must be >= 0")
        arr = arr.astype(np.int64)
    return arr


def _check_groups(groups, n: int, where: str) -> np.ndarray:
    if groups is None:
        raise ValueError(f"{where}: rank needs groups (rows per session, rows consecutive by session)")
    g = np.asarray(groups, dtype=np.int64).ravel()
    if (g <= 0).any() or int(g.sum()) != n:
        raise ValueError(f"{where}: groups must be positive and sum to the {n} rows (got sum {int(g.sum())})")
    return g


class ModelAdapter:
    """Interface and shared validation. Subclasses implement ``_fit`` / ``_predict`` on clean float matrices."""

    family: str = ""

    def __init__(self, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None) -> None:
        if self.family not in DEFAULTS:
            raise TypeError("instantiate a concrete adapter (see make_adapter)")
        if n_jobs < 1:
            raise ValueError("n_jobs must be >= 1")
        params = {} if params is None else dict(params)
        unknown = sorted(set(params) - set(DEFAULTS[self.family]))
        if unknown:
            raise ValueError(f"{self.family}: unknown params {unknown}; known {sorted(DEFAULTS[self.family])}")
        self.n_jobs = int(n_jobs)
        self.params: Dict[str, Any] = {**DEFAULTS[self.family], **params}
        self.task: Optional[str] = None
        self.seed: Optional[int] = None
        self.feature_names: Optional[Tuple[str, ...]] = None
        self.n_features: Optional[int] = None
        self.n_classes: Optional[int] = None
        #: boosting rounds kept by early stopping (a list for quantile: one per alpha); None for linear / naive
        self.n_iter: Any = None

    # -- public
    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None,
            feature_names: Optional[Sequence[str]] = None) -> "ModelAdapter":
        if task not in TASKS:
            raise UnsupportedTaskError(f"unknown task {task!r}; known {TASKS}")
        if task not in SUPPORTED_TASKS[self.family]:
            raise UnsupportedTaskError(f"{self.family} does not support task {task!r} "
                                       f"(supports {SUPPORTED_TASKS[self.family]})")
        Xt, names = _as_matrix(X_train, feature_names, "X_train")
        Xv, vnames = _as_matrix(X_val, feature_names, "X_val")
        if Xt.shape[1] != Xv.shape[1] or (names is not None and vnames is not None and names != vnames):
            raise ValueError("X_train and X_val must have the same columns")
        if len(Xt) == 0:
            raise ValueError("X_train is empty")
        if len(Xv) == 0 and self.needs_validation:
            raise ValueError(f"{self.family}: the validation block is empty (early stopping needs one)")
        yt = _as_labels(y_train, task, len(Xt), "y_train")
        yv = _as_labels(y_val, task, len(Xv), "y_val")
        gt = gv = None
        if task == "rank":
            gt = _check_groups(groups_train, len(Xt), "groups_train")
            gv = _check_groups(groups_val, len(Xv), "groups_val")
            if yt.max() > 30 or yv.max() > 30:
                raise ValueError("rank relevance labels must be in 0..30 (LightGBM's default label gain)")
        if task in ("binary", "multiclass"):
            k = int(yt.max()) + 1
            if task == "binary" and (k > 2 or yv.max(initial=0) > 1):
                raise ValueError("binary labels must be 0 / 1 (map the ml_targets 0 / 2 labels first)")
            present = np.unique(yt)
            if task == "multiclass" and (k < 3 or len(present) != k):
                raise ValueError(f"multiclass needs >= 3 classes, all of 0..k-1 present in y_train "
                                 f"(present: {present.tolist()})")
            if task == "binary" and len(present) != 2:
                raise ValueError("binary y_train must contain both classes")
            if task == "multiclass" and yv.size and int(yv.max()) >= k:
                raise ValueError("y_val has a class absent from y_train")
            self.n_classes = 2 if task == "binary" else k
        self.task, self.seed = task, int(seed)
        self.feature_names, self.n_features = names, Xt.shape[1]
        self._fit(Xt, yt, Xv, yv, task, int(seed), gt, gv)
        return self

    def predict(self, X) -> np.ndarray:
        if self.task is None:
            raise RuntimeError(f"{self.family}: predict before fit")
        arr, names = _as_matrix(X, None, "X")
        if arr.shape[1] != self.n_features:
            raise ValueError(f"X has {arr.shape[1]} columns, the model was fitted on {self.n_features}")
        if self.feature_names is not None and names is not None and names != self.feature_names:
            raise ValueError("X columns differ from the fitted feature names (order matters)")
        out = np.asarray(self._predict(arr), dtype=np.float64)
        if not self.nan_ok and not np.isfinite(out).all():
            raise ValueError(f"{self.family}: non-finite prediction")
        return out

    # -- subclass hooks
    needs_validation = True
    #: True where a NaN output is meaningful (the naive score is NaN where its feature is not yet available)
    nan_ok = False

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        raise NotImplementedError

    def _predict(self, X) -> np.ndarray:
        raise NotImplementedError


# ------------------------------------------------------------------ LightGBM
class LGBMAdapter(ModelAdapter):
    """LightGBM: every task. Early stopping on the validation block (``early_stopping_rounds``), up to
    ``n_estimators`` rounds. Quantile = nine independent models, sorted per row at predict (monotone)."""

    family = "lgbm"

    def _common(self, seed: int) -> Dict[str, Any]:
        p = self.params
        return dict(n_estimators=p["n_estimators"], learning_rate=p["learning_rate"], num_leaves=p["num_leaves"],
                    min_child_samples=p["min_child_samples"], subsample=p["subsample"],
                    subsample_freq=p["subsample_freq"], colsample_bytree=p["colsample_bytree"],
                    reg_lambda=p["reg_lambda"], n_jobs=self.n_jobs, random_state=seed, deterministic=True,
                    force_row_wise=True, verbose=-1)

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        import warnings
        with warnings.catch_warnings():
            # lightgbm 4.7 deprecates ``eval_set`` in favour of eval_X / eval_y, which older releases lack
            warnings.filterwarnings("ignore", message=".*'eval_set' is deprecated.*")
            self._fit_lgbm(Xt, yt, Xv, yv, task, seed, gt, gv)

    def _fit_lgbm(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        import lightgbm as lgb
        stop = [lgb.early_stopping(self.params["early_stopping_rounds"], verbose=False)]
        c = self._common(seed)
        if task == "rank":
            m = lgb.LGBMRanker(objective="lambdarank", **c)
            m.fit(Xt, yt, group=gt, eval_set=[(Xv, yv)], eval_group=[gv],
                  eval_at=[int(self.params["rank_eval_at"])], callbacks=stop)
            self._models, self.n_iter = [m], int(m.best_iteration_)
        elif task == "quantile":
            self._models = []
            for a in QUANTILE_LEVELS:
                m = lgb.LGBMRegressor(objective="quantile", alpha=a, **c)
                m.fit(Xt, yt, eval_set=[(Xv, yv)], callbacks=stop)
                self._models.append(m)
            self.n_iter = [int(m.best_iteration_) for m in self._models]
        elif task == "regression":
            m = lgb.LGBMRegressor(objective="regression", **c)
            m.fit(Xt, yt, eval_set=[(Xv, yv)], callbacks=stop)
            self._models, self.n_iter = [m], int(m.best_iteration_)
        else:
            obj = "binary" if task == "binary" else "multiclass"
            m = lgb.LGBMClassifier(objective=obj, **c)
            m.fit(Xt, yt, eval_set=[(Xv, yv)], callbacks=stop)
            self._models, self.n_iter = [m], int(m.best_iteration_)

    def _predict(self, X) -> np.ndarray:
        if self.task == "quantile":
            q = np.column_stack([m.predict(X, num_iteration=m.best_iteration_) for m in self._models])
            return np.sort(q, axis=1)
        m = self._models[0]
        if self.task in ("binary", "multiclass"):
            return m.predict_proba(X, num_iteration=m.best_iteration_)
        return m.predict(X, num_iteration=m.best_iteration_)


# ------------------------------------------------------------------ XGBoost
class XGBAdapter(ModelAdapter):
    """XGBoost (``hist``): binary, multiclass, regression."""

    family = "xgb"

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        import xgboost as xgb
        p = self.params
        kw = dict(n_estimators=p["n_estimators"], learning_rate=p["learning_rate"], max_depth=p["max_depth"],
                  min_child_weight=p["min_child_weight"], subsample=p["subsample"],
                  colsample_bytree=p["colsample_bytree"], reg_lambda=p["reg_lambda"], tree_method="hist",
                  n_jobs=self.n_jobs, random_state=seed, early_stopping_rounds=p["early_stopping_rounds"],
                  verbosity=0)
        if task == "regression":
            m = xgb.XGBRegressor(objective="reg:squarederror", **kw)
        elif task == "binary":
            m = xgb.XGBClassifier(objective="binary:logistic", **kw)
        else:
            m = xgb.XGBClassifier(objective="multi:softprob", **kw)
        m.fit(Xt, yt, eval_set=[(Xv, yv)], verbose=False)
        self._model, self.n_iter = m, int(m.best_iteration) + 1

    def _predict(self, X) -> np.ndarray:
        m = self._model
        it = (0, self.n_iter)
        if self.task == "regression":
            return m.predict(X, iteration_range=it)
        return m.predict_proba(X, iteration_range=it)


# ------------------------------------------------------------------ CatBoost
class CatBoostAdapter(ModelAdapter):
    """CatBoost (CPU): binary, multiclass, regression. ``use_best_model`` keeps the early-stopping optimum."""

    family = "catboost"

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        from catboost import CatBoostClassifier, CatBoostRegressor
        p = self.params
        kw = dict(iterations=p["n_estimators"], learning_rate=p["learning_rate"], depth=p["depth"],
                  l2_leaf_reg=p["l2_leaf_reg"], subsample=p["subsample"], bootstrap_type="Bernoulli",
                  random_seed=seed, thread_count=self.n_jobs, early_stopping_rounds=p["early_stopping_rounds"],
                  use_best_model=True, verbose=False, allow_writing_files=False)
        if task == "regression":
            m = CatBoostRegressor(loss_function="RMSE", **kw)
        elif task == "binary":
            m = CatBoostClassifier(loss_function="Logloss", **kw)
        else:
            m = CatBoostClassifier(loss_function="MultiClass", **kw)
        m.fit(Xt, yt, eval_set=(Xv, yv))
        self._model, self.n_iter = m, int(m.get_best_iteration()) + 1

    def _predict(self, X) -> np.ndarray:
        if self.task == "regression":
            return self._model.predict(X)
        return self._model.predict_proba(X)


# ------------------------------------------------------------------ linear baseline
class LinearAdapter(ModelAdapter):
    """Logistic regression (``C``, binary / multiclass) or ridge (``alpha``, regression) on robust-scaled features.

    The scaler is fitted on the TRAINING block only (median / inter-quartile range per feature, NaN-aware; a feature
    with no spread keeps scale 1). After scaling, NaN becomes 0 (= the training median). With
    ``missing_indicators=True`` one 0/1 "was missing" column is appended for every feature that had a NaN in the
    training block, so the model can tell a missing value from a median one. Seed-free."""

    family = "linear"
    needs_validation = False

    def __init__(self, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None,
                 missing_indicators: bool = False) -> None:
        super().__init__(n_jobs, params)
        self.missing_indicators = bool(missing_indicators)

    def _design(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self._center) / self._scale
        parts = [np.nan_to_num(Z, nan=0.0)]
        if self.missing_indicators:
            parts.append(np.isnan(X[:, self._nan_cols]).astype(np.float64))
        return np.hstack(parts)

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        from sklearn.linear_model import LogisticRegression, Ridge
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)      # all-NaN column: handled just below
            self._center = np.nanmedian(Xt, axis=0)
            iqr = np.nanpercentile(Xt, 75, axis=0) - np.nanpercentile(Xt, 25, axis=0)
        self._center = np.where(np.isfinite(self._center), self._center, 0.0)
        self._scale = np.where(np.isfinite(iqr) & (iqr > 1e-12), iqr, 1.0)
        self._nan_cols = np.flatnonzero(np.isnan(Xt).any(axis=0))
        D = self._design(Xt)
        p = self.params
        if task == "regression":
            self._model = Ridge(alpha=p["alpha"])
        else:
            self._model = LogisticRegression(C=p["C"], max_iter=int(p["max_iter"]))
        self._model.fit(D, yt)

    def _predict(self, X) -> np.ndarray:
        D = self._design(X)
        if self.task == "regression":
            return self._model.predict(D)
        return self._model.predict_proba(D)


# ------------------------------------------------------------------ naive benchmark
class NaiveAdapter(ModelAdapter):
    """No model: the named feature column (``params["column"]``, default ``t_ret_20`` = 20-session momentum) is the
    prediction. Needs the feature names (a DataFrame, or ``feature_names`` at fit). Its output is a score, not a
    probability, whatever the task. Ignores the seed and the labels."""

    family = "naive"
    needs_validation = False
    nan_ok = True

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv) -> None:
        col = self.params["column"]
        if self.feature_names is None:
            raise ValueError("naive needs the feature names (pass a DataFrame or feature_names=...)")
        if col not in self.feature_names:
            raise ValueError(f"naive column {col!r} is not among the features")
        self._idx = self.feature_names.index(col)

    def _predict(self, X) -> np.ndarray:
        return X[:, self._idx].copy()

    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None,
            feature_names: Optional[Sequence[str]] = None) -> "ModelAdapter":
        # labels are irrelevant to the naive score, so do not insist on their form (binary balance, groups ...)
        if task not in TASKS:
            raise UnsupportedTaskError(f"unknown task {task!r}; known {TASKS}")
        if task not in SUPPORTED_TASKS[self.family]:
            raise UnsupportedTaskError(f"naive does not support task {task!r} (supports {SUPPORTED_TASKS['naive']})")
        Xt, names = _as_matrix(X_train, feature_names, "X_train")
        self.task, self.seed = task, int(seed)
        self.feature_names, self.n_features = names, Xt.shape[1]
        self._fit(Xt, None, None, None, task, int(seed), None, None)
        return self


_FAMILIES = {"lgbm": LGBMAdapter, "xgb": XGBAdapter, "catboost": CatBoostAdapter, "linear": LinearAdapter,
             "naive": NaiveAdapter}


def make_adapter(family: str, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None, **kwargs) -> ModelAdapter:
    """Adapter of ``family`` ('lgbm' | 'xgb' | 'catboost' | 'linear' | 'naive'); ``kwargs`` go to the class
    (``missing_indicators`` for linear)."""
    if family not in _FAMILIES:
        raise ValueError(f"unknown model family {family!r}; known {sorted(_FAMILIES)}")
    return _FAMILIES[family](n_jobs=n_jobs, params=params, **kwargs)
