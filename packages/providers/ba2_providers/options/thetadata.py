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

DATA SHAPE. There are TWO request shapes here, and for a bulk backfill only one of them is
sane:

  * WIDE (``fetch_underlying_eod_bars``) — ``expiration="*"`` returns the whole chain, every
    listed expiration at once, for every day in the window. ~2,950 rows/s measured. THIS IS
    THE ONE TO USE for backfills.
  * PER-EXPIRY (``fetch_eod_bars``) — one (underlying, single expiration) per request, looped.
    43 rows/s effective. Kept because it satisfies the OptionsDataProviderInterface contract
    (a caller asking for a specific contract list) and because the greeks endpoint has no
    wide form, but it must NOT be used to move bulk history.

The per-expiry shape was originally believed to be the only one available: a
``"When expiration=*, you must request data a day-at-a-time"`` rejection was observed on
``option_history_greeks_eod`` and wrongly assumed to hold for every endpoint. Re-measured
2026-09-03, it does not: ``option_history_eod`` and ``option_history_open_interest`` both
accept ``expiration="*"`` across a full 365-day range, and only the greeks endpoint enforces
the day-at-a-time rule. That single mistake made a ~6-day backfill look like an ~80-150 day
one. See docs/2026-09-03-thetadata-eod-backfill-assessment.md §4.

Both shapes still obey the server's hard 365-DAY CAP per request (measured live 2026-09-02:
"Too many days between start and end date; max 365 days allowed", grpc StatusCode.
INVALID_ARGUMENT), so a multi-year window is chunked by ``_chunk_window``.

In the PER-EXPIRY shape the window is narrowed to end at the EXPIRY, not the run's global end,
before chunking: an option never trades past its own expiration, so a 2020-01-17 expiry
queried all the way to a 2026-09 run end wasted 6 of 7 chunk-pairs getting NoDataFoundError
for years the contract could never have traded in (measured live: ~9.5 min for that ONE unit).
Only the END is narrowed, never the START -- a LEAPS contract can legitimately have started
trading long before the run's start date, so narrowing that side risks losing real data. The
WIDE shape needs none of this: it has no per-expiry loop to clamp.

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
#: than this must be split into consecutive sub-requests -- see _chunk_window. This is the
#: CEILING, not the size we ask for: see _REQUEST_WINDOW_DAYS.
_MAX_WINDOW_DAYS = 365

#: The window size we actually REQUEST, well under the server's 365-day cap. The reason is
#: MEMORY, not speed: throughput is flat across window sizes in the wide shape (measured
#: 2026-09-03: 2,981 rows/s at 30 days vs 2,949 at 365), but a 30-day window holds ~44k rows
#: (~10 MB) in flight where a 365-day one holds ~565k (~130 MB). A previous backfill was
#: killed at 17.9 GB resident, so 13x less resident for the same rate is free.
#:
#: (This constant previously cited ThetaData support's "one month per request" guidance and a
#: claimed 12x scan saving. That guidance was given about the PER-EXPIRY shape and does not
#: describe the wide one, where the measured difference is ~1%.)
#: https://docs.thetadata.us/Articles/Data-And-Requests/Request-Sizing.html
_REQUEST_WINDOW_DAYS = 30


def _chunk_window(start: date, end: date,
                  max_days: int = _REQUEST_WINDOW_DAYS) -> List[Tuple[date, date]]:
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


#: The exchange ThetaData's "current day" is measured in. US options trade on US Eastern, and
#: a backfill machine anywhere else will disagree with the server about what "today" is for
#: part of every 24 hours -- see the clamp in fetch_underlying_eod_bars.
_EXCHANGE_TZ = "America/New_York"


def _exchange_today() -> date:
    """Today's date AT THE EXCHANGE, not on this machine.

    Falls back to the local date if the tz database is unavailable (a bare Windows install
    without ``tzdata``): a wrong-by-one date is far better than a provider that cannot fetch
    at all, and the caller subtracts a further day on top of this.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(_EXCHANGE_TZ)).date()
    except Exception:  # noqa: BLE001 -- see docstring; must never break a fetch
        logger.warning("thetadata: no %s tz data; falling back to the local date for the "
                       "current-day clamp", _EXCHANGE_TZ)
        return date.today()


def _traded(close: Optional[float]) -> bool:
    """Did this contract actually trade on this bar's day?

    ThetaData reports EOD OHLC as a TRADE statistic and returns 0.0 for every field on a day
    with no trade. An option can never print at 0.00, so a non-positive close is exactly the
    no-trade sentinel -- verified live 2026-09-03 on MSFT/F/GE/KO across 2020-2023 plus AAPL
    2024 (4,944 rows): ``close > 0`` holds if and only if ``volume > 0``, zero exceptions in
    either direction, with open/high/low 0.0 on every no-trade row.

    Kept as ONE function so the wide and per-expiry paths cannot drift apart on the rule.
    """
    return close is not None and close > 0


def _quote(bid: Optional[float],
           ask: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """``(bid, ask)`` as a real NBBO, or ``(None, None)`` when there is no quote at all.

    The two zeros mean OPPOSITE things and must not be collapsed:

      * ``ask == 0`` -- there is no quote. A listed option always has an offer, so a zero ask
        is ThetaData's "nothing here" sentinel, and passing it through as a real price makes
        the contract look free to the no-arb guard. That is the artifact class that produced
        fabricated option profits, so it stays nulled.
      * ``bid == 0`` with a real ask -- a GENUINE quote meaning nobody is bidding. Verified
        live 2026-09-03: such rows still carry a ``bid_exchange`` stamp and an ask with real
        size, and they are 16.6% of a liquid chain (3,024 of 3,101 with real open interest).
        Nulling this would erase the difference between "we know nobody bids" -- which is
        exactly what a liquidity gate needs -- and "we do not know".
    """
    if ask is None or ask <= 0:
        return None, None
    return bid, ask


#: Above this, a vendor "implied volatility" is not a measurement. IV is stored as a DECIMAL
#: (0.2841 == 28.41%), so 100.0 is 10,000% annualised -- already far past anything a real
#: contract prints. The cutoff exists because ThetaData signals "could not invert" with an
#: INT32_MAX sentinel rather than a null, at more than one scale factor. Measured on the live
#: backfill 2026-09-04: 214748.3646 (== 2147483646/10000) and 21474.8365 (/100000) both
#: appear, together with a tail of 10^2-10^3 values, totalling 0.4% of rows. Storing those as
#: real numbers puts a 21,474 into anything that reads vendor IV.
_MAX_PLAUSIBLE_IV = 100.0


def _clean_iv(value: Any) -> Optional[float]:
    """A vendor implied_vol as a usable decimal, or None when it is not a measurement.

    ONE function so the wide and per-expiry paths cannot drift apart on the rule -- they write
    into the same store, and an `iv` whose meaning depended on which code path fetched it
    would be worse than no iv at all.
    """
    iv = _num(value)
    if iv is None or iv == 0.0 or iv > _MAX_PLAUSIBLE_IV:
        return None
    return iv


def _index_implied_vol(df: Any) -> Dict[Tuple[float, str, date], float]:
    """(strike, 'call'|'put', bar_date) -> implied_vol, from an ``option_history_greeks_eod``
    dataframe. Same join key as ``_index_open_interest``; separate function because the date
    lives in a different column (``timestamp``, not ``created``).

    TWO vendor "no value" encodings are dropped rather than stored, both for the same reason
    the class docstring gives for ``iv``: None must never be coerced to a number, because
    downstream reads a number as a measurement.

      * ``0.0``            -- "could not invert this", not a contract with zero volatility.
      * ``> _MAX_PLAUSIBLE_IV`` -- an INT32_MAX sentinel (see that constant).
    """
    out: Dict[Tuple[float, str, date], float] = {}
    if df is None or len(df) == 0:
        return out
    dropped = 0
    for row in df.itertuples(index=False):
        r = row._asdict()
        strike = _num(r.get("strike"))
        bar_date = _parse_date(r.get("timestamp") or r.get("created"))
        raw = _num(r.get("implied_vol"))
        iv = _clean_iv(raw)
        if strike is None or bar_date is None or iv is None:
            if raw is not None and raw > _MAX_PLAUSIBLE_IV:
                dropped += 1
            continue
        out[(strike, _right_word(r.get("right")), bar_date)] = iv
    if dropped:
        # Counted and reported, not silently discarded: a sudden jump in this number means the
        # vendor changed an encoding, which is exactly the thing that must not pass unnoticed.
        logger.warning("thetadata: dropped %d implied_vol value(s) above %.0f (vendor "
                       "sentinel, not a measurement)", dropped, _MAX_PLAUSIBLE_IV)
    return out


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

        for (underlying, expiry), wanted in by_underlying_expiry.items():
            # An option NEVER trades past its own expiration, so querying any further than
            # that wastes real requests on chunks guaranteed to answer "nothing here" --
            # measured live 2026-09-02: a 2020-01-17 expiry queried all the way to a
            # 2026-09-02 run end spent 7 chunk-pairs of calls (~9.5 min) getting NoDataFound
            # on 6 of them for months the contract could never have traded in. Capping the
            # window's END at the expiry (never touching the START -- a contract, especially
            # a LEAPS, can legitimately have started trading long before the run's start
            # date, so narrowing that side risks losing real data) fixes that for every
            # expiry earlier than the run's end, which in a multi-year backfill is most of
            # them.
            group_end = min(end, expiry)
            if group_end < start:
                continue  # this expiry is entirely before the window -- nothing to fetch
            # A contract does not exist before it is LISTED, and the run's start date says
            # nothing about when that was: AAPL's 2024-06-21 expiry first traded 2022-03-11,
            # so a 2020-01-01 run start scans 2.2 years this contract could not have traded
            # in (measured live 2026-09-03; its 2022-03-18 expiry wastes 68% of the range,
            # and a weekly -- listed weeks, not years, before expiry -- wastes nearly all of
            # it). option_list_dates answers that in one small call, so ask rather than scan.
            # Best-effort by construction: any failure leaves `group_start` at the caller's
            # own start, which is exactly today's behaviour.
            group_start = start
            try:
                dates_df = client.option_list_dates(
                    request_type="quote", symbol=underlying, expiration=expiry)
                first_traded = _parse_date(
                    dates_df.iloc[0].get("date")) if dates_df is not None and len(dates_df) else None
                if first_traded is not None and first_traded > group_start:
                    group_start = first_traded
            except Exception:  # noqa: BLE001 -- an optimisation must never fail a fetch
                pass
            if group_end < group_start:
                continue
            # One (underlying, expiry) partition, but the window may be wider than the
            # server's 365-day-per-request cap -- see _chunk_window. Chunks are
            # non-overlapping, so no bar_date can appear in two of them: concatenating the
            # per-chunk results (by just yielding as each chunk is processed) is safe without
            # any cross-chunk de-duplication.
            windows = _chunk_window(group_start, group_end)
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
                    o, h, l, close = (_num(r.get(k))
                                      for k in ("open", "high", "low", "close"))
                    bid, ask = _quote(_num(r.get("bid")), _num(r.get("ask")))
                    # Same no-trade / zero-bid rules as the wide path -- see _traded and
                    # fetch_underlying_eod_bars. Kept identical on purpose: the two shapes
                    # write into the SAME parquet store, so a divergence here would make a
                    # partition's meaning depend on which code path happened to fetch it.
                    if not _traded(close):
                        o = h = l = close = None
                    if close is None and bid is None and ask is None:
                        continue  # neither a trade nor a quote: no information at all
                    yield OptionEodBar(
                        occ_symbol=occ, bar_date=bar_date,
                        open=o, high=h, low=l, close=close,
                        volume=(int(_num(r.get("volume"))) if _num(r.get("volume")) is not None
                               else None),
                        bid=bid, ask=ask,   # see _quote: 0 ask == no quote, 0 bid == real quote
                        open_interest=oi_map.get((strike, _right_word(right), bar_date)),
                        iv=_clean_iv(r.get("implied_vol")),
                    )

    def fetch_underlying_eod_bars(self, underlying: str, *, start: date,
                                  end: date) -> Iterator[OptionEodBar]:
        """EVERY expiration of one underlying, via ``expiration="*"`` — the WIDE shape.

        This replaces the per-(underlying, expiry) loop in ``fetch_eod_bars`` for bulk
        backfills, and it is 40-68x faster. Measured live 2026-09-03 on AAPL:

            option_history_eod  exp="*"  365-day range   ~2,950 rows/s
            per-expiry loop, one whole unit                  43 rows/s effective

        The gap was never a server throughput ceiling (the earlier assessment's claim); it was
        per-request overhead across tens of thousands of small windows. One call returns the
        whole chain -- ~16 simultaneously-live expirations for a large-cap -- for every day in
        the window. Verified row-for-row identical to the per-expiry path over the same expiry
        and window: 0 rows lost, 0 extra, 0 differing closes.

        THREE endpoints, because the fields we need are split across them and only one of them
        takes a wide date range:

          * ``option_history_eod``            bars + bid/ask.  exp="*" + 365-day range: OK
          * ``option_history_open_interest``  open interest.   exp="*" + 365-day range: OK
          * ``option_history_greeks_eod``     implied_vol.     exp="*" is ONE DAY AT A TIME

        That last restriction is a hard server rule, not a size cap: measured live, neither
        ``max_dte`` (30/60/200) nor ``strike_range`` (5/20) nor both together lifts it at any
        range -- every combination returns "When expiration=*, you must request data a
        day-at-a-time". So IV costs one request per trading day while bars and OI cost one per
        window, and IV is consequently ~40% of a backfill's wall clock.

        WINDOW SIZE is ``_REQUEST_WINDOW_DAYS`` (30) rather than the server's 365-day cap even
        though throughput is flat between them (2,981 vs 2,949 rows/s measured). The reason is
        MEMORY, not speed: a 30-day window holds ~44k rows (~10 MB) in flight, a 365-day one
        ~565k (~130 MB), and a previous backfill was killed at 17.9 GB. Same speed, 13x less
        resident -- so the narrow window is free.

        Yields every contract the server returns, NOT only a caller-supplied contract list:
        the wide call reports the REAL listed chain, which is strictly better than the
        synthetic Friday x strike ladder our discovery guesses at.
        """
        client = self._get_client()
        underlying = underlying.upper()

        # NO CURRENT-DAY DATA IN THE WIDE SHAPE. ThetaData rejects a window touching TODAY
        # with "Cannot fetch current-day data without specifying an expiration"
        # (INVALID_ARGUMENT, measured live 2026-09-03) -- the restriction is specific to
        # expiration="*", which is exactly what this method uses.
        #
        # This has to be clamped here rather than left to the caller. A backfill's window ends
        # at "today" by default, so the LAST chunk of EVERY symbol would hit it -- and it hits
        # at the END, after the whole 6.7-year fetch has already been paid for. Observed live:
        # ABEV failed after 941 s having written nothing, and the error is permanent (not
        # transient), so the retry loop does not save it. Every symbol would have failed
        # identically and the run would have produced zero partitions.
        #
        # Yesterday, not today: an EOD bar for the current session does not exist until that
        # session closes, so nothing is lost by excluding it.
        #
        # "Today" is EXCHANGE-LOCAL, not machine-local, and that distinction is load-bearing.
        # Measured live: with the machine on CET, this clamp used date.today() and worked all
        # evening, then started failing again the moment local midnight passed -- 01:01 CET on
        # the 4th is 19:01 ET on the 3rd, so "local yesterday" (the 3rd) was still the CURRENT
        # day in New York and the server refused it. Every symbol processed after local
        # midnight lost its last windows to this. Any machine west of the Atlantic hits the
        # mirror image of the same bug.
        end = min(end, _exchange_today() - timedelta(days=1))
        if end < start:
            return

        for w_start, w_end in _chunk_window(start, end):
            try:
                bars_df = client.option_history_eod(
                    symbol=underlying, expiration="*", strike="*", right="both",
                    start_date=w_start, end_date=w_end)
            except self._no_data_exc:
                continue  # no chain at all in this window -- normal, not an error
            if bars_df is None or len(bars_df) == 0:
                continue

            try:
                oi_df = client.option_history_open_interest(
                    symbol=underlying, expiration="*", strike="*", right="both",
                    start_date=w_start, end_date=w_end)
            except self._no_data_exc:
                oi_df = None  # bars alone are still worth keeping
            oi_map = _index_open_interest(oi_df)

            # IV: one request per TRADING DAY (see the day-at-a-time rule above). Only days
            # that actually returned bars are asked for -- asking for a market holiday would
            # spend a request to be told there is nothing.
            iv_map: Dict[Tuple[float, str, date], float] = {}
            for day in sorted({d for d in (
                    _parse_date(v) for v in bars_df["created"]) if d is not None}):
                try:
                    greeks_df = client.option_history_greeks_eod(
                        symbol=underlying, expiration="*", strike="*", right="both",
                        start_date=day, end_date=day)
                except self._no_data_exc:
                    continue
                except Exception as e:  # noqa: BLE001
                    # IV is the one field we cannot re-derive, but a single bad day must not
                    # cost the whole window's bars -- log loudly and carry on without it.
                    logger.warning("thetadata: implied_vol fetch failed for %s %s: %s: %s",
                                   underlying, day, type(e).__name__, e)
                    continue
                iv_map.update(_index_implied_vol(greeks_df))

            # ORDERING CONTRACT: bars are yielded in NON-DECREASING bar_date order, across the
            # whole call. Windows are already walked oldest-first, but the server's row order
            # WITHIN a window is its own (contract-major, not date-major), so the frame is
            # sorted here to make the guarantee hold end to end.
            #
            # tools/warm_options_history.py depends on this: it writes an expiry's partition
            # as soon as a bar dated after that expiry arrives, which is only sound if no
            # earlier-dated bar can follow. Without the sort, a later date appearing early in
            # a window would close an expiry that still had rows further down the SAME window,
            # silently truncating that partition.
            # NOT wrapped in a try: a failure here must propagate. Yielding unsorted rows
            # would not look like an error, it would look like slightly smaller partitions --
            # the exact silent-truncation this ordering exists to prevent. `created` is
            # load-bearing anyway (it is the bar_date below), so a frame that cannot be sorted
            # by it has nothing usable in it.
            bars_df = bars_df.sort_values("created", kind="mergesort")

            for row in bars_df.itertuples(index=False):
                r = row._asdict()
                strike = _num(r.get("strike"))
                bar_date = _parse_date(r.get("created"))
                expiry = _parse_date(r.get("expiration"))
                right = r.get("right")
                if strike is None or bar_date is None or expiry is None:
                    continue
                o, h, l, close = (_num(r.get(k)) for k in ("open", "high", "low", "close"))
                bid, ask = _quote(_num(r.get("bid")), _num(r.get("ask")))
                if not _traded(close):
                    # ThetaData's EOD OHLC is a TRADE statistic and it reports 0.0 for a day
                    # the contract did not trade -- measured across 4 underlyings x 4 years,
                    # close > 0 iff volume > 0, with open/high/low 0.0 alongside. Passing that
                    # 0.0 through as a price marks a contract that merely did not trade as
                    # worthless; on a liquid chain that is 44.9% of rows, 28.3% of which are
                    # quoted at a median $60.75 mid. So the whole OHLC block becomes None
                    # ("did not trade"), and bid/ask below carry the day's real mark.
                    o = h = l = close = None
                if close is None and bid is None and ask is None:
                    # Neither a trade nor a quote: genuinely no information about this
                    # contract on this day. Measured at 0.0% of rows, but a bar with no price
                    # at all must not reach the store -- a chain row with no price silently
                    # skips the selector's penny-contract gate instead of being rejected.
                    continue
                key = (strike, _right_word(right), bar_date)
                yield OptionEodBar(
                    occ_symbol=_occ_symbol(underlying, expiry, right, strike),
                    bar_date=bar_date,
                    open=o, high=h, low=l, close=close,
                    volume=(int(_num(r.get("volume"))) if _num(r.get("volume")) is not None
                           else None),
                    bid=bid, ask=ask,   # see _quote: 0 ask == no quote, 0 bid == real quote
                    open_interest=oi_map.get(key),
                    iv=iv_map.get(key),
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
