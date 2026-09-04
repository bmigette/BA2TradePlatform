"""Symbol-info panel data layer: ETF holdings, dividends and TOTAL RETURN.

The UI (a per-symbol popup, several symbols compared side by side) asks for
three things this module answers, per symbol, in one consistent shape:

* **ETF facts** — holdings count, top-10 weight, asset class, category, AUM,
  PE, and the holdings themselves with weights.
* **Income** — dividend yield and payout ratio.
* **Total return INCLUDING dividends** for YTD / 1y / 3y / 5y, plus a charting
  time series (price, dividend bars, cumulative total-return line).

Read :func:`get_symbol_info` and :func:`get_symbols_info`; everything else is
either a cached low-level fetcher (mockable in tests) or a pure computation.


adjClose, and the trap
----------------------
FMP's ``historical-price-full`` returns both ``close`` and ``adjClose``, and
**``adjClose`` already includes reinvested dividends**. So there are two correct
recipes and several plausible-looking wrong ones:

* total return from ``adjClose`` alone — correct;
* total return from ``close`` **plus** separately-summed dividends — also correct;
* any mixture — silently double-counts or omits, and the error looks reasonable.

**This module uses ``adjClose`` alone, everywhere, for total return.** Two
reasons. First, it is one series, so there is no seam at which the two recipes
can be crossed. Second — the decisive one — ``adjClose`` is *also* split
adjusted, and a 3y or 5y window on a real symbol crosses splits; the
``close`` + dividends recipe would additionally need the close series *and*
every historical dividend back-adjusted for each split, which is three more
places to get it wrong.

``dividends_paid_per_share`` and the :class:`DividendEvent` series are therefore
**cash paid per share on the ex-date** — NOT the reinvested quantity, and never
added to a return derived from ``adjClose``. The chart wants both: the dividend
bars are paid cash, the cumulative total-return line is reinvested growth. They
are different quantities and this module keeps them apart.

``test_symbol_info.py`` pins this with a fixture whose four candidate answers
(+26.5% correct, +15.0% / +31.5% / +10.0% wrong) are all distinct numbers.


Splits
------
``price_return_pct`` (the price-only companion figure) comes from ``close``, and
an unadjusted close is not comparable across a split. Whether FMP's ``close`` is
itself split-adjusted is not something this module can verify offline, so it
refuses to guess: when a split falls inside the window — or when the split
history could not be fetched at all — ``price_return_pct`` is ``None`` with a
reason. ``total_return_pct`` is unaffected, because ``adjClose`` is split
adjusted by construction.


Unknown is never zero
---------------------
The house pattern (``ba2_common.core.option_lifecycle``): a value that could not
be measured is ``None``, and a ``detail`` names the missing input. Every group
here carries ``details: Dict[str, str]`` mapping *field name* -> *why it is
None*, read through :meth:`Unknowable.why`. A whole-group failure is recorded
once under the key ``"*"``.

The distinctions that matter, and that are individually pinned by tests:

===========================  ==============================  ========================
field                        FACT                            UNKNOWN
===========================  ==============================  ========================
``is_etf``                   ``False`` (it is a stock)       ``None`` + ``why``
``EtfProfile.holdings``      ``()`` and no reason            ``()`` + ``why``
``dividends``                ``()`` and no reason            ``()`` + ``why``
``dividend_yield_pct``       ``0.0`` (pays nothing)          ``None`` + ``why``
``total_return_pct``         ``0.0`` (flat window)           ``None`` + ``why``
===========================  ==============================  ========================

A 3y or 5y total return for a symbol that has only existed 18 months is
**not computable**: ``None``, with a reason naming the window and the earliest
price actually available. It is never zero, and never a since-inception number
quietly relabelled.


Time
----
``as_of`` is a REQUIRED argument. Nothing in this module reads the wall clock —
a YTD number computed from an implicit "today" cannot be tested, only trusted.
Callers pass ``date.today()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import fmpsdk

from ba2_common.logger import logger
from ba2_providers import symbol_snapshot
from ba2_providers.fmp_common import fmp_http_get, fmp_list_call, fmp_live_cached
# Re-exported deliberately: the fetchers below raise it, so a caller that wants to
# handle an FMP outage itself can `from ba2_providers.symbol_info import FMPError`
# rather than reaching into fmp_common.
from ba2_providers.fmp_common import FMPError

#: The public surface, in the order a caller meets it: the two entry points, the
#: JSON view, the value objects, the pure computations, then the cached fetchers.
__all__ = [
    # entry points
    "get_symbol_info", "get_symbols_info", "symbol_info_to_dict",
    # value objects
    "SymbolInfo", "EtfProfile", "EtfHolding", "IncomeInfo", "WindowReturn",
    "SeriesBundle", "SeriesPoint", "PricePoint", "DividendEvent", "SplitEvent",
    "Unknowable",
    # pure computation
    "window_start_date", "compute_window_return", "build_series", "parse_income",
    "parse_etf_profile", "parse_price_history", "parse_dividends", "parse_splits",
    # cached fetchers
    "fetch_profile", "fetch_etf_info", "fetch_etf_holdings", "fetch_ratios_ttm",
    "fetch_price_history", "fetch_dividends", "fetch_splits",
    # constants + the error callers may want to catch
    "WINDOWS", "TOP_N_HOLDINGS", "CACHE_TTL_SECONDS", "PRICE_FETCH_BUFFER_DAYS",
    "CHART_HISTORY_YEARS", "years_before",
    "FMPError",
]

#: Windows the panel offers. ``"ytd"`` is measured from the LAST CLOSE OF THE
#: PRIOR YEAR (the standard base), not from the first bar of January.
WINDOWS: Tuple[str, ...] = ("ytd", "1y", "3y", "5y")

_YEARS_BACK = {"1y": 1, "3y": 3, "5y": 5}


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Unknowable:
    """Mixin for groups that carry per-field "why is this None" reasons.

    ``details`` maps a field name to the reason that field is ``None``. A
    failure that took out the whole group is stored once under ``"*"``.
    """
    details: Dict[str, str] = field(default_factory=dict)

    def why(self, field_name: str) -> str:
        """Why ``field_name`` is ``None``/empty. ``""`` when it was measured."""
        return self.details.get(field_name) or self.details.get("*") or ""

    def is_unknown(self, field_name: str) -> bool:
        """True when ``field_name`` is unknown (as opposed to a measured value)."""
        return self.why(field_name) != ""


@dataclass(frozen=True)
class PricePoint:
    """One daily bar. ``close`` is FMP's close; ``adj_close`` is its ``adjClose``."""
    date: date
    close: Optional[float]
    adj_close: Optional[float]


@dataclass(frozen=True)
class DividendEvent:
    """One cash dividend. ``date`` is the EX-dividend date (FMP's ``date``).

    ``dividend`` is as declared; ``adj_dividend`` is FMP's split-adjusted
    ``adjDividend`` and is the one a multi-year bar chart should plot, because
    the as-declared amount is not comparable across a split.
    """
    ex_date: date
    dividend: Optional[float]
    adj_dividend: Optional[float]

    @property
    def chart_amount(self) -> Optional[float]:
        """The split-comparable amount to plot: ``adj_dividend`` when present.

        Falls back to ``dividend`` ONLY when ``adj_dividend`` is absent — which
        is the as-declared amount and therefore not split-comparable; callers
        that care can check ``adj_dividend is None`` themselves.
        """
        return self.adj_dividend if self.adj_dividend is not None else self.dividend


@dataclass(frozen=True)
class SplitEvent:
    """One share split. ``ratio`` is ``numerator / denominator`` (4-for-1 -> 4.0)."""
    date: date
    numerator: Optional[float]
    denominator: Optional[float]
    ratio: Optional[float]


@dataclass(frozen=True)
class WindowReturn(Unknowable):
    """Return over one named window.

    ``total_return_pct`` INCLUDES reinvested dividends and is derived from
    ``adjClose`` alone (see the module docstring). ``price_return_pct`` is the
    price-only companion from ``close``, suppressed across a split.
    ``dividends_paid_per_share`` is cash PAID in the window — never added to
    ``total_return_pct``.

    Percent units throughout: ``26.5`` means +26.5%.
    """
    window: str = ""
    total_return_pct: Optional[float] = None
    price_return_pct: Optional[float] = None
    dividends_paid_per_share: Optional[float] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    start_adj_close: Optional[float] = None
    end_adj_close: Optional[float] = None


@dataclass(frozen=True)
class EtfHolding:
    """One ETF constituent. ``weight_pct`` is percent (``14.46`` == 14.46%)."""
    symbol: Optional[str]
    name: Optional[str]
    weight_pct: Optional[float]
    shares: Optional[float]
    market_value: Optional[float]


@dataclass(frozen=True)
class EtfProfile(Unknowable):
    """ETF facts. Present only when ``SymbolInfo.is_etf`` is ``True``.

    ``holdings`` empty with no reason means FMP returned no constituents;
    empty WITH a reason (``why("holdings")``) means the fetch failed.
    """
    holdings_count: Optional[int] = None
    top10_weight_pct: Optional[float] = None
    asset_class: Optional[str] = None
    category: Optional[str] = None
    assets_under_management: Optional[float] = None
    pe_ratio: Optional[float] = None
    expense_ratio: Optional[float] = None
    nav: Optional[float] = None
    inception_date: Optional[str] = None
    etf_company: Optional[str] = None
    holdings: Tuple[EtfHolding, ...] = ()


@dataclass(frozen=True)
class IncomeInfo(Unknowable):
    """Dividend yield and payout ratio.

    ``dividend_yield_pct`` / ``payout_ratio_pct`` come from FMP's TTM ratios and
    are converted from FMP's fraction to PERCENT (``0.0044`` -> ``0.44``).
    ``dividend_yield_pct_computed`` is the independent cross-check derived from
    the dividend series and the last close; it is offered ALONGSIDE the reported
    figure rather than silently substituted for it.
    """
    dividend_yield_pct: Optional[float] = None
    payout_ratio_pct: Optional[float] = None
    trailing_12m_dividend_per_share: Optional[float] = None
    dividend_yield_pct_computed: Optional[float] = None


@dataclass(frozen=True)
class SeriesPoint:
    """One charting point. ``cumulative_total_return_pct`` is REINVESTED growth
    versus the first point of the series, from ``adj_close``."""
    date: date
    close: Optional[float]
    adj_close: Optional[float]
    cumulative_total_return_pct: Optional[float]


@dataclass(frozen=True)
class SeriesBundle(Unknowable):
    """The three chart series. ``dividends`` are PAID cash; ``points`` carry the
    REINVESTED cumulative total-return line. Different quantities, on purpose."""
    points: Tuple[SeriesPoint, ...] = ()
    dividends: Tuple[DividendEvent, ...] = ()
    splits: Tuple[SplitEvent, ...] = ()
    start: Optional[date] = None
    end: Optional[date] = None


@dataclass(frozen=True)
class SymbolInfo(Unknowable):
    """Everything the panel needs for ONE symbol. Shape is identical for every
    symbol, so several can be rendered side by side without special-casing.

    ``is_etf`` is TRI-STATE: ``True`` (``etf`` populated), ``False`` (a stock —
    it has no holdings, which is a fact, and ``etf`` is ``None``), or ``None``
    (we could not tell — ``why("is_etf")`` says why, and ``etf`` is ``None`` for
    a completely different reason).
    """
    symbol: str = ""
    as_of: Optional[date] = None
    is_etf: Optional[bool] = None
    etf: Optional[EtfProfile] = None
    income: IncomeInfo = field(default_factory=IncomeInfo)
    returns: Mapping[str, WindowReturn] = field(default_factory=dict)
    series: SeriesBundle = field(default_factory=SeriesBundle)
    history_start: Optional[date] = None
    last_close: Optional[float] = None


# ---------------------------------------------------------------------------
# pure computation — no network, no clock
# ---------------------------------------------------------------------------
def years_before(as_of: date, years: int) -> date:
    """``as_of`` minus whole years, folding 29 February to 28. Pure.

    Split out of :func:`window_start_date` so the CHART can ask for a span that is
    not one of the return :data:`WINDOWS`. Those two spans used to be the same
    number, which is why a ten-year chart range could never work: the returns table
    needs at most five years, so five years was all that was ever fetched.
    """
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:      # 29 Feb -> 28 Feb
        return as_of.replace(year=as_of.year - years, day=28)


def window_start_date(window: str, as_of: date) -> date:
    """The date a window is measured FROM.

    ``ytd`` -> 31 December of the prior year (the standard base is the prior
    year's last close, not the first bar of January). ``1y``/``3y``/``5y`` ->
    the same calendar day N years earlier, with 29 February folded to 28.
    """
    if window == "ytd":
        return date(as_of.year - 1, 12, 31)
    if window not in _YEARS_BACK:
        raise ValueError(
            f"unknown window {window!r} — expected one of {WINDOWS}")
    return years_before(as_of, _YEARS_BACK[window])


def _last_at_or_before(points: Sequence[PricePoint], cutoff: date) -> Optional[PricePoint]:
    """The most recent bar dated on or before ``cutoff`` (``points`` ascending)."""
    found = None
    for p in points:
        if p.date <= cutoff:
            found = p
        else:
            break
    return found


def compute_window_return(
    window: str,
    points: Sequence[PricePoint],
    dividends: Optional[Sequence[DividendEvent]],
    splits: Optional[Sequence[SplitEvent]],
    as_of: date,
) -> WindowReturn:
    """Return over ``window``, from price bars that are already sorted ascending.

    ``dividends`` / ``splits`` accept ``None`` to mean **could not be fetched**,
    which is not the same as ``[]`` (there genuinely are none) — the former makes
    the dependent field unknown, the latter does not.

    The base bar is the last one dated on or before ``window_start_date``. If no
    such bar exists the symbol did not exist that far back and the window is
    **not computable**: every figure is ``None`` and ``why`` names the window and
    the earliest price we do have. That is deliberately strict — reporting a
    since-inception number under a "5y" label is the failure this avoids.
    """
    start_cutoff = window_start_date(window, as_of)
    details: Dict[str, str] = {}

    if not points:
        return WindowReturn(
            details={"*": "no price history available — no return can be computed"},
            window=window)

    end = _last_at_or_before(points, as_of)
    if end is None:
        return WindowReturn(
            details={"*": (f"no price on or before as_of {as_of.isoformat()} — the earliest "
                           f"price is {points[0].date.isoformat()}")},
            window=window)

    base = _last_at_or_before(points, start_cutoff)
    if base is None:
        earliest = points[0].date
        months = round((as_of - earliest).days / 30.44, 1)
        return WindowReturn(
            details={"*": (
                f"a {window} return needs a price on or before {start_cutoff.isoformat()}, but "
                f"the earliest price available is {earliest.isoformat()} ({months} months of "
                f"history) — not computable")},
            window=window, end_date=end.date, end_adj_close=end.adj_close)

    # --- total return: adjClose ONLY. Never close, never plus dividends. -----
    total_return_pct: Optional[float] = None
    if base.adj_close is None or end.adj_close is None:
        missing = "start" if base.adj_close is None else "end"
        details["total_return_pct"] = (
            f"the {missing} bar has no adjClose — total return is unmeasurable")
    elif base.adj_close <= 0:
        details["total_return_pct"] = (
            f"the start bar's adjClose is {base.adj_close} — a return needs a positive base")
    else:
        total_return_pct = (end.adj_close / base.adj_close - 1.0) * 100.0

    # --- price-only return: close, and only when the window crosses no split --
    price_return_pct: Optional[float] = None
    if splits is None:
        details["price_return_pct"] = (
            "split history could not be fetched — an unadjusted close is not comparable "
            "across a split, so a price-only return cannot be certified")
    else:
        crossed = [s for s in splits if base.date < s.date <= end.date]
        if crossed:
            names = ", ".join(
                f"{s.date.isoformat()}"
                + (f" ({s.ratio:g}-for-1)" if s.ratio is not None else "")
                for s in crossed)
            details["price_return_pct"] = (
                f"a split falls inside the window ({names}) — the unadjusted close is not "
                f"comparable across it; use total_return_pct, which is split adjusted")
        elif base.close is None or end.close is None:
            missing = "start" if base.close is None else "end"
            details["price_return_pct"] = f"the {missing} bar has no close"
        elif base.close <= 0:
            details["price_return_pct"] = (
                f"the start bar's close is {base.close} — a return needs a positive base")
        else:
            price_return_pct = (end.close / base.close - 1.0) * 100.0

    # --- dividends PAID in the window (cash per share, NOT reinvested) -------
    dividends_paid: Optional[float] = None
    if dividends is None:
        details["dividends_paid_per_share"] = (
            "dividend history could not be fetched — paid dividends are unknown, not zero")
    else:
        in_window = [d for d in dividends if base.date < d.ex_date <= end.date]
        amounts = [d.chart_amount for d in in_window]
        if any(a is None for a in amounts):
            details["dividends_paid_per_share"] = (
                f"{sum(1 for a in amounts if a is None)} of {len(amounts)} dividend records "
                f"carry no amount — the window total would be understated")
        else:
            dividends_paid = float(sum(amounts))

    return WindowReturn(
        details=details,
        window=window,
        total_return_pct=total_return_pct,
        price_return_pct=price_return_pct,
        dividends_paid_per_share=dividends_paid,
        start_date=base.date,
        end_date=end.date,
        start_adj_close=base.adj_close,
        end_adj_close=end.adj_close,
    )


# ---------------------------------------------------------------------------
# parsing FMP payloads -> value objects (pure)
# ---------------------------------------------------------------------------
def _as_float(value: Any) -> Optional[float]:
    """Coerce an FMP numeric to float. Absent/null/unparseable -> ``None``.

    Never ``0.0``: a field FMP did not send and a field FMP sent as zero are
    different facts, and this is the seam where they would be conflated.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> Optional[date]:
    """Parse FMP's ``YYYY-MM-DD`` (or ``YYYY-MM-DD HH:MM:SS``) date string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _historical_rows(payload: Any) -> List[dict]:
    """The ``historical`` list out of an FMP ``historical-price-full*`` payload.

    All three of price / ``stock_dividend`` / ``stock_split`` wrap their rows in
    ``{"symbol": ..., "historical": [...]}``. A missing key yields ``[]`` — for
    dividends and splits that is the normal shape for a symbol that has none. A
    *fetch failure* never reaches here: the fetchers raise, and the assembler
    turns the exception into an explicit unknown.
    """
    if isinstance(payload, dict):
        rows = payload.get("historical")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if isinstance(payload, list):       # some FMP endpoints return a bare list
        return [r for r in payload if isinstance(r, dict)]
    return []


def parse_price_history(payload: Any) -> List[PricePoint]:
    """FMP ``historical-price-full`` payload -> bars sorted OLDEST FIRST.

    ``adjClose`` is carried through verbatim and is **never** defaulted to
    ``close``: that substitution would silently strip the reinvested dividends
    out of every total return while still producing a plausible number.
    """
    points = [
        PricePoint(date=d, close=_as_float(r.get("close")),
                   adj_close=_as_float(r.get("adjClose")))
        for r in _historical_rows(payload)
        for d in [_as_date(r.get("date"))] if d is not None
    ]
    points.sort(key=lambda p: p.date)
    return points


def parse_dividends(payload: Any) -> List[DividendEvent]:
    """FMP ``historical-price-full/stock_dividend`` payload -> ex-date ascending."""
    events = [
        DividendEvent(ex_date=d, dividend=_as_float(r.get("dividend")),
                      adj_dividend=_as_float(r.get("adjDividend")))
        for r in _historical_rows(payload)
        for d in [_as_date(r.get("date"))] if d is not None
    ]
    events.sort(key=lambda e: e.ex_date)
    return events


def parse_splits(payload: Any) -> List[SplitEvent]:
    """FMP ``historical-price-full/stock_split`` payload -> date ascending.

    ``ratio`` stays ``None`` when either side is missing or the denominator is
    zero — an unknown ratio must not read as a 0-for-1 split.
    """
    events = []
    for r in _historical_rows(payload):
        d = _as_date(r.get("date"))
        if d is None:
            continue
        num = _as_float(r.get("numerator"))
        den = _as_float(r.get("denominator"))
        ratio = num / den if (num is not None and den) else None
        events.append(SplitEvent(date=d, numerator=num, denominator=den, ratio=ratio))
    events.sort(key=lambda e: e.date)
    return events


# ---------------------------------------------------------------------------
# the chart bundle (pure)
# ---------------------------------------------------------------------------
def build_series(
    points: Sequence[PricePoint],
    dividends: Optional[Sequence[DividendEvent]],
    splits: Optional[Sequence[SplitEvent]],
    start: date,
    end: date,
) -> SeriesBundle:
    """Clip every series to ``[start, end]`` and attach the cumulative line.

    Three series, two different quantities:

    * ``points[].close`` / ``adj_close`` — the price line;
    * ``dividends[].chart_amount``       — **cash PAID** per share, bar chart;
    * ``points[].cumulative_total_return_pct`` — **REINVESTED** growth versus the
      first point in the window, from ``adj_close``.

    The last two are not the same number and must never be added together.

    ``dividends=None`` / ``splits=None`` mean *could not be fetched*: the series
    comes back empty **with a reason**, which the UI must render as n/a rather
    than as an empty bar chart.
    """
    details: Dict[str, str] = {}
    clipped = [p for p in points if start <= p.date <= end]

    base = clipped[0].adj_close if clipped else None
    if clipped and (base is None or base <= 0):
        details["points"] = (
            f"the first bar in the window ({clipped[0].date.isoformat()}) has no usable "
            f"adjClose ({base!r}) — the cumulative total-return line has no base")
        base = None

    series_points = tuple(
        SeriesPoint(
            date=p.date, close=p.close, adj_close=p.adj_close,
            cumulative_total_return_pct=(
                (p.adj_close / base - 1.0) * 100.0
                if (base is not None and p.adj_close is not None) else None),
        )
        for p in clipped
    )

    if dividends is None:
        details["dividends"] = (
            "dividend history could not be fetched — the bar series is unknown, not empty")
        div_series: Tuple[DividendEvent, ...] = ()
    else:
        div_series = tuple(d for d in dividends if start <= d.ex_date <= end)

    if splits is None:
        details["splits"] = (
            "split history could not be fetched — the window may cross a split")
        split_series: Tuple[SplitEvent, ...] = ()
    else:
        split_series = tuple(s for s in splits if start <= s.date <= end)

    return SeriesBundle(details=details, points=series_points, dividends=div_series,
                        splits=split_series, start=start, end=end)


# ---------------------------------------------------------------------------
# ETF profile + holdings (pure)
# ---------------------------------------------------------------------------
#: FMP has renamed / re-cased these fields between API generations, so each is
#: read through a candidate list — the same defence ``FMPCompanyDetailsProvider``
#: uses for its TTM ratios. When NONE of the candidates is present the field is
#: unknown and the reason names the keys that were tried, so a rename shows up as
#: a legible n/a instead of a wrong number.
_ETF_INFO_KEYS: Dict[str, Tuple[str, ...]] = {
    "asset_class": ("assetClass", "asset_class", "assetclass"),
    "category": ("category", "etfCategory", "fundCategory", "sector"),
    "assets_under_management": ("assetsUnderManagement", "aum", "totalAssets"),
    "pe_ratio": ("peRatio", "pe", "priceEarningsRatio", "peRatioTTM"),
    "expense_ratio": ("expenseRatio", "netExpenseRatio"),
    "nav": ("nav", "navPrice"),
    "etf_company": ("etfCompany", "fundFamily", "issuer"),
    "inception_date": ("inceptionDate", "inception"),
    "holdings_count": ("holdingsCount", "holdingsCounts", "numberOfHoldings"),
}

#: FMP's ``etf-holder`` rows have moved between these names too.
_HOLDING_SYMBOL_KEYS = ("asset", "symbol", "ticker")
_HOLDING_NAME_KEYS = ("name", "securityName", "holdingName")
_HOLDING_WEIGHT_KEYS = ("weightPercentage", "weight", "weightPercent", "pctVal")
_HOLDING_SHARES_KEYS = ("sharesNumber", "shares", "sharesHeld")
_HOLDING_VALUE_KEYS = ("marketValue", "value")

#: How many holdings the "top-N" concentration figure covers.
TOP_N_HOLDINGS = 10


def _pick(payload: Mapping[str, Any], keys: Sequence[str]) -> Tuple[Any, bool]:
    """First present, non-null value among ``keys``. Returns ``(value, found)``."""
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k], True
    return None, False


def parse_etf_profile(
    info: Optional[Mapping[str, Any]],
    holdings: Optional[Sequence[Mapping[str, Any]]],
) -> EtfProfile:
    """FMP ``etf-info`` + ``etf-holder`` payloads -> :class:`EtfProfile`.

    ``info=None`` / ``holdings=None`` mean **that fetch failed**; ``[]`` means
    FMP genuinely returned no constituents. The two produce the same empty
    ``holdings`` tuple but different ``details``, so the UI can tell "this fund
    reports nothing" from "we could not ask".

    ``holdings_count`` comes from FMP's ``holdingsCount`` alone and is never
    quietly replaced by ``len(holdings)``: the holder endpoint can truncate, and
    a truncated length presented as the fund's size is a wrong number rather
    than a missing one. Callers wanting the length can read ``len(holdings)``.

    ``top10_weight_pct`` is DERIVED from the holdings, so it survives an
    ``etf-info`` failure — but it goes unknown if any of the top-N rows carries
    no weight, because summing over the gap would understate concentration.
    """
    details: Dict[str, str] = {}

    # --- scalar fields off etf-info -------------------------------------
    values: Dict[str, Any] = {}
    if info is None:
        for name in _ETF_INFO_KEYS:
            details[name] = "FMP etf-info could not be fetched — this field is unknown"
            values[name] = None
    else:
        for name, keys in _ETF_INFO_KEYS.items():
            raw, found = _pick(info, keys)
            if not found:
                details[name] = (
                    f"FMP etf-info carried none of {'/'.join(keys)} — {name} is unknown")
                values[name] = None
            elif name in ("asset_class", "category", "etf_company", "inception_date"):
                values[name] = str(raw)
            elif name == "holdings_count":
                num = _as_float(raw)
                if num is None:
                    details[name] = f"FMP etf-info holdings count {raw!r} is not a number"
                values[name] = int(num) if num is not None else None
            else:
                num = _as_float(raw)
                if num is None:
                    details[name] = f"FMP etf-info {name} {raw!r} is not a number"
                values[name] = num

    # --- holdings -------------------------------------------------------
    if holdings is None:
        details["holdings"] = (
            "FMP etf-holder could not be fetched — the constituents are unknown, "
            "not absent")
        parsed: List[EtfHolding] = []
        top10: Optional[float] = None
        details["top10_weight_pct"] = (
            "the holdings could not be fetched, so the top-10 concentration is unknown")
    else:
        parsed = [
            EtfHolding(
                symbol=(lambda v: str(v) if v is not None else None)(
                    _pick(h, _HOLDING_SYMBOL_KEYS)[0]),
                name=(lambda v: str(v) if v is not None else None)(
                    _pick(h, _HOLDING_NAME_KEYS)[0]),
                weight_pct=_as_float(_pick(h, _HOLDING_WEIGHT_KEYS)[0]),
                shares=_as_float(_pick(h, _HOLDING_SHARES_KEYS)[0]),
                market_value=_as_float(_pick(h, _HOLDING_VALUE_KEYS)[0]),
            )
            for h in holdings if isinstance(h, Mapping)
        ]
        # Sort by weight descending; rows with no weight sink to the bottom so
        # they never displace a real holding out of the top N.
        parsed.sort(key=lambda h: (h.weight_pct is None, -(h.weight_pct or 0.0)))
        top_rows = parsed[:TOP_N_HOLDINGS]
        missing = [h for h in top_rows if h.weight_pct is None]
        if missing:
            details["top10_weight_pct"] = (
                f"{len(missing)} of the top {len(top_rows)} holdings carry no weight "
                f"(e.g. {missing[0].symbol!r}) — summing over the gap would understate "
                f"the concentration")
            top10 = None
        else:
            top10 = float(sum(h.weight_pct for h in top_rows))

    return EtfProfile(
        details=details,
        holdings_count=values.get("holdings_count"),
        top10_weight_pct=top10,
        asset_class=values.get("asset_class"),
        category=values.get("category"),
        assets_under_management=values.get("assets_under_management"),
        pe_ratio=values.get("pe_ratio"),
        expense_ratio=values.get("expense_ratio"),
        nav=values.get("nav"),
        inception_date=values.get("inception_date"),
        etf_company=values.get("etf_company"),
        holdings=tuple(parsed),
    )


# ---------------------------------------------------------------------------
# cached FMP fetchers
# ---------------------------------------------------------------------------
#: One day. The panel's inputs — an ETF's constituents, its AUM, a dividend
#: history, a multi-year price series — change at most daily, and the popup is
#: opened repeatedly for the same handful of symbols.
#:
#: The caching itself is ``fmp_common.fmp_live_cached``, NOT a new mechanism.
#: That matters more than it looks: ``fmp_live_cached`` keeps ONE CACHE PER
#: DISTINCT TTL, keyed by ``ttl_seconds``, because ``TTLCache`` fixes its expiry
#: window at construction — so passing a different TTL here would not shorten
#: this cache, it would create a second one and quietly halve the hit rate.
#: It is also a passthrough inside ``frozen_ttl_cache()`` (the backtest path),
#: which is right: a frozen cache never expires and would pin a live payload for
#: a whole run.
CACHE_TTL_SECONDS = 24 * 3600.0

_FMP_V3 = "https://financialmodelingprep.com/api/v3"
_FMP_V4 = "https://financialmodelingprep.com/api/v4"

#: Every cache key starts with this, so the panel can never collide with another
#: caller that happens to pick the same TTL.
_KEY_PREFIX = "symbol_info"


def _cached(key: str, fetch: Callable[[], Any]) -> Any:
    return fmp_live_cached(key, fetch, CACHE_TTL_SECONDS)


def _first_row(rows: Any) -> Optional[dict]:
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


def _get_json(path: str, api_key: str, symbol: str, endpoint: str,
              extra: Optional[Mapping[str, str]] = None, base: str = _FMP_V3) -> Any:
    """GET an FMP path through the shared ``fmp_http_get``.

    Reused rather than a bare ``requests.get`` so these calls inherit the 429/5xx
    backoff, the GLOBAL rate-limit gate (a bare get would storm the limit
    alongside the screener), and the hermetic-backtest guard.
    """
    params = {"apikey": api_key}
    params.update(extra or {})
    url = f"{base}/{path}" if path else base
    return fmp_http_get(url, params=params, symbol=symbol, endpoint=endpoint).json()


def fetch_etf_info(api_key: str, symbol: str) -> Optional[dict]:
    """FMP ``/v4/etf-info?symbol=`` for one symbol, or ``None`` when FMP has no row.

    Called over HTTP rather than through fmpsdk because the installed fmpsdk
    defines ``etf_info`` in ``fmpsdk/etf.py`` but does **not** re-export it from
    the package (unlike ``etf_holders``), so ``fmpsdk.etf_info`` does not exist.
    ``test_etf_info_is_not_available_through_fmpsdk`` pins that, and will fail if
    a future fmpsdk starts exporting it.

    Raises ``FMPError`` if the call itself fails — a failure must reach the
    assembler as an exception so it becomes an explicit unknown, never an empty
    dict that reads as "this ETF has no AUM".
    """
    sym = symbol.upper()

    def fetch():
        return _first_row(fmp_list_call(
            lambda: _get_json("etf-info", api_key, sym, "etf-info",
                              {"symbol": sym}, base=_FMP_V4),
            symbol=sym, endpoint="etf-info"))

    return _cached(f"{_KEY_PREFIX}:etf-info:{sym}", fetch)


def fetch_etf_holdings(api_key: str, symbol: str) -> List[dict]:
    """FMP ``/v3/etf-holder/{symbol}`` — the constituents with their weights."""
    sym = symbol.upper()

    def fetch():
        return fmp_list_call(lambda: fmpsdk.etf_holders(apikey=api_key, symbol=sym),
                             symbol=sym, endpoint="etf-holder")

    return _cached(f"{_KEY_PREFIX}:etf-holder:{sym}", fetch)


def fetch_profile(api_key: str, symbol: str) -> Optional[dict]:
    """FMP company profile — the ``isEtf`` flag comes from here.

    Delegates to ``symbol_snapshot.fetch_profile`` (the existing SYMBOL360
    helper) rather than making a second copy of the same call, and adds the
    1-day cache on top.
    """
    sym = symbol.upper()
    return _cached(f"{_KEY_PREFIX}:profile:{sym}",
                   lambda: symbol_snapshot.fetch_profile(api_key, sym))


def fetch_ratios_ttm(api_key: str, symbol: str) -> Optional[dict]:
    """FMP TTM financial ratios — the source of ``dividendYieldTTM`` /
    ``payoutRatioTTM``, the same two fields ``FMPCompanyDetailsProvider`` reads."""
    sym = symbol.upper()

    def fetch():
        return _first_row(fmp_list_call(
            lambda: fmpsdk.financial_ratios_ttm(apikey=api_key, symbol=sym),
            symbol=sym, endpoint="financial_ratios_ttm"))

    return _cached(f"{_KEY_PREFIX}:ratios-ttm:{sym}", fetch)


def fetch_price_history(api_key: str, symbol: str, start: date, end: date) -> Any:
    """FMP ``/v3/historical-price-full/{symbol}`` over ``[start, end]``.

    Returns the RAW payload (``{"symbol":…, "historical":[…]}``) — parse it with
    :func:`parse_price_history`. The cache key carries the window as well as the
    symbol: two different windows are two different responses, and serving one
    for the other would silently truncate a 5y return to a 1y one.
    """
    sym = symbol.upper()
    frm, to = start.isoformat(), end.isoformat()
    return _cached(
        f"{_KEY_PREFIX}:historical-price-full:{sym}:{frm}:{to}",
        lambda: _get_json(f"historical-price-full/{sym}", api_key, sym,
                          "historical-price-full", {"from": frm, "to": to}))


def fetch_dividends(api_key: str, symbol: str) -> Any:
    """FMP ``/v3/historical-price-full/stock_dividend/{symbol}`` (raw payload).

    Unwindowed on purpose: the full history is one small response, and caching it
    once per symbol serves every window the panel offers.
    """
    sym = symbol.upper()
    return _cached(
        f"{_KEY_PREFIX}:stock_dividend:{sym}",
        lambda: _get_json(f"historical-price-full/stock_dividend/{sym}", api_key, sym,
                          "stock_dividend"))


def fetch_splits(api_key: str, symbol: str) -> Any:
    """FMP ``/v3/historical-price-full/stock_split/{symbol}`` (raw payload)."""
    sym = symbol.upper()
    return _cached(
        f"{_KEY_PREFIX}:stock_split:{sym}",
        lambda: _get_json(f"historical-price-full/stock_split/{sym}", api_key, sym,
                          "stock_split"))


# ---------------------------------------------------------------------------
# income (pure)
# ---------------------------------------------------------------------------
#: ``dividendYielTTM`` is FMP's own typo, live on some plans.
#: ``FMPCompanyDetailsProvider`` already tolerates it (``:1026``) and so must this.
_YIELD_KEYS = ("dividendYieldTTM", "dividendYielTTM")
_PAYOUT_KEYS = ("payoutRatioTTM", "dividendPayoutRatioTTM")


def parse_income(
    ratios: Optional[Mapping[str, Any]],
    dividends: Optional[Sequence[DividendEvent]],
    last_close: Optional[float],
    as_of: date,
) -> IncomeInfo:
    """TTM yield / payout from FMP's ratios, plus a derived cross-check.

    FMP reports both ratios as FRACTIONS (``0.0044``); they are converted to
    PERCENT here (``0.44``) so the field name and the number agree — the same
    ``x100`` ``FMPCompanyDetailsProvider`` applies when it renders them.

    ``ratios=None`` / ``dividends=None`` mean the fetch failed. A symbol that
    genuinely pays nothing has a trailing-12m of ``0.0``; one whose dividend
    history could not be fetched has ``None``. Those are different facts and the
    UI must be able to render "n/a" for the second.
    """
    details: Dict[str, str] = {}
    yield_pct = payout_pct = None

    if ratios is None:
        details["dividend_yield_pct"] = (
            "FMP TTM ratios could not be fetched — the yield is unknown, not zero")
        details["payout_ratio_pct"] = (
            "FMP TTM ratios could not be fetched — the payout ratio is unknown, not zero")
    else:
        for target, keys in (("dividend_yield_pct", _YIELD_KEYS),
                             ("payout_ratio_pct", _PAYOUT_KEYS)):
            raw, found = _pick(ratios, keys)
            if not found:
                details[target] = (
                    f"FMP TTM ratios carried none of {'/'.join(keys)} — {target} is unknown")
                continue
            num = _as_float(raw)
            if num is None:
                details[target] = f"FMP TTM ratios {target} {raw!r} is not a number"
                continue
            value = num * 100.0
            if target == "dividend_yield_pct":
                yield_pct = value
            else:
                payout_pct = value

    # --- trailing 12 months of PAID dividends ---------------------------
    ttm: Optional[float] = None
    if dividends is None:
        details["trailing_12m_dividend_per_share"] = (
            "dividend history could not be fetched — the trailing 12m total is unknown, "
            "not zero")
    else:
        cutoff = window_start_date("1y", as_of)
        amounts = [d.chart_amount for d in dividends if cutoff < d.ex_date <= as_of]
        if any(a is None for a in amounts):
            details["trailing_12m_dividend_per_share"] = (
                f"{sum(1 for a in amounts if a is None)} of {len(amounts)} dividend records "
                f"in the last 12 months carry no amount — the total would be understated")
        else:
            ttm = float(sum(amounts))

    # --- the independent cross-check ------------------------------------
    computed: Optional[float] = None
    if ttm is None:
        details["dividend_yield_pct_computed"] = (
            "the trailing 12m dividend is unknown, so a derived yield cannot be computed")
    elif last_close is None:
        details["dividend_yield_pct_computed"] = (
            "no last close — a derived yield needs a price to divide by")
    elif last_close <= 0:
        details["dividend_yield_pct_computed"] = (
            f"the last close is {last_close} — a derived yield needs a positive price")
    else:
        computed = ttm / last_close * 100.0

    return IncomeInfo(details=details, dividend_yield_pct=yield_pct,
                      payout_ratio_pct=payout_pct,
                      trailing_12m_dividend_per_share=ttm,
                      dividend_yield_pct_computed=computed)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
#: The price request must start EARLIER than the longest window, because the
#: base bar is the last bar *on or before* the window start and that date is
#: usually a weekend or a holiday. Thirty days clears any holiday cluster while
#: keeping the payload small. This is a FETCH margin, not a data default: it
#: widens what we ask FMP for, and changes no computed value.
PRICE_FETCH_BUFFER_DAYS = 30


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


#: Years of price history fetched FOR THE CHART, independent of :data:`WINDOWS`.
#:
#: The two were the same number until 2026-09-05, and that was a bug with no visible
#: symptom: the returns table needs at most five years, so five years was all that was
#: ever fetched, so the chart's 10Y range silently showed the same five years on a
#: twenty-year-old ETF. Reported as "clicking 10Y or Max doesn't seem to do anything"
#: against XLK, which has traded since 1998.
#:
#: It costs roughly double the price payload per symbol. That is paid once per panel
#: open (the fetchers are cached for ``CACHE_TTL_SECONDS``) and buys a range control
#: that tells the truth; the alternative was to delete the 10Y button.
CHART_HISTORY_YEARS = 10


def _longest_window_years(windows: Sequence[str]) -> int:
    """Years of history the requested windows need (``ytd`` needs one)."""
    return max([_YEARS_BACK[w] for w in windows if w in _YEARS_BACK] or [1])


def _resolve_is_etf(api_key: str, symbol: str,
                    details: Dict[str, str]) -> Optional[bool]:
    """TRI-STATE. ``True``/``False`` are facts; ``None`` records why we can't tell."""
    try:
        profile = fetch_profile(api_key, symbol)
    except Exception as e:
        logger.warning(f"symbol_info: profile fetch failed for {symbol}: {e}")
        details["is_etf"] = (
            f"the FMP company profile could not be fetched ({_reason(e)}) — whether "
            f"{symbol} is an ETF is unknown, so its holdings are unknown too")
        details["etf"] = details["is_etf"]
        return None
    if not isinstance(profile, Mapping) or profile.get("isEtf") is None:
        details["is_etf"] = (
            f"the FMP company profile for {symbol} carried no 'isEtf' flag — whether it "
            f"is an ETF is unknown")
        details["etf"] = details["is_etf"]
        return None
    return bool(profile["isEtf"])


def get_symbol_info(
    api_key: str,
    symbol: str,
    *,
    as_of: date,
    windows: Sequence[str] = WINDOWS,
    chart_years: int = CHART_HISTORY_YEARS,
) -> SymbolInfo:
    """Everything the symbol-info panel needs for ONE symbol.

    ``as_of`` is required and is the ONLY clock: pass ``date.today()`` from the
    UI. Every window, and the chart's span, is measured from it.

    Never raises for a data problem. Each section degrades independently to
    ``None`` plus a reason on the owning group (``info.why(...)``,
    ``info.etf.why(...)``, ``info.income.why(...)``,
    ``info.returns[w].why(...)``, ``info.series.why(...)``), so a rate-limited
    holdings call cannot blank out the returns and a missing dividend history
    cannot make a real return look like zero.

    FMP endpoints used, all behind the 1-day cache:

    ==================================================  ==========================
    ``/v3/profile/{symbol}``  (via ``symbol_snapshot``)  the ``isEtf`` flag
    ``/v4/etf-info?symbol=``                             AUM, PE, class, category
    ``/v3/etf-holder/{symbol}``                          constituents + weights
    ``/v3/ratios-ttm/{symbol}``                          yield, payout ratio
    ``/v3/historical-price-full/{symbol}``               close + **adjClose**
    ``/v3/historical-price-full/stock_dividend/…``       PAID dividends
    ``/v3/historical-price-full/stock_split/…``          splits
    ==================================================  ==========================
    """
    sym = symbol.upper()
    details: Dict[str, str] = {}
    windows = tuple(windows)

    is_etf = _resolve_is_etf(api_key, sym, details)

    # --- ETF block ------------------------------------------------------
    etf: Optional[EtfProfile] = None
    if is_etf:
        info_payload: Optional[dict] = None
        holdings_payload: Optional[List[dict]] = None
        extra: Dict[str, str] = {}
        try:
            info_payload = fetch_etf_info(api_key, sym)
        except Exception as e:
            logger.warning(f"symbol_info: etf-info failed for {sym}: {e}")
            extra["__info__"] = _reason(e)
        try:
            holdings_payload = fetch_etf_holdings(api_key, sym)
        except Exception as e:
            logger.warning(f"symbol_info: etf-holder failed for {sym}: {e}")
            extra["__holdings__"] = _reason(e)
        etf = parse_etf_profile(info_payload, holdings_payload)
        # Fold the fetch errors into the reasons, so "FMP has no such field" and
        # "we never got to ask" do not read the same.
        for marker, prefix in (("__info__", "FMP etf-info could not be fetched"),
                               ("__holdings__", "FMP etf-holder could not be fetched")):
            if marker not in extra:
                continue
            targets = (list(_ETF_INFO_KEYS) if marker == "__info__"
                       else ["holdings", "top10_weight_pct"])
            for name in targets:
                etf.details[name] = f"{prefix} ({extra[marker]}) — {name} is unknown"

    # --- prices / dividends / splits ------------------------------------
    # The LONGER of what the returns table needs and what the chart draws -- see
    # CHART_HISTORY_YEARS. Taking the max rather than the chart figure alone keeps a
    # caller that asks for narrower windows from silently losing chart history.
    years = max(_longest_window_years(windows), chart_years)
    series_start = years_before(as_of, years)
    fetch_start = date.fromordinal(series_start.toordinal() - PRICE_FETCH_BUFFER_DAYS)

    points: List[PricePoint] = []
    price_error = ""
    try:
        points = parse_price_history(fetch_price_history(api_key, sym, fetch_start, as_of))
    except Exception as e:
        logger.warning(f"symbol_info: price history failed for {sym}: {e}")
        price_error = (f"the FMP price history could not be fetched ({_reason(e)}) — "
                       f"no return is computable")

    dividends: Optional[List[DividendEvent]] = None
    try:
        dividends = parse_dividends(fetch_dividends(api_key, sym))
    except Exception as e:
        logger.warning(f"symbol_info: dividend history failed for {sym}: {e}")
        details["dividends"] = (
            f"the FMP dividend history could not be fetched ({_reason(e)})")

    splits: Optional[List[SplitEvent]] = None
    try:
        splits = parse_splits(fetch_splits(api_key, sym))
    except Exception as e:
        logger.warning(f"symbol_info: split history failed for {sym}: {e}")
        details["splits"] = f"the FMP split history could not be fetched ({_reason(e)})"

    # --- returns + series ------------------------------------------------
    returns: Dict[str, WindowReturn] = {}
    for w in windows:
        if price_error:
            returns[w] = WindowReturn(details={"*": price_error}, window=w)
        else:
            returns[w] = compute_window_return(w, points, dividends, splits, as_of)

    series = build_series(points, dividends, splits, start=series_start, end=as_of)
    if price_error:
        series.details["points"] = price_error

    last_point = points[-1] if points else None
    income = parse_income(_safe_ratios(api_key, sym, details), dividends,
                          last_point.close if last_point else None, as_of)

    return SymbolInfo(
        details=details, symbol=sym, as_of=as_of, is_etf=is_etf, etf=etf,
        income=income, returns=returns, series=series,
        history_start=points[0].date if points else None,
        last_close=last_point.close if last_point else None,
    )


def _safe_ratios(api_key: str, symbol: str, details: Dict[str, str]) -> Optional[dict]:
    """``fetch_ratios_ttm`` with the failure recorded rather than raised."""
    try:
        return fetch_ratios_ttm(api_key, symbol)
    except Exception as e:
        logger.warning(f"symbol_info: ratios-ttm failed for {symbol}: {e}")
        details["ratios"] = f"the FMP TTM ratios could not be fetched ({_reason(e)})"
        return None


def get_symbols_info(
    api_key: str,
    symbols: Sequence[str],
    *,
    as_of: date,
    windows: Sequence[str] = WINDOWS,
    chart_years: int = CHART_HISTORY_YEARS,
) -> Dict[str, SymbolInfo]:
    """:func:`get_symbol_info` for several symbols — the comparison view.

    Returns an ``{UPPER_SYMBOL: SymbolInfo}`` mapping in the caller's order, with
    duplicates collapsed. Every entry has the SAME shape, so the UI can lay them
    out in one table without per-symbol special cases.

    One symbol's failure never takes down the batch: an unexpected error is
    caught and returned as that symbol's unknown, so five good columns still
    render beside one "n/a" column.
    """
    out: Dict[str, SymbolInfo] = {}
    for raw in symbols:
        sym = raw.upper()
        if sym in out:
            continue
        try:
            out[sym] = get_symbol_info(api_key, sym, as_of=as_of, windows=windows,
                                       chart_years=chart_years)
        except Exception as e:      # defence in depth: get_symbol_info shouldn't raise
            logger.error(f"symbol_info: {sym} failed entirely: {e}", exc_info=True)
            reason = f"{sym} could not be loaded ({_reason(e)})"
            out[sym] = SymbolInfo(
                details={"*": reason}, symbol=sym, as_of=as_of,
                returns={w: WindowReturn(details={"*": reason}, window=w) for w in windows},
            )
    return out


# ---------------------------------------------------------------------------
# JSON view (for the UI / any transport)
# ---------------------------------------------------------------------------
def _d(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def symbol_info_to_dict(info: SymbolInfo) -> dict:
    """JSON-serializable view of a :class:`SymbolInfo`.

    Dates become ISO strings and every group's ``details`` map is carried
    through under ``"details"``, so an "n/a" in the UI can still explain itself.
    """
    return {
        "symbol": info.symbol,
        "as_of": _d(info.as_of),
        "is_etf": info.is_etf,
        "history_start": _d(info.history_start),
        "last_close": info.last_close,
        "details": dict(info.details),
        "etf": None if info.etf is None else {
            "holdings_count": info.etf.holdings_count,
            "top10_weight_pct": info.etf.top10_weight_pct,
            "asset_class": info.etf.asset_class,
            "category": info.etf.category,
            "assets_under_management": info.etf.assets_under_management,
            "pe_ratio": info.etf.pe_ratio,
            "expense_ratio": info.etf.expense_ratio,
            "nav": info.etf.nav,
            "inception_date": info.etf.inception_date,
            "etf_company": info.etf.etf_company,
            "holdings": [
                {"symbol": h.symbol, "name": h.name, "weight_pct": h.weight_pct,
                 "shares": h.shares, "market_value": h.market_value}
                for h in info.etf.holdings
            ],
            "details": dict(info.etf.details),
        },
        "income": {
            "dividend_yield_pct": info.income.dividend_yield_pct,
            "payout_ratio_pct": info.income.payout_ratio_pct,
            "trailing_12m_dividend_per_share": info.income.trailing_12m_dividend_per_share,
            "dividend_yield_pct_computed": info.income.dividend_yield_pct_computed,
            "details": dict(info.income.details),
        },
        "returns": {
            w: {
                "window": r.window,
                "total_return_pct": r.total_return_pct,
                "price_return_pct": r.price_return_pct,
                "dividends_paid_per_share": r.dividends_paid_per_share,
                "start_date": _d(r.start_date),
                "end_date": _d(r.end_date),
                "start_adj_close": r.start_adj_close,
                "end_adj_close": r.end_adj_close,
                "details": dict(r.details),
            }
            for w, r in info.returns.items()
        },
        "series": {
            "start": _d(info.series.start),
            "end": _d(info.series.end),
            "points": [
                {"date": _d(p.date), "close": p.close, "adj_close": p.adj_close,
                 "cumulative_total_return_pct": p.cumulative_total_return_pct}
                for p in info.series.points
            ],
            "dividends": [
                {"ex_date": _d(x.ex_date), "dividend": x.dividend,
                 "adj_dividend": x.adj_dividend, "chart_amount": x.chart_amount}
                for x in info.series.dividends
            ],
            "splits": [
                {"date": _d(s.date), "numerator": s.numerator,
                 "denominator": s.denominator, "ratio": s.ratio}
                for s in info.series.splits
            ],
            "details": dict(info.series.details),
        },
    }
