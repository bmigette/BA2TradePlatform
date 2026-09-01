"""Historical option data from TastyTrade / dxfeed.

WHY THIS EXISTS. The offline options cache is missing liquidity data and has no real quotes:
its rows were written with ``bid = ask = close``, so not one of its 1,083,571 quoted chain
rows has ``ask > bid`` and there is no spread to model, and ``open_interest`` is NULL on all
1,440,782 chain rows so ``min_open_interest`` rejects entire chains.

THAT CLAIM USED TO BE WIDER THAN THE DATA SUPPORTS, and the correction is recorded here
rather than quietly dropped (re-measured 2026-08-31; full record in
``ba2_common.core.option_selector._publishes_spread``). This note previously said ``delta``
and ``iv`` were also NULL "across all 6,757,055 chain rows", and that ``method="delta"``
therefore selects nothing. Neither half is true of the cache on disk: the row count matches
no table in the file, and greeks are populated on 46% of the 1,440,782 chain rows and on
88.2% of the 19,484,995 ``option_bar`` rows, covering all 101 underlyings. Delta selection
works there. ``open_interest`` -- with no bar column to recover it -- is the one field that
is genuinely dead, and it alone is what this vendor switch was needed for. dxfeed's IV is
still worth having (a vendor IV beats a Black-Scholes inversion of the same bar's close),
but it is an improvement, not a rescue. Alpaca cannot
fix that (its history floors at a measured 2024-01-18 and its bars carry no IV), and
ThetaData needs a paid local terminal. A read-only probe
(``test_files/probe_tastytrade_option_history.py``) established that dxfeed can:

  * it serves daily candles for contracts that have ALREADY EXPIRED — the fact that makes a
    historical backfill possible at all;
  * those candles carry ``imp_volatility`` and ``open_interest``, the two fields that cannot
    be recovered from OHLC;
  * IV coverage floors out around October 2022 (hence ``history_floor``);
  * there is NO intraday data for dead contracts — daily only;
  * there is NO bid/ask for dead contracts, so ``bid``/``ask`` stay ``None`` here. Absent is
    honest; synthesizing a zero-width spread is what produced the fabricated option profits
    the arb guard was added for.

TRANSPORT. Two channels, both behind seams so every test runs offline:

  * REST (``/instruments/equity-options``) for contract discovery, which MUST pass
    ``with-expired=true`` — every contract in a 2023 window is dead today, so the default
    "currently tradable" listing returns an empty chain and silently builds an empty cache.
  * dxfeed WebSocket (``DXLinkStreamer.subscribe_candle``) for the bars. The socket needs an
    EXPLICIT certifi SSL context: the system trust store picks up a corporate root for the
    WebSocket while ``httpx`` uses certifi, so REST succeeds and the stream silently does not.

COMPLETION. dxfeed candles are ``IndexedEvent``s, so each subscription's history arrives as a
snapshot terminated by ``snapshotEnd``/``snapshotSnip``, and a contract with NO history
arrives as a single event carrying ``snapshotBegin | snapshotEnd | removeEvent``. That is how
"genuinely empty" is told apart from "did not answer yet" — the distinction the resumable
warm-up is built on. A contract that neither produced bars nor terminated its snapshot before
the socket died is UNRESOLVED, never empty: recording it as empty would bake a permanent hole
into the cache that no re-run revisits.
"""
from __future__ import annotations

import math
import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)
from ba2_common.logger import logger

#: dxfeed IndexedEvent flags (mirrored from tastytrade.dxfeed.event so the pure helpers here
#: do not need an SDK import just to read a bitmask).
REMOVE_EVENT = 0x2
SNAPSHOT_BEGIN = 0x4
SNAPSHOT_END = 0x8
SNAPSHOT_SNIP = 0x10

#: Measured IV-coverage floor. Prices reach further back, but IV — the whole point — does
#: not, and claiming depth we have no IV for would build a cache whose leading months
#: silently cannot do delta selection. Env-overridable for a probe that moves it.
DEFAULT_HISTORY_FLOOR = date(2022, 10, 1)

#: dxfeed serves no intraday data for dead contracts.
CANDLE_INTERVAL = "1d"

_OCC_RE = re.compile(r"^(?P<root>[A-Z][A-Z0-9]{0,5}?)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
_STREAMER_RE = re.compile(r"^\.(?P<root>[A-Z0-9]+?)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d+(?:\.\d+)?)$")


class StreamInterrupted(RuntimeError):
    """The candle stream died before every subscription finished.

    Carries whatever candles DID arrive so the caller keeps that work; the caller is
    responsible for treating the contracts that never terminated as UNRESOLVED.
    """

    def __init__(self, message: str, candles: Sequence[Any] = ()):
        super().__init__(message)
        self.candles = list(candles)


@dataclass
class CandleBatch:
    """The outcome of one ``fetch_bars_detailed`` call.

    ``empty`` and ``unresolved`` are what make resume trustworthy, and they must never be
    merged: ``empty`` is a durable fact to record, ``unresolved`` is work still owed.
    """
    bars: List[OptionEodBar] = field(default_factory=list)
    empty: Set[str] = field(default_factory=set)
    unresolved: Set[str] = field(default_factory=set)
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        """True when every requested contract was accounted for."""
        return not self.unresolved


# --------------------------------------------------------------------------- #
# symbol helpers (pure)
# --------------------------------------------------------------------------- #
def occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    """The canonical OCC id the rest of the platform keys on: ROOT + YYMMDD + C/P + strike*1000.

    Unpadded (``AAPL230120C00150000``), matching ThetaData's ``_occ_symbol`` and the existing
    caches — NOT the space-padded 21-char OCC form the TastyTrade SDK expects.
    """
    cp = "C" if str(right).upper().startswith("C") else "P"
    # Round in integer thousandths: 545.5 * 1000 is 545499.999... in binary float.
    thousandths = int(Decimal(str(float(strike))).scaleb(3).to_integral_value())
    return f"{underlying.upper()}{expiry:%y%m%d}{cp}{thousandths:08d}"


def parse_occ(occ: str) -> OptionContractMeta:
    """Recover a contract's identity from its OCC id. Raises ValueError on anything else."""
    m = _OCC_RE.match(str(occ).strip().upper())
    if not m:
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    yy, mm, dd = int(m["exp"][:2]), int(m["exp"][2:4]), int(m["exp"][4:6])
    return OptionContractMeta(
        occ_symbol=m.group(0),
        underlying=m["root"],
        option_type="call" if m["cp"] == "C" else "put",
        strike=int(m["strike"]) / 1000.0,
        expiry=date(2000 + yy, mm, dd),
    )


def occ_to_streamer(occ: str) -> str:
    """OCC -> the dxfeed streamer symbol (``.AAPL230120C150`` / ``.SPY240621P545.5``).

    Agrees with ``tastytrade.instruments.Option.occ_to_streamer_symbol`` (which takes the
    space-padded form); a divergence here means the streamer silently returns nothing.
    """
    m = parse_occ(occ)
    strike = f"{m.strike:.3f}".rstrip("0").rstrip(".")
    cp = "C" if m.option_type == "call" else "P"
    return f".{m.underlying}{m.expiry:%y%m%d}{cp}{strike}"


def streamer_to_occ(streamer: str) -> str:
    """The inverse of :func:`occ_to_streamer`."""
    m = _STREAMER_RE.match(strip_candle_suffix(str(streamer).strip().upper()))
    if not m:
        raise ValueError(f"not a dxfeed option streamer symbol: {streamer!r}")
    yy, mm, dd = int(m["exp"][:2]), int(m["exp"][2:4]), int(m["exp"][4:6])
    return occ_symbol(m["root"], date(2000 + yy, mm, dd), m["cp"], float(m["strike"]))


def strip_candle_suffix(event_symbol: str) -> str:
    """``.AAPL230120C150{=1d,tho=true}`` -> ``.AAPL230120C150``.

    dxfeed echoes the subscription's candle-period suffix back on every event; matching on
    the raw event symbol drops every bar on the floor.
    """
    return str(event_symbol).split("{", 1)[0]


# --------------------------------------------------------------------------- #
# candle -> bar (pure)
# --------------------------------------------------------------------------- #
def _f(value: Any) -> Optional[float]:
    """Decimal/str -> float, with NaN and None both meaning ABSENT (never 0.0)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _price(value: Any) -> Optional[float]:
    """A PRICE field, where zero means "not reported" rather than "free".

    The SDK annotates a Candle's OHLC as ``ZeroFromNone``, so a wire ``"NaN"`` does not
    arrive as ``None`` — it arrives as ``Decimal(0)``. Passing that through would mint a
    $0.00 option, which is exactly the artifact that produced fabricated option profits
    before the arb guard existed. ThetaData's provider applies the same rule to bid/ask.
    """
    f = _f(value)
    return None if f is None or f <= 0 else f


def _i(value: Any) -> Optional[int]:
    f = _f(value)
    return int(f) if f is not None else None


def is_empty_snapshot(candle: Any) -> bool:
    """True when dxfeed said "this contract has no history at all".

    Per the dxfeed IndexedEvent contract, an EMPTY snapshot is delivered as a single event
    with ``removeEvent`` set alongside ``snapshotEnd`` (or ``snapshotSnip``). Recognising it
    is what lets the warm-up record "genuinely empty" instead of retrying a dead strike on
    every run forever.
    """
    flags = int(getattr(candle, "event_flags", 0) or 0)
    return bool(flags & REMOVE_EVENT) and bool(flags & (SNAPSHOT_END | SNAPSHOT_SNIP))


def ends_snapshot(candle: Any) -> bool:
    """True when this event terminates the contract's snapshot (empty or not)."""
    flags = int(getattr(candle, "event_flags", 0) or 0)
    return bool(flags & (SNAPSHOT_END | SNAPSHOT_SNIP))


def candle_to_bar(candle: Any, occ: str,
                  tz: timezone = timezone.utc) -> Optional[OptionEodBar]:
    """One dxfeed candle -> one EOD bar, or None if it is not a usable bar.

    Not usable: a removal event (a deletion, not data) or a candle with no close. The bar
    date is the candle's ``time`` (epoch ms) as a UTC calendar date — for a daily US-equity
    candle every plausible dxfeed anchor (session open 09:30 ET, midnight ET, midnight UTC)
    lands on the same calendar day in UTC.
    """
    flags = int(getattr(candle, "event_flags", 0) or 0)
    if flags & REMOVE_EVENT:
        return None
    close = _price(getattr(candle, "close", None))
    if close is None:
        return None
    ms = getattr(candle, "time", None)
    if not ms:
        return None
    bar_date = datetime.fromtimestamp(int(ms) / 1000.0, tz).date()
    o = _price(getattr(candle, "open", None))
    h = _price(getattr(candle, "high", None))
    lo = _price(getattr(candle, "low", None))
    return OptionEodBar(
        occ_symbol=occ,
        bar_date=bar_date,
        open=o if o is not None else close,
        high=h if h is not None else close,
        low=lo if lo is not None else close,
        close=close,
        volume=_i(getattr(candle, "volume", None)),
        # There is NO historical bid/ask for a dead contract. Leave them absent rather than
        # synthesizing bid == ask == close, which is precisely the defect being replaced.
        bid=None,
        ask=None,
        open_interest=_i(getattr(candle, "open_interest", None)),
        iv=_f(getattr(candle, "imp_volatility", None)),
    )


# --------------------------------------------------------------------------- #
# offline discovery helpers (pure)
# --------------------------------------------------------------------------- #
def expiry_calendar(start: date, end: date) -> List[date]:
    """Every Friday in [start, end] — the standard weekly/monthly US equity expiry day.

    Used by the SYNTHETIC discovery fallback (no listing endpoint needed). Non-Friday
    expiries (Mon/Wed weeklies on the most liquid names, and Fridays moved by a holiday)
    are simply not generated; those contracts come back as empty snapshots and are recorded
    as such, which costs a request but never fabricates data.
    """
    if end < start:
        raise ValueError(f"reversed window: {start} .. {end}")
    first = start + timedelta(days=(4 - start.weekday()) % 7)
    out, d = [], first
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def _strike_increment(price: float) -> float:
    """The OCC strike spacing typical for a price level."""
    if price < 25:
        return 0.5
    if price < 50:
        return 1.0
    if price < 200:
        return 2.5
    if price < 500:
        return 5.0
    return 10.0


def strike_ladder(low: float, high: float, *, band_pct: float,
                  increment: Optional[float] = None) -> List[float]:
    """A strike ladder bracketing [low, high] widened by ``band_pct`` on each side.

    ``increment`` defaults to the spacing typical for the price level, so a $5 name gets
    $0.50 strikes and a $1000 name gets $10 ones instead of one ladder for everything.
    """
    if low <= 0 or high <= 0:
        raise ValueError(f"strike ladder needs positive prices, got {low} .. {high}")
    if high < low:
        low, high = high, low
    inc = float(increment) if increment else _strike_increment((low + high) / 2.0)
    lo = max(inc, low * (1.0 - band_pct / 100.0))
    hi = high * (1.0 + band_pct / 100.0)
    # BRACKET the band: floor the bottom and CEIL the top. Truncating the top would leave
    # the highest strikes in the requested band unfetched, which is invisible until a
    # strategy asks for a call above it and silently gets nothing.
    first = math.floor(lo / inc) * inc
    last = math.ceil(hi / inc) * inc
    out, s = [], first
    # Round to the increment's own precision so 0.1-style accumulation cannot drift.
    places = max(0, -Decimal(str(inc)).as_tuple().exponent)
    while s <= last + inc / 2:
        v = round(s, places)
        if v > 0:
            out.append(v)
        s += inc
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #
class TastyTradeOptionsProvider(OptionsDataProviderInterface):
    """Bulk historical option bars (daily, with IV + open interest) from dxfeed."""

    name = "tastytrade"

    def __init__(self, session: Any = None, *,
                 session_factory: Optional[Callable[[], Any]] = None,
                 history_floor_date: Optional[date] = None,
                 batch_size: int = 50,
                 quiet_seconds: float = 6.0,
                 hard_timeout: float = 180.0,
                 strict_snapshot: bool = False,
                 ssl_context: Optional[ssl.SSLContext] = None):
        # No connection is opened here: ``get_provider("options", "tastytrade")`` must be a
        # pure construction, not a network call.
        self._session = session
        self._session_factory = session_factory
        self._history_floor = history_floor_date or _floor_from_env()
        self.batch_size = int(batch_size)
        self.quiet_seconds = float(quiet_seconds)
        self.hard_timeout = float(hard_timeout)
        self.strict_snapshot = bool(strict_snapshot)
        self._ssl_context = ssl_context

    # -- interface ------------------------------------------------------
    def history_floor(self) -> date:
        return self._history_floor

    def discover_contracts(self, underlying: str, *, expiry_gte: date, expiry_lte: date,
                           strike_min: Optional[float] = None,
                           strike_max: Optional[float] = None,
                           max_contracts: Optional[int] = None) -> List[OptionContractMeta]:
        out: Dict[str, OptionContractMeta] = {}
        for item in self._list_instruments(underlying, expiry_gte, expiry_lte):
            meta = self._parse_instrument(item, underlying)
            if meta is None:
                continue
            if meta.expiry < expiry_gte or meta.expiry > expiry_lte:
                continue
            if strike_min is not None and meta.strike < strike_min:
                continue
            if strike_max is not None and meta.strike > strike_max:
                continue
            out.setdefault(meta.occ_symbol, meta)
        return _cap_near_the_money(list(out.values()), strike_min, strike_max, max_contracts)

    def fetch_eod_bars(self, contracts: Iterable[OptionContractMeta], *,
                       start: date, end: date) -> Iterator[OptionEodBar]:
        yield from self.fetch_bars_detailed(contracts, start=start, end=end).bars

    # -- the resume-aware form the warm-up uses -------------------------
    def fetch_bars_detailed(self, contracts: Iterable[OptionContractMeta], *,
                            start: date, end: date) -> CandleBatch:
        """Like :meth:`fetch_eod_bars`, but also reports which contracts were EMPTY and
        which are UNRESOLVED (the stream died before they answered)."""
        wanted = {c.occ_symbol: occ_to_streamer(c.occ_symbol) for c in contracts}
        batch = CandleBatch()
        if not wanted:
            return batch

        by_streamer = {v: k for k, v in wanted.items()}
        from_time = datetime.combine(start, time(0, 0), tzinfo=timezone.utc)
        symbols = list(wanted.values())

        for chunk in _chunks(symbols, self.batch_size):
            interrupted = False
            try:
                candles = list(self._collect(chunk, from_time))
            except StreamInterrupted as e:
                candles, interrupted = list(e.candles), True
                batch.interrupted = True
                logger.warning("candle stream interrupted over %d symbols: %s",
                               len(chunk), e)

            got: Set[str] = set()
            terminated: Set[str] = set()
            for c in candles:
                streamer = strip_candle_suffix(getattr(c, "event_symbol", ""))
                occ = by_streamer.get(streamer)
                if occ is None:
                    continue  # a symbol we did not ask for
                if ends_snapshot(c):
                    terminated.add(occ)
                if is_empty_snapshot(c):
                    continue
                bar = candle_to_bar(c, occ)
                if bar is None:
                    continue
                if bar.bar_date < start or bar.bar_date > end:
                    continue
                got.add(occ)
                batch.bars.append(bar)

            for streamer in chunk:
                occ = by_streamer[streamer]
                if occ in got:
                    if interrupted and occ not in terminated:
                        # Bars arrived but the snapshot never closed: the series is
                        # TRUNCATED. Keeping the rows and calling it done would cache a
                        # short history that no re-run ever repairs.
                        batch.unresolved.add(occ)
                    continue
                if occ in terminated:
                    batch.empty.add(occ)      # dxfeed said explicitly: nothing here
                elif interrupted or self.strict_snapshot:
                    batch.unresolved.add(occ)  # unknown — owed, not empty
                else:
                    # A clean drain with no events at all: the contract does not exist.
                    batch.empty.add(occ)
        # Rows for a truncated contract are not trustworthy; drop them so a retry writes a
        # whole series rather than merging onto a partial one.
        if batch.unresolved:
            batch.bars = [b for b in batch.bars if b.occ_symbol not in batch.unresolved]
        return batch

    # -- ssl ------------------------------------------------------------
    def ssl_context(self) -> ssl.SSLContext:
        """An SSL context pinned to certifi.

        The system trust store picks up a corporate root for the WebSocket while ``httpx``
        uses certifi, so REST works and the stream silently does not — a failure that costs
        an afternoon to diagnose because nothing errors, it just returns no data.
        """
        if self._ssl_context is None:
            import certifi
            self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        return self._ssl_context

    # -- transport: REST discovery --------------------------------------
    def _list_instruments(self, underlying: str, expiry_gte: date,
                          expiry_lte: date) -> List[dict]:
        """Raw ``/instruments/equity-options`` items for ``underlying``, expired INCLUDED."""
        items: List[dict] = []
        offset, total_pages = 0, 1
        while offset < total_pages:
            payload = self._rest_get("/instruments/equity-options", {
                "underlying-symbol[]": underlying.upper(),
                "with-expired": True,
                "per-page": 1000,
                "page-offset": offset,
            })
            data = (payload or {}).get("data") or {}
            items.extend(data.get("items") or [])
            pagination = (payload or {}).get("pagination") or {}
            total_pages = int(pagination.get("total-pages") or 1)
            offset += 1
            if offset > 500:  # pragma: no cover - runaway guard
                logger.warning("stopping instrument pagination for %s at 500 pages",
                               underlying)
                break
        return items

    def _rest_get(self, path: str, params: dict) -> dict:  # pragma: no cover - network
        session = self._require_session()
        return _run_sync(session._get(path, params=params))

    @staticmethod
    def _parse_instrument(item: dict, underlying: str) -> Optional[OptionContractMeta]:
        """One raw instrument item -> contract meta, or None if it must be skipped.

        Skips NON-STANDARD deliverables (``shares-per-contract != 100``): a post-corporate-
        action contract does not price like the ordinary one and is not what any strategy
        selects. Alpaca's builder rejects the same class via its ``1SPY...`` adjusted roots.
        """
        try:
            spc = item.get("shares-per-contract")
            if spc is not None and int(spc) != 100:
                return None
            root = str(item.get("underlying-symbol") or underlying).upper()
            raw = str(item.get("symbol") or "")
            occ_root = raw[:6].split()[0] if raw else root
            if occ_root and occ_root != root:
                return None  # an adjusted root such as "AAPL1"
            expiry = date.fromisoformat(str(item["expiration-date"]))
            strike = float(item["strike-price"])
            right = str(item.get("option-type") or "").upper()
            if right not in ("C", "P"):
                return None
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        return OptionContractMeta(
            occ_symbol=occ_symbol(root, expiry, right, strike),
            underlying=root,
            option_type="call" if right == "C" else "put",
            strike=strike,
            expiry=expiry,
        )

    # -- transport: dxfeed candles --------------------------------------
    def _collect(self, streamer_symbols: Sequence[str],
                 from_time: datetime) -> List[Any]:  # pragma: no cover - network
        """Subscribe to ``streamer_symbols`` and drain their candle snapshots.

        Returns once every symbol has terminated its snapshot, or after ``quiet_seconds``
        with no events at all, or at ``hard_timeout``. A socket failure raises
        :class:`StreamInterrupted` carrying whatever had already arrived, so the caller
        keeps that work and marks the rest as owed.
        """
        return _run_sync(self._collect_async(list(streamer_symbols), from_time))

    async def _collect_async(self, streamer_symbols: List[str],
                             from_time: datetime) -> List[Any]:  # pragma: no cover - network
        import asyncio

        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle

        session = self._require_session()
        collected: List[Any] = []
        outstanding = set(streamer_symbols)
        deadline = asyncio.get_running_loop().time() + self.hard_timeout

        try:
            async with DXLinkStreamer(session, ssl_context=self.ssl_context()) as streamer:
                await streamer.subscribe_candle(streamer_symbols, CANDLE_INTERVAL,
                                                from_time)
                while outstanding:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        candle = await asyncio.wait_for(
                            streamer.get_event(Candle),
                            timeout=min(self.quiet_seconds, remaining))
                    except asyncio.TimeoutError:
                        break  # quiet drain: nothing more is coming
                    collected.append(candle)
                    if ends_snapshot(candle):
                        outstanding.discard(
                            strip_candle_suffix(getattr(candle, "event_symbol", "")))
        except Exception as e:
            raise StreamInterrupted(f"{type(e).__name__}: {e}", collected) from e
        return collected

    # -- session --------------------------------------------------------
    def _require_session(self) -> Any:  # pragma: no cover - network
        if self._session is None:
            if self._session_factory is None:
                raise RuntimeError(
                    "TastyTradeOptionsProvider has no session. Pass session=... or "
                    "session_factory=... (credentials live in the TastyTrade account's "
                    "settings, or in TT_CLIENT_SECRET / TT_REFRESH_TOKEN)."
                )
            self._session = self._session_factory()
        return self._session


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #
def _floor_from_env() -> date:
    raw = os.getenv("TASTYTRADE_OPTIONS_HISTORY_FLOOR")
    if not raw:
        return DEFAULT_HISTORY_FLOOR
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("ignoring unparseable TASTYTRADE_OPTIONS_HISTORY_FLOOR=%r", raw)
        return DEFAULT_HISTORY_FLOOR


def _chunks(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _cap_near_the_money(contracts: List[OptionContractMeta],
                        strike_min: Optional[float], strike_max: Optional[float],
                        max_contracts: Optional[int]) -> List[OptionContractMeta]:
    """Apply ``max_contracts`` keeping the strikes NEAREST the band centre.

    Near-the-money is what strategies select; an arbitrary slice would spend the whole cap
    on wings. Same rule ``ThetaDataOptionsProvider`` follows.
    """
    if max_contracts is None or len(contracts) <= max_contracts:
        return contracts
    if strike_min is not None and strike_max is not None:
        centre = (strike_min + strike_max) / 2.0
    else:
        strikes = sorted(c.strike for c in contracts)
        centre = strikes[len(strikes) // 2]
    return sorted(contracts, key=lambda c: (abs(c.strike - centre), c.occ_symbol))[:max_contracts]


# One persistent background event loop per PROCESS, lazily started on first use and reused
# for the rest of the process's life. _require_session() caches self._session (and, inside
# it, the tastytrade SDK's Session with its long-lived httpx.AsyncClient / DXLink websocket)
# across every call for as long as a provider instance lives -- warm_options_history.py
# builds ONE provider and calls fetch_bars_detailed on it thousands of times. A socket
# transport (what that cached client/websocket ultimately rests on) is bound to the event
# loop that was running when it was opened; a FRESH `asyncio.run()` per call closes that loop
# the instant the call returns, so the cached session's connection is dead before the very
# next call touches it. Observed live as "RuntimeError: Event loop is closed" on every warm-up
# batch after the first, across all 8 parallel workers (2026-08-30). Routing every call
# through the SAME loop instead keeps the cached session's connection alive for as long as the
# process runs, matching what run_option_warmup_parallel.py's own docstring already promises
# ("its own asyncio event loop" -- one per worker, not one per call).
import threading

_bg_loop = None  # type: ignore[var-annotated]  # asyncio.AbstractEventLoop, once started
_bg_loop_lock = threading.Lock()


def _background_loop():  # pragma: no cover - network
    """Lazily start (once) and return the persistent per-process background event loop."""
    import asyncio
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="tastytrade-bg-loop", daemon=True)
            thread.start()
            _bg_loop = loop
        return _bg_loop


def _run_sync(coro):  # pragma: no cover - network
    """Run a coroutine from sync code, on the ONE persistent background loop for this
    process. Tolerates an already-running loop (the rare nested case: sync code called from
    within already-async code) via a one-off thread-pool run, same as before -- that path
    never touches the cached session, so it does not need the persistent loop."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()
