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
fitted on the fold's training rows only, optional symbol one-hot, warm-up windows dropped).

Determinism. Every random draw is seeded (weights, shuffling, dropout, MiniRocket kernels, Darts sampling) inside a
``fork_rng`` scope, so the global torch / numpy generators are left as they were. On the GPU the result is bitwise
repeatable only with ``torch.backends.cudnn.deterministic = True`` (and ``benchmark = False``): the adapters do not
change those global flags, the caller does.
"""
from __future__ import annotations

import contextlib
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services.ml_models import (QUANTILE_LEVELS, SUPPORTED_TASKS, TASKS, ModelAdapter, UnsupportedTaskError,
                                    _as_labels, _check_classes, _is_int)

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
                             rows=None, with_returns: bool = False,
                             horizon_steps: Optional[int] = None) -> SequenceData:
    """``SequenceData`` of a long panel (one block per symbol, ``panel.index`` = 0..n-1, as ``ml_panel.build_panel``
    returns it). Scaling (median / IQR, clipped at +-5) is fitted on ``fit_rows`` only; ``pooled`` appends a symbol
    one-hot. ``rows`` are the candidate samples: those whose window reaches back into the indicator warm-up or the
    previous symbol are DROPPED (index the labels with ``result.rows``). ``with_returns`` adds the bar log returns of
    ``close`` (the Darts target series); ``horizon_steps`` is stored for Darts."""
    from app.services import ml_search
    if not panel.index.equals(pd.RangeIndex(len(panel))):
        raise ValueError("the panel needs its positional RangeIndex (pass the full panel, not a sample_bars subset)")
    fd = ml_search.fold_data(panel, list(feats), np.asarray(fit_rows, dtype=np.int64), pooled)
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
    ``epochs``, gradient clipping at ``grad_clip``, mini-batches of ``batch_size``, validation loss (UNWEIGHTED
    cross-entropy / MSE) after every epoch, stop after ``patience`` epochs without an improvement of 1e-4, the best
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
            steps = max(1, math.ceil(len(rt) / bs))
            opt = torch.optim.AdamW([q for q in net.parameters() if q.requires_grad], lr=p["lr"],
                                    weight_decay=p["weight_decay"])
            sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=p["lr"], total_steps=steps * p["epochs"])
            gen = torch.Generator(device="cpu").manual_seed(seed)
            best, best_loss, best_epoch, bad, curve = None, float("inf"), 0, 0, []
            for ep in range(p["epochs"]):
                net.train()
                perm = torch.randperm(len(rt), generator=gen).to(device)
                for pos in torch.tensor_split(perm, steps):          # near-equal batches, none of size 1
                    opt.zero_grad(set_to_none=True)
                    loss = train_loss(net(window(Ft, rt[pos])), yt_t[pos])
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(net.parameters(), p["grad_clip"])
                    opt.step()
                    sched.step()
                net.eval()
                vl = 0.0
                with torch.no_grad():
                    for i in range(0, len(rv), EVAL_BATCH):
                        out = net(window(Fv, rv[i:i + EVAL_BATCH]))
                        yb = yv_t[i:i + EVAL_BATCH]
                        vl += float(val_sum(out.squeeze(-1), yb) if task == "regression" else val_sum(out, yb))
                vl /= len(rv)
                curve.append(vl)
                if vl < best_loss - 1e-4:
                    best_loss, best_epoch, bad = vl, ep + 1, 0
                    best = {k: v.detach().clone() for k, v in net.state_dict().items()}
                else:
                    bad += 1
                    if bad >= p["patience"]:
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
        F = torch.as_tensor(X.features, device=dev)
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


SEQUENCE_ADAPTERS: Dict[str, type] = {"tsai": TsaiAdapter}
