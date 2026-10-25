"""Backfill the ML forecast store for one TimesFM 3.0 model card. Run it in the ``ba2-ml`` venv:

    C:\\Users\\basti\\ba2-venvs\\ml\\Scripts\\python.exe tools\\ml_forecast\\build_store.py \\
        --model-card timesfm3-ctx512 --symbols-file syms.txt --start 2024-01-02 --end 2024-03-29 \\
        --sessions all --threads 6 --batch-size 64 --resume

Data: ``<cache>/AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet`` (split-adjusted ``Close``). A forecast for session S
sees only bars with ``Date <= S`` (asserted). A (symbol, session) with fewer than ``context_length`` such bars, or
without a bar on S, is skipped and counted; the expert then sees a store miss (no signal), which is correct.
Single writer per store (see ``store.write_rows``). Rows are buffered and flushed every ``--flush-sessions`` sessions.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ba2_common.core.ml_forecast import store  # noqa: E402
from ba2_common.core.ml_forecast.forecast_row import ForecastRow  # noqa: E402
from ba2_common.core.ml_forecast.model_card import model_key  # noqa: E402
from tools.ml_forecast.adapters.timesfm3 import (CARD_IDS, TimesFM3Adapter, load_timesfm3_predict_fn,  # noqa: E402
                                                 timesfm3_card)

log = logging.getLogger("ml_forecast.build_store")
OHLCV_DIRNAME = "AlpacaHistoryOHLCVProvider"
MIN_FREE_RAM_GB = 12.0

SymbolData = Dict[str, Tuple[np.ndarray, np.ndarray]]   # symbol -> (dates datetime64[D] ascending, closes float64)


@dataclass
class BuildStats:
    forecasts: int = 0
    skipped_short_history: int = 0
    skipped_no_bar: int = 0
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


def load_daily_closes(cache_root: Path, symbols: Sequence[str]) -> Tuple[SymbolData, List[str]]:
    data: SymbolData = {}
    missing: List[str] = []
    for s in symbols:
        p = Path(cache_root) / OHLCV_DIRNAME / f"{s}_1d.parquet"
        if not p.exists():
            missing.append(s)
            continue
        df = pd.read_parquet(p, columns=["Date", "Close"])
        d = pd.to_datetime(df["Date"]).to_numpy().astype("datetime64[D]")
        c = df["Close"].to_numpy(dtype=np.float64)
        order = np.argsort(d, kind="stable")
        d, c = d[order], c[order]
        if len(np.unique(d)) != len(d):
            raise ValueError(f"{s}: duplicate dates in {p}")
        data[s] = (d, c)
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


def context_for(dates: np.ndarray, closes: np.ndarray, session: date, n: int):
    """The last ``n`` closes with Date <= session whose last bar IS the session; None if not available.
    Returns (None, reason) or (array, None). Never reads a bar after the session (asserted)."""
    s64 = np.datetime64(session, "D")
    idx = int(np.searchsorted(dates, s64, side="right"))      # bars [0, idx) have Date <= session
    if idx == 0 or dates[idx - 1] != s64:
        return None, "no_bar"
    if idx < n:
        return None, "short"
    ctx = closes[idx - n:idx]
    assert dates[idx - 1] == s64 and dates[idx - n:idx].max() <= s64, "point-in-time violation"
    return ctx, None


def existing_sessions(cache_root: Path, key: str, symbol: str) -> set:
    p = Path(cache_root) / store.STORE_DIRNAME / key / f"{symbol}.parquet"
    if not p.exists():
        return set()
    return set(pd.to_datetime(pd.read_parquet(p, columns=["session"])["session"]).dt.date)


def build(card, data: SymbolData, sessions: Sequence[date], adapter: TimesFM3Adapter, cache_root: Path,
          batch_size: int = 64, resume: bool = True, flush_sessions: int = 20,
          build_params: Optional[dict] = None, guard: Optional[Callable[[], None]] = None) -> BuildStats:
    key = model_key(card, adapter.covariate_set)
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

    for sess in sessions:
        items = []
        for sym, (d, c) in data.items():
            if sess in done[sym]:
                st.skipped_resume += 1
                continue
            ctx, why = context_for(d, c, sess, n)
            if ctx is None:
                if why == "short":
                    st.skipped_short_history += 1
                else:
                    st.skipped_no_bar += 1
                continue
            items.append((sym, sess, ctx))
        for i in range(0, len(items), batch_size):
            if guard:
                guard()
            chunk = items[i:i + batch_size]
            try:
                run(chunk)
            except ValueError as e:      # isolate the bad row(s); count and name them, never repair
                log.warning("batch failed (%s); retrying one by one", e)
                for it in chunk:
                    try:
                        run([it])
                    except ValueError as e1:
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
    flush()
    st.seconds = time.time() - t0
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
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--flush-sessions", type=int, default=20)
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
    card = timesfm3_card(CARD_IDS[a.model_card])
    log.warning("TimesFM 3.0 weights are non-commercial / non-production (licence %s)", card.licence)
    symbols = parse_symbols(Path(a.symbols_file).read_text(encoding="utf-8"))
    data, missing = load_daily_closes(cache_root, symbols)
    if missing:
        log.warning("%d symbols without a daily parquet, skipped: %s", len(missing), missing[:20])
    if not data:
        raise SystemExit(f"no daily parquet found for any symbol under {cache_root / OHLCV_DIRNAME}")
    sessions = session_calendar(data, a.start, a.end, a.sessions)
    _ram_guard()
    predict = load_timesfm3_predict_fn(a.threads, a.batch_size)
    adapter = TimesFM3Adapter(card, predict)
    key = model_key(card, adapter.covariate_set)
    log.info("model card %s -> model_key %s, store %s", a.model_card, key, cache_root / store.STORE_DIRNAME / key)
    params = {"tool": "tools/ml_forecast/build_store.py", "card_id": a.model_card, "start": a.start.isoformat(),
              "end": a.end.isoformat(), "sessions": a.sessions, "threads": a.threads, "batch_size": a.batch_size,
              "source": f"{OHLCV_DIRNAME}/<SYM>_1d.parquet Close"}
    st = build(card, data, sessions, adapter, cache_root, a.batch_size, a.resume, a.flush_sessions, params,
               guard=_ram_guard)
    log.info("DONE forecasts=%d sessions=%d skipped: short_history=%d no_bar=%d already_stored=%d invalid=%d "
             "| %.2f forecasts/s over %.0fs", st.forecasts, st.sessions, st.skipped_short_history,
             st.skipped_no_bar, st.skipped_resume, st.invalid_forecasts, st.rate, st.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
