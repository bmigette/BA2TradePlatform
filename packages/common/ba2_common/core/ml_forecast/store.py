"""Forecast store: ``<cache_root>/ml_forecast/<model_key>/<SYMBOL>.parquet`` plus ``meta.json`` (card + build params).

Reads are point in time: ``read_row`` returns the row of the latest session <= the cutoff the CALLER passes (the
expert passes ``knowability.scan_cutoff_date(as_of)``; this module never looks at a clock). A missing symbol, a missing
store, no session at or before the cutoff, or a latest session older than ``max_age_days`` raises
``ForecastStoreMiss``; there is no default row.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa

from .forecast_row import (DECILE_LEVELS, DIRECTIONS, SL_GRID_PCT, TP_GRID_PCT, ForecastRow, decile_col, prob_col,
                           record_columns)
from .model_card import ModelCard, model_key as compute_model_key

STORE_DIRNAME = "ml_forecast"
#: A long weekend plus a holiday is 4-5 calendar days; beyond a week the store has a hole, not a quiet market.
MAX_STALE_CALENDAR_DAYS = 7

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._\-]{0,19}$")


class ForecastStoreError(Exception):
    pass


class ForecastStoreMiss(ForecastStoreError, LookupError):
    """No usable forecast row for (model_key, symbol, cutoff)."""


class ForecastStoreCardMismatch(ForecastStoreError):
    """A different model card is already stored under this key."""


def _root(cache_root: Optional[os.PathLike]) -> Path:
    if cache_root is None:
        from ba2_common import config   # resolved at call time so a CACHE_FOLDER override is honoured
        cache_root = config.CACHE_FOLDER
    return Path(cache_root) / STORE_DIRNAME


def _symbol(symbol: str) -> str:
    s = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not _SYMBOL_RE.match(s):
        raise ValueError(f"unsafe or blank symbol for a store path: {symbol!r}")
    return s


def _key_dir(root: Path, key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        raise ValueError(f"model_key must be 16 hex chars, got {key!r}")
    return root / key


# ---------------------------------------------------------------- cache
#: Symbol-frames kept per (store, horizon). Bounds memory for a wide universe: an entry is
#: sessions * (9 + path cells) float32 values for ONE horizon.
MAX_CACHED_FRAMES_PER_KEY_HORIZON = 256


class _Entry(NamedTuple):
    mtime: int
    sessions: np.ndarray            # datetime64[D], sorted ascending
    dec: np.ndarray                 # (n, 9) float32
    probs: Optional[np.ndarray]     # (n, 2 * |TP grid| * |SL grid|) float32, or None for decile-only stores


_lock = threading.Lock()
_frames: Dict[Tuple[str, int], "OrderedDict[str, _Entry]"] = {}
_metas: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {}

_PROB_CELLS = [(d, tp, sl) for d in DIRECTIONS for tp in TP_GRID_PCT for sl in SL_GRID_PCT]


def reset_cache() -> None:
    """Drop the per-process frame/meta caches (tests, and after an external rebuild)."""
    with _lock:
        _frames.clear()
        _metas.clear()


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except FileNotFoundError:
        raise ForecastStoreMiss(f"forecast store file missing: {p}") from None


def _verify_meta(key: str, meta: Dict[str, Any]) -> None:
    """One value, one source: the key must equal model_key(card, covariate_set), and the redundant meta fields
    must equal what the card defines."""
    card = ModelCard.from_dict(meta["card"])
    if (meta["context_length"] != card.context_length or list(meta["horizons"]) != list(card.horizons)
            or compute_model_key(card, meta["covariate_set"]) != key or meta["model_key"] != key):
        raise ForecastStoreError(f"store {key}: meta.json is inconsistent with its model key (card, context length, "
                                 f"covariate set or horizons were altered)")


def read_meta(key: str, cache_root: Optional[os.PathLike] = None) -> Dict[str, Any]:
    kdir = _key_dir(_root(cache_root), key)
    p = kdir / "meta.json"
    mt = _mtime(p)
    ck = (str(kdir), "meta")
    with _lock:
        hit = _metas.get(ck)
        if hit is not None and hit[0] == mt:
            return hit[1]
    meta = json.loads(p.read_text(encoding="utf-8"))
    _verify_meta(key, meta)
    with _lock:
        _metas[ck] = (mt, meta)
    return meta


def _columns(horizon: int, with_paths: bool) -> List[str]:
    cols = ["session"] + [decile_col(horizon, q) for q in DECILE_LEVELS]
    if with_paths:
        cols += [prob_col(d, horizon, tp, sl) for d, tp, sl in _PROB_CELLS]
    return cols


def _load_entry(kdir: Path, sym: str, horizon: int, with_paths: bool) -> _Entry:
    """The symbol's sessions + the horizon's columns, as float32 arrays. LRU-cached; invalidated by file mtime."""
    p = kdir / f"{sym}.parquet"
    mt = _mtime(p)
    ck = (str(kdir), horizon)
    with _lock:
        lru = _frames.setdefault(ck, OrderedDict())
        hit = lru.get(sym)
        if hit is not None and hit.mtime == mt:
            lru.move_to_end(sym)
            return hit
    cols = _columns(horizon, with_paths)
    try:
        df = pd.read_parquet(p, columns=cols)
    except (KeyError, pa.ArrowInvalid) as e:
        raise ForecastStoreError(f"{p}: missing forecast columns for horizon {horizon}: {e}") from e
    sessions = pd.to_datetime(df["session"]).to_numpy().astype("datetime64[D]")
    order = np.argsort(sessions, kind="stable")
    sessions = sessions[order]
    dec = df[cols[1:10]].to_numpy(dtype=np.float32)[order]
    probs = df[cols[10:]].to_numpy(dtype=np.float32)[order] if with_paths else None
    entry = _Entry(mt, sessions, dec, probs)
    with _lock:
        lru = _frames.setdefault(ck, OrderedDict())
        lru[sym] = entry
        lru.move_to_end(sym)
        while len(lru) > MAX_CACHED_FRAMES_PER_KEY_HORIZON:
            lru.popitem(last=False)
    return entry


# ---------------------------------------------------------------- reader
def read_row(model_key: str, symbol: str, decision_session_cutoff: date, horizon: int,
             cache_root: Optional[os.PathLike] = None,
             max_age_days: int = MAX_STALE_CALENDAR_DAYS) -> ForecastRow:
    """The row of the latest session <= ``decision_session_cutoff``, restricted to ``horizon`` (its deciles and,
    for path stores, its P(TP before SL) cells). Values come back as float32-rounded floats."""
    if not isinstance(decision_session_cutoff, date) or hasattr(decision_session_cutoff, "hour"):
        raise TypeError("decision_session_cutoff must be a plain date (pass knowability.scan_cutoff_date(as_of)), "
                        f"got {decision_session_cutoff!r}")
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 1:
        raise ValueError(f"max_age_days must be an int >= 1, got {max_age_days!r}")
    sym = _symbol(symbol)
    kdir = _key_dir(_root(cache_root), model_key)
    meta = read_meta(model_key, cache_root)
    card = ModelCard.from_dict(meta["card"])
    if horizon not in card.horizons:
        raise ValueError(f"horizon {horizon} is not one of the store's horizons {card.horizons}")
    with_paths = card.output_type == "paths"
    e = _load_entry(kdir, sym, horizon, with_paths)
    idx = int(np.searchsorted(e.sessions, np.datetime64(decision_session_cutoff, "D"), side="right")) - 1
    if idx < 0:
        first = e.sessions[0].astype(object) if len(e.sessions) else "none"
        raise ForecastStoreMiss(f"{model_key}/{sym}: no forecast session on or before {decision_session_cutoff} "
                                f"(first stored: {first})")
    session = e.sessions[idx].astype(object)
    if decision_session_cutoff - session > timedelta(days=max_age_days):
        raise ForecastStoreMiss(f"{model_key}/{sym}: latest forecast session {session} is more than "
                                f"{max_age_days} days before {decision_session_cutoff}")
    dec = tuple(float(x) for x in e.dec[idx])
    pp = None
    if e.probs is not None:
        pp = {(d, horizon, tp, sl): float(v) for (d, tp, sl), v in zip(_PROB_CELLS, e.probs[idx])}
    return ForecastRow(sym, session, {horizon: dec}, pp)


# ---------------------------------------------------------------- writer
def write_rows(model_key: str, card: ModelCard, build_params: Dict[str, Any], rows: Iterable[ForecastRow],
               covariate_set: Iterable[str], cache_root: Optional[os.PathLike] = None) -> None:
    """Upsert rows (by symbol + session) into the store of ``model_key``.

    SINGLE WRITER: one process writes a given store at a time (the build tool, or the live append job). The upsert is
    read-modify-write per symbol file, so concurrent writers can lose rows. Parquet files are replaced atomically
    and ``meta.json`` is written AFTER them, so a crash never leaves a meta without data.

    Refuses a card that differs from the one already stored under the key. Rows must carry exactly the card's
    horizons, and path probabilities iff the card's output type is ``paths``."""
    covariate_set = sorted(covariate_set)
    if compute_model_key(card, covariate_set) != model_key:
        raise ValueError(f"model_key {model_key} does not match model_key(card, covariate_set) = "
                         f"{compute_model_key(card, covariate_set)}")
    rows = list(rows)
    if not rows:
        raise ValueError("write_rows: no rows")
    with_paths = card.output_type == "paths"
    for r in rows:
        if r.horizons != card.horizons:
            raise ValueError(f"{r.symbol} {r.session}: row horizons {r.horizons} != card horizons {card.horizons}")
        if r.has_paths != with_paths:
            raise ValueError(f"{r.symbol} {r.session}: has_paths={r.has_paths} but card output_type={card.output_type}")
    kdir = _key_dir(_root(cache_root), model_key)
    kdir.mkdir(parents=True, exist_ok=True)
    meta_p = kdir / "meta.json"
    if meta_p.exists():
        existing = json.loads(meta_p.read_text(encoding="utf-8"))
        if existing["card"] != card.to_dict():
            raise ForecastStoreCardMismatch(f"model key {model_key} already holds a different model card")
    by_sym: Dict[str, List[ForecastRow]] = {}
    for r in rows:
        by_sym.setdefault(_symbol(r.symbol), []).append(r)
    cols = record_columns(card.horizons, with_paths)
    for sym, rs in by_sym.items():
        p = kdir / f"{sym}.parquet"
        new = pd.DataFrame([r.to_record() for r in rs], columns=cols)
        if p.exists():
            old = pd.read_parquet(p)
            old["session"] = pd.to_datetime(old["session"]).dt.date
            new["session"] = pd.to_datetime(new["session"]).dt.date
            new = pd.concat([old[~old["session"].isin(set(new["session"]))], new], ignore_index=True)
        new = new.sort_values("session").reset_index(drop=True)
        tmp = p.with_suffix(".parquet.tmp")
        new.to_parquet(tmp, index=False)
        os.replace(tmp, p)
    _atomic_write(meta_p, json.dumps({"model_key": model_key, "card": card.to_dict(), "context_length": card.context_length,
                                      "covariate_set": covariate_set, "horizons": list(card.horizons),
                                      "build": build_params},
                                     indent=2, sort_keys=True, default=str))
    reset_cache()


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
