"""IBKR Flex Web Service: the only IBKR source for dividends, cash transfers and NAV history.

The TWS API has no cash-transaction or account-history endpoint (design doc s7.1). IBKR serves those
through the Flex Web Service: an HTTPS report generated from an *Activity Flex Query* the operator
defines once in Client Portal (Reports > Flex Queries) and authorises with a token.

Protocol (v3):
  1. ``SendRequest?t=<token>&q=<queryId>&v=3``       -> ``<FlexStatementResponse>`` with a ReferenceCode + Url
  2. ``<Url>?t=<token>&q=<ReferenceCode>&v=3``      -> the statement XML, or ``Warn`` 1019 "in progress"

This module is PURE: XML parsing and the mapping onto the platform's records. The HTTP fetch is an
injected callable so tests never touch the network. **UNVERIFIED against a live statement**: attribute
names follow IBKR's published Flex schema (CashTransaction: ``type``, ``amount``, ``dateTime``,
``transactionID``, ``symbol``, ``actionID``, ``currency``, ``fxRateToBase``; EquitySummaryByReportDateInBase:
``reportDate``, ``cash``, ``total``). The operator's first Flex run (``tools/ibkr_paper_smoke.py --flex``)
prints what was parsed so a mismatch is visible at once.

The query must contain the **Cash Transactions** section (and, for balance history, **Equity Summary by
Report Date in Base**), with the date format left at IBKR's default or ISO.
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ba2_common.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL, CashTransfer)

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
FLEX_VERSION = "3"

#: Flex ``type`` values that are dividend income / its withholding.
DIVIDEND_TYPES = frozenset({"Dividends", "Payment In Lieu Of Dividends"})
WITHHOLDING_TYPES = frozenset({"Withholding Tax"})
TRANSFER_TYPES = frozenset({"Deposits/Withdrawals", "Deposits & Withdrawals"})
#: Error code meaning "the statement is still being generated; poll again".
IN_PROGRESS_CODE = "1019"


class FlexError(RuntimeError):
    """The Flex service refused or the statement could not be read."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass
class FlexStatement:
    account_id: str
    cash_transactions: List[Dict[str, str]] = field(default_factory=list)
    equity_summary: List[Dict[str, str]] = field(default_factory=list)


# ----------------------------------------------------------------------------- parsing
def parse_send_request(xml_text: str) -> Tuple[str, str]:
    """``(reference_code, statement_url)`` from a SendRequest response. Raises ``FlexError``."""
    root = _parse_xml(xml_text)
    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise FlexError(f"Flex SendRequest failed: {(root.findtext('ErrorMessage') or status).strip()}",
                        (root.findtext("ErrorCode") or "").strip() or None)
    reference = (root.findtext("ReferenceCode") or "").strip()
    url = (root.findtext("Url") or "").strip()
    if not reference or not url:
        raise FlexError("Flex SendRequest succeeded but carried no ReferenceCode/Url")
    return reference, url


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise FlexError(f"Flex returned something that is not XML: {e}: {text[:200]!r}") from e


def parse_statement(xml_text: str, account_id: Optional[str] = None) -> List[FlexStatement]:
    """All statements in a ``FlexQueryResponse`` (optionally only ``account_id``'s).

    Raises ``FlexError`` for a ``FlexStatementResponse`` (error/in-progress) instead of a statement.
    """
    root = _parse_xml(xml_text)
    if root.tag == "FlexStatementResponse":
        status = (root.findtext("Status") or "").strip()
        raise FlexError((root.findtext("ErrorMessage") or status or "Flex error").strip(),
                        (root.findtext("ErrorCode") or "").strip() or None)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"unexpected Flex root element {root.tag!r}")
    statements = []
    for node in root.iter("FlexStatement"):
        acct = node.attrib.get("accountId", "")
        if account_id and acct != account_id:
            continue
        statements.append(FlexStatement(
            account_id=acct,
            cash_transactions=[dict(n.attrib) for n in node.iter("CashTransaction")],
            equity_summary=[dict(n.attrib) for n in node.iter("EquitySummaryByReportDateInBase")]))
    return statements


_DATE_FORMATS = ("%Y%m%d", "%Y%m%d;%H%M%S", "%Y-%m-%d", "%Y-%m-%d;%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%m/%d/%Y", "%m/%d/%Y;%H:%M:%S", "%Y%m%d %H%M%S")


def parse_flex_date(text: Optional[str]) -> Optional[date]:
    """A Flex date/dateTime attribute in any of its configurable formats, else ``None``."""
    value = (text or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _amount(row: Dict[str, str]) -> Optional[float]:
    """Amount in the account base currency (USD): ``amount`` x ``fxRateToBase`` for other currencies."""
    try:
        amount = float(row.get("amount", ""))
    except ValueError:
        return None
    currency = (row.get("currency") or "").upper()
    if not currency:
        return None          # no currency stated: the amount cannot be interpreted, never assumed USD
    if currency != "USD":
        try:
            amount *= float(row["fxRateToBase"])
        except (KeyError, ValueError):
            return None
    return amount


def _row_date(row: Dict[str, str]) -> Optional[date]:
    return parse_flex_date(row.get("dateTime")) or parse_flex_date(row.get("reportDate")) \
        or parse_flex_date(row.get("settleDate"))


def _in_window(day: Optional[date], start: Optional[date], end: Optional[date]) -> bool:
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


# ----------------------------------------------------------------------------- mapping
def _external_id(row: Dict[str, str], day: Optional[date]) -> str:
    tid = (row.get("transactionID") or "").strip()
    if tid:
        return f"IBKR:{tid}"
    return f"IBKR:{row.get('type', '')}:{day}:{row.get('symbol', '')}:{row.get('amount', '')}"


def dividends_from(statements: List[FlexStatement], symbol: Optional[str] = None,
                   start: Any = None, end: Any = None) -> List[Dict[str, Any]]:
    """One record per ``(symbol, date)`` dividend: NET of withholding, floored at zero.

    Same record shape every adapter returns (``amount`` NET, ``gross_amount``, ``tax_withheld``,
    ``date``, ``drip_*`` None: IBKR reports reinvestment as ordinary trades).
    """
    start, end = _as_date(start), _as_date(end)
    gross: Dict[Tuple[str, date], float] = {}
    tax: Dict[Tuple[str, date], float] = {}
    for st in statements:
        for row in st.cash_transactions:
            kind = row.get("type", "")
            day = _row_date(row)
            sym = (row.get("symbol") or "").strip().upper()
            if kind not in DIVIDEND_TYPES | WITHHOLDING_TYPES or not sym or not _in_window(day, start, end):
                continue
            if symbol and sym != symbol.strip().upper():
                continue
            amount = _amount(row)
            if amount is None:
                continue
            key = (sym, day)
            if kind in DIVIDEND_TYPES and amount > 0:
                gross[key] = gross.get(key, 0.0) + amount
            elif kind in WITHHOLDING_TYPES or amount < 0:
                tax[key] = tax.get(key, 0.0) + abs(amount)
    out = []
    for (sym, day) in sorted(gross, key=lambda k: (str(k[1]), k[0])):
        g, t = round(gross[(sym, day)], 2), round(tax.get((sym, day), 0.0), 2)
        out.append({"symbol": sym, "amount": max(round(g - t, 2), 0.0), "gross_amount": g,
                    "tax_withheld": t, "date": day, "drip_quantity": None, "drip_price": None})
    return out


def cash_transfers_from(statements: List[FlexStatement], start: Any = None,
                        end: Any = None) -> List[CashTransfer]:
    """Deposits, withdrawals and (net) dividends as ``CashTransfer`` rows keyed by Flex transaction id."""
    start, end = _as_date(start), _as_date(end)
    transfers: List[CashTransfer] = []
    withholding: Dict[Tuple[str, date], float] = {}
    for st in statements:
        for row in st.cash_transactions:
            if row.get("type") in WITHHOLDING_TYPES:
                day, amount = _row_date(row), _amount(row)
                if day is not None and amount is not None:
                    key = ((row.get("symbol") or "").strip().upper(), day)
                    withholding[key] = withholding.get(key, 0.0) + abs(amount)
    for st in statements:
        for row in st.cash_transactions:
            kind, day, amount = row.get("type", ""), _row_date(row), _amount(row)
            if amount is None or not _in_window(day, start, end):
                continue
            if kind in TRANSFER_TYPES:
                transfers.append(CashTransfer(
                    external_id=_external_id(row, day), event_date=day,
                    event_type=CASH_TRANSFER_DEPOSIT if amount > 0 else CASH_TRANSFER_WITHDRAWAL,
                    amount=amount, description=row.get("description")))
            elif kind in DIVIDEND_TYPES and amount > 0:
                sym = (row.get("symbol") or "").strip().upper()
                net = max(round(amount - withholding.pop((sym, day), 0.0), 2), 0.0)
                transfers.append(CashTransfer(
                    external_id=_external_id(row, day), event_date=day,
                    event_type=CASH_TRANSFER_DIVIDEND, amount=net, symbol=sym or None,
                    description=row.get("description")))
    return transfers


def balance_history_from(statements: List[FlexStatement], start: Any = None,
                         end: Any = None) -> List[Dict[str, Any]]:
    """NAV snapshots from ``EquitySummaryByReportDateInBase``: net liquidating value, cash, equity."""
    start, end = _as_date(start), _as_date(end)
    out = []
    for st in statements:
        for row in st.equity_summary:
            day = parse_flex_date(row.get("reportDate"))
            if not _in_window(day, start, end):
                continue
            try:
                total, cash = float(row["total"]), float(row["cash"])
            except (KeyError, ValueError):
                continue
            out.append({"date": day, "net_liquidating_value": total, "cash_balance": cash,
                        "equity_value": total - cash})
    return sorted(out, key=lambda r: r["date"])


# ----------------------------------------------------------------------------- fetching
def default_http_get(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "BA2TradePlatform/ibkr-flex"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed IBKR host
        return response.read().decode("utf-8")


class FlexClient:
    """Fetch one Flex statement. ``http_get(url) -> text`` is injected (tests, proxies)."""

    def __init__(self, token: str, query_id: str,
                 http_get: Callable[[str], str] = default_http_get,
                 sleep: Callable[[float], None] = time.sleep, poll_attempts: int = 10,
                 poll_delay: float = 3.0):
        if not token or not query_id:
            raise ValueError("Flex needs both a token and a query id")
        self.token, self.query_id = token, query_id
        self._get, self._sleep = http_get, sleep
        self.poll_attempts, self.poll_delay = poll_attempts, poll_delay

    def _url(self, base: str, query: str) -> str:
        return f"{base}?{urllib.parse.urlencode({'t': self.token, 'q': query, 'v': FLEX_VERSION})}"

    def fetch(self, account_id: Optional[str] = None) -> List[FlexStatement]:
        reference, url = parse_send_request(self._get(self._url(SEND_REQUEST_URL, self.query_id)))
        last: Optional[FlexError] = None
        for attempt in range(self.poll_attempts):
            try:
                return parse_statement(self._get(self._url(url, reference)), account_id)
            except FlexError as e:
                if e.code != IN_PROGRESS_CODE:
                    raise
                last = e
                self._sleep(self.poll_delay)
        raise FlexError(f"Flex statement still not ready after {self.poll_attempts} polls", last.code
                        if last else None)
