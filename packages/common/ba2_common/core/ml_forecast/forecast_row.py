"""Forecast store row: one per (symbol, session).

The row at session S is forecast from bars up to and including S, so it is usable by any decision on S+1 or later.

Deciles are FRACTIONAL returns (0.02 = +2%) of the close H sessions after S relative to the close of S.
Path models also carry P(TP before SL) over a fixed percentage grid, for a long entry (TP above, SL below) and a
short entry (TP below, SL above), within H sessions.

Class models (output type ``class_probs``, the custom tsai models) carry, instead of deciles, three ordered classes
per horizon -- ``down`` (stop first / falls), ``mid`` (timeout / flat), ``up`` (take-profit first / rises) -- each
with its probability and its RETURN VALUE: the fractional return that outcome stands for at this row (triple barrier:
-m x ATR% / the training fold's mean timeout return / +k x ATR%; direction terciles: the training fold's class means).
Probabilities sum to 1; ``r_down < r_up``.

Column names (parquet): ``session``, ``h{H}_q{10..90}``, ``p_{long|short}_h{H}_tp{TP}_sl{SL}``,
``h{H}_p_{down|mid|up}``, ``h{H}_r_{down|mid|up}``.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

DECILE_LEVELS: Tuple[int, ...] = (10, 20, 30, 40, 50, 60, 70, 80, 90)
# TP / SL distances in PERCENT of the entry price. Changing a grid changes the store schema: bump the card version.
TP_GRID_PCT: Tuple[int, ...] = (1, 2, 3, 5, 8, 12)
SL_GRID_PCT: Tuple[int, ...] = (1, 2, 3, 5, 8)
DIRECTIONS: Tuple[str, ...] = ("long", "short")

ProbKey = Tuple[str, int, int, int]   # (direction, horizon, tp_pct, sl_pct)
CLASSES: Tuple[str, ...] = ("down", "mid", "up")
#: tolerance of sum(probabilities) == 1 (float32 storage)
CLASS_PROB_SUM_TOL = 1e-3

ClassForecast = Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]   # ((p, r) down, mid, up)


def decile_col(horizon: int, level: int) -> str:
    return f"h{horizon}_q{level}"


def prob_col(direction: str, horizon: int, tp_pct: int, sl_pct: int) -> str:
    return f"p_{direction}_h{horizon}_tp{tp_pct}_sl{sl_pct}"


def class_cols(horizon: int) -> List[str]:
    return [f"h{horizon}_p_{c}" for c in CLASSES] + [f"h{horizon}_r_{c}" for c in CLASSES]


def record_columns(horizons: Iterable[int], with_paths: bool, with_classes: bool = False) -> List[str]:
    cols = ["session"]
    if with_classes:
        for h in horizons:
            cols += class_cols(h)
        return cols
    for h in horizons:
        cols += [decile_col(h, q) for q in DECILE_LEVELS]
    if with_paths:
        for h in horizons:
            cols += [prob_col(d, h, tp, sl) for d in DIRECTIONS for tp in TP_GRID_PCT for sl in SL_GRID_PCT]
    return cols


def _check_classes(h: int, cf) -> ClassForecast:
    cf = tuple(tuple(x) for x in cf)
    if len(cf) != 3 or any(len(x) != 2 for x in cf):
        raise ValueError(f"horizon {h}: class forecast must be 3 (probability, return) pairs, got {cf!r}")
    if not all(_finite(v) for x in cf for v in x):
        raise ValueError(f"horizon {h}: non-finite class forecast {cf!r}")
    ps = [float(p) for p, _ in cf]
    if any(p < 0.0 or p > 1.0 for p in ps) or abs(sum(ps) - 1.0) > CLASS_PROB_SUM_TOL:
        raise ValueError(f"horizon {h}: class probabilities must be in [0, 1] and sum to 1, got {ps!r}")
    rd, ru = float(cf[0][1]), float(cf[2][1])
    if not rd < ru:
        raise ValueError(f"horizon {h}: down return {rd} must be below up return {ru}")
    if rd <= -1.0:
        raise ValueError(f"horizon {h}: down return {rd} is at or below -100%")
    return tuple((float(p), float(r)) for p, r in cf)


def _finite(x: Any) -> bool:
    return isinstance(x, numbers.Real) and not isinstance(x, bool) and math.isfinite(float(x))


@dataclass(frozen=True)
class ForecastRow:
    symbol: str
    session: date
    deciles: Mapping[int, Tuple[float, ...]]            # horizon -> (q10..q90) returns; empty for a class model
    path_probs: Optional[Mapping[ProbKey, float]] = None  # None = a decile-only model
    class_probs: Optional[Mapping[int, ClassForecast]] = None   # horizon -> ((p, r) down, mid, up); class models

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError(f"ForecastRow.symbol must be a non-blank str, got {self.symbol!r}")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not isinstance(self.session, date) or hasattr(self.session, "hour"):
            raise TypeError(f"ForecastRow.session must be a plain date, got {self.session!r}")
        if self.class_probs is not None:
            if self.deciles or self.path_probs is not None:
                raise ValueError("a class-probability row carries no deciles and no path probabilities")
            if not self.class_probs:
                raise ValueError("ForecastRow.class_probs must not be empty")
            cp = {}
            for h, cf in self.class_probs.items():
                if isinstance(h, bool) or not isinstance(h, int) or h <= 0:
                    raise ValueError(f"ForecastRow horizon must be a positive int, got {h!r}")
                cp[h] = _check_classes(h, cf)
            object.__setattr__(self, "class_probs", dict(sorted(cp.items())))
            object.__setattr__(self, "deciles", {})
            return
        if not self.deciles:
            raise ValueError("ForecastRow.deciles must not be empty")
        dec: Dict[int, Tuple[float, ...]] = {}
        for h, q in self.deciles.items():
            if isinstance(h, bool) or not isinstance(h, int) or h <= 0:
                raise ValueError(f"ForecastRow horizon must be a positive int, got {h!r}")
            q = tuple(q)
            if len(q) != len(DECILE_LEVELS):
                raise ValueError(f"horizon {h}: expected {len(DECILE_LEVELS)} deciles, got {len(q)}")
            if not all(_finite(x) for x in q):
                raise ValueError(f"horizon {h}: non-finite decile in {q!r}")
            if q[0] <= -1.0:
                raise ValueError(f"horizon {h}: q10 return {q[0]} is at or below -100%")
            if any(b < a for a, b in zip(q, q[1:])):
                raise ValueError(f"horizon {h}: deciles are not monotone non-decreasing: {q!r}")
            dec[h] = tuple(float(x) for x in q)
        object.__setattr__(self, "deciles", dict(sorted(dec.items())))
        if self.path_probs is not None:
            pp = dict(self.path_probs)
            expected = {(d, h, tp, sl) for d in DIRECTIONS for h in dec for tp in TP_GRID_PCT for sl in SL_GRID_PCT}
            if set(pp) != expected:
                raise ValueError(
                    f"path_probs must cover the full grid exactly: missing {len(expected - set(pp))}, "
                    f"unexpected {sorted(set(pp) - expected)[:3]}")
            for k, p in pp.items():
                if not _finite(p) or not 0.0 <= p <= 1.0:
                    raise ValueError(f"path probability {k} must be in [0, 1], got {p!r}")
            object.__setattr__(self, "path_probs", {k: float(v) for k, v in pp.items()})

    @property
    def horizons(self) -> Tuple[int, ...]:
        return tuple(self.class_probs) if self.class_probs is not None else tuple(self.deciles)

    @property
    def has_paths(self) -> bool:
        return self.path_probs is not None

    @property
    def has_classes(self) -> bool:
        return self.class_probs is not None

    def decile(self, horizon: int, level: int) -> float:
        if horizon not in self.deciles:
            raise KeyError(f"horizon {horizon} not in row (has {self.horizons})")
        return self.deciles[horizon][DECILE_LEVELS.index(level)]

    def path_prob(self, direction: str, horizon: int, tp_pct: int, sl_pct: int) -> float:
        if self.path_probs is None:
            raise KeyError("row has no path probabilities")
        return self.path_probs[(direction, horizon, tp_pct, sl_pct)]

    def to_record(self) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"session": self.session}
        if self.class_probs is not None:
            for h, cf in self.class_probs.items():
                for c, (p, r) in zip(CLASSES, cf):
                    rec[f"h{h}_p_{c}"] = p
                    rec[f"h{h}_r_{c}"] = r
            return rec
        for h, q in self.deciles.items():
            for lvl, v in zip(DECILE_LEVELS, q):
                rec[decile_col(h, lvl)] = v
        if self.path_probs is not None:
            for (d, h, tp, sl), p in self.path_probs.items():
                rec[prob_col(d, h, tp, sl)] = p
        return rec

    @classmethod
    def from_record(cls, symbol: str, rec: Mapping[str, Any], horizons: Iterable[int], with_paths: bool,
                    with_classes: bool = False) -> "ForecastRow":
        horizons = tuple(horizons)
        missing = [c for c in record_columns(horizons, with_paths, with_classes) if c not in rec]
        if missing:
            raise ValueError(f"forecast record for {symbol} is missing columns {missing[:5]}")
        sess = rec["session"]
        if hasattr(sess, "date") and callable(sess.date):   # pandas Timestamp / datetime
            sess = sess.date()
        if with_classes:
            cp = {h: tuple((float(rec[f"h{h}_p_{c}"]), float(rec[f"h{h}_r_{c}"])) for c in CLASSES) for h in horizons}
            return cls(symbol=symbol, session=sess, deciles={}, class_probs=cp)
        dec = {h: tuple(float(rec[decile_col(h, q)]) for q in DECILE_LEVELS) for h in horizons}
        pp = None
        if with_paths:
            pp = {(d, h, tp, sl): float(rec[prob_col(d, h, tp, sl)])
                  for d in DIRECTIONS for h in horizons for tp in TP_GRID_PCT for sl in SL_GRID_PCT}
        return cls(symbol=symbol, session=sess, deciles=dec, path_probs=pp)
