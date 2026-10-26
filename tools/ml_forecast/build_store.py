"""Backfill the ML forecast store for one model card (TimesFM 3.0 or Kronos). Run it in the ``ba2-ml`` venv:

    C:\\Users\\basti\\ba2-venvs\\ml\\Scripts\\python.exe tools\\ml_forecast\\build_store.py \\
        --model-card timesfm3-ctx512 --symbols-file syms.txt --start 2024-01-02 --end 2024-03-29 \\
        --sessions all --threads 6 --batch-size 64 --resume

    ... --model-card kronos-small --device cuda --n-paths 64 --batch-size 16 --kronos-src C:\\path\\to\\Kronos

Data: ``<cache>/AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet`` (split-adjusted; ``Close`` for TimesFM, Open/High/Low/
Close/Volume for Kronos). A forecast for session S sees only bars with ``Date <= S`` (asserted). A (symbol, session)
with fewer than ``context_length`` such bars, or without a bar on S, is skipped and counted; the expert then sees a
store miss (no signal), which is correct.
Single writer per store (see ``store.write_rows``). Rows are buffered and flushed every ``--flush-sessions`` sessions.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ba2_common.core.ml_forecast import context as ctxmod, store  # noqa: E402
from ba2_common.core.ml_forecast.forecast_row import ForecastRow  # noqa: E402
from ba2_common.core.ml_forecast.model_card import model_key  # noqa: E402
from tools.ml_forecast.adapters import kronos as kronos_ad  # noqa: E402
from tools.ml_forecast.adapters.timesfm3 import (CARD_IDS as TIMESFM_CARD_IDS, InputDataError, LOG_CARD_IDS,  # noqa: E402
                                                 TimesFM3Adapter, card_for_id, load_timesfm3_predict_fn)

#: every CLI model card name the builder accepts -> adapter family
CARD_IDS = {**{k: "timesfm" for k in (*TIMESFM_CARD_IDS, *LOG_CARD_IDS)},
            **{k: "kronos" for k in kronos_ad.CARD_IDS}}

log = logging.getLogger("ml_forecast.build_store")
OHLCV_DIRNAME = "AlpacaHistoryOHLCVProvider"
MIN_FREE_RAM_GB = 12.0

# symbol -> (dates datetime64[D] ascending, closes (N,) or OHLCV (N, 5) float64: the first axis is cut either way)
SymbolData = Dict[str, Tuple[np.ndarray, np.ndarray]]


@dataclass
class BuildStats:
    forecasts: int = 0
    skipped_short_history: int = 0
    skipped_no_bar: int = 0
    skipped_gap: int = 0
    skipped_resume: int = 0
    invalid_forecasts: int = 0
    sessions: int = 0
    seconds: float = 0.0
    invalid: List[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.forecasts / self.seconds if self.seconds > 0 else 0.0


def parse_symbols(text: str) -> List[str]:
    out: List[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for tok in line.replace(",", " ").split():
            t = tok.strip().upper()
            if t and t not in out:
                out.append(t)
    if not out:
        raise ValueError("symbols file holds no symbols")
    return out


def load_daily_closes(cache_root: Path, symbols: Sequence[str], source: str = "alpaca_build"
                      ) -> Tuple[SymbolData, List[str]]:
    data: SymbolData = {}
    missing: List[str] = []
    for s in symbols:
        if not (Path(cache_root) / ctxmod.SOURCE_DIRS[source] / f"{s}_1d.parquet").exists():
            missing.append(s)
            continue
        data[s] = ctxmod.load_daily_series(cache_root, s, source)
    return data, missing


def load_daily_ohlcv(cache_root: Path, symbols: Sequence[str], source: str = "alpaca_build"
                     ) -> Tuple[SymbolData, List[str]]:
    """Like ``load_daily_closes`` with the (N, 5) Open/High/Low/Close/Volume array (Kronos)."""
    data: SymbolData = {}
    missing: List[str] = []
    for s in symbols:
        if not (Path(cache_root) / ctxmod.SOURCE_DIRS[source] / f"{s}_1d.parquet").exists():
            missing.append(s)
            continue
        data[s] = ctxmod.load_daily_ohlcv_series(cache_root, s, source)
    return data, missing


def session_calendar(data: SymbolData, start: date, end: date, mode: str) -> List[date]:
    """Union of the symbols' bar dates in [start, end]. ``weekly`` keeps the last union date of each ISO week."""
    if mode not in ("all", "weekly"):
        raise ValueError(f"--sessions must be all|weekly, got {mode!r}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    union = np.unique(np.concatenate([d for d, _ in data.values()]))
    union = union[(union >= np.datetime64(start, "D")) & (union <= np.datetime64(end, "D"))]
    days = [x.astype(object) for x in union]
    if mode == "all":
        return days
    last: Dict[Tuple[int, int], date] = {}
    for d in days:
        iso = d.isocalendar()
        last[(iso[0], iso[1])] = d
    return sorted(last.values())


context_for = ctxmod.context_for      # the ONE shared slicing function (live uses it too)


def item_context(d: np.ndarray, c: np.ndarray, sess: date, n: int):
    """``(context, None)`` or ``(None, reason)``. Closes (1-D) give the array ``context_for`` returns; OHLCV (2-D)
    a DataFrame indexed by the bar dates (columns ``OHLCV_COLUMNS``), cut by the same point-in-time function."""
    if c.ndim == 1:
        return context_for(d, c, sess, n)
    wd, win, why = ctxmod.ohlcv_context_for(d, c, sess, n)
    if win is None:
        return None, why
    return pd.DataFrame(win, index=pd.DatetimeIndex(wd), columns=list(ctxmod.OHLCV_COLUMNS)), None


def existing_sessions(cache_root: Path, key: str, symbol: str) -> set:
    p = Path(cache_root) / store.STORE_DIRNAME / key / f"{symbol}.parquet"
    if not p.exists():
        return set()
    return set(pd.to_datetime(pd.read_parquet(p, columns=["session"])["session"]).dt.date)


class StoreLocked(RuntimeError):
    pass


@contextmanager
def store_lock(cache_root: Path, key: str):
    """Single-writer rule (``store.write_rows``): ``<key>/.lock``, created with O_EXCL for the build's duration.
    A stale lock (crashed build) is NOT stolen: the message names its owner and the operator deletes it."""
    kdir = Path(cache_root) / store.STORE_DIRNAME / key
    kdir.mkdir(parents=True, exist_ok=True)
    lock = kdir / ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            owner = lock.read_text(encoding="utf-8").strip()
        except OSError:
            owner = "unreadable"
        raise StoreLocked(f"{lock} exists (held by {owner}). Another build writes this store, or it crashed: "
                          f"if no build is running, delete the file and re-run with --resume") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid {os.getpid()} host {socket.gethostname()} since {time.strftime('%Y-%m-%d %H:%M:%S')}")
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def check_existing_store(card, covariate_set, cache_root: Path, key: str) -> None:
    """Refuse at once when a store under this key holds another card / covariate set (not after the first flush)."""
    try:
        meta = store.read_meta(key, cache_root)
    except store.ForecastStoreMiss:
        return                                    # a new store
    if meta["card"] != card.to_dict() or sorted(meta["covariate_set"]) != sorted(covariate_set):
        raise store.ForecastStoreCardMismatch(f"store {key} holds a different model card or covariate set")


def make_build_params(params: dict, cache_root: Path) -> dict:
    """Build params recorded in meta.json: the run's arguments plus the history provenance (source name, build
    manifest sha256, context-function version) that the live expert must match."""
    return {**params, **ctxmod.history_provenance(cache_root)}


def exit_code(st: "BuildStats", max_invalid_share: float) -> int:
    total = st.forecasts + st.invalid_forecasts
    return 1 if st.invalid_forecasts and st.invalid_forecasts / total > max_invalid_share else 0


def build(card, data: SymbolData, sessions: Sequence[date], adapter, cache_root: Path,
          batch_size: int = 64, resume: bool = True, flush_sessions: int = 20,
          build_params: Optional[dict] = None, guard: Optional[Callable[[], None]] = None) -> BuildStats:
    key = model_key(card, adapter.covariate_set)
    with store_lock(cache_root, key):
        check_existing_store(card, adapter.covariate_set, cache_root, key)
        return _build_locked(card, key, data, sessions, adapter, cache_root, batch_size, resume, flush_sessions,
                             build_params, guard)


def _build_locked(card, key, data, sessions, adapter, cache_root, batch_size, resume, flush_sessions,
                  build_params, guard) -> BuildStats:
    n = card.context_length
    done = {s: (existing_sessions(cache_root, key, s) if resume else set()) for s in data}
    st = BuildStats()
    # plan: eligible (symbol, session) pairs, for the ETA
    plan = 0
    for sess in sessions:
        for sym, (d, c) in data.items():
            if sess in done[sym]:
                continue
            if context_for(d, c, sess, n)[0] is not None:
                plan += 1
    log.info("plan: %d forecasts to compute over %d sessions (%d symbols)", plan, len(sessions), len(data))
    buf: List[ForecastRow] = []
    t0 = time.time()

    def flush():
        nonlocal buf
        if buf:
            store.write_rows(key, card, dict(build_params or {}), buf, adapter.covariate_set, cache_root)
            buf = []

    def run(chunk):
        rows = adapter.forecast_batch(chunk)
        buf.extend(rows)
        st.forecasts += len(rows)

    try:
        for sess in sessions:
            items = []
            for sym, (d, c) in data.items():
                if sess in done[sym]:
                    st.skipped_resume += 1
                    continue
                ctx, why = item_context(d, c, sess, n)
                if ctx is None:
                    attr = {"short": "skipped_short_history", "gap": "skipped_gap", "no_bar": "skipped_no_bar"}[why]
                    setattr(st, attr, getattr(st, attr) + 1)
                    continue
                items.append((sym, sess, ctx))
            for i in range(0, len(items), batch_size):
                if guard:
                    guard()
                chunk = items[i:i + batch_size]
                try:
                    run(chunk)
                except InputDataError as e:      # isolate the bad row(s); count and name them, never repair
                    log.warning("batch failed (%s); retrying one by one", e)
                    for it in chunk:
                        try:
                            run([it])
                        except InputDataError as e1:
                            st.invalid_forecasts += 1
                            st.invalid.append(f"{it[0]} {it[1]}: {e1}")
                            log.warning("invalid forecast skipped: %s %s: %s", it[0], it[1], e1)
            st.sessions += 1
            st.seconds = time.time() - t0
            if items:
                rate = st.forecasts / st.seconds if st.seconds else 0.0
                left = max(plan - st.forecasts - st.invalid_forecasts, 0)
                log.info("session %s (%d/%d): %d forecasts, %.2f forecasts/s, ETA %.1f min", sess, st.sessions,
                         len(sessions), st.forecasts, rate, (left / rate / 60) if rate else float("nan"))
            if st.sessions % flush_sessions == 0:
                flush()
    finally:
        st.seconds = time.time() - t0
        flush()
    return st


def _ram_guard() -> None:
    import psutil
    free = psutil.virtual_memory().available / 1e9
    if free < MIN_FREE_RAM_GB:
        raise MemoryError(f"free RAM {free:.1f} GB < {MIN_FREE_RAM_GB} GB: stopping (a GA master shares this PC); "
                          f"re-run with --resume")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-card", required=True, choices=sorted(CARD_IDS))
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--sessions", choices=("all", "weekly"), default="all")
    ap.add_argument("--cache-root", default=None, help="default: the BA2 cache dir (ba2_common.config.CACHE_FOLDER)")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=None,
                    help="contexts per model call (default 64 for TimesFM, 16 for Kronos: x n-paths rows per forward)")
    ap.add_argument("--device", default=None, help="torch device (default cpu for TimesFM, cuda for Kronos)")
    ap.add_argument("--n-paths", type=int, default=kronos_ad.DEFAULT_N_PATHS,
                    help="Kronos only: sampled paths per forecast; part of the model key")
    ap.add_argument("--kronos-src", default=None,
                    help=f"Kronos only: a clone of github.com/shiyu-coder/Kronos (default: ${kronos_ad.KRONOS_ENV})")
    ap.add_argument("--flush-sessions", type=int, default=20)
    ap.add_argument("--max-invalid-share", type=float, default=0.0,
                    help="exit non-zero when invalid (unusable-input) forecasts exceed this share (default 0 = any)")
    ap.add_argument("--resume", action="store_true", help="skip (symbol, session) pairs already in the store")
    a = ap.parse_args(argv)
    if not 1 <= a.threads <= 6:
        ap.error("--threads must be 1..6 (a live GA master shares this PC)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cache_root is None:
        from ba2_common import config
        cache_root = Path(config.CACHE_FOLDER)
    else:
        cache_root = Path(a.cache_root)
    family = CARD_IDS[a.model_card]
    if family == "kronos":
        card = kronos_ad.kronos_card(kronos_ad.CARD_IDS[a.model_card], a.n_paths)
        batch_size = a.batch_size if a.batch_size is not None else 16
        device = a.device if a.device is not None else "cuda"
        log.warning("Kronos has no published pretraining cutoff; 2020-2024 is almost certainly in its training "
                    "data (paper mid-2025): only later dates are clean evidence")
    else:
        card = card_for_id(a.model_card)       # price OR log space: never build a log id as a price card
        batch_size = a.batch_size if a.batch_size is not None else 64
        device = a.device if a.device is not None else "cpu"
        log.warning("TimesFM 3.0 weights are non-commercial / non-production (licence %s)", card.licence)
    symbols = parse_symbols(Path(a.symbols_file).read_text(encoding="utf-8"))
    data, missing = (load_daily_ohlcv if family == "kronos" else load_daily_closes)(cache_root, symbols)
    if missing:
        log.warning("%d symbols without a daily parquet, skipped: %s", len(missing), missing[:20])
    if not data:
        raise SystemExit(f"no daily parquet found for any symbol under {cache_root / OHLCV_DIRNAME}")
    sessions = session_calendar(data, a.start, a.end, a.sessions)
    _ram_guard()
    provenance: dict = {}
    if family == "kronos":
        sample_fn = kronos_ad.load_kronos_sample_fn(kronos_ad.CARD_IDS[a.model_card], device, a.kronos_src,
                                                    max_rows=batch_size * a.n_paths)
        provenance = {"kronos": sample_fn.provenance}
        adapter = kronos_ad.KronosAdapter(card, sample_fn, a.n_paths)
    else:
        adapter = TimesFM3Adapter(card, load_timesfm3_predict_fn(a.threads, batch_size, device))
    key = model_key(card, adapter.covariate_set)
    log.info("model card %s -> model_key %s, store %s", a.model_card, key, cache_root / store.STORE_DIRNAME / key)
    params = {"tool": "tools/ml_forecast/build_store.py", "card_id": a.model_card, "start": a.start.isoformat(),
              "end": a.end.isoformat(), "sessions": a.sessions, "threads": a.threads, "batch_size": batch_size,
              "device": device, **provenance,
              "source": f"{OHLCV_DIRNAME}/<SYM>_1d.parquet " + ("OHLCV" if family == "kronos" else "Close")}
    if family == "kronos":
        params["n_paths"] = a.n_paths
    params = make_build_params(params, cache_root)
    st = build(card, data, sessions, adapter, cache_root, batch_size, a.resume, a.flush_sessions, params,
               guard=_ram_guard)
    log.info("DONE forecasts=%d sessions=%d skipped: short_history=%d gap=%d no_bar=%d already_stored=%d invalid=%d "
             "| %.2f forecasts/s over %.0fs", st.forecasts, st.sessions, st.skipped_short_history, st.skipped_gap,
             st.skipped_no_bar, st.skipped_resume, st.invalid_forecasts, st.rate, st.seconds)
    rejected = getattr(adapter, "reject_log", [])
    if rejected:
        log.warning("Kronos dropped %d path(s) with a non-positive price in %d forecast(s): %s",
                    adapter.rejected_paths, len(rejected), "; ".join(rejected[:50]))
    if st.invalid:
        log.error("INVALID forecasts (%d): %s", st.invalid_forecasts, "; ".join(st.invalid))
    return exit_code(st, a.max_invalid_share)


if __name__ == "__main__":
    raise SystemExit(main())
