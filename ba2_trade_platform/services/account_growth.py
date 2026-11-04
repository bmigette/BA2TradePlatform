"""``GET /accounts/{id}/growth``, ``.../growth/labels`` and ``.../growth/labels/{label}/positions``.

The broker data (balance history, dividends, fills, positions) is read ONCE per request through
``accounts.broker_read``; the series are the SAME functions the Account Growth tab calls (``services.growth``).
Historical closes come from the same yfinance download the tab uses (``fetch_closes``).

A GET: nothing here writes. In particular the tab's "queue unknown instruments for auto-add" side effect is NOT
reproduced: a symbol without an Instrument row is simply 'Unlabeled'. The adapters turn a failed dividends / fills /
balance fetch into ``[]``, so an empty dataset is reported (``datasets``) and, when it leaves nothing to draw,
raises ``Unmeasurable`` -- an empty answer is never presented as a flat account."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ..core.utils import get_labels_by_symbol
from ..logger import logger
from . import accounts as accounts_svc
from . import growth
from ._common import jsonable, utcnow_iso
from .date_range import date_in_range
from .errors import BrokerUnreachable, Invalid, NotFound, Unmeasurable

#: Longest window one request may ask for (the series is one point per day per account).
MAX_SPAN_DAYS = 365 * 5
DEFAULT_SPAN_DAYS = 365
#: Label charts carry four arrays per label: more than this must be asked for by name.
MAX_LABELS = 25


def fetch_closes(symbols: List[str], start: date) -> Dict[str, Dict[str, float]]:
    """``{symbol: {YYYY-MM-DD: close}}`` for ``symbols`` from ``start`` on (yfinance, as the Account Growth tab does).
    A symbol Yahoo returns nothing for is absent."""
    if not symbols:
        return {}
    import yfinance as yf
    days = (date.today() - start).days
    period = '6mo' if days <= 180 else '1y' if days <= 365 else '3y' if days <= 3 * 365 else 'max'
    hist = yf.download(symbols, period=period, progress=False, auto_adjust=False)
    out = {}
    for sym in symbols:
        closes = growth.extract_yf_close_prices(hist, sym)
        if closes:
            out[sym] = closes
    return out


def _window(from_: Optional[date], to: Optional[date]):
    to = to or date.today()
    from_ = from_ or (to - timedelta(days=DEFAULT_SPAN_DAYS))
    if from_ > to:
        raise Invalid("from is after to", {"from": from_.isoformat(), "to": to.isoformat()})
    if (to - from_).days > MAX_SPAN_DAYS:
        raise Invalid(f"The window is longer than {MAX_SPAN_DAYS} days", {"max_days": MAX_SPAN_DAYS})
    return from_, to


def _cut(rows, from_: date, to: date):
    """Rows dated within [from_, to]; a row with no parseable date is dropped (it cannot be placed)."""
    return [r for r in rows if date_in_range(r.get('date'), from_) and not date_in_range(r.get('date'), to + timedelta(days=1))]


def _dataset(rows) -> Dict[str, Any]:
    n = len(rows)
    return {"rows": n, "fetch_ok": bool(n)}   # the adapters answer [] on failure: empty cannot be told from "none"


def _fetch(account_id: int, account, from_: date, to: date, *, with_balance: bool = True):
    start, end = datetime.combine(from_, datetime.min.time()), datetime.combine(to, datetime.max.time())
    balance = (accounts_svc.broker_read(account_id, "the balance history", account.get_balance_history,
                                        start_date=start, end_date=end) if with_balance else [])
    dividends = accounts_svc.broker_read(account_id, "the dividends fetch", account.get_dividends)
    trades = accounts_svc.broker_read(account_id, "the filled trades fetch", account.get_filled_trades)
    for rows in (balance, dividends, trades):
        if rows is None:
            raise BrokerUnreachable(f"Account {account_id}: a growth dataset could not be fetched", {"account_id": account_id})
    for entry in balance:
        entry['account_id'] = account_id
    for entry in dividends:
        entry['account_id'] = account_id
    for entry in trades:
        entry['account_id'] = account_id
    return list(balance), list(dividends), list(trades)


def account_growth(account_id: int, from_: Optional[date], to: Optional[date]) -> Dict[str, Any]:
    from_, to = _window(from_, to)
    account = accounts_svc._live_account(account_id)
    balance, dividends, trades = _fetch(account_id, account, from_, to)
    datasets = {"balance_history": _dataset(balance), "dividends": _dataset(dividends), "filled_trades": _dataset(trades)}
    series = growth.total_growth(_cut(balance, from_, to), _cut(dividends, from_, to), _cut(trades, from_, to), from_)
    if series is None:
        raise Unmeasurable(f"Account {account_id}: no balance history or dividends in the window "
                           f"(a failed fetch also reads as empty)", {"account_id": account_id, "datasets": datasets})
    dates = series['all_dates']
    last = len(dates) - 1
    pnl_last = series['pnl_pct_data'][last]
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "from": from_.isoformat(), "to": to.isoformat(),
        "datasets": datasets,
        "latest": {"date": dates[last], "balance": series['aligned_balance'][last], "cash": series['aligned_cash'][last],
                   "cumulative_dividends": series['aligned_div'][last], "invested_capital": series['aligned_invested'][last],
                   "pnl_pct": pnl_last,
                   "pnl_status": "measured" if pnl_last is not None else "unmeasured"},
        "series": {"dates": dates, "balance": series['aligned_balance'], "cash": series['aligned_cash'],
                   "cumulative_dividends": series['aligned_div'], "invested_capital": series['aligned_invested'],
                   "pnl_pct": series['pnl_pct_data']},
    })


def _position(account_id: int, pos):
    """A broker position with the attributes the series need. A position that lacks one cannot be charted: that is a
    figure the broker did not publish, not a zero."""
    row = dict(pos) if isinstance(pos, dict) else None
    get = (lambda k: row[k] if k in row else None) if row is not None else (lambda k: getattr(pos, k, None))
    missing = [k for k in ("symbol", "qty", "cost_basis") if get(k) is None]
    if missing:
        raise Unmeasurable(f"Account {account_id}: a position carries no {missing}", {"account_id": account_id})
    return SimpleNamespace(symbol=get("symbol"), qty=float(get("qty")), cost_basis=float(get("cost_basis")))


def _position_inputs(account_id: int, from_: date, to: date, only_labels: Optional[List[str]] = None):
    """Shared read for the two label endpoints: positions, symbol_info, trades, dividends, closes, dates."""
    account = accounts_svc._live_account(account_id)
    positions = accounts_svc.broker_read(account_id, "the positions fetch", account.get_positions)
    if positions is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed", {"account_id": account_id})
    positions = [_position(account_id, p) for p in positions]
    _, dividends, trades = _fetch(account_id, account, from_, to, with_balance=False)
    dividends, trades = _cut(dividends, from_, to), _cut(trades, from_, to)   # (the balance history is not needed)
    labels = get_labels_by_symbol([p.symbol for p in positions]) or {}
    info = growth.build_symbol_info(positions, labels)
    if not info:
        raise Unmeasurable(f"Account {account_id} holds no positions to chart", {"account_id": account_id})
    closes = accounts_svc.broker_read(account_id, "the historical prices", fetch_closes, sorted(info), from_)
    unpriced = sorted(s for s in info if s not in closes)
    if len(unpriced) == len(info):
        raise Unmeasurable("No historical prices could be fetched for any held symbol", {"unpriced_symbols": unpriced})
    dates = [d for d in growth.chart_dates(closes, from_) if not date_in_range(d, to + timedelta(days=1))]
    if not dates:
        raise Unmeasurable("No price history in the window", {"from": from_.isoformat(), "to": to.isoformat()})
    return info, trades, dividends, closes, dates, unpriced


def growth_by_label(account_id: int, labels: Optional[List[str]], from_: Optional[date], to: Optional[date]):
    from_, to = _window(from_, to)
    info, trades, dividends, closes, dates, unpriced = _position_inputs(account_id, from_, to)
    known = sorted({lb for i in info.values() for lb in i['labels']})
    if labels:
        missing = [lb for lb in labels if lb not in known]
        if missing:
            raise NotFound(f"The account holds nothing labelled {missing}", {"labels": missing, "known": known})
        info = {s: dict(i, labels=[lb for lb in i['labels'] if lb in labels]) for s, i in info.items()
                if any(lb in labels for lb in i['labels'])}
    elif len(known) > MAX_LABELS:
        raise Invalid(f"{len(known)} labels: ask for at most {MAX_LABELS} by name with ?labels=A,B", {"known": known})
    lg = growth.label_growth(info, dates, closes, trades, dividends)
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "from": from_.isoformat(), "to": to.isoformat(),
        "dates": lg['all_dates'], "unpriced_symbols": unpriced,
        "labels": {lb: {"value": lg['label_daily_values'][lb], "cumulative_dividends": lg['label_cum_divs'][lb],
                        "cumulative_cash_dividends": lg['label_cum_cash_divs'][lb],
                        "invested": lg['label_cum_invested'][lb]} for lb in lg['all_labels']},
    })


def growth_by_position(account_id: int, label: str, from_: Optional[date], to: Optional[date]):
    from_, to = _window(from_, to)
    info, trades, dividends, closes, dates, unpriced = _position_inputs(account_id, from_, to)
    in_label = {s: i for s, i in info.items() if label in i['labels']}
    if not in_label:
        raise NotFound(f"The account holds nothing labelled {label!r}",
                       {"label": label, "known": sorted({lb for i in info.values() for lb in i['labels']})})
    pg = growth.position_growth(in_label, dates, closes, trades, dividends)
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "label": label,
        "from": from_.isoformat(), "to": to.isoformat(), "dates": pg['all_dates'],
        "unpriced_symbols": [s for s in unpriced if s in in_label],
        "positions": {s: {"value": pg['sym_daily_values'][s], "cumulative_dividends": pg['sym_cum_divs'][s],
                          "cumulative_cash_dividends": pg['sym_cum_cash_divs'][s],
                          "invested": pg['sym_cum_invested'][s]}
                      for s in sorted(in_label) if s in pg['sym_daily_values']},
    })
