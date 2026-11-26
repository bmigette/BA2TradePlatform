"""The Overview "Heatmap" tab's rules: one treemap tile per symbol the account holds.

PURE: no NiceGUI, no database, no network. The loader (``services.position_heatmap``) turns the open transactions,
the account's prices, the daily history, the market caps, the split calendar and the instrument labels into the
plain inputs below; everything that decides a number, a colour, a size or a remembered choice is here and
unit-tested (``tests/test_position_heatmap.py``). The tab (``ui.components.PositionHeatmapTab``) only draws the
result.

THE BROKER'S POSITION IS WHAT IS HELD. The owner buys through the platform and directly at the broker, so the
platform's transactions are not a complete book. Per account and symbol:
  * all experts: the tile quantity is the BROKER's position (signed); every symbol the broker holds gets a tile; a
    symbol open on the platform that the broker does not hold gets none and is listed;
  * an expert filter: a named expert's quantity is its open platform transactions (only the platform can attribute a
    share to an expert); "No expert" is the broker's quantity MINUS every named expert's transactions. When the
    experts' records exceed the broker's position nothing is clamped: the experts keep their platform quantity, "No
    expert" gets none, and the symbol is listed;
  * an account whose broker positions could not be read falls back to the platform's transaction quantities, and
    the caption says so for that account.
The platform-vs-broker quantity differences are listed (``quantity_mismatches``) for information.

Periods (NYSE calendar, ``ba2_common.core.market_calendar``), measured at ``now`` (a tz-aware moment; a bare date
means "that day, after the close"):
  * today -- the current session while it is open or after its close; before the open, and on a weekend /
    holiday, the LAST COMPLETED session -- always against the close of the session before it;
  * week / month / year -- from Monday / the 1st / 1 January, against the last session BEFORE that day.
The reference close is the daily close OF the reference session, read by date, never "the latest bar".

Colour metric:
  * ``price`` -- current price / reference close - 1, independent of when the position was bought;
  * ``pnl``   -- the position's P/L over the period, counting fills inside it. With signed quantities
    (long +, short -): P/L = q_now x price - q_start x ref - cost of buys in the period + proceeds of sells in the
    period, where q_start = q_now - the signed fills inside the period. Percent = P/L / (|q_start| x ref + the
    notional of the fills that OPENED exposure inside the period: buys for a long, short sales for a short).

WHEN A FILL HAPPENED. The platform stores no fill time, only when each order was PLACED. A fill cannot precede its
order, so a fill whose order was placed inside the period is inside it. A MARKET order fills when placed (or at the
next open when placed outside the session). A filled LIMIT / STOP order placed BEFORE the period has an unknown fill
date -- unless ``seen_by`` (when the platform saw it filled: the entry's ``Transaction.open_date``) is before the
period -- so "My P/L" for that symbol and period is n/a, never a guessed number. Every lot's recorded fills must also
add up to its open quantity, or its P/L is n/a.

A split dated inside the period makes BOTH metrics n/a: the history is split-adjusted as of its fetch while broker
prices and the platform's fills are as traded, and nothing here can prove the two are on one basis.

WHOSE FILLS. For an account whose broker's own transaction history is stored (``broker_history``: TastyTrade,
``services.broker_share_history``), the broker's WHOLE position (all experts, or "No expert" when no named expert
holds the symbol) is measured from that history: the period starts at the reference session's CLOSE
(``regular_session_close_utc``), a share movement after it is inside; start quantity = broker quantity now - the
signed movements inside. Only an ``Equity`` row can change a tile (``TastyTradeAccount.classify_transaction``): an
equity trade or a reinvested dividend is COUNTED, rows of other instruments are ignored, every other equity row is a
REFUSAL row -- except an option assignment / exercise, an Equity Offering / Warrant row and a plain share split of a
named symbol, which refuse only the tile of their underlying / symbol when it is held (kind ``tile_only``: nothing when it is not). "My P/L" is n/a (with which reason) when: the history could not be read / is still loading / its sync
did not start after the positions read; it does not provably reach back to the period start (coverage is per
period: Today / This week stay computable when the year is not covered); a refusal row falls inside the period --
on the tile of its symbol or underlying, or on EVERY tile of the account when it is a corporate action or maps to no
held symbol; a transaction that was reversed, or reverses another, falls inside the period; a movement inside has no
price; a movement is newer than the positions read (minus ``RACE_MARGIN_S``: "position changing right now,
reload"); the platform has a fill for the symbol that no broker row matches (``platform_fill_missing``, a
BEST-EFFORT check), or a platform order created in the 30 minutes before the positions read has no broker rows of
its own (``recent_fill_missing``: "a recent platform fill is not in the broker history yet, reload"); or the start quantity lands on the other side. KNOWN AND ACCEPTED: a trade placed directly at
the broker can take a few minutes to reach the broker's history (the caption says so). Every other position is
computed from the PLATFORM's fills, and only when the platform's records cover the broker's whole position for that
symbol and account (within ``COVER_TOLERANCE``); otherwise it is n/a: "platform records cover X of the broker's Y
shares". Each tile's tooltip names its source. With an expert filter "My P/L" uses that expert's platform fills, and "No expert" uses the
platform's no-expert records only when they cover its whole remainder. Dividends and fees are not counted. When a
symbol's split calendar could not be read, its "My P/L" for a period longer than one session is n/a.

A transaction whose account cannot be resolved is never added to a symbol a broker holds (listed, not counted), and
a dust position (under 1e-6 shares, or worth under one cent) gets no tile.

Size mode: ``company`` -- the company's market cap (default, like a market heat map); ``exposure`` -- the
position's market value. An unknown size is a small minimum tile with a neutral border, listed under the chart.
Nothing is dropped silently, no missing or non-finite number becomes a 0, and options are counted, not drawn.
"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ba2_common.core.market_calendar import (
    NY_TZ, is_regular_session, next_regular_session, nyse_regular_sessions, prior_regular_session,
    regular_session_close_utc,
)

# --------------------------------------------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------------------------------------------

TIMEFRAMES: Dict[str, str] = {"today": "Today", "week": "This week", "month": "This month", "year": "This year"}
METRICS: Dict[str, str] = {"price": "Price change", "pnl": "My P/L"}
SIZE_MODES: Dict[str, str] = {"company": "Company size", "exposure": "My exposure"}
DEFAULT_TIMEFRAME = "today"
DEFAULT_METRIC = "price"
DEFAULT_SIZE = "company"

#: The expert key of a transaction no expert owns (portfolio allocator / manual / broker-imported).
NO_EXPERT = "No expert"
#: The label key of a symbol that carries no instrument label (the Overview's own convention).
UNLABELED = "Unlabeled"

#: Colour saturates at +/- this percent per timeframe, so one outlier does not wash out the rest.
CLAMP_PCT: Dict[str, float] = {"today": 3.0, "week": 6.0, "month": 10.0, "year": 30.0}

#: An unknown size is drawn as this share of the known total (or 1.0 each when nothing is known).
MIN_TILE_SHARE = 0.005

# Diverging red -> neutral -> green on the dark page (the percent is printed on every tile, so colour is
# never the only carrier of the sign).
RED = "#f63538"
NEUTRAL = "#414554"
GREEN = "#30cc5a"
NA_COLOR = "#262a35"
TILE_BORDER = "#1a1f2e"
UNKNOWN_SIZE_BORDER = "#cbd5e1"

STORAGE_KEY = "overview_heatmap_state"

#: ``today_status`` values
OPEN, PRE_OPEN, AFTER_CLOSE, NO_SESSION = "open", "pre_open", "after_close", "no_session"

_EPS = 1e-6
_NO_REFERENCE = "no reference close for the quantity held at the period start"
_RICH_TEXT = re.compile(r"[{}|]")

Moment = Union[datetime, date]


# --------------------------------------------------------------------------------------------------------------
# inputs / outputs
# --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    """One executed fill of a transaction's order. ``side`` 'BUY' | 'SELL'; ``qty`` > 0; ``price`` per share
    (None = the platform has no fill price); ``placed`` when its order was created (tz-aware); ``is_market`` a
    market order; ``seen_by`` the fill happened no later than this (None = unknown)."""
    side: str
    qty: float
    price: Optional[float]
    placed: datetime
    is_market: bool = True
    seen_by: Optional[datetime] = None


@dataclass(frozen=True)
class Lot:
    """One open transaction. ``direction`` 'long' | 'short'; ``quantity`` the open quantity (positive; None =
    unknown); ``expert`` its expert's display name or :data:`NO_EXPERT`; ``account_id`` None = unresolved."""
    transaction_id: int
    symbol: str
    expert: str
    direction: str
    quantity: Optional[float]
    fills: Tuple[Fill, ...] = ()
    is_option: bool = False
    account_id: Optional[int] = None


@dataclass(frozen=True)
class ShareMove:
    """One change of an equity position's quantity in the broker's own transaction history: ``quantity`` signed
    (+ in, - out), ``price`` per share (None = the row has no trade price), ``executed_at`` tz-aware UTC, ``kind``
    trade (counted) | split (refuses the account) | other (refuses its tile, or the account when it maps to no held
    symbol) | tile_only (refuses its held tile only, nothing when none is held)."""
    symbol: str
    quantity: float
    price: Optional[float]
    executed_at: Optional[datetime]
    kind: str
    detail: str = ""
    #: the broker's transaction id, the option's underlying (a refusal row lands there), the id this row reverses
    id: Optional[int] = None
    underlying: Optional[str] = None
    reverses_id: Optional[int] = None


@dataclass(frozen=True)
class PlatformOrder:
    """A platform order of the account that filled (``quantity`` = its filled quantity), ``side`` 'BUY' | 'SELL',
    ``created_at`` tz-aware: the broker history must show it (cross-check only)."""
    symbol: str
    side: str
    quantity: float
    created_at: datetime


@dataclass(frozen=True)
class BrokerHistory:
    """An account's share movements since ``covers_from`` (tz-aware; None = unknown), read AFTER its positions read,
    which finished at ``positions_at``; ``failure`` = why the read is unusable."""
    moves: Tuple[ShareMove, ...] = ()
    covers_from: Optional[datetime] = None
    positions_at: Optional[datetime] = None
    failure: Optional[str] = None


@dataclass(frozen=True)
class MarketInputs:
    """Per-symbol data the loader gathered. A symbol absent from a value map is unknown; its ``*_missing`` entry
    (when present) says why. ``broker_qty`` holds ``{account: {symbol: signed qty}}`` for the accounts whose broker
    positions were read (an account that is absent was not compared)."""
    prices: Mapping[str, float]
    price_missing: Mapping[str, str]
    closes: Mapping[str, Mapping[str, float]]          # {symbol: {YYYY-MM-DD: close}}
    closes_missing: Mapping[str, str]
    market_caps: Mapping[str, float]
    market_caps_missing: Mapping[str, str]
    labels: Mapping[str, Sequence[str]]
    splits: Mapping[str, Sequence[date]] = field(default_factory=dict)
    splits_missing: Mapping[str, str] = field(default_factory=dict)
    broker_qty: Mapping[int, Mapping[str, float]] = field(default_factory=dict)
    #: account -> why its broker positions could not be read (its tiles use the platform's quantities)
    broker_failed: Mapping[int, str] = field(default_factory=dict)
    #: account -> option rows in its broker positions (excluded, counted)
    broker_option_rows: Mapping[int, int] = field(default_factory=dict)
    #: account -> its broker transaction history (only accounts that have one: TastyTrade)
    broker_history: Mapping[int, BrokerHistory] = field(default_factory=dict)
    #: account -> the platform's filled orders (cross-checked against the broker history)
    platform_orders: Mapping[int, Sequence[PlatformOrder]] = field(default_factory=dict)


@dataclass(frozen=True)
class HeatmapState:
    """The user's choices. Empty ``experts`` / ``labels`` = all."""
    timeframe: str = DEFAULT_TIMEFRAME
    metric: str = DEFAULT_METRIC
    size: str = DEFAULT_SIZE
    experts: Tuple[str, ...] = ()
    labels: Tuple[str, ...] = ()


@dataclass
class Tile:
    symbol: str
    quantity: float                      # net signed (short < 0)
    current_price: Optional[float]
    reference_close: Optional[float]
    market_value: Optional[float]        # gross |qty| x price
    market_cap: Optional[float]
    price_change_pct: Optional[float]
    pnl_amount: Optional[float]
    pnl_pct: Optional[float]
    color_pct: Optional[float]           # the active metric's percent (None = n/a)
    size: float
    size_known: bool
    experts: Tuple[str, ...]
    labels: Tuple[str, ...]
    label_text: str = ""
    #: where "My P/L" comes from: "the broker's transaction history" / "the platform's fills" (or both)
    pnl_source: Optional[str] = None


@dataclass
class HeatmapView:
    state: HeatmapState
    period_start: date
    reference_session: date
    clamp: float
    today_status: str = AFTER_CLOSE
    tiles: List[Tile] = field(default_factory=list)
    issues: List[Tuple[str, str]] = field(default_factory=list)   # (symbol, reason), in symbol order
    options_excluded: int = 0
    #: (symbol, account, platform qty, broker qty) where they differ -- whole account, every expert
    quantity_mismatches: List[Tuple[str, int, float, float]] = field(default_factory=list)
    #: per-account notes for the caption (a broker book that could not be read)
    broker_notes: List[str] = field(default_factory=list)
    #: whether any account's "My P/L" is measured from the broker's transaction history
    broker_history_used: bool = False


# --------------------------------------------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------------------------------------------

def _positive(value: Any) -> Optional[float]:
    """``value`` as a float when it is a finite number above zero, else None (a price, a cap, a quantity)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# --------------------------------------------------------------------------------------------------------------
# periods
# --------------------------------------------------------------------------------------------------------------

def _check_timeframe(timeframe: str) -> None:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown heatmap timeframe {timeframe!r}; expected one of {list(TIMEFRAMES)}")


def _split_moment(now: Moment) -> Tuple[date, Optional[datetime]]:
    """``(New York date, the moment or None)``; a bare date is "that day after the close"."""
    if isinstance(now, datetime):
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError(f"the heatmap needs a timezone-aware moment, got naive {now!r}")
        return now.astimezone(NY_TZ).date(), now
    if isinstance(now, date):
        return now, None
    raise TypeError(f"now must be a datetime or a date, got {type(now).__name__}")


def _session_hours(day: date) -> Optional[Tuple[datetime, datetime]]:
    rows = nyse_regular_sessions(day, day)
    return rows[0] if rows else None


def today_status(now: Moment) -> Tuple[str, date]:
    """``(status, the session "Today" shows)``: ``open`` / ``after_close`` -> today's session; ``pre_open`` (a
    session day before the bell) / ``no_session`` (weekend, holiday) -> the last completed session."""
    today, moment = _split_moment(now)
    if is_regular_session(today):
        if moment is None:
            return AFTER_CLOSE, today
        opens, closes = _session_hours(today)
        if moment < opens:
            return PRE_OPEN, prior_regular_session(today)
        return (OPEN if moment < closes else AFTER_CLOSE), today
    return NO_SESSION, prior_regular_session(today)


def period_start(timeframe: str, now: Moment) -> date:
    """The first day the period counts (fills on/after it are "inside the period")."""
    _check_timeframe(timeframe)
    today, _moment = _split_moment(now)
    if timeframe == "today":
        return today_status(now)[1]
    if timeframe == "week":
        return date.fromordinal(today.toordinal() - today.weekday())
    if timeframe == "month":
        return today.replace(day=1)
    return today.replace(month=1, day=1)


def reference_session(timeframe: str, now: Moment) -> date:
    """The NYSE session whose close the period is measured from: the last session strictly before its start."""
    return prior_regular_session(period_start(timeframe, now))


def history_start(now: Moment) -> date:
    """The earliest daily close any timeframe needs (fetch history from here once, serve every timeframe)."""
    return min(reference_session(tf, now) for tf in TIMEFRAMES)


def warm_calendar(now: Moment) -> None:
    """Build the calendar tables this moment needs (call it from a worker thread, never the event loop)."""
    history_start(now)
    today_status(now)


def _ny_day(moment: datetime) -> date:
    return _split_moment(moment)[0]


def market_fill_day(placed: datetime) -> date:
    """The session a MARKET order placed at ``placed`` fills in: that day's when placed before its close, else
    the next session's open."""
    day = _ny_day(placed)
    if is_regular_session(day) and placed < _session_hours(day)[1]:
        return day
    return next_regular_session(day)


def fill_timing(fill: Fill, start: date) -> str:
    """``inside`` | ``before`` | ``unknown``: where ``fill`` falls against a period starting on ``start``."""
    if fill.is_market:
        return "inside" if market_fill_day(fill.placed) >= start else "before"
    if _ny_day(fill.placed) >= start:
        return "inside"                       # a fill cannot precede its order
    if fill.seen_by is not None and _ny_day(fill.seen_by) < start:
        return "before"
    return "unknown"


# --------------------------------------------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------------------------------------------

def _symbol_labels(labels: Mapping[str, Sequence[str]], symbol: str) -> Tuple[str, ...]:
    found = tuple(sorted({str(l).strip() for l in (labels.get(symbol) or ()) if str(l).strip()}))
    return found or (UNLABELED,)


#: Platform records "cover" a position when they match it within this fraction (fractional shares round).
COVER_TOLERANCE = 1e-4
#: A position under this many shares, or worth under this many dollars, is dust: no tile.
DUST_QTY = 1e-6
DUST_VALUE = 0.01
#: A broker movement this close to (or after) the positions read means the position is changing as it is read.
RACE_MARGIN_S = 60.0
#: The loader reads the platform's filled orders from this long before the earliest boundary (the pure check then
#: keeps only those created inside the history's proven coverage).
ORDER_LOOKBACK_S = 3 * 24 * 3600.0
#: A platform order created this long before the positions read must have its OWN broker row(s) ...
RECENT_ORDER_S = 30 * 60.0
#: ... executed from this long before its creation to this long after the positions read.
RECENT_MATCH_SKEW_S = 10 * 60.0
RECENT_FILL_MISSING = "a recent platform fill is not in the broker history yet, reload"
BROKER_HISTORY_DELAY_NOTE = ("My P/L from the broker's transaction history: a trade placed directly at the broker can "
                             "take a few minutes to reach the broker's history.")
#: A broker row may be timestamped this long before the platform order it fills was created (clock skew).
ORDER_SKEW_S = 300.0
PLATFORM_FILL_MISSING = "a platform fill is not in the broker history yet"
SOURCE_BROKER = "the broker's transaction history"
SOURCE_PLATFORM = "the platform's fills"


def _signed_qty(lot: Lot) -> float:
    return lot.quantity if lot.direction == "long" else -lot.quantity


def _tol(target: float) -> float:
    return max(_EPS, COVER_TOLERANCE * abs(target))


def _named_quantities(lots: Iterable[Lot], account_id: int) -> Dict[str, float]:
    """``{symbol: signed qty}`` of the NAMED experts' open equity transactions on ``account_id``."""
    out: Dict[str, float] = {}
    for l in lots:
        if l.account_id == account_id and not l.is_option and l.expert != NO_EXPERT and _positive(l.quantity):
            out[l.symbol] = out.get(l.symbol, 0.0) + _signed_qty(l)
    return out


def _has_no_expert_share(lots: Sequence[Lot], market: MarketInputs) -> bool:
    for account_id, book in market.broker_qty.items():
        named = _named_quantities(lots, account_id)
        for symbol, qty in book.items():
            q = _finite(qty)
            if q is not None and abs(q - (named[symbol] if symbol in named else 0.0)) > _tol(q):
                return True
    return False


def available_experts(lots: Iterable[Lot], market: Optional[MarketInputs] = None) -> List[str]:
    """Expert keys to offer, sorted, with :data:`NO_EXPERT` last: the experts of ``lots``, plus "No expert" when a
    broker position holds shares no named expert's transactions account for."""
    lots = list(lots)
    keys = {l.expert for l in lots}
    if market is not None and _has_no_expert_share(lots, market):
        keys.add(NO_EXPERT)
    return sorted(k for k in keys if k != NO_EXPERT) + ([NO_EXPERT] if NO_EXPERT in keys else [])


def available_labels(lots: Iterable[Lot], labels: Mapping[str, Sequence[str]],
                     market: Optional[MarketInputs] = None) -> List[str]:
    """Labels of the symbols in ``lots`` and in the broker positions, sorted, with :data:`UNLABELED` last."""
    symbols = {l.symbol for l in lots}
    if market is not None:
        symbols |= {s for book in market.broker_qty.values() for s in book}
    keys = {lbl for s in symbols for lbl in _symbol_labels(labels, s)}
    return sorted(k for k in keys if k != UNLABELED) + ([UNLABELED] if UNLABELED in keys else [])


def _label_ok(symbol: str, state: HeatmapState, labels: Mapping[str, Sequence[str]]) -> bool:
    return not state.labels or bool(set(_symbol_labels(labels, symbol)) & set(state.labels))


# --------------------------------------------------------------------------------------------------------------
# the maths
# --------------------------------------------------------------------------------------------------------------

def _signed(fill) -> float:
    return fill.qty if fill.side == "BUY" else -fill.qty


def _lot_pnl(lot: Lot, price: float, ref: Optional[float], start: date) -> Tuple[Optional[float], Optional[float],
                                                                                   Optional[str]]:
    """``(pnl, denominator, reason)`` of one lot over the period (reason set = not measurable)."""
    sign = 1.0 if lot.direction == "long" else -1.0
    q_now = sign * float(lot.quantity)
    if any(_positive(f.qty) is None for f in lot.fills):
        return None, None, f"transaction {lot.transaction_id} has a fill without a usable quantity"
    total = sum(_signed(f) for f in lot.fills)
    if abs(total - q_now) > _EPS * max(1.0, abs(q_now)):
        return None, None, (f"the recorded fills ({total:g}) do not reconcile with the open quantity "
                            f"({q_now:g}) of transaction {lot.transaction_id}")
    timed = [(f, fill_timing(f, start)) for f in lot.fills]
    unknown = [f for f, when in timed if when == "unknown"]
    if unknown:
        placed = min(_ny_day(f.placed) for f in unknown).isoformat()
        return None, None, (f"a limit/stop order placed before the period filled at an unknown date "
                            f"(placed {placed}, transaction {lot.transaction_id})")
    inside = [f for f, when in timed if when == "inside"]
    if any(_positive(f.price) is None for f in inside):
        return None, None, "a fill inside the period has no fill price"
    q_start = q_now - sum(_signed(f) for f in inside)
    if sign * q_start < -_EPS:
        return None, None, f"transaction {lot.transaction_id} held the opposite side at the period start"
    if abs(q_start) <= _EPS:
        q_start = 0.0
    if q_start and ref is None:
        return None, None, _NO_REFERENCE
    start_value = q_start * ref if q_start else 0.0
    buys = sum(f.qty * f.price for f in inside if f.side == "BUY")
    sells = sum(f.qty * f.price for f in inside if f.side == "SELL")
    opening = buys if sign > 0 else sells
    pnl = q_now * price - start_value - buys + sells
    return pnl, abs(start_value) + opening, None


def _lots_pnl(lots: Sequence[Lot], price: float, ref: Optional[float], start: date):
    total, denom = 0.0, 0.0
    for l in lots:
        amount, base, why = _lot_pnl(l, price, ref, start)
        if why is not None:
            return None, None, why
        total += amount
        denom += base
    return total, denom, None


@dataclass
class _Part:
    """One account's share of a symbol's tile. ``mode``: ``lots`` (these platform lots ARE the quantity) or ``cover``
    (a broker quantity: the platform lots must cover ``qty`` for "My P/L")."""
    account_id: Optional[int]
    qty: float
    mode: str
    lots: Tuple[Lot, ...] = ()
    experts: Tuple[str, ...] = ()
    whose: str = "the broker's"
    #: the broker's WHOLE position on this account (all experts, or a "No expert" remainder no named expert shares)
    whole: bool = False


def _maps_to(move: ShareMove, symbol: str) -> bool:
    return move.symbol == symbol or move.underlying == symbol


def _when(moment: Optional[datetime]) -> str:
    return "an unknown date" if moment is None else moment.astimezone(NY_TZ).date().isoformat()


def platform_fill_missing(symbol: str, orders: Sequence[PlatformOrder], moves: Sequence[ShareMove],
                          since: datetime) -> bool:
    """Whether the platform has a FILLED order of ``symbol`` created at or after ``since`` (the history's proven
    coverage start) that the broker rows ``moves`` do not account for (same side, enough quantity in total, not
    before the order was created minus ``ORDER_SKEW_S``).

    A BEST-EFFORT cross-check, NOT a proof: it only knows the platform's own orders (a trade placed directly at the
    broker is invisible to it), matches by side and total quantity rather than order by order, and dates an order
    by its creation (no fill time is stored)."""
    mine = [m for m in moves if _maps_to(m, symbol)]
    relevant = [o for o in orders if o.symbol == symbol and o.created_at >= since]
    for side, sign in (("BUY", 1.0), ("SELL", -1.0)):
        placed = [o for o in relevant if o.side == side]
        want = sum(o.quantity for o in placed)
        if want <= _EPS:
            continue
        earliest = min(o.created_at for o in placed)
        have = sum(abs(m.quantity) for m in mine if m.kind == "trade" and _finite(m.quantity) is not None
                   and m.quantity * sign > 0 and m.executed_at is not None
                   and (earliest - m.executed_at).total_seconds() <= ORDER_SKEW_S)
        if have + _tol(want) < want:
            return True
    return False


def recent_fill_missing(symbol: str, orders: Sequence[PlatformOrder], moves: Sequence[ShareMove],
                        positions_at: Optional[datetime]) -> bool:
    """Whether a platform order of ``symbol`` created within ``RECENT_ORDER_S`` before ``positions_at`` lacks its
    OWN broker trade rows: same sign, executed from its creation - ``RECENT_MATCH_SKEW_S`` to ``positions_at`` +
    ``RECENT_MATCH_SKEW_S``, adding up to its filled quantity within tolerance; each broker row serves ONE order
    (oldest order first, rows in time order). No fill time is stored, so "recent" is dated by the order's creation
    (an order created earlier that filled in the last 30 minutes is left to the aggregate check). Applies on top
    of ``platform_fill_missing``, which an older broker buy can satisfy for a missing recent fill."""
    if positions_at is None:
        return False
    recent = sorted((o for o in orders if o.symbol == symbol
                     and 0 <= (positions_at - o.created_at).total_seconds() <= RECENT_ORDER_S),
                    key=lambda o: o.created_at)
    trades = sorted((m for m in moves if _maps_to(m, symbol) and m.kind == "trade" and m.executed_at is not None
                     and _finite(m.quantity) is not None), key=lambda m: m.executed_at)
    used: set = set()
    for o in recent:
        sign = 1.0 if o.side == "BUY" else -1.0
        lo = o.created_at - timedelta(seconds=RECENT_MATCH_SKEW_S)
        hi = positions_at + timedelta(seconds=RECENT_MATCH_SKEW_S)
        got = 0.0
        for i, m in enumerate(trades):
            if i in used or m.quantity * sign <= 0 or not lo <= m.executed_at <= hi:
                continue
            if got >= o.quantity - _tol(o.quantity):
                break
            used.add(i)
            got += abs(m.quantity)
        if abs(got - o.quantity) > _tol(o.quantity):
            return True
    return False


def _history_pnl(symbol: str, q_now: float, history: BrokerHistory, price: float, ref: Optional[float],
                 boundary: datetime, held: Iterable[str], orders: Sequence[PlatformOrder]):
    """P/L of the broker's whole position from the broker's own share movements (see the module docstring)."""
    if history.failure:
        return None, None, (history.failure if history.failure.startswith("broker history still loading")
                            else f"the broker's transaction history could not be read ({history.failure})")
    if history.covers_from is None or history.covers_from > boundary:
        return None, None, (f"the broker's transaction history does not reach back to the period start "
                            f"({boundary.astimezone(NY_TZ):%Y-%m-%d %H:%M} New York)")
    held = set(held)

    def in_period(m: ShareMove) -> bool:
        return m.executed_at is None or m.executed_at > boundary

    for m in history.moves:                                   # rows that refuse EVERY tile of the account
        unheld = m.kind not in ("trade", "tile_only") and m.symbol not in held and m.underlying not in held
        if (m.kind == "split" or unheld) and in_period(m):
            return None, None, f"{m.detail} on {_when(m.executed_at)} in this account inside the period"
    mine = [m for m in history.moves if _maps_to(m, symbol)]
    if any(m.executed_at is None for m in mine):
        return None, None, "a broker transaction row of this symbol has no time (unrecognised transaction row)"
    by_id = {m.id: m for m in history.moves if m.id is not None}
    for m in history.moves:
        if m.reverses_id is None:
            continue
        target = by_id.get(m.reverses_id)
        if not (_maps_to(m, symbol) or (target is not None and _maps_to(target, symbol))):
            continue
        if in_period(m) or target is None or in_period(target):
            return None, None, f"a reversed broker transaction ({m.detail}) touches the period"
    inside = sorted((m for m in mine if m.executed_at > boundary), key=lambda m: m.executed_at)
    if history.positions_at is not None and any(
            (history.positions_at - m.executed_at).total_seconds() < RACE_MARGIN_S for m in inside):
        return None, None, "position changing right now, reload"
    for m in inside:
        if m.kind == "tile_only":
            return None, None, f"{m.detail} (on {_when(m.executed_at)})"
        if m.kind != "trade":
            return None, None, (f"a {m.kind} ({m.detail}) on {_when(m.executed_at)} inside the period: the broker's "
                                f"history cannot price it")
        if _positive(m.price) is None or _finite(m.quantity) is None:
            return None, None, f"a broker trade ({m.detail}) on {_when(m.executed_at)} inside the period has no price"
    if platform_fill_missing(symbol, orders, mine, history.covers_from):
        return None, None, PLATFORM_FILL_MISSING
    if recent_fill_missing(symbol, orders, mine, history.positions_at):
        return None, None, RECENT_FILL_MISSING
    q_start = q_now - sum(m.quantity for m in inside)
    if abs(q_start) <= _tol(q_now):
        q_start = 0.0
    if q_start * q_now < 0:
        return None, None, (f"the broker's history puts the position on the other side at the period start "
                            f"({q_start:g} shares)")
    if q_start and ref is None:
        return None, None, _NO_REFERENCE
    start_value = q_start * ref if q_start else 0.0
    buys = sum(m.quantity * m.price for m in inside if m.quantity > 0)
    sells = sum(-m.quantity * m.price for m in inside if m.quantity < 0)
    opening = buys if q_now > 0 else sells
    return q_now * price - start_value - buys + sells, abs(start_value) + opening, None


def _part_pnl(symbol: str, part: _Part, market: MarketInputs, price: float, ref: Optional[float], start: date,
              boundary: datetime, held: Mapping[Optional[int], set]):
    """``(pnl, denominator, reason, source)`` of one part."""
    if part.mode == "lots":
        return _lots_pnl(part.lots, price, ref, start) + (SOURCE_PLATFORM,)
    if part.whole and part.account_id in market.broker_history:
        acct = part.account_id
        return _history_pnl(symbol, part.qty, market.broker_history[acct], price, ref, boundary,
                            held.get(acct, set()), market.platform_orders.get(acct, ())) + (SOURCE_BROKER,)
    mine = sum(_signed_qty(l) for l in part.lots)
    if abs(mine - part.qty) > _tol(part.qty):
        return (None, None, f"platform records cover {abs(mine):g} of {part.whose} {abs(part.qty):g} shares",
                SOURCE_PLATFORM)
    return _lots_pnl(part.lots, price, ref, start) + (SOURCE_PLATFORM,)


def _source_of(part: _Part, market: MarketInputs, symbol: str) -> str:
    if not (part.whole and part.account_id in market.broker_history):
        return SOURCE_PLATFORM
    return SOURCE_BROKER


def _quantity_mismatches(lots: Sequence[Lot], broker_qty: Mapping[int, Mapping[str, float]]
                         ) -> List[Tuple[str, int, float, float]]:
    platform: Dict[Tuple[int, str], float] = {}
    for l in lots:
        q = _positive(l.quantity)
        if l.is_option or l.account_id is None or l.account_id not in broker_qty or q is None:
            continue
        key = (l.account_id, l.symbol)
        platform[key] = platform.get(key, 0.0) + (q if l.direction == "long" else -q)
    out = []
    for account_id, held in broker_qty.items():
        symbols = {s for (a, s) in platform if a == account_id} | set(held)
        for s in symbols:
            mine = platform[(account_id, s)] if (account_id, s) in platform else 0.0
            theirs = _finite(held[s]) if s in held else 0.0
            if theirs is None:
                continue
            if abs(mine - theirs) > _EPS * max(1.0, abs(theirs)):
                out.append((s, account_id, mine, theirs))
    return sorted(out, key=lambda m: (m[0], m[1]))


def _holdings(lots: Sequence[Lot], market: MarketInputs, state: HeatmapState):
    """``({symbol: [parts]}, issues, options excluded)`` for ``state`` -- see the module docstring for the rules."""
    parts: Dict[str, List[_Part]] = {}
    issues: List[Tuple[str, str]] = []
    labels = market.labels

    def expert_ok(name: str) -> bool:
        return not state.experts or name in state.experts

    equity, options = [], []
    for l in lots:
        if l.is_option:
            options.append(l)
        elif _positive(l.quantity) is None:
            if expert_ok(l.expert) and _label_ok(l.symbol, state, labels):
                issues.append((l.symbol, f"transaction {l.transaction_id} has no open quantity; not drawn"))
        else:
            equity.append(l)

    for acct in sorted(market.broker_qty):
        book = {s: q for s, q in ((s, _finite(q)) for s, q in market.broker_qty[acct].items()) if q is not None}
        acct_lots = [l for l in equity if l.account_id == acct]
        for sym in sorted(set(book) | {l.symbol for l in acct_lots}):
            if not _label_ok(sym, state, labels):
                continue
            b = book[sym] if sym in book else 0.0
            sym_lots = [l for l in acct_lots if l.symbol == sym]
            named = [l for l in sym_lots if l.expert != NO_EXPERT]
            unnamed = [l for l in sym_lots if l.expert == NO_EXPERT]
            expert_q = sum(_signed_qty(l) for l in named)
            held = abs(b) > _EPS
            exceed = abs(expert_q) > _EPS and (not held or b * expert_q < 0 or abs(expert_q) > abs(b) + _tol(b))
            exceed_why = f"experts' records exceed the broker's position (platform {expert_q:g}, broker {b:g})"
            not_held_why = (f"open on the platform ({sum(_signed_qty(l) for l in sym_lots):g}), "
                            f"not held at the broker (account {acct})")
            remainder = b - expert_q
            if not state.experts:
                if not held:
                    if sym_lots:
                        issues.append((sym, not_held_why))
                    continue
                if exceed:
                    issues.append((sym, exceed_why))
                experts = tuple(sorted({l.expert for l in named}))
                if not exceed and abs(remainder) > _tol(b):
                    experts += (NO_EXPERT,)
                parts.setdefault(sym, []).append(_Part(acct, b, "cover", tuple(sym_lots), experts, whole=True))
                continue
            chosen = [l for l in named if l.expert in state.experts]
            if exceed and (chosen or NO_EXPERT in state.experts):
                issues.append((sym, exceed_why))
            if chosen:
                parts.setdefault(sym, []).append(_Part(acct, sum(_signed_qty(l) for l in chosen), "lots",
                                                       tuple(chosen), tuple(sorted({l.expert for l in chosen}))))
            if NO_EXPERT in state.experts and not exceed:
                if abs(remainder) > _tol(b):
                    parts.setdefault(sym, []).append(_Part(acct, remainder, "cover", tuple(unnamed), (NO_EXPERT,),
                                                           "the No-expert", whole=abs(expert_q) <= _EPS))
                elif unnamed and not held:
                    issues.append((sym, not_held_why))

    # accounts whose broker book was not read (failed, or a transaction whose account is unknown): platform quantities
    held_somewhere = {s for book in market.broker_qty.values() for s, q in book.items()
                      if _finite(q) is not None and abs(_finite(q)) > _EPS}
    for l in equity:
        if l.account_id in market.broker_qty or not expert_ok(l.expert) or not _label_ok(l.symbol, state, labels):
            continue
        if l.account_id is None and l.symbol in held_somewhere:
            issues.append((l.symbol, f"transaction {l.transaction_id} has no account: not counted"))
            continue
        parts.setdefault(l.symbol, []).append(_Part(l.account_id, _signed_qty(l), "lots", (l,), (l.expert,)))

    if not state.experts:
        with_rows = {a for a, n in market.broker_option_rows.items() if n and a in market.broker_qty}
        excluded = sum(market.broker_option_rows[a] for a in with_rows)
        excluded += len({l.transaction_id for l in options
                         if l.account_id not in with_rows and _label_ok(l.symbol, state, labels)})
    else:
        excluded = len({l.transaction_id for l in options
                        if l.expert in state.experts and _label_ok(l.symbol, state, labels)})
    return parts, issues, excluded


def build_view(lots: Sequence[Lot], market: MarketInputs, state: HeatmapState, now: Moment) -> HeatmapView:
    """The tiles, the issues, the excluded-option count and the quantity differences for ``state`` at ``now``."""
    start = period_start(state.timeframe, now)
    ref_day = reference_session(state.timeframe, now)
    view = HeatmapView(state=state, period_start=start, reference_session=ref_day, clamp=CLAMP_PCT[state.timeframe],
                       today_status=today_status(now)[0])
    view.quantity_mismatches = _quantity_mismatches(lots, market.broker_qty)
    view.broker_history_used = bool(market.broker_history)
    view.broker_notes = [f"Account {a}: the broker's positions could not be read ({why}); its tiles use the "
                         f"platform's transaction quantities." for a, why in sorted(market.broker_failed.items())]
    parts, issues, view.options_excluded = _holdings(lots, market, state)

    today = _split_moment(now)[0]
    held: Dict[Optional[int], set] = {a: {s for s, q in book.items() if _finite(q)}
                                      for a, book in market.broker_qty.items()}
    for l in lots:
        held.setdefault(l.account_id, set()).add(l.symbol)
    for symbol in sorted(parts):
        gross = sum(abs(p.qty) for p in parts[symbol])
        price = _positive(market.prices.get(symbol))
        if gross < DUST_QTY or (price is not None and gross * price < DUST_VALUE):
            issues.append((symbol, f"dust position ({gross:g} shares): not drawn"))
            continue
        tile, tile_issues = _tile(symbol, parts[symbol], market, state, start, ref_day, today, held)
        view.tiles.append(tile)
        issues.extend((symbol, why) for why in tile_issues)

    known = [t.size for t in view.tiles if t.size_known]
    floor = MIN_TILE_SHARE * sum(known) if known else 1.0
    for t in view.tiles:
        if not t.size_known:
            t.size = floor
    view.issues = sorted(dict.fromkeys(issues), key=lambda i: i[0])
    return view


def _tile(symbol: str, parts: List[_Part], market: MarketInputs, state: HeatmapState, start: date,
          ref_day: date, today: date, held: Mapping[Optional[int], set]) -> Tuple[Tile, List[str]]:
    issues: List[str] = []
    price = _positive(market.prices.get(symbol))
    ref = _positive((market.closes.get(symbol) or {}).get(ref_day.isoformat()))
    mcap = _positive(market.market_caps.get(symbol))

    price_issue = None if price is not None else \
        f"no current price: {market.price_missing.get(symbol, 'the account did not return a usable one')}"
    ref_issue = None
    if ref is None:
        why = market.closes_missing.get(symbol)
        ref_issue = f"no daily close for {ref_day.isoformat()}" + (f": {why}" if why else "")
    split_days = sorted(d for d in (market.splits.get(symbol) or ()) if ref_day < d <= today)
    split_issue = (f"split on {split_days[0].isoformat()} inside the period: history and broker prices may be on "
                   f"different bases" if split_days else None)

    net = sum(p.qty for p in parts)
    gross = sum(abs(p.qty) for p in parts)
    value = gross * price if price is not None else None
    change = (price / ref - 1.0) * 100.0 if price is not None and ref is not None and not split_issue else None

    pnl = pnl_pct = None
    pnl_issue = split_issue
    if not split_issue and symbol in market.splits_missing and state.timeframe != "today":
        pnl_issue = (f"split calendar unavailable ({market.splits_missing[symbol]}): My P/L cannot rule out a split "
                     f"in a period longer than one session")
    if pnl_issue:
        pass
    elif price is not None:
        total, denom = 0.0, 0.0
        boundary = regular_session_close_utc(ref_day)
        for part in parts:
            amount, base, why, _source = _part_pnl(symbol, part, market, price, ref, start, boundary, held)
            if why is not None:
                pnl_issue = ref_issue if why == _NO_REFERENCE else why
                total = None
                break
            total += amount
            denom += base
        if total is not None:
            pnl = total
            pnl_pct = total / denom * 100.0 if denom > _EPS else None
            if pnl_pct is None:
                pnl_issue = "nothing was at risk in the period (no start value and no opening fill)"
    else:
        pnl_issue = price_issue

    if state.metric == "price":
        color_pct = change
        for why in (split_issue, price_issue, None if split_issue else ref_issue):
            if why:
                issues.append(why)
    else:
        color_pct = pnl_pct
        if pnl_issue:
            issues.append(pnl_issue)
        if price_issue:
            issues.append(price_issue)
    if symbol in market.splits_missing:
        issues.append(f"not checked for splits: {market.splits_missing[symbol]}")

    if state.size == "company":
        size_known = mcap is not None
        if not size_known:
            issues.append(f"no market cap: {market.market_caps_missing.get(symbol, 'the provider returned none')}")
        size = mcap if size_known else 0.0
    else:
        size_known = value is not None
        if not size_known and price_issue:
            issues.append(price_issue)
        size = value if size_known else 0.0

    names = {e for p in parts for e in p.experts}
    experts = tuple(sorted(n for n in names if n != NO_EXPERT)) + ((NO_EXPERT,) if NO_EXPERT in names else ())
    tile = Tile(symbol=symbol, quantity=net, current_price=price, reference_close=ref, market_value=value,
                market_cap=mcap, price_change_pct=change, pnl_amount=pnl, pnl_pct=pnl_pct, color_pct=color_pct,
                size=size, size_known=size_known, experts=experts, labels=_symbol_labels(market.labels, symbol))
    tile.label_text = f"{symbol}\n{_pct(color_pct)}"
    sources = sorted({_source_of(p, market, symbol) for p in parts})
    tile.pnl_source = " and ".join(sources) if sources else None
    return tile, list(dict.fromkeys(issues))


# --------------------------------------------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------------------------------------------

def _rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#{:02x}{:02x}{:02x}".format(round(ra + (rb - ra) * t), round(ga + (gb - ga) * t),
                                        round(ba + (bb - ba) * t))


def color_for(pct: Optional[float], clamp: float) -> str:
    """Diverging colour for ``pct``: neutral at 0, full red / green at -/+ ``clamp`` and beyond."""
    pct = _finite(pct)
    if pct is None:
        return NA_COLOR
    t = max(-1.0, min(1.0, pct / clamp))
    if t == 0:
        return NEUTRAL
    return _mix(NEUTRAL, GREEN if t > 0 else RED, abs(t))


def legend_stops(clamp: float) -> List[Tuple[str, str]]:
    """``[(label, colour)]`` for the colour key under the chart, from -clamp to +clamp."""
    steps = (-1.0, -2 / 3, -1 / 3, 0.0, 1 / 3, 2 / 3, 1.0)
    out = []
    for s in steps:
        text = "0%" if s == 0 else f"{s * clamp:+.{0 if clamp >= 6 else 1}f}%"
        if abs(s) == 1.0:
            text = ("<= " if s < 0 else ">= ") + text
        out.append((text, color_for(s * clamp, clamp)))
    return out


# --------------------------------------------------------------------------------------------------------------
# remembered choices (app.storage.user, see the tab)
# --------------------------------------------------------------------------------------------------------------

def storage_key(port: Optional[int]) -> str:
    """The ``app.storage.user`` key, per port like the account filter (``account_filter_storage_key``): two
    instances on different ports share the browser's storage, and expert names are database-scoped."""
    return f"{STORAGE_KEY}:{port}" if port else STORAGE_KEY


def _choice(stored: Mapping[str, Any], key: str, options: Mapping[str, str], default: str) -> str:
    value = stored.get(key)
    return value if isinstance(value, str) and value in options else default


def _stored_names(stored: Mapping[str, Any], key: str) -> List[str]:
    value = stored.get(key)
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str)))


def _selection(stored: Mapping[str, Any], key: str, available: Sequence[str]) -> Tuple[str, ...]:
    wanted = set(_stored_names(stored, key))
    return tuple(a for a in available if a in wanted)     # nothing left = () = all, never an empty chart


def resolve_state(stored: Any, available_experts_: Sequence[str], available_labels_: Sequence[str]) -> HeatmapState:
    """The choices to show, from what was stored and what exists now. Anything unusable falls back to its default;
    a stored expert / label that no longer exists is dropped (the rest kept; none left = all)."""
    stored = stored if isinstance(stored, Mapping) else {}
    return HeatmapState(
        timeframe=_choice(stored, "timeframe", TIMEFRAMES, DEFAULT_TIMEFRAME),
        metric=_choice(stored, "metric", METRICS, DEFAULT_METRIC),
        size=_choice(stored, "size", SIZE_MODES, DEFAULT_SIZE),
        experts=_selection(stored, "experts", available_experts_),
        labels=_selection(stored, "labels", available_labels_),
    )


def dropped_filters(stored: Any, available_experts_: Sequence[str], available_labels_: Sequence[str]
                    ) -> Tuple[List[str], List[str]]:
    """``(experts, labels)`` that were stored but are not on offer for the current account (not applied)."""
    stored = stored if isinstance(stored, Mapping) else {}
    return ([e for e in _stored_names(stored, "experts") if e not in set(available_experts_)],
            [l for l in _stored_names(stored, "labels") if l not in set(available_labels_)])


def state_to_storage(state: HeatmapState) -> Dict[str, Any]:
    return {"timeframe": state.timeframe, "metric": state.metric, "size": state.size,
            "experts": list(state.experts), "labels": list(state.labels)}


def merge_for_storage(state: HeatmapState, previous: Any, available_experts_: Sequence[str],
                      available_labels_: Sequence[str]) -> Dict[str, Any]:
    """What to store after a change: ``state``, plus every previously stored expert / label that is NOT on offer
    here (another account's expert stays remembered for when that account is selected again). One that is on offer
    and unticked is dropped -- that was the user's choice."""
    out = state_to_storage(state)
    previous = previous if isinstance(previous, Mapping) else {}
    for key, offered in (("experts", set(available_experts_)), ("labels", set(available_labels_))):
        out[key] += [v for v in _stored_names(previous, key) if v not in offered and v not in out[key]]
    return out


# --------------------------------------------------------------------------------------------------------------
# presentation (still pure: strings and an ECharts option dict)
# --------------------------------------------------------------------------------------------------------------

def _pct(value: Optional[float], decimals: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{decimals}f}%"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _signed_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return ("+" if value >= 0 else "-") + f"${abs(value):,.2f}"


def format_big(value: Optional[float]) -> str:
    """``$2.00T`` / ``$412.30B`` / ``$95.10M`` for a market cap."""
    if value is None:
        return "n/a"
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= div:
            return f"${value / div:.2f}{unit}"
    return f"${value:,.0f}"


def _price(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def tooltip_html(tile: Tile, view: HeatmapView) -> str:
    e = html.escape
    ref = view.reference_session.isoformat()
    pnl = "n/a" if tile.pnl_amount is None else f"{_signed_money(tile.pnl_amount)} ({_pct(tile.pnl_pct)})"
    rows = [
        f"<b>{e(tile.symbol)}</b>",
        f"Market cap: {format_big(tile.market_cap)}",
        f"My position value: {_money(tile.market_value)}",
        f"Quantity: {tile.quantity:g}" + (" (short)" if tile.quantity < 0 else ""),
        f"Price: {_price(tile.reference_close)} (close {ref}) &rarr; {_price(tile.current_price)}",
        f"Price change: {_pct(tile.price_change_pct)}",
        f"My P/L ({e(TIMEFRAMES[view.state.timeframe].lower())}): {pnl}"
        + (f" - from {html.escape(tile.pnl_source, quote=False)}" if tile.pnl_source else ""),
        f"Experts: {e(', '.join(tile.experts))}",
        f"Labels: {e(', '.join(tile.labels))}",
    ]
    return "<br>".join(rows)


def _label_symbol(symbol: str) -> str:
    """The symbol as it may appear inside an ECharts rich-text label (``{ } |`` are markup there)."""
    return _RICH_TEXT.sub("", symbol) or "?"


def chart_options(view: HeatmapView) -> Dict[str, Any]:
    """The ECharts treemap. The label and tooltip text of each tile are computed here; the two JS formatters only
    read them back (``p.data.lbl`` / ``p.data.tip``). Every number in it is finite."""
    data = []
    for t in view.tiles:
        style: Dict[str, Any] = {"color": color_for(t.color_pct, view.clamp)}
        if not t.size_known:
            style.update({"borderColor": UNKNOWN_SIZE_BORDER, "borderWidth": 2})
        data.append({"name": t.symbol, "value": t.size, "itemStyle": style,
                     "lbl": "{s|" + _label_symbol(t.symbol) + "}\n{p|" + _pct(t.color_pct) + "}",
                     "tip": tooltip_html(t, view)})
    return {
        "backgroundColor": "transparent",
        "tooltip": {"backgroundColor": "rgba(37, 43, 59, 0.95)", "borderColor": "rgba(255, 255, 255, 0.1)",
                    "textStyle": {"color": "#ffffff"},
                    ":formatter": "p => (p.data && p.data.tip) ? p.data.tip : p.name"},
        "series": [{
            "type": "treemap", "roam": False, "nodeClick": False, "breadcrumb": {"show": False},
            "left": 0, "right": 0, "top": 0, "bottom": 0, "squareRatio": 0.5 * (1 + 5 ** 0.5),
            "label": {"show": True, "color": "#ffffff", ":formatter": "p => p.data.lbl", "overflow": "truncate",
                      "rich": {"s": {"fontSize": 13, "fontWeight": "bold", "color": "#ffffff"},
                               "p": {"fontSize": 11, "color": "#ffffff"}}},
            "upperLabel": {"show": False},
            "itemStyle": {"borderColor": TILE_BORDER, "borderWidth": 1, "gapWidth": 2},
            "emphasis": {"itemStyle": {"borderColor": "#ffffff"}},
            "data": data,
        }],
    }


def _today_sentence(view: HeatmapView) -> str:
    shown, ref = view.period_start.isoformat(), view.reference_session.isoformat()
    if view.today_status == PRE_OPEN:
        return (f"The market has not opened yet today: Today shows the last completed session, {shown}, "
                f"against the close of {ref}.")
    if view.today_status == NO_SESSION:
        return f"The market is closed today: Today shows the last session, {shown}, against the close of {ref}."
    if view.today_status == OPEN:
        return f"Today: the {shown} session is in progress, against the close of {ref}."
    return f"Today: the {shown} session against the close of {ref}."


def caption(view: HeatmapView) -> str:
    """What the tiles are, what size and colour mean, for which period and metric, and what was left out."""
    s = view.state
    size = ("company market cap (Company size; a symbol with no market cap is a small bordered tile)"
            if s.size == "company" else "my position value, |quantity| x current price (My exposure)")
    metric = ("Price change - current price vs that close" if s.metric == "price"
              else "My P/L - my position's profit or loss since that close, counting buys and sells made since")
    parts = ["Tiles: what is held at the broker; expert split from the platform's records.",
             f"Size: {size}.",
             f"Colour: {metric}. Period: {TIMEFRAMES[s.timeframe]}, from the close of "
             f"{view.reference_session.isoformat()}. Red <= -{view.clamp:g}%, green >= +{view.clamp:g}%."]
    if s.metric == "pnl":
        parts.append("My P/L does not count dividends and fees.")
        if view.broker_history_used:
            parts.append(BROKER_HISTORY_DELAY_NOTE)
    if s.timeframe == "today":
        parts.append(_today_sentence(view))
    parts.extend(view.broker_notes)
    if view.options_excluded:
        n = view.options_excluded
        parts.append(f"{n} option position{'s' if n != 1 else ''} excluded (option contracts have no daily "
                     f"reference close here).")
    return " ".join(parts)
