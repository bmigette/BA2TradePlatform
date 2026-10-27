"""Model search over the point-in-time MAG7 panel (docs/plans/2026-10-27-custom-ml-models-design.md, section 2).

A SETUP (one genome) = timeframe, feature groups, target, pooled or per-symbol, architecture, lookback and the main
hyperparameters. A TRIAL trains the setup on every requested fold (``ml_targets.quarterly_folds``) and stitches the
fold predictions at the daily decision bar (the last bar of each session: the expert decides once a day).

Stage A (``handle_ml_model_search``, task type ``ml_model_search``) runs the GA on the four 2025 folds ONLY: the
2026 folds are never trained, predicted or scored during the search. Stage B (``run_setup`` with all folds and a
``save_dir``) retrains the chosen setups on all seven folds and saves every fold model with its scaler, feature
list and settings.

Fitness (``score_predictions``), on the 2025 stitched predictions, never accuracy:
  * each prediction is ranked by its probability of the favourable outcome (class models: P(up) / (P(up) + P(down)),
    i.e. P(take-profit before stop | a barrier is hit) for the triple barrier; quantile models: P(return > 0) from
    the deciles);
  * the top ``TOP_SHARE`` of each fold are the "signals";
  * a signal's outcome is the trade the daily expert would take: entry at the next session's open, exit at the close
    H sessions after the decision, divided by ATR% x sqrt(H) (volatility units), MINUS the mean of the same quantity
    over the MAG7 on that session (the volatility-matched, same-session control);
  * fitness = mean signal excess - the same statistic for the naive momentum control (20-session return as the
    score) - CAL_WEIGHT x calibration error. A setup that does not beat the naive control scores below zero.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services import ml_panel as MP
from app.services import ml_targets as MT

logger = logging.getLogger(__name__)

TOP_SHARE = 0.20
CAL_WEIGHT = 0.5
#: fitness of a setup whose target is refused by the class-balance check (far below any real score)
REFUSED_FITNESS = -10.0
NAIVE_SCORE_COLUMN = "d_ret_20d"
DECILES = (10, 20, 30, 40, 50, 60, 70, 80, 90)
STAGE_A_FOLDS = ("2025Q1", "2025Q2", "2025Q3", "2025Q4")

TARGETS: Dict[str, MT.TargetSpec] = {
    "tb_h3_r0.5": MT.TargetSpec("triple_barrier", 3, 0.5),
    "tb_h3_r1": MT.TargetSpec("triple_barrier", 3, 1.0),
    "tb_h5_r1": MT.TargetSpec("triple_barrier", 5, 1.0),
    "dir_h1": MT.TargetSpec("direction", 1),
    "dir_h3": MT.TargetSpec("direction", 3),
    "dir_h5": MT.TargetSpec("direction", 5),
    "fwd_h1": MT.TargetSpec("forward_return", 1),
    "fwd_h3": MT.TargetSpec("forward_return", 3),
    "fwd_h5": MT.TargetSpec("forward_return", 5),
}
FEATURE_SETS: Dict[str, Tuple[str, ...]] = {
    "t": ("technical",),
    "td": ("technical", "daily"),
    "tdm": ("technical", "daily", "market"),
    "tdf": ("technical", "daily", "fundamentals"),
    "tdmf": ("technical", "daily", "market", "fundamentals"),
}
TSAI_ARCHS = ("inception", "tst", "lstm_fcn")
DARTS_ARCHS = ("tft", "nhits")

#: GA genes (GeneticOptimizer format). ``tsai_arch`` applies to the class targets, ``darts_arch`` to forward_return.
GENE_SPACE: Dict[str, Dict[str, Any]] = {
    "timeframe": {"type": "choice", "choices": ["1h", "15m"]},
    "features": {"type": "choice", "choices": list(FEATURE_SETS)},
    "target": {"type": "choice", "choices": list(TARGETS)},
    "pooled": {"type": "choice", "choices": [1, 0]},
    "tsai_arch": {"type": "choice", "choices": list(TSAI_ARCHS)},
    "darts_arch": {"type": "choice", "choices": list(DARTS_ARCHS)},
    "lookback": {"type": "choice", "choices": [32, 64, 128]},
    "lr": {"type": "choice", "choices": [3e-4, 1e-3, 3e-3]},
    "width": {"type": "choice", "choices": [32, 64, 128]},
    "dropout": {"type": "choice", "choices": [0.0, 0.1, 0.3]},
    "epochs": {"type": "choice", "choices": [10, 20, 30]},
}


@dataclass(frozen=True)
class Setup:
    timeframe: str
    features: str
    target: str
    pooled: bool
    arch: str
    lookback: int
    lr: float
    width: int
    dropout: float
    epochs: int

    @classmethod
    def from_genes(cls, g: Dict[str, Any]) -> "Setup":
        spec = TARGETS[g["target"]]
        arch = g["tsai_arch"] if spec.is_classification else g["darts_arch"]
        return cls(g["timeframe"], g["features"], g["target"], bool(int(g["pooled"])), arch, int(g["lookback"]),
                   float(g["lr"]), int(g["width"]), float(g["dropout"]), int(g["epochs"]))

    @property
    def spec(self) -> MT.TargetSpec:
        return TARGETS[self.target]

    @property
    def groups(self) -> Tuple[str, ...]:
        return FEATURE_SETS[self.features]

    def key(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def label(self) -> str:
        return (f"{self.timeframe} {self.features} {self.target} {'pooled' if self.pooled else 'per-sym'} {self.arch} "
                f"L{self.lookback} w{self.width} lr{self.lr:g} do{self.dropout:g} ep{self.epochs}")


# ------------------------------------------------------------------ panel cache (per process)
_PANELS: Dict[str, pd.DataFrame] = {}
_PANEL_PATHS: Dict[str, str] = {}
_EXCURSIONS: Dict[Tuple[str, int], Tuple[np.ndarray, np.ndarray]] = {}


def set_panel_paths(paths: Dict[str, str]) -> None:
    _PANEL_PATHS.update(paths)


def get_panel(tf: str) -> pd.DataFrame:
    if tf not in _PANELS:
        if tf not in _PANEL_PATHS:
            raise MP.PanelDataError(f"no panel registered for timeframe {tf!r}")
        p = pd.read_parquet(_PANEL_PATHS[tf])
        p["session"] = pd.to_datetime(p["session"]).dt.date
        _PANELS[tf] = p
    return _PANELS[tf]


def get_excursions(tf: str, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    k = (tf, horizon)
    if k not in _EXCURSIONS:
        _EXCURSIONS[k] = MT.barrier_excursions(get_panel(tf), horizon)
    return _EXCURSIONS[k]


# ------------------------------------------------------------------ evaluation table
def eod_outcomes(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Per (symbol, decision session S): the daily expert's trade outcome -- entry at the open of S+1, exit at the
    close of S+H -- its volatility-unit value and the naive momentum score. NaN past the data end."""
    out = []
    for sym, g in panel.groupby("symbol", sort=False):
        d = g.groupby("session", sort=True).agg(open=("open", "first"), close=("close", "last"))
        eod = g[g["is_eod"]].set_index("session")
        nxt_open = d["open"].shift(-1)
        exit_close = d["close"].shift(-horizon)
        r = exit_close / nxt_open - 1.0
        atr_pct = eod["atr_d"] / eod["close"]
        t = pd.DataFrame({"symbol": sym, "ret": r.reindex(eod.index), "atr_pct": atr_pct,
                          "naive": eod[NAIVE_SCORE_COLUMN] if NAIVE_SCORE_COLUMN in eod else np.nan,
                          "fwd_close": eod[f"fwd_ret_{horizon}"]})
        out.append(t)
    t = pd.concat(out).reset_index().rename(columns={"index": "session"})
    t["z"] = t["ret"] / (t["atr_pct"] * math.sqrt(horizon))
    t["excess"] = t["z"] - t.groupby("session")["z"].transform("mean")
    return t


def _top_excess(df: pd.DataFrame, score: str) -> Tuple[float, int, Dict[str, float]]:
    per_fold, sel_all = {}, []
    for fold, g in df.groupby("fold"):
        g = g.dropna(subset=[score, "excess"])
        if g.empty:
            continue
        thr = g[score].quantile(1.0 - TOP_SHARE)
        sel = g[g[score] >= thr]
        per_fold[fold] = float(sel["excess"].mean())
        sel_all.append(sel)
    if not sel_all:
        return float("nan"), 0, per_fold
    s = pd.concat(sel_all)
    return float(s["excess"].mean()), int(len(s)), per_fold


def calibration_error(preds: pd.DataFrame, is_class: bool, labels: Optional[pd.Series] = None) -> float:
    """Class models: expected calibration error of the top-class probability (10 bins). Quantile models: mean absolute
    gap between each decile's level and the share of realised returns at or below it."""
    if is_class:
        P = preds[["p_down", "p_mid", "p_up"]].to_numpy()
        y = preds["label"].to_numpy()
        ok = ~np.isnan(y)
        P, y = P[ok], y[ok].astype(int)
        conf, pick = P.max(1), P.argmax(1)
        hit = (pick == y).astype(float)
        bins = np.minimum((conf * 10).astype(int), 9)
        e = 0.0
        for b in range(10):
            m = bins == b
            if m.any():
                e += m.mean() * abs(hit[m].mean() - conf[m].mean())
        return float(e)
    y = preds["fwd_close"].to_numpy()
    ok = ~np.isnan(y)
    gaps = [abs((y[ok] <= preds[f"q{l}"].to_numpy()[ok]).mean() - l / 100.0) for l in DECILES]
    return float(np.mean(gaps))


def p_up_deciles(Q: np.ndarray) -> np.ndarray:
    from ba2_common.core.ml_forecast.mapping import p_up_from_deciles
    return np.array([p_up_from_deciles(q) for q in Q])


def score_predictions(preds: pd.DataFrame, setup: Setup) -> Dict[str, Any]:
    """Fitness and its components from stitched predictions (one row per symbol and decision session)."""
    spec = setup.spec
    ev = eod_outcomes(get_panel(setup.timeframe), spec.horizon)
    df = preds.merge(ev, on=["symbol", "session"], how="left", suffixes=("", "_ev"))
    if spec.is_classification:
        df["score"] = df["p_up"] / (df["p_up"] + df["p_down"])
    else:
        df["score"] = p_up_deciles(df[[f"q{l}" for l in DECILES]].to_numpy())
    exc, n_sel, per_fold = _top_excess(df, "score")
    nexc, _, naive_fold = _top_excess(df, "naive")
    cal = calibration_error(df, spec.is_classification)
    fit = exc - nexc - CAL_WEIGHT * cal
    if not math.isfinite(fit):
        fit = REFUSED_FITNESS
    sel_ret = df.loc[df["score"] >= df.groupby("fold")["score"].transform(lambda s: s.quantile(1 - TOP_SHARE)), "ret"]
    return {"fitness": float(fit), "excess": exc, "naive_excess": nexc, "calibration": cal, "n_signals": n_sel,
            "excess_by_fold": per_fold, "naive_by_fold": naive_fold,
            "signal_mean_ret": float(sel_ret.mean()), "all_mean_ret": float(df["ret"].mean()),
            "signal_hit_rate": float((sel_ret > 0).mean())}


# ------------------------------------------------------------------ data for one fold
@dataclass
class FoldData:
    X: np.ndarray                 # (n_rows, C) scaled features (+ symbol one-hot when pooled), float32
    first_ok: Dict[str, int]      # first panel row of each symbol whose features are all finite
    scaler: Dict[str, List[float]]
    channels: List[str]


def _first_finite_rows(panel: pd.DataFrame, feats: Sequence[str]) -> Dict[str, int]:
    ok = np.isfinite(panel[list(feats)].to_numpy(dtype=np.float64)).all(1)
    out = {}
    for sym, g in panel.groupby("symbol", sort=False):
        bad = np.flatnonzero(~ok[g.index.values])
        # non-finite features are allowed in the warm-up only: a contiguous block from the symbol's first row
        if len(bad) and (bad[-1] != len(bad) - 1 or bad[-1] >= len(g) - 1):
            raise MP.PanelDataError(f"{sym}: non-finite features after the warm-up (rows {bad[-3:]})")
        out[sym] = int(g.index.values[len(bad)])
    return out


def fold_data(panel: pd.DataFrame, feats: Sequence[str], fit_rows: np.ndarray, pooled: bool) -> FoldData:
    raw = panel[list(feats)].to_numpy(dtype=np.float64)
    fit = raw[fit_rows]
    med = np.nanmedian(fit, 0)
    iqr = np.nanpercentile(fit, 75, 0) - np.nanpercentile(fit, 25, 0)
    iqr = np.where(iqr > 1e-12, iqr, np.nanstd(fit, 0) + 1e-12)
    X = np.clip((raw - med) / iqr, -5, 5)
    X = np.nan_to_num(X, nan=0.0)            # warm-up rows only; never inside a used window (first_ok)
    channels = list(feats)
    if pooled:
        syms = list(dict.fromkeys(panel["symbol"]))
        oh = (panel["symbol"].to_numpy()[:, None] == np.array(syms)[None, :]).astype(np.float64)
        X = np.concatenate([X, oh], 1)
        channels += [f"sym_{s}" for s in syms]
    return FoldData(X.astype(np.float32), _first_finite_rows(panel, feats),
                    {"median": med.tolist(), "iqr": iqr.tolist()}, channels)


def usable(panel: pd.DataFrame, rows: np.ndarray, lookback: int, first_ok: Dict[str, int]) -> np.ndarray:
    """Rows whose whole lookback window is inside the symbol and past its warm-up."""
    syms = panel["symbol"].to_numpy()[rows]
    start = rows - lookback + 1
    lim = np.array([first_ok[s] for s in syms])
    return rows[start >= lim]


# ------------------------------------------------------------------ tsai classifiers
def _tsai_params(arch: str, s: Setup) -> Dict[str, Any]:
    if arch == "inception":
        return {"nf": s.width // 2, "depth": 6, "ks": min(40, s.lookback)}
    if arch == "tst":
        return {"d_model": s.width, "n_heads": 4, "d_ff": 2 * s.width, "n_layers": 2, "dropout": s.dropout}
    if arch == "lstm_fcn":
        return {"hidden_size": s.width, "rnn_layers": 1, "cell_dropout": s.dropout, "fc_dropout": s.dropout}
    raise ValueError(f"unknown tsai arch {arch!r}")


def train_classifier(s: Setup, X: np.ndarray, y: np.ndarray, fit_rows: np.ndarray, val_rows: np.ndarray,
                     seed: int, device) -> Tuple[Any, Dict[str, Any]]:
    import torch
    from app.services.tsai_models import TSAIModelService
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = TSAIModelService(use_gpu=True).create_model(s.arch, _tsai_params(s.arch, s), c_in=X.shape[1], c_out=3,
                                                       seq_len=s.lookback).to(device)
    Xt = torch.as_tensor(X, device=device)
    yt = torch.as_tensor(np.nan_to_num(y, nan=-1).astype(np.int64), device=device)
    offs = torch.arange(-s.lookback + 1, 1, device=device)

    def batch(idx):
        w = Xt[idx[:, None] + offs[None, :]]           # (B, L, C)
        return w.permute(0, 2, 1).contiguous(), yt[idx]

    bs = 512
    steps = max(1, math.ceil(len(fit_rows) / bs))
    opt = torch.optim.AdamW(model.parameters(), lr=s.lr, weight_decay=1e-2)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=s.lr, total_steps=steps * s.epochs)
    lossf = torch.nn.CrossEntropyLoss()
    g = torch.Generator(device="cpu").manual_seed(seed)
    fit_t = torch.as_tensor(fit_rows, device=device)
    val_t = torch.as_tensor(val_rows, device=device)
    best, best_loss, bad, hist = None, float("inf"), 0, []
    for ep in range(s.epochs):
        model.train()
        perm = fit_t[torch.randperm(len(fit_t), generator=g).to(device)]
        for i in range(steps):
            xb, yb = batch(perm[i * bs:(i + 1) * bs])
            opt.zero_grad(set_to_none=True)
            loss = lossf(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
        model.eval()
        with torch.no_grad():
            vl, n = 0.0, 0
            for i in range(0, len(val_t), 2048):
                xb, yb = batch(val_t[i:i + 2048])
                vl += float(lossf(model(xb), yb)) * len(yb)
                n += len(yb)
        vl /= max(n, 1)
        hist.append(vl)
        if vl < best_loss - 1e-4:
            best_loss, bad = vl, 0
            best = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= 3:
                break
    if best is None:
        raise RuntimeError(f"{s.label()}: validation loss never finite ({hist})")
    model.load_state_dict(best)
    model.eval()
    return model, {"val_loss": best_loss, "epochs_run": len(hist), "val_curve": hist}


def predict_classifier(model, X: np.ndarray, rows: np.ndarray, lookback: int, device) -> np.ndarray:
    import torch
    Xt = torch.as_tensor(X, device=device)
    offs = torch.arange(-lookback + 1, 1, device=device)
    out = []
    with torch.no_grad():
        for i in range(0, len(rows), 2048):
            idx = torch.as_tensor(rows[i:i + 2048], device=device)
            w = Xt[idx[:, None] + offs[None, :]].permute(0, 2, 1).contiguous()
            out.append(torch.softmax(model(w), 1).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 3))


# ------------------------------------------------------------------ Darts quantile forecasters
_QUANTILES = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]


def _darts_model(s: Setup, n_out: int, seed: int):
    from darts.models import NHiTSModel, TFTModel
    from darts.utils.likelihood_models import QuantileRegression
    from pytorch_lightning.callbacks import EarlyStopping
    common = dict(input_chunk_length=s.lookback, output_chunk_length=n_out, batch_size=256, n_epochs=s.epochs,
                  likelihood=QuantileRegression(quantiles=_QUANTILES), random_state=seed,
                  optimizer_kwargs={"lr": s.lr},
                  pl_trainer_kwargs={"accelerator": "gpu", "devices": 1, "enable_progress_bar": False,
                                     "enable_model_summary": False, "logger": False,
                                     "callbacks": [EarlyStopping(monitor="val_loss", patience=3, min_delta=1e-4)]})
    if s.arch == "tft":
        return TFTModel(hidden_size=s.width, lstm_layers=1, num_attention_heads=4, dropout=s.dropout,
                        add_relative_index=True, **common)
    if s.arch == "nhits":
        return NHiTSModel(num_stacks=3, num_blocks=1, num_layers=2, layer_widths=s.width * 2, dropout=s.dropout,
                          **common)
    raise ValueError(f"unknown darts arch {s.arch!r}")


def run_darts_fold(s: Setup, panel: pd.DataFrame, fd: FoldData, fold: MT.Fold, train_rows: np.ndarray,
                   val_start_session, pred_rows: np.ndarray, seed: int) -> Tuple[np.ndarray, Dict[str, Any], Any]:
    """Forecast the next H sessions of bar log returns from the past bars (target) and the features (past
    covariates); the H-session return deciles come from 200 sampled paths. The training series ends before the
    validation block, the validation series before the prediction block, so no target window crosses a boundary."""
    from darts import TimeSeries
    bps = MP.bars_per_session(s.timeframe)
    n_out = s.spec.horizon * bps
    sess = panel["session"].to_numpy()
    close = panel["close"].to_numpy()
    syms = list(dict.fromkeys(panel["symbol"]))
    lr = np.zeros(len(panel))
    for sym in syms:
        idx = np.flatnonzero(panel["symbol"].to_numpy() == sym)
        lr[idx[1:]] = np.diff(np.log(close[idx]))
    first_train = int(train_rows.min())
    fit_mask = (np.isin(np.arange(len(panel)), train_rows)) & (sess < val_start_session)
    sd = float(lr[fit_mask].std()) or 1.0

    def ts(vals, start):
        return TimeSeries.from_times_and_values(pd.RangeIndex(start, start + len(vals)), vals.astype(np.float32))

    def series_for(sym, lo_session, hi_session, extra_back=0):
        idx = np.flatnonzero((panel["symbol"].to_numpy() == sym) & (sess >= lo_session) & (sess < hi_session))
        if len(idx) == 0:
            return None, None
        lo = max(int(idx[0]) - extra_back, fd.first_ok[sym])
        rows = np.arange(lo, int(idx[-1]) + 1)
        return ts(lr[rows] / sd, int(rows[0])), ts(fd.X[rows], int(rows[0]))

    groups = [syms] if s.pooled else [[x] for x in syms]
    deciles = np.full((len(pred_rows), len(DECILES)), np.nan)
    info: Dict[str, Any] = {"target_sd": sd, "n_out": n_out}
    models = {}
    for grp in groups:
        tr_s, tr_c, va_s, va_c = [], [], [], []
        for sym in grp:
            a, c = series_for(sym, panel["session"].iloc[first_train], val_start_session)
            b, d = series_for(sym, val_start_session, fold.pred_start, extra_back=s.lookback)
            if a is None or b is None or len(b) < s.lookback + n_out:
                raise MT.TargetBalanceError(f"{sym}: too few bars for lookback {s.lookback} + horizon {n_out}")
            tr_s.append(a); tr_c.append(c); va_s.append(b); va_c.append(d)
        model = _darts_model(s, n_out, seed)
        model.fit(tr_s, past_covariates=tr_c, val_series=va_s, val_past_covariates=va_c, verbose=False)
        models["+".join(grp)] = model
        mask = np.isin(panel["symbol"].to_numpy()[pred_rows], grp)
        rows = pred_rows[mask]
        ser, cov = [], []
        for r in rows:
            lo = int(r) - s.lookback + 1
            ser.append(ts(lr[lo:r + 1] / sd, lo))
            cov.append(ts(fd.X[lo:r + 1], lo))
        pred = model.predict(n_out, series=ser, past_covariates=cov, num_samples=200, verbose=False,
                             show_warnings=False)
        out = np.empty((len(rows), len(DECILES)))
        for i, p in enumerate(pred):
            paths = p.all_values()[:, 0, :] * sd          # (n_out, samples) log returns
            tot = np.expm1(paths.sum(0))
            out[i] = np.percentile(tot, DECILES)
        deciles[np.flatnonzero(mask)] = out
    return deciles, info, models


# ------------------------------------------------------------------ one setup over folds
def run_setup(s: Setup, fold_names: Sequence[str], seed: int = 42, save_dir: Optional[Path] = None,
              refuse_pred_imbalance: bool = True) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Train and predict ``s`` on the named folds; returns the stitched decision-bar predictions and the per-fold
    info (class shares, fitted target parameters, training curves). Raises TargetBalanceError on a refused fold.

    ``refuse_pred_imbalance`` applies the 15% check to the prediction block too (stage A, 2025 only); stage B
    records the 2026 prediction-block shares without acting on them."""
    import torch
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    panel = get_panel(s.timeframe)
    spec = s.spec
    feats = MP.feature_columns(panel, s.groups)
    folds = {f.name: f for f in MT.quarterly_folds()}
    exc = get_excursions(s.timeframe, spec.horizon) if spec.kind == "triple_barrier" else None
    eod = panel["is_eod"].to_numpy()
    sym_arr = panel["symbol"].to_numpy()
    out, info = [], {"setup": asdict(s), "features": feats, "folds": {}}
    for name in fold_names:
        f = folds[name]
        t0 = time.time()
        tr, pr = MT.fold_rows(panel, f, spec.horizon)
        ft = MT.fit_target(panel, spec, tr, exc)
        pr_eod = pr[eod[pr]]
        shares = ({"train": MT.class_shares(ft.labels, tr), "pred": MT.class_shares(ft.labels, pr)}
                  if spec.is_classification else {})
        if spec.is_classification:
            MT.check_balance(spec, name, {"train": shares["train"], **({"pred": shares["pred"]}
                                                                       if refuse_pred_imbalance else {})})
        fit, val = MT.split_validation(panel, tr, spec.horizon)
        fd = fold_data(panel, feats, fit, s.pooled)
        fit_u = usable(panel, fit[np.isfinite(ft.labels[fit])], s.lookback, fd.first_ok)
        val_u = usable(panel, val[np.isfinite(ft.labels[val])], s.lookback, fd.first_ok)
        pr_u = usable(panel, pr_eod, s.lookback, fd.first_ok)
        if len(pr_u) != len(pr_eod):
            raise MP.PanelDataError(f"{name}: {len(pr_eod) - len(pr_u)} decision bars lack a full lookback window")
        finfo: Dict[str, Any] = {"pred_start": str(f.pred_start), "pred_end": str(f.pred_end), "shares": shares,
                                 "target_params": ft.params, "n_fit": int(len(fit_u)), "n_val": int(len(val_u)),
                                 "n_pred": int(len(pr_u))}
        rec = pd.DataFrame({"symbol": sym_arr[pr_u], "session": panel["session"].to_numpy()[pr_u], "fold": name,
                            "row": pr_u, "label": ft.labels[pr_u]})
        if spec.is_classification:
            probs = np.zeros((len(pr_u), 3))
            groups = [None] if s.pooled else list(dict.fromkeys(sym_arr))
            curves = {}
            for gsym in groups:
                gf = fit_u if gsym is None else fit_u[sym_arr[fit_u] == gsym]
                gv = val_u if gsym is None else val_u[sym_arr[val_u] == gsym]
                gp = np.arange(len(pr_u)) if gsym is None else np.flatnonzero(sym_arr[pr_u] == gsym)
                model, tinfo = train_classifier(s, fd.X, ft.labels, gf, gv, seed, device)
                probs[gp] = predict_classifier(model, fd.X, pr_u[gp], s.lookback, device)
                curves[gsym or "pooled"] = tinfo
                if save_dir is not None:
                    _save_fold_model(save_dir, name, gsym or "pooled", s, feats, fd, ft, model=model)
                del model
                torch.cuda.empty_cache()
            finfo["training"] = curves
            rec["p_down"], rec["p_mid"], rec["p_up"] = probs[:, 0], probs[:, 1], probs[:, 2]
            cr = ft.class_returns[pr_u]
            rec["r_down"], rec["r_mid"], rec["r_up"] = cr[:, 0], cr[:, 1], cr[:, 2]
        else:
            vstart = panel["session"].to_numpy()[val.min()] if len(val) else f.pred_start
            dec, dinfo, models = run_darts_fold(s, panel, fd, f, tr, vstart, pr_u, seed)
            finfo["training"] = dinfo
            for j, l in enumerate(DECILES):
                rec[f"q{l}"] = dec[:, j]
            if save_dir is not None:
                for gname, m in models.items():
                    _save_fold_model(save_dir, name, gname, s, feats, fd, ft, darts_model=m, extra=dinfo)
        finfo["seconds"] = round(time.time() - t0, 1)
        info["folds"][name] = finfo
        out.append(rec)
    return pd.concat(out, ignore_index=True), info


def _save_fold_model(save_dir: Path, fold: str, group: str, s: Setup, feats, fd: FoldData, ft: MT.FoldTarget,
                     model=None, darts_model=None, extra: Optional[Dict] = None) -> None:
    """Provenance: the fold model with its feature list, scaler, target parameters and settings."""
    import torch
    d = Path(save_dir) / fold
    d.mkdir(parents=True, exist_ok=True)
    meta = {"setup": asdict(s), "features": list(feats), "channels": fd.channels, "scaler": fd.scaler,
            "target": asdict(ft.spec), "target_params": ft.params, "group": group, "extra": extra or {}}
    (d / f"{group}.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    if model is not None:
        torch.save(model.state_dict(), d / f"{group}.pt")
    if darts_model is not None:
        darts_model.save(str(d / f"{group}.darts.pt"))


# ------------------------------------------------------------------ stage A job
def quiet_training_logs() -> None:
    for name in ("pytorch_lightning", "lightning", "lightning.pytorch", "lightning_fabric", "darts"):
        logging.getLogger(name).setLevel(logging.ERROR)
    import warnings
    warnings.filterwarnings("ignore")


def _trial(genes: Dict[str, Any], panel_paths: Dict[str, str], out_dir: str, seed: int) -> Dict[str, Any]:
    """One stage-A trial (runs in a worker process)."""
    quiet_training_logs()
    set_panel_paths(panel_paths)
    s = Setup.from_genes(genes)
    t0 = time.time()
    rec: Dict[str, Any] = {"key": s.key(), "setup": asdict(s), "label": s.label()}
    try:
        preds, info = run_setup(s, STAGE_A_FOLDS, seed=seed)
        rec.update(score_predictions(preds, s))
        rec["folds"] = {k: {kk: v[kk] for kk in ("shares", "target_params", "n_fit", "n_pred", "seconds")}
                        for k, v in info["folds"].items()}
        pdir = Path(out_dir) / "predictions"
        pdir.mkdir(parents=True, exist_ok=True)
        preds.to_parquet(pdir / f"{s.key()}.parquet", index=False)
    except MT.TargetBalanceError as e:
        rec.update({"fitness": REFUSED_FITNESS, "refused": str(e)})
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def handle_ml_model_search(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task ``ml_model_search``: the stage-A GA. Payload keys (all required): ``panels`` {timeframe: parquet path},
    ``out_dir``, ``population``, ``generations``, ``early_stop``, ``early_stop_min_rel``, ``seed``, ``parallel``,
    ``top_n``. Writes ``trials.jsonl`` (every trial), ``predictions/<key>.parquet`` and ``result.json``."""
    from app.services.genetic import GeneticOptimizer
    for k in ("panels", "out_dir", "population", "generations", "early_stop", "early_stop_min_rel", "seed",
              "parallel", "top_n"):
        if k not in payload:
            raise ValueError(f"ml_model_search payload is missing {k!r}")
    out = Path(payload["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    panels = {tf: str(p) for tf, p in payload["panels"].items()}
    for tf in GENE_SPACE["timeframe"]["choices"]:
        if tf not in panels or not Path(panels[tf]).exists():
            raise MP.PanelDataError(f"panel for {tf} missing: {panels.get(tf)}")
    seed = int(payload["seed"])
    random.seed(seed)
    np.random.seed(seed)
    ga = GeneticOptimizer(param_ranges=GENE_SPACE, population_size=int(payload["population"]),
                          n_generations=int(payload["generations"]),
                          early_stopping_generations=int(payload["early_stop"]),
                          early_stopping_min_rel=float(payload["early_stop_min_rel"]), elitism_percent=10.0)
    memo: Dict[str, Dict[str, Any]] = {}
    log_p = out / "trials.jsonl"
    if log_p.exists():
        for line in log_p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            memo[r["key"]] = r
    pool = ProcessPoolExecutor(max_workers=int(payload["parallel"]))
    gen_no = {"g": 0}

    def progress(msg: str) -> None:
        try:
            from app.services.task_queue import get_task_queue
            get_task_queue().update_progress(task_id, min(99.0, 100.0 * gen_no["g"] / int(payload["generations"])), msg)
        except Exception as e:     # the CLI path has no queue row
            logger.debug(f"progress not recorded: {e}")
        logger.warning(msg)

    def batch_fitness(param_dicts: List[Dict[str, Any]]) -> List[float]:
        keys = [Setup.from_genes(g).key() for g in param_dicts]
        todo = {}
        for k, g in zip(keys, param_dicts):
            if k not in memo and k not in todo:
                todo[k] = g
        futs = {k: pool.submit(_trial, g, panels, str(out), seed) for k, g in todo.items()}
        for k, fu in futs.items():
            r = fu.result()
            r["generation"] = gen_no["g"]
            memo[k] = r
            with open(log_p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r, default=str) + "\n")
            progress(f"gen {gen_no['g']} trial {r['label']}: fitness {r['fitness']:.4f}"
                     + (f" REFUSED ({r['refused'][:80]})" if "refused" in r else
                        f" (excess {r['excess']:.4f} vs naive {r['naive_excess']:.4f}, cal {r['calibration']:.3f}, "
                        f"{r['seconds']}s)"))
        return [memo[k]["fitness"] for k in keys]

    def on_gen(g: int) -> None:
        gen_no["g"] = g

    try:
        res = ga.optimize(lambda p: batch_fitness([p])[0], on_generation_start=on_gen, batch_fitness=batch_fitness)
    finally:
        pool.shutdown(wait=True)
    ranked = sorted((r for r in memo.values() if "refused" not in r), key=lambda r: -r["fitness"])
    result = {"best": ranked[0] if ranked else None, "top": ranked[:int(payload["top_n"])],
              "generations_run": res.get("generations_run"), "n_trials": len(memo),
              "n_refused": sum(1 for r in memo.values() if "refused" in r),
              "history": [{"generation": h["generation"], "best_fitness": h["best_fitness"]} for h in ga.history]}
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    progress(f"ml_model_search done: {len(memo)} trials, best {result['best']['fitness'] if ranked else 'none'}")
    return result
