"""Input construction and block fits of the GPU families of the stage-1 grid (``ml_grid``: ``tsai-<arch>`` and
``darts-<arch>``; see "GPU families" in its docstring).

tsai (``fit_block_seq`` with a ``tsai-*`` family)
    * The 1h INTRADAY panel of the cell's symbols (``ml_panel.build_panel``: regular session, bars stamped at their END,
      technical + daily-context + market features, every feature causal) is built once per process
      (``get_intraday``, an LRU of ``INTRADAY_CACHE_SIZE``). It starts at the first day every symbol (and SPY / QQQ,
      the market context) has 5-minute bars and ends at ``min(eval_end, the daily panel's last session)``.
    * The block's DAILY decision rows come from the same code as a tabular cell (``ml_grid.prepare_block``: purged
      training rows, the train-tail validation split, class-balance check). A daily row is the decision at the session's
      close; its target looks ahead from there.
    * TRAIN samples: for the fit sessions, ``ml_panel.sample_bars(per_session, seed)`` bars of each (symbol, session) (the
      end-of-day bar always included), each labelled with the DAILY target of its session. An intraday bar's features end
      at or before the session's close and its label starts after it, so the label is always in the bar's future; and
      every fit row's target ended before the block (the daily purge). VALIDATION samples: the end-of-day bars of the
      validation (train-tail) sessions. PREDICTION: ONLY the end-of-day bar of each scorable test session, i.e. one row
      per (symbol, session), exactly as the daily rows of the tabular families.
    * Scaling (median / IQR, +-5) is fitted on the training samples only; a symbol id channel is appended.

darts (``fit_block_seq`` with a ``darts-*`` family)
    * The daily store frame: the cell's groups of ``panel.X`` (robust-scaled on the fit rows, NaN as 0) are the past
      covariates, the daily log return of the close is the target series (``daily_sequence_data``); one series per
      symbol, a global model over all of them. The forecast length is the cell's horizon in daily bars; a row's score is
      P(up) of the sampled H-day return (``DartsAdapter.score``). The adapter trains on every window of each series'
      span between the first fit row and the last fit row + horizon; the purge guarantees those targets end before the
      validation block (the adapter refuses otherwise).

Both: epochs are capped by ``GridSettings.gpu_max_epochs`` (early stopping on the train-tail validation block only,
never on test data), the seed goes to python / numpy / torch and to ``sample_bars``, the adapter is ``release()``d after
every block (also when the fit fails), and the fit time, device, epochs and sample counts of every block are recorded.
"""
from __future__ import annotations

import gc
import logging
import random
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.services import ml_grid as G
from app.services import ml_models as M
from app.services import ml_models_seq as MS
from app.services import ml_panel as MP

logger = logging.getLogger(__name__)

INTRADAY_TIMEFRAME = "1h"
INTRADAY_GROUPS: Tuple[str, ...] = ("technical", "daily", "market")
#: intraday panels (~1 GB for 20 symbols over 8 years of 1h bars with features) a worker keeps
INTRADAY_CACHE_SIZE = 2


# ================================================================== the intraday panel
@dataclass
class IntradayPanel:
    """The long 1h panel (one row per symbol and bar, ``RangeIndex`` = the positional row the windows are cut from)."""
    df: pd.DataFrame
    feats: List[str]
    first_day: date
    last_day: date
    seconds: float
    _eod: Optional[Tuple[Any, np.ndarray]] = None
    _daily_of: Optional[Tuple[Any, np.ndarray]] = None

    def eod_rows(self, panel: "G.Panel") -> np.ndarray:
        """Per row of the daily ``panel``: the positional row of its (symbol, session) end-of-day bar here, -1 when the
        intraday panel has none (before its first day, or a missing session)."""
        if self._eod is None or self._eod[0] is not panel:
            e = self.df.loc[self.df["is_eod"], ["symbol", "session"]]
            key = pd.MultiIndex.from_arrays([e["symbol"].to_numpy(), pd.to_datetime(e["session"].to_numpy())],
                                            names=["symbol", "session"])
            ser = pd.Series(e.index.to_numpy(dtype=np.int64), index=key)
            self._eod = (panel, ser.reindex(panel.index).fillna(-1).to_numpy(dtype=np.int64))
        return self._eod[1]

    def daily_of(self, panel: "G.Panel") -> np.ndarray:
        """Per row of this panel: the row of the daily ``panel`` of its (symbol, session), -1 when there is none."""
        if self._daily_of is None or self._daily_of[0] is not panel:
            key = pd.MultiIndex.from_arrays([self.df["symbol"].to_numpy(),
                                             pd.to_datetime(self.df["session"].to_numpy())],
                                            names=["symbol", "session"])
            self._daily_of = (panel, panel.index.get_indexer(key).astype(np.int64))
        return self._daily_of[1]


_INTRADAY: "OrderedDict[Tuple, IntradayPanel]" = OrderedDict()


def intraday_span(panel: "G.Panel", st: "G.GridSettings") -> Tuple[date, date, Tuple]:
    """(first day, last day, file stamps) of the intraday panel of ``panel``'s symbols: from the first day every symbol and
    the context symbols have 5-minute bars to ``min(eval_end, the last cached day, the daily panel's last session)``."""
    names = tuple(dict.fromkeys(tuple(panel.symbols) + MP.CONTEXT_SYMBOLS))
    firsts, lasts, stamps = [], [], []
    for s in names:
        f = MP._cache_file(Path(panel.cache_root), s)                  # PanelDataError when the cache lacks the symbol
        dates = pd.read_parquet(f, columns=["Date"])["Date"]
        firsts.append(dates.min().date())
        lasts.append(dates.max().date())
        stamps.append((s, f.stat().st_size, f.stat().st_mtime_ns))
    last = min(min(lasts), st.eval_end, panel.sessions.max().date())
    return max(firsts), last, tuple(stamps)


def get_intraday(panel: "G.Panel", st: "G.GridSettings") -> IntradayPanel:
    """Per-process LRU of the intraday panel of the cell's symbols (keyed by the file stamps and the span)."""
    first, last, stamps = intraday_span(panel, st)
    key = (str(panel.cache_root), tuple(panel.symbols), first, last, stamps)
    if key in _INTRADAY:
        _INTRADAY.move_to_end(key)
        return _INTRADAY[key]
    t0 = time.perf_counter()
    spec = MP.PanelSpec(INTRADAY_TIMEFRAME, first, last, tuple(panel.symbols), INTRADAY_GROUPS)
    df, meta = MP.build_panel(spec, Path(panel.cache_root))
    ip = IntradayPanel(df, list(meta["features"]), first, last, time.perf_counter() - t0)
    _INTRADAY[key] = ip
    while len(_INTRADAY) > INTRADAY_CACHE_SIZE:
        _INTRADAY.popitem(last=False)
    return ip


# ================================================================== the darts input
def daily_sequence_data(panel: "G.Panel", cols: List[int], fit_rows: np.ndarray, lookback: int,
                        horizon: int) -> MS.SequenceData:
    """``SequenceData`` of the daily store frame: the ``cols`` of ``panel.X`` robust-scaled on ``fit_rows`` only (median /
    IQR, clipped at +-5, NaN -> 0), the daily log return of the close as the target series (0 where there is no print),
    one series per symbol (``series_start`` = the symbol's first usable row). Rows are the panel's own (symbol-major)."""
    X = panel.X[:, cols].astype(np.float32)
    fit = X[np.asarray(fit_rows, dtype=np.int64)].astype(np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)               # an all-NaN column in the fit rows: neutral channel
        med = np.nanmedian(fit, axis=0)
        q75, q25 = np.nanpercentile(fit, [75, 25], axis=0)
        sd = np.nanstd(fit, axis=0)
    iqr = np.where(q75 - q25 > 1e-12, q75 - q25, sd + 1e-12)
    med = np.where(np.isfinite(med), med, 0.0)
    iqr = np.where(np.isfinite(iqr) & (iqr > 0), iqr, 1.0)
    X = np.clip((X - med.astype(np.float32)) / iqr.astype(np.float32), -5, 5)
    X = np.nan_to_num(X, nan=0.0, posinf=5.0, neginf=-5.0).astype(np.float32)
    close = np.asarray(panel.arrays.close, dtype=np.float64)           # (sessions, symbols)
    with np.errstate(all="ignore"):
        lr = np.log(close)
        ret = np.zeros_like(lr)
        ret[1:] = lr[1:] - lr[:-1]
    ret[~np.isfinite(ret)] = 0.0
    n_s, n_sym = close.shape
    ok = np.asarray(panel.ok, dtype=bool).reshape(n_sym, n_s)
    first = np.where(ok.any(1), ok.argmax(1), n_s) + np.arange(n_sym) * n_s
    start = np.repeat(first, n_s).astype(np.int64)
    channels = tuple(panel.columns[i] for i in cols)
    return MS.SequenceData(X, np.empty(0, dtype=np.int64), lookback, start, ret.T.reshape(-1), channels,
                           {"median": med.tolist(), "iqr": iqr.tolist()}, int(horizon))


# ================================================================== one block
def _usable(data: MS.SequenceData, rows: np.ndarray) -> np.ndarray:
    """Bool mask of the sample ``rows`` whose whole window lies inside their own series."""
    return (rows - data.lookback + 1) >= data.series_start[rows]


def _adapter_params(cell: "G.Cell", st: "G.GridSettings") -> Tuple[str, Dict[str, Any]]:
    base, arch = G.split_family(cell.family)
    p = {**cell.params_dict, "arch": arch}
    cap = p["epochs"] if "epochs" in p else M.DEFAULTS[base]["epochs"]
    p["epochs"] = min(int(cap), st.gpu_max_epochs)
    return base, p


def seed_everything(seed: int) -> None:
    """Seed python and numpy (the adapters seed torch and restore the generators they touched)."""
    random.seed(seed)
    np.random.seed(seed % (2 ** 32))
    try:
        import torch
        torch.manual_seed(seed)
    except ImportError:                                                # pragma: no cover  (torch is a requirement)
        pass


def fit_block_seq(cell: "G.Cell", seed: int, panel: "G.Panel", br: "G.BlockRows", block: Any, window: Any,
                  st: "G.GridSettings", n_jobs: int) -> "G.BlockResult":
    """Fit the cell's deep family on the purged window before ``block`` and score the block's daily rows."""
    import torch
    torch.set_num_threads(max(1, int(n_jobs)))
    seed_everything(seed)
    base, params = _adapter_params(cell, st)
    adapter = M.make_adapter(base, n_jobs, params)
    t0 = time.perf_counter()
    try:
        if base == "tsai":
            rows, scores, extra = _tsai_block(cell, seed, panel, br, st, adapter)
        else:
            rows, scores, extra = _darts_block(cell, seed, panel, br, st, adapter)
    finally:
        adapter.release()
        gc.collect()
    info = adapter.fit_info if adapter.fit_info is not None else {}
    audit = {**G.block_audit(br, block, window), **extra, "fit_seconds": time.perf_counter() - t0,
             "device": info["device"] if "device" in info else None,
             "epochs_run": info["epochs_run"] if "epochs_run" in info else None,
             "best_epoch": info["best_epoch"] if "best_epoch" in info else None,
             "val_loss": info["val_loss"] if "val_loss" in info else None}
    return G.BlockResult(rows, scores, extra["n_fit"], extra["n_val"], None, audit)


def _tsai_block(cell, seed, panel, br, st, adapter) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    ip = get_intraday(panel, st)
    L = st.seq_lookback
    eod, dof = ip.eod_rows(panel), ip.daily_of(panel)
    is_fit = np.zeros(len(panel.index), dtype=bool)
    is_fit[br.fit_idx] = True
    cand = np.flatnonzero((dof >= 0) & is_fit[np.maximum(dof, 0)])          # the intraday bars of the fit sessions
    if not len(cand):
        raise G.SkipBlock("no intraday bars in the training sessions (the 1h panel starts later)")
    sampled = MP.sample_bars(ip.df.iloc[cand], st.seq_samples_per_session, int(seed) % (2 ** 31))
    rows_t = sampled.index.to_numpy(dtype=np.int64)
    y_t = br.y_all[dof[rows_t]]
    rows_v, y_v = eod[br.val_idx], br.y_all[br.val_idx]
    keep_v = rows_v >= 0
    rows_v, y_v = rows_v[keep_v], y_v[keep_v]
    rows_p_all = eod[br.pr]
    keep_p = rows_p_all >= 0
    data = MS.sequence_data_from_panel(ip.df, ip.feats, rows_t, L, pooled=True)
    mt, mv = _usable(data, rows_t), _usable(data, rows_v)
    rows_p, pr = rows_p_all[keep_p], br.pr[keep_p]
    mp = _usable(data, rows_p)
    if mt.sum() < 2 or not mv.any():
        raise G.SkipBlock(f"too few usable intraday samples (train {int(mt.sum())}, validation {int(mv.sum())})")
    if not mp.any():
        raise G.SkipBlock("no end-of-day bar to score in the block")
    Xt, Xv, Xp = data.take(rows_t[mt]), data.take(rows_v[mv]), data.take(rows_p[mp])
    adapter.fit(Xt, y_t[mt], Xv, y_v[mv], br.ks.task, int(seed))
    scores = adapter.score(Xp)
    extra = {"n_fit": int(len(Xt)), "n_val": int(len(Xv)), "n_samples": int(len(Xt)),
             "n_fit_sessions": int(is_fit.sum()), "n_channels": int(data.n_channels), "lookback": L,
             "samples_per_session": st.seq_samples_per_session, "intraday_first": str(ip.first_day),
             "intraday_last": str(ip.last_day)}
    return pr[mp], scores, extra


def _darts_block(cell, seed, panel, br, st, adapter) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    H, L = cell.horizon, st.darts_lookback
    cols = panel.group_columns(cell.groups)
    data = daily_sequence_data(panel, cols, br.fit_idx, L, H)
    mt, mv, mp = _usable(data, br.fit_idx), _usable(data, br.val_idx), _usable(data, br.pr)
    if mt.sum() < 2 or not mv.any():
        raise G.SkipBlock(f"too few usable daily windows (train {int(mt.sum())}, validation {int(mv.sum())})")
    if not mp.any():
        raise G.SkipBlock("no scorable daily window in the block")
    fit_rows, val_rows = br.fit_idx[mt], br.val_idx[mv]
    Xt, Xv, Xp = data.take(fit_rows), data.take(val_rows), data.take(br.pr[mp])
    adapter.fit(Xt, br.y_all[fit_rows], Xv, br.y_all[val_rows], "quantile", int(seed))
    scores = adapter.score(Xp)
    extra = {"n_fit": int(len(Xt)), "n_val": int(len(Xv)), "n_samples": int(len(Xt)), "n_channels": int(data.n_channels),
             "lookback": L, "horizon_steps": H, "n_series": int(len(np.unique(Xt.series_start[Xt.rows])))}
    return br.pr[mp], scores, extra
