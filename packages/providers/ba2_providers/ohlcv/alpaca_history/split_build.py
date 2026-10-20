"""The build / I-O half of the split table: loaders, the FMP calendar fetch, the writers and ``build_split_table``.

The RULES (and the full spec: verdict codes, tolerances, inputs, outputs) live in ``split_table.py``; this module only
reads the inputs, runs ``decide_splits`` and writes ``<out>/_meta``.  Run it through the pipeline CLI:

    python tools/prepare_history.py --source alpaca --steps split-table

Runbook: docs/REPRODUCE-BACKTESTS.md section 4.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
import requests

from ba2_common import config as _cfg
from ba2_providers.fmp_common import fmp_http_get, redact
from ba2_providers.ohlcv.alpaca_history.split_table import (
    ACCEPTED_COLUMNS, COMPARE_COLUMNS, OVERRIDES_PATH, REVIEW_COLUMNS, SplitTableBuildError, alpaca_events_from_actions, spinoff_events_from_actions,
    build_rename_map, compare_with_fmp, corporate_action_paths, decide_splits, fmp_events_from_rows, load_overrides,
    read_corporate_actions, rename_successors, splits_by_symbol)


def read_csv_frame(path: str) -> pd.DataFrame:
    """Read one of the tool's CSVs.  ``keep_default_na=False``: pandas' default would turn the symbol "NA" (and "NAN",
    "NULL", ...) into NaN.  Only the numeric columns read an empty cell as NaN; symbols and text stay strings."""
    numeric = {c: [""] for c in ("alpaca_ratio", "fmp_ratio", "jump", "factor", "our_factor")}
    return pd.read_csv(path, keep_default_na=False, na_values=numeric, dtype={"symbol": str, "fmp_step": str})


DEFAULT_STORE = os.path.join(_cfg.BA2_HOME, "alpaca_store_daily2016")
DEFAULT_OUT = os.path.join(_cfg.CACHE_FOLDER, "AlpacaHistoryOHLCVProvider")
DEFAULT_DB = os.path.join(_cfg.TRADE_DIR, "db.sqlite")
DEFAULT_FMP_DAILY = os.path.join(_cfg.CACHE_FOLDER, "FMPOHLCVProvider")
CALENDAR_WINDOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json$")   # the names fetch_fmp_calendar writes
FMP_URL = "https://financialmodelingprep.com/api/v3/stock_split_calendar"
FMP_WINDOW_DAYS = 30            # the endpoint clamps a range to its last ~90 days: stay far inside it
FMP_ROW_CAP = 1000              # a window returning this many rows may be truncated: split it (1 day is the floor)


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:                      # a failed write leaves no temp file behind (and re-raises)
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_fmp_key(db: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("select value_str from appsetting where key = 'FMP_API_KEY'").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SplitTableBuildError(f"no FMP_API_KEY in {db}")
    return row[0]


def fetch_fmp_calendar(start: date, end: date, cache_dir: str,
                       get: Callable[[date, date], List[Dict[str, Any]]],
                       stats: Counter, used_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Every row of FMP's split calendar over [start, end], in windows of FMP_WINDOW_DAYS; a window that returns
    FMP_ROW_CAP rows or more may be truncated and is split in halves (down to one day, where it is refused).  The
    raw response of each window is cached in ``cache_dir`` and read back on a rerun; ``used_paths`` (if given) collects the
    cache file of every window read or written."""
    rows: List[Dict[str, Any]] = []

    def window(a: date, b: date) -> None:
        path = os.path.join(cache_dir, f"{a.isoformat()}_{b.isoformat()}.json")
        if used_paths is not None:
            used_paths.append(path)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            stats["fmp_windows_cached"] += 1
        else:
            got = get(a, b)
            atomic_write(path, json.dumps(got).encode("utf-8"))
            stats["fmp_windows_fetched"] += 1
        # measured: the endpoint's `to` can spill ONE day past the window (a 2024-06-17 row for ..06-16); that row
        # belongs to the next window, which asks for it itself.  Anything further out is a change we refuse.
        lo, hi = (a - timedelta(days=1)).isoformat(), (b + timedelta(days=1)).isoformat()
        far = [r for r in got if not (lo <= r["date"][:10] <= hi)]
        if far:
            raise SplitTableBuildError(f"FMP split calendar {a}..{b} returned {len(far)} row(s) outside the window "
                                       f"(first: {far[0]['date']}): the endpoint's range handling changed")
        spill = [r for r in got if not (a.isoformat() <= r["date"][:10] <= b.isoformat())]
        stats["fmp_rows_spilled_to_next_window_dropped"] += len(spill)
        got_in = [r for r in got if a.isoformat() <= r["date"][:10] <= b.isoformat()]
        if len(got) >= FMP_ROW_CAP:
            if a == b:
                raise SplitTableBuildError(f"FMP split calendar for the single day {a} returned {len(got)} rows "
                                           f"(>= {FMP_ROW_CAP}): cannot rule out truncation")
            mid = a + (b - a) // 2
            stats["fmp_windows_split"] += 1
            window(a, mid)
            window(mid + timedelta(days=1), b)
            return
        rows.extend(got_in)

    a = start
    while a <= end:
        b = min(a + timedelta(days=FMP_WINDOW_DAYS - 1), end)
        window(a, b)
        a = b + timedelta(days=1)
    return rows


def make_fmp_getter(key: str, getter: Optional[Callable] = None) -> Callable[[date, date], List[Dict[str, Any]]]:
    """A window fetcher.  requests' HTTPError text holds the full URL WITH the key, so every request error is re-raised
    as a SplitTableBuildError carrying the redacted text only (it reaches stderr and the pipeline report)."""
    def get(a: date, b: date) -> List[Dict[str, Any]]:
        try:
            r = fmp_http_get(FMP_URL, params={"from": a.isoformat(), "to": b.isoformat(), "apikey": key},
                             endpoint="stock_split_calendar", timeout=120, getter=getter)
        except requests.RequestException as e:
            raise SplitTableBuildError(f"FMP split calendar {a}..{b}: {type(e).__name__}: {redact(e)}") from None
        j = r.json()
        if not isinstance(j, list):
            raise SplitTableBuildError(f"FMP split calendar {a}..{b}: unexpected payload {str(j)[:200]!r}")
        return j
    return get


def load_store_bars(store: str, symbols: Optional[List[str]]) -> Tuple[Dict[str, pd.DataFrame], str]:
    """symbol -> frame(t, o, c) and a sha256 over every bar file's bytes (symbol-ordered)."""
    root = os.path.join(store, "bars", "1Day")
    names = sorted(d[len("symbol="):] for d in os.listdir(root)
                   if d.startswith("symbol=") and os.path.isfile(os.path.join(root, d, "all.parquet")))
    if symbols is not None:
        missing = sorted(set(symbols) - set(names))
        if missing:
            raise SplitTableBuildError(f"--symbols not in the store: {missing}")
        names = sorted(symbols)

    def read(sym: str) -> Tuple[str, pd.DataFrame, str]:
        with open(os.path.join(root, f"symbol={sym}", "all.parquet"), "rb") as f:
            raw = f.read()
        df = pd.read_parquet(io.BytesIO(raw), columns=["t", "o", "c"])
        return sym, df, hashlib.sha256(raw).hexdigest()

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(read, names))
    h = hashlib.sha256()
    for sym, _, d in res:
        h.update(f"{sym}:{d}\n".encode())
    return {sym: df for sym, df, _ in res}, h.hexdigest()


def sha256_files(paths: Iterable[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(os.path.basename(p).encode() + b":" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()


FMP_INPUT_COLUMNS = ["file", "size", "mtime_ns", "mtime_utc", "sha256"]


def load_fmp_daily(fmp_dir: str, symbols: Iterable[str]
                   ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any], List[Dict[str, Any]]]:
    """The FMP ADJUSTED daily file (``<SYM>_1d.parquet``, Date + Close) of each symbol that has one; the summary
    ``{"dir", "count", "sha256", "content_sha256"}`` (sha256 over name|size|mtime_ns of each file, content_sha256 over
    name|size|sha256-of-the-bytes) and the per-file records (FMP_INPUT_COLUMNS, sorted by file) -- the record of exactly
    which bytes were read, kept because that cache is archived after the build."""
    names = sorted(symbols)

    def read(sym: str):
        path = os.path.join(fmp_dir, f"{sym.replace('.', '-')}_1d.parquet")
        if not os.path.isfile(path):
            return sym, None, None
        stt = os.stat(path)
        with open(path, "rb") as f:
            raw = f.read()
        df = pd.read_parquet(io.BytesIO(raw), columns=["Date", "Close"])
        return sym, df, {"file": os.path.basename(path), "size": len(raw), "mtime_ns": stt.st_mtime_ns,
                         "mtime_utc": datetime.fromtimestamp(stt.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                         "sha256": hashlib.sha256(raw).hexdigest()}

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(read, names))
    records = sorted((r for _, _, r in res if r is not None), key=lambda r: r["file"])
    meta_h, content_h = hashlib.sha256(), hashlib.sha256()
    for r in records:
        meta_h.update(f"{r['file']}|{r['size']}|{r['mtime_ns']}\n".encode())
        content_h.update(f"{r['file']}|{r['size']}|{r['sha256']}\n".encode())
    return ({sym: df for sym, df, _ in res if df is not None},
            {"dir": fmp_dir, "count": len(records), "sha256": meta_h.hexdigest(), "content_sha256": content_h.hexdigest()},
            records)


def _csv_bytes(columns: Sequence[str], rows: Iterable[Mapping[str, Any]], run_id: str) -> bytes:
    """The rows as CSV with the run's ``run_id`` as the last column of every row."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns) + ["run_id"], lineterminator="\n")
    w.writeheader()
    w.writerows({**r, "run_id": run_id} for r in rows)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Yahoo: the confirmation source of UNLISTED (inferred) splits only
# ---------------------------------------------------------------------------
YAHOO_INPUT_COLUMNS = ["symbol", "ticker", "status", "n_splits", "fetched_at", "sha256"]


class YahooUnavailable(RuntimeError):
    """Yahoo could not be reached for a ticker after the retries (transient: never cached)."""


def yfinance_fetch(ticker: str) -> Optional[List[Tuple[str, float]]]:
    """``[(iso date, ratio), ...]`` of Yahoo's split history for ``ticker`` (new/old shares, 4-for-1 -> 4.0); ``[]`` for a
    known ticker with no split; None when Yahoo does not know the ticker (it answers 404)."""
    import yfinance as yf
    sp = yf.Ticker(ticker).splits
    if sp is None:
        return None
    return [((ts.tz_convert("America/New_York") if ts.tzinfo else ts).date().isoformat(), float(v)) for ts, v in sp.items()]


class YahooSplitSource:
    """``source(symbol) -> {"available", "ticker", "splits"}`` for ``decide_splits(yahoo=...)``.  Per-symbol raw results are
    cached as JSON in ``cache_dir`` (``fetched_at`` inside), so a rerun is offline; ``refresh`` fetches again.  Yahoo uses
    CURRENT tickers: when a symbol is unknown (404) or lists no split, its renamed successors (Alpaca name-change chain) are
    tried in turn and the answering ticker is recorded.  Requests are paced to ``min_interval`` seconds and retried; a
    symbol still failing is ``available: False`` and is NOT cached (a rerun tries it again)."""

    def __init__(self, cache_dir: str, renames: Mapping[str, Sequence[Tuple[date, str]]], *, refresh: bool = False,
                 fetch: Callable[[str], Optional[List[Tuple[str, float]]]] = yfinance_fetch,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 min_interval: float = 1.0, retries: int = 3, backoff: Sequence[float] = (2.0, 5.0, 15.0)):
        self.cache_dir, self.renames, self.refresh, self.fetch = cache_dir, renames, refresh, fetch
        self.sleep, self.clock, self.min_interval, self.retries, self.backoff = sleep, clock, min_interval, retries, backoff
        self.records: Dict[str, Dict[str, Any]] = {}       # symbol -> row of yahoo_inputs.csv, for the symbols consulted
        self._last = -1e18
        self.fetched = 0

    def _call(self, ticker: str) -> Optional[List[Tuple[str, float]]]:
        last: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            try:
                return self.fetch(ticker)
            except Exception as e:                      # a transient network / rate-limit error: retry, then give up loudly
                last = e
                if attempt < self.retries:
                    self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise YahooUnavailable(f"{ticker}: {type(last).__name__}: {last}")

    def _fetch_symbol(self, symbol: str) -> Dict[str, Any]:
        tried: List[str] = []
        empty_ok: Optional[str] = None
        try:
            for ticker in [symbol] + rename_successors(symbol, self.renames):
                got = self._call(ticker)
                if got is None:
                    tried.append(f"{ticker}: not found")
                    continue
                if got:
                    return {"status": "ok", "ticker": ticker, "splits": [[d, r] for d, r in got], "tried": tried}
                tried.append(f"{ticker}: no splits")
                empty_ok = empty_ok or ticker
        except YahooUnavailable as e:
            return {"status": "error", "ticker": None, "splits": [], "tried": tried + [str(e)]}
        if empty_ok is not None:
            return {"status": "ok", "ticker": empty_ok, "splits": [], "tried": tried}
        return {"status": "not_found", "ticker": None, "splits": [], "tried": tried}

    def __call__(self, symbol: str) -> Dict[str, Any]:
        path = os.path.join(self.cache_dir, f"{symbol}.json")
        if not self.refresh and os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            data = json.loads(raw)
        else:
            data = self._fetch_symbol(symbol)
            data = {"symbol": symbol, **data, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            raw = b""
            if data["status"] != "error":
                raw = json.dumps(data, indent=1).encode("utf-8")
                atomic_write(path, raw)
            self.fetched += 1
        self.records[symbol] = {"symbol": symbol, "ticker": data["ticker"] or "", "status": data["status"],
                                "n_splits": len(data["splits"]), "fetched_at": data["fetched_at"],
                                "sha256": hashlib.sha256(raw).hexdigest() if raw else ""}
        return {"available": data["status"] == "ok", "ticker": data["ticker"], "splits": data["splits"]}

    def inputs(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """The rows of yahoo_inputs.csv (sorted by symbol) and the summary for split_table.json."""
        rows = [self.records[s] for s in sorted(self.records)]
        h = hashlib.sha256()
        for r in rows:
            if r["sha256"]:
                h.update(f"{r['symbol']}|{r['sha256']}\n".encode())
        return rows, {"dir": self.cache_dir, "count": len(rows), "content_sha256": h.hexdigest(),
                      "inputs_csv": "yahoo_inputs.csv"}


def build_split_table(*, store: str = DEFAULT_STORE, out: str = DEFAULT_OUT, db: str = DEFAULT_DB,
                      fmp_daily_dir: str = DEFAULT_FMP_DAILY, symbols: Optional[List[str]] = None,
                      fmp_from: str = "2016-01-01", fmp_to: Optional[str] = None,
                      overrides_path: str = OVERRIDES_PATH, log: Callable[[str], None] = print,
                      refresh_yahoo: bool = False, yahoo_fetch: Optional[Callable] = None, yahoo_min_interval: float = 1.0,
                      yahoo_sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Build and write ``<out>/_meta/split_table.json`` (+ review / accepted / vs-FMP CSVs).  Returns a summary:
    ``{"meta", "asof", "symbols_judged", "symbols_with_splits", "accepted", "review_rows", "review_applied",
    "stats", "compare", "compare_examples"}``.  Raises ``SplitOverridesPending`` while an override is pending.
    ``fmp_to`` defaults to the store's end (STORE_INFO asof + 1 day); a value before the store's asof is refused."""
    overrides = load_overrides(overrides_path)
    with open(overrides_path, "rb") as f:
        overrides_sha = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(store, "STORE_INFO.json"), encoding="utf-8") as f:
        info = json.load(f)
    store_end = (date.fromisoformat(info["asof"]) + timedelta(days=1)).isoformat()
    if fmp_to is None:
        fmp_to = store_end
    elif fmp_to < info["asof"]:
        raise SplitTableBuildError(f"fmp_to {fmp_to} is before the store's asof {info['asof']}: FMP's calendar would not "
                                   f"cover the store's last sessions")
    stats: Counter = Counter()
    t0 = time.time()
    months = read_corporate_actions(store)
    renames = build_rename_map(months)
    alpaca = alpaca_events_from_actions(months, renames, stats)
    spinoffs = spinoff_events_from_actions(months, renames, stats)
    if stats["alpaca_forward_splits"] + stats["alpaca_reverse_splits"] <= 0:
        raise SplitTableBuildError(f"the store's corporate actions hold no forward or reverse split at all ({store}): "
                                   f"the files are empty or have changed shape")
    meta = os.path.join(out, "_meta")
    cal_dir = os.path.join(meta, "fmp_split_calendar")
    lazy: Dict[str, Any] = {}

    def get(a: date, b: date) -> List[Dict[str, Any]]:     # key read (and requests built) only for an uncached window
        if "get" not in lazy:
            lazy["get"] = make_fmp_getter(read_fmp_key(db))
        return lazy["get"](a, b)

    cal_used: List[str] = []
    cal_rows = fetch_fmp_calendar(date.fromisoformat(fmp_from), date.fromisoformat(fmp_to), cal_dir, get, stats, cal_used)
    fmp = fmp_events_from_rows(cal_rows, renames, stats)
    bars, bars_sha = load_store_bars(store, symbols)
    fmp_daily, fmp_files, fmp_records = load_fmp_daily(fmp_daily_dir, bars)
    log(f"loaded {len(bars)} symbols, {len(alpaca)} Alpaca and {len(fmp)} FMP listings, {fmp_files['count']} FMP daily "
        f"files in {time.time() - t0:.0f}s")

    breaks: Dict[str, List[str]] = {}
    yahoo = YahooSplitSource(os.path.join(meta, "yahoo_splits"), renames, refresh=refresh_yahoo,
                             fetch=yahoo_fetch or yfinance_fetch, sleep=yahoo_sleep, min_interval=yahoo_min_interval)
    accepted, review = decide_splits(bars, alpaca, fmp, overrides, stats, fmp_daily=fmp_daily, breaks=breaks,
                                     spinoffs=spinoffs, yahoo=yahoo)
    yahoo_rows, yahoo_files = yahoo.inputs()
    stats["yahoo_symbols_consulted"] = len(yahoo_rows)
    stats["yahoo_symbols_fetched"] = yahoo.fetched
    cmp_rows = compare_with_fmp(bars, accepted, review, alpaca, fmp, fmp_daily)

    h = hashlib.sha256()          # the inputs actually read: the store's action files, the calendar windows used
    for part in (bars_sha, sha256_files(corporate_action_paths(store)), sha256_files(cal_used), f"{fmp_from}|{fmp_to}"):
        h.update(part.encode())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{hashlib.sha256((stamp + h.hexdigest() + overrides_sha + fmp_files['content_sha256'] + yahoo_files['content_sha256']).encode()).hexdigest()[:8]}"
    table = {"run_id": run_id, "asof": info["asof"], "inputs_sha256": h.hexdigest(), "overrides_sha256": overrides_sha,
             "fmp_daily_files": {**fmp_files, "inputs_csv": "fmp_daily_inputs.csv"}, "yahoo_files": yahoo_files,
             "breaks": {k: sorted(v) for k, v in sorted(breaks.items())},
             "spinoffs": splits_by_symbol(accepted, "spinoff"), "splits": splits_by_symbol(accepted, "split")}
    # one coherent set: every CSV carries the run_id, and split_table.json is written LAST, so a crash never leaves a table
    # newer than its CSVs (a reader checks that the run_ids of the table and the CSVs match)
    atomic_write(os.path.join(meta, "split_review.csv"), _csv_bytes(REVIEW_COLUMNS, review, run_id))
    atomic_write(os.path.join(meta, "split_accepted.csv"), _csv_bytes(ACCEPTED_COLUMNS, accepted, run_id))
    atomic_write(os.path.join(meta, "split_vs_fmp.csv"), _csv_bytes(COMPARE_COLUMNS, cmp_rows, run_id))
    atomic_write(os.path.join(meta, "fmp_daily_inputs.csv"), _csv_bytes(FMP_INPUT_COLUMNS, fmp_records, run_id))
    atomic_write(os.path.join(meta, "yahoo_inputs.csv"), _csv_bytes(YAHOO_INPUT_COLUMNS, yahoo_rows, run_id))
    atomic_write(os.path.join(meta, "split_table.json"), json.dumps(table, indent=1).encode("utf-8"))
    for (src, verdict), k in sorted(Counter((a["source"], a["verdict"]) for a in accepted).items()):
        stats[f"accepted_{verdict}_from_{src}"] += k
    classes = ("agree", "ratio_differs", "ours_only", "fmp_only_accepted", "fmp_only_rejected")
    return {"meta": meta, "run_id": run_id, "asof": info["asof"], "fmp_to": fmp_to, "symbols_judged": len(bars),
            "symbols_with_splits": len(table["splits"]),
            "spinoffs_applied": sum(len(v) for v in table["spinoffs"].values()), "series_breaks": sum(len(v) for v in breaks.values()), "accepted": len(accepted), "review_rows": len(review),
            "review_applied": sum(r["applied"] for r in review),
            "fmp_daily_files": fmp_files, "yahoo_files": yahoo_files, "stats": dict(sorted(stats.items())),
            "compare": {c: sum(1 for r in cmp_rows if r["class"] == c) for c in classes},
            "compare_examples": {c: [r for r in cmp_rows if r["class"] == c][:10] for c in classes if c != "agree"}}
