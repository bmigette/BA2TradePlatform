"""Regressions for review findings R1 and R2 (option lifecycle and money).

R1 — the Options tab priced a SPREAD through the single-contract path. A spread has contract
legs, so choosing `option_orders[0]` as the representative marked ONE leg against the parent's
NET premium and quantity: the review measured +$730 where the executable mark was +$370.

R2 — the live payoff builder treated every order with a contract symbol as a new position, so
the closing fills cancelled the entry structure and drew +$370 at every underlying price
instead of the structure's -$600/+$400 expiration outcomes.

Both fixtures below are the review's own (test_files/review_option_ui_20260920.py), with the
assertions INVERTED: the probes there confirm the defects, these confirm the fix.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, TransactionStatus
from ba2_trade_platform.core.option_pnl_display import option_transaction_pnl
from ba2_trade_platform.core.option_positions import opening_legs
from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab
from ba2_trade_platform.ui.pages.option_trades import OptionTradesTab

ENTRY_AT = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)


def order(oid, contract, side, strike, premium, **extra):
    values = dict(id=oid, account_id=1, transaction_id=7, contract_symbol=contract,
                  side=side, option_type=NS(value='call'), strike=strike,
                  open_price=premium, quantity=1, filled_qty=1, multiplier=100,
                  underlying_symbol='XYZ', expiry=date(2026, 9, 18),
                  asset_class=AssetClass.OPTION, status=OrderStatus.FILLED,
                  position_intent=None, created_at=ENTRY_AT)
    values.update(extra)
    return NS(**values)


TXN = NS(id=7, symbol='XYZ', side=OrderDirection.BUY, quantity=1,
         open_price=6, close_price=None, multiplier=100, expiry=date(2026, 9, 18),
         option_strategy='bull_call_spread', status=TransactionStatus.OPENED,
         expert_id=None, take_profit=None, stop_loss=None, created_at=ENTRY_AT,
         open_date=ENTRY_AT, close_date=None)

PARENT = order(0, None, OrderDirection.BUY, None, 6)
ENTRY = [order(1, 'XYZ_C95', OrderDirection.BUY, 95, 8),
         order(2, 'XYZ_C105', OrderDirection.SELL, 105, 2)]
CLOSING = [order(3, 'XYZ_C95', OrderDirection.SELL, 95, 13.3, position_intent='sell_to_close'),
           order(4, 'XYZ_C105', OrderDirection.BUY, 105, 3.6, position_intent='buy_to_close')]


def spread_curve(orders):
    """The expiration curve the live dialog would draw for this order history."""
    return LiveTradesTab._payoff_for(None, TXN, orders)


class TestPayoffIsTheEntryStructure:
    """R2: the order history is not a position."""

    def test_the_entry_structure_has_its_real_expiration_outcomes(self):
        chart = spread_curve(ENTRY)
        assert chart.available
        assert chart.payoff_at(90) == pytest.approx(-600)
        assert chart.payoff_at(110) == pytest.approx(400)

    def test_closing_fills_do_not_cancel_the_structure(self):
        # The review's exact defect: adding the exits flattened the curve to +370 everywhere.
        chart = spread_curve(ENTRY + CLOSING)
        assert chart.payoff_at(90) == pytest.approx(-600)
        assert chart.payoff_at(110) == pytest.approx(400)
        assert chart.breakevens == (101.0,)
        assert chart.max_profit.amount == 400.0
        assert chart.max_loss.amount == 600.0

    def test_a_cancelled_order_never_becomes_exposure(self):
        cancelled = order(5, 'XYZ_C110', OrderDirection.BUY, 110, 1,
                          status=OrderStatus.CANCELED, position_intent='buy_to_open')
        chart = spread_curve(ENTRY + [cancelled])
        assert chart.payoff_at(110) == pytest.approx(400)
        assert chart.max_profit.amount == 400.0
        assert len(chart.legs) == 2

    def test_a_pending_order_is_not_a_position(self):
        pending = order(6, 'XYZ_C110', OrderDirection.BUY, 110, 1,
                        status=OrderStatus.PENDING, filled_qty=None)
        assert len(opening_legs(TXN, ENTRY + [pending]).legs) == 2

    def test_a_structure_with_nothing_executed_has_no_curve(self):
        pending = [order(7, 'XYZ_C95', OrderDirection.BUY, 95, 8,
                         status=OrderStatus.PENDING, filled_qty=None)]
        assert not spread_curve(pending).available


class TestOpeningLegSet:
    """The normalisation rules the review asked for, one test each."""

    def test_exits_are_excluded_and_named(self):
        leg_set = opening_legs(TXN, ENTRY + CLOSING)
        assert [leg.contract_symbol for leg in leg_set.legs] == ['XYZ_C95', 'XYZ_C105']
        assert leg_set.count == 2
        assert leg_set.excluded.count('closing fill') == 2

    def test_a_close_is_detected_without_a_position_intent(self):
        # No intent recorded: the reversing fill still closes the earlier one.
        plain_close = order(8, 'XYZ_C95', OrderDirection.SELL, 95, 13.3, position_intent=None)
        leg_set = opening_legs(TXN, ENTRY + [plain_close])
        assert leg_set.count == 2
        assert 'closing fill' in leg_set.excluded

    def test_scaling_in_is_one_leg_at_the_weighted_premium(self):
        scaled = ENTRY + [order(9, 'XYZ_C95', OrderDirection.BUY, 95, 10,
                                position_intent='buy_to_open')]
        leg_set = opening_legs(TXN, scaled)
        assert leg_set.count == 2
        long_leg = leg_set.legs[0]
        assert long_leg.contracts == 2.0
        assert long_leg.entry_premium == pytest.approx(9.0)  # (8 + 10) / 2
        assert long_leg.fills == 2

    def test_a_partial_fill_uses_its_filled_quantity(self):
        partial = [order(10, 'XYZ_C95', OrderDirection.BUY, 95, 8,
                         status=OrderStatus.PARTIALLY_FILLED, quantity=5, filled_qty=2)]
        leg_set = opening_legs(TXN, partial)
        assert leg_set.count == 1
        assert leg_set.legs[0].contracts == 2.0
        assert leg_set.legs[0].size_source == 'filled_qty'

    def test_a_partial_fill_without_a_filled_quantity_is_refused_not_guessed(self):
        partial = [order(11, 'XYZ_C95', OrderDirection.BUY, 95, 8,
                         status=OrderStatus.PARTIALLY_FILLED, quantity=5, filled_qty=None)]
        leg_set = opening_legs(TXN, partial)
        assert leg_set.count == 0
        assert 'partially filled without a recorded filled quantity' in leg_set.excluded

    def test_a_fill_price_beats_the_requested_price(self):
        leg_set = opening_legs(TXN, [order(12, 'XYZ_C95', OrderDirection.BUY, 95, 8,
                                           filled_avg_price=8.25)])
        assert leg_set.legs[0].entry_premium == pytest.approx(8.25)

    def test_a_leg_without_its_own_multiplier_borrows_the_structure_term(self):
        leg_set = opening_legs(TXN, [order(13, 'XYZ_C95', OrderDirection.BUY, 95, 8, multiplier=None)])
        assert leg_set.legs[0].multiplier == 100
        assert leg_set.legs[0].multiplier_recorded is True

    def test_the_parent_is_kept_for_pricing_but_is_not_a_leg(self):
        leg_set = opening_legs(TXN, [PARENT] + ENTRY)
        assert leg_set.parent is PARENT
        assert leg_set.count == 2
        assert 'structure parent (no contract symbol)' in leg_set.excluded


class FakeSession:
    def __init__(self, orders):
        self._orders = orders

    def exec(self, _):
        return NS(all=lambda: list(self._orders))

    def get(self, *_):
        return NS(name='Fixture account')

    def close(self):
        pass


def loader_rows(orders, transactions=None):
    account = MagicMock(spec=OptionsAccountInterface)
    tab = NS(_totals={}, _refresh_totals=lambda: None, _contract_quote=lambda *a: None)
    with patch.object(OptionTradesTab, '__init__', lambda *a, **k: None), \
            patch('ba2_trade_platform.ui.pages.option_trades.get_account_instance_from_id',
                  lambda *a, **k: account):
        return OptionTradesTab._build_rows(tab, transactions or [TXN], {}, FakeSession(orders))


class TestSpreadIsPricedAsAStructure:
    """R1: the dispatch follows the structure, not the representative's shape."""

    def test_the_spread_seam_is_the_one_called(self):
        calls = []

        def spread(_account, _order):
            calls.append('spread')
            return {'amount': 370.0, 'percent': 6.16}

        def single(_account, _order):
            calls.append('single')
            return {'amount': 730.0, 'percent': 12.17}

        with patch.object(TradeConditions, '_get_spread_pnl_via_transaction', spread), \
                patch.object(TradeConditions, '_get_option_pnl_via_transaction', single):
            rows = loader_rows([PARENT] + ENTRY)

        assert calls == ['spread']
        assert '370' in rows[0]['current_pnl']
        assert '730' not in rows[0]['current_pnl']
        assert rows[0]['current_pnl_numeric'] == pytest.approx(6.16)

    def test_a_single_contract_still_uses_the_single_contract_seam(self):
        calls = []
        single_txn = NS(**{**TXN.__dict__, 'option_strategy': 'long_call'})
        with patch.object(TradeConditions, '_get_spread_pnl_via_transaction',
                          lambda a, o: calls.append('spread') or {'amount': 1.0, 'percent': 1.0}), \
                patch.object(TradeConditions, '_get_option_pnl_via_transaction',
                             lambda a, o: calls.append('single') or {'amount': 530.0, 'percent': 66.25}):
            rows = loader_rows([order(1, 'XYZ_C95', OrderDirection.BUY, 95, 8)], [single_txn])

        assert calls == ['single']
        assert '530' in rows[0]['current_pnl']

    def test_the_leg_count_is_the_structure_not_the_order_history(self):
        rows = loader_rows([PARENT] + ENTRY + CLOSING)
        assert rows[0]['leg_count'] == 2
        assert rows[0]['order_count'] == 5  # the raw history is still reported separately

    def test_the_dispatch_is_decided_by_the_count(self):
        # Directly on the seam entry: a leg-shaped order with a multi-leg count is a structure.
        seen = []
        option_transaction_pnl(
            MagicMock(spec=OptionsAccountInterface), ENTRY[0], opening_legs=2,
            single_leg=lambda a, o: seen.append('single'),
            multi_leg=lambda a, o: seen.append('spread') or {'amount': 1.0, 'percent': 1.0},
        )
        assert seen == ['spread']

    def test_the_old_shape_based_dispatch_still_works_without_a_count(self):
        seen = []
        option_transaction_pnl(
            MagicMock(spec=OptionsAccountInterface), ENTRY[0],
            single_leg=lambda a, o: seen.append('single') or {'amount': 1.0, 'percent': 1.0},
            multi_leg=lambda a, o: seen.append('spread'),
        )
        assert seen == ['single']
