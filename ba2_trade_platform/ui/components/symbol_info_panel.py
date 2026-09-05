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

import bisect
import statistics
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


#: How often the fund actually pays. DERIVED from the ex-dividend dates rather than
#: read from a provider field: FMP publishes no reliable frequency for the income ETFs
#: this panel is used to compare, and the dates are already fetched for the dividend
#: bars, so the answer is measured from the same evidence the chart draws.
#:
#: Matched on the MEDIAN gap, not the count in a trailing year: a fund that changed
#: cadence, listed mid-year, or skipped a month would be misread by a count, whereas
#: the median is the cadence it actually keeps. Bands are generous because real
#: schedules drift (a "monthly" payer lands anywhere from 28 to 35 days apart).
PAYOUT_BANDS: Tuple[Tuple[float, float, str], ...] = (
    (0.0, 10.0, "Weekly"),
    (10.0, 20.0, "Bi-weekly"),
    (20.0, 45.0, "Monthly"),
    (45.0, 120.0, "Quarterly"),
    (120.0, 250.0, "Semi-annual"),
    (250.0, 450.0, "Annual"),
)
#: Two dates make one gap, which is an interval and not yet a cadence. One dividend
#: is a FACT about the fund (it has paid once) and still cannot answer "how often".
PAYOUT_NEED_TWO = ("only {n} distribution(s) on record, so there is no interval to "
                   "measure a cadence from")
PAYOUT_NONE = "no distributions on record in the fetched history"
PAYOUT_IRREGULAR_FMT = "irregular (~{days:.0f}d median)"
PAYOUT_FMT = "{name} (~{days:.0f}d)"
LABEL_PAYOUT_FREQ = "Payout frequency"


def payout_frequency(info: SymbolInfo) -> Cell:
    """How often this symbol distributes, measured from its ex-dividend dates.

    THREE OUTCOMES, and they are different claims:
      * a cadence -- the median gap fell in a known band;
      * ``-`` (not applicable) -- the history was fetched and contains NO
        distributions, which is a fact about a non-payer and not a gap in our data;
      * ``n/a`` plus a reason -- the dividend history could not be fetched, or there
        are too few dates to measure an interval at all.

    A median outside every band still reports the number, marked irregular: "we
    measured 63 days and have no name for it" is worth more than n/a.
    """
    # THE WHOLE-SYMBOL FAILURE COMES FIRST. A symbol whose fetch failed outright has
    # an empty dividend list for the same reason it has no price -- nothing came back
    # -- and reporting that as "no distributions on record" would state a fact about
    # the fund from an absence of evidence about it.
    failed = failure_reason(info)
    if failed:
        return Cell.na(failed)
    series = info.series
    if series.is_unknown("dividends"):
        return Cell.na(series.why("dividends"))
    events = sorted(d.ex_date for d in (series.dividends or []) if d.ex_date)
    if not events:
        return Cell.not_applicable(PAYOUT_NONE)
    if len(events) < 2:
        return Cell.na(PAYOUT_NEED_TWO.format(n=len(events)))
    # Most recent 12 gaps: a fund that switched from quarterly to weekly should read
    # as what it does NOW, and its whole history would average the two into nonsense.
    gaps = [(b - a).days for a, b in zip(events, events[1:]) if (b - a).days > 0]
    if not gaps:
        return Cell.na(PAYOUT_NEED_TWO.format(n=len(events)))
    recent = gaps[-12:]
    median = statistics.median(recent)
    for low, high, name in PAYOUT_BANDS:
        if low < median <= high:
            return Cell.value(PAYOUT_FMT.format(name=name, days=median),
                              note=f"median of the last {len(recent)} interval(s) "
                                   f"between ex-dividend dates")
    return Cell.value(PAYOUT_IRREGULAR_FMT.format(days=median),
                      note=f"median of the last {len(recent)} interval(s) between "
                           f"ex-dividend dates; outside every named cadence")


#: Dividend GROWTH: what the fund pays now against what it paid before, per share.
#:
#: Computed from the same ex-dividend history as the cadence above, on TRAILING
#: 12-MONTH TOTALS rather than per-payment amounts -- a weekly payer that moved to
#: monthly would otherwise look like a 4x cut, and a fund that shifted an ex-date
#: across a year boundary would show growth it never delivered. Totals absorb both.
#:
#: Split-adjusted (``chart_amount``, i.e. FMP's adjDividend where present), because
#: an as-declared amount is not comparable across a split -- the one thing that would
#: silently turn a 2:1 split into a "50% dividend cut".
DIV_GROWTH_LABELS: Dict[int, str] = {1: "Dividend growth 1Y", 3: "Dividend growth 3Y (CAGR)"}
#: A window whose EARLIER year predates the fund's first distribution cannot be a
#: growth rate: it would compare a full year against a partial one and report the
#: listing as spectacular growth. TSMY (first paid 2024-08) is exactly this case.
DIV_GROWTH_TOO_YOUNG = ("first distribution on {first} — a {years}Y comparison would "
                        "measure a partial year against a full one, not growth")
DIV_GROWTH_NO_BASE = ("nothing was paid in the 12 months ending {end}, so there is no "
                      "base to grow from")
DIV_GROWTH_NEEDS_HISTORY = "no dividend history, so growth cannot be measured"


def _paid_between(dividends, start: date, end: date) -> Optional[float]:
    """Split-adjusted total paid in ``(start, end]``, or ``None`` if any amount is
    missing -- a partial sum would understate the total and read as a cut."""
    amounts = [d.chart_amount for d in dividends if start < d.ex_date <= end]
    if any(a is None for a in amounts):
        return None
    return float(sum(amounts))


def dividend_growth(info: SymbolInfo, years: int) -> Cell:
    """Per-share dividend growth over ``years``, as a percent. 3Y is annualised.

    1Y is the plain change between the trailing 12 months and the 12 before it. 3Y is
    a CAGR, not a total change: "grew 90% over three years" and "grew 24% a year" are
    the same fact, and only the second is comparable against the 1Y figure beside it.

    KNOWN SENSITIVITY, stated because the number looks more precise than it is: a
    trailing-12-month window can catch 12 or 13 payments from the same monthly payer
    depending on where the ex-dates fall, which is +/-8% of phantom growth on a
    perfectly flat distribution (and +/-2% for a weekly payer). Totals are still the
    right basis -- per-payment amounts turn a cadence change into a 4x move, which is
    a far larger lie -- but read a single-digit figure as noise, not as a trend. The
    note on the cell carries both totals so the reader can see what was divided.
    """
    failed = failure_reason(info)
    if failed:
        return Cell.na(failed)
    series = info.series
    if series.is_unknown("dividends"):
        return Cell.na(series.why("dividends"))
    events = [d for d in (series.dividends or []) if d.ex_date]
    if not events:
        return Cell.not_applicable(DIV_GROWTH_NEEDS_HISTORY)

    as_of = info.as_of
    recent_start = _years_before_date(as_of, 1)
    base_end = _years_before_date(as_of, years)
    base_start = _years_before_date(as_of, years + 1)

    first = min(d.ex_date for d in events)
    if first > base_start:
        return Cell.na(DIV_GROWTH_TOO_YOUNG.format(first=first.isoformat(), years=years))

    recent = _paid_between(events, recent_start, as_of)
    base = _paid_between(events, base_start, base_end)
    if recent is None or base is None:
        return Cell.na("some dividend records carry no amount, so a total would be "
                       "understated and the growth wrong")
    if base <= 0:
        return Cell.na(DIV_GROWTH_NO_BASE.format(end=base_end.isoformat()))

    ratio = recent / base
    pct = ((ratio ** (1.0 / years)) - 1.0) * 100.0 if years > 1 else (ratio - 1.0) * 100.0
    note = (f"${recent:,.4f} paid in the last 12m vs ${base:,.4f} in the 12m ending "
            f"{base_end.isoformat()}" + (f", annualised over {years}y" if years > 1 else ""))
    return Cell.value(fmt_signed_pct(pct), note=note)


def _years_before_date(day: date, years: int) -> date:
    """``day`` minus whole years, folding 29 February to 28."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


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
        # Beside the yield it explains: 51.94% paid weekly and 51.94% paid once a
        # year are the same number describing very different instruments.
        (LABEL_PAYOUT_FREQ, payout_frequency(info)),
        # Is the income growing or shrinking? A high trailing yield on a shrinking
        # distribution is a different proposition from the same yield on a rising one.
        (DIV_GROWTH_LABELS[1], dividend_growth(info, 1)),
        (DIV_GROWTH_LABELS[3], dividend_growth(info, 3)),
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
             categories: Sequence[str], *, digits: Optional[int] = None) -> List[Optional[float]]:
    """``None`` where the series has no observation — never ``0.0``.

    ECharts renders ``None`` as a gap and ``0`` as a data point on the zero
    line; the second would claim a measurement that was never made.

    ``digits`` rounds the plotted value. It exists for the TOOLTIP, which prints
    whatever is in the data array verbatim: an unrounded float arrives as
    ``126.23655913978493``, seventeen digits of which about four are meaningful and
    none are readable. Rounding here rather than formatting in the tooltip keeps it
    a plain option dict with no JavaScript in it. ``None`` leaves the value exactly
    as the data layer measured it — used where the sub-cent matters (dividends).
    """
    values = (mapping.get(c) for c in categories)
    if digits is None:
        return list(values)
    return [None if v is None else round(float(v), digits) for v in values]


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
        # 2dp: a close is quoted in cents, and the tooltip prints this verbatim.
        "data": _aligned(closes, categories, digits=2),
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
        # 2dp: a total-return percentage is read to the tenth at best.
        "data": _aligned(cumulative, categories, digits=2),
    }


def _dividend_series(info, categories):
    """Discrete cash paid per share, at the ex-date. A BAR, never a line: a line
    would imply the payment exists on the days between ex-dates."""
    amounts = {_iso(d.ex_date): d.chart_amount for d in info.series.dividends}
    return {
        "_kind": KIND_DIVIDEND, "_symbol": info.symbol,
        "name": f"{info.symbol} dividend paid (cash / share)",
        "type": "bar", "yAxisIndex": 2, "barMaxWidth": 8, "color": COLOR_DIVIDEND,
        # NO rounding: dividends live in the sub-cent (fmt_per_share uses 4dp),
        # so 2dp here would round a real $0.0008 payment to $0.00.
        "data": _aligned(amounts, categories),
    }


#: Vertical stagger for the two RIGHT-hand axis names. Both are drawn at the top of
#: their own axis line, and those lines are only ``offset`` apart horizontally -- so
#: two names of this length ("Total return % (reinvested)", "Dividend paid (cash /
#: share)") overlapped into an unreadable smear. ``nameGap`` lifts the second one
#: clear; it is a VERTICAL separation because the horizontal room is what ran out.
NAME_GAP_DEFAULT = 15
NAME_GAP_STACKED = 38


def _axis(name: str, *, position: str, offset: int = 0,
          formatter: str = "{value}", split_line: bool = True,
          name_gap: int = NAME_GAP_DEFAULT) -> Dict[str, Any]:
    return {
        "type": "value", "name": name, "position": position, "offset": offset,
        "scale": True, "nameGap": name_gap,
        "nameTextStyle": {"color": "#a0aec0"},
        "axisLabel": {"color": "#a0aec0", "formatter": formatter},
        "splitLine": {"show": split_line,
                      "lineStyle": {"color": "rgba(255, 255, 255, 0.05)"}},
    }


#: Chart height. Taller than the 380px it was: a total-return overlay spanning five
#: years of daily bars is a SHAPE comparison, and at 380px the lines of three symbols
#: sat inside ~250px of plot with the legend and axis eating the rest.
CHART_HEIGHT_PX = 560

#: The range buttons, in the order they are drawn. ``all`` is last and is the DEFAULT:
#: the chart has always opened on the full history and a range control that silently
#: cropped it on open would change what the reader is looking at without being asked.
CHART_RANGES: Tuple[Tuple[str, str], ...] = (
    ("YTD", "ytd"), ("1Y", "1y"), ("3Y", "3y"), ("5Y", "5y"), ("10Y", "10y"),
    ("Max", "all"),
)
DEFAULT_CHART_RANGE = "all"
#: Years back per key; ``ytd`` and ``all`` are handled separately.
_RANGE_YEARS: Dict[str, int] = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}


def _years_before(day: date, years: int) -> date:
    """``day`` minus whole years, surviving 29 February (-> 28 Feb)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def range_start_iso(categories: Sequence[str], key: str,
                    today: Optional[date] = None) -> Optional[str]:
    """The first category at or after the window ``key`` opens. Pure.

    Returns an ACTUAL MEMBER of ``categories`` rather than a computed date, because
    the x axis is categorical: ECharts matches ``dataZoom.startValue`` against the
    category values themselves, and a date that happens to be a weekend or a holiday
    is in no category at all -- the zoom would then silently fall back to the full
    range and the button would look broken.

    ``None`` when there are no categories. A window that starts before the data does
    yields the FIRST category: "10Y" on three years of history shows the three years
    there are, which is the honest answer, rather than an empty chart.
    """
    if not categories:
        return None
    if key == "all":
        return categories[0]
    day = today or date.today()
    if key == "ytd":
        start = day.replace(month=1, day=1)
    elif key in _RANGE_YEARS:
        start = _years_before(day, _RANGE_YEARS[key])
    else:
        return categories[0]
    wanted = _iso(start)
    index = bisect.bisect_left(list(categories), wanted)
    return categories[index] if index < len(categories) else categories[-1]


#: Why a range button is dead. A range that reaches further back than the data does
#: resolves to the whole history -- which is what is already on screen -- so pressing
#: it moves nothing. That is correct arithmetic and an invisible UI: reported as
#: "clicking 10Y or Max doesn't seem to do anything" on a symbol carrying five weeks
#: of prices, where EVERY button was a no-op. They are disabled and say why instead.
RANGE_UNAVAILABLE_FMT = ("Only {span} of history here ({first} to {last}), which is "
                         "less than this range covers — the chart already shows all "
                         "of it.")


def _describe_span(first: str, last: str) -> str:
    """``2026-07-29``/``2026-09-04`` -> ``5 weeks``. Approximate on purpose: the
    sentence explains why a button is inert, not how long the series is."""
    try:
        days = (date.fromisoformat(last) - date.fromisoformat(first)).days
    except (TypeError, ValueError):
        return "the available history"
    if days >= 730:
        return f"{days // 365} years"
    if days >= 365:
        return "about a year"
    if days >= 60:
        return f"about {days // 30} months"
    if days >= 14:
        return f"about {days // 7} weeks"
    days = max(days, 1)
    return f"{days} day" if days == 1 else f"{days} days"


def range_is_usable(categories: Sequence[str], key: str,
                    today: Optional[date] = None) -> bool:
    """Would pressing this range actually change what is shown? Pure.

    ``all`` is always usable -- it is the way back from a zoom, so it stays live even
    when it happens to be where you already are. Every other range is usable only if
    it CROPS something: one that resolves to the first category shows the whole
    history, which is what ``all`` shows, and pressing it does nothing observable.
    """
    if not categories:
        return False
    if key == "all":
        return True
    return range_start_iso(categories, key, today) != categories[0]


def _data_zoom(categories: Sequence[str], key: str = DEFAULT_CHART_RANGE) -> List[Dict[str, Any]]:
    """Scroll/pinch to zoom INSIDE the plot, plus a draggable slider under it.

    Both entries carry the same window so the slider handles and the plot agree the
    moment the chart is drawn; ECharts keeps them in step afterwards.
    """
    start = range_start_iso(categories, key)
    end = categories[-1] if categories else None
    window = {"startValue": start, "endValue": end}
    return [
        {"type": "inside", **window},
        {"type": "slider", **window, "height": 16, "bottom": 6,
         "backgroundColor": "transparent",
         "borderColor": "rgba(255, 255, 255, 0.1)",
         "fillerColor": "rgba(77, 171, 247, 0.18)",
         "handleStyle": {"color": "#4dabf7"},
         "textStyle": {"color": "#a0aec0"}},
    ]


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
        # ANCHORED, not left to ECharts' default placement: the legend and the zoom
        # slider both want the bottom of the chart, and unpositioned the legend landed
        # on top of the slider. Stacked explicitly from the bottom edge up:
        # slider 6..22, legend 36..54, then the x-axis labels, then the plot.
        "legend": {"data": [s["name"] for s in series], "bottom": 36,
                   "textStyle": {"color": "#a0aec0"}},
        # ``top`` clears the lifted dividend axis name; ``bottom`` clears the legend
        # AND the slider beneath it.
        "grid": {"left": 60, "right": 130, "top": 90, "bottom": 96,
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
                     formatter="${value}", split_line=False,
                     name_gap=NAME_GAP_STACKED),
             "scale": False, "min": 0},
        ],
        "dataZoom": _data_zoom(categories),
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
        # ``type: scroll`` and NOT the default. A plain horizontal legend WRAPS
        # onto a second row when the items do not fit, and ``grid.top`` is a fixed
        # 60px -- so the second row lands on top of the plot. That was survivable
        # while this dialog was full-screen; it is not now that Compare is a
        # ~1200px box (``PANEL_WIDTH_COMPARE``), where eight or nine tickers are
        # enough to wrap. Scrolling PAGES the legend instead, so every series stays
        # reachable and none of them is drawn over the chart.
        # Anchored above the zoom slider -- see the single chart for the stacking.
        "legend": {"data": [s["name"] for s in series],
                   "type": "scroll", "bottom": 36,
                   "textStyle": {"color": "#a0aec0"}},
        # ``bottom`` clears the legend and the zoom slider stacked beneath the plot;
        # ``top`` no longer reserves a legend row, since the legend moved down.
        "grid": {"left": 60, "right": 40, "top": 40, "bottom": 96,
                 "containLabel": True},
        "xAxis": {"type": "category", "data": categories,
                  "axisLabel": {"color": "#a0aec0"},
                  "axisLine": {"lineStyle": {"color": "rgba(255, 255, 255, 0.1)"}}},
        "yAxis": [_axis(AXIS_NAME_RETURN, position="left", formatter="{value}%")],
        "dataZoom": _data_zoom(categories),
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
        # The cadence belongs next to the yield in a COMPARISON above all: these are
        # income funds, and a 51.94% yield paid weekly is a different instrument from
        # the same number paid annually.
        (LABEL_PAYOUT_FREQ, overview(LABEL_PAYOUT_FREQ)),
        (DIV_GROWTH_LABELS[1], overview(DIV_GROWTH_LABELS[1])),
        (DIV_GROWTH_LABELS[3], overview(DIV_GROWTH_LABELS[3])),
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
    categories = _category_dates(*[i.series for i in infos])
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            title = (f"{infos[0].symbol} — price, dividends and total return"
                     if len(infos) == 1 else "Total return comparison")
            ui.label(title).classes("text-base font-bold")
            range_row = ui.row().classes("items-center gap-1 no-wrap")
        chart = ui.echart(build_chart_options(infos))             .style(f"width: 100%; height: {CHART_HEIGHT_PX}px;")

        def _set_range(key: str) -> None:
            """Move BOTH dataZoom entries -- the inside one and the slider -- together.

            Written straight onto the option dict and pushed with ``update()`` rather
            than dispatched as an action, because the slider handles are rendered from
            these values: moving only the inside zoom leaves the slider showing a
            window the plot is not in.
            """
            start = range_start_iso(categories, key)
            end = categories[-1] if categories else None
            for zoom in chart.options.get("dataZoom", []):
                zoom["startValue"] = start
                zoom["endValue"] = end
            chart.update()
            for button, button_key in buttons:
                # Only the colour changes -- ``props`` merges, so re-sending the
                # layout props would be noise and re-sending ``disable`` would
                # re-enable a range that has no history to show.
                button.props(f"color={'primary' if button_key == key else 'grey'}")

        buttons = []
        span = (_describe_span(categories[0], categories[-1]) if categories else "")
        with range_row:
            for label, key in CHART_RANGES:
                usable = range_is_usable(categories, key)
                # ``key=key`` binds the loop variable: a closure over ``key`` alone
                # would give every button the last range in the tuple.
                button = ui.button(label, on_click=lambda _=None, key=key: _set_range(key))                     .props("flat dense no-caps"
                           + (" color=primary" if key == DEFAULT_CHART_RANGE else " color=grey"))
                if not usable:
                    # DISABLED, not hidden: the reader asked for a ten-year view and
                    # deserves to be told there is not ten years of data, rather than
                    # to find the button missing or -- worse -- inert.
                    button.disable()
                    button.tooltip(RANGE_UNAVAILABLE_FMT.format(
                        span=span, first=categories[0], last=categories[-1]))
                buttons.append((button, key))
        for note in build_chart_notes(infos):
            render_cell(note)


def render_comparison(infos: Mapping[str, SymbolInfo], symbols: Sequence[str]) -> None:
    """The side-by-side table. Every requested symbol gets a column, including
    the ones that failed — see :func:`build_comparison`.

    THE ``overflow-x-auto`` WRAPPER IS A SAFETY VALVE, not the normal path. The
    grid stays ``w-full`` and FITS, because a comparison is read by looking across
    the columns and a table you have to scroll sideways to compare is not one. The
    wrapper is there for the case ``w-full`` cannot honour -- a dozen symbols in a
    950px dialog, where the cells' own min-content width wins -- and it turns that
    into a scroll instead of columns disappearing off the edge.

    Neither of those is what fixed the chart, and the distinction matters if this
    is ever revisited: the chart was overflowing because ``ui.scroll_area()``'s
    content box is ``position: absolute; width: auto; min-width: 100%``, so every
    percentage width inside it resolved against a box the content itself was
    setting. See ``SymbolInfoPanel._build`` -- the container is a plain
    ``overflow-y-auto`` div now, with a definite width, and nothing inside it can
    push the chart past the dialog again.
    """
    table = build_comparison(infos, symbols)
    with ui.card().classes("w-full"):
        ui.label("Comparison").classes("text-base font-bold")
        with ui.element("div").classes("w-full overflow-x-auto"):
            with ui.grid(columns=len(table.columns) + 1).classes(
                    "w-full gap-x-4 gap-y-1"):
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


# ---------------------------------------------------------------------------
# HOW BIG THE DIALOG IS
#
# It was ``full-width maximized``, i.e. the whole screen, for a panel of cards
# that is a column of label/value pairs. Sized like the Manage-labels dialog
# instead: a fixed sensible width, capped at the viewport, with the CONTENT
# scrolling rather than the box growing.
#
# EVERY ONE OF THESE IS AN INLINE STYLE AND NOT A TAILWIND CLASS, which is the
# only part of this that is not taste. Quasar sizes a dialog with
# ``.q-dialog__inner--minimized > div { max-width: 560px }`` -- two selectors, so
# it outranks Tailwind's single-class ``max-w-[900px]`` and the dialog silently
# stays 560px wide however carefully the class is applied. An inline declaration
# beats both. This is the same cascade trap ``important_color_style`` exists for
# on the allocation page, in its width-shaped form.
# ---------------------------------------------------------------------------

#: One symbol: a column of cards and a single chart. Wide enough for the 5-column
#: returns grid to keep its headings on one line.
PANEL_WIDTH_SINGLE = 900

#: Compare: the same cards PLUS an N-column comparison grid and a chart carrying
#: one line per symbol, so it needs the extra room to keep the columns readable.
PANEL_WIDTH_COMPARE = 1200

#: Never taller than the screen, and never edge to edge on a laptop.
PANEL_MAX_HEIGHT_VH = 90
PANEL_MAX_WIDTH_VW = 95


def panel_width_px(symbol_count: int) -> int:
    """How wide the dialog is for this many symbols. Pure."""
    return PANEL_WIDTH_COMPARE if symbol_count > 1 else PANEL_WIDTH_SINGLE


def panel_card_style(symbol_count: int) -> str:
    """The dialog card's inline geometry. Pure. See the block comment above for
    why this is inline and not a class."""
    return (f"width: {panel_width_px(symbol_count)}px; "
            f"max-width: {PANEL_MAX_WIDTH_VW}vw; "
            f"max-height: {PANEL_MAX_HEIGHT_VH}vh; "
            f"background: #1a1f2e;")


class SymbolInfoPanel:
    """A dialog showing one symbol in full, or several side by side.

    The dialog is built empty and filled by :meth:`load`, which does the blocking
    data-layer call on a worker thread (``asyncio.to_thread``) so fetching six
    symbols does not freeze the browser. Re-loading rebuilds the body with
    ``container.clear()`` — no ``ui.refreshable``.

    It is a SIZED box, not a full screen: ``panel_card_style`` fixes the width and
    caps the height, and the body scrolls inside it. The chart is built at
    :meth:`render` time -- after the dialog is open and therefore already at its
    final size -- so it initialises into the box it will live in rather than into
    a full screen it then has to shrink out of; NiceGUI's ``ui.echart`` carries a
    ``ResizeObserver`` on top of that, which is what makes a window resize reflow
    it instead of clipping the axis.
    """

    def __init__(self, symbols: Sequence[str], *, api_key: str, as_of: date,
                 windows: Sequence[str] = WINDOWS,
                 fetch: Optional[Callable[..., Mapping[str, SymbolInfo]]] = None,
                 display_names: Optional[Mapping[str, str]] = None):
        self.symbols = _requested_order(symbols)
        #: ``{SYMBOL: company name}``, for the title and the overview heading. A symbol
        #: absent here shows its TICKER ALONE -- never a guessed or prettified name, and
        #: never the ticker presented as though it were the company.
        self.display_names = dict(display_names or {})
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
        # No ``full-width maximized``: this is a panel, not a screen.
        self.dialog = ui.dialog().props(
            'transition-show="slide-up" transition-hide="slide-down"')
        with self.dialog:
            # ``min-h-0`` is what lets the scroll area below actually scroll -- a
            # flex child's implicit ``min-height: auto`` otherwise refuses to
            # shrink past its content and the card grows through the cap instead.
            with ui.card().classes(
                    "flex flex-col flex-nowrap min-h-0 overflow-hidden").style(
                        panel_card_style(len(self.symbols))):
                with ui.row().classes(
                        "w-full items-center justify-between p-2 shrink-0"):
                    ui.label(f"Symbol info — {self._titled(self.symbols)}").classes(
                        "text-lg font-bold")
                    ui.button(icon="close", on_click=self.close).props("flat round")
                # THE CONTENT SCROLLS, not the dialog. ``min-h-0`` again, for the
                # same reason, and this is the element the cap actually bites on.
                #
                # A PLAIN OVERFLOW CONTAINER AND NOT ``ui.scroll_area()``, which is
                # what this was. Quasar's ``.q-scrollarea__content`` is
                # ``position: absolute; width: auto; min-width: 100%`` -- a
                # shrink-to-fit box that grows to its widest child -- so every
                # ``w-full`` inside it resolved against a width that the content
                # itself was setting. The nine-column comparison grid widened that
                # box and the chart, at ``width: 100%``, obediently followed it 140px
                # past the right-hand edge of the dialog, with Quasar's horizontal
                # thumb hidden so it could not even be scrolled to. Measured, not
                # guessed: a 1084px canvas in a 1045px card at a 1100px window.
                #
                # A normal ``overflow-y-auto`` div takes its width from the flex
                # parent, so it is DEFINITE, and every percentage inside it is bounded
                # by the dialog. ``styles.css`` already themes the native scrollbar.
                with ui.column().classes(
                        "flex-grow w-full min-h-0 overflow-y-auto overflow-x-hidden"):
                    self.body = ui.column().classes("w-full gap-3 p-2")
        with self.body:
            ui.spinner(size="lg")
            ui.label("Loading…").style(_MUTED)

    def _titled(self, symbols: Sequence[str]) -> str:
        """``AAPL (Apple Inc.)`` for each symbol, or the bare ticker when unnamed.

        The ticker LEADS: it is what was clicked, what the row says, and what the rest
        of the panel is keyed on. The name is the annotation, not the identity.
        """
        parts = []
        for symbol in symbols:
            name = (self.display_names.get(symbol) or "").strip()
            parts.append(f"{symbol} ({name})" if name else symbol)
        return ", ".join(parts)

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
                     fetch: Optional[Callable[..., Mapping[str, SymbolInfo]]] = None,
                     display_names: Optional[Mapping[str, str]] = None
                     ) -> SymbolInfoPanel:
    """Open the symbol-info dialog for one or more symbols.

    ``as_of`` is REQUIRED and is the only clock the panel has — pass
    ``date.today()`` from the caller, exactly as the data layer expects.

    Returns the panel so a caller can await ``panel.load_task`` (tests do); the
    UI itself does not need to.
    """
    panel = SymbolInfoPanel(symbols, api_key=api_key, as_of=as_of, windows=windows,
                            fetch=fetch, display_names=display_names)
    panel.open()
    panel.load_task = asyncio.create_task(panel.load())
    return panel
