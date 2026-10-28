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
cores. Sequence families (tsai / darts) will register in ``FAMILY_POOL`` with pool ``"gpu"`` and run in a second pool of
``gpu_slots`` workers; none exists yet, so that pool is never started.

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
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
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
EVAL_START, EVAL_END, DATA_START = date(2019, 1, 1), date(2026, 10, 12), date(2016, 1, 4)


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

    def __post_init__(self) -> None:
        if not self.eval_start < self.eval_end:
            raise ValueError("eval_start must be before eval_end")
        if not self.data_start < self.eval_start:
            raise ValueError("data_start must be before eval_start")
        if self.cost_bps < 0 or not 0 < self.timing_q < 1 or not 0 < self.val_share < 1:
            raise ValueError("cost_bps >= 0, 0 < timing_q < 1 and 0 < val_share < 1 required")
        for name in ("min_count", "min_obs", "regime_min_rows", "cores"):
            if not _is_int(getattr(self, name)) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be an integer >= 1")
        if not len(self.control_seeds):
            raise ValueError("control_seeds is empty")

    def to_dict(self) -> Dict[str, Any]:
        return {"eval_start": str(self.eval_start), "eval_end": str(self.eval_end), "data_start": str(self.data_start),
                "cost_bps": self.cost_bps, "timing_q": self.timing_q, "min_count": self.min_count,
                "val_share": self.val_share, "min_obs": self.min_obs, "regime_min_rows": self.regime_min_rows,
                "control_seeds": list(self.control_seeds), "cores": self.cores}

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

#: which worker pool runs a family. Sequence families (tsai, darts) will register here as "gpu".
FAMILY_POOL: Dict[str, str] = {f: "cpu" for f in M.SUPPORTED_TASKS}

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
    family = raw["family"]
    if family not in FAMILY_POOL:
        raise ValueError(f"family must be one of {sorted(FAMILY_POOL)}, got {family!r}")
    task = KINDS[kind].task
    if task not in M.SUPPORTED_TASKS[family]:
        raise ValueError(f"family {family!r} cannot do the {task!r} task of target {kind!r} "
                         f"(supports {M.SUPPORTED_TASKS[family]})")
    params = raw["params"] if "params" in raw else {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    M.make_adapter(family, 1, params)                      # unknown keys / out-of-range values raise
    months = raw["train_months"]
    if months != "expanding" and (isinstance(months, bool) or months not in W.TRAIN_MONTHS):
        raise ValueError(f"train_months must be one of {W.TRAIN_MONTHS} or 'expanding', got {months!r}")
    blocks = raw["blocks"]
    if blocks not in W.FREQS:
        raise ValueError(f"blocks must be one of {W.FREQS}, got {blocks!r}")
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


def store_signature(store: Union[str, Path], symbols: Sequence[str]) -> str:
    """Identity of the store data a cell reads, independent of where the store lives: a hash over the stores that hold
    ``symbols`` of (name, ``ensure.inputs`` -- code version, coverage columns, source fingerprints --, ``min_share``,
    symbols, first / last day, cache end). Raises when a symbol is in no store."""
    avail = discover_stores(store)
    owner: Dict[str, str] = {}
    for name, _, syms in avail:
        for s in syms:
            owner.setdefault(s, name)
    absent = [s for s in symbols if s not in owner]
    if absent:
        raise ValueError(f"symbols {absent} are in no store under {store} "
                         f"(stores: {', '.join(f'{n}={len(s)} symbols' for n, _, s in avail)})")
    parts = {}
    for name, d, _ in avail:
        if any(owner[s] == name for s in symbols):
            m = _meta_of(d)
            parts[name] = {"inputs": m["ensure"]["inputs"], "min_share": m["ensure"]["min_share"],
                           "symbols": m["symbols"], "first_day": m["first_day"], "last_day": m["last_day"],
                           "cache_end": m["cache_end"]}
    return hashlib.sha1(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def daily_fingerprint(cache_root: Union[str, Path], symbols: Sequence[str]) -> str:
    """Hash of the (size, mtime) of the daily cache file of every symbol: what the daily bars were read from."""
    parts = []
    for sym in sorted(symbols):
        st = F._cache_file(Path(cache_root), sym).stat()
        parts.append([sym, st.st_size, st.st_mtime_ns])
    return hashlib.sha1(json.dumps(parts).encode("utf-8")).hexdigest()[:16]


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
    owner: Dict[str, str] = {}
    for name, _, syms in avail:
        for s in syms:
            owner.setdefault(s, name)
    absent = [s for s in symbols if s not in owner]
    if absent:
        raise ValueError(f"symbols {absent} are in no store under {store} "
                         f"(stores: {', '.join(f'{n}={len(s)} symbols' for n, _, s in avail)})")
    used = [(n, d) for n, d, _ in avail if any(owner[s] == n for s in symbols)]
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
    bars, _ = F.load_daily(symbols, first, last, Path(cache_root), cache_end=cache_end)
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
                 arrays, tuple(n for n, _ in used), time.perf_counter() - t0,
                 daily_fingerprint(cache_root, symbols))


_PANELS: Dict[Tuple, Panel] = {}


def get_panel(store: str, symbols: Tuple[str, ...], track: str, cache_root: str) -> Panel:
    """Per-process cache of ``load_panel`` keyed by store (path + file fingerprints), symbol set, track, cache root."""
    key = (str(Path(store).resolve()), _store_fingerprint(store), tuple(symbols), track, str(cache_root))
    if key not in _PANELS:
        _PANELS[key] = load_panel(store, tuple(symbols), track, cache_root)
    return _PANELS[key]


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


def fit_block(cell: Cell, seed: int, panel: Panel, block: W.Block, window: W.Window, st: GridSettings,
              n_jobs: int) -> BlockResult:
    """Fit on the purged window before ``block`` and score its rows. Raises ``SkipBlock`` (reason in the message)."""
    H = cell.horizon
    ks = cell.spec
    tg = panel.targets(H)
    fold = T.Fold(block.name, window.start, block.start, block.end)
    score_col = {"volatility": f"realvol_{H}"}.get(cell.kind, f"rel_ret_{H}" if cell.track == "selection" else f"ret_{H}")
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
    iso = lambda i: str(pd.Timestamp(i).date())                       # noqa: E731
    audit = {"start": str(block.start), "end": str(block.end), "window_start": str(window.start),
             "fit_first": iso(day[fit_idx].min()), "fit_last": iso(day[fit_idx].max()),
             "val_first": iso(day[val_idx].min()), "val_last": iso(day[val_idx].max()),
             "fit_target_end_max": iso(tend[fit_idx].max()), "train_target_end_max": iso(tend[tr].max())}
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


def run_controls(d: pd.DataFrame, cell: Cell, st: GridSettings) -> Dict[str, Any]:
    if cell.kind == "volatility":
        return {"skipped": "a volatility model is a sizing model: the trading shuffle controls do not apply"}
    if cell.track == "selection":
        metric = E.spread_metric(cost_bps=st.cost_bps, assume_flat=True)
        outcomes, primary = d[["symbol", "session", "rel_ret", "vol"]], "shuffle_symbols"
    else:
        metric = E.timing_metric(q=st.timing_q, cost_bps=st.cost_bps, min_obs=st.min_obs, assume_flat=True)
        outcomes, primary = d[["symbol", "session", "ret", "vol"]], "shuffle_time"       # + the within-block null below
    timing = cell.track == "timing"
    rep = controls_report(d[["symbol", "session", "score"] + (["block"] if timing else [])], outcomes, metric,
                          seeds=st.control_seeds, within_block=timing)
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


def identity_of(cell: Cell, st: GridSettings, daily_fp: str) -> Dict[str, Any]:
    """What a finished row was computed from, for the resume check: the settings (not ``cores``), the store signature
    and the daily cache fingerprint."""
    settings = st.to_dict()
    del settings["cores"]
    return _sanitize({"settings": settings, "store_signature": cell.signature, "daily_fingerprint": daily_fp})


def run_cell_seed(cell: Cell, seed: int, panel: Panel, st: GridSettings, n_jobs: int,
                  pred_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One (cell, seed): walk-forward fit, stitch, evaluate. Returns the ``results.jsonl`` row (and writes the
    predictions parquet first when ``pred_dir`` is given)."""
    t_start = time.perf_counter()
    blocks = W.blocks(st.eval_start, st.eval_end, cell.blocks)
    skipped: List[Dict[str, str]] = []
    clipped: List[Dict[str, str]] = []
    info: List[Dict[str, Any]] = []
    rows_l, scores_l, block_l = [], [], []
    t_fit = 0.0
    for b in blocks:
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
                           "config": cell.config(), "identity": identity_of(cell, st, panel.daily_fingerprint),
                           "low_iter_share": float(np.mean([i <= LOW_ITER for i in iters])) if iters else None,
                           "n_blocks": len(blocks), "n_blocks_fitted": len(info),
                           "skipped_blocks": skipped, "clipped_blocks": clipped, "blocks": info,
                           "n_features": len(panel.group_columns(cell.groups)), "symbols": list(panel.symbols)}
    if not rows_l:
        row.update({"status": "no_predictions", "n_rows": 0, "primary": {"name": None, "value": None},
                    "metrics": {}, "regimes": {}, "controls": {}})
        row["timings"] = {"fit": t_fit, "panel_load": panel.load_seconds, "total": time.perf_counter() - t_start}
        return _sanitize(row)
    pred = _predictions_frame(cell, panel, np.concatenate(rows_l), np.concatenate(scores_l), np.concatenate(block_l))
    if pred_dir is not None:
        pred_dir.mkdir(parents=True, exist_ok=True)
        final = pred_dir / f"{cell.key}_{seed}.parquet"
        tmp = final.with_suffix(".tmp")
        pred.to_parquet(tmp, index=False)
        os.replace(tmp, final)
    ev = evaluate(pred, cell, st)
    row.update({"status": "ok", "n_rows": int(len(pred)), "primary": ev["primary"], "metrics": ev["metrics"],
                "regimes": ev["regimes"], "controls": ev["controls"]})
    row["timings"] = {"fit": t_fit, "metrics": ev["seconds_metrics"], "controls": ev["seconds_controls"],
                      "panel_load": panel.load_seconds, "total": time.perf_counter() - t_start}
    return _sanitize(row)


# ================================================================== the runner
_WORKER: Dict[str, Any] = {}


def _panel_for(cell: Cell, cache_root: str) -> Panel:
    return get_panel(cell.store, resolve_symbols(cell.symbols), cell.track, cache_root)


def _init_worker(settings: Dict[str, Any], cache_root: str, out_dir: str, n_jobs: int,
                 warm: Sequence[Dict[str, Any]]) -> None:
    """Pool initializer: remember the run constants and load each panel the pending tasks need, ONCE."""
    import warnings
    warnings.filterwarnings("ignore")
    _WORKER.update(settings=GridSettings.from_dict(settings), cache_root=cache_root, out_dir=out_dir, n_jobs=n_jobs)
    for cfg in warm:
        t0 = time.perf_counter()
        _panel_for(validate_cell({**cfg, "seeds": [0]}), cache_root)
        logger.info(f"worker {os.getpid()}: panel loaded in {time.perf_counter() - t0:.1f}s")


def _run_task(cfg: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Pool task: one (cell, seed). A raised exception becomes an ``error`` record (the grid goes on)."""
    cell = validate_cell({**cfg, "seeds": [seed]})
    try:
        panel = _panel_for(cell, _WORKER["cache_root"])
        return run_cell_seed(cell, seed, panel, _WORKER["settings"], _WORKER["n_jobs"],
                             Path(_WORKER["out_dir"]) / "predictions")
    except Exception as e:                                       # noqa: BLE001  (one bad cell must not stop the grid)
        return {"cell_key": cell.key, "seed": int(seed), "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(), "label": cell.label}


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
    ident = row.get("identity")
    if not isinstance(ident, dict):
        return ["no identity recorded (a row written before identity tracking)"]
    out = []
    for k in ("store_signature", "daily_fingerprint"):
        if ident.get(k) != expected[k]:
            out.append(f"{k}: row {ident.get(k)!r}, now {expected[k]!r}")
    old, new = ident.get("settings") or {}, expected["settings"]
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out.append(f"settings.{k}: row {old.get(k)!r}, now {new.get(k)!r}")
    return out


def _check_resume(rows: List[Dict[str, Any]], cells: Sequence[Cell], st: GridSettings, cache_root: str,
                  rerun_mismatched: bool, res_path: Path) -> set:
    """The (cell_key, seed) pairs whose finished row may be reused. A row of a cell in this run whose identity differs
    raises ``ResumeMismatchError``; with ``rerun_mismatched`` it is moved to ``results.stale.jsonl`` (and so recomputed).
    Rows of cells that are not in this run are left alone."""
    wanted = {(c.key, s): c for c in cells for s in c.seeds}
    fps: Dict[Tuple[str, ...], str] = {}
    mism: Dict[Tuple[str, int], List[str]] = {}
    finished = set()
    for r in rows:
        k = (r["cell_key"], int(r["seed"]))
        if k not in wanted:
            continue
        c = wanted[k]
        syms = tuple(sorted(resolve_symbols(c.symbols)))
        if syms not in fps:
            fps[syms] = daily_fingerprint(cache_root, syms)
        diff = _identity_diff(r, identity_of(c, st, fps[syms]))
        if diff:
            mism[k] = diff
        else:
            finished.add(k)
    if not mism:
        return finished
    if not rerun_mismatched:
        ex = "; ".join(f"cell {k[0]} seed {k[1]} ({wanted[k].label}): {', '.join(d[:3])}"
                       for k, d in list(mism.items())[:5])
        raise ResumeMismatchError(
            f"{len(mism)} finished row(s) in {res_path} were computed under other settings or data than this run "
            f"({ex}). Pass rerun_mismatched=True (CLI --rerun-mismatched) to move them to results.stale.jsonl and "
            f"recompute them, or use another --out.")
    keep = [r for r in rows if (r["cell_key"], int(r["seed"])) not in mism]
    stale = [r for r in rows if (r["cell_key"], int(r["seed"])) in mism]
    for r in stale:
        _append(res_path.with_name("results.stale.jsonl"), r)
    tmp = res_path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(_sanitize(r)) + "\n" for r in keep), encoding="utf-8")
    os.replace(tmp, res_path)
    logger.warning(f"ml_grid: {len(stale)} mismatched row(s) moved to results.stale.jsonl, they will be recomputed")
    return finished


def n_jobs_for(cpu_slots: int, cores: int) -> int:
    """Threads per fit so that ``cpu_slots * n_jobs <= cores``."""
    return max(1, int(cores) // max(1, int(cpu_slots)))


def run_grid(cells: Sequence[Cell], out_dir: Union[str, Path], cpu_slots: int, gpu_slots: int = 0,
             cache_root: Optional[Union[str, Path]] = None, settings: Optional[GridSettings] = None,
             progress: Optional[Callable[..., None]] = None, inline: bool = False,
             rerun_mismatched: bool = False) -> Dict[str, Any]:
    """Run every unfinished (cell, seed) of ``cells``; append a row to ``results.jsonl`` as each finishes.

    ``cpu_slots``: worker processes for the tabular families. ``gpu_slots``: workers for sequence families (none
    exist yet; a cell routed to the gpu pool with 0 slots is refused). ``inline=True`` runs in this process (tests,
    debugging) through the same code. ``rerun_mismatched``: see ``ResumeMismatchError`` / the module docstring. Returns ``{tasks, done, skipped_existing, errors, out_dir}``."""
    if not _is_int(cpu_slots) or cpu_slots < 1 or not _is_int(gpu_slots) or gpu_slots < 0:
        raise ValueError("cpu_slots must be an integer >= 1 and gpu_slots an integer >= 0")
    st = settings or GridSettings()
    if cache_root is None:
        from ba2_common.config import CACHE_FOLDER
        cache_root = CACHE_FOLDER
    cache_root = str(cache_root)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res_path, err_path = out / "results.jsonl", out / "errors.jsonl"
    say = progress or (lambda m, pct=None: logger.warning(m))
    finished = _check_resume(read_results(res_path), cells, st, cache_root, rerun_mismatched, res_path)
    tasks = [(c, s) for c in cells for s in c.seeds if (c.key, s) not in finished]
    skipped_existing = sum(len(c.seeds) for c in cells) - len(tasks)
    pools = {p: [t for t in tasks if t[0].pool == p] for p in ("cpu", "gpu")}
    if pools["gpu"] and gpu_slots < 1:
        raise ValueError(f"{len(pools['gpu'])} tasks need the gpu pool but gpu_slots is 0")
    if pools["gpu"]:
        raise NotImplementedError("sequence families are not implemented yet (the gpu pool hook is a placeholder)")
    summary = {"tasks": len(tasks), "done": 0, "skipped_existing": skipped_existing, "errors": 0, "out_dir": str(out)}
    say(f"ml_grid: {len(cells)} cells, {len(tasks)} (cell, seed) tasks to run, {skipped_existing} already finished")
    if not tasks:
        return summary
    n_jobs = n_jobs_for(cpu_slots, st.cores)
    warm, seen = [], set()
    for c, _ in tasks:
        if (c.store, c.symbols, c.track) not in seen:
            seen.add((c.store, c.symbols, c.track))
            warm.append(c.config())
    init_args = (st.to_dict(), cache_root, str(out), n_jobs, warm)

    def finish(r: Dict[str, Any]) -> None:
        if "error" in r:
            summary["errors"] += 1
            _append(err_path, r)
            say(f"ml_grid ERROR cell {r['cell_key']} seed {r['seed']} ({r['label']}): {r['error']}")
            return
        _append(res_path, r)
        summary["done"] += 1
        p = r["primary"]
        c = r["controls"] if isinstance(r["controls"], dict) else {}
        pv = c.get("p_value")
        say(f"ml_grid [{summary['done'] + summary['errors']}/{len(tasks)}] cell {r['cell_key']} seed {r['seed']} "
            f"{r['label']}: {p['name']}={'n/a' if p['value'] is None else format(p['value'], '+.4f')}"
            f"{'' if pv is None else f' p={pv:.3f}'} rows={r['n_rows']} "
            f"skipped={len(r['skipped_blocks'])} ({r['timings']['total']:.0f}s)",
            min(99.0, 100.0 * (summary["done"] + summary["errors"]) / len(tasks)))

    if inline:
        _init_worker(*init_args)
        for c, s in tasks:
            finish(_run_task(c.config(), s))
        return summary
    with ProcessPoolExecutor(max_workers=min(cpu_slots, len(pools["cpu"])), initializer=_init_worker,
                             initargs=init_args) as pool:
        futs = [pool.submit(_run_task, c.config(), s) for c, s in pools["cpu"]]
        for fu in as_completed(futs):
            finish(fu.result())
    return summary


# ================================================================== aggregation
def _factor_levels(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"target": cfg["target"]["kind"], "horizon": cfg["target"]["horizon"], "family": cfg["family"],
            "train_months": cfg["train_months"], "groups": "+".join(cfg["groups"])}


def factor_effects(results: Union[str, Path, Sequence[Dict[str, Any]]], track: str) -> Dict[str, pd.DataFrame]:
    """Aggregate finished rows of one ``track`` into ``{"factors": DataFrame, "cells": DataFrame}``.

    Only rows with ``status == "ok"`` and the track's primary metric (``PRIMARY[track]``; volatility models have a
    different primary and are left out) enter. Per cell: the mean and std (ddof 1; NaN for a single seed) of the
    primary over its seeds. ``factors``: indexed (factor, level) for the factors target / horizon / family /
    train_months / groups, with the mean, std (ddof 1) and median across the CELLS of the cell-level seed-mean, the
    number of cells and the share with a positive mean. ``cells``: one row per cell with its levels, ``mean``, ``std``,
    ``n_seeds``, ``p_value`` (mean of the gating control's p over seeds), ``low_iter_share`` (mean over seeds of the
    row's share of blocks with best iteration <= ``LOW_ITER``: near-constant models; NaN for non-boosting families),
    ``deterministic`` (naive / linear: their seed std of 0 is built in, NOT robustness) and ``score = mean - std``,
    ranked by score (descending; NaN last). ``factors`` also carry ``n_deterministic`` (cells of a deterministic
    family at that level) and ``low_iter_share`` (mean over the level's cells)."""
    if track not in TRACKS:
        raise ValueError(f"track must be one of {TRACKS}")
    rows = read_results(results) if isinstance(results, (str, Path)) else list(results)
    want = PRIMARY[track]
    by_cell: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("track") == track and r.get("status") == "ok" and r["primary"]["name"] == want \
                and r["primary"]["value"] is not None:
            by_cell.setdefault(r["cell_key"], []).append(r)
    cells = []
    for key, rs in by_cell.items():
        v = np.array([r["primary"]["value"] for r in rs], dtype=float)
        ps = [r["controls"].get("p_value") for r in rs if isinstance(r.get("controls"), dict)
              and r["controls"].get("p_value") is not None]
        li = [r["low_iter_share"] for r in rs if r.get("low_iter_share") is not None]
        cells.append({"cell_key": key, **_factor_levels(rs[0]["config"]), "n_seeds": len(v), "mean": float(v.mean()),
                      "std": float(v.std(ddof=1)) if len(v) > 1 else float("nan"),
                      "p_value": float(np.mean(ps)) if ps else float("nan"),
                      "low_iter_share": float(np.mean(li)) if li else float("nan"),
                      "deterministic": rs[0]["config"]["family"] in DETERMINISTIC_FAMILIES})
    cdf = pd.DataFrame(cells, columns=["cell_key", "target", "horizon", "family", "train_months", "groups", "n_seeds",
                                       "mean", "std", "p_value", "low_iter_share", "deterministic"])
    cdf["deterministic"] = cdf["deterministic"].astype(bool)
    cdf["score"] = cdf["mean"] - cdf["std"]
    cdf = cdf.sort_values("score", ascending=False, na_position="last", kind="mergesort").reset_index(drop=True)
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
    return {"factors": fdf, "cells": cdf}


# ================================================================== job handler
def handle_ml_grid(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task ``ml_grid``. Payload: ``out_dir``, ``cpu_slots``, ``gpu_slots`` (all required), exactly one of ``config_path``
    / ``cells``, and optional ``limit_cells``, ``cache_root``, ``settings`` (``GridSettings`` overrides), ``cores``,
    ``rerun_mismatched``."""
    for k in ("out_dir", "cpu_slots", "gpu_slots"):
        if k not in payload:
            raise ValueError(f"ml_grid payload is missing {k!r}")
    if bool(payload.get("config_path")) == (payload.get("cells") is not None):
        raise ValueError("ml_grid payload needs exactly one of 'config_path' and 'cells'")
    cells = load_cells(payload["config_path"] if payload.get("config_path") else payload["cells"])
    limit = payload.get("limit_cells")
    if limit is not None:
        if not _is_int(limit) or limit < 1:
            raise ValueError("limit_cells must be an integer >= 1")
        cells = cells[:limit]
    sd = dict(payload.get("settings") or {})
    if payload.get("cores") is not None:
        sd["cores"] = int(payload["cores"])
    elif "cores" not in sd:
        sd["cores"] = os.cpu_count() or 1
    st = GridSettings.from_dict(sd)

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

    res = run_grid(cells, payload["out_dir"], int(payload["cpu_slots"]), int(payload["gpu_slots"]),
                   payload.get("cache_root"), st, progress,
                   rerun_mismatched=bool(payload.get("rerun_mismatched", False)))
    progress(f"ml_grid done: {res['done']} rows, {res['errors']} errors, {res['skipped_existing']} already finished",
             100.0)
    return res
