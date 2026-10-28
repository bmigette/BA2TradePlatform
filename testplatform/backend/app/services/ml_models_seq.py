"""Sequence model adapters of the ML exploration v2 grid: ``tsai`` (GPU classifiers / regressors) and ``darts``
(quantile forecasters), behind the same ``ModelAdapter`` interface as the tabular families (``ml_models``).

INPUT CONTRACT. A sequence model reads a WINDOW of past bars, not a flat row, so ``X`` is a ``SequenceData``:

* ``features``  (n_total, C) float32 matrix of the scaled, finite feature channels of EVERY panel row (all symbols
  stacked, each symbol one contiguous block in time order). It is shared by the train / validation / test objects:
  windows are cut from it, so a sampled training set (``ml_panel.sample_bars``) still sees every bar in its windows.
* ``rows``      (n,) int64 row indices into ``features``: the samples. Sample ``i`` is the decision at bar ``rows[i]``.
* ``lookback``  L: the window of sample ``i`` is rows ``rows[i] - L + 1 .. rows[i]`` OF THE SAME SYMBOL, shape
  ``(C, L)`` (channel-first, the tsai convention). The sample's own bar is the window's last column; nothing after it
  is read.
* ``series_start`` (n_total,) int64: for every row, the first usable row of its own series. A window must not start
  before it (it would run into the symbol before, or into the indicator warm-up); the constructor refuses such rows.
* ``returns``   optional (n_total,) bar log returns (the Darts target series; 0 on a symbol's first row).
* ``horizon_steps`` optional H x bars-per-session: bars from the decision bar to the end of the label's horizon (the
  Darts forecast length; tsai does not use it).
* ``channels``  optional channel names; ``scaler`` the fitted scaling (median / iqr), carried for the model card.

``series_start`` is constant within a series and different between series (a symbol's rows share one value): Darts
groups the samples into series by it.

``y`` is aligned with ``rows`` (``y[i]`` belongs to ``rows[i]``). Labels are the same as for the tabular families:
binary 0 / 1, multiclass 0..k-1, regression floats (standardised inside the adapter with the TRAINING mean / sd and
mapped back at predict), quantile (darts) = the realised H-step return (validated, not trained on).

``sequence_data_from_panel`` builds the matrix the way the earlier search did (``ml_search.fold_data``: robust scaling
fitted on the fold's training rows only, float32 throughout, optional symbol id channel or one-hot, warm-up windows
dropped).

Determinism and numerics policy. Every random draw is seeded (weights, shuffling, dropout, MiniRocket kernels) inside
a ``fork_rng`` scope, so the global torch / numpy generators are left as they were; the Darts deciles use one fixed
set of uniform draws seeded with the fit seed. On the GPU the result is bitwise repeatable only with
``torch.backends.cudnn.deterministic = True`` and ``cudnn.benchmark = False``. The adapters NEVER change global
backend flags or precision: ``cudnn.deterministic`` / ``benchmark``, ``cudnn.allow_tf32`` (PyTorch default: on for
convolutions), ``cuda.matmul.allow_tf32`` and ``set_float32_matmul_precision`` (default: full float32 matmul) are set
once per process by the caller, who owns the speed / reproducibility trade-off. Fits made with different TF32
settings are not comparable.

GPU memory. ``release()`` (both adapters) hands it back after a fit's predictions are done.
"""
from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services.ml_models import (QUANTILE_LEVELS, SUPPORTED_TASKS, TASKS, ModelAdapter, UnsupportedTaskError,
                                    _as_labels, _check_classes, _is_int)

logger = logging.getLogger(__name__)

TSAI_ARCHS: Tuple[str, ...] = ("inception", "tst", "lstm_fcn", "minirocket", "patchtst", "tcn", "resnet")
DARTS_ARCHS: Tuple[str, ...] = ("nhits", "nbeats", "tcn", "tft")
CLASS_WEIGHTS: Tuple[str, ...] = ("none", "balanced")
DEVICES: Tuple[str, ...] = ("auto", "cuda", "cpu")
#: samples of the MiniRocket bias-fitting pass (the library itself uses at most num_dilations * 84 of them)
ROCKET_FIT_SAMPLE = 2048
EVAL_BATCH = 2048


# ------------------------------------------------------------------ input contract
@dataclass(frozen=True, eq=False)
class SequenceData:
    """Windows of past bars of a stacked multi-symbol panel (module docstring: the input contract)."""

    features: np.ndarray
    rows: np.ndarray
    lookback: int
    series_start: np.ndarray
    returns: Optional[np.ndarray] = None
    channels: Tuple[str, ...] = ()
    scaler: Optional[Dict[str, List[float]]] = None
    horizon_steps: Optional[int] = None

    def __post_init__(self):
        f = np.asarray(self.features)
        if f.ndim != 2:
            raise ValueError(f"features must be 2-D (rows x channels), got shape {f.shape}")
        f = np.ascontiguousarray(f, dtype=np.float32)
        if not np.isfinite(f).all():
            raise ValueError("features must be finite (scale and impute upstream; warm-up rows are never in a window)")
        object.__setattr__(self, "features", f)
        if not _is_int(self.lookback) or self.lookback < 1:
            raise ValueError(f"lookback must be an integer >= 1, got {self.lookback!r}")
        object.__setattr__(self, "lookback", int(self.lookback))
        st = np.asarray(self.series_start)
        if st.shape != (len(f),) or not np.issubdtype(st.dtype, np.integer):
            raise ValueError(f"series_start must be an integer vector with one entry per feature row ({len(f)})")
        object.__setattr__(self, "series_start", st.astype(np.int64))
        if self.returns is not None:
            r = np.asarray(self.returns, dtype=np.float64)
            if r.shape != (len(f),) or not np.isfinite(r).all():
                raise ValueError(f"returns must be a finite vector with one entry per feature row ({len(f)})")
            object.__setattr__(self, "returns", r)
        if self.horizon_steps is not None:
            if not _is_int(self.horizon_steps) or self.horizon_steps < 1:
                raise ValueError(f"horizon_steps must be an integer >= 1, got {self.horizon_steps!r}")
            object.__setattr__(self, "horizon_steps", int(self.horizon_steps))
        if self.channels and len(self.channels) != f.shape[1]:
            raise ValueError(f"{len(self.channels)} channel names for {f.shape[1]} channels")
        object.__setattr__(self, "channels", tuple(str(c) for c in self.channels))
        self._set_rows(self.rows)

    def _set_rows(self, rows) -> None:
        r = np.asarray(rows)
        if r.ndim != 1 or (r.size and not np.issubdtype(r.dtype, np.integer)):
            raise ValueError(f"rows must be a 1-D integer vector, got shape {r.shape} dtype {r.dtype}")
        r = r.astype(np.int64)
        n = len(self.features)
        if r.size and (r.min() < 0 or r.max() >= n):
            raise ValueError(f"rows out of range 0..{n - 1}")
        if r.size:
            bad = r - self.lookback + 1 < self.series_start[r]
            if bad.any():
                i = int(np.flatnonzero(bad)[0])
                raise ValueError(f"window of row {int(r[i])} (lookback {self.lookback}) starts before its series "
                                 f"(first usable row {int(self.series_start[r[i]])}); {int(bad.sum())} such rows, "
                                 f"use usable_rows() to drop them")
        object.__setattr__(self, "rows", r)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def n_channels(self) -> int:
        return self.features.shape[1]

    def usable_rows(self, rows) -> np.ndarray:
        """The subset of ``rows`` whose whole window lies inside its own series (order kept)."""
        r = np.asarray(rows, dtype=np.int64)
        return r[r - self.lookback + 1 >= self.series_start[r]]

    def take(self, rows) -> "SequenceData":
        """The same matrix with other sample rows (validated; the arrays are shared, not copied)."""
        out = object.__new__(SequenceData)
        for k in ("features", "lookback", "series_start", "returns", "channels", "scaler", "horizon_steps"):
            object.__setattr__(out, k, getattr(self, k))
        out._set_rows(rows)
        return out

    def windows(self, rows: Optional[Sequence[int]] = None) -> np.ndarray:
        """(n, C, L) float32 windows of ``rows`` (default: the samples)."""
        r = self.rows if rows is None else np.asarray(rows, dtype=np.int64)
        idx = r[:, None] + np.arange(-self.lookback + 1, 1)[None, :]
        return np.ascontiguousarray(self.features[idx].transpose(0, 2, 1))


def sequence_data_from_panel(panel: pd.DataFrame, feats: Sequence[str], fit_rows, lookback: int, pooled: bool = False,
                             rows=None, with_returns: bool = False, horizon_steps: Optional[int] = None,
                             symbol_encoding: str = "id") -> SequenceData:
    """``SequenceData`` of a long panel (one block per symbol, ``panel.index`` = 0..n-1, as ``ml_panel.build_panel``
    returns it). Scaling (median / IQR, clipped at +-5) is fitted on ``fit_rows`` only; ``pooled`` appends the
    symbol as ONE ``sym_id`` channel (index scaled to [0, 1], the default for deep models) or, with
    ``symbol_encoding="onehot"``, a one-hot (refused above 50 symbols). ``rows`` are the candidate samples: those
    whose window reaches back into the indicator warm-up or the
    previous symbol are DROPPED (index the labels with ``result.rows``). ``with_returns`` adds the bar log returns of
    ``close`` (the Darts target series); ``horizon_steps`` is stored for Darts."""
    from app.services import ml_search
    if not panel.index.equals(pd.RangeIndex(len(panel))):
        raise ValueError("the panel needs its positional RangeIndex (pass the full panel, not a sample_bars subset)")
    fd = ml_search.fold_data(panel, list(feats), np.asarray(fit_rows, dtype=np.int64), pooled, symbol_encoding)
    sym = panel["symbol"].to_numpy()
    start = np.array([fd.first_ok[s] for s in sym], dtype=np.int64)
    ret = None
    if with_returns:
        ret = np.zeros(len(panel))
        close = panel["close"].to_numpy(dtype=np.float64)
        for s in dict.fromkeys(sym):
            idx = np.flatnonzero(sym == s)
            ret[idx[1:]] = np.diff(np.log(close[idx]))
    data = SequenceData(fd.X, np.empty(0, dtype=np.int64), lookback, start, ret, tuple(fd.channels), fd.scaler,
                        horizon_steps)
    return data.take(data.usable_rows(rows)) if rows is not None else data


# ------------------------------------------------------------------ architectures
def arch_params(arch: str, width: int, dropout: float, lookback: int, rocket_features: int = 10000) -> Dict[str, Any]:
    """Constructor parameters of ``TSAIModelService.create_model`` for ``arch`` at size ``width``. ``resnet`` has no
    size knob in tsai (``width`` is ignored); ``minirocket`` only has the feature count (its kernels are fixed)."""
    if arch == "inception":
        return {"nf": max(1, width // 2), "depth": 6, "ks": min(40, lookback)}
    if arch == "tst":
        return {"d_model": width, "n_heads": 4, "d_ff": 2 * width, "n_layers": 2, "dropout": dropout}
    if arch == "lstm_fcn":
        return {"hidden_size": width, "rnn_layers": 1, "cell_dropout": dropout, "fc_dropout": dropout}
    if arch == "minirocket":
        return {"num_features": rocket_features, "fc_dropout": dropout}
    if arch == "patchtst":
        return {"d_model": width, "n_heads": 4, "d_ff": 2 * width, "dropout": dropout}
    if arch == "tcn":
        return {"layers": [width] * 3, "ks": min(7, lookback), "conv_dropout": dropout}
    if arch == "resnet":
        return {}
    raise ValueError(f"unknown tsai arch {arch!r}; known {TSAI_ARCHS}")


# ------------------------------------------------------------------ shared helpers
def _n_batches(n: int, batch_size: int) -> int:
    """Batches per epoch: ceil(n / batch_size), fewer when that would leave a batch of one row (BatchNorm cannot train
    on it): the remainder is merged into the others, so every batch has between 2 and max(batch_size, 3) rows
    (n >= 2)."""
    steps = max(1, math.ceil(n / batch_size))
    return max(1, min(steps, n // 2))


def _batch_splits(perm, batch_size: int):
    """``perm`` cut into ``_n_batches`` near-equal chunks (sizes differ by at most one, none below 2)."""
    import torch
    return torch.tensor_split(torch.as_tensor(perm), _n_batches(len(perm), batch_size))


def _check_seq(X, where: str) -> SequenceData:
    if not isinstance(X, SequenceData):
        raise TypeError(f"{where}: X must be a SequenceData (windows of past bars), got {type(X).__name__}")
    return X


def _resolve_device(name: str):
    import torch
    if name == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if name == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("device='cuda' but CUDA is not available")
    return torch.device(name)


@contextlib.contextmanager
def _seeded(seed: int, device):
    """Seed torch (CPU and the device) and numpy for the block, then restore the generators the caller had."""
    import torch
    devices = [device.index if device.index is not None else torch.cuda.current_device()] if device.type == "cuda" else []
    np_state = np.random.get_state()
    try:
        with torch.random.fork_rng(devices=devices):
            torch.manual_seed(seed)
            np.random.seed(seed)
            yield
    finally:
        np.random.set_state(np_state)


def _check_seq_values(family: str, params: Dict[str, Any], archs: Tuple[str, ...]) -> None:
    if params["arch"] not in archs:
        raise ValueError(f"{family}: arch={params['arch']!r} must be one of {archs}")
    if params["device"] not in DEVICES:
        raise ValueError(f"{family}: device={params['device']!r} must be one of {DEVICES}")
    if "class_weights" in params and params["class_weights"] not in CLASS_WEIGHTS:
        raise ValueError(f"{family}: class_weights={params['class_weights']!r} must be one of {CLASS_WEIGHTS}")


class _SequenceAdapter(ModelAdapter):
    """Shared state and checks of the sequence families (``fit`` / ``predict`` read ``SequenceData``)."""

    archs: Tuple[str, ...] = ()

    def __init__(self, n_jobs: int = 1, params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(n_jobs, params)
        _check_seq_values(self.family, self.params, self.archs)

    def _reset(self) -> None:
        super()._reset()
        self.fit_info: Optional[Dict[str, Any]] = None
        self._dev_cache: Dict[str, Tuple[Any, Any]] = {}
        for attr in ("_net", "_device", "_y_mean", "_y_sd", "_lookback", "_models", "_sd", "_n_out"):
            self.__dict__.pop(attr, None)

    def _common_checks(self, X_train, y_train, X_val, y_val, task, feature_names):
        if task not in TASKS:
            raise UnsupportedTaskError(f"unknown task {task!r}; known {TASKS}")
        if task not in SUPPORTED_TASKS[self.family]:
            raise UnsupportedTaskError(f"{self.family} does not support task {task!r} "
                                       f"(supports {SUPPORTED_TASKS[self.family]})")
        Xt, Xv = _check_seq(X_train, "X_train"), _check_seq(X_val, "X_val")
        if len(Xt) == 0:
            raise ValueError("X_train is empty")
        if len(Xv) == 0:
            raise ValueError(f"{self.family}: the validation block is empty (early stopping needs one)")
        if Xt.lookback != Xv.lookback:
            raise ValueError(f"X_train and X_val must have the same lookback ({Xt.lookback} vs {Xv.lookback})")
        if Xt.n_channels != Xv.n_channels:
            raise ValueError(f"X_train and X_val must have the same channels ({Xt.n_channels} vs {Xv.n_channels})")
        if feature_names is not None and Xt.channels and tuple(feature_names) != Xt.channels:
            raise ValueError("feature_names disagree with the SequenceData channels")
        return Xt, Xv

    def _device_tensor(self, name: str, arr: np.ndarray, make):
        """The device copy of ``arr``, kept between ``predict`` calls while the SAME array object is passed (keyed by a
        weak reference to the array, so a freed or replaced array can never be mistaken for a cached one). Treat the
        arrays of a ``SequenceData`` as immutable; ``release()`` drops the copies."""
        import weakref
        hit = self._dev_cache.get(name)
        if hit is not None and hit[0]() is arr:
            return hit[1]
        tensor = make(arr)
        self._dev_cache[name] = (weakref.ref(arr), tensor)
        return tensor

    def _network_module(self):
        """The ``torch.nn.Module`` that holds the weights (the Darts wrapper keeps it in ``.model``)."""
        net = self._net
        return net.model if self.family == "darts" else net

    def _put_network_on_device(self) -> None:
        module = self._network_module()
        if next(module.parameters()).device != self._device:
            module.to(self._device)

    def release(self) -> None:
        """Give the GPU memory back: the cached device features go, the network moves to the CPU, and the CUDA caching
        allocator is emptied. The adapter stays fitted and usable (the next ``predict`` moves the network back).
        Safe on an unfitted adapter and idempotent. Call it when a fit's predictions are done (the grid runs hundreds
        of fits in one process)."""
        import gc
        import torch
        self._dev_cache = {}
        if "_net" in self.__dict__:
            self._network_module().to("cpu")
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _check_predict_input(self, X) -> SequenceData:
        if self.task is None:
            raise RuntimeError(f"{self.family}: predict before fit")
        X = _check_seq(X, "X")
        if X.n_channels != self.n_features:
            raise ValueError(f"X has {X.n_channels} channels, the model was fitted on {self.n_features}")
        if X.lookback != self._lookback:
            raise ValueError(f"X has lookback {X.lookback}, the model was fitted with {self._lookback}")
        return X


# ------------------------------------------------------------------ tsai
class TsaiAdapter(_SequenceAdapter):
    """A tsai network on lookback windows. ``binary`` / ``multiclass``: cross-entropy (``class_weights="balanced"``
    weights each class by n / (k x count) of the training labels); ``regression``: MSE on the standardised label.

    Training = the loop of the earlier ``ml_search.train_classifier``: AdamW (``lr``, ``weight_decay``), OneCycle over
    ``epochs``, gradient clipping at ``grad_clip``, mini-batches of ``batch_size`` (a one-row remainder is merged
    into the others, so a batch has between 2 and max(batch_size, 3) rows), validation loss (UNWEIGHTED
    cross-entropy / MSE) after every epoch, stop after ``patience`` epochs without an improvement of 1e-4 (never before ``min_epochs``, so a stop cannot
    land on OneCycle's warm-up at peak learning rate), the best
    epoch's weights restored. Data and windows live on the GPU (a window is a gather from the resident matrix).

    MiniRocket is a frozen random-kernel feature extractor plus a BatchNorm / Linear head: only the head (zero
    initialised) learns by gradient descent. Its feature biases are data dependent, so before training ``net.fit`` is
    run once on up to 2048 training windows (otherwise the first forward pass would fit them silently on whatever batch
    came first). ``fit_info`` holds val_loss, best_epoch, epochs_run, val_curve, device, trainable_parameters."""

    family = "tsai"
    archs = TSAI_ARCHS

    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None,
            feature_names: Optional[Sequence[str]] = None) -> "TsaiAdapter":
        self._reset()
        Xt, Xv = self._common_checks(X_train, y_train, X_val, y_val, task, feature_names)
        p = self.params
        if p["batch_size"] < 2:
            raise ValueError("tsai: batch_size must be >= 2 (BatchNorm layers need two rows per batch)")
        if p["arch"] in ("tst", "patchtst") and p["width"] % 4:
            raise ValueError(f"tsai: width={p['width']} must be a multiple of 4 (the {p['arch']} has 4 attention heads)")
        if task == "regression" and p["class_weights"] != "none":
            raise ValueError("tsai: class_weights applies to classification only (task is regression)")
        if len(Xt) < 2:
            raise ValueError("tsai: needs at least 2 training samples")
        yt = _as_labels(y_train, task, len(Xt), "y_train")
        yv = _as_labels(y_val, task, len(Xv), "y_val")
        n_classes = _check_classes(task, yt, yv)
        net, device, info, y_mean, y_sd = self._train(Xt, yt, Xv, yv, task, int(seed), n_classes)
        # reached only when the training succeeded
        self._net, self._device, self._lookback = net, device, Xt.lookback
        self._y_mean, self._y_sd = y_mean, y_sd
        self.fit_info, self.n_iter = info, info["best_epoch"]
        self.task, self.seed = task, int(seed)
        self.feature_names = Xt.channels or None
        self.n_features, self.n_classes = Xt.n_channels, n_classes
        return self

    def _train(self, Xt: SequenceData, yt, Xv: SequenceData, yv, task: str, seed: int, n_classes: Optional[int]):
        import torch
        from app.services.tsai_models import TSAIModelService
        p = self.params
        device = _resolve_device(p["device"])
        c_out = {"binary": 2, "multiclass": n_classes, "regression": 1}[task]
        L = Xt.lookback
        offs = torch.arange(-L + 1, 1, device=device)
        y_mean, y_sd = 0.0, 1.0
        if task == "regression":
            y_mean, y_sd = float(yt.mean()), float(yt.std())
            y_sd = y_sd if y_sd > 1e-12 else 1.0
            ytr, yva = ((yt - y_mean) / y_sd).astype(np.float32), ((yv - y_mean) / y_sd).astype(np.float32)
        else:
            ytr, yva = yt.astype(np.int64), yv.astype(np.int64)

        def tensors(X: SequenceData, y, shared=None):
            F = shared if shared is not None else torch.as_tensor(X.features, device=device)
            return F, torch.as_tensor(X.rows, device=device), torch.as_tensor(y, device=device)

        Ft, rt, yt_t = tensors(Xt, ytr)
        Fv, rv, yv_t = tensors(Xv, yva, shared=Ft if Xv.features is Xt.features else None)

        def window(F, rows):
            return F[rows[:, None] + offs[None, :]].permute(0, 2, 1).contiguous()          # (B, C, L)

        with _seeded(seed, device):
            ap = arch_params(p["arch"], p["width"], p["dropout"], L, p["rocket_features"])
            if p["arch"] == "minirocket":
                ap["random_state"] = seed
            svc = TSAIModelService(use_gpu=device.type == "cuda")
            net = svc.create_model(p["arch"], ap, c_in=Xt.n_channels, c_out=c_out, seq_len=L).to(device)
            if p["arch"] == "minirocket":
                k = min(len(rt), ROCKET_FIT_SAMPLE)
                pick = torch.as_tensor(np.random.default_rng(seed).choice(len(rt), k, replace=False), device=device)
                net.fit(window(Ft, rt[pick]))
            if task == "regression":
                lossf = torch.nn.MSELoss()

                def train_loss(out, y):
                    return lossf(out.squeeze(-1), y)
            else:
                w = None
                if p["class_weights"] == "balanced":
                    cnt = np.bincount(ytr, minlength=c_out).astype(np.float64)
                    w = torch.as_tensor(len(ytr) / (c_out * cnt), dtype=torch.float32, device=device)
                ce = torch.nn.CrossEntropyLoss(weight=w)
                train_loss = ce
            val_sum = torch.nn.MSELoss(reduction="sum") if task == "regression" else torch.nn.CrossEntropyLoss(reduction="sum")

            bs = p["batch_size"]
            steps = _n_batches(len(rt), bs)
            opt = torch.optim.AdamW([q for q in net.parameters() if q.requires_grad], lr=p["lr"],
                                    weight_decay=p["weight_decay"])
            sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=p["lr"], total_steps=steps * p["epochs"])
            gen = torch.Generator(device="cpu").manual_seed(seed)
            best, best_loss, best_epoch, bad, curve = None, float("inf"), 0, 0, []
            for ep in range(p["epochs"]):
                net.train()
                perm = torch.randperm(len(rt), generator=gen).to(device)
                for pos in _batch_splits(perm, bs):                # near-equal batches, none of size 1
                    opt.zero_grad(set_to_none=True)
                    loss = train_loss(net(window(Ft, rt[pos])), yt_t[pos])
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(net.parameters(), p["grad_clip"])
                    opt.step()
                    sched.step()
                net.eval()
                vl_sum = torch.zeros((), device=device)
                with torch.no_grad():
                    for i in range(0, len(rv), EVAL_BATCH):
                        out = net(window(Fv, rv[i:i + EVAL_BATCH]))
                        yb = yv_t[i:i + EVAL_BATCH]
                        vl_sum += val_sum(out.squeeze(-1), yb) if task == "regression" else val_sum(out, yb)
                vl = float(vl_sum) / len(rv)                      # the epoch's only device -> host sync
                curve.append(vl)
                if vl < best_loss - 1e-4:
                    best_loss, best_epoch, bad = vl, ep + 1, 0
                    best = {k: v.detach().clone() for k, v in net.state_dict().items()}
                else:
                    bad += 1
                    if bad >= p["patience"] and ep + 1 >= min(p["min_epochs"], p["epochs"]):
                        break
            if best is None:
                raise RuntimeError(f"tsai {p['arch']}: the validation loss was never finite ({curve})")
            net.load_state_dict(best)
            net.eval()
        info = {"val_loss": best_loss, "best_epoch": best_epoch, "epochs_run": len(curve), "val_curve": curve,
                "device": device.type, "arch": p["arch"],
                "trainable_parameters": int(sum(q.numel() for q in net.parameters() if q.requires_grad))}
        return net, device, info, y_mean, y_sd

    def predict(self, X) -> np.ndarray:
        import torch
        X = self._check_predict_input(X)
        dev, net = self._device, self._net
        self._put_network_on_device()
        F = self._device_tensor("features", X.features, lambda a: torch.as_tensor(a, device=dev))
        rows = torch.as_tensor(X.rows, device=dev)
        offs = torch.arange(-X.lookback + 1, 1, device=dev)
        outs = []
        net.eval()
        with torch.no_grad():
            for i in range(0, len(rows), EVAL_BATCH):
                r = rows[i:i + EVAL_BATCH]
                o = net(F[r[:, None] + offs[None, :]].permute(0, 2, 1).contiguous())
                outs.append(o.squeeze(-1).double() * self._y_sd + self._y_mean if self.task == "regression"
                            else torch.softmax(o.double(), 1))
        if not outs:
            return np.zeros((0,)) if self.task == "regression" else np.zeros((0, self.n_classes))
        out = torch.cat(outs).cpu().numpy()
        if not np.isfinite(out).all():
            raise ValueError("tsai: non-finite prediction")
        return out


# ------------------------------------------------------------------ darts
#: levels the Darts likelihood learns; the deciles are percentiles of the SAMPLED H-step cumulative return
DARTS_QUANTILES: Tuple[float, ...] = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
DECILE_PERCENTILES: Tuple[int, ...] = tuple(int(round(100 * q)) for q in QUANTILE_LEVELS)
PREDICT_CHUNK = 2048
#: architectures whose network is called directly on batched windows (verified equal to the public forecast)
BATCHED_ARCHS: Tuple[str, ...] = ("nhits", "nbeats", "tcn")


def _quiet_lightning() -> None:
    """No Lightning banners ("GPU available", "Tip: ... litlogger", "Trainer.fit stopped"): they are INFO records, and
    importing Lightning resets its logger to INFO, so the package is imported FIRST and the levels set after it. The
    Tensor Cores hint and one deprecation notice are warnings and are filtered by message only (the matmul precision
    is the caller's policy, see the module docstring)."""
    import warnings
    import pytorch_lightning  # noqa: F401  (its import sets the logger level)
    for name in ("pytorch_lightning", "lightning", "lightning.pytorch", "lightning_fabric", "darts"):
        logging.getLogger(name).setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", message=r".*has Tensor Cores.*")
    warnings.filterwarnings("ignore", message=r".*isinstance\(treespec, LeafSpec\).*")


def _best_checkpoint(ckpt) -> Tuple[int, float]:
    """(1-based best epoch, best validation loss) of a Lightning ``ModelCheckpoint``; raises rather than guessing when
    Darts / Lightning did not record them or changed the file naming."""
    if ckpt.best_model_score is None:
        raise RuntimeError("darts: the checkpoint callback recorded no best_model_score (no validation loss?)")
    loss = float(ckpt.best_model_score)
    if not math.isfinite(loss):
        raise RuntimeError(f"darts: the best validation loss is not finite ({loss})")
    found = re.search(r"epoch=(\d+)", os.path.basename(str(ckpt.best_model_path)))
    if not found:
        raise RuntimeError(f"darts: cannot read the best epoch from the checkpoint name {ckpt.best_model_path!r} "
                           f"(expected 'epoch=<n>')")
    return int(found.group(1)) + 1, loss


def _remove_dir(path: str) -> None:
    """Delete a temporary checkpoint directory; failures (Windows file locks) are logged, never swallowed silently."""
    errors: List[Tuple[str, BaseException]] = []
    try:
        shutil.rmtree(path, onexc=lambda func, p, exc: errors.append((str(p), exc)))
    except TypeError:                                    # Python < 3.12
        shutil.rmtree(path, onerror=lambda func, p, info: errors.append((str(p), info[1])))
    for p, exc in errors:
        logger.warning("could not remove the temporary checkpoint path %s: %s", p, exc)


def _darts_ts(values: np.ndarray, start: int):
    from darts import TimeSeries
    return TimeSeries.from_times_and_values(pd.RangeIndex(start, start + len(values)), values.astype(np.float32))


def _spans(X: SequenceData, n_out: int, where: str) -> Dict[int, Tuple[int, int]]:
    """Per series: (first row of the earliest window, last row of the latest forecast target) of the sample rows."""
    out: Dict[int, Tuple[int, int]] = {}
    st = X.series_start[X.rows]
    for s in np.unique(st):
        r = X.rows[st == s]
        lo, hi = int(r.min()) - X.lookback + 1, int(r.max()) + n_out
        if hi >= len(X.features) or X.series_start[hi] != s:
            raise ValueError(f"{where}: the {n_out}-bar targets of row {int(r.max())} run past the end of the series "
                             f"(drop the rows whose horizon is not complete)")
        out[int(s)] = (lo, hi)
    return out


def _interp_plan(u: np.ndarray, device):
    """Inverse-CDF plan for the uniform draws ``u`` (samples, steps): for every draw the two neighbouring learned
    quantile levels and the interpolation weight -- linear between levels, flat beyond the outer ones (the
    interpolation Darts' own ``QuantileRegression.sample`` uses). The same plan serves every row (see
    ``DartsAdapter``: common random numbers)."""
    import torch
    levels = np.asarray(DARTS_QUANTILES)
    idx = np.searchsorted(levels, u)
    lo, hi = np.clip(idx - 1, 0, len(levels) - 1), np.clip(idx, 0, len(levels) - 1)
    ql, qh = levels[lo], levels[hi]
    w = np.where(qh > ql, (u - ql) / np.where(qh > ql, qh - ql, 1.0), 0.0)
    step = np.broadcast_to(np.arange(u.shape[1])[None, :], u.shape)
    return (torch.as_tensor(np.ascontiguousarray(step), device=device), torch.as_tensor(lo, device=device),
            torch.as_tensor(hi, device=device), torch.as_tensor(w, dtype=torch.float32, device=device))


def _sample_paths(q, plan):
    """(rows, samples, steps) per-bar log returns drawn from the learned quantiles ``q`` (rows, steps, 11), which
    are first sorted along the level axis (a network's quantile outputs may cross)."""
    import torch
    step, lo, hi, w = plan
    q = torch.sort(q, dim=2).values
    ql, qh = q[:, step, lo], q[:, step, hi]
    return ql + w * (qh - ql)


def _deciles_from_quantiles(q, plan, sd: float):
    """(rows, 9) deciles of the H-step return: every sampled path is summed in log space, mapped with ``expm1`` and
    the nine percentiles of the samples are taken (linear interpolation, as ``np.percentile``)."""
    import torch
    tot = torch.expm1(_sample_paths(q, plan).sum(2) * sd)
    levels = torch.tensor(DECILE_PERCENTILES, dtype=tot.dtype, device=tot.device) / 100.0
    return torch.quantile(tot, levels, dim=1).T


def _darts_model(p: Dict[str, Any], lookback: int, n_out: int, seed: int, device, work_dir: str, name: str):
    from darts.models import NBEATSModel, NHiTSModel, TCNModel, TFTModel
    from darts.utils.likelihood_models import QuantileRegression
    from pytorch_lightning.callbacks import EarlyStopping
    common = dict(input_chunk_length=lookback, output_chunk_length=n_out, batch_size=p["batch_size"],
                  n_epochs=p["epochs"], likelihood=QuantileRegression(quantiles=list(DARTS_QUANTILES)),
                  random_state=seed, optimizer_kwargs={"lr": p["lr"]}, save_checkpoints=True, force_reset=True,
                  model_name=name, work_dir=work_dir,
                  pl_trainer_kwargs={"accelerator": "gpu" if device.type == "cuda" else "cpu", "devices": 1,
                                     "enable_progress_bar": False, "enable_model_summary": False, "logger": False,
                                     "min_epochs": min(p["min_epochs"], p["epochs"]),
                                     "callbacks": [EarlyStopping(monitor="val_loss", patience=p["patience"],
                                                                 min_delta=1e-4)]})
    arch, w, do = p["arch"], p["width"], p["dropout"]
    if arch == "nhits":
        return NHiTSModel(num_stacks=3, num_blocks=1, num_layers=2, layer_widths=2 * w, dropout=do, **common)
    if arch == "nbeats":
        return NBEATSModel(generic_architecture=True, num_stacks=3, num_blocks=1, num_layers=2, layer_widths=2 * w,
                           dropout=do, **common)
    if arch == "tcn":
        return TCNModel(kernel_size=3, num_filters=w, num_layers=None, dilation_base=2, dropout=do, **common)
    if arch == "tft":
        return TFTModel(hidden_size=w, lstm_layers=1, num_attention_heads=4, dropout=do, add_relative_index=True,
                        **common)
    raise ValueError(f"unknown darts arch {arch!r}; known {DARTS_ARCHS}")


class DartsAdapter(_SequenceAdapter):
    """A Darts quantile forecaster (N-HiTS, N-BEATS, TCN or TFT) of the next ``horizon_steps`` bars, as the earlier
    ``ml_search.run_darts_fold``: the TARGET series is the bar log return (divided by its training sd) and the scaled
    feature channels are PAST covariates; the model learns the quantiles of each future bar's return, and a forecast
    draws ``num_samples`` paths from the learned quantiles (inverse CDF, steps independent, as run_darts_fold), sums
    each path to an H-step return (``expm1`` of the summed log returns) and takes the nine decile percentiles.
    ``predict`` = those deciles, shape (n, 9), non-decreasing along each row (percentiles of one sample); ``score`` = P(up) from the deciles (``ba2_common.core.ml_forecast.mapping.p_up_from_deciles``,
    clamped to [0.05, 0.95]), ``score_kind`` "probability".

    Throughput: n-hits / n-beats / tcn run the network directly on batched windows held on the device (no TimeSeries
    per row), and the deciles come from ONE set of uniform draws shared by all rows (common random numbers), computed
    on the device: about 0.01 ms per row, 50x the per-row Darts forecast (tft keeps that slow path: 0.5 ms per row).
    The runner scores ONLY the end-of-day decision rows of a Darts cell (one row per symbol and session), never every
    intraday bar. The tcn needs ``lookback > horizon_steps`` (a Darts rule).

    Input contract beyond ``SequenceData``: ``returns`` and ``horizon_steps`` (= forecast length n_out) are required.
    Darts trains on EVERY window of each series' contiguous span, so ``rows`` only mark the span of decision bars per
    series (first row .. last row) and ``y`` (the realised H-step return) is validated but not trained on. The
    training span's targets run n_out bars past its last row: they must end STRICTLY before the first validation row of
    the same series (the purge; refused in any ordering, also validation before or inside the training block), and every block's last target must exist (refused otherwise). Train and validation
    must cover the same series; a model per symbol is one adapter per symbol. ``fit`` early-stops on the validation
    quantile loss and keeps the best checkpoint; the draws of ``predict`` come from an RNG seeded with (fit seed,
    decision row), so ``predict`` of the same row repeats exactly whatever else is in X.
    ``fit_info``: epochs_run, best_epoch, val_loss (the best validation quantile loss), n_series, target_sd, device."""

    family = "darts"
    archs = DARTS_ARCHS

    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None,
            feature_names: Optional[Sequence[str]] = None) -> "DartsAdapter":
        self._reset()
        Xt, Xv = self._common_checks(X_train, y_train, X_val, y_val, task, feature_names)
        p = self.params
        if p["arch"] == "tft" and p["width"] % 4:
            raise ValueError(f"darts: width={p['width']} must be a multiple of 4 (the tft has 4 attention heads)")
        for name, X in (("X_train", Xt), ("X_val", Xv)):
            if X.returns is None:
                raise ValueError(f"darts: {name} needs the bar returns (SequenceData.returns)")
            if X.horizon_steps is None:
                raise ValueError(f"darts: {name} needs horizon_steps (the forecast length in bars)")
        if Xt.horizon_steps != Xv.horizon_steps:
            raise ValueError(f"darts: horizon_steps differ ({Xt.horizon_steps} vs {Xv.horizon_steps})")
        _as_labels(y_train, "regression", len(Xt), "y_train")
        _as_labels(y_val, "regression", len(Xv), "y_val")
        n_out = Xt.horizon_steps
        if p["arch"] == "tcn" and n_out >= Xt.lookback:
            raise ValueError(f"darts: the tcn needs a lookback longer than the horizon ({Xt.lookback} <= {n_out} bars)")
        sp_t, sp_v = _spans(Xt, n_out, "X_train"), _spans(Xv, n_out, "X_val")
        if set(sp_t) != set(sp_v):
            raise ValueError("darts: X_train and X_val must cover the same series")
        for s in sp_t:
            last_train_row = int(Xt.rows[Xt.series_start[Xt.rows] == s].max())
            first_val_row = int(Xv.rows[Xv.series_start[Xv.rows] == s].min())
            if sp_t[s][1] >= first_val_row:               # any ordering: validation before / inside the training block
                raise ValueError(f"darts: the training targets (last training row {last_train_row} + {n_out} bars = "
                                 f"row {sp_t[s][1]}) reach the validation block (first row {first_val_row}); the "
                                 f"last training row must be at least {n_out + 1} bars before it")
        net, device, info, sd = self._train(Xt, Xv, sp_t, sp_v, n_out, int(seed))
        # reached only when the training succeeded
        self._net, self._device, self._lookback, self._n_out, self._sd = net, device, Xt.lookback, n_out, sd
        self.fit_info, self.n_iter = info, info["best_epoch"]
        self.task, self.seed = task, int(seed)
        self.feature_names = Xt.channels or None
        self.n_features, self.n_classes = Xt.n_channels, None
        return self

    def _train(self, Xt: SequenceData, Xv: SequenceData, sp_t, sp_v, n_out: int, seed: int):
        p = self.params
        _quiet_lightning()
        device = _resolve_device(p["device"])
        keys = sorted(sp_t)
        sd = float(np.concatenate([Xt.returns[sp_t[k][0]:sp_t[k][1] + 1] for k in keys]).std())
        sd = sd if sd > 1e-12 else 1.0
        tr_s = [_darts_ts(Xt.returns[sp_t[k][0]:sp_t[k][1] + 1] / sd, sp_t[k][0]) for k in keys]
        tr_c = [_darts_ts(Xt.features[sp_t[k][0]:sp_t[k][1] + 1], sp_t[k][0]) for k in keys]
        va_s = [_darts_ts(Xv.returns[sp_v[k][0]:sp_v[k][1] + 1] / sd, sp_v[k][0]) for k in keys]
        va_c = [_darts_ts(Xv.features[sp_v[k][0]:sp_v[k][1] + 1], sp_v[k][0]) for k in keys]
        name = f"ba2_{p['arch']}_{id(self)}_{seed}"
        tmp = tempfile.mkdtemp(prefix="ba2_darts_")
        try:
            with _seeded(seed, device):
                model = _darts_model(p, Xt.lookback, n_out, seed, device, tmp, name)
                model.fit(tr_s, past_covariates=tr_c, val_series=va_s, val_past_covariates=va_c, verbose=False)
                epochs_run = int(model.epochs_trained)
                best_epoch, val_loss = _best_checkpoint(model.trainer.checkpoint_callback)
                best = type(model).load_from_checkpoint(name, work_dir=tmp, best=True)   # the best validation epoch
        finally:
            _remove_dir(tmp)
        info = {"epochs_run": epochs_run, "best_epoch": best_epoch, "val_loss": val_loss, "n_series": len(keys),
                "target_sd": sd, "device": device.type, "arch": p["arch"]}
        return best, device, info, sd

    def _check_darts_input(self, X) -> SequenceData:
        X = self._check_predict_input(X)
        if X.returns is None:
            raise ValueError("darts: X needs the bar returns (SequenceData.returns)")
        if X.horizon_steps != self._n_out:
            raise ValueError(f"darts: X has horizon_steps {X.horizon_steps}, the model was fitted with {self._n_out}")
        return X

    def _quantiles_chunk(self, X: SequenceData, rows: np.ndarray, F, R):
        """(len(rows), n_out, 11) learned quantiles of the standardised bar returns, a torch tensor on the device."""
        import torch
        L, n_out = X.lookback, self._n_out
        if self.params["arch"] in BATCHED_ARCHS:
            # the network itself, on batched windows (no TimeSeries objects): the input Darts builds for a model with
            # past covariates is the target window and the covariate window side by side
            from darts.utils.data.torch_datasets.utils import PLModuleInput
            idx = torch.as_tensor(rows, device=self._device)[:, None] + torch.arange(-L + 1, 1, device=self._device)[None, :]
            out = self._net.model(PLModuleInput(past_target=R[idx].unsqueeze(-1), past_covariates=F[idx]))
            return out.prediction.reshape(len(rows), n_out, -1)
        # tft (relative index, static inputs): the public Darts forecast, one TimeSeries pair per row (slow)
        sd = self._sd
        ser = [_darts_ts(X.returns[r - L + 1:r + 1] / sd, int(r) - L + 1) for r in rows]
        cov = [_darts_ts(X.features[r - L + 1:r + 1], int(r) - L + 1) for r in rows]
        preds = self._net.predict(n_out, series=ser, past_covariates=cov, predict_likelihood_parameters=True,
                                  verbose=False, show_warnings=False)
        return torch.as_tensor(np.stack([pr.values(copy=False) for pr in preds]), device=self._device)

    def _device_inputs(self, X: SequenceData):
        import torch
        dev = self._device
        if self.params["arch"] in BATCHED_ARCHS:
            self._put_network_on_device()
            self._net.model.eval()
            return (self._device_tensor("features", X.features, lambda a: torch.as_tensor(a, device=dev)),
                    self._device_tensor("returns", X.returns,
                                        lambda a: torch.as_tensor((a / self._sd).astype(np.float32), device=dev)))
        return None, None

    def _forecast_quantiles(self, X: SequenceData) -> np.ndarray:
        """(len(X), n_out, 11) float32 learned quantiles (unsorted, as the network emits them)."""
        import torch
        X = self._check_darts_input(X)
        F, R = self._device_inputs(X)
        out = []
        with torch.no_grad():
            for i in range(0, len(X), PREDICT_CHUNK):
                out.append(self._quantiles_chunk(X, X.rows[i:i + PREDICT_CHUNK], F, R).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, self._n_out, len(DARTS_QUANTILES)), dtype=np.float32)

    def predict(self, X) -> np.ndarray:
        import torch
        X = self._check_darts_input(X)
        out = np.empty((len(X), len(DECILE_PERCENTILES)))
        if not len(X):
            return out
        sd, ns, n_out = self._sd, self.params["num_samples"], self._n_out
        # ONE set of uniform draws for every row (common random numbers): a row's deciles are a deterministic function
        # of its own forecast quantiles -- independent of the other rows and of the chunking -- and differences
        # between rows are never sampling noise.
        plan = _interp_plan(np.random.default_rng(self.seed).random((ns, n_out)), self._device)
        F, R = self._device_inputs(X)
        with torch.no_grad():
            for i in range(0, len(X), PREDICT_CHUNK):
                rows = X.rows[i:i + PREDICT_CHUNK]
                q = self._quantiles_chunk(X, rows, F, R).float()
                out[i:i + len(rows)] = _deciles_from_quantiles(q, plan, sd).double().cpu().numpy()
        if not np.isfinite(out).all():
            raise ValueError("darts: non-finite prediction")
        return out

    def score(self, X) -> np.ndarray:
        """P(up) of every row from its deciles."""
        from ba2_common.core.ml_forecast.mapping import p_up_from_deciles
        return np.array([p_up_from_deciles(q) for q in self.predict(X)], dtype=np.float64)

    @property
    def score_kind(self) -> str:
        if self.task is None:
            raise RuntimeError("darts: score_kind before fit")
        return "probability"


SEQUENCE_ADAPTERS: Dict[str, type] = {"tsai": TsaiAdapter, "darts": DartsAdapter}
