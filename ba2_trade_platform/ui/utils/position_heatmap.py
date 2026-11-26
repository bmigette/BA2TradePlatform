"""The Overview "Heatmap" tab's rules: one treemap tile per symbol the account holds.

PURE: no NiceGUI, no database, no network. The loader (``services.position_heatmap``) turns the open transactions,
the account's prices, the daily history, the market caps, the split calendar and the instrument labels into the
plain inputs below; everything that decides a number, a colour, a size or a remembered choice is here and
unit-tested (``tests/test_position_heatmap.py``). The tab (``ui.components.PositionHeatmapTab``) only draws the
result.

TILES ARE THE PLATFORM'S OPEN TRANSACTIONS (positions opened through the platform), not the broker's own book. Where
the broker's quantity for a symbol differs from the platform's, the difference is listed (``quantity_mismatches``).

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

Size mode: ``company`` -- the company's market cap (default, like a market heat map); ``exposure`` -- the
position's market value. An unknown size is a small minimum tile with a neutral border, listed under the chart.
Nothing is dropped silently, no missing or non-finite number becomes a 0, and options are counted, not drawn.
"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ba2_common.core.market_calendar import (
    NY_TZ, is_regular_session, next_regular_session, nyse_regular_sessions, prior_regular_session,
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


def available_experts(lots: Iterable[Lot]) -> List[str]:
    """Expert keys present in ``lots``, sorted, with :data:`NO_EXPERT` last."""
    keys = {l.expert for l in lots}
    return sorted(k for k in keys if k != NO_EXPERT) + ([NO_EXPERT] if NO_EXPERT in keys else [])


def available_labels(lots: Iterable[Lot], labels: Mapping[str, Sequence[str]]) -> List[str]:
    """Labels of the symbols in ``lots``, sorted, with :data:`UNLABELED` last."""
    keys = {lbl for l in lots for lbl in _symbol_labels(labels, l.symbol)}
    return sorted(k for k in keys if k != UNLABELED) + ([UNLABELED] if UNLABELED in keys else [])


def _in_scope(lot: Lot, state: HeatmapState, labels: Mapping[str, Sequence[str]]) -> bool:
    if state.experts and lot.expert not in state.experts:
        return False
    if state.labels and not set(_symbol_labels(labels, lot.symbol)) & set(state.labels):
        return False
    return True


# --------------------------------------------------------------------------------------------------------------
# the maths
# --------------------------------------------------------------------------------------------------------------

def _signed(fill: Fill) -> float:
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


def build_view(lots: Sequence[Lot], market: MarketInputs, state: HeatmapState, now: Moment) -> HeatmapView:
    """The tiles, the issues, the excluded-option count and the quantity differences for ``state`` at ``now``."""
    start = period_start(state.timeframe, now)
    ref_day = reference_session(state.timeframe, now)
    view = HeatmapView(state=state, period_start=start, reference_session=ref_day, clamp=CLAMP_PCT[state.timeframe],
                       today_status=today_status(now)[0])
    view.quantity_mismatches = _quantity_mismatches(lots, market.broker_qty)
    scoped = [l for l in lots if _in_scope(l, state, market.labels)]
    view.options_excluded = len({l.transaction_id for l in scoped if l.is_option})

    by_symbol: Dict[str, List[Lot]] = {}
    issues: List[Tuple[str, str]] = []
    for l in scoped:
        if l.is_option:
            continue
        if _positive(l.quantity) is None:
            issues.append((l.symbol, f"transaction {l.transaction_id} has no open quantity; not drawn"))
            continue
        by_symbol.setdefault(l.symbol, []).append(l)

    today = _split_moment(now)[0]
    for symbol in sorted(by_symbol):
        tile, tile_issues = _tile(symbol, by_symbol[symbol], market, state, start, ref_day, today)
        view.tiles.append(tile)
        issues.extend((symbol, why) for why in tile_issues)

    known = [t.size for t in view.tiles if t.size_known]
    floor = MIN_TILE_SHARE * sum(known) if known else 1.0
    for t in view.tiles:
        if not t.size_known:
            t.size = floor
    view.issues = sorted(issues, key=lambda i: i[0])
    return view


def _tile(symbol: str, lots: List[Lot], market: MarketInputs, state: HeatmapState, start: date,
          ref_day: date, today: date) -> Tuple[Tile, List[str]]:
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

    net = sum((l.quantity if l.direction == "long" else -l.quantity) for l in lots)
    gross = sum(l.quantity for l in lots)
    value = gross * price if price is not None else None
    change = (price / ref - 1.0) * 100.0 if price is not None and ref is not None and not split_issue else None

    pnl = pnl_pct = None
    pnl_issue = split_issue
    if split_issue:
        pass
    elif price is not None:
        total, denom = 0.0, 0.0
        for l in lots:
            amount, base, why = _lot_pnl(l, price, ref, start)
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

    experts = tuple(sorted({l.expert for l in lots}))
    tile = Tile(symbol=symbol, quantity=net, current_price=price, reference_close=ref, market_value=value,
                market_cap=mcap, price_change_pct=change, pnl_amount=pnl, pnl_pct=pnl_pct, color_pct=color_pct,
                size=size, size_known=size_known, experts=experts, labels=_symbol_labels(market.labels, symbol))
    tile.label_text = f"{symbol}\n{_pct(color_pct)}"
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
        f"My P/L ({e(TIMEFRAMES[view.state.timeframe].lower())}): {pnl}",
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
    parts = ["Tiles: positions opened through the platform (its open transactions), not the broker's own book.",
             f"Size: {size}.",
             f"Colour: {metric}. Period: {TIMEFRAMES[s.timeframe]}, from the close of "
             f"{view.reference_session.isoformat()}. Red <= -{view.clamp:g}%, green >= +{view.clamp:g}%."]
    if s.timeframe == "today":
        parts.append(_today_sentence(view))
    if view.options_excluded:
        n = view.options_excluded
        parts.append(f"{n} option position{'s' if n != 1 else ''} excluded (option contracts have no daily "
                     f"reference close here).")
    return " ".join(parts)
