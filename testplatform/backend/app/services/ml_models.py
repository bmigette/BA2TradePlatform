"""Model adapters for the ML exploration v2 grid (tabular families), one interface for every family.

The grid screens (target x model family x training length x feature group) configurations, three seeds each, on
walk-forward folds. Every cell needs the same calls -- ``fit`` on a training block with a time-ordered validation
block for early stopping, then ``predict`` / ``score`` on the next block -- so each family hides its library behind
``ModelAdapter``.

Tasks and what ``predict`` returns:

* ``binary``      labels {0, 1}; ``(n, 2)`` class probabilities (column 1 = P(class 1)). The ``ml_targets`` binary
  targets use labels 0 / 2: the caller maps them, an adapter never guesses.
* ``multiclass``  labels 0..k-1, every class present in the training block; ``(n, k)`` probabilities.
* ``regression``  float labels; ``(n,)`` point forecast.
* ``rank``        (lgbm only) non-negative integer relevance, LightGBM lambdarank; ``groups`` = rows per session,
  the rows being consecutive by session; ``(n,)`` score, higher = more relevant.
* ``quantile``    (lgbm only) nine models at alpha 0.1 .. 0.9; ``(n, 9)`` predictions, sorted per row so the quantiles
  never cross.

``score(X)`` is the ONE 1-D "higher = more bullish" output the runner and the metrics consume, whatever the task
(``score_kind`` names what it is): binary -> P(class 1); multiclass -> P(top class) - P(bottom class), which reads as
"net bullishness" ONLY for ordered classes (0 = down ... k-1 = up, as the ml_targets direction / triple-barrier
labels are); regression / rank -> the prediction; quantile -> the median (alpha 0.5); naive -> its feature column.

Families: ``lgbm``, ``xgb``, ``catboost``, ``linear`` (a robust-scaled logistic / ridge baseline) and ``naive`` (no
fit: one named feature column returned as the score; the benchmark every model has to beat). Two more families take
WINDOWS of past bars instead of a flat row and live in ``app.services.ml_models_seq`` (loaded on first use by
``make_adapter``): ``tsai`` (GPU classifiers / regressors: inception, tst, lstm_fcn, minirocket, patchtst, tcn,
resnet) and ``darts`` (N-HiTS / N-BEATS / TCN / TFT quantile forecasters whose score is P(up)). Their ``X`` is a
``SequenceData`` (see that module), not a matrix; everything else (``fit`` / ``predict`` / ``score`` / ``score_kind``,
``DEFAULTS``, param validation, the unfitted-after-a-failed-fit rule, the refusals) is the same.

Determinism: every stochastic family takes the seed and runs deterministically (LightGBM ``deterministic`` +
``force_row_wise``; XGBoost / CatBoost seeded, ``hist`` / CPU). Row and column subsampling are on by default
(``DEFAULTS``) so that different seeds really give different models. ``linear`` and ``naive`` have no randomness and
ignore the seed. Inputs with NaN are fine for the trees (native missing handling) and for ``linear`` (imputed after
scaling, see ``LinearAdapter``); infinities are always refused. float32 inputs are used as they are (no upcast copy).
Nothing here reads files or the clock.

State: a failed ``fit`` leaves the adapter UNFITTED (``predict`` raises); the fitted attributes are assigned only
after the underlying library fit succeeded.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd



def _lgb_eval(estimator_cls, Xv, yv) -> Dict[str, Any]:
    """The validation-set kwargs of ``estimator_cls.fit``: lightgbm >= 4.6 deprecates ``eval_set`` in favour of
    ``eval_X`` / ``eval_y``; older releases only know ``eval_set``. Choosing by signature (once per call) avoids both
    the deprecation warning and a per-fit ``warnings.catch_warnings``, which is not thread safe."""
    import inspect
    if "eval_X" in inspect.signature(estimator_cls.fit).parameters:
        return {"eval_X": Xv, "eval_y": yv}
    return {"eval_set": [(Xv, yv)]}


TASKS: Tuple[str, ...] = ("binary", "multiclass", "regression", "rank", "quantile")
QUANTILE_LEVELS: Tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MEDIAN_INDEX = QUANTILE_LEVELS.index(0.5)
#: linear features are clipped to +-LINEAR_CLIP after robust scaling (a heavy-tailed feature must not dominate)
LINEAR_CLIP = 5.0

#: tasks each family can do; anything else is refused at ``fit``
SUPPORTED_TASKS: Dict[str, Tuple[str, ...]] = {
    "lgbm": ("binary", "multiclass", "regression", "rank", "quantile"),
    "xgb": ("binary", "multiclass", "regression"),
    "catboost": ("binary", "multiclass", "regression"),
    "linear": ("binary", "multiclass", "regression"),
    # the naive column is a score, not a probability: it can stand in for a point forecast / ranking / binary score
    "naive": ("binary", "regression", "rank"),
    "tsai": ("binary", "multiclass", "regression"),
    "darts": ("quantile",),
}
#: families whose ``X`` is a ``ml_models_seq.SequenceData`` (windows) instead of a feature matrix
SEQUENCE_FAMILIES: Tuple[str, ...] = ("tsai", "darts")

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
    # ``min_epochs``: epochs that always run before early stopping may trigger (OneCycle warms up to its peak learning
    # rate first); clamped to ``epochs``.
    # tsai: ``arch`` one of ml_models_seq.TSAI_ARCHS; ``width`` is the architecture's main size (nf = width / 2 for
    # inception, d_model for tst / patchtst, hidden size for lstm_fcn, filters for tcn; resnet and minirocket have
    # none); ``class_weights`` "none" | "balanced" (classification only); ``device`` "auto" | "cuda" | "cpu";
    # ``rocket_features`` is the MiniRocket feature count (about; it is rounded to the kernel grid)
    "tsai": {"arch": "inception", "width": 64, "dropout": 0.1, "lr": 1e-3, "epochs": 20, "batch_size": 512,
             "patience": 3, "min_epochs": 3, "weight_decay": 1e-2, "grad_clip": 1.0, "class_weights": "none",
             "device": "auto", "rocket_features": 10000},
    # darts: ``arch`` one of ml_models_seq.DARTS_ARCHS; ``num_samples`` = sampled paths per forecast (the deciles
    # are percentiles of the summed paths)
    "darts": {"arch": "nhits", "width": 64, "dropout": 0.1, "lr": 1e-3, "epochs": 10, "batch_size": 256,
              "patience": 3, "min_epochs": 3, "num_samples": 200, "device": "auto"},
}

# value rules for ``params`` (every key in DEFAULTS that is not listed here is a free-form string, i.e. ``column``)
_POS_INT = {"n_estimators", "num_leaves", "early_stopping_rounds", "min_child_samples", "rank_eval_at", "max_depth",
            "depth", "max_iter", "width", "epochs", "batch_size", "patience", "min_epochs", "rocket_features",
            "num_samples"}
_NONNEG_INT = {"subsample_freq"}
_POS_FLOAT = {"learning_rate", "C", "alpha", "lr", "grad_clip"}
_DROPOUT = {"dropout"}                                              # [0, 1)
_UNIT_FLOAT = {"subsample", "colsample_bytree"}                     # (0, 1]
_NONNEG_FLOAT = {"reg_lambda", "min_child_weight", "l2_leaf_reg", "weight_decay"}


class UnsupportedTaskError(ValueError):
    """The (family, task) pair does not exist."""


def _is_int(v: Any) -> bool:
    return isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))


def _is_real(v: Any) -> bool:
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_)) \
        and math.isfinite(float(v))


def _check_params(family: str, params: Dict[str, Any]) -> None:
    """Refuse a hyper-parameter value that is the wrong type or out of range, naming it."""
    for k, v in params.items():
        if k in _POS_INT:
            ok, rule = _is_int(v) and v >= 1, "an integer >= 1"
        elif k in _NONNEG_INT:
            ok, rule = _is_int(v) and v >= 0, "an integer >= 0"
        elif k in _POS_FLOAT:
            ok, rule = _is_real(v) and v > 0, "a finite number > 0"
        elif k in _UNIT_FLOAT:
            ok, rule = _is_real(v) and 0 < v <= 1, "a number in (0, 1]"
        elif k in _NONNEG_FLOAT:
            ok, rule = _is_real(v) and v >= 0, "a finite number >= 0"
        elif k in _DROPOUT:
            ok, rule = _is_real(v) and 0 <= v < 1, "a number in [0, 1)"
        else:
            ok, rule = isinstance(v, str) and bool(v), "a non-empty string"
        if not ok:
            raise ValueError(f"{family}: param {k}={v!r} must be {rule}")


# ------------------------------------------------------------------ input helpers
def _as_matrix(X, names: Optional[Sequence[str]], where: str) -> Tuple[np.ndarray, Optional[Tuple[str, ...]]]:
    """Matrix of ``X`` (DataFrame or array) and its column names (None for an unnamed array): float32 stays float32,
    anything else becomes float64. Infinities are refused: a tree would silently bin them and a linear model would
    be wrecked."""
    if isinstance(X, pd.DataFrame):
        cols = tuple(str(c) for c in X.columns)
        if names is not None and tuple(names) != cols:
            raise ValueError(f"{where}: feature_names disagree with the DataFrame columns")
        dt = np.float32 if len(X.columns) and all(t == np.float32 for t in X.dtypes) else np.float64
        arr = X.to_numpy(dtype=dt)
    else:
        raw = np.asarray(X)
        arr = np.asarray(raw, dtype=np.float32 if raw.dtype == np.float32 else np.float64)
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
        if len(arr) and arr.min() < 0:
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


def _check_classes(task: str, yt: np.ndarray, yv: np.ndarray) -> Optional[int]:
    """Class-label rules of binary / multiclass (``None`` for the other tasks); returns the class count."""
    if task not in ("binary", "multiclass"):
        return None
    k = int(yt.max()) + 1
    if task == "binary" and (k > 2 or (yv.size and yv.max() > 1)):
        raise ValueError("binary labels must be 0 / 1 (map the ml_targets 0 / 2 labels first)")
    present = np.unique(yt)
    if task == "multiclass" and (k < 3 or len(present) != k):
        raise ValueError(f"multiclass needs >= 3 classes, all of 0..k-1 present in y_train "
                         f"(present: {present.tolist()})")
    if task == "binary" and len(present) != 2:
        raise ValueError("binary y_train must contain both classes")
    if task == "multiclass" and yv.size and int(yv.max()) >= k:
        raise ValueError("y_val has a class absent from y_train")
    return 2 if task == "binary" else k


class ModelAdapter:
    """Interface and shared validation. Subclasses implement ``_fit`` / ``_predict`` on clean matrices."""

    family: str = ""
    #: the fit needs a non-empty validation block (early stopping); linear / naive do not
    needs_validation = True
    #: the fit uses the labels; naive does not, so labels, groups and the class checks are skipped for it
    needs_labels = True
    #: True where a NaN output is meaningful (the naive score is NaN where its feature is not yet available)
    nan_ok = False

    def __init__(self, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None) -> None:
        if self.family not in DEFAULTS:
            raise TypeError("instantiate a concrete adapter (see make_adapter)")
        if not _is_int(n_jobs) or n_jobs < 1:
            raise ValueError("n_jobs must be an integer >= 1")
        params = {} if params is None else dict(params)
        unknown = sorted(set(params) - set(DEFAULTS[self.family]))
        if unknown:
            raise ValueError(f"{self.family}: unknown params {unknown}; known {sorted(DEFAULTS[self.family])}")
        _check_params(self.family, params)
        self.n_jobs = int(n_jobs)
        self.params: Dict[str, Any] = {**DEFAULTS[self.family], **params}
        self._reset()

    def _reset(self) -> None:
        """Back to the unfitted state (start of every ``fit``, so a failed refit leaves nothing half-set)."""
        self.task: Optional[str] = None
        self.seed: Optional[int] = None
        self.feature_names: Optional[Tuple[str, ...]] = None
        self.n_features: Optional[int] = None
        self.n_classes: Optional[int] = None
        #: boosting rounds kept by early stopping (a list for quantile: one per alpha); None for linear / naive
        self.n_iter: Any = None
        for attr in ("_models", "_model", "_center", "_scale", "_nan_cols", "_idx"):
            self.__dict__.pop(attr, None)

    # -- public
    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None,
            feature_names: Optional[Sequence[str]] = None) -> "ModelAdapter":
        self._reset()
        if task not in TASKS:
            raise UnsupportedTaskError(f"unknown task {task!r}; known {TASKS}")
        if task not in SUPPORTED_TASKS[self.family]:
            raise UnsupportedTaskError(f"{self.family} does not support task {task!r} "
                                       f"(supports {SUPPORTED_TASKS[self.family]})")
        Xt, names = _as_matrix(X_train, feature_names, "X_train")
        if len(Xt) == 0:
            raise ValueError("X_train is empty")
        no_val = (not self.needs_validation) and (X_val is None or len(X_val) == 0)
        if no_val:
            Xv = np.empty((0, Xt.shape[1]), dtype=Xt.dtype)
        else:
            Xv, vnames = _as_matrix(X_val, feature_names, "X_val")
            if Xt.shape[1] != Xv.shape[1] or (names is not None and vnames is not None and names != vnames):
                raise ValueError("X_train and X_val must have the same columns")
            if len(Xv) == 0 and self.needs_validation:
                raise ValueError(f"{self.family}: the validation block is empty (early stopping needs one)")
        yt = yv = gt = gv = None
        n_classes = None
        if self.needs_labels:
            yt = _as_labels(y_train, task, len(Xt), "y_train")
            yv = _as_labels([] if no_val else y_val, task, len(Xv), "y_val")
            if task == "rank":
                gt = _check_groups(groups_train, len(Xt), "groups_train")
                gv = None if no_val else _check_groups(groups_val, len(Xv), "groups_val")
                if yt.max() > 30 or (yv.size and yv.max() > 30):
                    raise ValueError("rank relevance labels must be in 0..30 (LightGBM's default label gain)")
            n_classes = _check_classes(task, yt, yv)
        self._fit(Xt, yt, Xv, yv, task, int(seed), gt, gv, names)
        # reached only when the library fit succeeded
        self.task, self.seed = task, int(seed)
        self.feature_names, self.n_features, self.n_classes = names, Xt.shape[1], n_classes
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

    def score(self, X) -> np.ndarray:
        """1-D higher = more bullish score (see the module docstring and ``score_kind``)."""
        out = self.predict(X)
        if self.family == "naive":
            return out
        if self.task == "binary":
            return out[:, 1]
        if self.task == "multiclass":
            return out[:, -1] - out[:, 0]
        if self.task == "quantile":
            return out[:, MEDIAN_INDEX]
        return out

    @property
    def score_kind(self) -> str:
        """What ``score`` is: probability | probability_spread | point_forecast | rank_score | median | feature."""
        if self.task is None:
            raise RuntimeError(f"{self.family}: score_kind before fit")
        if self.family == "naive":
            return "feature"
        return {"binary": "probability", "multiclass": "probability_spread", "regression": "point_forecast",
                "rank": "rank_score", "quantile": "median"}[self.task]

    # -- subclass hooks
    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
        raise NotImplementedError

    def _predict(self, X) -> np.ndarray:
        raise NotImplementedError


# ------------------------------------------------------------------ LightGBM
class LGBMAdapter(ModelAdapter):
    """LightGBM: every task. Early stopping on the validation block (``early_stopping_rounds``), up to
    ``n_estimators`` rounds. Quantile = nine independent boosters trained on ONE shared binned dataset, sorted per
    row at predict (monotone)."""

    family = "lgbm"

    def _sk_params(self, seed: int) -> Dict[str, Any]:
        p = self.params
        return dict(n_estimators=p["n_estimators"], learning_rate=p["learning_rate"], num_leaves=p["num_leaves"],
                    min_child_samples=p["min_child_samples"], subsample=p["subsample"],
                    subsample_freq=p["subsample_freq"], colsample_bytree=p["colsample_bytree"],
                    reg_lambda=p["reg_lambda"], n_jobs=self.n_jobs, random_state=seed, deterministic=True,
                    force_row_wise=True, verbose=-1)

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
        import lightgbm as lgb
        if task == "quantile":
            self._fit_quantile(Xt, yt, Xv, yv, seed)
            return
        stop = [lgb.early_stopping(self.params["early_stopping_rounds"], verbose=False)]
        c = self._sk_params(seed)
        if task == "rank":
            m = lgb.LGBMRanker(objective="lambdarank", **c)
            m.fit(Xt, yt, group=gt, eval_group=[gv], eval_at=[int(self.params["rank_eval_at"])], callbacks=stop,
                  **_lgb_eval(lgb.LGBMRanker, Xv, yv))
        elif task == "regression":
            m = lgb.LGBMRegressor(objective="regression", **c)
            m.fit(Xt, yt, callbacks=stop, **_lgb_eval(lgb.LGBMRegressor, Xv, yv))
        else:
            m = lgb.LGBMClassifier(objective="binary" if task == "binary" else "multiclass", **c)
            m.fit(Xt, yt, callbacks=stop, **_lgb_eval(lgb.LGBMClassifier, Xv, yv))
        self._models, self.n_iter = [m], int(m.best_iteration_)

    def _fit_quantile(self, Xt, yt, Xv, yv, seed: int) -> None:
        """Nine boosters, one binned ``Dataset`` (binning is the expensive, alpha-independent part)."""
        import lightgbm as lgb
        p = self.params
        ds_params = {"min_data_in_leaf": p["min_child_samples"], "verbosity": -1}
        train = lgb.Dataset(Xt, label=yt, params=ds_params, free_raw_data=False)
        valid = lgb.Dataset(Xv, label=yv, reference=train, params=ds_params, free_raw_data=False)
        models, iters = [], []
        for a in QUANTILE_LEVELS:
            params = dict(objective="quantile", alpha=a, metric="quantile", learning_rate=p["learning_rate"],
                          num_leaves=p["num_leaves"], min_data_in_leaf=p["min_child_samples"],
                          bagging_fraction=p["subsample"], bagging_freq=p["subsample_freq"],
                          feature_fraction=p["colsample_bytree"], lambda_l2=p["reg_lambda"],
                          num_threads=self.n_jobs, seed=seed, deterministic=True, force_row_wise=True,
                          verbosity=-1)
            b = lgb.train(params, train, num_boost_round=p["n_estimators"], valid_sets=[valid],
                          callbacks=[lgb.early_stopping(p["early_stopping_rounds"], verbose=False)])
            models.append(b)
            iters.append(int(b.best_iteration))
        self._models, self.n_iter = models, iters

    def _predict(self, X) -> np.ndarray:
        if self.task == "quantile":
            q = np.column_stack([m.predict(X, num_iteration=m.best_iteration) for m in self._models])
            return np.sort(q, axis=1)
        m = self._models[0]
        if self.task in ("binary", "multiclass"):
            return m.predict_proba(X, num_iteration=m.best_iteration_)
        return m.predict(X, num_iteration=m.best_iteration_)


# ------------------------------------------------------------------ XGBoost
class XGBAdapter(ModelAdapter):
    """XGBoost (``hist``): binary, multiclass, regression."""

    family = "xgb"

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
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

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
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
    with no spread keeps scale 1). Scaled values are clipped to +-``LINEAR_CLIP`` (5) so one heavy-tailed feature
    cannot dominate. After that NaN becomes 0 (= the training median). With ``missing_indicators=True`` one 0/1 "was
    missing" column is appended for every feature that had a NaN in the training block, so the model can tell a
    missing value from a median one. Needs no validation block (an empty or ``None`` one is fine). Seed-free."""

    family = "linear"
    needs_validation = False

    def __init__(self, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None,
                 missing_indicators: bool = False) -> None:
        super().__init__(n_jobs, params)
        self.missing_indicators = bool(missing_indicators)

    def _design(self, X: np.ndarray) -> np.ndarray:
        Z = np.clip((X - self._center) / self._scale, -LINEAR_CLIP, LINEAR_CLIP)
        parts = [np.nan_to_num(Z, nan=0.0)]
        if self.missing_indicators:
            parts.append(np.isnan(X[:, self._nan_cols]).astype(np.float64))
        return np.hstack(parts)

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
        from sklearn.linear_model import LogisticRegression, Ridge
        # an all-NaN column has no median / spread: centre 0, scale 1 (it then imputes to 0 like any missing value)
        seen = ~np.isnan(Xt).all(axis=0)
        center = np.zeros(Xt.shape[1])
        iqr = np.ones(Xt.shape[1])
        if seen.any():
            center[seen] = np.nanmedian(Xt[:, seen], axis=0)
            iqr[seen] = np.nanpercentile(Xt[:, seen], 75, axis=0) - np.nanpercentile(Xt[:, seen], 25, axis=0)
        self._center = center
        self._scale = np.where(iqr > 1e-12, iqr, 1.0)
        self._nan_cols = np.flatnonzero(np.isnan(Xt).any(axis=0))
        D = self._design(Xt)
        p = self.params
        if task == "regression":
            model = Ridge(alpha=p["alpha"])
        else:
            model = LogisticRegression(C=p["C"], max_iter=int(p["max_iter"]))
        model.fit(D, yt)
        self._model = model

    def _predict(self, X) -> np.ndarray:
        D = self._design(X)
        if self.task == "regression":
            return self._model.predict(D)
        return self._model.predict_proba(D)


# ------------------------------------------------------------------ naive benchmark
class NaiveAdapter(ModelAdapter):
    """No model: the named feature column (``params["column"]``, default ``t_ret_20`` = 20-session momentum) is the
    prediction. Needs the feature names (a DataFrame, or ``feature_names`` at fit). Its output is a score, not a
    probability, whatever the task. Uses no labels, groups or validation block (all may be ``None``) and ignores the
    seed."""

    family = "naive"
    needs_validation = False
    needs_labels = False
    nan_ok = True

    def _fit(self, Xt, yt, Xv, yv, task, seed, gt, gv, names) -> None:
        col = self.params["column"]
        if names is None:
            raise ValueError("naive needs the feature names (pass a DataFrame or feature_names=...)")
        if col not in names:
            raise ValueError(f"naive column {col!r} is not among the features")
        self._idx = names.index(col)

    def _predict(self, X) -> np.ndarray:
        return X[:, self._idx].copy()


_FAMILIES = {"lgbm": LGBMAdapter, "xgb": XGBAdapter, "catboost": CatBoostAdapter, "linear": LinearAdapter,
             "naive": NaiveAdapter}


def make_adapter(family: str, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None, **kwargs) -> ModelAdapter:
    """Adapter of ``family`` ('lgbm' | 'xgb' | 'catboost' | 'linear' | 'naive' | 'tsai' | 'darts'); ``kwargs`` go to
    the class (``missing_indicators`` for linear). The sequence families import torch, so they load on first use."""
    if family in SEQUENCE_FAMILIES:
        from app.services import ml_models_seq
        return ml_models_seq.SEQUENCE_ADAPTERS[family](n_jobs=n_jobs, params=params, **kwargs)
    if family not in _FAMILIES:
        raise ValueError(f"unknown model family {family!r}; known {sorted(_FAMILIES) + list(SEQUENCE_FAMILIES)}")
    return _FAMILIES[family](n_jobs=n_jobs, params=params, **kwargs)
