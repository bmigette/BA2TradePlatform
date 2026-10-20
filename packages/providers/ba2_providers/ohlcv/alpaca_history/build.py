"""Build the Alpaca history OHLCV cache (``<SYM>_1d.parquet`` / ``<SYM>_5min.parquet``) from the RAW stores and the split table.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Run it through the pipeline CLI, step ``build``:

    python tools/prepare_history.py --source alpaca --steps build

INPUTS (read-only)
  * the DAILY store (``--store``, default ~/Documents/ba2/alpaca_store_daily2016): ``bars/1Day/symbol=<S>/all.parquet`` (t UTC
    bar start, o h l c v n vw), ``manifest/symbol=<S>.json``, ``STORE_INFO.json``.  Every daily ``t`` must be 00:00 New York
    time (04:00Z / 05:00Z): the session is its date; anything else STOPS the symbol.
  * the 5-MINUTE store (``--store-5min``, default ~/Documents/ba2/alpaca_store): ``bars/5Min/symbol=<S>/<year>.parquet`` (t UTC bar
    START, including extended hours) and a manifest whose 5Min chunk entries carry ``days_without_intraday_bars
    {count, of, days}``.  Both stores must have the same asof (the same ticker mapping) as the split table.
  * ``<out>/_meta/split_table.json`` (the ``split-table`` step): ``splits`` / ``spinoffs`` / ``breaks`` by symbol, a ``run_id``.
    Every CSV beside it carries the same run_id; a mismatch REFUSES the build (``BuildError``): the CSVs and the table must
    come from one run.

SYMBOLS.  Every symbol that has daily bars in the daily store, plus every symbol that has 5-minute bars (an intraday symbol
without a daily bar is a build error: nothing to anchor its sessions).  A file is written only for a symbol with bars.
FILE NAMES.  ``<SYMBOL>_1d.parquet`` / ``<SYMBOL>_5min.parquet`` with the SYMBOL spelled like the store's folders and the FMP
cache: share classes use '-' (``BRK-B``), because the cache readers (``ba2_common.core.native_cache.timeseries_path``) use
``symbol.upper()`` verbatim and the platform's tickers are the FMP spelling.

SEMANTICS (the plan's "Semantics fixed", the split_table docstring wins on a conflict)
  * ADJUSTMENT.  F(t) = product of the split factors f with event date > t (a bar ON the event date is already on the new basis);
    prices (Open, High, Low, Close) are divided by the split factor AND the spin-off factors with date > t; VOLUME is multiplied
    by the SPLIT part only (a spin-off leaves the share count unchanged).  Back-adjusted to the store's asof basis.  Cash
    dividends are not adjusted (like FMP's ``close``).
  * SERIES BREAKS.  Only bars on/after the symbol's LAST break date are kept (1d and 5min); the dropped count is recorded.
  * DAILY.  ``Date`` = New York naive midnight of the session; Volume int64 (rounded after adjustment).
  * 5-MINUTE.  UTC bar starts converted to New York naive; only the regular session is kept: bar starts 09:30 .. 15:55 (a half
    day ends earlier: what exists is kept).  A session without 5-minute bars stays EMPTY (never filled from the daily bar);
    the store's ``days_without_intraday_bars`` per year are carried into the manifest.  Volume float64 like the FMP files.
  * COLUMNS exactly like the FMP files: ``Date, Open, High, Low, Close, Volume, effective_date`` (``effective_date = Date``).

OUTPUT.  ``<out>/<SYM>_1d.parquet``, ``<out>/<SYM>_5min.parquet`` (atomic: temp file + os.replace) and
``<out>/_meta/build_manifest.json`` (the only thing this step writes under ``_meta``): the stores' asof, the split table's
run_id / sha256 / inputs_sha256 / overrides_sha256, the input chunk hashes, per symbol the first/last date and bar count of
each interval, the bars dropped by a break, the carried ``days_without_intraday_bars`` per year and the events applied.
A symbol rebuilt without 5-minute bars has its old 5-minute file removed (the manifest lists it).  Files of symbols outside
this build are never touched; the manifest lists the ``*_1d/_5min.parquet`` files it does not describe (``unlisted_files``).
A SUBSET run (``--symbols``) merges its symbols into an existing manifest of the same split table; otherwise it replaces it.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ba2_common import config as _cfg
from ba2_providers.ohlcv.alpaca_history.split_build import DEFAULT_OUT, atomic_write

DEFAULT_DAILY_STORE = os.path.join(_cfg.BA2_HOME, "alpaca_store_daily2016")
DEFAULT_INTRADAY_STORE = os.path.join(_cfg.BA2_HOME, "alpaca_store")
NY = "America/New_York"
RTH_FIRST_MIN = 9 * 60 + 30              # first regular bar start 09:30
RTH_LAST_MIN = 15 * 60 + 55              # last regular bar start 15:55
COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume", "effective_date"]
PRICE_COLUMNS = ["Open", "High", "Low", "Close"]
RENAME = {"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}
MANIFEST_NAME = "build_manifest.json"
SPLIT_TABLE_NAME = "split_table.json"
RUN_ID_CSVS = ("split_review.csv", "split_accepted.csv", "split_vs_fmp.csv", "fmp_daily_inputs.csv", "yahoo_inputs.csv")


class BuildError(RuntimeError):
    """An input is missing, inconsistent or has changed shape: the build stops (the pipeline step fails)."""


# ---------------------------------------------------------------------------
# split table
# ---------------------------------------------------------------------------
class SplitTable:
    def __init__(self, raw: Mapping[str, Any], sha256: str):
        self.run_id: str = raw["run_id"]
        self.asof: str = raw["asof"]
        self.sha256 = sha256
        self.inputs_sha256: str = raw["inputs_sha256"]
        self.overrides_sha256: str = raw["overrides_sha256"]
        self.splits: Dict[str, List[Tuple[str, float]]] = {s: [(d, float(f)) for d, f in v] for s, v in raw["splits"].items()}
        self.spinoffs: Dict[str, List[Tuple[str, float]]] = {s: [(d, float(f)) for d, f in v] for s, v in raw["spinoffs"].items()}
        self.breaks: Dict[str, List[str]] = {s: list(v) for s, v in raw["breaks"].items()}

    def break_date(self, symbol: str) -> Optional[str]:
        return max(self.breaks[symbol]) if symbol in self.breaks else None

    def events(self, symbol: str) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        return (self.splits[symbol] if symbol in self.splits else [],
                self.spinoffs[symbol] if symbol in self.spinoffs else [])


def load_split_table(out: str) -> SplitTable:
    """Read ``<out>/_meta/split_table.json`` and REFUSE when any CSV of the split-table run carries another run_id."""
    meta = os.path.join(out, "_meta")
    path = os.path.join(meta, SPLIT_TABLE_NAME)
    if not os.path.isfile(path):
        raise BuildError(f"{path} not found: run the split-table step first")
    with open(path, "rb") as f:
        data = f.read()
    raw = json.loads(data)
    table = SplitTable(raw, hashlib.sha256(data).hexdigest())
    bad: List[str] = []
    for name in RUN_ID_CSVS:
        p = os.path.join(meta, name)
        if not os.path.isfile(p):
            raise BuildError(f"{p} not found: the split-table run is incomplete")
        with open(p, newline="", encoding="utf-8") as f:
            rows = csv.DictReader(f)
            if rows.fieldnames is None or "run_id" not in rows.fieldnames:
                raise BuildError(f"{p} has no run_id column")
            ids = {r["run_id"] for r in rows}
        if ids and ids != {table.run_id}:
            bad.append(f"{name}: {sorted(ids)}")
    if bad:
        raise BuildError(f"split table run_id {table.run_id} does not match its CSVs ({'; '.join(bad)}): rerun the "
                         f"split-table step so the table and the CSVs come from one run")
    return table


# ---------------------------------------------------------------------------
# pure transforms
# ---------------------------------------------------------------------------
def cumulative_factor(days: pd.Series, events: Sequence[Tuple[str, float]]) -> np.ndarray:
    """F(d) = product of f over events with event date > d, for each (normalized, tz-naive) day in ``days``."""
    if not events:
        return np.ones(len(days))
    ev = sorted(events)
    ev_days = np.array([np.datetime64(d, "D") for d, _ in ev])
    fac = np.array([f for _, f in ev], dtype="float64")
    suffix = np.ones(len(ev) + 1)
    suffix[:-1] = np.cumprod(fac[::-1])[::-1]
    idx = np.searchsorted(ev_days, days.values.astype("datetime64[D]"), side="right")
    return suffix[idx]


def adjust(frame: pd.DataFrame, splits: Sequence[Tuple[str, float]], spinoffs: Sequence[Tuple[str, float]] = ()) -> pd.DataFrame:
    """Back-adjust a frame with a naive ``Date`` and Open/High/Low/Close/Volume.  Prices / (split F x spin-off F); volume x
    split F only.  Returns a new frame; a bar on an event date is not adjusted by that event."""
    out = frame.copy()
    day = out["Date"].dt.normalize()
    f_split = cumulative_factor(day, splits)
    f_spin = cumulative_factor(day, spinoffs)
    div = f_split * f_spin
    for c in PRICE_COLUMNS:
        out[c] = out[c] / div
    out["Volume"] = out["Volume"] * f_split
    return out


def _check_columns(df: pd.DataFrame, what: str) -> None:
    missing = [c for c in ("t", "o", "h", "l", "c", "v") if c not in df.columns]
    if missing:
        raise BuildError(f"{what}: columns {missing} missing (have {list(df.columns)})")


def _to_native(df: pd.DataFrame, what: str) -> pd.DataFrame:
    _check_columns(df, what)
    t = df["t"]
    if t.dt.tz is None:
        raise BuildError(f"{what}: t is not timezone-aware UTC")
    out = df[["t", "o", "h", "l", "c", "v"]].rename(columns=RENAME)
    out["Date"] = t.dt.tz_convert(NY).dt.tz_localize(None).astype("datetime64[ns]")
    out = out.drop(columns=["t"])
    if out[PRICE_COLUMNS].isna().any().any():
        raise BuildError(f"{what}: NaN prices")
    return out


def to_native_daily(store_daily: pd.DataFrame, what: str = "daily") -> pd.DataFrame:
    """Store daily bars -> native frame.  Every ``t`` must be exactly 00:00 New York; each session once."""
    out = _to_native(store_daily, what)
    d = out["Date"]
    off = d[(d.dt.hour != 0) | (d.dt.minute != 0) | (d.dt.second != 0)]
    if len(off):
        raise BuildError(f"{what}: {len(off)} daily bar(s) not at 00:00 New York (first {off.iloc[0]}); the store's t is not "
                         f"the session start")
    if d.duplicated().any():
        raise BuildError(f"{what}: duplicate session {d[d.duplicated()].iloc[0].date()}")
    out = out.sort_values("Date", kind="stable").reset_index(drop=True)
    return out[["Date", "Open", "High", "Low", "Close", "Volume"]]


def to_native_5min(store_5min: pd.DataFrame, what: str = "5min") -> pd.DataFrame:
    """Store 5-minute bars (UTC starts, extended hours) -> New York naive, regular session only (09:30 .. 15:55 starts)."""
    out = _to_native(store_5min, what)
    d = out["Date"]
    minute = d.dt.hour * 60 + d.dt.minute
    out = out[(minute >= RTH_FIRST_MIN) & (minute <= RTH_LAST_MIN)]
    if out["Date"].duplicated().any():
        raise BuildError(f"{what}: duplicate bar start {out['Date'][out['Date'].duplicated()].iloc[0]}")
    out = out.sort_values("Date", kind="stable").reset_index(drop=True)
    return out[["Date", "Open", "High", "Low", "Close", "Volume"]]


def apply_break(frame: pd.DataFrame, break_date: Optional[str]) -> Tuple[pd.DataFrame, int]:
    """Keep the bars whose session is on/after ``break_date``; returns (kept, number dropped)."""
    if break_date is None or frame.empty:
        return frame, 0
    keep = frame["Date"].dt.normalize() >= pd.Timestamp(break_date)
    return frame[keep.values].reset_index(drop=True), int((~keep).sum())


def finish(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """The FMP file layout: Date, Open..Close float64, Volume (1d int64, 5min float64), effective_date = Date."""
    out = frame.copy()
    for c in PRICE_COLUMNS:
        out[c] = out[c].astype("float64")
    if interval == "1d":
        out["Volume"] = np.rint(out["Volume"]).astype("int64")
    else:
        out["Volume"] = out["Volume"].astype("float64")
    out["effective_date"] = out["Date"]
    return out[COLUMNS].reset_index(drop=True)


def parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# one symbol
# ---------------------------------------------------------------------------
def _read_manifest(store: str, symbol: str) -> Optional[Dict[str, Any]]:
    p = os.path.join(store, "manifest", f"symbol={symbol}.json")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


EMPTY_STATUSES = ("no_data", "no_daily_days")                 # the store holds no file for the chunk (nothing to fetch)
REFUSED_PREFIX = "refused_"                                  # the downloader refused the year (too many gap days): no file


def _chunk_digest(manifest: Mapping[str, Any], prefix: str, what: str) -> Tuple[List[str], Dict[str, Any], Dict[str, str]]:
    """(sorted ``<chunk>:<status>:<sha256>`` lines, the COMPLETE chunks, {chunk: status} of the empty / refused ones).
    ``no_data`` / ``no_daily_days`` chunks hold no file; ``refused_*`` chunks (a year with too many gap days) hold none either and
    are RECORDED in the manifest, not hidden.  Any other status STOPS the symbol."""
    lines, chunks, absent = [], {}, {}
    for key in sorted(manifest["chunks"]):
        if not key.startswith(prefix):
            continue
        c = manifest["chunks"][key]
        st = c["status"]
        if st == "complete":
            lines.append(f"{key}:{st}:{c['sha256']}")
            chunks[key] = c
        elif st in EMPTY_STATUSES or st.startswith(REFUSED_PREFIX):
            lines.append(f"{key}:{st}:")
            absent[key] = st
        else:
            raise BuildError(f"{what}: chunk {key} has status {st!r}")
    return lines, chunks, absent


def file_names(symbol: str) -> Tuple[str, str]:
    """Cache file names: the reader rule is ``symbol.upper()`` verbatim (native_cache.timeseries_path); the store/FMP spelling
    of share classes is ``BRK-B``."""
    s = symbol.upper()
    return f"{s}_1d.parquet", f"{s}_5min.parquet"


def build_symbol(symbol: str, *, daily_store: str, intraday_store: str, out: str, table: SplitTable) -> Dict[str, Any]:
    """Build both files of one symbol; returns its manifest entry.  One symbol at a time: memory is one symbol's bars."""
    t0 = time.time()
    entry: Dict[str, Any] = {"symbol": symbol}
    splits, spinoffs = table.events(symbol)
    brk = table.break_date(symbol)
    d_manifest = _read_manifest(daily_store, symbol)
    d_path = os.path.join(daily_store, "bars", "1Day", f"symbol={symbol}", "all.parquet")
    has_daily = os.path.isfile(d_path)
    i_dir = os.path.join(intraday_store, "bars", "5Min", f"symbol={symbol}")
    year_files = sorted(f for f in os.listdir(i_dir) if f.endswith(".parquet")) if os.path.isdir(i_dir) else []
    if not has_daily:
        if year_files:
            raise BuildError(f"{symbol}: 5-minute bars but no daily bars in {daily_store}")
        entry["skipped"] = "no bars"
        return entry
    if d_manifest is None:
        raise BuildError(f"{symbol}: daily bars without manifest/symbol={symbol}.json")
    digest, d_chunks, _ = _chunk_digest(d_manifest, "1Day", f"{symbol} daily")
    if not d_chunks:
        raise BuildError(f"{symbol}: a daily file exists but its manifest has no complete 1Day chunk")
    daily = to_native_daily(pd.read_parquet(d_path), f"{symbol} daily")
    daily, dropped_1d = apply_break(daily, brk)

    gap_days: Dict[str, Any] = {}
    refused: Dict[str, str] = {}
    intr = None
    dropped_5m = 0
    if year_files:
        i_manifest = _read_manifest(intraday_store, symbol)
        if i_manifest is None:
            raise BuildError(f"{symbol}: 5-minute bars without manifest/symbol={symbol}.json in {intraday_store}")
        i_digest, chunks, absent = _chunk_digest(i_manifest, "5Min", f"{symbol} 5min")
        digest += i_digest
        refused = {k.split("/")[1]: st for k, st in absent.items() if st.startswith(REFUSED_PREFIX)}
        listed = {k.split("/")[1] + ".parquet" for k in chunks if chunks[k]["bars"] > 0}
        if listed != set(year_files):
            raise BuildError(f"{symbol}: 5Min files {sorted(year_files)} differ from the manifest's {sorted(listed)}")
        frames = [to_native_5min(pd.read_parquet(os.path.join(i_dir, f)), f"{symbol} {f}") for f in year_files]
        intr = pd.concat(frames, ignore_index=True)
        if intr["Date"].duplicated().any():
            raise BuildError(f"{symbol}: a 5-minute bar start appears in two year files")
        intr = intr.sort_values("Date", kind="stable").reset_index(drop=True)
        intr, dropped_5m = apply_break(intr, brk)
        for k, c in chunks.items():
            if "days_without_intraday_bars" not in c:
                continue
            g = c["days_without_intraday_bars"]
            days = [d for d in g["days"] if brk is None or d >= brk]
            if days or g["count"] == 0:
                gap_days[k.split("/")[1]] = {"count": len(days), "of": g["of"], "days": days}
    entry["inputs_sha256"] = hashlib.sha256("\n".join(digest).encode()).hexdigest()
    entry["break_date"] = brk
    entry["dropped_by_break"] = {"1d": dropped_1d, "5min": dropped_5m}
    entry["days_without_intraday_bars"] = gap_days
    entry["refused_5min_years"] = refused

    first_day = daily["Date"].iloc[0].normalize() if len(daily) else None
    ev_s = [[d, f] for d, f in splits if first_day is not None and pd.Timestamp(d) > first_day]
    ev_o = [[d, f] for d, f in spinoffs if first_day is not None and pd.Timestamp(d) > first_day]
    entry["events"] = {"splits": ev_s, "spinoffs": ev_o}
    f1, f5 = file_names(symbol)
    entry["files"] = {}
    if len(daily):
        d_out = finish(adjust(daily, splits, spinoffs), "1d")
        atomic_write(os.path.join(out, f1), parquet_bytes(d_out))
        entry["1d"] = {"first": str(d_out["Date"].iloc[0].date()), "last": str(d_out["Date"].iloc[-1].date()), "bars": len(d_out)}
        entry["files"]["1d"] = f1
    elif os.path.isfile(os.path.join(out, f1)):
        os.remove(os.path.join(out, f1))
        entry["removed_stale"] = entry.get("removed_stale", []) + [f1]
    if intr is not None and len(intr):
        i_out = finish(adjust(intr, splits, spinoffs), "5min")
        atomic_write(os.path.join(out, f5), parquet_bytes(i_out))
        entry["5min"] = {"first": str(i_out["Date"].iloc[0]), "last": str(i_out["Date"].iloc[-1]), "bars": len(i_out)}
        entry["files"]["5min"] = f5
    elif os.path.isfile(os.path.join(out, f5)):
        os.remove(os.path.join(out, f5))
        entry["removed_stale"] = entry.get("removed_stale", []) + [f5]
    entry["elapsed_s"] = round(time.time() - t0, 3)
    return entry


def _task(args: Tuple[str, str, str, str, "SplitTable"]) -> Dict[str, Any]:
    symbol, daily_store, intraday_store, out, table = args
    try:
        return build_symbol(symbol, daily_store=daily_store, intraday_store=intraday_store, out=out, table=table)
    except Exception as e:                       # reported per symbol; the run finishes and then fails, named
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# whole build
# ---------------------------------------------------------------------------
def _store_asof(store: str) -> str:
    p = os.path.join(store, "STORE_INFO.json")
    if not os.path.isfile(p):
        raise BuildError(f"{p} not found")
    with open(p, encoding="utf-8") as f:
        return json.load(f)["asof"]


def _file_sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def list_symbols(daily_store: str, intraday_store: str) -> List[str]:
    syms = set()
    for store, kind in ((daily_store, "1Day"), (intraday_store, "5Min")):
        d = os.path.join(store, "bars", kind)
        if not os.path.isdir(d):
            raise BuildError(f"{d} not found")
        syms.update(n.split("=", 1)[1] for n in os.listdir(d) if n.startswith("symbol="))
    dots = sorted(s for s in syms if "." in s)
    if dots:
        raise BuildError(f"store folder names with '.': {dots[:5]} (the stores spell share classes with '-')")
    return sorted(syms)


def build_cache(*, daily_store: str = DEFAULT_DAILY_STORE, intraday_store: str = DEFAULT_INTRADAY_STORE,
                out: str = DEFAULT_OUT, symbols: Optional[Sequence[str]] = None, workers: int = 4,
                log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Build the cache for every store symbol (or ``symbols``) and write the manifest.  Raises ``BuildError`` on a bad input and
    when any symbol failed (after the others finished and the manifest was written)."""
    t0 = time.time()
    table = load_split_table(out)
    d_asof, i_asof = _store_asof(daily_store), _store_asof(intraday_store)
    if not (d_asof == i_asof == table.asof):
        raise BuildError(f"asof differs: daily store {d_asof}, 5-minute store {i_asof}, split table {table.asof}")
    universe = list_symbols(daily_store, intraday_store)
    if symbols is not None:
        want = [s.strip().upper().replace(".", "-") for s in symbols]
        unknown = sorted(set(want) - set(universe))
        if unknown:
            raise BuildError(f"symbols without bars in either store: {unknown}")
        todo = sorted(set(want))
    else:
        todo = universe
    os.makedirs(os.path.join(out, "_meta"), exist_ok=True)
    log(f"build: {len(todo)} symbols, {workers} workers, split table {table.run_id}")
    results: List[Dict[str, Any]] = []
    jobs = [(s, daily_store, intraday_store, out, table) for s in todo]
    if workers <= 1:
        for i, j in enumerate(jobs, 1):
            results.append(_task(j))
            if i % 250 == 0:
                log(f"build: {i}/{len(jobs)}")
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, r in enumerate(ex.map(_task, jobs, chunksize=8), 1):
                results.append(r)
                if i % 250 == 0:
                    log(f"build: {i}/{len(jobs)} ({time.time() - t0:.0f}s)")
    failed = {r["symbol"]: r["error"] for r in results if "error" in r}
    skipped = [r["symbol"] for r in results if "skipped" in r]
    built = {r["symbol"]: {k: v for k, v in r.items() if k != "symbol"} for r in results if "error" not in r and "skipped" not in r}

    mpath = os.path.join(out, "_meta", MANIFEST_NAME)
    entries: Dict[str, Any] = {}
    if symbols is not None and os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as f:
            old = json.load(f)
        if old["split_table"]["run_id"] == table.run_id and old["split_table"]["sha256"] == table.sha256:
            entries = dict(old["symbols"])
    entries.update(built)
    covered = {n for e in entries.values() for n in e["files"].values()}
    on_disk = {n for n in os.listdir(out) if n.endswith("_1d.parquet") or n.endswith("_5min.parquet")}
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store_asof": table.asof,
        "split_table": {"run_id": table.run_id, "sha256": table.sha256, "inputs_sha256": table.inputs_sha256,
                        "overrides_sha256": table.overrides_sha256},
        "stores": {
            "daily": {"path": daily_store, "STORE_INFO_sha256": _file_sha256(os.path.join(daily_store, "STORE_INFO.json"))},
            "5min": {"path": intraday_store, "STORE_INFO_sha256": _file_sha256(os.path.join(intraday_store, "STORE_INFO.json"))}},
        "input_chunks_sha256": hashlib.sha256("\n".join(f"{s}:{e['inputs_sha256']}" for s, e in sorted(entries.items())).encode()).hexdigest(),
        "subset": symbols is not None,
        "failed": failed,
        "skipped_no_bars": skipped,
        "unlisted_files": sorted(on_disk - covered),
        "workers": workers,
        "elapsed_s": round(time.time() - t0, 1),
        "symbols": entries,
    }
    atomic_write(mpath, json.dumps(manifest, indent=1).encode("utf-8"))
    summary = {"symbols_requested": len(todo), "built": len(built), "skipped_no_bars": len(skipped), "failed": len(failed),
               "files_1d": sum("1d" in e for e in built.values()), "files_5min": sum("5min" in e for e in built.values()),
               "bars_1d": sum(e["1d"]["bars"] for e in built.values() if "1d" in e),
               "bars_5min": sum(e["5min"]["bars"] for e in built.values() if "5min" in e),
               "dropped_by_break": sum(sum(e["dropped_by_break"].values()) for e in built.values()),
               "unlisted_files": len(manifest["unlisted_files"]), "elapsed_s": manifest["elapsed_s"], "manifest": mpath,
               "run_id": table.run_id,
               "per_symbol_s": {s: e["elapsed_s"] for s, e in built.items()}}
    if failed:
        first = next(iter(failed.items()))
        raise BuildError(f"{len(failed)} symbol(s) failed (listed in {mpath}); first: {first[0]}: {first[1]}")
    return summary
