"""The "w/ div" half is coloured by ITS OWN sign in the symbol table, not only on the label bar.

The label bar has split its caption since 2026-09-07 -- operator's words, in
``split_unrealised_pnl``'s own docstring: "keep the raw red, and div green". The symbol table
under it never did: it rendered the whole caption in ONE span coloured by the raw P&L, so a row
like BLOX (-0.65% on price, +16.97% once dividends are counted) read entirely red. For an income
sleeve that is the opposite of what the second number says, and it is the whole reason the
sleeve is held.

Reported from the live screen, WHEEL_L1_HR: the label header showed its dividend figure green
while every row beneath it showed the same kind of figure red.
"""
import pytest

from ba2_common.core.portfolio_allocation import UnrealisedPnL, format_unrealised_pnl, split_unrealised_pnl
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    PNL_PCT_EPSILON, pnl_div_color, pnl_div_delta_color,
)

#: Straight off the reported screen: (symbol, amount, raw %, with-dividend %).
LIVE_ROWS = [
    ("BLOX", 0.64, 0.65, 16.97),
    ("CHPY", -10.98, -3.78, 6.34),
    ("GPTY", -13.46, -10.03, 3.34),
    ("MAGY", -6.26, -8.49, 7.54),
    ("GDXY", 28.90, 11.71, 26.38),
    ("GIAX", -3.36, -2.94, -2.55),
]


def _pnl(amount, pct, total_pct):
    return UnrealisedPnL(amount=amount, pct=pct, total_pct=total_pct)


class TestTheDividendHalfCarriesItsOwnVerdict:
    @pytest.mark.parametrize("symbol,amount,pct,total", LIVE_ROWS)
    def test_it_follows_the_dividend_figure_not_the_raw_one(self, symbol, amount, pct, total):
        colour = pnl_div_delta_color(_pnl(amount, pct, total))
        assert colour == ("positive" if total > 0 else "negative"), symbol

    def test_the_rows_that_DISAGREE_are_the_point(self):
        """Down on price, up on total return -- these are the ones a single colour lied about."""
        disagreeing = [(s, p, t) for s, _a, p, t in LIVE_ROWS if (p < 0) != (t < 0)]
        assert len(disagreeing) == 3, f"expected BLOX/CHPY/GPTY/MAGY shapes, got {disagreeing}"
        for symbol, _pct, total in disagreeing:
            assert pnl_div_delta_color(_pnl(-1.0, -1.0, total)) == "positive", symbol

    def test_a_flat_dividend_figure_is_not_a_verdict(self):
        assert pnl_div_delta_color(_pnl(5.0, 5.0, 0.0)) == "grey-5"
        assert pnl_div_delta_color(_pnl(5.0, 5.0, PNL_PCT_EPSILON)) == "grey-5"

    def test_nothing_measurable_is_neutral(self):
        assert pnl_div_delta_color(None) == "grey-5"
        assert pnl_div_delta_color(_pnl(1.0, 1.0, None)) == "grey-5"

    @pytest.mark.parametrize("symbol,amount,pct,total", LIVE_ROWS)
    def test_both_spellings_of_the_same_verdict_agree(self, symbol, amount, pct, total):
        """The bar styles inline and the table uses a Quasar name; they must not diverge."""
        pnl = _pnl(amount, pct, total)
        quasar, css = pnl_div_delta_color(pnl), pnl_div_color(pnl)
        positive_css = pnl_div_color(_pnl(1.0, 1.0, 1.0))
        negative_css = pnl_div_color(_pnl(-1.0, -1.0, -1.0))
        assert (css == positive_css) == (quasar == "positive"), symbol
        assert (css == negative_css) == (quasar == "negative"), symbol


class TestTheCaptionIsUnchanged:
    """Splitting is a COLOURING change; the text must be byte-identical."""

    @pytest.mark.parametrize("symbol,amount,pct,total", LIVE_ROWS)
    def test_head_plus_div_plus_tail_is_exactly_the_caption(self, symbol, amount, pct, total):
        pnl = _pnl(amount, pct, total)
        head, div, tail = split_unrealised_pnl(pnl)
        assert head + (div or "") + tail == format_unrealised_pnl(pnl), symbol

    def test_a_row_with_no_dividend_figure_renders_as_one_string(self):
        """v-if in the cell: no empty span, no stray spacing, same output as before."""
        head, div, tail = split_unrealised_pnl(_pnl(1.0, 1.0, None))
        assert div is None and tail == ""


def test_the_symbol_cell_actually_uses_the_dividend_colour():
    """Guards the wiring, which is the half that was missing -- the helper existed and was
    only ever called by the label bar."""
    import inspect
    from ba2_trade_platform.ui.pages import portfolio_allocation as mod

    src = inspect.getsource(mod)
    at = src.index("body-cell-pnl")
    cell = src[at:at + 700]
    assert "pnl_div_color" in cell, "the P&L cell does not colour the dividend half"
    assert "pnl_head" in cell and "pnl_tail" in cell, "the P&L cell is not split into parts"
    assert "pnl_div_delta_color(r.pnl)" in src, "the row does not carry the dividend colour"
