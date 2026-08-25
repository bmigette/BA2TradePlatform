"""Symbol-info panel: ETF holdings, dividends, total return and a chart.

The data layer is ``ba2_providers.symbol_info``; this module only DISPLAYS it.
Read that module's docstring first — the semantics below are its semantics.

The one rule
------------
**Unknown is not zero, and the data layer already says which is which.** Every
group it returns is an :class:`~ba2_providers.symbol_info.Unknowable` carrying
``why(field)``. So the panel renders exactly two things:

* a MEASURED value, *including a genuine* ``0.0`` — a non-payer's dividend yield
  really is 0.00%, and printing "n/a" there invents a missing fact;
* ``n/a`` **plus the reason**, for anything unknown — never ``0.00%``, never an
  empty cell, never a row quietly dropped from the list.

Both directions are live bugs, so the decision is made in ONE function,
:func:`field_cell`, and the formatters below refuse a ``None`` outright
(:func:`_require`) so the wrong direction cannot be written by accident.

Two tri-states this panel must not flatten:

* ``is_etf`` — ``False`` means "a stock, so it has no holdings", which is a
  FACT and renders as text with no tooltip. ``None`` means "we could not tell"
  and renders as ``n/a`` with the profile-fetch reason. See
  :func:`describe_is_etf`.
* a 3y/5y window on an 18-month-old symbol — the data layer returns ``None``
  with a reason naming the earliest price it has. That reason is shown; no 0%,
  and no since-inception figure quietly relabelled.

Layering
--------
Everything above :class:`SymbolInfoPanel` is PURE — no ``nicegui`` import is
needed to call it, and every display DECISION (formatting, unknown-vs-zero,
comparison ordering, chart series assembly) lives there so it can be unit
tested without a browser. ``SymbolInfoPanel`` is a thin renderer over it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ba2_providers.symbol_info import (
    TOP_N_HOLDINGS, WINDOWS, EtfProfile, SymbolInfo, Unknowable,
)

# ---------------------------------------------------------------------------
# The unknown vocabulary
# ---------------------------------------------------------------------------
#: What an unmeasurable value reads as. Never "0", never "-", never "".
UNKNOWN_TEXT = "n/a"

#: Shown when the data layer left a value ``None`` without filing a reason. An
#: n/a whose tooltip is empty is indistinguishable from a rendering bug, so
#: there is always SOMETHING to hover.
NO_REASON = "the data layer recorded no reason for this"

#: A question that does not apply to this instrument — a stock's top-10 weight.
#: Deliberately NOT :data:`UNKNOWN_TEXT`: "there is no such thing here" is a fact
#: about the instrument, while "n/a" here would read as a failed holdings fetch.
NOT_APPLICABLE_TEXT = "—"


@dataclass(frozen=True)
class Cell:
    """One rendered value, in exactly one of THREE states.

    ==================  ====================  ===========  =================
    state               text                  ``unknown``  explanation in
    ==================  ====================  ===========  =================
    measured            the value (``0.00%``) ``False``    ``note`` (rare)
    unknown             ``n/a``               ``True``     ``reason``
    not applicable      ``—``                 ``False``    ``note``
    ==================  ====================  ===========  =================

    ``reason`` is non-empty only when ``unknown``; ``note`` explains a measured
    or not-applicable cell without claiming anything is missing. Construct
    through the three classmethods, never by hand — they are what keep
    "``0.0`` is a fact", "``None`` is not ``0.0``" and "this does not apply"
    from collapsing into each other.
    """
    text: str
    reason: str = ""
    unknown: bool = False
    note: str = ""

    @classmethod
    def value(cls, text: str, note: str = "") -> "Cell":
        return cls(text=text, reason="", unknown=False, note=note)

    @classmethod
    def na(cls, reason: str) -> "Cell":
        return cls(text=UNKNOWN_TEXT, reason=reason or NO_REASON, unknown=True)

    @classmethod
    def not_applicable(cls, note: str) -> "Cell":
        """Not measurable because the question is meaningless here — not missing."""
        return cls(text=NOT_APPLICABLE_TEXT, reason="", unknown=False,
                   note=note or "this metric does not apply to this instrument")


def _require(value: Optional[Any]) -> Any:
    """Guard every formatter against a ``None``.

    Formatters render MEASURED values only. Handing one a ``None`` is the exact
    shape of the bug this panel exists to avoid (``f"{None or 0:.2f}%"`` ->
    ``0.00%``), so it raises rather than inventing a zero. Unknowns go through
    :meth:`Cell.na`.
    """
    if value is None:
        raise ValueError(
            "symbol_info_panel formatters render measured values only — an unknown "
            "must go through Cell.na(reason), not through a formatter")
    return value


def fmt_pct(value: Optional[float]) -> str:
    """Unsigned percent: ``61.25`` -> ``61.25%``, ``0.0`` -> ``0.00%``."""
    return f"{float(_require(value)):.2f}%"


def fmt_signed_pct(value: Optional[float]) -> str:
    """Signed percent for returns: ``26.5`` -> ``+26.50%``, ``-13.0`` -> ``-13.00%``."""
    return f"{float(_require(value)):+.2f}%"


def fmt_money(value: Optional[float]) -> str:
    return f"${float(_require(value)):,.2f}"


def fmt_per_share(value: Optional[float]) -> str:
    """Cash per share — four decimals, because dividends live in the sub-cent."""
    return f"${float(_require(value)):,.4f}"


#: Thresholds for :func:`fmt_compact_money`, largest first.
_COMPACT_UNITS: Tuple[Tuple[float, str], ...] = (
    (1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"),
)


def fmt_compact_money(value: Optional[float]) -> str:
    """AUM-style short form: ``119_630_000_000`` -> ``119.63B``.

    No currency prefix: FMP does not tell us the fund's reporting currency, and
    stamping ``$`` on a EUR-denominated UCITS would be a wrong fact rather than
    a missing one.
    """
    number = float(_require(value))
    for threshold, suffix in _COMPACT_UNITS:
        if abs(number) >= threshold:
            return f"{number / threshold:,.2f}{suffix}"
    return f"{number:,.2f}"


def fmt_count(value: Optional[int]) -> str:
    return f"{int(_require(value)):,}"


def fmt_date(value: Optional[date]) -> str:
    return _require(value).isoformat()


def fmt_text(value: Optional[str]) -> str:
    return str(_require(value))


def fmt_ratio(value: Optional[float]) -> str:
    """A plain ratio with no unit: a P/E of ``39.32`` -> ``39.32``. No ``%``."""
    return f"{float(_require(value)):,.2f}"


def fmt_raw_number(value: Optional[float]) -> str:
    """A number whose UNIT the data layer does not pin down, printed verbatim.

    Used for the ETF expense ratio (see :data:`LABEL_EXPENSE`): FMP's
    ``expenseRatio`` may be a fraction or already a percent, and ``:,.2f`` would
    round a genuine ``0.0008`` to ``0.00`` — turning a measured value into a
    plausible zero, the exact failure this panel is built to avoid.
    """
    return f"{float(_require(value)):g}"


def field_cell(group: Unknowable, field_name: str, value: Optional[Any],
               formatter: Callable[[Any], str], *, fallback_reason: str = "") -> Cell:
    """THE unknown-vs-measured decision. Everything displayed goes through here.

    The gate is ``value is None`` — deliberately not ``if not value``, which
    would turn a genuine ``0.0`` (a non-payer's yield, a flat window's return)
    into an n/a. When it IS ``None`` the reason comes from the owning group's
    ``why()``, which already falls back to the group-wide ``"*"`` entry.

    ``fallback_reason`` covers the fields the data layer leaves reasonless —
    ``SymbolInfo.last_close`` and ``history_start`` are ``None`` when the price
    fetch failed, but the reason is filed on ``series.details['points']``.
    """
    if value is None:
        return Cell.na(group.why(field_name) or fallback_reason)
    return Cell.value(formatter(value))


# ---------------------------------------------------------------------------
# is_etf — tri-state
# ---------------------------------------------------------------------------
IS_ETF_YES_TEXT = "ETF"
#: NOT an n/a. "This is a stock" is a measured fact, and the absence of holdings
#: follows from it; rendering it the same as "we could not tell" would erase the
#: distinction the data layer went to the trouble of preserving.
IS_ETF_NO_TEXT = "Stock — not an ETF, so it has no holdings"


def describe_is_etf(info: SymbolInfo) -> Cell:
    """The tri-state, kept tri-state. ``None`` is the only unknown of the three."""
    if info.is_etf is None:
        return Cell.na(info.why("is_etf"))
    return Cell.value(IS_ETF_YES_TEXT if info.is_etf else IS_ETF_NO_TEXT)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
LABEL_TYPE = "Type"
LABEL_LAST_CLOSE = "Last close"
LABEL_DIV_YIELD = "Dividend yield (TTM)"
LABEL_DIV_YIELD_COMPUTED = "Dividend yield (computed)"
LABEL_PAYOUT = "Payout ratio (TTM)"
LABEL_TTM_DIV = "Trailing 12m dividend / share"
LABEL_HISTORY_START = "Price history starts"


def price_failure_reason(info: SymbolInfo) -> str:
    """Why there is no price data, when the panel needs it for a reasonless field.

    ``get_symbol_info`` files a price-fetch failure on ``series.details["points"]``
    and on every ``WindowReturn``, but leaves ``last_close`` / ``history_start``
    as bare ``None``s on ``SymbolInfo`` itself. Reaching one attribute across for
    the reason beats printing "no reason recorded" when the reason exists.
    """
    return info.series.why("points")


def build_overview_rows(info: SymbolInfo) -> List[Tuple[str, Cell]]:
    """``[(label, Cell), ...]`` for the overview block.

    Every label is always present, in a fixed order, whatever failed: a row that
    disappears when its value cannot be measured is a silently missing fact.
    """
    income = info.income
    price_reason = price_failure_reason(info)
    return [
        (LABEL_TYPE, describe_is_etf(info)),
        (LABEL_LAST_CLOSE, field_cell(info, "last_close", info.last_close, fmt_money,
                                      fallback_reason=price_reason)),
        (LABEL_DIV_YIELD, field_cell(income, "dividend_yield_pct",
                                     income.dividend_yield_pct, fmt_pct)),
        (LABEL_DIV_YIELD_COMPUTED, field_cell(income, "dividend_yield_pct_computed",
                                              income.dividend_yield_pct_computed, fmt_pct)),
        (LABEL_PAYOUT, field_cell(income, "payout_ratio_pct",
                                  income.payout_ratio_pct, fmt_pct)),
        (LABEL_TTM_DIV, field_cell(income, "trailing_12m_dividend_per_share",
                                   income.trailing_12m_dividend_per_share, fmt_per_share)),
        (LABEL_HISTORY_START, field_cell(info, "history_start", info.history_start,
                                         fmt_date, fallback_reason=price_reason)),
    ]


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
#: The order the panel shows windows in, and the heading each one gets. The
#: data layer returns a Mapping whose order is whatever the CALLER asked for;
#: the reader always wants shortest-first, so the panel imposes its own.
WINDOW_LABELS: Dict[str, str] = {"ytd": "YTD", "1y": "1Y", "3y": "3Y", "5y": "5Y"}

#: Filed against a window the data layer was never asked for. Still rendered:
#: a row that vanishes reads as "nothing to report", which is a different claim.
_WINDOW_NOT_FETCHED = "this window was not requested from the data layer"


@dataclass(frozen=True)
class ReturnRow:
    """One window's row. ``total`` INCLUDES reinvested dividends; ``dividends``
    is the cash paid over the window and is NOT part of ``total``."""
    window: str
    label: str
    total: Cell
    price: Cell
    dividends: Cell
    period: Cell


def _period_cell(ret) -> Cell:
    if ret.start_date is None or ret.end_date is None:
        missing = "start_date" if ret.start_date is None else "end_date"
        return Cell.na(ret.why(missing) or ret.why("*"))
    return Cell.value(f"{ret.start_date.isoformat()} → {ret.end_date.isoformat()}")


def build_returns_rows(info: SymbolInfo,
                       windows: Sequence[str] = WINDOWS) -> List[ReturnRow]:
    """One :class:`ReturnRow` per window, in :data:`WINDOW_LABELS` order.

    A window the data layer did not return still gets a row, marked unknown —
    the alternative is a table that silently shrinks when a fetch fails.
    """
    ordered = [w for w in WINDOW_LABELS if w in windows]
    ordered += [w for w in windows if w not in WINDOW_LABELS]
    rows: List[ReturnRow] = []
    for w in ordered:
        ret = info.returns.get(w)
        if ret is None:
            missing = Cell.na(f"{WINDOW_LABELS.get(w, w)}: {_WINDOW_NOT_FETCHED}")
            rows.append(ReturnRow(window=w, label=WINDOW_LABELS.get(w, w.upper()),
                                  total=missing, price=missing, dividends=missing,
                                  period=missing))
            continue
        rows.append(ReturnRow(
            window=w,
            label=WINDOW_LABELS.get(w, w.upper()),
            total=field_cell(ret, "total_return_pct", ret.total_return_pct, fmt_signed_pct),
            price=field_cell(ret, "price_return_pct", ret.price_return_pct, fmt_signed_pct),
            dividends=field_cell(ret, "dividends_paid_per_share",
                                 ret.dividends_paid_per_share, fmt_per_share),
            period=_period_cell(ret),
        ))
    return rows


# ---------------------------------------------------------------------------
# The ETF section
# ---------------------------------------------------------------------------
LABEL_HOLDINGS_COUNT = "Holdings"
LABEL_TOP10 = f"Top {TOP_N_HOLDINGS} weight"
LABEL_ASSET_CLASS = "Asset class"
LABEL_CATEGORY = "Category"
LABEL_AUM = "Assets under management"
LABEL_PE = "P/E ratio"
#: FMP does not document whether ``etf-info``'s ``expenseRatio`` is a FRACTION
#: (0.0008) or already a PERCENT (0.08), and the data layer passes it through
#: untouched. Multiplying by 100 on a guess would turn a missing unit into a
#: wrong number, so it is shown verbatim and the label says so.
LABEL_EXPENSE = "Expense ratio (as FMP reports it)"
LABEL_NAV = "NAV"
LABEL_INCEPTION = "Inception"
LABEL_ISSUER = "Issuer"

#: The ETF rows, in display order: (label, EtfProfile attribute, formatter).
_ETF_ROW_SPECS: Tuple[Tuple[str, str, Callable[[Any], str]], ...] = (
    (LABEL_HOLDINGS_COUNT, "holdings_count", fmt_count),
    (LABEL_TOP10, "top10_weight_pct", fmt_pct),
    (LABEL_ASSET_CLASS, "asset_class", fmt_text),
    (LABEL_CATEGORY, "category", fmt_text),
    (LABEL_AUM, "assets_under_management", fmt_compact_money),
    (LABEL_PE, "pe_ratio", fmt_ratio),
    (LABEL_EXPENSE, "expense_ratio", fmt_raw_number),
    (LABEL_NAV, "nav", fmt_money),
    (LABEL_INCEPTION, "inception_date", fmt_text),
    (LABEL_ISSUER, "etf_company", fmt_text),
)

#: ``is_etf`` tri-state, as a section state.
ETF_STATE_ETF = "etf"
ETF_STATE_STOCK = "stock"
ETF_STATE_UNKNOWN = "unknown"

#: A stock's empty holdings list is a FACT, so it is a value, not an n/a.
STOCK_HOLDINGS_NOTE = "A stock has no holdings."
#: FMP answered and listed nothing. Also a fact — distinct from a failed fetch.
NO_CONSTITUENTS_NOTE = "FMP reported no constituents for this fund."


@dataclass(frozen=True)
class HoldingRow:
    rank: int
    symbol: Cell
    name: Cell
    weight: Cell


@dataclass(frozen=True)
class EtfSection:
    """The ETF block. ``state`` mirrors ``is_etf``'s tri-state exactly."""
    state: str
    status: Cell
    rows: List[Tuple[str, Cell]] = field(default_factory=list)
    holdings: List[HoldingRow] = field(default_factory=list)
    holdings_note: Cell = Cell.value("")


#: ``EtfHolding`` is a plain record — it is NOT an ``Unknowable`` and carries no
#: ``details``. This stand-in lets a holding's fields go through the SAME
#: ``field_cell`` decision as everything else, with a per-row reason supplied by
#: :func:`_holding_reason` instead of by a ``details`` map.
_NO_DETAILS = Unknowable()


def _holding_reason(holding, field_name: str) -> str:
    who = holding.symbol or holding.name or "this constituent"
    return (f"FMP's etf-holder row for {who} carried no {field_name} — it is unknown, "
            f"not zero")


def build_holding_rows(etf: EtfProfile,
                       limit: int = TOP_N_HOLDINGS) -> List[HoldingRow]:
    """The top ``limit`` constituents, in the data layer's own weight order.

    A row whose weight FMP omitted is KEPT and marked n/a: dropping it would
    shrink the fund, and a ``0.00%`` would invent a holding that owns nothing.
    """
    rows: List[HoldingRow] = []
    for index, holding in enumerate(etf.holdings[:limit], start=1):
        rows.append(HoldingRow(
            rank=index,
            symbol=field_cell(_NO_DETAILS, "symbol", holding.symbol, fmt_text,
                              fallback_reason=_holding_reason(holding, "symbol")),
            name=field_cell(_NO_DETAILS, "name", holding.name, fmt_text,
                            fallback_reason=_holding_reason(holding, "name")),
            weight=field_cell(_NO_DETAILS, "weight_pct", holding.weight_pct, fmt_pct,
                              fallback_reason=_holding_reason(holding, "weight")),
        ))
    return rows


def _holdings_note(etf: EtfProfile, rows: List[HoldingRow]) -> Cell:
    """"Could not ask" vs "asked, and the fund lists nothing" — two facts, two texts."""
    if etf.is_unknown("holdings"):
        return Cell.na(etf.why("holdings"))
    if not etf.holdings:
        return Cell.value(NO_CONSTITUENTS_NOTE)
    shown, total = len(rows), len(etf.holdings)
    return Cell.value(f"Top {shown} of {total} constituents returned by FMP")


def build_etf_section(info: SymbolInfo) -> EtfSection:
    """The ETF block, in one of THREE shapes — one per ``is_etf`` state.

    * ``True``  — the real thing: scalar rows plus the top constituents.
    * ``False`` — **not applicable**. A stock having no holdings is a fact, so
      there are no rows to mark unknown and the note is a plain statement.
    * ``None``  — **unknown**. Every row is still listed, each an n/a carrying
      the profile-fetch reason, because we do NOT know there are no holdings.
    """
    status = describe_is_etf(info)

    if info.is_etf is False:
        return EtfSection(state=ETF_STATE_STOCK, status=status, rows=[], holdings=[],
                          holdings_note=Cell.value(STOCK_HOLDINGS_NOTE))

    if info.is_etf is None or info.etf is None:
        reason = info.why("etf") or info.why("is_etf")
        return EtfSection(
            state=ETF_STATE_UNKNOWN, status=status,
            rows=[(label, Cell.na(reason)) for label, _, _ in _ETF_ROW_SPECS],
            holdings=[], holdings_note=Cell.na(reason))

    etf = info.etf
    rows = [(label, field_cell(etf, attr, getattr(etf, attr), formatter))
            for label, attr, formatter in _ETF_ROW_SPECS]
    holdings = build_holding_rows(etf)
    return EtfSection(state=ETF_STATE_ETF, status=status, rows=rows,
                      holdings=holdings, holdings_note=_holdings_note(etf, holdings))


# ---------------------------------------------------------------------------
# The chart
# ---------------------------------------------------------------------------
# THREE SERIES, THREE QUANTITIES, THREE AXES.
#
# The data layer's docstring is emphatic that the dividend bars and the
# cumulative total-return line are different quantities and must never be added
# together. The chart enforces that structurally rather than by convention:
#
#   * the PRICE line   — dollars per share, from ``close``      -> left axis
#   * the RETURN line  — percent, reinvested, from ``adj_close`` -> right axis
#   * the DIVIDEND bars — dollars of CASH PAID per share, at the
#     ex-date, from ``chart_amount``                             -> far-right axis
#
# Nothing can silently add the bars into the line because they are a different
# series TYPE on a different AXIS with a different UNIT, and the return series'
# data is copied verbatim out of ``SeriesPoint.cumulative_total_return_pct``.
KIND_PRICE = "price"
KIND_RETURN = "total_return"
KIND_DIVIDEND = "dividend"

AXIS_NAME_PRICE = "Price (close)"
AXIS_NAME_RETURN = "Total return % (reinvested)"
AXIS_NAME_DIVIDEND = "Dividend paid (cash / share)"

#: One colour per role in the single-symbol chart, and a rotation for comparisons.
COLOR_PRICE = "#4dabf7"
COLOR_RETURN = "#00d4aa"
COLOR_DIVIDEND = "#ffa94d"
COMPARE_COLORS: Tuple[str, ...] = (
    "#00d4aa", "#4dabf7", "#ffa94d", "#ff6b6b", "#9775fa",
    "#69db7c", "#ffd43b", "#74c0fc",
)


def _iso(day: date) -> str:
    return day.isoformat()


def _category_dates(*bundles) -> List[str]:
    """The x axis: the UNION of every bar date and every ex-date, ascending.

    A union rather than the price dates alone, because an ex-date with no price
    bar (a data gap, a holiday quirk) would otherwise drop that dividend off the
    chart entirely — understating the payout history to make the axis tidy.
    """
    days = set()
    for bundle in bundles:
        days.update(p.date for p in bundle.points)
        days.update(d.ex_date for d in bundle.dividends)
    return [_iso(d) for d in sorted(days)]


def _aligned(mapping: Mapping[str, Optional[float]],
             categories: Sequence[str]) -> List[Optional[float]]:
    """``None`` where the series has no observation — never ``0.0``.

    ECharts renders ``None`` as a gap and ``0`` as a data point on the zero
    line; the second would claim a measurement that was never made.
    """
    return [mapping.get(c) for c in categories]


def _split_marklines(bundle) -> Dict[str, Any]:
    """Split annotations for the price line. Empty when the splits are unknown —
    an invented annotation is worse than none, and :func:`build_chart_notes`
    says so out loud instead."""
    data = []
    for split in bundle.splits:
        ratio = (f"{split.ratio:g}-for-1 split" if split.ratio is not None
                 else "split (ratio unknown)")
        data.append({"xAxis": _iso(split.date), "label": {"formatter": ratio}})
    return {"symbol": "none", "silent": True, "data": data}


def _price_series(info, categories):
    bundle = info.series
    closes = {_iso(p.date): p.close for p in bundle.points}
    return {
        "_kind": KIND_PRICE, "_symbol": info.symbol,
        "name": f"{info.symbol} price (close)",
        "type": "line", "yAxisIndex": 0, "showSymbol": False, "connectNulls": True,
        "smooth": False, "lineStyle": {"width": 2}, "color": COLOR_PRICE,
        "data": _aligned(closes, categories),
        "markLine": _split_marklines(bundle),
    }


def _return_series(info, categories, *, axis_index: int, color: str):
    """The reinvested line, copied VERBATIM from the data layer.

    No arithmetic happens here on purpose. ``cumulative_total_return_pct`` is
    already derived from ``adj_close`` alone; adding the paid dividends to it —
    the plausible-looking mistake the data layer's docstring devotes a section
    to — would double count them.
    """
    cumulative = {_iso(p.date): p.cumulative_total_return_pct for p in info.series.points}
    return {
        "_kind": KIND_RETURN, "_symbol": info.symbol,
        "name": f"{info.symbol} total return (reinvested)",
        "type": "line", "yAxisIndex": axis_index, "showSymbol": False,
        "connectNulls": True, "lineStyle": {"width": 2}, "color": color,
        "data": _aligned(cumulative, categories),
    }


def _dividend_series(info, categories):
    """Discrete cash paid per share, at the ex-date. A BAR, never a line: a line
    would imply the payment exists on the days between ex-dates."""
    amounts = {_iso(d.ex_date): d.chart_amount for d in info.series.dividends}
    return {
        "_kind": KIND_DIVIDEND, "_symbol": info.symbol,
        "name": f"{info.symbol} dividend paid (cash / share)",
        "type": "bar", "yAxisIndex": 2, "barMaxWidth": 8, "color": COLOR_DIVIDEND,
        "data": _aligned(amounts, categories),
    }


def _axis(name: str, *, position: str, offset: int = 0,
          formatter: str = "{value}", split_line: bool = True) -> Dict[str, Any]:
    return {
        "type": "value", "name": name, "position": position, "offset": offset,
        "scale": True, "nameTextStyle": {"color": "#a0aec0"},
        "axisLabel": {"color": "#a0aec0", "formatter": formatter},
        "splitLine": {"show": split_line,
                      "lineStyle": {"color": "rgba(255, 255, 255, 0.05)"}},
    }


def build_single_chart_options(info: SymbolInfo) -> Dict[str, Any]:
    """The one-symbol chart: price line + reinvested line + dividend bars."""
    categories = _category_dates(info.series)
    series = [
        _price_series(info, categories),
        _return_series(info, categories, axis_index=1, color=COLOR_RETURN),
        _dividend_series(info, categories),
    ]
    return {
        "backgroundColor": "transparent",
        "tooltip": {"trigger": "axis",
                    "backgroundColor": "rgba(37, 43, 59, 0.95)",
                    "borderColor": "rgba(255, 255, 255, 0.1)",
                    "textStyle": {"color": "#ffffff"}},
        "legend": {"data": [s["name"] for s in series],
                   "textStyle": {"color": "#a0aec0"}},
        "grid": {"left": 60, "right": 130, "top": 60, "bottom": 50,
                 "containLabel": True},
        "xAxis": {"type": "category", "data": categories,
                  "axisLabel": {"color": "#a0aec0"},
                  "axisLine": {"lineStyle": {"color": "rgba(255, 255, 255, 0.1)"}}},
        "yAxis": [
            _axis(AXIS_NAME_PRICE, position="left", formatter="${value}"),
            _axis(AXIS_NAME_RETURN, position="right", formatter="{value}%",
                  split_line=False),
            # Zero-based and gridless: bars measure a magnitude from zero, and a
            # third set of grid lines would be unreadable.
            {**_axis(AXIS_NAME_DIVIDEND, position="right", offset=70,
                     formatter="${value}", split_line=False),
             "scale": False, "min": 0},
        ],
        "series": series,
    }


def build_comparison_chart_options(infos: Sequence[SymbolInfo]) -> Dict[str, Any]:
    """Several symbols: one reinvested total-return line each, one shared % axis.

    Prices are not overlaid — a $282 ETF and a $6 stock on one axis compares
    nothing. Total return in percent is the quantity that IS comparable, and it
    is the only one drawn here. A symbol that failed keeps its (all-``None``)
    line so it stays in the legend rather than disappearing from the question.
    """
    categories = _category_dates(*[i.series for i in infos])
    series = [
        _return_series(info, categories, axis_index=0,
                       color=COMPARE_COLORS[index % len(COMPARE_COLORS)])
        for index, info in enumerate(infos)
    ]
    return {
        "backgroundColor": "transparent",
        "tooltip": {"trigger": "axis",
                    "backgroundColor": "rgba(37, 43, 59, 0.95)",
                    "borderColor": "rgba(255, 255, 255, 0.1)",
                    "textStyle": {"color": "#ffffff"}},
        "legend": {"data": [s["name"] for s in series],
                   "textStyle": {"color": "#a0aec0"}},
        "grid": {"left": 60, "right": 40, "top": 60, "bottom": 50,
                 "containLabel": True},
        "xAxis": {"type": "category", "data": categories,
                  "axisLabel": {"color": "#a0aec0"},
                  "axisLine": {"lineStyle": {"color": "rgba(255, 255, 255, 0.1)"}}},
        "yAxis": [_axis(AXIS_NAME_RETURN, position="left", formatter="{value}%")],
        "series": series,
    }


def build_chart_options(infos: Sequence[SymbolInfo]) -> Dict[str, Any]:
    """Dispatch: one symbol gets the three-series chart, several get the overlay."""
    infos = list(infos)
    if len(infos) == 1:
        return build_single_chart_options(infos[0])
    return build_comparison_chart_options(infos)


#: The chart series that can come back "empty WITH a reason", and what to call
#: them when they do. An empty bar chart reads as "pays nothing"; an unannotated
#: price line reads as "no split" — both are claims we may not be able to make.
_SERIES_NOTE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("points", "price / total-return line"),
    ("dividends", "dividend bars"),
    ("splits", "split annotations"),
)


def build_chart_notes(infos: Sequence[SymbolInfo]) -> List[Cell]:
    """One n/a note per chart series that is UNKNOWN rather than genuinely empty.

    Nothing is emitted for a symbol that simply never paid a dividend or never
    split: that is a measured, drawable nothing.
    """
    notes: List[Cell] = []
    for info in infos:
        for field_name, description in _SERIES_NOTE_FIELDS:
            if info.series.is_unknown(field_name):
                notes.append(Cell(text=f"{info.symbol} {description}: {UNKNOWN_TEXT}",
                                  reason=info.series.why(field_name), unknown=True))
    return notes


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
#: Filed when a symbol the caller asked for is absent from the batch entirely.
#: ``get_symbols_info`` promises one entry per symbol precisely so this cannot
#: happen, but a dropped column is invisible to the reader — who simply concludes
#: they asked for fewer symbols — so the UI checks rather than trusts.
_MISSING_FROM_BATCH = ("{symbol}: the data layer returned no entry for this symbol — "
                       "it was requested but never came back")


@dataclass(frozen=True)
class ComparisonColumn:
    """One symbol's column. ``failed`` marks a symbol the batch could not load —
    it is still a COLUMN, just an empty one, so the reader sees what they asked."""
    symbol: str
    failed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ComparisonTable:
    columns: List[ComparisonColumn] = field(default_factory=list)
    rows: List[Tuple[str, List[Cell]]] = field(default_factory=list)


def failure_reason(info: SymbolInfo) -> str:
    """The whole-symbol failure reason, or ``""``.

    Reads ``details["*"]`` DIRECTLY rather than ``why("*")``: a per-field reason
    (a missing payout ratio, say) must not promote an otherwise healthy symbol to
    "failed".
    """
    return info.details.get("*", "")


def _requested_order(symbols: Sequence[str]) -> List[str]:
    """Upper-cased, de-duplicated, in the caller's order — matching the data
    layer's own contract for ``get_symbols_info``."""
    seen: List[str] = []
    for raw in symbols:
        sym = raw.upper()
        if sym not in seen:
            seen.append(sym)
    return seen


#: ``(label, extractor)``. Each extractor takes a ``SymbolInfo`` and returns a
#: :class:`Cell`, so a row is just "this metric, for every column".
def _comparison_specs() -> List[Tuple[str, Callable[[SymbolInfo], Cell]]]:
    def overview(label: str) -> Callable[[SymbolInfo], Cell]:
        return lambda info: dict(build_overview_rows(info))[label]

    def total_return(window: str) -> Callable[[SymbolInfo], Cell]:
        def get(info: SymbolInfo) -> Cell:
            rows = {row.window: row for row in build_returns_rows(info)}
            return rows[window].total
        return get

    def etf_row(label: str) -> Callable[[SymbolInfo], Cell]:
        def get(info: SymbolInfo) -> Cell:
            section = build_etf_section(info)
            if section.state == ETF_STATE_STOCK:
                # A stock has no holdings — a FACT, not a gap. See Cell.not_applicable.
                return Cell.not_applicable(
                    f"{info.symbol} is a stock, so {label.lower()} does not apply")
            return dict(section.rows)[label]
        return get

    specs: List[Tuple[str, Callable[[SymbolInfo], Cell]]] = [
        (LABEL_TYPE, overview(LABEL_TYPE)),
        (LABEL_LAST_CLOSE, overview(LABEL_LAST_CLOSE)),
    ]
    specs += [(f"{WINDOW_LABELS[w]} total return", total_return(w)) for w in WINDOW_LABELS]
    specs += [
        (LABEL_DIV_YIELD, overview(LABEL_DIV_YIELD)),
        (LABEL_PAYOUT, overview(LABEL_PAYOUT)),
        (LABEL_TTM_DIV, overview(LABEL_TTM_DIV)),
        (LABEL_HISTORY_START, overview(LABEL_HISTORY_START)),
        (LABEL_HOLDINGS_COUNT, etf_row(LABEL_HOLDINGS_COUNT)),
        (LABEL_TOP10, etf_row(LABEL_TOP10)),
        (LABEL_ASSET_CLASS, etf_row(LABEL_ASSET_CLASS)),
        (LABEL_CATEGORY, etf_row(LABEL_CATEGORY)),
        (LABEL_AUM, etf_row(LABEL_AUM)),
        (LABEL_EXPENSE, etf_row(LABEL_EXPENSE)),
    ]
    return specs


def build_comparison(infos: Mapping[str, SymbolInfo],
                     symbols: Sequence[str]) -> ComparisonTable:
    """One column per REQUESTED symbol, one row per metric.

    Column order is the caller's, not the mapping's. Crucially, a symbol that
    failed — whether the data layer flagged it with a ``"*"`` detail or never
    returned it at all — still gets a full column of n/a cells carrying the
    failure reason. It appears in the comparison **as failed**, which is the
    answer to the question that was asked; dropping it silently changes the
    question.
    """
    order = _requested_order(symbols)
    columns: List[ComparisonColumn] = []
    resolved: List[Tuple[Optional[SymbolInfo], str]] = []

    for sym in order:
        info = infos.get(sym)
        if info is None:
            reason = _MISSING_FROM_BATCH.format(symbol=sym)
            columns.append(ComparisonColumn(symbol=sym, failed=True, reason=reason))
            resolved.append((None, reason))
            continue
        reason = failure_reason(info)
        columns.append(ComparisonColumn(symbol=sym, failed=bool(reason), reason=reason))
        resolved.append((info, reason))

    rows: List[Tuple[str, List[Cell]]] = []
    for label, extract in _comparison_specs():
        cells: List[Cell] = []
        for info, reason in resolved:
            if info is None:
                cells.append(Cell.na(reason))
            else:
                cells.append(extract(info))
        rows.append((label, cells))
    return ComparisonTable(columns=columns, rows=rows)


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------
# Everything above this line is pure and importable without nicegui. Below is a
# thin renderer that makes no decisions of its own: it walks the Cells the pure
# layer produced and draws them.
import asyncio                                            # noqa: E402

from nicegui import ui                                    # noqa: E402

from ba2_providers.symbol_info import get_symbols_info    # noqa: E402

from ...logger import logger                              # noqa: E402

#: Styling for the three Cell states. The unknown one is deliberately loud: an
#: n/a that looks like a value is how a reader mistakes it for one.
_VALUE_CLASSES = "text-sm"
_UNKNOWN_CLASSES = "text-sm italic"
_UNKNOWN_STYLE = "color: #ffd93d;"
_MUTED = "color: #a0aec0;"


def render_cell(cell: Cell) -> None:
    """Draw one :class:`Cell`, with its explanation attached.

    An unknown gets the warning colour AND a hoverable reason; a measured or
    not-applicable cell gets a tooltip only when it actually has a note. The
    reason is never dropped — an "n/a" the reader cannot interrogate is only
    marginally better than a blank cell.
    """
    if cell.unknown:
        label = ui.label(cell.text).classes(_UNKNOWN_CLASSES).style(_UNKNOWN_STYLE)
        label.tooltip(cell.reason)
        return
    label = ui.label(cell.text).classes(_VALUE_CLASSES)
    if cell.note:
        label.tooltip(cell.note)


def render_label_value_rows(rows: Sequence[Tuple[str, Cell]]) -> None:
    with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-1"):
        for label, cell in rows:
            ui.label(label).classes("text-sm").style(_MUTED)
            render_cell(cell)


def render_overview(info: SymbolInfo) -> None:
    with ui.card().classes("w-full"):
        ui.label(f"{info.symbol} — overview").classes("text-base font-bold")
        render_label_value_rows(build_overview_rows(info))


def render_etf_section(info: SymbolInfo) -> None:
    """The ETF block in whichever of its three shapes applies.

    A STOCK gets one sentence and no table — there is nothing missing to report.
    An UNDETERMINED symbol gets the full row list, every entry an n/a, because we
    do not know that its holdings are absent.
    """
    section = build_etf_section(info)
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.label(f"{info.symbol} — ETF profile").classes("text-base font-bold")
            render_cell(section.status)
        if section.rows:
            render_label_value_rows(section.rows)
        with ui.row().classes("items-center gap-2 mt-2"):
            render_cell(section.holdings_note)
        if not section.holdings:
            return
        with ui.grid(columns=4).classes("w-full gap-x-4 gap-y-1 mt-2"):
            for heading in ("#", "Symbol", "Name", "Weight"):
                ui.label(heading).classes("text-xs font-bold").style(_MUTED)
            for row in section.holdings:
                ui.label(str(row.rank)).classes("text-sm").style(_MUTED)
                render_cell(row.symbol)
                render_cell(row.name)
                render_cell(row.weight)


def render_returns(info: SymbolInfo) -> None:
    with ui.card().classes("w-full"):
        ui.label(f"{info.symbol} — total return").classes("text-base font-bold")
        ui.label("Total return includes reinvested dividends. The dividends column is "
                 "cash PAID per share over the window and is NOT added to it.").classes(
            "text-xs").style(_MUTED)
        with ui.grid(columns=5).classes("w-full gap-x-4 gap-y-1 mt-2"):
            for heading in ("Window", "Total return", "Price only", "Dividends paid",
                            "Period"):
                ui.label(heading).classes("text-xs font-bold").style(_MUTED)
            for row in build_returns_rows(info):
                ui.label(row.label).classes("text-sm font-medium")
                render_cell(row.total)
                render_cell(row.price)
                render_cell(row.dividends)
                render_cell(row.period)


def render_chart(infos: Sequence[SymbolInfo]) -> None:
    infos = list(infos)
    if not infos:
        return
    with ui.card().classes("w-full"):
        title = (f"{infos[0].symbol} — price, dividends and total return"
                 if len(infos) == 1 else "Total return comparison")
        ui.label(title).classes("text-base font-bold")
        ui.echart(build_chart_options(infos)).style("width: 100%; height: 380px;")
        for note in build_chart_notes(infos):
            render_cell(note)


def render_comparison(infos: Mapping[str, SymbolInfo], symbols: Sequence[str]) -> None:
    """The side-by-side table. Every requested symbol gets a column, including
    the ones that failed — see :func:`build_comparison`."""
    table = build_comparison(infos, symbols)
    with ui.card().classes("w-full"):
        ui.label("Comparison").classes("text-base font-bold")
        with ui.grid(columns=len(table.columns) + 1).classes("w-full gap-x-4 gap-y-1"):
            ui.label("").classes("text-xs")
            for column in table.columns:
                heading = ui.label(column.symbol).classes("text-xs font-bold")
                if column.failed:
                    heading.style(_UNKNOWN_STYLE)
                    heading.tooltip(column.reason)
            for label, cells in table.rows:
                ui.label(label).classes("text-sm").style(_MUTED)
                for cell in cells:
                    render_cell(cell)


class SymbolInfoPanel:
    """A dialog showing one symbol in full, or several side by side.

    The dialog is built empty and filled by :meth:`load`, which does the blocking
    data-layer call on a worker thread (``asyncio.to_thread``) so fetching six
    symbols does not freeze the browser. Re-loading rebuilds the body with
    ``container.clear()`` — no ``ui.refreshable``.
    """

    def __init__(self, symbols: Sequence[str], *, api_key: str, as_of: date,
                 windows: Sequence[str] = WINDOWS,
                 fetch: Optional[Callable[..., Mapping[str, SymbolInfo]]] = None):
        self.symbols = _requested_order(symbols)
        self.api_key = api_key
        self.as_of = as_of
        self.windows = tuple(windows)
        #: Injectable ONLY so the display can be tested without a network. The
        #: default is the real batch entry point.
        self._fetch = fetch or get_symbols_info
        self.load_task: Optional["asyncio.Task"] = None
        self.body = None
        self.dialog = None
        self._build()

    def _build(self) -> None:
        self.dialog = ui.dialog().props(
            'full-width maximized transition-show="slide-up" '
            'transition-hide="slide-down"')
        with self.dialog:
            with ui.card().classes("w-full h-full flex flex-col").style(
                    "max-width: 100%; background: #1a1f2e;"):
                with ui.row().classes("w-full items-center justify-between p-2"):
                    ui.label(f"Symbol info — {', '.join(self.symbols)}").classes(
                        "text-lg font-bold")
                    ui.button(icon="close", on_click=self.close).props("flat round")
                with ui.scroll_area().classes("flex-grow w-full"):
                    self.body = ui.column().classes("w-full gap-3 p-2")
        with self.body:
            ui.spinner(size="lg")
            ui.label("Loading…").style(_MUTED)

    def open(self) -> None:
        self.dialog.open()

    def close(self) -> None:
        self.dialog.close()

    async def load(self) -> None:
        """Fetch off the UI thread, then draw. Never raises to the caller."""
        try:
            infos = await asyncio.to_thread(
                self._fetch, self.api_key, self.symbols,
                as_of=self.as_of, windows=self.windows)
        except Exception as e:
            logger.error(f"symbol_info_panel: loading {self.symbols} failed: {e}",
                         exc_info=True)
            self._render_error(str(e))
            return
        self.render(infos)

    def _render_error(self, message: str) -> None:
        self.body.clear()
        with self.body:
            with ui.card().classes("w-full p-4"):
                ui.icon("error_outline", color="negative")
                ui.label("The symbol data could not be loaded.").classes("text-base")
                ui.label(message).classes("text-sm").style("color: #ff6b6b;")

    def render(self, infos: Mapping[str, SymbolInfo]) -> None:
        """Draw the whole body from scratch. ``clear()`` first, so a re-load
        replaces the previous render instead of stacking a second copy under it."""
        self.body.clear()
        ordered = [infos[s] for s in self.symbols if s in infos]
        with self.body:
            if len(self.symbols) > 1:
                render_comparison(infos, self.symbols)
                render_chart(ordered)
            for info in ordered:
                render_overview(info)
                render_etf_section(info)
                render_returns(info)
                if len(self.symbols) == 1:
                    render_chart([info])


def open_symbol_info(symbols: Sequence[str], *, api_key: str, as_of: date,
                     windows: Sequence[str] = WINDOWS,
                     fetch: Optional[Callable[..., Mapping[str, SymbolInfo]]] = None
                     ) -> SymbolInfoPanel:
    """Open the symbol-info dialog for one or more symbols.

    ``as_of`` is REQUIRED and is the only clock the panel has — pass
    ``date.today()`` from the caller, exactly as the data layer expects.

    Returns the panel so a caller can await ``panel.load_task`` (tests do); the
    UI itself does not need to.
    """
    panel = SymbolInfoPanel(symbols, api_key=api_key, as_of=as_of, windows=windows,
                            fetch=fetch)
    panel.open()
    panel.load_task = asyncio.create_task(panel.load())
    return panel
