"""Inputs for the Overview "Heatmap" tab: open transactions as lots, the account's prices and broker quantities, daily
history, market caps, the split calendar and instrument labels. Everything that DECIDES anything is in
``ui.utils.position_heatmap`` (pure); this module only reads, and runs in a worker thread.

WHERE EACH NUMBER COMES FROM (the same sources the other Overview views use):
  * open positions -- the OPENED / CLOSING ``Transaction`` rows in the header's account scope: a transaction belongs
    to the account of its orders (``services.account_scope``); one with NO order row belongs to its expert's account
    (``ExpertInstance.account_id``), and is unresolved (listed, not priced) when it has neither;
  * current price and broker quantity -- the ACCOUNT, through the management API's read path
    (``services.open_pnl._for_account``): ONE ``_positions_cache.read_positions`` per account inside
    ``share_positions()`` (a second load within 15 s reuses it) wrapped in ``broker_read``; the broker's position
    price for what it holds, the account price feed for the rest (``position_prices.held_position_prices``). A failed
    or raised positions read is NOT retried: every symbol goes to the price feed;
  * reference closes -- ``price_history.fetch_closes`` (each account's own history provider, FMP daily by default),
    ONE call PER ACCOUNT, so a source that fails (no keys, unreachable) marks only that account's symbols;
  * fills -- the transaction's own ``TradingOrder`` rows, counted by ``Transaction.get_current_open_qty``'s rule,
    priced at the order's ``open_price``, timed by when the order was PLACED (``created_at``; no fill time is stored
    anywhere) plus, for the entry, ``Transaction.open_date`` (set when the platform first saw the entry filled);
  * market cap -- FMP ``/quote`` (``marketCap``), 100 symbols per call (``core.fmp_live_price.fetch_fmp_quote_rows``),
    cached per symbol for a day in the shared ``fmp_common`` live cache;
  * splits -- FMP's per-symbol split calendar (``ba2_providers.symbol_info.fetch_splits``: 1-day memo, which the FMP
    history provider's own daily refresh already fills);
  * labels -- ``Instrument.labels`` (``core.utils.get_labels_by_symbol``), the portfolio allocator's labels.

BOUNDS. Each account's broker work runs in its own thread under ``BROKER_DEADLINE_S`` (a hung IBKR Gateway or
TastyTrade session marks that account "timed out"); the history reads share ``HISTORY_BUDGET_S``; market caps and
splits each run under ``MARKET_DATA_DEADLINE_S``. The tab adds an overall bound on top.

CACHES. Reference closes are kept per (history source, symbol) for ``CLOSES_TTL_S`` and never across a regular
session close (the next session's reference close would be missing). Concurrent loads of the same scope (two browser
tabs) share ONE in-flight load.

No value is defaulted: a symbol whose price, close, market cap or split calendar could not be had is absent from that
map and its reason is in the matching ``*_missing`` map."""
import hashlib
import math
import threading
import time
from collections import defaultdict
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlmodel import and_, or_, select

from ba2_common.config import get_app_setting
from ba2_common.core.market_calendar import nyse_regular_sessions

from ..core.db import get_db
from ..core.models import ExpertInstance, TradingOrder, Transaction
from ..core.types import AssetClass, OrderDirection, OrderStatus, OrderType
from ..core.utils import get_account_instance_from_id, get_labels_by_symbol, normalize_symbol
from ..logger import logger
from ..ui.utils.position_heatmap import NO_EXPERT, Fill, Lot, MarketInputs, history_start, warm_calendar
from . import price_history
from ._positions_cache import read_positions, share_positions
from .account_scope import account_transaction_ids
from .accounts import broker_read
from .open_pnl import PRICED_STATUSES, _account_of
from .position_prices import broker_position_prices_from, held_position_prices

#: One account's positions read + price-feed call.
BROKER_DEADLINE_S = 15.0
#: Every account's history reads together.
HISTORY_BUDGET_S = 45.0
#: The market caps, and separately the split calendars.
MARKET_DATA_DEADLINE_S = 20.0
#: How long a symbol's fetched closes are reused (and never across a regular session close).
CLOSES_TTL_S = 4 * 3600.0
#: Market caps change slowly; one day is fresh enough (one FMP call per 100 uncached symbols per day).
MARKET_CAP_TTL_S = 24 * 3600.0
_CAP_KEY = "overview_heatmap:market_cap:{}"


@dataclass
class HeatmapData:
    lots: List[Lot]
    market: MarketInputs
    #: what the load cost: positions reads, feed price calls, history symbols read (one provider read each; cache
    #: hits are not counted), FMP quote calls, split-calendar reads (FMP-memo hits included)
    calls: Dict[str, int] = field(default_factory=dict)


class _Deadline(Exception):
    pass


def _call_with_deadline(fn: Callable, timeout_s: float, name: str):
    """``fn()`` in a daemon thread; ``_Deadline`` when it has not returned after ``timeout_s`` (it keeps running and
    its result is dropped). Its exception is re-raised here."""
    box: Dict[str, object] = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 -- re-raised in the caller's thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name=name)
    t.start()
    t.join(max(0.0, timeout_s))
    if t.is_alive():
        raise _Deadline(f"{name} did not answer within {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _positive(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


# --------------------------------------------------------------------------------------------------------------
# rows -> lots
# --------------------------------------------------------------------------------------------------------------

def expert_display_name(expert: ExpertInstance) -> str:
    """``<alias | user_description | class>-<id>``: the name the Overview's expert dropdowns use
    (``core.utils.get_expert_options_for_ui``)."""
    return f"{expert.alias or expert.user_description or expert.expert}-{expert.id}"


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)   # the platform stores UTC


def _counted_fill_qty(order: TradingOrder) -> Optional[float]:
    """The filled quantity ``Transaction.get_current_open_qty`` counts for ``order``; ``None`` = not a fill. A
    filled order with NO filled quantity is returned as NaN, so the lot's reconciliation fails loudly."""
    if order.status in OrderStatus.get_executed_statuses():
        return order.filled_qty if order.filled_qty is not None else float("nan")
    if order.status in OrderStatus.get_terminal_statuses() and order.filled_qty and order.filled_qty > 0:
        return order.filled_qty
    return None


def lots_from_rows(transactions: Sequence[Transaction], orders_of: Dict[int, List[TradingOrder]],
                   expert_names: Dict[int, str], account_of: Dict[int, int]) -> List[Lot]:
    """One :class:`Lot` per open transaction. Each fill carries when its order was PLACED and whether it was a market
    order; the entry fill also carries ``seen_by`` = the transaction's ``open_date`` (the platform sets it once, when
    it first sees the entry filled, so the fill happened no later)."""
    lots: List[Lot] = []
    for tx in transactions:
        direction = "long" if tx.side == OrderDirection.BUY else "short"
        opening_side = OrderDirection.BUY if direction == "long" else OrderDirection.SELL
        fills: List[Fill] = []
        entry_seen = False
        for o in sorted(orders_of.get(tx.id, []), key=lambda o: (_aware(o.created_at) or datetime.min.replace(
                tzinfo=timezone.utc), o.id or 0)):
            if tx.asset_class != AssetClass.OPTION and o.asset_class == AssetClass.OPTION:
                continue                      # an option leg on a share transaction is not a share fill
            qty = _counted_fill_qty(o)
            if qty is None or o.created_at is None:
                continue
            seen_by = None
            if not entry_seen and o.side == opening_side:
                entry_seen = True
                seen_by = _aware(tx.open_date)
            fills.append(Fill(side="BUY" if o.side == OrderDirection.BUY else "SELL", qty=float(qty),
                              price=_positive(o.open_price), placed=_aware(o.created_at),
                              is_market=o.order_type == OrderType.MARKET, seen_by=seen_by))
        if tx.expert_id is None:
            expert = NO_EXPERT
        else:                                 # an id with no ExpertInstance row is named, not folded into NO_EXPERT
            expert = expert_names[tx.expert_id] if tx.expert_id in expert_names else f"expert {tx.expert_id} (no instance row)"
        lots.append(Lot(transaction_id=tx.id, symbol=normalize_symbol(tx.symbol) or tx.symbol, expert=expert,
                        direction=direction, quantity=tx.quantity, fills=tuple(fills),
                        is_option=tx.asset_class == AssetClass.OPTION, account_id=account_of.get(tx.id)))
    return lots


def _read_rows(account_id: Optional[int]):
    """``(transactions, orders_of, expert_names, account_of)`` -- plain copies, the session closed. In a one-account
    scope a transaction with no order row is included when its expert belongs to the account."""
    with get_db() as session:
        query = select(Transaction).where(Transaction.status.in_(PRICED_STATUSES))
        if account_id is not None:
            with_orders = select(TradingOrder.transaction_id).where(TradingOrder.transaction_id.isnot(None))
            experts_of_account = select(ExpertInstance.id).where(ExpertInstance.account_id == account_id)
            query = query.where(or_(Transaction.id.in_(account_transaction_ids(account_id)),
                                    and_(Transaction.id.notin_(with_orders),
                                         Transaction.expert_id.in_(experts_of_account))))
        txs = list(session.exec(query).all())
        ids = [t.id for t in txs]
        orders_of: Dict[int, List[TradingOrder]] = defaultdict(list)
        account_of: Dict[int, int] = {}
        expert_names: Dict[int, str] = {}
        if ids:
            for o in session.exec(select(TradingOrder).where(TradingOrder.transaction_id.in_(ids))).all():
                orders_of[o.transaction_id].append(o)
            account_of = _account_of(session, ids)
            expert_ids = sorted({t.expert_id for t in txs if t.expert_id is not None})
            expert_account: Dict[int, int] = {}
            if expert_ids:
                for e in session.exec(select(ExpertInstance).where(ExpertInstance.id.in_(expert_ids))).all():
                    expert_names[e.id] = expert_display_name(e)
                    expert_account[e.id] = e.account_id
            for t in txs:                      # no order row: the expert's account
                if t.id not in account_of and t.expert_id in expert_account:
                    account_of[t.id] = expert_account[t.expert_id]
        session.expunge_all()
    return txs, orders_of, expert_names, account_of


# --------------------------------------------------------------------------------------------------------------
# market caps and splits
# --------------------------------------------------------------------------------------------------------------

def fetch_market_caps(symbols: Iterable[str], *, quote_fn: Optional[Callable] = None,
                      calls: Optional[Dict[str, int]] = None) -> Tuple[Dict[str, float], Dict[str, str]]:
    """``({symbol: market cap}, {symbol: why missing})`` from FMP ``/quote``, cached per symbol for a day.

    Only uncached symbols are requested, 100 per call. A row without a finite positive ``marketCap`` (an ETF often
    has none) is missing, never a 0; a failed call marks its chunk. Only real values are cached, so a missing one is
    asked again on the next load. ``calls`` (when given) must carry ``fmp_quote_calls``."""
    from ba2_providers.fmp_common import FMP_LIVE_CACHE_MISS, fmp_live_cache_get, fmp_live_cache_put, redact
    from ..core.fmp_live_price import FMP_QUOTE_BATCH, fetch_fmp_quote_rows, to_fmp_symbol

    caps: Dict[str, float] = {}
    missing: Dict[str, str] = {}
    to_ask: Dict[str, List[str]] = {}
    for s in dict.fromkeys(symbols):
        hit = fmp_live_cache_get(_CAP_KEY.format(to_fmp_symbol(s)), MARKET_CAP_TTL_S)
        if hit is not FMP_LIVE_CACHE_MISS:
            caps[s] = hit
        else:
            to_ask.setdefault(to_fmp_symbol(s), []).append(s)
    if not to_ask:
        return caps, missing
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return caps, {s: "FMP_API_KEY is not configured" for spellings in to_ask.values() for s in spellings}
    fmp_syms = list(to_ask)
    for i in range(0, len(fmp_syms), FMP_QUOTE_BATCH):
        chunk = fmp_syms[i:i + FMP_QUOTE_BATCH]
        if calls is not None:
            calls["fmp_quote_calls"] += 1
        try:
            rows = fetch_fmp_quote_rows(api_key, chunk, quote_fn=quote_fn)
        except Exception as e:  # noqa: BLE001 -- deliberate: a failed chunk marks ITS symbols, the chart still draws
            reason = f"the FMP quote call failed ({type(e).__name__}: {redact(str(e))})"
            logger.warning(f"Heatmap market caps: {reason} for {len(chunk)} symbols")
            missing.update({s: reason for f in chunk for s in to_ask[f]})
            continue
        got: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            cap = _positive(row.get("marketCap"))
            if sym in to_ask and cap is not None:
                got[sym] = cap
        for f in chunk:
            if f in got:
                fmp_live_cache_put(_CAP_KEY.format(f), got[f], MARKET_CAP_TTL_S)
                caps.update({s: got[f] for s in to_ask[f]})
            else:
                missing.update({s: "FMP returned no market cap" for s in to_ask[f]})
    return caps, missing


def fetch_split_days(symbols: Iterable[str], *, calls: Optional[Dict[str, int]] = None
                     ) -> Tuple[Dict[str, List[date]], Dict[str, str]]:
    """``({symbol: [split dates]}, {symbol: why unknown})`` from FMP's split calendar (``symbol_info.fetch_splits``,
    1-day memo). Every dated split counts, its ratio known or not."""
    from ba2_providers import symbol_info
    from ba2_providers.fmp_common import redact
    from ..core.fmp_live_price import to_fmp_symbol

    wanted = list(dict.fromkeys(symbols))
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return {}, {s: "FMP_API_KEY is not configured" for s in wanted}
    out: Dict[str, List[date]] = {}
    missing: Dict[str, str] = {}
    for s in wanted:
        if calls is not None:
            calls["split_calendar_reads"] += 1
        try:
            out[s] = [e.date for e in symbol_info.parse_splits(symbol_info.fetch_splits(api_key, to_fmp_symbol(s)))]
        except Exception as e:  # noqa: BLE001 -- deliberate: one symbol's calendar failing is that symbol's note
            missing[s] = f"the FMP split calendar failed ({type(e).__name__}: {redact(str(e))})"
    return out, missing


# --------------------------------------------------------------------------------------------------------------
# reference closes (cached)
# --------------------------------------------------------------------------------------------------------------

_CLOSES_LOCK = threading.Lock()
#: (history source key, symbol) -> (fetched at, first date asked, {date: close})
_CLOSES: Dict[Tuple[str, str], Tuple[datetime, date, Dict[str, float]]] = {}


def clear_caches() -> None:
    with _CLOSES_LOCK:
        _CLOSES.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def _history_source(account_id: int) -> str:
    """A cache key for the account's history provider: its registry name and settings, hashed (the settings may hold
    market-data keys; nothing here is logged)."""
    from ..core.account_history import history_spec_for_account
    name, kwargs = history_spec_for_account(account_id)
    plain = sorted((k, v) for k, v in kwargs.items() if isinstance(v, (str, int, float)))
    return f"{name}:{hashlib.sha256(repr(plain).encode()).hexdigest()[:16]}"


def _session_closed_between(earlier: datetime, later: datetime) -> bool:
    sessions = nyse_regular_sessions(earlier.date() - timedelta(days=1), later.date() + timedelta(days=1))
    return any(earlier < close <= later for _open, close in sessions)


def _cached_closes(source: str, symbol: str, start: date, now: datetime) -> Optional[Dict[str, float]]:
    with _CLOSES_LOCK:
        hit = _CLOSES.get((source, symbol))
    if hit is None:
        return None
    fetched_at, first, closes = hit
    if first > start or (now - fetched_at).total_seconds() >= CLOSES_TTL_S or now < fetched_at \
            or _session_closed_between(fetched_at, now):
        return None
    return closes


def _closes(symbols_by_account: Dict[int, List[str]], start: date, now: datetime, calls: Dict[str, int]
            ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, str]]:
    """Per account (one source failing marks only its symbols), cache first, within ``HISTORY_BUDGET_S``."""
    out: Dict[str, Dict[str, float]] = {}
    missing: Dict[str, str] = {}
    budget_ends = time.monotonic() + HISTORY_BUDGET_S
    for account_id in sorted(symbols_by_account):
        symbols = [s for s in symbols_by_account[account_id] if s not in out]
        if not symbols:
            continue
        try:
            source = _history_source(account_id)
        except Exception as e:  # noqa: BLE001 -- deliberate: a bad source setting marks this account's symbols only
            reason = price_history.redact_message(f"account {account_id}'s history source: {type(e).__name__}: {e}")
            missing.update({s: reason for s in symbols})
            continue
        todo = []
        for s in symbols:
            hit = _cached_closes(source, s, start, now)
            if hit is not None:
                out[s] = hit
                missing.pop(s, None)
            else:
                todo.append(s)
        if not todo:
            continue
        remaining = budget_ends - time.monotonic()
        if remaining <= 1.0:
            missing.update({s: "the history reads ran out of time" for s in todo})
            continue
        calls["history_symbols"] += len(todo)
        errors: Dict[str, str] = {}
        try:
            got = price_history.fetch_closes({account_id: todo}, start, deadline_s=remaining, errors=errors)
        except Exception as e:  # noqa: BLE001 -- deliberate: this account's source down = its symbols n/a, with why
            reason = price_history.redact_message(f"{type(e).__name__}: {e}")
            logger.warning(f"Heatmap: daily history of account {account_id} unavailable: {reason}")
            missing.update({s: reason for s in todo})
            continue
        for s in todo:
            if s in got:
                out[s] = got[s]
                missing.pop(s, None)
                with _CLOSES_LOCK:
                    _CLOSES[(source, s)] = (now, start, got[s])
            elif s in errors:
                missing[s] = errors[s]
            elif s in (got.unfetched or []):
                missing[s] = "the history source did not answer in time"
            else:
                missing[s] = "the history source has no daily closes for it"
    return out, missing


# --------------------------------------------------------------------------------------------------------------
# the broker
# --------------------------------------------------------------------------------------------------------------

class _CountingAccount:
    """Passes ``get_instrument_current_price`` through and counts it (for the load's cost line)."""

    def __init__(self, account, calls: Dict[str, int], lock: threading.Lock):
        self._account = account
        self._calls = calls
        self._lock = lock

    def get_instrument_current_price(self, *args, **kwargs):
        with self._lock:
            self._calls["feed_price_calls"] += 1
        return self._account.get_instrument_current_price(*args, **kwargs)


def _broker_quantities(positions) -> Dict[str, float]:
    """``{symbol: signed qty}`` of the EQUITY positions in a positions read (the platform's Position convention: a
    short is negative). Option rows and unusable quantities are skipped."""
    out: Dict[str, float] = {}
    for pos in positions:
        row = pos if isinstance(pos, dict) else dict(pos)
        symbol = normalize_symbol(row.get("symbol"))
        qty = row.get("qty")
        if not symbol or "option" in str(row.get("asset_class") or "").lower():
            continue
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(float(qty)):
            continue
        out[symbol] = out.get(symbol, 0.0) + float(qty)
    return out


def _account_work(account_id: int, symbols: List[str], calls: Dict[str, int], lock: threading.Lock):
    """``(prices, missing, broker_qty or None)`` for one account. ONE positions read; a failed or raised read falls
    back to the price feed for every symbol (no second read)."""
    account = get_account_instance_from_id(account_id)
    if account is None:
        return {}, {s: f"account {account_id} cannot be instantiated" for s in symbols}, None
    with lock:
        calls["positions"] += 1
    with share_positions():
        try:
            positions = broker_read(account_id, "the positions fetch", read_positions, account, account_id)
        except Exception as e:  # noqa: BLE001 -- deliberate: a failed read is priced from the feed, not refused
            logger.warning(f"Heatmap: account {account_id} positions read failed ({type(e).__name__}); "
                           f"pricing from the account price feed")
            positions = None
    if positions is None:
        logger.warning(f"Heatmap: account {account_id} returned no positions; pricing from the account price feed")
    held = list(positions) if positions is not None else []
    missing: Dict[str, str] = {}
    try:
        got = held_position_prices(_CountingAccount(account, calls, lock), account_id, symbols, held)
    except Exception as e:  # noqa: BLE001 -- deliberate: the feed failing leaves the broker-held prices standing
        logger.warning(f"Heatmap: account {account_id} price feed failed: {type(e).__name__}: {e}")
        got = {s: p for s, p in broker_position_prices_from(held).items() if s in symbols}
        missing.update({s: f"account {account_id}'s price feed failed ({type(e).__name__})"
                        for s in symbols if s not in got})
    prices = {s: p for s, p in ((s, _positive(got.get(s))) for s in symbols) if p is not None}
    for s in symbols:
        if s not in prices and s not in missing:
            missing[s] = f"account {account_id} returned no usable price"
    return prices, missing, (_broker_quantities(held) if positions is not None else None)


def _prices(lots: List[Lot], calls: Dict[str, int], timing
            ) -> Tuple[Dict[str, float], Dict[str, str], Dict[int, List[str]], Dict[int, Dict[str, float]]]:
    by_account: Dict[int, set] = defaultdict(set)
    orphan: set = set()
    for l in lots:
        if l.account_id is not None:
            by_account[l.account_id].add(l.symbol)
        else:
            orphan.add(l.symbol)
    lock = threading.Lock()
    results: Dict[int, Tuple] = {}
    failures: Dict[int, BaseException] = {}

    def run(aid, syms):
        try:
            results[aid] = _account_work(aid, syms, calls, lock)
        except BaseException as e:  # noqa: BLE001 -- reported per account below
            failures[aid] = e

    with _step(timing, "broker", f"{len(by_account)} accounts"):
        threads = {aid: threading.Thread(target=run, args=(aid, sorted(syms)), daemon=True, name=f"heatmap-acct{aid}")
                   for aid, syms in by_account.items()}
        for t in threads.values():
            t.start()
        ends = time.monotonic() + BROKER_DEADLINE_S
        for t in threads.values():
            t.join(max(0.0, ends - time.monotonic()))
    prices: Dict[str, float] = {}
    missing: Dict[str, str] = {}
    broker_qty: Dict[int, Dict[str, float]] = {}
    for aid in sorted(by_account):
        symbols = sorted(by_account[aid])
        if threads[aid].is_alive() or (aid not in results and aid not in failures):
            missing.update({s: f"account {aid} timed out (no answer within {BROKER_DEADLINE_S:.0f}s)"
                            for s in symbols if s not in prices})
            continue
        if aid in failures:
            e = failures[aid]
            logger.warning(f"Heatmap: account {aid} could not be priced: {type(e).__name__}: {e}")
            missing.update({s: f"account {aid} could not be priced ({type(e).__name__})"
                            for s in symbols if s not in prices})
            continue
        got, why, held = results[aid]
        for s in symbols:
            if s in got and s not in prices:
                prices[s] = got[s]
                missing.pop(s, None)
            elif s not in prices and s in why:
                missing[s] = why[s]
        if held is not None:
            broker_qty[aid] = held
    for s in sorted(orphan - set(prices)):
        missing.setdefault(s, "the transaction has no order and no expert, so its account is unknown")
    return prices, missing, {a: sorted(v) for a, v in by_account.items()}, broker_qty


def _step(timing, name, note=None):
    from .load_timing import step
    return step(timing, name, note)


def _bounded(fn: Callable, symbols: List[str], what: str, calls: Dict[str, int]):
    if not symbols:
        return {}, {}
    try:
        return _call_with_deadline(lambda: fn(symbols, calls=calls), MARKET_DATA_DEADLINE_S, f"heatmap {what}")
    except _Deadline:
        return {}, {s: f"the {what} did not answer within {MARKET_DATA_DEADLINE_S:.0f}s" for s in symbols}


# --------------------------------------------------------------------------------------------------------------
# the load
# --------------------------------------------------------------------------------------------------------------

def _load(account_id: Optional[int], now: datetime, timing) -> HeatmapData:
    calls: Dict[str, int] = {"positions": 0, "feed_price_calls": 0, "history_symbols": 0, "fmp_quote_calls": 0,
                             "split_calendar_reads": 0}
    with _step(timing, "calendar"):
        warm_calendar(now)                     # the session tables are built here, never on the event loop
    with _step(timing, "db") as entry:
        txs, orders_of, expert_names, account_of = _read_rows(account_id)
        lots = lots_from_rows(txs, orders_of, expert_names, account_of)
        entry.note = f"{len(lots)} open transactions"
    equity = [l for l in lots if not l.is_option]
    symbols = sorted({l.symbol for l in equity})
    prices, price_missing, symbols_by_account, broker_qty = _prices(equity, calls, timing)

    market_data: Dict[str, Tuple] = {}

    def side_reads():
        market_data["caps"] = _bounded(fetch_market_caps, symbols, "market cap fetch", calls)
        market_data["splits"] = _bounded(fetch_split_days, symbols, "split calendar", calls)

    side = threading.Thread(target=side_reads, daemon=True, name="heatmap-market-data")
    side.start()                               # in parallel with the history reads
    with _step(timing, "history", f"{len(symbols)} symbols"):
        closes, closes_missing = _closes(symbols_by_account, history_start(now), now, calls)
    with _step(timing, "market caps + splits"):
        side.join(2 * MARKET_DATA_DEADLINE_S + 1)
    caps, caps_missing = market_data.get("caps") or ({}, {s: "the market cap fetch did not finish" for s in symbols})
    splits, splits_missing = market_data.get("splits") or (
        {}, {s: "the split calendar did not finish" for s in symbols})
    with _step(timing, "labels"):
        labels = get_labels_by_symbol(symbols) if symbols else {}
    market = MarketInputs(prices=prices, price_missing=price_missing, closes=closes, closes_missing=closes_missing,
                          market_caps=caps, market_caps_missing=caps_missing, labels=labels, splits=splits,
                          splits_missing=splits_missing, broker_qty=broker_qty)
    return HeatmapData(lots=lots, market=market, calls=calls)


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: Dict[Optional[int], Future] = {}


def load_heatmap_data(account_id: Optional[int], now: datetime, *, timing=None) -> HeatmapData:
    """Everything one heatmap load needs, for the header's account (``None`` = all accounts), at ``now`` (tz-aware).
    Worker thread only. A load of a scope that is already loading (another browser tab) waits for that one."""
    with _INFLIGHT_LOCK:
        running = _INFLIGHT.get(account_id)
        owner = running is None
        if owner:
            running = _INFLIGHT[account_id] = Future()
    if not owner:
        with _step(timing, "shared in-flight load"):
            return running.result()
    try:
        data = _load(account_id, now, timing)
        running.set_result(data)
        return data
    except BaseException as e:
        running.set_exception(e)
        raise
    finally:
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(account_id) is running:
                del _INFLIGHT[account_id]
