"""The two broker-fact chips beside a symbol's ⓘ in the allocator.

Asked for from live use on 2026-09-05. The rule they all serve: a chip is a CLAIM, so
it is drawn only where the broker actually made one. "The broker did not say" draws
nothing — it must never render the same as "the broker said no".
"""
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    FRACTIONABLE_BADGE, fractionable_badge, leverage_badge,
)


class TestFractionable:
    def test_a_yes_earns_the_F(self):
        chip, tip = fractionable_badge(True)
        assert chip == FRACTIONABLE_BADGE
        assert 'partial share' in tip

    def test_a_no_also_earns_a_chip_but_says_whole_shares(self):
        # Worth showing: it is what makes a rounded-down order explicable on the page
        # that produced it. The caller strikes it through so the two never look alike.
        chip, tip = fractionable_badge(False)
        assert chip == FRACTIONABLE_BADGE
        assert 'Whole shares only' in tip

    def test_silence_draws_nothing(self):
        assert fractionable_badge(None) is None

    def test_the_two_answers_do_not_share_a_tooltip(self):
        assert fractionable_badge(True)[1] != fractionable_badge(False)[1]


class TestLeverage:
    def test_reg_t_reads_as_Lx_2(self):
        chip, tip = leverage_badge(0.5)
        assert chip == 'Lx:2'
        assert '50%' in tip

    def test_full_payment_reads_as_Lx_1_and_says_so(self):
        chip, tip = leverage_badge(1.0)
        assert chip == 'Lx:1'
        assert 'No leverage' in tip

    def test_an_unpublished_rate_draws_nothing_rather_than_1x(self):
        # 1x would assert the broker demands full payment. It said nothing at all.
        assert leverage_badge(None) is None

    def test_a_nonsense_rate_draws_nothing(self):
        assert leverage_badge(0.0) is None
        assert leverage_badge(-1.0) is None

    def test_the_chip_rounds_but_the_tooltip_keeps_the_exact_figure(self):
        # A real rate carries float noise -- the live account reported 2.01153x and
        # 1.99791x for what are both plainly Reg-T 2x -- so the CHIP rounds to make
        # the glance readable while the tooltip stays exact.
        chip, tip = leverage_badge(0.4971)
        assert chip == 'Lx:2'
        assert '2.01167x' in tip or '2.011' in tip

    def test_a_genuine_fraction_still_rounds_in_the_chip(self):
        chip, tip = leverage_badge(0.7)
        assert chip == 'Lx:1'
        assert '1.42857x' in tip


class TestRowFields:
    """``_symbol_fact_fields`` flattens the pair into what the Quasar template reads."""

    def _fields(self, facts):
        from ba2_trade_platform.ui.pages.portfolio_allocation import _symbol_fact_fields
        return _symbol_fact_fields(facts)

    def test_a_symbol_with_no_stored_row_draws_no_chips(self):
        # None is what ``live['symbol_facts'].get(symbol)`` returns for a symbol the
        # broker has never described, and it must reach the template as empty strings.
        f = self._fields(None)
        assert f['frac_badge'] == '' and f['lev_badge'] == ''
        assert f['frac_strike'] is False

    def test_a_full_answer_fills_both_chips(self):
        class _Row:
            fractionable = True
            initial_margin_rate = 0.5
        f = self._fields(_Row())
        assert f['frac_badge'] == FRACTIONABLE_BADGE
        assert f['lev_badge'] == 'Lx:2'
        assert f['frac_strike'] is False
        assert f['frac_tip'] and f['lev_tip']

    def test_an_explicit_no_is_marked_for_striking(self):
        class _Row:
            fractionable = False
            initial_margin_rate = None
        f = self._fields(_Row())
        assert f['frac_strike'] is True
        assert f['lev_badge'] == ''      # unknown rate stays blank

    def test_one_fact_known_and_the_other_not(self):
        class _Row:
            fractionable = None
            initial_margin_rate = 1.0
        f = self._fields(_Row())
        assert f['frac_badge'] == ''
        assert f['lev_badge'] == 'Lx:1'


class TestTooltipStatFields:
    """The ⓘ tooltip's yield / 1Y / 3Y lines.

    Asked for on 2026-09-05. Exactly ONE of stat_line / stat_pending / stat_error is
    ever non-empty: the template draws whichever is set, and '' is the only thing it
    treats as "draw nothing".
    """

    def _fields(self, stats):
        from ba2_trade_platform.ui.pages.portfolio_allocation import _symbol_stat_fields
        return _symbol_stat_fields(stats)

    def _one_of(self, f):
        return sum(1 for k in ('stat_line', 'stat_pending', 'stat_error') if f[k])

    def test_a_full_row_renders_all_three_figures(self):
        class _S:
            dividend_yield_pct, total_return_1y_pct, total_return_3y_pct = 69.73, 34.95, 126.25
            error = None
        f = self._fields(_S())
        assert '69.73%' in f['stat_line'] and '+34.95%' in f['stat_line'] and '+126.25%' in f['stat_line']
        assert self._one_of(f) == 1

    def test_a_non_payer_shows_a_real_zero_not_a_dash(self):
        # 0.00% is a measured fact about the fund; a dash would invent a missing one.
        class _S:
            dividend_yield_pct, total_return_1y_pct, total_return_3y_pct = 0.0, 12.0, 5.0
            error = None
        assert 'Yield 0.00%' in self._fields(_S())['stat_line']

    def test_an_unmeasured_window_is_a_dash_not_a_zero(self):
        class _S:
            dividend_yield_pct, total_return_1y_pct, total_return_3y_pct = 5.0, 12.0, None
            error = None
        assert '3Y -' in self._fields(_S())['stat_line']

    def test_a_symbol_not_yet_fetched_says_so(self):
        # A third state beside "0.00%" and a figure: we have not looked yet.
        f = self._fields(None)
        assert f['stat_line'] == '' and 'not fetched' in f['stat_pending']
        assert self._one_of(f) == 1

    def test_a_failed_fetch_shows_the_reason_rather_than_a_blank(self):
        class _S:
            error = 'FMP timed out'
        f = self._fields(_S())
        assert 'FMP timed out' in f['stat_error']
        assert f['stat_line'] == '' and f['stat_pending'] == ''
        assert self._one_of(f) == 1

    def test_the_returns_are_signed_and_the_yield_is_not(self):
        # A negative yield is not a thing; the returns' direction is the whole point.
        class _S:
            dividend_yield_pct, total_return_1y_pct, total_return_3y_pct = 3.0, -8.0, 4.0
            error = None
        line = self._fields(_S())['stat_line']
        assert 'Yield 3.00%' in line and '1Y -8.00%' in line and '3Y +4.00%' in line
