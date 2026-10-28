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
      covariates (``daily_sequence_data``); one series per symbol, a global model over all of them. The TARGET series
      depends on the cell's target kind: ``rel_return`` (selection) models the MARKET-NEUTRAL series, the symbol's daily
      log return minus the same-session mean log return of the universe members DECIDABLE at that session (listed, not
      filled, an ATR available: the membership ``ml_targets`` uses for ``rel_ret``; 0 where fewer than
      ``MIN_SESSION_MEMBERS`` members), so P(up) means "beats the universe" and a shock common to every symbol cancels;
      ``quantile`` (timing) keeps the symbol's own log return. The forecast length is the cell's horizon in daily bars; a
      row's score is P(up) of the sampled H-day return (``DartsAdapter.score``). The adapter trains on every window of each series'
      span between the first fit row and the last fit row + horizon; the purge guarantees those targets end before the
      validation block (the adapter refuses otherwise).

Defaults and limits (``GridSettings``; the values enter a GPU row's identity, so changing one makes a resume refuse the
old rows). ``seq_lookback`` 35 one-hour bars (5 sessions), ``seq_samples_per_session`` 4 bars per (symbol, session) of a
training session, ``darts_lookback`` 20 daily bars, ``gpu_max_epochs`` 15, ``gpu_budget_minutes`` 20 per (cell, seed) (a
run setting, checked between blocks: a fit that is already running is never interrupted).
Blocks the intraday panel cannot serve: the real 5-minute cache starts on 2019-01-02 and the daily-context features
need a 200-session warm-up, so the first usable 1h bar is around November 2019. A tsai block whose training sessions
have no such bars is skipped (``SkipBlock``), and a block's test sessions without one are not scored: the 2019 blocks
of a tsai cell therefore score few or no rows and lower its block coverage. (darts reads the daily store, which starts
in 2016, and has no such gap.)
``n_iter`` of a GPU block is None: epochs are not boosting rounds, so ``low_iter_share`` is None and the near-constant
flag of the report never applies to these cells; the epochs are in the block audit (``epochs_run``, ``best_epoch``,
``val_loss``).

Both: the process-wide cuDNN flags are set deterministic (``cudnn.deterministic``, no ``benchmark``, deterministic
algorithms with ``warn_only``) before every block (the adapters leave them to the caller); TF32 stays at PyTorch's
defaults. That makes a rerun as repeatable as the kernels allow, NOT bitwise repeatable: an op without a deterministic
CUDA kernel (darts-nhits' interpolation backward is one) still varies between runs. The block audit records it
(``gpu_nondeterministic``, ``nondeterministic_ops``, from torch's warning), and so does the row.
Epochs are capped by ``GridSettings.gpu_max_epochs`` (early stopping on the train-tail validation block only, never on
test data), the seed goes to python / numpy / torch and to ``sample_bars``, the adapter is ``release()``d after
every block (also when the fit fails), and the fit time, device, epochs and sample counts of every block are recorded.
"""
from __future__ import annotations

import gc
import hashlib
import logging
import re
import os
import random
import time
import warnings
import weakref
from collections import OrderedDict
from functools import lru_cache
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
from app.services import ml_search
from app.services import ml_targets as T

logger = logging.getLogger(__name__)

INTRADAY_TIMEFRAME = "1h"
INTRADAY_GROUPS: Tuple[str, ...] = ("technical", "daily", "market")
#: intraday panels (~1 GB for 20 symbols over 8 years of 1h bars with features) a worker keeps: ONE (a GPU worker runs
#: one cell at a time, and tasks are grouped by panel)
INTRADAY_CACHE_SIZE = 1


# ================================================================== failures on the GPU
#: message fragments (lower case) of an error that leaves the CUDA context unusable for the rest of the process
STICKY_CUDA_MARKERS: Tuple[str, ...] = ("cuda error", "cudnn", "cublas", "device-side assert")


def classify_gpu_error(exc: BaseException) -> Optional[str]:
    """``"oom"`` for an out-of-memory error (recoverable: the next task may fit), ``"sticky"`` for one that poisons the
    CUDA context ("CUDA error", cuDNN, cuBLAS, a device-side assert: every later call in the process fails the same
    way), None for anything else. The exception chain (``__cause__`` / ``__context__``) is searched, so an error wrapped
    by a library is still found; an out-of-memory error wins over a sticky marker in the same chain."""
    chain: List[BaseException] = []
    e: Optional[BaseException] = exc
    while e is not None and all(e is not x for x in chain):
        chain.append(e)
        e = e.__cause__ or e.__context__
    for x in chain:
        if type(x).__name__ == "OutOfMemoryError" or "out of memory" in str(x).lower():
            return "oom"
    for x in chain:
        text = str(x).lower()
        if any(m in text for m in STICKY_CUDA_MARKERS):
            return "sticky"
    return None


def cleanup_gpu() -> None:
    """Collect garbage (a failed task's traceback frames hold tensors until the exception is gone) and hand the CUDA
    caching allocator's blocks back. Call it OUTSIDE the ``except`` block that handled the failure."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                                                  # noqa: BLE001  (cleanup must never raise)
        logger.warning("could not empty the CUDA cache", exc_info=True)


def _release_quietly(adapter: Any) -> None:
    """``adapter.release()`` that can never mask the error being handled: a failure is logged, not raised."""
    try:
        adapter.release()
    except Exception:                                                  # noqa: BLE001
        logger.warning("releasing the %s adapter failed", adapter.family, exc_info=True)


# ================================================================== the intraday panel
@dataclass
class IntradayPanel:
    """The long 1h panel (one row per symbol and bar, ``RangeIndex`` = the positional row the windows are cut from)."""
    df: pd.DataFrame
    feats: List[str]
    first_day: date
    last_day: date
    seconds: float
    # the arrays below belong to ONE daily panel, held WEAKLY: an evicted daily panel must be freeable (~100-200 MB)
    _eod: Optional[Tuple["weakref.ref", np.ndarray]] = None
    _daily_of: Optional[Tuple["weakref.ref", np.ndarray]] = None

    def eod_rows(self, panel: "G.Panel") -> np.ndarray:
        """Per row of the daily ``panel``: the positional row of its (symbol, session) end-of-day bar here, -1 when the
        intraday panel has none (before its first day, or a missing session)."""
        if self._eod is None or self._eod[0]() is not panel:
            e = self.df.loc[self.df["is_eod"], ["symbol", "session"]]
            key = pd.MultiIndex.from_arrays([e["symbol"].to_numpy(), pd.to_datetime(e["session"].to_numpy())],
                                            names=["symbol", "session"])
            ser = pd.Series(e.index.to_numpy(dtype=np.int64), index=key)
            self._eod = (weakref.ref(panel), ser.reindex(panel.index).fillna(-1).to_numpy(dtype=np.int64))
        return self._eod[1]

    def daily_of(self, panel: "G.Panel") -> np.ndarray:
        """Per row of this panel: the row of the daily ``panel`` of its (symbol, session), -1 when there is none."""
        if self._daily_of is None or self._daily_of[0]() is not panel:
            key = pd.MultiIndex.from_arrays([self.df["symbol"].to_numpy(),
                                             pd.to_datetime(self.df["session"].to_numpy())],
                                            names=["symbol", "session"])
            self._daily_of = (weakref.ref(panel), panel.index.get_indexer(key).astype(np.int64))
        return self._daily_of[1]


_INTRADAY: "OrderedDict[Tuple, IntradayPanel]" = OrderedDict()


@lru_cache(maxsize=256)
def _file_span(path: str, size: int, mtime_ns: int) -> Tuple[date, date]:
    """(first, last) day of the bars of one 5-minute cache file, cached on its size and mtime: the span is asked for at
    every block, the files are read once."""
    dates = pd.read_parquet(path, columns=["Date"])["Date"]
    return dates.min().date(), dates.max().date()


def intraday_bounds(cache_root: Any, symbols: Any, eval_end: date, store_last: date) -> Tuple[date, date, Tuple]:
    """(first day, last day, file stamps) of the intraday panel of ``symbols``: from the first day every symbol and the
    context symbols (SPY, QQQ) have 5-minute bars to ``min(eval_end, the last cached day, store_last)``: the store's last
    day (``store_span(...)[1]``), the same value the master's resume check and the identity use. The stamps (size, mtime) only key caches: they are never part of an identity."""
    names = tuple(dict.fromkeys(tuple(symbols) + MP.CONTEXT_SYMBOLS))
    firsts, lasts, stamps = [], [], []
    for s in names:
        f = MP._cache_file(Path(cache_root), s)                        # PanelDataError when the cache lacks the symbol
        stat = f.stat()
        first_s, last_s = _file_span(str(f), stat.st_size, stat.st_mtime_ns)
        firsts.append(first_s)
        lasts.append(last_s)
        stamps.append((s, stat.st_size, stat.st_mtime_ns))
    return max(firsts), min(min(lasts), eval_end, store_last), tuple(stamps)


def intraday_span(panel: "G.Panel", st: "G.GridSettings") -> Tuple[date, date, Tuple]:
    """``intraday_bounds`` of a daily ``panel``'s symbols under the run's ``eval_end``."""
    return intraday_bounds(panel.cache_root, panel.symbols, st.eval_end, panel.store_last)


@lru_cache(maxsize=1024)
def _intraday_hash(cache_root: str, symbol: str, size: int, mtime_ns: int, first: date, last: date) -> bytes:
    """SHA-1 of the row hashes of one symbol's 5-minute bars inside ``[first, last]`` (open, high, low, close, volume and
    the bar time). Cached on the file's size and mtime, so every cell and every resume check that shares a symbol reads
    the file once; only the 20-byte digest is kept."""
    df = pd.read_parquet(MP._cache_file(Path(cache_root), symbol), columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    window = df[(df["Date"] >= pd.Timestamp(first)) & (df["Date"] < pd.Timestamp(last) + pd.Timedelta(days=1))]
    return hashlib.sha1(pd.util.hash_pandas_object(window, index=False).to_numpy().tobytes()).digest()


def intraday_fingerprint(cache_root: Any, symbols: Any, eval_end: date, store_last: date) -> str:
    """Content hash of the 5-minute bars a tsai cell reads: every bar of ``symbols`` and of the context symbols inside
    ``intraday_bounds``. A touch of a file, or bars appended after the window, do not change it; a changed bar inside the
    window does. No mtime is part of it (the resume identity must survive a copy of the cache)."""
    first, last, stamps = intraday_bounds(cache_root, symbols, eval_end, store_last)
    h = hashlib.sha1()
    for sym, size, mtime in sorted(stamps):
        h.update(sym.encode("utf-8"))
        h.update(_intraday_hash(str(cache_root), sym, size, mtime, first, last))
    return h.hexdigest()[:16]


#: a ``ba2_darts_*`` temp directory untouched for this long belongs to a killed worker
STALE_TEMP_HOURS = 6.0


def sweep_stale_darts_dirs(root: Optional[Path] = None, max_age_hours: float = STALE_TEMP_HOURS) -> int:
    """Delete the ``ba2_darts_*`` checkpoint directories (``ml_models_seq`` makes one per Darts fit and removes it when the fit
    ends) that a killed worker left behind: only those untouched for ``max_age_hours``, so another live run's directory
    is never taken. Returns how many were removed."""
    import shutil
    import tempfile
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_hours * 3600.0
    n = 0
    for d in base.glob("ba2_darts_*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            logger.warning("could not sweep %s", d, exc_info=True)
    return n


@lru_cache(maxsize=4)
def _gpu_versions(cuda_initialized: bool) -> Dict[str, Any]:
    import torch
    try:
        cudnn = torch.backends.cudnn.version()
    except Exception:                                                  # noqa: BLE001
        cudnn = None
    device = torch.cuda.get_device_name(0) if cuda_initialized and torch.cuda.is_available() else None
    return {"torch_cuda": torch.version.cuda, "cudnn": cudnn, "device": device}


def gpu_versions() -> Dict[str, Any]:
    """CUDA / cuDNN versions and the device name of this process, for the audit trail of a GPU row. The device name is only
    read once CUDA is initialised (a fit has run): asking for it must never wake the GPU."""
    import torch
    return _gpu_versions(bool(torch.cuda.is_initialized()))


gpu_versions.cache_clear = _gpu_versions.cache_clear                  # type: ignore[attr-defined]


def get_intraday(panel: "G.Panel", st: "G.GridSettings", timer: Optional[Dict[str, float]] = None) -> IntradayPanel:
    """Per-process LRU of the intraday panel of the cell's symbols (keyed by the file stamps and the span). A build (a
    cache miss) adds its seconds to ``timer["build"]`` when a ``timer`` is given."""
    first, last, stamps = intraday_span(panel, st)
    key = (str(panel.cache_root), tuple(panel.symbols), first, last, stamps)
    if key in _INTRADAY:
        _INTRADAY.move_to_end(key)
        return _INTRADAY[key]
    t0 = time.perf_counter()
    spec = MP.PanelSpec(INTRADAY_TIMEFRAME, first, last, tuple(panel.symbols), INTRADAY_GROUPS)
    df, meta = MP.build_panel(spec, Path(panel.cache_root))
    ip = IntradayPanel(df, list(meta["features"]), first, last, time.perf_counter() - t0)
    if timer is not None:
        timer["build"] = ip.seconds
    _INTRADAY[key] = ip
    while len(_INTRADAY) > INTRADAY_CACHE_SIZE:
        _INTRADAY.popitem(last=False)
    return ip


# ================================================================== the darts input
def market_neutral_returns(ret: np.ndarray, finite: np.ndarray, member: np.ndarray) -> np.ndarray:
    """``ret`` (sessions, symbols) minus the same-session mean over the ``member`` symbols with a finite return; 0 where
    there is no return or fewer than ``T.MIN_SESSION_MEMBERS`` members to average (the ``rel_ret`` rule of
    ``ml_targets``)."""
    use = member & finite
    n = use.sum(axis=1)
    total = np.where(use, ret, 0.0).sum(axis=1)
    mean = np.where(n >= T.MIN_SESSION_MEMBERS, total / np.maximum(n, 1), np.nan)
    out = np.where(finite & np.isfinite(mean)[:, None], ret - mean[:, None], 0.0)
    return out


def daily_sequence_data(panel: "G.Panel", cols: List[int], fit_rows: np.ndarray, lookback: int, horizon: int,
                        neutral: bool = False) -> MS.SequenceData:
    """``SequenceData`` of the daily store frame: the ``cols`` of ``panel.X`` robust-scaled on ``fit_rows`` only (median /
    IQR, clipped at +-5, NaN -> 0), the target series (0 where there is no print), one series per symbol
    (``series_start`` = the symbol's first usable row). Rows are the panel's own (symbol-major). The series is the daily
    log return of the close, or with ``neutral`` that return minus the same-session mean of the universe members
    decidable at the session (``market_neutral_returns``): a common market shock then leaves it unchanged."""
    X = panel.X[:, cols].astype(np.float32)
    fit = X[np.asarray(fit_rows, dtype=np.int64)].astype(np.float64)
    med, iqr = np.empty(X.shape[1]), np.empty(X.shape[1])
    for j in range(X.shape[1]):
        med[j], iqr[j] = ml_search.robust_center_scale(fit[:, j])
    med = np.where(np.isfinite(med), med, 0.0)                         # an all-NaN column: a neutral channel
    iqr = np.where(np.isfinite(iqr) & (iqr > 0), iqr, 1.0)
    X = np.clip((X - med.astype(np.float32)) / iqr.astype(np.float32), -5, 5)
    X = np.nan_to_num(X, nan=0.0, posinf=5.0, neginf=-5.0).astype(np.float32)
    close = np.asarray(panel.arrays.close, dtype=np.float64)           # (sessions, symbols)
    with np.errstate(all="ignore"):
        lr = np.log(close)
        ret = np.full_like(lr, np.nan)
        ret[1:] = lr[1:] - lr[:-1]
    finite = np.isfinite(ret)
    if neutral:
        a = panel.arrays
        ret = market_neutral_returns(ret, finite, np.asarray(a.tradable, dtype=bool) & np.isfinite(a.atr_pct))
    else:
        ret = np.where(finite, ret, 0.0)
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
    p = {**M.DEFAULTS[base], **cell.params_dict, "arch": arch}          # every value explicit (device, epochs ...)
    p["epochs"] = min(int(p["epochs"]), st.gpu_max_epochs)
    return base, p


class WarningCollector:
    """Context manager that takes over the warnings of a block WITHOUT keeping each one: warnings of an op without a
    deterministic CUDA kernel are COUNTED per op name (``ops``; with ``torch.set_warn_always`` such an op warns at every
    call, thousands of times a block), any other warning is kept once with its count, up to ``max_others`` distinct ones
    (``others``; the overflow is counted in ``dropped``). On exit, also when the block raised, the filters and
    ``showwarning`` are restored and the kept warnings are re-emitted (with their count), so a failed fit loses none."""

    def __init__(self, max_others: int = 50) -> None:
        self.max_others = max_others
        self.ops: Dict[str, int] = {}
        self.others: "OrderedDict[Tuple, int]" = OrderedDict()
        self.dropped = 0
        self._ctx: Optional[Any] = None

    def _show(self, message: Any, category: Any, filename: str, lineno: int, file: Any = None, line: Any = None) -> None:
        text = str(message)
        m = NONDETERMINISTIC_OP.match(text)
        if m:
            self.ops[m.group(1)] = (self.ops[m.group(1)] if m.group(1) in self.ops else 0) + 1
            return
        key = (category, text, filename, lineno)
        if key in self.others:
            self.others[key] += 1
        elif len(self.others) < self.max_others:
            self.others[key] = 1
        else:
            self.dropped += 1

    def __enter__(self) -> "WarningCollector":
        self._ctx = warnings.catch_warnings()
        self._ctx.__enter__()
        warnings.simplefilter("always")
        warnings.showwarning = self._show                             # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._ctx.__exit__(*exc)                                      # filters and showwarning are back
        for (category, text, filename, lineno), n in self.others.items():
            warnings.warn_explicit(text + (f" [repeated {n} times]" if n > 1 else ""), category, filename, lineno)
        if self.dropped:
            logger.warning("%d further distinct warnings of a GPU block were dropped (the first %d are kept)",
                           self.dropped, self.max_others)
        return False


#: torch's warning for an op without a deterministic CUDA kernel (under ``use_deterministic_algorithms(warn_only=True)``)
NONDETERMINISTIC_OP = re.compile(r"^(\S+) does not have a deterministic implementation")


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
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")              # read when cuBLAS first runs in the process
    torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark = True, False    # as repeatable as the kernels allow (see gpu_nondeterministic)
    torch.use_deterministic_algorithms(True, warn_only=True)
    seed_everything(seed)
    base, params = _adapter_params(cell, st)
    if params["device"] == "auto" and not torch.cuda.is_available():
        raise RuntimeError("device='auto' but CUDA is not available in this GPU worker: refusing to fall back to the "
                           "CPU silently (set params.device to 'cpu' to run a deep cell on the CPU on purpose)")
    adapter = M.make_adapter(base, n_jobs, params)
    warn_always = torch.is_warn_always_enabled()
    collector = WarningCollector()
    try:
        with collector:
            torch.set_warn_always(True)                               # an op warns every time, not once per process
            try:
                if base == "tsai":
                    rows, scores, extra = _tsai_block(cell, seed, panel, br, st, adapter)
                else:
                    rows, scores, extra = _darts_block(cell, seed, panel, br, st, adapter)
            finally:
                torch.set_warn_always(warn_always)
    finally:
        _release_quietly(adapter)
        gc.collect()
    extra = {**extra, "gpu_nondeterministic": bool(collector.ops), "nondeterministic_ops": sorted(collector.ops),
             "nondeterministic_counts": dict(collector.ops)}
    info = adapter.fit_info if adapter.fit_info is not None else {}
    audit = {**G.block_audit(br, block, window), **extra,
             "device": info["device"] if "device" in info else None,
             "epochs_run": info["epochs_run"] if "epochs_run" in info else None,
             "best_epoch": info["best_epoch"] if "best_epoch" in info else None,
             "val_loss": info["val_loss"] if "val_loss" in info else None}
    return G.BlockResult(rows, scores, extra["n_fit"], extra["n_val"], None, audit)


def _tsai_block(cell, seed, panel, br, st, adapter) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    t0 = time.perf_counter()
    timer: Dict[str, float] = {"build": 0.0}
    ip = get_intraday(panel, st, timer)
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
    t1 = time.perf_counter()
    adapter.fit(Xt, y_t[mt], Xv, y_v[mv], br.ks.task, int(seed))
    scores = adapter.score(Xp)
    t2 = time.perf_counter()
    extra = {"prep_seconds": t1 - t0 - timer["build"], "intraday_build_seconds": timer["build"],
             "fit_seconds": t2 - t1,
             "n_fit": int(len(Xt)), "n_val": int(len(Xv)), "n_samples": int(len(Xt)),
             "n_fit_sessions": int(is_fit.sum()), "n_channels": int(data.n_channels), "lookback": L,
             "samples_per_session": st.seq_samples_per_session, "intraday_first": str(ip.first_day),
             "intraday_last": str(ip.last_day)}
    return pr[mp], scores, extra


def _darts_block(cell, seed, panel, br, st, adapter) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    t0 = time.perf_counter()
    H, L = cell.horizon, st.darts_lookback
    cols = panel.group_columns(cell.groups)
    neutral = cell.kind == "rel_return"
    data = daily_sequence_data(panel, cols, br.fit_idx, L, H, neutral=neutral)
    mt, mv, mp = _usable(data, br.fit_idx), _usable(data, br.val_idx), _usable(data, br.pr)
    if mt.sum() < 2 or not mv.any():
        raise G.SkipBlock(f"too few usable daily windows (train {int(mt.sum())}, validation {int(mv.sum())})")
    if not mp.any():
        raise G.SkipBlock("no scorable daily window in the block")
    fit_rows, val_rows = br.fit_idx[mt], br.val_idx[mv]
    Xt, Xv, Xp = data.take(fit_rows), data.take(val_rows), data.take(br.pr[mp])
    t1 = time.perf_counter()
    adapter.fit(Xt, br.y_all[fit_rows], Xv, br.y_all[val_rows], "quantile", int(seed))
    scores = adapter.score(Xp)
    t2 = time.perf_counter()
    extra = {"prep_seconds": t1 - t0, "intraday_build_seconds": 0.0, "fit_seconds": t2 - t1,
             "n_fit": int(len(Xt)), "n_val": int(len(Xv)), "n_samples": int(len(Xt)), "n_channels": int(data.n_channels),
             "lookback": L, "horizon_steps": H, "n_series": int(len(np.unique(Xt.series_start[Xt.rows]))),
             "target_series": "market_neutral" if neutral else "own_return"}
    return br.pr[mp], scores, extra
