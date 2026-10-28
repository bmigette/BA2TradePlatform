"""Cross-sectional ranking of a forecast store's symbols at a decision date (the MLForecast ``rank_top_n`` mode).

The ranked universe is EXPLICIT (a caller-passed symbol set, in the expert the ``rank_universe`` setting), never the
store directory's contents: a worker's local store may hold other symbols (a locked holdout, a wider build) and must
not change a rank. ``universe_id`` names the set (recorded with every decision).

One ``RankingPanel`` per (store root, model key, horizon, universe id, store stamp) holds, per symbol, the sorted
sessions and the two score arrays of ``mapping.bullish_score`` (float64) for ALL stored sessions of the universe,
built once from the decile / class columns only (never the TP/SL path cells). A rank at any cutoff is then a
``searchsorted`` per symbol plus the staleness rule, with no parquet read. The reference (slow) path is
``store.read_session`` + ``mapping.bullish_score``; the tests pin that both give identical ranks.

Point in time: only sessions <= the cutoff are ever looked up (the arrays hold later sessions too, which the
``searchsorted(side="right")`` never selects). Rank 1 = most bullish; equal scores are ordered by symbol name.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from functools import lru_cache
from datetime import date
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np

from . import mapping, store

#: Compact panels kept (each is sessions + 2 float64 arrays per symbol for one horizon).
PANEL_CACHE_SIZE = 4
#: Per-cutoff rankings kept per panel (the expert asks for the same date once per symbol).
CUTOFF_MEMO_SIZE = 16


@lru_cache(maxsize=256)
def _parse(raw) -> Tuple[Tuple[str, ...], str]:
    """``(sorted unique symbols, id)`` of a comma-separated string or a tuple of symbols. Memoised: the expert asks for
    the same universe once per symbol per decision date, and a wide universe is not cheap to validate and hash."""
    syms = tuple(store._universe(raw.split(",") if isinstance(raw, str) else raw))
    return syms, hashlib.sha256(",".join(syms).encode("utf-8")).hexdigest()[:16]


def parse_universe(symbols) -> Tuple[Tuple[str, ...], str]:
    """``(normalised symbols, universe id)``. ``symbols`` is a comma-separated string (the ``rank_universe`` setting) or
    an iterable of symbols; both spell the same set the same way. An empty or unsafe universe raises ``ValueError``."""
    if isinstance(symbols, str):
        return _parse(symbols)
    return _parse(tuple(symbols))


def normalize_universe(symbols) -> Tuple[str, ...]:
    """The sorted, de-duplicated, upper-cased symbols (validated as store-safe); an empty universe is refused."""
    return parse_universe(symbols)[0]


def universe_id(symbols) -> str:
    """First 16 hex characters of the sha256 of the sorted symbols joined by ``,`` (order and case independent)."""
    return parse_universe(symbols)[1]


class RankingPanel:
    def __init__(self, syms: List[str], sessions: List[np.ndarray], p: List[np.ndarray], r: List[np.ndarray]):
        self.syms, self.sessions, self.p, self.r = syms, sessions, p, r
        self._memo: "OrderedDict[tuple, Tuple[Mapping[str, int], int]]" = OrderedDict()

    @classmethod
    def build(cls, model_key: str, horizon: int, symbols: Iterable[str],
              cache_root: Optional[os.PathLike] = None) -> "RankingPanel":
        with_classes, entries = store.load_score_entries(model_key, horizon, symbols, cache_root)
        syms, sess, ps, rs = [], [], [], []
        for sym, (sessions, vals) in entries.items():
            p = np.empty(len(sessions), dtype=np.float64)
            r = np.empty(len(sessions), dtype=np.float64)
            for i in range(len(sessions)):
                if with_classes:
                    p[i], r[i] = mapping.bullish_score_classes(store.class_factors(vals[i]))
                else:
                    p[i], r[i] = mapping.bullish_score_deciles(tuple(float(x) for x in vals[i]))
            syms.append(sym), sess.append(sessions), ps.append(p), rs.append(r)
        return cls(syms, sess, ps, rs)

    def ranks_at(self, cutoff: date, max_age_days: int = store.MAX_STALE_CALENDAR_DAYS) -> Tuple[Mapping[str, int], int]:
        """``({symbol: rank}, n)`` over the symbols whose latest session <= ``cutoff`` is fresh (within
        ``max_age_days``); the mapping is read-only."""
        store._check_read_args(cutoff, max_age_days)
        mk = (cutoff, max_age_days)
        hit = self._memo.get(mk)
        if hit is not None:
            self._memo.move_to_end(mk)
            return hit
        c = np.datetime64(cutoff, "D")
        age = np.timedelta64(max_age_days, "D")
        scored = []
        for sym, sessions, p, r in zip(self.syms, self.sessions, self.p, self.r):
            idx = int(np.searchsorted(sessions, c, side="right")) - 1
            if idx < 0 or c - sessions[idx] > age:
                continue
            scored.append((-p[idx], -r[idx], sym))
        scored.sort()
        out = (MappingProxyType({sym: i + 1 for i, (_, _, sym) in enumerate(scored)}), len(scored))
        self._memo[mk] = out
        while len(self._memo) > CUTOFF_MEMO_SIZE:
            self._memo.popitem(last=False)
        return out


_lock = threading.Lock()
_panels: "OrderedDict[tuple, RankingPanel]" = OrderedDict()


def reset_panels() -> None:
    with _lock:
        _panels.clear()


def session_ranks(model_key: str, cutoff: date, horizon: int, symbols,
                  cache_root: Optional[os.PathLike] = None,
                  max_age_days: int = store.MAX_STALE_CALENDAR_DAYS,
                  universe_id: Optional[str] = None) -> Tuple[Mapping[str, int], int]:
    """``({symbol: rank}, n)`` of the universe's fresh rows at ``cutoff`` (rank 1 = most bullish, ties by symbol).

    The panel is keyed by the store's ``meta.json`` stamp, so a store rewritten through ``write_rows`` (in this or
    another process) is rebuilt; at most ``PANEL_CACHE_SIZE`` panels are kept. A universe symbol with no store file
    raises ``store.ForecastStoreError``; a stale symbol is absent.

    ``universe_id``: pass the id that ``parse_universe`` returned with ``symbols`` (then already the normalised tuple) to
    skip re-deriving it; omitted, ``symbols`` is parsed (memoised) here."""
    if universe_id is None:
        syms, uid = parse_universe(symbols)
    else:
        syms, uid = tuple(symbols), universe_id
    key = (str(store.root(cache_root)), model_key, horizon, uid, store.store_stamp(model_key, cache_root))
    with _lock:
        panel = _panels.get(key)
        if panel is None:
            panel = RankingPanel.build(model_key, horizon, syms, cache_root)
            _panels[key] = panel
            while len(_panels) > PANEL_CACHE_SIZE:
                _panels.popitem(last=False)
        else:
            _panels.move_to_end(key)
        return panel.ranks_at(cutoff, max_age_days)
