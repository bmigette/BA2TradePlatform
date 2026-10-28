"""Stage-1 grid runner of the ML exploration v2: screen many configurations ("cells") x seeds at SIGNAL level.

A cell is one JSON config (``validate_cell``; strict, unknown keys refused): a track, a feature store, a symbol set, a
feature-group set, a target, a model family + params, a training-window length, a block frequency and the seeds.
For every (cell, seed) the runner walks the out-of-sample blocks of ``settings.eval_start .. eval_end`` (2019-01-01 ..
2026-10-12): train on the window before the block (purged by the target horizon, early-stopping split off the END of the
window), fit, score the block. The stitched out-of-sample scores are then evaluated per track.

Tracks and primary metrics (one number per (cell, seed), the thing ``factor_effects`` aggregates):

* ``selection``: rank IC and the top-minus-bottom quintile spread of ``rel_ret_H`` in volatility units, after
  ``settings.cost_bps`` (7) per round trip per leg; the long-only top quintile too. PRIMARY = ``quintile_spread_net``.
  Control: ``shuffle_symbols`` (20 seeds, rank, one-sided p) -- the shuffle that destroys a cross-sectional signal.
* ``timing``: in a name when its score is at or above the causal per-symbol 60% quantile of its EARLIER scores
  (``ml_eval.with_cutoff``; the stitched frame holds the earlier blocks, so a block's cutoff history is exactly the
  scores of the earlier blocks, never its own or a later one's), net of costs against the unconditional mean.
  PRIMARY = ``timing_excess_net``; also ``timing_sharpe(horizon)``. Control: ``shuffle_time``.
* target ``volatility`` is a SIZING model, not a trading one: the only metric is the Spearman correlation of the
  prediction with ``realvol_H`` (``spearman`` pooled over rows, ``spearman_within_symbol`` mean over names), on either
  track; there is no shuffle control (the trading controls do not apply). PRIMARY = ``spearman``.

Every metric is reported overall and per market regime (``ml_walkforward.regime``) with the Newey-West t (lag H) of
its daily series. ``controls_report`` returns BOTH shuffles; ``controls.primary`` names the one that matches the track.

Rows used: ``listed & ~filled & ~label_bar_filled & ~ended`` AND a finite target AND ``atr_pct`` (``daily_fold_rows``).
The scoring rows of a block never depend on the TARGET fitted (a quintile model still predicts every row that has a
``rel_ret``): only the training rows are restricted to the target's rows. A block whose fitted target fails
``check_balance(mode="min_count")`` (50 per class, on the fit and the validation part) is SKIPPED and recorded in
``skipped_blocks`` (with its reason); it is never filled. ``clipped_blocks`` lists blocks whose fixed training window was
shortened by ``data_start``.

Target -> (column, adapter task):

    rel_return     rel_ret_H      regression       quintile       quintile_H    binary (rows outside the quintiles dropped)
    beats          beats_H        binary           rank           rank_rel_H    rank (lambdarank, groups = sessions)
    up             up_H           binary           terciles       fitted/block  multiclass (0/1/2 = down/flat/up)
    triple_barrier fitted/block   multiclass       binary_barrier fitted/block  binary (take-profit first; 0/2 -> 0/1)
    regression     z_H            regression       quantile       ret_H         quantile (score = the median, alpha .5)
    volatility     realvol_H      regression

``rel_return``, ``quintile``, ``beats`` and ``rank`` need a cross-section and exist on the selection track only.

Cell schema (every key required except ``params`` and ``target.ratio``; unknown keys are refused)::

    {"track": "selection" | "timing",
     "store": "<dir>",                              one built store, or a root holding stocks/ and etf/
     "symbols": "ndq30_dev" | "timing" | ["AAPL", ...],
     "groups": ["t", "mtf", "m", "x", "mac", "f", "ag", "at", "i", "s"],       (a non-empty subset)
     "target": {"kind": <one of the eleven kinds above>, "horizon": 1|3|5|10|20, "ratio": 1.0},
     "family": "lgbm" | "xgb" | "catboost" | "linear" | "naive", "params": {...},
     "train_months": 12 | 24 | 36 | "expanding", "blocks": "Q" | "H", "seeds": [1, 2, 3]}

``triple_barrier`` (three classes: stop first / timeout / take-profit first) and ``binary_barrier`` (take-profit first
or not) are deliberately TWO kinds, not one ``barriers`` kind with a mode: they have different adapter tasks, different
class counts for ``check_balance`` and different scores, and a cell's ``target.kind`` then says which one it is.
``ratio`` (stop distance / take-profit distance) applies to those two only.

Identity and resume. ``cell_key`` hashes the canonical config WITHOUT the seeds and WITHOUT the store path, plus the
store's SIGNATURE (a hash of the store meta's ``ensure.inputs``, ``min_share``, symbols and date range, over the stores
the cell reads): moving a store keeps its keys, rebuilding it from other inputs changes them. Every results row also
records ``identity`` = the ``GridSettings`` it ran under (without ``cores``, which does not change a result), the store
signature and ``daily_fingerprint`` (size + mtime of the daily cache files of the cell's symbols). A restart REUSES a
finished row only when all three match; otherwise it raises ``ResumeMismatchError`` naming the differences, or with
``rerun_mismatched=True`` (CLI ``--rerun-mismatched``) moves the mismatched rows to ``results.stale.jsonl`` and
recomputes them.

Stores. ``store`` is either one built store (a directory holding ``features.parquet``) or a stores ROOT whose
sub-directories (``stocks``, ``etf``) are stores. Every symbol is read from the first store listing it. When a cell's
symbols span several stores (the ``timing`` set: 16 ETFs from ``etf`` + AAPL GOOGL TSLA from ``stocks``) the features
are the columns present in ALL the stores involved, and every requested group must keep at least one column: a timing
cell therefore uses the groups the ETF store has (``t``, ``mtf``, ``m``, ``mac``) and a cell asking for ``x`` / ``f`` /...
on that set is refused with the group named, never padded with NaN. Daily bars (targets, ATR) are read with
``ml_features.load_daily`` from ``cache_root`` over the store's own range; the store's ``listed`` / ``filled`` /
``label_bar_filled`` / ``ended`` flags select the rows.

Parallelism. A ``ProcessPoolExecutor`` of ``cpu_slots`` workers runs the tabular families; each worker loads each panel
(stores + bars + targets) ONCE (``_init_worker`` warms the panels of the pending tasks; ``get_panel`` is a per-process
cache keyed by store, symbols, cache root and track). ``n_jobs = cores // cpu_slots`` per fit, so slots x n_jobs <=
cores. The sequence families (``tsai-<arch>``, ``darts-<arch>``; see "GPU families") are registered in ``FAMILY_POOL``
with pool ``"gpu"`` and run in a second spawn pool of ``gpu_slots`` workers, concurrently with the CPU pool, with the
same lifecycle (retries, probation, cancellation, lock); ``ml_grid_seq`` holds their input construction.

GPU families (Task 4.4b). ``family`` is ``tsai-<arch>`` for every ``ml_models_seq.TSAI_ARCHS`` entry (stage 1 uses
``tsai-inception`` and ``tsai-minirocket``) or ``darts-<arch>`` for ``nhits`` / ``nbeats`` / ``tcn`` (TFT is refused:
20-55 minutes a fit). The architecture comes from the family, so ``params`` must not set ``arch``. Both need half-year
blocks (``blocks: "H"``). tsai reads the 1h INTRADAY panel (``ml_panel``, regular session, bars stamped at their end;
the cell's ``groups`` must be ``["panel_1h"]``: the panel's own technical / daily-context / market features): training
samples are windows ending at ``sample_bars(per_session)`` bars of the training sessions (the end-of-day bar always
included), each labelled with the DAILY target of its session (decided at that session's close, so an intraday bar's
label lies in its future and never in its past), validation is the end-of-day bars of the train-tail sessions, and
ONLY the end-of-day bar of each test session is predicted: one daily row per (symbol, session), scored like a tabular
cell. darts reads the daily store frame (the cell's groups, robust-scaled on the training rows, NaN as 0, as past
covariates; the target series is the daily log return), forecasts the next ``horizon`` daily returns as quantiles and
scores a row with P(up) of the H-day return; target kinds ``rel_return`` and ``quantile`` only. A deep cell's time
budget (``GridSettings.gpu_budget_minutes``) is checked before each block: over it the task is an error row.

Outputs in ``--out``: ``results.jsonl`` (one row per finished (cell_key, seed); resume skips them),
``predictions/<cell_key>_<seed>.parquet`` (written BEFORE the row, so a row implies its file), ``errors.jsonl`` (a
(cell, seed) that raised; not finished, so a restart retries it). ``cell_key`` = a short hash of the canonical config
without the seeds (the store path as written is part of it).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import multiprocessing
import os
import socket
import threading
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from app.services import ml_eval as E
from app.services import ml_features as F
from app.services import ml_holdout as HO
from app.services import ml_models as M
from app.services import ml_models_seq as MS
from app.services import ml_targets as T
from app.services import ml_walkforward as W
from app.services.ml_controls import DEFAULT_SEEDS, controls_report

logger = logging.getLogger(__name__)

TRACKS: Tuple[str, ...] = ("selection", "timing")
PRIMARY: Dict[str, str] = {"selection": "quintile_spread_net", "timing": "timing_excess_net"}
VOLATILITY_PRIMARY = "spearman"
TIMING_ETFS: Tuple[str, ...] = HO.TIMING_ETFS
TIMING_STOCKS: Tuple[str, ...] = ("AAPL", "GOOGL", "TSLA")          # NOT MSFT NVDA AMZN META: locked
SYMBOL_ALIASES: Tuple[str, ...] = ("ndq30_dev", "timing")
#: stores of a stores ROOT, in the order a symbol is looked up
STORE_ORDER: Tuple[str, ...] = ("stocks", "etf")
#: a block whose best boosting iteration is at most this has an (almost) constant model: its scores are mostly ties
LOW_ITER = 3
#: families without randomness (``ml_models``): their seed std is 0 by construction, which is NOT robustness
DETERMINISTIC_FAMILIES: Tuple[str, ...] = ("naive", "linear")
from app.services.ml_grid_bounds import DATA_START, EVAL_END, EVAL_START          # noqa: E402  (stdlib-only module)


# ================================================================== settings
@dataclass(frozen=True)
class GridSettings:
    """Run-level constants (not part of a cell's identity). The defaults are the exploration's; tests shrink the window."""
    eval_start: date = EVAL_START
    eval_end: date = EVAL_END
    data_start: date = DATA_START
    cost_bps: float = 7.0
    timing_q: float = 0.6
    min_count: int = T.MIN_CLASS_COUNT
    val_share: float = 0.15
    min_obs: int = E.MIN_OBS
    regime_min_rows: int = 20
    control_seeds: Tuple[int, ...] = DEFAULT_SEEDS
    cores: int = 24
    #: GPU families only (they are left out of a tabular row's identity): tsai window length in 1h bars (35 = 5
    #: sessions), training bars per session and symbol (``ml_panel.sample_bars``; the end-of-day bar included),
    #: darts window in daily bars, and the cap on a deep cell's epochs (early stopping still applies)
    seq_lookback: int = 35
    seq_samples_per_session: int = 4
    darts_lookback: int = 20
    gpu_max_epochs: int = 15
    #: minutes one (cell, seed) of a GPU family may take before the task is aborted with an error row (a run setting:
    #: not part of any row's identity)
    gpu_budget_minutes: float = 20.0

    def __post_init__(self) -> None:
        if not self.eval_start < self.eval_end:
            raise ValueError("eval_start must be before eval_end")
        if not self.data_start < self.eval_start:
            raise ValueError("data_start must be before eval_start")
        if self.cost_bps < 0 or not 0 < self.timing_q < 1 or not 0 < self.val_share < 1:
            raise ValueError("cost_bps >= 0, 0 < timing_q < 1 and 0 < val_share < 1 required")
        for name in ("min_count", "min_obs", "regime_min_rows", "cores", "seq_lookback", "seq_samples_per_session",
                     "darts_lookback", "gpu_max_epochs"):
            if not _is_int(getattr(self, name)) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be an integer >= 1")
        if not _is_real(self.gpu_budget_minutes) or self.gpu_budget_minutes <= 0:
            raise ValueError("gpu_budget_minutes must be a number > 0")
        if not len(self.control_seeds):
            raise ValueError("control_seeds is empty")

    def to_dict(self) -> Dict[str, Any]:
        return {"eval_start": str(self.eval_start), "eval_end": str(self.eval_end), "data_start": str(self.data_start),
                "cost_bps": self.cost_bps, "timing_q": self.timing_q, "min_count": self.min_count,
                "val_share": self.val_share, "min_obs": self.min_obs, "regime_min_rows": self.regime_min_rows,
                "control_seeds": list(self.control_seeds), "cores": self.cores, "seq_lookback": self.seq_lookback,
                "seq_samples_per_session": self.seq_samples_per_session, "darts_lookback": self.darts_lookback,
                "gpu_max_epochs": self.gpu_max_epochs, "gpu_budget_minutes": self.gpu_budget_minutes}

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "GridSettings":
        d = dict(d or {})
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown grid settings {sorted(unknown)}; known {sorted(cls.__dataclass_fields__)}")
        for k in ("eval_start", "eval_end", "data_start"):
            if k in d and not isinstance(d[k], date):
                d[k] = date.fromisoformat(str(d[k]))
        if "control_seeds" in d:
            d["control_seeds"] = tuple(int(s) for s in d["control_seeds"])
        return cls(**d)


def _is_int(v: Any) -> bool:
    return isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))


def _is_real(v: Any) -> bool:
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_)) \
        and math.isfinite(float(v))


# ================================================================== cells
@dataclass(frozen=True)
class KindSpec:
    column: Optional[str]          # the ``daily_targets`` family the model trains on; None = fitted per block
    task: str                      # the adapter task
    selection_only: bool = False
    fitted: Optional[str] = None   # ``ml_targets.TargetSpec`` kind for a fitted target


#: ml_targets kind used for ``check_balance`` / the class names of a (non-fitted) binary target
_BALANCE_KIND = "binary_direction"

KINDS: Dict[str, KindSpec] = {
    "rel_return": KindSpec("rel_ret", "regression", selection_only=True),
    "quintile": KindSpec("quintile", "binary", selection_only=True),
    "beats": KindSpec("beats", "binary", selection_only=True),
    "rank": KindSpec("rank_rel", "rank", selection_only=True),
    "up": KindSpec("up", "binary"),
    "terciles": KindSpec(None, "multiclass", fitted="direction"),
    "triple_barrier": KindSpec(None, "multiclass", fitted="triple_barrier"),
    "binary_barrier": KindSpec(None, "binary", fitted="binary_barrier"),
    "regression": KindSpec("z", "regression"),
    "quantile": KindSpec("ret", "quantile"),
    "volatility": KindSpec("realvol", "regression"),
}
_BARRIER_KINDS = ("triple_barrier", "binary_barrier")

#: GPU families: ``tsai-<arch>`` for every tsai architecture, ``darts-<arch>`` for the batched Darts ones (tft is refused)
TSAI_FAMILIES: Tuple[str, ...] = tuple(f"tsai-{a}" for a in MS.TSAI_ARCHS)
GRID_DARTS_ARCHS: Tuple[str, ...] = tuple(a for a in MS.DARTS_ARCHS if a in MS.BATCHED_ARCHS)
DARTS_FAMILIES: Tuple[str, ...] = tuple(f"darts-{a}" for a in GRID_DARTS_ARCHS)
GPU_FAMILIES: Tuple[str, ...] = TSAI_FAMILIES + DARTS_FAMILIES
#: target kinds a darts cell may use: it forecasts the symbol's own bar returns, whatever the evaluation target
DARTS_KINDS: Tuple[str, ...] = ("rel_return", "quantile")
#: the intraday panel's feature group name a tsai cell must declare
TSAI_GROUPS: Tuple[str, ...] = ("panel_1h",)

#: which worker pool runs a family
FAMILY_POOL: Dict[str, str] = {**{f: "cpu" for f in M.SUPPORTED_TASKS if f not in M.SEQUENCE_FAMILIES},
                               **{f: "gpu" for f in GPU_FAMILIES}}


def split_family(family: str) -> Tuple[str, Optional[str]]:
    """``("tsai", "inception")`` for ``"tsai-inception"``; ``(family, None)`` for a tabular family."""
    if family in GPU_FAMILIES:
        base, _, arch = family.partition("-")
        return base, arch
    return family, None

_CELL_KEYS = ("track", "store", "symbols", "groups", "target", "family", "params", "train_months", "blocks", "seeds")
_CELL_OPTIONAL = ("params",)
_TARGET_KEYS = ("kind", "horizon", "ratio")


@dataclass(frozen=True)
class Cell:
    """A validated, canonical grid cell (build with ``validate_cell``)."""
    track: str
    store: str
    symbols: Union[str, Tuple[str, ...]]
    groups: Tuple[str, ...]
    kind: str
    horizon: int
    ratio: float
    family: str
    params: Tuple[Tuple[str, Any], ...]
    train_months: Union[int, str]
    blocks: str
    seeds: Tuple[int, ...]
    signature: str = ""            # the store signature (``store_signature``): part of the key, not of the config

    @property
    def params_dict(self) -> Dict[str, Any]:
        return dict(self.params)

    def config(self, with_seeds: bool = False) -> Dict[str, Any]:
        """The canonical config (JSON-able; it keeps the store path so the cell can be loaded again)."""
        out = {"track": self.track, "store": self.store,
               "symbols": self.symbols if isinstance(self.symbols, str) else list(self.symbols),
               "groups": list(self.groups),
               "target": {"kind": self.kind, "horizon": self.horizon, "ratio": self.ratio},
               "family": self.family, "params": self.params_dict, "train_months": self.train_months,
               "blocks": self.blocks}
        if with_seeds:
            out["seeds"] = list(self.seeds)
        return out

    @property
    def key(self) -> str:
        cfg = self.config()
        del cfg["store"]                                   # the path is where it lives; the signature is what it is
        blob = json.dumps({"config": cfg, "store_signature": self.signature}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def label(self) -> str:
        return (f"{self.track} {self.kind} h{self.horizon} {self.family} {self.train_months} "
                f"{'+'.join(self.groups)} {self.blocks}")

    @property
    def spec(self) -> KindSpec:
        return KINDS[self.kind]

    @property
    def pool(self) -> str:
        return FAMILY_POOL[self.family]


def resolve_symbols(symbols: Union[str, Sequence[str]]) -> Tuple[str, ...]:
    """The symbol tuple of a cell's ``symbols`` (an alias or an explicit list)."""
    if isinstance(symbols, str):
        if symbols == "ndq30_dev":
            return tuple(HO.load()["ndq30"]["dev"])
        if symbols == "timing":
            return TIMING_ETFS + TIMING_STOCKS
        raise ValueError(f"symbols alias must be one of {SYMBOL_ALIASES}, got {symbols!r}")
    return tuple(symbols)


def normalise_train_months(v: Any) -> Union[int, str]:
    """The canonical ``train_months``: an ``int`` of ``W.TRAIN_MONTHS`` or ``"expanding"``. Accepts 36, 36.0, "36",
    " 36 " and any case of "expanding", so that two spellings of one cell give one ``cell_key``; refuses the rest."""
    x = v
    if isinstance(x, str):
        x = x.strip()
        if x.lower() == "expanding":
            return "expanding"
        if x.isdigit():
            x = int(x)
    if isinstance(x, (float, np.floating)) and not isinstance(x, bool) and float(x).is_integer():
        x = int(x)
    if isinstance(x, bool) or not _is_int(x) or int(x) not in W.TRAIN_MONTHS:
        raise ValueError(f"train_months must be one of {W.TRAIN_MONTHS} or 'expanding', got {v!r}")
    return int(x)


def validate_cell(raw: Any) -> Cell:
    """Strict validation of one cell config. Unknown or missing keys, wrong types, unknown groups / kinds / families,
    a (family, task) pair that does not exist, a selection-only target on the timing track and any LOCKED symbol
    (``LockedSymbolError``) are refused with a message naming the problem."""
    if not isinstance(raw, dict):
        raise ValueError(f"a cell must be a JSON object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(_CELL_KEYS))
    if unknown:
        raise ValueError(f"unknown cell keys {unknown}; known {list(_CELL_KEYS)}")
    missing = [k for k in _CELL_KEYS if k not in raw and k not in _CELL_OPTIONAL]
    if missing:
        raise ValueError(f"cell is missing keys {missing}")
    track = raw["track"]
    if track not in TRACKS:
        raise ValueError(f"track must be one of {TRACKS}, got {track!r}")
    family = raw["family"]
    if not isinstance(family, str) or family not in FAMILY_POOL:
        raise ValueError(f"family must be one of {sorted(FAMILY_POOL)}, got {family!r}")
    base_family, arch = split_family(family)
    store = raw["store"]
    if not isinstance(store, str) or not store:
        raise ValueError("store must be a non-empty directory string")
    sym = raw["symbols"]
    if isinstance(sym, str):
        if sym not in SYMBOL_ALIASES:
            raise ValueError(f"symbols must be one of {SYMBOL_ALIASES} or a list of symbols, got {sym!r}")
        if track == "selection" and sym == "timing":
            raise ValueError("the 'timing' symbol set mixes ETFs and stocks: not a selection universe")
        symbols: Union[str, Tuple[str, ...]] = sym
    else:
        if not isinstance(sym, (list, tuple)) or not sym or not all(isinstance(s, str) for s in sym):
            raise ValueError("symbols must be an alias or a non-empty list of strings")
        if len(set(sym)) != len(sym):
            raise ValueError("symbols holds duplicates")
        symbols = tuple(sorted(sym))
    HO.assert_unlocked(resolve_symbols(symbols), f"the grid ({track})")
    if track == "selection" and set(resolve_symbols(symbols)) & set(TIMING_ETFS):
        raise ValueError(f"a selection cell must contain no ETF (found "
                         f"{sorted(set(resolve_symbols(symbols)) & set(TIMING_ETFS))}): ETFs are timing instruments")
    groups = raw["groups"]
    if not isinstance(groups, (list, tuple)) or not groups or not all(isinstance(g, str) for g in groups) \
            or len(set(groups)) != len(groups):
        raise ValueError("groups must be a non-empty list of distinct group names")
    if base_family == "tsai":
        if tuple(groups) != TSAI_GROUPS:
            raise ValueError(f"a tsai cell reads the 1h panel's own features: groups must be exactly "
                             f"{list(TSAI_GROUPS)}, got {list(groups)}")
    else:
        bad = [g for g in groups if g not in F.GROUPS]
        if bad:
            raise ValueError(f"unknown feature groups {bad}; known {sorted(F.GROUPS)}")
    target = raw["target"]
    if not isinstance(target, dict):
        raise ValueError("target must be an object {kind, horizon[, ratio]}")
    tu = sorted(set(target) - set(_TARGET_KEYS))
    if tu or "kind" not in target or "horizon" not in target:
        raise ValueError(f"target needs kind and horizon and takes only {list(_TARGET_KEYS)} (unknown: {tu})")
    kind = target["kind"]
    if kind not in KINDS:
        raise ValueError(f"target kind must be one of {sorted(KINDS)}, got {kind!r}")
    if base_family == "darts" and kind not in DARTS_KINDS:
        raise ValueError(f"darts forecasts the symbol's own returns: target kind must be one of {DARTS_KINDS}, "
                         f"got {kind!r}")
    if track == "timing" and KINDS[kind].selection_only:
        raise ValueError(f"target {kind!r} is cross-sectional: selection track only")
    horizon = target["horizon"]
    if not _is_int(horizon) or horizon not in T.DAILY_HORIZONS:
        raise ValueError(f"target horizon must be one of {T.DAILY_HORIZONS}, got {horizon!r}")
    if kind == "volatility" and horizon < 2:
        raise ValueError("a volatility target needs horizon >= 2 (realvol_1 is undefined)")
    ratio = target["ratio"] if "ratio" in target else 1.0
    if not _is_real(ratio) or ratio <= 0:
        raise ValueError(f"target ratio must be a number > 0, got {ratio!r}")
    if kind not in _BARRIER_KINDS and float(ratio) != 1.0:
        raise ValueError(f"target ratio applies to {_BARRIER_KINDS} only (got {ratio!r} for {kind!r})")
    task = KINDS[kind].task
    if base_family != "darts" and task not in M.SUPPORTED_TASKS[base_family]:
        raise ValueError(f"family {family!r} cannot do the {task!r} task of target {kind!r} "
                         f"(supports {M.SUPPORTED_TASKS[base_family]})")
    params = raw["params"] if "params" in raw else {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if arch is not None:
        if "arch" in params:
            raise ValueError(f"params must not set 'arch': the family {family!r} fixes it")
        M.make_adapter(base_family, 1, {**params, "arch": arch})
    else:
        M.make_adapter(family, 1, params)                  # unknown keys / out-of-range values raise
    months = normalise_train_months(raw["train_months"])
    blocks = raw["blocks"]
    if blocks not in W.FREQS:
        raise ValueError(f"blocks must be one of {W.FREQS}, got {blocks!r}")
    if FAMILY_POOL[family] == "gpu" and blocks != "H":
        raise ValueError(f"a GPU family trains on half-year blocks (blocks: 'H'), got {blocks!r}")
    seeds = raw["seeds"]
    if not isinstance(seeds, (list, tuple)) or not seeds or not all(_is_int(s) for s in seeds) \
            or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be a non-empty list of distinct integers")
    return Cell(track, store, symbols, tuple(sorted(groups)), kind, int(horizon), float(ratio), family,
                tuple(sorted(params.items())), months, blocks, tuple(int(s) for s in seeds),
                store_signature(store, resolve_symbols(symbols)))


def load_cells(source: Union[str, Path, Sequence[Dict[str, Any]], Dict[str, Any]]) -> List[Cell]:
    """Cells from a JSON file path, a list of cell objects or ``{"cells": [...]}``. Two cells with the same
    ``cell_key`` are refused (merge their seeds in one cell)."""
    if isinstance(source, (str, Path)):
        source = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(source, dict):
        extra = set(source) - {"cells"}
        if extra or "cells" not in source:
            raise ValueError(f"a grid config object holds exactly the key 'cells' (got {sorted(source)})")
        source = source["cells"]
    if not isinstance(source, (list, tuple)) or not source:
        raise ValueError("the grid config must be a non-empty list of cells")
    cells = [validate_cell(c) for c in source]
    seen: Dict[str, int] = {}
    for i, c in enumerate(cells):
        if c.key in seen:
            raise ValueError(f"cells {seen[c.key]} and {i} are the same configuration (key {c.key})")
        seen[c.key] = i
    return cells


# ================================================================== stores and panels
@lru_cache(maxsize=256)
def _read_meta(meta_path: str, mtime_ns: int) -> Dict[str, Any]:
    return json.loads(Path(meta_path).read_text(encoding="utf-8"))


def _meta_of(d: Path) -> Dict[str, Any]:
    p = Path(str(d / "features.parquet") + ".meta.json")
    return _read_meta(str(p), p.stat().st_mtime_ns)


def discover_stores(store: Union[str, Path]) -> List[Tuple[str, Path, List[str]]]:
    """``[(name, dir, symbols)]`` of the built stores under ``store`` (one store, or a root with store sub-directories),
    in lookup order (``stocks``, ``etf``, then the others by name). ``symbols`` is the store's own row universe."""
    root = Path(store)
    if (root / "features.parquet").exists():
        dirs = [(root.name, root)]
    else:
        subs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "features.parquet").exists()) \
            if root.is_dir() else []
        subs.sort(key=lambda p: (STORE_ORDER.index(p.name) if p.name in STORE_ORDER else len(STORE_ORDER), p.name))
        dirs = [(p.name, p) for p in subs]
    if not dirs:
        raise FileNotFoundError(f"no feature store under {root} (a directory with features.parquet, or a root holding such directories)")
    return [(name, d, list(_meta_of(d)["symbols"])) for name, d in dirs]


def store_owners(avail: Sequence[Tuple[str, Path, Sequence[str]]]) -> Dict[str, str]:
    """symbol -> name of the first store (in lookup order) whose universe lists it."""
    owner: Dict[str, str] = {}
    for name, _, syms in avail:
        for sym in syms:
            owner.setdefault(sym, name)
    return owner


def stores_used(avail: Sequence[Tuple[str, Path, Sequence[str]]], symbols: Sequence[str], where: str = ""
                ) -> List[Tuple[str, Path]]:
    """``[(name, dir)]`` of the stores that own some of ``symbols`` (lookup order); raises when a symbol is in no store."""
    owner = store_owners(avail)
    absent = [s for s in symbols if s not in owner]
    if absent:
        raise ValueError(f"symbols {absent} are in no store{' under ' + where if where else ''} "
                         f"(stores: {', '.join(f'{n}={len(s)} symbols' for n, _, s in avail)})")
    return [(n, d) for n, d, _ in avail if any(owner[s] == n for s in symbols)]


def store_signature(store: Union[str, Path], symbols: Sequence[str]) -> str:
    """Identity of the store data a cell reads, independent of where the store lives: a hash over the stores that hold
    ``symbols`` of (name, ``ensure.inputs`` -- code version, coverage columns, source fingerprints --, ``min_share``,
    symbols, first / last day, cache end). Raises when a symbol is in no store, or when a store's meta has no
    ``ensure.inputs`` (built before the inputs were recorded)."""
    parts = {}
    for name, d in stores_used(discover_stores(store), symbols, str(store)):
        m = _meta_of(d)
        if "inputs" not in m["ensure"]:
            raise ValueError("store meta lacks ensure.inputs: rebuild the store")
        parts[name] = {"inputs": m["ensure"]["inputs"], "min_share": m["ensure"]["min_share"],
                       "symbols": m["symbols"], "first_day": m["first_day"], "last_day": m["last_day"],
                       "cache_end": m["cache_end"]}
    return hashlib.sha1(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def store_span(store: Union[str, Path], symbols: Sequence[str]) -> Tuple[date, date, Optional[date]]:
    """``(first_day, last_day, cache_end)`` of the stores holding ``symbols``; they must agree."""
    spans = set()
    for _, d in stores_used(discover_stores(store), symbols, str(store)):
        m = _meta_of(d)
        spans.add((m["first_day"], m["last_day"], m["cache_end"]))
    if len(spans) != 1:
        raise ValueError(f"the stores under {store} were built over different ranges {sorted(spans)}")
    first, last, end = next(iter(spans))
    return date.fromisoformat(first), date.fromisoformat(last), date.fromisoformat(end) if end else None


@lru_cache(maxsize=1024)
def _bar_hash(cache_root: str, symbol: str, size: int, mtime_ns: int, first: date, last: date) -> bytes:
    """Row hashes of one symbol's bars inside ``[first, last]``. Cached on the file's size and mtime, so the API's
    resume check and every symbol set that shares a symbol read each file once."""
    raw = F._read_daily(symbol, Path(cache_root))
    window = raw[(raw.index >= pd.Timestamp(first)) & (raw.index <= pd.Timestamp(last))][list(F.BAR_COLUMNS)]
    return pd.util.hash_pandas_object(window, index=True).to_numpy().tobytes()


def daily_fingerprint(cache_root: Union[str, Path], symbols: Sequence[str], first: date, last: date) -> str:
    """Hash of the daily bars (open, high, low, close, volume, row by row) that fall in ``[first, last]``, per symbol:
    exactly what the panel reads. A touch of a file or bars appended AFTER ``last`` do not change it; a changed bar
    inside the window does. Per-symbol hashes are cached (``_bar_hash``)."""
    h = hashlib.sha1()
    for sym in sorted(symbols):
        st = F._cache_file(Path(cache_root), sym).stat()
        h.update(sym.encode("utf-8"))
        h.update(_bar_hash(str(cache_root), sym, st.st_size, st.st_mtime_ns, first, last))
    return h.hexdigest()[:16]


@lru_cache(maxsize=64)
def _schema_names(path: str, mtime_ns: int) -> Tuple[str, ...]:
    import pyarrow.parquet as pq
    return tuple(pq.read_schema(path).names)


def usable_feature_columns(store: Union[str, Path], symbols: Sequence[str]) -> List[str]:
    """The feature columns (a registered group's prefix) present in EVERY store that owns some of ``symbols``,
    read from the parquet schemas only (no data)."""
    cols: Optional[List[str]] = None
    prefixes = tuple(g.prefix for g in F.GROUPS.values())
    for _, d in stores_used(discover_stores(store), symbols, str(store)):
        f = d / "features.parquet"
        names = [c for c in _schema_names(str(f), f.stat().st_mtime_ns) if c.startswith(prefixes)]
        cols = names if cols is None else [c for c in cols if c in set(names)]
    return cols or []


def check_groups(cells: Sequence["Cell"]) -> None:
    """Refuse, before any work starts, every cell asking for a feature group that has no column for some of its
    symbols (a group missing from one of the stores involved)."""
    problems: List[str] = []
    cache: Dict[Tuple, List[str]] = {}
    for c in cells:
        if split_family(c.family)[0] == "tsai":              # the 1h panel's own features, not a store group
            continue
        key = (c.store, c.symbols)
        if key not in cache:
            cache[key] = usable_feature_columns(c.store, resolve_symbols(c.symbols))
        for g in c.groups:
            if not any(col.startswith(F.GROUPS[g].prefix) for col in cache[key]):
                stores = [n for n, _ in stores_used(discover_stores(c.store), resolve_symbols(c.symbols), c.store)]
                problems.append(f"feature group {g!r} has no column in every store involved ({', '.join(stores)}): "
                                f"cell {c.key} ({c.label})")
    if problems:
        raise ValueError(f"{len(problems)} cell(s) use a feature group with no usable column, refused up front: "
                         + "; ".join(problems[:10]) + ("; ..." if len(problems) > 10 else ""))


def _store_fingerprint(store: Union[str, Path]) -> Tuple:
    return tuple((name, (d / "features.parquet").stat().st_mtime_ns, (d / "features.parquet").stat().st_size)
                 for name, d, _ in discover_stores(store))


@dataclass
class Panel:
    """Everything one (store, symbols, track) needs, loaded once per process: the feature matrix, the row-usability
    mask, the daily arrays and, lazily per horizon, the targets and barrier excursions."""
    symbols: Tuple[str, ...]
    track: str
    index: pd.MultiIndex
    sessions: pd.DatetimeIndex                 # per row
    columns: Tuple[str, ...]
    X: np.ndarray                              # (rows, len(columns)) float32
    ok: np.ndarray                             # listed & ~filled & ~label_bar_filled & ~ended
    bars: Dict[str, pd.DataFrame]
    arrays: T.DailyArrays
    stores: Tuple[str, ...]
    load_seconds: float
    daily_fingerprint: str = ""
    cache_root: str = ""
    _targets: Dict[int, pd.DataFrame] = field(default_factory=dict)
    _exc: Dict[int, T.DailyExcursions] = field(default_factory=dict)

    def group_columns(self, groups: Sequence[str]) -> List[int]:
        """Positions of the feature columns of ``groups`` (``*_covered`` and ``<prefix>missing`` flags included)."""
        pos: List[int] = []
        for g in groups:
            prefix = F.GROUPS[g].prefix
            found = [i for i, c in enumerate(self.columns) if c.startswith(prefix)]
            if not found:
                raise ValueError(f"feature group {g!r} has no column present in every store involved "
                                 f"({', '.join(self.stores)}); the cell cannot use it")
            pos += found
        return pos

    def targets(self, horizon: int) -> pd.DataFrame:
        if horizon not in self._targets:
            kinds = ["ret", "vol", "up", "z", "realvol", "target_end"]
            if self.track == "selection":
                kinds += ["rel_ret", "quintile", "beats", "rank_rel"]
            tg = T.daily_targets(self.bars, [horizon], None, kinds=kinds, arrays=self.arrays)
            if not tg.index.equals(self.index):
                raise RuntimeError("daily targets and the feature rows disagree on the (symbol, session) index")
            self._targets[horizon] = tg
        return self._targets[horizon]

    def excursions(self, horizon: int) -> T.DailyExcursions:
        if horizon not in self._exc:
            self._exc[horizon] = T.daily_excursions(None, [horizon], arrays=self.arrays)[horizon]
        return self._exc[horizon]


def load_panel(store: Union[str, Path], symbols: Tuple[str, ...], track: str, cache_root: Union[str, Path]) -> Panel:
    """Read the stores holding ``symbols``, the daily bars and the usability flags (see the module docstring)."""
    t0 = time.perf_counter()
    avail = discover_stores(store)
    owner = store_owners(avail)
    used = stores_used(avail, symbols, str(store))
    frames: Dict[str, pd.DataFrame] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    for name, d in used:
        df = F.load_feature_store(d, mask=True)
        metas[name] = df.attrs["meta"]
        want = [s for s in symbols if owner[s] == name]
        frames[name] = df[df.index.get_level_values("symbol").isin(want)]
    firsts = {(m["first_day"], m["last_day"], m["cache_end"]) for m in metas.values()}
    if len(firsts) != 1:
        raise ValueError(f"the stores under {store} were built over different ranges {sorted(firsts)}")
    first_s, last_s, cache_end_s = next(iter(firsts))
    first, last = date.fromisoformat(first_s), date.fromisoformat(last_s)
    cache_end = date.fromisoformat(cache_end_s) if cache_end_s else None
    files = [F._cache_file(Path(cache_root), sym) for sym in symbols]
    stamp = [(f.stat().st_size, f.stat().st_mtime_ns) for f in files]
    fingerprint = daily_fingerprint(cache_root, symbols, first, last)         # BEFORE the bars are used
    bars, _ = F.load_daily(symbols, first, last, Path(cache_root), cache_end=cache_end)
    if stamp != [(f.stat().st_size, f.stat().st_mtime_ns) for f in files]:
        raise RuntimeError("the daily cache changed while the panel was loading; run again")
    arrays = T.daily_arrays(bars)
    index = pd.MultiIndex.from_product([arrays.symbols, arrays.sessions], names=["symbol", "session"])
    cols: Optional[List[str]] = None
    for df in frames.values():
        fc = F.feature_columns(df)
        cols = fc if cols is None else [c for c in cols if c in set(fc)]
    parts = []
    for name, df in frames.items():
        part = df[list(cols) + ["listed", "filled", "ended", "label_bar_filled"]]
        parts.append(part)
    allrows = pd.concat(parts) if len(parts) > 1 else parts[0]
    lost = index.difference(allrows.index)
    if len(lost):
        raise ValueError(f"the stores lack {len(lost)} (symbol, session) rows of the daily bars, first {lost[0]}")
    allrows = allrows.reindex(index)
    lbf = allrows["label_bar_filled"]
    ok = (allrows["listed"].astype(bool) & ~allrows["filled"].astype(bool) & ~allrows["ended"].astype(bool)
          & (lbf == False).fillna(False).astype(bool)).to_numpy()          # noqa: E712  (NA = unknown: not usable)
    X = allrows[list(cols)].to_numpy(dtype=np.float32)
    return Panel(symbols, track, index, pd.DatetimeIndex(index.get_level_values("session")), tuple(cols), X, ok, bars,
                 arrays, tuple(n for n, _ in used), time.perf_counter() - t0, fingerprint, str(cache_root))


#: panels (stores + bars + targets, ~100-200 MB each) a worker process keeps; the least recently used is evicted
PANEL_CACHE_SIZE = 2
_PANELS: "OrderedDict[Tuple, Panel]" = OrderedDict()


def get_panel(store: str, symbols: Tuple[str, ...], track: str, cache_root: str) -> Panel:
    """Per-process LRU cache (``PANEL_CACHE_SIZE``) of ``load_panel`` keyed by store (path + file fingerprints), symbol
    set, track, cache root. Evicting a panel frees its cached targets and excursions with it."""
    key = (str(Path(store).resolve()), _store_fingerprint(store), tuple(symbols), track, str(cache_root))
    if key in _PANELS:
        _PANELS.move_to_end(key)
        return _PANELS[key]
    panel = load_panel(store, tuple(symbols), track, cache_root)
    _PANELS[key] = panel
    while len(_PANELS) > PANEL_CACHE_SIZE:
        _PANELS.popitem(last=False)
    return panel


# ================================================================== one block
class SkipBlock(Exception):
    """A block that is not fitted (recorded with its reason in ``skipped_blocks``), never silently filled."""


def _group_arrays(sessions: np.ndarray, rows: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """``(rows ordered by session, rows per session)``: the layout lambdarank needs."""
    order = rows[np.argsort(sessions[rows], kind="stable")]
    _, sizes = np.unique(sessions[order], return_counts=True)
    return order, sizes


def _counts(y: np.ndarray, binary: bool) -> List[int]:
    """Class counts in the ``ml_targets.class_counts`` layout (a two-class target uses classes 0 and 2)."""
    if binary:
        return [int((y == 0).sum()), 0, int((y == 1).sum())]
    return [int((y == c).sum()) for c in (0, 1, 2)]


@dataclass
class BlockResult:
    rows: np.ndarray
    scores: np.ndarray
    n_fit: int
    n_val: int
    n_iter: Any
    audit: Dict[str, Any] = field(default_factory=dict)


def best_iter(n_iter: Any) -> Optional[int]:
    """The best boosting iteration of the model that produced the score: the int of a single booster, the median
    level (alpha 0.5, the score) of a quantile model's nine, None for a family without boosting rounds."""
    if n_iter is None:
        return None
    if isinstance(n_iter, (list, tuple)):
        return int(n_iter[M.MEDIAN_INDEX])
    return int(n_iter)


@dataclass
class BlockRows:
    """The daily rows of one block, the same for every family: purged training rows split into fit / validation (the
    validation block is the train tail), the scorable rows of the block and the labels."""
    ks: KindSpec
    spec: Any
    tg: pd.DataFrame
    tr: np.ndarray
    pr: np.ndarray
    y_all: np.ndarray
    fit_idx: np.ndarray
    val_idx: np.ndarray
    day: np.ndarray
    tend: np.ndarray


def block_audit(br: BlockRows, block: W.Block, window: W.Window) -> Dict[str, Any]:
    """What a block was fitted on: its bounds and the first / last session and target end of its fit, validation and
    training rows (``train_target_end_max`` must lie before ``start``: every label was decided before the block)."""
    iso = lambda i: str(pd.Timestamp(i).date())                       # noqa: E731
    day, tend = br.day, br.tend
    return {"start": str(block.start), "end": str(block.end), "window_start": str(window.start),
            "fit_first": iso(day[br.fit_idx].min()), "fit_last": iso(day[br.fit_idx].max()),
            "val_first": iso(day[br.val_idx].min()), "val_last": iso(day[br.val_idx].max()),
            "fit_target_end_max": iso(tend[br.fit_idx].max()), "train_target_end_max": iso(tend[br.tr].max())}


def prepare_block(cell: Cell, panel: Panel, block: W.Block, window: W.Window, st: GridSettings) -> BlockRows:
    """The daily rows of ``block`` (see ``BlockRows``). Raises ``SkipBlock`` (reason in the message)."""
    H = cell.horizon
    ks = cell.spec
    tg = panel.targets(H)
    fold = T.Fold(block.name, window.start, block.start, block.end)
    if cell.kind == "volatility":
        score_col = f"realvol_{H}"
    else:
        score_col = f"rel_ret_{H}" if cell.track == "selection" else f"ret_{H}"
    train_col = f"ret_{H}" if ks.column is None else f"{ks.column}_{H}"
    tr, _ = T.daily_fold_rows(tg, fold, H, train_col)
    _, pr = T.daily_fold_rows(tg, fold, H, score_col)
    tr, pr = tr[panel.ok[tr]], pr[panel.ok[pr]]
    if not len(pr):
        raise SkipBlock("no scorable rows in the block")
    if not len(tr):
        raise SkipBlock("no training rows")
    n = len(tg)
    if ks.fitted is not None:
        spec = T.TargetSpec(ks.fitted, H, cell.ratio)
        mask = np.zeros(n, dtype=bool)
        mask[tr] = True
        try:
            ft = T.fit_daily_target(tg, spec, mask, panel.excursions(H) if cell.kind in _BARRIER_KINDS else None)
        except T.TargetBalanceError as e:
            raise SkipBlock(f"target not fittable: {e}") from e
        lab = ft.labels
        tr = tr[np.isfinite(lab[tr])]
        y_all = (lab == 2).astype(np.float64) if ks.task == "binary" else lab
        y_all = np.where(np.isfinite(lab), y_all, np.nan)
    else:
        spec = T.TargetSpec(_BALANCE_KIND, H)
        y_all = tg[train_col].to_numpy(dtype=np.float64)
    if len(tr) < 2:
        raise SkipBlock("fewer than 2 training rows")
    day = panel.sessions.to_numpy()
    tend = tg[f"target_end_{H}"].to_numpy()
    try:
        fit_m, val_m = W.validation_split(np.ones(len(tr), dtype=bool), day[tr], tend[tr], st.val_share)
    except ValueError as e:
        raise SkipBlock(f"validation split: {e}") from e
    fit_idx, val_idx = tr[fit_m], tr[val_m]
    if ks.task in ("binary", "multiclass"):
        binary = ks.task == "binary"
        counts = {"fit": _counts(y_all[fit_idx], binary), "val": _counts(y_all[val_idx], binary)}
        try:
            T.check_balance(spec, block.name, counts_by_part=counts, mode="min_count", min_count=st.min_count)
        except T.TargetBalanceError as e:
            raise SkipBlock(f"unbalanced target: {e}") from e
    return BlockRows(ks, spec, tg, tr, pr, y_all, fit_idx, val_idx, day, tend)


def fit_block(cell: Cell, seed: int, panel: Panel, block: W.Block, window: W.Window, st: GridSettings,
              n_jobs: int) -> BlockResult:
    """Fit on the purged window before ``block`` and score its rows. Raises ``SkipBlock`` (reason in the message)."""
    br = prepare_block(cell, panel, block, window, st)
    if cell.pool == "gpu":
        from app.services.ml_grid_seq import fit_block_seq
        return fit_block_seq(cell, seed, panel, br, block, window, st, n_jobs)
    ks, tg, pr, y_all, fit_idx, val_idx = br.ks, br.tg, br.pr, br.y_all, br.fit_idx, br.val_idx
    cols = panel.group_columns(cell.groups)
    names = [panel.columns[i] for i in cols]
    gt = gv = None
    if ks.task == "rank":
        grp = tg["session_group"].to_numpy()
        fit_idx, gt = _group_arrays(grp, fit_idx)
        val_idx, gv = _group_arrays(grp, val_idx)
    Xf, Xv = panel.X[np.ix_(fit_idx, cols)], panel.X[np.ix_(val_idx, cols)]
    adapter = M.make_adapter(cell.family, n_jobs, cell.params_dict)
    adapter.fit(Xf, y_all[fit_idx], Xv, y_all[val_idx], ks.task, seed, groups_train=gt, groups_val=gv,
                feature_names=names)
    scores = adapter.score(panel.X[np.ix_(pr, cols)])
    audit = block_audit(BlockRows(ks, br.spec, tg, br.tr, pr, y_all, fit_idx, val_idx, br.day, br.tend), block, window)
    return BlockResult(pr, scores, len(fit_idx), len(val_idx), adapter.n_iter, audit)


# ================================================================== metrics
def _stat(series: pd.Series, lag: int) -> Dict[str, Any]:
    s = pd.Series(series, dtype=float)
    return {"mean": float(s.mean()) if len(s.dropna()) else float("nan"), "t": E.newey_west_t(s, lag),
            "n": int(s.notna().sum())}


def _series_regimes(series: pd.Series, lag: int, min_rows: int) -> Dict[str, Dict[str, Any]]:
    """Per-regime ``{mean, t, n}`` of a daily series (key ``all`` = everything); a thin regime has NaN mean / t."""
    if not len(series):
        return {}
    means = E.by_regime(series, lambda s: float(s.mean()), min_rows)
    ts = E.by_regime(series, lambda s: E.newey_west_t(s, lag), min_rows)
    ns = E.by_regime(series, lambda s: float(len(s)), 1)
    return {r: {"mean": means[r], "t": ts[r], "n": int(ns[r])} for r in means}


def selection_metrics(d: pd.DataFrame, H: int, st: GridSettings) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    ic, ic_s = E.rank_ic(d, assume_flat=True)
    net = E.quintile_spread(d, st.cost_bps, assume_flat=True)
    gross = E.quintile_spread(d, 0.0, assume_flat=True)
    series = {"rank_ic": ic_s, "quintile_spread_net": net.series, "long_only_net": net.long_series,
              "quintile_spread_gross": gross.series}
    metrics = {k: _stat(v, H) for k, v in series.items()}
    regimes = {k: _series_regimes(v, H, st.regime_min_rows) for k, v in series.items()}
    return metrics, regimes, net.mean


def block_cutoff(d: pd.DataFrame, q: float, min_obs: int) -> np.ndarray:
    """Causal timing cutoff of every row of the stitched predictions ``d`` (columns symbol, session, score, block),
    computed block by block: block k's cutoffs use, per symbol, ONLY the scores of the EARLIER blocks (``history``) and
    of its own earlier sessions -- never a later block's. Equal to one ``panel_expanding_cutoff`` pass over the whole
    frame (checked in the tests); written out so the look-ahead rule is explicit. NaN until a symbol has ``min_obs``
    earlier scores."""
    start = d.groupby("block", sort=False)["session"].min().sort_values()
    out = np.full(len(d), np.nan)
    key = d[["symbol", "session", "score"]]
    for name, first in start.items():
        mine = (d["block"] == name).to_numpy()
        hist = key[(d["session"] < first).to_numpy()]
        out[mine] = E.panel_expanding_cutoff(key[mine], q, min_obs, history=hist if len(hist) else None).to_numpy()
    return out


def timing_metrics(d: pd.DataFrame, H: int, st: GridSettings) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    d = d.assign(cutoff=block_cutoff(d, st.timing_q, st.min_obs))
    kw = dict(cutoff_col="cutoff", assume_flat=True)

    def one(frame: pd.DataFrame, cost: float) -> Tuple[E.Timing, float]:
        return E.timing_excess(frame, cost, **kw), E.timing_sharpe(frame, H, cost, **kw)

    def stats(frame: pd.DataFrame) -> Dict[str, Any]:
        net, sh = one(frame, st.cost_bps)
        gross = E.timing_excess(frame, 0.0, **kw)
        return {"timing_excess_net": {**_stat(net.series, H), "mean": net.excess, "in_share": net.in_share,
                                      "n_in": net.n_in},
                "timing_excess_gross": {**_stat(gross.series, H), "mean": gross.excess},
                "timing_sharpe_net": {"mean": sh, "t": float("nan"), "n": int(frame["session"].nunique())}}
    overall = stats(d)
    overall["timing_excess_by_symbol"] = {k: float(v) for k, v in
                                          E.timing_by_symbol(d, st.cost_bps, **kw)["excess"].items()}
    names = W.regimes(d["session"])
    regimes: Dict[str, Dict[str, Any]] = {k: {} for k in ("timing_excess_net", "timing_excess_gross",
                                                         "timing_sharpe_net")}
    for r in ["all"] + [x for x in W.REGIMES if (names == x).any()]:
        sub = d if r == "all" else d[names == r]
        if len(sub) < st.regime_min_rows:
            nan = {"mean": float("nan"), "t": float("nan"), "n": int(sub["session"].nunique())}
            for k in regimes:
                regimes[k][r] = dict(nan)
            continue
        s = overall if r == "all" else stats(sub)
        for k in regimes:
            regimes[k][r] = s[k]
    return {k: overall[k] for k in overall}, regimes, overall["timing_excess_net"]["mean"]


def volatility_metrics(d: pd.DataFrame, H: int, st: GridSettings) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    d = d.dropna(subset=["score", "realvol"])
    if d.empty:
        return {}, {}, float("nan")

    def pooled(f: pd.DataFrame) -> float:
        return float(f["score"].corr(f["realvol"], method="spearman")) if len(f) > 2 else float("nan")

    within = [pooled(f) for _, f in d.groupby("symbol", sort=True) if len(f) > 20]
    metrics = {"spearman": {"mean": pooled(d), "n": int(len(d))},
               "spearman_within_symbol": {"mean": float(np.nanmean(within)) if within else float("nan"),
                                          "n": len(within)}}
    mr = max(st.regime_min_rows, 3)
    vals, ns = E.by_regime(d, pooled, mr), E.by_regime(d, lambda f: float(len(f)), 1)
    regimes = {"spearman": {r: {"mean": vals[r], "n": int(ns[r])} for r in vals}}
    return metrics, regimes, metrics["spearman"]["mean"]


def regime_metric(track: str, st: GridSettings) -> Callable[[pd.DataFrame], Dict[str, float]]:
    """Frame (symbol, session, score + outcomes) -> ``{"all": value, regime: value}`` of the track's primary metric,
    defined exactly as the row's own ``regimes`` (``selection_metrics`` / ``timing_metrics``): the mean of the daily
    spread series per regime, resp. the timing excess of the regime's rows under the cutoff computed on the WHOLE frame
    (after any shuffle, before slicing). A regime with fewer than ``regime_min_rows`` is NaN. Used for the controls so
    a regime's null is measured the way the regime's value is."""
    if track == "selection":
        def selection(d: pd.DataFrame) -> Dict[str, float]:
            sp = E.quintile_spread(d, st.cost_bps, assume_flat=True)
            per = E.by_regime(sp.series, lambda s: float(s.mean()), st.regime_min_rows) if len(sp.series) else {}
            return {**{r: v for r, v in per.items() if r != "all"}, "all": sp.mean}
        return selection

    def timing(d: pd.DataFrame) -> Dict[str, float]:
        d = E.with_cutoff(d, st.timing_q, st.min_obs, assume_flat=True)
        out = {"all": E.timing_excess(d, st.cost_bps, cutoff_col="cutoff", assume_flat=True).excess}
        names = W.regimes(d["session"])
        for r in W.REGIMES:
            if (names == r).any():
                sub = d[names == r]
                out[r] = (float("nan") if len(sub) < st.regime_min_rows else
                          E.timing_excess(sub, st.cost_bps, cutoff_col="cutoff", assume_flat=True).excess)
        return out
    return timing


def _strict_by_regime(rep: Dict[str, Any], names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Per regime: ``null_means`` = EVERY gating control's null mean (the gate takes the highest, per seed), and for
    DISPLAY the control with the larger one-sided p among ``names`` (the whole period's rule, so the same control the
    whole-period p comes from) with its null mean / std, p, rank and the model's value. A larger p does not mean a
    higher null mean, so ``null_mean`` / ``null_std`` here are not the strictest null; ``null_means`` is. A regime
    where any control has no finite p reports the last control and no p."""
    out: Dict[str, Dict[str, Any]] = {}
    for reg in rep[names[0]]["by_regime"]:
        ents = {n: rep[n]["by_regime"][reg] for n in names}
        if any(e["p_value"] is None for e in ents.values()):
            strict, p = names[-1], None
        else:
            strict = max(names, key=lambda n: (ents[n]["p_value"], n == names[-1]))
            p = ents[strict]["p_value"]
        e = ents[strict]
        out[reg] = {"control": strict, "null_mean": e["mean"], "null_std": e["std"], "p_value": p, "rank": e["rank"],
                    "model": e["model"], "null_means": {n: ents[n]["mean"] for n in names}}
    return out


def run_controls(d: pd.DataFrame, cell: Cell, st: GridSettings) -> Dict[str, Any]:
    if cell.kind == "volatility":
        return {"skipped": "a volatility model is a sizing model: the trading shuffle controls do not apply"}
    if cell.track == "selection":
        outcomes, primary = d[["symbol", "session", "rel_ret", "vol"]], "shuffle_symbols"
    else:
        outcomes, primary = d[["symbol", "session", "ret", "vol"]], "shuffle_time"       # + the within-block null below
    timing = cell.track == "timing"
    rep = controls_report(d[["symbol", "session", "score"] + (["block"] if timing else [])], outcomes, None,
                          seeds=st.control_seeds, within_block=timing, regime_fn=regime_metric(cell.track, st))
    names = ("shuffle_time", "shuffle_time_within_block") if timing else (primary,)
    ps = {n: rep[n]["p_value"] for n in names}
    if any(v is None for v in ps.values()):
        strict = names[-1]                                    # a non-finite null: report it, no p-value
        value = None
    else:
        strict = max(names, key=lambda n: (ps[n], n == names[-1]))      # the stricter null = the larger p
        value = ps[strict]
    rep["primary"], rep["p_value"], rep["rank"] = strict, value, rep[strict]["rank"]
    rep["p_values"] = ps
    rep["by_regime"] = _strict_by_regime(rep, names)
    return rep


def evaluate(pred: pd.DataFrame, cell: Cell, st: GridSettings) -> Dict[str, Any]:
    """Metrics, regimes, controls and the primary metric of stitched out-of-sample predictions ``pred`` (columns
    symbol, session, score and the outcomes ``ret`` / ``vol`` [/ ``rel_ret`` / ``realvol``])."""
    d = E.flat(pred, ("score",))
    d = d[np.isfinite(d["score"].to_numpy(dtype=float))].reset_index(drop=True)
    H = cell.horizon
    if cell.kind == "volatility":
        fn, name = volatility_metrics, VOLATILITY_PRIMARY
    elif cell.track == "selection":
        fn, name = selection_metrics, PRIMARY["selection"]
    else:
        fn, name = timing_metrics, PRIMARY["timing"]
    t0 = time.perf_counter()
    metrics, regimes, primary = fn(d, H, st)
    t1 = time.perf_counter()
    controls = run_controls(d, cell, st) if len(d) else {"skipped": "no predictions"}
    return {"metrics": metrics, "regimes": regimes, "controls": controls,
            "primary": {"name": name, "value": primary}, "seconds_metrics": t1 - t0,
            "seconds_controls": time.perf_counter() - t1}


# ================================================================== one (cell, seed)
def _sanitize(x: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, NaN / inf to None."""
    if isinstance(x, dict):
        return {str(k): _sanitize(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_sanitize(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(x) if math.isfinite(float(x)) else None
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (date, pd.Timestamp)):
        return str(x)
    return x


def _predictions_frame(cell: Cell, panel: Panel, rows: np.ndarray, scores: np.ndarray, block_of: np.ndarray
                       ) -> pd.DataFrame:
    H = cell.horizon
    tg = panel.targets(H)
    idx = tg.index[rows]
    out = {"symbol": idx.get_level_values("symbol").to_numpy(), "session": idx.get_level_values("session").to_numpy(),
           "block": block_of, "score": scores}
    for c in ("ret", "vol", "rel_ret") + (("realvol",) if cell.kind == "volatility" else ()):
        if f"{c}_{H}" in tg.columns:
            out[c] = tg[f"{c}_{H}"].to_numpy(dtype=np.float64)[rows]
    return pd.DataFrame(out).sort_values(["session", "symbol"], kind="mergesort").reset_index(drop=True)


_CODE_MODULES = ("ml_grid", "ml_models", "ml_targets", "ml_eval", "ml_walkforward", "ml_controls", "ml_features",
                 "ml_grid_seq", "ml_models_seq", "ml_panel", "ml_search")


@lru_cache(maxsize=1)
def library_versions() -> Dict[str, Any]:
    """Audit fields: the Python and library versions a row was computed with, and a hash of the sources of the ML
    modules (``code``). Not part of the resume identity: a library upgrade is visible, not a forced rerun."""
    import platform
    from importlib.metadata import PackageNotFoundError, version
    out: Dict[str, Any] = {"python": platform.python_version()}
    for dist, key in (("numpy", "numpy"), ("pandas", "pandas"), ("pyarrow", "pyarrow"), ("scikit-learn", "sklearn"),
                      ("scipy", "scipy"), ("lightgbm", "lightgbm"), ("xgboost", "xgboost"), ("catboost", "catboost"),
                      ("threadpoolctl", "threadpoolctl"), ("torch", "torch"), ("darts", "darts"),
                      ("pytorch-lightning", "pytorch_lightning"), ("tsai", "tsai")):
        try:
            out[key] = version(dist)
        except PackageNotFoundError:
            out[key] = None
    h = hashlib.sha1()
    for mod in _CODE_MODULES:
        h.update((Path(__file__).parent / f"{mod}.py").read_bytes().replace(b"\r\n", b"\n"))
    out["code"] = h.hexdigest()[:12]
    return out


#: a GPU task is aborted after block k when elapsed / k x n_blocks exceeds this many budgets
BUDGET_PROJECTION = 1.5
#: the master kills a GPU task that has been in flight longer than this many budgets
HARD_DEADLINE_BUDGETS = 3.0

#: the ``GridSettings`` only a GPU family reads (a tabular row's identity leaves them out)
SEQ_SETTINGS: Tuple[str, ...] = ("seq_lookback", "seq_samples_per_session", "darts_lookback", "gpu_max_epochs")


class BudgetExceeded(RuntimeError):
    """A GPU (cell, seed) ran past ``GridSettings.gpu_budget_minutes``."""


def identity_of(cell: Cell, st: GridSettings, daily_fp: str, intraday_fp: str = "") -> Dict[str, Any]:
    """What a finished row was computed from, for the resume check: the settings (not ``cores``), the store signature,
    the daily cache fingerprint and, for a tsai cell, the fingerprint of the 5-minute bars it reads (a content hash,
    ``ml_grid_seq.intraday_fingerprint``)."""
    settings = st.to_dict()
    for name in ("cores", "gpu_budget_minutes"):             # how many threads / how long it may take: not what it computes
        del settings[name]
    if cell.pool != "gpu":                                    # a tabular row does not depend on the sequence settings
        for name in SEQ_SETTINGS:
            del settings[name]
    out = {"settings": settings, "store_signature": cell.signature, "daily_fingerprint": daily_fp}
    if split_family(cell.family)[0] == "tsai":
        out["intraday_fingerprint"] = intraday_fp
    return _sanitize(out)


def run_cell_seed(cell: Cell, seed: int, panel: Panel, st: GridSettings, n_jobs: int,
                  pred_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One (cell, seed): walk-forward fit, stitch, evaluate. Returns the ``results.jsonl`` row (and writes the
    predictions parquet first when ``pred_dir`` is given)."""
    t_start = time.perf_counter()
    gpu = cell.pool == "gpu"
    if gpu and cell.blocks != "H":
        raise ValueError(f"a GPU family trains on half-year blocks (blocks: 'H'), got {cell.blocks!r}")
    intraday_fp = ""
    if split_family(cell.family)[0] == "tsai":
        from app.services.ml_grid_seq import intraday_fingerprint
        syms = tuple(sorted(resolve_symbols(cell.symbols)))
        intraday_fp = intraday_fingerprint(panel.cache_root, syms, st.eval_end, store_span(cell.store, syms)[1])
    blocks = W.blocks(st.eval_start, st.eval_end, cell.blocks)
    skipped: List[Dict[str, str]] = []
    clipped: List[Dict[str, str]] = []
    info: List[Dict[str, Any]] = []
    rows_l, scores_l, block_l = [], [], []
    t_fit = 0.0
    budget_s = st.gpu_budget_minutes * 60.0
    excluded = 0.0                  # seconds the budget clock does not count (the intraday panel build: shared work)

    def elapsed() -> float:
        return time.perf_counter() - t_start - excluded
    for b in blocks:
        if gpu:
            done_blocks = len(info) + len(skipped)
            if elapsed() > budget_s:
                raise BudgetExceeded(f"time budget of {st.gpu_budget_minutes:g} minutes exceeded after "
                                     f"{done_blocks} of {len(blocks)} blocks ({elapsed():.0f} s)")
            if done_blocks and elapsed() / done_blocks * len(blocks) > BUDGET_PROJECTION * budget_s:
                raise BudgetExceeded(f"time budget of {st.gpu_budget_minutes:g} minutes: {elapsed():.0f} s for "
                                     f"{done_blocks} of {len(blocks)} blocks, projected "
                                     f"{elapsed() / done_blocks * len(blocks):.0f} s in all, over {BUDGET_PROJECTION:g} x the budget")
        win = W.train_window(b, cell.train_months, st.data_start)
        if win.clipped:
            clipped.append({"block": b.name, "window_start": str(win.start)})
        t0 = time.perf_counter()
        try:
            res = fit_block(cell, seed, panel, b, win, st, n_jobs)
        except SkipBlock as e:
            skipped.append({"block": b.name, "reason": str(e)})
            continue
        finally:
            t_fit += time.perf_counter() - t0
        if gpu and "intraday_build_seconds" in res.audit:
            excluded += res.audit["intraday_build_seconds"]
        rows_l.append(res.rows)
        scores_l.append(res.scores)
        block_l.append(np.full(len(res.rows), b.name, dtype=object))
        bi = best_iter(res.n_iter)
        entry = {"block": b.name, "n_fit": res.n_fit, "n_val": res.n_val, "n_iter": bi, "n_pred": int(len(res.rows)),
                 **res.audit}
        if isinstance(res.n_iter, (list, tuple)):
            entry["n_iter_levels"] = [int(i) for i in res.n_iter]
        info.append(entry)
    iters = [b["n_iter"] for b in info if b["n_iter"] is not None]
    row: Dict[str, Any] = {"cell_key": cell.key, "seed": int(seed), "track": cell.track, "label": cell.label,
                           "config": cell.config(), "identity": identity_of(cell, st, panel.daily_fingerprint, intraday_fp),
                           "versions": library_versions(),
                           "deterministic": cell.family in DETERMINISTIC_FAMILIES,
                           "seeds_run": len(effective_seeds(cell)),
                           "low_iter_share": float(np.mean([i <= LOW_ITER for i in iters])) if iters else None,
                           "n_blocks": len(blocks), "n_blocks_fitted": len(info),
                           "skipped_blocks": skipped, "clipped_blocks": clipped, "blocks": info,
                           "n_features": (len(panel.group_columns(cell.groups)) if not gpu or
                                          split_family(cell.family)[0] == "darts"
                                          else (info[0]["n_channels"] if info else 0)),
                           "symbols": list(panel.symbols)}
    if gpu:
        from app.services.ml_grid_seq import gpu_versions
        row["versions"] = {**row["versions"], **gpu_versions()}
        row["over_budget"] = elapsed() > budget_s              # finished work is kept; the flag says it was slow
        ops = sorted({op for b in info for op in (b["nondeterministic_ops"] if "nondeterministic_ops" in b else [])})
        row["gpu_nondeterministic"], row["nondeterministic_ops"] = bool(ops), ops
    if not rows_l:
        row.update({"status": "no_predictions", "n_rows": 0, "primary": {"name": None, "value": None},
                    "metrics": {}, "regimes": {}, "controls": {}})
        row["timings"] = {"fit": t_fit, "panel_load": panel.load_seconds, "total": time.perf_counter() - t_start}
        return _sanitize(row)
    pred = _predictions_frame(cell, panel, np.concatenate(rows_l), np.concatenate(scores_l), np.concatenate(block_l))
    floats = [c for c in pred.columns if pred[c].dtype == np.float64]
    stored = pred.astype({c: np.float32 for c in floats})
    pred = stored.astype({c: np.float64 for c in floats})            # the metrics see EXACTLY what the file holds
    if pred_dir is not None:
        pred_dir.mkdir(parents=True, exist_ok=True)
        final = pred_dir / f"{cell.key}_{seed}.parquet"
        tmp = final.with_name(f"{final.name}.{os.getpid()}.tmp")           # unique per process: no two writers share one
        try:
            stored.to_parquet(tmp, index=False, compression="zstd")
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
    ev = evaluate(pred, cell, st)
    row.update({"status": "ok", "n_rows": int(len(pred)), "primary": ev["primary"], "metrics": ev["metrics"],
                "regimes": ev["regimes"], "controls": ev["controls"]})
    row["timings"] = {"fit": t_fit, "metrics": ev["seconds_metrics"], "controls": ev["seconds_controls"],
                      "panel_load": panel.load_seconds, "total": time.perf_counter() - t_start}
    if gpu:
        for key, field_name in (("block_fit", "fit_seconds"), ("block_prep", "prep_seconds"),
                                ("intraday_build", "intraday_build_seconds")):
            row["timings"][key] = float(sum(b[field_name] for b in info if field_name in b))
    return _sanitize(row)


# ================================================================== the runner
_WORKER: Dict[str, Any] = {}
#: warnings known to be noise in a grid worker, as (message regex, category): nothing else is silenced
NOISY_WARNINGS: Tuple[Tuple[str, type], ...] = ()
#: seconds the master waits on the pool before it looks at cancellation again
POLL_SECONDS = 1.0
#: worker i (in the order the pool initialised them) waits i x this before its FIRST panel load, so the workers do not
#: all parse the same parquet files and peak in memory together
PANEL_STAGGER_SECONDS = 1.5
#: a task whose worker dies is retried this many times, then it becomes an error record
MAX_RETRIES = 3
#: relative cost of a family's fit (slowest first); quantile (nine boosters) counts as one step slower
FAMILY_COST: Dict[str, int] = {"catboost": 4, "xgb": 3, "lgbm": 2, "linear": 1, "naive": 0,
                               **{f: 6 for f in TSAI_FAMILIES}, **{f: 5 for f in DARTS_FAMILIES}}


def _panel_for(cell: Cell, cache_root: str) -> Panel:
    if not _WORKER["staggered"]:
        _WORKER["staggered"] = True
        if _WORKER["index"] > 0:
            time.sleep(_WORKER["index"] * PANEL_STAGGER_SECONDS)
    return get_panel(cell.store, resolve_symbols(cell.symbols), cell.track, cache_root)


def _init_worker(settings: Dict[str, Any], cache_root: str, out_dir: str, n_jobs: int, counter: Any = None,
                 workers: int = 0) -> None:
    """Pool initializer: remember the run constants, cap the BLAS / OpenMP threads at ``n_jobs`` (so slots x n_jobs
    never exceeds the cores) and silence only ``NOISY_WARNINGS``. Panels are NOT loaded here: the task that needs one
    loads it (``get_panel``, an LRU of ``PANEL_CACHE_SIZE``). ``counter`` (a shared ``multiprocessing.Value`` of the
    pool) numbers the workers 0, 1, 2 ... for the staggered first panel load; without one the worker is number 0. With
    ``workers`` (the pool size) only the FIRST generation is staggered: a replacement worker (after a break, a
    ``max_tasks_per_child`` retirement or a termination) is number 0, so it never waits for a stagger that is over."""
    import warnings
    index = 0
    if counter is not None:
        with counter.get_lock():
            index = counter.value
            counter.value += 1
        if workers > 0 and index >= workers:
            index = 0
    for message, category in NOISY_WARNINGS:
        warnings.filterwarnings("ignore", message=message, category=category)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = str(n_jobs)
    try:
        from threadpoolctl import threadpool_limits
        _WORKER["limits"] = threadpool_limits(limits=n_jobs)
    except ImportError:
        logger.warning("threadpoolctl is not installed: BLAS / OpenMP threads are limited by environment variables only")
    _WORKER.update(settings=GridSettings.from_dict(settings), cache_root=cache_root, out_dir=out_dir, n_jobs=n_jobs,
                   index=index, staggered=False, poisoned=False)


def _error_record(cell_key: str, seed: int, message: str, label: str = "", tb: str = "") -> Dict[str, Any]:
    return {"cell_key": cell_key, "seed": int(seed), "error": message, "traceback": tb, "label": label}


def _label_of(cfg: Dict[str, Any]) -> str:
    try:
        return (f"{cfg['track']} {cfg['target']['kind']} h{cfg['target']['horizon']} {cfg['family']} "
                f"{cfg['train_months']} {'+'.join(cfg['groups'])} {cfg['blocks']}")
    except (KeyError, TypeError):
        return ""


def _run_task(cfg: Dict[str, Any], seed: int, expected_key: str) -> Dict[str, Any]:
    """Pool task: one (cell, seed). EVERYTHING, validation included, is inside the try: whatever raises becomes an
    ``error`` record and the grid goes on. The worker rebuilds the cell from its config and refuses it when the key it
    gets differs from the planned ``expected_key`` (the store changed between planning and running).

    GPU cells (the cell's family routes to the gpu pool): a worker poisoned by an earlier sticky CUDA error exits at the
    start of its next task (``os._exit``: the pool breaks and is rebuilt, a fresh process gets a fresh CUDA context);
    a failure is classified (``ml_grid_seq.classify_gpu_error``: ``cuda_error`` of the error record is ``"oom"`` or
    ``"sticky"``; a sticky one poisons this worker); and after the task, with the exception gone, the worker collects
    garbage and empties the CUDA cache. A budget overrun is an error record with ``timeout: True``."""
    if "poisoned" in _WORKER and _WORKER["poisoned"]:
        os._exit(1)
    record: Optional[Dict[str, Any]] = None
    gpu = isinstance(cfg, dict) and "family" in cfg and cfg["family"] in GPU_FAMILIES
    try:
        cell = validate_cell(cfg)
        if seed not in cell.seeds:
            raise RuntimeError(f"seed {seed} is not one of the cell's seeds {list(cell.seeds)}")
        if cell.key != expected_key:
            raise RuntimeError(f"cell key mismatch in the worker: planned {expected_key}, validated {cell.key} "
                               f"(did the store change after the plan was made?)")
        panel = _panel_for(cell, _WORKER["cache_root"])
        record = run_cell_seed(cell, seed, panel, _WORKER["settings"], _WORKER["n_jobs"],
                               Path(_WORKER["out_dir"]) / "predictions")
    except Exception as e:                                       # noqa: BLE001  (one bad cell must not stop the grid)
        record = _error_record(expected_key, seed, f"{type(e).__name__}: {e}", _label_of(cfg), traceback.format_exc())
        if isinstance(e, BudgetExceeded):
            record["timeout"] = True
        if gpu:
            from app.services.ml_grid_seq import classify_gpu_error
            kind = classify_gpu_error(e)
            if kind is not None:
                record["cuda_error"] = kind
                if kind == "sticky":
                    _WORKER["poisoned"] = True
    if gpu:                                                      # the exception (and the tensors in its frames) is gone
        from app.services.ml_grid_seq import cleanup_gpu
        cleanup_gpu()
    return record


def effective_seeds(cell: Cell) -> Tuple[int, ...]:
    """The seeds actually run: all of them, except for a deterministic family (naive / linear), which runs once with
    the lowest seed (its other seeds would only repeat it)."""
    return (min(cell.seeds),) if cell.family in DETERMINISTIC_FAMILIES else cell.seeds


def plan_tasks(cells: Sequence[Cell]) -> List[Tuple[Cell, int]]:
    """(cell, seed) tasks ordered for the workers: cells sharing a panel (store, symbols, track) are consecutive, so a
    worker's panel cache is reused, panels in order of first appearance; inside a panel the slowest families first
    (``FAMILY_COST``, quantile one step slower), so a long fit does not start last."""
    groups: "OrderedDict[Tuple, List[Cell]]" = OrderedDict()
    for c in cells:
        groups.setdefault((c.store, c.symbols, c.track), []).append(c)
    out: List[Tuple[Cell, int]] = []
    for members in groups.values():
        for c in sorted(members, key=lambda c: -(2 * FAMILY_COST[c.family] + (c.kind == "quantile"))):
            out += [(c, sd) for sd in effective_seeds(c)]
    return out


# ------------------------------------------------------------------ lock file
class GridLockedError(RuntimeError):
    """Another live run holds the output directory."""


def pid_alive(pid: int) -> bool:
    """Is a process with this pid running? (``os.kill(pid, 0)`` would TERMINATE it on Windows, so it is not used.)"""
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))         # SYNCHRONIZE
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102        # WAIT_TIMEOUT: still running
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


LOCK_NAME = ".ml_grid.lock"


#: reading a lock another process may be writing: this many tries, this far apart, before it is called unreadable
LOCK_READ_TRIES = 5
LOCK_READ_WAIT = 0.1


def _read_lock(path: Path) -> Optional[Dict[str, Any]]:
    """The lock's content; None when the file is gone; ``{}`` when it stays unreadable (empty, half-written) after
    ``LOCK_READ_TRIES`` tries ``LOCK_READ_WAIT`` apart: a holder that is mid-write is given a moment."""
    for i in range(LOCK_READ_TRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            if i + 1 < LOCK_READ_TRIES:
                time.sleep(LOCK_READ_WAIT)
    return {}


def _create_lock(path: Path, tmp: Path) -> bool:
    """Put the already complete ``tmp`` at ``path`` atomically (a hard link fails if ``path`` exists); False if the
    lock exists. A filesystem without hard links falls back to an exclusive create + write (readers then rely on the
    patient read above)."""
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    except OSError:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tmp.read_text(encoding="utf-8"))
        return True


def acquire_lock(out: Path) -> Path:
    """Create ``out/.ml_grid.lock`` (pid, host, start time) or raise ``GridLockedError`` when a live run holds it. The
    content is written to a temp file first and linked into place, so no reader ever sees a half-written lock. A lock
    whose pid is dead on this host (or that stays unreadable) is stale and taken over, after a re-read confirms it is
    still the same stale lock; one from another host cannot be judged and is respected."""
    import uuid
    path = out / LOCK_NAME
    me = {"pid": os.getpid(), "host": socket.gethostname(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = out / f"{LOCK_NAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(me), encoding="utf-8")
    try:
        for _ in range(3):
            if _create_lock(path, tmp):
                return path
            held = _read_lock(path)
            if held is None:                                       # it vanished between the two calls: try again
                continue
            if not held:
                held = {"pid": -1, "host": me["host"], "started": "unreadable lock"}
            if held["host"] == me["host"] and not pid_alive(int(held["pid"])):
                if _read_lock(path) == (held if held["pid"] != -1 else {}):          # still the same stale lock
                    logger.warning(f"ml_grid: stale lock of dead pid {held['pid']} (started {held['started']}) removed")
                    path.unlink(missing_ok=True)
                continue
            raise GridLockedError(f"another ml_grid run is already running in {out} (pid {held['pid']} on "
                                  f"{held['host']}, started {held['started']}); remove {path} only if it is not")
        raise GridLockedError(f"could not take the lock {path}")
    finally:
        tmp.unlink(missing_ok=True)


def release_lock(path: Path) -> None:
    """Remove the lock if it is still ours."""
    try:
        if json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid():
            path.unlink()
    except (OSError, json.JSONDecodeError, KeyError):
        pass


# ------------------------------------------------------------------ cancellation
def make_cancel_check(task_id: str, interval: float = 5.0) -> Callable[[], bool]:
    """A ``cancel_check`` for ``run_grid``: True once the queue task ``task_id`` has status ``cancelled`` (polled at
    most every ``interval`` seconds). Outside the queue (the CLI) there is no row and it stays False."""
    state = {"t": -float("inf"), "v": False}

    def check() -> bool:
        now = time.monotonic()
        if now - state["t"] < interval:
            return state["v"]
        state["t"] = now
        try:
            from app.services.task_queue import get_task_queue
            row = get_task_queue().get_task_status(task_id)
            state["v"] = bool(row) and row["status"] == "cancelled"
        except Exception as e:                                    # noqa: BLE001  (no queue / no row: not cancelled)
            logger.debug(f"cancel check: no task status for {task_id}: {e}")
        return state["v"]
    return check


# ------------------------------------------------------------------ the pool driver
def _terminate_pool(pool: Any) -> None:
    """Kill the worker processes of ``pool`` (``shutdown`` only stops idle ones): used when a task is stuck or the run is
    cancelled, where a running task's result would be thrown away anyway."""
    terminate = getattr(pool, "terminate_workers", None)               # Python 3.14
    if terminate is not None:
        terminate()
        return
    procs = getattr(pool, "_processes", None)                          # 3.12: no public API
    for p in list(procs.values() if procs else []):
        try:
            p.terminate()
        except Exception:                                              # noqa: BLE001  (already gone)
            pass


def _drive_pool(tasks: Sequence[Tuple[Cell, int]], cpu_slots: int, init_args: Tuple, task_fn: Callable,
                finish: Callable[[Dict[str, Any]], None], cancelled: Callable[[], bool],
                summary: Dict[str, Any], deadline: Optional[float] = None, window_factor: int = 2,
                max_tasks_per_child: Optional[int] = None) -> None:
    """Run ``tasks`` on a spawn-context process pool with at most ``window_factor x cpu_slots`` futures in flight
    (default 2; the GPU lane uses 1, so a submitted task starts at once and ``deadline`` can run from its submission).

    ``deadline`` (seconds, None = none): a task in flight longer than that is recorded as a TIMEOUT error and the pool's
    workers are TERMINATED and the pool rebuilt; the other in-flight tasks go back to the queue UNCHARGED (they did
    nothing wrong). ``max_tasks_per_child``: workers are replaced after that many tasks (a long-lived CUDA process
    leaks). On cancellation or a master exception the workers are terminated too (a stuck kernel would otherwise keep
    the process, and the GPU, busy for hours).

    A worker that dies (``BrokenProcessPool``) takes the whole pool with it: every in-flight task is charged one
    attempt, the pool is rebuilt and the tasks go back to the front of the queue. After a break the pool runs on
    PROBATION: a window of ``max(1, cpu_slots // 4)`` while any retried task is in flight or waiting, so a crowd is not
    thrown at the poison again; a task already charged TWICE runs alone on an otherwise idle pool, which isolates the
    one that kills its worker. After ``MAX_RETRIES`` retries a task becomes an error record. A submit that finds the
    pool already broken puts the task back and rebuilds. ``finish`` (which writes to disk) is never called inside a
    ``try`` that reads ``except`` as "the pool broke": an ``OSError`` from it propagates. The pool is always shut down
    with ``wait=False, cancel_futures=True``, so a master exception or Ctrl-C leaves nothing queued."""
    window = window_factor * cpu_slots
    probation_window = max(1, cpu_slots // 4)
    queue: deque = deque((c, s, 0) for c, s in tasks)                 # (cell, seed, times its pool broke)
    retry: deque = deque()
    inflight: Dict[Any, Tuple[Cell, int, int]] = {}
    order = {(c.key, sd): i for i, (c, sd) in enumerate(tasks)}
    submitted: Dict[Any, float] = {}                                  # future -> when it went to the pool
    workers = min(cpu_slots, len(tasks))

    def new_pool() -> ProcessPoolExecutor:
        ctx = multiprocessing.get_context("spawn")
        extra = {} if max_tasks_per_child is None else {"max_tasks_per_child": max_tasks_per_child}
        return ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init_worker,
                                   initargs=(*init_args, ctx.Value("i", 0), workers), **extra)

    def limit() -> int:
        if any(a >= 2 for _, _, a in inflight.values()):
            return 1
        if retry or any(a > 0 for _, _, a in inflight.values()):
            return probation_window
        return window
    summary["pool_rebuilds"] = 0
    pool = new_pool()
    abnormal = False
    try:
        while queue or retry or inflight:
            if cancelled():
                summary["cancelled"] = True
                abnormal = True
                return
            rebuild = False
            while (retry or queue) and len(inflight) < limit():
                src = retry if retry else queue
                c, sd, a = src[0]
                if a >= 2 and inflight:
                    break                                             # charged twice: only on an idle pool
                src.popleft()
                try:
                    fu = pool.submit(task_fn, c.config(with_seeds=True), sd, c.key)
                except BrokenProcessPool:
                    src.appendleft((c, sd, a))
                    rebuild = True
                    break
                inflight[fu] = (c, sd, a)
                submitted[fu] = time.monotonic()
            done, _ = wait(list(inflight), timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
            broken: List[Tuple[Cell, int, int]] = []
            for fu in done:
                c, sd, a = inflight.pop(fu)
                try:
                    r = fu.result()
                except BrokenProcessPool:
                    broken.append((c, sd, a))
                    continue
                except Exception as e:                            # noqa: BLE001  (e.g. an unpicklable result)
                    r = _error_record(c.key, sd, f"{type(e).__name__}: {e}", c.label, traceback.format_exc())
                finish(r)                                         # outside the try: an OSError here is not "broken"
            late = [] if deadline is None else [fu for fu in inflight if time.monotonic() - submitted[fu] > deadline]
            if late:                                              # stuck: record the timeouts, kill the pool, requeue the rest
                for fu in late:
                    c, sd, a = inflight.pop(fu)
                    rec = _error_record(c.key, sd, f"hard deadline of {deadline:.0f} s exceeded: the task was stuck "
                                                   f"and its worker process was terminated", c.label)
                    rec["timeout"] = True
                    finish(rec)
                _terminate_pool(pool)
                pool.shutdown(wait=False, cancel_futures=True)
                innocent = []
                for fu, (c, sd, a) in list(inflight.items()):
                    if fu.done() and not fu.cancelled() and fu.exception() is None:
                        finish(fu.result())
                    else:
                        innocent.append((c, sd, a))               # not at fault: back to the queue, no attempt charged
                inflight.clear()
                submitted.clear()
                for c, sd, a in sorted(innocent, key=lambda t: order[(t[0].key, t[1])], reverse=True):
                    (retry if a else queue).appendleft((c, sd, a))
                logger.warning(f"ml_grid: {len(late)} task(s) passed the hard deadline of {deadline:.0f} s; the pool's "
                               f"workers were terminated, rebuilding")
                summary["pool_rebuilds"] += 1
                pool = new_pool()
                continue
            if not broken and not rebuild:
                continue
            for fu, (c, sd, a) in list(inflight.items()):         # the rest of the pool died with it: collect them
                wait([fu], timeout=60)
                try:
                    r = fu.result(timeout=0)
                except Exception:                                 # noqa: BLE001  (broken, cancelled or not finished)
                    broken.append((c, sd, a))
                else:
                    finish(r)
                inflight.pop(fu)
            submitted.clear()
            pool.shutdown(wait=False, cancel_futures=True)
            broken.sort(key=lambda t: order[(t[0].key, t[1])])      # futures complete in any order: retry in plan order
            for c, sd, a in broken:
                if a + 1 > MAX_RETRIES:
                    finish(_error_record(c.key, sd, f"worker process died on {a + 1} attempts ({MAX_RETRIES} retries); "
                                                     f"the task is given up", c.label))
                else:
                    retry.append((c, sd, a + 1))
            logger.warning(f"ml_grid: the worker pool broke ({len(broken)} task(s) in flight); rebuilding")
            summary["pool_rebuilds"] += 1
            pool = new_pool()
    except BaseException:
        abnormal = True
        raise
    finally:
        if abnormal:                                                  # cancelled / failed: running tasks are not wanted
            _terminate_pool(pool)
        pool.shutdown(wait=False, cancel_futures=True)


#: a GPU worker process is replaced after this many tasks (a long-lived CUDA process leaks memory)
GPU_MAX_TASKS_PER_CHILD = 40
#: consecutive CUDA-flavoured error records after which the master aborts the GPU lane
GPU_BREAKER_LIMIT = 10


class GpuBreaker:
    """Circuit breaker of the GPU lane: ``record`` every row the lane finishes; ``limit`` CONSECUTIVE error records with a
    ``cuda_error`` trip it (a finished row resets the count; an ordinary error neither counts nor resets). Something is
    wrong with the device or the driver, not with the cells, and a multi-day run must not burn through its tasks."""

    def __init__(self, limit: int) -> None:
        self.limit, self.count, self.tripped = limit, 0, False

    def record(self, row: Dict[str, Any]) -> bool:
        if "error" in row:
            if "cuda_error" in row and row["cuda_error"]:
                self.count += 1
                if self.count >= self.limit:
                    self.tripped = True
        else:
            self.count = 0
        return self.tripped


def _drive_lanes(cpu_tasks: Sequence[Tuple[Cell, int]], gpu_tasks: Sequence[Tuple[Cell, int]], cpu_slots: int,
                 gpu_slots: int, init_args: Tuple, task_fn: Callable, finish: Callable[[Dict[str, Any]], None],
                 cancelled: Callable[[], bool], summary: Dict[str, Any],
                 gpu_kwargs: Optional[Dict[str, Any]] = None) -> None:
    """Run the CPU tasks on a pool of ``cpu_slots`` workers and the GPU tasks on a second pool of ``gpu_slots``, the two
    ``_drive_pool`` lifecycles (retries, probation, cancellation, ``BrokenProcessPool``) side by side. One lane runs in
    the calling thread. With both, each lane runs in its own thread: ``finish`` and the cancel check are serialised by
    one lock, ``summary["pool_rebuilds"]`` is the sum of the lanes', and a failure in one lane stops the other (its
    in-flight tasks are cancelled, its rows so far are kept), every lane error is logged and the first is re-raised
    (the others as notes on it). The GPU lane has a circuit breaker (``GpuBreaker``): after ``GPU_BREAKER_LIMIT``
    consecutive CUDA errors it is aborted LOUDLY (``summary["gpu_lane_aborted"]``, an error log line) while the CPU lane
    goes on; its unfinished tasks are retried by the next run. ``gpu_kwargs`` go to the GPU lane's ``_drive_pool`` (the
    hard ``deadline``, ``window_factor``, ``max_tasks_per_child``)."""
    lanes = [(name, t, n) for name, t, n in (("cpu", cpu_tasks, cpu_slots), ("gpu", gpu_tasks, gpu_slots)) if t]
    lock = threading.RLock()
    stop = threading.Event()
    gpu_abort = threading.Event()
    breaker = GpuBreaker(GPU_BREAKER_LIMIT)
    subs: List[Dict[str, Any]] = [{} for _ in lanes]
    errors: List[BaseException] = []

    def rebuilds() -> int:
        return sum(s["pool_rebuilds"] for s in subs if "pool_rebuilds" in s)

    def lane_finish(name: str) -> Callable[[Dict[str, Any]], None]:
        def f(r: Dict[str, Any]) -> None:
            with lock:
                summary["pool_rebuilds"] = rebuilds()
                finish(r)
                if name == "gpu" and breaker.record(r) and not gpu_abort.is_set():
                    gpu_abort.set()
                    summary["gpu_lane_aborted"] = True
                    logger.error(f"ml_grid: the GPU lane is ABORTED after {GPU_BREAKER_LIMIT} consecutive CUDA errors "
                                 f"(last: {r['error']}); its unfinished tasks are left for the next run, the CPU lane "
                                 f"goes on. Check the driver / the device before restarting.")
        return f

    def lane_stopped(name: str) -> Callable[[], bool]:
        def f() -> bool:
            with lock:
                return stop.is_set() or (name == "gpu" and gpu_abort.is_set()) or cancelled()
        return f

    def lane(i: int) -> None:
        name, tasks, slots = lanes[i]
        try:
            extra = gpu_kwargs if (name == "gpu" and gpu_kwargs) else {}
            _drive_pool(tasks, slots, init_args, task_fn, lane_finish(name), lane_stopped(name), subs[i], **extra)
        except BaseException as e:                                # noqa: BLE001  (re-raised by the caller)
            errors.append(e)
            stop.set()

    def join_all(timeout: Optional[float]) -> None:
        end = None if timeout is None else time.monotonic() + timeout
        for th in threads:
            th.join(None if end is None else max(0.0, end - time.monotonic()))

    if len(lanes) == 1:
        threads: List[threading.Thread] = []
        lane(0)
        if errors:
            raise errors[0]
    else:
        threads = [threading.Thread(target=lane, args=(i,), daemon=True, name=f"ml_grid-lane-{i}")
                   for i in range(len(lanes))]
        for th in threads:
            th.start()
        try:
            while any(th.is_alive() for th in threads):
                for th in threads:
                    th.join(0.2)
        except BaseException:                                     # Ctrl-C: stop the lanes and let them finish before leaving
            stop.set()
            join_all(LANE_JOIN_SECONDS)
            raise
        if errors:
            join_all(LANE_JOIN_SECONDS)
            for e in errors:
                logger.error("ml_grid: a lane failed: %r", e, exc_info=e)
            first = errors[0]
            for e in errors[1:]:
                first.add_note(f"another lane failed too: {e!r}")
            raise first
    summary["pool_rebuilds"] = rebuilds()
    summary["cancelled"] = any(s["cancelled"] for (name, _, _), s in zip(lanes, subs)
                               if "cancelled" in s and not (name == "gpu" and gpu_abort.is_set()))


#: seconds the master waits for the lane threads to wind down after a failure or Ctrl-C
LANE_JOIN_SECONDS = 30.0


def read_results(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Rows of a ``results.jsonl``; an unparseable line (a write cut short by a crash) is skipped with a warning."""
    p = Path(path)
    rows: List[Dict[str, Any]] = []
    if not p.exists():
        return rows
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"{p}: line {i + 1} is not valid JSON (an interrupted write?); ignored")
    return rows


def _append(path: Path, row: Dict[str, Any]) -> None:
    prefix = ""
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                prefix = "\n"                                      # a crash left a partial line: do not glue onto it
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(prefix + json.dumps(_sanitize(row)) + "\n")


class ResumeMismatchError(RuntimeError):
    """Finished rows in ``results.jsonl`` were computed under other settings / data than this run uses."""


def _identity_diff(row: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    """How a finished row's recorded identity differs from the one this run would produce (empty = reusable)."""
    ident = row["identity"] if "identity" in row else None
    if not isinstance(ident, dict):
        return ["no identity recorded (a row written before identity tracking)"]
    out = []
    for k in ("store_signature", "daily_fingerprint", "intraday_fingerprint"):
        if k not in expected:
            continue                                  # only tsai rows carry the intraday fingerprint
        got = ident[k] if k in ident else None
        if got != expected[k]:
            out.append(f"{k}: row {got!r}, now {expected[k]!r}")
    old = ident["settings"] if "settings" in ident and isinstance(ident["settings"], dict) else {}
    new = expected["settings"]
    for k in sorted(set(old) | set(new)):
        a_, b_ = (old[k] if k in old else None), (new[k] if k in new else None)
        if a_ != b_:
            out.append(f"settings.{k}: row {a_!r}, now {b_!r}")
    return out


def _timed_out_tasks(err_path: Path) -> set:
    """The (cell_key, seed) whose LAST record in ``errors.jsonl`` is a timeout."""
    last: Dict[Tuple[str, int], bool] = {}
    for e in read_results(err_path):
        last[(e["cell_key"], int(e["seed"]))] = bool("timeout" in e and e["timeout"])
    return {k for k, v in last.items() if v}


def _check_resume(rows: List[Dict[str, Any]], cells: Sequence[Cell], st: GridSettings, cache_root: str,
                  rerun_mismatched: bool, res_path: Path, dry_run: bool = False) -> set:
    """The (cell_key, seed) pairs whose finished row may be reused. A row of a cell in this run whose identity differs
    raises ``ResumeMismatchError``; with ``rerun_mismatched`` it is moved to ``results.stale.jsonl`` (and so recomputed).
    Rows of cells that are not in this run are left alone. A row whose predictions file is gone is not reused either
    (it is moved to the stale file and recomputed, with or without ``rerun_mismatched``; a ``no_predictions`` row has
    no file by design). ``dry_run`` checks and raises but moves nothing."""
    wanted = {(c.key, s): c for c in cells for s in c.seeds}
    fps: Dict[Tuple, str] = {}
    mism: Dict[Tuple[str, int], List[str]] = {}
    lost: Dict[Tuple[str, int], List[str]] = {}
    pred_dir = res_path.parent / "predictions"
    finished = set()
    for r in rows:
        k = (r["cell_key"], int(r["seed"]))
        if k not in wanted:
            continue
        c = wanted[k]
        syms = tuple(sorted(resolve_symbols(c.symbols)))
        first, last, _ = store_span(c.store, syms)
        if (syms, first, last) not in fps:
            fps[(syms, first, last)] = daily_fingerprint(cache_root, syms, first, last)
        ifp = ""
        if split_family(c.family)[0] == "tsai":
            from app.services.ml_grid_seq import intraday_fingerprint
            if ("intraday", syms, last) not in fps:
                fps[("intraday", syms, last)] = intraday_fingerprint(cache_root, syms, st.eval_end, last)
            ifp = fps[("intraday", syms, last)]
        diff = _identity_diff(r, identity_of(c, st, fps[(syms, first, last)], ifp))
        if diff:
            mism[k] = diff
        elif r["status"] == "ok" and not (pred_dir / f"{k[0]}_{k[1]}.parquet").exists():
            lost[k] = ["predictions file missing"]
        else:
            finished.add(k)
    if not mism and not lost:
        return finished
    if mism and not rerun_mismatched:
        ex = "; ".join(f"cell {k[0]} seed {k[1]} ({wanted[k].label}): {', '.join(d[:3])}"
                       for k, d in list(mism.items())[:5])
        raise ResumeMismatchError(
            f"{len(mism)} finished row(s) in {res_path} were computed under other settings or data than this run "
            f"({ex}). Pass rerun_mismatched=True (CLI --rerun-mismatched) to move them to results.stale.jsonl and "
            f"recompute them, or use another --out.")
    if dry_run:
        return finished
    move = set(mism) | set(lost)
    keep = [r for r in rows if (r["cell_key"], int(r["seed"])) not in move]
    stale = [r for r in rows if (r["cell_key"], int(r["seed"])) in move]
    for r in stale:
        _append(res_path.with_name("results.stale.jsonl"), r)
    tmp = res_path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(_sanitize(r)) + "\n" for r in keep), encoding="utf-8")
    os.replace(tmp, res_path)
    logger.warning(f"ml_grid: {len(stale)} row(s) ({len(mism)} mismatched, {len(lost)} without predictions file) "
                   f"moved to results.stale.jsonl, they will be recomputed")
    return finished


def resolve_cache_root(cache_root: Optional[Union[str, Path]]) -> str:
    """``cache_root`` as a string; ``None`` means the platform cache folder."""
    if cache_root is None:
        from ba2_common.config import CACHE_FOLDER
        return str(CACHE_FOLDER)
    return str(cache_root)


#: measured on the exploration's panels: parquet+zstd bytes per prediction row relative to the raw column bytes
PARQUET_RATIO = 0.6
#: typical size of one results.jsonl row (metrics, regimes, controls, 31 block audit entries)
RESULT_ROW_BYTES = 14_000


def _fmt_bytes(n: float) -> str:
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


def estimate_disk(tasks: Sequence[Tuple[Cell, int]], st: GridSettings) -> Dict[str, Any]:
    """Rough bytes the run will write: the predictions files (rows = symbols x sessions of the evaluation window,
    float32 columns score / ret / vol [+ rel_ret on the selection track, + realvol for a volatility target] + session
    + symbol + block, times ``PARQUET_RATIO`` for zstd) and the results rows."""
    sessions = float(np.busday_count(st.eval_start, st.eval_end)) * 0.96          # ~ NYSE sessions
    pred = 0.0
    rows_per_task = 0.0
    for c, _ in tasks:
        n_float = 3 + (1 if c.track == "selection" else 0) + (1 if c.kind == "volatility" else 0)
        rows = len(resolve_symbols(c.symbols)) * sessions
        pred += rows * (4 * n_float + 8 + 4 + 2) * PARQUET_RATIO
        rows_per_task = max(rows_per_task, rows)
    results = len(tasks) * RESULT_ROW_BYTES
    return {"predictions": int(pred), "results": int(results), "total": int(pred) + int(results),
            "rows_per_task": int(rows_per_task)}


def n_jobs_for(cpu_slots: int, cores: int) -> int:
    """Threads per fit so that ``cpu_slots * n_jobs <= cores``."""
    return max(1, int(cores) // max(1, int(cpu_slots)))


def run_grid(cells: Sequence[Cell], out_dir: Union[str, Path], cpu_slots: int, gpu_slots: int = 0,
             cache_root: Optional[Union[str, Path]] = None, settings: Optional[GridSettings] = None,
             progress: Optional[Callable[..., None]] = None, inline: bool = False,
             rerun_mismatched: bool = False, cancel_check: Optional[Callable[[], bool]] = None,
             task_fn: Optional[Callable] = None, retry_timeouts: bool = False) -> Dict[str, Any]:
    """Run every unfinished (cell, seed) of ``cells``; append a row to ``results.jsonl`` as each finishes.

    ``cpu_slots``: worker processes for the tabular families. ``gpu_slots``: workers of the second pool, for the
    sequence families (``tsai-*``, ``darts-*``); a cell routed to the gpu pool with 0 slots is refused. The two pools are
    separate caps and run concurrently. ``inline=True`` runs in this process (tests,
    debugging) through the same code. ``rerun_mismatched``: see ``ResumeMismatchError`` / the module docstring.
    ``cancel_check``: polled between submissions; when it returns True the run stops cooperatively (rows already
    written stay; a restart resumes). ``task_fn``: the per-task function (default ``_run_task``; a module-level
    callable so a spawned worker can import it; for tests). Only one run may use an ``out_dir`` at a time
    (``GridLockedError``). A (cell, seed) whose last error record is a TIMEOUT (``timeout: true``: over its time budget, or
    killed by the master's hard deadline) is not retried by a restart (it would only time out again, hours later)
    unless ``retry_timeouts``; ``summary["skipped_timeouts"]`` counts them when there are some. Returns ``{tasks, done, skipped_existing, errors, cancelled, pool_rebuilds, out_dir}``."""
    if not _is_int(cpu_slots) or cpu_slots < 1 or not _is_int(gpu_slots) or gpu_slots < 0:
        raise ValueError("cpu_slots must be an integer >= 1 and gpu_slots an integer >= 0")
    st = settings or GridSettings()
    cache_root = resolve_cache_root(cache_root)
    check_groups(cells)                                       # refuse an unusable group before anything starts
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(out)
    try:
        return _run_locked(cells, out, cpu_slots, gpu_slots, cache_root, st, progress, inline, rerun_mismatched,
                           cancel_check or (lambda: False), task_fn or _run_task, retry_timeouts)
    finally:
        release_lock(lock)


# The dashboard hook (``_refresh_outputs``) is called at the start, after EVERY finished row (results and errors alike)
# and at the end; ``ml_grid_report.refresh`` decides in O(1) whether a live rebuild is due (it waits max(60 s, 10 x the
# last build)); only the start and the end are forced.


_REFRESH_FAILURES: Dict[str, int] = {}


def _refresh_outputs(out: Path, summary: Dict[str, Any], final: bool, finished: int) -> None:
    """Rebuild ``dashboard.html`` (``final``: also the gate and ``report.md``; the start of a run and the end always
    build, a live one is throttled and incremental, see ``ml_grid_report.refresh``).
    Reporting must never stop a grid: whatever goes wrong (an open file, a bad row) is logged and the run goes on. The
    first failure of a run is logged in full, the following ones are only counted (one line at the end)."""
    key = str(out)
    if finished == 0 and not final:
        _REFRESH_FAILURES[key] = 0
    try:
        from app.services import ml_grid_report
        ml_grid_report.refresh(out, summary, final, force=final or finished == 0)
    except Exception as e:                                      # noqa: BLE001
        _REFRESH_FAILURES[key] = (_REFRESH_FAILURES[key] if key in _REFRESH_FAILURES else 0) + 1
        if _REFRESH_FAILURES[key] == 1:
            logger.warning(f"ml_grid: could not rebuild the dashboard / report in {out} after {finished} rows: {e}",
                           exc_info=True)
    if final:
        n = _REFRESH_FAILURES.pop(key, 0)
        if n > 1:
            logger.warning(f"ml_grid: {n} dashboard / report rebuilds failed in this run (the first was logged in full)")


def _run_locked(cells, out: Path, cpu_slots: int, gpu_slots: int, cache_root: str, st: GridSettings,
                progress, inline: bool, rerun_mismatched: bool, cancelled, task_fn,
                retry_timeouts: bool = False) -> Dict[str, Any]:
    res_path, err_path = out / "results.jsonl", out / "errors.jsonl"
    say = progress or (lambda m, pct=None: logger.warning(m))
    finished = _check_resume(read_results(res_path), cells, st, cache_root, rerun_mismatched, res_path)
    plan = plan_tasks(cells)
    tasks = [(c, sd) for c, sd in plan if (c.key, sd) not in finished]
    skipped_existing = len(plan) - len(tasks)
    timed_out = set() if retry_timeouts else _timed_out_tasks(err_path)
    skipped_timeouts = [(c, sd) for c, sd in tasks if (c.key, sd) in timed_out]
    tasks = [t for t in tasks if (t[0].key, t[1]) not in timed_out]
    gpu_tasks = [t for t in tasks if t[0].pool == "gpu"]
    cpu_tasks = [t for t in tasks if t[0].pool != "gpu"]
    if gpu_tasks and gpu_slots < 1:
        raise ValueError(f"{len(gpu_tasks)} tasks need the gpu pool but gpu_slots is 0")
    summary = {"tasks": len(tasks), "done": 0, "skipped_existing": skipped_existing, "errors": 0, "cancelled": False,
               "pool_rebuilds": 0, "out_dir": str(out)}
    if skipped_timeouts:
        summary["skipped_timeouts"] = len(skipped_timeouts)
        say(f"ml_grid: {len(skipped_timeouts)} task(s) timed out in an earlier run and are skipped "
            f"(--retry-timeouts runs them again)")
    say(f"ml_grid: {len(cells)} cells, {len(tasks)} (cell, seed) tasks to run, {skipped_existing} already finished")

    try:
        from app.services.ml_grid_report import read_run_summary
        prior_rebuilds = int(read_run_summary(out)["pool_rebuilds_total"]) \
            if "pool_rebuilds_total" in read_run_summary(out) else 0
    except Exception:                                           # noqa: BLE001  (reporting is never fatal)
        prior_rebuilds = 0
    planned = {"planned_count": len(plan), "planned_hash": ""}
    try:                                                        # the list lives in planned_keys.txt, written once
        from app.services.ml_grid_report import write_planned
        planned["planned_hash"] = write_planned(out, [f"{c.key}:{sd}" for c, sd in plan])
    except Exception:                                           # noqa: BLE001  (reporting is never fatal)
        logger.warning(f"ml_grid: could not write planned_keys.txt in {out}", exc_info=True)

    def refresh(final: bool, finished: int) -> None:
        _refresh_outputs(out, {**summary, "planned_tasks": len(plan), "planned_cells": len(cells),
                               **planned, "prior_pool_rebuilds": prior_rebuilds}, final, finished)
    if not tasks:
        refresh(True, 0)
        return summary
    refresh(False, 0)
    import shutil
    est = estimate_disk(tasks, st)
    free = shutil.disk_usage(out).free
    say(f"ml_grid: estimated disk {_fmt_bytes(est['total'])} for {len(tasks)} tasks (predictions "
        f"{_fmt_bytes(est['predictions'])}, results {_fmt_bytes(est['results'])}); {_fmt_bytes(free)} free in {out}")
    if est["total"] > 0.9 * free:
        logger.warning(f"ml_grid: the estimated disk need exceeds 90% of the free space in {out}")
    workers = (cpu_slots if cpu_tasks else 0) + (gpu_slots if gpu_tasks else 0)       # the pools are separate caps
    init_args = (st.to_dict(), cache_root, str(out), n_jobs_for(max(1, workers), st.cores))

    def finish(r: Dict[str, Any]) -> None:
        if "error" in r:
            summary["errors"] += 1
            _append(err_path, r)
            say(f"ml_grid ERROR cell {r['cell_key']} seed {r['seed']} ({r['label']}): {r['error']}")
        else:
            _append(res_path, r)
            summary["done"] += 1
            p = r["primary"]
            c = r["controls"] if isinstance(r["controls"], dict) else {}
            pv = c["p_value"] if "p_value" in c else None
            say(f"ml_grid [{summary['done'] + summary['errors']}/{len(tasks)}] cell {r['cell_key']} seed {r['seed']} "
                f"{r['label']}: {p['name']}={'n/a' if p['value'] is None else format(p['value'], '+.4f')}"
                f"{'' if pv is None else f' p={pv:.3f}'} rows={r['n_rows']} "
                f"skipped={len(r['skipped_blocks'])} ({r['timings']['total']:.0f}s)",
                min(99.0, 100.0 * (summary["done"] + summary["errors"]) / len(tasks)))
        n = summary["done"] + summary["errors"]
        if n < len(tasks):                                       # the final rebuild covers the last row
            refresh(False, n)

    try:
        if inline:
            _init_worker(*init_args)
            for c, sd in tasks:
                if cancelled():
                    summary["cancelled"] = True
                    break
                finish(task_fn(c.config(with_seeds=True), sd, c.key))
        else:
            _drive_lanes(cpu_tasks, gpu_tasks, cpu_slots, gpu_slots, init_args, task_fn, finish, cancelled, summary,
                         {"deadline": HARD_DEADLINE_BUDGETS * st.gpu_budget_minutes * 60.0, "window_factor": 1,
                          "max_tasks_per_child": GPU_MAX_TASKS_PER_CHILD})
    finally:                                                    # also when the run dies: the files say how far it got
        if "gpu_lane_aborted" in summary and summary["gpu_lane_aborted"]:
            say("ml_grid: GPU LANE ABORTED by the circuit breaker (see the error log); rerun after checking the device")
        refresh(True, summary["done"] + summary["errors"])
    return summary


# ================================================================== aggregation
def _factor_levels(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"target": cfg["target"]["kind"], "horizon": cfg["target"]["horizon"], "family": cfg["family"],
            "train_months": cfg["train_months"], "groups": "+".join(cfg["groups"])}


def _field(row: Dict[str, Any], key: str) -> Any:
    """A row's value, None when the row (an older version's) lacks the field."""
    return row[key] if key in row else None


def factor_effects(results: Union[str, Path, Sequence[Dict[str, Any]]], track: str, min_coverage: float = 0.8,
                   max_low_iter_share: Optional[float] = None) -> Dict[str, pd.DataFrame]:
    """Aggregate finished rows of one ``track`` into ``{"factors", "cells", "excluded"}`` DataFrames.

    Rows are deduplicated on (cell_key, seed), the LAST one counting (a rerun replaces). Only rows with ``status ==
    "ok"`` and the track's primary metric (``PRIMARY[track]``; volatility models have a different primary and are left
    out) enter. Per cell: the mean and std (ddof 1; NaN for a single seed; 0 for a deterministic family, see below) of
    the primary over its seeds, and

    * ``coverage`` = the LOWEST over the cell's seeds of ``n_blocks_fitted / n_blocks`` (blocks skipped for a thin
      target class reduce it); a seed with status ``no_predictions`` counts as coverage 0, so a cell where some seed
      produced nothing is excluded by the default ``min_coverage`` (0.8), and one where NO seed did is listed in
      ``excluded``; a cell with no coverage information is excluded too;
    * ``low_iter_share`` = mean over seeds of the row's share of blocks with best iteration <= ``LOW_ITER`` (a
      near-constant model; NaN for non-boosting families, which are never excluded by it); with ``max_low_iter_share``
      a cell above it is excluded;
    * ``p_value`` = the MEDIAN over seeds of the gating control's p, ``p_values`` = the per-seed values (by seed);
    * ``deterministic`` (naive / linear run once; their std of 0 is built in, NOT robustness).

    ``cells`` ranks the kept cells by ``score = mean - std`` (descending, NaN last); ``excluded`` lists the dropped ones
    with their ``reason``. ``factors``: indexed (factor, level) for target / horizon / family / train_months / groups,
    from the kept cells: the mean, std and median of the cell-level means, ``n_cells``, ``share_positive``,
    ``n_deterministic`` and ``low_iter_share``."""
    if track not in TRACKS:
        raise ValueError(f"track must be one of {TRACKS}")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError("min_coverage must be in [0, 1]")
    if max_low_iter_share is not None and not 0.0 <= max_low_iter_share <= 1.0:
        raise ValueError("max_low_iter_share must be None or in [0, 1]")
    rows = read_results(results) if isinstance(results, (str, Path)) else list(results)
    last: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in rows:
        last[(r["cell_key"], int(r["seed"]))] = r
    want = PRIMARY[track]
    by_cell: Dict[str, List[Dict[str, Any]]] = {}
    empty: Dict[str, List[Dict[str, Any]]] = {}                 # seeds that produced no predictions at all
    for r in last.values():
        if _field(r, "track") != track:
            continue
        if _field(r, "status") == "ok" and r["primary"]["name"] == want and r["primary"]["value"] is not None:
            by_cell.setdefault(r["cell_key"], []).append(r)
        elif _field(r, "status") == "no_predictions" and r["config"]["target"]["kind"] != "volatility":
            empty.setdefault(r["cell_key"], []).append(r)
    kept, dropped = [], []
    for key in empty:                                           # a cell with no usable seed at all is listed, not lost
        if key not in by_cell:
            r0 = empty[key][0]
            dropped.append({"cell_key": key, **_factor_levels(r0["config"]), "coverage": 0.0,
                            "low_iter_share": float("nan"), "reason": "no seed produced predictions (coverage 0)"})
    for key, rs in by_cell.items():
        rs.sort(key=lambda r: int(r["seed"]))
        n_empty = len(empty[key]) if key in empty else 0
        v = np.array([r["primary"]["value"] for r in rs], dtype=float)
        det = rs[0]["config"]["family"] in DETERMINISTIC_FAMILIES
        cov = [r["n_blocks_fitted"] / r["n_blocks"] for r in rs
               if _field(r, "n_blocks") and _field(r, "n_blocks_fitted") is not None]
        cov_ok = len(cov) == len(rs)
        cov += [0.0] * n_empty                                  # a seed with no predictions is coverage 0
        ps = [r["controls"]["p_value"] for r in rs if isinstance(_field(r, "controls"), dict)
              and "p_value" in r["controls"] and r["controls"]["p_value"] is not None]
        li = [r["low_iter_share"] for r in rs if _field(r, "low_iter_share") is not None]
        rec = {"cell_key": key, **_factor_levels(rs[0]["config"]), "n_seeds": len(v), "mean": float(v.mean()),
               "std": 0.0 if det else (float(v.std(ddof=1)) if len(v) > 1 else float("nan")),
               "p_value": float(np.median(ps)) if ps else float("nan"), "p_values": ps,
               "coverage": float(min(cov)) if cov_ok else float("nan"),
               "low_iter_share": float(np.mean(li)) if li else float("nan"), "deterministic": det}
        reason = None
        if not np.isfinite(rec["coverage"]):
            reason = "no coverage information (n_blocks / n_blocks_fitted missing)"
        elif rec["coverage"] < min_coverage:
            reason = f"coverage {rec['coverage']:.2f} < min_coverage {min_coverage:g}"
        elif max_low_iter_share is not None and np.isfinite(rec["low_iter_share"]) \
                and rec["low_iter_share"] > max_low_iter_share:
            reason = f"low_iter_share {rec['low_iter_share']:.2f} > max_low_iter_share {max_low_iter_share:g}"
        (dropped if reason else kept).append({**rec, "reason": reason} if reason else rec)
    cols = ["cell_key", "target", "horizon", "family", "train_months", "groups", "n_seeds", "mean", "std", "p_value",
            "p_values", "coverage", "low_iter_share", "deterministic"]
    cdf = pd.DataFrame(kept, columns=cols)
    cdf["deterministic"] = cdf["deterministic"].astype(bool)
    cdf["score"] = cdf["mean"] - cdf["std"]
    cdf = cdf.sort_values("score", ascending=False, na_position="last", kind="mergesort").reset_index(drop=True)
    xdf = pd.DataFrame(dropped, columns=cols + ["reason"])[["cell_key", "reason", "coverage", "low_iter_share", "target",
                                                           "horizon", "family", "train_months", "groups"]]
    parts = []
    for factor in ("target", "horizon", "family", "train_months", "groups"):
        lv = cdf[factor].astype(str)
        g = cdf.groupby(lv, sort=True)["mean"]
        f = pd.DataFrame({"n_cells": g.size(), "mean": g.mean(), "std": g.std(ddof=1), "median": g.median(),
                          "share_positive": g.apply(lambda s: float((s > 0).mean())),
                          "n_deterministic": cdf.groupby(lv, sort=True)["deterministic"].sum().astype(int),
                          "low_iter_share": cdf.groupby(lv, sort=True)["low_iter_share"].mean()})
        f.index = pd.MultiIndex.from_product([[factor], f.index], names=["factor", "level"])
        parts.append(f)
    fdf = pd.concat(parts) if parts and len(cdf) else pd.DataFrame(
        columns=["n_cells", "mean", "std", "median", "share_positive", "n_deterministic", "low_iter_share"],
        index=pd.MultiIndex.from_arrays([[], []], names=["factor", "level"]))
    return {"factors": fdf, "cells": cdf, "excluded": xdf}


# ================================================================== job handler
PAYLOAD_KEYS = ("out_dir", "cpu_slots", "gpu_slots", "config_path", "cells", "limit_cells", "cache_root", "settings",
                "cores", "rerun_mismatched", "retry_timeouts")


def prepare_grid(payload: Dict[str, Any]) -> Tuple[List[Cell], GridSettings, str]:
    """Validate an ``ml_grid`` payload and return ``(cells, settings, cache_root)``. Every key of ``PAYLOAD_KEYS`` must be
    present (no defaults; ``None`` is a value for ``config_path`` / ``cells`` (exactly one is set), ``limit_cells``,
    ``cache_root`` and ``settings``); ``cores`` is an explicit integer."""
    for k in PAYLOAD_KEYS:
        if k not in payload:
            raise ValueError(f"ml_grid payload is missing {k!r}")
    if not _is_int(payload["cpu_slots"]) or payload["cpu_slots"] < 1:
        raise ValueError("cpu_slots must be an integer >= 1")
    if not _is_int(payload["gpu_slots"]) or payload["gpu_slots"] < 0:
        raise ValueError("gpu_slots must be an integer >= 0")
    if not _is_int(payload["cores"]) or payload["cores"] < 1:
        raise ValueError("cores must be an explicit integer >= 1")
    if not isinstance(payload["rerun_mismatched"], bool):
        raise ValueError("rerun_mismatched must be true or false")
    if not isinstance(payload["retry_timeouts"], bool):
        raise ValueError("retry_timeouts must be true or false")
    limit = payload["limit_cells"]
    if limit is not None and (not _is_int(limit) or limit < 1):
        raise ValueError("limit_cells must be null or an integer >= 1")
    if bool(payload["config_path"]) == (payload["cells"] is not None):
        raise ValueError("ml_grid payload needs exactly one of 'config_path' and 'cells'")
    cells = load_cells(payload["config_path"] if payload["config_path"] else payload["cells"])
    if limit is not None:
        cells = cells[:limit]
    if payload["settings"] is not None and not isinstance(payload["settings"], dict):
        raise ValueError("settings must be null or an object")
    st = GridSettings.from_dict({**(payload["settings"] or {}), "cores": payload["cores"]})
    return cells, st, resolve_cache_root(payload["cache_root"])


def check_lock(out: Path) -> None:
    """Raise ``GridLockedError`` when a live run holds ``out`` (without taking the lock)."""
    path = out / LOCK_NAME
    if not path.exists():
        return
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        alive = held["host"] != socket.gethostname() or pid_alive(int(held["pid"]))
    except (OSError, json.JSONDecodeError, KeyError):
        return
    if alive:
        raise GridLockedError(f"another ml_grid run is already running in {out} (pid {held['pid']} on {held['host']}, "
                              f"started {held['started']})")


def precheck_grid(payload: Dict[str, Any]) -> int:
    """Everything that can be refused before a task is queued: the payload, the cells (holdout lock, schema), unusable
    feature groups, a live run in ``out_dir`` and finished rows whose identity differs from this run's
    (``ResumeMismatchError`` unless ``rerun_mismatched``). Changes nothing. Returns the number of cells."""
    cells, st, cache_root = prepare_grid(payload)
    check_groups(cells)
    out = Path(payload["out_dir"])
    check_lock(out)
    _check_resume(read_results(out / "results.jsonl"), cells, st, cache_root, payload["rerun_mismatched"],
                  out / "results.jsonl", dry_run=True)
    return len(cells)


def handle_ml_grid(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task ``ml_grid``. The payload carries every key of ``PAYLOAD_KEYS`` (``prepare_grid``): ``out_dir``, ``cpu_slots``,
    ``gpu_slots``, ``config_path`` / ``cells`` (exactly one), ``limit_cells``, ``cache_root``, ``settings`` (``GridSettings``
    overrides), ``cores`` and ``rerun_mismatched``. A queue cancellation stops the run cooperatively."""
    cells, st, cache_root = prepare_grid(payload)

    last = {"pct": 0.0}

    def progress(msg: str, pct: Optional[float] = None) -> None:
        if pct is not None:
            last["pct"] = pct
        try:
            from app.services.task_queue import get_task_queue
            get_task_queue().update_progress(task_id, last["pct"], msg)
        except Exception as e:                                   # the CLI path has no queue row
            logger.debug(f"progress not recorded: {e}")
        logger.warning(msg)

    res = run_grid(cells, payload["out_dir"], payload["cpu_slots"], payload["gpu_slots"], cache_root, st, progress,
                   rerun_mismatched=payload["rerun_mismatched"], cancel_check=make_cancel_check(task_id),
                   retry_timeouts=payload["retry_timeouts"])
    progress(f"ml_grid {'CANCELLED' if res['cancelled'] else 'done'}: {res['done']} rows, {res['errors']} errors, "
             f"{res['skipped_existing']} already finished", None if res["cancelled"] else 100.0)
    return res
