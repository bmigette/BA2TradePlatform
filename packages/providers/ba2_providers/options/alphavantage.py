"""Alpha Vantage ``HISTORICAL_OPTIONS`` -> the BA2 option parquet store.

A SECOND, SEPARATE store beside ThetaData's (``ThetaDataOptionsProvider/``, never touched here):
``CACHE_FOLDER/AlphaVantageOptionsProvider/``. A backtest reads either through
``options_store`` (``--option-history-source {thetadata,alphavantage}``).

WHAT AV SERVES. ``function=HISTORICAL_OPTIONS&symbol=X&date=YYYY-MM-DD`` is ONE end-of-day chain
(all expiries) per call: contractID, expiration, strike, type, last, mark, bid, bid_size, ask,
ask_size, volume, open_interest, implied_volatility, delta, gamma, theta, vega, rho (all strings).
There is NO open/high/low. The download unit is therefore (symbol, DATE), the opposite of the
ThetaData/TastyTrade stores whose unit is (underlying, EXPIRY).

LAYOUT (the READ layout is exactly the ThetaData one, so ``ParquetOptionsProvider`` and
``OptionHistoryParquetStore.read_underlying`` need no change)::

    <root>/AAPL/exp=2023-01-20/AAPL_2023-01-20_1d.parquet + _manifest.json     <- what a backtest reads
    <root>/AAPL/_days/2022-03-01.parquet                                         <- staged, not yet compacted
    <root>/AAPL/_ledger.jsonl                                                    <- one line per (date) fetched

Crash safety. A day is downloaded -> mapped -> written to ``_days/`` (temp + ``os.replace``) -> THEN
one line is appended to the ledger. The ledger is the resume authority: a date in it (``ok`` or
``empty``) is never fetched again; a date not in it is fetched again (a killed run loses at most the
days in flight). ``compact`` merges the staged days into the per-expiry partitions (dedupe on
(occ_symbol, bar_date), atomic rewrite, manifest last) and only then deletes the staged files, so a kill
in the middle repeats an idempotent merge.

MAPPING (decisions, all measured on real chains 2026-11-07, see docs/REPRODUCE-BACKTESTS.md s4):

* ``close`` = AV ``last`` ONLY on a day the contract traded (``volume > 0`` and ``last > 0``), else None
  -- ThetaData's rule (``thetadata._traded``: close > 0 iff volume > 0). AV carries a STALE ``last`` on
  no-trade rows (1,841 of 9,496 NVDA 2024-05-31 contracts had volume 0 and last > 0): taking it would
  date an old print today. ``mark`` (the vendor mid) is kept in its own column, never in ``close``; the
  backtest marks a no-trade row from the real bid/ask (``contract()``), exactly as for ThetaData.
* ``open = high = low = close``. AV has no OHLC. The backtest fills ``next_bar_open`` at the fill
  day's ``open`` and returns NO fill when it is None, so None would make the store untradeable. The
  fill reference is therefore the fill day's LAST print instead of its first: it sits on the fill day
  (never the decision day), so it is not look-ahead for decisions, but it is a coarser reference than a
  true open. Every manifest says ``ohlc: "close-only"``.
* ``bid``/``ask`` = the vendor NBBO through ``thetadata._quote`` (ask <= 0 -> no quote; bid 0 with an ask
  is a real "nobody bids"). ``open_interest``, ``volume`` as Int64. ``iv`` through
  ``thetadata._clean_iv`` (0 or absurd -> None). A row with neither a trade nor a quote is dropped.
* Extra columns the ThetaData tree does not have, KEPT for the record (the reader ignores them and
  inverts its own greeks): ``mark, bid_size, ask_size, delta, gamma, theta, vega, rho``.
* STRIKES ARE AS-TRADED, like ThetaData: NVDA 2024-05-31 (10:1 split on 2024-06-10) lists strikes
  1005..1165 around a ~$1,100 spot. The Part E split-basis machinery therefore applies unchanged.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)
from ba2_common.logger import logger

from .parquet_store import BARS_INTERVAL, MANIFEST_NAME, OptionHistoryParquetStore, _tmp_name
from .thetadata import _clean_iv, _num, _quote, _traded

#: Sub-directory of CACHE_FOLDER (named for the provider class like its siblings).
PROVIDER_DIR = "AlphaVantageOptionsProvider"
API_BASE_URL = "https://www.alphavantage.co/query"
FUNCTION = "HISTORICAL_OPTIONS"
#: AV rejects dates before this (measured); the store's own floor is the owner's download start.
AV_EARLIEST_DATE = date(2008, 1, 1)
STORE_FLOOR = date(2020, 1, 1)

STAGE_DIR = "_days"
LEDGER_NAME = "_ledger.jsonl"
MANIFEST_SOURCE = "alphavantage"

#: The columns of the ThetaData parquet tree, then what only AV has.
BASE_COLUMNS: Sequence[str] = OptionHistoryParquetStore.COLUMNS
EXTRA_COLUMNS: Sequence[str] = ("mark", "bid_size", "ask_size", "delta", "gamma", "theta", "vega", "rho")
COLUMNS: Sequence[str] = tuple(BASE_COLUMNS) + tuple(EXTRA_COLUMNS)

_OCC = re.compile(r"^(?P<root>[A-Z][A-Z0-9]{0,5}?)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class AlphaVantageOptionsError(RuntimeError):
    """A response that is not a chain (error body, missing ``data``)."""


class AlphaVantageThrottled(AlphaVantageOptionsError):
    """AV said rate limit / premium-only (an ``Information`` body). Retry later; never recorded."""


class AlphaVantageOptionsProvider(OptionsDataProviderInterface):
    """Registry entry for the floor lookup. AV is a chain-BY-DATE vendor and is fed by
    ``tools/download_av_option_chains.py``; the contract-list interface does not apply."""

    name = "alphavantage"

    def history_floor(self) -> date:
        return STORE_FLOOR

    def discover_contracts(self, underlying, *, expiry_gte, expiry_lte, strike_min=None,
                           strike_max=None, max_contracts=None) -> List[OptionContractMeta]:
        raise NotImplementedError("alphavantage serves one chain per (symbol, date); "
                                  "use tools/download_av_option_chains.py")

    def fetch_eod_bars(self, contracts, *, start, end) -> Iterator[OptionEodBar]:
        raise NotImplementedError("alphavantage serves one chain per (symbol, date); "
                                  "use tools/download_av_option_chains.py")


# ---------------------------------------------------------------------------- HTTP
def redact(text: str, key: Optional[str]) -> str:
    """``text`` with the API key (and any ``apikey=...`` query value) removed: a requests
    exception message embeds the full URL, key included."""
    out = re.sub(r"(?i)(apikey=)[^&\s'\")]+", r"\1***", str(text))
    if key:
        out = out.replace(key, "***")
    return out


def fetch_chain_payload(symbol: str, day: date, *, api_key: str, session: Any = None,
                        timeout: float = 90.0) -> List[Dict[str, Any]]:
    """The raw contract rows of ``symbol``'s chain on ``day`` (``[]`` = AV serves an empty chain).

    Raises ``AlphaVantageThrottled`` on a rate-limit / premium notice and ``AlphaVantageOptionsError`` on
    any other non-chain body or HTTP failure. The key never appears in a message (``redact``)."""
    import requests
    http = session or requests
    params = {"function": FUNCTION, "symbol": symbol, "date": day.isoformat(), "apikey": api_key}
    try:
        resp = http.get(API_BASE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:  # noqa: BLE001 -- re-raised as a typed error with the key removed
        raise AlphaVantageOptionsError(f"{type(e).__name__}: {redact(e, api_key)[:300]}") from None
    return parse_payload(body, symbol, day)


def parse_payload(body: Any, symbol: str, day: date) -> List[Dict[str, Any]]:
    if not isinstance(body, dict):
        raise AlphaVantageOptionsError(f"unexpected body type {type(body).__name__}")
    info = body.get("Information") or body.get("Note")
    if info:
        text = str(info)
        if any(k in text.lower() for k in ("rate limit", "per minute", "per day", "premium", "call frequency")):
            raise AlphaVantageThrottled(text[:200])
        raise AlphaVantageOptionsError(f"Information: {text[:200]}")
    if "Error Message" in body:
        raise AlphaVantageOptionsError(f"Error Message: {str(body['Error Message'])[:200]}")
    data = body.get("data")
    if data is None:
        raise AlphaVantageOptionsError(
            f"no 'data' in the response for {symbol} {day} (keys {sorted(body)[:6]}, "
            f"message {str(body.get('message'))[:80]!r})")
    if not isinstance(data, list):
        raise AlphaVantageOptionsError(f"'data' is {type(data).__name__}, not a list")
    return data


# ---------------------------------------------------------------------------- mapping
@dataclass
class ChainStats:
    rows_in: int = 0
    rows_out: int = 0
    dropped_unparsable: int = 0
    dropped_priceless: int = 0
    dropped_wrong_underlying: int = 0
    dropped_wrong_date: int = 0


def map_chain(symbol: str, day: date, rows: Iterable[Dict[str, Any]]):
    """AV rows -> ``(DataFrame with COLUMNS, ChainStats)``. Pure; see the module docstring for the rules."""
    import pandas as pd

    symbol = symbol.upper()
    stats = ChainStats()
    out: List[Dict[str, Any]] = []
    for r in rows:
        stats.rows_in += 1
        occ = str(r.get("contractID") or "").strip().upper()
        m = _OCC.match(occ)
        if not m:
            stats.dropped_unparsable += 1
            continue
        if m["root"] != symbol:
            # An adjusted/odd root (post-split "AAPL1") is a different deliverable: not this symbol's chain.
            stats.dropped_wrong_underlying += 1
            continue
        if str(r.get("date") or day.isoformat())[:10] != day.isoformat():
            stats.dropped_wrong_date += 1
            continue
        expiry = date(2000 + int(m["exp"][:2]), int(m["exp"][2:4]), int(m["exp"][4:6]))
        strike = int(m["strike"]) / 1000.0
        last, vol = _num(r.get("last")), _num(r.get("volume"))
        close = last if (vol is not None and vol > 0 and _traded(last)) else None
        bid, ask = _quote(_num(r.get("bid")), _num(r.get("ask")))
        if close is None and bid is None and ask is None:
            stats.dropped_priceless += 1
            continue
        oi = _num(r.get("open_interest"))
        out.append({
            "occ_symbol": occ, "underlying": symbol,
            "option_type": "call" if m["cp"] == "C" else "put",
            "strike": strike, "expiry": expiry.isoformat(), "bar_date": day.isoformat(),
            "open": close, "high": close, "low": close, "close": close,     # close-only vendor
            "volume": None if vol is None else int(vol),
            "bid": bid, "ask": ask, "iv": _clean_iv(r.get("implied_volatility")),
            "open_interest": None if oi is None else int(oi),
            "mark": _num(r.get("mark")),
            "bid_size": _int(r.get("bid_size")), "ask_size": _int(r.get("ask_size")),
            "delta": _num(r.get("delta")), "gamma": _num(r.get("gamma")), "theta": _num(r.get("theta")),
            "vega": _num(r.get("vega")), "rho": _num(r.get("rho")),
        })
    stats.rows_out = len(out)
    return _typed(pd.DataFrame(out, columns=list(COLUMNS))), stats


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return None if n is None else int(n)


def _typed(df):
    for col in ("open", "high", "low", "close", "strike", "bid", "ask", "iv", "mark",
                "delta", "gamma", "theta", "vega", "rho"):
        df[col] = df[col].astype("float64")
    for col in ("volume", "open_interest", "bid_size", "ask_size"):
        df[col] = df[col].astype("Int64")
    for col in ("occ_symbol", "underlying", "option_type", "expiry", "bar_date"):
        df[col] = df[col].astype("string")
    return df


# ---------------------------------------------------------------------------- store
def default_root() -> str:
    import ba2_common.config as _cfg          # at CALL time (tests rebind CACHE_FOLDER)
    return os.path.join(_cfg.CACHE_FOLDER, PROVIDER_DIR)


def _atomic_write(path: str, write: Callable[[str], None]) -> None:
    tmp = _tmp_name(path)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover
                pass


class AvOptionStore:
    """Per-(symbol, date) ledger + staged days + compaction into the ThetaData-shaped partitions."""

    def __init__(self, root: Optional[str] = None):
        self._root = root
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        return self._root if self._root is not None else default_root()

    def symbol_dir(self, symbol: str) -> str:
        return os.path.join(self.root, symbol.upper())

    def _ledger_path(self, symbol: str) -> str:
        return os.path.join(self.symbol_dir(symbol), LEDGER_NAME)

    def _stage_dir(self, symbol: str) -> str:
        return os.path.join(self.symbol_dir(symbol), STAGE_DIR)

    def _stage_path(self, symbol: str, day: date) -> str:
        return os.path.join(self._stage_dir(symbol), f"{day.isoformat()}.parquet")

    # -- ledger ---------------------------------------------------------
    def ledger(self, symbol: str) -> Dict[date, dict]:
        """``{date: entry}`` of every recorded fetch (last line wins). A torn last line is ignored:
        that date is simply fetched again."""
        out: Dict[date, dict] = {}
        try:
            with open(self._ledger_path(symbol), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        out[date.fromisoformat(e["d"])] = e
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        return out

    def _append_ledger(self, symbol: str, entry: dict) -> None:
        os.makedirs(self.symbol_dir(symbol), exist_ok=True)
        with self._lock, open(self._ledger_path(symbol), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def pending_days(self, symbol: str, days: Sequence[date]) -> List[date]:
        done = self.ledger(symbol)
        return [d for d in days if d not in done]

    # -- recording a day ------------------------------------------------
    def record_day(self, symbol: str, day: date, df, stats: Optional[ChainStats] = None,
                   *, now: Optional[datetime] = None) -> dict:
        """Persist one fetched day (rows, or a recorded EMPTY) and mark it done. Stage first, ledger last."""
        symbol = symbol.upper()
        os.makedirs(self._stage_dir(symbol), exist_ok=True)
        n = len(df)
        if n:
            _atomic_write(self._stage_path(symbol, day), lambda p: df.to_parquet(p, index=False, compression="zstd"))
        entry = {"d": day.isoformat(), "s": "ok" if n else "empty", "rows": n,
                 "at": (now or datetime.now(timezone.utc)).isoformat()}
        if stats is not None:
            entry["in"] = stats.rows_in
            entry["dropped"] = (stats.dropped_unparsable + stats.dropped_priceless +
                                stats.dropped_wrong_underlying + stats.dropped_wrong_date)
        self._append_ledger(symbol, entry)
        return entry

    def staged_days(self, symbol: str) -> List[date]:
        d = self._stage_dir(symbol)
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(os.listdir(d)):
            if name.endswith(".parquet"):
                try:
                    out.append(date.fromisoformat(name[:-len(".parquet")]))
                except ValueError:
                    pass
        return out

    # -- compaction -----------------------------------------------------
    def compact(self, symbol: str) -> int:
        """Merge every staged day into the per-expiry partitions; returns the partitions written.
        Idempotent: the merge dedupes on (occ_symbol, bar_date), staged files are removed last."""
        import pandas as pd

        symbol = symbol.upper()
        days = self.staged_days(symbol)
        if not days:
            return 0
        new = pd.concat((pd.read_parquet(self._stage_path(symbol, d)) for d in days), ignore_index=True)
        ledger = self.ledger(symbol)
        covered = sorted(ledger) or days
        written = 0
        reader = OptionHistoryParquetStore(self.root)
        for expiry_iso, grp in new.groupby("expiry", sort=True):
            expiry = date.fromisoformat(str(expiry_iso))
            path = reader.bars_path(symbol, expiry)
            if os.path.exists(path):
                old = pd.read_parquet(path)
                grp = pd.concat([old, grp], ignore_index=True)
            grp = (grp.drop_duplicates(["occ_symbol", "bar_date"], keep="last")
                      .sort_values(["occ_symbol", "bar_date"], kind="mergesort").reset_index(drop=True))
            grp = _typed(grp.reindex(columns=list(COLUMNS)))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _atomic_write(path, lambda p, g=grp: g.to_parquet(p, index=False, compression="zstd"))
            manifest = {
                "schema_version": OptionHistoryParquetStore.SCHEMA_VERSION, "status": "complete",
                "underlying": symbol, "expiry": expiry.isoformat(),
                "start": covered[0].isoformat(), "end": covered[-1].isoformat(),
                "interval": BARS_INTERVAL, "rows": int(len(grp)), "contracts": int(grp["occ_symbol"].nunique()),
                "empty_contracts": [], "written_at": datetime.now(timezone.utc).isoformat(),
                "source": MANIFEST_SOURCE, "ohlc": "close-only", "close": "last-if-traded",
            }
            mpath = reader.manifest_path(symbol, expiry)
            _atomic_write(mpath, lambda p, m=manifest: _dump_json(p, m))
            written += 1
        for d in days:
            try:
                os.remove(self._stage_path(symbol, d))
            except OSError:  # pragma: no cover
                pass
        return written

    # -- inspection -----------------------------------------------------
    def disk_bytes(self, symbol: Optional[str] = None) -> int:
        base = self.symbol_dir(symbol) if symbol else self.root
        total = 0
        for dirpath, _d, files in os.walk(base):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:  # pragma: no cover
                    pass
        return total

    def coverage(self, symbol: str, days: Sequence[date]) -> Tuple[int, int]:
        """``(recorded, wanted)`` of ``days`` for ``symbol`` (read-only; prewarm reports it)."""
        done = self.ledger(symbol)
        return sum(1 for d in days if d in done), len(days)


def _dump_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True)
