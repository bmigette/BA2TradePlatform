"""Shuffle controls and baselines for the ML exploration v2 grid.

A model score that "works" may be exploiting something other than what it was built for. Two permutation controls
say where a signal lives, each keeping the score distribution intact:

* ``shuffle_symbols``: within every session the scores are permuted across symbols. The session's multiset of scores
  is unchanged, but which name carries which score is random. It destroys a CROSS-SECTIONAL signal (rank IC, quintile
  spread) and leaves a signal that is common to all names on a day (market timing) untouched.
* ``shuffle_time``: for every symbol the scores are permuted across its sessions. The symbol's multiset of scores is
  unchanged. It destroys a TIMING signal (score high on the days the name does well) and leaves a persistent
  name-level signal (a symbol that always scores high and always does well) untouched.

* ``shuffle_time_within_block``: the scores of every symbol are permuted across its sessions INSIDE each walk-forward
  block (a ``block`` column naming the model that scored the row). Block-level information survives (which model, how
  confident it was on average, a regime the block sits in), only the day-to-day placement is destroyed. A timing model
  whose "in" share merely tracks the regime keeps its metric under this null although the full ``shuffle_time`` would
  wipe it out: it is the STRICTER of the two timing nulls, and the one to gate on together with ``shuffle_time``.

``controls_report`` puts a metric of the real scores beside the same metric over 20 seeds of each shuffle (and the
model's empirical rank / one-sided p-value among them) and, when their score frames are passed, the naive momentum
and linear baselines. A real signal sits far from the shuffle distribution; a model that merely re-discovers a
persistent name effect keeps its value under ``shuffle_time``, one that merely tracks the market keeps it under
``shuffle_symbols``.

Order of operations for timing metrics: shuffle the score, THEN compute the causal cutoff from the shuffled score,
THEN evaluate (and only then slice by regime). Hence a timing metric passed to ``controls_report`` must be built with
``ml_eval.timing_metric(q=...)``, which recomputes the cutoff on each frame it receives; a cutoff column computed
before the shuffle would keep describing the unshuffled scores and make the control meaningless.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from app.services.ml_eval import flat

_OUTCOMES = ("ret", "rel_ret", "vol")


def _permute_within(df: pd.DataFrame, by: Union[str, List[str]], seed: int) -> pd.DataFrame:
    """Copy of ``flat(df)`` whose ``score`` is permuted within each group of ``by`` (a column or a list of columns). Groups are visited in sorted
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


def shuffle_time_within_block(preds: pd.DataFrame, seed: int, block_col: str = "block") -> pd.DataFrame:
    """Permute each symbol's ``score`` across its sessions within each block (``block_col``): the per-(symbol, block)
    score multiset is preserved. Refuses a frame without the block column."""
    if block_col not in preds.columns:
        raise ValueError(f"the within-block shuffle needs a {block_col!r} column (the walk-forward block of each row)")
    return _permute_within(preds, ["symbol", block_col], seed)


def _join(preds: pd.DataFrame, outcomes: pd.DataFrame, extra: Sequence[str] = ()) -> pd.DataFrame:
    p = flat(preds, ("score", *extra))[["symbol", "session", "score", *extra]]
    o = flat(outcomes)
    cols = [c for c in _OUTCOMES if c in o.columns]
    if not cols:
        raise ValueError(f"outcomes frame has none of {_OUTCOMES}")
    return p.merge(o[["symbol", "session", *cols]], on=["symbol", "session"], how="left")


DEFAULT_SEEDS: Tuple[int, ...] = tuple(range(1, 21))


def empirical_rank(model: float, shuffled: Sequence[float]) -> Tuple[Optional[int], Optional[float]]:
    """``(rank, p_value)`` of ``model`` among the finite ``shuffled`` values, higher = better.
    ``rank = 1 + #{shuffled > model}`` (1 = beats every shuffle); ``p_value = (1 + #{shuffled >= model}) / (1 + n)``,
    the one-sided permutation p-value with the model counted as one of the draws (so never below ``1 / (1 + n)``;
    ties count against the model). ``(None, None)`` when the model value or every shuffle is not finite."""
    v = np.asarray(shuffled, dtype=float)
    v = v[np.isfinite(v)]
    if not np.isfinite(model) or not len(v):
        return None, None
    return 1 + int((v > model).sum()), (1 + int((v >= model).sum())) / (1 + len(v))


def _summary(values: Sequence[float], model: float) -> Dict[str, Any]:
    v = np.asarray(values, dtype=float)
    rank, p = empirical_rank(model, v)
    return {"mean": float(np.nanmean(v)) if np.isfinite(v).any() else float("nan"),
            "std": float(np.nanstd(v, ddof=1)) if np.isfinite(v).sum() > 1 else float("nan"),
            "values": [float(x) for x in v], "rank": rank, "p_value": p}


def _regime_summaries(model: Dict[str, float], per_seed: Sequence[Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    """Per regime (every key but ``all``): the shuffles' mean / std / rank / one-sided p-value of the model's value
    there (same formula as the whole period, ``empirical_rank``) and the model's own value."""
    out: Dict[str, Dict[str, Any]] = {}
    for reg in model:
        if reg == "all":
            continue
        vals = [seed_vals[reg] if reg in seed_vals else float("nan") for seed_vals in per_seed]
        s = _summary(vals, model[reg])
        out[reg] = {"mean": s["mean"], "std": s["std"], "p_value": s["p_value"], "rank": s["rank"],
                    "model": float(model[reg])}
    return out


def controls_report(preds: pd.DataFrame, outcomes: pd.DataFrame,
                    metric_fn: Optional[Callable[[pd.DataFrame], float]],
                    seeds: Sequence[int] = DEFAULT_SEEDS, naive: Optional[pd.DataFrame] = None,
                    linear: Optional[pd.DataFrame] = None, within_block: bool = False,
                    regime_fn: Optional[Callable[[pd.DataFrame], Dict[str, float]]] = None) -> Dict[str, Any]:
    """The model's metric beside its controls.

    ``preds`` / ``naive`` / ``linear``: frames with ``symbol``, ``session``, ``score``; ``outcomes``: ``symbol``,
    ``session`` and ``ret`` / ``rel_ret`` / ``vol``; ``metric_fn``: a frame with score and outcomes -> float (e.g.
    ``ml_eval.rank_ic_metric()``). The model's rows without a finite score or outcome are dropped first, so the
    model, both shuffles and the baselines are measured on the same sessions and names (a baseline missing a score
    there, e.g. momentum before its window fills, simply has NaN on that row and the metric skips it).

    Returns ``{"model": m, "shuffle_symbols": {mean, std, values, rank, p_value}, "shuffle_time": {...},
    "seeds": [...]}`` plus ``"shuffle_time_within_block"`` (``within_block=True``; ``preds`` needs a ``block`` column) plus ``"naive"`` / ``"linear"`` (floats) when their frames are passed. ``std`` is across seeds
    (ddof 1); ``rank`` / ``p_value`` are the model's among the shuffles (``empirical_rank``). The frames handed to
    ``metric_fn`` are already in ``ml_eval.flat`` layout, so a metric may be built with ``assume_flat=True``.
    ``metric_fn`` is applied to every shuffled frame: for a timing metric use ``timing_metric(q=...)`` (module
    docstring).

    ``regime_fn`` (instead of ``metric_fn``, which may then be None): a frame -> ``{"all": whole period, regime:
    value, ...}``. The whole-period numbers are its ``"all"`` and are exactly what ``metric_fn`` would give; each
    shuffle summary also gets ``"by_regime": {regime: {mean, std, p_value, rank, model}}``, the model's value in the
    regime ranked among the shuffles' values in that same regime. A regime fn must slice AFTER any shuffle-dependent
    step (a causal cutoff), as the module docstring says."""
    if not len(seeds):
        raise ValueError("need at least one seed")
    if metric_fn is None and regime_fn is None:
        raise ValueError("pass metric_fn or regime_fn")
    if regime_fn is not None:
        whole = lambda f: float(regime_fn(f)["all"])                                              # noqa: E731
    else:
        whole = metric_fn
    base = _join(preds, outcomes, ("block",) if within_block else ())
    outcome_cols = [c for c in _OUTCOMES if c in base.columns]
    base = base.dropna(subset=["score", *outcome_cols]).reset_index(drop=True)
    if base.empty:
        raise ValueError("no row of the predictions has a score and an outcome")
    model_reg = regime_fn(base) if regime_fn is not None else None
    model = float(model_reg["all"]) if model_reg is not None else float(metric_fn(base))
    report: Dict[str, Any] = {"model": model, "seeds": [int(s) for s in seeds]}
    shufflers = [("shuffle_symbols", shuffle_symbols), ("shuffle_time", shuffle_time)]
    if within_block:
        shufflers.append(("shuffle_time_within_block", shuffle_time_within_block))
    for name, shuffler in shufflers:
        frames = [regime_fn(shuffler(base, s)) for s in seeds] if regime_fn is not None else None
        report[name] = _summary([f["all"] for f in frames] if frames is not None
                                else [metric_fn(shuffler(base, s)) for s in seeds], model)
        if frames is not None:
            report[name]["by_regime"] = _regime_summaries(model_reg, frames)
    for name, frame in (("naive", naive), ("linear", linear)):
        if frame is None:
            continue
        f = flat(frame, ("score",))[["symbol", "session", "score"]].rename(columns={"score": "_other"})
        m = base.drop(columns="score").merge(f, on=["symbol", "session"], how="left").rename(columns={"_other": "score"})
        report[name] = whole(m)
    return report
