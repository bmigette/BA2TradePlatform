"""Download RAW Alpaca stock bars (SIP feed) and corporate actions into a separate, resumable store.

Why raw: Alpaca's adjusted bars depend on the corporate-action table at query time, so an adjusted
store goes stale at the next split. This store keeps what actually traded plus the actions; the
adjustment is ours to apply.

Guarantees
- A chunk (one symbol's daily history, one symbol-year of intraday bars, one month of corporate
  actions) is fetched completely, validated, and only then written: temp file + os.replace, then
  the manifest. A crash, a failed page or a refused chunk leaves no file a reader could take for data.
- An intraday chunk that lacks bars on MORE than --max-gap-days days where the symbol has a daily bar with
  trades is REFUSED (nothing written, the days are listed in the manifest). Up to that many such days, the
  chunk is stored and its manifest entry lists them (days_without_intraday_bars); readers must honour it. Use --retry-refused to try such chunks again. A daily bar
  with volume 0 and 0 trades is Alpaca's placeholder for a day nothing traded: no intraday bar is expected
  on it, and the chunk's manifest entry counts those days (zero_trade_daily_days).
- Transport errors, HTTP 5xx and 429 are retried with exponential backoff (429 honours Retry-After /
  X-RateLimit-Reset). A chunk whose retries run out is left untouched and fetched on the next run.
- Resume: a chunk with a manifest entry for the same window whose file is present is skipped.

Layout (under --store)
  STORE_INFO.json                               feed, adjustment, asof, start: pinned at creation
  bars/1Day/symbol=<S>/all.parquet              t (UTC bar start), o, h, l, c, v, n, vw
  bars/5Min/symbol=<S>/<year>.parquet           one file per UTC calendar year
  manifest/symbol=<S>.json                      one entry per chunk (window, bars, pages, sha256, status)
  corporate_actions/ym=<YYYY-MM>.json.gz        market-wide, as the vendor returned them
  corporate_actions/manifest.json
  state/progress.json, state/failures.jsonl

Credentials: --keys-db/--account-id (read-only, table accountsetting) or the environment variables
APCA_API_KEY_ID / APCA_API_SECRET_KEY. They are never logged or written.

Example
  python tools/alpaca_history_download.py --store D:/alpaca_store --symbols-file symbols.txt \
      --start 2019-01-01 --end 2026-10-13 --keys-db ~/Documents/ba2/trade/db.sqlite --account-id 1 --max-gap-days 5

Resume with the SAME --end: without it the end defaults to today, every daily chunk's window changes and every
daily file is fetched again. Normally run through `tools/prepare_history.py --source alpaca --steps download` (the
recorded arguments of both stores); the stores, results and later steps: docs/REPRODUCE-BACKTESTS.md §4.0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

DATA_HOST = "https://data.alpaca.markets"
LAYOUT_VERSION = 1
FEED = "sip"
ADJUSTMENT = "raw"
PAGE_LIMIT = 10000
CA_PAGE_LIMIT = 1000
NEW_YORK = "America/New_York"
BAR_COLUMNS = ["t", "o", "h", "l", "c", "v", "n", "vw"]
INTRADAY_TIMEFRAME = "5Min"
DAILY_TIMEFRAME = "1Day"
CA_TYPES = ("forward_split,reverse_split,unit_split,stock_dividend,cash_dividend,spin_off,"
            "cash_merger,stock_merger,stock_and_cash_merger,redemption,name_change,worthless_removal,"
            "rights_distribution")
STATUS_COMPLETE = "complete"
STATUS_NO_DATA = "no_data"
STATUS_NO_DAILY_DAYS = "no_daily_days"
REPLACE_ATTEMPTS = 20      # os.replace tries before a held target file stops the run
REPLACE_WAIT_S = 0.25
UTC = dt.timezone.utc


class FatalError(Exception):
    """The run cannot continue (credentials, store mismatch, an outage)."""


class ChunkFailed(Exception):
    """Transport gave up on this chunk. Nothing is recorded; the next run fetches it again."""


class ChunkRefused(Exception):
    """The vendor answered, and the answer is not usable. Recorded in the manifest; no file is written."""

    def __init__(self, reason: str, detail: object = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def log(message: str) -> None:
    print(f"{dt.datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}", flush=True)


def iso_z(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    # Windows refuses the swap while another process (a reader, an indexer, a virus scan) holds the
    # target open; that hold is brief, so wait it out a bounded number of times, then raise.
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT_S)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_bytes(path, json.dumps(payload, indent=1, sort_keys=True).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def alpaca_symbol(symbol: str) -> str:
    """Our (FMP-style) class-share spelling BRK-B is BRK.B at Alpaca."""
    return symbol.replace("-", ".")


class Transport:
    """Throttled GET with retry and backoff against the Alpaca data API."""

    def __init__(self, key_id: str, secret: str, *, rpm: float, max_attempts: int, min_remaining: int,
                 opener: Callable = urllib.request.urlopen, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        if rpm <= 0 or max_attempts < 1:
            raise FatalError("rpm must be > 0 and max_attempts >= 1")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret,
                         "Accept": "application/json", "Accept-Encoding": "gzip"}
        self._gap = 60.0 / rpm
        self._max_attempts = max_attempts
        self._min_remaining = min_remaining
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._next_at = 0.0
        self._guard_state: Optional[bool] = None
        self.requests = 0
        self.bytes = 0
        self.retries = 0
        self.rate_limited = 0

    def _throttle(self) -> None:
        wait = self._next_at - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._next_at = self._clock() + self._gap

    def _backoff(self, attempt: int) -> float:
        return min(120.0, 2.0 * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)

    def _rate_limit_wait(self, headers, attempt: int) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after is not None:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        reset = headers.get("X-RateLimit-Reset") if headers is not None else None
        if reset is not None:
            try:
                return min(120.0, max(1.0, float(reset) - self._clock() + 1.0))
            except ValueError:
                pass
        return self._backoff(attempt)

    def _note_budget(self, headers) -> None:
        """Leave --min-remaining calls of the shared per-minute budget to whoever else uses the key."""
        remaining, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        present = remaining is not None and reset is not None
        if self._guard_state is None or self._guard_state != present:
            self._guard_state = present
            log("rate-limit headers present: shared-budget guard ACTIVE" if present else
                "WARNING rate-limit headers absent: shared-budget guard INACTIVE, throttle only")
        if not present:
            return
        try:
            if int(remaining) < self._min_remaining:
                resume_at = float(reset) + 1.0
                if resume_at > self._next_at:
                    log(f"budget low (remaining {remaining}): pausing {max(0.0, resume_at - self._clock()):.0f}s")
                    self._next_at = resume_at
        except ValueError:
            log(f"WARNING unparseable rate-limit headers remaining={remaining!r} reset={reset!r}")

    def get(self, path: str, params: dict) -> dict:
        url = f"{DATA_HOST}{path}?{urllib.parse.urlencode(params)}"
        last_error = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers)
            try:
                with self._opener(request, timeout=90) as response:
                    raw = response.read()
                    headers = response.headers
                self.requests += 1
                self.bytes += len(raw)
                if headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError(f"JSON root is {type(payload).__name__}, expected an object")
            except urllib.error.HTTPError as error:
                body = error.read()[:300].decode("utf-8", "replace")
                if error.code in (401, 403):
                    raise FatalError(f"HTTP {error.code} from Alpaca (credentials or plan): {body}") from error
                if error.code == 429:
                    self.rate_limited += 1
                    wait = self._rate_limit_wait(error.headers, attempt)
                elif error.code >= 500:
                    wait = self._backoff(attempt)
                else:
                    raise ChunkRefused(f"http_{error.code}", body) from error
                last_error = f"HTTP {error.code}: {body}"
            except (urllib.error.URLError, http.client.HTTPException, OSError, EOFError, ValueError) as error:
                # URLError/OSError: connection and timeout; HTTPException: a cut response;
                # ValueError: truncated or non-JSON body. All are worth another attempt.
                wait = self._backoff(attempt)
                last_error = f"{type(error).__name__}: {error}"
            else:
                self._note_budget(headers)
                return payload
            self.retries += 1
            if attempt < self._max_attempts:
                log(f"  retry {attempt}/{self._max_attempts - 1} in {wait:.0f}s after {last_error}")
                self._sleep(wait)
        raise ChunkFailed(f"retries exhausted after {self._max_attempts} attempts: {last_error}")


def fetch_bars(transport: Transport, symbol: str, timeframe: str, start: dt.datetime,
               end_exclusive: dt.datetime, asof: str) -> tuple[list, int]:
    """All pages of one symbol's raw bars in [start, end_exclusive). Returns (bars, pages)."""
    params = {"symbols": symbol, "timeframe": timeframe, "start": iso_z(start),
              # Alpaca's `end` is inclusive; bars sit on a grid of whole minutes, so one second short
              # of the boundary keeps the boundary bar out of this chunk and in the next one.
              "end": iso_z(end_exclusive - dt.timedelta(seconds=1)),
              "adjustment": ADJUSTMENT, "feed": FEED, "limit": PAGE_LIMIT, "sort": "asc", "asof": asof}
    bars: list = []
    pages = 0
    token = None
    seen_tokens: set = set()
    while True:
        query = dict(params)
        if token:
            query["page_token"] = token
        payload = transport.get("/v2/stocks/bars", query)
        pages += 1
        if "bars" not in payload or "next_page_token" not in payload:
            raise ChunkRefused("bad_response_shape", sorted(payload)[:10])
        by_symbol = payload["bars"] or {}
        unexpected = sorted(set(by_symbol) - {symbol})
        if unexpected:
            raise ChunkRefused("unexpected_symbols", unexpected[:10])
        bars.extend(by_symbol.get(symbol, []))
        token = payload["next_page_token"]
        if not token:
            return bars, pages
        if token in seen_tokens:
            raise ChunkRefused("pagination_loop", token)
        seen_tokens.add(token)


def bars_to_frame(bars: list, start: dt.datetime, end_exclusive: dt.datetime) -> pd.DataFrame:
    """Validate one chunk's bars and return them as a frame. Refuses anything that is not clean."""
    if not bars:
        return pd.DataFrame({name: [] for name in BAR_COLUMNS})
    frame = pd.DataFrame(bars)
    missing = [name for name in BAR_COLUMNS if name not in frame.columns]
    if missing:
        raise ChunkRefused("missing_columns", missing)
    if frame[BAR_COLUMNS].isna().any().any():
        raise ChunkRefused("null_values", int(frame[BAR_COLUMNS].isna().any(axis=1).sum()))
    try:
        frame["t"] = pd.to_datetime(frame["t"], utc=True)
    except (ValueError, TypeError) as error:
        raise ChunkRefused("bad_timestamp", str(error)[:200]) from error
    stamps = frame["t"]
    if stamps.duplicated().any():
        raise ChunkRefused("duplicate_timestamps", str(stamps[stamps.duplicated()].iloc[0]))
    if not stamps.is_monotonic_increasing:
        raise ChunkRefused("unsorted_timestamps")
    if (stamps < pd.Timestamp(start)).any() or (stamps >= pd.Timestamp(end_exclusive)).any():
        raise ChunkRefused("bar_outside_window", [str(stamps.iloc[0]), str(stamps.iloc[-1])])
    prices = frame[["o", "h", "l", "c"]].astype("float64")
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ChunkRefused("non_positive_or_non_finite_price")
    if (prices["h"] < prices["l"]).any():
        raise ChunkRefused("high_below_low")
    if (frame["v"] < 0).any():
        raise ChunkRefused("negative_volume")
    return frame[BAR_COLUMNS].reset_index(drop=True)


def session_days(frame: pd.DataFrame) -> set:
    """New York calendar dates that have at least one bar."""
    if frame.empty:
        return set()
    return set(frame["t"].dt.tz_convert(NEW_YORK).dt.date)


def year_chunks(start: dt.datetime, end_exclusive: dt.datetime) -> list:
    """[start, end) cut at UTC year boundaries."""
    chunks = []
    cursor = start
    while cursor < end_exclusive:
        boundary = dt.datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        chunks.append((cursor, min(boundary, end_exclusive)))
        cursor = boundary
    return chunks


def month_chunks(start: dt.date, end_exclusive: dt.date) -> list:
    """Calendar months as (label, first_day, last_day_inclusive), clipped to [start, end)."""
    chunks = []
    cursor = start
    while cursor < end_exclusive:
        next_month = dt.date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        last = min(next_month, end_exclusive) - dt.timedelta(days=1)
        chunks.append((cursor.strftime("%Y-%m"), cursor, last))
        cursor = next_month
    return chunks


class SymbolManifest:
    """manifest/symbol=<S>.json: what this store holds for one symbol, one entry per chunk."""

    def __init__(self, store: Path, symbol: str):
        self.path = store / "manifest" / f"symbol={symbol}.json"
        self.data = {"symbol": symbol, "alpaca_symbol": alpaca_symbol(symbol), "chunks": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def entry(self, key: str) -> Optional[dict]:
        return self.data["chunks"].get(key)

    def record(self, key: str, entry: dict) -> None:
        self.data["chunks"][key] = entry
        atomic_write_json(self.path, self.data)


class Downloader:
    def __init__(self, store: Path, transport: Transport, *, start: dt.datetime, end_exclusive: dt.datetime,
                 asof: str, retry_refused: bool, max_consecutive_failures: int, max_gap_days: int):
        self.store = store
        self.transport = transport
        self.start = start
        self.end_exclusive = end_exclusive
        self.asof = asof
        self.retry_refused = retry_refused
        self.max_consecutive_failures = max_consecutive_failures
        self.max_gap_days = max_gap_days
        self.counts = {"written": 0, "skipped": 0, "refused": 0, "failed": 0, "empty": 0}
        self.consecutive_failures = 0
        self.started = time.time()

    # --- bookkeeping -------------------------------------------------------------------------
    def _failure(self, scope: str, key: str, error: Exception) -> None:
        self.counts["failed"] += 1
        self.consecutive_failures += 1
        path = self.store / "state" / "failures.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": iso_z(dt.datetime.now(UTC)), "scope": scope, "chunk": key,
                                     "error": str(error)}) + "\n")
        log(f"FAILED {scope} {key}: {error}")
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise FatalError(f"{self.consecutive_failures} chunks failed in a row: stopping "
                             "(nothing partial was written; run again to resume)")

    def write_progress(self, phase: str, position: str) -> None:
        atomic_write_json(self.store / "state" / "progress.json", {
            "at": iso_z(dt.datetime.now(UTC)), "phase": phase, "position": position, "pid": os.getpid(),
            "counts": self.counts, "requests": self.transport.requests, "bytes": self.transport.bytes,
            "retries": self.transport.retries, "rate_limited": self.transport.rate_limited,
            "elapsed_s": round(time.time() - self.started)})

    def _window(self, start: dt.datetime, end_exclusive: dt.datetime) -> dict:
        return {"start": iso_z(start), "end_exclusive": iso_z(end_exclusive)}

    def _is_done(self, manifest: SymbolManifest, key: str, window: dict, path: Optional[Path]) -> bool:
        entry = manifest.entry(key)
        if entry is None:
            return False
        same_window = {name: entry.get(name) for name in window} == window
        status = entry["status"]
        if status == STATUS_COMPLETE and same_window:
            if path is not None and path.exists() and path.stat().st_size == entry["file_bytes"]:
                return True
            log(f"WARNING {manifest.data['symbol']} {key}: manifest says complete but the file is "
                "missing or has another size: fetching again")
            return False
        if status == STATUS_COMPLETE:
            # Stored for an earlier window; this run extends it unless that extension was already refused.
            refused = entry.get("refused_update")
            return (bool(refused) and {name: refused.get(name) for name in window} == window
                    and not self.retry_refused)
        if not same_window:
            return False
        if status in (STATUS_NO_DATA, STATUS_NO_DAILY_DAYS):
            return True
        return not self.retry_refused  # a refused chunk is retried only on request

    def _write_bars(self, manifest: SymbolManifest, key: str, window: dict, path: Path,
                    frame: pd.DataFrame, pages: int, extra: Optional[dict] = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        frame.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, path)
        entry = dict(window, status=STATUS_COMPLETE, bars=int(len(frame)), pages=pages,
                     first_t=iso_z(frame["t"].iloc[0]), last_t=iso_z(frame["t"].iloc[-1]),
                     file=path.relative_to(self.store).as_posix(), file_bytes=path.stat().st_size,
                     sha256=sha256_file(path), asof=self.asof, fetched_at=iso_z(dt.datetime.now(UTC)))
        entry.update(extra or {})
        manifest.record(key, entry)
        self.counts["written"] += 1

    def _refuse(self, manifest: SymbolManifest, key: str, window: dict, path: Path,
                error: ChunkRefused) -> None:
        refusal = dict(window, status=f"refused_{error.reason}", detail=error.detail, asof=self.asof,
                       fetched_at=iso_z(dt.datetime.now(UTC)))
        entry = manifest.entry(key)
        if entry is not None and entry["status"] == STATUS_COMPLETE:
            # Verified data stored for an earlier window stays; the refused update is noted beside it.
            manifest.record(key, dict(entry, refused_update=refusal))
        else:
            manifest.record(key, refusal)
        self.counts["refused"] += 1
        self.consecutive_failures = 0  # the vendor answered: this is not an outage
        log(f"REFUSED {manifest.data['symbol']} {key}: {error.reason} {json.dumps(error.detail)[:300]}")

    # --- daily ---------------------------------------------------------------------------------
    def daily(self, symbol: str) -> None:
        manifest = SymbolManifest(self.store, symbol)
        key = f"{DAILY_TIMEFRAME}/all"
        window = self._window(self.start, self.end_exclusive)
        path = self.store / "bars" / DAILY_TIMEFRAME / f"symbol={symbol}" / "all.parquet"
        if self._is_done(manifest, key, window, path):
            self.counts["skipped"] += 1
            return
        try:
            bars, pages = fetch_bars(self.transport, manifest.data["alpaca_symbol"], DAILY_TIMEFRAME,
                                     self.start, self.end_exclusive, self.asof)
            frame = bars_to_frame(bars, self.start, self.end_exclusive)
            if not frame.empty and len(session_days(frame)) != len(frame):
                raise ChunkRefused("two_daily_bars_on_one_day")
            if frame.empty and (manifest.entry(key) or {}).get("status") == STATUS_COMPLETE:
                raise ChunkRefused("no_bars_where_an_earlier_fetch_had_some")
        except ChunkRefused as error:
            self._refuse(manifest, key, window, path, error)
            return
        except ChunkFailed as error:
            self._failure(symbol, key, error)
            return
        self.consecutive_failures = 0
        if frame.empty:
            manifest.record(key, dict(window, status=STATUS_NO_DATA, asof=self.asof,
                                      fetched_at=iso_z(dt.datetime.now(UTC))))
            self.counts["empty"] += 1
            log(f"NO DATA {symbol} ({manifest.data['alpaca_symbol']}): Alpaca has no daily bars in the window")
            return
        self._write_bars(manifest, key, window, path, frame, pages)

    def _daily_days(self, symbol: str, manifest: SymbolManifest) -> Optional[tuple]:
        """(traded days, zero-trade days) from the stored daily file; None when there is no usable daily chunk.

        Alpaca serves a placeholder daily bar on a day nothing traded (volume 0, 0 trades, flat price); no
        intraday bar can exist on such a day, so it is not a day an intraday chunk must cover."""
        key = f"{DAILY_TIMEFRAME}/all"
        window = self._window(self.start, self.end_exclusive)
        entry = manifest.entry(key)
        if entry is None or {name: entry.get(name) for name in window} != window:
            return None
        if entry["status"] == STATUS_NO_DATA:
            return set(), set()
        if entry["status"] != STATUS_COMPLETE:
            return None
        frame = pd.read_parquet(self.store / entry["file"], columns=["t", "v", "n"])
        no_trade = (frame["v"] == 0) & (frame["n"] == 0)
        return session_days(frame[~no_trade]), session_days(frame[no_trade])

    # --- intraday ------------------------------------------------------------------------------
    def intraday(self, symbol: str) -> None:
        manifest = SymbolManifest(self.store, symbol)
        daily = self._daily_days(symbol, manifest)
        if daily is None:
            # No verified daily series means no way to tell a complete intraday chunk from a holed one.
            log(f"SKIP {symbol} {INTRADAY_TIMEFRAME}: no complete daily chunk for this window to check against")
            return
        days, zero_trade_days = daily
        for chunk_start, chunk_end in year_chunks(self.start, self.end_exclusive):
            key = f"{INTRADAY_TIMEFRAME}/{chunk_start.year}"
            window = self._window(chunk_start, chunk_end)
            path = self.store / "bars" / INTRADAY_TIMEFRAME / f"symbol={symbol}" / f"{chunk_start.year}.parquet"
            if self._is_done(manifest, key, window, path):
                self.counts["skipped"] += 1
                continue
            expected = {day for day in days if chunk_start.date() <= day < _ceil_date(chunk_end)}
            zero_trade = {day for day in zero_trade_days if chunk_start.date() <= day < _ceil_date(chunk_end)}
            if not expected:
                if (manifest.entry(key) or {}).get("status") == STATUS_COMPLETE:
                    self._refuse(manifest, key, window, path,
                                 ChunkRefused("no_daily_days_where_an_earlier_fetch_had_bars"))
                    continue
                manifest.record(key, dict(window, status=STATUS_NO_DAILY_DAYS, asof=self.asof,
                                          fetched_at=iso_z(dt.datetime.now(UTC))))
                self.counts["empty"] += 1
                continue
            began = time.time()
            try:
                bars, pages = fetch_bars(self.transport, manifest.data["alpaca_symbol"], INTRADAY_TIMEFRAME,
                                         chunk_start, chunk_end, self.asof)
                frame = bars_to_frame(bars, chunk_start, chunk_end)
                missing = sorted(expected - session_days(frame))
                gaps = {"count": len(missing), "of": len(expected), "days": [day.isoformat() for day in missing[:400]]}
                if missing and (len(missing) > self.max_gap_days or frame.empty):
                    raise ChunkRefused("days_without_intraday_bars", gaps)
            except ChunkRefused as error:
                self._refuse(manifest, key, window, path, error)
                continue
            except ChunkFailed as error:
                self._failure(symbol, key, error)
                continue
            self.consecutive_failures = 0
            extra_days = len(session_days(frame) - expected)
            self._write_bars(manifest, key, window, path, frame, pages,
                             {"daily_days": len(expected), "zero_trade_daily_days": len(zero_trade),
                              "days_without_daily_bar": extra_days, "days_without_intraday_bars": gaps})
            log(f"  {symbol} {key}: {len(frame):,} bars, {pages} pages, {time.time() - began:.1f}s")

    # --- corporate actions ---------------------------------------------------------------------
    def corporate_actions(self) -> None:
        folder = self.store / "corporate_actions"
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        for label, first, last in month_chunks(self.start.date(), _ceil_date(self.end_exclusive)):
            path = folder / f"ym={label}.json.gz"
            window = {"start": first.isoformat(), "end_inclusive": last.isoformat()}
            entry = manifest.get(label)
            if (entry and {name: entry.get(name) for name in window} == window and path.exists()
                    and path.stat().st_size == entry["file_bytes"]):
                self.counts["skipped"] += 1
                continue
            try:
                actions, pages = self._fetch_corporate_actions(first, last)
            except ChunkRefused as error:
                self.counts["refused"] += 1
                log(f"REFUSED corporate actions {label}: {error.reason} {json.dumps(error.detail)[:300]}")
                continue
            except ChunkFailed as error:
                self._failure("corporate_actions", label, error)
                continue
            self.consecutive_failures = 0
            atomic_write_bytes(path, gzip.compress(json.dumps(actions, sort_keys=True).encode("utf-8")))
            manifest[label] = dict(window, pages=pages, file_bytes=path.stat().st_size,
                                   sha256=sha256_file(path), types=CA_TYPES,
                                   rows={kind: len(rows) for kind, rows in sorted(actions.items())},
                                   fetched_at=iso_z(dt.datetime.now(UTC)))
            atomic_write_json(manifest_path, manifest)
            self.counts["written"] += 1
            log(f"  corporate actions {label}: {sum(len(rows) for rows in actions.values()):,} rows, {pages} pages")

    def _fetch_corporate_actions(self, first: dt.date, last: dt.date) -> tuple[dict, int]:
        params = {"types": CA_TYPES, "start": first.isoformat(), "end": last.isoformat(),
                  "limit": CA_PAGE_LIMIT, "sort": "asc"}
        merged: dict = {}
        pages = 0
        token = None
        seen_tokens: set = set()
        while True:
            query = dict(params)
            if token:
                query["page_token"] = token
            payload = self.transport.get("/v1/corporate-actions", query)
            pages += 1
            if "next_page_token" not in payload or not isinstance(payload.get("corporate_actions"), dict):
                raise ChunkRefused("bad_response_shape", sorted(payload)[:10])
            for kind, rows in payload["corporate_actions"].items():
                if not isinstance(rows, list):
                    raise ChunkRefused("bad_response_shape", kind)
                merged.setdefault(kind, []).extend(rows)
            token = payload["next_page_token"]
            if not token:
                return merged, pages
            if token in seen_tokens:
                raise ChunkRefused("pagination_loop", token)
            seen_tokens.add(token)


def _ceil_date(moment: dt.datetime) -> dt.date:
    """The first date NOT covered by a window that ends (exclusive) at `moment`."""
    return moment.date() if moment.time() == dt.time(0) else moment.date() + dt.timedelta(days=1)


def sweep_temp_files(store: Path) -> int:
    removed = 0
    for tmp in store.rglob("*.tmp"):
        tmp.unlink()
        removed += 1
    return removed


def open_store(store: Path, start: dt.datetime, asof: Optional[str], default_asof: str) -> str:
    """Create or check STORE_INFO.json; returns the store's pinned asof date."""
    info_path = store / "STORE_INFO.json"
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        expected = {"layout_version": LAYOUT_VERSION, "feed": FEED, "adjustment": ADJUSTMENT,
                    "start": iso_z(start)}
        actual = {name: info[name] for name in expected}
        if actual != expected:
            raise FatalError(f"store {store} was created with {actual}, this run wants {expected}: "
                             "one store holds one basis; use another folder")
        if asof is not None and asof != info["asof"]:
            raise FatalError(f"store asof is {info['asof']}, --asof {asof} would mix ticker mappings")
        return info["asof"]
    pinned = asof or default_asof
    atomic_write_json(info_path, {
        "layout_version": LAYOUT_VERSION, "vendor": "alpaca", "feed": FEED, "adjustment": ADJUSTMENT,
        "start": iso_z(start), "asof": pinned, "timestamps": "UTC, start of bar",
        "note": "raw traded prices; apply corporate_actions yourself", "created": iso_z(dt.datetime.now(UTC))})
    return pinned


def load_credentials(keys_db: Optional[str], account_id: Optional[int]) -> tuple[str, str]:
    if keys_db is None:
        try:
            return os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
        except KeyError as error:
            raise FatalError(f"no --keys-db and environment variable {error} is not set") from error
    if account_id is None:
        raise FatalError("--keys-db needs --account-id")
    uri = "file:" + Path(keys_db).expanduser().as_posix() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("select key, value_str, value_json from accountsetting "
                                  "where account_id = ? and key in ('api_key', 'api_secret')",
                                  (account_id,)).fetchall()
    finally:
        connection.close()
    found = {}
    for key, value_str, value_json in rows:
        value = value_str if value_str not in (None, "") else json.loads(value_json) if value_json else None
        if value:
            found[key] = str(value)
    if set(found) != {"api_key", "api_secret"}:
        raise FatalError(f"account {account_id} in {keys_db} has no api_key/api_secret")
    return found["api_key"], found["api_secret"]


def read_symbols(path: Path) -> list:
    symbols, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if not symbols:
        raise FatalError(f"{path} lists no symbols")
    return symbols


def parse_day(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--symbols-file", required=True, type=Path, help="one symbol per line, in priority order")
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD (UTC midnight)")
    parser.add_argument("--end", help="first day NOT fetched, YYYY-MM-DD; default: today (UTC), i.e. through yesterday")
    parser.add_argument("--asof", help="ticker-mapping date pinned in the store; default at creation: the day before --end")
    parser.add_argument("--phases", default="corporate_actions,daily,intraday",
                        help="comma list out of corporate_actions, daily, intraday (run in that fixed order)")
    parser.add_argument("--rpm", type=float, default=60.0, help="request ceiling per minute")
    parser.add_argument("--min-remaining", type=int, default=60,
                        help="pause until the vendor's window resets when fewer calls than this remain")
    parser.add_argument("--max-attempts", type=int, default=8, help="attempts per request")
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--max-gap-days", type=int, required=True,
                        help="store an intraday year missing at most this many traded days (listed in the manifest)")
    parser.add_argument("--retry-refused", action="store_true", help="fetch chunks again that an earlier run refused")
    parser.add_argument("--keys-db")
    parser.add_argument("--account-id", type=int)
    args = parser.parse_args(argv)

    try:
        start = parse_day(args.start)
        today = dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end_exclusive = parse_day(args.end) if args.end else today
        if end_exclusive > today:
            raise FatalError("--end is in the future: the free plan serves no bars from the last 15 minutes, "
                             "and an unfinished day would be stored as if complete")
        if start >= end_exclusive:
            raise FatalError("--start must be before --end")
        phases = [name.strip() for name in args.phases.split(",") if name.strip()]
        unknown = sorted(set(phases) - {"corporate_actions", "daily", "intraday"})
        if unknown:
            raise FatalError(f"unknown phases {unknown}")
        symbols = read_symbols(args.symbols_file)
        key_id, secret = load_credentials(args.keys_db, args.account_id)
        args.store.mkdir(parents=True, exist_ok=True)
        default_asof = (end_exclusive - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        asof = open_store(args.store, start, args.asof, default_asof)
        removed = sweep_temp_files(args.store)
        transport = Transport(key_id, secret, rpm=args.rpm, max_attempts=args.max_attempts,
                              min_remaining=args.min_remaining)
        run = Downloader(args.store, transport, start=start, end_exclusive=end_exclusive, asof=asof,
                         retry_refused=args.retry_refused,
                         max_consecutive_failures=args.max_consecutive_failures, max_gap_days=args.max_gap_days)
        log(f"store {args.store} | {len(symbols)} symbols | {iso_z(start)} .. {iso_z(end_exclusive)} (exclusive) | "
            f"asof {asof} | phases {phases} | rpm {args.rpm:g} | swept {removed} temp files | pid {os.getpid()}")
        try:
            if "corporate_actions" in phases:
                run.corporate_actions()
                run.write_progress("corporate_actions", "done")
                log(f"corporate actions done | {run.counts}")
            if "daily" in phases:
                for index, symbol in enumerate(symbols, 1):
                    run.daily(symbol)
                    if index % 50 == 0 or index == len(symbols):
                        run.write_progress("daily", f"{index}/{len(symbols)} {symbol}")
                        log(f"daily {index}/{len(symbols)} | {run.counts} | requests {transport.requests} "
                            f"| 429s {transport.rate_limited}")
            if "intraday" in phases:
                for index, symbol in enumerate(symbols, 1):
                    run.intraday(symbol)
                    run.write_progress("intraday", f"{index}/{len(symbols)} {symbol}")
                    if index % 10 == 0 or index == len(symbols):
                        log(f"intraday {index}/{len(symbols)} | {run.counts} | requests {transport.requests} "
                            f"| {transport.bytes / 1e6:.0f} MB | 429s {transport.rate_limited}")
        finally:
            run.write_progress("stopped", "see the log")
        log(f"DONE | {run.counts} | requests {transport.requests} | retries {transport.retries} "
            f"| 429s {transport.rate_limited} | {transport.bytes / 1e6:.0f} MB")
        return 1 if run.counts["failed"] else 0
    except FatalError as error:
        log(f"FATAL {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
