"""``GET /accounts/{id}/growth``, ``.../growth/labels`` and ``.../growth/labels/{label}/positions``.

The broker data (balance history, dividends, fills, positions) is read through ``accounts.broker_read`` and kept
``FETCH_TTL_S`` seconds per (account, window) so a client polling the three endpoints costs one broker round trip;
the series are the SAME functions the Account Growth tab calls (``services.growth``). Historical closes come from the
same FMP-by-default source the tab uses (``services.price_history``, the account's history provider), from the window start,
within an overall deadline.

A GET: nothing here writes. In particular the tab's "queue unknown instruments for auto-add" side effect is NOT
reproduced: a symbol without an Instrument row is simply 'Unlabeled'. The datasets are read through the adapters' STRICT
variants (``accounts.strict_history``): a failed fetch is a 503/502, never an empty list, so ``datasets`` only counts rows. A broker row that lacks a field the maths needs (a missing balance, quantity, price, amount) is NEVER
read as 0: it is skipped, counted, and the response says ``partial: true`` with ``skipped_rows`` and the reasons.

LABEL / POSITION SERIES: the quantity timeline walks BACK from today's position quantity, so those series are always
computed through today (trades and dividends are not cut at ``to``) and only the OUTPUT dates are truncated at ``to``."""
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.utils import get_labels_by_symbol
from ..logger import logger
from . import accounts as accounts_svc
from . import growth, price_history
from ._common import jsonable, utcnow_iso
from ._positions_cache import read_positions
from .date_range import _as_date, date_in_range
from .errors import BrokerUnreachable, Invalid, NotFound, Unmeasurable

#: Longest window one request may ask for (the series is one point per day per account).
MAX_SPAN_DAYS = 365 * 5
DEFAULT_SPAN_DAYS = 365
#: Label charts carry four arrays per label: more than this must be asked for by name.
MAX_LABELS = 25
#: How long fetched broker rows are reused (per account and window).
FETCH_TTL_S = 60.0
#: Entries kept at most (one per account and window): the oldest go first.
MAX_CACHE_ENTRIES = 64

_CACHE: Dict[tuple, Tuple[float, list]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _cached(key: tuple, fetch: Callable[[], Optional[list]]):
    """Rows for ``key``, fetched at most once per ``FETCH_TTL_S``. Only a non-empty answer is kept (``[]`` is also what
    a failed adapter fetch returns). Always returns fresh dict copies: callers never touch the cached or adapter rows."""
    with _LOCK:
        hit = _CACHE[key] if key in _CACHE else None
        if hit is not None and time.monotonic() - hit[0] < FETCH_TTL_S:
            return [dict(r) for r in hit[1]]
    rows = fetch()
    if rows is None:
        return None
    rows = [dict(r) for r in rows]
    if rows:
        with _LOCK:
            now = time.monotonic()
            for stale in [k for k, (stamp, _r) in _CACHE.items() if now - stamp >= FETCH_TTL_S]:
                del _CACHE[stale]
            _CACHE[key] = (now, rows)
            while len(_CACHE) > MAX_CACHE_ENTRIES:               # oldest first (dicts keep insertion order)
                del _CACHE[next(iter(_CACHE))]
    return [dict(r) for r in rows]


def fetch_closes(account_id: int, symbols: List[str], start: date) -> Dict[str, Dict[str, float]]:
    """``{symbol: {YYYY-MM-DD: close}}`` for ``symbols`` from ``start`` on, from the account's own history source (FMP by
    default; see ``services.price_history``). A symbol the source has nothing for is absent; a dead source or the
    deadline is ``PriceSourceUnreachable``."""
    return price_history.fetch_closes({account_id: list(symbols)}, start)


def _window(from_: Optional[date], to: Optional[date]):
    to = to or date.today()
    from_ = from_ or (to - timedelta(days=DEFAULT_SPAN_DAYS))
    if from_ > to:
        raise Invalid("from is after to", {"from": from_.isoformat(), "to": to.isoformat()})
    if (to - from_).days > MAX_SPAN_DAYS:
        raise Invalid(f"The window is longer than {MAX_SPAN_DAYS} days", {"max_days": MAX_SPAN_DAYS})
    return from_, to


def _cut(rows, from_: date, to: date):
    """Rows dated within [from_, to]; a row with no parseable date was already dropped by validation."""
    return [r for r in rows if date_in_range(r.get('date'), from_) and not date_in_range(r.get('date'), to + timedelta(days=1))]


def _num(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _bad_balance(r) -> Optional[str]:
    if _as_date(r.get('date')) is None or r.get('date') is None:
        return "no usable date"
    if not _num(r.get('net_liquidating_value')):
        return "no net_liquidating_value"
    if not _num(r.get('cash_balance')) and r.get('profit_loss') is None:
        return "no cash_balance (and no profit_loss to derive flows from)"
    return None


def _bad_dividend(r) -> Optional[str]:
    if r.get('date') is None or _as_date(r.get('date')) is None:
        return "no usable date"
    if not r.get('symbol'):
        return "no symbol"
    if not _num(r.get('amount')):
        return "no amount"
    return None


def _bad_trade(r) -> Optional[str]:
    if r.get('date') is None or _as_date(r.get('date')) is None:
        return "no usable date"
    if not r.get('symbol'):
        return "no symbol"
    if r.get('side') not in ('BUY', 'SELL'):
        return "unknown side"
    if not _num(r.get('qty')) or not _num(r.get('price')):
        return "no qty or price"
    return None


def _validated(rows: list, check: Callable[[dict], Optional[str]], account_id: int):
    """``(good rows with account_id set on a COPY, {reason: count})``."""
    good, skipped = [], {}
    for r in rows:
        why = check(r)
        if why is not None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        good.append(dict(r, account_id=account_id))
    return good, skipped


def _fetch(account_id: int, account, from_: date, to: date, *, with_balance: bool = True):
    """The broker datasets, validated. Returns ``(balance, dividends, trades, report)``; ``report`` has the
    per-dataset row counts and the skipped rows."""
    def read(what: str, method: str, **kwargs):
        rows = accounts_svc.strict_history(account_id, account, method, what, **kwargs)
        if rows is None:
            raise BrokerUnreachable(f"Account {account_id}: {what} failed", {"account_id": account_id})
        return rows
    from . import portfolio      # the 60 s per-account dividends cache of the portfolio endpoint
    balance_raw = (_cached(("balance", account_id, from_, to), lambda: read(
        "the balance history", "get_balance_history", start_date=datetime.combine(from_, datetime.min.time()),
        end_date=datetime.combine(to, datetime.max.time()))) if with_balance else [])
    dividends_raw, _age, _hit = portfolio.cached_dividends(
        account_id, lambda: read("the dividends fetch", "get_dividends"))
    dividends_raw = [dict(r) for r in dividends_raw]
    trades_raw = _cached(("trades", account_id), lambda: read("the filled trades fetch", "get_filled_trades"))
    balance, sk_b = _validated(balance_raw, _bad_balance, account_id)
    dividends, sk_d = _validated(dividends_raw, _bad_dividend, account_id)
    trades, sk_t = _validated(trades_raw, _bad_trade, account_id)
    skipped = {k: v for k, v in (("balance_history", sk_b), ("dividends", sk_d), ("filled_trades", sk_t)) if v}
    # (strict reads: a failure already raised, so an empty dataset is a real "nothing", and the old fetch_ok flag is gone)
    report = {"datasets": {"balance_history": {"rows": len(balance_raw)}, "dividends": {"rows": len(dividends_raw)},
                           "filled_trades": {"rows": len(trades_raw)}},
              "partial": bool(skipped), "skipped_rows": sum(sum(v.values()) for v in skipped.values()),
              "skipped": skipped}
    return balance, dividends, trades, report


def account_growth(account_id: int, from_: Optional[date], to: Optional[date]) -> Dict[str, Any]:
    from_, to = _window(from_, to)
    account = accounts_svc._live_account(account_id)
    balance, dividends, trades, report = _fetch(account_id, account, from_, to)
    series = growth.total_growth(_cut(balance, from_, to), _cut(dividends, from_, to), _cut(trades, from_, to), from_)
    if series is None:
        raise Unmeasurable(f"Account {account_id}: no usable balance history or dividends in the window "
                           f"", {"account_id": account_id, **report})
    dates = series['all_dates']
    last = len(dates) - 1
    pnl_last = series['pnl_pct_data'][last]
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "from": from_.isoformat(), "to": to.isoformat(), **report,
        "latest": {"date": dates[last], "balance": series['aligned_balance'][last], "cash": series['aligned_cash'][last],
                   "cumulative_dividends": series['aligned_div'][last], "invested_capital": series['aligned_invested'][last],
                   "pnl_pct": pnl_last,
                   "pnl_status": "measured" if pnl_last is not None else "unmeasured"},
        "series": {"dates": dates, "balance": series['aligned_balance'], "cash": series['aligned_cash'],
                   "cumulative_dividends": series['aligned_div'], "invested_capital": series['aligned_invested'],
                   "pnl_pct": series['pnl_pct_data']},
    })


def _position(pos):
    """``(SimpleNamespace | None, reason | None)``. A position that lacks a field the series need cannot be charted:
    it is a figure the broker did not publish, not a zero."""
    from types import SimpleNamespace
    row = dict(pos) if isinstance(pos, dict) else None
    get = (lambda k: row[k] if k in row else None) if row is not None else (lambda k: getattr(pos, k, None))
    missing = [k for k in ("symbol", "qty", "cost_basis") if get(k) is None or (k != "symbol" and not _num(get(k)))]
    if missing:
        return None, f"no {', '.join(missing)}"
    return SimpleNamespace(symbol=get("symbol"), qty=float(get("qty")), cost_basis=float(get("cost_basis"))), None


def _position_inputs(account_id: int, from_: date, select: Callable[[Dict[str, dict]], Dict[str, dict]]):
    """Shared read for the two label endpoints. ``select`` narrows ``{symbol: info}`` BEFORE any price is fetched."""
    account = accounts_svc._live_account(account_id)
    positions = accounts_svc.broker_read(account_id, "the positions fetch", read_positions, account, account_id)
    if positions is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed", {"account_id": account_id})
    good, skipped_positions = [], []
    for p in positions:
        obj, why = _position(p)
        if obj is None:
            sym = (p.get('symbol') if isinstance(p, dict) else getattr(p, 'symbol', None))
            skipped_positions.append({"symbol": sym, "reason": why})
        else:
            good.append(obj)
    # trades and dividends are NOT cut at `to`: the quantity timeline walks back from today's quantity
    _, dividends, trades, report = _fetch(account_id, account, from_, date.today(), with_balance=False)
    labels = get_labels_by_symbol([p.symbol for p in good]) or {}
    info = growth.build_symbol_info(good, labels)
    if not info:
        raise Unmeasurable(f"Account {account_id} has no chartable positions",
                           {"account_id": account_id, "skipped_positions": skipped_positions})
    known = sorted({lb for i in info.values() for lb in i['labels']})
    info = select(info)
    closes = fetch_closes(account_id, sorted(info), from_)
    timed_out = bool(getattr(closes, "timed_out", False))        # the deadline passed with symbols unfetched
    report = dict(report, partial=report["partial"] or timed_out, price_timed_out=timed_out)
    unpriced = sorted(s for s in info if s not in closes)
    if len(unpriced) == len(info):
        raise Unmeasurable("No historical prices could be fetched for any held symbol", {"unpriced_symbols": unpriced})
    dates = growth.chart_dates(closes, from_)
    if not dates:
        raise Unmeasurable("No price history in the window", {"from": from_.isoformat()})
    return info, trades, dividends, closes, dates, unpriced, skipped_positions, known, report


def _upto(dates: List[str], to: date) -> int:
    """How many of the (ascending) ``dates`` are on or before ``to``."""
    return sum(1 for d in dates if d <= to.isoformat())


def _partial(report, skipped_positions) -> Dict[str, Any]:
    partial = report["partial"] or bool(skipped_positions)
    return {"partial": partial, "price_timed_out": report["price_timed_out"], "skipped_rows": report["skipped_rows"], "skipped": report["skipped"],
            "skipped_positions": skipped_positions}


def growth_by_label(account_id: int, labels: Optional[List[str]], from_: Optional[date], to: Optional[date]):
    from_, to = _window(from_, to)
    state = {}

    def select(info):
        known = sorted({lb for i in info.values() for lb in i['labels']})
        if labels:
            missing = [lb for lb in labels if lb not in known]
            if missing:
                raise NotFound(f"The account holds nothing labelled {missing}", {"labels": missing, "known": known})
            return {s: dict(i, labels=[lb for lb in i['labels'] if lb in labels]) for s, i in info.items()
                    if any(lb in labels for lb in i['labels'])}
        if len(known) > MAX_LABELS:
            raise Invalid(f"{len(known)} labels: ask for at most {MAX_LABELS} by name with ?labels=A,B", {"known": known})
        return info
    info, trades, dividends, closes, dates, unpriced, skipped_positions, _known, report = \
        _position_inputs(account_id, from_, select)
    lg = growth.label_growth(info, dates, closes, trades, dividends)
    n = _upto(lg['all_dates'], to)
    if n == 0:
        raise Unmeasurable("No price history in the window", {"from": from_.isoformat(), "to": to.isoformat()})
    cut = lambda xs: xs[:n]    # noqa: E731
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "from": from_.isoformat(), "to": to.isoformat(),
        "dates": cut(lg['all_dates']), "unpriced_symbols": unpriced, **_partial(report, skipped_positions),
        "labels": [{"name": lb, "value": cut(lg['label_daily_values'][lb]),
                    "cumulative_dividends": cut(lg['label_cum_divs'][lb]),
                    "cumulative_cash_dividends": cut(lg['label_cum_cash_divs'][lb]),
                    "invested": cut(lg['label_cum_invested'][lb])} for lb in lg['all_labels']],
    })


def growth_by_position(account_id: int, label: str, from_: Optional[date], to: Optional[date]):
    from_, to = _window(from_, to)

    def select(info):
        in_label = {s: i for s, i in info.items() if label in i['labels']}
        if not in_label:
            raise NotFound(f"The account holds nothing labelled {label!r}",
                           {"label": label, "known": sorted({lb for i in info.values() for lb in i['labels']})})
        return in_label                      # prices are fetched for THIS label's symbols only
    info, trades, dividends, closes, dates, unpriced, skipped_positions, _known, report = \
        _position_inputs(account_id, from_, select)
    pg = growth.position_growth(info, dates, closes, trades, dividends)
    n = _upto(pg['all_dates'], to)
    if n == 0:
        raise Unmeasurable("No price history in the window", {"from": from_.isoformat(), "to": to.isoformat()})
    cut = lambda xs: xs[:n]    # noqa: E731
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "label": label,
        "from": from_.isoformat(), "to": to.isoformat(), "dates": cut(pg['all_dates']),
        "unpriced_symbols": unpriced, **_partial(report, skipped_positions),
        "positions": [{"symbol": s, "value": cut(pg['sym_daily_values'][s]),
                       "cumulative_dividends": cut(pg['sym_cum_divs'][s]),
                       "cumulative_cash_dividends": cut(pg['sym_cum_cash_divs'][s]),
                       "invested": cut(pg['sym_cum_invested'][s])}
                      for s in sorted(info) if s in pg['sym_daily_values']],
    })
