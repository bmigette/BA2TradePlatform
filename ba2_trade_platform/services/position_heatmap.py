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
TastyTrade session marks that account "timed out"); the history reads share ``HISTORY_BUDGET_S``; the market caps
run in their own thread WHILE the history is read; the split calendar is read AFTER it (the history refresh fills the
same memo), at most ``price_history.MAX_CONCURRENT`` requests at a time ON THE HISTORY PATH'S SHARED download
slots, and keeps every calendar that arrived within
``MARKET_DATA_DEADLINE_S`` (only the rest are "not checked"). The broker's transaction history (TastyTrade) and the
platform's filled orders it is cross-checked with run in their own thread from the end of the positions reads, WHILE
the tile inputs are read, and are joined last with what is left of ``broker_share_history.WAIT_S`` (never extending
the load); anything failing there makes only that account's "My P/L" n/a. The tab adds an overall bound on top.

TIMING. One ``[PERF:page:load] Heatmap(...)`` line per load: every step with its own wall time (the parallel market
caps included) and counts -- symbols read / cache or memo hits / failed / timed out -- so one line explains a slow
load. ``progress(text)`` reports the running step to the tab's status line.

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
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlmodel import and_, or_, select

from ba2_common.config import get_app_setting
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions

from ..core.db import get_db
from ..core.models import ExpertInstance, TradingOrder, Transaction
from ..core.types import AssetClass, OrderDirection, OrderStatus, OrderType
from ..core.utils import get_account_instance_from_id, get_labels_by_symbol
from ..logger import logger
from ..ui.utils.position_heatmap import (NO_EXPERT, ORDER_LOOKBACK_S, TIMEFRAMES, BrokerHistory, Fill, Lot,
                                        MarketInputs, PlatformOrder, reference_session,
                                        regular_session_close_utc, warm_calendar)
from . import broker_share_history as bsh
from . import price_history
from .errors import PriceSourceUnreachable
from ._positions_cache import POSITIONS_TTL_S, read_positions, share_positions
from .account_scope import account_transaction_ids
from .account_fanout import BROKER_DEADLINE_S, FAILED as FANOUT_FAILED, OK as FANOUT_OK, accounts_in_scope, fan_out
from .accounts import broker_read
from .open_pnl import PRICED_STATUSES, _account_of
from .position_prices import broker_position_prices_from, held_position_prices

# BROKER_DEADLINE_S (account_fanout): one account's positions read + price-feed call.
#: Every account's history reads together.
HISTORY_BUDGET_S = 45.0
#: The market caps, and separately the split calendars.
MARKET_DATA_DEADLINE_S = 20.0
#: How long to wait before asking a BUSY history source again (every shared download slot in use).
HISTORY_BUSY_RETRY_S = 1.0
HISTORY_BUSY = "history busy, reload"
#: A history read is not started with less time than this left in the budget.
MIN_HISTORY_READ_S = 0.2
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


def _canonical(raw) -> str:
    """ONE spelling per instrument, for every map this load builds: the repo's canonicaliser
    (``ba2_providers.screener.symbol_universe.canonical_symbol``, ``BRK.B`` -> ``BRK-B``) -- the spelling of FMP and of
    the Alpaca history store. Alpaca and IBKR (``ibkr_mapping.from_ib_symbol``) report a share class with a dot,
    TastyTrade passes its own symbol through (a slash, ``BRK/B``): the slash is read as the dot first."""
    return bsh.canonical_symbol(raw)


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
        lots.append(Lot(transaction_id=tx.id, symbol=_canonical(tx.symbol) or tx.symbol, expert=expert,
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
    asked again on the next load. ``calls`` (when given) must carry ``fmp_quote_calls`` and
    ``market_cap_cache_hits``."""
    from ba2_providers.fmp_common import FMP_LIVE_CACHE_MISS, fmp_live_cache_get, fmp_live_cache_put, redact
    from ..core.fmp_live_price import FMP_QUOTE_BATCH, fetch_fmp_quote_rows, to_fmp_symbol

    caps: Dict[str, float] = {}
    missing: Dict[str, str] = {}
    to_ask: Dict[str, List[str]] = {}
    for s in dict.fromkeys(symbols):
        hit = fmp_live_cache_get(_CAP_KEY.format(to_fmp_symbol(s)), MARKET_CAP_TTL_S)
        if hit is not FMP_LIVE_CACHE_MISS:
            caps[s] = hit
            if calls is not None:
                calls["market_cap_cache_hits"] += 1
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


def fetch_split_days(symbols: Iterable[str], *, calls: Optional[Dict[str, int]] = None,
                     deadline_s: float = None, workers: int = None,
                     progress: Optional[Callable[[str], None]] = None
                     ) -> Tuple[Dict[str, List[date]], Dict[str, str]]:
    """``({symbol: [split dates]}, {symbol: why unknown})`` from FMP's split calendar (``symbol_info.fetch_splits``:
    one FMP request per symbol through ``fmp_http_get``, 1-day memo). Every dated split counts, its ratio known or not.

    A symbol whose calendar is already in the memo (the FMP history provider's daily refresh puts it there) is a memo
    hit. The rest are read by at most ``price_history.MAX_CONCURRENT`` threads, each request holding one of the
    history path's shared download slots. PARTIAL on timeout: after ``deadline_s`` every
    calendar that arrived is returned; only the symbols still unanswered are "did not answer". ``calls`` (when given)
    must carry ``split_calendar_reads``, ``split_memo_hits`` and ``split_timed_out``."""
    from ba2_providers import symbol_info
    from ba2_providers.fmp_common import FMP_LIVE_CACHE_MISS, fmp_live_cache_get, redact
    from ..core.fmp_live_price import to_fmp_symbol

    deadline_s = MARKET_DATA_DEADLINE_S if deadline_s is None else deadline_s
    workers = price_history.MAX_CONCURRENT if workers is None else min(workers, price_history.MAX_CONCURRENT)
    ends = time.monotonic() + deadline_s
    stop = threading.Event()
    wanted = list(dict.fromkeys(symbols))
    if not wanted:
        return {}, {}
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return {}, {s: "FMP_API_KEY is not configured" for s in wanted}
    lock = threading.Lock()
    out: Dict[str, List[date]] = {}
    missing: Dict[str, str] = {}
    done = threading.Event()
    state = {"finished": 0}

    def count(key):
        if calls is not None:
            with lock:
                calls[key] += 1

    def read(s):
        try:
            got = [e.date for e in symbol_info.parse_splits(symbol_info.fetch_splits(api_key, to_fmp_symbol(s)))]
        except Exception as e:  # noqa: BLE001 -- deliberate: one symbol's calendar failing is that symbol's note
            got, why = None, f"the FMP split calendar failed ({type(e).__name__}: {redact(str(e))})"
        with lock:
            if got is None:
                missing[s] = why
            else:
                out[s] = got
            state["finished"] += 1
            finished = state["finished"]
            if finished == len(wanted):
                done.set()
        if progress is not None:
            progress(f"Checking splits: {finished} of {len(wanted)} symbols")

    queue = []
    for s in wanted:
        # The memo key of ``symbol_info.fetch_splits`` (pinned by test_a_split_calendar_the_history_read_...): a hit
        # is read straight away, the rest go to the pool.
        if fmp_live_cache_get(f"{symbol_info._KEY_PREFIX}:stock_split:{to_fmp_symbol(s).upper()}",
                              symbol_info.CACHE_TTL_SECONDS) is not FMP_LIVE_CACHE_MISS:
            count("split_memo_hits")
            read(s)
        else:
            queue.append(s)
    pending = list(queue)
    if queue:
        def worker():
            # Each request holds one of the history path's SHARED download slots (``price_history.download_slot``):
            # the process-wide cap on calls against the FMP key is never exceeded, whoever else is downloading.
            # After the deadline nothing new starts.
            while not stop.is_set():
                with price_history.download_slot(ends - time.monotonic()) as slot:
                    if not slot or stop.is_set():
                        return
                    with lock:
                        if not pending:
                            return
                        s = pending.pop(0)
                    count("split_calendar_reads")
                    read(s)

        for i in range(min(workers, len(queue))):
            threading.Thread(target=worker, daemon=True, name=f"heatmap-splits-{i}").start()
        done.wait(max(0.0, deadline_s))
        stop.set()
    with lock:
        late = [s for s in wanted if s not in out and s not in missing]
        pending.clear()                        # nothing new starts after the deadline
        for s in late:
            missing[s] = f"the split calendar did not answer within {deadline_s:.0f}s"
        if calls is not None:
            calls["split_timed_out"] += len(late)
        return dict(out), dict(missing)


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
    bsh.clear()


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


def _read_history(account_id: int, symbols: List[str], start: date, end: Optional[datetime], budget_ends: float):
    """``(closes, {symbol: why missing}, None)`` from ``fetch_closes``, or ``(None, None, reason)`` when the
    account's source failed as a whole. A BUSY source (every shared download slot in use) is asked again every
    ``HISTORY_BUSY_RETRY_S`` until ``budget_ends``; still busy then = ``HISTORY_BUSY``, not a failure."""
    errors: Dict[str, str] = {}
    while True:
        try:
            got = price_history.fetch_closes({account_id: symbols}, start, deadline_s=budget_ends - time.monotonic(),
                                             errors=errors, end=end)
            break
        except PriceSourceUnreachable as e:
            if not e.detail.get("busy"):
                reason = price_history.redact_message(f"{type(e).__name__}: {e}")
                logger.warning(f"Heatmap: daily history of account {account_id} unavailable: {reason}")
                return None, None, reason
            if budget_ends - time.monotonic() <= HISTORY_BUSY_RETRY_S:
                logger.warning(f"Heatmap: daily history of account {account_id}: every download slot stayed busy")
                return None, None, HISTORY_BUSY
            time.sleep(HISTORY_BUSY_RETRY_S)
        except Exception as e:  # noqa: BLE001 -- deliberate: this account's source down = its symbols n/a, with why
            reason = price_history.redact_message(f"{type(e).__name__}: {e}")
            logger.warning(f"Heatmap: daily history of account {account_id} unavailable: {reason}")
            return None, None, reason
    why: Dict[str, str] = {}
    for s in symbols:
        if s in got:
            continue
        if s in errors:
            why[s] = errors[s]
        elif s in (got.unfetched or []):
            why[s] = "the history source did not answer in time"
        else:
            why[s] = "the history source has no daily closes for it"
    return dict(got), why, None


def _closes(symbols_by_account: Dict[int, List[str]], needed: Sequence[date], now: datetime, calls: Dict[str, int],
            progress: Optional[Callable[[str], None]] = None
            ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, str]]:
    """The daily closes of the ``needed`` reference sessions, per account (one source failing marks only its
    symbols), cache first, within ``HISTORY_BUDGET_S``.

    FROM DISK FIRST. The first read is PINNED to the newest reference session (a past end date): the history provider
    serves it from its own daily cache, with no vendor call. Only a symbol whose answer lacks one of the ``needed``
    closes (its cache file ends before it, or there is none) is read again as a LATEST read, which tops the provider's
    cache up exactly as before. A LATEST read of every symbol was what made the first load slow: a daily file older
    than a day, or ANY file while a session is open or settling (once per 10 minutes per symbol), is re-fetched from
    the vendor, with its split check, even though the heatmap never needs the forming bar.

    Progress is reported per account (``price_history.fetch_closes`` reads one account's symbols in one call)."""
    out: Dict[str, Dict[str, float]] = {}
    missing: Dict[str, str] = {}
    start = min(needed)
    wanted_days = {d.isoformat() for d in needed}
    newest = max(needed)
    pinned_end = datetime(newest.year, newest.month, newest.day, 23, 59)      # naive, like price_history's now
    budget_ends = time.monotonic() + HISTORY_BUDGET_S
    busy_seen = False
    total = len({s for v in symbols_by_account.values() for s in v})

    def report():
        if progress is not None:
            progress(f"Reading daily history: {len(out) + len(missing)} of {total} symbols")

    def complete(closes) -> bool:
        return closes is not None and wanted_days <= set(closes)

    for account_id in sorted(symbols_by_account):
        report()
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
                calls["history_cache_hits"] += 1
            else:
                todo.append(s)
        if not todo:
            continue
        if budget_ends - time.monotonic() <= MIN_HISTORY_READ_S:
            out_of_time = HISTORY_BUSY if busy_seen else "the history reads ran out of time"
            missing.update({s: out_of_time for s in todo})
            continue
        calls["history_symbols"] += len(todo)
        got, why, failure = _read_history(account_id, todo, start, pinned_end, budget_ends)
        if failure is not None:
            busy_seen = busy_seen or failure == HISTORY_BUSY
            missing.update({s: failure for s in todo})
            continue
        short = [s for s in todo if not complete(got.get(s))]
        if short and budget_ends - time.monotonic() > MIN_HISTORY_READ_S:
            calls["history_topped_up"] += len(short)
            latest, why_latest, failure = _read_history(account_id, short, start, None, budget_ends)
            for s in short:
                if failure is not None:
                    why[s] = failure
                elif s in latest:
                    got[s] = latest[s]
                    why.pop(s, None)
                else:
                    why[s] = why_latest[s]
        elif short:
            why.update({s: "the history reads ran out of time" for s in short if s not in got})
        for s in todo:
            if s in got:
                out[s] = got[s]
                if complete(got[s]):
                    missing.pop(s, None)
                    with _CLOSES_LOCK:
                        _CLOSES[(source, s)] = (now, start, got[s])
                elif s in why:
                    missing[s] = why[s]         # partial closes: the reason a reference close is missing
            else:
                missing[s] = why[s]
                if why[s] == "the history source did not answer in time":
                    calls["history_timed_out"] += 1
    calls["history_busy"] = len([s for s, why in missing.items() if why == HISTORY_BUSY])
    calls["history_failed"] = (len([s for s in missing if s not in out]) - calls["history_timed_out"]
                               - calls["history_busy"])
    report()
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


def _broker_book(positions) -> Tuple[Dict[str, float], int, Dict[str, str]]:
    """``({canonical symbol: signed qty} of the EQUITY rows, number of OPTION rows, {canonical: the broker's own
    spelling})`` of a positions read (the platform's Position convention: a short is negative). Alpaca lists option
    positions here (``asset_class`` ``us_option``); IBKR and TastyTrade return the equity book only. Unusable
    quantities are skipped."""
    out: Dict[str, float] = {}
    spelled: Dict[str, str] = {}
    options = 0
    for pos in positions:
        row = pos if isinstance(pos, dict) else dict(pos)
        raw = row.get("symbol")
        symbol = _canonical(raw)
        if not symbol:
            continue
        if "option" in str(row.get("asset_class") or "").lower():
            options += 1
            continue
        qty = row.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(float(qty)):
            continue
        out[symbol] = out.get(symbol, 0.0) + float(qty)
        spelled.setdefault(symbol, str(raw).strip())
    return out, options, spelled


def _account_work(account_id: int, raw_symbols: List[str], calls: Dict[str, int], lock: threading.Lock) -> Dict:
    """One account: ``{prices, missing, book (None = not read), option_rows, failure}``, all keyed by CANONICAL symbol.
    ONE positions read and, for the platform symbols the broker does not hold, the account's price feed -- nothing
    else at the broker. A failed or raised positions read falls back to the feed for every symbol (no second read)
    and is reported as ``failure``. The broker and the feed are asked in THEIR spelling (the platform's stored one for
    a symbol the broker does not hold); only the answers are canonicalised."""
    account = get_account_instance_from_id(account_id)
    if account is None:
        why = f"account {account_id} cannot be instantiated"
        return {"prices": {}, "missing": {_canonical(s): why for s in raw_symbols}, "book": None, "option_rows": 0,
                "failure": why, "account": None, "positions_at": None, "positions_seq": None}
    with lock:
        calls["positions"] += 1
    failure = None
    with share_positions():
        try:
            positions = broker_read(account_id, "the positions fetch", read_positions, account, account_id)
        except Exception as e:  # noqa: BLE001 -- deliberate: a failed read is priced from the feed, not refused
            logger.warning(f"Heatmap: account {account_id} positions read failed ({type(e).__name__}); "
                           f"pricing from the account price feed")
            positions, failure = None, f"the positions read failed ({type(e).__name__})"
    positions_at = datetime.now(timezone.utc)
    positions_seq = bsh.next_seq()          # AFTER the read: a history sync started later is newer than these positions
    if positions is None:
        failure = failure or "the positions read returned nothing"
        logger.warning(f"Heatmap: account {account_id}: {failure}; pricing from the account price feed")
    held = list(positions) if positions is not None else []
    book, option_rows, spelled = _broker_book(held)
    held_canonical = set(book)
    wanted = sorted({s for s in raw_symbols if _canonical(s) not in held_canonical} | set(spelled.values()))
    missing: Dict[str, str] = {}
    try:
        got = held_position_prices(_CountingAccount(account, calls, lock), account_id, wanted, held)
    except Exception as e:  # noqa: BLE001 -- deliberate: the feed failing leaves the broker-held prices standing
        logger.warning(f"Heatmap: account {account_id} price feed failed: {type(e).__name__}: {e}")
        got = {s: p for s, p in broker_position_prices_from(held).items() if s in wanted}
        missing.update({_canonical(s): f"account {account_id}'s price feed failed ({type(e).__name__})"
                        for s in wanted if s not in got})
    prices: Dict[str, float] = {}
    for raw in wanted:
        price = _positive(got.get(raw))
        if price is not None:
            prices.setdefault(_canonical(raw), price)
    for raw in wanted:
        c = _canonical(raw)
        if c not in prices and c not in missing:
            missing[c] = f"account {account_id} returned no usable price"
    return {"prices": prices, "missing": missing, "book": book if positions is not None else None,
            "option_rows": option_rows, "failure": failure, "account": account, "positions_at": positions_at,
            "positions_seq": positions_seq}


# --------------------------------------------------------------------------------------------------------------
# the broker's transaction history (TastyTrade: ``get_share_movements``)
# --------------------------------------------------------------------------------------------------------------

def _has_share_history(account) -> bool:
    """Whether the account type offers the share-movement read (TastyTrade). Asked of the CLASS: a broker object is
    asked for nothing but its positions and that read."""
    return callable(getattr(type(account), "read_share_movements", None))


def _platform_orders(accounts: Sequence[int], since: datetime) -> Dict[int, List[PlatformOrder]]:
    """The platform's FILLED equity orders of ``accounts`` created since ``since`` (any transaction, or none: the
    allocator's sells of pre-existing shares have no transaction) -- the broker history must show them."""
    if not accounts:
        return {}
    executed, terminal = OrderStatus.get_executed_statuses(), OrderStatus.get_terminal_statuses()
    out: Dict[int, List[PlatformOrder]] = defaultdict(list)
    with get_db() as session:
        rows = session.exec(select(TradingOrder).where(TradingOrder.account_id.in_(list(accounts)))
                            .where(TradingOrder.created_at >= since.replace(tzinfo=None))).all()
        for o in rows:
            if o.asset_class == AssetClass.OPTION or not o.filled_qty or not o.filled_qty > 0:
                continue
            if o.status not in executed and o.status not in terminal:
                continue
            created = _aware(o.created_at)
            symbol = _canonical(o.symbol)
            if created is None or not symbol:
                continue
            out[o.account_id].append(PlatformOrder(symbol=symbol, side="BUY" if o.side == OrderDirection.BUY else
                                                   "SELL", quantity=float(o.filled_qty), created_at=created))
    return dict(out)


def _histories_in_background(read: "_BrokerRead", first_day: date, since: datetime, calls: Dict[str, int]):
    """Start the history path in its own thread: the platform's filled orders, then for every account with the
    share-movement read whose positions were read a stored-copy sync that STARTED after those positions
    (``broker_share_history.request_sync``), each waited for until ``broker_share_history.WAIT_S`` after the positions
    read. It runs WHILE the tile inputs are read and can never fail them: every exception in it becomes that account's
    ``BrokerHistory(failure=...)`` ("My P/L" n/a), the quantities and prices stand regardless.

    Returns ``finish()``: ``(histories, platform orders, wall seconds, note)``, waiting only for what is left of the
    WAIT_S window (nothing, when the tile inputs took longer)."""
    accounts = sorted(read.share_accounts)
    began = time.monotonic()
    deadline = began + bsh.WAIT_S
    lock = threading.Lock()
    box: Dict[str, Any] = {"histories": {}, "orders": {}, "seconds": None}

    def put(aid, history):
        with lock:
            box["histories"].setdefault(aid, history)

    def fail_all(why):
        for aid in accounts:
            put(aid, BrokerHistory(positions_at=read.share_accounts[aid][1], failure=why))

    def run():
        try:
            orders = _platform_orders(accounts, since)
        except Exception as e:  # noqa: BLE001 -- the cross-check cannot run: "My P/L" n/a, the tiles stand
            why = bsh.redacted(f"the platform's orders could not be read ({type(e).__name__}: {e})")
            logger.warning(f"Heatmap: {why}")
            fail_all(why)
            box["seconds"] = time.monotonic() - began
            return
        with lock:
            box["orders"] = orders
        futures = {}
        for aid in accounts:
            account, positions_at, positions_seq = read.share_accounts[aid]
            try:
                futures[aid] = bsh.request_sync(aid, account, first_day, positions_seq)
                with lock:
                    calls["history_syncs"] += 1
            except Exception as e:  # noqa: BLE001 -- this account's "My P/L" n/a
                why = bsh.redacted(f"the broker history sync could not start ({type(e).__name__}: {e})", account)
                logger.warning(f"Heatmap: account {aid}: {why}")
                put(aid, BrokerHistory(positions_at=positions_at, failure=why))
        for aid, future in futures.items():
            account, positions_at, _seq = read.share_accounts[aid]
            try:
                store, why = bsh.wait_for_store(future, deadline - time.monotonic())
                put(aid, BrokerHistory(positions_at=positions_at, failure=why) if store is None
                    else bsh.history_from_store(store, positions_at))
            except Exception as e:  # noqa: BLE001 -- a stored row the code cannot read: this account's n/a
                why = bsh.redacted(f"the stored broker history could not be used ({type(e).__name__}: {e})", account)
                logger.warning(f"Heatmap: account {aid}: {why}")
                put(aid, BrokerHistory(positions_at=positions_at, failure=why))
        box["seconds"] = time.monotonic() - began

    t = threading.Thread(target=run, daemon=True, name="heatmap-share-history")
    t.start()

    def finish():
        t.join(max(0.0, deadline - time.monotonic()))
        with lock:
            histories, orders = dict(box["histories"]), dict(box["orders"])
        for aid in accounts:                       # not answered within the window: still loading
            histories.setdefault(aid, BrokerHistory(positions_at=read.share_accounts[aid][1],
                                                    failure=bsh.STILL_LOADING))
        ready = sum(1 for h in histories.values() if h.failure is None)
        seconds = box["seconds"] if box["seconds"] is not None else time.monotonic() - began
        return histories, orders, seconds, f"{len(accounts)} accounts: {ready} ready, {len(accounts) - ready} not ready"

    return finish


@dataclass
class _BrokerRead:
    prices: Dict[str, float] = field(default_factory=dict)
    missing: Dict[str, str] = field(default_factory=dict)
    symbols_by_account: Dict[int, List[str]] = field(default_factory=dict)
    broker_qty: Dict[int, Dict[str, float]] = field(default_factory=dict)
    broker_failed: Dict[int, str] = field(default_factory=dict)
    option_rows: Dict[int, int] = field(default_factory=dict)
    #: accounts with the share-movement read whose positions were read: (the account, when its positions were read,
    #: the ``broker_share_history.next_seq`` taken right after that read)
    share_accounts: Dict[int, Tuple[Any, datetime, int]] = field(default_factory=dict)


def _brokers(lots: List[Lot], accounts: Sequence[int], raw_by_account: Dict[int, set], calls: Dict[str, int],
             timing) -> _BrokerRead:
    """Every account in scope -- also one with no platform rows -- in parallel, each under ``BROKER_DEADLINE_S``: ONE
    positions read (+ the feed for symbols the broker does not hold). The share-movement history is NOT read here:
    it is the stored copy's own sync (``_histories_in_background``), after these positions."""
    by_account: Dict[int, set] = defaultdict(set)
    orphan: set = set()
    for a in accounts:
        by_account[a]
    for l in lots:
        if l.account_id is not None:
            by_account[l.account_id].add(l.symbol)
        else:
            orphan.add(l.symbol)
    lock = threading.Lock()

    def run(aid):
        res = _account_work(aid, sorted(raw_by_account.get(aid) or ()), calls, lock)
        res["share_history"] = res["book"] is not None and _has_share_history(res["account"])
        return res

    out = _BrokerRead()
    with _step(timing, "broker") as entry:
        outcomes, still_running = fan_out(list(by_account), run, BROKER_DEADLINE_S, "heatmap-acct")
        results: Dict[int, Dict] = {aid: o.value for aid, o in outcomes.items() if o.status == FANOUT_OK}
        failures: Dict[int, BaseException] = {aid: o.error for aid, o in outcomes.items() if o.status == FANOUT_FAILED}
        with lock:
            entry.note = (f"{len(by_account)} accounts: {calls['positions']} positions read, "
                          f"{calls['feed_price_calls']} feed calls, {len(still_running)} timed out")
    for aid in sorted(by_account):
        symbols = sorted(by_account[aid])
        out.symbols_by_account[aid] = symbols
        if aid not in results and aid not in failures:
            why = f"account {aid} timed out (no answer within {BROKER_DEADLINE_S:.0f}s)"
            out.broker_failed[aid] = why
            out.missing.update({s: why for s in symbols if s not in out.prices})
            continue
        if aid not in results:
            e = failures[aid]
            logger.warning(f"Heatmap: account {aid} could not be priced: {type(e).__name__}: {e}")
            why = f"account {aid} could not be read ({type(e).__name__})"
            out.broker_failed[aid] = why
            out.missing.update({s: why for s in symbols if s not in out.prices})
            continue
        res = results[aid]
        if res["share_history"]:
            out.share_accounts[aid] = (res["account"], res["positions_at"], res["positions_seq"])
        if res["book"] is None:
            out.broker_failed[aid] = res["failure"]
        else:
            out.broker_qty[aid] = res["book"]
            out.option_rows[aid] = res["option_rows"]
            out.symbols_by_account[aid] = sorted(set(symbols) | set(res["book"]))
        for s in out.symbols_by_account[aid]:
            if s in res["prices"] and s not in out.prices:
                out.prices[s] = res["prices"][s]
                out.missing.pop(s, None)
            elif s not in out.prices and s in res["missing"]:
                out.missing[s] = res["missing"][s]
    for s in sorted(orphan - set(out.prices)):
        out.missing.setdefault(s, "the transaction has no order and no expert, so its account is unknown")
    return out


#: The header's account, or every account (``account_fanout.accounts_in_scope``: shared with the Live View).
_accounts_in_scope = accounts_in_scope


def _step(timing, name, note=None):
    from .load_timing import step
    return step(timing, name, note)


# --------------------------------------------------------------------------------------------------------------
# the load
# --------------------------------------------------------------------------------------------------------------

def _market_caps_in_background(symbols: List[str], calls: Dict[str, int]):
    """Start the market-cap read in its own thread (it runs while the history is read). Returns ``finish()``:
    ``((caps, missing), wall seconds, note)``, waiting at most ``MARKET_DATA_DEADLINE_S`` from the start."""
    box: Dict[str, object] = {}
    began = time.monotonic()

    def run():
        try:
            box["value"] = fetch_market_caps(symbols, calls=calls)
        except Exception as e:  # noqa: BLE001 -- deliberate: no market caps = small bordered tiles, with why
            box["error"] = e
        box["seconds"] = time.monotonic() - began

    t = threading.Thread(target=run, daemon=True, name="heatmap-market-caps")
    t.start()

    def finish():
        t.join(max(0.0, began + MARKET_DATA_DEADLINE_S - time.monotonic()))
        if t.is_alive():
            return ({}, {s: f"the market cap fetch did not answer within {MARKET_DATA_DEADLINE_S:.0f}s"
                         for s in symbols}), time.monotonic() - began, f"{len(symbols)} symbols: TIMED OUT"
        if "error" in box:
            e = box["error"]
            why = price_history.redact_message(f"the market cap fetch failed ({type(e).__name__}: {e})")
            return ({}, {s: why for s in symbols}), box["seconds"], f"{len(symbols)} symbols: FAILED"
        caps, missing = box["value"]
        note = (f"{len(symbols)} symbols: {calls['market_cap_cache_hits']} cache hits, "
                f"{calls['fmp_quote_calls']} FMP calls, {len(missing)} missing")
        return (caps, missing), box["seconds"], note

    return finish


def _load(account_id: Optional[int], now: datetime, timing,
          progress: Optional[Callable[[str], None]] = None) -> HeatmapData:
    say = progress or (lambda text: None)
    calls: Dict[str, int] = {"positions": 0, "feed_price_calls": 0, "history_symbols": 0, "history_cache_hits": 0,
                             "history_failed": 0, "history_timed_out": 0, "history_topped_up": 0, "fmp_quote_calls": 0,
                             "market_cap_cache_hits": 0, "split_calendar_reads": 0, "split_memo_hits": 0,
                             "split_timed_out": 0, "history_busy": 0, "history_syncs": 0}
    say("Reading open positions...")
    with _step(timing, "calendar"):
        warm_calendar(now)                     # the session tables are built here, never on the event loop
    with _step(timing, "db") as entry:
        txs, orders_of, expert_names, account_of = _read_rows(account_id)
        lots = lots_from_rows(txs, orders_of, expert_names, account_of)
        entry.note = f"{len(lots)} open transactions"
    equity = [l for l in lots if not l.is_option]
    accounts = _accounts_in_scope(account_id)
    say(f"Reading the broker positions ({len(accounts)} account{'s' if len(accounts) != 1 else ''})...")
    raw_by_account: Dict[int, set] = defaultdict(set)      # the spellings the platform stored, per account
    for tx in txs:
        if tx.asset_class != AssetClass.OPTION and tx.id in account_of and isinstance(tx.symbol, str):
            raw_by_account[account_of[tx.id]].add(tx.symbol.strip().upper())
    first_day = min(reference_session(tf, now) for tf in TIMEFRAMES)     # the earliest reference session
    read = _brokers(equity, accounts, raw_by_account, calls, timing)
    since = regular_session_close_utc(first_day) - timedelta(seconds=ORDER_LOOKBACK_S)
    finish_histories = _histories_in_background(read, first_day, since, calls) if read.share_accounts else None
    symbols_by_account = read.symbols_by_account
    # every symbol a broker holds, and every one the platform has open (a failed broker read keeps the platform's)
    symbols = sorted({l.symbol for l in equity} | {s for book in read.broker_qty.values() for s in book})

    finish_caps = _market_caps_in_background(symbols, calls) if symbols else None
    with _step(timing, "history") as entry:
        needed = sorted({reference_session(tf, now) for tf in TIMEFRAMES})
        closes, closes_missing = _closes(symbols_by_account, needed, now, calls, say)
        entry.note = (f"{len(symbols)} symbols: {calls['history_cache_hits']} cache hits, "
                      f"{calls['history_symbols']} read from disk, {calls['history_topped_up']} topped up, "
                      f"{calls['history_failed']} failed, {calls['history_timed_out']} timed out")
    # AFTER the history: the FMP history provider's daily refresh has just put most split calendars in the memo.
    say(f"Checking splits: 0 of {len(symbols)} symbols")
    with _step(timing, "splits") as entry:
        splits, splits_missing = fetch_split_days(symbols, calls=calls, progress=say) if symbols else ({}, {})
        entry.note = (f"{len(symbols)} symbols: {calls['split_memo_hits']} memo hits, "
                      f"{calls['split_calendar_reads']} read, "
                      f"{len(splits_missing) - calls['split_timed_out']} failed, "
                      f"{calls['split_timed_out']} timed out")
    if finish_caps is not None:
        say("Reading market caps...")
        (caps, caps_missing), seconds, note = finish_caps()
        _add(timing, "market caps (parallel)", seconds, note)
    else:
        caps, caps_missing = {}, {}
    with _step(timing, "labels"):
        labels = _labels(symbols, {s for spellings in raw_by_account.values() for s in spellings})
    if finish_histories is not None:
        say("Reading the broker's transaction history...")
        histories, orders, seconds, note = finish_histories()
        _add(timing, "share history (parallel)", seconds, note)
    else:
        histories, orders = {}, {}
    say("Drawing...")
    market = MarketInputs(prices=read.prices, price_missing=read.missing, closes=closes, closes_missing=closes_missing,
                          market_caps=caps, market_caps_missing=caps_missing, labels=labels, splits=splits,
                          splits_missing=splits_missing, broker_qty=read.broker_qty, broker_failed=read.broker_failed,
                          broker_option_rows=read.option_rows, broker_history=histories, platform_orders=orders)
    return HeatmapData(lots=lots, market=market, calls=calls)


def _labels(symbols: List[str], spellings: Iterable[str]) -> Dict[str, List[str]]:
    """Instrument labels by CANONICAL symbol: the Instrument rows are looked up in every spelling (a label saved under
    ``BRK.B`` belongs to ``BRK-B``)."""
    if not symbols:
        return {}
    found = get_labels_by_symbol(sorted(set(symbols) | set(spellings)))
    out: Dict[str, List[str]] = {}
    for spelled, names in found.items():
        c = _canonical(spelled)
        out[c] = sorted(set(out.get(c, [])) | set(names))
    return out


def _add(timing, name, seconds, note=None):
    from .load_timing import add
    add(timing, name, seconds, note)


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: Dict[Optional[int], Future] = {}


def load_heatmap_data(account_id: Optional[int], now: datetime, *, timing=None,
                      progress: Optional[Callable[[str], None]] = None) -> HeatmapData:
    """Everything one heatmap load needs, for the header's account (``None`` = all accounts), at ``now`` (tz-aware).
    Worker thread only. A load of a scope that is already loading (another browser tab) waits for that one.
    ``progress(text)`` is called from worker threads with what the load is doing ("Reading daily history: 40 of 125
    symbols"); it must only store the text."""
    with _INFLIGHT_LOCK:
        running = _INFLIGHT.get(account_id)
        owner = running is None
        if owner:
            running = _INFLIGHT[account_id] = Future()
    if not owner:
        if progress is not None:
            progress("Waiting for the load another browser tab started...")
        with _step(timing, "shared in-flight load"):
            return running.result()
    try:
        data = _load(account_id, now, timing, progress)
        running.set_result(data)
        return data
    except BaseException as e:
        running.set_exception(e)
        raise
    finally:
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(account_id) is running:
                del _INFLIGHT[account_id]
