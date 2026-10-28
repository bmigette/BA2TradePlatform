"""Shuffle controls and baselines for the ML exploration v2 grid.

A model score that "works" may be exploiting something other than what it was built for. Two permutation controls
say where a signal lives, each keeping the score distribution intact:

* ``shuffle_symbols``: within every session the scores are permuted across symbols. The session's multiset of scores
  is unchanged, but which name carries which score is random. It destroys a CROSS-SECTIONAL signal (rank IC, quintile
  spread) and leaves a signal that is common to all names on a day (market timing) untouched.
* ``shuffle_time``: for every symbol the scores are permuted across its sessions. The symbol's multiset of scores is
  unchanged. It destroys a TIMING signal (score high on the days the name does well) and leaves a persistent
  name-level signal (a symbol that always scores high and always does well) untouched.

``controls_report`` puts a metric of the real scores beside the same metric over several seeds of each shuffle and,
when their score frames are passed, the naive momentum and linear baselines. A real signal sits many shuffle standard
deviations away from the shuffle mean; a model that merely re-discovers a persistent name effect keeps its value under
``shuffle_time``, one that merely tracks the market keeps it under ``shuffle_symbols``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from app.services.ml_eval import flat

_OUTCOMES = ("ret", "rel_ret", "vol")


def _permute_within(df: pd.DataFrame, by: str, seed: int) -> pd.DataFrame:
    """Copy of ``flat(df)`` whose ``score`` is permuted within each group of ``by``. Groups are visited in sorted
    order and rows within a group in (session, symbol) order, so the result depends only on the data and the seed."""
    d = flat(df, ("score",))
    rng = np.random.default_rng(seed)
    score = d["score"].to_numpy(copy=True)
    for _, pos in sorted(d.groupby(by, sort=True).indices.items(), key=lambda kv: kv[0]):
        score[pos] = score[rng.permutation(pos)]
    d["score"] = score
    return d


def shuffle_symbols(preds: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Permute ``score`` across symbols within each session (the per-session score multiset is preserved)."""
    return _permute_within(preds, "session", seed)


def shuffle_time(preds: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Permute each symbol's ``score`` across its sessions (the per-symbol score multiset is preserved)."""
    return _permute_within(preds, "symbol", seed)


def _join(preds: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    p = flat(preds, ("score",))[["symbol", "session", "score"]]
    o = flat(outcomes)
    cols = [c for c in _OUTCOMES if c in o.columns]
    if not cols:
        raise ValueError(f"outcomes frame has none of {_OUTCOMES}")
    return p.merge(o[["symbol", "session", *cols]], on=["symbol", "session"], how="left")


def _summary(values: Sequence[float]) -> Dict[str, Any]:
    v = np.asarray(values, dtype=float)
    return {"mean": float(np.nanmean(v)) if np.isfinite(v).any() else float("nan"),
            "std": float(np.nanstd(v, ddof=1)) if np.isfinite(v).sum() > 1 else float("nan"),
            "values": [float(x) for x in v]}


def controls_report(preds: pd.DataFrame, outcomes: pd.DataFrame, metric_fn: Callable[[pd.DataFrame], float],
                    seeds: Sequence[int] = (1, 2, 3), naive: Optional[pd.DataFrame] = None,
                    linear: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """The model's metric beside its controls.

    ``preds`` / ``naive`` / ``linear``: frames with ``symbol``, ``session``, ``score``; ``outcomes``: ``symbol``,
    ``session`` and ``ret`` / ``rel_ret`` / ``vol``; ``metric_fn``: a frame with score and outcomes -> float (e.g.
    ``ml_eval.rank_ic_metric()``). The model's rows without a finite score or outcome are dropped first, so the
    model, both shuffles and the baselines are measured on the same sessions and names (a baseline missing a score
    there, e.g. momentum before its window fills, simply has NaN on that row and the metric skips it).

    Returns ``{"model": m, "shuffle_symbols": {mean, std, values}, "shuffle_time": {...}, "seeds": [...]}`` plus
    ``"naive"`` / ``"linear"`` (floats) when their frames are passed. ``std`` is across seeds (ddof 1)."""
    if not len(seeds):
        raise ValueError("need at least one seed")
    base = _join(preds, outcomes)
    outcome_cols = [c for c in _OUTCOMES if c in base.columns]
    base = base.dropna(subset=["score", *outcome_cols]).reset_index(drop=True)
    if base.empty:
        raise ValueError("no row of the predictions has a score and an outcome")
    report: Dict[str, Any] = {"model": float(metric_fn(base)), "seeds": [int(s) for s in seeds]}
    report["shuffle_symbols"] = _summary([metric_fn(shuffle_symbols(base, s)) for s in seeds])
    report["shuffle_time"] = _summary([metric_fn(shuffle_time(base, s)) for s in seeds])
    for name, frame in (("naive", naive), ("linear", linear)):
        if frame is None:
            continue
        f = flat(frame, ("score",))[["symbol", "session", "score"]].rename(columns={"score": "_other"})
        m = base.drop(columns="score").merge(f, on=["symbol", "session"], how="left").rename(columns={"_other": "score"})
        report[name] = float(metric_fn(m))
    return report
