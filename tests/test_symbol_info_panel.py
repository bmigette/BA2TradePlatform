"""Display tests for the symbol-info panel (`ui/components/symbol_info_panel.py`).

The DATA layer (`ba2_providers.symbol_info`) is already tested to death; nothing
here re-tests a return calculation. What is tested here is the DISPLAY, and in
particular the one rule the display can silently break:

    **Unknown is not zero.** A measured ``0.0`` is a fact and renders as
    ``0.00%``; a ``None`` renders as ``n/a`` *plus the data layer's reason*, and
    never as ``0.00%``, never blank, never omitted.

Two tri-states the panel must not flatten, both pinned below:

* ``is_etf`` — ``False`` is "a stock, so no holdings" (a FACT); ``None`` is
  "could not determine" (UNKNOWN). Different text, different tooltip.
* a 3y/5y window on an 18-month-old symbol — ``None`` with a reason naming the
  earliest price. Not ``0.00%``, and not a since-inception figure relabelled.

How this runs
-------------
Two harnesses, deliberately:

1. **Pure helpers, called directly.** Every DECISION — formatting,
   unknown-vs-zero, comparison ordering, chart series assembly — lives in
   ``symbol_info_panel``'s pure layer and is unit-tested with no UI at all.
2. **The bare ``nicegui.Client`` harness** copied from
   ``tests/test_portfolio_allocation_page.py`` for the handful of assertions that
   are genuinely about the element tree (does the n/a carry its reason into a
   tooltip? does a failed symbol still get a column?). ``nicegui.testing`` is
   used nowhere in ``tests/`` and is not introduced here.

No network: the data layer is never called, only handed in as value objects.
No clock: ``AS_OF`` is a frozen literal, and it is deliberately NOT today.
Never ``caplog``: ``logger.py`` sets ``propagate = False``.
"""
import asyncio
from datetime import date

import pytest

from ba2_providers.symbol_info import (
    DividendEvent, EtfHolding, EtfProfile, IncomeInfo, SeriesBundle, SeriesPoint,
    SplitEvent, SymbolInfo, WindowReturn,
)

from ba2_trade_platform.ui.components import symbol_info_panel as panel


# A frozen "today". Never date.today(): the whole data layer takes as_of as an
# explicit argument precisely so no test needs a clock.
AS_OF = date(2026, 8, 20)


# ---------------------------------------------------------------------------
# Fixtures — hand-built value objects, exactly as the data layer would emit them
# ---------------------------------------------------------------------------

def _xlk_holdings():
    """The reference shape from the brief: XLK's top three by weight."""
    return (
        EtfHolding(symbol="NVDA", name="NVIDIA Corporation", weight_pct=14.46,
                   shares=1_000.0, market_value=1.0),
        EtfHolding(symbol="AAPL", name="Apple Inc.", weight_pct=12.26,
                   shares=1_000.0, market_value=1.0),
        EtfHolding(symbol="MSFT", name="Microsoft Corporation", weight_pct=9.90,
                   shares=1_000.0, market_value=1.0),
    )


def _xlk_etf():
    return EtfProfile(
        holdings_count=76, top10_weight_pct=61.25, asset_class="Equity",
        category="Technology", assets_under_management=119_630_000_000.0,
        # NAV deliberately != last_close, so "$282.14" identifies exactly ONE
        # rendered cell and the re-render test can count it.
        pe_ratio=39.32, expense_ratio=0.0008, nav=281.90,
        inception_date="1998-12-16", etf_company="SPDR",
        holdings=_xlk_holdings(),
    )


def _series(n=5, start=date(2026, 8, 10)):
    points = tuple(
        SeriesPoint(date=date.fromordinal(start.toordinal() + i),
                    close=100.0 + i, adj_close=100.0 + i,
                    cumulative_total_return_pct=i * 1.0)
        for i in range(n)
    )
    return SeriesBundle(points=points, dividends=(), splits=(),
                        start=points[0].date, end=points[-1].date)


def _returns(**overrides):
    base = {
        w: WindowReturn(window=w, total_return_pct=10.0, price_return_pct=9.0,
                        dividends_paid_per_share=1.0,
                        start_date=date(2025, 12, 31), end_date=AS_OF,
                        start_adj_close=100.0, end_adj_close=110.0)
        for w in ("ytd", "1y", "3y", "5y")
    }
    base.update(overrides)
    return base


def make_info(**overrides):
    """A fully-measured ETF SymbolInfo; override any group to make it unknown."""
    defaults = dict(
        symbol="XLK", as_of=AS_OF, is_etf=True, etf=_xlk_etf(),
        income=IncomeInfo(dividend_yield_pct=0.55, payout_ratio_pct=21.4,
                          trailing_12m_dividend_per_share=1.5424,
                          dividend_yield_pct_computed=0.54),
        returns=_returns(), series=_series(),
        history_start=date(1998, 12, 22), last_close=282.14,
    )
    defaults.update(overrides)
    return SymbolInfo(**defaults)


def _cells(rows):
    """``[(label, Cell), ...]`` -> ``{label: Cell}``."""
    return {label: cell for label, cell in rows}


# ===========================================================================
# 1. The Cell: the one place unknown-vs-measured is decided
# ===========================================================================

def test_a_measured_zero_renders_as_zero_not_as_na():
    """A non-payer's dividend yield really IS 0%. Rendering it 'n/a' is a lie."""
    income = IncomeInfo(dividend_yield_pct=0.0)
    cell = panel.field_cell(income, "dividend_yield_pct",
                            income.dividend_yield_pct, panel.fmt_pct)
    assert cell.unknown is False
    assert cell.text == "0.00%"
    assert cell.reason == ""


def test_an_unknown_never_renders_as_zero():
    """The inverse, and the more dangerous direction."""
    income = IncomeInfo(details={"dividend_yield_pct": "FMP TTM ratios could not be fetched"})
    cell = panel.field_cell(income, "dividend_yield_pct",
                            income.dividend_yield_pct, panel.fmt_pct)
    assert cell.unknown is True
    assert cell.text == panel.UNKNOWN_TEXT
    assert "0.00%" not in cell.text
    assert cell.text.strip() != ""


def test_an_unknown_carries_the_data_layers_reason_verbatim():
    reason = ("FMP TTM ratios could not be fetched — the yield is unknown, not zero")
    income = IncomeInfo(details={"dividend_yield_pct": reason})
    cell = panel.field_cell(income, "dividend_yield_pct", None, panel.fmt_pct)
    assert cell.reason == reason


def test_a_whole_group_failure_reason_reaches_every_field_in_it():
    """``Unknowable.why`` falls back to ``"*"``; the panel must use that fallback."""
    reason = "XLK could not be loaded (FMPError: 429)"
    ret = WindowReturn(details={"*": reason}, window="1y")
    cell = panel.field_cell(ret, "total_return_pct", ret.total_return_pct,
                            panel.fmt_signed_pct)
    assert cell.unknown is True
    assert cell.reason == reason


def test_an_unknown_with_no_recorded_reason_still_says_something():
    """A blank tooltip is as useless as no tooltip; there is always SOME text."""
    cell = panel.field_cell(IncomeInfo(), "payout_ratio_pct", None, panel.fmt_pct)
    assert cell.unknown is True
    assert cell.reason == panel.NO_REASON
    assert cell.reason != ""


def test_a_formatter_refuses_to_render_none_at_all():
    """The seam that makes 'unknown rendered as 0.00%' impossible to write by hand."""
    for formatter in (panel.fmt_pct, panel.fmt_signed_pct, panel.fmt_money,
                      panel.fmt_compact_money, panel.fmt_count, panel.fmt_per_share):
        with pytest.raises(ValueError):
            formatter(None)


# ===========================================================================
# 2. Formatting — the reference shapes from the brief
# ===========================================================================

@pytest.mark.parametrize("value,expected", [
    (61.25, "61.25%"),
    (0.0, "0.00%"),
    (39.32, "39.32%"),
])
def test_fmt_pct(value, expected):
    assert panel.fmt_pct(value) == expected


@pytest.mark.parametrize("value,expected", [
    (26.5, "+26.50%"),
    (-13.0, "-13.00%"),
    (0.0, "+0.00%"),
])
def test_fmt_signed_pct_always_shows_the_sign(value, expected):
    assert panel.fmt_signed_pct(value) == expected


@pytest.mark.parametrize("value,expected", [
    (119_630_000_000.0, "119.63B"),
    (1_500_000_000_000.0, "1.50T"),
    (2_400_000.0, "2.40M"),
    (950.0, "950.00"),
    (-3_000_000.0, "-3.00M"),
])
def test_fmt_compact_money_matches_the_reference_shape(value, expected):
    assert panel.fmt_compact_money(value) == expected


def test_fmt_count_thousands_separated():
    assert panel.fmt_count(76) == "76"
    assert panel.fmt_count(1234) == "1,234"


def test_fmt_per_share_keeps_the_cents_that_matter():
    assert panel.fmt_per_share(1.5424) == "$1.5424"
    assert panel.fmt_per_share(0.0) == "$0.0000"


# ===========================================================================
# 3. is_etf is TRI-STATE and the three states must not render alike
# ===========================================================================

def test_is_etf_true_says_etf():
    cell = panel.describe_is_etf(make_info(is_etf=True))
    assert cell.unknown is False
    assert "ETF" in cell.text


def test_is_etf_false_is_a_FACT_about_a_stock_not_an_unknown():
    cell = panel.describe_is_etf(make_info(symbol="AAPL", is_etf=False, etf=None))
    assert cell.unknown is False, "a stock is a measured fact, not a missing value"
    assert cell.text != panel.UNKNOWN_TEXT
    assert cell.reason == ""


def test_is_etf_none_is_an_unknown_with_a_reason():
    reason = ("the FMP company profile could not be fetched (FMPError: 429) — whether "
              "ZZZZ is an ETF is unknown, so its holdings are unknown too")
    cell = panel.describe_is_etf(
        make_info(symbol="ZZZZ", is_etf=None, etf=None, details={"is_etf": reason}))
    assert cell.unknown is True
    assert cell.text == panel.UNKNOWN_TEXT
    assert cell.reason == reason


def test_is_etf_none_and_is_etf_false_do_not_render_the_same():
    """The single mutation this file exists to catch first."""
    stock = panel.describe_is_etf(make_info(symbol="AAPL", is_etf=False, etf=None))
    dunno = panel.describe_is_etf(
        make_info(symbol="ZZZZ", is_etf=None, etf=None,
                  details={"is_etf": "no isEtf flag"}))
    assert stock.text != dunno.text
    assert stock.unknown != dunno.unknown


# ===========================================================================
# 4. Overview
# ===========================================================================

def test_overview_shows_the_measured_numbers():
    rows = _cells(panel.build_overview_rows(make_info()))
    assert rows[panel.LABEL_LAST_CLOSE].text == "$282.14"
    assert rows[panel.LABEL_DIV_YIELD].text == "0.55%"
    assert rows[panel.LABEL_PAYOUT].text == "21.40%"
    assert rows[panel.LABEL_TTM_DIV].text == "$1.5424"
    assert rows[panel.LABEL_HISTORY_START].text == "1998-12-22"


def test_overview_renders_a_non_payers_zero_yield_as_zero():
    info = make_info(income=IncomeInfo(dividend_yield_pct=0.0,
                                       trailing_12m_dividend_per_share=0.0,
                                       dividend_yield_pct_computed=0.0,
                                       payout_ratio_pct=0.0))
    rows = _cells(panel.build_overview_rows(info))
    assert rows[panel.LABEL_DIV_YIELD].text == "0.00%"
    assert rows[panel.LABEL_TTM_DIV].text == "$0.0000"
    assert all(not rows[k].unknown for k in
               (panel.LABEL_DIV_YIELD, panel.LABEL_TTM_DIV, panel.LABEL_PAYOUT))


def test_overview_renders_an_unfetchable_yield_as_na_with_its_reason():
    reason = "FMP TTM ratios could not be fetched — the yield is unknown, not zero"
    info = make_info(income=IncomeInfo(details={"dividend_yield_pct": reason,
                                                "payout_ratio_pct": reason}))
    rows = _cells(panel.build_overview_rows(info))
    assert rows[panel.LABEL_DIV_YIELD].text == panel.UNKNOWN_TEXT
    assert rows[panel.LABEL_DIV_YIELD].reason == reason


def test_a_missing_last_close_borrows_the_price_fetch_reason_rather_than_shrugging():
    """``SymbolInfo`` records no reason for ``last_close``/``history_start`` — the
    price failure is filed on ``series.details['points']``. Rendering
    'n/a — no reason recorded' when the reason is one attribute away is a
    self-inflicted unknown."""
    reason = ("the FMP price history could not be fetched (FMPError: 429) — "
              "no return is computable")
    info = make_info(last_close=None, history_start=None,
                     series=SeriesBundle(details={"points": reason}))
    rows = _cells(panel.build_overview_rows(info))
    assert rows[panel.LABEL_LAST_CLOSE].unknown is True
    assert rows[panel.LABEL_LAST_CLOSE].reason == reason
    assert rows[panel.LABEL_HISTORY_START].reason == reason


def test_every_overview_label_is_present_even_when_everything_is_unknown():
    """No field may be silently omitted just because it could not be measured."""
    info = SymbolInfo(symbol="ZZZZ", as_of=AS_OF, details={"*": "total failure"})
    rows = panel.build_overview_rows(info)
    labels = [label for label, _ in rows]
    for expected in (panel.LABEL_LAST_CLOSE, panel.LABEL_TYPE, panel.LABEL_DIV_YIELD,
                     panel.LABEL_PAYOUT, panel.LABEL_TTM_DIV, panel.LABEL_HISTORY_START):
        assert expected in labels
    assert all(cell.text == panel.UNKNOWN_TEXT for _, cell in rows)
    assert all(cell.reason for _, cell in rows), "an n/a with no reason is useless"


# ===========================================================================
# 5. Returns — YTD / 1y / 3y / 5y, and the windows a young symbol cannot fill
# ===========================================================================

def test_returns_are_listed_in_the_canonical_window_order():
    """Dict order is the caller's, not the reader's; the panel imposes its own."""
    scrambled = {w: _returns()[w] for w in ("5y", "ytd", "3y", "1y")}
    rows = panel.build_returns_rows(make_info(returns=scrambled))
    assert [r.window for r in rows] == ["ytd", "1y", "3y", "5y"]
    assert [r.label for r in rows] == ["YTD", "1Y", "3Y", "5Y"]


def test_a_measured_total_return_is_signed_and_breaks_out_price_and_dividends():
    rows = {r.window: r for r in panel.build_returns_rows(make_info())}
    assert rows["1y"].total.text == "+10.00%"
    assert rows["1y"].price.text == "+9.00%"
    assert rows["1y"].dividends.text == "$1.0000"
    assert rows["1y"].period.text == "2025-12-31 → 2026-08-20"


def test_a_flat_window_is_zero_percent_not_na():
    """``total_return_pct == 0.0`` is a measured flat window, per the data layer's
    own FACT/UNKNOWN table."""
    flat = WindowReturn(window="1y", total_return_pct=0.0, price_return_pct=0.0,
                        dividends_paid_per_share=0.0,
                        start_date=date(2025, 8, 20), end_date=AS_OF)
    rows = {r.window: r for r in panel.build_returns_rows(make_info(returns={"1y": flat}))}
    assert rows["1y"].total.unknown is False
    assert rows["1y"].total.text == "+0.00%"
    assert rows["1y"].dividends.text == "$0.0000"


def test_a_3y_window_on_an_18_month_old_symbol_is_na_with_the_earliest_price_named():
    """The headline case from the brief. Not 0%, and not a since-inception figure."""
    reason = ("a 3y return needs a price on or before 2023-08-20, but the earliest price "
              "available is 2025-02-14 (18.2 months of history) — not computable")
    young = WindowReturn(details={"*": reason}, window="3y",
                         end_date=AS_OF, end_adj_close=44.0)
    rows = {r.window: r for r in panel.build_returns_rows(make_info(returns={"3y": young}))}
    cell = rows["3y"].total
    assert cell.unknown is True
    assert cell.text == panel.UNKNOWN_TEXT
    assert cell.text != "0.00%" and cell.text != "+0.00%"
    assert cell.reason == reason
    assert "2025-02-14" in cell.reason


def test_a_split_inside_the_window_suppresses_the_price_return_but_not_the_total():
    """``total_return_pct`` comes from adjClose and is split adjusted; only the
    price-only companion is unsafe. Showing both as n/a would understate what
    we know."""
    reason = ("a split falls inside the window (2024-06-10 (10-for-1)) — the unadjusted "
              "close is not comparable across it; use total_return_pct, which is split "
              "adjusted")
    split_window = WindowReturn(details={"price_return_pct": reason}, window="5y",
                                total_return_pct=412.0, price_return_pct=None,
                                dividends_paid_per_share=0.64,
                                start_date=date(2021, 8, 20), end_date=AS_OF)
    rows = {r.window: r for r in panel.build_returns_rows(make_info(returns={"5y": split_window}))}
    assert rows["5y"].total.text == "+412.00%"
    assert rows["5y"].total.unknown is False
    assert rows["5y"].price.unknown is True
    assert rows["5y"].price.reason == reason


def test_unfetchable_dividends_make_the_window_dividend_na_not_zero():
    reason = "dividend history could not be fetched — paid dividends are unknown, not zero"
    ret = WindowReturn(details={"dividends_paid_per_share": reason}, window="1y",
                       total_return_pct=8.0, price_return_pct=8.0,
                       start_date=date(2025, 8, 20), end_date=AS_OF)
    rows = {r.window: r for r in panel.build_returns_rows(make_info(returns={"1y": ret}))}
    assert rows["1y"].dividends.unknown is True
    assert rows["1y"].dividends.reason == reason


def test_a_window_the_data_layer_never_returned_is_still_a_visible_row():
    """A row that vanishes reads as 'nothing to report'. Absence needs saying."""
    rows = {r.window: r for r in panel.build_returns_rows(make_info(returns={}))}
    assert set(rows) == set(WINDOWS_ORDER_FOR_TEST)
    for w in WINDOWS_ORDER_FOR_TEST:
        assert rows[w].total.unknown is True
        assert rows[w].total.reason


WINDOWS_ORDER_FOR_TEST = ("ytd", "1y", "3y", "5y")


# ===========================================================================
# 6. The ETF section — and the three things is_etf can be
# ===========================================================================

def test_the_etf_section_reproduces_the_reference_shape():
    """XLK, from the brief: 76 holdings, top-10 61.25%, Equity, Technology,
    119.63B, PE 39.32, NVIDIA 14.46% / Apple 12.26% / Microsoft 9.90%."""
    section = panel.build_etf_section(make_info())
    assert section.state == panel.ETF_STATE_ETF
    rows = _cells(section.rows)
    assert rows[panel.LABEL_HOLDINGS_COUNT].text == "76"
    assert rows[panel.LABEL_TOP10].text == "61.25%"
    assert rows[panel.LABEL_ASSET_CLASS].text == "Equity"
    assert rows[panel.LABEL_CATEGORY].text == "Technology"
    assert rows[panel.LABEL_AUM].text == "119.63B"
    assert rows[panel.LABEL_PE].text == "39.32"

    holdings = section.holdings
    assert [(h.symbol.text, h.weight.text) for h in holdings[:3]] == [
        ("NVDA", "14.46%"), ("AAPL", "12.26%"), ("MSFT", "9.90%")]
    assert holdings[0].name.text == "NVIDIA Corporation"
    assert [h.rank for h in holdings[:3]] == [1, 2, 3]


def test_a_stock_gets_no_etf_rows_because_that_is_a_fact_not_a_gap():
    section = panel.build_etf_section(make_info(symbol="AAPL", is_etf=False, etf=None))
    assert section.state == panel.ETF_STATE_STOCK
    assert section.rows == []
    assert section.holdings == []
    assert section.holdings_note.unknown is False, (
        "'a stock has no holdings' is a measured fact — rendering it n/a invents a gap")


def test_an_undetermined_symbol_keeps_every_etf_row_as_na_with_the_reason():
    """``is_etf=None`` is NOT ``is_etf=False``: we do not know there are no
    holdings, so every ETF field is an explicit unknown rather than absent."""
    reason = ("the FMP company profile for ZZZZ carried no 'isEtf' flag — whether it "
              "is an ETF is unknown")
    section = panel.build_etf_section(
        make_info(symbol="ZZZZ", is_etf=None, etf=None,
                  details={"is_etf": reason, "etf": reason}))
    assert section.state == panel.ETF_STATE_UNKNOWN
    rows = _cells(section.rows)
    for label in (panel.LABEL_HOLDINGS_COUNT, panel.LABEL_TOP10, panel.LABEL_ASSET_CLASS,
                  panel.LABEL_CATEGORY, panel.LABEL_AUM, panel.LABEL_PE):
        assert rows[label].text == panel.UNKNOWN_TEXT
        assert rows[label].reason == reason
    assert section.holdings_note.unknown is True
    assert section.holdings_note.reason == reason


def test_the_stock_and_the_undetermined_sections_are_not_the_same_section():
    stock = panel.build_etf_section(make_info(symbol="AAPL", is_etf=False, etf=None))
    dunno = panel.build_etf_section(make_info(symbol="ZZZZ", is_etf=None, etf=None,
                                              details={"is_etf": "no flag"}))
    assert stock.state != dunno.state
    assert stock.holdings_note.unknown != dunno.holdings_note.unknown
    assert len(stock.rows) != len(dunno.rows)


def test_a_fund_that_reports_no_constituents_is_a_fact_a_failed_fetch_is_not():
    """``holdings=()`` with no reason vs ``holdings=()`` WITH one — the data
    layer distinguishes them and so must the note."""
    empty = panel.build_etf_section(make_info(etf=EtfProfile(holdings=())))
    reason = ("FMP etf-holder could not be fetched — the constituents are unknown, "
              "not absent")
    failed = panel.build_etf_section(
        make_info(etf=EtfProfile(holdings=(), details={"holdings": reason})))
    assert empty.holdings_note.unknown is False
    assert failed.holdings_note.unknown is True
    assert failed.holdings_note.reason == reason
    assert empty.holdings_note.text != failed.holdings_note.text


def test_a_holding_with_no_weight_is_na_and_still_listed():
    """Dropping the row would quietly shrink the fund; a 0.00% would invent one."""
    etf = EtfProfile(holdings_count=2, holdings=(
        EtfHolding(symbol="AAA", name="Alpha", weight_pct=5.0, shares=None, market_value=None),
        EtfHolding(symbol="BBB", name="Beta", weight_pct=None, shares=None, market_value=None),
    ))
    holdings = panel.build_etf_section(make_info(etf=etf)).holdings
    assert len(holdings) == 2
    assert holdings[1].symbol.text == "BBB"
    assert holdings[1].weight.unknown is True
    assert holdings[1].weight.text == panel.UNKNOWN_TEXT
    assert holdings[1].weight.reason


def test_top10_weight_is_na_when_the_data_layer_could_not_certify_it():
    reason = ("1 of the top 10 holdings carry no weight (e.g. 'BBB') — summing over the "
              "gap would understate the concentration")
    etf = EtfProfile(holdings_count=76, top10_weight_pct=None,
                     details={"top10_weight_pct": reason}, holdings=_xlk_holdings())
    rows = _cells(panel.build_etf_section(make_info(etf=etf)).rows)
    assert rows[panel.LABEL_TOP10].text == panel.UNKNOWN_TEXT
    assert rows[panel.LABEL_TOP10].reason == reason


def test_the_holdings_list_is_capped_at_the_data_layers_top_n():
    many = tuple(EtfHolding(symbol=f"S{i}", name=f"Name {i}", weight_pct=float(50 - i),
                            shares=None, market_value=None) for i in range(30))
    holdings = panel.build_etf_section(make_info(etf=EtfProfile(holdings=many))).holdings
    assert len(holdings) == panel.TOP_N_HOLDINGS
    assert holdings[0].symbol.text == "S0"


# ===========================================================================
# 7. The chart — three series, three quantities, three axes
# ===========================================================================

def _bundle(points, dividends=(), splits=(), details=None):
    return SeriesBundle(details=dict(details or {}), points=tuple(points),
                        dividends=tuple(dividends), splits=tuple(splits),
                        start=date(2026, 8, 10), end=date(2026, 8, 14))


def _pt(day, close, cum):
    return SeriesPoint(date=date(2026, 8, day), close=close, adj_close=close,
                       cumulative_total_return_pct=cum)


def _series_by_kind(options):
    """``{kind: series-dict}`` for a single-symbol chart."""
    return {s["_kind"]: s for s in options["series"]}


def test_the_chart_has_the_three_series_on_three_different_axes():
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0), _pt(11, 105.0, 5.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 11), dividend=0.5, adj_dividend=0.5)]))
    options = panel.build_chart_options([info])
    kinds = _series_by_kind(options)
    assert set(kinds) == {panel.KIND_PRICE, panel.KIND_RETURN, panel.KIND_DIVIDEND}
    assert kinds[panel.KIND_PRICE]["type"] == "line"
    assert kinds[panel.KIND_RETURN]["type"] == "line"
    assert kinds[panel.KIND_DIVIDEND]["type"] == "bar", (
        "paid cash is a discrete event, not a continuous quantity — a line implies "
        "the dividend exists between ex-dates")
    axes = {k: s["yAxisIndex"] for k, s in kinds.items()}
    assert len({axes[panel.KIND_PRICE], axes[panel.KIND_RETURN],
                axes[panel.KIND_DIVIDEND]}) == 3
    assert len(options["yAxis"]) == 3


def test_the_dividend_bars_are_never_on_the_reinvested_lines_axis():
    """$/share and % are not the same quantity; sharing an axis asserts they are."""
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 10), dividend=0.5, adj_dividend=0.5)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    assert kinds[panel.KIND_DIVIDEND]["yAxisIndex"] != kinds[panel.KIND_RETURN]["yAxisIndex"]


def test_the_axes_are_labelled_with_their_three_different_units():
    info = make_info(series=_bundle([_pt(10, 100.0, 0.0)]))
    names = [axis["name"] for axis in panel.build_chart_options([info])["yAxis"]]
    assert names == [panel.AXIS_NAME_PRICE, panel.AXIS_NAME_RETURN, panel.AXIS_NAME_DIVIDEND]
    assert len(set(names)) == 3


def test_the_reinvested_line_is_exactly_the_data_layers_cumulative_figure():
    """The trap the data layer's docstring is about: adding the paid dividends to
    an adjClose-derived return double counts. The line is the cumulative figure,
    untouched."""
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0), _pt(11, 105.0, 5.0), _pt(12, 110.0, 10.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 11), dividend=0.5, adj_dividend=0.5)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    assert kinds[panel.KIND_RETURN]["data"] == [0.0, 5.0, 10.0]
    assert kinds[panel.KIND_RETURN]["data"] != [0.0, 5.5, 10.5]
    assert kinds[panel.KIND_DIVIDEND]["data"] == [None, 0.5, None]


def test_the_price_line_is_the_close_not_the_adjusted_close():
    """Two lines both derived from adjClose would say the same thing twice; the
    price line is what the instrument actually traded at."""
    points = (SeriesPoint(date=date(2026, 8, 10), close=100.0, adj_close=90.0,
                          cumulative_total_return_pct=0.0),)
    kinds = _series_by_kind(panel.build_chart_options([make_info(series=_bundle(points))]))
    assert kinds[panel.KIND_PRICE]["data"] == [100.0]


def test_the_dividend_bar_plots_the_split_adjusted_amount():
    """``chart_amount`` prefers ``adjDividend``: an as-declared amount is not
    comparable across a split, which a 3y/5y window will cross."""
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 10), dividend=1.00,
                                 adj_dividend=0.25)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    assert kinds[panel.KIND_DIVIDEND]["data"] == [0.25]


def test_a_dividend_whose_ex_date_has_no_price_bar_is_still_plotted():
    """Dropping it would understate the payout history; the x axis widens instead."""
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0), _pt(12, 102.0, 2.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 11), dividend=0.4, adj_dividend=0.4)]))
    options = panel.build_chart_options([info])
    assert options["xAxis"]["data"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    kinds = _series_by_kind(options)
    assert kinds[panel.KIND_DIVIDEND]["data"] == [None, 0.4, None]
    assert kinds[panel.KIND_PRICE]["data"] == [100.0, None, 102.0]
    assert kinds[panel.KIND_PRICE]["connectNulls"] is True


def test_a_dividend_record_with_no_amount_leaves_a_gap_not_a_zero_bar():
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0)],
        dividends=[DividendEvent(ex_date=date(2026, 8, 10), dividend=None,
                                 adj_dividend=None)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    assert kinds[panel.KIND_DIVIDEND]["data"] == [None]
    assert kinds[panel.KIND_DIVIDEND]["data"] != [0.0]


def test_a_split_in_the_window_is_annotated_on_the_price_line():
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0), _pt(11, 25.0, 0.0)],
        splits=[SplitEvent(date=date(2026, 8, 11), numerator=4.0, denominator=1.0,
                           ratio=4.0)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    marks = kinds[panel.KIND_PRICE]["markLine"]["data"]
    assert [m["xAxis"] for m in marks] == ["2026-08-11"]
    assert "4" in marks[0]["label"]["formatter"]


def test_an_unfetchable_split_history_is_a_note_not_a_silently_unmarked_chart():
    reason = "split history could not be fetched — the window may cross a split"
    info = make_info(series=_bundle([_pt(10, 100.0, 0.0)], details={"splits": reason}))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    assert kinds[panel.KIND_PRICE].get("markLine", {"data": []})["data"] == []
    notes = panel.build_chart_notes([info])
    assert any(n.unknown and n.reason == reason for n in notes)


def test_an_unfetchable_dividend_history_is_a_note_not_an_empty_bar_chart():
    """``dividends=()`` with a reason means UNKNOWN. An empty bar chart reads as
    'this symbol pays nothing', which is a different claim."""
    reason = "dividend history could not be fetched — the bar series is unknown, not empty"
    info = make_info(series=_bundle([_pt(10, 100.0, 0.0)], details={"dividends": reason}))
    notes = panel.build_chart_notes([info])
    assert any(n.unknown and n.reason == reason for n in notes)
    assert any("XLK" in n.text for n in notes)


def test_a_symbol_that_genuinely_paid_nothing_produces_no_unknown_note():
    info = make_info(series=_bundle([_pt(10, 100.0, 0.0)]))
    assert [n for n in panel.build_chart_notes([info]) if n.unknown] == []


# --- comparison chart -------------------------------------------------------

def test_comparing_symbols_overlays_one_reinvested_line_each_on_one_axis():
    a = make_info(symbol="XLK", series=_bundle([_pt(10, 100.0, 0.0), _pt(11, 105.0, 5.0)]))
    b = make_info(symbol="SPY", series=_bundle([_pt(10, 400.0, 0.0), _pt(11, 404.0, 1.0)]))
    options = panel.build_chart_options([a, b])
    assert [s["_symbol"] for s in options["series"]] == ["XLK", "SPY"]
    assert all(s["_kind"] == panel.KIND_RETURN for s in options["series"])
    assert all(s["yAxisIndex"] == 0 for s in options["series"])
    assert len(options["yAxis"]) == 1
    assert options["yAxis"][0]["name"] == panel.AXIS_NAME_RETURN


def test_a_comparison_aligns_symbols_with_different_calendars_on_a_shared_axis():
    a = make_info(symbol="XLK", series=_bundle([_pt(10, 100.0, 0.0), _pt(12, 102.0, 2.0)]))
    b = make_info(symbol="SPY", series=_bundle([_pt(11, 400.0, 0.0), _pt(12, 404.0, 1.0)]))
    options = panel.build_chart_options([a, b])
    assert options["xAxis"]["data"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    by_symbol = {s["_symbol"]: s["data"] for s in options["series"]}
    assert by_symbol["XLK"] == [0.0, None, 2.0]
    assert by_symbol["SPY"] == [None, 0.0, 1.0]


def test_a_failed_symbol_keeps_its_line_in_the_comparison_legend():
    """Vanishing from the chart is how a reader concludes they only asked for two."""
    good = make_info(symbol="XLK", series=_bundle([_pt(10, 100.0, 0.0)]))
    dead = SymbolInfo(symbol="ZZZZ", as_of=AS_OF,
                      details={"*": "ZZZZ could not be loaded (FMPError: 429)"})
    options = panel.build_chart_options([good, dead])
    assert [s["_symbol"] for s in options["series"]] == ["XLK", "ZZZZ"]
    assert "ZZZZ" in " ".join(options["legend"]["data"])
    assert all(v is None for v in
               next(s for s in options["series"] if s["_symbol"] == "ZZZZ")["data"])


def test_an_empty_symbol_list_still_produces_a_drawable_chart():
    options = panel.build_chart_options([])
    assert options["series"] == []
    assert options["xAxis"]["data"] == []


# ===========================================================================
# 8. Not-applicable — the THIRD rendering, distinct from measured and unknown
# ===========================================================================

def test_not_applicable_is_neither_a_value_nor_an_unknown():
    measured = panel.Cell.value("0.00%")
    unknown = panel.Cell.na("FMP TTM ratios could not be fetched")
    na_field = panel.Cell.not_applicable("AAPL is a stock — ETF holdings do not apply")
    assert len({measured.text, unknown.text, na_field.text}) == 3
    assert unknown.unknown is True
    assert na_field.unknown is False, (
        "'this question does not apply' is a fact about the instrument, not a gap "
        "in the data")
    assert na_field.note


# ===========================================================================
# 9. Comparison — several symbols, and the one that failed
# ===========================================================================

def _column(table, symbol):
    return next(c for c in table.columns if c.symbol == symbol)


def _row(table, label):
    return next(cells for lbl, cells in table.rows if lbl == label)


def test_the_comparison_keeps_the_callers_column_order():
    infos = {"SPY": make_info(symbol="SPY"), "XLK": make_info(symbol="XLK")}
    table = panel.build_comparison(infos, ["XLK", "SPY"])
    assert [c.symbol for c in table.columns] == ["XLK", "SPY"]


def test_the_comparison_collapses_duplicates_and_uppercases():
    infos = {"XLK": make_info(symbol="XLK")}
    table = panel.build_comparison(infos, ["xlk", "XLK"])
    assert [c.symbol for c in table.columns] == ["XLK"]


def test_a_failed_symbol_appears_in_the_comparison_as_failed_not_missing():
    """``get_symbols_info`` guarantees one entry per symbol whatever happens; a UI
    that filters the broken one out throws that guarantee away."""
    reason = "ZZZZ could not be loaded (FMPError: HTTP 429 rate limited)"
    infos = {"XLK": make_info(symbol="XLK"),
             "ZZZZ": SymbolInfo(symbol="ZZZZ", as_of=AS_OF, details={"*": reason},
                                returns={w: WindowReturn(details={"*": reason}, window=w)
                                         for w in ("ytd", "1y", "3y", "5y")})}
    table = panel.build_comparison(infos, ["XLK", "ZZZZ"])
    assert [c.symbol for c in table.columns] == ["XLK", "ZZZZ"]
    assert _column(table, "ZZZZ").failed is True
    assert _column(table, "ZZZZ").reason == reason
    assert _column(table, "XLK").failed is False
    dead = 1
    for _, cells in table.rows:
        assert len(cells) == 2
        assert cells[dead].unknown is True
        assert cells[dead].reason


def test_a_symbol_the_batch_never_returned_still_gets_a_column():
    """Defence in depth against the other way a symbol can vanish."""
    table = panel.build_comparison({"XLK": make_info(symbol="XLK")}, ["XLK", "GONE"])
    assert [c.symbol for c in table.columns] == ["XLK", "GONE"]
    assert _column(table, "GONE").failed is True
    assert "GONE" in _column(table, "GONE").reason


def test_the_comparison_shows_the_return_windows_and_the_income_fields():
    table = panel.build_comparison({"XLK": make_info(symbol="XLK")}, ["XLK"])
    labels = [label for label, _ in table.rows]
    for expected in ("YTD total return", "1Y total return", "3Y total return",
                     "5Y total return", panel.LABEL_DIV_YIELD, panel.LABEL_LAST_CLOSE,
                     panel.LABEL_TYPE):
        assert expected in labels
    assert _row(table, "1Y total return")[0].text == "+10.00%"


def test_an_etf_only_metric_is_not_applicable_for_a_stock_not_unknown():
    """A stock has no top-10 weight. That is a fact about the instrument, and it
    must not read like a failed holdings fetch."""
    infos = {"XLK": make_info(symbol="XLK"),
             "AAPL": make_info(symbol="AAPL", is_etf=False, etf=None)}
    table = panel.build_comparison(infos, ["XLK", "AAPL"])
    etf_cell, stock_cell = _row(table, panel.LABEL_TOP10)
    assert etf_cell.text == "61.25%"
    assert stock_cell.unknown is False
    assert stock_cell.text == panel.NOT_APPLICABLE_TEXT
    assert stock_cell.note


def test_an_etf_only_metric_IS_unknown_when_we_cannot_tell_what_the_symbol_is():
    """``is_etf=None`` again: not applicable and not known are different answers."""
    reason = "the FMP company profile could not be fetched (FMPError: 429)"
    infos = {"ZZZZ": make_info(symbol="ZZZZ", is_etf=None, etf=None,
                               details={"is_etf": reason, "etf": reason})}
    table = panel.build_comparison(infos, ["ZZZZ"])
    cell = _row(table, panel.LABEL_TOP10)[0]
    assert cell.unknown is True
    assert cell.reason == reason
    assert cell.text != panel.NOT_APPLICABLE_TEXT


def test_a_zero_yield_column_stays_zero_next_to_an_unknown_one():
    """Side by side is where flattening is most visible — and most misleading."""
    infos = {"BRK.B": make_info(symbol="BRK.B", is_etf=False, etf=None,
                                income=IncomeInfo(dividend_yield_pct=0.0)),
             "ZZZZ": make_info(symbol="ZZZZ", is_etf=False, etf=None,
                               income=IncomeInfo(details={"dividend_yield_pct": "no ratios"}))}
    payer, unknown = _row(panel.build_comparison(infos, ["BRK.B", "ZZZZ"]),
                          panel.LABEL_DIV_YIELD)
    assert payer.text == "0.00%" and payer.unknown is False
    assert unknown.text == panel.UNKNOWN_TEXT and unknown.unknown is True


def test_every_comparison_row_has_exactly_one_cell_per_column():
    infos = {"XLK": make_info(symbol="XLK"), "AAPL": make_info(symbol="AAPL",
                                                               is_etf=False, etf=None)}
    table = panel.build_comparison(infos, ["XLK", "AAPL", "GONE"])
    assert all(len(cells) == len(table.columns) for _, cells in table.rows)


# ===========================================================================
# 10. The rendered dialog — bare ``nicegui.Client``, exactly as
#     tests/test_portfolio_allocation_page.py does it. No ``nicegui.testing``.
# ===========================================================================

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-symbol-info-panel'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _texts(client):
    return [el._text for el in client.elements.values() if getattr(el, "_text", None)]


def _tooltips(client):
    from nicegui import ui as nicegui_ui
    return [el._text for el in client.elements.values()
            if isinstance(el, nicegui_ui.tooltip) and el._text]


def _open(client, symbols, infos, *, raises=None, record_thread=None):
    """Open the panel with a FAKE data layer and drive the load to completion.

    No network and no database: ``fetch`` is the seam ``open_symbol_info`` takes
    precisely so the display can be tested against hand-built value objects.
    """
    def fake_fetch(api_key, syms, *, as_of, windows):
        assert api_key == "TEST-KEY"
        assert as_of == AS_OF
        if raises is not None:
            raise raises
        return infos

    holder = {}

    async def _run():
        with client:
            holder["panel"] = panel.open_symbol_info(
                symbols, api_key="TEST-KEY", as_of=AS_OF, fetch=fake_fetch)
            await holder["panel"].load_task

    asyncio.run(_run())
    return holder["panel"]


@pytest.fixture
def to_thread_inline(monkeypatch):
    """Run ``asyncio.to_thread`` bodies inline AND count them.

    Inline because there is no event loop worth spinning up here; counted
    because "the fetch must not block the UI" is a behaviour, and a direct call
    would pass every other assertion in this file.
    """
    calls = []

    async def _inline(func, /, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline)
    return calls


def test_the_panel_draws_the_symbol_and_its_measured_values(nicegui_client, to_thread_inline):
    _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    text = " | ".join(_texts(nicegui_client))
    assert "XLK" in text
    assert "$282.14" in text
    assert "0.55%" in text
    assert "61.25%" in text
    assert "NVIDIA Corporation" in text


def test_the_fetch_is_offloaded_so_several_symbols_cannot_freeze_the_ui(
        nicegui_client, to_thread_inline):
    _open(nicegui_client, ["XLK", "SPY"],
          {"XLK": make_info(symbol="XLK"), "SPY": make_info(symbol="SPY")})
    assert len(to_thread_inline) == 1, (
        "the blocking data-layer call must go through asyncio.to_thread")


def test_a_rendered_na_carries_its_reason_into_a_tooltip(nicegui_client, to_thread_inline):
    """An n/a the reader cannot interrogate is barely better than a blank."""
    reason = "FMP TTM ratios could not be fetched — the yield is unknown, not zero"
    info = make_info(income=IncomeInfo(details={"dividend_yield_pct": reason}))
    _open(nicegui_client, ["XLK"], {"XLK": info})
    assert panel.UNKNOWN_TEXT in _texts(nicegui_client)
    assert reason in _tooltips(nicegui_client)


def test_a_measured_value_is_not_given_an_unknown_tooltip(nicegui_client, to_thread_inline):
    _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    assert panel.NO_REASON not in _tooltips(nicegui_client)


def test_a_stock_shows_no_holdings_table_and_says_why_not(nicegui_client, to_thread_inline):
    _open(nicegui_client, ["AAPL"],
          {"AAPL": make_info(symbol="AAPL", is_etf=False, etf=None)})
    text = " | ".join(_texts(nicegui_client))
    assert panel.STOCK_HOLDINGS_NOTE in text
    assert "NVIDIA Corporation" not in text
    assert panel.LABEL_TOP10 not in text


def test_an_undetermined_symbol_shows_the_etf_rows_as_na_not_as_a_stock(
        nicegui_client, to_thread_inline):
    reason = "the FMP company profile could not be fetched (FMPError: 429)"
    _open(nicegui_client, ["ZZZZ"],
          {"ZZZZ": make_info(symbol="ZZZZ", is_etf=None, etf=None,
                             details={"is_etf": reason, "etf": reason})})
    text = " | ".join(_texts(nicegui_client))
    assert panel.LABEL_TOP10 in text, "the row must still be listed, marked unknown"
    assert panel.STOCK_HOLDINGS_NOTE not in text
    assert reason in _tooltips(nicegui_client)


def test_comparing_symbols_draws_a_column_for_the_one_that_failed(
        nicegui_client, to_thread_inline):
    reason = "ZZZZ could not be loaded (FMPError: HTTP 429 rate limited)"
    infos = {"XLK": make_info(symbol="XLK"),
             "ZZZZ": SymbolInfo(symbol="ZZZZ", as_of=AS_OF, details={"*": reason})}
    _open(nicegui_client, ["XLK", "ZZZZ"], infos)
    text = " | ".join(_texts(nicegui_client))
    assert "XLK" in text
    assert "ZZZZ" in text, "a failed symbol must appear AS FAILED, not vanish"
    assert reason in _tooltips(nicegui_client)


def test_a_second_load_replaces_the_body_rather_than_appending_to_it(
        nicegui_client, to_thread_inline):
    p = _open(nicegui_client, ["XLK"], {"XLK": make_info()})

    async def _again():
        with nicegui_client:
            await p.load()

    asyncio.run(_again())
    assert _texts(nicegui_client).count("$282.14") == 1


def test_a_data_layer_explosion_becomes_a_banner_not_a_traceback(
        nicegui_client, to_thread_inline, monkeypatch):
    logged = []
    monkeypatch.setattr(panel.logger, "error", lambda msg, *a, **k: logged.append(str(msg)))
    _open(nicegui_client, ["XLK"], {}, raises=RuntimeError("FMP is down"))
    text = " | ".join(_texts(nicegui_client))
    assert "FMP is down" in text
    assert any("FMP is down" in m for m in logged)


def test_the_panel_avoids_the_forbidden_nicegui_constructs():
    """``ui.refreshable``/``ui.stepper``/``ui.aggrid`` are banned in this codebase;
    the panel rebuilds with ``container.clear()`` instead."""
    import ast
    import inspect
    source = inspect.getsource(panel)
    # AST, not a substring search: the module's own docstring NAMES the banned
    # constructs to explain why it avoids them, and a text scan would flag that
    # prose as a violation.
    tree = ast.parse(source)
    used = {f"{node.value.id}.{node.attr}" for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
    for banned in ("ui.refreshable", "ui.stepper", "ui.aggrid"):
        assert banned not in used
    assert "clear" in {node.attr for node in ast.walk(tree)
                       if isinstance(node, ast.Attribute)}


# ===========================================================================
# 11. Regressions found by mutation testing (each of these had a survivor)
# ===========================================================================

def test_the_window_order_is_the_panels_not_the_callers():
    """Survivor M19. The canonical-order test passed ``windows=WINDOWS``, which is
    ALREADY ordered — so ``ordered = list(windows)`` survived it. A caller that
    asks for 5y first must still get a shortest-first table."""
    rows = panel.build_returns_rows(make_info(), windows=("5y", "ytd", "3y", "1y"))
    assert [r.window for r in rows] == ["ytd", "1y", "3y", "5y"]


def test_a_sub_cent_expense_ratio_is_not_rounded_into_a_zero():
    """Survivor M28. ``:,.2f`` turns FMP's 0.0008 into '0.00' — a measured value
    displayed as the zero this panel exists to never invent."""
    assert panel.fmt_raw_number(0.0008) == "0.0008"
    rows = _cells(panel.build_etf_section(make_info()).rows)
    assert rows[panel.LABEL_EXPENSE].text == "0.0008"
    assert rows[panel.LABEL_EXPENSE].text != "0.00"
    assert rows[panel.LABEL_EXPENSE].unknown is False


def test_a_split_with_no_ratio_is_annotated_as_unknown_not_as_one_for_one():
    """Survivor M30. ``split.ratio or 1`` labels an unknown ratio '1-for-1',
    which is not merely missing — it asserts the split changed nothing."""
    info = make_info(series=_bundle(
        [_pt(10, 100.0, 0.0), _pt(11, 25.0, 0.0)],
        splits=[SplitEvent(date=date(2026, 8, 11), numerator=None, denominator=None,
                           ratio=None)]))
    kinds = _series_by_kind(panel.build_chart_options([info]))
    formatter = kinds[panel.KIND_PRICE]["markLine"]["data"][0]["label"]["formatter"]
    assert "1-for-1" not in formatter
    assert "unknown" in formatter.lower()


def test_a_symbol_missing_from_the_batch_gets_EXPLAINED_cells_not_blank_ones():
    """Survivor M34. The column existing is not enough — a column of empty strings
    reads as 'measured, and there was nothing there'."""
    table = panel.build_comparison({"XLK": make_info(symbol="XLK")}, ["XLK", "GONE"])
    for label, cells in table.rows:
        assert cells[1].unknown is True, label
        assert cells[1].text == panel.UNKNOWN_TEXT, label
        assert "GONE" in cells[1].reason, label


# ===========================================================================
# HOW BIG THE DIALOG IS
#
# It used to be ``full-width maximized`` -- the whole screen -- for a column of
# label/value cards. It is a sized box now, on the Manage-labels dialog's terms:
# a fixed width, capped at the viewport, with the CONTENT scrolling.
# ===========================================================================

def _dialog_card(client):
    from nicegui import ui as nicegui_ui
    cards = [el for el in client.elements.values()
             if isinstance(el, nicegui_ui.card)]
    assert cards, "the panel drew no card"
    return cards[0]


def test_the_panel_is_no_longer_a_FULL_SCREEN_dialog(nicegui_client,
                                                     to_thread_inline):
    opened = _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    props = opened.dialog._props

    assert 'maximized' not in props
    assert 'full-width' not in props


def test_ONE_symbol_gets_the_narrower_panel(nicegui_client, to_thread_inline):
    opened = _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    style = _dialog_card(nicegui_client)._style

    assert style['width'] == f'{panel.PANEL_WIDTH_SINGLE}px'
    assert opened.dialog is not None


def test_COMPARE_gets_the_wider_one_because_it_carries_more_series(
        nicegui_client, to_thread_inline):
    _open(nicegui_client, ["XLK", "SPY", "QQQ"],
          {s: make_info(symbol=s) for s in ("XLK", "SPY", "QQQ")})
    style = _dialog_card(nicegui_client)._style

    assert style['width'] == f'{panel.PANEL_WIDTH_COMPARE}px'
    assert panel.PANEL_WIDTH_COMPARE > panel.PANEL_WIDTH_SINGLE


def test_the_panel_is_CAPPED_at_the_viewport_in_both_directions(
        nicegui_client, to_thread_inline):
    """A fixed 1200px box on a 1024px laptop is a dialog with its right-hand
    column off the screen."""
    _open(nicegui_client, ["XLK", "SPY"],
          {s: make_info(symbol=s) for s in ("XLK", "SPY")})
    style = _dialog_card(nicegui_client)._style

    assert style['max-width'] == f'{panel.PANEL_MAX_WIDTH_VW}vw'
    assert style['max-height'] == f'{panel.PANEL_MAX_HEIGHT_VH}vh'
    assert panel.PANEL_MAX_HEIGHT_VH <= 90


def test_the_geometry_is_INLINE_because_quasar_outranks_a_class(
        nicegui_client, to_thread_inline):
    """THE reason this is a style and not ``max-w-[1200px]``.

    Quasar sizes a dialog with ``.q-dialog__inner--minimized > div { max-width:
    560px }`` -- two selectors, so it beats Tailwind's single-class arbitrary
    value and the dialog silently stays 560px wide however carefully the class was
    applied. An inline declaration outranks both. Same cascade trap as
    ``important_color_style`` on the allocation page, in its width-shaped form."""
    _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    card = _dialog_card(nicegui_client)

    assert 'width' in card._style and 'max-width' in card._style
    joined = ' '.join(card._classes)
    assert 'max-w-[' not in joined
    assert 'w-[' not in joined


def test_the_CONTENT_scrolls_rather_than_the_dialog_growing(nicegui_client,
                                                            to_thread_inline):
    """The cap is only a cap if something inside it can shrink. A flex child's
    implicit ``min-height: auto`` refuses to, which is why ``min-h-0`` is on both
    the card and the scrolling container.

    And it is a PLAIN ``overflow-y-auto`` container, not ``ui.scroll_area()``.
    Quasar's ``.q-scrollarea__content`` is ``position: absolute; width: auto;
    min-width: 100%`` -- a shrink-to-fit box that grows to its widest child -- so
    every ``w-full`` inside it resolved against a width the content was itself
    setting. Measured in a real browser at a 1100px window: the nine-column
    comparison grid widened that box and the chart, at ``width: 100%``, followed it
    to a 1084px canvas inside a 1045px card, past the right-hand edge, with
    Quasar's horizontal thumb hidden so it could not even be scrolled to. A normal
    overflow div takes a DEFINITE width from its flex parent and nothing inside it
    can do that."""
    from nicegui import ui as nicegui_ui

    _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    card = _dialog_card(nicegui_client)

    assert 'min-h-0' in card._classes
    assert 'overflow-hidden' in card._classes
    assert not [el for el in nicegui_client.elements.values()
                if isinstance(el, nicegui_ui.scroll_area)]
    scrollers = [el for el in nicegui_client.elements.values()
                 if 'overflow-y-auto' in getattr(el, '_classes', [])]
    assert len(scrollers) == 1
    assert 'flex-grow' in scrollers[0]._classes
    assert 'min-h-0' in scrollers[0]._classes


def test_the_chart_is_built_at_RENDER_and_therefore_inside_the_sized_box(
        nicegui_client, to_thread_inline):
    """A chart sized at BUILD time to a full screen does not reflow into a
    constrained container -- it overflows or clips its axis. This one is created
    by ``render()``, after the dialog is open and already at its final width, so
    it initialises into the box it will live in. ``ui.echart``'s own
    ``ResizeObserver`` covers the window-resize case on top of that."""
    from nicegui import ui as nicegui_ui

    opened = _open(nicegui_client, ["XLK"], {"XLK": make_info()})
    charts = [el for el in nicegui_client.elements.values()
              if isinstance(el, nicegui_ui.echart)]

    assert len(charts) == 1
    # Inside the BODY, which is what ``render()`` fills -- not inside the dialog
    # shell that ``_build`` draws before anything has been fetched.
    assert charts[0] in list(opened.body.descendants())
    assert 'width: 100%' in charts[0]._style.get('width', '100%') or \
        charts[0]._style.get('width') == '100%'


def test_the_COMPARE_legend_pages_instead_of_wrapping_over_the_plot():
    """``grid.top`` is a fixed 60px, so a legend that wraps to a second row is
    drawn on top of the chart. At the narrower Compare width eight or nine tickers
    are enough to wrap, so the legend scrolls."""
    infos = [make_info(symbol=s) for s in
             ("XLK", "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK", "SMH", "SOXX")]
    options = panel.build_comparison_chart_options(infos)

    assert options["legend"]["type"] == "scroll"
    assert len(options["legend"]["data"]) == 9
    assert options["grid"]["top"] == 60


def test_every_compare_series_still_gets_its_own_colour_up_to_the_palette():
    """"Confirm Compare's series are still distinguishable at the narrower
    width". They are, until the palette runs out -- which it does at nine."""
    symbols = [f"S{i}" for i in range(len(panel.COMPARE_COLORS) + 1)]
    options = panel.build_comparison_chart_options(
        [make_info(symbol=s) for s in symbols])
    colours = [s["color"] for s in options["series"]]

    assert len(set(colours[:len(panel.COMPARE_COLORS)])) == len(panel.COMPARE_COLORS)
    # ...and the ninth repeats the first. Documented, not fixed here.
    assert colours[len(panel.COMPARE_COLORS)] == colours[0]
