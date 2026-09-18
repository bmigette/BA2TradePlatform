"""The Current column paints when the price is within 5% of a bracket leg.

Lets the reader see which positions are about to resolve without comparing three numbers per
row by eye. The arithmetic is direction-dependent and that is the whole risk in it: a short
approaches its STOP by rising, so reading one with a long's rule paints it green exactly when
it is in trouble.
"""
import pytest

from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages.live_trades import (
    PRICE_NEAR_LEG_FRACTION, price_proximity_zone,
)

BUY, SELL = OrderDirection.BUY, OrderDirection.SELL


class TestTheLongCase:
    """The user's own example: SLDB, stop 8.53, price 8.81 -> red, because 8.81 < 8.53 x 1.05."""

    def test_the_reported_row_paints_red(self):
        assert price_proximity_zone(8.81, 14.65, 8.53, BUY) == 'sl'

    def test_just_inside_the_band_paints(self):
        assert price_proximity_zone(8.53 * 1.05, 14.65, 8.53, BUY) == 'sl'

    def test_just_outside_the_band_does_not(self):
        assert price_proximity_zone(8.53 * 1.0501, 14.65, 8.53, BUY) == ''

    def test_a_price_already_through_the_stop_still_paints(self):
        """Below the stop is not 'past caring' -- the position is live until the leg fills."""
        assert price_proximity_zone(7.00, 14.65, 8.53, BUY) == 'sl'

    def test_near_the_target_paints_green(self):
        assert price_proximity_zone(14.00, 14.65, 8.53, BUY) == 'tp'
        assert price_proximity_zone(14.65 * 0.95, 14.65, 8.53, BUY) == 'tp'

    def test_mid_bracket_paints_nothing(self):
        assert price_proximity_zone(11.50, 14.65, 8.53, BUY) == ''

    @pytest.mark.parametrize("current,tp,sl,expected", [
        (8.81, 14.65, 8.53, 'sl'),     # SLDB -- the row that prompted this
        (8.89, 14.59, 8.13, ''),       # ALHC, 8.13 x 1.05 = 8.54 < 8.89
        (57.19, None, 51.97, ''),      # FHI, stop only, 51.97 x 1.05 = 54.57 < 57.19
        (11.32, 17.26, 10.24, ''),     # CADL
        (14.73, 15.04, 12.81, 'tp'),   # NCLH, 15.04 x 0.95 = 14.29 <= 14.73
    ])
    def test_rows_from_the_live_screen(self, current, tp, sl, expected):
        assert price_proximity_zone(current, tp, sl, BUY) == expected

    def test_sana_is_outside_the_band_by_a_hair(self):
        """2.99 x 1.05 = 3.1395, and the price is 3.20 -- it must NOT paint."""
        assert 3.20 > 2.99 * 1.05
        assert price_proximity_zone(3.20, 5.34, 2.99, BUY) == ''


class TestTheShortCaseIsMirrored:
    """A short's stop is ABOVE and its target BELOW."""

    def test_rising_into_the_stop_paints_red(self):
        assert price_proximity_zone(9.80, 6.00, 10.00, SELL) == 'sl'

    def test_falling_into_the_target_paints_green(self):
        assert price_proximity_zone(6.20, 6.00, 10.00, SELL) == 'tp'

    def test_the_long_rule_would_have_got_both_backwards(self):
        """THE BUG THIS GUARDS: same numbers, opposite answers by direction."""
        near_short_stop = 9.80          # bad for a short, harmless for a long
        assert price_proximity_zone(near_short_stop, 6.00, 10.00, SELL) == 'sl'
        assert price_proximity_zone(near_short_stop, 14.00, 6.00, BUY) == ''

    def test_a_short_mid_bracket_paints_nothing(self):
        assert price_proximity_zone(8.00, 6.00, 10.00, SELL) == ''

    def test_a_plain_string_side_is_accepted(self):
        """Rows carry `txn.side.value` in places; both spellings must agree."""
        assert price_proximity_zone(9.80, 6.00, 10.00, 'SELL') == 'sl'
        assert price_proximity_zone(8.81, 14.65, 8.53, 'BUY') == 'sl'


class TestMissingData:
    def test_a_stop_only_row_is_judged_on_the_stop(self):
        """Most DS rows on the live screen carry no target at all."""
        assert price_proximity_zone(53.00, None, 51.97, BUY) == 'sl'
        assert price_proximity_zone(80.00, None, 51.97, BUY) == ''

    def test_a_target_only_row_is_judged_on_the_target(self):
        assert price_proximity_zone(14.00, 14.65, None, BUY) == 'tp'

    def test_no_legs_at_all_paints_nothing(self):
        assert price_proximity_zone(10.0, None, None, BUY) == ''

    @pytest.mark.parametrize("bad", [None, '', 'n/a', 0, -1])
    def test_an_unusable_price_paints_nothing_rather_than_guessing(self, bad):
        assert price_proximity_zone(bad, 14.65, 8.53, BUY) == ''

    @pytest.mark.parametrize("bad", [0, -5, None, ''])
    def test_a_non_positive_leg_is_not_a_leg(self, bad):
        assert price_proximity_zone(8.81, 14.65, bad, BUY) == ''


def test_the_stop_wins_when_both_are_within_range():
    """A bracket tighter than 2x the band satisfies both; the risk side is what matters."""
    assert price_proximity_zone(10.0, 10.2, 9.9, BUY) == 'sl'
    assert price_proximity_zone(10.0, 9.8, 10.1, SELL) == 'sl'


def test_the_band_is_five_percent():
    assert PRICE_NEAR_LEG_FRACTION == 0.05
