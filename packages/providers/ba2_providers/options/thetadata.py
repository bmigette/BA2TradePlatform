"""ThetaData cloud historical-options provider (official ``thetadata`` Python library).

TRANSPORT (rewritten 2026-09-02). The previous implementation of this module talked to a
LOCALLY-RUNNING "Theta Terminal" desktop process over unauthenticated REST on
``127.0.0.1:25503`` — that was ThetaData's only offering when this file was first written.
ThetaData has since shipped an official cloud Python library (``pip install thetadata``) that
connects directly to their servers with a bearer API key (``ThetaClient(api_key=...)``), no
terminal required. That is the transport a "td1_prod_..." key is for, and it is what this
module now uses exclusively.

WHY THIS PROVIDER EXISTS AT ALL, alongside TastyTrade's (``tastytrade.py``, the platform's
other historical-options source): TastyTrade/dxfeed's IV coverage floors around October 2022
(measured) — real, but recent. A live probe against ThetaData with a real key found AAPL
option EXPIRATIONS back to 2012-06-01, comfortably covering a 2020 backfill.

DATA SHAPE. Unlike the old local-terminal REST endpoint (one CSV call returned a whole chain's
OHLC across ALL expirations for a window), the cloud library requires ONE (underlying, single
expiration) call per request. That call CAN span a multi-week date window in one shot
(confirmed: a 2-week AAPL window in one ``option_history_greeks_eod`` call) but NOT an
arbitrary one: the server enforces a hard 365-DAY CAP per request (measured live 2026-09-02:
"Too many days between start and end date; max 365 days allowed", grpc StatusCode.
INVALID_ARGUMENT) — a multi-year backfill window is chunked into <=365-day sub-requests by
``_chunk_window`` and looped over per (underlying, expiry), rather than sent as one call. So
the natural unit of work is the SAME as TastyTrade's: one (underlying, expiry) partition,
fetched with (up to) two calls PER WINDOW CHUNK instead of TastyTrade's one call total:

  * ``option_history_greeks_eod`` — OHLC + bid/ask/size + the full greeks block, INCLUDING
    ``implied_vol`` (a superset of the plain ``option_history_eod`` endpoint, so that one is
    never called separately here).
  * ``option_history_open_interest`` — ThetaData does not fold OI into the bars call the way
    TastyTrade's dxfeed candles do; it is one snapshot per (contract, day), joined back onto
    the bars by (strike, right, date).

Both calls RAISE ``thetadata.errors.NoDataFoundError`` (rather than returning an empty
dataframe) when a request's window/expiry genuinely has nothing — a normal outcome (a
low-volume underlying with no listed chain on a given Friday), not an error; caught per-chunk
and treated the same as an empty response (see ``ThetaDataOptionsProvider._no_data_exc``).

Both calls are per-(underlying, single expiry), so ``fetch_eod_bars``/``fetch_bars_detailed``
group the requested contracts by (underlying, expiry) exactly like the caller (TastyTrade
today) already assumes — see ``tools/warm_options_history.py``'s ``WorkUnit``.

CONCURRENCY. Unlike TastyTrade (separate OS PROCESSES, each with its own dxfeed session --
see ``tools/run_option_warmup_parallel.py``), ThetaData's account authenticates ONE session
per api_key: launching 8 separate processes each calling ``ThetaClient(api_key=...)``
independently was measured live 2026-09-02 to invalidate each other's sessions --
``StatusCode.UNAUTHENTICATED: "Invalid session ID. This can occur if more than one terminal
is running."`` -- failing almost every unit outright. The library's fix is
``existing_authorized_client``: a second ``ThetaClient`` built from an already-authenticated
one SHARES that session rather than opening a new, competing one (confirmed live: 4 threads
sharing one authenticated client via this parameter, zero errors). Sharing a Python object
only works within one process, so ThetaData concurrency here is THREADS, not processes --
``_get_client`` authenticates ONE session on first use (lock-guarded) and hands every thread
its own ``existing_authorized_client``-linked client (thread-local, never shared directly:
a gRPC client object is not documented as thread-safe, one per thread is the safe default).

Docs: https://docs.thetadata.us/Python-Library/Getting-Started.html
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    CandleBatch, OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)

logger = logging.getLogger(__name__)

# Depth to accept without rejecting the window up front (see history_floor). NOT a claim that
# every underlying has data this far back — it is a live-verified floor for at least one name
# (AAPL, back to 2012), kept conservative here so a 2020 backfill is comfortably inside it
# without overclaiming depth for names that listed later. Env-overridable for a probe that
# wants to push it.
_DEFAULT_HISTORY_YEARS = float(os.getenv("THETADATA_HISTORY_YEARS", "8"))

#: The server's hard per-request cap: "Too many days between start and end date; max 365
#: days allowed" (grpc StatusCode.INVALID_ARGUMENT, measured live 2026-09-02). A window wider
#: than this must be split into consecutive sub-requests -- see _chunk_window.
_MAX_WINDOW_DAYS = 365


def _chunk_window(start: date, end: date,
                  max_days: int = _MAX_WINDOW_DAYS) -> List[Tuple[date, date]]:
    """Split ``[start, end]`` (inclusive) into consecutive, non-overlapping sub-windows each
    spanning at most ``max_days``. A window already within the cap returns a single chunk
    identical to the input -- no behaviour change for the common (narrow-window) case.

    Non-overlapping by construction, so a caller merging bars across chunks never has to
    worry about a bar_date appearing in two of them.
    """
    out: List[Tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=max_days))
        out.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return out


def _parse_date(value: Any) -> Optional[date]:
    """Accepts a ``date``/``datetime``/pandas ``Timestamp`` (tz-aware or not) or a
    ``YYYY-MM-DD``/``YYYYMMDD`` string — the library returns dates as plain strings in some
    columns (``expiration`` from ``option_list_expirations``) and as pandas datetime64 in
    others (``timestamp`` from ``option_history_open_interest``), so every shape must be
    handled rather than assumed."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas.Timestamp / numpy.datetime64 duck-type a `.date()` or `.to_pydatetime()`.
    to_date = getattr(value, "date", None)
    if callable(to_date):
        try:
            return to_date()
        except TypeError:
            pass
    s = str(value).strip()
    if not s or s.lower() == "nat":
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    return None


def _num(value: Any) -> Optional[float]:
    """NaN-safe float extraction from a pandas cell. pandas represents a missing numeric as
    ``float('nan')``, not ``None`` — a bare ``value is None`` check would let NaN through as a
    real number (and ``float('nan')`` is truthy, so it would not even get caught downstream)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN != NaN


def _right_word(right: Any) -> str:
    """'CALL'/'call'/'C' -> 'call'; anything else -> 'put'. ThetaData's dataframes spell it
    out ('CALL'/'PUT'); normalise once so every call site agrees."""
    return "call" if str(right).strip().lower().startswith("c") else "put"


def _occ_symbol(underlying: str, expiry: date, right: Any, strike: float) -> str:
    """ROOT + YYMMDD + C/P + strike*1000 (8 digits) — the OCC id the rest of the platform (and
    the TastyTrade-built caches) key on. Matches ``tastytrade.py``'s ``occ_symbol``."""
    cp = "C" if _right_word(right) == "call" else "P"
    return f"{underlying.upper()}{expiry:%y%m%d}{cp}{int(round(float(strike) * 1000)):08d}"


def _index_open_interest(df: Any) -> Dict[Tuple[float, str, date], int]:
    """(strike, 'call'|'put', bar_date) -> open_interest, from an
    ``option_history_open_interest`` dataframe. One row per (contract, day) — no aggregation
    needed, just a lookup index for the bars merge."""
    out: Dict[Tuple[float, str, date], int] = {}
    if df is None or len(df) == 0:
        return out
    for row in df.itertuples(index=False):
        r = row._asdict()
        strike = _num(r.get("strike"))
        bar_date = _parse_date(r.get("timestamp") or r.get("date"))
        oi = _num(r.get("open_interest"))
        if strike is None or bar_date is None or oi is None:
            continue
        out[(strike, _right_word(r.get("right")), bar_date)] = int(oi)
    return out


class _NeverRaised(Exception):
    """Placeholder for ThetaDataOptionsProvider._no_data_exc before a live client has resolved
    the real thetadata.errors.NoDataFoundError. Nothing ever raises this on purpose."""


class ThetaDataOptionsProvider(OptionsDataProviderInterface):
    """Historical options via ThetaData's cloud API (official ``thetadata`` Python library)."""

    name = "thetadata"

    def __init__(self, api_key: Optional[str] = None, history_years: Optional[float] = None,
                 dataframe_type: str = "pandas"):
        # NO required credentials at construction (matches Alpaca/TastyTrade here): the
        # registry (OPTIONS_PROVIDERS / options_history_floor) constructs every provider with
        # no args just to ask its history_floor(), and must not need a live key to do that.
        # api_key is resolved lazily, in _get_client, only once a method that actually talks
        # to the network is called.
        self.api_key = api_key or os.getenv("THETADATA_API_KEY")
        self.history_years = float(history_years if history_years is not None
                                   else _DEFAULT_HISTORY_YEARS)
        self._dataframe_type = dataframe_type
        self._client = None  # lazy: the ONE authenticated session (or a test-injected fake)
        # True only once THIS instance has authenticated _client itself (see _get_client) --
        # a test/other caller that injects its own fake straight into _client leaves this
        # False, which is what tells _get_client to hand that fake back verbatim instead of
        # trying to wrap it in a real ThetaClient(existing_authorized_client=...).
        self._owns_session = False
        self._session_lock = threading.Lock()
        self._thread_local = threading.local()
        # Resolved alongside _client, in _get_client -- the real thetadata.errors.
        # NoDataFoundError once a live client exists. _NeverRaised until then, deliberately:
        # nothing ever raises it, so a caller that reaches the per-chunk except clause before
        # _get_client() has run (should not happen, but must not silently swallow real
        # exceptions if it somehow does) catches nothing rather than everything.
        self._no_data_exc: type = _NeverRaised

    # -- interface ------------------------------------------------------
    def _get_client(self):
        """The calling thread's client. Every thread shares the SAME authenticated session
        (see the module docstring's CONCURRENCY section) but gets its OWN client object -- a
        gRPC client is not documented thread-safe, so one per thread is the conservative
        choice even though the session underneath is shared."""
        if self._client is None:
            with self._session_lock:
                if self._client is None:  # re-check: lost the race to another thread
                    if not self.api_key:
                        raise RuntimeError(
                            "ThetaDataOptionsProvider has no api_key (pass one, or set "
                            "THETADATA_API_KEY) -- it was only asked for its history_floor() "
                            "until now, which needs no key.")
                    from thetadata import ThetaClient
                    from thetadata.errors import NoDataFoundError
                    self._client = ThetaClient(api_key=self.api_key,
                                               dataframe_type=self._dataframe_type)
                    self._no_data_exc = NoDataFoundError
                    self._owns_session = True

        if not self._owns_session:
            # A fake/other client was injected directly (tests) -- hand it back verbatim;
            # wrapping it in a real ThetaClient(existing_authorized_client=...) would both
            # need the real thetadata package and defeat the injection entirely.
            return self._client

        thread_client = getattr(self._thread_local, "client", None)
        if thread_client is None:
            from thetadata import ThetaClient
            thread_client = ThetaClient(existing_authorized_client=self._client,
                                        dataframe_type=self._dataframe_type)
            self._thread_local.client = thread_client
        return thread_client

    def history_floor(self) -> date:
        today = date.today()
        return today.replace(year=today.year - int(self.history_years))

    def discover_contracts(self, underlying: str, *, expiry_gte: date, expiry_lte: date,
                           strike_min: Optional[float] = None,
                           strike_max: Optional[float] = None,
                           max_contracts: Optional[int] = None) -> List[OptionContractMeta]:
        """Only reached by ``--discovery rest``; the default (and what a real backfill uses)
        is ``--discovery synthetic``, which builds contracts from the local price cache and
        never calls this. Kept correct rather than optimised: one ``option_list_strikes`` call
        per expiry in the window."""
        client = self._get_client()
        exps = client.option_list_expirations(underlying.upper())
        out: List[OptionContractMeta] = []
        for row in exps.itertuples(index=False):
            exp = _parse_date(getattr(row, "expiration", None))
            if exp is None or exp < expiry_gte or exp > expiry_lte:
                continue
            strikes = client.option_list_strikes(underlying.upper(), exp)
            for srow in strikes.itertuples(index=False):
                strike = _num(getattr(srow, "strike", None))
                if strike is None:
                    continue
                if strike_min is not None and strike < strike_min:
                    continue
                if strike_max is not None and strike > strike_max:
                    continue
                for right in ("call", "put"):
                    occ = _occ_symbol(underlying, exp, right, strike)
                    out.append(OptionContractMeta(occ_symbol=occ, underlying=underlying.upper(),
                                                   option_type=right, strike=strike, expiry=exp))
        if max_contracts is not None and len(out) > max_contracts:
            # Keep strikes nearest the band centre — near-the-money is what gets selected.
            if strike_min is not None and strike_max is not None:
                centre = (strike_min + strike_max) / 2.0
            else:
                strikes_sorted = sorted(c.strike for c in out)
                centre = strikes_sorted[len(strikes_sorted) // 2]
            out = sorted(out, key=lambda c: abs(c.strike - centre))[:max_contracts]
        return out

    def fetch_eod_bars(self, contracts: Iterable[OptionContractMeta], *,
                       start: date, end: date) -> Iterator[OptionEodBar]:
        client = self._get_client()
        by_underlying_expiry: Dict[Tuple[str, date], set] = {}
        for c in contracts:
            by_underlying_expiry.setdefault((c.underlying.upper(), c.expiry), set()).add(
                c.occ_symbol)

        windows = _chunk_window(start, end)
        for (underlying, expiry), wanted in by_underlying_expiry.items():
            # One (underlying, expiry) partition, but the window may be wider than the
            # server's 365-day-per-request cap -- see _chunk_window. Chunks are
            # non-overlapping, so no bar_date can appear in two of them: concatenating the
            # per-chunk results (by just yielding as each chunk is processed) is safe without
            # any cross-chunk de-duplication.
            for w_start, w_end in windows:
                try:
                    bars_df = client.option_history_greeks_eod(
                        symbol=underlying, expiration=expiry, strike="*", right="both",
                        start_date=w_start, end_date=w_end)
                except self._no_data_exc:
                    # The library RAISES rather than returning an empty dataframe when a
                    # request's window/expiry genuinely has nothing -- e.g. a low-volume
                    # underlying with no listed chain on a given Friday. Confirmed live
                    # 2026-09-02 (NoDataFoundError), the same "normal, not an error" case the
                    # len()==0 branch below already handles for the rare case it DOES return
                    # an empty frame instead.
                    continue
                if bars_df is None or len(bars_df) == 0:
                    continue  # nothing for this expiry in this chunk -- normal, not an error
                try:
                    oi_df = client.option_history_open_interest(
                        symbol=underlying, expiration=expiry, strike="*", right="both",
                        start_date=w_start, end_date=w_end)
                except self._no_data_exc:
                    oi_df = None  # no OI for this chunk; bars alone are still worth keeping
                oi_map = _index_open_interest(oi_df)

                for row in bars_df.itertuples(index=False):
                    r = row._asdict()
                    strike = _num(r.get("strike"))
                    bar_date = _parse_date(r.get("timestamp") or r.get("created"))
                    right = r.get("right")
                    if strike is None or bar_date is None:
                        continue
                    occ = _occ_symbol(underlying, expiry, right, strike)
                    if occ not in wanted:
                        continue
                    close = _num(r.get("close"))
                    if close is None:
                        continue  # a row with no close is not a usable bar
                    yield OptionEodBar(
                        occ_symbol=occ, bar_date=bar_date,
                        open=_num(r.get("open")) if _num(r.get("open")) is not None else close,
                        high=_num(r.get("high")) if _num(r.get("high")) is not None else close,
                        low=_num(r.get("low")) if _num(r.get("low")) is not None else close,
                        close=close,
                        volume=(int(_num(r.get("volume"))) if _num(r.get("volume")) is not None
                               else None),
                        bid=_num(r.get("bid")) or None,   # 0 -> None: "no quote"
                        ask=_num(r.get("ask")) or None,
                        open_interest=oi_map.get((strike, _right_word(right), bar_date)),
                        iv=_num(r.get("implied_vol")),
                    )

    def fetch_bars_detailed(self, contracts: Iterable[OptionContractMeta], *,
                            start: date, end: date) -> CandleBatch:
        """Matches TastyTrade's ``fetch_bars_detailed`` contract (see ``CandleBatch``) so
        ``tools/warm_options_history.py``'s retry/requeue loop works unchanged for either
        provider. ThetaData's API is plain request/response (no streaming "still pending"
        state), so ``unresolved`` is always empty here: a contract either came back with bars
        or it did not, on this one attempt — there is nothing partial to re-subscribe to."""
        contracts = list(contracts)
        wanted = {c.occ_symbol for c in contracts}
        if not wanted:
            return CandleBatch()
        bars = list(self.fetch_eod_bars(contracts, start=start, end=end))
        seen = {b.occ_symbol for b in bars}
        return CandleBatch(bars=bars, empty=wanted - seen, unresolved=set())
