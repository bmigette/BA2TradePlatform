"""Live RAW-bar client for the Alpaca history top-up (plan 2026-10-24-history-source-task11, Task 2).

The same request shape as ``tools/alpaca_history_download.fetch_bars`` (that module lives in ``tools/``, which the
packages cannot import): ``adjustment=raw``, ``feed=sip``, ascending pages, ``end`` one second short of the exclusive
boundary. The bars are converted with the build's own ``to_native_daily`` / ``to_native_5min`` (one tz / regular-session
rule), so a live row and a built row cannot differ in layout.

Every failure is loud: ``AlpacaLiveFetchError``. An EMPTY answer is returned as an empty frame (the caller knows which
sessions it expected and judges completeness); a failure is never turned into an empty frame.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from ba2_providers.ohlcv.alpaca_history import build

DATA_HOST = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
FEED = "sip"
ADJUSTMENT = "raw"
PAGE_LIMIT = 10000
MAX_ATTEMPTS = 3
BAR_COLUMNS = ["t", "o", "h", "l", "c", "v"]
NATIVE_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


class AlpacaLiveFetchError(RuntimeError):
    """A live Alpaca bar request failed or answered something unusable. Nothing was written."""


def alpaca_symbol(symbol: str) -> str:
    """Our class-share spelling BRK-B is BRK.B at Alpaca (the downloader's rule)."""
    return symbol.strip().upper().replace("-", ".")


class UrllibTransport:
    """``get(url, headers) -> (status, body bytes)``; the only code that touches the network."""

    def get(self, url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300]
        except (urllib.error.URLError, OSError, EOFError) as e:
            raise AlpacaLiveFetchError(f"network error: {type(e).__name__}: {e}") from e


def _iso_z(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ny_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=NY)


class AlpacaLiveBars:
    def __init__(self, api_key: str, api_secret: str, *, transport: Optional[Any] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key or not api_secret:
            raise AlpacaLiveFetchError("Alpaca api_key / api_secret missing")
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret,
                         "Accept-Encoding": "gzip"}
        self._transport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep

    def _get(self, params: Dict[str, Any]) -> dict:
        url = f"{DATA_HOST}{BARS_PATH}?{urllib.parse.urlencode(params)}"
        last = "no attempt"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            status, body = self._transport.get(url, self._headers)
            if status == 200:
                try:
                    payload = json.loads(body)
                except ValueError as e:
                    raise AlpacaLiveFetchError(f"malformed JSON from Alpaca: {e}") from e
                if not isinstance(payload, dict) or "bars" not in payload or "next_page_token" not in payload:
                    raise AlpacaLiveFetchError(f"unexpected response shape from Alpaca: {str(payload)[:120]}")
                return payload
            text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
            last = f"HTTP {status}: {text[:200]}"
            if status == 429 or status >= 500:
                if attempt < MAX_ATTEMPTS:
                    self._sleep(2.0 ** attempt)
                continue
            raise AlpacaLiveFetchError(last)                 # other 4xx: retrying cannot help
        raise AlpacaLiveFetchError(f"retries exhausted after {MAX_ATTEMPTS} attempts: {last}")

    def _bars(self, symbol: str, timeframe: str, start: dt.datetime, end_exclusive: dt.datetime) -> pd.DataFrame:
        sym = alpaca_symbol(symbol)
        params = {"symbols": sym, "timeframe": timeframe, "start": _iso_z(start),
                  "end": _iso_z(end_exclusive - dt.timedelta(seconds=1)),
                  "adjustment": ADJUSTMENT, "feed": FEED, "limit": PAGE_LIMIT, "sort": "asc"}
        bars: list = []
        seen: set = set()
        token = None
        while True:
            q = dict(params)
            if token:
                q["page_token"] = token
            payload = self._get(q)
            by_symbol = payload["bars"] or {}
            unexpected = sorted(set(by_symbol) - {sym})
            if unexpected:
                raise AlpacaLiveFetchError(f"Alpaca answered unexpected symbols {unexpected[:5]} for {sym}")
            bars.extend(by_symbol[sym] if sym in by_symbol else [])
            token = payload["next_page_token"]
            if not token:
                break
            if token in seen:
                raise AlpacaLiveFetchError(f"pagination loop at token {token}")
            seen.add(token)
        if not bars:
            return pd.DataFrame(columns=BAR_COLUMNS)
        frame = pd.DataFrame(bars)
        missing = [c for c in BAR_COLUMNS if c not in frame.columns]
        if missing:
            raise AlpacaLiveFetchError(f"{sym}: bar columns {missing} missing")
        frame = frame[BAR_COLUMNS].copy()
        if frame.isna().any().any():
            raise AlpacaLiveFetchError(f"{sym}: {int(frame.isna().any(axis=1).sum())} bar(s) with null values")
        frame["t"] = pd.to_datetime(frame["t"], utc=True)
        prices = frame[["o", "h", "l", "c"]].astype("float64")
        if (prices <= 0).any().any() or (prices["h"] < prices["l"]).any() or (frame["v"] < 0).any():
            raise AlpacaLiveFetchError(f"{sym}: bars with non-positive prices, high below low or negative volume")
        return frame

    def _native(self, symbol: str, timeframe: str, start: dt.date, end_exclusive: dt.date, convert, what: str) -> pd.DataFrame:
        frame = self._bars(symbol, timeframe, _ny_midnight(start), _ny_midnight(end_exclusive))
        if frame.empty:
            return pd.DataFrame(columns=NATIVE_COLUMNS)
        try:
            return convert(frame, f"{symbol} live {what}")
        except build.BuildError as e:
            raise AlpacaLiveFetchError(str(e)) from e

    def daily(self, symbol: str, start: dt.date, end_exclusive: dt.date) -> pd.DataFrame:
        """RAW daily bars of the sessions in [start, end_exclusive) in the native layout (Date NY-naive, OHLCV)."""
        return self._native(symbol, "1Day", start, end_exclusive, build.to_native_daily, "daily")

    def intraday_5min(self, symbol: str, start: dt.date, end_exclusive: dt.date) -> pd.DataFrame:
        """RAW regular-session 5-minute bars of the sessions in [start, end_exclusive), native layout."""
        return self._native(symbol, "5Min", start, end_exclusive, build.to_native_5min, "5min")
