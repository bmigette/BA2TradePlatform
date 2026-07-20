"""As-of-clamped reader over OptionsHistoryCache. Returns ONLY data dated <= the engine
clock (no lookahead). Chain rows are mapped to OptionContract; bars stay dicts.

Worker-process-level in-memory cache (2026-07-20 fix, mirrors price_source.py's
_WORKER_BAR_CACHE and FMPSenateTraderWeight's _WORKER_SCORING_CACHE): before this fix,
OptionsHistoryCache opened a FRESH sqlite3 connection on EVERY single read (read_chain,
read_bar, latest_chain_as_of, latest_bar_on_or_before), and a fresh HistoricalOptionsProvider
was built once per GA trial from the SAME shared cache file -- so one trial issued thousands of
tiny DB round-trips (one per bar per contract), repeated identically by every trial in the job
and by every worker process. The options cache file is large (2.6GB, ~14M bar rows across ~100
underlyings as of 2026-07-20) so it can't be loaded wholesale into a worker's memory the way
FMPSenateTraderWeight's scoring caches are -- instead this caches PER (db_path, underlying)
chain history and PER (db_path, occ_symbol) bar history, lazily on first access, shared across
every HistoricalOptionsProvider built in the same worker process for the rest of its life. A
single backtest only ever touches a handful of underlyings/contracts, so the lazy per-key load
stays small regardless of the whole cache file's size. Bounded LRU (not unbounded) because a
REMOTE worker's trial-serving process pool is long-lived across many different optimization
jobs (see worker_server.py's module-level _POOL), so keys could otherwise accumulate across
jobs touching different universes over the process's lifetime."""
from __future__ import annotations
from bisect import bisect_right
from collections import OrderedDict
from datetime import date
import os
import sqlite3
from typing import Dict, List, Optional, Tuple
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from .options_cache import OptionsHistoryCache

_CHAIN_CACHE_MAX = int(os.getenv("BT_OPTION_CHAIN_CACHE_MAX", "300"))
_BAR_CACHE_MAX = int(os.getenv("BT_OPTION_BAR_CACHE_MAX", "3000"))

_WORKER_CHAIN_CACHE: "OrderedDict[Tuple[str, str], _ChainHistory]" = OrderedDict()
_WORKER_BAR_CACHE: "OrderedDict[Tuple[str, str], _BarHistory]" = OrderedDict()


def clear_worker_options_cache() -> None:
    """Drop every cached chain/bar history (test isolation / explicit reset)."""
    _WORKER_CHAIN_CACHE.clear()
    _WORKER_BAR_CACHE.clear()


class _ChainHistory:
    """One underlying's full chain history: every cached (as_of -> rows) snapshot."""
    __slots__ = ("by_asof", "dates")

    def __init__(self, by_asof: Dict[str, List[dict]]):
        self.by_asof = by_asof
        self.dates = sorted(by_asof)  # ascending, for bisect

    def latest_as_of(self, on_or_before: str) -> Optional[str]:
        i = bisect_right(self.dates, on_or_before)
        return self.dates[i - 1] if i else None


class _BarHistory:
    """One contract's full daily-bar history."""
    __slots__ = ("by_date", "dates")

    def __init__(self, by_date: Dict[str, dict]):
        self.by_date = by_date
        self.dates = sorted(by_date)  # ascending, for bisect

    def latest_on_or_before(self, on_or_before: str) -> Optional[dict]:
        i = bisect_right(self.dates, on_or_before)
        return self.by_date[self.dates[i - 1]] if i else None


def _load_chain_history(db_path: str, underlying: str) -> _ChainHistory:
    cx = sqlite3.connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        by_asof: Dict[str, List[dict]] = {}
        for r in cx.execute(
            "SELECT * FROM option_chain WHERE underlying=? ORDER BY as_of", (underlying,)
        ):
            by_asof.setdefault(r["as_of"], []).append(dict(r))
    finally:
        cx.close()
    return _ChainHistory(by_asof)


def _load_bar_history(db_path: str, occ_symbol: str) -> _BarHistory:
    cx = sqlite3.connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        by_date = {
            r["date"]: dict(r)
            for r in cx.execute(
                "SELECT * FROM option_bar WHERE occ_symbol=? ORDER BY date", (occ_symbol,)
            )
        }
    finally:
        cx.close()
    return _BarHistory(by_date)


def _chain_history(db_path: str, underlying: str) -> _ChainHistory:
    key = (db_path, underlying)
    hist = _WORKER_CHAIN_CACHE.get(key)
    if hist is not None:
        _WORKER_CHAIN_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _load_chain_history(db_path, underlying)
    _WORKER_CHAIN_CACHE[key] = hist
    while len(_WORKER_CHAIN_CACHE) > _CHAIN_CACHE_MAX:
        _WORKER_CHAIN_CACHE.popitem(last=False)
    return hist


def _bar_history(db_path: str, occ_symbol: str) -> _BarHistory:
    key = (db_path, occ_symbol)
    hist = _WORKER_BAR_CACHE.get(key)
    if hist is not None:
        _WORKER_BAR_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _load_bar_history(db_path, occ_symbol)
    _WORKER_BAR_CACHE[key] = hist
    while len(_WORKER_BAR_CACHE) > _BAR_CACHE_MAX:
        _WORKER_BAR_CACHE.popitem(last=False)
    return hist


def _to_contract(r: dict, greeks_row: Optional[dict] = None) -> OptionContract:
    # greeks_row (the AS-OF-CLAMPED daily bar for this contract, when available) carries the
    # POINT-IN-TIME iv/greeks computed by fetch_options.py's Black-Scholes inversion of that
    # day's close (see option_greeks.py) — preferred over the chain row's, which is a single
    # snapshot fixed at the build's start date and goes stale as the backtest clock advances.
    # Only prefer it when its OWN iv actually computed (non-None) — a bar can exist with
    # iv/greeks still None (missing underlying close that day, or a pre-existing cache built
    # before this feature), in which case fall back to the chain row rather than lose greeks.
    g = greeks_row if (greeks_row and greeks_row.get("iv") is not None) else r
    return OptionContract(
        symbol=r["occ_symbol"], underlying=r.get("underlying") or "",
        option_type=OptionRight(r["option_type"]), strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), bid=r.get("bid"), ask=r.get("ask"),
        last=r.get("last"), implied_volatility=g.get("iv"), delta=g.get("delta"),
        gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"),
        open_interest=r.get("open_interest"), volume=r.get("volume"))

class HistoricalOptionsProvider:
    def __init__(self, cache_db: str):
        self.cache = OptionsHistoryCache(cache_db)
        self.db_path = self.cache.db_path

    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        hist = _chain_history(self.db_path, underlying)
        snap = hist.latest_as_of(as_of.isoformat())
        if snap is None:
            return []
        out: List[OptionContract] = []
        for r in hist.by_asof[snap]:
            r = {**r, "underlying": underlying}
            exp = date.fromisoformat(r["expiry"])
            if exp < expiry_min or exp > expiry_max:
                continue
            if option_type is not None and r["option_type"] != option_type.value:
                continue
            if strike_min is not None and r["strike"] < strike_min:
                continue
            if strike_max is not None and r["strike"] > strike_max:
                continue
            greeks_row = _bar_history(self.db_path, r["occ_symbol"]).latest_on_or_before(as_of.isoformat())
            out.append(_to_contract(r, greeks_row))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        bar = _bar_history(self.db_path, occ_symbol).by_date.get(as_of.isoformat())
        if bar is None:
            return None
        return OptionQuote(symbol=occ_symbol, bid=None, ask=None, last=bar.get("close"))

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        return _bar_history(self.db_path, occ_symbol).by_date.get(as_of.isoformat())

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """Mean IV across the underlying's cached contracts as of ``as_of`` — feeds
        ``IVRankCondition``'s rolling history. Reads each contract's AS-OF-CLAMPED daily-bar IV
        (point-in-time, computed by fetch_options.py's Black-Scholes inversion), not the static
        start-date chain snapshot, so this tracks IV changes across the whole backtest window."""
        hist = _chain_history(self.db_path, underlying)
        snap = hist.latest_as_of(as_of.isoformat())
        if snap is None:
            return None
        ivs: List[float] = []
        for r in hist.by_asof[snap]:
            bar = _bar_history(self.db_path, r["occ_symbol"]).latest_on_or_before(as_of.isoformat())
            iv = (bar or {}).get("iv") or r.get("iv")
            if iv:
                ivs.append(float(iv))
        return float(sum(ivs) / len(ivs)) if ivs else None
