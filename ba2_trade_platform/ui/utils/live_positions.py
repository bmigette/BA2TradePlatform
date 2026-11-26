"""The Live Trades "Live View" tab, decided in pure code: the broker's positions as table rows, the Total row, the
P/L Open bar, the weights, the column sets (phone and desktop), sorting, filtering, display strings, the remembered
choices, and what is shown for an account whose positions read failed.

No NiceGUI, no DB, no broker: ``services.live_positions`` reads, ``ui.components.LivePositionsTab`` draws.

NUMBERS ARE THE BROKER'S. Every figure is a ``Position`` field as the account adapter reported it (signed: a short
has a negative qty, cost basis and market value). Percentages arrive as FRACTIONS (0.063 = 6.3%). A field that is
missing or not a finite number is ``None``: shown as "n/a", left out of a total (which then says how many it left
out, "30.11 (2 n/a)"), and never read as 0.

A FAILED READ NEVER LOOKS LIKE "NO POSITIONS". An account whose read failed or timed out keeps its last good rows,
marked stale, and a warning names it; on the first load there is nothing to keep, so its rows are absent and the
warning says so. A flat account (the broker answered ``[]``) is not a failure."""
import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .responsive import PIN_TAG_PREFIX, PinnedColumn, phone_media, scroll_table_phone_css, tag_quasar_columns

#: The app's P/L colours (the Account Overview and Live Trades tables use the same two), and the grey of 0.00 / n/a.
GREEN = "#4ade80"
RED = "#f87171"
GREY = "#9ca3af"
NA = "n/a"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed out"
STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_TIMED_OUT)

LIVE_VIEW_TAB = "Live View"
LIVE_VIEW_ICON = "monitor_heart"

#: Root class of the q-table (the phone sheet is scoped to it) and the class of controls a phone does not get.
TABLE_CLASS = "lv-table"
DESKTOP_ONLY_CLASS = "lv-desktop-only"

#: Refresh interval choices: key -> label. Keys are what is stored.
INTERVALS: Dict[str, str] = {"5": "5 s", "10": "10 s", "30": "30 s", "off": "Off"}
DEFAULT_INTERVAL = "10"
DEFAULT_SORT_BY = "pl_day"
DEFAULT_DESCENDING = True

STORAGE_KEY = "live_trades_page_state"


# ----------------------------------------------------------------------------------------------------------- numbers

def _num(value) -> Optional[float]:
    """A finite float, or None. ``bool`` and strings are not numbers here (a "12" is a malformed payload)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def fmt_money(value: Optional[float]) -> str:
    return NA if value is None else f"{value:,.2f}"


def fmt_pct(fraction: Optional[float], decimals: int) -> str:
    return NA if fraction is None else f"{fraction * 100:.{decimals}f}%"


def fmt_qty(value: Optional[float]) -> str:
    if value is None:
        return NA
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def fmt_price(value: Optional[float]) -> str:
    if value is None:
        return NA
    return f"{value:,.4f}" if abs(value) < 1 else f"{value:,.2f}"


def sign_colour(value: Optional[float], decimals: int) -> str:
    """Green above zero, red below, grey for what SHOWS as zero at ``decimals`` and for n/a."""
    if value is None or round(value, decimals) == 0:
        return GREY
    return GREEN if value > 0 else RED


# ----------------------------------------------------------------------------------------------------------- columns

@dataclass(frozen=True)
class Column:
    name: str
    label: str
    #: text | money | price | qty | pct1 | pct2 | labels
    kind: str
    coloured: bool = False


COLUMNS: Tuple[Column, ...] = (
    Column("symbol", "Symbol", "text"),
    Column("account", "Account", "text"),
    Column("qty", "Qty", "qty"),
    Column("price", "Price", "price"),
    Column("day_chg_pct", "Day chg %", "pct2", coloured=True),
    Column("pl_day", "P/L Day", "money", coloured=True),
    Column("pl_day_pct", "P/L Day %", "pct2", coloured=True),
    Column("pl_open_pct", "P/L Opn%", "pct1", coloured=True),
    Column("pl_open", "P/L Open", "money", coloured=True),
    Column("avg_cost", "Avg cost", "price"),
    Column("cost_basis", "Cost basis", "money"),
    Column("mkt_value", "Mkt value", "money"),
    Column("weight_pct", "Weight %", "pct1"),
    Column("asset_class", "Asset class", "text"),
    Column("labels", "Labels", "labels"),
)
COLUMN_BY_NAME: Dict[str, Column] = {c.name: c for c in COLUMNS}

#: Desktop: identity, position, the day, the open P/L, then the book-keeping columns.
DESKTOP_ORDER: Tuple[str, ...] = tuple(c.name for c in COLUMNS)
#: Phone: what the tastytrade Positions screen shows without scrolling, then everything else (sideways scroll).
PHONE_VISIBLE: Tuple[str, ...] = ("symbol", "pl_day", "pl_open_pct", "pl_open")
PHONE_ORDER: Tuple[str, ...] = PHONE_VISIBLE + tuple(n for n in DESKTOP_ORDER if n not in PHONE_VISIBLE)

#: The money / percent columns the Total row sums.
TOTALLED = ("pl_day", "pl_open", "cost_basis", "mkt_value", "pl_open_pct")

#: Phone: the symbol is pinned; every other column is at least this wide (the text fits on one line).
LIVE_PINNED: Tuple[PinnedColumn, ...] = (PinnedColumn("symbol", 0, 84),)
LIVE_MIN_WIDTHS: Dict[str, int] = {
    "pl_day": 92, "pl_open_pct": 76, "pl_open": 104, "account": 110, "qty": 64, "price": 84, "day_chg_pct": 84,
    "pl_day_pct": 84, "avg_cost": 84, "cost_basis": 104, "mkt_value": 104, "weight_pct": 76, "asset_class": 96,
    "labels": 140,
}


def column_names(phone: bool, show_account: bool) -> List[str]:
    order = PHONE_ORDER if phone else DESKTOP_ORDER
    return [n for n in order if show_account or n != "account"]


# ----------------------------------------------------------------------------------------------------------- rows

@dataclass(frozen=True)
class PositionRow:
    key: str
    account_id: int
    account: str
    symbol: str
    asset_class: str
    qty: Optional[float]
    price: Optional[float]
    day_chg_pct: Optional[float]
    pl_day: Optional[float]
    pl_day_pct: Optional[float]
    pl_open: Optional[float]
    pl_open_pct: Optional[float]
    avg_cost: Optional[float]
    cost_basis: Optional[float]
    mkt_value: Optional[float]
    labels: Tuple[str, ...] = ()
    stale: bool = False


def _mapping(raw) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    return dict(raw)          # a Position (SQLModel) iterates as (field, value) pairs


def position_row(account_id: int, account_name: str, raw, labels: Iterable[str]) -> Optional[PositionRow]:
    """One broker position (a ``Position`` or its dict) as a row; ``None`` when it carries no symbol."""
    row = _mapping(raw)
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    symbol = symbol.strip()
    asset_class = row.get("asset_class")
    asset_class = getattr(asset_class, "value", asset_class)
    return PositionRow(
        key=f"{account_id}:{symbol}", account_id=account_id, account=str(account_name), symbol=symbol,
        asset_class=str(asset_class) if asset_class is not None else "",
        qty=_num(row.get("qty")), price=_num(row.get("current_price")), day_chg_pct=_num(row.get("change_today")),
        pl_day=_num(row.get("unrealized_intraday_pl")), pl_day_pct=_num(row.get("unrealized_intraday_plpc")),
        pl_open=_num(row.get("unrealized_pl")), pl_open_pct=_num(row.get("unrealized_plpc")),
        avg_cost=_num(row.get("avg_entry_price")), cost_basis=_num(row.get("cost_basis")),
        mkt_value=_num(row.get("market_value")), labels=tuple(labels))


def mark_stale(row: PositionRow) -> PositionRow:
    return replace(row, stale=True)


def search_text(row: PositionRow) -> str:
    """What the text filter matches: symbol, account, asset class and labels, lower case."""
    return " ".join([row.symbol, row.account, row.asset_class, *row.labels]).lower()


def filter_rows(rows: Sequence[PositionRow], term: Optional[str]) -> List[PositionRow]:
    """The rows the table shows for filter ``term`` -- the same rule as ``FILTER_METHOD_JS`` (the browser's)."""
    t = (term or "").strip().lower()
    return [r for r in rows if not t or t in search_text(r)]


#: The q-table's ``filter-method``: the browser filters on the row's ``search`` text, exactly like ``filter_rows``,
#: so the Total row (computed here for the filtered rows) sums the rows on screen.
FILTER_METHOD_JS = ("(rows, terms) => { const t = String(terms || '').trim().toLowerCase(); "
                    "return t ? rows.filter(r => (r.search || '').indexOf(t) !== -1) : rows; }")


# ----------------------------------------------------------------------------------------------------------- totals

@dataclass(frozen=True)
class Total:
    value: Optional[float]
    #: rows left out because their value is n/a
    missing: int = 0


@dataclass(frozen=True)
class Totals:
    pl_day: Total
    pl_open: Total
    cost_basis: Total
    mkt_value: Total
    pl_open_pct: Total
    rows: int
    stale_rows: int
    unread_accounts: int = 0


def _sum(values: Sequence[Optional[float]]) -> Total:
    known = [v for v in values if v is not None]
    missing = len(values) - len(known)
    if values and not known:
        return Total(None, missing)
    return Total(sum(known), missing)


def compute_totals(rows: Sequence[PositionRow], unread_accounts: int = 0) -> Totals:
    """Sums of P/L Day, P/L Open, cost basis and market value; P/L Opn% = total P/L Open / total |cost basis| over
    the rows that have both."""
    both = [(r.pl_open, r.cost_basis) for r in rows if r.pl_open is not None and r.cost_basis is not None]
    denominator = sum(abs(cb) for _pl, cb in both)
    pct = Total(sum(pl for pl, _cb in both) / denominator if denominator else None, len(rows) - len(both))
    return Totals(pl_day=_sum([r.pl_day for r in rows]), pl_open=_sum([r.pl_open for r in rows]),
                  cost_basis=_sum([r.cost_basis for r in rows]), mkt_value=_sum([r.mkt_value for r in rows]),
                  pl_open_pct=pct, rows=len(rows), stale_rows=sum(1 for r in rows if r.stale),
                  unread_accounts=unread_accounts)


def _text(value: Optional[float], kind: str) -> str:
    if kind == "money":
        return fmt_money(value)
    if kind == "price":
        return fmt_price(value)
    if kind == "qty":
        return fmt_qty(value)
    if kind == "pct1":
        return fmt_pct(value, 1)
    if kind == "pct2":
        return fmt_pct(value, 2)
    raise ValueError(f"not a numeric column kind: {kind!r}")


def _colour(value: Optional[float], kind: str) -> str:
    if kind == "pct1":
        return sign_colour(None if value is None else value * 100, 1)
    if kind == "pct2":
        return sign_colour(None if value is None else value * 100, 2)
    return sign_colour(value, 2)


def total_text(total: Total, kind: str) -> str:
    text = _text(total.value, kind)
    if total.value is not None and total.missing:
        text += f" ({total.missing} {NA})"
    return text


def _total_label(totals: Totals) -> str:
    return "Total (partial)" if totals.unread_accounts else "Total"


def quasar_columns(phone: bool, show_account: bool, totals: Totals) -> List[dict]:
    """The q-table's columns, tagged for the phone sheet. Each carries the Total row's ``total_text`` and
    ``total_color`` (the ``top-row`` slot reads them), so the Total stays under the header whatever the sort."""
    out = []
    for name in column_names(phone, show_account):
        col = COLUMN_BY_NAME[name]
        numeric = col.kind not in ("text", "labels")
        quasar = {"name": name, "label": col.label, "field": name, "sortable": True,
                  "align": "right" if numeric else "left", "total_text": "", "total_color": ""}
        if numeric:
            quasar["sortOrder"] = "da"           # the first tap on a number column sorts it largest first
        if name == "symbol":
            quasar["total_text"] = _total_label(totals)
        elif name in TOTALLED:
            total = getattr(totals, name)
            quasar["total_text"] = total_text(total, col.kind)
            quasar["total_color"] = _colour(total.value, col.kind) if col.coloured else ""
        out.append(quasar)
    return tag_quasar_columns(out, PIN_TAG_PREFIX)


# ----------------------------------------------------------------------------------------------------------- bars

def bar_fractions(rows: Sequence[PositionRow]) -> Dict[str, Optional[float]]:
    """``{row key: |P/L Open| / the largest |P/L Open|}``; ``None`` for an n/a P/L Open, 0 when nothing is open."""
    largest = max((abs(r.pl_open) for r in rows if r.pl_open is not None), default=0.0)
    return {r.key: None if r.pl_open is None else (abs(r.pl_open) / largest if largest else 0.0) for r in rows}


def weights(rows: Sequence[PositionRow]) -> Dict[str, Optional[float]]:
    """``{row key: |market value| / the sum of |market value|}`` over ``rows``; ``None`` for an n/a market value."""
    total = sum(abs(r.mkt_value) for r in rows if r.mkt_value is not None)
    return {r.key: None if r.mkt_value is None or not total else abs(r.mkt_value) / total for r in rows}


def table_rows(rows: Sequence[PositionRow]) -> List[dict]:
    """The q-table rows: per numeric column the raw value (what Quasar sorts by; ``None`` for n/a), ``<name>_text``
    and ``<name>_color`` (``""`` for an uncoloured column); the bar; the labels as chips and as text; ``search``."""
    bars = bar_fractions(rows)
    shares = weights(rows)
    out = []
    for r in rows:
        values = {"qty": r.qty, "price": r.price, "day_chg_pct": r.day_chg_pct, "pl_day": r.pl_day,
                  "pl_day_pct": r.pl_day_pct, "pl_open_pct": r.pl_open_pct, "pl_open": r.pl_open,
                  "avg_cost": r.avg_cost, "cost_basis": r.cost_basis, "mkt_value": r.mkt_value,
                  "weight_pct": shares[r.key]}
        d: Dict[str, Any] = {"key": r.key, "account_id": r.account_id, "account": r.account, "symbol": r.symbol,
                             "asset_class": r.asset_class, "labels": ", ".join(r.labels),
                             "labels_list": list(r.labels), "search": search_text(r), "stale": r.stale,
                             "pl_open_bar": bars[r.key]}
        for name, value in values.items():
            col = COLUMN_BY_NAME[name]
            d[name] = value
            d[f"{name}_text"] = _text(value, col.kind)
            d[f"{name}_color"] = _colour(value, col.kind) if col.coloured else ""
        out.append(d)
    return out


# ----------------------------------------------------------------------------------------------------------- sorting

def sort_key(value) -> Tuple:
    """Quasar's order: n/a below every value (first ascending, last descending); text ignores case."""
    if value is None:
        return (0,)
    if isinstance(value, str):
        return (1, value.lower())
    return (1, float(value))


def sort_rows(rows: Sequence[dict], sort_by: str, descending: bool) -> List[dict]:
    """The rows in the order the table shows them (ties keep the symbol / account order)."""
    stable = sorted(rows, key=lambda d: (d["symbol"].lower(), d["account"].lower()))
    return sorted(stable, key=lambda d: sort_key(d.get(sort_by)), reverse=descending)


# ----------------------------------------------------------------------------------------------------------- accounts

@dataclass(frozen=True)
class AccountRead:
    """One account's answer in one load. ``rows`` is empty unless ``status`` is ok."""
    account_id: int
    name: str
    status: str
    reason: Optional[str] = None
    rows: Tuple[PositionRow, ...] = ()


@dataclass(frozen=True)
class AccountBook:
    """What is on screen for one account: its last good rows (``as_of`` = when they were read; ``None`` = never),
    and the status of the LATEST read."""
    account_id: int
    name: str
    rows: Tuple[PositionRow, ...]
    as_of: Optional[datetime]
    status: str
    reason: Optional[str] = None


def merge_reads(previous: Mapping[int, AccountBook], reads: Sequence[AccountRead],
                now: datetime) -> Dict[int, AccountBook]:
    """The new screen state: an ok read replaces the account's rows; a failed / timed-out one keeps the last good
    rows, marked stale. Accounts not in ``reads`` (out of scope now) are dropped."""
    out: Dict[int, AccountBook] = {}
    for read in reads:
        if read.status == STATUS_OK:
            out[read.account_id] = AccountBook(read.account_id, read.name, tuple(read.rows), now, STATUS_OK)
            continue
        before = previous.get(read.account_id)
        kept = tuple(mark_stale(r) for r in before.rows) if before is not None else ()
        out[read.account_id] = AccountBook(read.account_id, read.name, kept,
                                           before.as_of if before is not None else None, read.status, read.reason)
    return out


def book_rows(book: Mapping[int, AccountBook]) -> List[PositionRow]:
    return [r for aid in sorted(book) for r in book[aid].rows]


def unread_accounts(book: Mapping[int, AccountBook]) -> int:
    return sum(1 for b in book.values() if b.status != STATUS_OK)


def book_warnings(book: Mapping[int, AccountBook]) -> List[str]:
    """One line per account whose latest read failed, naming it and saying what is shown instead."""
    out = []
    for aid in sorted(book):
        b = book[aid]
        if b.status == STATUS_OK:
            continue
        why = b.reason or b.status
        if b.as_of is not None:
            shown = (f"showing its positions as of {b.as_of.strftime('%H:%M:%S')} (stale)" if b.rows
                     else f"it had no positions at {b.as_of.strftime('%H:%M:%S')} (stale)")
        else:
            shown = "its positions are not shown"
        out.append(f"Account '{b.name}' could not be read ({why}): {shown}.")
    return out


# ----------------------------------------------------------------------------------------------------------- choices

@dataclass(frozen=True)
class PageState:
    """What the Live Trades page remembers: the selected tab, and the Live View's interval and sort."""
    tab: str
    interval: str = DEFAULT_INTERVAL
    sort_by: str = DEFAULT_SORT_BY
    descending: bool = DEFAULT_DESCENDING


def storage_key(port: Optional[int]) -> str:
    """The ``app.storage.user`` key, per port like the heatmap's: two instances on different ports share the
    browser's storage."""
    return f"{STORAGE_KEY}:{port}" if port else STORAGE_KEY


def interval_seconds(choice: str) -> Optional[float]:
    """Seconds between refreshes; ``None`` = Off."""
    if choice not in INTERVALS:
        raise ValueError(f"unknown refresh interval {choice!r}")
    return None if choice == "off" else float(choice)


def resolve_state(stored: Any, tabs: Sequence[str]) -> PageState:
    """The stored choices, each one kept only when it is still valid (the first tab is the default)."""
    s = stored if isinstance(stored, Mapping) else {}
    tab = s.get("tab")
    interval = s.get("interval")
    sort_by = s.get("sort_by")
    descending = s.get("descending")
    return PageState(tab=tab if isinstance(tab, str) and tab in tabs else tabs[0],
                     interval=interval if isinstance(interval, str) and interval in INTERVALS else DEFAULT_INTERVAL,
                     sort_by=sort_by if isinstance(sort_by, str) and sort_by in COLUMN_BY_NAME else DEFAULT_SORT_BY,
                     descending=descending if isinstance(descending, bool) else DEFAULT_DESCENDING)


def state_for_storage(state: PageState) -> Dict[str, Any]:
    return {"tab": state.tab, "interval": state.interval, "sort_by": state.sort_by, "descending": state.descending}


# ----------------------------------------------------------------------------------------------------------- phone

def live_view_phone_css() -> str:
    """Phone only: the table scrolls sideways inside its box with the Symbol pinned (the Stocks table's mechanism),
    the selection column and the label controls are hidden."""
    hide = phone_media(f"""
    .{TABLE_CLASS} th.q-table--col-auto-width, .{TABLE_CLASS} td.q-table--col-auto-width {{ display: none !important; }}
    .{DESKTOP_ONLY_CLASS} {{ display: none !important; }}
""")
    return "\n".join((scroll_table_phone_css(TABLE_CLASS, PIN_TAG_PREFIX, LIVE_PINNED, LIVE_MIN_WIDTHS), hide))
