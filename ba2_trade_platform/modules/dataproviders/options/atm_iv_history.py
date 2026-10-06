"""Live ATM implied-volatility HISTORY, derived from Alpaca daily option bars.

WHY. ``get_iv_rank`` is a percentile of the current ATM IV against a trailing year of ATM IVs.
Live used to have ~6 samples (the 16:30 ``option_iv_snapshot`` job) of a DIFFERENT statistic
(Alpaca snapshot IV, calls+puts nearest strike) than the backtest ranks (ThetaData bars inverted
with the backtest's own Black-Scholes, calls only, |delta| nearest 0.5). The feasibility study
(``iv_history_study``) showed that inverting Alpaca DAILY BAR CLOSES with the backtest's own
function and selection reproduces the backtest statistic: median diff 0.00 vp, p95 0.9 vp, rank<25
flag agreement 99.4%. This module is that derivation, as a lazily-filled disk cache.

THE STATISTIC ("rule C", must stay in lockstep with ``ParquetOptionsProvider._compute_atm_iv``)
  * calls only, 20..45 calendar days to expiry (``DTE_MIN``/``DTE_MAX``);
  * candidates per session = the ``N_STRIKES`` (24; see its comment) listed strikes nearest spot,
    per expiry;
  * price = the session's own daily bar CLOSE (no carry-forward: a contract with no bar that
    session is not a candidate);
  * IV by Black-Scholes bisection (``bs_inversion``, a verified copy of the backtest function),
    T = (expiry - session).days / 365, r = FRED DGS3MO as-of the session, spot = FMP close x the
    as-traded split factor (option strikes are as traded, FMP closes are split-adjusted);
  * pick the candidate with the smallest key (| |delta| - 0.5 |, expiry, strike).

NEVER FABRICATED. A session with no usable bar is a TOMBSTONE row (iv = NaN, with a reason), not
a carried-forward value. A missing FRED cache or missing spot makes the series ``unavailable``;
there is no default rate or price. A bar volume below ``MIN_BAR_VOLUME`` is a ``low_volume``
tombstone (a one-lot close is not a price).

LAZY FILL (the cache fills itself; nothing depends on a tool). A request computes the sessions
missing up to the LAST COMPLETED session (never today's incomplete bar). The caller's thread is
capped (``max_api_calls``/``max_seconds``: enough for a warm cache's one new day); a cold
backfill is handed to ONE daemon worker thread behind a shared token bucket and the caller gets
the partial series with status ``filling``. One fill per (cache, symbol) at a time.

STORE: ``<CACHE_FOLDER>/AtmIvHistory/<SYMBOL>.parquet`` (one row per session, atomic replace under
a per-file lock) + ``_contracts/<SYMBOL>.{parquet,json}`` (contract listings: the inactive set is
fetched once and extended; the active set is refreshed daily).

LIVE-ONLY: nothing under ``packages/`` or ``testplatform/`` imports this module.
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ba2_common.logger import logger

from .bs_inversion import iv_and_delta

# ---------------------------------------------------------------------------------------------
# Method definition. Changing ANY of these changes the statistic: bump METHOD_VERSION so stored
# rows are recomputed.
# ---------------------------------------------------------------------------------------------
METHOD_VERSION = "ruleC-barclose-v1"
PROVENANCE_DERIVED = "derived_alpaca_bars"
PROVENANCE_SNAPSHOT = "snapshot"
DTE_MIN = 20
DTE_MAX = 45
#: Strikes per expiry admitted as candidates (nearest spot). The feasibility study NAMED rule C
#: "6 nearest strikes" but its range fetch silently admitted every contract that had been among
#: the 6 nearest on ANY session of a batch, i.e. an effective set of ~20+ strikes. Measured on
#: the study's replay fixture, a literal 6 loses accuracy against the backtest (p95 |diff| 5.4 vp
#: vs 1.8 vp; contract agreement 82.6% vs 86.6%) because the |delta|-0.5 strike of a high-vol
#: name sits several strikes above spot. 24 reproduces the study's effective set on the fixture
#: and costs ~1 extra bar call per symbol-year (bars are range-fetched per contract).
N_STRIKES = 24
MIN_BAR_VOLUME = 5
CHUNK_SESSIONS = 60
BARS_BATCH = 200                      # symbols per option-bars call (AlpacaOptionsProvider's)
HISTORY_FLOOR = date(2024, 1, 18)     # Alpaca option history floor (AlpacaOptionsProvider)
CALLS_PER_MINUTE = 150
INACTIVE_TAIL_REFRESH_DAYS = 7
FAIL_COOLDOWN_SECONDS = 1800
MAX_CONSECUTIVE_FAILURES = 3

STATUS_COMPLETE = "complete"
STATUS_FILLING = "filling"
STATUS_UNAVAILABLE = "unavailable"

STORE_COLUMNS = ["session_date", "iv", "reason", "occ", "strike", "expiry", "dte", "delta",
                 "bar_close", "bar_volume", "spot", "rate", "provenance", "method_version",
                 "computed_at"]


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------
class AtmIvUnavailable(RuntimeError):
    """A prerequisite (risk-free rate, spot, credentials) cannot be stated. Never defaulted."""


class SpotUnavailable(AtmIvUnavailable):
    pass


class BudgetExhausted(RuntimeError):
    """The caller's API-call / time / rate budget does not allow the next call."""


class RateLimited(RuntimeError):
    """The data API answered 429."""


class ApiFailure(RuntimeError):
    """The data API failed in a way retrying immediately will not fix."""


# ---------------------------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------------------------
@dataclass
class AtmIvSeries:
    """What ``get_atm_iv_series`` returns.

    ``values`` maps session date -> ATM IV (fraction) for every session with a DERIVED value,
    window start .. ``end_session`` inclusive. ``covered``/``expected`` is the coverage n/m
    (sessions with a value / sessions in the window at or after the history floor).
    """
    symbol: str
    end_session: date
    status: str
    values: Dict[date, float] = field(default_factory=dict)
    covered: int = 0
    expected: int = 0
    tombstones: int = 0
    provenance: Dict[str, int] = field(default_factory=dict)
    reason: Optional[str] = None
    window_start: Optional[date] = None
    #: sessions neither valued nor tombstoned (still to fill, or no spot to compute them)
    unresolved: int = 0

    @property
    def coverage(self) -> str:
        return f"{self.covered}/{self.expected}"

    @property
    def is_complete(self) -> bool:
        return self.status == STATUS_COMPLETE


# ---------------------------------------------------------------------------------------------
# Pure selection (shared by the fill and by the parity tests)
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    occ: str
    strike: float
    expiry: date
    close: float
    volume: Optional[int] = None


@dataclass(frozen=True)
class Selection:
    key: Tuple[float, int, float]
    iv: float
    delta: float
    candidate: Candidate
    dte: int


def select_atm(session: date, spot: float, rate: float,
               candidates: Iterable[Candidate]) -> Optional[Selection]:
    """The candidate the backtest's ``_compute_atm_iv`` would pick, or None.

    Same arithmetic: T = (expiry - session).days / 365; key = (| |delta| - 0.5 |, expiry ordinal,
    strike); the strictly smallest key wins (first seen on an exact tie, as the backtest loop).
    """
    best: Optional[Selection] = None
    for c in candidates:
        T = (c.expiry - session).days / 365.0
        iv, delta = iv_and_delta(float(c.close), float(spot), float(c.strike), T, float(rate), True)
        if iv is None or delta is None:
            continue
        key = (abs(abs(delta) - 0.5), c.expiry.toordinal(), float(c.strike))
        if best is None or key < best.key:
            best = Selection(key, float(iv), float(delta), c, (c.expiry - session).days)
    return best


def nearest_strikes(strikes: np.ndarray, spot: float, n: int = N_STRIKES) -> np.ndarray:
    """The ``n`` listed strikes nearest ``spot`` (study's exact expression)."""
    ks = np.sort(np.unique(np.asarray(strikes, dtype=float)))
    return ks[np.argsort(np.abs(ks - spot))[:n]]


# ---------------------------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------------------------
def _ny_tz():
    from ba2_common.core.market_calendar import NY_TZ
    return NY_TZ


def last_completed_session(now: Optional[datetime] = None) -> date:
    """The latest NYSE session strictly BEFORE today's New York date.

    Today's bar is never final intraday, and a bar polled just after the close is not reliably
    published yet; the conservative rule (yesterday's session at the earliest) keeps a tombstone
    from ever being written for a bar that simply had not landed.
    """
    from ba2_common.core import market_calendar
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_ny_tz()).date()
    days = market_calendar.regular_session_dates(today - timedelta(days=12), today - timedelta(days=1))
    if not days:
        raise AtmIvUnavailable(f"no NYSE session found before {today}")
    return days[-1]


def window_sessions(end_session: date, lookback_days: int) -> List[date]:
    """NYSE sessions in [end_session - lookback_days, end_session], not before the history floor."""
    from ba2_common.core import market_calendar
    start = max(end_session - timedelta(days=int(lookback_days)), HISTORY_FLOOR)
    return market_calendar.regular_session_dates(start, end_session)


# ---------------------------------------------------------------------------------------------
# Token bucket + budget
# ---------------------------------------------------------------------------------------------
class TokenBucket:
    """Shared API rate limiter (default ~150 calls/minute, small burst)."""

    def __init__(self, per_minute: float = CALLS_PER_MINUTE, burst: float = 20.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.rate = per_minute / 60.0
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self):
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, blocking: bool = True) -> bool:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self.rate
            if not blocking:
                return False
            self._sleep(min(max(wait, 0.001), 5.0))


#: ONE bucket for the whole process: the data API limit is per key, not per provider object.
_SHARED_BUCKET = TokenBucket()


class _Budget:
    """Caps a unit of work: API calls, wall time, and the shared token bucket."""

    def __init__(self, max_calls: Optional[int], max_seconds: Optional[float],
                 bucket: TokenBucket, blocking: bool):
        self.max_calls = max_calls
        self.deadline = None if max_seconds is None else time.monotonic() + max_seconds
        self.bucket = bucket
        self.blocking = blocking
        self.used = 0

    def can_afford(self, n: int) -> bool:
        return self.max_calls is None or self.used + n <= self.max_calls

    def require(self, n: int) -> None:
        if not self.can_afford(n):
            raise BudgetExhausted(f"needs {n} API call(s), {self.max_calls - self.used} left")

    def spend(self, n: int = 1) -> None:
        for _ in range(n):
            if not self.can_afford(1):
                raise BudgetExhausted("API call budget exhausted")
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise BudgetExhausted("time budget exhausted")
            if not self.bucket.acquire(blocking=self.blocking):
                raise BudgetExhausted("shared rate bucket empty")
            self.used += 1


# ---------------------------------------------------------------------------------------------
# Default Alpaca-backed clients (reuse AlpacaOptionsProvider's credentials/clients/floor)
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BarRec:
    day: date
    close: float
    volume: Optional[int]


def _is_rate_limited(exc: BaseException) -> bool:
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    if code == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


class AlpacaBarsClient:
    """``fetch(symbols, start, end) -> {occ: [BarRec]}``; RAISES on any failure.

    ``AlpacaOptionsProvider.fetch_eod_bars`` swallows a failed batch (a warning, then the batch is
    simply missing), which here would be indistinguishable from "no bars that day" and would
    write false tombstones. The credentials/client come from that provider; the call is ours.
    """

    def __init__(self, options_provider: Any):
        self._p = options_provider

    def fetch(self, symbols: Sequence[str], start: date, end: date) -> Dict[str, List[BarRec]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from ba2_providers.options.alpaca import _bar_price

        _, dc = self._p._clients()
        req = OptionBarsRequest(
            symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
            end=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc))
        resp = dc.get_option_bars(req)
        out: Dict[str, List[BarRec]] = {}
        for occ, bars in (getattr(resp, "data", {}) or {}).items():
            for b in bars:
                ts = getattr(b, "timestamp", None)
                close = _bar_price(getattr(b, "close", None))
                if ts is None or close is None:
                    continue
                d = ts.astimezone(_ny_tz()).date() if ts.tzinfo else ts.date()
                vol = getattr(b, "volume", None)
                out.setdefault(occ, []).append(BarRec(d, close, None if vol is None else int(vol)))
        return out


class AlpacaContractLister:
    """``list_page(underlying, status, gte, lte, page_token) -> (rows, next_token)``; CALLS only."""

    def __init__(self, options_provider: Any):
        self._p = options_provider

    def list_page(self, underlying: str, status: str, gte: Optional[date], lte: Optional[date],
                  page_token: Optional[str] = None):
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus, ContractType

        tc, _ = self._p._clients()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying.upper()],
            status=AssetStatus.INACTIVE if status == "inactive" else AssetStatus.ACTIVE,
            type=ContractType.CALL, expiration_date_gte=gte, expiration_date_lte=lte,
            limit=10000, page_token=page_token)
        resp = tc.get_option_contracts(req)
        rows = []
        for c in resp.option_contracts or []:
            exp = getattr(c, "expiration_date", None)
            if exp is None or getattr(c, "strike_price", None) is None:
                continue
            rows.append({"occ": c.symbol,
                         "expiry": exp if isinstance(exp, date) else date.fromisoformat(str(exp)[:10]),
                         "strike": float(c.strike_price),
                         "size": int(getattr(c, "size", 100) or 100),
                         "root": str(getattr(c, "root_symbol", "") or "")})
        return rows, getattr(resp, "next_page_token", None)


# ---------------------------------------------------------------------------------------------
# Default spot source: FMP daily close x as-traded split factor
# ---------------------------------------------------------------------------------------------
class FmpSpotSource:
    """``spots(symbol, start, end) -> {session: as-traded close}``; RAISES SpotUnavailable.

    FMP closes are split-adjusted as of their fetch while option strikes are as traded
    (NFLX 2024: close $55 vs a $553 chain). The factor is the platform's own verified machinery
    (``ba2_common.core.split_basis.resolve_symbol_split_basis``); a mixed/unprovable basis REFUSES
    rather than assuming 1.
    """

    def __init__(self, ohlcv_loader: Optional[Callable[[str, date, date], Tuple[pd.DataFrame, Optional[str]]]] = None,
                 split_loader: Optional[Callable[[str], Optional[list]]] = None):
        self._ohlcv_loader = ohlcv_loader or self._default_ohlcv
        self._split_loader = split_loader or self._default_splits
        self._memo: Dict[tuple, Dict[date, float]] = {}

    # -- defaults -------------------------------------------------------------------------
    @staticmethod
    def _default_ohlcv(symbol: str, start: date, end: date):
        """Top up through the provider, then read the daily parquet (the basis proof needs the
        whole file, not a slice)."""
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        from ba2_trade_platform import config

        provider = FMPOHLCVProvider()
        provider.get_ohlcv_data(symbol, start_date=datetime(start.year, start.month, start.day) - timedelta(days=7),
                                end_date=datetime(end.year, end.month, end.day) + timedelta(days=1),
                                interval="1d")
        path = os.path.join(config.CACHE_FOLDER, "FMPOHLCVProvider", f"{symbol.upper()}_1d.parquet")
        if not os.path.exists(path):
            raise SpotUnavailable(f"{symbol}: no FMP daily cache at {path}")
        return pd.read_parquet(path, columns=["Date", "Open", "High", "Low", "Close"]), path

    @staticmethod
    def _default_splits(symbol: str):
        from ba2_common.core.split_basis import CalendarSplit
        from ba2_providers import symbol_info
        from ba2_providers.market_conditions.fmp_source import SPLIT_CALENDAR_NAMESPACE
        from ba2_trade_platform import config

        cal = os.path.join(config.CACHE_FOLDER, "fmp_history",
                           f"{SPLIT_CALENDAR_NAMESPACE}__{symbol.upper()}.json")
        if os.path.exists(cal):
            with open(cal, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return [CalendarSplit(e.date, float(e.ratio) if e.ratio else float("nan"))
                    for e in symbol_info.parse_splits(payload)]
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        return FMPOHLCVProvider()._split_calendar(symbol, "1d")

    # -- API ------------------------------------------------------------------------------
    def spots(self, symbol: str, start: date, end: date) -> Dict[date, float]:
        from ba2_common.core.split_basis import (SplitBasisRefused, read_full_fetch_marker,
                                                  resolve_symbol_split_basis)
        df, path = self._ohlcv_loader(symbol, start, end)
        if df is None or df.empty:
            raise SpotUnavailable(f"{symbol}: no daily OHLCV")
        d = pd.to_datetime(df["Date"])
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_localize(None)
        days = d.dt.normalize().to_numpy(dtype="datetime64[ns]")
        c = df["Close"].to_numpy(dtype=float)
        memo_key = (symbol.upper(), len(df), float(c[-1]), str(days[-1]), start, end)
        hit = self._memo.get(memo_key)
        if hit is not None:
            return hit
        try:
            splits = self._split_loader(symbol)
            marker = read_full_fetch_marker(path) if path else None
            basis = resolve_symbol_split_basis(symbol, days, df["Open"].to_numpy(dtype=float),
                                               df["High"].to_numpy(dtype=float),
                                               df["Low"].to_numpy(dtype=float), c, splits,
                                               marker=marker)
        except SplitBasisRefused as e:
            raise SpotUnavailable(f"{symbol}: split basis refused: {e}") from e
        out: Dict[date, float] = {}
        for ts, px in zip(pd.DatetimeIndex(days), c):
            day = ts.date()
            if start <= day <= end and math.isfinite(px) and px > 0:
                out[day] = float(px) * basis.factor(day)
        self._memo = {memo_key: out}
        return out


def _default_rate_source(start: date, end: date):
    from ba2_providers.macro.risk_free_rate import RiskFreeRateUnavailable, fred_dgs3mo_rate
    try:
        return fred_dgs3mo_rate(start, end)
    except RiskFreeRateUnavailable as e:
        raise AtmIvUnavailable(f"risk-free rate unavailable: {e}") from e


# ---------------------------------------------------------------------------------------------
# Background worker (one daemon thread, shared by every provider object)
# ---------------------------------------------------------------------------------------------
class _BackgroundWorker:
    def __init__(self):
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="atm-iv-history-fill",
                                                daemon=True)
                self._thread.start()
        self._q.put(job)

    def _run(self):
        while True:
            try:
                job = self._q.get(timeout=60)
            except queue.Empty:
                with self._lock:
                    if self._q.empty():
                        self._thread = None
                        return
                continue
            try:
                job()
            except BaseException as e:  # noqa: BLE001 - a fill must never kill the worker
                logger.error(f"ATM-IV history background job crashed: {e}", exc_info=True)


_BG = _BackgroundWorker()

# (cache_dir, symbol) -> True while a fill owns the symbol; shared across provider objects.
_FILL_LOCK = threading.Lock()
_FILLING: Dict[Tuple[str, str], bool] = {}
_FAIL_UNTIL: Dict[Tuple[str, str], float] = {}
_WARNED: Dict[str, date] = {}


def _warn_once_per_day(key: str, message: str, today: date) -> None:
    with _FILL_LOCK:
        if _WARNED.get(key) == today:
            return
        _WARNED[key] = today
    logger.warning(message)


# ---------------------------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------------------------
class AtmIvHistoryProvider:
    """See the module docstring. Every external dependency is an injectable seam."""

    def __init__(self, *, cache_dir: Optional[str] = None, options_provider: Any = None,
                 bars_client: Any = None, contract_lister: Any = None, spot_source: Any = None,
                 rate_source: Optional[Callable[[date, date], Any]] = None,
                 now: Optional[Callable[[], datetime]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 bucket: Optional[TokenBucket] = None,
                 background_runner: Optional[Callable[[Callable[[], None]], None]] = None):
        if cache_dir is None:
            from ba2_trade_platform import config
            cache_dir = os.path.join(config.CACHE_FOLDER, "AtmIvHistory")
        self.cache_dir = os.path.abspath(cache_dir)
        self._options_provider = options_provider
        self._bars_client = bars_client
        self._lister = contract_lister
        self.spot_source = spot_source if spot_source is not None else FmpSpotSource()
        self._rate_source = rate_source or _default_rate_source
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self.bucket = bucket or _SHARED_BUCKET
        self._runner = background_runner or _BG.submit
        #: every API call this provider made (listing pages + bar batches); for tests/diagnostics
        self.api_calls = 0

    # -- clients --------------------------------------------------------------------------
    def _bars(self):
        if self._bars_client is None:
            self._bars_client = AlpacaBarsClient(self._options_provider)
        return self._bars_client

    def _listing_client(self):
        if self._lister is None:
            self._lister = AlpacaContractLister(self._options_provider)
        return self._lister

    # -- paths / store --------------------------------------------------------------------
    def store_path(self, symbol: str) -> str:
        return os.path.join(self.cache_dir, f"{symbol.upper()}.parquet")

    def _contracts_paths(self, symbol: str) -> Tuple[str, str]:
        base = os.path.join(self.cache_dir, "_contracts", symbol.upper())
        return base + ".parquet", base + ".json"

    @staticmethod
    def _lock_for(path: str) -> threading.Lock:
        from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
        return MarketDataProviderInterface._get_cache_lock(path)

    def _atomic_write_parquet(self, df: pd.DataFrame, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def read_store(self, symbol: str) -> pd.DataFrame:
        path = self.store_path(symbol)
        with self._lock_for(path):
            return self._read_store_locked(path)

    def _read_store_locked(self, path: str) -> pd.DataFrame:
        if not os.path.exists(path):
            return pd.DataFrame(columns=STORE_COLUMNS)
        try:
            df = pd.read_parquet(path)
        except Exception as e:  # noqa: BLE001
            bad = path + ".corrupt"
            logger.error(f"ATM-IV history store {path} is unreadable ({e}); moved to {bad} and "
                         f"rebuilding from the API", exc_info=True)
            try:
                os.replace(path, bad)
            except OSError:
                pass
            return pd.DataFrame(columns=STORE_COLUMNS)
        df["session_date"] = pd.to_datetime(df["session_date"]).dt.normalize()
        return df

    @staticmethod
    def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
        """Fixed dtypes so a tombstone-only batch and a value batch concatenate and serialise."""
        df = df.copy()
        df["session_date"] = pd.to_datetime(df["session_date"]).dt.normalize()
        df["expiry"] = pd.to_datetime(df["expiry"])
        df["computed_at"] = pd.to_datetime(df["computed_at"])
        for c in ("iv", "strike", "dte", "delta", "bar_close", "bar_volume", "spot", "rate"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
        for c in ("reason", "occ", "provenance", "method_version"):
            df[c] = df[c].fillna("").astype(str)
        return df[STORE_COLUMNS]

    def _merge_rows(self, symbol: str, rows: List[dict]) -> None:
        if not rows:
            return
        path = self.store_path(symbol)
        new = self._normalize_frame(pd.DataFrame(rows, columns=STORE_COLUMNS))
        with self._lock_for(path):
            old = self._read_store_locked(path)
            if len(old):
                old = old[~old["session_date"].isin(new["session_date"])]
                merged = pd.concat([old, new], ignore_index=True) if len(old) else new
            else:
                merged = new
            merged = merged.sort_values("session_date").reset_index(drop=True)
            self._atomic_write_parquet(merged, path)

    # -- window / missing -----------------------------------------------------------------
    def _fresh_sessions(self, df: pd.DataFrame) -> set:
        if df.empty:
            return set()
        cur = df[df["method_version"] == METHOD_VERSION]
        return {ts.date() for ts in cur["session_date"]}

    def _assemble(self, symbol: str, df: pd.DataFrame, window: List[date], end_session: date,
                  status: str, reason: Optional[str] = None) -> AtmIvSeries:
        wset = set(window)
        values: Dict[date, float] = {}
        tomb = 0
        if not df.empty:
            cur = df[df["method_version"] == METHOD_VERSION]
            for ts, iv in zip(cur["session_date"], cur["iv"]):
                d = ts.date()
                if d not in wset:
                    continue
                if iv is None or (isinstance(iv, float) and math.isnan(iv)):
                    tomb += 1
                else:
                    values[d] = float(iv)
        resolved = len(values) + tomb
        return AtmIvSeries(
            symbol=symbol.upper(), end_session=end_session, status=status, values=values,
            covered=len(values), expected=len(window), tombstones=tomb,
            provenance={PROVENANCE_DERIVED: len(values)} if values else {},
            reason=reason, window_start=window[0] if window else None,
            unresolved=max(len(window) - resolved, 0))

    def peek(self, symbol: str, end_session: date, lookback_days: int = 365) -> AtmIvSeries:
        """The stored series only: ZERO API calls, no fill, no prerequisites."""
        window = window_sessions(end_session, lookback_days)
        df = self.read_store(symbol)
        missing = [d for d in window if d not in self._fresh_sessions(df)]
        return self._assemble(symbol, df, window, end_session,
                              STATUS_COMPLETE if not missing else STATUS_FILLING)

    # -- public API -----------------------------------------------------------------------
    def last_completed_session(self) -> date:
        return last_completed_session(self._now())

    def get_atm_iv_series(self, symbol: str, end_session: date, lookback_days: int = 365,
                          max_api_calls: int = 2, max_seconds: float = 3.0) -> AtmIvSeries:
        sym = symbol.upper()
        end_session = min(end_session, self.last_completed_session())
        window = window_sessions(end_session, lookback_days)
        df = self.read_store(sym)
        fresh = self._fresh_sessions(df)
        missing = [d for d in window if d not in fresh]
        if not missing:
            return self._assemble(sym, df, window, end_session, STATUS_COMPLETE)

        key = (self.cache_dir, sym)
        today = self._now().astimezone(_ny_tz()).date()
        with _FILL_LOCK:
            cooling = _FAIL_UNTIL.get(key, 0.0) > time.monotonic()
            busy = _FILLING.get(key, False)
            if not cooling and not busy:
                _FILLING[key] = True
        if cooling:
            return self._assemble(sym, df, window, end_session, STATUS_UNAVAILABLE,
                                  "background fill failed recently; retrying later")
        if busy:
            return self._assemble(sym, df, window, end_session, STATUS_FILLING,
                                  "another caller is filling this symbol")

        handed_off = False
        status, reason = STATUS_COMPLETE, None
        try:
            budget = _Budget(max_api_calls, max_seconds, self.bucket, blocking=False)
            try:
                self._run_fill(sym, window, budget, background=False)
            except AtmIvUnavailable as e:
                status, reason = STATUS_UNAVAILABLE, str(e)
                _warn_once_per_day(f"unavail:{key}", f"ATM-IV history unavailable for {sym}: {e}", today)
            except (BudgetExhausted, RateLimited, ApiFailure) as e:
                self._runner(lambda: self._background_job(key, sym, end_session, lookback_days))
                handed_off = True
                status, reason = STATUS_FILLING, f"continuing in background ({type(e).__name__})"
            if status == STATUS_COMPLETE:
                df = self.read_store(sym)
                still = [d for d in window if d not in self._fresh_sessions(df)]
                # sessions with no spot cannot be filled; they stay unresolved (not "filling")
                if still:
                    reason = f"{len(still)} session(s) not computable (no spot)"
        finally:
            if not handed_off:
                with _FILL_LOCK:
                    _FILLING.pop(key, None)
        df = self.read_store(sym)
        return self._assemble(sym, df, window, end_session, status, reason)

    # -- background -----------------------------------------------------------------------
    def _background_job(self, key, sym: str, end_session: date, lookback_days: int) -> None:
        today = self._now().astimezone(_ny_tz()).date()
        failures = 0
        t0 = time.monotonic()
        try:
            window = window_sessions(end_session, lookback_days)
            n_missing = len([d for d in window if d not in self._fresh_sessions(self.read_store(sym))])
            logger.info(f"ATM-IV history fill started for {sym}: {n_missing} of {len(window)} "
                        f"session(s) to derive")
            while True:
                budget = _Budget(None, None, self.bucket, blocking=True)
                try:
                    self._run_fill(sym, window, budget, background=True)
                    break
                except AtmIvUnavailable as e:
                    _warn_once_per_day(f"unavail:{key}", f"ATM-IV history fill for {sym} cannot "
                                       f"proceed: {e}", today)
                    with _FILL_LOCK:
                        _FAIL_UNTIL[key] = time.monotonic() + FAIL_COOLDOWN_SECONDS
                    return
                except (RateLimited, ApiFailure, BudgetExhausted) as e:
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.warning(f"ATM-IV history fill for {sym} giving up after {failures} "
                                       f"consecutive failures ({type(e).__name__}: {e}); will retry "
                                       f"in {FAIL_COOLDOWN_SECONDS // 60} min")
                        with _FILL_LOCK:
                            _FAIL_UNTIL[key] = time.monotonic() + FAIL_COOLDOWN_SECONDS
                        return
                    self._sleep(min(30.0 * failures, 120.0))
            df = self.read_store(sym)
            vals = self._assemble(sym, df, window, end_session, STATUS_COMPLETE)
            logger.info(f"ATM-IV history fill completed for {sym}: coverage {vals.coverage}, "
                        f"{vals.tombstones} tombstone(s), {time.monotonic() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ATM-IV history fill for {sym} failed unexpectedly: {e}", exc_info=True)
            with _FILL_LOCK:
                _FAIL_UNTIL[key] = time.monotonic() + FAIL_COOLDOWN_SECONDS
        finally:
            with _FILL_LOCK:
                _FILLING.pop(key, None)

    # -- the fill -------------------------------------------------------------------------
    def _run_fill(self, sym: str, window: List[date], budget: _Budget, *, background: bool) -> None:
        df = self.read_store(sym)
        fresh = self._fresh_sessions(df)
        missing = [d for d in window if d not in fresh]
        if not missing:
            return
        lo, hi = min(missing), max(missing)
        rate = self._rate_source(lo, hi)           # AtmIvUnavailable -> series unavailable
        spots = self.spot_source.spots(sym, lo, hi)  # SpotUnavailable -> series unavailable
        fillable = sorted((d for d in missing if d in spots), reverse=True)   # newest first
        if not fillable:
            return
        listing = self._ensure_listing(sym, budget)
        for i in range(0, len(fillable), CHUNK_SESSIONS):
            chunk = fillable[i:i + CHUNK_SESSIONS]
            rows = self._compute_chunk(sym, chunk, listing, spots, rate, budget)
            self._merge_rows(sym, rows)

    # -- contract listing -----------------------------------------------------------------
    def _read_listing(self, sym: str):
        pq, js = self._contracts_paths(sym)
        if not (os.path.exists(pq) and os.path.exists(js)):
            return None, None
        try:
            with open(js, "r", encoding="utf-8") as f:
                meta = json.load(f)
            df = pd.read_parquet(pq)
            df["expiry"] = pd.to_datetime(df["expiry"]).dt.normalize()
            return df, meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ATM-IV contract listing for {sym} unreadable ({e}); refetching")
            return None, None

    def _write_listing(self, sym: str, df: pd.DataFrame, meta: dict) -> None:
        pq, js = self._contracts_paths(sym)
        with self._lock_for(pq):
            self._atomic_write_parquet(df, pq)
            tmp = js + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            os.replace(tmp, js)

    def _list_all(self, sym: str, status: str, gte: Optional[date], lte: Optional[date],
                  budget: _Budget) -> pd.DataFrame:
        rows: List[dict] = []
        token = None
        while True:
            self._spend(budget)
            try:
                page, token = self._listing_client().list_page(sym, status, gte, lte, token)
            except Exception as e:  # noqa: BLE001
                raise self._classify(e) from e
            rows.extend(r for r in page if r.get("size", 100) == 100 and r.get("root", sym) in ("", sym))
            if not token:
                break
        out = pd.DataFrame(rows, columns=["occ", "expiry", "strike", "size", "root"])
        out["expiry"] = pd.to_datetime(out["expiry"]).dt.normalize()
        return out

    def _ensure_listing(self, sym: str, budget: _Budget) -> pd.DataFrame:
        today = self._now().astimezone(_ny_tz()).date()
        df, meta = self._read_listing(sym)
        floor_gte = HISTORY_FLOOR + timedelta(days=DTE_MIN)
        if df is None:
            inactive = self._list_all(sym, "inactive", floor_gte, today, budget)
            df = inactive
            meta = {"inactive_through": today.isoformat(), "active_fetched": None}
            self._write_listing(sym, df, meta)   # progress survives a budget stop before 'active'
        else:
            through = date.fromisoformat(meta["inactive_through"])
            if (today - through).days > INACTIVE_TAIL_REFRESH_DAYS:
                tail = self._list_all(sym, "inactive", through + timedelta(days=1), today, budget)
                df = pd.concat([df, tail], ignore_index=True).drop_duplicates("occ", keep="last")
                meta["inactive_through"] = today.isoformat()
                self._write_listing(sym, df, meta)
        if meta.get("active_fetched") != today.isoformat():
            active = self._list_all(sym, "active", None, today + timedelta(days=60), budget)
            # contracts that were active at the last refresh and have since expired are in neither
            # fresh list (not active, not in the once-fetched inactive set): keep them (graduate)
            keep = df[df["expiry"] < pd.Timestamp(today)] if len(df) else df
            df = pd.concat([keep, active], ignore_index=True).drop_duplicates("occ", keep="last")
            # keep everything already known that expired earlier too (inactive set rows)
            meta["active_fetched"] = today.isoformat()
            self._write_listing(sym, df, meta)
        return df

    # -- API plumbing ---------------------------------------------------------------------
    def _spend(self, budget: _Budget) -> None:
        budget.spend(1)
        self.api_calls += 1

    @staticmethod
    def _classify(e: BaseException) -> Exception:
        if isinstance(e, (BudgetExhausted, RateLimited, ApiFailure, AtmIvUnavailable)):
            return e
        if _is_rate_limited(e):
            return RateLimited(str(e))
        return ApiFailure(f"{type(e).__name__}: {e}")

    def _fetch_batch(self, batch: Sequence[str], start: date, end: date, budget: _Budget):
        attempt = 0
        while True:
            self._spend(budget)
            try:
                return self._bars().fetch(batch, start, end)
            except Exception as e:  # noqa: BLE001
                err = self._classify(e)
                if isinstance(err, RateLimited) and budget.blocking and attempt < 4:
                    attempt += 1
                    self._sleep(min(5.0 * 2 ** attempt, 60.0))
                    continue
                raise err from e

    # -- one chunk of sessions ------------------------------------------------------------
    def _compute_chunk(self, sym: str, sessions: List[date], listing: pd.DataFrame,
                       spots: Dict[date, float], rate_obj: Any, budget: _Budget) -> List[dict]:
        by_exp: Dict[date, Tuple[np.ndarray, Dict[float, str]]] = {}
        if len(listing):
            for exp, g in listing.groupby("expiry"):
                by_exp[exp.date()] = (np.sort(g["strike"].unique()),
                                      {float(k): o for k, o in zip(g["strike"], g["occ"])})
        member: Dict[date, Dict[str, Tuple[float, date]]] = {}
        need: Dict[str, List[date]] = {}
        for d in sessions:
            S = float(spots[d])
            m: Dict[str, Tuple[float, date]] = {}
            for e, (ks, occs) in by_exp.items():
                if DTE_MIN <= (e - d).days <= DTE_MAX:
                    for K in nearest_strikes(ks, S):
                        occ = occs[float(K)]
                        m[occ] = (float(K), e)
                        need.setdefault(occ, []).append(d)
            member[d] = m
        occ_list = sorted(need, key=lambda o: (min(need[o]), o))
        batches = [occ_list[i:i + BARS_BATCH] for i in range(0, len(occ_list), BARS_BATCH)]
        budget.require(len(batches))        # a chunk is atomic: never spend calls we cannot finish
        bars: Dict[str, Dict[date, BarRec]] = {}
        for batch in batches:
            start = min(min(need[o]) for o in batch)
            end = max(max(need[o]) for o in batch)
            for occ, recs in self._fetch_batch(batch, start, end, budget).items():
                bars[occ] = {r.day: r for r in recs}

        now_ts = pd.Timestamp(self._now().astimezone(timezone.utc).replace(tzinfo=None))
        rows: List[dict] = []
        for d in sessions:
            S = float(spots[d])
            r = float(rate_obj.rate_on(d))
            cands = []
            for occ, (K, e) in member[d].items():
                b = bars.get(occ, {}).get(d)
                if b is not None:
                    cands.append(Candidate(occ, K, e, b.close, b.volume))
            base = dict(session_date=d, spot=S, rate=r, provenance=PROVENANCE_DERIVED,
                        method_version=METHOD_VERSION, computed_at=now_ts)
            if not cands:
                rows.append(self._tomb(base, "no_bar"))
                continue
            sel = select_atm(d, S, r, cands)
            if sel is None:
                rows.append(self._tomb(base, "no_iv"))
                continue
            c = sel.candidate
            detail = dict(occ=c.occ, strike=c.strike, expiry=pd.Timestamp(c.expiry), dte=sel.dte,
                          delta=sel.delta, bar_close=c.close,
                          bar_volume=np.nan if c.volume is None else float(c.volume))
            if c.volume is None or c.volume < MIN_BAR_VOLUME:
                rows.append(self._tomb({**base, **detail}, "low_volume"))
                continue
            rows.append({**base, **detail, "iv": sel.iv, "reason": "ok"})
        return rows

    @staticmethod
    def _tomb(base: dict, reason: str) -> dict:
        row = {c: np.nan for c in STORE_COLUMNS}
        row.update(base)
        row.update(iv=np.nan, reason=reason)
        if not isinstance(row.get("occ"), str):
            row["occ"] = ""
        return row


# ---------------------------------------------------------------------------------------------
# Snapshot merge (used by the account; kept here so the precedence rule has one home)
# ---------------------------------------------------------------------------------------------
def merge_snapshots(series: AtmIvSeries, snapshots: Dict[date, float],
                    stored_sessions: Optional[Iterable[date]] = None) -> Tuple[Dict[date, float], Dict[str, int]]:
    """Derived values first; a snapshot only fills a date with NO derived row (value or
    tombstone). ``stored_sessions`` = every session that has a derived row. Returns
    ``(values, provenance_counts)``."""
    have = set(stored_sessions or ()) | set(series.values)
    merged = dict(series.values)
    n_snap = 0
    for d, v in snapshots.items():
        if d not in have and series.window_start is not None and series.window_start <= d <= series.end_session:
            merged[d] = float(v)
            n_snap += 1
    prov: Dict[str, int] = {}
    if series.values:
        prov[PROVENANCE_DERIVED] = len(series.values)
    if n_snap:
        prov[PROVENANCE_SNAPSHOT] = n_snap
    return merged, prov


# ---------------------------------------------------------------------------------------------
# Per-credential provider registry (the account objects are rebuilt often; the provider's
# state -- bucket, fills, background thread -- is process-wide anyway)
# ---------------------------------------------------------------------------------------------
_REGISTRY: Dict[Tuple[str, bool], AtmIvHistoryProvider] = {}
_REGISTRY_LOCK = threading.Lock()


def get_provider_for_credentials(api_key: str, api_secret: str, paper: bool) -> AtmIvHistoryProvider:
    from ba2_providers.options.alpaca import AlpacaOptionsProvider
    k = (api_key, bool(paper))
    with _REGISTRY_LOCK:
        p = _REGISTRY.get(k)
        if p is None:
            p = AtmIvHistoryProvider(options_provider=AlpacaOptionsProvider(
                api_key=api_key, api_secret=api_secret, paper=bool(paper)))
            _REGISTRY[k] = p
        return p
