"""``GET /accounts/{id}/dividends``: the broker's dividend feed (``get_dividends``) plus the income ledger
(``PortfolioIncomeEvent`` rows and the open income total).

The two answer different questions and are returned side by side, never merged:

* ``broker``  -- every dividend the broker booked in the window (NET of tax; ``drip_quantity`` set = reinvested, so
  the cash never left the position). ``totals.cash_net`` is the cash-only sum the allocation page uses
  (``cash_dividends_by_symbol``); ``totals.drip_net`` is what was reinvested.
* ``ledger``  -- the cash waiting to be deployed by allocation runs (rolling sync window, consumed oldest-first).

The adapters answer ``[]`` when their fetch fails, so an empty broker answer carries ``fetch_ok: false`` and a note:
it is not "no dividends"."""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ba2_common.core.portfolio_allocation import cash_dividends_by_symbol

from ..core.portfolio_allocation_service import get_open_income_total, get_recent_income_events
from .date_range import date_in_range
from . import accounts as accounts_svc
from . import portfolio
from ._common import jsonable, utcnow_iso
from .errors import BrokerUnreachable, Invalid

MAX_SPAN_DAYS = 365 * 5
#: Rows at which a broker answer is suspected to be a truncated first page (Alpaca: 100 per page).
BROKER_PAGE_SIZE = 100
DEFAULT_SPAN_DAYS = 365


def _money(row: Dict[str, Any], key: str) -> Optional[float]:
    return float(row[key]) if key in row and row[key] is not None else None


def account_dividends(account_id: int, from_: Optional[date], to: Optional[date], symbol: Optional[str],
                      *, limit: int, offset: int) -> Dict[str, Any]:
    to = to or date.today()
    from_ = from_ or (to - timedelta(days=DEFAULT_SPAN_DAYS))
    if from_ > to:
        raise Invalid("from is after to", {"from": from_.isoformat(), "to": to.isoformat()})
    if (to - from_).days > MAX_SPAN_DAYS:
        raise Invalid(f"The window is longer than {MAX_SPAN_DAYS} days", {"max_days": MAX_SPAN_DAYS})
    wanted = symbol.strip().upper() if symbol else None
    account = accounts_svc._live_account(account_id)
    # The FULL history through the 60 s per-account cache the portfolio endpoint also uses (one REST fetch serves both);
    # the window and the symbol are applied here.
    def fetch():
        rows_ = accounts_svc.broker_read(account_id, "the dividends fetch", account.get_dividends)
        if rows_ is None:
            raise BrokerUnreachable(f"Account {account_id}: the dividends fetch failed", {"account_id": account_id})
        return rows_
    raw, cache_age_s, from_cache = portfolio.cached_dividends(account_id, fetch)
    raw = [dict(r) for r in raw]            # never touch the cached / adapter rows
    in_window = [r for r in raw
                 if date_in_range(r.get('date'), from_) and not date_in_range(r.get('date'), to + timedelta(days=1))
                 and (wanted is None or str(r.get('symbol') or '').strip().upper() == wanted)]
    rows, skipped = [], 0
    for r in in_window:                     # a payment with no usable amount cannot be summed: skip + count, never 0
        if r.get('amount') is None or isinstance(r.get('amount'), bool):
            skipped += 1
            continue
        try:
            float(r['amount'])
        except (TypeError, ValueError):
            skipped += 1
            continue
        rows.append(r)
    rows.sort(key=lambda r: str(r.get('date')), reverse=True)

    net_all = [float(r['amount']) for r in rows]
    cash = [r for r in rows if not r.get('drip_quantity')]
    gross_rows = [r for r in rows if _money(r, 'gross_amount') is not None]
    tax_rows = [r for r in rows if _money(r, 'tax_withheld') is not None]
    totals = {
        "net": sum(net_all),
        "cash_net": sum(float(r['amount']) for r in cash),
        "drip_net": sum(float(r['amount']) for r in rows if r.get('drip_quantity')),
        "gross": sum(_money(r, 'gross_amount') for r in gross_rows),
        "tax_withheld": sum(_money(r, 'tax_withheld') for r in tax_rows),
        # gross / tax are totals over the rows that carry them; this says how many did not
        "rows_without_gross": len(rows) - len(gross_rows), "rows_without_tax": len(rows) - len(tax_rows),
        "payments": len(rows),
    }
    items = [{"date": r.get('date'), "symbol": r.get('symbol'), "amount": float(r['amount']),
              "gross_amount": _money(r, 'gross_amount'), "tax_withheld": _money(r, 'tax_withheld'),
              "kind": "drip" if r.get('drip_quantity') else "cash",
              "drip_quantity": r.get('drip_quantity'), "drip_price": r.get('drip_price')}
             for r in rows[offset:offset + limit]]
    cash_by = cash_dividends_by_symbol(rows)
    broker: Dict[str, Any] = {"fetch_ok": bool(raw), "items": items, "total": len(rows), "limit": limit,
                              "offset": offset, "totals": totals,
                              "cash_by_symbol": [{"symbol": k, "cash": v} for k, v in cash_by.items()],
                              "partial": skipped > 0, "skipped_rows": skipped,
                              "cache_age_s": cache_age_s, "from_cache": from_cache,
                              # Alpaca's activities endpoint returns at most 100 rows per page and the adapter does
                              # not page: a full page may be the first of several.
                              "truncation_suspected": len(raw) >= BROKER_PAGE_SIZE}
    if broker["truncation_suspected"]:
        broker["truncation_note"] = (f"The broker returned {len(raw)} rows (>= {BROKER_PAGE_SIZE}, a typical page size): "
                                     "older payments may be missing.")
    if not raw:
        broker["note"] = ("The broker adapter answers [] when its fetch fails, so an empty answer cannot be told "
                          "from 'no dividends'.")

    days = max(1, (date.today() - from_).days)
    events = [e for e in get_recent_income_events(account_id, days=days)
              if e["event_date"] <= to and (wanted is None or str(e["symbol"] or "").upper() == wanted)]
    return jsonable({
        "as_of": utcnow_iso(), "account_id": account_id, "from": from_.isoformat(), "to": to.isoformat(),
        "symbol": wanted, "broker": broker,
        "ledger": {"events": events, "open_income_total": get_open_income_total(account_id)},
    })
